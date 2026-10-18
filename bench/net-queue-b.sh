#!/bin/bash
# rest of the network sessions after net-2 (restarted 2026-10-18 after the client: option-file bug)
. /root/bench/queue-common.sh
set -x
run_session /root/bench/net3.ini "net-3: connection storm from same DC" tps 6
run_session /root/bench/net4.ini "net-4: large results over WAN (20 ms)" tps 6
echo QUEUEDONE
