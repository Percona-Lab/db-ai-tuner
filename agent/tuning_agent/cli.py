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
import sys
import time
import uuid

from . import __version__, baseline, bench, clientrole, collector, engine, evaluate, hostinfo, llmassist, mysqldb, ostune
from .knowledge import tradeoffs_in
from .applier import Applier
from .client import APIError, Client
from .knowledge import human_size, normalize
from .state import SampleStore, State

CONFIG_PATH = os.environ.get("TUNING_AGENT_CONFIG", "/etc/tuning-agent/agent.ini")
JSON = False


class Fail(Exception):
    pass


class TooSlow(Fail):
    pass


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
        return {
            "hardware": self.hw(),
            "db": {"flavor": flavor, "version": ver, "version_comment": comment, "version_tuple": vt,
                   "datadir": self.datadir(), "data_bytes": data_bytes, "tables": tables,
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


def _apply_and_record(c, changes, source, reason_default="", restart=None):
    allow = c.cfg.get("allow_restart", "true").lower() == "true" if restart is None else restart
    for ch in changes:
        ch.setdefault("source", source)
        ch.setdefault("reason", reason_default)
    client_changes = [ch for ch in changes if ch["variable"].startswith("client:")]
    changes = [ch for ch in changes if not ch["variable"].startswith("client:")]
    ap = Applier(c.cfg, c.state, c.db, log=log)
    res = ap.apply(changes, allow_restart=allow) if changes else {"applied": [], "restarted": False, "needs_restart": [],
                                                                  "mismatches": [], "errors": [], "cnf_path": ap.cnf_path}
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
    c.event("apply", f"{source}: applied {len(res['applied'])} change(s)" + (" with restart" if res["restarted"] else ""),
            {"result": res, "requested": changes})
    return res


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


def cmd_baseline(a):
    c = Ctx()
    v = c.variables
    flavor, vt, ver, _ = mysqldb.flavor_of(v)
    hw = c.hw()
    opts = {"max_connections": a.max_connections}
    if a.reserve_mem:
        from .knowledge import parse_size
        opts["reserve_bytes"] = parse_size(a.reserve_mem)
    items, calc = baseline.generate(hw, v, flavor, vt, opts)
    for it in items:
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
                                        "noise_pct": a.noise_pct, "notes": a.notes, "workload": a.workload, "os_tuning": os_tuning,
                                        "study": a.study})
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
                "noise_pct": a.noise_pct, "started": time.time(), "runs": [], "best": None, "os_tuning": os_tuning}
        if r.get("study"):
            sess["study"] = r["study"]
        c.state.set("session", sess)
        emit({"ok": True, "session": sess, "url": r.get("url"), "study": r.get("study")},
             f"Session #{sess['id']} started (goal={a.goal}, policy={a.policy}). {r.get('url', '')}\n"
             + (f"Part of study '{r['study']['name']}': {r['study']['url']}\n" if r.get("study") else "") +
             "Next: start your workload, then `tuning-agent measure start` / `measure stop` (or `measure run --duration 300`).")
    elif a.action == "stop":
        sess = c.session()
        if c.state.get("measurement"):
            raise Fail("a measurement is in progress; stop it first")
        _remember_noise(c, sess)
        c.client.post(f"/sessions/{sess['id']}/close", {"summary": {"best": sess.get("best"), "runs": len(sess.get("runs", []))}})
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
    seq = len(sess["runs"]) + 1
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
    summary, inputs = collector.analyze_window(a_snap, b_snap, samples)
    try:  # who holds swapped-out memory at the end of the window
        top = hostinfo.swap_top()
        if top:
            inputs["swap_top"] = [{"process": n, "pid": p, "swap_mb": round(b / 2 ** 20, 1)} for n, p, b in top]
    except Exception:
        pass
    if extra_inputs:
        inputs.update(extra_inputs)
        for k, v in (extra_inputs.get("probe") or {}).items():
            summary["probe_" + k] = v
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
    runs = sess["runs"]
    prev = runs[-1] if runs else None
    first = runs[0] if runs else None
    d_prev = _improvement(sess["goal"], gv, prev.get("goal_value")) if prev else None
    d_first = _improvement(sess["goal"], gv, first.get("goal_value")) if first else None
    _refresh_noise(c, sess, runs)
    verdict, evaluation = evaluate.verdict({"managed": m["managed"], "goal_value": gv, "summary": summary, "changes": m.get("changes")}, runs, sess["goal"],
        sess.get("noise") or ({"noise_pct": sess["noise_pct"]} if sess.get("noise_pct") else None))
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
    for key, sel in (("best", runs), ("best_safe", [r for r in runs if not tradeoffs_in(r.get("managed"))])):
        b = engine.best_config(sel, sess["goal"], noise)
        if b:
            sess[key] = {"seq": b["seqs"][-1], "runs": b["seqs"], "label": b["label"], "goal_value": round(b["mean"], 4),
                         "mean_of_runs": b["n"], "tradeoffs": tradeoffs_in(b["managed"])}


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


