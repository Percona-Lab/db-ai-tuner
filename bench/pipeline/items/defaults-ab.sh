#!/bin/bash
# Defaults that changed between 8.0 and 8.4 (server-variables inventory 2026-10-21): which of them matter, per workload?
# Each 8.0 value tried alone and all together against the tuned 8.4 configuration, on a write-heavy and a read-heavy load.
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
dataset 16 10000000
post() {
  try $1 "all 8.0 defaults together" innodb_io_capacity=200 innodb_io_capacity_max=2000 innodb_adaptive_hash_index=ON innodb_change_buffering=all innodb_flush_method=fsync innodb_log_buffer_size=16M innodb_use_fdatasync=OFF innodb_read_io_threads=4
  try $1 "8.0 default: adaptive hash index ON" innodb_adaptive_hash_index=ON
  try $1 "8.0 default: change buffering all" innodb_change_buffering=all
  try $1 "8.0 default: io_capacity 200 / 2000" innodb_io_capacity=200 innodb_io_capacity_max=2000
  try $1 "8.0 default: flush_method fsync" innodb_flush_method=fsync
}
for w in "oltp_update_index uniform write" "oltp_read_only special read"; do
  set -- $w
  P=/root/bench/pipe-defaults-$3.ini
  sb_profile $P "sysbench $1 16 x 10M (~37 GB, > RAM), 64 threads, $2 ($(hostname))" $1 16 10000000 64 $2 600 600
export -f post
  STUDY="pipeline: 8.0 vs 8.4 defaults $(date -u +%F)" POST=post run_session $P "defaults 8.0 vs 8.4, $3 ($(hostname))" tps 4
done
echo PIPEOK
