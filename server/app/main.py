"""AI DB Tuner web service."""
import io
import json
import os
import re
import tarfile
import time

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import auth, llm, store

APP_DIR = os.path.dirname(__file__)
AGENT_DIR = os.environ.get("TUNER_AGENT_DIR", os.path.join(APP_DIR, "..", "..", "agent"))
PUBLIC_URL = os.environ.get("TUNER_PUBLIC_URL", "http://localhost:8000").rstrip("/")
ONLINE_S = 60

app = FastAPI(title="AI DB Tuner", docs_url=None, redoc_url=None)
store.init()

VOLATILE = {"gtid_executed", "gtid_purged", "timestamp", "pseudo_thread_id", "rand_seed1", "rand_seed2", "last_insert_id",
            "insert_id", "identity", "warning_count", "error_count", "gtid_owned", "innodb_buffer_pool_resize_status"}


def now():
    return time.time()


# ---------------------------------------------------------------------------- helpers

def own_env(env_id, user_id):
    e = store.q1("SELECT * FROM environments WHERE id=? AND user_id=?", (env_id, user_id))
    if not e:
        raise HTTPException(404, "environment not found")
    return dict(e)


def own_session(session_id, user_id):
    s = store.q1("SELECT s.* FROM tsessions s JOIN environments e ON e.id=s.env_id WHERE s.id=? AND e.user_id=?", (session_id, user_id))
    if not s:
        raise HTTPException(404, "session not found")
    return dict(s)


def own_run(run_id, user_id):
    r = store.q1("SELECT r.* FROM runs r JOIN tsessions s ON s.id=r.session_id JOIN environments e ON e.id=s.env_id "
                 "WHERE r.id=? AND e.user_id=?", (run_id, user_id))
    if not r:
        raise HTTPException(404, "run not found")
    return dict(r)


FLAVOR_NAMES = {"percona": "Percona Server", "mysql": "MySQL", "mariadb": "MariaDB", "pxc": "Percona XtraDB Cluster"}


def env_brief(e):
    f = store.uj(e["facts"], {}) or {}
    hw, db = f.get("hardware", {}), f.get("db", {})
    return {
        "id": e["id"], "name": e["name"], "hostname": e["hostname"], "agent_version": e["agent_version"],
        "created": e["created"], "last_seen": e["last_seen"], "online": bool(e["last_seen"] and now() - e["last_seen"] < ONLINE_S),
        "db": f"{db.get('version_comment', '')} {db.get('version', '')}".strip(), "flavor": db.get("flavor"),
        "db_short": f"{FLAVOR_NAMES.get(db.get('flavor'), db.get('flavor') or '')} {str(db.get('version', '')).split('-log')[0]}".strip(),
        "cpus": hw.get("cpus"), "ram_bytes": hw.get("ram_bytes"), "disk": "HDD" if hw.get("disk_rotational") else "SSD",
        "data_bytes": db.get("data_bytes"), "cost_month": e["cost_month"] if "cost_month" in e.keys() else None,
        **{k: (e[k] if k in e.keys() else None) for k in ("cost_hourly", "provisioned_at", "destroyed_at", "plan", "agent_state", "agent_state_at")},
        "cpu_model": hw.get("cpu_model"),
    }


RUN_LIST_FIELDS = ("id", "session_id", "seq", "label", "notes", "status", "started", "ended", "goal_value", "goal_source",
                   "delta_pct", "delta_vs_first_pct", "verdict", "abort_reason")


def run_brief(r):
    d = {k: r[k] for k in RUN_LIST_FIELDS}
    d["summary"] = store.uj(r["summary"], {})
    d["changes"] = store.uj(r["changes"], [])
    d["external"] = store.uj(r["external"], {})
    d["managed"] = store.uj(r["managed"], {})
    d["evaluation"] = store.uj(r["evaluation"], {})
    d["tradeoffs"] = store.uj(r["tradeoffs"], []) or []
    return d


def config_diff(before, after, changes):
    before, after = before or {}, after or {}
    reasons = {c["variable"]: c for c in (changes or [])}
    out = []
    for k in sorted(set(before) | set(after)):
        if k in VOLATILE:
            continue
        a, b = before.get(k), after.get(k)
        if a != b:
            c = reasons.get(k)
            out.append({"variable": k, "before": a, "after": b, "reason": (c or {}).get("reason"),
                        "source": (c or {}).get("source") or "outside the agent"})
    return out


def previous_run(r):
    """Previous run of the same session (runs of other sessions measure other workloads and are not comparable)."""
    p = store.q1("SELECT * FROM runs WHERE session_id=? AND seq<? AND status<>'aborted' ORDER BY seq DESC LIMIT 1", (r["session_id"], r["seq"]))
    return dict(p) if p else None


def metrics_window(env_id, t0, t1, points=400, host_id=None):
    if host_id:
        rows = store.q("SELECT ts, data FROM host_metrics WHERE host_id=? AND ts>=? AND ts<=? ORDER BY ts", (host_id, t0, t1))
    else:
        rows = store.q("SELECT ts, data FROM metrics WHERE env_id=? AND ts>=? AND ts<=? ORDER BY ts", (env_id, t0, t1))
    if not rows:
        return {"ts": [], "db": {}, "os": {}, "gaps": []}
    samples = [(r["ts"], store.uj(r["data"], {})) for r in rows]
    span = max(t1 - t0, 1)
    bucket = max(span / points, 1)
    buckets = {}
    for ts, d in samples:
        b = int((ts - t0) // bucket)
        buckets.setdefault(b, []).append(d)
    out = {"ts": [], "db": {}, "os": {}, "down": []}
    keys = {"db": set(), "os": set()}
    for _, d in samples:
        for g in ("db", "os"):
            keys[g].update(k for k, v in (d.get(g) or {}).items() if isinstance(v, (int, float)))
    for i, b in enumerate(sorted(buckets)):
        group = buckets[b]
        out["ts"].append(round(t0 + (b + 0.5) * bucket, 1))
        out["down"].append(any(not d.get("db_up", True) for d in group))
        for g in ("db", "os"):
            for k in keys[g]:
                vals = [d[g][k] for d in group if isinstance((d.get(g) or {}).get(k), (int, float))]
                out[g].setdefault(k, []).append(round(sum(vals) / len(vals), 4) if vals else None)
    return out


# ---------------------------------------------------------------------------- account API

@app.post("/api/auth/register")
def register(response: Response, body: dict = Body(...)):
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "enter a valid email address")
    if len(pw) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if store.q1("SELECT 1 FROM users WHERE email=?", (email,)):
        raise HTTPException(409, "an account with this email already exists")
    uid = store.ex("INSERT INTO users(email, name, pw_hash, created) VALUES (?,?,?,?)",
                   (email, (body.get("name") or "").strip()[:80], auth.hash_password(pw), now()))
    key = auth.new_api_key(uid, "default")
    token = auth.new_login(uid)
    response.set_cookie(auth.COOKIE, token, max_age=auth.LOGIN_TTL, httponly=True, samesite="lax", secure=PUBLIC_URL.startswith("https"))
    return {"ok": True, "api_key": key}


