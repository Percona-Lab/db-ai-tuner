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
from .knowledge import KB, fmt_size, human_size, normalize, parse_size

GiB = 1024 ** 3
MiB = 1024 ** 2


def _v(vars_, name, default=None):
    return vars_.get(name, default)


def _int(vars_, name, default=0):
    try:
        return int(float(vars_.get(name, default)))
    except (TypeError, ValueError):
        return default


def _on(vars_, name):
    return str(vars_.get(name, "")).upper() in ("ON", "1")


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
    if steal >= 5:
        add("steal", "medium", f"Hypervisor steal {steal:.1f}%: noisy neighbour; results will be noisy.", {"cpu_steal": steal})
    if iow >= 15 or (s.get("disk_util", 0) >= 90 and s.get("disk_await_ms", 0) >= 2):
        add("io", "high", f"I/O bound (iowait {iow:.0f}%, disk util {s.get('disk_util', 0):.0f}%, "
            f"await {s.get('disk_await_ms', 0):.1f} ms).",
            {k: s.get(k) for k in ("cpu_iowait", "disk_util", "disk_await_ms", "disk_r_iops", "disk_w_iops")})
    if s.get("bp_hit_pct") is not None and s["bp_hit_pct"] < 99 and s.get("bp_disk_reads", 0) > 50:
        add("memory", "high" if s["bp_hit_pct"] < 95 else "medium",
            f"Working set does not fit: buffer pool hit ratio {s['bp_hit_pct']:.2f}%, {s['bp_disk_reads']:.0f} page reads/s from disk.",
            {"bp_hit_pct": s["bp_hit_pct"], "bp_disk_reads": s.get("bp_disk_reads"), "data_bytes": inp.get("data_bytes")})
    if (s.get("checkpoint_age_pct_max") or 0) >= 70:
        add("redo", "high", f"Redo log nearly full (checkpoint age peaked at {s['checkpoint_age_pct_max']:.0f}% of capacity): "
            "forced synchronous flushing stalls writes.", {"checkpoint_age_pct_max": s["checkpoint_age_pct_max"], "redo_mbs": s.get("redo_mbs")})
    tps = s.get("tps") or 0
    fsyncs = s.get("data_fsyncs") or 0
    if tps > 0 and fsyncs / tps > 0.5 and s.get("disk_await_ms", 0) > 0.5:
        add("commit", "medium", f"Commit-bound: {fsyncs:.0f} fsyncs/s for {tps:.0f} tps, disk await {s.get('disk_await_ms', 0):.2f} ms.",
            {"data_fsyncs": fsyncs, "tps": tps, "disk_await_ms": s.get("disk_await_ms")})
    lw = s.get("row_lock_waits") or 0
    if lw > 0 and tps > 0 and lw / tps > 0.02:
        add("locks", "medium" if lw / tps < 0.1 else "high",
            f"Row lock contention: {lw:.1f} waits/s (avg {s.get('row_lock_wait_avg_ms', 0):.1f} ms) for {tps:.0f} tps. "
            "Usually a workload/schema property; config helps little.", {"row_lock_waits": lw, "row_lock_wait_avg_ms": s.get("row_lock_wait_avg_ms")})
    if (s.get("swap_in", 0) or 0) + (s.get("swap_out", 0) or 0) > 10:
        add("swap", "high", "The host is swapping: memory is over-committed.", {"swap_in": s.get("swap_in"), "swap_out": s.get("swap_out")})
    tr = s.get("threads_running_avg") or 0
    if tr > cpus * 4 and busy > 60:
        add("concurrency", "medium", f"{tr:.0f} threads running on {cpus} CPUs: heavy oversubscription, mutex convoys likely.",
            {"threads_running_avg": tr, "cpus": cpus})
    if not out:
        add("none", "low", "No dominant bottleneck: the server is not saturated by this workload (client-bound or too little load?).",
            {"cpu_busy": busy, "cpu_iowait": iow})
    return out


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
            if var not in self.v:
                continue
            if normalize(var, self.v.get(var)) == normalize(var, val):
                continue
            meta = KB.get(var, {})
            r = meta.get("risk", "low")
            if order.index(r) > order.index(risk):
                risk = r
            if not meta.get("dynamic", False):
                restart = True
            ch.append({"variable": var, "current": self.v.get(var), "proposed": val})
        if kind == "config" and not ch:
            return
        self.items.append({
            "id": id, "title": title, "kind": kind, "changes": ch, "rationale": rationale,
            "evidence": {k: v for k, v in evidence.items() if v is not None},
            "impact": impact, "confidence": confidence, "risk": risk, "restart": restart,
            "goals": list(goals),
        })


