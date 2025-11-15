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
 *		index_batch_getnext_tid - amgetbatch index_getnext_tid implementation
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
#include "access/nbtree.h"		/* XXX for MaxTIDsPerBTreePage (should remove) */
#include "access/tableam.h"
#include "optimizer/cost.h"
#include "pgstat.h"
#include "utils/memdebug.h"

/* private batching utility functions */
static bool batch_getnext(IndexScanDesc scan, ScanDirection direction);
static BlockNumber batch_getnext_stream(ReadStream *stream,
										void *callback_private_data,
										void *per_buffer_data);
static pg_attribute_always_inline bool batch_advance_pos(IndexScanDesc scan,
														 IndexScanBatchPos *pos,
														 ScanDirection direction);
static void batch_reset_pos(IndexScanDesc scan, IndexScanBatchPos *pos);
static void batch_free(IndexScanDesc scan, IndexScanBatch batch);

/* batch debug functions */
static void batch_assert_pos_valid(IndexScanDesc scan, IndexScanBatchPos *pos);
static void batch_assert_batch_valid(IndexScanDesc scan, IndexScanBatch batch);
static void batch_assert_batches_valid(IndexScanDesc scan);
static void batch_debug_print_batches(const char *label, IndexScanDesc scan);

/*
 * Maximum number of batches (leaf pages) we can keep in memory.
 *
 * The value 64 value is arbitrary, it's about 1MB of data with 8KB pages
 * (512kB for pages, and then a bit of overhead). We should not really need
 * this many batches in most cases, though. The read stream looks ahead just
 * enough to queue enough IOs, adjusting the distance (TIDs, but ultimately
 * the number of future batches) to meet that.
 */
#define INDEX_SCAN_MAX_BATCHES	64

#define INDEX_SCAN_BATCH_COUNT(scan) \
	((scan)->batchState->nextBatch - (scan)->batchState->headBatch)

/* Did we already load batch with the requested index? */
#define INDEX_SCAN_BATCH_LOADED(scan, idx) \
	((idx) < (scan)->batchState->nextBatch)

/* Have we loaded the maximum number of batches? */
#define INDEX_SCAN_BATCH_FULL(scan) \
	(INDEX_SCAN_BATCH_COUNT(scan) == scan->batchState->maxBatches)

/* Return batch for the provided index. */
#define INDEX_SCAN_BATCH(scan, idx)	\
		((scan)->batchState->batches[(idx) % INDEX_SCAN_MAX_BATCHES])

/* Is the position invalid/undefined? */
#define INDEX_SCAN_POS_INVALID(pos) \
		(((pos)->batch == -1) && ((pos)->index == -1))

/*
 * Controls when we cancel use of a read stream to do prefetching
 */
#define INDEX_SCAN_MIN_DISTANCE_NBATCHES	20
#define INDEX_SCAN_MIN_TUPLE_DISTANCE		7

#ifdef INDEXAM_DEBUG
#define DEBUG_LOG(...) elog(AmRegularBackendProcess() ? NOTICE : DEBUG2, __VA_ARGS__)
#else
#define DEBUG_LOG(...)
#endif

/*
 * index_batch_init
 *		Initialize various fields / arrays needed by batching.
 *
 * FIXME This is a bit ad-hoc hodge podge, due to how I was adding more and
 * more pieces. Some of the fields may be not quite necessary, needs cleanup.
 */
void
index_batch_init(IndexScanDesc scan)
{
	/* init batching info */
	Assert(scan->indexRelation->rd_indam->amgetbatch != NULL);
	Assert(scan->indexRelation->rd_indam->amfreebatch != NULL);

	scan->batchState = palloc(sizeof(IndexScanBatchState));

	/*
	 * Initialize the batch.
	 *
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
	scan->batchState->dropPin =
		(!scan->xs_want_itup && IsMVCCSnapshot(scan->xs_snapshot) &&
		 RelationNeedsWAL(scan->indexRelation));
	scan->batchState->finished = false;
	scan->batchState->reset = false;
	scan->batchState->prefetchingLockedIn = false;
	scan->batchState->disabled = false;
	scan->batchState->currentPrefetchBlock = InvalidBlockNumber;
	scan->batchState->direction = NoMovementScanDirection;
	/* positions in the queue of batches */
	batch_reset_pos(scan, &scan->batchState->readPos);
	batch_reset_pos(scan, &scan->batchState->streamPos);
	batch_reset_pos(scan, &scan->batchState->markPos);

	scan->batchState->markBatch = NULL;
	scan->batchState->maxBatches = INDEX_SCAN_MAX_BATCHES;
	scan->batchState->headBatch = 0;	/* initial head batch */
	scan->batchState->nextBatch = 0;	/* initial batch starts empty */

	scan->batchState->batches =
		palloc(sizeof(IndexScanBatchData *) * scan->batchState->maxBatches);

	scan->batchState->prefetch = NULL;
	scan->batchState->prefetchArg = NULL;
}