@app.post("/api/auth/login")
def login(response: Response, body: dict = Body(...)):
    email = (body.get("email") or "").strip().lower()
    u = store.q1("SELECT * FROM users WHERE email=?", (email,))
    if not u or not auth.check_password(body.get("password") or "", u["pw_hash"]):
        time.sleep(0.5)
        raise HTTPException(401, "wrong email or password")
    token = auth.new_login(u["id"])
    response.set_cookie(auth.COOKIE, token, max_age=auth.LOGIN_TTL, httponly=True, samesite="lax", secure=PUBLIC_URL.startswith("https"))
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.end_login(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    u = auth.current_user(request, required=False)
    if not u:
        return {"user": None}
    return {"user": {"id": u["id"], "email": u["email"], "name": u["name"]}, "server": PUBLIC_URL}


@app.post("/api/auth/password")
def change_password(request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    if not auth.check_password(body.get("old") or "", u["pw_hash"]):
        raise HTTPException(400, "current password is wrong")
    if len(body.get("new") or "") < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    store.ex("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(body["new"]), u["id"]))
    return {"ok": True}


@app.get("/api/settings/llm")
def get_llm_settings(request: Request):
    u = auth.current_user(request)
    return {**llm.settings(u["id"]), "modes": llm.MODES, "default_model": llm.DEFAULT_MODEL, "usage": llm.usage_summary(u["id"])}


@app.put("/api/settings/llm")
def put_llm_settings(request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    if body.get("mode") and body["mode"] not in llm.MODES:
        raise HTTPException(400, "unknown mode")
    if body.get("key"):
        ok, info = llm.verify_key(body["key"].strip())
        if not ok:
            raise HTTPException(400, f"OpenRouter rejected the key ({info.get('error')})")
        store.ex("UPDATE users SET llm_key_enc=? WHERE id=?", (llm.encrypt(body["key"].strip()), u["id"]))
    if body.get("remove_key"):
        store.ex("UPDATE users SET llm_key_enc=NULL WHERE id=?", (u["id"],))
    if body.get("model"):
        store.ex("UPDATE users SET llm_model=? WHERE id=?", (body["model"][:120], u["id"]))
    if body.get("mode"):
        store.ex("UPDATE users SET llm_mode=? WHERE id=?", (body["mode"], u["id"]))
    return get_llm_settings(request)


@app.get("/api/llm/models")
def llm_models(request: Request):
    auth.current_user(request)
    return {"models": llm.models()}


@app.get("/api/llm/calls")
def llm_calls(request: Request, session: int = None):
    u = auth.current_user(request)
    rows = store.q("SELECT id, session_id, ts, model, status, cached, latency_s, prompt_tokens, completion_tokens, cost, error, response "
                   "FROM llm_calls WHERE user_id=? AND (? IS NULL OR session_id=?) ORDER BY id DESC LIMIT 50", (u["id"], session, session))
    return {"calls": [store.row(r, ("response",)) for r in rows]}


@app.get("/api/keys")
def list_keys(request: Request):
    u = auth.current_user(request)
    rows = store.q("SELECT id, name, prefix, created, last_used, revoked FROM api_keys WHERE user_id=? ORDER BY id", (u["id"],))
    return {"keys": [dict(r) for r in rows]}


@app.post("/api/keys")
def create_key(request: Request, body: dict = Body(default={})):
    u = auth.current_user(request)
    key = auth.new_api_key(u["id"], (body.get("name") or "agent key")[:60])
    return {"api_key": key}


@app.delete("/api/keys/{key_id}")
def revoke_key(key_id: int, request: Request):
    u = auth.current_user(request)
    store.ex("UPDATE api_keys SET revoked=? WHERE id=? AND user_id=? AND revoked IS NULL", (now(), key_id, u["id"]))
    return {"ok": True}


# ---------------------------------------------------------------------------- UI data API

@app.get("/api/environments")
def environments(request: Request):
    u = auth.current_user(request)
    out = []
    for e in store.q("SELECT * FROM environments WHERE user_id=? ORDER BY name COLLATE NOCASE, id", (u["id"],)):
        b = env_brief(e)
        s = store.q1("SELECT COUNT(*) n, MAX(created) last FROM tsessions WHERE env_id=?", (e["id"],))
        b["sessions"] = s["n"]
        act = store.q1("SELECT id, name, goal FROM tsessions WHERE env_id=? AND status='active' ORDER BY id DESC LIMIT 1", (e["id"],))
        b["active_session"] = dict(act) if act else None
        m = store.q1("SELECT data FROM metrics WHERE env_id=? ORDER BY ts DESC LIMIT 1", (e["id"],))
        b["latest"] = store.uj(m["data"]) if m else None
        b["clients"] = [{"id": h["id"], "name": h["name"], "online": bool(h["last_seen"] and now() - h["last_seen"] < ONLINE_S)}
                        for h in store.q("SELECT id, name, last_seen FROM hosts WHERE env_id=? ORDER BY name", (e["id"],))]
        out.append(b)
    return {"environments": out}


@app.get("/api/environments/{env_id}")
def environment(env_id: int, request: Request):
    u = auth.current_user(request)
    e = own_env(env_id, u["id"])
    sessions = []
    for s in store.q("SELECT * FROM tsessions WHERE env_id=? ORDER BY id DESC", (env_id,)):
        d = dict(s)
        d["summary"] = store.uj(d["summary"])
        runs = [dict(r) for r in store.q("SELECT seq, label, goal_value, status, verdict, started, ended FROM runs WHERE session_id=? AND status<>'aborted' ORDER BY seq", (s["id"],))]
        d["runs"] = len(runs)
        done = [r for r in runs if r["goal_value"] is not None and r["verdict"] != "unreliable"]
        if done:
            first = done[0]["goal_value"]
            best = (min if s["goal"] == "latency" else max)(r["goal_value"] for r in done)
            d["first_value"], d["best_value"] = first, best
            if first:
                d["best_gain_pct"] = round(100 * ((first - best) / first if s["goal"] == "latency" else (best - first) / first), 2)
        sessions.append(d)
    events = [store.row(r, ("data",)) for r in store.q("SELECT * FROM events WHERE env_id=? ORDER BY ts DESC LIMIT 50", (env_id,))]
    hosts = [host_brief(dict(h)) for h in store.q("SELECT * FROM hosts WHERE env_id=? ORDER BY name", (env_id,))]
    return {"environment": env_brief(e), "facts": store.uj(e["facts"], {}), "sessions": sessions, "events": events, "hosts": hosts}


@app.patch("/api/environments/{env_id}")
def rename_environment(env_id: int, request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    own_env(env_id, u["id"])
    if body.get("name"):
        store.ex("UPDATE environments SET name=? WHERE id=?", (body["name"][:80], env_id))
    for f in ("cost_month", "cost_hourly", "provisioned_at", "destroyed_at"):
        if f in body:
            c = body.get(f)
            store.ex(f"UPDATE environments SET {f}=? WHERE id=?", (float(c) if c not in (None, "") else None, env_id))
    if "plan" in body:
        store.ex("UPDATE environments SET plan=? WHERE id=?", ((body.get("plan") or "")[:80] or None, env_id))
    return {"ok": True}


@app.delete("/api/environments/{env_id}")
def delete_environment(env_id: int, request: Request):
    u = auth.current_user(request)
    own_env(env_id, u["id"])
    for s in store.q("SELECT id FROM tsessions WHERE env_id=?", (env_id,)):
        _delete_session(s["id"])
    store.ex("DELETE FROM metrics WHERE env_id=?", (env_id,))
    store.ex("DELETE FROM events WHERE env_id=?", (env_id,))
    store.ex("DELETE FROM environments WHERE id=?", (env_id,))
    return {"ok": True}


@app.get("/api/environments/{env_id}/metrics")
def env_metrics(env_id: int, request: Request, start: float = None, end: float = None, points: int = 400):
    u = auth.current_user(request)
    own_env(env_id, u["id"])
    end = end or now()
    start = start or end - 3600
    pts = min(max(points, 20), 2000)
    m = metrics_window(env_id, start, end, pts)
    for h in store.q("SELECT id, name FROM hosts WHERE env_id=? ORDER BY name", (env_id,)):
        hm = metrics_window(None, start, end, pts, host_id=h["id"])
        m.setdefault("clients", []).append({"id": h["id"], "name": h["name"], "ts": hm["ts"], "os": hm["os"]})
    return m


@app.get("/api/environments/{env_id}/waits")
def env_waits(env_id: int, request: Request, start: float = None, end: float = None, limit: int = 15):
    """Top performance_schema wait events over a time range (average seconds waited per second = threads waiting)."""
    u = auth.current_user(request)
    own_env(env_id, u["id"])
    end = end or now()
    start = start or end - 3600
    tot, n = {}, 0
    for r in store.q("SELECT data FROM metrics WHERE env_id=? AND ts>=? AND ts<=?", (env_id, start, end)):
        w = (store.uj(r["data"], {}) or {}).get("waits")
        if not w:
            continue
        n += 1
        for k, v in w.items():
            tot[k] = tot.get(k, 0) + v
    rows = sorted(tot.items(), key=lambda x: -x[1])[:limit]
    allw = sum(tot.values()) or 1
    return {"samples": n, "events": [{"event": k, "avg_threads": round(v / n, 4), "pct": round(100 * v / allw, 1)} for k, v in rows] if n else []}


def _delete_session(sid):
    store.ex("DELETE FROM runs WHERE session_id=?", (sid,))
    store.ex("DELETE FROM recommendations WHERE session_id=?", (sid,))
    store.ex("DELETE FROM events WHERE session_id=?", (sid,))
    store.ex("DELETE FROM tsessions WHERE id=?", (sid,))


@app.get("/api/events")
def events_page(request: Request, env: int = None, session: int = None, offset: int = 0, limit: int = 20):
    u = auth.current_user(request)
    if session:
        own_session(session, u["id"])
        where, arg = "session_id=?", session
    else:
        own_env(env, u["id"])
        where, arg = "env_id=?", env
    total = store.q1(f"SELECT COUNT(*) n FROM events WHERE {where}", (arg,))["n"]
    rows = store.q(f"SELECT * FROM events WHERE {where} ORDER BY ts DESC LIMIT ? OFFSET ?", (arg, min(max(limit, 1), 200), max(offset, 0)))
    return {"total": total, "events": [store.row(r, ("data",)) for r in rows]}


@app.get("/api/sessions/{sid}")
def session(sid: int, request: Request):
    u = auth.current_user(request)
    s = own_session(sid, u["id"])
    s["summary"] = store.uj(s["summary"])
    s["calibration"] = store.uj(s.get("calibration"))
    e = own_env(s["env_id"], u["id"])
    runs = [run_brief(r) for r in store.q("SELECT * FROM runs WHERE session_id=? ORDER BY seq, id", (sid,))]
    recs = [store.row(r, ("items", "diagnosis", "workload", "llm")) for r in store.q("SELECT * FROM recommendations WHERE session_id=? ORDER BY id DESC", (sid,))]
    events = [store.row(r, ("data",)) for r in store.q("SELECT * FROM events WHERE session_id=? ORDER BY ts DESC LIMIT 200", (sid,))]
    st = store.q1("SELECT id, name, kind FROM studies WHERE id=?", (s.get("study_id"),)) if s.get("study_id") else None
    return {"session": s, "environment": env_brief(e), "runs": runs, "recommendations": recs, "events": events, "cost": session_costs(s, e),
            "study": dict(st) if st else None}


@app.patch("/api/sessions/{sid}")
def update_session(sid: int, request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    own_session(sid, u["id"])
    for f in ("name", "notes"):
        if f in body:
            store.ex(f"UPDATE tsessions SET {f}=? WHERE id=?", ((body[f] or "")[:4000], sid))
    if "study_id" in body:
        st = body.get("study_id")
        if st:
            own_study(int(st), u["id"])
        store.ex("UPDATE tsessions SET study_id=? WHERE id=?", (int(st) if st else None, sid))
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: int, request: Request):
    u = auth.current_user(request)
    own_session(sid, u["id"])
    _delete_session(sid)
    return {"ok": True}


def _parse_experiment(body):
    kind = body.get("kind")
    if kind == "try":
        changes = []
        for line in str(body.get("settings") or "").replace(",", "\n").splitlines():
            if line.strip():
                if "=" not in line:
                    raise HTTPException(400, f"expected variable=value, got {line.strip()!r}")
                k, v = line.split("=", 1)
                changes.append({"variable": k.strip(), "value": v.strip()})
        if not changes:
            raise HTTPException(400, "add at least one variable=value")
        spec = {"changes": changes}
    elif kind == "sweep":
        var = str(body.get("variable") or "").strip()
        values = [v.strip() for v in str(body.get("values") or "").split(",") if v.strip()]
        if not var or len(values) < 2:
            raise HTTPException(400, "a sweep needs a variable and at least two comma-separated values")
        spec = {"variable": var, "values": values[:12]}
    else:
        raise HTTPException(400, "kind must be 'try' or 'sweep'")
    return kind, spec, max(1, min(int(body.get("repeats") or 1), 5))


def _experiments(sid):
    return [store.row(r, ("spec", "results")) for r in store.q("SELECT * FROM experiments WHERE session_id=? ORDER BY id DESC", (sid,))]


@app.get("/api/sessions/{sid}/experiments")
def list_experiments(sid: int, request: Request):
    u = auth.current_user(request)
    own_session(sid, u["id"])
    return {"experiments": _experiments(sid)}


@app.post("/api/sessions/{sid}/experiments")
def add_experiment(sid: int, request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    s = own_session(sid, u["id"])
    if s["status"] != "active":
        raise HTTPException(400, "the session is not active")
    kind, spec, repeats = _parse_experiment(body)
    eid = store.ex("INSERT INTO experiments(session_id, created, created_by, kind, spec, repeats, reason, force) VALUES (?,?,?,?,?,?,?,?)",
                   (sid, now(), "human:" + u["email"], kind, store.j(spec), repeats, (body.get("reason") or "")[:500], 1 if body.get("force") else 0))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)",
             (s["env_id"], sid, now(), "experiment", f"experiment #{eid} queued by {u['email']}: {kind} {json.dumps(spec)[:200]}"))
    return {"id": eid, "experiments": _experiments(sid)}


@app.delete("/api/experiments/{eid}")
def cancel_experiment(eid: int, request: Request):
    u = auth.current_user(request)
    r = store.q1("SELECT * FROM experiments WHERE id=?", (eid,))
    if not r:
        raise HTTPException(404, "experiment not found")
    own_session(r["session_id"], u["id"])
    store.ex("UPDATE experiments SET status='cancelled', updated=? WHERE id=? AND status='queued'", (now(), eid))
    return {"ok": True}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int, request: Request, vs: int = None):
    u = auth.current_user(request)
    r = own_run(run_id, u["id"])
    s = own_session(r["session_id"], u["id"])
    prev = own_run(vs, u["id"]) if vs else previous_run(r)
    cfg = store.uj(r["config"], {})
    changes = store.uj(r["changes"], [])
    diff = config_diff(store.uj(prev["config"], {}) if prev else {}, cfg, changes) if prev else []
    recs = [store.row(x, ("items", "diagnosis", "workload", "llm")) for x in store.q("SELECT * FROM recommendations WHERE run_id=? ORDER BY id", (run_id,))]
    siblings = [{"id": x["id"], "seq": x["seq"], "label": x["label"]} for x in store.q("SELECT id, seq, label FROM runs WHERE session_id=? AND status<>'aborted' ORDER BY seq", (r["session_id"],))]
    out = run_brief(r)
    out.update({
        "inputs": store.uj(r["inputs"], {}), "diagnosis": store.uj(r["diagnosis"], []), "managed": store.uj(r["managed"], {}),
        "config_count": len(cfg), "diff": diff, "compared_with": run_brief(prev) if prev else None,
        "session": {**{k: s[k] for k in ("id", "name", "goal", "policy", "noise_pct", "env_id", "status", "goal_metric")},
                    "calibration": store.uj(s.get("calibration"))},
        "evaluation": store.uj(r["evaluation"], {}),
        "recommendations": recs, "runs": siblings,
        "environment": {"id": s["env_id"], "name": (store.q1("SELECT name FROM environments WHERE id=?", (s["env_id"],)) or {"name": ""})["name"]},
    })
    return out


@app.get("/api/runs/{run_id}/config")
def run_config(run_id: int, request: Request):
    u = auth.current_user(request)
    r = own_run(run_id, u["id"])
    return {"config": store.uj(r["config"], {}), "managed": store.uj(r["managed"], {}), "meta": store.uj(r["config_meta"], {}) or {}}


@app.get("/api/runs/{run_id}/metrics")
def run_metrics(run_id: int, request: Request, pad: float = 60, points: int = 400):
    u = auth.current_user(request)
    r = own_run(run_id, u["id"])
    s = own_session(r["session_id"], u["id"])
    t0 = (r["started"] or now()) - pad
    t1 = (r["ended"] or now()) + (pad if r["ended"] else 0)
    m = metrics_window(s["env_id"], t0, t1, points)
    m["window"] = [r["started"], r["ended"]]
    for h in store.q("SELECT id, name FROM hosts WHERE env_id=? ORDER BY name", (s["env_id"],)):
        hm = metrics_window(None, t0, t1, points, host_id=h["id"])
        if hm["ts"]:
            m.setdefault("clients", []).append({"name": h["name"], "ts": hm["ts"], "os": hm["os"]})
    return m


# ---------------------------------------------------------------------------- agent API

def agent_env(env_id, key):
    e = store.q1("SELECT * FROM environments WHERE id=? AND user_id=?", (env_id, key["user_id"]))
    if not e:
        raise HTTPException(404, "environment not found for this API key")
    return dict(e)


def agent_session(sid, key):
    s = store.q1("SELECT s.* FROM tsessions s JOIN environments e ON e.id=s.env_id WHERE s.id=? AND e.user_id=?", (sid, key["user_id"]))
    if not s:
        raise HTTPException(404, "session not found for this API key")
    return dict(s)


def agent_run(rid, key):
    r = store.q1("SELECT r.*, s.env_id FROM runs r JOIN tsessions s ON s.id=r.session_id JOIN environments e ON e.id=s.env_id "
                 "WHERE r.id=? AND e.user_id=?", (rid, key["user_id"]))
    if not r:
        raise HTTPException(404, "run not found for this API key")
    return dict(r)


@app.post("/api/v1/agent/hello")
def agent_hello(body: dict = Body(...), key=Depends(auth.agent_key)):
    uid = (body.get("agent_uid") or "")[:64]
    if not uid:
        raise HTTPException(400, "agent_uid required")
    facts = body.get("facts") or {}
    host = (facts.get("hardware") or {}).get("hostname")
    e = store.q1("SELECT id FROM environments WHERE user_id=? AND agent_uid=?", (key["user_id"], uid))
    if e:
        store.ex("UPDATE environments SET hostname=?, agent_version=?, facts=?, last_seen=?, api_key_id=? WHERE id=?",
                 (host, body.get("agent_version"), store.j(facts), now(), key["id"], e["id"]))
        eid = e["id"]
    else:
        eid = store.ex("INSERT INTO environments(user_id, agent_uid, name, hostname, agent_version, facts, created, last_seen, api_key_id) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (key["user_id"], uid, (body.get("name") or host or "environment")[:80], host,
                                                       body.get("agent_version"), store.j(facts), now(), now(), key["id"]))
        store.ex("INSERT INTO events(env_id, ts, type, message) VALUES (?,?,?,?)", (eid, now(), "agent", "environment registered"))
    return {"environment_id": eid, "url": f"{PUBLIC_URL}/#/env/{eid}"}


@app.post("/api/v1/agent/metrics")
def agent_metrics(body: dict = Body(...), key=Depends(auth.agent_key)):
    e = agent_env(body.get("environment_id"), key)
    rows = []
    for s in (body.get("samples") or [])[:2000]:
        if isinstance(s, dict) and isinstance(s.get("ts"), (int, float)):
            rows.append((e["id"], float(s["ts"]), store.j({k: s.get(k) for k in ("os", "db", "db_up", "db_error", "session_id", "run_id", "waits")})))
    c = store.conn()
    c.execute("BEGIN")
    c.executemany("INSERT OR REPLACE INTO metrics(env_id, ts, data) VALUES (?,?,?)", rows)
    c.execute("UPDATE environments SET last_seen=?, agent_state='running' WHERE id=?", (now(), e["id"]))
    c.execute("COMMIT")
    return {"ok": True, "stored": len(rows)}


@app.post("/api/v1/agent/bye")
def agent_bye(body: dict = Body(...), key=Depends(auth.agent_key)):
    """The agent daemon was stopped on purpose (service stop): show 'agent stopped' instead of 'offline'."""
    e = agent_env(body.get("environment_id"), key)
    store.ex("UPDATE environments SET agent_state='stopped', agent_state_at=? WHERE id=?", (now(), e["id"]))
    return {"ok": True}


@app.post("/api/v1/agent/events")
def agent_event(body: dict = Body(...), key=Depends(auth.agent_key)):
    e = agent_env(body.get("environment_id"), key)
    sid = body.get("session_id")
    if sid:
        agent_session(sid, key)
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message, data) VALUES (?,?,?,?,?,?)",
             (e["id"], sid, now(), (body.get("type") or "event")[:40], (body.get("message") or "")[:500], store.j(body.get("data"))))
    store.ex("UPDATE environments SET last_seen=? WHERE id=?", (now(), e["id"]))
    return {"ok": True}


