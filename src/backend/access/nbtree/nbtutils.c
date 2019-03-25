/*-------------------------------------------------------------------------
 *
 * nbtutils.c
 *	  Utility code for Postgres btree implementation.
 *
 * Portions Copyright (c) 1996-2024, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 *
 * IDENTIFICATION
 *	  src/backend/access/nbtree/nbtutils.c
 *
 *-------------------------------------------------------------------------
 */

#include "postgres.h"

#include <time.h>

#include "access/nbtree.h"
#include "access/reloptions.h"
#include "access/relscan.h"
#include "catalog/catalog.h"
#include "commands/progress.h"
#include "lib/qunique.h"
#include "miscadmin.h"
#include "utils/array.h"
#include "utils/datum.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"


typedef struct BTSortArrayContext
{
	FmgrInfo   *sortproc;
	Oid			collation;
	bool		reverse;
} BTSortArrayContext;

typedef struct ScanKeyAttr
{
	ScanKey		skey;
	int			ikey;
} ScanKeyAttr;

static void _bt_setup_array_cmp(IndexScanDesc scan, ScanKey skey, Oid elemtype,
								FmgrInfo *orderproc, FmgrInfo **sortprocp);
static Datum _bt_find_extreme_element(IndexScanDesc scan, ScanKey skey,
									  Oid elemtype, StrategyNumber strat,
									  Datum *elems, int nelems);
static int	_bt_sort_array_elements(ScanKey skey, FmgrInfo *sortproc,
									bool reverse, Datum *elems, int nelems);
static int	_bt_merge_arrays(ScanKey skey, FmgrInfo *sortproc, bool reverse,
							 Datum *elems_orig, int nelems_orig,
							 Datum *elems_next, int nelems_next);
static int	_bt_compare_array_elements(const void *a, const void *b, void *arg);
static inline int32 _bt_compare_array_skey(FmgrInfo *orderproc,
										   Datum tupdatum, bool tupnull,
										   Datum arrdatum, ScanKey cur);
static int	_bt_binsrch_array_skey(FmgrInfo *orderproc,
								   bool cur_elem_start, ScanDirection dir,
								   Datum tupdatum, bool tupnull,
								   BTArrayKeyInfo *array, ScanKey cur,
								   int32 *set_elem_result);
static bool _bt_advance_array_keys_increment(IndexScanDesc scan, ScanDirection dir);
static bool _bt_tuple_before_array_skeys(IndexScanDesc scan, ScanDirection dir,
										 IndexTuple tuple, bool readpagetup,
										 int sktrig);
static bool _bt_advance_array_keys(IndexScanDesc scan, BTReadPageState *pstate,
								   IndexTuple tuple, int sktrig);
static void _bt_update_keys_with_arraykeys(IndexScanDesc scan);
#ifdef USE_ASSERT_CHECKING
static bool _bt_verify_keys_with_arraykeys(IndexScanDesc scan);
#endif
static bool _bt_compare_scankey_args(IndexScanDesc scan, ScanKey op,
									 ScanKey leftarg, ScanKey rightarg,
									 bool *result);
static bool _bt_fix_scankey_strategy(ScanKey skey, int16 *indoption);
static void _bt_mark_scankey_required(ScanKey skey);
static bool _bt_check_compare(ScanDirection dir, BTScanOpaque so,
							  IndexTuple tuple, int tupnatts, TupleDesc tupdesc,
							  int numArrayKeys, bool *continuescan, int *ikey,
							  bool continuescanPrechecked, bool haveFirstMatch);
static bool _bt_check_rowcompare(ScanKey skey,
								 IndexTuple tuple, int tupnatts, TupleDesc tupdesc,
								 ScanDirection dir, bool *continuescan);
static int	_bt_keep_natts(Relation rel, IndexTuple lastleft,
						   IndexTuple firstright, BTScanInsert itup_key);


/*
 * _bt_mkscankey
 *		Build an insertion scan key that contains comparison data from itup
 *		as well as comparator routines appropriate to the key datatypes.
 *
 *		The result is intended for use with _bt_compare() and _bt_truncate().
 *		Callers that don't need to fill out the insertion scankey arguments
 *		(e.g. they use an ad-hoc comparison routine, or only need a scankey
 *		for _bt_truncate()) can pass a NULL index tuple.  The scankey will
 *		be initialized as if an "all truncated" pivot tuple was passed
 *		instead.
 *
 *		Note that we may occasionally have to share lock the metapage to
 *		determine whether or not the keys in the index are expected to be
 *		unique (i.e. if this is a "heapkeyspace" index).  We assume a
 *		heapkeyspace index when caller passes a NULL tuple, allowing index
 *		build callers to avoid accessing the non-existent metapage.  We
 *		also assume that the index is _not_ allequalimage when a NULL tuple
 *		is passed; CREATE INDEX callers call _bt_allequalimage() to set the
 *		field themselves.
 */
BTScanInsert
_bt_mkscankey(Relation rel, IndexTuple itup)
{
	BTScanInsert key;
	ScanKey		skey;
	TupleDesc	itupdesc;
	int			indnkeyatts;
	int16	   *indoption;
	int			tupnatts;
	int			i;

	itupdesc = RelationGetDescr(rel);
	indnkeyatts = IndexRelationGetNumberOfKeyAttributes(rel);
	indoption = rel->rd_indoption;
	tupnatts = itup ? BTreeTupleGetNAtts(itup, rel) : 0;

	Assert(tupnatts <= IndexRelationGetNumberOfAttributes(rel));

	/*
	 * We'll execute search using scan key constructed on key columns.
	 * Truncated attributes and non-key attributes are omitted from the final
	 * scan key.
	 */
	key = palloc(offsetof(BTScanInsertData, scankeys) +
				 sizeof(ScanKeyData) * indnkeyatts);
	if (itup)
		_bt_metaversion(rel, &key->heapkeyspace, &key->allequalimage);
	else
	{
		/*
		 * XXX Continue to work with v4 indexes, while making sure new indexes are
		 * v3 indexes
		 */
		key->heapkeyspace = false;
		key->allequalimage = false;
	}
	key->anynullkeys = false;	/* initial assumption */
	key->nextkey = false;		/* usual case, required by btinsert */
	key->backward = false;		/* usual case, required by btinsert */
	key->keysz = Min(indnkeyatts, tupnatts);
	key->scantid = key->heapkeyspace && itup ?
		BTreeTupleGetHeapTID(itup) : NULL;
	skey = key->scankeys;
	for (i = 0; i < indnkeyatts; i++)
	{
		FmgrInfo   *procinfo;
		Datum		arg;
		bool		null;
		int			flags;

		/*
		 * We can use the cached (default) support procs since no cross-type
		 * comparison can be needed.
		 */
		procinfo = index_getprocinfo(rel, i + 1, BTORDER_PROC);

		/*
		 * Key arguments built from truncated attributes (or when caller
		 * provides no tuple) are defensively represented as NULL values. They
		 * should never be used.
		 */
		if (i < tupnatts)
			arg = index_getattr(itup, i + 1, itupdesc, &null);
		else
		{
			arg = (Datum) 0;
			null = true;
		}
		flags = (null ? SK_ISNULL : 0) | (indoption[i] << SK_BT_INDOPTION_SHIFT);
		ScanKeyEntryInitializeWithInfo(&skey[i],
									   flags,
									   (AttrNumber) (i + 1),
									   InvalidStrategy,
									   InvalidOid,
									   rel->rd_indcollation[i],
									   procinfo,
									   arg);
		/* Record if any key attribute is NULL (or truncated) */
		if (null)
			key->anynullkeys = true;
	}

	/*
	 * In NULLS NOT DISTINCT mode, we pretend that there are no null keys, so
	 * that full uniqueness check is done.
	 */
	if (rel->rd_index->indnullsnotdistinct)
		key->anynullkeys = false;

	return key;
}

/*
 * free a retracement stack made by _bt_search.
 */
void
_bt_freestack(BTStack stack)
{
	BTStack		ostack;

	while (stack != NULL)
	{
		ostack = stack;
		stack = stack->bts_parent;
		pfree(ostack);
	}
}


/*
 *	_bt_preprocess_array_keys() -- Preprocess SK_SEARCHARRAY scan keys
 *
 * If there are any SK_SEARCHARRAY scan keys, deconstruct the array(s) and
 * set up BTArrayKeyInfo info for each one that is an equality-type key.
 * Prepare modified scan keys in so->arrayKeyData, which will hold the current
 * array elements.
 *
 * _bt_preprocess_keys treats each primitive scan as an independent piece of
 * work.  We perform all preprocessing that must work "across array keys".
 * This division of labor makes sense once you consider that we're called only
 * once per btrescan, whereas _bt_preprocess_keys is called once per primitive
 * index scan.
 *
 * Currently we perform two kinds of preprocessing to deal with redundancies.
 * For inequality array keys, it's sufficient to find the extreme element
 * value and replace the whole array with that scalar value.  This eliminates
 * all but one array key as redundant.  Similarly, we are capable of "merging
 * together" multiple equality array keys (from two or more input scan keys)
 * into a single output scan key that contains only the intersecting array
 * elements.  This can eliminate many redundant array elements, as well as
 * eliminating whole array scan keys as redundant.  It can also allow us to
 * detect contradictory quals early.
 *
 * Note: _bt_start_array_keys actually sets up the cur_elem counters later on,
 * once the scan direction is known.
 *
 * Note: the reason we need so->arrayKeyData, rather than just scribbling
 * on scan->keyData, is that callers are permitted to call btrescan without
 * supplying a new set of scankey data.
 *
 * Note: _bt_preprocess_keys is responsible for creating the so->keyData scan
 * keys used by _bt_checkkeys.  Index scans that don't use equality array keys
 * will have _bt_preprocess_keys treat scan->keyData as input and so->keyData
 * as output.  Scans that use equality array keys have _bt_preprocess_keys
 * treat so->arrayKeyData (which is our output) as their input, while (as per
 * usual) outputting so->keyData for _bt_checkkeys.  This function adds an
 * additional layer of indirection that allows _bt_preprocess_keys to avoid
 * dealing with SK_SEARCHARRAY directly.  (Actually, _bt_preprocess_keys knows
 * that it must not eliminate "redundant" scan keys on the basis of what are
 * actually just the current array elements.)
 */
void
_bt_preprocess_array_keys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	Relation	rel = scan->indexRelation;
	int			numberOfKeys = scan->numberOfKeys;
	int16	   *indoption = rel->rd_indoption;
	int			numArrayKeys;
	int			prevArrayAtt = -1;
	Oid			prevElemtype = InvalidOid;
	ScanKey		cur;
	int			i;
	MemoryContext oldContext;

	/* Quick check to see if there are any array keys */
	numArrayKeys = 0;
	for (i = 0; i < numberOfKeys; i++)
	{
		cur = &scan->keyData[i];
		if (cur->sk_flags & SK_SEARCHARRAY)
		{
			numArrayKeys++;
			Assert(!(cur->sk_flags & (SK_ROW_HEADER | SK_SEARCHNULL | SK_SEARCHNOTNULL)));
			/* If any arrays are null as a whole, we can quit right now. */
			if (cur->sk_flags & SK_ISNULL)
			{
				so->numArrayKeys = -1;
				so->arrayKeyData = NULL;
				return;
			}
		}
	}

	/* Quit if nothing to do. */
	if (numArrayKeys == 0)
	{
		so->numArrayKeys = 0;
		so->arrayKeyData = NULL;
		return;
	}

	/*
	 * Make a scan-lifespan context to hold array-associated data, or reset it
	 * if we already have one from a previous rescan cycle.
	 */
	if (so->arrayContext == NULL)
		so->arrayContext = AllocSetContextCreate(CurrentMemoryContext,
												 "BTree array context",
												 ALLOCSET_SMALL_SIZES);
	else
		MemoryContextReset(so->arrayContext);

	oldContext = MemoryContextSwitchTo(so->arrayContext);

	/* Create modifiable copy of scan->keyData in the workspace context */
	so->arrayKeyData = (ScanKey) palloc(numberOfKeys * sizeof(ScanKeyData));
	memcpy(so->arrayKeyData, scan->keyData, numberOfKeys * sizeof(ScanKeyData));

	/* Allocate space for per-array data in the workspace context */
	so->arrayKeys = (BTArrayKeyInfo *) palloc(numArrayKeys * sizeof(BTArrayKeyInfo));
	so->advanceDir = NoMovementScanDirection;

	/* Allocate space for ORDER procs that we'll use to advance the arrays */
	so->orderProcs = (FmgrInfo *) palloc(numberOfKeys * sizeof(FmgrInfo));
	so->orderProcsMap = (int *) palloc(numberOfKeys * sizeof(int));

	/* Now process each array key */
	numArrayKeys = 0;
	for (i = 0; i < numberOfKeys; i++)
	{
		FmgrInfo	sortproc;
		FmgrInfo   *sortprocp = &sortproc;
		bool		reverse;
		Oid			elemtype;
		ArrayType  *arrayval;
		int16		elmlen;
		bool		elmbyval;
		char		elmalign;
		int			num_elems;
		Datum	   *elem_values;
		bool	   *elem_nulls;
		int			num_nonnulls;
		int			j;

		cur = &so->arrayKeyData[i];
		reverse = (indoption[cur->sk_attno - 1] & INDOPTION_DESC) != 0;

		/*
		 * Determine the nominal datatype of the array elements.  We have to
		 * support the convention that sk_subtype == InvalidOid means the
		 * opclass input type; this is a hack to simplify life for
		 * ScanKeyInit().
		 */
		elemtype = cur->sk_subtype;
		if (elemtype == InvalidOid)
			elemtype = rel->rd_opcintype[cur->sk_attno - 1];

		/*
		 * Attributes with equality-type scan keys (including but not limited
		 * to array scan keys) will need a 3-way ORDER proc to perform binary
		 * searches for the next matching array element.  Set that up now.
		 *
		 * Array scan keys with cross-type equality operators will require a
		 * separate same-type ORDER proc for sorting their array.  Otherwise,
		 * sortproc just points to the same proc used during binary searches.
		 */
		if (cur->sk_strategy == BTEqualStrategyNumber)
			_bt_setup_array_cmp(scan, cur, elemtype,
								&so->orderProcs[i], &sortprocp);

		if (!(cur->sk_flags & SK_SEARCHARRAY))
			continue;

		/*
		 * First, deconstruct the array into elements.  Anything allocated
		 * here (including a possibly detoasted array value) is in the
		 * workspace context.
		 */
		arrayval = DatumGetArrayTypeP(cur->sk_argument);
		/* We could cache this data, but not clear it's worth it */
		get_typlenbyvalalign(ARR_ELEMTYPE(arrayval),
							 &elmlen, &elmbyval, &elmalign);
		deconstruct_array(arrayval,
						  ARR_ELEMTYPE(arrayval),
						  elmlen, elmbyval, elmalign,
						  &elem_values, &elem_nulls, &num_elems);

		/*
		 * Compress out any null elements.  We can ignore them since we assume
		 * all btree operators are strict.
		 */
		num_nonnulls = 0;
		for (j = 0; j < num_elems; j++)
		{
			if (!elem_nulls[j])
				elem_values[num_nonnulls++] = elem_values[j];
		}

		/* We could pfree(elem_nulls) now, but not worth the cycles */

		/* If there's no non-nulls, the scan qual is unsatisfiable */
		if (num_nonnulls == 0)
		{
			numArrayKeys = -1;
			break;
		}

		/*
		 * If the comparison operator is not equality, then the array qual
		 * degenerates to a simple comparison against the smallest or largest
		 * non-null array element, as appropriate.
		 */
		switch (cur->sk_strategy)
		{
			case BTLessStrategyNumber:
			case BTLessEqualStrategyNumber:
				cur->sk_argument =
					_bt_find_extreme_element(scan, cur, elemtype,
											 BTGreaterStrategyNumber,
											 elem_values, num_nonnulls);
				continue;
			case BTEqualStrategyNumber:
				/* proceed with rest of loop */
				break;
			case BTGreaterEqualStrategyNumber:
			case BTGreaterStrategyNumber:
				cur->sk_argument =
					_bt_find_extreme_element(scan, cur, elemtype,
											 BTLessStrategyNumber,
											 elem_values, num_nonnulls);
				continue;
			default:
				elog(ERROR, "unrecognized StrategyNumber: %d",
					 (int) cur->sk_strategy);
				break;
		}

		/*
		 * Sort the non-null elements and eliminate any duplicates.  We must
		 * sort in the same ordering used by the index column, so that the
		 * arrays can be advanced in lockstep with the scan's progress through
		 * the index's key space.
		 */
		Assert(cur->sk_strategy == BTEqualStrategyNumber);
		num_elems = _bt_sort_array_elements(cur, sortprocp, reverse,
											elem_values, num_nonnulls);

		/*
		 * If this scan key is semantically equivalent to a previous equality
		 * operator array scan key, merge the two arrays together to eliminate
		 * redundant non-intersecting elements (and whole scan keys).
		 *
		 * _bt_preprocess_keys is subject to restrictions on eliminating array
		 * scankeys as redundant: they can't be assumed redundant, since we
		 * must always keep around a scan key in so->keyData for use with any
		 * later elements from the same array.  Detecting redundant array
		 * elements here should more than make up for those restrictions.
		 *
		 * We don't support merging arrays (for same-attribute scankeys) when
		 * the array element types don't match.  This is orthogonal to whether
		 * or not cross-type operators happen to be in use, so the restriction
		 * shouldn't come up all that often.  (Note that there are no special
		 * restrictions on _bt_preprocess_keys's detection of _contradictory_
		 * array quals, which is generally the case that matters most of all.)
		 */
		if (prevArrayAtt == cur->sk_attno && prevElemtype == elemtype)
		{
			BTArrayKeyInfo *prev = &so->arrayKeys[numArrayKeys - 1];

			Assert(so->arrayKeyData[prev->scan_key].sk_attno == cur->sk_attno);
			Assert(so->arrayKeyData[prev->scan_key].sk_func.fn_oid ==
				   cur->sk_func.fn_oid);
			Assert(so->arrayKeyData[prev->scan_key].sk_collation ==
				   cur->sk_collation);

			num_elems = _bt_merge_arrays(cur, sortprocp, reverse,
										 prev->elem_values, prev->num_elems,
										 elem_values, num_elems);

			pfree(elem_values);

			/*
			 * If there are no intersecting elements left from merging this
			 * array into the previous array on the same attribute, the scan
			 * qual is unsatisfiable
			 */
			if (num_elems == 0)
			{
				numArrayKeys = -1;
				break;
			}

			/*
			 * Lower the number of elements from the previous array, and mark
			 * this scan key/array as redundant for every primitive index scan
			 */
			prev->num_elems = num_elems;
			cur->sk_flags |= SK_BT_RDDNARRAY;
			continue;
		}

		/*
		 * And set up the BTArrayKeyInfo data.
		 */
		so->arrayKeys[numArrayKeys].scan_key = i;
		so->arrayKeys[numArrayKeys].num_elems = num_elems;
		so->arrayKeys[numArrayKeys].elem_values = elem_values;
		numArrayKeys++;
		prevArrayAtt = cur->sk_attno;
		prevElemtype = elemtype;
	}

	so->numArrayKeys = numArrayKeys;

	MemoryContextSwitchTo(oldContext);
}

/*
 * _bt_setup_array_cmp() -- Set up array comparison functions
 *
 * Sets ORDER proc in caller's orderproc argument, which is used during binary
 * searches of arrays during the index scan.  Also sets a same-type ORDER proc
 * in caller's *sortprocp argument.
 *
 * Caller should pass an orderproc pointing to space that'll store the ORDER
 * proc for the scan, and a *sortprocp pointing to its own separate space.
 *
 * In the common case where we don't need to deal with cross-type operators,
 * only one ORDER proc is actually required by caller.  We'll set *sortprocp
 * to point to the same memory that caller's orderproc continues to point to.
 * Otherwise, *sortprocp will continue to point to separate memory, which
 * we'll initialize separately (with an "(elemtype, elemtype)" ORDER proc that
 * can be used to sort arrays).
 *
 * Array preprocessing calls here with all equality strategy scan keys,
 * including any that don't use an array at all.  See _bt_advance_array_keys
 * for an explanation of why we need to treat these as degenerate single-value
 * arrays when the scan advances its array state machine.
 */
