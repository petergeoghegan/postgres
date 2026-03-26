/*-------------------------------------------------------------------------
 *
 * heapam_indexscan.c
 *	  heap table plain index scan and index-only scan code
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 *
 * IDENTIFICATION
 *	  src/backend/access/heap/heapam_indexscan.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/amapi.h"
#include "access/heapam.h"
#include "access/visibilitymap.h"
#include "storage/predicate.h"
#include "utils/pgstat_internal.h"


static pg_attribute_always_inline bool heapam_index_fetch_tuple_impl(struct IndexFetchTableData *scan,
																	 ItemPointer tid,
																	 Snapshot snapshot,
																	 TupleTableSlot *slot,
																	 bool *heap_continue,
																	 bool *all_dead);
static pg_attribute_always_inline bool heapam_index_getnext_slot(IndexScanDesc scan,
																 ScanDirection direction,
																 TupleTableSlot *slot,
																 bool index_only);
static pg_attribute_always_inline bool heapam_index_fetch_heap(IndexScanDesc scan,
															   TupleTableSlot *slot,
															   bool *heap_continue);

/* ------------------------------------------------------------------------
 * Index Scan Callbacks for heap AM
 * ------------------------------------------------------------------------
 */

IndexFetchTableData *
heapam_index_fetch_begin(Relation rel)
{
	IndexFetchHeapData *hscan = palloc0_object(IndexFetchHeapData);

	hscan->xs_base.rel = rel;
	hscan->xs_cbuf = InvalidBuffer;
	hscan->xs_blk = InvalidBlockNumber;
	hscan->xs_vmbuffer = InvalidBuffer;

	return &hscan->xs_base;
}

void
heapam_index_fetch_reset(IndexFetchTableData *scan)
{
	IndexFetchHeapData *hscan = (IndexFetchHeapData *) scan;

	/* Resets are a no-op (XXX amgetbatch commit resets xs_vm_items here) */
	(void) hscan;

	/*
	 * Deliberately avoid dropping pins now held in xs_cbuf and xs_vmbuffer.
	 * This saves cycles during certain tight nested loop joins, and during
	 * merge joins that frequently restore a saved mark.  It can also avoid
	 * repeated pinning and unpinning of the same buffer across rescans.
	 */
}

void
heapam_index_fetch_end(IndexFetchTableData *scan)
{
	IndexFetchHeapData *hscan = (IndexFetchHeapData *) scan;

	/* drop pin if there's a pinned heap page */
	if (BufferIsValid(hscan->xs_cbuf))
		ReleaseBuffer(hscan->xs_cbuf);

	/* drop pin if there's a pinned visibility map page */
	if (BufferIsValid(hscan->xs_vmbuffer))
		ReleaseBuffer(hscan->xs_vmbuffer);

	pfree(hscan);
}

/* amgettuple, plain index scan */
pg_attribute_hot bool
heapam_index_plain_amgettuple_getnext_slot(IndexScanDesc scan,
										   ScanDirection direction,
										   TupleTableSlot *slot)
{
	Assert(!scan->xs_want_itup);
	Assert(scan->indexRelation->rd_indam->amgettuple != NULL);

	return heapam_index_getnext_slot(scan, direction, slot, false);
}

/* amgettuple, index-only scan */
pg_attribute_hot bool
heapam_index_only_amgettuple_getnext_slot(IndexScanDesc scan,
										  ScanDirection direction,
										  TupleTableSlot *slot)
{
	Assert(scan->xs_want_itup);
	Assert(scan->indexRelation->rd_indam->amgettuple != NULL);

	return heapam_index_getnext_slot(scan, direction, slot, true);
}

bool
heapam_index_fetch_tuple(struct IndexFetchTableData *scan,
						 ItemPointer tid,
						 Snapshot snapshot,
						 TupleTableSlot *slot,
						 bool *heap_continue, bool *all_dead)
{

	return heapam_index_fetch_tuple_impl(scan, tid, snapshot, slot,
										 heap_continue, all_dead);
}

