#!/bin/bash
# helpers for queue scripts
wait_idle() { while ps -eo cmd | grep -q "[t]uning-agent autotune\|[s]ession.sh"; do sleep 60; done; }
update_agent() { curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null && systemctl restart tuning-agent; tuning-agent session stop >/dev/null 2>&1 || true; }
purge_binlogs() { mysql -e "FLUSH BINARY LOGS; PURGE BINARY LOGS BEFORE NOW() + INTERVAL 1 SECOND" || true; }
run_session() { update_agent; /root/bench/session.sh "$1" "$2" "$3" "${4:-8}" > "/root/bench/session-$(basename $1 .ini).log" 2>&1 < /dev/null \
  || { echo "QUEUEFAILED: session $1 failed (see /root/bench/session-$(basename $1 .ini).log)"; exit 1; }; }
step() { "$@" || { echo "QUEUEFAILED: $*"; exit 1; }; }
