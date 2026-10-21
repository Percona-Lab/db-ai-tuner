#!/bin/bash
# EPYC 4245P: ground-truth response curves (buffer pool: uniform / hot set; concurrency limit and thread pool under
# contention), then the agent on the contention workload - how close does it land to the measured optimum?
. /root/bench/bm2/common.sh
set -x
D=/root/bench/bm2
rm -f /root/bench/GO   # set only after the deploy canary passes on the current build
bash $D/prep84.sh || { echo "QUEUEFAILED: prep84"; exit 1; }
prepare 16 10000000
wait_go
export SCOPE=knobs,os CALIBRATE=3 BASELINE_ARGS="--reserve-mem 2G" STUDY="Bare metal: response curves (EPYC 4245P)"
cat > $D/post-bp.sh <<'P'
#!/bin/bash
# every point measured from the same parent (the baseline): a person's sweep runs all values
tuning-agent experiment add --sweep innodb_buffer_pool_size=2G,4G,8G,12G,16G,20G --reason "ground truth: buffer pool response curve" || exit 1
tuning-agent experiment run --bench $1 || exit 1
P
cat > $D/post-conc.sh <<'P'
#!/bin/bash
set -x
tuning-agent experiment add --sweep innodb_thread_concurrency=6,12,24,48,96 --reason "ground truth: concurrency limit curve" || exit 1
tuning-agent experiment run --bench $1 || exit 1
tuning-agent apply --set innodb_thread_concurrency=0 --reason "back to the parent" || exit 1
for n in 6 12 24 48; do
  tuning-agent experiment add --try thread_handling=pool-of-threads --try thread_pool_size=$n --reason "ground truth: thread pool size $n" || exit 1
  tuning-agent experiment run --bench $1 || exit 1
  tuning-agent apply --set thread_handling=one-thread-per-connection --reason "back to the parent" || exit 1
done
P
chmod +x $D/post-*.sh
POST=$D/post-bp.sh run_session $D/bp-uniform.ini "Ground truth: buffer pool curve, uniform 37 GB (EPYC)" tps 0
POST=$D/post-bp.sh run_session $D/bp-pareto.ini "Ground truth: buffer pool curve, hot set 37 GB (EPYC)" tps 0
POST=$D/post-conc.sh run_session $D/contention.ini "Ground truth: concurrency limit and thread pool, 256 threads (EPYC)" tps 0
run_session $D/contention.ini "Agent: contention workload, 256 threads (EPYC) - compare with the ground truth" tps 12
echo QUEUEDONE
