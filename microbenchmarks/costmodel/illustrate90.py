#!/usr/bin/env python3
"""90%-cached runs for the illustration queries.

State: the whole table is prewarmed into shared_buffers, then every heap
page whose block number hashes to 0 mod 10 is evicted with
pg_buffercache_evict(), and the table's files are dropped from the OS page
cache, so the scan finds a pseudo-random 90% of its pages in shared_buffers
and reads the other 10% from the device (isolated single-page misses, the
uniform-miss model behind the 90% cache-hit assumption of the page costs).
Index and visibility map warm.  Outputs to results/illustration/*_cached90_*."""
import json
import os
import sys

sys.path.insert(0, "/mnt/nvme/postgresql/patch/claude_work/costmodel")
import rig  # noqa: E402

OUT = "/mnt/nvme/postgresql/patch/claude_work/costmodel/results/illustration"
QUERIES = [
    ("t_dups", "t_dups_grp", "SELECT * FROM t_dups WHERE grp BETWEEN 40001 AND 55625 ORDER BY grp",
     {"plain": {"enable_bitmapscan": "off", "enable_seqscan": "off"},
      "bitmap": {"enable_indexscan": "off", "enable_seqscan": "off"}}),
    ("t_runs", "t_runs_id", "SELECT * FROM t_runs WHERE id BETWEEN 2500001 AND 3500000 ORDER BY id",
     {"plain": {"enable_bitmapscan": "off", "enable_seqscan": "off"},
      "bitmap": {"enable_indexscan": "off", "enable_seqscan": "off"}}),
    ("t_sparse", "t_sparse_id", "SELECT * FROM t_sparse WHERE id BETWEEN 50001 AND 70000 ORDER BY id",
     {"plain": {"enable_bitmapscan": "off", "enable_seqscan": "off"},
      "bitmap": {"enable_indexscan": "off", "enable_seqscan": "off"}}),
    ("ios_vm40", "t_sparse_ios_id", "SELECT id FROM t_sparse_ios WHERE id BETWEEN 50001 AND 130000",
     {"ios": {"enable_bitmapscan": "off", "enable_seqscan": "off"},
      "bitmap": {"enable_indexonlyscan": "off", "enable_indexscan": "off", "enable_seqscan": "off"}}),
]
TABLE_OF = {"ios_vm40": "t_sparse_ios"}


def cache90(r, table, index):
    r.prewarm(table)
    r.prewarm(table, "vm")
    r.prewarm(index)
    r.exec(f"""SELECT count(*) FROM (
                 SELECT pg_buffercache_evict(b.bufferid)
                 FROM pg_buffercache b, pg_class c
                 WHERE c.relname = '{table}'
                   AND b.relfilenode = pg_relation_filenode(c.oid)
                   AND b.reldatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
                   AND b.relforknumber = 0
                   AND hashint4(b.relblocknumber::int4) % 10 = 0) s""")
    files = r.main_fork_files(table)
    rig.fadvise_dontneed(files)
    relpages = r.q1("SELECT relpages FROM pg_class WHERE relname = %s", table)
    sb = r.sb_blocks(table)
    res = sum(rig.resident_bytes(f) or 0 for f in files)
    frac = sb / relpages
    if not (0.85 <= frac <= 0.95) or res > 4 * 1024 * 1024:
        raise RuntimeError(f"cache90 {table}: {sb}/{relpages} in shared_buffers, {res} bytes resident in OS cache")
    return frac


def explain_text(r, sql):
    with r.conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, IO) " + sql)
        return "\n".join(row[0] for row in cur.fetchall())


def main():
    r = rig.Rig()
    summary = {}
    for name, index, sql, plans in QUERIES:
        table = TABLE_OF.get(name, name)
        for plan, gucs in plans.items():
            for eic in (16, 100):
                best = None
                for run in range(2):
                    r.reset_gucs()
                    r.set_gucs(gucs)
                    r.set_gucs({"work_mem": "1GB", "max_parallel_workers_per_gather": 0,
                                "random_page_cost": 4.0, "seq_page_cost": 1.0,
                                "effective_io_concurrency": eic})
                    frac = cache90(r, table, index)
                    text = explain_text(r, sql)
                    ms = float([l for l in text.splitlines() if l.startswith("Execution Time")][0].split()[2])
                    if best is None or ms < best[0]:
                        best = (ms, text, frac)
                key = f"{name}_{plan}_cached90_eic{eic}"
                with open(os.path.join(OUT, key + ".txt"), "w") as f:
                    f.write(f"-- {sql}\n-- {plan}, {best[2] * 100:.0f}% of heap pages in shared_buffers, eic={eic}\n{best[1]}\n")
                summary[key] = best[0]
                hit = [l.strip() for l in best[1].splitlines() if "Buffers: shared" in l][:1]
                print(f"{key:36} {best[0]:8.1f} ms  ({best[2] * 100:.0f}% cached; {hit[0] if hit else ''})", flush=True)
    json.dump(summary, open(os.path.join(OUT, "summary_cached90.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
