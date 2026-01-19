/*-------------------------------------------------------------------------
 *
 * indexbatch.c
 *	  amgetbatch implementation routines
 *
 * Portions Copyright (c) 1996-2025, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 *
 * IDENTIFICATION
 *	  src/backend/access/index/indexbatch.c
 *
 * INTERFACE ROUTINES
 *		index_batchscan_init - initialize fields for a batch index scan
 *		index_batchscan_reset - reset state needed by a batch index scan
 *		index_batchscan_end - free resources at end of batch index scan
 *		index_batchscan_mark_pos - set a mark from scanPos position
 *		index_batchscan_restore_pos - restore mark to scanPos position
 *		tableam_util_kill_scanpositem - record that scanPos item is dead
 *		tableam_util_free_batch - release resources associated with a batch
 *		indexam_util_batch_unlock - unlock batch's buffer lock
 *		indexam_util_batch_alloc - allocate a new batch
 *		indexam_util_batch_release - release allocated batch
 *
 *-------------------------------------------------------------------------
 */

#include "postgres.h"

#include "access/amapi.h"
#include "access/tableam.h"
#include "common/int.h"
#include "lib/qunique.h"
#include "utils/memdebug.h"

static int	batch_compare_int(const void *va, const void *vb);

/*
 * index_batchscan_init - initialize fields for a batch index scan.
 *
 * Sets up the batch ring buffer structure and its initial read position.
 * Also determines whether the scan will eagerly drop index page pins.
 *
 * Only call here when all of the index related fields in 'scan' were already
 * initialized.
 */
void
index_batchscan_init(IndexScanDesc scan)
{
	/* Both amgetbatch and amfreebatch must be present together */
	Assert(scan->indexRelation->rd_indam->amgetbatch != NULL);
	Assert(scan->indexRelation->rd_indam->amfreebatch != NULL);

	/* Tracks scan direction used to return last item */
	scan->batchringbuf.direction = NoMovementScanDirection;

	/* positions in the ring buffer of batches */
	batch_reset_pos(&scan->batchringbuf.scanPos);
	batch_reset_pos(&scan->batchringbuf.markPos);
	batch_reset_pos(&scan->batchringbuf.prefetchPos);

	scan->batchringbuf.markBatch = NULL;
	scan->batchringbuf.headBatch = 0;	/* initial head batch */
	scan->batchringbuf.nextBatch = 0;	/* initial batch starts empty */
	memset(&scan->batchringbuf.cache, 0, sizeof(scan->batchringbuf.cache));
	scan->batchringbuf.currentPrefetchBlock = InvalidBlockNumber;
	scan->batchringbuf.paused = false;

	scan->usebatchring = true;
}

/*
 * index_batchscan_reset - reset state used for a batch index scan
 *
 * Resets all loaded batches in the ring buffer, and resets the read position
 * to the initial state (or just initialize ring buffer state).  When
 * 'complete' is true, also frees the scan's marked batch (if any), which is
 * useful when ending an amgetbatch-based index scan.
 */
