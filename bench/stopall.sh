#!/bin/bash
# stop queues, sessions and benchmarks (prefix match on each process's own command line)
ps -eo pid=,args= | while read -r pid args; do
  case "$args" in
    "/bin/bash /root/bench/"*|"/opt/tuning-agent/venv/bin/python3 /usr/local/bin/tuning-agent autotune"*|"/opt/tuning-agent/venv/bin/python3 /usr/local/bin/tuning-agent measure"*|sysbench\ *|/opt/go-tpc/go-tpc\ *|./hammerdbcli\ *) kill "$pid" 2>/dev/null;;
  esac
done
echo stopped
