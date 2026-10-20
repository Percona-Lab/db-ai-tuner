#!/bin/bash
# Insert Benchmark (Mark Callaghan, github.com/mdcallag/mytools bench/ibench) for the AI DB Tuner.
# Installs the client (iibench.py at a pinned commit + python3-mysqldb) and the benchmark user on a tuner host.
# Usage (on the host): /root/bench/ibench/install.sh
set -e
D=/root/bench/ibench
COMMIT=${IBENCH_COMMIT:-e1ba3d9edff258d867ef6430353cb4dc60e5eca4}   # mytools main, 2026-06-17
mkdir -p $D
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-mysqldb sysstat >/dev/null
if [ ! -s $D/iibench.py ]; then
  curl -fsSL "https://raw.githubusercontent.com/mdcallag/mytools/$COMMIT/bench/ibench/iibench.py" -o $D/iibench.py.new
  mv $D/iibench.py.new $D/iibench.py
fi
python3 -c "import MySQLdb" || { echo "python3-mysqldb missing"; exit 1; }
mysql -e "CREATE DATABASE IF NOT EXISTS ib;
          CREATE USER IF NOT EXISTS 'ib'@'localhost' IDENTIFIED BY 'ib';
          GRANT ALL ON ib.* TO 'ib'@'localhost';
          GRANT PROCESS ON *.* TO 'ib'@'localhost'"
echo "ibench installed in $D (iibench.py from mytools $COMMIT)"
