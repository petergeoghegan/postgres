/*-------------------------------------------------------------------------
 *
 * indexbatch.c
 *	  Batch-based index scan infrastructure for the amgetbatch interface.
 *
 * This module provides the core infrastructure for batch-based index scans,
 * which allow index AMs to return multiple matching TIDs per page in a single
 * call.  The batch ring buffer is managed by the table AM, with help from us,
 * and with help from the ring buffer inline functions in relscan.h.  This
 * approach enables efficient prefetching of table AM blocks during ordered
 * index scans.
 *
 * The ring buffer loads batches in index key space order.  This allows the
 * table AM to maintain an adequate prefetch distance: its read stream
 * callback is thereby able to request table blocks referenced by index pages
 * that are well ahead of the current scan position's index page.
 *
 * There's three types of functions in this module:
 *
 * 1. Core batch scan lifecycle (index_batchscan_*): Functions that manage
 *    batch scan state including initialization, reset, cleanup, and the
 *    mark/restore operations needed for merge joins.  Called by indexam.c
 *    routines that manage index scans on behalf of the core executor.
 *
 * 2. Table AM utilities (tableam_util_*): Helper functions called by table
 *    AMs during amgetbatch index scans.  These handle cross-batch direction
 *    changes, recording dead items for later LP_DEAD marking, and freeing
 *    batches when the table AM is done with them.
 *
 * 3. Index AM utilities (indexam_util_*): Helper functions called by index
 *    AMs that implement the amgetbatch interface.  These manage batch
 *    allocation, index page buffer lock release, and batch memory recycling.
 *
 * These three layers coordinate without explicit coupling: the core lifecycle
 * functions assume that table AMs use scanPos/scanBatch and prefetchPos/
 * prefetchBatch in a standardized way (see heapam_handler.c for the reference
 * implementation), while table AMs assume that index AMs free and unlock
 * batches according to the conventions established here.  See indexam.sgml
 * for the full specification of the amgetbatch/amfreebatch contract.
 *
 * When the scan direction changes, the table AM must immediately stop its
 * read stream -- blocks already requested via prefetchPos will no longer
 * match what scanPos needs to return.  Crossing a batch boundary in a new
 * scan direction is a separate process, handled here: table AMs are required
 * to call tableam_util_batch_dirchange to leave the scan's batch ring buffer
 * in a consistent state.  The current implementation handles this by simply
 * discarding most batches.  The key invariant is that all loaded batches must
 * be in a consistent scan direction order.  (During cross-batch direction
 * changes, the current scanBatch will have its IndexScanBatchData.dir
 * flipped, but we have no provision for keeping all other loaded batches.
 * It's not clear that it'd be useful to hold onto them; the scan direction is
 * unlikely to change back.  The scan batch direction invariant keeps things
 * simple: it is convenient for most code that deals with batches to be able
 * to assume that the common case where scan direction never changes is the
 * only case.)
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 *
 * IDENTIFICATION
 *	  src/backend/access/index/indexbatch.c
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
 * Initialize fields for a batch index scan.
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

	scan->batchringbuf.scanPos.valid = false;
	scan->batchringbuf.markPos.valid = false;
	scan->batchringbuf.prefetchPos.valid = false;

	scan->batchringbuf.markBatch = NULL;
	scan->batchringbuf.headBatch = 0;	/* initial head batch */
	scan->batchringbuf.nextBatch = 0;	/* initial batch starts empty */
	memset(&scan->batchringbuf.cache, 0, sizeof(scan->batchringbuf.cache));
	scan->batchringbuf.currentPrefetchBlock = InvalidBlockNumber;
	scan->batchringbuf.paused = false;

	/*
	 * Start by resolving visibility for just one item, then gradually ramp up
	 * the number of items processed.
	 */
	scan->batchringbuf.vmItems = 1;

	scan->usebatchring = true;
}

