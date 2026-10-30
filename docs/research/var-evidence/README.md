# Evidence per tunable variable (2026-10-31)

For each of the 313 settings the tuner may change, the question: **how do we know this value is not optimal here, which
way to move it, and how much could it matter?** The answers come from measured signals, not rules of thumb. The tuner
should try a change because the evidence shows its mechanism costs time or resources, then confirm it with an experiment
whose predicted metric movement can be checked.

- `evidence.json`: one entry per variable (format in `BRIEF.md`):
  - when the variable can matter at all;
  - its signals: exact counter or table, direction, condition, mechanism;
  - how to bound the gain from measurements;
  - a validating experiment (change, predicted movement, how to read it, cost);
  - whether the tuner collects the signals today;
  - sources.
- `out/<group>.json` and `out/<group>.notes.md`: the eight research groups and their ten-point notes.
- `chunks/`: the catalog entries each group started from.

Coverage of the signals today: **72** variables fully collected, **156** partly, **85** not at all.

## What to add to the collector, by how much it unlocks

Ordered by the number of high- and medium-impact variables each addition gives evidence for.

| # | Add | Unlocks | Notes |
|---|---|---|---|
| 1 | InnoDB flushing and redo metrics (`innodb_metrics`): `buffer_flush_n_to_flush_by_age` vs `_by_dirty_page`, `_requested` vs `buffer_flush_avg_page_rate`, `buffer_flush_sync_total_pages` / `_sync_waits`, `log_on_file_space_waits`, `log_on_flush_waits`, `buffer_LRU_get_free_waits`, dblwr counters; derived: pages flushed per transaction | redo capacity, io_capacity / _max, adaptive flushing, flush_sync, max_dirty_pages_pct(_lwm), LRU scan depth / flush size, doublewrite | Says *which* mechanism drives flushing and whether flushing stalls the foreground. Write amplification per transaction is the efficiency metric. |
| 2 | Binary log file I/O (`file_summary_by_event_name` `wait/io/file/sql/binlog`: fsync count and time, bytes), plus the group-commit waits `MYSQL_BIN_LOG::LOCK_sync` / `COND_done` | sync_binlog, group commit delay, binlog off, row image, format, compression, rows_query events | Only InnoDB file types are collected today. MariaDB group commit is already measurable from status (`Binlog_group_commit_trigger_*`). |
| 3 | Thread pool counters: Percona `Threadpool_*` (queue length, starved, average queue wait), MariaDB `THREAD_POOL_STATS` / `_QUEUES` | thread_pool_size, oversubscribe, stall_limit, max_threads, high-prio settings | Queue wait is the time that bounds the gain. None collected today. |
| 4 | Plans of the top statements by time (`EXPLAIN FORMAT=TREE` / `EXPLAIN ANALYZE`; MariaDB `ANALYZE FORMAT=JSON`), planning time (`stage/sql/statistics`, `stage/sql/optimizing`), warning 3170 counts | every optimizer flag and limit | Test flags on one statement first (`SET_VAR` / hints); the bound is that statement's share of time. Most do nothing on OLTP. |
| 5 | Commit-path and background threads per thread: CPU and run-queue wait of log_writer / log_flusher / page_cleaner / purge (performance_schema `THREAD_OS_ID` + `/proc` task counters) | log_writer_threads, log spin settings, page cleaners, `rg:innodb_bg_cpus`, purge settings | The purge part is built (purge triage, 2026-10-31). Extending it to the other background threads is the same code. |
| 6 | OS state: write cache and FUA per device, flush latency per device, CPU governor and effective MHz of mysqld, `HugePages_*`, THP `vmstat` counters, dTLB page-walk cycles, NUMA node count and `numastat`, swap-in/out rates | disk_write_cache, mount_barrier, cpu_governor, hugepages / large_pages, THP, NUMA settings, swap settings | Several are cheap reads of sysfs and `/proc`. The page-walk share needs perf. **The OS group was written mostly from memory: verify before use.** |
| 7 | Network: `softnet_stat` drops / squeeze, `nstat` listen overflows / drops / retransmits, per-CPU softirq, `/proc/interrupts`; client side: squeeze, drops and `cpu_mhz` into the summary, and the client wait split into network and client processing | net.core.*, somaxconn / syn_backlog, rps, IRQ CPUs, client settings | With these at zero, the network settings can be skipped outright. |
| 8 | Smaller ones: TempTable memory (`memory/temptable/*`), made-young / not-young rates, ICP counters, deadlocks (`lock_deadlocks`; MariaDB `Innodb_deadlocks`), gap-lock share (`data_locks`), AHI search counters, `Table_open_cache_misses` / `_overflows`, query and slow log file I/O | temptable_max_ram, old_blocks_*, ICP, deadlock detection, isolation, AHI, table caches, logging | |