static void
_bt_setup_array_cmp(IndexScanDesc scan, ScanKey skey, Oid elemtype,
					FmgrInfo *orderproc, FmgrInfo **sortprocp)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	Relation	rel = scan->indexRelation;
	RegProcedure cmp_proc;
	Oid			opclasstype = rel->rd_opcintype[skey->sk_attno - 1];

	Assert(skey->sk_strategy == BTEqualStrategyNumber);
	Assert(OidIsValid(elemtype));

	/*
	 * Look up the appropriate comparison function in the opfamily.  This must
	 * use the opclass type as its left hand arg type, and the array element
	 * as its right hand arg type (since binary searches search for the array
	 * value that best matches the next on-disk index tuple for the scan).
	 *
	 * Note: it's possible that this would fail, if the opfamily lacks the
	 * required cross-type ORDER proc.  But this is no different to the case
	 * where _bt_first fails to find an ORDER proc for its insertion scan key.
	 */
	cmp_proc = get_opfamily_proc(rel->rd_opfamily[skey->sk_attno - 1],
								 opclasstype, elemtype, BTORDER_PROC);
	if (!RegProcedureIsValid(cmp_proc))
		elog(ERROR, "missing support function %d(%u,%u) for attribute %d of index \"%s\"",
			 BTORDER_PROC, opclasstype, elemtype,
			 skey->sk_attno, RelationGetRelationName(rel));

	/* Set ORDER proc for caller */
	fmgr_info_cxt(cmp_proc, orderproc, so->arrayContext);

	if (opclasstype == elemtype || !(skey->sk_flags & SK_SEARCHARRAY))
	{
		/*
		 * A second opfamily support proc lookup can be avoided in the common
		 * case where the ORDER proc used for the scan's binary searches uses
		 * the opclass/on-disk datatype for both its left and right arguments.
		 *
		 * Also avoid a separate lookup whenever scan key lacks an array.
		 * There is nothing for caller to sort anyway, but be consistent.
		 */
		*sortprocp = orderproc;
		return;
	}

	/*
	 * Look up the appropriate same-type comparison function in the opfamily.
	 *
	 * Note: it's possible that this would fail, if the opfamily is
	 * incomplete, but it seems quite unlikely that an opfamily would omit
	 * non-cross-type support functions for any datatype that it supports at
	 * all.
	 */
	cmp_proc = get_opfamily_proc(rel->rd_opfamily[skey->sk_attno - 1],
								 elemtype, elemtype, BTORDER_PROC);
	if (!RegProcedureIsValid(cmp_proc))
		elog(ERROR, "missing support function %d(%u,%u) for attribute %d of index \"%s\"",
			 BTORDER_PROC, elemtype, elemtype,
			 skey->sk_attno, RelationGetRelationName(rel));

	/* Set same-type ORDER proc for caller */
	fmgr_info_cxt(cmp_proc, *sortprocp, so->arrayContext);
}

/*
 * _bt_find_extreme_element() -- get least or greatest array element
 *
 * scan and skey identify the index column, whose opfamily determines the
 * comparison semantics.  strat should be BTLessStrategyNumber to get the
 * least element, or BTGreaterStrategyNumber to get the greatest.
 */
static Datum
_bt_find_extreme_element(IndexScanDesc scan, ScanKey skey, Oid elemtype,
						 StrategyNumber strat,
						 Datum *elems, int nelems)
{
	Relation	rel = scan->indexRelation;
	Oid			cmp_op;
	RegProcedure cmp_proc;
	FmgrInfo	flinfo;
	Datum		result;
	int			i;

	/*
	 * Look up the appropriate comparison operator in the opfamily.
	 *
	 * Note: it's possible that this would fail, if the opfamily is
	 * incomplete, but it seems quite unlikely that an opfamily would omit
	 * non-cross-type comparison operators for any datatype that it supports
	 * at all.
	 */
	Assert(skey->sk_strategy != BTEqualStrategyNumber);
	Assert(OidIsValid(elemtype));
	cmp_op = get_opfamily_member(rel->rd_opfamily[skey->sk_attno - 1],
								 elemtype,
								 elemtype,
								 strat);
	if (!OidIsValid(cmp_op))
		elog(ERROR, "missing operator %d(%u,%u) in opfamily %u",
			 strat, elemtype, elemtype,
			 rel->rd_opfamily[skey->sk_attno - 1]);
	cmp_proc = get_opcode(cmp_op);
	if (!RegProcedureIsValid(cmp_proc))
		elog(ERROR, "missing oprcode for operator %u", cmp_op);

	fmgr_info(cmp_proc, &flinfo);

	Assert(nelems > 0);
	result = elems[0];
	for (i = 1; i < nelems; i++)
	{
		if (DatumGetBool(FunctionCall2Coll(&flinfo,
										   skey->sk_collation,
										   elems[i],
										   result)))
			result = elems[i];
	}

	return result;
}

/*
 * _bt_sort_array_elements() -- sort and de-dup array elements
 *
 * The array elements are sorted in-place, and the new number of elements
 * after duplicate removal is returned.
 *
 * skey identifies the index column whose opfamily determines the comparison
 * semantics, and sortproc is a corresponding ORDER proc.  If reverse is true,
 * we sort in descending order.
 *
 * Note: sortproc arg must be an ORDER proc suitable for sorting: it must
 * compare arguments that are both of the same type as the array elements
 * being sorted (even during scans that perform binary searches against the
 * arrays using distinct cross-type ORDER procs).
 */
static int
_bt_sort_array_elements(ScanKey skey, FmgrInfo *sortproc, bool reverse,
						Datum *elems, int nelems)
{
	BTSortArrayContext cxt;

	if (nelems <= 1)
		return nelems;			/* no work to do */

	/* Sort the array elements */
	cxt.sortproc = sortproc;
	cxt.collation = skey->sk_collation;
	cxt.reverse = reverse;
	qsort_arg(elems, nelems, sizeof(Datum),
			  _bt_compare_array_elements, &cxt);

	/* Now scan the sorted elements and remove duplicates */
	return qunique_arg(elems, nelems, sizeof(Datum),
					   _bt_compare_array_elements, &cxt);
}

/*
 * _bt_merge_arrays() -- merge together duplicate array keys
 *
 * Both scan keys have array elements that have already been sorted and
 * deduplicated.
 */
static int
_bt_merge_arrays(ScanKey skey, FmgrInfo *sortproc, bool reverse,
				 Datum *elems_orig, int nelems_orig,
				 Datum *elems_next, int nelems_next)
{
	BTSortArrayContext cxt;
	Datum	   *merged = palloc(sizeof(Datum) * Min(nelems_orig, nelems_next));
	int			merged_nelems = 0;

	/*
	 * Incrementally copy the original array into a temp buffer, skipping over
	 * any items that are missing from the "next" array
	 */
	cxt.sortproc = sortproc;
	cxt.collation = skey->sk_collation;
	cxt.reverse = reverse;
	for (int i = 0; i < nelems_orig; i++)
	{
		Datum	   *elem = elems_orig + i;

		if (bsearch_arg(elem, elems_next, nelems_next, sizeof(Datum),
						_bt_compare_array_elements, &cxt))
			merged[merged_nelems++] = *elem;
	}

	/*
	 * Overwrite the original array with temp buffer so that we're only left
	 * with intersecting array elements
	 */
	memcpy(elems_orig, merged, merged_nelems * sizeof(Datum));
	pfree(merged);

	return merged_nelems;
}

/*
 * qsort_arg comparator for sorting array elements
 */
static int
_bt_compare_array_elements(const void *a, const void *b, void *arg)
{
	Datum		da = *((const Datum *) a);
	Datum		db = *((const Datum *) b);
	BTSortArrayContext *cxt = (BTSortArrayContext *) arg;
	int32		compare;

	compare = DatumGetInt32(FunctionCall2Coll(cxt->sortproc,
											  cxt->collation,
											  da, db));
	if (cxt->reverse)
		INVERT_COMPARE_RESULT(compare);
	return compare;
}

/*
 * _bt_compare_array_skey() -- apply array comparison function
 *
 * Compares caller's tuple attribute value to a scan key/array element.
 * Helper function used during binary searches of SK_SEARCHARRAY arrays.
 *
 *		This routine returns:
 *			<0 if tupdatum < arrdatum;
 *			 0 if tupdatum == arrdatum;
 *			>0 if tupdatum > arrdatum.
 *
 * This is essentially the same interface as _bt_compare: both functions
 * compare the value that they're searching for to a binary search pivot.
 * However, unlike _bt_compare, this function's "tuple argument" comes first,
 * while its "array/scankey argument" comes second.
*/
static inline int32
_bt_compare_array_skey(FmgrInfo *orderproc,
					   Datum tupdatum, bool tupnull,
					   Datum arrdatum, ScanKey cur)
{
	int32		result = 0;

	Assert(cur->sk_strategy == BTEqualStrategyNumber);

	if (tupnull)				/* NULL tupdatum */
	{
		if (cur->sk_flags & SK_ISNULL)
			result = 0;			/* NULL "=" NULL */
		else if (cur->sk_flags & SK_BT_NULLS_FIRST)
			result = -1;		/* NULL "<" NOT_NULL */
		else
			result = 1;			/* NULL ">" NOT_NULL */
	}
	else if (cur->sk_flags & SK_ISNULL) /* NOT_NULL tupdatum, NULL arrdatum */
	{
		if (cur->sk_flags & SK_BT_NULLS_FIRST)
			result = 1;			/* NOT_NULL ">" NULL */
		else
			result = -1;		/* NOT_NULL "<" NULL */
	}
	else
	{
		/*
		 * Like _bt_compare, we need to be careful of cross-type comparisons,
		 * so the left value has to be the value that came from an index tuple
		 */
		result = DatumGetInt32(FunctionCall2Coll(orderproc, cur->sk_collation,
												 tupdatum, arrdatum));

		/*
		 * We flip the sign by following the obvious rule: flip whenever the
		 * column is a DESC column.
		 *
		 * _bt_compare does it the wrong way around (flip when *ASC*) in order
		 * to compensate for passing its orderproc arguments backwards.  We
		 * don't need to play these games because we find it natural to pass
		 * tupdatum as the left value (and arrdatum as the right value).
		 */
		if (cur->sk_flags & SK_BT_DESC)
			INVERT_COMPARE_RESULT(result);
	}

	return result;
}

/*
 * _bt_binsrch_array_skey() -- Binary search for next matching array key
 *
 * Returns an index to the first array element >= caller's tupdatum argument.
 * This convention is more natural for forwards scan callers, but that can't
 * really matter to backwards scan callers.  Both callers require handling for
 * the case where the match we return is < tupdatum, and symmetric handling
 * for the case where our best match is > tupdatum.
 *
 * Also sets *set_elem_result to whatever _bt_compare_array_skey returned when
 * we compared the returned array element to caller's tupdatum argument.  This
 * helps our caller to determine how advancing its array (to the element we'll
 * return an offset to) might need to carry to higher order arrays.
 *
 * cur_elem_start indicates if the binary search should begin at the array's
 * current element (or have the current element as an upper bound for backward
 * scans).  It's safe for searches against required scan key arrays to reuse
 * earlier search bounds like this because such arrays always advance in
 * lockstep with the index scan's progress through the index's key space.
 */
static int
_bt_binsrch_array_skey(FmgrInfo *orderproc,
					   bool cur_elem_start, ScanDirection dir,
					   Datum tupdatum, bool tupnull,
					   BTArrayKeyInfo *array, ScanKey cur,
					   int32 *set_elem_result)
{
	int			low_elem = 0,
				mid_elem = -1,
				high_elem = array->num_elems - 1,
				result = 0;

	Assert(cur->sk_flags & SK_SEARCHARRAY);
	Assert(cur->sk_strategy == BTEqualStrategyNumber);

	if (cur_elem_start)
	{
		if (ScanDirectionIsForward(dir))
			low_elem = array->cur_elem;
		else
			high_elem = array->cur_elem;
	}

	while (high_elem > low_elem)
	{
		Datum		arrdatum;

		mid_elem = low_elem + ((high_elem - low_elem) / 2);
		arrdatum = array->elem_values[mid_elem];

		result = _bt_compare_array_skey(orderproc, tupdatum, tupnull,
										arrdatum, cur);

		if (result == 0)
		{
			/*
			 * It's safe to quit as soon as we see an equal array element.
			 * This often saves an extra comparison or two...
			 */
			low_elem = mid_elem;
			break;
		}

		if (result > 0)
			low_elem = mid_elem + 1;
		else
			high_elem = mid_elem;
	}

	/*
	 * ...but our caller also cares about how its searched-for tuple datum
	 * compares to the low_elem datum.  Must always set *set_elem_result with
	 * the result of that comparison specifically.
	 */
	if (low_elem != mid_elem)
		result = _bt_compare_array_skey(orderproc, tupdatum, tupnull,
										array->elem_values[low_elem], cur);

	*set_elem_result = result;

	return low_elem;
}

/*
 * _bt_start_array_keys() -- Initialize array keys at start of a scan
 *
 * Set up the cur_elem counters and fill in the first sk_argument value for
 * each array scankey.  We can't do this until we know the scan direction.
 */
void
_bt_start_array_keys(IndexScanDesc scan, ScanDirection dir)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	int			i;

	for (i = 0; i < so->numArrayKeys; i++)
	{
		BTArrayKeyInfo *curArrayKey = &so->arrayKeys[i];
		ScanKey		skey = &so->arrayKeyData[curArrayKey->scan_key];

		Assert(curArrayKey->num_elems > 0);
		if (ScanDirectionIsBackward(dir))
			curArrayKey->cur_elem = curArrayKey->num_elems - 1;
		else
			curArrayKey->cur_elem = 0;
		skey->sk_argument = curArrayKey->elem_values[curArrayKey->cur_elem];
	}

	so->advanceDir = dir;
}

/*
 * _bt_advance_array_keys_increment() -- Advance to next set of array elements
 *
 * Advances the array keys by a single increment in the current scan
 * direction.  When there are multiple array keys this can roll over from the
 * lowest order array to higher order arrays.
 *
 * Returns true if there is another set of values to consider, false if not.
 * On true result, the scankeys are initialized with the next set of values.
 * On false result, the scankeys stay the same, and the array keys are not
 * advanced (every array remains at its final element for scan direction).
 *
 * Note: routine only sets so->arrayKeyData[] "input" scankeys to incremented
 * element values.  It will not set the same values in the scan's search-type
 * so->keyData[] "output" scan keys.  _bt_update_keys_with_arraykeys needs to
 * be called to actually change the qual used by _bt_checkkeys to decide which
 * tuples it should return (new primitive index scans call _bt_preprocess_keys
 * instead, which builds a whole new set of output keys from scratch).
 */
static bool
_bt_advance_array_keys_increment(IndexScanDesc scan, ScanDirection dir)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	bool		found = false;

	Assert(!so->needPrimScan);

	/*
	 * We must advance the last array key most quickly, since it will
	 * correspond to the lowest-order index column among the available
	 * qualifications.  Rolling over like this is necessary to ensure correct
	 * ordering of output when there are multiple array keys.
	 */
	for (int i = so->numArrayKeys - 1; i >= 0; i--)
	{
		BTArrayKeyInfo *curArrayKey = &so->arrayKeys[i];
		ScanKey		skey = &so->arrayKeyData[curArrayKey->scan_key];
		int			cur_elem = curArrayKey->cur_elem;
		int			num_elems = curArrayKey->num_elems;

		if (ScanDirectionIsBackward(dir))
		{
			if (--cur_elem < 0)
			{
				cur_elem = num_elems - 1;
				found = false;	/* need to advance next array key */
			}
			else
				found = true;
		}
		else
		{
			if (++cur_elem >= num_elems)
			{
				cur_elem = 0;
				found = false;	/* need to advance next array key */
			}
			else
				found = true;
		}

		curArrayKey->cur_elem = cur_elem;
		skey->sk_argument = curArrayKey->elem_values[cur_elem];
		if (found)
			break;
	}

	if (found)
		return true;

	/*
	 * Don't allow the entire set of array keys to roll over: restore the
	 * array keys to the state they were in just before we were called.
	 *
	 * This ensures that the array keys only ratchet forward (or backwards in
	 * the case of backward scans).  Our "so->arrayKeyData[]" scan keys should
	 * always match the current "so->keyData[]" search-type scan keys (except
	 * for a brief moment during array key advancement).
	 */
	for (int i = 0; i < so->numArrayKeys; i++)
	{
		BTArrayKeyInfo *rollarray = &so->arrayKeys[i];
		ScanKey		skey = &so->arrayKeyData[rollarray->scan_key];

		if (ScanDirectionIsBackward(dir))
			rollarray->cur_elem = 0;
		else
			rollarray->cur_elem = rollarray->num_elems - 1;
		skey->sk_argument = rollarray->elem_values[rollarray->cur_elem];
	}

	return false;
}

/*
 * _bt_rewind_array_keys() -- Handle array keys during btrestrpos
 *
 * Restore the array keys to the start of the key space for the current scan
 * direction as of the last time the arrays advanced.
 *
 * Once the scan reaches _bt_advance_array_keys, the arrays will advance up to
 * the key space of the actual tuples from the mark position's leaf page.
 */
void
_bt_rewind_array_keys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	bool		changed = false;

	Assert(so->numArrayKeys > 0);
	Assert(so->advanceDir != NoMovementScanDirection);

	for (int i = 0; i < so->numArrayKeys; i++)
	{
		BTArrayKeyInfo *curArrayKey = &so->arrayKeys[i];
		ScanKey		skey = &so->arrayKeyData[curArrayKey->scan_key];
		int			first_elem_dir;

		if (ScanDirectionIsForward(so->advanceDir))
			first_elem_dir = 0;
		else
			first_elem_dir = curArrayKey->num_elems - 1;

		if (curArrayKey->cur_elem != first_elem_dir)
		{
			curArrayKey->cur_elem = first_elem_dir;
			skey->sk_argument = curArrayKey->elem_values[first_elem_dir];
			changed = true;
		}
	}

	if (changed)
		_bt_update_keys_with_arraykeys(scan);

	Assert(_bt_verify_keys_with_arraykeys(scan));

	/*
	 * Invert the scan direction as of the last time the array keys advanced.
	 *
	 * In the common case where the scan direction hasn't changed, this won't
	 * affect the behavior of the scan at all -- advanceDir will be reset to
	 * the current scan direction in the next call to _bt_advance_array_keys.
	 *
	 * This prevents _bt_steppage from fully trusting currPos.moreRight and
	 * currPos.moreLeft in cases where _bt_readpage/_bt_checkkeys don't get
	 * the opportunity to consider advancing the array keys as expected.
	 */
	if (ScanDirectionIsForward(so->advanceDir))
		so->advanceDir = BackwardScanDirection;
	else
		so->advanceDir = ForwardScanDirection;

	so->needPrimScan = false;	/* defensive */
}

/*
 * _bt_tuple_before_array_skeys() -- _bt_checkkeys array helper function
 *
 * Routine to determine if a continuescan=false tuple (set that way by an
 * initial call to _bt_check_compare) must advance the scan's array keys.
 * Only call here when _bt_check_compare already set continuescan=false.
 * _bt_checkkeys calls here (in scans with array equality scan keys) to deal
 * with _bt_check_compare's inability to distinguishing between the < and >
 * cases (it uses equality operator scan keys, not 3-way ORDER procs).
 *
 * We always compare the tuple using the current array keys.  "readpagetup"
 * indicates if tuple is the scan's current _bt_readpage-wise tuple, rather
 * than a finaltup precheck/an assertion.  (!readpagetup finaltup precheck
 * callers won't have actually called _bt_check_compare for finaltup before
 * calling here, but they only call here to determine if the beginning of
 * matches for the current set of array keys at least starts somewhere on the
 * scan's current leaf page.)
 *
 * Returns true when caller passes a tuple that is < the current set of array
 * keys for the most significant non-equal column/scan key (or > for backwards
 * scans).  This means that it isn't time to advance the array keys just yet
 * (during readpagetup calls).  Our readpagetup caller must then suppress its
 * initial _bt_check_compare call (by setting pstate.continuescan=true once
 * more), allowing the scan to move on to the next _bt_readpage-wise tuple.
 * (In the case of !readpagetup finaltup precheck callers, this just indicates
 * that the start of matches for the current set of required array keys isn't
 * even on this page.)
 *
 * Returns false when caller's tuple is >= the current array keys (or <=, in
 * the case of backwards scans).  This confirms that readpagetup caller's
 * _bt_check_compare set pstate.continuescan=false due to locating the true
 * end of matching tuples for current qual (not some point before the start of
 * matching tuples), which means it's time to advance any required array keys,
 * and consider if tuple is a match for the new post-array-advancement qual.
 * (In the case of !readpagetup finaltup precheck callers, this just indicates
 * that the start of matches for the current set of required array keys must
 * be somewhere on the scan's current page.  We don't consider the possible
 * influence of required-in-opposite-direction-only inequality scan keys on
 * the initial position that _bt_first would locate for the current qual.  It
 * is up to _bt_advance_array_keys to deal with that as a special case.)
 *
 * As an optimization, readpagetup callers pass a _bt_check_compare-set sktrig
 * value to indicate which scan key triggered _bt_checkkeys to recheck with us
 * (!readpagetup callers must always pass sktrig=0).  This allows us to avoid
 * wastefully checking earlier scan keys that _bt_check_compare already found
 * to be satisfied by the current qual/set of array keys.
 */
