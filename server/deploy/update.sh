#!/bin/bash
# Zero-downtime deploy: pull, start the new version on the idle port, health-check, switch nginx (graceful reload), stop the old one.
set -euo pipefail
cd /opt/aituner
git pull --ff-only
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
systemctl stop aituner@$CUR 2>/dev/null || true
systemctl disable -q aituner@$CUR 2>/dev/null || true
systemctl enable -q aituner@$NEW
systemctl stop aituner 2>/dev/null || true; systemctl disable -q aituner 2>/dev/null || true   # legacy single unit
echo "deployed $(git rev-parse --short HEAD) on port $NEW"
