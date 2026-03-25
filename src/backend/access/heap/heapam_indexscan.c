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
#include "access/indexbatch.h"
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
																 bool index_only,
																 bool amgetbatch);
static pg_attribute_always_inline bool heapam_index_fetch_heap(IndexScanDesc scan,
															   TupleTableSlot *slot,
															   bool *heap_continue,
															   bool amgetbatch);
static pg_attribute_always_inline ItemPointer heapam_index_getnext_scanbatch_pos(IndexScanDesc scan,
																				 IndexFetchHeapData *hscan,
																				 ScanDirection direction,
																				 bool *all_visible);
static inline ItemPointer heapam_index_return_scanpos_tid(IndexScanDesc scan,
														  IndexFetchHeapData *hscan,
														  ScanDirection direction,
														  IndexScanBatch scanBatch,
														  BatchRingItemPos *scanPos,
														  bool *all_visible);
static void heapam_index_batch_pos_visibility(IndexScanDesc scan,
											  ScanDirection direction,
											  IndexScanBatch batch,
											  HeapBatchData *hbatch,
											  BatchRingItemPos *pos);

/* ------------------------------------------------------------------------
 * Index Scan Callbacks for heap AM
 * ------------------------------------------------------------------------
 */

IndexFetchTableData *
heapam_index_fetch_begin(Relation rel)
{
	IndexFetchHeapData *hscan = palloc0_object(IndexFetchHeapData);

	hscan->xs_base.rel = rel;
	hscan->xs_base.batch_opaque_size = MAXALIGN(sizeof(HeapBatchData));
	hscan->xs_base.batch_per_item_size = sizeof(uint8); /* visInfo element size */

	/* Current heap block state */
	Assert(hscan->xs_cbuf == InvalidBuffer);
	hscan->xs_blk = InvalidBlockNumber;

	/* VM related state */
	Assert(hscan->xs_vmbuffer == InvalidBuffer);
	hscan->xs_vm_items = 1;

	return &hscan->xs_base;
}

void
heapam_index_fetch_reset(IndexFetchTableData *scan)
{
	IndexFetchHeapData *hscan = (IndexFetchHeapData *) scan;

	/* Rescans should avoid an excessive number of VM lookups */
	hscan->xs_vm_items = 1;

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

	/*
	 * Note: we expect the core executor to call index_batchscan_end (when the
	 * scan is usebatchring).  This will free all batch related resources.
	 */
}

/*
 * Initialize the heap table AM's per-batch opaque area (HeapBatchData).
 *
 * Called by indexam_util_batch_alloc for each new or recycled batch.
 * Sets up the visInfo pointer for index-only scans, or NULL otherwise.
 */
void
heapam_index_fetch_batch_init(IndexScanDesc scan, IndexScanBatch batch,
							  bool new_alloc)
{
	HeapBatchData *hbatch = heap_batch_data(batch, scan);

	if (scan->xs_want_itup)
	{
		if (new_alloc)
		{
			/*
			 * Point visInfo into the trailing per-item area that follows
			 * items[] in the batch allocation.
			 */
			Size		itemsEnd;

			itemsEnd = MAXALIGN(offsetof(IndexScanBatchData, items) +
								sizeof(BatchMatchingItem) * scan->maxitemsbatch);
			hbatch->visInfo = (uint8 *) ((char *) batch + itemsEnd);
		}

		/* Clear visibility flags (needed for both new and recycled batches) */
		memset(hbatch->visInfo, 0, scan->maxitemsbatch);
	}
	else
	{
		hbatch->visInfo = NULL;
	}
}

/* amgetbatch, plain index scan */
pg_attribute_hot bool
heapam_index_plain_amgetbatch_getnext_slot(IndexScanDesc scan,
										   ScanDirection direction,
										   TupleTableSlot *slot)
{
	Assert(!scan->xs_want_itup && scan->usebatchring);
	Assert(scan->indexRelation->rd_indam->amgetbatch != NULL);

	return heapam_index_getnext_slot(scan, direction, slot, false, true);
}

/* amgetbatch, index-only scan */
pg_attribute_hot bool
heapam_index_only_amgetbatch_getnext_slot(IndexScanDesc scan,
										  ScanDirection direction,
										  TupleTableSlot *slot)
{
	Assert(scan->xs_want_itup && scan->usebatchring);
	Assert(scan->indexRelation->rd_indam->amgetbatch != NULL);

	return heapam_index_getnext_slot(scan, direction, slot, true, true);
}

/* amgettuple, plain index scan */
pg_attribute_hot bool
heapam_index_plain_amgettuple_getnext_slot(IndexScanDesc scan,
										   ScanDirection direction,
										   TupleTableSlot *slot)
{
	Assert(!scan->xs_want_itup && !scan->usebatchring);
	Assert(scan->indexRelation->rd_indam->amgettuple != NULL);

	return heapam_index_getnext_slot(scan, direction, slot, false, false);
}