static bool
_bt_tuple_before_array_skeys(IndexScanDesc scan, ScanDirection dir,
							 IndexTuple tuple, bool readpagetup, int sktrig)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	Relation	rel = scan->indexRelation;
	TupleDesc	itupdesc = RelationGetDescr(rel);
	int			ntupatts = BTreeTupleGetNAtts(tuple, rel);

	Assert(so->numArrayKeys > 0);
	Assert(so->numberOfKeys > 0);
	Assert(!so->needPrimScan);
	Assert(sktrig == 0 || readpagetup);

	for (; sktrig < so->numberOfKeys; sktrig++)
	{
		ScanKey		cur = so->keyData + sktrig;
		FmgrInfo   *orderproc;
		Datum		tupdatum;
		bool		tupnull;
		int32		result;

		if (!(((cur->sk_flags & SK_BT_REQFWD) && ScanDirectionIsForward(dir)) ||
			  ((cur->sk_flags & SK_BT_REQBKWD) &&
			   ScanDirectionIsBackward(dir))))
		{
			/*
			 * Not required in current scan direction.
			 *
			 * Unlike _bt_check_compare and _bt_advance_array_keys, we never
			 * deal with non-required keys -- even when they happen to have
			 * arrays that might need to be advanced.
			 */
			continue;
		}

		/* readpagetup calls require one ORDER proc comparison (at most) */
		Assert(!readpagetup || cur == so->keyData + sktrig);

		/*
		 * Inequality strategy scan keys (that are required in current scan
		 * direction) aren't something that we deal with
		 */
		if (cur->sk_strategy != BTEqualStrategyNumber)
		{
			/*
			 * We must give up right away when this was caller's trigger scan
			 * key, to avoid confusing our assertions
			 */
			if (readpagetup)
				return false;

			continue;
		}

		if (cur->sk_attno > ntupatts)
		{
			Assert(!readpagetup);

			/*
			 * When we reach a high key's truncated attribute, assume that the
			 * tuple attribute's value is >= the scan's equality constraint
			 * scan keys, forcing another _bt_advance_array_keys call.
			 *
			 * You might wonder why we don't treat truncated attributes as
			 * having values < our equality constraints instead; we're not
			 * treating the truncated attributes as having -inf values here,
			 * which is how things are done in _bt_compare.
			 *
			 * We're often called during finaltup prechecks, where we help our
			 * caller to decide whether or not it should terminate the current
			 * primitive index scan.  Our behavior here implements a policy of
			 * being slightly optimistic about what will be found on the next
			 * page when the current primitive scan continues onto that page.
			 * (This is also closest to what _bt_check_compare does.)
			 */
			return false;
		}

		orderproc = &so->orderProcs[so->orderProcsMap[sktrig]];
		tupdatum = index_getattr(tuple, cur->sk_attno, itupdesc, &tupnull);

		result = _bt_compare_array_skey(orderproc, tupdatum, tupnull,
										cur->sk_argument, cur);

		/*
		 * Does this comparison indicate that caller must _not_ advance the
		 * scan's arrays just yet?
		 */
		if ((ScanDirectionIsForward(dir) && result < 0) ||
			(ScanDirectionIsBackward(dir) && result > 0))
			return true;

		/*
		 * Does this comparison indicate that caller should now advance the
		 * scan's arrays?
		 */
		if (readpagetup || result != 0)
		{
			Assert(result != 0);
			return false;
		}

		/*
		 * Inconclusive -- need to check later scan keys, too.
		 *
		 * This must be a finaltup precheck, or perhaps a call made from an
		 * assertion.
		 */
		Assert(result == 0);
		Assert(!readpagetup);
	}

	return false;
}

/*
 * _bt_array_keys_remain() -- start scheduled primitive index scan?
 *
 * Returns true if _bt_checkkeys scheduled another primitive index scan, just
 * as the last one ended.  Otherwise returns false, indicating that the array
 * keys are now fully exhausted.
 *
 * Only call here during scans with one or more equality type array scan keys.
 */
bool
_bt_array_keys_remain(IndexScanDesc scan, ScanDirection dir)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;

	Assert(so->numArrayKeys > 0);
	Assert(so->advanceDir == dir);

	/*
	 * Array keys are advanced within _bt_checkkeys when the scan reaches the
	 * leaf level (more precisely, they're advanced when the scan reaches the
	 * end of each distinct set of array elements).  This process avoids
	 * repeat access to leaf pages (across multiple primitive index scans) by
	 * advancing the scan's array keys when it allows the primitive index scan
	 * to find nearby matching tuples (or when it eliminates ranges of array
	 * key space that can't possibly be satisfied by any index tuple).
	 *
	 * _bt_checkkeys sets a simple flag variable to schedule another primitive
	 * index scan.  This tells us what to do.  We cannot rely on _bt_first
	 * always reaching _bt_checkkeys, though.  There are various cases where
	 * that won't happen.  For example, if the index is completely empty, then
	 * _bt_first won't get as far as calling _bt_readpage/_bt_checkkeys.
	 *
	 * We also don't expect _bt_checkkeys to be reached when searching for a
	 * non-existent value that happens to be higher than any existing value in
	 * the index.  No _bt_checkkeys are expected when _bt_readpage reads the
	 * rightmost page during such a scan -- even a _bt_checkkeys call against
	 * the high key won't happen.  There is an analogous issue for backwards
	 * scans that search for a value lower than all existing index tuples.
	 *
	 * We don't actually require special handling for these cases -- we don't
	 * need to be explicitly instructed to _not_ perform another primitive
	 * index scan.  This is correct for all of the cases we've listed so far,
	 * which all involve primitive index scans that access pages "near the
	 * boundaries of the key space" (the leftmost page, the rightmost page, or
	 * an imaginary empty leaf root page).  If _bt_checkkeys cannot be reached
	 * by a primitive index scan for one set of array keys, it follows that it
	 * also won't be reached for any later set of array keys...
	 */
	if (!so->qual_ok)
	{
		/*
		 * ...though there is one exception: _bt_first's _bt_preprocess_keys
		 * call can determine that the scan's input scan keys can never be
		 * satisfied.  That might be true for one set of array keys, but not
		 * the next set.
		 *
		 * Handle this by advancing the array keys incrementally ourselves.
		 * When this succeeds, start another primitive index scan.
		 */
		CHECK_FOR_INTERRUPTS();

		Assert(!so->needPrimScan);
		if (_bt_advance_array_keys_increment(scan, dir))
			return true;

		/* Array keys are now exhausted */
	}

	/*
	 * Has another primitive index scan been scheduled by _bt_checkkeys?
	 */
	if (so->needPrimScan)
	{
		/* Yes -- tell caller to call _bt_first once again */
		so->needPrimScan = false;
		if (scan->parallel_scan != NULL)
			_bt_parallel_next_primitive_scan(scan);

		return true;
	}

	/*
	 * No more primitive index scans.  Terminate the top-level scan.
	 */
	if (scan->parallel_scan != NULL)
		_bt_parallel_done(scan);

	return false;
}

/*
 * _bt_advance_array_keys() -- Advance array elements using a tuple
 *
 * Like _bt_check_compare, our return value indicates if tuple satisfied the
 * qual (specifically our new qual).  There must be a new qual whenever we're
 * called (unless the top-level scan terminates).  After we return, all later
 * calls to _bt_check_compare will also use the same new qual (a qual with the
 * newly advanced array key values that were set here by us).
 *
 * We'll also set pstate.continuescan for caller.  When this is set to false,
 * it usually just ends the ongoing primitive index scan (we'll have scheduled
 * another one in passing).  But when all required array keys were exhausted,
 * setting pstate.continuescan=false here ends the top-level index scan (since
 * no new primitive scan will have been scheduled).  Most calls here will have
 * us set pstate.continuescan=true, which just indicates that the scan should
 * proceed onto the next tuple (just like when _bt_check_compare does it).
 *
 * _bt_tuple_before_array_skeys is responsible for determining if the current
 * place in the scan is >= the current array keys.  Calling here before that
 * point will prematurely advance the array keys, leading to wrong query
 * results.
 *
 * We're responsible for ensuring that caller's tuple is <= current/newly
 * advanced required array keys once we return.  We try to find an exact
 * match, but failing that we'll advance the array keys to whatever set of
 * array elements comes next in the key space for the current scan direction.
 * Required array keys "ratchet forwards".  They can only advance as the scan
 * itself advances through the index/key space.
 *
 * (The invariants are the same for backwards scans, except that the operators
 * are flipped: just replace the precondition's >= operator with a <=, and the
 * postcondition's <= operator with with a >=.  In other words, just swap the
 * precondition with the postcondition.)
 *
 * We also deal with "advancing" non-required arrays here.  Sometimes that'll
 * be the sole reason for calling here.  These calls are the only exception to
 * the general rule about always advancing required array keys (since they're
 * the only case where we simply don't need to touch any required array, which
 * must already be satisfied by caller's tuple).  Calls triggered by any scan
 * key that's required in the current scan direction are strictly guaranteed
 * to advance the required array keys (or end the top-level scan), though.
 *
 * Note that we deal with non-array required equality strategy scan keys as
 * degenerate single element arrays here.  Obviously, they can never really
 * advance in the way that real arrays can, but they must still affect how we
 * advance real array scan keys (exactly like true array equality scan keys).
 * We have to keep around a 3-way ORDER proc for these (using the "=" operator
 * won't do), since in general whether the tuple is < or > _any_ unsatisfied
 * required equality key influences how the scan's real arrays must advance.
 *
 * Note also that we may sometimes need to advance the array keys when the
 * existing array keys are already an exact match for every corresponding
 * value from caller's tuple.  This is how we deal with inequalities that are
 * required in the current scan direction.  They can advance the array keys
 * here, even though they don't influence the initial positioning strategy
 * within _bt_first (only inequalities required in the _opposite_ direction to
 * the scan influence _bt_first in this way).  When sktrig corresponds to a
 * required _inequality_ scan key that wasn't satisfied by caller's tuple,
 * we'll still perform array key advancement (just like when sktrig is a
 * required non-array equality strategy scan key).
 *
 * The array keys will always advance to the maximum possible extent that we
 * can know to be safe based on caller's tuple alone (or else we'll end the
 * top-level scan) when the call here was triggered by any required scan key
 * (regardless of whether the scan key uses the equality strategy or not).
 * Our caller would probably not scan noticeably many extra tuples if we only
 * provided a weaker version of this guarantee, but we still prefer to be
 * absolute about it.  This helps make our contract simple but precise.
 */
