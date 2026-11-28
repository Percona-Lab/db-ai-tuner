#!/bin/bash
# pfcal.sh: page-fault calibration under each swap setup (v2, 2026-11-28). Root; mysqld instances may keep running.
# usage: pfcal.sh <out dir> <swap file path on the data disk> [size MB]
# Content files: /root/pfcal/content-z10.ibd (sysbench --data-random-pct=10, ~3x lz4) and content-z100.ibd (stock).
set -u
O=$1; SWF=$2; MB=${3:-2048}; B=/root/pfcal
mkdir -p $O
gcc -O2 -o $B/pfcal $B/pfcal.c || exit 1
reset_swap() {
  swapoff -a 2>/dev/null; [ -e /dev/zram0 ] && { swapoff /dev/zram0 2>/dev/null; echo 1 > /sys/block/zram0/reset; }
  echo N > /sys/module/zswap/parameters/enabled
}
setup() {  # setup <mode>
  reset_swap
  sysctl -q vm.page-cluster=0 vm.swappiness=100
  case $1 in
    disk)      swapon $SWF ;;
    zram-lz4|zram-zstd)
               modprobe zram num_devices=1; echo ${1#zram-} > /sys/block/zram0/comp_algorithm
               echo 8G > /sys/block/zram0/disksize; mkswap -q /dev/zram0; swapon -p 100 /dev/zram0 ;;
    zswap-lz4|zswap-zstd)
               echo ${1#zswap-} > /sys/module/zswap/parameters/compressor; echo zsmalloc > /sys/module/zswap/parameters/zpool
               echo 50 > /sys/module/zswap/parameters/max_pool_percent; echo N > /sys/module/zswap/parameters/shrinker_enabled
               echo Y > /sys/module/zswap/parameters/enabled; swapon $SWF ;;
  esac
}
[ -e $SWF ] || { fallocate -l 16G $SWF && chmod 600 $SWF && mkswap -q $SWF; }
HOSTINFO="$(hostname) | $(lscpu | sed -n 's/^Model name: *//p' | head -1) | $(uname -r) | swap file on $(df --output=source $SWF | tail -1)"
echo "$HOSTINFO" > $O/host.txt
for content in ${CONTENTS:-z10 z100}; do
  for mode in ${MODES:-disk zram-lz4 zram-zstd zswap-lz4 zswap-zstd}; do
    setup $mode
    $B/pfcal $MB $B/content-$content.ibd 3 | sed "s/^{/{\"mode\":\"$mode\",\"content\":\"$content\",/" >> $O/pfcal.jsonl
    echo "$mode/$content: $(grep "\"mode\":\"$mode\",\"content\":\"$content\".*swapped\"" $O/pfcal.jsonl | tail -1)"
  done
done
reset_swap; swapon -a; sysctl -q vm.page-cluster=3 vm.swappiness=60
echo 20 > /sys/module/zswap/parameters/max_pool_percent; echo Y > /sys/module/zswap/parameters/shrinker_enabled
echo lzo > /sys/module/zswap/parameters/compressor; echo zbud > /sys/module/zswap/parameters/zpool
echo PFCALDONE
