#!/bin/bash
# run after the host's phase-B queue: compat.sh for each "flavor:version" argument
while ! grep -q QUEUEDONE /root/bench/queue.log; do sleep 120; done
for fv in "$@"; do /root/bench/compat.sh ${fv%%:*} ${fv##*:}; done
echo ALLCOMPATDONE > /root/bench/compat-all.done
