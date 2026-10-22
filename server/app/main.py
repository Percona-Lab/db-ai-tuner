"""AI DB Tuner web service."""
import contextvars
import io
import json
import threading
import math
import os
import re
import tarfile
import time

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, learner, llm, status, store

APP_DIR = os.path.dirname(__file__)
AGENT_DIR = os.environ.get("TUNER_AGENT_DIR", os.path.join(APP_DIR, "..", "..", "agent"))
PUBLIC_URL = os.environ.get("TUNER_PUBLIC_URL", "http://localhost:8000").rstrip("/")
ONLINE_S = 60

app = FastAPI(title="AI DB Tuner", docs_url=None, redoc_url=None)
store.init()

# ---------------------------------------------------------------------------- read-only share links
SHARE = contextvars.ContextVar("share", default=None)  # scope of the share link used by the current request
SHARE_PATHS = re.compile(r"^/api/(me|policies|environments(/\d+(/(metrics|waits))?)?|sessions/\d+(/experiments)?|runs/\d+(/(config|metrics))?|"
                         r"studies(/\d+(/costs)?)?|events)$")


def share_scope(sh):
    if sh["kind"] == "environment":
        return {"kind": "environment", "id": sh["object_id"], "envs": {sh["object_id"]}, "studies": set()}
    envs = {r["env_id"] for r in store.q("SELECT DISTINCT env_id FROM tsessions WHERE study_id=?", (sh["object_id"],))}
    return {"kind": "study", "id": sh["object_id"], "envs": envs, "studies": {sh["object_id"]}}


@app.middleware("http")
async def share_guard(request: Request, call_next):
    return await _share_guard(request, call_next)


@app.middleware("http")
async def timing(request: Request, call_next):
    """Request timings for /status (route templates, not concrete URLs)."""
    t0 = time.time()
    resp = await call_next(request)
    route = getattr(request.scope.get("route"), "path", None) or ("/static/*" if request.url.path.startswith("/static/") else "other")
    status.record(request.method, route, (time.time() - t0) * 1000, resp.status_code)
    return resp


