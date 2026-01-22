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
 * Location of a BatchMatchingItem that appears in a IndexScanBatch returned
 * by (and subsequently passed to) an amgetbatch routine
 */
typedef struct BatchRingItemPos
{
	/* Position references a valid BatchRingBuffer.batches[] entry? */
	bool		valid;

	/* BatchRingBuffer.batches[]-wise index to relevant IndexScanBatch */
	uint8		batch;

	/* IndexScanBatch.items[]-wise index to relevant BatchMatchingItem */
	int			item;

} BatchRingItemPos;

/*
 * Matching item returned by amgetbatch (in returned IndexScanBatch) during an
 * index scan.  Used by table AM to locate relevant matching table tuple.
 */
typedef struct BatchMatchingItem
{
	ItemPointerData heapTid;	/* TID of referenced heap item */
	OffsetNumber indexOffset;	/* index item's location within page */
	LocationIndex tupleOffset;	/* IndexTuple's offset in workspace, if any */
	bool		checkedVisible; /* checked heapTid table block's visibility? */
	bool		allVisible;		/* table block is known to be all-visible? */
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
	 * knownEndLeft and knownEndRight are used by table AMs to track whether
	 * there may be matching index entries to the left and right of currPage,
	 * respectively.  This helps them to avoid repeatedly calling amgetbatch.
	 */
	bool		knownEndLeft;
	bool		knownEndRight;

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
	/* current positions in batches[] for scan */
	BatchRingItemPos scanPos;	/* scan's read position */
	BatchRingItemPos markPos;	/* mark/restore position */
	BatchRingItemPos prefetchPos;	/* prefetching position */

	IndexScanBatch markBatch;

	/*
	 * headBatch is an index to the earliest still-valid batch in 'batches'.
	 * In practice this must be the scan's current scanPos batch (scanBatch).
	 */
	uint8		headBatch;

	/*
	 * nextBatch is an index to the next empty batch slot in 'batches'.  This
	 * is only actually usable when the scan is !index_scan_batch_full().
	 */
	uint8		nextBatch;

	/*
	 * Should indexam_util_batch_release save caller's batch in cache[]?
	 */
	bool		done;

	/* Array of pointers to cached recyclable batches */
	IndexScanBatch cache[INDEX_SCAN_CACHE_BATCHES];

	/* Array of pointers to ring buffer batches */
	IndexScanBatch batches[INDEX_SCAN_MAX_BATCHES];

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
	/* xs_snapshot uses an MVCC snapshot? */
	bool		MVCCScan;

	/*
	 * Instrumentation counters maintained by all index AMs during both
	 * amgettuple calls and amgetbitmap calls (unless field remains NULL)
	 */
	struct IndexScanInstrumentation *instrument;

	/*
	 * In an index-only scan, a successful table_index_getnext_slot call must
	 * fill either xs_itup (and xs_itupdesc) or xs_hitup (and xs_hitupdesc) to
	 * provide the data returned by the scan.  It can fill both, in which case
	 * the heap format will be used.
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

	int64		tuples_needed;

	/*
	 * Flag to request early abort during get_actual_variable_range scans.
	 * Such scans must end on the rightmost (or leftmost) index page, no
	 * matter whether there are more matches in previous (or later) pages.
	 */
	bool		xs_read_extremal_only;
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
 * Count how many batches are currently loaded in the ring buffer.
 */
static inline uint8
index_scan_batch_count(IndexScanDescData *scan)
{
	return (uint8) (scan->batchringbuf.nextBatch -
					scan->batchringbuf.headBatch);
}

/*
 * Did we already load batch with the requested index?
 */
static inline bool
index_scan_batch_loaded(IndexScanDescData *scan, uint8 idx)
{
	return (int8) (idx - scan->batchringbuf.headBatch) >= 0 &&
		(int8) (idx - scan->batchringbuf.nextBatch) < 0;
}

/*
 * Have we loaded the maximum number of batches?
 */
static inline bool
index_scan_batch_full(IndexScanDescData *scan)
{
	return index_scan_batch_count(scan) == INDEX_SCAN_MAX_BATCHES;
}

/*
 * Return batch for the provided index.
 */
static inline IndexScanBatch
index_scan_batch(IndexScanDescData *scan, uint8 idx)
{
	Assert(index_scan_batch_loaded(scan, idx));

	return scan->batchringbuf.batches[idx & (INDEX_SCAN_MAX_BATCHES - 1)];
}

/*
 * Append given batch to scan's batch ring buffer.
 */
static inline void
index_scan_batch_append(IndexScanDescData *scan, IndexScanBatch batch)
{
	BatchRingBuffer *ringbuf = &scan->batchringbuf;
	uint8		nextBatch = ringbuf->nextBatch;

	ringbuf->batches[nextBatch & (INDEX_SCAN_MAX_BATCHES - 1)] = batch;
	ringbuf->nextBatch++;
}

/*
 * Compare two batch ring positions in the given scan direction.
 *
 * Returns negative if pos1 is behind pos2, 0 if equal, positive if pos1 is
 * ahead of pos2.
 */
static inline int
index_scan_pos_cmp(BatchRingItemPos *pos1, BatchRingItemPos *pos2,
				   ScanDirection direction)
{
	int8		batchdiff = (int8) (pos1->batch - pos2->batch);

	if (batchdiff != 0)
		return batchdiff;

	/* Same batch, compare items */
	if (ScanDirectionIsForward(direction))
		return pos1->item - pos2->item;
	else
		return pos2->item - pos1->item;
}

/*
 * Return the signed distance in batches between two positions.
 *
 * Positive means pos1 is ahead of pos2 by that many batches.
 */
static inline int8
index_scan_pos_batch_distance(BatchRingItemPos *pos1, BatchRingItemPos *pos2)
{
	return (int8) (pos1->batch - pos2->batch);
}

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
index_scan_pos_advance(ScanDirection direction,
					   IndexScanBatch batch, BatchRingItemPos *pos)
{
	Assert(pos->valid);

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
 * Advance batch position to the start of its new batch.
 *
 * Sets the given position to the fist item in the given scan direction (or to
 * the last item, when scanning backwards).   Also advances/increments batch
 * offset from position such that it points to newBatchForPos.
 */
static inline void
index_scan_pos_nextbatch(ScanDirection direction,
						 IndexScanBatch newBatch, BatchRingItemPos *pos)
{
	Assert(newBatch->dir == direction);

	/* Increment batch (often wraps uint8 batch field) */
	if (pos->valid)
		pos->batch++;
	else
		pos->batch = 0;

	pos->valid = true;

	if (ScanDirectionIsForward(direction))
		pos->item = newBatch->firstItem;
	else
		pos->item = newBatch->lastItem;
}

#endif							/* RELSCAN_H */