void
index_batchscan_reset(IndexScanDesc scan, bool complete)
{
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;

	batch_assert_batches_valid(scan);
	Assert(scan->xs_heapfetch);

	if (scan->xs_heapfetch->rs)
		read_stream_reset(scan->xs_heapfetch->rs);

	/* reset the positions */
	batch_reset_pos(&batchringbuf->scanPos);
	batch_reset_pos(&batchringbuf->prefetchPos);

	/*
	 * With "complete" reset, make sure to also free the marked batch, either
	 * by just forgetting it (if it's still in the ring buffer), or by
	 * explicitly freeing it.
	 */
	if (complete && unlikely(batchringbuf->markBatch != NULL))
	{
		BatchRingItemPos *markPos = &batchringbuf->markPos;
		IndexScanBatch markBatch = batchringbuf->markBatch;

		/* always reset the position, forget the marked batch */
		batchringbuf->markBatch = NULL;

		/*
		 * If we've already moved past the marked batch (it's not loaded into
		 * the ring buffer), free it explicitly now.  Otherwise, it'll be
		 * freed along with the other loaded batches.
		 */
		if (!INDEX_SCAN_BATCH_LOADED(scan, markPos->batch))
			tableam_util_free_batch(scan, markBatch);

		batch_reset_pos(&batchringbuf->markPos);
	}

	/* now release all other currently loaded batches */
	while (INDEX_SCAN_BATCH_COUNT(scan) > 0)
	{
		IndexScanBatch batch = INDEX_SCAN_BATCH(scan,
												batchringbuf->headBatch);

		tableam_util_free_batch(scan, batch);

		/* update the valid range, so that asserts / debugging works */
		batchringbuf->headBatch++;
	}

	/* reset relevant batch state fields */
	batchringbuf->headBatch = 0;	/* initial batch */
	batchringbuf->nextBatch = 0;	/* initial batch is empty */

	scan->finished = false;
	batchringbuf->currentPrefetchBlock = InvalidBlockNumber;
	batchringbuf->paused = false;

	batch_assert_batches_valid(scan);
}

/*
 * index_batchscan_end - free resources at end of batch index scan
 *
 * Called when an index scan is being ended, right before the owning scan
 * descriptor goes away.  Cleans up all batch related resources.
 */
void
index_batchscan_end(IndexScanDesc scan)
{
	/* Call amfreebatch and all remaining loaded batches (even markBatch) */
	index_batchscan_reset(scan, true);

	for (int i = 0; i < INDEX_SCAN_CACHE_BATCHES; i++)
	{
		IndexScanBatch cached = scan->batchringbuf.cache[i];

		if (cached == NULL)
			continue;

		if (cached->killedItems)
			pfree(cached->killedItems);
		if (cached->currTuples)
			pfree(cached->currTuples);
		pfree(cached);
	}
}

/*
 * index_batchscan_mark_pos - set a mark from scanPos position
 *
 * Saves the current read position and associated batch so that the scan can
 * be restored to this point later, via a call to index_batchscan_restore_pos.
 * The marked batch is retained and not freed until a new mark is set or the
 * scan ends (or until the mark is restored).
 */
void
index_batchscan_mark_pos(IndexScanDesc scan)
{
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;
	BatchRingItemPos *markPos = &batchringbuf->markPos;
	IndexScanBatch markBatch = batchringbuf->markBatch;

	/*
	 * Free the previous mark batch (if any), but only if the batch is no
	 * longer loaded into the ring buffer
	 */
	if (markBatch && !INDEX_SCAN_BATCH_LOADED(scan, markPos->batch))
	{
		batchringbuf->markBatch = NULL;
		tableam_util_free_batch(scan, markBatch);
	}

	/* copy the scan's position */
	batchringbuf->markPos = batchringbuf->scanPos;
	batchringbuf->markBatch = INDEX_SCAN_BATCH(scan,
											   batchringbuf->markPos.batch);

	/* scanPos/markPos must be valid */
	batch_assert_pos_valid(scan, &batchringbuf->markPos);
}

/*
 * index_batchscan_restore_pos - restore mark to scanPos position
 *
 * Restores the scan to a position previously saved by
 * index_batchscan_mark_pos.  The marked batch is restored as the current
 * batch, allowing the scan to resume from the marked position.  Also notifies
 * the index AM via a call to its amposreset routine, which allows it to
 * invalidate any private state that independently tracks scan progress (such
 * as array key state).
 *
 * Function currently just discards most batch ring buffer state.  It might
 * make sense to teach it to hold on to other nearby batches (still-held
 * batches that are likely to be needed once the scan finishes returning
 * matching items from the restored batch) as an optimization.  Such a scheme
 * would have the benefit of avoiding repeat calls to amgetbatch/repeatedly
 * reading the same index pages.
 */
