#!/bin/bash
# bb_small.sh <benchbase workload>: load a small data set from BenchBase's MySQL sample config into database bb_<w>
w=$1; S=/opt/benchbase/config/mysql/sample_${w}_config.xml; [ -f $S ] || { echo "no sample config for $w"; exit 2; }
declare -A SF=([tpcc]=20 [ycsb]=1000 [twitter]=100 [wikipedia]=10 [epinions]=10 [seats]=5 [auctionmark]=5 [tatp]=10 [smallbank]=10 [voter]=10 [resourcestresser]=10 [sibench]=10 [hyadapt]=10 [otmetrics]=10 [tpch]=1)
db=bb_$w; mysql -e "DROP DATABASE IF EXISTS $db; CREATE DATABASE $db"
C=/root/survey/bb-$w.xml
sed -e "s#jdbc:mysql://[^/]*/[A-Za-z0-9_]*#jdbc:mysql://localhost:3306/$db#" -e "s#<username>[^<]*#<username>bench#" -e "s#<password>[^<]*#<password>bench#" \
    -e "s#<scalefactor>[^<]*#<scalefactor>${SF[$w]:-1}#" -e "s#<loaderThreads>[^<]*#<loaderThreads>8#" $S > $C
grep -q loaderThreads $C || sed -i "s#</scalefactor>#</scalefactor><loaderThreads>8</loaderThreads>#" $C
cd /opt/benchbase && java -Xmx6g -jar benchbase.jar -b $w -c $C --create=true --load=true --execute=false 2>&1 | grep -v "^\s*at " | tail -5
echo "db=$db size_mb=$(mysql -N -e "SELECT ROUND(SUM(data_length+index_length)/1048576) FROM information_schema.tables WHERE table_schema='$db'")"
