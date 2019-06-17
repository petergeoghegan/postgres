/*-------------------------------------------------------------------------
 *
 * nbtutils.c
 *	  Utility code for Postgres btree implementation.
 *
 * Portions Copyright (c) 1996-2023, PostgreSQL Global Development Group
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
	FmgrInfo	flinfo;
	Oid			collation;
	bool		reverse;
} BTSortArrayContext;

static Datum _bt_find_extreme_element(IndexScanDesc scan, ScanKey skey,
									  StrategyNumber strat,
									  Datum *elems, int nelems);
static int	_bt_sort_array_elements(IndexScanDesc scan, ScanKey skey,
									bool reverse,
									Datum *elems, int nelems);
static int	_bt_compare_array_elements(const void *a, const void *b, void *arg);
static bool _bt_advance_array_keys_locally(IndexScanDesc scan,
										   IndexTuple tuple, bool final,
										   BTReadPageState *pstate);
static bool _bt_tuple_might_match_next_array_keys(IndexScanDesc scan,
												  IndexTuple tuple,
												  ScanDirection dir);
static bool _bt_compare_scankey_args(IndexScanDesc scan, ScanKey op,
									 ScanKey leftarg, ScanKey rightarg,
									 bool *result);
static bool _bt_fix_scankey_strategy(ScanKey skey, int16 *indoption);
static void _bt_mark_scankey_required(ScanKey skey);
static bool _bt_check_key(Relation rel, BTScanOpaque so, TupleDesc tupdesc, IndexTuple tuple,
						  int tupnatts, ScanDirection dir, bool *continuescan);
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
 *		When itup is a non-pivot tuple, the returned insertion scan key is
 *		suitable for finding a place for it to go on the leaf level.  Pivot
 *		tuples can be used to re-find leaf page with matching high key, but
 *		then caller needs to set scan key's pivotsearch field to true.  This
 *		allows caller to search for a leaf page with a matching high key,
 *		which is usually to the left of the first leaf page a non-pivot match
 *		might appear on.
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
		/* Utility statement callers can set these fields themselves */
		key->heapkeyspace = true;
		key->allequalimage = false;
	}
	key->anynullkeys = false;	/* initial assumption */
	key->nextkey = false;
	key->pivotsearch = false;
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
 * array elements during each primitive indexscan operation.  For inequality
 * array keys, it's sufficient to find the extreme element value and replace
 * the whole array with that scalar value.
 *
 * It's important that we consistently avoid leaving behind SK_SEARCHARRAY
 * inequalities after preprocessing, since _bt_tuple_behind_cur_array_keys
 * expects to be able to treat SK_SEARCHARRAY keys as equality constraints.
 * This makes it possible for the scan to take advantage of naturally occuring
 * locality to avoid continually redescending the index in _bt_first.  We can
 * advance the array keys opportunistically inside _bt_check_array_keys.  This
 * won't affect the externally visible behavior of the scan.
 *
 * In the worst case, the number of primitive index scans will equal the
 * number of array elements (or the product of the number of array keys when
 * there are multiple arrays/columns involved).  It's also possible that the
 * total number of primitive index scans will be far less than that.
 *
 * We always sort and deduplicate arrays up-front for equality array keys.
 * ScalarArrayOpExpr execution need only visit leaf pages that might contain
 * matches exactly once, while preserving the sort order of the index.  This
 * isn't just about performance; it also avoids needing duplicate elimination
 * of matching TIDs (we prefer deduplicating search keys once, up-front).
 * Equality SK_SEARCHARRAY keys are disjuncts that we always process in
 * index/key space order, which makes this general approach feasible.  Every
 * index tuple will match no more than one single distinct combination of
 * equality-constrained keys (array keys and other equality keys).
 *
 * Note: the reason we need so->arrayKeyData, rather than just scribbling
 * on scan->keyData, is that callers are permitted to call btrescan without
 * supplying a new set of scankey data.
 */
void
_bt_preprocess_array_keys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	int			numberOfKeys = scan->numberOfKeys;
	int16	   *indoption = scan->indexRelation->rd_indoption;
	int			numArrayKeys;
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
			if (so->log_btree_verbosity)
			{
				char	   *flags = dump_scankey_flags(cur);

				appendStringInfo(&so->debugstr,
								 "_bt_preprocess_array_keys: SK_SEARCHARRAY input %d: [ flags: %s, attno: %u func OID: %u ]\n",
								 i, flags, cur->sk_attno,
								 cur->sk_func.fn_oid);
				pfree(flags);
			}

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
		if (so->log_btree_verbosity)
			appendStringInfo(&so->debugstr,
							 "_bt_preprocess_array_keys: numArrayKeys == 0 so nothing to do\n");
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
	so->arrayKeyData = (ScanKey) palloc(scan->numberOfKeys * sizeof(ScanKeyData));
	memcpy(so->arrayKeyData,
		   scan->keyData,
		   scan->numberOfKeys * sizeof(ScanKeyData));

	/* Allocate space for per-array data in the workspace context */
	so->arrayKeys = (BTArrayKeyInfo *) palloc0(numArrayKeys * sizeof(BTArrayKeyInfo));

	/* Now process each array key */
	numArrayKeys = 0;
	for (i = 0; i < numberOfKeys; i++)
	{
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
					_bt_find_extreme_element(scan, cur,
											 BTGreaterStrategyNumber,
											 elem_values, num_nonnulls);
				continue;
			case BTEqualStrategyNumber:
				/* proceed with rest of loop */
				break;
			case BTGreaterEqualStrategyNumber:
			case BTGreaterStrategyNumber:
				cur->sk_argument =
					_bt_find_extreme_element(scan, cur,
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
		 * successive primitive indexscans produce data in index order.
		 */
		num_elems = _bt_sort_array_elements(scan, cur,
											(indoption[cur->sk_attno - 1] & INDOPTION_DESC) != 0,
											elem_values, num_nonnulls);

		/*
		 * And set up the BTArrayKeyInfo data.
		 */
		so->arrayKeys[numArrayKeys].scan_key = i;
		so->arrayKeys[numArrayKeys].num_elems = num_elems;
		so->arrayKeys[numArrayKeys].elem_values = elem_values;

		/*
		 * Note: Will report elem_values (array element values for scankey) in
		 * _bt_start_array_keys and _bt_advance_array_keys, rather than doing
		 * that here
		 */
		if (so->log_btree_verbosity)
			appendStringInfo(&so->debugstr,
							 "_bt_preprocess_array_keys: scan_key: %d, num_elems: %d\n",
							 so->arrayKeys[numArrayKeys].scan_key,
							 so->arrayKeys[numArrayKeys].num_elems);

		numArrayKeys++;
	}

	so->numArrayKeys = numArrayKeys;

	if (so->log_btree_verbosity)
		appendStringInfo(&so->debugstr,
						 "                           so->numArrayKeys is %d, so->arrayKeyCount is %d\n",
						 so->numArrayKeys, so->arrayKeyCount);

	MemoryContextSwitchTo(oldContext);
}

