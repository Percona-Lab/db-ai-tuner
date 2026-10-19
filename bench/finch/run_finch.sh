#!/bin/bash
# Run one Finch stage of the orders workload for warm-up + duration seconds and print a parseable summary.
# Usage: run_finch.sh <oltp|churn> <warmup_s> <duration_s> [finch -p key=val ...]
# churn runs with FINCH_RECONNECT_EVERY=1 (new MySQL connection per request; needs the patched finch).
STAGE=$1; W=$2; D=$3; shift 3
H=$(cd $(dirname $0) && pwd)
CSV=/root/bench/finch-stats-$(date +%s)-$$.csv
[ "$STAGE" = churn ] && export FINCH_RECONNECT_EVERY=${FINCH_RECONNECT_EVERY:-1}
finch -p runtime=$((W + D))s -p csv=$CSV "$@" $H/orders/$STAGE.yaml 2>&1 | grep -v "reconnect"
python3 -I $H/finch_summary.py $CSV $W
mv $CSV /root/bench/finch-last-$STAGE.csv
