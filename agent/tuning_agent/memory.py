"""Memory budget of a MySQL configuration: is it safe for production on this host?

Memory is one pool shared by every option. Some options are sizes (innodb_buffer_pool_size), some are counts with a
memory cost when used (table_open_cache, max_prepared_stmt_count), some are per connection (thread stack, network
buffers) and some per running query (sort, join, read buffers, in-memory temporary tables) - allocated only while a
statement needs them, sometimes more than once per statement.

Budgeting for the worst case (every one of max_connections connections holding every buffer, several times) gives
absurdly small settings; ignoring it gives configurations that swap or get OOM-killed under load. The model in between:

* global allocations at their configured size (buffer pool plus its control structures, log buffer, key buffer, the
  TempTable pool when it fills, performance_schema);
* count-based caches at their configured count times an estimated cost per entry;
* every allowed connection at its base cost (thread stack resident part, network buffers, session state);
* ACTIVE = 2 x CPU cores connections running at the same time, each holding all its per-query buffers once
  (sort + join + read + read_rnd + binlog cache + an in-memory temporary table) - the cores bound how many statements
  can really be using their buffers at once;
* mysqld's fixed overhead (code, dictionary, background threads), measured when a run exists.

Total plus the OS reserve (and a co-located client's reserve) must fit in RAM. Per-entry costs are estimates (see
COST); measured mysqld memory from runs (PSS + swap) is reported next to the model so the estimates can be checked.
"""
KiB, MiB, GiB = 1024, 1024 ** 2, 1024 ** 3

COST = {  # memory per entry / per connection (bytes), measured with tools/memprobe on tuner-vbm (PS 9.7, 2026-10-20)
    "bp_control": 0.08,              # mysqld = 0.57 GiB + 1.077 x pool (2G -> 2.72, 6G -> 7.03 GiB): control blocks, page hash, AHI
    "table_open_cache": 32 * KiB,    # opened table: ~49 KiB definition + instance together
    "table_definition_cache": 17 * KiB,
    "prepared_stmt": 25 * KiB,       # measured 25 KiB per (short) statement
    "connection_base": 160 * KiB,    # measured ~130 KiB per idle connection with performance_schema off
    "pfs_base": 240 * MiB,           # measured 1611 vs 1369 MiB idle, performance_schema on vs off
    "pfs_per_connection": 256 * KiB,  # measured 380-410 KiB per connection with it on vs ~130 off
    "active_exec": 4 * MiB,          # execution memory per running statement beyond the configured buffers: 1.5 MiB (sorts) - 9 MiB (joins)
    "mysqld_fixed": 350 * MiB,       # 0.57 GiB fixed at 1 GiB pool minus performance_schema
}
PER_QUERY = ("sort_buffer_size", "join_buffer_size", "read_buffer_size", "read_rnd_buffer_size", "binlog_cache_size")


