/*--------------------------------------------------------------------------
 *
 * test_indexscan.c
 *		Test helpers for low-level index scan behavior.
 *
 * Copyright (c) 2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *		src/test/modules/index/test_indexscan.c
 *
 * -------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/genam.h"
#include "access/relscan.h"
#include "access/table.h"
#include "access/tableam.h"
#include "executor/tuptable.h"
#include "fmgr.h"
#include "funcapi.h"
#include "storage/itemptr.h"
#include "utils/builtins.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/tuplestore.h"

PG_MODULE_MAGIC;

/*
 * index_scan_tids(heaprel regclass, indexrel regclass, snaptype text,
 *				   dir text) RETURNS SETOF tid
 *
 * Scan indexrel (an index on heaprel) with zero scan keys, using a snapshot
 * of the given type and the given scan direction, and return the heap TID of
 * every tuple that the scan returns, in scan order.
 */
PG_FUNCTION_INFO_V1(index_scan_tids);
Datum
index_scan_tids(PG_FUNCTION_ARGS)
{
	Oid			heapoid = PG_GETARG_OID(0);
	Oid			indexoid = PG_GETARG_OID(1);
	char	   *snaptype = text_to_cstring(PG_GETARG_TEXT_PP(2));
	char	   *dirstr = text_to_cstring(PG_GETARG_TEXT_PP(3));
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Relation	heaprel;
	Relation	indexrel;
	ScanDirection dir;
	SnapshotData snapdata;
	Snapshot	snapshot;
	IndexScanDesc scan;
	TupleTableSlot *slot;
	bool		recheck;

	InitMaterializedSRF(fcinfo, MAT_SRF_USE_EXPECTED_DESC);

	if (strcmp(dirstr, "forward") == 0)
		dir = ForwardScanDirection;
	else if (strcmp(dirstr, "backward") == 0)
		dir = BackwardScanDirection;
	else
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("invalid scan direction \"%s\"", dirstr)));

	heaprel = table_open(heapoid, AccessShareLock);
	indexrel = index_open(indexoid, AccessShareLock);

	if (indexrel->rd_index->indrelid != heapoid)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("\"%s\" is not an index on table \"%s\"",
						RelationGetRelationName(indexrel),
						RelationGetRelationName(heaprel))));

	if (strcmp(snaptype, "mvcc") == 0)
		snapshot = GetActiveSnapshot();
	else if (strcmp(snaptype, "any") == 0)
		snapshot = SnapshotAny;
	else if (strcmp(snaptype, "self") == 0)
		snapshot = SnapshotSelf;
	else if (strcmp(snaptype, "dirty") == 0)
	{
		InitDirtySnapshot(snapdata);
		snapshot = &snapdata;
	}
	else if (strcmp(snaptype, "nonvacuumable") == 0)
	{
		InitNonVacuumableSnapshot(snapdata, GlobalVisTestFor(heaprel));
		snapshot = &snapdata;
	}
	else
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("invalid snapshot type \"%s\"", snaptype)));

	slot = table_slot_create(heaprel, NULL);

	scan = index_beginscan(heaprel, indexrel, false, snapshot, NULL,
						   0, 0, SO_NONE);
	index_rescan(scan, NULL, 0, NULL, 0);

	while (table_index_getnext_slot(scan, dir, slot, &recheck))
	{
		ItemPointerData tid = slot->tts_tid;
		Datum		values[1];
		bool		nulls[1];

		/* with zero scan keys, no AM should ever request a recheck */
		if (recheck)
			elog(ERROR, "unexpected recheck request from keyless index scan");

		values[0] = ItemPointerGetDatum(&tid);
		nulls[0] = false;
		tuplestore_putvalues(rsinfo->setResult, rsinfo->setDesc,
							 values, nulls);
	}

	index_endscan(scan);
	ExecDropSingleTupleTableSlot(slot);
	index_close(indexrel, AccessShareLock);
	table_close(heaprel, AccessShareLock);

	return (Datum) 0;
}
