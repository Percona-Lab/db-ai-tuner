#!/bin/bash
# tuner-big: HammerDB 200W from tuner-bigcli, data on /dev/sdc, free-range tuning (knobs, OS, filesystems, allocators)
. /root/bench/queue-common.sh
set -x
tuning-agent baseline --apply
mysql -e "DROP DATABASE IF EXISTS tpcc"
ssh -i /root/.ssh/bench_key -o BatchMode=yes root@50.116.46.101 "cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_HOST=173.230.136.182 HDB_WH=200 HDB_BUILD_VU=16 ./hammerdbcli auto /root/bench/hdb_build200.tcl" > /root/bench/build200.log 2>&1
grep -q "HDB BUILD COMPLETE" /root/bench/build200.log || { echo "QUEUEFAILED: build"; exit 1; }
[ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = 200 ] || { echo "QUEUEFAILED: warehouse count"; exit 1; }
mysql -e "SELECT ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables WHERE table_schema='tpcc'"
SCOPE=knobs,os,filesystem PRE_APPLY="os:datadir_fs=ext4" run_session /root/bench/hammerdb200big.ini "HammerDB 200W on 16 CPUs (free range: knobs, OS, filesystems, allocators)" tps 40
echo QUEUEDONE
