#!/bin/bash
# Compressed-memory study (2026-11-21): can zswap or zram (lz4) let a buffer pool larger than RAM beat the best
# buffer pool that fits without them, on an I/O-bound OLTP workload?
#   workload: HammerDB TPROC-C 1000 warehouses (~100 GB, ~3x RAM), all-warehouse, 64 VU co-located, binlog off,
#             5 min ramp-up + 15 min measured, pristine data restored before every point (cold start)
#   modes:    disk  - the host's own 8 GB swap file, zswap off, swappiness 60 (how the box normally runs)
#             zswap - zswap on (lz4, zsmalloc, max_pool_percent 50) in front of a 64 GB swap file on the other SSD
#                     (/data, not the datadir's drive), swappiness 100
#             zram  - zram swap only (lz4, 32 GB device, priority 100), no disk swap, zswap off, swappiness 180
#             compressed modes also: vm.page-cluster 0, watermark_boost_factor 0, watermark_scale_factor 125 (Arch wiki)
#   WL=am (2026-11-21, second round): BenchBase AuctionMark ~100 GB instead (its pages compress ~2.9x lz4 / ~4.5x zstd,
#             HammerDB's only ~1.3x: docs/research/zstudy/), 64 terminals, 5 min warm-up + 15 min measured; modes take a
#             compressor suffix: zswap-lz4 zswap-zstd zram-lz4 zram-zstd (plain zswap / zram = lz4)
# Runs outside the tuner (OOM kills are an expected outcome). Results: /root/bench/zstudy[-am]/results.jsonl
# usage: zstudy.sh setup                      # move the canary's datadir aside, fresh datadir, build 1000 W, pristine copy
#        zstudy.sh run mode:bp[:tag] ...      # e.g. disk:24 zswap:32 zram:36:r2
#        zstudy.sh teardown                   # swap back to the host's own, the canary's datadir back, agent back
set -u
WL=${WL:-hdb}
if [ $WL = am ]; then O=/root/bench/zstudy-am; BASE=/data/zstudy_am_base; else O=/root/bench/zstudy; BASE=/data/zstudy_base; fi
mkdir -p $O                       # BASE: pristine data set (other SSD than the datadir)
AM_SF=${AM_SF:-300}               # AuctionMark scale factor: SF 5 loaded 1.66 GB (survey), so ~300 for ~100 GB
CNF=/etc/mysql/mysql.conf.d/zzz-zstudy.cnf
KEEP=/data/mysql.pre-zstudy       # the canary's own datadir while the study runs
ZSWF=/data/zstudy-swap.img        # zswap's backing swap file
HDB="cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp"
log() { echo "$(date -u +%FT%T) $*" | tee -a $O/study.log; }
q() { mysql -N -e "$1"; }

study_cnf() {  # same server settings for every point; only innodb_buffer_pool_size changes
  cat > $CNF <<EOF
[mysqld]
max_connections = 300
innodb_buffer_pool_size = ${1}G
innodb_buffer_pool_instances = 8
innodb_buffer_pool_load_at_startup = OFF
innodb_buffer_pool_dump_at_shutdown = OFF
innodb_redo_log_capacity = 8G
innodb_io_capacity = 2000
innodb_io_capacity_max = 4000
EOF
}

setup() {
  systemctl stop tuning-agent 2>/dev/null; systemctl disable tuning-agent 2>/dev/null
  systemctl stop mysql
  if [ ! -d $KEEP ]; then mv /var/lib/mysql $KEEP && mkdir /var/lib/mysql && chown mysql:mysql /var/lib/mysql && chmod 750 /var/lib/mysql; fi
  study_cnf 20
  printf '[mysqld]\ninnodb_flush_log_at_trx_commit = 0\n' > /etc/mysql/mysql.conf.d/zzzz-zstudy-build.cnf
  [ -d /var/lib/mysql/mysql ] || mysqld --initialize-insecure --user=mysql --datadir=/var/lib/mysql || { log "STUDYFAILED: initialize"; exit 1; }
  systemctl start mysql || { log "STUDYFAILED: start"; exit 1; }
  q "CREATE USER IF NOT EXISTS tpcc@localhost IDENTIFIED BY 'tpcc'; CREATE USER IF NOT EXISTS tpcc@'127.0.0.1' IDENTIFIED BY 'tpcc'; GRANT ALL ON *.* TO tpcc@localhost; GRANT ALL ON *.* TO tpcc@'127.0.0.1'"
  if [ $WL = am ]; then
    log "loading AuctionMark SF $AM_SF"
    mkdir -p /root/bench/bb-sf; echo "$AM_SF" > /root/bench/bb-sf/auctionmark.sf   # bb_run.sh picks keys from it
    BB_SF=$AM_SF bash /root/bench/benchbase/bb_load.sh auctionmark 12 > $O/build.log 2>&1 || { log "STUDYFAILED: load (see build.log)"; exit 1; }
    log "loaded: $(grep LOADED $O/build.log)"
  else
    log "building 1000 W"
    ( eval "$HDB HDB_WH=1000 HDB_BUILD_VU=8 ./hammerdbcli auto /root/bench/hdb_build200.tcl" ) > $O/build.log 2>&1
    grep -q "HDB BUILD COMPLETE" $O/build.log && ! grep -qi "error" <(tail -50 $O/build.log) || { log "STUDYFAILED: build (see build.log)"; exit 1; }
    log "built: $(q "select round(sum(data_length+index_length)/1073741824,1) from information_schema.tables where table_schema='tpcc'") GB (tables)"
  fi
  rm -f /etc/mysql/mysql.conf.d/zzzz-zstudy-build.cnf
  q "SET GLOBAL innodb_fast_shutdown = 0"; systemctl stop mysql
  rsync -a --delete /var/lib/mysql/ $BASE/
  log "pristine copy: $(du -sh $BASE | cut -f1); SETUPDONE"
}

