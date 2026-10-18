#!/bin/bash
# tuner-db2 retry: CH-benCHmark with memory reserved for the co-located go-tpc client, then the compatibility matrix
. /root/bench/queue-common.sh
set -x
wait_idle; update_agent
export BASELINE_ARGS="--reserve-mem 1.5G"
systemctl start mysql 2>/dev/null
tuning-agent baseline --apply $BASELINE_ARGS
mysql -e "DROP DATABASE IF EXISTS ch"
step /root/bench/prep-ch.sh
mysql ch -e "ANALYZE TABLE customer, district, history, item, new_order, order_line, orders, stock, warehouse, nation, region, supplier" >/dev/null 2>&1
purge_binlogs; rm -rf /var/lib/mysql_base
run_session /root/bench/ch.ini "CH-benCHmark 10W (TPC-C + analytical stream)" tps 8
unset BASELINE_ARGS
for fv in percona:8.0 mysql:8.4 mysql:9.7 mariadb:10.11 mariadb:11.4 percona:8.4; do /root/bench/compat.sh ${fv%%:*} ${fv##*:}; done
echo QUEUEDONE
