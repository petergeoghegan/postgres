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
#include "storage/read_stream.h"
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
	ReadStream *rs;
} IndexFetchTableData;

/*
 * Location of a BatchMatchingItem that appears in a IndexScanBatch returned
 * by (and subsequently passed to) an amgetbatch routine
 */
typedef struct BatchRingItemPos
{
	/* BatchRingBuffer.batches[]-wise index to relevant IndexScanBatch */
	uint8		batch;

	/* IndexScanBatch.items[]-wise index to relevant BatchMatchingItem */
	int			item;
} BatchRingItemPos;

static inline void
batch_reset_pos(BatchRingItemPos *pos)
{
	/*
	 * Set item to -1, indicating that the position is invalid.
	 *
	 * Set batch to max value so that incrementing in index_batchpos_newbatch
	 * wraps around to 0.
	 */
	pos->item = -1;
	pos->batch = PG_UINT8_MAX;
}

/*
 * Matching item returned by amgetbatch (in returned IndexScanBatch) during an
 * index scan.  Used by table AM to locate relevant matching table tuple.
 */
typedef struct BatchMatchingItem
{
	ItemPointerData heapTid;	/* TID of referenced heap item */
	OffsetNumber indexOffset;	/* index item's location within page */
	LocationIndex tupleOffset;	/* IndexTuple's offset in workspace, if any */
	bool		allVisible;		/* TID points to all-visible page */
} BatchMatchingItem;

/*
 * Data about one batch of items returned by (and passed to) amgetbatch during
 * index scans
 */
typedef struct IndexScanBatchData
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
	XLogRecPtr	lsn;			/* currPage's LSN */

	/* scan direction when the index page was read */
	ScanDirection dir;

	/*
	 * moreLeft and moreRight are used by index AMs to track whether there may
	 * be matching index entries to the left and right currPage, respectively.
	 *
	 * Note: the exact interpretation of these fields varies across index AMs.
	 * Table AMs must not rely on them directly; they must always call index
	 * AM's amgetbatch routine to determine if there's no more batches in the
	 * current scan direction.
	 */
	bool		moreLeft;
	bool		moreRight;

	/*
	 * Matching items state for this batch.  Output by index AM for table AM.
	 *
	 * The items array is always ordered in index order (ie, increasing
	 * indexoffset).  When scanning backwards it is convenient for index AMs
	 * to fill the array back-to-front, so we start at the last slot and fill
	 * downwards.  Hence they need a first-valid-entry and a last-valid-entry
	 * counter.
	 */
	int			firstItem;		/* first valid index in items[] */
	int			lastItem;		/* last valid index in items[] */

	/* info about killed items if any (killedItems is NULL if never used) */
	int		   *killedItems;	/* indexes of killed items */
	int			numKilled;		/* number of currently stored items */

	/*
	 * If we are doing an index-only scan, these are the tuple storage
	 * workspaces for the matching tuples (tuples referenced by items[]). Each
	 * is of size BLCKSZ, so it can hold as much as a full page's worth of
	 * tuples.
	 */
	char	   *currTuples;		/* tuple storage for items[] */
	BatchMatchingItem items[FLEXIBLE_ARRAY_MEMBER]; /* matching items */
} IndexScanBatchData;

typedef struct IndexScanBatchData *IndexScanBatch;

/*
 * Maximum number of batches (leaf pages) we can keep in memory.  We need a
 * minimum of two, since we'll only consider releasing one batch when another
 * is read.
 *
 * The choice of 64 batches is arbitrary.  It's about 1MB of data with 8KB
 * pages (512kB for pages, and then a bit of overhead). We should not really
 * need this many batches in most cases, though. The read stream looks ahead
 * just enough to queue enough IOs, adjusting the distance (TIDs, but
 * ultimately the number of future batches) to meet that.
 */
#define INDEX_SCAN_MAX_BATCHES		64
#define INDEX_SCAN_CACHE_BATCHES	2
#define INDEX_SCAN_BATCH_COUNT(scan) \
	((uint8) ((scan)->batchringbuf.nextBatch - (scan)->batchringbuf.headBatch))

/* Did we already load batch with the requested index? */
#define INDEX_SCAN_BATCH_LOADED(scan, idx) \
	((int8) ((idx) - (scan)->batchringbuf.headBatch) >= 0 && \
	 (int8) ((idx) - (scan)->batchringbuf.nextBatch) < 0)

/* Have we loaded the maximum number of batches? */
#define INDEX_SCAN_BATCH_FULL(scan) \
	(INDEX_SCAN_BATCH_COUNT(scan) == INDEX_SCAN_MAX_BATCHES)

/* Return batch for the provided index */
#define INDEX_SCAN_BATCH(scan, idx)	\
( \
	AssertMacro(INDEX_SCAN_BATCH_LOADED(scan, idx)), \
	((scan)->batchringbuf.batches[(idx) & (INDEX_SCAN_MAX_BATCHES - 1)]) \
)

/* Append given batch to scan's batch ring buffer */
#define INDEX_SCAN_BATCH_APPEND(scan, batch) \
	do { \
		BatchRingBuffer *mringbuf = &(scan)->batchringbuf;	\
		uint8			nextBatch = mringbuf->nextBatch; \
		mringbuf->batches[nextBatch & (INDEX_SCAN_MAX_BATCHES - 1)] = (batch); \
		mringbuf->nextBatch++; \
	} while(0)

/* Is the position invalid/undefined? */
#define INDEX_SCAN_POS_INVALID(pos) ((pos)->item == -1)

