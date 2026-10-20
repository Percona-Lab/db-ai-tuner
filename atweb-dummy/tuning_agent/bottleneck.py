"""Where the database's time goes: what keeps the workload from going faster.

Instead of labelling a run "CPU-bound" or "I/O-bound", account for the time of the database threads in one unit -
average number of threads in each state over the measured window - and rank the components:

* on CPU, split by what the CPU profile says the CPU does (useful SQL / B-tree work, spinning on latches, the memory
  allocator, performance_schema, parsing / optimizing, the commit path, ...); unprofiled CPU stays one component;
* waiting for a CPU (runnable but not running: scheduler run-queue time);
* waiting in performance_schema wait classes (data file I/O, redo / binlog I/O, mutex / rwlock, table / metadata
  locks, other files) - global summaries, so background threads (page cleaners, log writer) are included: an upper
  bound for what user threads wait on;
* row lock waits (InnoDB status counters);
* client / network (socket waits: the server waits for the next statement - the client or the network is the limit);
* off-CPU time measured by eBPF probes (probes.py): name resolution during connect.

Each component carries the bottleneck tags it maps to; mechanisms.MECHANISMS says which tags a change relieves, so an
experiment's relevance is the share of time it can act on. Classes like "I/O-bound" remain convenience labels only.
"""

# CPU profile category -> (component name, bottleneck tags)
CPU_PARTS = {
    "spin / mutex wait": ("CPU: spinning on latches", ("cpu-spin", "concurrency")),
    "memory allocator": ("CPU: memory allocator", ("cpu-allocator",)),
    "performance_schema": ("CPU: performance_schema", ("cpu-pfs",)),
    "compression / checksums": ("CPU: checksums / compression", ("cpu",)),
    "parser / optimizer": ("CPU: parsing / optimizing", ("cpu-parse",)),
    "redo / commit": ("CPU: redo / commit path", ("commit",)),
    "row locks": ("CPU: row lock management", ("locks",)),
    "buffer pool": ("CPU: buffer pool page access", ("cpu",)),
    "B-tree / row access": ("CPU: B-tree / row access", ("cpu",)),
    "SQL executor": ("CPU: SQL execution", ("cpu",)),
    "network / protocol": ("CPU: network / protocol", ("cpu-network",)),
    "disk I/O (kernel)": ("CPU: kernel I/O path", ("io",)),
    "scheduler / futex": ("CPU: scheduler / futex (sleep-wakeup)", ("cpu-futex", "concurrency")),
    "memory management (kernel)": ("CPU: kernel memory management", ("cpu-mm",)),
    "other": ("CPU: other", ("cpu",)),
}
WAIT_PARTS = {
    "data_io": ("Waiting: data file I/O", ("io", "io-read")),
    "redo_io": ("Waiting: redo log I/O (commit)", ("commit",)),
    "binlog_io": ("Waiting: binary log I/O (commit)", ("commit",)),
    "sync": ("Waiting: mutex / rwlock", ("concurrency",)),
    "table_lock": ("Waiting: table locks", ("locks",)),
    "metadata_lock": ("Waiting: metadata locks", ("locks",)),
    "other_file_io": ("Waiting: other file I/O (temp files, ...)", ("io",)),
    "socket": ("Client / network: waiting for the next statement", ("network",)),
}


# bottleneck types the breakdown can measure; other diagnoses (network backlog, flush pressure, memory / swap, redo
# capacity, ...) keep counting through the diagnosis
from .probes import OFFCPU_CATEGORIES  # noqa: E402

OFFCPU_TAGS = [(c, t) for c, t, _, _ in OFFCPU_CATEGORIES] + [("Waiting: page fault / swap-in", ("memory", "swap")),
                                                                ("Waiting: client (next statement)", ("network",))]
TAGS = {t for _, ts in list(CPU_PARTS.values()) + list(WAIT_PARTS.values()) + OFFCPU_TAGS for t in ts} | {"sched-delay", "cpu-pressure", "locks", "dns"}


