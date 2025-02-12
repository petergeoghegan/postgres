/*-------------------------------------------------------------------------
 *
 * indexam.c
 *	  general index access method routines
 *
 * Portions Copyright (c) 1996-2025, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 *
 * IDENTIFICATION
 *	  src/backend/access/index/indexam.c
 *
 * INTERFACE ROUTINES
 *		index_open		- open an index relation by relation OID
 *		index_close		- close an index relation
 *		index_beginscan - start a scan of an index with amgettuple
 *		index_beginscan_bitmap - start a scan of an index with amgetbitmap
 *		index_rescan	- restart a scan of an index
 *		index_endscan	- end a scan
 *		index_insert	- insert an index tuple into a relation
 *		index_markpos	- mark a scan position
 *		index_restrpos	- restore a scan position
 *		index_parallelscan_estimate - estimate shared memory for parallel scan
 *		index_parallelscan_initialize - initialize parallel scan
 *		index_parallelrescan  - (re)start a parallel scan of an index
 *		index_beginscan_parallel - join parallel index scan
 *		index_getnext_tid	- get the next TID from a scan
 *		index_fetch_heap		- get the scan's next heap tuple
 *		index_getnext_slot	- get the next tuple from a scan
 *		index_getbitmap - get all tuples from a scan
 *		index_bulk_delete	- bulk deletion of index tuples
 *		index_vacuum_cleanup	- post-deletion cleanup of an index
 *		index_can_return	- does index support index-only scans?
 *		index_getprocid - get a support procedure OID
 *		index_getprocinfo - get a support procedure's lookup info
 *
 * NOTES
 *		This file contains the index_ routines which used
 *		to be a scattered collection of stuff in access/genam.
 *
 *-------------------------------------------------------------------------
 */

#include "postgres.h"

#include "access/amapi.h"
#include "access/nbtree.h"		/* XXX for MaxTIDsPerBTreePage (should remove) */
#include "access/relation.h"
#include "access/reloptions.h"
#include "access/relscan.h"
#include "access/tableam.h"
#include "catalog/index.h"
#include "catalog/pg_type.h"
#include "nodes/execnodes.h"
#include "pgstat.h"
#include "storage/lmgr.h"
#include "storage/predicate.h"
#include "storage/read_stream.h"
#include "utils/ruleutils.h"
#include "utils/snapmgr.h"
#include "utils/syscache.h"

/* enable reading batches / prefetching of TIDs from the index */
bool		enable_indexscan_batching = false;

/* ----------------------------------------------------------------
 *					macros used in index_ routines
 *
 * Note: the ReindexIsProcessingIndex() check in RELATION_CHECKS is there
 * to check that we don't try to scan or do retail insertions into an index
 * that is currently being rebuilt or pending rebuild.  This helps to catch
 * things that don't work when reindexing system catalogs, as well as prevent
 * user errors like index expressions that access their own tables.  The check
 * doesn't prevent the actual rebuild because we don't use RELATION_CHECKS
 * when calling the index AM's ambuild routine, and there is no reason for
 * ambuild to call its subsidiary routines through this file.
 * ----------------------------------------------------------------
 */
#define RELATION_CHECKS \
do { \
	Assert(RelationIsValid(indexRelation)); \
	Assert(PointerIsValid(indexRelation->rd_indam)); \
	if (unlikely(ReindexIsProcessingIndex(RelationGetRelid(indexRelation)))) \
		ereport(ERROR, \
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED), \
				 errmsg("cannot access index \"%s\" while it is being reindexed", \
						RelationGetRelationName(indexRelation)))); \
} while(0)

#define SCAN_CHECKS \
( \
	AssertMacro(IndexScanIsValid(scan)), \
	AssertMacro(RelationIsValid(scan->indexRelation)), \
	AssertMacro(PointerIsValid(scan->indexRelation->rd_indam)) \
)

#define CHECK_REL_PROCEDURE(pname) \
do { \
	if (indexRelation->rd_indam->pname == NULL) \
		elog(ERROR, "function \"%s\" is not defined for index \"%s\"", \
			 CppAsString(pname), RelationGetRelationName(indexRelation)); \
} while(0)

#define CHECK_SCAN_PROCEDURE(pname) \
do { \
	if (scan->indexRelation->rd_indam->pname == NULL) \
		elog(ERROR, "function \"%s\" is not defined for index \"%s\"", \
			 CppAsString(pname), RelationGetRelationName(scan->indexRelation)); \
} while(0)

#ifdef INDEXAM_DEBUG
#define DEBUG_LOG(...) elog(WARNING, __VA_ARGS__)
#else
#define DEBUG_LOG(...)
#endif

static IndexScanDesc index_beginscan_internal(Relation indexRelation,
											  int nkeys, int norderbys, Snapshot snapshot,
											  ParallelIndexScanDesc pscan, bool temp_snap);
static inline void validate_relation_kind(Relation r);

/* index batching */
static void index_batch_init(IndexScanDesc scan);
static void index_batch_reset(IndexScanDesc scan, bool complete);
static void index_batch_end(IndexScanDesc scan);
static bool index_batch_getnext(IndexScanDesc scan);
static void index_batch_free(IndexScanDesc scan, IndexScanBatch batch);
static ItemPointer index_batch_getnext_tid(IndexScanDesc scan,
										   ScanDirection direction);

static BlockNumber index_scan_stream_read_next(ReadStream *stream,
											   void *callback_private_data,
											   void *per_buffer_data);

static bool index_batch_pos_advance(IndexScanDesc scan, IndexScanBatchPos *pos);
static void index_batch_pos_reset(IndexScanDesc scan, IndexScanBatchPos *pos);
static void index_batch_kill_item(IndexScanDesc scan);

static void AssertCheckBatchPosValid(IndexScanDesc scan, IndexScanBatchPos *pos);
static void AssertCheckBatch(IndexScanDesc scan, IndexScanBatch batch);
static void AssertCheckBatches(IndexScanDesc scan);


#define INDEX_SCAN_BATCH(scan, idx)	\
		((scan)->xs_batches->batches[(idx) % (scan)->xs_batches->maxBatches])


/* ----------------------------------------------------------------
 *				   index_ interface functions
 * ----------------------------------------------------------------
 */

/* ----------------
 *		index_open - open an index relation by relation OID
 *
 *		If lockmode is not "NoLock", the specified kind of lock is
 *		obtained on the index.  (Generally, NoLock should only be
 *		used if the caller knows it has some appropriate lock on the
 *		index already.)
 *
 *		An error is raised if the index does not exist.
 *
 *		This is a convenience routine adapted for indexscan use.
 *		Some callers may prefer to use relation_open directly.
 * ----------------
 */
Relation
index_open(Oid relationId, LOCKMODE lockmode)
{
	Relation	r;

	r = relation_open(relationId, lockmode);

	validate_relation_kind(r);

	return r;
}

/* ----------------
 *		try_index_open - open an index relation by relation OID
 *
 *		Same as index_open, except return NULL instead of failing
 *		if the relation does not exist.
 * ----------------
 */
Relation
try_index_open(Oid relationId, LOCKMODE lockmode)
{
	Relation	r;

	r = try_relation_open(relationId, lockmode);

	/* leave if index does not exist */
	if (!r)
		return NULL;

	validate_relation_kind(r);

	return r;
}

/* ----------------
 *		index_close - close an index relation
 *
 *		If lockmode is not "NoLock", we then release the specified lock.
 *
 *		Note that it is often sensible to hold a lock beyond index_close;
 *		in that case, the lock is released automatically at xact end.
 * ----------------
 */
void
index_close(Relation relation, LOCKMODE lockmode)
{
	LockRelId	relid = relation->rd_lockInfo.lockRelId;

	Assert(lockmode >= NoLock && lockmode < MAX_LOCKMODES);

	/* The relcache does the real work... */
	RelationClose(relation);

	if (lockmode != NoLock)
		UnlockRelationId(&relid, lockmode);
}

/* ----------------
 *		validate_relation_kind - check the relation's kind
 *
 *		Make sure relkind is an index or a partitioned index.
 * ----------------
 */
static inline void
validate_relation_kind(Relation r)
{
	if (r->rd_rel->relkind != RELKIND_INDEX &&
		r->rd_rel->relkind != RELKIND_PARTITIONED_INDEX)
		ereport(ERROR,
				(errcode(ERRCODE_WRONG_OBJECT_TYPE),
				 errmsg("\"%s\" is not an index",
						RelationGetRelationName(r))));
}


/* ----------------
 *		index_insert - insert an index tuple into a relation
 * ----------------
 */
