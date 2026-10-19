#!/bin/bash
# resume-b2.sh <remaining experiment runs> [now]
# Continue a running Battery 2 session with the current agent: stop the old autotune (inside a measured window, so the
# run is aborted cleanly; or wait for it to finish when it is already stopping), keep the session open, update the agent,
# resume autotune with the remaining budget, close the session and mark the queue done for the orchestrator.
REMAIN=$1; set -x
cd /root/bench
pids() { pgrep -f "^$1"; }  # anchored: matches the process command line from its start only
for p in $(pids "/bin/bash ./queue.sh") $(pids "/bin/bash /root/bench/session.sh"); do kill $p; done  # no session stop
AT=$(pids "/opt/tuning-agent/venv/bin/python3 /usr/local/bin/tuning-agent autotune")
if [ -n "$AT" ] && ! grep -q "stopping$" session-battery.log; then
  for i in $(seq 1 360); do tuning-agent status --json 2>/dev/null | grep -q '"measurement_in_progress": {' && break; sleep 10; done
  kill $AT
fi
while [ -n "$(pids '/opt/tuning-agent/venv/bin/python3 /usr/local/bin/tuning-agent autotune')" ]; do sleep 10; done
tuning-agent status --json 2>/dev/null | grep -q '"measurement_in_progress": {' && tuning-agent measure abort --notes "resume with the fixed agent"
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null 2>&1 && systemctl restart tuning-agent
grep -q "failed before; next candidate" /opt/tuning-agent/venv/lib/python3.12/site-packages/tuning_agent/cli.py || { echo "QUEUEFAILED: agent update" >> queue.log; exit 1; }
echo "== resumed with the fixed agent: $REMAIN experiment runs left" >> session-battery.log
tuning-agent autotune --bench /root/bench/battery.ini --calibrate ${CALIBRATE:-3} --max-runs $REMAIN --tradeoffs >> session-battery.log 2>&1 \
  || { echo "QUEUEFAILED: resumed autotune" >> queue.log; exit 1; }
tuning-agent session stop >> session-battery.log 2>&1
echo QUEUEDONE >> queue.log
