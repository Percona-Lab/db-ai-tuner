# zswap and zram: can a compressed swap tier let the buffer pool grow past RAM? (2026-11-21)

Published (public): https://github.com/Percona-Lab/zram-zswap-db-benchmark - that repo has the full write-up incl. round 3 and codec latency; this copy is the working notes.

Question (user, 2026-11-21): on an I/O-bound OLTP benchmark with the binary log off, can zswap or zram with lz4 give
better throughput by letting `innodb_buffer_pool_size` go beyond what is safe without them?

Host: tuner-canary (Vultr bare metal, Xeon E3-1270 v6, 8 threads, 31 GB RAM, 2 x 240 GB Intel SATA SSD: datadir on
sdb, the second drive `/data` on sda), Ubuntu 24.04, kernel 6.8.0-151, Percona Server 8.4.11. Script:
`bench/bm2/zstudy.sh` (+ `zstudy_point.py`); raw per-point results: `hammerdb-1000w-results.jsonl` here; full logs
archived in `~/zstudy-hammerdb-canary-2026-11-21.tgz` on the Claude Code VM (not in the repo).

AI-generated study: numbers are measured, but the write-up and conclusions were produced by an AI agent and
spot-checked only; they may contain mistakes.

## Round 1: HammerDB TPROC-C 1000 W (~100 GB)

Workload: HammerDB 6.0 TPROC-C, 1000 warehouses (84 GB of tables, ~93 GB on disk, ~3x RAM), all-warehouse mode,
64 virtual users co-located, binlog off, 5 min ramp-up + 15 min measured, pristine data restored before every point.
Server settings the same for every point (redo 8 GB, io_capacity 2000/4000, 8 buffer pool instances, O_DIRECT); only
the buffer pool size and the swap setup change.

Modes:

| Mode | Setup |
|---|---|
| disk | the host's own 8 GB swap file (datadir drive), zswap off, kernel defaults, swappiness 60 |
| disk2 | control for zswap: 64 GB swap file on the second SSD, zswap off, the compressed modes' sysctls |
| zswap | zswap (lz4, zsmalloc, max_pool_percent 50) in front of the same 64 GB swap file on the second SSD |
| zram | zram only (lz4, 32 GB device, priority 100), no disk swap, zswap off |

Compressed modes and disk2 use the Arch-wiki zram sysctls: `vm.page-cluster=0`, `vm.watermark_boost_factor=0`,
`vm.watermark_scale_factor=125`; swappiness 180 (zram) / 100 (zswap, disk2).

| Mode | BP GB | NOPM | vs disk 26 GB | NEW_ORDER p95 ms | CPU % per 1k NOPM | notes |
|---|---|---|---|---|---|---|
| disk | 20 | 15,420 | -42% | 262 | 1.23 | |
| disk | 24 | 24,863 | -7% | 183 | 1.31 | |
| disk | 26 | 26,695 / 26,428 | (base) | 160 / 165 | 1.30 | repeat within 1% |
| disk | 28 | 27,696 / 27,314 | +3% | 184 / 175 | 1.35 | 1.5 GB of mysqld swapped |
| disk2 | 32 | 29,149 | +10% | 155 | 1.61 | 8k swap-ins/s from the 2nd SSD |
| disk2 | 36 | 30,326 | +14% | 154 | 1.80 | |
| disk2 | 40 | 30,367 | +14% | 145 | 2.06 | 20k swap-ins/s |
| zswap | 28 | 27,298 | +2% | 156 | 1.82 | |
| zswap | 32 | 28,876 | +8% | 161 | 2.00 | |
| zswap | 36 | 30,828 | +16% | 144 | 2.01 | |
| zswap | 40 | **32,327 / 32,363** | **+21%** | 139 / 133 | 2.04 | repeat within 0.1% |
| zram | 28 | 27,977 | +5% | 158 | 1.33 | |
| zram | 32 | 29,391 / 28,927 | +10% | 155 / 152 | 1.94 | 240 MB/s in and out of zram |
| zram | 36, 40 | OOM-killed | | | | compressed pages no longer fit |

