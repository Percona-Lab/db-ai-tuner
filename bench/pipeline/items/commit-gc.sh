#!/bin/bash
# Commit-bound validation of the group commit analysis (2026-10-22): in-memory data, single-row updates from 256
# threads, one session with the binary log off (InnoDB redo group commit only) and one with it on (binary log group
# commit, sync_binlog = 1). Checks: commit diagnosis from measured waits, batch vs waiting vs in flight, write-ahead sweep,
# binlog delay sized from the waiters only when grouping is loose, storage advice.
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
dataset 16 2000000
P=/root/bench/pipe-commit.ini
sb_profile $P "sysbench oltp_update_non_index 16 x 2M (~8 GB, in memory), 256 threads, uniform: commit-bound ($(hostname))" oltp_update_non_index 16 2000000 256 uniform 180 300
STUDY="pipeline: group commit $(date -u +%F)" PRE_APPLY="disable_log_bin=ON" run_session $P "commit-gc binlog off ($(hostname))" tps 8
STUDY="pipeline: group commit $(date -u +%F)" run_session $P "commit-gc binlog on ($(hostname))" tps 8
echo PIPEOK
