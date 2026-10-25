# Advanced MySQL / MariaDB tuning levers the tuner does not use yet (2026-10-24)

**Starting point.**
- The rule engine has about 110 rules: buffer pool, redo, flushing, purge, spin, thread pool, AHI, binlog, group
  commit, performance_schema, NUMA, huge pages, allocators, I/O scheduler, swap, network, CPU governor, IRQ split,
  resource groups for InnoDB background and purge threads.
- The parameter catalog knows more variables than the rules use. The AI planner and LLM suggestions can propose a
  catalog variable, but no rule ever does.
- This review lists what is missing, in two groups: variables in the catalog without a rule, and whole classes the
  tuner does not model (settings applied after start, schema and statistics objects, hardware state).

**Priority** is the expected value for the workloads we run (OLTP at high concurrency, I/O-bound reads, commit-bound
writes, analytics) against the effort and risk.

## 1. Same class as resource groups: applied to the running server, not persisted by an option file

| Lever | What it does | Why it matters | Status | Priority |
|---|---|---|---|---|
| USER resource groups (MySQL 8.0+) | Lower the priority of, or bind, foreground thread classes (reporting, batch) with `SET RESOURCE GROUP` per connection or `/*+ RESOURCE_GROUP(g) */` hints | Mixed OLTP plus reporting: keeps p99 of the OLTP part flat. Needs the application or a proxy to tag sessions. | not modelled | advice, P3 |
| Buffer pool dump / load (`innodb_buffer_pool_dump_at_shutdown`, `_load_at_startup`, `_dump_pct`) | Restart with a warm buffer pool | Restart recovery time in production | not in rules | **Not for the tuner's own runs** (user, 2026-10-25): a restored pool starts each run from what the previous run cached, so runs start in conditions that depend on run order; every run builds its own cache. Production advice only, P3 |
| Histograms (`ANALYZE TABLE … UPDATE HISTOGRAM`; MariaDB engine-independent statistics, `histogram_type=JSON_HB`) | Better plans for skewed columns | Query-plan wins on analytics and skewed OLTP | `stats-histograms` rule exists for MySQL | extend to MariaDB, P2 |
| Pinning or renicing purge and page-cleaner threads on MariaDB (no resource groups) | taskset / renice by `THREAD_OS_ID` after each start | Same remedy as `rg:purge`, MariaDB edition | advice only | P2 (needs a re-apply hook like resgroups.ensure) |

## 2. Semantics and durability trade-offs: large levers, the policy decides

| Lever | Effect | Status | Priority |
|---|---|---|---|
| `transaction_isolation = READ-COMMITTED` | No gap or next-key locks on scans; read views per statement, so purge can advance sooner. Large on hot-row write contention and long history lists. Changes semantics (phantoms, non-repeatable reads). | recorded, not a rule | **P1** (trade-off class "semantics") |
| `innodb_deadlock_detect = OFF` (with a low `innodb_lock_wait_timeout`) | At hundreds of threads on hot rows, deadlock detection itself serialises; a known lever for very high concurrency | catalog only | **P1** (gate: row-lock wait time high, deadlocks rare) |
| `innodb_doublewrite = DETECT_ONLY` (MySQL 8.0.30+) | Writes doublewrite metadata, not page copies: most of the doublewrite-off gain, and torn pages are detected (not repaired) | trade-off defined, no rule | **P1**: a middle step before `doublewrite-off` |
| `binlog_order_commits = OFF` | Commits leave in parallel instead of binlog order: commit-bound with the binlog on | catalog only | P2 |
| `innodb_flush_log_at_timeout` (with `trx_commit=2`) | Flush interval: at most N s of commits lost on an OS crash | no rule | P3 |
| `binlog_transaction_compression = ON` | zstd-compressed binlog: less I/O and replication traffic, more CPU | no rule | P2 (I/O-bound with the binlog on) |
| `binlog_row_value_options = PARTIAL_JSON` | Smaller binlog for JSON updates | no rule | P3 |

## 3. Concurrency internals

| Lever | Status | Priority |
|---|---|---|
| Thread pool tuning, Percona / MariaDB (`thread_pool_size`, `thread_pool_oversubscribe`, `thread_pool_stall_limit`, `thread_pool_max_threads`, `thread_pool_high_prio_mode/tickets`). Today the rule only switches `thread_handling`; it lost 31 of 32 times, mostly at defaults sized for other loads. | on/off only | **P1**: sweep size and oversubscribe before calling the pool a loss |
| `innodb_thread_concurrency` with `innodb_concurrency_tickets` and `innodb_thread_sleep_delay` | concurrency sweep exists, companions missing | P2 |
| `innodb_sync_array_size`, `innodb_spin_wait_pause_multiplier` (8.0.16+) | not in rules (the spin delay sweep exists) | P2: spin-bound CPU profiles |
| `innodb_adaptive_hash_index_parts` | catalog only (static) | P2: AHI latch contention when AHI on wins |
| Redo spin (`innodb_log_spin_cpu_abs_lwm`, `_pct_hwm`, `innodb_log_wait_for_flush_spin_hwm`) | mechanism-tagged, no rule | P2: CPU-saturated commit path (where log writer threads OFF won) |

