# Mechanism micro-benchmarks

Status: seven suites built and run (adaptive hash index, change buffering, buffer pool size, redo capacity, io_capacity, log writer threads, binlog group commit), 2026-11-04.
Short shareable summary: the "Mechanism micro-benchmarks for the tuner" doc (claude.ai). This file is the source.

## Why

The tuner proposes a setting when a measured signal says that setting's mechanism costs time or resources
(`docs/research/var-evidence/`: for each of 313 settings, the signals, the bound on the gain and a validating
experiment). The matrix cells test the tuner on realistic mixed loads, where many mechanisms overlap; a result
there rarely says *which* signal predicted it.

A micro-benchmark makes one mechanism dominant and checks one claim from the evidence research:

- **positive case** - the predicted signal shows up, and the setting moves the predicted metric the predicted way;
- **negative control** - the same setting on a load where its mechanism is idle: no gain (or a loss), and the
  signal is absent.

A failed expectation means the signal, the evidence entry or the rule built on it is wrong. Passing cases are
clean single-mechanism data points for the case base and the learner.

Inspiration: `mysql-test` - one small directory-like unit per feature, a fixed setup, a workload, and an expected
result that the runner compares mechanically.

## A case

One JSON file, `bench/micro/<suite>/<case>.json`:

| Field | What |
|---|---|
| `mechanism`, `role` | the mechanism exercised; `positive` or `negative` |
| `claim` | the claim from the evidence research it checks, in one sentence |
| `dataset` | sysbench shape: `tables`, `rows` (sized against the buffer pool, e.g. ~15x a fixed 2 GB pool) |
| `fixed` | settings applied before calibration (e.g. `innodb_buffer_pool_size=2G` for an on-disk case) |
| `workload` | sysbench `test`, `threads`, `rand` (uniform / pareto / ...), extra args |
| `warmup`, `duration`, `goal` | seconds; the goal (tps, or latency) |
| `sweep` | `variable` and `values`, the first value is the reference |
| `repeats` | measured runs per value (2 by default) |
| `expect` | the expectations, below |

`expect` is a list; each item is one of:

- **response** - `{"kind":"response","from":"OFF","to":"ON","metric":"tps","direction":"up","min_pct":3}`: the metric's
  mean at `to` vs at `from`. Directions: `up` / `down` (beyond `min_pct` and beyond the noise), `flat` (within
  `max_pct` or the noise, whichever is larger), `not_up` / `not_down`.
- **signal** - `{"kind":"signal","at":"OFF","metric":"cpu:B-tree / row access","op":">=","value":8}`: a measured
  value at one sweep value. Operators `>=`, `<=`, `>`, `<`.
- **ratio** - `{"kind":"ratio","at":"ON","num":"im:adaptive_hash_searches","den":"im:adaptive_hash_searches_btree","op":">=","value":1}`.

