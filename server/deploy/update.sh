#!/bin/bash
# Pull latest code and restart the web app.
set -euo pipefail
cd /opt/aituner
git pull --ff-only
/opt/aituner/venv/bin/pip install -q -r server/requirements.txt
systemctl restart aituner
sleep 2
curl -fsS http://127.0.0.1:8000/healthz && echo " deployed $(git rev-parse --short HEAD)"
