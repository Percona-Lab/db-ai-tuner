#!/bin/bash
# learning study (db3): JOB (optimizer / read-bound) tuning session with the current engine (read-ahead, join buffer).
. /root/bench/queue-common.sh
set -x
update_agent
step /root/bench/job/session-only.sh
echo QUEUEDONE
