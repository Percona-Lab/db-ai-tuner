#!/bin/bash
# Swap study: buffer pool size stepped up past RAM with and without swap, data 3x RAM, plain sysbench (read-only).
#  - safety margin: at which pool size does mysqld get OOM-killed without swap?
#  - cost of unused swap: same pool sizes below the limit, swap on vs off
#  - benefit: does a pool beyond RAM with swap ever beat the best size without it?
# Runs outside the tuner (OOM kills are an expected outcome). Results: /root/bench/swapstudy/results.jsonl
# usage: [SB_TEST=oltp_read_write] [OUT=dir] swap-study.sh [dist:swap ...]   e.g. uniform:off uniform:on (default: special and uniform, swap off then on)
set -u
O=${OUT:-/root/bench/swapstudy}; mkdir -p $O
TEST=${SB_TEST:-oltp_read_only}   # sysbench test of the measured runs (the data is the same for all oltp_* tests)
CNF=/etc/mysql/mysql.conf.d/zzz-swapstudy.cnf
SWF=/swapstudy.img
SB="--mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=swapstudy"
RAM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1048576 ))
log() { echo "$(date -u +%FT%T) $*" | tee -a $O/study.log; }

tuning-agent session stop >/dev/null 2>&1 || true
tuning-agent rollback --reset >/dev/null 2>&1 || true
printf '[mysqld]\nmax_connections = 300\ninnodb_buffer_pool_instances = 8\ninnodb_buffer_pool_load_at_startup = OFF\ninnodb_buffer_pool_dump_at_shutdown = OFF\n' > $CNF
systemctl restart mysql
mysql -e "CREATE DATABASE IF NOT EXISTS swapstudy; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON *.* TO sbtest@localhost"
# data about 3x RAM: 16 tables, rows sized from RAM (~230 bytes per row incl. index)
ROWS=$(( RAM_GB * 3 * 1073741824 / 230 / 16 ))
if [ "$(mysql -N -e "select count(*) from information_schema.tables where table_schema='swapstudy'")" -lt 16 ]; then
  log "preparing 16 x $ROWS rows (~$(( RAM_GB * 3 )) GB)"
  sysbench oltp_read_only $SB --tables=16 --table-size=$ROWS --threads=16 prepare > $O/prepare.log 2>&1 || { log "STUDYFAILED: prepare"; exit 1; }
fi
log "data: $(mysql -N -e "select round(sum(data_length+index_length)/1073741824,1) from information_schema.tables where table_schema='swapstudy'") GB, RAM ${RAM_GB} GB, swappiness $(cat /proc/sys/vm/swappiness)"

swap_mode() {  # off | on (a swap file of about 1x RAM; the host's own swap partition stays off during the study)
  # mysqld first: swapoff has to bring every swapped page back into RAM, and with a large pool there is no room (the
  # first version's "off" phase silently kept 30 GB of swap)
  systemctl stop mysql
  swapoff -a || { log "STUDYFAILED: swapoff"; exit 1; }
  rm -f $SWF
  if [ "$1" = on ]; then
    fallocate -l ${RAM_GB}G $SWF && chmod 600 $SWF && mkswap -q $SWF && swapon $SWF || { log "STUDYFAILED: swap file"; exit 1; }
  fi
  local tot; tot=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
  if { [ "$1" = off ] && [ "$tot" -ne 0 ]; } || { [ "$1" = on ] && [ "$tot" -eq 0 ]; }; then log "STUDYFAILED: swap $1 not in effect ($tot kB)"; exit 1; fi
  log "swap $1: $(awk '/SwapTotal/ {print $2/1048576 " GB"}' /proc/meminfo)"
  systemctl start mysql
}