bool
index_insert(Relation indexRelation,
			 Datum *values,
			 bool *isnull,
			 ItemPointer heap_t_ctid,
			 Relation heapRelation,
			 IndexUniqueCheck checkUnique,
			 bool indexUnchanged,
			 IndexInfo *indexInfo)
{
	RELATION_CHECKS;
	CHECK_REL_PROCEDURE(aminsert);

	if (!(indexRelation->rd_indam->ampredlocks))
		CheckForSerializableConflictIn(indexRelation,
									   (ItemPointer) NULL,
									   InvalidBlockNumber);

	return indexRelation->rd_indam->aminsert(indexRelation, values, isnull,
											 heap_t_ctid, heapRelation,
											 checkUnique, indexUnchanged,
											 indexInfo);
}

/* -------------------------
 *		index_insert_cleanup - clean up after all index inserts are done
 * -------------------------
 */
void
index_insert_cleanup(Relation indexRelation,
					 IndexInfo *indexInfo)
{
	RELATION_CHECKS;

	if (indexRelation->rd_indam->aminsertcleanup)
		indexRelation->rd_indam->aminsertcleanup(indexRelation, indexInfo);
}

/*
 * index_beginscan - start a scan of an index with amgettuple
 *
 * Caller must be holding suitable locks on the heap and the index.
 */
IndexScanDesc
index_beginscan(Relation heapRelation,
				Relation indexRelation,
				Snapshot snapshot,
				int nkeys, int norderbys,
				bool enable_batching)
{
	ReadStream   *rs = NULL;
	IndexScanDesc scan;

	Assert(snapshot != InvalidSnapshot);

	scan = index_beginscan_internal(indexRelation, nkeys, norderbys, snapshot, NULL, false);

	/*
	 * Save additional parameters into the scandesc.  Everything else was set
	 * up by RelationGetIndexScan.
	 */
	scan->heapRelation = heapRelation;
	scan->xs_snapshot = snapshot;

	/*
	 * If explicitly requested and supported by both the index AM and the
	 * plan, initialize batching info. We only use stream read API with
	 * batching enabled (so not with systable scans). But maybe we should
	 * change that, and just use different read_next callbacks (or something
	 * like that)?
	 *
	 * XXX We do this after ambeginscan(), which means the AM can't init the
	 * private data in there (it doesn't even know if batching will be used at
	 * that point).
	 *
	 * XXX Maybe we should have a separate "amcanbatch" call, to let the AM
	 * decide if batching is supported depending on the scan details. That
	 * might be needed for certain index AMs, that can do batching only for
	 * some scans (I'm thinking about GiST/SP-GiST indexes, with ORDER BY).
	 *
	 * XXX Do this before initializing xs_heapfetch, so that we can pass the
	 * read stream to it.
	 */
	if ((indexRelation->rd_indam->amgetbatch != NULL) &&
		enable_batching &&
		enable_indexscan_batching)
	{
		index_batch_init(scan);

		/* initialize stream */
		rs = read_stream_begin_relation(READ_STREAM_DEFAULT,
										NULL,
										heapRelation,
										MAIN_FORKNUM,
										index_scan_stream_read_next,
										scan,
										0);
	}

	/* prepare to fetch index matches from table */
	scan->xs_heapfetch = table_index_fetch_begin(heapRelation, rs);

	return scan;
}

/*
 * index_beginscan_bitmap - start a scan of an index with amgetbitmap
 *
 * As above, caller had better be holding some lock on the parent heap
 * relation, even though it's not explicitly mentioned here.
 */
IndexScanDesc
index_beginscan_bitmap(Relation indexRelation,
					   Snapshot snapshot,
					   int nkeys)
{
	IndexScanDesc scan;

	Assert(snapshot != InvalidSnapshot);

	scan = index_beginscan_internal(indexRelation, nkeys, 0, snapshot, NULL, false);

	/*
	 * Save additional parameters into the scandesc.  Everything else was set
	 * up by RelationGetIndexScan.
	 */
	scan->xs_snapshot = snapshot;

	return scan;
}

/*
 * index_beginscan_internal --- common code for index_beginscan variants
 */
static IndexScanDesc
index_beginscan_internal(Relation indexRelation,
						 int nkeys, int norderbys, Snapshot snapshot,
						 ParallelIndexScanDesc pscan, bool temp_snap)
{
	IndexScanDesc scan;

	RELATION_CHECKS;
	CHECK_REL_PROCEDURE(ambeginscan);

	if (!(indexRelation->rd_indam->ampredlocks))
		PredicateLockRelation(indexRelation, snapshot);

	/*
	 * We hold a reference count to the relcache entry throughout the scan.
	 */
	RelationIncrementReferenceCount(indexRelation);

	/*
	 * Tell the AM to open a scan.
	 */
	scan = indexRelation->rd_indam->ambeginscan(indexRelation, nkeys,
												norderbys);
	/* Initialize information for parallel scan. */
	scan->parallel_scan = pscan;
	scan->xs_temp_snap = temp_snap;

	/*
	 * No batching by default, so set it to NULL. Will be initialized later if
	 * batching is requested and AM supports it.
	 */
	scan->xs_batches = NULL;

	return scan;
}

/* ----------------
 *		index_rescan  - (re)start a scan of an index
 *
 * During a restart, the caller may specify a new set of scankeys and/or
 * orderbykeys; but the number of keys cannot differ from what index_beginscan
 * was told.  (Later we might relax that to "must not exceed", but currently
 * the index AMs tend to assume that scan->numberOfKeys is what to believe.)
 * To restart the scan without changing keys, pass NULL for the key arrays.
 * (Of course, keys *must* be passed on the first call, unless
 * scan->numberOfKeys is zero.)
 * ----------------
 */
void
index_rescan(IndexScanDesc scan,
			 ScanKey keys, int nkeys,
			 ScanKey orderbys, int norderbys)
{
	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(amrescan);

	Assert(nkeys == scan->numberOfKeys);
	Assert(norderbys == scan->numberOfOrderBys);

	/* Release resources (like buffer pins) from table accesses */
	if (scan->xs_heapfetch)
		table_index_fetch_reset(scan->xs_heapfetch);

	scan->kill_prior_tuple = false; /* for safety */
	scan->xs_heap_continue = false;

	/*
	 * Reset the batching. This makes it look like there are no batches,
	 * discards reads already scheduled to the read stream, etc. We Do this
	 * before calling amrescan, so that it can reinitialize everything.
	 */
	index_batch_reset(scan, true);

	scan->indexRelation->rd_indam->amrescan(scan, keys, nkeys,
											orderbys, norderbys);
}

/* ----------------
 *		index_endscan - end a scan
 * ----------------
 */
void
index_endscan(IndexScanDesc scan)
{
	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(amendscan);

	/* Cleanup batching, so that the AM can release pins and so on. */
	index_batch_end(scan);

	/* Release resources (like buffer pins) from table accesses */
	if (scan->xs_heapfetch)
	{
		table_index_fetch_end(scan->xs_heapfetch);
		scan->xs_heapfetch = NULL;
	}

	/* End the AM's scan */
	scan->indexRelation->rd_indam->amendscan(scan);

	/* Release index refcount acquired by index_beginscan */
	RelationDecrementReferenceCount(scan->indexRelation);

	if (scan->xs_temp_snap)
		UnregisterSnapshot(scan->xs_snapshot);

	/* Release the scan data structure itself */
	IndexScanEnd(scan);
}

/* ----------------
 *		index_markpos  - mark a scan position
 * ----------------
 */
void
index_markpos(IndexScanDesc scan)
{
	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(ammarkpos);

	/*
	 * Without batching, just use the ammarkpos() callback. With batching
	 * everything is handled at this layer, without AM.
	 */
	if (scan->xs_batches == NULL)
		scan->indexRelation->rd_indam->ammarkpos(scan);
	else
	{
		IndexScanBatches  *batches = scan->xs_batches;
		IndexScanBatchPos *pos = &batches->markPos;
		IndexScanBatchData *batch = batches->markBatch;

		/*
		 * Free the previous mark batch (if any). We actually free it only
		 * if the batch is no longer valid (in the current first/next range).
		 * This means that if we're marking the same batch (different item),
		 * we don't really do anything.
		 *
		 * XXX Should have some macro to check if pos is in queue, etc.
		 */
		if ((batch != NULL) &&
			(pos->batch < batches->firstBatch || pos->batch >= batches->nextBatch))
		{
			batches->markBatch = NULL;
			index_batch_free(scan, batch);
		}

		/* just copy the read position (which has to be valid) */
		batches->markPos = batches->readPos;
		batches->markBatch = INDEX_SCAN_BATCH(scan, batches->markPos.batch);

		/* FIXME we need to make sure the batch does not get freed */

		AssertCheckBatchPosValid(scan, &batches->markPos);
	}
}

/* ----------------
 *		index_restrpos	- restore a scan position
 *
 * NOTE: this only restores the internal scan state of the index AM.  See
 * comments for ExecRestrPos().
 *
 * NOTE: For heap, in the presence of HOT chains, mark/restore only works
 * correctly if the scan's snapshot is MVCC-safe; that ensures that there's at
 * most one returnable tuple in each HOT chain, and so restoring the prior
 * state at the granularity of the index AM is sufficient.  Since the only
 * current user of mark/restore functionality is nodeMergejoin.c, this
 * effectively means that merge-join plans only work for MVCC snapshots.  This
 * could be fixed if necessary, but for now it seems unimportant.
 * ----------------
 */
