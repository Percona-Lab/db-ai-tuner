#!/bin/bash
# canary.sh [max_runs]: full autotune session with very short runs on the canary host, using the agent the server ships now.
# Not a benchmark: it exercises every code path (calibration, noise plan, requirements, rules, LLM, sweeps, confirmations,
# reverts, trade-off phase, final confirmations). Check the result with tools/canary_check.py <session id>.
. /root/bench/queue-common.sh
set -x
update_agent
STUDY="Canary" SCOPE=knobs,os run_session /root/bench/canary.ini "canary $(date -u +%F' '%H:%M) agent $(tuning-agent --version 2>/dev/null)" tps ${1:-16}
echo CANARYDONE
