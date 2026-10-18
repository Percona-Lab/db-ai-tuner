#!/bin/bash
# retry compat runs that failed for harness reasons, after the host's queue has finished
while ! grep -q QUEUEDONE /root/bench/queue.log; do sleep 120; done
for fv in "$@"; do /root/bench/compat.sh ${fv%%:*} ${fv##*:}; done
echo RETRYDONE > /root/bench/compat-retry.done
