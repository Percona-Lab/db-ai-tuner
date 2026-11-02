#!/bin/bash
# micro-post.sh <profile>: in the calibrated session, the case's sweep (each value REPEATS times), then its expectations
PROF=$1
eval "$(python3 - $MICRO_CASE <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))
print(f"VAR={shlex.quote(c['sweep']['variable'])} VALS={shlex.quote(','.join(c['sweep']['values']))} REP={int(c.get('repeats', 2))}")
PY
)"
set -x
tuning-agent experiment add --sweep "$VAR=$VALS" --repeats $REP --force \
  --reason "mechanism micro-benchmark $(basename $(dirname $MICRO_CASE))/$(basename $MICRO_CASE .json): each value measured from the same start" || exit 1
tuning-agent experiment run --bench "$PROF" || exit 1
/opt/tuning-agent/venv/bin/python /root/bench/micro/check.py "$MICRO_CASE"
exit 0  # a failed expectation is a result, not a failed item