static bool
_bt_advance_array_keys(IndexScanDesc scan, BTReadPageState *pstate,
					   IndexTuple tuple, int sktrig)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	Relation	rel = scan->indexRelation;
	ScanDirection dir = pstate->dir;
	TupleDesc	itupdesc = RelationGetDescr(rel);
	int			ikey,
				arrayidx = 0,
				ntupatts = BTreeTupleGetNAtts(tuple, rel);
	bool		arrays_advanced = false,
				arrays_exhausted,
				sktrigrequired = false,
				beyond_end_advance = false,
				foundRequiredOppositeDirOnly = false,
				all_required_or_array_satisfied = true,
				all_required_satisfied = true;

	/*
	 * Precondition state machine assertions
	 */
	Assert(!so->needPrimScan);
	Assert(_bt_verify_keys_with_arraykeys(scan));
	Assert(!_bt_tuple_before_array_skeys(scan, dir, tuple, false, 0));

	/*
	 * Iterate through the scan's search-type scankeys (so->keyData[]), and
	 * set input scan keys (so->arrayKeyData[]) to new array values
	 */
	for (ikey = 0; ikey < so->numberOfKeys; ikey++)
	{
		ScanKey		cur = so->keyData + ikey;
		FmgrInfo   *orderproc;
		BTArrayKeyInfo *array = NULL;
		ScanKey		skeyarray = NULL;
		int			attnum = cur->sk_attno;
		Datum		tupdatum;
		bool		requiredSameDir = false,
					requiredOppositeDirOnly = false,
					tupnull;
		int32		result;
		int			set_elem = 0;

		if (cur->sk_flags & SK_SEARCHARRAY &&
			cur->sk_strategy == BTEqualStrategyNumber)
		{
			/* Set up array state */
			Assert(arrayidx < so->numArrayKeys);
			array = &so->arrayKeys[arrayidx++];
			skeyarray = &so->arrayKeyData[array->scan_key];
			Assert(skeyarray->sk_attno == attnum);
		}

		/*
		 * Optimization: Skip over known-satisfied scan keys
		 */
		if (ikey < sktrig)
			continue;

		if (((cur->sk_flags & SK_BT_REQFWD) && ScanDirectionIsForward(dir)) ||
			((cur->sk_flags & SK_BT_REQBKWD) && ScanDirectionIsBackward(dir)))
			requiredSameDir = true;
		else if (((cur->sk_flags & SK_BT_REQFWD) && ScanDirectionIsBackward(dir)) ||
				 ((cur->sk_flags & SK_BT_REQBKWD) && ScanDirectionIsForward(dir)))
			requiredOppositeDirOnly = true;

		if (ikey == sktrig)
			sktrigrequired = requiredSameDir;

		/*
		 * When we come across an inequality scan key that's required in the
		 * opposite direction only, remember it in a flag variable for later.
		 * The flag helps with avoiding unnecessary finaltup checks later on.
		 */
		if (requiredOppositeDirOnly && sktrigrequired &&
			all_required_or_array_satisfied)
		{
			Assert(cur->sk_strategy != BTEqualStrategyNumber);
			Assert(all_required_satisfied);

			foundRequiredOppositeDirOnly = true;

			continue;
		}

		/*
		 * Other than that, we're not interested in scan keys that aren't
		 * required in the current scan direction (unless they're non-required
		 * array equality scan keys, which still need to be advanced by us)
		 */
		if (!requiredSameDir && !array)
			continue;

		/*
		 * Handle a required non-array scan key that the initial call to
		 * _bt_check_compare indicated triggered array advancement, if any.
		 *
		 * The non-array scan key's strategy will be <, <=, or = during a
		 * forwards scan (or any one of =, >=, or > during a backwards scan).
		 * It follows that the corresponding tuple attribute's value must now
		 * be either > or >= the scan key value (for backwards scans it must
		 * be either < or <= that value).
		 *
		 * If this is a required equality strategy scan key, this is just an
		 * optimization; _bt_tuple_before_array_skeys already confirmed that
		 * this scan key places us ahead of caller's tuple.  There's no need
		 * to repeat that work now. (We only do comparisons of any required
		 * non-array equality scan keys that come after the triggering key.)
		 *
		 * If this is a required inequality strategy scan key, we _must_ rely
		 * on _bt_check_compare like this; it knows all the intricacies around
		 * evaluating inequality strategy scan keys (e.g., row comparisons).
		 * There is no simple mapping onto the opclass ORDER proc we can use.
		 * But once we know that we have an unsatisfied inequality, we can
		 * treat it in the same way as an unsatisfied equality at this point.
		 *
		 * The arrays advance correctly in both cases because both involve the
		 * scan reaching the end of the key space for a higher order array key
		 * (or some distinct set of higher-order array keys, taken together).
		 * The only real difference is that in the equality case the end is
		 * "strictly at the end of an array key", whereas in the inequality
		 * case it's "within an array key".  Either way we'll increment higher
		 * order arrays by one increment (the next-highest array might need to
		 * roll over to the next-next highest array in turn, and so on).
		 *
		 * See below for a full explanation of "beyond end" advancement.
		 */
		if (ikey == sktrig && !array)
		{
			Assert(requiredSameDir);
			Assert(all_required_or_array_satisfied && all_required_satisfied);
			Assert(!arrays_advanced);

			beyond_end_advance = true;
			all_required_or_array_satisfied = all_required_satisfied = false;

			continue;
		}

		/*
		 * Nothing for us to do with a required inequality strategy scan key
		 * that wasn't the one that _bt_check_compare stopped on
		 */
		if (cur->sk_strategy != BTEqualStrategyNumber)
			continue;

		/*
		 * Here we perform steps for all array scan keys after a required
		 * array scan key whose binary search triggered "beyond end of array
		 * element" array advancement due to encountering a tuple attribute
		 * value > the closest matching array key (or < for backwards scans).
		 *
		 * See below for a full explanation of "beyond end" advancement.
		 */
		if (beyond_end_advance)
		{
			int			final_elem_dir;

			if (ScanDirectionIsBackward(dir) || !array)
				final_elem_dir = 0;
			else
				final_elem_dir = array->num_elems - 1;

			if (array && array->cur_elem != final_elem_dir)
			{
				array->cur_elem = final_elem_dir;
				skeyarray->sk_argument = array->elem_values[final_elem_dir];
				arrays_advanced = true;
			}

			continue;
		}

		/*
		 * Here we perform steps for all array scan keys after a required
		 * array scan key whose tuple attribute was < the closest matching
		 * array key when we dealt with it (or > for backwards scans).
		 *
		 * This earlier required array key already puts us ahead of caller's
		 * tuple in the key space (for the current scan direction).  We must
		 * make sure that subsequent lower-order array keys do not put us too
		 * far ahead (ahead of tuples that have yet to be seen by our caller).
		 * For example, when a tuple "(a, b) = (42, 5)" advances the array
		 * keys on "a" from 40 to 45, we must also set "b" to whatever the
		 * first array element for "b" is.  It would be wrong to allow "b" to
		 * be set based on the tuple value.
		 *
		 * Perform the same steps with truncated high key attributes.  You can
		 * think of this as a "binary search" for the element closest to the
		 * value -inf.  Again, the arrays must never get ahead of the scan.
		 */
		if (!all_required_or_array_satisfied || attnum > ntupatts)
		{
			int			first_elem_dir;

			if (ScanDirectionIsForward(dir) || !array)
				first_elem_dir = 0;
			else
				first_elem_dir = array->num_elems - 1;

			if (array && array->cur_elem != first_elem_dir)
			{
				array->cur_elem = first_elem_dir;
				skeyarray->sk_argument = array->elem_values[first_elem_dir];
				arrays_advanced = true;
			}

			/*
			 * If this is a truncated finaltup high key, we can avoid a
			 * useless _bt_check_compare recheck later on
			 */
			all_required_or_array_satisfied = false;

			/*
			 * Deliberately don't unset all_required_satisfied, so that when
			 * we encounter a truncated finaltup high key attribute we'll be
			 * optimistic about its corresponding required scan key being
			 * satisfied when we go on to check it against tuples from this
			 * page's right sibling leaf page.
			 *
			 * For example, when a finaltuple "(a, b) = (66, -inf)" advances
			 * the array keys on "a" from 45 to 66, we'll set "b" to whatever
			 * the first array element for "b" is.  all_required_satisfied
			 * won't be unset when we reach "b", so we won't go on to start a
			 * new primitive index scan once outside the loop.  We'll make the
			 * optimistic assumption that the current/finaltup page's right
			 * sibling page leaf page will be found to contain tuples >= our
			 * new post-finaltup array keys.
			 *
			 * There is a chance that we'll find that even the right sibling
			 * leaf page has a finaltup < our new array keys.  That means that
			 * our policy incurs a single extra leaf page, that could have
			 * been avoided by unsetting all_required_satisfied here instead.
			 * We're optimistic here because being pessimistic loses again and
			 * again with certain types of queries, whereas being optimistic
			 * can only lose when we reach a finaltuple that represents the
			 * boundary between two large, adjoining groups of tuples.
			 */
			continue;
		}

		/*
		 * Search in scankey's array for the corresponding tuple attribute
		 * value from caller's tuple
		 */
		orderproc = &so->orderProcs[so->orderProcsMap[ikey]];
		tupdatum = index_getattr(tuple, attnum, itupdesc, &tupnull);

		if (array)
		{
			bool		ratchets = (requiredSameDir && !arrays_advanced);

			/*
			 * Binary search for closest match that's available from the array
			 */
			set_elem = _bt_binsrch_array_skey(orderproc, ratchets, dir,
											  tupdatum, tupnull,
											  array, cur, &result);

			/*
			 * Required arrays only ever ratchet forwards (backwards).
			 *
			 * This condition makes it safe for binary searches to skip over
			 * array elements that the scan must already be ahead of by now.
			 * That is strictly an optimization.  Our assertion verifies that
			 * the condition holds, which doesn't depend on the optimization.
			 */
			Assert(!ratchets ||
				   ((ScanDirectionIsForward(dir) && set_elem >= array->cur_elem) ||
					(ScanDirectionIsBackward(dir) && set_elem <= array->cur_elem)));
			Assert(set_elem >= 0 && set_elem < array->num_elems);
		}
		else
		{
			Assert(requiredSameDir);

			/*
			 * This is a required non-array equality strategy scan key, which
			 * we'll treat as a degenerate single value array.
			 *
			 * This scan key's imaginary "array" can't really advance, but it
			 * can still roll over like any other array.  (Actually, this is
			 * no different to real single value arrays, which never advance
			 * without rolling over -- they can never truly advance, either.)
			 */
			result = _bt_compare_array_skey(orderproc, tupdatum, tupnull,
											cur->sk_argument, cur);
		}

		/*
		 * Consider "beyond end of array element" array advancement.
		 *
		 * When the tuple attribute value is > the closest matching array key
		 * (or < in the backwards scan case), we need to ratchet this array
		 * forward (backward) by one increment, so that caller's tuple ends up
		 * being < final array value instead (or > final array value instead).
		 * This process has to work for all of the arrays, not just this one:
		 * it must "carry" to higher-order arrays when the set_elem that we
		 * just found happens to be the final one for the scan's direction.
		 * Incrementing (decrementing) set_elem itself isn't good enough.
		 *
		 * Our approach is to provisionally use set_elem as if it was an exact
		 * match now, then set each later/less significant array to whatever
		 * its final element is.  Once outside the loop we'll then "increment
		 * this array's set_elem" by calling _bt_advance_array_keys_increment.
		 * That way the process rolls over to higher order arrays as needed.
		 *
		 * Under this scheme any required arrays only ever ratchet forwards
		 * (or backwards), and always do so to the maximum possible extent
		 * that we can know will be safe without seeing the scan's next tuple.
		 * We don't need any special handling for required scan keys that lack
		 * a real array to advance, nor for redundant scan keys that couldn't
		 * be eliminated by _bt_preprocess_keys.  It won't matter if some of
		 * our "true" array scan keys (or even all of them) are non-required.
		 */
		if (requiredSameDir &&
			((ScanDirectionIsForward(dir) && result > 0) ||
			 (ScanDirectionIsBackward(dir) && result < 0)))
			beyond_end_advance = true;

		/*
		 * Also track whether all relevant attributes from caller's tuple will
		 * be equal to the scan's array keys once we're done with it
		 */
		if (result != 0)
		{
			all_required_or_array_satisfied = false;
			if (requiredSameDir)
				all_required_satisfied = false;
		}

		/*
		 * Optimization: If this call was triggered by a non-required array,
		 * and we know that tuple won't satisfy the qual, we give up right
		 * away.  This often avoids advancing the array keys, which avoids
		 * wasting cycles on updates to unsatisfiable non-required arrays.
		 */
		if (!sktrigrequired && !all_required_or_array_satisfied)
			break;

		/* Advance array keys, even when set_elem isn't an exact match */
		if (array && array->cur_elem != set_elem)
		{
			array->cur_elem = set_elem;
			skeyarray->sk_argument = array->elem_values[set_elem];
			arrays_advanced = true;
		}
	}

	/*
	 * Consider if we need to advance the array keys incrementally to finish
	 * off "beyond end of array element" array advancement.  This is the only
	 * way that the array keys can be exhausted, which is the only way that
	 * the top-level index scan can be terminated here by us.
	 */
	arrays_exhausted = false;
	if (beyond_end_advance)
	{
		/* Non-required scan keys never exhaust arrays/end top-level scan */
		Assert(sktrigrequired && !all_required_satisfied);

		if (!_bt_advance_array_keys_increment(scan, dir))
			arrays_exhausted = true;
		else
			arrays_advanced = true;
	}

	if (arrays_advanced)
	{
		/*
		 * Finalize advancing the array keys by performing in-place updates to
		 * the associated array search-type scan keys that _bt_checkkeys uses
		 */
		_bt_update_keys_with_arraykeys(scan);
		so->advanceDir = dir;

		/*
		 * If any required array keys were advanced, be prepared to recheck
		 * the final tuple against the new array keys (as an optimization)
		 */
		if (sktrigrequired)
			pstate->finaltupchecked = false;
	}

	Assert(_bt_verify_keys_with_arraykeys(scan));
	if (arrays_exhausted)
	{
		Assert(sktrigrequired && !all_required_satisfied);

		/*
		 * End the top-level index scan
		 */
		pstate->continuescan = false;	/* Agree with _bt_check_compare */
		so->needPrimScan = false;	/* All array keys now processed */

		/* This tuple doesn't match any qual */
		return false;
	}

	/*
	 * Does caller's tuple now match the new qual?  Call _bt_check_compare a
	 * second time to find out (unless it's already clear that it can't).
	 */
	if (all_required_or_array_satisfied && arrays_advanced)
	{
		int			insktrig = sktrig + 1;

		Assert(all_required_satisfied);

		if (likely(_bt_check_compare(dir, so, tuple, ntupatts, itupdesc,
									 so->numArrayKeys, &pstate->continuescan,
									 &insktrig, false, false)))
			return true;

		/*
		 * Consider "second pass" handling of required inequalities.
		 *
		 * It's possible that our _bt_check_compare call indicated that the
		 * scan should be terminated due to an unsatisfied inequality that
		 * wasn't initially recognized as such by us.  Handle this by calling
		 * ourselves recursively while indicating that the trigger is now the
		 * inequality that we missed first time around.
		 *
		 * We must do this in order to honor our contract with caller.  We
		 * promise to always advance the array keys to the maximum possible
		 * extent that we can know to be safe based on caller's tuple alone.
		 * It probably wouldn't really matter if we just ignored this case
		 * (the very next tuple could advance the array keys instead), but
		 * handling this precisely keeps our contract simple and general.
		 */
		if (!pstate->continuescan)
		{
			ScanKey		inequal PG_USED_FOR_ASSERTS_ONLY = so->keyData + insktrig;
			bool		satisfied PG_USED_FOR_ASSERTS_ONLY;

			Assert(sktrigrequired);

			/*
			 * Assert that this scan key is an inequality scan key marked
			 * required in the current scan direction
			 */
			Assert(inequal->sk_strategy != BTEqualStrategyNumber);
			Assert(((inequal->sk_flags & SK_BT_REQFWD) &&
					ScanDirectionIsForward(dir)) ||
				   ((inequal->sk_flags & SK_BT_REQBKWD) &&
					ScanDirectionIsBackward(dir)));

			/*
			 * The tuple must use "beyond end" advancement during the
			 * recursive call, so we cannot possibly end up back here when
			 * recursing.  We'll consume a small, fixed amount of stack space.
			 */
			Assert(!beyond_end_advance);

			satisfied = _bt_advance_array_keys(scan, pstate, tuple, insktrig);

			/* This tuple doesn't satisfy the inequality */
			Assert(!satisfied);
			return false;
		}

		/*
		 * Some non-required scan key (from new qual) still not satisfied.
		 *
		 * All required scan keys are still satisfied, though, so we can trust
		 * all_required_satisfied below.  We now know for sure that even later
		 * unsatisfied required inequalities can't have been overlooked.
		 */
	}

	/*
	 * Postcondition state machine assertion (for still-unsatisfied tuples).
	 *
	 * Caller's tuple is now < the newly advanced array keys (or > when this
	 * is a backwards scan) when not all required scan keys from the new qual
	 * (including any required inequality keys) were found to be satisified.
	 */
	Assert(_bt_tuple_before_array_skeys(scan, dir, tuple, false, 0) ==
		   !all_required_satisfied);

	/*
	 * If this call was just to deal with advancing (or considering the need
	 * to advance) a non-required array scan key, we must stick with the
	 * current primitive index scan
	 */
	if (!sktrigrequired)
	{
		Assert(all_required_satisfied && !foundRequiredOppositeDirOnly &&
			   !arrays_exhausted);

		pstate->continuescan = true;	/* Override _bt_check_compare */
		so->needPrimScan = false;	/* cannot start new primitive scan */

		/*
		 * This tuple doesn't satisfy some non-required scan key (typically
		 * caller's sktrig non-required array scan key, occasionally some
		 * later non-array scan key that happened to also be unsatisfied)
		 */
		return false;
	}

	/*
	 * Handle post-array-advance scheduling of new primitive index scans.
	 *
	 * By here we have established that the scan's required arrays were
	 * advanced, but did not become exhausted.
	 */
	Assert(arrays_advanced && !arrays_exhausted && sktrigrequired);

	/*
	 * Handle the case where one or more required scan keys aren't satisfied,
	 * even though caller's tuple is finaltup -- its leaf page's last tuple.
	 *
	 * We shouldn't let our caller continue to the next leaf page unless it's
	 * already near-certain that it covers key space that's relevant to the
	 * top-level index scan.  (It's not quite fully certain because we don't
	 * insist on having an exact match for required truncated attributes.  See
	 * the comments about truncated finaltup in the loop above for details.)
	 */
	if (!all_required_satisfied && tuple == pstate->finaltup)
	{
		pstate->continuescan = false;	/* Agree with _bt_check_compare */
		so->needPrimScan = true;	/* Call _bt_first again */

		/* This tuple (finaltup) doesn't match the qual */
		return false;
	}

	/*
	 * Handle inequalities marked required in the opposite scan direction.
	 * They can signal that we should start a new primitive index scan.
	 *
	 * It's possible that the scan is now positioned at the start of
	 * "matching" tuples (matching according to _bt_tuple_before_array_skeys),
	 * but is nevertheless still many leaf pages before the page/key space
	 * that _bt_first is capable of skipping ahead to.  Groveling through all
	 * of these leaf pages will always give correct answers, but it can be
	 * very inefficient.  We must avoid scanning extra pages unnecessarily.
	 *
	 * Apply a test using finaltup (not caller's tuple) to avoid the problem:
	 * if even finaltup doesn't satisfy this less significant inequality scan
	 * key (once we temporarily flip the scan direction), skip by starting a
	 * new primitive index scan.  When we skip, we know for sure that all of
	 * the tuples on the current page following caller's tuple are also before
	 * the _bt_first-wise start of tuples for our new qual.  That suggests
	 * that there might be many skippable leaf pages beyond the current page.
	 *
	 * _bt_tuple_before_array_skeys won't be able to deal with this itself
	 * later on (it doesn't know how), so we must deal with it now, up front.
	 */
	if (foundRequiredOppositeDirOnly && all_required_satisfied &&
		pstate->finaltup)
	{
		int			nfinaltupatts = BTreeTupleGetNAtts(pstate->finaltup, rel);
		ScanDirection flipped = -dir;
		bool		continuescanflip;
		int			opsktrig;
		ScanKey		inequal;

		/*
		 * We're checking finaltup (which is usually not caller's tuple), so
		 * cannot reuse work from caller's earlier _bt_check_compare call here
		 */
		opsktrig = 0;
		_bt_check_compare(flipped, so, pstate->finaltup, nfinaltupatts,
						  itupdesc, so->numArrayKeys, &continuescanflip,
						  &opsktrig, false, false);

		/*
		 * Test "opsktrig > sktrig" to make sure that finaltup contains the
		 * same prefix of key columns as caller's original tuple (a prefix
		 * that satisfies required equality scankeys whose ikey is <= sktrig).
		 *
		 * Must also avoid mistaking an unsatisfied array scan key that isn't
		 * required (in either direction) with an unsatisfied inequality scan
		 * key that is required in the opposite-to-scan direction.
		 */
		inequal = so->keyData + opsktrig;
		if (!continuescanflip && opsktrig > sktrig &&
			!(inequal->sk_flags & SK_SEARCHARRAY))
		{
			/*
			 * Assert that this scan key is an inequality scan key marked
			 * required in the opposite-to-scan direction only
			 */
			Assert(inequal->sk_strategy != BTEqualStrategyNumber);
			Assert(((inequal->sk_flags & SK_BT_REQFWD) &&
					ScanDirectionIsForward(flipped)) ||
				   ((inequal->sk_flags & SK_BT_REQBKWD) &&
					ScanDirectionIsBackward(flipped)));

			pstate->continuescan = false;
			so->needPrimScan = true;

			/*
			 * We established that caller's tuple doesn't satisfy qual already
			 * (before we examined finaltup)
			 */
			return false;
		}
	}

	/*
	 * Stick with the ongoing primitive index scan for now.
	 *
	 * It's possible that later tuples will also turn out to have values that
	 * are still < the now-current array keys (or > the current array keys).
	 * Our caller will handle this by performing what amounts to a linear
	 * search of the page, implemented by calling _bt_check_compare and then
	 * _bt_tuple_before_array_skeys for each tuple.  Our caller should locate
	 * the first tuple >= the array keys before long (or locate the first
	 * tuple <= the array keys before long).
	 *
	 * This approach has various advantages over a binary search of the page.
	 * We expect that our caller will either quickly discover the next tuple
	 * covered by the current array keys, or quickly discover that it needs
	 * another primitive index scan (using its finaltup precheck) instead.
	 * Repeated binary searching (one binary search per array advancement) is
	 * unlikely to outperform one continuous linear search of the whole page.
	 */
	pstate->continuescan = true;	/* Override _bt_check_compare */
	so->needPrimScan = false;	/* redundant */

	/* This tuple doesn't match the qual */
	return false;
}

/*
 *	_bt_preprocess_keys() -- Preprocess scan keys
 *
 * The given search-type keys (in scan->keyData[] or so->arrayKeyData[])
 * are copied to so->keyData[] with possible transformation.
 * scan->numberOfKeys is the number of input keys, so->numberOfKeys gets
 * the number of output keys (possibly less, never greater).
 *
 * The output keys are marked with additional sk_flags bits beyond the
 * system-standard bits supplied by the caller.  The DESC and NULLS_FIRST
 * indoption bits for the relevant index attribute are copied into the flags.
 * Also, for a DESC column, we commute (flip) all the sk_strategy numbers
 * so that the index sorts in the desired direction.
 *
 * One key purpose of this routine is to discover which scan keys must be
 * satisfied to continue the scan.  It also attempts to eliminate redundant
 * keys and detect contradictory keys.  (If the index opfamily provides
 * incomplete sets of cross-type operators, we may fail to detect redundant
 * or contradictory keys, but we can survive that.)
 *
 * The output keys must be sorted by index attribute.  Presently we expect
 * (but verify) that the input keys are already so sorted --- this is done
 * by match_clauses_to_index() in indxpath.c.  Some reordering of the keys
 * within each attribute may be done as a byproduct of the processing here,
 * but no other code depends on that.  Note that index scans with array scan
 * keys depend on state (maintained here by us) that maps each of our input
 * scan keys to its corresponding output scan key.  This indirection allows
 * index scans to use an ikey offset-to-output-scankey to look up the cached
 * ORDER proc for the scankey.
 *
 * The output keys are marked with flags SK_BT_REQFWD and/or SK_BT_REQBKWD
 * if they must be satisfied in order to continue the scan forward or backward
 * respectively.  _bt_checkkeys uses these flags.  For example, if the quals
 * are "x = 1 AND y < 4 AND z < 5", then _bt_checkkeys will reject a tuple
 * (1,2,7), but we must continue the scan in case there are tuples (1,3,z).
 * But once we reach tuples like (1,4,z) we can stop scanning because no
 * later tuples could match.  This is reflected by marking the x and y keys,
 * but not the z key, with SK_BT_REQFWD.  In general, the keys for leading
 * attributes with "=" keys are marked both SK_BT_REQFWD and SK_BT_REQBKWD.
 * For the first attribute without an "=" key, any "<" and "<=" keys are
 * marked SK_BT_REQFWD while any ">" and ">=" keys are marked SK_BT_REQBKWD.
 * This can be seen to be correct by considering the above example.  Note
 * in particular that if there are no keys for a given attribute, the keys for
 * subsequent attributes can never be required; for instance "WHERE y = 4"
 * requires a full-index scan.
 *
 * If possible, redundant keys are eliminated: we keep only the tightest
 * >/>= bound and the tightest </<= bound, and if there's an = key then
 * that's the only one returned.  (So, we return either a single = key,
 * or one or two boundary-condition keys for each attr.)  However, if we
 * cannot compare two keys for lack of a suitable cross-type operator,
 * we cannot eliminate either.  If there are two such keys of the same
 * operator strategy, the second one is just pushed into the output array
 * without further processing here.  We may also emit both >/>= or both
 * </<= keys if we can't compare them.  The logic about required keys still
 * works if we don't eliminate redundant keys.
 *
 * Note that one reason we need direction-sensitive required-key flags is
 * precisely that we may not be able to eliminate redundant keys.  Suppose
 * we have "x > 4::int AND x > 10::bigint", and we are unable to determine
 * which key is more restrictive for lack of a suitable cross-type operator.
 * _bt_first will arbitrarily pick one of the keys to do the initial
 * positioning with.  If it picks x > 4, then the x > 10 condition will fail
 * until we reach index entries > 10; but we can't stop the scan just because
 * x > 10 is failing.  On the other hand, if we are scanning backwards, then
 * failure of either key is indeed enough to stop the scan.  (In general, when
 * inequality keys are present, the initial-positioning code only promises to
 * position before the first possible match, not exactly at the first match,
 * for a forward scan; or after the last match for a backward scan.)
 *
 * As a byproduct of this work, we can detect contradictory quals such
 * as "x = 1 AND x > 2".  If we see that, we return so->qual_ok = false,
 * indicating the scan need not be run at all since no tuples can match.
 * (In this case we do not bother completing the output key array!)
 * Again, missing cross-type operators might cause us to fail to prove the
 * quals contradictory when they really are, but the scan will work correctly.
 *
 * Index scans with array keys need to be able to advance each array's keys
 * and make them the current search-type scan keys without calling here.  They
 * expect to be able to call _bt_update_keys_with_arraykeys instead.  We need
 * to be careful about that case when we determine redundancy; equality quals
 * must not be eliminated as redundant on the basis of array input keys that
 * might change before another call here can take place.  Note, however, that
 * the presence of an array scan key doesn't affect how we determine if index
 * quals are contradictory.  Contradictory qual scans move on to the next
 * primitive index scan right away, by incrementing the scan's array keys once
 * control reaches _bt_array_keys_remain.  There won't be a call to
 * _bt_update_keys_with_arraykeys, so there's nothing for us to break.
 *
 * Row comparison keys are currently also treated without any smarts:
 * we just transfer them into the preprocessed array without any
 * editorialization.  We can treat them the same as an ordinary inequality
 * comparison on the row's first index column, for the purposes of the logic
 * about required keys.
 *
 * Note: the reason we have to copy the preprocessed scan keys into private
 * storage is that we are modifying the array based on comparisons of the
 * key argument values, which could change on a rescan or after moving to
 * new elements of array keys.  Therefore we can't overwrite the source data.
 */