/*
 * _bt_find_extreme_element() -- get least or greatest array element
 *
 * scan and skey identify the index column, whose opfamily determines the
 * comparison semantics.  strat should be BTLessStrategyNumber to get the
 * least element, or BTGreaterStrategyNumber to get the greatest.
 */
static Datum
_bt_find_extreme_element(IndexScanDesc scan, ScanKey skey,
						 StrategyNumber strat,
						 Datum *elems, int nelems)
{
	Relation	rel = scan->indexRelation;
	Oid			elemtype,
				cmp_op;
	RegProcedure cmp_proc;
	FmgrInfo	flinfo;
	Datum		result;
	int			i;

	/*
	 * Determine the nominal datatype of the array elements.  We have to
	 * support the convention that sk_subtype == InvalidOid means the opclass
	 * input type; this is a hack to simplify life for ScanKeyInit().
	 */
	elemtype = skey->sk_subtype;
	if (elemtype == InvalidOid)
		elemtype = rel->rd_opcintype[skey->sk_attno - 1];

	/*
	 * Look up the appropriate comparison operator in the opfamily.
	 *
	 * Note: it's possible that this would fail, if the opfamily is
	 * incomplete, but it seems quite unlikely that an opfamily would omit
	 * non-cross-type comparison operators for any datatype that it supports
	 * at all.
	 */
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
 * scan and skey identify the index column, whose opfamily determines the
 * comparison semantics.  If reverse is true, we sort in descending order.
 */
static int
_bt_sort_array_elements(IndexScanDesc scan, ScanKey skey,
						bool reverse,
						Datum *elems, int nelems)
{
	Relation	rel = scan->indexRelation;
	Oid			elemtype;
	RegProcedure cmp_proc;
	BTSortArrayContext cxt;

	if (nelems <= 1)
		return nelems;			/* no work to do */

	/*
	 * Determine the nominal datatype of the array elements.  We have to
	 * support the convention that sk_subtype == InvalidOid means the opclass
	 * input type; this is a hack to simplify life for ScanKeyInit().
	 */
	elemtype = skey->sk_subtype;
	if (elemtype == InvalidOid)
		elemtype = rel->rd_opcintype[skey->sk_attno - 1];

	/*
	 * Look up the appropriate comparison function in the opfamily.
	 *
	 * Note: it's possible that this would fail, if the opfamily is
	 * incomplete, but it seems quite unlikely that an opfamily would omit
	 * non-cross-type support functions for any datatype that it supports at
	 * all.
	 */
	cmp_proc = get_opfamily_proc(rel->rd_opfamily[skey->sk_attno - 1],
								 elemtype,
								 elemtype,
								 BTORDER_PROC);
	if (!RegProcedureIsValid(cmp_proc))
		elog(ERROR, "missing support function %d(%u,%u) in opfamily %u",
			 BTORDER_PROC, elemtype, elemtype,
			 rel->rd_opfamily[skey->sk_attno - 1]);

	/* Sort the array elements */
	fmgr_info(cmp_proc, &cxt.flinfo);
	cxt.collation = skey->sk_collation;
	cxt.reverse = reverse;
	qsort_arg(elems, nelems, sizeof(Datum),
			  _bt_compare_array_elements, &cxt);

	/* Now scan the sorted elements and remove duplicates */
	return qunique_arg(elems, nelems, sizeof(Datum),
					   _bt_compare_array_elements, &cxt);
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

	compare = DatumGetInt32(FunctionCall2Coll(&cxt->flinfo,
											  cxt->collation,
											  da, db));
	if (cxt->reverse)
		INVERT_COMPARE_RESULT(compare);
	return compare;
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
		Oid			typOutput;
		bool		varlenatype;
		char	   *val;

		Assert(curArrayKey->num_elems > 0);
		if (ScanDirectionIsBackward(dir))
			curArrayKey->cur_elem = curArrayKey->num_elems - 1;
		else
			curArrayKey->cur_elem = 0;
		skey->sk_argument = curArrayKey->elem_values[curArrayKey->cur_elem];

		if (so->log_btree_verbosity)
		{
			if (skey->sk_subtype != InvalidOid)
				getTypeOutputInfo(skey->sk_subtype,
								  &typOutput, &varlenatype);
			else
				getTypeOutputInfo(scan->indexRelation->rd_opcintype[i],
								  &typOutput, &varlenatype);
			val = OidOutputFunctionCall(typOutput, skey->sk_argument);
			appendStringInfo(&so->debugstr,
							 "_bt_start_array_keys: cur_elem %u, sk_attno: %d, val: %s [%s, %s]\n",
							 curArrayKey->cur_elem,
							 skey->sk_attno, val,
							 (skey->sk_flags & SK_BT_NULLS_FIRST) != 0 ? "NULLS FIRST" : "NULLS LAST",
							 (skey->sk_flags & SK_BT_DESC) != 0 ? "DESC" : "ASC");
			if (val)
				pfree(val);
		}
	}

	/* Tell _bt_advance_array_keys to advance array keys when called */
	so->arrayKeysStarted = true;
}

