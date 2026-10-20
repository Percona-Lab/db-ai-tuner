#!/bin/bash
# One Insert Benchmark step with N clients (one table per client), the way mdcallag's np.sh runs it, but time-limited
# so the tuning agent can measure a fixed window.
#
#   ibench_run.sh write <clients> <warmup_s> <duration_s> [rows_per_commit]
#       l.i1-style (rows_per_commit 50) or l.i2-style (5): inserts as fast as possible, a second connection per client
#       deletes from the other end of the table at the same rate (--delete_per_insert), 3 secondary indexes.
#   ibench_run.sh query <clients> <warmup_s> <duration_s> <inserts_per_s_per_client> <range|point>
#       qrN / qpN: one query connection per client as fast as possible, background inserts and deletes rate-limited
#       to N/s per client (50 rows per commit).
#
# Each client runs for warmup + duration (+5 s) seconds (iibench --max_seconds). Afterwards ibench_summary.py prints the
# metrics of the measured window [warmup, warmup + duration] only. A sampler records InnoDB history list length,
# dirty pages, bp wait_free and iowait every 5 s for the bottleneck summary.
MODE=$1; N=$2; W=$3; D=$4
D_IB=/root/bench/ibench
OUT=${IBENCH_OUT:-/root/bench/ibench/last}
SOCK=${IBENCH_SOCKET:-/var/run/mysqld/mysqld.sock}
SEED=${IBENCH_SEED:-1733776944}          # fixed seeds: every run inserts the same values (Mark uses start time + client)
TOTAL=$(( W + D + 5 ))
[ -n "$MODE" ] && [ -n "$N" ] && [ -n "$W" ] && [ -n "$D" ] || { echo "usage: $0 write|query clients warmup duration ..."; exit 2; }

mkdir -p "$OUT"; rm -f "$OUT"/o.ib.* "$OUT"/samples.tsv
common="--dbms=mysql --db_name=ib --secs_per_report=1 --db_host=localhost --db_user=ib --db_password=ib --db_socket=$SOCK
 --engine=innodb --engine_options= --unique_checks=1 --bulk_load=0 --delete_per_insert --max_rows=2000000000 --max_seconds=$TOTAL
 --num_secondary_indexes=3 --data_length_min=10 --data_length_max=20 --dbopt=none"
case "$MODE" in
  write) RPC=${5:-50}; extra="--rows_per_commit=$RPC --inserts_per_second=0 --query_threads=0" ;;
  query) IPS=$5; KIND=${6:-range}; [ -n "$IPS" ] || { echo "query mode needs inserts/s per client"; exit 2; }
         extra="--rows_per_commit=50 --inserts_per_second=$IPS --query_threads=1"
         [ "$KIND" = point ] && extra="$extra --query_pk_only" ;;
  *) echo "mode must be write or query"; exit 2 ;;
esac

# sampler: server state every 5 s (t, history list, dirty pages, total pages, wait_free, data reads, data writes,
# checkpoint age, checkpoint max age, change buffer size (pages), iowait jiffies, total jiffies)
sampler() {
  local t0=$(date +%s)
  while :; do
    local now=$(date +%s)
    local s=$(mysql -N -B -e "SELECT
        (SELECT count FROM information_schema.innodb_metrics WHERE name='trx_rseg_history_len'),
        (SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_buffer_pool_pages_dirty'),
        (SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_buffer_pool_pages_total'),
        (SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_buffer_pool_wait_free'),
        (SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_data_reads'),
        (SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_data_writes'),
        IFNULL((SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_checkpoint_age'), 0),
        IFNULL((SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_checkpoint_max_age'), 0),
        IFNULL((SELECT count FROM information_schema.innodb_metrics WHERE name='ibuf_size'), 0)" 2>/dev/null | tr '\t' ' ')
    local cpu=$(awk '/^cpu /{print $6, $2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat)
    [ -n "$s" ] && echo "$((now - t0)) $s $cpu" >> "$OUT/samples.tsv"
    sleep 5
  done
}
sampler & SPID=$!
# change buffer (INSERT BUFFER section) at the start of the measured window and at the end
ibuf_snap() { mysql -N -B --raw -e "SHOW ENGINE INNODB STATUS" 2>/dev/null | sed -n '/^INSERT BUFFER AND ADAPTIVE HASH INDEX/,/^Hash table/p' > "$OUT/$1"; }
rm -f "$OUT"/ibuf.*
( sleep $W; ibuf_snap ibuf.start ) & IPID=$!
trap 'kill $SPID 2>/dev/null; kill $(jobs -p) 2>/dev/null' EXIT

pids=()
for n in $(seq 1 $N); do
  python3 $D_IB/iibench.py $common $extra --table_name=pi$n --seed=$(( SEED + n )) --my_id=$n > "$OUT/o.ib.$n" 2>&1 &
  pids+=($!)
done
rc=0
for p in "${pids[@]}"; do wait $p || rc=1; done
kill $SPID 2>/dev/null
ibuf_snap ibuf.end
echo "ibench mode=$MODE clients=$N warmup=$W duration=$D ${RPC:+rows_per_commit=$RPC} ${IPS:+inserts_per_s_per_client=$IPS kind=$KIND}"
for n in $(seq 1 $N); do grep -h -A1 "Insert rt:\|Delete rt:\|Query rt:" "$OUT/o.ib.$n" | grep -v "^--" | sed "s/^/client$n /" | head -6; done
grep -h "Traceback\|Error\|error" "$OUT"/o.ib.* | sort | uniq -c | head -5
python3 -I $D_IB/ibench_summary.py "$OUT" $N $W $D ${IPS:-0}
exit $rc
