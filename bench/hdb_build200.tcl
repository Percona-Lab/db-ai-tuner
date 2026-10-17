dbset db mysql
dbset bm TPC-C
diset connection mysql_host 127.0.0.1
diset connection mysql_port 3306
diset connection mysql_socket /var/run/mysqld/mysqld.sock
diset tpcc mysql_user tpcc
diset tpcc mysql_pass tpcc
diset tpcc mysql_dbase tpcc
diset tpcc mysql_count_ware 200
diset tpcc mysql_num_vu 4
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_partition false
buildschema
vudestroy
puts "HDB BUILD COMPLETE"
exit