void
index_restrpos(IndexScanDesc scan)
{
	Assert(IsMVCCSnapshot(scan->xs_snapshot));

	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(amrestrpos);

	/* release resources (like buffer pins) from table accesses */
	if (scan->xs_heapfetch)
		table_index_fetch_reset(scan->xs_heapfetch);

	scan->kill_prior_tuple = false; /* for safety */
	scan->xs_heap_continue = false;

	/*
	 * Without batching, just use the amrestrpos() callback. With batching
	 * everything is handled at this layer, without AM.
	 */
	if (scan->xs_batches == NULL)
		scan->indexRelation->rd_indam->amrestrpos(scan);
	else
	{
		IndexScanBatches   *batches = scan->xs_batches;
		IndexScanBatchPos  *pos = &batches->markPos;
		IndexScanBatchData *batch = scan->xs_batches->markBatch;

		Assert(batch != NULL);

		// the pos can be invalid, if we already advanced pas the the marked
		// batch (and stashed it in markBatch instead of freeing)
		// AssertCheckBatchPosValid(scan, &pos);

		/* FIXME check the batch was not freed yet */
		index_batch_reset(scan, false);

		/* XXX ugly - we reinstall the batch as the "first and only one" */
		batches->markPos = *pos;
		batches->readPos = *pos;
		batches->firstBatch = pos->batch;
		batches->nextBatch = (batches->firstBatch + 1);

		INDEX_SCAN_BATCH(scan, batches->markPos.batch) = batch;

		/* XXX I really dislike that we have so many definitions of "current"
		 * batch. We have readPos, streamPos, currentBatch, ... We should make
		 * that somewhat more consistent. */
		batches->currentBatch = batch;
		batches->markBatch = batch; /* also remember this */
	}
}

/*
 * index_parallelscan_estimate - estimate shared memory for parallel scan
 */
Size
index_parallelscan_estimate(Relation indexRelation, int nkeys, int norderbys,
							Snapshot snapshot)
{
	Size		nbytes;

	Assert(snapshot != InvalidSnapshot);

	RELATION_CHECKS;

	nbytes = offsetof(ParallelIndexScanDescData, ps_snapshot_data);
	nbytes = add_size(nbytes, EstimateSnapshotSpace(snapshot));
	nbytes = MAXALIGN(nbytes);

	/*
	 * If amestimateparallelscan is not provided, assume there is no
	 * AM-specific data needed.  (It's hard to believe that could work, but
	 * it's easy enough to cater to it here.)
	 */
	if (indexRelation->rd_indam->amestimateparallelscan != NULL)
		nbytes = add_size(nbytes,
						  indexRelation->rd_indam->amestimateparallelscan(nkeys,
																		  norderbys));

	return nbytes;
}

/*
 * index_parallelscan_initialize - initialize parallel scan
 *
 * We initialize both the ParallelIndexScanDesc proper and the AM-specific
 * information which follows it.
 *
 * This function calls access method specific initialization routine to
 * initialize am specific information.  Call this just once in the leader
 * process; then, individual workers attach via index_beginscan_parallel.
 */
void
index_parallelscan_initialize(Relation heapRelation, Relation indexRelation,
							  Snapshot snapshot, ParallelIndexScanDesc target)
{
	Size		offset;

	Assert(snapshot != InvalidSnapshot);

	RELATION_CHECKS;

	offset = add_size(offsetof(ParallelIndexScanDescData, ps_snapshot_data),
					  EstimateSnapshotSpace(snapshot));
	offset = MAXALIGN(offset);

	target->ps_locator = heapRelation->rd_locator;
	target->ps_indexlocator = indexRelation->rd_locator;
	target->ps_offset = offset;
	SerializeSnapshot(snapshot, target->ps_snapshot_data);

	/* aminitparallelscan is optional; assume no-op if not provided by AM */
	if (indexRelation->rd_indam->aminitparallelscan != NULL)
	{
		void	   *amtarget;

		amtarget = OffsetToPointer(target, offset);
		indexRelation->rd_indam->aminitparallelscan(amtarget);
	}
}

/* ----------------
 *		index_parallelrescan  - (re)start a parallel scan of an index
 * ----------------
 */
void
index_parallelrescan(IndexScanDesc scan)
{
	SCAN_CHECKS;

	if (scan->xs_heapfetch)
		table_index_fetch_reset(scan->xs_heapfetch);

	/*
	 * Reset the batching. This makes it look like there are no batches,
	 * discards reads already scheduled to the read stream, etc. We Do this
	 * before calling amrescan, so that it can reinitialize everything.
	 */
	index_batch_reset(scan, true);

	/* amparallelrescan is optional; assume no-op if not provided by AM */
	if (scan->indexRelation->rd_indam->amparallelrescan != NULL)
		scan->indexRelation->rd_indam->amparallelrescan(scan);
}

/*
 * index_beginscan_parallel - join parallel index scan
 *
 * Caller must be holding suitable locks on the heap and the index.
 */
IndexScanDesc
index_beginscan_parallel(Relation heaprel, Relation indexrel, int nkeys,
						 int norderbys, ParallelIndexScanDesc pscan,
						 bool enable_batching)
{
	Snapshot	snapshot;
	IndexScanDesc scan;
	ReadStream   *rs = NULL;

	Assert(RelFileLocatorEquals(heaprel->rd_locator, pscan->ps_locator));
	Assert(RelFileLocatorEquals(indexrel->rd_locator, pscan->ps_indexlocator));

	snapshot = RestoreSnapshot(pscan->ps_snapshot_data);
	RegisterSnapshot(snapshot);
	scan = index_beginscan_internal(indexrel, nkeys, norderbys, snapshot,
									pscan, true);

	/*
	 * Save additional parameters into the scandesc.  Everything else was set
	 * up by index_beginscan_internal.
	 */
	scan->heapRelation = heaprel;
	scan->xs_snapshot = snapshot;

	/*
	 * If explicitly requested and supported by both the index AM and the
	 * plan, initialize batching info. We only use stream read API with
	 * batching enabled (so not with systable scans). But maybe we should
	 * change that, and just use different read_next callbacks (or something
	 * like that)?
	 *
	 * XXX We do this after ambeginscan(), which means the AM can't init the
	 * private data in there (it doesn't even know if batching will be used at
	 * that point).
	 *
	 * XXX Maybe we should have a separate "amcanbatch" call, to let the AM
	 * decide if batching is supported depending on the scan details. That
	 * might be needed for certain index AMs, that can do batching only for
	 * some scans (I'm thinking about GiST/SP-GiST indexes, with ORDER BY).
	 *
	 * XXX Do this before initializing xs_heapfetch, so that we can pass the
	 * read stream to it.
	 *
	 * XXX Pretty duplicate with the code in index_beginscan(), so maybe move
	 * into a shared function.
	 */
	if ((indexrel->rd_indam->amgetbatch != NULL) &&
		enable_batching &&
		enable_indexscan_batching)
	{
		index_batch_init(scan);

		/* initialize stream */
		rs = read_stream_begin_relation(READ_STREAM_DEFAULT,
										NULL,
										heaprel,
										MAIN_FORKNUM,
										index_scan_stream_read_next,
										scan,
										0);
	}

	/* prepare to fetch index matches from table */
	scan->xs_heapfetch = table_index_fetch_begin(heaprel, rs);

	return scan;
}

/* ----------------
 * index_getnext_tid - get the next TID from a scan
 *
 * The result is the next TID satisfying the scan keys,
 * or NULL if no more matching tuples exist.
 * ----------------
 */
