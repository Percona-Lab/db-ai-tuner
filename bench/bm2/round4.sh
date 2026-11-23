#!/bin/bash
# Round 4 of the zram/zswap study (2026-11-23): other ways to get compression into the cache, on the same 40 GB
# compressible sysbench data set (WL=sbz) and server. Runs after round 3's follow-ups (zstudy-sbz-run3). Phases:
#   1 InnoDB bases on /data: ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8 and 4 (zlib), page compression COMPRESSION='lz4'
#   2 InnoDB runs
#   3 ZFS: /data's contents moved to the system disk, the second SSD (sda) becomes a ZFS pool with lz4 and zstd
#     datasets (recordsize 16K), datadir bind-mounted from them; buffer pool + capped ARC (compressed ARC)
#   4 /data restored as before (ext4 on /dev/sda, same fstab line), contents moved back
#   5 MyRocks (percona-server-rocksdb): lz4 and zstd loads, block cache sizes; the OS page cache holds compressed SSTs
#   6 teardown: plugin package removed, study copies deleted, the canary's own datadir, swap and agent back
# usage: round4.sh [first phase]   (log: /root/bench/zstudy-sbz/round4.log)
set -u
export WL=sbz SBZ_ROWS=11000000
Z=/root/bench/bm2/zstudy4.sh
O=/root/bench/zstudy-sbz
KEEP=/var/lib/zkeep                       # /data's contents while sda is ZFS
SBZ="LUA_PATH='/root/bench/sysbench-z/?.lua;;' sysbench /root/bench/sysbench-z/oltp_read_write.lua --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest --tables=16 --table-size=$SBZ_ROWS --data-random-pct=10"
log() { echo "$(date -u +%FT%T) $*" | tee -a $O/round4.log; }
die() { log "ROUND4FAILED: $*"; exit 1; }
q() { mysql -N -e "$1"; }
start_with() {  # start_with <cnf extra lines>: a load-time server (16 GB pool) on the current datadir
  printf '[mysqld]\nmax_connections = 300\ninnodb_buffer_pool_size = 16G\ninnodb_redo_log_capacity = 8G\ninnodb_io_capacity = 2000\ninnodb_io_capacity_max = 4000\n%b' "${1:-}" > /etc/mysql/mysql.conf.d/zzz-zstudy.cnf
  systemctl start mysql || die "mysqld start"
}
snapshot_to() {  # slow shutdown, sparse copy of the datadir to $1
  q "SET GLOBAL innodb_fast_shutdown = 0"; systemctl stop mysql
  rm -rf "$1"; rsync -aS /var/lib/mysql/ "$1/" || die "copy to $1"
  log "base $1: $(du -sh --apparent-size "$1" | cut -f1) apparent, $(du -sh "$1" | cut -f1) on disk"
}
PH=${1:-1}
while systemctl is-active -q zstudy-sbz-run2 || systemctl is-active -q zstudy-sbz-run3; do sleep 60; done
log "round 4 from phase $PH"
# round 3's swap files are no longer needed
swapoff /data/zstudy-swap.img 2>/dev/null; rm -f /data/zstudy-swap.img /data/zstudy-zram-wb.img

if [ $PH -le 1 ]; then
  for v in kbs8 kbs4 pclz4; do
    systemctl stop mysql; rsync -aS --delete /data/zstudy_sbz_base/ /var/lib/mysql/ || die "restore for $v"
    start_with
    case $v in
      kbs8) ddl="ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8" ;;
      kbs4) ddl="ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=4" ;;
      pclz4) ddl="COMPRESSION='lz4'" ;;
    esac
    log "converting to $ddl"
    for t in $(seq 1 16); do
      if [ $v = pclz4 ]; then echo "ALTER TABLE sbtest.sbtest$t $ddl; OPTIMIZE TABLE sbtest.sbtest$t;"
      else echo "ALTER TABLE sbtest.sbtest$t $ddl;"; fi
    done | xargs -P 4 -d '\n' -I{} mysql -e "{}" > $O/convert-$v.log 2>&1 || die "convert $v (see convert-$v.log)"
    log "$v: $(q "select group_concat(distinct row_format), group_concat(distinct create_options) from information_schema.tables where table_schema='sbtest'")"
    snapshot_to /data/zbase_$v
  done
fi

if [ $PH -le 2 ]; then
  ZBASE=/data/zbase_kbs8 $Z run inno-kbs8:16 inno-kbs8:24 inno-kbs8:26
  ZBASE=/data/zbase_kbs4 $Z run inno-kbs4:16 inno-kbs4:24 inno-kbs4:26
  ZBASE=/data/zbase_pclz4 $Z run inno-pclz4:26
  rm -rf /data/zbase_kbs8 /data/zbase_kbs4 /data/zbase_pclz4
fi

