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


def flags_subset(wanted, actual):
    """optimizer_switch: every flag in `wanted` has that value in `actual`."""
    act = dict(x.split("=", 1) for x in str(actual or "").split(",") if "=" in x)
    for x in str(wanted or "").split(","):
        if "=" in x:
            k, v = x.split("=", 1)
            if act.get(k.strip()) != v.strip():
                return False
    return True


def same_value(name, a, b):
    return normalize(name, a) == normalize(name, b)


# ---- trade-offs ("benchmark cheats") --------------------------------------------------
# A setting is a trade-off when its VALUE gives up a guarantee production systems normally rely on.
# category: durability | observability | replication | semantics
def _neq(x):
    return lambda v: str(v).strip().upper() != x


TRADEOFFS = {
    "innodb_flush_log_at_trx_commit": (_neq("1"), "durability",
        "Committed transactions from the last ~1 s are lost on an OS crash or power loss (value 0: also on a mysqld crash).", "value is not 1"),
    "sync_binlog": (_neq("1"), "durability",
        "Binary log events can be lost on an OS crash: replicas and point-in-time recovery diverge from the data.", "value is not 1 (with the binary log enabled)"),
    "innodb_doublewrite": (lambda v: str(v).upper() in ("OFF", "0", "DETECT_ONLY"), "durability",
        "Torn (partially written) pages after a crash can no longer be repaired: possible unrecoverable corruption.", "OFF or DETECT_ONLY"),
    "skip_log_bin": (lambda v: True, "replication",
        "No binary log: no replication and no point-in-time recovery.", "option present"),
    "disable_log_bin": (lambda v: True, "replication",
        "No binary log: no replication and no point-in-time recovery.", "option present"),
    "performance_schema": (_neq("ON"), "observability",
        "No statement latency / wait instrumentation: diagnostics and query analysis are lost.", "OFF"),
    "binlog_row_image": (lambda v: str(v).upper() != "FULL", "replication",
        "Row events no longer carry full before/after images; some CDC / audit consumers break.", "MINIMAL or NOBLOB"),
    "transaction_isolation": (lambda v: str(v).upper() not in ("REPEATABLE-READ", "SERIALIZABLE"), "semantics",
        "Weaker isolation changes what concurrent transactions see; application correctness must be re-validated.", "READ-COMMITTED or READ-UNCOMMITTED"),
    "innodb_checksum_algorithm": (lambda v: str(v).lower().startswith("none"), "durability",
        "Page corruption is no longer detected.", "none / strict_none"),
}

# option-file switches that are not visible as variables in SHOW VARIABLES
OPTIONS = {
    "skip_log_bin": "log_bin",
    "disable_log_bin": "log_bin",
}
KB["skip_log_bin"] = dict(kind="str", dynamic=False, risk="durability", cat="replication",
                          desc="Start without the binary log (option-file switch).")
KB["disable_log_bin"] = dict(kind="str", dynamic=False, risk="durability", cat="replication",
                             desc="Start without the binary log (option-file switch).")
KB["transaction_isolation"] = dict(kind="enum", dynamic=True, risk="semantics", cat="semantics",
                                   desc="Default transaction isolation level.")
KB["innodb_parallel_read_threads"] = dict(kind="int", dynamic=True, risk="none", cat="concurrency",
                                          desc="Threads for parallel clustered index reads (COUNT(*), CHECK TABLE).")
KB["temptable_max_ram"] = dict(kind="size", dynamic=True, risk="low", cat="memory",
                               desc="RAM the TempTable engine may use before spilling to mmap/disk.")
KB["read_rnd_buffer_size"] = dict(kind="size", dynamic=True, risk="low", cat="memory",
                                  desc="Buffer for multi-range reads / sorted row reads.")
KB["innodb_read_ahead_threshold"] = dict(kind="int", dynamic=True, risk="none", cat="io",
                                         desc="Sequential pages accessed in an extent that trigger linear read-ahead.")
KB["innodb_random_read_ahead"] = dict(kind="bool", dynamic=True, risk="none", cat="io",
                                      desc="Random read-ahead of whole extents.")
