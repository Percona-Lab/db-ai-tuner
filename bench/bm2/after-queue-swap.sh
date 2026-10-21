#!/bin/bash
# start the swap study once the box's tuning queue has finished (or failed)
until grep -qE "QUEUEDONE|QUEUEFAILED" /root/bench/queue-sata.log 2>/dev/null; do sleep 120; done
exec /root/bench/bm2/swap-study.sh