point() {  # point <distribution> <swap> <bp GB>
  local dist=$1 sw=$2 bp=$3 t0 res
  sed -i '/^innodb_buffer_pool_size/d' $CNF; echo "innodb_buffer_pool_size = ${bp}G" >> $CNF
  t0=$(date -u '+%F %T')
  sync; echo 3 > /proc/sys/vm/drop_caches
  if ! systemctl restart mysql; then
    echo "{\"dist\":\"$dist\",\"swap\":\"$sw\",\"bp_gb\":$bp,\"outcome\":\"start failed\"}" >> $O/results.jsonl; log "$dist/$sw/${bp}G: mysqld did not start"; return 2
  fi
  vmstat -n 10 > $O/vmstat-$dist-$sw-$bp.log & local vm=$!
  sysbench $TEST $SB --tables=16 --table-size=$ROWS --threads=64 --rand-type=$dist --time=900 --report-interval=10 \
    --mysql-ignore-errors=all run > $O/sb-$dist-$sw-$bp.log 2>&1
  local rss=$(ps -o rss= -C mysqld | head -1); local swp=$(awk '/VmSwap/ {print $2}' /proc/$(pidof mysqld | cut -d' ' -f1)/status 2>/dev/null)
  kill $vm 2>/dev/null
  local oom=$(journalctl -k --since "$t0" --no-pager 2>/dev/null | grep -ciE "out of memory: killed process [0-9]+ \(mysqld\)|oom-kill.*mysqld")
  res=$(python3 - "$O/sb-$dist-$sw-$bp.log" "$O/vmstat-$dist-$sw-$bp.log" <<'PY'
import re, sys, json
iv = [(float(m.group(1)), float(m.group(2)), float(m.group(3))) for m in
      re.finditer(r"\[ (\d+)s \].*?tps: ([\d.]+).*?lat \(ms,95%\): ([\d.]+)", open(sys.argv[1]).read())]
win = [x for x in iv if x[0] > 600]  # the last 5 minutes: after a 10-minute warm-up
vm = [l.split() for l in open(sys.argv[2]) if l.strip() and l.split()[0].isdigit()][1:]
si = [int(x[6]) for x in vm[-30:]] if vm else []
so = [int(x[7]) for x in vm[-30:]] if vm else []
out = {"tps": round(sum(t for _, t, _ in win) / len(win), 1) if win else None,
       "p95_ms": round(sorted(p for *_, p in win)[len(win) // 2], 2) if win else None,
       "tps_min_10s": min((t for _, t, _ in win), default=None),
       "swap_in_kbs": round(sum(si) / len(si), 1) if si else None, "swap_out_kbs": round(sum(so) / len(so), 1) if so else None}
print(json.dumps(out))
PY
)
  echo "{\"dist\":\"$dist\",\"swap\":\"$sw\",\"bp_gb\":$bp,\"outcome\":\"$([ "$oom" -gt 0 ] && echo oom-killed || echo ok)\",\"mysqld_rss_gb\":$(python3 -c "print(round(${rss:-0}/1048576,2))"),\"mysqld_swap_gb\":$(python3 -c "print(round(${swp:-0}/1048576,2))"),\"m\":$res}" >> $O/results.jsonl
  log "$dist/$sw/${bp}G: $(tail -1 $O/results.jsonl)"
  [ "$oom" -gt 0 ] && return 1 || return 0
}

# phases: arguments like "uniform:off uniform:on" (default: both distributions, swap off then on)
PHASES="${*:-special:off special:on uniform:off uniform:on}"
for ph in $PHASES; do
  dist=${ph%%:*}; sw=${ph##*:}
  {
    swap_mode $sw
    sizes="12 16 20 24 26 28 30"; [ $sw = on ] && sizes="$sizes 32 34 36"
    ooms=0
    for bp in $sizes; do
      point $dist $sw $bp; rc=$?
      [ $rc -ne 0 ] && ooms=$((ooms + 1))
      [ $ooms -ge 2 ] && { log "$dist/$sw: stopping after 2 failures (OOM / no start)"; break; }
    done
  }
done
# back to the host's own state
swapoff $SWF 2>/dev/null; rm -f $SWF; swapon -a
rm -f $CNF; systemctl restart mysql
log "STUDYDONE"
