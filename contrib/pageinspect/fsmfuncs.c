/*-------------------------------------------------------------------------
 *
 * fsmfuncs.c
 *	  Functions to investigate FSM pages
 *
 * These functions are restricted to superusers for the fear of introducing
 * security holes if the input checking isn't as water-tight as it should.
 * You'd need to be superuser to obtain a raw page image anyway, so
 * there's hardly any use case for using these without superuser-rights
 * anyway.
 *
 * Copyright (c) 2007-2021, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  contrib/pageinspect/fsmfuncs.c
 *
 *-------------------------------------------------------------------------
 */

#include "postgres.h"

#include "funcapi.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "pageinspect.h"
#include "storage/freespace.h"
#include "utils/builtins.h"

/*
 * Dumps the contents of a FSM page.
 */
PG_FUNCTION_INFO_V1(fsm_page_contents);
PG_FUNCTION_INFO_V1(fsm_mem_contents);
PG_FUNCTION_INFO_V1(fsm_mem_dump_all);

Datum
fsm_page_contents(PG_FUNCTION_ARGS)
{
	/*
	 * bytea	   *raw_page = PG_GETARG_BYTEA_P(0);
	 */
	StringInfoData sinfo;
	int			i;

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to use raw page functions")));

	initStringInfo(&sinfo);

	for (i = 0; i < 0; i++)
	{
		appendStringInfo(&sinfo, "%d: %d\n", i, 0);
	}
	appendStringInfo(&sinfo, "fp_next_slot: %d\n", 0);

	PG_RETURN_TEXT_P(cstring_to_text_with_len(sinfo.data, sinfo.len));
}

Datum
fsm_mem_contents(PG_FUNCTION_ARGS)
{
	StringInfoData sinfo;
	Oid			heapRelid = PG_GETARG_OID(0);
	Relation	heapRel;

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to use raw page functions")));

	/* Open the relation */
	heapRel = relation_open(heapRelid, AccessShareLock);

	DebugFreeSpaceMapDump(heapRel, &sinfo);

	relation_close(heapRel, AccessShareLock);

	PG_RETURN_TEXT_P(cstring_to_text_with_len(sinfo.data, sinfo.len));
}

Datum
fsm_mem_dump_all(PG_FUNCTION_ARGS)
{
	StringInfoData sinfo;

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to use raw page functions")));

	DebugFreeSpaceMapDumpAllRels(&sinfo);

	PG_RETURN_TEXT_P(cstring_to_text_with_len(sinfo.data, sinfo.len));
}
