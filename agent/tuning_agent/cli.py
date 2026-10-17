"""tuning-agent command line interface.

Designed to be driven by people and by coding agents (Claude Code): every command is
non-interactive, supports --json, and exits non-zero on failure.
"""
import argparse
import configparser
import json
import os
import signal
import sys
import time
import uuid

from . import __version__, baseline, collector, engine, hostinfo, mysqldb
from .applier import Applier
from .client import APIError, Client
from .knowledge import human_size, normalize
from .state import SampleStore, State

CONFIG_PATH = os.environ.get("TUNING_AGENT_CONFIG", "/etc/tuning-agent/agent.ini")
JSON = False


class Fail(Exception):
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

    @property
    def variables(self):
        if self._vars is None:
            self._vars = self.db.variables()
        return self._vars

    def refresh(self):
        self._vars = None

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
            "agent": {"version": __version__, "cnf_path": Applier(self.cfg, self.state, self.db).cnf_path,
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
        self.state.set("environment_id", r["environment_id"])
        return r, f

    def env_id(self):
        if not self.state.get("environment_id"):
            self.hello()
        return self.state["environment_id"]

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
    sec.setdefault("interval", "10")
    if "server" not in sec or "api_key" not in sec:
        raise Fail("--server and --api-key are required on first init")
    with open(CONFIG_PATH, "w") as f:
        cp.write(f)
    os.chmod(CONFIG_PATH, 0o600)
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


def _apply_and_record(c, changes, source, reason_default="", restart=None):
    allow = c.cfg.get("allow_restart", "true").lower() == "true" if restart is None else restart
    for ch in changes:
        ch.setdefault("source", source)
        ch.setdefault("reason", reason_default)
    ap = Applier(c.cfg, c.state, c.db, log=log)
    res = ap.apply(changes, allow_restart=allow)
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
        r = c.client.post("/sessions", {"environment_id": c.env_id(), "name": a.name, "goal": a.goal, "policy": a.policy,
                                        "noise_pct": a.noise_pct, "notes": a.notes, "workload": a.workload})
        sess = {"id": r["session_id"], "name": a.name or r.get("name"), "goal": a.goal, "policy": a.policy,
                "noise_pct": a.noise_pct, "started": time.time(), "runs": [], "best": None}
        c.state.set("session", sess)
        emit({"ok": True, "session": sess, "url": r.get("url")},
             f"Session #{sess['id']} started (goal={a.goal}, policy={a.policy}). {r.get('url', '')}\n"
             "Next: start your workload, then `tuning-agent measure start` / `measure stop` (or `measure run --duration 300`).")
    elif a.action == "stop":
        sess = c.session()
        if c.state.get("measurement"):
            raise Fail("a measurement is in progress; stop it first")
        c.client.post(f"/sessions/{sess['id']}/close", {"summary": {"best": sess.get("best"), "runs": len(sess.get("runs", []))}})
        hist = c.state.get("sessions_done") or []
        hist.append({k: sess.get(k) for k in ("id", "name", "goal", "best")})
        c.state.data["sessions_done"] = hist[-20:]
        c.state.data["session"] = None
        c.state.save()
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


def _measure_start(c, label=None, notes=None):
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
    seq = len(sess["runs"]) + 1
    pending = c.state.get("pending_changes") or []
    managed = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    label = label or ("initial" if seq == 1 and not managed else f"run-{seq}")
    r = c.client.post("/runs", {"session_id": sess["id"], "seq": seq, "label": label, "notes": notes,
                                "started": snap["t"], "config": snap["variables"], "managed": managed, "changes": pending})
    snap_path = os.path.join(c.state.dir, "measure_start.json")
    with open(snap_path, "w") as f:
        json.dump(snap, f, default=str)
    m = {"run_id": r["run_id"], "seq": seq, "label": label, "started": snap["t"], "changes": pending, "managed": managed}
    c.state.data["measurement"] = m
    c.state.data["pending_changes"] = []
    c.state.save()
    c.event("measure_start", f"measurement started: run {seq} '{label}'", {"run_id": r["run_id"]})
    return m, warn, r.get("url")


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


def _measure_stop(c, external=None, goal_value=None, notes=None):
    sess = c.session()
    m = c.state.get("measurement")
    if not m:
        raise Fail("no measurement in progress")
    c.refresh()
    with open(os.path.join(c.state.dir, "measure_start.json")) as f:
        a_snap = json.load(f)
    b_snap = collector.full_snapshot(c.db, hostinfo.device_for_path(c.datadir()))
    if a_snap.get("hist"):
        a_snap["hist"] = [tuple(x) for x in a_snap["hist"]]
    a_snap["stmt"] = tuple(a_snap["stmt"]) if a_snap.get("stmt") else None
    a_snap["waits"] = {k: tuple(v) for k, v in (a_snap.get("waits") or {}).items()}
    samples = SampleStore(c.state.dir).window(a_snap["t"], b_snap["t"])
    restarted = int(float(b_snap["status"].get("Uptime", 0))) < (b_snap["t"] - a_snap["t"])
    if restarted:
        raise Fail("the server restarted during the measurement window; counters are not comparable. "
                   "Run `tuning-agent measure abort` and measure again.")
    summary, inputs = collector.analyze_window(a_snap, b_snap, samples)
    external = external or {}
    gv, src = _goal_value(sess["goal"], summary, external, goal_value)
    summary["goal_value"] = gv
    summary["goal_source"] = src
    runs = sess["runs"]
    prev = runs[-1] if runs else None
    first = runs[0] if runs else None
    best = sess.get("best")
    d_prev = _improvement(sess["goal"], gv, prev.get("goal_value")) if prev else None
    d_first = _improvement(sess["goal"], gv, first.get("goal_value")) if first else None
    d_best = _improvement(sess["goal"], gv, best.get("goal_value")) if best else None
    noise = float(sess.get("noise_pct") or 2.0)
    if d_prev is None:
        verdict = "reference"
    elif d_prev > noise:
        verdict = "improved"
    elif d_prev < -noise:
        verdict = "regressed"
    else:
        verdict = "neutral"
    ctx = {"goal": sess["goal"], "policy": sess["policy"], "hw": c.hw(), "variables": b_snap["variables"],
           "summary": summary, "inputs": inputs}
    diagnosis = engine.diagnose(ctx)
    run = {"id": m["run_id"], "seq": m["seq"], "label": m["label"], "started": m["started"], "ended": b_snap["t"],
           "changes": m["changes"], "managed": m["managed"], "goal_value": gv, "delta_pct": d_prev, "delta_vs_first_pct": d_first,
           "delta_vs_best_pct": d_best, "verdict": verdict, "summary": summary}
    c.client.post(f"/runs/{m['run_id']}/finish", {
        "ended": b_snap["t"], "summary": summary, "external": external, "inputs": inputs, "diagnosis": diagnosis,
        "goal_value": gv, "goal_source": src, "delta_pct": d_prev, "delta_vs_first_pct": d_first, "verdict": verdict, "notes": notes})
    runs.append(run)
    scores = engine.config_scores(runs, sess["goal"])
    if scores:
        b = scores[0]
        sess["best"] = {"seq": b["seqs"][-1], "runs": b["seqs"], "label": b["label"], "goal_value": round(b["mean"], 4),
                        "mean_of_runs": b["n"]}
    sess["last_inputs_path"] = os.path.join(c.state.dir, f"run{run['seq']}_inputs.json")
    with open(sess["last_inputs_path"], "w") as f:
        json.dump({"summary": summary, "inputs": inputs, "variables": b_snap["variables"], "diagnosis": diagnosis}, f, default=str)
    sess["last_recommendation"] = None  # based on an older run
    c.state.data["session"] = sess
    c.state.data["measurement"] = None
    c.state.save()
    c.event("measure_stop", f"run {run['seq']} '{run['label']}' finished: goal={gv} ({verdict})", {"run_id": m["run_id"]})
    return run, diagnosis


def _fmt_run(run, diagnosis, goal):
    s = run["summary"]
    unit = "ms" if goal == "latency" else "/s"
    lines = [f"Run {run['seq']} '{run['label']}': goal {goal} = {run['goal_value']} {unit} ({s.get('goal_source')})  verdict: {run['verdict']}"]
    if run.get("delta_pct") is not None:
        lines.append(f"  vs previous {run['delta_pct']:+.2f}%   vs first {run['delta_vs_first_pct']:+.2f}%   (positive = better)")
    lines.append(f"  tps {s.get('tps')}  qps {s.get('qps')}  avg latency {s.get('avg_latency_ms')} ms  p95 {s.get('p95_latency_ms')} ms  "
                 f"cpu {s.get('cpu_busy')}% iowait {s.get('cpu_iowait')}%  bp hit {s.get('bp_hit_pct')}%  ckpt age max {s.get('checkpoint_age_pct_max')}%")
    for d in diagnosis:
        lines.append(f"  [{d['severity']}] {d['type']}: {d['text']}")
    return "\n".join(lines)


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
        c.client.post(f"/runs/{m['run_id']}/abort", {})
        c.state.data["pending_changes"] = m.get("changes", []) + (c.state.get("pending_changes") or [])
        c.state.data["measurement"] = None
        c.state.save()
        c.event("measure_abort", f"measurement of run {m['seq']} aborted", {})
        emit({"ok": True, "aborted": m["run_id"]}, f"Measurement of run {m['seq']} aborted.")
    elif a.action == "run":
        if a.warmup:
            log(f"warm-up {a.warmup}s ...")
            time.sleep(a.warmup)
        m, warn, url = _measure_start(c, a.label, a.notes)
        log(f"measuring run {m['seq']} for {a.duration}s ...")
        interrupted = False
        try:
            time.sleep(a.duration)
        except KeyboardInterrupt:
            interrupted = True
        run, diag = _measure_stop(c, _parse_kv(a.external), a.goal_value, a.notes)
        emit({"ok": True, "run": run, "diagnosis": diag, "interrupted": interrupted}, _fmt_run(run, diag, c.session()["goal"]))


def _engine_ctx(c, sess):
    path = sess.get("last_inputs_path")
    if not sess["runs"] or not path or not os.path.exists(path):
        last = {"summary": {}, "inputs": {}}
    else:
        with open(path) as f:
            last = json.load(f)
    flavor, vt, ver, _ = mysqldb.flavor_of(c.variables)
    return {"goal": sess["goal"], "policy": sess["policy"], "hw": c.hw(), "flavor": flavor, "version": vt,
            "variables": c.variables, "summary": last.get("summary"), "inputs": last.get("inputs"),
            "history": sess["runs"], "noise_pct": sess.get("noise_pct"),
            "managed": {k: v["value"] for k, v in c.state.get("managed", {}).items()}}


def cmd_recommend(a):
    c = Ctx()
    sess = c.session()
    if c.state.get("pending_changes"):
        log("note: there are applied changes that have not been measured yet")
    ctx = _engine_ctx(c, sess)
    res = engine.run_engine(ctx)
    run_id = sess["runs"][-1]["id"] if sess["runs"] else None
    r = c.client.post("/recommendations", {"session_id": sess["id"], "run_id": run_id, "goal": sess["goal"], "policy": sess["policy"],
                                           "items": res["items"], "diagnosis": res["diagnosis"], "next": res["next"]})
    sess["last_recommendation"] = {"id": r.get("recommendation_id"), "items": res["items"], "next": res["next"]}
    c.state.set("session", sess)
    lines = [f"Recommendations (goal={sess['goal']}, policy={sess['policy']}) based on run {sess['runs'][-1]['seq'] if sess['runs'] else '-'}:"]
    for d in res["diagnosis"]:
        lines.append(f"  diagnosis [{d['severity']}] {d['type']}: {d['text']}")
    for it in res["items"]:
        ch = ", ".join(f"{x['variable']}: {x['current']} -> {x['proposed']}" for x in it["changes"]) or "(no config change)"
        flag = "" if it["kind"] != "advice" else " [advice]"
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
    emit({"ok": not res.get("rolled_back") and not res.get("errors"), "result": res}, "Applied:\n" + _print_apply(res))
    if res.get("rolled_back"):
        raise Fail("apply failed and was rolled back")


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
        if a.best:
            b = (sess or {}).get("best")
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
    last_prune = 0
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
            s["session_id"] = (st.get("session") or {}).get("id")
            s["run_id"] = (st.get("measurement") or {}).get("run_id")
            store.add(s)
        except Exception as e:
            log(f"sample error: {e}")
            sampler = None
        try:
            if time.time() - last_hello > 600:
                c.refresh()
                c.hello()
                last_hello = time.time()
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


GUIDE = """tuning-agent workflow (for people and coding agents)

  1. tuning-agent facts                      # what hardware / server we tune
  2. tuning-agent baseline [--apply]         # hardware+version based starting config
  3. tuning-agent session start --goal tps|latency [--policy safe|benchmark] [--name NAME] [--noise-pct 2]
  4. start the workload (benchmark) and let it warm up
  5. tuning-agent measure start --label baseline ... tuning-agent measure stop [--external tps=1234]
     or: tuning-agent measure run --warmup 60 --duration 300 --label baseline
  6. tuning-agent recommend                  # ranked recommendations with evidence
  7. tuning-agent apply --next | --rec ID [--rec ID2] | --set var=value [--reason "why"]
  8. measure again (step 5); the run is compared with the previous one (verdict improved/neutral/regressed)
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
    s.add_argument("--noise-pct", type=float, default=2.0)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_session)

    s = sp.add_parser("measure", help="measurement windows (runs)")
    s.add_argument("action", choices=["start", "stop", "abort", "run"])
    s.add_argument("--label")
    s.add_argument("--notes")
    s.add_argument("--external", action="append", help="KEY=VALUE reported by the benchmark (repeatable)")
    s.add_argument("--goal-value", type=float)
    s.add_argument("--duration", type=int, default=300)
    s.add_argument("--warmup", type=int, default=0)
    s.set_defaults(fn=cmd_measure)

    sp.add_parser("recommend", help="analyse the last run and recommend changes").set_defaults(fn=cmd_recommend)

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
    g.add_argument("--best", action="store_true")
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
