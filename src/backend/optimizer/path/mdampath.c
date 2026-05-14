/*-------------------------------------------------------------------------
 *
 * mdampath.c
 *	  MDAM (Multi-Dimensional Access Method) OR-clause optimization for
 *	  B-tree index scans.
 *
 * Transforms complex OR predicates (often involving multiple columns of a
 * single B-tree index) into non-overlapping index scan retrievals combined
 * via Append.  Always preserves the index's underlying key space ordering.
 *
 * Based on the "general OR optimization" section of "Efficient Search of
 * Multidimensional B-Trees".  This complements nbtree skip scan, which the
 * underlying index scans tend to make heavy use of when multi column indexes
 * are involved.
 *
 * Algorithm:
 *
 *   1. DNF conversion + simplification: Convert the predicate tree into
 *      OR-of-ANDs form, simplifying each conjunction (detect contradictions,
 *      intersect overlapping ranges, merge EQ/IN).
 *
 *   2. Generate initial retrievals (shattering): For each index column,
 *      collect boundary values ("critical points") from the DNF, partition
 *      each column's space into elementary intervals, enumerate all
 *      combinations, and filter to keep only paths satisfying at least
 *      one DNF clause.  Guarantees non-overlapping paths.
 *
 *   3. Merge retrievals: For each column in reverse index order, coalesce
 *      overlapping/adjacent intervals and fold col=v1, col=v2 into
 *      col IN (v1,v2).
 *
 *   3a. SAOP-aware path simplification: For columns carrying a SAOP plus
 *      sibling atoms, narrow the SAOP's value set against the siblings'
 *      effective interval and drop the now-redundant siblings.  Paths
 *      whose SAOPs empty out are pruned as fully contradictory.  This
 *      mirrors what _bt_preprocess_keys does at runtime, but doing it
 *      here can enable step 4 transformations -- e.g. a SAOP that
 *      collapses to a single EQ can now participate in coalescing.
 *
 *   4. Expand leading constraints, sort, and coalesce: Expand leading IN
 *      and range constraints into elementary intervals so each path has
 *      point constraints on leading columns (enabling key space ordering).
 *      Sort by index key space, then coalesce adjacent paths that were
 *      needlessly shattered.  Step 4c invokes the simplification pass
 *      again on each candidate pair before testing whether they merge.
 *
 * Some OR predicates admit a correct Append-of-IndexScans but no Append
 * arrangement that preserves index key space order.  The canonical shape is
 * a non-point leading-column constraint shared between arms whose OR splits
 * on *different* later columns, e.g.
 *
 *   dept > 10 AND sdate = '1995-03-01' AND (item_class = 5 OR store = 50)
 *
 * Within the shared 'dept > 10' range each individual 'dept' value can
 * produce rows from either disjunct, in either order, so the two arms'
 * tuples interleave column-by-column down the key.  The only way to split
 * cleanly would be to expand 'dept > 10' into one EQ per distinct dept
 * value present in the table -- a runtime-unknown, potentially unbounded
 * count of disjuncts.  There is no static rewriting in terms of nbtree CNF
 * scankeys plus ScalarArrayOps that preserves index key space order.  We
 * could still emit an unordered MDAM Append in this situation (an index-only
 * scan without path keys is possible in principle), but for now we opt not
 * to.  When mdam_detect_ordering_conflict identifies such a qual, we give up.
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/backend/optimizer/path/mdampath.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/nbtree.h"
#include "access/stratnum.h"
#include "catalog/pg_amop.h"
#include "catalog/pg_operator.h"
#include "catalog/pg_opfamily.h"
#include "catalog/pg_type.h"
#include "nodes/makefuncs.h"
#include "nodes/nodeFuncs.h"
#include "optimizer/cost.h"
#include "optimizer/mdampath.h"
#include "optimizer/optimizer.h"
#include "optimizer/pathnode.h"
#include "optimizer/paths.h"
#include "optimizer/restrictinfo.h"
#include "miscadmin.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/datum.h"
#include "utils/fmgroids.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/selfuncs.h"

/* GUC variable */
bool		enable_mdam = true;

/* Collation match check -- same as in indxpath.c */
#define IndexCollMatchesExprColl(idxcollation, exprcollation) \
	((idxcollation) == InvalidOid || (idxcollation) == (exprcollation))

/* Maximum number of retrievals we'll generate before giving up */
#define MDAM_MAX_RETRIEVALS		512

/* Maximum number of critical points per column */
#define MDAM_MAX_CRITICAL_POINTS 128

/* Maximum number of DNF conjuncts after conversion */
#define MDAM_MAX_DNF_CONJUNCTS	128

/*
 * Conservative upper bound on the DNF size implied purely by the input
 * predicate structure (each AND multiplies, each OR sums, leaves count as
 * 1).  When mdam_estimate_dnf_size exceeds this, generate_mdam_or_paths
 * bails before any per-index allocation: even if the cross-product later
 * folds many conjuncts to contradictions, the planning cost of producing
 * and merging the un-folded retrievals dominates.  Set well below
 * MDAM_MAX_DNF_CONJUNCTS so it triggers earlier than the in-pipeline cap.
 */
#define MDAM_MAX_DNF_PREESTIMATE	64

#ifdef MDAM_DEBUG
#define MDAM_LOG(...)	elog(DEBUG1, __VA_ARGS__)
#else
#define MDAM_LOG(...)	((void) 0)
#endif

/*
 * Per-column comparison context (cached once during initialization).
 */
typedef struct MdamColContext
{
	int			colno;			/* 0-based index column number */
	Oid			typid;			/* column data type */
	int16		typlen;			/* type length */
	bool		typbyval;		/* type pass-by-value? */
	Oid			collid;			/* collation OID */
	Oid			opfamily;		/* btree operator family */
	Oid			eq_opr;			/* equality operator OID */
	FmgrInfo	cmp_finfo;		/* btree ORDER proc */
} MdamColContext;

/*
 * Top-level context for one MDAM transformation.
 */
typedef struct MdamContext
{
	PlannerInfo *root;
	RelOptInfo *rel;
	IndexOptInfo *index;
	int			nkeycolumns;
	MdamColContext *col_ctx;	/* array[nkeycolumns] */
	MemoryContext mdam_mcxt;	/* scratch memory context */
	bool		retrievals_truncated;	/* shattering hit the safety cap */
	List	   *contradictory;	/* contradictory cross-product conjuncts
								 * dropped from DNF but available for
								 * standalone emission when needed */
} MdamContext;

/*
 * Operator types for MDAM atoms.
 *
 * These mirror the comparison operators in a B-tree opfamily plus special
 * types for ScalarArrayOp arrays and unconstrained columns.
 */
typedef enum MdamOpType
{
	MDAM_OP_EQ,					/* column = value */
	MDAM_OP_LT,					/* column < value */
	MDAM_OP_LE,					/* column <= value */
	MDAM_OP_GT,					/* column > value */
	MDAM_OP_GE,					/* column >= value */
	MDAM_OP_SAOP,				/* column IN (v1, v2, ...) */
	MDAM_OP_RANGE_EXCL,			/* low < column < high (exclusive both ends) */
	MDAM_OP_IS_NULL,			/* column IS NULL */
	MDAM_OP_IS_NOT_NULL,		/* column IS NOT NULL */
	MDAM_OP_IS_ANYTHING			/* column is unconstrained */
} MdamOpType;

/*
 * MdamAtom -- a single predicate on one index column.
 *
 * For EQ/LT/LE/GT/GE: 'value' holds the constant.
 * For SAOP: 'in_values' is a sorted array of n_in_values Datums.
 * For RANGE_EXCL: 'range_lo' and 'range_hi' are the exclusive bounds.
 * For IS_ANYTHING: all value fields are unused.
 */
typedef struct MdamAtom
{
	int			colno;			/* index column number (0-based) */
	MdamOpType	op;
	Datum		value;			/* for EQ, LT, LE, GT, GE */
	Datum	   *in_values;		/* for SAOP: palloc'd sorted array */
	int			n_in_values;	/* number of SAOP array values */
	Datum		range_lo;		/* for RANGE_EXCL: exclusive lower bound */
	Datum		range_hi;		/* for RANGE_EXCL: exclusive upper bound */
} MdamAtom;

/*
 * Single-column interval [lo, hi] representation, used for interval
 * arithmetic.
 */
typedef struct MdamInterval
{
	Datum		lo;
	Datum		hi;
	bool		lo_inclusive;
	bool		hi_inclusive;
	bool		lo_infinite;	/* no lower bound (-inf bound)? */
	bool		hi_infinite;	/* no upper bound (+inf bound)? */
	bool		is_null_interval;	/* "column IS NULL" pseudo-interval */

	/*
	 * "column IS NOT NULL" marker; only meaningful when the interval is
	 * otherwise unbounded
	 */
	bool		is_not_null_interval;
} MdamInterval;

/* Which side of an MdamInterval is being tightened. */
typedef enum MdamBound
{
	MDAM_LO,
	MDAM_HI
} MdamBound;

static MdamContext *mdam_init_context(PlannerInfo *root, RelOptInfo *rel,
									 IndexOptInfo *index);
static int	mdam_compare(MdamContext *ctx, int colno, Datum v1, Datum v2);
static bool mdam_datum_eq(MdamContext *ctx, int colno, Datum v1, Datum v2);
static Datum mdam_copy_datum(MdamContext *ctx, int colno, Datum val);
static MdamAtom *mdam_make_atom(MdamContext *ctx, int colno, MdamOpType op,
								Datum value);
static MdamAtom *mdam_make_atom_saop(MdamContext *ctx, int colno,
									 Datum *values, int nvalues);
static MdamAtom *mdam_make_atom_range(MdamContext *ctx, int colno,
									  Datum lo, Datum hi);
static MdamAtom *mdam_copy_atom(MdamContext *ctx, MdamAtom *src);
static List *mdam_extract_dnf(MdamContext *ctx, List *clauses);
static List *mdam_expr_to_dnf(MdamContext *ctx, Expr *expr);
static List *mdam_conjunct_from_opexpr(MdamContext *ctx, OpExpr *opexpr);
static List *mdam_conjunct_from_saop(MdamContext *ctx,
									 ScalarArrayOpExpr *saop);
static List *mdam_dnf_from_not_in_saop(MdamContext *ctx,
									   ScalarArrayOpExpr *saop);
static List *mdam_conjunct_from_nulltest(MdamContext *ctx, NullTest *nt);
static List *mdam_simplify_conjunct(MdamContext *ctx, List *atoms);
static void tighten_bound(MdamContext *ctx, int colno, MdamInterval *iv,
						  MdamBound side, Datum val, bool inclusive);
static MdamInterval *mdam_extract_interval(MdamContext *ctx, int colno,
										   List *col_atoms);
static bool mdam_point_in_interval(MdamContext *ctx, int colno,
								   Datum point, MdamInterval *iv);
static List *mdam_interval_to_atoms(MdamContext *ctx, int colno,
									MdamInterval *iv);
static bool mdam_intervals_overlap(MdamContext *ctx, int colno,
								   MdamInterval *a, MdamInterval *b);
static List *mdam_merge_interval_list(MdamContext *ctx, int colno,
									  List *intervals);
static List *mdam_get_critical_points(MdamContext *ctx, int colno, List *dnf);
static List *mdam_generate_elementary_intervals(MdamContext *ctx, int colno,
												List *critical_points,
												List *dnf);
static List *mdam_elem_intervals_for_col(MdamContext *ctx, int colno,
										 List *dnf);
static List *mdam_generate_retrievals(MdamContext *ctx, List *dnf);
static void mdam_generate_recursive(MdamContext *ctx, List *orig_dnf,
									int col_idx, List *current_path,
									List **result);
static bool mdam_retrieval_satisfies_dnf(MdamContext *ctx, List *orig_dnf,
										 List *path);
static bool atom_contains_point(MdamContext *ctx, MdamAtom *atom, Datum point);
static bool mdam_atoms_compatible(MdamContext *ctx, MdamAtom *dnf_atom,
								  MdamAtom *path_atom);
static List *mdam_atoms_for_col(List *path, int colno);
static bool mdam_col_atoms_has_saop(List *col_atoms_list);
static bool mdam_col_atoms_preserve_verbatim(List *col_atoms_list);
static List *mdam_atoms_except_col(List *path, int colno);
static List *mdam_sort_path_atoms(List *path);
static List *mdam_path_base(List *path, int colno);
static bool mdam_base_atoms_equal(MdamContext *ctx, List *a, List *b);
static List *mdam_merge_retrievals(MdamContext *ctx, List *paths);
static MdamAtom *mdam_single_eq_on_col(List *path, int colno);
static List *mdam_merge_eq_to_in(MdamContext *ctx, int colno, List *paths,
								 bool adjacent_only);
static int	mdam_last_constrained_col(List *path);
static MdamInterval *mdam_get_path_sort_key(MdamContext *ctx, List *path);
static int	mdam_interval_cmp(MdamContext *ctx, int col,
							  const MdamInterval *ia, const MdamInterval *ib);
static int	mdam_path_sort_cmp(const void *a, const void *b, void *arg);
static List *mdam_expand_sort_coalesce(MdamContext *ctx, List *dnf,
									   List *paths);
static List *mdam_expand_leading(MdamContext *ctx, List *dnf, List *paths);
static List *mdam_sort_by_key_space(MdamContext *ctx, List *paths);
static List *mdam_coalesce_adjacent(MdamContext *ctx, List *paths);
static bool mdam_path_is_contradictory(MdamContext *ctx, List *path);
static List *mdam_simplify_path(MdamContext *ctx, List *path);
static bool mdam_detect_ordering_conflict(MdamContext *ctx, List *paths);
static Expr *mdam_make_indexop(MdamColContext *cc, Var *indexvar,
							   int16 strategy, Datum val);
static Expr *mdam_atom_to_expr(MdamContext *ctx, MdamAtom *atom);
static IndexPath *mdam_build_index_path(MdamContext *ctx,
										List *retrieval_atoms,
										List *or_rinfos,
										ScanDirection scandir);
static void mdam_add_paths(MdamContext *ctx, List *retrievals,
						   List *or_rinfos, ScanDirection scandir);

/*
 * Sort key for path ordering.  Precomputed before sort to avoid palloc inside
 * qsort comparator.
 */
typedef struct MdamSortEntry
{
	List	   *path;
	MdamInterval *key;			/* array[nkeycolumns] */
} MdamSortEntry;


/*
 * generate_mdam_or_paths
 *		Main entry point: called from create_index_paths() to generate
 *		MDAM-style Append-of-IndexScans paths for OR predicates.
 *
 * For each B-tree index on the relation, we check if the restriction
 * clauses contain OR predicates that reference indexed columns.  If so,
 * we run the MDAM transformation pipeline and generate an Append path
 * with non-overlapping IndexScan sub-paths (which often collapses to a
 * single IndexScan once overlapping ranges are coalesced).
 */

/*
 * mdam_clause_is_disjunctive
 *		True if the clause is logically disjunctive for MDAM's purposes:
 *		either an explicit BoolExpr OR, or a NOT IN (ScalarArrayOpExpr with
 *		useOr = false), which we expand into an OR of inequality ranges.
 *
 *		MDAM only fires when at least one such clause is present; the gate
 *		filters out plain conjunctive predicates that ordinary index path
 *		generation handles directly.
 */
static bool
mdam_clause_is_disjunctive(RestrictInfo *rinfo)
{
	if (restriction_is_or_clause(rinfo))
		return true;
	if (IsA(rinfo->clause, ScalarArrayOpExpr))
	{
		ScalarArrayOpExpr *saop = (ScalarArrayOpExpr *) rinfo->clause;

		return !saop->useOr;
	}
	return false;
}

/*
 * mdam_estimate_dnf_size
 *		Conservative upper bound on the number of DNF conjuncts that
 *		mdam_extract_dnf would produce for the given expression.  OR sums
 *		its children, AND multiplies them, leaves count as 1.  Saturates at
 *		MDAM_MAX_DNF_PREESTIMATE + 1 to avoid integer overflow and to
 *		short-circuit further recursion once the threshold is exceeded.
 *
 *		This is structural only -- it never allocates, calls into the
 *		catalog, or inspects values.  False positives (over-estimating a
 *		predicate whose DNF would collapse via contradictions or SAOP
 *		folding) cause an unnecessary MDAM bail; false negatives are caught
 *		by the in-pipeline MDAM_MAX_DNF_CONJUNCTS cap.
 */
static int
mdam_estimate_dnf_size(Expr *expr)
{
	if (expr == NULL)
		return 1;

	if (IsA(expr, RestrictInfo))
		return mdam_estimate_dnf_size(((RestrictInfo *) expr)->clause);

	if (IsA(expr, BoolExpr))
	{
		BoolExpr   *boolexpr = (BoolExpr *) expr;
		ListCell   *lc;

		if (boolexpr->boolop == OR_EXPR)
		{
			int			acc = 0;

			foreach(lc, boolexpr->args)
			{
				acc += mdam_estimate_dnf_size((Expr *) lfirst(lc));
				if (acc > MDAM_MAX_DNF_PREESTIMATE)
					return MDAM_MAX_DNF_PREESTIMATE + 1;
			}
			return acc;
		}
		if (boolexpr->boolop == AND_EXPR)
		{
			int			acc = 1;

			foreach(lc, boolexpr->args)
			{
				acc *= mdam_estimate_dnf_size((Expr *) lfirst(lc));
				if (acc > MDAM_MAX_DNF_PREESTIMATE)
					return MDAM_MAX_DNF_PREESTIMATE + 1;
			}
			return acc;
		}
		/* NOT: MDAM can't transform; treat as leaf */
		return 1;
	}

	/*
	 * Leaves (OpExpr, NullTest, IN-form SAOP, NOT IN, etc.) count as 1.
	 * NOT IN actually expands to N+1 conjuncts inside the pipeline, but
	 * the in-pipeline cap catches that; we don't peek at array lengths
	 * here.
	 */
	return 1;
}

