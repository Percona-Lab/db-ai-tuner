"""Rule-based recommendation engine.

Inputs (ctx):
  goal          'tps' | 'latency'
  policy        'safe' (production-safe only) | 'benchmark' (also durability / observability trade-offs)
  hw            hardware facts
  flavor, version
  variables     current SHOW GLOBAL VARIABLES (lower-case names)
  summary       summary of the last measured run
  inputs        decision inputs of the last measured run (status rates, waits, top statements ...)
  history       list of earlier runs: {seq, label, changes:[{variable,old,new}], verdict, goal_value}

Every recommendation carries the evidence it was derived from so the decision can be audited.
"""
import math
import re

from .evaluate import RESOLUTION_NOISE, mean_noise
from . import bottleneck, knowledge
from .mechanisms import MECHANISMS, missing_mechanism, name_resolution, relevance
from .knowledge import KB, OPTIONS, flags_subset, fmt_size, human_size, normalize, parse_size, tradeoff_of

GiB = 1024 ** 3
MiB = 1024 ** 2
SWAP_MIN, SWAP_CAP = 4 * GiB, 16 * GiB  # swap the agent aims for: room for cold memory and a margin before an OOM kill
SWAP_SHARE = 0.20  # of RAM (user, 2026-10-25: 1x RAM took a lot of a small drive for no reason; 20% leaves room to swap out
                   # memory nothing uses, bounded 4-16 GiB). A larger swap already on the host is never shrunk.


def swap_target(ram):
    """Total swap the agent aims for: 20% of RAM, at least 4 GiB, at most 16 GiB."""
    return int(min(max(ram * SWAP_SHARE, SWAP_MIN), SWAP_CAP))


def _v(vars_, name, default=None):
    return vars_.get(name, default)


def _int(vars_, name, default=0):
    try:
        return int(float(vars_.get(name, default)))
    except (TypeError, ValueError):
        return default


def _on(vars_, name):
    return str(vars_.get(name, "")).upper() in ("ON", "1")


def classify_workload(summary, inputs):
    """oltp-write | oltp-read | olap | mixed, with the evidence used."""
    s, rates = summary or {}, (inputs or {}).get("status_rates", {})
    stmts = s.get("statements_per_s") or s.get("qps") or 0
    if not stmts:
        return {"type": "idle", "evidence": {}}
    writes = sum(rates.get(k, 0) for k in ("Com_insert", "Com_update", "Com_delete", "Com_replace", "Com_insert_select",
                                             "Com_update_multi", "Com_delete_multi"))
    reads = rates.get("Com_select", 0)
    calls = rates.get("Com_call_procedure", 0)
    rows_read = rates.get("Innodb_rows_read", 0)
    rows_per_stmt = rows_read / max(stmts, 1e-9)
    lat = s.get("avg_latency_ms") or 0
    ev = {"statements_per_s": round(stmts, 1), "avg_latency_ms": lat, "rows_read_per_statement": round(rows_per_stmt, 1),
          "write_share_pct": round(100 * writes / max(writes + reads, 1e-9), 1), "procedure_calls_per_s": round(calls, 1),
          "sort_merge_passes_per_s": rates.get("Sort_merge_passes", 0), "tmp_disk_tables_per_s": rates.get("Created_tmp_disk_tables", 0),
          "full_joins_per_s": rates.get("Select_full_join", 0)}
    # latency counts only together with the work per statement: queueing at 4096 connections pushed a read-write load over
    # 50 ms and made it "olap" (r1 HC-3: join-buffer and tmp-table experiments for point reads and short transactions)
    if rows_per_stmt >= 5000 or (lat >= 50 and rows_per_stmt >= 200):
        kind = "olap"
    elif writes + reads == 0 and calls > 0:
        kind = "oltp-write"  # stored-procedure OLTP (e.g. HammerDB): statements run inside procedures
    elif writes / max(writes + reads, 1e-9) >= 0.15:
        kind = "oltp-write"
    elif (lat >= 10 and rows_per_stmt >= 50) or rows_per_stmt >= 500:
        kind = "mixed"
    else:
        kind = "oltp-read"
    return {"type": kind, "evidence": ev}


def harmful_swap(s):
    """Swap that costs performance: pages read back in at a meaningful rate, memory stalls, or OOM kills.
    Swap-out alone (cold pages parked in swap) is not harmful: it frees RAM for caches."""
    return ((s.get("swap_in") or 0) > 64 or (s.get("psi_memory_full") or 0) >= 2 or (s.get("psi_memory_some") or 0) >= 10
            or (s.get("oom_kills") or 0) > 0)


def _numval(var, x):
    """Numeric value of a setting for ordering sweep points (sizes included; thread concurrency 0 = unlimited)."""
    if x is None:
        return None
    if var == "innodb_thread_concurrency" and str(x).strip() == "0":
        return float("inf")
    try:
        return float(x)
    except (TypeError, ValueError):
        try:
            return float(parse_size(str(x)))
        except Exception:
            return None


def nice_value(var, x):
    """An agent-generated value snapped to a round one (knowledge.nice_number), in the setting's own notation."""
    n = _numval(var, x)
    if n is None or n in (float("inf"),):
        return x
    r = knowledge.nice_number(var, n)
    if r == n:
        return x
    return fmt_size(int(r)) if (KB.get(var) or {}).get("kind") == "size" else (str(int(r)) if r == int(r) else str(r))


def nice_values(var, values, current=None):
    """Sweep points snapped to round values, duplicates and the current value dropped, order kept."""
    out, seen = [], {_numval(var, current)} if current is not None else set()
    for x in values:
        y = nice_value(var, x)
        k = _numval(var, y)
        if k not in seen:
            seen.add(k)
            out.append(y)
    return out


def shaped_values(var, values, current, direction=None):
    """Sweep points ordered (and completed) by how the setting's response is expected to look (catalog):
    saturating - upward from the current value, smallest step first (the sweep stops once a step stops helping);
    optimum - both sides of the current value, nearest first (the sweep then extends towards the better side).
    direction ("up" / "down"): the run's evidence already points one way (e.g. prefetched pages evicted unused: read
    ahead less) - that side first, nearest first, then the other side."""
    shape = knowledge.response_shape(var)
    cur = _numval(var, current)
    values = nice_values(var, values, current)
    nums = [(x, _numval(var, x)) for x in values]
    if cur is None or any(n is None for _, n in nums) or shape not in ("saturating", "optimum"):
        return values, shape
    if shape == "saturating":
        up = sorted([(x, n) for x, n in nums if n > cur], key=lambda p: p[1])
        return ([x for x, _ in up] or values), shape
    below = sorted([(x, n) for x, n in nums if n < cur], key=lambda p: -p[1])
    above = sorted([(x, n) for x, n in nums if n > cur], key=lambda p: p[1])
    size = (knowledge.KB.get(var) or {}).get("kind") == "size"
    fmt = (lambda n: fmt_size(int(n))) if size else (lambda n: str(int(n)))
    if not below and cur not in (float("inf"),) and cur >= 2:
        below = [(fmt(cur / 2), cur / 2)]
    if not above and cur not in (float("inf"),):
        above = [(fmt(cur * 2), cur * 2)]
    if direction in ("up", "down"):
        first, second = (above, below) if direction == "up" else (below, above)
        return [p[0] for p in first + second], shape
    out = []
    for i in range(max(len(below), len(above))):  # bracket first: the nearest value on each side, then farther ones
        out += [p[0] for p in (below[i:i + 1] + above[i:i + 1])]
    return out, shape


def swap_hazard(s, noise_pct=None):
    """mysqld's own memory was paged back in from swap during the window (its cgroup's anonymous refaults, not host-wide
    swap-in) and, when an off-CPU profile measured it, waiting for those pages cost time beyond noise."""
    from .evaluate import swap_cost
    return swap_cost(s, noise_pct)


def bp_round(v, n, up=False):
    """MySQL sizes the buffer pool in multiples of chunk_size x instances (and silently rounds up otherwise)."""
    chunk = _int(v, "innodb_buffer_pool_chunk_size", 0) or 128 * MiB
    unit = chunk * max(1, _int(v, "innodb_buffer_pool_instances", 1))
    k = int(n) // unit + (1 if up and int(n) % unit else 0)
    return max(unit, k * unit)


def group_commit(s, inp, breakdown, log_bin=None):
    """Group commit efficiency: the achieved batch against what was possible - the threads waiting at commit (all of
    them can join the next flush) and, as the ceiling, the transactions in flight. Two different mechanisms:
    * binary log on: the binary log group commit (flush / sync / commit stages); its leader also flushes the redo for the
      whole group, so the batch is commits per binary log fsync, and binlog_group_commit_sync_* shape it;
    * binary log off: InnoDB's own redo group commit (log writer / flusher threads); batch = commits per redo fsync; no
      binlog setting has anything to act on.
    Complete grouping (batch >= 70% of the waiters) leaves the flush latency as the limit. None without the counters."""
    commits = s.get("tps") or 0
    dur = s.get("duration_s") or 0
    fio = inp.get("file_io") or {}
    redo = s.get("log_fsyncs_per_s") or (inp.get("db_rates") or {}).get("log_fsyncs") or (
        (fio.get("redo") or {}).get("syncs", 0) / dur if dur and (fio.get("redo") or {}).get("syncs") else None)
    binlog_on = str(log_bin).upper() == "ON"
    bsync = (fio.get("binlog") or {}).get("syncs", 0) / dur if binlog_on and dur and (fio.get("binlog") or {}).get("syncs") else None
    flushes = bsync if binlog_on and bsync else redo
    if not commits or not flushes:
        return None
    batch = commits / flushes
    waiters = sum(p.get("threads") or 0 for p in breakdown or [] if "commit" in (p.get("tags") or ()) and str(p.get("name", "")).startswith("Waiting"))
    in_flight = s.get("threads_running_avg") or s.get("threads_connected_avg")
    out = {"mechanism": "binlog" if binlog_on else "innodb", "batch": round(batch, 1), "flushes_per_s": round(flushes, 1),
           "redo_fsyncs_per_s": round(redo, 1) if redo else None, "binlog_fsyncs_per_s": round(bsync, 1) if bsync else None,
           "commits_per_s": round(commits, 1), "flush_interval_ms": round(1000.0 / flushes, 1),
           "in_flight": round(in_flight, 1) if in_flight else None}
    if waiters >= 1:
        out["waiters"] = round(waiters, 1)
        out["of_waiters_pct"] = round(min(100.0, 100.0 * batch / waiters))
    if in_flight:
        out["of_in_flight_pct"] = round(min(100.0, 100.0 * batch / in_flight))
    out["complete"] = bool(waiters >= 1 and batch >= 0.7 * waiters)
    return out


def gc_text(g):
    what = "binary log group commit" if g.get("mechanism") == "binlog" else "InnoDB redo group commit"
    unit = "binary log fsync" if g.get("mechanism") == "binlog" else "redo flush"
    t = f"{what}: {g['batch']:.0f} commits per {unit} ({g['flushes_per_s']:.1f}/s, one every {g['flush_interval_ms']:.0f} ms)"
    if g.get("waiters"):
        t += f", {g['waiters']:.0f} threads waiting at commit"
    if g.get("in_flight"):
        t += f", {g['in_flight']:.0f} in flight"
    if g.get("waiters"):
        t += (": grouping is complete (every waiting commit joins the next flush), so the flush time is the limit" if g["complete"] else
              f": batches hold only {g['of_waiters_pct']}% of the waiting commits - the grouping itself loses")
    return t


def diagnose(ctx):
    """Classify the bottleneck of the last run."""
    s = ctx.get("summary") or {}
    inp = ctx.get("inputs") or {}
    hw = ctx.get("hw") or {}
    cpus = int(hw.get("cpus") or 1)
    out = []

    def add(kind, severity, text, evidence):
        out.append({"type": kind, "severity": severity, "text": text, "evidence": evidence})

    busy, iow, steal = s.get("cpu_busy", 0), s.get("cpu_iowait", 0), s.get("cpu_steal", 0)
    my = s.get("mysqld_cpu_pct")
    other = f" mysqld uses {my:.0f}% of the host CPU; the rest is other processes (e.g. a co-located benchmark client)." if my is not None else ""
    if busy >= 85:
        add("cpu", "high", f"CPU saturated ({busy:.0f}% busy).{other} Gains must come from doing less work per transaction "
            "or reducing contention, not from more I/O capacity.", {"cpu_busy": busy, "mysqld_cpu_pct": my, "cpus": cpus})
    elif busy >= 65:
        add("cpu", "medium", f"CPU fairly busy ({busy:.0f}%).", {"cpu_busy": busy})
    else:
        # few query threads (analytical streams): each can saturate one core while the host looks idle
        tr = s.get("threads_running_avg") or 0
        if cpus >= 4 and 0.5 <= tr <= 4 and busy >= 0.8 * 100.0 * max(1.0, tr) / cpus:
            add("cpu", "medium", f"CPU-bound per query: {tr:.1f} running thread(s) keep about {busy * cpus / 100:.1f} of {cpus} cores busy "
                f"({busy:.0f}% of the host). Single-threaded execution: gains come from less work per query (optimizer, buffers, "
                "parallel reads), not from more I/O.", {"cpu_busy": busy, "threads_running_avg": tr, "cpus": cpus})
    if steal >= 3 or (s.get("cpu_steal_max") or 0) >= 15:
        add("steal", "high" if steal >= 8 else "medium",
            f"Hypervisor CPU steal {steal:.1f}% on average (peak {s.get('cpu_steal_max') or steal:.0f}% in a 10 s sample): noisy neighbours "
            "on a shared host take CPU at random, so small differences between runs are not trustworthy. Dedicated-CPU instances avoid it.",
            {"cpu_steal": steal, "cpu_steal_max": s.get("cpu_steal_max"), "client_cpu_steal": s.get("client_cpu_steal")})
    # pressure stall information (Linux 4.20+): prefer mysqld's own cgroup numbers, fall back to host-wide
    def psi(res):
        v = s.get(f"mysqld_psi_{res}")
        return (v, "mysqld cgroup") if v is not None else (s.get(f"psi_{res}"), "host")
    cpu_some, src = psi("cpu_some")
    if cpu_some is not None and cpu_some >= 20:
        add("cpu-pressure", "high" if cpu_some >= 50 else "medium",
            f"CPU pressure: runnable {src} tasks waited for a CPU {cpu_some:.0f}% of the time (PSI cpu some).",
            {"psi_cpu_some": cpu_some, "source": src, "psi_cpu_some_host": s.get("psi_cpu_some")})
    wait_pct, delay = s.get("dbproc_runq_wait_pct"), s.get("dbproc_sched_delay_us")
    # a queue that matters: enough threads waiting relative to the CPUs, or busy CPUs - 13% of runnable time on a host 77%
    # idle (one thread waiting 32 us) is not a bottleneck (user 194, where commits waited 79% of the time)
    queued = (s.get("dbproc_runq_wait_threads") or 0) >= max(1.0, 0.25 * cpus) or (s.get("cpu_busy") or 0) >= 60
    if wait_pct is not None and (wait_pct >= 10 or (delay or 0) >= 500):
        add("sched-delay", ("high" if wait_pct >= 25 else "medium") if queued else "low",
            f"Scheduling delay: database threads were runnable but waiting for a CPU {wait_pct:.0f}% of their runnable time "
            f"(avg {delay or 0:.0f} µs each time they got a CPU, worst interval {s.get('dbproc_sched_delay_us_max') or 0:.0f} µs). "
            "This adds directly to query latency, most visibly at the tail; fewer runnable threads (thread pool, concurrency "
            "limits) or keeping other work off the database CPUs shortens it."
            + ("" if queued else f" Few threads wait ({s.get('dbproc_runq_wait_threads') or 0:.1f} on {cpus} CPUs, {s.get('cpu_busy') or 0:.0f}% busy): minor."),
            {k: s.get(k) for k in ("dbproc_runq_wait_pct", "dbproc_sched_delay_us", "dbproc_sched_delay_us_max", "dbproc_runq_wait_threads",
                                   "dbproc_nvcsw_per_s", "host_runq_wait_pct", "mysqld_cpu_pct", "cpu_busy")})
    mem_some, src = psi("memory_some")
    if mem_some is not None and mem_some >= 5:
        add("memory-pressure", "high" if mem_some >= 20 else "medium",
            f"Memory pressure: {src} tasks stalled on memory (reclaim/swap/refaults) {mem_some:.1f}% of the time (PSI memory some).",
            {"psi_memory_some": mem_some, "psi_memory_full": s.get("psi_memory_full"), "source": src})
    io_full, src = psi("io_full")
    io_some, _ = psi("io_some")
    if io_some is not None and ((io_full or 0) >= 10 or io_some >= 40):
        add("io", "high" if (io_full or 0) >= 25 else "medium",
            f"I/O pressure: {src} tasks stalled on I/O {io_some:.0f}% of the time, all non-idle tasks stalled {io_full or 0:.0f}% (PSI io).",
            {"psi_io_some": io_some, "psi_io_full": io_full, "source": src, "disk_await_ms": s.get("disk_await_ms"), "disk_util": s.get("disk_util")})
    elif io_some is None and (iow >= 15 or (s.get("disk_util", 0) >= 90 and s.get("disk_await_ms", 0) >= 2)):
        add("io", "high", f"I/O bound (iowait {iow:.0f}%, disk util {s.get('disk_util', 0):.0f}%, "
            f"await {s.get('disk_await_ms', 0):.1f} ms).",
            {k: s.get(k) for k in ("cpu_iowait", "disk_util", "disk_await_ms", "disk_r_iops", "disk_w_iops")})
    if s.get("bp_hit_pct") is not None and s["bp_hit_pct"] < 99 and s.get("bp_disk_reads", 0) > 50:
        add("memory", "high" if s["bp_hit_pct"] < 95 else "medium",
            f"Working set does not fit: buffer pool hit ratio {s['bp_hit_pct']:.2f}%, {s['bp_disk_reads']:.0f} page reads/s from disk.",
            {"bp_hit_pct": s["bp_hit_pct"], "bp_disk_reads": s.get("bp_disk_reads"), "data_bytes": inp.get("data_bytes")})
    # purge falling behind costs no thread waiting time, so the time breakdown cannot show it: the history list can
    # (lab 213: 23M -> 28M inside one window, 43M later; r1 RW-2 3.9M -> 5.4M - the purge levers ranked 9th-14th)
    hll = s.get("history_list_length_max") or 0
    hll_ev = (((s.get("steady_state") or {}).get("evidence") or {}).get("history_list_length") or [None, None])
    growing = hll_ev[0] is not None and hll_ev[1] is not None and hll_ev[1] > hll_ev[0] * 1.05
    unb = next((u for u in s.get("unbounded_growth") or [] if u["metric"] == "history_list_length"), None)
    pt_ = (ctx.get("variables") or {}).get("innodb_purge_threads", "?")
    if unb:
        # growing at an undiminished rate: not a backlog that settles but a configuration that cannot keep up
        add("purge", "high",
            f"Purge cannot keep up: the undo history list grows without slowing down ({unb['start']:,} -> {unb['end']:,} in the window, "
            f"~{unb['per_hour']:,} per hour) with {pt_} purge thread(s). Unsustainable: left running, "
            "undo and version chains grow until reads slow down and the undo tablespaces fill. More purge threads, or a purge lag "
            "bound that slows writers to what purge can keep up with, act on it.",
            {"history_list_length_max": hll, "unbounded_growth": unb, "innodb_purge_threads": pt_})
    elif hll >= 5_000_000 or (hll >= 1_000_000 and growing):
        add("purge", "high" if hll >= 5_000_000 and growing else "medium",
            f"Purge is falling behind: the undo history list reached {hll:,.0f} entries"
            + (f" and grew {hll_ev[0]:,.0f} -> {hll_ev[1]:,.0f} within the window" if growing else "")
            + ". Reads walk longer version chains and undo grows; more purge threads or a purge lag bound act on it.",
            {"history_list_length_max": hll, "history_list_length_window": hll_ev})
    fp_ = s.get("datadir_free_pct")
    if fp_ is not None and fp_ < 10:
        add("disk-space", "high" if fp_ < 5 else "medium",
            f"The data disk has {s.get('datadir_free_gb')} GiB free ({fp_:.1f}%)"
            + (f", filling at {s.get('datadir_growth_gb_per_h')} GiB/h (full in ~{s.get('datadir_full_in_h')} h)" if s.get("datadir_full_in_h") else "")
            + ". Below ~10% free SSDs and filesystems slow down, so results can drift for reasons that are not the configuration; "
              "free space or reset the data set between runs.", {k: s.get(k) for k in ("datadir_free_gb", "datadir_free_pct", "datadir_growth_gb_per_h", "datadir_full_in_h")})
    ck_, du_ = s.get("checkpoint_age_pct_max") or 0, s.get("disk_util") or 0
    if 50 <= ck_ < 70 and du_ >= 60:
        # checkpoint-driven flushing on a busy disk before the redo log is "nearly full": a larger redo log spreads it out
        # (user 191 / 169: checkpoint 57-58% with the disk 81-86% busy, redo-grow "validation only" - it gained later)
        add("redo", "medium", f"Checkpoint age peaks at {ck_:.0f}% of the redo capacity with the disk {du_:.0f}% busy: flushing is "
            "driven by redo space; a larger redo log spreads the writes out.", {"checkpoint_age_pct_max": ck_, "disk_util": du_})
    if (s.get("checkpoint_age_pct_max") or 0) >= 70:
        add("redo", "high", f"Redo log nearly full (checkpoint age peaked at {s['checkpoint_age_pct_max']:.0f}% of capacity): "
            "forced synchronous flushing stalls writes.", {"checkpoint_age_pct_max": s["checkpoint_age_pct_max"], "redo_mbs": s.get("redo_mbs")})
    # I/O-bound InnoDB comes in three kinds that need different experiments: bound by reads (working set misses
    # memory), by flushing dirty pages (free-page waits, checkpoint pressure, dirty pages at the limit) or by commits
    # (redo fsync per transaction, the "commit" diagnosis below)
    r_iops, w_iops = s.get("disk_r_iops") or 0, s.get("disk_w_iops") or 0
    io_tot = r_iops + w_iops
    wait_free = s.get("bp_wait_free") or 0
    ck_now = s.get("checkpoint_age_pct_max") or 0
    dirty = s.get("bp_dirty_pct_avg") or 0
    try:
        max_dirty = float((ctx.get("variables") or {}).get("innodb_max_dirty_pages_pct") or 90)
    except (TypeError, ValueError):
        max_dirty = 90.0
    reads = s.get("bp_disk_reads") or 0
    if reads >= 100 and io_tot and r_iops >= 0.6 * io_tot:
        add("io-read", "high" if (s.get("bp_hit_pct") or 100) < 95 else "medium",
            f"Read-bound I/O: {reads:.0f} page reads/s from disk, reads are {100 * r_iops / io_tot:.0f}% of disk operations. "
            "More memory for pages and a faster read path help; flushing settings matter little.",
            {"bp_disk_reads": reads, "disk_r_iops": r_iops, "disk_w_iops": w_iops, "bp_hit_pct": s.get("bp_hit_pct")})
    if wait_free >= 1 or ck_now >= 75 or (dirty >= 0.9 * max_dirty and io_tot and w_iops >= 0.5 * io_tot):
        why = [x for x in (f"{wait_free:.0f} free-page waits/s" if wait_free >= 1 else "",
                           f"checkpoint age {ck_now:.0f}% of the redo capacity" if ck_now >= 75 else "",
                           f"dirty pages {dirty:.0f}% (limit {max_dirty:.0f}%)" if dirty >= 0.9 * max_dirty else "") if x]
        add("io-flush", "high" if wait_free >= 10 or ck_now >= 90 else "medium",
            f"Flush-bound: page cleaners cannot keep up ({', '.join(why)}). Flushing capacity, LRU depth, redo size and dirty "
            "page thresholds matter; read settings do not.",
            {"bp_wait_free": wait_free, "checkpoint_age_pct_max": ck_now, "bp_dirty_pct_avg": dirty, "innodb_max_dirty_pages_pct": max_dirty,
             "disk_w_iops": w_iops, "bp_pages_flushed": s.get("bp_pages_flushed")})
    tps = s.get("tps") or 0
    fsyncs = s.get("data_fsyncs") or 0
    if tps > 0 and fsyncs / tps > 0.5 and s.get("disk_await_ms", 0) > 0.5:
        add("commit", "medium", f"Commit-bound: {fsyncs:.0f} fsyncs/s for {tps:.0f} tps, disk await {s.get('disk_await_ms', 0):.2f} ms.",
            {"data_fsyncs": fsyncs, "tps": tps, "disk_await_ms": s.get("disk_await_ms")})
    # measured time waiting for commits (off-CPU profile / wait instruments): group commit can bring fsyncs per transaction
    # far below 1 (session 194: 0.04, 23 commits per fsync) while every commit still waits for a slow flush
    commit_share = sum(p.get("share_pct") or 0 for p in ctx.get("breakdown") or [] if "commit" in (p.get("tags") or ()))
    noise = float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0)
    # redo fsyncs (the group commit batches); older agents only report all data-file fsyncs
    lf = s.get("log_fsyncs_per_s") or ((inp.get("db_rates") or {}).get("log_fsyncs")) or fsyncs
    if commit_share > noise and not any(d["type"] == "commit" for d in out):
        w_ms = max(s.get("disk_w_await_ms") or 0, s.get("disk_flush_await_ms") or 0)
        log_bin = (ctx.get("variables") or {}).get("log_bin")
        g = group_commit(s, inp, ctx.get("breakdown"), log_bin)
        add("commit", "high" if commit_share >= 50 else "medium",
            f"Commit-bound: {commit_share:.0f}% of the threads' time waits for commits "
            + ("(binary log group commit and redo flush)" if str(log_bin).upper() == "ON" else "(redo flush)")
            + (f"; each write takes {w_ms:.1f} ms" if w_ms else "") + (f". {gc_text(g)[0].upper() + gc_text(g)[1:]}" if g else "") + ".",
            {"commit_wait_share_pct": round(commit_share, 1), "disk_w_await_ms": s.get("disk_w_await_ms"), "log_fsyncs_per_s": lf, "tps": tps,
             "group_commit": g})
    # CPU profile (perf sampling of a slice of the run): which kind of work burns the database's CPU
    cp = (inp.get("cpu_profile") or {}).get("categories") or {}
    if cp and busy >= 50:
        rules = (("spin / mutex wait", 10, "cpu-spin", "Threads burn CPU spinning on InnoDB mutexes / rw-locks: contention, not useful work."),
                 ("memory allocator", 8, "cpu-allocator", "malloc/free is a large share of CPU: an allocator with less locking (jemalloc/tcmalloc) may help."),
                 ("performance_schema", 6, "cpu-pfs", "performance_schema instrumentation costs a noticeable share of CPU."),
                 ("parser / optimizer", 15, "cpu-parse", "Parsing and optimising statements is a large share of CPU: prepared statements or simpler queries help more than knobs."),
                 ("memory management (kernel)", 6, "cpu-mm", "Kernel page-fault / compaction work (transparent huge pages, reclaim) takes noticeable CPU."),
                 ("scheduler / futex", 12, "cpu-futex", "Much CPU goes to sleeping and waking threads (futex): too many runnable threads for the CPUs."),
                 ("network / protocol", 20, "cpu-network", "Network stack and protocol handling are a large share of CPU."))
        for cat, thr, kind, why in rules:
            if cp.get(cat, 0) >= thr:
                add(kind, "medium", f"CPU profile: {cat} {cp[cat]:.0f}% of database CPU samples. {why}",
                    {"cpu_profile_categories": dict(list(cp.items())[:6])})

    # wait profile (performance_schema): which class of waits dominates, excluding table I/O (row calls incl. buffer hits)
    wc = inp.get("wait_classes") or {}
    real = {k: v for k, v in wc.items() if k != "table_io"}
    tot = sum(real.values())
    dur = s.get("duration_s") or 0
    if tot > 0 and dur:
        top_cls, top_v = max(real.items(), key=lambda x: x[1])
        share, threads = 100 * top_v / tot, top_v / dur
        names = {"redo_io": "redo log I/O (commit fsyncs)", "data_io": "data file I/O", "binlog_io": "binary log I/O", "sync": "mutex / rwlock contention",
                 "table_lock": "table locks", "metadata_lock": "metadata locks", "socket": "client sockets"}
        # a share of a tiny wait total is not a bottleneck: the waiting threads must be a meaningful part of the load
        # (O3: 4 of 829 running threads waiting on data I/O with the disk 8% busy was diagnosed as I/O-bound)
        floor = max(0.5, 0.05 * (s.get("threads_running_avg") or 0), 0.25 * cpus)
        io_ok = top_cls != "data_io" or (s.get("psi_io_some") or 0) >= 5 or (s.get("disk_util") or 0) >= 30 or (s.get("cpu_iowait") or 0) >= 5
        if threads >= floor and io_ok and share >= 40 and top_cls in names:
            kind = {"redo_io": "commit", "binlog_io": "commit", "data_io": "io", "sync": "concurrency"}.get(top_cls, "wait-" + top_cls)
            if not any(d["type"] == kind for d in out):
                add(kind, "medium", f"Wait profile: {names[top_cls]} accounts for {share:.0f}% of non-row wait time "
                    f"({threads:.1f} threads waiting on average).", {"wait_classes": wc, "duration_s": dur})
    lw = s.get("row_lock_waits") or 0
    # units of work: transactions, or statements when they autocommit (Com_commit stays 0: C4's 9,000 lock waits/s
    # went undiagnosed)
    work = tps or s.get("handler_commits") or s.get("statements_per_s") or s.get("qps") or 0
    if lw > 0 and work > 0 and lw / work > 0.02:
        add("locks", "medium" if lw / work < 0.1 else "high",
            f"Row lock contention: {lw:.1f} waits/s (avg {s.get('row_lock_wait_avg_ms', 0):.1f} ms) for {work:.0f} transactions or statements/s. "
            "Usually a workload/schema property; config helps little.", {"row_lock_waits": lw, "row_lock_wait_avg_ms": s.get("row_lock_wait_avg_ms")})
    if (s.get("oom_kills") or 0) > 0:
        add("oom", "high", f"The kernel OOM killer fired {s['oom_kills']} time(s) during the measurement: memory is over-committed "
            "(buffer pool + per-connection memory + other processes on the host).", {"oom_kills": s.get("oom_kills")})
    sw_ev = {k: s.get(k) for k in ("swap_in", "swap_out", "swap_in_mbs", "swap_out_mbs", "swap_used_mb", "dbproc_swap_mb",
                                   "dbproc_pgmajfault_per_s", "psi_memory_some", "psi_memory_full")}
    if harmful_swap(s):
        add("swap", "high", f"Pages are being swapped back in ({s.get('swap_in') or 0:.0f} pages/s, {s.get('swap_in_mbs') or 0:.2f} MiB/s) "
            f"or memory stalls are high: memory is over-committed for this workload.", sw_ev)
    elif (s.get("swap_used_mb") or 0) > 64:
        add("swap-cold", "low", f"{s['swap_used_mb']:.0f} MiB swapped out but hardly read back ({s.get('swap_in') or 0:.1f} pages/s): "
            "cold memory parked in swap leaves more RAM for caches. Not a problem.", sw_ev)
    tr = s.get("threads_running_avg") or 0
    if tr > cpus * 4 and busy > 60:
        add("concurrency", "medium", f"{tr:.0f} threads running on {cpus} CPUs: heavy oversubscription, mutex convoys likely.",
            {"threads_running_avg": tr, "cpus": cpus})
    ovf = (s.get("net_listen_overflows") or 0) + (s.get("net_listen_drops") or 0) + (s.get("net_tcp_req_q_full_drop") or 0)
    if ovf > 0:
        add("connections", "high", f"Connection attempts dropped by full accept/SYN queues ({ovf:.2f}/s).",
            {"listen_overflows": s.get("net_listen_overflows"), "syn_queue_drops": s.get("net_tcp_req_q_full_drop")})
    elif (s.get("net_conn_per_s") or 0) > 50 * cpus:
        add("connections", "medium", f"{s['net_conn_per_s']:.0f} new TCP connections/s: per-request connections (pooling would help).",
            {"net_conn_per_s": s.get("net_conn_per_s")})
    if (s.get("net_softnet_drops") or 0) > 0 or ((s.get("softirq_max_cpu_pct") or 0) > 50 and (s.get("softirq_avg_cpu_pct") or 0) < 25):
        add("softirq", "medium", f"Packet processing concentrated or dropping (busiest CPU {s.get('softirq_max_cpu_pct', 0):.0f}% softirq, "
            f"average {s.get('softirq_avg_cpu_pct', 0):.0f}%; softnet drops {s.get('net_softnet_drops', 0)}/s).",
            {"softirq_max_cpu_pct": s.get("softirq_max_cpu_pct"), "net_softnet_drops": s.get("net_softnet_drops")})
    rtt = s.get("rtt_avg_ms")
    if (s.get("net_retrans_pct") or 0) > 0.5 and (rtt or 99) < 2:
        add("network-loss", "medium", f"{s['net_retrans_pct']:.2f}% TCP retransmits on a low-latency link: packet loss on the path or host.",
            {"net_retrans_pct": s.get("net_retrans_pct"), "rtt_avg_ms": rtt})
    # a TCP RTT measured on a saturated server includes the server's own queueing (r1 HC-2: 20-25 ms at 100% CPU on a path
    # that measured 0.3-0.8 ms in the other cells): the path's latency shows in the minimum RTT, or with CPUs to spare
    rtt_min = s.get("rtt_min_ms")
    path_rtt = rtt_min if rtt_min is not None else (rtt if (s.get("cpu_busy") or 0) < 80 else None)
    if path_rtt and path_rtt >= 10:
        rtt = path_rtt
        add("network-rtt", "medium" if rtt < 40 else "high", f"Clients are {rtt:.0f} ms away (TCP RTT): every round trip costs more than "
            "most statements; batching, pooling and window settings matter.", {"rtt_avg_ms": rtt, "rtt_p95_ms": s.get("rtt_p95_ms")})
    if (s.get("client_cpu_busy") or 0) >= 90:
        add("client-bound", "high", f"The benchmark/application client is saturated ({s['client_cpu_busy']:.0f}% CPU): measured throughput "
            "reflects the client, not the database. Use a bigger client host or fewer client-side cycles before tuning the server.",
            {"client_cpu_busy": s.get("client_cpu_busy")})
    # Galera flow control (measured paused time) and MyRocks block-cache misses: clustering / engine bottlenecks
    rates_ = inp.get("status_rates") or {}
    paused = (rates_.get("wsrep_flow_control_paused_ns") or 0) / 1e9 * 100
    if (ctx.get("cluster") or {}).get("type") == "galera" and paused > float(ctx.get("noise_pct") or 2.0):
        add("flow-control", "high" if paused > 20 else "medium", f"Galera flow control paused writes {paused:.1f}% of the time: an applier "
            "queue on some node fills up and the whole cluster waits.", {"flow_control_paused_pct": round(paused, 2)})
    rmiss = rates_.get("rocksdb_block_cache_data_miss") or rates_.get("rocksdb_block_cache_miss") or 0
    rhit = rates_.get("rocksdb_block_cache_data_hit") or rates_.get("rocksdb_block_cache_hit") or 0
    if rmiss > 0 and ((ctx.get("engines") or {}).get("data") or {}).get("rocksdb"):
        add("rocksdb-cache", "medium" if rmiss / max(rhit + rmiss, 1) > 0.05 else "low",
            f"MyRocks block cache: {rmiss:.0f} misses/s ({100 * rmiss / max(rhit + rmiss, 1):.1f}% of lookups).",
            {"rocksdb_block_cache_miss_per_s": round(rmiss, 1)})
    # memory MySQL keeps allocating during the window (its own accounting): a leak or an unbounded cache is a memory risk
    # before it is an OOM kill; the buffer pool is allocated in full at start-up and does not count here
    mp = (inp.get("memory_pfs") or {})
    gr = mp.get("growth_mb")
    if gr and mp.get("total_mb") and gr > 0.01 * mp["total_mb"] and mp.get("growing"):
        add("memory-growth", "low", f"MySQL's own memory grew by {gr:.0f} MB during the window ({mp['total_mb']:.0f} MB in total); most: "
            + ", ".join(f"{g['event'].replace('memory/', '')} +{g['mb']:.0f} MB" for g in mp["growing"][:3]) + ".",
            {"pfs_mem_growth_mb": gr, "pfs_mem_total_mb": mp["total_mb"]})
    # NUMA placement measured in the run (multi-node hosts): a node out of memory while others are free, mysqld's memory
    # concentrated on one node, a pool larger than a node, remote allocations
    nev = numa_evidence(s, ctx.get("os_facts") or {}, _int(ctx.get("variables") or {}, "innodb_buffer_pool_size"))
    if nev and nev["imbalance"]:
        add("numa", "medium", f"NUMA ({nev['nodes']} nodes): " + "; ".join(nev["imbalance"]) + ".",
            {k: s.get(k) for k in ("numa_mysqld_max_node_pct", "numa_other_node_pct", "numa_miss_per_s", "numa_node_free_min_mb",
                                   "numa_node_free_max_mb")})
    if not out:
        add("none", "low", "No dominant bottleneck: the server is not saturated by this workload (client-bound or too little load?).",
            {"cpu_busy": busy, "cpu_iowait": iow})
    # memory no process holds: RAM minus available minus mysqld minus the huge-page pool. Kernel or driver allocations
    # (tuner-vbm-amd: the irdma RDMA driver of an E810 NIC held ~6.6 GB of 30 GB) shrink what the buffer pool can use
    ram = int(hw.get("ram_bytes") or 0)
    from . import memory as _mem
    held = _mem.host_other_bytes(s, ram)
    if held and held > max(2 * GiB, 0.08 * ram):
        add("memory-gap", "low",  # advice about the host: it does not make experiments more relevant
            f"{held / GiB:.1f} GiB of {ram / GiB:.0f} GiB RAM is held outside mysqld and the page cache (kernel, drivers or other "
            "processes, a co-located client included). The buffer pool budget subtracts it. If no process explains it, check "
            "kernel allocations (slabtop, /proc/meminfo: SUnreclaim, VmallocUsed) and drivers such as RDMA (irdma on Intel E810 "
            "NICs: blacklist it and reboot when RDMA is not used).",
            {"held_gb": round(held / GiB, 1), "ram_gb": round(ram / GiB, 1), "mem_available_mb": s.get("mem_available_mb"),
             "dbproc_rss_mb": s.get("dbproc_rss_mb")})
    # large transactions: redo per commit far above OLTP's few KB (env 57: data writes far above the pages flushed came from
    # multi-MB transactions, not from redo in the data counters)
    commits = s.get("handler_commits") or 0
    if commits >= 1 and s.get("redo_mbs"):
        kb = s["redo_mbs"] * 1024 / commits
        if kb >= 256:
            rows = (s.get("rows_changed_per_s") or 0) / commits
            add("large-transactions", "low",
                f"Large transactions: {kb:,.0f} KB of redo per commit" + (f" and {rows:,.0f} rows changed per commit" if rows else "")
                + ". Commit-path settings (group commit, trx_commit, sync_binlog) matter less; redo log buffer and capacity, "
                "binlog cache and undo/purge matter more.",
                {"redo_kb_per_commit": round(kb), "rows_per_commit": round(rows) if rows else None, "commits_per_s": commits})
    return out


