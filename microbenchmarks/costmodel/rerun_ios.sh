#!/bin/bash
set -e
W=/mnt/nvme/postgresql/patch/claude_work/costmodel
cd $W
PGS=$W/stage/mnt/nvme/postgresql/patch/claude_work/costmodel/inst/bin
PGP=$W/patchbuild/tmp_install/mnt/nvme/postgresql/patch/claude_work/costmodel/patchinst/bin
$PGS/pg_ctl -D data -l logs/server.log -w start > /dev/null
nohup python3 nvme_heartbeat.py /mnt/nvme/postgresql/patch/dm-delay-backing.img > logs/heartbeat.log 2>&1 &
echo $! > heartbeat.pid
sleep 1
echo "== $(date) reset t_sparse_ios"
$W/inst/bin/psql -p 54329 -h $W/sock -d costmodel -Xq -v ON_ERROR_STOP=1 -f illustrate_setup_ios.sql > logs/setup_ios2.log 2>&1
echo "== $(date) coverage series"
python3 illustrate_ios.py > logs/illustrate_ios.log 2>&1
echo "== $(date) 90% cached"
python3 illustrate90.py > logs/illustrate90.log 2>&1
kill $(cat heartbeat.pid)
$PGS/pg_ctl -D data -w stop -m fast > /dev/null
echo "== $(date) planner choices with the patch"
$PGP/pg_ctl -D data -l logs/server_patchB.log -w start > /dev/null
python3 illustrate_choices.py patch > logs/choices_patch.log 2>&1
$PGP/pg_ctl -D data -w stop -m fast > /dev/null
echo "== $(date) RERUN_IOS_DONE"