void
generate_mdam_or_paths(PlannerInfo *root, RelOptInfo *rel)
{
	ListCell   *lc;
	List	   *or_rinfos = NIL;
	int			est;

	/* GUC check */
	if (!enable_mdam)
		return;

	/*
	 * Skip partition child relations.  An MDAM Append produced here would be
	 * flattened into the parent partition-wise Append by
	 * accumulate_append_subpath, which then trips partprune's "no duplicate
	 * subplans per partition relid" assertion.
	 */
	if (rel->reloptkind == RELOPT_OTHER_MEMBER_REL)
		return;

	/* Collect disjunctive clauses (OR or NOT IN) from restriction list */
	foreach(lc, rel->baserestrictinfo)
	{
		RestrictInfo *rinfo = lfirst_node(RestrictInfo, lc);

		if (mdam_clause_is_disjunctive(rinfo))
			or_rinfos = lappend(or_rinfos, rinfo);
	}

	MDAM_LOG("MDAM: rel %u has %d restriction clauses, %d are disjunctive",
			 rel->relid, list_length(rel->baserestrictinfo), list_length(or_rinfos));

	if (or_rinfos == NIL)
		return;

	/*
	 * Bail before any per-index allocation if the structural DNF size --
	 * the product over baserestrictinfo of each clause's OR-arm sum -- is
	 * already too large to handle profitably.  The actual cross-product
	 * inside mdam_extract_dnf would do non-trivial allocation and
	 * simplification work per produced conjunct before tripping the
	 * MDAM_MAX_DNF_CONJUNCTS cap; for the cases that would have failed
	 * anyway, skip that work entirely.
	 */
	est = 1;
	foreach(lc, rel->baserestrictinfo)
	{
		RestrictInfo *rinfo = lfirst_node(RestrictInfo, lc);

		est *= mdam_estimate_dnf_size(rinfo->clause);
		if (est > MDAM_MAX_DNF_PREESTIMATE)
		{
			MDAM_LOG("MDAM: predicted DNF size %d exceeds %d, skipping",
					 est, MDAM_MAX_DNF_PREESTIMATE);
			return;
		}
	}

	/* Try each index */
	foreach(lc, rel->indexlist)
	{
		IndexOptInfo *index = (IndexOptInfo *) lfirst(lc);
		MdamContext *ctx;
		MemoryContext old_mcxt;
		List	   *dnf;
		List	   *initial_retrievals;
		List	   *merged;
		List	   *simplified;
		List	   *final_paths;
		List	   *bwd_pathkeys;
		ListCell   *lc_m;

		check_stack_depth();

		/* B-tree only */
		if (index->relam != BTREE_AM_OID)
			continue;
		/* Skip partial indexes that don't match */
		if (index->indpred != NIL && !index->predOK)
			continue;

		ctx = mdam_init_context(root, rel, index);
		if (ctx == NULL)
			continue;

		old_mcxt = MemoryContextSwitchTo(ctx->mdam_mcxt);

		/* Step 1: Convert to DNF and simplify */
		dnf = mdam_extract_dnf(ctx, rel->baserestrictinfo);
		if (dnf == NIL ||
			list_length(dnf) + list_length(ctx->contradictory) < 2)
		{
			MDAM_LOG("MDAM: DNF extraction returned %d conjuncts (%d contradictory), skipping",
					 dnf ? list_length(dnf) : 0,
					 list_length(ctx->contradictory));
			MemoryContextSwitchTo(old_mcxt);
			MemoryContextDelete(ctx->mdam_mcxt);
			continue;
		}
		MDAM_LOG("MDAM: DNF has %d conjuncts", list_length(dnf));

		/* Step 2: Generate initial retrievals (shattering) */
		initial_retrievals = mdam_generate_retrievals(ctx, dnf);
		if (initial_retrievals == NIL ||
			list_length(initial_retrievals) > MDAM_MAX_RETRIEVALS ||
			ctx->retrievals_truncated)
		{
			MDAM_LOG("MDAM: shattering produced %d retrievals%s, skipping",
					 initial_retrievals ? list_length(initial_retrievals) : 0,
					 ctx->retrievals_truncated ? " (truncated)" : "");
			MemoryContextSwitchTo(old_mcxt);
			MemoryContextDelete(ctx->mdam_mcxt);
			continue;
		}
		MDAM_LOG("MDAM: %d initial retrievals", list_length(initial_retrievals));

		/* Step 3: Merge retrievals */
		merged = mdam_merge_retrievals(ctx, initial_retrievals);
		MDAM_LOG("MDAM: %d retrievals after merging", list_length(merged));

		/*
		 * Step 3a: SAOP-aware simplification.  Narrow SAOPs against sibling
		 * atoms and drop the now-redundant siblings; fully contradictory
		 * paths are pruned outright.  This can be an enabling transformation
		 * for step 4 (e.g. step 4c coalesce can now combine paths whose SAOPs
		 * shrink to a single EQ).
		 */
		simplified = NIL;
		foreach(lc_m, merged)
		{
			List	   *p = (List *) lfirst(lc_m);
			List	   *s = mdam_simplify_path(ctx, p);

			if (s != NULL)
				simplified = lappend(simplified, s);
		}
		merged = simplified;

		/* Step 4: Expand, sort, coalesce */
		final_paths = mdam_expand_sort_coalesce(ctx, dnf, merged);
		MDAM_LOG("MDAM: %d paths after expand/sort/coalesce",
				 final_paths ? list_length(final_paths) : 0);

		if (final_paths == NIL)
		{
			MemoryContextSwitchTo(old_mcxt);
			MemoryContextDelete(ctx->mdam_mcxt);
			continue;
		}

		/*
		 * Check for ordering conflicts.  A no-op for a single retrieval, but
		 * cheap to call uniformly here so the dispatch below stays simple.
		 */
		if (mdam_detect_ordering_conflict(ctx, final_paths))
		{
			MDAM_LOG("MDAM: ordering conflict detected, skipping");
			MemoryContextSwitchTo(old_mcxt);
			MemoryContextDelete(ctx->mdam_mcxt);
			continue;
		}

		MDAM_LOG("MDAM: adding paths for %d retrievals",
				 list_length(final_paths));
		MemoryContextSwitchTo(old_mcxt);

		mdam_add_paths(ctx, final_paths, or_rinfos, ForwardScanDirection);

		/*
		 * If a backward scan produces useful pathkeys (e.g. for ORDER BY DESC
		 * on the leading index columns), also emit a backward-scan variant
		 */
		bwd_pathkeys = build_index_pathkeys(root, index,
											BackwardScanDirection);
		bwd_pathkeys = truncate_useless_pathkeys(root, rel, bwd_pathkeys);
		if (bwd_pathkeys != NIL)
			mdam_add_paths(ctx, final_paths, or_rinfos,
						   BackwardScanDirection);
	}
}

/* ---------------------------------------------------
 * Context initialization and comparison utilities
 * ---------------------------------------------------
 */

/*
 * mdam_init_context
 *		Set up comparison functions and type info for each index column.
 */
static MdamContext *
mdam_init_context(PlannerInfo *root, RelOptInfo *rel, IndexOptInfo *index)
{
	MdamContext *ctx;
	MemoryContext mdam_mcxt;
	MemoryContext old_mcxt;

	mdam_mcxt = AllocSetContextCreate(CurrentMemoryContext,
									  "MDAM working context",
									  ALLOCSET_DEFAULT_SIZES);
	old_mcxt = MemoryContextSwitchTo(mdam_mcxt);

	ctx = palloc0(sizeof(MdamContext));
	ctx->root = root;
	ctx->rel = rel;
	ctx->index = index;
	ctx->nkeycolumns = index->nkeycolumns;
	ctx->mdam_mcxt = mdam_mcxt;
	ctx->col_ctx = palloc0(sizeof(MdamColContext) * index->nkeycolumns);

	for (int i = 0; i < index->nkeycolumns; i++)
	{
		MdamColContext *cc = &ctx->col_ctx[i];
		Oid			opfamily = index->opfamily[i];
		Oid			opcintype = index->opcintype[i];
		Oid			cmp_proc;

		cc->colno = i;
		cc->typid = opcintype;
		cc->collid = index->indexcollations[i];
		cc->opfamily = opfamily;

		get_typlenbyval(opcintype, &cc->typlen, &cc->typbyval);

		/* Get equality operator from the opfamily */
		cc->eq_opr = get_opfamily_member(opfamily, opcintype, opcintype,
										 BTEqualStrategyNumber);

		/* Get btree comparison support function */
		cmp_proc = get_opfamily_proc(opfamily, opcintype, opcintype,
									 BTORDER_PROC);
		if (!OidIsValid(cmp_proc))
		{
			/* Can't do MDAM without comparison function */
			MemoryContextSwitchTo(old_mcxt);
			MemoryContextDelete(mdam_mcxt);
			return NULL;
		}
		fmgr_info(cmp_proc, &cc->cmp_finfo);
	}

	MemoryContextSwitchTo(old_mcxt);
	return ctx;
}

/*
 * Compare two Datum values for a column. Returns <0, 0, >0.
 */
static int
mdam_compare(MdamContext *ctx, int colno, Datum v1, Datum v2)
{
	MdamColContext *cc = &ctx->col_ctx[colno];

	return DatumGetInt32(FunctionCall2Coll(&cc->cmp_finfo, cc->collid,
										   v1, v2));
}

static bool
mdam_datum_eq(MdamContext *ctx, int colno, Datum v1, Datum v2)
{
	return mdam_compare(ctx, colno, v1, v2) == 0;
}

typedef struct DatumSortArg
{
	MdamContext *ctx;
	int			colno;
}			DatumSortArg;

static int
datum_cmp_cb(const void *a, const void *b, void *arg)
{
	DatumSortArg *as = (DatumSortArg *) arg;

	return mdam_compare(as->ctx, as->colno,
						*(const Datum *) a, *(const Datum *) b);
}

/*
 * Sort a Datum array into canonical (opfamily-order) order.
 */
static void
sort_datums_canonical(MdamContext *ctx, int colno, Datum *arr, int n)
{
	DatumSortArg arg = {ctx, colno};

	qsort_arg(arr, n, sizeof(Datum), datum_cmp_cb, &arg);
}

/*
 * Copy a Datum value, handling pass-by-reference types.
 */
static Datum
mdam_copy_datum(MdamContext *ctx, int colno, Datum val)
{
	MdamColContext *cc = &ctx->col_ctx[colno];

	if (cc->typbyval)
		return val;
	return datumCopy(val, cc->typbyval, cc->typlen);
}


/* ---------------------------------------------------
 * Atom constructors
 * ---------------------------------------------------
 */
static MdamAtom *
mdam_make_atom(MdamContext *ctx, int colno, MdamOpType op, Datum value)
{
	MdamAtom   *atom = palloc0(sizeof(MdamAtom));

	atom->colno = colno;
	atom->op = op;
	if (op != MDAM_OP_IS_ANYTHING &&
		op != MDAM_OP_IS_NULL &&
		op != MDAM_OP_IS_NOT_NULL)
		atom->value = mdam_copy_datum(ctx, colno, value);
	return atom;
}

static MdamAtom *
mdam_make_atom_saop(MdamContext *ctx, int colno, Datum *values, int nvalues)
{
	MdamAtom   *atom = palloc0(sizeof(MdamAtom));

	atom->colno = colno;
	atom->op = MDAM_OP_SAOP;
	atom->in_values = palloc(sizeof(Datum) * nvalues);
	atom->n_in_values = nvalues;
	for (int i = 0; i < nvalues; i++)
	{
		atom->in_values[i] = mdam_copy_datum(ctx, colno, values[i]);
	}
	return atom;
}

static MdamAtom *
mdam_make_atom_range(MdamContext *ctx, int colno, Datum lo, Datum hi)
{
	MdamAtom   *atom = palloc0(sizeof(MdamAtom));

	atom->colno = colno;
	atom->op = MDAM_OP_RANGE_EXCL;
	atom->range_lo = mdam_copy_datum(ctx, colno, lo);
	atom->range_hi = mdam_copy_datum(ctx, colno, hi);
	return atom;
}

static MdamAtom *
mdam_copy_atom(MdamContext *ctx, MdamAtom *src)
{
	MdamAtom   *dst = palloc0(sizeof(MdamAtom));

	dst->colno = src->colno;
	dst->op = src->op;

	switch (src->op)
	{
		case MDAM_OP_EQ:
		case MDAM_OP_LT:
		case MDAM_OP_LE:
		case MDAM_OP_GT:
		case MDAM_OP_GE:
			dst->value = mdam_copy_datum(ctx, src->colno, src->value);
			break;
		case MDAM_OP_SAOP:
			{
				dst->in_values = palloc(sizeof(Datum) * src->n_in_values);
				dst->n_in_values = src->n_in_values;
				for (int i = 0; i < src->n_in_values; i++)
				{
					Datum		v = src->in_values[i];

					dst->in_values[i] = mdam_copy_datum(ctx, src->colno, v);
				}
			}
			break;
		case MDAM_OP_RANGE_EXCL:
			dst->range_lo = mdam_copy_datum(ctx, src->colno, src->range_lo);
			dst->range_hi = mdam_copy_datum(ctx, src->colno, src->range_hi);
			break;
		case MDAM_OP_IS_NULL:
		case MDAM_OP_IS_NOT_NULL:
		case MDAM_OP_IS_ANYTHING:
			break;
	}
	return dst;
}


/* ---------------------------------------------------
 * DNF extraction from expression trees
 * ---------------------------------------------------
 */

/*
 * mdam_extract_dnf
 *		Convert restriction clauses to internal DNF representation.
 *
 * Input: list of RestrictInfo nodes from rel->baserestrictinfo.
 * Output: list of retrieval paths (each path is a List of MdamAtom*).
 * Returns NIL if the clauses can't be represented in MDAM form.
 *
 * Multiple RestrictInfo nodes are implicitly ANDed together.
 *
 * A non-OR top-level RestrictInfo that cannot be converted to atoms
 * (typically because it references no index column, e.g. a filter on a
 * non-indexed attribute) is silently skipped here.  It remains in
 * rel->baserestrictinfo and is applied as a per-branch qpqual on each
 * IndexScan child of the resulting Append by the planner's normal path
 * (qpqual = baserestrictinfo - indexclauses, see is_redundant_with_indexclauses).
 * A disjunctive clause (BoolExpr OR or NOT IN) that itself fails DNF
 * conversion is still a hard failure -- silently dropping a disjunct would
 * lose rows.
 */
static List *
mdam_extract_dnf(MdamContext *ctx, List *clauses)
{
	List	   *result = NIL;
	ListCell   *lc;
	bool		found_or = false;

	/* Start with a single empty conjunct */
	result = list_make1(NIL);

	foreach(lc, clauses)
	{
		RestrictInfo *rinfo = lfirst_node(RestrictInfo, lc);
		List	   *clause_dnf;
		List	   *new_result = NIL;
		ListCell   *lc2,
				   *lc3;

		clause_dnf = mdam_expr_to_dnf(ctx, rinfo->clause);
		if (clause_dnf == NIL)
		{
			if (mdam_clause_is_disjunctive(rinfo))
				return NIL;		/* disjunctive clause must be fully representable */
			continue;			/* non-disjunctive residual: leave for qpqual */
		}

		if (mdam_clause_is_disjunctive(rinfo))
			found_or = true;

		/*
		 * Cross-product: for each existing conjunct and each new DNF arm,
		 * merge their atoms.  This distributes AND over OR.
		 *
		 * Contradictory cross-product results (e.g. item_class=4 ANDed with
		 * item_class=5) are stashed in ctx->contradictory rather than the
		 * working DNF.  Two reasons: (1) keeping them in the DNF would make
		 * their atoms contribute to per-column critical-point computation,
		 * refining the elementary-interval partition for non-contradictory
		 * conjuncts and producing extra Append branches the original
		 * predicate didn't need; (2) the stash count satisfies the per-index
		 * ">= 2 retrievals" gate so MDAM still fires on predicates whose only
		 * "second arm" is contradictory. mdam_generate_retrievals re-emits
		 * the stash as standalone retrievals; the step 3a simplification pass
		 * then prunes any that remain fully contradictory.
		 */
		foreach(lc2, result)
		{
			List	   *existing = (List *) lfirst(lc2);

			foreach(lc3, clause_dnf)
			{
				List	   *new_arm = (List *) lfirst(lc3);
				List	   *merged = list_concat_copy(existing, new_arm);
				List	   *simplified;

				simplified = mdam_simplify_conjunct(ctx, merged);
				if (simplified == NIL)
				{
					ctx->contradictory = lappend(ctx->contradictory,
												 merged);
					continue;
				}

				new_result = lappend(new_result, simplified);

				/* Safety check: bail if DNF blows up */
				if (list_length(new_result) > MDAM_MAX_DNF_CONJUNCTS)
					return NIL;
			}
		}

		result = new_result;
		if (result == NIL)
			return NIL;			/* nothing in the cross-product */
	}

	/* Only worth doing MDAM if there's at least one OR clause */
	if (!found_or)
		return NIL;

	return result;
}

/*
 * mdam_expr_to_dnf
 *		Convert a single expression to DNF form (list of lists of MdamAtom).
 *		Returns NIL if the expression can't be handled.
 */
static List *
mdam_expr_to_dnf(MdamContext *ctx, Expr *expr)
{
	check_stack_depth();

	if (IsA(expr, BoolExpr))
	{
		BoolExpr   *boolexpr = (BoolExpr *) expr;

		if (boolexpr->boolop == OR_EXPR)
		{
			/* OR: collect all arms */
			List	   *result = NIL;
			ListCell   *lc;

			foreach(lc, boolexpr->args)
			{
				Expr	   *arg = (Expr *) lfirst(lc);
				List	   *arm_dnf;

				/*
				 * OR args in the optimizer are wrapped in RestrictInfo.
				 */
				if (IsA(arg, RestrictInfo))
					arg = ((RestrictInfo *) arg)->clause;

				arm_dnf = mdam_expr_to_dnf(ctx, arg);
				if (arm_dnf == NIL)
					return NIL;
				result = list_concat(result, arm_dnf);

				if (list_length(result) > MDAM_MAX_DNF_CONJUNCTS)
					return NIL;
			}
			return result;
		}
		else if (boolexpr->boolop == AND_EXPR)
		{
			/* AND: cross-product the DNF of each operand */
			List	   *result = list_make1(NIL);
			ListCell   *lc;

			foreach(lc, boolexpr->args)
			{
				Expr	   *arg = (Expr *) lfirst(lc);
				List	   *arm_dnf;
				List	   *new_result = NIL;
				ListCell   *lc2,
						   *lc3;

				if (IsA(arg, RestrictInfo))
					arg = ((RestrictInfo *) arg)->clause;

				arm_dnf = mdam_expr_to_dnf(ctx, arg);
				if (arm_dnf == NIL)
					return NIL;

				foreach(lc2, result)
				{
					List	   *existing = (List *) lfirst(lc2);

					foreach(lc3, arm_dnf)
					{
						List	   *new_arm = (List *) lfirst(lc3);
						List	   *merged = list_concat_copy(existing, new_arm);
						List	   *simplified;

						simplified = mdam_simplify_conjunct(ctx, merged);
						if (simplified != NIL)
						{
							new_result = lappend(new_result, simplified);
							if (list_length(new_result) > MDAM_MAX_DNF_CONJUNCTS)
								return NIL;
						}
					}
				}

				result = new_result;
				if (result == NIL)
					return NIL;
			}
			return result;
		}
		else
		{
			/* NOT -- can't handle */
			return NIL;
		}
	}
	else if (IsA(expr, OpExpr))
	{
		List	   *atoms = mdam_conjunct_from_opexpr(ctx, (OpExpr *) expr);

		if (atoms == NIL)
			return NIL;
		return list_make1(atoms);
	}
	else if (IsA(expr, ScalarArrayOpExpr))
	{
		ScalarArrayOpExpr *saop = (ScalarArrayOpExpr *) expr;
		List	   *atoms;

		/*
		 * NOT IN (i.e. "x <> ALL (array)") expands into a multi-conjunct
		 * DNF of inequality ranges, so it has its own helper.
		 */
		if (!saop->useOr)
			return mdam_dnf_from_not_in_saop(ctx, saop);

		atoms = mdam_conjunct_from_saop(ctx, saop);
		if (atoms == NIL)
			return NIL;
		return list_make1(atoms);
	}
	else if (IsA(expr, NullTest))
	{
		List	   *atoms = mdam_conjunct_from_nulltest(ctx, (NullTest *) expr);

		if (atoms == NIL)
			return NIL;
		return list_make1(atoms);
	}
	else if (IsA(expr, RestrictInfo))
	{
		return mdam_expr_to_dnf(ctx, ((RestrictInfo *) expr)->clause);
	}

	/* Can't handle this expression type */
	return NIL;
}

