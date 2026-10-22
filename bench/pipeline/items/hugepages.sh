#!/bin/bash
# Huge pages through the agent (AppArmor fix of 2026-10-21): a large in-memory buffer pool, read-mostly point selects
# (TLB-sensitive). large_pages with the huge pages it needs, 3 runs, against the tuned configuration.
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
dataset 16 2000000
P=/root/bench/pipe-point.ini
sb_profile $P "sysbench oltp_point_select 16 x 2M (~8 GB, in memory), 64 threads, uniform ($(hostname))" oltp_point_select 16 2000000 64 uniform 180 300
post() {
  local bp=$(cur innodb_buffer_pool_size); local pages=$(( bp / 2097152 + bp / 2097152 / 20 + 64 ))
  REPEATS=3 try $1 "large pages for a $(( bp / 1073741824 )) GB buffer pool" large_pages=ON os:hugepages=$pages
}
export -f post
STUDY="pipeline: huge pages $(date -u +%F)" POST=post run_session $P "huge pages ($(hostname))" tps 4
echo PIPEOK