ItemPointer
index_getnext_tid(IndexScanDesc scan, ScanDirection direction)
{
	bool		found;

	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(amgettuple);

	/* XXX: we should assert that a snapshot is pushed or registered */
	Assert(TransactionIdIsValid(RecentXmin));

	/*
	 * When using batching (which may be disabled for various reasons - e.g.
	 * through a GUC, the index AM not supporting it), redirect the code to
	 * the "batch" variant. If needed (e.g. for the first call) the call may
	 * read the first batch (leaf page) from the index.
	 *
	 * XXX Maybe we should enable batching based on the plan too, so that we
	 * don't do batching when it's probably useless (e.g. semijoins or queries
	 * with LIMIT 1 etc.). But maybe the approach with slow ramp-up (starting
	 * with small batches) will handle that well enough. This is what the
	 * amcanbatch() callback would determine.
	 *
	 * XXX Perhaps it'd be possible to do both in index_getnext_slot(), i.e.
	 * call either the original code without batching, or the new batching
	 * code if supported/enabled. It's not great to have duplicated code.
	 *
	 * XXX If needed, index_batch_getnext_tid loads the batch.
	 */
	if (scan->xs_batches != NULL)
		return index_batch_getnext_tid(scan, direction);

	/*
	 * The AM's amgettuple proc finds the next index entry matching the scan
	 * keys, and puts the TID into scan->xs_heaptid.  It should also set
	 * scan->xs_recheck and possibly scan->xs_itup/scan->xs_hitup, though we
	 * pay no attention to those fields here.
	 */
	found = scan->indexRelation->rd_indam->amgettuple(scan, direction);

	/* Reset kill flag immediately for safety */
	scan->kill_prior_tuple = false;
	scan->xs_heap_continue = false;

	/* If we're out of index entries, we're done */
	if (!found)
	{
		/* release resources (like buffer pins) from table accesses */
		if (scan->xs_heapfetch)
			table_index_fetch_reset(scan->xs_heapfetch);

		return NULL;
	}
	Assert(ItemPointerIsValid(&scan->xs_heaptid));

	pgstat_count_index_tuples(scan->indexRelation, 1);

	/* Return the TID of the tuple we found. */
	return &scan->xs_heaptid;
}

/* ----------------
 *		index_fetch_heap - get the scan's next heap tuple
 *
 * The result is a visible heap tuple associated with the index TID most
 * recently fetched by index_getnext_tid, or NULL if no more matching tuples
 * exist.  (There can be more than one matching tuple because of HOT chains,
 * although when using an MVCC snapshot it should be impossible for more than
 * one such tuple to exist.)
 *
 * On success, the buffer containing the heap tup is pinned (the pin will be
 * dropped in a future index_getnext_tid, index_fetch_heap or index_endscan
 * call).
 *
 * Note: caller must check scan->xs_recheck, and perform rechecking of the
 * scan keys if required.  We do not do that here because we don't have
 * enough information to do it efficiently in the general case.
 * ----------------
 */
bool
index_fetch_heap(IndexScanDesc scan, TupleTableSlot *slot)
{
	bool		all_dead = false;
	bool		found;

	found = table_index_fetch_tuple(scan->xs_heapfetch, &scan->xs_heaptid,
									scan->xs_snapshot, slot,
									&scan->xs_heap_continue, &all_dead);

	if (found)
		pgstat_count_heap_fetch(scan->indexRelation);

	/*
	 * If we scanned a whole HOT chain and found only dead tuples, tell index
	 * AM to kill its entry for that TID (this will take effect in the next
	 * amgettuple call, in index_getnext_tid).  We do not do this when in
	 * recovery because it may violate MVCC to do so.  See comments in
	 * RelationGetIndexScan().
	 */
	if (!scan->xactStartedInRecovery)
	{
		if (scan->xs_batches == NULL)
		{
			scan->kill_prior_tuple = all_dead;
		}
		else if (all_dead)
		{
			/* batch case - record the killed tuple in the batch */
			index_batch_kill_item(scan);
		}
	}

	return found;
}

/* ----------------
 *		index_getnext_slot - get the next tuple from a scan
 *
 * The result is true if a tuple satisfying the scan keys and the snapshot was
 * found, false otherwise.  The tuple is stored in the specified slot.
 *
 * On success, resources (like buffer pins) are likely to be held, and will be
 * dropped by a future index_getnext_tid, index_fetch_heap or index_endscan
 * call).
 *
 * Note: caller must check scan->xs_recheck, and perform rechecking of the
 * scan keys if required.  We do not do that here because we don't have
 * enough information to do it efficiently in the general case.
 * ----------------
 */
bool
index_getnext_slot(IndexScanDesc scan, ScanDirection direction, TupleTableSlot *slot)
{
	for (;;)
	{
		if (!scan->xs_heap_continue)
		{
			ItemPointer tid;

			/* Time to fetch the next TID from the index */
			tid = index_getnext_tid(scan, direction);

			/* If we're out of index entries, we're done */
			if (tid == NULL)
				break;

			Assert(ItemPointerEquals(tid, &scan->xs_heaptid));
		}

		/*
		 * Fetch the next (or only) visible heap tuple for this index entry.
		 * If we don't find anything, loop around and grab the next TID from
		 * the index.
		 */
		Assert(ItemPointerIsValid(&scan->xs_heaptid));
		if (index_fetch_heap(scan, slot))
			return true;
	}

	return false;
}

/* ----------------
 *		index_getbitmap - get all tuples at once from an index scan
 *
 * Adds the TIDs of all heap tuples satisfying the scan keys to a bitmap.
 * Since there's no interlock between the index scan and the eventual heap
 * access, this is only safe to use with MVCC-based snapshots: the heap
 * item slot could have been replaced by a newer tuple by the time we get
 * to it.
 *
 * Returns the number of matching tuples found.  (Note: this might be only
 * approximate, so it should only be used for statistical purposes.)
 * ----------------
 */
int64
index_getbitmap(IndexScanDesc scan, TIDBitmap *bitmap)
{
	int64		ntids;

	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(amgetbitmap);

	/* just make sure this is false... */
	scan->kill_prior_tuple = false;

	/*
	 * have the am's getbitmap proc do all the work.
	 */
	ntids = scan->indexRelation->rd_indam->amgetbitmap(scan, bitmap);

	pgstat_count_index_tuples(scan->indexRelation, ntids);

	return ntids;
}

/* ----------------
 *		index_bulk_delete - do mass deletion of index entries
 *
 *		callback routine tells whether a given main-heap tuple is
 *		to be deleted
 *
 *		return value is an optional palloc'd struct of statistics
 * ----------------
 */
IndexBulkDeleteResult *
index_bulk_delete(IndexVacuumInfo *info,
				  IndexBulkDeleteResult *istat,
				  IndexBulkDeleteCallback callback,
				  void *callback_state)
{
	Relation	indexRelation = info->index;

	RELATION_CHECKS;
	CHECK_REL_PROCEDURE(ambulkdelete);

	return indexRelation->rd_indam->ambulkdelete(info, istat,
												 callback, callback_state);
}

/* ----------------
 *		index_vacuum_cleanup - do post-deletion cleanup of an index
 *
 *		return value is an optional palloc'd struct of statistics
 * ----------------
 */
IndexBulkDeleteResult *
index_vacuum_cleanup(IndexVacuumInfo *info,
					 IndexBulkDeleteResult *istat)
{
	Relation	indexRelation = info->index;

	RELATION_CHECKS;
	CHECK_REL_PROCEDURE(amvacuumcleanup);

	return indexRelation->rd_indam->amvacuumcleanup(info, istat);
}

/* ----------------
 *		index_can_return
 *
 *		Does the index access method support index-only scans for the given
 *		column?
 * ----------------
 */
bool
index_can_return(Relation indexRelation, int attno)
{
	RELATION_CHECKS;

	/* amcanreturn is optional; assume false if not provided by AM */
	if (indexRelation->rd_indam->amcanreturn == NULL)
		return false;

	return indexRelation->rd_indam->amcanreturn(indexRelation, attno);
}

/* ----------------
 *		index_getprocid
 *
 *		Index access methods typically require support routines that are
 *		not directly the implementation of any WHERE-clause query operator
 *		and so cannot be kept in pg_amop.  Instead, such routines are kept
 *		in pg_amproc.  These registered procedure OIDs are assigned numbers
 *		according to a convention established by the access method.
 *		The general index code doesn't know anything about the routines
 *		involved; it just builds an ordered list of them for
 *		each attribute on which an index is defined.
 *
 *		As of Postgres 8.3, support routines within an operator family
 *		are further subdivided by the "left type" and "right type" of the
 *		query operator(s) that they support.  The "default" functions for a
 *		particular indexed attribute are those with both types equal to
 *		the index opclass' opcintype (note that this is subtly different
 *		from the indexed attribute's own type: it may be a binary-compatible
 *		type instead).  Only the default functions are stored in relcache
 *		entries --- access methods can use the syscache to look up non-default
 *		functions.
 *
 *		This routine returns the requested default procedure OID for a
 *		particular indexed attribute.
 * ----------------
 */
RegProcedure
index_getprocid(Relation irel,
				AttrNumber attnum,
				uint16 procnum)
{
	RegProcedure *loc;
	int			nproc;
	int			procindex;

	nproc = irel->rd_indam->amsupport;

	Assert(procnum > 0 && procnum <= (uint16) nproc);

	procindex = (nproc * (attnum - 1)) + (procnum - 1);

	loc = irel->rd_support;

	Assert(loc != NULL);

	return loc[procindex];
}

/* ----------------
 *		index_getprocinfo
 *
 *		This routine allows index AMs to keep fmgr lookup info for
 *		support procs in the relcache.  As above, only the "default"
 *		functions for any particular indexed attribute are cached.
 *
 * Note: the return value points into cached data that will be lost during
 * any relcache rebuild!  Therefore, either use the callinfo right away,
 * or save it only after having acquired some type of lock on the index rel.
 * ----------------
 */
