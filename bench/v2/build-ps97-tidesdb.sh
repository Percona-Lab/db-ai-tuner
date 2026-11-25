#!/bin/bash
# Build Percona Server 9.7 from source with the TidesDB storage engine (Evgeniy Patlan's MySQL 9.7 port,
# github.com/EvgeniyPatlan/tidesdb-mysql) compiled in next to InnoDB and MyRocks, release settings (the port's own
# scripts build Debug). Installs to /opt/ps97 and runs as a separate instance (port 3307, datadir /data/ps97/data),
# leaving the host's packaged MySQL alone. Then a smoke test on InnoDB, MyRocks and TidesDB.
# usage: build-ps97-tidesdb.sh [wait-for-file]   (log: /data/ps97/build.log)
set -euo pipefail
PS_TAG=${PS_TAG:-Percona-Server-9.7.2-3}
PORT_REF=${PORT_REF:-v0.5.0}
TIDESDB_TAG=${TIDESDB_TAG:-v10.0.1}
W=/data/ps97; SRC=$W/src; PREFIX=/opt/ps97
mkdir -p $W $SRC
exec > >(tee -a $W/build.log) 2>&1
log() { echo "$(date -u +%FT%T) $*"; }
if [ -n "${1:-}" ]; then until grep -q -E "ROUND7DONE|ROUND7FAILED" "$1" 2>/dev/null; do sleep 120; done; fi
log "build start: $PS_TAG + tidesdb-mysql $PORT_REF + TidesDB $TIDESDB_TAG"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -yq build-essential cmake ninja-build bison pkg-config git libssl-dev libncurses-dev libaio-dev \
  libudev-dev zlib1g-dev liblz4-dev libzstd-dev libsnappy-dev libgflags-dev libcurl4-openssl-dev libreadline-dev \
  libtirpc-dev rpcsvc-proto libnuma-dev libpam0g-dev libldap-dev libsasl2-dev libkrb5-dev libprotobuf-dev \
  protobuf-compiler libicu-dev libevent-dev patchelf >/dev/null
log "deps installed: $(cmake --version | head -1), $(gcc --version | head -1)"

