"""One benchmarking command at a time on a database host, and queued experiments started when the host is idle.

Every command that drives the benchmark (autotune, measure run, experiment run, run-length, purge-triage) holds an
exclusive lock on <state_dir>/run.lock for as long as it runs; a second one refuses to start. The daemon uses the same lock
to tell an idle host from a busy one: experiments queued on the web page (or with `tuning-agent experiment add`) after
autotune finished are started by it once the session is open, nothing holds the lock, no measurement is open and no data
load is under way - for IDLE_S (user, 2026-10-28: "it is very natural for a user to wait until everything automatic was
done and then add things through the web, or ask their agent to - it should also work")."""
import contextlib
import fcntl
import json
import os
import time

IDLE_S = 300  # the host has to be idle this long before queued experiments start (a person or agent between two commands)


class Busy(Exception):
    pass


def _path(state_dir):
    return os.path.join(state_dir, "run.lock")


def holder(state_dir):
    """What holds the lock ({pid, what, since}), or None when it is free."""
    p = _path(state_dir)
    try:
        f = open(p, "a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            try:
                return json.loads(f.read() or "{}") or {"what": "unknown"}
            except ValueError:
                return {"what": "unknown"}
        fcntl.flock(f, fcntl.LOCK_UN)
        return None
    finally:
        f.close()


@contextlib.contextmanager
def held(state_dir, what):
    """Hold the run lock while the block runs; Busy when another command has it."""
    os.makedirs(state_dir, exist_ok=True)
    f = open(_path(state_dir), "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            try:
                h = json.loads(f.read() or "{}")
            except ValueError:
                h = {}
            raise Busy(f"another run is in progress on this host: {h.get('what', 'unknown')} (pid {h.get('pid', '?')}, since "
                       f"{time.strftime('%H:%M', time.localtime(h['since'])) if h.get('since') else '?'})")
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"pid": os.getpid(), "what": what, "since": round(time.time(), 1)}))
        f.flush()
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


class AutoStart:
    """The daemon's side: decides when queued experiments may start. tick() -> None, or the reason they start now."""

    def __init__(self, idle_s=IDLE_S):
        self.idle_s = idle_s
        self.free_since = None
        self.tried = {}  # queue signature -> attempts: a queue that does not move after two starts is not started again

    def tick(self, now, *, enabled, session, measuring, lock_holder, loading, queued, bench):
        if not enabled or not session or not queued:
            self.free_since = None
            return None
        if measuring or lock_holder or loading:
            self.free_since = None  # busy: the idle time starts again once it is free
            return None
        if self.free_since is None:
            self.free_since = now
        if now - self.free_since < self.idle_s:
            return None
        if not bench:
            return None  # no benchmark profile recorded for the session: the page says how to run them
        sig = tuple(sorted(e["id"] for e in queued))
        if self.tried.get(sig, 0) >= 2:
            return None
        self.tried[sig] = self.tried.get(sig, 0) + 1
        self.free_since = None
        return f"{len(queued)} queued experiment(s) (#{', #'.join(str(e['id']) for e in queued)}), host idle for {self.idle_s // 60} min"
