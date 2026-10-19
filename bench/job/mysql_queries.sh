#!/bin/bash
# Make the MySQL copy of the 113 JOB queries: mysql_queries.sh [src dir] [dst dir]
# The original PostgreSQL text runs unchanged on MySQL 8.4 except for one alias: "AS character" (10b, 10c) is a
# reserved word in MySQL and gets backquoted. Everything else (implicit comma joins, LIKE, IN, BETWEEN, MIN) is
# identical. Case-sensitive LIKE as in PostgreSQL comes from the database collation (utf8mb4_0900_bin, see schema.sql).
SRC=${1:-/root/job/jobq}; DST=${2:-/root/job/queries}
mkdir -p $DST
for f in $SRC/[0-9]*.sql; do sed -e 's/ AS character,/ AS `character`,/' $f > $DST/$(basename $f); done
ls $DST/*.sql | wc -l
