#!/bin/bash
. /root/bench/queue-common.sh
set -x
tuning-agent rollback --reset
mysql -e "DROP DATABASE IF EXISTS sbtest; CREATE DATABASE sbtest"
sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-db=sbtest --tables=4 --table-size=1000000 --threads=4 prepare > /root/bench/prep-sb1g.log 2>&1 || { echo "QUEUEFAILED: prepare"; exit 1; }
STUDY="Method check: old vs new engine" run_session /root/bench/sb1g-uniform.ini "method check: sysbench 1 GB uniform, new engine (compare #7)" tps 12
echo QUEUEDONE
