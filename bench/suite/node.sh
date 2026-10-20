#!/bin/bash
# Suite node setup: node.sh <suite version> <cell> <agent name> <api key> <study> [client ip]
# Percona Server 9.7 with its defaults (binary log ON, full durability), the benchmark data set of the cell, the
# benchmark profile /root/bench/suite.ini and the tuning session (queue.sh). Cells: docs/battery/suite-v1.md.
# With a client ip (class L cells) the benchmark runs on the client host over the network.
set -x
V=$1; CELL=$2; NAME=$3; KEY=$4; STUDY=$5; CLI=$6
export DEBIAN_FRONTEND=noninteractive
B=/root/bench; P=$B/suite.ini
hostnamectl set-hostname $NAME
systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer || true
apt-get update -q
apt-get install -yq curl gnupg lsb-release python3-venv sysstat fio rsync sysbench libjemalloc2 libtcmalloc-minimal4 libtcl8.6 bpftrace \
  linux-tools-common linux-tools-$(uname -r) || true
curl -sO https://repo.percona.com/apt/percona-release_latest.generic_all.deb && apt-get install -yq ./percona-release_latest.generic_all.deb
percona-release enable-only ps-97-lts release && apt-get update -q
debconf-set-selections <<< "percona-server-server percona-server-server/root-pass password"
debconf-set-selections <<< "percona-server-server percona-server-server/re-root-pass password"
apt-get install -yq percona-server-server || { echo SETUPFAILED; exit 1; }
# many connections: limits for mysqld and the client (4096-connection cells)
mkdir -p /etc/systemd/system/mysql.service.d && printf '[Service]\nLimitNOFILE=1048576\n' > /etc/systemd/system/mysql.service.d/limits.conf
printf '[mysqld]\n# suite: listen on all interfaces for a separate client host; everything else is the server default\nbind_address = 0.0.0.0\n' > /etc/mysql/mysql.conf.d/suite.cnf
systemctl daemon-reload; systemctl restart mysql
HOSTS="localhost"; [ -n "$CLI" ] && HOSTS="localhost %"
for h in $HOSTS; do
  mysql -e "CREATE USER IF NOT EXISTS sbtest@'$h' IDENTIFIED WITH caching_sha2_password BY 'sbtest'; GRANT ALL ON *.* TO sbtest@'$h';
            CREATE USER IF NOT EXISTS tpcc@'$h' IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@'$h';"
done
mysql -e "CREATE USER IF NOT EXISTS tpcc@'127.0.0.1' IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@'127.0.0.1'"
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash -s -- --api-key $KEY --name $NAME || { echo SETUPFAILED; exit 1; }

# ---------------------------------------------------------------------------------------------------- cell data + profile
DB=$(hostname -I | awk '{print $1}')
if [ -n "$CLI" ]; then
  SSHC="ssh -i /root/.ssh/bench_key -o BatchMode=yes -o StrictHostKeyChecking=accept-new root@$CLI"
  SBC="sysbench --mysql-host=$DB --mysql-user=sbtest --mysql-password=sbtest --mysql-db=sbtest"
  wrap() { echo "$SSHC \"ulimit -n 1048576; $1\""; }
else
  SBC="sysbench --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest"
  wrap() { echo "ulimit -n 65535; $1"; }
