/*-------------------------------------------------------------------------
 *
 * relscan.h
 *	  POSTGRES relation scan descriptor definitions.
 *
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/include/access/relscan.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef RELSCAN_H
#define RELSCAN_H

#include "access/htup_details.h"
#include "access/itup.h"
#include "access/sdir.h"
#include "nodes/tidbitmap.h"
#include "port/atomics.h"
#include "storage/buf.h"
#include "storage/relfilelocator.h"
#include "storage/spin.h"
#include "utils/relcache.h"


struct ParallelTableScanDescData;

/*
 * Generic descriptor for table scans. This is the base-class for table scans,
 * which needs to be embedded in the scans of individual AMs.
 */
typedef struct TableScanDescData
{
	/* scan parameters */
	Relation	rs_rd;			/* heap relation descriptor */
	struct SnapshotData *rs_snapshot;	/* snapshot to see */
	int			rs_nkeys;		/* number of scan keys */
	struct ScanKeyData *rs_key; /* array of scan key descriptors */

	/*
	 * Scan type-specific members
	 */
	union
	{
		/* Iterator for Bitmap Table Scans */
		TBMIterator rs_tbmiterator;

		/*
		 * Range of ItemPointers for table_scan_getnextslot_tidrange() to
		 * scan.
		 */
		struct
		{
			ItemPointerData rs_mintid;
			ItemPointerData rs_maxtid;
		}			tidrange;
	}			st;

	/*
	 * Information about type and behaviour of the scan, a bitmask of members
	 * of the ScanOptions enum (see tableam.h).
	 */
	uint32		rs_flags;

	struct ParallelTableScanDescData *rs_parallel;	/* parallel scan
													 * information */
} TableScanDescData;
typedef struct TableScanDescData *TableScanDesc;

/*
 * Shared state for parallel table scan.
 *
 * Each backend participating in a parallel table scan has its own
 * TableScanDesc in backend-private memory, and those objects all contain a
 * pointer to this structure.  The information here must be sufficient to
 * properly initialize each new TableScanDesc as workers join the scan, and it
 * must act as a information what to scan for those workers.
 */
typedef struct ParallelTableScanDescData
{
	RelFileLocator phs_locator; /* physical relation to scan */
	bool		phs_syncscan;	/* report location to syncscan logic? */
	bool		phs_snapshot_any;	/* SnapshotAny, not phs_snapshot_data? */
	Size		phs_snapshot_off;	/* data for snapshot */
} ParallelTableScanDescData;
typedef struct ParallelTableScanDescData *ParallelTableScanDesc;

/*
 * Shared state for parallel table scans, for block oriented storage.
 */
typedef struct ParallelBlockTableScanDescData
{
	ParallelTableScanDescData base;

	BlockNumber phs_nblocks;	/* # blocks in relation at start of scan */
	slock_t		phs_mutex;		/* mutual exclusion for setting startblock */
	BlockNumber phs_startblock; /* starting block number */
	BlockNumber phs_numblock;	/* # blocks to scan, or InvalidBlockNumber if
								 * no limit */
	pg_atomic_uint64 phs_nallocated;	/* number of blocks allocated to
										 * workers so far. */
}			ParallelBlockTableScanDescData;
typedef struct ParallelBlockTableScanDescData *ParallelBlockTableScanDesc;

/*
 * Per backend state for parallel table scan, for block-oriented storage.
 */
typedef struct ParallelBlockTableScanWorkerData
{
	uint64		phsw_nallocated;	/* Current # of blocks into the scan */
	uint32		phsw_chunk_remaining;	/* # blocks left in this chunk */
	uint32		phsw_chunk_size;	/* The number of blocks to allocate in
									 * each I/O chunk for the scan */
} ParallelBlockTableScanWorkerData;
typedef struct ParallelBlockTableScanWorkerData *ParallelBlockTableScanWorker;

/*
 * Base class for fetches from a table via an index. This is the base-class
 * for such scans, which needs to be embedded in the respective struct for
 * individual AMs.
 */
typedef struct IndexFetchTableData
{
	Relation	rel;
} IndexFetchTableData;

/*
 * Queue-wise location of a BatchMatchingItem that appears in a BatchIndexScan
 * returned by (and subsequently passed to) an amgetbatch routine
 */
