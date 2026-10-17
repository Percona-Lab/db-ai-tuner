#!/bin/bash
# tuner-db2: sysbench 1 GB uniform + special, then CH-benCHmark
. /root/bench/queue-common.sh
set -x
wait_idle; update_agent
apt-get install -yq sysbench >/dev/null
tuning-agent baseline --apply
/root/bench/prep-sysbench.sh 4 1000000
run_session /root/bench/sb1g-uniform.ini "sysbench 1 GB in-memory, uniform" tps 8
run_session /root/bench/sb1g-special.ini "sysbench 1 GB in-memory, special (skewed)" tps 8
tuning-agent baseline --apply
/root/bench/prep-ch.sh
mysql ch -e "ANALYZE TABLE customer, district, history, item, new_order, order_line, orders, stock, warehouse, nation, region, supplier" >/dev/null 2>&1
purge_binlogs; rm -rf /var/lib/mysql_base
run_session /root/bench/ch.ini "CH-benCHmark 10W (TPC-C + analytical stream)" tps 8
echo QUEUEDONE
