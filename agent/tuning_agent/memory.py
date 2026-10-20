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

COST = {  # estimated memory per entry / per connection (bytes); sources in docs/design/memory-budget.md
    "bp_control": 0.03,              # buffer pool control blocks, page hash, AHI: ~3% on top of innodb_buffer_pool_size
    "table_open_cache": 8 * KiB,     # per open table instance (handler, InnoDB prebuilt)
    "table_definition_cache": 6 * KiB,
    "prepared_stmt": 16 * KiB,       # per prepared statement (parse tree, metadata); grows with statement size
    "connection_base": 768 * KiB,    # thread stack resident part, THD, network buffers (net_buffer_length x 2), session caches
    "pfs_base": 192 * MiB,           # performance_schema fixed part (instruments, digests, history), when ON
    "pfs_per_connection": 128 * KiB,
    "mysqld_fixed": 400 * MiB,       # code, data dictionary, background threads
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


def budget(variables, hw, reserve_bytes=0, active_per_cpu=2, measured=None):
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
    add("Buffer pool control blocks / page hash / AHI", bp * COST["bp_control"], "~3% of the buffer pool")
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
    add("Per-query buffers of active connections", active * per_q,
        f"{active} active (2 x {cpus} CPUs, at most max_connections) x (" + " + ".join(PER_QUERY) + ")")
    add("mysqld fixed overhead", COST["mysqld_fixed"], "code, data dictionary, background threads")
    os_res = max(GiB, int(ram * 0.10))
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
               "max_connections", *PER_QUERY}


def text(b):
    """One line: what the configuration needs vs the host."""
    return (f"{b['needed'] / GiB:.1f} GiB needed ({b['mysqld_bytes'] / GiB:.1f} GiB mysqld at {b['active_connections']} active of "
            f"{b['max_connections']} connections + {b['os_reserve'] / GiB:.1f} GiB OS"
            + (f" + {b['client_reserve'] / GiB:.1f} GiB client" if b["client_reserve"] else "") + f") of {b['ram'] / GiB:.1f} GiB RAM")
