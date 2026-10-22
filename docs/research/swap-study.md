# Swap and buffer pool size: safety margin, cost and benefit (2026-10-21)

Question: hosts without swap turn memory pressure into an OOM kill. Does adding swap cost anything when it is not
needed, how close to RAM can the buffer pool go without swap, and can a buffer pool larger than RAM (with swap) beat the
best size without it?

Setup: Percona Server 8.4.11, plain sysbench `oltp_read_only`, 16 tables at about 3x RAM, 64 threads, 10 min warm-up
+ 5 min measured per point, `innodb_buffer_pool_size` stepped from 12 GB upward, swap off (none at all) and on (a swap
file of 1x RAM), `vm.swappiness = 60`. Two access distributions: `special` (sysbench's default hot set) and `uniform`
(every row equally likely: the working set is all the data). One run per point; repeat runs of the same configuration
on these hosts vary by about 1-2%. Script: `bench/bm2/swap-study.sh`; raw data: `~/swapstudy/` (not in the repo).

| Host | CPU / storage | RAM | Data |
|---|---|---|---|
| tuner-vbm-amd | AMD EPYC, NVMe | 30 GB (24.7 GB available: ~6.6 GB held by the kernel outside every named counter) | 84 GB |
| tuner-vbm-sata | Intel, SATA SSD | 31 GB (31.2 GB available) | 86 GB |

## Results

Throughput (tps) and p95 latency (ms); "vs best off" compares with the best size that ran without swap.

**NVMe host (AMD)**

| BP GB | no swap, special | swap, special | vs best off | no swap, uniform | swap, uniform | vs best off | swapped GB |
|---|---|---|---|---|---|---|---|
| 12 | 21,823 (4.0) | 21,914 (4.0) | -2.0% | 12,756 (6.1) | 12,797 (6.1) | -9.4% | 0 |
| 16 | 22,120 (4.0) | 22,411 (4.0) | +0.2% | 13,414 (5.9) | 13,407 (5.9) | -5.1% | 0 |
| 20 | **22,370** (4.0) | 22,947 (3.9) | +2.6% | **14,133** (5.7) | 14,131 (5.7) | 0.0% | 0 |
| 24 | OOM at start | 21,074 (8.9) | -5.8% | OOM at start | 10,895 (9.7) | -22.9% | 2.5 |
| 26 | OOM at start | 19,981 (8.7) | -10.7% | OOM at start | 7,872 (14.0) | -44.3% | 4.9 |
| 30 | | 19,660 (8.9) | -12.1% | | 7,020 (16.7) | -50.3% | 10.1 |
| 36 | | 20,594 (8.7) | -7.9% | | 5,248 (24.8) | -62.9% | 17.7 |

**SATA host (Intel)**

| BP GB | no swap, special | swap, special | vs best off | no swap, uniform | swap, uniform | vs best off | swapped GB |
|---|---|---|---|---|---|---|---|
| 12 | 7,888 (16.4) | 7,905 (16.4) | -6.7% | 1,804 (42.6) | 1,789 (44.2) | -17.9% | 0 |
| 20 | 8,276 (17.3) | 8,282 (17.3) | -2.3% | 1,987 (40.4) | 1,991 (40.4) | -8.7% | 0 |
| 24 | 8,386 (17.3) | 8,427 (17.3) | -0.6% | 2,094 (39.7) | 2,078 (40.4) | -4.7% | 0 |
| 26 | **8,475** (17.0) | 8,444 (17.0) | -0.4% | **2,180** (37.6) | 2,150 (38.9) | -1.4% | 0 |
| 28 | OOM at start | 8,492 (16.7) | +0.2% | OOM at start | 2,245 (36.9) | **+3.0%** | 0.7 |
| 30 | OOM at start | 8,178 (17.6) | -3.5% | | 2,106 (41.9) | -3.4% | 2.8 |
| 32 | | 7,405 (21.1) | -12.6% | | 1,714 (55.8) | -21.4% | 5.2 |
| 36 | | 7,525 (19.7) | -11.2% | | 1,485 (64.5) | -31.9% | 10.3 |

## Findings

1. **Unused swap costs nothing.** At every size that fits in memory, swap on and off are within -1.4% to +2.6%: the
   run-to-run noise. Adding swap is free protection until it is used.
2. **Without swap, the failure is at startup, not under load.** InnoDB 8.4 zero-fills the whole buffer pool when it
   starts (the kernel log shows `ib_buf_create` in the OOM report), so a pool that does not fit is OOM-killed within
   seconds of starting - every time, on both hosts. mysqld needs about 1.06 x the pool + ~0.5 GB resident
   (24 GB -> 25.4 GB). The largest pool that started: 26 GB of 31 GB RAM (SATA, 84% of RAM; 93% resident) and 20 GB of
   30 GB (AMD, where 6.6 GB of RAM is not available: 24 GB needed 25.4 GB of 24.7 GB available).
3. **Swap rarely beats the best size without it.** One case: SATA, uniform access, 28 GB with swap: +3.0% over the best
   no-swap 26 GB, with 0.65 GB of cold non-buffer-pool memory swapped out and no swap-in traffic. The special hot set
   gained +0.2% there (noise). As soon as pages come back from swap (from 30 GB on SATA, 24 GB on AMD) throughput falls
   and p95 latency doubles.
4. **With swap, over-sizing degrades instead of crashing - by how much depends on the access pattern and the swap
   device.** A hot set on NVMe swap: -6% to -16% even with 17.7 GB in swap (the kernel keeps the hot pages resident; swap
   acts like a slow second cache). Uniform access: -23% at 2.5 GB swapped, -63% at 17.7 GB on NVMe, -32% on SATA. The
   alternative without swap at those sizes is no database.
5. **The margin to aim for:** keep mysqld's resident memory below what the host actually has available, not below RAM.
   On AMD, 6.6 GB of RAM was invisible to a RAM-based budget (kernel allocations no counter names; not page cache, slab
   or the ZFS cache).

## What changed in the tuner

* Swap as protection (`best-practices-swap`, 1x RAM, at most 64 GiB) stays as it is: finding 1 confirms it costs
  nothing unused, finding 4 that it turns a crash into a measurable slowdown.
* The memory budget's OS reserve is now the larger of 10% of RAM and what the host measurably uses outside mysqld (RAM
  minus available minus mysqld's resident memory), so a buffer pool the host cannot hold is not proposed as safe
  (finding 5).
* Growing the buffer pool into swap (`bp-overcommit`) remains a low-confidence experiment kept only if the goal
  improves and pages are not swapped back in: finding 3 shows the possible gain is small and the downside steep.
* A buffer pool that fails at startup is an apply failure (rolled back, recorded), never a measured result; with swap
  present, the swap-in rate and the p95 latency are the evidence that a size is too large.
