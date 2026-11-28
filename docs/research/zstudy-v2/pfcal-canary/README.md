# Page-fault calibration, tuner-canary (2026-11-28)

Xeon E3-1270 v6 (2017), kernel 6.8.0-151, SATA SSD (Intel SSDSC2BB240). `bench/v2/pfcal.c`: 2 GB of real InnoDB pages
(sysbench-compressible pct 10 = z10, stock = z100), pushed out with MADV_PAGEOUT, touched in random order, one access
per page, vm.page-cluster=0. Means over rounds 2-3.

| Fault served from (z10) | mean | p50 | p99 |
|---|---|---|---|
| resident page, cache miss (no fault) | 0.1 us | 0.09 | 0.4 |
| first touch, zero-fill minor fault | 1.5 us | 1.4 | 2.7 |
| zram lz4 | 5.2 us | 4.4 | 44 |
| zswap lz4 (shrinker off) | 5.7 us | 5.5 | 7.3 |
| zram zstd | 11.0 us | 10.2 | 49 |
| zswap zstd | 11.4 us | 11.2 | 13.9 |
| SATA SSD 4K random read, idle, QD1 (fio) | 189 us | 161 | 367 |

Stock (incompressible) pages: within ~10% of the same. Disk swap could not be timed with pfcal on an idle box: the kernel
keeps disk-swapped pages in the swap cache while memory is plentiful (even after a second MADV_PAGEOUT), so faults were
minor faults (1.4 us); the disk part is measured with fio instead (disk swap-in ~= read latency + ~1.5 us).
Single-threaded: zswap is as fast as zram per fault, so v1's 20-25% zswap throughput gap is not swap-in cost; measure
swap-out/reclaim and 64-thread contention in v2.
