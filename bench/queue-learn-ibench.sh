#!/bin/bash
# learning study (vbm2, SATA): steady-state tuning of the Insert Benchmark with the current engine (io_capacity cap fix,
# hypotheses, warm-up checks), then the change-buffering A/B in the warm regime. About 7 h.
. /root/bench/queue-common.sh
set -x
update_agent
D=/root/bench/ibench
export SKIP_DEFAULTS=1 CALIBRATE=2
step $D/pilot.sh tune $D/ibench-li1-steady.ini 9
unset SKIP_DEFAULTS CALIBRATE
step $D/pilot.sh cbab $D/ibench-li1-steady.ini 2
echo QUEUEDONE
