#!/bin/bash
# std-bench.sh <percona|mysql|mariadb> <series>: the standard cross-variant benchmark (requested 2026-10-22) - sysbench
# oltp_read_write, ~20 GB (16 x 5.4M rows), default (special) distribution, 1024 connections, binary log off, 10 min warm-up
# + 10 min measured, 25 tuning runs. Same hardware for every variant / release series (E-2286G, SATA).
F=$1; V=$2
. /root/bench/pipeline/lib.sh
set -x
if [ "$F $V" = "percona 8.4" ]; then ensure_ps84; else step bash /root/bench/pipeline/install_variant.sh $F $V; fi
dataset 16 5400000
P=/root/bench/pipe-std.ini
sb_profile $P "sysbench oltp_read_write ~20 GB (16 x 5.4M), special, 1024 connections, binlog off, 10 + 10 min ($F $V, E-2286G, SATA)" oltp_read_write 16 5400000 1024 special 600 600
# MariaDB: binary log off by default (and no disable_log_bin option); MySQL / Percona: on by default
PRE=""; [ "$F" != mariadb ] && PRE="disable_log_bin=ON"
STUDY="pipeline: standard benchmark x variants $(date -u +%F)" PRE_APPLY="$PRE" run_session $P "std oltp_rw 20GB 1024c $F $V ($(hostname))" tps 25
echo PIPEOK
