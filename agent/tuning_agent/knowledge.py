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


POW2_INTS = ("threads", "concurrency", "instances", "cleaners", "thread_pool_size", "thread_pool_max_active")


def _pow2(n):
    return n > 0 and (n & (n - 1)) == 0


def _one_digit(n):
    """1000, 3000, 40: one significant digit."""
    n = abs(int(n))
    while n >= 10 and n % 10 == 0:
        n //= 10
    return n < 10


def roundness(var, value):
    """0 = round (one significant digit, a power of two, whole GiB / power-of-two size), 1 = two significant digits,
    2 = anything else. Among settings that perform the same, the rounder value is the one to recommend."""
    import math
    try:
        n = float(value)
    except (TypeError, ValueError):
        try:
            n = parse_size(str(value))
        except Exception:
            n = None
        if n is None:
            return 2  # not a number (O_DIRECT, STATEMENT): no roundness preference
    if not math.isfinite(n):
        return 2
    if n != int(n):
        return 2
    n = int(n)
    if abs(n) <= 10 or _pow2(abs(n)) or _one_digit(n):
        return 0
    if (KB.get(var) or {}).get("kind") == "size" and n % (512 * 1024 ** 2) == 0:
        return 0
    digits = str(abs(n)).rstrip("0")
    return 1 if len(digits) <= 2 else 2


def nice_number(var, x):
    """Snap an agent-generated value with three or more significant digits (or a fraction) to a round one: sizes down to a power of two below 1 GiB, to 512 MiB steps up to 8 GiB
    and whole GiB above; thread-like counts to a power of two; other counts and percentages to one significant digit
    (1072 -> 1000, 47 -> 50). Small values (<= 10) and already round values are kept."""
    import math
    kind = (KB.get(var) or {}).get("kind")
    # integers and sizes only: fractions (dirty-page percentages 0.5 / 99.9), pseudo settings (os:hugepages page counts,
    # os:swapfile) and settings unknown to the KB keep the exact value
    if x is None or not math.isfinite(x) or x <= 0 or kind not in ("int", "size") or str(var).startswith(("os:", "client:", "rg:", "ps:")):
        return x
    if roundness(var, x) < 2:  # two significant digits (24 = 2 x 12 cores, 1500) carry meaning: kept
        return x
    if kind == "size":
        G, M = 1024 ** 3, 1024 ** 2
        # sizes are memory: round down, never above what a budget or cap computed
        if x >= 8 * G:
            return float(math.floor(x / G) * G)
        if x >= G:
            return float(math.floor(x / (512 * M)) * 512 * M)
        return float(2 ** math.floor(math.log2(max(x, 1024))))
    if abs(x) <= 10:
        return float(round(x))
    if any(t in var for t in POW2_INTS):
        return float(2 ** round(math.log2(x)))
    e = 10 ** int(math.floor(math.log10(abs(x))))
    return float(round(x / e) * e)


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
# MyRocks (Percona Server's RocksDB engine) and Galera (Percona XtraDB Cluster, MariaDB Galera): their own settings
KB["rocksdb_block_cache_size"] = dict(kind="size", dynamic=True, risk="none", cat="memory",
                                      desc="MyRocks block cache: uncompressed data / index blocks in memory (the RocksDB counterpart of the buffer pool).")
KB["rocksdb_db_write_buffer_size"] = dict(kind="size", dynamic=False, risk="none", cat="memory",
                                          desc="MyRocks: memory for all memtables (writes buffered before flushing to SST files); 0 = unlimited.")
KB["rocksdb_max_background_jobs"] = dict(kind="int", dynamic=True, risk="none", cat="io",
                                         desc="MyRocks: background flush and compaction threads.")
KB["wsrep_applier_threads"] = dict(kind="int", dynamic=True, risk="none", cat="replication",
                                   desc="Galera: threads applying replicated write sets (wsrep_slave_threads before 8.0.26).")
KB["wsrep_slave_threads"] = dict(kind="int", dynamic=True, risk="none", cat="replication",
                                 desc="Galera: threads applying replicated write sets (older name of wsrep_applier_threads).")
KB["innodb_numa_interleave"] = dict(kind="bool", dynamic=False, risk="none", cat="memory",
                                    desc="Interleave buffer pool allocation across NUMA nodes.")
KB["large_pages"] = dict(kind="bool", dynamic=False, risk="low", cat="memory",
                         desc="Allocate the buffer pool from huge pages (needs vm.nr_hugepages and memlock).")
KB["binlog_cache_size"] = dict(kind="size", dynamic=True, risk="none", cat="memory",
                               desc="Per-session binlog cache.")


# ideas from docs/research/mysql-tuning-ideas.md
KB.setdefault("innodb_flush_sync", dict(kind="bool", dynamic=True, risk="none", cat="io",
              desc="ON: during sync flushing (checkpoint pressure) ignore innodb_io_capacity_max."))
