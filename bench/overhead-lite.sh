#!/bin/bash
# after the main overhead experiment: full performance_schema + agent vs lite profile + agent, interleaved, 3 repeats each
while [ ! -f /root/bench/overhead.done ]; do sleep 120; done
/root/bench/overhead2.sh 2000 32 "agent lite" -lite > /root/bench/overhead-2000-lite.log 2>&1
mysql -e "DROP DATABASE IF EXISTS tpcc"
/root/bench/overhead2.sh 200 16 "agent lite" -lite > /root/bench/overhead-200-lite.log 2>&1
echo LITEDONE > /root/bench/overhead-lite.done
