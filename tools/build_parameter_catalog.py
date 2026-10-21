#!/usr/bin/env python3
"""Build the shipped parameter catalog (agent/tuning_agent/knowledge/parameters.json) from the agent's knowledge tables
plus curated impact priors. Re-run after editing the priors below or the knowledge tables; review the diff like code.

Each entry: scope (mysql | os | pseudo), category, description, impact (high | medium | low | unknown: how much it can
move results when it counts), when (the conditions / measured bottleneck in which it counts), precondition (what must
happen in the workload for it to act at all), safety (safe | trade-off category), restart, risk notes, mechanism tags,
and the tuner rules that change it. The learned layer (learning.db, hosted service) adds measured impact per workload
class on top; see docs/design/code-data-separation.md.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from tuning_agent import engine, knowledge, mechanisms, ostune  # noqa: E402

# curated priors: impact when it counts, and when that is (from docs, the studies of 2026-10-17..20 and field experience)
PRIORS = {
    "innodb_buffer_pool_size": ("high", "the working set misses memory (data page reads); too large causes swapping / OOM"),
    "innodb_redo_log_capacity": ("high", "write-heavy load with checkpoint age near capacity (forced flushing); measured +2-10%"),
    "innodb_log_file_size": ("high", "as innodb_redo_log_capacity on older versions"),
    "innodb_flush_log_at_trx_commit": ("high", "commit-bound load (redo fsync per transaction); measured +26-34%"),
    "sync_binlog": ("high", "binary log on and commit-bound (binlog fsync per group)"),
    "disable_log_bin": ("high", "binary log on and commit or CPU bound; measured +18%"),
    "skip_log_bin": ("high", "as disable_log_bin"),
    "innodb_doublewrite": ("high", "write / flush-bound load; measured +12% (YCSB-A) to +35-69% (ingest)"),
    "innodb_change_buffering": ("high", "insert / update-heavy with secondary indexes and data larger than memory; off measured -23% to -88% on ingest"),
    "performance_schema": ("medium", "CPU-bound, many short statements; measured 3-5% (up to 25% of CPU on 2 vCPU)"),
    "ps:profile": ("medium", "as performance_schema, keeping digests and wait summaries"),
    "thread_handling": ("medium", "connections far above CPU cores with run-queue waits; hurt at low concurrency (-9%)"),
    "innodb_io_capacity": ("medium", "flush-bound with disk headroom; raising it on a saturated disk measured -12% to -35%"),
    "innodb_io_capacity_max": ("medium", "as innodb_io_capacity"),
    "innodb_flush_method": ("medium", "double buffering in the page cache; O_DIRECT_NO_FSYNC is a trade-off"),
    "innodb_adaptive_hash_index": ("medium", "point lookups on a hot set (helps) vs latch contention or analytics (JOB 88% slower with it ON)"),
    "innodb_log_writer_threads": ("medium", "few CPUs under pressure (+10% on 2 vCPU); lost 2-13% at 12-16 vCPU"),
    "innodb_buffer_pool_instances": ("medium", "many cores with buffer pool mutex contention"),
    "innodb_max_dirty_pages_pct_lwm": ("medium", "flush-bound; depends on update locality"),
    "innodb_lru_scan_depth": ("medium", "free-page waits (single-page flushes in user threads)"),
    "innodb_purge_threads": ("medium", "history list growing under write load"),
    "innodb_read_ahead_threshold": ("medium", "scans with disk reads (analytics)"),
    "innodb_random_read_ahead": ("low", "partly cached extents read randomly"),
    "innodb_read_io_threads": ("low", "many concurrent disk reads"),
    "innodb_write_io_threads": ("low", "flush-bound with many outstanding writes"),
    "innodb_flush_neighbors": ("low", "rotating disks only (high there)"),
    "innodb_thread_concurrency": ("low", "extreme oversubscription; lost in every measured case so far"),
    "innodb_spin_wait_delay": ("low", "latch spinning in the CPU profile"),
    "innodb_sync_spin_loops": ("low", "latch spinning in the CPU profile"),
    "binlog_group_commit_sync_delay": ("medium", "binary log on, commit-bound at high concurrency"),
    "table_open_cache": ("low", "tables opened during the run (Opened_tables > 0)"),
    "table_definition_cache": ("low", "table definitions loaded during the run"),
    "thread_cache_size": ("low", "connections created during the run"),
    "skip_name_resolve": ("low", "measured time in host name resolution on connect"),
    "tmp_table_size": ("medium", "analytics spilling temporary tables to disk"),
    "max_heap_table_size": ("medium", "as tmp_table_size"),
    "sort_buffer_size": ("low", "sort merge passes during the run"),
    "join_buffer_size": ("medium", "joins without usable indexes / hash joins spilling (analytics)"),
    "optimizer_switch": ("high", "analytics with correlated subqueries (subquery_to_derived: TPC-H +70%)"),
    "innodb_log_buffer_size": ("low", "large transactions waiting for log buffer space"),
    "innodb_max_purge_lag": ("low", "history list growth hurting reads; slows writers"),
    "max_connections": ("low", "a benchmark prerequisite; too high wastes memory"),
    "back_log": ("low", "bursts of new connections dropped by the accept queue"),
    "large_pages": ("medium", "memory-bound work with a large buffer pool (TLB misses); needs measured headroom"),
    "os:hugepages": ("medium", "as large_pages"),
    "os:malloc": ("medium", "allocator time in the CPU profile, many threads"),
    "os:vm.swappiness": ("medium", "memory pressure with mysqld memory swapped out"),
    "os:swapfile": ("low", "safety net for memory experiments; no effect without memory pressure"),
    "os:transparent_hugepage": ("low", "page-fault / compaction stalls in the kernel profile"),
    "os:scheduler": ("low", "I/O-bound on SSD / NVMe"),
    "os:read_ahead_kb": ("low", "sequential scans read from disk"),
    "os:mount_noatime": ("low", "metadata writes on file access"),
    "os:mount_barrier": ("medium", "commit-bound on disks with a volatile write cache; durability trade-off"),
    "os:vm.dirty_bytes": ("low", "fsync stalls from large page-cache writeback"),
    "os:vm.dirty_background_bytes": ("low", "as vm.dirty_bytes"),
    "os:cpu_governor": ("medium", "CPU-bound on hosts that scale the clock down"),
    "os:kernel.sched_autogroup_enabled": ("low", "run-queue waits with other process groups on the host"),
    "os:kernel.numa_balancing": ("low", "multi-socket hosts"),
    "os:net.core.somaxconn": ("low", "accept-queue overflows"),
    "os:net.ipv4.tcp_max_syn_backlog": ("low", "SYN-queue drops"),
    "os:datadir_fs": ("medium", "I/O or commit-bound: file system choice changes allocation and journaling"),
}


# Priors from public documentation (MySQL / Percona / MariaDB manuals, release notes) for performance-relevant variables
# the tuner has not measured yet: a starting estimate, marked prior_source = "documentation" in the catalog and replaced
# by measured impact once experiments exist. (category, impact, when it counts, description, safety, safety note)
DOC_PRIORS = {
    # InnoDB flushing / redo / I/O
    "innodb_adaptive_flushing": ("flushing", "medium", "write-heavy load with checkpoint age swings (off only for experiments)", "Adapts the flushing rate to the redo generation rate.", "safe", None),
    "innodb_adaptive_flushing_lwm": ("flushing", "low", "write-heavy; redo fill level at which adaptive flushing starts", "Low water mark (% of redo capacity) for adaptive flushing.", "safe", None),
    "innodb_flushing_avg_loops": ("flushing", "low", "bursty writes: how quickly flushing follows load changes", "Iterations over which the flushing state is averaged.", "safe", None),
    "innodb_idle_flush_pct": ("flushing", "low", "idle periods between bursts", "Share of innodb_io_capacity used to flush when idle (8.0.18+).", "safe", None),
    "innodb_flush_log_at_timeout": ("durability", "low", "with innodb_flush_log_at_trx_commit = 0 or 2: seconds of commits at risk", "Interval of the redo flush when not flushing at every commit.", "durability", "Longer intervals lose more committed transactions on an OS crash."),
    "innodb_log_write_ahead_size": ("redo", "low", "commit-bound on storage with a larger block than 512 bytes (read-on-write)", "Redo write-ahead block size.", "safe", None),
    "innodb_log_spin_cpu_abs_lwm": ("redo", "low", "commit-bound with spare CPU (user threads spin waiting for redo flush)", "Minimum CPU use below which threads do not spin on redo flush.", "safe", None),
    "innodb_log_spin_cpu_pct_hwm": ("redo", "low", "as innodb_log_spin_cpu_abs_lwm", "Maximum CPU use above which threads stop spinning on redo flush.", "safe", None),
    "innodb_log_wait_for_flush_spin_hwm": ("redo", "low", "as innodb_log_spin_cpu_abs_lwm", "Average flush time above which threads stop spinning for redo flush.", "safe", None),
    "innodb_fsync_threshold": ("flushing", "low", "file creation / extension stalls on large tables", "Bytes written before an fsync during file creation (0 = at the end).", "safe", None),
    "innodb_use_native_aio": ("io", "medium", "I/O-bound on Linux (off only where AIO is broken, e.g. some network file systems)", "Linux native asynchronous I/O for data files.", "safe", None),
    "innodb_doublewrite_pages": ("flushing", "low", "flush-bound with doublewrite on (batch size per doublewrite write)", "Pages per doublewrite batch per thread.", "safe", None),
    "innodb_doublewrite_files": ("flushing", "low", "flush-bound with doublewrite on", "Number of doublewrite files.", "safe", None),
    "innodb_doublewrite_dir": ("flushing", "medium", "flush-bound with doublewrite on and a second, faster device (measured +6% with the doublewrite on its own SATA device)", "Directory of the doublewrite files.", "safe", None),
    "innodb_parallel_doublewrite_path": ("flushing", "medium", "as innodb_doublewrite_dir (Percona Server 8.0 name)", "Percona Server parallel doublewrite file path.", "safe", None),
    "innodb_extend_and_initialize": ("io", "low", "inserts growing tablespaces (file extension writes zeroes)", "Whether file extension writes zeroes (OFF uses fallocate, 8.0.22+).", "safe", None),
    "innodb_autoextend_increment": ("io", "low", "inserts growing the system tablespace", "Extension size of auto-extending system tablespace files.", "safe", None),
    "innodb_lru_flush_size": ("flushing", "low", "free-page waits (MariaDB 10.6+)", "Pages flushed per LRU batch (MariaDB).", "safe", None),
    "innodb_lru_threads": ("flushing", "low", "free-page waits (Percona Server)", "LRU manager threads (Percona Server).", "safe", None),
    "innodb_empty_free_list_algorithm": ("flushing", "low", "free-page waits under I/O-bound load (Percona Server)", "What a user thread does when the free list is empty (backoff / legacy).", "safe", None),
    "innodb_cleaner_lsn_age_factor": ("flushing", "low", "write-heavy with checkpoint age swings (Percona Server)", "Formula for adaptive flushing from LSN age (high_checkpoint / legacy).", "safe", None),
    "innodb_log_file_buffering": ("redo", "low", "commit-bound on MariaDB (redo through the page cache or not)", "Whether the redo log file uses the page cache (MariaDB 10.8+).", "safe", None),
    "innodb_log_file_write_through": ("redo", "low", "commit-bound on MariaDB with a write-through capable device", "Write-through redo writes (MariaDB 11.x).", "safe", None),
    "innodb_data_file_write_through": ("io", "low", "flush-bound on MariaDB with a write-through capable device", "Write-through data file writes (MariaDB 11.x).", "safe", None),
    "innodb_use_atomic_writes": ("flushing", "medium", "write-heavy on storage with atomic writes (MariaDB: replaces the doublewrite buffer there)", "Atomic writes where the device supports them (MariaDB).", "safe", None),
    # buffer pool behaviour
    "innodb_old_blocks_time": ("buffer pool", "medium", "scans mixed with point lookups (scans evicting the hot set)", "Milliseconds a page stays in the old sublist before it can be made young.", "safe", None),
    "innodb_old_blocks_pct": ("buffer pool", "low", "scans mixed with point lookups", "Share of the buffer pool LRU used for the old sublist.", "safe", None),
    "innodb_buffer_pool_load_at_startup": ("buffer pool", "low", "restarts: warm-up time after a restart", "Load the saved page list at startup.", "safe", None),
    "innodb_buffer_pool_dump_at_shutdown": ("buffer pool", "low", "restarts: warm-up time after a restart", "Save the page list at shutdown.", "safe", None),
    "innodb_buffer_pool_dump_pct": ("buffer pool", "low", "restarts with a large buffer pool", "Share of the most recently used pages saved.", "safe", None),
    "innodb_change_buffer_max_size": ("buffer pool", "medium", "insert / update-heavy with secondary indexes and data larger than memory", "Maximum share of the buffer pool used by the change buffer.", "safe", None),
    "innodb_adaptive_hash_index_parts": ("concurrency", "medium", "adaptive hash index on with latch contention on its partitions", "Number of adaptive hash index partitions.", "safe", None),
    "innodb_adaptive_hash_index_cells": ("concurrency", "low", "adaptive hash index on (MariaDB 11.x)", "Size of the adaptive hash index hash table (MariaDB).", "safe", None),
    # concurrency / purge / locking
    "innodb_purge_batch_size": ("purge", "low", "history list growing under write load", "Undo log pages purged per batch.", "safe", None),
    "innodb_max_purge_lag_delay": ("purge", "low", "with innodb_max_purge_lag set: the longest delay per DML", "Maximum delay (microseconds) imposed by innodb_max_purge_lag.", "safe", None),
    "innodb_autoinc_lock_mode": ("concurrency", "medium", "concurrent inserts into tables with AUTO_INCREMENT (bulk inserts)", "AUTO_INCREMENT locking mode (0 traditional, 1 consecutive, 2 interleaved).", "replication", "Mode 2 needs row-based binary logging for safe replication."),
    "innodb_sync_array_size": ("concurrency", "low", "many threads waiting on InnoDB latches", "Partitions of the InnoDB wait array.", "safe", None),
    "innodb_spin_wait_pause_multiplier": ("concurrency", "low", "latch spinning in the CPU profile (PAUSE latency differs by CPU)", "Multiplier of PAUSE instructions in spin-wait loops (8.0.16+).", "safe", None),
    "innodb_concurrency_tickets": ("concurrency", "low", "with innodb_thread_concurrency > 0", "Rows a thread may handle before re-entering the concurrency queue.", "safe", None),
    "innodb_commit_concurrency": ("concurrency", "low", "commit-bound at high concurrency (0 = unlimited, startup only)", "Threads allowed to commit at once.", "safe", None),
    "innodb_lock_wait_timeout": ("locking", "none", "lock waits: changes errors, not throughput", "Seconds a transaction waits for a row lock.", "semantics", "Changes which transactions fail with a lock wait timeout."),
    "innodb_deadlock_detect": ("locking", "medium", "very high concurrency on the same rows (deadlock detection itself becomes the bottleneck)", "Deadlock detection on lock waits.", "semantics", "Off relies on innodb_lock_wait_timeout to resolve deadlocks."),
    "innodb_stats_persistent_sample_pages": ("optimizer", "low", "plans changing with statistics (skewed data)", "Pages sampled for persistent statistics.", "safe", None),
    "innodb_stats_on_metadata": ("optimizer", "low", "information_schema / SHOW queries on many tables", "Update statistics on metadata queries.", "safe", None),
    "innodb_open_files": ("table cache", "low", "many tablespaces (file-per-table) opened during the run", "InnoDB's limit of open tablespace files.", "safe", None),
    "innodb_sort_buffer_size": ("ddl", "low", "index builds (ALTER TABLE, data loads)", "Sort buffer per index build.", "safe", None),
    "innodb_ddl_threads": ("ddl", "low", "index builds", "Threads used by parallel index builds (8.0.27+).", "safe", None),
    "innodb_ddl_buffer_size": ("ddl", "low", "index builds", "Buffer per DDL thread (8.0.27+).", "safe", None),
    "innodb_parallel_read_threads": ("io", "low", "CHECK TABLE / COUNT(*) on the clustered index", "Threads for parallel clustered index reads.", "safe", None),
    "innodb_fill_factor": ("ddl", "low", "index builds followed by inserts in key order", "Page fill during sorted index builds.", "safe", None),
    "innodb_page_size": ("storage", "medium", "set at initialization only; small pages for point lookups, larger for scans / compression", "InnoDB page size (initialization only).", "safe", None),
    "innodb_rollback_segments": ("concurrency", "low", "very high write concurrency (undo slot contention)", "Rollback segments per undo tablespace.", "safe", None),
    "innodb_undo_log_truncate": ("purge", "low", "undo tablespaces growing", "Truncate undo tablespaces above innodb_max_undo_log_size.", "safe", None),
    "innodb_snapshot_isolation": ("locking", "none", "MariaDB 11.6+: write conflicts become errors under REPEATABLE READ", "Snapshot isolation for REPEATABLE READ (MariaDB).", "semantics", "Changes which transactions fail."),
    # memory, temporary tables
    "temptable_max_ram": ("memory", "medium", "analytics with large in-memory temporary tables (TempTable engine)", "Global RAM cap of the TempTable engine.", "safe", None),
    "temptable_max_mmap": ("memory", "low", "TempTable overflow to memory-mapped files (8.0.23+)", "Cap on TempTable memory-mapped files.", "safe", None),
    "temptable_use_mmap": ("memory", "low", "TempTable overflow (deprecated in 8.0.26)", "TempTable overflow to memory-mapped files.", "safe", None),
    "internal_tmp_mem_storage_engine": ("memory", "medium", "analytics with in-memory temporary tables (TempTable vs MEMORY)", "Engine for in-memory internal temporary tables.", "safe", None),
    "tmp_memory_table_size": ("memory", "medium", "as tmp_table_size (MariaDB name)", "Largest in-memory temporary table (MariaDB).", "safe", None),
    "aria_pagecache_buffer_size": ("memory", "medium", "MariaDB analytics with on-disk temporary tables (Aria)", "Aria page cache.", "safe", None),
    "aria_used_for_temp_tables": ("memory", "low", "MariaDB on-disk temporary tables", "Aria (vs MyISAM) for on-disk temporary tables.", "safe", None),
    "key_buffer_size": ("memory", "low", "MyISAM tables only (MariaDB also: MyISAM temporary tables)", "MyISAM key cache.", "safe", None),
    "bulk_insert_buffer_size": ("memory", "low", "bulk inserts into MyISAM", "MyISAM bulk insert tree cache.", "safe", None),
    "read_buffer_size": ("memory", "low", "sequential scans of MyISAM / temporary tables", "Per-scan buffer.", "safe", None),
    "read_rnd_buffer_size": ("memory", "low", "sorted reads (MRR / filesort results)", "Per-read buffer for reads in sorted order.", "safe", None),
    "max_length_for_sort_data": ("optimizer", "low", "filesorts of wide rows (deprecated in 8.0.20)", "Row size limit for single-pass sorts.", "safe", None),
    "join_cache_level": ("optimizer", "medium", "MariaDB joins without usable indexes (hash join / BKA levels)", "Join buffer algorithms allowed (MariaDB).", "safe", "Plans change; measure the whole workload."),
    "join_buffer_space_limit": ("memory", "low", "MariaDB joins with several join buffers", "Total join buffer per query (MariaDB).", "safe", None),
    "mrr_buffer_size": ("memory", "low", "MariaDB multi-range reads", "MRR buffer (MariaDB).", "safe", None),
    "connection_memory_limit": ("memory", "none", "a safety cap (8.0.28+): queries over it fail", "Per-connection memory limit.", "semantics", "Statements exceeding it fail."),
    "global_connection_memory_limit": ("memory", "none", "a safety cap (8.0.28+)", "Memory limit across all connections.", "semantics", "Statements fail once it is reached."),
    "max_session_mem_used": ("memory", "none", "a safety cap (MariaDB)", "Per-session memory limit (MariaDB).", "semantics", "Statements exceeding it fail."),
    # optimizer
    "optimizer_search_depth": ("optimizer", "medium", "joins of many tables (plan search time grows exponentially)", "Depth of the join order search (0 = automatic).", "safe", "Plans change; measure the whole workload."),
    "optimizer_prune_level": ("optimizer", "low", "joins of many tables", "Heuristic pruning of join orders.", "safe", "Plans change; measure the whole workload."),
    "optimizer_use_condition_selectivity": ("optimizer", "medium", "MariaDB multi-joins with skewed data", "Which statistics MariaDB uses for selectivity.", "safe", "Plans change; measure the whole workload."),
    "eq_range_index_dive_limit": ("optimizer", "low", "IN lists longer than the limit (index dives vs statistics)", "Equality ranges above which statistics replace index dives.", "safe", None),
    "range_optimizer_max_mem_size": ("optimizer", "low", "very long IN lists falling back to full scans", "Memory cap of the range optimizer.", "safe", None),
    "histogram_size": ("optimizer", "low", "MariaDB statistics on skewed columns", "Histogram size for engine-independent statistics (MariaDB).", "safe", None),
    "use_stat_tables": ("optimizer", "low", "MariaDB plans using engine-independent statistics", "Use of engine-independent statistics (MariaDB).", "safe", None),
    "query_cache_type": ("cache", "medium", "MariaDB only: read-mostly, few writes (otherwise invalidation contention)", "Query cache (removed in MySQL 8.0).", "safe", None),
    "query_cache_size": ("cache", "medium", "as query_cache_type", "Query cache size (MariaDB).", "safe", None),
    # binary log, replication
    "binlog_format": ("replication", "low", "statements touching many rows (ROW writes more)", "Binary log format.", "replication", "Statement format is unsafe for some statements."),
    "binlog_row_image": ("replication", "low", "updates of wide rows with binary log on", "Columns logged per row change (full / minimal / noblob).", "replication", "Minimal images limit some replication tooling."),
    "binlog_transaction_compression": ("replication", "low", "binary log I/O-bound (8.0.20+); costs CPU", "Compress transaction payloads in the binary log.", "safe", None),
    "binlog_cache_size": ("memory", "low", "large transactions with binary log on (spill to a temporary file)", "Per-session binlog transaction cache.", "safe", None),
    "binlog_commit_wait_count": ("durability", "medium", "MariaDB binary log on, commit-bound at high concurrency", "Commits to wait for to form a group (MariaDB).", "safe", "Adds commit latency."),
    "binlog_commit_wait_usec": ("durability", "medium", "as binlog_commit_wait_count", "Longest wait for a commit group (MariaDB).", "safe", "Adds commit latency."),
    "binlog_group_commit_sync_no_delay_count": ("durability", "low", "with binlog_group_commit_sync_delay", "Group size that ends the delay early.", "safe", None),
    "binlog_max_flush_queue_time": ("durability", "low", "deprecated, no effect in current versions", "Deprecated.", "safe", None),
    "replica_parallel_workers": ("replication", "high", "replicas lagging behind a write-heavy source", "Parallel applier threads on a replica.", "safe", None),
    "replica_preserve_commit_order": ("replication", "low", "parallel replication: commit order as on the source", "Commit order on the replica.", "replication", "Off lets replicas expose a commit order the source never had."),
    "slave_parallel_threads": ("replication", "high", "MariaDB replicas lagging behind a write-heavy source", "Parallel applier threads (MariaDB).", "safe", None),
    "slave_parallel_mode": ("replication", "medium", "MariaDB parallel replication", "Parallel replication mode (MariaDB).", "safe", None),
    "sync_relay_log": ("durability", "low", "replicas: fsync per relay log event", "Relay log sync frequency.", "durability", "0 can lose relay log on crash (recovered with relay_log_recovery)."),
    "sync_relay_log_info": ("durability", "low", "replicas", "Applier position sync frequency.", "safe", None),
    # Galera / PXC
    "wsrep_applier_threads": ("cluster", "high", "PXC nodes falling behind (flow control paused)", "Galera applier threads (PXC 8.0.26+ name).", "safe", None),
    "wsrep_slave_threads": ("cluster", "high", "Galera nodes falling behind (flow control paused)", "Galera applier threads.", "safe", None),
    "wsrep_sync_wait": ("cluster", "medium", "reads that must see the latest cluster writes (causality checks per read)", "Causality checks before statements.", "semantics", "Lower values allow stale reads on other nodes."),
    "wsrep_trx_fragment_size": ("cluster", "medium", "large transactions in Galera (streaming replication)", "Fragment size for streaming replication.", "safe", None),
    "wsrep_retry_autocommit": ("cluster", "low", "certification conflicts on autocommit statements", "Retries of autocommit statements after conflicts.", "semantics", None),
    "wsrep_provider_options": ("cluster", "high", "Galera: gcache size, flow control limits (gcs.fc_limit), send window", "Galera provider options string.", "safe", None),
    "pxc_strict_mode": ("cluster", "none", "PXC: blocks unsupported features", "PXC validation mode.", "semantics", None),
    # connections, threads
    "thread_pool_max_threads": ("connections", "low", "thread pool on with stalls", "Thread pool thread limit.", "safe", None),
    "thread_pool_oversubscribe": ("connections", "medium", "thread pool on: active threads per group", "Threads per group allowed to run at once.", "safe", None),
    "thread_pool_stall_limit": ("connections", "medium", "thread pool on with long statements blocking a group", "Milliseconds before a group is considered stalled.", "safe", None),
    "thread_pool_high_prio_mode": ("connections", "low", "thread pool on with open transactions waiting (Percona)", "Which connections get the high-priority queue.", "safe", None),
    "thread_pool_high_prio_tickets": ("connections", "low", "as thread_pool_high_prio_mode", "High-priority tickets per connection.", "safe", None),
    "thread_pool_idle_timeout": ("connections", "low", "thread pool on with bursty load", "Seconds before an idle pool thread exits.", "safe", None),
    "thread_stack": ("connections", "low", "deep recursion / stored programs (a minimum, not a tuning knob)", "Stack size per thread.", "safe", None),
    "max_allowed_packet": ("connections", "none", "a size limit: large rows or statements fail above it", "Largest packet / statement.", "semantics", None),
    "net_buffer_length": ("connections", "low", "result sets with large rows", "Initial connection buffer.", "safe", None),
    "host_cache_size": ("connections", "low", "connects from many hosts with name resolution on", "Host cache entries.", "safe", None),
    "table_open_cache_triggers": ("table cache", "low", "tables with many triggers (9.x)", "Cached table instances with triggers.", "safe", None),
    "schema_definition_cache": ("table cache", "low", "many schemas", "Cached schema definitions.", "safe", None),
    "tablespace_definition_cache": ("table cache", "low", "many tablespaces", "Cached tablespace definitions.", "safe", None),
    "stored_program_cache": ("table cache", "low", "many stored routines per connection", "Cached routines per connection.", "safe", None),
    "metadata_locks_hash_instances": ("concurrency", "low", "MariaDB metadata lock contention", "MDL hash partitions (MariaDB).", "safe", None),
    # observability with a cost
    "slow_query_log": ("observability", "low", "very high statement rates with long_query_time near 0", "Slow query log.", "safe", None),
    "long_query_time": ("observability", "low", "with slow_query_log on: logging cost at low thresholds", "Slow query threshold.", "safe", None),
    "log_slow_rate_limit": ("observability", "low", "slow log sampling (Percona / MariaDB)", "Log one of every N sessions or queries.", "safe", None),
    "general_log": ("observability", "high", "general log on: every statement written (off for any benchmark)", "General query log.", "observability", None),
    "innodb_monitor_enable": ("observability", "low", "InnoDB metrics counters enabled", "Enables INNODB_METRICS counters.", "safe", None),
    "innodb_status_output": ("observability", "low", "periodic InnoDB status output", "Periodic InnoDB monitor output.", "safe", None),
    "binlog_rows_query_log_events": ("observability", "low", "binary log on: statement text logged per row event", "Log statement text with row events.", "safe", None),
    "innodb_print_all_deadlocks": ("observability", "low", "frequent deadlocks", "Write all deadlocks to the error log.", "safe", None),
    "performance_schema_max_digest_length": ("observability", "low", "performance_schema on (memory per digest)", "Digest text length.", "safe", None),
    "innodb_cmp_per_index_enabled": ("observability", "low", "compressed tables: per-index statistics cost", "Per-index compression statistics.", "safe", None),
}


# optimizer_switch is a container: each flag is its own setting with its own impact and safety. None of them changes
# query results; the risk of every flag is plan regressions (some queries faster, others slower), so they are "safe"
# for correctness but need the whole workload measured. (flag, server default, impact, when it counts, note)
OPTIMIZER_FLAGS = [
    ("subquery_to_derived", "off", "high", "analytics with correlated scalar subqueries in SELECT / WHERE (TPC-H +70%)", "can slow queries the default plan handled well"),
    ("batched_key_access", "off", "medium", "joins doing many random index lookups into data larger than memory (needs mrr=on and a join buffer)", "with mrr_cost_based=off it is used even when not cheaper"),
    ("mrr", "on", "medium", "range / ref reads of secondary indexes on disk-bound data (reads in primary-key order)", ""),
    ("mrr_cost_based", "on", "low", "forcing MRR / BKA on regardless of cost", "off forces MRR even where it is slower"),
    ("block_nested_loop", "on", "low", "joins without usable indexes (8.0.20+: controls hash join use)", ""),
    ("hash_join", "on", "low", "equi-joins without usable indexes (no-op on 8.0.20+, see block_nested_loop)", ""),
    ("index_merge", "on", "low", "OR / AND conditions over several indexed columns", ""),
    ("index_merge_union", "on", "low", "OR over several indexed columns", ""),
    ("index_merge_sort_union", "on", "low", "OR over ranges on several indexes", ""),
    ("index_merge_intersection", "on", "medium", "AND over several indexes; known to pick slow intersections on correlated columns", "turning it off is a common fix for intersect plans"),
    ("engine_condition_pushdown", "on", "low", "NDB only", ""),
    ("index_condition_pushdown", "on", "medium", "range scans with extra conditions on index columns (fewer row reads)", ""),
    ("materialization", "on", "medium", "IN-subqueries (semijoin materialization)", ""),
    ("semijoin", "on", "medium", "IN / EXISTS subqueries", ""),
    ("loosescan", "on", "low", "semijoin strategy choice", ""),
    ("firstmatch", "on", "low", "semijoin strategy choice", ""),
    ("duplicateweedout", "on", "low", "semijoin strategy choice", ""),
    ("subquery_materialization_cost_based", "on", "low", "choice between materialization and EXISTS rewrite", ""),
    ("use_index_extensions", "on", "low", "secondary indexes used with their implicit primary-key columns", ""),
    ("condition_fanout_filter", "on", "medium", "join order estimates on filtered columns (JOB-like multi-joins)", "estimates change join orders"),
    ("derived_merge", "on", "medium", "derived tables / views merged into the outer query", ""),
    ("derived_condition_pushdown", "on", "medium", "conditions pushed into derived tables", ""),
    ("use_invisible_indexes", "off", "low", "testing invisible indexes", "on makes the optimizer use indexes marked invisible"),
    ("skip_scan", "on", "low", "range on the second column of a composite index", ""),
    ("prefer_ordering_index", "on", "low", "ORDER BY ... LIMIT choosing an ordering index over a better filter", "off is a common fix for slow ORDER BY LIMIT plans"),
    ("hypergraph_optimizer", "off", "high", "complex multi-way joins (experimental optimizer in 8.0-9.x builds that include it)", "experimental: plans and supported features differ; debug builds only in many versions"),
    ("hash_set_operations", "on", "low", "UNION / INTERSECT / EXCEPT with large inputs", ""),
    ("favor_range_scan", "off", "low", "range scans vs ref access choice (9.x)", ""),
]


# how performance responds to the value (to be validated per workload; the learner records sweep curves):
#   saturating: more is better up to a point, then flat (or slightly worse through its cost) - climb, stop at saturation
#   optimum:    too little and too much both hurt - bracket both sides, refine towards the better one
#   binary / choice: compare the alternatives
#   tradeoff:   monotonic in speed, paid with durability or safety - each step is a decision, not a search
RESPONSE = {
    "innodb_buffer_pool_size": ("saturating", "up to the working set / data size; capped by the memory budget"),
    "innodb_redo_log_capacity": ("saturating", "until checkpoint age stays well below capacity; recovery time grows"),
    "innodb_log_file_size": ("saturating", "as innodb_redo_log_capacity"),
    "innodb_log_buffer_size": ("saturating", "until no log buffer waits"),
    "table_open_cache": ("saturating", "until no tables are opened during the run"),
    "table_definition_cache": ("saturating", "until no definitions are loaded during the run"),
    "tmp_table_size": ("saturating", "until temporary tables stop spilling to disk; capped by the memory budget"),
    "max_heap_table_size": ("saturating", "as tmp_table_size"),
    "sort_buffer_size": ("optimum", "large enough to avoid merge passes; larger allocations cost time per sort"),
    "join_buffer_size": ("optimum", "large enough for the hash join build side; larger allocations cost time"),
    "read_buffer_size": ("optimum", "scan buffering; larger allocations cost time per scan"),
    "read_rnd_buffer_size": ("optimum", "as read_buffer_size"),
    "thread_cache_size": ("saturating", "until no threads are created during the run"),
    "innodb_thread_concurrency": ("optimum", "0 = unlimited; too low starves CPUs, too high lets contention through"),
    "thread_pool_size": ("optimum", "around the number of CPUs; too few starves, too many brings contention back"),
    "innodb_purge_threads": ("optimum", "enough to keep the history list bounded; more compete with user threads"),
    "innodb_buffer_pool_instances": ("optimum", "enough to split mutex contention; more add overhead"),
    "innodb_page_cleaners": ("optimum", "as innodb_buffer_pool_instances for flushing"),
    "innodb_io_capacity": ("optimum", "matched to what the storage sustains: too low falls behind, too high steals I/O from reads"),
    "innodb_io_capacity_max": ("optimum", "as innodb_io_capacity, for catch-up flushing"),
    "innodb_lru_scan_depth": ("optimum", "enough free pages; deeper scans cost CPU"),
    "innodb_max_dirty_pages_pct_lwm": ("optimum", "trade between write combining and flushing smoothness"),
    "innodb_spin_wait_delay": ("optimum", "depends on the CPU: too short burns CPU, too long delays wake-ups"),
    "innodb_sync_spin_loops": ("optimum", "as innodb_spin_wait_delay"),
    "innodb_read_io_threads": ("optimum", "enough outstanding reads for the device; more add context switches"),
    "innodb_write_io_threads": ("optimum", "as innodb_read_io_threads for writes"),
    "innodb_read_ahead_threshold": ("optimum", "earlier read-ahead helps scans, wastes I/O on random access"),
    "binlog_group_commit_sync_delay": ("optimum", "longer batches fewer fsyncs but adds commit latency"),
    "max_connections": ("saturating", "a prerequisite: enough for the clients; more only cost memory"),
    "innodb_flush_log_at_trx_commit": ("tradeoff", "1 (durable) / 2 / 0: faster, less durable"),
    "sync_binlog": ("tradeoff", "1 (durable) / N / 0"),
    "innodb_doublewrite": ("binary", ""), "innodb_adaptive_hash_index": ("binary", ""), "innodb_change_buffering": ("choice", ""),
    "innodb_flush_method": ("choice", ""), "performance_schema": ("binary", ""), "thread_handling": ("choice", ""),
    "innodb_log_writer_threads": ("binary", ""), "large_pages": ("binary", ""), "os:malloc": ("choice", ""),
}

# memory cost per setting (agent/tuning_agent/memory.py budgets with these): memory is one pool shared by all options
MEMORY_COST = {
    "innodb_buffer_pool_size": "direct: its size, plus ~3% for control blocks / page hash / AHI",
    "innodb_log_buffer_size": "direct: its size",
    "key_buffer_size": "direct: its size",
    "tmp_table_size": "per active connection (one in-memory temporary table), capped by temptable_max_ram",
    "max_heap_table_size": "per active connection (with tmp_table_size, the lower applies)",
    "temptable_max_ram": "global cap on in-memory temporary tables",
    "sort_buffer_size": "per active connection, per sort - possibly more than once per query",
    "join_buffer_size": "per active connection, per join - possibly more than once per query",
    "read_buffer_size": "per active connection, per sequential scan",
    "read_rnd_buffer_size": "per active connection, per sorted read",
    "binlog_cache_size": "per active connection with a transaction (binary log on)",
    "table_open_cache": "~8 KiB per cached table instance",
    "table_definition_cache": "~6 KiB per cached table definition",
    "max_prepared_stmt_count": "~16 KiB per prepared statement held (a cap; budgeted at up to 20 per connection)",
    "max_connections": "~768 KiB base per allowed connection (+ performance_schema ~128 KiB); 2 x CPUs of them assumed active with all per-query buffers",
    "performance_schema": "~192 MiB fixed + ~128 KiB per allowed connection",
    "thread_cache_size": "~256 KiB per cached idle thread",
    "os:hugepages": "pins 2 MiB per page: unusable by anything else, even when the buffer pool does not use it",
    "os:swapfile": "disk, not memory: lets cold memory be swapped instead of OOM",
}


def versions(v, all_series):
    """Where a server variable exists and how it differs: {"series": [...] | "all", "missing": [...], "default": value or
    {series: value}, "dynamic": bool or {series: bool}} from the release inventory (tools/varinventory)."""
    if not all_series:
        return None
    if not v:
        return None  # a startup option rather than a variable (disable_log_bin), or a plugin's (rocksdb_*)
    have = all_series if v.get("series") == "all" else v.get("series") or []
    out = {"series": "all" if have == all_series else have, "missing": [s for s in all_series if s not in have]}
    for k in ("default", "dynamic", "min", "max", "values"):
        if v.get(k) is not None:
            out[k] = v[k]
    return out


def main():
    # which rules change which variables: b.add("<id>", "<title>", [("<var>", ...), ...] and sweep("<id>", "<title>", "<var>", ...)
    import re
    src = open(engine.__file__).read()
    rules_by_var = {}
    for m in re.finditer(r'b\.add\(\s*"([a-z0-9-]+)",\s*(?:f?"[^"]*"|\([^)]*\)),\s*\[((?:\("[^"]+",[^)]*\),?\s*)+)', src):
        for var in re.findall(r'\("([a-z_:.0-9-]+)",', m.group(2)):
            rules_by_var.setdefault(var, set()).add(m.group(1))
    for m in re.finditer(r'sweep\(\s*"([a-z0-9-]+)",\s*"[^"]*",\s*"([a-z_:.0-9-]+)"', src):
        rules_by_var.setdefault(m.group(2), set()).add(m.group(1))
    out = {}
    try:
        sv = json.load(open(os.path.join(ROOT, "agent", "tuning_agent", "knowledge_data", "server_variables.json")))
    except OSError:
        sv = {"series": {}, "variables": {}}
    all_series = sorted(sv["series"])
    names = set(knowledge.KB) | set(ostune.OS_KB) | set(knowledge.TRADEOFFS) | set(PRIORS) | set(DOC_PRIORS)
    for name in sorted(names):
        kb = knowledge.KB.get(name) or {}
        okb = ostune.OS_KB.get(name)
        trade = knowledge.TRADEOFFS.get(name)
        rules = sorted(rules_by_var.get(name, set()))
        tags = sorted({t for r in rules for t in (mechanisms.MECHANISMS.get(r) or ((),))[0]})
        need = mechanisms.VAR_NEEDS.get(name)
        cnt = mechanisms.VAR_COUNTERS.get(name)
        doc = DOC_PRIORS.get(name)
        impact, when = PRIORS.get(name) or ((doc[1], doc[2]) if doc else ("unknown", ""))
        trade = trade or ((None, doc[4], doc[5]) if doc and doc[4] != "safe" else None)
        out[name] = {
            "scope": "os" if name.startswith("os:") else "pseudo" if ":" in name else "mysql",
            "category": kb.get("cat") or (okb[2] if okb else "") or (doc[0] if doc else ""),
            "description": kb.get("desc") or (okb[0] if okb else "") or (doc[3] if doc else ""),
            "prior_source": "curated" if name in PRIORS else "documentation" if doc else None,
            "impact": impact, "when": when,
            "precondition": ({"writes": "the workload writes (redo / flushing active)", "reads": "pages are read from disk",
                              "dns": "measured time in host name resolution"}.get(need) if need else None)
                            or (f"{cnt[1]} during the run ({cnt[0].lstrip('_')} > 0)" if cnt else None),
            "safety": trade[1] if trade else ("safe" if kb.get("risk") in (None, "none", "low") else kb.get("risk")),
            "safety_note": trade[2] if trade and len(trade) > 2 else None,
            "restart": (not kb.get("dynamic", True)) if kb else (bool(okb[1]) if okb else None),
            "mechanism_tags": tags, "rules": rules, "memory_cost": MEMORY_COST.get(name),
            "response": {"shape": RESPONSE[name][0], "note": RESPONSE[name][1]} if name in RESPONSE else None,
            "versions": versions(sv["variables"].get(name), all_series) if ":" not in name else None,
        }
        if doc and not kb and out[name]["versions"]:
            dyn = out[name]["versions"].get("dynamic")
            out[name]["restart"] = (not dyn) if isinstance(dyn, bool) else None
    if "optimizer_switch" in out:
        out["optimizer_switch"].update(impact="container", when="a list of independent optimizer flags: see the optimizer_switch:<flag> entries",
                                       description="Optimizer flags (on / off each); the tuner changes them one flag at a time.")
    for flag, default, impact, when, note in OPTIMIZER_FLAGS:
        out[f"optimizer_switch:{flag}"] = {
            "scope": "mysql", "category": "optimizer", "parent": "optimizer_switch", "server_default": default,
            "description": f"optimizer_switch flag {flag} (server default {default}).", "impact": impact, "when": when,
            "precondition": "statements whose plans use this strategy", "safety": "safe",
            "safety_note": ("Does not change query results; plans change, so some queries may get slower. " + note[:1].upper() + note[1:]).strip(),
            "restart": False, "mechanism_tags": ["cpu-exec", "io-read"],
            "rules": sorted(r for r in rules_by_var.get("optimizer_switch", set()) if flag.replace("_", "-") in r or r in ("bka-mrr",) and flag in ("batched_key_access", "mrr", "mrr_cost_based"))}
    path = os.path.join(ROOT, "agent", "tuning_agent", "knowledge_data", "parameters.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"version": 1, "generated_from": "tools/build_parameter_catalog.py", "parameters": out}, f, indent=1, sort_keys=True)
    print(f"{len(out)} parameters -> {path}; with an impact prior: {sum(1 for v in out.values() if v['impact'] != 'unknown')}"
          f" (from documentation: {sum(1 for v in out.values() if v.get('prior_source') == 'documentation')})")


if __name__ == "__main__":
    main()
