#!/usr/bin/env python3
"""Record the planner's unforced choice for the illustration queries under the
stock model and under the committed patch, at eic 16 and 100.  Run once
against the GUC build (stock = debug_index_cost_model 0) and once against the
patch build (no GUC); pass the label as argv[1]."""
import sys, os
sys.path.insert(0, "/mnt/nvme/postgresql/patch/claude_work/costmodel")
import rig  # noqa: E402

label = sys.argv[1]
OUT = "/mnt/nvme/postgresql/patch/claude_work/costmodel/results/illustration"
QUERIES = [
    ("t_dups", "SELECT * FROM t_dups WHERE grp BETWEEN 40001 AND 55625 ORDER BY grp"),
    ("t_runs", "SELECT * FROM t_runs WHERE id BETWEEN 2500001 AND 3500000 ORDER BY id"),
    ("t_sparse", "SELECT * FROM t_sparse WHERE id BETWEEN 50001 AND 70000 ORDER BY id"),
    ("t_sparse_ios_vm40", "SELECT id FROM t_sparse_ios WHERE id BETWEEN 50001 AND 130000"),
]
r = rig.Rig()
with open(os.path.join(OUT, f"choices_{label}.txt"), "w") as f:
    for name, sql in QUERIES:
        for eic in (16, 100):
            r.reset_gucs()
            r.set_gucs({"work_mem": "1GB", "max_parallel_workers_per_gather": 0,
                        "random_page_cost": 4.0, "seq_page_cost": 1.0,
                        "effective_io_concurrency": eic})
            with r.conn.cursor() as cur:
                cur.execute("EXPLAIN " + sql)
                text = "\n".join(row[0] for row in cur.fetchall())
            f.write(f"-- {name} eic={eic} ({label})\n{text}\n\n")
            print(f"{label:6} {name:18} eic={eic:3}: {text.splitlines()[0]}")
