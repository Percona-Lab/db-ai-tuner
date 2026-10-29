#!/bin/bash
# sweep-post.sh <profile>: the characterization sweep of a sweep cell (matrix.sh with SWEEP_VAR / SWEEP_VALUES), queued and
# run in the session the calibration opened - every value measured against the same calibrated start, nothing tuned.
# SWEEP_WITH: settings applied with every value (e.g. a buffer pool size kept for an instances sweep).
PROF=$1
set -x
tuning-agent experiment add --sweep "$SWEEP_VAR=$SWEEP_VALUES" ${SWEEP_WITH:+--try "$SWEEP_WITH"} \
  --reason "characterization sweep: how the workload responds across the whole range of $SWEEP_VAR" || exit 1
tuning-agent experiment run --bench "$PROF" || exit 1
