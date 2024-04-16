/*-------------------------------------------------------------------------
 *
 * skipsupport.c
 *	  Support routines for B-Tree skip scans.
 *
 *
 * Portions Copyright (c) 1996-2024, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * IDENTIFICATION
 *	  src/backend/utils/adt/skipsupport.c
 *
 *-------------------------------------------------------------------------
 */

#include "postgres.h"

#include <limits.h>

#include "access/nbtree.h"
#include "utils/lsyscache.h"
#include "utils/skipsupport.h"

bool
skipsupport_setup_callbacks(Oid opfamily, Oid opcintype, bool reverse,
							SkipSupport sksup)
{
	Oid			skipSupportFunction;

	/*
	 * XXX Short-term measure, to paper-over a known test failure caused by
	 * cross-type preprocessing bug (needed for 32-bit CI to pass).
	 *
	 * This is required to get the create_index regression test to pass when
	 * !USE_FLOAT8_BYVAL.  Once _bt_skip_scankey_preprocess is taught to set
	 * low_value and high_value in a way that accounts for cross-type
	 * differences within a B-Tree opfamily (by fixing its FIXME item), we
	 * won't need to do this anymore.
	 */
#ifndef USE_FLOAT8_BYVAL
	if (opcintype == INT8OID)
		return false;
#endif

	/* Look for a skip support function */
	skipSupportFunction = get_opfamily_proc(opfamily, opcintype, opcintype,
											BTSKIPSUPPORT_PROC);
	if (!OidIsValid(skipSupportFunction))
		return false;

	OidFunctionCall1(skipSupportFunction, PointerGetDatum(sksup));

	if (reverse)
	{
		Datum		low_elem = sksup->low_elem;

		sksup->low_elem = sksup->high_elem;
		sksup->high_elem = low_elem;
	}

	return true;
}