/* ----------------
 *		index_batch_getnext_tid - amgetbatch index_getnext_tid implementation
 *
 * If we advance to the next batch, we release the previous one (unless it's
 * tracked for mark/restore).
 *
 * If the scan direction changes, we release all batches except the current
 * one (per readPos), to make it look it's the only batch we loaded.
 *
 * Returns the first/next TID, or NULL if no more items.
 *
 * XXX Maybe if we advance the position to the next batch, we could keep the
 * batch for a bit more, in case the scan direction changes (as long as it
 * fits into maxBatches)? But maybe that's unnecessary complexity for too
 * little gain, we'd need to be careful about releasing the batches lazily.
 * ----------------
 */
ItemPointer
index_batch_getnext_tid(IndexScanDesc scan, ScanDirection direction)
{
	IndexScanBatchState *batchState = scan->batchState;
	IndexScanBatchPos *readPos;

	/* shouldn't get here without batching */
	batch_assert_batches_valid(scan);

	/* Initialize direction on first call */
	if (batchState->direction == NoMovementScanDirection)
		batchState->direction = direction;

	/*
	 * Handle cancelling the use of the read stream for prefetching
	 */
	else if (unlikely(batchState->disabled && scan->xs_heapfetch->rs))
	{
		batch_reset_pos(scan, &batchState->streamPos);

		read_stream_reset(scan->xs_heapfetch->rs);
		scan->xs_heapfetch->rs = NULL;
	}

	/*
	 * Handle change of scan direction (reset stream, ...).
	 *
	 * Release future batches properly, to make it look like the current batch
	 * is the only one we loaded. Also reset the stream position, as if we are
	 * just starting the scan.
	 */
	else if (unlikely(batchState->direction != direction))
	{
		/* release "future" batches in the wrong direction */
		while (batchState->nextBatch > batchState->headBatch + 1)
		{
			IndexScanBatch fbatch;

			batchState->nextBatch--;
			fbatch = INDEX_SCAN_BATCH(scan, batchState->nextBatch);
			batch_free(scan, fbatch);
		}

		/*
		 * Remember the new direction, and make sure the scan is not marked as
		 * "finished" (we might have already read the last batch, but now we
		 * need to start over). Do this before resetting the stream - it
		 * should not invoke the callback until the first read, but it may
		 * seem a bit confusing otherwise.
		 */
		batchState->direction = direction;
		batchState->finished = false;
		batchState->currentPrefetchBlock = InvalidBlockNumber;

		batch_reset_pos(scan, &batchState->streamPos);
		if (scan->xs_heapfetch->rs)
			read_stream_reset(scan->xs_heapfetch->rs);
	}

	/* shortcut for the read position, for convenience */
	readPos = &batchState->readPos;

	DEBUG_LOG("batch_getnext_tid readPos %d %d direction %d",
			  readPos->batch, readPos->index, direction);

	/*
	 * Try advancing the batch position. If that doesn't succeed, it means we
	 * don't have more items in the current batch, and there's no future batch
	 * loaded. So try loading another batch, and maybe retry.
	 *
	 * FIXME This loop shouldn't happen more than twice. Maybe we should have
	 * some protection against infinite loops? To detect cases when the
	 * advance/getnext functions get to disagree?
	 */
	while (true)
	{
		/*
		 * If we manage to advance to the next items, return it and we're
		 * done. Otherwise try loading another batch.
		 */
		if (batch_advance_pos(scan, readPos, direction))
		{
			IndexScanBatchData *readBatch = INDEX_SCAN_BATCH(scan, readPos->batch);

			/* set the TID / itup for the scan */
			scan->xs_heaptid = readBatch->items[readPos->index].heapTid;

			/*
			 * XXX What about xs_hitup?  Don't we need a way to handle that
			 * for GiST + GP-GiST support?
			 */
			if (scan->xs_want_itup)
				scan->xs_itup =
					(IndexTuple) (readBatch->currTuples +
								  readBatch->items[readPos->index].tupleOffset);

			DEBUG_LOG("readBatch %p firstItem %d lastItem %d readPos %d/%d TID (%u,%u)",
					  readBatch, readBatch->firstItem, readBatch->lastItem,
					  readPos->batch, readPos->index,
					  ItemPointerGetBlockNumber(&scan->xs_heaptid),
					  ItemPointerGetOffsetNumber(&scan->xs_heaptid));

			/*
			 * If we advanced to the next batch, release the batch we no
			 * longer need. The positions is the "read" position, and we can
			 * compare it to headBatch.
			 */
			if (unlikely(readPos->batch != batchState->headBatch))
			{
				IndexScanBatchData *headBatch = INDEX_SCAN_BATCH(scan,
																 batchState->headBatch);

				/*
				 * XXX When advancing readPos, the streamPos may get behind as
				 * we're only advancing it when actually requesting heap
				 * blocks. But we may not do that often enough - e.g. IOS may
				 * not need to access all-visible heap blocks, so the
				 * read_next callback does not get invoked for a long time.
				 * It's possible the stream gets so far behind the position
				 * that is becomes invalid, as we already removed the batch.
				 * But that means we don't need any heap blocks until the
				 * current read position -- if we did, we would not be in this
				 * situation (or it's a sign of a bug, as those two places are
				 * expected to be in sync). So if the streamPos still points
				 * at the batch we're about to free, reset the position --
				 * we'll set it to readPos in the read_next callback later on.
				 *
				 * XXX This can happen after the queue gets full, we "pause"
				 * the stream, and then reset it to continue. But I think that
				 * just increases the probability of hitting the issue, it's
				 * just more chance to to not advance the streamPos, which
				 * depends on when we try to fetch the first heap block after
				 * calling read_stream_reset().
				 *
				 * FIXME Simplify/clarify/shorten this comment. Can it
				 * actually happen, if we never pull from the stream in IOS?
				 * We probably don't look ahead for the first call.
				 */
				if (unlikely(batchState->streamPos.batch == batchState->headBatch))
				{
					DEBUG_LOG("batch_pos_reset called early (streamPos.batch == headBatch)");
					batch_reset_pos(scan, &batchState->streamPos);
				}

				DEBUG_LOG("batch_getnext_tid free headBatch %p headBatch %d nextBatch %d",
						  headBatch, batchState->headBatch, batchState->nextBatch);

				/* Free the head batch (except when it's markBatch) */
				batch_free(scan, headBatch);

				/*
				 * In any case, remove the batch from the regular queue, even
				 * if we kept it for mark/restore.
				 */
				batchState->headBatch++;

				DEBUG_LOG("batch_getnext_tid batch freed headBatch %d nextBatch %d",
						  batchState->headBatch, batchState->nextBatch);

				batch_debug_print_batches("batch_getnext_tid / free old batch", scan);

				/* we can't skip any batches */
				Assert(batchState->headBatch == readPos->batch);
			}

			pgstat_count_index_tuples(scan->indexRelation, 1);
			return &scan->xs_heaptid;
		}

		/*
		 * We failed to advance, i.e. we ran out of currently loaded batches.
		 * So if we filled the queue, this is a good time to reset the stream
		 * (before we try loading the next batch).
		 */
		if (unlikely(batchState->reset))
		{
			DEBUG_LOG("resetting read stream readPos %d,%d",
					  readPos->batch, readPos->index);

			batchState->reset = false;
			batchState->currentPrefetchBlock = InvalidBlockNumber;

			/*
			 * Need to reset the stream position, it might be too far behind.
			 * Ultimately we want to set it to readPos, but we can't do that
			 * yet - readPos still point sat the old batch, so just reset it
			 * and we'll init it to readPos later in the callback.
			 */
			batch_reset_pos(scan, &batchState->streamPos);

			if (scan->xs_heapfetch->rs)
				read_stream_reset(scan->xs_heapfetch->rs);
		}

		/*
		 * Failed to advance the read position, so try reading the next batch.
		 * If this fails, we're done - there's nothing more to load.
		 *
		 * Most of the batches should be loaded from read_stream_next_buffer,
		 * but we need to call index_batch_getnext here too, for two reasons.
		 * First, the read_stream only gets working after we try fetching the
		 * first heap tuple, so we need to load the initial batch (the head).
		 * Second, while most batches will be preloaded by the stream thanks
		 * to prefetching, it's possible to set effective_io_concurrency=0,
		 * and in that case all the batches get loaded from here.
		 */
		if (!batch_getnext(scan, direction))
			break;

		DEBUG_LOG("loaded next batch, retry to advance position");
	}

	/*
	 * If we get here, we failed to advance the position and there are no more
	 * batches, so we're done.
	 */
	DEBUG_LOG("no more batches to process");

	/*
	 * Reset the position - we must not keep the last valid position, in case
	 * we change direction of the scan and start scanning again. If we kept
	 * the position, we'd skip the first item.
	 *
	 * XXX This is a bit strange. Do we really need to reset the position
	 * after returning the last item? I wonder if it means the API is not
	 * quite right.
	 */
	batch_reset_pos(scan, readPos);

	return NULL;
}