typedef struct BatchQueueItemPos
{
	/* BatchQueue.batches[]-wise index to relevant BatchIndexScan */
	int			batch;

	/* BatchIndexScan.items[]-wise index to relevant BatchMatchingItem */
	int			item;
} BatchQueueItemPos;

static inline void
batch_reset_pos(BatchQueueItemPos *pos)
{
	pos->batch = -1;
	pos->item = -1;
}

/*
 * Matching item returned by amgetbatch (in returned BatchIndexScan) during an
 * index scan.  Used by table AM to locate relevant matching table tuple.
 */
typedef struct BatchMatchingItem
{
	ItemPointerData heapTid;	/* TID of referenced heap item */
	OffsetNumber indexOffset;	/* index item's location within page */
	LocationIndex tupleOffset;	/* IndexTuple's offset in workspace, if any */
} BatchMatchingItem;

/*
 * Data about one batch of items returned by (and passed to) amgetbatch during
 * index scans
 */
typedef struct BatchIndexScanData
{
	/*
	 * Information output by amgetbatch index AMs upon returning a batch with
	 * one or more matching items, describing details of the index page where
	 * matches were located.
	 *
	 * Used in the next amgetbatch call to determine which index page to read
	 * next (or to determine if there's no further matches in current scan
	 * direction).
	 */
	BlockNumber currPage;		/* Index page with matching items */
	BlockNumber prevPage;		/* currPage's left link */
	BlockNumber nextPage;		/* currPage's right link */

	Buffer		buf;			/* currPage buf (invalid means unpinned) */
	XLogRecPtr	lsn;			/* currPage's LSN (when dropPin) */

	/* scan direction when the index page was read */
	ScanDirection dir;

	/*
	 * moreLeft and moreRight track whether we think there may be matching
	 * index entries to the left and right of the current page, respectively
	 */
	bool		moreLeft;
	bool		moreRight;

	/*
	 * The items array is always ordered in index order (ie, increasing
	 * indexoffset).  When scanning backwards it is convenient to fill the
	 * array back-to-front, so we start at the last slot and fill downwards.
	 * Hence we need both a first-valid-entry and a last-valid-entry counter.
	 */
	int			firstItem;		/* first valid index in items[] */
	int			lastItem;		/* last valid index in items[] */

	/* info about killed items if any (killedItems is NULL if never used) */
	int		   *killedItems;	/* indexes of killed items */
	int			numKilled;		/* number of currently stored items */

	/*
	 * Matching items state for this batch.
	 *
	 * If we are doing an index-only scan, these are the tuple storage
	 * workspaces for the matching tuples (tuples referenced by items[]). Each
	 * is of size BLCKSZ, so it can hold as much as a full page's worth of
	 * tuples.
	 */
	char	   *currTuples;		/* tuple storage for items[] */
	BatchMatchingItem items[FLEXIBLE_ARRAY_MEMBER];
} BatchIndexScanData;

typedef struct BatchIndexScanData *BatchIndexScan;

/*
 * Maximum number of batches (leaf pages) we can keep in memory.  We need a
 * minimum of two, since we'll only consider releasing one batch when another
 * is read.
 */
#define INDEX_SCAN_MAX_BATCHES		2
#define INDEX_SCAN_CACHE_BATCHES	2
#define INDEX_SCAN_BATCH_COUNT(scan) \
	((scan)->batchqueue->nextBatch - (scan)->batchqueue->headBatch)

/* Did we already load batch with the requested index? */
#define INDEX_SCAN_BATCH_LOADED(scan, idx) \
	((idx) >= 0 && (idx) < (scan)->batchqueue->nextBatch)

/* Have we loaded the maximum number of batches? */
#define INDEX_SCAN_BATCH_FULL(scan) \
	(INDEX_SCAN_BATCH_COUNT(scan) == INDEX_SCAN_MAX_BATCHES)

/* Return batch for the provided index. */
#define INDEX_SCAN_BATCH(scan, idx)	\
		((scan)->batchqueue->batches[(idx) % INDEX_SCAN_MAX_BATCHES])

/* Is the position invalid/undefined? */
#define INDEX_SCAN_POS_INVALID(pos) \
		(((pos)->batch == -1) && ((pos)->item == -1))

