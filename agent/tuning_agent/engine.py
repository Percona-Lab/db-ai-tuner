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
import re

from .mechanisms import MECHANISMS, relevance
from .knowledge import KB, flags_subset, fmt_size, human_size, normalize, parse_size, tradeoff_of

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
    if lat >= 50 or rows_per_stmt >= 5000:
        kind = "olap"
    elif writes + reads == 0 and calls > 0:
        kind = "oltp-write"  # stored-procedure OLTP (e.g. HammerDB): statements run inside procedures
    elif writes / max(writes + reads, 1e-9) >= 0.15:
        kind = "oltp-write"
    elif lat >= 10 or rows_per_stmt >= 500:
        kind = "mixed"
    else:
        kind = "oltp-read"
    return {"type": kind, "evidence": ev}


def harmful_swap(s):
    """Swap that costs performance: pages read back in at a meaningful rate, memory stalls, or OOM kills.
    Swap-out alone (cold pages parked in swap) is not harmful: it frees RAM for caches."""
    return ((s.get("swap_in") or 0) > 64 or (s.get("psi_memory_full") or 0) >= 2 or (s.get("psi_memory_some") or 0) >= 10
            or (s.get("oom_kills") or 0) > 0)


def bp_round(v, n, up=False):
    """MySQL sizes the buffer pool in multiples of chunk_size x instances (and silently rounds up otherwise)."""
    chunk = _int(v, "innodb_buffer_pool_chunk_size", 0) or 128 * MiB
    unit = chunk * max(1, _int(v, "innodb_buffer_pool_instances", 1))
    k = int(n) // unit + (1 if up and int(n) % unit else 0)
    return max(unit, k * unit)


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
    if wait_pct is not None and (wait_pct >= 10 or (delay or 0) >= 500):
        add("sched-delay", "high" if wait_pct >= 25 else "medium",
            f"Scheduling delay: database threads were runnable but waiting for a CPU {wait_pct:.0f}% of their runnable time "
            f"(avg {delay or 0:.0f} µs each time they got a CPU, worst interval {s.get('dbproc_sched_delay_us_max') or 0:.0f} µs). "
            "This adds directly to query latency, most visibly at the tail. Fewer runnable threads (thread pool, concurrency "
            "limits) or keeping other work off the database CPUs helps more than faster I/O.",
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
    if (s.get("checkpoint_age_pct_max") or 0) >= 70:
        add("redo", "high", f"Redo log nearly full (checkpoint age peaked at {s['checkpoint_age_pct_max']:.0f}% of capacity): "
            "forced synchronous flushing stalls writes.", {"checkpoint_age_pct_max": s["checkpoint_age_pct_max"], "redo_mbs": s.get("redo_mbs")})
    tps = s.get("tps") or 0
    fsyncs = s.get("data_fsyncs") or 0
    if tps > 0 and fsyncs / tps > 0.5 and s.get("disk_await_ms", 0) > 0.5:
        add("commit", "medium", f"Commit-bound: {fsyncs:.0f} fsyncs/s for {tps:.0f} tps, disk await {s.get('disk_await_ms', 0):.2f} ms.",
            {"data_fsyncs": fsyncs, "tps": tps, "disk_await_ms": s.get("disk_await_ms")})
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
        if threads >= 0.5 and share >= 40 and top_cls in names:
            kind = {"redo_io": "commit", "binlog_io": "commit", "data_io": "io", "sync": "concurrency"}.get(top_cls, "wait-" + top_cls)
            if not any(d["type"] == kind for d in out):
                add(kind, "medium", f"Wait profile: {names[top_cls]} accounts for {share:.0f}% of non-row wait time "
                    f"({threads:.1f} threads waiting on average).", {"wait_classes": wc, "duration_s": dur})
    lw = s.get("row_lock_waits") or 0
    if lw > 0 and tps > 0 and lw / tps > 0.02:
        add("locks", "medium" if lw / tps < 0.1 else "high",
            f"Row lock contention: {lw:.1f} waits/s (avg {s.get('row_lock_wait_avg_ms', 0):.1f} ms) for {tps:.0f} tps. "
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
    if rtt and rtt >= 10:
        add("network-rtt", "medium" if rtt < 40 else "high", f"Clients are {rtt:.0f} ms away (TCP RTT): every round trip costs more than "
            "most statements; batching, pooling and window settings matter.", {"rtt_avg_ms": rtt, "rtt_p95_ms": s.get("rtt_p95_ms")})
    if (s.get("client_cpu_busy") or 0) >= 90:
        add("client-bound", "high", f"The benchmark/application client is saturated ({s['client_cpu_busy']:.0f}% CPU): measured throughput "
            "reflects the client, not the database. Use a bigger client host or fewer client-side cycles before tuning the server.",
            {"client_cpu_busy": s.get("client_cpu_busy")})
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

    # ---------------- memory -----------------------------------------------------
    if swapping and bp > 512 * MiB:
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
        ):
            if var in v:
                b.add(f"practice-{var}", why.split(" (")[0], [(var, val)], why, {var: v.get(var)}, impact=1, confidence=2)
    if flavor == "mariadb" and v.get("innodb_autoinc_lock_mode") == "1" and (v.get("log_bin") != "ON" or v.get("binlog_format") == "ROW"):
        b.add("practice-autoinc-lock", "Interleaved auto-increment locking", [("innodb_autoinc_lock_mode", "2")],
              "Mode 2 lets concurrent inserts allocate auto-increment values without a table-level lock; safe with row-based "
              "binary logging (MySQL 8.0+ default).", {"innodb_autoinc_lock_mode": v.get("innodb_autoinc_lock_mode"), "binlog_format": v.get("binlog_format")},
              impact=1, confidence=2)

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
    if have_run and "thread_handling" in v and v.get("thread_handling") == "one-thread-per-connection" and \
            (tr > cpus * 4 and busy >= 70 or tr > cpus and (s.get("dbproc_runq_wait_pct") or 0) >= 15):
        b.add("thread-pool", "Try the thread pool", [("thread_handling", "pool-of-threads")],
              f"{tr:.0f} concurrently running threads on {cpus} CPUs; a thread pool caps active threads and reduces context switching. "
              "Can lower throughput for commit-heavy loads (smaller group commits). Experiment.",
              {"threads_running_avg": tr, "cpus": cpus, "ctx_switches": (inp.get("os_rates") or {}).get("ctx_switches"),
               "dbproc_runq_wait_pct": s.get("dbproc_runq_wait_pct")}, impact=2, confidence=1)

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
    page_writes = rates.get("Innodb_pages_written", 0) or rates.get("Innodb_dblwr_pages_written", 0)
    if have_run and wtype in ("oltp-write", "mixed") and "innodb_doublewrite" in v and str(v.get("innodb_doublewrite")).upper() == "ON" \
            and rates.get("Innodb_dblwr_pages_written", 0) > 50:
        b.add("doublewrite-off", "Disable the doublewrite buffer (DURABILITY TRADE-OFF)", [("innodb_doublewrite", "OFF")],
              f"{rates.get('Innodb_dblwr_pages_written', 0):.0f} pages/s are written twice. Without doublewrite a crash during a page write "
              "can leave a torn page that InnoDB cannot repair.", {"Innodb_dblwr_pages_written_per_s": rates.get("Innodb_dblwr_pages_written")},
              impact=2, confidence=2, kind="config" if policy == "benchmark" else "advice")
    if have_run and v.get("log_bin") == "ON" and wtype in ("oltp-write", "mixed") and commit_bound:
        b.add("binlog-off", "Run without the binary log (REPLICATION / PITR TRADE-OFF)", [("disable_log_bin", "")],
              "Every commit is a two-phase commit between the binlog and InnoDB with its own fsync. Without the binlog there is no replication "
              "and no point-in-time recovery.", {"fsyncs_per_trx": s.get("fsyncs_per_trx"), "log_bin": v.get("log_bin")},
              impact=3, confidence=2, kind="config" if policy == "benchmark" else "advice")
    if policy == "benchmark" and have_run and goal == "tps" and busy >= 80 and _on(v, "performance_schema"):
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
        if (s.get("bp_disk_reads") or 0) > 100 and "innodb_read_ahead_threshold" in v and _int(v, "innodb_read_ahead_threshold", 56) > 32 and \
                rates.get("Handler_read_rnd_next", 0) > 10 * max(rates.get("Handler_read_key", 0), 1):
            b.add("read-ahead", "Earlier linear read-ahead for scans", [("innodb_read_ahead_threshold", "32")],
                  "Scan-heavy analytical reads from disk: triggering linear read-ahead earlier turns more random page reads into larger sequential ones. Experiment.",
                  {"bp_disk_reads": s.get("bp_disk_reads"), "Handler_read_rnd_next_per_s": rates.get("Handler_read_rnd_next")}, impact=1, confidence=1)
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
    if have_run and wtype in ("oltp-write", "mixed"):
        if v.get("log_bin") == "ON" and "binlog_group_commit_sync_delay" in v and _int(v, "binlog_group_commit_sync_delay") == 0 \
                and (s.get("fsyncs_per_trx") or 0) > 0.8 and (s.get("threads_running_avg") or 0) >= 4:
            b.add("group-commit-delay", "Grow binlog commit groups", [("binlog_group_commit_sync_delay", "200"),
                                                                       ("binlog_group_commit_sync_no_delay_count", "16")],
                  f"{s.get('fsyncs_per_trx'):.2f} fsyncs per transaction with {s.get('threads_running_avg'):.0f} running threads: waiting up to 200 µs "
                  "lets more transactions share one binlog fsync. Durability unchanged; may add latency at low concurrency. Experiment.",
                  {"fsyncs_per_trx": s.get("fsyncs_per_trx"), "threads_running_avg": s.get("threads_running_avg")}, impact=2, confidence=1)
        if "innodb_change_buffering" in v and v.get("innodb_change_buffering") == "none" and (s.get("bp_hit_pct") or 100) < 99 and writes > 50:
            b.add("change-buffer", "Buffer secondary index changes", [("innodb_change_buffering", "all")],
                  "Write-heavy workload whose secondary-index pages are often not in memory: the change buffer defers those reads. "
                  "MySQL 8.4 disables it by default (helps little on SSD, can slow crash recovery). Experiment.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "writes_per_s": round(writes, 1)}, impact=1, confidence=1)
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
        if (commit_bound or redo_mbs > 2) and v.get("os:vm.dirty_bytes") == "0" and v.get("os:vm.dirty_background_bytes") == "0" and ram >= 2 * GiB:
            b.add("os-dirty-writeback", "Write back dirty page cache earlier", [("os:vm.dirty_background_bytes", str(64 * MiB)),
                                                                                 ("os:vm.dirty_bytes", str(512 * MiB)),
                                                                                 ("os:vm.dirty_expire_centisecs", "500"),
                                                                                 ("os:vm.dirty_writeback_centisecs", "100")],
                  "Binary log and redo are written through the page cache and fsync'ed at commit. With the default ratios gigabytes can "
                  "accumulate, turning periodic writeback and fsyncs into stalls; small byte limits keep fsync work small and steady. Experiment.",
                  {"redo_mbs": redo_mbs, "fsyncs_per_trx": s.get("fsyncs_per_trx"), "disk_await_ms": s.get("disk_await_ms")}, impact=1, confidence=1)
        if v.get("os:malloc") == "glibc" and busy_ >= 70 and (s.get("threads_running_avg") or 0) >= cpus:
            b.add("os-jemalloc", "Run mysqld with jemalloc", [("os:malloc", "jemalloc")],
                  f"CPU-bound ({busy_:.0f}%) with {s.get('threads_running_avg', 0):.0f} running threads: jemalloc has less allocator lock "
                  "contention and fragmentation than glibc malloc (Percona recommendation). Needs a mysqld restart. Experiment.",
                  {"cpu_busy": busy_, "threads_running_avg": s.get("threads_running_avg")}, impact=2, confidence=1)
        if "large_pages" in v and not _on(v, "large_pages") and bp >= 8 * GiB and ram >= 16 * GiB and not swapping and mem_avail > GiB:
            pages = int((bp + 192 * MiB) // (2 * MiB))
            b.add("os-hugepages", "Back the buffer pool with huge pages", [("os:hugepages", str(pages)), ("large_pages", "ON")],
                  f"Reserves {pages} x 2 MiB pages for the {human_size(bp)} buffer pool: fewer TLB misses for memory-bound work "
                  "(MySQL docs / field reports: 5-30%). The memory is reserved even when unused; needs a restart. Experiment.",
                  {"innodb_buffer_pool_size": human_size(bp), "hugepages": pages, "mem_available": human_size(mem_avail)}, impact=2, confidence=1)
        if int(osf.get("os:numa_nodes") or 1) > 1 and v.get("os:kernel.numa_balancing") == "1":
            ch = [("os:kernel.numa_balancing", "0")]
            if "innodb_numa_interleave" in v and not _on(v, "innodb_numa_interleave"):
                ch.append(("innodb_numa_interleave", "ON"))
            b.add("os-numa", "Stop automatic NUMA page migration", ch,
                  f"{osf.get('os:numa_nodes')} NUMA nodes: the kernel keeps migrating buffer pool pages between nodes; database profiles "
                  "(tuned oracle/mssql) disable it and InnoDB interleaves the buffer pool instead.", {"numa_nodes": osf.get("os:numa_nodes")},
                  impact=2, confidence=2)
        my = s.get("mysqld_cpu_pct")
        if v.get("os:kernel.sched_autogroup_enabled") == "1" and my is not None and busy_ >= 85 and my < 0.8 * busy_:
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
        if gov and gov != "performance":
            b.add("os-governor", "CPU frequency governor 'performance'", [("os:cpu_governor", "performance")],
                  f"Governor {gov} lowers clock speed between bursts, adding latency.", {"cpu_governor": gov}, impact=1, confidence=2)
        if v.get("os:mount_barrier") == "ON" and commit_bound and wtype in ("oltp-write", "mixed"):
            b.add("os-nobarrier", "Disable ext4 write barriers (DURABILITY TRADE-OFF)", [("os:mount_barrier", "OFF")],
                  "Barriers force the device to persist its write cache at every fsync. Without them a power loss can lose or reorder "
                  "writes the database already acknowledged.", {"fsyncs_per_trx": s.get("fsyncs_per_trx"), "disk_await_ms": s.get("disk_await_ms")},
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
        if conn_rate > 5 and str(v.get("skip_name_resolve", "")).upper() == "OFF":
            b.add("skip-name-resolve", "Skip reverse DNS on connect", [("skip_name_resolve", "ON")],
                  f"{conn_rate:.0f} new connections/s each pay a reverse DNS lookup; with DNS hiccups connects stall for seconds.",
                  {"net_conn_per_s": conn_rate}, impact=2, confidence=2)
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
    def sweep(id_, title, var, values, rationale, evidence, impact=2):
        values = [x for x in dict.fromkeys(values) if x is not None and normalize(var, x) != normalize(var, v.get(var))]
        if var in v and len(values) >= 2:
            b.add(id_, title, [(var, values[0])], rationale + f" Values: {', '.join(map(str, values))}.", evidence, impact=impact, confidence=2)
            if b.items and b.items[-1]["id"] == id_:
                b.items[-1]["sweep"] = {"variable": var, "values": [str(x) for x in values]}
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
        if ctx.get("os_tuning") and swap_free < 1 * GiB and ram >= 2 * GiB:
            size = 1 * GiB if ram <= 8 * GiB else 2 * GiB
            extra_swap = [("os:swapfile", fmt_size(size))]
            room = min(int(ram * 0.15), cold + (swap_free + size) // 2)
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
    if have_run and diag_types & {"memory", "io"} and not swapping and mem_avail > 512 * MiB and bp:
        top = bp_round(v, min(ram * 0.80, bp + mem_avail - 512 * MiB))
        mid = bp_round(v, (bp + top) // 2)
        if top > bp * 1.15:
            sweep("sweep-bp", "Sweep the buffer pool size", "innodb_buffer_pool_size", [fmt_size(mid), fmt_size(top)],
                  "The working set misses memory and RAM is free: measure how throughput responds to more buffer pool.",
                  {"bp_hit_pct": s.get("bp_hit_pct"), "mem_available": human_size(mem_avail)}, impact=3)
    if have_run and "redo" in diag_types and cap:
        sweep("sweep-redo", "Sweep the redo log capacity", "innodb_redo_log_capacity", [fmt_size(cap * 2), fmt_size(cap * 4)],
              "Checkpoint pressure: measure 2x and 4x the redo capacity.", {"checkpoint_age_pct_max": ck})
    if have_run and diag_types & {"io", "redo"} and ssd:
        sweep("sweep-io-capacity", "Sweep the background flushing budget", "innodb_io_capacity", [str(ioc // 2), str(ioc * 2), str(ioc * 4)],
              "I/O or redo pressure: find the flushing budget the storage sustains.", {"innodb_io_capacity": ioc, "disk_util": s.get("disk_util")})
    if have_run and diag_types & {"concurrency", "cpu-pressure", "sched-delay"} and "innodb_thread_concurrency" in v:
        sweep("sweep-thread-concurrency", "Sweep InnoDB thread concurrency", "innodb_thread_concurrency", [str(cpus * 2), str(cpus * 4), "0"],
              f"{s.get('threads_running_avg', 0):.0f} running threads on {cpus} CPUs: measure admission limits.",
              {"threads_running_avg": s.get("threads_running_avg"), "cpus": cpus})

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


def score(item, goal):
    s = item["impact"] * 2 + item["confidence"]
    if goal not in item.get("goals", [goal]):
        s -= 3
    if item["kind"] == "advice":
        s -= 10
    return s


def apply_history(items, history):
    """Mark/skip items already tried."""
    tried = {}
    tried_ids = {}
    for run in history:
        for c in run.get("changes") or []:
            tried[(c["variable"], normalize(c["variable"], c.get("new")))] = run
            src = c.get("source") or ""
            if src.startswith("rec:"):
                tried_ids[src[4:]] = run
    out = []
    for it in items:
        prior = [tried.get((c["variable"], normalize(c["variable"], c["proposed"]))) for c in it["changes"]]
        prior = [p for p in prior if p] or ([tried_ids[it["id"]]] if it["id"] in tried_ids and it["kind"] == "config" else [])
        if prior:
            p = prior[-1]
            it["tried_in_run"] = p.get("seq")
            it["tried_verdict"] = p.get("verdict")
            if p.get("verdict") in ("regressed", "neutral", "efficiency-loss"):
                it["confidence"] = 0
                it["note"] = f"Already tried in run #{p.get('seq')} ({p.get('verdict')})."
        out.append(it)
    return out


def config_key(managed):
    return tuple(sorted((k, normalize(k, v)) for k, v in (managed or {}).items()))


def config_scores(history, goal="tps"):
    """Mean goal value per distinct configuration (repeated runs of one config are averaged)."""
    groups = {}
    for r in history:
        if r.get("goal_value") is None:
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
PRACTICE_IDS = {"flush-neighbors", "o-direct", "os-swappiness", "os-thp-off", "os-scheduler", "os-noatime", "os-numa", "os-aio", "os-governor"}
BUNDLE = "best-practices"


def is_practice_run(r):
    return str(r.get("label") or "").startswith(BUNDLE)


def _practice_vars(r):
    return sorted({c["variable"] for c in r.get("changes") or [] if str(c.get("source") or "").startswith("rec:" + BUNDLE)})


def accepted_practices(history):
    """Variables of practice runs that did not regress: they stay."""
    return {v for r in history if is_practice_run(r) and r.get("verdict") not in ("regressed", "efficiency-loss") for v in _practice_vars(r)}


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
              "restart": any(i.get("restart") for i in src), "goals": ["tps", "latency"], "priority": True, "practice": True}
    return [bundle] + rest


def best_config(history, goal="tps", noise=2.0):
    """Highest mean wins only when it beats the others beyond noise; within noise prefer configurations that were
    kept (improved / efficiency-gain / the reference) and measured more often."""
    scores = config_scores(history, goal)
    if not scores:
        return None
    top = scores[0]["mean"]
    tied = [c for c in scores if abs(c["mean"] - top) / abs(top) * 100 <= noise]
    acc = accepted_practices(history)
    for c in tied:
        runs = [r for r in history if config_key(r.get("managed")) == c["key"]]
        c["kept"] = any(r.get("verdict") in KEEP or (is_practice_run(r) and r.get("verdict") == "neutral") for r in runs)
        c["practices"] = len(acc & set(c["managed"] or {}))
    tied.sort(key=lambda c: (not c["kept"], -c["practices"], -c["n"], c["mean"] if goal == "latency" else -c["mean"]))
    return tied[0]


def revert_suggestions(history, variables, goal="tps", noise=2.0, managed=None):
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
        if gap > noise:
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
    if just_measured and first_eval and not practice_kept and not verdicts & {"improved", "efficiency-gain"} and verdicts & {"neutral", "efficiency-loss"}:
        parent_run = next((r for r in history if r["seq"] == first_eval["parent_runs"][-1]), None)
        if parent_run is not None:
            items.append(_revert_item(f"revert-to-run-{parent_run['seq']}", "Revert the last experiment: no measurable benefit",
                                      managed, parent_run.get("managed") or {}, variables,
                                      f"The experiment was within the {noise_txt} on the goal ({first_eval.get('goal_delta_pct'):+.1f}%) and did not reduce "
                                      "CPU or I/O per transaction. Keeping changes without a measurable benefit only adds risk.",
                                      {"goal_delta_pct": first_eval.get("goal_delta_pct"), "parent_runs": first_eval.get("parent_runs")},
                                      priority=False))
    return [i for i in items if i]


def _revert_item(id_, title, managed, target, variables, rationale, evidence, priority):
    ch = []
    for k in sorted(set(managed or {}) | set(target)):
        if normalize(k, (managed or {}).get(k)) != normalize(k, target.get(k)) and k in variables:
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


def run_engine(ctx):
    ctx = dict(ctx)
    ctx["workload"] = classify_workload(ctx.get("summary"), ctx.get("inputs")) if ctx.get("summary") else {"type": None}
    ctx["diagnosis"] = diagnose(ctx) if ctx.get("summary") else []
    items = recommend(ctx)
    items = apply_history(items, ctx.get("history") or [])
    items = practice_bundle(items, ctx.get("history") or [])
    items = items + per_query_hints(ctx.get("history") or [], ctx["variables"])
    items = revert_suggestions(ctx.get("history") or [], ctx["variables"], ctx.get("goal", "tps"),
                               float(((ctx.get("noise") or {}).get("noise_pct")) or ctx.get("noise_pct") or 2.0), ctx.get("managed")) + items
    benchmark = ctx.get("policy") == "benchmark"
    has_major = any(d.get("severity") in ("high", "medium") and d["type"] != "none" for d in ctx["diagnosis"])
    exp = ctx.get("experience") or {}
    for it in items:
        it.setdefault("safety", "safe")
        it.setdefault("tradeoffs", [])
        # mechanism prior: does this change act on what is actually limiting the server?
        rel, matched = relevance(it["id"], ctx["diagnosis"])
        if rel is not None:
            it["mechanism"] = MECHANISMS[it["id"]][1]
            it["addresses"] = matched
            it["relevance"] = rel
        if it["id"] in (ctx.get("failed") or []):
            it["confidence"] = 0
            it["note"] = (it.get("note", "") + " Failed earlier in this session (apply/restart failure or run too slow).").strip()
        # experience prior: how did this change do in earlier sessions with a similar workload?
        e = exp.get(it["id"])
        if e and e.get("n"):
            it["experience"] = e
        if it["safety"] == "trade-off" and it["kind"] == "config" and not benchmark:
            it["kind"] = "advice"
            it["note"] = (it.get("note", "") + " Trade-off: not applied under policy=safe.").strip()
        sc = score(it, ctx.get("goal", "tps")) + (2 if it["kind"] == "revert" else 0)
        if it.get("relevance") is not None:
            sc += 2 * it["relevance"] if it["relevance"] else (-3 if has_major else 0)
        if it.get("experience"):
            e = it["experience"]
            sc += round(3 * (e.get("wins", 0) - e.get("losses", 0)) / max(e["n"], 1) * min(e["n"], 3) / 3, 2)
            if e.get("losses", 0) >= 2 and not e.get("wins"):
                it["confidence"] = min(it["confidence"], 1)
        it["score"] = 100 if it.get("priority") else sc
    items.sort(key=lambda i: (i["safety"] == "trade-off" and i["kind"] != "revert", -i["score"]))
    for n, it in enumerate(items, 1):
        it["rank"] = n
    actionable = [i for i in items if i["kind"] in ("config", "revert") and i["confidence"] > 0]
    safe_next = next((i for i in actionable if i["safety"] == "safe"), None)
    trade_next = next((i for i in actionable if i["safety"] == "trade-off"), None)
    nxt = safe_next or (trade_next if benchmark else None)
    return {"workload": ctx["workload"], "diagnosis": ctx["diagnosis"], "items": items, "next": nxt["id"] if nxt else None,
            "phase": "safe" if safe_next else ("trade-offs" if nxt else "done")}
