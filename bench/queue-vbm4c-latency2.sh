#!/bin/bash
# tuner-vbm4c, second session after the 200W (CPU-bound) one: HammerDB 1000W all-warehouse (I/O-bound on 32 GB),
# same tail-latency goal and protocol. Pristine copy on the second SSD (sda, blank before), live data on the system disk.
. /root/bench/queue-common.sh
set -x
while ! grep -qE "QUEUEDONE|QUEUEFAILED" /root/bench/queue-latency.log; do sleep 120; done
grep -q QUEUEDONE /root/bench/queue-latency.log || { echo "QUEUEFAILED: the 200W session did not finish"; exit 1; }
[ -n "$(lsblk -no FSTYPE /dev/sda)" ] || mkfs.ext4 -F -q /dev/sda
mkdir -p /data && (mountpoint -q /data || mount /dev/sda /data) || { echo "QUEUEFAILED: mount /data"; exit 1; }
grep -q "^/dev/sda /data" /etc/fstab || echo "/dev/sda /data ext4 defaults 0 2" >> /etc/fstab
systemctl stop mysql
[ -d /var/lib/mysql_base ] && [ ! -L /var/lib/mysql_base ] && mv /var/lib/mysql_base /data/old200w_base
mkdir -p /data/mysql_base && ln -sfn /data/mysql_base /var/lib/mysql_base
systemctl start mysql || { echo "QUEUEFAILED: mysql start"; exit 1; }
mysql -e "DROP DATABASE IF EXISTS tpcc"
(cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_WH=1000 HDB_BUILD_VU=8 ./hammerdbcli auto /root/bench/hdb_build200.tcl) > /root/bench/build1000.log 2>&1
[ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = 1000 ] || { echo "QUEUEFAILED: build 1000"; exit 1; }
mysql -e "SELECT ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables WHERE table_schema='tpcc'"
P=/root/bench/hammerdb1000lat.ini
sed -e 's/200W, 16 VU co-located/1000W all-warehouse, 16 VU co-located/' -e 's/HDB_VU=16 /HDB_VU=16 HDB_ALLWH=true /' \
    -e 's/^timeout = .*/timeout = 3600/' /root/bench/hammerdb200lat.ini > $P
grep -q "HDB_ALLWH=true" $P || { echo "QUEUEFAILED: profile"; exit 1; }
SCOPE=knobs,os STUDY="E3-1270: HammerDB 1000W I/O-bound tail latency" \
  run_session $P "HammerDB 1000W all-warehouse tail latency (NEW_ORDER P99), 16 VU, binlog off (E3-1270)" latency 30
echo QUEUEDONE