@app.post("/api/v1/agent/sessions")
def agent_session_start(body: dict = Body(...), key=Depends(auth.agent_key)):
    e = agent_env(body.get("environment_id"), key)
    goal = body.get("goal") if body.get("goal") in ("tps", "latency") else "tps"
    store.ex("UPDATE tsessions SET status='abandoned', ended=? WHERE env_id=? AND status='active'", (now(), e["id"]))
    n = store.q1("SELECT COUNT(*) n FROM tsessions WHERE env_id=?", (e["id"],))["n"] + 1
    name = (body.get("name") or f"Session {n} ({'TPS' if goal == 'tps' else 'latency'})")[:120]
    sid = store.ex("INSERT INTO tsessions(env_id, name, goal, policy, noise_pct, workload, notes, created, os_tuning) VALUES (?,?,?,?,?,?,?,?,?)",
                   (e["id"], name, goal, body.get("policy") or "safe", body.get("noise_pct") or 2.0, body.get("workload"), body.get("notes"), now(),
                    1 if body.get("os_tuning", True) else 0))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message, data) VALUES (?,?,?,?,?,?)",
             (e["id"], sid, now(), "session", f"session started: goal={goal}, policy={body.get('policy') or 'safe'}", store.j(body)))
    study = None
    if body.get("study"):
        study = study_get_or_create(key["user_id"], body["study"], goal, body.get("workload"))
        store.ex("UPDATE tsessions SET study_id=? WHERE id=?", (study["id"], sid))
    return {"session_id": sid, "name": name, "url": f"{PUBLIC_URL}/#/session/{sid}",
            "study": {"id": study["id"], "name": study["name"], "url": f"{PUBLIC_URL}/#/study/{study['id']}"} if study else None}