static pg_attribute_always_inline bool
heapam_index_fetch_tuple_impl(struct IndexFetchTableData *scan,
							  ItemPointer tid,
							  Snapshot snapshot,
							  TupleTableSlot *slot,
							  bool *heap_continue, bool *all_dead)
{
	IndexFetchHeapData *hscan = (IndexFetchHeapData *) scan;
	BufferHeapTupleTableSlot *bslot = (BufferHeapTupleTableSlot *) slot;
	bool		got_heap_tuple;

	Assert(TTS_IS_BUFFERTUPLE(slot));

	/* We can skip the buffer-switching logic if we're on the same page. */
	if (hscan->xs_blk != ItemPointerGetBlockNumber(tid))
	{
		Assert(!*heap_continue);

		/* Remember this buffer's block number for next time */
		hscan->xs_blk = ItemPointerGetBlockNumber(tid);

		if (BufferIsValid(hscan->xs_cbuf))
			ReleaseBuffer(hscan->xs_cbuf);

		hscan->xs_cbuf = ReadBuffer(hscan->xs_base.rel, hscan->xs_blk);

		/*
		 * Prune page when it is pinned for the first time
		 */
		heap_page_prune_opt(hscan->xs_base.rel, hscan->xs_cbuf,
							&hscan->xs_vmbuffer);
	}

	Assert(BufferGetBlockNumber(hscan->xs_cbuf) == hscan->xs_blk);
	Assert(hscan->xs_blk == ItemPointerGetBlockNumber(tid));

	/* Obtain share-lock on the buffer so we can examine visibility */
	LockBuffer(hscan->xs_cbuf, BUFFER_LOCK_SHARE);
	got_heap_tuple = heap_hot_search_buffer(tid,
											hscan->xs_base.rel,
											hscan->xs_cbuf,
											snapshot,
											&bslot->base.tupdata,
											all_dead,
											!*heap_continue);
	bslot->base.tupdata.t_self = *tid;
	LockBuffer(hscan->xs_cbuf, BUFFER_LOCK_UNLOCK);

	if (got_heap_tuple)
	{
		/*
		 * Only in a non-MVCC snapshot can more than one member of the HOT
		 * chain be visible.
		 */
		*heap_continue = !IsMVCCLikeSnapshot(snapshot);

		slot->tts_tableOid = RelationGetRelid(scan->rel);
		ExecStoreBufferHeapTuple(&bslot->base.tupdata, slot, hscan->xs_cbuf);
	}
	else
	{
		/* We've reached the end of the HOT chain. */
		*heap_continue = false;
	}

	return got_heap_tuple;
}

/*
 * Common implementation for both heapam_index_*_getnext_slot variants.
 *
 * The result is true if a tuple satisfying the scan keys and the snapshot was
 * found, false otherwise.  The tuple is stored in the specified slot.
 *
 * On success, resources (like buffer pins) are likely to be held, and will be
 * dropped by a future call here (or by a later call to heapam_index_fetch_end
 * through index_endscan).
 *
 * The index_only parameter is a compile-time constant at each call site,
 * allowing the compiler to specialize the code for each variant.
 */