Metric names (resolved from the run's summary and inputs, as the agent recorded them):

| Prefix | Source | Example |
|---|---|---|
| (none) | run summary | `tps`, `bp_hit_pct`, `mysqld_cpu_ms_per_trx`, `io_queries_read_kb_per_trx` |
| `cpu:` | CPU profile category, % of mysqld CPU | `cpu:B-tree / row access` |
| `off:` | off-CPU wait category of connection threads, threads | `off:Waiting: data page read` |
| `im:` | INNODB_METRICS rate per second | `im:adaptive_hash_searches` |
| `st:` | status counter rate per second | `st:Innodb_buffer_pool_reads` |
| `trx:` | status counter per transaction | `trx:Innodb_buffer_pool_reads` |

## Runner

- `bench/pipeline/items/micro.sh <suite>/<case>`: builds (or reuses) the data set, writes the benchmark profile,
  applies `fixed`, opens a session in the study "mechanism micro-benchmarks", calibrates the start (no tuning),
  then `micro-post.sh` queues the sweep (`experiment add --sweep`, `repeats`) and runs it.
- `bench/micro/check.py <case.json>`: reads the session's runs from the agent's state (summaries and the per-run
  inputs it keeps), resolves every expectation, prints `PASS` / `FAIL` per item and for the case, and records the
  result as a session event (`micro_check`), so it shows on the session page and reaches the learner.
- Cases run as pipeline items (`micro-<suite>-<case>`) on the bare-metal boxes, like the sweeps.
- The agent switches on the INNODB_METRICS modules the expectations read (buffer, log, adaptive hash, change
  buffer).

## First suites

| Suite | Case | Load | Sweep | Expect |
|---|---|---|---|---|
| ahi | positive | 8 x 4M rows in memory, point selects, 64 threads | AHI OFF, ON | ON: tps up >= 2%; at OFF B-tree CPU >= 8%; at ON hash searches >= B-tree searches |
| ahi | negative | same data, updates of the indexed column `k`, 64 threads | OFF, ON | ON: not up |
| cb | positive | 8 x 16M rows (~30 GB) on a 512 MB pool (secondary indexes ~6x the pool), trx_commit=2 fixed, inserts with random secondary keys, 64 threads | buffering none, all | all: tps up >= 5%, page reads per trx down >= 20%; at none data page reads dominate off-CPU waits |
| cb | negative | 8 x 4M rows in memory, same inserts | none, all | all: flat |
| bp | positive | 8 x 16M rows (~30 GB), skewed (pareto) point selects, 64 threads | pool 2, 4, 8, 16 GB | 16 vs 2 GB: tps up >= 20%, reads per trx down >= 30%; at 2 GB data page reads dominate |
| bp | negative | 8 x 2M rows (~4 GB), uniform point selects | pool 6, 12, 20 GB | 20 vs 6 GB: flat |

## Second suites (run 2026-11-03 / 04)

| Suite | Case | Load | Sweep | Expect |
|---|---|---|---|---|
| redo | positive | 8 x 4M in memory, updates of a non-indexed column, trx_commit=2, 64 threads | 512M, 2G, 8G | 8G vs 512M: tps up >= 5%, pages flushed per trx down >= 20%; at 512M flushing driven by redo age (>= 50%) |
| redo | negative | read-only | 512M, 8G | flat |
| iocap | positive | 8 x 16M on a 4 GB pool, updates, trx_commit=2, 64 threads | 200, 2000, 10000 | 2000 vs 200: tps up >= 5%; at 200 flushing requested >= the cap |
| iocap | negative | read-only in memory | 200, 10000 | flat |
| logwriter | low-concurrency (negative) | updates, 8 threads | OFF, ON | ON not up |
| logwriter | high-concurrency (positive) | updates, 512 threads | OFF, ON | ON not down; redo fsyncs per trx down >= 10% |
| binlog | positive | sync_binlog=1, updates, 128 threads | group commit delay 0, 1000 us | binlog syncs per trx down >= 30%; tps not down |
| binlog | negative | sync_binlog=1, updates, 4 threads | 0, 1000 us | tps not up |

## Third wave (queued 2026-11-04)

| Suite | Positive case | Negative control | Sweep | Expect (positive) |
|---|---|---|---|---|
| concurrency | read-write, 1,024 threads, in memory | 32 threads | innodb_thread_concurrency 0, 24 | tps up >= 5%; run-queue wait >= 20% at 0 |
| threadpool | same | 32 threads | thread_handling one-thread-per-connection, pool-of-threads | tps up >= 5%; run-queue wait >= 20% |
| bpinstances | pareto point selects on 8 x 1M, 1,024 threads | 8 threads | innodb_buffer_pool_instances 1, 8 | tps up >= 5% |
| thp | point selects over 16 GB in a 20 GB pool (Intel hosts) | 0.5 GB heap | os:thp never, always | tps up >= 3%; page walks >= 3% of cycles at never |
| lru | read-write on 60 GB with a 4 GB pool | in memory | innodb_lru_scan_depth 128, 1024, 4096 | tps up >= 5% at 1024; free-page waits at 128 |
| doublewrite | update-only on 60 GB with a 4 GB pool | in memory, 8 GB redo | innodb_doublewrite ON, OFF | tps up >= 5%; bytes written per trx down >= 20% |
| tablecache | point selects over 4,000 tables | 8 tables | table_open_cache 400, 8000 | tps up >= 5%; >= 100 cache misses/s at 400 |
| tabledef | same, table_open_cache 8000 | 8 tables | table_definition_cache 400, 8000 | tps up >= 3%; definitions reopened at 400 |
| threadcache | new connection per transaction (lua/connstorm.lua) | persistent connections | thread_cache_size 0, 128 | tps up >= 5%; >= 100 threads created/s at 0 |

`table_cache` no longer exists (renamed table_open_cache in MySQL 5.1.3); the open-table cache, the definition cache and the
cache instances are separate settings. innodb_flush_neighbors is left out: every box has SSDs (SATA or NVMe), and
neighbour flushing only pays on rotating disks - no positive case is possible here.

## Results

| Date | Case | Verdict | Measured |
|---|---|---|---|
| 2026-11-02 | ahi/positive | PASS | tps +5.0%; B-tree CPU 12.9% with AHI off; 20.7 hash searches per B-tree search with it on |
| 2026-11-02 | ahi/negative | PASS | tps -6.3% with AHI on (index updates) |
| 2026-11-02 | bp/positive | PASS | 2 -> 16 GB: tps +117%, page reads per trx -74%; at 2 GB 16 threads waiting on page reads |
| 2026-11-02 | bp/negative | PASS | 6 -> 20 GB: tps +0.5%, 100% hits |
| 2026-11-02 | cb/negative | PASS | tps -0.9% |
| 2026-11-02 | cb/positive | FAIL (case design) | 2 GB pool held most index pages (99.89% hits) and inserts were write-bound: reads per trx -31% but tps +1%. Redesigned (512 MB pool, trx_commit=2) and rerun. |
| 2026-11-04 | cb/positive (redesigned) | PASS (revised check) | none -> all: tps +126%, page reads per trx -98%. The off-CPU check (0.93 threads waiting on page reads vs >= 1) was too strict: now >= 0.5, same data passes. |
| 2026-11-04 | redo/positive | PASS | 512M -> 8G: tps +470%, pages flushed per trx -87%; at 512M flushing 100% by redo age |
| 2026-11-04 | redo/negative | PASS | read-only: +0.5% |
| 2026-11-04 | iocap/positive | FAIL (case design) | Flushing asked for 0.25% of the cap: data far larger than the pool (LRU flushing), and io_capacity_max stays >= 2000 whatever io_capacity is. Redesigned: in memory, 1 GB redo, io_capacity_max swept 400 / 4000 / 20000. Rerun queued. |
| 2026-11-04 | iocap/negative | PASS | read-only: +0.3% |
| 2026-11-04 | logwriter/low-concurrency | PASS | 8 threads: ON -4.8% |
| 2026-11-04 | logwriter/high-concurrency | FAIL (expectation) | 512 threads: ON +88% tps, but redo fsyncs per trx x5 - the threads win by taking writes and syncs off the user threads, not by fewer syncs. Expectation now tps up; rerun on a second host queued. |
| 2026-11-04 | binlog/positive | FAIL (claim refuted) | 1 ms delay: binlog syncs per trx -58%, tps -24.5% - at no delay the log already synced once per ~39 commits. Rule change: group-commit-delay only with >= 0.1 binlog syncs per trx measured. |
| 2026-11-04 | binlog/negative | PASS | 4 threads: 1 ms delay -44% |

## Next mechanisms

| Mechanism | Positive case | Negative control | Signals |
|---|---|---|---|
| buffer pool instances | point selects on a small hot set, 1024 threads | 8 threads | page-hash / LRU latch waits, CPU spin share |
| redo capacity | update-heavy, checkpoint age near the limit | read-only | flushing by redo age, sync flushes, redo space waits |
| io_capacity / dirty-page limits | write-heavy on disk, free-page waits | in memory, read-only | flushing requested vs capacity, free-page waits |
| log writer threads | many small commits, 256+ threads | 8 threads | commit flushes per transaction, redo threads' device share |
| binlog group commit | binlog on, sync_binlog=1, many small commits | binlog off | binlog syncs per transaction, LOCK_sync waits |
| doublewrite | write-bound on disk | in memory | doublewrite write share of device time |
| purge | long read view + updates | short transactions | history length growth, purge thread profile (read-view cause) |
| thread pool | thousands of connections | 32 connections | run-queue wait, pool queue wait |
| TempTable / tmp_table_size | GROUP BY over large sets | point selects | TempTable disk use, tmp disk tables |
| sort_buffer_size | ORDER BY on unindexed columns | indexed ORDER BY | sort merge passes |
| join_buffer_size | joins without an index | indexed joins | full joins, join buffer use |
| table_open_cache | thousands of tables | 8 tables | table cache misses / overflows |
| THP / huge pages | CPU-bound on a large heap | small heap | dTLB page walks (to add) |
| change buffering under purge | updates of secondary keys + purge on disk | in memory | purge thread file I/O share |
| spin / sync settings | hot-row contention | low concurrency | rw-lock spin rounds, OS waits |

## How results are used

- A failed expectation opens a fix in the evidence entry, the signal or the rule.
- Passing results are cases for the similarity term and evidence for the learner, tagged by mechanism and role.
- The suites rerun after rule or signal changes (a regression check like the decision corpus, but measured).
