"""mysqld crashing or hanging while the agent works with it: noticed, its evidence saved, the hang ended.

User report, session 381 (Percona Server 9.7.2 on a RAM disk): after innodb_log_writer_threads was turned off at runtime
the reset's shutdown never finished - ib_log_checkpointer spinning at 100% CPU in log_checkpointer -> fil_flush_file_spaces
- and the session sat in "resetting data" for an hour until the reset command timed out. Nothing recorded what happened.

A Watch runs beside a measured run (and every stop / start the agent makes) and checks the mysqld service every CHECK_S:
* crash: the main process changed (or vanished) while nobody restarted it - systemd's Restart=on-failure brings mysqld back
  and a benchmark can carry on against a cold server. Evidence: the signal / assertion / backtrace from the error log and
  the journal.
* hung stop or start: in 'deactivating' / 'activating' for STATE_MIN_S without disk or file I/O progress for STALL_S (a slow
  shutdown flushing pages or a crash recovery reads and writes; a spinning or deadlocked one does not).
* hung while running: every ping times out for UNRESPONSIVE_S (refused connections - max_connections - do not count).
For a hang the evidence is taken from the live process - the busiest threads, kernel stacks, a perf profile, gdb / eu-stack
stacks when installed - then mysqld is killed (SIGKILL: InnoDB recovers; the benchmark restores its data anyway), so the
blocked stop / reset returns and the session goes on without the configuration that caused it.
Everything goes to <state_dir>/incidents/<time>-<kind>/ on the host and a summary to the server (event 'incident')."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

CHECK_S = 15
STATE_MIN_S = 300
STALL_S = 180
STALL_IO_BYTES = 8 << 20  # less disk I/O than this, and ...
STALL_FILE_BYTES = 64 << 20  # ... less read()/write() traffic than this over STALL_S: no progress
UNRESPONSIVE_S = 600
PING_S = 60

_cfg = {"report": None, "outdir": "/var/lib/tuning-agent/incidents", "ping": None, "log": print, "context": None,
        "error_log": None, "cnf_path": None}
_current = None


def configure(**kw):
    """report(kind, message, data), outdir, ping() -> True / False (timed out) / None (unknown), log, context() -> dict
    (the session, the run, the last applied changes), error_log path, cnf_path (the managed option file)."""
    _cfg.update({k: v for k, v in kw.items() if v is not None})


def _sh(argv, timeout=30):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except Exception as e:  # noqa
        return f"({argv[0]}: {e})"


def unit(service):
    """(ActiveState, SubState, MainPID) of the service."""
    out = _sh(["systemctl", "show", service, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"], 15)
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    try:
        pid = int(kv.get("MainPID") or 0)
    except ValueError:
        pid = 0
    return kv.get("ActiveState", ""), kv.get("SubState", ""), pid


def io(pid):
    """Bytes the process moved so far: (disk read + write, read() + write() traffic), or None when it is gone."""
    try:
        kv = dict(l.split(": ") for l in open(f"/proc/{pid}/io").read().splitlines())
        return int(kv["read_bytes"]) + int(kv["write_bytes"]), int(kv["rchar"]) + int(kv["wchar"])
    except (OSError, KeyError, ValueError):
        return None


def _threads(pid):
    out = {}
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            try:
                s = open(f"/proc/{pid}/task/{tid}/stat").read()
                rest = s[s.rindex(")") + 2:].split()
                out[int(tid)] = (s[s.index("(") + 1:s.rindex(")")], rest[0], int(rest[11]) + int(rest[12]))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    return out


def busiest_threads(pid, seconds=5, top=12):
    """[{tid, name, state, cpu_pct}] over a short sample, busiest first."""
    a = _threads(pid)
    time.sleep(seconds)
    b = _threads(pid)
    hz = os.sysconf("SC_CLK_TCK")
    rows = [{"tid": t, "name": n, "state": st, "cpu_pct": round(100.0 * (ticks - a[t][2]) / hz / seconds, 1)}
            for t, (n, st, ticks) in b.items() if t in a]
    rows.sort(key=lambda r: -r["cpu_pct"])
    blocked = [r for r in rows if r["state"] == "D"]  # in uninterruptible I/O: as telling as a spinning one
    return rows[:top] + [r for r in blocked if r not in rows[:top]][:top]


def _perf(pid, outdir, seconds=10):
    if not shutil.which("perf"):
        return ""
    data = os.path.join(outdir, "perf.data")
    _sh(["perf", "record", "-g", "-p", str(pid), "-o", data, "--", "sleep", str(seconds)], seconds + 60)
    rep = _sh(["perf", "report", "-i", data, "--stdio", "--no-children", "--percent-limit", "2", "--sort", "comm,symbol"], 120)
    with contextlib.suppress(OSError):
        os.remove(data)  # the report is the evidence; the data file can be large
    return rep


def _perf_top(rep, n=8):
    """The leading 'xx.xx%  comm  [.] symbol' lines of a perf report."""
    out = []
    for l in rep.splitlines():
        m = re.match(r"\s*([\d.]+)%\s+(\S+)\s+\[[.k]\]\s+(.+)", l)
        if m:
            out.append(f"{m.group(1)}% {m.group(2)} {m.group(3).strip()[:90]}")
        if len(out) >= n:
            break
    return out


def _stacks(pid):
    if shutil.which("gdb"):
        return _sh(["gdb", "-p", str(pid), "-batch", "-ex", "set pagination off", "-ex", "thread apply all bt"], 180)
    if shutil.which("eu-stack"):
        return _sh(["eu-stack", "-p", str(pid)], 180)
    return ""


def _error_log(n=300):
    p = _cfg.get("error_log")
    for path in [p, "/var/log/mysql/error.log", "/var/log/mysqld.log", "/var/lib/mysql/error.log"]:
        if path and str(path).startswith("/") and os.path.exists(path):
            try:
                return open(path, errors="replace").read().splitlines()[-n:]
            except OSError:
                continue
    return []


def crash_lines(lines):
    """The last crash report in error log lines: signal / assertion / fatal line and the backtrace after it."""
    start = None
    for i, l in enumerate(lines):
        if re.search(r"got signal \d+|Assertion failure|assertion failed|terminate called|\[FATAL\]|\[ERROR\] \[MY-013183\]", l, re.I):
            start = i
    if start is None:
        return []
    return lines[max(0, start - 3):start + 40]


def journal_exits(service, since):
    """systemd's lines about the service ending abnormally since `since` (epoch seconds): killed by a signal, a core dump, a
    stop / start timeout. An exit code alone (a start refused by a bad option) is no crash."""
    out = _sh(["journalctl", "-u", service, "--no-pager", "--since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))], 30)
    return [l for l in out.splitlines() if re.search(r"code=(killed|dumped)|timed out|Failed with result '(signal|core-dump|timeout|watchdog)'", l, re.I)][-12:]


def collect(kind, phase, pid, service, live, detail=""):
    """Evidence for an incident into its own directory; a summary dict for the server. live: the process can be examined."""
    t = time.time()
    d = os.path.join(_cfg["outdir"], time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"-{kind}")
    os.makedirs(d, exist_ok=True)
    s = {"kind": kind, "phase": phase, "pid": pid, "ts": round(t, 1), "detail": detail, "evidence_dir": d}
    ctx = {}
    with contextlib.suppress(Exception):
        ctx = (_cfg["context"] or (lambda: {}))() or {}
    s.update({k: v for k, v in ctx.items() if k in ("session_id", "run", "last_changes", "server_version")})
    if live and pid:
        s["threads"] = busiest_threads(pid)
        kst = []
        for r in s["threads"][:6]:
            with contextlib.suppress(OSError):
                kst.append(f"--- {r['tid']} {r['name']} ({r['state']}, {r['cpu_pct']}% CPU)\n" + open(f"/proc/{pid}/task/{r['tid']}/stack").read())
        open(os.path.join(d, "threads.txt"), "w").write(json.dumps(s["threads"], indent=1) + "\n\n" + "\n".join(kst))
        rep = _perf(pid, d)
        if rep:
            open(os.path.join(d, "perf-report.txt"), "w").write(rep)
            s["perf_top"] = _perf_top(rep)
        st = _stacks(pid)
        if st:
            open(os.path.join(d, "stacks.txt"), "w").write(st)
            s["stacks_file"] = "stacks.txt"
    el = _error_log()
    open(os.path.join(d, "error.log.tail"), "w").write("\n".join(el) + "\n")
    s["error_log"] = [l[:300] for l in (crash_lines(el) if kind == "crash" else [l for l in el if re.search(r"\[(ERROR|Warning|System)\]", l)][-15:])][:40]
    jr = _sh(["journalctl", "-u", service, "-n", "100", "--no-pager"], 30)
    open(os.path.join(d, "journal.txt"), "w").write(jr)
    s["journal"] = journal_exits(service, t - 3600)
    if _cfg.get("cnf_path") and os.path.exists(_cfg["cnf_path"]):
        with contextlib.suppress(OSError):
            shutil.copy(_cfg["cnf_path"], os.path.join(d, "managed.cnf"))
    open(os.path.join(d, "context.json"), "w").write(json.dumps(ctx, indent=1, default=str))
    s["diagnosis"] = diagnose(s)
    open(os.path.join(d, "summary.json"), "w").write(json.dumps(s, indent=1, default=str))
    return s


def diagnose(s):
    what = {"crash": "mysqld crashed", "hang": "mysqld hung"}[s["kind"]]
    where = {"stopping": "while shutting down", "starting": "while starting (recovery)", "running": "while running",
             "unknown": ""}.get(s["phase"], s["phase"])
    out = f"{what} {where}".strip()
    if s.get("detail"):
        out += f" ({s['detail']})"
    busy = [r for r in s.get("threads") or [] if r["cpu_pct"] >= 50]
    if busy:
        out += "; busiest: " + ", ".join(f"{r['name']} {r['cpu_pct']:.0f}% CPU" for r in busy[:3])
    if s.get("perf_top"):
        out += "; perf: " + "; ".join(s["perf_top"][:3])
    if s["kind"] == "crash":
        first = next((l for l in s.get("error_log") or [] if re.search(r"signal|Assertion|assertion|terminate|FATAL", l)), None)
        if first:
            out += "; error log: " + first.strip()[-200:]
        elif s.get("journal"):
            out += "; systemd: " + s["journal"][-1].strip()[-200:]
    if s.get("last_changes"):
        out += "; last change: " + ", ".join(s["last_changes"])[:300]
    return out


class Watch:
    """Watches the mysqld service in a thread (see the module doc). incident: the first one seen (a dict), else None."""

    def __init__(self, service):
        self.service = service
        self.incident = None
        self.killed = set()
        self._expect = 0
        self._stop = threading.Event()
        self._t = None
        _, _, self.pid = unit(service)
        self._since = None  # when the current stop / start began
        self._io = []  # (time, disk bytes, file bytes) while stopping / starting
        self._unresp = None
        self._last_ping = 0
        self._lock = threading.Lock()

    def start(self):
        self._t = threading.Thread(target=self._loop, name="serverwatch", daemon=True)
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)

    @contextlib.contextmanager
    def expecting(self):
        """The agent itself stops / starts / restarts mysqld in here (a reset, a restart): a new main process is no crash."""
        self._expect += 1
        try:
            yield self
        finally:
            self._expect -= 1
            if not self._expect:
                self.pid = unit(self.service)[2]

    def _loop(self):
        while not self._stop.wait(CHECK_S):
            try:
                self.check()
            except Exception as e:  # noqa - the watch never breaks a run
                _cfg["log"](f"server watch: {type(e).__name__}: {e}")

    def check(self, now=None):
        with self._lock:  # the agent also checks right away when it loses mysqld
            return self._check(now)

    def _check(self, now=None):
        if self.incident:
            return
        now = now or time.time()
        state, sub, pid = unit(self.service)
        if pid != self.pid:
            if self.pid and not self._expect and self.pid not in self.killed:
                ended = self.pid
                self.pid = pid
                return self._found("crash", "running", ended, live=False,
                                   detail=f"main process {ended} ended, " + (f"systemd started {pid}" if pid else "mysqld is down"))
            self.pid = pid
            self._since, self._io, self._unresp = None, [], None
        if state in ("deactivating", "activating") and pid:
            if self._since is None:
                self._since, self._io = now, []
            x = io(pid)
            if x:
                self._io = [p for p in self._io if now - p[0] <= STALL_S + 2 * CHECK_S] + [(now, x[0], x[1])]
            old = [p for p in self._io if now - p[0] >= STALL_S]
            if now - self._since >= STATE_MIN_S and old and x:
                ref = old[-1]
                disk, files = x[0] - ref[1], x[1] - ref[2]
                if disk < STALL_IO_BYTES and files < STALL_FILE_BYTES:
                    phase = "stopping" if state == "deactivating" else "starting"
                    return self._hang(phase, pid, f"{int(now - self._since)} s {phase} ({sub}), {disk >> 20} MiB disk I/O in the last {int(now - ref[0])} s")
            return
        self._since, self._io = None, []
        if state == "active" and pid and not self._expect and _cfg.get("ping") and now - self._last_ping >= PING_S:
            self._last_ping = now
            ok = _cfg["ping"]()
            if ok is False:
                self._unresp = self._unresp or now
                if now - self._unresp >= UNRESPONSIVE_S:
                    return self._hang("running", pid, f"no answer to SELECT 1 for {int(now - self._unresp)} s")
            elif ok:
                self._unresp = None

    def _hang(self, phase, pid, detail):
        _cfg["log"](f"WARNING: mysqld looks hung ({detail}): saving evidence, then killing it")
        s = self._found("hang", phase, pid, live=True, detail=detail)
        self.killed.add(pid)
        _sh(["systemctl", "kill", "-s", "KILL", "--kill-who=main", self.service], 30)
        s["killed"] = True
        return s

    def _found(self, kind, phase, pid, live, detail):
        s = collect(kind, phase, pid, self.service, live, detail)
        self.incident = s
        _cfg["log"](f"INCIDENT: {s['diagnosis']} - evidence in {s['evidence_dir']}")
        if _cfg.get("report"):
            with contextlib.suppress(Exception):
                _cfg["report"]("incident", s["diagnosis"], s)
        return s


@contextlib.contextmanager
def watching(service):
    """A Watch for the duration (the current one when already watching)."""
    global _current
    if _current is not None:
        yield _current
        return
    w = _current = Watch(service).start()
    try:
        yield w
    finally:
        w.stop()
        _current = None


@contextlib.contextmanager
def expected(service):
    """A stop / start / restart the agent makes: watched for hangs (a Watch of its own when none runs), not a crash."""
    with watching(service) as w, w.expecting():
        yield w


def current():
    return _current


def _ts(line):
    m = re.match(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)", line)
    if not m:
        return None
    import calendar
    return calendar.timegm(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S"))  # mysqld logs UTC


def post_mortem(service, since, phase):
    """mysqld found down, or a stop / start failed: a crash since `since` (its report in the error log, or systemd saying
    the process was killed / dumped core) recorded as an incident; None when there is no sign of one."""
    w = _current
    if w and w.incident:
        return w.incident
    lines = crash_lines(_error_log())
    times = [t for t in map(_ts, lines) if t]
    if not (lines and times and max(times) >= since - 5) and not journal_exits(service, since):
        return None
    s = collect("crash", phase, None, service, live=False, detail="found after the fact")
    if w:
        w.incident = s
    _cfg["log"](f"INCIDENT: {s['diagnosis']} - evidence in {s['evidence_dir']}")
    if _cfg.get("report"):
        with contextlib.suppress(Exception):
            _cfg["report"]("incident", s["diagnosis"], s)
    return s