/*
 * _bt_advance_array_keys() -- Advance to next set of array elements
 *
 * Returns true if there is another set of values to consider, false if not.
 * On true result, the scankeys are initialized with the next set of values.
 *
 * On false result, local advancement of the array keys has reached the end of
 * each of the arrays for the current scan direction.  Only our btgettuple and
 * btgetbitmap callers should rely on this.
 */
bool
_bt_advance_array_keys(IndexScanDesc scan, ScanDirection dir)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	bool		found = false;
	int			i;

	if (!so->arrayKeysStarted)
		return false;

	/*
	 * We must advance the last array key most quickly, since it will
	 * correspond to the lowest-order index column among the available
	 * qualifications. This is necessary to ensure correct ordering of output
	 * when there are multiple array keys.
	 *
	 * Imagine an odometer (mileage gauge) where the least significant
	 * (rightmost) decimal digit is advanced every time we drive another mile.
	 * This device has a rotary mechanism that displays every integer from 0
	 * through to (say) 9999 exactly once (until we wraparound to 0 again).
	 *
	 * The process by which we advance the current set of array keys works a
	 * little like this.  Each "wheel" of the mechanism is comparable to one
	 * of our arrays.  The digits from each of the 4 wheels are comparable to
	 * individual array elements.  Every call here always increases the least
	 * significant array's current element, and may also need to advance the
	 * current element from all of the higher order arrays.
	 *
	 * You can think of non-array scankeys (which in general must represent
	 * simple equality constraints) for an attribute between two other array
	 * key attributes as a degenerate single-value arrays.  We don't have to
	 * deal with those here because (conceptually) they start with cur_elem 0,
	 * which "advances" by "wrapping back around" to 0 (which is a no-op).
	 * That's why _bt_tuple_behind_cur_array_keys doesn't need to distinguish
	 * between SK_SEARCHARRAY keys and other keys considered to be equality
	 * constraints.
	 */
	for (i = so->numArrayKeys - 1; i >= 0; i--)
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
		{
			Oid			typOutput;
			bool		varlenatype;
			char	   *val;

			if (so->log_btree_verbosity)
			{
				if (skey->sk_subtype != InvalidOid)
					getTypeOutputInfo(skey->sk_subtype,
									  &typOutput, &varlenatype);
				else
					getTypeOutputInfo(scan->indexRelation->rd_opcintype[i],
									  &typOutput, &varlenatype);
				val = OidOutputFunctionCall(typOutput, skey->sk_argument);
				appendStringInfo(&so->debugstr,
								 "_bt_advance_array_keys: cur_elem %u, sk_attno: %d, val: %s [%s, %s]\n",
								 curArrayKey->cur_elem,
								 skey->sk_attno, val,
								 (skey->sk_flags & SK_BT_NULLS_FIRST) != 0 ? "NULLS FIRST" : "NULLS LAST",
								 (skey->sk_flags & SK_BT_DESC) != 0 ? "DESC" : "ASC");
				if (val)
					pfree(val);
			}

			break;
		}
	}

	/* Scan reached the end of its array keys in the current scan direction */
	if (!found)
		so->arrayKeysStarted = false;

	/* advance parallel scan */
	if (scan->parallel_scan != NULL)
		_bt_parallel_advance_array_keys(scan);

	if (!found && so->log_btree_verbosity)
		appendStringInfo(&so->debugstr,
						 "_bt_advance_array_keys: failed to find new cur_elem\n");

	return found;
}

/*
 * Check if we need to advance SK_SEARCHARRAY array keys when _bt_check_key
 * returns false and sets continuescan=false.  It's possible that the tuple
 * will be a match after we advance the array keys.
 *
 * It is often possible for SK_SEARCHARRAY scans to skip another descent that
 * repositions the scan at the beginning of the start of matches for the next
 * set of array keys.  This process is opportunistic.  The technique works out
 * when the end of matches for the current set of array keys is physically
 * close to the start of matches for the next set of array keys, due to
 * naturally occurring locality.  The technique fails when tuples matching the
 * next set of array keys happen to be far away.
 *
 * Returns true when array key advances.  Caller must recheck the tuple that
 * initially set continuescan=false against the new array keys.
 *
 * Returns false when array key has not or cannot advance.
 */
