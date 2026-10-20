#!/bin/bash
# Insert Benchmark pilot on one tuner host (run on the host, detached):
#   (setsid nohup /root/bench/ibench/pilot.sh <stage> > /root/bench/ibench/pilot-<stage>.log 2>&1 < /dev/null &)
# stages:
#   load    update the agent, install iibench, load l.i0 + l.x (8 x 50M rows), snapshot to /var/lib/mysql_base,
#           time one reset
#   measure <profile.ini> [n]   n (default 3) measurement runs of a profile in their own session (reset before each)
#   tune    <profile.ini> [max_runs]   one tuning session (session.sh) on a profile
#   cbab    <profile.ini> [pairs]      innodb_change_buffering all/none A/B, interleaved, baseline config
#   all     the post-load queue used in the pilot: measure qr100 x3, tune l.i1 (8 runs), cbab l.i1 x3 pairs
. /root/bench/queue-common.sh
set -x
D=/root/bench/ibench
STAGE=$1
case "$STAGE" in
load)
  update_agent
  tuning-agent session stop >/dev/null 2>&1 || true
  # an earlier pristine copy is moved aside, never deleted
  if [ -n "$(ls -A /var/lib/mysql_base/ 2>/dev/null)" ]; then mv /var/lib/mysql_base /root/old-$(date +%Y%m%d%H%M)-base; fi
  step $D/install.sh
  IBENCH_CLIENTS=8 IBENCH_ROWS=50000000 $D/load.sh > $D/load.log 2>&1
  grep -q LOADDONE $D/load.log || { echo "QUEUEFAILED: load (see $D/load.log)"; exit 1; }
  grep -v '^+' $D/load.log | tail -25
  du -sh /var/lib/mysql_base
  t=$(date +%s); step /root/bench/reset_datadir.sh; echo "reset took $(( $(date +%s) - t )) s"
  ;;
measure)
  P=$2; N=${3:-3}
  goal=tps
  tuning-agent session stop >/dev/null 2>&1 || true
  step tuning-agent session start --goal $goal --policy benchmark --scope knobs,os --study "Pilot: ibench" \
       --name "ibench pilot: repeat runs $(basename $P .ini)" --workload "$(grep '^name' $P | cut -d= -f2- | sed 's/^ //')"
  for i in $(seq 1 $N); do
    step tuning-agent measure run --bench $P --label "pilot repeat $i"
  done
  tuning-agent session stop
  ;;
tune)
  P=$2; MAX=${3:-8}
  STUDY="Pilot: ibench" SCOPE=knobs,os run_session $P "ibench pilot: $(basename $P .ini) tuning" tps $MAX
  grep -m1 -i "session" /root/bench/session-$(basename $P .ini).log
  ;;
cbab)
  # change-buffering A/B on a profile: all / none interleaved, n pairs (default 3), data reset before every run,
  # baseline configuration otherwise; set with tuning-agent apply before each run (dynamic variable)
  P=$2; N=${3:-3}
  tuning-agent session stop >/dev/null 2>&1 || true
  # back to the baseline configuration (a tuning session may have left its best configuration applied)
  step tuning-agent rollback --reset
  step tuning-agent baseline --apply
  step tuning-agent session start --goal tps --policy benchmark --scope knobs --study "Pilot: ibench" \
       --name "ibench: change buffering A/B $(basename $P .ini)" --workload "$(grep '^name' $P | cut -d= -f2- | sed 's/^ //')"
  for i in $(seq 1 $N); do
    for v in all none; do
      step tuning-agent apply --set innodb_change_buffering=$v --reason "change-buffering A/B, pair $i"
      step tuning-agent measure run --bench $P --label "change_buffering=$v ($i/$N)"
    done
  done
  tuning-agent apply --set innodb_change_buffering=all --reason "A/B done: back to the server default" || true
  tuning-agent session stop
  ;;
all)
  step $0 measure $D/ibench-qr100.ini 3
  step $0 tune $D/ibench-li1.ini 8
  step $0 cbab $D/ibench-li1.ini 3
  ;;
*) echo "stage: load | measure <profile> [n] | tune <profile> [max_runs]"; exit 2 ;;
esac
echo "PILOTDONE $STAGE"