cd $SRC
[ -d tidesdb-mysql ] || git clone -q https://github.com/EvgeniyPatlan/tidesdb-mysql.git
git -C tidesdb-mysql checkout -q $PORT_REF
[ -d tidesdb ] || git clone -q --depth=1 --branch $TIDESDB_TAG https://github.com/tidesdb/tidesdb.git
for p in tidesdb-mysql/docker/patches/tidesdb/*.patch; do
  git -C tidesdb apply --reverse --check "$p" 2>/dev/null && continue
  git -C tidesdb apply "$p" && log "patched TidesDB: $(basename $p)"
done
if [ ! -d percona-server ]; then
  git clone -q --depth=1 --branch $PS_TAG https://github.com/percona/percona-server.git
  git -C percona-server submodule update -q --init --recursive --depth=1
fi
log "sources ready: PS $(git -C percona-server describe --tags), port $(git -C tidesdb-mysql rev-parse --short HEAD)"

# TidesDB library: static, PIC, release
cmake -S tidesdb -B tidesdb/build-rel -G Ninja -DBUILD_SHARED_LIBS=OFF -DTIDESDB_BUILD_TESTS=OFF \
  -DTIDESDB_WITH_MIMALLOC=OFF -DTIDESDB_WITH_TCMALLOC=OFF -DTIDESDB_WITH_JEMALLOC=OFF \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_INSTALL_PREFIX=$SRC/tidesdb-prefix >/dev/null
cmake --build tidesdb/build-rel -j"$(nproc)" >/dev/null && cmake --install tidesdb/build-rel >/dev/null
log "TidesDB library: $(find $SRC/tidesdb-prefix -name libtidesdb.a)"

# plugin source into the Percona Server tree
mkdir -p percona-server/storage/tidesdb
cp tidesdb-mysql/plugin/*.cc tidesdb-mysql/plugin/*.h tidesdb-mysql/plugin/CMakeLists.txt percona-server/storage/tidesdb/

# Percona Server: release build with InnoDB, MyRocks and TidesDB
export TIDESDB_ROOT=$SRC/tidesdb-prefix
cmake -S percona-server -B percona-server/build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX=$PREFIX \
  -DCMAKE_PREFIX_PATH=$SRC/tidesdb-prefix -DWITH_ROCKSDB=ON -DWITH_UNIT_TESTS=OFF -DWITH_NDB=OFF -DWITH_NDBCLUSTER=OFF \
  -DWITH_AUTHENTICATION_LDAP=OFF -DWITH_AUTHENTICATION_KERBEROS=OFF -DWITH_AUTHENTICATION_FIDO=OFF \
  -DWITH_SSL=system -DWITH_ZSTD=system -DWITH_LZ4=system -DPLUGIN_TIDESDB=DYNAMIC 2>&1 | tail -5
log "configured; building (1-3 h)"
cmake --build percona-server/build -j"$(nproc)" 2>&1 | tail -20
cmake --install percona-server/build >/dev/null
log "installed: $($PREFIX/bin/mysqld --version); plugins: $(ls $PREFIX/lib/plugin | grep -E 'tidesdb|rocksdb' | tr '\n' ' ')"

# separate instance: port 3307, its own datadir
id mysql >/dev/null 2>&1 || useradd -r -s /bin/false mysql
mkdir -p $W/data $W/run && chown -R mysql:mysql $W/data $W/run
cat > $W/my.cnf <<EOF
[mysqld]
basedir = $PREFIX
datadir = $W/data
port = 3307
socket = $W/run/mysqld.sock
pid-file = $W/run/mysqld.pid
log-error = $W/mysqld.err
user = mysql
disable_log_bin
plugin-load-add = ha_rocksdb.so
plugin-load-add = ha_tidesdb.so
innodb_buffer_pool_size = 4G
max_connections = 300
[client]
socket = $W/run/mysqld.sock
EOF
[ -d $W/data/mysql ] || $PREFIX/bin/mysqld --defaults-file=$W/my.cnf --initialize-insecure
$PREFIX/bin/mysqld --defaults-file=$W/my.cnf --daemonize
for i in $(seq 1 60); do $PREFIX/bin/mysqladmin --defaults-file=$W/my.cnf -uroot ping >/dev/null 2>&1 && break; sleep 2; done
M="$PREFIX/bin/mysql --defaults-file=$W/my.cnf -uroot"
log "engines: $($M -N -e "SELECT GROUP_CONCAT(CONCAT(engine,'=',support)) FROM information_schema.engines WHERE engine IN ('InnoDB','ROCKSDB','TIDESDB')")"
$M -e "SHOW GLOBAL VARIABLES LIKE 'tidesdb%'" > $W/tidesdb-variables.txt
$M -e "CREATE USER IF NOT EXISTS sbtest@'%' IDENTIFIED BY 'sbtest'; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON *.* TO sbtest@'%'; GRANT ALL ON *.* TO sbtest@localhost"

# smoke test: patched sysbench (compressible data), 4 x 200k rows, 16 threads, 60 s per engine
for e in innodb rocksdb tidesdb; do
  $M -e "DROP DATABASE IF EXISTS sbtest; CREATE DATABASE sbtest"
  SB="sysbench /root/bench/sysbench-z/oltp_read_write.lua --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=$W/run/mysqld.sock --mysql-db=sbtest --tables=4 --table-size=200000 --data-random-pct=10 --mysql-storage-engine=$e"
  LUA_PATH='/root/bench/sysbench-z/?.lua;;' $SB --threads=1 prepare > $W/smoke-$e-prepare.log 2>&1 || { log "SMOKE $e: prepare FAILED (smoke-$e-prepare.log)"; continue; }
  LUA_PATH='/root/bench/sysbench-z/?.lua;;' $SB --threads=16 --time=60 --mysql-ignore-errors=all run > $W/smoke-$e-run.log 2>&1 || true
  log "SMOKE $e: $(grep -E 'transactions:|ignored errors:' $W/smoke-$e-run.log | tr -s ' ' | tr '\n' ' ')"
done
$PREFIX/bin/mysqladmin --defaults-file=$W/my.cnf -uroot shutdown
log "BUILDDONE"