void
_bt_preprocess_keys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	int			numberOfKeys = scan->numberOfKeys;
	int16	   *indoption = scan->indexRelation->rd_indoption;
	int			new_numberOfKeys;
	int			numberOfEqualCols;
	ScanKey		inkeys;
	ScanKey		outkeys;
	int		   *orderProcsMap = NULL;
	ScanKey		cur;
	ScanKeyAttr xform[BTMaxStrategyNumber];
	bool		test_result;
	int			i,
				j;
	AttrNumber	attno;

	/* initialize result variables */
	so->qual_ok = true;
	so->numberOfKeys = 0;

	if (numberOfKeys < 1)
		return;					/* done if qual-less scan */

	/*
	 * Read so->arrayKeyData if array keys are present, else scan->keyData
	 */
	if (so->arrayKeyData != NULL)
	{
		inkeys = so->arrayKeyData;
		orderProcsMap = so->orderProcsMap;
	}
	else
		inkeys = scan->keyData;

	outkeys = so->keyData;
	cur = &inkeys[0];
	/* we check that input keys are correctly ordered */
	if (cur->sk_attno < 1)
		elog(ERROR, "btree index keys must be ordered by attribute");

	/* We can short-circuit most of the work if there's just one key */
	if (numberOfKeys == 1)
	{
		/* Apply indoption to scankey (might change sk_strategy!) */
		if (!_bt_fix_scankey_strategy(cur, indoption))
			so->qual_ok = false;
		memcpy(outkeys, cur, sizeof(ScanKeyData));
		so->numberOfKeys = 1;
		/* We can mark the qual as required if it's for first index col */
		if (cur->sk_attno == 1)
			_bt_mark_scankey_required(outkeys);
		if (orderProcsMap)
			orderProcsMap[0] = 0;
		return;
	}

	/*
	 * Otherwise, do the full set of pushups.
	 */
	new_numberOfKeys = 0;
	numberOfEqualCols = 0;

	/*
	 * Initialize for processing of keys for attr 1.
	 *
	 * xform[i] points to the currently best scan key of strategy type i+1; it
	 * is NULL if we haven't yet found such a key for this attr.
	 */
	attno = 1;
	memset(xform, 0, sizeof(xform));

	/*
	 * Loop iterates from 0 to numberOfKeys inclusive; we use the last pass to
	 * handle after-last-key processing.  Actual exit from the loop is at the
	 * "break" statement below.
	 */
	for (i = 0;; cur++, i++)
	{
		if (i < numberOfKeys)
		{
			/* Apply indoption to scankey (might change sk_strategy!) */
			if (!_bt_fix_scankey_strategy(cur, indoption))
			{
				/* NULL can't be matched, so give up */
				so->qual_ok = false;
				return;
			}
		}

		/*
		 * If we are at the end of the keys for a particular attr, finish up
		 * processing and emit the cleaned-up keys.
		 */
		if (i == numberOfKeys || cur->sk_attno != attno)
		{
			int			priorNumberOfEqualCols = numberOfEqualCols;

			/* check input keys are correctly ordered */
			if (i < numberOfKeys && cur->sk_attno < attno)
				elog(ERROR, "btree index keys must be ordered by attribute");

			/*
			 * If = has been specified, all other keys can be eliminated as
			 * redundant.  If we have a case like key = 1 AND key > 2, we can
			 * set qual_ok to false and abandon further processing.
			 *
			 * We also have to deal with the case of "key IS NULL", which is
			 * unsatisfiable in combination with any other index condition. By
			 * the time we get here, that's been classified as an equality
			 * check, and we've rejected any combination of it with a regular
			 * equality condition; but not with other types of conditions.
			 */
			if (xform[BTEqualStrategyNumber - 1].skey)
			{
				ScanKey		eq = xform[BTEqualStrategyNumber - 1].skey;

				for (j = BTMaxStrategyNumber; --j >= 0;)
				{
					ScanKey		chk = xform[j].skey;

					if (!chk || j == (BTEqualStrategyNumber - 1))
						continue;

					if (eq->sk_flags & SK_SEARCHNULL)
					{
						/* IS NULL is contradictory to anything else */
						so->qual_ok = false;
						return;
					}

					if (_bt_compare_scankey_args(scan, chk, eq, chk,
												 &test_result))
					{
						if (!test_result)
						{
							/* keys proven mutually contradictory */
							so->qual_ok = false;
							return;
						}
						else if (!(eq->sk_flags & SK_SEARCHARRAY))
						{
							/* else discard the redundant non-equality key */
							xform[j].skey = NULL;
							xform[j].ikey = -1;
						}
					}
					/* else, cannot determine redundancy, keep both keys */
				}
				/* track number of attrs for which we have "=" keys */
				numberOfEqualCols++;
			}

			/* try to keep only one of <, <= */
			if (xform[BTLessStrategyNumber - 1].skey &&
				xform[BTLessEqualStrategyNumber - 1].skey)
			{
				ScanKey		lt = xform[BTLessStrategyNumber - 1].skey;
				ScanKey		le = xform[BTLessEqualStrategyNumber - 1].skey;

				if (_bt_compare_scankey_args(scan, le, lt, le,
											 &test_result))
				{
					if (test_result)
						xform[BTLessEqualStrategyNumber - 1].skey = NULL;
					else
						xform[BTLessStrategyNumber - 1].skey = NULL;
				}
			}

			/* try to keep only one of >, >= */
			if (xform[BTGreaterStrategyNumber - 1].skey &&
				xform[BTGreaterEqualStrategyNumber - 1].skey)
			{
				ScanKey		gt = xform[BTGreaterStrategyNumber - 1].skey;
				ScanKey		ge = xform[BTGreaterEqualStrategyNumber - 1].skey;

				if (_bt_compare_scankey_args(scan, ge, gt, ge,
											 &test_result))
				{
					if (test_result)
						xform[BTGreaterEqualStrategyNumber - 1].skey = NULL;
					else
						xform[BTGreaterStrategyNumber - 1].skey = NULL;
				}
			}

			/*
			 * Emit the cleaned-up keys into the outkeys[] array, and then
			 * mark them if they are required.  They are required (possibly
			 * only in one direction) if all attrs before this one had "=".
			 */
			for (j = BTMaxStrategyNumber; --j >= 0;)
			{
				if (xform[j].skey)
				{
					ScanKey		outkey = &outkeys[new_numberOfKeys++];

					memcpy(outkey, xform[j].skey, sizeof(ScanKeyData));
					if (orderProcsMap)
						orderProcsMap[new_numberOfKeys - 1] = xform[j].ikey;
					if (priorNumberOfEqualCols == attno - 1)
						_bt_mark_scankey_required(outkey);
				}
			}

			/*
			 * Exit loop here if done.
			 */
			if (i == numberOfKeys)
				break;

			/* Re-initialize for new attno */
			attno = cur->sk_attno;
			memset(xform, 0, sizeof(xform));
		}

		/* check strategy this key's operator corresponds to */
		j = cur->sk_strategy - 1;

		/* if row comparison, push it directly to the output array */
		if (cur->sk_flags & SK_ROW_HEADER)
		{
			ScanKey		outkey = &outkeys[new_numberOfKeys++];

			memcpy(outkey, cur, sizeof(ScanKeyData));
			if (orderProcsMap)
				orderProcsMap[new_numberOfKeys - 1] = i;
			if (numberOfEqualCols == attno - 1)
				_bt_mark_scankey_required(outkey);

			/*
			 * We don't support RowCompare using equality; such a qual would
			 * mess up the numberOfEqualCols tracking.
			 */
			Assert(j != (BTEqualStrategyNumber - 1));
			continue;
		}

		/*
		 * Is this an array scan key that _bt_preprocess_array_keys merged
		 * with some earlier array key during its initial preprocessing pass?
		 */
		if (cur->sk_flags & SK_BT_RDDNARRAY)
		{
			/*
			 * key is redundant for this primitive index scan (and will be
			 * redundant during all subsequent primitive index scans)
			 */
			Assert(j == (BTEqualStrategyNumber - 1));
			Assert(cur->sk_flags & SK_SEARCHARRAY);
			Assert(xform[j].skey->sk_attno == cur->sk_attno);
			continue;
		}

		/*
		 * have we seen a scan key for this same attribute and using this same
		 * operator strategy before now?
		 */
		if (xform[j].skey == NULL)
		{
			/* nope, so this scan key wins by default (at least for now) */
			xform[j].skey = cur;
			xform[j].ikey = i;
		}
		else
		{
			ScanKey		outkey;

			/* yup, keep only the more restrictive key if possible */
			if (_bt_compare_scankey_args(scan, cur, cur, xform[j].skey,
										 &test_result))
			{
				if (test_result)
				{
					/* Redundant scan keys */
					if (j == (BTEqualStrategyNumber - 1) &&
						(xform[j].skey->sk_flags & SK_SEARCHARRAY))
					{
						/*
						 * Equality strategy array scan keys can never be
						 * truly redundant (unless marked SK_BT_RDDNARRAY). We
						 * cannot eliminate our previous best scan key, since
						 * _bt_update_keys_with_arraykeys might be broken by
						 * that later on.
						 *
						 * Fall through to "keep both" path usually used when
						 * we cannot prove which key is more restrictive
						 * either way.
						 */
					}
					else
					{
						/*
						 * Replace previous best scan key with new best scan
						 * key (this scan key, cur)
						 */
						Assert((xform[j].skey->sk_flags & SK_SEARCHARRAY) == 0 ||
							   xform[j].skey->sk_strategy != BTEqualStrategyNumber);

						xform[j].skey = cur;
						xform[j].ikey = i;
						continue;
					}
				}
				else
				{
					if (j == (BTEqualStrategyNumber - 1))
					{
						/* key == a && key == b, but a != b */
						so->qual_ok = false;
						return;
					}
					else
					{
						/*
						 * Do nothing with cur -- xform[j] is more
						 * restrictive, and so will usually be chosen for this
						 * attribute when we're done with its scan keys.
						 */
						continue;
					}
				}
			}

			/*
			 * Keep both.
			 *
			 * We can't determine which key is more restrictive (or we can't
			 * eliminate an array scan key).  Replace it in xform[j], and push
			 * the cur one directly to the output array, too.
			 */
			outkey = &outkeys[new_numberOfKeys++];

			memcpy(outkey, xform[j].skey, sizeof(ScanKeyData));
			if (orderProcsMap)
				orderProcsMap[new_numberOfKeys - 1] = xform[j].ikey;
			if (numberOfEqualCols == attno - 1)
				_bt_mark_scankey_required(outkey);
			xform[j].skey = cur;
			xform[j].ikey = i;
		}
	}

	so->numberOfKeys = new_numberOfKeys;
}

/*
 *	_bt_update_keys_with_arraykeys() -- Finalize advancing array keys
 *
 * Transfers newly advanced array keys that were set in "so->arrayKeyData[]"
 * over to corresponding "so->keyData[]" scan keys.  Reuses most of the work
 * that took place within _bt_preprocess_keys, only changing the array keys.
 *
 * It's safe to call here while holding a buffer lock, which isn't something
 * that _bt_preprocess_keys can guarantee.
 */
static void
_bt_update_keys_with_arraykeys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	int			arrayidx = 0;

	Assert(so->qual_ok);

	for (int ikey = 0; ikey < so->numberOfKeys; ikey++)
	{
		ScanKey		cur = so->keyData + ikey;
		BTArrayKeyInfo *array;
		ScanKey		skeyarray;

		Assert((cur->sk_flags & SK_BT_RDDNARRAY) == 0);

		/* Just update equality array scan keys */
		if (cur->sk_strategy != BTEqualStrategyNumber ||
			!(cur->sk_flags & SK_SEARCHARRAY))
			continue;

		array = &so->arrayKeys[arrayidx++];
		skeyarray = &so->arrayKeyData[array->scan_key];

		/* Update the scan key's argument */
		Assert(cur->sk_attno == skeyarray->sk_attno);
		cur->sk_argument = skeyarray->sk_argument;
	}

	Assert(arrayidx == so->numArrayKeys);
}

/*
 * Verify that the scan's "so->arrayKeyData[]" scan keys are in agreement with
 * the current "so->keyData[]" search-type scan keys.  Used within assertions.
 */
#ifdef USE_ASSERT_CHECKING
static bool
_bt_verify_keys_with_arraykeys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	int			last_proc_map = -1,
				last_sk_attno = 0,
				arrayidx = 0;

	if (!so->qual_ok)
		return false;

	for (int ikey = 0; ikey < so->numberOfKeys; ikey++)
	{
		ScanKey		cur = so->keyData + ikey;
		BTArrayKeyInfo *array;
		ScanKey		skeyarray;

		if (cur->sk_strategy != BTEqualStrategyNumber ||
			!(cur->sk_flags & SK_SEARCHARRAY))
			continue;

		array = &so->arrayKeys[arrayidx++];
		skeyarray = &so->arrayKeyData[array->scan_key];

		/*
		 * Verify that so->orderProcsMap[] mappings are in order for
		 * SK_SEARCHARRAY equality strategy scan keys
		 */
		if (last_proc_map >= so->orderProcsMap[ikey])
			return false;
		last_proc_map = so->orderProcsMap[ikey];

		/* Verify so->arrayKeyData[] input key has expected sk_argument */
		if (skeyarray->sk_argument != array->elem_values[array->cur_elem])
			return false;

		/* Verify so->arrayKeyData[] input key agrees with output key */
		if (cur->sk_attno != skeyarray->sk_attno)
			return false;
		if (cur->sk_argument != skeyarray->sk_argument)
			return false;
		if (last_sk_attno > cur->sk_attno)
			return false;
		last_sk_attno = cur->sk_attno;
	}

	if (arrayidx != so->numArrayKeys)
		return false;

	return true;
}
#endif

/*
 * Compare two scankey values using a specified operator.
 *
 * The test we want to perform is logically "leftarg op rightarg", where
 * leftarg and rightarg are the sk_argument values in those ScanKeys, and
 * the comparison operator is the one in the op ScanKey.  However, in
 * cross-data-type situations we may need to look up the correct operator in
 * the index's opfamily: it is the one having amopstrategy = op->sk_strategy
 * and amoplefttype/amoprighttype equal to the two argument datatypes.
 *
 * If the opfamily doesn't supply a complete set of cross-type operators we
 * may not be able to make the comparison.  If we can make the comparison
 * we store the operator result in *result and return true.  We return false
 * if the comparison could not be made.
 *
 * Note: op always points at the same ScanKey as either leftarg or rightarg.
 * Since we don't scribble on the scankeys, this aliasing should cause no
 * trouble.
 *
 * Note: this routine needs to be insensitive to any DESC option applied
 * to the index column.  For example, "x < 4" is a tighter constraint than
 * "x < 5" regardless of which way the index is sorted.
 */
static bool
_bt_compare_scankey_args(IndexScanDesc scan, ScanKey op,
						 ScanKey leftarg, ScanKey rightarg,
						 bool *result)
{
	Relation	rel = scan->indexRelation;
	Oid			lefttype,
				righttype,
				optype,
				opcintype,
				cmp_op;
	StrategyNumber strat;

	/*
	 * First, deal with cases where one or both args are NULL.  This should
	 * only happen when the scankeys represent IS NULL/NOT NULL conditions.
	 */
	if ((leftarg->sk_flags | rightarg->sk_flags) & SK_ISNULL)
	{
		bool		leftnull,
					rightnull;

		if (leftarg->sk_flags & SK_ISNULL)
		{
			Assert(leftarg->sk_flags & (SK_SEARCHNULL | SK_SEARCHNOTNULL));
			leftnull = true;
		}
		else
			leftnull = false;
		if (rightarg->sk_flags & SK_ISNULL)
		{
			Assert(rightarg->sk_flags & (SK_SEARCHNULL | SK_SEARCHNOTNULL));
			rightnull = true;
		}
		else
			rightnull = false;

		/*
		 * We treat NULL as either greater than or less than all other values.
		 * Since true > false, the tests below work correctly for NULLS LAST
		 * logic.  If the index is NULLS FIRST, we need to flip the strategy.
		 */
		strat = op->sk_strategy;
		if (op->sk_flags & SK_BT_NULLS_FIRST)
			strat = BTCommuteStrategyNumber(strat);

		switch (strat)
		{
			case BTLessStrategyNumber:
				*result = (leftnull < rightnull);
				break;
			case BTLessEqualStrategyNumber:
				*result = (leftnull <= rightnull);
				break;
			case BTEqualStrategyNumber:
				*result = (leftnull == rightnull);
				break;
			case BTGreaterEqualStrategyNumber:
				*result = (leftnull >= rightnull);
				break;
			case BTGreaterStrategyNumber:
				*result = (leftnull > rightnull);
				break;
			default:
				elog(ERROR, "unrecognized StrategyNumber: %d", (int) strat);
				*result = false;	/* keep compiler quiet */
				break;
		}
		return true;
	}

	/*
	 * The opfamily we need to worry about is identified by the index column.
	 */
	Assert(leftarg->sk_attno == rightarg->sk_attno);

	opcintype = rel->rd_opcintype[leftarg->sk_attno - 1];

	/*
	 * Determine the actual datatypes of the ScanKey arguments.  We have to
	 * support the convention that sk_subtype == InvalidOid means the opclass
	 * input type; this is a hack to simplify life for ScanKeyInit().
	 */
	lefttype = leftarg->sk_subtype;
	if (lefttype == InvalidOid)
		lefttype = opcintype;
	righttype = rightarg->sk_subtype;
	if (righttype == InvalidOid)
		righttype = opcintype;
	optype = op->sk_subtype;
	if (optype == InvalidOid)
		optype = opcintype;

	/*
	 * If leftarg and rightarg match the types expected for the "op" scankey,
	 * we can use its already-looked-up comparison function.
	 */
	if (lefttype == opcintype && righttype == optype)
	{
		*result = DatumGetBool(FunctionCall2Coll(&op->sk_func,
												 op->sk_collation,
												 leftarg->sk_argument,
												 rightarg->sk_argument));
		return true;
	}

	/*
	 * Otherwise, we need to go to the syscache to find the appropriate
	 * operator.  (This cannot result in infinite recursion, since no
	 * indexscan initiated by syscache lookup will use cross-data-type
	 * operators.)
	 *
	 * If the sk_strategy was flipped by _bt_fix_scankey_strategy, we have to
	 * un-flip it to get the correct opfamily member.
	 */
	strat = op->sk_strategy;
	if (op->sk_flags & SK_BT_DESC)
		strat = BTCommuteStrategyNumber(strat);

	cmp_op = get_opfamily_member(rel->rd_opfamily[leftarg->sk_attno - 1],
								 lefttype,
								 righttype,
								 strat);
	if (OidIsValid(cmp_op))
	{
		RegProcedure cmp_proc = get_opcode(cmp_op);

		if (RegProcedureIsValid(cmp_proc))
		{
			*result = DatumGetBool(OidFunctionCall2Coll(cmp_proc,
														op->sk_collation,
														leftarg->sk_argument,
														rightarg->sk_argument));
			return true;
		}
	}

	/* Can't make the comparison */
	*result = false;			/* suppress compiler warnings */
	return false;
}

/*
 * Adjust a scankey's strategy and flags setting as needed for indoptions.
 *
 * We copy the appropriate indoption value into the scankey sk_flags
 * (shifting to avoid clobbering system-defined flag bits).  Also, if
 * the DESC option is set, commute (flip) the operator strategy number.
 *
 * A secondary purpose is to check for IS NULL/NOT NULL scankeys and set up
 * the strategy field correctly for them.
 *
 * Lastly, for ordinary scankeys (not IS NULL/NOT NULL), we check for a
 * NULL comparison value.  Since all btree operators are assumed strict,
 * a NULL means that the qual cannot be satisfied.  We return true if the
 * comparison value isn't NULL, or false if the scan should be abandoned.
 *
 * This function is applied to the *input* scankey structure; therefore
 * on a rescan we will be looking at already-processed scankeys.  Hence
 * we have to be careful not to re-commute the strategy if we already did it.
 * It's a bit ugly to modify the caller's copy of the scankey but in practice
 * there shouldn't be any problem, since the index's indoptions are certainly
 * not going to change while the scankey survives.
 */
