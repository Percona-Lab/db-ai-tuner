#!/bin/bash
# micro.sh <suite>/<case>: a mechanism micro-benchmark (docs/design/micro-benchmarks.md) - one mechanism made dominant,
# its setting swept, the expectations of bench/micro/<suite>/<case>.json checked by bench/micro/check.py
CASE=$1
J=/root/bench/micro/$CASE.json
. /root/bench/pipeline/lib.sh
set -x
[ -f $J ] || { echo "QUEUEFAILED: no case $J"; exit 1; }
ensure_ps84
eval "$(python3 - $J <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))
w, d = c["workload"], c["dataset"]
for k, v in (("T", d["tables"]), ("R", d["rows"]), ("TEST", w["test"]), ("TH", w["threads"]), ("RND", w.get("rand", "uniform")),
             ("WU", c["warmup"]), ("DU", c["duration"]), ("GOAL", c.get("goal", "tps")), ("EXTRA", w.get("extra", "")),
             ("FIXED", " ".join(f"{k}={v}" for k, v in (c.get("fixed") or {}).items())), ("MECH", c["mechanism"]), ("ROLE", c["role"])):
    print(f"{k}={shlex.quote(str(v))}")
PY
)"
dataset $T $R || exit 1
P=/root/bench/pipe-micro-${CASE//\//-}.ini
sb_profile $P "micro $CASE: $MECH, $ROLE case - sysbench $TEST ${T}x${R}, $TH threads, $RND ($(hostname))" $TEST $T $R $TH $RND $WU $DU $EXTRA
export SKIP_DEFAULTS=1 AI_OFF=1 AFTER_BASELINE="$FIXED" MICRO_CASE=$J POST=/root/bench/pipeline/items/micro-post.sh
STUDY="mechanism micro-benchmarks" run_session $P "micro $CASE ($(hostname))" $GOAL 0
grep -q '^micro-benchmark .*: PASS' /root/bench/session-$(basename $P .ini).log && echo "MICRO PASS" || echo "MICRO NOT PASSED"
echo PIPEOK
