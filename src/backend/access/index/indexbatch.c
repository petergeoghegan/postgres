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
 *		index_batch_init - Initialize fields needed by batching
 *		index_batch_reset - reset a batch
 *		index_batch_mark_pos - set a mark from current batch position
 *		index_batch_restore_pos - restore mark to current batch position
 *		index_batch_kill_item - record dead index tuple
 *		index_batch_end - end batch
 *
 *		indexam_util_batch_unlock - unlock batch's buffer lock
 *		indexam_util_batch_alloc - allocate another batch
 *		indexam_util_batch_release - release allocated batch
 *
 *-------------------------------------------------------------------------
 */

#include "postgres.h"

#include "access/amapi.h"
#include "access/tableam.h"
#include "common/int.h"
#include "lib/qunique.h"
#include "pgstat.h"
#include "utils/memdebug.h"

static int	batch_compare_int(const void *va, const void *vb);
static void batch_debug_print_batches(const char *label, IndexScanDesc scan);

/*
 * index_batch_init
 *		Initialize various fields and arrays needed by batching.
 *
 * Sets up the batch queue structure and its initial read position.  Also
 * determines whether the scan will eagerly drop index page pins.
 *
 * Only call here when all of the index related fields in 'scan' were already
 * initialized.
 */
void
index_batch_init(IndexScanDesc scan)
{
	/* Both amgetbatch and amfreebatch must be present together */
	Assert(scan->indexRelation->rd_indam->amgetbatch != NULL);
	Assert(scan->indexRelation->rd_indam->amfreebatch != NULL);

	scan->batchqueue = palloc_object(BatchQueue);

	/*
	 * We prefer to eagerly drop leaf page pins before amgetbatch returns.
	 * This avoids making VACUUM wait to acquire a cleanup lock on the page.
	 *
	 * We cannot safely drop leaf page pins during index-only scans due to a
	 * race condition involving VACUUM setting pages all-visible in the VM.
	 * It's also unsafe for plain index scans that use a non-MVCC snapshot.
	 *
	 * When we drop pins eagerly, the mechanism that marks index tuples as
	 * LP_DEAD has to deal with concurrent TID recycling races.  The scheme
	 * used to detect unsafe TID recycling won't work when scanning unlogged
	 * relations (since it involves saving an affected page's LSN).  Opt out
	 * of eager pin dropping during unlogged relation scans for now.
	 */
	scan->dropPin =
		(!scan->xs_want_itup && IsMVCCSnapshot(scan->xs_snapshot) &&
		 RelationNeedsWAL(scan->indexRelation));
	scan->finished = false;
	scan->batchqueue->reset = false;
	scan->batchqueue->prefetchingLockedIn = false;
	scan->batchqueue->disabled = false;
	scan->batchqueue->currentPrefetchBlock = InvalidBlockNumber;
	scan->batchqueue->direction = NoMovementScanDirection;

	/* positions in the queue of batches */
	batch_reset_pos(&scan->batchqueue->readPos);
	batch_reset_pos(&scan->batchqueue->markPos);
	batch_reset_pos(&scan->batchqueue->streamPos);

	scan->batchqueue->markBatch = NULL;
	scan->batchqueue->headBatch = 0;	/* initial head batch */
	scan->batchqueue->nextBatch = 0;	/* initial batch starts empty */
	memset(&scan->batchqueue->cache, 0, sizeof(scan->batchqueue->cache));
}

/* ----------------
 *		index_batch_reset - reset batch queue and read position
 *
 * Resets all loaded batches in the queue, and resets the read position to the
 * initial state (or just initialize queue state).  When 'complete' is true,
 * also frees the scan's marked batch (if any), which is useful when ending an
 * amgetbatch-based index scan.
 * ----------------
 */