swap_mode() {  # mysqld must be down: swapoff brings every swapped page back
  systemctl stop mysql
  swapoff -a 2>/dev/null; [ -e /dev/zram0 ] && { swapoff /dev/zram0 2>/dev/null; echo 1 > /sys/block/zram0/reset; }
  echo N > /sys/module/zswap/parameters/enabled
  # compressed modes: Arch wiki zram tuning (ChromeOS / Pop!_OS values): no swap read-ahead (page-cluster 0: buffer pool
  # pages come back one at a time at random), no watermark boost, earlier background reclaim; disk mode keeps the defaults
  if [ ${1%%-*} = disk ]; then sysctl -q vm.page-cluster=3 vm.watermark_boost_factor=15000 vm.watermark_scale_factor=10
  else sysctl -q vm.page-cluster=0 vm.watermark_boost_factor=0 vm.watermark_scale_factor=125; fi
  local kind=${1%%-*} comp=lz4; [ "$kind" != "$1" ] && comp=${1#*-}
  case $kind in
    disk)  sysctl -q vm.swappiness=60; swapon /swapfile ;;
    zswap) sysctl -q vm.swappiness=100
           [ -e $ZSWF ] || { fallocate -l 64G $ZSWF && chmod 600 $ZSWF && mkswap -q $ZSWF; }
           echo $comp > /sys/module/zswap/parameters/compressor && echo zsmalloc > /sys/module/zswap/parameters/zpool \
             && echo 50 > /sys/module/zswap/parameters/max_pool_percent && echo Y > /sys/module/zswap/parameters/enabled \
             && swapon $ZSWF || { log "STUDYFAILED: zswap"; exit 1; } ;;
    disk2) sysctl -q vm.swappiness=100   # control for zswap: the same swap file on the second SSD and sysctls, zswap off
           [ -e $ZSWF ] || { fallocate -l 64G $ZSWF && chmod 600 $ZSWF && mkswap -q $ZSWF; }
           swapon $ZSWF || { log "STUDYFAILED: disk2"; exit 1; } ;;
    zram)  sysctl -q vm.swappiness=180; modprobe zram num_devices=1   # zswap stays off (it would cache in front of zram)
           echo $comp > /sys/block/zram0/comp_algorithm && echo ${ZRAM_GB:-64}G > /sys/block/zram0/disksize && mkswap -q /dev/zram0 \
             && swapon -p 100 /dev/zram0 || { log "STUDYFAILED: zram"; exit 1; } ;;
  esac
  log "mode $1: $(swapon --noheadings --show=NAME,SIZE | tr '\n' ' ') zswap=$(cat /sys/module/zswap/parameters/enabled)/$(cat /sys/module/zswap/parameters/compressor) zram=$(cat /sys/block/zram0/comp_algorithm 2>/dev/null | grep -o '\[[a-z0-9-]*\]') swappiness=$(cat /proc/sys/vm/swappiness)"
}

snap() {  # memory + InnoDB counters at a point in time, one JSON object
  local z=/sys/kernel/debug/zswap
  python3 - <<PY
import json, subprocess, os
def st():
    out = subprocess.run(["mysql","-N","-e","show global status where variable_name in ('Innodb_data_reads','Innodb_data_writes','Innodb_buffer_pool_read_requests','Innodb_buffer_pool_reads','Innodb_buffer_pool_wait_free','Innodb_pages_written','Com_commit')"],capture_output=True,text=True).stdout
    return {l.split()[0]: int(l.split()[1]) for l in out.splitlines() if l.strip()}
mi = {l.split(':')[0]: int(l.split()[1]) for l in open('/proc/meminfo')}
vs = {l.split()[0]: int(l.split()[1]) for l in open('/proc/vmstat')}
d = {"status": st(), "mem_avail_kb": mi["MemAvailable"], "swap_used_kb": mi["SwapTotal"]-mi["SwapFree"],
     "zswap_kb": mi.get("Zswap"), "zswapped_kb": mi.get("Zswapped"), "pswpin": vs.get("pswpin"), "pswpout": vs.get("pswpout"),
     "zswpin": vs.get("zswpin"), "zswpout": vs.get("zswpout"), "zswpwb": vs.get("zswpwb")}
try: d["zram_mm_stat"] = open('/sys/block/zram0/mm_stat').read().split()
except Exception: pass
try:
    pid = subprocess.run(["pidof","mysqld"],capture_output=True,text=True).stdout.split()[0]
    s = {l.split(':')[0]: l.split()[1] for l in open(f'/proc/{pid}/status') if l.startswith(('VmRSS','VmSwap'))}
    d["mysqld_rss_kb"], d["mysqld_swap_kb"] = int(s["VmRSS"]), int(s.get("VmSwap", 0))
except Exception: pass
print(json.dumps(d))
PY
}

