#!/bin/bash
# matrix.sh <cell>: one cell of the case-base matrix v2 (docs/battery/matrix-v2.md, first wave 2026-10-26): an ordinary
# autotune session - Percona Server 8.4, rules + learnings + similarity (no AI), run length chosen automatically,
# production-safe first then trade-offs, 16 runs - whose runs become cases for the similarity ranking. Cells sharing a data
# set reuse its pristine copy.
CELL=$1
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
P=/root/bench/pipe-matrix-$CELL.ini
GOAL=tps
auto_len() {  # the run length from the run-length probe (warm-up and window), not fixed numbers
  sed -i -e 's/^warmup = .*/warmup = auto/' -e 's/^duration = .*/duration = auto/' -e 's/^timeout = .*/timeout = 14400/' $1
}
latency_goal() {  # latency goal: the benchmark's 95th percentile, lower is better
  sed -i 's/^goal_metric = .*/goal_metric = sysbench_p95_ms/' $1
  GOAL=latency
}
tpcc_profile() {  # tpcc_profile <warehouses> <threads>
  cat > $P <<INI
[bench]
name = matrix $CELL: go-tpc TPC-C $1 W, $2 threads ($(hostname))
reset = /root/bench/reset_datadir.sh
run = ulimit -n 1048576; /opt/go-tpc/go-tpc tpcc run -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D gotpcc$1 --warehouses $1 -T $2 --time {total}s --interval 30s --ignore-error
warmup = auto
duration = auto
timeout = 14400
goal_metric = tpmC

[metrics]
tpmC = tpmC: ([\d.]+)

[requires]
max_connections = $(( $2 * 2 + 100 ))
INI
}
case $CELL in
  # atvbm: sysbench in memory, 8 x 4M (~8 GB)
  M4) dataset 8 4000000; sb_profile $P "matrix M4: sysbench oltp_write_only 8 x 4M (in memory), 8 threads ($(hostname))" oltp_write_only 8 4000000 8 uniform 300 300 ;;
  M5) dataset 8 4000000; sb_profile $P "matrix M5: sysbench oltp_update_non_index 8 x 4M (in memory), 1024 threads ($(hostname))" oltp_update_non_index 8 4000000 1024 uniform 300 300 ;;
  M6) dataset 8 4000000; sb_profile $P "matrix M6: sysbench oltp_read_write 8 x 4M (in memory), hot rows, 48 threads, p95 latency ($(hostname))" oltp_read_write 8 4000000 48 pareto 300 300
      latency_goal $P ;;
  # atvbm2: sysbench partly cached, 16 x 16M (~60 GB), then mostly on disk, 32 x 28M (~220 GB)
  P4) dataset 16 16000000; sb_profile $P "matrix P4: sysbench oltp_read_write 16 x 16M (~60 GB, partly cached), 8 threads ($(hostname))" oltp_read_write 16 16000000 8 uniform 300 300 ;;
  D1) dataset 32 28000000; sb_profile $P "matrix D1: sysbench oltp_read_only 32 x 28M (~220 GB, on disk), 48 threads ($(hostname))" oltp_read_only 32 28000000 48 uniform 300 300 ;;
  D3) dataset 32 28000000; sb_profile $P "matrix D3: sysbench oltp_read_only 32 x 28M (~220 GB, on disk), hot set, 1024 threads ($(hostname))" oltp_read_only 32 28000000 1024 pareto 300 300 ;;
  D4) dataset 32 28000000; sb_profile $P "matrix D4: sysbench oltp_write_only 32 x 28M (~220 GB, on disk), 8 threads ($(hostname))" oltp_write_only 32 28000000 8 uniform 300 300 ;;
  # atvbmmia (SATA): TPC-C in memory and on disk, TPC-H SF10
  C2) gotpc_prep tpcc gotpcc100 100; dataset 1 10000; tpcc_profile 100 256 ;;
  C4) gotpc_prep tpcc gotpcc1000 1000; dataset 1 10000; tpcc_profile 1000 1024 ;;
  D5) gotpc_prep tpch tpch10 10; dataset 1 10000
      sed -e "s/-D tpch --sf 1/-D tpch10 --sf 10/" -e "s/^name = .*/name = matrix D5: go-tpc TPC-H SF10, 22 queries, 1 stream, 600 s cap per query ($(hostname))/" \
          -e "s/max_execution_time=120000/max_execution_time=600000/" -e "s/^timeout = .*/timeout = 21600/" -e "/^capped_queries/d" /root/bench/tpch.ini > $P
      GOAL=latency ;;
  # canary-type cell on a pipeline box: read-only point lookups, hot set, low concurrency, latency goal (~60 GB)
  C6) dataset 16 16000000; sb_profile $P "matrix C6: sysbench oltp_point_select 16 x 16M (~60 GB), hot set, 8 threads, p95 latency ($(hostname))" oltp_point_select 16 16000000 8 pareto 300 300
      latency_goal $P ;;
  *) echo "QUEUEFAILED: unknown cell $CELL"; exit 1 ;;
esac
grep -q "^warmup = auto" $P || [ $CELL = D5 ] || auto_len $P
export AI_OFF=1
STUDY="${MATRIX_STUDY:-case-base matrix v2 (wave 1) 2026-10-26}" run_session $P "matrix $CELL ($(hostname))" $GOAL 16
echo PIPEOK
