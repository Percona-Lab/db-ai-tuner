#!/bin/bash
# tuner-big: HammerDB 2000W (~200 GB, I/O-bound on 32 GB RAM), 40 experiments x (5 + 20 min), knobs + OS (no filesystem
# experiments: the data disk also holds the pristine copy used for resets)
. /root/bench/queue-common.sh
set -x
sed -i '/^fs_device/d; /^allow_fs_experiments/d' /etc/tuning-agent/agent.ini; systemctl restart tuning-agent
systemctl stop mysql
mkdir -p /root/old200w && [ -d /var/lib/mysql_base ] && [ ! -L /var/lib/mysql_base ] && mv /var/lib/mysql_base /root/old200w/mysql_base
mountpoint -q /var/lib/mysql && umount /var/lib/mysql
mountpoint -q /data || { mkfs.ext4 -F -q -E lazy_itable_init=0,lazy_journal_init=0 /dev/sdc && mkdir -p /data && mount /dev/sdc /data; }
grep -q "^/dev/sdc /data" /etc/fstab || echo "/dev/sdc /data ext4 defaults 0 2" >> /etc/fstab
mkdir -p /data/mysql /data/mysql_base; chown mysql:mysql /data/mysql
rsync -a /var/lib/mysql/ /data/mysql/ && rm -rf /var/lib/mysql/* && mount --bind /data/mysql /var/lib/mysql
grep -q "^/data/mysql /var/lib/mysql" /etc/fstab || echo "/data/mysql /var/lib/mysql none bind 0 0" >> /etc/fstab
ln -sfn /data/mysql_base /var/lib/mysql_base
systemctl start mysql || { echo "QUEUEFAILED: mysql start on /data"; exit 1; }
tuning-agent baseline --apply || { echo "QUEUEFAILED: baseline"; exit 1; }
mysql -e "DROP DATABASE IF EXISTS tpcc"
ssh -i /root/.ssh/bench_key -o BatchMode=yes root@50.116.46.101 "mysql --get-server-public-key -h 173.230.136.182 -utpcc -ptpcc -e 'SELECT 1' >/dev/null 2>&1; cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_HOST=173.230.136.182 HDB_WH=2000 HDB_BUILD_VU=32 ./hammerdbcli auto /root/bench/hdb_build200.tcl" > /root/bench/build2000.log 2>&1
[ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = 2000 ] || { echo "QUEUEFAILED: build 2000"; exit 1; }
mysql -e "SELECT ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables WHERE table_schema='tpcc'"
SCOPE=knobs,os STUDY="HammerDB 2000W hardware scaling" \
  run_session /root/bench/hammerdb2000big.ini "HammerDB 2000W on 16 dedicated vCPU (extensive)" tps 40
echo QUEUEDONE
