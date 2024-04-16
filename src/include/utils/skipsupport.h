/*-------------------------------------------------------------------------
 *
 * skipsupport.h
 *	  Support routines for B-Tree skip scan.
 *
 * B-Tree operator classes for discrete types can optionally provide a support
 * function for skipping.  This is used during skip scans.
 *
 * A B-tree operator class that implements skip support provides B-tree index
 * scans with a way of enumerating and iterating through every possible value
 * from the domain of indexable values.  This gives scans a way to determine
 * the next value in line for a given skip array/scan key/skipped attribute.
 * This happens at the point where the scan determines that another primitive
 * index scan is required.  The next value is used (in combination with at
 * least one additional lower-order non-skip key, taken from the SQL query) to
 * relocate the scan, skipping over many irrelevant leaf pages in the process.
 *
 * Skip support generally works best with discrete types such as integer,
 * date, and boolean; types where there is a decent chance that indexes will
 * contain contiguous values (given a leading attributes using the opclass).
 * When gaps/discontinuities are naturally rare (e.g., a leading identity
 * column in a composite index, a date column preceding a product_id column),
 * then it makes sense for skip scans to optimistically assume that the next
 * distinct indexable value will find directly matching index tuples.
 *
 * The B-Tree code can fall back on next-key sentinel values for any opclass
 * that doesn't provide its own skip support function.  There is no point in
 * providing skip support unless the next indexed key value is often the next
 * indexable value (at least with some workloads).  Opclasses where that never
 * works out in practice should just rely on the B-Tree AM's generic next-key
 * fallback strategy.  Opclasses where adding skip support is infeasible or
 * hard (e.g., an opclass for a continuous type) can also use the fallback.
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
typedef Datum (*SkipSupportIncDec) (Relation rel,
									Datum existing,
									bool *overflow);

/*
 * State/callbacks used by skip arrays to procedurally generate elements.
 *
 * A BTSKIPSUPPORT_PROC function must set each and every field when called.
 * If an opclass can only set some of the fields, then it cannot safely
 * provide a skip support routine.
 */
typedef struct SkipSupportData
{
	/*
	 * low_elem and high_elem must be set with the lowest and highest possible
	 * values from the domain of indexable values (assuming standard ascending
	 * order).  This helps the B-Tree code with finding its initial position
	 * at the leaf level (during the skip scan's first primitive index scan).
	 * In other words, it gives the B-Tree code a useful value to start from,
	 * before any data has been read from the index.
	 *
	 * low_elem and high_elem are also used by skip scans to determine when
	 * they've reached the final possible value (in the current direction).
	 * It's typical for the scan to run out of leaf pages before it runs out
	 * of unscanned indexable values, but it's still useful for the scan to
	 * have a way to recognize when it has reached the last possible value
	 * (this saves us a useless probe that just lands on the final leaf page).
	 */
	Datum		low_elem;		/* lowest sorting/leftmost non-NULL value */
	Datum		high_elem;		/* highest sorting/rightmost non-NULL value */

	/*
	 * Decrement/increment functions.
	 *
	 * Returns a decremented/incremented copy of caller's existing datum,
	 * allocated in caller's memory context (in the case of pass-by-reference
	 * types).  It's not okay for these functions to leak any memory.
	 *
	 * Both decrement and increment callbacks are guaranteed to never be
	 * called with a NULL "existing" arg.
	 *
	 * When the decrement function (or increment function) is called with a
	 * value that already matches low_elem (or high_elem), function must set
	 * the *overflow argument.  The return value is undefined, and the B-Tree
	 * code is entitled to assume that no memory will have been allocated.
	 *
	 * The B-Tree skip scan caller's "existing" datum is often just a straight
	 * copy of a value from an index tuple.  Operator classes must be liberal
	 * in accepting every possible representational variation within the
	 * underlying data type.  On the other hand, opclasses are _not_ expected
	 * to preserve any information that doesn't affect how datums are sorted
	 * (e.g., skip support for a fixed precision numeric type isn't required
	 * to preserve datum display scale).
	 */
	SkipSupportIncDec decrement;
	SkipSupportIncDec increment;
} SkipSupportData;

extern bool PrepareSkipSupportFromOpclass(Oid opfamily, Oid opcintype,
										  bool reverse, SkipSupport sksup);

#endif							/* SKIPSUPPORT_H */
