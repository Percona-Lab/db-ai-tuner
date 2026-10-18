#!/bin/bash
# session.sh <profile.ini> <name> <goal> [max_runs] : full protocol, unattended
PROF=$1; NAME=$2; GOAL=${3:-tps}; MAX=${4:-10}
set -x
# a crashed driver can leave a measurement marked as running: this script is the only driver, so abort it
tuning-agent status --json 2>/dev/null | grep -q '"measurement_in_progress": {' && tuning-agent measure abort --notes "stale measurement from a crashed driver"
tuning-agent session stop >/dev/null 2>&1 || true   # a finished earlier session may still be open
tuning-agent rollback --reset || exit 1
if grep -q "^reset.*reset_datadir" $PROF; then
  # pristine data set: snapshot once, restore before the session
  if [ ! -d /var/lib/mysql_base ]; then systemctl stop mysql && rsync -a /var/lib/mysql/ /var/lib/mysql_base/ && systemctl start mysql; fi
  /root/bench/reset_datadir.sh || exit 1
fi
tuning-agent session start --goal $GOAL --policy benchmark --name "$NAME" --workload "$(grep '^name' $PROF | cut -d= -f2- | sed 's/^ //')" || exit 1
tuning-agent measure run --bench $PROF --label "server defaults" || exit 1
tuning-agent baseline --apply || exit 1
tuning-agent autotune --bench $PROF --calibrate 3 --max-runs $MAX --tradeoffs || exit 1
tuning-agent session stop
echo SESSIONDONE
