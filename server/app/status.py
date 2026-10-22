"""Server health: host trends (CPU, memory, swap, disk I/O and space), database probes and request timings.

Kept in its own small SQLite file so watching the server adds nothing to the main database. One worker process (the
one holding a file lock) samples the host every 30 s; every worker buffers its request timings in memory and flushes
them every 30 s. Samples are kept 7 days, request timings 1 day.
"""
import fcntl
import json
import os
import sqlite3
import threading
import time

DIR = os.path.dirname(os.environ.get("TUNER_DB", "/var/lib/aituner/tuner.db"))
PATH = os.path.join(DIR, "status.db")
MAIN_DB = os.environ.get("TUNER_DB", "/var/lib/aituner/tuner.db")
INTERVAL = 30
STARTED = time.time()
_reqs = []  # (ts, method, route, ms, status) not yet flushed
_lock = threading.Lock()


def _db():
    c = sqlite3.connect(PATH, timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, data TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS reqs (ts REAL, method TEXT, route TEXT, ms REAL, status INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS reqs_ts ON reqs(ts)")
    c.execute("CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, kind TEXT, severity TEXT, env_id INTEGER, session_id INTEGER, "
              "message TEXT, first_seen REAL, last_seen REAL, resolved_at REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS watchdog (id INTEGER PRIMARY KEY CHECK (id = 1), last_scan REAL, scan_ms REAL, error TEXT, checked TEXT)")
    return c


# ------------------------------------------------------------------------------------------------- stall watchdog
WATCHDOG_EVERY = 60
_watchdog = None  # set by the app: fn() -> ([alert dict], checked summary dict)


def set_watchdog(fn):
    global _watchdog
    _watchdog = fn


def _scan(c):
    t0 = time.time()
    try:
        found, checked = _watchdog()
        err = None
    except Exception as e:  # never let the watchdog take the sampler down
        found, checked, err = None, {}, f"{type(e).__name__}: {e}"[:300]
    now = time.time()
    if found is not None:
        keys = set()
        for a in found:
            keys.add(a["key"])
            if c.execute("SELECT 1 FROM alerts WHERE key=? AND resolved_at IS NULL", (a["key"],)).fetchone():
                c.execute("UPDATE alerts SET last_seen=?, message=?, severity=? WHERE key=? AND resolved_at IS NULL",
                          (now, a["message"], a["severity"], a["key"]))
            else:
                c.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?,?,NULL)",
                          (a["key"], a["kind"], a["severity"], a.get("env_id"), a.get("session_id"), a["message"], now, now))
        for (k,) in c.execute("SELECT key FROM alerts WHERE resolved_at IS NULL").fetchall():
            if k not in keys:
                c.execute("UPDATE alerts SET resolved_at=? WHERE key=?", (now, k))
        c.execute("DELETE FROM alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?", (now - 7 * 86400,))
    c.execute("INSERT OR REPLACE INTO watchdog VALUES (1,?,?,?,?)", (now, round((time.time() - t0) * 1000, 1), err, json.dumps(checked)))


def record(method, route, ms, status):
    with _lock:
        _reqs.append((time.time(), method, route, round(ms, 2), status))


# ------------------------------------------------------------------------------------------------- host counters

def _cpu():
    v = [int(x) for x in open("/proc/stat").readline().split()[1:9]]
    return {"user": v[0] + v[1], "system": v[2] + v[5] + v[6], "idle": v[3], "iowait": v[4], "steal": v[7]}


def _vm():
    d = dict(line.split()[:2] for line in open("/proc/vmstat"))
    return {"pswpin": int(d.get("pswpin", 0)), "pswpout": int(d.get("pswpout", 0)), "pgmajfault": int(d.get("pgmajfault", 0))}


def _disk_dev():
    st = os.stat(DIR).st_dev
    major, minor = os.major(st), os.minor(st)
    for line in open("/proc/partitions").read().splitlines()[2:]:
        p = line.split()
        if len(p) == 4 and int(p[0]) == major and int(p[1]) == minor:
            name = p[3]
            base = name.rstrip("0123456789")
            if base.endswith("p") and base[:-1] and os.path.exists(f"/sys/block/{base[:-1]}"):
                base = base[:-1]
            return base if os.path.exists(f"/sys/block/{base}") else name
    return None