@app.post("/api/v1/agent/sessions/{sid}/close")
def agent_session_close(sid: int, body: dict = Body(default={}), key=Depends(auth.agent_key)):
    s = agent_session(sid, key)
    store.ex("UPDATE tsessions SET status='closed', ended=?, summary=? WHERE id=?", (now(), store.j(body.get("summary")), sid))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)", (s["env_id"], sid, now(), "session", "session closed"))
    return {"ok": True}


@app.post("/api/v1/agent/sessions/{sid}/update")
def agent_session_update(sid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    agent_session(sid, key)
    if body.get("noise_pct") is not None:
        store.ex("UPDATE tsessions SET noise_pct=? WHERE id=?", (float(body["noise_pct"]), sid))
    if body.get("goal_metric"):
        store.ex("UPDATE tsessions SET goal_metric=? WHERE id=?", (str(body["goal_metric"])[:40], sid))
    if "calibration" in body:
        store.ex("UPDATE tsessions SET calibration=? WHERE id=?", (store.j(body["calibration"]), sid))
    return {"ok": True}


def cpu_class(n):
    n = int(n or 0)
    return "1-2" if n <= 2 else "3-8" if n <= 8 else "9-32" if n <= 32 else "33+"


@app.get("/api/v1/agent/experience")
def agent_experience(workload: str = None, cpus: int = None, exclude_session: int = None, key=Depends(auth.agent_key)):
    """Outcomes of each recommendation in this account's earlier sessions with a similar workload and CPU class."""
    rows = store.q(
        "SELECT r.changes, r.verdict, r.evaluation, json_extract(r.inputs, '$.workload.type') AS wl, "
        "json_extract(e.facts, '$.hardware.cpus') AS cpus, r.session_id FROM runs r JOIN tsessions s ON s.id=r.session_id "
        "JOIN environments e ON e.id=s.env_id WHERE e.user_id=? AND r.status='done' AND r.verdict IN "
        "('improved','efficiency-gain','neutral','regressed','efficiency-loss')", (key["user_id"],))
    out = {}
    for r in rows:
        if exclude_session and r["session_id"] == exclude_session:
            continue
        if workload and r["wl"] and r["wl"] != workload:
            continue
        if cpus and r["cpus"] and cpu_class(r["cpus"]) != cpu_class(cpus):
            continue
        ids = {c.get("source", "")[4:] for c in (store.uj(r["changes"], []) or []) if str(c.get("source", "")).startswith("rec:")}
        ids = {i for i in ids if not i.startswith(("revert", "rollback"))}
        delta = (store.uj(r["evaluation"], {}) or {}).get("goal_delta_pct")
        for rid in ids:
            e = out.setdefault(rid, {"n": 0, "wins": 0, "losses": 0, "neutral": 0, "deltas": []})
            e["n"] += 1
            if r["verdict"] in ("improved", "efficiency-gain"):
                e["wins"] += 1
            elif r["verdict"] in ("regressed", "efficiency-loss"):
                e["losses"] += 1
            else:
                e["neutral"] += 1
            if delta is not None:
                e["deltas"].append(delta)
    for e in out.values():
        d = e.pop("deltas")
        e["mean_delta_pct"] = round(sum(d) / len(d), 2) if d else None
    return {"workload": workload, "cpu_class": cpu_class(cpus) if cpus else None, "experience": out}


@app.post("/api/v1/agent/llm/suggest")
def agent_llm_suggest(body: dict = Body(...), key=Depends(auth.agent_key)):
    s = agent_session(body.get("session_id"), key)
    res = llm.suggest(key["user_id"], s["env_id"], s["id"], body.get("context") or {}, force=bool(body.get("force")),
                      trigger=body.get("trigger") or "routine")
    if res.get("enabled") and not res.get("error"):
        store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)",
                 (s["env_id"], s["id"], now(), "llm", f"{res.get('model')}: {len(res.get('suggestions') or [])} suggestion(s)"
                  + (" (cached)" if res.get("cached") else "")))
    return res


