#!/bin/bash
# Warm-up check on real data (2026-10-22): a disk-bound read-write load with a deliberately short warm-up. Expect the
# 'warm-up may be short' calibration note with a suggested warm-up, 'warm-up?' badges, and no change of verdicts.
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
dataset 16 10000000
P=/root/bench/pipe-warmup.ini
sb_profile $P "sysbench oltp_read_write 16 x 10M (~37 GB, > RAM), 64 threads, special, 60 s warm-up (deliberately short) ($(hostname))" oltp_read_write 16 10000000 64 special 60 600
STUDY="pipeline: warm-up check $(date -u +%F)" run_session $P "warm-up short ($(hostname))" tps 6
echo PIPEOK
