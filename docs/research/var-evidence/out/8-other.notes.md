# 8-other: most useful findings

1. transaction_isolation (semantics-gated) is the only potentially high-impact knob here: bound = row-lock-wait share due to gap/next-key locks. Not collected: performance_schema.data_locks gap-lock share (LOCK_MODE LIKE '%GAP%') and deadlocks (innodb_metrics lock_deadlocks / MariaDB Innodb_deadlocks).
2. innodb_page_size (reinit only) can matter (medium) for I/O-bound random point lookups; add per-table hot-row density (AVG_ROW_LENGTH, rows read vs pages read) to bound it.
3. query_cache_type/size (MariaDB): all Qcache_* counters are already collected; hit share = Qcache_hits/(Qcache_hits+Com_select). Missing: separating query-cache mutex waits in perf/eBPF categories.
4. os:transparent_hugepage: add /proc/vmstat deltas (compact_stall, thp_fault_alloc/fallback, thp_split_page) and perf dTLB/page-walk counters; today only the kernel CPU profile hints at it.
5. Purge group (innodb_max_purge_lag[_delay], innodb_purge_batch_size): history length is collected; add purge_dml_delay_usec, purge_undo_log_pages, purge_invoked (innodb_metrics, disabled by default) and logical reads per row over time to measure the read cost of history.
6. innodb_undo_log_truncate: add undo tablespace sizes and undo_truncate_count/usec (MariaDB: Innodb_undo_truncations) to tie throughput dips to truncations.
7. rg:innodb_bg_cpus: per-thread run-queue wait is collected; add per-background-thread CPU (performance_schema.threads THREAD_OS_ID -> /proc/<tid>/stat) to correlate flush bursts with query CPU starvation.
8. ps:profile is fully measurable from the existing CPU profile (performance_schema category share) - low-medium gain when CPU-bound.
9. No throughput signal (validate by experiment or leave alone): innodb_lock_wait_timeout, innodb_snapshot_isolation, innodb_checksum_algorithm (crc32 already), binlog_transaction_dependency_history_size (affects replica parallelism only).
10. DDL-only knobs (innodb_ddl_buffer_size, innodb_ddl_threads, innodb_sort_buffer_size, innodb_fill_factor) cannot matter for DML benchmarks; if DDL is measured, add events_stages 'stage/innodb/alter table (...)' timings and innodb_metrics index_page_splits.
