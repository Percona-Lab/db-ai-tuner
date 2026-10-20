#!/bin/bash
# learning study (vbm): tuning sessions on BenchBase YCSB-A (zipfian, update-heavy) and TATP with the current engine.
. /root/bench/queue-common.sh
set -x
update_agent
export STUDY="Learning: BenchBase" TUNE=bb-ycsba MAX=12
step /root/bench/benchbase/pilot.sh
export TUNE=bb-tatp MAX=10
step /root/bench/benchbase/pilot.sh
echo QUEUEDONE