static bool
_bt_fix_scankey_strategy(ScanKey skey, int16 *indoption)
{
	int			addflags;

	addflags = indoption[skey->sk_attno - 1] << SK_BT_INDOPTION_SHIFT;

	/*
	 * We treat all btree operators as strict (even if they're not so marked
	 * in pg_proc). This means that it is impossible for an operator condition
	 * with a NULL comparison constant to succeed, and we can reject it right
	 * away.
	 *
	 * However, we now also support "x IS NULL" clauses as search conditions,
	 * so in that case keep going. The planner has not filled in any
	 * particular strategy in this case, so set it to BTEqualStrategyNumber
	 * --- we can treat IS NULL as an equality operator for purposes of search
	 * strategy.
	 *
	 * Likewise, "x IS NOT NULL" is supported.  We treat that as either "less
	 * than NULL" in a NULLS LAST index, or "greater than NULL" in a NULLS
	 * FIRST index.
	 *
	 * Note: someday we might have to fill in sk_collation from the index
	 * column's collation.  At the moment this is a non-issue because we'll
	 * never actually call the comparison operator on a NULL.
	 */
	if (skey->sk_flags & SK_ISNULL)
	{
		/* SK_ISNULL shouldn't be set in a row header scankey */
		Assert(!(skey->sk_flags & SK_ROW_HEADER));

		/* Set indoption flags in scankey (might be done already) */
		skey->sk_flags |= addflags;

		/* Set correct strategy for IS NULL or NOT NULL search */
		if (skey->sk_flags & SK_SEARCHNULL)
		{
			skey->sk_strategy = BTEqualStrategyNumber;
			skey->sk_subtype = InvalidOid;
			skey->sk_collation = InvalidOid;
		}
		else if (skey->sk_flags & SK_SEARCHNOTNULL)
		{
			if (skey->sk_flags & SK_BT_NULLS_FIRST)
				skey->sk_strategy = BTGreaterStrategyNumber;
			else
				skey->sk_strategy = BTLessStrategyNumber;
			skey->sk_subtype = InvalidOid;
			skey->sk_collation = InvalidOid;
		}
		else
		{
			/* regular qual, so it cannot be satisfied */
			return false;
		}

		/* Needn't do the rest */
		return true;
	}

	/* Adjust strategy for DESC, if we didn't already */
	if ((addflags & SK_BT_DESC) && !(skey->sk_flags & SK_BT_DESC))
		skey->sk_strategy = BTCommuteStrategyNumber(skey->sk_strategy);
	skey->sk_flags |= addflags;

	/* If it's a row header, fix row member flags and strategies similarly */
	if (skey->sk_flags & SK_ROW_HEADER)
	{
		ScanKey		subkey = (ScanKey) DatumGetPointer(skey->sk_argument);

		for (;;)
		{
			Assert(subkey->sk_flags & SK_ROW_MEMBER);
			addflags = indoption[subkey->sk_attno - 1] << SK_BT_INDOPTION_SHIFT;
			if ((addflags & SK_BT_DESC) && !(subkey->sk_flags & SK_BT_DESC))
				subkey->sk_strategy = BTCommuteStrategyNumber(subkey->sk_strategy);
			subkey->sk_flags |= addflags;
			if (subkey->sk_flags & SK_ROW_END)
				break;
			subkey++;
		}
	}

	return true;
}

/*
 * Mark a scankey as "required to continue the scan".
 *
 * Depending on the operator type, the key may be required for both scan
 * directions or just one.  Also, if the key is a row comparison header,
 * we have to mark its first subsidiary ScanKey as required.  (Subsequent
 * subsidiary ScanKeys are normally for lower-order columns, and thus
 * cannot be required, since they're after the first non-equality scankey.)
 *
 * Note: when we set required-key flag bits in a subsidiary scankey, we are
 * scribbling on a data structure belonging to the index AM's caller, not on
 * our private copy.  This should be OK because the marking will not change
 * from scan to scan within a query, and so we'd just re-mark the same way
 * anyway on a rescan.  Something to keep an eye on though.
 */
static void
_bt_mark_scankey_required(ScanKey skey)
{
	int			addflags;

	switch (skey->sk_strategy)
	{
		case BTLessStrategyNumber:
		case BTLessEqualStrategyNumber:
			addflags = SK_BT_REQFWD;
			break;
		case BTEqualStrategyNumber:
			addflags = SK_BT_REQFWD | SK_BT_REQBKWD;
			break;
		case BTGreaterEqualStrategyNumber:
		case BTGreaterStrategyNumber:
			addflags = SK_BT_REQBKWD;
			break;
		default:
			elog(ERROR, "unrecognized StrategyNumber: %d",
				 (int) skey->sk_strategy);
			addflags = 0;		/* keep compiler quiet */
			break;
	}

	skey->sk_flags |= addflags;

	if (skey->sk_flags & SK_ROW_HEADER)
	{
		ScanKey		subkey = (ScanKey) DatumGetPointer(skey->sk_argument);

		/* First subkey should be same column/operator as the header */
		Assert(subkey->sk_flags & SK_ROW_MEMBER);
		Assert(subkey->sk_attno == skey->sk_attno);
		Assert(subkey->sk_strategy == skey->sk_strategy);
		subkey->sk_flags |= addflags;
	}
}

/*
 * Test whether an indextuple satisfies all the scankey conditions.
 *
 * Return true if so, false if not.  If the tuple fails to pass the qual,
 * we also determine whether there's any need to continue the scan beyond
 * this tuple, and set pstate.continuescan accordingly.  See comments for
 * _bt_preprocess_keys(), above, about how this is done.
 *
 * Forward scan callers call with a high key tuple last in the hopes of having
 * us set pstate.continuescan to false, and avoiding an unnecessary visit to
 * the page to the right.  Pass finaltup=true for these high key calls.
 * Backwards scan callers shouldn't do this, but should still let us know
 * which tuple is last by passing finaltup=true for the final non-pivot tuple
 * (the non-pivot tuple at page offset number one).
 *
 * Callers with equality strategy array scan keys must set up page state that
 * helps us know when to start or stop primitive index scans on their behalf.
 * The finaltup tuple should be stashed in pstate.finaltup, so we don't have
 * to wait until the finaltup call to be able to see what's up with the page.
 *
 * Advances the scan's array keys in passing when required.  Note that we rely
 * on _bt_readpage calling here in page offset number order (for the current
 * scan direction).  Any other order confuses array advancement.
 *
 * scan: index scan descriptor (containing a search-type scankey)
 * pstate: Page level input and output parameters
 * tuple: index tuple to test
 * finaltup: Is tuple the final one we'll be called with for this page?
 * tupnatts: number of attributes in tupnatts (high key may be truncated)
 * continuescanPrechecked: indicates that continuescan flag is known to
 * 						   be true for the last item on the page
 * haveFirstMatch: indicates that we already have at least one match
 * 							  in the current page
 */
bool
_bt_checkkeys(IndexScanDesc scan, BTReadPageState *pstate,
			  IndexTuple tuple, bool finaltup, int tupnatts,
			  bool continuescanPrechecked, bool haveFirstMatch)
{
	TupleDesc	tupdesc = RelationGetDescr(scan->indexRelation);
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	int			numArrayKeys = so->numArrayKeys;
	ScanDirection dir = pstate->dir;
	int			ikey = 0;
	bool		res;

	Assert(BTreeTupleGetNAtts(tuple, scan->indexRelation) == tupnatts);
	Assert(!numArrayKeys || so->advanceDir == dir);
	Assert(!so->needPrimScan);

	res = _bt_check_compare(dir, so, tuple, tupnatts, tupdesc,
							numArrayKeys, &pstate->continuescan, &ikey,
							continuescanPrechecked, haveFirstMatch);

	/*
	 * Only one _bt_check_compare call is required in the common case where
	 * there are no equality strategy array scan keys.  Otherwise we can only
	 * accept _bt_check_compare's answer unreservedly when it didn't set
	 * pstate.continuescan=false.
	 */
	if (!numArrayKeys || pstate->continuescan)
		return res;

	/*
	 * _bt_check_compare call set continuescan=false in the presence of
	 * equality type array keys.  This likely means that the tuple is just
	 * past the end of matches for the current array keys (if the current set
	 * of array keys is the final set, the top-level scan will terminate).
	 *
	 * It's also possible that the scan is still _before_ the _start_ of
	 * tuples matching the current set of array keys.  Check for that first.
	 */
	if (_bt_tuple_before_array_skeys(scan, dir, tuple, true, ikey))
	{
		/*
		 * Current tuple is < the current array scan keys/equality constraints
		 * (or > in the backward scan case).  Don't need to advance the array
		 * keys.  Must decide whether to start a new primitive scan instead.
		 *
		 * If this tuple isn't the finaltup for the page, then recheck the
		 * finaltup stashed in pstate as an optimization.  That allows us to
		 * quit scanning this page early when it's clearly hopeless (we don't
		 * need to wait for the finaltup call to give up on a primitive scan).
		 */
		if (finaltup || (!pstate->finaltupchecked && pstate->finaltup &&
						 _bt_tuple_before_array_skeys(scan, dir,
													  pstate->finaltup,
													  false, 0)))
		{
			/*
			 * Give up on the ongoing primitive index scan.
			 *
			 * Even the final tuple (the high key for forward scans, or the
			 * tuple from page offset number 1 for backward scans) is before
			 * the current array keys.  That strongly suggests that continuing
			 * this primitive scan would be less efficient than starting anew.
			 *
			 * See also: _bt_advance_array_keys's handling of the case where
			 * finaltup itself advances the array keys to non-matching values.
			 */
			pstate->continuescan = false;

			/*
			 * Set up a new primitive index scan that will reposition the
			 * top-level scan to the first leaf page whose key space is
			 * covered by our array keys.  The top-level scan will "skip" a
			 * part of the index that can only contain non-matching tuples.
			 *
			 * Note: the next primitive index scan is guaranteed to land on
			 * some later leaf page (ideally it won't be this page's sibling).
			 * It follows that the top-level scan can never access the same
			 * leaf page more than once (unless the scan changes direction or
			 * btrestrpos is called).  btcostestimate relies on this.
			 */
			so->needPrimScan = true;
		}
		else
		{
			/*
			 * Stick with the ongoing primitive index scan, for now (override
			 * _bt_check_compare's suggestion that we end the scan).
			 *
			 * Note: we will end up here again and again given a group of
			 * tuples > the previous array keys and < the now-current keys
			 * (though only after an initial finaltup precheck determined that
			 * this page definitely covers key space from both array keysets).
			 * In effect, we perform a linear search of the page's remaining
			 * unscanned tuples every time the arrays advance past the key
			 * space of the scan's then-current tuple.
			 */
			pstate->continuescan = true;

			/*
			 * Our finaltup precheck determined that it is >= the current keys
			 * (though the _current_ tuple is still < the current array keys).
			 *
			 * Remember that fact in pstate now.  This avoids wasting cycles
			 * on repeating the same precheck step (checking the same finaltup
			 * against the same array keys) during later calls here for later
			 * tuples from this same leaf page.
			 */
			pstate->finaltupchecked = true;
		}

		/* This indextuple doesn't match the qual */
		return false;
	}

	/*
	 * Caller's tuple is >= the current set of array keys and other equality
	 * constraint scan keys (or <= if this is a backwards scan).  It's now
	 * clear that we _must_ advance any required array keys in lockstep with
	 * the scan (unless the required array keys become exhausted instead, or
	 * unless the ikey trigger corresponds to a non-required array scan key).
	 *
	 * Note: we might even advance the required arrays when all existing keys
	 * are already equal to the values from the tuple at this point.  See the
	 * comments above _bt_advance_array_keys about required-inequality-driven
	 * array advancement.
	 *
	 * Note: we _won't_ advance any required arrays when the ikey/trigger scan
	 * key corresponds to a non-required array found to be unsatisfied by the
	 * current keys.  (We might not even "advance" the non-required array.)
	 */
	return _bt_advance_array_keys(scan, pstate, tuple, ikey);
}

/*
 * Test whether an indextuple satisfies current scan condition.
 *
 * Return true if so, false if not.  If not, also clear *continuescan if
 * it's not possible for any future tuples in the current scan direction to
 * pass the qual with the current set of array keys.
 *
 * This is a subroutine for _bt_checkkeys.  It is written with the assumption
 * that reaching the end of each distinct set of array keys terminates the
 * ongoing primitive index scan.  It is up to our caller (which has more high
 * level context than us) to override that initial determination when it makes
 * more sense to advance the array keys and continue with further tuples from
 * the same leaf page.
 */
static bool
_bt_check_compare(ScanDirection dir, BTScanOpaque so,
				  IndexTuple tuple, int tupnatts, TupleDesc tupdesc,
				  int numArrayKeys, bool *continuescan, int *ikey,
				  bool continuescanPrechecked, bool haveFirstMatch)
{
	*continuescan = true;		/* default assumption */

	for (; *ikey < so->numberOfKeys; (*ikey)++)
	{
		ScanKey		key = so->keyData + *ikey;
		Datum		datum;
		bool		isNull;
		Datum		test;
		bool		requiredSameDir = false,
					requiredOppositeDirOnly = false;

		/*
		 * Check if the key is required in the current scan direction, in the
		 * opposite scan direction _only_, or in neither direction
		 */
		if (((key->sk_flags & SK_BT_REQFWD) && ScanDirectionIsForward(dir)) ||
			((key->sk_flags & SK_BT_REQBKWD) && ScanDirectionIsBackward(dir)))
			requiredSameDir = true;
		else if (((key->sk_flags & SK_BT_REQFWD) && ScanDirectionIsBackward(dir)) ||
				 ((key->sk_flags & SK_BT_REQBKWD) && ScanDirectionIsForward(dir)))
			requiredOppositeDirOnly = true;

		/*
		 * If the caller told us the *continuescan flag is known to be true
		 * for the last item on the page, then we know the keys required for
		 * the current direction scan should be matched.  Otherwise, the
		 * *continuescan flag would be set for the current item and
		 * subsequently the last item on the page accordingly.
		 *
		 * If the key is required for the opposite direction scan, we can skip
		 * the check if the caller tells us there was already at least one
		 * matching item on the page. Also, we require the *continuescan flag
		 * to be true for the last item on the page to know there are no
		 * NULLs.
		 *
		 * Both cases above work except for the row keys, where NULLs could be
		 * found in the middle of matching values.
		 */
		if ((requiredSameDir || (requiredOppositeDirOnly && haveFirstMatch)) &&
			!(key->sk_flags & SK_ROW_HEADER) && continuescanPrechecked)
			continue;

		if (key->sk_attno > tupnatts)
		{
			/*
			 * This attribute is truncated (must be high key).  The value for
			 * this attribute in the first non-pivot tuple on the page to the
			 * right could be any possible value.  Assume that truncated
			 * attribute passes the qual.
			 */
			Assert(BTreeTupleIsPivot(tuple));
			continue;
		}

		/* row-comparison keys need special processing */
		if (key->sk_flags & SK_ROW_HEADER)
		{
			if (_bt_check_rowcompare(key, tuple, tupnatts, tupdesc, dir,
									 continuescan))
				continue;
			return false;
		}

		datum = index_getattr(tuple,
							  key->sk_attno,
							  tupdesc,
							  &isNull);

		if (key->sk_flags & SK_ISNULL)
		{
			/* Handle IS NULL/NOT NULL tests */
			if (key->sk_flags & SK_SEARCHNULL)
			{
				if (isNull)
					continue;	/* tuple satisfies this qual */
			}
			else
			{
				Assert(key->sk_flags & SK_SEARCHNOTNULL);
				if (!isNull)
					continue;	/* tuple satisfies this qual */
			}

			/*
			 * Tuple fails this qual.  If it's a required qual for the current
			 * scan direction, then we can conclude no further tuples will
			 * pass, either.
			 */
			if (requiredSameDir)
				*continuescan = false;

			/*
			 * In any case, this indextuple doesn't match the qual.
			 */
			return false;
		}

		if (isNull)
		{
			if (key->sk_flags & SK_BT_NULLS_FIRST)
			{
				/*
				 * Since NULLs are sorted before non-NULLs, we know we have
				 * reached the lower limit of the range of values for this
				 * index attr.  On a backward scan, we can stop if this qual
				 * is one of the "must match" subset.  We can stop regardless
				 * of whether the qual is > or <, so long as it's required,
				 * because it's not possible for any future tuples to pass. On
				 * a forward scan, however, we must keep going, because we may
				 * have initially positioned to the start of the index.
				 */
				if ((key->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)) &&
					ScanDirectionIsBackward(dir))
					*continuescan = false;
			}
			else
			{
				/*
				 * Since NULLs are sorted after non-NULLs, we know we have
				 * reached the upper limit of the range of values for this
				 * index attr.  On a forward scan, we can stop if this qual is
				 * one of the "must match" subset.  We can stop regardless of
				 * whether the qual is > or <, so long as it's required,
				 * because it's not possible for any future tuples to pass. On
				 * a backward scan, however, we must keep going, because we
				 * may have initially positioned to the end of the index.
				 */
				if ((key->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)) &&
					ScanDirectionIsForward(dir))
					*continuescan = false;
			}

			/*
			 * In any case, this indextuple doesn't match the qual.
			 */
			return false;
		}

		/*
		 * Apply the key checking function.  When the key is required for
		 * opposite-direction scans it must be an inequality satisfied by
		 * _bt_first(), barring NULLs, which we just checked a moment ago.
		 *
		 * (Also can't apply this optimization with scans that use arrays,
		 * since _bt_advance_array_keys() sometimes allows the scan to see a
		 * few tuples from before the would-be _bt_first() starting position
		 * for the scan's just-advanced array keys.)
		 *
		 * Even required equality quals (that can't use this optimization due
		 * to being required in both scan directions) rely on the assumption
		 * that _bt_first() will always use the quals for initial positioning
		 * purposes.  We stop the scan as soon as any required equality qual
		 * fails, so it had better only happen at the end of equal tuples in
		 * the current scan direction (never at the start of equal tuples).
		 * See comments in _bt_first().
		 *
		 * (The required equality quals issue also has specific implications
		 * for scans that use arrays.  They sometimes perform a linear search
		 * of remaining unscanned tuples, forcing the primitive index scan to
		 * continue until it locates tuples >= the scan's new array keys.)
		 */
		if (!(requiredOppositeDirOnly && haveFirstMatch) || numArrayKeys)
		{
			test = FunctionCall2Coll(&key->sk_func, key->sk_collation,
									 datum, key->sk_argument);
		}
		else
		{
			test = true;
			Assert(test == FunctionCall2Coll(&key->sk_func, key->sk_collation,
											 datum, key->sk_argument));
		}

		if (!DatumGetBool(test))
		{
			/*
			 * Tuple fails this qual.  If it's a required qual for the current
			 * scan direction, then we can conclude no further tuples will
			 * pass, either.
			 */
			if (requiredSameDir)
				*continuescan = false;

			/*
			 * Always set continuescan=false for equality-type array keys that
			 * don't pass -- even for an array scan key not marked required.
			 *
			 * A non-required scan key (array or otherwise) can never actually
			 * terminate the scan.  It's just convenient for callers to treat
			 * continuescan=false as a signal that it might be time to advance
			 * the array keys, independent of whether they're required or not.
			 * (Even setting continuescan=false with a required scan key won't
			 * usually end a scan that uses arrays.)
			 */
			if (numArrayKeys && (key->sk_flags & SK_SEARCHARRAY) &&
				key->sk_strategy == BTEqualStrategyNumber)
				*continuescan = false;

			/*
			 * In any case, this indextuple doesn't match the qual.
			 */
			return false;
		}
	}

	/* If we get here, the tuple passes all index quals. */
	return true;
}

/*
 * Test whether an indextuple satisfies a row-comparison scan condition.
 *
 * Return true if so, false if not.  If not, also clear *continuescan if
 * it's not possible for any future tuples in the current scan direction
 * to pass the qual.
 *
 * This is a subroutine for _bt_checkkeys/_bt_check_compare.
 */
