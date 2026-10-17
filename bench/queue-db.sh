#!/bin/bash
# tuner-db: HammerDB 200W
. /root/bench/queue-common.sh
set -x
wait_idle; update_agent
tuning-agent rollback --reset
mysql -e "DROP DATABASE IF EXISTS tpcc"; rm -rf /var/lib/mysql_base; purge_binlogs
tuning-agent baseline --apply   # faster build; session.sh resets to defaults before measuring
cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient ./hammerdbcli auto /root/bench/hdb_build200.tcl > /root/bench/build200.log 2>&1
mysql tpcc -e "ANALYZE TABLE customer, district, history, item, new_order, order_line, orders, stock, warehouse" >/dev/null
purge_binlogs; df -h / | tail -1
run_session /root/bench/hammerdb200.ini "HammerDB TPROC-C 200W, 10 VU (I/O-bound)" tps 8
echo QUEUEDONE
