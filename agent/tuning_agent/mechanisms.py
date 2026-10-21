"""What each recommendation physically changes and which bottlenecks it can relieve.

Used to rank experiments: a change whose mechanism matches the diagnosed bottleneck is tried first; a change that
cannot touch the bottleneck is tried last (it may still help, but it is unlikely to). Bottleneck names are the
diagnosis types from engine.diagnose().
"""

MECHANISMS = {
    "bp-grow": (("memory", "io", "io-read"), "More pages cached: fewer disk reads."),
    "bp-shrink": (("swap", "memory-pressure", "oom"), "Gives memory back to the OS: no swapping/reclaim stalls."),
    "bp-instances": (("concurrency", "cpu-spin", "cpu-bufpool", "cpu-pressure"), "Splits buffer pool mutexes."),
    "redo-grow": (("redo", "io", "io-flush"), "Fewer forced checkpoints; pages absorb more changes before being written."),
    "log-buffer": (("redo", "commit"), "Fewer waits for redo log buffer space."),
    "io-capacity-up": (("redo", "io", "io-flush"), "Page cleaners flush harder: keeps redo free."),
    "io-capacity-down": (("io", "io-read"), "Fewer background writes competing with foreground I/O."),
    "lru-depth": (("io", "io-flush"), "More free pages ready: no single-page flushes in user threads."),
    "flush-neighbors": (("io", "io-flush"), "No extra neighbour page writes."),
    "o-direct": (("io", "memory", "io-read"), "No double buffering in the OS page cache."),
    "ahi-on": (("cpu-btree", "cpu-pressure"), "Hash lookups skip B-tree descents for hot point reads."),
    "ahi-off": (("concurrency", "cpu-spin"), "Removes AHI latch contention."),
    "log-writer-threads-off": (("commit", "cpu-commit", "sched-delay", "cpu-pressure"), "No dedicated log threads competing for few CPUs."),
    "spin-loops": (("cpu-spin", "cpu-pressure", "concurrency", "sched-delay"), "Less CPU burned spinning on contended latches."),
    "thread-pool": (("concurrency", "cpu-pressure", "sched-delay", "cpu-futex", "cpu-spin"), "Caps active threads: less context switching."),
    "table-open-cache": (("cpu-tablecache", "cpu-exec", "locks"), "Avoids reopening tables."),
    "table-def-cache": (("cpu-tablecache", "cpu-exec"), "Avoids re-reading table definitions."),
    "thread-cache": (("connections", "cpu-mm"), "Avoids thread creation per connection."),
    "max-connections": ((), "Admits more clients."),
    "tmp-tables": (("io", "cpu"), "Temporary tables stay in memory."),
    "tmp-olap": (("io", "cpu"), "Analytical temporary tables stay in memory."),
    "sort-buffer": (("io", "cpu"), "Sorts finish in memory."),
    "sort-buffer-olap": (("io", "cpu"), "Large analytical sorts finish in memory."),
    "join-buffer": (("io", "cpu"), "Hash joins build in memory instead of spilling."),
    "sweep-read-ahead": (("io", "io-read", "memory"), "Larger, earlier sequential reads for scans; fewer synchronous page reads."),
    "random-read-ahead": (("io", "io-read"), "Prefetches the rest of partly cached extents."),
    "read-ahead-less": (("io", "io-read", "memory"), "No wasted prefetch I/O or buffer pool churn."),
    "read-ahead": (("io", "io-read"), "Larger sequential reads for scans."),
    "subquery-to-derived": (("cpu-exec", "cpu-btree", "io-read"), "Correlated subqueries computed once instead of per row."),
    "bka-mrr": (("io", "io-read"), "Index lookups fetched in primary-key order."),
    "group-commit-delay": (("commit",), "More transactions share one binlog fsync."),
    "change-buffer-off": (("io-flush", "cpu-bufpool", "concurrency"), "No change-buffer merges competing with foreground work or taking buffer pool pages."),
    "change-buffer": (("io", "io-read"), "Secondary index updates without reading the page."),
    "trx-commit-2": (("commit", "io"), "No fsync per commit."),
    "sweep-log-write-ahead": (("commit", "redo"), "Redo writes aligned with the device block: no read-modify-write per flush."),
    "commit-storage-latency": (("commit", "io"), "Faster or cached storage writes: each commit waits less for its flush."),
    "log-writer-threads-on": (("commit",), "Dedicated log threads gather the waiting commits into one write and flush."),
    "sync-binlog": (("commit", "io"), "No binlog fsync per commit group."),
    "doublewrite-off": (("io", "commit", "io-flush"), "Pages written once instead of twice."),
    "binlog-off": (("commit", "io", "cpu"), "No binary log writes, fsyncs or two-phase commit."),
    "ps-off": (("cpu-pfs", "cpu-pressure"), "No instrumentation overhead."),
    "binlog-minimal": (("io",), "Smaller binary log writes for updates (helps only when binlog I/O volume matters)."),
    "os-swappiness": (("swap", "memory-pressure"), "Kernel keeps database memory resident."),
    "os-thp-off": (("memory", "cpu-mm"), "No compaction / khugepaged stalls."),
    "os-scheduler": (("io",), "No request reordering overhead on SSD."),
    "os-noatime": (("io",), "No access-time metadata writes."),
    "os-dirty-writeback": (("commit", "io"), "Small, steady writeback: shorter fsyncs."),
    "os-jemalloc": (("cpu-allocator", "cpu-pressure", "concurrency"), "Less allocator lock contention."),
    "os-hugepages": (("cpu", "memory"), "Fewer TLB misses."),
    "os-governor": (("cpu",), "Full clock speed."),
    "os-numa": (("numa", "cpu", "memory"), "No cross-node page migration."),
    "sweep-applier-threads": (("flow-control",), "Write sets applied in parallel: applier queues drain, flow control pauses less."),
    "rocksdb-cache-grow": (("rocksdb-cache", "io-read", "memory"), "More MyRocks blocks cached: fewer reads from SST files."),
    "os-zone-reclaim-off": (("numa", "memory", "swap"), "A full node allocates from other nodes instead of reclaiming / swapping."),
    "numa-interleave": (("numa", "memory", "io-read"), "Buffer pool spread over all nodes: no node runs out first."),
    "numa-interleave-all": (("numa", "memory"), "All of mysqld's memory spread over all nodes."),
    "numa-single-node": (("numa", "cpu", "memory"), "Only local memory accesses."),
    "os-autogroup": (("cpu-pressure", "sched-delay"), "mysqld threads compete for CPU individually, not as one group."),
    "os-aio": (("io",), "Native async I/O stays available."),
    "ps-lite": (("cpu-pfs", "cpu-pressure"), "Less instrumentation work per statement and allocation."),
    "sweep-dirty-lwm": (("io", "redo", "io-flush"), "Dirty pages absorb more updates before being written, or flushing starts earlier."),
    "sweep-lru-depth": (("io", "io-flush"), "Free pages ready for user threads (no single-page flushes)."),
    "sweep-read-io-threads": (("io", "memory", "io-read"), "More asynchronous reads in flight."),
    "sweep-purge-threads": (("locks", "io"), "Purge keeps up with undo history."),
    "flush-sync-off": (("io", "redo", "io-flush", "io-read"), "Reads are not starved during sync flushing."),
    "sweep-bp-instances": (("concurrency", "cpu"), "Less buffer pool mutex contention."),
    "purge-lag": (("locks", "io", "memory"), "Bounded history: shorter version chains for readers."),
    "sweep-spin-wait-delay": (("cpu-spin", "concurrency"), "Less CPU burned spinning, or faster wake-ups."),
    "writeset-history": (("cpu-commit", "commit"), "Less hashing work per commit."),
    "o-direct-no-fsync": (("commit", "io"), "One fsync fewer per data file write batch."),
    "bp-overcommit": (("memory", "io"), "Cold memory goes to swap; the larger cache saves more reads than swap costs."),
    "os-irq-split": (("softirq", "sched-delay", "network-rtt"), "Interrupts no longer preempt query threads."),
    "rg-innodb-bg": (("sched-delay",), "Background flushing no longer competes with query threads for CPU."),
    "fs-xfs": (("io", "commit"), "Different allocation/journaling behaviour for data and redo files."),
    "fs-ext4": (("io", "commit"), "Fewer journal commits."),
    "fs-zfs": (("io", "commit", "memory"), "Atomic writes, compression, different caching."),
    "fs-btrfs": (("io",), "Extent-based allocation without copy-on-write."),
    "zfs-doublewrite-off": (("io", "commit"), "Pages written once instead of twice."),
    "malloc-jemalloc-tuned": (("cpu-allocator", "memory", "concurrency"), "Allocator variant."),
    "malloc-tcmalloc": (("cpu-allocator", "concurrency"), "Allocator variant."),
    "malloc-mimalloc": (("cpu-allocator", "concurrency"), "Allocator variant."),
    "malloc-glibc-tuned": (("memory", "cpu-allocator"), "Allocator variant."),
    "sweep-bp": (("memory", "io"), "More pages cached: fewer disk reads."),
    "sweep-redo": (("redo", "io", "io-flush"), "Fewer forced checkpoints."),
    "sweep-io-capacity": (("io", "redo", "io-flush"), "Background flushing matched to the storage."),
    "sweep-thread-concurrency": (("concurrency", "cpu-pressure"), "Caps threads inside InnoDB."),
    "net-listen-backlog": (("connections",), "Bursts of connects queue instead of being dropped."),
    "net-syn-backlog": (("connections",), "Half-open connections queue instead of being dropped."),
    "skip-name-resolve": (("dns",), "No reverse DNS lookup per connection."),
    "net-netdev-backlog": (("softirq", "network-loss"), "Fewer packets dropped before protocol processing."),
    "net-netdev-budget": (("softirq",), "More packets per softirq round."),
    "net-rps": (("softirq", "cpu-pressure"), "Packet processing spread over all CPUs."),
    "net-conntrack": (("connections", "network-loss"), "No drops from a full conntrack table."),
    "net-no-slow-start-idle": (("network-rtt",), "Idle pooled connections keep their congestion window."),
    "net-bbr": (("network-rtt", "network-loss"), "Bandwidth/RTT-based pacing on long paths."),
    "client-ports": (("connections",), "Client keeps finding free ephemeral ports."),
    "client-no-slow-start-idle": (("network-rtt",), "Client connections keep their congestion window."),
    "client-bbr": (("network-rtt", "network-loss"), "Client pacing on long paths."),
    "client-rps": (("softirq", "client-bound"), "Client packet processing spread over CPUs."),
    "os-nobarrier": (("commit", "io"), "fsync does not flush the device write cache."),
}

