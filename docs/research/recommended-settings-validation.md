# Recommended MySQL/InnoDB Settings: What Still Needs Validation on PS 9.7 / 8.4

*2026-10-19. This is a condensed refresh of [`mysql-tuning-ideas.md`](mysql-tuning-ideas.md) (96 ideas). It covers only the settings that **Percona** or **Mark Callaghan** recommend changing from the defaults. Each one is checked against the live 9.7.2 defaults and our tuner results.*

## Sources and method

**Defaults.** Read from PS 9.7.2-2 on `atvbm4c`: 8 vCPU, 32 GB, binlog OFF. The source was `performance_schema.variables_info` (source = COMPILED), read-only. `mysqld --verbose --help` is misleading for auto-sized variables. For example, it prints `innodb_log_writer_threads=OFF`, `purge_threads=4` and `flush_method=fsync`, but the running server reports ON, 1 and O_DIRECT. Auto-sized defaults depend on vCPU count and binlog state, as the table notes. Defaults for 8.4 come from the 8.4.11 run snapshots in our DB.

**Mark's configs.** Sparse clone of `mdcallag/mytools`, `bench/conf/arc/oct24/` (HEAD 2026-06-17). The newest MySQL files are:
- `c8r32/my97/etc/my.cnf.cz13a_c8r32`: MySQL 9.7, 8 cores / 32 GB. Last content change 2026-03-24.
- `c24r64/my95/etc/my.cnf.cz13a_c24r64`: 2026-03-24.
- `c32r128/my9500/etc/my.cnf.cz13a_c32r128`: renamed 2026-04-17.
- `*_hg` variants: cz13a plus `hypergraph_optimizer=on`, 2026-04-17.
- `my.cnf.cz12a_*`: the same base for 8.0/8.4. `cz12b` is cz12a with `O_DIRECT` and GTID lines removed.

The c24/c32 files differ from c8 only in sizing: BP, redo, purge/cleaner/IO threads 8, BP instances 8. Only c8 has `log_writer_threads=OFF`.

