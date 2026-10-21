#!/bin/bash
# tuner-vbm2: repeatability - the same cell twice on one host (the third run is on tuner-vbm, same hardware model)
. /root/bench/bm2/common.sh
set -x
D=/root/bench/bm2
rm -f /root/bench/GO   # set only after the deploy canary passes on the current build
bash $D/prep84.sh || { echo "QUEUEFAILED: prep84"; exit 1; }
prepare 16 2000000
wait_go
export SCOPE=knobs,os CALIBRATE=3 BASELINE_ARGS="--reserve-mem 2G" STUDY="Bare metal: repeatability"
run_session $D/repeat-rw.ini "Repeatability 1/3: read/write 8 GB in memory, 32 threads (tuner-vbm2)" tps 12
run_session $D/repeat-rw.ini "Repeatability 2/3: read/write 8 GB in memory, 32 threads (tuner-vbm2)" tps 12
echo QUEUEDONE
