#!/bin/bash
# priors.sh <write|read|binlog>: measure parameters whose impact is still a documentation prior (parameter catalog,
# prior_source = documentation) - one alternative value each, 2 runs, on a workload where they can act. The learner turns
# these single-setting experiments into measured impact.
G=$1
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
dataset 16 10000000
case $G in
  write)
    P=/root/bench/pipe-priors-write.ini
    sb_profile $P "sysbench oltp_update_index 16 x 10M (~37 GB, > RAM), 64 threads, uniform ($(hostname))" oltp_update_index 16 10000000 64 uniform 600 600
    post() {
      try $1 "prior: adaptive flushing low-water mark" innodb_adaptive_flushing_lwm=20
      try $1 "prior: flushing average loops" innodb_flushing_avg_loops=5
      try $1 "prior: purge batch size" innodb_purge_batch_size=1000
      try $1 "prior: sync array size" innodb_sync_array_size=16
      try $1 "prior: spin wait pause multiplier" innodb_spin_wait_pause_multiplier=10
      try $1 "prior: redo write-ahead size" innodb_log_write_ahead_size=16384
      try $1 "prior: idle flush pct" innodb_idle_flush_pct=1
      try $1 "prior: change buffer max size" innodb_change_buffer_max_size=50
    } ;;
  read)
    P=/root/bench/pipe-priors-read.ini
    sb_profile $P "sysbench oltp_read_only 16 x 10M (~37 GB, > RAM), 64 threads, special ($(hostname))" oltp_read_only 16 10000000 64 special 600 600
    post() {
      try $1 "prior: old blocks time" innodb_old_blocks_time=0
      try $1 "prior: old blocks pct" innodb_old_blocks_pct=20
      try $1 "prior: adaptive hash index partitions" innodb_adaptive_hash_index_parts=32
      try $1 "prior: table open cache instances" table_open_cache_instances=4
      try $1 "prior: read-ahead threshold" innodb_read_ahead_threshold=8
      try $1 "prior: stats persistent sample pages" innodb_stats_persistent_sample_pages=100
    } ;;
  binlog)
    P=/root/bench/pipe-priors-binlog.ini
    sb_profile $P "sysbench oltp_write_only 16 x 10M (~37 GB, > RAM), 128 threads, uniform, binary log on ($(hostname))" oltp_write_only 16 10000000 128 uniform 600 600
    post() {
      try $1 "prior: binlog row image minimal" binlog_row_image=MINIMAL
      try $1 "prior: binlog transaction compression" binlog_transaction_compression=ON
      try $1 "prior: binlog cache size" binlog_cache_size=1M
      try $1 "prior: group commit no-delay count" binlog_group_commit_sync_delay=500 binlog_group_commit_sync_no_delay_count=64
    } ;;
esac
STUDY="pipeline: documentation priors $(date -u +%F)" POST=post run_session $P "priors $G ($(hostname))" tps 4
echo PIPEOK
