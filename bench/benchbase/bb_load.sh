#!/bin/bash
# bb_load.sh <workload> [loader threads]: create the database and load the workload's data set with BenchBase
# (--create=true --load=true). Database name, scale factor and connection come from <workload>.xml.tpl.
W=$1; D=/root/bench/benchbase; T=$D/$W.xml.tpl
[ -f "$T" ] || { echo "no template $T"; exit 2; }
BENCH=$(sed -n 's/.*bb-bench: *\([a-z]*\).*/\1/p' "$T" | head -1)
DB=$(sed -n 's#.*jdbc:mysql://[^/]*/\([A-Za-z0-9_]*\)?.*#\1#p' "$T" | head -1)
# BenchBase connects as bb/bb over TCP to localhost (template URL); fresh hosts have no such user
mysql -e "CREATE USER IF NOT EXISTS bb@'localhost' IDENTIFIED BY 'bb'; GRANT ALL ON *.* TO bb@'localhost'" || exit 2
mysql -e "DROP DATABASE IF EXISTS $DB; CREATE DATABASE $DB"
mkdir -p $D/load; C=$D/load/$W.xml
sed -e "s/@WARMUP@/0/g" -e "s/@TIME@/10/g" -e "s/@TERMINALS@/1/g" "$T" > "$C"
[ -n "$2" ] && sed -i "s#<loaderThreads>[0-9]*</loaderThreads>#<loaderThreads>$2</loaderThreads>#" "$C"
cd /opt/benchbase || exit 2
t0=$(date +%s)
java -Xmx8g -jar benchbase.jar -b "$BENCH" -c "$C" --create=true --load=true --execute=false -d $D/load 2>&1 | grep -v "^\s*at " | tail -n 30
rc=${PIPESTATUS[0]}
mysql -e "ANALYZE TABLE $(mysql -N -e "SELECT GROUP_CONCAT(CONCAT('$DB.', table_name)) FROM information_schema.tables WHERE table_schema='$DB'")" >/dev/null
echo "LOADED $W db=$DB rc=$rc seconds=$(( $(date +%s) - t0 )) size_gb=$(mysql -N -e "SELECT ROUND(SUM(data_length+index_length)/1073741824,1) FROM information_schema.tables WHERE table_schema='$DB'")"
exit $rc