/* ----------------
 *		batch_getnext - get the next batch of TIDs from a scan
 *
 * Returns true if we managed to read a batch of TIDs, or false if there are no
 * more TIDs in the scan. The load may also return false if we used the maximum
 * number of batches (INDEX_SCAN_MAX_BATCHES), in which case we'll reset the
 * stream and continue the scan later.
 *
 * Returns true if the batch was loaded successfully, false otherwise.
 *
 * This only loads the TIDs and resets the various batch fields to fresh
 * state. It does not set xs_heaptid/xs_itup/xs_hitup, that's the
 * responsibility of the following batch_getnext_tid() calls.
 * ----------------
 */
static bool
batch_getnext(IndexScanDesc scan, ScanDirection direction)
{
	IndexScanBatchState *batchState = scan->batchState;
	IndexScanBatch priorbatch = NULL,
				batch = NULL;

	/* XXX: we should assert that a snapshot is pushed or registered */
	Assert(TransactionIdIsValid(RecentXmin));

	/* Did we already read the last batch for this scan? */
	if (batchState->finished)
		return false;

	/*
	 * If we already used the maximum number of batch slots available, it's
	 * pointless to try loading another one. This can happen for various
	 * reasons, e.g. for index-only scans on all-visible table, or skipping
	 * duplicate blocks on perfectly correlated indexes, etc.
	 *
	 * We could enlarge the array to allow more batches, but that's futile, we
	 * can always construct a case using more memory. Not only it would risk
	 * OOM, it'd also be inefficient because this happens early in the scan
	 * (so it'd interfere with LIMIT queries).
	 */
	if (INDEX_SCAN_BATCH_FULL(scan))
	{
		DEBUG_LOG("index_batch_getnext: ran out of space for batches");
		scan->batchState->reset = true;
		return false;
	}

	batch_debug_print_batches("index_batch_getnext / start", scan);

	/*
	 * Check if there's an existing batch that amgetbatch has to pick things
	 * up from
	 */
	if (batchState->headBatch < batchState->nextBatch)
		priorbatch = INDEX_SCAN_BATCH(scan, batchState->nextBatch - 1);

	batch = scan->indexRelation->rd_indam->amgetbatch(scan, priorbatch,
													  direction);
	if (batch != NULL)
	{
		/*
		 * We got the batch from the AM, but we need to add it to the queue.
		 * Maybe that should be part of the "batch allocation" that happens in
		 * the AM?
		 */
		int			batchIndex = batchState->nextBatch;

		INDEX_SCAN_BATCH(scan, batchIndex) = batch;

		batchState->nextBatch++;

		DEBUG_LOG("index_batch_getnext headBatch %d nextBatch %d batch %p",
				  batchState->headBatch, batchState->nextBatch, batch);

		/* Delay initializing stream until reading from scan's second batch */
		if (priorbatch && !scan->xs_heapfetch->rs && !batchState->disabled &&
			enable_indexscan_prefetch)
			scan->xs_heapfetch->rs =
				read_stream_begin_relation(READ_STREAM_DEFAULT, NULL,
										   scan->heapRelation, MAIN_FORKNUM,
										   batch_getnext_stream, scan, 0);
	}
	else
		batchState->finished = true;

	batch_assert_batches_valid(scan);

	batch_debug_print_batches("index_batch_getnext / end", scan);

	return (batch != NULL);
}