/* amgettuple, index-only scan */
pg_attribute_hot bool
heapam_index_only_amgettuple_getnext_slot(IndexScanDesc scan,
										  ScanDirection direction,
										  TupleTableSlot *slot)
{
	Assert(scan->xs_want_itup && !scan->usebatchring);
	Assert(scan->indexRelation->rd_indam->amgettuple != NULL);

	return heapam_index_getnext_slot(scan, direction, slot, true, false);
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
 * Common implementation for all four heapam_index_*_getnext_slot variants.
 *
 * The result is true if a tuple satisfying the scan keys and the snapshot was
 * found, false otherwise.  The tuple is stored in the specified slot.
 *
 * On success, resources (like buffer pins) are likely to be held, and will be
 * dropped by a future call here (or by a later call to heapam_index_fetch_end
 * through index_endscan).
 *
 * The index_only and amgetbatch parameters are compile-time constants at each
 * call site, allowing the compiler to specialize the code for each variant:
 */
static pg_attribute_always_inline bool
heapam_index_getnext_slot(IndexScanDesc scan, ScanDirection direction,
						  TupleTableSlot *slot, bool index_only,
						  bool amgetbatch)
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
			if (amgetbatch)
				tid = heapam_index_getnext_scanbatch_pos(scan, hscan,
														 direction,
														 index_only ?
														 &all_visible : NULL);
			else
				tid = index_getnext_tid(scan, direction);

			/* If we're out of index entries, we're done */
			if (tid == NULL)
				break;

			/* For non-batch index-only scans, check the visibility map */
			if (index_only && !amgetbatch)
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

				if (!heapam_index_fetch_heap(scan, slot, &heap_continue,
											 amgetbatch))
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
			if (heapam_index_fetch_heap(scan, slot, &heap_continue,
										amgetbatch))
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
						bool *heap_continue, bool amgetbatch)
{
	bool		all_dead = false;
	bool		found;

	found = heapam_index_fetch_tuple_impl(scan->xs_heapfetch, &scan->xs_heaptid,
										  scan->xs_snapshot, slot,
										  heap_continue, &all_dead);

	if (found)
		pgstat_count_heap_fetch(scan->indexRelation);

	/*
	 * If we scanned a whole HOT chain and found only dead tuples, remember it
	 * for later.  We do not do this when in recovery because it may violate
	 * MVCC to do so.  See comments in RelationGetIndexScan().
	 */
	if (!scan->xactStartedInRecovery)
	{
		if (amgetbatch)
		{
			if (all_dead)
				tableam_util_kill_scanpositem(scan);
		}
		else
		{
			/*
			 * Tell amgettuple-based index AM to kill its entry for that TID
			 * (this will take effect in the next call, in index_getnext_tid)
			 */
			scan->kill_prior_tuple = all_dead;
		}
	}

	return found;
}

/*
 * Get next TID from batch ring buffer, moving in the given scan direction.
 * Also sets *all_visible for item when caller passes a non-NULL arg.
 */
static pg_attribute_always_inline ItemPointer
heapam_index_getnext_scanbatch_pos(IndexScanDesc scan,
								   IndexFetchHeapData *hscan,
								   ScanDirection direction,
								   bool *all_visible)
{
	BatchRingBuffer *batchringbuf = &scan->batchringbuf;
	BatchRingItemPos *scanPos = &batchringbuf->scanPos;
	IndexScanBatch scanBatch = NULL;
	bool		hadExistingScanBatch;

	Assert(!scanPos->valid || batchringbuf->headBatch == scanPos->batch);
	Assert(scanPos->valid || index_scan_batch_count(scan) == 0);
	Assert(all_visible == NULL || scan->xs_want_itup);

	/*
	 * Check if there's an existing loaded scanBatch for us to return the next
	 * matching item's TID/index tuple from
	 */
	hadExistingScanBatch = scanPos->valid;
	if (scanPos->valid)
	{
		/*
		 * scanPos is valid, so scanBatch must already be loaded in batch ring
		 * buffer.  We rely on that here.
		 */
		pg_assume(batchringbuf->headBatch == scanPos->batch);

		scanBatch = index_scan_batch(scan, scanPos->batch);

		if (index_scan_pos_advance(direction, scanBatch, scanPos))
			return heapam_index_return_scanpos_tid(scan, hscan, direction,
												   scanBatch, scanPos,
												   all_visible);
	}

	/*
	 * Either ran out of items from our existing scanBatch, or it hasn't been
	 * loaded yet (because this is the first call here for the entire scan).
	 * Try to advance scanBatch to the next batch (or get the first batch).
	 */
	scanBatch = tableam_util_fetch_next_batch(scan, direction,
											  scanBatch, scanPos);

	if (!scanBatch)
	{
		/*
		 * We're done; no more batches in the current scan direction.
		 *
		 * Note: scanPos is generally still valid at this point.  The scan
		 * might still back up in the other direction.
		 */
		return NULL;
	}

	/*
	 * Advanced scanBatch.  Now position scanPos to the start of new
	 * scanBatch.
	 */
	index_scan_pos_nextbatch(direction, scanBatch, scanPos);
	Assert(index_scan_batch(scan, scanPos->batch) == scanBatch);

	/*
	 * Remove the head batch from the batch ring buffer (except when this new
	 * scanBatch is our only one)
	 */
	if (hadExistingScanBatch)
	{
		IndexScanBatch headBatch = index_scan_batch(scan,
													batchringbuf->headBatch);

		Assert(headBatch != scanBatch);
		Assert(batchringbuf->headBatch != scanPos->batch);

		/* free obsolescent head batch (unless it is scan's markBatch) */
		tableam_util_free_batch(scan, headBatch);

		/* Remove the batch from the ring buffer (even if it's markBatch) */
		batchringbuf->headBatch++;
	}

	/* In practice scanBatch will always be the ring buffer's headBatch */
	Assert(batchringbuf->headBatch == scanPos->batch);

	return heapam_index_return_scanpos_tid(scan, hscan, direction,
										   scanBatch, scanPos, all_visible);
}

