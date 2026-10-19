#!/bin/bash
# JOB pilot (atdb3): 2 plain repeat runs for the run-to-run CV, then one short tuning session (goal latency, 8 runs).
# Run detached: (setsid nohup /root/bench/job/pilot.sh > /root/bench/job-pilot.log 2>&1 < /dev/null &)
. /root/bench/queue-common.sh
CAP=${CAP:-60}
for i in 1 2; do
  t0=$(date +%s); /root/bench/job/job_warm.sh; echo "warm-up $(( $(date +%s) - t0 )) s"
  /root/bench/job/run_job.sh $CAP > /root/bench/job-pilot-run$i.log 2>&1; tail -1 /root/bench/job-pilot-run$i.log
done
[ "${SKIP_SESSION:-0}" = 1 ] && exit 0
export STUDY="Pilot: JOB" SCOPE=knobs,os
run_session /root/bench/job/job.ini "JOB pilot: optimizer-bound query set (latency)" latency 8
echo PILOTDONE
