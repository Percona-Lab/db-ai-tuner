"""What each recommendation physically changes and which bottlenecks it can relieve.

Used to rank experiments: a change whose mechanism matches the diagnosed bottleneck is tried first; a change that
cannot touch the bottleneck is tried last (it may still help, but it is unlikely to). Bottleneck names are the
diagnosis types from engine.diagnose().
"""

MECHANISMS = {
    "bp-grow": (("memory", "io", "io-read"), "More pages cached: fewer disk reads."),
    "bp-shrink": (("swap", "memory-pressure", "oom"), "Gives memory back to the OS: no swapping/reclaim stalls."),
    "bp-instances": (("concurrency", "cpu", "cpu-pressure"), "Splits buffer pool mutexes."),
    "redo-grow": (("redo", "io", "io-flush"), "Fewer forced checkpoints; pages absorb more changes before being written."),
    "log-buffer": (("redo", "commit"), "Fewer waits for redo log buffer space."),
    "io-capacity-up": (("redo", "io", "io-flush"), "Page cleaners flush harder: keeps redo free."),
    "io-capacity-down": (("io", "io-read"), "Fewer background writes competing with foreground I/O."),
    "lru-depth": (("io", "io-flush"), "More free pages ready: no single-page flushes in user threads."),
    "flush-neighbors": (("io", "io-flush"), "No extra neighbour page writes."),
    "o-direct": (("io", "memory", "io-read"), "No double buffering in the OS page cache."),
    "ahi-on": (("cpu", "cpu-pressure"), "Hash lookups skip B-tree descents for hot point reads."),
    "ahi-off": (("concurrency", "cpu"), "Removes AHI latch contention."),
    "log-writer-threads-off": (("cpu", "cpu-pressure", "commit"), "No dedicated log threads competing for few CPUs."),
    "spin-loops": (("cpu", "cpu-pressure", "concurrency", "sched-delay", "cpu-spin"), "Less CPU burned spinning on contended latches."),
    "thread-pool": (("concurrency", "cpu-pressure", "sched-delay", "cpu-futex", "cpu-spin"), "Caps active threads: less context switching."),
    "table-open-cache": (("cpu",), "Avoids reopening tables."),
    "table-def-cache": (("cpu",), "Avoids re-reading table definitions."),
    "thread-cache": (("cpu",), "Avoids thread creation per connection."),
    "max-connections": ((), "Admits more clients."),
    "tmp-tables": (("io", "cpu"), "Temporary tables stay in memory."),
    "tmp-olap": (("io", "cpu"), "Analytical temporary tables stay in memory."),
    "sort-buffer": (("io", "cpu"), "Sorts finish in memory."),
    "sort-buffer-olap": (("io", "cpu"), "Large analytical sorts finish in memory."),
    "join-buffer": (("io", "cpu"), "Hash joins build in memory instead of spilling."),
    "read-ahead": (("io", "io-read"), "Larger sequential reads for scans."),
    "subquery-to-derived": (("cpu", "io"), "Correlated subqueries computed once instead of per row."),
    "bka-mrr": (("io", "io-read"), "Index lookups fetched in primary-key order."),
    "group-commit-delay": (("commit",), "More transactions share one binlog fsync."),
    "change-buffer-off": (("io-flush", "cpu", "concurrency", "io"), "No change-buffer merges competing with foreground work or taking buffer pool pages."),
    "change-buffer": (("io", "io-read"), "Secondary index updates without reading the page."),
    "trx-commit-2": (("commit", "io"), "No fsync per commit."),
    "sync-binlog": (("commit", "io"), "No binlog fsync per commit group."),
    "doublewrite-off": (("io", "commit", "io-flush"), "Pages written once instead of twice."),
    "binlog-off": (("commit", "io", "cpu"), "No binary log writes, fsyncs or two-phase commit."),
    "ps-off": (("cpu", "cpu-pressure", "cpu-pfs"), "No instrumentation overhead."),
    "binlog-minimal": (("io",), "Smaller binary log writes for updates (helps only when binlog I/O volume matters)."),
    "os-swappiness": (("swap", "memory-pressure"), "Kernel keeps database memory resident."),
    "os-thp-off": (("memory", "cpu", "cpu-mm"), "No compaction / khugepaged stalls."),
    "os-scheduler": (("io",), "No request reordering overhead on SSD."),
    "os-noatime": (("io",), "No access-time metadata writes."),
    "os-dirty-writeback": (("commit", "io"), "Small, steady writeback: shorter fsyncs."),
    "os-jemalloc": (("cpu", "cpu-pressure", "concurrency", "cpu-allocator"), "Less allocator lock contention."),
    "os-hugepages": (("cpu", "memory"), "Fewer TLB misses."),
    "os-governor": (("cpu",), "Full clock speed."),
    "os-numa": (("cpu", "memory"), "No cross-node page migration."),
    "os-autogroup": (("cpu-pressure", "cpu", "sched-delay"), "mysqld threads compete for CPU individually, not as one group."),
    "os-aio": (("io",), "Native async I/O stays available."),
    "ps-lite": (("cpu", "cpu-pressure", "cpu-pfs"), "Less instrumentation work per statement and allocation."),
    "sweep-dirty-lwm": (("io", "redo", "commit", "io-flush"), "Dirty pages absorb more updates before being written, or flushing starts earlier."),
    "sweep-lru-depth": (("io", "io-flush"), "Free pages ready for user threads (no single-page flushes)."),
    "sweep-read-io-threads": (("io", "memory", "io-read"), "More asynchronous reads in flight."),
    "sweep-purge-threads": (("locks", "io"), "Purge keeps up with undo history."),
    "flush-sync-off": (("io", "redo", "io-flush", "io-read"), "Reads are not starved during sync flushing."),
    "sweep-bp-instances": (("concurrency", "cpu"), "Less buffer pool mutex contention."),
    "purge-lag": (("locks", "io", "memory"), "Bounded history: shorter version chains for readers."),
    "sweep-spin-wait-delay": (("cpu-spin", "concurrency"), "Less CPU burned spinning, or faster wake-ups."),
    "writeset-history": (("cpu", "commit"), "Less hashing work per commit."),
    "o-direct-no-fsync": (("commit", "io"), "One fsync fewer per data file write batch."),
    "bp-overcommit": (("memory", "io"), "Cold memory goes to swap; the larger cache saves more reads than swap costs."),
    "os-irq-split": (("softirq", "sched-delay", "network-rtt"), "Interrupts no longer preempt query threads."),
    "rg-innodb-bg": (("sched-delay",), "Background flushing no longer competes with query threads for CPU."),
    "fs-xfs": (("io", "commit"), "Different allocation/journaling behaviour for data and redo files."),
    "fs-ext4": (("io", "commit"), "Fewer journal commits."),
    "fs-zfs": (("io", "commit", "memory"), "Atomic writes, compression, different caching."),
    "fs-btrfs": (("io",), "Extent-based allocation without copy-on-write."),
    "zfs-doublewrite-off": (("io", "commit"), "Pages written once instead of twice."),
    "malloc-jemalloc-tuned": (("cpu", "memory", "concurrency"), "Allocator variant."),
    "malloc-tcmalloc": (("cpu", "concurrency"), "Allocator variant."),
    "malloc-mimalloc": (("cpu", "concurrency"), "Allocator variant."),
    "malloc-glibc-tuned": (("memory", "cpu"), "Allocator variant."),
    "sweep-bp": (("memory", "io"), "More pages cached: fewer disk reads."),
    "sweep-redo": (("redo", "io", "io-flush"), "Fewer forced checkpoints."),
    "sweep-io-capacity": (("io", "redo", "io-flush"), "Background flushing matched to the storage."),
    "sweep-thread-concurrency": (("concurrency", "cpu-pressure"), "Caps threads inside InnoDB."),
    "net-listen-backlog": (("connections",), "Bursts of connects queue instead of being dropped."),
    "net-syn-backlog": (("connections",), "Half-open connections queue instead of being dropped."),
    "skip-name-resolve": (("connections",), "No reverse DNS lookup per connection."),
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
