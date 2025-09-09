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

#include "storage/buf.h"

/* struct definitions appear in relscan.h */
typedef struct IndexScanDescData *IndexScanDesc;
typedef struct IndexScanBatchData *IndexScanBatch;

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
