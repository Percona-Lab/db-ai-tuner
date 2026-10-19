#!/bin/bash
# load_all.sh [workload...]: load the BenchBase data sets into the live datadir (default: all six), redo durability relaxed
# during the load only (runtime SET GLOBAL; the next restart restores the configured value).
# Then move any old pristine copy aside (never deleted) so session.sh snapshots the new data set into /var/lib/mysql_base:
#   systemctl stop mysql; mv /var/lib/mysql_base /root/old-<what>-base; mkdir /var/lib/mysql_base; systemctl start mysql
# Sizes on atvbm (E-2286G, 32 GB, Percona Server 9.7, 12 loader threads):
#   tatp sf10 0.5 GB 1 min | smallbank sf1 0.2 GB 6 s | twitter sf200 0.9 GB 13 s | wikipedia sf100 25 GB 12 min
#   ycsba sf60000 (60M rows) / ycsbc sf100000 (100M rows): see docs/research/benchmark-benchbase.md
D=/root/bench/benchbase
mysql -e "SET GLOBAL innodb_flush_log_at_trx_commit = 0"
for w in ${@:-tatp smallbank twitter wikipedia ycsba ycsbc}; do
  $D/bb_load.sh $w 12 > $D/load-$w.log 2>&1; tail -1 $D/load-$w.log
done
mysql -e "SET GLOBAL innodb_flush_log_at_trx_commit = 1"
mysql -e "SELECT table_schema, ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables WHERE table_schema LIKE 'bb\_%' GROUP BY 1"
echo LOADALLDONE
