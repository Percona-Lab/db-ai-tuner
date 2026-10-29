#!/bin/bash
# Zero-downtime deploy: fetch, run the server tests on the new tree, pull, start the new version on the idle port,
# health-check, switch nginx (graceful reload), stop the old one.
# SKIP_SERVER_TESTS=1 update.sh: deploy without the test gate (emergencies only).
set -euo pipefail
cd /opt/aituner
git fetch -q
REV=$(git rev-parse '@{u}')
# gate: the server tests (server/tests/run.sh: API and background processes on temporary databases) on the exact tree
# about to be deployed, extracted to a temporary directory - a failure leaves /opt/aituner and the running version as they are
if [ "${SKIP_SERVER_TESTS:-}" = 1 ]; then
  echo "WARNING: SKIP_SERVER_TESTS=1 - deploying ${REV:0:7} WITHOUT the server tests"
else
  /opt/aituner/venv/bin/python -c "import pytest, httpx" 2>/dev/null || /opt/aituner/venv/bin/pip install -q pytest httpx || true
  if /opt/aituner/venv/bin/python -c "import pytest, httpx" 2>/dev/null; then
    TT=$(mktemp -d /tmp/aituner-gate-XXXXXX)
    git archive "$REV" server docs/claude-code-skill | tar -x -C "$TT"
    /opt/aituner/venv/bin/pip install -q -r "$TT/server/requirements.txt"
    echo "server tests on ${REV:0:7} ..."
    if ! timeout 180 env PY=/opt/aituner/venv/bin/python bash "$TT/server/tests/run.sh" > "$TT/tests.log" 2>&1; then
      tail -40 "$TT/tests.log"
      echo "SERVER TESTS FAILED on ${REV:0:7}: not deployed; the running version keeps serving (full log: $TT/tests.log)"
      exit 1
    fi
    tail -1 "$TT/tests.log"
    # the agent ships from this server too (install.sh): its tests on the same revision, in a venv of its own (PyMySQL)
    AV=/opt/aituner/agent-test-venv
    [ -x $AV/bin/python ] || python3 -m venv $AV
    $AV/bin/python -c "import pymysql" 2>/dev/null || $AV/bin/pip install -q "PyMySQL>=1.0"
    git archive "$REV" agent | tar -x -C "$TT"
    # the similarity core is one module in two places (the agent computes the state vector, the server the neighbours)
    if ! cmp -s "$TT/agent/tuning_agent/simcore.py" "$TT/server/app/simcore.py"; then
      echo "simcore.py differs between agent/tuning_agent and server/app on ${REV:0:7}: not deployed"
      exit 1
    fi
    echo "agent tests on ${REV:0:7} ..."
    if ! (cd "$TT/agent" && timeout 300 $AV/bin/python tests/test_engine.py > "$TT/agent-tests.log" 2>&1); then
      grep -E "^FAIL|Error" "$TT/agent-tests.log" | tail -40
      echo "AGENT TESTS FAILED on ${REV:0:7}: not deployed; the running version keeps serving (full log: $TT/agent-tests.log)"
      exit 1
    fi
    echo "agent tests: $(grep -c '^ok' "$TT/agent-tests.log") passed"
    rm -rf "$TT"
  else
    echo "WARNING: pytest / httpx could not be installed into /opt/aituner/venv - server tests SKIPPED for ${REV:0:7}"
  fi
fi
git merge -q --ff-only "$REV"
/opt/aituner/venv/bin/pip install -q -r server/requirements.txt
cp server/deploy/aituner@.service /etc/systemd/system/aituner@.service
systemctl daemon-reload
CUR=$(grep -oE '127.0.0.1:(8000|8001)' /etc/nginx/aituner-upstream.conf 2>/dev/null | head -1 | cut -d: -f2 || true)
CUR=${CUR:-8000}
NEW=$([ "$CUR" = 8000 ] && echo 8001 || echo 8000)
systemctl stop aituner@$NEW 2>/dev/null || true
systemctl start aituner@$NEW
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:$NEW/healthz >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:$NEW/healthz >/dev/null || { echo "new version failed health check; old one keeps serving"; systemctl stop aituner@$NEW; exit 1; }
# the UI must load without JavaScript errors before any traffic moves to the new version
if [ -x /opt/pw/bin/python ]; then
  PLAYWRIGHT_BROWSERS_PATH=/opt/pw/browsers /opt/pw/bin/python /opt/aituner/server/deploy/ui_check.py http://127.0.0.1:$NEW/ \
    || { echo "new version's UI failed to load; old one keeps serving"; systemctl stop aituner@$NEW; exit 1; }
fi
echo "set \$aituner_upstream http://127.0.0.1:$NEW;" > /etc/nginx/aituner-upstream.conf
nginx -t -q && systemctl reload nginx
sleep 5   # let in-flight requests on the old version finish
# a learner pass runs inside the old version: wait for it (at most 20 min) instead of cutting it off (the 15:28 pass of
# 2026-10-28 ended "interrupted (service restart)" by a deploy)
for i in $(seq 1 120); do
  n=$(sqlite3 -readonly /var/lib/aituner/learning.db "SELECT COUNT(*) FROM learner_runs WHERE ended IS NULL AND started > strftime('%s','now') - 3600" 2>/dev/null || echo 0)
  [ "${n:-0}" = 0 ] && break
  [ $i = 1 ] && echo "a learner pass is running on the old version: waiting for it before stopping it"
  sleep 10
done
systemctl stop aituner@$CUR 2>/dev/null || true
systemctl disable -q aituner@$CUR 2>/dev/null || true
systemctl enable -q aituner@$NEW
systemctl stop aituner 2>/dev/null || true; systemctl disable -q aituner 2>/dev/null || true   # legacy single unit
echo "deployed $(git rev-parse --short HEAD) on port $NEW"
# after the switch, a short pass of the UI suite with real data (read-only) when a test account is set up in
# /etc/aituner-uitest.env (UI_EMAIL, UI_PASSWORD_FILE, optional UI_BASE): warnings only, never a rollback
if [ -f /etc/aituner-uitest.env ] && [ -x /opt/pw/bin/python ]; then
  UL=$(mktemp /tmp/aituner-uisuite-XXXXXX.log)
  BASE=$(grep -oP '^TUNER_PUBLIC_URL=\K.*' /etc/aituner.env 2>/dev/null | tr -d "\"'" || true)
  if ( set -a; . /etc/aituner-uitest.env; set +a
       PLAYWRIGHT_BROWSERS_PATH=/opt/pw/browsers timeout 600 /opt/pw/bin/python /opt/aituner/server/tests/ui_suite.py \
         "${UI_BASE:-${BASE:-http://127.0.0.1:$NEW}}" --max-sessions 3 --phone-pages 4 ) > "$UL" 2>&1; then
    echo "UI suite: $(tail -1 "$UL")"
    rm -f "$UL"
  else
    echo "WARNING: the UI suite found problems on the deployed version (not rolled back; full log: $UL):"
    grep -E '^(FAIL |       - )' "$UL" || tail -5 "$UL"
    tail -1 "$UL"
  fi
fi
