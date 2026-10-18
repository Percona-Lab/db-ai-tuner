#!/bin/bash
# stop only queue drivers; match on the process's own command prefix so the caller's command line never matches
ps -eo pid=,args= | while read -r pid args; do
  case "$args" in "/bin/bash /root/bench/queue-"*) kill "$pid" 2>/dev/null;; esac
done
echo queues-stopped
