#!/bin/bash
# Read-only validation on tuner-vbm-amd (EPYC 4245P, NVMe): no writes, no data reset - checks the evidence gate
# (flushing / redo / commit settings must be validation-only), read-path experiments, storage latency and stall
# detection without SSD write pressure. 1) I/O-bound oltp_read_only, 2) CPU-bound point_select (in memory).
. /root/bench/queue-common.sh
set -x
update_agent
D=/root/bench/amd-ro
mysql -e "CREATE DATABASE IF NOT EXISTS sbtest; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON sbtest.* TO sbtest@localhost"
n=$(mysql -N -e "select count(*) from information_schema.tables where table_schema='sbtest'")
if [ "$n" -lt 16 ]; then
  step sysbench oltp_read_only --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest \
    --tables=16 --table-size=10000000 --threads=16 prepare > $D/prepare.log 2>&1
fi
mysql -N -e "select round(sum(data_length+index_length)/1e9,1) from information_schema.tables where table_schema='sbtest'"
export BASELINE_ARGS="--reserve-mem 2G" SCOPE=knobs,os CALIBRATE=3 STUDY="EPYC 4245P: read-only validation"
run_session $D/sbro-io.ini "Read-only I/O-bound: sysbench oltp_read_only 37 GB, 64 threads (EPYC 4245P)" tps 14
run_session $D/sbps-mem.ini "Read-only CPU-bound: sysbench point_select 9 GB in memory, 64 threads (EPYC 4245P)" tps 12
echo QUEUEDONE