void
index_batch_reset(IndexScanDesc scan, bool complete)
{
	BatchQueue *batchqueue = scan->batchqueue;

	/* bail out if batching not enabled */
	if (!batchqueue)
		return;

	batch_assert_batches_valid(scan);
	batch_debug_print_batches("index_batch_reset", scan);
	Assert(scan->xs_heapfetch);
	if (scan->xs_heapfetch->rs)
		read_stream_reset(scan->xs_heapfetch->rs);

	/* reset the positions */
	batch_reset_pos(&batchqueue->readPos);
	batch_reset_pos(&batchqueue->streamPos);

	/*
	 * With "complete" reset, make sure to also free the marked batch, either
	 * by just forgetting it (if it's still in the queue), or by explicitly
	 * freeing it.
	 */
	if (complete && unlikely(batchqueue->markBatch != NULL))
	{
		BatchQueueItemPos *markPos = &batchqueue->markPos;
		BatchIndexScan markBatch = batchqueue->markBatch;

		/* always reset the position, forget the marked batch */
		batchqueue->markBatch = NULL;

		/*
		 * If we've already moved past the marked batch (it's not loaded into
		 * the queue), free it explicitly now.  Otherwise, it'll be freed
		 * along with the other loaded batches.
		 */
		if (!INDEX_SCAN_BATCH_LOADED(scan, markPos->batch))
			batch_free(scan, markBatch);

		batch_reset_pos(&batchqueue->markPos);
	}

	/* now release all other currently loaded batches */
	while (batchqueue->headBatch < batchqueue->nextBatch)
	{
		BatchIndexScan batch = INDEX_SCAN_BATCH(scan, batchqueue->headBatch);

		DEBUG_LOG("freeing batch %d %p", batchqueue->headBatch, batch);

		batch_free(scan, batch);

		/* update the valid range, so that asserts / debugging works */
		batchqueue->headBatch++;
	}

	/* reset relevant batch state fields */
	batchqueue->headBatch = 0;	/* initial batch */
	batchqueue->nextBatch = 0;	/* initial batch is empty */

	scan->finished = false;
	batchqueue->reset = false;
	batchqueue->currentPrefetchBlock = InvalidBlockNumber;

	batch_assert_batches_valid(scan);
}

/* ----------------
 *		index_batch_mark_pos - mark current position in scan for restoration
 *
 * Saves the current read position and associated batch so that the scan can
 * be restored to this point later, via a call to index_batch_restore_pos.
 * The marked batch is retained and not freed until a new mark is set or the
 * scan ends (or until the mark is restored).
 * ----------------
 */
void
index_batch_mark_pos(IndexScanDesc scan)
{
	BatchQueue *batchqueue = scan->batchqueue;
	BatchQueueItemPos *markPos = &batchqueue->markPos;
	BatchIndexScan markBatch = batchqueue->markBatch;

	/*
	 * Free the previous mark batch (if any), but only if the batch is no
	 * longer loaded into the queue
	 */
	if (markBatch && !INDEX_SCAN_BATCH_LOADED(scan, markPos->batch))
	{
		batchqueue->markBatch = NULL;
		batch_free(scan, markBatch);
	}

	/* copy the read position */
	batchqueue->markPos = batchqueue->readPos;
	batchqueue->markBatch = INDEX_SCAN_BATCH(scan, batchqueue->markPos.batch);

	/* readPos/markPos must be valid */
	batch_assert_pos_valid(scan, &batchqueue->markPos);
}

/* ----------------
 *		index_batch_restore_pos - restore scan to a previously marked position
 *
 * Restores the scan to a position previously saved by index_batch_mark_pos.
 * The marked batch is restored as the current batch, allowing the scan to
 * resume from the marked position.  Also notifies the index AM via a call to
 * its amposreset routine, which allows it to invalidate any private state
 * that independently tracks scan progress (such as array key state)
 *
 * Function currently just discards most batch queue state.  It might make
 * sense to teach it to hold on to other nearby batches (still-held batches
 * that are likely to be needed once the scan finishes returning matching
 * items from the restored batch) as an optimization.  Such a scheme would
 * have the benefit of avoiding repeat calls to amgetbatch/repeatedly reading
 * the same index pages.
 * ----------------
 */
void
index_batch_restore_pos(IndexScanDesc scan)
{
	BatchQueue *batchqueue = scan->batchqueue;
	BatchQueueItemPos *markPos = &batchqueue->markPos;
	BatchIndexScan markBatch = batchqueue->markBatch;

	/*
	 * XXX Disable this optimization when I/O prefetching is in use, at least
	 * until the possible interactions with streamPos are fully understood.
	 */
#if 0
	if (readPos->batch == markPos->batch &&
		readPos->batch == batchqueue->headBatch)
	{
		/*
		 * We don't have to discard the scan's state after all, since the
		 * current headBatch is also the batch that we're restoring to
		 */
		readPos->item = markPos->item;
		return;
	}
#endif

	/*
	 * Call amposreset to let index AM know to invalidate any private state
	 * that independently tracks the scan's progress
	 */
	scan->indexRelation->rd_indam->amposreset(scan, markBatch);

	/*
	 * Reset the batching state, except for the marked batch, and make it look
	 * like we have a single batch -- the marked one.
	 */
	index_batch_reset(scan, false);

	batchqueue->readPos = *markPos;
	batchqueue->nextBatch = batchqueue->headBatch = markPos->batch;

	INDEX_SCAN_BATCH_APPEND(scan, markBatch);
}