# partitioned structures whose partitions should spread evenly over NUMA nodes: values as multiples of the node count
NUMA_PARTITIONED = {"innodb_buffer_pool_instances": 64, "innodb_page_cleaners": 64, "table_open_cache_instances": 64}


def _numa_multiple(var, val, ctx):
    nodes = int((ctx.get("os_facts") or {}).get("os:numa_nodes") or 1)
    try:
        n = int(val)
    except (TypeError, ValueError):
        return val
    if nodes < 2 or n <= 1 or n % nodes == 0:
        return val
    return str(min(NUMA_PARTITIONED[var], -(-n // nodes) * nodes))


class Builder:
    def __init__(self, ctx):
        self.ctx = ctx
        self.items = []
        self.v = ctx["variables"]

    def add(self, id, title, changes, rationale, evidence, impact=2, confidence=2, kind="config", goals=("tps", "latency")):
        """changes: list of (variable, value)"""
        ch = []
        risk = "none"
        order = ["none", "low", "compat", "observability", "durability"]
        restart = False
        for var, val in changes:
            if var not in self.v and var not in ("skip_log_bin", "disable_log_bin"):
                continue
            if var in self.v and (flags_subset(val, self.v.get(var)) if KB.get(var, {}).get("kind") == "flags"
                                  else normalize(var, self.v.get(var)) == normalize(var, val)):
                continue
            meta = KB.get(var, {})
            r = meta.get("risk", "low")
            if order.index(r) > order.index(risk):
                risk = r
            if not meta.get("dynamic", False):
                restart = True
            if var in NUMA_PARTITIONED:
                val = _numa_multiple(var, val, self.ctx)
                if normalize(var, self.v.get(var)) == normalize(var, val):
                    continue
            ch.append({"variable": var, "current": self.v.get(var), "proposed": val})
        if kind == "config" and not ch:
            return
        trade = [t for t in (tradeoff_of(c["variable"], c["proposed"]) for c in ch) if t]
        self.items.append({
            "id": id, "title": title, "kind": kind, "changes": ch, "rationale": rationale,
            "safety": "trade-off" if trade else "safe", "tradeoffs": trade,
            "evidence": {k: v for k, v in evidence.items() if v is not None},
            "impact": impact, "confidence": confidence, "risk": risk, "restart": restart,
            "goals": list(goals),
        })


def recommend(ctx):
    v = ctx["variables"]
    s = ctx.get("summary") or {}
    busy = s.get("cpu_busy") or 0
    inp = ctx.get("inputs") or {}
    hw = ctx.get("hw") or {}
    goal = ctx.get("goal", "tps")
    policy = ctx.get("policy", "safe")
    flavor = ctx.get("flavor", "mysql")
    cpus = int(hw.get("cpus") or 1)
    ram = int(hw.get("ram_bytes") or 0)
    ssd = not hw.get("disk_rotational", False)
    rates = inp.get("status_rates", {})
    b = Builder(ctx)
    have_run = bool(s)
    wtype = (ctx.get("workload") or {}).get("type")

    bp = _int(v, "innodb_buffer_pool_size")
    data_bytes = inp.get("data_bytes") or 0
    max_conn = _int(v, "max_connections", 151)
    mem_avail = (s.get("mem_available_mb") or 0) * MiB
    mem_psi = s.get("psi_memory_some") or 0
    swapping = harmful_swap(s)

    # measured shares several gates below use (time breakdown; CPU profile categories)
    commit_sh_ = sum(p.get("share_pct") or 0 for p in ctx.get("breakdown") or [] if "commit" in (p.get("tags") or ()))
    cpu_cat_ = ((inp.get("cpu_profile") or {}).get("categories") or {})
    noise_ = float(ctx.get("noise_pct") or 2.0)

    # ---------------- memory -----------------------------------------------------
    # shrinking only for swapping that costs: swap-in of ~0.3 MB/s triggered it twice (lab 205 / 179, -1.6%)
    if swapping and bp > 512 * MiB and ((s.get("swap_in") or 0) > 1280 or (s.get("psi_memory_full") or 0) >= 2
                                        or (s.get("psi_memory_some") or 0) >= 10 or (s.get("oom_kills") or 0) > 0):
        new = bp_round(v, bp * 0.85)
        if new >= bp:
            new = bp_round(v, bp - 1)
        b.add("bp-shrink", "Shrink the buffer pool: host is under memory pressure", [("innodb_buffer_pool_size", fmt_size(new))],
              "Swapping / memory reclaim stalls make every page access unpredictable; giving memory back to the OS is the first fix.",
              {"swap_in": s.get("swap_in"), "swap_out": s.get("swap_out"), "psi_memory_some": mem_psi, "innodb_buffer_pool_size": human_size(bp)}, impact=3, confidence=3)
    elif have_run and s.get("bp_hit_pct", 100) < 99.5 and (s.get("bp_disk_reads") or 0) > 20:
        headroom = mem_avail - max(512 * MiB, int(ram * 0.05))
        want = min(data_bytes * 1.1 if data_bytes else bp * 2, bp + headroom, ram * 0.85)
        new = bp_round(v, want)
        if new > bp * 1.05:
            b.add("bp-grow", "Grow the buffer pool: working set misses memory",
                  [("innodb_buffer_pool_size", fmt_size(new))],
                  f"Hit ratio {s['bp_hit_pct']:.2f}% with {s['bp_disk_reads']:.0f} disk page reads/s; "
                  f"{human_size(mem_avail)} RAM still available. Larger pool converts disk reads to memory reads.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "bp_disk_reads": s.get("bp_disk_reads"), "data_size": human_size(data_bytes) if data_bytes else None,
                   "mem_available": human_size(mem_avail), "innodb_buffer_pool_size": human_size(bp)}, impact=3, confidence=3)
        else:
            b.add("data-exceeds-ram", "Working set larger than usable memory", [],
                  f"Hit ratio {s['bp_hit_pct']:.2f}% but no memory headroom left ({human_size(mem_avail)} available). "
                  "More RAM, compression or a smaller working set (archiving, better indexes) are the levers.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "mem_available": human_size(mem_avail)}, impact=2, confidence=2, kind="advice")

    if bp >= 2 * GiB and "innodb_buffer_pool_instances" in v and flavor != "mariadb":
        inst = _int(v, "innodb_buffer_pool_instances", 1)
        want = 8 if bp >= 8 * GiB else max(1, bp // GiB)
        if inst < want and cpus >= 4:
            b.add("bp-instances", "More buffer pool instances", [("innodb_buffer_pool_instances", str(want))],
                  "Splits buffer pool mutexes for concurrent workloads.", {"current_instances": inst, "cpus": cpus}, impact=1, confidence=2)

    # ---------------- redo / checkpoint ------------------------------------------
    cap = _int(v, "innodb_redo_log_capacity") or _int(v, "innodb_log_file_size") * _int(v, "innodb_log_files_in_group", 1)
    ck = s.get("checkpoint_age_pct_max") or 0
    redo_mbs = s.get("redo_mbs") or 0
    if have_run and cap and (ck >= 60 or (redo_mbs * 300 * MiB > cap)):
        free = hw.get("disk_free_bytes") or 0
        want = max(cap * 2, int(redo_mbs * MiB * 600))
        want = min(want, 32 * GiB, int(free * 0.2) if free else want)
        want = want // (512 * MiB) * (512 * MiB)
        if want > cap:
            var = "innodb_redo_log_capacity" if "innodb_redo_log_capacity" in v else "innodb_log_file_size"
            val = want if var == "innodb_redo_log_capacity" or flavor == "mariadb" else want // max(1, _int(v, "innodb_log_files_in_group", 1))
            b.add("redo-grow", "Larger redo log", [(var, fmt_size(val))],
                  f"Checkpoint age peaked at {ck:.0f}% of the {human_size(cap)} redo log while writing {redo_mbs:.1f} MiB/s of redo "
                  f"(redo covers {cap / MiB / max(redo_mbs, 0.01):.0f} s of writes). Room for ~10 minutes avoids "
                  "sync-flush stalls and lets pages absorb more changes before being written.",
                  {"checkpoint_age_pct_max": ck, "redo_mbs": redo_mbs, "redo_capacity": human_size(cap)}, impact=3 if ck >= 75 else 2, confidence=3)
    if have_run and rates.get("Innodb_log_waits", 0) > 0:
        lbs = _int(v, "innodb_log_buffer_size", 16 * MiB)
        b.add("log-buffer", "Larger redo log buffer", [("innodb_log_buffer_size", fmt_size(max(lbs * 2, 64 * MiB)))],
              "Transactions waited for redo log buffer space.", {"Innodb_log_waits_per_s": rates.get("Innodb_log_waits")}, impact=2, confidence=3)

    # ---------------- flushing / IO ----------------------------------------------
    ioc = _int(v, "innodb_io_capacity", 200)
    dirty = s.get("bp_dirty_pct_avg") or 0
    if have_run and ssd and ck >= 50 and ioc < 4000 and (s.get("disk_util") or 0) < 70:
        b.add("io-capacity-up", "Raise background flushing budget", [("innodb_io_capacity", str(ioc * 2)), ("innodb_io_capacity_max", str(ioc * 4))],
              f"Redo fill {ck:.0f}% with the disk only {s.get('disk_util', 0):.0f}% utilised: page cleaners can flush harder.",
              {"checkpoint_age_pct_max": ck, "disk_util": s.get("disk_util"), "innodb_io_capacity": ioc, "bp_dirty_pct_avg": dirty}, impact=2, confidence=2)
    # flush-bound without redo pressure: user threads wait for free pages or dirty pages sit at their limit (r1 WI-1: 94.6%
    # dirty, 34 free-page waits/s at 17% checkpoint age - io-capacity-up never fired, the only sweep went down to 1000)
    wait_free_ = s.get("bp_wait_free") or 0
    try:
        maxd_ = float(v.get("innodb_max_dirty_pages_pct") or 90)
    except (TypeError, ValueError):
        maxd_ = 90.0
    if have_run and ssd and ck < 50 and ioc < 10000 and (wait_free_ >= 1 or dirty >= 0.9 * maxd_):
        b.add("io-capacity-up", "Raise background flushing budget", [("innodb_io_capacity", str(min(20000, ioc * 2))),
                                                                     ("innodb_io_capacity_max", str(min(40000, ioc * 4)))],
              f"Flush-bound: {wait_free_:.0f} free-page waits/s, dirty pages {dirty:.0f}% (limit {maxd_:.0f}%) - the page cleaners fall "
              f"behind at innodb_io_capacity {ioc}. Doubling the background flushing budget lets them keep free pages ready. Experiment.",
              {"bp_wait_free": wait_free_, "bp_dirty_pct_avg": dirty, "innodb_io_capacity": ioc, "disk_util": s.get("disk_util")},
              impact=2, confidence=2)
    if have_run and ck < 30 and ioc >= 2000 and (s.get("disk_w_iops") or 0) > ioc * 0.5 and (s.get("disk_util") or 0) > 60:
        b.add("io-capacity-down", "Flush less eagerly", [("innodb_io_capacity", str(max(200, ioc // 2))), ("innodb_io_capacity_max", str(max(400, ioc)))],
              "Redo has plenty of headroom yet background flushing keeps the disk busy; lazier flushing merges more writes per page.",
              {"checkpoint_age_pct_max": ck, "disk_w_iops": s.get("disk_w_iops"), "disk_util": s.get("disk_util")}, impact=1, confidence=2)
    if have_run and rates.get("Innodb_buffer_pool_wait_free", 0) > 0:
        lru = _int(v, "innodb_lru_scan_depth", 1024)
        b.add("lru-depth", "Deeper LRU scan for free pages", [("innodb_lru_scan_depth", str(min(lru * 2, 8192)))],
              "Threads waited for free buffer pool pages (single-page flushing).", {"Innodb_buffer_pool_wait_free_per_s": rates.get("Innodb_buffer_pool_wait_free")}, impact=2, confidence=2)
    if ssd and _int(v, "innodb_flush_neighbors", 0) != 0:
        b.add("flush-neighbors", "Disable neighbour flushing on SSD", [("innodb_flush_neighbors", "0")],
              "Neighbour flushing only helps rotating disks.", {"disk_rotational": hw.get("disk_rotational")}, impact=1, confidence=3)
    if "innodb_flush_method" in v and v.get("innodb_flush_method") in ("fsync", "") and flavor != "mariadb":
        b.add("o-direct", "Use O_DIRECT", [("innodb_flush_method", "O_DIRECT")],
              "Avoid double buffering data pages in the OS cache.", {"innodb_flush_method": v.get("innodb_flush_method")}, impact=2, confidence=3)

    # ---------------- ideas from blogs / benchmarks (docs/research/mysql-tuning-ideas.md) ----------------
    hll = s.get("history_list_length_max") or 0
    unb_hll = unbounded_hll(s)
    blocked = purge_blocked(s)
    if have_run and unb_hll and blocked:
        # purge is not slow, it is held back: no purge setting helps while a read view this old must stay visible
        b.add("purge-blocked", "Purge is held back by a long-open transaction", [],
              f"The history list grew past {PURGE_UNBOUNDED_MIN:,} while a transaction stayed open for {blocked // 60} min: its read view "
              "keeps every newer undo record visible, so purge cannot remove them however fast it runs. Find it in "
              "information_schema.innodb_trx (ORDER BY trx_started) and end it, or keep such reads off this server (a replica). "
              "Purge settings are not tried while it lasts.", {"oldest_trx_s_max": blocked, "unbounded_growth": unb_hll},
              impact=3, confidence=2, kind="advice")
        unb_hll = None  # the remedies below act on purge speed: none of them can help
    if have_run and not blocked and (hll > 200000 and wtype in ("oltp-write", "mixed") or unb_hll) and "innodb_max_purge_lag" in v \
            and _int(v, "innodb_max_purge_lag", 0) == 0:
        # unbounded growth: the cap is the level the window started from, so writers are slowed only beyond what purge
        # handled before (stability first); otherwise 80% of the peak
        lag_cap = max(200000, int(unb_hll["start"])) if unb_hll else max(200000, int(hll * 0.8))
        b.add("purge-lag", "Throttle DML when purge falls behind", [("innodb_max_purge_lag", nice_value("innodb_max_purge_lag", str(lag_cap))),
                                                                     ("innodb_max_purge_lag_delay", "1000000")],
              f"History list peaked at {hll:,.0f} undo records: purge cannot keep up, so reads walk ever longer version chains. "
              "A purge lag limit slows writers slightly to keep history bounded. "
              + ("Stability first: the history grows without bound, so this configuration cannot run in production as it is - a "
                 "bounded history at some cost in write throughput is the better configuration." if unb_hll else
                 "Experiment: it trades write throughput for stable read latency."),
              {"history_list_length_max": hll, "unbounded_growth": unb_hll}, impact=3 if unb_hll else 1, confidence=2 if unb_hll else 1)
        b.items[-1]["lead"] = "Practitioners report it for insert-heavy benchmarks; unverified here."
    if have_run and unb_hll and "innodb_purge_batch_size" in v and _int(v, "innodb_purge_batch_size", 300) < 1000:
        b.add("purge-batch", "Larger purge batches", [("innodb_purge_batch_size", "1000")],
              f"The history list grows without slowing down ({unb_hll['start']:,} -> {unb_hll['end']:,} in the window): larger batches let "
              "each purge pass free more undo per round of coordination. Stability remedy, measured.",
              {"unbounded_growth": unb_hll, "innodb_purge_batch_size": v.get("innodb_purge_batch_size")}, impact=2, confidence=1)
    if have_run and unb_hll and busy >= 70:
        # purge competes with the user threads for the CPUs: a higher priority for the purge threads (their own MySQL resource
        # group, re-applied after every start) keeps purge running - safer than starving it or slowing every writer
        tr_ = s.get("threads_running_avg") or 0
        nice_cap = str(v.get("os:mysqld_cap_sys_nice") or "").upper()
        why = (f"Purge cannot keep up on {cpus} CPUs that are {busy:.0f}% busy: the purge threads compete with {tr_:.0f} running user "
               "threads. A resource group with a higher priority (THREAD_PRIORITY -10) for the purge coordinator and workers keeps purge "
               "running ahead of them. Applied after mysqld starts and re-applied after every restart (no option file carries it).")
        if "rg:purge" in v and (nice_cap == "ON" or (ctx.get("os_tuning") and "os:mysqld_cap_sys_nice" in v)):
            ch = [("rg:purge", "prio=-10")] + ([("os:mysqld_cap_sys_nice", "ON")] if nice_cap != "ON" else [])
            pt_now = _int(v, "innodb_purge_threads", 4)
            if nice_cap != "ON" and pt_now < 4:
                # a restart anyway: one purge thread at a higher priority is still one thread against hundreds (MySQL 8.4
                # defaults to 1 on up to 16 logical CPUs) - give it workers in the same step (user, 2026-10-25)
                ch.append(("innodb_purge_threads", str(min(4, max(2, cpus // 2)))))
            b.add("purge-priority", "Higher priority for the purge threads", ch,
                  why + (" mysqld needs CAP_SYS_NICE for thread priorities: added to its service (a restart)." if nice_cap != "ON" else "")
                  + (f" With the restart, {min(4, max(2, cpus // 2))} purge threads instead of {pt_now}: priority for one thread alone "
                     "is rarely enough." if nice_cap != "ON" and pt_now < 4 else ""),
                  {"cpu_busy": busy, "cpus": cpus, "threads_running_avg": tr_, "unbounded_growth": unb_hll}, impact=3, confidence=2)
        else:
            b.add("purge-priority", "Higher priority for the purge threads", [],
                  why + (" mysqld lacks CAP_SYS_NICE and OS tuning is off: add AmbientCapabilities=CAP_SYS_NICE to the mysqld service, then "
                         "CREATE RESOURCE GROUP rg_purge TYPE=SYSTEM THREAD_PRIORITY=-10 and SET RESOURCE GROUP rg_purge FOR <THREAD_IDs of "
                         "thread/innodb/srv_purge_thread and srv_worker_thread> after every start." if "rg:purge" in v else
                         " Resource groups are not available on this server (MariaDB, or MySQL before 8.0): pin or renice the purge threads "
                         "by THREAD_OS_ID (taskset / renice) after every start."),
                  {"cpu_busy": busy, "cpus": cpus, "threads_running_avg": tr_}, impact=2, confidence=1, kind="advice")
    tr_now = s.get("threads_running_avg") or 0
    if have_run and unb_hll and tr_now > 2 * cpus:
        # every running user thread creates undo; purge has a few threads. Fewer threads inside InnoDB at a time (or in the
        # thread pool) leaves purge its share of the CPUs - a stability remedy that costs less than throttling every writer
        # with innodb_max_purge_lag. Online in MySQL / Percona (innodb_thread_concurrency) and in a running thread pool
        tp_on = str(v.get("thread_handling") or "").lower() == "pool-of-threads"
        if not tp_on and "thread_pool_size" in v and "thread_handling" in v:
            # the thread pool caps the threads that run at once - and so the undo they create - for any number of connections
            # (Percona Server, MariaDB). It may cost throughput at this concurrency; stability comes first (user, 2026-10-25):
            # a bounded history at a lower rate is the configuration to tune from, never an unbounded one
            b.add("purge-threadpool", "Thread pool: fewer threads creating undo at once", [("thread_handling", "pool-of-threads"),
                                                                                           ("thread_pool_size", str(cpus))],
                  f"{tr_now:.0f} user threads run on {cpus} CPUs and create undo faster than purge removes it: the history list grows "
                  "without bound. The thread pool runs about one statement per CPU at a time and queues the rest, so purge gets "
                  "its share of the CPUs. Stability remedy: likely to cost throughput, kept when it bounds the history; tuning "
                  "continues from it. Needs a restart.",
                  {"threads_running_avg": tr_now, "cpus": cpus, "unbounded_growth": unb_hll}, impact=3, confidence=2)
        if tp_on and "thread_pool_oversubscribe" in v and _int(v, "thread_pool_oversubscribe", 3) > 1:
            ch = [("thread_pool_oversubscribe", "1")]
        elif not tp_on and "innodb_thread_concurrency" in v and _int(v, "innodb_thread_concurrency", 0) == 0 and flavor != "mariadb":
            ch = [("innodb_thread_concurrency", str(max(8, 2 * cpus)))]
        else:
            ch = []
        if ch:
            b.add("purge-concurrency", "Fewer user threads creating undo at once", ch,
                  f"{tr_now:.0f} user threads run on {cpus} CPUs and each creates undo faster than the purge threads remove it: the history "
                  "list grows without bound. Capping the threads inside InnoDB (or in the thread pool) at a time leaves purge CPU time. "
                  "Stability remedy: measured for a bounded history first, throughput second.",
                  {"threads_running_avg": tr_now, "cpus": cpus, "unbounded_growth": unb_hll, "thread_handling": v.get("thread_handling")},
                  impact=2, confidence=2)
    if have_run and v.get("log_bin") == "ON" and _int(v, "binlog_transaction_dependency_history_size", 25000) > 1000 and (s.get("cpu_busy") or 0) >= 80 \
            and wtype in ("oltp-write", "mixed") and "binlog_transaction_dependency_history_size" in v:
        b.add("writeset-history", "Smaller WRITESET dependency history", [("binlog_transaction_dependency_history_size", "1000")],
              "Every commit hashes its rows into the WRITESET history used by parallel replicas; on CPU-bound write loads a smaller history "
              "cut that cost (Percona measured up to 17% on 8.4). Replicas may apply less in parallel. Experiment.",
              {"cpu_busy": busy}, impact=1, confidence=1)
    if policy == "benchmark" and have_run and v.get("innodb_flush_method") == "O_DIRECT" and flavor != "mariadb" \
            and str(hw.get("filesystem") or "") in ("ext4", "xfs") and (s.get("data_fsyncs") or 0) > 50:
        b.add("o-direct-no-fsync", "Skip fsync after O_DIRECT data writes (TRADE-OFF)", [("innodb_flush_method", "O_DIRECT_NO_FSYNC")],
              f"{s.get('data_fsyncs'):.0f} data-file fsyncs/s. With O_DIRECT the data is already on the device; the extra fsync only "
              "persists file metadata, which XFS/ext4 handle on their own in most cases. Durability trade-off; needs a restart.",
              {"data_fsyncs": s.get("data_fsyncs"), "filesystem": hw.get("filesystem")}, impact=1, confidence=1)

    # ---------------- best practices --------------------------------------------
    # Settings that are not the default for compatibility reasons but are generally better. MySQL 8.4 changed these
    # defaults ("new defaults for InnoDB", WL#15660); older servers keep the 8.0 values. Applied as one bundle, kept
    # unless they regress (see practice_bundle()).
    ver = str(v.get("version") or "")
    old_mysql = flavor != "mariadb" and ver[:3] in ("8.0", "5.7")
    if old_mysql:
        for var, val, why in (
                ("innodb_change_buffering", "none", "Change buffering rarely pays off on SSD and adds merge work (MySQL 8.4 default: none)."),
                ("innodb_adaptive_hash_index", "OFF", "The adaptive hash index causes latch contention under concurrency (MySQL 8.4 default: OFF)."),
                ("innodb_use_fdatasync", "ON", "fdatasync() skips needless metadata flushes (MySQL 8.4 default: ON)."),
                ("innodb_doublewrite_pages", "128", "Larger doublewrite batches (MySQL 8.4 default: 128)."),
                ("innodb_buffer_pool_in_core_file", "OFF", "Keeps the buffer pool out of core dumps (MySQL 8.4 default: OFF)."),
                ("innodb_page_cleaners", str(_int(v, "innodb_buffer_pool_instances", 1)), "One page cleaner per buffer pool instance (MySQL 8.4 default)."),
        ):
            if var in v:
                b.add(f"practice-{var}", why.split(" (")[0], [(var, val)], why, {var: v.get(var)}, impact=1, confidence=2)
    if flavor == "mariadb" and v.get("innodb_autoinc_lock_mode") == "1" and (v.get("log_bin") != "ON" or v.get("binlog_format") == "ROW"):
        b.add("practice-autoinc-lock", "Interleaved auto-increment locking", [("innodb_autoinc_lock_mode", "2")],
              "Mode 2 lets concurrent inserts allocate auto-increment values without a table-level lock; safe with row-based "
              "binary logging (MySQL 8.0+ default).", {"innodb_autoinc_lock_mode": v.get("innodb_autoinc_lock_mode"), "binlog_format": v.get("binlog_format")},
              impact=1, confidence=2)

    # ---------------- concurrency (experiments) ----------------------------------
    tr = s.get("threads_running_avg") or 0
    selects = rates.get("Com_select", 0)
    writes = sum(rates.get(k, 0) for k in ("Com_insert", "Com_update", "Com_delete", "Com_replace"))
    if have_run and "innodb_adaptive_hash_index" in v:
        # no cut-off on the B-tree share (a 15% threshold switched it off at 13-14% on 294 / 299 / 300 and on at 17% on the
        # canaries): any measured B-tree time makes it a candidate; how much time it acts on (relevance, from the breakdown)
        # and its record in comparable sessions (294: -7.5%) rank it. Without a CPU profile: a busy CPU stands in for it
        cats_ = (inp.get("cpu_profile") or {}).get("categories")
        btree = (cats_.get("B-tree / row access") or 0) if cats_ else None  # profiled: 0 when none of it is B-tree work
        if not _on(v, "innodb_adaptive_hash_index") and selects > writes and (s.get("bp_hit_pct") or 0) > 98 \
                and ((btree or 0) > 0 or (btree is None and busy >= 70)):
            b.add("ahi-on", "Try the adaptive hash index", [("innodb_adaptive_hash_index", "ON")],
                  "Read-mostly workload with a hot in-memory working set" + (f", {btree:.0f}% of the database CPU in B-tree / row access"
                                                                             if btree else "") +
                  ": AHI can shortcut B-tree descents for point lookups (its latches can cost more than it saves at high "
                  "concurrency or with writes). Experiment: keep only if the goal metric improves.",
                  {"selects_per_s": selects, "writes_per_s": round(writes, 1), "bp_hit_pct": s.get("bp_hit_pct"), "cpu_btree_pct": btree},
                  impact=2, confidence=1)
        ahi_wait = sum(w["time_s"] for w in inp.get("top_waits", []) if "btr_search" in w["event"] or "ahi" in w["event"].lower())
        sem = (inp.get("innodb_status") or {}).get("semaphores", "")
        if _on(v, "innodb_adaptive_hash_index") and (ahi_wait > 0 or "btr0sea" in sem):
            b.add("ahi-off", "Disable the adaptive hash index (contention)", [("innodb_adaptive_hash_index", "OFF")],
                  "Threads are waiting on AHI latches.", {"ahi_wait_s": ahi_wait}, impact=2, confidence=2)
    # not where commits dominate (the log threads do the grouping: lab 205 -38%, 217 -46%) or at high concurrency (174 -15%)
    # without an fsync per commit (trx_commit 0/2) on saturated CPUs the hand-offs to the log threads are the cost, at any
    # concurrency: 9 of 10 wins, +7.7% to +25.4% (h2h W1 285/288: 250 running threads on 12 CPUs, the gate below kept it
    # out of the rules arm, 283). With fsync per commit the log threads do the grouping: -38% / -46% (205, 217)
    no_fsync = str(v.get("innodb_flush_log_at_trx_commit", "1")) in ("0", "2")
    lw_ok = (cpus <= 16 and busy >= 60 and commit_sh_ < 40 and (s.get("threads_running_avg") or 0) <= 8 * cpus) or (no_fsync and busy >= 85)
    if have_run and "innodb_log_writer_threads" in v and _on(v, "innodb_log_writer_threads") and lw_ok:
        b.add("log-writer-threads-off", "Disable dedicated redo log writer threads", [("innodb_log_writer_threads", "OFF")],
              (f"Commits do not wait for an fsync (innodb_flush_log_at_trx_commit={v.get('innodb_flush_log_at_trx_commit')}) and the "
               f"{cpus} CPUs are {busy:.0f}% busy: handing every commit to the log writer/flusher threads costs context switches "
               "and run-queue waits. Measured +8% to +25% in such conditions (9 of 10). Experiment." if no_fsync and busy >= 85 else
               f"On {cpus} CPUs that are {busy:.0f}% busy the dedicated log writer/flusher threads compete with user threads; "
               "MySQL docs suggest disabling them on small systems. Experiment."),
              {"cpus": cpus, "cpu_busy": busy, "innodb_flush_log_at_trx_commit": v.get("innodb_flush_log_at_trx_commit")},
              impact=3 if no_fsync and busy >= 85 else 2, confidence=2 if no_fsync and busy >= 85 else 1)
    if have_run and busy >= 80 and "innodb_sync_spin_loops" in v and _int(v, "innodb_sync_spin_loops", 30) > 10 and tr > cpus:
        b.add("spin-loops", "Spin less before sleeping on mutexes", [("innodb_sync_spin_loops", "10")],
              f"CPU saturated with {tr:.0f} running threads on {cpus} CPUs: shorter spinning gives cycles back to useful work. Experiment.",
              {"cpu_busy": busy, "threads_running_avg": tr, "innodb_sync_spin_loops": v.get("innodb_sync_spin_loops")}, impact=2, confidence=1)
    # runnable threads as the scheduler sees them: sampled Threads_running undercounts when the run queue is long (C3:
    # Threads_running 12-14 while ~500 threads waited for a CPU at 97% run-queue wait - the pool was never proposed)
    active = max(tr, (s.get("dbproc_runq_wait_threads") or 0) + min(cpus, busy / 100.0 * cpus))
    # the pool exists only where thread_pool_size does (Percona Server, MariaDB, MySQL Enterprise): MySQL Community has
    # thread_handling but not its pool-of-threads value
    # and only at high concurrency: it regressed in every 64-connection cell with a co-located client (-4% to -29%, r1)
    # and at 2x vCPUs (user sessions), and won at 1024 connections on 16 vCPUs (HC-1 +9%)
    conns = s.get("threads_connected_avg") or 0
    high_conc = conns >= 16 * cpus  # a deep run queue alone was not enough: r1 RO-1 (64 on 8 vCPUs, 72% queued) lost 3.6%
    if have_run and high_conc and "thread_handling" in v and "thread_pool_size" in v and v.get("thread_handling") == "one-thread-per-connection" and \
            (active > cpus * 4 and busy >= 70 or active > cpus and (s.get("dbproc_runq_wait_pct") or 0) >= 15):
        tr = active
        b.add("thread-pool", "Try the thread pool", [("thread_handling", "pool-of-threads")],
              f"{tr:.0f} concurrently running threads on {cpus} CPUs; a thread pool caps active threads and reduces context switching. "
              "Can lower throughput for commit-heavy loads (smaller group commits). Experiment.",
              {"threads_running_avg": tr, "cpus": cpus, "ctx_switches": (inp.get("os_rates") or {}).get("ctx_switches"),
               "dbproc_runq_wait_pct": s.get("dbproc_runq_wait_pct")}, impact=2, confidence=1)

    if any(i["id"] == "purge-threadpool" for i in b.items):
        b.items = [i for i in b.items if i["id"] != "thread-pool"]

    # ---------------- kernel time (CPU profile: where the kernel spends mysqld's CPU) ----------------
    kp = (inp.get("cpu_profile") or {}).get("kernel") or {}
    ka = kp.get("areas") or {}
    if have_run and kp.get("pct"):
        mit = str((ctx.get("os_facts") or {}).get("os:cpu_mitigations") or "")
        sysc = ka.get("syscall entry / exit", 0)
        if sysc >= 3 and mit and mit not in ("off", "none costly"):
            b.add("kernel-mitigations", "CPU vulnerability mitigations cost kernel entry and exit", [],
                  f"{kp['pct']:.0f}% of mysqld's CPU is kernel time; {sysc:.1f}% is syscall entry / exit alone, which these mitigations make "
                  f"expensive ({mit[:300]}), and IBRS / buffer clearing slow kernel code beyond that. On a dedicated, single-tenant "
                  "database host running only trusted code, booting with mitigations=off removes that cost - a security trade-off "
                  "(other tenants or untrusted code could read memory across boundaries), never applied by the tuner. Needs a reboot.",
                  {"kernel_pct": kp["pct"], "syscall_entry_exit_pct": sysc, "mitigations": mit}, impact=2, confidence=1, kind="advice")
            b.items[-1]["tradeoffs"] = [{"category": "security", "lost": "protection against CPU side-channel attacks between processes / tenants"}]
        net = ka.get("network stack", 0)
        if net >= 5:
            b.add("kernel-network", "Kernel network stack time", [],
                  f"{net:.1f}% of mysqld's CPU is the kernel network stack (TCP send / receive, wake-ups). A client on the same host is "
                  "faster over the Unix socket (no TCP); a separate client host moves its half off this machine. Interrupt placement "
                  "(IRQ split) and fewer, larger round trips (batching, prepared statements) cut it too.",
                  {"network_stack_pct": net, "kernel_pct": kp["pct"]}, impact=1, confidence=2, kind="advice")
        sch = ka.get("scheduling / futex", 0)
        if sch >= 8 and (s.get("threads_running_avg") or 0) > 2 * cpus:
            b.add("kernel-scheduling", "Kernel time switching threads", [],
                  f"{sch:.1f}% of mysqld's CPU is the kernel scheduling threads (futex wake-ups, context switches) with "
                  f"{s.get('threads_running_avg', 0):.0f} threads running on {cpus} CPUs: fewer runnable threads - a concurrency cap "
                  "(innodb_thread_concurrency) or the thread pool - cut this directly; the experiments that do so are ranked with it.",
                  {"scheduling_futex_pct": sch, "threads_running_avg": s.get("threads_running_avg"), "cpus": cpus},
                  impact=2, confidence=2, kind="advice")

    # ---------------- connections / caches ----------------------------------------
    if have_run and rates.get("Opened_tables", 0) > 1:
        toc = _int(v, "table_open_cache", 4000)
        b.add("table-open-cache", "Larger table open cache", [("table_open_cache", str(toc * 2))],
              f"{rates['Opened_tables']:.1f} table opens/s: the cache is thrashing.", {"Opened_tables_per_s": rates.get("Opened_tables"), "table_open_cache": toc}, impact=1, confidence=3)
    if have_run and rates.get("Opened_table_definitions", 0) > 0.5:
        tdc = _int(v, "table_definition_cache", 2000)
        b.add("table-def-cache", "Larger table definition cache", [("table_definition_cache", str(tdc * 2))],
              "Table definitions are being re-read.", {"Opened_table_definitions_per_s": rates.get("Opened_table_definitions")}, impact=1, confidence=3)
    if have_run and rates.get("Threads_created", 0) > 1:
        tc = _int(v, "thread_cache_size", 9)
        b.add("thread-cache", "Larger thread cache", [("thread_cache_size", str(max(64, tc * 2)))],
              f"{rates['Threads_created']:.1f} threads created/s: connection churn.", {"Threads_created_per_s": rates.get("Threads_created")}, impact=1, confidence=3)
    status_max_used = None
    st = inp.get("db_rates") or {}
    if have_run and (s.get("threads_connected_avg") or 0) > max_conn * 0.85:
        new = int(max_conn * 1.5)
        b.add("max-connections", "Raise max_connections", [("max_connections", str(new))],
              "Connections are close to the limit.", {"threads_connected_avg": s.get("threads_connected_avg"), "max_connections": max_conn}, impact=2, confidence=2)
    if have_run:
        tmp = rates.get("Created_tmp_disk_tables", 0)
        tot = rates.get("Created_tmp_tables", 0)
        if tmp > 1 and tot and tmp / tot > 0.1:
            cur = min(_int(v, "tmp_table_size", 16 * MiB), _int(v, "max_heap_table_size", 16 * MiB))
            new = fmt_size(min(cur * 4, 256 * MiB))
            b.add("tmp-tables", "Larger in-memory temporary tables", [("tmp_table_size", new), ("max_heap_table_size", new)],
                  f"{100 * tmp / tot:.0f}% of temporary tables spill to disk ({tmp:.1f}/s).", {"tmp_disk_per_s": tmp, "tmp_per_s": tot}, impact=1, confidence=2)
        if rates.get("Sort_merge_passes", 0) > 1:
            sb = _int(v, "sort_buffer_size", 256 * 1024)
            b.add("sort-buffer", "Larger sort buffer", [("sort_buffer_size", fmt_size(min(sb * 4, 4 * MiB)))],
                  "Sorts spill to merge passes.", {"Sort_merge_passes_per_s": rates.get("Sort_merge_passes")}, impact=1, confidence=2)

    # ---------------- durability / observability trade-offs (benchmark policy) ----
    commit_share = sum(p.get("share_pct") or 0 for p in ctx.get("breakdown") or [] if "commit" in (p.get("tags") or ()))
    commit_bound = any(d["type"] == "commit" for d in ctx.get("diagnosis", [])) or (
        (s.get("tps") or 0) > 0 and (s.get("data_fsyncs") or 0) / max(s.get("tps") or 1, 1) > 0.3) or (
        # measured time: commit / group-commit waiting beyond what noise could hide (the evidence gate's own rule)
        commit_share > float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0))
    if goal in ("tps", "latency") and have_run and commit_bound:
        dur_kind = "config" if policy == "benchmark" else "advice"
        if _int(v, "innodb_flush_log_at_trx_commit", 1) == 1:
            b.add("trx-commit-2", "Relax redo flushing at commit (DURABILITY TRADE-OFF)", [("innodb_flush_log_at_trx_commit", "2")],
                  "Commits wait for an fsync of the redo log. Value 2 writes at commit and fsyncs once per second: "
                  "a power loss/OS crash can lose ~1 s of committed transactions. Only acceptable where that loss is acceptable.",
                  {"data_fsyncs": s.get("data_fsyncs"), "tps": s.get("tps"), "disk_await_ms": s.get("disk_await_ms")}, impact=3, confidence=3, kind=dur_kind)
        if v.get("log_bin") == "ON" and _int(v, "sync_binlog", 1) == 1:
            # sync_binlog before binlog off (user): the binary log stays for replicas / PITR, and when relaxing its fsync
            # removes the commit backlog, turning the log off has little left to win
            b.add("sync-binlog", "Relax binlog fsync (DURABILITY TRADE-OFF)", [("sync_binlog", "1000")],
                  "Binary log fsync at every commit group. Relaxing can lose binlog events on OS crash (replicas/PITR diverge). "
                  "Tried before turning the binary log off: it keeps the log, and if the commit backlog goes away there is little "
                  "left for binlog-off to win.",
                  {"data_fsyncs": s.get("data_fsyncs"), "tps": s.get("tps"), "commit_wait_pct": round(commit_share, 1)}, impact=3, confidence=2, kind=dur_kind)
    page_writes = rates.get("Innodb_pages_written", 0) or rates.get("Innodb_dblwr_pages_written", 0)
    if have_run and wtype in ("oltp-write", "mixed") and "innodb_doublewrite" in v and str(v.get("innodb_doublewrite")).upper() == "ON" \
            and rates.get("Innodb_dblwr_pages_written", 0) > 50:
        b.add("doublewrite-off", "Disable the doublewrite buffer (DURABILITY TRADE-OFF)", [("innodb_doublewrite", "OFF")],
              f"{rates.get('Innodb_dblwr_pages_written', 0):.0f} pages/s are written twice. Without doublewrite a crash during a page write "
              "can leave a torn page that InnoDB cannot repair.", {"Innodb_dblwr_pages_written_per_s": rates.get("Innodb_dblwr_pages_written")},
              impact=2, confidence=2, kind="config" if policy == "benchmark" else "advice")
    if have_run and v.get("log_bin") == "ON" and wtype in ("oltp-write", "mixed") and commit_bound \
            and not (_int(v, "sync_binlog", 1) == 1 and any(i["id"] == "sync-binlog" for i in b.items)):
        # only after sync_binlog was relaxed (or cannot be) and the commit path still waits: the binary log's remaining
        # cost is its write and group-commit stage, which only significant writes make large (user, 2026-10-25)
        b.add("binlog-off", "Run without the binary log (REPLICATION / PITR TRADE-OFF)", [("disable_log_bin", "")],
              "Every commit is a two-phase commit between the binlog and InnoDB with its own fsync. Without the binlog there is no replication "
              "and no point-in-time recovery.", {"fsyncs_per_trx": s.get("fsyncs_per_trx"), "log_bin": v.get("log_bin")},
              impact=3, confidence=2, kind="config" if policy == "benchmark" else "advice")
    # measured cost, not a CPU threshold: performance_schema's share of the CPU profile (r1: 15-24% in six cells; ps-off
    # vanished when cpu_busy fell to 79.8%)
    pfs_sh_ = cpu_cat_.get("performance_schema")
    pfs_costly = (pfs_sh_ is not None and pfs_sh_ * (s.get("mysqld_cpu_pct") or busy) / 100 >= max(noise_, 5) * 0.5) \
        or (pfs_sh_ is None and busy >= 80)
    if policy == "benchmark" and have_run and pfs_costly and v.get("ps:profile") == "full":
        pfs = ((inp.get("cpu_profile") or {}).get("categories") or {}).get("performance_schema")
        b.add("ps-lite", "Lighter performance_schema (OBSERVABILITY TRADE-OFF)", [("ps:profile", "lite")],
              "Keeps what tuning needs (statement digests, latency histogram, wait summaries) and stops statement/transaction "
              "history and memory accounting, which cost CPU on every statement and allocation. Measured: full instrumentation "
              f"costs ~4.5% on HammerDB vs none.{f' The CPU profile shows {pfs:.0f}% of database CPU in performance_schema.' if pfs else ''}",
              {"cpu_busy": busy, "pfs_cpu_pct": pfs}, impact=2, confidence=2)
    if policy == "benchmark" and have_run and goal == "tps" and pfs_costly and _on(v, "performance_schema") and v.get("ps:profile") != "full":
        b.add("ps-off", "Disable Performance Schema (OBSERVABILITY TRADE-OFF)", [("performance_schema", "OFF")],
              "On a CPU-bound server instrumentation costs a few percent of CPU. You lose statement latency measurement "
              "(the tuner will rely on throughput only) and most diagnostics.", {"cpu_busy": busy}, impact=2, confidence=2)
    if policy == "benchmark" and have_run and "binlog_row_image" in v and v.get("binlog_row_image") == "FULL" and v.get("log_bin") == "ON" and redo_mbs > 1:
        b.add("binlog-minimal", "Log minimal row images", [("binlog_row_image", "MINIMAL")],
              "Smaller binlog writes for update-heavy workloads; downstream consumers needing full images (CDC) break.",
              {"redo_mbs": redo_mbs}, impact=1, confidence=1)

    # ---------------- workload-specific rules ---------------------------------------
    conc = max(1.0, s.get("threads_running_avg") or 1)
    if have_run and wtype in ("olap", "mixed"):
        mem_budget = ram * 0.10  # per-query buffers may use up to ~10% of RAM across concurrently running queries
        if rates.get("Select_full_join", 0) > 0 or rates.get("Select_scan", 0) > 0.2:
            jb = _int(v, "join_buffer_size", 256 * 1024)
            want = int(min(64 * MiB, max(jb * 8, 8 * MiB), mem_budget / conc))
            if want > jb * 1.5:
                b.add("join-buffer", "Larger join buffer for analytical joins", [("join_buffer_size", fmt_size(want // MiB * MiB or want))],
                      f"Analytical workload: {rates.get('Select_full_join', 0):.2f} joins/s without usable indexes (hash joins in MySQL 8.0+ "
                      "use join_buffer_size; when the build side does not fit, the join spills to disk in chunks). "
                      f"Sized for ~{conc:.0f} concurrent queries within 10% of RAM.",
                      {"Select_full_join_per_s": rates.get("Select_full_join"), "Select_scan_per_s": rates.get("Select_scan"),
                       "threads_running_avg": conc, "join_buffer_size": jb}, impact=2, confidence=2)
        if rates.get("Sort_merge_passes", 0) > 0.05:
            sb = _int(v, "sort_buffer_size", 256 * 1024)
            want = int(min(32 * MiB, max(sb * 8, 4 * MiB), mem_budget / conc))
            if want > sb * 1.5:
                b.add("sort-buffer-olap", "Larger sort buffer for analytical sorts", [("sort_buffer_size", fmt_size(want // MiB * MiB or want))],
                      f"Sorts spill to merge passes ({rates['Sort_merge_passes']:.2f}/s) in an analytical workload with ~{conc:.0f} concurrent queries.",
                      {"Sort_merge_passes_per_s": rates.get("Sort_merge_passes"), "sort_buffer_size": sb}, impact=2, confidence=2)
        tmpd, tmpa = rates.get("Created_tmp_disk_tables", 0), rates.get("Created_tmp_tables", 0)
        if tmpd > 0 and tmpa and tmpd / tmpa > 0.02:
            cur = min(_int(v, "tmp_table_size", 16 * MiB), _int(v, "max_heap_table_size", 16 * MiB))
            want = int(min(512 * MiB, max(cur * 4, 128 * MiB), mem_budget / conc))
            ch = [("tmp_table_size", fmt_size(want // MiB * MiB)), ("max_heap_table_size", fmt_size(want // MiB * MiB))]
            if "temptable_max_ram" in v and _int(v, "temptable_max_ram") < want * conc:
                ch.append(("temptable_max_ram", fmt_size(int(min(ram * 0.15, max(want * conc, GiB))) // MiB * MiB)))
            if want > cur * 1.5:
                b.add("tmp-olap", "Keep analytical temporary tables in memory", ch,
                      f"{100 * tmpd / tmpa:.0f}% of internal temporary tables go to disk ({tmpd:.2f}/s) for GROUP BY / DISTINCT / derived tables.",
                      {"tmp_disk_per_s": tmpd, "tmp_per_s": tmpa, "tmp_table_size": cur}, impact=2, confidence=2)
        pass  # read-ahead experiments: see "read path" below (any read-bound workload, not only scans)
        top = (inp.get("top_statements") or [])
        timeouts = rates.get("Max_execution_time_exceeded", 0)
        sub = [q for q in top if q.get("pct_time", 0) >= 5 and str(q.get("query", "")).upper().count("SELECT") >= 2]
        if "subquery_to_derived=off" in str(v.get("optimizer_switch", "")) and (sub or timeouts > 0):
            b.add("subquery-to-derived", "Rewrite correlated scalar subqueries as derived tables",
                  [("optimizer_switch", "subquery_to_derived=on")],
                  "Long-running statements contain nested subqueries"
                  + (f" and {timeouts * (s.get('duration_s') or 0):.0f} statement(s) hit max_execution_time" if timeouts else "")
                  + ". With subquery_to_derived the optimizer turns correlated scalar subqueries (e.g. TPC-H Q17/Q20 style "
                  "\"x < (SELECT 0.2*AVG(..) WHERE corr)\") into a derived table computed once and hash-joined, instead of "
                  "re-running the subquery per outer row. Same results; off by default in MySQL 8.0/8.4. Experiment.",
                  {"statements_with_subqueries": [q["query"][:100] for q in sub[:3]], "Max_execution_time_exceeded_per_s": timeouts},
                  impact=3, confidence=2)
        if top and any(q.get("pct_time", 0) > 10 and q.get("rows_examined_avg", 0) > 100000 for q in top):
            b.add("stats-histograms", "Refresh optimizer statistics for the big tables", [],
                  "Long analytical queries examine millions of rows; join order depends on index statistics. Run ANALYZE TABLE on the "
                  "large tables (optionally with innodb_stats_persistent_sample_pages=128 first) and add histograms on filtered "
                  "non-indexed columns (ANALYZE TABLE t UPDATE HISTOGRAM ON col). A config change alone has no effect until "
                  "statistics are recalculated.",
                  {"top_statement_rows_examined": max(q.get("rows_examined_avg", 0) for q in top)}, impact=1, confidence=2, kind="advice")
        key_reads = rates.get("Handler_read_key", 0)
        osw = str(v.get("optimizer_switch", ""))
        if "batched_key_access=off" in osw and key_reads > 1000 and (rates.get("Select_full_join", 0) > 0 or rates.get("Select_scan", 0) > 0):
            ch = [("optimizer_switch", "batched_key_access=on,mrr_cost_based=off")]
            if _int(v, "read_rnd_buffer_size", 262144) < 4 * MiB:
                ch.append(("read_rnd_buffer_size", "4M"))
            b.add("bka-mrr", "Batched Key Access joins with Multi-Range Read", ch,
                  f"{key_reads:.0f} index lookups/s in an analytical workload: BKA collects join keys in the join buffer and MRR fetches "
                  "the matching rows in primary-key order (fewer random page accesses). Off by default because the cost model is "
                  "conservative. Same results. Experiment.",
                  {"Handler_read_key_per_s": key_reads, "Select_full_join_per_s": rates.get("Select_full_join")}, impact=2, confidence=1)
    commit_diag = any(d["type"] == "commit" for d in ctx.get("diagnosis") or [])
    w_ms = max(s.get("disk_w_await_ms") or 0, s.get("disk_flush_await_ms") or 0)
    gc0 = group_commit(s, inp, ctx.get("breakdown"), v.get("log_bin"))
    if have_run and commit_diag and w_ms >= 5 and not (gc0 and gc0.get("waiters") and not gc0["complete"]):  # loose grouping: tune it first
        why = "the drive's write cache is off" if v.get("os:disk_write_cache") == "OFF" else ""
        b.add("commit-storage-latency", "Commits wait for slow storage writes",
              [], f"Each write to the data device takes {w_ms:.1f} ms (fast SSD: well under 1 ms) and commits wait for it"
              + (f"; {why}" if why else "") + "." + (f" {gc_text(gc0)[0].upper() + gc_text(gc0)[1:]}." if gc0 else "")
              + " What remains is the storage: a drive with "
              "power-loss protection, the drive write cache (a durability trade-off without it), a separate fast device for the "
              "redo log, or relaxing durability (innodb_flush_log_at_trx_commit = 2). Server settings cannot make the device faster.",
              {"disk_w_await_ms": s.get("disk_w_await_ms"), "disk_flush_await_ms": s.get("disk_flush_await_ms"),
               "data_fsyncs": s.get("data_fsyncs"), "tps": s.get("tps"), "log_bin": v.get("log_bin"),
               "disk_write_cache": v.get("os:disk_write_cache")}, impact=3, confidence=2, kind="advice")
    if have_run and wtype in ("oltp-write", "mixed"):
        gc = group_commit(s, inp, ctx.get("breakdown"), v.get("log_bin"))
        # worth it when the batches hold far fewer commits than are waiting (or fsyncs per transaction stay high): a short
        # wait lets the waiting commits join; with complete grouping it only adds latency
        loose = (gc and gc.get("waiters") and not gc["complete"] and gc["waiters"] >= 4) or (
            not (gc and gc.get("waiters")) and (s.get("fsyncs_per_trx") or 0) > 0.8 and (s.get("threads_running_avg") or 0) >= 4)
        if v.get("log_bin") != "ON" and gc and gc.get("waiters") and not gc["complete"] and gc["waiters"] >= 4 \
                and "innodb_log_writer_threads" in v and not _on(v, "innodb_log_writer_threads"):
            # InnoDB's own group commit (no binary log): the dedicated log writer / flusher threads gather the waiting
            # commits into one write and flush; without them user threads take turns
            b.add("log-writer-threads-on", "Let dedicated log threads group the commits", [("innodb_log_writer_threads", "ON")],
                  f"{gc_text(gc)[0].upper() + gc_text(gc)[1:]}. Dedicated log writer / flusher threads gather all waiting commits into one "
                  "write and flush. Experiment.", {"group_commit": gc}, impact=2, confidence=1)
        if v.get("log_bin") == "ON" and "binlog_group_commit_sync_delay" in v and _int(v, "binlog_group_commit_sync_delay") == 0 and loose:
            w_ms = max(s.get("disk_w_await_ms") or 0, s.get("disk_flush_await_ms") or 0)
            delay = nice_value("binlog_group_commit_sync_delay", int(min(2000, max(100, 250 * w_ms))) if w_ms else 200)  # a fraction of one flush
            count = nice_value("binlog_group_commit_sync_no_delay_count", int(max(4, min(1000, (gc or {}).get("waiters") or 16))))  # flush early once the waiters joined
            b.add("group-commit-delay", "Grow binlog commit groups", [("binlog_group_commit_sync_delay", str(delay)),
                                                                       ("binlog_group_commit_sync_no_delay_count", str(count))],
                  (f"{gc_text(gc)[0].upper() + gc_text(gc)[1:]}. " if gc else f"{s.get('fsyncs_per_trx'):.2f} fsyncs per transaction with "
                   f"{s.get('threads_running_avg'):.0f} running threads. ")
                  + f"Waiting up to {delay} µs (or until {count} commits joined) lets more transactions share one flush. "
                  "Durability unchanged; adds latency when the commits do not come. Experiment.",
                  {"group_commit": gc, "fsyncs_per_trx": s.get("fsyncs_per_trx"), "threads_running_avg": s.get("threads_running_avg")},
                  impact=2, confidence=1)
        # change buffering is a both-ways experiment like the adaptive hash index (and the default flipped: 8.4 none, 9.7 all):
        # it saves secondary-index page reads on writes, but costs merge work, buffer pool space and latching
        cb = str(v.get("innodb_change_buffering") or "").lower()
        if cb == "none" and (s.get("bp_hit_pct") or 100) < 99.5 and writes > 50:
            b.add("change-buffer", "Buffer secondary index changes", [("innodb_change_buffering", "all")],
                  "Write-heavy workload whose secondary-index pages are often not in memory: the change buffer defers those reads "
                  f"({(s.get('bp_hit_pct') or 0):.2f}% buffer pool hit ratio, {writes:.0f} row changes/s), at the cost of merge work and "
                  "slower crash recovery. Experiment.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "writes_per_s": round(writes, 1)}, impact=2, confidence=1)
            b.items[-1]["lead"] = "Published benchmarks report a large loss without it on I/O-bound inserts; our own A/B measured about -23% (cold regime). Unverified for this workload."
        elif cb and cb != "none" and writes > 50:
            b.add("change-buffer-off", "Disable change buffering", [("innodb_change_buffering", "none")],
                  f"Write-heavy workload with change buffering '{cb}' (9.7 default): merges of buffered secondary-index changes compete "
                  "with foreground work and take buffer pool space; on fast storage reading the page directly can be cheaper "
                  "(MySQL 8.4 default and Percona's 2026 benchmark config: none). Experiment.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "writes_per_s": round(writes, 1), "innodb_change_buffering": cb}, impact=2, confidence=1)
        lw = s.get("row_lock_waits") or 0
        if lw > 1 and (s.get("tps") or 0) > 0:
            hot = [q for q in (inp.get("top_statements") or []) if q.get("lock_ms_avg", 0) > 1][:3]
            b.add("row-locks", "Row lock contention is a workload property", [],
                  f"{lw:.1f} row lock waits/s (avg {s.get('row_lock_wait_avg_ms') or 0:.1f} ms). Configuration cannot remove hot-row contention "
                  "(e.g. TPC-C district/warehouse rows); shorter transactions, consistent lock order or fewer concurrent clients on the same rows can.",
                  {"row_lock_waits_per_s": lw, "hot_statements": [q["query"][:80] for q in hot]}, impact=1, confidence=2, kind="advice")

    diag_types_early = {d["type"] for d in ctx.get("diagnosis") or [] if d.get("severity") in ("high", "medium")}
    # InnoDB background threads on their own CPUs (MySQL resource groups): flushing bursts stop preempting queries.
    if have_run and "rg:innodb_bg_cpus" in v and v.get("rg:innodb_bg_cpus") == "off" and cpus >= 4 and wtype in ("oltp-write", "mixed") \
            and ("sched-delay" in diag_types_early or (ctx.get("goal") == "latency" and (s.get("dbproc_runq_wait_pct") or 0) >= 5)):
        n = max(1, cpus // 4)
        bg = f"{cpus - n}-{cpus - 1}" if n > 1 else str(cpus - 1)
        b.add("rg-innodb-bg", "Run InnoDB background threads on dedicated CPUs", [("rg:innodb_bg_cpus", bg)],
              f"Query threads wait for CPU ({s.get('dbproc_runq_wait_pct') or 0:.0f}% of runnable time) on a write-heavy load. A MySQL "
              f"resource group keeps page cleaners, purge and log threads on CPU(s) {bg}, so flushing bursts do not preempt queries. "
              "Background work gets fewer CPUs: watch checkpoint age. Experiment.",
              {"dbproc_runq_wait_pct": s.get("dbproc_runq_wait_pct"), "cpus": cpus, "checkpoint_age_pct_max": s.get("checkpoint_age_pct_max")},
              impact=1, confidence=1)

    # ---------------- operating system ---------------------------------------------
    # NUMA: memory placement on multi-node hosts (the buffer pool interleave is a server setting: also without OS tuning)
    _numa_rules(b, v, s, ctx.get("os_facts") or {}, ctx, have_run, bp, ram)
    if ctx.get("os_tuning") and have_run:
        osf = ctx.get("os_facts") or {}
        busy_ = s.get("cpu_busy") or 0
        try:
            sw = int(v.get("os:vm.swappiness", "0"))
        except ValueError:
            sw = 0
        if sw > 10:
            b.add("os-swappiness", "Keep database memory out of swap (vm.swappiness=1)", [("os:vm.swappiness", "1")],
                  f"vm.swappiness={sw} lets the kernel swap out buffer pool pages to keep page cache; 1 swaps only under real memory pressure.",
                  {"vm.swappiness": sw, "swap_in": s.get("swap_in"), "swap_out": s.get("swap_out"), "psi_memory_some": s.get("psi_memory_some")},
                  impact=2 if swapping else 1, confidence=3)
        if v.get("os:thp") == "always":
            b.add("os-thp-off", "Disable Transparent Huge Pages", [("os:thp", "never")],
                  "InnoDB accesses memory sparsely; THP compaction and khugepaged add latency spikes and memory bloat (worse with jemalloc).",
                  {"transparent_hugepage": v.get("os:thp")}, impact=2, confidence=2)
        if ssd and v.get("os:scheduler") not in (None, "none", "noop", "mq-deadline"):
            b.add("os-scheduler", "Use the 'none' I/O scheduler on SSD", [("os:scheduler", "none")],
                  f"Scheduler {v.get('os:scheduler')} reorders requests for rotating disks; SSD/NVMe/virtual disks do better without.",
                  {"scheduler": v.get("os:scheduler")}, impact=1, confidence=2)
        if v.get("os:mount_noatime") == "OFF":
            b.add("os-noatime", "Mount the data filesystem with noatime", [("os:mount_noatime", "ON")],
                  "Access-time updates turn reads into metadata writes; databases never need them.",
                  {"filesystem": osf.get("os:filesystem")}, impact=1, confidence=2)
        # only on a measured build-up of dirty page cache: redo / binary log fsync'ed many times a second never accumulate
        # much (each fsync writes it out), so commit-bound load alone is no evidence. Data files without O_DIRECT, a binary
        # log with sync_binlog = 0 or temporary files can pile up gigabytes, and the writeback of that pile stalls fsyncs.
        dirty_max = s.get("page_cache_dirty_mb_max")
        if dirty_max is not None and dirty_max >= 256 and v.get("os:vm.dirty_bytes") == "0" and v.get("os:vm.dirty_background_bytes") == "0" \
                and ram >= 2 * GiB and (s.get("tput_dip_samples") or (s.get("disk_await_ms") or 0) >= 5 or commit_bound):
            b.add("os-dirty-writeback", "Write back dirty page cache earlier", [("os:vm.dirty_background_bytes", str(64 * MiB)),
                                                                                 ("os:vm.dirty_bytes", str(512 * MiB)),
                                                                                 ("os:vm.dirty_expire_centisecs", "500"),
                                                                                 ("os:vm.dirty_writeback_centisecs", "100")],
                  f"Up to {dirty_max:,.0f} MB of written data waited in the page cache during the run (average "
                  f"{s.get('page_cache_dirty_mb_avg') or 0:,.0f} MB) under the default ratio limits; writing that out in bulk competes "
                  "with redo / binary log fsyncs. Small byte limits keep writeback small and steady. Experiment.",
                  {"page_cache_dirty_mb_max": dirty_max, "page_cache_dirty_mb_avg": s.get("page_cache_dirty_mb_avg"),
                   "log_fsyncs_per_s": s.get("log_fsyncs_per_s"), "disk_await_ms": s.get("disk_await_ms"),
                   "innodb_flush_method": v.get("innodb_flush_method")}, impact=1, confidence=1)
        alloc_pct = ((inp.get("cpu_profile") or {}).get("categories") or {}).get("memory allocator", 0)
        # with a profile, the allocator's measured share decides (allocator at 0.9-1.4% of the time: 7 neutral runs)
        profiled_ = bool((inp.get("cpu_profile") or {}).get("categories"))
        if v.get("os:malloc") == "glibc" and busy_ >= 70 and (alloc_pct >= 2 if profiled_ else (s.get("threads_running_avg") or 0) >= cpus):
            b.add("os-jemalloc", "Run mysqld with jemalloc", [("os:malloc", "jemalloc")],
                  f"CPU-bound ({busy_:.0f}%) with {s.get('threads_running_avg', 0):.0f} running threads: jemalloc has less allocator lock "
                  "contention and fragmentation than glibc malloc (Percona recommendation). Needs a mysqld restart. Experiment.",
                  {"cpu_busy": busy_, "threads_running_avg": s.get("threads_running_avg")}, impact=2, confidence=1)
        # headroom from what this host measured: huge pages are pinned, so mysqld's memory beyond the buffer pool and
        # everything else on the host must still fit in what is left (#124: the reservation ran the host out of memory)
        pages = int((bp + 192 * MiB) // (2 * MiB))
        db_mb = s.get("dbproc_pss_mb") or 0
        mysqld_other = max(0, db_mb * MiB - bp) if db_mb else 2 * GiB
        others = max(0, ram - mem_avail - db_mb * MiB) if db_mb else 0
        headroom = ram - pages * 2 * MiB - mysqld_other - others - GiB
        if "large_pages" in v and not _on(v, "large_pages") and bp >= 8 * GiB and ram >= 16 * GiB and not swapping and mem_avail > GiB \
                and headroom > 0:
            b.add("os-hugepages", "Back the buffer pool with huge pages", [("os:hugepages", str(pages)), ("large_pages", "ON")],
                  f"Reserves {pages} x 2 MiB pages for the {human_size(bp)} buffer pool: fewer TLB misses for memory-bound work "
                  "(MySQL docs / field reports: 5-30%). The memory is reserved even when unused; needs a restart. Experiment.",
                  {"innodb_buffer_pool_size": human_size(bp), "hugepages": pages, "mem_available": human_size(mem_avail)}, impact=2, confidence=1)
        my = s.get("mysqld_cpu_pct")
        if v.get("os:kernel.sched_autogroup_enabled") == "1" and my is not None and busy_ >= 85 and my < 0.8 * busy_ \
                and (s.get("threads_connected_avg") or 0) < OVERSUB_CONN_PER_CPU * cpus:
            b.add("os-autogroup", "Disable scheduler autogroups", [("os:kernel.sched_autogroup_enabled", "0")],
                  f"CPU saturated and mysqld gets only {my:.0f}% of {busy_:.0f}% busy CPU: other processes (e.g. the benchmark client) "
                  "run in separate autogroups that get an equal CPU share regardless of their thread count. tuned's postgresql profile "
                  "disables autogroups for database servers. Experiment.", {"mysqld_cpu_pct": my, "cpu_busy": busy_}, impact=1, confidence=1)
        try:
            aio_used, aio_max = int(osf.get("os:aio_in_use") or 0), int(v.get("os:fs.aio-max-nr") or 0)
        except ValueError:
            aio_used = aio_max = 0
        if aio_max and aio_used > 0.7 * aio_max:
            b.add("os-aio", "More asynchronous I/O slots", [("os:fs.aio-max-nr", str(max(1048576, aio_max * 2)))],
                  f"{aio_used} of {aio_max} AIO request slots in use: InnoDB falls back to simulated AIO when the limit is reached.",
                  {"aio_in_use": aio_used, "aio_max": aio_max}, impact=2, confidence=3)
        gov = v.get("os:cpu_governor")
        # a guest's governor does not set the host's clock (backtest: counted in the best-practice bundle on VMs, no effect)
        if gov and gov != "performance" and not is_vm(ctx.get("hw")):
            b.add("os-governor", "CPU frequency governor 'performance'", [("os:cpu_governor", "performance")],
                  f"Governor {gov} lowers clock speed between bursts, adding latency.", {"cpu_governor": gov}, impact=1, confidence=2)
        if v.get("os:mount_barrier") == "ON" and commit_bound and wtype in ("oltp-write", "mixed"):
            b.add("os-nobarrier", "Disable ext4 write barriers (DURABILITY TRADE-OFF)", [("os:mount_barrier", "OFF")],
                  "Barriers force the device to persist its write cache at every fsync. Without them a power loss can lose or reorder "
                  "writes the database already acknowledged.", {"fsyncs_per_trx": s.get("fsyncs_per_trx"), "disk_await_ms": s.get("disk_await_ms")},
                  impact=2, confidence=1, kind="config" if policy == "benchmark" else "advice")

    # Drive write cache off (common on SATA SSDs / HDDs behind RAID or set by a provider): every write waits for the media.
    # On measured slow writes / cache flushes, a durability trade-off like nobarrier: run under the benchmark policy only.
    if ctx.get("os_tuning") and v.get("os:disk_write_cache") == "OFF":
        w_ms = max(s.get("disk_w_await_ms") or 0, s.get("disk_flush_await_ms") or 0)
        io_diag = {d["type"] for d in ctx.get("diagnosis") or [] if d.get("severity") in ("high", "medium")} & {"io", "redo", "commit"}
        if have_run and w_ms >= 1.0 and (commit_bound or io_diag):
            b.add("os-disk-write-cache", "Turn the drive's write cache on (DURABILITY TRADE-OFF)", [("os:disk_write_cache", "ON")],
                  f"The data drive's volatile write cache is off and writes take {w_ms:.1f} ms on average: each one waits for the media. "
                  "With the cache on, writes complete in the drive's cache (typically well under 1 ms). Without power-loss "
                  "protection a power cut loses what is still in the cache: acceptable for a benchmark box, not for production "
                  "(datacenter drives with power-loss protection usually report no volatile cache at all).",
                  {"disk_w_await_ms": s.get("disk_w_await_ms"), "disk_flush_await_ms": s.get("disk_flush_await_ms"),
                   "fsyncs_per_trx": s.get("fsyncs_per_trx"), "disk_write_cache": "OFF"},
                  impact=2, confidence=1, kind="config" if policy == "benchmark" else "advice")

    # ---------------- network (decision table in docs/research/os-network-tuning.md) ----
    if have_run:
        ovf = (s.get("net_listen_overflows") or 0) + (s.get("net_listen_drops") or 0)
        if ovf > 0:
            ch = []
            if ctx.get("os_tuning") and _int(v, "os:net.core.somaxconn", 4096) < 4096:
                ch.append(("os:net.core.somaxconn", "4096"))
            bl = _int(v, "back_log", 80)
            ch.append(("back_log", str(max(bl * 2, min(max(max_conn, 512), 4096)))))
            b.add("net-listen-backlog", "Larger accept backlog: connections are being dropped", ch,
                  f"{ovf:.2f} listen-queue overflows/drops per second: bursts of new connections overflow mysqld's accept queue "
                  f"(back_log={bl}, capped by net.core.somaxconn) and clients see connect timeouts/retries.",
                  {"listen_overflows_per_s": s.get("net_listen_overflows"), "back_log": bl, "somaxconn": v.get("os:net.core.somaxconn")},
                  impact=3, confidence=3)
        if ctx.get("os_tuning") and (s.get("net_tcp_req_q_full_drop") or 0) > 0 and _int(v, "os:net.ipv4.tcp_max_syn_backlog", 4096) < 8192:
            b.add("net-syn-backlog", "Larger SYN queue", [("os:net.ipv4.tcp_max_syn_backlog", "8192")],
                  "Half-open connections are dropped because the SYN queue is full.", {"syn_queue_drops_per_s": s.get("net_tcp_req_q_full_drop")},
                  impact=2, confidence=3)
        conn_rate = s.get("net_conn_per_s") or 0
        nr = name_resolution(ctx.get("inputs"))
        if nr and nr[0] > 0 and str(v.get("skip_name_resolve", "")).upper() == "OFF":
            dur = (s.get("duration_s") or 0) or 1
            b.add("skip-name-resolve", "Skip reverse DNS on connect", [("skip_name_resolve", "ON")],
                  f"eBPF probes measured {nr[0]:.2f} s in host name resolution ({nr[1]} lookups, "
                  f"{1000 * nr[0] / max(nr[1], 1):.1f} ms each) during the {dur:.0f} s window: connecting threads wait on DNS.",
                  {"name_resolution_s": nr[0], "lookups": nr[1], "net_conn_per_s": conn_rate}, impact=2, confidence=2)
        if conn_rate > 50 * cpus:
            b.add("client-pooling", "Use connection pooling in the application", [],
                  f"{conn_rate:.0f} TCP connections/s: per-request connections cost a TCP handshake, authentication and a server thread "
                  "each. A pool (or ProxySQL) removes most of it; this is usually the largest single client-side win.",
                  {"net_conn_per_s": conn_rate}, impact=3, confidence=3, kind="advice")
        if ctx.get("os_tuning"):
            if (s.get("net_softnet_drops") or 0) > 0 and _int(v, "os:net.core.netdev_max_backlog", 1000) < 16384:
                b.add("net-netdev-backlog", "Larger per-CPU packet backlog", [("os:net.core.netdev_max_backlog", "16384")],
                      "Packets are dropped before the network stack processes them (softnet backlog full).",
                      {"softnet_drops_per_s": s.get("net_softnet_drops")}, impact=2, confidence=3)
            if (s.get("net_softnet_squeeze") or 0) > 1 and _int(v, "os:net.core.netdev_budget", 300) < 600:
                b.add("net-netdev-budget", "Larger softirq packet budget", [("os:net.core.netdev_budget", "600"),
                                                                              ("os:net.core.netdev_budget_usecs", "8000")],
                      "Packet processing regularly runs out of its per-round budget (time_squeeze).",
                      {"softnet_squeeze_per_s": s.get("net_softnet_squeeze")}, impact=1, confidence=2)
            if (s.get("softirq_max_cpu_pct") or 0) > 50 and (s.get("softirq_avg_cpu_pct") or 0) < 25 and v.get("os:rps") == "off":
                b.add("net-rps", "Spread packet processing over all CPUs (RPS/RFS)", [("os:rps", "on")],
                      f"One CPU spends {s['softirq_max_cpu_pct']:.0f}% in softirq while the average is {s['softirq_avg_cpu_pct']:.0f}%: "
                      "the NIC has fewer queues than CPUs. RPS/RFS steer packets to the CPU running the consuming thread.",
                      {"softirq_max_cpu_pct": s.get("softirq_max_cpu_pct")}, impact=2, confidence=2)
            if cpus >= 4 and (s.get("softirq_max_cpu_pct") or 0) > 30 and v.get("os:net_irq_cpus") == "auto" \
                    and v.get("os:cpu_affinity") == "all" and "os:rps" in v and v.get("os:rps") == "off":
                last = f"1-{cpus - 1}"
                b.add("os-irq-split", "Separate network interrupts from database threads", [("os:net_irq_cpus", "0"), ("os:cpu_affinity", last)],
                      f"One CPU spends {s['softirq_max_cpu_pct']:.0f}% in packet processing. Pinning the NIC interrupts to CPU 0 and mysqld "
                      f"to CPUs {last} stops interrupts from preempting query threads (lower tail latency) at the cost of one CPU "
                      "for the database. Alternative to RPS. Experiment.",
                      {"softirq_max_cpu_pct": s.get("softirq_max_cpu_pct"), "cpus": cpus}, impact=1, confidence=1)
            if (s.get("conntrack_fill_pct") or 0) > 80:
                cur = _int(v, "os:net.netfilter.nf_conntrack_max", 262144)
                b.add("net-conntrack", "Larger connection tracking table", [("os:net.netfilter.nf_conntrack_max", str(cur * 4))],
                      f"Conntrack table {s['conntrack_fill_pct']:.0f}% full: new connections are dropped when it fills. "
                      "Alternatively exempt the database port from tracking.", {"conntrack_fill_pct": s.get("conntrack_fill_pct")},
                      impact=3, confidence=3)
            rtt = s.get("rtt_avg_ms") or 0
            if rtt >= 10:
                if str(v.get("os:net.ipv4.tcp_slow_start_after_idle", "1")) == "1":
                    b.add("net-no-slow-start-idle", "Keep the congestion window on idle pooled connections", [("os:net.ipv4.tcp_slow_start_after_idle", "0")],
                          f"With {rtt:.0f} ms RTT, a pooled connection that was idle restarts from a small congestion window and needs "
                          "extra round trips to send a large result. Disabling slow start after idle keeps the window.",
                          {"rtt_avg_ms": rtt}, impact=2, confidence=2)
                if v.get("os:net.ipv4.tcp_congestion_control") == "cubic":
                    b.add("net-bbr", "BBR congestion control with fq", [("os:net.ipv4.tcp_congestion_control", "bbr"), ("os:net.core.default_qdisc", "fq")],
                          f"{rtt:.0f} ms RTT: BBR paces by measured bandwidth and RTT instead of loss, filling long paths faster "
                          "(applies to data this host sends: result sets on the server, large writes on clients). Experiment.",
                          {"rtt_avg_ms": rtt, "net_retrans_pct": s.get("net_retrans_pct")}, impact=2, confidence=1)
                b.add("client-compression", "Compress the protocol over the long link", [],
                      f"{rtt:.0f} ms RTT: for result-heavy traffic enable zstd protocol compression in the client "
                      "(--compression-algorithms=zstd); batch statements to save round trips.", {"rtt_avg_ms": rtt}, impact=1, confidence=2, kind="advice")
        kernel = str((ctx.get("hw") or {}).get("kernel", ""))
        km = re.match(r"(\d+)\.(\d+)", kernel)
        if km and (int(km.group(1)), int(km.group(2))) >= (6, 6) and (s.get("threads_running_avg") or 0) > 4 * cpus:
            b.add("eevdf-oversubscription", "Fewer runnable threads (EEVDF scheduler)", [],
                  f"Kernel {kernel} uses the EEVDF scheduler, which AWS measured 11-18% slower for MySQL when runnable threads exceed "
                  f"CPUs ({s.get('threads_running_avg', 0):.0f} running on {cpus} CPUs here). The effective fix is fewer runnable threads: "
                  "a thread pool or smaller client pools.", {"threads_running_avg": s.get("threads_running_avg"), "kernel": kernel},
                  impact=2, confidence=2, kind="advice")

    # ---------------- latency improvements the goal could not show ---------------
    # a change whose run left the goal unchanged but made statements clearly faster at no extra CPU or I/O per transaction
    # (session 294: os-autogroup, TPS +0.7%, avg statement latency -24%, p95 -37%): offered, not run again
    cur_m = {k: (m.get("value") if isinstance(m, dict) else m) for k, m in (ctx.get("managed") or {}).items()}
    for h in ctx.get("history") or []:
        sg = (h.get("evaluation") or {}).get("secondary_gain")
        own = [x for x in h.get("changes") or [] if str(x.get("source", "")).startswith("rec:")
               and not str(x["source"])[4:].startswith(("revert", "rollback", "drop"))]
        if not sg or not own or all(str(cur_m.get(x["variable"])) == str(x.get("new")) for x in own):
            continue
        txt = ", ".join(f"{g['label']} {-g['faster_pct']:+.0f}%" for g in sg.values())
        b.add(f"latency-run{h['seq']}", f"Lower statement latency: {h.get('label')}", [(x["variable"], str(x.get("new"))) for x in own],
              f"Run {h['seq']} ({h.get('label')}) left the goal unchanged ({((h.get('evaluation') or {}).get('goal_delta_pct') or 0):+.1f}%) "
              f"but statements got faster ({txt}) at no extra CPU or I/O per transaction. Worth keeping where latency matters; "
              "with a closed-loop benchmark on the same host the saved time can move to the client, so throughput does not show it.",
              {"run": h["seq"], "latency": sg}, impact=1, confidence=3, kind="advice")

    # ---------------- client hosts (applied through the client role) ---------------
    if have_run and ctx.get("os_tuning") and any(k.startswith("client:os:") for k in v):
        tw, cr = s.get("client_net_time_wait") or 0, s.get("client_net_conn_per_s") or 0
        if tw > 20000 or cr > 200:
            ch = []
            if v.get("client:os:net.ipv4.tcp_tw_reuse") != "1":
                ch.append(("client:os:net.ipv4.tcp_tw_reuse", "1"))
            if not str(v.get("client:os:net.ipv4.ip_local_port_range", "")).replace("\t", " ").startswith("1024 "):
                ch.append(("client:os:net.ipv4.ip_local_port_range", "1024 65535"))
            b.add("client-ports", "Client: more ephemeral ports and TIME_WAIT reuse", ch,
                  f"The client opens {cr:.0f} connections/s and holds {tw:.0f} sockets in TIME_WAIT: it can run out of ephemeral ports "
                  "(connect fails with EADDRNOTAVAIL). Pooling is the real fix; these settings raise the ceiling.",
                  {"client_net_conn_per_s": cr, "client_net_time_wait": tw}, impact=2, confidence=3)
        crtt = s.get("client_rtt_avg_ms") or 0
        if crtt >= 10 and str(v.get("client:os:net.ipv4.tcp_slow_start_after_idle", "1")) == "1":
            b.add("client-no-slow-start-idle", "Client: keep the congestion window on idle connections",
                  [("client:os:net.ipv4.tcp_slow_start_after_idle", "0")],
                  f"{crtt:.0f} ms RTT to the database: idle pooled connections restart slow start for every request burst.",
                  {"client_rtt_avg_ms": crtt}, impact=2, confidence=2)
        if crtt >= 10 and v.get("client:os:net.ipv4.tcp_congestion_control") == "cubic" and (s.get("client_net_tx_mbs") or 0) > 1:
            b.add("client-bbr", "Client: BBR congestion control with fq", [("client:os:net.ipv4.tcp_congestion_control", "bbr"),
                                                                            ("client:os:net.core.default_qdisc", "fq")],
                  f"The client sends {s.get('client_net_tx_mbs'):.1f} MiB/s over a {crtt:.0f} ms path (large writes/loads). Experiment.",
                  {"client_rtt_avg_ms": crtt, "client_net_tx_mbs": s.get("client_net_tx_mbs")}, impact=1, confidence=1)
        if (s.get("client_softirq_max_cpu_pct") or 0) > 50 and (s.get("client_softirq_avg_cpu_pct") or 0) < 25 and v.get("client:os:rps") == "off":
            b.add("client-rps", "Client: spread packet processing over all CPUs", [("client:os:rps", "on")],
                  f"One client CPU spends {s['client_softirq_max_cpu_pct']:.0f}% in softirq.", {"client_softirq_max_cpu_pct": s.get("client_softirq_max_cpu_pct")},
                  impact=2, confidence=2)

    # ---------------- sweeps: measure several values where a single step is a guess ----
    def sweep(id_, title, var, values, rationale, evidence, impact=2, lead=None, direction=None):
        if var in NUMA_PARTITIONED:
            values = [_numa_multiple(var, x, ctx) for x in values]
        values = [x for x in dict.fromkeys(values) if x is not None and normalize(var, x) != normalize(var, v.get(var))]
        values, shape = shaped_values(var, values, v.get(var), direction)
        if var in v and len(values) >= 2:
            b.add(id_, title, [(var, values[0])], rationale + f" Values: {', '.join(map(str, values))}.", evidence, impact=impact, confidence=2)
            if b.items and b.items[-1]["id"] == id_:
                b.items[-1]["sweep"] = {"variable": var, "values": [str(x) for x in values], "shape": shape}
                if lead:
                    b.items[-1]["lead"] = lead
    diag_types = {d["type"] for d in ctx.get("diagnosis") or [] if d.get("severity") in ("high", "medium")}
    # Memory overcommit with swap: when the working set misses memory and RAM is used up, a larger buffer pool can win
    # even if cold memory (idle processes, unused allocations) moves to swap: the cache saves more reads than swap costs.
    # The run's swap-in rate and the goal decide (harmful swap triggers bp-shrink on the next round).
    swap_free = ((s.get("swap_total_mb") or 0) - (s.get("swap_used_mb") or 0)) * MiB
    cold = (s.get("inactive_anon_mb") or 0) * MiB
    if have_run and diag_types & {"memory", "io"} and not swapping and bp and mem_avail <= max(768 * MiB, ram * 0.12) \
            and s.get("bp_hit_pct", 100) < 99.5 and (not data_bytes or bp < data_bytes):
        extra_swap = []
        room = min(int(ram * 0.15), cold + swap_free // 2)
        swap_after = swap_free
        swap_total = (s.get("swap_total_mb") or 0) * MiB
        if ctx.get("os_tuning") and swap_total < swap_target(ram) * 0.9 and ram >= 2 * GiB:
            # extra OS tuning, part of this experiment: swap up to 20% of RAM, 4-16 GiB (the agent's swap file
            # replaces an earlier one), so a buffer pool that outgrows memory shows as swapping instead of an OOM kill
            own = _swapfile_bytes(v.get("os:swapfile"))
            size = max(1 * GiB, (swap_target(ram) - (swap_total - own)) // GiB * GiB)
            if size > own:
                extra_swap = [("os:swapfile", fmt_size(size))]
                swap_after = swap_free + size - own
                room = min(int(ram * 0.15), cold + swap_after // 2)
        # the buffer pool must fit in RAM plus the swap it can use (half of it, 1 GiB for the rest of the server): beyond that
        # mysqld is OOM-killed instead of slow
        room = min(room, max(0, ram + swap_after // 2 - 1 * GiB - bp))
        new = bp_round(v, bp + room)
        if new >= bp + max(128 * MiB, bp * 0.05):
            b.add("bp-overcommit", "Grow the buffer pool into memory freed by swapping out cold pages",
                  [("innodb_buffer_pool_size", fmt_size(new))] + extra_swap,
                  f"Working set misses memory ({s.get('bp_hit_pct', 0):.2f}% hit ratio) and only {human_size(mem_avail)} RAM is available. "
                  f"{human_size(cold)} of anonymous memory is inactive and {human_size(swap_free)} swap is free"
                  f"{' (plus a new swap file)' if extra_swap else ''}: letting cold pages go to swap gives the buffer pool "
                  f"{human_size(new - bp)} more. Kept only if the goal improves and pages are not swapped back in heavily. Experiment.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "mem_available": human_size(mem_avail), "inactive_anon": human_size(cold),
                   "swap_free": human_size(swap_free), "swap_in": s.get("swap_in")}, impact=2, confidence=1)
    # Swap as OS protection: without it (Scaleway hosts come with 0) memory pressure ends in an OOM kill of mysqld instead
    # of slower pages, and every memory-using setting is sized against a hard wall. 20% of RAM, 4-16 GiB, on the
    # free disk; measured on its own and kept unless it costs throughput (a protective change, like the best practices).
    # Memory settings measured before the swap state changed are measured again after it (apply_history).
    swap_total = (s.get("swap_total_mb") or 0) * MiB
    own_swap = _swapfile_bytes(v.get("os:swapfile"))
    if have_run and ctx.get("os_tuning") and not ctx.get("no_swap") and ram >= 2 * GiB and swap_total < swap_target(ram) * 0.9 \
            and "os:swapfile" in v:
        free = int((ctx.get("hw") or {}).get("disk_free_bytes") or 0)
        size = (swap_target(ram) - (swap_total - own_swap)) // GiB * GiB
        if free:
            size = min(size, max(0, int((free - 2 * GiB) * 0.5)) // GiB * GiB)  # at most half of the free disk
        pressure = mem_avail <= max(768 * MiB, ram * 0.10) or bool(diag_types & {"memory", "swap"})
        if size >= GiB and size > own_swap and pressure:  # the baseline adds swap as a practice; an experiment only with pressure
            b.add("best-practices-swap", "Add swap (20% of RAM, 4-16 GiB): memory pressure slows instead of OOM-killing mysqld",
                  [("os:swapfile", fmt_size(size))],
                  f"{'No swap' if not swap_total else human_size(swap_total) + ' of swap'} on a host with {human_size(ram)} RAM: when memory "
                  "runs out the kernel kills mysqld instead of moving cold pages out, so memory settings are sized against a hard "
                  f"wall. A {human_size(size)} swap file is protection, measured on its own and kept unless it costs throughput; "
                  "memory settings measured before it are measured again with it.",
                  {"swap_total": human_size(swap_total), "ram": human_size(ram), "disk_free": human_size(free) if free else None},
                  impact=3 if not swap_total else 2, confidence=2)
    if have_run and diag_types & {"memory", "io"} and not swapping and mem_avail > 512 * MiB and bp:
        top = bp_round(v, min(ram * 0.80, bp + mem_avail - 512 * MiB))
        mid = bp_round(v, (bp + top) // 2)
        if top > bp * 1.15:
            sweep("sweep-bp", "Sweep the buffer pool size", "innodb_buffer_pool_size", [fmt_size(mid), fmt_size(top)],
                  "The working set misses memory and RAM is free: measure how throughput responds to more buffer pool.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "mem_available": human_size(mem_avail)}, impact=3)
    # only with a read-modify-write to remove: redo writes that read the block first (0 redo reads in every run of 10 neutral
    # write-ahead sweeps - audit of lab / user sessions)
    redo_reads_ = ((inp.get("file_io") or {}).get("redo") or {}).get("reads") or 0
    if have_run and "commit" in diag_types and "innodb_log_write_ahead_size" in v and redo_reads_ > 0:
        # redo writes padded to this size: smaller than the file system / device block costs a read-modify-write, larger
        # writes more than needed per flush. Cheap, safe, powers of two.
        wa = _int(v, "innodb_log_write_ahead_size", 8192)
        vals = [str(x) for x in (4096, 16384) if x != wa]
        sweep("sweep-log-write-ahead", "Sweep the redo write-ahead (padding) size", "innodb_log_write_ahead_size", vals,
              f"Commits wait for redo writes: the write-ahead size ({wa} bytes now) decides how redo writes line up with the "
              "file system and device blocks (read-modify-write when smaller).", {"innodb_log_write_ahead_size": wa})
    if have_run and "redo" in diag_types and cap:
        sweep("sweep-redo", "Sweep the redo log capacity", "innodb_redo_log_capacity", [fmt_size(cap * 2), fmt_size(cap * 4)],
              "Checkpoint pressure: measure 2x and 4x the redo capacity.", {"checkpoint_age_pct_max": ck})
    if have_run and diag_types & {"io", "redo"} and ssd:
        sweep("sweep-io-capacity", "Sweep the background flushing budget", "innodb_io_capacity", [str(ioc // 2), str(ioc * 2), str(ioc * 4)],
              "I/O or redo pressure: find the flushing budget the storage sustains.", {"innodb_io_capacity": ioc, "disk_util": s.get("disk_util")})
    # ---- advanced flushing / I/O ideas (docs/research/mysql-tuning-ideas.md B2, B9, C8, C13, B5, C2) ----
    io_bound = bool(diag_types & {"io", "memory"}) or (s.get("bp_disk_reads") or 0) > 500
    write_heavy = wtype in ("oltp-write", "mixed")
    if have_run and io_bound and write_heavy and "innodb_max_dirty_pages_pct_lwm" in v:
        sweep("sweep-dirty-lwm", "Sweep the dirty-page low-water mark", "innodb_max_dirty_pages_pct_lwm", ["0", "30", "80"],
              f"Dirty pages averaged {s.get('bp_dirty_pct_avg') or 0:.0f}% and checkpoint age peaked at {ck:.0f}%. The low-water mark sets when "
              "background flushing starts: lower writes pages earlier (smoother, less checkpoint pressure), higher lets hot pages absorb "
              "more updates per write (less write I/O). The better side depends on update locality, so it is measured, not assumed.",
              {"bp_dirty_pct_avg": s.get("bp_dirty_pct_avg"), "checkpoint_age_pct_max": ck}, impact=2,
              lead="Practitioners run opposite settings (0 vs 80) and each reports a win; unverified here, it only motivates the test.")
    wait_free = rates.get("Innodb_buffer_pool_wait_free", 0)
    if have_run and io_bound and "innodb_lru_scan_depth" in v and wait_free > 0:  # free-page waits: what the depth acts on
        lru = _int(v, "innodb_lru_scan_depth", 1024)
        sweep("sweep-lru-depth", "Sweep the LRU scan depth", "innodb_lru_scan_depth", [str(max(256, lru // 4)), str(lru * 2), str(lru * 4)],
              "Page cleaners keep this many pages free per buffer pool instance. Deeper scans avoid single-page flushes by user "
              "threads (free-page waits); shallower scans spend less CPU and I/O when free pages are plentiful.",
              {"Innodb_buffer_pool_wait_free_per_s": wait_free}, impact=2)
    # read I/O threads serve asynchronous reads (read-ahead): with none, synchronous page reads are done by the user threads
    # themselves (r1: 7 neutral runs in RO-2, KV-1, RO-4 at read-ahead 0)
    if have_run and io_bound and cpus >= 8 and (s.get("bp_disk_reads") or 0) > 200 and "innodb_read_io_threads" in v \
            and ((rates.get("Innodb_buffer_pool_read_ahead") or 0) + (rates.get("Innodb_buffer_pool_read_ahead_rnd") or 0)) > 0:
        rio = _int(v, "innodb_read_io_threads", 4)
        vals = [x for x in (8, 16, 32) if x > rio][:2]
        if vals:
            sweep("sweep-read-io-threads", "Sweep InnoDB read I/O threads", "innodb_read_io_threads", [str(x) for x in vals],
                  f"{s.get('bp_disk_reads'):.0f} page reads/s from disk on {cpus} CPUs: more read threads keep more asynchronous reads "
                  "(read-ahead, prefetch) in flight on NVMe. Needs a restart.", {"bp_disk_reads": s.get("bp_disk_reads")}, impact=1)
    # read path: InnoDB read-ahead (linear: prefetch the next extent after N sequential page reads; random: prefetch the
    # rest of an extent when many of its pages are already cached). Counters show whether prefetching was used or wasted.
    ra, ra_ev = s.get("read_ahead_per_s") or 0, s.get("read_ahead_evicted_per_s") or 0
    reads_now = s.get("bp_disk_reads") or 0
    # only where read-ahead is at work: point lookups read single pages and give it nothing to act on (r1: 16 sweep runs
    # in 5 cells, improvements only in the analytic scans of AN-2)
    if have_run and reads_now >= 100 and ra > 0 and "innodb_read_ahead_threshold" in v:
        cur = _int(v, "innodb_read_ahead_threshold", 56)
        waste = ra_ev / ra if ra else 0
        vals = ["64", "32"] if waste > 0.3 else ["8", "32", "64"]  # wasted prefetch: read ahead less, not more
        sweep("sweep-read-ahead", "Sweep linear read-ahead threshold", "innodb_read_ahead_threshold", [x for x in vals if x != str(cur)],
              f"{reads_now:.0f} page reads/s from disk; linear read-ahead prefetched {ra:.0f} pages/s"
              + (f", {100 * waste:.0f}% of them evicted unused" if ra else "")
              + ". A lower threshold prefetches sooner (larger sequential reads for range / index scans), 64 almost disables it.",
              {"bp_disk_reads": reads_now, "read_ahead_per_s": ra, "read_ahead_evicted_per_s": ra_ev, "innodb_read_ahead_threshold": cur}, impact=2,
              direction="up" if waste > 0.3 else None)
        if not _on(v, "innodb_random_read_ahead") and waste < 0.3:
            b.add("random-read-ahead", "Random read-ahead", [("innodb_random_read_ahead", "ON")],
                  f"{reads_now:.0f} page reads/s from disk: random read-ahead fetches the rest of an extent once enough of its pages "
                  "are cached, helping access patterns that are clustered but not sequential. Off by default since 5.5; watch the "
                  "evicted-unused counter. Experiment.", {"bp_disk_reads": reads_now, "read_ahead_per_s": ra}, impact=1, confidence=1)
    if have_run and ra >= 50 and ra_ev / ra > 0.5 and _int(v, "innodb_read_ahead_threshold", 56) < 64:
        b.add("read-ahead-less", "Read ahead less: prefetched pages are wasted", [("innodb_read_ahead_threshold", "64")] +
              ([("innodb_random_read_ahead", "OFF")] if _on(v, "innodb_random_read_ahead") else []),
              f"{100 * ra_ev / ra:.0f}% of read-ahead pages ({ra_ev:.0f}/s) were evicted before use: wasted I/O and buffer pool churn.",
              {"read_ahead_per_s": ra, "read_ahead_evicted_per_s": ra_ev}, impact=2, confidence=2)
    hll_max = s.get("history_list_length_max") or 0
    if have_run and hll_max > 100000 and "innodb_purge_threads" in v and not purge_blocked(s):
        pt = _int(v, "innodb_purge_threads", 4)
        # a large backlog is a strong signal (O4: 1.68 M with 1 purge thread never got to run at impact 1): more threads first
        vals = [str(pt * 2), "8", "1"] if hll_max > 300000 else [x for x in ("1", "8") if x != str(pt)] + [str(pt * 2)]
        unb_h = any(u["metric"] == "history_list_length" for u in s.get("unbounded_growth") or [])
        if unb_h:  # purge cannot keep up: more threads only (fewer cannot help), up to the CPUs available
            vals = [str(x) for x in sorted({min(pt * 2, 32), min(max(4, pt * 4), 32), min(int(hw.get("cpus") or 8), 32), 32}) if x > pt]
        sweep("sweep-purge-threads", "Sweep purge threads", "innodb_purge_threads", vals,
              f"Undo history reached {hll_max:,.0f} entries with {pt} purge thread(s). More purge threads help large I/O-bound boxes "
              "and hurt small CPU-bound ones: the direction depends on the machine, so it is measured.",
              {"history_list_length_max": hll_max}, impact=3 if hll_max > 1000000 or unb_h else 2 if hll_max > 300000 else 1,
              lead="Published results disagree between large and small machines; unverified here.")
        if b.items and b.items[-1]["id"] == "sweep-purge-threads":
            it_ = b.items[-1]
            if busy >= 85 and unb_h and STABILITY_FIRST:
                # unsustainable: stability before throughput - one step up is measured even on saturated CPUs, after the
                # purge lag bound (run_engine orders the stability remedies)
                it_.pop("sweep")  # one value: a single change (its first value), not a sweep (h2h W3 rules: HTTP 400)
                it_["rationale"] = ("Purge cannot keep up: the history list grows without slowing down. Stability first: more purge "
                                    "threads are measured even though the CPUs are busy. " + it_["rationale"])
            elif busy >= 85:
                # no CPU for more purge threads: 23 sweep runs, 0 wins, 17 losses (mean -10%, worst -51%), and its +6 priority
                # put it ahead of trx-commit-2 (h2h 283/285/288). On saturated CPUs a growing backlog is advice, not a run
                it_["kind"] = "advice"
                it_["note"] = (f"Advice: the CPUs are {busy:.0f}% busy, so more purge threads compete with the transactions "
                               "(0 wins in 23 measured sweeps). A history list that keeps growing means this load is faster than "
                               "this machine can clean up after it - often the effect of a trade-off that sped up commits.")
            elif unb_h:
                # an unsustainable configuration with CPU to spare: more purge threads come before tuning it
                it_["score_prior"] = it_.get("score_prior", 0) + 2
                it_.setdefault("score_parts", []).append(["unbounded history with CPU to spare", 2])
                it_["rationale"] = ("Purge cannot keep up: the history list grows without slowing down, so this configuration is "
                                    "unsustainable. " + it_["rationale"])
    if have_run and ck >= 75 and _on(v, "innodb_flush_sync"):
        b.add("flush-sync-off", "Respect io_capacity_max during sync flushing", [("innodb_flush_sync", "OFF")],
              f"Checkpoint age reached {ck:.0f}%: InnoDB then flushes as fast as it can, ignoring io_capacity_max and starving reads. "
              "OFF keeps the cap (Percona; Mark's a3 config: minor gains and losses). Experiment.", {"checkpoint_age_pct_max": ck},
              impact=1, confidence=1)
    if have_run and cpus >= 16 and diag_types & {"concurrency", "cpu", "io"} and flavor != "mariadb" and "innodb_buffer_pool_instances" in v:
        bpi = _int(v, "innodb_buffer_pool_instances", 8)
        if bpi < 32:
            sweep("sweep-bp-instances", "Sweep buffer pool instances", "innodb_buffer_pool_instances", [str(x) for x in (16, 32) if x > bpi],
                  f"{cpus} CPUs with {bpi} buffer pool instance(s) and contention/CPU/I-O pressure diagnosed: more instances split the LRU and "
                  "free-list mutexes. Needs a restart.", {"innodb_buffer_pool_instances": bpi}, impact=1,
                  lead="Reported to help on many-core servers; unverified here.")
    if have_run and "cpu-spin" in diag_types and "innodb_spin_wait_delay" in v:
        sweep("sweep-spin-wait-delay", "Sweep the spin-wait delay", "innodb_spin_wait_delay", ["2", "12", "24"],
              "The CPU profile shows threads spinning on InnoDB latches. The pause between spin rounds trades CPU for wake-up latency; "
              "its best value depends on the CPU (PAUSE instruction cost differs a lot between generations - MySQL bug 116463).",
              {"cpu_profile_categories": ((inp.get("cpu_profile") or {}).get("categories") or {})}, impact=1)
    # a limit at or above the connection count admits everyone - a no-op (64 for 64 virtual users in three user sessions);
    # and after the thread pool (stronger admission control) lost in this session, a weaker one will not win
    conns = s.get("threads_connected_avg") or 0
    limits = [x for x in (cpus * 2, cpus * 4) if not conns or x < 0.75 * conns]
    pool_lost = any(str(h.get("label") or "").startswith("thread-pool") and h.get("verdict") == "regressed" for h in ctx.get("history") or [])
    if have_run and limits and not pool_lost and diag_types & {"concurrency", "cpu-pressure", "sched-delay"} and "innodb_thread_concurrency" in v:
        sweep("sweep-thread-concurrency", "Sweep InnoDB thread concurrency", "innodb_thread_concurrency", [str(x) for x in limits] + ["0"],
              f"{s.get('threads_running_avg', 0):.0f} running threads on {cpus} CPUs: measure admission limits.",
              {"threads_running_avg": s.get("threads_running_avg"), "cpus": cpus})

    tc_now = _int(v, "innodb_thread_concurrency", 0)
    if have_run and tc_now > 0 and not unb_hll and "innodb_thread_concurrency" in v:
        # a cap that keeps the undo history bounded (the purge triage's remedy) is a start, not the answer: its value is
        # tuned here - every value measured, and only stable configurations can win (user, 2026-10-25)
        sweep("tune-concurrency-cap", "Tune the InnoDB thread concurrency cap", "innodb_thread_concurrency",
              [str(x) for x in sorted({max(1, tc_now // 2), tc_now * 3 // 2, tc_now * 2}) if x != tc_now] + ["0"],
              f"innodb_thread_concurrency={tc_now} is set (by the purge triage or earlier tuning) and the history is bounded: values "
              "around it - and no cap (0): other changes since, e.g. a larger buffer pool, may have made it unnecessary - measured "
              "for throughput; a value that lets the history grow again cannot win.",
              {"innodb_thread_concurrency": tc_now, "threads_running_avg": s.get("threads_running_avg"), "cpus": cpus}, impact=2)
        if b.items and b.items[-1]["id"] == "tune-concurrency-cap" and "0" in b.items[-1]["sweep"]["values"]:
            vs_ = b.items[-1]["sweep"]["values"]  # no cap first: the most telling point once other settings changed
            b.items[-1]["sweep"]["values"] = ["0"] + [x for x in vs_ if x != "0"]
        # its companions, like the thread pool's settings (user): how long a thread keeps its slot (tickets: row operations
        # before it queues again) and how waiting threads back off (sleep delay; adaptive up to its max)
        if "innodb_concurrency_tickets" in v:
            tk = _int(v, "innodb_concurrency_tickets", 5000)
            sweep("tune-concurrency-tickets", "Tune InnoDB concurrency tickets", "innodb_concurrency_tickets",
                  [str(x) for x in (500, 2000, 20000) if x != tk],
                  f"innodb_thread_concurrency={tc_now} with {tk} tickets: fewer tickets rotate threads through the slots more often "
                  "(fairer, more switching), more let a thread finish its transaction in one go (shorter open transactions).",
                  {"innodb_concurrency_tickets": tk}, impact=1)
        if "innodb_thread_sleep_delay" in v:
            sd = _int(v, "innodb_thread_sleep_delay", 10000)
            sweep("tune-concurrency-sleep", "Tune the concurrency queue sleep", "innodb_thread_sleep_delay",
                  [str(x) for x in (0, 1000, 50000) if x != sd],
                  f"Threads waiting for one of the {tc_now} slots sleep {sd} us before queueing (adaptive up to "
                  f"innodb_adaptive_max_sleep_delay): shorter wakes them sooner, longer spins less.", {"innodb_thread_sleep_delay": sd}, impact=1)

    if have_run and str(v.get("thread_handling") or "").lower() == "pool-of-threads" and not unb_hll and "thread_pool_size" in v:
        # the thread pool's settings are tuned, not only the pool switched on (user): how many threads run at once
        tps_ = _int(v, "thread_pool_size", cpus)
        sweep("tune-thread-pool-size", "Tune the thread pool size", "thread_pool_size",
              [str(x) for x in sorted({max(2, tps_ // 2), tps_ * 3 // 2, tps_ * 2}) if x != tps_],
              f"The thread pool runs with {tps_} groups; how many threads it runs at once (groups x (1 + oversubscribe)) trades "
              "contention against idle CPUs. Values around the current one measured; with a stability remedy in place only "
              "stable configurations can win.", {"thread_pool_size": tps_, "cpus": cpus}, impact=2)
        if "thread_pool_oversubscribe" in v:
            ov_ = _int(v, "thread_pool_oversubscribe", 3)
            sweep("tune-thread-pool-oversubscribe", "Tune thread pool oversubscription", "thread_pool_oversubscribe",
                  [str(x) for x in (1, 2, 3, 5) if x != ov_],
                  f"thread_pool_oversubscribe={ov_}: how many more threads a group may run while one waits (locks, I/O).",
                  {"thread_pool_oversubscribe": ov_}, impact=2)

    # ---------------- clustering and other storage engines ---------------------------
    rates_ = inp.get("status_rates") or {}
    cl = ctx.get("cluster") or {}
    if have_run and cl.get("type") == "galera":
        # flow control: the cluster paused writes because an applier queue somewhere filled up - measured time, not a guess
        paused = (rates_.get("wsrep_flow_control_paused_ns") or 0) / 1e9 * 100
        tvar = "wsrep_applier_threads" if "wsrep_applier_threads" in v else "wsrep_slave_threads"
        if paused > float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0) and tvar in v:
            cur = max(1, _int(v, tvar, 1))
            sweep("sweep-applier-threads", "Sweep Galera applier threads", tvar, [str(cur * 2), str(cur * 4), str(max(cur * 4, cpus))],
                  f"Writes were paused by flow control {paused:.1f}% of the time: an applier queue fills up and the cluster waits for it. "
                  f"More applier threads ({tvar} = {cur}) apply write sets in parallel where they do not conflict.",
                  {"flow_control_paused_pct": round(paused, 2), tvar: cur}, impact=3)
    eng = ctx.get("engines") or {}
    rocks = ((eng.get("data") or {}).get("rocksdb")) or 0
    if have_run and rocks and "rocksdb_block_cache_size" in v:
        hit = (rates_.get("rocksdb_block_cache_data_hit") or rates_.get("rocksdb_block_cache_hit") or 0)
        miss = (rates_.get("rocksdb_block_cache_data_miss") or rates_.get("rocksdb_block_cache_miss") or 0)
        bc = _int(v, "rocksdb_block_cache_size")
        if miss > 0 and hit + miss > 0 and bc < rocks:
            new = bp_round(v, min(rocks, bc * 2)) if bc else GiB
            b.add("rocksdb-cache-grow", "Grow the MyRocks block cache", [("rocksdb_block_cache_size", fmt_size(new))],
                  f"MyRocks holds {human_size(rocks)} and its {human_size(bc)} block cache misses {miss:.0f} times/s "
                  f"({100 * miss / (hit + miss):.1f}% of lookups): more cached blocks, fewer reads. The memory budget limits the size.",
                  {"rocksdb_data": human_size(rocks), "rocksdb_block_cache_size": human_size(bc), "block_cache_miss_per_s": round(miss, 1)},
                  impact=2, confidence=2)

    # ---------------- filesystem experiments (opt-in scope) --------------------------
    if have_run and "filesystem" in (ctx.get("scope") or []) and ctx.get("fs_device") and v.get("os:datadir_fs") not in (None, "rootfs"):
        from .fslab import CANDIDATES
        cur_fs = str(v.get("os:datadir_fs"))
        for val, why in CANDIDATES:
            if cur_fs.split(":")[0] == val.split(":")[0] and cur_fs == val:
                continue
            b.add(f"fs-{val.split(':')[0]}", f"Filesystem: {val.split(':')[0]}", [("os:datadir_fs", val)],
                  why + " The data set is moved onto a freshly formatted experiment device and restored from the pristine copy.",
                  {"current": cur_fs, "candidate": val}, impact=2, confidence=1)
        if cur_fs.startswith("zfs") and str(v.get("innodb_doublewrite", "")).upper() == "ON":
            b.add("zfs-doublewrite-off", "ZFS: doublewrite buffer is redundant", [("innodb_doublewrite", "OFF")],
                  "ZFS writes records atomically (copy-on-write), so torn pages cannot happen; Percona/OpenZFS guidance disables "
                  "InnoDB doublewrite on ZFS. Not a durability trade-off on ZFS.", {"filesystem": cur_fs}, impact=2, confidence=2)
            b.items[-1]["safety"], b.items[-1]["tradeoffs"], b.items[-1]["risk"] = "safe", [], "none"

    # ---------------- allocator sensitivity: try variants once an allocator change mattered ----
    if ctx.get("os_tuning") and have_run:
        sensitive = None
        for r in ctx.get("history") or []:
            if any(ch["variable"] == "os:malloc" for ch in r.get("changes") or []) and r.get("verdict") in (
                    "improved", "regressed", "efficiency-gain", "efficiency-loss"):
                sensitive = r
        if sensitive:
            cur = str(v.get("os:malloc", "glibc"))
            variants = [("jemalloc:background_thread:true,dirty_decay_ms:5000", "jemalloc with a background purge thread and slower page return"),
                        ("tcmalloc", "gperftools tcmalloc: per-thread caches, low lock contention"),
                        ("mimalloc", "Microsoft mimalloc: free-list sharding, good small-object throughput"),
                        ("glibc:arena_max=4", "glibc malloc with fewer arenas: less fragmentation / RSS growth")]
            for val, why in variants:
                if val != cur:
                    b.add(f"malloc-{val.split(':')[0]}{'-tuned' if ':' in val else ''}", f"Allocator variant: {val}", [("os:malloc", val)],
                          f"Run #{sensitive['seq']} showed this workload is allocator-sensitive ({sensitive['verdict']}); {why}. Needs a mysqld restart.",
                          {"allocator_run": sensitive.get("seq"), "verdict": sensitive.get("verdict"), "current": cur}, impact=2, confidence=1)

    # ---------------- query advice -------------------------------------------------
    for q in (inp.get("top_statements") or [])[:10]:
        if q["pct_time"] < 5:
            continue
        if q["no_index_used"] > 0 or (q["rows_examined_avg"] > 1000 and q["rows_examined_avg"] > 50 * max(q["rows_sent_avg"], 1)):
            b.add(f"query-{q['digest'].split(':')[-1][:8]}", f"Query consumes {q['pct_time']:.0f}% of time with poor index use", [],
                  f"Examines {q['rows_examined_avg']:.0f} rows to return {q['rows_sent_avg']:.0f}"
                  f"{' and runs without an index' if q['no_index_used'] else ''}: {q['query'][:200]}",
                  {"avg_ms": q["avg_ms"], "per_s": q["per_s"], "pct_time": q["pct_time"]}, impact=2, confidence=2, kind="advice")

    return b.items


def score_parts(item, goal):
    """score() term by term: [[what, points]]."""
    out = [[f"expected impact {item['impact']} of 3 (x2)", item["impact"] * 2], [f"confidence {item['confidence']} of 3", item["confidence"]]]
    if goal not in item.get("goals", [goal]):
        out.append([f"not aimed at the goal ({goal})", -3])
    if item["kind"] == "advice":
        out.append(["advice only, not run", -10])
    return out


def score(item, goal):
    s = item["impact"] * 2 + item["confidence"]
    if goal not in item.get("goals", [goal]):
        s -= 3
    if item["kind"] == "advice":
        s -= 10
    return s


# settings whose effect flips with the commit path: tried with an fsync per commit, they say nothing about the same change
# without one (log writer threads: -38% at trx_commit=1, +25% at trx_commit=2 on the same box)
COMMIT_PATH = ("innodb_flush_log_at_trx_commit", "sync_binlog", "disable_log_bin", "log_bin")
COMMIT_PATH_DEPENDENT = {"log-writer-threads-off"}


def apply_history(items, history, swap_now_mb=None, managed_now=None):
    """Mark/skip items already tried. A memory-using setting tried while the swap state was different (no swap -> a swap
    file of 20% of RAM, or back) is tried again: its best value depends on whether pressure means slower or killed."""
    tried = {}
    tried_ids = {}
    swept = {}  # variable -> last run of an agent sweep over it (screening skips points: the first value may never run)
    for run in history:
        for c in run.get("changes") or []:
            tried[(c["variable"], normalize(c["variable"], c.get("new")))] = run
            src = c.get("source") or ""
            if src.startswith("rec:"):
                tried_ids[src[4:]] = run
            if src.startswith("exp:") and str(run.get("label") or "").startswith(f"exp#{src[4:]} "):
                swept[c["variable"]] = run  # the sweep's own runs, not a later run its restore was recorded with
    out = []
    for it in items:
        prior = [tried.get((c["variable"], normalize(c["variable"], c["proposed"]))) for c in it["changes"]]
        prior = [p for p in prior if p] or ([tried_ids[it["id"]]] if it["id"] in tried_ids and it["kind"] == "config" else [])
        if not prior and it.get("sweep") and it["sweep"]["variable"] in swept:
            prior = [swept[it["sweep"]["variable"]]]
        if prior:
            p = prior[-1]
            it["tried_in_run"] = p.get("seq")
            it["tried_verdict"] = p.get("verdict")
            swap_then = (p.get("summary") or {}).get("swap_total_mb")
            from .memory import MEMORY_VARS
            if swap_now_mb is not None and swap_then is not None and abs(swap_now_mb - swap_then) >= 1024 \
                    and any(c["variable"] in MEMORY_VARS for c in it["changes"]):
                it["retest"] = {"run": p.get("seq"), "swap_then_mb": swap_then, "swap_now_mb": swap_now_mb}
                it["note"] = (f"Tried in run #{p.get('seq')} ({p.get('verdict')}) with {swap_then / 1024:.0f} GiB swap; now "
                              f"{swap_now_mb / 1024:.0f} GiB: memory settings are measured again with the new swap state.")
                out.append(it)
                continue
            if it["id"] in COMMIT_PATH_DEPENDENT and managed_now is not None:
                then = {k: (p.get("managed") or {}).get(k) for k in COMMIT_PATH}
                now_ = {k: managed_now.get(k) for k in COMMIT_PATH}
                if then != now_:
                    it["retest"] = {"run": p.get("seq"), "commit_path_then": then, "commit_path_now": now_}
                    it["note"] = (f"Tried in run #{p.get('seq')} ({p.get('verdict')}) with another commit path "
                                  f"({', '.join(f'{k}={v}' for k, v in then.items() if v is not None) or 'defaults'}): measured again.")
                    out.append(it)
                    continue
            if p.get("verdict") in ("regressed", "neutral", "efficiency-loss"):
                it["confidence"] = 0
                it["note"] = f"Already tried in run #{p.get('seq')} ({p.get('verdict')})."
        out.append(it)
    return out


def _retest_after_shift(it, ctx):
    """Coordinate-wise tuning can stop at a local optimum: a setting found flat early can matter once another change moved
    the bottleneck (a larger buffer pool turns an I/O-bound run CPU-bound). A change tried without effect is tried again
    when the measured share of time it acts on grew by more than the noise threshold since that run."""
    if it.get("tried_verdict") not in ("neutral", "efficiency-loss") or it.get("confidence") != 0:
        return
    h = it.get("hypothesis") or {}
    run = next((r for r in ctx.get("history") or [] if r.get("seq") == it.get("tried_in_run")), None)
    then = (((run or {}).get("evaluation") or {}).get("hypothesis") or {}).get("share_pct")
    now = h.get("share_pct")
    noise = float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0)
    if then is None or now is None or now - then <= noise:
        return
    it["confidence"] = 1
    it["retest"] = {"run": it.get("tried_in_run"), "share_then_pct": then, "share_now_pct": now}
    it["note"] = (f"Tried in run #{it.get('tried_in_run')} without effect, when {', '.join(h.get('target') or [])} took {then:.0f}% of "
                  f"the time; now {now:.0f}%: the bottleneck shifted, tested again.")


def config_key(managed):
    return tuple(sorted((k, normalize(k, v)) for k, v in (managed or {}).items()))


def config_scores(history, goal="tps"):
    """Mean goal value per distinct configuration (repeated runs of one config are averaged)."""
    groups = {}
    for r in history:
        if r.get("goal_value") is None or r.get("verdict") in ("unreliable", "superseded"):
            continue
        g = groups.setdefault(config_key(r.get("managed")), {"runs": [], "managed": r.get("managed") or {}})
        g["runs"].append(r)
    out = []
    for key, g in groups.items():
        vals = [r["goal_value"] for r in g["runs"]]
        out.append({"key": key, "managed": g["managed"], "mean": sum(vals) / len(vals), "n": len(vals),
                    "seqs": [r["seq"] for r in g["runs"]], "label": g["runs"][0].get("label")})
    out.sort(key=lambda c: c["mean"], reverse=(goal != "latency"))
    return out


KEEP = ("improved", "efficiency-gain", "reference")

# rules whose changes are best practices: applied as a bundle and kept unless they regress
# hygiene only: settings with no plausible downside for a database server. Workload-dependent ones (NUMA balancing off
# can help or hurt: em-b212x 342 bundled it with swappiness / noatime / governor, the bundle regressed 3.7% and all four
# were reverted unexplained - user, 2026-10-25) are experiments of their own
PRACTICE_IDS = {"flush-neighbors", "o-direct", "os-swappiness", "os-thp-off", "os-scheduler", "os-noatime", "os-zone-reclaim-off", "os-aio", "os-governor"}
BUNDLE = "best-practices"
# settings with a known mechanism to help and no known way to hurt when applied correctly (user, 2026-10-25: huge pages in
# 341 gave +2.2% / +1.2% and -3.5% CPU per transaction, within noise, and were rolled back for lack of proof): kept unless
# measured harm - unlike settings that can help or hurt (NUMA balancing, concurrency limits, AHI, purge, spin, ...), which
# need a gain beyond noise. Evidence of harm in comparable sessions (experience: 2+ losses, no win) revokes it.
LOW_DOWNSIDE_VARS = {"os:hugepages", "large_pages", "os:thp", "os:vm.swappiness", "os:mount_noatime", "os:cpu_governor",
                     "os:scheduler", "innodb_flush_neighbors", "os:vm.zone_reclaim_mode", "os:fs.aio-max-nr", "os:swapfile"}


def is_practice_run(r):
    return str(r.get("label") or "").startswith(BUNDLE)


def _practice_vars(r):
    return sorted({c["variable"] for c in r.get("changes") or [] if str(c.get("source") or "").startswith("rec:" + BUNDLE)})


def accepted_practices(history):
    """Variables of practice runs that did not regress: they stay."""
    return {v for r in history if is_practice_run(r) and r.get("verdict") not in ("regressed", "efficiency-loss") for v in _practice_vars(r)}


def stack_small_wins(items, ctx):
    """Each experiment alone may stay within the noise threshold although it helps a little. Collect experiments that
    were reverted as 'neutral' but measured above the parent by at least a third of the threshold (and did not cost
    efficiency), and propose them together once: small gains add up, and the combination is measured like any other
    experiment."""
    hist = ctx.get("history") or []
    v = ctx["variables"]
    noise = float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0)
    if len(hist) < 8 or any(str(r.get("label") or "").startswith("stack-small-wins") for r in hist):
        return items
    lower = ctx.get("goal") == "latency"
    cand = {}
    for r in hist:
        ev = r.get("evaluation") or {}
        d = ev.get("goal_delta_pct")
        if r.get("verdict") != "neutral" or d is None or d < max(0.3, noise / 3) or ev.get("efficiency_worse"):
            continue
        own = [c for c in r.get("changes") or [] if str(c.get("source") or "").startswith(("rec:", "exp:")) and not c["variable"].startswith("client:")]
        for c in own:
            val = (r.get("managed") or {}).get(c["variable"])
            if val is None or normalize(c["variable"], v.get(c["variable"])) == normalize(c["variable"], val):
                continue
            if c["variable"] not in cand or d > cand[c["variable"]][1]:
                cand[c["variable"]] = (val, d, r["seq"])
    if len(cand) < 2:
        return items
    top = sorted(cand.items(), key=lambda x: -x[1][1])[:5]
    ch = [{"variable": k, "current": v.get(k), "proposed": val} for k, (val, d, seq) in top if k in v]
    trade = [t for t in (tradeoff_of(c["variable"], c["proposed"]) for c in ch) if t]
    if len(ch) < 2 or trade:
        return items
    return items + [{"id": "stack-small-wins", "title": "Combine experiments that helped a little", "kind": "config", "changes": ch,
                     "rationale": "These changes each measured above their parent but within the ±%.1f%% noise threshold, so each was "
                                  "reverted alone: %s. Small gains add up; the combination is measured once like any other experiment."
                                  % (noise, ", ".join(f"{k}={val} (+{d:.1f}% in run #{seq})" for k, (val, d, seq) in top)),
                     "safety": "safe", "tradeoffs": [], "evidence": {k: f"+{d:.2f}% run #{seq}" for k, (val, d, seq) in top},
                     "impact": 2, "confidence": 2, "risk": "low", "restart": True, "goals": ["tps", "latency"]}]


def practice_bundle(items, history):
    """Replace individual best-practice items with one bundle. A bundle that regresses is split in halves (bisection)
    until the offending setting is found; that setting is then excluded, the others are kept."""
    prac = [i for i in items if (i["id"] in PRACTICE_IDS or i["id"].startswith("practice-")) and i["kind"] == "config"
            and i["safety"] == "safe" and i.get("confidence", 0) > 0]
    if not prac:
        return items
    rest = [i for i in items if i not in prac]
    changes = {c["variable"]: (c, i) for i in prac for c in i["changes"]}
    runs = [r for r in history if is_practice_run(r)]
    bad = [r for r in runs if r.get("verdict") in ("regressed", "efficiency-loss")]
    tested = {tuple(_practice_vars(r)) for r in runs}
    excluded, pending = set(), None
    for r in bad:
        vs = _practice_vars(r)
        if len(vs) == 1:
            excluded.update(vs)
            continue
        halves = (vs[:len(vs) // 2], vs[len(vs) // 2:])
        excluded.update(vs)  # suspects until their half passes
        for h in halves:
            if tuple(h) not in tested:
                pending = pending or [x for x in h if x in changes]
    excluded -= accepted_practices(history)
    if pending:
        chosen, bid, title = pending, BUNDLE + "-bisect", "Re-test half of the regressed best-practice bundle"
        why = "The best-practice bundle regressed; measuring half of it finds the setting responsible and keeps the others."
    else:
        chosen, bid, title = [k for k in changes if k not in excluded], BUNDLE, "Apply best-practice settings"
        why = ("Settings that are not the default (mostly for compatibility) but are generally better for database servers. "
               "Each alone is usually within the noise, so they are applied together and kept unless the bundle measurably "
               "regresses the goal or efficiency; a regressing bundle is bisected.")
    if not chosen:
        return rest
    src = [changes[k][1] for k in chosen]
    bundle = {"id": bid, "title": title, "kind": "config", "changes": [changes[k][0] for k in chosen],
              "rationale": why + " " + " ".join(f"{changes[k][0]['variable']}: {changes[k][1]['rationale']}" for k in chosen)[:3000],
              "safety": "safe", "tradeoffs": [], "evidence": {"settings": len(chosen), "rules": sorted({i["id"] for i in src}),
                                                             "excluded_after_regression": sorted(excluded) or None},
              "impact": 2, "confidence": 3, "risk": max((i.get("risk", "none") for i in src), key=["none", "low", "compat", "observability", "durability"].index),
              "restart": any(i.get("restart") for i in src), "goals": ["tps", "latency"], "priority": True, "practice": True,
              # the bundle acts on what its members act on: evidence for any member is evidence for the bundle
              "acts_on_tags": sorted({t for i in src for t in (MECHANISMS.get(i["id"]) or ((),))[0]})}
    return [bundle] + rest


EFFICIENCY_TIEBREAK = ("mysqld_cpu_ms_per_trx", "disk_ios_per_trx", "disk_kb_per_trx", "db_mem_mb")
EFFICIENCY_TIE_MIN = 0.03  # resource differences below 3% are not a reason to prefer a configuration


def _unearned_unsteady(r):
    """An improvement measured while deferred work still piled up, small enough to come from that unpaid work."""
    ev = r.get("evaluation") or {}
    return bool(ev.get("not_steady")) and not ev.get("not_steady_beyond_bias")


# stability first (unless the session turns it off): a configuration whose undo history grows without bound cannot run
# in production however fast it measures - while any stable configuration exists, the best one is chosen among them, and
# the remedies (purge threads, a purge lag bound, larger purge batches) run before tuning experiments
STABILITY_FIRST = True
STABILITY_IDS = ("sweep-purge-threads", "purge-lag", "purge-batch", "purge-priority", "purge-concurrency", "purge-threadpool")
STABILITY_LAST_RESORT = ("purge-lag",)  # throttles every writer: only when the other remedies were tried or cannot run
STABILITY_TRIES = 3  # remedies that run first; after them the stability candidates rank like any other


PURGE_UNBOUNDED_MIN = 1000000  # a history list growing past this without slowing down is a stability problem (below it: settling)
PURGE_BLOCKED_TRX_S = 300  # a transaction open this long holds a read view purge cannot pass: purge tuning cannot help


def unbounded_hll(summary):
    """The undo history list grew without slowing down in this run (collector.unbounded_growth) and past
    PURGE_UNBOUNDED_MIN: purge cannot keep up. Growth that stays below it is a level still settling, not a stability problem."""
    u = next((u for u in (summary or {}).get("unbounded_growth") or [] if u.get("metric") == "history_list_length"), None)
    return u if u and max(u.get("end") or 0, (summary or {}).get("history_list_length_max") or 0) >= PURGE_UNBOUNDED_MIN else None


def purge_blocked(summary):
    """The history grows because a transaction stays open (its read view must stay visible), not because purge is too
    slow: the oldest open transaction's age when it is PURGE_BLOCKED_TRX_S or more, else None."""
    age = (summary or {}).get("oldest_trx_s_max") or 0
    return age if age >= PURGE_BLOCKED_TRX_S else None


def config_unstable(history, key):
    """Every measured run of this configuration had unbounded history growth."""
    rs = [r for r in history if config_key(r.get("managed")) == key and r.get("summary")]
    return bool(rs) and all(unbounded_hll(r.get("summary")) for r in rs)


def best_config(history, goal="tps", noise=2.0):
    """Highest mean wins when it beats the others beyond noise (scaled for the number of runs in each mean). Within
    noise: a configuration confirmed better than another tied one wins over it (an improvement kept over its parent),
    then configurations that were kept (improved / efficiency-gain / the reference), then the more often measured."""
    scores = config_scores(history, goal)
    if not scores:
        return None
    if STABILITY_FIRST:
        stable = [c for c in scores if not config_unstable(history, c["key"])]
        if stable and len(stable) < len(scores):
            scores = stable
    # candidates: configurations that earned their place (kept: confirmed improvement, efficiency gain, reference,
    # accepted best practice) or were measured at least twice and beat the best kept one beyond noise. A single
    # neutral run (e.g. one sweep point) that happens to have the highest number is not a candidate.
    def kept(c):
        rs = [r for r in history if config_key(r.get("managed")) == c["key"]]
        # a configuration that gives something up (durability, observability, ...) has to earn it with the goal itself,
        # not with efficiency alone (TATP kept doublewrite OFF for +0.03%)
        if any(tradeoff_of(k, m.get("value") if isinstance(m, dict) else m) for k, m in (c.get("managed") or {}).items()):
            return any(r.get("verdict") == "improved" and (r.get("evaluation") or {}).get("confirmed") is not False
                       and not _unearned_unsteady(r) for r in rs)
        # an improvement measured while deferred work (flushing, purge, change-buffer merges) still piled up is not earned
        # an improvement / efficiency gain is earned once its repeats confirmed it; a single unconfirmed run is not
        # (canary #202: one unconfirmed sweep point became the best safe configuration)
        conf = lambda r: (r.get("evaluation") or {}).get("confirmed")  # noqa: E731
        if STABILITY_FIRST and any((r.get("evaluation") or {}).get("stabilized") for r in rs):
            return True  # removed unbounded growth: kept for stability whatever it cost (within stability_max_cost)
        return any((r.get("verdict") == "reference" or (r.get("verdict") in KEEP and (conf(r) is True or (conf(r) is None and len(rs) >= 2))))
                   and not (r.get("verdict") == "improved" and _unearned_unsteady(r))
                   or (is_practice_run(r) and r.get("verdict") == "neutral") for r in rs)
    kept_cfgs = [c for c in scores if kept(c)]
    if kept_cfgs:
        bk = kept_cfgs[0]
        sign = -1 if goal == "latency" else 1
        scores = [c for c in scores if c in kept_cfgs or (c["n"] >= 2 and sign * (c["mean"] - bk["mean"]) / abs(bk["mean"]) * 100
                                                          > mean_noise(noise, c["n"], bk["n"]))]
    top = scores[0]
    tied = [c for c in scores if abs(c["mean"] - top["mean"]) / abs(top["mean"]) * 100 <= mean_noise(noise, top["n"], c["n"])]
    acc = accepted_practices(history)
    by_seq = {r["seq"]: r for r in history}
    # "kept over" edges: child configuration -> parent it replaced (improved, efficiency gain, accepted best practice)
    over = {}
    for r in history:
        if r.get("verdict") in ("improved", "efficiency-gain") or (is_practice_run(r) and r.get("verdict") == "neutral"):
            for ps in ((r.get("evaluation") or {}).get("parent_runs") or []):
                pk = config_key((by_seq.get(ps) or {}).get("managed"))
                if ps in by_seq and pk != config_key(r.get("managed")):
                    over.setdefault(config_key(r.get("managed")), set()).add(pk)
    beaten = set()
    for c in tied:
        runs = [r for r in history if config_key(r.get("managed")) == c["key"]]
        c["kept"] = any(r.get("verdict") in KEEP or (is_practice_run(r) and r.get("verdict") == "neutral") for r in runs)
        c["practices"] = len(acc & set(c["managed"] or {}))
        todo = list(over.get(c["key"], ()))
        while todo:  # everything this configuration was (transitively) kept over
            k = todo.pop()
            if k not in beaten and k != c["key"]:
                beaten.add(k)
                todo.extend(over.get(k, ()))
    # tie-breaker on resources: within the goal noise, using less CPU / I/O per transaction or memory is better
    eff = {}
    for k in EFFICIENCY_TIEBREAK:
        vals = {}
        for c in tied:
            xs = [(r.get("summary") or {}).get(k) for r in history if config_key(r.get("managed")) == c["key"]]
            xs = [x for x in xs if x]
            if xs:
                vals[c["key"]] = sum(xs) / len(xs)
        if len(vals) == len(tied) and len(tied) > 1:
            lo = min(vals.values())
            if lo > 0 and max(vals.values()) / lo - 1 > EFFICIENCY_TIE_MIN:  # only meaningful differences
                for key, x in vals.items():
                    eff[key] = eff.get(key, 0.0) + (x / lo - 1)
    for c in tied:
        c["resource_score"] = round(eff.get(c["key"], 0.0), 4)
    # swapping mysqld memory back in is a hazard (a thread waiting for a swap-in while holding a latch stalls the ones
    # queued behind it): among configurations tied on the goal, the one without swap-ins wins. Only a tie-break - it
    # never promotes a configuration that measured worse (#125: the -35% run was the only one without cold swap)
    for c in tied:
        c["swap_hazard"] = any(swap_hazard(r.get("summary") or {}, noise) for r in history if config_key(r.get("managed")) == c["key"])
    tied.sort(key=lambda c: (c["key"] in beaten, not c["kept"], c["swap_hazard"], -c["practices"], c["resource_score"], -c["n"],
                             c["mean"] if goal == "latency" else -c["mean"]))
    pick = tied[0]
    # never report a best that measured worse than the configuration tuning started from: ties are broken on kept-ness,
    # practices and resources, and with wide noise that picked configurations below the start (user 169 / 174, r1 HC-2,
    # RW-4: -1% to -4.5%); then the start remains the best
    start_runs = [r for r in history if str(r.get("label") or "").startswith("baseline")] or \
                 [r for r in history if r.get("verdict") == "reference"]
    if start_runs:
        sk = config_key(start_runs[0].get("managed"))
        start = next((c for c in scores if c["key"] == sk), None)
        if start and pick["key"] != sk and start["mean"]:
            # a tie within the resolution floor may still go to the leaner configuration; a real shortfall may not
            d = 100.0 * (pick["mean"] - start["mean"]) / abs(start["mean"]) * (-1 if goal == "latency" else 1)
            if d < -RESOLUTION_NOISE:
                return start
    return pick


def harmful_rules(experience):
    """Rule ids whose comparable experience shows harm (2+ losses, no win): low-downside keeping does not apply to them."""
    return {k for k, e in (experience or {}).items() if e.get("losses", 0) >= 2 and not e.get("wins") and not e.get("scope")}


def _low_kept(r, harmful):
    ev = r.get("evaluation") or {}
    if not ev.get("low_downside_kept"):
        return False
    srcs = {str(c.get("source") or "")[4:] for c in r.get("changes") or [] if str(c.get("source") or "").startswith("rec:")}
    return not (srcs & set(harmful or ()))


def revert_suggestions(history, variables, goal="tps", noise=2.0, managed=None, harmful=None):
    """Suggest returning to the best configuration when the current one is measurably worse."""
    if len(history) < 2:
        return []
    scores = config_scores(history, goal)
    if not scores:
        return []
    best = best_config(history, goal, noise)
    cur_key = config_key(managed)
    cur = next((c for c in scores if c["key"] == cur_key), None)
    items = []
    noise_txt = f"noise threshold ±{noise}%"
    if cur is not None and cur["key"] != best["key"]:
        gap = (best["mean"] - cur["mean"]) / best["mean"] * 100 * (-1 if goal == "latency" else 1)
        if gap > mean_noise(noise, best["n"], cur["n"]):
            items.append(_revert_item(f"rollback-to-run-{best['seqs'][-1]}", f"Return to the best configuration (run #{best['seqs'][-1]})",
                                      managed, best["managed"], variables,
                                      f"The current configuration averages {cur['mean']:.4g} over {cur['n']} run(s) versus {best['mean']:.4g} over "
                                      f"{best['n']} run(s) for the best one: {gap:.1f}% worse ({noise_txt}).",
                                      {"best_mean": round(best["mean"], 3), "best_runs": best["seqs"], "current_mean": round(cur["mean"], 3),
                                       "current_runs": cur["seqs"], "gap_pct": round(gap, 2)}, priority=True))
            return [i for i in items if i]
    # the current configuration is a within-noise experiment without an efficiency win: revert it to its parent
    cur_runs = [r for r in history if config_key(r.get("managed")) == cur_key]
    first_eval = next((r.get("evaluation") for r in cur_runs if (r.get("evaluation") or {}).get("parent_runs")), None)
    verdicts = {r.get("verdict") for r in cur_runs}
    just_measured = bool(history) and config_key(history[-1].get("managed")) == cur_key
    practice_kept = any(is_practice_run(r) for r in cur_runs) and "efficiency-loss" not in verdicts
    # the calibration's starting configuration is where the session starts, never an experiment to revert: judged against
    # the server defaults it can come out "neutral" (not steady) and was reverted to the defaults, then rolled back, in a
    # loop that ended RW-3 and WI-1 without a single experiment (suite r1)
    calibration = any(str(r.get("label") or "").startswith("baseline") for r in cur_runs)
    stabilized_ = STABILITY_FIRST and any((r.get("evaluation") or {}).get("stabilized") for r in cur_runs)
    low_kept = any(_low_kept(r, harmful) for r in cur_runs)  # low downside, no measured harm: kept (user, 341 huge pages)
    if just_measured and first_eval and not practice_kept and not calibration and not stabilized_ and not low_kept \
            and not verdicts & {"improved", "efficiency-gain"} \
            and verdicts & {"neutral", "efficiency-loss"}:
        parent_run = next((r for r in history if r["seq"] == first_eval["parent_runs"][-1]), None)
        if parent_run is not None:
            # priority: costs no run (merged into the next experiment) and keeps neutral changes from piling up
            items.append(_revert_item(f"revert-to-run-{parent_run['seq']}", "Revert the last experiment: no measurable benefit",
                                      managed, parent_run.get("managed") or {}, variables,
                                      f"The experiment was within the {noise_txt} on the goal ({first_eval.get('goal_delta_pct'):+.1f}%) and did not reduce "
                                      "CPU or I/O per transaction. Keeping changes without a measurable benefit only adds risk.",
                                      {"goal_delta_pct": first_eval.get("goal_delta_pct"), "parent_runs": first_eval.get("parent_runs")},
                                      priority=True))
    if not [i for i in items if i]:
        drop = _unearned(history, managed, harmful)
        if drop:
            start = next((r.get("managed") or {} for r in history if str(r.get("label") or "").startswith("baseline")), {})
            target = {k: v for k, v in (managed or {}).items() if k not in drop}
            target.update({k: start[k] for k in drop if k in start})
            items.append(_revert_item(f"drop-unearned-{history[-1]['seq']}", "Remove changes that no longer have a win behind them",
                                      managed, target, variables,
                                      f"{', '.join(sorted(drop))} stayed in the configuration after the run that kept "
                                      f"{'it' if len(drop) == 1 else 'them'} was re-judged as within noise (mean of its repeats, or a tighter "
                                      "threshold): without a measured benefit a change only adds risk. Measured once without it.",
                                      {"unearned": sorted(drop)}, priority=True))
    return [i for i in items if i]


def _big_gain(r):
    ev = r.get("evaluation") or {}
    d, nz = ev.get("goal_delta_pct"), ev.get("goal_noise_pct")
    return r.get("verdict") in ("improved", "repeat") and d is not None and nz and d >= 2 * float(nz)


def _unearned(history, managed, harmful=None):
    """Managed changes (vs the calibration's starting configuration) that no run still earns: the runs that set them
    are neither kept (improved / efficiency gain, not unconfirmed) nor an accepted best-practice bundle. Suite r1:
    MX-1 jemalloc and RW-1 log_writer_threads were downgraded after confirmation runs and stayed to the end."""
    start = next((r.get("managed") or {} for r in history if str(r.get("label") or "").startswith("baseline")), None)
    if start is None:
        return set()
    out = set()
    for k, v in (managed or {}).items():
        if normalize(k, start.get(k)) == normalize(k, v) or k.startswith("client:"):
            continue
        setters = [r for r in history if any(c.get("variable") == k and normalize(k, c.get("new")) == normalize(k, v)
                                             and str(c.get("source") or "").startswith(("rec:", "exp:")) for c in r.get("changes") or [])]
        if not setters:
            continue  # applied outside the experiments (requirements, a person): not the tuner's to remove
        # a later winner on top of it earns its own change, not this one (MX-1: ps-off improved on top of jemalloc)
        # hysteresis: once confirmed and still above its parent, a change is not dropped because the noise threshold moved
        # (canaries #198 / #201: the confirmed winner removed twice, -6.2% / -4.6%, as the threshold drifted)
        earned = any((r.get("verdict") in ("improved", "efficiency-gain") and (r.get("evaluation") or {}).get("confirmed") is not False)
                     or ((r.get("evaluation") or {}).get("ever_confirmed") and ((r.get("evaluation") or {}).get("confirmed_delta_pct") or 0) > 0)
                     or (is_practice_run(r) and r.get("verdict") in ("neutral", "efficiency-gain", "improved"))
                     # a gain of twice the threshold it was measured against is not noise however the threshold moves later
                     # (12 drops, 0 gains, 7 losses: binlog-off dropped for -25% / -28% in 249 / 271, log writers for -18%)
                     or _big_gain(r) or (STABILITY_FIRST and (r.get("evaluation") or {}).get("stabilized"))
                     or _low_kept(r, harmful) for r in setters)
        if not earned:
            out.add(k)
    # the chunk size is the companion of the instance count / pool size (it keeps the pool at the intended size): it goes
    # with them, never alone (user 191: a chunk dropped alone grew the pool from 45 to 48 GiB)
    bp_main = {"innodb_buffer_pool_instances", "innodb_buffer_pool_size"}
    if "innodb_buffer_pool_chunk_size" in out and not (bp_main & out):
        out.discard("innodb_buffer_pool_chunk_size")
    return out


def _revert_item(id_, title, managed, target, variables, rationale, evidence, priority):
    ch = []
    for k in sorted(set(managed or {}) | set(target)):
        # startup-only options (disable_log_bin) and pseudo settings are not server variables but are reverted all the same:
        # MX-1's regressed binlog-off was never rolled back and the trade-offs stacked on it (suite r1)
        known = k in variables or k in ("skip_log_bin", "disable_log_bin") or k in OPTIONS or k.startswith(("os:", "ps:", "rg:", "client:"))
        if normalize(k, (managed or {}).get(k)) != normalize(k, target.get(k)) and known:
            ch.append({"variable": k, "current": variables.get(k), "proposed": target.get(k), "unset": k not in target})
    if not ch:
        return None
    seq = id_.rsplit("-", 1)[-1]
    return {"id": id_, "title": title, "kind": "revert", "changes": ch,
            "rationale": rationale + f" `tuning-agent rollback --to-run {seq}` does the same.",
            "evidence": evidence, "impact": 3 if priority else 1, "confidence": 3 if priority else 2, "risk": "none",
            "restart": any(not KB.get(c["variable"], {}).get("dynamic", False) for c in ch), "goals": ["tps", "latency"],
            "priority": priority}


def per_query_hints(history, variables):
    """A global change helped some statements and hurt others: suggest applying it per statement with a SET_VAR hint."""
    if not history:
        return []
    last = history[-1]
    qi = (last.get("evaluation") or {}).get("query_impact") or {}
    if not qi.get("mixed") or last.get("verdict") in ("improved", "efficiency-gain"):
        return []
    hintable = [c for c in (last.get("changes") or []) if not c["variable"].startswith("os:")
                and KB.get(c["variable"], {}).get("cat") in ("optimizer", "memory") and KB.get(c["variable"], {}).get("dynamic")]
    if not hintable:
        return []
    winners = [r for r in qi["rows"] if r["faster_pct"] > qi["noise_pct"]]
    sv = ", ".join(f"{c['variable']}={c['new']}" if c["variable"] != "optimizer_switch" else f"optimizer_switch='{c.get('new')}'" for c in hintable)
    return [{
        "id": f"per-query-{hintable[0]['variable']}", "title": "Apply the change only to the statements it helps (SET_VAR hint)",
        "kind": "advice", "changes": [], "safety": "safe", "tradeoffs": [],
        "rationale": f"Run #{last['seq']} changed {', '.join(c['variable'] for c in hintable)} globally: {qi['winners']} top statement(s) got "
                     f">{qi['noise_pct']:.0f}% faster but {qi['losers']} got slower, so the workload as a whole did not improve. "
                     f"MySQL can scope the setting to single statements: SELECT /*+ SET_VAR({sv}) */ ... "
                     "(per-statement settings, as in CMU's query-level tuning work).",
        "evidence": {"faster": [f"{r['faster_pct']:+.0f}% {r['query'][:70]}" for r in winners[:4]],
                     "slower": [f"{r['faster_pct']:+.0f}% {r['query'][:70]}" for r in qi["rows"] if r["faster_pct"] < -qi["noise_pct"]][:4]},
        "impact": 2, "confidence": 2, "risk": "none", "restart": False, "goals": ["tps", "latency"],
    }]


def _breaks_requirement(it, requires):
    """True when a change would lower a benchmark prerequisite (those are not tuning knobs for this benchmark)."""
    for ch in it.get("changes") or []:
        want = requires.get(ch.get("variable"))
        if want is None:
            continue
        try:
            if float(ch.get("proposed")) < float(want):
                return True
        except (TypeError, ValueError):
            if str(ch.get("proposed")).lower() != str(want).lower():
                return True
    return False


def no_evidence(it, ctx):
    """Why there is no evidence for this change (None = there is). Evidence is any combination of sources showing the
    thing the change acts on: measured time (CPU profile, off-CPU profile, performance_schema waits, eBPF probes) or a
    diagnosis grounded in status counters, performance_schema or system statistics (PSI, iowait, swap-in, free-page
    waits, checkpoint age, ...). Changes whose mechanism is unknown cannot be judged and keep their place."""
    tags = set((MECHANISMS.get(it["id"]) or ((),))[0]) | set(it.get("acts_on_tags") or ())
    if not tags or it.get("source") == "requested":
        return None
    parts = ctx.get("breakdown") or []
    hit = bottleneck.acted_on(tags, parts)
    if hit:
        # Amdahl: removing the targeted time entirely gains at most its share of the busy (non-idle-client) time; when
        # that is below the noise threshold the experiment cannot show an effect (#132: read-ahead sweep on 0.2% I/O)
        idle = sum(p["share_pct"] for p in parts if "network" in p["tags"])
        share = sum(p["share_pct"] for p in hit)
        busy_share = 100.0 * share / max(1e-6, 100.0 - idle)
        noise = float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 0)
        if not noise or busy_share >= noise:
            return None
        diag_hit = {d["type"] for d in ctx.get("diagnosis") or [] if d["type"] != "none"} & bottleneck.specific(tags, parts)
        if not diag_hit:
            return (f"what it acts on takes {busy_share:.1f}% of the busy database time "
                    f"({', '.join(p['name'] for p in hit[:3])}): even removing it could not exceed the ±{noise:.1f}% noise")
    # a generic diagnosis ("I/O pressure") is not evidence for a change aimed at a specific kind once the kind is known
    rel = bottleneck.specific(tags, parts)
    diag = {d["type"] for d in ctx.get("diagnosis") or [] if d["type"] != "none"}
    if "io" in rel and rel & {"io-read", "io-flush", "commit", "redo"} and diag & {"io-read", "io-flush", "commit"}:
        rel.discard("io")
    if rel & diag:
        return None
    srcs = sorted({p.get("source", "") for p in ctx.get("breakdown") or []} - {""})
    return (f"no evidence for what it acts on ({', '.join(sorted(tags))}): no measured time"
            f"{' (' + '; '.join(srcs) + ')' if srcs else ''} and no counter or system statistic points at it")


def numa_evidence(s, osf, bp=0):
    """What the run measured on a multi-node host: None on a uniform host, else {"imbalance": [why], ...}."""
    nn = int(osf.get("os:numa_nodes") or 1)
    if nn < 2:
        return None
    mem = [float(x) * GiB for x in str(osf.get("os:numa_node_mem_gb") or "").split(",") if x]
    why = []
    skew = s.get("numa_mysqld_max_node_pct")
    if skew is not None and skew > 100.0 / nn + 25:
        why.append(f"{skew:.0f}% of mysqld's memory is on one of {nn} nodes")
    fmin, fmax = s.get("numa_node_free_min_mb"), s.get("numa_node_free_max_mb")
    if mem and fmin is not None and fmax is not None and fmin * MiB < 0.03 * min(mem) and fmax * MiB > 0.2 * max(mem):
        why.append(f"one node is out of memory ({fmin:.0f} MB free) while another has {fmax:.0f} MB free")
    if mem and bp and bp > 0.8 * min(mem):
        why.append(f"the {human_size(bp)} buffer pool does not fit one node ({human_size(min(mem))})")
    if (s.get("numa_miss_per_s") or 0) > 0 and (s.get("numa_other_node_pct") or 0) >= 1:
        why.append(f"{s['numa_other_node_pct']:.1f}% of allocations came from another node ({s['numa_miss_per_s']:.0f} misses/s)")
    return {"nodes": nn, "node_mem": mem, "imbalance": why,
            "migrating": (s.get("numa_pages_migrated_per_s") or 0) > 0 or (s.get("numa_hint_faults_per_s") or 0) > 0}


def _numa_rules(b, v, s, osf, ctx, have_run, bp, ram):
    """Multi-node hosts: zone reclaim and automatic balancing (best practices), then memory placement from evidence:
    the buffer pool interleaved (InnoDB), all of mysqld's memory interleaved (systemd NUMAPolicy), or mysqld kept on
    one node when it fits there (CPUs and memory)."""
    ev = numa_evidence(s, osf, bp)
    if not ev:
        return
    nn = ev["nodes"]
    if ctx.get("os_tuning") and v.get("os:vm.zone_reclaim_mode") not in (None, "", "0"):
        b.add("os-zone-reclaim-off", "Allocate from other NUMA nodes instead of reclaiming locally", [("os:vm.zone_reclaim_mode", "0")],
              f"vm.zone_reclaim_mode = {v.get('os:vm.zone_reclaim_mode')} on {nn} NUMA nodes: a full node drops page cache or swaps "
              "although other nodes have free memory (the 'MySQL swap insanity' pattern); database guidance is 0.",
              {"numa_nodes": nn, "zone_reclaim_mode": v.get("os:vm.zone_reclaim_mode")}, impact=2, confidence=3)
    if ctx.get("os_tuning") and v.get("os:kernel.numa_balancing") == "1":
        moving = (f"{s.get('numa_pages_migrated_per_s') or 0:.0f} pages/s migrated, {s.get('numa_hint_faults_per_s') or 0:.0f} hint faults/s"
                  if have_run else "")
        b.add("os-numa", "Stop automatic NUMA page migration", [("os:kernel.numa_balancing", "0")],
              f"{nn} NUMA nodes{': ' + moving if moving else ''}: the kernel samples and migrates database pages between nodes "
              "(page faults and copies on hot buffer pool pages); database profiles (tuned oracle / mssql) disable it.",
              {"numa_nodes": nn, "numa_pages_migrated_per_s": s.get("numa_pages_migrated_per_s"),
               "numa_hint_faults_per_s": s.get("numa_hint_faults_per_s")}, impact=2 if ev["migrating"] else 1, confidence=2)
    if not have_run:
        return
    why = "; ".join(ev["imbalance"])
    evidence = {"numa_nodes": nn, "numa_mysqld_max_node_pct": s.get("numa_mysqld_max_node_pct"),
                "numa_other_node_pct": s.get("numa_other_node_pct"), "numa_node_free_min_mb": s.get("numa_node_free_min_mb")}
    if ev["imbalance"] and "innodb_numa_interleave" in v and not _on(v, "innodb_numa_interleave"):
        b.add("numa-interleave", "Interleave the buffer pool across NUMA nodes", [("innodb_numa_interleave", "ON")],
              f"Measured: {why}. InnoDB allocates the buffer pool interleaved across all nodes: every node keeps free memory and "
              "accesses spread evenly instead of one node serving (and running out for) everything. Needs a restart.",
              evidence, impact=3, confidence=2)
    if ev["imbalance"] and ctx.get("os_tuning") and v.get("os:mysqld_numa_policy", "default") == "default" and "os:mysqld_numa_policy" in v:
        b.add("numa-interleave-all", "Interleave all of mysqld's memory across NUMA nodes", [("os:mysqld_numa_policy", "interleave")],
              f"Measured: {why}. Beyond the buffer pool (innodb_numa_interleave), connection buffers, caches and the dictionary also "
              "come from one node; a process-wide interleave policy (numactl --interleave, systemd NUMAPolicy) spreads them too. "
              "Needs a restart.", evidence, impact=2, confidence=1)
    # the whole server fits one node (CPUs it needs and its memory): local memory only, no cross-node traffic at all
    # mysqld_cpu_pct is a share of the whole host: CPUs used = share x host CPUs (user 194: "0.2 CPUs" for ~6.8); memory is
    # the larger of what was measured (huge pages included) and the configured buffer pool (17 GiB measured for a 188 GiB pool)
    cpus_used = (s.get("mysqld_cpu_pct") or 0) / 100.0 * int((ctx.get("hw") or {}).get("cpus") or 1)
    lists = str(osf.get("os:numa_node_cpulists") or "").split(";")
    ncpus = [int(x) for x in str(osf.get("os:numa_node_cpus") or "").split(",") if x.isdigit()]
    db_mem = max((s.get("db_mem_mb") or 0) * MiB, _int(v, "innodb_buffer_pool_size", 0))
    remote = (s.get("numa_other_node_pct") or 0) >= 5  # only with measured cross-node traffic to remove (194: 0.0-0.47%)
    if ctx.get("os_tuning") and remote and lists and lists[0] and ncpus and ev["node_mem"] and "os:mysqld_numa_policy" in v \
            and 0 < cpus_used < 0.7 * ncpus[0] and db_mem and db_mem < 0.7 * ev["node_mem"][0] and v.get("os:mysqld_numa_policy", "default") == "default":
        # InnoDB's own interleave overrides the process policy for the buffer pool (verified on a 2-node VM: bind:0 left the
        # pool interleaved over both nodes) - it goes off with the bind
        ch = [("os:cpu_affinity", lists[0]), ("os:mysqld_numa_policy", "bind:0")]
        if _on(v, "innodb_numa_interleave"):
            ch.append(("innodb_numa_interleave", "OFF"))
        b.add("numa-single-node", "Keep mysqld on one NUMA node", ch,
              f"mysqld uses {cpus_used:.1f} CPUs and {human_size(db_mem)} - both fit node 0 ({ncpus[0]} CPUs, {human_size(ev['node_mem'][0])}) - "
              f"and {s.get('numa_other_node_pct')}% of its allocations come from another node: bound there, every memory access is local. Kept only if the goal improves; memory growth beyond the node would fail "
              "instead of spilling over, so it is proposed only while mysqld uses under 70% of the node's memory. Experiment.",
              dict(evidence, mysqld_cpus=round(cpus_used, 2), node0_cpus=ncpus[0]), impact=2, confidence=1)


def _swapfile_bytes(val):
    try:
        return 0 if str(val or "0").lower() in ("0", "off", "") else parse_size(val)
    except Exception:
        return 0


def basis(it, exp_delta=0):
    """What this recommendation rests on, for the person auditing it: where the idea came from (a built-in rule, the AI, the
    user) and each part of its ranking by kind - built-in (the shipped rules and catalog: mechanism vs measured bottleneck,
    shipped impact), learned (from earlier sessions of every account: experience, hypothesis track record, learned impact,
    insights from learning.db) - with the score each part added."""
    src = it.get("source")
    origin = ("llm" if src in ("llm", "planner") else "user" if src == "requested" else "rule")
    out = {"origin": origin, "rule": it["id"] if origin == "rule" else None, "built_in": [], "learned": []}
    if it.get("relevance") is not None:
        out["built_in"].append({"what": "mechanism vs measured bottleneck" + (f" ({', '.join(it.get('addresses') or [])})" if it.get("addresses") else ""),
                                "score": 2 * it["relevance"]})
    pi = it.get("param_impact") or {}
    if pi:
        part = {"what": f"parameter impact {pi.get('impact')}", "score": knowledge.IMPACT_SCORE.get(pi.get("impact"), 0)}
        (out["learned"] if pi.get("source") == "learned" else out["built_in"]).append(part)
    e = it.get("experience") or {}
    if e.get("n"):
        out["learned"].append({"what": f"earlier sessions: {e.get('wins', 0)} wins, {e.get('losses', 0)} losses in {e['n']} runs"
                                       + (f" ({e['scope']})" if e.get("scope") else "") + (" - known neutral here" if it.get("known_neutral") else ""),
                               "score": exp_delta})
    rs = it.get("rescore") or {}
    if rs.get("delta"):
        out["learned"].append({"what": "hypothesis track record: " + "; ".join(rs.get("why") or [])[:200], "score": rs["delta"]})
    for x in it.get("insights") or []:
        out["learned"].append({"what": f"insight #{x.get('id')} ({x.get('status')}): {str(x.get('statement'))[:160]}",
                               "insight": x.get("id"),
                               "score": round(float(x.get("weight") or 1.0) * (1 if x["direction"] == "helps" else -2 if x["direction"] == "hurts" else -1), 2)
                               if x.get("status") == "repeated" else 0})
    return out


VM_VENDORS = ("qemu", "kvm", "linode", "akamai", "xen", "vmware", "microsoft corporation", "amazon ec2", "google", "digitalocean",
              "openstack", "innotek", "bochs", "parallels", "red hat", "hetzner", "vultr")
OVERSUB_CONN_PER_CPU = 16  # connections per vCPU from which autogroups and concurrency limiters lost (299/300: -13% to -28%)


LIMITERS = ("innodb_thread_concurrency", "os:kernel.sched_autogroup_enabled")


def oversubscription_block(it, ctx):
    """Concurrency limiters and scheduler groups at 16+ connections per vCPU - rule or AI ideas - lost every time
    (299 AI innodb_thread_concurrency=24 -17.5%, 300 os-autogroup -27.5%): not run; returns the reason, else None."""
    s = ctx.get("summary") or {}
    cpus = int((ctx.get("hw") or {}).get("cpus") or 1)
    conns = s.get("threads_connected_avg") or 0
    # introducing a limiter is blocked; tuning one already in place (a stability remedy) is not
    cur = ctx.get("variables") or {}
    lim = [c["variable"] for c in it.get("changes") or [] if c.get("variable") in LIMITERS and str(c.get("proposed")) not in ("0", "1")
           and str(cur.get(c["variable"]) or "0") in ("0", "", "OFF", "1" if c["variable"].startswith("os:") else "0")]
    if lim and conns >= OVERSUB_CONN_PER_CPU * cpus:
        return (f"Not run: {conns:.0f} connections on {cpus} vCPUs ({conns / cpus:.0f} per vCPU): limiting concurrency or regrouping the "
                f"scheduler ({', '.join(lim)}) lost in every comparable session at 16+ connections per vCPU.")
    return None


def is_vm(hw):
    """A virtual machine (DMI system vendor of a hypervisor): CPU frequency and idle states belong to the host."""
    vendor = str((hw or {}).get("vendor") or "").lower()
    return any(x in vendor for x in VM_VENDORS)


def matching_insights(it, insights):
    """Learned insights about the settings this change sets (the same variable and value, or the same variable)."""
    ch = {str(c["variable"]).lower(): str(c.get("proposed")).lower() for c in it.get("changes") or []}
    out = []
    for x in insights or []:
        vs = {str(s.get("variable")).lower(): str(s.get("value")).lower() for s in x.get("settings") or []}
        if vs and all(k in ch for k in vs) and all(not v or ch[k] == v for k, v in vs.items()):
            out.append({k: x.get(k) for k in ("id", "statement", "direction", "status", "confidence", "support", "contradict",
                                               "sessions", "hosts", "caveats", "weight", "scope")})  # weight: same server series counts fully
    return out[:3]


def hypothesis(it, ctx):
    """Why the tuner wants to test this change, stated as a testable claim about the last run."""
    h = _hypothesis(it, ctx)
    leads = [it["lead"]] if it.get("lead") else []  # outside reports only motivate a test; the claim stays what this run measures
    for x in it.get("insights") or []:
        # a summary of earlier experiments, not a fact about this system: it may not repeat here
        leads.append(f"Earlier experiments ({x['status']}: {x['support']} runs for, {x['contradict']} against, "
                     f"{x.get('sessions') or '?'} session(s) on {x.get('hosts') or '?'} host(s)) - may not hold here: {x['statement']}"
                     + (f" Caveats: {x['caveats']}" if x.get("caveats") else ""))
    if leads:
        h["lead"] = " ".join(leads)
    return h


def _hypothesis(it, ctx):
    first = (it.get("rationale") or "").split(". ")[0].strip().rstrip(".")
    if it["kind"] == "revert":
        return {"text": f"{it.get('title')}: {first}.", "kind": "revert"}
    if it.get("no_mechanism"):
        return {"text": f"Validation only - {it['no_mechanism']}.", "kind": "validation"}
    tags = list(MECHANISMS.get(it["id"], ((), ""))[0]) or list(it.get("acts_on_tags") or [])
    parts = [p for p in ctx.get("breakdown") or [] if set(tags) & set(p["tags"]) and "network" not in p["tags"]]
    mech = MECHANISMS.get(it["id"], ((), ""))[1]
    if parts:
        top = parts[0]
        share = sum(p["share_pct"] for p in parts)
        names = ", ".join(p["name"] for p in parts[:3])
        return {"kind": "bottleneck", "target": [p["name"] for p in parts], "tags": tags, "share_pct": round(share, 1),
                "text": f"{names} take{'s' if len(parts) == 1 else ''} {share:.0f}% of database time in the last run; "
                        f"{mech[:1].lower() + mech[1:] if mech else first} "
                        f"Prediction: the goal improves beyond noise and {top['name']} shrinks per transaction."}
    diag = [d["type"] for d in ctx.get("diagnosis") or [] if d["type"] in tags]
    if diag:
        return {"kind": "diagnosis", "tags": tags, "target": diag,
                "text": f"Diagnosis {', '.join(diag)}: {first}. Prediction: the goal improves beyond noise."}
    return {"kind": "exploratory", "tags": tags,
            "text": f"Exploratory - no measured bottleneck points at it: {first}. Tried after better-founded ideas."}


def credibility(it, hexp):
    """How often this idea's hypothesis (same change, same targeted component) held in earlier sessions:
    (score delta, note). Similar sessions (workload, machine size, concurrency) count fully, others half."""
    h = it.get("hypothesis") or {}
    tgt = (h.get("target") or ["-"])[0]
    for scope, w in (("similar", 1.0), ("all", 0.5)):
        e = next((x for x in (hexp or {}).get(scope) or [] if x["id"] == it["id"] and x["target"] == tgt), None)
        if e and e["n"] >= 2:
            held = e.get("supported", 0) + e.get("improved-other-mechanism", 0) + e.get("improved-unverified", 0)
            rate = held / e["n"]
            # the track record has to be able to outweigh a tag match (canaries: ahi-on held in 0 of 5 and still ran in
            # 16 of 20 sessions; +-2 points never moved it): up to +-4, and a lever that never held in 3+ tests drops further
            delta = round(w * 8 * (rate - 0.5) * min(e["n"], 4) / 4, 2)
            if held == 0 and e["n"] >= 3:
                delta -= round(w * 4, 2)
            where = "similar sessions" if scope == "similar" else "other workloads / machines"
            return delta, (f"held in {held} of {e['n']} earlier tests in {where}"
                           + (f"; {e.get('moved', 0)} times the targeted time shrank without a gain" if e.get("moved") else ""))
    return 0.0, None


def rescore(it, hyps):
    """Adjust an idea by what earlier hypotheses in this session showed: (score delta, notes)."""
    tags = set(MECHANISMS.get(it["id"], ((), ""))[0])
    targets = set((it.get("hypothesis") or {}).get("target") or [])
    delta, notes = 0.0, []
    for h in hyps or []:
        if h.get("id") == it["id"]:
            continue
        ht, htags = set(h.get("target") or []), set(h.get("tags") or [])
        o = h.get("outcome")
        if o == "supported" and tags & htags:
            delta += 1
            notes.append(f"run {h['run']} supported acting on {', '.join(sorted(ht)) or 'this bottleneck'}: related lever")
        elif o == "moved" and targets & ht:
            delta -= 2
            notes.append(f"run {h['run']} shrank {', '.join(sorted(targets & ht))} without a gain: not what limits the goal now")
        elif o == "refuted" and tags == htags and tags:
            delta -= 1
            notes.append(f"run {h['run']} refuted the same mechanism")
    return delta, notes


ALSO_FROM_DIAGNOSIS = {"io-read", "io-flush"}
GENERIC_RESOURCE = {"cpu", "cpu-pressure", "sched-delay", "io", "concurrency"}  # diagnoses of a whole resource (or of many runnable threads), not of one way to spend it
SPECIFIC_KINDS = set().union(*bottleneck.SPECIFIC.values())
QUEUE_LEVERS = {"thread-pool", "sweep-thread-concurrency"}
STOP_AFTER_FRUITLESS = 6  # status / OS evidence (read vs write split) the wait classes cannot separate


def run_engine(ctx):
    ctx = dict(ctx)
    ctx["workload"] = classify_workload(ctx.get("summary"), ctx.get("inputs")) if ctx.get("summary") else {"type": None}
    # where the database's time goes (on CPU by profile category, waiting for CPU / I/O / locks / latches / the client)
    inp = ctx.get("inputs") or {}
    ctx["breakdown"] = inp.get("time_breakdown") or bottleneck.breakdown(ctx.get("summary"), inp, (ctx.get("hw") or {}).get("cpus"))
    ctx["diagnosis"] = diagnose(ctx) if ctx.get("summary") else []
    items = recommend(ctx)
    items = apply_history(items, ctx.get("history") or [], (ctx.get("summary") or {}).get("swap_total_mb"), ctx.get("managed"))
    items = practice_bundle(items, ctx.get("history") or [])
    items = stack_small_wins(items, ctx)
    items = items + per_query_hints(ctx.get("history") or [], ctx["variables"])
    items = revert_suggestions(ctx.get("history") or [], ctx["variables"], ctx.get("goal", "tps"),
                               float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0), ctx.get("managed"),
                               harmful_rules(ctx.get("experience"))) + items
    items = [it for it in items if not _breaks_requirement(it, (ctx.get("bench") or {}).get("requires") or {})]
    # the account's parameter policy (admin defaults + the user's own list): blocked settings never become experiments;
    # returning a blocked setting to an earlier value (revert / rollback) is still allowed
    blocked = set(ctx.get("blocked") or [])
    if blocked:
        keep = []
        for it in items:
            hit = sorted({n for c in it.get("changes") or [] for n in [c["variable"]] + knowledge.setting_names(c["variable"], c.get("proposed"), c.get("current"))} & blocked)
            if hit and it["kind"] != "revert" and not it["id"].startswith(("revert", "rollback")):
                ctx.setdefault("_blocked_items", []).append({"id": it["id"], "variables": hit})
                continue
            keep.append(it)
        items = keep
    # memory: a change to a memory-related setting must keep the configuration within this host's memory budget
    # (memory.budget: caches, connections, 2 x CPUs active connections with their per-query buffers); beyond it the
    # configuration may swap or be OOM-killed under load - a trade-off, never a safe change
    from . import memory as _mem
    hw_ = ctx.get("hw") or {}
    if hw_.get("ram_bytes"):
        other_ = _mem.host_other_bytes(ctx.get("summary"), hw_.get("ram_bytes"))
        act_ = _mem.active_from(ctx.get("summary"))
        cur_b = _mem.budget(ctx["variables"], hw_, ctx.get("client_reserve") or 0, host_other=other_, active_measured=act_)
        # the model checked against the run: mysqld's measured memory (resident + swapped) above what the model says for the
        # current configuration is an error of the model on this host and workload - carried into every projection
        sm_ = ctx.get("summary") or {}
        huge_ = (sm_.get("hugepages_total_mb") or 0) if str(ctx["variables"].get("large_pages", "")).upper() in ("ON", "1") else 0
        seen = ((sm_.get("dbproc_pss_mb") or 0) + (sm_.get("dbproc_swap_pss_mb") or 0) + huge_) * MiB
        offset = max(0, seen - cur_b["mysqld_bytes"]) if sm_.get("dbproc_pss_mb") else 0
        sm0 = ctx.get("summary") or {}
        swap_free0 = max(0, ((sm0.get("swap_total_mb") or 0) - (sm0.get("swap_used_mb") or 0))) * MiB

        # without swap, going over is an OOM kill, not slower pages: keep a margin the model cannot promise to be exact to
        # (r2 RO-4.ns: 11G estimated at 15.3 of 15.6 GiB, OOM-killed) - at least 1 GiB or 5% of RAM
        margin0 = max(GiB, int(0.05 * (hw_.get("ram_bytes") or 0))) if swap_free0 < GiB else 0

        def _fits(var, val):  # a configuration with this value stays within RAM + free swap (else: an OOM kill, not a trade-off)
            nb = _mem.budget(dict(ctx["variables"], **{var: val}), hw_, ctx.get("client_reserve") or 0, host_other=other_, active_measured=act_)
            return nb["needed"] + offset + margin0 <= nb["ram"] + swap_free0 or nb["needed"] <= cur_b["needed"]
        for it in items:
            sw = it.get("sweep") or {}
            if sw.get("variable") in _mem.MEMORY_VARS and it["kind"] == "config":
                # every value of a sweep, not only the first one the item carries (suite r2 RO-4.ns: 11G was checked, 12G
                # was not, and OOM-killed mysqld on a 15.6 GiB host without swap)
                keep = [x for x in sw["values"] if _fits(sw["variable"], x)]
                if len(keep) < len(sw["values"]):
                    dropped = [x for x in sw["values"] if x not in keep]
                    it["rationale"] = (it.get("rationale") or "") + (f" Left out: {', '.join(dropped)} (more than RAM plus free swap"
                                                                       " on this host).")
                    if len(keep) >= 2:
                        sw["values"] = keep
                    else:
                        it.pop("sweep", None)
                    if keep:
                        for c_ in it["changes"]:
                            if c_["variable"] == sw["variable"]:
                                c_["proposed"] = keep[0]
            ch = {c["variable"]: c.get("proposed") for c in it.get("changes") or [] if c["variable"] in _mem.MEMORY_VARS and not c.get("unset")}
            if not ch or it["kind"] not in ("config",):
                continue
            nb = _mem.budget(dict(ctx["variables"], **ch), hw_, ctx.get("client_reserve") or 0, host_other=other_, active_measured=act_)
            if offset:
                nb["needed"] += offset
                nb["mysqld_bytes"] += offset
                nb["headroom"] -= offset
                nb["over_budget"] = bool(nb["ram"]) and nb["needed"] > nb["ram"]
                nb["measured_offset"] = offset
            it.setdefault("evidence", {})["memory_budget"] = _mem.text(nb)
            sm = ctx.get("summary") or {}
            swap_free = max(0, ((sm.get("swap_total_mb") or 0) - (sm.get("swap_used_mb") or 0))) * MiB
            if any(c["variable"] == "os:swapfile" for c in it.get("changes") or []):
                swap_free += _swapfile_bytes(next(c["proposed"] for c in it["changes"] if c["variable"] == "os:swapfile"))
            # without swap a margin the model cannot promise to be exact to: an 11G pool estimated at 15.3 of 15.6 GiB was
            # OOM-killed (r2 RO-4.ns) - at least 1 GiB or 5% of RAM before the configuration counts as fitting
            margin = max(GiB, int(0.05 * nb["ram"])) if swap_free < GiB else 0
            if (nb["over_budget"] or nb["needed"] + margin > nb["ram"]) and nb["needed"] > cur_b["needed"]:
                # beyond RAM + free swap the kernel OOM-kills mysqld (suite RO-4: a 12 GB pool on 16 GB with 0.5 GB swap):
                # that is a crash, not a trade-off. With OS tuning the experiment brings a swap file of 20% of RAM (4-16 GiB)
                # (pressure then shows as swapping in the run); without it the change is not run.
                if nb["needed"] + margin > nb["ram"] + swap_free:
                    size = max(nb["needed"] - nb["ram"] - swap_free + GiB, swap_target(nb["ram"]) // GiB * GiB)
                    if size <= SWAP_CAP and ctx.get("os_tuning") and not ctx.get("no_swap") and "os:swapfile" in ctx["variables"] and not any(
                            c["variable"] == "os:swapfile" for c in it.get("changes") or []):
                        it["changes"].append({"variable": "os:swapfile", "current": ctx["variables"].get("os:swapfile"), "proposed": fmt_size(size)})
                        it["rationale"] = (it.get("rationale") or "") + (f" Needs {human_size(nb['needed'])} with {human_size(nb['ram'])} RAM and "
                                           f"{human_size(swap_free)} free swap: a {fmt_size(size)} swap file comes with it, so memory pressure "
                                           "shows as swapping instead of an OOM kill.")
                    else:
                        it["confidence"] = 0
                        it["note"] = (f"Not run: needs {human_size(nb['needed'])}"
                                      + (f" plus a {human_size(margin)} safety margin (no swap: going over is an OOM kill)" if margin else "")
                                      + f", more than {human_size(nb['ram'])} RAM plus {human_size(swap_free)} free swap - mysqld would be OOM-killed"
                                      + (" (no swap by design on this host)." if ctx.get("no_swap") else
                                         f" (it would need a {fmt_size(size)} swap file, more than the {fmt_size(SWAP_CAP)} cap)." if size > SWAP_CAP
                                         else " (swap needs OS tuning)."))
                        continue
                it["safety"] = "trade-off"
                it["tradeoffs"] = list(it.get("tradeoffs") or []) + [{
                    "variable": ", ".join(sorted(ch)), "value": ", ".join(f"{k}={v}" for k, v in ch.items()), "category": "memory",
                    "lost": f"Production safety: the configuration would need {_mem.text(nb)} - under load it can swap or be "
                            "OOM-killed. Measured only as a benchmark trade-off."}]
    benchmark = ctx.get("policy") == "benchmark"
    has_major = any(d.get("severity") in ("high", "medium") and d["type"] != "none" for d in ctx["diagnosis"])
    exp = ctx.get("experience") or {}
    unstable_now = bool(unbounded_hll(ctx.get("summary")))
    # stability first, but bounded: the first STABILITY_TRIES remedies run before tuning, then they compete by score
    # (backtest 288: purge chased 6 times, -1.2% / -0.75% / -0.3%, while trade-offs worth +23% to +66% waited)
    stab_tried = sum(1 for h in ctx.get("history") or [] if any(str(c.get("source") or "")[4:] in STABILITY_IDS
                                                               or str(c.get("source") or "").startswith("rec:sweep-purge")
                                                               for c in h.get("changes") or []))
    busy_now = (ctx.get("summary") or {}).get("cpu_busy") or 0
    # the purge lag bound throttles every writer: a stopgap, measured only when no other stability remedy is left to try
    tried_ids = {str(c.get("source") or "")[4:] for h in ctx.get("history") or [] for c in h.get("changes") or []}
    tried_ids |= {"sweep-purge-threads" for x in tried_ids if x.startswith("sweep-purge")}
    others_left = any(i["id"] in STABILITY_IDS and i["id"] not in STABILITY_LAST_RESORT and i["kind"] == "config"
                      and i.get("confidence", 0) > 0 and i["id"] not in (ctx.get("failed") or []) and i["id"] not in tried_ids
                      for i in items)
    for it in items:
        it.setdefault("safety", "safe")
        it.setdefault("tradeoffs", [])
        # mechanism prior: how much of what keeps the workload from going faster can this change act on? From the time
        # breakdown when there is one; diagnoses the breakdown cannot see (flush pressure, redo capacity, memory / swap)
        # still count through the diagnosis
        rel, matched = relevance(it["id"], ctx["diagnosis"])
        if it["id"] in MECHANISMS and ctx["breakdown"]:
            rb, mb = bottleneck.relevance(MECHANISMS[it["id"]][0], ctx["breakdown"])
            # evidence combines sources: a diagnosis from counters / system statistics counts unless this run's measured
            # time already covers that bottleneck (then its share decides)
            in_time = {t for p in ctx["breakdown"] for t in p["tags"]}
            # waiting for a CPU is measured too (the off-CPU profile's run-queue share): it decides instead of the
            # scheduling-delay diagnosis (session 194: thread pool ranked first on 'sched-delay' with 9.7% of time queued)
            q = next((p.get("cpu_queue_share_pct") for p in ctx["breakdown"] if p.get("cpu_queue_share_pct") is not None), None)
            if q is not None:
                in_time.add("sched-delay")
            diag_ = [d for d in ctx["diagnosis"] if d["type"] not in in_time or d["type"] in ALSO_FROM_DIAGNOSIS]
            if set(MECHANISMS[it["id"]][0]) & SPECIFIC_KINDS & in_time and it["id"] not in QUEUE_LEVERS:
                # the specific kind it acts on is measured (spinning, the allocator, read I/O ...): that share decides, not a
                # diagnosis of the whole resource - big items first (user, 353: spin-loops ranked first with +6 for "CPU
                # saturated" while spinning was 1% of the time)
                diag_ = [d for d in diag_ if d["type"] not in GENERIC_RESOURCE or d["type"] in ALSO_FROM_DIAGNOSIS]
            rd, md = relevance(it["id"], diag_)
            rel, matched = max(rb or 0, rd or 0), mb + md
            # only levers that reduce the queue itself (admission control) earn the queue share: the boost for every
            # "sched-delay" tag lifted log-writer-off, spin-loops and autogroup as high as the thread pool (r1 review)
            if q is not None and it["id"] in QUEUE_LEVERS:
                rq = 4 if q >= 60 else 3 if q >= 30 else 2 if q >= 15 else 1 if q >= 5 else 0
                if rq > rel:
                    rel, matched = rq, matched + [f"Waiting for a CPU ({q:.0f}%)"]
        if it["id"] not in MECHANISMS and it.get("acts_on_tags") and ctx["breakdown"]:
            # a bundle (or an analysed suggestion) acts on what its members act on
            rel, matched = bottleneck.relevance(it["acts_on_tags"], ctx["breakdown"])
            it["mechanism"] = "Acts on what its settings act on: " + ", ".join(it["acts_on_tags"])
            it["addresses"] = matched
            it["relevance"] = rel
        elif rel is not None:
            it["mechanism"] = MECHANISMS[it["id"]][1]
            it["addresses"] = matched
            it["relevance"] = rel
        nomech = missing_mechanism(it["id"], ctx.get("summary"), [{"variable": c["variable"]} for c in it.get("changes") or []],
                                   inp.get("status_rates"), inp) if it["kind"] == "config" else None
        if nomech:
            # what it acts on provably does not happen in this workload (a counter at zero, no writes): advice, never a
            # run (r1: log-writer-threads-off with "redo 0.00 MB/s", thread cache with Threads_created = 0 still ran)
            it["mechanism_absent"] = nomech
            it["confidence"] = 0
        if not nomech and it["kind"] == "config" and ctx.get("summary"):
            nomech = no_evidence(it, ctx)
        if nomech:
            it["no_mechanism"] = nomech
            it["note"] = (it.get("note", "") + f" Validation only: {nomech}.").strip()
            it.pop("sweep", None)  # one point at most, after everything with a mechanism
        if it["kind"] == "config" and it.get("changes"):
            # parameter catalog: what the changed settings can move when they count (learned impact overrides the shipped
            # prior once there is measured evidence in these conditions)
            names = [n for c in it["changes"] for n in knowledge.setting_names(c["variable"], c.get("proposed"), c.get("current"))]
            imps = [knowledge.effective_impact(n, ctx.get("param_impact")) for n in names]
            best_imp = max(imps, key=lambda x: knowledge.IMPACT_SCORE.get(x[0], 0))
            it["param_impact"] = {"impact": best_imp[0], "source": best_imp[1]}
            it["score_prior"] = it.get("score_prior", 0) + knowledge.IMPACT_SCORE.get(best_imp[0], 0)
            if knowledge.IMPACT_SCORE.get(best_imp[0], 0):
                it.setdefault("score_parts", []).append([f"parameter impact {best_imp[0]} ({'learned' if best_imp[1] == 'learned' else 'catalog'})",
                                                         knowledge.IMPACT_SCORE[best_imp[0]]])
        learned = matching_insights(it, ctx.get("insights"))
        if learned:
            # what the learner extracted from earlier sessions in these conditions (learning.db): a lead and a small prior
            # for validated insights - the evidence gate above still decides whether the change runs at all
            it["insights"] = learned
            for x in learned:  # only results that repeated across sessions move the ranking, and only a little
                if x["status"] == "repeated":  # weighted by how close the insight's server was (variant, release series)
                    w_ = float(x.get("weight", 1.0)) * (1 if x["direction"] == "helps" else -2 if x["direction"] == "hurts" else -1)
                    it["score_prior"] = it.get("score_prior", 0) + w_
                    it.setdefault("score_parts", []).append([f"insight #{x.get('id')}: {x['direction']} in earlier sessions", round(w_, 2)])
        if it["kind"] in ("config", "revert"):
            it["hypothesis"] = hypothesis(it, ctx)
            _retest_after_shift(it, ctx)
            rd_, rn_ = rescore(it, ctx.get("hypotheses"))
            cd_, cn_ = credibility(it, ctx.get("hypothesis_experience"))
            if cn_:
                rd_, rn_ = rd_ + cd_, rn_ + [cn_]
                it["hypothesis"]["track_record"] = cn_
            if rn_:
                it["rescore"] = {"delta": round(rd_, 2), "why": rn_}
        if it.get("sweep") and (it.get("relevance") or 0) < 2:
            # Battery 1: 33 sweep runs, no real gain; without a corroborated mechanism start with two points only
            it["sweep"]["values"] = it["sweep"]["values"][:2]
            it["score_note"] = "sweep without a matching bottleneck: 2 points, lower priority"
        if it["id"] in (ctx.get("failed") or []) and not it.get("retest"):
            it["confidence"] = 0
            it["note"] = (it.get("note", "") + " Failed earlier in this session (apply/restart failure or run too slow).").strip()
        ob = None if it["id"] in ("purge-concurrency", "purge-threadpool") else oversubscription_block(it, ctx)  # a stability remedy, judged by the history
        if ob and it["kind"] == "config":
            it["confidence"] = 0
            it["note"] = (it.get("note", "") + " " + ob).strip()
        # experience prior: how did this change do in earlier sessions with a similar workload?
        e = exp.get(it["id"])
        if e and e.get("n"):
            it["experience"] = e
        if it["safety"] == "trade-off" and it["kind"] == "config" and not benchmark:
            it["kind"] = "advice"
            it["note"] = (it.get("note", "") + " Trade-off: not applied under policy=safe.").strip()
        sc = score(it, ctx.get("goal", "tps")) + (2 if it["kind"] == "revert" else 0) - (2 if it.get("score_note") else 0) + it.get("score_prior", 0) \
            - (10 if it.get("no_mechanism") else 0) + (it.get("rescore") or {}).get("delta", 0)
        if it.get("relevance") is not None:
            sc += 2 * it["relevance"] if it["relevance"] else (-3 if has_major else 0)
        # every term of the ranking, for the person asking why this experiment and not another one (user, 2026-10-25)
        parts = score_parts(it, ctx.get("goal", "tps")) + (it.get("score_parts") or [])
        if it["kind"] == "revert":
            parts.append(["returns to a measured configuration", 2])
        if it.get("score_note"):
            parts.append([it["score_note"], -2])
        if it.get("no_mechanism"):
            parts.append(["nothing in this workload for it to act on", -10])
        if (it.get("rescore") or {}).get("delta"):
            parts.append(["hypotheses earlier in this session: " + "; ".join(it["rescore"]["why"]), it["rescore"]["delta"]])
        if it.get("relevance"):
            parts.append(["acts on the measured bottleneck" + (f" ({', '.join(it.get('addresses') or [])})" if it.get("addresses") else ""),
                          2 * it["relevance"]])
        elif it.get("relevance") == 0 and has_major:
            parts.append(["not aimed at the current bottleneck", -3])
        sc_before_exp = sc
        if it.get("experience"):
            e = it["experience"]
            w = e.get("weight", 0.5 if e.get("scope") else 1.0)  # less comparable evidence counts less (version, variant, conditions)
            ed_ = round(w * 3 * (e.get("wins", 0) - e.get("losses", 0)) / max(e["n"], 1) * min(e["n"], 3) / 3, 2)
            sc += ed_
            if ed_:
                parts.append([f"earlier sessions: {e.get('wins', 0)} better, {e.get('losses', 0)} worse in {e['n']} runs", ed_])
            # how large the measured effect was, not only its sign: a lever averaging -30% in comparable sessions (the
            # thread pool on 64-connection co-located cells: -21% to -73% in 8 sessions, all counted "neutral" while
            # not-steady neutralised regressions) must rank behind untried ideas
            md, nz = e.get("mean_delta_pct"), float(ctx.get("noise_pct") or 2.0)
            if md is not None and e.get("n", 0) >= 2 and md < -2 * nz and not e.get("wins"):
                sc -= round(w * min(abs(md), 30.0) / 5.0, 2)
                parts.append([f"earlier sessions averaged {md:+.1f}%", -round(w * min(abs(md), 30.0) / 5.0, 2)])
            # strong penalties only from comparable evidence (same workload, CPU class and concurrency): contention-related
            # changes (thread pool, spin settings, concurrency limits) that lose on small low-concurrency boxes can win
            # on many cores with 1000+ connections
            if e.get("losses", 0) >= 2 and not e.get("wins") and not e.get("scope"):
                it["confidence"] = min(it["confidence"], 1)
            if e.get("n", 0) >= 5 and e.get("losses", 0) >= 0.7 * e["n"] and not e.get("wins") and not e.get("scope"):
                sc -= 4  # consistently harmful in comparable sessions (e.g. the thread pool at low concurrency): try it last
                parts.append(["worse in most comparable sessions", -4])
            elif e.get("n", 0) >= 3 and not e.get("wins") and not e.get("scope") and not it.get("retest"):
                # known neutral here: 3+ comparable runs (any account), none a win - 34 of 39 such repeats were neutral or
                # worse again. Ranked behind untried ideas; a shifted bottleneck (retest) measures it again
                sc -= 3
                it["known_neutral"] = {"n": e["n"], "losses": e.get("losses", 0)}
                parts.append([f"known neutral here: no win in {e['n']} comparable runs", -3])
        # priority (best-practice bundle, reverts) only with evidence: a bundle nothing measured points at goes last
        # priority only for returning to measured configurations; everything else - the best-practice bundle included -
        # ranks by the evidence for what it acts on
        it["score"] = 100 if it.get("priority") and not it.get("no_mechanism") and not it.get("practice") else sc
        it["score_parts"] = [[w_, round(v_, 2)] for w_, v_ in parts if v_]
        if round(sum(v_ for _, v_ in it["score_parts"]), 2) != round(sc, 2):
            it["score_parts"].append(["other", round(sc - sum(v_ for _, v_ in it["score_parts"]), 2)])
        if it["score"] != sc:
            it["score_override"] = "returns to a measured configuration: before new experiments"
        if it["id"] == BUNDLE + "-bisect":
            # a regressed hygiene bundle is split next: otherwise its harmless members stay reverted unexplained (342)
            it["score"] = max(it["score"], 99)
            it["score_override"] = "a regressed best-practice bundle is split next"
        last_resort = it["id"] in STABILITY_LAST_RESORT
        if STABILITY_FIRST and unstable_now and it["id"] in STABILITY_IDS and it["kind"] == "config" and it.get("confidence", 0) > 0 \
                and (not last_resort and stab_tried < STABILITY_TRIES or last_resort and not others_left and it["id"] not in tried_ids):
            # stability first: while the history grows without bound, its remedies run before tuning experiments. On busy
            # CPUs the purge lag bound first (more purge threads compete with the transactions), else more purge threads
            # on busy CPUs: a higher priority for purge first (costs the user threads least), then the lag bound (slows every
            # writer), then more purge threads (compete for the same CPUs); with CPU to spare more purge threads first
            # the thread pool before the cruder innodb_thread_concurrency cap (user); the purge lag bound always last
            order = (("purge-priority", "purge-threadpool", "purge-concurrency", "sweep-purge-threads", "purge-batch", "purge-lag")
                     if busy_now >= 85 else
                     ("sweep-purge-threads", "purge-threadpool", "purge-priority", "purge-batch", "purge-concurrency", "purge-lag"))
            if "prio=" in str((ctx.get("variables") or {}).get("rg:purge") or "") and _int(ctx.get("variables") or {}, "innodb_purge_threads", 4) < 4:
                # purge already runs at a higher priority but with few threads: more of them next, then the pool
                order = ("sweep-purge-threads",) + tuple(x for x in order if x != "sweep-purge-threads")
            it["score"] = 100 + (len(order) - order.index(it["id"]))
            it["stability"] = True
            it["score_override"] = "stability first: purge remedies run before tuning, in a fixed order"
            it["score_note"] = "stability first: the undo history grows without bound in the current configuration"
        it["basis"] = basis(it, round(sc - sc_before_exp, 2))
    # durability trade-offs in a synchronous cluster: a committed transaction is on other nodes too, so one node crashing
    # loses nothing - only all of them crashing together does. Still a trade-off (the policy decides), but the meaning differs
    cl_ = ctx.get("cluster") or {}
    if cl_.get("type") in ("galera", "group-replication"):
        for it in items:
            if any(c["variable"] in ("innodb_flush_log_at_trx_commit", "sync_binlog", "innodb_doublewrite") for c in it.get("changes") or []):
                it["rationale"] = (it.get("rationale") or "") + (
                    f" In this {cl_['type']} cluster{' of ' + str(cl_['size']) + ' nodes' if cl_.get('size') else ''} a committed transaction "
                    "is also on other nodes: one node crashing loses nothing; all nodes crashing together (power loss) can.")
    # NUMA: a latch handed between threads on different nodes moves its cache line across the interconnect - measured
    # spinning costs more there, so the changes that act on it rank higher (with the measured share, not by default)
    nodes_ = int((ctx.get("os_facts") or {}).get("os:numa_nodes") or 1)
    spin_share = sum(p.get("share_pct") or 0 for p in ctx.get("breakdown") or [] if "cpu-spin" in (p.get("tags") or ()))
    if nodes_ > 1 and spin_share > float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0):
        for it in items:
            if it["kind"] == "config" and it["id"] in MECHANISMS and "cpu-spin" in MECHANISMS[it["id"]][0] and it["score"] < 100:
                it["score"] += 1
                it.setdefault("score_parts", []).append([f"{nodes_} NUMA nodes: latch contention costs more", 1])
                it["score_note"] = (f"{nodes_} NUMA nodes and {spin_share:.0f}% of the time spinning on latches: cross-node cache-line "
                                    "transfers make contention costlier")
    # no swap at all: the swap state is a precondition for sizing memory (pressure means OOM without it, slower pages
    # with it) - memory-setting experiments wait until the swap experiment ran (then they are measured with it)
    sw = next((i for i in items if i["id"] == "best-practices-swap" and i.get("confidence", 0) > 0), None)
    if sw and not (ctx.get("summary") or {}).get("swap_total_mb"):
        from .memory import MEMORY_VARS
        for it in items:
            if it is not sw and it["kind"] == "config" and any(c["variable"] in MEMORY_VARS for c in it["changes"]) \
                    and it["score"] >= sw["score"] and it["score"] < 100:
                it["score"] = sw["score"] - 0.5
                it["score_override"] = "memory settings wait for the swap experiment"
                it["score_note"] = "after the swap experiment: memory settings are sized with swap in place"
    items.sort(key=lambda i: (i["safety"] == "trade-off" and i["kind"] != "revert", -i["score"]))
    for n, it in enumerate(items, 1):
        it["rank"] = n
    # benchmark prerequisites ([requires]: max_connections, back_log ...) are set by the profile, never tuned (r1 HC-1 / HC-2
    # spent runs on max-connections)
    required = set(((inp.get("bench") or {}).get("requires") or {}).keys())
    # AI-suggested ideas: three runs per session unless one of them wins (user sessions: 0 of 14 improved the goal)
    hist = ctx.get("history") or []
    llm_runs = [h for h in hist if str(h.get("label") or "").startswith("llm-")]
    llm_won = any(h.get("verdict") in ("improved", "efficiency-gain") for h in llm_runs)
    for it in items:
        if required & {c["variable"] for c in it.get("changes") or []} and it["kind"] == "config":
            it["confidence"] = 0
            it["note"] = (it.get("note", "") + " Not run: a benchmark prerequisite ([requires]), not a tuning setting.").strip()
        if it["id"].startswith("llm-") and len(llm_runs) >= 3 and not llm_won and it["kind"] == "config" and not ctx.get("explore"):
            it["confidence"] = 0
            it["note"] = (it.get("note", "") + f" Not run: {len(llm_runs)} AI-suggested experiments in this session, none kept.").strip()
    # admission control on a commit-bound load shrinks the commit groups (lab / r1: 9 picks with commits >= 50% of the time,
    # 0 wins, -15% to -75%)
    commit_sh = sum(p.get("share_pct") or 0 for p in ctx["breakdown"] or [] if "commit" in (p.get("tags") or ()))
    if commit_sh >= 40 and ((ctx.get("summary") or {}).get("cpu_busy") or 0) < 90:
        for it in items:
            if it["id"] in QUEUE_LEVERS and it["kind"] == "config":
                it["confidence"] = 0
                it["note"] = (it.get("note", "") + f" Not run: commits take {commit_sh:.0f}% of the time - limiting admission "
                              "shrinks the commit groups.").strip()
    actionable = [i for i in items if i["kind"] in ("config", "revert") and i["confidence"] > 0]
    safe_next = next((i for i in actionable if i["safety"] == "safe"), None)
    trade_next = next((i for i in actionable if i["safety"] == "trade-off"), None)
    nxt = safe_next or (trade_next if benchmark else None)
    # benchmark policy: a trade-off backed by evidence goes before a safe idea that has none, or that scores far lower
    # (trx-commit-2 / ps-off / doublewrite-off had the top scores in five sessions and ran last or never)
    if benchmark and safe_next and trade_next and safe_next["kind"] == "config" and (
            trade_next["score"] > safe_next["score"] + 4
            or ((safe_next.get("no_mechanism") or (safe_next.get("relevance") or 0) <= 1) and (trade_next.get("relevance") or 0) >= 2)):
        nxt = trade_next
    # commit-bound under the benchmark policy: the commit-path trade-off first (backtest: canaries ran best-practices and
    # ahi-on before trx-commit-2, which then gained +19% to +29.5%). Still a trade-off: never part of the production-safe
    # configuration, and never run under policy=safe
    commit_now = sum(p.get("share_pct") or 0 for p in ctx.get("breakdown") or [] if "commit" in (p.get("tags") or ()))
    tc2 = next((i for i in actionable if i["id"] == "trx-commit-2"), None)
    if benchmark and tc2 and commit_now >= 20 and not (nxt and nxt.get("kind") == "revert") and not (nxt and nxt.get("stability")):
        nxt = tc2
    # value of information: after a long run of experiments that kept nothing, an idea that acts on little or nothing of
    # the measured time will not be the one that changes that - stop and say what limits the workload (user 194: 21 runs
    # after run 10 on a storage-bound host, all neutral)
    streak = 0
    for h in reversed(hist):
        lab = str(h.get("label") or "")
        if lab.startswith(("baseline", "calibration", "server defaults")) or "(confirm" in lab or "re-check" in lab or h.get("verdict") == "repeat":
            continue
        if h.get("verdict") == "improved":  # efficiency gains within noise do not end a fruitless streak
            break
        streak += 1
    stop = None
    noise = float(ctx.get("noise_pct") or 2.0)
    # the most the next idea could gain: half of the share of the time it acts on (relevance levels: <5 / 5-15 / 15-30 /
    # 30-60 / >60%); when even that is below the noise it cannot show (user 194 / 174, lab: ~40 points that could not)
    ceiling = {0: 2.5, 1: 15, 2: 30, 3: 60, 4: 100}.get(nxt.get("relevance") or 0, 0) * 0.5 if nxt else 0
    why = ""
    if nxt and nxt["kind"] == "config" and streak >= STOP_AFTER_FRUITLESS and (nxt.get("relevance") or 0) <= 1:
        why = f"{streak} experiments in a row kept nothing, and the best remaining idea ({nxt['id']}) acts on little of the measured time."
    elif nxt and nxt["kind"] == "config" and nxt["safety"] == "safe" and nxt.get("relevance") is not None and streak >= 3 and ceiling < noise:
        why = (f"{streak} experiments in a row kept nothing, and the best remaining idea ({nxt['id']}) acts on at most ~{2 * ceiling:.0f}% "
               f"of the time - even halving that is below the ±{noise:.1f}% noise.")
    # safe policy, storage-bound commits: grouping is already complete and each flush is slow - no server setting the
    # policy allows can move it, the storage can (user 194: 27 runs, ~13.5 h, after the answer was known at run 4)
    gc = group_commit(ctx.get("summary") or {}, inp, ctx["breakdown"], (ctx.get("variables") or {}).get("log_bin"))
    w_ms = max((ctx.get("summary") or {}).get("disk_w_await_ms") or 0, (ctx.get("summary") or {}).get("disk_flush_await_ms") or 0)
    tried_bundle = any(str(h.get("label") or "").startswith("best-practices") for h in hist)
    if not why and not benchmark and commit_sh >= 50 and gc and gc.get("complete") and w_ms >= 5 and tried_bundle and streak >= 1:
        why = (f"Storage-bound commits: {commit_sh:.0f}% of the time waits for commit flushes of {w_ms:.1f} ms each, and group commit "
               "already batches every waiting commit - no production-safe server setting changes that. See the storage advice.")
    if why and nxt and nxt["kind"] == "config" and not ctx.get("explore"):  # explore: the run budget decides, not the ceiling
        stop = why + " What limits the workload: " + "; ".join(d.get("text", "")[:160] for d in ctx["diagnosis"][:2] if d.get("type") != "none")
        nxt = None
    return {"stop_reason": stop,"workload": ctx["workload"], "diagnosis": ctx["diagnosis"], "breakdown": ctx["breakdown"], "items": items, "next": nxt["id"] if nxt else None,
            "blocked_by_policy": ctx.get("_blocked_items") or [],
            "phase": "safe" if safe_next else ("trade-offs" if nxt else "done"),
            # who chose the next step and which learned data was loaded when it was chosen (the planner overwrites "by")
            "decision": {"by": "rules", "learning": learning_state(ctx)}}


def learning_state(ctx):
    """What learned data this decision had: comparable earlier runs (all accounts), hypothesis outcomes, learned parameter
    impacts and insights from learning.db. Zero everywhere means built-in rules only."""
    exp = ctx.get("experience") or {}
    return {"experience_ideas": len(exp), "experience_runs": int(sum((e or {}).get("n") or 0 for e in exp.values())),
            "hypothesis_outcomes": len(ctx.get("hypothesis_experience") or []),
            "learned_parameters": sum(1 for v in (ctx.get("param_impact") or {}).values() if (v or {}).get("impact")),
            "insights": len(ctx.get("insights") or [])}
