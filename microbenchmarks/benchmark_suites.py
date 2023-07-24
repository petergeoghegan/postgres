"""Data-driven benchmark suite registry.

Every benchmark suite is described by two files in ``suites/``:

* ``<order>-<name>.toml`` -- suite metadata plus the timed queries.
* ``<order>-<name>.sql``  -- the data-loading DDL (one script per suite),
  referenced from the TOML via ``setup_sql_file``.

This module loads them into ``BENCHMARK_SUITES``, the single source of truth
consumed by ``prefetch_benchmark.py`` (runner + ``--list-modes``),
``patch_report.py`` (suite discovery) and ``perf_flamegraph.py``.  The default
suite is defined exactly like every other suite -- it is just the one that sets
``default = true`` and carries no ``cli_flag``.

To add a suite: drop a ``<order>-<name>.toml`` + matching ``.sql`` into
``suites/``.  No code change is required.

TOML schema (per file)::

    [suite]
    mode_prefix     = "example"        # required; unique short id
    order           = 35               # required; sort key (BENCHMARK_SUITES order)
    cli_flag        = "--example"      # optional; omit for the default suite
    cli_dest        = "example_tests"  # optional; argparse dest
    help            = "..."            # optional; argparse help
    title           = "..."           # required; human-readable title
    tables          = ["t_example"]    # required; tables the suite owns
    setup_sql_file  = "35-example.sql" # required; sidecar DDL (beside the .toml)
    row_description = "10M rows"       # required; progress-message size hint
    uncached_runs   = 3               # optional; default 3
    cached_runs     = 40              # optional; default 40
    sync_stats      = false           # optional; default false (copy optimizer stats)
    all_visible     = true            # optional; default true.  false => the suite's
                                      #   tables are intentionally not all-visible
                                      #   (e.g. ios_fetch's LP_DEAD poison), which both
                                      #   skips the all-visible verify check and the
                                      #   post-load VACUUM (FREEZE).
    default         = true            # optional; the ONE default suite sets this and
                                      #   omits cli_flag/cli_dest/help (it is the
                                      #   top-level 00-*.toml, run when no flag is given)

    [queries.<QID>]                   # one table per timed query, in run order
    name            = "..."           # required; description
    sql             = '''SELECT ...'''        # required; the timed statement
    evict           = ["t_example"]           # required; relations to evict pre-run
    prewarm_indexes = ["t_example_idx"]       # required
    prewarm_tables  = ["t_example"]           # required
    gucs            = { enable_seqscan = "off" }   # optional; per-query GUC overrides
    index_only      = true            # optional; pure index-only scan (skip heap evict)
    setup_table     = "..."           # optional; carried verbatim (currently unused)

index_only marks a *pure* index-only scan: the plan has NO heap-reading scan node
(no plain Index Scan, Seq Scan or Bitmap Heap Scan -- only Index Only Scan) and
Heap Fetches: 0, so the query never touches the heap.  run_query() then skips heap
eviction and the OS-cache drop for it -- that work is wasted (the heap is untouched)
and skipping it makes the query behave identically cached and uncached.  This only
speeds up setup; the timed query is unchanged (the index is in shared_buffers either
way).  Two ways a query is NOT pure index-only and must NOT set index_only:
  - a plain Index Scan anywhere in the plan (e.g. the inner side of a lateral join)
    -- it reads the heap even though an Index Only Scan sits elsewhere;
  - an Index Only Scan with Heap Fetches > 0 -- the table is not all-visible, so it
    still visits the heap.  The ios_fetch suite (IF*) is exactly this: its index-only
    scans run over a deliberately not-all-visible table (all_visible = false) and
    expect heap fetches, so they are correctly excluded.
"""
import re
import sys
import time
import tomllib
from collections import OrderedDict
from pathlib import Path

import psycopg

SUITES_DIR = Path(__file__).resolve().parent / "suites"

# Optional [suite] keys and the defaults the loader applies when they are absent.
_SUITE_DEFAULTS = {
    "default": False,
    "cli_flag": None,
    "cli_dest": None,
    "help": None,
    "uncached_runs": 3,
    "cached_runs": 40,
    "sync_stats": False,
    "all_visible": True,
}


