#!/bin/bash
# pilot.sh [profile names...]: BenchBase pilot on one host (run from /root/bench/benchbase, profiles in the same directory).
#  1) metric checks: per profile, a session "BenchBase pilot checks: <profile>" with N measurement runs at the
#     baseline configuration (tuning-agent measure run --bench: the agent resets, runs and parses), then one timed reset
#  2) TUNE=<profile>: one tuning session (session.sh, max_runs ${MAX:-8}) on that profile
# Usage: N=3 TUNE=bb-ycsba ./pilot.sh bb-tatp bb-smallbank bb-twitter bb-wikipedia bb-ycsba bb-ycsbc
. /root/bench/queue-common.sh
set -x
D=/root/bench/benchbase; N=${N:-3}; STUDY=${STUDY:-"Pilot: BenchBase"}
export BASELINE_ARGS=${BASELINE_ARGS:-"--reserve-mem 4G"}   # BenchBase JVM (-Xmx3g) runs on the same host
tuning-agent session stop >/dev/null 2>&1 || true
# pristine data set (all bb_* databases): snapshot once
if [ -z "$(ls -A /var/lib/mysql_base/ 2>/dev/null)" ]; then
  t0=$(date +%s); mkdir -p /var/lib/mysql_base && systemctl stop mysql && rsync -a /var/lib/mysql/ /var/lib/mysql_base/ && systemctl start mysql || { echo "QUEUEFAILED: snapshot"; exit 1; }
  echo "SNAPSHOT seconds=$(( $(date +%s) - t0 )) size=$(du -sh /var/lib/mysql_base | cut -f1)"
fi
for p in "$@"; do
  P=$D/$p.ini
  step tuning-agent session start --goal tps --policy benchmark --study "$STUDY" --name "BenchBase pilot checks: $p" \
    --workload "$(sed -n 's/^name = //p' $P)"
  for i in $(seq 1 $N); do tuning-agent measure run --bench $P --label "check $i" || echo "RUNFAILED $p $i"; done
  tuning-agent session stop
  if grep -q '^reset' $P; then t0=$(date +%s); /root/bench/reset_datadir.sh; echo "RESET $p seconds=$(( $(date +%s) - t0 ))"; fi
  for r in $(ls -1dt $D/results/${p#bb-}-*/ | head -$N | tac); do echo "CHECK $p $(python3 -I $D/bb_summary.py $r)"; done
done
if [ -n "$TUNE" ]; then
  SCOPE=knobs,os STUDY="$STUDY" run_session $D/$TUNE.ini "BenchBase pilot: $(sed -n "s/^name = BenchBase //p" $D/$TUNE.ini)" tps ${MAX:-8}
  grep -h "^Session #" /root/bench/session-$TUNE.log
fi
echo QUEUEDONE
