#!/bin/bash
# h2h.sh <W1|W2|W3|W4> <rules|planner>: head-to-head of the AI planner (Opus 5.5, no reviewer) against the rule engine on
# the same workload and hardware (2026-10-23; rerun 2026-10-25 with the rules arm without AI). Each workload runs once per arm on both boxes of a pair, in opposite order
# (crossover), so box and order effects cancel. Same agent build, profile, run cap and noise handling; only the choice of
# the next step differs. Percona Server 8.4, benchmark policy (production-safe first, then trade-offs), 16 runs.
W=$1; ARM=$2; RERUN=${3:-}   # rerun: the same arm with the current engine, in its own study (2026-10-26)
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
P=/root/bench/pipe-h2h-$W.ini
PRE=""
case $W in
  W1) dataset 16 2000000   # commit-bound writes: in memory, binary log on, fsync-bound on SATA
      sb_profile $P "h2h W1 commit-bound: sysbench oltp_update_index 16 x 2M (in memory), 256 threads, binlog on ($(hostname))" oltp_update_index 16 2000000 256 uniform 300 600 ;;
  W2) dataset 16 10000000  # read I/O-bound: ~37 GB on 31 GB RAM
      sb_profile $P "h2h W2 read-I/O-bound: sysbench oltp_read_write 16 x 10M (~37 GB, > RAM), 64 threads, uniform ($(hostname))" oltp_read_write 16 10000000 64 uniform 900 600 ;;
  W3) dataset 16 1000000   # CPU-bound high concurrency: in memory, performance_schema-heavy
      sb_profile $P "h2h W3 CPU-bound: sysbench oltp_read_write 16 x 1M (in memory), 512 threads, binlog on ($(hostname))" oltp_read_write 16 1000000 512 special 300 600 ;;
  W4) dataset 16 5400000   # the standard benchmark: 1024 connections, binary log off (purge cannot keep up at defaults)
      sb_profile $P "h2h W4 standard: sysbench oltp_read_write ~20 GB (16 x 5.4M), 1024 connections, binlog off ($(hostname))" oltp_read_write 16 5400000 1024 special 600 600
      PRE="disable_log_bin=ON" ;;
  *) echo "QUEUEFAILED: unknown workload $W"; exit 1 ;;
esac
# planner arm: rules + learnings + the AI planner; rules arm: rules + learnings, no AI at all (2026-10-25 rerun, choice B)
[ $ARM = planner ] && export PLANNER=1
[ $ARM = rules ] && export AI_OFF=1
[ -n "$RERUN" ] && H2H_STUDY="head-to-head rerun: rules + learnings + similarity (engine of 2026-10-26)"
STUDY="${H2H_STUDY:-head-to-head: planner vs rules + learnings 2026-10-25}" PRE_APPLY="$PRE" run_session $P "h2h $W $ARM${RERUN:+ rerun} ($(hostname))" tps 16
echo PIPEOK