@app.get("/api/v1/agent/sessions/{sid}/experiments")
def agent_experiments(sid: int, status: str = "queued", key=Depends(auth.agent_key)):
    agent_session(sid, key)
    return {"experiments": [e for e in _experiments(sid) if not status or e["status"] == status][::-1]}


@app.post("/api/v1/agent/sessions/{sid}/experiments")
def agent_add_experiment(sid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    s = agent_session(sid, key)
    kind, spec, repeats = _parse_experiment(body)
    eid = store.ex("INSERT INTO experiments(session_id, created, created_by, kind, spec, repeats, reason, force) VALUES (?,?,?,?,?,?,?,?)",
                   (sid, now(), body.get("created_by") or "agent", kind, store.j(spec), repeats, (body.get("reason") or "")[:500], 1 if body.get("force") else 0))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)",
             (s["env_id"], sid, now(), "experiment", f"experiment #{eid} queued: {kind} {json.dumps(spec)[:200]}"))
    return {"id": eid}


@app.post("/api/v1/agent/experiments/{eid}")
def agent_update_experiment(eid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    r = store.q1("SELECT * FROM experiments WHERE id=?", (eid,))
    if not r:
        raise HTTPException(404, "experiment not found")
    s = agent_session(r["session_id"], key)
    store.ex("UPDATE experiments SET status=?, results=COALESCE(?, results), message=COALESCE(?, message), updated=? WHERE id=?",
             (body.get("status") or r["status"], store.j(body.get("results")) if body.get("results") is not None else None,
              body.get("message"), now(), eid))
    if body.get("status") in ("done", "failed"):
        store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)",
                 (s["env_id"], s["id"], now(), "experiment", f"experiment #{eid} {body['status']}: {(body.get('message') or '')[:250]}"))
    return {"ok": True}


# ---------------------------------------------------------------------------- client hosts + command channel

def agent_host(host_id, key):
    h = store.q1("SELECT * FROM hosts WHERE id=? AND user_id=?", (host_id, key["user_id"]))
    if not h:
        raise HTTPException(404, "host not found for this API key")
    return dict(h)