static pg_attribute_always_inline bool
heapam_index_getnext_slot(IndexScanDesc scan, ScanDirection direction,
						  TupleTableSlot *slot, bool index_only)
{
	IndexFetchHeapData *hscan = (IndexFetchHeapData *) scan->xs_heapfetch;
	ItemPointer tid;
	bool		heap_continue = false;
	bool		all_visible = false;
	BlockNumber last_visited_block = InvalidBlockNumber;
	uint8		n_visited_pages = 0,
				xs_visited_pages_limit = 0;

	if (index_only)
		xs_visited_pages_limit = scan->xs_visited_pages_limit;

	for (;;)
	{
		if (!heap_continue)
		{
			/* Get the next TID from the index */
			tid = index_getnext_tid(scan, direction);

			/* If we're out of index entries, we're done */
			if (tid == NULL)
				break;

			/* For index-only scans, check the visibility map */
			if (index_only)
				all_visible = VM_ALL_VISIBLE(scan->heapRelation,
											 ItemPointerGetBlockNumber(tid),
											 &hscan->xs_vmbuffer);
		}

		Assert(ItemPointerIsValid(&scan->xs_heaptid));

		if (index_only)
		{
			/*
			 * We can skip the heap fetch if the TID references a heap page on
			 * which all tuples are known visible to everybody.  In any case,
			 * we'll use the index tuple not the heap tuple as the data
			 * source.
			 */
			if (!all_visible)
			{
				/*
				 * Rats, we have to visit the heap to check visibility.
				 */
				if (scan->instrument)
					scan->instrument->ntablefetches++;

				if (!heapam_index_fetch_heap(scan, slot, &heap_continue))
				{
					/*
					 * No visible tuple.  If caller set a visited-pages limit
					 * (only selfuncs.c does this), count distinct heap pages
					 * and give up once we've visited too many.
					 */
					if (unlikely(xs_visited_pages_limit > 0))
					{
						Assert(hscan->xs_blk == ItemPointerGetBlockNumber(tid));

						if (hscan->xs_blk != last_visited_block)
						{
							last_visited_block = hscan->xs_blk;
							if (++n_visited_pages > xs_visited_pages_limit)
								return false;	/* give up */
						}
					}
					continue;	/* no visible tuple, try next index entry */
				}

				ExecClearTuple(slot);

				/*
				 * Only MVCC snapshots are supported with standard index-only
				 * scans, so there should be no need to keep following the HOT
				 * chain once a visible entry has been found.  Other callers
				 * (currently only selfuncs.c) use SnapshotNonVacuumable, and
				 * want us to assume that just having one visible tuple in the
				 * hot chain is always good enough.
				 */
				Assert(!(heap_continue && IsMVCCSnapshot(scan->xs_snapshot)));
			}
			else
			{
				/*
				 * We didn't access the heap, so we'll need to take a
				 * predicate lock explicitly, as if we had.  For now we do
				 * that at page level.
				 */
				PredicateLockPage(scan->heapRelation,
								  ItemPointerGetBlockNumber(tid),
								  scan->xs_snapshot);
			}

			/*
			 * Return matching index tuple now set in scan->xs_itup (or return
			 * matching heap tuple now set in scan->xs_hitup)
			 */
			return true;
		}
		else
		{
			/*
			 * Fetch the next (or only) visible heap tuple for this index
			 * entry.  If we don't find anything, loop around and grab the
			 * next TID from the index.
			 */
			if (heapam_index_fetch_heap(scan, slot, &heap_continue))
				return true;
		}
	}

	return false;
}

/*
 * Get the scan's next heap tuple.
 *
 * The result is a visible heap tuple associated with the index TID most
 * recently fetched by our caller in scan->xs_heaptid, or NULL if no more
 * matching tuples exist.  (There can be more than one matching tuple because
 * of HOT chains, although when using an MVCC snapshot it should be impossible
 * for more than one such tuple to exist.)
 *
 * On success, the buffer containing the heap tup is pinned.  The pin must be
 * dropped elsewhere.
 */
static pg_attribute_always_inline bool
heapam_index_fetch_heap(IndexScanDesc scan, TupleTableSlot *slot,
						bool *heap_continue)
{
	bool		all_dead = false;
	bool		found;

	found = heapam_index_fetch_tuple_impl(scan->xs_heapfetch, &scan->xs_heaptid,
										  scan->xs_snapshot, slot,
										  heap_continue, &all_dead);

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
		scan->kill_prior_tuple = all_dead;

	return found;
}