/*
 * Save the current scanPos/scanBatch item's TID in scan's xs_heaptid, and
 * return a pointer to that TID.  When all_visible isn't NULL (during an
 * index-only scan), also sets item's visibility status in *all_visible.
 *
 * heapam_index_getnext_scanbatch_pos helper function.
 */
static inline ItemPointer
heapam_index_return_scanpos_tid(IndexScanDesc scan, IndexFetchHeapData *hscan,
								ScanDirection direction,
								IndexScanBatch scanBatch,
								BatchRingItemPos *scanPos,
								bool *all_visible)
{
	HeapBatchData *hbatch;

	pgstat_count_index_tuples(scan->indexRelation, 1);

	/* Set xs_heaptid, which heapam_index_getnext_slot will need */
	scan->xs_heaptid = scanBatch->items[scanPos->item].tableTid;

	if (all_visible == NULL)
	{
		/*
		 * Plain index scan.
		 */
		Assert(!scan->xs_want_itup);
		return &scan->xs_heaptid;
	}

	/*
	 * Index-only scan.
	 *
	 * Also set xs_itup, which heapam_index_getnext_slot needs too.
	 */
	Assert(scan->xs_want_itup);
	scan->xs_itup = (IndexTuple) (scanBatch->currTuples +
								  scanBatch->items[scanPos->item].tupleOffset);

	/*
	 * Set visibility info for the current scanPos item (plus possibly some
	 * additional items in the current scan direction) as needed
	 */
	hbatch = heap_batch_data(scanBatch, scan);
	if (!(hbatch->visInfo[scanPos->item] & HEAP_BATCH_VIS_CHECKED))
		heapam_index_batch_pos_visibility(scan, direction, scanBatch, hbatch,
										  scanPos);

	/* Finally, set all_visible for heapam_index_getnext_slot */
	*all_visible =
		(hbatch->visInfo[scanPos->item] & HEAP_BATCH_VIS_ALL_VISIBLE) != 0;

	return &scan->xs_heaptid;
}

/*
 * Obtain visibility information for a TID from caller's batch.
 *
 * Called during amgetbatch index-only scans.  We always check the visibility
 * of caller's item (an offset into caller's batch->items[] array).  We might
 * also set visibility info for other items from caller's batch more
 * proactively when that makes sense.
 *
 * We keep two competing considerations in balance when determining whether to
 * check additional items: the need to keep the cost of visibility map access
 * under control when most items will never be returned by the scan anyway
 * (important for inner index scans of anti-joins and semi-joins), and the
 * need to unguard batches promptly.
 *
 * Once we've resolved visibility for all items in a batch, we can safely
 * unguard it by calling amunguardbatch.  This is safe with respect to
 * concurrent VACUUM because the batch's guard (typically a buffer pin on the
 * originating index page) blocks VACUUM from acquiring a conflicting cleanup
 * lock on that page.  Copying the relevant visibility map data into our local
 * cache suffices to prevent unsafe concurrent TID recycling: if any of these
 * TIDs point to dead heap tuples, VACUUM cannot possibly return from
 * ambulkdelete and mark the pointed-to heap pages as all-visible.  VACUUM
 * _can_ do so once the batch is unguarded, but that's okay; we'll be working
 * off of cached visibility info that indicates that the dead TIDs are NOT
 * all-visible.
 *
 * What about the opposite case, where a page was all-visible when we cached
 * the VM bits but tuples on it are deleted afterwards?  That is safe too: any
 * tuple that was visible to all when we read the VM must also be visible to
 * our MVCC snapshot, so it is correct to skip the heap fetch for those TIDs.
 */
