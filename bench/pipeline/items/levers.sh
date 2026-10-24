#!/bin/bash
# levers.sh <hot|tpcc|pool|ahi|write|trx2|tpch|platform>: measure the advanced levers the rules do not use yet
# (docs/research/advanced-levers-2026-10-24.md) on a workload where each can act. One benchmark per item and session:
# experiments are compared with earlier runs of the same configuration, so benchmarks must not mix in a session.
# Calibration (3 runs of the baseline), then each lever value against that base, 2 runs each (try); host states the tuner
# does not manage (C-states, SMT) as alternating A/B runs (ab). The learner turns them into measured impact.
G=$1
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
GOAL=tps
case $G in
  hot)  # hot rows: deadlock detection cost and gap locking at high concurrency
    dataset 1 10000
    P=/root/bench/pipe-levers-hot.ini
    sb_profile $P "sysbench oltp_update_non_index 1 x 10k (hot rows), 256 threads, uniform ($(hostname))" oltp_update_non_index 1 10000 256 uniform 300 300
    post() {
      try $1 "lever: deadlock detection off" innodb_deadlock_detect=OFF innodb_lock_wait_timeout=5
      try $1 "lever: READ-COMMITTED" transaction_isolation=READ-COMMITTED
    } ;;
  tpcc)  # TPC-C district / warehouse hot rows, locking reads (SELECT ... FOR UPDATE): gap locks and deadlock detection
    gotpc_prep tpcc gotpcc 10
    dataset 1 10000  # the pristine copy (reset_datadir.sh) then holds the TPC-C data too
    P=/root/bench/pipe-levers-tpcc.ini
    cat > $P <<INI
