"""Where the purge threads spend their time (user, 2026-10-31: is purge starved of CPU, blocked on latches, or waiting
for disk reads?). For the InnoDB purge coordinator and workers (performance_schema.threads -> OS thread ids):

  on CPU, run-queue wait          the kernel's per-thread scheduler accounting (/proc/<pid>/task/<tid>/schedstat)
  disk reads                      the thread's own read bytes (/proc/<pid>/task/<tid>/io) and its file I/O wait time
                                  (performance_schema wait/io/file/innodb/%, instrumented by default)
  latches                         its InnoDB mutex / rw-lock wait time (wait/synch/%/innodb/%: off by default, switched on
                                  for the measurement and back)
  purge done                      the InnoDB purge counters (module_purge: records purged, undo pages, invocations)

The rest of the threads' time is idle (waiting for work) or waits nothing above names. Session 439 (100 GB on a 20 GB
pool, change buffering off): 32 purge threads at the highest priority on CPUs of their own changed nothing - the pattern
of purge waiting for leaf page reads, which more CPU cannot fix."""
import os
import time

PURGE_NAMES = ("thread/innodb/srv_purge_thread", "thread/innodb/srv_worker_thread")
PURGE_COUNTERS = ("purge_del_mark_records", "purge_upd_exist_or_extern_records", "purge_undo_log_pages", "purge_invoked")
SYNCH_LIKE = "wait/synch/%innodb%"


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def threads(db):
    """[(thread_id, os tid)] of the purge coordinator and workers."""
    try:
        rows = db.rows("SELECT THREAD_ID, THREAD_OS_ID FROM performance_schema.threads WHERE NAME IN (%s)"
                       % ",".join("'%s'" % n for n in PURGE_NAMES))
    except Exception:
        return []
    return [(int(r[0]), int(r[1])) for r in rows if r[1]]


def begin(db):
    """Switch on what the profile needs that is off by default (InnoDB latch instruments, the purge counters); returns
    what to switch back."""
    undo = {}
    try:
        off = [r[0] for r in db.rows("SELECT NAME FROM performance_schema.setup_instruments WHERE NAME LIKE %s "
                                     "AND (ENABLED='NO' OR TIMED='NO')", (SYNCH_LIKE,))]
        if off:
            db.execute("UPDATE performance_schema.setup_instruments SET ENABLED='YES', TIMED='YES' WHERE NAME LIKE %s "
                       "AND (ENABLED='NO' OR TIMED='NO')", (SYNCH_LIKE,))
            undo["instruments"] = off
    except Exception:
        pass
    try:
        db.execute("SET GLOBAL innodb_monitor_enable = 'module_purge'")
    except Exception:
        pass
    return undo


def end(db, undo):
    names = undo.get("instruments") or []
    for i in range(0, len(names), 200):
        part = names[i:i + 200]
        try:
            db.execute("UPDATE performance_schema.setup_instruments SET ENABLED='NO', TIMED='NO' WHERE NAME IN (%s)"
                       % ",".join(["%s"] * len(part)), tuple(part))
        except Exception:
            pass


def snapshot(db, pid):
    """Counters of the purge threads now."""
    ths = threads(db)
    out = {"t": time.time(), "threads": {}, "counters": {}}
    ids = [t for t, _ in ths]
    waits = {}
    if ids:
        try:
            for tid, ev, sumt in db.rows(
                    "SELECT THREAD_ID, EVENT_NAME, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_by_thread_by_event_name "
                    "WHERE THREAD_ID IN (%s) AND SUM_TIMER_WAIT > 0 AND (EVENT_NAME LIKE 'wait/io/file/innodb/%%%%' OR EVENT_NAME LIKE %%s)"
                    % ",".join(str(x) for x in ids), (SYNCH_LIKE,)):
                w = waits.setdefault(int(tid), {"io_ps": 0, "latch_ps": 0})
                w["io_ps" if str(ev).startswith("wait/io/") else "latch_ps"] += int(sumt or 0)
        except Exception:
            pass
    for thread_id, os_tid in ths:
        st = _read(f"/proc/{pid}/task/{os_tid}/schedstat").split()
        io = dict(l.split(": ", 1) for l in _read(f"/proc/{pid}/task/{os_tid}/io").splitlines() if ": " in l)
        out["threads"][os_tid] = {"cpu_ns": int(st[0]) if len(st) > 1 else None, "runq_ns": int(st[1]) if len(st) > 1 else None,
                                  "read_bytes": int(io.get("read_bytes", 0) or 0), **waits.get(thread_id, {"io_ps": 0, "latch_ps": 0})}
    try:
        for name, cnt in db.rows("SELECT NAME, COUNT FROM information_schema.INNODB_METRICS WHERE NAME IN (%s)"
                                 % ",".join("'%s'" % n for n in PURGE_COUNTERS)):
            out["counters"][name] = int(cnt or 0)
    except Exception:
        pass
    return out