KB.setdefault("innodb_max_purge_lag", dict(kind="int", dynamic=True, risk="none", cat="purge",
              desc="History list length above which DML is delayed so purge can catch up (0 = never)."))
KB.setdefault("innodb_max_purge_lag_delay", dict(kind="int", dynamic=True, risk="none", cat="purge",
              desc="Maximum delay in microseconds imposed by innodb_max_purge_lag."))
KB.setdefault("binlog_transaction_dependency_history_size", dict(kind="int", dynamic=True, risk="none", cat="binlog",
              desc="Rows kept in the WRITESET dependency history for parallel replication."))
TRADEOFFS["innodb_flush_method"] = (lambda v: str(v).upper() == "O_DIRECT_NO_FSYNC", "durability",
                                    "Skips fsync() after data file writes; safe only when the filesystem persists metadata on its own "
                                    "(XFS/ext4 with O_DIRECT); a crash on other setups can lose file-size changes.", "O_DIRECT_NO_FSYNC")


# operating-system pseudo-variables (see ostune.py)
from .ostune import OS_KB  # noqa: E402
for _k, (_desc, _restart, _cat) in OS_KB.items():
    KB[_k] = dict(kind="str", dynamic=not _restart, risk="durability" if _k in ("os:mount_barrier", "os:disk_write_cache") else "low",
                  cat="os/" + _cat, desc=_desc)
TRADEOFFS["os:mount_barrier"] = (lambda v: str(v).upper() == "OFF", "durability",
                                 "Without write barriers a power loss can lose or reorder writes the database already considered durable.",
                                 "OFF (ext4 nobarrier / barrier=0)")
TRADEOFFS["os:disk_write_cache"] = (lambda v: str(v).upper() == "ON", "durability",
                                    "Writes are acknowledged from the drive's volatile cache. Without power-loss protection a power cut loses "
                                    "what is still in it - committed transactions too when the drive acknowledges cache flushes early. "
                                    "Acceptable on a benchmark box, not in production unless the drive has power-loss protection.",
                                    "ON (volatile drive write cache)")


TRADEOFFS["skip_name_resolve"] = (lambda v: str(v).upper() in ("ON", "1"), "semantics",
                                  "Host-name based accounts (user@hostname) stop matching; only IP-based and wildcard grants work.", "ON")
KB["back_log"] = dict(kind="int", dynamic=False, risk="none", cat="connections", desc="Listen backlog of mysqld (capped by net.core.somaxconn).")
TRADEOFFS["os:datadir_fs"] = (lambda v: any(x in str(v) for x in ("barrier=0", "nobarrier", "data=writeback", "sync=disabled")),
                              "durability", "The filesystem may acknowledge writes before they are durable (crash can lose or corrupt recent data).",
                              "options barrier=0 / nobarrier / data=writeback / sync=disabled")


from .resgroups import RG_KB  # noqa: E402
from .psprofile import PS_KB  # noqa: E402
for _k, (_desc, _restart, _cat) in PS_KB.items():
    KB[_k] = dict(kind="str", dynamic=True, risk="observability", cat="mysql/" + _cat, desc=_desc)
TRADEOFFS["ps:profile"] = (lambda v: str(v) == "lite", "observability",
                           "performance_schema statement/transaction history and memory accounting are no longer collected "
                           "(digests, latency histogram and wait summaries stay).", "lite")
for _k, (_desc, _restart, _cat) in RG_KB.items():
    KB[_k] = dict(kind="str", dynamic=True, risk="low", cat="mysql/" + _cat, desc=_desc)


# client-host settings (applied through the client role); same semantics as the os:* entries
for _k, (_desc, _restart, _cat) in list(OS_KB.items()):
    KB["client:" + _k] = dict(kind="str", dynamic=True, risk="low", cat="client/" + _cat, desc="Client host: " + _desc)


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


# ---------------------------------------------------------------------------------------------- parameter catalog
_CATALOG = None


def catalog():
    """The shipped parameter catalog (knowledge_data/parameters.json, built by tools/build_parameter_catalog.py):
    impact (high / medium / low / unknown), when it counts, precondition, safety, restart, mechanism tags, rules."""
    global _CATALOG
    if _CATALOG is None:
        import json
        import os
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_data", "parameters.json")) as f:
                _CATALOG = json.load(f).get("parameters") or {}
        except (OSError, ValueError):
            _CATALOG = {}
    return _CATALOG


_SERVER_VARS = None


def server_variables():
    """The release inventory (knowledge_data/server_variables.json, tools/varinventory): per server variable the series
    that have it, default, runtime changeability, range - measured on the official images."""
    global _SERVER_VARS
    if _SERVER_VARS is None:
        import json
        import os
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_data", "server_variables.json")) as f:
                _SERVER_VARS = json.load(f)
        except (OSError, ValueError):
            _SERVER_VARS = {"series": {}, "variables": {}}
    return _SERVER_VARS