/*
 * mdam_conjunct_from_opexpr
 *		Convert an OpExpr (e.g. dept = 5, sdate > '1995-01-01') to a
 *		single-element list containing one MdamAtom.
 *		Returns NIL if the expression doesn't match any index column.
 */
static List *
mdam_conjunct_from_opexpr(MdamContext *ctx, OpExpr *opexpr)
{
	IndexOptInfo *index = ctx->index;
	Node	   *leftop,
			   *rightop;
	Oid			opno;
	int			strategy;
	Datum		constval;
	MdamAtom   *atom;

	if (list_length(opexpr->args) != 2)
		return NIL;

	leftop = (Node *) linitial(opexpr->args);
	rightop = (Node *) lsecond(opexpr->args);
	opno = opexpr->opno;

	/* Try to match the operator to an index column */
	for (int colno = 0; colno < ctx->nkeycolumns; colno++)
	{
		Oid			opfamily = index->opfamily[colno];
		bool		is_left;
		Node	   *constNode;

		if (match_index_to_operand(leftop, colno, index) &&
			!contain_volatile_functions(rightop))
		{
			is_left = true;
		}
		else if (match_index_to_operand(rightop, colno, index) &&
				 !contain_volatile_functions(leftop))
		{
			is_left = false;
			opno = get_commutator(opno);
			if (!OidIsValid(opno))
				continue;
		}
		else
			continue;

		/* Check collation compatibility */
		if (!IndexCollMatchesExprColl(index->indexcollations[colno],
									  opexpr->inputcollid))
			continue;

		/* Check the operator is in the opfamily and get its strategy */
		strategy = get_op_opfamily_strategy(opno, opfamily);
		if (strategy == 0)
			continue;

		/* Extract constant value */
		constNode = is_left ? rightop : leftop;

		/* Strip RelabelType */
		if (IsA(constNode, RelabelType))
			constNode = (Node *) ((RelabelType *) constNode)->arg;

		if (!IsA(constNode, Const))
			return NIL;		/* non-constant, can't use for MDAM */

		if (((Const *) constNode)->constisnull)
			return NIL;		/* NULL constant, skip */

		/* Reject cross-type comparisons (for now) */
		if (exprType(constNode) != ctx->col_ctx[colno].typid)
			return NIL;

		constval = ((Const *) constNode)->constvalue;

		/* Map btree strategy to MdamOpType */
		switch (strategy)
		{
			case BTEqualStrategyNumber:
				atom = mdam_make_atom(ctx, colno, MDAM_OP_EQ, constval);
				break;
			case BTLessStrategyNumber:
				atom = mdam_make_atom(ctx, colno, MDAM_OP_LT, constval);
				break;
			case BTLessEqualStrategyNumber:
				atom = mdam_make_atom(ctx, colno, MDAM_OP_LE, constval);
				break;
			case BTGreaterStrategyNumber:
				atom = mdam_make_atom(ctx, colno, MDAM_OP_GT, constval);
				break;
			case BTGreaterEqualStrategyNumber:
				atom = mdam_make_atom(ctx, colno, MDAM_OP_GE, constval);
				break;
			default:
				return NIL;		/* unknown strategy */
		}

		return list_make1(atom);
	}

	return NIL;					/* no matching index column */
}

/*
 * mdam_conjunct_from_saop
 *		Convert a ScalarArrayOpExpr (e.g. dept = ANY(ARRAY[2,4,5])) to an
 *		MdamAtom with MDAM_OP_SAOP.
 */
static List *
mdam_conjunct_from_saop(MdamContext *ctx, ScalarArrayOpExpr *saop)
{
	IndexOptInfo *index = ctx->index;
	Node	   *leftop;
	Node	   *rightop;
	int			strategy;
	MdamAtom   *atom;

	if (!saop->useOr)
		return NIL;				/* ALL, not ANY */

	if (list_length(saop->args) != 2)
		return NIL;

	leftop = (Node *) linitial(saop->args);
	rightop = (Node *) lsecond(saop->args);

	for (int colno = 0; colno < ctx->nkeycolumns; colno++)
	{
		Oid			opfamily = index->opfamily[colno];

		if (!match_index_to_operand(leftop, colno, index))
			continue;
		if (!IndexCollMatchesExprColl(index->indexcollations[colno],
									  saop->inputcollid))
			continue;
		strategy = get_op_opfamily_strategy(saop->opno, opfamily);
		if (strategy == 0)
			continue;

		/* Only equality SAOPs can be converted to IN */
		if (strategy != BTEqualStrategyNumber)
			return NIL;

		/* Extract the array of constants */
		if (IsA(rightop, Const))
		{
			Const	   *arrayConst = (Const *) rightop;
			ArrayType  *arrayVal;
			Datum	   *elems;
			bool	   *nulls;
			int			nelems;
			Oid			elemtype;
			int16		elemlen;
			bool		elembyval;
			char		elemalign;
			int			nvalid;
			Datum	   *valid_elems;

			if (arrayConst->constisnull)
				return NIL;

			arrayVal = DatumGetArrayTypeP(arrayConst->constvalue);
			elemtype = ARR_ELEMTYPE(arrayVal);

			get_typlenbyvalalign(elemtype, &elemlen, &elembyval, &elemalign);
			deconstruct_array(arrayVal, elemtype, elemlen, elembyval,
							  elemalign, &elems, &nulls, &nelems);

			/* Filter out NULLs */
			valid_elems = palloc(sizeof(Datum) * nelems);
			nvalid = 0;
			for (int i = 0; i < nelems; i++)
			{
				if (!nulls[i])
					valid_elems[nvalid++] = elems[i];
			}

			if (nvalid == 0)
				return NIL;

			if (nvalid == 1)
				atom = mdam_make_atom(ctx, colno, MDAM_OP_EQ, valid_elems[0]);
			else
				atom = mdam_make_atom_saop(ctx, colno, valid_elems, nvalid);

			pfree(valid_elems);
			return list_make1(atom);
		}

		/* Non-Const array (ArrayExpr with Params etc.) -- can't handle */
		return NIL;
	}

	return NIL;
}

/*
 * mdam_dnf_from_not_in_saop
 *		Convert a ScalarArrayOpExpr with useOr=false (i.e. "x <> ALL (array)",
 *		the representation of x NOT IN (v1, v2, ..., vN)) into a DNF that
 *		exposes the predicate to btree as an OR of inequality ranges:
 *
 *			(x < v[0])
 *		 OR (v[0] < x < v[1])
 *		 OR ...
 *		 OR (v[N-2] < x < v[N-1])
 *		 OR (v[N-1] < x)
 *
 *		Each arm is a separate conjunct, so downstream MDAM machinery can
 *		shatter, merge, and combine these with predicates on later index
 *		columns (e.g. "foo NOT IN (1,3) AND bar = 5" on an index (foo, bar)).
 *
 *		Returns a list of conjuncts (each a List of MdamAtom *) or NIL if
 *		the predicate can't be transformed.
 */
static List *
mdam_dnf_from_not_in_saop(MdamContext *ctx, ScalarArrayOpExpr *saop)
{
	IndexOptInfo *index = ctx->index;
	Node	   *leftop;
	Node	   *rightop;
	Oid			eq_op;
	int			match_colno = -1;
	Oid			match_opfamily = InvalidOid;
	Oid			coltype;
	Const	   *arrayConst;
	ArrayType  *arrayVal;
	Datum	   *elems;
	bool	   *nulls;
	int			nelems;
	Oid			elemtype;
	int16		elemlen;
	bool		elembyval;
	char		elemalign;
	int			nvalid;
	List	   *dnf;

	Assert(!saop->useOr);

	if (list_length(saop->args) != 2)
		return NIL;

	leftop = (Node *) linitial(saop->args);
	rightop = (Node *) lsecond(saop->args);

	/*
	 * The btree opfamily does not contain <> directly; it contains =, and
	 * <> is recorded as =' s negator.  Look up the negator of the SAOP's
	 * operator and require it to be a btree equality strategy member of a
	 * matching index column's opfamily.
	 */
	eq_op = get_negator(saop->opno);
	if (!OidIsValid(eq_op))
		return NIL;

	for (int colno = 0; colno < ctx->nkeycolumns; colno++)
	{
		Oid			opfamily = index->opfamily[colno];

		if (!match_index_to_operand(leftop, colno, index))
			continue;
		if (!IndexCollMatchesExprColl(index->indexcollations[colno],
									  saop->inputcollid))
			continue;
		if (get_op_opfamily_strategy(eq_op, opfamily) != BTEqualStrategyNumber)
			continue;
		match_colno = colno;
		match_opfamily = opfamily;
		break;
	}

	if (match_colno < 0)
		return NIL;

	/*
	 * The < and > operators are required for the arms.  All real btree
	 * opfamilies provide them, but probe defensively.
	 */
	coltype = index->opcintype[match_colno];
	if (!OidIsValid(get_opfamily_member(match_opfamily, coltype, coltype,
										BTLessStrategyNumber)))
		return NIL;
	if (!OidIsValid(get_opfamily_member(match_opfamily, coltype, coltype,
										BTGreaterStrategyNumber)))
		return NIL;

	if (!IsA(rightop, Const))
		return NIL;
	arrayConst = (Const *) rightop;
	if (arrayConst->constisnull)
		return NIL;

	arrayVal = DatumGetArrayTypeP(arrayConst->constvalue);
	elemtype = ARR_ELEMTYPE(arrayVal);
	get_typlenbyvalalign(elemtype, &elemlen, &elembyval, &elemalign);
	deconstruct_array(arrayVal, elemtype, elemlen, elembyval, elemalign,
					  &elems, &nulls, &nelems);

	/*
	 * SQL semantics: x NOT IN (..., NULL, ...) is never true.  Bail so the
	 * executor evaluates the predicate as a filter (it will reject every
	 * row).  Representing an unconditional-false path inside the MDAM
	 * pipeline would require new "contradiction" infrastructure.
	 */
	for (int i = 0; i < nelems; i++)
	{
		if (nulls[i])
			return NIL;
	}

	if (nelems == 0)
		return NIL;

	sort_datums_canonical(ctx, match_colno, elems, nelems);

	nvalid = 1;
	for (int i = 1; i < nelems; i++)
	{
		if (!mdam_datum_eq(ctx, match_colno, elems[nvalid - 1], elems[i]))
			elems[nvalid++] = elems[i];
	}

	/*
	 * The expansion produces nvalid + 1 conjuncts; respect the DNF cap to
	 * keep planning bounded.
	 */
	if (nvalid + 1 > MDAM_MAX_DNF_CONJUNCTS)
		return NIL;

	dnf = NIL;
	dnf = lappend(dnf,
				  list_make1(mdam_make_atom(ctx, match_colno,
											MDAM_OP_LT, elems[0])));
	for (int i = 0; i < nvalid - 1; i++)
	{
		List	   *arm;

		arm = list_make2(mdam_make_atom(ctx, match_colno,
										MDAM_OP_GT, elems[i]),
						 mdam_make_atom(ctx, match_colno,
										MDAM_OP_LT, elems[i + 1]));
		dnf = lappend(dnf, arm);
	}
	dnf = lappend(dnf,
				  list_make1(mdam_make_atom(ctx, match_colno,
											MDAM_OP_GT, elems[nvalid - 1])));

	return dnf;
}

/*
 * mdam_conjunct_from_nulltest
 *		Convert a NullTest (column IS NULL / column IS NOT NULL) to an
 *		MdamAtom with MDAM_OP_IS_NULL or MDAM_OP_IS_NOT_NULL.
 *
 * Row-wise NullTest (argisrow) is not indexable.  amsearchnulls is implied
 * by the index being btree (the only AM MDAM runs on).
 */
static List *
mdam_conjunct_from_nulltest(MdamContext *ctx, NullTest *nt)
{
	IndexOptInfo *index = ctx->index;
	MdamOpType	op;

	if (nt->argisrow)
		return NIL;

	switch (nt->nulltesttype)
	{
		case IS_NULL:
			op = MDAM_OP_IS_NULL;
			break;
		case IS_NOT_NULL:
			op = MDAM_OP_IS_NOT_NULL;
			break;
		default:
			return NIL;
	}

	for (int colno = 0; colno < ctx->nkeycolumns; colno++)
	{
		if (!match_index_to_operand((Node *) nt->arg, colno, index))
			continue;

		return list_make1(mdam_make_atom(ctx, colno, op, (Datum) 0));
	}

	return NIL;
}


/* ---------------------------------------------------
 * DNF simplification -- Step 1
 * ---------------------------------------------------
 */

/*
 * mdam_simplify_conjunct
 *		Simplify a conjunction (list of atoms) by intersecting constraints
 *		per column, detecting contradictions, and producing a canonical form.
 *
 * Returns NIL if the conjunction is contradictory (empty result set).
 */
static List *
mdam_simplify_conjunct(MdamContext *ctx, List *atoms)
{
	List	   *result = NIL;
	List	  **col_atoms;
	ListCell   *lc;

	col_atoms = palloc0(sizeof(List *) * ctx->nkeycolumns);

	/* Group atoms by column */
	foreach(lc, atoms)
	{
		MdamAtom   *atom = (MdamAtom *) lfirst(lc);

		if (atom->op == MDAM_OP_IS_ANYTHING)
			continue;
		col_atoms[atom->colno] = lappend(col_atoms[atom->colno], atom);
	}

	/* Process each column */
	for (int i = 0; i < ctx->nkeycolumns; i++)
	{
		List	   *atoms_for_col = col_atoms[i];
		Datum	   *eq_values = NULL;
		int			n_eq = 0;
		Datum	   *in_intersection = NULL;
		int			n_in_intersection = -1; /* -1 = not yet set */
		List	   *range_atoms = NIL;

		if (atoms_for_col == NIL)
			continue;

		/* Categorize atoms into EQ, IN, and range types */
		eq_values = palloc(sizeof(Datum) * list_length(atoms_for_col));

		foreach(lc, atoms_for_col)
		{
			MdamAtom   *atom = (MdamAtom *) lfirst(lc);

			switch (atom->op)
			{
				case MDAM_OP_EQ:
					eq_values[n_eq++] = atom->value;
					break;
				case MDAM_OP_SAOP:
					if (n_in_intersection < 0)
					{
						/* First IN: take all values */
						in_intersection = palloc(sizeof(Datum) * atom->n_in_values);
						memcpy(in_intersection, atom->in_values,
							   sizeof(Datum) * atom->n_in_values);
						n_in_intersection = atom->n_in_values;
					}
					else
					{
						/* Intersect with existing IN values */
						int			new_n = 0;

						for (int j = 0; j < n_in_intersection; j++)
						{
							bool		found = false;

							for (int k = 0; k < atom->n_in_values; k++)
							{
								Datum		v = atom->in_values[k];

								if (mdam_datum_eq(ctx, i, in_intersection[j], v))
								{
									found = true;
									break;
								}
							}
							if (found)
								in_intersection[new_n++] = in_intersection[j];
						}
						n_in_intersection = new_n;
						if (n_in_intersection == 0)
							return NIL; /* contradiction */
					}
					break;
				default:
					range_atoms = lappend(range_atoms, atom);
					break;
			}
		}

		/* Check for multiple distinct EQ values = contradiction */
		if (n_eq > 1)
		{
			for (int j = 1; j < n_eq; j++)
			{
				if (!mdam_datum_eq(ctx, i, eq_values[0], eq_values[j]))
					return NIL; /* contradiction: e.g. dept=5 AND dept=10 */
			}
		}

		/*
		 * Determine candidate point values from EQ + IN intersection.
		 */
		if (n_eq > 0 && n_in_intersection >= 0)
		{
			/* Both EQ and IN present: EQ value must be in IN set */
			bool		found = false;

			for (int j = 0; j < n_in_intersection; j++)
			{
				if (mdam_datum_eq(ctx, i, eq_values[0], in_intersection[j]))
				{
					found = true;
					break;
				}
			}
			if (!found)
				return NIL;		/* contradiction */

			/* Result is just the single EQ value */
			n_in_intersection = -1; /* handled via EQ below */
		}

		if (n_eq > 0)
		{
			/* We have a point constraint. Check it against range atoms. */
			if (range_atoms != NIL)
			{
				MdamInterval *range_iv;

				range_iv = mdam_extract_interval(ctx, i, range_atoms);
				if (range_iv == NULL)
					return NIL; /* contradictory ranges */
				if (!mdam_point_in_interval(ctx, i, eq_values[0], range_iv))
					return NIL; /* EQ value outside range */
			}
			result = lappend(result, mdam_make_atom(ctx, i, MDAM_OP_EQ,
													eq_values[0]));
		}
		else if (n_in_intersection >= 0)
		{
			/* Filter IN values by range if present */
			if (range_atoms != NIL)
			{
				MdamInterval *range_iv;
				int			kept = 0;

				range_iv = mdam_extract_interval(ctx, i, range_atoms);

				if (range_iv == NULL)
					return NIL;

				for (int j = 0; j < n_in_intersection; j++)
				{
					if (mdam_point_in_interval(ctx, i, in_intersection[j],
											   range_iv))
						in_intersection[kept++] = in_intersection[j];
				}
				n_in_intersection = kept;
			}

			if (n_in_intersection == 0)
				return NIL;		/* all IN values filtered out */

			if (n_in_intersection == 1)
			{
				MdamAtom   *eq;

				eq = mdam_make_atom(ctx, i, MDAM_OP_EQ, in_intersection[0]);
				result = lappend(result, eq);
			}
			else
			{
				MdamAtom   *saop;

				sort_datums_canonical(ctx, i, in_intersection, n_in_intersection);
				saop = mdam_make_atom_saop(ctx, i, in_intersection,
										   n_in_intersection);
				result = lappend(result, saop);
			}
		}
		else if (range_atoms != NIL)
		{
			MdamInterval *iv = mdam_extract_interval(ctx, i, range_atoms);
			List	   *iv_atoms;

			if (iv == NULL)
				return NIL;

			iv_atoms = mdam_interval_to_atoms(ctx, i, iv);
			result = list_concat(result, iv_atoms);
		}
	}

	pfree(col_atoms);
	return result;
}