## Settings that cannot matter: never propose them where this holds

- **Documented no-ops:**
  - binlog_max_flush_queue_time;
  - metadata_locks_hash_instances (MariaDB);
  - innodb_rollback_segments (128 is the maximum);
  - hash_join (8.0.19+);
  - engine_condition_pushdown (NDB only);
  - max_length_for_sort_data (8.0.20+);
  - innodb_parallel_doublewrite_path (after PS 8.0.22);
  - innodb_buffer_pool_chunk_size (MariaDB 10.11.12+ / 11.4.6+).
- **Unused on a client host:** cpu_affinity, datadir_fs, disk_write_cache, hugepages, malloc, mount_barrier, mysqld_numa_policy, aio-max-nr, somaxconn, tcp_max_syn_backlog, scheduler / read_ahead.
- **Gated by a condition:**
  - NUMA settings on single-node hosts;
  - the connection path (back_log, host cache, name resolution, thread cache) without new connections during the run;
  - DDL settings without DDL;
  - change-buffer size with buffering off or removed (MariaDB 11);
  - read_ahead_kb for O_DIRECT data files;
  - fs.aio-max-nr without io_setup EAGAIN;
  - WAN TCP settings on a LAN.
- **No performance signal; functional limits only:**
  - max_allowed_packet, thread_stack, tcp keepalive, ip_local_port_range, tcp_tw_reuse;
  - innodb_lock_wait_timeout, innodb_fsync_threshold, innodb_idle_flush_pct, innodb_autoextend_increment.

## Per-group highlights

- **Buffer pool and memory:**
  - The pool size is fully covered: the data-page-read wait share bounds it, and disk reads per transaction are the predicted metric.
  - TempTable overflow can be invisible in 8.0 (bug #98741): use the memory instruments.
- **I/O, flushing and redo:**
  - Redo capacity is the top lever. Whether flushing is driven by redo age or by dirty pages decides it; see item 1 above.
  - Raising io_capacity on a saturated disk measured −12% to −35%.
- **Durability, concurrency and replication:**
  - trx_commit and doublewrite are well covered.
  - Replication and Galera settings need replica- or cluster-side data, which a single-server benchmark cannot provide.
- **Connections and observability:** performance_schema's cost shows in the CPU profile (3–5% typical, up to about 25% on 2 vCPUs). Cut instruments before switching it off; the tuner needs it to observe.
- **Optimizer:** plans first. `index_merge_intersection` and `prefer_ordering_index` are the flags most often worth turning off.
- **OS:** see item 6 above. Durability and clock settings have the largest effect and none of their state is collected.
- **Client:** the database-side "waiting for the client" share, minus the network round trip, bounds every client setting.
- **Other:** `transaction_isolation` can be large, but only where the application allows the semantics. `innodb_page_size` needs a new instance.

## Caveats

- Counter names were checked against the official documentation where the agents could fetch it.
  - mariadb.com returned 403 to automated fetches.
  - The OS group and parts of the other groups rely on memory and are marked so in their notes.
  - Verify any name against a live server before adding it to the collector.
- Impact estimates are the typical range in practice. The tuner's own measurements (the learner's `param_evidence`) replace them where they exist.