#ifdef INDEXAM_DEBUG
#define DEBUG_LOG(...) elog(AmRegularBackendProcess() ? NOTICE : DEBUG2, __VA_ARGS__)
#else
#define DEBUG_LOG(...)
#endif

/*
 * State used by table AMs to manage an index scan that uses the amgetbatch
 * interface.  Scans work with a queue of batches returned by amgetbatch.
 *
 * Batches are kept in the order that they were returned in by amgetbatch,
 * since that is the same order that table_index_getnext_slot will return
 * matches in.  However, table AMs are free to fetch table tuples in whatever
 * order is most convenient/efficient -- provided that such reordering cannot
 * affect the order that table_index_getnext_slot later returns tuples in.
 *
 * This data structure also provides table AMs with a way to read ahead of the
 * current read position by _multiple_ batches/index pages.  The further out
 * the table AM reads ahead like this, the further it can see into the future.
 * That way the table AM is able to reorder work as aggressively as desired.
 * For example, index scans sometimes need to readahead by as many as a few
 * dozen amgetbatch batches in order to maintain an optimal I/O prefetch
 * distance (distance for reading table blocks/fetching table tuples).
 */
typedef struct BatchQueue
{
	/* Current scan direction, for the currently loaded batches */
	ScanDirection direction;

	/* current positions in batches[] for scan */
	BatchQueueItemPos readPos;	/* read position */
	BatchQueueItemPos markPos;	/* mark/restore position */

	BatchIndexScan markBatch;

	/*
	 * Array of batches returned by the AM. The array has a capacity (but can
	 * be resized if needed). The headBatch is an index of the batch we're
	 * currently reading from (this needs to be translated by modulo
	 * INDEX_SCAN_MAX_BATCHES into index in the batches array).
	 */
	int			headBatch;		/* head batch slot */
	int			nextBatch;		/* next empty batch slot */

	/* Array of pointers to cached recyclable batches */
	BatchIndexScan cache[INDEX_SCAN_CACHE_BATCHES];

	/* Array of pointers to queued batches */
	BatchIndexScan batches[INDEX_SCAN_MAX_BATCHES];

} BatchQueue;

struct IndexScanInstrumentation;

/*
 * We use the same IndexScanDescData structure for both amgettuple-based
 * and amgetbitmap-based index scans.  Some fields are only relevant in
 * amgettuple-based scans.
 */
typedef struct IndexScanDescData
{
	/* scan parameters */
	Relation	heapRelation;	/* heap relation descriptor, or NULL */
	Relation	indexRelation;	/* index relation descriptor */
	struct SnapshotData *xs_snapshot;	/* snapshot to see */
	int			numberOfKeys;	/* number of index qualifier conditions */
	int			numberOfOrderBys;	/* number of ordering operators */
	struct ScanKeyData *keyData;	/* array of index qualifier descriptors */
	struct ScanKeyData *orderByData;	/* array of ordering op descriptors */

	/* index access method's private state */
	void	   *opaque;			/* access-method-specific info */

	/* table access method's private amgetbatch state */
	BatchQueue *batchqueue;		/* amgetbatch related state */

	bool		xs_want_itup;	/* caller requests index tuples */
	bool		xs_temp_snap;	/* unregister snapshot at scan end? */

	/* signaling to index AM about killing index tuples */
	bool		kill_prior_tuple;	/* last-returned tuple is dead */
	bool		ignore_killed_tuples;	/* do not return killed entries */
	bool		xactStartedInRecovery;	/* prevents killing/seeing killed
										 * tuples */
	/* amgetbatch can safely drop pins on returned batch's index page? */
	bool		dropPin;

	/*
	 * Did we read the final batch in this scan direction?
	 */
	bool		finished;

	/*
	 * Instrumentation counters maintained by all index AMs during both
	 * amgettuple calls and amgetbitmap calls (unless field remains NULL)
	 */
	struct IndexScanInstrumentation *instrument;

	/*
	 * In an index-only scan, a successful amgettuple call must fill either
	 * xs_itup (and xs_itupdesc) or xs_hitup (and xs_hitupdesc) to provide the
	 * data returned by the scan.  It can fill both, in which case the heap
	 * format will be used.
	 */
	IndexTuple	xs_itup;		/* index tuple returned by AM */
	struct TupleDescData *xs_itupdesc;	/* rowtype descriptor of xs_itup */
	HeapTuple	xs_hitup;		/* index data returned by AM, as HeapTuple */
	struct TupleDescData *xs_hitupdesc; /* rowtype descriptor of xs_hitup */

	ItemPointerData xs_heaptid; /* result */
	bool		xs_heap_continue;	/* T if must keep walking, potential
									 * further results */
	IndexFetchTableData *xs_heapfetch;

	bool		xs_recheck;		/* T means scan keys must be rechecked */
	uint16		maxitemsbatch;	/* set by ambeginscan when amgetbatch used */

	/*
	 * When fetching with an ordering operator, the values of the ORDER BY
	 * expressions of the last returned tuple, according to the index.  If
	 * xs_recheckorderby is true, these need to be rechecked just like the
	 * scan keys, and the values returned here are a lower-bound on the actual
	 * values.
	 */
	Datum	   *xs_orderbyvals;
	bool	   *xs_orderbynulls;
	bool		xs_recheckorderby;

	/* parallel index scan information, in shared memory */
	struct ParallelIndexScanDescData *parallel_scan;
} IndexScanDescData;

