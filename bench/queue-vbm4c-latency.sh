#!/bin/bash
# tuner-vbm4c (Xeon E3-1270 4c/8t, 32 GB): HammerDB 200W, 16 VU co-located, binlog off. Goal: tail latency
# (NEW_ORDER P99 from HammerDB's time profile, lower is better); NOPM and other percentiles recorded alongside.
# Data restored before every run, 5 min ramp-up + 15 min measured, up to 30 experiments.
. /root/bench/queue-common.sh
set -x
update_agent
P=/root/bench/hammerdb200lat.ini
cat > $P <<'INI'
[bench]
name = HammerDB TPROC-C 200W, 16 VU co-located, 5 + 15 min, binlog off: tail latency (NEW_ORDER P99) (Vultr bare metal E3-1270)
reset = /root/bench/reset_datadir.sh
run = rm -f /tmp/hdbxtprofile.log; cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_VU=16 HDB_RAMPUP=5 HDB_DURATION={duration_min} ./hammerdbcli auto /root/bench/hdb_run.tcl; awk '/SUMMARY OF/ {s = ""} {s = s $0 "\n"} END {printf "%s", s}' /tmp/hdbxtprofile.log
warmup = 305
duration = 900
timeout = 2700
goal_metric = neword_p99_ms

[metrics]
neword_p99_ms = PROC: NEWORD\s+CALLS:[^\n]*\s+P99: ([\d.]+)ms
neword_p95_ms = PROC: NEWORD\s+CALLS:[^\n]*\s+P99: [\d.]+ms\s+P95: ([\d.]+)ms
payment_p99_ms = PROC: PAYMENT\s+CALLS:[^\n]*\s+P99: ([\d.]+)ms
nopm = achieved (\d+) NOPM
tpm = from (\d+) MySQL TPM
INI
SCOPE=knobs,os STUDY="E3-1270: HammerDB 200W tail latency" \
  run_session $P "HammerDB 200W tail latency (NEW_ORDER P99), 16 VU, binlog off (E3-1270)" latency 30
echo QUEUEDONE