# ── data verification / loading helpers ────────────────────────────────────
def _verify_tables(conn_details, tables, suite_name, check_all_visible=True):
    """Verify that tables exist, have data, and (optionally) are fully all-visible."""
    try:
        conn = psycopg.connect(**conn_details)
        conn.autocommit = True
        if check_all_visible:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_visibility")
        for table in tables:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'r'
                    )
                """, (table,))
                if not cur.fetchone()[0]:
                    print(f"Table {table} does not exist")
                    conn.close()
                    return False
                cur.execute(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)")
                if not cur.fetchone()[0]:
                    print(f"Table {table} exists but has 0 rows")
                    conn.close()
                    return False
                if check_all_visible:
                    av_sql = """
                        SELECT c.relpages,
                               (SELECT all_visible
                                FROM pg_visibility_map_summary(%s::regclass))
                        FROM pg_class c
                        WHERE c.relname = %s AND c.relkind = 'r'
                    """
                    cur.execute(av_sql, (table, table))
                    row = cur.fetchone()
                    if row and row[0] != row[1]:
                        relpages, all_visible = row
                        # A load can leave some pages not-all-visible (e.g. a
                        # snapshot briefly held during its VACUUM FREEZE).  A plain
                        # VACUUM FREEZE repairs it in well under a second, so heal it
                        # here rather than dead-ending -- the harness would otherwise
                        # exit before it could run the very VACUUM this used to
                        # advise.  Only error if it STILL can't mark every page,
                        # which points at a real cause (e.g. a long-running
                        # transaction holding back OldestXmin).
                        print(f"Table {table}: {all_visible}/{relpages} pages all-visible; "
                              f"running VACUUM (FREEZE) to repair...")
                        cur.execute(f"VACUUM (FREEZE) {table}")
                        cur.execute(av_sql, (table, table))
                        relpages, all_visible = cur.fetchone()
                        if relpages != all_visible:
                            conn.close()
                            sys.exit(f"ERROR: Table {table}: still only {all_visible}/{relpages} "
                                     f"pages all-visible after VACUUM (FREEZE) (a long-running "
                                     f"transaction may be holding back OldestXmin). "
                                     f"Reload the data.")
        conn.close()
        if check_all_visible:
            print("All tables exist, have data, and are all-visible ✓")
        else:
            print("All tables exist and have data ✓")
        return True
    except Exception as e:
        print(f"Error verifying {suite_name} data: {e}")
        return False


def ensure_all_visible_after_load(conn_details, tables):
    """After loading, verify all pages are all-visible; VACUUM (FREEZE) if not.

    Disconnects, waits one second, reconnects, then checks the visibility map
    for each table.  Any table not fully all-visible is VACUUM (FREEZE)d
    individually.  Crucially this only vacuums *these* tables, never the whole
    database: all suites share one database, and an unqualified VACUUM FREEZE
    would also vacuum other suites' tables -- e.g. removing the LP_DEAD poison
    the ios_fetch suite deliberately keeps to force heap fetches.
    """
    time.sleep(1)
    conn = psycopg.connect(**conn_details)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_visibility")

    needs_vacuum = []
    for table in tables:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.relpages,
                       (SELECT all_visible
                        FROM pg_visibility_map_summary(%s::regclass))
                FROM pg_class c
                WHERE c.relname = %s AND c.relkind = 'r'
            """, (table, table))
            row = cur.fetchone()
            if row:
                relpages, all_visible = row
                if relpages != all_visible:
                    print(f"Table {table}: {all_visible}/{relpages} pages "
                          f"all-visible after load")
                    needs_vacuum.append(table)

    for table in needs_vacuum:
        print(f"Running VACUUM (FREEZE) {table} to set all pages all-visible...")
        with conn.cursor() as cur:
            cur.execute(f"VACUUM (FREEZE) {table}")
    if needs_vacuum:
        print("VACUUM (FREEZE) complete.")

    conn.close()


def _load_sql(conn_details, data_sql, suite_name, row_description):
    """Execute a data-loading SQL script with progress messages."""
    print("\n" + "=" * 50)
    print(f"Loading {suite_name} benchmark data...")
    print(f"This will take several minutes for {row_description}.")
    print("=" * 50 + "\n")

    conn = psycopg.connect(**conn_details)
    conn.autocommit = True

    # The harness's cache prep needs these extensions: pg_prewarm for
    # prewarm_relations(), pg_buffercache for evict_relations().  Ensure them once
    # here rather than repeating CREATE EXTENSION at the top of every suite's .sql.
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")

    statements = []
    current_stmt = []
    for line in data_sql.split('\n'):
        stripped = line.strip()
        if stripped.startswith('--') or not stripped:
            continue
        current_stmt.append(line)
        if stripped.endswith(';'):
            statements.append('\n'.join(current_stmt))
            current_stmt = []

    for statement in statements:
        statement = statement.strip()
        if not statement:
            continue
        try:
            if 'INSERT INTO' in statement:
                tbl_match = re.search(r'INSERT INTO (\S+)', statement)
                tbl_name = tbl_match.group(1) if tbl_match else "table"
                print(f"Loading {tbl_name}...")
            elif 'CREATE INDEX' in statement:
                idx_match = re.search(r'CREATE INDEX (\S+)', statement)
                idx_name = idx_match.group(1) if idx_match else "index"
                print(f"Creating index {idx_name}...")

            with conn.cursor() as cur:
                cur.execute(statement)

        except Exception as e:
            print(f"Error executing: {statement[:80]}...")
            print(f"Error: {e}")
            conn.close()
            sys.exit(1)

    conn.close()
    print(f"{suite_name} data loading complete.")


