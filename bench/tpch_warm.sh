#!/bin/bash
# Warm the buffer pool: scan every TPC-H table's clustered index once.
for t in lineitem orders partsupp part customer supplier nation region; do
  mysql -N -e "SELECT COUNT(*) FROM tpch.$t FORCE INDEX (PRIMARY)" >/dev/null
done