def series_of(flavor, version):
    """'percona 8.4' from a flavor and a version string (the inventory's and the server's evidence key)."""
    import re
    m = re.match(r"(\d+)\.(\d+)", str(version or ""))
    return f"{flavor} {m.group(1)}.{m.group(2)}" if flavor and m else None


# accepted by SET GLOBAL only for some transitions (innodb_doublewrite: ON <-> DETECT_ONLY online, OFF needs a restart):
# the knowledge base's flag stays in charge
PARTLY_DYNAMIC = {"innodb_doublewrite"}


def dynamic_in(variable, series):
    """Whether the variable can be changed at runtime in this release series, as measured (True / False), or None when
    the inventory does not cover the series or the variable (fall back to the knowledge base)."""
    inv = server_variables()
    v = (inv.get("variables") or {}).get(variable)
    if not v or series not in (inv.get("series") or {}) or "dynamic" not in v or variable in PARTLY_DYNAMIC:
        return None
    d = v["dynamic"]
    if isinstance(d, dict):
        return d.get(series)
    have = v.get("series")
    return d if have == "all" or series in (have or []) else None


IMPACT_SCORE = {"high": 1, "medium": 0, "low": -1, "none": -2, "unknown": 0}  # none: a limit or safety cap, not a performance knob


def effective_impact(variable, learned=None):
    """The impact the tuner works with: the learned one (hosted service, enough measured experiments in matching
    conditions) when present, else the shipped prior. Returns (impact, source)."""
    le = (learned or {}).get(variable)
    if le and le.get("impact") and le.get("impact") != "unknown":
        return le["impact"], "learned"
    return (catalog().get(variable) or {}).get("impact", "unknown"), "shipped"


CONTAINERS = {"optimizer_switch"}  # variables that are lists of independent flags: catalog, policy and learning per flag


def setting_names(variable, value, current=None):
    """The catalog names a change touches: the variable itself, or for a flag container (optimizer_switch) one name per
    flag the value sets differently from the current value ("optimizer_switch:subquery_to_derived")."""
    if variable not in CONTAINERS:
        return [variable]
    def flags(v):
        out = {}
        for part in str(v or "").split(","):
            k, _, x = part.strip().partition("=")
            if k:
                out[k.strip()] = x.strip().lower()
        return out
    new, cur = flags(value), flags(current)
    names = [f"{variable}:{k}" for k, x in new.items() if cur.get(k) != x]
    return names or [variable]


def run_tradeoffs(run):
    """What a measured configuration gives up: the static trade-offs of its settings plus a memory budget the host could
    not hold under load (recorded per run at measurement time)."""
    out = list(tradeoffs_in((run or {}).get("managed")))
    mb = ((run or {}).get("summary") or {}).get("memory_budget") or {}
    if mb.get("over_budget"):
        out.append({"variable": "memory", "value": mb.get("text", ""), "category": "memory",
                    "lost": "Production safety: memory budget exceeded - swapping / OOM under load."})
    return out


def start_tradeoffs(runs):
    """What the session's starting configuration already gives up (the first run's; e.g. a benchmark defined without a
    binary log, or a setup already over the memory budget). Every run inherits these: they say the benchmark setup is
    not production-safe, not what the tuner did."""
    first = min((r for r in runs or [] if r.get("managed") is not None), key=lambda r: r.get("seq") or 0, default=None)
    if not first:
        return []
    out = run_tradeoffs(first)
    for t in out:
        if t.get("category") == "memory":  # compared by size below, not by its text
            t["needed"] = (((first.get("summary") or {}).get("memory_budget") or {}).get("needed"))
    return out


MEM_SLACK = 0.01  # a memory trade-off is the tuner's only when the run needs this share of RAM more than the start


def added_tradeoffs(run, start):
    """The trade-offs of a run beyond those of the starting configuration: what the tuner's changes gave up. A start
    already over the memory budget (session 299: 62.8 of 62.8 GiB at 1100 connections) marks a run only when it needs
    more memory than the start did."""
    have = {(t.get("variable"), str(t.get("value"))) for t in start or [] if t.get("category") != "memory"}
    sm = next((t for t in start or [] if t.get("category") == "memory"), None)
    mb = ((run or {}).get("summary") or {}).get("memory_budget") or {}
    out = []
    for t in run_tradeoffs(run):
        if t.get("category") == "memory":
            if sm and sm.get("needed") and mb.get("needed") is not None and mb["needed"] <= sm["needed"] + MEM_SLACK * (mb.get("ram") or 0):
                continue
        elif (t.get("variable"), str(t.get("value"))) in have:
            continue
        out.append(t)
    return out


def response_shape(variable):
    """saturating | optimum | binary | choice | tradeoff | None (unknown) - from the parameter catalog."""
    if str(variable).startswith("optimizer_switch:"):
        return "binary"
    return ((catalog().get(variable) or {}).get("response") or {}).get("shape")