def watchdog_scan():
    """Stall watchdog (run by the status sampler every minute): silent agents and client hosts, session phases and
    measurements that are long overdue. Returns (alerts, what was checked)."""
    t = now()
    alerts = []
    envs = store.q("SELECT * FROM environments WHERE destroyed_at IS NULL")
    active = {r["env_id"]: r for r in store.q("SELECT * FROM tsessions WHERE status='active'")}
    for e in envs:
        e = dict(e)
        silent = t - (e.get("last_seen") or 0)
        if e.get("agent_state") in ("stopped", "shutdown") and (e.get("agent_state_at") or 0) >= (e.get("last_seen") or 0):
            continue  # stopped on purpose
        s = active.get(e["id"])
        if silent > 300 and (s or silent < 86400):
            alerts.append({"key": f"agent-silent:{e['id']}", "kind": "agent-silent", "severity": "high" if s else "medium",
                           "env_id": e["id"], "session_id": s["id"] if s else None,
                           "message": f"{e['name']}: agent silent for {int(silent // 60)} min" + (f" during session #{s['id']}" if s else "")})
    hosts = store.q("SELECT h.*, e.name en FROM hosts h JOIN environments e ON e.id=h.env_id WHERE h.destroyed_at IS NULL AND e.destroyed_at IS NULL")
    for h in hosts:
        if h["env_id"] in active and t - (h["last_seen"] or 0) > 300:
            alerts.append({"key": f"client-silent:{h['id']}", "kind": "client-silent", "severity": "medium", "env_id": h["env_id"],
                           "message": f"{h['en']}: client host {h['name']} silent for {int((t - (h['last_seen'] or 0)) // 60)} min during session"})
    for env_id, s in active.items():
        e = next((x for x in envs if x["id"] == env_id), None)
        if not e:
            continue
        runs = store.q("SELECT status, started, ended FROM runs WHERE session_id=? AND status IN ('done','measuring') ORDER BY started", (s["id"],))
        done = [r for r in runs if r["status"] == "done" and r["started"] and r["ended"]]
        meas = sorted(r["ended"] - r["started"] for r in done)
        typ_meas = meas[len(meas) // 2] if meas else None
        starts = [r["started"] for r in done]
        gaps = sorted(b - a for a, b in zip(starts, starts[1:]))
        typ_cycle = gaps[len(gaps) // 2] if gaps else None
        act = session_activity(e, s["id"])
        name = f"{e['name']} session #{s['id']}"
        if act.get("source") == "agent" and act.get("stale"):
            over = int((t - (act.get("until") or t)) // 60)
            alerts.append({"key": f"phase-overdue:{s['id']}", "kind": "phase-overdue", "severity": "high", "env_id": env_id, "session_id": s["id"],
                           "message": f"{name}: '{act.get('phase')}' should have ended {over} min ago"})
        m = next((r for r in reversed(runs) if r["status"] == "measuring"), None)
        if m and typ_meas and t - m["started"] > 3 * typ_meas + 1800:
            alerts.append({"key": f"measure-overdue:{s['id']}", "kind": "measure-overdue", "severity": "high", "env_id": env_id, "session_id": s["id"],
                           "message": f"{name}: run measuring for {int((t - m['started']) // 60)} min (typical {int(typ_meas // 60)} min)"})
        last = max([r["started"] or 0 for r in runs] + [r["ended"] or 0 for r in runs] + [s["created"]])
        limit = max(3600, 3 * (typ_cycle or 0))
        silent = any(a["kind"] == "agent-silent" and a["env_id"] == env_id for a in alerts)  # the root cause is already reported
        stopped = e["agent_state"] in ("stopped", "shutdown") and (e["agent_state_at"] or 0) >= (e["last_seen"] or 0)
        if act.get("source") != "agent" and not m and not silent and t - last > limit:
            alerts.append({"key": f"no-progress:{s['id']}", "kind": "no-progress", "severity": "medium" if stopped else "high",
                           "env_id": env_id, "session_id": s["id"],
                           "message": f"{name}: no run started or ended for {int((t - last) // 60)} min (limit {int(limit // 60)} min)"
                                      + (" - the agent was stopped on purpose; close the session or restart the agent" if stopped else "")})
    return alerts, {"environments": len(envs), "client_hosts": len(hosts), "active_sessions": len(active)}




@app.get("/status", response_class=HTMLResponse)
def server_status(request: Request, h: int = 24):
    """Service health page for the admin: host trends, database probes, request timings, watchdog, learner."""
    u = auth.current_user(request, required=False)
    if not u:
        return RedirectResponse("/#/login", status_code=303)
    if u.get("share") or not u.get("is_admin"):
        return HTMLResponse("<p>The status page is for administrators.</p>", status_code=403)
    return HTMLResponse(status.html(max(1, min(int(h), 168))), headers={"Cache-Control": "no-cache"})


@app.get("/status.json")
def server_status_json(request: Request, h: int = 24):
    auth.require_admin(request)
    return status.report(max(1, min(int(h), 168)))


async def _share_guard(request: Request, call_next):
    """Requests with a share token: GET only, only the pages a shared object needs, only inside its scope."""
    if request.headers.get("x-share-token") and request.url.path.startswith("/api/"):
        sh = auth.share_of(request)
        if not sh:
            return JSONResponse({"detail": "this share link was revoked or does not exist"}, status_code=401)
        if request.method != "GET" or not SHARE_PATHS.match(request.url.path):
            return JSONResponse({"detail": "read-only shared view"}, status_code=403)
        store.ex("UPDATE shares SET last_used=?, uses=uses+1 WHERE id=?", (time.time(), sh["id"]))
        tok = SHARE.set(share_scope(sh))
        try:
            return await call_next(request)
        finally:
            SHARE.reset(tok)
    return await call_next(request)


def in_scope(env_id=None, study_id=None):
    sc = SHARE.get()
    if sc is None:
        return True
    if study_id is not None:
        return study_id in sc["studies"]
    return env_id in sc["envs"]

VOLATILE = {"gtid_executed", "gtid_purged", "timestamp", "pseudo_thread_id", "rand_seed1", "rand_seed2", "last_insert_id",
            "insert_id", "identity", "warning_count", "error_count", "gtid_owned", "innodb_buffer_pool_resize_status"}


def now():
    return time.time()


# ---------------------------------------------------------------------------- helpers

def own_env(env_id, user_id):
    e = store.q1("SELECT * FROM environments WHERE id=? AND user_id=?", (env_id, user_id))
    if not e or not in_scope(env_id=e["id"]):
        raise HTTPException(404, "environment not found")
    return dict(e)


def own_session(session_id, user_id):
    s = store.q1("SELECT s.* FROM tsessions s JOIN environments e ON e.id=s.env_id WHERE s.id=? AND e.user_id=?", (session_id, user_id))
    if not s or not in_scope(env_id=s["env_id"]):
        raise HTTPException(404, "session not found")
    return dict(s)


def own_run(run_id, user_id):
    r = store.q1("SELECT r.*, s.env_id AS _env FROM runs r JOIN tsessions s ON s.id=r.session_id JOIN environments e ON e.id=s.env_id "
                 "WHERE r.id=? AND e.user_id=?", (run_id, user_id))
    if not r or not in_scope(env_id=r["_env"]):
        raise HTTPException(404, "run not found")
    return dict(r)


FLAVOR_NAMES = {"percona": "Percona Server", "mysql": "MySQL", "mariadb": "MariaDB", "pxc": "Percona XtraDB Cluster"}


AUTO_ARCHIVE_S = 3 * 86400  # no contact for this long and no active session: archived automatically (back on the next report)


def env_archived(e):
    """(archived, how): 'manual' (archived by its owner, or the machine deleted by a study script), 'auto' (no contact
    for AUTO_ARCHIVE_S and no active session: a deleted machine cannot say so), or (False, None)."""
    if e["destroyed_at"]:
        return True, "manual"
    hold = e["archive_hold"] if "archive_hold" in e.keys() else None
    if not hold and now() - (e["last_seen"] or e["created"] or 0) > AUTO_ARCHIVE_S \
            and not store.q1("SELECT 1 FROM tsessions WHERE env_id=? AND status='active'", (e["id"],)):
        return True, "auto"
    return False, None


def env_brief(e):
    f = store.uj(e["facts"], {}) or {}
    archived, how = env_archived(e)
    hw, db = f.get("hardware", {}), f.get("db", {})
    return {
        "id": e["id"], "name": e["name"], "hostname": e["hostname"], "agent_version": e["agent_version"],
        "created": e["created"], "last_seen": e["last_seen"], "online": bool(e["last_seen"] and now() - e["last_seen"] < ONLINE_S),
        "db": f"{db.get('version_comment', '')} {db.get('version', '')}".strip(), "flavor": db.get("flavor"),
        "db_short": f"{FLAVOR_NAMES.get(db.get('flavor'), db.get('flavor') or '')} {str(db.get('version', '')).split('-log')[0]}".strip(),
        "cpus": hw.get("cpus"), "ram_bytes": hw.get("ram_bytes"), "disk": "HDD" if hw.get("disk_rotational") else "SSD",
        "data_bytes": db.get("data_bytes"), "cost_month": e["cost_month"] if "cost_month" in e.keys() else None,
        **{k: (e[k] if k in e.keys() else None) for k in ("cost_hourly", "provisioned_at", "destroyed_at", "plan", "agent_state", "agent_state_at")},
        "cpu_model": hw.get("cpu_model"), "archived": archived, "archived_how": how,
        "archived_at": e["destroyed_at"] if how == "manual" else (e["last_seen"] or e["created"] or 0) + AUTO_ARCHIVE_S if how == "auto" else None,
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
    if u.get("share"):
        sh = u["share"]
        obj = store.q1("SELECT name FROM environments WHERE id=?" if sh["kind"] == "environment" else "SELECT name FROM studies WHERE id=?",
                       (sh["object_id"],))
        return {"user": {"email": "shared view", "name": None, "share": {"kind": sh["kind"], "id": sh["object_id"],
                                                                         "name": obj["name"] if obj else None, "label": sh["label"]}},
                "server": PUBLIC_URL}
    return {"user": {"id": u["id"], "email": u["email"], "name": u["name"], "is_admin": bool(u.get("is_admin"))}, "server": PUBLIC_URL}


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
    return {**llm.settings(), "service": True, "admin": bool(u.get("is_admin")), "modes": llm.MODES, "default_model": llm.DEFAULT_MODEL,
            "usage": llm.usage_summary(u["id"])}


@app.put("/api/settings/llm")
def put_llm_settings(request: Request, body: dict = Body(...)):
    u = auth.require_admin(request)  # the service's AI key and settings belong to the admin account
    if body.get("mode") and body["mode"] not in llm.MODES:
        raise HTTPException(400, "unknown mode")
    # one service row holds the AI settings (llm._service_row): every admin edits that row, not their own
    row = llm._service_row()
    tid = row["id"] if row else u["id"]
    if body.get("key"):
        ok, info = llm.verify_key(body["key"].strip())
        if not ok:
            raise HTTPException(400, f"OpenRouter rejected the key ({info.get('error')})")
        store.ex("UPDATE users SET llm_key_enc=? WHERE id=?", (llm.encrypt(body["key"].strip()), tid))
    if body.get("remove_key"):
        store.ex("UPDATE users SET llm_key_enc=NULL WHERE id=?", (tid,))
    if body.get("model"):
        store.ex("UPDATE users SET llm_model=? WHERE id=?", (body["model"][:120], tid))
    if body.get("mode"):
        store.ex("UPDATE users SET llm_mode=? WHERE id=?", (body["mode"], tid))
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
        if not in_scope(env_id=e["id"]):
            continue
        b = env_brief(e)
        s = store.q1("SELECT COUNT(*) n, MAX(created) last FROM tsessions WHERE env_id=?", (e["id"],))
        b["sessions"] = s["n"]
        act = store.q1("SELECT id, name, goal FROM tsessions WHERE env_id=? AND status='active' ORDER BY id DESC LIMIT 1", (e["id"],))
        b["active_session"] = dict(act, activity=session_activity(e, act["id"])) if act else None
        m = store.q1("SELECT data FROM metrics WHERE env_id=? ORDER BY ts DESC LIMIT 1", (e["id"],))
        b["latest"] = store.uj(m["data"]) if m else None
        b["clients"] = [{"id": h["id"], "name": h["name"], "online": bool(h["last_seen"] and now() - h["last_seen"] < ONLINE_S)}
                        for h in store.q("SELECT id, name, last_seen FROM hosts WHERE env_id=? AND destroyed_at IS NULL ORDER BY name", (e["id"],))]
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
        done = [r for r in runs if r["goal_value"] is not None and r["verdict"] not in ("unreliable", "superseded")]
        if done:
            first = done[0]["goal_value"]
            best = (min if s["goal"] == "latency" else max)(r["goal_value"] for r in done)
            d["first_value"], d["best_value"] = first, best
            if first:
                d["best_gain_pct"] = round(100 * ((first - best) / first if s["goal"] == "latency" else (best - first) / first), 2)
        sessions.append(d)
    events = [store.row(r, ("data",)) for r in store.q("SELECT * FROM events WHERE env_id=? ORDER BY ts DESC LIMIT 50", (env_id,))]
    hosts = [host_brief(dict(h)) for h in store.q("SELECT * FROM hosts WHERE env_id=? ORDER BY name", (env_id,))]
    act = next((x for x in sessions if x["status"] == "active"), None)
    if act:
        activity = dict(session_activity(e, act["id"]), session_id=act["id"], session_name=act["name"])
    else:  # outside sessions: e.g. data loading reported by a setup script, if recent
        a = store.uj(e.get("activity")) if e.get("activity") else None
        activity = dict(a, at=e["activity_at"], source="agent") if a and e.get("activity_at") and now() - e["activity_at"] < 6 * 3600 and a.get("phase") != "idle" else None
    return {"environment": env_brief(e), "facts": store.uj(e["facts"], {}), "sessions": sessions, "events": events, "hosts": hosts,
            "activity": activity, "steal": steal_profile(env_id)}


@app.post("/api/shares")
def create_share(request: Request, body: dict = Body(...)):
    """Read-only link to an environment or a study (with all its environments), usable without an account."""
    import secrets
    u = auth.current_user(request)
    kind = body.get("kind")
    oid = int(body.get("id") or 0)
    if kind == "environment":
        own_env(oid, u["id"])
    elif kind == "study":
        own_study(oid, u["id"])
    else:
        raise HTTPException(400, "kind must be environment or study")
    token = secrets.token_urlsafe(24)
    sid = store.ex("INSERT INTO shares(user_id, token, kind, object_id, label, created) VALUES (?,?,?,?,?,?)",
                   (u["id"], token, kind, oid, (body.get("label") or "")[:120] or None, now()))
    return {"id": sid, "token": token, "url": f"{PUBLIC_URL}/#/s/{token}"}


@app.get("/api/shares")
def list_shares(request: Request, kind: str = None, id: int = None):
    u = auth.current_user(request)
    q, args = "SELECT * FROM shares WHERE user_id=? AND revoked IS NULL", [u["id"]]
    if kind and id:
        q += " AND kind=? AND object_id=?"; args += [kind, id]
    return {"shares": [dict(r, url=f"{PUBLIC_URL}/#/s/{r['token']}") for r in store.q(q + " ORDER BY id DESC", args)]}


@app.delete("/api/shares/{share_id}")
def revoke_share(share_id: int, request: Request):
    u = auth.current_user(request)
    store.ex("UPDATE shares SET revoked=? WHERE id=? AND user_id=?", (now(), share_id, u["id"]))
    return {"ok": True}


@app.post("/api/llm/replay")
def llm_replay(request: Request, body: dict = Body(...)):
    """Audit: re-ask a stored LLM call with another model and/or a patched context (operator only: it spends the service key)."""
    u = auth.require_admin(request)
    try:
        return llm.replay(u["id"], int(body["call_id"]), body.get("model"), body.get("context"), body.get("patch"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/hosts/{host_id}")
def update_host(host_id: int, request: Request, body: dict = Body(...)):
    u = auth.current_user(request)
    h = store.q1("SELECT * FROM hosts WHERE id=? AND user_id=?", (host_id, u["id"]))
    if not h:
        raise HTTPException(404, "host not found")
    if "destroyed_at" in body:
        store.ex("UPDATE hosts SET destroyed_at=? WHERE id=?", (float(body["destroyed_at"]) if body["destroyed_at"] else None, host_id))
    return {"ok": True}


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
    if "archived" in body:  # Archive / Unarchive: unarchiving also holds off the automatic archive until the agent reports again
        store.ex("UPDATE environments SET destroyed_at=?, archive_hold=? WHERE id=?",
                 (now() if body["archived"] else None, None if body["archived"] else 1, env_id))
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
    s["activity"] = session_activity(e, sid) if s.get("status") == "active" else None
    if s["calibration"] and not s["calibration"].get("plan"):
        s["calibration"]["plan"] = noise_breakdown(sid, e["id"], s["calibration"])
    if s["calibration"] and not s["calibration"].get("data_state"):
        s["calibration"]["data_state"] = data_state_derived(sid, s["calibration"])
    st = store.q1("SELECT id, name, kind FROM studies WHERE id=?", (s.get("study_id"),)) if s.get("study_id") else None
    return {"session": s, "environment": env_brief(e), "runs": runs, "recommendations": recs, "events": events, "cost": session_costs(s, e),
            "study": dict(st) if st else None, "requirements": session_requirements(sid), "benchmarks": session_benchmarks(sid)}


def session_benchmarks(sid):
    """The benchmark(s) a session ran, as recorded by the runs: command as executed, reset, phases, profile file and the
    scripts it calls (agents from 2026-10-22; older runs give command / reset / phases). Runs with the same specification
    are grouped, so a mid-session change (e.g. a lengthened warm-up) shows as a second entry with its runs."""
    out, by = [], {}
    for r in store.q("SELECT seq, json_extract(inputs, '$.bench') b FROM runs WHERE session_id=? AND inputs IS NOT NULL ORDER BY seq", (sid,)):
        b = store.uj(r["b"]) if r["b"] else None
        if not b:
            continue
        key = b.get("spec_sha") or json.dumps([b.get("command") or b.get("run"), b.get("reset"), b.get("warmup"), b.get("duration")])
        if key not in by:
            spec = {k: b.get(k) for k in ("name", "profile", "command", "run", "reset", "warmup", "duration", "phases", "goal_metric",
                                          "data_state", "requires", "profile_text", "scripts", "spec_sha", "restart_before_run", "observed")}
            by[key] = dict(spec, runs=[])
            out.append(by[key])
        by[key]["runs"].append(r["seq"])
    return out


_nb_cache = {}


def noise_breakdown(sid, env_id, cal):
    """For sessions calibrated by older agents: split the calibration noise into within-run and between-run parts from
    the stored 10 s samples (batch means of 60 s blocks), the same split the agent's noise plan makes."""
    seqs = [int(x) for x in (cal.get("runs") or [])]
    cv = cal.get("goal_cv_pct")
    ck = (sid, tuple(seqs), cv)
    if ck in _nb_cache:
        return _nb_cache[ck]
    out = None
    if seqs and cv is not None:
        ses, dur = [], []
        for r in store.q(f"SELECT started, ended FROM runs WHERE session_id=? AND seq IN ({','.join('?' * len(seqs))})", (sid, *seqs)):
            if not (r["started"] and r["ended"]):
                continue
            vals = []
            for m in store.q("SELECT data FROM metrics WHERE env_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (env_id, r["started"], r["ended"])):
                d = (store.uj(m["data"]) or {}).get("db") or {}
                v = d.get("tps") or d.get("qps")
                if v:
                    vals.append(v)
            k = len(vals) // 6
            if k >= 3:
                bm = [sum(vals[i * 6:(i + 1) * 6]) / 6 for i in range(k)]
                mu = sum(bm) / k
                ses.append(100 * (sum((x - mu) ** 2 for x in bm) / (k - 1)) ** 0.5 / k ** 0.5 / mu)
                dur.append(r["ended"] - r["started"])
        if ses:
            within = (sum(x * x for x in ses) / len(ses)) ** 0.5
            between = max(0.0, cv * cv - within * within) ** 0.5
            target, K = 3.0, 2.5
            out = {"within_cv_pct": round(within, 2), "between_cv_pct": round(between, 2), "target_pct": target,
                   "noise_pct": cal.get("noise_pct"), "run_seconds": int(sum(dur) / len(dur)), "derived": True}
            if (cal.get("noise_pct") or 0) <= target:
                out["action"] = "ok"
            elif between >= 0.8 * target / K or within <= between:
                out.update(action="between-runs", repeats=min(9, math.ceil((K * between / target) ** 2)))
            else:
                need = ((target / K) ** 2 - between ** 2) ** 0.5
                out.update(action="longer-runs", seconds=int(math.ceil(out["run_seconds"] * (within / need) ** 2 / 60) * 60))
    _nb_cache[ck] = out
    return out


def data_state_derived(sid, cal):
    """Older sessions: does the benchmark write while its profile has no reset? (from the calibration runs)"""
    seqs = [int(x) for x in (cal.get("runs") or [])]
    if not seqs:
        return None
    rows = store.q(f"SELECT goal_value, inputs FROM runs WHERE session_id=? AND seq IN ({','.join('?' * len(seqs))}) ORDER BY seq", (sid, *seqs))
    w, reset, vals = [], None, []
    for r in rows:
        inp = store.uj(r["inputs"]) or {}
        b = inp.get("bench") or {}
        reset = reset or b.get("reset") or (b.get("data_state") in ("reset", "seasoned", "read-only") and b.get("data_state"))
        sr = inp.get("status_rates") or {}
        w.append(sum(float(sr.get(k) or 0) for k in ("Innodb_rows_inserted", "Innodb_rows_updated", "Innodb_rows_deleted")))
        if r["goal_value"] is not None:
            vals.append(r["goal_value"])
    if not rows or not any(store.uj(r["inputs"]) for r in rows):
        return None
    writes = sum(w) / len(w) > 1
    t = None
    if len(vals) >= 3:
        n, mx, my = len(vals), (len(vals) - 1) / 2, sum(vals) / len(vals)
        t = 100 * sum((i - mx) * (v - my) for i, v in enumerate(vals)) / sum((i - mx) ** 2 for i in range(n)) / my
    return {"writes": writes, "rows_changed_per_s": round(sum(w) / len(w), 1), "trend_pct_per_run": round(t, 2) if t is not None else None,
            "status": "drifting" if writes and not reset else "ok", "derived": True}


def session_requirements(sid):
    """Benchmark prerequisites of the session (settings the benchmark needs to run at all), from its latest run."""
    r = store.q1("SELECT inputs FROM runs WHERE session_id=? AND inputs IS NOT NULL AND inputs LIKE '%\"requirements\"%' ORDER BY seq DESC LIMIT 1", (sid,))
    if not r:
        return []
    b = (store.uj(r["inputs"]) or {}).get("bench") or {}
    return b.get("requirements") or []


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
    # the warm-up before the measured window: the agent records what it waited (and any delay before the window); older
    # runs fall back to the profile's warm-up setting
    w = store.q1("SELECT json_extract(inputs, '$.bench.warmup_s') ws, json_extract(inputs, '$.bench.warmup') wp, "
                 "json_extract(inputs, '$.bench.window_start_delay_s') wd FROM runs WHERE id=?", (run_id,))
    warm = None
    try:
        secs = float(w["ws"]) if w and w["ws"] is not None else float(w["wp"]) if w and w["wp"] not in (None, "", "auto") else 0
        if secs > 0 and r["started"]:
            warm = [r["started"] - secs - max(0.0, float(w["wd"] or 0)), r["started"]]
    except (TypeError, ValueError):
        warm = None
    t0 = (warm[0] if warm else (r["started"] or now())) - pad
    t1 = (r["ended"] or now()) + (pad if r["ended"] else 0)
    m = metrics_window(s["env_id"], t0, t1, points)
    m["window"] = [r["started"], r["ended"]]
    m["warmup"] = warm
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
    c.execute("UPDATE environments SET last_seen=?, agent_state='running', archive_hold=NULL WHERE id=?", (now(), e["id"]))
    c.execute("COMMIT")
    return {"ok": True, "stored": len(rows)}


@app.post("/api/v1/agent/activity")
def agent_activity(body: dict = Body(...), key=Depends(auth.agent_key)):
    e = agent_env(body.get("environment_id"), key)
    act = {k: body.get(k) for k in ("phase", "detail", "until", "session_id")}
    act["phase"] = str(act.get("phase") or "")[:60]
    act["detail"] = str(act.get("detail") or "")[:200]
    store.ex("UPDATE environments SET activity=?, activity_at=? WHERE id=?", (store.j(act), now(), e["id"]))
    return {"ok": True}


_STEAL_CACHE = {}


def steal_profile(env_id):
    """Normal CPU steal of an environment: distribution of the 10 s samples of the last 7 days (cached 10 min)."""
    hit = _STEAL_CACHE.get(env_id)
    if hit and now() - hit[0] < 600:
        return hit[1]
    vals = sorted(r[0] for r in store.q("SELECT json_extract(data, '$.os.cpu_steal') FROM metrics WHERE env_id=? AND ts>?",
                                        (env_id, now() - 7 * 86400)) if r[0] is not None)
    out = None
    if len(vals) >= 30:
        q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
        out = {"samples": len(vals), "mean": round(sum(vals) / len(vals), 3), "p50": round(q(0.5), 2), "p99": round(q(0.99), 2),
               "max": round(vals[-1], 2), "pct_over_1": round(100 * sum(v > 1 for v in vals) / len(vals), 2)}
    _STEAL_CACHE[env_id] = (now(), out)
    return out


def session_activity(env, session_id):
    """What an active session is doing: the agent's own report when fresh, else derived from its latest run."""
    env = dict(env)
    act = store.uj(env.get("activity")) if env.get("activity") else None
    if act and act.get("session_id") in (session_id, None) and env.get("activity_at") and now() - env["activity_at"] < 6 * 3600:
        act = dict(act, at=env["activity_at"], source="agent")
        until = act.get("until")
        if until and now() > until + 900:
            act["stale"] = True  # the phase should have ended 15+ min ago without a new report
        return act
    r = store.q1("SELECT seq, label, status, started, ended FROM runs WHERE session_id=? ORDER BY seq DESC, id DESC LIMIT 1", (session_id,))
    if not r:
        return {"phase": "preparing", "detail": "no run yet", "source": "derived", "at": None}
    if r["status"] == "measuring":
        return {"phase": "measuring", "detail": f"run #{r['seq']} {r['label']}", "since": r["started"], "source": "derived"}
    return {"phase": "between runs", "detail": f"run #{r['seq']} ended (reset, warm-up or applying the next change)", "since": r["ended"],
            "source": "derived"}


@app.post("/api/v1/agent/bye")
def agent_bye(body: dict = Body(...), key=Depends(auth.agent_key)):
    """The agent daemon was stopped on purpose: a service stop ('agent stopped') or the host shutting down ('host shut
    down': a powered-off or deleted machine), instead of 'offline'."""
    e = agent_env(body.get("environment_id"), key)
    store.ex("UPDATE environments SET agent_state=?, agent_state_at=? WHERE id=?",
             ("shutdown" if body.get("host_shutdown") else "stopped", now(), e["id"]))
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
    # results say something about the engine build that produced them: an agent older than the server's build is
    # warned (the harness updates at the earliest opportunity) and the session is recorded as run on a stale agent (the
    # learner weighs it like pre-fix evidence) - never refused: a deploy must not break anyone's next session
    have = str(body.get("agent_version") or "").partition("+")[2]
    want = _agent_build()
    stale = want not in ("", "unknown") and have != want
    warnings = [f"agent build {have or 'unknown'} differs from the server's {want}: update the agent at the earliest "
                f"opportunity (curl -fsSL {PUBLIC_URL}/install.sh | bash); this session is recorded as run on an outdated agent"] if stale else []
    # the host's clock: run windows, metric samples and event times come from it; a wrong clock misplaces all of them
    # (a user found a box hours off). A few seconds: a warning; over a minute: refused until the clock is fixed.
    if body.get("agent_time") is not None:
        skew = float(body["agent_time"]) - now()
        fix = "enable time synchronisation (timedatectl set-ntp true; chrony / systemd-timesyncd) and check the time zone settings"
        ntp = "" if body.get("ntp_synchronized") is not False else " The host reports its clock as not synchronised."
        if abs(skew) > 60:
            raise HTTPException(400, f"the host's clock is {skew:+.0f} s off the server's: {fix}, then start the session again.{ntp}")
        if abs(skew) > 2:
            warnings.append(f"the host's clock is {skew:+.1f} s off the server's: {fix}.{ntp}")
    warning = " | ".join(warnings) or None
    goal = body.get("goal") if body.get("goal") in ("tps", "latency") else "tps"
    store.ex("UPDATE tsessions SET status='abandoned', ended=? WHERE env_id=? AND status='active'", (now(), e["id"]))
    n = store.q1("SELECT COUNT(*) n FROM tsessions WHERE env_id=?", (e["id"],))["n"] + 1
    name = (body.get("name") or f"Session {n} ({'TPS' if goal == 'tps' else 'latency'})")[:120]
    sid = store.ex("INSERT INTO tsessions(env_id, name, goal, policy, noise_pct, workload, notes, created, os_tuning, agent_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (e["id"], name, goal, body.get("policy") or "safe", body.get("noise_pct") or 2.0, body.get("workload"), body.get("notes"), now(),
                    1 if body.get("os_tuning", True) else 0,
                    ((str(body.get("agent_version") or "")[:40] + (" stale" if want not in ("", "unknown") and have != want else "")) or None)))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message, data) VALUES (?,?,?,?,?,?)",
             (e["id"], sid, now(), "session", f"session started: goal={goal}, policy={body.get('policy') or 'safe'}", store.j(body)))
    study = None
    if body.get("study"):
        study = study_get_or_create(key["user_id"], body["study"], goal, body.get("workload"))
        store.ex("UPDATE tsessions SET study_id=? WHERE id=?", (study["id"], sid))
    return {"session_id": sid, "name": name, "url": f"{PUBLIC_URL}/#/session/{sid}", "warning": warning,
            "study": {"id": study["id"], "name": study["name"], "url": f"{PUBLIC_URL}/#/study/{study['id']}"} if study else None}


@app.post("/api/v1/agent/sessions/{sid}/close")
def agent_session_close(sid: int, body: dict = Body(default={}), key=Depends(auth.agent_key)):
    s = agent_session(sid, key)
    store.ex("UPDATE tsessions SET status='closed', ended=?, summary=? WHERE id=?", (now(), store.j(body.get("summary")), sid))
    store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)", (s["env_id"], sid, now(), "session", "session closed"))
    _clear_activity(s["env_id"], sid)
    return {"ok": True}


def _clear_activity(env_id, sid):
    """A closed session leaves nothing running on the dashboard (a finished autotune once left 'measuring' behind)."""
    e = store.q1("SELECT activity FROM environments WHERE id=?", (env_id,))
    act = json.loads(e["activity"]) if e and e["activity"] else {}
    if not act or act.get("session_id") in (None, sid):
        store.ex("UPDATE environments SET activity=?, activity_at=? WHERE id=?",
                 (store.j({"phase": "idle", "detail": f"session #{sid} closed", "session_id": None}), now(), env_id))


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


CONC_CLASSES = ("low", "medium", "high", "extreme")


def conc_class(n, cpus=None):
    """Client concurrency relative to the server's vCPUs - contention effects (latches, thread pool, spin settings,
    scheduling) depend on connections per CPU, not on the count: low ~0.5x (CPU left for background work and the client),
    medium ~2x (some waiting), high ~16x (contention exposed), extreme beyond 64x (thread handling: thousands of
    connections). Class boundaries sit between those points. None when either number is unknown."""
    n, c = int(float(n or 0)), int(cpus or 0)
    if not n or not c:
        return None
    r = n / c
    return "low" if r <= 1 else "medium" if r <= 6 else "high" if r <= 64 else "extreme"


def server_series(flavor, version):
    """'percona 8.4', 'mysql 9.7', 'mariadb 11.4': the variant and its release series (major.minor). Behaviour and
    optimal settings can differ between series and variants, so evidence is weighed by how close its server was."""
    if not flavor or not version:
        return None
    m = re.match(r"(\d+)\.(\d+)", str(version))
    return f"{str(flavor).lower()} {m.group(1)}.{m.group(2)}" if m else None


@app.get("/api/v1/agent/experience")
def agent_experience(workload: str = None, cpus: int = None, conns: int = None, exclude_session: int = None, numa_nodes: int = None,
                     flavor: str = None, version: str = None, cluster: str = None, engine: str = None, key=Depends(auth.agent_key)):
    """Outcomes of each recommendation in this account's earlier sessions with a similar workload, CPU class and concurrency,
    in tiers of server closeness: the same variant and release series, the same variant in other series, other variants."""
    rows = store.q(
        "SELECT r.changes, r.verdict, r.evaluation, json_extract(r.inputs, '$.workload.type') AS wl, "
        "json_extract(e.facts, '$.hardware.cpus') AS cpus, json_extract(r.summary, '$.threads_connected_avg') AS conns, r.session_id, "
        "json_extract(e.facts, '$.db.flavor') AS fl, json_extract(e.facts, '$.db.version') AS ver FROM runs r JOIN tsessions s ON s.id=r.session_id "
        "JOIN environments e ON e.id=s.env_id WHERE e.user_id=? AND r.status='done' AND r.verdict IN "
        "('improved','efficiency-gain','neutral','regressed','efficiency-loss')", (key["user_id"],))
    series = server_series(flavor, version)
    out, wide, hyp, hyp_wide = {}, {}, {}, {}
    family, other = {}, {}  # similar conditions on the same variant in another series / on another variant
    for r in rows:
        if exclude_session and r["session_id"] == exclude_session:
            continue
        similar = (not (workload and r["wl"] and r["wl"] != workload) and not (cpus and r["cpus"] and cpu_class(r["cpus"]) != cpu_class(cpus))
                   and not (conns and r["conns"] and conc_class(r["conns"], r["cpus"]) != conc_class(conns, cpus)))
        _experience_add(wide, r)  # every workload and machine size: a weaker prior when similar sessions are few
        _hypothesis_add(hyp_wide, r)
        if not similar:
            continue
        rs = server_series(r["fl"], r["ver"])
        if series and rs and rs != series:
            _experience_add(family if (r["fl"] or "").lower() == str(flavor).lower() else other, r)
            continue
        _experience_add(out, r)
        _hypothesis_add(hyp, r)
    for d in (out, wide, family, other):
        for e in d.values():
            x = e.pop("deltas")
            e["mean_delta_pct"] = round(sum(x) / len(x), 2) if x else None
    try:  # learnings (learning.db) that hold in these conditions: leads and priors, never a replacement for evidence
        insights = learner.matching(workload, cpu_class(cpus) if cpus else None, conc_class(conns, cpus),
                                    ("multi-node" if numa_nodes > 1 else "uniform") if numa_nodes else None, series, cluster, engine)
        params = learner.param_impact(workload, cpu_class(cpus) if cpus else None, series=series)
    except Exception:
        insights, params = [], {}
    return {"workload": workload, "cpu_class": cpu_class(cpus) if cpus else None, "concurrency_class": conc_class(conns, cpus),
            "series": series, "experience": out, "experience_family": family, "experience_other_variant": other, "experience_all": wide,
            "hypotheses": list(hyp.values()), "hypotheses_all": list(hyp_wide.values()),
            "insights": insights, "parameters": params}


def learning_data():
    """Everything the tuner has learned across sessions (all accounts): outcomes of each idea by workload, CPU class
    and concurrency class; hypothesis outcomes by the component they targeted; cached LLM bottleneck analyses. This is
    what agents receive (per account) through /experience; here it is shown whole for review."""
    rows = store.q(
        "SELECT r.changes, r.verdict, r.evaluation, json_extract(r.inputs, '$.workload.type') AS wl, "
        "json_extract(e.facts, '$.hardware.cpus') AS cpus, json_extract(r.summary, '$.threads_connected_avg') AS conns, r.session_id "
        "FROM runs r JOIN tsessions s ON s.id=r.session_id JOIN environments e ON e.id=s.env_id WHERE r.status='done' AND r.verdict IN "
        "('improved','efficiency-gain','neutral','regressed','efficiency-loss')")
    ctx, hyp = {}, {}
    sessions = {}
    for r in rows:
        key = (r["wl"] or "-", cpu_class(r["cpus"]) if r["cpus"] else "-", conc_class(r["conns"], r["cpus"]) or "-")
        d = ctx.setdefault(key, {})
        before = {k: v["n"] for k, v in d.items()}
        _experience_add(d, r)
        for rid, e in d.items():
            if e["n"] != before.get(rid, 0):
                sessions.setdefault((key, rid), set()).add(r["session_id"])
        _hypothesis_add(hyp, r)
    experience = []
    for (wl, cc, kc), d in ctx.items():
        for rid, e in d.items():
            x = e["deltas"]
            experience.append({"idea": rid, "workload": wl, "cpu_class": cc, "concurrency_class": kc, "n": e["n"], "wins": e["wins"],
                               "losses": e["losses"], "neutral": e["neutral"], "mean_delta_pct": round(sum(x) / len(x), 2) if x else None,
                               "sessions": sorted(sessions.get(((wl, cc, kc), rid), ()))})
    experience.sort(key=lambda e: (e["idea"], -e["n"]))
    an = store.q("SELECT prompt_hash, MIN(id) first_id, MIN(session_id) first_session, COUNT(*) uses, SUM(cached) reuses, "
                 "MAX(signature) signature, MAX(CASE WHEN cached=0 THEN response END) response, MAX(model) model "
                 "FROM llm_calls WHERE purpose='analyze' AND status='ok' GROUP BY prompt_hash ORDER BY first_id")
    analyses = []
    for a in an:
        resp, sig = store.uj(a["response"], {}) or {}, store.uj(a["signature"], {}) or {}
        analyses.append({"component": sig.get("component"), "conditions": {k: sig.get(k) for k in ("flavor", "version", "workload", "goal", "cpus", "disk", "settings")},
                         "frames": sig.get("frames"), "hypothesis": resp.get("hypothesis"), "confirm_by": resp.get("confirm_by"),
                         "changes": [{"title": c.get("title"), "changes": c.get("changes"), "mechanism": c.get("mechanism")} for c in resp.get("changes") or []],
                         "model": a["model"], "first_session": a["first_session"], "uses": a["uses"], "reuses": a["reuses"] or 0})
    return {"runs": len(rows), "experience": experience, "hypotheses": sorted(hyp.values(), key=lambda h: (h["id"], -h["n"])),
            "llm_analyses": analyses}


@app.get("/api/admin/learning")
def admin_learning(request: Request):
    auth.require_admin(request)
    d = learning_data()
    d["snapshots"] = [dict(r) for r in store.q("SELECT id, created, label, runs FROM learning_snapshots ORDER BY id DESC")]
    return d


@app.get("/api/parameters")
def parameters_catalog(request: Request, workload: str = None):
    """The parameter catalog the tuner works with: shipped knowledge (impact prior, when it counts, precondition,
    safety, restart) and, where enough experiments exist, the measured impact from the learnings."""
    auth.current_user(request)
    try:
        with open(os.path.join(AGENT_DIR, "tuning_agent", "knowledge_data", "parameters.json")) as f:
            cat = json.load(f).get("parameters") or {}
    except (OSError, ValueError):
        cat = {}
    try:
        learned_all = learner.param_impact()
        learned_wl = {}
        for wl in sorted({r["workload"] for r in learner.db().execute("SELECT DISTINCT workload FROM param_evidence") if r["workload"]}):
            for var, v in learner.param_impact(wl).items():
                if v["impact"] != "unknown":
                    learned_wl.setdefault(var, {})[wl] = v
    except Exception:
        learned_all, learned_wl = {}, {}
    u = auth.current_user(request)
    pol = param_policies(u["id"])
    out = []
    for name, p in sorted(cat.items()):
        lv = learned_all.get(name)
        out.append(dict(p, name=name, learned=lv, learned_by_workload=learned_wl.get(name),
                        effective_impact=lv["impact"] if lv and lv["impact"] != "unknown" else p.get("impact"), **pol.get(name, _POL0)))
    try:
        with open(os.path.join(AGENT_DIR, "tuning_agent", "knowledge_data", "server_variables.json")) as f:
            series = sorted(json.load(f).get("series") or {}, key=lambda s: (s.split()[0], [int(x) for x in s.split()[1].split(".")]))
    except (OSError, ValueError):
        series = []
    return {"parameters": out, "admin": bool(u.get("is_admin")), "series": series}


SERVICE = 0  # param_policy.user_id of the service defaults set by the admin
_POL0 = {"default_mode": "tune", "user_mode": None, "mode": "tune", "default_note": None, "user_note": None}


def param_policies(user_id):
    """{variable: {default_mode, user_mode, mode, notes}}: whether the tuner may change each setting for this user. The
    admin's service default applies unless the user set their own."""
    out = {}
    for r in store.q("SELECT * FROM param_policy WHERE user_id IN (?, ?)", (SERVICE, user_id)):
        e = out.setdefault(r["variable"], dict(_POL0))
        if r["user_id"] == SERVICE:
            e["default_mode"], e["default_note"] = r["mode"], r["note"]
        else:
            e["user_mode"], e["user_note"] = r["mode"], r["note"]
    for e in out.values():
        e["mode"] = e["user_mode"] or e["default_mode"]
    return out


def _set_policy(user_id, variable, mode, note):
    if mode not in ("tune", "block", "default"):
        raise HTTPException(400, "mode: tune | block | default")
    if mode == "default":
        store.ex("DELETE FROM param_policy WHERE user_id=? AND variable=?", (user_id, variable[:120]))
    else:
        store.ex("INSERT INTO param_policy(user_id, variable, mode, note, updated) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(user_id, variable) DO UPDATE SET mode=excluded.mode, note=excluded.note, updated=excluded.updated",
                 (user_id, variable[:120], mode, (note or "")[:500] or None, now()))


@app.put("/api/parameters/{variable}/policy")
def put_param_policy(variable: str, request: Request, body: dict = Body(...)):
    """A user's own list: tune or block a setting for their sessions ('default' = follow the service default)."""
    u = auth.current_user(request)
    if u.get("share"):
        raise HTTPException(403, "read-only shared view")
    _set_policy(u["id"], variable, body.get("mode"), body.get("note"))
    return param_policies(u["id"]).get(variable, _POL0)


@app.put("/api/admin/parameters/{variable}/policy")
def put_param_default(variable: str, request: Request, body: dict = Body(...)):
    """The service default every user starts from (admin only)."""
    auth.require_admin(request)
    _set_policy(SERVICE, variable, body.get("mode"), body.get("note"))
    return {"variable": variable, "default_mode": "tune" if body.get("mode") == "default" else body.get("mode")}


@app.get("/api/v1/agent/param-policy")
def agent_param_policy(key=Depends(auth.agent_key)):
    """Settings the tuner must not change for this account (user list over the service defaults)."""
    pol = param_policies(key["user_id"])
    return {"blocked": sorted(v for v, e in pol.items() if e["mode"] == "block"),
            "notes": {v: e["user_note"] or e["default_note"] for v, e in pol.items() if e["mode"] == "block"}}


@app.get("/api/admin/learner")
def admin_learner(request: Request):
    auth.require_admin(request)
    return learner.overview()


@app.post("/api/admin/learner/run")
def admin_learner_run(request: Request, body: dict = Body(default={})):
    auth.require_admin(request)
    threading.Thread(target=learner.run_pass, args=("admin", body.get("max_sessions")), daemon=True).start()
    return {"started": True}


@app.post("/api/admin/learner/prune")
def admin_learner_prune(request: Request):
    auth.require_admin(request)
    learner.revalidate()
    return {"archived": learner.prune()}


@app.patch("/api/admin/insights/{iid}")
def admin_insight(iid: int, request: Request, body: dict = Body(...)):
    auth.require_admin(request)
    if body.get("status") not in ("observed", "repeated", "contested", "contradicted", "implemented", "retired-by-admin", "archived"):
        raise HTTPException(400, "status: observed | repeated | contested | contradicted | implemented | retired-by-admin | archived")
    learner.db().execute("UPDATE insights SET status=?, notes=COALESCE(?, notes), updated=? WHERE id=?",
                         (body["status"], body.get("notes"), now(), iid))
    return {"ok": True}


@app.patch("/api/admin/proposals/{pid}")
def admin_proposal(pid: int, request: Request, body: dict = Body(...)):
    auth.require_admin(request)
    if body.get("status") not in ("open", "accepted", "rejected", "done"):
        raise HTTPException(400, "status: open | accepted | rejected | done")
    learner.db().execute("UPDATE proposals SET status=? WHERE id=?", (body["status"], pid))
    return {"ok": True}


@app.post("/api/admin/learning/snapshots")
def admin_learning_snapshot(request: Request, body: dict = Body(default={})):
    u = auth.require_admin(request)
    d = learning_data()
    sid = store.ex("INSERT INTO learning_snapshots(created, created_by, label, runs, data) VALUES (?,?,?,?,?)",
                   (now(), u["id"], (body.get("label") or "")[:200], d["runs"], json.dumps(d)))
    return {"id": sid, "runs": d["runs"]}


@app.get("/api/admin/learning/snapshots/{sid}")
def admin_learning_snapshot_get(sid: int, request: Request):
    auth.require_admin(request)
    r = store.q1("SELECT * FROM learning_snapshots WHERE id=?", (sid,))
    if not r:
        raise HTTPException(404, "no such snapshot")
    return JSONResponse(json.loads(r["data"]), headers={"Content-Disposition": f'attachment; filename="aituner-learning-{sid}.json"'})


def _hypothesis_add(out, r):
    """Learning from hypotheses: how often 'change X acts on bottleneck component C' held. Keyed by the idea and the
    component it targeted, so the same setting can be credible for one bottleneck and not for another."""
    h = (store.uj(r["evaluation"], {}) or {}).get("hypothesis") or {}
    if h.get("outcome") in (None, "done") or not h.get("id"):
        return
    for tgt in (h.get("target") or ["-"])[:1]:
        e = out.setdefault(f"{h['id']}|{tgt}", {"id": h["id"], "target": tgt, "n": 0, "supported": 0, "moved": 0, "refuted": 0,
                                                 "improved-other-mechanism": 0})
        e["n"] += 1
        e[h["outcome"]] = e.get(h["outcome"], 0) + 1


def _experience_add(out, r):
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


@app.post("/api/v1/agent/llm/analyze")
def agent_llm_analyze(body: dict = Body(...), key=Depends(auth.agent_key)):
    s = agent_session(body.get("session_id"), key)
    res = llm.analyze(key["user_id"], s["env_id"], s["id"], body.get("context") or {}, body.get("signature") or {})
    if res.get("enabled") and not res.get("error"):
        comp = (body.get("signature") or {}).get("component")
        store.ex("INSERT INTO events(env_id, session_id, ts, type, message) VALUES (?,?,?,?,?)",
                 (s["env_id"], s["id"], now(), "llm", f"{res.get('model')}: bottleneck analysis of {comp}: {len(res.get('changes') or [])} change(s)"
                  + (f" (cached from session {res['cached_from']['session']})" if res.get("cached") else "")))
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
            "destroyed_at": h["destroyed_at"] if "destroyed_at" in h.keys() else None,
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


def _agent_build():
    """Git commit of the deployed tree, read from .git (the service user may not be allowed to run git on the checkout)."""
    try:
        g = os.path.join(AGENT_DIR, "..", ".git")
        head = open(os.path.join(g, "HEAD")).read().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            p = os.path.join(g, ref)
            if os.path.exists(p):
                return open(p).read().strip()[:7]
            for line in open(os.path.join(g, "packed-refs")):
                if line.strip().endswith(" " + ref):
                    return line.split()[0][:7]
            return "unknown"
        return head[:7]
    except Exception:
        return "unknown"


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
            build = _agent_build().encode()
            ti = tarfile.TarInfo("tuning-agent/tuning_agent/_build.py")
            body = b'BUILD = "' + build + b'"\n'
            ti.size, ti.mtime = len(body), int(time.time())
            t.addfile(ti, io.BytesIO(body))
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
    if not st or not in_scope(study_id=st["id"]):
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
                         "cost": session_costs(s, e), "env_cost": env_cost(e),
                         "activity": session_activity(e, s["id"]) if s["status"] == "active" else None})
    return sessions


@app.get("/api/studies")
def studies(request: Request):
    u = auth.current_user(request)
    out = []
    for st in store.q("SELECT * FROM studies WHERE user_id=? ORDER BY id DESC", (u["id"],)):
        if not in_scope(study_id=st["id"]):
            continue
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
    return {"study": st, "sessions": study_view(st, u["id"]), "other_sessions": [] if SHARE.get() else others}


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


# background threads start last: their first scan uses functions defined anywhere in this module (now(), ...)
status.set_watchdog(watchdog_scan)
status.start()
learner.start()  # background learner: observations -> learning.db (docs/design/agentic-saas.md)