static bool
_bt_advance_array_keys_locally(IndexScanDesc scan, IndexTuple tuple,
							   bool final, BTReadPageState *pstate)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;

	Assert(!pstate->continuescan);
	Assert(so->arrayKeysStarted);

	if (so->disableDynamic)
		return false;

	/*
	 * Search type scan key already indicated that this tuple terminates the
	 * scan in respect of that set of keys (some of which are array elements).
	 *
	 * Is it possible that this tuple will be a match once the array keys are
	 * advanced?
	 */
	if (!_bt_tuple_might_match_next_array_keys(scan, tuple, pstate->dir))
	{
		/*
		 * Tuple definitely won't be returned by _bt_checkkeys, since it
		 * definitely isn't a match for any set of search keys.
		 */
		pstate->continuescan = true;

		if (!pstate->match_for_cur_array_keys &&
			(final || (!pstate->highkeychecked && pstate->highkey)))
		{
			/*
			 * Avoid repeated useless comparisons of non-pivot tuples on the
			 * same page by determining in advance if page high key will be
			 * able to terminate this _bt_first-wise scan.  If it does then
			 * the next time we end up back here (the next time a non-pivot
			 * tuple sets continuescan=false), we'll end the _bt_first-wise
			 * scan there and then, before scanning all remaining non-pivot
			 * tuples.
			 *
			 * TODO Explain the speculative nature of inferring that we're
			 * likely to find more matches on the next page here
			 *
			 * XXX Is this overly reliant on _bt_readpage() getting called
			 * with an offset number below P_FIRSTDATAKEY() in the backwards
			 * scan case, at least when _bt_first chooses backup=true and
			 * nextkey=true?
			 */
			Assert(ScanDirectionIsForward(pstate->dir) || !pstate->highkey);
			Assert(ScanDirectionIsBackward(pstate->dir) || !final);

			pstate->highkeychecked = true;	/* Or a backward scan */

			if (final ||
				!_bt_tuple_might_match_next_array_keys(scan,
													   pstate->highkey,
													   pstate->dir))
			{
				/*
				 * The high key definitely isn't a match for the next set of
				 * array keys, so it's unlikely that we'll find a match in the
				 * right sibling page.  Another _bt_first-wise primitive index
				 * scan will take place for the current set of array keys
				 * instead.  (Or the lowest non-pivot tuple on the page isn't
				 * a match in the backwards scan case.)
				 *
				 * Back up the array keys so that btgettuple or btgetbitmap
				 * won't advance the keys past the now-current set.  This is
				 * safe because we haven't returned any tuples matching this
				 * set of keys.
				 */
				ScanDirection flipdir = -pstate->dir;

				if (_bt_advance_array_keys(scan, flipdir))
					_bt_preprocess_keys(scan);
				else
					Assert(false);

				/* End primitive index scan after all */
				pstate->continuescan = false;
			}
		}

		return false;
	}

	if (!_bt_advance_array_keys(scan, pstate->dir))
	{
		/*
		 * Ran out of array keys to advance the scan to.  The btrescan-wise
		 * scan has ended.
		 */
		Assert(!so->arrayKeysStarted);

		return false;
	}

	/*
	 * Successfully advanced the array keys.
	 *
	 * We'll now need to see what _bt_check_key loop in our caller says about
	 * the same tuple with this new set of keys.
	 *
	 * Note: this is only provisional -- we may yet back out of advancing the
	 * array keys by "deadvancing" them before we leave this leaf page.  It's
	 * important that we have the opportunity to set continuescan=false before
	 * leaving the current leaf page if we're not confident that committing to
	 * the current set of array keys is the right thing to do.
	 */
	_bt_preprocess_keys(scan);

	if (pstate->highkey)
	{
		/* High key precheck might be repeated for new array keys */
		pstate->match_for_cur_array_keys = false;
		pstate->highkeychecked = false;
	}

	return true;
}

/*
 * Helper routine used by _bt_advance_array_keys_locally.
 *
 * We're called with tuples that _bt_check_key set continuescan to false for.
 * Caller's tuple is therefore known to be located at some point in the key
 * space > where one of our search-type scan keys terminated one of our
 * primitive index scans.
 *
 * We distinguish between search-type scan keys that have equality constraints
 * on an index column (which are always marked as required in both directions)
 * and other search-type scan keys that are required in one direction only.
 * The distinction is important independent of the current scan direction,
 * since it makes it possible to distinguish between the end of the current
 * local/primitive index scan, and the end of the btrescan-wise index scan.
 *
 * We help _bt_check_key to "suppress" termination of primitive index scans
 * when it's not yet clear if the next primitive index scan needs to descend
 * the index anew by calling _bt_first.  We also help our caller identify
 * where matches for the next set of array keys _might_ begin when it turns
 * out that we can elide another descent of the index for the next set of
 * array keys.  There is a gap of 0 or more non-matching index tuples between
 * the last tuple that satisfies the current set of scan keys (including its
 * array keys), and the first tuple that might satisfy the next set (caller
 * won't know for sure until after it advances the current set of array keys).
 *
 * A qual such as "WHERE x IN (3,4,5) AND y < 42" will have its 'y' scan key
 * marked SK_BT_REQFWD (though never SK_BT_REQBKWD).  _bt_check_key() will set
 * continuescan=false as soon as the scan reaches a tuple matching (3, 42) or
 * a tuple matching (4, 1).  Eliding another desent of the index hinges upon
 * the gap between these two possible matches being small and manageable.
 * Caller forcibly continues its scan through these gap tuples, and calls back
 * here to check if it has found the point that it might be necessary to
 * advance its array keys.
 *
 * Returns false when caller's tuple definitely isn't where the next group of
 * matching tuples begins.  Caller can either continue the process with the
 * very next tuple from its leaf page, or give up completely.  Giving up means
 * that caller accepts that there must be another _bt_first descent (if and
 * when the executor next calls btgettuple/btgetbitmap).
 *
 * Returns true when caller passed a tuple that might be a match for the next
 * set of array keys.  That is, when tuple is > the current set of array keys
 * for a forward scan (actually, > the current set of search-type scan keys
 * that are equality constraints, or < for backwards scans).  Caller must
 * attempt to advance the array keys when this happens.  When the array keys
 * can be advanced, caller must then call _bt_check_key once more for the same
 * tuple (the new array keys might indicate it's a match this time around).
 *
 * Note: Our test is based on the current scan keys (and array keys) rather
 * than the next set in line because it's not yet clear if the next set in
 * line will find any matches whatsoever.  Once caller is positioned at the
 * first tuple that might satisfy the next set of array keys, it could be
 * necessary for it to advance its array keys more than once.  Queries with
 * several ScalarArrayOpExpr clauses routinely cycle through a huge number of
 * distinct combinations of array keys before finding the set that matches
 * caller's tuple (or the set that comes after the tuple in the key space in
 * the event that no set of keys matches tuple).
 *
 * Note: Each index tuple can only be a match for a single combination of
 * equality constraint/array keys (when it matches any at all).
 */