/* Generic structure for parallel scans */
typedef struct ParallelIndexScanDescData
{
	RelFileLocator ps_locator;	/* physical table relation to scan */
	RelFileLocator ps_indexlocator; /* physical index relation to scan */
	Size		ps_offset_ins;	/* Offset to SharedIndexScanInstrumentation */
	Size		ps_offset_am;	/* Offset to am-specific structure */
	char		ps_snapshot_data[FLEXIBLE_ARRAY_MEMBER];
}			ParallelIndexScanDescData;

struct TupleTableSlot;

/* Struct for storage-or-index scans of system tables */
typedef struct SysScanDescData
{
	Relation	heap_rel;		/* catalog being scanned */
	Relation	irel;			/* NULL if doing heap scan */
	struct TableScanDescData *scan; /* only valid in storage-scan case */
	struct IndexScanDescData *iscan;	/* only valid in index-scan case */
	struct SnapshotData *snapshot;	/* snapshot to unregister at end of scan */
	struct TupleTableSlot *slot;
} SysScanDescData;

/*
 * Check that a position (batch,item) is valid with respect to the batches we
 * have currently loaded.
 */
static inline void
batch_assert_pos_valid(IndexScanDescData *scan, BatchQueueItemPos *pos)
{
#ifdef USE_ASSERT_CHECKING
	BatchQueue *batchqueue = scan->batchqueue;

	/* make sure the position is valid for currently loaded batches */
	Assert(pos->batch >= batchqueue->headBatch);
	Assert(pos->batch < batchqueue->nextBatch);
#endif
}

/*
 * Check a single batch is valid.
 */
static inline void
batch_assert_batch_valid(IndexScanDescData *scan, BatchIndexScan batch)
{
	/* batch must have one or more matching items returned by index AM */
	Assert(batch->firstItem >= 0 && batch->firstItem <= batch->lastItem);
	Assert(batch->items != NULL);

	/*
	 * The number of killed items must be valid, and there must be an array of
	 * indexes if there are items.
	 */
	Assert(batch->numKilled >= 0);
	Assert(!(batch->numKilled > 0 && batch->killedItems == NULL));
}

static inline void
batch_assert_batches_valid(IndexScanDescData  *scan)
{
#ifdef USE_ASSERT_CHECKING
	BatchQueue *batchqueue = scan->batchqueue;

	/* we should have batches initialized */
	Assert(batchqueue != NULL);

	/* The head/next indexes should define a valid range */
	Assert(batchqueue->headBatch >= 0 &&
		   batchqueue->headBatch <= batchqueue->nextBatch);

	/* Check all current batches */
	for (int i = batchqueue->headBatch; i < batchqueue->nextBatch; i++)
	{
		BatchIndexScan batch = INDEX_SCAN_BATCH(scan, i);

		batch_assert_batch_valid(scan, batch);
	}
#endif
}

#endif							/* RELSCAN_H */
