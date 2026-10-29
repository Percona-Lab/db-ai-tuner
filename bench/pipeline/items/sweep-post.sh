#!/bin/bash
# sweep-post.sh <profile>: the characterization sweep of a sweep cell (matrix.sh with SWEEP_VAR / SWEEP_VALUES), queued and
# run in the session the calibration opened - every value measured against the same calibrated start, nothing tuned.
# SWEEP_WITH: settings applied with every value (e.g. a buffer pool size kept for an instances sweep).
PROF=$1
if [ "$SWEEP_VALUES" = auto-bp ]; then
  # from far too small up to the calibrated (tuned) pool; the steepest step gets two more points (--refine)
  base=$(mysql -N -e "SELECT @@innodb_buffer_pool_size DIV 1048576")
  SWEEP_VALUES=$(for v in 512 1024 2048 4096 8192 16384 32768 65536; do [ $v -lt $((base * 9 / 10)) ] && printf "%sM," $v; done; echo "${base}M")
  REFINE=${REFINE:-2}
fi
set -x
tuning-agent experiment add --sweep "$SWEEP_VAR=$SWEEP_VALUES" ${SWEEP_WITH:+--try "$SWEEP_WITH"} ${REFINE:+--refine $REFINE} \
  --reason "characterization sweep: how the workload responds across the whole range of $SWEEP_VAR" || exit 1
tuning-agent experiment run --bench "$PROF" || exit 1