SEVERITY = {"high": 3, "medium": 2, "low": 1}


# What a change needs to act on at all. Unlike the bottleneck match (a prior that only orders experiments), these are
# physical preconditions: flushing / redo / commit / purge settings cannot change anything when the workload writes
# nothing (e.g. TPC-H), read-path settings nothing without disk reads. Such experiments move to the very end as
# validation-only checks (one point, no sweep).
WRITES = {"redo-grow", "log-buffer", "io-capacity-up", "io-capacity-down", "flush-neighbors", "log-writer-threads-off",
          "group-commit-delay", "change-buffer-off", "change-buffer", "trx-commit-2", "sync-binlog", "doublewrite-off",
          "binlog-off", "binlog-minimal", "os-dirty-writeback", "sweep-dirty-lwm", "sweep-purge-threads", "flush-sync-off",
          "purge-lag", "writeset-history", "o-direct-no-fsync", "zfs-doublewrite-off", "sweep-redo", "sweep-io-capacity",
          "os-nobarrier"}
READS = {"read-ahead", "sweep-read-io-threads", "sweep-read-ahead", "random-read-ahead", "read-ahead-less"}
ANY_IO = {"bp-grow", "bp-overcommit", "sweep-bp", "o-direct", "os-scheduler"}


def activity(summary):
    s = summary or {}
    writes = (s.get("redo_mbs") or 0) >= 0.05 or (s.get("bp_pages_flushed") or 0) >= 5
    reads = (s.get("bp_disk_reads") or 0) >= 10
    return {"writes": writes, "reads": reads}


