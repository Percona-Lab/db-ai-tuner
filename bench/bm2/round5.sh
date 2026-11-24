#!/bin/bash
# Round 5 of the zram/zswap study (2026-11-24): data size sweep. The same compressible sysbench data (WL=sbz,
# --data-random-pct=10, ~3.3x lz4) at 36 / 49 / 62 / 75 GB of tables (16 x 11M / 15M / 19M / 23M rows), loaded fresh
# per size, uniform access. Per size:
#   disk:26        normal swap, the best fixed pool that fits (baseline)
#   disk2:B        swap alone: a pool covering the data, 80 GB swap file on the 2nd SSD
#   zram:B         zram alone, device large enough for everything (128 GB): expected to OOM once compressed data > RAM
#   zramdisk:B     zram + swap: zram device 32 GB (~10 GB RAM at ~3.3x) at priority 100, the swap file at priority 10
#   zswap-lz4:B    zswap + swap: max_pool_percent 32 (~10 GB RAM, the same compressed budget), the same swap file
# B = 1.1 x the loaded table size. A mode that is OOM-killed is skipped at larger sizes.
# Swap devices are chosen by free slots, not RAM: zram only falls back to the lower-priority device when its disksize is
# full, so disksize IS zram's memory budget (disksize / ratio).
# While it runs, the canary's pipeline copies (/data/canary_base, /data/pipeline_base) live on the system disk.
# usage: round5.sh   (log: /root/bench/zstudy-sbz/round5.log)
set -u
export WL=sbz SBZ_WARM=1200 ZSWF_GB=80
Z=/root/bench/bm2/zstudy5.sh
O=/root/bench/zstudy-sbz
KEEP=/var/lib/zkeep5
log() { echo "$(date -u +%FT%T) $*" | tee -a $O/round5.log; }
die() { log "ROUND5FAILED: $*"; exit 1; }
oomed() { grep -q "\"mode\": \"$1\".*\"outcome\": \"oom-killed\"" $O/results.jsonl 2>/dev/null || \
          grep -q "\"mode\": \"$1\".*\"outcome\": \"start failed\"" $O/results.jsonl 2>/dev/null; }
# wait on round 4's own log, not its unit state (a transient systemctl failure ended the first queue at 10:00 on 11-24)
until grep -q -E "ROUND4DONE|ROUND4FAILED" $O/round4.log 2>/dev/null; do sleep 120; done
grep -q ROUND4DONE $O/round4.log || die "round 4 failed (ROUND4FAILED in round4.log)"
log "round 5: data size sweep"
mkdir -p $KEEP
for d in canary_base pipeline_base; do [ -d /data/$d ] && { mv /data/$d $KEEP/ 2>/dev/null || { rsync -a /data/$d $KEEP/ && rm -rf /data/$d; } || die "move $d"; }; done
for rows in 11000000 15000000 19000000 23000000; do
  tag=r$(( rows / 1000000 ))m
  SBZ_ROWS=$rows $Z setup || die "setup $rows"
  gb=$(grep "loaded:" $O/study.log | tail -1 | sed 's/.*loaded: \([0-9.]*\) GB.*/\1/')
  B=$(python3 -c "import math; print(math.ceil($gb * 1.1))")
  log "rows $rows: $gb GB of tables, big pool $B GB"
  export SBZ_ROWS=$rows
  $Z run disk:26:$tag disk2:$B:$tag
  oomed zram-lz4 || oomed zram || ZRAM_GB=128 $Z run zram:$B:$tag
  ZRAM_GB=32 $Z run zramdisk:$B:$tag
  ZSWAP_POOL_PCT=32 $Z run zswap-lz4:$B:$tag
  unset SBZ_ROWS
done
log "teardown"
systemctl stop mysql
for d in canary_base pipeline_base; do [ -d $KEEP/$d ] && { rsync -a $KEEP/$d /data/ && rm -rf $KEEP/$d || die "move $d back"; }; done
rmdir $KEEP 2>/dev/null
$Z teardown
rm -rf /data/zstudy_sbz_base
log "ROUND5DONE"
