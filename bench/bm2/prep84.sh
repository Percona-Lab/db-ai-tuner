#!/bin/bash
# Percona Server 8.4 LTS with debug symbols in place of whatever is installed; fresh data directory (benchmark data only)
# usage: prep84.sh [ps-84-lts|ps-97-lts]
REPO=${1:-ps-84-lts}
set -ex
export DEBIAN_FRONTEND=noninteractive
tuning-agent session stop >/dev/null 2>&1 || true
tuning-agent rollback --reset || true
systemctl stop mysql || true
apt-get purge -yq 'percona-server-*' 'percona-mysql-router*' >/dev/null || true
rm -rf /var/lib/mysql /var/lib/mysql_base
mkdir -p /var/lib/mysql_base && chown mysql:mysql /var/lib/mysql_base 2>/dev/null || true
percona-release enable-only $REPO release
apt-get update -q >/dev/null
debconf-set-selections <<< "percona-server-server percona-server-server/root-pass password"
debconf-set-selections <<< "percona-server-server percona-server-server/re-root-pass password"
if [ "$REPO" = ps-84-lts ]; then apt-get install -yq percona-server-server percona-server-dbg >/dev/null; else apt-get install -yq percona-server-server >/dev/null; fi
rm -f /etc/mysql/mysql.conf.d/lab.cnf   # server defaults (binary log on), as in the suite
systemctl restart mysql
mysql -e "CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON *.* TO sbtest@localhost; CREATE DATABASE IF NOT EXISTS sbtest"
mysql -N -e "SELECT @@version, @@log_bin"
systemctl restart tuning-agent
