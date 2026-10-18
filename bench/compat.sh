#!/bin/bash
# compat.sh <percona|mysql|mariadb> <series e.g. 8.0|8.4|9.7|10.11|11.4|11.8|12.3|13.0>
# Replace the server on this host with the given flavor/version and run a short tuning session on sysbench 1 GB.
F=$1; V=$2; LOG=/root/bench/compat-$F-$V.log
exec > $LOG 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
systemctl stop tuning-agent
for s in mysql mysqld mariadb; do systemctl stop $s 2>/dev/null; done
apt-get purge -yq 'percona-server-*' 'mysql-server*' 'mysql-community-*' 'mysql-client*' 'mariadb-*' 'libmariadb3' 'galera-*' 2>/dev/null
apt-get purge -yq mysql-common mariadb-common 2>/dev/null
update-alternatives --remove-all my.cnf 2>/dev/null || true
apt-get autoremove -yq
rm -f /usr/local/bin/mysql /usr/local/bin/mysqladmin
export DEBIAN_FRONTEND=noninteractive
# never stop at a dpkg config-file prompt (e.g. an AppArmor profile left by the previous flavour)
echo 'Dpkg::Options { "--force-confnew"; "--force-confdef"; }' > /etc/apt/apt.conf.d/90compat-noninteractive
rm -f /etc/apparmor.d/usr.sbin.mysqld /etc/apparmor.d/usr.sbin.mariadbd
rm -rf /var/lib/mysql /var/lib/mysql_base /etc/mysql /var/log/mysql /etc/apt/sources.list.d/mysql*.list /etc/apt/sources.list.d/mariadb*.list
for f in /etc/apt/sources.list.d/percona-*.list; do [ -e "$f" ] && percona-release disable all >/dev/null 2>&1; break; done
case $F in
  percona)
    case $V in 8.0) R=ps-80;; 8.4) R=ps-84-lts;; 9.7) R=ps-97-lts;; esac
    # older percona-release builds do not know the newest repositories: update the tool first
    curl -fsSLO https://repo.percona.com/apt/percona-release_latest.generic_all.deb && apt-get install -yq ./percona-release_latest.generic_all.deb
    percona-release setup -y $R
    ls /etc/apt/sources.list.d/ | grep -q "${R}" || percona-release enable-only $R release
    ls /etc/apt/sources.list.d/ | grep -q "${R}" || { echo "COMPAT FAIL: percona repository $R not enabled"; exit 1; }
    apt-get update -q
    debconf-set-selections <<< "percona-server-server percona-server-server/root-pass password"
    debconf-set-selections <<< "percona-server-server percona-server-server/re-root-pass password"
    apt-get install -yq percona-server-server ;;
  mysql)
    case $V in 8.4) R=mysql-8.4-lts;; 9.7) R=mysql-9.7-lts;; esac
    curl -fsSL https://repo.mysql.com/RPM-GPG-KEY-mysql-2025 | gpg --dearmor -o /usr/share/keyrings/mysql.gpg --yes
    echo "deb [signed-by=/usr/share/keyrings/mysql.gpg] http://repo.mysql.com/apt/ubuntu noble $R" > /etc/apt/sources.list.d/mysql.list
    apt-get update -q
    debconf-set-selections <<< "mysql-community-server mysql-community-server/root-pass password"
    debconf-set-selections <<< "mysql-community-server mysql-community-server/re-root-pass password"
    apt-get install -yq mysql-server ;;
  mariadb)
    curl -fsSL https://r.mariadb.com/downloads/mariadb_repo_setup | bash -s -- --mariadb-server-version=$V --skip-maxscale --skip-tools
    apt-get update -q
    apt-get install -yq mariadb-server ;;
esac
systemctl start mysql 2>/dev/null || systemctl start mariadb
command -v mysql >/dev/null || ln -sf "$(command -v mariadb)" /usr/local/bin/mysql
command -v mysqladmin >/dev/null || ln -sf "$(command -v mariadb-admin)" /usr/local/bin/mysqladmin
mysql -e "SELECT VERSION(), @@version_comment" || { echo "COMPAT FAIL: cannot connect as root"; exit 1; }
# the installed server must be the requested one (a broken repo silently falls back to the distribution package)
GOT=$(mysql -N -e "SELECT CONCAT(VERSION(), ' ', @@version_comment)")
case "$F" in percona) echo "$GOT" | grep -q "^$V\..*Percona" ;; mysql) echo "$GOT" | grep -q "^$V\..*MySQL Community" ;; mariadb) echo "$GOT" | grep -q "^$V\..*MariaDB" ;; esac \
  || { echo "COMPAT FAIL: wanted $F $V, got: $GOT"; exit 1; }
apt-get install -yq sysbench >/dev/null || { echo "COMPAT FAIL: sysbench could not be installed next to $F $V"; exit 1; }
mysql -e "CREATE DATABASE IF NOT EXISTS sbtest; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON sbtest.* TO sbtest@localhost;"
# agent: forget settings that belonged to the previous server
/opt/tuning-agent/venv/bin/python - <<'PY'
import json; p="/var/lib/tuning-agent/state.json"; d=json.load(open(p))
for k in ("session","measurement","compiled_defaults"): d[k]=None
d["managed"]={}; d["pending_changes"]=[]
json.dump(d, open(p,"w"), indent=1)
PY
sed -i '/^cnf_path\|^service_name/d' /etc/tuning-agent/agent.ini
SOCK=$(mysql -N -e "SELECT @@socket"); sed -i "s|^mysql_socket.*|mysql_socket = $SOCK|" /etc/tuning-agent/agent.ini
systemctl start tuning-agent
tuning-agent facts
tuning-agent baseline --apply
if ! mysqladmin ping >/dev/null 2>&1; then
  echo "COMPAT FAIL: mysqld is down after the baseline configuration ($F $V)"
  cat /etc/mysql/*/zz-tuning-agent.cnf /etc/mysql/conf.d/zz-tuning-agent.cnf 2>/dev/null
  tail -40 /var/log/mysql/error.log 2>/dev/null; journalctl -u mysql -u mariadb -n 30 --no-pager
  exit 1
fi
sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-socket=$SOCK --mysql-db=sbtest --tables=4 --table-size=1000000 --threads=2 prepare >/dev/null
sed -i "s|--mysql-socket=[^ ]*|--mysql-socket=$SOCK|" /root/bench/sbcompat.ini
NAME="compat: $(mysql -N -e 'SELECT CONCAT(@@version_comment, " ", VERSION())' | cut -c1-80)"
/root/bench/session.sh /root/bench/sbcompat.ini "$NAME" tps 4
grep -c Traceback $LOG
echo COMPATDONE
