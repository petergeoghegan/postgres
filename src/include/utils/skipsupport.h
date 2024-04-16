/*-------------------------------------------------------------------------
 *
 * skipsupport.h
 *	  Support routines for B-Tree skip scans.
 *
 * B-Tree operator classes for discrete types (such as integer and text) can
 * optionally provide a support function that is used during skip scans.
 *
 *
 * Portions Copyright (c) 1996-2024, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/include/utils/skipsupport.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef SKIPSUPPORT_H
#define SKIPSUPPORT_H

#include "utils/relcache.h"

typedef struct SkipSupportData *SkipSupport;

typedef struct SkipSupportData
{
	/* State used by skip arrays to procedurally generate elements */
	Datum		low_elem;		/* lowest sorting/leftmost non-NULL value */
	Datum		high_elem;		/* highest sorting/rightmost non-NULL value */

	/* per-opclass/per-type callbacks to decrement/increment skip arrays */
	Datum (*decrement) (Relation rel, Datum existing);
	Datum (*increment) (Relation rel, Datum existing);

} SkipSupportData;

extern bool skipsupport_setup_callbacks(SkipSupport sksup, bool reverse,
										Oid opfamily, Oid opcintype);

#endif							/* SKIPSUPPORT_H */
