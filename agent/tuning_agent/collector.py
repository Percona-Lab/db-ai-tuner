"""Metric sampling and measurement-window analysis."""
import hashlib
import os
import time

from . import mysqldb
from .hostinfo import OSCounters

# per-second rates computed from SHOW GLOBAL STATUS counters
RATE_COUNTERS = {
    "qps": ["Questions"],
    "commits": ["Com_commit"],
    "rollbacks": ["Com_rollback"],
    "handler_commits": ["Handler_commit"],
    "selects": ["Com_select"],
    "inserts": ["Com_insert", "Com_insert_select"],
    "updates": ["Com_update", "Com_update_multi"],
    "deletes": ["Com_delete", "Com_delete_multi"],
    "calls": ["Com_call_procedure"],
    "bp_read_requests": ["Innodb_buffer_pool_read_requests"],
    "bp_disk_reads": ["Innodb_buffer_pool_reads"],
    "bp_pages_flushed": ["Innodb_buffer_pool_pages_flushed"],
    "bp_wait_free": ["Innodb_buffer_pool_wait_free"],
    "read_ahead": ["Innodb_buffer_pool_read_ahead"],  # pages prefetched by linear read-ahead
    "read_ahead_rnd": ["Innodb_buffer_pool_read_ahead_rnd"],  # by random read-ahead
    "read_ahead_evicted": ["Innodb_buffer_pool_read_ahead_evicted"],  # prefetched, evicted before any use: wasted I/O
    "data_reads": ["Innodb_data_reads"],
    "data_writes": ["Innodb_data_writes"],  # every write through InnoDB's file layer: redo + data pages + doublewrite
    "log_writes": ["Innodb_log_writes"],  # redo log write calls
    "dblwr_writes": ["Innodb_dblwr_writes"],  # doublewrite batches
    "data_written_mbs": ["Innodb_data_written"],  # bytes (scaled to MiB below)
    "log_written_mbs": ["Innodb_os_log_written"],
    "data_fsyncs": ["Innodb_data_fsyncs"],
    "log_fsyncs": ["Innodb_os_log_fsyncs"],
    "log_waits": ["Innodb_log_waits"],
    "row_lock_waits": ["Innodb_row_lock_waits"],
    "rows_read": ["Innodb_rows_read"],
    "rows_changed": ["Innodb_rows_inserted", "Innodb_rows_updated", "Innodb_rows_deleted"],
    "tmp_tables": ["Created_tmp_tables"],
    "tmp_disk_tables": ["Created_tmp_disk_tables"],
    "sort_merge_passes": ["Sort_merge_passes"],
    "select_full_join": ["Select_full_join"],
    "select_scan": ["Select_scan"],
    "opened_tables": ["Opened_tables"],
    "opened_table_defs": ["Opened_table_definitions"],
    "threads_created": ["Threads_created"],
    "aborted_connects": ["Aborted_connects"],
    "slow_queries": ["Slow_queries"],
    "binlog_cache_disk_use": ["Binlog_cache_disk_use"],
    "handler_read_rnd_next": ["Handler_read_rnd_next"],
}
GAUGES = {
    "threads_running": "Threads_running",
    "threads_connected": "Threads_connected",
    "row_lock_current_waits": "Innodb_row_lock_current_waits",
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DBSampler:
    """Cheap per-interval sample of the database."""

    def __init__(self, db):
        self.db = db
        self.variables = None
        self.ps = None

    def snapshot(self):
        if self.variables is None:
            self.variables = self.db.variables()
            self.ps = self.db.ps_enabled()
        s = {"t": time.time(), "status": self.db.status()}
        if self.ps:
            s["stmt"] = self.db.statement_totals()
        s["innodb"] = mysqldb.parse_innodb_status(self.db.innodb_status_text())
        try:  # the oldest open transaction: one that stays open holds a read view, and purge cannot pass it
            s["oldest_trx_s"] = self.db.scalar("SELECT IFNULL(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW())), 0) "
                                               "FROM information_schema.innodb_trx")
        except Exception:
            pass
        return s

    def rates(self, a, b):
        dt = max(b["t"] - a["t"], 1e-6)
        sa, sb = a["status"], b["status"]
        out = {}
        for name, keys in RATE_COUNTERS.items():
            tot = 0.0
            seen = False
            for k in keys:
                x, y = _num(sa.get(k)), _num(sb.get(k))
                if x is not None and y is not None:
                    tot += y - x
                    seen = True
            if seen:
                out[name] = round(max(tot, 0) / dt, 2)
        if "data_fsyncs" in out and "log_fsyncs" in out:
            # Innodb_data_fsyncs counts every InnoDB fsync, the redo log's included (8.4, measured: 2,000 commits moved both
            # counters by ~2,000); the data-file part is the difference
            out["datafile_fsyncs"] = round(max(out["data_fsyncs"] - out["log_fsyncs"], 0), 2)
        for k in ("data_written_mbs", "log_written_mbs"):  # bytes/s -> MiB/s
            if k in out:
                out[k] = round(out[k] / 2 ** 20, 2)
        out["tps"] = round(out.get("commits", 0) + out.get("rollbacks", 0), 2)
        if not out["tps"] and out.get("handler_commits"):
            # autocommit statements send no COMMIT: each is a transaction the engine commits (r1 WI-3: tps 0 hid a
            # workload spending 45% of its time in group commit from every commit rule)
            out["tps"] = out["handler_commits"]
            out["tps_from"] = "Handler_commit (autocommit)"
            if str((self.variables or {}).get("log_bin", "")).upper() in ("ON", "1"):
                # with the binary log on every transaction commits twice (two-phase commit: the binary log and InnoDB):
                # counted once (lab 221: 84,579 "tps" for 42,282 transactions, so binlog-off runs looked +100% unreliable)
                out["tps"] = round(out["handler_commits"] / 2, 2)
                out["tps_from"] = "Handler_commit / 2 (autocommit, binary log two-phase commit)"
        for name, k in GAUGES.items():
            v = _num(sb.get(k))
            if v is not None:
                out[name] = v
        req, rd = out.get("bp_read_requests"), out.get("bp_disk_reads")
        if req:
            out["bp_hit_pct"] = round(100.0 * (1 - (rd or 0) / req), 3)
        tot = _num(sb.get("Innodb_buffer_pool_pages_total"))
        if tot:
            out["bp_dirty_pct"] = round(100.0 * (_num(sb.get("Innodb_buffer_pool_pages_dirty")) or 0) / tot, 2)
            out["bp_free_pct"] = round(100.0 * (_num(sb.get("Innodb_buffer_pool_pages_free")) or 0) / tot, 2)
        x, y = _num(sa.get("Innodb_os_log_written")), _num(sb.get("Innodb_os_log_written"))
        if x is not None and y is not None:
            out["redo_mbs"] = round((y - x) / dt / 2 ** 20, 3)
        x, y = _num(sa.get("Innodb_row_lock_time")), _num(sb.get("Innodb_row_lock_time"))
        w = (_num(sb.get("Innodb_row_lock_waits")) or 0) - (_num(sa.get("Innodb_row_lock_waits")) or 0)
        if x is not None and w > 0:
            out["row_lock_wait_avg_ms"] = round((y - x) / w, 2)
        for k in ("Bytes_received", "Bytes_sent"):
            x, y = _num(sa.get(k)), _num(sb.get(k))
            if x is not None:
                out[k.lower().replace("bytes_", "net_") + "_mbs"] = round((y - x) / dt / 2 ** 20, 3)
        if a.get("stmt") and b.get("stmt"):
            dc = b["stmt"][0] - a["stmt"][0]
            dw = b["stmt"][1] - a["stmt"][1]
            if dc > 0:
                out["avg_latency_ms"] = round(dw / dc / 1e9, 4)
                out["statements"] = round(dc / dt, 2)
        ib = b.get("innodb", {})
        if "lsn" in ib and "checkpoint_lsn" in ib:
            age = ib["lsn"] - ib["checkpoint_lsn"]
            out["checkpoint_age_mb"] = round(age / 2 ** 20, 2)
            cap = mysqldb.redo_capacity(self.variables or {})
            if cap:
                out["checkpoint_age_pct"] = round(100.0 * age / cap, 2)
        if "history_list_length" in ib:
            out["history_list_length"] = ib["history_list_length"]
        if "ibuf_size" in ib:
            out["ibuf_size"] = ib["ibuf_size"]
        if b.get("oldest_trx_s") is not None:
            out["oldest_trx_s"] = int(b["oldest_trx_s"])
        if "os_wait_reservations" in ib and "os_wait_reservations" in a.get("innodb", {}):
            out["innodb_os_waits"] = round((ib["os_wait_reservations"] - a["innodb"]["os_wait_reservations"]) / dt, 1)
        return out


class Sampler:
    """OS + DB sampler producing one merged metrics dict per call."""

    def __init__(self, db, device):
        self.dbs = DBSampler(db)
        self.oss = OSCounters(device)
        self.prev_db = None
        self.prev_os = None

    def reset_db(self):
        self.prev_db = None
        self.dbs.variables = None

    def sample(self):
        now_os = self.oss.snapshot()
        out = {"ts": round(now_os["t"], 3), "os": {}, "db": {}, "db_up": True}
        if self.prev_os:
            out["os"] = OSCounters.rates(self.prev_os, now_os)
        self.prev_os = now_os
        try:
            now_db = self.dbs.snapshot()
            if self.prev_db:
                out["db"] = self.dbs.rates(self.prev_db, now_db)
            self.prev_db = now_db
            # performance_schema wait summary: one in-memory table read per sample
            w, wt = self.dbs.db.waits(), time.time()
            if w and getattr(self, "prev_waits", None) and wt > self.prev_waits[1]:
                cls, top = wait_rates(self.prev_waits[0], w, wt - self.prev_waits[1])
                out["db"].update(cls)
                out["waits"] = top
            self.prev_waits = (w, wt) if w else None
        except Exception as e:  # database down / restarting
            out["db_up"] = False
            out["db_error"] = str(e)[:200]
            self.reset_db()
            try:
                self.dbs.db.close()
            except Exception:
                pass
        return out


WAIT_CLASSES = (  # (prefix, class) - first match wins
    ("wait/io/file/innodb/innodb_log_file", "redo_io"), ("wait/io/file/innodb/innodb_redo", "redo_io"),
    ("wait/io/file/innodb/", "data_io"), ("wait/io/file/sql/binlog", "binlog_io"), ("wait/io/file/", "other_file_io"),
    ("wait/io/table/", "table_io"), ("wait/lock/metadata/", "metadata_lock"), ("wait/lock/", "table_lock"),
    ("wait/synch/", "sync"), ("wait/io/socket/", "socket"),
)


def wait_class(event):
    for prefix, cls in WAIT_CLASSES:
        if event.startswith(prefix):
            return cls
    return "other"


def wait_rates(a, b, dt, top=10):
    """Seconds waited per second (= average number of threads waiting) per class, and the top events."""
    classes, events = {}, {}
    for name, (cnt, tm) in b.items():
        c0, t0 = a.get(name, (0, 0))
        if tm < t0 or cnt < c0:  # counters truncated
            continue
        sec = (tm - t0) / 1e12 / dt
        if sec <= 0:
            continue
        classes[wait_class(name)] = classes.get(wait_class(name), 0) + sec
        events[name] = sec
    return ({f"wait_{k}_s": round(v, 4) for k, v in classes.items()},
            {k: round(v, 5) for k, v in sorted(events.items(), key=lambda x: -x[1])[:top]})


# ---- measurement windows -------------------------------------------------------

def full_snapshot(db, device):
    """Everything the analysis needs at the boundary of a measurement window."""
    ps = db.ps_enabled()
    snap = {
        "t": time.time(),
        "variables": db.variables(),
        "status": db.status(),
        "innodb_metrics": db.innodb_metrics(),
        "innodb_text": db.innodb_status_text(),
        "os": OSCounters(device).snapshot(),
        "ps": ps,
    }
    if ps:
        snap["stmt"] = db.statement_totals()
        snap["hist"] = db.statement_histogram()
        snap["digests"] = [d for d in db.digests() if not _own_query(d.get("DIGEST_TEXT"))]
        for d in db.prepared():
            if _own_query(d.get("DIGEST_TEXT")):
                continue
            d["DIGEST"] = "prepared:" + hashlib.md5((d["DIGEST_TEXT"] or "").encode()).hexdigest()[:16]
            d["SCHEMA_NAME"] = d.get("SCHEMA_NAME") or ""
            snap["digests"].append(d)
        snap["waits"] = db.waits()
        snap["file_io"] = db.file_io()
        snap["wait_coverage"] = db.wait_coverage()
        snap["mem"] = db.memory()
    snap["data_bytes"], snap["tables"] = db.table_sizes()
    snap["errors"] = db.errors_raised() if ps else None
    try:  # the data directory's filesystem: a seasoned / growing data set can fill it (session 222)
        st = os.statvfs(snap["variables"].get("datadir") or "/var/lib/mysql")
        snap["fs"] = {"free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}
    except (OSError, TypeError):
        snap["fs"] = None
    return snap


MEM_CATEGORIES = (("memory/innodb/buf_buf_pool", "InnoDB buffer pool"), ("memory/innodb/", "InnoDB other"),
                  ("memory/performance_schema/", "performance_schema"), ("memory/temptable/", "TempTable (in-memory temp tables)"),
                  ("memory/sql/", "server (connections, statements, caches)"), ("memory/mysys/", "server (connections, statements, caches)"))


def memory_window(a, b):
    """MySQL's own memory accounting at the end of the window: total, by category, connection vs background threads,
    the top allocations with their high-water marks, and what grew during the window."""
    ev_b = (b or {}).get("events") or {}
    ev_a = (a or {}).get("events") or {}
    MB = 2 ** 20
    cur = {k: max(0, v[0]) for k, v in ev_b.items()}  # alloc in one thread, free in another: per-event currents can go negative
    cats = {}
    for k, v in cur.items():
        name = next((n for p, n in MEM_CATEGORIES if k.startswith(p)), "other")
        cats[name] = cats.get(name, 0) + v
    total = sum(cur.values())
    growth = {k: cur[k] - max(0, (ev_a.get(k) or [0])[0]) for k in cur} if ev_a else {}
    top = sorted(ev_b.items(), key=lambda x: -max(0, x[1][0]))[:12]
    return {"total_mb": round(total / MB, 1), "categories_mb": {k: round(v / MB, 1) for k, v in sorted(cats.items(), key=lambda x: -x[1])},
            "connections_mb": round((b or {}).get("fg", 0) / MB, 1), "background_mb": round((b or {}).get("bg", 0) / MB, 1),
            "growth_mb": round((total - sum(max(0, v[0]) for v in ev_a.values())) / MB, 1) if ev_a else None,
            "growing": [{"event": k, "mb": round(g / MB, 1)} for k, g in sorted(growth.items(), key=lambda x: -x[1])[:5] if g > MB],
            "top": [{"event": k, "current_mb": round(max(0, v[0]) / MB, 1), "high_mb": round(v[1] / MB, 1)} for k, v in top]}


# file I/O by what it is for: data pages (read path vs flushing), doublewrite, redo, binlog, temporary files
FILE_KINDS = (("innodb_data_file", "data"), ("innodb_dblwr_file", "doublewrite"), ("innodb_log_file", "redo"),
              ("innodb_temp_file", "temp"), ("binlog", "binlog"), ("relaylog", "binlog"))


def file_io_delta(a, b):
    """{kind: {read_s, write_s, sync_s, reads, writes, syncs}} over the window (sync = fsync and other misc operations)."""
    out = {}
    for name, v in b.items():
        v0 = a.get(name) or [0] * 6
        d = [x - y for x, y in zip(v, v0)]
        if any(x < 0 for x in d):
            continue  # truncated
        kind = next((k for pat, k in FILE_KINDS if pat in name), "other")
        o = out.setdefault(kind, {"reads": 0, "read_s": 0.0, "writes": 0, "write_s": 0.0, "syncs": 0, "sync_s": 0.0})
        o["reads"] += d[0]; o["read_s"] += d[1] / 1e12
        o["writes"] += d[2]; o["write_s"] += d[3] / 1e12
        o["syncs"] += d[4]; o["sync_s"] += d[5] / 1e12
    return {k: {x: round(y, 4) if isinstance(y, float) else y for x, y in v.items()} for k, v in out.items()
            if v["read_s"] + v["write_s"] + v["sync_s"] > 0}


ZERO_EVIDENCE = {"Threads_created", "Opened_tables", "Opened_table_definitions", "Created_tmp_disk_tables", "Created_tmp_tables",
                 "Sort_merge_passes", "Innodb_buffer_pool_wait_free", "Innodb_log_waits", "Innodb_row_lock_waits",
                 "Select_full_join", "Select_scan", "Connections", "Aborted_connects", "Innodb_buffer_pool_reads"}


def _pctl(vals, q):
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def sample_spread(samples, group, keys):
    """p50 / p95 / max and CV of each metric over the run's samples: how much a latency moves inside the window."""
    out = {}
    for k in keys:
        vals = [s[group][k] for s in samples if s.get(group, {}).get(k) is not None]
        if len(vals) < 3:
            continue
        m = sum(vals) / len(vals)
        out[f"{k}_p50"] = round(_pctl(vals, 0.5), 3)
        out[f"{k}_p95"] = round(_pctl(vals, 0.95), 3)
        out[f"{k}_max"] = round(max(vals), 3)
        if m:
            out[f"{k}_cv_pct"] = round(100.0 * (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 / m, 1)
    return out


def within_run_stability(samples, key="tps"):
    """Throughput dips inside the window: samples below half the median (stalls, e.g. log write or flushing stalls,
    swap-in storms), the longest run of such samples, and the worst sample relative to the median."""
    vals = [s["db"][key] for s in samples if s.get("db", {}).get(key) is not None]
    if len(vals) < 5:
        vals = [s["db"]["qps"] for s in samples if s.get("db", {}).get("qps") is not None]
        if len(vals) < 5:
            return {}
    med = _pctl(vals, 0.5)
    if not med:
        return {}
    low = [v < 0.5 * med for v in vals]
    longest = cur = 0
    for x in low:
        cur = cur + 1 if x else 0
        longest = max(longest, cur)
    return {"tput_dip_samples": sum(low), "tput_dip_longest_samples": longest, "tput_samples": len(vals),
            "tput_min_vs_median_pct": round(100.0 * min(vals) / med, 1)}


def db_wait_coverage(snap):
    return snap.get("wait_coverage")


OWN_MARKERS = ("SUM_TIMER_", "COUNT_EXECUTE", "COUNT_STAR", "_BYTES_USED", "EVENT_NAME", "RESOURCE_GROUP", "THREAD_OS_ID",
               "TRX_STARTED", "VARIABLE_VALUE")


def _own_query(text):
    """Monitoring statements issued by the agent itself. Digest texts are cut at 400 characters, so the
    performance_schema table name can be past the cut: the agent's own column names count too (342: its query on
    prepared_statements_instances showed as a top statement)."""
    t = (text or "").upper()
    return (t.startswith("SHOW ") or t.startswith("SELECT @@") or "PERFORMANCE_SCHEMA" in t
            or "INFORMATION_SCHEMA" in t or t.startswith("SET GLOBAL") or any(m in t for m in OWN_MARKERS))


def use_prepared_tail(snap, tail):
    """Replace the end snapshot's prepared statements with the last reading taken while the benchmark was still
    connected, when that reading holds more executions (the benchmark exited and took its statements along)."""
    if not tail or not snap or snap.get("digests") is None:
        return
    ts, rows = tail
    have = sum(int(d.get("COUNT_STAR") or 0) for d in snap["digests"] if str(d.get("DIGEST", "")).startswith("prepared:"))
    if sum(int(d.get("COUNT_STAR") or 0) for d in rows) <= have:
        return
    snap["digests"] = [d for d in snap["digests"] if not str(d.get("DIGEST", "")).startswith("prepared:")]
    for d in rows:
        if _own_query(d.get("DIGEST_TEXT")):
            continue
        d = dict(d)
        d["DIGEST"] = "prepared:" + hashlib.md5((d["DIGEST_TEXT"] or "").encode()).hexdigest()[:16]
        d["SCHEMA_NAME"] = d.get("SCHEMA_NAME") or ""
        snap["digests"].append(d)
    snap["prepared_at"] = ts


def _percentile_from_hist(hist_a, hist_b, q):
    if not hist_a or not hist_b or len(hist_a) != len(hist_b):
        return None
    counts = [(hb[0], hb[1] - ha[1]) for ha, hb in zip(hist_a, hist_b)]
    total = sum(c for _, c in counts)
    if total <= 0:
        return None
    need = q * total
    acc, low = 0, 0
    for high, c in counts:
        if c and acc + c >= need:
            # interpolated inside the bucket: performance_schema's buckets are ~4.7% wide, and the bucket edge alone moves
            # in steps (sysbench's own p95 sat at 29.19 ms in six runs of M6) - small changes must show as small changes
            return round((low + (high - low) * (need - acc) / c) / 1e9, 4)  # ps -> ms
        acc += c
        low = high
    return None


def steady_state(samples):
    """Is the measured window in steady state, or is deferred work still piling up? Flushing (dirty pages, checkpoint
    age), purge (history list) and change-buffer merges can be postponed: a window that ends while that debt grows
    never pays it, which flatters settings that defer more. Compares the middle and last third of the window: growth still
    going on at the end. State that settles earlier inside the window is a warm-up warning (warmup_signals), not this."""
    def thirds(key):
        v = [s["db"][key] for s in samples if s.get("db", {}).get(key) is not None]
        if len(v) < 9:
            return None
        n = len(v) // 3
        return sum(v[n:2 * n]) / n, sum(v[-n:]) / n
    growing, ev = [], {}
    # thresholds for one third of the window (half of the earlier first-to-last-third ones: same growth rate)
    for key, label, absmin, rel in (("bp_dirty_pct", "dirty pages %", 1.5, 0.075), ("checkpoint_age_pct", "checkpoint age %", 2.5, 0.075),
                                    ("history_list_length", "history list length", 25000, 0.125), ("ibuf_size", "change buffer pages", 1000, 0.125)):
        t = thirds(key)
        if not t:
            continue
        a, b = t
        ev[key] = [round(a, 1), round(b, 1)]
        if b - a >= absmin and b >= a * (1 + rel):
            growing.append(f"{label} {a:,.1f} -> {b:,.1f}")
    tp = thirds("tps") or thirds("qps")
    if tp and tp[0]:
        drift = 100.0 * (tp[1] - tp[0]) / tp[0]
        ev["throughput_drift_pct"] = round(drift, 1)
        if abs(drift) >= 3:  # falling: debt being paid; rising: still warming up (caches filling, #124 +4-9%)
            growing.append(f"throughput {drift:+.0f}% from the middle to the last third")
    return {"steady": not growing, "growing": growing, "evidence": ev} if ev else None



# deferred work that can grow without bound: (key, label, minimum rise over the window, minimum rise relative to its start)
UNBOUNDED_KEYS = (("history_list_length", "history list length", 50000, 0.10), ("ibuf_size", "change buffer pages", 5000, 0.10))


def _slope(pts):
    """Least-squares growth per second of [(ts, value)]."""
    n = len(pts)
    if n < 3:
        return None
    mt, mv = sum(t for t, _ in pts) / n, sum(v for _, v in pts) / n
    d = sum((t - mt) ** 2 for t, _ in pts)
    return sum((t - mt) * (v - mv) for t, v in pts) / d if d else None


def unbounded_growth(samples, tail=None):
    """Deferred work that keeps growing at an undiminished rate through the window (and the end of the warm-up): not a
    state that a longer warm-up would settle, but a configuration that cannot keep up - e.g. purge falling behind the
    undo records many writers generate. A curve that flattens (checkpoint age, dirty pages) is settling instead."""
    out = []
    for key, label, absmin, rel in UNBOUNDED_KEYS:
        pts = [(s.get("ts") or 0, s["db"][key]) for s in samples if (s.get("db") or {}).get(key) is not None]
        if len(pts) < 12:
            continue
        n = len(pts) // 3
        first, last = _slope(pts[:n]), _slope(pts[-n:])
        rise = pts[-1][1] - pts[0][1]
        if first is None or last is None or last <= 0 or rise < absmin or rise < rel * max(pts[0][1], 1):
            continue
        tp = [(s.get("ts") or 0, s["db"][key]) for s in (tail or []) if (s.get("db") or {}).get(key) is not None]
        before = _slope(tp) if len(tp) >= 3 else None
        ref = max(x for x in (first, before) if x is not None)
        # slowing down clearly AND close to a level: settling, not unbounded. A slowdown that still adds more than a quarter
        # of the level per hour is not settling (session 373 run 21: the history list 1.13M -> 1.29M in the window at ~2M/h,
        # after ~4.5M/h in the warm-up, passed as settling)
        if last < 0.5 * ref and last * 3600 < 0.25 * max(pts[-1][1], 1):
            continue
        out.append({"metric": key, "label": label, "start": round(pts[0][1]), "end": round(pts[-1][1]),
                    "rate_per_s": round(last, 1), "rate_first_third_per_s": round(first, 1),
                    "rate_warmup_tail_per_s": round(before, 1) if before is not None else None,
                    "per_hour": round(last * 3600)})
    return out


# state that a warm-up has to bring to its working level before measuring: (key, label, minimum absolute change,
# minimum change relative to the settled level). Each one moving at the start of the window and settling only inside it
# means the first part of the window measured a different state (redo / dirty pages not yet at their equilibrium: less
# flushing than in the steady state; a cold or filling buffer pool: more reads).
WARMUP_SIGNALS = (("checkpoint_age_pct", "checkpoint age %", 3.0, 0.15), ("bp_dirty_pct", "dirty pages %", 2.0, 0.15),
                  ("bp_free_pct", "free buffer pool pages %", 5.0, 0.0), ("bp_hit_pct", "buffer pool hit %", 0.5, 0.0),
                  ("history_list_length", "history list length", 20000, 0.25), ("ibuf_size", "change buffer pages", 1000, 0.25),
                  ("tps", "throughput", 0.0, 0.05), ("avg_latency_ms", "average latency", 0.0, 0.10))


MEASURE_CANDIDATES_S = (120, 180, 300, 450, 600, 900, 1200, 1800, 2400, 2700, 3600)
WINDOW_CAP_S = 2400       # measured window at most 40 min by default: beyond that more runs buy more than longer ones
WINDOW_MIN_S = 300        # at least 5 min: transients, the CPU / off-CPU profiles and the steady-state checks need room
BETWEEN_CV_PRIOR = 1.0    # % run-to-run CV assumed before calibration measures it (calibrations after a reset: 0.6-1.5%)
OVERHEAD_DEFAULT_S = 120  # reset / restart of a run when the probe could not time it


def best_window(within_var, overhead_s, between_cv, candidates=MEASURE_CANDIDATES_S, cap_s=WINDOW_CAP_S, floor_s=WINDOW_MIN_S):
    """The window W that buys the most precision per hour of benchmarking. A run costs overhead + W; its result varies by
    the within-run part (within_var / W, CV %^2, shrinks with W) plus the between-run part (between_cv^2, does not).
    For a fixed time per configuration the variance of the mean is proportional to (within_var / W + between^2) x
    (overhead + W), smallest at W* = sqrt(within_var x overhead) / between: longer windows past it buy less than more
    runs would (user, 2026-10-25: pop2hc32 HammerDB chose 60 min windows to reach half the target within-run noise).
    Not shorter than floor_s (WINDOW_MIN_S, or several periods of a slow cycle). Returns the best candidate up to cap_s,
    and the cost table [(W, relative cost)]."""
    b2 = max(between_cv, 0.05) ** 2
    floor_s = min(floor_s, cap_s)
    cands = [w for w in candidates if floor_s <= w <= cap_s] or [min(candidates, key=lambda w: abs(w - floor_s))]
    cost = [(w, (within_var / w + b2) * (overhead_s + w)) for w in cands]
    best = min(cost, key=lambda x: x[1])[0]
    lo = min(c for _, c in cost)
    return best, [(w, round(c / lo, 3)) for w, c in cost]


def measure_length(samples, start_ts=None, key="tps", target_pct=3.0, k=2.5, overhead_s=None, between_cv=BETWEEN_CV_PRIOR,
                   cap_s=WINDOW_CAP_S):
    """How long must a measured window be? From the settled part of one long run: the variance of the mean of a window
    of W seconds, estimated by batch means (block sizes 10 s .. 5 min; the largest variance x block length is used, so
    slow cycles - checkpoint or flush waves - count), gives the within-run noise of a W-second run: k x CV(W). The
    recommendation is the window with the most precision per hour (best_window): the run's overhead (reset + warm-up)
    against the run-to-run noise longer windows cannot remove - until calibration measures it, BETWEEN_CV_PRIOR.
    Calibration re-plans once with the measured value (evaluate.noise_plan). Returns None without 5 minutes of samples."""
    pts = [(x.get("ts") or 0, x["db"][key]) for x in samples
           if (x.get("db") or {}).get(key) is not None and (start_ts is None or (x.get("ts") or 0) >= start_ts)]
    if len(pts) < 30:
        return None
    gaps = sorted(pts[i + 1][0] - pts[i][0] for i in range(len(pts) - 1))
    dt = max(gaps[len(gaps) // 2], 1e-6)
    vals = [v for _, v in pts]
    mean = sum(vals) / len(vals)
    if not mean:
        return None
    sig2, block = 0.0, 1
    for b in (1, 2, 3, 6, 9, 12, 18, 30):
        nb = len(vals) // b
        if nb < 6:
            break
        bm = [sum(vals[i * b:(i + 1) * b]) / b for i in range(nb)]
        m_ = sum(bm) / nb
        v_ = sum((x - m_) ** 2 for x in bm) / (nb - 1)
        sig2, block = v_ * b * dt, b  # the largest block size that still leaves 6+ blocks (batch means)
    budget = target_pct / 2
    table = [(w, round(100 * k * (sig2 / w) ** 0.5 / abs(mean), 2)) for w in MEASURE_CANDIDATES_S]
    within_var = 1e4 * sig2 / mean ** 2  # CV %^2 x seconds: a W-second window has CV^2 = within_var / W
    ov = overhead_s if overhead_s is not None else OVERHEAD_DEFAULT_S
    # at least three of the slowest cycle the samples show (a checkpoint or flush wave): a window shorter than a few
    # periods catches the wave in one phase or another
    var1 = sum((v - mean) ** 2 for v in vals) / max(len(vals) - 1, 1)
    corr_s = sig2 / var1 if var1 else dt  # integrated correlation time: ~one sample for plain noise, longer for slow waves
    rec, cost = best_window(within_var, ov, between_cv, cap_s=cap_s, floor_s=max(WINDOW_MIN_S, int(3 * corr_s)))
    return {"recommended_s": rec, "reaches_target": dict(table)[rec] <= budget,
            "predicted_within_noise_pct": dict(table)[rec], "target_pct": target_pct, "budget_within_pct": budget, "table": table,
            "basis_s": int(pts[-1][0] - pts[0][0]), "block_s": int(block * dt), "mean": round(mean, 2),
            "within_var": round(within_var, 1), "correlation_s": int(corr_s), "overhead_s": int(ov), "between_cv_assumed": between_cv, "cap_s": cap_s,
            "cost_table": cost, "rule": "most precision per hour of benchmarking (overhead vs between-run noise)"}


def bp_free_inert(samples):
    """Free buffer pool pages falling while every page the workload needs is already cached: the pool absorbing a growing
    data set, not a warm-up. Hit rate at least 99.5%, disk reads negligible (at most 1 per 100 transactions or 20/s) in
    the last fifth of the samples. Such a slope changes no work, so it does not set a warm-up (pop2hc32 session 335: free
    pages kept falling 38% -> 7% for 40 min at 100% hits and flat throughput, and stretched the warm-up to 70 min)."""
    tail = samples[-max(18, len(samples) // 5):]
    hit = [x["db"]["bp_hit_pct"] for x in tail if (x.get("db") or {}).get("bp_hit_pct") is not None]
    reads = [x["db"].get("bp_disk_reads") or 0 for x in tail if x.get("db")]
    tps = [x["db"].get("tps") or 0 for x in tail if x.get("db")]
    if len(hit) < 9 or not reads:
        return False
    r, t = sum(reads) / len(reads), sum(tps) / len(tps) if tps else 0
    return min(hit) >= 99.5 and r <= max(20.0, 0.01 * t)


def settle_times(samples, t0=None):
    """How long each state metric takes to reach its working level from the start of a benchmark (warmup-probe). For each
    metric: the level of its last 20% (at least 3 minutes), a tolerance (its relative / absolute minimum from
    WARMUP_SIGNALS, or twice the scatter at that level, whichever is larger), and the first moment after which the
    3-minute rolling mean stays within it. Metrics growing without bound are reported apart: no warm-up settles them.
    Returns {"metrics": {key: {...}}, "unbounded": [...], "elapsed_s"}."""
    if not samples:
        return {"metrics": {}, "unbounded": [], "elapsed_s": 0}
    t0 = t0 if t0 is not None else samples[0].get("ts") or 0
    end = samples[-1].get("ts") or t0
    unb = {u["metric"]: u for u in unbounded_growth([x for x in samples if (x.get("ts") or 0) >= t0 + 0.4 * (end - t0)])}
    out = {}
    for key, label, absmin, rel in WARMUP_SIGNALS:
        pts = [(x.get("ts") or 0, x["db"][key]) for x in samples if (x.get("db") or {}).get(key) is not None]
        if len(pts) < 18:
            continue
        tail_n = max(18, len(pts) // 5)
        last = [v for _, v in pts[-tail_n:]]
        level = sum(last) / len(last)
        sd = (sum((v - level) ** 2 for v in last) / max(len(last) - 1, 1)) ** 0.5
        tol = max(absmin, rel * abs(level), 2 * sd, 1e-9)
        w = 18  # 3 minutes of 10 s samples
        sm = [(pts[i][0], sum(v for _, v in pts[max(0, i - w + 1):i + 1]) / len(pts[max(0, i - w + 1):i + 1])) for i in range(len(pts))]
        settled = next((sm[i][0] for i in range(len(sm)) if all(abs(v - level) <= tol for _, v in sm[i:])), None)
        out[key] = {"label": label, "level": round(level, 2), "tolerance": round(tol, 2),
                    "settled_after_s": int(settled - t0) if settled is not None else None,
                    "plateau_s": int(end - settled) if settled is not None else 0, "unbounded": key in unb}
    if "bp_free_pct" in out and bp_free_inert(samples):
        out["bp_free_pct"]["inert"] = True  # reported, but it sets no warm-up (bp_free_inert)
    return {"metrics": out, "unbounded": list(unb.values()), "elapsed_s": int(end - t0)}


def warmup_signals(samples, warmup_s=None, tail=None, skip=()):
    """Was the warm-up long enough? For each state metric: its level in the first minute of the window vs the level it
    settled at (the last third), and how far into the window it got there. A metric still moving at the end is the
    steady-state check's (not steady); this one catches state that settles only inside the window - the window then
    averages two regimes. `tail`: samples of the last part of the warm-up - an adequate warm-up ends in the state the
    window measures, so its last 10-20% should look like the window. Returns {"settling", "tail_vs_window",
    "suggest_warmup_s", "text"} or None; a warning, not a verdict."""
    if len(samples) < 18:
        return None
    t0 = samples[0].get("ts") or 0
    out, differs = [], []
    inert = bp_free_inert(samples)
    for key, label, absmin, rel in WARMUP_SIGNALS:
        if key in skip:  # growing without bound: no warm-up settles it (unbounded_growth reports it)
            continue
        if key == "bp_free_pct" and inert:  # the pool filling with pages it does not need yet: no warm-up issue
            continue
        tv = [s["db"][key] for s in (tail or []) if (s.get("db") or {}).get(key) is not None]
        wv = [s["db"][key] for s in samples if (s.get("db") or {}).get(key) is not None]
        if len(tv) >= 3 and len(wv) >= 9:
            tm, wm = sum(tv) / len(tv), sum(wv) / len(wv)
            if wm != tm and abs(wm - tm) >= absmin and abs(wm - tm) >= rel * abs(wm):
                differs.append({"metric": key, "label": label, "warmup_tail": round(tm, 2), "window": round(wm, 2)})
        pts = [(s.get("ts") or 0, s["db"][key]) for s in samples if (s.get("db") or {}).get(key) is not None]
        if len(pts) < 18:
            continue
        w = 6  # ~1 minute of 10 s samples
        sm = [(pts[i][0], sum(v for _, v in pts[max(0, i - w + 1):i + 1]) / len(pts[max(0, i - w + 1):i + 1])) for i in range(len(pts))]
        n = len(pts) // 3
        plateau = sum(v for _, v in pts[-n:]) / n
        start = sum(v for _, v in pts[:w]) / w
        ch = plateau - start
        if abs(ch) < absmin or abs(ch) < rel * abs(plateau) or not ch:
            continue
        # settled: the smoothed value stays within a quarter of the change from the plateau from then on
        tol = abs(ch) * 0.25
        settled = None
        for i in range(len(sm)):
            if all(abs(v - plateau) <= tol for _, v in sm[i:]):
                settled = sm[i][0]
                break
        if settled is None:
            continue
        after = settled - t0
        span = (pts[-1][0] - t0) or 1
        if after < 0.2 * span:  # settled early in the window: the first minutes barely matter
            continue
        out.append({"metric": key, "label": label, "start": round(start, 2), "settled_at": round(plateau, 2),
                    "settled_after_s": int(after), "window_share_pct": round(100.0 * after / span)})
    if not out and not differs:
        return None
    longest = max([x["settled_after_s"] for x in out] or [0])
    # current warm-up plus the settling time (whole minutes); without a settling time, half as long again
    sug = int(-(-((warmup_s or 0) + (longest or (warmup_s or 0) // 2)) // 60) * 60) if warmup_s else None
    parts = []
    if differs:
        parts.append("the end of the warm-up differs from the window (" + "; ".join(
            "%s %s vs %s" % (x["label"], format(x["warmup_tail"], ",.4g"), format(x["window"], ",.4g")) for x in differs) + ")")
    parts += ["%s %s -> %s, settled %d min into the window" % (x["label"], format(x["start"], ",.4g"), format(x["settled_at"], ",.4g"),
                                                               x["settled_after_s"] // 60) for x in out]
    return {"settling": out, "tail_vs_window": differs, "suggest_warmup_s": sug,
            "text": "Warm-up may be too short: " + "; ".join(parts)
                    + (". A warm-up of about %d s would measure the settled state only." % sug if sug else ".")}


DISK_FULL_ERRORS = {1114: "table is full", 1021: "disk full", 3: "error writing file", 1030: "storage engine error"}


def _errors_and_space(a, b, dt):
    """Errors returned to clients in the window (top 5 by count) and the data directory's free space and growth."""
    out = {}
    ea, eb = a.get("errors"), b.get("errors")
    if ea is not None and eb is not None:
        d = []
        for num, (name, n) in eb.items():
            k = n - (ea.get(num) or [None, 0])[1]
            if k > 0:
                d.append({"number": num, "name": name, "count": k, "per_s": round(k / dt, 2)})
        d.sort(key=lambda x: -x["count"])
        out["client_errors"] = d[:5]
        out["client_errors_per_s"] = round(sum(x["count"] for x in d) / dt, 2)
    fa, fb = a.get("fs"), b.get("fs")
    if fb and fb.get("total"):
        out["datadir_free_gb"] = round(fb["free"] / 2 ** 30, 2)
        out["datadir_free_pct"] = round(100.0 * fb["free"] / fb["total"], 2)
        if fa:
            grow = (fa["free"] - fb["free"]) / dt * 3600  # bytes per hour (positive: filling)
            out["datadir_growth_gb_per_h"] = round(grow / 2 ** 30, 3)
            if grow > 0:
                out["datadir_full_in_h"] = round(fb["free"] / grow, 1)
    return out


STARTUP_MAX_SHARE = 0.10  # at most this share of the window is set aside as the benchmark's start-up


def startup_samples(samples, started_at, window_s):
    """How many leading samples of the window belong to the benchmark's own start-up: a benchmark whose measured phase is
    a new process (warm-up and measurement as two invocations) connects and prepares its threads again inside the window
    (session 299: 1024 sysbench threads, the first 10 s sample 30% low on every run - read as a warm-up still settling).
    Counted only when that process started at or after the window opened, from its start until throughput is back to 90%
    of the window median, at most 10% of the window."""
    if not started_at or not samples or (samples[0].get("ts") or 0) > started_at + 30:
        return 0
    tps = sorted(s["db"]["tps"] for s in samples if (s.get("db") or {}).get("tps") is not None)
    if len(tps) < 6:
        return 0
    med = tps[len(tps) // 2]
    n = 0
    for s in samples:
        if (s.get("ts") or 0) - started_at > STARTUP_MAX_SHARE * window_s:
            break
        if (s.get("ts") or 0) >= started_at and ((s.get("db") or {}).get("tps") or 0) >= 0.9 * med:
            break
        n += 1
    return n if n < len(samples) // 2 else 0


def _tail_ref(tail):
    """The warm-up's last minutes, the state the measured phase should continue: connections and throughput (mean, sd)."""
    tps = [(x.get("db") or {}).get("tps") for x in tail or [] if (x.get("db") or {}).get("tps") is not None]
    con = [(x.get("db") or {}).get("threads_connected") for x in tail or [] if (x.get("db") or {}).get("threads_connected") is not None]
    if len(tps) < 3:
        return None
    m = sum(tps) / len(tps)
    sd = (sum((v - m) ** 2 for v in tps) / (len(tps) - 1)) ** 0.5
    return {"tps": m, "tps_sd": sd, "connected": (sum(con) / len(con)) if con else None}


def startup_end(samples, started_at, tail):
    """Index of the first window sample after the new benchmark process's start-up, judged against the warm-up it should
    continue (user, run 5251: a fixed 90%-of-median rule left out 21 s while connections took ~60 s to recover): its
    connections back to the warm-up's level (98%) and throughput back inside the warm-up's spread (mean - 2 sd, at
    least 95% of it) for two samples in a row. None when it never gets there in the first half of the window."""
    ref = _tail_ref(tail)
    if not ref or not started_at:
        return None
    lo = min(ref["tps"] - 2 * ref["tps_sd"], 0.95 * ref["tps"])
    ok_run, half = 0, samples[len(samples) // 2].get("ts") if samples else None
    for i, x in enumerate(samples):
        d = x.get("db") or {}
        if half is not None and (x.get("ts") or 0) > half:
            return None
        con_ok = ref["connected"] is None or d.get("threads_connected") is None or d["threads_connected"] >= 0.98 * ref["connected"]
        if (x.get("ts") or 0) >= started_at and con_ok and (d.get("tps") or 0) >= lo:
            ok_run += 1
            if ok_run == 2:
                return max(0, i - 1)
        else:
            ok_run = 0
    return None


def startup_ramp_s(samples, started_at, window_s):
    """Seconds from the measured phase's own benchmark start until throughput is back to 90% of the window median - the
    whole ramp, not capped like startup_samples (em-b212x session 337: 1024 sysbench threads reconnecting took ~30 s of
    a 2 min window, 10 s were left out, the rest moved the result 5-7% from run to run). None when not measurable."""
    if not started_at or not samples or (samples[0].get("ts") or 0) > started_at + 30:
        return None
    tps = sorted(s["db"]["tps"] for s in samples if (s.get("db") or {}).get("tps") is not None)
    if len(tps) < 6:
        return None
    med = tps[len(tps) // 2]
    for s in samples:
        t = s.get("ts") or 0
        if t - started_at > 0.5 * window_s:
            return None
        if t >= started_at and ((s.get("db") or {}).get("tps") or 0) >= 0.9 * med:
            return round(max(0.0, t - started_at), 1)
    return None


def analyze_window(a, b, samples, warmup_s=None, warmup_tail=None, bench_started_at=None):
    """Summary + decision inputs for a measurement window between snapshots a and b. bench_started_at: start of the
    benchmark process that runs the measured phase, when it started with the window (its start-up is left out of the
    sample-based analyses; the counters still cover the whole window, as the benchmark's own result does)."""
    dt = max(b["t"] - a["t"], 1e-6)
    ramp = startup_ramp_s(samples, bench_started_at, dt)
    skip = startup_samples(samples, bench_started_at, dt)
    client_restart = None
    if bench_started_at and samples and (samples[0].get("ts") or 0) <= bench_started_at + 30 and warmup_tail:
        # a new benchmark process for the measured phase: its start-up judged against the warm-up it should continue
        end = startup_end(samples, bench_started_at, warmup_tail)
        ref = _tail_ref(warmup_tail)
        if end is not None:
            skip = end
            ramp = round((samples[end]["ts"] or 0) - bench_started_at, 1) if end else ramp
        rest = [(x.get("db") or {}).get("tps") for x in samples[skip if end is not None else len(samples) // 2:] if (x.get("db") or {}).get("tps") is not None]
        if ref and rest:
            gap = (sum(rest) / len(rest) - ref["tps"]) / ref["tps"] * 100 if ref["tps"] else 0
            # two averages compared: their standard errors, not the 10 s samples' spread (5251: 10,737 vs 11,385 tps,
            # -5.7%, samples +-4.8% each)
            n_t = len([x for x in warmup_tail if (x.get("db") or {}).get("tps") is not None])
            m_w = sum(rest) / len(rest)
            sd_w = (sum((v - m_w) ** 2 for v in rest) / max(len(rest) - 1, 1)) ** 0.5
            se = (ref["tps_sd"] ** 2 / max(n_t, 1) + sd_w ** 2 / len(rest)) ** 0.5
            if end is None or gap < -max(2.0, 200 * se / ref["tps"] if ref["tps"] else 0):
                # the restarted client never got back to the warm-up's level: the warm-up did not warm what the window
                # measures (warm-up and measurement as two benchmark processes)
                client_restart = {"recovered": end is not None, "window_vs_warmup_pct": round(gap, 1),
                                  "warmup_tps": round(ref["tps"], 1), "window_tps": round(sum(rest) / len(rest), 1)}
    startup_s = round((samples[skip]["ts"] - samples[0]["ts"]), 1) if skip else None
    samples = samples[skip:]
    # the tail: samples at the window's end where the load had already stopped (a benchmark ending inside the window, the
    # last 10 s sample reaching past its end) - left out of the sample-based analyses like the start-up (user, 2026-10-26)
    tail_s = None
    tv = [(x.get("db") or {}).get("tps") for x in samples]
    cv_ = [(x.get("db") or {}).get("threads_connected") for x in samples]
    known = sorted(v for v in tv if v is not None)
    kc = sorted(v for v in cv_ if v is not None)
    if len(known) >= 6 and len(kc) >= 6:
        # a wind-down, not a stall: throughput down AND the clients disconnecting (a stall keeps its connections and
        # stays in the stall statistics)
        med_, medc = known[len(known) // 2], kc[len(kc) // 2]
        cut = len(samples)
        while cut > len(samples) // 2 and tv[cut - 1] is not None and tv[cut - 1] < 0.5 * med_ \
                and cv_[cut - 1] is not None and cv_[cut - 1] < 0.9 * medc:
            cut -= 1
        if cut < len(samples):
            tail_s = round((samples[-1].get("ts") or 0) - (samples[cut - 1].get("ts") or 0), 1)
            samples = samples[:cut]
    sa, sb = a["status"], b["status"]
    deltas = {}
    for k, v in sb.items():
        x, y = _num(sa.get(k)), _num(v)
        # unchanged counters stay (as 0) for the ones evidence checks read: "it never happened" is evidence too
        if x is not None and y is not None and (y != x or k in ZERO_EVIDENCE):
            deltas[k] = y - x
    rates = DBSampler(None)
    rates.variables = b["variables"]
    db_rates = rates.rates({"t": a["t"], "status": sa, "stmt": a.get("stmt"), "innodb": mysqldb.parse_innodb_status(a.get("innodb_text"))},
                           {"t": b["t"], "status": sb, "stmt": b.get("stmt"), "innodb": mysqldb.parse_innodb_status(b.get("innodb_text"))})
    os_rates = OSCounters.rates(a["os"], b["os"])

    def avg(group, key):
        vals = [s[group][key] for s in samples if key in s.get(group, {})]
        return round(sum(vals) / len(vals), 3) if vals else None

    def mx(group, key):
        vals = [s[group][key] for s in samples if key in s.get(group, {})]
        return max(vals) if vals else None

    def mn(group, key):
        vals = [s[group][key] for s in samples if key in s.get(group, {})]
        return min(vals) if vals else None

    def sd(group, key):
        vals = [s[group][key] for s in samples if key in s.get(group, {})]
        if len(vals) < 3:
            return None
        m = sum(vals) / len(vals)
        return round((sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5, 3)

    def stdev_pct(group, key):
        vals = [s[group][key] for s in samples if key in s.get(group, {})]
        if len(vals) < 3:
            return None
        m = sum(vals) / len(vals)
        if not m:
            return None
        var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
        return round(100.0 * var ** 0.5 / m, 2)

    def batch_se_pct(group, key, block=6):
        """Standard error of the window mean from the run's own samples (batch means of ~60 s blocks, robust to
        short-term autocorrelation): the run-to-run spread this run length would show from within-run noise alone."""
        vals = [s[group][key] for s in samples if s.get(group, {}).get(key) is not None]
        # short windows (canaries: 30-60 s) get smaller blocks rather than no within-run estimate: at least 4 blocks
        # when the samples allow, never fewer than 3
        block = max(2, min(block, len(vals) // 4))  # >= 2 samples per block: adjacent 10 s samples are correlated
        k = len(vals) // block
        if k < 3:
            return None
        means = [sum(vals[i * block:(i + 1) * block]) / block for i in range(k)]
        m = sum(means) / k
        if not m:
            return None
        sd = (sum((x - m) ** 2 for x in means) / (k - 1)) ** 0.5
        return round(100.0 * sd / k ** 0.5 / m, 3)

    summary = {
        "duration_s": round(dt, 1),
        "startup_excluded_s": startup_s,
        "tail_excluded_s": tail_s,
        "client_restart": client_restart,
        "startup_ramp_s": ramp,
        "tps": db_rates.get("tps"),
        "within_se_pct": batch_se_pct("db", "tps") or batch_se_pct("db", "qps"),
        "rows_changed_per_s": avg("db", "rows_changed"),
        "steady_state": steady_state(samples),
        "unbounded_growth": unbounded_growth(samples, warmup_tail) or None,
        "warmup_signals": warmup_signals(samples, warmup_s, warmup_tail, {x["metric"] for x in unbounded_growth(samples, warmup_tail)}),
        "qps": db_rates.get("qps"),
        "handler_commits": db_rates.get("handler_commits"),
        "statements_per_s": db_rates.get("statements"),
        "avg_latency_ms": db_rates.get("avg_latency_ms"),
        "p95_latency_ms": _percentile_from_hist(a.get("hist"), b.get("hist"), 0.95),
        "p99_latency_ms": _percentile_from_hist(a.get("hist"), b.get("hist"), 0.99),
        "bp_hit_pct": db_rates.get("bp_hit_pct"),
        "bp_disk_reads": db_rates.get("bp_disk_reads"),
        "bp_wait_free": db_rates.get("bp_wait_free"),
        "read_ahead_per_s": round((db_rates.get("read_ahead") or 0) + (db_rates.get("read_ahead_rnd") or 0), 2),
        "read_ahead_evicted_per_s": db_rates.get("read_ahead_evicted"),  # user threads waiting for a clean free page (flush-bound)
        "bp_pages_flushed": db_rates.get("bp_pages_flushed"),
        "redo_mbs": db_rates.get("redo_mbs"),
        "data_fsyncs": db_rates.get("data_fsyncs"),
        "row_lock_waits": db_rates.get("row_lock_waits"),
        "row_lock_wait_avg_ms": db_rates.get("row_lock_wait_avg_ms"),
        "tmp_disk_tables": db_rates.get("tmp_disk_tables"),
        "threads_running_avg": avg("db", "threads_running"), "threads_running_max": mx("db", "threads_running"),
        "threads_connected_avg": avg("db", "threads_connected"),
        "checkpoint_age_pct_max": mx("db", "checkpoint_age_pct"),
        "bp_dirty_pct_avg": avg("db", "bp_dirty_pct"),
        "history_list_length_max": mx("db", "history_list_length"),
        "ibuf_size_max": mx("db", "ibuf_size"),
        "oldest_trx_s_max": mx("db", "oldest_trx_s"),
        "tps_cv_pct": stdev_pct("db", "tps"),
        # storage latency, reads and writes apart (window average and the spread of the 10 s samples)
        **{k: os_rates.get(k) for k in ("disk_r_await_ms", "disk_w_await_ms", "disk_flush_await_ms", "disk_flush_per_s")},
        **sample_spread(samples, "os", ("disk_r_await_ms", "disk_w_await_ms", "disk_flush_await_ms")),
        # stability inside the window: throughput dips and stalls (a run can average well and still stall)
        **within_run_stability(samples),
        "cpu_busy": os_rates.get("cpu_busy"),
        "cpu_user": os_rates.get("cpu_user"),
        "cpu_sys": os_rates.get("cpu_sys"),
        "cpu_iowait": os_rates.get("cpu_iowait"),
        "mysqld_cpu_pct": os_rates.get("mysqld_cpu_pct"),
        **{k: os_rates.get(k) for k in ("psi_cpu_some", "psi_memory_some", "psi_memory_full", "psi_io_some", "psi_io_full",
                                         "mysqld_psi_cpu_some", "mysqld_psi_memory_some", "mysqld_psi_memory_full",
                                         "mysqld_psi_io_some", "mysqld_psi_io_full")},
        "psi_io_full_max": mx("os", "psi_io_full"),
        **{k: os_rates.get(k) for k in ("net_listen_overflows", "net_listen_drops", "net_tcp_req_q_full_drop", "net_tcp_timeouts",
                                         "net_retrans_pct", "net_conn_per_s", "net_resets_per_s", "net_time_wait", "net_softnet_drops",
                                         "net_softnet_squeeze", "conntrack_fill_pct", "net_estab")},
        "softirq_max_cpu_pct": avg("os", "softirq_max_cpu_pct"), "softirq_avg_cpu_pct": avg("os", "softirq_avg_cpu_pct"),
        "rtt_avg_ms": avg("os", "rtt_avg_ms"), "rtt_p95_ms": avg("os", "rtt_p95_ms"), "rtt_min_ms": avg("os", "rtt_min_ms"), "mysql_conns_avg": avg("os", "mysql_conns"),
        "psi_memory_some_max": mx("os", "psi_memory_some"),
        "cpu_steal": os_rates.get("cpu_steal"),
        "cpu_steal_max": mx("os", "cpu_steal"),
        # how steady the steal was (spread of the 10 s samples, percentage points): a steady level lowers every run
        # alike, a fluctuating one moves results (user, 2026-10-25)
        "cpu_steal_sd": sd("os", "cpu_steal"),
        # CPU clock and thermal throttling (bare metal mostly): a box clocking down mid-run is an outside effect as steal is
        "cpu_mhz": avg("os", "cpu_mhz"), "cpu_mhz_min": mn("os", "cpu_mhz"), "cpu_mhz_cv_pct": stdev_pct("os", "cpu_mhz"),
        "cpu_throttle_events": os_rates.get("cpu_throttle_events"),
        "load1_avg": avg("os", "load1"),
        "disk_r_iops": os_rates.get("disk_r_iops"),
        "disk_w_iops": os_rates.get("disk_w_iops"),
        "disk_r_mbs": os_rates.get("disk_r_mbs"),
        "disk_w_mbs": os_rates.get("disk_w_mbs"),
        "disk_util": os_rates.get("disk_util"),
        "disk_await_ms": os_rates.get("disk_await_ms"),
        "swap_in": os_rates.get("swap_in"),
        "oom_kills": os_rates.get("oom_kills"),
        "swap_out": os_rates.get("swap_out"),
        "mem_used_pct": os_rates.get("mem_used_pct"),
        "mem_available_mb": os_rates.get("mem_available_mb"),
        "hugepages_total_mb": os_rates.get("hugepages_total_mb"), "hugepages_free_mb": os_rates.get("hugepages_free_mb"),
        # dirty page cache (buffered writes not yet on disk): the build-up vm.dirty_* limits act on. Redo fsync'ed at every
        # commit never gets far; data files without O_DIRECT, binary log with sync_binlog = 0, temporary files can
        "page_cache_dirty_mb_max": mx("os", "page_cache_dirty_mb"),
        "page_cache_dirty_mb_avg": avg("os", "page_cache_dirty_mb"),
        "log_fsyncs_per_s": db_rates.get("log_fsyncs"),
        # NUMA (multi-node hosts only): remote share of allocations, misses, page migrations, the tightest node
        **{k: os_rates.get(k) for k in ("numa_other_node_pct", "numa_miss_per_s", "numa_pages_migrated_per_s", "numa_hint_faults_per_s",
                                         "numa_hint_faults_remote_pct", "numa_node_free_min_mb", "numa_node_free_max_mb") if k in os_rates},
        # database memory footprint incl. what was swapped out (swapping must not look like using less memory)
        "db_mem_mb": (round((os_rates.get("dbproc_pss_mb") or 0) + (os_rates.get("dbproc_swap_pss_mb") or 0)
                            + (os_rates.get("dbproc_hugetlb_mb") or 0), 1)
                      if os_rates.get("dbproc_pss_mb") is not None else None),
        **{k: os_rates.get(k) for k in ("dbproc_procs", "dbproc_rss_mb", "dbproc_vsz_mb", "dbproc_pss_mb", "dbproc_pss_anon_mb",
                                         "dbproc_pss_file_mb", "dbproc_pss_shmem_mb", "dbproc_swap_pss_mb", "dbproc_swap_mb",
                                         "dbproc_cg_mem_mb", "dbproc_cg_swap_mb")},
        # scheduling delay: per-thread rates exist only in the collector daemon's samples; host-wide ones also between the window snapshots
        **{k: avg("os", k) for k in ("dbproc_runq_wait_threads", "dbproc_sched_delay_us", "dbproc_runq_wait_pct", "dbproc_nvcsw_per_s", "dbproc_threads")},
        **{k: os_rates.get(k) if os_rates.get(k) is not None else avg("os", k) for k in ("host_runq_wait_threads", "host_sched_delay_us", "host_runq_wait_pct")},
        "dbproc_sched_delay_us_max": mx("os", "dbproc_sched_delay_us"), "dbproc_runq_wait_pct_max": mx("os", "dbproc_runq_wait_pct"),
        **{k: os_rates.get(k) for k in ("swap_in_mbs", "swap_out_mbs", "swap_used_mb", "swap_total_mb", "inactive_anon_mb",
                                         "dbproc_pgmajfault_per_s", "dbproc_workingset_refault_anon_per_s", "dbproc_zswpin_per_s",
                                         "dbproc_zswpout_per_s")},
        "swap_in_mbs_max": mx("os", "swap_in_mbs"), "swap_used_mb_max": mx("os", "swap_used_mb"),
        "dbproc_pss_mb_max": mx("os", "dbproc_pss_mb"), "dbproc_swap_pss_mb_max": mx("os", "dbproc_swap_pss_mb"),
        "samples": len(samples),
    }
    if not summary["tps"] and summary["qps"]:
        summary["tps_source"] = "none"
    summary = {k: v for k, v in summary.items() if v is not None}
    from .evaluate import add_efficiency
    add_efficiency(summary, db_rates)

    inputs = {
        "status_rates": {k: round(v / dt, 3) for k, v in sorted(deltas.items())},
        "db_rates": db_rates,
        "os_rates": os_rates,
        "data_bytes": b.get("data_bytes"),
        **_errors_and_space(a, b, dt),
        "tables": b.get("tables"),
        "ps_enabled": b.get("ps"),
    }
    # top waits in window
    if a.get("waits") and b.get("waits"):
        wd = []
        for name, (cnt, tm) in b["waits"].items():
            c0, t0 = a["waits"].get(name, (0, 0))
            if tm - t0 > 0:
                wd.append({"event": name, "class": wait_class(name), "count": cnt - c0, "time_s": round((tm - t0) / 1e12, 3),
                           "avg_us": round((tm - t0) / 1e6 / max(cnt - c0, 1), 2)})
        wd.sort(key=lambda r: -r["time_s"])
        # table I/O events contain the file / sync / lock waits nested in them (NESTING_EVENT_ID) plus the row call's CPU:
        # shares are of the real waits only, table I/O gets none (it would count the same time twice)
        total = sum(r["time_s"] for r in wd if r["class"] != "table_io") or 1
        for r in wd:
            r["pct"] = None if r["class"] == "table_io" else round(100 * r["time_s"] / total, 1)
        inputs["top_waits"] = wd[:25]
        by = {}
        for r in wd:
            by[r["class"]] = by.get(r["class"], 0) + r["time_s"]
        inputs["wait_classes"] = {k: round(v, 3) for k, v in sorted(by.items(), key=lambda x: -x[1])}
        inputs["wait_instruments"] = db_wait_coverage(b)
    if a.get("file_io") and b.get("file_io"):
        inputs["file_io"] = file_io_delta(a["file_io"], b["file_io"])
    if b.get("mem"):
        inputs["memory_pfs"] = memory_window(a.get("mem"), b["mem"])
    # top statements in window
    if a.get("digests") is not None and b.get("digests"):
        prev = {(d["SCHEMA_NAME"], d["DIGEST"]): d for d in a["digests"]}
        top = []
        for d in b["digests"]:
            p = prev.get((d["SCHEMA_NAME"], d["DIGEST"]), {})
            if int(d["COUNT_STAR"] or 0) < int(p.get("COUNT_STAR", 0) or 0):
                p = {}  # counters reset (connection closed / table truncated)
            cnt = int(d["COUNT_STAR"] or 0) - int(p.get("COUNT_STAR", 0) or 0)
            if cnt <= 0:
                continue
            tm = int(d["SUM_TIMER_WAIT"] or 0) - int(p.get("SUM_TIMER_WAIT", 0) or 0)

            def dd(k):
                return int(d[k] or 0) - int(p.get(k, 0) or 0)

            prep = d["DIGEST"].startswith("prepared:")
            # a prepared-statement reading taken before the benchmark exited covers less than the whole window
            dt_ = max(1e-6, b["prepared_at"] - a["t"]) if prep and b.get("prepared_at") and b["prepared_at"] > a["t"] else dt
            top.append({
                "schema": d["SCHEMA_NAME"], "digest": d["DIGEST"], "prepared": prep, "query": d["DIGEST_TEXT"],
                "count": cnt, "per_s": round(cnt / dt_, 2), "total_s": round(tm / 1e12 * dt / dt_, 3),
                "avg_ms": round(tm / cnt / 1e9, 3), "lock_ms_avg": round(dd("SUM_LOCK_TIME") / cnt / 1e9, 3),
                "rows_examined_avg": round(dd("SUM_ROWS_EXAMINED") / cnt, 1),
                "rows_sent_avg": round(dd("SUM_ROWS_SENT") / cnt, 1),
                "rows_affected_avg": round(dd("SUM_ROWS_AFFECTED") / cnt, 1),
                "tmp_disk_tables": dd("SUM_CREATED_TMP_DISK_TABLES"),
                "sort_merge_passes": dd("SUM_SORT_MERGE_PASSES"),
                "no_index_used": dd("SUM_NO_INDEX_USED"),
                "full_joins": dd("SUM_SELECT_FULL_JOIN"),
            })
        top.sort(key=lambda r: -r["total_s"])
        tot = sum(r["total_s"] for r in top) or 1
        for r in top:
            r["pct_time"] = round(100.0 * r["total_s"] / tot, 1)
        inputs["top_statements"] = top[:20]
    # innodb metrics deltas
    im = []
    for k, v in (b.get("innodb_metrics") or {}).items():
        d = v - (a.get("innodb_metrics") or {}).get(k, 0)
        if d:
            im.append((k, round(d / dt, 3)))
    inputs["innodb_metrics_rates"] = dict(sorted(im))
    txt = b.get("innodb_text", "")
    inputs["innodb_status"] = {
        "semaphores": mysqldb.section(txt, "SEMAPHORES", 3000),
        "log": mysqldb.section(txt, "LOG", 1500),
        "buffer_pool": mysqldb.section(txt, "BUFFER POOL AND MEMORY", 2000),
        "row_operations": mysqldb.section(txt, "ROW OPERATIONS", 1500),
        "transactions_head": mysqldb.section(txt, "TRANSACTIONS", 1500),
    }
    return summary, inputs
