#!/bin/bash
# AI DB Tuner agent installer.
#   curl -fsSL __SERVER__/install.sh | sudo bash -s -- --api-key KEY [--name NAME] [--mysql-user U --mysql-password P]
# Installs into /opt/tuning-agent, links /usr/local/bin/tuning-agent, runs `tuning-agent init` and starts the
# tuning-agent systemd service (metrics collector).
set -euo pipefail
SERVER="__SERVER__"
INIT_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2;;
    *) INIT_ARGS+=("$1"); shift;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  if command -v apt-get >/dev/null; then apt-get install -y -q python3-venv >/dev/null
  elif command -v dnf >/dev/null; then dnf install -y -q python3 >/dev/null; fi
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$SERVER/download/tuning-agent.tar.gz" | tar -xz -C "$TMP"
mkdir -p /opt/tuning-agent
[ -x /opt/tuning-agent/venv/bin/pip ] || python3 -m venv /opt/tuning-agent/venv
/opt/tuning-agent/venv/bin/pip install -q --upgrade "$TMP/tuning-agent"
ln -sf /opt/tuning-agent/venv/bin/tuning-agent /usr/local/bin/tuning-agent
cp "$TMP/tuning-agent/tuning-agent.service" /etc/systemd/system/tuning-agent.service
systemctl daemon-reload
if [ ${#INIT_ARGS[@]} -gt 0 ]; then
  tuning-agent init --server "$SERVER" "${INIT_ARGS[@]}"
  systemctl enable --now tuning-agent
  systemctl restart tuning-agent
  echo "tuning-agent installed and running. Try: tuning-agent guide"
else
  echo "tuning-agent installed. Next: tuning-agent init --server $SERVER --api-key KEY && systemctl enable --now tuning-agent"
fi
