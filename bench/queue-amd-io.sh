#!/bin/bash
# tuner-vbm-amd (EPYC 4245P 6c/12t, 32 GB, NVMe): I/O-bound HammerDB 1000W, all-warehouse access, 128 VU co-located, binlog off.
# 1) full tuning session  2) redo-log deep dive on the best configuration  3) buffer pool push with a 64 GB swap file
. /root/bench/queue-common.sh
set -x
update_agent
P=/root/bench/hammerdb1000amd.ini
cat > $P <<'INI'
[bench]
name = HammerDB TPROC-C 1000W all-warehouse, 128 VU co-located, 5 min ramp-up + 10 min, binlog off (Vultr bare metal EPYC 4245P)
reset = /root/bench/reset_datadir.sh
run = cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient TMP=/tmp HDB_VU=128 HDB_ALLWH=true HDB_RAMPUP=5 HDB_DURATION={duration_min} ./hammerdbcli auto /root/bench/hdb_run.tcl
warmup = 305
duration = 600
timeout = 2400
goal_metric = nopm

[metrics]
nopm = achieved (\d+) NOPM
tpm = from (\d+) MySQL TPM

[requires]
max_connections = 300
INI
tuning-agent session stop >/dev/null 2>&1 || true
tuning-agent rollback --reset || { echo "QUEUEFAILED: rollback"; exit 1; }
# old 200W pristine copy aside (kept), fresh 1000W data set
systemctl stop mysql; [ -d /var/lib/mysql_base ] && [ "$(ls -A /var/lib/mysql_base)" ] && mv /var/lib/mysql_base /root/old200w_base; mkdir -p /var/lib/mysql_base
systemctl start mysql || { echo "QUEUEFAILED: mysql start"; exit 1; }
mysql -e "DROP DATABASE IF EXISTS tpcc; SET GLOBAL max_connections = 300"
(cd /opt/HammerDB-6.0 && LD_LIBRARY_PATH=/opt/mysqlclient HDB_WH=1000 HDB_BUILD_VU=12 ./hammerdbcli auto /root/bench/hdb_build200.tcl) > /root/bench/build1000.log 2>&1
[ "$(mysql -N -e 'SELECT COUNT(*) FROM tpcc.warehouse')" = 1000 ] || { echo "QUEUEFAILED: build 1000"; exit 1; }
mysql -e "SELECT ROUND(SUM(data_length+index_length)/1073741824,1) gb FROM information_schema.tables WHERE table_schema='tpcc'"
cat > /root/bench/amd-post.sh <<'POST'
#!/bin/bash
# runs inside the session after autotune, on its best production-safe configuration
P=$1; set -x
add() { tuning-agent experiment add "$@" || true; }
# 2) redo log deep dive: each series keeps its best value only if it beats noise, the next one builds on it
add --sweep innodb_log_write_ahead_size=512,4096,16384 --reason "redo write padding (write-ahead size vs NVMe 4K pages)"
add --sweep innodb_log_buffer_size=16M,256M --reason "log buffer size at 128 VU"
add --sweep innodb_redo_log_capacity=16G,32G --reason "redo capacity: checkpoint pressure on an I/O-bound set"
add --try innodb_log_writer_threads=ON --reason "dedicated log writer threads at high concurrency"
add --sweep innodb_log_wait_for_flush_spin_hwm=0,4000 --reason "spin vs sleep while waiting for redo flush"
add --sweep innodb_log_spin_cpu_abs_lwm=0,200 --reason "redo spin CPU low watermark"
add --sweep innodb_log_spin_cpu_pct_hwm=10,100 --reason "redo spin CPU high watermark"
add --try innodb_log_checksums=OFF --reason "redo block checksums CPU"
add --try innodb_use_fdatasync=OFF --reason "fsync vs fdatasync for redo/data"
tuning-agent experiment run --bench $P || exit 1
# 3) buffer pool push with a large swap file: step the pool up past RAM while it still pays off
tuning-agent apply --set os:swapfile=64G --reason "large swap for the buffer pool push" || exit 1
cur=$(( $(mysql -N -e "SELECT @@innodb_buffer_pool_size") / 1073741824 ))
vals=$(python3 -c "c=$cur; print(','.join(f'{x}G' for x in sorted(set(list(range(c+2, 31, 2)) + list(range(28, 37)))) if x > c))")
add --sweep innodb_buffer_pool_size=$vals --force --reason "buffer pool push from ${cur}G with 64 GB swap: how far does a larger cache still win?"
tuning-agent experiment run --bench $P || exit 1
POST
chmod +x /root/bench/amd-post.sh
POST=/root/bench/amd-post.sh SCOPE=knobs,os STUDY="EPYC 4245P: HammerDB 1000W I/O-bound, 128 VU" \
  run_session $P "HammerDB 1000W I/O-bound, 128 VU, binlog off (EPYC 4245P): tuning, redo deep dive, buffer pool push" tps 24
echo QUEUEDONE
