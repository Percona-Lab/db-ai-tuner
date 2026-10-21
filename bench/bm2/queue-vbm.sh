#!/bin/bash
# tuner-vbm: memory model on 8.4; the cost of missing debug symbols (same cell on 8.4 with symbols and 9.7 without);
# then the third repeatability session (same hardware model as tuner-vbm2, another data centre)
. /root/bench/bm2/common.sh
set -x
D=/root/bench/bm2
rm -f /root/bench/GO   # set only after the deploy canary passes on the current build
bash $D/prep84.sh || { echo "QUEUEFAILED: prep84"; exit 1; }
/opt/tuning-agent/venv/bin/python /root/bench/bm2/mem_validate.py > /root/bench/memprobe-84.json 2> /root/bench/memprobe-84.log || echo "memprobe failed (see memprobe-84.log)"
prepare 16 10000000
wait_go
export SCOPE=knobs,os CALIBRATE=3 BASELINE_ARGS="--reserve-mem 2G"
STUDY="Bare metal: debug symbols 8.4 vs 9.7" run_session $D/ro-io.ini "Read-only I/O-bound 37 GB on PS 8.4 with debug symbols (tuner-vbm)" tps 10
bash $D/prep84.sh ps-97-lts || { echo "QUEUEFAILED: prep 9.7"; exit 1; }
prepare 16 10000000
STUDY="Bare metal: debug symbols 8.4 vs 9.7" run_session $D/ro-io.ini "Read-only I/O-bound 37 GB on PS 9.7 without debug symbols (tuner-vbm)" tps 10
bash $D/prep84.sh || { echo "QUEUEFAILED: back to 8.4"; exit 1; }
prepare 16 2000000
STUDY="Bare metal: repeatability" run_session $D/repeat-rw.ini "Repeatability 3/3: read/write 8 GB in memory, 32 threads (tuner-vbm)" tps 12
echo QUEUEDONE
