#!/bin/bash
# survey.sh <bench...>: load a small data set per benchmark, then measure page and dump compressibility
O=/root/survey; mkdir -p $O; cd /root
mysql -e "SET GLOBAL innodb_max_dirty_pages_pct = 0; SET GLOBAL innodb_max_dirty_pages_pct_lwm = 0"
log() { echo "$(date -u +%T) $*" | tee -a $O/log; }
measure() {  # measure <name> <schema>
  for i in $(seq 1 120); do [ "$(mysql -N -e "show global status like 'Innodb_buffer_pool_pages_dirty'" | cut -f2)" -lt 50 ] && break; sleep 5; done
  local raw cmp
  raw=$(mysqldump --single-transaction --no-tablespaces $2 2>/dev/null | tee >(lz4 -1 -c | wc -c > $O/$1.dumpc) | wc -c); sleep 1
  cmp=$(cat $O/$1.dumpc); r=$(python3 -c "print(round($raw/$cmp,2) if $cmp else '')")
  python3 /root/pagecomp.py $1 /var/lib/mysql/$2 $r | tee -a $O/results.jsonl
}
GT=$(ls /opt/go-tpc/bin/go-tpc /opt/go-tpc/go-tpc 2>/dev/null | head -1)
for b in "$@"; do
  log "load $b"
  case $b in
    hammerdb) (cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_WH=20 HDB_BUILD_VU=8 ./hammerdbcli auto /root/bench/hdb_build200.tcl) > $O/hammerdb.log 2>&1; measure hammerdb-tpcc tpcc ;;
    gotpcc) mysql -e "create database if not exists gotpcc"; $GT tpcc prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D gotpcc --warehouses 20 -T 8 > $O/gotpcc.log 2>&1; measure go-tpc-tpcc gotpcc ;;
    gotpch) mysql -e "create database if not exists gotpch"; $GT tpch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D gotpch --sf 1 -T 8 > $O/gotpch.log 2>&1; measure go-tpc-tpch gotpch ;;
    sysbench) mysql -e "create database if not exists sbtest"; sysbench oltp_read_write --mysql-host=127.0.0.1 --mysql-user=bench --mysql-password=bench --tables=8 --table-size=1000000 --threads=8 prepare > $O/sysbench.log 2>&1; measure sysbench sbtest ;;
    employees) [ -d test_db ] || git clone -q https://github.com/datacharmer/test_db.git; (cd test_db && mysql < employees.sql) > $O/employees.log 2>&1; measure employees employees ;;
    imdb) bash /root/bench/job/load.sh /root/job/csv /root/job/jobq > $O/imdb.log 2>&1; measure imdb-job imdb ;;
    bb-*) w=${b#bb-}; bash /root/bb_small.sh $w > $O/$b.log 2>&1; db=$(grep -o "db=[a-z_0-9]*" $O/$b.log | tail -1 | cut -d= -f2); [ -n "$db" ] && measure $b $db || log "FAILED $b" ;;
  esac
  log "done $b"
done
log SURVEYDONE
