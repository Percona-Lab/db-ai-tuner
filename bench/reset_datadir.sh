#!/bin/bash
# Restore the pristine data set: stop mysqld, rsync the base copy, start mysqld (cold).
set -e
# the data set is replaced anyway: skip the shutdown flush of dirty pages (minutes on a large buffer pool)
mysql -e "SET GLOBAL innodb_fast_shutdown = 2" 2>/dev/null || true
systemctl stop mysql
rsync -a --delete /var/lib/mysql_base/ /var/lib/mysql/
sync; echo 3 > /proc/sys/vm/drop_caches
systemctl start mysql
for i in $(seq 1 60); do mysqladmin ping >/dev/null 2>&1 && exit 0; sleep 2; done
echo "mysqld did not come back"; exit 1
