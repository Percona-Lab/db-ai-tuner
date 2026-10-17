#!/bin/bash
set -x
mysql -e "CREATE USER IF NOT EXISTS tpcc@'localhost' IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@'localhost';
          CREATE USER IF NOT EXISTS tpcc@'127.0.0.1' IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@'127.0.0.1';"
cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient ./hammerdbcli auto /root/bench/build.tcl > /root/bench/build-hdb.log 2>&1
cd /opt/go-tpc
./go-tpc tpcc prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D gotpcc --warehouses 10 -T 4 > /root/bench/build-gotpcc.log 2>&1
./go-tpc tpch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D tpch --sf 1 -T 4 --analyze > /root/bench/build-tpch.log 2>&1
mysql -e "SELECT table_schema, ROUND(SUM(data_length+index_length)/1048576) mb FROM information_schema.tables GROUP BY 1"
echo ALLDONE
