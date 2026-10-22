#!/bin/bash
# Drive write cache as a measured trade-off (os:disk_write_cache, added 2026-10-22): the same commit-bound load with the
# data drive's volatile cache as found and switched, 3 runs each way. On drives with power-loss protection the cache may
# not be switchable (then the experiment fails cleanly and says so - also a result).
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
dataset 16 2000000
P=/root/bench/pipe-commit.ini
sb_profile $P "sysbench oltp_update_non_index 16 x 2M (~8 GB, in memory), 256 threads, uniform: commit-bound ($(hostname))" oltp_update_non_index 16 2000000 256 uniform 180 300
dev=$(lsblk -no PKNAME $(findmnt -no SOURCE /var/lib/mysql 2>/dev/null || findmnt -no SOURCE /) | head -1)
echo "data drive $dev: write cache $(cat /sys/block/$dev/queue/write_cache); hdparm: $(hdparm -W /dev/$dev 2>&1 | tr -s ' ' | tail -1)"
now=$(cat /sys/block/$dev/queue/write_cache)
other=$([ "$now" = "write back" ] && echo OFF || echo ON)
post() { REPEATS=3 try $1 "drive write cache switched ($now -> $other)" os:disk_write_cache=$other; }
export -f post; export now other
STUDY="pipeline: drive write cache $(date -u +%F)" PRE_APPLY="disable_log_bin=ON" POST=post run_session $P "write-cache A/B ($(hostname))" tps 4
echo "data drive $dev after: $(cat /sys/block/$dev/queue/write_cache)"
echo PIPEOK
