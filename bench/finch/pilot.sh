#!/bin/bash
# Finch pilot (atdb3): 3 repeat runs of each profile, each from the pristine data set (reset_datadir.sh).
# Prerequisite: install.sh, finch setup.yaml loaded, and /var/lib/mysql_base snapshot (see docs/research/benchmark-finch.md).
# Run detached: (setsid nohup /root/bench/finch/pilot.sh > /root/bench/finch-pilot.log 2>&1 < /dev/null &)
W=${W:-60}; D=${D:-240}; N=${N:-3}
for p in ${PROFILES:-oltp churn}; do
  for i in $(seq 1 $N); do
    t0=$(date +%s); /root/bench/reset_datadir.sh || exit 1; echo "reset $(( $(date +%s) - t0 )) s"
    /root/bench/finch/run_finch.sh $p $W $D > /root/bench/finch-pilot-$p-$i.log 2>&1
    echo "$p run $i: $(grep '^FINCH' /root/bench/finch-pilot-$p-$i.log)"
  done
done
echo PILOTDONE