void
index_batchscan_restore_pos(IndexScanDesc scan)
{
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;
	BatchRingItemPos *markPos = &batchringbuf->markPos;
	IndexScanBatch markBatch = batchringbuf->markBatch;

	/*
	 * XXX Disable this optimization when I/O prefetching is in use, at least
	 * until the possible interactions with prefetchPos are fully understood.
	 */
#if 0
	if (scanPos->batch == markPos->batch &&
		scanPos->batch == batchringbuf->headBatch)
	{
		/*
		 * We don't have to discard the scan's state after all, since the
		 * current headBatch is also the batch that we're restoring to
		 */
		scanPos->item = markPos->item;
		return;
	}
#endif

	/*
	 * Call amposreset to let index AM know to invalidate any private state
	 * that independently tracks the scan's progress
	 */
	scan->indexRelation->rd_indam->amposreset(scan, markBatch);

	/* Remove all batches from the ring buffer except for the marked batch */
	index_batchscan_reset(scan, false);

	/*
	 * "Append" markBatch, making the ring buffer appear as if it was the
	 * first batch ever returned by amgetbatch for the scan
	 */
	markPos->batch = 0;
	batchringbuf->scanPos = *markPos;
	batchringbuf->nextBatch = batchringbuf->headBatch = markPos->batch;
	INDEX_SCAN_BATCH_APPEND(scan, markBatch);
	Assert(INDEX_SCAN_BATCH(scan, batchringbuf->scanPos.batch) == markBatch);

	/*
	 * Note: markBatch.killedItems[] might already contain dead items, and
	 * might yet have more dead items saved.  tableam_util_free_batch is
	 * prepared for that.
	 */
}

/* ----------------------------------------------------------------
 *			utility functions called by table AMs
 * ----------------------------------------------------------------
 */

/*
 * tableam_util_kill_scanpositem - record that scanPos item is dead
 *
 * Records an offset to the scanBatch item of the currently-read tuple, saving
 * it in scanBatch's killedItems array. The items' index tuples will later be
 * marked LP_DEAD when current scanBatch is freed by amfreebatch routine (see
 * tableam_util_free_batch wrapper function).
 */
void
tableam_util_kill_scanpositem(IndexScanDesc scan)
{
	BatchRingItemPos *scanPos = &scan->batchringbuf.scanPos;
	IndexScanBatch scanBatch = INDEX_SCAN_BATCH(scan, scanPos->batch);

	batch_assert_pos_valid(scan, scanPos);

	if (scanBatch->killedItems == NULL)
		scanBatch->killedItems = palloc_array(int, scan->maxitemsbatch);
	if (scanBatch->numKilled < scan->maxitemsbatch)
		scanBatch->killedItems[scanBatch->numKilled++] = scanPos->item;
}

/*
 * tableam_util_free_batch - release resources associated with a batch
 *
 * Called by table AM's ordered index scan implementation when it is finished
 * with a batch and wishes to release its resources.
 *
 * This calls the index AM's amfreebatch callback to release AM-specific
 * resources, and to set LP_DEAD bits on the batch's index page (in index AMs
 * that implement that optimization).  Every amfreebatch routine must recycle
 * the underlying batch memory by passing it to indexam_util_batch_release.
 */
void
tableam_util_free_batch(IndexScanDesc scan, IndexScanBatch batch)
{
	batch_assert_batch_valid(scan, batch);

	/* don't free the batch that is marked */
	if (batch == scan->batchringbuf.markBatch)
		return;

	/*
	 * batch.killedItems[] is now in whatever order the scan returned items
	 * in.  We might have even saved the same item/TID twice.
	 *
	 * Sort and unique-ify killedItems[].  That way the index AM can safely
	 * assume that items will always be in their original index page order.
	 */
	if (batch->numKilled > 1)
	{
		qsort(batch->killedItems, batch->numKilled, sizeof(int),
			  batch_compare_int);
		batch->numKilled = qunique(batch->killedItems, batch->numKilled,
								   sizeof(int), batch_compare_int);
	}

	scan->indexRelation->rd_indam->amfreebatch(scan, batch);
}

