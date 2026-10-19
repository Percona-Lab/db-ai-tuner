"""Metric sampling and measurement-window analysis."""
import hashlib
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
    "data_writes": ["Innodb_data_writes"],
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
        out["tps"] = round(out.get("commits", 0) + out.get("rollbacks", 0), 2)
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
            d["DIGEST"] = "prepared:" + hashlib.md5((d["DIGEST_TEXT"] or "").encode()).hexdigest()[:16]
            d["SCHEMA_NAME"] = d.get("SCHEMA_NAME") or ""
            snap["digests"].append(d)
        snap["waits"] = db.waits()
        snap["wait_coverage"] = db.wait_coverage()
    snap["data_bytes"], snap["tables"] = db.table_sizes()
    return snap


def db_wait_coverage(snap):
    return snap.get("wait_coverage")


def _own_query(text):
    """Monitoring statements issued by the agent itself."""
    t = (text or "").upper()
    return (t.startswith("SHOW ") or t.startswith("SELECT @@") or "PERFORMANCE_SCHEMA" in t
            or "INFORMATION_SCHEMA" in t or t.startswith("SET GLOBAL"))


def _percentile_from_hist(hist_a, hist_b, q):
    if not hist_a or not hist_b or len(hist_a) != len(hist_b):
        return None
    counts = [(hb[0], hb[1] - ha[1]) for ha, hb in zip(hist_a, hist_b)]
    total = sum(c for _, c in counts)
    if total <= 0:
        return None
    need = q * total
    acc = 0
    for high, c in counts:
        acc += c
        if acc >= need:
            return round(high / 1e9, 4)  # ps -> ms
    return None


def analyze_window(a, b, samples):
    """Summary + decision inputs for a measurement window between snapshots a and b."""
    dt = max(b["t"] - a["t"], 1e-6)
    sa, sb = a["status"], b["status"]
    deltas = {}
    for k, v in sb.items():
        x, y = _num(sa.get(k)), _num(v)
        if x is not None and y is not None and y != x:
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
        "tps": db_rates.get("tps"),
        "within_se_pct": batch_se_pct("db", "tps") or batch_se_pct("db", "qps"),
        "rows_changed_per_s": avg("db", "rows_changed"),
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
        "threads_running_avg": avg("db", "threads_running"),
        "threads_connected_avg": avg("db", "threads_connected"),
        "checkpoint_age_pct_max": mx("db", "checkpoint_age_pct"),
        "bp_dirty_pct_avg": avg("db", "bp_dirty_pct"),
        "history_list_length_max": mx("db", "history_list_length"),
        "tps_cv_pct": stdev_pct("db", "tps"),
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
        "rtt_avg_ms": avg("os", "rtt_avg_ms"), "rtt_p95_ms": avg("os", "rtt_p95_ms"), "mysql_conns_avg": avg("os", "mysql_conns"),
        "psi_memory_some_max": mx("os", "psi_memory_some"),
        "cpu_steal": os_rates.get("cpu_steal"),
        "cpu_steal_max": mx("os", "cpu_steal"),
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
        # database memory footprint incl. what was swapped out (swapping must not look like using less memory)
        "db_mem_mb": (round((os_rates.get("dbproc_pss_mb") or 0) + (os_rates.get("dbproc_swap_pss_mb") or 0), 1)
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
        total = sum(r["time_s"] for r in wd) or 1
        for r in wd:
            r["pct"] = round(100 * r["time_s"] / total, 1)
        inputs["top_waits"] = wd[:25]
        by = {}
        for r in wd:
            by[r["class"]] = by.get(r["class"], 0) + r["time_s"]
        inputs["wait_classes"] = {k: round(v, 3) for k, v in sorted(by.items(), key=lambda x: -x[1])}
        inputs["wait_instruments"] = db_wait_coverage(b)
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

            top.append({
                "schema": d["SCHEMA_NAME"], "digest": d["DIGEST"], "prepared": d["DIGEST"].startswith("prepared:"), "query": d["DIGEST_TEXT"],
                "count": cnt, "per_s": round(cnt / dt, 2), "total_s": round(tm / 1e12, 3),
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
