"""tuning-agent command line interface.

Designed to be driven by people and by coding agents (Claude Code): every command is
non-interactive, supports --json, and exits non-zero on failure.
"""
import argparse
import configparser
import json
import re
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid

from . import __version__, baseline, bench, bottleneck, clientrole, collector, engine, evaluate, hostinfo, llmassist, mechanisms, mysqldb, ostune, planner
from .knowledge import added_tradeoffs, run_tradeoffs, start_tradeoffs, tradeoffs_in
from .applier import Applier
from .knowledge import OPTIONS as KOPTIONS
from .client import APIError, Client
from .knowledge import human_size, normalize
from .state import SampleStore, State

CONFIG_PATH = os.environ.get("TUNING_AGENT_CONFIG", "/etc/tuning-agent/agent.ini")
JSON = False


class Fail(Exception):
    pass


class ServerDown(Fail):
    """mysqld does not come back after the reset (e.g. OOM-killed on start with an oversized buffer pool)."""


CONFIRM_MARGIN = 3.0  # an improvement this many times the noise threshold is kept without a confirmation run
FRUITLESS_STOP = 8  # experiments in a row without a gain: hand over to trade-offs, then stop


def _fruitless_since_gain(sess):
    """Experiment runs after the session's last gain (repeats, confirmations, re-checks and the calibration excluded)."""
    runs = sess.get("runs") or []
    last = max((i for i, r in enumerate(runs) if r.get("verdict") in ("improved", "efficiency-gain", "reference")), default=-1)
    skip = ("baseline", "calibration", "re-check", "best safe", "best with")
    return [r for r in runs[last + 1:] if r.get("verdict") not in ("repeat", "superseded")
            and not str(r.get("label") or "").startswith(skip) and "(confirm" not in str(r.get("label") or "")
            and "(repeat" not in str(r.get("label") or "")]


class TooSlow(Fail):
    pass


class BenchFailed(Fail):
    """The benchmark itself failed under this configuration (exited during warm-up: e.g. mysqld OOM-killed, connections
    not accepted in time). Under an experiment: a failed experiment (reverted), not the end of the session."""


def _oom_killed_since(t0):
    """mysqld OOM-killed by the kernel since t0 (epoch seconds)? The kernel log line, or None."""
    try:
        out = subprocess.run(["journalctl", "-k", "--no-pager", "--since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))],
                             capture_output=True, text=True, timeout=30).stdout
        hits = [l for l in out.splitlines() if re.search(r"Out of memory: Killed process \d+ \(mysqld\)", l)]
        return hits[-1] if hits else None
    except Exception:
        return None


def emit(obj, text=None):
    if JSON:
        print(json.dumps(obj, indent=1, default=str))
    else:
        print(text if text is not None else json.dumps(obj, indent=1, default=str))


def log(msg):
    if not JSON:
        print(msg, flush=True)
    else:
        print(msg, file=sys.stderr, flush=True)


# ---- config / context ---------------------------------------------------------------

def load_cfg():
    cp = configparser.ConfigParser()
    if not cp.read(CONFIG_PATH):
        raise Fail(f"no config at {CONFIG_PATH}: run `tuning-agent init --server URL --api-key KEY` first")
    return dict(cp["agent"])


class Ctx:
    def __init__(self, need_server=True):
        self.cfg = load_cfg()
        self.state = State(self.cfg.get("state_dir", "/var/lib/tuning-agent"))
        self.db = mysqldb.DB(self.cfg)
        self.client = Client(self.cfg["server"], self.cfg["api_key"]) if need_server else None
        self._vars = None
        self._hosts = None
        # stability first unless this session or the agent turned it off (agent.ini stability = off / session --stability off)
        sess = self.state.get("session") or {}
        engine.STABILITY_FIRST = str(sess.get("stability") or self.cfg.get("stability", "first")).strip().lower() not in ("off", "no", "false")

    @property
    def variables(self):
        if self._vars is None:
            self._vars = self.db.variables()
        return self._vars

    def refresh(self):
        self._vars = None
        self._hosts = None

    def os_tuning_allowed(self):
        return str(self.cfg.get("allow_os_tuning", "true")).lower() == "true"

    def os_settings(self):
        ap = Applier(self.cfg, self.state, self.db)
        return ostune.current(self.datadir(), hostinfo.device_for_path(self.datadir()), ap.service, ap.fs_device)

    def client_hosts(self):
        if self._hosts is None:
            try:
                self._hosts = [h for h in self.client.get(f"/environments/{self.env_id()}/hosts").get("hosts", []) if h.get("role") == "client"]
            except Exception:
                self._hosts = []
        return self._hosts

    def all_settings(self):
        v = dict(self.variables)
        v.update(self.os_settings())
        hosts = [h for h in self.client_hosts() if h.get("online")]
        if hosts:  # client settings as client:os:* (from the first online client; clients are tuned together)
            for k, val in ((hosts[0].get("facts") or {}).get("os") or {}).items():
                if k.startswith("os:"):
                    v["client:" + k] = val
        return v

    def datadir(self):
        return (self.variables.get("datadir") or "/var/lib/mysql").rstrip("/")

    def hw(self):
        return hostinfo.hardware(self.datadir())

    def facts(self):
        v = self.variables
        flavor, vt, ver, comment = mysqldb.flavor_of(v)
        data_bytes, tables = self.db.table_sizes()
        keys = ["innodb_buffer_pool_size", "innodb_buffer_pool_instances", "innodb_redo_log_capacity", "innodb_log_file_size",
                "innodb_flush_log_at_trx_commit", "sync_binlog", "log_bin", "innodb_flush_method", "innodb_io_capacity",
                "max_connections", "performance_schema", "thread_handling", "innodb_dedicated_server", "binlog_format",
                "transaction_isolation", "character_set_server", "innodb_page_size", "gtid_mode", "innodb_adaptive_hash_index"]
        try:
            st = self.db.status()
        except Exception:
            st = {}
        eng = self.db.engines()
        return {
            "hardware": self.hw(),
            "db": {"flavor": flavor, "version": ver, "version_comment": comment, "version_tuple": vt,
                   "datadir": self.datadir(), "data_bytes": data_bytes, "tables": tables,
                   # clustering (Galera / Group Replication) and storage engines add settings and change trade-offs
                   "cluster": mysqldb.cluster_of(v, st), "engines": eng, "primary_engine": primary_engine(eng),
                   "key_variables": {k: v[k] for k in keys if k in v}},
            "os": ostune.facts(self.datadir(), hostinfo.device_for_path(self.datadir()), Applier(self.cfg, self.state, self.db).service),
            "agent": {"version": __version__, "cnf_path": Applier(self.cfg, self.state, self.db).cnf_path,
                      "os_tuning": self.os_tuning_allowed(),
                      "service": self.cfg.get("service_name") or None,
                      "managed": {k: v["value"] for k, v in self.state.get("managed", {}).items()},
                      "allow_restart": self.cfg.get("allow_restart", "true") == "true"},
        }

    def hello(self):
        if not self.state.get("agent_uid"):
            self.state.set("agent_uid", str(uuid.uuid4()))
        f = self.facts()
        r = self.client.post("/hello", {"agent_uid": self.state["agent_uid"], "name": self.cfg.get("name") or f["hardware"]["hostname"],
                                        "agent_version": __version__, "facts": f})
        if self.state.get("environment_id") != r["environment_id"]:
            self.state.set("environment_id", r["environment_id"])
        return r, f

    def env_id(self):
        if not self.state.get("environment_id"):
            self.hello()
        return self.state["environment_id"]

    def activity(self, phase, detail=None, seconds=None):
        """What the agent is doing right now (shown next to the active session). Best effort, one quick attempt."""
        if not self.client:
            return
        old = self.client.timeout
        try:
            self.client.timeout = 5
            self.client._call("POST", "/activity", {"environment_id": self.env_id(), "session_id": (self.state.get("session") or {}).get("id"),
                                                    "phase": phase, "detail": detail, "until": time.time() + seconds if seconds else None})
        except Exception:
            pass
        finally:
            self.client.timeout = old

    def event(self, kind, message, data=None):
        try:
            self.client.post("/events", {"environment_id": self.env_id(), "session_id": (self.state.get("session") or {}).get("id"),
                                         "type": kind, "message": message, "data": data or {}})
        except APIError as e:
            log(f"warning: could not record event: {e}")

    def session(self, required=True):
        s = self.state.get("session")
        if required and not s:
            raise Fail("no active tuning session: run `tuning-agent session start --goal tps|latency`")
        return s


# ---- commands -----------------------------------------------------------------------

def cmd_init(a):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH)
    if "agent" not in cp:
        cp["agent"] = {}
    sec = cp["agent"]
    for k in ("server", "api_key", "name", "mysql_user", "mysql_password", "mysql_host", "mysql_port", "mysql_socket",
              "cnf_path", "service_name", "state_dir"):
        val = getattr(a, k, None)
        if val is not None:
            sec[k] = str(val)
    sec.setdefault("mysql_user", "root")
    if "mysql_socket" not in sec and "mysql_host" not in sec:
        for p in ("/var/run/mysqld/mysqld.sock", "/var/lib/mysql/mysql.sock", "/run/mysqld/mysqld.sock", "/tmp/mysql.sock"):
            if os.path.exists(p):
                sec["mysql_socket"] = p
                break
    sec.setdefault("allow_restart", "true" if not a.no_restart else "false")
    if a.no_os_tuning:
        sec["allow_os_tuning"] = "false"
    if a.fs_device:
        sec["fs_device"] = a.fs_device
    if a.allow_fs_experiments:
        sec["allow_fs_experiments"] = "true"
    sec.setdefault("allow_os_tuning", "true")
    sec.setdefault("interval", "10")
    if "server" not in sec or "api_key" not in sec:
        raise Fail("--server and --api-key are required on first init")
    if a.role == "client":
        if not a.environment:
            raise Fail("--role client needs --environment <id of the database environment it talks to>")
        sec["role"], sec["environment_id"] = "client", str(a.environment)
    with open(CONFIG_PATH, "w") as f:
        cp.write(f)
    os.chmod(CONFIG_PATH, 0o600)
    if sec.get("role") == "client":
        c = Ctx()
        r = clientrole.hello(c)
        emit({"ok": True, "host_id": r["host_id"], "environment_id": r["environment_id"]},
             f"Client host #{r['host_id']} linked to environment #{r['environment_id']}. Start the daemon: systemctl enable --now tuning-agent")
        return
    c = Ctx()
    c.db.connect()
    r, f = c.hello()
    c.event("agent", "agent initialised", {"agent_version": __version__})
    emit({"ok": True, "environment_id": r["environment_id"], "environment_url": r.get("url"), "facts": f},
         f"Connected. Environment #{r['environment_id']}: {r.get('url', '')}\n"
         f"{f['db']['version_comment']} {f['db']['version']} on {f['hardware']['cpus']} CPUs / {human_size(f['hardware']['ram_bytes'])} RAM")


def cmd_facts(a):
    c = Ctx(need_server=False)
    f = c.facts()
    hw, db = f["hardware"], f["db"]
    emit(f, "\n".join([
        f"Host:     {hw['hostname']}  {hw['os']}  kernel {hw['kernel']}",
        f"CPU:      {hw['cpus']} x {hw['cpu_model']}",
        f"RAM:      {human_size(hw['ram_bytes'])}  swap {human_size(hw['swap_bytes'])}",
        f"Disk:     {hw['data_device']} ({'HDD' if hw['disk_rotational'] else 'SSD'}) {hw['filesystem']} free {human_size(hw['disk_free_bytes'])} of {human_size(hw['disk_total_bytes'])}",
        f"Database: {db['version_comment']} {db['version']} ({db['flavor']}), data {human_size(db['data_bytes'] or 0)} in {db['tables']} InnoDB tables",
    ]))


def cmd_status(a):
    c = Ctx(need_server=False)
    st = c.state
    store = SampleStore(c.state.dir)
    last = store.latest_ts()
    sess = st.get("session")
    meas = st.get("measurement")
    runs = (sess or {}).get("runs", [])
    out = {
        "environment_id": st.get("environment_id"),
        "server": c.cfg.get("server"),
        "daemon_running": bool(last and time.time() - last < 3 * int(c.cfg.get("interval", 10))),
        "last_sample_age_s": round(time.time() - last, 1) if last else None,
        "session": {k: sess.get(k) for k in ("id", "name", "goal", "policy", "noise_pct", "started")} if sess else None,
        "measurement_in_progress": meas,
        "runs": [{k: r.get(k) for k in ("seq", "id", "label", "goal_value", "delta_pct", "delta_vs_first_pct", "verdict", "changes")} for r in runs],
        "best_run": (sess or {}).get("best"),
        "pending_changes": st.get("pending_changes"),
        "managed_settings": {k: v["value"] for k, v in st.get("managed", {}).items()},
    }
    lines = [f"Environment #{out['environment_id']}  daemon: {'running' if out['daemon_running'] else 'NOT RUNNING'}"]
    if sess:
        lines.append(f"Session #{sess['id']} '{sess.get('name')}' goal={sess['goal']} policy={sess['policy']}")
        for r in runs:
            gv = r.get("goal_value")
            lines.append(f"  run {r['seq']:>2} {r.get('label', ''):<24} {gv if gv is not None else '-':>12}  "
                         f"{(str(round(r['delta_pct'], 1)) + '%') if r.get('delta_pct') is not None else '':>8}  {r.get('verdict') or ''}")
    else:
        lines.append("No active session.")
    if meas:
        lines.append(f"Measurement in progress since {time.strftime('%H:%M:%S', time.localtime(meas['started']))} (run {meas['seq']})")
    if out["pending_changes"]:
        lines.append("Pending (applied, not yet measured): " + ", ".join(f"{p['variable']}={p['new']}" for p in out["pending_changes"]))
    emit(out, "\n".join(lines))


def _maintenance_tasks(c):
    """Background jobs that can run during measurements (OtterTune lesson: a nightly statistics job skewed results)."""
    import subprocess as sp
    out = []
    r = sp.run(["systemctl", "list-timers", "--no-legend", "--no-pager"], capture_output=True, text=True)
    for unit in ("apt-daily", "apt-daily-upgrade", "unattended-upgrades", "man-db", "logrotate", "fstrim"):
        if unit in r.stdout:
            out.append(f"timer {unit}")
    if sp.run(["systemctl", "is-active", "unattended-upgrades"], capture_output=True, text=True).stdout.strip() == "active":
        out.append("unattended-upgrades service (can restart mysqld)")
    try:
        n = c.db.scalar("SELECT COUNT(*) FROM information_schema.EVENTS WHERE STATUS='ENABLED'")
        if n:
            out.append(f"{n} MySQL scheduled event(s)")
    except Exception:
        pass
    return out


def _finish_pending_restart(c):
    """A restart deferred to a data reset that never came (the loop ended): restart now so the server runs what the
    option file says."""
    if c.state.get("restart_pending"):
        log("== applying the deferred restart")
        Applier(c.cfg, c.state, c.db, log=log).restart()
        c.state.set("restart_pending", False)
        c.db.connect(retries=30, delay=2)


def _reset_restarts(c):
    """The session's benchmark restores the data set (stop, copy, start mysqld) before every run."""
    try:
        prof = (c.state.get("session") or {}).get("bench")
        return bool(prof) and "reset_datadir" in str(bench.Profile(prof).reset or "")
    except Exception:
        return False


PAIRS = (("innodb_io_capacity", "innodb_io_capacity_max"),)


def _order_pairs(changes, current=None):
    """A value and its ceiling set live: the ceiling goes first when the new ceiling still covers the current value
    (raising), otherwise the value goes first (lowering) - else InnoDB clamps the value to the old ceiling (#125 run 8
    measured io_capacity 4000 while 8000 was requested)."""
    out = list(changes)
    for base, mx in PAIRS:
        b = next((ch for ch in out if ch["variable"] == base), None)
        m = next((ch for ch in out if ch["variable"] == mx), None)
        if b is None or m is None:
            continue
        try:
            cur_base = float((current or {}).get(base) or 0)
            ceiling_first = float(m.get("value") or 0) >= cur_base
        except (TypeError, ValueError):
            continue
        out = [ch for ch in out if ch is not b and ch is not m] + ([m, b] if ceiling_first else [b, m])
    return out


def _config_mismatches(c):
    """Managed settings whose running value differs from the configured one: [{variable, configured, running}]."""
    out = []
    try:
        now_vars = c.db.variables()
        for var, m in (c.state.get("managed") or {}).items():
            if ":" in var or var not in now_vars or var in KOPTIONS:
                continue
            if normalize(var, now_vars.get(var)) != normalize(var, m.get("value")):
                out.append({"variable": var, "configured": m.get("value"), "running": now_vars.get(var)})
                log(f"WARNING: {var} is {now_vars.get(var)}, configured {m.get('value')} - the run does not measure the configuration")
    except Exception:
        pass
    return out


def _bp_companions(vs, cur, source):
    """Changes that keep the buffer pool at the intended size for the intended number of instances: a chunk size
    dividing size / instances (MiB granularity, at most 128 MiB), and the size itself rounded down to whole MiB per
    instance if needed. [] when the server would already keep the size exact."""
    from .knowledge import parse_size
    MiB_ = 1024 ** 2
    def val(k, d):
        if k in vs and vs[k].get("value") is not None:
            return int(parse_size(str(vs[k]["value"])))
        return int(float(cur.get(k) or d))
    size, inst = val("innodb_buffer_pool_size", 128 * MiB_), max(1, val("innodb_buffer_pool_instances", 1))
    chunk = val("innodb_buffer_pool_chunk_size", 128 * MiB_)
    if size < 1024 ** 3:
        inst = 1  # MySQL uses one instance below 1 GiB
    if size % (chunk * inst) == 0:
        return []
    out = []
    per = size // inst // MiB_  # MiB per instance
    # chunks of 32-128 MiB (tiny chunks mean thousands of them); if none divides the size per instance, the size goes down
    # to whole 128 MiB chunks per instance (at most instances x 128 MiB less) and says so
    best = next((d for d in range(128, 31, -1) if per % d == 0), None)
    if best is None:
        per, best = per // 128 * 128, 128
    if size != per * inst * MiB_:
        size = per * inst * MiB_
        out.append({"variable": "innodb_buffer_pool_size", "value": str(size), "source": source,
                    "reason": f"companion: {per} MiB per instance ({inst} instances) so the size is exactly what is measured"})
    out.append({"variable": "innodb_buffer_pool_chunk_size", "value": f"{best}M", "source": source,
                "reason": f"companion: buffer pool size = {per} MiB x {inst} instances exactly (MySQL rounds the size up to a "
                          f"multiple of chunk x instances: {chunk // MiB_} MiB chunks would change it)"})
    return out


def _apply_and_record(c, changes, source, reason_default="", restart=None):
    allow = c.cfg.get("allow_restart", "true").lower() == "true" if restart is None else restart
    defer = source in ("autotune", "rollback") or str(source).startswith("exp:")
    for ch in changes:
        ch.setdefault("source", source)
        ch.setdefault("reason", reason_default)
    client_changes = [ch for ch in changes if ch["variable"].startswith("client:")]
    changes = [ch for ch in changes if not ch["variable"].startswith("client:")]
    # InnoDB silently caps innodb_io_capacity at innodb_io_capacity_max: raise the max together so the value is really measured
    vs = {ch["variable"]: ch for ch in changes}
    if "innodb_io_capacity" in vs and "innodb_io_capacity_max" not in vs:
        try:
            want = int(float(vs["innodb_io_capacity"]["value"]))
            if want > int(c.db.variables().get("innodb_io_capacity_max") or 0):
                changes.append({"variable": "innodb_io_capacity_max", "value": str(want * 2), "source": source,
                                "reason": "companion: InnoDB caps innodb_io_capacity at innodb_io_capacity_max"})
        except Exception:
            pass
    # MySQL rounds innodb_buffer_pool_size up to a multiple of chunk_size x instances: 32 instances turned a 45 GB pool
    # into 48 GB (Scaleway sweep: the "win" was the larger pool). Keep the intended size exact with a chunk size that
    # divides it (45G / 32 = 1440M -> 120M chunks); a size no chunk can fit is rounded down by at most instances x 1 MiB.
    vs = {ch["variable"]: ch for ch in changes}
    if "innodb_buffer_pool_instances" in vs or "innodb_buffer_pool_size" in vs or "innodb_buffer_pool_chunk_size" in vs:
        try:
            cur = c.db.variables()
            comp = _bp_companions(vs, cur, source)
            if comp:
                changes = [ch for ch in changes if ch["variable"] not in {x["variable"] for x in comp}] + comp
        except Exception:
            pass
    try:
        changes = _order_pairs(changes, c.db.variables())
    except Exception:
        changes = _order_pairs(changes)
    ap = Applier(c.cfg, c.state, c.db, log=log, activity=c.activity)
    try:
        res = ap.apply(changes, allow_restart=allow, defer_restart=defer and _reset_restarts(c)) if changes else {
                                                                      "applied": [], "restarted": False, "needs_restart": [],
                                                                      "mismatches": [], "errors": [], "cnf_path": ap.cnf_path}
    except Exception as e:
        # a change that cannot be applied (e.g. huge pages: not enough contiguous memory) is a failed experiment, not a
        # crashed session: the applier already restored the option file, the OS settings of this batch and mysqld
        log(f"   apply failed: {type(e).__name__}: {e}")
        try:
            c.db.connect(retries=30, delay=2)
        except Exception:
            pass
        c.event("apply", f"{source}: apply failed and was rolled back: {str(e)[:300]}")
        return {"applied": [], "restarted": False, "needs_restart": [], "mismatches": [], "errors": [f"{type(e).__name__}: {e}"],
                "rolled_back": True, "cnf_path": ap.cnf_path}
    if res.get("restart_deferred"):
        c.state.set("restart_pending", True)  # cleared by the next data reset; finish_pending_restart() otherwise
    hp = next((ch for ch in changes if ch["variable"] == "os:hugepages"), None)
    if hp and int(float(hp.get("value") or 0)) > 0 and not res.get("restart_deferred") and res.get("restarted"):
        # InnoDB falls back to normal memory when it cannot use the reserved pages (memlock limit, too few pages): the
        # pool then lives twice - pinned and unused, plus in normal memory - and the host runs out of memory
        mi = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("HugePages_")}
        tot, free = mi.get("HugePages_Total", 0), mi.get("HugePages_Free", 0)
        if tot and free > 0.5 * tot:
            log(f"   huge pages not used by mysqld ({free} of {tot} still free): rolling back")
            undo = [{"variable": "os:hugepages", "value": "0", "source": source, "reason": "huge pages not used by mysqld"}]
            if any(ch["variable"] == "large_pages" for ch in changes):
                undo.append({"variable": "large_pages", "value": "OFF", "source": source, "reason": "huge pages not used by mysqld"})
            Applier(c.cfg, c.state, c.db, log=log).apply(undo, allow_restart=True)
            c.event("apply", f"{source}: huge pages reserved but not used by mysqld ({free}/{tot} free): rolled back")
            return {"applied": [], "restarted": True, "needs_restart": [], "mismatches": [], "errors": ["huge pages not used by mysqld"],
                    "rolled_back": True, "cnf_path": res.get("cnf_path")}
    if client_changes and not res.get("rolled_back"):
        _apply_clients(c, client_changes, res)
    if res["applied"]:
        pend = c.state.get("pending_changes") or []
        by = {p["variable"]: p for p in pend}
        for e in res["applied"]:
            if e["variable"] in by:   # changed again before measuring: keep the original old value
                by[e["variable"]]["new"] = e["new"]
                by[e["variable"]]["reason"] = e["reason"]
            else:
                by[e["variable"]] = e
        pend = [p for p in by.values() if normalize(p["variable"], p["old"]) != normalize(p["variable"], p["new"])]
        c.state.set("pending_changes", pend)
    try:
        c.refresh()
        c.hello()
    except Exception as e:  # noqa
        log(f"warning: could not refresh environment facts: {e}")
    c.event("apply", apply_summary(source, res, changes), {"result": res, "requested": changes})
    return res


def apply_summary(source, res, requested=None):
    """'stability-triage: innodb_purge_batch_size 300 -> 1000 (purge triage: purge batch size 1000)' - what was set, not
    a count (user: 'applied 1 change(s)' said nothing about what was tried)."""
    app = res.get("applied") or []
    parts = [f"{x['variable']} {x.get('old') if x.get('old') not in (None, '') else '(default)'} -> "
             f"{x.get('new') if x.get('new') is not None else '(default)'}" for x in app[:4]]
    more = f" and {len(app) - 4} more" if len(app) > 4 else ""
    why = next((x.get("reason") for x in app if x.get("reason")), None) or next((x.get("reason") for x in requested or [] if x.get("reason")), None)
    if not app:
        txt = "no change (already set)" if not res.get("errors") else "nothing applied: " + "; ".join(res["errors"])[:200]
    else:
        txt = ", ".join(parts) + more
    return (f"{source}: {txt}" + (" (restart)" if res.get("restarted") else "") + (f" - {why}" if why and app else ""))[:500]


def _apply_clients(c, changes, res, timeout=180):
    """Send OS changes to every online client host of the environment and wait for their results."""
    hosts = [h for h in c.client_hosts() if h.get("online")]
    if not hosts:
        res["errors"].append("client settings requested but no client host is online")
        return
    payload = {"changes": [{"variable": ch["variable"][len("client:"):], "value": ch.get("value"), "reason": ch.get("reason"),
                            "source": ch.get("source")} for ch in changes]}
    sid = (c.state.get("session") or {}).get("id")
    pending = {c.client.post(f"/hosts/{h['id']}/commands", {"action": "os_apply", "payload": payload, "session_id": sid})["command_id"]: h
               for h in hosts}
    deadline, results = time.time() + timeout, {}
    while pending and time.time() < deadline:
        for cid in list(pending):
            r = c.client.get(f"/commands/{cid}")
            if r["status"] in ("done", "failed"):
                results[pending.pop(cid)["name"]] = r.get("result") or {}
        if pending:
            time.sleep(2)
    for name in [h["name"] for h in pending.values()]:
        res["errors"].append(f"client {name}: no answer within {timeout}s")
    managed = c.state.data.setdefault("managed", {})
    for name, r in results.items():
        for e in r.get("errors") or []:
            res["errors"].append(f"client {name}: {e}")
    first = next(iter(results.values()), {})
    for a in first.get("applied") or []:
        key = "client:" + a["variable"]
        res["applied"].append({"variable": key, "old": a.get("old"), "new": a.get("new"), "reason": "", "source": "client"})
    for ch in changes:
        if ch.get("value") is None:
            managed.pop(ch["variable"], None)
        elif not any(e for e in res["errors"] if ch["variable"][len("client:"):] in e):
            managed[ch["variable"]] = {"value": str(ch["value"]), "reason": ch.get("reason", ""), "source": ch.get("source", "")}
    c.state.save()
    c.refresh()


def _print_apply(res):
    lines = []
    for e in res["applied"]:
        lines.append(f"  {e['variable']}: {e['old']} -> {e['new']}")
    if res.get("restarted"):
        lines.append("  (server restarted)")
    if res.get("needs_restart"):
        lines.append("  restart required for: " + ", ".join(res["needs_restart"]) + " (restart disabled)")
    for m in res.get("mismatches", []):
        lines.append(f"  WARNING {m['variable']}: wanted {m['wanted']}, server reports {m['actual']}")
    for e in res.get("errors", []):
        lines.append(f"  ERROR {e}")
    if not res["applied"] and not res.get("errors"):
        lines.append("  nothing to change")
    return "\n".join(lines)


def _client_reserve(c):
    """Memory declared for a co-located benchmark client at baseline time (--reserve-mem), in bytes."""
    return int(c.state.get("client_reserve") or 0)


def _measured_overhead(c):
    """mysqld's memory beyond the buffer pool in the latest run on this host (PSS + swapped-out), in bytes, or None."""
    try:
        sess = c.session() or {}
    except Exception:
        return None
    for r in reversed(sess.get("runs") or []):
        s = r.get("summary") or {}
        pss = s.get("dbproc_pss_mb")
        bp = ((r.get("managed") or {}).get("innodb_buffer_pool_size") or {})
        bp = bp.get("value") if isinstance(bp, dict) else bp
        try:
            from .knowledge import parse_size
            bpb = parse_size(str(bp)) if bp else int(c.variables.get("innodb_buffer_pool_size") or 0)
        except Exception:
            continue
        if pss and bpb:
            return max(0, int((pss + (s.get("dbproc_swap_mb") or 0)) * 2 ** 20) - int(bpb))
    return None


def _cap_nice_prep(c):
    """CAP_SYS_NICE for mysqld as host preparation: it only permits thread priorities (no effect on performance by
    itself), and without it the purge triage cannot try a higher purge priority online - only a restart experiment
    later (canary 2026-10-25). When OS tuning is allowed, resource groups exist and mysqld lacks it; else None."""
    from . import resgroups
    sess = c.session() or {}
    if not sess.get("os_tuning", c.os_tuning_allowed()) or not engine.STABILITY_FIRST:
        return None
    try:
        if not resgroups.available(c.db) or resgroups.has_cap_sys_nice() is not False:
            return None
        if "os:mysqld_cap_sys_nice" not in (c.os_settings() or {}):
            return None
    except Exception:
        return None
    return {"variable": "os:mysqld_cap_sys_nice", "value": "ON",
            "reason": "host preparation: lets resource groups raise thread priorities (the purge threads when the undo history "
                      "grows without bound); no effect until a priority is set"}