def _maybe_profile(c, sess, prof, warmup_s):
    """CPU profile for a slice of this run when the workload is CPU-bound and this configuration was not profiled yet.
    agent.ini: cpu_profiling = auto (default) | always | off, profile_hz (49), profile_seconds (20)."""
    from . import cpuprof
    mode = str(c.cfg.get("cpu_profiling", "auto")).lower()
    if mode == "off" or warmup_s < 30:  # needs room for the profile before the measured window starts
        return None
    key = evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()})
    done = sess.get("profiled_configs") or []
    if mode == "auto":
        runs = [x for x in sess.get("runs", []) if (x.get("summary") or {}).get("cpu_busy") is not None]
        if not runs or runs[-1]["summary"]["cpu_busy"] < 60 or repr(key) in done:
            return None
    if not cpuprof.ensure_perf(log):
        log("CPU profiling skipped: perf is not available")
        return None
    secs = min(int(c.cfg.get("profile_seconds", 20)), warmup_s - 10)
    p = cpuprof.Profiler(c.state.dir, secs, int(sess.get("profile_hz") or c.cfg.get("profile_hz", 49)))
    p.start(delay=max(0, warmup_s - secs - 5))  # ends 5 s before the measurement starts
    sess.setdefault("profiled_configs", []).append(repr(key))
    c.state.set("session", sess)
    log(f"   CPU profile: {secs}s at {p.hz} Hz at the end of the warm-up (outside the measured window)")
    return p


def _finish_profile(c, p, t_meas0):
    from . import cpuprof
    p.join(timeout=p.seconds + 120)
    try:
        pids = set(hostinfo.service_pids(hostinfo.mysqld_pid() or 0) or [])
        res = p.process(pids)
    except Exception as e:  # never fail a run because of the profiler
        return {"error": str(e)[:300]}
    res["in_measured_window"] = False  # taken at the end of the warm-up
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
    return out


