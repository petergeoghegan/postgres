#!/bin/bash
# Delay-device phase: start the user's dm-delay cluster with the scratch build,
# load the half-size tables into a new database, run the grid (cold at eic 16
# and 100, warm once).
set -e
W=/mnt/nvme/postgresql/patch/claude_work/costmodel
PSQL="$W/inst/bin/psql -p 54330 -h $W/sock -Xq -v ON_ERROR_STOP=1"
cd $W
echo "== $(date) start delay cluster"
$W/inst/bin/pg_ctl -D /mnt/nvme/postgresql/patch/data-delay -w stop -m fast || true
./start_delay.sh
$PSQL -d postgres -c "SELECT name, setting FROM pg_settings WHERE name IN ('shared_buffers','huge_pages_status','io_method','random_page_cost','effective_io_concurrency','io_max_concurrency','data_directory')"
$PSQL -d postgres -c "DROP DATABASE IF EXISTS costmodel" -c "CREATE DATABASE costmodel"
$PSQL -d costmodel -c "CREATE EXTENSION pg_prewarm; CREATE EXTENSION pg_buffercache;"
echo "== $(date) load (reads are delayed 1 ms; this takes a while)"
$PSQL -d costmodel -f gen_tables_delay.sql
$PSQL -d costmodel -f extra_indexes.sql
echo "== $(date) grid"
RIG_PROFILE=delay python3 rig.py run --kinds single --layouts corr,rand,run4,jit,dup64,run16 --caches cold,warm --eics 16,100 --runs-cold 1 --runs-warm 2 --out timing.jsonl
RIG_PROFILE=delay python3 rig.py run --kinds two,limit --caches cold,warm --eics 16,100 --runs-cold 1 --runs-warm 2 --out timing.jsonl
echo "== $(date) cost phase (same planner, half-size tables)"
RIG_PROFILE=delay python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 16 --models models.json --out costs.jsonl
RIG_PROFILE=delay python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 100 --models models.json --out costs.jsonl
echo "== $(date) DELAY_PHASE_DONE"