/*
 * Reset state used for a batch index scan
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
	IndexScanBatch markBatch = batchringbuf->markBatch;
	bool		markBatchFreed = false;

	Assert(scan->xs_heapfetch);

	if (scan->xs_heapfetch->rs)
		read_stream_reset(scan->xs_heapfetch->rs);

	batchringbuf->scanPos.valid = false;
	batchringbuf->prefetchPos.valid = false;

	/*
	 * When called with "complete" we must make sure that markBatch is freed,
	 * and that all markBatch related state is reset
	 */
	if (complete && unlikely(markBatch != NULL))
	{
		/*
		 * We'll free markBatch during this call.
		 *
		 * Note: we must set batchringbuf.markBatch to NULL up front like this
		 * so that tableam_util_free_batch actually frees markBatch later on.
		 */
		batchringbuf->markBatch = NULL;
		batchringbuf->markPos.valid = false;
	}

	/*
	 * Release all currently loaded batches, being sure to avoid freeing
	 * markBatch (unless called with complete, where we're supposed to)
	 */
	while (index_scan_batch_count(scan) > 0)
	{
		IndexScanBatch batch = index_scan_batch(scan,
												batchringbuf->headBatch);

		if (complete || batch != markBatch)
		{
			markBatchFreed = (batch == markBatch);
			tableam_util_free_batch(scan, batch);
		}

		/* update the valid range, so that asserts / debugging works */
		batchringbuf->headBatch++;
	}

	if (complete && markBatch != NULL && !markBatchFreed)
	{
		/*
		 * We didn't free markBatch because it was no longer loaded in ring
		 * buffer.  Do so now instead.
		 */
		tableam_util_free_batch(scan, markBatch);
	}

	/* reset relevant batch state fields */
	batchringbuf->headBatch = 0;	/* initial batch */
	batchringbuf->nextBatch = 0;	/* initial batch is empty */

	batchringbuf->currentPrefetchBlock = InvalidBlockNumber;
	batchringbuf->paused = false;

	/* reset the visibility check batch size */
	batchringbuf->vmItems = 1;
}

/*
 * Free resources at end of batch index scan
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
 * Set a mark from scanPos position
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
	BatchRingItemPos *scanPos = &scan->batchringbuf.scanPos;
	BatchRingItemPos *markPos = &batchringbuf->markPos;
	IndexScanBatch scanBatch = index_scan_batch(scan, scanPos->batch);
	IndexScanBatch markBatch = batchringbuf->markBatch;
	bool		freeMarkBatch;

	/*
	 * Free the previous mark batch (if any) -- but only if it isn't our
	 * scanBatch (defensively make sure that markBatch isn't some later
	 * still-needed batch, too)
	 */
	if (!markBatch || markBatch == scanBatch)
	{
		/* Definitely no markBatch that we should free now */
		freeMarkBatch = false;
	}
	else if (likely(!index_scan_batch_loaded(scan, markPos->batch)))
	{
		/* Definitely have a no-longer-loaded markBatch to free */
		freeMarkBatch = true;
	}
	else
	{
		/*
		 * It looks like markBatch is loaded/still needed within batchringbuf.
		 *
		 * index_scan_batch_loaded indicates that markpos->batch is loaded
		 * already, but we cannot fully trust it here.  It's just about
		 * possible that markpos->batch falls within a since-recycled range of
		 * batch offset numbers (following uint8 overflow).
		 *
		 * Make sure that markBatch really is loaded by directly comparing it
		 * against all loaded batches.  We must not fail to release markBatch
		 * when nobody else will later on.
		 *
		 * Note: in practice we're very unlikely to end up here.  It is very
		 * atypical for an index scan on the inner side of a merge join to
		 * hold on to a mark that trails the current scanBatch this much.
		 */
		freeMarkBatch = true;	/* i.e. index_scan_batch_loaded lied to us */

		for (uint8 i = batchringbuf->headBatch; i != batchringbuf->nextBatch; i++)
		{
			if (index_scan_batch(scan, i) == markBatch)
			{
				/* index_scan_batch_loaded was right/no overflow happened */
				freeMarkBatch = false;
				break;
			}
		}
	}

	if (freeMarkBatch)
	{
		/* Free markBatch, since it isn't loaded/needed for batchringbuf */
		batchringbuf->markBatch = NULL; /* else call won't free markBatch */
		tableam_util_free_batch(scan, markBatch);
	}

	/* copy the scan's position */
	batchringbuf->markPos = *scanPos;
	batchringbuf->markBatch = scanBatch;
}

