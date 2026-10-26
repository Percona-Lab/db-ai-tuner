#!/bin/bash
# session.sh <profile.ini> <name> <goal> [max_runs] : full protocol, unattended (PLANNER=1: an AI planner chooses each step; AI_OFF=1: no AI at all; EXPLORE=1: autotune --explore)
PROF=$1; NAME=$2; GOAL=${3:-tps}; MAX=${4:-10}
set -x
# the agent must match the server's build (the server refuses stale agents): update it before every session
[ -n "$NO_AGENT_UPDATE" ] || { curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null && systemctl restart tuning-agent; }
# a crashed driver can leave a measurement marked as running: this script is the only driver, so abort it
tuning-agent status --json 2>/dev/null | grep -q '"measurement_in_progress": {' && tuning-agent measure abort --notes "stale measurement from a crashed driver"
tuning-agent session stop >/dev/null 2>&1 || true   # a finished earlier session may still be open
tuning-agent rollback --reset || exit 1
if grep -q "^reset.*reset_datadir" $PROF; then
  # pristine data set: snapshot once, restore before the session
  # an empty (or missing) base would wipe the data set at the first reset
  if [ -z "$(ls -A /var/lib/mysql_base/ 2>/dev/null)" ]; then mkdir -p /var/lib/mysql_base/ && systemctl stop mysql && rsync -a /var/lib/mysql/ /var/lib/mysql_base/ && systemctl start mysql || exit 1; fi
  [ -d /var/lib/mysql_base/mysql ] || { echo "pristine copy /var/lib/mysql_base is not a data directory"; exit 1; }
  /root/bench/reset_datadir.sh || exit 1
fi
for kv in $PRE_APPLY; do tuning-agent apply --set "$kv" --reason "session setup" || exit 1; done  # e.g. PRE_APPLY="os:datadir_fs=ext4"
tuning-agent session start --goal $GOAL --policy benchmark ${PLANNER:+--planner} ${AI_OFF:+--ai off} ${SCOPE:+--scope $SCOPE} ${STUDY:+--study "$STUDY"} --name "$NAME" --workload "$(grep '^name' $PROF | cut -d= -f2- | sed 's/^ //')" || exit 1
[ -n "$SKIP_DEFAULTS" ] || tuning-agent measure run --bench $PROF --label "server defaults" || exit 1
tuning-agent baseline --apply $BASELINE_ARGS || exit 1  # e.g. BASELINE_ARGS="--reserve-mem 1.5G" with a co-located client
tuning-agent autotune --bench $PROF --calibrate ${CALIBRATE:-3} --max-runs $MAX --tradeoffs ${EXPLORE:+--explore} || exit 1
[ -n "$POST" ] && { $POST "$PROF" || exit 1; }  # extra phases in the same session (e.g. queued experiment series)
tuning-agent session stop
echo SESSIONDONE
