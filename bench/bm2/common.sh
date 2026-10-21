# shared by the bm2 queues
. /root/bench/queue-common.sh
SB="--mysql-user=sbtest --mysql-password=sbtest --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-db=sbtest"
prepare() {  # prepare <tables> <rows>
  [ "$(mysql -N -e "select count(*) from information_schema.tables where table_schema='sbtest'")" -ge "$1" ] || \
    step sysbench oltp_read_write $SB --tables=$1 --table-size=$2 --threads=16 prepare
  mysql -N -e "select concat(round(sum(data_length+index_length)/1073741824,1),' GB') from information_schema.tables where table_schema='sbtest'"
}
wait_go() {  # sessions start only after the deploy canary passed on the current build
  echo "waiting for /root/bench/GO"; until [ -f /root/bench/GO ]; do sleep 60; done; echo GO
}
