#!/bin/bash
# after pilot.sh all: steady-state checks with the extended sampler (checkpoint age, change buffer size)
while ps -eo args= | grep -q "^/bin/bash /root/bench/ibench/pilot.sh all"; do sleep 30; done
/root/bench/ibench/pilot.sh measure /root/bench/ibench/ibench-qr100.ini 1 || exit 1
/root/bench/ibench/pilot.sh measure /root/bench/ibench/ibench-li1-steady.ini 2 || exit 1
echo FOLLOWUPDONE
