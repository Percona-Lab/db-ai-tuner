#!/bin/bash
# prep-sysbench.sh <tables> <rows>: (re)create sbtest
set -e
mysql -e "DROP DATABASE IF EXISTS sbtest; CREATE DATABASE sbtest; CREATE USER IF NOT EXISTS sbtest@'localhost' IDENTIFIED BY 'sbtest'; GRANT ALL ON sbtest.* TO sbtest@'localhost';"
sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest --tables=$1 --table-size=$2 --threads=2 prepare
mysql -e "SELECT ROUND(SUM(data_length+index_length)/1048576) mb FROM information_schema.tables WHERE table_schema='sbtest'"
