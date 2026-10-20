#!/bin/bash
# Deploy canary on bare metal (tuner-canary): a short full autotune session with the agent the server ships now, under a
# time limit. Check it with: tools/canary_check.py <session> --log ... --budget N --expect docs/canary/expect-bm.json
# usage: run-bm.sh [max_runs] [minutes]
. /root/bench/queue-common.sh
set -x
update_agent
LIMIT=${2:-75}
export STUDY="Deploy canary (bare metal)" SCOPE=knobs,os
NAME="deploy canary $(date -u +%F' '%H:%M) agent $(tuning-agent --version 2>/dev/null)"
if ! timeout --signal=TERM ${LIMIT}m /root/bench/session.sh /root/bench/canary/bm.ini "$NAME" tps ${1:-10} > /root/bench/session-canary-bm.log 2>&1 < /dev/null; then
  echo "CANARYFAILED: session failed or exceeded ${LIMIT} min (see session-canary-bm.log)"
  tuning-agent measure abort >/dev/null 2>&1; tuning-agent session stop >/dev/null 2>&1
  exit 1
fi
echo CANARYDONE
