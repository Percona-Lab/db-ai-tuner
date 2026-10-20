#!/bin/bash
# Insert Benchmark data load = Mark Callaghan's l.i0 + l.x steps, then the pristine snapshot for reset_datadir.sh.
#   l.i0: N clients, one table each (ib.pi1..piN), PK-order inserts of ROWS rows per table, 100 rows per commit,
#         no secondary indexes (iibench --setup --num_secondary_indexes=0)
#   l.x : each client inserts a few rows then creates the 3 secondary indexes on its table in one ALTER
#         (iibench --secondary_at_end --num_secondary_indexes=3), all tables in parallel
# then ANALYZE, clean shutdown (all dirty pages flushed; l.i0 leaves no purge work) and copy to /var/lib/mysql_base.
# Usage (on the host): [IBENCH_NOSNAP=1 for a smoke test] IBENCH_CLIENTS=4 IBENCH_ROWS=100000000 /root/bench/ibench/load.sh
set -x
N=${IBENCH_CLIENTS:-4}
ROWS=${IBENCH_ROWS:-100000000}     # per table
D_IB=/root/bench/ibench
SOCK=/var/run/mysqld/mysqld.sock
L=$D_IB/load; mkdir -p $L
[ -z "$(ls -A /var/lib/mysql_base/ 2>/dev/null)" ] || { echo "LOADFAILED: /var/lib/mysql_base is not empty - move it aside first"; exit 1; }
mysql -e "DROP DATABASE IF EXISTS ib; CREATE DATABASE ib" || { echo "LOADFAILED: create database"; exit 1; }
# faster index build only (the resulting indexes are the same sorted bulk build); restored by the restart below
mysql -e "SET GLOBAL innodb_ddl_buffer_size = 256*1024*1024"
common="--dbms=mysql --db_name=ib --secs_per_report=10 --db_host=localhost --db_user=ib --db_password=ib --db_socket=$SOCK
 --engine=innodb --engine_options= --unique_checks=1 --bulk_load=0 --data_length_min=10 --data_length_max=20
 --inserts_per_second=0 --query_threads=0 --dbopt=none"
t0=$(date +%s)
# l.i0
pids=()
for n in $(seq 1 $N); do
  python3 $D_IB/iibench.py $common --setup --num_secondary_indexes=0 --rows_per_commit=100 --max_rows=$ROWS \
    --table_name=pi$n --seed=$(( 1733776768 + n )) --my_id=$n > $L/l.i0.$n 2>&1 &
  pids+=($!)
done
for p in "${pids[@]}"; do wait $p || { echo "LOADFAILED: l.i0 client"; exit 1; }; done
t1=$(date +%s)
# l.x (Mark: np.sh with 100 rows in total, --secondary_at_end)
pids=()
for n in $(seq 1 $N); do
  python3 $D_IB/iibench.py $common --secondary_at_end --num_secondary_indexes=3 --rows_per_commit=$(( 100 / N )) --max_rows=$(( 100 / N )) \
    --table_name=pi$n --seed=$(( 1733776886 + n )) --my_id=$n > $L/l.x.$n 2>&1 &
  pids+=($!)
done
for p in "${pids[@]}"; do wait $p || { echo "LOADFAILED: l.x client"; exit 1; }; done
t2=$(date +%s)
for n in $(seq 1 $N); do mysql -e "ANALYZE TABLE ib.pi$n" >/dev/null; done
echo "l.i0 $((t1 - t0)) s ($(( N * ROWS / (t1 - t0 + 1) )) rows/s), l.x $((t2 - t1)) s"
mysql -e "SELECT table_name, table_rows, ROUND(data_length/1073741824,1) data_gb, ROUND(index_length/1073741824,1) index_gb
          FROM information_schema.tables WHERE table_schema='ib'"
for n in $(seq 1 $N); do mysql -N -e "SELECT 'pi$n', COUNT(*) FROM ib.pi$n" ; done
# pristine copy (the same steps session.sh uses), after a clean shutdown
[ -n "$IBENCH_NOSNAP" ] && { echo "LOADDONE (no snapshot)"; exit 0; }
mkdir -p /var/lib/mysql_base
systemctl stop mysql || { echo "LOADFAILED: stop"; exit 1; }
du -sh /var/lib/mysql /var/lib/mysql/ib
rsync -a /var/lib/mysql/ /var/lib/mysql_base/ || { echo "LOADFAILED: rsync"; exit 1; }
systemctl start mysql || { echo "LOADFAILED: start"; exit 1; }
echo "LOADDONE total $(( $(date +%s) - t0 )) s"
