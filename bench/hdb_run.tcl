proc e {n {d ""}} { if {[info exists ::env($n)] && $::env($n) ne ""} {return $::env($n)}; return $d }
dbset db mysql
dbset bm TPC-C
diset connection mysql_host 127.0.0.1
diset connection mysql_port 3306
diset connection mysql_socket /var/run/mysqld/mysqld.sock
diset tpcc mysql_user tpcc
diset tpcc mysql_pass tpcc
diset tpcc mysql_dbase tpcc
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_driver timed
diset tpcc mysql_rampup [e HDB_RAMPUP 2]
diset tpcc mysql_duration [e HDB_DURATION 5]
diset tpcc mysql_allwarehouse false
diset tpcc mysql_timeprofile true
diset tpcc mysql_keyandthink false
diset tpcc mysql_total_iterations 10000000000
loadscript
vuset logtotemp 0
vuset unique 1
vuset showoutput 0
vuset vu [e HDB_VU 10]
vucreate
set out [vurun]
vudestroy
puts "HDB RUN COMPLETE $out"
exit