def _diskstats(dev):
    for line in open("/proc/diskstats"):
        p = line.split()
        if len(p) > 13 and p[2] == dev:
            return {"rd_ios": int(p[3]), "rd_sec": int(p[5]), "wr_ios": int(p[7]), "wr_sec": int(p[9]), "io_ms": int(p[12])}
    return None


def _meminfo():
    d = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        d[k] = int(v.split()[0]) * 1024
    return d


def _app_rss():
    tot = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
            if b"uvicorn" in cmd or b"spawn_main" in cmd and b"python" in cmd:
                for line in open(f"/proc/{pid}/status"):
                    if line.startswith("VmRSS:"):
                        tot += int(line.split()[1]) * 1024
        except OSError:
            pass
    return tot


def _snapshot(dev):
    return {"t": time.time(), "cpu": _cpu(), "vm": _vm(), "disk": _diskstats(dev) if dev else None}


def _db_probe():
    t0 = time.time()
    c = sqlite3.connect(MAIN_DB, timeout=10)
    try:
        c.execute("SELECT 1").fetchone()
        c.execute("SELECT COUNT(*) FROM tsessions").fetchone()
    finally:
        c.close()
    return round((time.time() - t0) * 1000, 2)


def _sample(prev, cur, dev):
    dt = max(cur["t"] - prev["t"], 1e-6)
    a, b = prev["cpu"], cur["cpu"]
    tot = sum(b.values()) - sum(a.values()) or 1
    pct = lambda k: round(100.0 * (b[k] - a[k]) / tot, 2)
    m = _meminfo()
    st = os.statvfs(DIR)
    out = {"cpu_busy": round(100 - pct("idle") - pct("iowait"), 2), "cpu_user": pct("user"), "cpu_system": pct("system"),
           "cpu_iowait": pct("iowait"), "cpu_steal": pct("steal"), "load1": float(open("/proc/loadavg").read().split()[0]),
           "mem_total_mb": m["MemTotal"] // 2 ** 20, "mem_available_mb": m.get("MemAvailable", 0) // 2 ** 20,
           "swap_used_mb": (m.get("SwapTotal", 0) - m.get("SwapFree", 0)) // 2 ** 20,
           "swap_in_kbs": round((cur["vm"]["pswpin"] - prev["vm"]["pswpin"]) * 4 / dt, 1),
           "swap_out_kbs": round((cur["vm"]["pswpout"] - prev["vm"]["pswpout"]) * 4 / dt, 1),
           "major_faults_s": round((cur["vm"]["pgmajfault"] - prev["vm"]["pgmajfault"]) / dt, 1),
           "disk_free_gb": round(st.f_bavail * st.f_frsize / 2 ** 30, 2), "disk_total_gb": round(st.f_blocks * st.f_frsize / 2 ** 30, 2),
           "app_rss_mb": _app_rss() // 2 ** 20}
    if prev.get("disk") and cur.get("disk"):
        d0, d1 = prev["disk"], cur["disk"]
        out.update(disk_read_mbs=round((d1["rd_sec"] - d0["rd_sec"]) * 512 / dt / 2 ** 20, 3),
                   disk_write_mbs=round((d1["wr_sec"] - d0["wr_sec"]) * 512 / dt / 2 ** 20, 3),
                   disk_iops=round((d1["rd_ios"] - d0["rd_ios"] + d1["wr_ios"] - d0["wr_ios"]) / dt, 1),
                   disk_util=round(min(100.0, (d1["io_ms"] - d0["io_ms"]) / (dt * 10)), 1))
    for f, k in ((MAIN_DB, "db_mb"), (MAIN_DB + "-wal", "db_wal_mb")):
        try:
            out[k] = round(os.path.getsize(f) / 2 ** 20, 1)
        except OSError:
            pass
    try:
        out["db_probe_ms"] = _db_probe()
    except Exception:
        out["db_probe_ms"] = None
    return out


def _flush(c):
    with _lock:
        batch, _reqs[:] = list(_reqs), []
    if batch:
        c.executemany("INSERT INTO reqs VALUES (?,?,?,?,?)", batch)


def _loop():
    c = _db()
    lockf = open(os.path.join(DIR, "status.lock"), "w")
    sampler = False
    dev = _disk_dev()
    prev = None
    last_scan = 0
    while True:
        try:
            if not sampler:
                try:
                    fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    sampler = True
                except OSError:
                    pass
            if sampler:
                cur = _snapshot(dev)
                if prev:
                    c.execute("INSERT OR REPLACE INTO samples VALUES (?,?)", (cur["t"], json.dumps(_sample(prev, cur, dev))))
                    c.execute("DELETE FROM samples WHERE ts < ?", (time.time() - 7 * 86400,))
                    c.execute("DELETE FROM reqs WHERE ts < ?", (time.time() - 86400,))
                prev = cur
                if _watchdog and time.time() - last_scan >= WATCHDOG_EVERY:
                    _scan(c)
                    last_scan = time.time()
            _flush(c)
        except Exception:
            pass
        time.sleep(INTERVAL)


def start():
    threading.Thread(target=_loop, daemon=True, name="status-sampler").start()


# ------------------------------------------------------------------------------------------------- report

def _pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return round(v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))], 1)


