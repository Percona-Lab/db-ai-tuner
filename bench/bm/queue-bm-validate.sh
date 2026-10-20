#!/bin/bash
# Short validation of the current engine on bare metal (read-only, in memory): clean restart per run, CPU / off-CPU
# profiles, block-latency tails, evidence gate. Usage: queue-bm-validate.sh <study>
. /root/bench/queue-common.sh
set -x
D=/root/bench/bm
mysql -e "CREATE DATABASE IF NOT EXISTS sbtest; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON sbtest.* TO sbtest@localhost"
[ "$(mysql -N -e "select count(*) from information_schema.tables where table_schema='sbtest'")" -ge 16 ] || \
  step sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest --tables=16 --table-size=1000000 --threads=16 prepare
export SCOPE=knobs,os STUDY="$1" CALIBRATE=3 BASELINE_ARGS="--reserve-mem 2G"
run_session $D/sbps-short.ini "Bare-metal validation: point select in memory, 64 threads ($(hostname))" tps 8
echo QUEUEDONE