def _bench_run(c, prof, label=None, notes=None, warmup_override=None):
    """reset -> start benchmark -> warm-up -> measure -> wait for exit -> parse metrics -> finish run."""
    sess = c.session()
    logdir = os.path.join(c.state.dir, "bench")
    os.makedirs(logdir, exist_ok=True)
    seq = len(sess["runs"]) + 1
    logfile = os.path.join(logdir, f"s{sess['id']}-run{seq}.log")
    if prof.reset:
        log(f"reset: {prof.reset}")
        c.activity("resetting data", prof.reset.split()[0].rsplit("/", 1)[-1])
        c.db.close()
        rc, out = bench.run_shell(prof.reset, timeout=3600, log=logfile + ".reset")
        if rc != 0:
            raise Fail(f"reset command failed ({rc}): {out[-500:]}")
        c.db.connect(retries=30, delay=2)
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
    if sess.get("run_seconds") and prof.can_resize() and prof.duration != int(sess["run_seconds"]):
        # run length chosen at calibration (noise plan)
        prof.timeout += int(sess["run_seconds"]) - prof.duration
        prof.duration = int(sess["run_seconds"])
    r = bench.Running(prof.command(), logfile)
    profiler = None
    try:
        warm = warmup_override if warmup_override is not None else prof.warmup
        if str(warm) == "auto":
            waited = _warmup(c, "auto")
        else:
            waited = int(warm or 0)
            # CPU profile during the end of the warm-up: the load is already on, and the measured window stays untouched
            profiler = _maybe_profile(c, sess, prof, waited)
            if waited:
                log(f"warm-up {waited}s ...")
                c.activity("warm-up", label, waited)
                deadline = time.time() + waited
                while time.time() < deadline and r.alive():
                    time.sleep(1)
        if not r.alive():
            raise Fail(f"benchmark exited during warm-up (code {r.p.returncode}); tail:\n{r.output()[-1500:]}")
        m, warn, url = _measure_start(c, label, notes, warmed=True)
        t_meas0 = time.time()
        if prof.duration > 0:
            log(f"measuring run {m['seq']} for {prof.duration}s ...")
            c.activity("measuring", f"run #{m['seq']} {label or ''}", prof.duration)
            deadline = time.time() + prof.duration
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
        b_snap = _end_snapshot(c)
        rc = r.wait(max(10, prof.timeout - (time.time() - r.t0)))
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
    extra = {"bench": {**prof.describe(), "requirements": requirements, "exit_code": rc, "metrics": metrics, "log_tail": text[-4000:]}, "probe": env_probe}
    if profiler:
        extra["cpu_profile"] = _finish_profile(c, profiler, t_meas0)
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
    """Long-term goal CV measured by earlier sessions of the same benchmark on this environment (median of the last 5)."""
    hist = (c.state.get("noise_history") or {}).get(_noise_key(sess)) or []
    vals = sorted(h["cv"] for h in hist[-5:])
    return vals[len(vals) // 2] if vals else None


def _refresh_noise(c, sess, runs):
    """Re-estimate the goal noise threshold from all repeats so far (variance is environment and workload specific)."""
    if not sess.get("noise"):
        return
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


def _remember_noise(c, sess):
    """At session end: keep this environment + benchmark's long-term variance as the prior for the next session."""
    est = (sess.get("noise") or {}).get("estimate") or {}
    if est.get("long_term_dof"):
        h = c.state.data.setdefault("noise_history", {}).setdefault(_noise_key(sess), [])
        h.append({"cv": est["pooled_cv_pct"], "dof": est["dof"], "session": sess.get("id"), "t": time.time()})
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


def _season(c, prof):
    """data_state = seasoned: run the benchmark repeatedly (unmeasured) until throughput stops trending - the data set
    has reached a steady state, so a series of tuning runs will not move it much - or until season_hours.
    Throughput per iteration = server-side transactions (Com_commit + Com_rollback) per second."""
    t0 = time.time()
    limit, need = prof.season_hours * 3600, prof.season_min_hours * 3600
    log(f"== seasoning the data set: benchmark iterations until throughput is steady (|trend| < {prof.season_slope}%/h "
        f"over the last hour, at least {prof.season_min_hours:g} h, at most {prof.season_hours:g} h)")
    pts = []  # (hours since start, tps)
    logdir = os.path.join(c.state.dir, "bench")
    os.makedirs(logdir, exist_ok=True)
    steady = False
    if prof.reset:
        log("   note: the profile also has a reset command; seasoning runs once, before any reset")

    def commits():
        st = c.db.status()
        return sum(float(st.get(k) or 0) for k in ("Com_commit", "Com_rollback")), time.time()

    while time.time() - t0 < limit:
        c.activity("seasoning", f"steady-state pre-run, {(time.time() - t0) / 3600:.1f} of max {prof.season_hours:g} h", None)
        a, ta = commits()
        r = bench.Running(prof.command(), os.path.join(logdir, "season.log"))
        while r.alive() and time.time() - t0 < limit:
            time.sleep(5)
        if r.alive():
            r.kill()
        b, tb = commits()
        tps = (b - a) / max(tb - ta, 1)
        pts.append(((tb - t0) / 3600, tps))
        log(f"   {(tb - t0) / 3600:5.2f} h: {tps:.1f} tps")
        last = [p for p in pts if p[0] >= pts[-1][0] - 1.0]
        if pts[-1][0] >= max(need, 1.0) and len(last) >= 4:
            slope = evaluate.trend_pct([p[1] for p in last], [p[0] for p in last])
            if slope is not None and abs(slope) < prof.season_slope:
                steady = True
                break
    slope = evaluate.trend_pct([p[1] for p in pts[-8:]], [p[0] for p in pts[-8:]]) if len(pts) >= 3 else None
    res = {"hours": round((time.time() - t0) / 3600, 2), "iterations": len(pts), "steady": steady,
           "trend_pct_per_hour": round(slope, 2) if slope is not None else None, "series": [[round(h, 3), round(v, 2)] for h, v in pts]}
    sess = c.session()
    sess["seasoning"] = res
    c.state.set("session", sess)
    txt = (f"Data set seasoned for {res['hours']} h ({len(pts)} benchmark iterations): "
           + ("throughput steady" if steady else "still trending at the time limit") + (f", trend {slope:+.2f}%/h" if slope is not None else ""))
    log("   " + txt)
    c.event("season", txt, res)
    return res


def _noise_plan(c, prof, runs, n):
    """After calibration: is the noise threshold small enough to see typical gains, and what would reduce it?
    Longer runs only when the noise is mostly within-run (they do not remove run-to-run shifts); then the earlier
    calibration runs are superseded (different run length) and the start configuration is calibrated again."""
    sess = c.session()
    cal = sess["noise"]
    ds = evaluate.data_state_check(runs, prof.data_state)
    cal["data_state"] = ds
    if ds["status"] == "drifting":
        log(f"   WARNING: {ds['text']}")
        c.event("data-state", ds["text"], ds)
    plan = evaluate.noise_plan(runs, cal, prof.duration, prof.max_duration)
    if ds["status"] == "drifting" and plan["action"] in ("between-runs", "longer-runs"):
        plan["text"] += " Likely cause: the data set is not restored between runs (set reset = ..., or data_state = seasoned)."
    cal["plan"] = plan
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


def _reevaluate(c, sess):
    noise = sess.get("noise") or ({"noise_pct": sess["noise_pct"]} if sess.get("noise_pct") else None)
    changed = []
    for i, r in enumerate(sess["runs"]):
        v, ev = evaluate.verdict(r, sess["runs"][:i], sess["goal"], noise)
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
    flavor, vt, ver, _ = mysqldb.flavor_of(c.variables)
    _ap = Applier(c.cfg, c.state, c.db)
    osf = ostune.facts(c.datadir(), hostinfo.device_for_path(c.datadir()), _ap.service, _ap.fs_device)
    experience = {}
    try:
        from .engine import classify_workload
        wl = classify_workload(last.get("summary"), last.get("inputs")).get("type") if last.get("summary") else None
        conns = (last.get("summary") or {}).get("threads_connected_avg")
        q = (f"/experience?cpus={os.cpu_count()}&exclude_session={sess['id']}" + (f"&workload={wl}" if wl else "")
             + (f"&conns={int(conns)}" if conns else ""))
        ex = c.client.get(q)
        experience = ex.get("experience", {})
        for rid, e in (ex.get("experience_all") or {}).items():  # thin same-class evidence: fall back to all sessions
            if e.get("n", 0) >= 3 and (experience.get(rid) or {}).get("n", 0) < 2:
                experience[rid] = dict(e, scope="other workloads / machine sizes / concurrency")
    except Exception as e:  # experience is a hint; never block a recommendation on it
        log(f"note: no experience data ({e})")
    b = (last.get("inputs") or {}).get("bench") or {}
    bench_info = {k: b.get(k) for k in ("name", "run", "warmup", "duration", "goal_metric", "requires") if b.get(k) is not None}
    return {"bench": bench_info, "diagnosis_basis_run": basis, "goal": sess["goal"], "policy": sess["policy"], "hw": c.hw(), "flavor": flavor, "version": vt,
            "os_tuning": c.os_tuning_allowed() and sess.get("os_tuning", True), "os_facts": osf, "experience": experience,
            "failed": sess.get("failed_recs") or [],
            "scope": sess.get("scope") or ["knobs", "os"], "fs_device": _ap.fs_device,
            "variables": c.all_settings(), "summary": last.get("summary"), "inputs": last.get("inputs"),
            "history": sess["runs"], "noise_pct": sess.get("noise_pct"), "noise": sess.get("noise"),
            "managed": {k: v["value"] for k, v in c.state.get("managed", {}).items()}}


def _recommend(c, sess, llm=False):
    c.refresh()
    ctx = _engine_ctx(c, sess)
    res = engine.run_engine(ctx)
    why = llmassist.trigger(res, explicit=llm)
    pending = sess.get("llm_pending") or []  # accepted earlier, not measured yet: they stay candidates
    fresh, model = [], None
    if why and ctx.get("summary"):
        try:
            meta = c.db.variables_meta()
        except Exception:
            meta = {}
        try:
            r = c.client.post("/llm/suggest", {"session_id": sess["id"], "trigger": why, "force": bool(llm),
                                                "context": llmassist.context(ctx, res, meta)})
        except Exception as e:  # LLM help is optional
            r = {"enabled": False, "reason": str(e)}
        if r.get("enabled") and r.get("suggestions") is not None:
            fresh, model = [dict(sg, _model=r.get("model")) for sg in r.get("suggestions") or []], r.get("model")
            res["llm"] = {"model": model, "trigger": why, "cached": r.get("cached"), "notes": r.get("notes")}
        elif why != "routine" and r.get("reason"):
            res["llm"] = {"skipped": r.get("reason"), "trigger": why}
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
            extra += its
            keep.append(sg)
        elif sg in fresh:
            rejected += rj
    if res.get("llm") and "model" in res["llm"]:
        res["llm"].update(accepted=len([s for s in keep if s in fresh]), rejected=rejected, pending=len([s for s in keep if s not in fresh]))
    sess["llm_pending"] = keep
    if extra:
        for it in extra:
            it["score"] = engine.score(it, ctx.get("goal", "tps")) + (2 if why not in (None, "routine") else 0)
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
    res.pop("_ctx", None)
    run_id = sess["runs"][-1]["id"] if sess["runs"] else None
    r = c.client.post("/recommendations", {"session_id": sess["id"], "run_id": run_id, "goal": sess["goal"], "policy": sess["policy"],
                                           "items": res["items"], "diagnosis": res["diagnosis"], "next": res["next"],
                                           "workload": res.get("workload"), "phase": res.get("phase"), "llm": res.get("llm")})
    sess["last_recommendation"] = {"id": r.get("recommendation_id"), "items": res["items"], "next": res["next"]}
    c.state.set("session", sess)
    return res


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
    for arm in arms:
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
        for i in range(int(exp.get("repeats") or 1)):
            run, _ = _bench_run(c, prof, f"exp#{eid} {label(arm)}" + (f" ({i + 1}/{exp['repeats']})" if (exp.get("repeats") or 1) > 1 else ""))
            if run.get("goal_value") is not None:
                vals.append(run["goal_value"])
        mean = sum(vals) / len(vals) if vals else None
        d = _improvement(goal, mean, pm["goal"]) if (pm and mean is not None) else None
        results.append({"arm": label(arm), "status": "measured", "runs": len(vals), "goal_mean": round(mean, 4) if mean is not None else None,
                        "delta_pct": d, "verdict": None if d is None else ("better" if d > noise else "worse" if d < -noise else "within noise")})
        log(f"   {label(arm)}: {mean} ({'' if d is None else f'{d:+.1f}%'})")
    measured = [r for r in results if r["status"] == "measured" and r.get("delta_pct") is not None]
    best = max(measured, key=lambda r: r["delta_pct"], default=None)
    keep = best if best and best["delta_pct"] > noise else None
    if keep:
        arm = next(a for a in arms if label(a) == keep["arm"])
        target = dict(parent)
        for x in arm:
            target[x["variable"]] = x["value"]
        _goto_managed(c, target, f"experiment #{eid}: best value, {keep['delta_pct']:+.1f}% vs parent", f"exp:{eid}")
        msg = f"kept {keep['arm']} ({keep['delta_pct']:+.1f}% vs parent mean {pm['goal']:.4g}, noise ±{noise}%)"
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
        emit({"ok": True, "ran": done}, "\n".join(f"#{d['id']}: {d['result']}" for d in done) or "No queued experiments.")


def cmd_autotune(a):
    """Unattended loop: calibrate (if needed) -> recommend -> apply next -> measure (bench) ... -> best config confirmation."""
    c = Ctx()
    sess = c.session()
    prof = bench.Profile(a.bench)
    if sess.get("bench_name") != prof.name:  # noise history is kept per environment + benchmark
        sess["bench_name"] = prof.name
        c.state.set("session", sess)
    if a.tradeoffs and sess["policy"] != "benchmark":
        raise Fail("--tradeoffs needs a session with --policy benchmark")
    if "filesystem" in (sess.get("scope") or []) and c.all_settings().get("os:datadir_fs") == "rootfs":
        log("== scope includes filesystem: moving the data directory to ext4 on the experiment device first (fair baseline)")
        _apply_and_record(c, [{"variable": "os:datadir_fs", "value": "ext4", "reason": "filesystem scope: common baseline device",
                               "source": "fs-baseline"}], "autotune")
    if prof.data_state == "seasoned" and not sess.get("seasoning") and prof.season_hours > 0:
        _season(c, prof)
    if not sess.get("noise"):
        log(f"== calibration: {a.calibrate} runs of the current configuration")
        c.activity("calibrating", f"{a.calibrate} runs of the starting configuration to measure noise")
        runs = []
        extra = 0
        while len([r for r in runs if r.get("verdict") not in evaluate.EXCLUDED]) < a.calibrate:
            run, _ = _bench_run(c, prof, f"baseline ({len(runs) + 1}/{a.calibrate + extra})")
            log(f"   run {run['seq']}: goal {run['goal_value']}" + (" (unreliable: CPU steal, measured again)" if run.get("verdict") == "unreliable" else ""))
            runs.append(run)
            if run.get("verdict") == "unreliable":
                extra += 1
                if extra > a.calibrate:  # the host is too noisy right now: calibrate with what we have
                    log("   WARNING: persistent CPU steal; calibrating with the runs available")
                    break
        runs = [r for r in runs if r.get("verdict") not in evaluate.EXCLUDED] or runs
        sess = c.session()
        cal = evaluate.calibrate(runs)
        _set_calibration(c, sess, cal)
        _reevaluate(c, c.session())
        log(_fmt_cal(cal))
        runs = _noise_plan(c, prof, runs, a.calibrate)

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
            log("   " + _run_experiment(c, prof, e))
            measured += len(e["spec"].get("values") or [1]) * int(e.get("repeats") or 1)
            continue
        c.activity("choosing next experiment", "diagnosis and recommendation")
        res = _recommend(c, sess)
        nxt = res.get("next")
        if nxt in (sess.get("failed_recs") or []):
            alt = next((i for i in res["items"] if i["kind"] in ("config", "revert") and i.get("confidence", 0) > 0
                        and i["id"] not in (sess.get("failed_recs") or []) and (i.get("safety") == "safe" or a.tradeoffs)), None)
            if not alt:
                log(f"== {nxt} failed before and nothing else is left; stopping")
                break
            log(f"== {nxt} failed before; next candidate: {alt['id']}")
            nxt = alt["id"]
        item = next((i for i in res["items"] if i["id"] == nxt), None)
        # benchmark policy: keep the last 30% of the budget for trade-offs, otherwise safe experiments use it all up
        if a.tradeoffs and measured >= int(a.max_runs * 0.7) and (not item or item.get("safety") == "safe" and item["kind"] != "revert"):
            tried = {r["label"].split(" ")[0] for r in sess["runs"]}
            trade = next((i for i in res["items"] if i.get("safety") == "trade-off" and i["kind"] == "config" and i.get("confidence", 0) > 0
                          and i["id"] not in tried and i["id"] not in (sess.get("failed_recs") or [])), None)
            if trade:
                log(f"== trade-off budget: {measured} of {a.max_runs} runs used, switching to trade-off experiments")
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
                log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}"
                    + (" (DRIFT)" if (run.get('evaluation') or {}).get('drift') else ""))
                continue
        if item.get("sweep"):  # measure several values as an experiment instead of a single step
            body = {"kind": "sweep", "variable": item["sweep"]["variable"], "values": ",".join(item["sweep"]["values"]),
                    "reason": item["rationale"][:400], "created_by": ("llm:" if item.get("source") == "llm" else "agent:") + nxt}
            eid = c.client.post(f"/sessions/{sess['id']}/experiments", body)["id"]
            exp = next((e for e in _queued_experiments(c, sess) if e["id"] == eid), None)
            if exp:
                log(f"== sweep {nxt}: {item['sweep']['variable']} = {', '.join(item['sweep']['values'])} (experiment #{eid})")
                log("   " + _run_experiment(c, prof, exp))
                measured += len(item["sweep"]["values"])
                # an improved sweep point is a single run like any other improvement: confirm it before building on it
                sess = c.session()
                pts = [r for r in sess["runs"] if str(r.get("label") or "").startswith(f"exp#{eid} ") and r.get("verdict") == "improved"]
                if pts and measured < a.max_runs:
                    lower = sess["goal"] == "latency"
                    bp = (min if lower else max)(pts, key=lambda r: r["goal_value"])
                    cur = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
                    want = bp.get("managed") or {}
                    ch = [{"variable": k, "value": want.get(k), "reason": f"autotune: confirm sweep point run {bp['seq']}",
                           "source": f"exp:{eid}"} for k in set(cur) | set(want) if normalize(k, cur.get(k)) != normalize(k, want.get(k))]
                    if ch:
                        _apply_and_record(c, ch, "autotune")
                    log(f"   sweep point run {bp['seq']} improved: measuring it again to confirm")
                    run, _ = _bench_run(c, prof, f"exp#{eid} {item['sweep']['variable']}={want.get(item['sweep']['variable'])} (confirm)")
                    measured += 1
                    _confirm(c)
            sess = c.session()
            sess.setdefault("failed_recs", []).append(nxt)  # one sweep per rule and session
            c.state.set("session", sess)
            continue
        log(f"== experiment: {nxt} ({item['title']}) [{item.get('safety')}]")
        c.activity("applying changes", f"{nxt}: {item['title']}" + (" (restart)" if item.get("restart") else ""))
        changes = [{"variable": ch["variable"], "value": None if ch.get("unset") else ch["proposed"], "reason": item["rationale"],
                    "source": f"rec:{nxt}"} for ch in item["changes"]]
        before_exp = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
        resa = _apply_and_record(c, changes, "autotune")
        if resa.get("rolled_back"):
            log(f"   apply failed and was rolled back: {resa.get('errors')}")
            sess = c.session()
            sess.setdefault("failed_recs", []).append(nxt)
            c.state.set("session", sess)
            continue
        sess = c.session()
        if sess.get("last_recommendation"):
            sess["last_recommendation"]["applied"] = True
            c.state.set("session", sess)
        if _known_config(c, sess):
            log("   configuration already measured: no new run needed")
            if item["kind"] != "revert":  # an experiment that only leads back to a measured configuration is done
                sess = c.session()
                sess.setdefault("failed_recs", []).append(nxt)
                c.state.set("session", sess)
            continue
        try:
            run, _ = _bench_run(c, prof, f"{nxt}")
        except TooSlow as e:
            log(f"   {e}; reverting {nxt}")
            cur = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
            undo = [{"variable": k, "value": before_exp.get(k), "reason": f"autotune: {nxt} made the benchmark too slow", "source": "rollback"}
                    for k in set(cur) | set(before_exp) if normalize(k, cur.get(k)) != normalize(k, before_exp.get(k))]
            _apply_and_record(c, undo, "rollback")
            sess = c.session()
            sess.setdefault("failed_recs", []).append(nxt)
            c.state.set("session", sess)
            measured += 1
            continue
        measured += 1
        log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
        if run["verdict"] == "improved" and measured < a.max_runs:
            # a single run above the threshold can be luck (winner's curse): measure it again before building on it
            log("   improvement: measuring the same configuration again to confirm it")
            run, _ = _bench_run(c, prof, f"{nxt} (confirm)")
            measured += 1
            log(f"   run {run['seq']}: goal {run['goal_value']} -> {run['verdict']}")
            _confirm(c)  # the improvement must hold for the mean of both runs
        if run["verdict"] == "unreliable":
            log("   hypervisor steal made the run unreliable: measuring the same configuration again")
            run, _ = _bench_run(c, prof, f"{nxt} (re-measure: CPU steal)")
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
    if best_safe:
        goto(best_safe, "best production-safe configuration")
        if a.confirm:
            _bench_run(c, prof, "best safe config, confirmation")
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
    res = _apply_and_record(c, changes, "rollback")
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
    last_prune = last_rg = 0
    rg_err = None
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
            if "rg:innodb_bg_cpus" in (st.get("managed") or {}) and time.time() - last_rg > 60:
                last_rg = time.time()
                from . import resgroups
                try:
                    if resgroups.ensure(c.db):  # mysqld was restarted outside the agent (e.g. a benchmark reset)
                        log("re-assigned InnoDB background threads to their resource group")
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
        if time.time() - last_prune > 3600:
            store.prune()
            last_prune = time.time()
        while not stop["flag"] and time.time() - t0 < interval:
            time.sleep(0.5)
    try:  # stopped on purpose: tell the server, so the UI shows "agent stopped" rather than "offline"
        c.client.timeout = 5
        c.client._call("POST", "/bye", {"environment_id": c.env_id()})  # one attempt: never delay the service stop
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
  * Re-measure the best and the baseline at the end: differences under the noise level (--noise-pct) are not real.
  * policy=safe never applies durability trade-offs (they are shown as advice); policy=benchmark may.
  * Pass --external key=value on measure stop to record benchmark-reported numbers (e.g. tps=, nopm=, latency=);
    a key equal to the goal (tps / latency) becomes the goal value.
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
    s.add_argument("--study", help="study (name or id) this session belongs to; created if it does not exist. Studies compare "
                                   "sessions across environments (hardware scaling, minimum hardware for a target)")
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