def recommend(ctx):
    v = ctx["variables"]
    s = ctx.get("summary") or {}
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

    bp = _int(v, "innodb_buffer_pool_size")
    data_bytes = inp.get("data_bytes") or 0
    max_conn = _int(v, "max_connections", 151)
    mem_avail = (s.get("mem_available_mb") or 0) * MiB
    swapping = (s.get("swap_in", 0) or 0) + (s.get("swap_out", 0) or 0) > 10

    # ---------------- memory -----------------------------------------------------
    if swapping and bp > 512 * MiB:
        new = int(bp * 0.85) // (128 * MiB) * (128 * MiB)
        b.add("bp-shrink", "Shrink the buffer pool: host is swapping", [("innodb_buffer_pool_size", fmt_size(new))],
              "Swap activity makes every page access unpredictable; giving memory back to the OS is the first fix.",
              {"swap_in": s.get("swap_in"), "swap_out": s.get("swap_out"), "innodb_buffer_pool_size": human_size(bp)}, impact=3, confidence=3)
    elif have_run and s.get("bp_hit_pct", 100) < 99.5 and (s.get("bp_disk_reads") or 0) > 20:
        headroom = mem_avail - max(512 * MiB, int(ram * 0.05))
        want = min(data_bytes * 1.1 if data_bytes else bp * 2, bp + headroom, ram * 0.85)
        new = int(want) // (128 * MiB) * (128 * MiB)
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

    # ---------------- concurrency (experiments) ----------------------------------
    busy = s.get("cpu_busy") or 0
    tr = s.get("threads_running_avg") or 0
    selects = rates.get("Com_select", 0)
    writes = sum(rates.get(k, 0) for k in ("Com_insert", "Com_update", "Com_delete", "Com_replace"))
    if have_run and "innodb_adaptive_hash_index" in v:
        if not _on(v, "innodb_adaptive_hash_index") and selects > writes and (s.get("bp_hit_pct") or 0) > 98:
            b.add("ahi-on", "Try the adaptive hash index", [("innodb_adaptive_hash_index", "ON")],
                  "Read-mostly workload with a hot in-memory working set: AHI can shortcut B-tree descents for point lookups. "
                  "Experiment: keep only if the goal metric improves.",
                  {"selects_per_s": selects, "writes_per_s": round(writes, 1), "bp_hit_pct": s.get("bp_hit_pct")}, impact=2, confidence=1)
        ahi_wait = sum(w["time_s"] for w in inp.get("top_waits", []) if "btr_search" in w["event"] or "ahi" in w["event"].lower())
        sem = (inp.get("innodb_status") or {}).get("semaphores", "")
        if _on(v, "innodb_adaptive_hash_index") and (ahi_wait > 0 or "btr0sea" in sem):
            b.add("ahi-off", "Disable the adaptive hash index (contention)", [("innodb_adaptive_hash_index", "OFF")],
                  "Threads are waiting on AHI latches.", {"ahi_wait_s": ahi_wait}, impact=2, confidence=2)
    if have_run and "innodb_log_writer_threads" in v and _on(v, "innodb_log_writer_threads") and cpus <= 8 and busy >= 70:
        b.add("log-writer-threads-off", "Disable dedicated redo log writer threads", [("innodb_log_writer_threads", "OFF")],
              f"On {cpus} CPUs that are {busy:.0f}% busy the dedicated log writer/flusher threads compete with user threads; "
              "MySQL docs suggest disabling them on small systems. Experiment.",
              {"cpus": cpus, "cpu_busy": busy}, impact=2, confidence=1)
    if have_run and busy >= 80 and "innodb_sync_spin_loops" in v and _int(v, "innodb_sync_spin_loops", 30) > 10 and tr > cpus:
        b.add("spin-loops", "Spin less before sleeping on mutexes", [("innodb_sync_spin_loops", "10")],
              f"CPU saturated with {tr:.0f} running threads on {cpus} CPUs: shorter spinning gives cycles back to useful work. Experiment.",
              {"cpu_busy": busy, "threads_running_avg": tr, "innodb_sync_spin_loops": v.get("innodb_sync_spin_loops")}, impact=2, confidence=1)
    if have_run and "thread_handling" in v and v.get("thread_handling") == "one-thread-per-connection" and tr > cpus * 4 and busy >= 70:
        b.add("thread-pool", "Try the thread pool", [("thread_handling", "pool-of-threads")],
              f"{tr:.0f} concurrently running threads on {cpus} CPUs; a thread pool caps active threads and reduces context switching. "
              "Can lower throughput for commit-heavy loads (smaller group commits). Experiment.",
              {"threads_running_avg": tr, "cpus": cpus, "ctx_switches": (inp.get("os_rates") or {}).get("ctx_switches")}, impact=2, confidence=1)

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
    commit_bound = any(d["type"] == "commit" for d in ctx.get("diagnosis", [])) or (
        (s.get("tps") or 0) > 0 and (s.get("data_fsyncs") or 0) / max(s.get("tps") or 1, 1) > 0.3)
    if goal in ("tps", "latency") and have_run and commit_bound:
        dur_kind = "config" if policy == "benchmark" else "advice"
        if _int(v, "innodb_flush_log_at_trx_commit", 1) == 1:
            b.add("trx-commit-2", "Relax redo flushing at commit (DURABILITY TRADE-OFF)", [("innodb_flush_log_at_trx_commit", "2")],
                  "Commits wait for an fsync of the redo log. Value 2 writes at commit and fsyncs once per second: "
                  "a power loss/OS crash can lose ~1 s of committed transactions. Only acceptable where that loss is acceptable.",
                  {"data_fsyncs": s.get("data_fsyncs"), "tps": s.get("tps"), "disk_await_ms": s.get("disk_await_ms")}, impact=3, confidence=3, kind=dur_kind)
        if v.get("log_bin") == "ON" and _int(v, "sync_binlog", 1) == 1:
            b.add("sync-binlog", "Relax binlog fsync (DURABILITY TRADE-OFF)", [("sync_binlog", "1000")],
                  "Binary log fsync at every commit group. Relaxing can lose binlog events on OS crash (replicas/PITR diverge).",
                  {"data_fsyncs": s.get("data_fsyncs"), "tps": s.get("tps")}, impact=2, confidence=2, kind=dur_kind)
    if policy == "benchmark" and have_run and goal == "tps" and busy >= 80 and _on(v, "performance_schema"):
        b.add("ps-off", "Disable Performance Schema (OBSERVABILITY TRADE-OFF)", [("performance_schema", "OFF")],
              "On a CPU-bound server instrumentation costs a few percent of CPU. You lose statement latency measurement "
              "(the tuner will rely on throughput only) and most diagnostics.", {"cpu_busy": busy}, impact=2, confidence=2)
    if policy == "benchmark" and have_run and "binlog_row_image" in v and v.get("binlog_row_image") == "FULL" and v.get("log_bin") == "ON" and redo_mbs > 1:
        b.add("binlog-minimal", "Log minimal row images", [("binlog_row_image", "MINIMAL")],
              "Smaller binlog writes for update-heavy workloads; downstream consumers needing full images (CDC) break.",
              {"redo_mbs": redo_mbs}, impact=1, confidence=1)

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