/*
 * batch_getnext_stream
 *		return the next block to pass to the read stream
 *
 * This assumes the "current" scan direction, requested by the caller.
 *
 * If the direction changes before consuming all blocks, we'll reset the stream
 * and start from scratch. The scan direction change is handled elsewhere. Here
 * we rely on having the correct value in batchState->direction.
 *
 * The position of the read_stream is stored in streamPos, which may be ahead of
 * the current readPos (which is what got consumed by the scan).
 *
 * The streamPos can however also get behind readPos too, when some blocks are
 * skipped and not returned to the read_stream. An example is an index scan on
 * a correlated index, with many duplicate blocks are skipped, or an IOS where
 * all-visible blocks are skipped.
 *
 * The initial batch is always loaded from batch_getnext_tid(). We don't
 * get here until the first read_stream_next_buffer() call, when pulling the
 * first heap tuple from the stream. After that, most batches should be loaded
 * by this callback, driven by the read_stream look-ahead distance. However,
 * with disabled prefetching (that is, with effective_io_concurrency=0), all
 * batches will be loaded in batch_getnext_tid.
 *
 * It's possible we got here only fairly late in the scan, e.g. if many tuples
 * got skipped in the index-only scan, etc. In this case just use the read
 * position as a streamPos starting point.
 *
 * XXX It seems the readPos/streamPos comments should be placed elsewhere. The
 * read_stream callback does not seem like the right place.
 */