# The same preconditions per variable, so a change from any source (rule, LLM, sweep) is checked by what it sets, not by
# the name of the rule that proposed it. "dns": acts only on time spent resolving client host names, measured by the eBPF
# probes - no measurement means no evidence (not "probably fine").
_W = ("innodb_flush_log_at_trx_commit", "sync_binlog", "innodb_io_capacity", "innodb_io_capacity_max", "innodb_log_file_size",
      "innodb_redo_log_capacity", "innodb_log_files_in_group", "innodb_max_dirty_pages_pct", "innodb_max_dirty_pages_pct_lwm",
      "innodb_doublewrite", "innodb_flush_neighbors", "innodb_change_buffering", "innodb_change_buffer_max_size",
      "innodb_purge_threads", "innodb_max_purge_lag", "innodb_max_purge_lag_delay", "innodb_log_buffer_size",
      "innodb_adaptive_flushing", "innodb_adaptive_flushing_lwm", "innodb_flush_sync", "innodb_log_writer_threads",
      "innodb_page_cleaners", "innodb_write_io_threads", "binlog_group_commit_sync_delay", "binlog_row_image",
      "binlog_transaction_dependency_history_size", "innodb_flushing_avg_loops", "innodb_log_spin_cpu_abs_lwm",
      "innodb_log_spin_cpu_pct_hwm", "innodb_log_wait_for_flush_spin_hwm", "os:vm.dirty_bytes", "os:vm.dirty_background_bytes",
      "os:vm.dirty_ratio", "os:vm.dirty_background_ratio", "os:vm.dirty_expire_centisecs", "os:vm.dirty_writeback_centisecs")