static bool
_bt_tuple_might_match_next_array_keys(IndexScanDesc scan, IndexTuple tuple,
									  ScanDirection dir)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	Relation	rel = scan->indexRelation;
	TupleDesc	itupdesc = RelationGetDescr(rel);
	bool		tuple_ahead = true;
	int			ncmpkey;

	Assert(!so->disableDynamic);
	Assert(so->qual_ok);
	Assert(so->numArrayKeys > 0);
	Assert(so->numberOfKeys > 0);
	Assert(so->inskey.keysz > 0);

	if (so->log_btree_verbosity >= 2)
	{
		char	   *pitup = _nbtree_print_itup(tuple, rel);

		appendStringInfo(&so->debugstr,
						 "_bt_tuple_might_match_next_array_keys: testing %s with TID (%u,%u)\n",
						 pitup,
						 ItemPointerGetBlockNumberNoCheck(&tuple->t_tid),
						 ItemPointerGetOffsetNumberNoCheck(&tuple->t_tid));

		pfree(pitup);
	}

	ncmpkey = Min(BTreeTupleGetNAtts(tuple, rel), so->numberOfKeys);
	for (int attnum = 1; attnum <= ncmpkey; attnum++)
	{
		ScanKey		cur = &so->keyData[attnum - 1];
		ScanKey		iscankey;
		Datum		datum;
		bool		isNull;
		int32		result;

		if ((ScanDirectionIsForward(dir) &&
			 (cur->sk_flags & SK_BT_REQFWD) == 0) ||
			(ScanDirectionIsBackward(dir) &&
			 (cur->sk_flags & SK_BT_REQBKWD) == 0))
		{
			/*
			 * This scan key is not marked as required for the current
			 * direction, so there are no further attributes to consider. This
			 * tuple definitely isn't at the start of the next group of
			 * matching tuples.
			 */
			break;
		}

		Assert(cur->sk_attno == attnum);
		if (cur->sk_attno > so->inskey.keysz)
		{
			/*
			 * There is no equality constraint on this column/scan key to
			 * break the tie.  This tuple definitely isn't at the start of the
			 * next group of matching tuples.
			 */
			Assert(cur->sk_strategy != BTEqualStrategyNumber);
			Assert((cur->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)) !=
				   (SK_BT_REQFWD | SK_BT_REQBKWD));
			break;
		}

		/*
		 * This column has an equality constraint/insertion scan key entry
		 */
		Assert((cur->sk_flags & (SK_BT_REQFWD | SK_BT_REQBKWD)) ==
			   (SK_BT_REQFWD | SK_BT_REQBKWD));
		Assert(cur->sk_strategy == BTEqualStrategyNumber);

		/*
		 * Row comparison scan keys may be present after (though never before)
		 * columns that we recognized as having equality constraints.
		 *
		 * A qual like "WHERE a in (1, 2, 3) AND (b, c) >= (500, 7)" is safe,
		 * whereas "WHERE (a, b) >= (1, 500) AND c in (7, 8, 9)" is unsafe.
		 * Assert that this isn't one of the unsafe cases in passing.
		 */
		Assert((cur->sk_flags & SK_ROW_HEADER) == 0);

		/*
		 * We'll need to use this attribute's 3-way comparison order proc
		 * (btree opclass support function 1) from its insertion-type scan key
		 */
		iscankey = &so->inskey.scankeys[attnum - 1];
		Assert(iscankey->sk_flags == cur->sk_flags);
		Assert(iscankey->sk_attno == cur->sk_attno);
		Assert(iscankey->sk_subtype == cur->sk_subtype);
		Assert(iscankey->sk_collation == cur->sk_collation);

		/*
		 * The 3-way comparison order proc will be called using the
		 * search-type scan key's current sk_argument
		 */
		datum = index_getattr(tuple, attnum, itupdesc, &isNull);

		if (so->log_btree_verbosity >= 2)
		{
			char	   *flags;

			appendStringInfo(&so->debugstr,
							 "                                       attnum %d: [datumTuple: %lu, datumSearchSK: %lu\n",
							 attnum, datum, cur->sk_argument);
			flags = dump_scankey_flags(iscankey);
			appendStringInfo(&so->debugstr,
							 "                                       insert-type sk:    [ flags: %s, attno: %u func OID: %u ]\n",
							 flags, iscankey->sk_attno,
							 iscankey->sk_func.fn_oid);
			pfree(flags);
			flags = dump_scankey_flags(cur);
			appendStringInfo(&so->debugstr,
							 "                                       search-type sk: [ flags: %s, attno: %u func OID: %u ]\n",
							 flags, cur->sk_attno,
							 cur->sk_func.fn_oid);
			pfree(flags);
		}

		if (iscankey->sk_flags & SK_ISNULL) /* key is NULL */
		{
			if (isNull)
				result = 0;		/* NULL "=" NULL */
			else if (iscankey->sk_flags & SK_BT_NULLS_FIRST)
				result = -1;	/* NULL "<" NOT_NULL */
			else
				result = 1;		/* NULL ">" NOT_NULL */
		}
		else if (isNull)		/* key is NOT_NULL and item is NULL */
		{
			if (iscankey->sk_flags & SK_BT_NULLS_FIRST)
				result = 1;		/* NOT_NULL ">" NULL */
			else
				result = -1;	/* NOT_NULL "<" NULL */
		}
		else
		{
			/*
			 * The sk_func needs to be passed the index value as left arg and
			 * the sk_argument as right arg (they might be of different
			 * types).  We want to keep this consistent with what _bt_compare
			 * does, so we flip the sign of the comparison result.  (Unless
			 * it's a DESC column, in which case we *don't* flip the sign.)
			 */
			result = DatumGetInt32(FunctionCall2Coll(&iscankey->sk_func,
													 cur->sk_collation, datum,
													 cur->sk_argument));
			if (!(iscankey->sk_flags & SK_BT_DESC))
				INVERT_COMPARE_RESULT(result);
		}

		if (result != 0)
		{
			if (ScanDirectionIsForward(dir))
				tuple_ahead = result < 0;
			else
				tuple_ahead = result > 0;

			break;
		}
	}

	if (so->log_btree_verbosity >= 2)
		appendStringInfo(&so->debugstr,
						 "_bt_tuple_might_match_next_array_keys: returns %s\n",
						 tuple_ahead ? "true" : "false");

	return tuple_ahead;
}