/* ---------------------------------------------------
 * Interval arithmetic
 * ---------------------------------------------------
 */

/*
 * Tighten one side of an interval to incorporate (val, inclusive).
 * If the existing bound is already strictly tighter, leave it alone; if
 * equal, an exclusive new bound dominates an inclusive existing one.
 */
static void
tighten_bound(MdamContext *ctx, int colno, MdamInterval *iv,
			  MdamBound side, Datum val, bool inclusive)
{
	Datum	   *cur = (side == MDAM_LO) ? &iv->lo : &iv->hi;
	bool	   *incl = (side == MDAM_LO) ? &iv->lo_inclusive : &iv->hi_inclusive;
	bool	   *inf = (side == MDAM_LO) ? &iv->lo_infinite : &iv->hi_infinite;
	int			sign = (side == MDAM_LO) ? 1 : -1;
	int			cmp;

	if (*inf)
	{
		*cur = val;
		*incl = inclusive;
		*inf = false;
		return;
	}
	cmp = mdam_compare(ctx, colno, val, *cur) * sign;
	if (cmp > 0)
	{
		*cur = val;
		*incl = inclusive;
	}
	else if (cmp == 0)
		*incl = *incl && inclusive;
}

/*
 * mdam_extract_interval
 *		Compute the effective interval from a list of ANDed atoms on one column.
 *		Returns NULL for contradictory constraints.
 *
 * SAOPs are rejected: a discrete value set has no lossless interval
 * representation, and no opclass infrastructure to derive one.  Callers
 * must filter SAOP-bearing atom lists via mdam_col_atoms_has_saop.
 */
static MdamInterval *
mdam_extract_interval(MdamContext *ctx, int colno, List *col_atoms)
{
	MdamInterval *iv = palloc0(sizeof(MdamInterval));
	ListCell   *lc;

	Assert(!mdam_col_atoms_has_saop(col_atoms));

	iv->lo_infinite = true;
	iv->hi_infinite = true;
	iv->lo_inclusive = true;
	iv->hi_inclusive = true;

	foreach(lc, col_atoms)
	{
		MdamAtom   *atom = (MdamAtom *) lfirst(lc);

		switch (atom->op)
		{
			case MDAM_OP_IS_ANYTHING:
				continue;

			case MDAM_OP_IS_NULL:

				/*
				 * IS NULL is orthogonal to any value-based constraint on the
				 * same column.  k IS NULL AND k <op> v is always
				 * contradictory because strict comparison operators are NULL
				 * for NULL inputs.
				 */
				if (iv->is_null_interval)
					continue;	/* duplicate IS NULL */
				if (iv->is_not_null_interval ||
					!iv->lo_infinite || !iv->hi_infinite)
				{
					pfree(iv);
					return NULL;
				}
				iv->is_null_interval = true;
				continue;

			case MDAM_OP_IS_NOT_NULL:

				/*
				 * IS NOT NULL is trivially true against any strict
				 * value-based constraint (those already exclude NULL).  We
				 * still record it via is_not_null_interval so that, if no
				 * value-based atom ever tightens this interval, the output
				 * stage emits an explicit IS NOT NULL scankey rather than
				 * leaving the column unconstrained (which would scan NULLs).
				 */
				if (iv->is_null_interval)
				{
					pfree(iv);
					return NULL;	/* IS NULL AND IS NOT NULL */
				}
				iv->is_not_null_interval = true;
				continue;

			case MDAM_OP_EQ:
				tighten_bound(ctx, colno, iv, MDAM_LO, atom->value, true);
				tighten_bound(ctx, colno, iv, MDAM_HI, atom->value, true);
				break;
			case MDAM_OP_LT:
				tighten_bound(ctx, colno, iv, MDAM_HI, atom->value, false);
				break;
			case MDAM_OP_LE:
				tighten_bound(ctx, colno, iv, MDAM_HI, atom->value, true);
				break;
			case MDAM_OP_GT:
				tighten_bound(ctx, colno, iv, MDAM_LO, atom->value, false);
				break;
			case MDAM_OP_GE:
				tighten_bound(ctx, colno, iv, MDAM_LO, atom->value, true);
				break;
			case MDAM_OP_RANGE_EXCL:
				tighten_bound(ctx, colno, iv, MDAM_LO, atom->range_lo, false);
				tighten_bound(ctx, colno, iv, MDAM_HI, atom->range_hi, false);
				break;
			default:
				elog(ERROR, "unexpected MdamOpType %d in mdam_extract_interval",
					 (int) atom->op);
				break;
		}

		/*
		 * Mixing a value-based atom with a prior IS NULL is contradictory
		 * (the value space and the NULL "point" are disjoint).
		 */
		if (iv->is_null_interval)
		{
			pfree(iv);
			return NULL;
		}

		/* Check whether the tightened interval became empty. */
		if (!iv->lo_infinite && !iv->hi_infinite)
		{
			int			cmp = mdam_compare(ctx, colno, iv->lo, iv->hi);

			if (cmp > 0 ||
				(cmp == 0 && (!iv->lo_inclusive || !iv->hi_inclusive)))
			{
				pfree(iv);
				return NULL;
			}
		}
	}

	return iv;
}

/*
 * Check if a point value lies within an interval.
 */
static bool
mdam_point_in_interval(MdamContext *ctx, int colno, Datum point,
					   MdamInterval *iv)
{
	/* A non-NULL value is never contained in an IS NULL interval. */
	if (iv->is_null_interval)
		return false;

	if (!iv->lo_infinite)
	{
		int			cmp = mdam_compare(ctx, colno, point, iv->lo);

		if (cmp < 0 || (cmp == 0 && !iv->lo_inclusive))
			return false;
	}
	if (!iv->hi_infinite)
	{
		int			cmp = mdam_compare(ctx, colno, point, iv->hi);

		if (cmp > 0 || (cmp == 0 && !iv->hi_inclusive))
			return false;
	}
	return true;
}

/*
 * Convert an interval back to a list of MdamAtom constraints.
 */
static List *
mdam_interval_to_atoms(MdamContext *ctx, int colno, MdamInterval *iv)
{
	List	   *result = NIL;

	if (iv->is_null_interval)
		return list_make1(mdam_make_atom(ctx, colno, MDAM_OP_IS_NULL, (Datum) 0));

	if (iv->lo_infinite && iv->hi_infinite)
	{
		if (iv->is_not_null_interval)
			return list_make1(mdam_make_atom(ctx, colno,
											 MDAM_OP_IS_NOT_NULL,
											 (Datum) 0));
		return NIL;				/* unconstrained */
	}

	if (!iv->lo_infinite && !iv->hi_infinite &&
		mdam_compare(ctx, colno, iv->lo, iv->hi) == 0)
	{
		/* Point interval: EQ */
		return list_make1(mdam_make_atom(ctx, colno, MDAM_OP_EQ, iv->lo));
	}

	if (!iv->lo_infinite)
	{
		MdamOpType	op = iv->lo_inclusive ? MDAM_OP_GE : MDAM_OP_GT;

		result = lappend(result, mdam_make_atom(ctx, colno, op, iv->lo));
	}

	if (!iv->hi_infinite)
	{
		MdamOpType	op = iv->hi_inclusive ? MDAM_OP_LE : MDAM_OP_LT;

		result = lappend(result, mdam_make_atom(ctx, colno, op, iv->hi));
	}

	return result;
}

/*
 * Do two intervals overlap on the given column?  An "overlap" here is
 * any value the two intervals share -- a touching boundary counts only
 * if both sides are inclusive at the touch.
 */
static bool
mdam_intervals_overlap(MdamContext *ctx, int colno,
					   MdamInterval *a, MdamInterval *b)
{
	/*
	 * IS NULL is disjoint from any value-based interval (and from IS NOT
	 * NULL); two IS NULL intervals share the NULL "point".  An unbounded IS
	 * NOT NULL interval covers the entire non-NULL value space, so it
	 * overlaps any value-bounded interval.  (Bounded IS NOT NULL intervals
	 * fall through to the normal lo/hi overlap test below.)
	 */
	if (a->is_null_interval || b->is_null_interval)
		return a->is_null_interval && b->is_null_interval;
	if (a->is_not_null_interval && a->lo_infinite && a->hi_infinite)
		return true;
	if (b->is_not_null_interval && b->lo_infinite && b->hi_infinite)
		return true;

	if (!a->hi_infinite && !b->lo_infinite)
	{
		int			cmp = mdam_compare(ctx, colno, a->hi, b->lo);

		if (cmp < 0)
			return false;
		if (cmp == 0 && (!a->hi_inclusive || !b->lo_inclusive))
			return false;
	}
	if (!b->hi_infinite && !a->lo_infinite)
	{
		int			cmp = mdam_compare(ctx, colno, b->hi, a->lo);

		if (cmp < 0)
			return false;
		if (cmp == 0 && (!b->hi_inclusive || !a->lo_inclusive))
			return false;
	}
	return true;
}

/*
 * Merge a list of intervals into a minimal sorted list of non-overlapping
 * intervals.  Input intervals should already be sorted by lower bound.
 */
static List *
mdam_merge_interval_list(MdamContext *ctx, int colno, List *intervals)
{
	List	   *merged = NIL;
	ListCell   *lc;
	ListCell   *lc2;
	bool		input_covers_null = false;
	bool		has_unconstrained;

	/*
	 * Pre-scan inputs to detect whether NULL rows are covered by some input
	 * interval -- either explicitly via an IS NULL pseudo-interval, or
	 * implicitly via a truly-unconstrained value interval (lo and hi both
	 * infinite, produced by extract_interval from an empty or IS_ANYTHING
	 * atom list).  When NULL is covered by input, an output value interval
	 * spanning (-inf, +inf) safely includes NULL (the index scan returns NULL
	 * rows when there's no scan key on the column).  When NULL is NOT covered
	 * by input, merging two value-bounded inputs into an unconstrained output
	 * would wrongly admit NULL rows the source predicates excluded.
	 */
	foreach(lc, intervals)
	{
		MdamInterval *iv = (MdamInterval *) lfirst(lc);

		if (iv->is_null_interval ||
			(iv->lo_infinite && iv->hi_infinite && !iv->is_not_null_interval))
		{
			input_covers_null = true;
			break;
		}
	}

	foreach(lc, intervals)
	{
		MdamInterval *cur = (MdamInterval *) lfirst(lc);
		MdamInterval *last;
		bool		can_merge;

		if (merged == NIL)
		{
			merged = list_make1(cur);
			continue;
		}

		last = (MdamInterval *) llast(merged);

		/*
		 * IS NULL is disjoint from any value-based interval and only
		 * coalesces with another IS NULL.  Don't merge a value interval into
		 * an IS NULL slot or vice versa -- doing so would widen the effective
		 * predicate.
		 */
		if (last->is_null_interval || cur->is_null_interval)
		{
			if (last->is_null_interval && cur->is_null_interval)
				continue;		/* duplicate IS NULL, drop cur */
			merged = lappend(merged, cur);
			continue;
		}

		/*
		 * Check if current interval overlaps or is adjacent to last.
		 * "adjacent" means the end of last touches the start of current.
		 */
		if (last->hi_infinite)
			can_merge = true;
		else if (cur->lo_infinite)
			can_merge = true;
		else
		{
			int			cmp = mdam_compare(ctx, colno, cur->lo, last->hi);

			can_merge = (cmp < 0 ||
						 (cmp == 0 && (last->hi_inclusive || cur->lo_inclusive)));
		}

		if (!can_merge)
		{
			merged = lappend(merged, cur);
			continue;
		}

		/*
		 * Special case: extending last->hi to +inf when last->lo is already
		 * -inf would yield an unconstrained value interval (-inf, +inf).  An
		 * index scan with no scan key on this column matches NULL rows too,
		 * but the union of two value-bounded inputs (each of which excludes
		 * NULL via strict comparison semantics) does NOT include NULL.
		 * Emitting one merged (-inf, +inf) interval would therefore wrongly
		 * return rows where the column is NULL.
		 *
		 * Only apply this when no input interval already covers NULL (via IS
		 * NULL or a truly-unconstrained value interval).  If some input does
		 * cover NULL, the original predicate's union already includes NULL
		 * rows, so merging to a plain unconstrained (-inf, +inf) is correct
		 * -- it matches that semantics exactly.
		 *
		 * can_merge guarantees last and cur overlap or touch with an
		 * inclusive boundary, so their value-space union is (-inf, +inf).
		 * Combined with !input_covers_null this is exactly IS NOT NULL --
		 * merge to (-inf, +inf) and mark is_not_null_interval so
		 * mdam_interval_to_atoms emits an explicit IS NOT NULL scankey.
		 */
		if (cur->hi_infinite && last->lo_infinite && !last->hi_infinite &&
			!cur->lo_infinite && !input_covers_null)
		{
			last->hi_infinite = true;
			last->is_not_null_interval = true;
			continue;
		}

		/* Merge: extend last's upper bound */
		if (cur->hi_infinite)
			last->hi_infinite = true;
		else if (!last->hi_infinite)
		{
			int			cmp = mdam_compare(ctx, colno, cur->hi, last->hi);

			if (cmp > 0)
			{
				last->hi = cur->hi;
				last->hi_inclusive = cur->hi_inclusive;
			}
			else if (cmp == 0)
				last->hi_inclusive = last->hi_inclusive || cur->hi_inclusive;
		}

		/*
		 * If the merged interval ended up at (-inf, +inf) and at least one
		 * input was IS_NOT_NULL, the union excludes NULL.  Mark last so
		 * mdam_interval_to_atoms emits an explicit IS NOT NULL scankey rather
		 * than leaving the column unconstrained (which would match NULLs
		 * too).
		 */
		if (last->lo_infinite && last->hi_infinite &&
			(last->is_not_null_interval || cur->is_not_null_interval))
			last->is_not_null_interval = true;
	}

	/*
	 * Drop any IS NULL interval when an unconstrained (-inf, +inf)
	 * value-based interval is also present.  An Index Scan with no scan key
	 * on this column already returns NULL rows alongside value rows, so
	 * emitting an additional IS NULL sub-path would duplicate them, returning
	 * duplicate rows.  IS NOT NULL pseudo-intervals look unconstrained in
	 * lo/hi but explicitly exclude NULL, so they don't trigger this pruning.
	 */
	has_unconstrained = false;
	foreach(lc2, merged)
	{
		MdamInterval *iv = (MdamInterval *) lfirst(lc2);

		if (!iv->is_null_interval && !iv->is_not_null_interval &&
			iv->lo_infinite && iv->hi_infinite)
		{
			has_unconstrained = true;
			break;
		}
	}

	if (has_unconstrained)
	{
		List	   *pruned = NIL;

		foreach(lc2, merged)
		{
			MdamInterval *iv = (MdamInterval *) lfirst(lc2);

			if (!iv->is_null_interval)
				pruned = lappend(pruned, iv);
		}
		merged = pruned;
	}

	return merged;
}


/* ---------------------------------------------------
 * Generate initial retrievals (shattering) -- Step 2
 * ---------------------------------------------------
 */

/*
 * Collect all critical (boundary) values for a column from the DNF.
 * Returns a sorted, deduplicated list of Datum values (as DatumCopy'd).
 */
static List *
mdam_get_critical_points(MdamContext *ctx, int colno, List *dnf)
{
	List	   *points = NIL;
	ListCell   *lc,
			   *lc2;
	int			ip,
				n,
				out;
	Datum	   *arr;

	/* Collect all values */
	foreach(lc, dnf)
	{
		List	   *conjunct = (List *) lfirst(lc);

		foreach(lc2, conjunct)
		{
			MdamAtom   *atom = (MdamAtom *) lfirst(lc2);

			if (atom->colno != colno)
				continue;

			switch (atom->op)
			{
				case MDAM_OP_EQ:
				case MDAM_OP_LT:
				case MDAM_OP_LE:
				case MDAM_OP_GT:
				case MDAM_OP_GE:
					points = lappend(points, (void *) atom->value);
					break;
				case MDAM_OP_SAOP:
					for (int i = 0; i < atom->n_in_values; i++)
					{
						void	   *v = (void *) atom->in_values[i];

						points = lappend(points, v);
					}
					break;
				case MDAM_OP_RANGE_EXCL:
					points = lappend(points,
									 (void *) atom->range_lo);
					points = lappend(points,
									 (void *) atom->range_hi);
					break;
				default:
					break;
			}
		}
	}

	if (points == NIL)
		return NIL;

	/* Convert to array, sort, deduplicate */
	n = list_length(points);
	arr = palloc(sizeof(Datum) * n);
	ip = 0;
	foreach(lc, points)
	{
		arr[ip++] = (Datum) lfirst(lc);
	}

	sort_datums_canonical(ctx, colno, arr, n);

	/* Deduplicate */
	out = 0;
	for (int i = 0; i < n; i++)
	{
		if (out == 0 || mdam_compare(ctx, colno, arr[i], arr[out - 1]) != 0)
			arr[out++] = arr[i];
	}
	n = out;

	/*
	 * Safety limit.  NIL is also the "column unconstrained" return for the
	 * "no atoms produced points" path above, and the caller can't tell the
	 * two apart, so signal overflow via the truncation flag and bail MDAM out
	 * at the top level rather than silently degrading the column to
	 * IS_ANYTHING (which loses coverage).
	 */
	if (n > MDAM_MAX_CRITICAL_POINTS)
	{
		ctx->retrievals_truncated = true;
		pfree(arr);
		return NIL;
	}

	/* Convert back to list */
	points = NIL;
	for (int i = 0; i < n; i++)
	{
		points = lappend(points, (void *) arr[i]);
	}

	pfree(arr);
	return points;
}

/*
 * Generate elementary intervals for a column from its critical points.
 * For critical points [a, b]: <a, =a, (a,b), =b, >b
 *
 * When 'dnf' is non-NIL and any conjunct could match a NULL-valued row
 * (either silent on this column or carrying an IS NULL atom), also append
 * an MDAM_OP_IS_NULL pseudo-interval.  Without it the recursion would
 * shatter the silent / IS NULL arm into {< v, = v, > v} strict comparisons
 * that all drop NULL rows.  Likewise, when no critical points exist but
 * some arm carries an IS NULL or IS NOT NULL atom, partition the column
 * into {IS NULL, IS NOT NULL} pseudo-intervals so each arm's NULL handling
 * is honored independently.
 */
