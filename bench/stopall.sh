#!/bin/bash
# stop queues, sessions, benchmarks on this host (run as a file so patterns never match the caller's command line)
for p in $(ps -eo pid,cmd | awk '/queue-compat\.sh|queue-db[0-9]*\.sh|compat\.sh|session\.sh|tuning-agent autotune|tuning-agent measure|sysbench |go-tpc |hammerdbcli/ && !/awk/ {print $1}'); do
  [ "$p" != "$$" ] && kill $p 2>/dev/null
done
sleep 2; ps -eo pid,etime,cmd | grep -E "queue|compat|session\.sh|autotune|sysbench|go-tpc|hammerdb" | grep -v grep | grep -v stopall
echo stopped