static bool
_bt_check_rowcompare(ScanKey skey, IndexTuple tuple, int tupnatts,
					 TupleDesc tupdesc, ScanDirection dir, bool *continuescan)
{
	ScanKey		subkey = (ScanKey) DatumGetPointer(skey->sk_argument);
	int32		cmpresult = 0;
	bool		result;

	/* First subkey should be same as the header says */
	Assert(subkey->sk_attno == skey->sk_attno);

	/* Loop over columns of the row condition */
	for (;;)
	{
		Datum		datum;
		bool		isNull;

		Assert(subkey->sk_flags & SK_ROW_MEMBER);

		if (subkey->sk_attno > tupnatts)
		{
			/*
			 * This attribute is truncated (must be high key).  The value for
			 * this attribute in the first non-pivot tuple on the page to the
			 * right could be any possible value.  Assume that truncated
			 * attribute passes the qual.
			 */
			Assert(BTreeTupleIsPivot(tuple));
			cmpresult = 0;
			if (subkey->sk_flags & SK_ROW_END)
				break;
			subkey++;
			continue;
		}

		datum = index_getattr(tuple,
							  subkey->sk_attno,
							  tupdesc,
							  &isNull);

		if (isNull)
		{
			if (subkey->sk_flags & SK_BT_NULLS_FIRST)
			{
				/*
				 * Since NULLs are sorted before non-NULLs, we know we have
				 * reached the lower limit of the range of values for this
				 * index attr.  On a backward scan, we can stop if this qual
				 * is one of the "must match" subset.  We can stop regardless
				 * of whether the qual is > or <, so long as it's required,
				 * because it's not possible for any future tuples to pass. On
				 * a forward scan, however, we must keep going, because we may
				 * have initially positioned to the start of the index.
				 */
				if ((subkey->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)) &&
					ScanDirectionIsBackward(dir))
					*continuescan = false;
			}
			else
			{
				/*
				 * Since NULLs are sorted after non-NULLs, we know we have
				 * reached the upper limit of the range of values for this
				 * index attr.  On a forward scan, we can stop if this qual is
				 * one of the "must match" subset.  We can stop regardless of
				 * whether the qual is > or <, so long as it's required,
				 * because it's not possible for any future tuples to pass. On
				 * a backward scan, however, we must keep going, because we
				 * may have initially positioned to the end of the index.
				 */
				if ((subkey->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)) &&
					ScanDirectionIsForward(dir))
					*continuescan = false;
			}

			/*
			 * In any case, this indextuple doesn't match the qual.
			 */
			return false;
		}

		if (subkey->sk_flags & SK_ISNULL)
		{
			/*
			 * Unlike the simple-scankey case, this isn't a disallowed case.
			 * But it can never match.  If all the earlier row comparison
			 * columns are required for the scan direction, we can stop the
			 * scan, because there can't be another tuple that will succeed.
			 */
			if (subkey != (ScanKey) DatumGetPointer(skey->sk_argument))
				subkey--;
			if ((subkey->sk_flags & SK_BT_REQFWD) &&
				ScanDirectionIsForward(dir))
				*continuescan = false;
			else if ((subkey->sk_flags & SK_BT_REQBKWD) &&
					 ScanDirectionIsBackward(dir))
				*continuescan = false;
			return false;
		}

		/* Perform the test --- three-way comparison not bool operator */
		cmpresult = DatumGetInt32(FunctionCall2Coll(&subkey->sk_func,
													subkey->sk_collation,
													datum,
													subkey->sk_argument));

		if (subkey->sk_flags & SK_BT_DESC)
			INVERT_COMPARE_RESULT(cmpresult);

		/* Done comparing if unequal, else advance to next column */
		if (cmpresult != 0)
			break;

		if (subkey->sk_flags & SK_ROW_END)
			break;
		subkey++;
	}

	/*
	 * At this point cmpresult indicates the overall result of the row
	 * comparison, and subkey points to the deciding column (or the last
	 * column if the result is "=").
	 */
	switch (subkey->sk_strategy)
	{
			/* EQ and NE cases aren't allowed here */
		case BTLessStrategyNumber:
			result = (cmpresult < 0);
			break;
		case BTLessEqualStrategyNumber:
			result = (cmpresult <= 0);
			break;
		case BTGreaterEqualStrategyNumber:
			result = (cmpresult >= 0);
			break;
		case BTGreaterStrategyNumber:
			result = (cmpresult > 0);
			break;
		default:
			elog(ERROR, "unrecognized RowCompareType: %d",
				 (int) subkey->sk_strategy);
			result = 0;			/* keep compiler quiet */
			break;
	}

	if (!result)
	{
		/*
		 * Tuple fails this qual.  If it's a required qual for the current
		 * scan direction, then we can conclude no further tuples will pass,
		 * either.  Note we have to look at the deciding column, not
		 * necessarily the first or last column of the row condition.
		 */
		if ((subkey->sk_flags & SK_BT_REQFWD) &&
			ScanDirectionIsForward(dir))
			*continuescan = false;
		else if ((subkey->sk_flags & SK_BT_REQBKWD) &&
				 ScanDirectionIsBackward(dir))
			*continuescan = false;
	}

	return result;
}

/*
 * _bt_killitems - set LP_DEAD state for items an indexscan caller has
 * told us were killed
 *
 * scan->opaque, referenced locally through so, contains information about the
 * current page and killed tuples thereon (generally, this should only be
 * called if so->numKilled > 0).
 *
 * The caller does not have a lock on the page and may or may not have the
 * page pinned in a buffer.  Note that read-lock is sufficient for setting
 * LP_DEAD status (which is only a hint).
 *
 * We match items by heap TID before assuming they are the right ones to
 * delete.  We cope with cases where items have moved right due to insertions.
 * If an item has moved off the current page due to a split, we'll fail to
 * find it and do nothing (this is not an error case --- we assume the item
 * will eventually get marked in a future indexscan).
 *
 * Note that if we hold a pin on the target page continuously from initially
 * reading the items until applying this function, VACUUM cannot have deleted
 * any items from the page, and so there is no need to search left from the
 * recorded offset.  (This observation also guarantees that the item is still
 * the right one to delete, which might otherwise be questionable since heap
 * TIDs can get recycled.)	This holds true even if the page has been modified
 * by inserts and page splits, so there is no need to consult the LSN.
 *
 * If the pin was released after reading the page, then we re-read it.  If it
 * has been modified since we read it (as determined by the LSN), we dare not
 * flag any entries because it is possible that the old entry was vacuumed
 * away and the TID was re-used by a completely different heap tuple.
 */
void
_bt_killitems(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	Page		page;
	BTPageOpaque opaque;
	OffsetNumber minoff;
	OffsetNumber maxoff;
	int			i;
	int			numKilled = so->numKilled;
	bool		killedsomething = false;
	bool		droppedpin PG_USED_FOR_ASSERTS_ONLY;

	Assert(BTScanPosIsValid(so->currPos));

	/*
	 * Always reset the scan state, so we don't look for same items on other
	 * pages.
	 */
	so->numKilled = 0;

	if (BTScanPosIsPinned(so->currPos))
	{
		/*
		 * We have held the pin on this page since we read the index tuples,
		 * so all we need to do is lock it.  The pin will have prevented
		 * re-use of any TID on the page, so there is no need to check the
		 * LSN.
		 */
		droppedpin = false;
		_bt_lockbuf(scan->indexRelation, so->currPos.buf, BT_READ);

		page = BufferGetPage(so->currPos.buf);
	}
	else
	{
		Buffer		buf;

		droppedpin = true;
		/* Attempt to re-read the buffer, getting pin and lock. */
		buf = _bt_getbuf(scan->indexRelation, so->currPos.currPage, BT_READ);

		page = BufferGetPage(buf);
		if (BufferGetLSNAtomic(buf) == so->currPos.lsn)
			so->currPos.buf = buf;
		else
		{
			/* Modified while not pinned means hinting is not safe. */
			_bt_relbuf(scan->indexRelation, buf);
			return;
		}
	}

	opaque = BTPageGetOpaque(page);
	minoff = P_FIRSTDATAKEY(opaque);
	maxoff = PageGetMaxOffsetNumber(page);

	for (i = 0; i < numKilled; i++)
	{
		int			itemIndex = so->killedItems[i];
		BTScanPosItem *kitem = &so->currPos.items[itemIndex];
		OffsetNumber offnum = kitem->indexOffset;

		Assert(itemIndex >= so->currPos.firstItem &&
			   itemIndex <= so->currPos.lastItem);
		if (offnum < minoff)
			continue;			/* pure paranoia */
		while (offnum <= maxoff)
		{
			ItemId		iid = PageGetItemId(page, offnum);
			IndexTuple	ituple = (IndexTuple) PageGetItem(page, iid);
			bool		killtuple = false;

			if (BTreeTupleIsPosting(ituple))
			{
				int			pi = i + 1;
				int			nposting = BTreeTupleGetNPosting(ituple);
				int			j;

				/*
				 * We rely on the convention that heap TIDs in the scanpos
				 * items array are stored in ascending heap TID order for a
				 * group of TIDs that originally came from a posting list
				 * tuple.  This convention even applies during backwards
				 * scans, where returning the TIDs in descending order might
				 * seem more natural.  This is about effectiveness, not
				 * correctness.
				 *
				 * Note that the page may have been modified in almost any way
				 * since we first read it (in the !droppedpin case), so it's
				 * possible that this posting list tuple wasn't a posting list
				 * tuple when we first encountered its heap TIDs.
				 */
				for (j = 0; j < nposting; j++)
				{
					ItemPointer item = BTreeTupleGetPostingN(ituple, j);

					if (!ItemPointerEquals(item, &kitem->heapTid))
						break;	/* out of posting list loop */

					/*
					 * kitem must have matching offnum when heap TIDs match,
					 * though only in the common case where the page can't
					 * have been concurrently modified
					 */
					Assert(kitem->indexOffset == offnum || !droppedpin);

					/*
					 * Read-ahead to later kitems here.
					 *
					 * We rely on the assumption that not advancing kitem here
					 * will prevent us from considering the posting list tuple
					 * fully dead by not matching its next heap TID in next
					 * loop iteration.
					 *
					 * If, on the other hand, this is the final heap TID in
					 * the posting list tuple, then tuple gets killed
					 * regardless (i.e. we handle the case where the last
					 * kitem is also the last heap TID in the last index tuple
					 * correctly -- posting tuple still gets killed).
					 */
					if (pi < numKilled)
						kitem = &so->currPos.items[so->killedItems[pi++]];
				}

				/*
				 * Don't bother advancing the outermost loop's int iterator to
				 * avoid processing killed items that relate to the same
				 * offnum/posting list tuple.  This micro-optimization hardly
				 * seems worth it.  (Further iterations of the outermost loop
				 * will fail to match on this same posting list's first heap
				 * TID instead, so we'll advance to the next offnum/index
				 * tuple pretty quickly.)
				 */
				if (j == nposting)
					killtuple = true;
			}
			else if (ItemPointerEquals(&ituple->t_tid, &kitem->heapTid))
				killtuple = true;

			/*
			 * Mark index item as dead, if it isn't already.  Since this
			 * happens while holding a buffer lock possibly in shared mode,
			 * it's possible that multiple processes attempt to do this
			 * simultaneously, leading to multiple full-page images being sent
			 * to WAL (if wal_log_hints or data checksums are enabled), which
			 * is undesirable.
			 */
			if (killtuple && !ItemIdIsDead(iid))
			{
				/* found the item/all posting list items */
				ItemIdMarkDead(iid);
				killedsomething = true;
				break;			/* out of inner search loop */
			}
			offnum = OffsetNumberNext(offnum);
		}
	}

	/*
	 * Since this can be redone later if needed, mark as dirty hint.
	 *
	 * Whenever we mark anything LP_DEAD, we also set the page's
	 * BTP_HAS_GARBAGE flag, which is likewise just a hint.  (Note that we
	 * only rely on the page-level flag in !heapkeyspace indexes.)
	 */
	if (killedsomething)
	{
		opaque->btpo_flags |= BTP_HAS_GARBAGE;
		MarkBufferDirtyHint(so->currPos.buf, true);
	}

	_bt_unlockbuf(scan->indexRelation, so->currPos.buf);
}


/*
 * The following routines manage a shared-memory area in which we track
 * assignment of "vacuum cycle IDs" to currently-active btree vacuuming
 * operations.  There is a single counter which increments each time we
 * start a vacuum to assign it a cycle ID.  Since multiple vacuums could
 * be active concurrently, we have to track the cycle ID for each active
 * vacuum; this requires at most MaxBackends entries (usually far fewer).
 * We assume at most one vacuum can be active for a given index.
 *
 * Access to the shared memory area is controlled by BtreeVacuumLock.
 * In principle we could use a separate lmgr locktag for each index,
 * but a single LWLock is much cheaper, and given the short time that
 * the lock is ever held, the concurrency hit should be minimal.
 */

typedef struct BTOneVacInfo
{
	LockRelId	relid;			/* global identifier of an index */
	BTCycleId	cycleid;		/* cycle ID for its active VACUUM */
} BTOneVacInfo;

typedef struct BTVacInfo
{
	BTCycleId	cycle_ctr;		/* cycle ID most recently assigned */
	int			num_vacuums;	/* number of currently active VACUUMs */
	int			max_vacuums;	/* allocated length of vacuums[] array */
	BTOneVacInfo vacuums[FLEXIBLE_ARRAY_MEMBER];
} BTVacInfo;

static BTVacInfo *btvacinfo;


/*
 * _bt_vacuum_cycleid --- get the active vacuum cycle ID for an index,
 *		or zero if there is no active VACUUM
 *
 * Note: for correct interlocking, the caller must already hold pin and
 * exclusive lock on each buffer it will store the cycle ID into.  This
 * ensures that even if a VACUUM starts immediately afterwards, it cannot
 * process those pages until the page split is complete.
 */
BTCycleId
_bt_vacuum_cycleid(Relation rel)
{
	BTCycleId	result = 0;
	int			i;

	/* Share lock is enough since this is a read-only operation */
	LWLockAcquire(BtreeVacuumLock, LW_SHARED);

	for (i = 0; i < btvacinfo->num_vacuums; i++)
	{
		BTOneVacInfo *vac = &btvacinfo->vacuums[i];

		if (vac->relid.relId == rel->rd_lockInfo.lockRelId.relId &&
			vac->relid.dbId == rel->rd_lockInfo.lockRelId.dbId)
		{
			result = vac->cycleid;
			break;
		}
	}

	LWLockRelease(BtreeVacuumLock);
	return result;
}

/*
 * _bt_start_vacuum --- assign a cycle ID to a just-starting VACUUM operation
 *
 * Note: the caller must guarantee that it will eventually call
 * _bt_end_vacuum, else we'll permanently leak an array slot.  To ensure
 * that this happens even in elog(FATAL) scenarios, the appropriate coding
 * is not just a PG_TRY, but
 *		PG_ENSURE_ERROR_CLEANUP(_bt_end_vacuum_callback, PointerGetDatum(rel))
 */
BTCycleId
_bt_start_vacuum(Relation rel)
{
	BTCycleId	result;
	int			i;
	BTOneVacInfo *vac;

	LWLockAcquire(BtreeVacuumLock, LW_EXCLUSIVE);

	/*
	 * Assign the next cycle ID, being careful to avoid zero as well as the
	 * reserved high values.
	 */
	result = ++(btvacinfo->cycle_ctr);
	if (result == 0 || result > MAX_BT_CYCLE_ID)
		result = btvacinfo->cycle_ctr = 1;

	/* Let's just make sure there's no entry already for this index */
	for (i = 0; i < btvacinfo->num_vacuums; i++)
	{
		vac = &btvacinfo->vacuums[i];
		if (vac->relid.relId == rel->rd_lockInfo.lockRelId.relId &&
			vac->relid.dbId == rel->rd_lockInfo.lockRelId.dbId)
		{
			/*
			 * Unlike most places in the backend, we have to explicitly
			 * release our LWLock before throwing an error.  This is because
			 * we expect _bt_end_vacuum() to be called before transaction
			 * abort cleanup can run to release LWLocks.
			 */
			LWLockRelease(BtreeVacuumLock);
			elog(ERROR, "multiple active vacuums for index \"%s\"",
				 RelationGetRelationName(rel));
		}
	}

	/* OK, add an entry */
	if (btvacinfo->num_vacuums >= btvacinfo->max_vacuums)
	{
		LWLockRelease(BtreeVacuumLock);
		elog(ERROR, "out of btvacinfo slots");
	}
	vac = &btvacinfo->vacuums[btvacinfo->num_vacuums];
	vac->relid = rel->rd_lockInfo.lockRelId;
	vac->cycleid = result;
	btvacinfo->num_vacuums++;

	LWLockRelease(BtreeVacuumLock);
	return result;
}

/*
 * _bt_end_vacuum --- mark a btree VACUUM operation as done
 *
 * Note: this is deliberately coded not to complain if no entry is found;
 * this allows the caller to put PG_TRY around the start_vacuum operation.
 */
void
_bt_end_vacuum(Relation rel)
{
	int			i;

	LWLockAcquire(BtreeVacuumLock, LW_EXCLUSIVE);

	/* Find the array entry */
	for (i = 0; i < btvacinfo->num_vacuums; i++)
	{
		BTOneVacInfo *vac = &btvacinfo->vacuums[i];

		if (vac->relid.relId == rel->rd_lockInfo.lockRelId.relId &&
			vac->relid.dbId == rel->rd_lockInfo.lockRelId.dbId)
		{
			/* Remove it by shifting down the last entry */
			*vac = btvacinfo->vacuums[btvacinfo->num_vacuums - 1];
			btvacinfo->num_vacuums--;
			break;
		}
	}

	LWLockRelease(BtreeVacuumLock);
}

/*
 * _bt_end_vacuum wrapped as an on_shmem_exit callback function
 */
void
_bt_end_vacuum_callback(int code, Datum arg)
{
	_bt_end_vacuum((Relation) DatumGetPointer(arg));
}

/*
 * BTreeShmemSize --- report amount of shared memory space needed
 */
Size
BTreeShmemSize(void)
{
	Size		size;

	size = offsetof(BTVacInfo, vacuums);
	size = add_size(size, mul_size(MaxBackends, sizeof(BTOneVacInfo)));
	return size;
}

/*
 * BTreeShmemInit --- initialize this module's shared memory
 */
void
BTreeShmemInit(void)
{
	bool		found;

	btvacinfo = (BTVacInfo *) ShmemInitStruct("BTree Vacuum State",
											  BTreeShmemSize(),
											  &found);

	if (!IsUnderPostmaster)
	{
		/* Initialize shared memory area */
		Assert(!found);

		/*
		 * It doesn't really matter what the cycle counter starts at, but
		 * having it always start the same doesn't seem good.  Seed with
		 * low-order bits of time() instead.
		 */
		btvacinfo->cycle_ctr = (BTCycleId) time(NULL);

		btvacinfo->num_vacuums = 0;
		btvacinfo->max_vacuums = MaxBackends;
	}
	else
		Assert(found);
}

bytea *
btoptions(Datum reloptions, bool validate)
{
	static const relopt_parse_elt tab[] = {
		{"fillfactor", RELOPT_TYPE_INT, offsetof(BTOptions, fillfactor)},
		{"vacuum_cleanup_index_scale_factor", RELOPT_TYPE_REAL,
		offsetof(BTOptions, vacuum_cleanup_index_scale_factor)},
		{"deduplicate_items", RELOPT_TYPE_BOOL,
		offsetof(BTOptions, deduplicate_items)}
	};

	return (bytea *) build_reloptions(reloptions, validate,
									  RELOPT_KIND_BTREE,
									  sizeof(BTOptions),
									  tab, lengthof(tab));
}

/*
 *	btproperty() -- Check boolean properties of indexes.
 *
 * This is optional, but handling AMPROP_RETURNABLE here saves opening the rel
 * to call btcanreturn.
 */
bool
btproperty(Oid index_oid, int attno,
		   IndexAMProperty prop, const char *propname,
		   bool *res, bool *isnull)
{
	switch (prop)
	{
		case AMPROP_RETURNABLE:
			/* answer only for columns, not AM or whole index */
			if (attno == 0)
				return false;
			/* otherwise, btree can always return data */
			*res = true;
			return true;

		default:
			return false;		/* punt to generic code */
	}
}

/*
 *	btbuildphasename() -- Return name of index build phase.
 */
char *
btbuildphasename(int64 phasenum)
{
	switch (phasenum)
	{
		case PROGRESS_CREATEIDX_SUBPHASE_INITIALIZE:
			return "initializing";
		case PROGRESS_BTREE_PHASE_INDEXBUILD_TABLESCAN:
			return "scanning table";
		case PROGRESS_BTREE_PHASE_PERFORMSORT_1:
			return "sorting live tuples";
		case PROGRESS_BTREE_PHASE_PERFORMSORT_2:
			return "sorting dead tuples";
		case PROGRESS_BTREE_PHASE_LEAF_LOAD:
			return "loading tuples in tree";
		default:
			return NULL;
	}
}

