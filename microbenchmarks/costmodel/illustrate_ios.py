#!/usr/bin/env python3
"""Index-only scan with partial visibility-map coverage (LP_DEAD recipe).

t_sparse_ios (see illustrate_setup_ios.sql) has one primary and one filler
row per page.  Each stage deletes the filler row on a deterministic
pseudo-random set of pages and runs VACUUM (INDEX_CLEANUP off, ANALYZE), which
prunes the deleted rows into LP_DEAD stubs without removing them: such a page
cannot be marked all-visible by VACUUM or by a scan's opportunistic pruning,
so an index-only scan must fetch it.  REINDEX drops the deleted rows' index
entries.  Coverage: 100%, then 60%, then 40% of the pages all-visible.
Measures the index-only scan against the bitmap heap scan + Sort alternative,
cached and heap-uncached, and records the planner's unforced choice under the
stock model and under the patch (debug_index_cost_model 17) at eic=100.
Outputs to results/illustration/ios_*."""
import os
import sys
import json

sys.path.insert(0, "/mnt/nvme/postgresql/patch/claude_work/costmodel")
import rig  # noqa: E402

OUT = "/mnt/nvme/postgresql/patch/claude_work/costmodel/results/illustration"
SQL = "SELECT id FROM t_sparse_ios WHERE id BETWEEN 50001 AND 130000"
PLANS = {
    "ios": {"enable_bitmapscan": "off", "enable_seqscan": "off"},
    "bitmap": {"enable_indexonlyscan": "off", "enable_indexscan": "off", "enable_seqscan": "off"},
}
STATES = [("cached", None), ("uncached_eic100", 100), ("uncached_eic16", 16)]
COVERAGE = [("vm100", None),
            ("vm60", "DELETE FROM t_sparse_ios WHERE id > 200000 AND abs(hashint4(id - 200000)) % 5 < 2"),
            ("vm40", "DELETE FROM t_sparse_ios WHERE id > 200000 AND abs(hashint4(id - 200000)) % 5 = 2")]


def explain_text(r, sql, analyze=True):
    with r.conn.cursor() as cur:
        cur.execute(("EXPLAIN (ANALYZE, BUFFERS, IO) " if analyze else "EXPLAIN ") + sql)
        return "\n".join(row[0] for row in cur.fetchall())


def base_gucs(r, eic):
    r.reset_gucs()
    r.set_gucs({"work_mem": "1GB", "max_parallel_workers_per_gather": 0,
                "random_page_cost": 4.0, "seq_page_cost": 1.0,
                "effective_io_concurrency": eic})


def main():
    r = rig.Rig()
    r.exec("SET synchronous_commit = on")
    summary = {}
    for cov, lock_sql in COVERAGE:
        if lock_sql:
            r.exec(lock_sql)
            r.exec("VACUUM (INDEX_CLEANUP off, ANALYZE) t_sparse_ios")
            r.exec("REINDEX INDEX t_sparse_ios_id")
            r.exec("CHECKPOINT")
        r.exec("ANALYZE t_sparse_ios")
        with r.conn.cursor() as cur:
            cur.execute("SELECT relpages, relallvisible FROM pg_class WHERE relname = 't_sparse_ios'")
            relpages, relallvisible = cur.fetchone()
        print(f"== {cov}: relpages={relpages} relallvisible={relallvisible} "
              f"({100.0 * relallvisible / relpages:.0f}%)", flush=True)
        # planner's unforced choices
        choices = {}
        for label, model, eic in (("stock_eic16", 0, 16), ("stock_eic100", 0, 100), ("patch_eic100", 17, 100)):
            base_gucs(r, eic)
            r.set_gucs({"debug_index_cost_model": model})
            text = explain_text(r, SQL, analyze=False)
            choices[label] = text
            print(f"   {label:13}: {text.splitlines()[0]}", flush=True)
        with open(os.path.join(OUT, f"ios_{cov}_choices.txt"), "w") as f:
            for k, v in choices.items():
                f.write(f"-- {k}\n{v}\n\n")
        for plan, gucs in PLANS.items():
            for state, eic in STATES:
                best = None
                for run in range(3 if state == "cached" else 2):
                    base_gucs(r, eic or 100)
                    r.set_gucs(gucs)
                    if state == "cached":
                        r.prewarm("t_sparse_ios")
                    else:
                        r.evict("t_sparse_ios")
                    r.prewarm("t_sparse_ios", "vm")
                    r.prewarm("t_sparse_ios_id")
                    text = explain_text(r, SQL)
                    ms = float([l for l in text.splitlines() if l.startswith("Execution Time")][0].split()[2])
                    if best is None or ms < best[0]:
                        best = (ms, text)
                name = f"ios_{cov}_{plan}_{state}"
                with open(os.path.join(OUT, name + ".txt"), "w") as f:
                    f.write(f"-- {SQL}\n-- {plan}, {cov}, {state}\n{best[1]}\n")
                summary[name] = best[0]
                hf = [l.strip() for l in best[1].splitlines() if "Heap Fetches" in l]
                print(f"   {name:34} {best[0]:8.1f} ms  {hf[0] if hf else ''}", flush=True)
    json.dump(summary, open(os.path.join(OUT, "ios_summary.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
