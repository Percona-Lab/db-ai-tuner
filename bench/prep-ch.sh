#!/bin/bash
# CH-benCHmark data (TPC-C 10W + CH tables); skip go-tpc's slow consistency checks
set -e
cd /opt/go-tpc
timeout 3h ./go-tpc ch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses 10 -T 2 > /root/bench/prep-ch.log 2>&1 || true
mysql -N -e "SELECT CONCAT(table_name,' ',table_rows) FROM information_schema.tables WHERE table_schema='ch'"
