#!/bin/bash
# new bare-metal box: HammerDB 200W (co-located client, 16 VU), 5 min ramp-up + 10 min, 20 experiments, full scope
. /root/bench/queue-common.sh
set -x
tuning-agent baseline --apply || { echo "QUEUEFAILED: baseline"; exit 1; }
mysql -e "DROP DATABASE IF EXISTS tpcc"; rm -rf /var/lib/mysql_base
(cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_WH=200 HDB_BUILD_VU=$(nproc) ./hammerdbcli auto /root/bench/hdb_build200.tcl) > /root/bench/build200.log 2>&1
[ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = 200 ] || { echo "QUEUEFAILED: build"; exit 1; }
SCOPE=knobs,os,filesystem PRE_APPLY="os:datadir_fs=ext4" STUDY="HammerDB 200W hardware scaling" \
  run_session /root/bench/hammerdb200vbm.ini "HammerDB 200W on bare metal $(lscpu | sed -n 's/^Model name: *//p' | head -1 | sed 's/(R)//g; s/ CPU.*//; s/ Processor//' | cut -c1-30) (free range)" tps 20
echo QUEUEDONE
