#!/bin/bash
# Noise-handling canary on a shared cloud VM (tuner-os, 2 vCPU): calibration, the noise plan, disturbance detection,
# steal / unsteady handling under real neighbours. Runs nightly from cron; check with tools/canary_check.py --noise.
. /root/bench/queue-common.sh
set -x
update_agent
export STUDY="Noise canary (cloud VM)" SCOPE=knobs,os
if ! timeout --signal=TERM 150m /root/bench/session.sh /root/bench/canary.ini "noise canary $(date -u +%F' '%H:%M) agent $(tuning-agent --version 2>/dev/null)" tps ${1:-12} \
     > /root/bench/session-canary-noise.log 2>&1 < /dev/null; then
  echo "CANARYFAILED: see session-canary-noise.log"
  # timeout signals session.sh only: stop what it started (autotune, the benchmark client), then close the session
  for p in $(ps -eo pid,args | awk '/tuning-agent autotune|tuning-agent measure run/ && !/awk/ {print $1}'); do kill $p; done
  for p in $(ps -eo pid,comm | awk '$2=="sysbench" {print $1}'); do kill $p; done
  sleep 5
  tuning-agent measure abort >/dev/null 2>&1; tuning-agent session stop >/dev/null 2>&1
  exit 1
fi
echo CANARYDONE