if [ $PH -le 3 ]; then
  log "ZFS: moving /data to $KEEP, sda becomes a ZFS pool"
  systemctl stop mysql
  mkdir -p $KEEP && rsync -aS /data/ $KEEP/ || die "copy /data away"
  umount /data || die "umount /data"
  sed -i 's|^/dev/sda /data ext4 defaults 0 2|#zstudy# /dev/sda /data ext4 defaults 0 2|' /etc/fstab
  zpool create -f -o ashift=12 -O atime=off -O xattr=sa -O mountpoint=none zdata /dev/sda || die "zpool create"
  zfs create -o mountpoint=/zdata/lz4 -o recordsize=16k -o compression=lz4 -o logbias=latency -o primarycache=all zdata/lz4
  zfs create -o mountpoint=/zdata/zstd -o recordsize=16k -o compression=zstd -o logbias=latency -o primarycache=all zdata/zstd
  for c in lz4 zstd; do
    rsync -a $KEEP/zstudy_sbz_base/ /zdata/$c/ && chown mysql:mysql /zdata/$c && zfs snapshot zdata/$c@pristine || die "zfs load $c"
    log "zdata/$c: $(zfs get -H -o value used,compressratio,logicalused zdata/$c | tr '\n' ' ')"
  done
  rm -rf /var/lib/mysql/*            # the live copy on the system disk; datadir is bind-mounted from ZFS from here
  for c in lz4 zstd; do
    ZFS_DS=zdata/$c ZFS_ARC_GB=18 $Z run zfs-$c:8:arc18
    ZFS_DS=zdata/$c ZFS_ARC_GB=14 $Z run zfs-$c:12:arc14
    ZFS_DS=zdata/$c ZFS_ARC_GB=10 $Z run zfs-$c:16:arc10
  done
  ZFS_DS=zdata/lz4 ZFS_ARC_GB=4 $Z run zfs-lz4:24:arc4     # control: a normal-size pool, small ARC
fi

if [ $PH -le 4 ]; then
  log "restoring /data as before"
  systemctl stop mysql; mountpoint -q /var/lib/mysql && umount /var/lib/mysql
  zpool destroy zdata 2>/dev/null
  mkfs.ext4 -q -F /dev/sda || die "mkfs /data"
  sed -i 's|^#zstudy# /dev/sda /data ext4 defaults 0 2|/dev/sda /data ext4 defaults 0 2|' /etc/fstab
  grep -q '^/dev/sda /data ext4' /etc/fstab || die "fstab line"
  mount /data && rsync -aS $KEEP/ /data/ && rm -rf $KEEP || die "move /data back"
  echo $(( 0 )) > /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null
  log "/data restored: $(df -h /data | tail -1)"
fi

if [ $PH -le 5 ]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -yq percona-server-rocksdb >/dev/null 2>&1 || die "install percona-server-rocksdb"
  for c in lz4 zstd; do
    [ $c = lz4 ] && cf="compression=kLZ4Compression;bottommost_compression=kLZ4Compression" \
                 || cf="compression=kZSTD;bottommost_compression=kZSTD"
    systemctl stop mysql; rsync -aS --delete /data/zstudy_sbz_base/ /var/lib/mysql/ || die "restore for rocks $c"
    start_with "plugin-load-add = ha_rocksdb.so\nrocksdb_block_cache_size = 8G\nrocksdb_default_cf_options = $cf\n"
    q "DROP DATABASE sbtest; CREATE DATABASE sbtest"
    log "MyRocks $c: loading"
    ( eval "$SBZ --mysql-storage-engine=rocksdb --threads=16 prepare" ) > $O/rocks-load-$c.log 2>&1 || die "rocks load $c"
    q "SET GLOBAL rocksdb_force_flush_memtable_now = 1" ; q "SET GLOBAL rocksdb_compact_cf = 'default'"
    log "MyRocks $c: $(du -sh /var/lib/mysql/.rocksdb | cut -f1) on disk"
    systemctl stop mysql; rm -rf /data/zbase_rocks_$c; rsync -aS /var/lib/mysql/ /data/zbase_rocks_$c/ || die "copy rocks $c"
  done
  ROCKS_CF="compression=kLZ4Compression;bottommost_compression=kLZ4Compression" ZBASE=/data/zbase_rocks_lz4 $Z run rocks-lz4:8 rocks-lz4:16 rocks-lz4:24
  ROCKS_CF="compression=kZSTD;bottommost_compression=kZSTD" ZBASE=/data/zbase_rocks_zstd $Z run rocks-zstd:8 rocks-zstd:16 rocks-zstd:24
  rm -rf /data/zbase_rocks_lz4 /data/zbase_rocks_zstd
fi

if [ $PH -le 6 ]; then
  log "teardown"
  systemctl stop mysql
  DEBIAN_FRONTEND=noninteractive apt-get remove -yq percona-server-rocksdb >/dev/null 2>&1
  $Z teardown
  rm -rf /data/zstudy_sbz_base
  log "ROUND4DONE"
fi
