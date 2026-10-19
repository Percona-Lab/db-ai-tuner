#!/bin/bash
# Tuning session only (the repeat-run CV is already known): session.sh job.ini, goal latency, 8 runs.
. /root/bench/queue-common.sh
export STUDY="Pilot: JOB" SCOPE=knobs,os
run_session /root/bench/job/job.ini "JOB pilot: optimizer-bound query set (latency)" latency 8
echo PILOTDONE
