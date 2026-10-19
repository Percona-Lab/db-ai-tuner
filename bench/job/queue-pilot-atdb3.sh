#!/bin/bash
# atdb3 pilot queue: Finch repeat runs (writing, data reset each run), then the JOB pilot (2 runs + tuning session).
# (setsid nohup /root/bench/job/queue-pilot-atdb3.sh > /root/bench/queue-pilot.log 2>&1 < /dev/null &)
echo "start $(date)"
/root/bench/finch/pilot.sh > /root/bench/finch-pilot.log 2>&1; echo "finch pilot done $(date)"; tail -8 /root/bench/finch-pilot.log
/root/bench/job/pilot.sh > /root/bench/job-pilot.log 2>&1; echo "job pilot done $(date)"; tail -4 /root/bench/job-pilot.log