def score(item, goal):
    s = item["impact"] * 2 + item["confidence"]
    if goal not in item.get("goals", [goal]):
        s -= 3
    if item["kind"] == "advice":
        s -= 10
    return s


def apply_history(items, history):
    """Mark/skip items already tried; add revert suggestions for regressions."""
    tried = {}
    for run in history:
        for c in run.get("changes") or []:
            tried[(c["variable"], normalize(c["variable"], c.get("new")))] = run
    out = []
    for it in items:
        prior = [tried.get((c["variable"], normalize(c["variable"], c["proposed"]))) for c in it["changes"]]
        prior = [p for p in prior if p]
        if prior:
            p = prior[-1]
            it["tried_in_run"] = p.get("seq")
            it["tried_verdict"] = p.get("verdict")
            if p.get("verdict") in ("regressed", "neutral"):
                it["confidence"] = 0
                it["note"] = f"Already tried in run #{p.get('seq')} ({p.get('verdict')})."
        out.append(it)
    return out


def revert_suggestions(history, variables):
    """If the newest measured run regressed, suggest reverting its changes."""
    if len(history) < 2:
        return []
    last = history[-1]
    if last.get("verdict") != "regressed" or not last.get("changes"):
        return []
    ch = []
    for c in last["changes"]:
        if c.get("old") is not None and c["variable"] in variables:
            ch.append({"variable": c["variable"], "current": variables.get(c["variable"]), "proposed": c["old"]})
    if not ch:
        return []
    return [{
        "id": f"revert-run-{last.get('seq')}", "title": f"Revert the changes of run #{last.get('seq')} (regressed)", "kind": "revert",
        "changes": ch, "rationale": f"Run #{last.get('seq')} was {last.get('delta_pct', 0):+.1f}% versus the previous run on the goal metric.",
        "evidence": {"goal_value": last.get("goal_value"), "delta_pct": last.get("delta_pct")}, "impact": 3, "confidence": 3,
        "risk": "none", "restart": any(not KB.get(c["variable"], {}).get("dynamic", False) for c in ch), "goals": ["tps", "latency"],
    }]


def run_engine(ctx):
    ctx = dict(ctx)
    ctx["diagnosis"] = diagnose(ctx) if ctx.get("summary") else []
    items = recommend(ctx)
    items = apply_history(items, ctx.get("history") or [])
    items = revert_suggestions(ctx.get("history") or [], ctx["variables"]) + items
    allowed_durability = ctx.get("policy") == "benchmark"
    for it in items:
        if it["risk"] == "durability" and not allowed_durability and it["kind"] == "config":
            it["kind"] = "advice"
        it["score"] = score(it, ctx.get("goal", "tps")) if it["kind"] != "revert" else 100
    items.sort(key=lambda i: -i["score"])
    for n, it in enumerate(items, 1):
        it["rank"] = n
    nxt = next((i for i in items if i["kind"] in ("config", "revert") and i["confidence"] > 0), None)
    return {"diagnosis": ctx["diagnosis"], "items": items, "next": nxt["id"] if nxt else None}