def breakdown(summary, inputs, cpus):
    """[{name, threads, share_pct, tags, source}] sorted by time, or [] when nothing is known."""
    s, inp = summary or {}, inputs or {}
    dur = s.get("duration_s") or 0
    parts = []

    def add(name, threads, tags, source):
        if threads and threads > 0:
            parts.append({"name": name, "threads": round(threads, 3), "tags": list(tags), "source": source})

    cpu_pct = s.get("mysqld_cpu_pct")
    queue = s.get("dbproc_runq_wait_threads") or 0
    if cpu_pct is not None and cpus and cpu_pct > 0:
        # time runnable but waiting for a CPU is a consequence of CPU demand: any change that does less CPU work also
        # shortens the queue, so the queue time is attributed to the CPU components in proportion. The cost of
        # oversubscription itself (context switches, futex sleep / wake-up, spinning) is measured by the CPU profile.
        on_cpu = cpu_pct / 100.0 * cpus
        cats = ((inp.get("cpu_profile") or {}).get("categories") or {})
        if cats and sum(cats.values()) > 0:
            tot = sum(cats.values())
            for cat, pct in cats.items():
                name, tags = CPU_PARTS.get(cat, CPU_PARTS["other"])
                add(name, (on_cpu + queue) * pct / tot, tags, "CPU profile (running + waiting for a CPU)")
        else:
            add("CPU (not profiled)", on_cpu + queue, ("cpu",), "mysqld CPU time (running + waiting for a CPU)")
    else:
        add("Waiting for a CPU (run queue)", queue, ("sched-delay", "cpu-pressure"), "scheduler statistics")
    nr = ((inp.get("probes") or {}).get("name_resolution") or {})
    fg = offcpu_foreground(inp)
    if fg:
        # every blocked moment of the connection threads, classified by where they blocked: replaces the
        # performance_schema wait classes and row lock counters (which miss uninstrumented waits)
        for cat, c in fg.items():
            if "dns" in c.get("tags", ()) and nr:
                continue  # the uprobes cover the whole measured window, the off-CPU slice only a part
            add(cat, c.get("threads") or 0, c.get("tags") or (), "eBPF off-CPU profile of connection threads (end of warm-up)")
    else:
        if dur:
            for cls, sec in (inp.get("wait_classes") or {}).items():
                if cls in WAIT_PARTS:
                    name, tags = WAIT_PARTS[cls]
                    add(name, sec / dur, tags, "performance_schema waits (incl. background threads)")
        lw, lms = s.get("row_lock_waits") or 0, s.get("row_lock_wait_avg_ms") or 0
        add("Waiting: row locks", lw * lms / 1000.0, ("locks",), "InnoDB row lock counters")
    add("Connection setup: name resolution (DNS)", nr.get("threads") or 0, ("dns",), "eBPF uprobes on libc getnameinfo / getaddrinfo")
    tot = sum(p["threads"] for p in parts)
    if not tot:
        return []
    merged = {}
    for p in parts:  # several profile categories can map to one component
        m = merged.setdefault(p["name"], dict(p, threads=0.0))
        m["threads"] += p["threads"]
    out = sorted(merged.values(), key=lambda p: -p["threads"])
    for p in out:
        p["threads"] = round(p["threads"], 3)
        p["share_pct"] = round(100.0 * p["threads"] / tot, 1)
    if queue and out:  # how much of the CPU time was spent waiting for one (shown with the CPU components)
        out[0]["cpu_queue_share_pct"] = round(100.0 * queue / tot, 1)
    return out


def offcpu_foreground(inputs):
    """{category: {threads, seconds, tags}} of the connection threads' off-CPU time, or {} when not measured."""
    o = (inputs or {}).get("offcpu") or {}
    return {} if o.get("error") else (o.get("foreground") or {})


def acted_on(relieves, parts):
    """Components (with time) a change with these relieved tags acts on."""
    rel = set(relieves)
    return [p for p in parts if rel & set(p["tags"]) and "network" not in p["tags"] and p.get("threads", 0) > 0]


def relevance(relieves, parts):
    """(0..3, matched component names): the share of database time a change can act on."""
    if not parts:
        return None, []
    rel = set(relieves)
    hit = [p for p in parts if rel & set(p["tags"]) and "network" not in p["tags"]]
    share = sum(p["share_pct"] for p in hit)
    score = 3 if share >= 30 else 2 if share >= 15 else 1 if share >= 5 else 0
    return score, [f"{p['name']} ({p['share_pct']:.0f}%)" for p in hit]
