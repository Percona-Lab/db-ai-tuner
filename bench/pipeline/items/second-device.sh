#!/bin/bash
# Second drive for write-path files: the doublewrite buffer on the box's other drive (innodb_doublewrite_dir). Candidate
# rule from the SATA bare-metal runs (+6%); now measured with repeats on SATA and NVMe, write-heavy and disk-bound.
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
d=$(second_disk); echo "second drive: $d"
mkdir -p /disk2/dblwr && chown mysql:mysql /disk2/dblwr
dataset 16 10000000
P=/root/bench/pipe-update-disk.ini
sb_profile $P "sysbench oltp_update_index 16 x 10M (~37 GB, > RAM), 64 threads, uniform: write / flush-bound ($(hostname))" oltp_update_index 16 10000000 64 uniform 600 600
post() { REPEATS=3 try $1 "doublewrite buffer on the second drive /dev/$d" innodb_doublewrite_dir=/disk2/dblwr; }
STUDY="pipeline: second drive $(date -u +%F)" POST=post run_session $P "second drive ($(hostname))" tps 6
echo PIPEOK