## 4. Memory, temporary tables, parallelism

| Lever | Status | Priority |
|---|---|---|
| `temptable_use_mmap` / `temptable_max_mmap`, `internal_tmp_mem_storage_engine` (TempTable vs MEMORY) | not in rules ("priors-analytics" pipeline item still TODO) | **P1** for analytics (TPC-H / CH) |
| `innodb_parallel_read_threads` (parallel clustered index scans: COUNT(*), CHECK TABLE) | catalog only | P2 analytics |
| `innodb_ddl_threads`, `innodb_ddl_buffer_size` | not modelled | P3 (DDL, not steady state) |
| MariaDB: `tmp_memory_table_size`, Aria page cache for internal temp tables (`aria_pagecache_buffer_size`) | not in rules | P2 MariaDB analytics |
| `global_connection_memory_limit` / `connection_memory_limit` (8.0.28+) | noted in memory-budget.md | P3 production guardrail |

## 5. I/O and flushing

| Lever | Status | Priority |
|---|---|---|
| `innodb_idle_flush_pct` | not in rules | P3 (idle periods only) |
| Undo: `innodb_undo_tablespaces`, `innodb_max_undo_log_size`, `innodb_undo_log_truncate` | not in rules | P2 with the stability-first work (long history lists grow undo) |
| `innodb_use_native_aio` (off on some filesystems and containers) | not checked | P2 check: diagnose when it is off |
| `innodb_fsync_threshold` (fsync in chunks while creating or extending files) | not in rules | P3 |
| Page / table compression (`ROW_FORMAT=COMPRESSED`, `COMPRESSION='lz4'`, MariaDB page compression) | schema-level, not modelled | P2 advice for I/O- or space-bound sets |
| MariaDB: `innodb_lru_flush_size`, `innodb_log_file_buffering`, `innodb_flush_method` defaults per version | not in rules | P2 (MariaDB) |

## 6. OS and hardware state (bare metal especially)

| Lever | Status | Priority |
|---|---|---|
| CPU idle states (C-states: `intel_idle.max_cstate`, `/dev/cpu_dma_latency`, tuned `latency-performance`) | governor rule exists, C-states not | **P1** for latency goals on bare metal (wake-up latency) |
| SMT on / off (`/sys/devices/system/cpu/smt/control`) | not modelled | P2 (measured trade-off; runtime switchable) |
| NVMe interrupt affinity / queue mapping | the IRQ split rule exists for NICs | P3 |
| cgroup v2 `cpu.weight` / `io.weight` between mysqld and a co-located client or backup | not modelled | P2 (co-located clients appear in many sessions) |
| Kernel scheduler tunables (EEVDF slice, migration cost) | EEVDF oversubscription rule exists | P3 |

## 7. MariaDB-specific (the engine mostly shares the MySQL rules today)

- **Optimizer cost model** (11.0+, `optimizer_*_cost` variables), `optimizer_use_condition_selectivity`,
  `join_cache_level`, `optimizer_switch` flags (rowid_filter, condition pushdown). P2.
- **Group commit for replication:** `binlog_commit_wait_count` / `binlog_commit_wait_usec`. P2 (commit-bound, binlog on).
- **Query cache:** still present; usually harmful under write concurrency; a check that it is off. P3.
- **`innodb_snapshot_isolation`** (11.6+): a correctness setting, report only.
- **Thread pool on by default on some builds:** detect and tune (section 3).

## Recommended next steps

1. **P1 rules**, each gated on measured evidence as the existing rules are, with the trade-off class where semantics
   change:
   - READ-COMMITTED;
   - deadlock detection off;
   - doublewrite DETECT_ONLY as the step before doublewrite-off;
   - thread pool parameter sweep;
   - TempTable mmap / internal temporary table engine for analytics;
   - C-states for latency goals.
2. **Shorter warm-ups without changing run conditions** (done 2026-10-25): the run-length probe ignores state that
   changes no work (free buffer pool pages falling at a full hit rate). Buffer pool dump/load is not used for the
   tuner's runs: each run must build its own cache under its own configuration.
3. **A MariaDB pass:** its own rules where it differs (thread pool, optimizer costs, group commit wait, Aria temp
   tables, flushing variables).
4. **Settings applied after start beyond resource groups:**
   - generalise the re-apply hook (resgroups.ensure);
   - add MariaDB thread pinning;
   - keep histograms and statistics refresh as objects the tuner records and can reproduce.
5. **Measurement.** Every new lever goes through the learning database like the others. The experience counts decide
   its ranking after a few sessions, and "known neutral here" retires the ones that do not pay.
