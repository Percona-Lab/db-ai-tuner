#!/bin/bash
# Create and load the orders data set (1M customers, 100k products, 2M orders, 8 hot counters; ~0.6 GB, ~1 min).
H=$(cd $(dirname $0) && pwd)
cd $H/orders && time finch setup.yaml 2>&1 | grep -v "reconnect"
mysql -e "SELECT table_name, table_rows, ROUND((data_length+index_length)/1048576) mb FROM information_schema.tables WHERE table_schema='orders'"