def _ai_stats(hours):
    try:
        from . import llm
        return llm.stats(hours)
    except Exception as e:  # the AI section must never take the status page down
        return {"error": str(e)[:200]}


def report(hours=24):
    c = _db()
    since = time.time() - hours * 3600
    samples = [(ts, json.loads(d)) for ts, d in c.execute("SELECT ts, data FROM samples WHERE ts >= ? ORDER BY ts", (since,))]
    rq = c.execute("SELECT route, method, ms, status FROM reqs WHERE ts >= ?", (time.time() - 3600,)).fetchall()
    alerts = [dict(zip(("key", "kind", "severity", "env_id", "session_id", "message", "first_seen", "last_seen", "resolved_at"), r))
              for r in c.execute("SELECT * FROM alerts WHERE resolved_at IS NULL OR resolved_at > ? ORDER BY resolved_at IS NOT NULL, first_seen DESC",
                                 (time.time() - 86400,))]
    wd = c.execute("SELECT last_scan, scan_ms, error, checked FROM watchdog WHERE id=1").fetchone()
    c.close()
    routes = {}
    for route, method, ms, status in rq:
        routes.setdefault(f"{method} {route}", []).append((ms, status))
    slow = sorted(({"route": k, "count": len(v), "p50_ms": _pct([x[0] for x in v], 0.5), "p95_ms": _pct([x[0] for x in v], 0.95),
                    "max_ms": round(max(x[0] for x in v), 1), "errors": sum(1 for x in v if x[1] >= 500)} for k, v in routes.items()),
                  key=lambda r: -(r["p95_ms"] or 0))
    allms = [r[2] for r in rq]
    last = samples[-1][1] if samples else {}
    warn = []
    if allms and _pct(allms, 0.95) > 1000:
        warn.append(f"slow requests: p95 {_pct(allms, 0.95):.0f} ms over the last hour")
    if last.get("mem_available_mb") is not None and last["mem_available_mb"] < 0.1 * last["mem_total_mb"]:
        warn.append(f"low memory: {last['mem_available_mb']} MiB available")
    if last.get("disk_total_gb") and last["disk_free_gb"] < 0.1 * last["disk_total_gb"]:
        warn.append(f"disk almost full: {last['disk_free_gb']} GiB free")
    if (last.get("swap_in_kbs") or 0) + (last.get("swap_out_kbs") or 0) > 1024:
        warn.append("swapping")
    if last.get("cpu_busy", 0) > 85:
        warn.append(f"CPU busy {last['cpu_busy']:.0f}%")
    if (last.get("db_probe_ms") or 0) > 500:
        warn.append(f"database probe slow ({last['db_probe_ms']:.0f} ms)")
    active = [a for a in alerts if not a["resolved_at"]]
    if active:
        warn.append(f"watchdog: {len(active)} active alert(s)")
    wds = {"last_scan": wd[0], "scan_ms": wd[1], "error": wd[2], "checked": json.loads(wd[3] or "{}")} if wd else None
    if not wds or not wds["last_scan"] or time.time() - wds["last_scan"] > 5 * WATCHDOG_EVERY:
        warn.append("watchdog not running")
    elif wds["error"]:
        warn.append(f"watchdog error: {wds['error']}")
    return {"ok": not warn, "warnings": warn, "uptime_s": round(time.time() - STARTED), "now": time.time(), "last": last,
            "requests_last_hour": {"count": len(allms), "p50_ms": _pct(allms, 0.5), "p95_ms": _pct(allms, 0.95),
                                   "p99_ms": _pct(allms, 0.99), "errors": sum(1 for r in rq if r[3] >= 500)},
            "slowest_routes": slow[:12], "watchdog": wds, "ai": _ai_stats(hours), "alerts": alerts, "series": [{"ts": ts, **d} for ts, d in samples]}