# variables whose mechanism acts only on an event a status counter counts: if the counter did not move, nothing to act on
VAR_COUNTERS = {"thread_cache_size": ("Threads_created", "thread creation"), "table_open_cache": ("Opened_tables", "table opening"),
                "table_open_cache_instances": ("Opened_tables", "table opening"),
                "table_definition_cache": ("Opened_table_definitions", "table definition loading"),
                "tmp_table_size": ("Created_tmp_disk_tables", "on-disk temporary tables"),
                "max_heap_table_size": ("Created_tmp_disk_tables", "on-disk temporary tables"),
                "sort_buffer_size": ("Sort_merge_passes", "sort merge passes"),
                "os:net.ipv4.tcp_max_syn_backlog": ("_syn_drops", "SYN queue drops"), "back_log": ("_listen_drops", "accept queue overflows"),
                "os:net.core.somaxconn": ("_listen_drops", "accept queue overflows")}
VAR_NEEDS = dict(
    {v: "writes" for v in _W},
    **{v: "reads" for v in ("innodb_read_ahead_threshold", "innodb_random_read_ahead", "innodb_read_io_threads")},
    **{v: "dns" for v in ("skip_name_resolve", "host_cache_size")},
)


def name_resolution(inputs):
    """(seconds, calls) spent resolving host names in the measured window, or None when it was not measured."""
    nr = ((inputs or {}).get("probes") or {}).get("name_resolution")
    return (nr["seconds"], nr["calls"]) if nr else None


def missing_for_variables(changes, summary, rates=None, inputs=None):
    """Why none of the changed variables can act in this workload (None = at least one can, or nothing is known)."""
    if summary is None or not changes:
        return None
    s, a = summary, activity(summary)
    r = dict(rates or {})
    if "net_listen_overflows" in s or "net_listen_drops" in s:
        r["_listen_drops"] = (s.get("net_listen_overflows") or 0) + (s.get("net_listen_drops") or 0)
    if "net_tcp_req_q_full_drop" in s:
        r["_syn_drops"] = s.get("net_tcp_req_q_full_drop") or 0
    why = []
    for c in changes:
        cnt = VAR_COUNTERS.get(c.get("variable"))
        if cnt and cnt[0] in r and r[cnt[0]] <= 0:
            why.append(f"{c['variable']} only acts on {cnt[1]}, which did not happen in the window ({cnt[0].lstrip('_')} = 0)")
            continue
        need = VAR_NEEDS.get(c.get("variable"))
        if need is None:
            return None  # a variable without a known precondition: cannot rule it out
        if need == "writes" and not a["writes"]:
            why.append(f"{c['variable']} only affects writes, flushing or commits and the workload writes almost nothing "
                       f"(redo {(s.get('redo_mbs') or 0):.2f} MB/s)")
        elif need == "reads" and not a["reads"]:
            why.append(f"{c['variable']} only affects reads from disk and there are almost none ({(s.get('bp_disk_reads') or 0):.0f} page reads/s)")
        elif need == "dns":
            nr = name_resolution(inputs)
            if nr is None:
                why.append(f"{c['variable']} only acts on host name resolution, and its time was not measured (no eBPF probe data)")
            elif nr[0] <= 0:
                why.append(f"{c['variable']} only acts on host name resolution, and the probes measured none in the window "
                           f"({nr[1]} lookups)")
            else:
                return None
        else:
            return None
    return "; ".join(why) or None


