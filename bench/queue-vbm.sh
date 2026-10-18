#!/bin/bash
# tuner-vbm: HammerDB 200W then 2000W, free-range tuning (knobs, OS, filesystems, allocators), 40 runs each, data on the 2nd NVMe
. /root/bench/queue-common.sh
set -x
build() {  # $1 = warehouses; data on the root disk, the session moves it to the experiment device
  tuning-agent rollback --reset || exit 1
  tuning-agent baseline --apply || exit 1
  mysql -e "DROP DATABASE IF EXISTS tpcc"; rm -rf /var/lib/mysql_base
  (cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_WH=$1 HDB_BUILD_VU=12 ./hammerdbcli auto /root/bench/hdb_build200.tcl) > /root/bench/build$1.log 2>&1
  [ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = "$1" ] || { echo "QUEUEFAILED: build $1"; exit 1; }
  mysql -e "SELECT ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables WHERE table_schema='tpcc'"
}
build 200
SCOPE=knobs,os,filesystem PRE_APPLY="os:datadir_fs=ext4" STUDY="HammerDB 200W hardware scaling" \
  run_session /root/bench/hammerdb200vbm.ini "HammerDB 200W on bare metal E-2286G (free range)" tps 40
build 2000
SCOPE=knobs,os,filesystem PRE_APPLY="os:datadir_fs=ext4" STUDY="HammerDB 2000W on bare metal" \
  run_session /root/bench/hammerdb2000vbm.ini "HammerDB 2000W on bare metal E-2286G (free range)" tps 40
echo QUEUEDONE