FmgrInfo *
index_getprocinfo(Relation irel,
				  AttrNumber attnum,
				  uint16 procnum)
{
	FmgrInfo   *locinfo;
	int			nproc;
	int			optsproc;
	int			procindex;

	nproc = irel->rd_indam->amsupport;
	optsproc = irel->rd_indam->amoptsprocnum;

	Assert(procnum > 0 && procnum <= (uint16) nproc);

	procindex = (nproc * (attnum - 1)) + (procnum - 1);

	locinfo = irel->rd_supportinfo;

	Assert(locinfo != NULL);

	locinfo += procindex;

	/* Initialize the lookup info if first time through */
	if (locinfo->fn_oid == InvalidOid)
	{
		RegProcedure *loc = irel->rd_support;
		RegProcedure procId;

		Assert(loc != NULL);

		procId = loc[procindex];

		/*
		 * Complain if function was not found during IndexSupportInitialize.
		 * This should not happen unless the system tables contain bogus
		 * entries for the index opclass.  (If an AM wants to allow a support
		 * function to be optional, it can use index_getprocid.)
		 */
		if (!RegProcedureIsValid(procId))
			elog(ERROR, "missing support function %d for attribute %d of index \"%s\"",
				 procnum, attnum, RelationGetRelationName(irel));

		fmgr_info_cxt(procId, locinfo, irel->rd_indexcxt);

		if (procnum != optsproc)
		{
			/* Initialize locinfo->fn_expr with opclass options Const */
			bytea	  **attoptions = RelationGetIndexAttOptions(irel, false);
			MemoryContext oldcxt = MemoryContextSwitchTo(irel->rd_indexcxt);

			set_fn_opclass_options(locinfo, attoptions[attnum - 1]);

			MemoryContextSwitchTo(oldcxt);
		}
	}

	return locinfo;
}

/* ----------------
 *		index_store_float8_orderby_distances
 *
 *		Convert AM distance function's results (that can be inexact)
 *		to ORDER BY types and save them into xs_orderbyvals/xs_orderbynulls
 *		for a possible recheck.
 * ----------------
 */
void
index_store_float8_orderby_distances(IndexScanDesc scan, Oid *orderByTypes,
									 IndexOrderByDistance *distances,
									 bool recheckOrderBy)
{
	int			i;

	Assert(distances || !recheckOrderBy);

	scan->xs_recheckorderby = recheckOrderBy;

	for (i = 0; i < scan->numberOfOrderBys; i++)
	{
		if (orderByTypes[i] == FLOAT8OID)
		{
#ifndef USE_FLOAT8_BYVAL
			/* must free any old value to avoid memory leakage */
			if (!scan->xs_orderbynulls[i])
				pfree(DatumGetPointer(scan->xs_orderbyvals[i]));
#endif
			if (distances && !distances[i].isnull)
			{
				scan->xs_orderbyvals[i] = Float8GetDatum(distances[i].value);
				scan->xs_orderbynulls[i] = false;
			}
			else
			{
				scan->xs_orderbyvals[i] = (Datum) 0;
				scan->xs_orderbynulls[i] = true;
			}
		}
		else if (orderByTypes[i] == FLOAT4OID)
		{
			/* convert distance function's result to ORDER BY type */
			if (distances && !distances[i].isnull)
			{
				scan->xs_orderbyvals[i] = Float4GetDatum((float4) distances[i].value);
				scan->xs_orderbynulls[i] = false;
			}
			else
			{
				scan->xs_orderbyvals[i] = (Datum) 0;
				scan->xs_orderbynulls[i] = true;
			}
		}
		else
		{
			/*
			 * If the ordering operator's return value is anything else, we
			 * don't know how to convert the float8 bound calculated by the
			 * distance function to that.  The executor won't actually need
			 * the order by values we return here, if there are no lossy
			 * results, so only insist on converting if the *recheck flag is
			 * set.
			 */
			if (scan->xs_recheckorderby)
				elog(ERROR, "ORDER BY operator must return float8 or float4 if the distance function is lossy");
			scan->xs_orderbynulls[i] = true;
		}
	}
}

/* ----------------
 *      index_opclass_options
 *
 *      Parse opclass-specific options for index column.
 * ----------------
 */
bytea *
index_opclass_options(Relation indrel, AttrNumber attnum, Datum attoptions,
					  bool validate)
{
	int			amoptsprocnum = indrel->rd_indam->amoptsprocnum;
	Oid			procid = InvalidOid;
	FmgrInfo   *procinfo;
	local_relopts relopts;

	/* fetch options support procedure if specified */
	if (amoptsprocnum != 0)
		procid = index_getprocid(indrel, attnum, amoptsprocnum);

	if (!OidIsValid(procid))
	{
		Oid			opclass;
		Datum		indclassDatum;
		oidvector  *indclass;

		if (!DatumGetPointer(attoptions))
			return NULL;		/* ok, no options, no procedure */

		/*
		 * Report an error if the opclass's options-parsing procedure does not
		 * exist but the opclass options are specified.
		 */
		indclassDatum = SysCacheGetAttrNotNull(INDEXRELID, indrel->rd_indextuple,
											   Anum_pg_index_indclass);
		indclass = (oidvector *) DatumGetPointer(indclassDatum);
		opclass = indclass->values[attnum - 1];

		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("operator class %s has no options",
						generate_opclass_name(opclass))));
	}

	init_local_reloptions(&relopts, 0);

	procinfo = index_getprocinfo(indrel, attnum, amoptsprocnum);

	(void) FunctionCall1(procinfo, PointerGetDatum(&relopts));

	return build_local_reloptions(&relopts, attoptions, validate);
}

/*
 * INDEX BATCHING AND PREFETCHING
 *
 * Allows reading chunks of items from an index, instead of reading them
 * one by one. This reduces the overhead of accessing index pages, and
 * also allows acting on "future" TIDs - e.g. we can prefetch heap pages
 * that will be needed, etc.
 *
 * For the purpose of this description, a "batch" is roughly equivalent
 * to "tuples from a single leaf page". This may not be entirely correct
 * for some special index AMs (e.g. ordered scans in SP-GiST), but we're
 * going to focus on BTREE for now.
 *
 *
 * index AM vs. indexam.c
 * ----------------------
 * In the following text "index AM" refers to a specific AM implementation,
 * e.g. BTREE (code in src/backend/access/nbtree), while indexam.c is the
 * shared executor level used to interact with indexes.
 *
 *
 * why support batching
 * --------------------
 * Why do we want to support batching? The initial motivation was to allow
 * prefetching. With regular index scans we could prefetch heap TIDs from
 * a single leaf page, but that's hidden in each AM, and it seems wrong to
 * do these "heap" prefetches from within the index AM. This is why the
 * prefetching "abstracts" the idea of a batch, and moves it to the common
 * indexam.c layer.
 *
 * Another reason why prefetching at the AM level is that it can only
 * prefetch within a single leaf page - so as we're nearing the end of
 * the page, the prefetch distance drops to 0. Most indexes have many
 * index items per leaf page, so this is not a fatal issue, but it's not
 * great either.
 *
 *
 * index AM batching
 * -----------------
 * The regular (old) index scans access leaf pages one at a time, and
 * discard the "current leaf" before proceeding to the next one (this
 * ignores mark/restore, but that's a detail). All this state is stored
 * at the scan-level (e.g. in BTScanOpaque for BTREE). So there can be
 * only one leaf page "loaded" into the scan at a time.
 *
 * The batching relaxes this, and allows keeping multiple leaf pages for
 * a scan at the same time. It's a bit like having multiple "positions"
 * within the index at the same time.
 *
 * The batches are kept in the index scan, but not in the "scan opaque"
 * struct. Instead, there's an array (or queue) of leaf pages, and some
 * of the information was moved from the scan-level opaque struct to a
 * new batch-level struct.
 *
 * This means AM-specific scan-opaque struct gets split and moved into
 * three places:
 *
 * 1) scan-opaque struct - Fields that are truly related to the scan as
 *    a whole remain in the struct (which is AM-specific, i.e. each AM
 *    method may keep something different).
 *
 * 2) batch-opaque struct - Fields that are really about individual leaf
 *    pages are moved to a new batch-level struct. A good example are
 *    for example the position of the leaf page in the index (current
 *    page, left/righ pages, etc.).
 *
 * 3) batch struct itself - A big part of the refactoring is introducing
 *    a common representation of a batch. Until now each AM had it's own
 *    way of representing tuples from a leaf page, and accessing it
 *    required going through the AM again. The common representation
 *    allows accessing the batches through the indexam.c layer, without
 *    having to go through the AM.
 *
 * To support batching, the index AM needs to implement two optional
 * callbacks - amgetbatch() and amfreebatch(), which load data from the
 * "next" leaf page, and then free it when the batch is no longer needed.
 *
 * For now the amgettuple() callback is still required even for AMs that
 * support batching, so that we can fall-back to the non-batched scan
 * for cases when we don't allow batching (e.g. scans of system tables)
 * or when batching is disabled using the enable_indexscan_batching GUC.
 *
 * The AM may need to keep pins on the leaf pages. This is considered
 * the responsibility of the AM, as it's likely dependent on the internal
 * details of the index AM.
 *
 *
 * For now, each batch is exactly one whole leaf page. We might allow
 * batches to be smaller or larger, but that doesn't seem very useful.
 * It would make things more complex, without providing much benefit.
 * Ultimately it's up to the index AM - it can produce any batches it
 * wants, as long as it keeps necessary information in the batch-opaque
 * struct, and handles this in the amgetbatch/amfreebatch callbacks.
 *
 *
 * batch = sliding window
 * ----------------------
 * A good way to visualize batching is a sliding window over the key
 * space of an index. At any time, we have a "window" representing a
 * range of the keys, consisting of one or more batches, each with items
 * from a single leaf page.
 *
 *
 * prefetching leaf pages
 * ----------------------
 * This patch is only about prefetching pages from the indexed relation
 * (e.g. heap), not about prefetching index leaf pages etc. The read_next
 * callback does read leaf pages when needed (after reaching the end of
 * the current batch), but this is synchronous, and the callback will
 * block until the leaf page is read and returned.
 *
 *
 * gradual ramp up
 * ---------------
 * The prefetching is driven by the read_stream API / implementation.
 * There are no explicit fadvise calls in the index code, that all happens
 * in the read stream. The read stream does the usual gradual ramp up to
 * not regress LIMIT 1 queries etc.
 *
 *
 * kill_prior_tuples (TODO/FIXME)
 * ------------------------------
 * If we decide a tuple should be killed, the same scan flag is used to pass
 * this information to indexam.c - the item is recorded in the batch, and the
 * killing is postponed until the batch is freed using amfreebatch(). The scan
 * flag is reset to false, so that the index AM does not get confused and does
 * not do something for a different "current" item.
 *
 *
 * mark/restore (TODO/FIXME)
 * -------------------------
 * With batching, the index AM does not know the the "current" position on
 * the leaf page - we don't propagate this to the index AM while walking
 * items in the batch. To make ammarkpos() work, the index AM can check
 * the current position in the batch, and translate it to the proper page
 * position, using the private information (about items in the batch).
 */

