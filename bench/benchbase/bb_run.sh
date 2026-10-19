#!/bin/bash
# bb_run.sh <workload> <warmup s> <duration s> [terminals]: one BenchBase measurement run for a tuner profile.
# Renders /root/bench/benchbase/<workload>.xml.tpl (@WARMUP@ @TIME@ @TERMINALS@) into a per-run config, runs BenchBase with
# --execute=true (one work phase: <warmup> seconds unmeasured, then <time> measured seconds), then prints the parseable
#   BBSUMMARY ... throughput_rps=.. goodput_rps=.. p95_ms=.. p99_ms=..
# line from the run's summary.json (bb_summary.py). Per-request raw files are removed; summaries stay in results/<workload>-<ts>.
W=$1; WARM=${2:-60}; DUR=${3:-300}; TERM=$4
D=/root/bench/benchbase; T=$D/$W.xml.tpl
[ -f "$T" ] || { echo "no template $T"; exit 2; }
BENCH=$(sed -n 's/.*bb-bench: *\([a-z]*\).*/\1/p' "$T" | head -1)
[ -n "$TERM" ] || TERM=$(sed -n 's/.*bb-terminals: *\([0-9]*\).*/\1/p' "$T" | head -1)
TS=$(date +%Y%m%d-%H%M%S); OUT=$D/results/$W-$TS; mkdir -p "$OUT"
sed -e "s/@WARMUP@/$WARM/g" -e "s/@TIME@/$DUR/g" -e "s/@TERMINALS@/${TERM:-64}/g" "$T" > "$OUT/config.xml"
echo "BenchBase $BENCH ($W): $TERM terminals, ${WARM}s warm-up + ${DUR}s measured; results $OUT"
cd /opt/benchbase || exit 2
ulimit -n 65535
# heap: latency samples are kept in memory (~40 B per request); 3 GB covers ~50M requests. The co-located client is
# why sessions use BASELINE_ARGS="--reserve-mem 4G".
java ${BB_JAVA_OPTS:--Xms1g -Xmx3g} -jar benchbase.jar -b "$BENCH" -c "$OUT/config.xml" --create=false --load=false --execute=true \
  -d "$OUT" 2>&1 | grep -v "^\s*at \|DEBUG" | tail -n 40
rc=${PIPESTATUS[0]}
rm -f "$OUT"/*.raw.csv "$OUT"/*.samples.csv 2>/dev/null
python3 -I $D/bb_summary.py "$OUT" || exit 1
# keep the 200 newest result dirs
ls -1dt $D/results/*/ 2>/dev/null | tail -n +201 | while read -r old; do rm -f "$old"/* && rmdir "$old"; done
exit $rc