SERIES = [("CPU busy %", ["cpu_busy", "cpu_iowait", "cpu_steal"], "%"), ("Load (1 min)", ["load1"], ""),
          ("Memory available MiB", ["mem_available_mb"], "MiB"), ("App memory (RSS) MiB", ["app_rss_mb"], "MiB"),
          ("Swap used MiB", ["swap_used_mb"], "MiB"), ("Swap in / out KiB/s", ["swap_in_kbs", "swap_out_kbs"], "KiB/s"),
          ("Disk read / write MiB/s", ["disk_read_mbs", "disk_write_mbs"], "MiB/s"), ("Disk utilisation %", ["disk_util"], "%"),
          ("Disk free GiB", ["disk_free_gb"], "GiB"), ("Database + WAL MiB", ["db_mb", "db_wal_mb"], "MiB"),
          ("Database probe ms", ["db_probe_ms"], "ms")]
COLORS = ["#2f6fdb", "#e08a1e", "#c0392b"]


def _spark(series, keys, w=320, h=56):
    pts = [(s["ts"], [s.get(k) for k in keys]) for s in series]
    vals = [v for _, vs in pts for v in vs if v is not None]
    if len(pts) < 2 or not vals:
        return '<svg width="%d" height="%d"></svg>' % (w, h)
    t0, t1 = pts[0][0], pts[-1][0]
    lo, hi = min(0, min(vals)), max(vals) or 1
    paths = []
    for i, _ in enumerate(keys):
        xy = [((ts - t0) / ((t1 - t0) or 1) * (w - 2) + 1, h - 2 - ((vs[i] - lo) / ((hi - lo) or 1)) * (h - 4))
              for ts, vs in pts if vs[i] is not None]
        if xy:
            paths.append(f'<polyline fill="none" stroke="{COLORS[i % 3]}" stroke-width="1.3" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in xy)}"/>')
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">{"".join(paths)}</svg>'


def _bars(series, stacks, w=520, h=90, line=None):
    """Stacked bars per time bucket (stacks: [(key, color)]), with an optional line (key, color) on its own scale."""
    if not series:
        return f'<svg width="{w}" height="{h}"></svg>'
    n = len(series)
    hi = max(sum(b.get(k) or 0 for k, _ in stacks) for b in series) or 1
    bw = (w - 2) / n
    out = []
    for i, b in enumerate(series):
        y = h - 1
        tip = time.strftime("%m-%d %H:%M", time.gmtime(b["ts"])) + " UTC: " + ", ".join(f"{k.split('/')[-1]} {b.get(k) or 0:.3g}" for k, _ in stacks)
        for k, col in stacks:
            v = b.get(k) or 0
            if v:
                bh = v / hi * (h - 6)
                y -= bh
                out.append(f'<rect x="{1 + i * bw:.1f}" y="{y:.1f}" width="{max(bw - 1, 1):.1f}" height="{bh:.1f}" fill="{col}"><title>{tip}</title></rect>')
    if line:
        k, col = line
        lv = [b.get(k) or 0 for b in series]
        lh = max(lv) or 1
        pts = " ".join(f"{1 + (i + 0.5) * bw:.1f},{h - 2 - v / lh * (h - 8):.1f}" for i, v in enumerate(lv))
        out.append(f'<polyline fill="none" stroke="{col}" stroke-width="1.6" points="{pts}"/>')
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img">{"".join(out)}</svg>'