static void
AssertCheckBatchPosValid(IndexScanDesc scan, IndexScanBatchPos *pos)
{
#ifdef USE_ASSERT_CHECKING
	IndexScanBatches *batch = scan->xs_batches;

	/* make sure the position is valid for currently loaded batches */
	if (!((pos->batch >= batch->firstBatch) &&
		   (pos->batch < batch->nextBatch)))
		elog(ERROR, "AssertCheckBatchPosValid");
#endif
}

/*
 * check a single batch is valid
 */
static void
AssertCheckBatch(IndexScanDesc scan, IndexScanBatch batch)
{
#ifdef USE_ASSERT_CHECKING
	Assert(batch->firstItem <= batch->lastItem);
	Assert(batch->items != NULL);
#endif
}

/*
 * Comprehensive check of various invariants on the index batch. Makes sure
 * the indexes are set as expected, the buffer size is within limits, and
 * so on.
 */
static void
AssertCheckBatches(IndexScanDesc scan)
{
#ifdef USE_ASSERT_CHECKING
	IndexScanBatches  *batches = scan->xs_batches;

	/*
	 * Is the capacity sensible?
	 *
	 * FIXME The 128 value is an arbitrary safety limit for development,
	 * but it might be exceeded in some strange cases (with a single index
	 * tuple per leaf page, etc.)
	 *
	 * XXX Maybe we should have a way to limit how far the read stream can
	 * prefetch, because that's what determines how many batches we may
	 * need. Either when creating the read stream (but there doesn't seem
	 * to be an argument for that), or by returning a special "retry"
	 * result (InvalidBlockNumber means 'end of stream').
	 */
	Assert((batches->maxBatches > 0) && (batches->maxBatches < 128));

	/* The index of the first batch should be valid with (numBatches > 0). */
	Assert((batches->firstBatch >= 0) && (batches->firstBatch <= batches->nextBatch));

	/* The number of batches loaded needs to be valid. */
	Assert((batches->nextBatch - batches->firstBatch) <= batches->maxBatches);

	/* FIXME do checks on all loaded batches */
#endif
}

/* debug: print info about current batches */
static void
index_batch_print(const char *label, IndexScanDesc scan)
{
#ifdef INDEXAM_DEBUG
	IndexScanBatches *batches = scan->xs_batches;

	if (!scan->xs_batches)
		return;

	DEBUG_LOG("%s: batches firstBatch %d nextBatch %d",
				  label, batches->firstBatch, batches->nextBatch);

	for (int i = batches->firstBatch; i < batches->nextBatch; i++)
	{
		IndexScanBatchData *batch = INDEX_SCAN_BATCH(scan, i);
		DEBUG_LOG("%s: batch %d %p", label, i, batch);
	}
#endif
}

/*
 * index_batch_pos_advance
 *		Advance the position to the next item, depending on scan direction.
 *
 * XXX We expect to only do this when the advance is possible/valid.
 */
static bool
index_batch_pos_advance(IndexScanDesc scan, IndexScanBatchPos *pos)
{
	IndexScanBatchData *batch;
	ScanDirection	direction = scan->xs_batches->direction;

	/* should know direction by now */
	Assert(direction != NoMovementScanDirection);

	/*
	 * Do we already have the next TID in the currently loaded batches?
	 *
	 * We can only advance if we already have some batches loaded, and
	 * there's either enough items in the current batch, or some more
	 * items in the subsequent batches.
	 *
	 * We either set need_batch=true, or advance the position to the
	 * next item to return.
	 */
	if (scan->xs_batches->firstBatch == scan->xs_batches->nextBatch)
		return false; /* not advanced */

	/*
	 * After loading the first batch, so move to first item (or last, for
	 * backwards scans).
	 */
	if (pos->batch == -1)
	{
		batch = INDEX_SCAN_BATCH(scan, scan->xs_batches->firstBatch);

		pos->batch = 0;
		pos->index = (ScanDirectionIsForward(direction)) ? batch->firstItem : batch->lastItem;

		/* the position has to be valid */
		AssertCheckBatchPosValid(scan, pos);

		return true;
	}

	/* the current position has to be valid */
	AssertCheckBatchPosValid(scan, pos);

	/*
	 * Try advancing to the next item in the current batch, or maybe to the
	 * next batch (if already loaded).
	 */
	batch = INDEX_SCAN_BATCH(scan, pos->batch);
	Assert(batch != NULL);

	/*
	 * Advance to the next item in the same batch, or to the next
	 * batch. If not possible, we need to load a new batch.
	 */
	if (ScanDirectionIsForward(direction))
	{
		if (pos->index < batch->lastItem)
		{
			pos->index++;

			/* the position has to be valid */
			AssertCheckBatchPosValid(scan, pos);

			return true;
		}
	}
	else	/* ScanDirectionIsBackward */
	{
		if (pos->index > batch->firstItem)
		{
			pos->index--;

			/* the position has to be valid */
			AssertCheckBatchPosValid(scan, pos);

			return true;
		}
	}

	/* do we already have a batch loaded? */
	if (pos->batch + 1 < scan->xs_batches->nextBatch)
	{
		pos->batch++;

		batch = INDEX_SCAN_BATCH(scan, pos->batch);
		Assert(batch != NULL);

		pos->index = (ScanDirectionIsForward(direction)) ? batch->firstItem : batch->lastItem;

		/* the position has to be valid */
		AssertCheckBatchPosValid(scan, pos);

		return true;
	}

	/* can't advance */
	return false;
}

/*
 * index_batch_pos_reset
 *		Reset the position to the index, maybe disable update on first advance.
 *
 * Index specifies the item index. The 'restored' flag may be used to disable
 * updating the index on the first advance. This is needed after mark/restore,
 * because we restore to the item we're expected to return next, so we must
 * not skip it.
 *
 * XXX Would be nice to have an assert that the final position is valid,
 * but that requires knowing nheaptids.
 */
static void
index_batch_pos_reset(IndexScanDesc scan, IndexScanBatchPos *pos)
{
	pos->batch = -1;
	pos->index = -1;
}

/*
 * index_scan_stream_read_next
 *		return the next block to pass to the read stream
 *
 * This assumes the "current" scan direction, requested by the caller. If
 * that changes before consuming all buffers, we'll reset the stream and
 * start from scratch. Which may seem inefficient, but it's no worse than
 * what we do now, and it's not a very common case.
 *
 * The scan direction change is checked / handled elsewhere.
 */
