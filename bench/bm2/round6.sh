#!/bin/bash
# Round 6 of the zram/zswap study (2026-11-25), after round 5:
#   A  InnoDB ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8 with the fastest zlib level (innodb_compression_level=1),
#      24 / 26 GB pool, plus a repeat of level 6 at 24 GB (40 GB compressible sysbench, as in round 4)
#   B  repeat of MyRocks lz4, 16 GB block cache (round 4's best)
#   C  repeats of round 5 at 48 GB of tables (all five modes)
#   D  real data: JOB (IMDb 2013, 113 queries) with RAM reduced to ~8 GB by reserving 23 GB as huge pages, so the
#      ~8 GB data set does not fit uncompressed but does compressed (lz4 ~1.7x, zstd ~2.7x)
#   then the canary is restored (datadir, swap, sysctls, agent, huge pages 0, MyRocks package removed)
# ZFS is not repeated: it needs the second SSD reformatted again, and it lost by 35-70%.
# usage: round6.sh [first part A|B|C|D]   (log: /root/bench/zstudy-sbz/round6.log)
set -u
Z=/root/bench/bm2/zstudy6.sh
O=/root/bench/zstudy-sbz
log() { echo "$(date -u +%FT%T) $*" | tee -a $O/round6.log; }
die() { log "ROUND6FAILED: $*"; exit 1; }
q() { mysql -N -e "$1"; }
SBZ="LUA_PATH='/root/bench/sysbench-z/?.lua;;' sysbench /root/bench/sysbench-z/oltp_read_write.lua --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest --tables=16 --table-size=11000000 --data-random-pct=10"
start_with() {
  printf '[mysqld]\nmax_connections = 300\ninnodb_buffer_pool_size = 16G\ninnodb_redo_log_capacity = 8G\ninnodb_io_capacity = 2000\ninnodb_io_capacity_max = 4000\n%b' "${1:-}" > /etc/mysql/mysql.conf.d/zzz-zstudy.cnf
  systemctl start mysql || die "mysqld start"
}
P=${1:-A}
until grep -q -E "ROUND5DONE|ROUND5FAILED: [^r]" $O/round5.log 2>/dev/null; do sleep 120; done
grep -q ROUND5DONE $O/round5.log || die "round 5 failed: fix it first (round5.log)"
log "round 6 from part $P"
DEBIAN_FRONTEND=noninteractive apt-get install -yq bc >/dev/null 2>&1

if [ "$P" = A ] || [ "$P" = B ]; then
  export WL=sbz SBZ_ROWS=11000000 SBZ_WARM=900
  $Z setup || die "sbz setup"
fi
if [ "$P" = A ]; then
  for lvl in 1 6; do
    systemctl stop mysql; rsync -aS --delete /data/zstudy_sbz_base/ /var/lib/mysql/ || die "restore"
    start_with "innodb_compression_level = $lvl\n"
    log "converting to KEY_BLOCK_SIZE=8 at zlib level $lvl"
    for t in $(seq 1 16); do echo "ALTER TABLE sbtest.sbtest$t ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8;"; done \
      | xargs -P 4 -d '\n' -I{} mysql -e "{}" > $O/convert-kbs8-l$lvl.log 2>&1 || die "convert level $lvl"
    q "SET GLOBAL innodb_fast_shutdown = 0"; systemctl stop mysql
    rm -rf /data/zbase_kbs8_l$lvl; rsync -aS /var/lib/mysql/ /data/zbase_kbs8_l$lvl/ || die "copy"
    log "base kbs8 level $lvl: $(du -sh /data/zbase_kbs8_l$lvl | cut -f1)"
  done
  ZCNF_EXTRA="innodb_compression_level = 1" ZBASE=/data/zbase_kbs8_l1 $Z run inno-kbs8:24:L1 inno-kbs8:26:L1
  ZBASE=/data/zbase_kbs8_l6 $Z run inno-kbs8:24:r2
  rm -rf /data/zbase_kbs8_l1 /data/zbase_kbs8_l6
fi
if [ "$P" = A ] || [ "$P" = B ]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -yq percona-server-rocksdb >/dev/null 2>&1 || die "install rocksdb"
  cf="compression=kLZ4Compression;bottommost_compression=kLZ4Compression"
  systemctl stop mysql; rsync -aS --delete /data/zstudy_sbz_base/ /var/lib/mysql/ || die "restore for rocks"
  start_with "plugin-load-add = ha_rocksdb.so\nrocksdb_block_cache_size = 8G\nrocksdb_default_cf_options = $cf\n"
  q "DROP DATABASE sbtest; CREATE DATABASE sbtest"
  log "MyRocks lz4: loading"
  ( eval "$SBZ --mysql-storage-engine=rocksdb --threads=16 prepare" ) > $O/rocks-load-r6.log 2>&1 || die "rocks load"
  q "SET GLOBAL rocksdb_force_flush_memtable_now = 1"; q "SET GLOBAL rocksdb_compact_cf = 'default'"
  systemctl stop mysql; rm -rf /data/zbase_rocks_lz4; rsync -aS /var/lib/mysql/ /data/zbase_rocks_lz4/ || die "copy rocks"
  ROCKS_CF="$cf" ZBASE=/data/zbase_rocks_lz4 $Z run rocks-lz4:16:r2
  rm -rf /data/zbase_rocks_lz4
  systemctl stop mysql; DEBIAN_FRONTEND=noninteractive apt-get remove -yq percona-server-rocksdb >/dev/null 2>&1
  rm -f /etc/mysql/mysql.conf.d/zzz-zstudy.cnf
fi
if [ "$P" \< D ]; then
  export WL=sbz SBZ_ROWS=15000000 SBZ_WARM=1200 ZSWF_GB=80
  $Z setup || die "setup 15M"
  $Z run disk:26:r15m-r2 disk2:53:r15m-r2
  ZRAM_GB=128 $Z run zram:53:r15m-r2
  ZRAM_GB=32 $Z run zramdisk:53:r15m-r2
  ZSWAP_POOL_PCT=32 $Z run zswap-lz4:53:r15m-r2
  rm -rf /data/zstudy_sbz_base
fi
# D: JOB, real data, RAM reduced with reserved huge pages
export WL=job ZSWF_GB=64
unset SBZ_ROWS SBZ_WARM
$Z setup || die "JOB setup"
systemctl stop mysql; sync; echo 3 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory
sysctl -q vm.nr_hugepages=11776                     # 23 GB reserved: ~8 GB left for the OS, mysqld and swap caches
log "huge pages reserved: $(grep HugePages_Total /proc/meminfo | awk '{print $2}') x 2 MB; MemAvailable $(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo) GB"
$Z run disk:4:job
$Z run disk2:9:job
ZRAM_GB=24 $Z run zram:9:job zram-zstd:9:job
ZSWAP_POOL_PCT=15 $Z run zswap-lz4:9:job
sysctl -q vm.nr_hugepages=0
log "teardown"
$Z teardown
rm -rf /data/zstudy_job_base /data/zstudy_sbz_base
log "ROUND6DONE"