/* ----------------------------------------------------------------
 *			utility functions called by amgetbatch index AMs
 *
 * These functions manage batch allocation, unlock/pin management, and batch
 * resource recycling.  Index AMs implementing amgetbatch should use these
 * rather than managing buffers directly.
 * ----------------------------------------------------------------
 */

/*
 * indexam_util_batch_unlock - unlock batch's shared buffer lock
 *
 * Unlocks caller's batch->buf in preparation for amgetbatch returning items
 * saved in that batch.  Performs extra steps required by amgetbatch callers
 * in passing.
 *
 * Only call here when a batch has one or more matching items to return using
 * amgetbatch (or for amgetbitmap to load into its bitmap of matching TIDs).
 * When an index page has no matches, it's always safe for index AMs to drop
 * both the lock and the pin for themselves.
 *
 * Note: It is convenient for index AMs that implement both amgetbatch and
 * amgetbitmap to consistently use the same batch management approach, since
 * that avoids introducing special cases to lower-level code.  We drop both
 * the lock and the pin on batch's page on behalf of amgetbitmap callers.
 * Such amgetbitmap callers must be careful to free all batches with matching
 * items once they're done saving the matching TIDs (there will never be any
 * calls to amfreebatch, so amgetbitmap must call indexam_util_batch_release
 * directly, in lieu of a deferred call to amfreebatch from core code).  We
 * never drop the pin for an amgetbatch caller, though.
 */
void
indexam_util_batch_unlock(IndexScanDesc scan, IndexScanBatch batch)
{
	/* batch must have one or more matching items returned by index AM */
	Assert(batch->firstItem >= 0 && batch->firstItem <= batch->lastItem);

	if (scan->usebatchring)
	{
		/* amgetbatch (not amgetbitmap) caller */
		Assert(scan->heapRelation != NULL);

		/*
		 * Have to set batch->lsn so that amfreebatch has a way to detect when
		 * concurrent heap TID recycling by VACUUM might have taken place.
		 * It'll only be safe to set any index tuple LP_DEAD bits when the
		 * page LSN hasn't advanced.
		 */
		batch->lsn = BufferGetLSNAtomic(batch->buf);

		/* Drop the lock */
		LockBuffer(batch->buf, BUFFER_LOCK_UNLOCK);

#ifdef USE_VALGRIND
		if (!RelationUsesLocalBuffers(scan->indexRelation))
			VALGRIND_MAKE_MEM_NOACCESS(BufferGetPage(batch->buf), BLCKSZ);
#endif

		/* table AM determines when it'll be safe to drop pins on batches */
	}
	else
	{
		/* amgetbitmap (not amgetbatch) caller */
		Assert(scan->heapRelation == NULL);

		/* drop both the lock and the pin */
		LockBuffer(batch->buf, BUFFER_LOCK_UNLOCK);

#ifdef USE_VALGRIND
		if (!RelationUsesLocalBuffers(scan->indexRelation))
			VALGRIND_MAKE_MEM_NOACCESS(BufferGetPage(batch->buf), BLCKSZ);
#endif
		ReleaseBuffer(batch->buf);
		batch->buf = InvalidBuffer;
	}
}

/*
 * indexam_util_batch_alloc - allocate a new batch
 *
 * Used by index AMs that support amgetbatch interface (both during amgetbatch
 * and amgetbitmap scans).
 *
 * Returns IndexScanBatch with space to fit scan->maxitemsbatch-many
 * BatchMatchingItem entries.  This will either be a newly allocated batch, or
 * a batch recycled from the cache managed by indexam_util_batch_release.  See
 * comments above indexam_util_batch_release.
 *
 * Index AMs that use batches should call this from either their amgetbatch or
 * amgetbitmap routines only.  Note in particular that it cannot safely be
 * called from a amfreebatch routine.
 */
