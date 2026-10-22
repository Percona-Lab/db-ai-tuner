#!/bin/bash
# The swap study again with writes (oltp_read_write): dirty buffer pool pages and redo in the picture. Runs outside the tuner (an OOM kill is an expected outcome). Results: /root/bench/swapstudy-rw/.
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
SB_TEST=oltp_read_write OUT=/root/bench/swapstudy-rw bash /root/bench/bm2/swap-study.sh uniform:off uniform:on special:on || { echo "QUEUEFAILED: swap study"; exit 1; }
rm -f /var/lib/mysql_base.dataset   # the study used its own database
echo PIPEOK
