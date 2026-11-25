#!/bin/bash
# Round 7 (2026-11-25): zswap with its shrinker off (shrinker_enabled=N; every earlier zswap run used the default Y,
# which writes cold pages back to disk long before the pool is full). After round 6 part C.
#   40 GB of tables (round 3 setup): references zram lz4 and zswap lz4 (shrinker on) in the same session, then
#   zswap lz4 shrinker off at 40 and 44 GB pools (max_pool_percent 50)
#   48 GB of tables (round 5 setup): zswap + swap shrinker off with the same ~10 GB budget (32%), and with 50%
set -u
Z=/root/bench/bm2/zstudy7.sh
O=/root/bench/zstudy-sbz
log() { echo "$(date -u +%FT%T) $*" | tee -a $O/round7.log; }
die() { log "ROUND7FAILED: $*"; exit 1; }
loaded_ok() { gb=$(grep "loaded:" $O/study.log | tail -1 | sed 's/.*loaded: \([0-9.]*\) GB.*/\1/'); python3 -c "import sys; sys.exit(0 if float('${gb:-0}') > $1 else 1)" || die "load too small (${gb:-none} GB)"; }
until grep -q -E "ROUND6CDONE|ROUND6CFAILED" $O/round6.log 2>/dev/null; do sleep 120; done
log "round 7: zswap shrinker off"
systemctl stop mysql; fstrim -v / | tee -a $O/round7.log; fstrim -v /data | tee -a $O/round7.log
export WL=sbz SBZ_ROWS=11000000 SBZ_WARM=900 ZSWF_GB=64
$Z setup || die "setup 11M"; loaded_ok 30
ZRAM_GB=64 $Z run zram:40:r7
ZSWAP_POOL_PCT=50 ZSWAP_SHRINKER=Y $Z run zswap-lz4:40:r7
ZSWAP_POOL_PCT=50 ZSWAP_SHRINKER=N $Z run zswap-lz4:40:nos zswap-lz4:44:nos
export SBZ_ROWS=15000000 SBZ_WARM=1200 ZSWF_GB=80
$Z setup || die "setup 15M"; loaded_ok 40
ZSWAP_POOL_PCT=32 ZSWAP_SHRINKER=N $Z run zswap-lz4:53:r15m-nos
ZSWAP_POOL_PCT=50 ZSWAP_SHRINKER=N $Z run zswap-lz4:53:r15m-nos50
log "teardown"
$Z teardown
rm -rf /data/zstudy_sbz_base
log "ROUND7DONE"
