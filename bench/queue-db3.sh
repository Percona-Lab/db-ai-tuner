#!/bin/bash
# tuner-db3: sysbench 20 GB uniform + special
. /root/bench/queue-common.sh
set -x
wait_idle; update_agent
apt-get install -yq sysbench >/dev/null
tuning-agent baseline --apply
/root/bench/prep-sysbench.sh 16 5200000
purge_binlogs; df -h / | tail -1
run_session /root/bench/sb20g-uniform.ini "sysbench 20 GB I/O-bound, uniform" tps 8
run_session /root/bench/sb20g-special.ini "sysbench 20 GB I/O-bound, special (skewed)" tps 8
echo QUEUEDONE
