#!/bin/bash
# Battery 2 contention tests on tuner-big (16 vCPU, 32 GB) with the client on tuner-bigcli (8 vCPU, same DC):
# which rules behave differently at higher core counts and 64 / 256 / 1024 connections (thread pool, spin, LWT, ...)
. /root/bench/queue-common.sh
set -x
DB=173.230.136.182; CLI=50.116.46.101
SSHC="ssh -tt -i /root/.ssh/bench_key -o BatchMode=yes root@$CLI"
STUDY="Battery 2: contention (16 vCPU, remote client)"
update_agent
# the finished 2000W study: drop its data, keep its pristine copy aside (no deletion)
systemctl stop mysql
[ -d /data/mysql_base ] && [ "$(ls -A /data/mysql_base)" ] && mv /data/mysql_base /data/old2000w_base
mkdir -p /data/mysql_base
systemctl start mysql || { echo "QUEUEFAILED: mysql start"; exit 1; }
mysql -e "DROP DATABASE IF EXISTS tpcc; CREATE DATABASE IF NOT EXISTS sbtest;
  CREATE USER IF NOT EXISTS sbtest@'%' IDENTIFIED BY 'sbtest'; GRANT ALL ON *.* TO sbtest@'%';
  CREATE USER IF NOT EXISTS tpcc@'%' IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@'%'; SET GLOBAL max_connections = 1100"
scp -i /root/.ssh/bench_key -o BatchMode=yes /root/bench/connstorm.lua root@$CLI:/root/connstorm.lua
SB="sysbench --mysql-host=$DB --mysql-user=sbtest --mysql-password=sbtest --mysql-db=sbtest --tables=16 --table-size=1000000"
$SSHC "ulimit -n 65535; $SB --threads=16 oltp_read_write prepare" > /root/bench/prep-sb.log 2>&1 || { echo "QUEUEFAILED: sysbench prepare"; exit 1; }
$SSHC "mysql --get-server-public-key -h $DB -utpcc -ptpcc -e 'SELECT 1' >/dev/null 2>&1; cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_HOST=$DB HDB_WH=100 HDB_BUILD_VU=16 ./hammerdbcli auto /root/bench/hdb_build200.tcl" > /root/bench/prep-hdb.log 2>&1
[ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = 100 ] || { echo "QUEUEFAILED: HammerDB build"; exit 1; }
mysql -e "SELECT table_schema, ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables GROUP BY 1"
M="[metrics]\nsysbench_tps = transactions:\\\\s+\\\\d+\\\\s+\\\\(([\\\\d.]+) per sec\nsysbench_qps = queries:\\\\s+\\\\d+\\\\s+\\\\(([\\\\d.]+) per sec\nsysbench_p95_ms = 95th percentile:\\\\s+([\\\\d.]+)\n"
prof() {  # id name client-command goal_metric requires
  printf "[bench]\nname = %s\nreset = /root/bench/reset_datadir.sh\nrun = %s\nwarmup = 120\nduration = 300\ntimeout = 1200\ngoal_metric = %s\n\n$M\n[requires]\nmax_connections = 1100\nmax_prepared_stmt_count = 1048576\n" \
    "$2" "$SSHC \"ulimit -n 65535; $3\"" "$4" > /root/bench/contention-$1.ini
}
prof c1 "sysbench oltp_read_write in-memory 16x1M, 64 connections from tuner-bigcli" "$SB --threads=64 --time={total} --report-interval=10 --mysql-ignore-errors=all oltp_read_write run" sysbench_tps
prof c2 "sysbench oltp_read_write in-memory 16x1M, 1024 connections (plain statements) from tuner-bigcli" "$SB --threads=1024 --db-ps-mode=disable --time={total} --report-interval=10 --mysql-ignore-errors=all oltp_read_write run" sysbench_tps
prof c3 "sysbench oltp_point_select 16x1M, 1024 connections (plain statements) from tuner-bigcli" "$SB --threads=1024 --db-ps-mode=disable --time={total} --report-interval=10 --mysql-ignore-errors=all oltp_point_select run" sysbench_qps
prof c4 "sysbench oltp_update_non_index hot rows (pareto h=0.1), 256 connections from tuner-bigcli" "$SB --threads=256 --rand-type=pareto --rand-pareto-h=0.1 --time={total} --report-interval=10 --mysql-ignore-errors=all oltp_update_non_index run" sysbench_tps
prof c6 "connection storm: new connection + 5 point selects per event, 64 threads from tuner-bigcli" "sysbench /root/connstorm.lua --threads=64 --mysql-host=$DB --mysql-user=sbtest --mysql-password=sbtest --mysql-db=sbtest --tables=16 --table-size=1000000 --time={total} --report-interval=10 --mysql-ignore-errors=all run" sysbench_tps
printf "[bench]\nname = HammerDB TPROC-C 100W, 256 VU from tuner-bigcli, 2 + 5 min\nreset = /root/bench/reset_datadir.sh\nrun = %s\nwarmup = 125\nduration = 300\ntimeout = 1500\ngoal_metric = nopm\n\n[metrics]\nnopm = achieved (\\\\d+) NOPM\ntpm = from (\\\\d+) MySQL TPM\n\n[requires]\nmax_connections = 1100\n" \
  "$SSHC \"mysql --get-server-public-key -h $DB -utpcc -ptpcc -e 'SELECT 1' >/dev/null 2>&1; cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_HOST=$DB HDB_VU=256 HDB_RAMPUP=2 HDB_DURATION={duration_min} ./hammerdbcli auto /root/bench/hdb_run.tcl\"" > /root/bench/contention-c5.ini
scp -i /root/.ssh/bench_key -o BatchMode=yes /root/bench/hdb_run.tcl root@$CLI:/root/bench/hdb_run.tcl
for c in c1 c2 c3 c4 c5 c6; do
  SCOPE=knobs,os STUDY="$STUDY" run_session /root/bench/contention-$c.ini "Battery 2: $(echo $c | tr a-z A-Z) $(sed -n 's/^name = //p' /root/bench/contention-$c.ini | cut -c1-80)" tps 16
done
echo QUEUEDONE