/*
 * Restore mark to scanPos position
 *
 * Restores the scan to a position saved by index_batchscan_mark_pos earlier.
 * The scan's markPos becomes its scanPos.  The marked batch is restored as
 * the current scanBatch when needed.
 *
 * We just discard all batch's (other than markBatch/restored scanBatch),
 * except when markBatch is already the scan's current scanBatch.  We always
 * invalidate prefetchPos and close the scan's read stream, if any.  This
 * approach keeps things simple for table AMs: most code that deals with
 * batches is thereby able to assume that the common case where scan direction
 * never changes is the only case (tableam_util_batch_dirchange takes a
 * similar approach to handling a cross-batch change in scan direction).
 */
void
index_batchscan_restore_pos(IndexScanDesc scan)
{
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;
	BatchRingItemPos *scanPos = &scan->batchringbuf.scanPos;
	BatchRingItemPos *markPos = &batchringbuf->markPos;
	IndexScanBatch markBatch = batchringbuf->markBatch;
	IndexScanBatch scanBatch = index_scan_batch(scan, scanPos->batch);

	/*
	 * Restoring a mark always required stopping prefetching/that we stop
	 * using scan's read stream.  This is similar to the handling table AMs
	 * implement to deal with a tuple-level change in the scan's direction.
	 */
	if (scan->xs_heapfetch->rs)
	{
		read_stream_end(scan->xs_heapfetch->rs);
		scan->xs_heapfetch->rs = NULL;
	}
	batchringbuf->prefetchPos.valid = false;
	batchringbuf->paused = false;

	if (scanBatch == markBatch)
	{
		/* markBatch is already scanBatch; needn't change batchringbuf */
		Assert(scanPos->batch == markPos->batch);

		scanPos->item = markPos->item;
		return;
	}

	/*
	 * markBatch is behind scanBatch, and so must not be loaded anymore.  We
	 * have to deal with restoring the mark the hard way: by invalidating all
	 * other loaded batches.  This is similar to the case where the scan
	 * direction changes and the scan actually crosses batch/index page
	 * boundaries (see tableam_util_batch_dirchange).
	 *
	 * Call amposreset to let index AM know to invalidate any private state
	 * that independently tracks the scan's progress.
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
	index_scan_batch_append(scan, markBatch);
	Assert(index_scan_batch(scan, batchringbuf->scanPos.batch) == markBatch);

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
 * Handle cross-batch change in scan direction
 *
 * Called by table AM when its scan changes direction in a way that
 * necessitates backing the scan up to an index page originally associated
 * with a now-freed batch.
 *
 * When we return, batchringbuf will only contain one batch (the current
 * headBatch/scanBatch).  Caller can then safely pass this batch to amgetbatch
 * to determine which batch comes next in the new scan direction.  From that
 * point on batchringbuf will look as if our new scan direction had been used
 * from the start.  This approach isn't particularly efficient, but it works
 * well enough for what ought to be a relatively rare occurrence.
 *
 * Caller must have invalidated the scan's read stream before calling here.
 * That needs to happen as soon as the scan requests a tuple in whatever scan
 * direction is opposite-to-current.  We only deal with the case where the
 * scan backs up by enough items to cross a batch boundary (when the scan
 * resumes scanning in its original direction/ends before crossing a boundary,
 * there isn't any need to call here).
 */
void
tableam_util_batch_dirchange(IndexScanDesc scan)
{
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;
	IndexScanBatch head;

	/*
	 * Release batches starting from the current "tail" batch, working
	 * backwards until the current head batch (which must also be the current
	 * scanBatch) is the only batch hasn't been freed
	 */
	while (index_scan_batch_count(scan) > 1)
	{
		IndexScanBatch tail = index_scan_batch(scan,
											   batchringbuf->nextBatch - 1);

		Assert(!batchringbuf->markBatch);
		tableam_util_free_batch(scan, tail);
		batchringbuf->nextBatch--;
	}

	/* scanBatch is now the only batch still loaded */
	Assert(batchringbuf->headBatch == batchringbuf->scanPos.batch);

	/*
	 * Deal with index AM state that independently tracks the progress of the
	 * scan.  Do this by flipping the batch-level scan direction, and then
	 * calling the index AM's amposreset.
	 */
	head = index_scan_batch(scan, batchringbuf->headBatch);
	head->dir = -head->dir;
	if (scan->indexRelation->rd_indam->amposreset)
		scan->indexRelation->rd_indam->amposreset(scan, head);
}

/*
 * Record that scanPos item is dead
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
	IndexScanBatch scanBatch = index_scan_batch(scan, scanPos->batch);

	if (scanBatch->killedItems == NULL)
		scanBatch->killedItems = palloc_array(int, scan->maxitemsbatch);
	if (scanBatch->numKilled < scan->maxitemsbatch)
		scanBatch->killedItems[scanBatch->numKilled++] = scanPos->item;
}

/*
 * Release resources associated with a batch
 *
 * Called by table AM's ordered index scan implementation when it is finished
 * with a batch and wishes to release its resources.
 *
 * We release the batch's buffer pin if table AM hasn't released it already.
 * For plain index scans with an MVCC snapshot, the table AM caller releases
 * the pin immediately, so we never release the pin here.  Index-only scans
 * must delay dropping the pin until visibility is resolved for all items in
 * the batch, so we may need to release the pin here.  For non-MVCC snapshot
 * scans, the pin is always held until this function releases it.
 *
 * We also call the index AM's amfreebatch callback to release AM-specific
 * resources, and to set LP_DEAD bits on the batch's index page (in index AMs
 * that implement that optimization).  Every amfreebatch routine must recycle
 * the underlying batch memory by passing it to indexam_util_batch_release.
 *
 * Note: Calling here when 'batch' is also batchringbuf.markBatch is a no-op.
 * Callers that don't want this should set batchringbuf.markBatch to NULL
 * before calling us.  Note that markBatch has to be explicitly freed.
 */
void
tableam_util_free_batch(IndexScanDesc scan, IndexScanBatch batch)
{
	Assert(BufferIsValid(batch->buf) || scan->MVCCScan);

	/* don't free caller's batch if it is scan's current markBatch */
	if (batch == scan->batchringbuf.markBatch)
		return;

	if (BufferIsValid(batch->buf))
	{
		/* table AM didn't unpin page earlier -- do it now */
		Assert(!scan->MVCCScan || scan->xs_want_itup);

		ReleaseBuffer(batch->buf);
		batch->buf = InvalidBuffer;
	}
#ifdef USE_ASSERT_CHECKING
	else if (scan->xs_want_itup)
	{
		/*
		 * Index-only scan that dropped this batch's pin eagerly.
		 *
		 * Table AM must have checked visibility for every item in the batch.
		 */
		for (int i = batch->firstItem; i <= batch->lastItem; i++)
			Assert(batch->items[i].checkedVisible);
	}
#endif

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
 * Unlock batch's shared buffer lock
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
 * Allocate a new batch
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
	batch->knownEndLeft = false;
	batch->knownEndRight = false;
	batch->firstItem = -1;
	batch->lastItem = -1;
	batch->numKilled = 0;

	return batch;
}

/*
 * Release allocated batch
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

		if (batch->knownEndLeft || batch->knownEndRight)
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
 * qsort comparison function for int arrays
 */
static int
batch_compare_int(const void *va, const void *vb)
{
	int			a = *((const int *) va);
	int			b = *((const int *) vb);

	return pg_cmp_s32(a, b);
}
