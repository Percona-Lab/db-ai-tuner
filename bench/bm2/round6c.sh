#!/bin/bash
# Round 6 part C rerun (2026-11-25): repeats of round 5 at 48 GB of tables on a fresh datadir. The first attempt ran on
# the datadir MyRocks had used (plugin removed, its tables left behind): the load failed and every run was empty.
# Runs after round 6 (its teardown restores the canary; setup here initializes a fresh study datadir), then tears down.
set -u
Z=/root/bench/bm2/zstudy6.sh
O=/root/bench/zstudy-sbz
log() { echo "$(date -u +%FT%T) $*" | tee -a $O/round6.log; }
die() { log "ROUND6CFAILED: $*"; exit 1; }
until grep -q -E "ROUND6DONE|ROUND6FAILED" $O/round6.log 2>/dev/null; do sleep 120; done
log "round 6 part C rerun"
systemctl stop mysql
rm -rf /data/mysql.pre-zstudy.c6 2>/dev/null
fstrim -v / | tee -a $O/round6.log; fstrim -v /data | tee -a $O/round6.log     # SSD state: see the round 4 slow-write period
export WL=sbz SBZ_ROWS=15000000 SBZ_WARM=1200 ZSWF_GB=80
$Z setup || die "setup 15M"
gb=$(grep "loaded:" $O/study.log | tail -1 | sed 's/.*loaded: \([0-9.]*\) GB.*/\1/')
python3 -c "import sys; sys.exit(0 if float('${gb:-0}') > 40 else 1)" || die "load too small (${gb:-none} GB)"
$Z run disk:26:r15m-r2b disk2:53:r15m-r2b
ZRAM_GB=128 $Z run zram:53:r15m-r2b
ZRAM_GB=32 $Z run zramdisk:53:r15m-r2b
ZSWAP_POOL_PCT=32 $Z run zswap-lz4:53:r15m-r2b
log "teardown"
$Z teardown
rm -rf /data/zstudy_sbz_base
log "ROUND6CDONE"
