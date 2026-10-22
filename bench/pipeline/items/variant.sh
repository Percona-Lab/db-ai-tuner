#!/bin/bash
# variant.sh <percona|mysql|mariadb> <series>: the repeat3 workload tuned on another variant / release series on the same
# hardware. Do the same settings win, by the same margins? Feeds the evidence weighting by variant and series.
F=$1; V=$2
. /root/bench/pipeline/lib.sh
set -x
step bash /root/bench/pipeline/install_variant.sh $F $V
dataset 16 10000000
P=/root/bench/pipe-rw.ini
sb_profile $P "sysbench oltp_read_write 16 x 10M (~37 GB, > RAM), 64 threads, special, 10 + 10 min (E-2286G, SATA)" oltp_read_write 16 10000000 64 special 600 600
STUDY="pipeline: variants $(date -u +%F)" run_session $P "variant $F $V ($(hostname))" tps 10
echo PIPEOK
