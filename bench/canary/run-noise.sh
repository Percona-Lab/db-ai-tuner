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
  tuning-agent measure abort >/dev/null 2>&1; tuning-agent session stop >/dev/null 2>&1
  exit 1
fi
echo CANARYDONE