static BlockNumber
batch_getnext_stream(ReadStream *stream, void *callback_private_data,
					 void *per_buffer_data)
{
	IndexScanDesc scan = (IndexScanDesc) callback_private_data;
	IndexScanBatchState *batchState = scan->batchState;
	IndexScanBatchPos *streamPos = &batchState->streamPos;
	ScanDirection direction = batchState->direction;

	/* By now we should know the direction of the scan. */
	Assert(direction != NoMovementScanDirection);

	/*
	 * The read position (readPos) has to be valid.
	 *
	 * We initialize/advance it before even attempting to read the heap tuple,
	 * and it gets invalidated when we reach the end of the scan (but then we
	 * don't invoke the callback again).
	 *
	 * XXX This applies to the readPos. We'll use streamPos to determine which
	 * blocks to pass to the stream, and readPos may be used to initialize it.
	 */
	batch_assert_pos_valid(scan, &batchState->readPos);

	/*
	 * Try to advance the streamPos to the next item, and if that doesn't
	 * succeed (if there are no more items in loaded batches), try loading the
	 * next one.
	 *
	 * FIXME Unlike batch_getnext_tid, this can loop more than twice. If many
	 * blocks get skipped due to currentPrefetchBlock or all-visibility (per
	 * the "prefetch" callback), we get to load additional batches. In the
	 * worst case we hit the INDEX_SCAN_MAX_BATCHES limit and have to "pause"
	 * the stream.
	 */
	while (true)
	{
		bool		advanced = false;

		/*
		 * If the stream position has not been initialized yet, set it to the
		 * current read position. This is the item the caller is trying to
		 * read, so it's what we should return to the stream.
		 */
		if (INDEX_SCAN_POS_INVALID(streamPos))
		{
			*streamPos = batchState->readPos;
			advanced = true;
		}
		else if (batch_advance_pos(scan, streamPos, direction))
		{
			advanced = true;
		}

		/*
		 * FIXME Maybe check the streamPos is not behind readPos?
		 *
		 * FIXME Actually, could streamPos get stale/lagging behind readPos,
		 * and if yes how much. Could it get so far behind to not be valid,
		 * pointing at a freed batch? In that case we can't even advance it,
		 * and we should just initialize it to readPos. We might do that
		 * anyway, I guess, just to save on "pointless" advances (it must
		 * agree with readPos, we can't allow "retroactively" changing the
		 * block sequence).
		 */

		/*
		 * If we advanced the position, either return the block for the TID,
		 * or skip it (and then try advancing again).
		 *
		 * The block may be "skipped" for two reasons. First, the caller may
		 * define a "prefetch" callback that tells us to skip items (IOS does
		 * this to skip all-visible pages). Second, currentPrefetchBlock is
		 * used to skip duplicate block numbers (a sequence of TIDS for the
		 * same block).
		 */
		if (advanced)
		{
			IndexScanBatch streamBatch = INDEX_SCAN_BATCH(scan, streamPos->batch);
			ItemPointer tid = &streamBatch->items[streamPos->index].heapTid;

			DEBUG_LOG("batch_getnext_stream: index %d TID (%u,%u)",
					  streamPos->index,
					  ItemPointerGetBlockNumber(tid),
					  ItemPointerGetOffsetNumber(tid));

			/*
			 * If there's a prefetch callback, use it to decide if we need to
			 * read the next block.
			 *
			 * We need to do this before checking currentPrefetchBlock; it's
			 * essential that the VM cache used by index-only scans is
			 * initialized here.
			 */
			if (batchState->prefetch &&
				!batchState->prefetch(scan, batchState->prefetchArg, streamPos))
			{
				DEBUG_LOG("batch_getnext_stream: skip block (callback)");
				continue;
			}

			/* same block as before, don't need to read it */
			if (batchState->currentPrefetchBlock == ItemPointerGetBlockNumber(tid))
			{
				DEBUG_LOG("batch_getnext_stream: skip block (currentPrefetchBlock)");
				continue;
			}

			batchState->currentPrefetchBlock = ItemPointerGetBlockNumber(tid);

			return batchState->currentPrefetchBlock;
		}

		/*
		 * Couldn't advance the position, no more items in the loaded batches.
		 * Try loading the next batch - if that succeeds, try advancing again
		 * (this time the advance should work, but we may skip all the items).
		 *
		 * If we fail to load the next batch, we're done.
		 */
		if (!batch_getnext(scan, direction))
			break;

		/*
		 * Consider disabling prefetching when we can't keep a sufficiently
		 * large "index tuple distance" between readPos and streamPos.
		 *
		 * Only consider doing this when we're not on the scan's initial
		 * batch, when readPos and streamPos share the same batch.
		 */
		if (!batchState->finished && !batchState->prefetchingLockedIn)
		{
			int			indexdiff;

			if (streamPos->batch <= INDEX_SCAN_MIN_DISTANCE_NBATCHES)
			{
				/* Too early to check if prefetching should be disabled */
			}
			else if (batchState->readPos.batch == streamPos->batch)
			{
				IndexScanBatchPos *readPos = &batchState->readPos;

				if (ScanDirectionIsForward(direction))
					indexdiff = streamPos->index - readPos->index;
				else
				{
					IndexScanBatch readBatch =
						INDEX_SCAN_BATCH(scan, readPos->batch);

					indexdiff = (readPos->index - readBatch->firstItem) -
						(streamPos->index - readBatch->firstItem);
				}

				if (indexdiff < INDEX_SCAN_MIN_TUPLE_DISTANCE)
				{
					batchState->disabled = true;
					return InvalidBlockNumber;
				}
				else
				{
					batchState->prefetchingLockedIn = true;
				}
			}
			else
				batchState->prefetchingLockedIn = true;
		}
	}

	/* no more items in this scan */
	return InvalidBlockNumber;
}

/*
 * index_batch_reset
 *		Reset the batch before reading the next chunk of data.
 *
 * complete - true means we reset even marked batch
 *
 * XXX Should this reset the batch memory context, xs_itup, xs_hitup, etc?
 */
