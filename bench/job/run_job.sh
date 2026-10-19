#!/bin/bash
# Run the 113 JOB queries once, sequentially, one connection per query, each capped by max_execution_time.
# Usage: run_job.sh [cap_seconds] [query_dir] [db]
# Prints one line per query and a summary:
#   JOB 1a time=0.412 status=ok
#   JOB total_s=812.3 geomean_s=2.41 capped=3 errors=0 queries=113
# A capped query counts as the cap (its elapsed time), an error as its elapsed time.
CAP=${1:-90}; QDIR=${2:-/root/job/queries}; DB=${3:-imdb}
ONLY=${JOB_ONLY:-}           # optional: space-separated query names (e.g. "1a 17b") for quick tests
total=0; logsum=0; n=0; capped=0; errors=0
for q in $(ls $QDIR | grep -E '^[0-9]+[a-z]\.sql$' | sort -V); do
  name=${q%.sql}
  [ -n "$ONLY" ] && ! [[ " $ONLY " == *" $name "* ]] && continue
  t0=$(date +%s.%N)
  out=$( { echo "SET SESSION max_execution_time = $((CAP * 1000));"; cat $QDIR/$q; } | mysql -N $DB 2>&1 )
  rc=$?
  t1=$(date +%s.%N)
  dt=$(echo "$t1 - $t0" | bc -l)
  st=ok
  if [ $rc -ne 0 ]; then
    if echo "$out" | grep -q "ERROR 3024\|maximum statement execution time"; then st=capped; capped=$((capped + 1))
    else st=error; errors=$((errors + 1)); echo "JOB-ERROR $name: $(echo "$out" | head -1)"; fi
  fi
  printf 'JOB %s time=%.3f status=%s\n' $name $dt $st
  total=$(echo "$total + $dt" | bc -l); logsum=$(echo "$logsum + l($dt)" | bc -l); n=$((n + 1))
done
[ $n -gt 0 ] || { echo "no queries in $QDIR"; exit 1; }
printf 'JOB total_s=%.2f geomean_s=%.4f capped=%d errors=%d queries=%d\n' $total $(echo "e($logsum / $n)" | bc -l) $capped $errors $n