def _baseline_swap(c, hw):
    """Swap as part of the baseline (best practice): unused swap costs nothing (swap study, 2026-10-21), and without it
    memory pressure is an OOM kill instead of slower pages, so memory settings cannot be explored safely (r2 RO-4.ns, 294,
    299). About 1x RAM (at most 64 GiB, at most half the free disk), before calibration so every run has it. Not when OS
    tuning is off or the host is configured without swap (agent.ini swap = none): then the session runs without, flagged."""
    GiB = 1024 ** 3
    sess = c.session() or {}
    if not sess.get("os_tuning", c.os_tuning_allowed()) or str(c.cfg.get("swap", "allowed")).strip().lower() in ("none", "no", "off", "false"):
        return None
    ram, have = int(hw.get("ram_bytes") or 0), int(hw.get("swap_bytes") or 0)
    if ram < 2 * GiB or have >= engine.swap_target(ram) * 0.9:
        return None
    try:
        osv = c.os_settings() or {}
    except Exception:
        return None
    if "os:swapfile" not in osv:
        return None
    own = engine._swapfile_bytes(osv.get("os:swapfile"))
    size = (engine.swap_target(ram) - (have - own)) // GiB * GiB
    free = int(hw.get("disk_free_bytes") or 0)
    if free:
        size = min(size, max(0, int((free - 2 * GiB) * 0.5)) // GiB * GiB)
    if size < GiB or size <= own:
        return None
    return {"variable": "os:swapfile", "value": engine.fmt_size(size),
            "reason": f"Best practice: a {human_size(size)} swap file ({human_size(have)} swap now, {human_size(ram)} RAM). Unused swap "
                      "costs nothing; with it, memory pressure shows as swapping (measured) instead of an OOM kill."}


def cmd_baseline(a):
    c = Ctx()
    v = c.variables
    flavor, vt, ver, _ = mysqldb.flavor_of(v)
    hw = c.hw()
    opts = {"max_connections": a.max_connections}
    if a.reserve_mem:
        from .knowledge import parse_size
        opts["reserve_bytes"] = parse_size(a.reserve_mem)
    if a.apply:
        c.state.set("client_reserve", int(opts.get("reserve_bytes") or 0))
    mo = _measured_overhead(c)
    if mo:
        opts["measured_overhead_bytes"] = mo
    try:
        opts["engines"] = c.db.engines()  # memory split between InnoDB and MyRocks by the data each holds
    except Exception:
        pass
    opts["host_other_bytes"] = _host_other_now()
    items, calc = baseline.generate(hw, v, flavor, vt, opts)
    sw = _baseline_swap(c, hw)
    if sw:
        items.append(sw)
    cn = _cap_nice_prep(c)
    if cn:
        items.append(cn)
    osv = None
    for it in items:
        if it["variable"].startswith("os:"):
            osv = osv if osv is not None else (c.os_settings() or {})
            it["current"] = osv.get(it["variable"])
            it["change"] = True
            continue
        it["current"] = v.get(it["variable"])
        it["change"] = normalize(it["variable"], it["current"]) != normalize(it["variable"], it["value"])
    out = {"flavor": flavor, "version": ver, "hardware": {k: hw[k] for k in ("cpus", "ram_bytes", "disk_rotational", "disk_free_bytes")},
           "calculation": calc, "settings": items, "applied": None}
    text = [f"Baseline for {flavor} {ver} on {hw['cpus']} CPUs / {human_size(hw['ram_bytes'])} RAM / "
            f"{'HDD' if hw['disk_rotational'] else 'SSD'}:"]
    for it in items:
        mark = "*" if it["change"] else " "
        text.append(f" {mark} {it['variable']:<34} {str(it['current']):>14} -> {it['value']:<10} {it['reason']}")
    if a.apply:
        changes = [{"variable": it["variable"], "value": it["value"], "reason": it["reason"], "source": "baseline"} for it in items]
        res = _apply_and_record(c, changes, "baseline")
        out["applied"] = res
        text.append("Applied:\n" + _print_apply(res))
    else:
        text.append("(* = differs from current. Re-run with --apply to write and apply it.)")
        c.event("baseline", "baseline configuration generated", {"settings": items, "calculation": calc})
    emit(out, "\n".join(text))
    if a.apply and out["applied"].get("rolled_back"):
        raise Fail("baseline apply failed and was rolled back")


def _cluster_ctx(c):
    try:
        return mysqldb.cluster_of(c.variables, c.db.status())
    except Exception:
        return {}


def _engines_ctx(c):
    try:
        e = c.db.engines()
        return dict(e, primary=primary_engine(e))
    except Exception:
        return {}


def primary_engine(eng):
    """The storage engine holding most of the data (innodb when there is none yet)."""
    d = (eng or {}).get("data") or {}
    return max(d, key=d.get) if d and max(d.values()) > 0 else "innodb"


def _ntp_synchronized():
    """systemd's view of clock synchronisation: True / False, None when unknown."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True, timeout=5).stdout.strip()
        return {"yes": True, "no": False}.get(out)
    except Exception:
        return None


def cmd_session(a):
    c = Ctx()
    if a.action == "start":
        if c.state.get("measurement"):
            raise Fail("a measurement is in progress; stop it first")
        old = c.state.get("session")
        if old and not a.force:
            raise Fail(f"session #{old['id']} is still active: `tuning-agent session stop` it or pass --force")
        os_tuning = c.os_tuning_allowed() and not a.no_os_tuning
        r = c.client.post("/sessions", {"environment_id": c.env_id(), "name": a.name, "goal": a.goal, "policy": a.policy,
                                        "agent_version": __version__, "allow_stale_agent": bool(getattr(a, "allow_stale_agent", False)),
                                        "agent_time": time.time(), "ntp_synchronized": _ntp_synchronized(),
                                        "noise_pct": a.noise_pct, "notes": a.notes, "workload": a.workload, "os_tuning": os_tuning,
                                        "study": a.study, "planner": bool(getattr(a, "planner", False)), "ai": getattr(a, "ai", None)})
        if r.get("warning"):
            log("WARNING: " + r["warning"])
        warn = _maintenance_tasks(c)
        if warn:
            log("WARNING: scheduled tasks can disturb measurements: " + "; ".join(warn))
        if c.state.get("pending_changes"):
            log("note: changes applied before this session are part of its starting configuration")
            c.state.data["pending_changes"] = []
        scope = [x.strip() for x in (a.scope or "knobs,os").split(",") if x.strip()]
        if "filesystem" in scope and not (c.cfg.get("fs_device") and str(c.cfg.get("allow_fs_experiments")).lower() == "true"):
            raise Fail("scope 'filesystem' needs a dedicated device: tuning-agent init --fs-device /dev/... --allow-fs-experiments")
        sess = {"id": r["session_id"], "name": a.name or r.get("name"), "goal": a.goal, "policy": a.policy, "scope": scope,
                "noise_pct": a.noise_pct, "started": time.time(), "runs": [], "best": None, "os_tuning": os_tuning,
                "stability": getattr(a, "stability", None), "ai": getattr(a, "ai", None)}
        if r.get("study"):
            sess["study"] = r["study"]
        c.state.set("session", sess)
        c.state.set("requirements", {})  # the previous benchmark's prerequisites are not this session's
        emit({"ok": True, "session": sess, "url": r.get("url"), "study": r.get("study")},
             f"Session #{sess['id']} started (goal={a.goal}, policy={a.policy}). {r.get('url', '')}\n"
             + (f"Part of study '{r['study']['name']}': {r['study']['url']}\n" if r.get("study") else "") +
             "Next: start your workload, then `tuning-agent measure start` / `measure stop` (or `measure run --duration 300`).")
    elif a.action == "stop":
        sess = c.session()
        if c.state.get("measurement"):
            raise Fail("a measurement is in progress; stop it first")
        _remember_noise(c, sess)
        c.client.post(f"/sessions/{sess['id']}/close", {"summary": {"best": sess.get("best"), "best_safe": sess.get("best_safe"), "runs": len(sess.get("runs", []))}})
        hist = c.state.get("sessions_done") or []
        hist.append({k: sess.get(k) for k in ("id", "name", "goal", "best")})
        c.state.data["sessions_done"] = hist[-20:]
        c.state.data["session"] = None
        c.state.save()
        c.activity("idle", f"session #{sess['id']} closed")
        emit({"ok": True, "closed": sess["id"], "best": sess.get("best")}, f"Session #{sess['id']} closed. Best run: {sess.get('best')}")
    else:  # show
        emit(c.session(required=False) or {}, None)


def _parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise Fail(f"expected KEY=VALUE, got {it!r}")
        k, val = it.split("=", 1)
        try:
            out[k.strip()] = float(val) if "." in val or "e" in val.lower() else int(val)
        except ValueError:
            out[k.strip()] = val
    return out


def _measure_start(c, label=None, notes=None, warmed=False):
    sess = c.session()
    if c.state.get("measurement"):
        raise Fail("measurement already in progress (run `tuning-agent measure stop`)")
    store = SampleStore(c.state.dir)
    last = store.latest_ts()
    warn = None
    if not last or time.time() - last > 3 * int(c.cfg.get("interval", 10)):
        warn = "the tuning-agent daemon is not sampling (systemctl start tuning-agent); OS graphs will be missing"
        log("WARNING: " + warn)
    c.refresh()
    snap = collector.full_snapshot(c.db, hostinfo.device_for_path(c.datadir()))
    snap["variables"].update(c.os_settings())
    up = int(float(snap["status"].get("Uptime", 1e9)))
    if up < 300 and not warmed:
        w2 = f"mysqld restarted {up}s ago: caches may still be cold (use `measure run --warmup auto`)"
        log("WARNING: " + w2)
        warn = (warn + "; " if warn else "") + w2
    # numbers are never reused: an aborted run keeps its number on the server (#162 had two runs "2")
    seq = max(len(sess["runs"]), int(sess.get("last_seq") or 0)) + 1
    sess["last_seq"] = seq
    c.state.set("session", sess)
    pending = c.state.get("pending_changes") or []
    managed = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    label = label or ("initial" if seq == 1 and not managed else f"run-{seq}")
    r = c.client.post("/runs", {"session_id": sess["id"], "seq": seq, "label": label, "notes": notes,
                                "started": snap["t"], "config": snap["variables"], "managed": managed, "changes": pending,
                                "config_meta": _config_meta(c, snap["variables"]), "tradeoffs": tradeoffs_in(managed)})
    snap_path = os.path.join(c.state.dir, "measure_start.json")
    with open(snap_path, "w") as f:
        json.dump(snap, f, default=str)
    m = {"run_id": r["run_id"], "seq": seq, "label": label, "started": snap["t"], "changes": pending, "managed": managed}
    c.state.data["measurement"] = m
    c.state.data["pending_changes"] = []
    c.state.save()
    c.event("measure_start", f"measurement started: run {seq} '{label}'", {"run_id": r["run_id"]})
    return m, warn, r.get("url")


def _config_meta(c, variables):
    """Per variable: compiled default and where the current value comes from."""
    try:
        meta = c.db.variables_meta()
    except Exception:
        meta = {}
    ver = variables.get("version", "")
    cache = c.state.get("compiled_defaults") or {}
    if cache.get("version") != ver:
        cache = {"version": ver, "defaults": mysqldb.compiled_defaults()}
        c.state.set("compiled_defaults", cache)
    defaults = cache.get("defaults") or {}
    out = {}
    managed = c.state.get("managed", {})
    originals = c.state.get("os_original") or {}
    for k in variables:
        if k.startswith("os:"):
            out[k] = {"source": "OS-AGENT" if k in managed else "OS", "default": originals.get(k, variables[k]) if k in managed else variables[k]}
            continue
        m = dict(meta.get(k) or {})
        if "default" not in m and k in defaults:
            m["default"] = defaults[k]
        if m:
            out[k] = m
    return out


def _goal_value(goal, summary, external, explicit):
    if explicit is not None:
        return float(explicit), "explicit"
    if goal == "tps":
        for k in ("tps", "TPS", "nopm", "NOPM", "throughput"):
            if k in external:
                return float(external[k]), f"external:{k}"
        if summary.get("tps"):
            return summary["tps"], "commits/s"
        if summary.get("qps"):
            return summary["qps"], "queries/s (no explicit commits seen)"
        return None, None
    for k in ("latency", "avg_latency_ms", "latency_ms"):
        if k in external:
            return float(external[k]), f"external:{k}"
    if summary.get("avg_latency_ms") is not None:
        return summary["avg_latency_ms"], "performance_schema avg statement latency (ms)"
    return None, None


def _improvement(goal, new, old):
    """Positive = better."""
    if new is None or old in (None, 0):
        return None
    if goal == "latency":
        return round(100.0 * (old - new) / old, 2)
    return round(100.0 * (new - old) / old, 2)


def _end_snapshot(c):
    c.refresh()
    return collector.full_snapshot(c.db, hostinfo.device_for_path(c.datadir()))


def _assess_hypothesis(c, sess, seq, verdict, evaluation, summary, inputs):
    """Did the run confirm the hypothesis it was meant to test? Goal verdict + whether the targeted part of the
    database's time shrank per unit of work (vs the parent run's breakdown)."""
    h = dict(sess.pop("pending_hypothesis"))
    out = dict(h, run=seq)
    targets = set(h.get("target") or [])
    change = None
    if targets and evaluation.get("parent_runs"):
        try:
            # the parent configuration's run with the most detail: profiles run once per configuration, so a repeat of
            # the parent may have none - compare against the profiled one (same granularity on both sides)
            pars = []
            for pr in reversed(evaluation["parent_runs"]):
                try:
                    with open(os.path.join(c.state.dir, f"run{pr}_inputs.json")) as f:
                        pars.append(json.load(f))
                except OSError:
                    pass
            detail = lambda x: (bool(((x.get("inputs") or {}).get("cpu_profile") or {}).get("categories")),
                                bool(((x.get("inputs") or {}).get("offcpu") or {}).get("foreground")))
            par = max(pars, key=detail)
            pb = par["inputs"].get("time_breakdown") or bottleneck.breakdown(par["summary"], par["inputs"], (c.hw() or {}).get("cpus"))
            work = lambda s: (s.get("tps") or 0) or (s.get("qps") or 0) or 1.0  # per unit of work: transactions (queries)
            nb = inputs["time_breakdown"]
            # same granularity on both sides: when one run has no CPU profile, its CPU is one component - compare total CPU
            profiled = lambda b: any(p["name"].startswith("CPU: ") for p in b)
            cpu_key = (lambda n: "CPU" if n.startswith("CPU") else n) if profiled(pb) != profiled(nb) else (lambda n: n)
            tk = {cpu_key(n) for n in targets}
            before = sum(p["threads"] for p in pb if cpu_key(p["name"]) in tk) / work(par["summary"])
            after = sum(p["threads"] for p in nb if cpu_key(p["name"]) in tk) / work(summary)
            if any(cpu_key(n) != n for n in targets):
                out["evidence_note"] = "CPU compared as a whole (only one of the two runs has a CPU profile)"
            if before > 0:
                change = round(100.0 * (after - before) / before, 1)
                out["evidence"] = {"targeted_time_per_unit_change_pct": change}
        except (OSError, ValueError, KeyError):
            pass
    shrank = change is not None and change <= -10
    if h.get("kind") in ("revert", "validation", "requested"):
        out["outcome"] = "done"
    elif verdict == "improved":
        # "supported" needs both halves of the prediction: the goal improved AND the targeted time shrank (r1 / user
        # review: "supported" was recorded for goal losses and efficiency-only results, and taught the learner)
        out["outcome"] = "supported" if shrank else ("improved-unverified" if change is None else "improved-other-mechanism")
    elif verdict == "efficiency-gain":
        out["outcome"] = "efficiency"  # less work for the same goal: not a win for the hypothesis
    elif shrank:
        out["outcome"] = "moved"  # the mechanism worked, but that part of the time was not what limited the goal
    else:
        out["outcome"] = "refuted"
    pr = h.get("prediction") or {}
    gd = evaluation.get("goal_delta_pct")
    if pr and gd is not None and pr.get("goal_delta_pct_low") is not None:
        # the planner's prediction against the measurement: its track record is what makes its next choices credible
        lo, hi = float(pr["goal_delta_pct_low"]), float(pr["goal_delta_pct_high"])
        out["prediction_check"] = {"predicted": [lo, hi], "measured": gd, "within": lo - 0.5 <= gd <= hi + 0.5,
                                   "direction_right": (gd > 0) == ((lo + hi) / 2 > 0) if abs(gd) >= 0.5 else abs((lo + hi) / 2) < 2}
    sess.setdefault("hypotheses", []).append({k: out.get(k) for k in ("id", "run", "kind", "target", "tags", "outcome", "prediction_check")})
    log(f"   hypothesis ({h.get('id')}): {out['outcome']}" + (f", targeted time per unit {change:+.0f}%" if change is not None else ""))
    return out


def _measure_stop(c, external=None, goal_value=None, notes=None, b_snap=None, extra_inputs=None, goal_source=None):
    sess = c.session()
    m = c.state.get("measurement")
    if not m:
        raise Fail("no measurement in progress")
    with open(os.path.join(c.state.dir, "measure_start.json")) as f:
        a_snap = json.load(f)
    if b_snap is None:
        b_snap = _end_snapshot(c)
    if a_snap.get("hist"):
        a_snap["hist"] = [tuple(x) for x in a_snap["hist"]]
    a_snap["stmt"] = tuple(a_snap["stmt"]) if a_snap.get("stmt") else None
    a_snap["waits"] = {k: tuple(v) for k, v in (a_snap.get("waits") or {}).items()}
    a_snap["file_io"] = {k: list(v) for k, v in (a_snap.get("file_io") or {}).items()}
    samples = SampleStore(c.state.dir).window(a_snap["t"], b_snap["t"])
    restarted = int(float(b_snap["status"].get("Uptime", 0))) < (b_snap["t"] - a_snap["t"])
    if restarted:
        why = "mysqld restarted during the measurement window: counters are not comparable"
        try:
            ooms = int(b_snap["os"]["vm"].get("oom_kill", 0)) - int(a_snap["os"]["vm"].get("oom_kill", 0))
            if ooms > 0:
                why = f"mysqld was killed by the kernel OOM killer ({ooms}x) during the measurement window: memory is over-committed"
        except (KeyError, TypeError, ValueError):
            pass
        c.client.post(f"/runs/{m['run_id']}/abort", {"reason": why})
        c.state.data["pending_changes"] = (m.get("changes") or []) + (c.state.get("pending_changes") or [])
        c.state.data["measurement"] = None
        c.state.save()
        raise Fail(why + " (run aborted; measure again)")
    wu_s = ((extra_inputs or {}).get("bench") or {}).get("warmup_s")
    # the last ~15% of the warm-up (at least a minute): an adequate warm-up ends in the state the window measures
    tail = SampleStore(c.state.dir).window(a_snap["t"] - max(60, 0.15 * wu_s), a_snap["t"]) if wu_s else None
    # the process running the measured phase, when the benchmark started a new one with the window (warm-up and
    # measurement as separate invocations): its start-up is not the configuration warming up
    win0 = a_snap["t"]
    started = [o.get("started_at") for o in ((extra_inputs or {}).get("bench") or {}).get("observed") or []
               if o.get("started_at") and win0 - 5 <= o["started_at"] <= win0 + 30]
    summary, inputs = collector.analyze_window(a_snap, b_snap, samples, wu_s, tail, min(started) if started else None)
    try:  # who holds swapped-out memory at the end of the window
        top = hostinfo.swap_top()
        if top:
            inputs["swap_top"] = [{"process": n, "pid": p, "swap_mb": round(b / 2 ** 20, 1)} for n, p, b in top]
    except Exception:
        pass
    inputs["agent_version"] = __version__  # which engine build measured and judged this run
    try:  # memory budget of the configuration as measured, with mysqld's measured memory next to the model
        from . import memory
        # a buffer pool in huge pages is not in PSS (session 294: 4.8 GiB "measured" next to a 51 GiB pool): counted from the pool
        huge = (summary.get("hugepages_total_mb") or 0) if str(c.variables.get("large_pages", "")).upper() in ("ON", "1") else 0
        mb = memory.budget(c.variables, c.hw(), _client_reserve(c), measured=((summary.get("dbproc_pss_mb") or 0) + (summary.get("dbproc_swap_mb") or 0) + huge) * 2 ** 20 or None,
                           host_other=memory.host_other_bytes(summary, (c.hw() or {}).get("ram_bytes")),
                           active_measured=memory.active_from(summary))
        summary["memory_budget"] = {"needed": mb["needed"], "ram": mb["ram"], "headroom": mb["headroom"], "over_budget": mb["over_budget"],
                                    "mysqld_model": mb["mysqld_bytes"], "mysqld_measured": mb.get("measured_mysqld_bytes"),
                                    "active_connections": mb["active_connections"], "text": memory.text(mb)}
        inputs["memory_budget"] = mb
    except Exception:
        pass
    mp = inputs.get("memory_pfs") or {}
    if mp.get("total_mb"):
        # MySQL's own accounting next to what the OS sees: the difference is memory MySQL does not account for (thread
        # stacks, the binary, allocator overhead and fragmentation, uninstrumented libraries)
        summary["pfs_mem_total_mb"] = mp["total_mb"]
        summary["pfs_mem_growth_mb"] = mp.get("growth_mb")
        conns = summary.get("threads_connected_avg") or 0
        if mp.get("connections_mb") is not None and conns:
            summary["pfs_mem_per_connection_kb"] = round(mp["connections_mb"] * 1024 / conns, 1)
        if summary.get("dbproc_pss_mb") is not None:
            seen = (summary.get("dbproc_pss_mb") or 0) + (summary.get("dbproc_swap_pss_mb") or 0)
            summary["pfs_mem_unaccounted_mb"] = round(seen - mp["total_mb"], 1)
    try:  # NUMA: the topology and where mysqld's memory lives (only on multi-node hosts)
        from . import numa as _numa
        from .hostinfo import mysqld_pid as _mpid
        topo = _numa.topology()
        if topo["nodes"] > 1:
            pl = _numa.placement(_mpid()) if _mpid() else {}
            inputs["numa"] = {"topology": topo, "mysqld": pl}
            summary["numa_nodes"] = topo["nodes"]
            if pl:
                summary["numa_mysqld_max_node_pct"] = pl["max_node_pct"]
    except Exception:  # noqa
        pass
    if extra_inputs:
        inputs.update(extra_inputs)
        if (extra_inputs.get("cow") or {}).get("issues"):
            summary["cow_issues"] = extra_inputs["cow"]["issues"]
        if extra_inputs.get("config_mismatch"):
            summary["config_mismatch"] = extra_inputs["config_mismatch"]
        if extra_inputs.get("config_mismatch_all"):
            summary["config_mismatch_all"] = extra_inputs["config_mismatch_all"]
        if (extra_inputs.get("cow") or {}).get("intended"):
            summary["cow_intended"] = extra_inputs["cow"]["intended"]
        for k, v in (extra_inputs.get("probe") or {}).items():
            summary["probe_" + k] = v
        # storage tail latency of the data device (eBPF block histogram): read / write / flush p99, worst I/O, slow I/Os
        bl = (extra_inputs.get("block_latency") or {}).get("by_device_op") or {}
        if bl:
            dev = (hostinfo.device_for_path(c.datadir()) or "").split("/")[-1]
            base = re.sub(r"p\d+$", "", dev) if dev.startswith(("nvme", "mmcblk")) else re.sub(r"\d+$", "", dev)
            mine = {k.split(":", 1)[1]: v for k, v in bl.items() if k.split(":", 1)[0] in (dev, base)} or \
                {k.split(":", 1)[1]: v for k, v in sorted(bl.items(), key=lambda x: x[1]["count"])}
            for op in ("read", "write", "flush", "flush+write"):
                v = mine.get(op)
                if v:
                    key = op.replace("+", "_")
                    summary[f"blk_{key}_p99_ms"] = round((v.get("p99_us") or 0) / 1000, 3)
                    summary[f"blk_{key}_p999_ms"] = round((v.get("p999_us") or 0) / 1000, 3)
                    summary[f"blk_{key}_max_ms"] = round((v.get("max_us") or 0) / 1000, 3)
                    summary[f"blk_{key}_slow_ios"] = v.get("slow_10ms")
    summary["tradeoffs"] = tradeoffs_in(m["managed"])
    clients = {}
    for h in c.client_hosts():
        try:
            w = c.client.get(f"/hosts/{h['id']}/window?start={a_snap['t']}&end={b_snap['t']}")
            if w.get("samples"):
                clients[h["name"]] = w
        except Exception:
            pass
    if clients:
        inputs["clients"] = clients
        keys = ("cpu_busy", "cpu_steal", "net_conn_per_s", "net_time_wait", "net_retrans_pct", "rtt_avg_ms", "rtt_p95_ms", "softirq_max_cpu_pct",
                "softirq_avg_cpu_pct", "net_tx_mbs", "net_rx_mbs", "psi_cpu_some")
        for k in keys:
            vals = [w["avg"][k] for w in clients.values() if k in w["avg"]]
            if vals:
                summary["client_" + k] = round(max(vals), 3)
    external = external or {}
    gv, src = _goal_value(sess["goal"], summary, external, goal_value)
    if goal_source and goal_value is not None:
        src = goal_source
    summary["goal_value"] = gv
    summary["goal_source"] = src
    fg = ((inputs.get("offcpu") or {}).get("foreground") or {})
    if not fg:  # profiled once per configuration: a repeat takes the share measured in the profiled run of this configuration
        key = evaluate.config_key(m["managed"])
        prev = [r for r in sess["runs"] if evaluate.config_key(r.get("managed")) == key
                and (r.get("summary") or {}).get("offcpu_swapin_share_pct") is not None]
        if prev:
            summary["offcpu_swapin_share_pct"] = prev[-1]["summary"]["offcpu_swapin_share_pct"]
    if fg:  # measured time connection threads waited for pages coming back from swap (share of their off-CPU time: an upper bound)
        tot = sum((x.get("seconds") or 0) for x in fg.values())
        sw = sum((x.get("seconds") or 0) for x in fg.values() if "swap" in (x.get("tags") or []))
        summary["offcpu_swapin_share_pct"] = round(100.0 * sw / tot, 3) if tot else 0.0
    runs = sess["runs"]
    prev = runs[-1] if runs else None
    first = runs[0] if runs else None
    d_prev = _improvement(sess["goal"], gv, prev.get("goal_value")) if prev else None
    d_first = _improvement(sess["goal"], gv, first.get("goal_value")) if first else None
    _refresh_noise(c, sess, runs)
    # the same fields a re-evaluation sees (the label tells a sweep point's own change from a sweep restore)
    hint = c.state.get("parent_hint")
    verdict, evaluation = evaluate.verdict({"seq": m.get("seq"), "label": m.get("label"), "managed": m["managed"], "goal_value": gv,
                                            "summary": summary, "changes": m.get("changes"), "parent_hint": hint}, runs, sess["goal"],
        sess.get("noise") or ({"noise_pct": sess["noise_pct"]} if sess.get("noise_pct") else None))
    if hint:
        evaluation["parent_hint"] = hint  # a re-evaluation compares with the same parent
    if evaluation.get("parent_runs"):
        try:  # query-level view: which statements got faster / slower than under the parent configuration
            with open(os.path.join(c.state.dir, f"run{evaluation['parent_runs'][-1]}_inputs.json")) as f:
                parent_inputs = json.load(f).get("inputs", {})
            qi = evaluate.query_impact(inputs.get("top_statements"), parent_inputs.get("top_statements"))
            if qi:
                evaluation["query_impact"] = qi
        except (OSError, ValueError):
            pass
    ctx = {"goal": sess["goal"], "policy": sess["policy"], "hw": c.hw(), "variables": b_snap["variables"],
           "summary": summary, "inputs": inputs}
    diagnosis = engine.diagnose(ctx)
    inputs["workload"] = engine.classify_workload(summary, inputs)
    inputs["time_breakdown"] = bottleneck.breakdown(summary, inputs, (c.hw() or {}).get("cpus"))
    if verdict not in ("repeat", "reference") and sess.get("pending_hypothesis"):
        evaluation["hypothesis"] = _assess_hypothesis(c, sess, m["seq"], verdict, evaluation, summary, inputs)
    run = {"id": m["run_id"], "seq": m["seq"], "label": m["label"], "started": m["started"], "ended": b_snap["t"],
           "changes": m["changes"], "managed": m["managed"], "goal_value": gv, "delta_pct": d_prev, "delta_vs_first_pct": d_first,
           "verdict": verdict, "evaluation": evaluation, "summary": summary}
    c.client.post(f"/runs/{m['run_id']}/finish", {
        "ended": b_snap["t"], "summary": summary, "external": external, "inputs": inputs, "diagnosis": diagnosis,
        "goal_value": gv, "goal_source": src, "delta_pct": d_prev, "delta_vs_first_pct": d_first, "verdict": verdict,
        "evaluation": evaluation, "notes": notes})
    runs.append(run)
    _update_best(sess)
    sess["last_inputs_path"] = os.path.join(c.state.dir, f"run{run['seq']}_inputs.json")
    with open(sess["last_inputs_path"], "w") as f:
        json.dump({"summary": summary, "inputs": inputs, "variables": b_snap["variables"], "diagnosis": diagnosis}, f, default=str)
    sess["last_recommendation"] = None  # based on an older run
    c.state.data["session"] = sess
    c.state.data["measurement"] = None
    c.state.save()
    c.event("measure_stop", f"run {run['seq']} '{run['label']}' finished: goal={gv} ({verdict})", {"run_id": m["run_id"]})
    return run, diagnosis


def _update_best(sess):
    noise = float((sess.get("noise") or {}).get("noise_pct") or sess.get("noise_pct") or 2.0)
    runs = sess["runs"]
    # trade-offs of the starting configuration (the benchmark's own setup, e.g. no binary log) are not the tuner's:
    # "best safe" = no trade-off beyond them (session 226: disable_log_bin from the benchmark made every run a trade-off)
    start = start_tradeoffs(runs)
    sess["start_tradeoffs"] = start
    for key, sel in (("best", runs), ("best_safe", [r for r in runs if not added_tradeoffs(r, start)])):
        b = engine.best_config(sel, sess["goal"], noise)
        if b:
            sess[key] = {"seq": b["seqs"][-1], "runs": b["seqs"], "label": b["label"], "goal_value": round(b["mean"], 4),
                         "mean_of_runs": b["n"], "tradeoffs": added_tradeoffs(next((r for r in sel if r["seq"] == b["seqs"][-1]), {"managed": b["managed"]}), start),
                         **({"start_tradeoffs": start} if start else {})}


def _fmt_run(run, diagnosis, goal):
    s = run["summary"]
    src = str(s.get("goal_source") or "")
    unit = src.split(":", 1)[1].split()[0] if src.startswith("benchmark:") else ("ms" if goal == "latency" else "/s")
    lines = [f"Run {run['seq']} '{run['label']}': goal {goal} = {run['goal_value']} {unit} ({s.get('goal_source')})  verdict: {run['verdict']}"]
    ev = run.get("evaluation") or {}
    if ev.get("same_config_runs"):
        lines.append(f"  {ev['note']} (deviation {ev['deviation_pct']:+.2f}% from mean {ev['same_config_mean']:.4g})")
    if ev.get("goal_delta_pct") is not None:
        lines.append(f"  vs parent configuration (mean of runs {ev['parent_runs']}) {ev['goal_delta_pct']:+.2f}% "
                     f"(noise threshold {ev['goal_noise_pct']}%{'' if ev.get('calibrated') else ', NOT calibrated: run `tuning-agent calibrate`'})")
        for k, e in (ev.get("efficiency") or {}).items():
            if k in evaluate.KEY_EFFICIENCY:
                lines.append(f"    {e['label']}: {e['parent']:.4g} -> {e['now']:.4g} ({e['improvement_pct']:+.1f}% less work, noise {e['noise_pct']}%)")
    if run.get("delta_pct") is not None:
        lines.append(f"  vs previous run {run['delta_pct']:+.2f}%   vs first run {run['delta_vs_first_pct']:+.2f}%   (positive = better)")
    lines.append(f"  tps {s.get('tps')}  qps {s.get('qps')}  avg latency {s.get('avg_latency_ms')} ms  p95 {s.get('p95_latency_ms')} ms  "
                 f"cpu {s.get('cpu_busy')}% iowait {s.get('cpu_iowait')}%  bp hit {s.get('bp_hit_pct')}%  ckpt age max {s.get('checkpoint_age_pct_max')}%")
    for d in diagnosis:
        lines.append(f"  [{d['severity']}] {d['type']}: {d['text']}")
    return "\n".join(lines)


def _warmup(c, spec):
    """spec: seconds, or 'auto' = until the buffer pool load finished and throughput is stable."""
    if spec in (None, "", "0", 0):
        return 0
    if str(spec) != "auto":
        log(f"warm-up {int(spec)}s ...")
        time.sleep(int(spec))
        return int(spec)
    t0 = time.time()
    store = SampleStore(c.state.dir)
    min_s, max_s, window = 60, 900, 60
    log("warm-up auto: waiting for buffer pool load and stable throughput (60-900 s) ...")
    while True:
        time.sleep(10)
        el = time.time() - t0
        try:
            st = c.db.status()
        except Exception:
            continue
        load = str(st.get("Innodb_buffer_pool_load_status", ""))
        loading = "completed" not in load.lower() and "not started" not in load.lower() and load != ""
        samples = [x for x in store.window(time.time() - window, time.time()) if x.get("db", {}).get("tps") is not None]
        vals = [x["db"]["tps"] or x["db"].get("qps", 0) for x in samples]
        stable = False
        if len(vals) >= 5 and sum(vals) > 0:
            m = sum(vals) / len(vals)
            sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
            first, last = sum(vals[:len(vals) // 2]) / (len(vals) // 2), sum(vals[len(vals) // 2:]) / (len(vals) - len(vals) // 2)
            stable = sd / m < 0.05 and abs(last - first) / m < 0.03
        if el >= min_s and not loading and stable:
            log(f"warm-up done after {el:.0f}s")
            return round(el)
        if el >= max_s:
            log(f"warm-up stopped at the {max_s}s limit (load status: {load or '-'}; throughput stable: {stable})")
            return round(el)


def _maybe_profile(c, sess, prof, warmup_s, in_window=False):
    """CPU profile for a slice of this run when this configuration was not profiled yet.
    agent.ini: cpu_profiling = auto (default) | always | off, profile_hz (49), profile_seconds (20)."""
    from . import cpuprof
    mode = str(c.cfg.get("cpu_profiling", "auto")).lower()
    if mode == "off" or (warmup_s < 30 and not in_window):  # needs room for the profile before the measured window starts
        return None
    key = evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()})
    done = sess.get("profiled_configs") or []
    if mode == "auto" and repr(key) in done:  # once per configuration: where the CPU goes is part of every breakdown
        return None
    if not cpuprof.ensure_perf(log):
        log("CPU profiling skipped: perf is not available")
        return None
    secs = int(c.cfg.get("profile_seconds", 20)) if in_window else min(int(c.cfg.get("profile_seconds", 20)), warmup_s - 10)
    p = cpuprof.Profiler(c.state.dir, secs, int(sess.get("profile_hz") or c.cfg.get("profile_hz", 49)))
    p.in_window = in_window
    if in_window:
        # no warm-up (an analytical query set that runs to completion): a short low-rate slice early in the window -
        # without it the breakdown has no CPU detail at all (JOB)
        p.start(delay=15)
        log(f"   CPU profile: {secs}s at {p.hz} Hz early in the measured window (the benchmark has no warm-up)")
    else:
        p.start(delay=max(0, warmup_s - secs - 5))  # ends 5 s before the measurement starts
        log(f"   CPU profile: {secs}s at {p.hz} Hz at the end of the warm-up (outside the measured window)")
    sess.setdefault("profiled_configs", []).append(repr(key))
    c.state.set("session", sess)
    return p


OFFCPU_GAP = 30  # s between the end of the off-CPU slice and the CPU profile: bpftrace symbolises its maps at exit


def _maybe_offcpu(c, sess, warmup_s, cpu_secs, in_window=False):
    """Off-CPU profile (eBPF): what connection threads wait for, by stack, once per configuration. It runs before the CPU
    profile so the two never overlap (kprobe overhead would show up as CPU). agent.ini: offcpu_profiling = auto | off,
    offcpu_seconds (10)."""
    from . import probes
    if str(c.cfg.get("offcpu_profiling", "auto")).lower() == "off" or not probes.available() or sess.get("offcpu_failures", 0) >= 2:
        return None
    key = repr(evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()}))
    if key in (sess.get("offcpu_configs") or []):
        return None
    secs = int(c.cfg.get("offcpu_seconds", 10))
    pid = hostinfo.mysqld_pid()
    if not pid:
        return None
    if in_window:
        # no warm-up (a query set that runs to completion): a slice inside the window, after the CPU profile's slice
        delay = 15 + (cpu_secs + OFFCPU_GAP if cpu_secs else 0)
        o = probes.OffCpu(pid, secs, c.state.dir).start(delay=delay)
        log(f"   off-CPU profile: {secs}s inside the measured window (the benchmark has no warm-up), {delay}s in")
    else:
        end_before = (cpu_secs + 5 if cpu_secs else 0) + OFFCPU_GAP
        if warmup_s < secs + end_before + 10:
            return None
        o = probes.OffCpu(pid, secs, c.state.dir).start(delay=warmup_s - secs - end_before)
        log(f"   off-CPU profile: {secs}s, {end_before}s before the measured window")
    o.in_window = in_window
    sess.setdefault("offcpu_configs", []).append(key)
    c.state.set("session", sess)
    return o


def _host_other_now():
    """Memory the host uses outside mysqld right now: RAM minus available minus mysqld's resident memory (memory.
    host_other_bytes, live). Kernel or driver allocations no counter names land here (tuner-vbm-amd: ~6.6 GB of 30 GB)."""
    try:
        mi = hostinfo.meminfo()
        pid = hostinfo.mysqld_pid()
        rss = 0
        if pid:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
        hp = mi.get("HugePages_Total", 0) / 1024 * mi.get("Hugepagesize", 0)  # pages x page bytes (meminfo() scales every value by 1024)
        return max(0, int(mi.get("MemTotal", 0) - mi.get("MemAvailable", 0) - rss - hp)) or None
    except (OSError, ValueError):
        return None


def _observe(r, prev):
    """Benchmark tool processes seen now, added to what was seen before (never fails a run)."""
    try:
        got = r.observe()
    except Exception:
        return prev
    have = {tuple(o["argv"]) for o in prev}
    return prev + [o for o in got if tuple(o["argv"]) not in have]


def _finish_offcpu(o):
    o.join(timeout=o.seconds + 900)
    try:
        res = o.result()
    except Exception as e:  # never fail a run because of the profiler
        return {"error": str(e)[:300]}
    fg = res.get("foreground") or {}
    if fg:
        log("   off-CPU (connection threads): " + ", ".join(f"{k.replace('Waiting: ', '')} {v['threads']:.2f}" for k, v in list(fg.items())[:5]))
    res["in_measured_window"] = bool(getattr(o, "in_window", False))
    return res


def _start_probes(c, seconds):
    """eBPF off-CPU probes (name resolution, ...) over the measured window. agent.ini: probes = auto (default) | off."""
    from . import probes
    if str(c.cfg.get("probes", "auto")).lower() == "off" or not probes.available():
        return None
    try:
        pid = hostinfo.mysqld_pid()
        p = probes.Probes(pid, seconds).start() if pid else None
        if p:
            p.block = probes.BlockLatency(seconds).start()  # storage tail latency over the same window
        return p
    except Exception as e:  # never fail a run because of a probe
        log(f"   eBPF probes not started: {e}")
        return None


def _finish_profile(c, p, t_meas0):
    from . import cpuprof
    p.join(timeout=p.seconds + 120)
    try:
        pids = set(hostinfo.service_pids(hostinfo.mysqld_pid() or 0) or [])
        res = p.process(pids)
    except Exception as e:  # never fail a run because of the profiler
        return {"error": str(e)[:300]}
    res["in_measured_window"] = bool(getattr(p, "in_window", False))
    cats = res.get("categories") or {}
    if cats:
        log("   CPU profile: " + ", ".join(f"{k} {v}%" for k, v in list(cats.items())[:5])
            )
    return res


def _ensure_requirements(c, prof):
    """Benchmark prerequisites ([requires] in the profile): raise them at runtime before the run if the server is below.
    Numeric values are minimums; others must match. Returns what was checked / changed for the run record."""
    out = []
    for var, want in (prof.requires or {}).items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", var):
            out.append({"variable": var, "required": want, "error": "not a server variable name"})
            continue
        try:
            cur = c.db.scalar(f"SELECT @@GLOBAL.{var}")
        except Exception as e:
            out.append({"variable": var, "required": want, "error": str(e)[:200]})
            continue
        try:
            ok = float(cur) >= float(want)
        except (TypeError, ValueError):
            ok = str(cur).lower() == str(want).lower()
        item = {"variable": var, "required": want, "server": str(cur), "raised": False}
        if not ok:
            try:
                c.db.execute(f"SET GLOBAL {var} = %s", (int(want) if str(want).isdigit() else want,))
                item["raised"] = True
                log(f"benchmark requirement: {var} {cur} -> {want}")
            except Exception as e:
                item["error"] = str(e)[:200]
                log(f"WARNING: benchmark requirement {var}={want} could not be set: {e}")
        out.append(item)
    # remembered: every restart the agent makes re-applies them (Applier.reapply_runtime) - SET GLOBAL values do not
    # survive one, and a benchmark connecting right after a restart failed with 'Too many connections' (user report)
    c.state.set("requirements", {k: str(v) for k, v in (prof.requires or {}).items() if re.fullmatch(r"[a-z_][a-z0-9_]*", k)})
    return out


def _bench_run(c, prof, label=None, notes=None, warmup_override=None):
    """One measured run (_bench_run_once). mysqld lost while the agent talks to it - OOM-killed or crashed during the
    warm-up or at the measurement start - is the configuration failing, not the session: it comes back as ServerDown /
    BenchFailed, which autotune reverts and continues from (r2 RO-4.ns: an OOM kill surfaced as a pymysql read timeout in
    the measurement start and ended the session)."""
    import pymysql
    t0 = time.time()
    try:
        return _bench_run_once(c, prof, label, notes, warmup_override)
    except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
        oom = _oom_killed_since(t0)
        try:  # wait for mysqld to come back (systemd restarts it) so the revert can be applied
            c.db.connect(retries=60, delay=2)
        except Exception:
            pass
        if c.state.get("measurement"):  # a run had been started: it measured nothing
            try:
                c.client.post(f"/runs/{c.state['measurement']['run_id']}/abort", {"reason": f"mysqld lost: {str(e)[:300]}"})
            except Exception:
                pass
            c.state.data["measurement"] = None
            c.state.save()
        if oom:
            raise BenchFailed("mysqld was OOM-killed during the run (" + oom.split("kernel:")[-1].strip()[:160] + ")")
        raise ServerDown(f"lost the connection to mysqld during the run: {str(e)[:300]}")


def _bench_run_once(c, prof, label=None, notes=None, warmup_override=None):
    """reset -> start benchmark -> warm-up -> measure -> wait for exit -> parse metrics -> finish run."""
    t_run0 = time.time()
    sess = c.session()
    if (prof.auto_warmup or prof.auto_duration) and not sess.get("run_plan"):
        log(f"note: the profile leaves the run length to the probe; without one this run uses warm-up {prof.warmup} s and "
            f"{prof.duration} s measured (find them with `tuning-agent run-length --bench ...`, or let autotune do it)")
    logdir = os.path.join(c.state.dir, "bench")
    os.makedirs(logdir, exist_ok=True)
    seq = len(sess["runs"]) + 1
    logfile = os.path.join(logdir, f"s{sess['id']}-run{seq}.log")
    # a writing benchmark's reset restores the data and restarts mysqld itself; a read-only or seasoned one keeps its
    # data, so the clean start is a restart (before any warm-up step its "reset" runs, e.g. tpch_warm.sh)
    if prof.restart_before_run and (not prof.reset or prof.data_state in ("read-only", "seasoned")):
        log("clean start: restarting mysqld and dropping the OS page cache")
        c.activity("clean start", "restarting mysqld")
        ok, err = Applier(c.cfg, c.state, c.db, log=log, activity=c.activity).restart()
        if not ok:
            raise ServerDown(f"mysqld restart before the run failed: {err}")
        subprocess.run(["sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"], capture_output=True)
        c.db.connect(retries=30, delay=2)
        c.state.set("restart_pending", False)
    if prof.reset:
        log(f"reset: {prof.reset}")
        c.activity("resetting data", prof.reset.split()[0].rsplit("/", 1)[-1])
        c.db.close()
        rc, out = bench.run_shell(prof.reset, timeout=3600, log=logfile + ".reset")
        if rc != 0:
            raise ServerDown(f"reset command failed ({rc}): {out[-500:]}")
        c.db.connect(retries=30, delay=2)
        c.state.set("restart_pending", False)

    try:  # free space on the data disk before the run: a seasoned / growing data set can fill it (session 222)
        st_ = os.statvfs(c.variables.get("datadir") or "/var/lib/mysql")
        free_pct = 100.0 * st_.f_bavail / max(st_.f_blocks, 1)
        if free_pct < 2:
            raise Fail(f"the data disk is almost full ({free_pct:.1f}% free): free space or reset the data set before measuring")
        if free_pct < 10:
            log(f"WARNING: the data disk has {free_pct:.1f}% free - below ~10% SSDs and filesystems slow down")
    except OSError:
        pass
    # the configuration the run is labelled with must be the one the server runs: a value MySQL silently adjusted
    # (buffer pool rounded to chunk x instances: 45G -> 48G) makes the run measure something else - judged unreliable
    mismatch = _config_mismatches(c)
    # a mismatch every run shares (e.g. table_open_cache capped by the open-files limit) does not bias a comparison: only
    # one new since the previous run does
    before = {(x["variable"], str(x["running"])) for x in ((sess.get("runs") or [{}])[-1].get("summary") or {}).get("config_mismatch_all") or []}
    mismatch_all, mismatch = mismatch, [x for x in mismatch if (x["variable"], str(x["running"])) not in before]
    cow = None
    if prof.reset or not sess.get("runs"):
        # copy-on-write under the data: a reflink / snapshot clone of the pristine copy makes every first write copy its
        # block - runs start slow and speed up, favouring settings that postpone writes. Checked on the host itself.
        try:
            from . import cowcheck
            cow = cowcheck.check(c.datadir())
            if prof.copy_on_write == "intended" and cow["fs_issues"]:
                # measuring a copy-on-write file system on purpose: a note on the run, not a warning or noise source
                cow["intended"] = cow["fs_issues"]
                cow["issues"] = [t for t in cow["issues"] if t not in cow["fs_issues"]]
                if not sess.get("cow_noted"):
                    log(f"note: copy-on-write file system ({cow['fs']}) intended by the profile: its cost is part of the measurement")
                    sess["cow_noted"] = True
                    c.state.set("session", sess)
            if cow["issues"] and (sess.get("cow_warned") != cow["issues"]):
                for t in cow["issues"]:
                    log("WARNING copy-on-write data: " + t)
                sess["cow_warned"] = cow["issues"]
                c.state.set("session", sess)
                c.event("environment", "copy-on-write under the data directory: " + "; ".join(cow["issues"]), cow)
        except Exception as e:  # noqa - never fail a run because of the check
            log(f"copy-on-write check failed: {e}")
    env_probe = {}
    # environment check: during calibration only (its baseline), and again when the previous run showed unusual steal.
    # Between runs it mostly measured its own noise; steal is measured passively all the time.
    prev = (sess.get("runs") or [{}])[-1]
    probe_now = prof.probe == "always" or (prof.probe and (not sess.get("noise") or str(label or "").startswith("baseline")
                                                           or ((prev.get("evaluation") or {}).get("steal"))))
    if probe_now:
        log("environment probe (CPU, storage) ...")
        c.activity("environment check", "quick CPU and storage benchmark")
        try:
            env_probe = bench.probe(c.datadir(), log)
        except Exception as e:  # never fail a run because of the probe
            log(f"probe failed: {e}")
    requirements = _ensure_requirements(c, prof)
    log(f"benchmark: {prof.name}")
    c.activity("starting benchmark", label)
    if sess.get("warmup_seconds") and prof.can_resize() and str(prof.warmup) != str(sess["warmup_seconds"]):
        prof.timeout += int(sess["warmup_seconds"]) - int(prof.warmup)
        prof.warmup = str(sess["warmup_seconds"])  # warm-up chosen at calibration (steady state)
    if sess.get("run_seconds") and prof.can_resize() and prof.duration != int(sess["run_seconds"]):
        # run length chosen at calibration (noise plan)
        prof.timeout += int(sess["run_seconds"]) - prof.duration
        prof.duration = int(sess["run_seconds"])
    r = bench.Running(prof.command(), logfile)
    t_launch = time.time()  # the benchmark's own clock starts here: its measured phase is launch + warm-up .. + duration
    profiler = offcpu = None
    try:
        warm = warmup_override if warmup_override is not None else prof.warmup
        if str(warm) == "auto":
            waited = _warmup(c, "auto")
        else:
            waited = int(warm or 0)
            # CPU profile during the end of the warm-up: the load is already on, and the measured window stays untouched
            profiler = _maybe_profile(c, sess, prof, waited)
            offcpu = _maybe_offcpu(c, sess, waited, profiler.seconds if profiler else 0)
            if waited:
                log(f"warm-up {waited}s ...")
                c.activity("warm-up", label, waited)
                deadline = time.time() + waited
                while time.time() < deadline and r.alive():
                    time.sleep(1)
        if not r.alive():
            oom = _oom_killed_since(t_run0)
            raise BenchFailed(("mysqld was OOM-killed during the warm-up (" + oom.split("kernel:")[-1].strip()[:160] + "); " if oom else "")
                              + f"benchmark exited during warm-up (code {r.p.returncode}); tail:\n{r.output()[-1500:]}")
        m, warn, url = _measure_start(c, label, notes, warmed=True)
        t_meas0 = time.time()
        observed = _observe(r, [])
        probes = _start_probes(c, prof.duration if prof.duration > 0 else prof.timeout)
        if profiler is None and not waited:
            profiler = _maybe_profile(c, c.session(), prof, 0, in_window=True)
        if offcpu is None and not waited:
            offcpu = _maybe_offcpu(c, c.session(), 0, profiler.seconds if profiler else 0, in_window=True)
        if prof.duration > 0:
            # the window ends when the benchmark's measured phase ends, not duration seconds after the agent's own setup
            # (snapshot, eBPF probes): on a 32-CPU host the setup took ~140 s and the window ran past the end of HammerDB's
            # timed phase, measuring the virtual users shutting down (client NUMA host, session #194)
            planned_end = t_launch + (waited or 0) + prof.duration
            delay = time.time() - (t_launch + (waited or 0))
            deadline = min(time.time() + prof.duration, planned_end) if waited else time.time() + prof.duration
            m["window_start_delay_s"] = round(delay, 1)
            if waited and delay > 0.05 * prof.duration:
                log(f"WARNING: the measured window starts {delay:.0f}s after the warm-up ended (agent setup): it is shortened to end with "
                    "the benchmark's measured phase")
            log(f"measuring run {m['seq']} for {max(0, deadline - time.time()):.0f}s ...")
            c.activity("measuring", f"run #{m['seq']} {label or ''}", prof.duration)
            while time.time() < deadline and r.alive():
                time.sleep(1)
        else:
            # early abort (OtterTune lesson): a configuration that makes the run take far longer than usual is a failure
            durs = sorted(x["summary"].get("duration_s") for x in sess["runs"] if (x.get("summary") or {}).get("duration_s"))
            limit = min(prof.timeout, 2.5 * durs[len(durs) // 2]) if len(durs) >= 2 else prof.timeout
            log(f"measuring run {m['seq']} until the benchmark finishes (abort after {limit:.0f}s) ...")
            c.activity("measuring", f"run #{m['seq']} {label or ''} (until the benchmark finishes)", durs[len(durs) // 2] if durs else None)
            t_start = time.time()
            deadline = t_start + limit
            while time.time() < deadline and r.alive():
                time.sleep(2)
            if r.alive():
                raise TooSlow(f"run exceeded {limit:.0f}s (2.5x the typical run time): configuration rejected")
        observed = _observe(r, observed)  # tools that started later (no warm-up, multi-stage drivers)
        if probes:
            probes.stop()
            if getattr(probes, "block", None):
                probes.block.stop()
        b_snap = _end_snapshot(c)
        # after the measured window the benchmark still has to exit (1024 threads disconnecting, a final report): said so,
        # with the time it takes, instead of 'measuring' going stale (user session 311: 15 min overdue alerts)
        t_exit, last_ = time.time(), 0
        limit_ = max(10, prof.timeout - (time.time() - r.t0))
        # a benchmark told its length (placeholders) that keeps running well past it never ends by itself: sysbench treats
        # --time=0 as "no limit" (a script that passes {warmup}=0 through as a time, user report) - stopped with a warning
        # instead of holding the session until the profile's timeout
        planned_end = r.t0 + (waited or 0) + max(prof.duration, 0) + 30
        grace = max(120, 0.2 * max(prof.duration, 0))
        if prof.can_resize() and prof.duration > 0:
            limit_ = min(limit_, max(10, planned_end + grace - time.time()))
        while r.alive() and time.time() - t_exit < limit_:
            if time.time() - last_ >= 60:
                c.activity("waiting for the benchmark to exit", f"run #{m['seq']}: measured window done "
                           + (f"{int(time.time() - t_exit) // 60} min ago" if time.time() - t_exit >= 60 else "just now"),
                           limit_ - (time.time() - t_exit))
                last_ = time.time()
            time.sleep(2)
        if r.alive() and prof.can_resize() and prof.duration > 0 and time.time() >= planned_end + grace - 5:
            msg = (f"the benchmark was still running {int(time.time() - planned_end)} s after its planned end (warm-up {waited or 0} s + "
                   f"{prof.duration} s): stopped. Check that the run command ends by itself - e.g. sysbench --time=0 means no limit, "
                   "so a {warmup} of 0 must skip the warm-up step, not run without a time limit.")
            log("WARNING: " + msg)
            c.event("benchmark", msg, {"run_seq": m.get("seq")})
        rc = r.wait(max(1, limit_ - (time.time() - t_exit)))
    except BaseException as exc:
        r.kill()
        if c.state.get("measurement"):
            m0 = c.state["measurement"]
            why = "interrupted (Ctrl-C / SIGTERM)" if isinstance(exc, KeyboardInterrupt) else f"agent error: {type(exc).__name__}: {exc}"
            try:
                c.client.post(f"/runs/{m0['run_id']}/abort", {"reason": why[:900]})
            except Exception:
                pass
            # the changes were not measured: hand them back to the next run
            c.state.data["pending_changes"] = (m0.get("changes") or []) + (c.state.get("pending_changes") or [])
            c.state.data["measurement"] = None
            c.state.save()
        raise
    text = r.output()
    metrics = prof.parse(text)
    goal_value = goal_src = None
    if prof.goal_metric:
        if prof.goal_metric not in metrics:
            log(f"WARNING: goal metric {prof.goal_metric!r} not found in benchmark output; using server-side measurement")
        else:
            goal_value = metrics[prof.goal_metric] * prof.goal_scale
            goal_src = f"benchmark:{prof.goal_metric}" + (f" x{prof.goal_scale:g}" if prof.goal_scale != 1 else "")
    spec = prof.spec()
    if observed:
        import hashlib
        spec["observed"] = observed
        spec["spec_sha"] = hashlib.sha256((spec["spec_sha"] + json.dumps([o["argv"] for o in observed])).encode()).hexdigest()[:12]
    extra = {"bench": {**spec, "requirements": requirements, "exit_code": rc, "metrics": metrics, "log_tail": text[-4000:],
                       "window_start_delay_s": m.get("window_start_delay_s"), "warmup_s": waited}, "probe": env_probe}
    if cow is not None:
        extra["cow"] = cow
    if mismatch:
        extra["config_mismatch"] = mismatch
    if mismatch_all:
        extra["config_mismatch_all"] = mismatch_all
    if profiler:
        extra["cpu_profile"] = _finish_profile(c, profiler, t_meas0)
    if offcpu:
        extra["offcpu"] = _finish_offcpu(offcpu)
        if (extra["offcpu"] or {}).get("error"):
            # a profiler that fails keeps failing on this host (tuning-db-arm16: a bpftrace assertion in 24 of 24 runs):
            # stop after two failures in the session and say why once, instead of paying for it in every run
            s2 = c.session()
            s2["offcpu_failures"] = s2.get("offcpu_failures", 0) + 1
            if s2["offcpu_failures"] == 2:
                msg = ("off-CPU profiling disabled for this session after two failures: " + str(extra["offcpu"]["error"])[:200]
                       + ". Time breakdowns use the CPU profile and wait instruments only (commit / lock waits are less detailed).")
                log("   WARNING: " + msg)
                c.event("profiler", msg, {"error": extra["offcpu"]["error"]})
            c.state.set("session", s2)
    if probes:
        extra["probes"] = probes.result()
        if getattr(probes, "block", None):
            extra["block_latency"] = probes.block.result()
            bl = extra["block_latency"].get("by_device_op") or {}
            if bl:
                log("   block I/O latency: " + ", ".join(f"{k} p99 {v['p99_us'] / 1000:.2f} ms / max {(v.get('max_us') or 0) / 1000:.1f} ms"
                                                       for k, v in sorted(bl.items(), key=lambda x: -x[1]["count"])[:3]))
        nr = extra["probes"].get("name_resolution")
        if nr:
            log(f"   eBPF probes: name resolution {nr['calls']} calls, {nr['seconds']:.3f} s in the window")
    if goal_value is not None and sess.get("goal_metric") != prof.goal_metric:
        sess["goal_metric"] = prof.goal_metric
        c.state.set("session", sess)
        try:
            c.client.post(f"/sessions/{sess['id']}/update", {"goal_metric": prof.goal_metric})
        except Exception:
            pass
    return _measure_stop(c, metrics, goal_value, notes, b_snap=b_snap, extra_inputs=extra, goal_source=goal_src)


def cmd_measure(a):
    c = Ctx()
    if a.action == "start":
        m, warn, url = _measure_start(c, a.label, a.notes)
        emit({"ok": True, "measurement": m, "warning": warn, "run_url": url},
             f"Measuring run {m['seq']} '{m['label']}' (run #{m['run_id']}). Stop with `tuning-agent measure stop`.")
    elif a.action == "stop":
        run, diag = _measure_stop(c, _parse_kv(a.external), a.goal_value, a.notes)
        emit({"ok": True, "run": run, "diagnosis": diag}, _fmt_run(run, diag, c.session()["goal"]))
    elif a.action == "abort":
        m = c.state.get("measurement")
        if not m:
            raise Fail("no measurement in progress")
        c.client.post(f"/runs/{m['run_id']}/abort", {"reason": "aborted with `tuning-agent measure abort`" + (f": {a.notes}" if a.notes else "")})
        c.state.data["pending_changes"] = m.get("changes", []) + (c.state.get("pending_changes") or [])
        c.state.data["measurement"] = None
        c.state.save()
        c.event("measure_abort", f"measurement of run {m['seq']} aborted", {})
        emit({"ok": True, "aborted": m["run_id"]}, f"Measurement of run {m['seq']} aborted.")
    elif a.action == "run" and a.bench:
        run, diag = _bench_run(c, bench.Profile(a.bench), a.label, a.notes, a.warmup)
        emit({"ok": True, "run": run, "diagnosis": diag}, _fmt_run(run, diag, c.session()["goal"]))
    elif a.action == "run":
        warm = a.warmup
        if warm is None:
            uptime = int(float(c.db.status().get("Uptime", 1e9)))
            warm = "auto" if uptime < 600 else 0
            if warm:
                log(f"server up only {uptime}s (restarted?): using --warmup auto")
        waited = _warmup(c, warm)
        m, warn, url = _measure_start(c, a.label, a.notes, warmed=bool(waited))
        log(f"measuring run {m['seq']} for {a.duration}s ...")
        interrupted = False
        try:
            time.sleep(a.duration)
        except KeyboardInterrupt:
            interrupted = True
        run, diag = _measure_stop(c, _parse_kv(a.external), a.goal_value, a.notes)
        emit({"ok": True, "run": run, "diagnosis": diag, "interrupted": interrupted}, _fmt_run(run, diag, c.session()["goal"]))


def _noise_key(sess):
    return str(sess.get("bench_name") or sess.get("workload") or sess.get("name") or "")[:120]


def _prior_cv(c, sess):
    """Long-term goal CV measured by earlier sessions of the same benchmark on this environment (median of the last 5).
    Entries from before 2026-10-22 (v < 2) pooled selected first runs into the CV (winner's curse) and are ignored."""
    hist = [h for h in ((c.state.get("noise_history") or {}).get(_noise_key(sess)) or []) if h.get("v", 1) >= 2]
    vals = sorted(h["cv"] for h in hist[-5:])
    return vals[len(vals) // 2] if vals else None


def _refresh_noise(c, sess, runs):
    """Re-estimate the goal noise threshold from all repeats so far (variance is environment and workload specific)."""
    if not sess.get("noise"):
        return
    # calibration runs the calibration itself set aside (a cold first run before the slide was detected) stay out of
    # every later estimate too (user session 194: back in, they took the threshold from ±5.1% to ±11.3%)
    cal_runs = set((sess.get("noise") or {}).get("runs") or [])
    if cal_runs:
        runs = [r for r in runs if not (str(r.get("label") or "").startswith("baseline") and r["seq"] not in cal_runs
                                        and r["seq"] < max(cal_runs))]
    est = evaluate.noise_estimate(runs, _prior_cv(c, sess))
    if not est:
        return
    old = sess["noise"].get("noise_pct")
    sess["noise"]["noise_pct"] = est["noise_pct"]
    sess["noise"]["estimate"] = est
    sess["noise_pct"] = est["noise_pct"]
    c.state.set("session", sess)
    if old != est["noise_pct"]:
        log(f"   noise threshold ±{old}% -> ±{est['noise_pct']}% (pooled CV {est['pooled_cv_pct']}%, {est['basis']})")
        try:
            c.client.post(f"/sessions/{sess['id']}/update", {"noise_pct": est["noise_pct"], "calibration": sess["noise"]})
        except Exception:
            pass
        if old and est["noise_pct"] < old:
            # a tighter threshold can turn earlier "neutral" runs into real effects (C5 thread pool +4.8% at ±6.3%)
            ch = [x for x in _reevaluate(c, c.session()) if x[2] != x[1]]
            if ch:
                log("   re-judged with the tighter threshold: " + ", ".join(f"run {s}: {a} -> {b}" for s, a, b in ch))


def _remember_noise(c, sess):
    """At session end: keep this environment + benchmark's long-term variance as the prior for the next session."""
    est = (sess.get("noise") or {}).get("estimate") or {}
    if est.get("long_term_dof"):
        h = c.state.data.setdefault("noise_history", {}).setdefault(_noise_key(sess), [])
        h.append({"cv": est["pooled_cv_pct"], "dof": est["dof"], "session": sess.get("id"), "t": time.time(), "v": 2})
        del h[:-10]
        c.state.save()


def _set_calibration(c, sess, cal):
    est = evaluate.noise_estimate([{"seq": i, "goal_value": v, "managed": {}} for i, v in enumerate(cal.get("_goal_values") or [])],
                                  _prior_cv(c, sess)) if cal.get("_goal_values") else None
    if est:
        cal["noise_pct"] = est["noise_pct"]
        cal["estimate"] = est
    cal.pop("_goal_values", None)
    sess["noise"] = cal
    sess["noise_pct"] = cal["noise_pct"]
    c.state.set("session", sess)
    c.client.post(f"/sessions/{sess['id']}/update", {"noise_pct": cal["noise_pct"], "calibration": cal})
    c.event("calibrate", f"noise calibrated from runs {cal['runs']}: goal CV {cal['goal_cv_pct']}% -> threshold ±{cal['noise_pct']}%", cal)


SEASON_SAMPLE_S = 300


def _datadir_free_pct(c):
    try:
        st = os.statvfs(c.datadir())
        return 100.0 * st.f_bavail / st.f_blocks if st.f_blocks else None
    except Exception:
        return None


def _season(c, prof):
    """data_state = seasoned: run the benchmark (unmeasured) until its throughput stops trending - the data set has
    reached a steady state, so a series of tuning runs will not move it much - or until season_hours. One continuous run
    sampled every 5 minutes when the profile can set its length (repeated iterations otherwise); evaluate.season_check
    decides from the measured noise. Throughput = server-side transactions (Com_commit + Com_rollback) per second."""
    t0 = time.time()
    limit, need = prof.season_hours * 3600, prof.season_min_hours * 3600
    run_h = ((int(prof.warmup) if str(prof.warmup).isdigit() else 0) + prof.duration) / 3600
    span_h = max(1.0, 4 * run_h)  # drift has to stay small between re-checks of the current configuration (~4 runs)
    continuous = prof.can_resize()
    log(f"== seasoning the data set: {'one continuous benchmark run' if continuous else 'benchmark iterations'}, throughput every "
        f"{SEASON_SAMPLE_S // 60} min, until the trend is within what {span_h:.1f} h between re-checks tolerate given the measured "
        f"noise (at least {prof.season_min_hours:g} h, at most {prof.season_hours:g} h)")
    pts = []  # (hours since start, tps)
    logdir = os.path.join(c.state.dir, "bench")
    os.makedirs(logdir, exist_ok=True)
    chk = {}
    if prof.reset:
        log("   note: the profile also has a reset command; seasoning runs once, before any reset")
    # the benchmark's own prerequisites (max_connections, max_prepared_stmt_count ...): seasoning runs the same benchmark
    # (user report, POP2: it failed on both hosts until they were set by hand)
    _ensure_requirements(c, prof)
    zero, quick = 0, 0

    def failing(why, r):
        tail = (r.output() or "")[-1500:] if r else ""
        free = _datadir_free_pct(c)
        c.event("season", f"seasoning stopped: {why}", {"tail": tail[-800:], "datadir_free_pct": free})
        raise Fail(f"seasoning stopped: {why}" + (f" (data disk {free:.1f}% free)" if free is not None and free < 10 else "")
                   + (f"; benchmark output:\n{tail}" if tail else ""))

    def commits():
        st = c.db.status()
        return sum(float(st.get(k) or 0) for k in ("Com_commit", "Com_rollback")), time.time()

    while time.time() - t0 < limit and not chk.get("stop"):
        left = limit - (time.time() - t0)
        r = bench.Running(prof.command(total_s=int(left) + 60) if continuous else prof.command(), os.path.join(logdir, "season.log"))
        a, ta = commits()
        first = True
        while r.alive() and time.time() - t0 < limit:
            time.sleep(5)
            if time.time() - ta < SEASON_SAMPLE_S:
                continue
            b, tb = commits()
            rate = (b - a) / max(tb - ta, 1)
            if not (first and not continuous):  # an iteration's first sample holds its ramp-up
                pts.append(((tb - t0) / 3600, rate))
            first, a, ta = False, b, tb
            # a benchmark that is running but doing nothing (every statement failing: missing prerequisites, a full disk)
            # is not seasoning: two samples in a row with no commits stop it with the benchmark's own error output
            zero = zero + 1 if rate < 0.5 else 0
            if zero >= 2:
                failing(f"no transactions for {2 * SEASON_SAMPLE_S // 60} min while the benchmark runs", r)
            free = _datadir_free_pct(c)
            if free is not None and free < 2:
                failing(f"the data disk is full ({free:.1f}% free)", r)
            h = pts[-1][0] if pts else 0
            c.activity("seasoning", f"steady-state pre-run, {h:.1f} of max {prof.season_hours:g} h"
                       + (f", trend {chk['trend_pct_per_hour']:+.2f}%/h (allowed {chk['allowed_pct_per_hour']:.2f})" if chk.get("trend_pct_per_hour") is not None else ""), None)
            if pts and time.time() - t0 >= need:
                chk = evaluate.season_check(pts, span_h, prof.duration / 3600)
                if pts and len(pts) % 6 == 0 or chk.get("stop"):
                    log(f"   {h:5.2f} h: {pts[-1][1]:.1f} tps" + (f"; trend {chk['trend_pct_per_hour']:+.2f}%/h +/-{chk['trend_se_pct']:.2f}, "
                        f"noise {chk['noise_pct']:.2f}%, allowed {chk['allowed_pct_per_hour']:.2f}%/h" if "trend_pct_per_hour" in chk else ""))
                if chk.get("stop"):
                    break
        if r.alive():
            r.kill()
        elif not chk.get("stop") and time.time() - t0 < limit:
            # the benchmark ended by itself: an iteration that fails or ends within 2 min, three times in a row, is a
            # benchmark that cannot run (it was retried for an hour with zero throughput)
            rc = r.p.returncode
            quick = quick + 1 if (rc not in (0, None) or time.time() - r.t0 < 120) else 0
            if quick >= 3:
                failing(f"the benchmark exited {quick} times in a row (exit code {rc})", r)
    steady = bool(chk.get("stop"))
    slope = chk.get("trend_pct_per_hour")
    if slope is None and len(pts) >= 3:
        slope = evaluate.trend_pct([p[1] for p in pts[-12:]], [p[0] for p in pts[-12:]])
    res = {"hours": round((time.time() - t0) / 3600, 2), "iterations": len(pts), "samples": len(pts), "continuous": continuous,
           "steady": steady, "stop_reason": chk.get("reason") if steady else "time limit", "check": chk, "span_h": round(span_h, 2),
           "trend_pct_per_hour": round(slope, 2) if slope is not None else None, "series": [[round(h, 3), round(v, 2)] for h, v in pts]}
    sess = c.session()
    sess["seasoning"] = res
    c.state.set("session", sess)
    txt = (f"Data set seasoned for {res['hours']} h ({len(pts)} throughput samples): "
           + (f"steady - {chk.get('text')}" if steady else "still trending at the time limit")
           + (f"; trend {slope:+.2f}%/h" if slope is not None and not steady else "")
           + (f"; noise {chk['noise_pct']:.2f}%, allowed trend {chk['allowed_pct_per_hour']:.2f}%/h" if chk.get("noise_pct") is not None else ""))
    log("   " + txt)
    c.event("season", txt, res)
    return res


def _queue_promising_retests(c):
    """Changes that beat the measured variation but not the conservative floor threshold (which holds until a re-check of
    the same configuration measures drift) were reverted as neutral (session 294: os-autogroup +0.7% at CV 0.21% against
    the 2% floor). They are remembered; once the noise estimate has time-separated repeats, each is measured again with
    repeats. Returns True when it queued something."""
    sess = c.session()
    seen = set(sess.get("promising_runs") or [])
    for r in sess["runs"]:
        if (r.get("evaluation") or {}).get("promising") and r["seq"] not in seen:
            seen.add(r["seq"])
    queued = set(sess.get("promising_queued") or [])
    sess["promising_runs"] = sorted(seen)
    if not (sess.get("noise") or {}).get("long_term_dof"):
        c.state.set("session", sess)
        return False
    cur = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    did = False
    for seq in sorted(seen - queued):
        r = next((x for x in sess["runs"] if x["seq"] == seq), None)
        own = [x for x in (r or {}).get("changes") or [] if str(x.get("source", "")).startswith("rec:")
               and not str(x["source"])[4:].startswith(("revert", "rollback", "drop"))]
        queued.add(seq)
        if not own or all(str(cur.get(x["variable"])) == str(x.get("new")) for x in own):
            continue
        settings = ",".join(f"{x['variable']}={x.get('new')}" for x in own)
        d = ((r.get("evaluation") or {}).get("promising") or {}).get("goal_delta_pct")
        try:
            c.client.post(f"/sessions/{sess['id']}/experiments", {"kind": "try", "settings": settings, "repeats": 2,
                          "reason": f"re-test of run {seq} ({r.get('label')}): {d:+.2f}% beat the measured variation but not the "
                                    "floor threshold; a re-check has measured drift since", "created_by": "agent:retest-promising"})
            log(f"== re-test queued: run {seq} {r.get('label')} ({settings}), 2 runs")
            did = True
        except Exception as e:  # the re-test is a bonus, never a reason to stop
            log(f"note: could not queue the re-test of run {seq}: {e}")
    sess["promising_queued"] = sorted(queued)
    c.state.set("session", sess)
    return did


def _post_run_plan(c, sess):
    """The run length chosen for the session, shown in the session header (user: where do I see it?)."""
    try:
        c.client.post(f"/sessions/{sess['id']}/update", {"run_plan": sess.get("run_plan")})
    except Exception as e:  # noqa
        log(f"warning: could not record the run-length plan: {e}")


def _review_run_plan(c, runs):
    """How the run-length probe's predictions held up in calibration (kept for the data review): the measured within-run
    noise against the predicted one, and whether the warm-up reached the state the window measured."""
    sess = c.session()
    plan = sess.get("run_plan")
    if not plan or plan.get("review"):
        return
    ses = [(r.get("summary") or {}).get("within_se_pct") for r in runs]
    ses = [x for x in ses if x]
    measured = round(evaluate.NOISE_K * (sum(x * x for x in ses) / len(ses)) ** 0.5, 2) if ses else None
    wu = (sess.get("noise") or {}).get("warmup") or {}
    review = {"predicted_within_noise_pct": plan.get("predicted_within_noise_pct"), "measured_within_noise_pct": measured,
              "calibration_noise_pct": (sess.get("noise") or {}).get("noise_pct"),
              "warmup_adequate": bool(wu.get("adequate", True)) and not wu.get("settling_runs"),
              "warmup_note": wu.get("text")}
    plan["review"] = review
    sess["run_plan"] = plan
    c.state.set("session", sess)
    _post_run_plan(c, sess)
    txt = (f"run-length plan held up: within-run noise ±{measured}% measured vs ±{plan.get('predicted_within_noise_pct')}% predicted; "
           f"warm-up {'adequate' if review['warmup_adequate'] else 'NOT adequate'}")
    log("   " + txt)
    c.event("run_length_review", txt, review)


def _noise_plan(c, prof, runs, n):
    """After calibration: is the noise threshold small enough to see typical gains, and what would reduce it?
    Longer runs only when the noise is mostly within-run (they do not remove run-to-run shifts); then the earlier
    calibration runs are superseded (different run length) and the start configuration is calibrated again."""
    sess = c.session()
    cal = sess["noise"]
    ds = evaluate.data_state_check(runs, prof.data_state)
    cal["data_state"] = ds
    wu = evaluate.warmup_check(runs, int(prof.warmup) if str(prof.warmup).isdigit() else None)
    if wu:
        cal["warmup"] = wu
        if wu.get("adequate") and (wu.get("settling_runs") or wu.get("unbounded")):
            log(f"   WARNING: {wu['text']}")
            c.event("unsustainable" if wu.get("unbounded") else "warmup", wu["text"], wu)
        if not wu["adequate"]:
            log(f"   WARNING: {wu['text']}")
            c.event("warmup", wu["text"], wu)
            base = int(getattr(prof, "base_warmup", prof.warmup) if str(prof.warmup).isdigit() else 0)
            cap = max(3 * base, 3600)  # up to an hour: large I/O-bound sets take long to reach steady state
            if prof.adaptive and prof.can_resize() and base and int(prof.warmup) < cap:
                new = min(cap, int(prof.warmup) * 2)
                sess["warmup_seconds"] = new
                wu["applied_warmup_s"] = new
                note = f"Measured with a {prof.warmup} s warm-up that did not reach steady state; superseded by calibration with {new} s."
                for r in sess["runs"]:
                    if r["seq"] in {x["seq"] for x in runs}:
                        r["superseded"] = note
                sess["noise"] = None
                c.state.set("session", sess)
                _reevaluate(c, sess)
                prof.warmup = str(new)
                log(f"== recalibration with a {new} s warm-up")
                c.activity("calibrating", f"{n} runs with a {new} s warm-up (steady state)")
                new_runs = []
                while len([r for r in new_runs if r.get("verdict") not in evaluate.EXCLUDED]) < n and len(new_runs) < 2 * n:
                    run, _ = _bench_run(c, prof, f"baseline warm-up {new}s ({len(new_runs) + 1}/{n})")
                    new_runs.append(run)
                new_runs = [r for r in new_runs if r.get("verdict") not in evaluate.EXCLUDED] or new_runs
                sess = c.session()
                cal = evaluate.calibrate(new_runs)
                _set_calibration(c, sess, cal)
                _reevaluate(c, c.session())
                sess = c.session()
                cal = sess["noise"]
                cal["warmup"] = dict(evaluate.warmup_check(new_runs, new) or {}, previous=wu)
                runs = new_runs
    if ds["status"] == "drifting":
        log(f"   WARNING: {ds['text']}")
        c.event("data-state", ds["text"], ds)
    plan = evaluate.noise_plan(runs, cal, prof.duration, prof.max_duration)
    if ds["status"] == "drifting" and plan["action"] in ("between-runs", "longer-runs"):
        plan["text"] += " Likely cause: the data set is not restored between runs (set reset = ..., or data_state = seasoned)."
    cl = [x for x in (evaluate.client_saturated(r.get("summary") or {}) for r in runs) if x]
    if cl and str(c.cfg.get("allow_noisy_environment", "false")).lower() != "true":
        msg = (f"environment unfit for tuning: the benchmark client limits the measurement ({cl[0]}, {len(cl)} of {len(runs)} "
               "calibration runs) - no server setting can show an effect. Use a bigger client host or a lighter client "
               "(or set allow_noisy_environment = true in agent.ini).")
        c.event("environment", msg, {"client_saturated": cl})
        raise Fail(msg)
    dist = evaluate.disturbances(runs)
    if dist:
        plan["disturbances"] = dist
        plan["text"] += " Measured in the calibration runs: " + "; ".join(dist) + "."
    cal["plan"] = plan
    # an unsteady window is the benchmark's run length (the warm-up check above lengthens it), not the environment
    env_dist = [d for d in dist if "steady state" not in d]
    if plan["action"] == "between-runs" and env_dist and plan.get("repeats", 2) > 2 \
            and str(c.cfg.get("allow_noisy_environment", "false")).lower() != "true":
        # tuning on top of a measured disturbance only finds noise (session 88: mysqld swapping 2.3 GB back and forth,
        # noise growing from 10% to 40%, 50 runs that could not separate any setting): fix the environment first
        _set_calibration(c, sess, cal)
        msg = ("environment unfit for tuning: run-to-run noise ±" + str(cal.get("noise_pct")) + "% hides changes smaller than that, "
               "and the calibration runs show why - " + "; ".join(env_dist) + ". Fix it (or set allow_noisy_environment = true in agent.ini).")
        c.event("environment", msg, plan)
        raise Fail(msg)
    if plan["action"] == "longer-runs" and not (prof.adaptive and prof.can_resize()):
        plan["applied"] = False
        plan["text"] += (" Not applied: adaptive_duration = off." if not prof.adaptive else
                         " Not applied: the run command has a fixed length (use {duration} / {total} placeholders in the profile).")
    _set_calibration(c, sess, cal)
    log(f"   noise plan: {plan['text']}")
    c.event("noise-plan", plan["text"], plan)
    if plan["action"] != "longer-runs" or plan.get("applied") is False:
        return runs
    old = prof.duration
    sess = c.session()
    sess["run_seconds"] = plan["seconds"]
    note = f"Measured with {old // 60} min runs; superseded by calibration with {plan['seconds'] // 60} min runs (noise plan)."
    for r in sess["runs"]:
        if r["seq"] in {x["seq"] for x in runs}:
            r["superseded"] = note
    sess["noise"] = None
    c.state.set("session", sess)
    _reevaluate(c, sess)
    c.activity("calibrating", f"{n} runs at {plan['seconds'] // 60} min (longer runs to reduce noise)")
    log(f"== recalibration: {n} runs of {plan['seconds'] // 60} min")
    new = []
    while len([r for r in new if r.get("verdict") not in evaluate.EXCLUDED]) < n and len(new) < 2 * n:
        run, _ = _bench_run(c, prof, f"baseline {plan['seconds'] // 60}min ({len(new) + 1}/{n})")
        log(f"   run {run['seq']}: goal {run['goal_value']}")
        new.append(run)
    new = [r for r in new if r.get("verdict") not in evaluate.EXCLUDED] or new
    sess = c.session()
    cal = evaluate.calibrate(new)
    _set_calibration(c, sess, cal)
    _reevaluate(c, c.session())
    log(_fmt_cal(cal))
    sess = c.session()
    after = evaluate.noise_plan(new, sess["noise"], prof.duration, prof.max_duration)
    after["previous"] = {k: plan.get(k) for k in ("noise_pct", "run_seconds", "within_cv_pct", "between_cv_pct")}
    after["applied"] = True
    if after["action"] == "longer-runs":  # one adjustment per session: report, do not loop
        after["text"] = f"Runs lengthened to {prof.duration // 60} min (threshold ±{plan['noise_pct']}% -> ±{sess['noise']['noise_pct']}%)."
    sess["noise"]["plan"] = after
    _set_calibration(c, sess, sess["noise"])
    log(f"   noise plan after recalibration: {after['text']}")
    return new


def _fmt_cal(cal):
    lines = [f"Calibration from runs {cal['runs']}: goal mean {cal['goal_mean']:.4g}, CV {cal['goal_cv_pct']}%, range {cal['goal_range_pct']}%",
             f"  goal noise threshold: ±{cal['noise_pct']}% ({(cal.get('estimate') or {}).get('basis') or str(cal['k']) + ' x CV'})"]
    for k, e in cal["efficiency"].items():
        lines.append(f"  {evaluate.EFFICIENCY[k]}: mean {e['mean']:.4g}, CV {e['cv_pct']}% -> threshold ±{e['noise_pct']}%")
    return "\n".join(lines)


TRIAGE_SEG_S = 300       # each remedy is measured this long (the first TRIAGE_SETTLE_S not counted): a remedy shows in minutes
TRIAGE_SETTLE_S = 60
TRIAGE_RESTART_LEAD_S = 120  # after a mysqld restart: the benchmark reconnects and warms up before the step is measured
TRIAGE_HELP = 0.5        # a partial remedy: growth per committed transaction at most half the reference's
TRIAGE_FIX = 0.02        # stable = bounded: the history flat (growth at most 2% of the reference) or shrinking. A quarter was
                         # not enough: concurrency 8 on the canary cut 216k/min to 22.5k/min - still +1.35M an hour (user)
TRIAGE_FREE_PCT = 5.0    # a stable remedy costing at most this much throughput ends the triage early: nothing cheaper to find
TRIAGE_BUDGET_S = 7200
PROVISIONAL_WINDOW_S = 600  # measurement length while no stable configuration exists


def _segment(c, t0, t1):
    """History growth per minute and mean throughput of one triage step (samples from t0 to t1)."""
    smp = SampleStore(c.state.dir).window(t0, t1)
    pts = [(x.get("ts") or 0, x["db"]["history_list_length"]) for x in smp if (x.get("db") or {}).get("history_list_length") is not None]
    tps = [x["db"]["tps"] for x in smp if (x.get("db") or {}).get("tps") is not None]
    sl = collector._slope(pts)
    return {"hll_per_min": round(sl * 60) if sl is not None else None, "hll_end": int(pts[-1][1]) if pts else None,
            "tps": round(sum(tps) / len(tps), 1) if tps else None}


def _per_trx(seg):
    """History growth per committed transaction in a triage step: what purge fails to remove, independent of throughput."""
    if seg.get("hll_per_min") is None or not seg.get("tps"):
        return None
    return seg["hll_per_min"] / (seg["tps"] * 60.0)


def _triage_candidates(c):
    """Purge remedies to try, each measured on its own: [{label, changes: [(var, value)], restart, last}]. Online ones
    first (cheaper to try), then the ones that need a mysqld restart - the thread pool, more purge threads at a higher
    priority - and the purge lag bound last: a stopgap that throttles every writer, and one that cannot act while purge
    is stalled (MySQL computes the delay at the start of each purge batch)."""
    from .knowledge import dynamic_in, series_of
    from .mysqldb import flavor_of
    from . import resgroups
    v = c.db.variables()
    fl = flavor_of(v)[0]
    series = series_of(fl, v.get("version")) if v.get("version") else None
    cpus = os.cpu_count() or 1
    out = []

    def add(label, changes, restart=False, last=False):
        out.append({"label": label, "changes": changes, "restart": restart, "last": last})

    rg = resgroups.available(c.db)
    cap = resgroups.has_cap_sys_nice() if rg else None
    try:
        can_cap = rg and cap is False and c.os_tuning_allowed() and "os:mysqld_cap_sys_nice" in (c.os_settings() or {})
    except Exception:
        can_cap = False
    prio = ([("rg:purge", "prio=-10")] if rg and cap else [("os:mysqld_cap_sys_nice", "ON"), ("rg:purge", "prio=-10")] if can_cap else [])
    if prio:
        add("purge threads at a higher priority", prio, restart=not cap)
    bs = int(v.get("innodb_purge_batch_size") or 300)
    for x in (1000, 5000):
        if x > bs:
            add(f"purge batch size {x}", [("innodb_purge_batch_size", str(x))])
    if "innodb_thread_concurrency" in v and int(v.get("innodb_thread_concurrency") or 0) == 0 and fl != "mariadb":
        # to control purge the cap has to be low - at or below the cores - so purge gets CPU and locks between user threads
        for x in sorted({cpus, max(2, cpus // 2)}, reverse=True):
            add(f"innodb_thread_concurrency {x}", [("innodb_thread_concurrency", str(x))])
    tp_on = str(v.get("thread_handling") or "").lower() == "pool-of-threads"
    if "thread_pool_size" in v and not tp_on:
        add("thread pool", [("thread_handling", "pool-of-threads"), ("thread_pool_size", str(cpus))], restart=True)
        if "thread_pool_oversubscribe" in v:
            add("thread pool, oversubscribe 1", [("thread_handling", "pool-of-threads"), ("thread_pool_size", str(cpus)),
                                                 ("thread_pool_oversubscribe", "1")], restart=True)
    elif tp_on and int(v.get("thread_pool_oversubscribe") or 3) > 1:
        add("thread pool oversubscribe 1", [("thread_pool_oversubscribe", "1")])
    pt = int(v.get("innodb_purge_threads") or 4)
    pt_online = bool(dynamic_in("innodb_purge_threads", series))
    for x in sorted({min(32, max(4, pt * 2)), min(32, max(8, cpus // 2)), 32}):
        if x > pt:
            # the maximum (32, InnoDB's cap) at the highest priority: the fastest purge InnoDB can run - the upper bound of
            # what purge alone can do here (user, 2026-10-25)
            top = x == 32 and bool(prio)
            p_ = [(k, "prio=-20") if k == "rg:purge" else (k, v_) for k, v_ in prio] if top else prio
            add(f"{x} purge threads" + (" at the highest priority" if top else " at a higher priority" if prio else ""),
                [("innodb_purge_threads", str(x))] + p_, restart=not pt_online or (bool(prio) and not cap))
    if "innodb_max_purge_lag" in v and int(v.get("innodb_max_purge_lag") or 0) == 0:
        add("purge lag bound (stopgap)", [("innodb_max_purge_lag", "__HLL__"), ("innodb_max_purge_lag_delay", "300000")], last=True)
    return out


RESTART_VARS = ("thread_handling", "os:mysqld_cap_sys_nice")


def _purge_triage(c, running, deadline, start_bench=None, measure_all=False):
    """Find a configuration whose undo history stays bounded before anything is measured for long. The benchmark keeps
    running; each remedy is measured on its own for TRIAGE_SEG_S (after a restart, the benchmark is started again and
    warms up first): history growth per minute and throughput. A remedy that brings the growth down to TRIAGE_FIX of the
    reference (or makes the history shrink) is stable; among stable ones the fastest wins - kept whatever it costs
    (stability first, then performance). Without one: the two best partial remedies (growth per transaction halved)
    together, then the purge lag bound. start_bench() starts the benchmark again after a restart (None: online remedies
    only). Returns {"stable", "kept", "steps", "running"}; the winner stays applied, recorded as the session's start."""
    cands = _triage_candidates(c)
    if start_bench is None:
        cands = [x for x in cands if not x["restart"]]
    steps = []
    prev = {k: x.get("value") for k, x in c.state["managed"].items()}  # back to the managed value, or unmanaged
    trial = {}  # settings the triage has in effect now

    def needs_restart(changes):
        return any(k in RESTART_VARS or (k == "innodb_purge_threads" and not _triage_online(c, k))
                   or (k.startswith("thread_pool_") and str(c.db.variables().get("thread_handling") or "") != "pool-of-threads"
                       and dict(changes).get("thread_handling") == "pool-of-threads") for k, _ in changes)

    def switch(changes, label):
        nonlocal running, trial
        want = dict(changes)
        undo = {k: prev.get(k) for k in trial if k not in want}
        diff = [(k, x) for k, x in list(undo.items()) + list(want.items()) if trial.get(k, prev.get(k)) != x or k in undo]
        if not diff:
            return
        restart = needs_restart(list(want.items())) or needs_restart([(k, trial[k]) for k in undo])
        if restart and running.alive():
            running.kill()
        _apply_and_record(c, [{"variable": k, "value": x, "reason": f"purge triage: {label}"} for k, x in diff], "stability-triage",
                          restart=restart)
        trial = want
        if restart:
            c.db.connect(retries=30, delay=2)
            running = start_bench()
            c.activity("purge triage", f"{label}: benchmark warming up after the restart")
            t = time.time()
            while time.time() - t < TRIAGE_RESTART_LEAD_S and running.alive():
                time.sleep(10)

    def measure(label, changes):
        t0 = time.time()
        c.activity("purge triage", f"{label} ({TRIAGE_SEG_S // 60} min)")
        while time.time() - t0 < TRIAGE_SEG_S and running.alive():
            time.sleep(15)
        seg = _segment(c, t0 + TRIAGE_SETTLE_S, time.time())
        seg.update(label=label, changes=[f"{k}={x}" for k, x in changes])
        steps.append(seg)
        return seg

    log(f"== purge triage: the history list grows past {engine.PURGE_UNBOUNDED_MIN:,} without slowing down; each remedy "
        f"{TRIAGE_SEG_S // 60} min on the running benchmark (restart remedies after a {TRIAGE_RESTART_LEAD_S // 60}-min warm-up)")
    ref = measure("current configuration", [])
    if not ref["hll_per_min"] or ref["hll_per_min"] <= 0:
        log(f"   history not growing now ({ref['hll_per_min']}/min): nothing to fix")
        return {"stable": True, "kept": [], "steps": steps, "running": running}
    log(f"   reference: history +{ref['hll_per_min']:,}/min at {ref['tps']} tps")
    c.event("purge_triage", f"purge triage: history +{ref['hll_per_min']:,}/min at {ref['tps']} tps on the current configuration; "
            f"trying {', '.join(x['label'] for x in cands) or 'nothing (no remedy available here)'}, {TRIAGE_SEG_S // 60} min each",
            dict(ref, steps_planned=[x["label"] for x in cands]))

    def judge(cand):
        changes = [(k, str(max(engine.PURGE_UNBOUNDED_MIN, int(ref.get("hll_end") or 0))) if x == "__HLL__" else x)
                   for k, x in cand["changes"]]
        switch(changes, cand["label"])
        seg = measure(cand["label"], changes)
        if any(k == "rg:purge" for k, _ in changes):  # did the kernel really give the purge threads the priority?
            from . import resgroups
            try:
                seg["purge_nice"] = sorted(set(resgroups.thread_nice(c.db, resgroups.PURGE_THREADS)))
            except Exception:
                pass
        cost = 100.0 * (ref["tps"] - seg["tps"]) / ref["tps"] if ref.get("tps") and seg.get("tps") is not None else 0.0
        stable = seg["hll_per_min"] is not None and seg["hll_per_min"] <= max(0, TRIAGE_FIX * ref["hll_per_min"])
        # partial: judged per committed transaction - lower growth from lower throughput is no purge improvement
        # (env 56: batch size 1000 "cut" growth 40% at -38% tps, the same ~1 undo log per transaction)
        partial = not stable and _per_trx(seg) is not None and _per_trx(ref) and _per_trx(seg) <= TRIAGE_HELP * _per_trx(ref)
        seg.update(cost_pct=round(cost, 1), result="stable" if stable else "partial" if partial else "no effect", _changes=changes)
        log(f"   {cand['label']:42} history {seg['hll_per_min']:+,}/min, {seg['tps']} tps ({-cost:+.1f}%): {seg['result']}"
            + (f" (purge threads' nice: {seg['purge_nice']})" if "purge_nice" in seg else ""))
        c.event("purge_triage", f"purge triage step: {cand['label']} ({', '.join(f'{k}={x}' for k, x in changes)}): history "
                f"{seg['hll_per_min']:+,}/min vs {ref['hll_per_min']:+,}/min, {seg['tps']} tps ({-cost:+.1f}%) - {seg['result']}",
                dict({k: x for k, x in seg.items() if k != "_changes"}, reference=ref))
        return seg

    def time_left(cand):
        return time.time() + TRIAGE_SEG_S + (TRIAGE_RESTART_LEAD_S + 120 if cand["restart"] else 0) <= deadline

    results = []
    for cand in [x for x in cands if not x["last"]]:
        if not time_left(cand) or not running.alive():
            break
        seg = judge(cand)
        results.append(seg)
        if seg["result"] == "stable" and seg["cost_pct"] <= TRIAGE_FREE_PCT and not measure_all:
            break  # bounded at (almost) no cost: nothing better to find
    stable = [x for x in results if x["result"] == "stable"]
    if not stable:
        # the best partial remedy combined with each other partial one that sets other variables (e.g. a concurrency cap
        # with more purge threads at a higher priority): a lower growth from both sides may bound it
        part = sorted([x for x in results if x["result"] == "partial"], key=lambda x: _per_trx(x))
        # ... and with the purge-speed remedies even when they did nothing alone: with every CPU taken by user threads,
        # more purge threads or a higher priority cannot act until a cap frees room for them (canary 2026-10-25)
        speed = [x for x in results if x["result"] == "no effect" and any(k in ("innodb_purge_threads", "rg:purge") for k, _ in x["_changes"])]
        speed.sort(key=lambda x: -int(dict(x["_changes"]).get("innodb_purge_threads") or 0))
        for other in part[1:] + speed:
            if stable or not part:
                break
            if set(dict(part[0]["_changes"])) & set(dict(other["_changes"])):
                continue
            both = {"label": f"{part[0]['label']} + {other['label']}", "changes": part[0]["_changes"] + other["_changes"],
                    "restart": True, "last": False}
            if time_left(both) and running.alive():
                seg = judge(both)
                results.append(seg)
                if seg["result"] == "stable":
                    stable = [seg]
    if not stable:
        for cand in [x for x in cands if x["last"]]:
            if time_left(cand) and running.alive():
                seg = judge(cand)
                if seg["result"] == "stable":
                    stable = [seg]
    win = max(stable, key=lambda x: x["tps"] or 0) if stable else None
    switch(win["_changes"] if win else [], "the stable configuration" if win else "back to the starting configuration")
    kept = win["_changes"] if win else []
    for x in steps:
        x.pop("_changes", None)
    txt = (f"purge triage: stable with {', '.join(f'{k}={x}' for k, x in kept) or 'the current configuration'} "
           f"({win['tps']} tps, {-win['cost_pct']:+.1f}% vs the start)" if win else
           "purge triage: no remedy bounds the undo history under this load (" + ", ".join(
               f"{x['label']}: {x['result']} ({x['hll_per_min']:+,}/min)" for x in results)
           + "); the configuration cannot run this load in production")
    log("   " + txt)
    c.event("purge_triage", txt, {"stable": bool(win), "kept": kept, "steps": steps, "reference": ref})
    return {"stable": bool(win), "kept": kept, "steps": steps, "running": running}


def _triage_online(c, var):
    from .knowledge import dynamic_in, series_of
    from .mysqldb import flavor_of
    v = c.db.variables()
    return bool(dynamic_in(var, series_of(flavor_of(v)[0], v.get("version")) if v.get("version") else None))


def _run_length_probe(c, prof, max_s=3600, target_pct=None):
    """One continuous run from the benchmark's clean start until every state metric has settled and held its level
    (warm-up), then the measurement length that keeps the within-run noise inside half the target noise threshold."""
    target_pct = target_pct or evaluate.NOISE_TARGET
    if c.state.get("measurement"):
        raise Fail("a measurement is in progress")
    if not prof.can_resize():
        raise Fail("the profile's run command has no {total}/{duration} placeholder: its length cannot be set for one long run")
    logdir = os.path.join(c.state.dir, "bench")
    os.makedirs(logdir, exist_ok=True)
    logfile = os.path.join(logdir, f"run-length-probe-{int(time.time())}.log")
    if prof.restart_before_run and (not prof.reset or prof.data_state in ("read-only", "seasoned")):
        log("clean start: restarting mysqld and dropping the OS page cache")
        ok, err = Applier(c.cfg, c.state, c.db, log=log).restart()
        if not ok:
            raise ServerDown(f"mysqld restart failed: {err}")
        subprocess.run(["sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"], capture_output=True)
        c.db.connect(retries=30, delay=2)
    if prof.reset:
        log(f"reset: {prof.reset}")
        c.db.close()
        rc, out = bench.run_shell(prof.reset, timeout=3600, log=logfile + ".reset")
        if rc != 0:
            raise ServerDown(f"reset command failed ({rc}): {out[-500:]}")
        c.db.connect(retries=30, delay=2)
    hold = lambda el: max(600, 0.25 * el)  # noqa: E731  a settled level has to hold this long to count
    log(f"== run-length probe: one continuous run of up to {max_s // 60} min; each state metric must settle and hold its level "
        f"for at least 10 min (and a quarter of the run); then the measurement length for a ±{target_pct:g}% noise target")
    # the benchmark's prerequisites after the restart / reset, right before it connects (user report: the 1024-thread probe
    # failed with 'Too many connections' - max_connections was back at its option-file value after the restart)
    _ensure_requirements(c, prof)
    t0 = time.time()
    start = t0
    # room for a purge triage inside the same run: a configuration whose undo history grows without bound never settles,
    # so its run length is found only after the triage made it stable (user, session 324: 41 + 60 min runs on an unstable one)
    r = bench.Running(prof.command(total_s=max_s + TRIAGE_BUDGET_S + 60), logfile)
    done = False
    triage = None
    try:
        while r.alive() and time.time() - t0 < max_s:
            time.sleep(60)
            el = time.time() - t0
            c.activity("run-length probe", f"{el / 60:.0f} of max {max_s // 60} min")
            if el < 600:
                continue
            smp_ = SampleStore(c.state.dir).window(t0, time.time())
            res = collector.settle_times(smp_, t0)
            m = res["metrics"]
            if triage is None and engine.STABILITY_FIRST:
                hmax = max([(x.get("db") or {}).get("history_list_length") or 0 for x in smp_] or [0])
                omax = max([(x.get("db") or {}).get("oldest_trx_s") or 0 for x in smp_] or [0])
                unb = engine.unbounded_hll({"unbounded_growth": res["unbounded"], "history_list_length_max": hmax})
                if unb and engine.purge_blocked({"oldest_trx_s_max": omax}):
                    triage = {"stable": False, "blocked_by_trx_s": omax, "kept": [], "steps": []}
                    log(f"   history list unbounded, but a transaction has been open {omax // 60} min: purge is held back, not slow "
                        "(no purge setting can help); end that transaction")
                    c.event("purge_triage", f"purge held back by a transaction open {omax // 60} min: no triage", triage)
                elif unb:
                    def start_bench():
                        _ensure_requirements(c, prof)
                        return bench.Running(prof.command(total_s=max_s + TRIAGE_BUDGET_S + 60), logfile + f".{int(time.time())}")
                    triage = _purge_triage(c, r, start + max_s + TRIAGE_BUDGET_S, start_bench)
                    r = triage.pop("running")
                    if not triage["stable"]:
                        log("   no stable configuration: the run length is not measured on an unstable one (it would only measure "
                            "the drift); a provisional plan follows")
                        break
                    log("   stable: the run-length probe starts over on this configuration")
                    t0 = time.time()
                    continue
            pending = [k for k, x in m.items() if not x["unbounded"] and (x["settled_after_s"] is None or x["plateau_s"] < hold(el))]
            if int(el) % 300 < 60:
                log(f"   {el / 60:4.0f} min: " + "; ".join(f"{x['label']} " + (f"settled at {x['settled_after_s'] // 60} min"
                    if x["settled_after_s"] is not None else "moving") for x in m.values()))
            if m and not pending:
                done = True
                break
    finally:
        early = not r.alive() and time.time() - start < min(300, max_s / 4)
        if r.alive():
            r.kill()
    if early:
        # the benchmark died before it measured anything (canary 2026-10-25: 1024 sysbench threads, open-files limit): a
        # failed probe, never a run-length plan
        try:
            tail = open(logfile).read()[-800:]
        except OSError:
            tail = ""
        raise BenchFailed(f"the benchmark exited after {time.time() - start:.0f} s, before the run-length probe could measure "
                          f"anything (exit code {r.p.returncode}); its output ends with:\n{tail}")
    smp = SampleStore(c.state.dir).window(t0, time.time())
    res = collector.settle_times(smp, t0)
    m = res["metrics"]
    settled = [x for x in m.values() if x["settled_after_s"] is not None and not x["unbounded"]]
    longest = max([x["settled_after_s"] for x in settled] or [0])
    rec = max(60, int(-(-longest * 1.2 // 60)) * 60)
    ml = collector.measure_length(smp, t0 + longest, "tps", target_pct, evaluate.NOISE_K)
    unsettled = [x["label"] for x in m.values() if x["settled_after_s"] is None and not x["unbounded"]]
    log("== run-length probe " + ("finished: every metric settled" if done else "stopped: no stable configuration (purge triage)"
                                  if triage and not triage.get("stable") else "stopped at the time limit")
        + f" after {(time.time() - start) // 60:.0f} min")
    for k, x in sorted(m.items(), key=lambda kv: -(kv[1]["settled_after_s"] or 10 ** 9)):
        log(f"   {x['label']:28} " + ("grows without bound - no warm-up settles it (unsustainable configuration)" if x["unbounded"]
            else f"settled after {x['settled_after_s'] // 60} min {x['settled_after_s'] % 60:02d} s at {x['level']:,} (held {x['plateau_s'] // 60} min)"
            if x["settled_after_s"] is not None else "did not settle within the probe"))
    txt = (f"recommended warm-up: {rec} s ({rec // 60} min; longest settling time + 20%)"
           + (f"; not settled: {', '.join(unsettled)} - run the probe longer" if unsettled else "")
           + (f"; growing without bound: {', '.join(u['label'] for u in res['unbounded'])}" if res["unbounded"] else ""))
    if ml:
        txt += (f"; recommended measurement: {ml['recommended_s']} s (predicted within-run noise ±{ml['predicted_within_noise_pct']}% "
                f"of a ±{target_pct:g}% target" + ("" if ml["reaches_target"] else "; the target is not reached within an hour") + ")")
        log("   within-run noise by measurement length: " + ", ".join(f"{w // 60 if w >= 60 else w}{'m' if w >= 60 else 's'} ±{n}%" for w, n in ml["table"][:7]))
    log("   " + txt)
    unstable = bool(triage) and not triage.get("stable")
    if unstable:
        # provisional: the settled metrics' warm-up, and a window no longer than PROVISIONAL_WINDOW_S - on a growing history
        # a longer window measures the drift, not the configuration. Probed again once a stable configuration is found
        txt += f"; provisional (no stable configuration): measurement {min((ml or {}).get('recommended_s') or 600, PROVISIONAL_WINDOW_S)} s"
    out = {"recommended_warmup_s": rec,
           "recommended_duration_s": min((ml or {}).get("recommended_s") or 600, PROVISIONAL_WINDOW_S) if unstable else (ml or {}).get("recommended_s"),
           "measure": ml, "complete": done and not unsettled and not unstable, "unstable": unstable, "triage": triage, **res}
    c.event("run_length", txt, out)
    return out


def cmd_purge_triage(a):
    """Is the undo history bounded under this benchmark, and which online purge setting makes it so? One benchmark run:
    a lead-in until the history has grown, then the triage steps."""
    c = Ctx()
    prof = bench.Profile(a.bench)
    if c.state.get("measurement"):
        raise Fail("a measurement is in progress")
    if not prof.can_resize():
        raise Fail("the profile's run command has no {total}/{duration} placeholder: its length cannot be set")
    logdir = os.path.join(c.state.dir, "bench")
    os.makedirs(logdir, exist_ok=True)
    _ensure_requirements(c, prof)
    r = bench.Running(prof.command(total_s=a.lead + TRIAGE_BUDGET_S * 2 + 60), os.path.join(logdir, f"purge-triage-{int(time.time())}.log"))
    try:
        t0 = time.time()
        while r.alive() and time.time() - t0 < a.lead:
            c.activity("purge triage", f"lead-in {(time.time() - t0) / 60:.0f} of {a.lead // 60} min")
            time.sleep(30)
        def start_bench():
            _ensure_requirements(c, prof)
            return bench.Running(prof.command(total_s=TRIAGE_BUDGET_S * 2 + 60), os.path.join(logdir, f"purge-triage-{int(time.time())}.log"))
        out = _purge_triage(c, r, time.time() + TRIAGE_BUDGET_S * (2 if a.all else 1), start_bench, measure_all=a.all)
        r = out.pop("running")
    finally:
        if r.alive():
            r.kill()
        c.activity("idle", "purge triage finished")
    if getattr(a, "json", False):
        print(json.dumps(out, default=str))


def cmd_run_length(a):
    """How long a warm-up and how long a measurement does this benchmark need on the current configuration?"""
    c = Ctx()
    prof = bench.Profile(a.bench)
    out = _run_length_probe(c, prof, a.max or prof.run_length_max, a.target or prof.noise_target)
    if a.apply and out["complete"]:
        sets = {"warmup": out["recommended_warmup_s"]}
        if out.get("recommended_duration_s"):
            sets["duration"] = out["recommended_duration_s"]
        lines = open(a.bench).read().splitlines()
        for key, v in sets.items():
            if any(re.match(rf"\s*{key}\s*=", ln) for ln in lines):
                lines = [f"{key} = {v}" if re.match(rf"\s*{key}\s*=", ln) else ln for ln in lines]
            else:
                i = next((n for n, ln in enumerate(lines) if ln.strip() == "[bench]"), 0)
                lines.insert(i + 1, f"{key} = {v}")
        tmp = a.bench + ".new"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, a.bench)
        log(f"   {a.bench}: " + ", ".join(f"{k} = {v}" for k, v in sets.items()))
    if getattr(a, "json", False):
        print(json.dumps(out, default=str))


def cmd_calibrate(a):
    c = Ctx()
    sess = c.session()
    if a.from_runs:
        seqs = [int(x) for x in a.from_runs.split(",")]
        runs = [r for r in sess["runs"] if r["seq"] in seqs]
        if len(runs) < 2:
            raise Fail("need at least 2 existing runs")
        if len({evaluate.config_key(r.get("managed")) for r in runs}) > 1:
            raise Fail("calibration runs must share one configuration")
    else:
        if c.state.get("pending_changes") and sess["runs"]:
            log("note: calibrating the current configuration, which has unmeasured changes")
        runs = []
        label = a.label or ("baseline" if not sess["runs"] else "calibration")
        prof = bench.Profile(a.bench) if a.bench else None
        for i in range(a.runs):
            if prof:
                run, diag = _bench_run(c, prof, f"{label} ({i + 1}/{a.runs})", a.notes)
                log(f"  goal = {run['goal_value']}")
                runs.append(run)
                sess = c.session()
                continue
            if a.warmup and (i == 0 or a.warmup_each):
                _warmup(c, a.warmup)
            m, warn, url = _measure_start(c, f"{label} ({i + 1}/{a.runs})", a.notes, warmed=bool(a.warmup) or i > 0)
            log(f"measuring run {m['seq']} ({i + 1}/{a.runs}) for {a.duration}s ...")
            time.sleep(a.duration)
            run, diag = _measure_stop(c, None, None, a.notes)
            log(f"  goal = {run['goal_value']}")
            runs.append(run)
            sess = c.session()
    cal = evaluate.calibrate(runs)
    if not cal:
        raise Fail("calibration needs at least 2 runs with a goal value")
    sess = c.session()
    _set_calibration(c, sess, cal)
    changed = _reevaluate(c, c.session())
    emit({"ok": True, "calibration": cal, "reevaluated": changed},
         _fmt_cal(cal) + ("\nRe-evaluated: " + ", ".join(f"run {s} {o}->{n}" for s, o, n in changed) if changed else ""))


KEEP_ON_REJUDGE = ("hypothesis",)


def _reevaluate(c, sess):
    noise = sess.get("noise") or ({"noise_pct": sess["noise_pct"]} if sess.get("noise_pct") else None)
    changed = []
    for i, r in enumerate(sess["runs"]):
        v, ev = evaluate.verdict(r, sess["runs"][:i], sess["goal"], noise)
        # what the verdict does not recompute stays: the hypothesis this run tested and its outcome
        ev = dict(ev, **{k: (r.get("evaluation") or {})[k] for k in KEEP_ON_REJUDGE if k in (r.get("evaluation") or {})})
        if v in ("improved", "efficiency-gain") and r.get("verdict") not in ("improved", "efficiency-gain") and i + 1 < len(sess["runs"]):
            # promoted only because the threshold tightened later (canary #146 run 7: +3.0% within ±4.07% at the time,
            # beyond ±2.07% at the end): a single run the session moved on from - not kept until a repeat confirms it
            same = [x for x in sess["runs"] if evaluate.config_key(x.get("managed")) == evaluate.config_key(r.get("managed"))
                    and x.get("verdict") not in evaluate.EXCLUDED]
            if len(same) < 2:
                ev = dict(ev, confirmed=False, rejudged_unconfirmed=True,
                          note=f"Beyond the threshold only after it tightened to ±{(noise or {}).get('noise_pct')}%; measured once, "
                               "not kept until a repeat confirms it.")
        if v != r.get("verdict") or ev != r.get("evaluation"):
            changed.append((r["seq"], r.get("verdict"), v))
            r["verdict"], r["evaluation"] = v, ev
            c.client.post(f"/runs/{r['id']}/evaluation", {"verdict": v, "evaluation": ev})
    changed += _confirm(c, sess, save=False)
    _update_best(sess)
    c.state.set("session", sess)
    return changed


def _confirm(c, sess=None, save=True):
    """Re-judge improvements whose configuration has been measured again (confirmation, re-checks)."""
    sess = sess or c.session()
    noise = sess.get("noise") or ({"noise_pct": sess["noise_pct"]} if sess.get("noise_pct") else None)
    changed = evaluate.confirm_pass(sess["runs"], sess["goal"], noise)
    for seq, old, new in changed:
        r = next(x for x in sess["runs"] if x["seq"] == seq)
        if r.get("id"):
            c.client.post(f"/runs/{r['id']}/evaluation", {"verdict": r["verdict"], "evaluation": r["evaluation"]})
        if old != new:
            log(f"   run {seq}: {old} -> {new} ({(r['evaluation'] or {}).get('note') or 'confirmed by its repeat'})")
        if old in ("improved", "efficiency-gain") and new not in ("improved", "efficiency-gain"):
            # the result did not hold on its repeat: the hypothesis it seemed to support did not either
            for h in sess.get("hypotheses") or []:
                if h.get("run") == seq and h.get("outcome") not in ("done", "refuted"):
                    h["outcome"] = "not-confirmed"
            hy = (r.get("evaluation") or {}).get("hypothesis")
            if hy and hy.get("outcome") not in ("done", "refuted"):
                r["evaluation"]["hypothesis"] = dict(hy, outcome="not-confirmed")
                if r.get("id"):
                    c.client.post(f"/runs/{r['id']}/evaluation", {"verdict": r["verdict"], "evaluation": r["evaluation"]})
    if save:
        _update_best(sess)
        c.state.set("session", sess)
    return changed


def cmd_reevaluate(a):
    c = Ctx()
    sess = c.session()
    changed = _reevaluate(c, sess)
    emit({"ok": True, "changed": changed},
         "\n".join(f"  run {s}: {o} -> {n}" for s, o, n in changed) or "All verdicts unchanged.")


def _fill_profiles(c, sess, last, basis=None):
    """Profiles run once per configuration, so a repeat or confirmation has none: take them from the latest profiled run
    of the same configuration and rebuild the time breakdown, so the next decision keeps the detailed evidence."""
    inp = last.setdefault("inputs", {})
    need = [k for k in ("cpu_profile", "offcpu") if not (inp.get(k) or {}).get("categories" if k == "cpu_profile" else "foreground")]
    if not need or not sess["runs"]:
        return
    # the configuration the inputs belong to: after a revert that is the configuration now in effect (`basis`), not the
    # last run's - looking up the wrong one found no profiles and hid a 77-80% commit wait behind "CPU (not profiled)"
    br = next((r for r in sess["runs"] if r["seq"] == basis), None) if basis else None
    key = evaluate.config_key((br or sess["runs"][-1]).get("managed"))
    skip = (br or sess["runs"][-1])["seq"]
    for r in reversed(sess["runs"]):
        if r["seq"] == skip:
            continue
        if not need or evaluate.config_key(r.get("managed")) != key:
            continue
        try:
            with open(os.path.join(c.state.dir, f"run{r['seq']}_inputs.json")) as f:
                old = json.load(f).get("inputs") or {}
        except (OSError, ValueError):
            continue
        for k in list(need):
            if (old.get(k) or {}).get("categories" if k == "cpu_profile" else "foreground"):
                inp[k] = dict(old[k], from_run=r["seq"])
                need.remove(k)
    # still nothing: the most recent profile of any configuration - the shape of the CPU time rarely changes with one
    # setting, while "CPU (not profiled)" as one block matches every CPU lever (r1 audit: 30 of 66 decisions were made
    # on unprofiled runs; AN-2 ahi-on flipped to "no evidence" right after a profile showed 43% B-tree time)
    for r in reversed(sess["runs"]):
        if not need:
            break
        if r["seq"] == skip:
            continue
        try:
            with open(os.path.join(c.state.dir, f"run{r['seq']}_inputs.json")) as f:
                old = json.load(f).get("inputs") or {}
        except (OSError, ValueError):
            continue
        for k in list(need):
            if (old.get(k) or {}).get("categories" if k == "cpu_profile" else "foreground"):
                inp[k] = dict(old[k], from_run=r["seq"], other_config=True)
                need.remove(k)
    if any(k in inp and (inp[k] or {}).get("from_run") for k in ("cpu_profile", "offcpu")):
        inp["time_breakdown"] = bottleneck.breakdown(last.get("summary"), inp, (c.hw() or {}).get("cpus"))


def _engine_ctx(c, sess):
    path = sess.get("last_inputs_path")
    basis = None
    if sess["runs"]:
        cur = evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()})
        if evaluate.config_key(sess["runs"][-1].get("managed")) != cur:
            # the last run's configuration was reverted: its symptoms (e.g. swapping caused by a filesystem experiment)
            # no longer apply; diagnose from the latest run of the configuration now in effect
            same = [r for r in sess["runs"] if evaluate.config_key(r.get("managed")) == cur and r.get("goal_value") is not None]
            if same:
                p2 = os.path.join(c.state.dir, f"run{same[-1]['seq']}_inputs.json")
                if os.path.exists(p2):
                    path, basis = p2, same[-1]["seq"]
    if not sess["runs"] or not path or not os.path.exists(path):
        last = {"summary": {}, "inputs": {}}
    else:
        with open(path) as f:
            last = json.load(f)
        _fill_profiles(c, sess, last, basis)
    flavor, vt, ver, _ = mysqldb.flavor_of(c.variables)
    _ap = Applier(c.cfg, c.state, c.db)
    osf = ostune.facts(c.datadir(), hostinfo.device_for_path(c.datadir()), _ap.service, _ap.fs_device)
    experience = {}
    hyp_exp = {}
    insights = []
    param_impact = {}
    blocked = set()
    try:
        from .engine import classify_workload
        wl = classify_workload(last.get("summary"), last.get("inputs")).get("type") if last.get("summary") else None
        conns = (last.get("summary") or {}).get("threads_connected_avg")
        q = (f"/experience?cpus={os.cpu_count()}&exclude_session={sess['id']}" + (f"&workload={wl}" if wl else "")
             + (f"&conns={int(conns)}" if conns else "") + f"&numa_nodes={(c.hw() or {}).get('numa_nodes') or 1}"
             + f"&flavor={flavor}&version={ver}&cluster={(_cluster_ctx(c) or {}).get('type') or 'standalone'}"
             + f"&engine={(_engines_ctx(c) or {}).get('primary') or 'innodb'}")
        ex = c.client.get(q)
        experience = ex.get("experience", {})
        insights = ex.get("insights") or []
        param_impact = ex.get("parameters") or {}
        try:  # the account's parameter policy: settings the tuner must not change
            blocked = set(c.client.get("/param-policy").get("blocked") or [])
        except Exception:
            blocked = set()
        hyp_exp = {"similar": ex.get("hypotheses") or [], "all": ex.get("hypotheses_all") or []}
        # evidence tiers, most specific first: this variant and release series in similar conditions (full weight), the same
        # variant in other series, other variants (MySQL / Percona / MariaDB differ in options, defaults and behaviour),
        # then every session. A tier is used only where the more specific ones have too little.
        tiers = [(ex.get("experience_family") or {}, 2, 0.6, "same server variant, other release series"),
                 (ex.get("experience_other_variant") or {}, 3, 0.3, "other server variants"),
                 (ex.get("experience_all") or {}, 3, 0.3, "other workloads / machine sizes / concurrency")]
        for data, need, weight, scope in tiers:
            for rid, e in data.items():
                if e.get("n", 0) >= need and (experience.get(rid) or {}).get("n", 0) < 2:
                    experience[rid] = dict(e, scope=scope, weight=weight)
    except Exception as e:  # experience is a hint; never block a recommendation on it
        log(f"note: no experience data ({e})")
    b = (last.get("inputs") or {}).get("bench") or {}
    bench_info = {k: b.get(k) for k in ("name", "run", "warmup", "duration", "goal_metric", "requires") if b.get(k) is not None}
    return {"bench": bench_info, "diagnosis_basis_run": basis, "goal": sess["goal"], "policy": sess["policy"], "hw": c.hw(), "flavor": flavor, "version": vt,
            "os_tuning": c.os_tuning_allowed() and sess.get("os_tuning", True), "os_facts": osf, "experience": experience,
            # swap = none in agent.ini: no swap by design (a production philosophy) - never add it, size memory for RAM alone
            "no_swap": str(c.cfg.get("swap", "allowed")).strip().lower() in ("none", "no", "off", "false"),
            "insights": insights, "param_impact": param_impact, "blocked": sorted(blocked), "client_reserve": _client_reserve(c),
            "cluster": _cluster_ctx(c), "engines": _engines_ctx(c),
            "failed": sess.get("failed_recs") or [],
            "scope": sess.get("scope") or ["knobs", "os"], "fs_device": _ap.fs_device,
            "variables": c.all_settings(), "summary": last.get("summary"), "inputs": last.get("inputs"),
            "history": sess["runs"], "noise_pct": sess.get("noise_pct"), "noise": sess.get("noise"),
            "hypotheses": sess.get("hypotheses") or [], "hypothesis_experience": hyp_exp,
            "managed": {k: v["value"] for k, v in c.state.get("managed", {}).items()}}


def _recommend(c, sess, llm=False):
    c.refresh()
    ctx = _engine_ctx(c, sess)
    res = engine.run_engine(ctx)
    # a session started with --ai off (rules + learnings only, e.g. the head-to-head's rules arm) asks no model anything
    ai_off = str(sess.get("ai") or "").lower() == "off"
    why = None if ai_off else llmassist.trigger(res, explicit=llm)
    if ai_off:
        res["llm"] = {"skipped": "AI off for this session (rules + learnings only)"}
    pending = [] if ai_off else (sess.get("llm_pending") or [])  # accepted earlier, not measured yet: they stay candidates
    fresh, model = [], None
    if why and ctx.get("summary"):
        try:
            meta = c.db.variables_meta()
        except Exception:
            meta = {}
        try:
            r = c.client.post_ai("/llm/suggest", {"session_id": sess["id"], "trigger": why, "force": bool(llm),
                                                "context": llmassist.context(ctx, res, meta)})
        except Exception as e:  # LLM help is optional
            r = {"enabled": False, "reason": str(e)}
        if r.get("enabled") and r.get("suggestions") is not None:
            fresh, model = [dict(sg, _model=r.get("model")) for sg in r.get("suggestions") or []], r.get("model")
            res["llm"] = {"model": model, "trigger": why, "cached": r.get("cached"), "notes": r.get("notes")}
        elif why != "routine" and r.get("reason"):
            res["llm"] = {"skipped": r.get("reason"), "trigger": why}
    # measured time no candidate acts on: ask for a bottleneck analysis (hypothesis, changes, mechanism), cached by the
    # conditions so similar situations reuse it
    if ctx.get("summary") and not ai_off:
        analyzed = set(sess.get("analyzed_components") or [])
        for comp in llmassist.uncovered(res):
            if comp["name"] in analyzed:
                continue
            try:
                ra = c.client.post_ai("/llm/analyze", {"session_id": sess["id"], "context": llmassist.analysis_context(ctx, res, comp),
                                                     "signature": llmassist.signature(ctx, res, comp)})
            except Exception as e:  # optional
                ra = {"enabled": False, "reason": str(e)}
            res.setdefault("analyses", []).append({"component": comp["name"], "share_pct": comp.get("share_pct"),
                                                   **{k: ra.get(k) for k in ("enabled", "reason", "hypothesis", "confirm_by", "notes",
                                                                             "cached", "cached_from", "model", "error")},
                                                   "changes": len(ra.get("changes") or [])})
            if not ra.get("enabled"):
                break  # not configured or over the limit: nothing more to ask this round
            analyzed.add(comp["name"])
            if ra.get("error"):
                continue
            log(f"   bottleneck analysis of {comp['name']} ({comp.get('share_pct')}%)" + (" [cached from similar conditions]" if ra.get("cached") else "")
                + f": {(ra.get('hypothesis') or '')[:200]}")
            fresh += llmassist.analysis_suggestions(ra, comp)
        sess["analyzed_components"] = sorted(analyzed)
    seen, allsg = set(), []
    for sg in fresh + pending:
        k = json.dumps(sorted((str(x.get("variable")), str(x.get("value"))) for x in sg.get("changes") or []))
        if k not in seen:
            seen.add(k)
            allsg.append(sg)
    extra, keep, rejected = [], [], []
    done_ids = set(sess.get("failed_recs") or [])
    for sg in allsg:  # re-checked every time: dropped once tried (incl. as a sweep), already in effect or no longer valid
        its, rj = llmassist.sanitize([sg], ctx, res, sg.get("_model") or model)
        its = [i for i in its if i["id"] not in done_ids]
        if its:
            for it in its:
                # the same physical preconditions as rule-based experiments: no precondition, no experiment until the end
                nomech = mechanisms.missing_for_variables(it["changes"], ctx.get("summary"), (ctx.get("inputs") or {}).get("status_rates"),
                                                          ctx.get("inputs"))
                if nomech:
                    it["no_mechanism"] = nomech
                    it["note"] = f"Validation only: {nomech}."
                    it.pop("sweep", None)
                    it["hypothesis"] = {"kind": "validation", "text": f"Validation only - {nomech}. (LLM suggestion: {sg.get('rationale') or it.get('title')})"}
                    continue
                an = sg.get("_analysis")
                if an:  # from a bottleneck analysis: it targets one measured component
                    it["acts_on_tags"] = an["tags"]
                    it["addresses"] = [f"{an['component']} ({an.get('share_pct') or 0:.0f}%)"]
                    cf = an.get("cached_from") or {}
                    it["hypothesis"] = {"kind": "llm-analysis", "target": [an["component"]], "tags": an["tags"], "share_pct": an.get("share_pct"),
                                        "text": f"{an['component']} takes {an.get('share_pct') or 0:.0f}% of database time and no rule acts on it. "
                                                f"Hypothesis ({sg.get('_model') or model}{', reused from similar conditions in session ' + str(cf.get('session')) if cf else ''}): "
                                                f"{an.get('hypothesis') or ''} Mechanism: {sg.get('mechanism') or ''} "
                                                f"{'Precondition: ' + an['precondition'] + ' ' if an.get('precondition') else ''}"
                                                f"Prediction: {an.get('confirm_by') or 'this component shrinks per transaction'}"}
                    continue
                it["hypothesis"] = {"kind": "llm", "text": f"LLM ({sg.get('_model') or model}): {sg.get('rationale') or it.get('title')}"
                                    + (f" Mechanism: {sg['mechanism']}" if sg.get("mechanism") else "")}
            extra += its
            keep.append(sg)
        elif sg in fresh:
            rejected += rj
    if res.get("llm") and "model" in res["llm"]:
        res["llm"].update(accepted=len([s for s in keep if s in fresh]), rejected=rejected, pending=len([s for s in keep if s not in fresh]),
                          # every suggestion as the model gave it, for people to review next to what ran
                          suggestions=[{k: sg.get(k) for k in ("title", "changes", "mechanism", "expected_effect", "risk", "rationale", "sweep_values")}
                                       for sg in fresh if not sg.get("_analysis")])
    sess["llm_pending"] = keep
    if extra:
        for it in extra:
            it["score"] = engine.score(it, ctx.get("goal", "tps")) + (2 if why not in (None, "routine") else 0) - (10 if it.get("no_mechanism") else 0)
            ob = engine.oversubscription_block(it, ctx)  # AI ideas get the same gate as the rules'
            if ob:
                it["confidence"], it["note"] = 0, (it.get("note", "") + " " + ob).strip()
            if it["safety"] == "trade-off" and ctx.get("policy") != "benchmark":
                it["kind"] = "advice"
        ids = {i["id"] for i in res["items"]}
        for i in extra:  # one candidate per id (a pending and a fresh suggestion can be the same change)
            if i["id"] not in ids:
                ids.add(i["id"])
                res["items"].append(i)
        res["items"].sort(key=lambda i: (i.get("safety") == "trade-off" and i["kind"] != "revert", -i.get("score", 0)))
        for n, it in enumerate(res["items"], 1):
            it["rank"] = n
        if not res.get("next") or why == "rules-exhausted":
            nxt = next((i for i in res["items"] if i["kind"] in ("config", "revert") and i["confidence"] > 0 and i["id"] not in done_ids
                        and (i.get("safety") == "safe" or ctx.get("policy") == "benchmark")), None)
            res["next"] = nxt["id"] if nxt else None
    if ctx.get("summary") and not ai_off:
        _plan(c, sess, ctx, res)
    res.pop("_ctx", None)
    run_id = sess["runs"][-1]["id"] if sess["runs"] else None
    r = c.client.post("/recommendations", {"session_id": sess["id"], "run_id": run_id, "goal": sess["goal"], "policy": sess["policy"],
                                           "items": res["items"], "diagnosis": res["diagnosis"], "next": res["next"],
                                           "workload": res.get("workload"), "phase": res.get("phase"), "decision": res.get("decision"),
                                           "llm": dict(res.get("llm") or {}, **({"analyses": res["analyses"]} if res.get("analyses") else {})) or None})
    sess["last_recommendation"] = {"id": r.get("recommendation_id"), "items": res["items"], "next": res["next"]}
    c.state.set("session", sess)
    return res


def _plan(c, sess, ctx, res):
    """Planner mode (account setting): a model chooses the next step from the evidence, a second model may review it, and
    planner.check keeps what may run. Everything the models said is stored with the recommendation for people to review."""
    ids = [i["id"] for i in res["items"] if i["kind"] in ("config", "revert", "advice")][:40]
    try:
        fb = c.client.get(f"/sessions/{sess['id']}/feedback?items=" + urllib.parse.quote(",".join(ids)))
    except Exception:
        fb = {}
    try:
        meta = c.db.variables_meta()
    except Exception:
        meta = {}
    pk = planner.packet(ctx, res, meta)
    if sess.get("planner_rejected"):
        pk["rejected_this_session"] = sess["planner_rejected"]
    if fb.get("session") or fb.get("other_sessions"):
        # people's views of earlier runs and advice: an opinion to weigh, not a measurement
        pk["human_feedback"] = fb
    try:
        r = c.client.post_ai("/llm/plan", {"session_id": sess["id"], "packet": pk})
    except Exception as e:  # optional: without a plan the engine's own choice stands
        r = {"enabled": False, "reason": str(e)[:200]}
    if not r.get("enabled"):
        return
    rec = {k: r.get(k) for k in ("model", "plan", "critic", "critic_model", "final", "error", "critic_error") if r.get(k) is not None}
    rec["engine_next"] = res.get("next")
    if r.get("error") or not r.get("plan"):
        res.setdefault("llm", {})["planner"] = rec
        return
    p = dict(r["plan"], _model=r.get("model"))
    if r.get("final"):
        p.update(action=r["final"]["action"], choice=r["final"]["choice"])
    nxt, added, why = planner.check(p, ctx, res)
    rec.update(applied=nxt, rejected=why)
    if why is not None:  # told to the planner next time (backtest: ps:profile=lite proposed 23 times under policy=safe)
        sess_ = c.session()
        ch_ = p.get("choice") or {}
        sess_.setdefault("planner_rejected", []).append({"choice": ch_.get("candidate_id") or ", ".join(
            f"{x.get('variable')}={x.get('value')}" for x in ch_.get("changes") or []) or p.get("action"), "reason": str(why)[:200]})
        sess_["planner_rejected"] = sess_["planner_rejected"][-10:]
        c.state.set("session", sess_)
    if why is None:
        for it in added:
            res["items"].append(it)
        if p["action"] == "stop":
            res["next"] = None
            res["stop_reason"] = f"Planner ({r.get('model')}): {p.get('diagnosis', '')[:300]} {p.get('stop_advice', '')[:400]}".strip()
        else:
            res["next"] = nxt
            it = next((i for i in res["items"] if i["id"] == nxt), None)
            if it is not None:
                it["planner"] = {"model": r.get("model"), "prediction": p.get("prediction"), "confidence": p.get("confidence"),
                                 "why_above_noise": p.get("why_above_noise")}
                h = it.get("hypothesis") or {}
                it["hypothesis"] = dict(h, kind=h.get("kind") or "planner", text=(h.get("text") or "") + f" Planner: {p.get('rationale', '')[:500]}",
                                        prediction=p.get("prediction"))
        res["planner_decided"] = True
        res.setdefault("decision", {})["by"] = f"planner:{r.get('model')}" + (f"+reviewer:{r.get('critic_model')}" if r.get("critic") else "")
        res["decision"]["engine_choice"] = rec["engine_next"]
        log(f"   planner {r.get('model')}: " + ("stop" if p["action"] == "stop" else nxt)
            + (f" (engine: {rec['engine_next']})" if rec["engine_next"] != nxt else "")
            + (f"; reviewer {r.get('critic_model')}: {(r.get('critic') or {}).get('verdict')}" if r.get("critic") else ""))
    else:
        log(f"   planner {r.get('model')} choice not used ({why}); keeping {res.get('next')}")
        res.setdefault("decision", {})["by"] = "rules"
        res["decision"]["planner_rejected"] = f"{r.get('model')}: {why}"[:300]
    res.setdefault("llm", {})["planner"] = rec


def cmd_recommend(a):
    c = Ctx()
    sess = c.session()
    if c.state.get("pending_changes"):
        log("note: there are applied changes that have not been measured yet")
    if not sess.get("noise"):
        log("note: noise is not calibrated; run `tuning-agent calibrate --runs 3` (or --from-runs) so verdicts use measured noise")
    res = _recommend(c, sess, llm=getattr(a, "llm", False))
    lines = [f"Recommendations (goal={sess['goal']}, policy={sess['policy']}, workload={(res.get('workload') or {}).get('type')}, "
             f"phase={res.get('phase')}) based on run {sess['runs'][-1]['seq'] if sess['runs'] else '-'}:"]
    for d in res["diagnosis"]:
        lines.append(f"  diagnosis [{d['severity']}] {d['type']}: {d['text']}")
    for it in res["items"]:
        ch = ", ".join(f"{x['variable']}: {x['current']} -> {'(server default)' if x.get('unset') else x['proposed']}" for x in it["changes"]) or "(no config change)"
        flag = ("" if it["kind"] != "advice" else " [advice]") + (" [TRADE-OFF: " + ", ".join(t["category"] for t in it.get("tradeoffs", [])) + "]" if it.get("safety") == "trade-off" else "")
        lines.append(f"  {it['rank']:>2}. {it['id']:<26} {it['title']}{flag}\n      {ch}\n      risk={it['risk']} restart={it['restart']} "
                     f"impact={it['impact']} confidence={it['confidence']}{' — ' + it['note'] if it.get('note') else ''}\n      why: {it['rationale']}")
    if res["next"]:
        lines.append(f"Suggested next experiment: `tuning-agent apply --rec {res['next']}` then measure again.")
    emit({"ok": True, **res}, "\n".join(lines))


def cmd_apply(a):
    c = Ctx()
    if c.state.get("measurement"):
        raise Fail("a measurement is in progress; stop it before changing the configuration")
    sess = c.session(required=False)
    changes = []
    if a.rec or a.next:
        last = (sess or {}).get("last_recommendation")
        if not last:
            raise Fail("no current recommendation: run `tuning-agent recommend` after the latest measured run")
        if last.get("applied") and a.next:
            raise Fail(f"the suggested next step ({last.get('next')}) was already applied: measure, then run `tuning-agent recommend` again")
        ids = list(a.rec or [])
        if a.next:
            if not last.get("next"):
                raise Fail("the last recommendation has no suggested next step")
            ids.append(last["next"])
        by = {i["id"]: i for i in last["items"]}
        for rid in ids:
            it = by.get(rid)
            if not it:
                raise Fail(f"unknown recommendation id {rid!r}; known: {', '.join(by)}")
            if it["kind"] == "advice" and not a.force:
                raise Fail(f"{rid} is advice only (risk {it['risk']}); pass --force to apply anyway")
            for ch in it["changes"]:
                changes.append({"variable": ch["variable"], "value": None if ch.get("unset") else ch["proposed"], "reason": a.reason or it["rationale"],
                                "source": f"rec:{rid}"})
    for k, v in _parse_kv_str(a.set).items():
        changes.append({"variable": k, "value": v, "reason": a.reason or "", "source": "manual"})
    for k in a.unset or []:
        changes.append({"variable": k, "value": None, "reason": a.reason or "unset", "source": "manual"})
    if not changes:
        raise Fail("nothing to apply: use --rec ID, --next, --set var=value or --unset var")
    res = _apply_and_record(c, changes, "apply", restart=False if a.no_restart else None)
    if (a.rec or a.next) and sess and sess.get("last_recommendation") and not res.get("rolled_back"):
        sess["last_recommendation"]["applied"] = True
        c.state.set("session", sess)
    known = _known_config(c, sess)
    text = "Applied:\n" + _print_apply(res)
    if known:
        text += (f"\nThis configuration was already measured in runs {known['runs']} (mean {known['mean']:.4g}); "
                 "re-measuring is optional — you can go straight to `tuning-agent recommend` / the next experiment.")
    emit({"ok": not res.get("rolled_back") and not res.get("errors"), "result": res, "known_configuration": known}, text)
    if res.get("rolled_back"):
        raise Fail("apply failed and was rolled back")


# ---- human / agent specified experiments ---------------------------------------------------
def _validate_changes(c, changes, force=False):
    """Same safety checks as for LLM suggestions (minus 'already tried': repeating is legitimate when asked)."""
    v = c.all_settings()
    ram = int(c.hw().get("ram_bytes") or 0)
    ok, errors = [], []
    for ch in changes:
        var = ch["variable"].strip()
        var = var if var.startswith("os:") else var.lower().replace("-", "_")
        val = str(ch["value"]).strip().strip("'\"")
        if var in llmassist.DENY:
            errors.append(f"{var} is not tunable by the agent")
        elif var not in v and var not in ("skip_log_bin", "disable_log_bin"):
            errors.append(f"{var} does not exist on this server")
        elif var.startswith("os:") and not c.os_tuning_allowed():
            errors.append("OS tuning is disabled for this agent")
        else:
            from .knowledge import KB as _KB, parse_size as _ps
            n = _ps(val) if _KB.get(var, {}).get("kind") == "size" else None
            if n is not None and ram and not force and n > ram * (0.85 if var == "innodb_buffer_pool_size" else 0.25):
                errors.append(f"{var}={val} exceeds the memory budget of this {ram / 2 ** 30:.1f} GiB host (use force to override)")
            else:
                ok.append({"variable": var, "value": val})
    return ok, errors


def _goto_managed(c, target, reason, source):
    cur = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    changes = [{"variable": k, "value": target.get(k), "reason": reason, "source": source}
               for k in set(cur) | set(target) if normalize(k, cur.get(k)) != normalize(k, target.get(k))]
    return _apply_and_record(c, changes, source.split(":")[0]) if changes else None


def _num(v):
    """Numeric value of a setting (sizes like 4G included), or None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            from .knowledge import parse_size
            return float(parse_size(str(v)))
        except Exception:
            return None


def _sv(var, x):
    """A sweep value as a number for ordering and distances (sizes parsed; thread concurrency 0 = unlimited = above any
    limit)."""
    n = engine._numval(var, x)
    return 1e12 if n == float("inf") else n


def _screen_order(arms, pv):
    """Sensitivity before refinement: on each side of the parent value, the widest step first (the strongest signal, and
    an optimum at the far end is not missed - #27 thread concurrency 4), then the rest nearest first. Sides in the order
    the sweep listed them (evidence direction / bracket order first)."""
    sides = []
    for a in arms:
        x = _sv(a[0]["variable"], a[0]["value"])
        sd = 0 if x is None else (1 if x > pv else -1)
        if sd not in sides:
            sides.append(sd)
    out = []
    for sd in sides:
        mine = [a for a in arms if (lambda x: (0 if x is None else (1 if x > pv else -1)))(_sv(a[0]["variable"], a[0]["value"])) == sd]
        if sd == 0:
            out += mine
            continue
        mine.sort(key=lambda a: abs(_sv(a[0]["variable"], a[0]["value"]) - pv))
        out += mine[-1:] + mine[:-1]
    return out


def _extend_optimum(results, pv, noise, worse_at, last_value, spec):
    meas = [(r, _sv(spec["variable"], r["arm"].split("=", 1)[1])) for r in results if r["status"] == "measured" and r.get("delta_pct") is not None]
    meas = [(r, x) for r, x in meas if x is not None]
    if not meas:
        return None
    br, bx = max(meas, key=lambda p: p[0]["delta_pct"])
    side = 1 if bx > pv else -1
    farthest = all(abs(x - pv) <= abs(bx - pv) for r, x in meas if (x > pv) == (side > 0))
    if not (br["delta_pct"] > noise and farthest and side not in worse_at):
        return None
    if bx >= 1e12:  # the best is "unlimited" (thread concurrency 0): nothing lies beyond it
        return None
    nx = bx * 2 if side > 0 else bx / 2
    if side < 0 and nx < 1:
        return None
    size = isinstance(last_value, str) and last_value[-1:].upper() in ("K", "M", "G")
    val = engine.fmt_size(int(nx)) if size else str(int(nx))
    val = engine.nice_value(spec["variable"], val)
    if any(_sv(spec["variable"], r["arm"].split("=", 1)[1]) == _sv(spec["variable"], val) for r in results):
        return None
    return val


def _durability_rank(var, value):
    """0 = most durable. Among values within noise of the best, the safest wins (r1 WI-3: the sweep kept sync_binlog=0,
    never syncing, although 100 was within 1%)."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    if var == "sync_binlog":  # 1 = every commit group, N = every N groups, 0 = never
        return float("inf") if n == 0 else n
    if var == "innodb_flush_log_at_trx_commit":  # 1 > 2 (OS crash loses ~1 s) > 0 (mysqld crash loses ~1 s)
        return {1: 0, 2: 1, 0: 2}.get(n, 3)
    return 0


def _good_enough(measured, best, noise, var, pv, shape):
    """The value to keep: among those within noise of the best (they cannot be told apart), the cheapest - the smaller
    value for memory-costing / more-is-better settings - then the roundest, then the smallest change from the parent.
    The exact optimum is rarely resolvable within noise; a value that costs less and reads better is worth more."""
    from .knowledge import KB, catalog, roundness
    tied = [r for r in measured if r["delta_pct"] > noise and r["delta_pct"] >= best["delta_pct"] - noise]
    if len(tied) < 2:
        return best
    costly = shape == "saturating" or (KB.get(var) or {}).get("kind") == "size" or bool((catalog().get(var) or {}).get("memory_cost"))
    def key(r):
        v = r["arm"].split("=", 1)[1]
        x = _sv(var, v)
        return (_durability_rank(var, v), x if costly and x is not None else 0, roundness(var, v), abs((x if x is not None else 0) - (pv or 0)))
    return min(tied, key=key)


def _run_experiment(c, prof, exp):
    """Measure every arm of an experiment from the same parent configuration; keep the best arm only if it beats noise."""
    sess = c.session()
    eid, spec = exp["id"], exp["spec"]
    arms = [spec["changes"]] if exp["kind"] == "try" else [[{"variable": spec["variable"], "value": x}] for x in spec["values"]]
    label = lambda arm: ", ".join(f"{x['variable']}={x['value']}" for x in arm)
    parent = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    goal = sess["goal"]
    pkey = evaluate.config_key(parent)
    pm = evaluate.config_means(sess["runs"], pkey, recent=evaluate.RECENT)
    noise = float((sess.get("noise") or {}).get("noise_pct") or sess.get("noise_pct") or 2.0)
    c.client.post(f"/experiments/{eid}", {"status": "running"})
    results, problems = [], []
    # an agent's sweep stops going further in a direction once a smaller step that way was worse beyond noise
    # (#125: io_capacity 4000 was -12%, then 8000 cost a -35% run); sweeps a person asked for run every point
    auto = str(exp.get("created_by") or "").startswith(("agent:", "llm:")) and exp["kind"] == "sweep"
    pv = None
    if auto and exp["kind"] == "sweep":
        cur = parent.get(spec.get("variable"))
        if cur is None:  # not managed yet: the server's running value (most sweeps start from a default)
            try:
                cur = c.db.variables().get(spec["variable"])
            except Exception:
                cur = None
        pv = _sv(spec["variable"], cur)
    worse_at, oom_at = {}, {}
    from .knowledge import response_shape
    shape = response_shape(spec.get("variable")) if exp["kind"] == "sweep" else None
    extended = 0
    arms = list(arms)
    screen = auto and pv is not None and shape in ("saturating", "optimum")
    if screen:
        arms = _screen_order(arms, pv)
    def side_of(arm):  # the same sides as _screen_order: non-numeric values are a side of their own (0)
        x = _sv(arm[0]["variable"], arm[0]["value"]) if screen else None
        return 0 if x is None else (1 if x > pv else -1)
    state, best_on = {}, {}  # side -> screen / hint / flat / sensitive / knee; side -> best delta measured there
    i = -1
    while True:
        if i + 1 >= len(arms):
            # an inner optimum may lie beyond the best value when it is the farthest one tried on its side and beat the
            # parent: one more step that way (at most twice), with a round value
            nxt = _extend_optimum(results, pv, noise, worse_at, arms[-1][0]["value"], spec) if (
                auto and shape == "optimum" and extended < 2 and pv is not None) else None
            if not nxt:
                break
            arms.append([{"variable": spec["variable"], "value": nxt}])
            extended += 1
            log(f"   optimum may lie beyond the best value so far: one more step, {spec['variable']}={nxt}")
        i += 1
        arm = arms[i]
        side = side_of(arm)
        if screen and state.get(side) in ("flat", "knee"):
            why = ("screened: the widest step this way moved the goal less than half the noise threshold" if state[side] == "flat"
                   else "knee reached: a smaller value already performs within noise of the best one, larger ones only cost more")
            results.append({"arm": label(arm), "status": "skipped", "reason": why})
            log(f"   {label(arm)}: skipped ({why})")
            continue
        x0 = _sv(arm[0]["variable"], arm[0]["value"]) if pv is not None else None
        if x0 is not None and x0 != pv and abs(x0 - pv) >= oom_at.get(1 if x0 > pv else -1, float("inf")):
            results.append({"arm": label(arm), "status": "skipped", "reason": "a smaller step in the same direction OOM-killed mysqld"})
            log(f"   {label(arm)}: skipped (a smaller step this way OOM-killed mysqld)")
            continue
        if auto and pv is not None:
            x = _sv(arm[0]["variable"], arm[0]["value"])
            if x is not None and x != pv:
                sd = 1 if x > pv else -1
                if sd in worse_at and abs(x - pv) > worse_at[sd]:
                    results.append({"arm": label(arm), "status": "skipped",
                                    "reason": "a smaller step in the same direction was already worse beyond noise"})
                    log(f"   {label(arm)}: skipped (a smaller step this way was already worse beyond noise)")
                    continue
        ok, errs = _validate_changes(c, arm, force=bool(exp.get("force")))
        if errs:
            problems.append(f"{label(arm)}: " + "; ".join(errs))
            results.append({"arm": label(arm), "status": "rejected", "reason": "; ".join(errs)})
            continue
        target = dict(parent)
        for x in ok:
            target[x["variable"]] = x["value"]
        why = f"experiment #{eid}" + (f": {exp['reason']}" if exp.get("reason") else "")
        res = _goto_managed(c, target, why, f"exp:{eid}")
        if res and res.get("rolled_back"):
            results.append({"arm": label(arm), "status": "failed", "reason": "; ".join(res.get("errors") or ["apply failed"])})
            continue
        vals = []
        crashed = None
        for rep in range(int(exp.get("repeats") or 1)):  # not `i`: that is the arm index of the while loop (#142 re-ran one arm forever)
            try:
                run, _ = _bench_run(c, prof, f"exp#{eid} {label(arm)}" + (f" ({rep + 1}/{exp['repeats']})" if (exp.get("repeats") or 1) > 1 else ""))
            except (TooSlow, ServerDown, BenchFailed) as e:
                # one value that OOM-kills mysqld or breaks the benchmark is a result of the sweep, not the end of the session
                # (suite r2 RO-4.ns: buffer pool 12G on a 15.6 GiB host without swap ended the session)
                crashed = str(e)[:300]
                break
            if run.get("goal_value") is not None:
                vals.append(run["goal_value"])
        if crashed:
            log(f"   {label(arm)}: failed - {crashed}; back to the parent configuration")
            results.append({"arm": label(arm), "status": "failed", "reason": crashed})
            c.event("experiment", f"experiment #{eid}: {label(arm)} failed, reverted; the sweep continues", {"error": crashed})
            _goto_managed(c, dict(parent), f"experiment #{eid}: {label(arm)} failed", f"exp:{eid}")
            x = _sv(arm[0]["variable"], arm[0]["value"])
            if "OOM" in crashed and x is not None and pv is not None and x != pv:
                # more of a memory setting needs more memory: values further out on this side would be killed too
                sd = 1 if x > pv else -1
                oom_at[sd] = min(oom_at.get(sd, float("inf")), abs(x - pv))
            continue
        mean = sum(vals) / len(vals) if vals else None
        d = _improvement(goal, mean, pm["goal"]) if (pm and mean is not None) else None
        results.append({"arm": label(arm), "status": "measured", "runs": len(vals), "goal_mean": round(mean, 4) if mean is not None else None,
                        "delta_pct": d, "verdict": None if d is None else ("better" if d > noise else "worse" if d < -noise else "within noise")})
        log(f"   {label(arm)}: {mean} ({'' if d is None else f'{d:+.1f}%'})")
        if auto and pv is not None and d is not None and d < -noise:
            x = _sv(arm[0]["variable"], arm[0]["value"])
            if x is not None and x != pv:
                sd = 1 if x > pv else -1
                worse_at[sd] = min(worse_at.get(sd, float("inf")), abs(x - pv))
        if screen and d is not None:
            st = state.get(side, "screen")
            best_on[side] = max(best_on.get(side, d), d)
            if st == "screen":
                # sensitivity first: the widest step on this side. Clearly flat -> the rest of the side is skipped; a hint
                # (between half the threshold and the threshold) -> the next value is measured too; beyond noise -> refine
                state[side] = "sensitive" if abs(d) > noise else "hint" if abs(d) > noise / 2 else "flat"
                if state[side] == "flat":
                    log(f"   screened flat this way ({d:+.1f}%, threshold ±{noise}%): no smaller step can be told apart from noise")
            elif st == "hint":
                state[side] = "sensitive" if abs(d) > noise else "flat"
            elif st == "sensitive" and shape == "saturating" and side > 0 and d > noise and d >= best_on[side] - noise:
                # more is better up to a point: refining upward from the smallest value, the first one within noise of the
                # best measured is the knee - larger values only cost more
                state[side] = "knee"
    measured = [r for r in results if r["status"] == "measured" and r.get("delta_pct") is not None]
    best = max(measured, key=lambda r: r["delta_pct"], default=None)
    keep = _good_enough(measured, best, noise, spec.get("variable"), pv, shape) if best and best["delta_pct"] > noise else None
    if keep:
        arm = next(a for a in arms if label(a) == keep["arm"])
        target = dict(parent)
        for x in arm:
            target[x["variable"]] = x["value"]
        _goto_managed(c, target, f"experiment #{eid}: best value, {keep['delta_pct']:+.1f}% vs parent", f"exp:{eid}")
        msg = f"kept {keep['arm']} ({keep['delta_pct']:+.1f}% vs parent mean {pm['goal']:.4g}, noise ±{noise}%)"
        if keep is not best:
            msg += f"; within noise of the best ({best['arm']} {best['delta_pct']:+.1f}%) and cheaper / rounder"
    else:
        _goto_managed(c, parent, f"experiment #{eid}: no value beat the parent beyond noise", f"exp:{eid}")
        msg = "no value beat the parent configuration beyond noise; parent kept" + (f" (best: {best['arm']} {best['delta_pct']:+.1f}%)" if best else "")
    if problems:
        msg += " | " + " | ".join(problems)
    c.client.post(f"/experiments/{eid}", {"status": "done" if measured else "failed", "message": msg,
                                          "results": {"parent_mean": pm["goal"] if pm else None, "parent_runs": pm["seqs"] if pm else [],
                                                      "noise_pct": noise, "arms": results, "kept": keep["arm"] if keep else None}})
    return msg


def _queued_experiments(c, sess):
    try:
        return c.client.get(f"/sessions/{sess['id']}/experiments?status=queued").get("experiments", [])
    except Exception:
        return []


def cmd_profile(a):
    """Ad-hoc CPU profile of the database (and the host) for N seconds."""
    from . import cpuprof
    c = Ctx(need_server=False)
    if not cpuprof.ensure_perf(log):
        raise Fail("perf is not available (install linux-tools for this kernel)")
    p = cpuprof.Profiler(c.state.dir, a.seconds, a.hz)
    log(f"profiling for {a.seconds}s at {a.hz} Hz per CPU ...")
    p.start()
    p.join(a.seconds + 120)
    res = p.process(set(hostinfo.service_pids(hostinfo.mysqld_pid() or 0) or []))
    if a.folded:
        with open(a.folded, "w") as f:
            for k, v in res.get("folded") or []:
                f.write(f"{k} {v}\n")
    lines = [f"{res.get('samples')} samples, {res.get('db_samples')} in the database; call stacks: {res.get('call_stacks')}",
             "Host: " + ", ".join(f"{k} {v}%" for k, v in (res.get("by_process") or {}).items()),
             "Database CPU by category:"] + [f"  {v:5.1f}%  {k}" for k, v in (res.get("categories") or {}).items()] + \
            ["Top functions (self):"] + [f"  {x['pct']:5.1f}%  {x['fn']}" for x in (res.get("top_self") or [])[:15]]
    emit({k: v for k, v in res.items() if k != "folded" or a.json_folded}, "\n".join(lines) if not res.get("error") else res["error"])


def cmd_activity(a):
    """Report what is happening on this host (for scripts: e.g. loading benchmark data before a session)."""
    c = Ctx()
    c.activity(a.phase, a.detail, a.seconds)


def cmd_llm_context(a):
    """Print the context the tuner would send to the LLM now (for audits / replays)."""
    c = Ctx()
    sess = c.session()
    ctx = _engine_ctx(c, sess)
    res = engine.run_engine(ctx)
    try:
        meta = c.db.variables_meta()
    except Exception:
        meta = {}
    print(json.dumps(llmassist.context(ctx, res, meta), indent=1, default=str))


def cmd_diag(a):
    """Diagnostic instrumentation switches (runtime only, reset by a mysqld restart)."""
    c = Ctx(need_server=False)
    if a.what == "waits":
        if a.state == "status":
            emit(c.db.wait_coverage() or {}, json.dumps(c.db.wait_coverage()))
            return
        sess = c.state.get("session")
        if sess and not a.force:
            raise Fail(f"session #{sess['id']} is active: changing instrumentation changes performance and would skew its runs "
                       "(pass --force to do it anyway)")
        c.db.set_sync_waits(a.state == "on")
        cov = c.db.wait_coverage() or {}
        emit({"ok": True, "coverage": cov},
             f"mutex/rwlock/condition wait instruments {'enabled' if a.state == 'on' else 'disabled'} until the next restart: "
             f"{cov.get('enabled')} of {cov.get('instruments')} wait instruments timed ({cov.get('sync_enabled')} synchronisation)."
             + (" Expect a few % lower throughput while on." if a.state == "on" else ""))


def cmd_study(a):
    c = Ctx()
    if a.action == "create":
        if not a.name:
            raise Fail("study name required")
        r = c.client.post("/studies", {"name": a.name, "kind": a.kind, "goal": a.goal, "target": a.target, "notes": a.notes})
        emit(r, f"Study #{r['id']} '{r['name']}': {r['url']}\nAttach sessions with `tuning-agent session start --study \"{r['name']}\" ...` "
                "on each environment.")
        return
    studies = c.client.get("/studies")["studies"]
    if a.action == "list":
        emit({"studies": studies}, "\n".join(f"#{s['id']:<4} {s['name']:<40} {s['kind']:<13} {len(s['sessions'])} session(s)  {s['url']}"
                                              for s in studies) or "No studies yet.")
        return
    st = next((s for s in studies if a.name and (str(s["id"]) == a.name or s["name"] == a.name)), None)
    if not st:
        raise Fail(f"study {a.name!r} not found")
    lat = st.get("goal") == "latency"
    lines = [f"Study #{st['id']} {st['name']} ({st['kind']}, goal {st.get('goal')}" + (f", target {st['target']:g}" if st.get("target") else "") + ")"]
    for s in st["sessions"]:
        e, r = s["environment"], s["results"]
        bs = (r.get("best_safe") or {}).get("mean")
        meets = "" if not st.get("target") or bs is None else ("  meets target" if (bs <= st["target"] if lat else bs >= st["target"]) else "  below target")
        lines.append(f"  {e['name']:<16} {e.get('cpus')} CPU {round((e.get('ram_bytes') or 0) / 2 ** 30)} GiB  session #{s['id']} "
                     f"first {((r.get('first') or {}).get('value'))!s:>10}  best safe {bs!s:>10}  best {((r.get('best') or {}).get('mean'))!s:>10}{meets}")
    emit(st, "\n".join(lines))


def cmd_experiment(a):
    c = Ctx()
    sess = c.session()
    if a.action == "add":
        if a.sweep:
            var, _, vals = a.sweep.partition("=")
            body = {"kind": "sweep", "variable": var, "values": vals}
        elif a.try_:
            body = {"kind": "try", "settings": "\n".join(a.try_)}
        else:
            raise Fail("use --sweep var=v1,v2,... or --try var=value (repeatable)")
        body.update({"repeats": a.repeats, "reason": a.reason, "force": a.force, "created_by": "cli"})
        r = c.client.post(f"/sessions/{sess['id']}/experiments", body)
        emit({"ok": True, "id": r["id"]}, f"Experiment #{r['id']} queued; autotune runs it next, or run it now with `tuning-agent experiment run --bench PROFILE`.")
    elif a.action == "list":
        ex = c.client.get(f"/sessions/{sess['id']}/experiments?status=").get("experiments", [])
        emit({"experiments": ex}, "\n".join(f"#{e['id']} {e['status']:<9} {e['kind']:<5} {json.dumps(e['spec'])[:80]}  {e.get('message') or ''}" for e in ex) or "No experiments.")
    elif a.action == "run":
        prof_path = a.bench or sess.get("bench")
        if not prof_path:
            raise Fail("experiments are measured with a benchmark profile: pass --bench PROFILE")
        prof = bench.Profile(prof_path)
        done = []
        for e in _queued_experiments(c, sess):
            log(f"== experiment #{e['id']} ({e['kind']}, from {e.get('created_by')})")
            done.append({"id": e["id"], "result": _run_experiment(c, prof, e)})
        _finish_pending_restart(c)
        emit({"ok": True, "ran": done}, "\n".join(f"#{d['id']}: {d['result']}" for d in done) or "No queued experiments.")


def _safe_wins_in_tradeoff_phase(sess, best_safe):
    """Changes of safe experiments that improved while the configuration already carried trade-offs, and are not in the
    best safe configuration: [{variable, new, source}]."""
    safe_cfg = next((r.get("managed") or {} for r in sess["runs"] if r["seq"] == best_safe["seq"]), {})
    out, seen = [], set()
    for r in sess["runs"]:
        if r.get("verdict") != "improved" or (r.get("evaluation") or {}).get("confirmed") is False or not added_tradeoffs(r, start_tradeoffs(sess["runs"])):
            continue
        own = [x for x in r.get("changes") or [] if str(x.get("source", "")).startswith("rec:")
               and not str(x["source"])[4:].startswith(("revert", "rollback"))]
        if not own or tradeoffs_in({x["variable"]: x.get("new") for x in own}):
            continue
        # levers whose effect depends on the trade-offs (log writer threads off: +25% at trx_commit=2, -26% / -41% on durable
        # commits in W1 288 / 285) are measured here like any other: judged against best safe, one run when it loses. Left
        # out, a lever first measured after a trade-off never reached the safe configuration (deploy canary #310)
        for x in own:
            cur = safe_cfg.get(x["variable"])
            cur = cur.get("value") if isinstance(cur, dict) else cur
            if normalize(x["variable"], cur) != normalize(x["variable"], x.get("new")) and x["variable"] not in seen:
                seen.add(x["variable"])
                out.append({"variable": x["variable"], "new": x.get("new"), "source": x["source"]})
    return out


def cmd_autotune(a):
    """Run the loop; whatever happens, leave a truthful status behind (a finished autotune left 'measuring' on the
    dashboard until a person reset it) and close the session when asked to."""
    try:
        _autotune(a)
    finally:
        try:
            c = Ctx()
            sess = c.session() or {}
            if getattr(a, "close", False) and sess.get("id"):
                cmd_session(argparse.Namespace(action="stop"))
            elif sess.get("id"):
                c.activity("idle", f"autotune finished ({len(sess.get('runs') or [])} runs); session #{sess['id']} still open - "
                                   "tuning-agent session stop when done")
                log(f"Session #{sess['id']} is still open: `tuning-agent session stop` when done (or run autotune with --close).")
        except Exception:  # noqa - never mask the loop's own outcome
            pass


def _autotune(a):
    """Unattended loop: calibrate (if needed) -> recommend -> apply next -> measure (bench) ... -> best config confirmation."""
    c = Ctx()
    sess = c.session()
    prof = bench.Profile(a.bench)
    if sess.get("bench_name") != prof.name:  # noise history is kept per environment + benchmark
        sess["bench_name"] = prof.name
        c.state.set("session", sess)
    if a.tradeoffs and sess["policy"] != "benchmark":
        raise Fail("--tradeoffs needs a session with --policy benchmark")
    if str(c.cfg.get("debug_symbols", "auto")).lower() != "off":
        from . import probes
        try:
            probes.ensure_debug_symbols(log)
        except Exception as e:  # profiles still work with exported symbols
            log(f"debug symbols: {e}")
    cn = _cap_nice_prep(c)
    if cn and not sess.get("runs"):
        log("== host preparation: CAP_SYS_NICE for mysqld (thread priorities for the purge triage; a restart, no effect by itself)")
        _apply_and_record(c, [dict(cn, source="host-prep")], "autotune")
    if "filesystem" in (sess.get("scope") or []) and c.all_settings().get("os:datadir_fs") == "rootfs":
        log("== scope includes filesystem: moving the data directory to ext4 on the experiment device first (fair baseline)")
        _apply_and_record(c, [{"variable": "os:datadir_fs", "value": "ext4", "reason": "filesystem scope: common baseline device",
                               "source": "fs-baseline"}], "autotune")
    if prof.data_state == "seasoned" and not sess.get("seasoning") and prof.season_hours > 0:
        _season(c, prof)
    if prof.auto_warmup or prof.auto_duration:
        plan = sess.get("run_plan")
        if not plan:
            out = _run_length_probe(c, prof, prof.run_length_max, prof.noise_target)
            plan = {"warmup_s": out["recommended_warmup_s"] if prof.auto_warmup else int(prof.warmup),
                    "duration_s": (out.get("recommended_duration_s") or prof.duration) if prof.auto_duration else prof.duration,
                    "auto": [k for k, on in (("warmup", prof.auto_warmup), ("duration", prof.auto_duration)) if on],
                    "predicted_within_noise_pct": (out.get("measure") or {}).get("predicted_within_noise_pct"),
                    "target_pct": (out.get("measure") or {}).get("target_pct"), "complete": out["complete"],
                    "settling": {k: x["settled_after_s"] for k, x in out["metrics"].items()},
                    "unbounded": [u["label"] for u in out["unbounded"]],
                    "unstable": bool(out.get("unstable")), "triage_kept": (out.get("triage") or {}).get("kept") or []}
            sess = c.session()
            sess["run_plan"] = plan
            c.state.set("session", sess)
            _post_run_plan(c, sess)
        if plan.get("unstable") and not getattr(a, "allow_unstable", False):
            # no configuration found whose undo history stays bounded: every run would measure the drift, and the best
            # of them could not run this load in production (user, session 328: 40 + 10 min runs destined to be useless)
            msg = ("autotune stopped: no stable configuration - the undo history grows without bound under this load with every "
                   "remedy tried (see the purge triage). Lower the client concurrency, or run with --allow-unstable to tune anyway.")
            log("== " + msg)
            c.event("session", msg, {"triage": plan.get("triage_kept"), "unstable": True})
            return
        prof.warmup, prof.duration = str(plan["warmup_s"]), int(plan["duration_s"])
        prof.base_warmup, prof.base_duration = prof.warmup, prof.duration
        prof.timeout = max(prof.timeout, (plan["warmup_s"] + plan["duration_s"]) * 3)
        log(f"== run length: warm-up {plan['warmup_s']} s, measurement {plan['duration_s']} s ({', '.join(plan['auto'])} from the run-length probe)")
    if not sess.get("noise"):
        log(f"== calibration: {a.calibrate} runs of the current configuration")
        c.activity("calibrating", f"{a.calibrate} runs of the starting configuration to measure noise")
        # calibration runs of this configuration from an earlier, interrupted autotune (an aborted run, a restarted
        # harness) count: the calibration continues instead of starting over at 1/3 (#162)
        key = evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()})
        runs = [r for r in c.session()["runs"] if str(r.get("label") or "").startswith("baseline")
                and evaluate.config_key(r.get("managed")) == key]
        if runs:
            log(f"   continuing the calibration: {len(runs)} run(s) of this configuration already measured")
        extra = 0
        while len([r for r in runs if r.get("verdict") not in evaluate.EXCLUDED]) < a.calibrate:
            run, _ = _bench_run(c, prof, f"baseline ({len(runs) + 1}/{a.calibrate + extra})")
            log(f"   run {run['seq']}: goal {run['goal_value']}" + (" (unreliable: CPU steal, measured again)" if run.get("verdict") == "unreliable" else ""))
            runs.append(run)
            cl = evaluate.client_saturated(run.get("summary") or {})
            if cl and str(c.cfg.get("allow_noisy_environment", "false")).lower() != "true":
                msg = (f"environment unfit for tuning: the benchmark client limits the measurement ({cl}) - no server setting "
                       "can show an effect. Use a bigger client host or a lighter client (or set allow_noisy_environment = true).")
                c.event("environment", msg, {"client_saturated": cl, "run": run["seq"]})
                raise Fail(msg)
            if run.get("verdict") == "unreliable":
                extra += 1
                if extra > a.calibrate:  # the host is too noisy right now: calibrate with what we have
                    log("   WARNING: persistent CPU steal; calibrating with the runs available")
                    break
        runs = [r for r in runs if r.get("verdict") not in evaluate.EXCLUDED] or runs
        slide = evaluate.calibration_slide(runs)
        if slide:
            # every run moved the same way by more than each run's own precision explains: the starting state is still
            # settling (JOB: a 3-run slide set the threshold to ±5% and hid a ~2.8% read-ahead gain) - one more run, and
            # calibrate on the latest ones
            log(f"   calibration slide: {slide}; one more calibration run")
            run, _ = _bench_run(c, prof, f"baseline ({len(runs) + 1}, after a calibration slide)")
            log(f"   run {run['seq']}: goal {run['goal_value']}")
            runs = (runs + [run])[-a.calibrate:]
        sess = c.session()
        cal = evaluate.calibrate(runs)
        _set_calibration(c, sess, cal)
        _reevaluate(c, c.session())
        log(_fmt_cal(cal))
        runs = _noise_plan(c, prof, runs, a.calibrate)
        _review_run_plan(c, runs)

    sess = c.session()
    sess["bench"] = os.path.abspath(a.bench)
    c.state.set("session", sess)
    measured = iterations = 0
    while measured < a.max_runs and iterations < a.max_runs * 3:
        iterations += 1
        sess = c.session()
        queued = _queued_experiments(c, sess)
        if queued:  # human / agent specified experiments go first
            e = queued[0]
            log(f"== experiment #{e['id']} ({e['kind']}, from {e.get('created_by')}): {json.dumps(e['spec'])[:120]}")
            if not str(e.get("created_by") or "").startswith(("agent:", "llm:")):  # a person asked for it
                sess["pending_hypothesis"] = {"id": f"exp#{e['id']}", "kind": "requested",
                                              "text": f"Requested by {e.get('created_by') or 'a user'}" + (f": {e['reason']}" if e.get("reason") else "")}
                c.state.set("session", sess)
            n0 = len(c.session()["runs"])
            log("   " + _run_experiment(c, prof, e))
            measured += max(1, len(c.session()["runs"]) - n0)  # runs it really made (screening skips, extensions add)
            continue
        if _queue_promising_retests(c):
            continue  # the queued re-tests run first, as experiments
        c.activity("choosing next experiment", "diagnosis and recommendation")
        res = _recommend(c, sess)
        nxt = res.get("next")
        # done for this session - unless the engine asks for it again (memory settings after the swap state changed)
        blocked = set(sess.get("failed_recs") or []) - {i["id"] for i in res["items"] if i.get("retest")}
        if nxt in blocked:
            alt = next((i for i in res["items"] if i["kind"] in ("config", "revert") and i.get("confidence", 0) > 0
                        and i["id"] not in blocked and (i.get("safety") == "safe" or a.tradeoffs)), None)
            if not alt:
                log(f"== {nxt} failed before and nothing else is left; stopping")
                break
            log(f"== {nxt} failed before; next candidate: {alt['id']}")
            nxt = alt["id"]
        item = next((i for i in res["items"] if i["id"] == nxt), None)
        # benchmark policy: keep the last 30% of the budget for trade-offs, otherwise safe experiments use it all up
        # ...unless the safe candidate targets the dominant measured bottleneck (r1 HC-3: thread pool for 3700 threads on
        # 16 CPUs, 97% of runnable time waiting for a CPU, was displaced by ps-off and never ran)
        # relevance 4 (the dominant measured time, >= 60%) and at most twice: with relevance 3 nearly every safe item was
        # "targeted", and 10 deploy canaries in a row never reached a trade-off (canary review 2026-10-22)
        targeted = (bool(item) and (item.get("relevance") or 0) >= 4 and not item.get("no_evidence") and not item.get("no_mechanism")
                    and sess.get("tradeoff_overrides", 0) < 2)
        in_tradeoff_budget = a.tradeoffs and measured >= int(a.max_runs * 0.7)
        if in_tradeoff_budget and targeted and item.get("safety") == "safe":
            sess["tradeoff_overrides"] = sess.get("tradeoff_overrides", 0) + 1
            c.state.set("session", sess)
        if in_tradeoff_budget and not targeted and not res.get("planner_decided") and (not item or item.get("safety") == "safe" and item["kind"] != "revert"):
            tried = {r["label"].split(" ")[0] for r in sess["runs"]}
            trade = next((i for i in res["items"] if i.get("safety") == "trade-off" and i["kind"] == "config" and i.get("confidence", 0) > 0
                          and i["id"] not in tried and i["id"] not in blocked), None)
            if trade:
                log(f"== trade-off budget: {measured} of {a.max_runs} runs used, switching to trade-off experiments")
                item, nxt = trade, trade["id"]
        last3 = [r.get("verdict") for r in sess["runs"][-3:]]
        if len(last3) == 3 and all(v == "unreliable" for v in last3):
            # the environment, not the configuration: 303 measured 25 runs, every one unreliable (steady 2-2.7% steal under
            # the old absolute rule), each experiment twice
            why = ("Stopped: the last 3 runs were all unreliable (" + "; ".join(sorted({str((r.get("evaluation") or {}).get("note") or "")[:80]
                   for r in sess["runs"][-3:]})) + "). Fix the environment (see the run notes) before tuning further.")
            log("== " + why)
            c.event("stop", why, {"runs": measured})
            break
        fruitless = _fruitless_since_gain(sess)
        if item and item["kind"] != "revert" and len(fruitless) >= FRUITLESS_STOP:
            # diminishing returns: 26% of recent runs came after a session's last gain (tails of 18-21 runs) and the ceiling
            # check rarely trips. Safe ideas that long without a gain hand over to the trade-offs; after that, stop
            start_ = start_tradeoffs(sess["runs"])
            trade_tried = any(added_tradeoffs(r, start_) for r in fruitless)
            tried = {r["label"].split(" ")[0] for r in sess["runs"]}
            trade = next((i for i in res["items"] if i.get("safety") == "trade-off" and i["kind"] == "config" and i.get("confidence", 0) > 0
                          and i["id"] not in tried and i["id"] not in blocked), None) if a.tradeoffs and not trade_tried else None
            if trade and item.get("safety") == "safe":
                log(f"== {len(fruitless)} experiments since the last gain: switching to trade-off experiments ({trade['id']})")
                item, nxt = trade, trade["id"]
            else:
                why = (f"Diminishing returns: {len(fruitless)} experiments since the last gain (runs "
                       f"{fruitless[0]['seq']}-{fruitless[-1]['seq']}) without one.")
                log("== " + why)
                c.event("stop", why, {"runs": measured})
                break
        if not item and res.get("stop_reason"):
            log("== " + res["stop_reason"])
            c.event("stop", res["stop_reason"], {"runs": measured})
            trade = next((i for i in res["items"] if i.get("safety") == "trade-off" and i["kind"] == "config" and i.get("confidence", 0) > 0
                          and i["id"] not in {r["label"].split(" ")[0] for r in sess["runs"]}), None) if a.tradeoffs and not res.get("planner_decided") else None
            if trade:
                log(f"== safe ideas exhausted: switching to trade-off experiments ({trade['id']})")
                item, nxt = trade, trade["id"]
        if not item:
            log("== no further experiments suggested")
            break
        if item.get("safety") == "trade-off" and not a.tradeoffs:
            log(f"== safe experiments exhausted; next would be trade-off {nxt} (re-run with --tradeoffs to continue)")
            break
        if a.recheck_every and item["kind"] != "revert":
            cur = evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()})
            mine = [r for r in sess["runs"] if evaluate.config_key(r.get("managed")) == cur]
            since = len([r for r in sess["runs"] if r["seq"] > max([r["seq"] for r in mine] or [0])])
            # no time-separated repeat yet: the noise threshold is still the conservative default, so re-check early
            no_long_term = not (((sess.get("noise") or {}).get("estimate") or {}).get("long_term_dof"))
            due = since >= (min(a.recheck_every, 3) if no_long_term else a.recheck_every)
            # pending changes that only lead back to an already-measured configuration (a revert) do not block a re-check
            if mine and due and (not c.state.get("pending_changes") or _known_config(c, sess)):
                log(f"== re-check: measuring the current configuration again ({since} runs since it was last measured) to control drift")
                run, _ = _bench_run(c, prof, f"re-check (run {mine[-1]['seq']} config)")
                measured += 1  # a run like any other: --max-runs is the session's cost (r1 HC-3: 26 runs on a budget of 16)
                log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}"
                    + (" (DRIFT)" if (run.get('evaluation') or {}).get('drift') else ""))
                continue
        if item.get("sweep") and item.get("hypothesis"):
            log(f"   why: {item['hypothesis']['text']}")
            sess = c.session()
            sess["pending_hypothesis"] = dict(item["hypothesis"], id=nxt)
            c.state.set("session", sess)
        if item.get("sweep"):  # measure several values as an experiment instead of a single step
            body = {"kind": "sweep", "variable": item["sweep"]["variable"], "values": ",".join(item["sweep"]["values"]),
                    "reason": item["rationale"][:400], "created_by": ("llm:" if item.get("source") == "llm" else "agent:") + nxt}
            eid = c.client.post(f"/sessions/{sess['id']}/experiments", body)["id"]
            exp = next((e for e in _queued_experiments(c, sess) if e["id"] == eid), None)
            if exp:
                log(f"== sweep {nxt}: {item['sweep']['variable']} = {', '.join(item['sweep']['values'])} (experiment #{eid})")
                n0 = len(c.session()["runs"])
                log("   " + _run_experiment(c, prof, exp))
                sess = c.session()
                measured += len(sess["runs"]) - n0  # screening skips points, the optimum extension adds some
                # the sweep left the server on the value it kept (the cheapest / roundest within noise of the best): an
                # improved sweep point is a single run like any other improvement - confirm that configuration
                cur = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
                pts = [r for r in sess["runs"] if str(r.get("label") or "").startswith(f"exp#{eid} ")
                       and evaluate.config_key(r.get("managed")) == evaluate.config_key(cur)]
                if pts and pts[-1].get("verdict") in ("improved", "efficiency-gain") and measured < a.max_runs:
                    kp = pts[-1]
                    log(f"   sweep point run {kp['seq']} improved: measuring it again to confirm")
                    run, _ = _bench_run(c, prof, f"exp#{eid} {item['sweep']['variable']}={cur.get(item['sweep']['variable'])} (confirm)")
                    measured += 1
                    _confirm(c)
            sess = c.session()
            sess.setdefault("failed_recs", []).append(nxt)  # one sweep per rule and session
            c.state.set("session", sess)
            continue
        log(f"== experiment: {nxt} ({item['title']}) [{item.get('safety')}]")
        if item.get("hypothesis"):
            log(f"   why: {item['hypothesis']['text']}")
            sess = c.session()
            sess["pending_hypothesis"] = dict(item["hypothesis"], id=nxt)
            c.state.set("session", sess)
        c.activity("applying changes", f"{nxt}: {item['title']}" + (" (restart)" if item.get("restart") else ""))
        changes = [{"variable": ch["variable"], "value": None if ch.get("unset") else ch["proposed"], "reason": item["rationale"],
                    "source": f"rec:{nxt}"} for ch in item["changes"]]
        before_exp = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
        resa = _apply_and_record(c, changes, "autotune")
        if resa.get("rolled_back"):
            log(f"   apply failed and was rolled back: {resa.get('errors')}")
            sess = c.session()
            sess.pop("pending_hypothesis", None)
            sess.setdefault("failed_recs", []).append(nxt)
            c.state.set("session", sess)
            continue
        sess = c.session()
        if sess.get("last_recommendation"):
            sess["last_recommendation"]["applied"] = True
            c.state.set("session", sess)
        if _known_config(c, sess):
            log("   configuration already measured: no new run needed")
            sess = c.session()
            sess.pop("pending_hypothesis", None)
            if item["kind"] != "revert":  # an experiment that only leads back to a measured configuration is done
                sess.setdefault("failed_recs", []).append(nxt)
            else:
                # reverts cost no run, so two of them could alternate forever (r1: 24 times until the iteration cap): a
                # configuration reached twice by a revert without a measurement in between ends that revert
                key = repr(evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()}))
                seen = sess.setdefault("revert_targets", [])
                if key in seen:
                    log("   reverts are going back and forth between measured configurations: no more reverts until the next run")
                    sess.setdefault("failed_recs", []).append(nxt)
                seen.append(key)
            c.state.set("session", sess)
            continue
        sess = c.session()
        sess["revert_targets"] = []
        c.state.set("session", sess)
        try:
            run, _ = _bench_run(c, prof, f"{nxt}")
        except (TooSlow, ServerDown, BenchFailed) as e:
            why = ("OOM-killed mysqld" if "OOM-killed" in str(e) else "made the benchmark too slow" if isinstance(e, TooSlow)
                   else "broke the benchmark" if isinstance(e, BenchFailed) else "took mysqld down")
            log(f"   {str(e)[:600]}; reverting {nxt}")
            c.event("experiment", f"{nxt} {why}: reverted, the session continues", {"error": str(e)[:1000]})
            cur = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
            undo = [{"variable": k, "value": before_exp.get(k), "reason": f"autotune: {nxt} {why}", "source": "rollback"}
                    for k in set(cur) | set(before_exp) if normalize(k, cur.get(k)) != normalize(k, before_exp.get(k))]
            _apply_and_record(c, undo, "rollback")
            sess = c.session()
            sess.setdefault("failed_recs", []).append(nxt)
            c.state.set("session", sess)
            measured += 1
            continue
        measured += 1
        log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
        ev0 = run.get("evaluation") or {}
        d0, nz0 = ev0.get("goal_delta_pct"), float(ev0.get("goal_noise_pct") or 0)
        if run["verdict"] == "improved" and d0 is not None and nz0 and d0 >= CONFIRM_MARGIN * nz0 and (sess.get("noise") or {}).get("runs"):
            # far beyond the threshold, luck cannot explain it: 112 confirmations of gains at 2x the threshold or more, 0 failed
            # (all 6 failures were below 1.1x). Kept without a repeat - re-checks later still catch drift
            r0 = next((x for x in c.session()["runs"] if x["seq"] == run["seq"]), run)
            r0["evaluation"] = dict(ev0, confirmed=True, ever_confirmed=True, confirmed_by=f"margin: {d0:+.1f}% is {d0 / nz0:.1f}x the threshold")
            if r0.get("id"):
                c.client.post(f"/runs/{r0['id']}/evaluation", {"verdict": r0["verdict"], "evaluation": r0["evaluation"]})
            sess_ = c.session()
            for x in sess_["runs"]:
                if x["seq"] == run["seq"]:
                    x["evaluation"] = r0["evaluation"]
            c.state.set("session", sess_)
            log(f"   improvement of {d0:+.1f}% is {d0 / nz0:.1f}x the ±{nz0}% threshold: kept without a confirmation run")
        elif run["verdict"] in ("improved", "efficiency-gain") and measured < a.max_runs:
            # a single run above the threshold can be luck (winner's curse): measure it again before building on it. An
            # efficiency gain is kept with the goal within noise: one low run can hide a real loss (canary #141)
            log(f"   {'improvement' if run['verdict'] == 'improved' else 'efficiency gain'}: measuring the same configuration again to confirm it")
            first = run
            run, _ = _bench_run(c, prof, f"{nxt} (confirm)")
            measured += 1
            log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
            nz = float((first.get("evaluation") or {}).get("goal_noise_pct") or 0)
            if nz and first.get("goal_value") and run.get("goal_value") is not None and measured < a.max_runs \
                    and abs(run["goal_value"] - first["goal_value"]) / abs(first["goal_value"]) * 100 > 2 * nz:
                # the two runs disagree by more than twice the noise: one of them is wrong - a third run decides instead of
                # a wild pair inflating the session's noise (r1 RW-1: 1955 vs 2148, noise 2% -> 8%)
                log("   the confirmation disagrees with the first run by more than twice the noise threshold: a third run decides")
                run, _ = _bench_run(c, prof, f"{nxt} (confirm 2)")
                measured += 1
                log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
            _confirm(c)  # the improvement must hold for the mean of its runs
        elif run["verdict"] == "neutral" and measured < a.max_runs:
            ev = run.get("evaluation") or {}
            d, nz = ev.get("goal_delta_pct"), ev.get("goal_noise_pct")
            if d is not None and nz and nz / 2 < d <= nz:
                # within the threshold but above half of it: one more run (two runs resolve smaller differences) instead of
                # calling it noise - the noise plan asked for repeats that single runs never got (r1 KV-1: 1.4-3% gains)
                sess = c.session()
                r0 = next(x for x in sess["runs"] if x["seq"] == run["seq"])
                r0["evaluation"] = dict(ev, hint_repeat=True)
                c.state.set("session", sess)
                if r0.get("id"):
                    c.client.post(f"/runs/{r0['id']}/evaluation", {"verdict": r0["verdict"], "evaluation": r0["evaluation"]})
                log(f"   within noise but above half the threshold ({d:+.1f}%, ±{nz}%): measuring it again")
                run, _ = _bench_run(c, prof, f"{nxt} (repeat: within noise)")
                measured += 1
                log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
                _confirm(c)  # the mean of both runs against the (tighter) two-run threshold
        ue = run.get("evaluation") or {}
        deterministic = ue.get("config_mismatch") or ue.get("client_saturated") or ue.get("goal_server_mismatch_pct") is not None \
            or ue.get("client_stalled")
        cf = ue.get("client_failures") or {}
        sm_ = run.get("summary") or {}
        nxt_h = (int(prof.warmup) if str(prof.warmup).isdigit() else 0) / 3600 + prof.duration / 3600
        if cf.get("disk_full") or (sm_.get("datadir_full_in_h") is not None and sm_["datadir_full_in_h"] < 2 * nxt_h) \
                or (sm_.get("datadir_free_pct") is not None and sm_["datadir_free_pct"] < 2):
            why = (cf.get("text") or "") if cf.get("disk_full") else \
                (f"the data disk fills: {sm_.get('datadir_free_gb')} GiB free, growing {sm_.get('datadir_growth_gb_per_h')} GiB/h "
                 f"(full in ~{sm_.get('datadir_full_in_h')} h)")
            log("== stopping: " + why[:300])
            c.event("stop", "data disk full or about to fill: " + why[:300], {"run": run["seq"]})
            break
        if ue.get("client_stalled"):
            # two stalled runs in a row: the benchmark client is broken, not the configuration - stop instead of measuring
            # nothing for hours (session 222: three 15-minute runs at 1% CPU)
            prev = [r for r in c.session()["runs"] if r["seq"] < run["seq"]][-1:]
            if prev and ((prev[0].get("evaluation") or {}).get("client_stalled")):
                log("== the benchmark client stalled in two runs in a row: stopping - check the client (errors, dead virtual users)")
                c.event("stop", "benchmark client stalled in two consecutive runs", {"runs": [prev[0]["seq"], run["seq"]]})
                break
        if run["verdict"] == "unreliable" and deterministic:
            # the same configuration would be unreliable again (lab 221 / 213: a re-measure that could never pass)
            log("   unreliable for a reason a repeat cannot change: " + str(ue.get("note") or "")[:200])
        elif run["verdict"] == "unreliable":
            log("   the run was unreliable (" + str(ue.get("note") or "transient disturbance")[:120] + "): measuring the same configuration again")
            run, _ = _bench_run(c, prof, f"{nxt} (re-measure: unreliable run)")
            measured += 1
            log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
        shift = (run.get("evaluation") or {}).get("environment_shift")
        storage_change = any(ch["variable"].startswith(("os:datadir_fs", "os:scheduler", "os:read_ahead", "os:mount_"))
                             for ch in item["changes"])
        if shift and storage_change:
            log("   environment probe changed because the experiment changed the storage stack itself: not re-measuring")
        elif shift and run["verdict"] in ("improved", "regressed", "efficiency-gain", "efficiency-loss"):
            log(f"   environment probe shifted ({shift}): measuring the same configuration again before deciding")
            run, _ = _bench_run(c, prof, f"{nxt} (re-measure: environment shift)")
            measured += 1
            log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
    def goto(target, why):
        run = next(r for r in c.session()["runs"] if r["seq"] == target["seq"])
        cur = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
        want = run.get("managed") or {}
        if evaluate.config_key(cur) != evaluate.config_key(want):
            log(f"== switching to {why} (run {target['seq']})")
            changes = [{"variable": k, "value": want.get(k), "reason": f"autotune: {why}", "source": "rollback"}
                       for k in set(cur) | set(want) if normalize(k, cur.get(k)) != normalize(k, want.get(k))]
            _apply_and_record(c, changes, "rollback")

    sess = c.session()
    best, best_safe = sess.get("best"), sess.get("best_safe")
    if a.confirm and best and best_safe and best["seq"] != best_safe["seq"] and best.get("tradeoffs"):
        goto(best, "best configuration with trade-offs")
        _bench_run(c, prof, "best with trade-offs, confirmation")
    if best_safe and a.confirm:
        # safe changes that improved during the trade-off phase were measured on top of trade-offs, so the safe set never
        # saw them: measure them once on the best safe configuration (twice: a single run can be luck)
        extra = _safe_wins_in_tradeoff_phase(c.session(), best_safe)
        if extra:
            goto(best_safe, "best production-safe configuration")
            ids = sorted({x["source"][4:] for x in extra})
            log(f"== safe changes that improved in the trade-off phase, on the best safe configuration: {', '.join(ids)}")
            _apply_and_record(c, [{"variable": x["variable"], "value": x["new"], "reason": f"autotune: {x['source'][4:]} improved in the "
                                   "trade-off phase; measured on the best safe configuration", "source": "autotune"} for x in extra], "autotune")
            c.state.set("parent_hint", list(best_safe.get("runs") or [best_safe["seq"]]))  # judged against best safe
            try:
                for i in (1, 2):
                    run, _ = _bench_run(c, prof, f"best safe + {', '.join(ids)} ({i}/2)")
                    log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
                    if run["verdict"] in ("regressed", "efficiency-loss", "unreliable"):
                        break  # a loss on the first run: no second run (W1 285 / 288: -41% / -26%, measured twice)
            finally:
                c.state.set("parent_hint", None)
            _confirm(c)
            best_safe = c.session().get("best_safe")
    if best_safe:
        goto(best_safe, "best production-safe configuration")
        if a.confirm:
            _bench_run(c, prof, "best safe config, confirmation")
            # the confirmation is one more run of that configuration: re-judge with it, and leave the server on the
            # configuration that is best after it (canary #141 confirmed an efficiency gain 5% low and stayed on it)
            _confirm(c)
            after = c.session().get("best_safe")
            cfg = lambda b: evaluate.config_key(next((r.get("managed") for r in c.session()["runs"] if r["seq"] == b["seq"]), {}))  # noqa: E731
            if after and cfg(after) != cfg(best_safe):
                log(f"   the confirmation changed the best safe configuration: run {best_safe['seq']} -> runs {after['runs']}")
                goto(after, "best production-safe configuration after the confirmation")
    _finish_pending_restart(c)
    sess = c.session()
    out = {"ok": True, "best": sess.get("best"), "best_safe": sess.get("best_safe"),
           "runs": [{k: r.get(k) for k in ("seq", "label", "goal_value", "verdict")} for r in sess["runs"]]}
    lines = [f"Autotune finished: {len(sess['runs'])} runs."]
    for r in sess["runs"]:
        lines.append(f"  run {r['seq']:>2} {r['label']:<34} {r.get('goal_value')!s:>12}  {r.get('verdict')}")
    lines.append(f"Best safe configuration: {sess.get('best_safe')}")
    lines.append(f"Best overall configuration: {sess.get('best')}")
    emit(out, "\n".join(lines))


def _known_config(c, sess):
    if not sess:
        return None
    key = evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()})
    runs = [r for r in sess.get("runs", []) if evaluate.config_key(r.get("managed")) == key and r.get("goal_value") is not None]
    if not runs:
        return None
    return {"runs": [r["seq"] for r in runs], "mean": sum(r["goal_value"] for r in runs) / len(runs)}


