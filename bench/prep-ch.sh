#!/bin/bash
# CH-benCHmark data (TPC-C 10W + CH tables); skip go-tpc's slow consistency checks
set -e
mysql -e "CREATE USER IF NOT EXISTS 'tpcc'@'localhost' IDENTIFIED BY 'tpcc'; CREATE USER IF NOT EXISTS 'tpcc'@'127.0.0.1' IDENTIFIED BY 'tpcc';
  GRANT ALL ON *.* TO 'tpcc'@'localhost'; GRANT ALL ON *.* TO 'tpcc'@'127.0.0.1'"
cd /opt/go-tpc
# go-tpc's ch prepare only adds the CH tables (nation, region, supplier, views): the TPC-C schema has to be loaded first
timeout 3h ./go-tpc tpcc prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses 10 -T 2 --no-check > /root/bench/prep-ch-tpcc.log 2>&1 || true
timeout 3h ./go-tpc ch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses 10 -T 2 > /root/bench/prep-ch.log 2>&1 || true
mysql -N -e "SELECT CONCAT(table_name,' ',table_rows) FROM information_schema.tables WHERE table_schema='ch'"
# go-tpc exits non-zero on its own checks; the data must be there though
[ "$(mysql -N -e "SELECT COUNT(*) FROM ch.warehouse" 2>/dev/null)" = 10 ] || { echo "CH prepare failed: see /root/bench/prep-ch.log"; exit 1; }
