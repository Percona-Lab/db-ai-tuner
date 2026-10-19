#!/bin/bash
# Install Finch (github.com/square/finch) with the connection-churn patch, and the MySQL user it uses.
# Usage: install.sh   (Ubuntu, run as root on the database host; needs git and curl)
set -e
H=$(cd $(dirname $0) && pwd)
GO=1.24.9
[ -x /usr/local/go/bin/go ] || { curl -fsSL -o /tmp/go.tgz https://go.dev/dl/go$GO.linux-amd64.tar.gz && tar -C /usr/local -xzf /tmp/go.tgz; }
[ -d /root/finch-src ] || git clone -q https://github.com/square/finch.git /root/finch-src
git -C /root/finch-src checkout -q 3f4bb5c          # v1.0.0 + fixes, 2025-07-30
python3 -I $H/apply_reconnect_patch.py /root/finch-src   # FINCH_RECONNECT_EVERY=N (idempotent)
(cd /root/finch-src/bin/finch && PATH=$PATH:/usr/local/go/bin go build -o /usr/local/bin/finch .)
finch --version
mysql -e "CREATE USER IF NOT EXISTS finch@localhost IDENTIFIED BY 'finch'; CREATE USER IF NOT EXISTS finch@'127.0.0.1' IDENTIFIED BY 'finch';
  GRANT ALL ON orders.* TO finch@localhost; GRANT ALL ON orders.* TO finch@'127.0.0.1';"
