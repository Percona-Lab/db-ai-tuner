#!/bin/bash
# db_warm.sh <database>: warm the buffer pool by scanning every table's clustered index once (read-only analytics cells
# restart mysqld for configuration changes; the warm-up makes each run start from the same cache state)
DB=$1
for t in $(mysql -N -e "SELECT table_name FROM information_schema.tables WHERE table_schema='$DB' AND table_type='BASE TABLE'"); do
  mysql -N -e "SELECT COUNT(*) FROM \`$DB\`.\`$t\` FORCE INDEX (PRIMARY)" >/dev/null 2>&1 || mysql -N -e "SELECT COUNT(*) FROM \`$DB\`.\`$t\`" >/dev/null
done