def host_brief(h):
    f = store.uj(h["facts"], {}) or {}
    m = store.q1("SELECT data FROM host_metrics WHERE host_id=? ORDER BY ts DESC LIMIT 1", (h["id"],))
    return {"id": h["id"], "name": h["name"], "role": h["role"], "env_id": h["env_id"], "last_seen": h["last_seen"],
            "online": bool(h["last_seen"] and now() - h["last_seen"] < ONLINE_S), "facts": f, "latest": store.uj(m["data"]) if m else None}


@app.post("/api/v1/agent/hosts/hello")
def agent_host_hello(body: dict = Body(...), key=Depends(auth.agent_key)):
    env = agent_env(body.get("environment_id"), key)
    uid = (body.get("agent_uid") or "")[:64]
    if not uid:
        raise HTTPException(400, "agent_uid required")
    h = store.q1("SELECT id FROM hosts WHERE user_id=? AND agent_uid=?", (key["user_id"], uid))
    facts = store.j(body.get("facts") or {})
    if h:
        store.ex("UPDATE hosts SET env_id=?, name=?, role=?, facts=?, last_seen=? WHERE id=?",
                 (env["id"], (body.get("name") or "")[:80], body.get("role") or "client", facts, now(), h["id"]))
        hid = h["id"]
    else:
        hid = store.ex("INSERT INTO hosts(user_id, env_id, agent_uid, name, role, facts, created, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                       (key["user_id"], env["id"], uid, (body.get("name") or "")[:80], body.get("role") or "client", facts, now(), now()))
        store.ex("INSERT INTO events(env_id, ts, type, message) VALUES (?,?,?,?)", (env["id"], now(), "agent", f"client host {body.get('name')} linked"))
    return {"host_id": hid, "environment_id": env["id"]}


@app.post("/api/v1/agent/hosts/{hid}/metrics")
def agent_host_metrics(hid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    agent_host(hid, key)
    rows = [(hid, float(x["ts"]), store.j({k: x.get(k) for k in ("os",)})) for x in (body.get("samples") or [])[:2000]
            if isinstance(x, dict) and isinstance(x.get("ts"), (int, float))]
    c = store.conn()
    c.execute("BEGIN")
    c.executemany("INSERT OR REPLACE INTO host_metrics(host_id, ts, data) VALUES (?,?,?)", rows)
    c.execute("UPDATE hosts SET last_seen=? WHERE id=?", (now(), hid))
    c.execute("COMMIT")
    return {"ok": True, "stored": len(rows)}


@app.get("/api/v1/agent/hosts/{hid}/commands")
def agent_host_commands(hid: int, key=Depends(auth.agent_key)):
    agent_host(hid, key)
    store.ex("UPDATE hosts SET last_seen=? WHERE id=?", (now(), hid))
    rows = store.q("SELECT * FROM commands WHERE host_id=? AND status='queued' ORDER BY id", (hid,))
    return {"commands": [store.row(r, ("payload", "result")) for r in rows]}


@app.post("/api/v1/agent/commands/{cid}")
def agent_command_update(cid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    r = store.q1("SELECT * FROM commands WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404, "command not found")
    agent_host(r["host_id"], key)
    store.ex("UPDATE commands SET status=?, result=COALESCE(?, result), updated=? WHERE id=?",
             (body.get("status") or r["status"], store.j(body.get("result")) if body.get("result") is not None else None, now(), cid))
    return {"ok": True}


@app.get("/api/v1/agent/commands/{cid}")
def agent_command_get(cid: int, key=Depends(auth.agent_key)):
    r = store.q1("SELECT * FROM commands WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404, "command not found")
    agent_host(r["host_id"], key)
    return store.row(r, ("payload", "result"))


@app.get("/api/v1/agent/environments/{env_id}/hosts")
def agent_env_hosts(env_id: int, key=Depends(auth.agent_key)):
    agent_env(env_id, key)
    return {"hosts": [host_brief(dict(h)) for h in store.q("SELECT * FROM hosts WHERE env_id=? ORDER BY name", (env_id,))]}


@app.post("/api/v1/agent/hosts/{hid}/commands")
def agent_queue_command(hid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    h = agent_host(hid, key)
    cid = store.ex("INSERT INTO commands(host_id, session_id, created, action, payload) VALUES (?,?,?,?,?)",
                   (hid, body.get("session_id"), now(), body.get("action"), store.j(body.get("payload"))))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)",
             (h["env_id"], body.get("session_id"), now(), "client", f"{body.get('action')} sent to {h['name']}"))
    return {"command_id": cid}


@app.get("/api/v1/agent/hosts/{hid}/window")
def agent_host_window(hid: int, start: float, end: float, key=Depends(auth.agent_key)):
    """Averages of a client host's metrics over a measurement window."""
    agent_host(hid, key)
    rows = store.q("SELECT data FROM host_metrics WHERE host_id=? AND ts>=? AND ts<=?", (hid, start, end))
    acc = {}
    for r in rows:
        for k, v in ((store.uj(r["data"], {}) or {}).get("os") or {}).items():
            if isinstance(v, (int, float)):
                acc.setdefault(k, []).append(v)
    return {"samples": len(rows), "avg": {k: round(sum(v) / len(v), 4) for k, v in acc.items()},
            "max": {k: max(v) for k, v in acc.items()}}


@app.post("/api/v1/agent/runs")
def agent_run_create(body: dict = Body(...), key=Depends(auth.agent_key)):
    s = agent_session(body.get("session_id"), key)
    rid = store.ex("INSERT INTO runs(session_id, seq, label, notes, status, started, config, managed, changes, config_meta, tradeoffs) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (s["id"], body.get("seq"), (body.get("label") or "")[:120], body.get("notes"), "measuring", body.get("started") or now(),
                    store.j(body.get("config")), store.j(body.get("managed")), store.j(body.get("changes")),
                    store.j(body.get("config_meta")), store.j(body.get("tradeoffs") or [])))
    return {"run_id": rid, "url": f"{PUBLIC_URL}/#/run/{rid}"}


@app.post("/api/v1/agent/runs/{rid}/finish")
def agent_run_finish(rid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    agent_run(rid, key)
    store.ex("UPDATE runs SET status='done', ended=?, summary=?, external=?, inputs=?, diagnosis=?, goal_value=?, goal_source=?, "
             "delta_pct=?, delta_vs_first_pct=?, verdict=?, evaluation=?, notes=COALESCE(?, notes) WHERE id=?",
             (body.get("ended") or now(), store.j(body.get("summary")), store.j(body.get("external")), store.j(body.get("inputs")),
              store.j(body.get("diagnosis")), body.get("goal_value"), body.get("goal_source"), body.get("delta_pct"),
              body.get("delta_vs_first_pct"), body.get("verdict"), store.j(body.get("evaluation")), body.get("notes"), rid))
    return {"ok": True}


@app.post("/api/v1/agent/runs/{rid}/evaluation")
def agent_run_evaluation(rid: int, body: dict = Body(...), key=Depends(auth.agent_key)):
    agent_run(rid, key)
    store.ex("UPDATE runs SET verdict=?, evaluation=? WHERE id=?", (body.get("verdict"), store.j(body.get("evaluation")), rid))
    return {"ok": True}


@app.post("/api/v1/agent/runs/{rid}/abort")
def agent_run_abort(rid: int, body: dict = Body(default={}), key=Depends(auth.agent_key)):
    r = agent_run(rid, key)
    reason = (body.get("reason") or "aborted (no reason reported by this agent version)")[:1000]
    store.ex("UPDATE runs SET status='aborted', ended=?, abort_reason=? WHERE id=?", (now(), reason, rid))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)",
             (r["env_id"], r["session_id"], now(), "measure_abort", f"run {r['seq']} aborted: {reason[:300]}"))
    return {"ok": True}


@app.post("/api/v1/agent/recommendations")
def agent_recommendation(body: dict = Body(...), key=Depends(auth.agent_key)):
    s = agent_session(body.get("session_id"), key)
    if body.get("run_id"):
        agent_run(body["run_id"], key)
    rid = store.ex("INSERT INTO recommendations(session_id, run_id, created, goal, policy, items, diagnosis, next, workload, phase, llm) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (s["id"], body.get("run_id"), now(), body.get("goal"), body.get("policy"), store.j(body.get("items")),
                    store.j(body.get("diagnosis")), body.get("next"), store.j(body.get("workload")), body.get("phase"), store.j(body.get("llm"))))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message, data) VALUES (?,?,?,?,?,?)",
             (s["env_id"], s["id"], now(), "recommend", f"{len(body.get('items') or [])} recommendation(s); next: {body.get('next') or '-'}",
              store.j({"recommendation_id": rid})))
    return {"recommendation_id": rid}