void
index_batch_reset(IndexScanDesc scan, bool complete)
{
	IndexScanBatchState *batchState = scan->batchState;

	/* bail out if batching not enabled */
	if (!batchState)
		return;

	batch_assert_batches_valid(scan);

	batch_debug_print_batches("index_batch_reset", scan);

	/* With batching enabled, we should have a read stream. Reset it. */
	Assert(scan->xs_heapfetch);
	if (scan->xs_heapfetch->rs)
		read_stream_reset(scan->xs_heapfetch->rs);

	/* reset the positions */
	batch_reset_pos(scan, &batchState->readPos);
	batch_reset_pos(scan, &batchState->streamPos);

	/*
	 * With "complete" reset, make sure to also free the marked batch, either
	 * by just forgetting it (if it's still in the queue), or by explicitly
	 * freeing it.
	 *
	 * XXX Do this before the loop, so that it calls the amfreebatch().
	 */
	if (complete && unlikely(batchState->markBatch != NULL))
	{
		IndexScanBatchPos *markPos = &batchState->markPos;
		IndexScanBatch markBatch = batchState->markBatch;

		/* always reset the position, forget the marked batch */
		batchState->markBatch = NULL;

		/*
		 * If we've already moved past the marked batch (it's not in the
		 * current queue), free it explicitly. Otherwise it'll be in the freed
		 * later.
		 */
		if (markPos->batch < batchState->headBatch ||
			markPos->batch >= batchState->nextBatch)
			batch_free(scan, markBatch);

		/* reset position only after the queue range check */
		batch_reset_pos(scan, &batchState->markPos);
	}

	/* release all currently loaded batches */
	while (batchState->headBatch < batchState->nextBatch)
	{
		IndexScanBatch batch = INDEX_SCAN_BATCH(scan, batchState->headBatch);

		DEBUG_LOG("freeing batch %d %p", batchState->headBatch, batch);

		batch_free(scan, batch);

		/* update the valid range, so that asserts / debugging works */
		batchState->headBatch++;
	}

	/* reset relevant batch state fields */
	Assert(batchState->maxBatches == INDEX_SCAN_MAX_BATCHES);
	batchState->headBatch = 0;	/* initial batch */
	batchState->nextBatch = 0;	/* initial batch is empty */

	batchState->finished = false;
	batchState->reset = false;
	batchState->currentPrefetchBlock = InvalidBlockNumber;

	batch_assert_batches_valid(scan);
}

/*
 * batch_advance_pos
 *		Advance the position to the next item, depending on scan direction.
 *
 * Advance the position to the next item, either in the same batch or the
 * following one (if already available).
 *
 * We can advance only if we already have some batches loaded, and there's
 * either enough items in the current batch, or some more items in the
 * subsequent batches.
 *
 * If this is the first advance (right after loading the initial/head batch),
 * position is still undefined. Otherwise we expect the position to be valid.
 *
 * Returns true if the position was advanced, false otherwise.
 *
 * The position is guaranteed to be valid only after a successful advance.
 * If an advance fails (false returned), the position can be invalid.
 *
 * XXX This seems like a good place to enforce some "invariants", e.g.
 * that the positions are always valid. We should never get here with
 * invalid position (so probably should be initialized as part of loading the
 * initial/head batch), and then invalidated if advance fails. Could be tricky
 * for the stream position, though, because it can get "lag" for IOS etc.
 */
static pg_attribute_always_inline bool
batch_advance_pos(IndexScanDesc scan, IndexScanBatchPos *pos,
				  ScanDirection direction)
{
	IndexScanBatchData *batch;

	/* make sure we have batching initialized and consistent */
	batch_assert_batches_valid(scan);

	/* should know direction by now */
	Assert(direction == scan->batchState->direction);
	Assert(direction != NoMovementScanDirection);

	/* We can't advance if there are no batches available. */
	if (INDEX_SCAN_BATCH_COUNT(scan) == 0)
		return false;

	/*
	 * If the position has not been advanced yet, it has to be right after we
	 * loaded the initial batch (must be the head batch). In that case just
	 * initialize it to the batch's first item (or its last item, when
	 * scanning backwards).
	 *
	 * XXX Maybe we should just explicitly initialize the position after
	 * loading the initial batch, without having to go through the advance.
	 */
	if (INDEX_SCAN_POS_INVALID(pos))
	{
		/*
		 * We should have loaded the scan's initial batch, or maybe we have
		 * changed the direction of the scan after scanning all the way to the
		 * end (in which case the position is invalid, and we make it look
		 * like there is just one batch). We should have just one batch,
		 * though.
		 *
		 * XXX Actually, could there be more batches? Maybe we prefetched more
		 * batches right away? It doesn't seem to be a substantial invariant.
		 */
		Assert(INDEX_SCAN_BATCH_COUNT(scan) == 1);

		/*
		 * Get the initial batch (which must be the head), and initialize the
		 * position to the appropriate item for the current scan direction
		 */
		batch = INDEX_SCAN_BATCH(scan, scan->batchState->headBatch);

		pos->batch = scan->batchState->headBatch;

		if (ScanDirectionIsForward(direction))
			pos->index = batch->firstItem;
		else
			pos->index = batch->lastItem;

		batch_assert_pos_valid(scan, pos);

		return true;
	}

	/*
	 * The position is already defined, so we should have some batches loaded
	 * and the position has to be valid with respect to those.
	 */
	batch_assert_pos_valid(scan, pos);

	/*
	 * Advance to the next item in the same batch, if there are more items. If
	 * we're at the last item, we'll try advancing to the next batch later.
	 */
	batch = INDEX_SCAN_BATCH(scan, pos->batch);

	if (ScanDirectionIsForward(direction))
	{
		if (++pos->index <= batch->lastItem)
		{
			batch_assert_pos_valid(scan, pos);

			return true;
		}
	}
	else						/* ScanDirectionIsBackward */
	{
		if (--pos->index >= batch->firstItem)
		{
			batch_assert_pos_valid(scan, pos);

			return true;
		}
	}

	/*
	 * We couldn't advance within the same batch, try advancing to the next
	 * batch, if it's already loaded.
	 */
	if (INDEX_SCAN_BATCH_LOADED(scan, pos->batch + 1))
	{
		/* advance to the next batch */
		pos->batch++;

		batch = INDEX_SCAN_BATCH(scan, pos->batch);
		Assert(batch != NULL);

		if (ScanDirectionIsForward(direction))
			pos->index = batch->firstItem;
		else
			pos->index = batch->lastItem;

		batch_assert_pos_valid(scan, pos);

		return true;
	}

	/* can't advance */
	return false;
}