static BlockNumber
index_scan_stream_read_next(ReadStream *stream,
							void *callback_private_data,
							void *per_buffer_data)
{
	IndexScanDesc	scan = (IndexScanDesc) callback_private_data;
	IndexScanBatchPos *pos = &scan->xs_batches->streamPos;
	ScanDirection	direction = scan->xs_batches->direction;

	/* we should have set the direction already */
	Assert(direction != NoMovementScanDirection);

	/*
	 * The read position should be valid, because we initialize (advance) it
	 * before maybe attempting to read the heap tuple. And the read position
	 * always lags behind the "read stream" position, so it can't be invalid
	 * yet.
	 */
	AssertCheckBatchPosValid(scan, &scan->xs_batches->readPos);

	/* XXX this loop shouldn't happen more than twice */
	while (true)
	{
		bool	advanced = false;

		/*
		 * Try to advance to the next item, and if there's none in the current
		 * batch then load the next batch.
		 */
		if ((pos->batch == -1) && (pos->index == -1))
		{
			/*
			 * No stream position - most likely all the batches until now were skipped
			 * in an IOS scan, or something like that. In this case just use the read
			 * position as a starting point.
			 */
			*pos = scan->xs_batches->readPos;
			advanced = true;
		}
		else if (index_batch_pos_advance(scan, pos))
		{
			advanced = true;
		}

		if (advanced)
		{
			IndexScanBatch	batch = INDEX_SCAN_BATCH(scan, pos->batch);
			ItemPointer		tid = &batch->items[pos->index].heapTid;

			DEBUG_LOG("index_scan_stream_read_next: index %d TID (%u,%u)",
				 pos->index,
				 ItemPointerGetBlockNumber(tid),
				 ItemPointerGetOffsetNumber(tid));

			/*
			 * if there's a prefetch callback, use it to decide if we will
			 * need to read the block
			 */
			if (scan->xs_batches->prefetchCallback &&
				!scan->xs_batches->prefetchCallback(scan, scan->xs_batches->prefetchArgument, pos))
			{
				DEBUG_LOG("index_scan_stream_read_next: skip block (callback)");
				continue;
			}

			/* same block as before, don't need to read it */
			if (scan->xs_batches->lastBlock == ItemPointerGetBlockNumber(tid))
			{
				DEBUG_LOG("index_scan_stream_read_next: skip block (lastBlock)");
				continue;
			}

			scan->xs_batches->lastBlock = ItemPointerGetBlockNumber(tid);

			return ItemPointerGetBlockNumber(tid);
		}

		/*
		 * advanced=false: There are no more items in the current batch, or maybe
		 * we don't have any batches yet (if is the first time through).
		 */

		/* try loading the next batch */
		if (index_batch_getnext(scan))
			continue;

		/* can't load batch, we're done with this scan */
		break;
	}

	return InvalidBlockNumber;
}

/* ----------------
 *		index_batch_getnext - get the next batch of TIDs from a scan
 *
 * Returns true if we managed to read at least some TIDs into the batch,
 * or false if there are no more TIDs in the scan. The xs_heaptids and
 * xs_nheaptids fields contain the TIDS and the number of elements.
 *
 * XXX This only loads the TIDs and resets the various batch fields to
 * fresh state. It does not set xs_heaptid/xs_itup/xs_hitup, that's the
 * responsibility of the following index_batch_getnext_tid() calls.
 * ----------------
 */
static bool
index_batch_getnext(IndexScanDesc scan)
{
	IndexScanBatchData *batch;
	ItemPointerData		tid;
	ScanDirection		direction = scan->xs_batches->direction;
	IndexTuple			itup;

	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(amgetbatch);

	/* XXX: we should assert that a snapshot is pushed or registered */
	Assert(TransactionIdIsValid(RecentXmin));

	/*
	 * FIXME don't overflow the array, should resize the array instead
	 * or release batches that are no longer needed.
	 */
	if (!((scan->xs_batches->nextBatch - scan->xs_batches->firstBatch) < scan->xs_batches->maxBatches))
		elog(ERROR, "index_batch_getnext");

	/*
	 * Did we already read the last batch for this scan? We may read the
	 * batches in two places, so we need to remember that, otherwise the
	 * retry restarts the scan.
	 */
	if (scan->xs_batches->finished)
		return NULL;

	index_batch_print("index_batch_getnext / start", scan);

	/*
	 * FIXME btgetbatch calls _bt_returnitem, which however sets xs_heaptid,
	 * and so would interfere with index scans (because this may get executed
	 * from the read_stream_next_buffer callback during the scan (fetching
	 * heap tuples in heapam_index_fetch_tuple). Ultimately we should not do
	 * _bt_returnitem at all, just functions like _bt_steppage etc. while
	 * loading the next batch.
	 */
	tid = scan->xs_heaptid;
	itup = scan->xs_itup;

	batch = scan->indexRelation->rd_indam->amgetbatch(scan, direction);
	if (batch != NULL)
	{
		/* position of the newly loaded batch */
		int	batchIndex = scan->xs_batches->nextBatch;

		INDEX_SCAN_BATCH(scan, batchIndex) = batch;

		scan->xs_batches->nextBatch++;

		/*
		 * XXX Also remember this batch as a starting point for the next
		 * time we do amgetbatch() so that we restore BTScanOpaque into the
		 * right state.
		 *
		 * XXX This is an unholy mash of pointers and "positions", with
		 * readPos and streamPos and now also currentBatch. Needs to get
		 * cleanup up somehow.
		 */
		scan->xs_batches->currentBatch = batch;

		DEBUG_LOG("index_batch_getnext firstBatch %d nextBatch %d batch %p",
					  scan->xs_batches->firstBatch, scan->xs_batches->nextBatch, batch);
	}
	else
		scan->xs_batches->finished = true;

	/* XXX see FIXME above */
	scan->xs_heaptid = tid;
	scan->xs_itup = itup;

	AssertCheckBatches(scan);

	index_batch_print("index_batch_getnext / end", scan);

	return (batch != NULL);
}

/* ----------------
 *		index_getnext_batch_tid - get the next TID from the current batch
 *
 * Same calling convention as index_getnext_tid(), except that NULL means
 * no more items in the current batch, there may be more batches.
 *
 * XXX This only sets xs_heaptid and xs_itup (if requested). Not sure if
 * we need to do something with xs_hitup.
 *
 * FIXME Should this set xs_hitup?
 * ----------------
 */
static ItemPointer
index_batch_getnext_tid(IndexScanDesc scan, ScanDirection direction)
{
	IndexScanBatchPos *pos = &scan->xs_batches->readPos;

	/* shouldn't get here without batching */
	Assert(scan->xs_batches != NULL);

	/* FIXME handle change of scan direction (reset stream, ...) */
	scan->xs_batches->direction = direction;

	DEBUG_LOG("index_batch_getnext_tid pos %d %d direction %d",
				  pos->batch, pos->index, direction);

	/* XXX this loop shouldn't happen more than twice */
	/* FIXME maybe have some protection against infinite loops? */
	while (true)
	{
		/* try to load batch, or return the next item
		 *
		 * FIXME If we andvance read position to the next batch, we should
		 * release the previous batch. Or maybe we could keep it in case the
		 * scan direction changes, but not more than maxBatches?
		 */
		if (index_batch_pos_advance(scan, pos))
		{
			IndexScanBatchData *batch = INDEX_SCAN_BATCH(scan, pos->batch);
			scan->xs_heaptid = batch->items[pos->index].heapTid;

			/* also update the index tuple  */
			scan->xs_itup = (IndexTuple) (batch->currTuples + batch->items[pos->index].tupleOffset);

			DEBUG_LOG("pos batch %p first %d last %d pos %d/%d TID (%u,%u)",
						  batch, batch->firstItem, batch->lastItem,
						  pos->batch, pos->index,
						  ItemPointerGetBlockNumber(&scan->xs_heaptid),
						  ItemPointerGetOffsetNumber(&scan->xs_heaptid));

			/*
			 * If we advanced to the next batch, release the batch we no
			 * longer need. The positions is the "read" position, and we
			 * can compare it to firstBatch.
			 *
			 * XXX Maybe we should do that in index_batch_getnext instead,
			 * and only when needed / under memory pressure?
			 */
			if (pos->batch != scan->xs_batches->firstBatch)
			{
				batch = INDEX_SCAN_BATCH(scan, scan->xs_batches->firstBatch);

				DEBUG_LOG("index_batch_getnext_tid free batch %p firstBatch %d nextBatch %d",
							  batch,
							  scan->xs_batches->firstBatch,
							  scan->xs_batches->nextBatch);

				/*
				 * FIXME needs to be careful about markPos - we should not free
				 * a batch markPos points to (just like nbtree). Maybe we can
				 * keep the batch aside too?
				 */
				index_batch_free(scan, batch);
				scan->xs_batches->firstBatch++;

				DEBUG_LOG("index_batch_getnext_tid batch freed firstBatch %d nextBatch %d",
							  scan->xs_batches->firstBatch,
							  scan->xs_batches->nextBatch);

				index_batch_print("index_batch_getnext_tid / free old batch", scan);

				/* we can't skip any batches */
				Assert(scan->xs_batches->firstBatch == pos->batch);
			}

			return &scan->xs_heaptid;
		}

		/* try loading the next batch */
		/* FIXME why do we even call index_batch_getnext here? surely that should
		 * only be done from index_scan_stream_read_next? */
		if (index_batch_getnext(scan))
		{
			DEBUG_LOG("loaded next batch");
			continue;
		}

		DEBUG_LOG("no more batches to process");

		/* can't load batch, we're done with this scan */
		return NULL;
	}

	/*
	 * If we get here, it's a sign something is really wrong. The only
	 * explanation would be that we keep loading batches that are however
	 * empty, and that's illegal.
	 */
	elog(ERROR, "failed to find item in batch or load the next batch");

	return NULL;
}