# Mechanisms whose activity a counter shows directly (evidence, not a threshold: the thing exists or it does not)
def _no_ibuf(s):
    if "ibuf_size_max" not in s:
        return None  # not measured: cannot rule it out
    # "Ibuf: size 1" is the empty change buffer (its root page): buffered changes start at 2
    return None if s["ibuf_size_max"] > 1 else "the change buffer held no buffered changes in the window (InnoDB status: size 1 = empty): disabling it changes nothing"


def _never(counter, what):
    def check(s, rates):
        if counter not in (rates or {}):
            return None  # not measured
        return None if rates[counter] > 0 else f"{what} did not happen in the window ({counter} = 0): this setting has nothing to act on"
    return check


COUNTER_PRECONDITIONS = {
    "change-buffer-off": lambda s, r: _no_ibuf(s),
    "thread-cache": _never("Threads_created", "thread creation"),
    "table-open-cache": _never("Opened_tables", "table opening"),
    "table-def-cache": _never("Opened_table_definitions", "table definition loading"),
    "tmp-tables": _never("Created_tmp_disk_tables", "on-disk temporary tables"),
    "tmp-olap": _never("Created_tmp_disk_tables", "on-disk temporary tables"),
    "sort-buffer": _never("Sort_merge_passes", "sort merge passes"),
    "sort-buffer-olap": _never("Sort_merge_passes", "sort merge passes"),
}


def missing_mechanism(item_id, summary, changes=None, rates=None, inputs=None):
    """Why the change has nothing to act on in this workload (None = it has)."""
    if summary is None:
        return None
    if item_id in COUNTER_PRECONDITIONS:
        why = COUNTER_PRECONDITIONS[item_id](summary, rates)
        if why:
            return why
    if changes is not None and item_id not in WRITES | READS | ANY_IO:
        return missing_for_variables(changes, summary, rates, inputs)
    a = activity(summary)
    if item_id in WRITES and not a["writes"]:
        return f"the workload writes almost nothing (redo {(summary.get('redo_mbs') or 0):.2f} MB/s): this setting only affects writes, flushing or commits"
    if item_id in READS and not a["reads"]:
        return f"no buffer pool misses ({(summary.get('bp_disk_reads') or 0):.0f} page reads/s): this setting only affects reads from disk"
    if item_id in ANY_IO and not (a["reads"] or a["writes"]):
        return "no disk reads and almost no writes: the data is in memory and nothing is flushed"
    return None


def relevance(item_id, diagnosis):
    """(score 0..3, list of matched bottlenecks) - how directly the change addresses the diagnosed bottlenecks."""
    if item_id.startswith("revert") or item_id.startswith("rollback"):
        return None, []
    m = MECHANISMS.get(item_id)
    if not m:
        return None, []
    relieves = set(m[0])
    # the generic "io" match only counts when the kind of I/O bottleneck is unknown: once the diagnosis says
    # read-bound, flush-bound or commit-bound, a change must address that kind to be relevant
    kinds = {d["type"] for d in diagnosis or []} & {"io-read", "io-flush", "commit"}
    if kinds and "io" in relieves and not relieves & kinds:
        relieves.discard("io")
    matched = [(d["type"], SEVERITY.get(d.get("severity"), 1)) for d in diagnosis or [] if d["type"] in relieves]
    if not matched:
        return 0, []
    return max(w for _, w in matched), [t for t, _ in matched]
