"""Baseline configuration derived from hardware and server version only."""
from .knowledge import fmt_size, human_size

GiB = 1024 ** 3
MiB = 1024 ** 2


def _round_down(n, unit):
    return max(unit, (int(n) // unit) * unit)


def generate(hw, variables, flavor, version, opts=None):
    """Return list of {variable, value, reason}. Only variables the server knows are included."""
    opts = opts or {}
    ram = int(hw["ram_bytes"])
    cpus = int(hw["cpus"] or 1)
    ssd = not hw.get("disk_rotational", False)
    max_conn = int(opts.get("max_connections") or max(151, min(1000, cpus * 50)))
    reserve = int(opts.get("reserve_bytes") or max(GiB, int(ram * 0.10)))
    out = []

    def add(var, value, reason):
        if var in variables:
            out.append({"variable": var, "value": value, "reason": reason})

    # -- buffer pool: what is left after OS + server overheads, capped at 75% of RAM
    overhead = 400 * MiB + max_conn * 2 * MiB // 3
    bp = min(int(ram * 0.75), ram - reserve - overhead)
    bp = max(bp, 128 * MiB)
    instances = 1
    if "innodb_buffer_pool_instances" in variables and flavor != "mariadb":
        instances = 8 if bp >= 8 * GiB else max(1, bp // GiB)
        add("innodb_buffer_pool_instances", str(instances),
            f"{instances} instance(s) for a {human_size(bp)} pool (~1 GiB per instance, max 8) to spread mutex contention.")
    chunk = int(variables.get("innodb_buffer_pool_chunk_size") or 128 * MiB)
    bp = _round_down(bp, chunk * instances)
    add("innodb_buffer_pool_size", fmt_size(bp),
        f"{human_size(ram)} RAM - {human_size(reserve)} OS reserve - {human_size(overhead)} server/connection overhead "
        f"(capped at 75% of RAM), rounded to chunk*instances.")

    # -- redo log
    redo = min(16 * GiB, max(GiB, ram // 4))
    free = hw.get("disk_free_bytes") or 0
    if free:
        redo = min(redo, max(512 * MiB, int(free * 0.05)))
    redo = _round_down(redo, 512 * MiB) if redo >= GiB else redo
    if "innodb_redo_log_capacity" in variables:
        add("innodb_redo_log_capacity", fmt_size(redo),
            "Redo sized at ~25% of RAM (1-16 GiB) so checkpoints are not forced under write bursts.")
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
    io_threads = 4 if cpus <= 8 else min(16, cpus // 2)
    add("innodb_read_io_threads", str(io_threads), f"{io_threads} read I/O threads for {cpus} CPUs.")
    add("innodb_write_io_threads", str(io_threads), f"{io_threads} write I/O threads for {cpus} CPUs.")

    # -- durability stays safe in the baseline
    add("innodb_flush_log_at_trx_commit", "1", "Full durability: fsync redo at each commit.")
    if variables.get("log_bin") == "ON":
        add("sync_binlog", "1", "Full durability for the binary log.")

    # -- connections / caches
    add("max_connections", str(max_conn), f"{max_conn} connections for {cpus} CPUs (override with --max-connections).")
    tmp = "64M" if ram >= 8 * GiB else "32M"
    add("tmp_table_size", tmp, "In-memory temporary table limit scaled to RAM.")
    add("max_heap_table_size", tmp, "Matches tmp_table_size (the lower of the two applies).")
    if int(variables.get("table_open_cache", 0) or 0) < 4000:
        add("table_open_cache", "4000", "Enough open table handles for typical schemas.")
    add("performance_schema", "ON", "Required for statement latency measurement.")
    return out, {"buffer_pool": bp, "redo": redo, "max_connections": max_conn, "reserve": reserve}