def _ai_section(a, h_):
    if not a:
        return ""
    t, p = a["totals"], a["previous"]
    fmt = lambda v, d=0: "–" if v is None else f"{v:,.{d}f}"
    money = lambda v: "–" if v is None else (f"${v:,.2f}" if v >= 1 else f"${v:,.3f}")

    def trend(cur, old, money_=False):
        if not old:
            return '<span class="u"> new</span>' if cur else ""
        d = 100.0 * (cur - old) / old
        return f'<span class="u" title="previous {a["hours"]} h: {money(old) if money_ else fmt(old)}"> {"▲" if d > 0 else "▼" if d < 0 else "="} {abs(d):.0f}%</span>'
    st = a.get("settings") or {}
    ex = a["experiments"]
    key = a.get("key") or {}
    if key.get("error"):
        keyv = '<span class="bad">unreachable</span>'
    elif key:
        keyv = (f'{money(key.get("usage_monthly"))}<span class="u"> this month</span>' if key.get("usage_monthly") is not None else money(key.get("usage")))
        if key.get("limit") is not None:
            keyv += f'<span class="u"> · {money(key.get("limit_remaining"))} of {money(key.get("limit"))} left</span>'
    else:
        keyv = "–"
    tiles = [
        ("Live calls", f'{fmt(t["live"])}{trend(t["live"], p["live"])}', "Calls sent to OpenRouter in the period; the arrow compares with the period before."),
        ("Cached answers", f'{fmt(t["cached"])}<span class="u"> · {fmt(t["cache_hit_pct"], 0)}% of calls</span>', "Answers reused from an earlier call with the same context or the same conditions: no cost."),
        ("Cost", f'{money(t["cost"])}{trend(t["cost"], p["cost"], True)}', "Cost reported by OpenRouter for the live calls in the period."),
        ("Cost per live call", money(t["cost_per_live_call"]), ""),
        ("Tokens in / out", f'{fmt(t["tokens_in"])} / {fmt(t["tokens_out"])}', ""),
        ("Latency p50 / p95", f'{fmt(t["latency_p50_s"], 1)} / {fmt(t["latency_p95_s"], 1)}<span class="u"> s</span>', "Live calls only."),
        ("Errors", f'<span class="{"bad" if t["errors"] else ""}">{fmt(t["errors"])}</span>', ""),
        ("AI experiments", f'{fmt(ex["tried"])} run<span class="u"> · {fmt(ex["improved"])} improved'
                           + (f' · {money(ex["cost_per_improvement"])} per improvement' if ex["cost_per_improvement"] else "") + "</span>",
         "Measured runs of changes the model proposed (ideas and bottleneck analyses) in the period, and how many improved the goal. Verdicts: "
         + ", ".join(f"{k} {v}" for k, v in sorted(ex["verdicts"].items(), key=lambda x: -x[1]))),
        ("OpenRouter key", keyv, "Usage and limit of the service key as OpenRouter reports them (refreshed every 10 min)."),
    ]
    info = lambda tip: ' <span class="i" title="%s">ⓘ</span>' % h_.escape(tip) if tip else ""
    tl = "".join(f'<div class="t"><div class="k">{h_.escape(k)}{info(tip)}</div><div class="v">{v}</div></div>' for k, v, tip in tiles)
    mcol = ["#e08a1e", "#8e44ad", "#16a085", "#d35400", "#2c3e50", "#b7950b"]
    mstack = [("cost:" + m["model"], mcol[i % len(mcol)]) for i, m in enumerate(a["by_model"])]
    mleg = " ".join(f'<i style=background:{c}></i>{h_.escape(k[5:].split("/")[-1])}' for k, c in mstack)
    unit = "hour" if a["bucket_s"] == 3600 else "day" if a["bucket_s"] == 86400 else "6 h"
    charts = (f'<div class="g2"><div class="t"><div class="k">Calls per {unit}</div>{_bars(a["series"], [("live", "#2f6fdb"), ("cached", "#9bbcf0"), ("errors", "#c0392b")])}'
              f'<div class="lg"><i style=background:#2f6fdb></i>live <i style=background:#9bbcf0></i>cached <i style=background:#c0392b></i>errors</div></div>'
              f'<div class="t"><div class="k">Cost per {unit} by model (bars) and cumulative (line)</div>{_bars(a["series"], mstack, line=("cum_cost", "#6b6b70"))}'
              f'<div class="lg">{mleg} <i style=background:#6b6b70></i>cumulative {money(t["cost"])}</div></div></div>')

    def table(rows, first, label):
        if not rows:
            return ""
        name = lambda v: f'<span title="{h_.escape(str(v))}">{h_.escape(str(v).split("/")[-1])}</span>'
        body = "".join(f'<tr><td class=n>{name(r[first])}</td><td>{fmt(r["live"])}</td><td>{fmt(r["cached"])}</td><td class=x>{fmt(r["errors"])}</td>'
                       f'<td class=x>{fmt(r["tokens_in"])}</td><td class=x>{fmt(r["tokens_out"])}</td><td>{money(r["cost"])}</td><td>{money(r["cost_per_live_call"])}</td>'
                       f'<td class=x>{fmt(r["latency_p50_s"], 1)} / {fmt(r["latency_p95_s"], 1)}</td></tr>' for r in rows)
        return (f'<div class="w"><table><tr><th class=n>{label}</th><th>live</th><th>cached</th><th class=x>errors</th><th class=x>tokens in</th><th class=x>tokens out</th>'
                f'<th>cost</th><th>per call</th><th class=x>latency p50 / p95 s</th></tr>{body}</table></div>')
    sess = "".join(f'<tr><td class=m><a href="/#/sessions/{s["session_id"]}">#{s["session_id"]}</a> {h_.escape(s.get("name") or "")}</td>'
                   f'<td class=m>{h_.escape(s.get("env") or "")}</td><td>{fmt(s["calls"])}</td><td>{fmt(s["cached"])}</td><td>{money(s["cost"])}</td></tr>'
                   for s in a["top_sessions"])
    sess_t = (f'<h3>Sessions with the highest AI cost</h3><div class="w"><table><tr><th class=m>session</th><th class=m>environment</th>'
              f'<th>calls</th><th>cached</th><th>cost</th></tr>{sess}</table></div>') if sess else ""
    return (f'<h2>AI model use (OpenRouter)</h2><p class="sub" style="margin:0 0 8px">default model {h_.escape(st.get("model") or "–")} · mode '
            f'{h_.escape(st.get("mode") or "–")}</p><div class="g">{tl}</div>{charts}'
            f'<h3>By model</h3>{table(a["by_model"], "model", "model")}<h3>By purpose</h3>{table(a["by_purpose"], "purpose", "purpose")}{sess_t}')


