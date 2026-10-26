"""Similarity ranking core (shared, identical in agent/tuning_agent/simcore.py and server/app/simcore.py - a test checks):
the measured state of a run as a vector, and what a change did from the most similar measured states in earlier sessions.

Prototype: tools/dbench/knn.py (2026-10-25/26). On the decision corpus, using only sessions before each decision, the
rules alone scored +0.45 pp per decision with 22 picks measured as regressions; blended with the k=15 similarity
prediction +0.90 with 5 (+1.03 with cross-session experience as well); the plain average outcome of the same change, with
similarity switched off, +0.42 - the similar states are what helps, not averaging.
"""
import collections
import math

# ---- state vectors (from what the agent measures; shares and ratios, so hosts of different sizes compare)
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


# ---- nearest measured states
def standardiser(cases):
    vals = collections.defaultdict(list)
    for c in cases:
        for k, v in c["vec"].items():
            if v is not None:
                vals[k].append(v)
    st = {}
    for k, xs in vals.items():
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1)) or 1.0
        st[k] = (m, sd)
    return st


def z(vec, st):
    return {k: (v - st[k][0]) / st[k][1] for k, v in vec.items() if v is not None and k in st}


def cosine(a, b, min_shared=10):
    ks = set(a) & set(b)
    if len(ks) < min_shared:
        return None
    dot = sum(a[k] * b[k] for k in ks)
    na = math.sqrt(sum(a[k] ** 2 for k in ks))
    nb = math.sqrt(sum(b[k] ** 2 for k in ks))
    return dot / (na * nb) if na and nb else None


def change_key(ch):
    return tuple(sorted((str(k).lower(), str(v).lower()) for k, v in (ch or {}).items()))


def item_changes(it):
    return {str(c["variable"]).lower(): str(c.get("proposed", c.get("value"))).lower() for c in it.get("changes") or []}


def matches(case, it):
    if case["rule"] and case["rule"] == it["id"]:
        return True
    ic = item_changes(it)
    return bool(ic) and change_key(case["changes"]) == change_key(ic)


def predict(qz, it, pool, k, sim=None):
    """Similarity-weighted outcome (noise widths) of this change from the k most similar states it was tried in;
    one vote per session (its cases averaged first). (pred, support sessions, neighbours)"""
    sim = sim or cosine
    sims = []
    for c in pool:
        if not matches(c, it):
            continue
        s = sim(qz, c["_z"])
        if s is not None and s > 0:
            sims.append((s, c))
    sims.sort(key=lambda x: -x[0])
    top = sims[:k]
    if not top:
        return None, 0, [], []
    per = collections.defaultdict(list)
    for s, c in top:
        per[c["session"]].append((s, max(-5.0, min(5.0, c["z"]))))
    num = den = 0.0
    for xs in per.values():
        w = max(s for s, _ in xs)
        num += w * sum(v for _, v in xs) / len(xs)
        den += w
    return (num / den if den else None), len(per), [(round(s, 3), c["session"], c["seq"], c["z"]) for s, c in top[:5]], top


K = 15                 # nearest measured states per candidate (prototype: k=15 best on the temporal corpus)
MIN_SESSIONS = 2       # a prediction counts only with support from this many sessions
BLEND_WEIGHT = 4.0     # score points per noise width of predicted outcome (prototype blend)
PRED_CAP = 3.0         # noise widths: one similar case far off cannot dominate the ranking


def predictions(qvec, items, cases, st, k=K):
    """{item id: {pred (noise widths), sessions, n, better, worse, neighbours}} for the candidates that have support.
    cases: dicts with session, seq, rule, changes, z (outcome in noise widths), verdict, _z (standardised vector)."""
    qz = z(qvec, st)
    out = {}
    for it in items:
        pred, nsess, _, top = predict(qz, it, cases, k)
        if pred is None or nsess < MIN_SESSIONS:
            continue
        out[it["id"]] = {"pred": round(max(-PRED_CAP, min(PRED_CAP, pred)), 2), "sessions": nsess, "n": len(top),
                         "better": sum(1 for _, c in top if c["verdict"] in ("improved", "efficiency-gain")),
                         "worse": sum(1 for _, c in top if c["verdict"] in ("regressed", "efficiency-loss")),
                         "neighbours": [{"sim": round(sv, 3), "session": c["session"], "seq": c["seq"], "delta_pct": c.get("delta"),
                                         "verdict": c["verdict"], "facets": c.get("facets")} for sv, c in top[:5]]}
    return out


def facets(summary, inputs, cpus, bp_bytes=None, data_bytes=None, goal=None):
    """The human-readable side of the characterisation (explanations and fairness across classes; the vector finds the
    neighbours): write class, concurrency per vCPU, memory fit, goal."""
    s, inp = summary or {}, inputs or {}
    tps = _f(s.get("tps")) or 0
    redo = _f(s.get("redo_mbs"))
    fs = (_f(s.get("data_fsyncs")) or 0) / tps if tps else 0
    write = None if redo is None else "read-only" if redo < 0.1 else "commit-heavy" if fs >= 0.5 else "writes"
    c = float(cpus or 0)
    conn = _f(s.get("threads_connected_avg"))
    per = conn / c if conn and c else None
    conc = None if per is None else "low" if per <= 1 else "medium" if per <= 6 else "high" if per <= 64 else "extreme"
    bp, data = _size(bp_bytes), _f(data_bytes if data_bytes is not None else inp.get("data_bytes"))
    fit = None
    if bp and data:
        r = data / bp
        fit = "in memory" if r <= 0.9 else "partly cached" if r <= 3 else "mostly on disk"
    return {k: v for k, v in (("writes", write), ("concurrency", conc), ("memory", fit), ("goal", goal)) if v}