fi
HDB="cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp"; GT=/opt/go-tpc/go-tpc
RESET="reset = $B/reset_datadir.sh"
SBM='[metrics]\nsysbench_tps = transactions:\\s+\\d+\\s+\\(([\\d.]+) per sec\nsysbench_qps = queries:\\s+\\d+\\s+\\(([\\d.]+) per sec\nsysbench_p95_ms = 95th percentile:\\s+([\\d.]+)\n'
GOAL=tps; MAX=16; BASELINE_ARGS=""; REQ=""
sb_prepare() {  # tables rows
  mysql -e "CREATE DATABASE IF NOT EXISTS sbtest"
  eval "$(wrap "$SBC --tables=$1 --table-size=$2 --threads=16 oltp_read_write prepare")" > $B/prep.log 2>&1 || { echo SETUPFAILED: prepare; exit 1; }
}
sb_profile() {  # name test tables rows threads extra warmup duration reset-line
  local run; run=$(wrap "$SBC --tables=$3 --table-size=$4 --threads=$5 $6 --time={total} --report-interval=10 --mysql-ignore-errors=all $2 run")
  printf "[bench]\nname = %s\n%s\nrun = %s\nwarmup = %s\nduration = %s\ntimeout = %s\n\n$SBM" "$1" "$9" "$run" "$7" "$8" $(( $7 + $8 + 1200 )) > $P
}
tuning-agent activity "loading data" "suite $V cell $CELL: building the data set"
case $CELL in
  RO-1) sb_prepare 16 1000000
    sb_profile "RO-1 sysbench oltp_point_select, 16 x 1M rows (~4 GB, in memory), 64 threads" oltp_point_select 16 1000000 64 "--rand-type=uniform" 180 600 "data_state = read-only"
    REQ="max_prepared_stmt_count = 1048576" ;;
  RO-2) sb_prepare 32 6000000
    sb_profile "RO-2 sysbench oltp_read_only, 32 x 6M rows (~46 GB, ~3x RAM), uniform, 64 threads" oltp_read_only 32 6000000 64 "--rand-type=uniform" 600 600 "data_state = read-only"
    REQ="max_prepared_stmt_count = 1048576" ;;
  RO-4) sb_prepare 64 10000000
    sb_profile "RO-4 sysbench oltp_read_only, 64 x 10M rows (~150 GB, ~10x RAM), pareto hot set, 64 threads" oltp_read_only 64 10000000 64 "--rand-type=pareto" 600 600 "data_state = read-only"
    REQ="max_prepared_stmt_count = 1048576" ;;
  RW-1) sb_prepare 16 1000000
    sb_profile "RW-1 sysbench oltp_read_write, 16 x 1M rows (in memory), 16 threads, binary log on" oltp_read_write 16 1000000 16 "" 300 600 "$RESET" ;;
  RW-2) sb_prepare 32 6000000
    sb_profile "RW-2 sysbench oltp_read_write, 32 x 6M rows (~46 GB, ~3x RAM), 64 threads" oltp_read_write 32 6000000 64 "" 900 600 "$RESET"
    REQ="max_prepared_stmt_count = 1048576" ;;
  WI-3) sb_prepare 16 1000000
    sb_profile "WI-3 sysbench oltp_update_non_index, 16 x 1M rows (in memory), uniform, 64 threads: write-intensive in memory" oltp_update_non_index 16 1000000 64 "--rand-type=uniform" 600 600 "$RESET" ;;
  RW-3) (eval "$HDB HDB_WH=80 HDB_BUILD_VU=8 ./hammerdbcli auto $B/hdb_build200.tcl") > $B/prep.log 2>&1
    printf '[bench]\nname = RW-3 HammerDB TPROC-C 80W, 16 VU: tail latency (NEW_ORDER P99)\n%s\nrun = rm -f /tmp/hdbxtprofile.log; %s HDB_VU=16 HDB_RAMPUP=5 HDB_DURATION={duration_min} ./hammerdbcli auto %s/hdb_run.tcl; awk %s/SUMMARY OF/ {s = ""} {s = s $0 "\\n"} END {printf "%%s", s}%s /tmp/hdbxtprofile.log\nwarmup = 305\nduration = 600\ntimeout = 2400\ngoal_metric = neword_p99_ms\n\n[metrics]\nneword_p99_ms = PROC: NEWORD\\s+CALLS:[^\\n]*\\s+P99: ([\\d.]+)ms\nnopm = achieved (\\d+) NOPM\n' \
      "$RESET" "$HDB" $B "'" "'" > $P; GOAL=latency ;;
  RW-4) (eval "$HDB HDB_WH=800 HDB_BUILD_VU=8 ./hammerdbcli auto $B/hdb_build200.tcl") > $B/prep.log 2>&1
    printf '[bench]\nname = RW-4 HammerDB TPROC-C 800W all-warehouse (~5x RAM), 64 VU\n%s\nrun = %s HDB_VU=64 HDB_ALLWH=true HDB_RAMPUP=10 HDB_DURATION={duration_min} ./hammerdbcli auto %s/hdb_run.tcl\nwarmup = 605\nduration = 600\ntimeout = 3000\ngoal_metric = nopm\n\n[metrics]\nnopm = achieved (\\d+) NOPM\ntpm = from (\\d+) MySQL TPM\n' "$RESET" "$HDB" $B > $P; MAX=14 ;;
  WI-1) $B/ibench/install.sh > $B/prep.log 2>&1
    IBENCH_CLIENTS=8 IBENCH_ROWS=25000000 $B/ibench/load.sh >> $B/prep.log 2>&1; grep -q LOADDONE $B/prep.log || { echo SETUPFAILED: ibench load; exit 1; }
    printf '[bench]\nname = WI-1 Insert Benchmark l.i1 steady: 8 clients x 8 tables (25M rows, 3 secondary indexes, ~30 GB, ~2x RAM), 12 min warm-up + 5 min\n%s\nrun = %s/ibench/ibench_run.sh write 8 {warmup} {duration} 50\nwarmup = 720\nduration = 300\ntimeout = 2400\ngoal_metric = ibench_inserts_per_s\nadaptive_duration = off\n\n[metrics]\nibench_inserts_per_s = ibench_inserts_per_s = ([\\d.]+)\n' "$RESET" $B > $P; MAX=12 ;;
  KV-1) $B/benchbase/install.sh > $B/prep.log 2>&1 || { echo SETUPFAILED: benchbase; exit 1; }
    sed -i 's#<scalefactor>[0-9]*</scalefactor>#<scalefactor>30000</scalefactor>#' $B/benchbase/ycsba.xml.tpl
    $B/benchbase/bb_load.sh ycsba 16 >> $B/prep.log 2>&1 || { echo SETUPFAILED: ycsb load; exit 1; }
    printf '[bench]\nname = KV-1 BenchBase YCSB-A 50/50 read/update, zipfian, 30M rows (~35 GB, ~2x RAM), 64 terminals\n%s\nrun = %s/benchbase/bb_run.sh ycsba {warmup} {duration}\nwarmup = 300\nduration = 600\ntimeout = 2400\ngoal_metric = bb_tps\n\n[metrics]\nbb_tps = BBSUMMARY .* throughput_rps=([\\d.]+)\nbb_p99_ms = BBSUMMARY .* p99_ms=([\\d.]+)\n' "$RESET" $B > $P
    BASELINE_ARGS="--reserve-mem 4G" ;;
  AN-1) $GT tpch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D tpch --sf 10 -T 8 --analyze > $B/prep.log 2>&1
    printf '[bench]\nname = AN-1 TPC-H SF10 (go-tpc), 22 queries, 1 stream, 300 s cap per query\nreset = %s/tpch_warm.sh\ndata_state = read-only\nrun = %s tpch run -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D tpch --sf 10 --count 22 --enable-query-tuning=false --conn-params max_execution_time=300000 --ignore-error\nwarmup = 0\nduration = 0\ntimeout = 7200\ngoal_metric = pass_seconds\n\n[metrics]\npass_seconds = sum:\\[Summary\\] Q\\d+: ([\\d.]+)s\ngeomean_seconds = geomean:\\[Summary\\] Q\\d+: ([\\d.]+)s\n' $B "$GT" > $P
    GOAL=latency; MAX=10 ;;
  AN-2) $B/job/install.sh > $B/prep.log 2>&1 && $B/job/load.sh >> $B/prep.log 2>&1 || { echo SETUPFAILED: job; exit 1; }
    cp $B/job/job.ini $P; GOAL=latency; MAX=10 ;;
  MX-1) $GT tpcc prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses 500 -T 8 --no-check > $B/prep.log 2>&1
    $GT ch prepare -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses 500 -T 8 >> $B/prep.log 2>&1
    printf '[bench]\nname = MX-1 CH-benCHmark 500W: 32 TPC-C threads + 1 analytical stream\n%s\nrun = %s ch run -H 127.0.0.1 -P 3306 -U tpcc -p tpcc -D ch --warehouses 500 -T 32 -t 1 --time {total}s --interval 30s --conn-params max_execution_time=120000 --ap-conn-params max_execution_time=120000 --ignore-error\nwarmup = 300\nduration = 600\ntimeout = 2400\ngoal_metric = tpmc_whole_run\n\n[metrics]\ntpmc_whole_run = tpmC: ([\\d.]+)\nqphh_whole_run = QphH: ([\\d.]+)\n' "$RESET" "$GT" > $P; MAX=14 ;;
  HC-1|HC-2) [ -n "$CLI" ] || { echo SETUPFAILED: $CELL needs a client host; exit 1; }
    TH=$([ $CELL = HC-1 ] && echo 1024 || echo 4096); sb_prepare 16 4000000
    sb_profile "$CELL sysbench oltp_point_select, 16 x 4M rows (~15 GB, in memory), $TH connections from a separate client (plain statements)" oltp_point_select 16 4000000 $TH "--rand-type=uniform --db-ps-mode=disable" 300 600 "data_state = read-only"
    REQ="max_connections = $((TH + 100))