static List *
mdam_generate_elementary_intervals(MdamContext *ctx, int colno,
								   List *critical_points,
								   List *dnf)
{
	List	   *intervals = NIL;
	int			npts;
	Datum	   *pts;
	ListCell   *lc;
	int			ip;
	bool		need_is_null = false;
	bool		need_is_not_null = false;
	bool		any_is_null_atom = false;
	bool		any_is_not_null_atom = false;
	bool		any_silent_arm = false;

	if (dnf != NIL)
	{
		ListCell   *lc_c;

		foreach(lc_c, dnf)
		{
			List	   *conjunct = (List *) lfirst(lc_c);
			ListCell   *lc_a;
			bool		constrains_col = false;

			foreach(lc_a, conjunct)
			{
				MdamAtom   *atom = (MdamAtom *) lfirst(lc_a);

				if (atom->colno != colno)
					continue;
				if (atom->op == MDAM_OP_IS_ANYTHING)
					continue;
				constrains_col = true;
				if (atom->op == MDAM_OP_IS_NULL)
					any_is_null_atom = true;
				else if (atom->op == MDAM_OP_IS_NOT_NULL)
					any_is_not_null_atom = true;
			}
			if (!constrains_col)
				any_silent_arm = true;
		}
	}

	/*
	 * An IS NULL pseudo is required whenever some arm could match a
	 * NULL-valued row at this column: silent arms (no atom -> match
	 * anything), explicit IS NULL atoms, and IS NOT NULL atoms (so the IS NOT
	 * NULL arm has a NULL slot to reject, preventing a silent sibling from
	 * inheriting NULL rows it shouldn't).
	 */
	need_is_null = any_silent_arm || any_is_null_atom || any_is_not_null_atom;

	if (critical_points == NIL)
	{
		/*
		 * No critical points.  If no NULL-related concern, the column is
		 * truly unconstrained: emit a single IS_ANYTHING interval.
		 */
		if (!any_is_null_atom && !any_is_not_null_atom)
		{
			MdamAtom   *atom = palloc0(sizeof(MdamAtom));

			atom->colno = colno;
			atom->op = MDAM_OP_IS_ANYTHING;
			return list_make1(atom);
		}

		/*
		 * Partition into {IS NULL, IS NOT NULL} pseudo-intervals so each
		 * arm's NULL handling is honored.  IS NOT NULL pseudo is always
		 * needed when partitioning here (otherwise silent / IS NOT NULL arms
		 * would have no non-NULL slot to match).
		 */
		need_is_not_null = true;
	}

	if (critical_points != NIL)
	{
		MdamAtom   *gt;

		npts = list_length(critical_points);
		pts = palloc(sizeof(Datum) * npts);
		ip = 0;
		foreach(lc, critical_points)
		{
			pts[ip++] = (Datum) lfirst(lc);
		}

		/* < first point */
		intervals = lappend(intervals,
							mdam_make_atom(ctx, colno, MDAM_OP_LT, pts[0]));

		for (int i = 0; i < npts; i++)
		{
			MdamAtom   *eq;

			/* = this point */
			eq = mdam_make_atom(ctx, colno, MDAM_OP_EQ, pts[i]);
			intervals = lappend(intervals, eq);

			/* (this, next) exclusive range */
			if (i + 1 < npts && mdam_compare(ctx, colno, pts[i], pts[i + 1]) < 0)
			{
				Datum		lo = pts[i];
				Datum		hi = pts[i + 1];
				MdamAtom   *r = mdam_make_atom_range(ctx, colno, lo, hi);

				intervals = lappend(intervals, r);
			}
		}

		/* > last point */
		gt = mdam_make_atom(ctx, colno, MDAM_OP_GT, pts[npts - 1]);
		intervals = lappend(intervals, gt);

		pfree(pts);
	}

	if (need_is_null)
	{
		MdamAtom   *n = mdam_make_atom(ctx, colno, MDAM_OP_IS_NULL, (Datum) 0);

		intervals = lappend(intervals, n);
	}

	if (need_is_not_null)
	{
		MdamAtom   *nn = mdam_make_atom(ctx, colno, MDAM_OP_IS_NOT_NULL,
										(Datum) 0);

		intervals = lappend(intervals, nn);
	}

	return intervals;
}

/*
 * Convenience: critical points + elementary intervals for one column in one
 * call.  Used by the shatter/expand passes that don't need critical points
 * for their own purposes.
 */
static List *
mdam_elem_intervals_for_col(MdamContext *ctx, int colno, List *dnf)
{
	List	   *cpts = mdam_get_critical_points(ctx, colno, dnf);

	return mdam_generate_elementary_intervals(ctx, colno, cpts, dnf);
}

/*
 * mdam_generate_retrievals
 *		Step 2: Generate initial non-overlapping retrieval paths by shattering
 *		the value space into elementary intervals and filtering.
 */
static List *
mdam_generate_retrievals(MdamContext *ctx, List *dnf)
{
	List	   *result = NIL;

#ifdef MDAM_DEBUG
	for (int col = 0; col < ctx->nkeycolumns; col++)
	{
		List	   *cpts = mdam_get_critical_points(ctx, col, dnf);
		List	   *eivs;

		eivs = mdam_generate_elementary_intervals(ctx, col, cpts, dnf);
		MDAM_LOG("MDAM: col %d: %d critical points, %d elementary intervals",
				 col, list_length(cpts), list_length(eivs));
	}
#endif

	mdam_generate_recursive(ctx, dnf, 0, NIL, &result);

	/*
	 * Append the contradictory cross-product arms stashed during DNF
	 * extraction so step 3a's simplification pass has a chance to look at
	 * them: SAOP-bearing arms can narrow against siblings, and arms that
	 * remain fully contradictory drop out cleanly.  Surviving arms become
	 * real Append children backed by per-arm Index Conds; nbtree's runtime
	 * _bt_preprocess_keys is the last-line safety net for any residual
	 * inconsistency that the planner didn't resolve.
	 */
	if (ctx->contradictory != NIL)
	{
		ListCell   *lc;

		foreach(lc, ctx->contradictory)
		{
			List	   *conjunct = (List *) lfirst(lc);
			List	   *copy = NIL;
			ListCell   *lc2;

			foreach(lc2, conjunct)
			{
				MdamAtom   *atom = (MdamAtom *) lfirst(lc2);

				copy = lappend(copy, mdam_copy_atom(ctx, atom));
			}
			result = lappend(result, copy);
		}
	}

	return result;
}

/*
 * Recursive helper: enumerate combinations of elementary intervals across
 * all index columns, keeping only paths that satisfy at least one DNF clause.
 */
static void
mdam_generate_recursive(MdamContext *ctx, List *orig_dnf,
						int col_idx, List *current_path, List **result)
{
	List	   *elem_intervals;
	ListCell   *lc;

	check_stack_depth();

	/* Base case: all columns processed */
	if (col_idx >= ctx->nkeycolumns)
	{
		if (mdam_retrieval_satisfies_dnf(ctx, orig_dnf, current_path))
		{
			/* Deep copy the path atoms */
			List	   *copy = NIL;
			ListCell   *lc2;

			foreach(lc2, current_path)
			{
				MdamAtom   *atom = (MdamAtom *) lfirst(lc2);

				copy = lappend(copy, mdam_copy_atom(ctx, atom));
			}
			*result = lappend(*result, copy);
		}
		return;
	}

	/* Safety: bail out if we already have too many retrievals */
	if (list_length(*result) >= MDAM_MAX_RETRIEVALS)
	{
		ctx->retrievals_truncated = true;
		return;
	}

	elem_intervals = mdam_elem_intervals_for_col(ctx, col_idx, orig_dnf);

	foreach(lc, elem_intervals)
	{
		MdamAtom   *interval_atom = (MdamAtom *) lfirst(lc);
		List	   *next_path;

		if (interval_atom->op == MDAM_OP_IS_ANYTHING)
			next_path = list_copy(current_path);
		else
			next_path = lappend(list_copy(current_path), interval_atom);

		mdam_generate_recursive(ctx, orig_dnf, col_idx + 1,
								next_path, result);

		if (list_length(*result) >= MDAM_MAX_RETRIEVALS)
		{
			ctx->retrievals_truncated = true;
			return;
		}
	}
}

/*
 * Check if a retrieval path satisfies at least one DNF conjunct.
 */
static bool
mdam_retrieval_satisfies_dnf(MdamContext *ctx, List *orig_dnf, List *path)
{
	ListCell   *lc;

	foreach(lc, orig_dnf)
	{
		List	   *conjunct = (List *) lfirst(lc);
		bool		all_satisfied = true;
		ListCell   *lc2;

		foreach(lc2, conjunct)
		{
			MdamAtom   *dnf_atom = (MdamAtom *) lfirst(lc2);
			MdamAtom   *path_atom = NULL;
			ListCell   *lc3;

			/* Find the path atom for this column */
			foreach(lc3, path)
			{
				MdamAtom   *pa = (MdamAtom *) lfirst(lc3);

				if (pa->colno == dnf_atom->colno)
				{
					path_atom = pa;
					break;
				}
			}

			/* If no path atom, column is unconstrained (IS_ANYTHING) */
			if (path_atom == NULL)
			{
				/*
				 * An unconstrained column in the path is compatible with any
				 * DNF atom -- the path includes all values for this column.
				 */
				continue;
			}

			if (!mdam_atoms_compatible(ctx, dnf_atom, path_atom))
			{
				all_satisfied = false;
				break;
			}
		}

		if (all_satisfied)
			return true;
	}

	return false;
}

/*
 * Test whether `point` lies within the value space of `atom`.
 * IS_NULL and IS_ANYTHING are filtered by the caller.  IS_NOT_NULL matches
 * any non-NULL value (callers only pass concrete value-space points here).
 */
static bool
atom_contains_point(MdamContext *ctx, MdamAtom *atom, Datum point)
{
	int			colno = atom->colno;

	switch (atom->op)
	{
		case MDAM_OP_EQ:
			return mdam_datum_eq(ctx, colno, point, atom->value);
		case MDAM_OP_LT:
			return mdam_compare(ctx, colno, point, atom->value) < 0;
		case MDAM_OP_LE:
			return mdam_compare(ctx, colno, point, atom->value) <= 0;
		case MDAM_OP_GT:
			return mdam_compare(ctx, colno, point, atom->value) > 0;
		case MDAM_OP_GE:
			return mdam_compare(ctx, colno, point, atom->value) >= 0;
		case MDAM_OP_SAOP:
			for (int i = 0; i < atom->n_in_values; i++)
			{
				if (mdam_datum_eq(ctx, colno, point, atom->in_values[i]))
					return true;
			}
			return false;
		case MDAM_OP_RANGE_EXCL:
			return (mdam_compare(ctx, colno, point, atom->range_lo) > 0 &&
					mdam_compare(ctx, colno, point, atom->range_hi) < 0);
		case MDAM_OP_IS_NOT_NULL:
			return true;
		case MDAM_OP_IS_NULL:
		case MDAM_OP_IS_ANYTHING:
			break;				/* filtered by caller */
	}
	return false;
}

/*
 * Check whether two atoms on the same column have overlapping value spaces.
 */
static bool
mdam_atoms_compatible(MdamContext *ctx, MdamAtom *dnf_atom,
					  MdamAtom *path_atom)
{
	int			colno = dnf_atom->colno;
	List	   *both;
	MdamInterval *iv;

	Assert(colno == path_atom->colno);

	if (path_atom->op == MDAM_OP_IS_ANYTHING ||
		dnf_atom->op == MDAM_OP_IS_ANYTHING)
		return true;

	/*
	 * IS NULL only matches IS NULL on either side.  Strict comparison
	 * operators (EQ/LT/LE/GT/GE/SAOP/RANGE_EXCL) evaluate to NULL (treated as
	 * FALSE) for NULL inputs, so the value-space and the IS NULL "point" are
	 * disjoint.
	 */
	if (path_atom->op == MDAM_OP_IS_NULL || dnf_atom->op == MDAM_OP_IS_NULL)
		return (path_atom->op == MDAM_OP_IS_NULL &&
				dnf_atom->op == MDAM_OP_IS_NULL);

	/*
	 * IS NOT NULL is compatible with any value-based atom (those already
	 * exclude NULL) and with another IS NOT NULL.  Incompatibility with IS
	 * NULL is handled by the branch above.
	 */
	if (path_atom->op == MDAM_OP_IS_NOT_NULL ||
		dnf_atom->op == MDAM_OP_IS_NOT_NULL)
		return true;

	/* If either side is a point (EQ), test point-in-other-atom directly. */
	if (path_atom->op == MDAM_OP_EQ)
		return atom_contains_point(ctx, dnf_atom, path_atom->value);
	if (dnf_atom->op == MDAM_OP_EQ)
		return atom_contains_point(ctx, path_atom, dnf_atom->value);

	/*
	 * SAOP vs non-SAOP: enumerating IN values gives an exact answer; the
	 * generic interval intersection below would over-approximate the SAOP as
	 * [min,max] and could report false overlaps.
	 */
	if (dnf_atom->op == MDAM_OP_SAOP && path_atom->op != MDAM_OP_SAOP)
	{
		for (int i = 0; i < dnf_atom->n_in_values; i++)
		{
			if (atom_contains_point(ctx, path_atom, dnf_atom->in_values[i]))
				return true;
		}
		return false;
	}
	if (path_atom->op == MDAM_OP_SAOP && dnf_atom->op != MDAM_OP_SAOP)
	{
		for (int i = 0; i < path_atom->n_in_values; i++)
		{
			if (atom_contains_point(ctx, dnf_atom, path_atom->in_values[i]))
				return true;
		}
		return false;
	}

	/*
	 * Two SAOPs: compatible iff their value sets share at least one element.
	 * Iterate explicitly; there is no interval representation.
	 */
	if (dnf_atom->op == MDAM_OP_SAOP && path_atom->op == MDAM_OP_SAOP)
	{
		for (int i = 0; i < dnf_atom->n_in_values; i++)
		{
			for (int j = 0; j < path_atom->n_in_values; j++)
			{
				if (mdam_datum_eq(ctx, colno,
								  dnf_atom->in_values[i],
								  path_atom->in_values[j]))
					return true;
			}
		}
		return false;
	}

	/* General case: interval intersection. */
	both = list_make2(dnf_atom, path_atom);
	iv = mdam_extract_interval(ctx, colno, both);
	list_free(both);
	return (iv != NULL);
}


/* ---------------------------------------------------
 * Merge retrievals -- Step 3
 * ---------------------------------------------------
 */

/*
 * Helper: get atoms for a specific column from a path.
 */
static List *
mdam_atoms_for_col(List *path, int colno)
{
	List	   *result = NIL;
	ListCell   *lc;

	foreach(lc, path)
	{
		MdamAtom   *atom = (MdamAtom *) lfirst(lc);

		if (atom->colno == colno)
			result = lappend(result, atom);
	}
	return result;
}

/*
 * Helper: return true if any atom in the per-column list is a SAOP.
 *
 * extract_interval rejects SAOP atoms outright (a discrete value set has
 * no interval form).  Callers consult this predicate to filter SAOP-
 * bearing atom lists onto a separate code path before extract_interval
 * would error.
 */
static bool
mdam_col_atoms_has_saop(List *col_atoms_list)
{
	ListCell   *lc;

	foreach(lc, col_atoms_list)
	{
		MdamAtom   *a = (MdamAtom *) lfirst(lc);

		if (a->op == MDAM_OP_SAOP)
			return true;
	}
	return false;
}

/*
 * Helper: return true if the column-atom list must be preserved verbatim
 * through step-3 merge.  SAOP-bearing lists qualify (extract_interval
 * would error -- see mdam_col_atoms_has_saop), as do multi-atom shapes:
 * step-2 recursion never produces multi-atom columns, so they only arise
 * from the unsimplified contradictory retrievals re-emitted by
 * mdam_generate_retrievals.
 */
static bool
mdam_col_atoms_preserve_verbatim(List *col_atoms_list)
{
	return (list_length(col_atoms_list) > 1 ||
			mdam_col_atoms_has_saop(col_atoms_list));
}

/*
 * Helper: get atoms for all columns EXCEPT the specified one.
 */
static List *
mdam_atoms_except_col(List *path, int colno)
{
	List	   *result = NIL;
	ListCell   *lc;

	foreach(lc, path)
	{
		MdamAtom   *atom = (MdamAtom *) lfirst(lc);

		if (atom->colno != colno)
			result = lappend(result, atom);
	}
	return result;
}

/*
 * Canonical sort key for a path: sort atoms by (colno, op, value).
 */
static List *
mdam_sort_path_atoms(List *path)
{
	/* Simple: just sort by colno since we have at most one atom per col */
	int			n = list_length(path);
	MdamAtom  **arr;
	List	   *result = NIL;

	if (n <= 1)
		return path;

	arr = palloc(sizeof(MdamAtom *) * n);
	for (int i = 0; i < n; i++)
	{
		arr[i] = (MdamAtom *) list_nth(path, i);
	}

	/* Insertion sort by colno */
	for (int i = 1; i < n; i++)
	{
		MdamAtom   *key = arr[i];
		int			j = i - 1;

		while (j >= 0 && arr[j]->colno > key->colno)
		{
			arr[j + 1] = arr[j];
			j--;
		}
		arr[j + 1] = key;
	}

	for (int i = 0; i < n; i++)
	{
		result = lappend(result, arr[i]);
	}

	pfree(arr);
	return result;
}

/*
 * Helper: a path's "base" on a column is all atoms on columns other than
 * that column, in canonical (colno) order.  Two paths with the same base
 * on a column are candidates for coalescing their atoms on that column.
 */
static List *
mdam_path_base(List *path, int colno)
{
	return mdam_sort_path_atoms(mdam_atoms_except_col(path, colno));
}

/*
 * Test whether two paths' atoms are pairwise equal (same colno, op, and
 * value/in_values/range bounds).  Lists must be in canonical sort order.
 */
static bool
mdam_base_atoms_equal(MdamContext *ctx, List *a, List *b)
{
	ListCell   *lca,
			   *lcb;

	if (list_length(a) != list_length(b))
		return false;

	forboth(lca, a, lcb, b)
	{
		MdamAtom   *aa = (MdamAtom *) lfirst(lca);
		MdamAtom   *ab = (MdamAtom *) lfirst(lcb);

		if (aa->colno != ab->colno || aa->op != ab->op)
			return false;

		switch (aa->op)
		{
			case MDAM_OP_EQ:
			case MDAM_OP_LT:
			case MDAM_OP_LE:
			case MDAM_OP_GT:
			case MDAM_OP_GE:
				if (!mdam_datum_eq(ctx, aa->colno, aa->value, ab->value))
					return false;
				break;
			case MDAM_OP_SAOP:
				{
					if (aa->n_in_values != ab->n_in_values)
						return false;
					for (int i = 0; i < aa->n_in_values; i++)
					{
						if (!mdam_datum_eq(ctx, aa->colno, aa->in_values[i],
										   ab->in_values[i]))
							return false;
					}
				}
				break;
			case MDAM_OP_RANGE_EXCL:
				if (!mdam_datum_eq(ctx, aa->colno, aa->range_lo, ab->range_lo) ||
					!mdam_datum_eq(ctx, aa->colno, aa->range_hi, ab->range_hi))
					return false;
				break;
			default:
				break;
		}
	}
	return true;
}

