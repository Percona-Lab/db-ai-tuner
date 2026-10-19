#!/bin/bash
# Load the JOB data set into MySQL database imdb: schema, LOAD DATA, the paper's foreign-key indexes, ANALYZE.
# Usage: load.sh [csv dir] [query dir]   (defaults /root/job/csv /root/job/jobq; run install.sh first)
# Optional: JOB_HISTOGRAMS=1 also builds histograms on the filtered columns (histograms.sql).
# Not binlogged (sql_log_bin=0) and with the redo log disabled during the load; ~25-35 min on 2 vCPU.
set -e
CSV=${1:-/root/job/csv}; Q=${2:-/root/job/jobq}; H=$(cd $(dirname $0) && pwd); DB=${JOB_DB:-imdb}
t0=$(date +%s)
LI=$(mysql -N -e "SELECT @@local_infile")
mysql -e "SET GLOBAL local_infile = 1"
mysql -e "ALTER INSTANCE DISABLE INNODB REDO_LOG"
trap 'mysql -e "ALTER INSTANCE ENABLE INNODB REDO_LOG; SET GLOBAL local_infile = '$LI'"' EXIT
mysql -e "SET sql_log_bin = 0; DROP DATABASE IF EXISTS $DB; CREATE DATABASE $DB CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
{ echo "SET sql_log_bin = 0;"; cat $H/schema.sql; } | mysql $DB
python3 -I $H/gen_load.py $H/schema.sql $CSV | while read -r stmt; do
  t=$(echo "$stmt" | sed 's/.*INTO TABLE \([a-z_]*\).*/\1/'); s=$(date +%s)
  { echo "SET sql_log_bin = 0; SET foreign_key_checks = 0; SET unique_checks = 0;"; printf '%s\n' "$stmt"; echo "SHOW COUNT(*) WARNINGS;"; } | \
    mysql -N --local-infile=1 $DB | tail -1 | \
    xargs printf "loaded %-16s warnings=%s" $t; echo " $(( $(date +%s) - s )) s"
done
echo "foreign-key indexes (fkindexes.sql from the JOB repository)"
{ echo "SET sql_log_bin = 0;"; cat $Q/fkindexes.sql; } | mysql $DB
mysql -N $DB -e "SET sql_log_bin = 0; ANALYZE TABLE $(mysql -N -e "SELECT GROUP_CONCAT(table_name) FROM information_schema.tables WHERE table_schema='$DB'")" | grep -v "status.*OK" || true
[ "${JOB_HISTOGRAMS:-0}" = 1 ] && { echo "SET sql_log_bin = 0;"; cat $H/histograms.sql; } | mysql $DB
mysql -e "SELECT table_name, table_rows, ROUND((data_length+index_length)/1048576) mb FROM information_schema.tables WHERE table_schema='$DB' ORDER BY 3 DESC"
echo "JOB load done in $(( $(date +%s) - t0 )) s"