def profile(a, b):
    """Shares (%) of the purge threads' time between two snapshots, read MB/s, records purged per second, and the cause:
    cpu (busy on CPU), cpu-starved (waiting for a CPU), io (waiting for file reads), latch (InnoDB mutexes / rw-locks) or
    None when no one share stands out."""
    if not a or not b:
        return None
    dt = b["t"] - a["t"]
    common = [t for t in b["threads"] if t in a["threads"]]
    if dt <= 0 or not common:
        return None
    wall = dt * 1e9 * len(common)
    def tot(key, scale=1.0):
        s = 0.0
        for t in common:
            x, y = a["threads"][t].get(key), b["threads"][t].get(key)
            if x is not None and y is not None and y >= x:
                s += (y - x) * scale
        return s
    cpu, runq = tot("cpu_ns"), tot("runq_ns")
    io, latch = tot("io_ps", 1e-3), tot("latch_ps", 1e-3)  # picoseconds -> ns
    share = lambda x: round(min(100.0, 100.0 * x / wall), 1)  # noqa: E731
    out = {"threads": len(common), "cpu_pct": share(cpu), "runq_pct": share(runq), "io_pct": share(io), "latch_pct": share(latch),
           "read_mb_s": round(tot("read_bytes") / dt / 2 ** 20, 2)}
    out["other_pct"] = round(max(0.0, 100.0 - out["cpu_pct"] - out["runq_pct"] - out["io_pct"] - out["latch_pct"]), 1)
    ca, cb = a.get("counters") or {}, b.get("counters") or {}
    recs = sum(cb.get(k, 0) - ca.get(k, 0) for k in ("purge_del_mark_records", "purge_upd_exist_or_extern_records")
               if k in ca and k in cb)
    if any(k in ca for k in PURGE_COUNTERS):
        out["purged_records_s"] = round(recs / dt, 1)
        out["undo_pages_s"] = round((cb.get("purge_undo_log_pages", 0) - ca.get("purge_undo_log_pages", 0)) / dt, 1)
    out["cause"] = cause(out)
    return out


def cause(p):
    """The dominant reason the purge threads do not go faster, if one stands out (at least 25% of their time and the
    largest): io, latch, cpu-starved (run queue), cpu (busy computing)."""
    if not p:
        return None
    cands = {"io": p["io_pct"], "latch": p["latch_pct"], "cpu-starved": p["runq_pct"], "cpu": p["cpu_pct"]}
    k, v = max(cands.items(), key=lambda kv: kv[1])
    return k if v >= 25 else None


def text(p):
    if not p:
        return ""
    s = (f"purge threads ({p['threads']}): on CPU {p['cpu_pct']}%, waiting for a CPU {p['runq_pct']}%, file I/O {p['io_pct']}%, "
         f"latches {p['latch_pct']}%, other / idle {p['other_pct']}%; reads {p['read_mb_s']} MB/s")
    if p.get("purged_records_s") is not None:
        s += f"; {p['purged_records_s']:,} records purged/s"
    return s + (f" -> {p['cause']}" if p.get("cause") else "")


def mysqld_pid(db):
    """mysqld's process id, from its pid file."""
    try:
        path = db.scalar("SELECT @@pid_file")
        if path and not os.path.isabs(path):
            path = os.path.join(db.scalar("SELECT @@datadir") or "", path)
        return int(_read(path).strip())
    except Exception:
        return None
