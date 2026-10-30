#!/bin/bash
# matrix.sh <cell>: one cell of the case-base matrix v2 (docs/battery/matrix-v2.md, first wave 2026-10-26): an ordinary
# autotune session - Percona Server 8.4, rules + learnings + similarity (no AI), run length chosen automatically,
# production-safe first then trade-offs, 16 runs - whose runs become cases for the similarity ranking. Cells sharing a data
# set reuse its pristine copy.
CELL=$1
# matrix.sh <cell> sweep <variable> <values|auto-bp> [with]: a characterization sweep of the cell's workload instead of tuning
if [ "${2:-}" = sweep ]; then SWEEP_VAR=$3; SWEEP_VALUES=$4; SWEEP_WITH=${5:-}; fi
# matrix.sh <cell> confirm <session>: the cell's workload on this box, calibrated, then that session's best configurations
# against its start configuration in interleaved rounds (cross-host confirmation, tuning-agent confirm)
if [ "${2:-}" = confirm ]; then CONFIRM_SESSION=$3; fi
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
P=/root/bench/pipe-matrix-$CELL.ini
GOAL=tps
auto_len() {  # the run length from the run-length probe (warm-up and window), not fixed numbers
  sed -i -e 's/^warmup = .*/warmup = auto/' -e 's/^duration = .*/duration = auto/' -e 's/^timeout = .*/timeout = 14400/' $1
}
latency_goal() {  # latency goal: the server's p95 statement latency (performance_schema histogram, interpolated), lower is
  # better - sysbench's own p95 comes from coarse buckets (M6: 29.19 ms in six of eight runs), the server's does not
  sed -i '/^goal_metric = /d' $1
  GOAL=latency
  export LATENCY_STAT=p95
}
tpcc_profile() {  # tpcc_profile <warehouses> <threads>; goal: transactions/s measured by the server in the window (go-tpc's
  # tpmC covers the whole run, warm-up included, and its interval lines carry no time to cut the window from)
  cat > $P <<INI
[bench]
name = matrix $CELL: go-tpc TPC-C $1 W, $2 threads ($(hostname))
reset = /root/bench/reset_datadir.sh
run = ulimit -n 1048576; /opt/go-tpc/go-tpc tpcc run -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D gotpcc$1 --warehouses $1 -T $2 --time {total}s --interval 30s --ignore-error
warmup = auto
duration = auto
timeout = 14400

[metrics]
tpmC_whole_run = tpmC: ([\d.]+)

[requires]
max_connections = $(( $2 * 2 + 100 ))
max_prepared_stmt_count = 1048576
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
  # D5 cold passes (no warm-up script: mysqld restarted and caches dropped before each), D6 warm passes
  D5) gotpc_prep tpch tpch10 10; dataset 1 10000
      sed -e "s/-D tpch --sf 1/-D tpch10 --sf 10/" -e "s/^name = .*/name = matrix D5: go-tpc TPC-H SF10, 22 queries, 1 stream, 600 s cap per query ($(hostname))/" \
          -e "s/max_execution_time=120000/max_execution_time=600000/" -e "s/^timeout = .*/timeout = 21600/" -e "/^capped_queries/d" -e "/^reset = /d" /root/bench/tpch.ini > $P
      GOAL=latency ;;
  # canary-type cell on a pipeline box: read-only point lookups, hot set, low concurrency, latency goal (~60 GB)
  C6) dataset 16 16000000; sb_profile $P "matrix C6: sysbench oltp_point_select 16 x 16M (~60 GB), hot set, 8 threads, p95 latency ($(hostname))" oltp_point_select 16 16000000 8 pareto 300 300
      latency_goal $P ;;
  # wave 2, atvbmamd (NVMe, AMD EPYC): partly cached 16 x 16M (~60 GB), then in memory 8 x 4M, then TPC-H SF30
  P1) dataset 16 16000000; sb_profile $P "matrix P1: sysbench oltp_read_only 16 x 16M (~60 GB, partly cached), 256 threads ($(hostname))" oltp_read_only 16 16000000 256 uniform 300 300 ;;
  P5) dataset 16 16000000; sb_profile $P "matrix P5: sysbench oltp_update_index 16 x 16M (~60 GB, partly cached), 48 threads ($(hostname))" oltp_update_index 16 16000000 48 uniform 300 300 ;;
  P3) dataset 16 16000000; sb_profile $P "matrix P3: sysbench oltp_write_only 16 x 16M (~60 GB, partly cached), 1024 threads ($(hostname))" oltp_write_only 16 16000000 1024 uniform 300 300 ;;
  M2) dataset 8 4000000; sb_profile $P "matrix M2: sysbench oltp_read_only 8 x 4M (in memory), 48 threads ($(hostname))" oltp_read_only 8 4000000 48 uniform 300 300 ;;
  M1) dataset 8 4000000; sb_profile $P "matrix M1: sysbench oltp_point_select 8 x 4M (in memory), 1024 threads ($(hostname))" oltp_point_select 8 4000000 1024 uniform 300 300 ;;
  X1) dataset 8 4000000; sb_profile $P "matrix X1 (explore): sysbench oltp_read_write 8 x 4M (in memory), 256 threads ($(hostname))" oltp_read_write 8 4000000 256 uniform 300 300
      EXPLORE_CELL=1 ;;
  D6) gotpc_prep tpch tpch30 30; dataset 1 10000
      sed -e "s/-D tpch --sf 1/-D tpch30 --sf 30/" -e "s/^name = .*/name = matrix D6: go-tpc TPC-H SF30, 22 queries, 1 stream, warm passes, 900 s cap per query ($(hostname))/" \
          -e "s/max_execution_time=120000/max_execution_time=900000/" -e "s/^timeout = .*/timeout = 28800/" -e "/^capped_queries/d" -e "/^reset = /d" /root/bench/tpch.ini > $P
      printf 'passes = warm\nwarm_passes = 1\n' >> /tmp/matrix-d6-extra && sed -i "/^\[bench\]/r /tmp/matrix-d6-extra" $P && rm -f /tmp/matrix-d6-extra
      GOAL=latency ;;
  # tuner-canary (8 vCPU): HammerDB TPROC-C 200 W, 64 virtual users, NOPM (HammerDB reports it for the timed part only, so
  # its own ramp-up is the warm-up: split shape, fixed lengths). The schema is built once; session.sh snapshots the
  # pristine copy (sbtest and tpcc) for the resets
  H1) if ! mysql -N -e "SELECT 1 FROM tpcc.warehouse LIMIT 1" >/dev/null 2>&1; then
        mysql -e "CREATE USER IF NOT EXISTS 'tpcc'@'localhost' IDENTIFIED BY 'tpcc'; GRANT ALL ON tpcc.* TO 'tpcc'@'localhost'" || exit 1
        step bash -c "cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_WH=200 HDB_BUILD_VU=8 ./hammerdbcli auto /root/bench/hdb_build200.tcl > /root/bench/build200-h1.log 2>&1"
        mysql -N -e "SELECT COUNT(*) FROM tpcc.warehouse" | grep -qx 200 || { echo "QUEUEFAILED: TPROC-C build incomplete"; exit 1; }
        rm -rf /var/lib/mysql_base/*  # the pristine copy is taken again with the new schema
      fi
      cat > $P <<INI
[bench]
name = matrix H1: HammerDB TPROC-C 200 W, 64 VU, 5 min ramp-up + 10 min ($(hostname))
reset = /root/bench/reset_datadir.sh
run = cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_VU=64 HDB_RAMPUP=5 HDB_DURATION=10 ./hammerdbcli auto /root/bench/hdb_run.tcl
shape = split
warmup = 305
duration = 600
timeout = 1500
goal_metric = nopm

[metrics]
nopm = achieved (\d+) NOPM
tpm = from (\d+) MySQL TPM

[requires]
max_connections = 228
INI
      ;;
  *) echo "QUEUEFAILED: unknown cell $CELL"; exit 1 ;;
esac
grep -q "^warmup = auto" $P || [ $CELL = D5 ] || [ $CELL = D6 ] || [ $CELL = H1 ] || auto_len $P
# the explore cells use the AI once the rules run out (autotune --explore); the others run without AI
if [ -n "${EXPLORE_CELL:-}" ]; then export EXPLORE=1; else export AI_OFF=1; fi
if [ -n "${CONFIRM_SESSION:-}" ]; then
  export CONFIRM_SESSION POST=/root/bench/pipeline/items/confirm-post.sh
  STUDY="cross-host confirmations" run_session $P "confirm session #$CONFIRM_SESSION: matrix $CELL ($(hostname))" $GOAL 0
elif [ -n "${SWEEP_VAR:-}" ]; then
  # characterization sweep (user, 2026-10-29): the cell's workload, calibrated, then one setting across its whole range -
  # buffer pool from far too small to as large as memory allows, buffer pool instances from 1 - each value measured and
  # profiled, nothing tuned: response curves and contention profiles as evidence for the rules and the case base
  # auto-bp is expanded by sweep-post.sh once the calibration has set the tuned pool: the range runs up to that size
  # (70% of RAM stopped at 21G below a 23-25G tuned pool, and P1 / P4 were still rising there)
  export SWEEP_VAR SWEEP_VALUES SWEEP_WITH POST=/root/bench/pipeline/items/sweep-post.sh
  STUDY="characterization sweeps 2026-10-29" run_session $P "sweep $SWEEP_VAR: matrix $CELL ($(hostname))" $GOAL 0
else
  STUDY="${MATRIX_STUDY:-case-base matrix v2 2026-10-26}" run_session $P "matrix $CELL ($(hostname))" $GOAL 16
fi
echo PIPEOK
