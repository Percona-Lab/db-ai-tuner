# MySQL / InnoDB Tuning Ideas: Candidate Experiments for the AI DB Tuner

*Research survey, 2026-10-18. Scope: MySQL 5.7 → 9.x, Percona Server 8.0/8.4/9.x, MariaDB 10.11 → 12.x on Linux.*

**What this is.** Ideas collected from the web so the tuner has a list of **hypotheses to test**. These are not established facts. Each idea records what the source claims, under which conditions, and the trigger that should make the auto-tuner try it. Many posts are old, tested one workload on one box, or contradict each other. Contradictions are noted where found.

**Sources covered (all opened and read; the URLs and dates are listed in [References](#references)):**
- **Mark Callaghan, Small Datum.** The 2023 InnoDB tuning series (configs a1-a22), `innodb_log_writer_threads`, the 8.4/9.x regression posts, mallocators, huge pages, `innodb_page_cleaners`, MariaDB `binlog_storage_engine`, and the performance-schema posts. His actual my.cnf files came from `github.com/mdcallag/mytools/bench/conf` (sparse clone). The main files were `arc/sep24/c8r32/my804/etc/my.cnf.cz11a_c8r32`, `arc/oct24/c8r32/my97/etc/my.cnf.cz13a_c8r32`, `arc/oct24/c8r32/my8406_rel_o2nofp/etc/my.cnf.cz12a_*`, `arc/aug23.c2.inno.tune/my.cnf.cy10a*_gcp_c2s30`, and the MariaDB 12.3 file `arc/oct24/c32r128/ma1203/etc/my.cnf.cz12c_c32r128`.
- **Percona blog, docs and forums.** Flushing, io_capacity, redo sizing, binlog, thread pool, doublewrite_pages, buffer-pool instances, Linux/OS settings, jemalloc, ext4/XFS, page size, P_S overhead, the 2026 ecosystem benchmark, the 9.7 PGO build, and the PS 8.4 tuning docs.
- **Oracle/MySQL.** lefred's post on the 8.4 defaults, the `innodb_dedicated_server` manual page, the lock-free WAL design post, the P_S overhead post, the manual's sysvar and InnoDB parameter pages, Dimitri Kravtchuk's pre-FOSDEM 2025 slides (dimitrik.free.fr was unreachable), and MySQL bugs 116463, 86246 and 87933.
- **Other.** The HammerDB spin-multiplier test, Releem, MariaDB docs on the flush-method split and MDEV-30136, and lefred's critique of the Percona 2026 benchmark.
- OS and network topics are covered in depth in [`os-network-tuning.md`](os-network-tuning.md). This file only lists the OS ideas that MySQL-specific sources make.

**Notation.**
- **TRADE-OFF(D)** means durability or crash-safety is weakened.
- **TRADE-OFF(S)** means query semantics or consistency change.
- **TRADE-OFF(O)** means observability or operability is lost (replication, PITR, P_S).
- Lines marked **Local** are our own earlier measurements (`mysqltune` study 2026-10-17 on 8 vCPU with HammerDB, and aituner sessions on 2 to 16 vCPU). They are not web sources.
- Status counters are written as `SHOW GLOBAL STATUS` names. *IM* is `information_schema.innodb_metrics`.

Total: **96 ideas**, in groups A to K.

---

## A. Redo log and commit path

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| A1 | `innodb_log_writer_threads` ON→**OFF** | Dedicated log writer/flusher threads cause **3-200× more redo fsyncs per commit**. OFF is always faster on 8 cores (up to 3×) and up to 100× faster on a 32-core box with slow fsync. ON helps only at the highest concurrency on 40 cores. Dimitri says "small systems up to 8 cores ⇒ REDO threads = OFF". | 8.0.22+ (not in MariaDB) | [S6] 2024-01-03, [O3] 2025-02, [O7] | **Contradicted:** a 2021 forum user saw IO double with ON, but commit latency rose 4× with OFF (0.5→2.1 ms) [P27]. **Local:** OFF gave +8-10% on 2-8 vCPU in 3 of 4 sessions. ON was better on the binlog HammerDB run (p99 halved). | vCPU ≤ 16, or `Innodb_os_log_fsyncs`/`Com_commit` ≫ 1, or redo fsync latency > 1 ms, or `Threads_running` < 2×CPU. Try ON when `Threads_running` ≫ CPU and log_sys mutex waits dominate. |
| A2 | `innodb_redo_log_capacity` → ≈1 h of redo; try 4G/8G/16G/32G | A larger redo lowers LSN-driven (sync) flushing and allows more write combining. Target is 1 hour of LSN growth. | 8.0.30+ (dynamic). Before that, `innodb_log_file_size × innodb_log_files_in_group` | [P6] 2023-03-06, [P1] 2020-05-14, [S11] 2022-10 | Mark: 16G vs 32G had minimal effect on a 15-core box (a11/a12) [S2]. Recovery time grows. 8.4 `innodb_dedicated_server` uses vCPU/2 GB with a 16 GB cap [O2]. **Local:** 16G gave no gain when CPU-bound. | Checkpoint age (`Innodb_redo_log_current_lsn − Innodb_redo_log_checkpoint_lsn`) > 70-75% of capacity, or *IM* `buffer_flush_sync_*` rising, or 1 h of LSN growth > capacity. |
| A3 | `innodb_log_buffer_size` 16M → 64M/128M/256M | Fewer forced redo writes for large transactions. | All; 8.4 default 64M | [O1] 2024, [P15], Mark cz13a uses 64M [S1] | Rarely a bottleneck once ≥ 64M. | `Innodb_log_waits` > 0, or large-transaction workload. |
| A4 | `innodb_flush_log_at_trx_commit` 1→**2** (or 0) **TRADE-OFF(D)** | Write to the OS cache at commit and fsync about once per second. Removes the per-commit fsync. | All | Mark uses 2 in all benchmark configs [S1]. Dimitri insists on 1 ("data safety first") [O3]. | Huge win when fsync is slow; small when the device has power-loss-protected cache. Loses up to ~1 s of commits on an OS crash. | Policy allows D trade-off and commit latency is dominated by redo fsync (`Innodb_os_log_fsyncs` ≈ commits, fsync > 0.5 ms). |
| A5 | `sync_binlog` 1 → 1000 / 0 **TRADE-OFF(D)** | Fewer binlog fsyncs. With **0**, the OS flushes a whole binlog at rotation, giving a **stall about every 40 s**. **1000** is a middle ground. | All | [P8] 2018-05-04 | Old hardware (2018 SSD). Stall size scales with `max_binlog_size` and dirty-page settings. | Binlog on, D trade-off allowed, binlog fsync latency high. If 0 is chosen, watch for periodic throughput dips at rotation. |
| A6 | `innodb_use_fdatasync` OFF→ON | fdatasync skips metadata flush. Claimed "performance benefit". | 8.0.26+; default ON in 8.4 | [O1] | No independent numbers found. | 8.0.26-8.0.x with OFF on a write-heavy load. |
| A7 | `innodb_log_spin_cpu_abs_lwm` (80) / `innodb_log_spin_cpu_pct_hwm` (50) / `innodb_log_wait_for_flush_spin_hwm` | User threads spin while waiting for redo flush only if CPU is free. Spinning cuts latency when CPU is idle and wastes CPU when saturated. | 8.0.11+ | [O4] 2018-06-18, [S6] mentions | Rarely tested publicly. | CPU saturated with redo waits → raise abs_lwm / lower pct_hwm to stop spinning. CPU idle with high commit latency → the opposite. |
| A8 | `binlog_group_commit_sync_delay` 0 → 100…1000 µs (+ `_no_delay_count`) | Bigger binlog fsync groups at high concurrency. | 5.7+ | [O9] bug 86246 | Bug report: 700k → 10k TPM with delay = 500 ms (absurd value) and sync_binlog = 0. Closed "can't repeat". **Local:** 500 µs gave no gain on HammerDB 64 VU. | Only with sync_binlog = 1, binlog fsync > 1 ms and many concurrent committers. Never with sync_binlog = 0. |
| A9 | `innodb_flush_method` → `O_DIRECT_NO_FSYNC` | Skips the fsync after data-file writes. Mark finds O_DIRECT_NO_FSYNC > O_DIRECT > fsync on write-heavy steps; MariaDB 11.4 lost about 10-15% when it lost this mode. | MySQL 5.6+ (8.0 dedicated_server chose it; 8.4 default O_DIRECT) | [S12] 2024-04-13, [S1] | The MariaDB docs call it unsafe [M1]: file-extension metadata may not be durable on some filesystems. Treat as a mild **TRADE-OFF(D)**. | Write-heavy run and data-file fsyncs visible (`Innodb_data_fsyncs` high). |
| A10 | `innodb_flush_method` fsync → **O_DIRECT** | Avoids double caching and the multi-second write stalls seen with fsync. a4 (fsync) caused about 100 s write stalls. | All; 8.4 default | [S2] 2023-06-13, [O1] | Mark still uses fsync plus a small BP for his "cached by OS" scenario, so buffered IO can win when the BP is small and RAM is free. | Current value fsync and BP ≥ 50% of RAM. Reverse test when BP ≪ RAM and the OS page cache is idle. |
| A11 | `innodb_doublewrite` ON → OFF / `DETECT_ONLY` **TRADE-OFF(D)** | Halves page-write IO and removes doublewrite contention on single-page flush. Mark's a17 showed "substantial but unreliable" gains and fewer flush stalls. | OFF: all. DETECT_ONLY: 8.0.30+ (dynamic between ON and DETECT_*) | [S3] 2023-07-17, [S5] 2023-08-12, [O7] | Only safe on storage with atomic 16 KiB writes (see os-network-tuning #3). Mark suspects his SSD was failing during a17. | Write-bound IO, policy allows D, or storage guarantees atomic 16K writes. |
| A12 | `innodb_doublewrite_pages` 4 → **128** | The 8.0.20+ default (= write_io_threads = 4) makes doublewrite batches tiny. 128 gave **−55% write IOPs, −10% read IOPs** (single-thread inserts with secondary indexes). | 8.0.20-8.0.x (8.4 default 128) | [P12] 2025-11-05, [O1] | One workload, single thread. | Version 8.0.20+ and value < 64. |
| A13 | MariaDB `binlog_storage_engine=innodb` | Binlog kept inside InnoDB, so 1 fsync per commit instead of 2. Durable configs: 1.75× load and **4-6.8× random writes** on a slow-fsync small server; ~1.08-1.3× on a fast-fsync 48-core box. | MariaDB 12.3+ | [S14] posts 2026-02-16 … 2026-03-18 | New feature; tooling and replica compatibility not reviewed. | MariaDB ≥ 12.3, binlog on, sync_binlog = 1 and trx_commit = 1. |
| A14 | Disable binlog (`disable_log_bin`) **TRADE-OFF(O)** | MySQL 8.0 lost **up to 30%** with binlog on (vs 13% for PS 5.7). | 8.0+ (binlog on by default) | [P7] 2018-05-04, [O3] slides 23-24 | **Local:** +20% (HammerDB 8 vCPU). No PITR or replication without it. | Binlog not required by policy (bench / ephemeral replica). |

## B. Flushing and checkpoint

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| B1 | `innodb_io_capacity` / `_max`: **two schools** (low 200-2000/2000-4000 vs high 10000/20000; Mark a2: 20k/40k) | **Low:** dirty pages stay longer and absorb more updates (62 vs 20 updates per flushed page at 100 vs 5000), with less SSD wear. **High:** keeps up on NVMe; a2 made index creation 2× faster on the 80-core box. | All; 8.4 default 10000 | [P2] 2019-12-18, [P1] 2020-05-14, [S4] 2023-08-03, [S2] | Mark on a 15-core box: "despite 19 variants … no significant improvement" [S3]. Percona's advice predates 8.0.19+ flushing changes. **Local:** io_cap 2000 cut page writes 3.3× with no TPS change (CPU-bound). | Checkpoint age < 30% but page writes/s high → **lower**. Checkpoint age > 75% or sync flushes → **raise `_max`**. "intended loop took N ms" in the error log → lower `_max` [P1]. |
| B2 | `innodb_max_dirty_pages_pct_lwm`: **80** (Mark) vs **0** (Percona) vs 10 (8.0 default); `md1` = 0.1 | High lwm lets dirty pages accumulate (write combining). Percona says 0 disables the "old 5.0-era" pre-flushing. Mark's a6 (40/50) **boosted queries** at a small insert cost [S2]. | 5.7+ | [S1], [S2], [P1] | Directly contradictory advice; workload-dependent. | Write-heavy: test 10 vs 50 vs 80. Read latency under writes: test lower. |
| B3 | `innodb_max_dirty_pages_pct` 90 → 50 | Fewer dirty pages ease the page cleaners and reduce single-page flushes. | All | [S10] 2023-07-06, [S2] a6/a19 | Proposal plus mixed results. | `Innodb_buffer_pool_wait_free` > 0 or single-page flush evidence. |
| B4 | `innodb_adaptive_flushing_lwm` 10 → 20-30 (max 70) | Starts adaptive flushing later. Percona: raising it "causes stalls"; prefer a bigger redo. | 5.6+ | [P1] | Few numbers. | Only after A2 is maxed and checkpoint age oscillates. |
| B5 | `innodb_flush_sync` ON → OFF | Respects io_capacity_max even during sync flush (protects reads). | 5.7+ | [P1], [S2] a3 | a3 gave "minor improvements and regressions". | Read p99 spikes coincide with sync-flush events. |
| B6 | `innodb_idle_flush_pct` 100 → 1 | Less flushing when idle (SSD wear, change-buffer merge priority). | 8.0.19+ | [S1] (Mark sets 1), [P1] | A15 test (removing it) had no clear effect. | Bursty workload with idle periods. |
| B7 | `innodb_flush_neighbors` 1 → 0 on SSD | No seek benefit on SSD; spreads writes. | Default 0 since 8.0 | [O7], [S1] | Only matters on 5.7 or with old configs. | Value ≠ 0 on SSD/NVMe. Value 1 on HDD. |
| B8 | `innodb_page_cleaners` = `innodb_buffer_pool_instances` | More BP instances than cleaners leads to more single-page flushes. Mark's a18 (4/4) was "generally good". | 5.7+; 8.4 default = BP instances | [S10], [S4] | Mark's a10 (8 purge + 8 cleaners) was harmful on a medium box but good on a large IO-bound one. | cleaners < BP instances, or `Innodb_buffer_pool_wait_free` > 0. |
| B9 | `innodb_lru_scan_depth` 1024 → 2048/4096/8192 (Mark's MariaDB config 5000); Percona formula `(cur − min_free_seen) × 4` | Deeper LRU scan gives more free pages and fewer single-page flushes. | All | [S2]-[S4], [P1], [S11] | "Inconsistent, marginal" across three servers. Larger values add LRU mutex contention [S10]. | `Innodb_buffer_pool_wait_free` > 0, or "Free buffers" near 0 in IO-bound runs. |
| B10 | PS `innodb_cleaner_lsn_age_factor` high_checkpoint ↔ legacy | Percona's formula ramps flushing slowly, then fast, at high checkpoint age. Use legacy for extreme bursts. | PS only | [P1], [P13] | No numbers. | PS with checkpoint-age bursts. |
| B11 | PS `innodb_empty_free_list_algorithm` legacy ↔ **backoff** | Backoff stops query threads from doing single-page LRU flushes while holding mutexes. | PS 5.6+ | [P14] 2016-05-05, [P13], Mark ps1/ps2 [S1] | **Default conflict:** 2020 blog says backoff; 8.4 docs list legacy. Mark tested both but no result was found. | PS, `Innodb_buffer_pool_wait_free` > 0. |
| B12 | PS `innodb_lru_threads` OFF → ON | One LRU flusher thread per BP instance. | PS 8.4 | [P13], [P14] | No 8.4 numbers. | PS, IO-bound and highly concurrent. |
| B13 | Benchmark hygiene: `innodb_buffer_pool_load_at_startup/dump_at_shutdown` | Mark disables both so cache state is controlled. Production wants them ON for faster warm-up. | 5.6+ | [S1] | Not a throughput knob. | Tuner should pin these for reproducible runs. |

## C. Buffer pool, LRU, change buffer, AHI, purge, page format

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| C1 | `innodb_buffer_pool_size` → 75% RAM if dedicated (50-80%) | Caches the working set. | All | [O2], [R1] 2024-04-22 | Over-commit causes swap or OOM. **Local:** +1 GB on 16 GB caused swapping. | Read IO/s high and free RAM ≥ 10%. Shrink if swap-in or PSI memory > 0. |
| C2 | `innodb_buffer_pool_instances` 8 → 16/32/64 | Less BP mutex contention. **64 best** on 80 vCPU for both IO-bound and CPU-bound, with fewer stalls. | MySQL/PS (MariaDB ≥ 10.5 ignores or removed it) | [P22] 2020-08-13, [P23] 2020-08-14 | One machine (2×Xeon 6230). Mark uses 4 on 8 cores. 8.4 auto-sizes. | Many cores (≥ 32) with throughput stalls/variance, or BP mutex waits in P_S. |
| C3 | `innodb_change_buffering`: none ↔ all / inserts | Buffers secondary-index changes for uncached pages. **8.4 default none** ("modern storage"). Mark (2023): none **doubled KB written per insert** and broke SLAs when IO-bound. 2026: similar when cached, more IO when IO-bound. Dimitri: IBUF "helps IO-bound, beware side effects" (BP shrink, recovery). | MySQL all (removed in MariaDB 11.x per its docs; verify) | [O1], [S2], [S1] (2026 cz12a_nocb), [O3] | Clear conflict between the vendor default and benchmarks. | IO-bound, write-heavy, tables with secondary indexes → test `all` vs `none`. Watch IO per transaction and `Innodb_ibuf_*` merges. |
| C4 | `innodb_change_buffer_max_size` 25 → 50 | More room for buffered changes on insert-heavy loads. | MySQL | (hypothesis; Mark uses 25) | Unsourced value. | Only if C3=`all` wins and the ibuf is full. |
| C5 | `innodb_adaptive_hash_index` OFF ↔ ON | AHI speeds B-tree descents (non-covering SI range scans were 1.75× slower without it at 1 thread; point queries ~3%) but contends at high write concurrency. 8.4 default OFF. | All | [S19] 2014 posts, [O1], [O3] | 2014 data is from 5.7.5. **Local:** ON +3.3% (HammerDB 8 vCPU), large win on TPC-H, neutral on sysbench. | CPU-bound and read-heavy with SI lookups → try ON. Many `btr_search` latch waits → OFF. |
| C6 | `innodb_adaptive_hash_index_parts` 8 → 16/32 | Shards the AHI latch. | 5.7+ | [O7] | No benchmark found. | AHI ON and AHI latch contention remains. |
| C7 | `innodb_max_purge_lag` 0 → 50k/200k/**500k** + `innodb_max_purge_lag_delay` = 1000000 | Throttles DML when history grows. Without it the DB grows **2×+** and some queries got "1000× slower". Write rate fell about 240k → 90k/s in-memory. | All | [S4] 2023-08-03, [S1] (in all current configs) | Throughput vs space/read-stability trade-off. a13/a20/a21 failed some SLA steps when IO-bound. | History list length (*IM* `trx_rseg_history_len`) > 1M and rising, or DB size growing faster than data. |
| C8 | `innodb_purge_threads` 1 / 4 / 8 | More purge parallelism. 8.4 uses 1 on ≤ 16 vCPU. | All | [S2] (a10 harmful, medium), [S4] (good, large IO-bound), [O1] | Opposite results on different boxes. | HLL rising with idle CPU → raise. Small box with CPU saturated → 1. |
| C9 | `innodb_numa_interleave` OFF → ON | Even BP allocation across NUMA nodes avoids per-node swapping. | 5.7+; 8.4 default ON | [P3] 2018-07, [P4] 2020-07-06, [O1] | Single-socket or VM: no-op. | `numactl --hardware` shows > 1 node. |
| C10 | PS `innodb_buffer_pool_populate` | Prefaults the BP at startup (separate from interleave since 8.4.11). | PS 8.4.11+ | [P15] | Startup-time knob only. | Slow warm-up / page-fault spikes after start. |
| C11 | `innodb_page_size` 16k → 8k/4k (needs a new instance) | Matches SSD 4K pages: **1.6-1.73×** TPC-C on a PM1725, IO-bound. Worse on a consumer 850 Pro. +21% disk usage. | 5.7+ | [P18] 2016-08 | 2016 hardware. **Local:** 8k neutral (in-memory HammerDB). Expensive (rebuild). | IO-bound on enterprise NVMe; only when schema/rebuild changes are allowed. |
| C12 | Transparent page compression (`COMPRESSION='lz4'`) | Up to **50%** gain IO-bound (1 TB) when data compresses. | 5.7+, needs hole-punch FS | [O3] slide 42 | Table-level DDL; fragmentation; CPU cost. | IO-bound, data compressible, CPU headroom. |
| C13 | `innodb_read_io_threads` / `innodb_write_io_threads` 4 → 8-16 (8.4 read = CPUs/2) | More AIO request slots. | All | [O1], [R1], [S1] | Few numbers; NVMe queue depth matters more. | Pending reads/writes (`Innodb_data_pending_*`) > 0 consistently. |
| C14 | `innodb_use_native_aio` ON → OFF | a1: **queries improved, inserts got worse**. Native AIO overloaded the IO queues. | All | [S2] | Odd result; possibly device-specific. | Low priority; IO-bound read-mostly with deep IO queues. |

## D. Concurrency and contention

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| D1 | `innodb_thread_concurrency` 0 → CPUs (e.g. 8, 16) | Limits threads inside InnoDB. =8 was slightly best on 8 vCPU, and it is dynamic. | All | [P10] 2024-01-02 | Releem's "2×CPU + disks" formula is 5.x folklore [R1]. **Local:** 16 regressed HammerDB. | `Threads_running` > 2× vCPU with high context switches/s. |
| D2 | Thread pool (`thread_handling=pool-of-threads`, `thread_pool_size`=CPUs) | Bounds runnable threads for many short queries at very high concurrency. **Hurts with long queries** (QPS "to almost zero"). | PS, MariaDB, MySQL EE | [P10] | **Local:** −13% (HammerDB; smaller commit groups) and regressed on sysbench. | Connections ≫ cores, short statements, throughput collapse beyond N threads. |
| D3 | `innodb_spin_wait_delay` 6 → **5** (scan 2…12) | 8.0.30 changed the random range (+1), giving **+20% average busy-wait**. Reverting the range equals 5. | 8.0.30+, 8.4, 9.x | [S22] bug 116463 (2024-10-23) | Effect size on throughput unmeasured. **Local:** 1 was neutral. | CPU-bound with high mutex/rw-lock spin (`perf` shows `ut_delay`). |
| D4 | `innodb_sync_spin_loops` 30 → 10 | Fewer spin rounds before sleeping. | All | (no web benchmark); [O7] | **Local:** +10% on 8 vCPU with binlog off (max latency 23 s → 0.5 s), but a "+2.75%" result on go-tpc was drift. | CPU-saturated small box with many runnable threads. |
| D5 | `innodb_spin_wait_pause_multiplier` 50 → 25 (never 5) | Shorter PAUSE loops on CPUs with long PAUSE latency (Skylake ~140 cycles). 25 slightly better up to 40 users; 5 was far worse. | 8.0.16+ | [O8] 2019-05-05 | HammerDB conclusion: keep the default. | Low priority; only with heavy spin on Skylake+. |
| D6 | `innodb_sync_array_size` 1 → 8/16 (restart) | Splits the wait array for > 768 waiting threads. | All | [O7] | Manual only. | Thousands of connections and sync-array waits. |
| D7 | `table_open_cache` → 200000 and `table_open_cache_instances` → 64 (vs manual 8-16, default 16) | Avoids cache thrash and splits the table-cache mutex. | 5.6+ | [P11] 2026-03-26, [O6] | Percona's values are generous; the manual warns about memory with large triggers. | `Table_open_cache_misses` or `_overflows` rising, or `Opened_tables` growing. |
| D8 | `transaction_isolation` RR ↔ RC **TRADE-OFF(S)** | RR builds one read view per transaction instead of per statement, giving 5-10% better RW at high concurrency (5.6) [S16]. RC failed to scale past 36 threads on 144 cores (5.7, about 3× slower) [P17]. RC means fewer gap locks and less history. | All | [S16] 2016-11-21, [P17] 2017-01-19 | Old versions. RC trx_sys contention largely fixed in 8.0 (not verified). | RR→RC when there are lock waits or deadlocks from gap locks and the app allows it. RC→RR when trx_sys/read-view mutex is hot. |
| D9 | MariaDB `innodb_snapshot_isolation` → OFF **TRADE-OFF(S)** | Mark disables it in MariaDB 12 configs (it adds write-conflict checks). | MariaDB 11.x+ | [S1] (ma1203 cz12c) | No measured result found. | MariaDB, write-heavy, app tolerant of the old RR behaviour. |

## E. CPU efficiency

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| E1 | `performance_schema=OFF` **TRADE-OFF(O)** | Instrumentation overhead. 1-3% CPU-bound with defaults (2015) [O5]. Mark: 5-10% (2016) [S16], 4-11% plus 1.5× faster index builds (2024) [S17]. A P_S counter serialized parallel index builds (2× slowdown, bug 113505) [S15]. | All | [O5] 2015-04-17, [S16], [S17], [S15] 2023-12-24 | Old 2011 figures of 25% [P21] are outdated. **Local:** +4.6% (b07 vs b04). | CPU-bound and P_S not required by policy. |
| E2 | Lighter P_S (disable `memory/%` instruments and history consumers) | Keeps P_S at lower cost. | 5.7+ | [O5] (cost scales with instruments) | **Local:** neutral. | E1 not allowed. |
| E3 | `gtid_mode=OFF`, `enforce_gtid_consistency=OFF` **TRADE-OFF(O)** | 9.5 made GTID default ON: CPU/op +10-15%, context switches +23-45%, writes +16-24%, 5-17% lower write throughput. | MySQL 9.5+ | [S7] 2025-12-17 | Needed for most HA tooling. | 9.5+, write-heavy, no GTID replication required. |
| E4 | `character_set_server=latin1` vs utf8mb4 **TRADE-OFF(S)** | About 5% CPU difference noted. | All | [S13] 2026-04-03 | Benchmark-only; app charset decides. | Never in production unless the schema is latin1 already. |
| E5 | `eq_range_index_dive_limit` 200 → 10 | Fewer optimizer index dives for long IN-lists (statistics instead). | 5.6+ | Mark uses 10 [S1] | No measured result; plan quality risk. | Long IN-list queries with high optimize time. |
| E6 | Server-side prepared statements / Unix socket (client side) | Removes parse/optimize CPU and the TCP stack. Dimitri: "test prep.stmt on/off, then socket vs IP". | n/a | [O3] slides 13, 31-32; [S21] (Postgres +25-50%) | MySQL-specific numbers not in the sources read. | Client config experiment; CPU in the parser. |
| E7 | TLS: avoid OpenSSL 3.0 (3.1+ or 1.1.1 better); avoid per-request TLS connects | OpenSSL 3.x loses 15-20% vs 1.1.1. SSL reconnects are 3× slower at low load. | build/OS | [O3] slides 37-38 | Platform-dependent. | High `Connections`/s with SSL on. |
| E8 | Build level: PGO (+6.5% avg, up to 14%), LTO/-O3 (+10-20% with P_S compiled out), text in huge pages (+5-9%) | Better i-cache/iTLB use. | n/a | [P26] 2026-05-22, [S17], [S20] | Not knobs; tuner can only recommend a binary. | CPU-bound with high iTLB/i-cache misses. |
| E9 | `innodb_autoinc_lock_mode=2` | Interleaved auto-inc locking. | Default 2 since 8.0 | [S1] | **TRADE-OFF(S)** with statement-based binlog. | 5.7 or value ≠ 2 with ROW binlog and concurrent inserts. |
| E10 | `optimizer_switch='hypergraph_optimizer=on'` | New join optimizer. Mark's cz13a_hg variant. | 9.x builds that ship it | [S1] | No result read; plan regressions possible. | OLAP or join-heavy sessions, per-query test. |
| E11 | `innodb_parallel_read_threads`, `innodb_ddl_threads`, `innodb_ddl_buffer_size` | Parallel clustered-index scans (COUNT(*), CHECK) and index builds. | 8.0.14+ / 8.0.27+ | [S15], [O1] (8.4 = CPUs/8) | Helps DDL and maintenance, not OLTP. | Maintenance-window DDL or COUNT(*) heavy loads. |

## F. Temp tables, sort, join

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| F1 | `sort_buffer_size` 256K → 512K/1M/2M (session-level preferred) | Fewer merge passes. The manual warns that Linux allocation slows above **256 KB and 2 MB**. Percona 2010 found the sweet spot at 440K-6.4M with no 256K penalty. | All | [O6], [P16] 2010-10-25, [R2] | The 2006/2010 mmap horror stories predate modern glibc. | `Sort_merge_passes`/s > 0 sustained. |
| F2 | `join_buffer_size` (default 256K) | Used by BNL and hash join (8.0.18+). Increasing it globally rarely helps. | All | (unsourced; manual knowledge) | Per-join allocation × connections. | Hash joins spilling to disk in EXPLAIN ANALYZE; analytic sessions only. |
| F3 | `temptable_max_ram` (8.4: 3% RAM, 1-4 GB), `temptable_use_mmap` OFF, `temptable_max_mmap` | Shared pool for internal temp tables. 8.4 overflows to InnoDB, not mmap files. | 8.0+ | [O1], [P15] | 8.0.32 temp-table inserts were 10-15% slower than 5.7 [P28] (unresolved). | `Created_tmp_disk_tables`/`Created_tmp_tables` > 5-10%. |
| F4 | `internal_tmp_mem_storage_engine=MEMORY` with `tmp_table_size`/`max_heap_table_size` | Old engine may be faster for small temp tables (5.7 vs 8.0 regression). | 8.0+ | [P28] 2023-06 | Forum anecdote; MEMORY lacks BLOB/TEXT support. | Temp-table-heavy queries regressed vs 5.7. |
| F5 | `default_tmp_storage_engine=MyISAM` (explicit CREATE TEMPORARY) | Mark's benchmark setting. | ≤ 8.x | [S1] | Unclear benefit; MyISAM being phased out. | Low priority. |

## G. Connections and caches

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| G1 | `max_connections` sized to the real need (Dimitri uses 4000 in a bench config) | Avoids connection refusals; memory per thread. | All | [O3] slide 28 | Not a throughput knob. | `Max_used_connections` ≥ 90% of max. |
| G2 | `thread_cache_size` | Reuses threads on connect churn. | All | (unsourced) | Autosized in 8.0. | `Threads_created`/`Connections` > 1%. |
| G3 | `table_definition_cache`, `open_files_limit`/LimitNOFILE | Avoids reopening .ibd/definitions. | All | (unsourced) | Usually autosized. | `Opened_table_definitions` rising, "Too many open files". |
| G4 | `skip_name_resolve` and the rest of the network side | See os-network-tuning.md §6. | | | | |

## H. Binlog and replication

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| H1 | `binlog_transaction_dependency_history_size` 25000 → 1000-5000 | 8.4 always tracks WRITESET in a `std::map` (log-time lookups). PS 8.4 replaced it with a hash map: up to 17% better writes. A smaller history means a cheaper map (hypothesis). | 8.4+ (`binlog_transaction_dependency_tracking` removed) | [P9] 2024-08-26 | Smaller history reduces replica parallelism. | MySQL 8.4+, binlog on, CPU-bound write-heavy; `perf` shows writeset map. |
| H2 | Use Percona Server 8.4 instead of MySQL 8.4 for binlog write-heavy workloads | Same fix. | 8.4 | [P9] | Vendor-reported. | Recommendation only. |
| H3 | `max_binlog_size` 1G → 256M (Mark) | Smaller files mean smaller page-cache flush bursts at rotation with sync_binlog=0 (combines with A5). | All | [S1], [P8] | Hypothesis combining two sources. | sync_binlog=0 and periodic stalls at rotation. |
| H4 | `binlog_format=ROW` + `binlog_row_image=MINIMAL` | Smaller binlog events. | All | (no web benchmark) | **Local:** +0.6% (noise). | Wide-row updates with binlog bytes/s high. |
| H5 | `binlog_cache_size` raise | Avoids spilling transaction binlog cache to temp files. | All | (unsourced) | | `Binlog_cache_disk_use` > 0. |
| H6 | Replicas: `replica_parallel_workers` 4 → 8-32, WRITESET (8.4 default), `replica_preserve_commit_order` | Parallel apply. 8.0.1 replication improvements gave up to +25-65% on sysbench in non-durable mode. | 8.0+ | search result summary only (not opened) | Not verified here. | Replica lag with applier CPU idle. |
| H7 | Relaxed durability on replicas only (trx_commit=2, sync_binlog=0) **TRADE-OFF(D)** | Replica rebuildable from the source. | All | derived from A4/A5 | Policy decision. | Role = replica and lag > target. |

## I. OS and filesystem (MySQL-specific claims; details in os-network-tuning.md)

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| I1 | XFS, or ext4 with `dioread_nolock`; avoid ext4 on kernels 4.9-5.5 | ext4 O_DIRECT regression: up to 2× slower, 30-80% on NVMe. | kernel | [P19] 2019-11-12 | Fixed in 5.4/5.5+. 2012 test said ext4 > XFS [P20]; 2018: "use either" [P8]. | Old kernel with ext4. On new kernels, A/B test XFS vs ext4. |
| I2 | `noatime,nodiratime` | Skips atime metadata writes. | | [P5] 2023-09-28 | Small effect with O_DIRECT. | Mount has relatime/atime. |
| I3 | `vm.swappiness=1` | Avoids swapping BP pages. | | [P3], [P4], [P5] | 0 can trigger OOM. | `pswpout` > 0. |
| I4 | THP `never` (enabled + defrag) | Sparse access patterns; jemalloc+THP fails to return memory, leading to OOM. | | [P5], [P24] 2017-01-03 | THP=madvise is fine on Ubuntu. | THP=always. |
| I5 | `vm.dirty_ratio` low / `dirty_bytes` ~1 GB | Big write cache caused swap and stalls (20% of 48 GB ≈ 10 GB dirty). | | [P25] 2013-04-19 | Matters for buffered IO (binlog, fsync method). | `Dirty` in /proc/meminfo > 1 GB with stalls. |
| I6 | I/O scheduler `none` / mq-deadline (old advice: noop/deadline) | Less scheduler overhead on SSD/NVMe. | | [P3], [P4] | `elevator=` boot parameter is obsolete on blk-mq kernels. | Scheduler is bfq/kyber on NVMe. |
| I7 | CPU governor `performance` | No frequency ramp latency. | bare metal | [P4] | No-op on most VMs. | Governor ≠ performance. |
| I8 | Separate devices for data / redo / binlog | Isolates fsync latency. Mark keeps txlogs and binlogs in separate dirs. | | [P3], [S1] | Hardware change. | Redo fsync latency rises under data IO. |
| I9 | Explicit huge pages (`large_pages`, `vm.nr_hugepages`) | TLB: **InnoDB up to +6%** (Postgres +32%). Brittle (OOM, startup failures). | | [S9] 2023-02-13, [S20] | **Local:** PS 8.4 mmap errno 13 led to an OOM loop. | BP ≥ 16 GB and CPU-bound read workload. |
| I10 | `OOMScoreAdjust=-800` for mysqld | Kernel kills something else first. | | [P5] | Availability, not speed. | Always (safety). |
| I11 | Redo fsync latency check: `fio --ioengine=psync --bs=512 --fsync=1 --numjobs=1` | 1 ms fsync caps single-thread commit at 1000 TPS. | | [O3] slide 22, 39 | Diagnostic. | Run before A1/A4/A13 decisions. |

## J. Allocators and memory

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| J1 | jemalloc / tcmalloc (`malloc-lib`, `LD_PRELOAD`) | InnoDB: **+2.5% (jemalloc) / +3.5% (tcmalloc)** QPS. RSS is similar for InnoDB (1.06×); MyRocks glibc RSS was 3.6×. Dimitri: "glibc-malloc ⇒ DANGER", +10 GB RSS in a week; tcmalloc "most efficient", jemalloc "may have allocation spikes". Mark uses jemalloc in every config. | All | [S8] 2025-04-11, [O3] slides 33-36, [P24], [S1] | Small throughput effect for InnoDB; the main value is RSS stability. | RSS ≫ BP + tracked memory, or RSS growth over hours, or CPU in malloc. |
| J2 | tcmalloc vs jemalloc A/B | Dimitri prefers tcmalloc; Mark found tcmalloc slightly better for InnoDB. | All | [S8], [O3] | | After J1 wins. |

## K. Version-specific: Percona and MariaDB

| ID | Knob → candidates | Claimed mechanism; helps / hurts | Versions | Sources | Skepticism | Experiment trigger |
|---|---|---|---|---|---|---|
| K1 | Re-baseline on 8.4: drop 8.0-era overrides | 8.4 changed ~20 InnoDB defaults (AHI off, ibuf none, io_capacity 10000, log buffer 64M, dblwr pages 128, fdatasync on, NUMA interleave on, auto BP instances/cleaners/read threads, purge threads 1 on ≤ 16 vCPU, temptable). | 8.4+ | [O1], [P15] | Some new defaults are contested (C3, C5, A1). | Upgraded instance with an old my.cnf. |
| K2 | `innodb_dedicated_server=ON` | Auto BP (75% > 4 GB), redo (vCPU/2 GB, max 16 GB). No flush_method in 8.4. Dimitri: "just default config + dedicated=ON". | 8.0+ | [O2], [O3] slide 28 | Mark's configs leave it ON but override most values. | Dedicated host with untuned config. |
| K3 | MariaDB: `innodb_log_file_buffering=ON`, `innodb_data_file_buffering=OFF`, `*_write_through=OFF` (Mark cz12c) | Replaces `innodb_flush_method` (deprecated 11.0). All four are dynamic. No equivalent of O_DIRECT_NO_FSYNC (MDEV-33545). | MariaDB 11.0+ | [M1], [M2], [S12], [S1] | Effect of log buffering ON vs OFF is not measured in the sources read. | MariaDB ≥ 11: test log buffering ON vs OFF with trx_commit=1. |
| K4 | MariaDB `innodb_log_file_size` large (Mark: 100G with 100G BP; lefred: ≥ 4G for parity) | Single redo file; `innodb_log_files_in_group` ignored. | MariaDB 10.5+ | [S1], [O11] 2026-04-05 | | Same trigger as A2. |
| K5 | MariaDB `innodb_log_write_ahead_size=4096` | Avoids read-modify-write of the redo block (block size adapts since 10.8). | MariaDB | [S1], [M1] | Unmeasured. | Redo on 4K-sector device with read IO on the redo file. |
| K6 | MariaDB `innodb_undo_tablespaces=32` | More undo tablespaces (purge/truncate parallelism hypothesis). | MariaDB | [S1] | Unmeasured; restart/init-time. | HLL high on MariaDB. |
| K7 | MariaDB `query_cache_type=0`, `query_cache_size=0` | QC mutex serializes. Mark disables it. (MySQL 8.0 removed the QC.) | MariaDB | [S1] | | QC enabled with concurrent writes. |
| K8 | MariaDB ≥ 10.5 point-query regression (BP instances removed): choose version | 50%+ point-query regression 10.2→12.3; MariaDB ~2× fewer QPS than MySQL on 5 of 9 point tests; MariaDB writes 1.3-1.5× faster than MySQL. | MariaDB | [S13] | Root cause is suspected, not proven. | Version choice advice only. |
| K9 | PS 8.4 thread pool, LRU threads, free-list backoff | See D2, B11, B12. | PS | [P13], [P10] | | |

---

## Top 15 to test first (ranked)

Ranking is by (likely effect × evidence quality × safety), with triggers so the tuner does not try them blindly.

1. **A1 `innodb_log_writer_threads=OFF`** on ≤ 16 vCPU or low concurrency. Strongest repeated evidence: Mark on three servers, Dimitri's guidance, and our own +8-10% wins. It is dynamic and costs nothing to try. Keep the reverse test for high-concurrency boxes because of [P27].
2. **A2 redo capacity sized to ≥ 1 h of LSN growth / checkpoint age < 70%.** Cheap, dynamic in 8.0.30+, and the mechanism is universal. The gain depends on being flush-bound.
3. **J1 jemalloc or tcmalloc.** Small QPS gain (+2.5-3.5%) but it removes RSS growth and OOM risk. Multiple independent sources agree.
4. **C3 `innodb_change_buffering` all vs none (IO-bound with secondary indexes).** The 8.4 default flipped to none, while Mark and Dimitri show write amplification doubling without it when IO-bound. Potentially large and needs a restart-free toggle (dynamic).
5. **C5 AHI ON vs OFF.** Dynamic. Large wins on read and analytic loads (local TPC-H, 2014 range-scan data) versus contention on write-heavy loads. Must be measured per workload.
6. **A12 `innodb_doublewrite_pages=128`** on 8.0.20-8.0.x. Halved write IOPs in Percona's test, and 8.4 adopted it as default.
7. **C7 `innodb_max_purge_lag` (200k-500k) + delay 1 s.** Prevents 2× space growth and pathological read slowdowns. It is a throughput-vs-stability trade, so the trigger should be HLL growth only.
8. **B1 io_capacity / io_capacity_max tuned by checkpoint age.** Lower when checkpoint age is low (write deflation), raise when sync flushing appears. Both schools agree on using checkpoint age as the control signal.
9. **B8 + C2 page_cleaners = BP instances; BP instances 8→16-64 on many-core.** Fixes single-page flush and BP stalls (Mark's reasoning, Vadim's 64-instance result).
10. **A10/A9 flush method: O_DIRECT (never fsync with a big BP), then O_DIRECT_NO_FSYNC as a mild trade-off.** Consistent ordering in Mark's MySQL and MariaDB tests.
11. **D3/D4 spin tuning: `innodb_spin_wait_delay=5`, `innodb_sync_spin_loops=10`.** Bug-documented +20% busy-wait in 8.0.30+. Local +10% on a small CPU-bound box, though one apparent win turned out to be drift, so repeat runs are required.
12. **E1/E2 P_S lighter or OFF (observability trade-off).** 1-11% across sources. Only if policy allows.
13. **E3 GTID OFF on 9.5+ (ops trade-off) and H1 dependency history size on 8.4+.** Measured 5-17% write regressions tied to defaults that changed in new versions.
14. **D7 `table_open_cache` / instances** when misses or overflows appear. Cheap, dynamic, low risk.
15. **A3 log buffer 64-256M** when `Innodb_log_waits` > 0. Trivial and safe; usually already fine on 8.4.

Gated trade-off experiments, which policy must allow explicitly: **A4** trx_commit=2, **A5** sync_binlog=0/1000, **A11** doublewrite OFF / DETECT_ONLY, **A14** binlog off, **D8** RC isolation, **E4** charset, **H7** relaxed replica durability.

## Ideas considered wrong, outdated or obsolete

| Idea | Why |
|---|---|
| `innodb_thread_concurrency = 2×CPUs + disks` [R1] | 5.0/5.1-era formula. Modern guidance is 0 (unlimited) or ≈ CPUs only under oversubscription [P10]. |
| Set `innodb_io_capacity` to the SSD's spec IOPS | Percona shows it destroys write combining and wears SSDs [P2]. 8.4 already defaults to 10000, so tune `_max` from checkpoint age instead. |
| `innodb_max_dirty_pages_pct_lwm=0` "to disable old flushing" [P1] | In 8.0 the default is 10 and Mark runs 80 successfully. It is a workload knob, not a fix. |
| Query cache tuning (MySQL) | Removed in 8.0. On MariaDB it is a scalability bottleneck; disable it (K7). |
| `innodb_log_file_size` / `innodb_log_files_in_group` | Deprecated in 8.0.30 and ignored when `innodb_redo_log_capacity` is set. MariaDB uses a single file. |
| `binlog_transaction_dependency_tracking=WRITESET` | Removed in 8.4 (always WRITESET); deprecated in 8.0.35 [P9]. |
| `innodb_buffer_pool_instances` on MariaDB ≥ 10.5 | Ignored or removed (and suspected as the cause of the regression) [S13]. |
| `innodb_flush_method` on MariaDB ≥ 11.0 | Deprecated; use the four boolean variables [M1][M2]. Some benchmarks still set it [O11]. |
| Percona `innodb_parallel_doublewrite_path` | 5.7-only. Superseded by the upstream 8.0.20 doublewrite files (`innodb_doublewrite_files/_pages/_dir`). |
| `innodb_checksums=OFF` | Removed in 8.0; checksums are crc32 by default, so the CPU gain is negligible. If tested at all, `innodb_checksum_algorithm=none` is **TRADE-OFF(D)**. |
| P_S costs 25% [P21] | 2011, MySQL 5.6.2. Later 1-3% [O5], or 4-11% in Mark's 2024 runs. |
| sort_buffer above 256K is 50× slower (2006-era) | glibc and MySQL changed. Percona's own 2010 test found no 256K penalty [P16]. Still keep it session-scoped. |
| ext4 beats XFS (2012) [P20] | Superseded by kernel fixes and the 4.9-5.5 ext4 regression [P19]. On current kernels the difference is small [P8]. |
| `elevator=noop` on the kernel command line [P4] | Single-queue elevators are gone on blk-mq kernels. Use `none`/`mq-deadline` via sysfs/udev. |
| Tweaking `innodb_spin_wait_pause_multiplier` | The HammerDB study found the default best and low values destructive [O8]. |
| ARM spin bug workaround (2017) [O10] | Pre-8.0.16 code. Revisit only with fresh ARM evidence. |
| "AHI OFF is always better" / "change buffer is useless on SSD" (8.4 default rationale) | Contradicted for read-heavy secondary-index workloads (AHI) and IO-bound write workloads (ibuf). Test, don't assume. |
| `innodb_doublewrite=OFF` because "SSD is fast" | Only safe with guaranteed atomic 16K writes; otherwise torn pages. |

## Notable contradictions (for the tuner's priors)

- **log_writer_threads**: OFF is better on small or low-concurrency boxes [S6][O3] and in our local data. ON gave lower commit latency at the cost of 28× more redo writes in one 2021 report [P27].
- **Change buffer**: the vendor default is none [O1]; independent benchmarks show it helps IO-bound writes [S2][O3].
- **AHI**: the vendor default is OFF [O1]; old and local data show large read wins [S19].
- **io_capacity and dirty lwm**: Percona says go low or 0 [P1][P2]; Mark uses 10000/20000 and lwm 80 [S1].
- **Purge threads and page cleaners at 8**: harmful on a 15-core box [S2], good on an 80-core IO-bound box [S4].
- **RC vs RR**: RR wins (5.6, [S16]); RC collapses on 144 cores (5.7, [P17]); RC slightly better in tpcc 2009-era (search result only).
- **Default `innodb_empty_free_list_algorithm` in PS**: backoff [P1] vs legacy [P13].

---

## References

Each key is followed by the URL that was opened and its date.

**Small Datum / Mark Callaghan**
- [S1]: https://github.com/mdcallag/mytools/tree/master/bench/conf (configs read via git: `arc/aug23.c2.inno.tune/*`, `arc/sep24/c8r32/my804|my8039/etc/my.cnf.cz1*`, `arc/oct24/c8r32/my97|my8406_rel_o2nofp/etc/my.cnf.cz12*|cz13*`, `arc/oct24/c32r128/ma1203/etc/my.cnf.cz12*`), files dated 2023-2026
- [S2]: https://smalldatum.blogspot.com/2023/06/tuning-innodb-for-insert-benchmark-on.html (2023-06-13)
- [S3]: https://smalldatum.blogspot.com/2023/07/tuning-innodb-for-insert-benchmark-on.html (2023-07-17)
- [S4]: https://smalldatum.blogspot.com/2023/08/tuning-innodb-for-insert-benchmark-on.html (2023-08-03)
- [S5]: https://smalldatum.blogspot.com/2023/08/tuning-innodb-for-insert-benchmark-on_12.html (2023-08-12)
- [S6]: https://smalldatum.blogspot.com/2024/01/innodblogwriterthreads-and-insert.html (2024-01-03)
- [S7]: https://smalldatum.blogspot.com/2025/12/performance-regressions-in-mysql-84-and.html (2025-12-17)
- [S8]: https://smalldatum.blogspot.com/2025/04/battle-of-mallocators.html (2025-04-11)
- [S9]: https://smalldatum.blogspot.com/2023/02/sysbench-with-huge-pages-for-postgres.html (2023-02-13)
- [S10]: https://smalldatum.blogspot.com/2023/07/fun-with-innodbpagecleaners.html (2023-07-06)
- [S11]: https://smalldatum.blogspot.com/2022/10/reasons-for-writeback-with-update-in.html (2022-10-18)
- [S12]: https://smalldatum.blogspot.com/2024/04/the-insert-benchmark-mariadb-mysql.html (2024-04-13)
- [S13]: https://smalldatum.blogspot.com/2026/04/cpu-bound-sysbench-on-large-server.html (2026-04-03)
- [S14]: https://smalldatum.blogspot.com/search?q=binlog_storage_engine (lists posts 2026-02-16, 2026-02-17, 2026-03-18 ×2)
- [S15]: https://smalldatum.blogspot.com/2023/12/create-innodb-indexes-2x-faster-with.html (2023-12-24)
- [S16]: https://smalldatum.blogspot.com/2016/11/sysbench-innodb-transaction-isolation.html (2016-11-21)
- [S17]: https://smalldatum.blogspot.com/search?q=performance_schema (summarizes 2024-01-01, 2024-01-25, 2024-09-11 posts)
- [S19]: https://smalldatum.blogspot.com/search?q=adaptive+hash+index (2014-10-04/06 posts)
- [S20]: https://smalldatum.blogspot.com/search?q=huge+pages (incl. 2024-10-06 "text in huge pages")
- [S21]: https://smalldatum.blogspot.com/search?q=prepared+statements (2021-2023 posts)
- [S22]: https://bugs.mysql.com/bug.php?id=116463 (Callaghan, 2024-10-23; release note 8.0.42/8.4.5/9.3.0)

**Percona**
- [P1]: https://www.percona.com/blog/tuning-mysql-innodb-flushing-for-a-write-intensive-workload/ (2020-05-14)
- [P2]: https://www.percona.com/blog/give-love-to-your-ssds-reduce-innodb_io_capacity_max/ (2019-12-18)
- [P3]: https://www.percona.com/blog/linux-os-tuning-for-mysql-database-performance/ (2018-07)
- [P4]: https://www.percona.com/blog/mysql-101-linux-tuning-for-mysql/ (2020-07-06)
- [P5]: https://www.percona.com/blog/dont-start-using-your-mysql-server-until-youve-configured-your-os/ (2023-09-28)
- [P6]: https://www.percona.com/blog/how-to-calculate-a-good-mysql-redo-log-size-in-mysql-8/ (2023-03-06)
- [P7]: https://www.percona.com/blog/how-binary-logs-affect-mysql-8-0-performance/ (2018-05-04)
- [P8]: https://www.percona.com/blog/how-binary-logs-and-filesystems-affect-mysql-performance/ (2018-05-04)
- [P9]: https://www.percona.com/blog/percona-server-for-mysql-performance-improvements-august-2024/ (2024-08-26)
- [P10]: https://www.percona.com/blog/is-thread-pool-plugin-the-right-choice-for-your-workload/ (2024-01-02)
- [P11]: https://www.percona.com/blog/2026-mysql-ecosystem-performance-benchmark-report/ (2026-03-26)
- [P12]: https://www.percona.com/blog/surprise-with-innodb_doublewrite_pages-in-mysql-8-0-20/ (2025-11-05)
- [P13]: https://docs.percona.com/percona-server/8.4/xtradb-performance-improvements.html (PS 8.4 docs)
- [P14]: https://www.percona.com/blog/percona-server-5-7-multi-threaded-lru-flushing/ (2016-05-05)
- [P15]: https://docs.percona.com/percona-server/8.4/8.4-defaults-and-tuning.html (PS 8.4 docs)
- [P16]: https://www.percona.com/blog/impact-of-the-sort-buffer-size-in-mysql/ (2010-10-25)
- [P17]: https://www.percona.com/blog/open-source-databases-big-machines-read-committed-scale-144-cores/ (2017-01-19)
- [P18]: https://www.percona.com/blog/small-innodb_page_size-performance-boost-ssd/ (2016-08-10)
- [P19]: https://www.percona.com/blog/watch-out-for-disk-i-o-performance-issues-when-running-ext4/ (2019-11-12)
- [P20]: https://www.percona.com/blog/ext4-vs-xfs-on-ssd/ (2012-03-15)
- [P21]: https://www.percona.com/blog/performance-schema-overhead/ (2011-04-26)
- [P22]: https://www.percona.com/blog/how-many-innodb_buffer_pool_instances-do-you-need-in-mysql-8/ (2020-08-13)
- [P23]: https://www.percona.com/blog/part-two-how-many-innodb_buffer_pool_instances-do-you-need-in-mysql-8-with-a-cpu-bound-workload/ (2020-08-14)
- [P24]: https://www.percona.com/blog/enabling-and-disabling-jemalloc-on-percona-server/ (2017-01-03)
- [P25]: https://www.percona.com/blog/swap-insanity-tome-iii-the-write-cache/ (2013-04-19)
- [P26]: https://www.percona.com/blog/mysql-9-7-0-pgo-benchmark-analysis/ (2026-05-22)
- [P27]: https://forums.percona.com/t/enormous-increase-in-number-of-redo-writes-in-8-0-with-innodb-log-writer-threads-and-slow-transactions-processing-without-it/11491 (2021-07)
- [P28]: https://forums.percona.com/t/mysql-8-0-slow-temp-table-populate/22962 (2023-06)

**Oracle / MySQL / HammerDB**
- [O1]: https://lefred.be/content/mysql-8-4-lts-new-production-ready-defaults-for-innodb/ (2024, 8.4 LTS release)
- [O2]: https://dev.mysql.com/doc/refman/8.4/en/innodb-dedicated-server.html (8.4 manual)
- [O3]: https://lefred.be/wp-content/uploads/2025/03/MySQL_Perf-preFOSDEM2025-dim.pdf (Dimitri Kravtchuk, pre-FOSDEM 2025)
- [O4]: https://dev.mysql.com/blog-archive/mysql-8-0-new-lock-free-scalable-wal-design/ (2018-06-18)
- [O5]: https://dev.mysql.com/blog-archive/performance-schema-great-power-comes-without-great-cost/ (2015-04-17)
- [O6]: https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html (8.4 manual)
- [O7]: https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html (8.4 manual)
- [O8]: https://www.hammerdb.com/blog/?p=378 (2019-05-05)
- [O9]: https://bugs.mysql.com/bug.php?id=86246 (2017)
- [O10]: https://bugs.mysql.com/bug.php?id=87933 (2017-10-02)
- [O11]: https://lefred.be/content/a-response-to-perconas-2026-mysql-ecosystem-benchmark-useful-data-but-not-a-realistic-mariadb-comparison/ (2026-04-05)

**MariaDB / Releem**
- [M1]: https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-flush-method
- [M2]: https://jira.mariadb.org/browse/MDEV-30136 (fixed 11.0.1)
- [R1]: https://releem.com/blog/innodb-performance-tuning (2024-04-22)
- [R2]: https://releem.com/docs/mysql-performance-tuning/sort_buffer_size

[S1]: https://github.com/mdcallag/mytools/tree/master/bench/conf
[S2]: https://smalldatum.blogspot.com/2023/06/tuning-innodb-for-insert-benchmark-on.html
[S3]: https://smalldatum.blogspot.com/2023/07/tuning-innodb-for-insert-benchmark-on.html
[S4]: https://smalldatum.blogspot.com/2023/08/tuning-innodb-for-insert-benchmark-on.html
[S5]: https://smalldatum.blogspot.com/2023/08/tuning-innodb-for-insert-benchmark-on_12.html
[S6]: https://smalldatum.blogspot.com/2024/01/innodblogwriterthreads-and-insert.html
[S7]: https://smalldatum.blogspot.com/2025/12/performance-regressions-in-mysql-84-and.html
[S8]: https://smalldatum.blogspot.com/2025/04/battle-of-mallocators.html
[S9]: https://smalldatum.blogspot.com/2023/02/sysbench-with-huge-pages-for-postgres.html
[S10]: https://smalldatum.blogspot.com/2023/07/fun-with-innodbpagecleaners.html
[S11]: https://smalldatum.blogspot.com/2022/10/reasons-for-writeback-with-update-in.html
[S12]: https://smalldatum.blogspot.com/2024/04/the-insert-benchmark-mariadb-mysql.html
[S13]: https://smalldatum.blogspot.com/2026/04/cpu-bound-sysbench-on-large-server.html
[S14]: https://smalldatum.blogspot.com/search?q=binlog_storage_engine
[S15]: https://smalldatum.blogspot.com/2023/12/create-innodb-indexes-2x-faster-with.html
[S16]: https://smalldatum.blogspot.com/2016/11/sysbench-innodb-transaction-isolation.html
[S17]: https://smalldatum.blogspot.com/search?q=performance_schema
[S19]: https://smalldatum.blogspot.com/search?q=adaptive+hash+index
[S20]: https://smalldatum.blogspot.com/search?q=huge+pages
[S21]: https://smalldatum.blogspot.com/search?q=prepared+statements
[S22]: https://bugs.mysql.com/bug.php?id=116463
[P1]: https://www.percona.com/blog/tuning-mysql-innodb-flushing-for-a-write-intensive-workload/
[P2]: https://www.percona.com/blog/give-love-to-your-ssds-reduce-innodb_io_capacity_max/
[P3]: https://www.percona.com/blog/linux-os-tuning-for-mysql-database-performance/
[P4]: https://www.percona.com/blog/mysql-101-linux-tuning-for-mysql/
[P5]: https://www.percona.com/blog/dont-start-using-your-mysql-server-until-youve-configured-your-os/
[P6]: https://www.percona.com/blog/how-to-calculate-a-good-mysql-redo-log-size-in-mysql-8/
[P7]: https://www.percona.com/blog/how-binary-logs-affect-mysql-8-0-performance/
[P8]: https://www.percona.com/blog/how-binary-logs-and-filesystems-affect-mysql-performance/
[P9]: https://www.percona.com/blog/percona-server-for-mysql-performance-improvements-august-2024/
[P10]: https://www.percona.com/blog/is-thread-pool-plugin-the-right-choice-for-your-workload/
[P11]: https://www.percona.com/blog/2026-mysql-ecosystem-performance-benchmark-report/
[P12]: https://www.percona.com/blog/surprise-with-innodb_doublewrite_pages-in-mysql-8-0-20/
[P13]: https://docs.percona.com/percona-server/8.4/xtradb-performance-improvements.html
[P14]: https://www.percona.com/blog/percona-server-5-7-multi-threaded-lru-flushing/
[P15]: https://docs.percona.com/percona-server/8.4/8.4-defaults-and-tuning.html
[P16]: https://www.percona.com/blog/impact-of-the-sort-buffer-size-in-mysql/
[P17]: https://www.percona.com/blog/open-source-databases-big-machines-read-committed-scale-144-cores/
[P18]: https://www.percona.com/blog/small-innodb_page_size-performance-boost-ssd/
[P19]: https://www.percona.com/blog/watch-out-for-disk-i-o-performance-issues-when-running-ext4/
[P20]: https://www.percona.com/blog/ext4-vs-xfs-on-ssd/
[P21]: https://www.percona.com/blog/performance-schema-overhead/
[P22]: https://www.percona.com/blog/how-many-innodb_buffer_pool_instances-do-you-need-in-mysql-8/
[P23]: https://www.percona.com/blog/part-two-how-many-innodb_buffer_pool_instances-do-you-need-in-mysql-8-with-a-cpu-bound-workload/
[P24]: https://www.percona.com/blog/enabling-and-disabling-jemalloc-on-percona-server/
[P25]: https://www.percona.com/blog/swap-insanity-tome-iii-the-write-cache/
[P26]: https://www.percona.com/blog/mysql-9-7-0-pgo-benchmark-analysis/
[P27]: https://forums.percona.com/t/enormous-increase-in-number-of-redo-writes-in-8-0-with-innodb-log-writer-threads-and-slow-transactions-processing-without-it/11491
[P28]: https://forums.percona.com/t/mysql-8-0-slow-temp-table-populate/22962
[O1]: https://lefred.be/content/mysql-8-4-lts-new-production-ready-defaults-for-innodb/
[O2]: https://dev.mysql.com/doc/refman/8.4/en/innodb-dedicated-server.html
[O3]: https://lefred.be/wp-content/uploads/2025/03/MySQL_Perf-preFOSDEM2025-dim.pdf
[O4]: https://dev.mysql.com/blog-archive/mysql-8-0-new-lock-free-scalable-wal-design/
[O5]: https://dev.mysql.com/blog-archive/performance-schema-great-power-comes-without-great-cost/
[O6]: https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html
[O7]: https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html
[O8]: https://www.hammerdb.com/blog/?p=378
[O9]: https://bugs.mysql.com/bug.php?id=86246
[O10]: https://bugs.mysql.com/bug.php?id=87933
[O11]: https://lefred.be/content/a-response-to-perconas-2026-mysql-ecosystem-benchmark-useful-data-but-not-a-realistic-mariadb-comparison/
[M1]: https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-flush-method
[M2]: https://jira.mariadb.org/browse/MDEV-30136
[R1]: https://releem.com/blog/innodb-performance-tuning
[R2]: https://releem.com/docs/mysql-performance-tuning/sort_buffer_size