def _n(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        try:
            from .knowledge import parse_size
            return int(parse_size(str(v)))
        except Exception:
            return d


def host_other_bytes(summary, ram):
    """Memory the host uses outside mysqld, measured in a run: RAM minus what the kernel calls available minus mysqld's
    resident memory (page cache counts as available). Kernel or driver allocations no counter names show up here
    (tuner-vbm-amd: ~6.6 GB of 30 GB, so a 24 GB buffer pool was OOM-killed at startup with a budget that allowed it)."""
    s = summary or {}
    if not ram or s.get("mem_available_mb") is None or s.get("dbproc_rss_mb") is None:
        return None
    return max(0, int(ram - (s["mem_available_mb"] + s["dbproc_rss_mb"]) * 2 ** 20))


def budget(variables, hw, reserve_bytes=0, active_per_cpu=2, measured=None, host_other=None):
    """{"items": [{name, bytes, basis}], "total", "ram", "headroom", "over_budget", "active_connections", ...}"""
    v = variables or {}
    ram = int((hw or {}).get("ram_bytes") or 0)
    cpus = int((hw or {}).get("cpus") or 1)
    max_conn = _n(v.get("max_connections"), 151)
    active = min(max_conn, active_per_cpu * cpus)
    items = []

    def add(name, b, basis):
        if b:
            items.append({"name": name, "bytes": int(b), "basis": basis})

    bp = _n(v.get("innodb_buffer_pool_size"))
    add("InnoDB buffer pool", bp, "innodb_buffer_pool_size")
    add("Buffer pool control blocks / page hash / AHI", bp * COST["bp_control"], "~8% of the buffer pool (measured)")
    add("Redo log buffer", _n(v.get("innodb_log_buffer_size")), "innodb_log_buffer_size")
    add("Key buffer (MyISAM)", _n(v.get("key_buffer_size")), "key_buffer_size")
    tmp_each = min(_n(v.get("tmp_table_size"), 16 * MiB), _n(v.get("max_heap_table_size"), 16 * MiB))
    tt = _n(v.get("temptable_max_ram"), GiB)
    add("In-memory temporary tables", min(tt, active * tmp_each) if tt else active * tmp_each,
        f"min(temptable_max_ram, {active} active x min(tmp_table_size, max_heap_table_size))")
    if str(v.get("performance_schema", "ON")).upper() in ("ON", "1"):
        add("performance_schema", COST["pfs_base"] + max_conn * COST["pfs_per_connection"], f"~{COST['pfs_base'] // MiB} MiB + per connection")
    add("Table cache", _n(v.get("table_open_cache"), 4000) * COST["table_open_cache"], f"table_open_cache x ~{COST['table_open_cache'] // KiB} KiB")
    add("Table definition cache", _n(v.get("table_definition_cache"), 2000) * COST["table_definition_cache"],
        f"table_definition_cache x ~{COST['table_definition_cache'] // KiB} KiB")
    # prepared statements: the cap is a limit, not an allocation - count what connections can realistically hold:
    # ~20 statements per allowed connection, up to the cap
    ps = min(_n(v.get("max_prepared_stmt_count"), 16382), max_conn * 20)
    add("Prepared statements", ps * COST["prepared_stmt"], f"min(max_prepared_stmt_count, {max_conn} connections x 20) x ~{COST['prepared_stmt'] // KiB} KiB")
    add("Connections (base)", max_conn * COST["connection_base"], f"max_connections {max_conn} x ~{COST['connection_base'] // KiB} KiB")
    per_q = sum(_n(v.get(k)) for k in PER_QUERY)
    # sort and join buffers are allocated as needed (measured: the same ~1.5 MiB per sorting connection at 256K and
    # 64M sort_buffer_size): their configured sizes are an upper bound, counted once per active connection
    add("Per-query buffers of active connections", active * per_q,
        f"{active} active (2 x {cpus} CPUs, at most max_connections) x (" + " + ".join(PER_QUERY) + "), an upper bound")
    add("Execution memory of active statements", active * COST["active_exec"], f"{active} active x ~{COST['active_exec'] // MiB} MiB")
    # MyRocks shares the same memory: its block cache and memtables (only when the engine is loaded)
    if "rocksdb_block_cache_size" in v:
        add("MyRocks block cache", _n(v.get("rocksdb_block_cache_size")), "rocksdb_block_cache_size")
        add("MyRocks memtables", _n(v.get("rocksdb_db_write_buffer_size")) or 0, "rocksdb_db_write_buffer_size (0 = unlimited: not counted)")
    add("mysqld fixed overhead", COST["mysqld_fixed"], "code, data dictionary, background threads")
    # the OS reserve: 10% of RAM, or what the host measurably uses outside mysqld when that is more (a co-located
    # client is in that measurement and in reserve_bytes: counted once)
    os_res = max(GiB, int(ram * 0.10), int((host_other or 0) - int(reserve_bytes or 0)))
    total = sum(i["bytes"] for i in items)
    need = total + os_res + int(reserve_bytes or 0)
    out = {"items": items, "mysqld_bytes": total, "os_reserve": os_res, "client_reserve": int(reserve_bytes or 0), "ram": ram,
           "needed": need, "headroom": ram - need, "over_budget": bool(ram) and need > ram, "active_connections": active,
           "max_connections": max_conn}
    if measured:
        out["measured_mysqld_bytes"] = int(measured)
    return out


MEMORY_VARS = {"innodb_buffer_pool_size", "innodb_log_buffer_size", "key_buffer_size", "tmp_table_size", "max_heap_table_size",
               "temptable_max_ram", "performance_schema", "table_open_cache", "table_definition_cache", "max_prepared_stmt_count",
               "max_connections", "rocksdb_block_cache_size", "rocksdb_db_write_buffer_size", *PER_QUERY}


def text(b):
    """One line: what the configuration needs vs the host."""
    return (f"{b['needed'] / GiB:.1f} GiB needed ({b['mysqld_bytes'] / GiB:.1f} GiB mysqld"
            + (f" incl. {b['measured_offset'] / GiB:.1f} GiB measured above the model" if b.get("measured_offset") else "")
            + f" at {b['active_connections']} active of "
            f"{b['max_connections']} connections + {b['os_reserve'] / GiB:.1f} GiB OS"
            + (f" + {b['client_reserve'] / GiB:.1f} GiB client" if b["client_reserve"] else "") + f") of {b['ram'] / GiB:.1f} GiB RAM")
