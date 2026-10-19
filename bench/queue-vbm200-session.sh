#!/bin/bash
# restart only the 200W session (data already built)
. /root/bench/queue-common.sh
set -x
SCOPE=knobs,os,filesystem PRE_APPLY="os:datadir_fs=ext4" STUDY="HammerDB 200W hardware scaling" \
  run_session /root/bench/hammerdb200vbm.ini "HammerDB 200W on bare metal $(lscpu | sed -n 's/^Model name: *//p' | head -1 | sed 's/(R)//g; s/ CPU.*//; s/ Processor//' | cut -c1-30) (free range)" tps 20
echo QUEUEDONE
