# Mechanism micro-benchmarks

Status: first three suites built (adaptive hash index, change buffering, buffer pool size), 2026-11-02.
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

## Results

| Date | Case | Verdict | Measured |
|---|---|---|---|
| 2026-11-02 | ahi/positive | PASS | tps +5.0%; B-tree CPU 12.9% with AHI off; 20.7 hash searches per B-tree search with it on |
| 2026-11-02 | ahi/negative | PASS | tps -6.3% with AHI on (index updates) |
| 2026-11-02 | bp/positive | PASS | 2 -> 16 GB: tps +117%, page reads per trx -74%; at 2 GB 16 threads waiting on page reads |
| 2026-11-02 | bp/negative | PASS | 6 -> 20 GB: tps +0.5%, 100% hits |
| 2026-11-02 | cb/negative | PASS | tps -0.9% |
| 2026-11-02 | cb/positive | FAIL (case design) | 2 GB pool held most index pages (99.89% hits) and inserts were write-bound: reads per trx -31% but tps +1%. Redesigned (512 MB pool, trx_commit=2) and rerun. |

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
