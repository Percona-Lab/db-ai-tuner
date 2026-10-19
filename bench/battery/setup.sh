#!/bin/bash
# Battery node setup: setup.sh <TEST> <agent name> <api key> <study>
# Percona Server 9.7, binary log off, benchmark client on the same machine; prepares the test's data set, writes the
# benchmark profile /root/bench/battery.ini and starts the tuning session.
set -x
T=$1; NAME=$2; KEY=$3; STUDY=$4
export DEBIAN_FRONTEND=noninteractive
hostnamectl set-hostname $NAME
systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer || true
apt-get update -q
apt-get install -yq curl gnupg lsb-release python3-venv sysstat fio rsync sysbench libjemalloc2 libtcmalloc-minimal4 libmimalloc2.0 libtcl8.6 \
  linux-tools-common linux-tools-$(uname -r) || true
curl -sO https://repo.percona.com/apt/percona-release_latest.generic_all.deb && apt-get install -yq ./percona-release_latest.generic_all.deb
percona-release enable-only ps-97-lts release && apt-get update -q
debconf-set-selections <<< "percona-server-server percona-server-server/root-pass password"
debconf-set-selections <<< "percona-server-server percona-server-server/re-root-pass password"
apt-get install -yq percona-server-server || { echo SETUPFAILED; exit 1; }
{ echo "[mysqld]"; echo "# battery: binary log disabled"; echo "disable_log_bin"
  [ "$T" = O3 ] && echo "# battery O3: 1024 client connections must be possible even with server defaults" && echo "max_connections = 1100"
} > /etc/mysql/mysql.conf.d/lab.cnf
# many connections: raise limits for mysqld and the client
mkdir -p /etc/systemd/system/mysql.service.d && printf '[Service]\nLimitNOFILE=65535\n' > /etc/systemd/system/mysql.service.d/limits.conf
systemctl daemon-reload; systemctl restart mysql
mysql -e "CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON *.* TO sbtest@localhost;
  CREATE USER IF NOT EXISTS tpcc@localhost IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@localhost;
  CREATE USER IF NOT EXISTS tpcc@'127.0.0.1' IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@'127.0.0.1';"
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash -s -- --api-key $KEY --name $NAME || { echo SETUPFAILED; exit 1; }
tuning-agent baseline --apply $([ "$T" = O3 ] && echo "--max-connections 1100")
SB="sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-db=sbtest"
GT="/opt/go-tpc/go-tpc"; HDB="cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp"
P=/root/bench/battery.ini; RESET="reset = /root/bench/reset_datadir.sh"; MAX=20; GOAL=tps
tuning-agent activity "loading data" "battery $T: preparing the benchmark data set"
case $T in
  O1|O2|O3) TH=$(case $T in O1) echo 4;; O2) echo 64;; O3) echo 1024;; esac)
    mysql -e "CREATE DATABASE IF NOT EXISTS sbtest"; $SB --tables=16 --table-size=1500000 --threads=8 prepare > /root/bench/prep.log 2>&1
    printf '[bench]\nname = sysbench oltp_read_write in memory (16 x 1.5M rows, 5.6 GB), %s threads, 2 + 5 min\n%s\nrun = ulimit -n 65535; %s --tables=16 --table-size=1500000 --threads=%s --time=450 --report-interval=10 --mysql-ignore-errors=all run\nwarmup = 120\nduration = 300\ntimeout = 900\n\n[metrics]\nsysbench_tps = transactions:\\s+\\d+\\s+\\(([\\d.]+) per sec\nsysbench_p95_ms = 95th percentile:\\s+([\\d.]+)\n' "$TH" "$RESET" "$SB" "$TH" > $P ;;
  O4) mysql -e "CREATE DATABASE IF NOT EXISTS sbtest"; $SB --tables=32 --table-size=10000000 --threads=8 prepare > /root/bench/prep.log 2>&1
    printf '[bench]\nname = sysbench oltp_read_write I/O-bound (32 x 10M rows, ~75 GB), 64 threads, 5 + 10 min\n%s\nrun = %s --tables=32 --table-size=10000000 --threads=64 --time=930 --report-interval=10 --mysql-ignore-errors=all run\nwarmup = 300\nduration = 600\ntimeout = 1800\n\n[metrics]\nsysbench_tps = transactions:\\s+\\d+\\s+\\(([\\d.]+) per sec\nsysbench_p95_ms = 95th percentile:\\s+([\\d.]+)\n' "$RESET" "$SB" > $P; MAX=14 ;;
  O5|O6) WH=$([ $T = O5 ] && echo 80 || echo 800); RU=$([ $T = O5 ] && echo 2 || echo 5); DU=$([ $T = O5 ] && echo 5 || echo 10)
    (eval "$HDB HDB_WH=$WH HDB_BUILD_VU=8 ./hammerdbcli auto /root/bench/hdb_build200.tcl") > /root/bench/prep.log 2>&1
    printf '[bench]\nname = HammerDB TPROC-C %sW, 64 VU, %s + %s min\n%s\nrun = %s HDB_VU=64 HDB_RAMPUP=%s HDB_DURATION=%s ./hammerdbcli auto /root/bench/hdb_run.tcl\nwarmup = %s\nduration = %s\ntimeout = %s\ngoal_metric = nopm\n\n[metrics]\nnopm = achieved (\\d+) NOPM\ntpm = from (\\d+) MySQL TPM\n' \
      $WH $RU $DU "$RESET" "$HDB" $RU $DU $((RU * 60 + 5)) $((DU * 60)) $(( (RU + DU) * 60 + 900 )) > $P; [ $T = O6 ] && MAX=14 ;;
  A1|A2) SF=$([ $T = A1 ] && echo 1 || echo 10); CAP=$([ $T = A1 ] && echo 120000 || echo 300000)
    $GT tpch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D tpch --sf $SF -T 8 --analyze > /root/bench/prep.log 2>&1
    printf '[bench]\nname = go-tpc TPC-H SF%s, 22 queries, 1 stream, %s s cap per query\nreset = /root/bench/tpch_warm.sh\nrun = %s tpch run -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D tpch --sf %s --count 22 --enable-query-tuning=false --conn-params max_execution_time=%s --ignore-error\nwarmup = 0\nduration = 0\ntimeout = %s\ngoal_metric = pass_seconds\n\n[metrics]\npass_seconds = sum:\\[Summary\\] Q\\d+: ([\\d.]+)s\ngeomean_seconds = geomean:\\[Summary\\] Q\\d+: ([\\d.]+)s\n' \
      $SF $((CAP / 1000)) "$GT" $SF $CAP $((CAP / 1000 * 22 + 600)) > $P; GOAL=latency; MAX=$([ $T = A1 ] && echo 16 || echo 10) ;;
  M1|M2) WH=$([ $T = M1 ] && echo 50 || echo 500); WU=$([ $T = M1 ] && echo 120 || echo 300); DU=$([ $T = M1 ] && echo 300 || echo 600)
    $GT tpcc prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses $WH -T 8 --no-check > /root/bench/prep.log 2>&1
    $GT ch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses $WH -T 8 >> /root/bench/prep.log 2>&1
    printf '[bench]\nname = CH-benCHmark %sW: 32 TPC-C threads + 1 analytical stream, %s + %s s\n%s\nrun = %s ch run -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses %s -T 32 -t 1 --time %ss --interval 30s --conn-params max_execution_time=120000 --ap-conn-params max_execution_time=120000 --ignore-error\nwarmup = %s\nduration = %s\ntimeout = %s\n\n[metrics]\ntpmc_whole_run = tpmC: ([\\d.]+)\nqphh_whole_run = QphH: ([\\d.]+)\n' \
      $WH $WU $DU "$RESET" "$GT" $WH $((WU + DU + 20)) $WU $DU $((WU + DU + 900)) > $P; [ $T = M2 ] && MAX=14 ;;
esac
grep -q "^run" $P || { echo SETUPFAILED: no profile; exit 1; }
mysql -e "SELECT table_schema, ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables GROUP BY 1"
cd /root/bench
cat > queue.sh <<Q
#!/bin/bash
. /root/bench/queue-common.sh
set -x
$([ "$T" = O3 ] && echo 'export BASELINE_ARGS="--max-connections 1100"')
SCOPE=knobs,os STUDY="$STUDY" run_session /root/bench/battery.ini "$STUDY: $T $(sed -n 's/^name = //p' $P | cut -c1-70)" $GOAL $MAX
echo QUEUEDONE
Q
chmod +x queue.sh; (setsid nohup ./queue.sh > queue.log 2>&1 < /dev/null &)
echo SETUPDONE