Findings:

1. **lz4 compresses HammerDB's InnoDB pages only 1.22-1.27x** (measured live in zram and zswap). zswap's pool filled
   at ~15 GB holding ~18.5 GB, the rest went to the swap file.
2. **Most of zswap's gain is the second SSD, not compression.** The same swap file without zswap (disk2) reaches +14%;
   zswap adds ~6% more (beyond the ~1% run-to-run noise measured by the repeats). In effect the buffer pool overflowed
   into a second drive that served 13-20k random reads/s at ~0.5 ms, while reads from the data drive fell.
3. **zram cannot grow far with data this incompressible**: +10% at 32 GB, OOM at 36 GB.
4. **The price is CPU**: ~2.0% CPU per 1k NOPM with swapping vs ~1.3% without (page faults, compression, reclaim).

Because the compression ratio, not the mechanism, limited this round, the next question was which benchmark has data
that compresses like real data might.

## Benchmark data compressibility survey

Method (`bench/bm2/survey.sh`, `bb_small.sh`, `age.sh`, `pagecomp.py`): on a short-lived Linode (g6-dedicated-8,
Percona Server 8.4.11), load a small data set of each benchmark (0.4-10 GB), wait until the buffer pool has no dirty
pages, then read every `.ibd` file in 4 KiB pieces - the unit the kernel compresses in zram/zswap - and compress each
piece on its own. All-zero pieces (unused extents) are left out: zram/zswap store them for free and a working buffer
pool holds real pages. A piece that does not shrink counts at 4096 bytes. Also: 16 KiB (InnoDB page) lz4, and
`mysqldump | lz4 -1` for comparison. "Aged" = after 10 minutes of the workload (32 terminals) on the loaded data.
Raw rows: `page-compressibility.jsonl`.

| Benchmark | Data MB | lz4 4 KiB | zstd-1 4 KiB | lz4 16 KiB | mysqldump + lz4 |
|---|---|---|---|---|---|
| BenchBase SmallBank | 2,344 | **3.49** (aged 3.30) | 6.32 (aged 5.71) | 3.77 | 4.00 |
| BenchBase AuctionMark | 1,658 | **2.79** (aged 2.92) | 4.34 (aged 4.50) | 2.88 | 2.19 |
| go-tpc TPC-H SF1 | 2,083 | 2.38 | 3.26 | 2.67 | 2.24 |
| BenchBase SEATS | 1,699 | 2.27 (aged 2.28) | 3.13 | 2.38 | 1.30 |
| BenchBase OT-Metrics | 9,726 | 2.02 | 3.15 | 2.12 | 2.98 |
| BenchBase Twitter | 433 | 1.96 (aged 1.96) | 2.40 | 2.01 | 1.21 |
| employees sample DB (datacharmer/test_db) | 144 | 1.76 | 2.41 | 1.78 | 2.36 |
| IMDb / Join Order Benchmark (real data) | 7,853 | 1.73 | 2.68 | 1.79 | 2.24 |
| BenchBase TATP | 772 | 1.59 | 2.35 | 1.62 | 1.58 |
| BenchBase TPC-C | 2,011 | 1.42 (aged 1.45) | 2.11 | 1.44 | 1.28 |
| BenchBase Epinions | 26 | 1.38 | 1.80 | 1.41 | 1.49 |
| go-tpc TPC-C | 1,749 | 1.36 | 1.84 | 1.38 | 1.25 |
| HammerDB TPROC-C | 1,708 | 1.33 | 1.72 | 1.35 | 1.24 |
| BenchBase Wikipedia | 2,327 | 1.22 | 1.45 | 1.25 | 1.52 |
| BenchBase YCSB | 1,190 | 1.14 | 1.36 | 1.17 | 1.09 |
| sysbench oltp (8 x 1M) | 1,819 | 1.14 | 2.12 | 1.16 | 1.06 |

What it shows:

* The small-scale estimate matches production scale: HammerDB 1.33x here vs 1.22-1.27x seen live in zram/zswap at
  1000 W.