/*
 * _bt_mark_array_keys() -- Handle array keys during btmarkpos
 *
 * Save the current state of the array keys as the "mark" position.
 */
void
_bt_mark_array_keys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	int			i;

	for (i = 0; i < so->numArrayKeys; i++)
	{
		BTArrayKeyInfo *curArrayKey = &so->arrayKeys[i];

		curArrayKey->mark_elem = curArrayKey->cur_elem;
	}
}

/*
 * _bt_restore_array_keys() -- Handle array keys during btrestrpos
 *
 * Restore the array keys to where they were when the mark was set.
 */
void
_bt_restore_array_keys(IndexScanDesc scan)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	bool		changed = false;
	int			i;

	/* Restore each array key to its position when the mark was set */
	for (i = 0; i < so->numArrayKeys; i++)
	{
		BTArrayKeyInfo *curArrayKey = &so->arrayKeys[i];
		ScanKey		skey = &so->arrayKeyData[curArrayKey->scan_key];
		int			mark_elem = curArrayKey->mark_elem;

		if (curArrayKey->cur_elem != mark_elem)
		{
			curArrayKey->cur_elem = mark_elem;
			skey->sk_argument = curArrayKey->elem_values[mark_elem];
			changed = true;
		}
	}

	/*
	 * If we changed any keys, we must redo _bt_preprocess_keys.  That might
	 * sound like overkill, but in cases with multiple keys per index column
	 * it seems necessary to do the full set of pushups.
	 */
	if (changed)
	{
		_bt_preprocess_keys(scan);
		/* The mark should have been set on a consistent set of keys... */
		Assert(so->qual_ok);
	}
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
 * but no other code depends on that.
 *
 * The output keys are marked with flags SK_BT_REQFWD and/or SK_BT_REQBKWD
 * if they must be satisfied in order to continue the scan forward or backward
 * respectively.  _bt_check_key uses these flags.  For example, if the quals
 * are "x = 1 AND y < 4 AND z < 5", then _bt_check_key will reject a tuple
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
 *
 * TODO Replace all calls to this function added by the patch with calls to
 * some other more specialized function with reduced surface area -- something
 * that is explicitly safe to call while holding a buffer lock.  That's been
 * put off for now because the code in this function is likely to need to be
 * better integrated with the planner before long anyway.
 *
 * (Besides, there are surprisingly few cycles wasted here.  Problems seem to
 * be limited to adversarial cases with many array keys, but few index tuples.)
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
	ScanKey		cur;
	ScanKey		xform[BTMaxStrategyNumber];
	bool		test_result;
	int			i,
				j;
	AttrNumber	attno;

	/* initialize result variables */
	so->qual_ok = true;
	so->numberOfKeys = 0;

	if (numberOfKeys < 1)
	{
		if (so->log_btree_verbosity)
			appendStringInfo(&so->debugstr,
							 "_bt_preprocess_keys: qual-less scan (i.e. numberOfKeys is 0)\n");

		return;					/* done if qual-less scan */
	}

	/*
	 * Read so->arrayKeyData if array keys are present, else scan->keyData
	 */
	if (so->arrayKeyData != NULL)
		inkeys = so->arrayKeyData;
	else
		inkeys = scan->keyData;

	outkeys = so->keyData;
	cur = &inkeys[0];
	/* we check that input keys are correctly ordered */
	if (cur->sk_attno < 1)
		elog(ERROR, "btree index keys must be ordered by attribute");

	if (so->log_btree_verbosity)
	{
		for (int inkeysn = 0; inkeysn < scan->numberOfKeys; inkeysn++)
		{
			ScanKey		j = inkeys + inkeysn;
			char	   *flags = dump_scankey_flags(j);

			appendStringInfo(&so->debugstr,
							 "_bt_preprocess_keys: input inkeys[%d]: [ flags: %s, attno: %u func OID: %u ]\n",
							 inkeysn, flags, cur->sk_attno,
							 cur->sk_func.fn_oid);

			pfree(flags);
		}
	}

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

		if (so->log_btree_verbosity)
			appendStringInfo(&so->debugstr,
							 "_bt_preprocess_keys: numberOfKeys == 1 case set so->keyData[0] to %zu, qual_ok to %d\n",
							 outkeys[0].sk_argument, so->qual_ok);

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
			if (xform[BTEqualStrategyNumber - 1])
			{
				ScanKey		eq = xform[BTEqualStrategyNumber - 1];

				for (j = BTMaxStrategyNumber; --j >= 0;)
				{
					ScanKey		chk = xform[j];

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
						/* else discard the redundant non-equality key */
						xform[j] = NULL;
					}
					/* else, cannot determine redundancy, keep both keys */
				}
				/* track number of attrs for which we have "=" keys */
				numberOfEqualCols++;
			}

			/* try to keep only one of <, <= */
			if (xform[BTLessStrategyNumber - 1]
				&& xform[BTLessEqualStrategyNumber - 1])
			{
				ScanKey		lt = xform[BTLessStrategyNumber - 1];
				ScanKey		le = xform[BTLessEqualStrategyNumber - 1];

				if (_bt_compare_scankey_args(scan, le, lt, le,
											 &test_result))
				{
					if (test_result)
						xform[BTLessEqualStrategyNumber - 1] = NULL;
					else
						xform[BTLessStrategyNumber - 1] = NULL;
				}
			}

			/* try to keep only one of >, >= */
			if (xform[BTGreaterStrategyNumber - 1]
				&& xform[BTGreaterEqualStrategyNumber - 1])
			{
				ScanKey		gt = xform[BTGreaterStrategyNumber - 1];
				ScanKey		ge = xform[BTGreaterEqualStrategyNumber - 1];

				if (_bt_compare_scankey_args(scan, ge, gt, ge,
											 &test_result))
				{
					if (test_result)
						xform[BTGreaterEqualStrategyNumber - 1] = NULL;
					else
						xform[BTGreaterStrategyNumber - 1] = NULL;
				}
			}

			/*
			 * Emit the cleaned-up keys into the outkeys[] array, and then
			 * mark them if they are required.  They are required (possibly
			 * only in one direction) if all attrs before this one had "=".
			 */
			for (j = BTMaxStrategyNumber; --j >= 0;)
			{
				if (xform[j])
				{
					ScanKey		outkey = &outkeys[new_numberOfKeys++];

					memcpy(outkey, xform[j], sizeof(ScanKeyData));
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
			if (numberOfEqualCols == attno - 1)
				_bt_mark_scankey_required(outkey);

			/*
			 * We don't support RowCompare using equality; such a qual would
			 * mess up the numberOfEqualCols tracking.
			 */
			Assert(j != (BTEqualStrategyNumber - 1));
			continue;
		}

		/* have we seen one of these before? */
		if (xform[j] == NULL)
		{
			/* nope, so remember this scankey */
			xform[j] = cur;
		}
		else
		{
			/* yup, keep only the more restrictive key */
			if (_bt_compare_scankey_args(scan, cur, cur, xform[j],
										 &test_result))
			{
				if (test_result)
					xform[j] = cur;
				else if (j == (BTEqualStrategyNumber - 1))
				{
					/* key == a && key == b, but a != b */
					so->qual_ok = false;
					return;
				}
				/* else old key is more restrictive, keep it */
			}
			else
			{
				/*
				 * We can't determine which key is more restrictive.  Keep the
				 * previous one in xform[j] and push this one directly to the
				 * output array.
				 */
				ScanKey		outkey = &outkeys[new_numberOfKeys++];

				memcpy(outkey, cur, sizeof(ScanKeyData));
				if (numberOfEqualCols == attno - 1)
					_bt_mark_scankey_required(outkey);
			}
		}
	}

	if (so->log_btree_verbosity)
		appendStringInfo(&so->debugstr,
						 "_bt_preprocess_keys: so->numberOfKeys will be %d\n",
						 so->numberOfKeys);

	so->numberOfKeys = new_numberOfKeys;
}