def html(hours=24):
    import html as h_
    r = report(hours)
    try:
        ai = _ai_section(r["ai"], h_) if "error" not in r["ai"] else ""
    except Exception as e:
        ai = None
        r["ai"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    if not ai and "error" in r["ai"]:
        ai = f'<h2>AI model use (OpenRouter)</h2><p class="bad">unavailable: {h_.escape(r["ai"]["error"])}</p>'
    last, rq = r["last"], r["requests_last_hour"]
    fmt = lambda v: "–" if v is None else (f"{v:,.1f}" if isinstance(v, float) else f"{v:,}")
    tiles = "".join(
        f'<div class="t"><div class="k">{h_.escape(title)}</div><div class="v">{" / ".join(fmt(last.get(k)) for k in keys)}'
        f'<span class="u"> {unit}</span></div>{_spark(r["series"], keys)}'
        f'<div class="lg">{" ".join(f"<i style=background:{COLORS[i % 3]}></i>{h_.escape(k)}" for i, k in enumerate(keys)) if len(keys) > 1 else ""}</div></div>'
        for title, keys, unit in SERIES)
    rows = "".join(f"<tr><td class=m>{h_.escape(x['route'])}</td><td>{x['count']}</td><td>{fmt(x['p50_ms'])}</td><td>{fmt(x['p95_ms'])}</td>"
                   f"<td>{fmt(x['max_ms'])}</td><td>{x['errors']}</td></tr>" for x in r["slowest_routes"])
    ago = lambda t: "–" if not t else (f"{int((time.time() - t) // 60)} min ago" if time.time() - t < 7200 else f"{(time.time() - t) / 3600:.1f} h ago")
    wd = r["watchdog"] or {}
    ck = wd.get("checked") or {}
    wd_line = (f'<span class="{"bad" if wd.get("error") or not wd.get("last_scan") or time.time() - wd["last_scan"] > 5 * WATCHDOG_EVERY else "ok"}">'
               f'{"not running" if not wd.get("last_scan") else ("error: " + h_.escape(wd["error"]) if wd.get("error") else "running")}</span>'
               f' · last scan {ago(wd.get("last_scan"))} ({fmt(wd.get("scan_ms"))} ms) every {WATCHDOG_EVERY} s · checked '
               f'{ck.get("environments", 0)} environments, {ck.get("client_hosts", 0)} client hosts, {ck.get("active_sessions", 0)} active sessions')
    arows = "".join(f'<tr><td class=m>{"<span class=bad>active</span>" if not a["resolved_at"] else "resolved " + ago(a["resolved_at"])}</td>'
                    f'<td class=m>{h_.escape(a["severity"])}</td><td class=m>{h_.escape(a["message"])}</td><td>{ago(a["first_seen"])}</td></tr>'
                    for a in r["alerts"]) or '<tr><td class=m colspan=4>No alerts in the last 24 h.</td></tr>'
    state = '<span class="ok">healthy</span>' if r["ok"] else '<span class="bad">' + h_.escape("; ".join(r["warnings"])) + "</span>"
    up = r["uptime_s"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tuner Server Status</title><meta http-equiv="refresh" content="60"><style>
:root{{--bg:#fafafa;--fg:#1d1d1f;--mut:#6b6b70;--card:#fff;--line:#e4e4e7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#e8e8ea;--mut:#9a9aa0;--card:#1e1e21;--line:#2e2e33}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;max-width:1100px;margin:auto}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:var(--mut);margin:0 0 14px}}.ok{{color:#1e8e3e;font-weight:600}}.bad{{color:#c0392b;font-weight:600}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}}.t{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:hidden}}
.t svg{{width:100%;height:56px}}.k{{color:var(--mut);font-size:12px}}.v{{font-size:18px;font-weight:600}}.u{{font-size:12px;color:var(--mut);font-weight:400}}
.lg{{font-size:11px;color:var(--mut)}}.lg i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 3px 0 6px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;font-size:13px}}
td,th{{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right}}td.m,th.m{{text-align:left;font-family:ui-monospace,monospace;word-break:break-all}}
.w{{overflow-x:auto}}h2{{font-size:15px;margin:18px 0 8px}}h3{{font-size:13px;margin:12px 0 6px;color:var(--mut)}}
.g2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px;margin-top:10px}}.g2 svg{{width:100%;height:90px}}.i{{cursor:help;color:var(--mut)}}
a{{color:#2f6fdb}}td.n,th.n{{text-align:left;font-family:ui-monospace,monospace;white-space:nowrap}}
@media (max-width:640px){{.x{{display:none}}}}</style></head><body>
<h1>Tuner server status</h1><p class="sub">{state} · up {up // 3600} h {up % 3600 // 60} min · last {hours} h, sampled every {INTERVAL} s ·
requests last hour: {rq['count']} · p50 {fmt(rq['p50_ms'])} ms · p95 {fmt(rq['p95_ms'])} ms · p99 {fmt(rq['p99_ms'])} ms · 5xx {rq['errors']}
· <a href="/status.json">JSON</a> · <a href="?h=1">1 h</a> <a href="?h=24">24 h</a> <a href="?h=168">7 d</a> <a href="?h=720">30 d</a></p>
<h2>Stall watchdog</h2><p class="sub" style="margin:0 0 8px">{wd_line}</p>
<div class="w"><table><tr><th class=m>state</th><th class=m>severity</th><th class=m>alert</th><th>since</th></tr>{arows}</table></div>
{ai}
<h2>Host</h2><div class="g">{tiles}</div>
<h2>Slowest endpoints, last hour</h2><div class="w"><table><tr><th class=m>endpoint</th><th>requests</th><th>p50 ms</th><th>p95 ms</th><th>max ms</th><th>5xx</th></tr>{rows}</table></div>
</body></html>"""
