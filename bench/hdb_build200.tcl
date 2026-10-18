proc e {n {d ""}} { if {[info exists ::env($n)] && $::env($n) ne ""} {return $::env($n)}; return $d }
dbset db mysql
dbset bm TPC-C
diset connection mysql_host [e HDB_HOST 127.0.0.1]
diset connection mysql_port 3306
if {[e HDB_HOST] eq ""} { diset connection mysql_socket /var/run/mysqld/mysqld.sock } else { diset connection mysql_socket null }
diset tpcc mysql_user tpcc
diset tpcc mysql_pass tpcc
diset tpcc mysql_dbase tpcc
diset tpcc mysql_count_ware [e HDB_WH 200]
diset tpcc mysql_num_vu [e HDB_BUILD_VU 4]
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_partition false
buildschema
vudestroy
puts "HDB BUILD COMPLETE"
exit
