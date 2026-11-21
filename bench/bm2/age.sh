#!/bin/bash
# age.sh <w...>: run each BenchBase workload 10 min (32 terminals) on its small data set, then measure pages again
O=/root/survey
for w in "$@"; do
  C=$O/bb-$w.xml; A=$O/bb-$w-age.xml
  sed -e "s#<terminals>[^<]*#<terminals>32#" -e "s#<time>[^<]*#<time>600#" -e "s#<warmup>[^<]*#<warmup>0#" -e "s#<rate>[^<]*#<rate>unlimited#" $C > $A
  echo "$(date -u +%T) age $w" >> $O/log
  (cd /opt/benchbase && java -Xmx6g -jar benchbase.jar -b $w -c $A --execute=true -d $O/age-$w) > $O/age-$w.log 2>&1
  for i in $(seq 1 120); do [ "$(mysql -N -e "show global status like 'Innodb_buffer_pool_pages_dirty'" | cut -f2)" -lt 50 ] && break; sleep 5; done
  python3 /root/pagecomp.py $w-aged /var/lib/mysql/bb_$w "" | tee -a $O/results.jsonl
  grep -h "Rate limited\|requests/sec" $O/age-$w.log | tail -1 >> $O/log
done
echo "$(date -u +%T) AGEDONE" >> $O/log
