#!/bin/bash
# tuner-db3: compatibility matrix rerun (MariaDB 11.8/12.3/13.0 + Percona Server 8.4 reference)
set -x
for fv in mariadb:11.8 mariadb:12.3 mariadb:13.0 percona:8.4; do /root/bench/compat.sh ${fv%%:*} ${fv##*:}; done
echo ALLCOMPATDONE > /root/bench/compat-all.done
