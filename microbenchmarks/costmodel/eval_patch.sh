#!/bin/bash
# Price the grid with the committed patch binary (no developer GUCs): NVMe
# profile on the scratch datadir, then the delay profile on data-delay.
set -e
W=/mnt/nvme/postgresql/patch/claude_work/costmodel
PGP=$W/patchbuild/tmp_install/mnt/nvme/postgresql/patch/claude_work/costmodel/patchinst/bin
cd $W
until grep -q PATCH4_DONE logs/build_patch4.log 2>/dev/null || grep -q "PATCH4_DONE" /dev/null; do sleep 10; done
echo "== $(date) NVMe"
$PGP/pg_ctl -D $W/data -l logs/server_patchB.log -w start
python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 16 --models patchB.json --out costs.jsonl
python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 100 --models patchB.json --out costs.jsonl
$PGP/pg_ctl -D $W/data -w stop -m fast
echo "== $(date) delay"
sed "s#\$W/inst/bin/pg_ctl#$PGP/pg_ctl#" start_delay.sh > start_delay_patch.sh && chmod +x start_delay_patch.sh && ./start_delay_patch.sh
RIG_PROFILE=delay python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 16 --models patchB.json --out costs.jsonl
RIG_PROFILE=delay python3 rig.py costs --kinds single,two,limit --layouts corr,rand,run4,jit,dup64,run16 --eic 100 --models patchB.json --out costs.jsonl
$PGP/pg_ctl -D /mnt/nvme/postgresql/patch/data-delay -w stop -m fast
echo "== $(date) EVAL_PATCH_DONE"