/*
 * mdam_merge_retrievals
 *		Step 3: Merge retrieval paths by coalescing intervals and folding
 *		EQ values into IN lists.  Process columns in reverse index order.
 */
static List *
mdam_merge_retrievals(MdamContext *ctx, List *paths)
{
	for (int col = ctx->nkeycolumns - 1; col >= 0; col--)
	{
		/*
		 * Stage 1: Interval coalescing.  Group paths by their "base" (atoms
		 * on all columns except the current one), then merge intervals on the
		 * current column.
		 */
		List	   *coalesced = NIL;
		ListCell   *lc;
		bool	   *used = palloc0(sizeof(bool) * list_length(paths));
		int			i = 0;

		foreach(lc, paths)
		{
			List	   *path = (List *) lfirst(lc);
			List	   *base;
			List	   *intervals = NIL;
			List	   *col_atoms_list;
			MdamInterval *iv;
			ListCell   *lc2;
			int			j;

			if (used[i])
			{
				i++;
				continue;
			}
			used[i] = true;

			base = mdam_path_base(path, col);
			col_atoms_list = mdam_atoms_for_col(path, col);

			/*
			 * Some col_atoms_list shapes don't survive an extract_interval ->
			 * interval_to_atoms round-trip: a SAOP has no interval form (its
			 * values are a discrete set), and a multi-atom column mixing
			 * SAOP+EQ or SAOP+range loses the SAOP's discrete constraint.
			 * These shapes only arise from contradictory retrievals
			 * re-emitted by mdam_generate_retrievals. Preserve them verbatim;
			 * the step 3a simplification pass picks them up immediately after
			 * and either narrows the SAOP against the siblings or prunes the
			 * path as contradictory.
			 */
			if (mdam_col_atoms_preserve_verbatim(col_atoms_list))
			{
				List	   *combined = list_concat_copy(base, col_atoms_list);

				coalesced = lappend(coalesced, mdam_sort_path_atoms(combined));
				i++;
				continue;
			}

			iv = mdam_extract_interval(ctx, col, col_atoms_list);
			if (iv)
				intervals = lappend(intervals, iv);
			else if (col_atoms_list != NIL)
			{
				/*
				 * Contradictory atoms on this column (e.g. col=4 AND col=5):
				 * extract_interval can't form a valid interval.  Emit the
				 * path verbatim and skip coalescing; step 3a's simplify pass
				 * prunes it next.
				 */
				List	   *combined = list_concat_copy(base, col_atoms_list);

				coalesced = lappend(coalesced, mdam_sort_path_atoms(combined));
				i++;
				continue;
			}

			/* Find other paths with same base */
			j = i + 1;
			for_each_from(lc2, paths, i + 1)
			{
				List	   *other = (List *) lfirst(lc2);
				List	   *other_base;

				if (used[j])
				{
					j++;
					continue;
				}

				other_base = mdam_path_base(other, col);

				if (mdam_base_atoms_equal(ctx, base, other_base))
				{
					List	   *other_col_atoms = mdam_atoms_for_col(other, col);

					/*
					 * Mirror the lossy-shape guard above: if 'other' is a
					 * preserved contradictory retrieval, don't fold it into
					 * 'intervals' or mark it used.  Let the outer foreach
					 * reach it on its own iteration and preserve it verbatim
					 * there.
					 */
					if (mdam_col_atoms_preserve_verbatim(other_col_atoms))
					{
						j++;
						continue;
					}

					iv = mdam_extract_interval(ctx, col, other_col_atoms);
					if (iv)
						intervals = lappend(intervals, iv);

					used[j] = true;
				}
				j++;
			}

			/* Merge the intervals */
			if (intervals != NIL)
			{
				List	   *merged_ivs;
				ListCell   *lc3;

				merged_ivs = mdam_merge_interval_list(ctx, col, intervals);

				foreach(lc3, merged_ivs)
				{
					MdamInterval *miv = (MdamInterval *) lfirst(lc3);
					List	   *iv_atoms = mdam_interval_to_atoms(ctx, col, miv);
					List	   *new_path = list_concat_copy(base, iv_atoms);
					List	   *sorted = mdam_sort_path_atoms(new_path);

					coalesced = lappend(coalesced, sorted);
				}
			}
			else
			{
				/* No intervals on this column: keep the base as-is */
				coalesced = lappend(coalesced, base);
			}

			i++;
		}

		pfree(used);
		paths = coalesced;

		/* Stage 2: EQ-to-IN merging */
		paths = mdam_merge_eq_to_in(ctx, col, paths, false);
	}

	return paths;
}

/*
 * If `path` has exactly one atom on `colno` and it's an EQ atom, return
 * it.  Otherwise return NULL.
 *
 * mdam_merge_eq_to_in uses this to gate folding: a multi-atom shape on
 * the column (a contradictory retrieval, or a range built from GE+LE)
 * must not be folded -- picking out an EQ and dropping the rest would
 * silently widen the path to cover real rows it shouldn't.
 */
static MdamAtom *
mdam_single_eq_on_col(List *path, int colno)
{
	MdamAtom   *eq = NULL;
	int			count = 0;
	ListCell   *lc;

	foreach(lc, path)
	{
		MdamAtom   *a = (MdamAtom *) lfirst(lc);

		if (a->colno != colno)
			continue;
		if (++count > 1)
			return NULL;
		if (a->op == MDAM_OP_EQ)
			eq = a;
	}
	return eq;
}

/*
 * mdam_merge_eq_to_in
 *		Merge paths that differ only in an EQ value on col into IN lists.
 */
static List *
mdam_merge_eq_to_in(MdamContext *ctx, int colno, List *paths,
					bool adjacent_only)
{
	List	   *result = NIL;
	bool	   *used;
	int			npaths = list_length(paths);
	ListCell   *lc;
	int			ip;

	used = palloc0(sizeof(bool) * npaths);

	ip = 0;
	foreach(lc, paths)
	{
		List	   *path = (List *) lfirst(lc);
		MdamAtom   *eq_atom;
		List	   *base;
		Datum	   *values;
		int			nvalues;
		ListCell   *lc2;
		int			j;

		if (used[ip])
		{
			ip++;
			continue;
		}
		used[ip] = true;

		eq_atom = mdam_single_eq_on_col(path, colno);
		if (eq_atom == NULL)
		{
			result = lappend(result, path);
			ip++;
			continue;
		}

		base = mdam_path_base(path, colno);
		values = palloc(sizeof(Datum) * npaths);
		values[0] = eq_atom->value;
		nvalues = 1;

		/* Find other paths with same base and EQ on this column */
		j = ip + 1;
		for_each_from(lc2, paths, ip + 1)
		{
			List	   *other = (List *) lfirst(lc2);
			MdamAtom   *other_eq;
			List	   *other_base;

			if (used[j])
			{
				if (adjacent_only)
					break;
				j++;
				continue;
			}

			other_eq = mdam_single_eq_on_col(other, colno);
			if (other_eq == NULL)
			{
				if (adjacent_only)
					break;
				j++;
				continue;
			}

			other_base = mdam_path_base(other, colno);

			if (mdam_base_atoms_equal(ctx, base, other_base))
			{
				values[nvalues++] = other_eq->value;
				used[j] = true;
			}
			else if (adjacent_only)
				break;

			j++;
		}

		/* Build merged atom */
		if (nvalues == 1)
			result = lappend(result, path);
		else
		{
			MdamAtom   *merged_atom;
			List	   *new_path;

			for (int k = 0; k < nvalues - 1; k++)
			{
				for (int l = k + 1; l < nvalues; l++)
				{
					if (mdam_compare(ctx, colno, values[k], values[l]) > 0)
					{
						Datum		tmp = values[k];

						values[k] = values[l];
						values[l] = tmp;
					}
				}
			}

			merged_atom = mdam_make_atom_saop(ctx, colno, values, nvalues);
			new_path = lappend(list_copy(base), merged_atom);
			result = lappend(result, mdam_sort_path_atoms(new_path));
		}

		pfree(values);
		ip++;
	}

	pfree(used);
	return result;
}


/* ---------------------------------------------------
 * Expand, sort, coalesce -- Step 4
 * ---------------------------------------------------
 */

/*
 * Get the index of the last constrained column in a path.
 */
static int
mdam_last_constrained_col(List *path)
{
	int			last = -1;
	ListCell   *lc;

	foreach(lc, path)
	{
		MdamAtom   *atom = (MdamAtom *) lfirst(lc);

		if (atom->op != MDAM_OP_IS_ANYTHING && atom->colno > last)
			last = atom->colno;
	}
	return last;
}

/*
 * Compute a sort key for a path based on effective intervals per column.
 * Returns a palloc'd array of MdamInterval structures (one per column).
 */
static MdamInterval *
mdam_get_path_sort_key(MdamContext *ctx, List *path)
{
	MdamInterval *key = palloc0(sizeof(MdamInterval) * ctx->nkeycolumns);

	for (int i = 0; i < ctx->nkeycolumns; i++)
	{
		List	   *col_atoms = mdam_atoms_for_col(path, i);
		MdamInterval *iv;

		/*
		 * For sort positioning a SAOP's extent in key space is exactly
		 * [min(values), max(values)] -- a sound use of the hull because the
		 * key is never converted back to atoms or emitted.  Synthesize a
		 * SAOP-free atom list (range bounds on the hull) and feed it through
		 * the normal interval path so any sibling EQ / range atoms still
		 * tighten the result.
		 */
		if (mdam_col_atoms_has_saop(col_atoms))
		{
			List	   *synth = NIL;
			ListCell   *lc;

			foreach(lc, col_atoms)
			{
				MdamAtom   *a = (MdamAtom *) lfirst(lc);

				if (a->op == MDAM_OP_SAOP)
				{
					Datum		min_v = a->in_values[0];
					Datum		max_v = a->in_values[0];
					MdamAtom   *ge;
					MdamAtom   *le;

					for (int j = 1; j < a->n_in_values; j++)
					{
						if (mdam_compare(ctx, i, a->in_values[j], min_v) < 0)
							min_v = a->in_values[j];
						if (mdam_compare(ctx, i, a->in_values[j], max_v) > 0)
							max_v = a->in_values[j];
					}
					ge = mdam_make_atom(ctx, i, MDAM_OP_GE, min_v);
					le = mdam_make_atom(ctx, i, MDAM_OP_LE, max_v);
					synth = lappend(synth, ge);
					synth = lappend(synth, le);
				}
				else
					synth = lappend(synth, a);
			}
			iv = mdam_extract_interval(ctx, i, synth);
		}
		else
			iv = mdam_extract_interval(ctx, i, col_atoms);

		if (iv)
			key[i] = *iv;
		else
		{
			/* Default: full range */
			key[i].lo_infinite = true;
			key[i].hi_infinite = true;
		}
	}

	return key;
}

/*
 * 3-way compare of two MdamIntervals on a single column.  IS NULL sorts to
 * the column's natural NULL position (nulls_first[col]) relative to
 * value-based intervals; equal-value bounds are tie-broken by inclusiveness
 * (an inclusive bound sorts before/after an exclusive one consistently).
 */
static int
mdam_interval_cmp(MdamContext *ctx, int col,
				  const MdamInterval *ia, const MdamInterval *ib)
{
	int			cmp;

	if (ia->is_null_interval || ib->is_null_interval)
	{
		if (ia->is_null_interval && ib->is_null_interval)
			return 0;
		if (ctx->index->nulls_first[col])
			return ia->is_null_interval ? -1 : 1;
		else
			return ia->is_null_interval ? 1 : -1;
	}

	/* Lower bound */
	if (ia->lo_infinite && !ib->lo_infinite)
		return -1;
	if (!ia->lo_infinite && ib->lo_infinite)
		return 1;
	if (!ia->lo_infinite && !ib->lo_infinite)
	{
		cmp = mdam_compare(ctx, col, ia->lo, ib->lo);
		if (cmp != 0)
			return cmp;
		if (ia->lo_inclusive != ib->lo_inclusive)
			return ia->lo_inclusive ? -1 : 1;
	}

	/* Upper bound */
	if (ia->hi_infinite && !ib->hi_infinite)
		return 1;
	if (!ia->hi_infinite && ib->hi_infinite)
		return -1;
	if (!ia->hi_infinite && !ib->hi_infinite)
	{
		cmp = mdam_compare(ctx, col, ia->hi, ib->hi);
		if (cmp != 0)
			return cmp;
		if (ia->hi_inclusive != ib->hi_inclusive)
			return ia->hi_inclusive ? 1 : -1;
	}

	return 0;
}

/*
 * Compare two MdamSortEntry's by precomputed sort keys.
 */
static int
mdam_path_sort_cmp(const void *a, const void *b, void *arg)
{
	const		MdamSortEntry *ea = (const MdamSortEntry *) a;
	const		MdamSortEntry *eb = (const MdamSortEntry *) b;
	MdamContext *ctx = (MdamContext *) arg;

	for (int col = 0; col < ctx->nkeycolumns; col++)
	{
		MdamInterval *ka = &ea->key[col];
		MdamInterval *kb = &eb->key[col];
		int			cmp = mdam_interval_cmp(ctx, col, ka, kb);

		if (cmp != 0)
			return cmp;
	}
	return 0;
}

/*
 * mdam_expand_sort_coalesce
 *		Step 4: Driver for expand (4a) / sort (4b) / coalesce (4c).
 */
static List *
mdam_expand_sort_coalesce(MdamContext *ctx, List *dnf, List *paths)
{
	paths = mdam_expand_leading(ctx, dnf, paths);
	paths = mdam_sort_by_key_space(ctx, paths);
	return mdam_coalesce_adjacent(ctx, paths);
}

/*
 * mdam_expand_leading
 *		Step 4a: Expand leading IN/range constraints into elementary intervals
 *		so every path has point constraints on its leading columns.
 */
static List *
mdam_expand_leading(MdamContext *ctx, List *dnf, List *paths)
{
	List	   *all_expanded = NIL;
	ListCell   *lc;

	MDAM_LOG("MDAM step 4a: expanding %d paths", list_length(paths));

	foreach(lc, paths)
	{
		List	   *path = (List *) lfirst(lc);
		List	   *current_paths = list_make1(path);

		for (int col_idx = 0; col_idx < ctx->nkeycolumns; col_idx++)
		{
			List	   *new_paths = NIL;
			ListCell   *plc;

			foreach(plc, current_paths)
			{
				List	   *p = (List *) lfirst(plc);
				MdamAtom   *atom = NULL;
				ListCell   *alc;
				int			p_last;
				List	   *col_atom_list;
				List	   *elem_ivs;
				List	   *base;

				p_last = mdam_last_constrained_col(p);

				/*
				 * Past the last constrained column there's nothing to expand.
				 * At the last constrained column itself we normally skip too
				 * -- but a SAOP whose last constrained column is the *leading*
				 * index column must be expanded into individual EQ paths.  A
				 * leading-col SAOP's effective sort interval spans [min, max]
				 * inclusive, which can enclose other paths' range stripes
				 * produced by shattering the leading column.  Leaving the
				 * SAOP fused would have Append emit its endpoint rows
				 * contiguously while the enclosed range stripes run later,
				 * breaking key-space order.  Splitting the SAOP lets sort
				 * interleave each EQ path between the right neighbors.  At
				 * non-leading p_last columns the leading columns are
				 * point-constrained, so SAOP fusion at p_last cannot enclose
				 * any other path's stripe and the split would just
				 * unnecessarily multiply the Append.
				 */
				if (col_idx > p_last)
				{
					new_paths = lappend(new_paths, p);
					continue;
				}
				if (col_idx == p_last)
				{
					List	   *only_list;
					MdamAtom   *only_atom;

					only_list = mdam_atoms_for_col(p, col_idx);
					only_atom = (only_list != NIL) ?
						(MdamAtom *) linitial(only_list) : NULL;

					if (col_idx != 0 ||
						only_atom == NULL ||
						list_length(only_list) != 1 ||
						only_atom->op != MDAM_OP_SAOP)
					{
						new_paths = lappend(new_paths, p);
						continue;
					}
					/* Fall through to expand the leading-col SAOP. */
				}

				/*
				 * Collect ALL atoms for this column (there may be multiple,
				 * e.g. GE + LE forming a range).
				 */
				col_atom_list = mdam_atoms_for_col(p, col_idx);

				atom = NULL;
				if (col_atom_list != NIL)
					atom = (MdamAtom *) linitial(col_atom_list);

				base = mdam_atoms_except_col(p, col_idx);

				if (atom == NULL || atom->op == MDAM_OP_IS_ANYTHING)
				{
					/* Shatter unconstrained column */
					elem_ivs = mdam_elem_intervals_for_col(ctx, col_idx, dnf);

					foreach(alc, elem_ivs)
					{
						MdamAtom   *ei = (MdamAtom *) lfirst(alc);
						List	   *atoms;

						if (ei->op == MDAM_OP_IS_ANYTHING)
						{
							if (list_length(elem_ivs) == 1)
							{
								atoms = mdam_sort_path_atoms(list_copy(base));
								new_paths = lappend(new_paths, atoms);
							}
						}
						else
						{
							MdamAtom   *ei_copy = mdam_copy_atom(ctx, ei);
							List	   *with_ei;

							with_ei = lappend(list_copy(base), ei_copy);
							atoms = mdam_sort_path_atoms(with_ei);
							new_paths = lappend(new_paths, atoms);
						}
					}
				}
				else if (list_length(col_atom_list) == 1 &&
						 atom->op == MDAM_OP_SAOP)
				{
					/* Expand IN into individual EQ paths */
					for (int vi = 0; vi < atom->n_in_values; vi++)
					{
						Datum		v = atom->in_values[vi];
						MdamAtom   *eq_atom;
						List	   *with_eq;
						List	   *sorted;

						eq_atom = mdam_make_atom(ctx, col_idx, MDAM_OP_EQ, v);
						with_eq = lappend(list_copy(base), eq_atom);
						sorted = mdam_sort_path_atoms(with_eq);
						new_paths = lappend(new_paths, sorted);
					}
				}
				else if (list_length(col_atom_list) == 1 &&
						 atom->op == MDAM_OP_EQ)
				{
					/* Single EQ: keep as-is */
					new_paths = lappend(new_paths, p);
				}
				else
				{
					/*
					 * Multi-atom or non-EQ single atom on this column.
					 *
					 * Defensive: if a SAOP appears alongside siblings here
					 * (step 3a should have narrowed it), preserve the path
					 * verbatim.  Routing through extract_interval would
					 * error, since SAOPs have no interval form.
					 */
					bool		shattered = false;

					if (mdam_col_atoms_has_saop(col_atom_list))
					{
						new_paths = lappend(new_paths, p);
						continue;
					}

					/*
					 * Range (possibly multi-atom like GE+LE). Shatter into
					 * elementary intervals, intersecting ALL atoms on this
					 * column with each interval.
					 */
					elem_ivs = mdam_elem_intervals_for_col(ctx, col_idx, dnf);

					foreach(alc, elem_ivs)
					{
						MdamAtom   *ei = (MdamAtom *) lfirst(alc);
						List	   *all_atoms;
						MdamInterval *iv;

						if (ei->op == MDAM_OP_IS_ANYTHING)
							continue;

						/* Intersect ALL column atoms with this interval */
						all_atoms = lappend(list_copy(col_atom_list), ei);
						iv = mdam_extract_interval(ctx, col_idx, all_atoms);
						list_free(all_atoms);

						if (iv != NULL &&
							!(iv->lo_infinite && iv->hi_infinite))
						{
							List	   *iv_atoms;

							iv_atoms = mdam_interval_to_atoms(ctx, col_idx, iv);
							if (iv_atoms != NIL)
							{
								List	   *base_copy = list_copy(base);
								List	   *combined;
								List	   *sorted;

								combined = list_concat(base_copy, iv_atoms);
								sorted = mdam_sort_path_atoms(combined);
								new_paths = lappend(new_paths, sorted);
								shattered = true;
							}
						}
					}

					if (!shattered)
						new_paths = lappend(new_paths, p);
				}
			}

			current_paths = new_paths;
		}

		all_expanded = list_concat(all_expanded, current_paths);
	}

	MDAM_LOG("MDAM step 4a done (pre-dedup): %d expanded paths",
			 list_length(all_expanded));

	return all_expanded;
}

