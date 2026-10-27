"""Baseline configuration derived from hardware and server version only."""
from .knowledge import fmt_size, human_size

GiB = 1024 ** 3
MiB = 1024 ** 2


def _round_down(n, unit):
    return max(unit, (int(n) // unit) * unit)


def _tmp_size(ram):
    return 64 * MiB if ram >= 8 * GiB else 32 * MiB


def generate(hw, variables, flavor, version, opts=None):
    """Return list of {variable, value, reason}. Only variables the server knows are included."""
    opts = opts or {}
    ram = int(hw["ram_bytes"])
    cpus = int(hw["cpus"] or 1)
    ssd = not hw.get("disk_rotational", False)
    max_conn = int(opts.get("max_connections") or max(151, min(1000, cpus * 50)))
    if not opts.get("max_connections"):
        # never below what the server runs now: a benchmark prerequisite raised it (session 381: 2048 -> 1000 under 1024
        # reconnecting threads)
        max_conn = max(max_conn, int(variables.get("max_connections") or 0))
    # OS reserve, plus memory for a co-located client when one is declared (--reserve-mem): added, not instead
    client = int(opts.get("reserve_bytes") or 0)
    # the OS reserve: 10% of RAM, or what the host measurably uses outside mysqld when more (a 24 GB pool on a 30 GB host
    # with 6.6 GB held by the kernel swapped and was OOM-killed at startup: tuner-vbm-amd, 2026-10-21/22)
    reserve = max(GiB, int(ram * 0.10), int(opts.get("host_other_bytes") or 0)) + client
    out = []

    def add(var, value, reason):
        if var in variables:
            out.append({"variable": var, "value": value, "reason": reason})

    # -- buffer pool: what is left after the OS, a co-located client and everything else mysqld needs at this
    # configuration (memory.budget: caches, connections, per-query buffers of 2 x CPUs active connections,
    # performance_schema, fixed overhead), capped at 75% of RAM
    from . import memory
    planned = dict(variables, max_connections=str(max_conn), innodb_buffer_pool_size="0",
                   tmp_table_size=str(_tmp_size(ram)), max_heap_table_size=str(_tmp_size(ram)))
    nb = memory.budget(planned, hw)
    overhead = nb["mysqld_bytes"]
    measured = int(opts.get("measured_overhead_bytes") or 0)
    if measured > overhead:  # mysqld's own memory beyond the buffer pool in the last run (PSS + swap) beats the estimate
        overhead = measured
    # 2% of RAM left unplanned: sized to exactly 100%, the measured runs (active connections, mysqld above the model,
    # memory outside mysqld) put the baseline itself over the budget, so every run became a memory trade-off and the best
    # production-safe configuration fell back to server defaults (h2h W3 on AMD: 0.7 GiB over)
    bp = min(int(ram * 0.75), int((ram * 0.98 - reserve - overhead) / (1 + memory.COST["bp_control"])))
    starved = None
    cur_bp = int(variables.get("innodb_buffer_pool_size") or 0)
    if bp < GiB and cur_bp > max(bp, 128 * MiB):
        # the reserve and the memory the host already uses leave (almost) nothing: never shrink the pool to the minimum
        # silently (session 380: 270 GB RAM, an 80 GB RAM disk already counted as host memory, plus --reserve-mem 204G ->
        # 128 MB) - keep the current size and say why
        starved = {"computed_bytes": max(bp, 0), "kept_bytes": cur_bp, "ram": ram, "host_other": int(opts.get("host_other_bytes") or 0),
                   "ramdisk": int(opts.get("ramdisk_bytes") or 0), "client_reserve": client}
        bp = cur_bp
    bp = max(bp, 128 * MiB)
    # MyRocks holding data shares the memory: its block cache gets the RocksDB share of the data (at least 256 MiB each)
    data = ((opts.get("engines") or {}).get("data")) or {}
    rocks, inno = data.get("rocksdb", 0), data.get("innodb", 0)
    if rocks and "rocksdb_block_cache_size" in variables and rocks + inno:
        share = rocks / (rocks + inno)
        cache = max(256 * MiB, int(bp * share) // MiB * MiB)
        bp = max(256 * MiB, bp - cache)
        add("rocksdb_block_cache_size", fmt_size(cache), f"MyRocks holds {100 * share:.0f}% of the data ({human_size(rocks)}): its block cache gets "
            "that share of the memory the buffer pool would otherwise use.")
    instances = 1
    from . import numa
    topo = numa.topology()
    nodes = topo["nodes"]
    if "innodb_buffer_pool_instances" in variables and flavor != "mariadb":
        instances = 8 if bp >= 8 * GiB else max(1, bp // GiB)
        why = f"{instances} instance(s) for a {human_size(bp)} pool (~1 GiB per instance, max 8) to spread mutex contention."
        if nodes > 1 and bp >= GiB and instances % nodes:
            instances = max(nodes, -(-instances // nodes) * nodes)  # a multiple of the NUMA nodes: partitions spread evenly
            why += f" Rounded to a multiple of the {nodes} NUMA nodes."
        add("innodb_buffer_pool_instances", str(instances), why)
    node_mem = min((x["mem_bytes"] for x in topo["per_node"] if x["mem_bytes"]), default=0)
    if nodes > 1 and node_mem and bp > 0.8 * node_mem and "innodb_numa_interleave" in variables \
            and str(variables.get("innodb_numa_interleave")).upper() not in ("ON", "1"):
        # a pool larger than one node cannot be local anyway: allocated from one node it fills that node first
        add("innodb_numa_interleave", "ON", f"The {human_size(bp)} buffer pool is larger than one of the {nodes} NUMA nodes "
            f"({human_size(node_mem)}): interleaved across all nodes, no node runs out first.")
    chunk = int(variables.get("innodb_buffer_pool_chunk_size") or 0) or 128 * MiB  # MariaDB 10.11+ reports 0 (auto)
    bp = _round_down(bp, chunk * instances)
    rd = int(opts.get("ramdisk_bytes") or 0)
    if not starved:
        add("innodb_buffer_pool_size", fmt_size(bp),
            f"{human_size(ram)} RAM - {human_size(reserve)} OS{' + client' if client else ''} reserve"
            + (f" (includes {human_size(rd)} held by RAM disks)" if rd else "") + f" - {human_size(overhead)} server/connection overhead"
            f"{' (measured in the last run)' if measured and overhead == measured else ''} "
            f"(capped at 75% of RAM), rounded to chunk*instances.")

    # -- redo log
    redo = min(16 * GiB, max(GiB, ram // 4))
    free = hw.get("disk_free_bytes") or 0
    if free:
        redo = min(redo, max(512 * MiB, int(free * 0.05)))
    # whole MiB at least: the server rounds innodb_redo_log_capacity down, and a run then measures a configuration other than
    # the one it is labelled with (r2 RO-4: 993309900 requested, 993001472 running -> the first calibration run unreliable)
    redo = _round_down(redo, 512 * MiB) if redo >= GiB else _round_down(redo, MiB)
    if "innodb_redo_log_capacity" in variables:
        add("innodb_redo_log_capacity", fmt_size(redo),
            "Redo sized at ~25% of RAM (1-16 GiB) so checkpoints are not forced under write bursts. Cost: crash recovery "
            f"applies up to {fmt_size(redo)} of redo after a crash, so it takes longer than with a small redo log.")
    elif flavor == "mariadb":
        add("innodb_log_file_size", fmt_size(redo), "Single MariaDB redo log sized at ~25% of RAM (1-16 GiB).")
    else:
        files = int(variables.get("innodb_log_files_in_group") or 2)
        add("innodb_log_file_size", fmt_size(redo // files),
            f"{files} redo files totalling ~25% of RAM (1-16 GiB).")
    add("innodb_log_buffer_size", "64M", "64 MiB redo buffer avoids log waits on large transactions.")

    # -- I/O
    if ssd:
        add("innodb_io_capacity", "2000", "SSD/cloud block storage: ~2000 IOPS of background flushing (MySQL 8.4's default 10000 assumes high-end local NVMe and can flush far more than needed).")
        add("innodb_io_capacity_max", "4000", "Twice innodb_io_capacity for catch-up flushing.")
        add("innodb_flush_neighbors", "0", "Neighbour flushing only helps rotating disks.")
    else:
        add("innodb_io_capacity", "200", "Rotating disk: modest background flushing budget.")
        add("innodb_io_capacity_max", "400", "Twice innodb_io_capacity.")
        add("innodb_flush_neighbors", "1", "Group neighbour page writes on rotating disk.")
    if "innodb_flush_method" in variables and variables.get("innodb_flush_method") not in ("O_DIRECT", "O_DIRECT_NO_FSYNC"):
        add("innodb_flush_method", "O_DIRECT", "Bypass the OS page cache for data files (no double buffering).")
    add("innodb_use_fdatasync", "ON", "fdatasync() skips needless metadata flushes.")
    # no I/O thread counts: 11 read-thread sweeps (2-16, sessions 150-294) gained nothing, write threads were never shown to
    # matter - a setting without a measured benefit stays at the server's default (user, 2026-10-27). The engine sweeps them
    # where the evidence points at them (read-ahead at work, flushing falling behind)

    # -- durability stays safe in the baseline
    add("innodb_flush_log_at_trx_commit", "1", "Full durability: fsync redo at each commit.")
    if variables.get("log_bin") == "ON":
        add("sync_binlog", "1", "Full durability for the binary log.")

    # -- connections / caches
    add("max_connections", str(max_conn), f"{max_conn} connections for {cpus} CPUs (override with --max-connections).")
    tmp = fmt_size(_tmp_size(ram))
    add("tmp_table_size", tmp, "In-memory temporary table limit scaled to RAM.")
    add("max_heap_table_size", tmp, "Matches tmp_table_size (the lower of the two applies).")
    if int(variables.get("table_open_cache", 0) or 0) < 4000:
        add("table_open_cache", "4000", "Enough open table handles for typical schemas.")
    add("performance_schema", "ON", "Required for statement latency measurement.")
    return out, {"buffer_pool": bp, "redo": redo, "max_connections": max_conn, "reserve": reserve, "bp_starved": starved,
                 "ramdisk": int(opts.get("ramdisk_bytes") or 0)}