/*
 * State used by table AMs to manage an index scan that uses the amgetbatch
 * interface.  Scans use a ring buffer of batches returned by amgetbatch.
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
typedef struct BatchRingBuffer
{
	/* Current scan direction, for the currently loaded batches */
	ScanDirection direction;

	/* current positions in batches[] for scan */
	BatchRingItemPos scanPos;	/* scan's read position */
	BatchRingItemPos markPos;	/* mark/restore position */
	BatchRingItemPos prefetchPos;	/* prefetching position */

	IndexScanBatch markBatch;

	/*
	 * Array of batches returned by the AM. The array has a capacity (but can
	 * be resized if needed). The headBatch is an index of the batch we're
	 * currently reading from (this needs to be translated by modulo
	 * INDEX_SCAN_MAX_BATCHES into index in the batches array).
	 */
	uint8		headBatch;		/* head batch slot */
	uint8		nextBatch;		/* next empty batch slot */

	/* Array of pointers to cached recyclable batches */
	IndexScanBatch cache[INDEX_SCAN_CACHE_BATCHES];

	/* Array of pointers to ring buffer batches */
	IndexScanBatch batches[INDEX_SCAN_MAX_BATCHES];

	/*
	 * Prefetching related state.
	 *
	 * XXX Should we move this to a heapam struct, such as IndexFetchHeapData?
	 *
	 * currentPrefetchBlock is the table AM block number that was returned by
	 * its read stream callback most recently.  Used to suppress duplicate
	 * successive read stream block requests.
	 *
	 * Occasionally, the read stream callback will request another table block
	 * when the scan has already stored INDEX_SCAN_MAX_BATCHES-many batches.
	 * The paused flag can set to remember that the callback had to return
	 * read_stream_pause() (rather than the next block in line to be read).
	 * When the scan can subsequently consumes enough scanPos items to make it
	 * safe to free another batch, it must check this flag.  If the flag is
	 * set, then the scan should call read_stream_resume (and unset the flag).
	 */
	BlockNumber currentPrefetchBlock;
	bool		paused;

} BatchRingBuffer;

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
	BatchRingBuffer batchringbuf;	/* amgetbatch related state */

	bool		usebatchring;	/* scan uses amgetbatch/batchringbuf? */

	bool		xs_want_itup;	/* caller requests index tuples */
	bool		xs_temp_snap;	/* unregister snapshot at scan end? */

	/* signaling to index AM about killing index tuples */
	bool		kill_prior_tuple;	/* last-returned tuple is dead */
	bool		ignore_killed_tuples;	/* do not return killed entries */
	bool		xactStartedInRecovery;	/* prevents killing/seeing killed
										 * tuples */
	/* Safe to drop index page pins eagerly? */
	bool		MVCCScan;

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
	bool		xs_visible;		/* T means the heap page is all-visible */
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
 * Advance position to its next item in the batch.
 *
 * Advance to the next item within the provided batch (or to the previous item,
 * when scanning backwards).
 *
 * Returns true if the position could be advanced.  Returns false when there
 * are no more items in the batch in the given direction.
 */
static inline bool
index_batchpos_advance(IndexScanBatch batch, BatchRingItemPos *pos,
					   ScanDirection direction)
{
	Assert(!INDEX_SCAN_POS_INVALID(pos));

	if (ScanDirectionIsForward(direction))
	{
		if (++pos->item > batch->lastItem)
			return false;
	}
	else						/* ScanDirectionIsBackward */
	{
		if (--pos->item < batch->firstItem)
			return false;
	}

	/* Advanced within batch */
	return true;
}

/*
 * Advance batch position start of its new batch.
 *
 * Sets the given position to the fist item in the given scan direction (or to
 * the last item, when scanning backwards).   Also advances/increments batch
 * offset from position such that it points to newBatchForPos.
 */
static inline void
index_batchpos_newbatch(IndexScanBatch newBatchForPos, BatchRingItemPos *pos,
						ScanDirection direction)
{
	Assert(newBatchForPos->dir == direction);

	/* Next batch successfully loaded */
	pos->batch++;
	if (ScanDirectionIsForward(direction))
		pos->item = newBatchForPos->firstItem;
	else
		pos->item = newBatchForPos->lastItem;

	Assert(!INDEX_SCAN_POS_INVALID(pos));
}

/*
 * Check that a position (batch,item) is valid with respect to the batches we
 * have currently loaded.
 */
static inline void
batch_assert_pos_valid(IndexScanDescData *scan, BatchRingItemPos *pos)
{
#ifdef USE_ASSERT_CHECKING
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;
	IndexScanBatch batch = INDEX_SCAN_BATCH(scan, pos->batch);

	/* make sure the position is valid for currently loaded batches */
	Assert((int8) (pos->batch - batchringbuf->headBatch) >= 0);
	Assert((int8) (pos->batch - batchringbuf->nextBatch) < 0);
	Assert(pos->item >= batch->firstItem);
	Assert(pos->item <= batch->lastItem);
#endif
}

/*
 * Check a single batch is valid.
 */
static inline void
batch_assert_batch_valid(IndexScanDescData *scan, IndexScanBatch batch)
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
batch_assert_batches_valid(IndexScanDescData *scan)
{
#ifdef USE_ASSERT_CHECKING
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;
	uint8		count = INDEX_SCAN_BATCH_COUNT(scan);

	/* The count should be within valid range */
	Assert(count <= INDEX_SCAN_MAX_BATCHES);

	/* Check all current batches */
	for (uint8 i = 0; i < count; i++)
	{
		uint8		idx = batchringbuf->headBatch + i;
		IndexScanBatch batch = INDEX_SCAN_BATCH(scan, idx);

		batch_assert_batch_valid(scan, batch);
	}
#endif
}

#endif							/* RELSCAN_H */
