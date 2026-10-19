#!/bin/bash
# install.sh: BenchBase (cmu-db/benchbase) with the MySQL profile under /opt/benchbase, plus the bb-* helpers in /root/bench/benchbase.
# BenchBase main targets Java 23 (pom.xml java.version=23), so Ubuntu 24.04's openjdk-21 cannot build it: use openjdk-25 (apt).
# Usage: install.sh [git ref]   (default: main; the built commit is recorded in /opt/benchbase/COMMIT)
set -ex
REF=${1:-main}
export DEBIAN_FRONTEND=noninteractive
apt-get install -yq openjdk-25-jdk-headless git python3 >/dev/null
SRC=/opt/benchbase-src
[ -d $SRC/.git ] || git clone -q https://github.com/cmu-db/benchbase.git $SRC
cd $SRC && git checkout -q -- pom.xml && git fetch -q origin && git checkout -q "$REF" && { git pull -q || true; }
# JDK 25 emits new lint warnings and the pom compiles with -Werror: drop -Werror for this local build only
sed -i 's#<arg>-Werror</arg>##' pom.xml
./mvnw -q -B clean package -P mysql -DskipTests -Dfmt.skip=true   # fmt-maven-plugin (google-java-format 1.24) breaks on JDK 25: skip source formatting
[ -d /opt/benchbase ] && mv /opt/benchbase /opt/benchbase.old.$(date +%s)
mkdir -p /opt/benchbase && tar xzf target/benchbase-mysql.tgz -C /opt/benchbase --strip-components=1
git rev-parse HEAD > /opt/benchbase/COMMIT
# helpers next to the queue scripts
mkdir -p /root/bench/benchbase
cp "$(dirname "$(readlink -f "$0")")"/{bb_run.sh,bb_summary.py,bb_load.sh,*.xml.tpl} /root/bench/benchbase/ 2>/dev/null || true
java -version 2>&1 | head -1; ls /opt/benchbase