* **mysqldump ratios do not predict page ratios** (OT-Metrics 2.98 vs 2.02; SEATS 1.30 vs 2.27): page layout, free
  space and record headers matter. Measure pages, not dumps.
* **Only SmallBank reaches 3x with lz4, and by an artifact**: account names are 64-character zero-padded numbers
  (`000...0001`); that table alone is 4.55x, while the tables its transactions update (checking, savings) are ~2x.
* **Even real data is ~1.7x with lz4** (IMDb, employees); zstd gives 2.4-2.7x on them.
* **zstd crosses 3x where lz4 does not**: AuctionMark 4.5x, SEATS 3.1x, OT-Metrics 3.2x, TPC-H 3.3x.
* Aging (10 minutes of workload) changed little: page splits and updates moved ratios by -0.2 to +0.13.

Choice for round 2: **BenchBase AuctionMark** (eBay-style auctions: users, items, bids, comments, purchases), the
most compressible OLTP workload without padding tricks (lz4 ~2.9x, zstd ~4.5x), tested with both lz4 and zstd for
zswap and zram.

## Round 2: BenchBase AuctionMark (~86 GB), lz4 and zstd - not I/O-bound enough to answer the question

Scale factor 140 (86 GB on disk; scale factor 300 was aborted at 130 GB, projected ~200 GB: AuctionMark's size is not
linear in the scale factor - plan ~0.6-0.7 GB per SF), 64 terminals co-located, 5 min warm-up + 15 min measured,
pristine data before each point. Raw rows: `auctionmark-sf140-results.jsonl`.

| Mode | BP GB | req/s | p95 ms | p99 ms | InnoDB reads/s | swap-ins/s |
|---|---|---|---|---|---|---|
| disk | 20 | 6,184 | 31.8 | 103 | 945 | 0 |
| disk | 24 | 7,357 | 28.3 | 60 | 683 | 0 |
| disk | 26 | 7,507 | 27.6 | 57 | 557 | 8 |
| disk | 28 | 6,503 | 34.8 | 79 | 698 | 3,963 (disk) |
| disk2 | 32 / 40 | 5,847 / 6,079 | 37 | 78 | ~1,000 | 5,400-6,000 (2nd SSD) |
| zswap lz4 | 32 / 40 / 48 | 7,509 / 7,513 / 7,472 | 27.4-27.7 | 56 | ~480 | ~3,500 (zswap) |
| zswap zstd | 32 / 40 / 48 / 56 | 7,487 / 7,502 / 7,503 / 7,468 | 27.5 | 56 | ~485 | ~3,500 (zswap) |
| zram lz4 | 32 / 40 / 48 | 7,544 / 7,534 / 7,553 | 27.2 | 55-57 | ~475 | ~3,500 (zram) |
| zram zstd | 32 / 40 / 48 / 56 | 7,506 / 7,491 / 7,536 / 7,530 | 27.4 | 56 | ~480 | ~3,500 (zram) |

Findings:

1. **This workload is CPU-bound with a hot set that fits in RAM**: at the best plain size (26 GB) the miss rate is
   0.16%, iowait 7.6%, CPU 88% busy (BenchBase's Java client shares the 8 threads). Every compressed setup lands at the
   same ~7,500 req/s as disk 26 GB, whatever the pool size (all within ~1%).
2. **The larger pools never filled with data.** A whole run reads ~1.4M pages (~23 GB); a 48 GB pool's extra memory is
   never-used pages. InnoDB zero-fills the pool at startup, the kernel swaps those zero pages out and zram/zswap store
   them almost for free (reported ratios 6-31x are zero pages, not AuctionMark data), and the swap-ins (~3,500/s) are
   InnoDB claiming a free page. Compression of real AuctionMark pages was never exercised.
3. **Compressed swap does make oversizing safe; disk swap does not**: at 28-40 GB, disk swap costs 13-22% (zero pages
   written to and read back from SSD), while zram/zswap cost nothing.

To test the question, the hot set has to exceed RAM but fit once compressed (see next steps).
