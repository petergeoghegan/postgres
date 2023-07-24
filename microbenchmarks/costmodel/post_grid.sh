#!/bin/bash
# After the first NVMe grid pass: install the newest scratch build (needs a
# server restart), add the extra layouts, drop the untrustworthy cold rows and
# re-run them, run the extra layouts, then the cost-estimate phase.
set -e
W=/mnt/nvme/postgresql/patch/claude_work/costmodel
PSQL="$W/inst/bin/psql -p 54329 -h $W/sock -d costmodel -Xq"
cd $W
echo "== $(date) stop/install/start"
$W/inst/bin/pg_ctl -D $W/data -w stop -m fast
ninja -C build install > logs/install3.log 2>&1
$W/inst/bin/pg_ctl -D $W/data -l logs/server.log -w start
$PSQL -c "SHOW debug_index_cost_model" | head -3
echo "== $(date) extra indexes"
$PSQL -f extra_indexes.sql
echo "== $(date) drop untrustworthy cold rows"
python3 drop_prenap.py
echo "== $(date) re-run dropped cells"
python3 rig.py run --kinds single --caches cold,warm --eics 16,100 --runs-cold 2 --runs-warm 3 --out timing.jsonl
echo "== $(date) extra layouts"
python3 rig.py run --kinds single --layouts dup64,run16 --caches cold,warm --eics 16,100 --runs-cold 2 --runs-warm 3 --out timing.jsonl
echo "== $(date) cost phase eic=16"
python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 16 --models models.json --out costs.jsonl
echo "== $(date) cost phase eic=100"
python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 100 --models models.json --out costs.jsonl
echo "== $(date) POST_GRID_DONE"