# ── suite loading ──────────────────────────────────────────────────────────
def _make_verify_fn(tables, suite_name, check_all_visible):
    """Build a one-arg verify_fn over the shared _verify_tables helper."""
    def verify(conn_details):
        return _verify_tables(conn_details, tables, suite_name,
                              check_all_visible=check_all_visible)
    return verify


def _make_load_fn(data_sql, suite_name, row_description):
    """Build a one-arg load_fn over the shared _load_sql helper."""
    def load(conn_details):
        _load_sql(conn_details, data_sql, suite_name, row_description)
    return load


def _load_one(toml_path):
    """Parse one suite TOML (+ its sidecar SQL) into a BENCHMARK_SUITES entry."""
    with open(toml_path, "rb") as f:
        raw = tomllib.load(f)
    spec = {**_SUITE_DEFAULTS, **raw["suite"]}

    # A suite's group is the suites/<group>/ subdirectory it lives in (None for
    # top-level suites).  It drives --help grouping in prefetch_benchmark.py.
    parent = toml_path.parent
    group = None if parent == SUITES_DIR else parent.name

    # The sidecar SQL sits beside the TOML (same directory, grouped or not).
    data_sql = (parent / spec["setup_sql_file"]).read_text()

    queries = OrderedDict()
    for qid, q in raw.get("queries", {}).items():
        entry = {
            "name": q["name"],
            "sql": q["sql"],
            "evict": q["evict"],
            "prewarm_indexes": q["prewarm_indexes"],
            "prewarm_tables": q["prewarm_tables"],
        }
        if "gucs" in q:
            entry["gucs"] = q["gucs"]
        if q.get("index_only"):
            entry["index_only"] = True
        if "setup_table" in q:
            entry["setup_table"] = q["setup_table"]
        queries[qid] = entry

    name = spec["mode_prefix"]
    all_visible = spec["all_visible"]
    return {
        "mode_prefix": name,
        "cli_flag": spec["cli_flag"],
        "cli_dest": spec["cli_dest"],
        "help": spec["help"],
        "title": spec["title"],
        "queries": queries,
        "verify_fn": _make_verify_fn(spec["tables"], name, all_visible),
        "load_fn": _make_load_fn(data_sql, name, spec["row_description"]),
        "tables": spec["tables"],
        "uncached_runs": spec["uncached_runs"],
        "cached_runs": spec["cached_runs"],
        "sync_stats": spec["sync_stats"],
        "expect_all_visible": all_visible,
        "group": group,
        "_order": spec["order"],
        "_default": spec["default"],
    }


def load_suites():
    """Load every suites/**/*.toml into the ordered BENCHMARK_SUITES list."""
    specs = [_load_one(p) for p in sorted(SUITES_DIR.rglob("*.toml"))]
    if not specs:
        sys.exit(f"Error: no benchmark suites found in {SUITES_DIR}")
    specs.sort(key=lambda s: s["_order"])

    # Exactly one suite is the default; it must sort to index 0 and carry no flag
    # (the no-flag fallthrough in suite selection lands on BENCHMARK_SUITES[0]).
    flagged_default = [s for s in specs if s["_default"]]
    if len(flagged_default) != 1:
        sys.exit(f"Error: exactly one suite must set default=true, "
                 f"found {len(flagged_default)}.")
    if specs[0] is not flagged_default[0]:
        sys.exit("Error: the default suite must have the lowest 'order'.")
    if specs[0]["cli_flag"] is not None:
        sys.exit("Error: the default suite must not define a cli_flag.")

    # Query ids must be globally unique so --queries can name any query
    # unambiguously and span several suites.  Fail loudly at import.
    owner = {}
    for s in specs:
        for qid in s["queries"]:
            if qid in owner:
                sys.exit(f"Error: duplicate query id '{qid}' in suites "
                         f"'{owner[qid]}' and '{s['mode_prefix']}'; query ids "
                         f"must be unique across all suites.")
            owner[qid] = s["mode_prefix"]

    for s in specs:
        del s["_order"]
        del s["_default"]
    return specs


BENCHMARK_SUITES = load_suites()


def build_query_suite_map():
    """Map every query id to its owning suite entry from BENCHMARK_SUITES.

    Ids are validated unique at load time, so this is a plain lookup builder.
    """
    owner = {}
    for suite in BENCHMARK_SUITES:
        for qid in suite["queries"].keys():
            owner[qid] = suite
    return owner
