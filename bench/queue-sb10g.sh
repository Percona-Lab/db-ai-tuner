#!/bin/bash
# sysbench 10 GB scaling study: 15 runs of 15 min (defaults + 3 calibration + 11 experiments), no data reset (oltp_read_write keeps the size)
. /root/bench/queue-common.sh
set -x
STUDY="Sysbench 10 GB on shared Linode (1-16 GB RAM)" run_session /root/bench/sb10g.ini "sysbench 10 GB on $(hostname) ($(free -g | awk '/Mem:/{print $2+1}') GB RAM, $(nproc) vCPU)" tps 11
echo QUEUEDONE