def _parse_kv_str(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise Fail(f"expected var=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cmd_rollback(a):
    c = Ctx()
    if c.state.get("measurement"):
        raise Fail("a measurement is in progress")
    sess = c.session(required=False)
    if a.reset:
        target = {}
        what = "server defaults + original option files (no managed settings)"
    else:
        runs = (sess or {}).get("runs", [])
        if a.best or a.best_safe:
            b = (sess or {}).get("best_safe" if a.best_safe else "best")
            if not b:
                raise Fail("no best run yet")
            seq = b["seq"]
        else:
            seq = a.to_run
        run = next((r for r in runs if r["seq"] == seq), None)
        if not run:
            raise Fail(f"run {seq} not found in the active session")
        target = run.get("managed") or {}
        what = f"configuration of run {seq} '{run['label']}'"
    managed = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    changes = []
    for k in set(managed) | set(target):
        if k not in target:
            changes.append({"variable": k, "value": None, "reason": f"rollback to {what}", "source": "rollback"})
        elif normalize(k, managed.get(k)) != normalize(k, target[k]):
            changes.append({"variable": k, "value": target[k], "reason": f"rollback to {what}", "source": "rollback"})
    if not changes:
        emit({"ok": True, "result": None}, "Already at " + what)
        return
    try:  # mysqld down (an interrupted session stopped it mid-restart): start it with the option file as it is, then roll back
        c.db.connect(retries=1, delay=0)
    except Exception:
        ap = Applier(c.cfg, c.state, c.db, log=log)
        log(f"mysqld is not running: starting {ap.service} before the rollback")
        subprocess.run(["systemctl", "start", ap.service], capture_output=True)
        c.db.connect(retries=30, delay=2)
    res = _apply_and_record(c, changes, "rollback")
    hard = [e for e in res.get("errors") or [] if not str(e).startswith("client")]  # an offline client host keeps its settings
    if hard or res.get("rolled_back"):
        # never report a rollback that did not happen: the next session measured "server defaults" with the previous
        # session's whole configuration (deploy canary #308 after #307 was stopped mid-restart)
        raise Fail(f"rollback to {what} failed: " + "; ".join(hard or ["apply rolled back"]))
    emit({"ok": True, "result": res}, f"Rolled back to {what}:\n" + _print_apply(res))


def cmd_config(a):
    c = Ctx(need_server=False)
    ap = Applier(c.cfg, c.state, c.db)
    managed = c.state.get("managed", {})
    emit({"cnf_path": ap.cnf_path, "service": ap.service, "managed": managed},
         f"{ap.cnf_path} (service {ap.service})\n" + "\n".join(f"  {k} = {v['value']}   # {v.get('source')}" for k, v in sorted(managed.items())))


def cmd_daemon(a):
    c = Ctx()
    if c.cfg.get("role") == "client":
        clientrole.daemon(c, log)
        return
    interval = float(c.cfg.get("interval", 10))
    store = SampleStore(c.state.dir)
    stop = {"flag": False}

    def handler(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    last_hello = 0
    device = None
    sampler = None
    last_prune = last_rg = last_load = 0
    rg_err = None
    from . import loaddetect
    loads = None
    log(f"tuning-agent {__version__} daemon: sampling every {interval:.0f}s, server {c.cfg['server']}")
    while not stop["flag"]:
        t0 = time.time()
        try:
            if sampler is None:
                c.db.connect(retries=1)
                device = hostinfo.device_for_path(c.datadir())
                sampler = collector.Sampler(c.db, device)
                sampler.sample()  # prime
                time.sleep(1)
            s = sampler.sample()
            st = c.state.load()
            if any(v in (st.get("managed") or {}) for v in ("rg:innodb_bg_cpus", "rg:purge")) and time.time() - last_rg > 60:
                last_rg = time.time()
                from . import resgroups
                try:
                    if resgroups.ensure(c.db):  # mysqld was restarted outside the agent (e.g. a benchmark reset)
                        log("re-assigned InnoDB background / purge threads to their resource groups")
                except Exception as e:  # never let it break sampling
                    if str(e) != rg_err:
                        log(f"resource group: {e}")
                    rg_err = str(e)
            s["session_id"] = (st.get("session") or {}).get("id")
            s["run_id"] = (st.get("measurement") or {}).get("run_id")
            store.add(s)
        except Exception as e:
            log(f"sample error: {e}")
            sampler = None
        try:
            if time.time() - last_hello > 600:
                try:
                    c.refresh()
                    c.hello()
                    last_hello = time.time()
                except APIError:
                    raise
                except Exception:  # database restarting: refresh facts on a later round
                    last_hello = time.time() - 540
            batch = store.unsent()
            if batch:
                c.client.post("/metrics", {"environment_id": c.env_id(), "samples": batch})
                store.mark_sent([b["ts"] for b in batch])
        except (APIError, Exception) as e:
            log(f"upload error: {e}")
        if time.time() - last_load > 60:
            # a data load (the benchmark's prepare step, a dump restore) can take hours before any session: say so
            last_load = time.time()
            try:
                loads = loads or loaddetect.Tracker(c.datadir())
                rep = loads.report(time.time(), c.db if sampler is not None else None)
                if rep:
                    c.activity(rep[0], rep[1])
            except Exception as e:  # never let it break sampling
                log(f"load detection: {e}")
        if time.time() - last_prune > 3600:
            store.prune()
            last_prune = time.time()
        while not stop["flag"] and time.time() - t0 < interval:
            time.sleep(0.5)
    try:  # stopped on purpose: tell the server, so the UI shows "agent stopped" rather than "offline"
        c.client.timeout = 5
        try:  # the whole host going down (power-off, deletion) rather than the service alone
            st = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            st = ""
        c.client._call("POST", "/bye", {"environment_id": c.env_id(), "host_shutdown": st == "stopping"})  # one attempt: never delay the stop
    except Exception:
        pass


GUIDE = """tuning-agent workflow (for people and coding agents)

  1. tuning-agent facts                      # what hardware / server we tune
  2. tuning-agent baseline [--apply]         # hardware+version based starting config
  3. tuning-agent session start --goal tps|latency [--policy safe|benchmark] [--name NAME] [--noise-pct 2]
  4. start the workload (benchmark) and let it warm up
  5. tuning-agent calibrate --runs 3 --warmup 120 --duration 300     # measure the start config 3x -> noise
     (single runs: tuning-agent measure run --duration 300, or measure start ... measure stop [--external tps=1234])
  6. tuning-agent recommend                  # ranked recommendations with evidence
  7. tuning-agent apply --next | --rec ID [--rec ID2] | --set var=value [--reason "why"]
  8. measure again (measure run); the run is compared with the mean of its parent configuration:
     improved / regressed (goal beyond noise), efficiency-gain / efficiency-loss (goal within noise but CPU or
     I/O per transaction measurably lower / higher), neutral, repeat
  9. repeat 6-8; tuning-agent rollback --best | --to-run N | --reset
 10. tuning-agent session stop

Rules of thumb
  * Change one thing at a time; measure each configuration for the same duration under the same load.
  * Reset the data set between runs if the benchmark grows/changes data; let caches warm up before measuring.
  * Restore it with a FULL copy (rsync -a, cp --reflink=never, a reload) - never a reflink / snapshot clone (cp --reflink,
    ZFS / LVM thin snapshots, cloud volume snapshots, container overlay): the first write to every block copies it, runs
    start slow and speed up, and settings that postpone writes look better than they are. No copy-on-write under the
    data (btrfs: chattr +C before loading). The agent checks the data files after each reset and warns.
    More: https://github.com/Percona-Lab/db-ai-tuner/blob/main/docs/benchmarking.md
  * Re-measure the best and the baseline at the end: differences under the noise level (--noise-pct) are not real.
  * policy=safe never applies durability trade-offs (they are shown as advice); policy=benchmark may.
  * Pass --external key=value on measure stop to record benchmark-reported numbers (e.g. tps=, nopm=, latency=);
    a key equal to the goal (tps / latency) becomes the goal value.
  * Close the session when done (tuning-agent session stop, or autotune --close): an open session shows as in progress
    on the dashboard. Autotune leaves it open on purpose for follow-up experiments.
  * Keep the host's clock synchronised (NTP): run windows and metric times come from it. The server warns at a few
    seconds of skew and refuses a session start at more than a minute.
  * Add --json to any command for machine-readable output.
"""


def cmd_guide(a):
    print(GUIDE)


def main(argv=None):
    global JSON
    p = argparse.ArgumentParser(prog="tuning-agent", description="AI database tuning agent for MySQL-family servers")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=__version__)
    sp = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    _add = sp.add_parser
    sp.add_parser = lambda *x, **kw: _add(*x, parents=[common], **kw)

    s = sp.add_parser("init", help="configure the agent and register this environment")
    s.add_argument("--server")
    s.add_argument("--api-key", dest="api_key")
    s.add_argument("--name")
    s.add_argument("--mysql-user", dest="mysql_user")
    s.add_argument("--mysql-password", dest="mysql_password")
    s.add_argument("--mysql-host", dest="mysql_host")
    s.add_argument("--mysql-port", dest="mysql_port")
    s.add_argument("--mysql-socket", dest="mysql_socket")
    s.add_argument("--cnf-path", dest="cnf_path")
    s.add_argument("--service-name", dest="service_name")
    s.add_argument("--state-dir", dest="state_dir")
    s.add_argument("--no-restart", action="store_true", help="never restart the database server")
    s.add_argument("--no-os-tuning", action="store_true", help="never change operating-system settings")
    s.add_argument("--role", choices=["server", "client"], default=None, help="client = application/benchmark host (no MySQL access needed)")
    s.add_argument("--environment", type=int, help="client role: id of the database environment this host talks to")
    s.add_argument("--fs-device", help="dedicated block device for filesystem experiments (it is reformatted!)")
    s.add_argument("--allow-fs-experiments", action="store_true", help="allow filesystem experiments on --fs-device")
    s.set_defaults(fn=cmd_init)

    sp.add_parser("facts", help="hardware and server facts").set_defaults(fn=cmd_facts)
    sp.add_parser("status", help="agent, session and run status").set_defaults(fn=cmd_status)
    sp.add_parser("guide", help="print the tuning workflow").set_defaults(fn=cmd_guide)
    sp.add_parser("config", help="show managed settings").set_defaults(fn=cmd_config)
    sp.add_parser("daemon", help="run the metrics collector (systemd service)").set_defaults(fn=cmd_daemon)

    s = sp.add_parser("baseline", help="generate (and optionally apply) a hardware/version based baseline")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--max-connections", type=int)
    s.add_argument("--reserve-mem", help="memory to leave for OS and co-located processes, e.g. 1G")
    s.set_defaults(fn=cmd_baseline)

    s = sp.add_parser("session", help="tuning sessions")
    s.add_argument("action", choices=["start", "stop", "show"])
    s.add_argument("--goal", choices=["tps", "latency"], default="tps")
    s.add_argument("--policy", choices=["safe", "benchmark"], default="safe")
    s.add_argument("--name")
    s.add_argument("--workload", help="description of the workload/benchmark")
    s.add_argument("--notes")
    s.add_argument("--noise-pct", type=float, default=None, help="fixed noise threshold (default: 2%% until calibrated)")
    s.add_argument("--no-os-tuning", action="store_true", help="do not tune operating-system settings in this session")
    s.add_argument("--scope", help="what may be tuned: knobs,os (default); add 'filesystem' for filesystem experiments")
    s.add_argument("--allow-stale-agent", action="store_true", help="kept for older scripts: a build mismatch is only a warning now")
    s.add_argument("--study", help="study (name or id) this session belongs to; created if it does not exist. Studies compare "
                                   "sessions across environments (hardware scaling, minimum hardware for a target)")
    s.add_argument("--ai", choices=["off"], default=None,
                   help="off: this session asks no AI model anything (rules + learnings only), whatever the account's AI mode")
    s.add_argument("--stability", choices=["first", "off"], default=None,
                   help="first (default): while the undo history grows without bound, stability remedies run before tuning and the best "
                        "configuration is a stable one; off: rank by the goal only")
    s.add_argument("--planner", action="store_true", help="an AI planner chooses each next step (the rules still check it); "
                                                         "this session only, whatever the account's AI mode")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_session)

    s = sp.add_parser("profile", help="CPU profile of the database for a few seconds (perf sampling, low overhead)")
    s.add_argument("--seconds", type=int, default=20)
    s.add_argument("--hz", type=int, default=49, help="samples per second per CPU")
    s.add_argument("--folded", help="also write folded stacks (for flamegraph.pl / speedscope) to this file")
    s.add_argument("--json-folded", action="store_true", help="include folded stacks in --json output")
    s.set_defaults(fn=cmd_profile)

    s = sp.add_parser("activity", help="report the current activity of this host to the server (shown in the UI)")
    s.add_argument("phase")
    s.add_argument("detail", nargs="?")
    s.add_argument("--seconds", type=float, help="expected duration")
    s.set_defaults(fn=cmd_activity)

    s = sp.add_parser("llm-context", help="print the context the LLM assistant would receive now")
    s.set_defaults(fn=cmd_llm_context)

    s = sp.add_parser("diag", help="diagnostic instrumentation (runtime only)")
    s.add_argument("what", choices=["waits"])
    s.add_argument("state", choices=["on", "off", "status"], help="waits on = also time mutex/rwlock/condition waits (costs a few %%)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_diag)

    s = sp.add_parser("study", help="studies: compare tuning sessions across environments")
    s.add_argument("action", choices=["create", "list", "show"])
    s.add_argument("name", nargs="?", help="study name (create/show) or id (show)")
    s.add_argument("--kind", choices=["compare", "scaling", "min-hardware"], default="compare")
    s.add_argument("--goal", choices=["tps", "latency"], default="tps")
    s.add_argument("--target", type=float, help="min-hardware: goal value to reach (e.g. 50000 NOPM, or 5 ms for latency)")
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_study)

    s = sp.add_parser("measure", help="measurement windows (runs)")
    s.add_argument("action", choices=["start", "stop", "abort", "run"])
    s.add_argument("--label")
    s.add_argument("--notes")
    s.add_argument("--external", action="append", help="KEY=VALUE reported by the benchmark (repeatable)")
    s.add_argument("--goal-value", type=float)
    s.add_argument("--duration", type=int, default=300)
    s.add_argument("--bench", help="benchmark profile (INI): the agent resets, runs and parses the benchmark itself")
    s.add_argument("--warmup", help="seconds, or 'auto' (buffer pool loaded + stable throughput). "
                                     "Default: auto if mysqld restarted in the last 10 min, else none")
    s.set_defaults(fn=cmd_measure)

    s = sp.add_parser("run-length", help="find the warm-up and measurement length the benchmark needs (one long run)")
    s.add_argument("--bench", required=True, help="benchmark profile")
    s.add_argument("--max", type=int, default=None, help="longest probe in seconds (default: the profile's run_length_max, 3600)")
    s.add_argument("--target", type=float, default=None, help="noise threshold to aim for, %% (default: the tuner's target)")
    s.add_argument("--apply", action="store_true", help="write the recommended warm-up into the profile")
    s.set_defaults(fn=cmd_run_length)
    s = sp.add_parser("purge-triage", help="find an online purge setting that keeps the undo history bounded (one benchmark run)")
    s.add_argument("--bench", required=True)
    s.add_argument("--lead", type=int, default=600, help="seconds of benchmark before the first step (the history has to grow)")
    s.add_argument("--all", action="store_true", help="measure every remedy (no early stop at a cheap stable one): a full comparison")
    s.set_defaults(fn=cmd_purge_triage)
    s = sp.add_parser("calibrate", help="measure the current configuration N times to calibrate the noise threshold")
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--duration", type=int, default=300)
    s.add_argument("--warmup", default=0, help="warm-up before the first run: seconds or 'auto'")
    s.add_argument("--warmup-each", action="store_true", help="warm up before every run")
    s.add_argument("--label")
    s.add_argument("--notes")
    s.add_argument("--from-runs", help="calibrate from existing runs of one configuration, e.g. 2,5,7")
    s.add_argument("--bench", help="benchmark profile to run for each calibration run")
    s.set_defaults(fn=cmd_calibrate)

    s = sp.add_parser("experiment", help="human-specified experiments: try settings or sweep one variable")
    s.add_argument("action", choices=["add", "list", "run"])
    s.add_argument("--sweep", help="var=v1,v2,v3 (e.g. innodb_buffer_pool_size=1G,2G,3G)")
    s.add_argument("--try", dest="try_", action="append", help="var=value (repeatable): one combined configuration")
    s.add_argument("--repeats", type=int, default=1)
    s.add_argument("--reason")
    s.add_argument("--force", action="store_true", help="skip the memory-budget guard")
    s.add_argument("--bench", help="benchmark profile used to measure (default: the session's autotune profile)")
    s.set_defaults(fn=cmd_experiment)

    s = sp.add_parser("autotune", help="unattended tuning loop driven by a benchmark profile")
    s.add_argument("--bench", required=True)
    s.add_argument("--calibrate", type=int, default=3, help="calibration runs if the session is not calibrated yet")
    s.add_argument("--max-runs", type=int, default=10, help="maximum experiment runs (calibration not counted)")
    s.add_argument("--tradeoffs", action="store_true", help="after safe experiments, continue with trade-offs (policy=benchmark)")
    s.add_argument("--no-confirm", dest="confirm", action="store_false", help="skip the confirmation run of the best config")
    s.add_argument("--close", action="store_true", help="close the session when autotune ends (otherwise it stays open for follow-up experiments)")
    s.add_argument("--allow-unstable", action="store_true",
                   help="tune even when the purge triage finds no configuration with a bounded undo history (results drift)")
    s.add_argument("--recheck-every", type=int, default=3,
                   help="re-measure the current configuration before an experiment when it was last measured this many runs ago (0 = off)")
    s.set_defaults(fn=cmd_autotune)

    sp.add_parser("reevaluate", help="recompute verdicts of all runs (e.g. after calibration)").set_defaults(fn=cmd_reevaluate)
    s = sp.add_parser("recommend", help="analyse the last run and recommend changes")
    s.add_argument("--llm", action="store_true", help="also ask the configured LLM (OpenRouter, set up in the web app) for knob ideas")
    s.set_defaults(fn=cmd_recommend)

    s = sp.add_parser("apply", help="apply recommendations or explicit settings")
    s.add_argument("--rec", action="append", help="recommendation id (repeatable)")
    s.add_argument("--next", action="store_true", help="apply the suggested next experiment")
    s.add_argument("--set", action="append", help="var=value (repeatable)")
    s.add_argument("--unset", action="append", help="stop managing var (back to server default/other files)")
    s.add_argument("--reason")
    s.add_argument("--no-restart", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_apply)

    s = sp.add_parser("rollback", help="return to an earlier configuration")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--to-run", type=int)
    g.add_argument("--best", action="store_true", help="best configuration (may include trade-offs)")
    g.add_argument("--best-safe", action="store_true", help="best configuration without trade-offs")
    g.add_argument("--reset", action="store_true", help="remove all managed settings")
    s.set_defaults(fn=cmd_rollback)

    a = p.parse_args(argv)
    JSON = a.json
    try:
        a.fn(a)
    except Fail as e:
        if JSON:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except APIError as e:
        if JSON:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"server error: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