**Mark's posts used:**
- [MC1] [MySQL 9.7.0 vs sysbench, small server](https://smalldatum.blogspot.com/2026/04/mysql-970-vs-sysbench-on-small-server.html), 2026-04-10
- [MC2] [IO-bound Insert Benchmark, 24/32-core](https://smalldatum.blogspot.com/2026/01/io-bound-insert-benchmark-vs-mysql-on.html), 2026-01-21
- [MC3] [Regressions in 8.4 and 9.x (GTID)](https://smalldatum.blogspot.com/2025/12/performance-regressions-in-mysql-84-and.html), 2025-12-17
- [MC4] [HammerDB tproc-c, small server](https://smalldatum.blogspot.com/2026/02/hammerdb-tproc-c-on-small-server.html), 2026-02-14
- [MC5] [CPU-bound sysbench, large server](https://smalldatum.blogspot.com/2026/04/cpu-bound-sysbench-on-large-server.html), 2026-04-03
- [MC6] [Battle of mallocators](https://smalldatum.blogspot.com/2025/04/battle-of-mallocators.html), 2025-04-11
- [MC7] [log_writer_threads](https://smalldatum.blogspot.com/2024/01/innodblogwriterthreads-and-insert.html), 2024-01-03
- [MC8] [bug 116463 spin_wait_delay](https://bugs.mysql.com/bug.php?id=116463), 2024-10-23
- 2023 tuning series: see the old survey [S2]-[S5]

**Percona sources:**
- [PD1] [PS 8.4 defaults & tuning](https://docs.percona.com/percona-server/8.4/8.4-defaults-and-tuning.html), updated 2026-09-25
- [PD2] [PS 9.7 migration overview](https://docs.percona.com/percona-server/9.7/whats-new-mysql-9.html), 2026-07-21
- [PB1] [2026 MySQL ecosystem benchmark](https://www.percona.com/blog/2026-mysql-ecosystem-performance-benchmark-report/), 2026-03-26. This is Percona's newest full config: BP about 75% of the tier, BP instances 8 when BP ≥ 8 GB, `io_capacity` 2500/5000, redo 4G, `change_buffering=none`, O_DIRECT, log buffer 64M, `table_open_cache` 200000, `max_connections` 4096, `sync_binlog=1`.
- [PB2] [Redo log size](https://www.percona.com/blog/how-to-calculate-a-good-mysql-redo-log-size-in-mysql-8/), 2023-03-06
- [PB3] [Reduce io_capacity_max](https://www.percona.com/blog/give-love-to-your-ssds-reduce-innodb_io_capacity_max/), 2019-12-18
- [PB4] [Flushing tuning](https://www.percona.com/blog/tuning-mysql-innodb-flushing-for-a-write-intensive-workload/), 2020-05-14
- [PB5] [BP instances](https://www.percona.com/blog/how-many-innodb_buffer_pool_instances-do-you-need-in-mysql-8/), 2020-08-13
- [PB6] [Thread pool](https://www.percona.com/blog/is-thread-pool-plugin-the-right-choice-for-your-workload/), 2024-01-02
- [PB7] [doublewrite_pages](https://www.percona.com/blog/surprise-with-innodb_doublewrite_pages-in-mysql-8-0-20/), 2025-11-05
- [PB8] [Configure your OS](https://www.percona.com/blog/dont-start-using-your-mysql-server-until-youve-configured-your-os/), 2023-09-28
- [PB9] [jemalloc](https://www.percona.com/blog/enabling-and-disabling-jemalloc-on-percona-server/), 2017-01-03
- [PB10] [PS 8.4 binlog improvements](https://www.percona.com/blog/percona-server-for-mysql-performance-improvements-august-2024/), 2024-08-26

**Oracle source.** [O1] [MySQL 9.7 InnoDB parameters](https://dev.mysql.com/doc/refman/9.7/en/innodb-parameters.html), which gives the `log_writer_threads` rule.

**Our evidence.** Taken from `~/aituner-review/tuner-b1.db`, 771 runs and 74 sessions.
- **Runs counted:** single-experiment runs with verdict improved (+), efficiency-gain (e+), neutral (=), efficiency-loss (e−) or regressed (−). Repeats, references, baselines, reverts and the one `unreliable` run are excluded. No run is `superseded`.
- **Attribution:** each run is attributed to the experiment named in its label. Revert/rollback changes in the same run are ignored.
- **Δ:** `goal_delta_pct` versus the parent, positive = better (TPS, or latency for TPC-H).
- **vCPU split:** `1-2v` / `4-8v` / `12-16v`.
- **"post-10-19":** sessions created on or after 2026-10-19, which are free of the merged-revert parent bug.
- **Caveat:** some earlier sysbench sessions did not reset data, so treat pre-10-19 medians as directional.
- **Servers:** PS 8.4.11 sessions (mostly 2 vCPU, binlog ON) and PS 9.7.2 sessions (2-16 vCPU, binlog OFF, `gtid_mode=ON`).

**Status legend:**
- **validated**: helps measurably in our data.
- **no measurable effect**: tried, n ≥ 3, within noise.
- **harmful**: mostly regressed.
- **untested**: n ≤ 1 or never tried in isolation.
- **default**: the recommended value already is the 9.7 default, so it needs no validation.

## Consolidated table

### Redo log and commit path

| Setting | 9.7 default | Percona recommends | Mark uses (newest config) | Became default in 8.4/9.x? | Our evidence (n, verdicts, median Δ) | Status | Validation proposal |
|---|---|---|---|---|---|---|---|
| `innodb_redo_log_capacity` | 100M | 4G [PB1]; size to about 1 h of redo [PB2] | 30G c8r32, 50G c24r64, 100G c32r128, about the BP size (cz13a) | No (only via `dedicated_server`) | The tuner baseline already uses 25% of RAM (1-16G); that bundle is +72% vs defaults. Growth experiments: n=12, +2/e+1/=7/−2, med **+0.7**. Results: 12v AMD 8→32G **+23%**; 12v E-2286G 8→32G **−6.7%**; 2v 1→2G neutral. | validated vs 100M; larger sizes inconclusive | 4G vs 25% RAM vs about the BP size, on write-heavy IO-bound loads (HammerDB 800W/2000W, sysbench 75 GB rw, 64 thr), 8 and 16 vCPU. Track checkpoint age % and page writes/trx. Repeat 3× (noise protocol). |
| `innodb_log_writer_threads` | **Auto [O1]:** binlog ON and > 32 CPUs → ON; binlog OFF and > 4 CPUs → ON; otherwise OFF. 8.4: ON. | No recommendation | OFF (c8r32 only; it is the 9.7 default there because binlog is ON) | Partly. With binlog ON and ≤ 32 vCPU, OFF is the 9.7 default. | n=30, +19/e+7/=3/−1, med **+6.6**. By size: 1-2v n16 +8.0 (8.4, binlog ON); 4-8v n10 +5.3 (9.7 binlog OFF: +3 to **+36%** on CH-bench 500W, +27% on 50W); 12v about 0 (e+); **16v −4.4%** (HammerDB 2000W). Post-10-19: n15, +5.5. | **validated**. The 9.7 binlog-OFF rule (ON when > 4 CPUs) is wrong at 6-8 vCPU. | Find the crossover: ON vs OFF at 8/12/16/32 vCPU, binlog OFF, low (16 thr) and high (256 thr) concurrency. Also check binlog ON at 8 vCPU to confirm the OFF default is fine. |
| `innodb_flush_log_at_trx_commit` | 1 | 1 (2 on replicas only) | **2** (all configs) | No | n=10, +9/=1, med **+16.3**. 2v +10.4, 4-8v +18.6, 16v +30.3. | validated (durability trade-off) | Done. Keep it gated by policy. |
| `sync_binlog` | 1 | 1 [PB1] | **0** | No | Not tested (9.7 sessions run binlog OFF). `disable_log_bin`: n=4, +3/=1, med +11.5 (2v, 8.4). | untested | Binlog ON, `trx_commit` = 1 and 2: compare `sync_binlog` 1 vs 1000 vs 0 on sysbench write-only and HammerDB 80W, 8 vCPU. Watch for stalls at binlog rotation with 0. |
| `gtid_mode` / `enforce_gtid_consistency` | **ON / ON** (new in 9.x) | OFF if GTID is not needed [PD2] | **OFF / OFF** | It is the reverse: ON became the default | Not tested. All 9.7 runs had GTID ON but binlog OFF, so GTID was mostly inert. | untested | Binlog ON: GTID ON vs OFF on write-heavy sysbench (update-index, insert) at 8 and 16 vCPU. Mark measured −17% throughput, +13% CPU/op and +24% KB written/commit with GTID on 48 cores [MC3]. |
| `max_binlog_size` | 1G | — | 256M | No | Not tested | untested (low) | Only together with `sync_binlog=0`: look for rotation stalls. |
| `binlog_transaction_dependency_history_size` | 10000000 (live 9.7.2; 8.4 = 25000) | PS hash map [PB10] | — | Changed in 9.x | Not tested | untested (low) | Binlog ON, CPU-bound write: 25000 vs 10M. |
| `innodb_log_buffer_size` | 64M | 64M [PB1] | 64M | **Yes (8.4)** | — | default | None |
| `innodb_use_fdatasync` | ON | — | ON | **Yes (8.4)** | — | default | None |

### Flushing and I/O

| Setting | 9.7 default | Percona recommends | Mark uses (newest config) | Became default in 8.4/9.x? | Our evidence (n, verdicts, median Δ) | Status | Validation proposal |
|---|---|---|---|---|---|---|---|
| `innodb_io_capacity` / `_max` | 10000 / auto (2×) | **2500 / 5000** [PB1]; lower `_max` [PB3] | 10000 / 20000 (equal to the default) | Yes (8.4) for Mark's value | The tuner baseline uses 2000/4000 (inside the baseline bundle, never isolated). Sweeps over 1000-4000: n=15, +1/=9/e−3/−2, med **−0.2**. Post-10-19: n6, −0.4. | no measurable effect within 1000-4000. **2000 vs 10000 is untested.** | 2000 vs 10000/20000 on IO-bound write-heavy loads (sysbench 75 GB, HammerDB 800W), 8 vCPU Linode and bare-metal NVMe. Metrics: TPS, p99, KB written/trx (the write-combining claim). |
| `innodb_max_dirty_pages_pct_lwm` | 10 | 0 [PB4] (2020) | **80** (with `max_dirty_pages_pct` 90, which is the default) | No | n=3 (0/30/80), one 8v session, med −0.4 | no measurable effect (thin) | 10 vs 80 on IO-bound write-heavy loads. Measure KB written/trx and read p99. |
| `innodb_flush_method` | O_DIRECT | O_DIRECT [PB1] (default) | **O_DIRECT_NO_FSYNC** (cz12b/cz13 variants use O_DIRECT) | O_DIRECT yes (8.4) | NO_FSYNC: n=5, =5, med **−0.6** (8v Linode) | no measurable effect | Only on bare-metal NVMe with heavy writes, where data-file fsyncs are visible. |
| `innodb_idle_flush_pct` | 100 | — | 1 | No | Not tested | untested (low) | Only matters for bursty loads with idle gaps. Skip unless SSD wear is a goal. |
| `innodb_page_cleaners` | = BP instances (8 at 8 BP inst.) | (= instances) | = BP instances (4 / 8) | **Yes (8.4)** | — | default | None |
| `innodb_read_io_threads` / `write_io_threads` | read = vCPU/2 (4 at 8v); write 4 | — | 4/4 (c8), 8/8 (c24, c32) | read: yes (8.4) | The tuner baseline sets 4/4 at ≤ 8 vCPU and vCPU/2 above (never isolated) | untested (low) | 16 vCPU IO-bound: default vs 8/8. Only if `Innodb_data_pending_*` > 0. |
| `innodb_flush_neighbors` | 0 | 0 | 0 | Yes (8.0) | — | default | None |
| `innodb_doublewrite_pages` | 128 | 128 [PB7] | 128 | **Yes (8.4)** | — | default | None |
| `innodb_lru_scan_depth` | 1024 | formula [PB4] | default | — | n=6, e+1/=4/e−1, med −1.4 | no measurable effect | None, unless `Innodb_buffer_pool_wait_free` > 0. |
| `innodb_adaptive_flushing_lwm`, `innodb_flush_sync`, PS `innodb_cleaner_lsn_age_factor` / `empty_free_list_algorithm` | 10, ON, high_checkpoint, legacy | 2020 flushing advice [PB4] | defaults | — | Not tested | untested (low) | Only if checkpoint age oscillates after the redo work. |

### Buffer pool and memory

| Setting | 9.7 default | Percona recommends | Mark uses (newest config) | Became default in 8.4/9.x? | Our evidence (n, verdicts, median Δ) | Status | Validation proposal |
|---|---|---|---|---|---|---|---|
| `innodb_buffer_pool_size` | 128M | about 75% RAM [PB1] | 23G/32G, 45G/64G, 100G/128G (72-78%) | No (only via `dedicated_server`) | Baseline bundle (mainly the BP): n=47, +45/=2, med **+72%**. Fine-tuning ±: n=14, med +0.8. | **validated** | Done |
| `innodb_dedicated_server` | OFF | Described, not recommended [PD1] | **ON** (but BP and redo are set explicitly, so it is a no-op) | No | Not tested | untested | Use it as the reference in the whole-config comparison: plain 9.7 + `dedicated_server=ON`. |
| `innodb_buffer_pool_instances` | min(BP/chunk/2, vCPU/4) → 2 at 8v | **8** if BP ≥ 8G [PB1]; up to 64 on 80 vCPU [PB5] | **4** (c8), **8** (c24, c32) | Auto since 8.4 | The tuner baseline uses 8 (1 per GiB, max 8), never isolated | untested | 8 and 16 vCPU, in-memory sysbench at 64 and 1024 thr plus HammerDB 80W: auto (2/4) vs 4 vs 8 vs 16. Look at BP mutex waits. |
| `innodb_change_buffering` | **all** (live 9.7.2, PS and Community; 8.4.11 = none) | **none** [PB1] | **all** | **Reverted in 9.x.** It was none in 8.4. Mark's value is the 9.7 default; Percona's is not. | Only one 8.0 bundle run (+7.9, not attributable) | **untested** | IO-bound inserts with secondary indexes (sysbench 75 GB `oltp_insert` + `update_index`; HammerDB 800W): all vs none on 9.7 and 8.4. Mark: none → **0.48× throughput** on l.i1, many more read/write IOs [MC2]. Metrics: TPS, reads/trx, KB written/trx. |
| `innodb_change_buffer_max_size` | 5 | — | 25 | No | Not tested | untested | Only after `all` wins: 5 vs 25 on the same load. |
| `innodb_adaptive_hash_index` | OFF | OFF (default) | OFF | **Yes (8.4)** | ON: n=36, +7/=26/−3, med +1.1. TPC-H **+21%** (2v and 8v); OLTP −6 to +8. | default (agreed); ON helps analytics | Not a validation item. Tuner keeps the per-workload test. |
| `innodb_max_purge_lag` / `_delay` | 0 / 0 | — | **500000 / 1000000** | No | n=1 (200k), neutral −1.5 | untested | Long (≥ 1 h) write-heavy run with HLL growth (HammerDB 2000W, or sysbench with a long reader). Metrics: HLL, DB size growth, read p99, TPS. |
| `innodb_purge_threads` | 1 (≤ 16 vCPU), 4 above | — | **4** (c8), **8** (c24, c32) | 8.4 auto | n=7 (1→2/4/6/8), =6/−1, med +0.1 (8-12v) | no measurable effect at 8-12v | Combine with the purge-lag test above. Check that HLL stays flat with 1. |
| `innodb_numa_interleave` / PS `buffer_pool_populate` | ON / ON | ON (default) [PD1] | ON | **Yes (8.4)** | — | default | None (single-socket hosts) |
| `table_open_cache` (+ instances) | 4000 (16) | **200000** [PB1] | default | — | n=1 (8000), neutral +1.7 | untested (low) | Only with schemas of thousands of tables (`Table_open_cache_overflows` > 0). |

### Concurrency and threads

| Setting | 9.7 default | Percona recommends | Mark uses (newest config) | Became default in 8.4/9.x? | Our evidence (n, verdicts, median Δ) | Status | Validation proposal |
|---|---|---|---|---|---|---|---|
| `thread_handling=pool-of-threads` (PS) | one-thread-per-connection | For many short connections, `thread_pool_size` = vCPU [PB6] | not used | No | n=16, +1/e−1/**−14**, med **−9.8**. 4-8v −17.3, 2v −10.9, 12-16v −2.0; one +2.2 (12v AMD). Post-10-19: n7, −9.5. | **harmful** at ≤ 1024 threads | Only the case Percona targets: 2000-4000 connections with point selects or a connect storm. Compare TPS and p99 vs the default. |
| `innodb_thread_concurrency` | 0 | Mentioned as an option [PB6] | 0 | — | n=44 sweep runs, +6/=31/−6, med −0.4 | no measurable effect | None |
| `innodb_spin_wait_delay` | 6 | — | default (bug report proposes 5) [MC8] | — | Not tested (`sync_spin_loops=10`: n=36, med +0.8) | untested (low) | CPU-bound 8 vCPU at 1024 thr: 6 vs 5. Only if `ut_delay` is hot in perf. |
| `innodb_autoinc_lock_mode` | 2 | 2 | 2 | Yes (8.0) | n=2, neutral | default | None |

### Optimizer and SQL

| Setting | 9.7 default | Percona recommends | Mark uses (newest config) | Became default in 8.4/9.x? | Our evidence (n, verdicts, median Δ) | Status | Validation proposal |
|---|---|---|---|---|---|---|---|
| `optimizer_switch hypergraph_optimizer=on` | off | — | **on** in `cz13a_hg` / z13b (2026-04-17). Result: read-only-distinct **1.26×**, range=10000 **1.15×**, small write regressions [MC1]. | No | Not tested. (`subquery_to_derived=on` is our own idea: n=3, +3, TPC-H **+63%**.) | untested | TPC-H SF1/SF10 (per-query latency, plan diffs) and sysbench read-only ranges. Run it on top of `subquery_to_derived`. |
| `eq_range_index_dive_limit` | 200 | — | 10 | No | Not tested | untested (low) | Only for long IN-lists (sysbench `update-inlist`, `select_random_points`). |
| `character_set_server` | utf8mb4 | — | latin1 (about 5% CPU [MC5]) | No | Not tested | untested; benchmark-only (semantics trade-off) | Not a tuner knob; mention in the whole-config comparison. |
| `default_tmp_storage_engine` | InnoDB | — | MyISAM | No | Not tested | untested (low) | Skip |

### OS and malloc

| Setting | 9.7 default | Percona recommends | Mark uses (newest config) | Became default in 8.4/9.x? | Our evidence (n, verdicts, median Δ) | Status | Validation proposal |
|---|---|---|---|---|---|---|---|
| jemalloc (`malloc-lib`) | glibc | jemalloc [PB9] (with THP off) | **jemalloc** (all configs). His tests: +2.5% jemalloc, +3.5% tcmalloc for InnoDB [MC6]. | No | n=41 (jemalloc, tuned jemalloc, tcmalloc, mimalloc, glibc arena), +4/e+1/=33/e−2/−1, med **−0.3** | no measurable effect on throughput | Its real claim is RSS stability: run 24 h with RSS sampling, glibc vs jemalloc, on 8 vCPU. |
| THP `never` | Ubuntu: madvise | never [PB8] | — | — | Not isolated (in the tuner's practice rule) | untested (low) | Only on hosts where THP=always. |
| `vm.swappiness=1`, `noatime` | 60, relatime | yes [PB8] | — | — | swappiness n=32, noatime n=29: all neutral (med +0.3) | no measurable effect | Keep as hygiene; no more tests. |
| CPU governor `performance` | VM-dependent | yes [PB8] | — | — | n=4, neutral | no measurable effect | None |
| `vm.dirty_*` bytes | ratio | low dirty bytes (2013) | — | — | n=7, +2/=5, med +0.2 | no measurable effect | None |
| Filesystem | ext4 | XFS or ext4 | ext4 + discard | — | n=14: zfs **−17%**, btrfs **−13.5%**, xfs vs ext4 mixed | ext4/XFS are fine; zfs and btrfs are harmful | None |
| Large pages | off | — | not in my.cnf (2023 test: +6%) | — | Earlier attempt hit an OOM loop on PS 8.4 | untested | Low; only BP ≥ 16 GB and CPU-bound read loads. |

**Recommended by neither source but measured by us** (for context): `performance_schema=OFF` n=11, +9, med **+4.4** (observability trade-off); `innodb_sync_spin_loops=10` n=36, med +0.8; `innodb_doublewrite=OFF` n=9, med +0.2 (no effect); `binlog_group_commit_sync_delay` n=8, med +0.1.

## Validation plan

### Open items, ordered by expected value

1. **`innodb_log_writer_threads` crossover.** The 9.7 binlog-OFF default (ON when > 4 vCPU) loses 3-36% at 6-8 vCPU in our data, is neutral at 12 and wins at 16. Test ON vs OFF at 8/12/16/32 vCPU, binlog OFF, with 16 and 256 threads. Add a binlog-ON check at 8 vCPU. Cheap because the setting is dynamic.
2. **`innodb_change_buffering` all vs none** on 9.7 (default all again) and 8.4 (default none). Percona benchmarks with none; Mark measured 0.48× without it. Use an IO-bound insert/update-index workload with secondary indexes and measure reads/trx and KB written/trx.
3. **GTID OFF with binlog ON** on 9.7 (default ON). Mark measured −17% writes at 48 cores. Every 9.x user with binlog on is affected.
4. **`sync_binlog` 1 / 1000 / 0** with binlog ON. This is a durability trade-off: Mark uses 0, Percona uses 1.
5. **Redo size:** 4G (Percona) vs 25% RAM (tuner) vs about the BP size (Mark), on write-heavy IO-bound loads. Our data is split (+23% / −6.7%), so 3× repeats are needed.
6. **`innodb_io_capacity`:** 2000-2500 (tuner/Percona) vs 10000 (default, Mark), on IO-bound writes. Measure page writes/trx. It has never been isolated.
7. **`innodb_max_dirty_pages_pct_lwm`:** 10 vs 80 (Mark), same workload as items 5-6.
8. **`hypergraph_optimizer=on`** on analytics (TPC-H, range scans), on top of `subquery_to_derived`.
9. **`innodb_buffer_pool_instances`:** auto (2-4) vs 4 vs 8 at 8 and 16 vCPU, high concurrency.
10. **`innodb_max_purge_lag` 500k/1s + `purge_threads` 4** on a long write-heavy run. Watch HLL and DB growth, not only TPS.
11. **Thread pool**, only at 2000-4000 connections. It is harmful at ≤ 1024 in our data.
12. **jemalloc**, 24 h RSS test. Throughput is already shown neutral.
13. **Low value:** O_DIRECT_NO_FSYNC on NVMe bare metal, `eq_range_index_dive_limit=10`, `idle_flush_pct=1`, `max_binlog_size=256M`, `spin_wait_delay=5`, `table_open_cache` 200000, `change_buffer_max_size` 25.

### Whole-config comparison A: Mark's newest InnoDB config vs our tuner's starting config

- **Mark:** `cz13a_c8r32` (8 vCPU) and `cz13a_c24r64` (≥ 16 vCPU), with BP and redo scaled to host RAM.
- **Variants:**
  - **A1, as published:** `trx_commit=2`, `sync_binlog=0`, latin1, GTID OFF.
  - **A2, durability-matched:** `trx_commit=1`, `sync_binlog=1`, utf8mb4.
- **Tuner:** the `baseline.py` output plus the accepted practice bundle.
- **Main differences:**
  - `io_capacity`: 10000/20000 vs 2000/4000.
  - Redo: about the BP size vs 25% RAM.
  - `lwm`: 80 vs 10.
  - `idle_flush_pct`: 1 vs 100.
  - Purge: threads 4 plus lag 500k vs default.
  - Flush method: O_DIRECT_NO_FSYNC vs O_DIRECT.
  - `change_buffer_max_size`: 25 vs 5.
  - Malloc: jemalloc vs glibc.
  - GTID OFF.
- **Workloads:** battery O1/O2 (in-memory sysbench 4/64 thr), O4 (IO-bound sysbench 75 GB), O5/O6 (HammerDB 80W/800W) and an IO-bound insert run, on 8 vCPU Linode dedicated and 16 vCPU. Run each 3×.
- **Report:** TPS, p99, CPU ms/trx and KB written/trx.
- **Goal:** if A2 ≈ tuner, the tuner's start is as good as an expert's. If A2 > tuner, bisect over the differences above.

### Whole-config comparison B: Percona's recommended set vs ours

- **Percona set:** the [PB1] 2026 config (BP about 75%, instances 8, `io_capacity` 2500/5000, redo 4G, `change_buffering=none`, O_DIRECT, log buffer 64M, `table_open_cache` 200000, `max_connections` 4096, `sync_binlog=1`, `trx_commit=1`) plus the OS advice from [PB8]/[PB9] (jemalloc, THP never, swappiness 1, noatime).
- **Compare it against:**
  - the tuner's starting config (same durability);
  - a third reference: plain 9.7 defaults + `innodb_dedicated_server=ON`.
- **Expected differences:** small. They come from redo (4G vs 25% RAM), change buffering none vs all on 9.7, and BP instances.
- **Workloads:** same battery and repeat count as A. The IO-bound insert run is where `change_buffering=none` should show.