back_log = 4096"; BASELINE_ARGS="--max-connections $((TH + 100))" ;;
  HC-3) [ -n "$CLI" ] || { echo SETUPFAILED: $CELL needs a client host; exit 1; }
    sb_prepare 16 8000000
    sb_profile "HC-3 sysbench oltp_read_write, 16 x 8M rows (~30 GB), 4096 connections from a separate client (plain statements)" oltp_read_write 16 8000000 4096 "--db-ps-mode=disable" 600 600 "$RESET"
    REQ="max_connections = 4200
back_log = 4096"; BASELINE_ARGS="--max-connections 4200" ;;
  *) echo "SETUPFAILED: unknown cell $CELL"; exit 1 ;;
esac
[ -n "$REQ" ] && printf '\n[requires]\n%s\n' "$REQ" >> $P
if [ -n "$SMOKE" ]; then  # harness smoke test: short windows, two experiments
  sed -i -e 's/^warmup = [1-9][0-9]*/warmup = 60/' -e 's/^duration = [1-9][0-9]*/duration = 120/' $P; MAX=2
fi
grep -q "^run" $P || { echo SETUPFAILED: no profile; exit 1; }
mysql -e "SELECT table_schema, ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables GROUP BY 1"
cat > $B/queue.sh <<Q
#!/bin/bash
. $B/queue-common.sh
set -x
export SCOPE=knobs,os STUDY="$STUDY" BASELINE_ARGS="$BASELINE_ARGS"
run_session $P "$STUDY: $CELL $(sed -n 's/^name = //p' $P | cut -d' ' -f2- | cut -c1-90)" $GOAL $MAX
echo QUEUEDONE
Q
chmod +x $B/queue.sh; cd $B && (setsid nohup ./queue.sh > queue.log 2>&1 < /dev/null &)
echo SETUPDONE