/*
 * index_batch_init
 *		Initialize various fields / arrays needed by batching.
 *
 * FIXME This is a bit ad-hoc hodge podge, due to how I was adding more and
 * more pieces. Some of the fields may be not quite necessary, needs cleanup.
 */
static void
index_batch_init(IndexScanDesc scan)
{
	/* init batching info, but only if batch supported */
	Assert(scan->indexRelation->rd_indam->amgetbatch != NULL);
	Assert(scan->indexRelation->rd_indam->amfreebatch != NULL);

	scan->xs_batches = palloc0(sizeof(IndexScanBatches));

	/* XXX we don't know direction of the scan yet */
	scan->xs_batches->direction = NoMovementScanDirection;

	/*
	 * XXX What's the maximum number of batches we might need? Presumably
	 * it's related to effective_io_concurrency - the extreme case would
	 * be one tuple per index page, which could need many batches (if we
	 * keep one batch per index leaf page - maybe we shouldn't?). For most
	 * indexes we'll need far fewer pages/batches, but we may need to
	 * resize the queue accordingly.
	 */
	scan->xs_batches->maxBatches = 16;	/* size of the batches array */
	scan->xs_batches->firstBatch = 0;	/* first batch */
	scan->xs_batches->nextBatch = 0;	/* first batch is empty */

	/* positions in the queue of batches */
	index_batch_pos_reset(scan, &scan->xs_batches->readPos);
	index_batch_pos_reset(scan, &scan->xs_batches->streamPos);
	index_batch_pos_reset(scan, &scan->xs_batches->markPos);

	scan->xs_batches->currentBatch = NULL;
	scan->xs_batches->lastBlock = InvalidBlockNumber;

	scan->xs_batches->batches = palloc(sizeof(IndexScanBatchData *) * scan->xs_batches->maxBatches);
}

/*
 * index_batch_reset
 *		Reset the batch before reading the next chunk of data.
 *
 * FIXME Another bit in need of cleanup. The currIndex default (-1) is not quite
 * correct, because for backwards scans is wrong.
 *
 * complete - true means we reset even marked batch
 *
 * XXX Isn't this the same as index_batch_empty?
 * XXX Should this reset the batch memory context, xs_itup, xs_hitup, etc?
 */
static void
index_batch_reset(IndexScanDesc scan, bool complete)
{
	IndexScanBatches *batches = scan->xs_batches;

	/* bail out if batching not enabled */
	if (!batches)
		return;

	index_batch_print("index_batch_reset", scan);

	/* With batching enabled, we should have a read stream. Reset it. */
	Assert(scan->xs_heapfetch);
	read_stream_reset(scan->xs_heapfetch->rs);

	/*
	 * With "complete" reset, make sure to also free the marked batch,
	 * either by just forgetting it (if it's still in the queue), or by
	 * explicitly freeing it.
	 *
	 * XXX Do this before the loop, so that it calls the amfreebatch().
	 */
	if (complete && (batches->markBatch != NULL))
	{
		IndexScanBatchPos *pos = &batches->markPos;
		IndexScanBatch	batch = batches->markBatch;

		/* always reset */
		batches->markBatch = NULL;

		/* we've already moved past that batch, so free it directly */
		if (pos->batch < batches->firstBatch || pos->batch >= batches->nextBatch)
		{
			index_batch_free(scan, batch);
		}
	}

	/* release all currently loaded batches */
	for (int i = batches->firstBatch; i < batches->nextBatch; i++)
	{
		IndexScanBatch	batch = INDEX_SCAN_BATCH(scan, i);

		DEBUG_LOG("freeing batch %d %p", i, batch);

		index_batch_free(scan, batch);
	}

	/* reset relevant IndexScanBatches fields */
	batches->maxBatches = 16;	/* size of the batches array */
	batches->firstBatch = 0;	/* first batch */
	batches->nextBatch = 0;		/* first batch is empty */

	index_batch_pos_reset(scan, &batches->readPos);
	index_batch_pos_reset(scan, &batches->streamPos);

	/*
	 * For a complete reset we've discarded the mark batch above, so forget
	 * the mark position too.
	 */
	if (complete)
		index_batch_pos_reset(scan, &batches->markPos);

	batches->finished = false;

	batches->currentBatch = NULL;
	batches->lastBlock = InvalidBlockNumber;

	/* XXX this will result in some batches not being freed, we need to
	 * have a flag that says whether to reset marked pos/batch too (for
	 * now we remember/restore them in restrpos) */

	AssertCheckBatches(scan);
}

static void
index_batch_kill_item(IndexScanDesc scan)
{
	IndexScanBatchPos *pos = &scan->xs_batches->readPos;
	IndexScanBatchData *batch = INDEX_SCAN_BATCH(scan, pos->batch);

	/* FIXME mark item at current readPos as deleted */
	AssertCheckBatchPosValid(scan, pos);

	/*
	 * XXX Too tied to btree (through MaxTIDsPerBTreePage), we should make
	 * this AM agnostic. We could maybe even replace this with Bitmapset.
	 * It might be more expensive if we only kill items at the end of the
	 * page (in which case we still have to walk the first part to find the
	 * bits at the end). But given the lower memory usage it still sees
	 * like a good tradeoff overall.
	 */
	if (batch->killedItems == NULL)
		batch->killedItems = (int *)
			palloc(MaxTIDsPerBTreePage * sizeof(int));
	if (batch->numKilled < MaxTIDsPerBTreePage)
			batch->killedItems[batch->numKilled++] = pos->index;

	// elog(WARNING, "index_batch_kill_item (%d,%d)", pos->batch, pos->index);
	// FIXME index_batch_kill_item not implemented
}

static void
index_batch_free(IndexScanDesc scan, IndexScanBatch batch)
{
	SCAN_CHECKS;
	CHECK_SCAN_PROCEDURE(amfreebatch);

	AssertCheckBatch(scan, batch);

	/* don't free the batch that is marked */
	if (batch == scan->xs_batches->markBatch)
		return;

	scan->indexRelation->rd_indam->amfreebatch(scan, batch);
}

/* */
static void
index_batch_end(IndexScanDesc scan)
{
	index_batch_reset(scan, true);
}

IndexScanBatch
index_batch_alloc(int maxitems, bool want_itup)
{
	IndexScanBatch	batch = palloc(sizeof(IndexScanBatchData));

	batch->firstItem = -1;
	batch->lastItem = -1;
	batch->itemIndex = -1;

	batch->killedItems = NULL;	/* FIXME allocate an array, actually */
	batch->numKilled = 0;		/* nothing killed yet */

	/*
	 * If we are doing an index-only scan, these are the tuple storage
	 * workspaces for the currPos and markPos respectively.  Each is of size
	 * BLCKSZ, so it can hold as much as a full page's worth of tuples.
	 *
	 * XXX allocate
	 */
	batch->currTuples = NULL;			/* tuple storage for currPos */
	if (want_itup)
		batch->currTuples = palloc(BLCKSZ);

	/*
	 * XXX Maybe don't size to MaxTIDsPerBTreePage? We don't reuse batches
	 * (unlike currPos), so we can size it for just what we need.
	 */
	batch->items = palloc0(sizeof(IndexScanBatchPosItem) * maxitems);

	/*
	 * batch contents (TIDs, index tuples, kill bitmap, ...)
	 *
	 * XXX allocate as needed?
	 */
	batch->itups = NULL;			/* IndexTuples, if requested */
	batch->htups = NULL;			/* HeapTuples, if requested */
	batch->recheck = NULL;			/* recheck flags */
	batch->privateData = NULL;		/* private data for batch */

	/* xs_orderbyvals / xs_orderbynulls */
	batch->orderbyvals = NULL;
	batch->orderbynulls = NULL;

	/* AM-specific per-batch state */
	batch->opaque = NULL;

	return batch;
}
