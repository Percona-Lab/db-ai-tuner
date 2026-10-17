#!/bin/bash
cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_VU=10 HDB_RAMPUP=2 HDB_DURATION=5 exec ./hammerdbcli auto /root/bench/hdb_run.tcl
