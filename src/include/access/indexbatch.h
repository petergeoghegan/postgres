/*-------------------------------------------------------------------------
 *
 * indexbatch.h
 *	  Batch-based index scan infrastructure for the amgetbatch interface.
 *
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/include/access/indexbatch.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef INDEXBATCH_H
#define INDEXBATCH_H

#include "access/amapi.h"
#include "access/genam.h"
#include "access/relscan.h"
#include "storage/buf.h"
#include "utils/rel.h"

/*
 * amgetbatch utilities called by indexam.c
 */
extern void index_batchscan_init(IndexScanDesc scan);
extern void index_batchscan_reset(IndexScanDesc scan);
extern void index_batchscan_end(IndexScanDesc scan);
extern void index_batchscan_mark_pos(IndexScanDesc scan);
extern void index_batchscan_restore_pos(IndexScanDesc scan);

/*
 * amgetbatch utilities called by table AMs
 */
extern void tableam_util_batch_dirchange(IndexScanDesc scan);
extern void tableam_util_kill_scanpositem(IndexScanDesc scan);
extern void tableam_util_free_batch(IndexScanDesc scan, IndexScanBatch batch);
extern void tableam_util_unguard_batch(IndexScanDesc scan, IndexScanBatch batch);

/*
 * Fetch the next batch of matching items for the scan (or the first).
 *
 * Called when caller's current batch (passed to us as priorBatch) has no more
 * matching items in the given scan direction.  Caller passes a NULL
 * priorBatch on the first call here for the scan.
 *
 * Returns the next batch to be processed by caller in the given scan
 * direction, or NULL when there are no more matches in that direction.
 *
 * This is where batches are appended to the scan's ring buffer.  We don't
 * free any batches here, though; that is left up to the caller.  The caller
 * is also responsible for advancing their position.
 */
static pg_attribute_always_inline IndexScanBatch
tableam_util_fetch_next_batch(IndexScanDesc scan, ScanDirection direction,
							  IndexScanBatch priorBatch, BatchRingItemPos *pos)
{
	IndexScanBatch batch = NULL;
	BatchRingBuffer *batchringbuf PG_USED_FOR_ASSERTS_ONLY = &scan->batchringbuf;

	if (!priorBatch)
	{
		/* First call for the scan */
		Assert(pos == &batchringbuf->scanPos);
	}
	else if (unlikely(priorBatch->dir != direction))
	{
		/*
		 * We detected a change in scan direction across batches.  Prepare
		 * scan's batchringbuf state for us to get the next batch for the
		 * opposite scan direction to the one used when priorBatch was
		 * returned by amgetbatch.
		 */
		tableam_util_batch_dirchange(scan);

		/* priorBatch is now batchringbuf's only batch */
		Assert(pos->batch == batchringbuf->headBatch);
		Assert(index_scan_batch_count(scan) == 1);
	}
	else if (index_scan_batch_loaded(scan, pos->batch + 1))
	{
		/* Next batch already loaded for us */
		batch = index_scan_batch(scan, pos->batch + 1);

		Assert(priorBatch->dir == direction);
		Assert(batch->dir == direction);
		return batch;
	}

	/*
	 * Assert preconditions for calling amgetbatch.
	 *
	 * priorBatch had better be for the last valid batch currently in the ring
	 * buffer (batches must stay in scan order).  If it isn't then we should
	 * have already returned some existing loaded batch earlier.
	 */
	Assert(!index_scan_batch_full(scan));
	Assert(!priorBatch ||
		   (index_scan_batch_count(scan) > 0 && priorBatch->dir == direction &&
			index_scan_batch(scan, batchringbuf->nextBatch - 1) == priorBatch));

	/*
	 * Before we call amgetbatch again, check if priorBatch is already known
	 * to be the last batch with matching items in this scan direction
	 */
	if (priorBatch &&
		(ScanDirectionIsForward(direction) ?
		 priorBatch->knownEndForward :
		 priorBatch->knownEndBackward))
		return NULL;

	batch = scan->indexRelation->rd_indam->amgetbatch(scan, priorBatch,
													  direction);
	if (batch)
	{
		/* We got the batch from the index AM */
		Assert(batch->dir == direction);

		/* Append batch to the end of ring buffer/write it to buffer index */
		index_scan_batch_append(scan, batch);
	}
	else
	{
		/* amgetbatch returned NULL */
		if (priorBatch)
		{
			/*
			 * There are no further matches to be found in the current scan
			 * direction, following priorBatch.  Remember that priorBatch is
			 * the last batch with matching items.
			 */
			if (ScanDirectionIsForward(direction))
				priorBatch->knownEndForward = true;
			else
				priorBatch->knownEndBackward = true;
		}
	}

	/* xs_hitup isn't currently supported by amgetbatch scans */
	Assert(!scan->xs_hitup);

	return batch;
}

/*
 * amgetbatch utilities called by index AMs
 */
extern void indexam_util_batch_unlock(IndexScanDesc scan, IndexScanBatch batch,
									  Buffer buf);
extern IndexScanBatch indexam_util_batch_alloc(IndexScanDesc scan);
extern void indexam_util_batch_release(IndexScanDesc scan, IndexScanBatch batch);

/*
 * Utility macro for accessing the index AM's per-batch opaque data.
 *
 * Each batch allocation places the index AM opaque area at a fixed negative
 * offset from the IndexScanBatch pointer (see indexam_util_batch_alloc).
 * This macro returns a typed pointer to that area, asserting that everybody
 * has the same idea about where the index AM opaque area is in passing.
 */
#define indexam_util_batch_get_amdata(scan, batch, type) \
	(AssertMacro((scan)->batch_index_opaque_size == MAXALIGN(sizeof(type))), \
	 ((type *) ((char *) (batch) - MAXALIGN(sizeof(type)))))

#endif							/* INDEXBATCH_H */
