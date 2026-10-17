"""Knowledge base about MySQL-family server variables the agent manages.

kind:     size | int | bool | enum | float | str
dynamic:  can be changed with SET GLOBAL without restart
risk:     none | low | durability | observability | compat
"""

KB = {
    # memory
    "innodb_buffer_pool_size": dict(kind="size", dynamic=True, risk="low", cat="memory",
        desc="Main InnoDB cache for data and index pages."),
    "innodb_buffer_pool_instances": dict(kind="int", dynamic=False, risk="none", cat="memory",
        desc="Number of buffer pool partitions; reduces mutex contention."),
    "innodb_buffer_pool_chunk_size": dict(kind="size", dynamic=False, risk="none", cat="memory",
        desc="Unit of buffer pool resizing."),
    "tmp_table_size": dict(kind="size", dynamic=True, risk="low", cat="memory",
        desc="Max size of in-memory internal temporary tables."),
    "max_heap_table_size": dict(kind="size", dynamic=True, risk="low", cat="memory",
        desc="Max size of MEMORY tables; caps in-memory temp tables too."),
    "sort_buffer_size": dict(kind="size", dynamic=True, risk="low", cat="memory",
        desc="Per-session sort buffer."),
    "join_buffer_size": dict(kind="size", dynamic=True, risk="low", cat="memory",
        desc="Per-join buffer for joins without usable indexes."),
    "innodb_log_buffer_size": dict(kind="size", dynamic=True, risk="none", cat="redo",
        desc="Buffer for redo log records before they are written to disk."),
    # redo / checkpointing
    "innodb_redo_log_capacity": dict(kind="size", dynamic=True, risk="none", cat="redo",
        desc="Total redo log size (MySQL 8.0.30+). Larger = fewer forced checkpoints."),
    "innodb_log_file_size": dict(kind="size", dynamic=False, risk="none", cat="redo",
        desc="Size of each redo log file (older MySQL / MariaDB)."),
    "innodb_log_files_in_group": dict(kind="int", dynamic=False, risk="none", cat="redo",
        desc="Number of redo log files (older MySQL)."),
    "innodb_log_writer_threads": dict(kind="bool", dynamic=True, risk="none", cat="redo",
        desc="Dedicated redo log writer threads (8.0.22+)."),
    "innodb_io_capacity": dict(kind="int", dynamic=True, risk="none", cat="io",
        desc="Background I/O budget (IOPS) for flushing."),
    "innodb_io_capacity_max": dict(kind="int", dynamic=True, risk="none", cat="io",
        desc="Upper bound of background flushing IOPS when behind."),
    "innodb_flush_method": dict(kind="enum", dynamic=False, risk="none", cat="io",
        desc="How data files are opened/flushed; O_DIRECT avoids double caching."),
    "innodb_flush_neighbors": dict(kind="int", dynamic=True, risk="none", cat="io",
        desc="Flush neighbouring dirty pages together; useless on SSD."),
    "innodb_use_fdatasync": dict(kind="bool", dynamic=True, risk="none", cat="io",
        desc="Use fdatasync() instead of fsync() where safe."),
    "innodb_read_io_threads": dict(kind="int", dynamic=False, risk="none", cat="io",
        desc="Background read I/O threads."),
    "innodb_write_io_threads": dict(kind="int", dynamic=False, risk="none", cat="io",
        desc="Background write I/O threads."),
    "innodb_lru_scan_depth": dict(kind="int", dynamic=True, risk="none", cat="io",
        desc="How far page cleaners scan LRU for free pages per instance."),
    "innodb_page_cleaners": dict(kind="int", dynamic=False, risk="none", cat="io",
        desc="Page cleaner threads."),
    "innodb_max_dirty_pages_pct": dict(kind="float", dynamic=True, risk="none", cat="io",
        desc="Target maximum dirty page percentage."),
    "innodb_max_dirty_pages_pct_lwm": dict(kind="float", dynamic=True, risk="none", cat="io",
        desc="Dirty page percentage where pre-flushing starts."),
    "innodb_adaptive_flushing_lwm": dict(kind="float", dynamic=True, risk="none", cat="io",
        desc="Redo fill % where adaptive flushing starts."),
    "innodb_doublewrite": dict(kind="enum", dynamic=False, risk="durability", cat="durability",
        desc="Doublewrite buffer protects against torn pages."),
    # durability
    "innodb_flush_log_at_trx_commit": dict(kind="int", dynamic=True, risk="durability", cat="durability",
        desc="1 = fsync redo at every commit (durable); 2 = write at commit, fsync ~1/s."),
    "sync_binlog": dict(kind="int", dynamic=True, risk="durability", cat="durability",
        desc="fsync binary log every N commit groups (1 = durable)."),
    "binlog_group_commit_sync_delay": dict(kind="int", dynamic=True, risk="low", cat="durability",
        desc="Microseconds to wait to grow binlog commit groups."),
    "binlog_group_commit_sync_no_delay_count": dict(kind="int", dynamic=True, risk="low", cat="durability",
        desc="Stop waiting once this many transactions are queued."),
    "binlog_order_commits": dict(kind="bool", dynamic=True, risk="low", cat="durability",
        desc="Commit in binlog order."),
    "binlog_row_image": dict(kind="enum", dynamic=True, risk="compat", cat="replication",
        desc="FULL logs all columns; MINIMAL only changed + PK."),
    # concurrency
    "innodb_adaptive_hash_index": dict(kind="bool", dynamic=True, risk="none", cat="concurrency",
        desc="Hash index built over hot B-tree pages; helps point lookups, can contend."),
    "innodb_adaptive_hash_index_parts": dict(kind="int", dynamic=False, risk="none", cat="concurrency",
        desc="AHI partitions."),
    "innodb_sync_spin_loops": dict(kind="int", dynamic=True, risk="none", cat="concurrency",
        desc="Spin iterations before a thread sleeps on a mutex/rw-lock."),
    "innodb_spin_wait_delay": dict(kind="int", dynamic=True, risk="none", cat="concurrency",
        desc="Max delay between spin polls."),
    "innodb_thread_concurrency": dict(kind="int", dynamic=True, risk="none", cat="concurrency",
        desc="Max threads inside InnoDB at once (0 = unlimited)."),
    "thread_handling": dict(kind="enum", dynamic=False, risk="low", cat="concurrency",
        desc="one-thread-per-connection or pool-of-threads (Percona/MariaDB thread pool)."),
    "thread_pool_size": dict(kind="int", dynamic=True, risk="none", cat="concurrency",
        desc="Thread pool groups."),
    "thread_cache_size": dict(kind="int", dynamic=True, risk="none", cat="connections",
        desc="Threads kept for reuse by new connections."),
    "max_connections": dict(kind="int", dynamic=True, risk="low", cat="connections",
        desc="Maximum concurrent client connections."),
    "table_open_cache": dict(kind="int", dynamic=True, risk="none", cat="connections",
        desc="Open table handles cache."),
    "table_open_cache_instances": dict(kind="int", dynamic=False, risk="none", cat="connections",
        desc="Partitions of the table open cache."),
    "table_definition_cache": dict(kind="int", dynamic=True, risk="none", cat="connections",
        desc="Cached table definitions."),
    "open_files_limit": dict(kind="int", dynamic=False, risk="none", cat="connections",
        desc="File descriptor limit for mysqld."),
    "skip_name_resolve": dict(kind="bool", dynamic=False, risk="compat", cat="connections",
        desc="Skip reverse DNS on connect; host-name grants stop matching."),
    # observability
    "performance_schema": dict(kind="bool", dynamic=False, risk="observability", cat="observability",
        desc="Performance Schema instrumentation (needed for latency measurement)."),
    "innodb_monitor_enable": dict(kind="str", dynamic=True, risk="none", cat="observability",
        desc="Enable InnoDB metrics counters."),
    # misc
    "innodb_change_buffering": dict(kind="enum", dynamic=True, risk="none", cat="io",
        desc="Buffer secondary index changes."),
    "innodb_stats_persistent": dict(kind="bool", dynamic=True, risk="none", cat="optimizer",
        desc="Persistent optimizer statistics."),
    "innodb_purge_threads": dict(kind="int", dynamic=False, risk="none", cat="io",
        desc="Purge threads."),
}

SIZE_UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_size(v):
    """'8G' -> 8589934592; plain numbers pass through."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper()
    if s and s[-1] in SIZE_UNITS:
        return int(float(s[:-1]) * SIZE_UNITS[s[-1]])
    try:
        return int(float(s))
    except ValueError:
        return None


def fmt_size(n):
    n = int(n)
    for u, m in (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if n >= m and n % m == 0:
            return f"{n // m}{u}"
    return str(n)


def human_size(n):
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


BOOL_TRUE = {"ON", "1", "TRUE", "YES"}
BOOL_FALSE = {"OFF", "0", "FALSE", "NO"}


def normalize(name, value):
    """Comparable canonical form of a variable value."""
    if value is None:
        return None
    meta = KB.get(name, {})
    kind = meta.get("kind")
    s = str(value).strip()
    if kind == "size":
        n = parse_size(s)
        return str(n) if n is not None else s
    if kind == "bool":
        if s.upper() in BOOL_TRUE:
            return "ON"
        if s.upper() in BOOL_FALSE:
            return "OFF"
    if kind == "float":
        try:
            return f"{float(s):g}"
        except ValueError:
            return s
    return s


def same_value(name, a, b):
    return normalize(name, a) == normalize(name, b)