/*
 * mdam_sort_by_key_space
 *		Step 4b: Sort paths by index key space order; dedup sort-equal paths.
 *
 * Precompute sort keys into a separate array, then sort with qsort_arg.
 * The sort positions structurally identical paths into the same contiguous
 * sort-equal group, which lets the dedup pass scan only within each group
 * instead of comparing every pair.  qsort isn't stable, so atom-equal paths
 * can interleave with same-sort-key-but-atom-different paths within a group;
 * tracking the kept paths of the current sort-equal group handles that.
 */
static List *
mdam_sort_by_key_space(MdamContext *ctx, List *paths)
{
	int			np = list_length(paths);
	MdamSortEntry *entries = palloc(sizeof(MdamSortEntry) * np);
	List	   *result = NIL;
	List	   *cur_group_kept = NIL;

	for (int si = 0; si < np; si++)
	{
		entries[si].path = (List *) list_nth(paths, si);
		entries[si].key = mdam_get_path_sort_key(ctx, entries[si].path);
	}

	qsort_arg(entries, np, sizeof(MdamSortEntry),
			  mdam_path_sort_cmp, ctx);

	for (int si = 0; si < np; si++)
	{
		bool		dup = false;
		ListCell   *gl;

		if (si > 0 &&
			mdam_path_sort_cmp(&entries[si - 1], &entries[si], ctx) != 0)
			cur_group_kept = NIL;	/* sort-equal group ended */

		foreach(gl, cur_group_kept)
		{
			if (mdam_base_atoms_equal(ctx, (List *) lfirst(gl),
									  entries[si].path))
			{
				dup = true;
				break;
			}
		}

		if (!dup)
		{
			result = lappend(result, entries[si].path);
			cur_group_kept = lappend(cur_group_kept, entries[si].path);
		}
		pfree(entries[si].key);
	}
	pfree(entries);

	return result;
}

/*
 * mdam_coalesce_adjacent
 *		Step 4c: Coalesce adjacent paths that were needlessly shattered,
 *		followed by a final EQ-to-IN merge pass.
 */
static List *
mdam_coalesce_adjacent(MdamContext *ctx, List *paths)
{
	List	   *all_expanded = paths;
	bool		changed = true;

	while (changed)
	{
		List	   *merged_paths = NIL;
		int			npaths = list_length(all_expanded);
		bool	   *skip;

		changed = false;
		skip = palloc0(sizeof(bool) * npaths);

		for (int i = 0; i < npaths; i++)
		{
			List	   *cur_path;
			bool		did_merge = false;

			if (skip[i])
				continue;

			cur_path = (List *) list_nth(all_expanded, i);
			if (i + 1 >= npaths || skip[i + 1])
			{
				merged_paths = lappend(merged_paths, cur_path);
				continue;
			}

			/* Try each column as merge dimension */
			for (int merge_col = 0; merge_col < ctx->nkeycolumns; merge_col++)
			{
				List	   *cur_base = mdam_path_base(cur_path, merge_col);
				List	   *next_path = (List *) list_nth(all_expanded, i + 1);
				List	   *next_base = mdam_path_base(next_path, merge_col);
				List	   *s_cur;
				List	   *s_next;
				List	   *cur_col_atoms;
				List	   *next_col_atoms;
				MdamInterval *iv1;
				MdamInterval *iv2;
				List	   *merged_ivs;
				List	   *ivs;
				int			null_sibling_idx = -1;
				MdamInterval *miv;
				List	   *new_atoms;
				List	   *new_path;
				bool		conflict = false;
				int			k;
				ListCell   *klc;

				if (!mdam_base_atoms_equal(ctx, cur_base, next_base))
					continue;

				/*
				 * Simplify both paths first: a SAOP that narrows to a single
				 * value becomes an EQ and can participate in coalescing.
				 * Fully contradictory paths drop out.
				 */
				s_cur = mdam_simplify_path(ctx, cur_path);
				s_next = mdam_simplify_path(ctx, next_path);
				if (s_cur == NULL || s_next == NULL)
					continue;
				cur_path = s_cur;
				next_path = s_next;

				cur_col_atoms = mdam_atoms_for_col(cur_path, merge_col);
				next_col_atoms = mdam_atoms_for_col(next_path, merge_col);

				/*
				 * Coalescing routes through extract_interval, which errors on
				 * SAOPs.  Step 3a above should already have narrowed SAOPs to
				 * single EQs where possible; any SAOP that survives doesn't
				 * participate here -- skip the merge and keep both paths.
				 */
				if (mdam_col_atoms_has_saop(cur_col_atoms) ||
					mdam_col_atoms_has_saop(next_col_atoms))
					continue;

				iv1 = mdam_extract_interval(ctx, merge_col, cur_col_atoms);
				iv2 = mdam_extract_interval(ctx, merge_col, next_col_atoms);
				if (!iv1 || !iv2)
					continue;

				ivs = list_make2(iv1, iv2);
				merged_ivs = mdam_merge_interval_list(ctx, merge_col, ivs);

				/*
				 * If iv1+iv2 together span (-inf, +inf) but exclude NULL --
				 * either as two disjoint value intervals or collapsed to a
				 * single unbounded IS NOT NULL interval -- look for a
				 * sibling path with IS NULL on merge_col and same base on
				 * the other columns.  Such a sibling indicates the original
				 * predicate already includes NULL rows on merge_col; we can
				 * then safely consolidate iv1+iv2 to a plain unconstrained
				 * (-inf, +inf) and consume the IS NULL sibling too,
				 * yielding a single coalesced path.
				 */
				if (list_length(merged_ivs) == 2 ||
					(list_length(merged_ivs) == 1 &&
					 ((MdamInterval *) linitial(merged_ivs))->is_not_null_interval &&
					 ((MdamInterval *) linitial(merged_ivs))->lo_infinite &&
					 ((MdamInterval *) linitial(merged_ivs))->hi_infinite))
				{
					k = 0;
					foreach(klc, all_expanded)
					{
						List	   *kpath;
						List	   *kbase;
						List	   *k_col_atoms;
						MdamAtom   *ka;
						MdamInterval *k_iv;
						List	   *retry;

						if (k == i || k == i + 1 || skip[k])
						{
							k++;
							continue;
						}
						kpath = (List *) lfirst(klc);
						kbase = mdam_path_base(kpath, merge_col);
						if (!mdam_base_atoms_equal(ctx, cur_base, kbase))
						{
							k++;
							continue;
						}
						k_col_atoms = mdam_atoms_for_col(kpath, merge_col);
						if (list_length(k_col_atoms) != 1)
						{
							k++;
							continue;
						}
						ka = (MdamAtom *) linitial(k_col_atoms);
						if (ka->op != MDAM_OP_IS_NULL)
						{
							k++;
							continue;
						}
						k_iv = mdam_extract_interval(ctx, merge_col, k_col_atoms);
						if (k_iv == NULL)
						{
							k++;
							continue;
						}

						/*
						 * mdam_merge_interval_list mutates its inputs in
						 * place, so the prior call above on [iv1, iv2] may
						 * have widened iv1 (e.g. to an IS NOT NULL
						 * (-inf, +inf)).  Re-extract from the original atom
						 * lists so the retry sees the unmutated inputs.
						 */
						iv1 = mdam_extract_interval(ctx, merge_col, cur_col_atoms);
						iv2 = mdam_extract_interval(ctx, merge_col, next_col_atoms);
						retry = list_make3(iv1, iv2, k_iv);
						merged_ivs = mdam_merge_interval_list(ctx, merge_col,
															  retry);
						if (list_length(merged_ivs) == 1)
						{
							null_sibling_idx = k;
							break;
						}
						/* Retry didn't help; re-extract and recompute. */
						iv1 = mdam_extract_interval(ctx, merge_col, cur_col_atoms);
						iv2 = mdam_extract_interval(ctx, merge_col, next_col_atoms);
						ivs = list_make2(iv1, iv2);
						merged_ivs = mdam_merge_interval_list(ctx, merge_col,
															  ivs);
						k++;
					}
				}

				if (list_length(merged_ivs) != 1)
					continue;

				miv = (MdamInterval *) linitial(merged_ivs);

				/*
				 * Safety: the merged interval on merge_col widens cur's
				 * stripe.  If any *other* path has a different base on
				 * merge_col, sits within the same prefix region (cols
				 * 0..merge_col-1 overlap cur's prefix), and has a merge_col
				 * stripe that overlaps the merged range, then Append
				 * concatenation of {merged, that other} would rewind
				 * merge_col -- the merged path emits its full stripe before
				 * the other even starts, but the other's merge_col values
				 * fall within the merged path's range.  Refuse the merge and
				 * keep the two paths separate; the already-shattered
				 * structure from step 4a is the correct shape.
				 *
				 * The prefix check matters: a path with a disjoint
				 * leading-column region can't cause a rewind no matter how
				 * its merge_col stripe sits, because Append already places it
				 * in a different leading-col band.
				 */
				k = 0;
				foreach(klc, all_expanded)
				{
					List	   *kpath;
					List	   *kbase;
					List	   *k_col_atoms;
					MdamInterval *k_iv;
					bool		prefix_compat = true;

					if (k == i || k == i + 1)
					{
						k++;
						continue;
					}
					kpath = (List *) lfirst(klc);

					/*
					 * Prefix check on cols 0..merge_col-1: each pair must
					 * overlap (or one side unconstrained), otherwise k is in
					 * a different region of the key space and can't rewind.
					 */
					for (int c = 0; c < merge_col; c++)
					{
						List	   *cur_c_atoms = mdam_atoms_for_col(cur_path, c);
						List	   *k_c_atoms = mdam_atoms_for_col(kpath, c);
						MdamInterval *cur_c_iv;
						MdamInterval *k_c_iv;

						if (cur_c_atoms == NIL || k_c_atoms == NIL)
							continue;

						/*
						 * SAOPs have no interval form; assume the prefix pair
						 * overlaps so the safety check stays conservative
						 * (refusing more merges, never fewer).
						 */
						if (mdam_col_atoms_has_saop(cur_c_atoms) ||
							mdam_col_atoms_has_saop(k_c_atoms))
							continue;
						cur_c_iv = mdam_extract_interval(ctx, c, cur_c_atoms);
						k_c_iv = mdam_extract_interval(ctx, c, k_c_atoms);
						if (cur_c_iv == NULL || k_c_iv == NULL)
							continue;
						if (!mdam_intervals_overlap(ctx, c, cur_c_iv, k_c_iv))
						{
							prefix_compat = false;
							break;
						}
					}
					if (!prefix_compat)
					{
						k++;
						continue;
					}

					kbase = mdam_path_base(kpath, merge_col);
					if (mdam_base_atoms_equal(ctx, cur_base, kbase))
					{
						k++;
						continue;
					}
					k_col_atoms = mdam_atoms_for_col(kpath, merge_col);

					/*
					 * SAOP on kpath's merge_col: assume the discrete set
					 * could overlap miv's range.  Refuses the merge -- safer
					 * than wrong, since the merged interval would widen cur's
					 * stripe to potentially cover SAOP values.
					 */
					if (mdam_col_atoms_has_saop(k_col_atoms))
					{
						conflict = true;
						break;
					}
					k_iv = mdam_extract_interval(ctx, merge_col, k_col_atoms);
					if (k_iv == NULL)
					{
						if (k_col_atoms == NIL)
						{
							conflict = true;
							break;
						}
						k++;
						continue;
					}
					if (mdam_intervals_overlap(ctx, merge_col, miv, k_iv))
					{
						conflict = true;
						break;
					}
					k++;
				}

				if (conflict)
					continue;

				new_atoms = mdam_interval_to_atoms(ctx, merge_col, miv);
				new_path = list_concat_copy(cur_base, new_atoms);
				merged_paths = lappend(merged_paths,
									   mdam_sort_path_atoms(new_path));
				skip[i + 1] = true; /* consumed */
				if (null_sibling_idx >= 0)
					skip[null_sibling_idx] = true;
				did_merge = true;
				changed = true;
				break;
			}

			if (!did_merge)
				merged_paths = lappend(merged_paths, cur_path);
		}

		pfree(skip);
		all_expanded = merged_paths;
	}

	/* Final EQ-to-IN pass (adjacent only) */
	changed = true;
	while (changed)
	{
		List	   *prev = all_expanded;

		for (int c = 0; c < ctx->nkeycolumns; c++)
		{
			all_expanded = mdam_merge_eq_to_in(ctx, c, all_expanded, true);
		}

		changed = (list_length(all_expanded) != list_length(prev));
	}

	return all_expanded;
}


/* ---------------------------------------------------
 * Ordering conflict detection
 * ---------------------------------------------------
 */

/*
 * mdam_path_is_contradictory
 *		True if any indexed column in PATH has multiple atoms that don't
 *		combine into a valid interval (e.g. col=4 AND col=5).  Step 3a's
 *		simplification pass normally prunes such paths upstream; this
 *		function is the local backstop for the ordering-conflict check
 *		so a path that emits zero rows doesn't influence Append ordering
 *		analysis.
 */
static bool
mdam_path_is_contradictory(MdamContext *ctx, List *path)
{
	for (int col = 0; col < ctx->nkeycolumns; col++)
	{
		List	   *col_atoms = mdam_atoms_for_col(path, col);
		MdamInterval *iv;

		if (col_atoms == NIL)
			continue;

		/*
		 * Skip SAOP-bearing columns -- full contradiction detection lives in
		 * mdam_simplify_path, which callers run first when
		 * partial-contradiction simplification matters.
		 */
		if (mdam_col_atoms_has_saop(col_atoms))
			continue;
		iv = mdam_extract_interval(ctx, col, col_atoms);
		if (iv == NULL)
			return true;
	}
	return false;
}

/*
 * mdam_simplify_path
 *		Filter each column's SAOPs against sibling atoms, drop atoms that
 *		the filtered SAOP makes redundant, and report full contradiction
 *		(NULL return).
 *
 * For a column carrying a SAOP plus EQ/range/IS NOT NULL siblings, the
 * surviving SAOP values are exactly those that satisfy every sibling.
 * Once narrowed, the siblings are implied by the surviving values and
 * collapse out, leaving just the (possibly-shrunk) SAOP -- or an EQ when
 * one value remains.
 *
 * IS NULL on the same column as a SAOP is a contradiction: SAOP values
 * are non-NULL by definition of the equality operator.
 *
 * Non-SAOP columns are passed through verbatim after an interval-based
 * contradiction check.
 */
static List *
mdam_simplify_path(MdamContext *ctx, List *path)
{
	List	   *new_path = NIL;

	for (int col = 0; col < ctx->nkeycolumns; col++)
	{
		List	   *col_atoms = mdam_atoms_for_col(path, col);
		List	   *non_saop;
		List	   *saops;
		MdamInterval *non_saop_iv;
		MdamAtom   *first_saop;
		Datum	   *survivors;
		int			n_survivors;
		bool		has_is_null;
		ListCell   *lc;

		if (col_atoms == NIL)
			continue;

		if (!mdam_col_atoms_has_saop(col_atoms))
		{
			MdamInterval *iv = mdam_extract_interval(ctx, col, col_atoms);

			if (iv == NULL)
				return NULL;
			new_path = list_concat(new_path, list_copy(col_atoms));
			continue;
		}

		non_saop = NIL;
		saops = NIL;
		has_is_null = false;

		foreach(lc, col_atoms)
		{
			MdamAtom   *a = (MdamAtom *) lfirst(lc);

			if (a->op == MDAM_OP_SAOP)
				saops = lappend(saops, a);
			else if (a->op == MDAM_OP_IS_NULL)
				has_is_null = true;
			else
				non_saop = lappend(non_saop, a);
		}

		if (has_is_null)
			return NULL;

		non_saop_iv = NULL;
		if (non_saop != NIL)
		{
			non_saop_iv = mdam_extract_interval(ctx, col, non_saop);
			if (non_saop_iv == NULL)
				return NULL;
		}

		first_saop = (MdamAtom *) linitial(saops);
		survivors = palloc(sizeof(Datum) * first_saop->n_in_values);
		n_survivors = 0;

		for (int j = 0; j < first_saop->n_in_values; j++)
		{
			Datum		v = first_saop->in_values[j];
			ListCell   *lc2;
			bool		ok = true;

			if (non_saop_iv != NULL &&
				!mdam_point_in_interval(ctx, col, v, non_saop_iv))
				continue;

			foreach(lc2, saops)
			{
				MdamAtom   *s = (MdamAtom *) lfirst(lc2);
				bool		found;

				if (s == first_saop)
					continue;
				found = false;
				for (int k = 0; k < s->n_in_values; k++)
				{
					if (mdam_datum_eq(ctx, col, v, s->in_values[k]))
					{
						found = true;
						break;
					}
				}
				if (!found)
				{
					ok = false;
					break;
				}
			}
			if (ok)
				survivors[n_survivors++] = v;
		}

		if (n_survivors == 0)
		{
			pfree(survivors);
			return NULL;
		}

		sort_datums_canonical(ctx, col, survivors, n_survivors);

		if (n_survivors == 1)
		{
			MdamAtom   *eq = mdam_make_atom(ctx, col, MDAM_OP_EQ,
											survivors[0]);

			new_path = lappend(new_path, eq);
		}
		else
		{
			MdamAtom   *saop = mdam_make_atom_saop(ctx, col, survivors,
												   n_survivors);

			new_path = lappend(new_path, saop);
		}

		pfree(survivors);
	}

	return mdam_sort_path_atoms(new_path);
}

