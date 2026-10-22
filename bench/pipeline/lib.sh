#!/bin/bash
# Shared by the study pipeline items (bench/pipeline/items/*.sh, started by bench/pipeline/run.py). Every item runs on one
# bare-metal box, leaves the server as Percona Server 8.4 or as the variant it studies, and prints session ids to its log.
. /root/bench/queue-common.sh
PIPE=/root/bench/pipeline
SOCK=$(mysql -N -e "SELECT @@socket" 2>/dev/null || echo /var/run/mysqld/mysqld.sock)
SB() { echo "--mysql-user=sbtest --mysql-password=sbtest --mysql-socket=$(mysql -N -e 'SELECT @@socket') --mysql-db=sbtest"; }

ensure_ps84() {  # the standard server: Percona Server 8.4 with debug symbols (CPU / off-CPU profiles need them)
  mysql -N -e "SELECT CONCAT(@@version, ' ', @@version_comment)" 2>/dev/null | grep -q "^8\.4\..*Percona Server" && return
  # from another variant: the full replace (purges MySQL / MariaDB packages that would break a plain install), then symbols
  step bash /root/bench/pipeline/install_variant.sh percona 8.4
  DEBIAN_FRONTEND=noninteractive apt-get install -yq percona-server-dbg >/dev/null || echo "percona-server-dbg not installed (profiles less detailed)"
  rm -f /var/lib/mysql_base.dataset
}

dataset() {  # dataset <tables> <rows>: sysbench sbtest of this shape, and the pristine copy reset_datadir.sh restores
  local want="$(mysql -N -e 'SELECT CONCAT(@@version, " ", @@version_comment)') $1 x $2"
  if [ "$(cat /var/lib/mysql_base.dataset 2>/dev/null)" = "$want" ] && [ -d /var/lib/mysql_base/sbtest ]; then
    /root/bench/reset_datadir.sh && return
  fi
  tuning-agent session stop >/dev/null 2>&1 || true
  tuning-agent rollback --reset || true
  mysql -e "DROP DATABASE IF EXISTS sbtest; CREATE DATABASE sbtest; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON *.* TO sbtest@localhost"
  step sysbench oltp_read_write $(SB) --tables=$1 --table-size=$2 --threads=16 prepare
  mysql -N -e "SELECT CONCAT('dataset ', ROUND(SUM(data_length+index_length)/1073741824,1), ' GB') FROM information_schema.tables WHERE table_schema='sbtest'"
  systemctl stop mysql 2>/dev/null || systemctl stop mariadb
  rm -rf /var/lib/mysql_base && mkdir -p /var/lib/mysql_base && rsync -a /var/lib/mysql/ /var/lib/mysql_base/ || { echo "QUEUEFAILED: pristine copy"; exit 1; }
  echo "$want" > /var/lib/mysql_base.dataset
  systemctl start mysql 2>/dev/null || systemctl start mariadb
}

sb_profile() {  # sb_profile <file> <name> <test> <tables> <rows> <threads> <rand-type> <warmup s> <duration s> [extra sysbench args]
  local f=$1 name=$2 test=$3 t=$4 r=$5 th=$6 rnd=$7 wu=$8 du=$9; shift 9
  cat > $f <<INI
[bench]
name = $name
run = ulimit -n 1048576; sysbench $test --tables=$t --table-size=$r --threads=$th --rand-type=$rnd $(SB) $([ $th -ge 1024 ] && echo --thread-init-timeout=600) --time={total} --report-interval=10 --mysql-ignore-errors=all $* run
warmup = $wu
duration = $du
timeout = $(( (wu + du) * 3 + 600 ))
goal_metric = sysbench_tps
reset = /root/bench/reset_datadir.sh

[metrics]
sysbench_tps = transactions:\s+\d+\s+\(([\d.]+) per sec
sysbench_p95_ms = 95th percentile:\s+([\d.]+)

[requires]
max_connections = $(( th * 2 + 100 ))
max_prepared_stmt_count = 1048576
INI
}

cur() { mysql -N -e "SELECT @@GLOBAL.$1"; }
# items hand a post() function to session.sh (POST=post): a child process, so the functions it calls must be exported
try() {  # try <profile> <why> <var=value>...: a queued experiment (2 runs) against the current configuration, then back
  local p=$1 why=$2; shift 2
  declare -A base; local a=()
  for kv in "$@"; do base[${kv%%=*}]=$(cur ${kv%%=*} 2>/dev/null); a+=(--try "$kv"); done
  tuning-agent experiment add "${a[@]}" --repeats ${REPEATS:-2} --force --reason "pipeline $PIPE_ITEM: $why" || return 0
  tuning-agent experiment run --bench $p || true
  for v in "${!base[@]}"; do
    [ -n "${base[$v]}" ] && [ "$(cur $v 2>/dev/null)" != "${base[$v]}" ] && tuning-agent apply --set "$v=${base[$v]}" --reason "back to the starting value"
  done
  return 0
}

second_disk() {  # second_disk: mount the box's second (non-system) drive at /disk2; prints the device. Never touches the root disk.
  local root=$(lsblk -no PKNAME $(findmnt -no SOURCE /) | head -1)
  local d=$(lsblk -dno NAME -e7 | grep -v "^$root$" | grep -vE "^(zram|loop)" | head -1)
  [ -n "$d" ] || { echo "QUEUEFAILED: no second drive"; exit 1; }
  local cur=$(findmnt -no TARGET /dev/$d | head -1)
  if [ -n "$cur" ] && [ "$cur" != /disk2 ]; then umount /dev/$d || { echo "QUEUEFAILED: $d busy at $cur"; exit 1; }; fi
  if [ -z "$(lsblk -no FSTYPE /dev/$d | head -1)" ]; then
    lsblk -no NAME /dev/$d | tail -n +2 | grep -q . && { echo "QUEUEFAILED: $d has partitions"; exit 1; }
    mkfs.ext4 -q -F /dev/$d || { echo "QUEUEFAILED: mkfs $d"; exit 1; }
  fi
  mkdir -p /disk2 && (mountpoint -q /disk2 || mount -o noatime /dev/$d /disk2) || { echo "QUEUEFAILED: mount $d"; exit 1; }
  # Ubuntu's mysqld AppArmor profile only allows its own paths
  if [ -d /etc/apparmor.d/local ] && ! grep -qs "/disk2/" /etc/apparmor.d/local/usr.sbin.mysqld; then
    echo "  /disk2/ r, /disk2/** rwk," >> /etc/apparmor.d/local/usr.sbin.mysqld
    apparmor_parser -r /etc/apparmor.d/usr.sbin.mysqld 2>/dev/null || true
  fi
  echo $d
}

export -f try cur SB
