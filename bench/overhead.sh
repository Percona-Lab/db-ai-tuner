#!/bin/bash
# Harness overhead experiment: same configuration (agent baseline), HammerDB TPROC-C, 3 conditions interleaved x 3 repeats:
#   none  - performance_schema OFF, tuning-agent stopped   (no instrumentation at all)
#   ps    - performance_schema ON (as the tuner requires), agent stopped
#   agent - performance_schema ON + tuning-agent daemon sampling every 10 s (how every tuning run is measured)
# usage: overhead.sh <warehouses> <virtual users>   results: /root/bench/overhead-<wh>.csv
set -x
WH=$1; VU=$2; OUT=/root/bench/overhead-$WH.csv; BASE=/root/base-$WH
hdb() { cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp "$@"; }
cpu_ticks() { [ -n "$1" ] && [ -r /proc/$1/stat ] && awk '{print $14+$15}' /proc/$1/stat || echo 0; }
if [ ! -d $BASE ]; then
  systemctl start tuning-agent; tuning-agent baseline --apply || exit 1   # "baseline parameters"
  mysql -e "DROP DATABASE IF EXISTS tpcc"
  HDB_WH=$WH HDB_BUILD_VU=12 hdb ./hammerdbcli auto /root/bench/hdb_build200.tcl > /root/bench/build-$WH.log 2>&1
  [ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = "$WH" ] || { echo "OVERHEADFAILED build"; exit 1; }
  systemctl stop mysql; rsync -a /var/lib/mysql/ $BASE/; systemctl start mysql
fi
[ -f $OUT ] || echo "workload,rep,cond,nopm,tpm,mysqld_cpu_s,agent_cpu_s,started" > $OUT
for rep in 1 2 3; do
  for cond in none ps agent; do
    grep -q "^$WH,$rep,$cond," $OUT && continue      # resumable
    systemctl stop tuning-agent mysql
    if [ $cond = none ]; then printf '[mysqld]\nperformance_schema = OFF\n' > /etc/mysql/mysql.conf.d/zzz-overhead.cnf; else rm -f /etc/mysql/mysql.conf.d/zzz-overhead.cnf; fi
    rsync -a --delete $BASE/ /var/lib/mysql/; sync; echo 3 > /proc/sys/vm/drop_caches
    systemctl start mysql; for i in $(seq 1 60); do mysqladmin ping >/dev/null 2>&1 && break; sleep 2; done
    [ $cond = agent ] && systemctl start tuning-agent
    MP=$(pidof mysqld); AP=$(systemctl show -p MainPID --value tuning-agent); m0=$(cpu_ticks $MP); a0=$(cpu_ticks $AP); t0=$(date +%s)
    HDB_VU=$VU HDB_RAMPUP=5 HDB_DURATION=30 hdb ./hammerdbcli auto /root/bench/hdb_run.tcl > /root/bench/overhead-$WH-$rep-$cond.log 2>&1
    m1=$(cpu_ticks $MP); a1=$(cpu_ticks $AP); HZ=$(getconf CLK_TCK)
    nopm=$(grep -o "achieved [0-9]* NOPM" /root/bench/overhead-$WH-$rep-$cond.log | grep -o "[0-9]*"); tpm=$(grep -o "from [0-9]* MySQL TPM" /root/bench/overhead-$WH-$rep-$cond.log | grep -o "[0-9]*")
    echo "$WH,$rep,$cond,$nopm,$tpm,$(( (m1-m0)/HZ )),$(( (a1-a0)/HZ )),$t0" >> $OUT
  done
done
rm -f /etc/mysql/mysql.conf.d/zzz-overhead.cnf
echo OVERHEADDONE-$WH
