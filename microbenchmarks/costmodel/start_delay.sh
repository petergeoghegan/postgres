#!/bin/bash
# Start the user's dm-delay cluster with the scratch build on port 54330.
# Command-line -c options override the copied postgresql.conf (16GB huge-page
# shared_buffers, random_page_cost=2, file logging, ...).
W=/mnt/nvme/postgresql/patch/claude_work/costmodel
D=/mnt/nvme/postgresql/patch/data-delay
exec $W/inst/bin/pg_ctl -D $D -l $W/logs/server_delay.log -w -t 300 start -o "\
 -p 54330 -k $W/sock -c listen_addresses='' -c max_connections=20 \
 -c shared_buffers=4GB -c huge_pages=off -c work_mem=1GB -c maintenance_work_mem=2GB \
 -c effective_cache_size=24GB -c io_method=io_uring -c io_combine_limit=16 \
 -c io_max_combine_limit=128 -c io_max_concurrency=128 -c effective_io_concurrency=16 \
 -c maintenance_io_concurrency=16 -c max_parallel_workers_per_gather=0 \
 -c max_parallel_maintenance_workers=0 -c autovacuum=off -c wal_level=minimal \
 -c max_wal_senders=0 -c max_wal_size=512MB -c wal_keep_size=0 -c max_slot_wal_keep_size=-1 -c checkpoint_timeout=1h \
 -c synchronous_commit=off -c jit=off -c track_io_timing=off -c random_page_cost=4 \
 -c log_min_messages=warning -c logging_collector=off -c log_destination=stderr \
 -c shared_preload_libraries='' -c restart_after_crash=off"