IndexScanBatch
indexam_util_batch_alloc(IndexScanDesc scan)
{
	IndexScanBatch batch = NULL;

	/* First look for an existing batch from ring buffer */
	if (scan->usebatchring)
	{
		for (int i = 0; i < INDEX_SCAN_CACHE_BATCHES; i++)
		{
			if (scan->batchringbuf.cache[i] != NULL)
			{
				/* Return cached unreferenced batch */
				batch = scan->batchringbuf.cache[i];
				scan->batchringbuf.cache[i] = NULL;
				break;
			}
		}
	}

	if (!batch)
	{
		batch = palloc(offsetof(IndexScanBatchData, items) +
					   sizeof(BatchMatchingItem) * scan->maxitemsbatch);

		/*
		 * If we are doing an index-only scan, we need a tuple storage
		 * workspace. We allocate BLCKSZ for this, which should always give
		 * the index AM enough space to fit a full page's worth of tuples.
		 */
		batch->currTuples = NULL;
		if (scan->xs_want_itup)
			batch->currTuples = palloc(BLCKSZ);

		/*
		 * Batches allocate killedItems lazily (though note that cached
		 * batches keep their killedItems allocation when recycled)
		 */
		batch->killedItems = NULL;
	}

	/* xs_want_itup scans must get a currTuples space */
	Assert(!(scan->xs_want_itup && (batch->currTuples == NULL)));

	/* shared initialization */
	batch->buf = InvalidBuffer;
	batch->firstItem = -1;
	batch->lastItem = -1;
	batch->numKilled = 0;

	return batch;
}

/*
 * indexam_util_batch_release - release allocated batch
 *
 * This function is called by index AMs to release a batch allocated by
 * indexam_util_batch_alloc.  Batches are cached here for reuse (when scan
 * hasn't already finished) to reduce palloc/pfree overhead.
 *
 * It's safe to release a batch immediately when it was used to read a page
 * that returned no matches to the scan.  Batches actually returned by index
 * AM's amgetbatch routine (i.e. batches for pages with one or more matches)
 * must be released by calling here at the end of their amfreebatch routine.
 * Index AMs that uses batches should call here to release a batch from any of
 * their amgetbatch, amgetbitmap, and amfreebatch routines.
 */
void
indexam_util_batch_release(IndexScanDesc scan, IndexScanBatch batch)
{
	Assert(batch->buf == InvalidBuffer);

	if (scan->usebatchring)
	{
		/* amgetbatch scan caller */
		Assert(scan->heapRelation != NULL);

		if (scan->finished)
		{
			/* Don't bother using cache when scan is ending */
		}
		else
		{
			/*
			 * Use cache.  This is generally only beneficial when there are
			 * many small rescans of an index.
			 */
			for (int i = 0; i < INDEX_SCAN_CACHE_BATCHES; i++)
			{
				if (scan->batchringbuf.cache[i] == NULL)
				{
					/* found empty slot, we're done */
					scan->batchringbuf.cache[i] = batch;
					return;
				}
			}
		}

		/*
		 * Failed to find a free slot for this batch.  We'll just free it
		 * ourselves.  This isn't really expected; it's just defensive.
		 */
		if (batch->killedItems)
			pfree(batch->killedItems);
		if (batch->currTuples)
			pfree(batch->currTuples);
	}
	else
	{
		/* amgetbitmap scan caller */
		Assert(scan->heapRelation == NULL);
		Assert(batch->killedItems == NULL);
		Assert(batch->currTuples == NULL);
	}

	/* no free slot to save this batch (expected with amgetbitmap callers) */
	pfree(batch);
}

/*
 * batch_compare_int - qsort comparison function for int arrays
 */
static int
batch_compare_int(const void *va, const void *vb)
{
	int			a = *((const int *) va);
	int			b = *((const int *) vb);

	return pg_cmp_s32(a, b);
}
