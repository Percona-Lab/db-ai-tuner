#!/bin/bash
# One-time setup of the bare-metal deploy canary host (tuner-canary): a small pristine data set for reset_datadir.sh.
# Earlier study data sets on /data stay where they are; /var/lib/mysql_base points at the canary set.
set -ex
mysql -e "CREATE DATABASE IF NOT EXISTS sbtest; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON sbtest.* TO sbtest@localhost"
mysql -e "DROP DATABASE sbtest; CREATE DATABASE sbtest"
sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest \
  --tables=4 --table-size=1000000 --threads=4 prepare
for db in $(mysql -N -e "select schema_name from information_schema.schemata where schema_name not in ('mysql','sys','information_schema','performance_schema','sbtest')"); do
  mysql -e "DROP DATABASE \`$db\`"   # other benchmarks' tables: the canary data set is sbtest only
done
mysql -e "SET GLOBAL innodb_fast_shutdown = 0"
systemctl stop mysql
mkdir -p /data/canary_base
rsync -a --delete /var/lib/mysql/ /data/canary_base/
ln -sfn /data/canary_base /var/lib/mysql_base
systemctl start mysql
du -sh /data/canary_base
echo SETUPDONE
