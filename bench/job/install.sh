#!/bin/bash
# Join Order Benchmark (Leis et al., VLDB 2015): fetch the IMDb data set (2013 snapshot, 21 tables, ~74M rows,
# 1.2 GB tgz / 3.7 GB CSV) and the 113 queries. Usage: install.sh [dir]   (default /root/job)
set -e
D=${1:-/root/job}; H=$(cd $(dirname $0) && pwd)
mkdir -p $D/dl $D/csv
cd $D
[ -s dl/imdb.tgz ] || curl -fsS -o dl/imdb.tgz https://event.cwi.nl/da/job/imdb.tgz  # also http://homepages.cwi.nl/~boncz/job/imdb.tgz
[ -s csv/title.csv ] || tar -xzf dl/imdb.tgz -C csv
# queries: original PostgreSQL text (plain SQL-92 joins, MIN() aggregates; they run unchanged on MySQL)
[ -d jobq ] || git clone -q https://github.com/gregrahn/join-order-benchmark.git jobq
git -C jobq checkout -q a396036
$H/mysql_queries.sh $D/jobq $D/queries