# ---------------------------------------------------------------------------- agent install

_tar_cache = {}


def agent_tarball():
    mtime = max(os.path.getmtime(os.path.join(dp, f)) for dp, _, fs in os.walk(AGENT_DIR) for f in fs)
    if _tar_cache.get("mtime") != mtime:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for dp, dns, fs in os.walk(AGENT_DIR):
                dns[:] = [d for d in dns if d not in ("__pycache__", ".git", "build") and not d.endswith(".egg-info")]
                for f in fs:
                    full = os.path.join(dp, f)
                    t.add(full, arcname=os.path.join("tuning-agent", os.path.relpath(full, AGENT_DIR)))
        _tar_cache.update(mtime=mtime, data=buf.getvalue())
    return _tar_cache["data"]


@app.get("/download/tuning-agent.tar.gz")
def download_agent():
    return Response(agent_tarball(), media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=tuning-agent.tar.gz"})


@app.get("/install.sh")
def install_script():
    with open(os.path.join(APP_DIR, "install.sh")) as f:
        return PlainTextResponse(f.read().replace("__SERVER__", PUBLIC_URL), media_type="text/x-shellscript")


@app.get("/agent-guide.md")
def agent_guide():
    with open(os.path.join(APP_DIR, "..", "..", "docs", "claude-code-skill", "SKILL.md")) as f:
        return PlainTextResponse(f.read().replace("__SERVER__", PUBLIC_URL), media_type="text/markdown")


def _knowledge():
    import importlib
    import sys
    if AGENT_DIR not in sys.path:
        sys.path.insert(0, os.path.abspath(AGENT_DIR))
    return importlib.import_module("tuning_agent.knowledge")


@app.get("/api/policies")
def policies():
    return _knowledge().policy_rules()


@app.get("/healthz")
def healthz():
    store.q1("SELECT 1")
    return {"ok": True}


@app.exception_handler(HTTPException)
def http_error(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/")
def index():
    return FileResponse(os.path.join(APP_DIR, "static", "index.html"), headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


# ---------------------------------------------------------------------------- studies (experiments across environments)
STUDY_KINDS = ("compare", "scaling", "min-hardware")


def own_study(study_id, user_id):
    st = store.q1("SELECT * FROM studies WHERE id=? AND user_id=?", (study_id, user_id))
    if not st:
        raise HTTPException(404, "study not found")
    return dict(st)


def study_get_or_create(user_id, ref, goal="tps", workload=None, kind="compare"):
    ref = str(ref).strip()
    st = store.q1("SELECT * FROM studies WHERE user_id=? AND (CAST(id AS TEXT)=? OR name=?) ORDER BY id LIMIT 1", (user_id, ref, ref))
    if st:
        return dict(st)
    sid = store.ex("INSERT INTO studies(user_id, name, kind, goal, workload, created) VALUES (?,?,?,?,?,?)",
                   (user_id, ref[:120], kind if kind in STUDY_KINDS else "compare", goal, workload, now()))
    return dict(store.q1("SELECT * FROM studies WHERE id=?", (sid,)))


def session_results(s):
    """Per-session headline numbers: first run (usually server defaults), best configuration and best production-safe
    configuration, each as the mean of the runs of that configuration (repeated runs average out noise)."""
    runs = [dict(r) for r in store.q("SELECT seq, label, goal_value, managed, tradeoffs, verdict FROM runs WHERE session_id=? "
                                     "AND status<>'aborted' AND goal_value IS NOT NULL AND COALESCE(verdict,'')<>'unreliable' ORDER BY seq", (s["id"],))]
    if not runs:
        return {"runs": 0}
    lat = s["goal"] == "latency"
    groups = {}
    for r in runs:
        m = store.uj(r["managed"], {}) or {}
        g = groups.setdefault(store.j(sorted(m.items())), {"runs": [], "managed": m, "safe": True})
        g["runs"].append(r)
        if store.uj(r["tradeoffs"]) or []:
            g["safe"] = False
    cfgs = [{"mean": sum(x["goal_value"] for x in g["runs"]) / len(g["runs"]), "n": len(g["runs"]), "seq": g["runs"][-1]["seq"],
             "label": g["runs"][0]["label"], "safe": g["safe"], "settings": len(g["managed"])} for g in groups.values()]
    pick = (min if lat else max)
    best = pick(cfgs, key=lambda c: c["mean"])
    safe = [c for c in cfgs if c["safe"]]
    best_safe = pick(safe, key=lambda c: c["mean"]) if safe else None
    first = runs[0]
    out = {"runs": len(runs), "first": {"value": first["goal_value"], "label": first["label"], "seq": first["seq"]},
           "best": best, "best_safe": best_safe}
    for k in ("best", "best_safe"):
        if out[k] and first["goal_value"]:
            f, b = first["goal_value"], out[k]["mean"]
            out[k]["gain_pct"] = round(100 * ((f - b) / f if lat else (b - f) / f), 2)
    return out


def hourly_rate(e):
    e = dict(e)
    if e.get("cost_hourly"):
        return float(e["cost_hourly"])
    return float(e["cost_month"]) / 730.0 if e.get("cost_month") else None


def session_costs(s, e):
    """Machine cost of a session and of each of its runs: wall time x the environment's hourly price. A run's time is
    everything since the previous run ended (data reset, warm-up, measurement)."""
    rate = hourly_rate(e)
    if not rate:
        return None
    runs = store.q("SELECT id, seq, started, ended, status FROM runs WHERE session_id=? ORDER BY seq, id", (s["id"],))
    prev, per_run = s["created"], {}
    for r in runs:
        end = r["ended"] or (now() if r["status"] == "measuring" else None)
        if end:
            per_run[r["id"]] = round((end - prev) / 3600 * rate, 4)
            prev = end
    end = s.get("ended") or (now() if s.get("status") == "active" else prev)
    return {"rate_hourly": rate, "hours": round((end - s["created"]) / 3600, 2), "total": round((end - s["created"]) / 3600 * rate, 3),
            "per_run": per_run}


def env_cost(e):
    rate = hourly_rate(e)
    e = dict(e)
    if not rate or not e.get("provisioned_at"):
        return None
    end = e.get("destroyed_at") or now()
    return {"rate_hourly": rate, "hours": round((end - e["provisioned_at"]) / 3600, 2), "total": round((end - e["provisioned_at"]) / 3600 * rate, 3),
            "destroyed": bool(e.get("destroyed_at"))}


def study_view(st, user_id):
    sessions = []
    for s in store.q("SELECT s.*, e.name env_name FROM tsessions s JOIN environments e ON e.id=s.env_id "
                     "WHERE s.study_id=? AND e.user_id=? ORDER BY s.id", (st["id"], user_id)):
        s = dict(s)
        e = own_env(s["env_id"], user_id)
        sessions.append({"id": s["id"], "name": s["name"], "status": s["status"], "goal": s["goal"], "policy": s["policy"],
                         "goal_metric": s.get("goal_metric"), "noise_pct": s["noise_pct"], "workload": s["workload"],
                         "created": s["created"], "environment": env_brief(e), "results": session_results(s),
                         "cost": session_costs(s, e), "env_cost": env_cost(e)})
    return sessions


@app.get("/api/studies")
def studies(request: Request):
    u = auth.current_user(request)
    out = []
    for st in store.q("SELECT * FROM studies WHERE user_id=? ORDER BY id DESC", (u["id"],)):
        st = dict(st)
        n = store.q1("SELECT COUNT(*) n, COUNT(DISTINCT env_id) e, SUM(status='active') a FROM tsessions WHERE study_id=?", (st["id"],))
        st.update(sessions=n["n"], environments=n["e"], active=n["a"] or 0)
        out.append(st)
    return {"studies": out, "kinds": STUDY_KINDS}


@app.post("/api/studies")
def create_study(request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    if not (body.get("name") or "").strip():
        raise HTTPException(400, "name required")
    kind = body.get("kind") if body.get("kind") in STUDY_KINDS else "compare"
    goal = body.get("goal") if body.get("goal") in ("tps", "latency") else "tps"
    target = float(body["target"]) if body.get("target") not in (None, "") else None
    sid = store.ex("INSERT INTO studies(user_id, name, kind, goal, target, workload, notes, created) VALUES (?,?,?,?,?,?,?,?)",
                   (u["id"], body["name"].strip()[:120], kind, goal, target, (body.get("workload") or "")[:300] or None,
                    (body.get("notes") or "")[:4000] or None, now()))
    for x in body.get("session_ids") or []:
        own_session(int(x), u["id"])
        store.ex("UPDATE tsessions SET study_id=? WHERE id=?", (sid, int(x)))
    return {"id": sid}


@app.get("/api/studies/{study_id}")
def study(study_id: int, request: Request):
    u = auth.current_user(request)
    st = own_study(study_id, u["id"])
    others = [dict(r) for r in store.q("SELECT s.id, s.name, s.goal, e.name env_name FROM tsessions s JOIN environments e ON e.id=s.env_id "
                                        "WHERE e.user_id=? AND (s.study_id IS NULL OR s.study_id<>?) ORDER BY s.id DESC LIMIT 200",
                                        (u["id"], study_id))]
    return {"study": st, "sessions": study_view(st, u["id"]), "other_sessions": others}


@app.get("/api/studies/{study_id}/costs")
def study_costs(study_id: int, request: Request, format: str = "json"):
    """Cost of a study: per environment (machine lifetime), per session and per run."""
    u = auth.current_user(request)
    st = own_study(study_id, u["id"])
    rows, envs = [], {}
    for x in study_view(st, u["id"]):
        e = x["environment"]
        envs[e["id"]] = {"environment": e["name"], "plan": e.get("plan"), "rate_hourly": (x["cost"] or {}).get("rate_hourly"), "machine": x["env_cost"]}
        for r in store.q("SELECT id, seq, label, goal_value, verdict FROM runs WHERE session_id=? ORDER BY seq", (x["id"],)):
            rows.append({"environment": e["name"], "session": x["id"], "run": r["seq"], "label": r["label"], "goal": r["goal_value"],
                         "verdict": r["verdict"], "cost_usd": ((x["cost"] or {}).get("per_run") or {}).get(r["id"])})
    total = round(sum((v["machine"] or {}).get("total") or 0 for v in envs.values()), 3)
    if format == "csv":
        from fastapi.responses import PlainTextResponse
        lines = ["environment,session,run,label,goal,verdict,cost_usd"] + [
            ",".join(str(r[k]).replace(",", ";") if r[k] is not None else "" for k in ("environment", "session", "run", "label", "goal", "verdict", "cost_usd")) for r in rows]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv")
    return {"study": st["name"], "total_usd": total, "environments": list(envs.values()), "runs": rows}


@app.patch("/api/studies/{study_id}")
def update_study(study_id: int, request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    own_study(study_id, u["id"])
    for f in ("name", "workload", "notes"):
        if f in body:
            store.ex(f"UPDATE studies SET {f}=? WHERE id=?", ((body[f] or "")[:4000] or None, study_id))
    if "kind" in body and body["kind"] in STUDY_KINDS:
        store.ex("UPDATE studies SET kind=? WHERE id=?", (body["kind"], study_id))
    if "goal" in body and body["goal"] in ("tps", "latency"):
        store.ex("UPDATE studies SET goal=? WHERE id=?", (body["goal"], study_id))
    if "target" in body:
        store.ex("UPDATE studies SET target=? WHERE id=?", (float(body["target"]) if body["target"] not in (None, "") else None, study_id))
    return {"ok": True}


@app.delete("/api/studies/{study_id}")
def delete_study(study_id: int, request: Request):
    u = auth.current_user(request)
    own_study(study_id, u["id"])
    store.ex("UPDATE tsessions SET study_id=NULL WHERE study_id=?", (study_id,))
    store.ex("DELETE FROM studies WHERE id=?", (study_id,))
    return {"ok": True}


@app.get("/api/v1/agent/studies")
def agent_studies(key=Depends(auth.agent_key)):
    out = []
    for st in store.q("SELECT * FROM studies WHERE user_id=? ORDER BY id DESC", (key["user_id"],)):
        st = dict(st)
        st["sessions"] = study_view(st, key["user_id"])
        st["url"] = f"{PUBLIC_URL}/#/study/{st['id']}"
        out.append(st)
    return {"studies": out}


@app.post("/api/v1/agent/studies")
def agent_study_create(body: dict = Body(...), key=Depends(auth.agent_key)):
    if not (body.get("name") or "").strip():
        raise HTTPException(400, "name required")
    st = study_get_or_create(key["user_id"], body["name"], body.get("goal") or "tps", body.get("workload"), body.get("kind") or "compare")
    upd = {k: body[k] for k in ("kind", "target", "notes") if body.get(k) not in (None, "")}
    if upd.get("kind") in STUDY_KINDS:
        store.ex("UPDATE studies SET kind=? WHERE id=?", (upd["kind"], st["id"]))
    if "target" in upd:
        store.ex("UPDATE studies SET target=? WHERE id=?", (float(upd["target"]), st["id"]))
    if "notes" in upd:
        store.ex("UPDATE studies SET notes=? WHERE id=?", (str(upd["notes"])[:4000], st["id"]))
    return {"id": st["id"], "name": st["name"], "url": f"{PUBLIC_URL}/#/study/{st['id']}"}