static void
heapam_index_batch_pos_visibility(IndexScanDesc scan, ScanDirection direction,
								  IndexScanBatch batch, HeapBatchData *hbatch,
								  BatchRingItemPos *pos)
{
	IndexFetchHeapData *hscan = (IndexFetchHeapData *) scan->xs_heapfetch;
	int			posItem = pos->item;
	bool		allbatchitemsvisible;
	BlockNumber curvmheapblkno = InvalidBlockNumber;
	uint8		curvmheapblkflags = 0;

	Assert(hbatch == heap_batch_data(batch, scan));

	/*
	 * The batch must still be guarded (amunguardbatch has not been called
	 * yet), so the TID recycling interlock is still in effect.
	 */
	Assert(!scan->batchImmediateUnguard);

	/*
	 * Set visibility info for a range of items, in scan order.
	 *
	 * Note: visibilitymap_get_status does not lock the visibility map buffer,
	 * so the result could be slightly stale.  See the "Memory ordering
	 * effects" discussion above visibilitymap_get_status for an explanation
	 * of why this is okay.
	 */
	if (ScanDirectionIsForward(direction))
	{
		int			lastSetItem = Min(batch->lastItem,
									  posItem + hscan->xs_vm_items - 1);

		for (int setItem = posItem; setItem <= lastSetItem; setItem++)
		{
			ItemPointer tid = &batch->items[setItem].tableTid;
			BlockNumber heapblkno = ItemPointerGetBlockNumber(tid);
			uint8		flags;

			if (heapblkno == curvmheapblkno)
			{
				hbatch->visInfo[setItem] = curvmheapblkflags;
				continue;
			}

			flags = HEAP_BATCH_VIS_CHECKED;
			if (VM_ALL_VISIBLE(scan->heapRelation, heapblkno, &hscan->xs_vmbuffer))
				flags |= HEAP_BATCH_VIS_ALL_VISIBLE;

			hbatch->visInfo[setItem] = curvmheapblkflags = flags;
			curvmheapblkno = heapblkno;
		}

		allbatchitemsvisible = lastSetItem >= batch->lastItem &&
			(posItem == batch->firstItem ||
			 (hbatch->visInfo[batch->firstItem] & HEAP_BATCH_VIS_CHECKED));
	}
	else
	{
		int			lastSetItem = Max(batch->firstItem,
									  posItem - hscan->xs_vm_items + 1);

		for (int setItem = posItem; setItem >= lastSetItem; setItem--)
		{
			ItemPointer tid = &batch->items[setItem].tableTid;
			BlockNumber heapblkno = ItemPointerGetBlockNumber(tid);
			uint8		flags;

			if (heapblkno == curvmheapblkno)
			{
				hbatch->visInfo[setItem] = curvmheapblkflags;
				continue;
			}

			flags = HEAP_BATCH_VIS_CHECKED;
			if (VM_ALL_VISIBLE(scan->heapRelation, heapblkno, &hscan->xs_vmbuffer))
				flags |= HEAP_BATCH_VIS_ALL_VISIBLE;

			hbatch->visInfo[setItem] = curvmheapblkflags = flags;
			curvmheapblkno = heapblkno;
		}

		allbatchitemsvisible = lastSetItem <= batch->firstItem &&
			(posItem == batch->lastItem ||
			 (hbatch->visInfo[batch->lastItem] & HEAP_BATCH_VIS_CHECKED));
	}

	/*
	 * It's safe to unguard the batch (via amunguardbatch) as soon as we've
	 * resolved the visibility status of all of its items (unless this is a
	 * non-MVCC scan)
	 */
	if (allbatchitemsvisible)
	{
		Assert(hbatch->visInfo[batch->firstItem] & HEAP_BATCH_VIS_CHECKED);
		Assert(hbatch->visInfo[batch->lastItem] & HEAP_BATCH_VIS_CHECKED);

		if (batch->isGuarded && scan->MVCCScan)
			tableam_util_unguard_batch(scan, batch);
	}

	/*
	 * Else check visibility for twice as many items next time, or all items.
	 * We check all items in one go once we're passed the scan's first batch.
	 */
	else if (hscan->xs_vm_items < (batch->lastItem - batch->firstItem))
		hscan->xs_vm_items *= 2;
	else
		hscan->xs_vm_items = scan->maxitemsbatch;
}