[bench]
name = go-tpc TPC-C 10W, 128 threads (district / warehouse hot rows) ($(hostname))
reset = /root/bench/reset_datadir.sh
run = /opt/go-tpc/go-tpc tpcc run -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D gotpcc --warehouses 10 -T 128 --time {total}s --interval 30s --ignore-error
warmup = 120
duration = 300
timeout = 1800
goal_metric = tpmC
[metrics]
tpmC = tpmC: ([\d.]+)
INI
    post() {
      try $1 "lever: READ-COMMITTED" transaction_isolation=READ-COMMITTED
      try $1 "lever: deadlock detection off" innodb_deadlock_detect=OFF innodb_lock_wait_timeout=5
    } ;;
  pool)  # 1024 connections: thread pool parameters instead of on / off (the rule lost 31 of 32 times at defaults)
    dataset 16 1000000
    P=/root/bench/pipe-levers-1024.ini
    sb_profile $P "sysbench oltp_read_write 16 x 1M (in memory), 1024 threads, special ($(hostname))" oltp_read_write 16 1000000 1024 special 300 300
    post() {
      try $1 "lever: thread pool, size = CPUs" thread_handling=pool-of-threads thread_pool_size=$(nproc)
      try $1 "lever: thread pool, size = 2 x CPUs" thread_handling=pool-of-threads thread_pool_size=$((2 * $(nproc)))
      try $1 "lever: thread pool, oversubscribe 10" thread_handling=pool-of-threads thread_pool_size=$(nproc) thread_pool_oversubscribe=10
      try $1 "lever: thread pool, stall limit 50 ms" thread_handling=pool-of-threads thread_pool_size=$(nproc) thread_pool_stall_limit=50
    } ;;
  ahi)  # hot in-memory reads at 256 threads: latch contention levers
    dataset 16 1000000
    P=/root/bench/pipe-levers-ro256.ini
    sb_profile $P "sysbench oltp_read_only 16 x 1M (in memory), 256 threads, special ($(hostname))" oltp_read_only 16 1000000 256 special 300 300
    post() {
      try $1 "lever: AHI on, default partitions" innodb_adaptive_hash_index=ON
      try $1 "lever: AHI on, 32 partitions" innodb_adaptive_hash_index=ON innodb_adaptive_hash_index_parts=32
      try $1 "lever: sync array size 16" innodb_sync_array_size=16
      try $1 "lever: spin wait pause multiplier 10" innodb_spin_wait_pause_multiplier=10
    } ;;
  write)  # writes larger than memory, binary log on, fsync per commit
    dataset 16 10000000
    P=/root/bench/pipe-levers-write.ini
    sb_profile $P "sysbench oltp_write_only 16 x 10M (~37 GB, > RAM), 128 threads, uniform, binary log on ($(hostname))" oltp_write_only 16 10000000 128 uniform 600 600
    post() {
      try $1 "lever: doublewrite DETECT_ONLY" innodb_doublewrite=DETECT_ONLY
      try $1 "lever: doublewrite OFF (reference for DETECT_ONLY)" innodb_doublewrite=OFF
      try $1 "lever: binlog order commits off" binlog_order_commits=OFF
      try $1 "lever: binlog transaction compression" binlog_transaction_compression=ON
    } ;;
  trx2)  # CPU-bound commit path without fsync per commit (where log writer threads off won 9 of 10)
    dataset 16 10000000
    P=/root/bench/pipe-levers-trx2.ini
    sb_profile $P "sysbench oltp_write_only 16 x 10M, 256 threads, uniform, trx_commit=2 (CPU-bound commits) ($(hostname))" oltp_write_only 16 10000000 256 uniform 600 600
    PRE_APPLY="innodb_flush_log_at_trx_commit=2"
    post() {
      try $1 "lever: log writer threads off" innodb_log_writer_threads=OFF
      try $1 "lever: redo spin off (abs lwm 0)" innodb_log_spin_cpu_abs_lwm=0
      try $1 "lever: redo spin always (pct hwm 100)" innodb_log_spin_cpu_pct_hwm=100
      try $1 "lever: flush log at timeout 2 s" innodb_flush_log_at_timeout=2
    } ;;
  tpch)  # analytics: temporary tables and parallel reads (goal: total pass time, lower is better)
    gotpc_prep tpch tpch 1
    P=/root/bench/tpch.ini
    GOAL=latency
    post() {
      try $1 "lever: TempTable RAM 4G" temptable_max_ram=4G
      try $1 "lever: internal temporary tables in MEMORY" internal_tmp_mem_storage_engine=MEMORY tmp_table_size=256M max_heap_table_size=256M
      try $1 "lever: TempTable mmap limit 0 (spill to InnoDB)" temptable_max_mmap=0
      try $1 "lever: parallel read threads = CPUs" innodb_parallel_read_threads=$(nproc)
    } ;;
  platform)  # host states: C-states (latency at low concurrency), SMT (CPU-bound), buffer pool dump / load (warm-up time)
    dataset 16 1000000
    P=/root/bench/pipe-levers-point.ini
    sb_profile $P "sysbench oltp_point_select 16 x 1M (in memory), 8 threads, uniform ($(hostname))" oltp_point_select 16 1000000 8 uniform 120 300
    P2=/root/bench/pipe-levers-cpu.ini
    sb_profile $P2 "sysbench oltp_read_only 16 x 1M (in memory), 64 threads, uniform: CPU-bound ($(hostname))" oltp_read_only 16 1000000 64 uniform 120 300
    post() {
      # A/B runs are compared by label (avg / p95 latency and tps from each run's summary), not by the session's verdicts
      if ls /sys/devices/system/cpu/cpu0/cpuidle/state1 >/dev/null 2>&1 && command -v cpupower >/dev/null; then
        ab $1 "C-states: A = all idle states, B = only exit latency <= 10 us" 3 "cpupower idle-set -D 10" "cpupower idle-set -E"
      fi
      if [ -w /sys/devices/system/cpu/smt/control ]; then
        ab $P2 "SMT: A = on, B = off" 3 "echo off > /sys/devices/system/cpu/smt/control" "echo on > /sys/devices/system/cpu/smt/control"
      fi
      # warm-up time after a clean restart without and with the buffer pool dump / load (run-length probe settle times)
      tuning-agent apply --set innodb_buffer_pool_dump_at_shutdown=OFF --set innodb_buffer_pool_load_at_startup=OFF --reason "pipeline $PIPE_ITEM: warm-up without dump/load"
      tuning-agent run-length --bench $P2 > /root/bench/pipeline/logs/levers-warmup-nodump.txt 2>&1 || true
      tuning-agent apply --set innodb_buffer_pool_dump_at_shutdown=ON --set innodb_buffer_pool_load_at_startup=ON --set innodb_buffer_pool_dump_pct=100 --reason "pipeline $PIPE_ITEM: warm-up with dump/load"
      tuning-agent run-length --bench $P2 > /root/bench/pipeline/logs/levers-warmup-dump.txt 2>&1 || true
    } ;;
  *) echo "QUEUEFAILED: unknown group $G"; exit 1 ;;
esac
export -f post
STUDY="pipeline: advanced levers $(date -u +%F)" POST=post PRE_APPLY="$PRE_APPLY" run_session $P "levers $G ($(hostname))" $GOAL 0
echo PIPEOK