/*
 * batch_pos_reset
 *		Reset the position, so that it looks as if never advanced.
 */
static void
batch_reset_pos(IndexScanDesc scan, IndexScanBatchPos *pos)
{
	pos->batch = -1;
	pos->index = -1;
}

void
index_batch_mark_pos(IndexScanDesc scan)
{
	IndexScanBatchState *batchState = scan->batchState;
	IndexScanBatchPos *markPos = &batchState->markPos;
	IndexScanBatchData *markBatch = batchState->markBatch;

	/*
	 * Free the previous mark batch (if any), but only if the batch is no
	 * longer valid (in the current head/next range). This means that if we're
	 * marking the same batch (different item), we don't really do anything.
	 *
	 * XXX Should have some macro for this check, I guess.
	 */
	if (markBatch != NULL && (markPos->batch < batchState->headBatch ||
							  markPos->batch >= batchState->nextBatch))
	{
		batchState->markBatch = NULL;
		batch_free(scan, markBatch);
	}

	/* just copy the read position */
	batchState->markPos = batchState->readPos;
	batchState->markBatch = INDEX_SCAN_BATCH(scan, batchState->markPos.batch);

	/* readPos/markPos must be valid */
	batch_assert_pos_valid(scan, &batchState->markPos);
}

void
index_batch_restore_pos(IndexScanDesc scan)
{
	IndexScanBatchState *batchState = scan->batchState;
	IndexScanBatchPos *markPos = &batchState->markPos;
	IndexScanBatchData *markBatch = batchState->markBatch;

	batchState = scan->batchState;
	markPos = &batchState->markPos;
	markBatch = scan->batchState->markBatch;

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

	batchState->markPos = *markPos;
	batchState->readPos = *markPos;
	batchState->headBatch = markPos->batch;
	batchState->nextBatch = (batchState->headBatch + 1);

	INDEX_SCAN_BATCH(scan, batchState->markPos.batch) = markBatch;
	batchState->markBatch = markBatch;	/* also remember this */
}

static void
batch_free(IndexScanDesc scan, IndexScanBatch batch)
{
	batch_assert_batch_valid(scan, batch);

	/* don't free the batch that is marked */
	if (batch == scan->batchState->markBatch)
		return;

	scan->indexRelation->rd_indam->amfreebatch(scan, batch);
}

void
index_batch_kill_item(IndexScanDesc scan)
{
	IndexScanBatchPos *readPos = &scan->batchState->readPos;
	IndexScanBatchData *readBatch = INDEX_SCAN_BATCH(scan, readPos->batch);

	batch_assert_pos_valid(scan, readPos);

	/*
	 * XXX Maybe we can move the state that indicates if an item has been
	 * killed into IndexScanBatchData.items[] array.
	 *
	 * See:
	 * https://postgr.es/m/CAH2-WznLN7P0i2-YEnv3QGmeA5AMjdcjkraO_nz3H2Va1V1WOA@mail.gmail.com
	 */
	if (readBatch->killedItems == NULL)
		readBatch->killedItems = (int *)
			palloc(MaxTIDsPerBTreePage * sizeof(int));
	if (readBatch->numKilled < MaxTIDsPerBTreePage)
		readBatch->killedItems[readBatch->numKilled++] = readPos->index;
}

void
index_batch_end(IndexScanDesc scan)
{
	index_batch_reset(scan, true);
}

/* ----------------------------------------------------------------
 *			utility functions called by amgetbatch index AMs
 * ----------------------------------------------------------------
 */

/*
 * Unlock batch->buf.  If batch scan is dropPin, drop the pin, too.  Dropping
 * the pin prevents VACUUM from blocking on acquiring a cleanup lock.
 */
void
indexam_util_batch_unlock(Relation rel, bool dropPin, IndexScanBatch batch)
{
	if (!dropPin)
	{
		if (!RelationUsesLocalBuffers(rel))
			VALGRIND_MAKE_MEM_NOACCESS(BufferGetPage(batch->buf), BLCKSZ);

		/* Just drop the lock (not the pin) */
		LockBuffer(batch->buf, BUFFER_LOCK_UNLOCK);
		return;
	}

	/*
	 * Drop both the lock and the pin.
	 *
	 * Have to set batch->lsn so that amfreebatch has a way to detect when
	 * concurrent heap TID recycling by VACUUM might have taken place.  It'll
	 * only be safe to set any index tuple LP_DEAD bits when the page LSN
	 * hasn't advanced.
	 */
	Assert(RelationNeedsWAL(rel));
	batch->lsn = BufferGetLSNAtomic(batch->buf);
	LockBuffer(batch->buf, BUFFER_LOCK_UNLOCK);
	if (!RelationUsesLocalBuffers(rel))
		VALGRIND_MAKE_MEM_NOACCESS(BufferGetPage(batch->buf), BLCKSZ);
	ReleaseBuffer(batch->buf);
	batch->buf = InvalidBuffer; /* defensive */
}

