#!/bin/bash
# tuner-vbm-sata (E-2286G 6c/12t, 32 GB, Intel SATA SSD): do the recommended settings still matter on PS 9.7?
# I/O-bound write-heavy HammerDB 500W all-warehouse, 64 VU, binlog off, data restored before every run, 5 + 10 min.
# One setting at a time against the tuner's starting configuration (2 runs per value, restored afterwards), baseline
# re-measured every second experiment, then two whole configurations (Percona's 2026 set, Mark Callaghan's cz13a scaled).
# See docs/research/recommended-settings-validation.md.
. /root/bench/queue-common.sh
set -x
update_agent
P=/root/bench/hammerdb500sata.ini
cat > $P <<'INI'
[bench]
name = HammerDB TPROC-C 500W all-warehouse, 64 VU co-located, 5 + 10 min, binlog off (E-2286G, SATA SSD)
reset = /root/bench/reset_datadir.sh
run = cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_VU=64 HDB_ALLWH=true HDB_RAMPUP=5 HDB_DURATION={duration_min} ./hammerdbcli auto /root/bench/hdb_run.tcl
warmup = 305
duration = 600
timeout = 3000
goal_metric = nopm

[metrics]
nopm = achieved (\d+) NOPM
tpm = from (\d+) MySQL TPM
INI
if [ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse' 2>/dev/null)" != 500 ]; then
  mysql -e "DROP DATABASE IF EXISTS tpcc"
  (cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_WH=500 HDB_BUILD_VU=12 ./hammerdbcli auto /root/bench/hdb_build200.tcl) > /root/bench/build500.log 2>&1
  [ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = 500 ] || { echo "QUEUEFAILED: build 500"; exit 1; }
fi
mysql -e "SELECT ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables WHERE table_schema='tpcc'"
cat > /root/bench/sata-validate.sh <<'POST'
#!/bin/bash
# inside the session, after calibration: one-at-a-time validation, then whole configurations
P=$1; set -x
n=0
cur() { mysql -N -e "SELECT @@GLOBAL.$1"; }
recheck() { n=$((n + 1)); [ $((n % 2)) = 0 ] && tuning-agent measure run --bench $P --label "baseline re-check $n"; }
one() {  # one <reason> <var> <values...>: sweep one variable, then put it back to the baseline value
  local why=$1 var=$2; shift 2
  local base; base=$(cur $var)
  if [ $# -eq 1 ]; then  # one value: a "try" (a sweep needs at least two values)
    tuning-agent experiment add --try "$var=$1" --repeats 2 --force --reason "validation: $why"
  else
    tuning-agent experiment add --sweep "$var=$(IFS=,; echo "$*")" --repeats 2 --force --reason "validation: $why"
  fi
  tuning-agent experiment run --bench $P
  [ "$(cur $var)" = "$base" ] || tuning-agent apply --set "$var=$base" --reason "validation: back to the baseline value"
  recheck
}
combo() {  # combo <reason> <var=value>...: one combined configuration (3 runs), then every variable back
  local why=$1; shift
  declare -A base; local a=()
  for kv in "$@"; do base[${kv%%=*}]=$(cur ${kv%%=*}); a+=(--try "$kv"); done
  tuning-agent experiment add "${a[@]}" --repeats 3 --force --reason "validation: $why"
  tuning-agent experiment run --bench $P
  for v in "${!base[@]}"; do [ "$(cur $v)" = "${base[$v]}" ] || tuning-agent apply --set "$v=${base[$v]}" --reason "validation: back to the baseline value"; done
  recheck
}
BP=$(( $(cur innodb_buffer_pool_size) / 1073741824 ))
# most valuable first: the list runs ~8-9 h after the data load; stopping early still leaves complete comparisons
one "change buffering (9.7 default all; 8.4 and Percona: none)" innodb_change_buffering none
one "redo 4G (Percona) and ~buffer pool size (Mark) vs 25% of RAM" innodb_redo_log_capacity 4G ${BP}G
one "io_capacity 10000 (9.7 default, Mark) vs the tuner's value" innodb_io_capacity 10000
one "dirty pages low-water mark 80 (Mark) vs 10" innodb_max_dirty_pages_pct_lwm 80
combo "Mark Callaghan cz13a, scaled, durability-matched" innodb_io_capacity=10000 innodb_io_capacity_max=20000 innodb_redo_log_capacity=${BP}G innodb_max_dirty_pages_pct_lwm=80 innodb_idle_flush_pct=1 innodb_purge_threads=4 innodb_max_purge_lag=500000 innodb_max_purge_lag_delay=1000000 innodb_flush_method=O_DIRECT_NO_FSYNC innodb_change_buffer_max_size=25 innodb_log_writer_threads=OFF
combo "Percona 2026 recommended set (durable)" innodb_buffer_pool_instances=8 innodb_io_capacity=2500 innodb_io_capacity_max=5000 innodb_redo_log_capacity=4G innodb_change_buffering=none table_open_cache=200000
one "O_DIRECT_NO_FSYNC (Mark)" innodb_flush_method O_DIRECT_NO_FSYNC
one "log writer threads (9.7 binlog-off default ON above 4 CPUs)" innodb_log_writer_threads OFF
combo "purge: Mark's max_purge_lag 500k / 1 s delay with 4 purge threads" innodb_max_purge_lag=500000 innodb_max_purge_lag_delay=1000000 innodb_purge_threads=4
POST
chmod +x /root/bench/sata-validate.sh
# session: server defaults, the tuner's starting config, 3 calibration runs, no autotune experiments, then the validation
POST=/root/bench/sata-validate.sh SCOPE=knobs,os STUDY="Settings validation: Percona / Mark Callaghan recommendations on PS 9.7" \
  run_session $P "Settings validation, HammerDB 500W I/O-bound, 64 VU (E-2286G SATA SSD)" tps 0
echo QUEUEDONE
