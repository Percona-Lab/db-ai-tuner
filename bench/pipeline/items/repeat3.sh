#!/bin/bash
# Repeatability across identical hosts: started at the same time on the three E-2286G / SATA boxes. Same data, same
# profile, same agent build. Questions: run-to-run vs host-to-host noise, and whether three independent tuning sessions
# converge to the same configuration (and the same gain). Also the Percona 8.4 reference for the variant items.
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
dataset 16 10000000
P=/root/bench/pipe-rw.ini
sb_profile $P "sysbench oltp_read_write 16 x 10M (~37 GB, > RAM), 64 threads, special, 10 + 10 min (E-2286G, SATA)" oltp_read_write 16 10000000 64 special 600 600
STUDY="pipeline: repeatability x3 $(date -u +%F)" run_session $P "repeat3 ($(hostname))" tps 10
echo PIPEOK