point() {  # point <mode> <bp GB> [tag]
  local mode=$1 bp=$2 tag=${3:-r1} id t0
  id=$mode-$bp-$tag
  study_cnf $bp
  q "SET GLOBAL innodb_fast_shutdown = 2" 2>/dev/null; systemctl stop mysql
  rsync -a --delete $BASE/ /var/lib/mysql/ || { log "STUDYFAILED: reset"; exit 1; }
  sync; echo 3 > /proc/sys/vm/drop_caches
  t0=$(date -u '+%F %T')
  if ! systemctl start mysql; then
    echo "{\"id\":\"$id\",\"mode\":\"$mode\",\"bp_gb\":$bp,\"outcome\":\"start failed\"}" >> $O/results.jsonl; log "$id: mysqld did not start"; return 2
  fi
  vmstat -n 10 > $O/vmstat-$id.log & local vm=$!
  iostat -dxm 10 sda sdb > $O/iostat-$id.log 2>/dev/null & local io=$!
  ( sleep 300; snap > $O/snap0-$id.json ) & local s0=$!      # the end of the 5-minute ramp-up / warm-up
  if [ $WL = am ]; then
    bash /root/bench/benchbase/bb_run.sh auctionmark 300 900 64 > $O/bb-$id.log 2>&1
  else
    rm -f /tmp/hdbxtprofile.log
    ( eval "$HDB HDB_VU=64 HDB_ALLWH=true HDB_RAMPUP=5 HDB_DURATION=15 ./hammerdbcli auto /root/bench/hdb_run.tcl" ) > $O/hdb-$id.log 2>&1
  fi
  snap > $O/snap1-$id.json
  kill $vm $io $s0 2>/dev/null; cp /tmp/hdbxtprofile.log $O/prof-$id.log 2>/dev/null
  local oom; oom=$(journalctl -k --since "$t0" --no-pager 2>/dev/null | grep -ciE "out of memory: killed process [0-9]+ \(mysqld\)|oom-kill.*mysqld")
  python3 /root/bench/bm2/zstudy_point.py "$O" "$id" "$mode" "$bp" "$oom" >> $O/results.jsonl
  log "$id: $(tail -1 $O/results.jsonl)"
}

case ${1:-} in
  setup) setup ;;
  run) shift; cur=""
       for p in "$@"; do
         IFS=: read -r mode bp tag <<< "$p"
         [ "$mode" != "$cur" ] && { swap_mode $mode; cur=$mode; oomed=""; }
         [ -n "$oomed" ] && { log "$mode-$bp: skipped (a smaller pool was OOM-killed in this mode)"; continue; }
         point $mode $bp ${tag:-r1}
         tail -1 $O/results.jsonl | grep -q '"outcome": "oom-killed"\|start failed' && oomed=1
       done; log "RUNDONE $*" ;;
  teardown)
       rm -f $CNF /etc/mysql/mysql.conf.d/zzzz-zstudy-build.cnf; systemctl stop mysql
       swapoff -a 2>/dev/null; [ -e /dev/zram0 ] && { swapoff /dev/zram0 2>/dev/null; echo 1 > /sys/block/zram0/reset; rmmod zram; }
       echo N > /sys/module/zswap/parameters/enabled; echo lzo > /sys/module/zswap/parameters/compressor
       echo zbud > /sys/module/zswap/parameters/zpool; echo 20 > /sys/module/zswap/parameters/max_pool_percent
       sysctl -q vm.swappiness=60 vm.page-cluster=3 vm.watermark_boost_factor=15000 vm.watermark_scale_factor=10; rm -f $ZSWF; swapon -a
       if [ -d $KEEP ]; then rm -rf /var/lib/mysql && mv $KEEP /var/lib/mysql; fi
       systemctl start mysql && systemctl enable tuning-agent && systemctl start tuning-agent
       log "TEARDOWNDONE (pristine copy kept in $BASE)" ;;
  *) echo "usage: $0 setup | run mode:bp[:tag]... | teardown"; exit 1 ;;
esac