/*
 * Save insertion scankey for searches with a SK_SEARCHARRAY scan key.
 *
 * We must save the initial positioning insertion scan key for SK_SEARCHARRAY
 * scans (barring those that only have SK_SEARCHARRAY inequalities).  Each
 * insertion scan key entry/column will have a corresponding "=" operator in
 * caller's search-type scan key, but that's no substitute for the 3-way
 * comparison function.
 *
 * _bt_tuple_might_match_next_array_keys needs to perform 3-way comparisons to
 * figure out if an ongoing scan can elide another descent of the index in
 * _bt_first.  It works by locating the end of the _current_ set of equality
 * constraint type scan keys -- not by locating the start of the next set.
 * This is comparable to how _bt_search locates an initial position in the
 * index when nextkey=true.
 */
void
_bt_array_keys_save_scankeys(IndexScanDesc scan, BTScanInsert inskey)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;

	Assert(so->numArrayKeys > 0);
	Assert(so->qual_ok);
	Assert(!BTScanPosIsValid(so->currPos));

	if (!inskey)
	{
		so->disableDynamic = true;
		return;
	}

	Assert(inskey->keysz > 0);
	memcpy(&so->inskey, inskey,
		   offsetof(BTScanInsertData, scankeys) +
		   sizeof(ScanKeyData) * inskey->keysz);
}

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
 * this tuple, and set *continuescan accordingly.  See comments for
 * _bt_preprocess_keys(), above, about how this is done.
 *
 * Advances the current set of array keys for SK_SEARCHARRAY scans where
 * appropriate.  Caller's tuple may be tested against multiple distinct sets
 * of array keys when using SK_SEARCHARRAY array key merging.
 *
 * Forward scan SK_SEARCHARRAY callers must pass a high key separately (all
 * other callers just pass NULL).  Separately, all forward scan callers can
 * pass us the page high key when they finish processing a page without ever
 * having *continuescan set to false by a non-pivot tuple.  There is a good
 * chance that the page high key will indicate that the scan cannot possibly
 * need to visit the current page's right sibling leaf page.
 *
 * Forward scan callers on the rightmost leaf page (and backwards scan callers
 * on the leftmost leaf page) must call _bt_nocheckkeys whenever no earlier
 * call here set *continuescan to false.  This performs steps equivalent to
 * checking the high key, even though there is no high key to check (you can
 * think of this as checking a "positive infinity" high key in the forward
 * scan case, or a "negative infinity low key" in the backward scan case).
 *
 * scan: index scan descriptor (containing a search-type scankey)
 * tuple: index tuple to test
 */
