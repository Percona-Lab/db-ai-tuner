#!/bin/bash
# Restore the pristine data set: stop mysqld, rsync the base copy, start mysqld (cold).
set -e
systemctl stop mysql
rsync -a --delete /var/lib/mysql_base/ /var/lib/mysql/
sync; echo 3 > /proc/sys/vm/drop_caches
systemctl start mysql
for i in $(seq 1 60); do mysqladmin ping >/dev/null 2>&1 && exit 0; sleep 2; done
echo "mysqld did not come back"; exit 1