/*
 * indexam_util_batch_alloc
 *		Allocate a batch that can fit maxitems index tuples.
 *
 * Returns a IndexScanBatch struct with capacity sufficient for maxitems index
 * tuples.
 *
 * maxitems determines the minimum size of the batch (it may be larger)
 * want_itup determines whether the bach allocates space for currTuples
 *
 * XXX Both index_batch_alloc() calls in btree use MaxTIDsPerBTreePage,
 * which seems unfortunate - it increases the allocation sizes, even if
 * the index would be fine with smaller arrays. This means all batches
 * exceed ALLOC_CHUNK_LIMIT, forcing a separate malloc (expensive).
 */
IndexScanBatch
indexam_util_batch_alloc(int maxitems, bool want_itup)
{
	IndexScanBatch batch = palloc(offsetof(IndexScanBatchData, items) +
								  sizeof(IndexScanBatchPosItem) * maxitems);

	batch->firstItem = -1;
	batch->lastItem = -1;
	batch->killedItems = NULL;
	batch->numKilled = 0;

	/*
	 * If we are doing an index-only scan, we need a tuple storage workspace.
	 * We allocate BLCKSZ for this, which should always give the index AM
	 * enough space to fit a full page's worth of tuples.
	 */
	batch->currTuples = NULL;
	if (want_itup)
		batch->currTuples = palloc(BLCKSZ);

	batch->buf = InvalidBuffer;
	batch->pos = NULL;
	batch->itemsvisibility = NULL;	/* per-batch IOS visibility */

	return batch;
}

/*
 * Check that a position (batch,item) is valid with respect to the batches we
 * have currently loaded.
 *
 * XXX The "marked" batch is an exception. The marked batch may get outside
 * the range of current batches, so make sure to never check the position
 * for that.
 */
static void
batch_assert_pos_valid(IndexScanDesc scan, IndexScanBatchPos *pos)
{
#ifdef USE_ASSERT_CHECKING
	IndexScanBatchState *batchState = scan->batchState;

	/* make sure the position is valid for currently loaded batches */
	Assert(pos->batch >= batchState->headBatch);
	Assert(pos->batch < batchState->nextBatch);
#endif
}

/*
 * Check a single batch is valid.
 */
static void
batch_assert_batch_valid(IndexScanDesc scan, IndexScanBatch batch)
{
#ifdef USE_ASSERT_CHECKING
	/* there must be valid range of items */
	Assert(batch->firstItem <= batch->lastItem);
	Assert(batch->firstItem >= 0);

	/* we should have items (buffer and pointers) */
	Assert(batch->items != NULL);

	/*
	 * The number of killed items must be valid, and there must be an array of
	 * indexes if there are items.
	 */
	Assert(batch->numKilled >= 0);
	Assert(!(batch->numKilled > 0 && batch->killedItems == NULL));
#endif
}

/*
 * Check invariants on current batches
 *
 * Makes sure the indexes are set as expected, the buffer size is within
 * limits, and so on.
 */
static void
batch_assert_batches_valid(IndexScanDesc scan)
{
#ifdef USE_ASSERT_CHECKING
	IndexScanBatchState *batchState = scan->batchState;

	/* we should have batches initialized */
	Assert(batchState != NULL);

	/* We should not have too many batches. */
	Assert(batchState->maxBatches > 0 &&
		   batchState->maxBatches <= INDEX_SCAN_MAX_BATCHES);

	/*
	 * The head/next indexes should define a valid range (in the cyclic
	 * buffer, and should not overflow maxBatches.
	 */
	Assert(batchState->headBatch >= 0 &&
		   batchState->headBatch <= batchState->nextBatch);
	Assert(batchState->nextBatch - batchState->headBatch <=
		   batchState->maxBatches);

	/* Check all current batches */
	for (int i = batchState->headBatch; i < batchState->nextBatch; i++)
	{
		IndexScanBatch batch = INDEX_SCAN_BATCH(scan, i);

		batch_assert_batch_valid(scan, batch);
	}
#endif
}

static void
batch_debug_print_batches(const char *label, IndexScanDesc scan)
{
#ifdef INDEXAM_DEBUG
	IndexScanBatchState *batches = scan->batchState;

	if (!scan->batchState)
		return;

	if (!AmRegularBackendProcess())
		return;
	if (IsCatalogRelation(scan->indexRelation))
		return;

	DEBUG_LOG("%s: batches headBatch %d nextBatch %d maxBatches %d",
			  label,
			  batches->headBatch, batches->nextBatch, batches->maxBatches);

	for (int i = batches->headBatch; i < batches->nextBatch; i++)
	{
		IndexScanBatchData *batch = INDEX_SCAN_BATCH(scan, i);
		BTScanPos	pos = (BTScanPos) batch->pos;

		DEBUG_LOG("    batch %d currPage %u %p firstItem %d lastItem %d killed %d",
				  i, pos->currPage, batch, batch->firstItem, batch->lastItem,
				  batch->numKilled);
	}
#endif
}
