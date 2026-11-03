#!/bin/bash
# benchbase.sh <workload> <target GB | sf=N> <terminals> [max runs]: an autotune session on a BenchBase workload
# (bench/benchbase/<workload>.xml.tpl; requested 2026-11-03: SEATS, AuctionMark, Epinions, Voter) - Percona Server 8.4 like
# the matrix cells, rules + learnings + similarity, production-safe first, then trade-offs.
# Data: BenchBase scale factors scale some tables only, so the size per unit is measured, not assumed: a probe load
# (grown 10x until it is >= 0.5 GB or takes 10 min), then the scale factor for the target size, capped so the load takes
# <= LOAD_MAX_S. The chosen one is kept in /root/bench/bb-sf/<workload>.sf ("sf target"), reused while the database is there.
# Every run starts from the pristine copy (reset_datadir.sh); other bb_* databases are dropped first (reloadable, and the
# copy is the whole datadir).
W=$1; TARGET=$2; TERM=$3; MAX=${4:-16}
. /root/bench/pipeline/lib.sh
set -x
LOAD_MAX_S=${LOAD_MAX_S:-5400}
D=/root/bench/benchbase; DB=bb_$W; SFF=/root/bench/bb-sf/$W.sf
ensure_ps84
[ -f /opt/benchbase/benchbase.jar ] || step bash $D/install.sh
mkdir -p /root/bench/bb-sf
size_gb() { mysql -N -e "SELECT ROUND(COALESCE(SUM(data_length+index_length),0)/1073741824,3) FROM information_schema.tables WHERE table_schema='$DB'"; }
load() {  # load <sf>: prints "seconds size_gb"
  BB_SF=$1 $D/bb_load.sh $W 12 > $D/load-$W.log 2>&1 || { tail -20 $D/load-$W.log; echo "QUEUEFAILED: load $W sf=$1"; exit 1; }
  sed -n 's/.*LOADED .* seconds=\([0-9]*\).*/\1/p' $D/load-$W.log | tail -1 | tr '\n' ' '; size_gb
}
have=$(mysql -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB'")
if [ "$have" -gt 0 ] && [ "$(cut -d' ' -f2 $SFF 2>/dev/null)" = "$TARGET" ]; then
  echo "data: $DB present at sf $(cut -d' ' -f1 $SFF) (target $TARGET)"
else
  tuning-agent session stop >/dev/null 2>&1 || true
  tuning-agent rollback --reset || true
  for o in $(mysql -N -e "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'bb\_%' AND schema_name <> '$DB'"); do
    mysql -e "DROP DATABASE $o"; echo "dropped $o (reloadable BenchBase data set)"
  done
  mysql -e "SET GLOBAL innodb_flush_log_at_trx_commit = 0"  # the load only; the next restart restores the configured value
  case $TARGET in
    sf=*) SF=${TARGET#sf=}; read -r secs gb <<< "$(load $SF)"; echo "LOAD $W sf=$SF ${gb} GB in ${secs}s" ;;
    *) SF=${PROBE_SF:-1}
       while :; do
         read -r secs gb <<< "$(load $SF)"; echo "PROBE $W sf=$SF ${gb} GB in ${secs}s"
         python3 -c "import sys; sys.exit(0 if float('$gb') >= 0.5 or int('${secs:-0}') >= 600 else 1)" && break
         SF=$(python3 -c "print(round($SF * 10, 3))")
       done
       NEW=$(python3 -c "
gb, s, sf, t, cap = float('$gb'), max(int('${secs:-1}'), 1), float('$SF'), float('$TARGET'), $LOAD_MAX_S
x = min(sf * t / max(gb, 1e-3), sf * cap / s)
print(int(x) if x >= 10 else round(x, 2))")
       echo "scale factor for ~$TARGET GB: $NEW (load time capped at ${LOAD_MAX_S}s)"
       if [ "$NEW" != "$SF" ]; then SF=$NEW; read -r secs gb <<< "$(load $SF)"; fi
       echo "LOAD $W sf=$SF ${gb} GB in ${secs}s" ;;
  esac
  mysql -e "SET GLOBAL innodb_flush_log_at_trx_commit = 1"
  echo "$SF $TARGET" > $SFF
  rm -f /var/lib/mysql_base.dataset  # the pristine copy must include it: dataset() makes a new one
fi
dataset 1 10000
GB=$(size_gb)
P=/root/bench/pipe-bb-$W.ini
NAME=$(sed -n 's/.*bb-bench: *\([a-z]*\).*/\1/p' $D/$W.xml.tpl | head -1)
cat > $P <<INI
[bench]
name = BenchBase $NAME sf $(cut -d' ' -f1 $SFF) (~$GB GB), $TERM terminals ($(hostname))
reset = /root/bench/reset_datadir.sh
run = /root/bench/benchbase/bb_run.sh $W {warmup} {duration} $TERM
warmup = 180
duration = 300
timeout = 2400
goal_metric = bb_tps
data_state = reset

[metrics]
bb_tps = BBSUMMARY .* throughput_rps=([\d.]+)
bb_avg_ms = BBSUMMARY .* avg_ms=([\d.]+)
bb_p95_ms = BBSUMMARY .* p95_ms=([\d.]+)
bb_p99_ms = BBSUMMARY .* p99_ms=([\d.]+)

[requires]
max_connections = $((TERM + 100))
INI
export BASELINE_ARGS="--reserve-mem 4G"  # the BenchBase JVM (-Xmx3g) runs on the same host
STUDY="BenchBase workloads 2026-11-03" run_session $P "BenchBase $NAME ($(hostname))" tps $MAX
grep -h "^Session #" /root/bench/session-pipe-bb-$W.log
echo PIPEOK
