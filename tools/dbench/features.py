"""State vectors for similarity search (case-based ranking prototype, 2026-10-25).

One vector per measured run state, from what the agent measures - not from workload labels (user: classifying databases
into workloads does not help much; similar evidence should). Shares and ratios only, so hosts of different sizes compare:
- where the database's time went (time breakdown shares by mechanism tag);
- where its CPU went (CPU profile categories);
- work per transaction (rows changed, redo, fsyncs, disk reads), memory fit (data / buffer pool, hit rate), concurrency
  (threads running and connected per vCPU), host state (CPU busy, system share, I/O wait, disk latency), undo backlog.
The same function serves the case base (past runs, on the server) and the decision points (corpus contexts).
"""
import math


def _l1p(x):
    return math.log1p(max(0.0, x or 0.0))

TB_GROUPS = (  # time breakdown tag -> feature (first match; CPU components all carry "cpu")
    ("cpu", "tb_cpu"), ("sched-delay", "tb_sched"), ("commit", "tb_commit"), ("io-read", "tb_io_read"), ("io-flush", "tb_io_flush"),
    ("io", "tb_io"), ("locks", "tb_locks"), ("concurrency", "tb_latch"), ("network", "tb_network"), ("memory", "tb_memory"),
    ("swap", "tb_memory"))
PROFILE_CATS = (("B-tree / row access", "p_btree"), ("spin / mutex wait", "p_spin"), ("redo / commit", "p_redo"),
                ("buffer pool", "p_bufpool"), ("parser / optimizer", "p_parser"), ("SQL executor", "p_exec"),
                ("statement dispatch", "p_dispatch"), ("network / protocol", "p_network"), ("scheduler / futex", "p_sched"),
                ("memory allocator", "p_malloc"), ("performance_schema", "p_pfs"), ("compression / checksums", "p_compress"),
                ("row locks", "p_rowlocks"), ("table open / cache", "p_tableopen"), ("disk I/O (kernel)", "p_kio"))
NAMES = sorted({n for _, n in TB_GROUPS} | {"tb_cpu_queue"} | {n for _, n in PROFILE_CATS} | {
    "rows_chg_trx", "redo_kb_trx", "fsync_trx", "reads_trx", "miss_pct", "data_over_bp", "run_per_cpu", "conn_per_cpu",
    "cpu_busy", "sys_share", "iowait", "r_await", "w_await", "dirty", "ckpt_age", "hll", "lock_wait_ms", "read_only"})


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _size(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    m = {"K": 2 ** 10, "M": 2 ** 20, "G": 2 ** 30, "T": 2 ** 40}
    try:
        return float(s[:-1]) * m[s[-1]] if s and s[-1] in m else float(s)
    except ValueError:
        return None


def vector(summary, inputs, cpus, bp_bytes=None, data_bytes=None):
    """{feature: value} - missing measurements stay missing (None), so a run without a CPU profile is compared on the rest."""
    s, inp = summary or {}, inputs or {}
    out = {}
    tb = inp.get("time_breakdown") or []
    if tb:
        acc = {}
        for p in tb:
            tags = set(p.get("tags") or [])
            sh = (p.get("share_pct") or 0) / 100.0
            for tag, name in TB_GROUPS:
                if tag in tags:
                    acc[name] = acc.get(name, 0) + sh
                    break
            if p.get("cpu_queue_share_pct"):
                acc["tb_cpu_queue"] = acc.get("tb_cpu_queue", 0) + p["cpu_queue_share_pct"] / 100.0
        for _, name in TB_GROUPS:
            out[name] = min(1.0, acc.get(name, 0.0))
        out["tb_cpu_queue"] = min(1.0, acc.get("tb_cpu_queue", 0.0))
    cats = (inp.get("cpu_profile") or {}).get("categories") or {}
    if cats:
        for c, name in PROFILE_CATS:
            out[name] = (cats.get(c) or 0) / 100.0
    tps = _f(s.get("tps")) or 0
    if tps > 0:
        out["rows_chg_trx"] = _l1p((_f(s.get("rows_changed_per_s")) or 0) / tps)
        out["redo_kb_trx"] = _l1p((_f(s.get("redo_mbs")) or 0) * 1024 / tps)
        out["fsync_trx"] = _l1p((_f(s.get("data_fsyncs")) or 0) / tps)
        out["reads_trx"] = _l1p((_f(s.get("bp_disk_reads")) or 0) / tps)
    if s.get("redo_mbs") is not None:
        out["read_only"] = 1.0 if (_f(s.get("redo_mbs")) or 0) < 0.1 else 0.0
    if s.get("bp_hit_pct") is not None:
        out["miss_pct"] = _l1p(max(0.0, 100 - (_f(s["bp_hit_pct"]) or 100)))
    bp, data = _size(bp_bytes), _f(data_bytes if data_bytes is not None else inp.get("data_bytes"))
    if bp and data:
        out["data_over_bp"] = math.log(max(data, 1) / max(bp, 1))
    c = float(cpus or 0)
    if c:
        if s.get("threads_running_avg") is not None:
            out["run_per_cpu"] = _l1p((_f(s["threads_running_avg"]) or 0) / c)
        if s.get("threads_connected_avg") is not None:
            out["conn_per_cpu"] = _l1p((_f(s["threads_connected_avg"]) or 0) / c)
    busy = _f(s.get("cpu_busy"))
    if busy is not None:
        out["cpu_busy"] = busy / 100.0
        if busy > 1 and s.get("cpu_sys") is not None:
            out["sys_share"] = (_f(s["cpu_sys"]) or 0) / busy
    if s.get("cpu_iowait") is not None:
        out["iowait"] = (_f(s["cpu_iowait"]) or 0) / 100.0
    for k, n in (("disk_r_await_ms", "r_await"), ("disk_w_await_ms", "w_await"), ("row_lock_wait_avg_ms", "lock_wait_ms")):
        if s.get(k) is not None:
            out[n] = _l1p(_f(s[k]) or 0)
    if s.get("bp_dirty_pct_avg") is not None:
        out["dirty"] = (_f(s["bp_dirty_pct_avg"]) or 0) / 100.0
    if s.get("checkpoint_age_pct_max") is not None:
        out["ckpt_age"] = (_f(s["checkpoint_age_pct_max"]) or 0) / 100.0
    if s.get("history_list_length_max") is not None:
        out["hll"] = math.log10(1 + (_f(s["history_list_length_max"]) or 0)) / 7.0
    return out
