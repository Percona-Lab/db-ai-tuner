#!/bin/bash
# tuner-vbm-sata, second validation round in steady state: 5 min warm-up was far too short for HammerDB 500W on SATA
# (baseline throughput still +6-10% and checkpoint age 30 -> 41% within the window). 45 min warm-up (the agent may
# raise it further at calibration, up to 60 min, if the starting configuration is still not steady) + 15 min measured.
# Key comparisons only, 2 runs each, each against the tuner's starting configuration (restored afterwards).
. /root/bench/queue-common.sh
set -x
update_agent
P=/root/bench/hammerdb500sata-steady.ini
cat > $P <<'INI'
[bench]
name = HammerDB TPROC-C 500W all-warehouse, 64 VU co-located, 45 min warm-up + 15 min (steady state), binlog off (E-2286G, SATA SSD)
reset = /root/bench/reset_datadir.sh
run = cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_VU=64 HDB_ALLWH=true HDB_RAMPUP={warmup_min} HDB_DURATION={duration_min} ./hammerdbcli auto /root/bench/hdb_run.tcl
warmup = 2700
duration = 900
timeout = 7200
goal_metric = nopm

[metrics]
nopm = achieved (\d+) NOPM
tpm = from (\d+) MySQL TPM
INI
cat > /root/bench/sata-steady.sh <<'POST'
#!/bin/bash
P=$1; set -x
cur() { mysql -N -e "SELECT @@GLOBAL.$1"; }
try() {  # try <reason> <var=value>...: 2 runs, then every variable back to the starting value
  local why=$1; shift
  declare -A base; local a=()
  for kv in "$@"; do base[${kv%%=*}]=$(cur ${kv%%=*}); a+=(--try "$kv"); done
  tuning-agent experiment add "${a[@]}" --repeats 2 --force --reason "steady-state validation: $why"
  tuning-agent experiment run --bench $P
  for v in "${!base[@]}"; do [ "$(cur $v)" = "${base[$v]}" ] || tuning-agent apply --set "$v=${base[$v]}" --reason "back to the starting value"; done
}
BP=$(( $(cur innodb_buffer_pool_size) / 1073741824 ))
try "Mark Callaghan cz13a, scaled, durability-matched" innodb_io_capacity=10000 innodb_io_capacity_max=20000 innodb_redo_log_capacity=${BP}G innodb_max_dirty_pages_pct_lwm=80 innodb_idle_flush_pct=1 innodb_purge_threads=4 innodb_max_purge_lag=500000 innodb_max_purge_lag_delay=1000000 innodb_flush_method=O_DIRECT_NO_FSYNC innodb_change_buffer_max_size=25 innodb_log_writer_threads=OFF
try "O_DIRECT_NO_FSYNC" innodb_flush_method=O_DIRECT_NO_FSYNC
tuning-agent measure run --bench $P --label "baseline re-check (drift)"
try "io_capacity 10000 together with dirty-page low-water mark 80 (interaction)" innodb_io_capacity=10000 innodb_io_capacity_max=20000 innodb_max_dirty_pages_pct_lwm=80
try "redo 4G (Percona)" innodb_redo_log_capacity=4G
try "Percona 2026 recommended set (durable)" innodb_buffer_pool_instances=8 innodb_io_capacity=2500 innodb_io_capacity_max=5000 innodb_redo_log_capacity=4G innodb_change_buffering=none table_open_cache=200000
POST
chmod +x /root/bench/sata-steady.sh
SKIP_DEFAULTS=1 CALIBRATE=2 POST=/root/bench/sata-steady.sh SCOPE=knobs,os STUDY="Settings validation: Percona / Mark Callaghan recommendations on PS 9.7" \
  run_session $P "Settings validation in steady state, HammerDB 500W I/O-bound, 64 VU, 45+15 min (E-2286G SATA SSD)" tps 0
echo QUEUEDONE