/*
 * mdam_detect_ordering_conflict
 *		Check if the sorted retrieval paths would produce rows out of
 *		index key space order when executed as UNION ALL (Append).
 *
 * Returns true if there's an ordering conflict (paths cannot be used).
 */
static bool
mdam_detect_ordering_conflict(MdamContext *ctx, List *paths)
{
	List	   *live_paths = NIL;
	int			npaths;
	MdamInterval **sort_keys;
	ListCell   *lc;

	/*
	 * Filter out contradictory paths -- they emit zero rows, so they don't
	 * influence Append ordering.  The remaining "live" paths are what
	 * actually appears in the output stream.
	 */
	foreach(lc, paths)
	{
		List	   *p = (List *) lfirst(lc);

		if (!mdam_path_is_contradictory(ctx, p))
			live_paths = lappend(live_paths, p);
	}

	npaths = list_length(live_paths);
	if (npaths <= 1)
		return false;
	paths = live_paths;

	sort_keys = palloc(sizeof(MdamInterval *) * npaths);
	for (int ip = 0; ip < npaths; ip++)
	{
		sort_keys[ip] =
			mdam_get_path_sort_key(ctx, (List *) list_nth(paths, ip));
	}

	for (int i = 0; i < npaths - 1; i++)
	{
		MdamInterval *ka = sort_keys[i];
		MdamInterval *kb = sort_keys[i + 1];
		int			first_diff = -1;
		bool		conflict = false;
		bool		settled = false;

		/* Find first column where the sort keys differ */
		for (int col = 0; col < ctx->nkeycolumns; col++)
		{
			bool		same = true;

			if (ka[col].is_null_interval != kb[col].is_null_interval)
				same = false;
			else if (ka[col].is_null_interval)
				;				/* both IS NULL on this column: equal */
			else if (ka[col].lo_infinite != kb[col].lo_infinite)
				same = false;
			else if (!ka[col].lo_infinite &&
					 mdam_compare(ctx, col, ka[col].lo, kb[col].lo) != 0)
				same = false;
			else if (ka[col].lo_inclusive != kb[col].lo_inclusive)
				same = false;
			else if (ka[col].hi_infinite != kb[col].hi_infinite)
				same = false;
			else if (!ka[col].hi_infinite &&
					 mdam_compare(ctx, col, ka[col].hi, kb[col].hi) != 0)
				same = false;
			else if (ka[col].hi_inclusive != kb[col].hi_inclusive)
				same = false;

			if (!same)
			{
				first_diff = col;
				break;
			}
		}

		if (first_diff < 0)
			continue;

		/*
		 * Columns before first_diff must be equal points in both paths. If
		 * any is a range, the divergence at first_diff can occur at different
		 * earlier-column values in different rows, so Append concatenation
		 * can't preserve key-space order.
		 */
		for (int col = 0; col < first_diff; col++)
		{
			bool		is_point;

			/*
			 * IS NULL is a discrete point (the NULL value).  Both paths
			 * agreeing on IS NULL counts as a point match on this column.
			 */
			if (ka[col].is_null_interval)
				is_point = true;
			else
				is_point = (!ka[col].lo_infinite && !ka[col].hi_infinite &&
							ka[col].lo_inclusive && ka[col].hi_inclusive &&
							mdam_compare(ctx, col, ka[col].lo,
										 ka[col].hi) == 0);

			if (!is_point)
			{
				conflict = true;
				break;
			}
		}

		if (conflict)
		{
			for (int j = 0; j < npaths; j++)
			{
				pfree(sort_keys[j]);
			}
			pfree(sort_keys);
			return true;
		}

		/*
		 * Starting at first_diff, A's interval must not strictly extend past
		 * B's: Append emits all of A before any of B, so if A's upper bound
		 * on this column reaches into B's lower bound, the boundary "rewinds"
		 * the column.  A single-point touch (A.hi == B.lo with both
		 * inclusive) is allowed *only if* the next column also has A's
		 * coverage ending where B's begins, recursively -- otherwise both
		 * paths span the same value of the touch column across some range of
		 * the next column, and Append still rewinds.  We descend one column
		 * at a time until we either find clean separation, an actual overlap,
		 * or exhaust columns.
		 */
		for (int col = first_diff; col < ctx->nkeycolumns && !settled; col++)
		{
			/*
			 * IS NULL is disjoint from any value-based interval on the same
			 * column.  When exactly one of A/B is IS NULL here, the column
			 * space usually cleanly separates A and B (NULL goes at
			 * nulls_first[col]'s end; the sort comparator has already placed
			 * them in storage order).  When both are IS NULL on this column
			 * they're tied -- descend to the next column to find a real
			 * differentiator.
			 *
			 * Exception: a path with no constraint at all on this column
			 * (lo_infinite && hi_infinite && !is_not_null_interval) admits
			 * NULL values too -- it scans the entire column including the
			 * NULL partition.  Pairing such a path with an IS NULL sibling
			 * means both paths emit rows from the NULL partition, and Append
			 * can't interleave them in key-space order: the unconstrained
			 * path emits its NULLs in one contiguous block (at one end of its
			 * scan), the IS NULL path emits another contiguous block, and
			 * they end up adjacent in the Append output without being
			 * merge-sorted by the later index columns.
			 */
			if (ka[col].is_null_interval || kb[col].is_null_interval)
			{
				const		MdamInterval *other;

				if (ka[col].is_null_interval && kb[col].is_null_interval)
					continue;

				other = ka[col].is_null_interval ? &kb[col] : &ka[col];
				if (other->lo_infinite && other->hi_infinite &&
					!other->is_not_null_interval)
				{
					conflict = true;
					settled = true;
					continue;
				}

				/* clean separation on this column */
				settled = true;
				continue;
			}

			if (ka[col].hi_infinite)
			{
				conflict = true;
				settled = true;
			}
			else if (kb[col].lo_infinite)
			{
				conflict = true;
				settled = true;
			}
			else
			{
				Datum		a_hi = ka[col].hi;
				Datum		b_lo = kb[col].lo;
				int			cmp = mdam_compare(ctx, col, a_hi, b_lo);

				if (cmp > 0)
				{
					conflict = true;
					settled = true;
				}
				else if (cmp < 0)
				{
					/* clean separation on this column */
					settled = true;
				}
				else
				{
					/* cmp == 0 */
					if (!ka[col].hi_inclusive || !kb[col].lo_inclusive)
					{
						/* not both inclusive: no actual overlap */
						settled = true;
					}
					/* both inclusive: touch -- descend to next col */
				}
			}
		}

		/*
		 * If we exited the loop without settling, every column was a
		 * both-inclusive touch -- A and B agree at a single point across
		 * every key column, which means they're effectively identical sort
		 * keys.  Sort would have been free to put them in either order; treat
		 * as no conflict.
		 */
		if (conflict)
		{
			for (int j = 0; j < npaths; j++)
			{
				pfree(sort_keys[j]);
			}
			pfree(sort_keys);
			return true;
		}
	}

	for (int ip = 0; ip < npaths; ip++)
	{
		pfree(sort_keys[ip]);
	}
	pfree(sort_keys);
	return false;
}


/* ---------------------------------------------------
 * Build paths from MDAM retrievals
 * ---------------------------------------------------
 */

/*
 * Build an OpExpr of the form: indexvar <strategy_op> Const(val).
 */
static Expr *
mdam_make_indexop(MdamColContext *cc, Var *indexvar, int16 strategy, Datum val)
{
	Const	   *constval = makeConst(cc->typid, -1, cc->collid, cc->typlen,
									 val, false, cc->typbyval);
	Oid			opno = (strategy == BTEqualStrategyNumber)
		? cc->eq_opr
		: get_opfamily_member(cc->opfamily, cc->typid, cc->typid, strategy);

	return make_opclause(opno, BOOLOID, false,
						 (Expr *) indexvar, (Expr *) constval,
						 InvalidOid, cc->collid);
}

/*
 * Convert an MdamAtom to an expression suitable for use as an index qual.
 * Returns an OpExpr, ScalarArrayOpExpr, BoolExpr (for RANGE_EXCL), or
 * NullTest (for IS_NULL).  Returns NULL for IS_ANYTHING.
 */
static Expr *
mdam_atom_to_expr(MdamContext *ctx, MdamAtom *atom)
{
	IndexOptInfo *index = ctx->index;
	MdamColContext *cc = &ctx->col_ctx[atom->colno];
	Var		   *indexvar;

	/* btree strategy number for each comparison-style op */
	static const int16 strat_for_op[] = {
		[MDAM_OP_EQ] = BTEqualStrategyNumber,
		[MDAM_OP_LT] = BTLessStrategyNumber,
		[MDAM_OP_LE] = BTLessEqualStrategyNumber,
		[MDAM_OP_GT] = BTGreaterStrategyNumber,
		[MDAM_OP_GE] = BTGreaterEqualStrategyNumber,
	};

	indexvar = makeVar(index->rel->relid,
					   index->indexkeys[atom->colno],
					   cc->typid, -1, cc->collid, 0);

	switch (atom->op)
	{
		case MDAM_OP_EQ:
		case MDAM_OP_LT:
		case MDAM_OP_LE:
		case MDAM_OP_GT:
		case MDAM_OP_GE:
			return mdam_make_indexop(cc, indexvar, strat_for_op[atom->op],
									 atom->value);

		case MDAM_OP_SAOP:
			{
				/* indexvar = ANY(ARRAY[v1, v2, ...]) */
				Oid			arraytype = get_array_type(cc->typid);
				int16		elemlen;
				bool		elembyval;
				char		elemalign;
				ArrayType  *arrayVal;
				Const	   *arrayConst;
				ScalarArrayOpExpr *saop;

				if (!OidIsValid(arraytype))
					return NULL;

				get_typlenbyvalalign(cc->typid, &elemlen, &elembyval,
									 &elemalign);
				arrayVal = construct_array(atom->in_values,
										   atom->n_in_values,
										   cc->typid, elemlen, elembyval,
										   elemalign);
				arrayConst = makeConst(arraytype, -1, cc->collid, -1,
									   PointerGetDatum(arrayVal),
									   false, false);

				saop = makeNode(ScalarArrayOpExpr);
				saop->opno = cc->eq_opr;
				saop->opfuncid = get_opcode(cc->eq_opr);
				saop->hashfuncid = InvalidOid;
				saop->negfuncid = InvalidOid;
				saop->useOr = true;
				saop->inputcollid = cc->collid;
				saop->args = list_make2(indexvar, arrayConst);
				saop->location = -1;
				return (Expr *) saop;
			}

		case MDAM_OP_RANGE_EXCL:
			{
				/* indexvar > range_lo AND indexvar < range_hi */
				Expr	   *gt;
				Expr	   *lt;

				gt = mdam_make_indexop(cc, indexvar, BTGreaterStrategyNumber,
									   atom->range_lo);
				lt = mdam_make_indexop(cc, indexvar, BTLessStrategyNumber,
									   atom->range_hi);
				return makeBoolExpr(AND_EXPR, list_make2(gt, lt), -1);
			}

		case MDAM_OP_IS_NULL:
			{
				NullTest   *nt = makeNode(NullTest);

				nt->arg = (Expr *) indexvar;
				nt->nulltesttype = IS_NULL;
				nt->argisrow = false;
				nt->location = -1;
				return (Expr *) nt;
			}

		case MDAM_OP_IS_NOT_NULL:
			{
				NullTest   *nt = makeNode(NullTest);

				nt->arg = (Expr *) indexvar;
				nt->nulltesttype = IS_NOT_NULL;
				nt->argisrow = false;
				nt->location = -1;
				return (Expr *) nt;
			}

		case MDAM_OP_IS_ANYTHING:
			return NULL;
	}

	return NULL;
}

/*
 * Build IndexClause nodes from a retrieval path's atoms and create an
 * IndexPath for the given index.
 *
 * or_rinfos: list of original OR RestrictInfo nodes that this retrieval
 * covers.  The first IndexClause's rinfo is set to the first OR
 * RestrictInfo so that is_redundant_with_indexclauses() recognizes the
 * original OR clause as handled, preventing it from appearing as a
 * redundant Filter qual.
 */
static IndexPath *
mdam_build_index_path(MdamContext *ctx, List *retrieval_atoms,
					  List *or_rinfos, ScanDirection scandir)
{
	IndexOptInfo *index = ctx->index;
	PlannerInfo *root = ctx->root;
	RelOptInfo *rel = ctx->rel;
	List	   *index_clauses = NIL;
	List	   *pathkeys;
	IndexPath  *ipath;
	bool		index_only_scan;
	bool		first_clause = true;

	for (int colno = 0; colno < ctx->nkeycolumns; colno++)
	{
		ListCell   *lc;

		foreach(lc, retrieval_atoms)
		{
			MdamAtom   *atom = (MdamAtom *) lfirst(lc);
			Expr	   *expr;
			IndexClause *iclause;

			if (atom->colno != colno)
				continue;
			if (atom->op == MDAM_OP_IS_ANYTHING)
				continue;

			expr = mdam_atom_to_expr(ctx, atom);
			if (expr == NULL)
				continue;

			if (atom->op == MDAM_OP_RANGE_EXCL)
			{
				/*
				 * RANGE_EXCL generates an AND of GT and LT.  We need to
				 * create two IndexClauses, one for each bound.
				 */
				BoolExpr   *boolexpr = (BoolExpr *) expr;
				ListCell   *elc;

				foreach(elc, boolexpr->args)
				{
					Expr	   *sub = (Expr *) lfirst(elc);
					RestrictInfo *rinfo = make_simple_restrictinfo(root, sub);

					iclause = makeNode(IndexClause);
					if (first_clause && or_rinfos != NIL)
					{
						iclause->rinfo = linitial_node(RestrictInfo, or_rinfos);
						first_clause = false;
					}
					else
						iclause->rinfo = rinfo;
					iclause->indexquals = list_make1(rinfo);
					iclause->lossy = false;
					iclause->indexcol = colno;
					iclause->indexcols = NIL;
					index_clauses = lappend(index_clauses, iclause);
				}
			}
			else
			{
				RestrictInfo *rinfo = make_simple_restrictinfo(root, expr);

				iclause = makeNode(IndexClause);
				if (first_clause && or_rinfos != NIL)
				{
					iclause->rinfo = linitial_node(RestrictInfo, or_rinfos);
					first_clause = false;
				}
				else
					iclause->rinfo = rinfo;
				iclause->indexquals = list_make1(rinfo);
				iclause->lossy = false;
				iclause->indexcol = colno;
				iclause->indexcols = NIL;
				index_clauses = lappend(index_clauses, iclause);
			}
		}
	}

	if (index_clauses == NIL)
		return NULL;

	/* Compute pathkeys for the index scan */
	pathkeys = build_index_pathkeys(root, index, scandir);

	/* Check if index-only scan is possible */
	index_only_scan = check_index_only(rel, index);

	ipath = create_index_path(root, index,
							  index_clauses,
							  NIL,	/* no ORDER BY */
							  NIL,
							  pathkeys,
							  scandir,
							  index_only_scan,
							  NULL,	/* no outer relids */
							  1.0,	/* loop_count */
							  false);	/* not partial */

	/*
	 * Override the row estimate computed by cost_index().  cost_index() sets
	 * path->rows to baserel->rows -- the post-restriction row count for the
	 * whole rel -- which is correct for a lone IndexPath but wrong here:
	 * each retrieval handles only a slice of the OR clause, and the Append
	 * we sit under will sum these.  Use the index AM's per-retrieval
	 * selectivity, which cost_index() already computed (and used internally
	 * for I/O and per-tuple CPU cost) but never wrote back to path->rows.
	 */
	ipath->path.rows = clamp_row_est(ipath->indexselectivity * rel->tuples);

	return ipath;
}

/*
 * mdam_add_paths
 *		Add the MDAM IndexPaths for the given retrievals to the relation,
 *		all scanning in the given direction.
 *
 * A single retrieval (the MDAM algorithm merged overlapping OR arms into
 * one tighter range) is emitted as a plain IndexPath.  Multiple retrievals
 * are wrapped in an Append in index key space order.
 */
static void
mdam_add_paths(MdamContext *ctx, List *retrievals, List *or_rinfos,
			   ScanDirection scandir)
{
	PlannerInfo *root = ctx->root;
	RelOptInfo *rel = ctx->rel;
	List	   *subpaths = NIL;
	int			nret = list_length(retrievals);

	/*
	 * Build each per-retrieval IndexPath.  For backward scans iterate the
	 * retrievals in reverse so that the resulting stream is in descending
	 * index key space order.
	 */
	for (int j = 0; j < nret; j++)
	{
		int			i = ScanDirectionIsBackward(scandir) ? (nret - 1 - j) : j;
		List	   *retrieval = (List *) list_nth(retrievals, i);
		IndexPath  *ipath;

		ipath = mdam_build_index_path(ctx, retrieval, or_rinfos, scandir);

		if (ipath == NULL)
			return;

		subpaths = lappend(subpaths, ipath);
	}

	if (subpaths == NIL)
		return;

	if (nret == 1)
	{
		add_path(rel, (Path *) linitial(subpaths));
	}
	else
	{
		AppendPath *appendpath;
		AppendPathInput input;
		List	   *pathkeys;

		/*
		 * Each IndexPath preserves the same pathkeys (same index, same
		 * direction), and the retrievals are sorted in key space order, so
		 * the Append preserves index ordering.
		 */
		pathkeys = build_index_pathkeys(root, ctx->index, scandir);

		memset(&input, 0, sizeof(input));
		input.subpaths = subpaths;
		input.partial_subpaths = NIL;
		input.child_append_relid_sets = NIL;

		appendpath = create_append_path(root, rel, input,
										pathkeys,
										NULL,	/* required_outer */
										0,	/* parallel_workers */
										false,	/* parallel_aware */
										-1);	/* rows: computed */
		appendpath->is_mdam = true;

		add_path(rel, (Path *) appendpath);
	}
}