/*
 * batch_free
 *		Release resources associated with a batch returned by the index AM.
 *
 * Called by table AM's ordered index scan implementation when it is finished
 * with a batch and wishes to release its resources.
 *
 * This calls the index AM's amfreebatch callback to release AM-specific
 * resources, and to set LP_DEAD bits on the batch's index page.  It isn't
 * safe for table AMs to fetch table tuples using TIDs saved from a batch that
 * was already freed: 'dropPin' scans need the index AM to retain a pin on the
 * TID's index page, as an interlock against concurrent TID recycling.
 */
void
batch_free(IndexScanDesc scan, BatchIndexScan batch)
{
	batch_assert_batch_valid(scan, batch);

	/* don't free the batch that is marked */
	if (batch == scan->batchqueue->markBatch)
		return;

	/*
	 * killedItems[] is now in whatever order the scan returned items in.
	 * Scrollable cursor scans might have even saved the same item/TID twice.
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

/* ----------------
 *		index_batch_kill_item - record item for deferred LP_DEAD marking
 *
 * Records the item index of the currently-read tuple in readBatch's
 * killedItems array. The items' index tuples will later be marked LP_DEAD
 * when current readBatch is freed by amfreebatch routine (see batch_free).
 * ----------------
 */
void
index_batch_kill_item(IndexScanDesc scan)
{
	BatchQueueItemPos *readPos = &scan->batchqueue->readPos;
	BatchIndexScan readBatch = INDEX_SCAN_BATCH(scan, readPos->batch);

	batch_assert_pos_valid(scan, readPos);

	if (readBatch->killedItems == NULL)
		readBatch->killedItems = palloc_array(int, scan->maxitemsbatch);
	if (readBatch->numKilled < scan->maxitemsbatch)
		readBatch->killedItems[readBatch->numKilled++] = readPos->item;
}

/* ----------------
 *		index_batch_end - end a batch scan and free all resources
 *
 * Called when an index scan is being ended, right before the owning scan
 * descriptor goes away.  Cleans up all batch related resources.
 * ----------------
 */
void
index_batch_end(IndexScanDesc scan)
{
	index_batch_reset(scan, true);

	/* bail out without batching */
	if (!scan->batchqueue)
		return;

	for (int i = 0; i < INDEX_SCAN_CACHE_BATCHES; i++)
	{
		BatchIndexScan cached = scan->batchqueue->cache[i];

		if (cached == NULL)
			continue;

		if (cached->killedItems)
			pfree(cached->killedItems);
		if (cached->currTuples)
			pfree(cached->currTuples);
		pfree(cached);
	}

	pfree(scan->batchqueue);
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
 * indexam_util_batch_unlock - Drop lock and conditionally drop pin on batch page
 *
 * Unlocks caller's batch->buf in preparation for amgetbatch returning items
 * saved in that batch.  Manages the details of dropping the lock and possibly
 * the pin for index AM caller (dropping the pin prevents VACUUM from blocking
 * on acquiring a cleanup lock, but isn't always safe).
 *
 * Only call here when a batch has one or more matching items to return using
 * amgetbatch (or for amgetbitmap to load into its bitmap of matching TIDs).
 * When an index page has no matches, it's always safe for index AMs to drop
 * both the lock and the pin for themselves.
 *
 * Note: It is convenient for index AMs that implement both amgetbitmap and
 * amgetbitmap to consistently use the same batch management approach, since
 * that avoids introducing special cases to lower-level code.  We always drop
 * both the lock and the pin on batch's page on behalf of amgetbitmap callers.
 * Such amgetbitmap callers must be careful to free all batches with matching
 * items once they're done saving the matching TIDs (there will never be any
 * calls to amfreebatch, so amgetbitmap must call indexam_util_batch_release
 * directly, in lieu of a deferred call to amfreebatch from core code).
 */
void
indexam_util_batch_unlock(IndexScanDesc scan, BatchIndexScan batch)
{
	Relation	rel = scan->indexRelation;
	bool		dropPin = scan->dropPin;

	/* batch must have one or more matching items returned by index AM */
	Assert(batch->firstItem >= 0 && batch->firstItem <= batch->lastItem);

	if (!dropPin)
	{
		if (!RelationUsesLocalBuffers(rel))
			VALGRIND_MAKE_MEM_NOACCESS(BufferGetPage(batch->buf), BLCKSZ);

		/* Just drop the lock (not the pin) */
		LockBuffer(batch->buf, BUFFER_LOCK_UNLOCK);
		return;
	}

	if (scan->batchqueue)
	{
		/* amgetbatch (not amgetbitmap) caller */
		Assert(scan->heapRelation != NULL);

		/*
		 * Have to set batch->lsn so that amfreebatch has a way to detect when
		 * concurrent heap TID recycling by VACUUM might have taken place.
		 * It'll only be safe to set any index tuple LP_DEAD bits when the
		 * page LSN hasn't advanced.
		 */
		Assert(RelationNeedsWAL(rel));
		Assert(!scan->xs_want_itup);
		batch->lsn = BufferGetLSNAtomic(batch->buf);
	}

	/* Drop both the lock and the pin */
	LockBuffer(batch->buf, BUFFER_LOCK_UNLOCK);
	if (!RelationUsesLocalBuffers(rel))
		VALGRIND_MAKE_MEM_NOACCESS(BufferGetPage(batch->buf), BLCKSZ);
	ReleaseBuffer(batch->buf);
	batch->buf = InvalidBuffer;
}

/*
 * indexam_util_batch_alloc
 *		Allocate a batch during a amgetbatch (or amgetbitmap) index scan.
 *
 * Returns BatchIndexScan with space to fit scan->maxitemsbatch-many
 * BatchMatchingItem entries.  This will either be a newly allocated batch, or
 * a batch recycled from the cache managed by indexam_util_batch_release.  See
 * comments above indexam_util_batch_release.
 *
 * Index AMs that use batches should call this from either their amgetbatch or
 * amgetbitmap routines only.  Note in particular that it cannot safely be
 * called from a amfreebatch routine.
 */
BatchIndexScan
indexam_util_batch_alloc(IndexScanDesc scan)
{
	BatchIndexScan batch = NULL;

	/* First look for an existing batch from queue's cache of batches */
	if (scan->batchqueue != NULL)
	{
		for (int i = 0; i < INDEX_SCAN_CACHE_BATCHES; i++)
		{
			if (scan->batchqueue->cache[i] != NULL)
			{
				/* Return cached unreferenced batch */
				batch = scan->batchqueue->cache[i];
				scan->batchqueue->cache[i] = NULL;
				break;
			}
		}
	}

	if (!batch)
	{
		batch = palloc(offsetof(BatchIndexScanData, items) +
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
 * indexam_util_batch_release
 *		Either stash the batch in a small cache for reuse, or free it.
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
indexam_util_batch_release(IndexScanDesc scan, BatchIndexScan batch)
{
	Assert(batch->buf == InvalidBuffer);

	if (scan->batchqueue)
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
				if (scan->batchqueue->cache[i] == NULL)
				{
					/* found empty slot, we're done */
					scan->batchqueue->cache[i] = batch;
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

static void
batch_debug_print_batches(const char *label, IndexScanDesc scan)
{
#ifdef INDEXAM_DEBUG
	BatchQueue *batchqueue = scan->batchqueue;

	if (!scan->batchqueue)
		return;

	if (!AmRegularBackendProcess())
		return;
	if (IsCatalogRelation(scan->indexRelation))
		return;

	DEBUG_LOG("%s: batches headBatch %d nextBatch %d",
			  label,
			  batchqueue->headBatch, batchqueue->nextBatch);

	for (int i = batchqueue->headBatch; i < batchqueue->nextBatch; i++)
	{
		BatchIndexScan batch = INDEX_SCAN_BATCH(scan, i);

		DEBUG_LOG("    batch %d currPage %u %p firstItem %d lastItem %d killed %d",
				  i, batch->currPage, batch, batch->firstItem,
				  batch->lastItem, batch->numKilled);
	}
#endif
}