bool
_bt_checkkeys(IndexScanDesc scan, IndexTuple tuple, bool final,
			  BTReadPageState *pstate)
{
	TupleDesc	tupdesc = RelationGetDescr(scan->indexRelation);
	int			natts = BTreeTupleGetNAtts(tuple, scan->indexRelation);
	BTScanOpaque so = (BTScanOpaque) scan->opaque;
	bool		res;

	/* This loop handles advancing to the next array elements, if any */
	do
	{
		res = _bt_check_key(scan->indexRelation, so, tupdesc, tuple, natts,
							pstate->dir, &pstate->continuescan);

		/* If we have a tuple, return it ... */
		if (res)
		{
			pstate->match_for_cur_array_keys = true;

			Assert(!so->numArrayKeys || so->disableDynamic ||
				   _bt_tuple_might_match_next_array_keys(scan, tuple,
														 pstate->dir));
			break;
		}

		/* ... otherwise see if we have more array keys to deal with */
	} while (so->numArrayKeys && !pstate->continuescan &&
			 _bt_advance_array_keys_locally(scan, tuple, final, pstate));

	return res;
}

/*
 *_bt_checkkeys() worker function.  Determines if the scan's current set of
 * scankeys is satisfied by index tuple.
 */
static bool
_bt_check_key(Relation rel, BTScanOpaque so, TupleDesc tupdesc, IndexTuple tuple,
			  int tupnatts, ScanDirection dir, bool *continuescan)
{
	int			keysz = so->numberOfKeys;
	int			ikey;
	ScanKey		key;
	int			iter = 0;

	Assert(!so->numArrayKeys || so->arrayKeysStarted);

	*continuescan = true;		/* default assumption */

	for (key = so->keyData, ikey = 0; ikey < keysz; key++, ikey++)
	{
		Datum		datum;
		bool		isNull;
		Datum		test;

		if (key->sk_attno > tupnatts)
		{
			/*
			 * This attribute is truncated (must be high key).  The value for
			 * this attribute in the first non-pivot tuple on the page to the
			 * right could be any possible value.  Assume that truncated
			 * attribute passes the qual.
			 */
			Assert(ScanDirectionIsForward(dir));
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
			if ((key->sk_flags & SK_BT_REQFWD) &&
				ScanDirectionIsForward(dir))
				*continuescan = false;
			else if ((key->sk_flags & SK_BT_REQBKWD) &&
					 ScanDirectionIsBackward(dir))
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

		test = FunctionCall2Coll(&key->sk_func, key->sk_collation,
								 datum, key->sk_argument);

		if (!DatumGetBool(test))
		{
			/*
			 * Tuple fails this qual.  If it's a required qual for the current
			 * scan direction, then we can conclude no further tuples will
			 * pass, either.
			 *
			 * Note: because we stop the scan as soon as any required equality
			 * qual fails, it is critical that equality quals be used for the
			 * initial positioning in _bt_first() when they are available. See
			 * comments in _bt_first().
			 */
			if ((key->sk_flags & SK_BT_REQFWD) &&
				ScanDirectionIsForward(dir))
				*continuescan = false;
			else if ((key->sk_flags & SK_BT_REQBKWD) &&
					 ScanDirectionIsBackward(dir))
				*continuescan = false;

			if (so->log_btree_verbosity >= 2)
			{
				char	   *pitup = _nbtree_print_itup(tuple, rel);

				appendStringInfo(&so->debugstr,
								 "_bt_check_key %d: comparing %s with TID (%u,%u) to %zu false result continuescan %d\n",
								 iter++, pitup,
								 ItemPointerGetBlockNumberNoCheck(&tuple->t_tid),
								 ItemPointerGetOffsetNumberNoCheck(&tuple->t_tid),
								 key->sk_argument, *continuescan);

				pfree(pitup);
			}

			/*
			 * In any case, this indextuple doesn't match the qual.
			 */
			return false;
		}
		if (so->log_btree_verbosity >= 2)
		{
			char	   *pitup = _nbtree_print_itup(tuple, rel);

			appendStringInfo(&so->debugstr,
							 "_bt_check_key %d: comparing %s with TID (%u,%u) to %zu true result continuescan %d\n",
							 iter++, pitup,
							 ItemPointerGetBlockNumberNoCheck(&tuple->t_tid),
							 ItemPointerGetOffsetNumberNoCheck(&tuple->t_tid),
							 key->sk_argument, *continuescan);

			pfree(pitup);
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
 * This is a subroutine for _bt_check_key, which see for more info.
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
			Assert(ScanDirectionIsForward(dir));
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

void
_bt_checkfinalkeys(IndexScanDesc scan, BTReadPageState *pstate)
{
	IndexTuple	highkey = pstate->highkey;

	Assert(pstate->continuescan);

	if (!pstate->highkey)
	{
		_bt_nocheckkeys(scan, pstate->dir);
		pstate->continuescan = false;
		return;
	}

	/*
	 * TODO Explain the speculative nature of inferring that we're likely to
	 * find more matches on the next page here
	 */
	pstate->highkey = NULL;
	_bt_checkkeys(scan, highkey, false, pstate);
}

/*
 * Perform final steps when the "end point" is reached on the leaf level
 * without any call to _bt_checkkeys setting *continuescan to false.
 *
 * Called on the rightmost page in the forward scan case, and the leftmost
 * page in the backwards scan case.  Only call here when _bt_checkkeys hasn't
 * already set continuescan to false.
 */
bool
_bt_nocheckkeys(IndexScanDesc scan, ScanDirection dir)
{
	BTScanOpaque so = (BTScanOpaque) scan->opaque;

	/* Only need to do real work in SK_SEARCHARRAY case, for now */
	if (!so->numArrayKeys)
		return false;

	Assert(so->arrayKeysStarted);

	while (_bt_advance_array_keys(scan, dir))
		_bt_preprocess_keys(scan);

	return true;
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
	int			nkilled = 0;

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
				nkilled++;
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

		if (so->log_btree_verbosity)
			appendStringInfo(&so->debugstr,
							 "_bt_killitems: killed %d out of %d numKilled from kill_prior_tuple heapam state\n",
							 nkilled, numKilled);
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