KB["innodb_stats_persistent_sample_pages"] = dict(kind="int", dynamic=True, risk="none", cat="optimizer",
                                                  desc="Pages sampled for persistent index statistics.")
KB["innodb_deadlock_detect"] = dict(kind="bool", dynamic=True, risk="low", cat="concurrency",
                                    desc="Deadlock detection.")
KB["optimizer_switch"] = dict(kind="flags", dynamic=True, risk="none", cat="optimizer",
                              desc="Optimizer feature flags (the agent only changes individual flags).")
KB["innodb_numa_interleave"] = dict(kind="bool", dynamic=False, risk="none", cat="memory",
                                    desc="Interleave buffer pool allocation across NUMA nodes.")
KB["large_pages"] = dict(kind="bool", dynamic=False, risk="low", cat="memory",
                         desc="Allocate the buffer pool from huge pages (needs vm.nr_hugepages and memlock).")
KB["binlog_cache_size"] = dict(kind="size", dynamic=True, risk="none", cat="memory",
                               desc="Per-session binlog cache.")


# operating-system pseudo-variables (see ostune.py)
from .ostune import OS_KB  # noqa: E402
for _k, (_desc, _restart, _cat) in OS_KB.items():
    KB[_k] = dict(kind="str", dynamic=not _restart, risk="durability" if _k == "os:mount_barrier" else "low", cat="os/" + _cat, desc=_desc)
TRADEOFFS["os:mount_barrier"] = (lambda v: str(v).upper() == "OFF", "durability",
                                 "Without write barriers a power loss can lose or reorder writes the database already considered durable.",
                                 "OFF (ext4 nobarrier / barrier=0)")


TRADEOFFS["os:datadir_fs"] = (lambda v: any(x in str(v) for x in ("barrier=0", "nobarrier", "data=writeback", "sync=disabled")),
                              "durability", "The filesystem may acknowledge writes before they are durable (crash can lose or corrupt recent data).",
                              "options barrier=0 / nobarrier / data=writeback / sync=disabled")


def tradeoff_of(name, value):
    """None for a production-safe value, else {variable, value, category, lost}."""
    t = TRADEOFFS.get(name)
    if not t or value is None:
        return None
    pred, cat, lost = t[:3]
    try:
        if pred(value):
            return {"variable": name, "value": str(value), "category": cat, "lost": lost}
    except Exception:
        return None
    return None


def tradeoffs_in(managed):
    out = []
    for k, v in (managed or {}).items():
        t = tradeoff_of(k, v)
        if t:
            out.append(t)
    return out


def policy_rules():
    """Machine- and human-readable description of the policies (served by the web app)."""
    return {
        "policies": {
            "safe": "Only production-safe changes are applied. Trade-off settings are shown as advice and never applied.",
            "benchmark": "Production-safe experiments first; when they are exhausted, trade-off settings may be applied. "
                         "Runs with trade-offs are flagged and their results are reported separately from production-safe results.",
        },
        "tradeoffs": [{"variable": k, "when": t[3] if len(t) > 3 else "", "category": t[1], "lost": t[2]} for k, t in TRADEOFFS.items()],
        "safe_definition": "A change is production-safe when it does not weaken durability, replication / point-in-time recovery, "
                           "observability or transaction semantics. Safe changes can still make performance worse - every experiment "
                           "is measured and reverted unless it beats the noise threshold or measurably reduces CPU / I/O per transaction.",
        "guards": [
            "Memory: buffer pool and per-session buffers are sized against free RAM; the buffer pool is shrunk when the host swaps or shows memory pressure.",
            "Restarts: only when the agent was installed without --no-restart; if mysqld fails to start, the previous option file is restored automatically.",
            "One experiment at a time; neutral and efficiency-loss changes are reverted to the parent configuration.",
            "Changes already tried in the session without benefit are not proposed again.",
        ],
        "variables": {k: {"desc": v.get("desc"), "dynamic": v.get("dynamic"), "category": v.get("cat")} for k, v in KB.items()},
    }