/*
 *	_bt_truncate() -- create tuple without unneeded suffix attributes.
 *
 * Returns truncated pivot index tuple allocated in caller's memory context,
 * with key attributes copied from caller's firstright argument.  If rel is
 * an INCLUDE index, non-key attributes will definitely be truncated away,
 * since they're not part of the key space.  More aggressive suffix
 * truncation can take place when it's clear that the returned tuple does not
 * need one or more suffix key attributes.  We only need to keep firstright
 * attributes up to and including the first non-lastleft-equal attribute.
 * Caller's insertion scankey is used to compare the tuples; the scankey's
 * argument values are not considered here.
 *
 * Note that returned tuple's t_tid offset will hold the number of attributes
 * present, so the original item pointer offset is not represented.  Caller
 * should only change truncated tuple's downlink.  Note also that truncated
 * key attributes are treated as containing "minus infinity" values by
 * _bt_compare().
 *
 * In the worst case (when a heap TID must be appended to distinguish lastleft
 * from firstright), the size of the returned tuple is the size of firstright
 * plus the size of an additional MAXALIGN()'d item pointer.  This guarantee
 * is important, since callers need to stay under the 1/3 of a page
 * restriction on tuple size.  If this routine is ever taught to truncate
 * within an attribute/datum, it will need to avoid returning an enlarged
 * tuple to caller when truncation + TOAST compression ends up enlarging the
 * final datum.
 */
IndexTuple
_bt_truncate(Relation rel, IndexTuple lastleft, IndexTuple firstright,
			 BTScanInsert itup_key)
{
	TupleDesc	itupdesc = RelationGetDescr(rel);
	int16		nkeyatts = IndexRelationGetNumberOfKeyAttributes(rel);
	int			keepnatts;
	IndexTuple	pivot;
	IndexTuple	tidpivot;
	ItemPointer pivotheaptid;
	Size		newsize;

	/*
	 * We should only ever truncate non-pivot tuples from leaf pages.  It's
	 * never okay to truncate when splitting an internal page.
	 */
	Assert(!BTreeTupleIsPivot(lastleft) && !BTreeTupleIsPivot(firstright));

	/* Determine how many attributes must be kept in truncated tuple */
	keepnatts = _bt_keep_natts(rel, lastleft, firstright, itup_key);

#ifdef DEBUG_NO_TRUNCATE
	/* Force truncation to be ineffective for testing purposes */
	keepnatts = nkeyatts + 1;
#endif

	pivot = index_truncate_tuple(itupdesc, firstright,
								 Min(keepnatts, nkeyatts));

	if (BTreeTupleIsPosting(pivot))
	{
		/*
		 * index_truncate_tuple() just returns a straight copy of firstright
		 * when it has no attributes to truncate.  When that happens, we may
		 * need to truncate away a posting list here instead.
		 */
		Assert(keepnatts == nkeyatts || keepnatts == nkeyatts + 1);
		Assert(IndexRelationGetNumberOfAttributes(rel) == nkeyatts);
		pivot->t_info &= ~INDEX_SIZE_MASK;
		pivot->t_info |= MAXALIGN(BTreeTupleGetPostingOffset(firstright));
	}

	/*
	 * If there is a distinguishing key attribute within pivot tuple, we're
	 * done
	 */
	if (keepnatts <= nkeyatts)
	{
		BTreeTupleSetNAtts(pivot, keepnatts, false);
		return pivot;
	}

	/*
	 * We have to store a heap TID in the new pivot tuple, since no non-TID
	 * key attribute value in firstright distinguishes the right side of the
	 * split from the left side.  nbtree conceptualizes this case as an
	 * inability to truncate away any key attributes, since heap TID is
	 * treated as just another key attribute (despite lacking a pg_attribute
	 * entry).
	 *
	 * Use enlarged space that holds a copy of pivot.  We need the extra space
	 * to store a heap TID at the end (using the special pivot tuple
	 * representation).  Note that the original pivot already has firstright's
	 * possible posting list/non-key attribute values removed at this point.
	 */
	newsize = MAXALIGN(IndexTupleSize(pivot)) + MAXALIGN(sizeof(ItemPointerData));
	tidpivot = palloc0(newsize);
	memcpy(tidpivot, pivot, MAXALIGN(IndexTupleSize(pivot)));
	/* Cannot leak memory here */
	pfree(pivot);

	/*
	 * Store all of firstright's key attribute values plus a tiebreaker heap
	 * TID value in enlarged pivot tuple
	 */
	tidpivot->t_info &= ~INDEX_SIZE_MASK;
	tidpivot->t_info |= newsize;
	BTreeTupleSetNAtts(tidpivot, nkeyatts, true);
	pivotheaptid = BTreeTupleGetHeapTID(tidpivot);

	/*
	 * Lehman & Yao use lastleft as the leaf high key in all cases, but don't
	 * consider suffix truncation.  It seems like a good idea to follow that
	 * example in cases where no truncation takes place -- use lastleft's heap
	 * TID.  (This is also the closest value to negative infinity that's
	 * legally usable.)
	 */
	ItemPointerCopy(BTreeTupleGetMaxHeapTID(lastleft), pivotheaptid);

	/*
	 * We're done.  Assert() that heap TID invariants hold before returning.
	 *
	 * Lehman and Yao require that the downlink to the right page, which is to
	 * be inserted into the parent page in the second phase of a page split be
	 * a strict lower bound on items on the right page, and a non-strict upper
	 * bound for items on the left page.  Assert that heap TIDs follow these
	 * invariants, since a heap TID value is apparently needed as a
	 * tiebreaker.
	 */
#ifndef DEBUG_NO_TRUNCATE
	Assert(ItemPointerCompare(BTreeTupleGetMaxHeapTID(lastleft),
							  BTreeTupleGetHeapTID(firstright)) < 0);
	Assert(ItemPointerCompare(pivotheaptid,
							  BTreeTupleGetHeapTID(lastleft)) >= 0);
	Assert(ItemPointerCompare(pivotheaptid,
							  BTreeTupleGetHeapTID(firstright)) < 0);
#else

	/*
	 * Those invariants aren't guaranteed to hold for lastleft + firstright
	 * heap TID attribute values when they're considered here only because
	 * DEBUG_NO_TRUNCATE is defined (a heap TID is probably not actually
	 * needed as a tiebreaker).  DEBUG_NO_TRUNCATE must therefore use a heap
	 * TID value that always works as a strict lower bound for items to the
	 * right.  In particular, it must avoid using firstright's leading key
	 * attribute values along with lastleft's heap TID value when lastleft's
	 * TID happens to be greater than firstright's TID.
	 */
	ItemPointerCopy(BTreeTupleGetHeapTID(firstright), pivotheaptid);

	/*
	 * Pivot heap TID should never be fully equal to firstright.  Note that
	 * the pivot heap TID will still end up equal to lastleft's heap TID when
	 * that's the only usable value.
	 */
	ItemPointerSetOffsetNumber(pivotheaptid,
							   OffsetNumberPrev(ItemPointerGetOffsetNumber(pivotheaptid)));
	Assert(ItemPointerCompare(pivotheaptid,
							  BTreeTupleGetHeapTID(firstright)) < 0);
#endif

	return tidpivot;
}

/*
 * _bt_keep_natts - how many key attributes to keep when truncating.
 *
 * Caller provides two tuples that enclose a split point.  Caller's insertion
 * scankey is used to compare the tuples; the scankey's argument values are
 * not considered here.
 *
 * This can return a number of attributes that is one greater than the
 * number of key attributes for the index relation.  This indicates that the
 * caller must use a heap TID as a unique-ifier in new pivot tuple.
 */
static int
_bt_keep_natts(Relation rel, IndexTuple lastleft, IndexTuple firstright,
			   BTScanInsert itup_key)
{
	int			nkeyatts = IndexRelationGetNumberOfKeyAttributes(rel);
	TupleDesc	itupdesc = RelationGetDescr(rel);
	int			keepnatts;
	ScanKey		scankey;

	/*
	 * _bt_compare() treats truncated key attributes as having the value minus
	 * infinity, which would break searches within !heapkeyspace indexes.  We
	 * must still truncate away non-key attribute values, though.
	 */
	if (!itup_key->heapkeyspace)
		return nkeyatts;

	scankey = itup_key->scankeys;
	keepnatts = 1;
	for (int attnum = 1; attnum <= nkeyatts; attnum++, scankey++)
	{
		Datum		datum1,
					datum2;
		bool		isNull1,
					isNull2;

		datum1 = index_getattr(lastleft, attnum, itupdesc, &isNull1);
		datum2 = index_getattr(firstright, attnum, itupdesc, &isNull2);

		if (isNull1 != isNull2)
			break;

		if (!isNull1 &&
			DatumGetInt32(FunctionCall2Coll(&scankey->sk_func,
											scankey->sk_collation,
											datum1,
											datum2)) != 0)
			break;

		keepnatts++;
	}

	/*
	 * Assert that _bt_keep_natts_fast() agrees with us in passing.  This is
	 * expected in an allequalimage index.
	 */
	Assert(!itup_key->allequalimage ||
		   keepnatts == _bt_keep_natts_fast(rel, lastleft, firstright));

	return keepnatts;
}

/*
 * _bt_keep_natts_fast - fast bitwise variant of _bt_keep_natts.
 *
 * This is exported so that a candidate split point can have its effect on
 * suffix truncation inexpensively evaluated ahead of time when finding a
 * split location.  A naive bitwise approach to datum comparisons is used to
 * save cycles.
 *
 * The approach taken here usually provides the same answer as _bt_keep_natts
 * will (for the same pair of tuples from a heapkeyspace index), since the
 * majority of btree opclasses can never indicate that two datums are equal
 * unless they're bitwise equal after detoasting.  When an index only has
 * "equal image" columns, routine is guaranteed to give the same result as
 * _bt_keep_natts would.
 *
 * Callers can rely on the fact that attributes considered equal here are
 * definitely also equal according to _bt_keep_natts, even when the index uses
 * an opclass or collation that is not "allequalimage"/deduplication-safe.
 * This weaker guarantee is good enough for nbtsplitloc.c caller, since false
 * negatives generally only have the effect of making leaf page splits use a
 * more balanced split point.
 */
int
_bt_keep_natts_fast(Relation rel, IndexTuple lastleft, IndexTuple firstright)
{
	TupleDesc	itupdesc = RelationGetDescr(rel);
	int			keysz = IndexRelationGetNumberOfKeyAttributes(rel);
	int			keepnatts;

	keepnatts = 1;
	for (int attnum = 1; attnum <= keysz; attnum++)
	{
		Datum		datum1,
					datum2;
		bool		isNull1,
					isNull2;
		Form_pg_attribute att;

		datum1 = index_getattr(lastleft, attnum, itupdesc, &isNull1);
		datum2 = index_getattr(firstright, attnum, itupdesc, &isNull2);
		att = TupleDescAttr(itupdesc, attnum - 1);

		if (isNull1 != isNull2)
			break;

		if (!isNull1 &&
			!datum_image_eq(datum1, datum2, att->attbyval, att->attlen))
			break;

		keepnatts++;
	}

	return keepnatts;
}

/*
 *  _bt_check_natts() -- Verify tuple has expected number of attributes.
 *
 * Returns value indicating if the expected number of attributes were found
 * for a particular offset on page.  This can be used as a general purpose
 * sanity check.
 *
 * Testing a tuple directly with BTreeTupleGetNAtts() should generally be
 * preferred to calling here.  That's usually more convenient, and is always
 * more explicit.  Call here instead when offnum's tuple may be a negative
 * infinity tuple that uses the pre-v11 on-disk representation, or when a low
 * context check is appropriate.  This routine is as strict as possible about
 * what is expected on each version of btree.
 */
bool
_bt_check_natts(Relation rel, bool heapkeyspace, Page page, OffsetNumber offnum)
{
	int16		natts = IndexRelationGetNumberOfAttributes(rel);
	int16		nkeyatts = IndexRelationGetNumberOfKeyAttributes(rel);
	BTPageOpaque opaque = BTPageGetOpaque(page);
	IndexTuple	itup;
	int			tupnatts;

	/*
	 * We cannot reliably test a deleted or half-dead page, since they have
	 * dummy high keys
	 */
	if (P_IGNORE(opaque))
		return true;

	Assert(offnum >= FirstOffsetNumber &&
		   offnum <= PageGetMaxOffsetNumber(page));

	itup = (IndexTuple) PageGetItem(page, PageGetItemId(page, offnum));
	tupnatts = BTreeTupleGetNAtts(itup, rel);

	/* !heapkeyspace indexes do not support deduplication */
	if (!heapkeyspace && BTreeTupleIsPosting(itup))
		return false;

	/* Posting list tuples should never have "pivot heap TID" bit set */
	if (BTreeTupleIsPosting(itup) &&
		(ItemPointerGetOffsetNumberNoCheck(&itup->t_tid) &
		 BT_PIVOT_HEAP_TID_ATTR) != 0)
		return false;

	/* INCLUDE indexes do not support deduplication */
	if (natts != nkeyatts && BTreeTupleIsPosting(itup))
		return false;

	if (P_ISLEAF(opaque))
	{
		if (offnum >= P_FIRSTDATAKEY(opaque))
		{
			/*
			 * Non-pivot tuple should never be explicitly marked as a pivot
			 * tuple
			 */
			if (BTreeTupleIsPivot(itup))
				return false;

			/*
			 * Leaf tuples that are not the page high key (non-pivot tuples)
			 * should never be truncated.  (Note that tupnatts must have been
			 * inferred, even with a posting list tuple, because only pivot
			 * tuples store tupnatts directly.)
			 */
			return tupnatts == natts;
		}
		else
		{
			/*
			 * Rightmost page doesn't contain a page high key, so tuple was
			 * checked above as ordinary leaf tuple
			 */
			Assert(!P_RIGHTMOST(opaque));

			/*
			 * !heapkeyspace high key tuple contains only key attributes. Note
			 * that tupnatts will only have been explicitly represented in
			 * !heapkeyspace indexes that happen to have non-key attributes.
			 */
			if (!heapkeyspace)
				return tupnatts == nkeyatts;

			/* Use generic heapkeyspace pivot tuple handling */
		}
	}
	else						/* !P_ISLEAF(opaque) */
	{
		if (offnum == P_FIRSTDATAKEY(opaque))
		{
			/*
			 * The first tuple on any internal page (possibly the first after
			 * its high key) is its negative infinity tuple.  Negative
			 * infinity tuples are always truncated to zero attributes.  They
			 * are a particular kind of pivot tuple.
			 */
			if (heapkeyspace)
				return tupnatts == 0;

			/*
			 * The number of attributes won't be explicitly represented if the
			 * negative infinity tuple was generated during a page split that
			 * occurred with a version of Postgres before v11.  There must be
			 * a problem when there is an explicit representation that is
			 * non-zero, or when there is no explicit representation and the
			 * tuple is evidently not a pre-pg_upgrade tuple.
			 *
			 * Prior to v11, downlinks always had P_HIKEY as their offset.
			 * Accept that as an alternative indication of a valid
			 * !heapkeyspace negative infinity tuple.
			 */
			return tupnatts == 0 ||
				ItemPointerGetOffsetNumber(&(itup->t_tid)) == P_HIKEY;
		}
		else
		{
			/*
			 * !heapkeyspace downlink tuple with separator key contains only
			 * key attributes.  Note that tupnatts will only have been
			 * explicitly represented in !heapkeyspace indexes that happen to
			 * have non-key attributes.
			 */
			if (!heapkeyspace)
				return tupnatts == nkeyatts;

			/* Use generic heapkeyspace pivot tuple handling */
		}
	}

	/* Handle heapkeyspace pivot tuples (excluding minus infinity items) */
	Assert(heapkeyspace);

	/*
	 * Explicit representation of the number of attributes is mandatory with
	 * heapkeyspace index pivot tuples, regardless of whether or not there are
	 * non-key attributes.
	 */
	if (!BTreeTupleIsPivot(itup))
		return false;

	/* Pivot tuple should not use posting list representation (redundant) */
	if (BTreeTupleIsPosting(itup))
		return false;

	/*
	 * Heap TID is a tiebreaker key attribute, so it cannot be untruncated
	 * when any other key attribute is truncated
	 */
	if (BTreeTupleGetHeapTID(itup) != NULL && tupnatts != nkeyatts)
		return false;

	/*
	 * Pivot tuple must have at least one untruncated key attribute (minus
	 * infinity pivot tuples are the only exception).  Pivot tuples can never
	 * represent that there is a value present for a key attribute that
	 * exceeds pg_index.indnkeyatts for the index.
	 */
	return tupnatts > 0 && tupnatts <= nkeyatts;
}

/*
 *
 *  _bt_check_third_page() -- check whether tuple fits on a btree page at all.
 *
 * We actually need to be able to fit three items on every page, so restrict
 * any one item to 1/3 the per-page available space.  Note that itemsz should
 * not include the ItemId overhead.
 *
 * It might be useful to apply TOAST methods rather than throw an error here.
 * Using out of line storage would break assumptions made by suffix truncation
 * and by contrib/amcheck, though.
 */
void
_bt_check_third_page(Relation rel, Relation heap, bool needheaptidspace,
					 Page page, IndexTuple newtup)
{
	Size		itemsz;
	BTPageOpaque opaque;

	itemsz = MAXALIGN(IndexTupleSize(newtup));

	/* Double check item size against limit */
	if (itemsz <= BTMaxItemSize(page))
		return;

	/*
	 * Tuple is probably too large to fit on page, but it's possible that the
	 * index uses version 2 or version 3, or that page is an internal page, in
	 * which case a slightly higher limit applies.
	 */
	if (!needheaptidspace && itemsz <= BTMaxItemSizeNoHeapTid(page))
		return;

	/*
	 * Internal page insertions cannot fail here, because that would mean that
	 * an earlier leaf level insertion that should have failed didn't
	 */
	opaque = BTPageGetOpaque(page);
	if (!P_ISLEAF(opaque))
		elog(ERROR, "cannot insert oversized tuple of size %zu on internal page of index \"%s\"",
			 itemsz, RelationGetRelationName(rel));

	ereport(ERROR,
			(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
			 errmsg("index row size %zu exceeds btree version %u maximum %zu for index \"%s\"",
					itemsz,
					needheaptidspace ? BTREE_VERSION : BTREE_NOVAC_VERSION,
					needheaptidspace ? BTMaxItemSize(page) :
					BTMaxItemSizeNoHeapTid(page),
					RelationGetRelationName(rel)),
			 errdetail("Index row references tuple (%u,%u) in relation \"%s\".",
					   ItemPointerGetBlockNumber(BTreeTupleGetHeapTID(newtup)),
					   ItemPointerGetOffsetNumber(BTreeTupleGetHeapTID(newtup)),
					   RelationGetRelationName(heap)),
			 errhint("Values larger than 1/3 of a buffer page cannot be indexed.\n"
					 "Consider a function index of an MD5 hash of the value, "
					 "or use full text indexing."),
			 errtableconstraint(heap, RelationGetRelationName(rel))));
}

/*
 * Are all attributes in rel "equality is image equality" attributes?
 *
 * We use each attribute's BTEQUALIMAGE_PROC opclass procedure.  If any
 * opclass either lacks a BTEQUALIMAGE_PROC procedure or returns false, we
 * return false; otherwise we return true.
 *
 * Returned boolean value is stored in index metapage during index builds.
 * Deduplication can only be used when we return true.
 */
bool
_bt_allequalimage(Relation rel, bool debugmessage)
{
	bool		allequalimage = true;

	/* v3 indexes don't support allequalimage */
	return false;

	/* INCLUDE indexes can never support deduplication */
	if (IndexRelationGetNumberOfAttributes(rel) !=
		IndexRelationGetNumberOfKeyAttributes(rel))
		return false;

	for (int i = 0; i < IndexRelationGetNumberOfKeyAttributes(rel); i++)
	{
		Oid			opfamily = rel->rd_opfamily[i];
		Oid			opcintype = rel->rd_opcintype[i];
		Oid			collation = rel->rd_indcollation[i];
		Oid			equalimageproc;

		equalimageproc = get_opfamily_proc(opfamily, opcintype, opcintype,
										   BTEQUALIMAGE_PROC);

		/*
		 * If there is no BTEQUALIMAGE_PROC then deduplication is assumed to
		 * be unsafe.  Otherwise, actually call proc and see what it says.
		 */
		if (!OidIsValid(equalimageproc) ||
			!DatumGetBool(OidFunctionCall1Coll(equalimageproc, collation,
											   ObjectIdGetDatum(opcintype))))
		{
			allequalimage = false;
			break;
		}
	}

	if (debugmessage)
	{
		if (allequalimage)
			elog(DEBUG1, "index \"%s\" can safely use deduplication",
				 RelationGetRelationName(rel));
		else
			elog(DEBUG1, "index \"%s\" cannot use deduplication",
				 RelationGetRelationName(rel));
	}

	return allequalimage;
}
