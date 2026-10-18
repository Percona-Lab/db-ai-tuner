#!/bin/bash
. /root/bench/queue-common.sh
set -x
STUDY="Method check: old vs new engine" run_session /root/bench/hammerdb200.ini "method check: HammerDB 200W 10 VU, new engine (compare #11)" tps 12
echo QUEUEDONE
