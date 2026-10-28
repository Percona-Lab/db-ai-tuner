"""Optional LLM assistance through OpenRouter.

The OpenRouter key is stored encrypted on the server and never sent to agents; agents ask the server, which calls the
model. Calls are cached by context hash, logged with tokens/cost, and gated: the server stops calling when LLM-sourced
experiments have not helped for a workload class.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from cryptography.fernet import Fernet, InvalidToken

from . import store

OPENROUTER = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
MODES = ("off", "when-needed", "always", "planner")
MAX_CALLS_PER_SESSION = 3
MAX_ANALYSES_PER_SESSION = 6
# the service key belongs to the operator: a budget per (non-admin) account and day for calls it pays for. Sessions are
# free to create, so per-session limits alone do not bound spend.
USER_DAILY_CALLS = 60
USER_DAILY_COST = 2.0  # USD
ANALYZE_PROMPT_VERSION = "1"


def _is_admin(user_id):
    r = store.q1("SELECT is_admin FROM users WHERE id=?", (user_id,))
    return bool(r and r["is_admin"])


def budget_left(user_id):
    """None when the account may call the model now, else the reason it may not."""
    if _is_admin(user_id):
        return None
    r = store.q1("SELECT COUNT(*) n, COALESCE(SUM(cost),0) c FROM llm_calls WHERE user_id=? AND cached=0 AND ts > ?",
                 (user_id, time.time() - 86400))
    if r["n"] >= USER_DAILY_CALLS:
        return f"daily limit of {USER_DAILY_CALLS} AI calls for this account reached"
    if r["c"] >= USER_DAILY_COST:
        return f"daily AI budget of ${USER_DAILY_COST:.2f} for this account reached"
    return None
GATE_MIN_TRIES, GATE_MAX_WINS = 4, 0  # after 4 LLM experiments with 0 wins in a workload class, stop asking


def _fernet():
    key = os.environ.get("TUNER_SECRET_KEY")
    if not key:
        raise RuntimeError("TUNER_SECRET_KEY is not configured on the server")
    return Fernet(key.encode())


def encrypt(s):
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s):
    try:
        return _fernet().decrypt(s.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _service_row():
    """The service's AI settings live on the admin account (set up with the service); users need no key of their own."""
    return store.q1("SELECT id, llm_key_enc, llm_model, llm_mode, llm_planner_model, llm_critic_model FROM users WHERE is_admin=1 "
                    "ORDER BY (llm_key_enc IS NULL), id LIMIT 1")


def settings(user_id=None):
    """Service-wide AI settings (the same for every user)."""
    r = _service_row()
    if not r:
        return {"configured": False, "model": DEFAULT_MODEL, "mode": "off", "planner_model": PLAN_MODEL, "critic_model": None}
    return {"configured": bool(r["llm_key_enc"]), "model": r["llm_model"] or DEFAULT_MODEL, "mode": r["llm_mode"] or "planner",  # default: rules + learnings + the AI planner
            "planner_model": r["llm_planner_model"] or PLAN_MODEL, "critic_model": r["llm_critic_model"] or None}


def service_key():
    r = _service_row()
    return decrypt(r["llm_key_enc"]) if r and r["llm_key_enc"] else None


_pool = None
DEADLINE_S = 240  # wall clock for one model call: OpenRouter keeps slow requests alive, so socket timeouts never fire


def _request(method, url, key, body=None, timeout=120, deadline=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Title", "AI DB Tuner")

    def go():
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    if not deadline:
        return go()
    global _pool
    if _pool is None:
        import concurrent.futures
        _pool = concurrent.futures.ThreadPoolExecutor(8, thread_name_prefix="llm")
    import concurrent.futures
    try:
        return _pool.submit(go).result(timeout=deadline)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"no answer within {deadline} s") from None


def verify_key(key):
    try:
        d = _request("GET", OPENROUTER + "/key", key, timeout=20)
        return True, d.get("data", {})
    except urllib.error.HTTPError as e:
        return False, {"error": f"HTTP {e.code}"}
    except Exception as e:
        return False, {"error": str(e)}


def models():
    req = urllib.request.Request(OPENROUTER + "/models")
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())["data"]
    out = []
    for m in data:
        if "structured_outputs" in (m.get("supported_parameters") or []) and not m["id"].endswith(":batch"):
            p = m.get("pricing") or {}
            out.append({"id": m["id"], "name": m.get("name"), "in_per_mtok": round(float(p.get("prompt") or 0) * 1e6, 3),
                        "out_per_mtok": round(float(p.get("completion") or 0) * 1e6, 3)})
    return sorted(out, key=lambda m: m["id"])


SYSTEM = """You are a MySQL-family database performance engineer helping an automated tuner.
The tuner has already tried or ruled out the changes listed in "history" and "rule_candidates".
Propose at most 3 NEW knob experiments (server variables, or OS settings named "os:<name>" from the "os_settings" list)
that address the diagnosed bottleneck for this exact server flavor and version. Each experiment is measured on its own,
so propose small, coherent changes. When the right value of a single numeric variable is uncertain, put 2-5 candidate
values in "sweep_values" (the tuner measures each); otherwise return an empty "sweep_values". Use only variables present in "current_variables" or "os_settings".
Never propose changes to: datadir, socket, port, server_id, read_only, sql_mode, character sets, replication topology.
Mark durability/replication/observability/semantics trade-offs honestly in "risk".
Judge past experiments by "configurations" (goal mean per configuration over all its runs) rather than by single-run
verdicts in "history": single runs on cloud VMs drift, and repeats of the same configuration reveal it.
Respect "server_facts" and "tuner_constraints": a knob that cannot act on this server (e.g. binary log settings with
log_bin=OFF) wastes a measured run.
If nothing is likely to help, return an empty list - do not guess."""

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["suggestions", "notes"],
    "properties": {
        "notes": {"type": "string"},
        "suggestions": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "changes", "mechanism", "expected_effect", "risk", "rationale", "sweep_values"],
            "properties": {
                "title": {"type": "string"},
                "changes": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                    "type": "object", "additionalProperties": False, "required": ["variable", "value"],
                    "properties": {"variable": {"type": "string"}, "value": {"type": "string"}}}},
                "mechanism": {"type": "string"},
                "expected_effect": {"type": "string", "enum": ["large", "moderate", "small"]},
                "risk": {"type": "string", "enum": ["none", "low", "durability", "replication", "observability", "semantics"]},
                "rationale": {"type": "string"},
                "sweep_values": {"type": "array", "maxItems": 6, "items": {"type": "string"}}}}}},
}


def gate(user_id, workload):
    """Has LLM advice been worth it for this workload class so far?"""
    rows = store.q("SELECT r.changes, r.verdict, " + store.WL_SQL + " wl FROM runs r JOIN tsessions s ON s.id=r.session_id "
                   "JOIN environments e ON e.id=s.env_id WHERE e.user_id=? AND r.status='done' AND r.changes LIKE '%rec:llm-%'", (user_id,))
    tries = wins = 0
    for r in rows:
        if workload and r["wl"] and r["wl"] != workload:
            continue
        tries += 1
        wins += r["verdict"] in ("improved", "efficiency-gain")
    return tries, wins


def suggest(user_id, env_id, session_id, context, force=False, trigger="routine"):
    st = settings(user_id)
    if not st.get("configured") or st["mode"] == "off":
        return {"enabled": False, "reason": "LLM assistance is not configured (account settings)"}
    force = force and _is_admin(user_id)  # skipping the gates is an operator's choice, not an agent request's
    # autotune --explore (user, 2026-10-25): the person asked for the whole run budget, ideas never tried included - the
    # track-record and per-session gates give way; the account's AI budget still applies
    explore = trigger == "explore"
    if st["mode"] == "when-needed" and trigger == "routine" and not force:
        return {"enabled": False, "reason": "rule-based recommendations are sufficient right now"}
    wl = (context.get("workload") or {}).get("type")
    tries, wins = gate(user_id, wl)
    if not force and not explore and tries >= GATE_MIN_TRIES and wins <= GATE_MAX_WINS:
        return {"enabled": False, "reason": f"LLM suggestions have not helped for {wl or 'this'} workloads ({tries} experiments, {wins} improvements)"}
    n = store.q1("SELECT COUNT(*) n FROM llm_calls WHERE session_id=? AND status='ok' AND cached=0 AND COALESCE(purpose,'suggest')='suggest'", (session_id,))["n"]
    if not force and not explore and n >= MAX_CALLS_PER_SESSION:
        return {"enabled": False, "reason": f"per-session limit of {MAX_CALLS_PER_SESSION} LLM calls reached"}
    payload = json.dumps(context, sort_keys=True, default=str)
    h = hashlib.sha256((st["model"] + payload).encode()).hexdigest()
    hit = store.q1("SELECT response FROM llm_calls WHERE user_id=? AND prompt_hash=? AND status='ok' ORDER BY id DESC LIMIT 1", (user_id, h))
    if not hit:
        why = budget_left(user_id)
        if why:
            return {"enabled": False, "reason": why}
    if hit:
        store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, response) VALUES (?,?,?,?,?,?,?,?,?)",
                 (user_id, env_id, session_id, time.time(), st["model"], h, "ok", 1, hit["response"]))
        return {"enabled": True, "cached": True, "model": st["model"], **json.loads(hit["response"])}
    key = service_key()
    body = {"model": st["model"], "temperature": 0.2, "usage": {"include": True},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "knob_suggestions", "strict": True, "schema": SCHEMA}}}
    t0 = time.time()
    try:
        d = _request("POST", OPENROUTER + "/chat/completions", key, body, deadline=DEADLINE_S)
        content = json.loads(d["choices"][0]["message"]["content"])
        usage = d.get("usage") or {}
        status, err = "ok", None
    except Exception as e:  # noqa
        content, usage, status, err = {"suggestions": [], "notes": ""}, {}, "error", str(e)[:500]
    store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, latency_s, prompt_tokens, "
             "completion_tokens, cost, response, error, context, purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (user_id, env_id, session_id, time.time(), st["model"], h, status, 0, round(time.time() - t0, 2), usage.get("prompt_tokens"),
              usage.get("completion_tokens"), usage.get("cost"), json.dumps(content), err, payload, "suggest"))
    if status != "ok":
        return {"enabled": True, "error": err, "suggestions": []}
    return {"enabled": True, "cached": False, "model": st["model"], "usage": usage, **content}


ANALYZE_SYSTEM = """You are a MySQL-family database performance engineer helping an automated, evidence-driven tuner.
The tuner measured where the database's time goes ("time_breakdown": average threads per state, from CPU and off-CPU
profiles, performance_schema waits and file I/O, eBPF probes) and found a component that takes real time but that none of
its rules acts on ("component", with the stacks / counters it saw in "evidence").
1. State a hypothesis: why does this time occur in this workload and configuration? Ground it in the evidence given;
   say what is uncertain.
2. Propose at most 3 changes (server variables from "current_variables", or "os:<name>" from "os_settings") that could
   reduce this component. For each give the physical mechanism - how the change shortens or removes this wait / work -
   and the precondition that must hold for it to act. Do not propose changes whose mechanism does not touch this
   component, and nothing the tuner already tried ("history") or has as a candidate ("rule_candidates").
3. Say which measurement would confirm or refute the hypothesis after the change (which component should shrink).
Outside reports and common advice are not evidence: use them only as leads, and say so. If no configuration change
can act on this component (e.g. it is a property of the workload or schema), return no changes and explain."""

ANALYZE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["hypothesis", "confirm_by", "changes", "notes"],
    "properties": {
        "hypothesis": {"type": "string"}, "confirm_by": {"type": "string"}, "notes": {"type": "string"},
        "changes": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "changes", "mechanism", "precondition", "expected_effect", "risk", "rationale", "sweep_values"],
            "properties": {
                "title": {"type": "string"},
                "changes": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                    "type": "object", "additionalProperties": False, "required": ["variable", "value"],
                    "properties": {"variable": {"type": "string"}, "value": {"type": "string"}}}},
                "mechanism": {"type": "string"}, "precondition": {"type": "string"},
                "expected_effect": {"type": "string", "enum": ["large", "moderate", "small"]},
                "risk": {"type": "string", "enum": ["none", "low", "durability", "replication", "observability", "semantics"]},
                "rationale": {"type": "string"},
                "sweep_values": {"type": "array", "maxItems": 6, "items": {"type": "string"}}}}}},
}


def analyze(user_id, env_id, session_id, context, signature):
    """Bottleneck analysis for one uncovered component. Cached by the signature of the conditions (server version,
    component, workload, hardware class, key settings, where the time is spent): similar conditions reuse the analysis,
    and the agent still validates every change against the current server."""
    st = settings(user_id)
    if not st.get("configured") or st["mode"] == "off":
        return {"enabled": False, "reason": "LLM assistance is not configured (account settings)"}
    if not isinstance(signature, dict) or not signature.get("component"):
        return {"enabled": False, "reason": "no bottleneck component in the analysis signature"}
    sig = json.dumps(signature, sort_keys=True, default=str)
    # same conditions, same model, same prompt -> the same analysis can be reused
    h = "analyze:" + hashlib.sha256(f"{st['model']}|{ANALYZE_PROMPT_VERSION}|{sig}".encode()).hexdigest()
    hit = store.q1("SELECT id, session_id, model, response FROM llm_calls WHERE user_id=? AND prompt_hash=? AND status='ok' AND cached=0 "
                   "ORDER BY id DESC LIMIT 1", (user_id, h))
    if hit:
        store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, response, purpose, signature) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (user_id, env_id, session_id, time.time(), hit["model"], h, "ok", 1, hit["response"], "analyze", sig))
        return {"enabled": True, "cached": True, "cached_from": {"call": hit["id"], "session": hit["session_id"]}, "model": hit["model"],
                **json.loads(hit["response"])}
    n = store.q1("SELECT COUNT(*) n FROM llm_calls WHERE session_id=? AND status='ok' AND cached=0 AND purpose='analyze'", (session_id,))["n"]
    if n >= MAX_ANALYSES_PER_SESSION:
        return {"enabled": False, "reason": f"per-session limit of {MAX_ANALYSES_PER_SESSION} bottleneck analyses reached"}
    why = budget_left(user_id)
    if why:
        return {"enabled": False, "reason": why}
    payload = json.dumps(context, sort_keys=True, default=str)
    key = service_key()
    body = {"model": st["model"], "temperature": 0.2, "usage": {"include": True},
            "messages": [{"role": "system", "content": ANALYZE_SYSTEM}, {"role": "user", "content": payload}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "bottleneck_analysis", "strict": True, "schema": ANALYZE_SCHEMA}}}
    t0 = time.time()
    try:
        d = _request("POST", OPENROUTER + "/chat/completions", key, body, deadline=DEADLINE_S)
        content = json.loads(d["choices"][0]["message"]["content"])
        usage = d.get("usage") or {}
        status, err = "ok", None
    except Exception as e:  # noqa
        content, usage, status, err = {"hypothesis": "", "confirm_by": "", "changes": [], "notes": ""}, {}, "error", str(e)[:500]
    store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, latency_s, prompt_tokens, "
             "completion_tokens, cost, response, error, context, purpose, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (user_id, env_id, session_id, time.time(), st["model"], h, status, 0, round(time.time() - t0, 2), usage.get("prompt_tokens"),
              usage.get("completion_tokens"), usage.get("cost"), json.dumps(content), err, payload, "analyze", sig))
    if status != "ok":
        return {"enabled": True, "error": err, "changes": []}
    return {"enabled": True, "cached": False, "model": st["model"], "usage": usage, **content}


def replay(user_id, call_id, model=None, context=None, patch=None):
    """Re-ask a stored call: the same context with another model (model question), or a patched / enriched context
    with the same model (context question). Recorded with purpose='replay' and excluded from the tuner's own limits."""
    row = store.q1("SELECT * FROM llm_calls WHERE id=? AND user_id=?", (call_id, user_id))
    if not row:
        raise ValueError("call not found")
    ctx = context if context is not None else (json.loads(row["context"]) if row["context"] else None)
    if ctx is None:
        raise ValueError("this call has no stored context (made before contexts were recorded): pass one")
    for k, v in (patch or {}).items():
        ctx[k] = v
    st = settings(user_id)
    model = model or row["model"] or st["model"]
    key = service_key()
    if not key:
        raise ValueError("AI assistance is not configured (no service key)")
    payload = json.dumps(ctx, sort_keys=True, default=str)
    body = {"model": model, "temperature": 0.2, "usage": {"include": True},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "knob_suggestions", "strict": True, "schema": SCHEMA}}}
    t0 = time.time()
    d = _request("POST", OPENROUTER + "/chat/completions", key, body, deadline=DEADLINE_S)
    content = json.loads(d["choices"][0]["message"]["content"])
    usage = d.get("usage") or {}
    store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, latency_s, prompt_tokens, "
             "completion_tokens, cost, response, context, purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (user_id, row["env_id"], row["session_id"], time.time(), model, f"replay-of-{call_id}", "ok", 0, round(time.time() - t0, 2),
              usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost"), json.dumps(content), payload, f"replay:{call_id}"))
    return {"model": model, "usage": usage, **content}


def usage_summary(user_id):
    r = store.q1("SELECT COUNT(*) calls, SUM(cached) cached, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) tokens, "
                 "SUM(COALESCE(cost,0)) cost FROM llm_calls WHERE user_id=?", (user_id,))
    tries, wins = gate(user_id, None)
    return {"calls": r["calls"] or 0, "cached": r["cached"] or 0, "tokens": r["tokens"] or 0, "cost_usd": round(r["cost"] or 0, 4),
            "experiments": tries, "improvements": wins}


_key_cache = {"t": 0, "data": None}


def key_status():
    """The service key's usage and limit as OpenRouter reports them; cached 10 minutes (a status page render must not wait on it)."""
    if time.time() - _key_cache["t"] < 600:
        return _key_cache["data"]
    k = service_key()
    data = None
    if k:
        try:
            d = _request("GET", OPENROUTER + "/key", k, timeout=5).get("data") or {}
            data = {f: d.get(f) for f in ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining", "is_free_tier")}
        except Exception as e:
            data = {"error": str(e)[:120]}
    _key_cache.update(t=time.time(), data=data)
    return data


def stats(hours=24):
    """AI model use over the last `hours` (and the period before it, for trends): per model and purpose, a time series of
    live / cached calls and cost, latency, the sessions that cost most, and what the experiments it proposed achieved."""
    now = time.time()
    since, prev = now - hours * 3600, now - 2 * hours * 3600
    bucket = 3600 if hours <= 48 else 86400 if hours > 24 * 14 else 6 * 3600
    rows = store.q("SELECT c.ts, c.model, COALESCE(c.purpose, 'suggest') purpose, c.status, c.cached, c.latency_s, c.prompt_tokens, "
                   "c.completion_tokens, c.cost, c.session_id, c.user_id FROM llm_calls c WHERE c.ts >= ? ORDER BY c.ts", (prev,))
    cur = [r for r in rows if r["ts"] >= since]
    old = [r for r in rows if r["ts"] < since]

    def tot(rs):
        live = [r for r in rs if not r["cached"]]
        lat = sorted(r["latency_s"] for r in live if r["latency_s"] is not None)
        q = lambda p: round(lat[min(len(lat) - 1, int(p * (len(lat) - 1) + 0.5))], 1) if lat else None
        return {"calls": len(rs), "live": len(live), "cached": len(rs) - len(live), "errors": sum(1 for r in rs if r["status"] != "ok"),
                "cost": round(sum(r["cost"] or 0 for r in live), 4), "tokens_in": sum(r["prompt_tokens"] or 0 for r in live),
                "tokens_out": sum(r["completion_tokens"] or 0 for r in live), "latency_p50_s": q(0.5), "latency_p95_s": q(0.95),
                "cache_hit_pct": round(100.0 * (len(rs) - len(live)) / len(rs), 1) if rs else None,
                "cost_per_live_call": round(sum(r["cost"] or 0 for r in live) / len(live), 4) if live else None}

    def group(key):
        g = {}
        for r in cur:
            g.setdefault(r[key], []).append(r)
        out = [dict(tot(v), **{key: k, "last_used": max(r["ts"] for r in v)}) for k, v in g.items()]
        return sorted(out, key=lambda x: -x["cost"])

    t0 = int(since // bucket * bucket)
    series = {t: {"ts": t, "live": 0, "cached": 0, "errors": 0, "cost": 0.0} for t in range(t0, int(now) + 1, bucket)}
    for r in cur:
        b = series.setdefault(int(r["ts"] // bucket * bucket), {"ts": int(r["ts"] // bucket * bucket), "live": 0, "cached": 0, "errors": 0, "cost": 0.0})
        b["cached" if r["cached"] else "live"] += 1
        b["errors"] += r["status"] != "ok"
        b["cost"] += 0 if r["cached"] else (r["cost"] or 0)
        if not r["cached"] and r["cost"]:
            b["cost:" + r["model"]] = b.get("cost:" + r["model"], 0) + r["cost"]
    ser, cum = [], 0.0
    for t in sorted(series):
        cum += series[t]["cost"]
        ser.append({k: (round(v, 4) if isinstance(v, float) else v) for k, v in series[t].items()} | {"cum_cost": round(cum, 4)})
    sess = {}
    for r in cur:
        if r["session_id"]:
            s = sess.setdefault(r["session_id"], {"session_id": r["session_id"], "calls": 0, "cached": 0, "cost": 0.0})
            s["calls"] += 1
            s["cached"] += bool(r["cached"])
            s["cost"] += 0 if r["cached"] else (r["cost"] or 0)
    top = sorted(sess.values(), key=lambda s: -s["cost"])[:8]
    for s in top:
        s["cost"] = round(s["cost"], 4)
        n = store.q1("SELECT s.name, e.name env FROM tsessions s JOIN environments e ON e.id=s.env_id WHERE s.id=?", (s["session_id"],))
        s.update(name=n["name"] if n else None, env=n["env"] if n else None)
    # what the AI-sourced experiments measured in the period (ideas and bottleneck analyses both carry rec:llm-* ids)
    ex = store.q("SELECT verdict, COUNT(*) n FROM runs WHERE status='done' AND started >= ? AND changes LIKE '%rec:llm-%' GROUP BY verdict", (since,))
    exp = {r["verdict"] or "-": r["n"] for r in ex}
    tried = sum(exp.values())
    wins = exp.get("improved", 0) + exp.get("efficiency-gain", 0)
    cur_t = tot(cur)
    return {"hours": hours, "bucket_s": bucket, "settings": settings(), "totals": cur_t, "previous": tot(old),
            "by_model": group("model"), "by_purpose": group("purpose"), "series": ser, "top_sessions": top,
            "experiments": {"tried": tried, "improved": wins, "verdicts": exp,
                            "cost_per_improvement": round(cur_t["cost"] / wins, 3) if wins else None},
            "key": key_status()}


# ------------------------------------------------------------------------------------------------- planner
PLAN_PROMPT_VERSION = "2"
PLAN_SYSTEM = """You are the planner of an automated MySQL / Percona Server / MariaDB tuner, acting as a senior performance
engineer. At each step you choose the single next measured run, or decide to stop. Every run costs 20-40 minutes of a
real server, so choose the experiment with the highest expected CONFIRMED gain of the goal per run.

How to decide:
1. Diagnose from measured time, not from counters crossing thresholds. "time_breakdown" is average database threads per
   state (shares of database time) built from CPU and off-CPU profiles, waits and eBPF probes; "profiles" and "summary"
   add detail. Name the dominant component(s) and the kind of limit: CPU-bound (on which code path), commit / fsync-bound,
   read-I/O-bound, flush-bound (dirty pages, free-page waits, checkpoint age), lock-bound (row locks, mutexes),
   queueing / over-subscription (run-queue wait, threads running >> CPUs), or client / network-bound.
2. Consider only changes whose physical mechanism acts on a component with measured time, and estimate the achievable
   effect: a change removes at most part of the component it acts on (a 6% component cannot yield +15%). A choice whose
   plausible effect is below noise_pct cannot be confirmed: do not pick it while a better-founded choice exists.
3. Prefer "candidates" (pre-validated for this server: they exist, fit memory, respect policy). Read each candidate's
   relevance (0-4: share of measured time it acts on), note, hypothesis, track_record and experience (results of the same
   change in comparable sessions; large losses there are strong evidence against it). "known_neutral" / learned_basis
   "0 wins in N runs" (N >= 3, comparable sessions of every account) outweighs relevance: pick such a change only with new
   evidence this session that earlier ones lacked. You may propose new changes when no candidate acts on the dominant
   component; use only variables in current_variables / os_settings / available_pseudo_variables.
   choice.candidate_id is either a listed candidate id or empty; anything else goes into choice.changes as variable/value
   pairs (e.g. {"variable": "innodb_io_capacity", "value": "4000"}).
4. Learn from this session: "configurations" (mean goal per configuration over all its runs) beats single-run verdicts;
   do not repeat refuted mechanisms; an outcome "moved" means the targeted time shrank but the goal did not follow, so
   that component no longer limits the goal.
5. Policy: under "safe" only changes without trade-offs may run (trade-offs are advice). Trade-offs include durability
   (innodb_flush_log_at_trx_commit != 1, sync_binlog != 1, innodb_doublewrite OFF / DETECT_ONLY, the binary log off),
   observability (performance_schema OFF, ps:profile=lite and other performance_schema reductions), replication and
   semantics (transaction_isolation, innodb_deadlock_detect OFF): a configuration with any of them is not production-safe.
   Under "benchmark" trade-offs may run, and when the evidence for a trade-off is stronger than for the remaining safe
   ideas, run it now rather than last. "rejected_this_session" lists your earlier choices the tuner's checks refused,
   with the reason: never choose them (or the same change under another name) again in this session.
6. Stop when no remaining idea can plausibly beat the noise on a component with measured time, or when the limit is
   outside the server's configuration (e.g. commits wait on slow fsyncs and group commit already batches every waiting
   commit; the CPU is saturated by the work itself; the client is the bottleneck). Then say in stop_advice what would help
   instead (hardware, a durability trade-off, schema / application changes).
7. Make a falsifiable prediction: the expected goal change range in %, and which measured component or metric should
   move, in which direction, by about how much.
"engine" shows what the rule engine would do; agree with it when it is right, overrule it when the evidence says so."""

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["diagnosis", "limit_kind", "action", "choice", "title", "mechanism", "risk", "prediction", "confidence",
                 "why_above_noise", "alternatives", "stop_advice", "rationale"],
    "properties": {
        "diagnosis": {"type": "string"},
        "limit_kind": {"type": "string", "enum": ["cpu", "commit", "read-io", "flush", "locks", "queueing", "client-network", "memory", "mixed", "unclear"]},
        "action": {"type": "string", "enum": ["run", "stop"]},
        "choice": {"type": "object", "additionalProperties": False, "required": ["candidate_id", "changes", "sweep_values"],
                   "properties": {"candidate_id": {"type": "string"},
                                  "changes": {"type": "array", "maxItems": 4, "items": {
                                      "type": "object", "additionalProperties": False, "required": ["variable", "value"],
                                      "properties": {"variable": {"type": "string"}, "value": {"type": "string"}}}},
                                  "sweep_values": {"type": "array", "maxItems": 5, "items": {"type": "string"}}}},
        "title": {"type": "string"}, "mechanism": {"type": "string"},
        "risk": {"type": "string", "enum": ["none", "low", "durability", "replication", "observability", "semantics"]},
        "prediction": {"type": "object", "additionalProperties": False,
                       "required": ["goal_delta_pct_low", "goal_delta_pct_high", "metric", "metric_direction", "metric_change_pct"],
                       "properties": {"goal_delta_pct_low": {"type": "number"}, "goal_delta_pct_high": {"type": "number"},
                                      "metric": {"type": "string"}, "metric_direction": {"type": "string", "enum": ["down", "up", "none"]},
                                      "metric_change_pct": {"type": "number"}}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "why_above_noise": {"type": "string"},
        "alternatives": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False, "required": ["candidate_id", "why_not_now"],
            "properties": {"candidate_id": {"type": "string"}, "why_not_now": {"type": "string"}}}},
        "stop_advice": {"type": "string"}, "rationale": {"type": "string"}},
}

CRITIC_SYSTEM = """You review the next step another engineer (the planner) chose for an automated MySQL-family tuner, with the
same evidence ("packet") in front of you. Check the plan against these failure patterns, which cost most runs so far:
- below the noise: the change acts on a component too small (or not measured at all) for its effect to beat noise_pct;
- no mechanism: the change cannot act here (feature off, nothing to read ahead, no waits of that kind, limit >= connections);
- should stop: the limit is outside the server configuration, or several runs in a row found nothing and the rest is small;
- ranking: a candidate with stronger measured evidence, a better track record, or (benchmark policy) an evidence-backed
  trade-off was passed over; a change that lost heavily in comparable sessions was chosen anyway;
- repeat: the same mechanism was already refuted, or its component already "moved".
Accept a reasonable plan; when you revise, give the step you would take instead (a candidate id or concrete changes, or stop)."""

CRITIC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "issues", "action", "candidate_id", "changes", "reason"],
    "properties": {"verdict": {"type": "string", "enum": ["accept", "revise"]},
                   "issues": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
                   "action": {"type": "string", "enum": ["run", "stop"]},
                   "candidate_id": {"type": "string"},
                   "changes": {"type": "array", "maxItems": 4, "items": {
                       "type": "object", "additionalProperties": False, "required": ["variable", "value"],
                       "properties": {"variable": {"type": "string"}, "value": {"type": "string"}}}},
                   "reason": {"type": "string"}},
}
PLAN_MODEL = "anthropic/claude-opus-5.5"


def _call(user_id, env_id, session_id, model, system, schema, name, payload, purpose, effort=None):
    """One structured call, logged in llm_calls (cost, tokens, latency) with its purpose; cached by model + prompt + payload."""
    h = f"{purpose}:" + hashlib.sha256(f"{model}|{PLAN_PROMPT_VERSION}|{system}|{payload}".encode()).hexdigest()
    hit = store.q1("SELECT response FROM llm_calls WHERE user_id=? AND prompt_hash=? AND status='ok' AND cached=0 ORDER BY id DESC LIMIT 1", (user_id, h))
    if hit:
        store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, response, purpose) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (user_id, env_id, session_id, time.time(), model, h, "ok", 1, hit["response"], purpose))
        return json.loads(hit["response"]), {"cached": True}
    body = {"model": model, "usage": {"include": True},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": payload}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}}
    if effort:
        body["reasoning"] = {"effort": effort}
    t0 = time.time()
    try:
        d = _request("POST", OPENROUTER + "/chat/completions", service_key(), body, timeout=600, deadline=DEADLINE_S)
        content = json.loads(d["choices"][0]["message"]["content"])
        usage, status, err = d.get("usage") or {}, "ok", None
    except Exception as e:  # noqa
        content, usage, status, err = None, {}, "error", str(e)[:500]
    store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, latency_s, prompt_tokens, "
             "completion_tokens, cost, response, error, context, purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (user_id, env_id, session_id, time.time(), model, h, status, 0, round(time.time() - t0, 2), usage.get("prompt_tokens"),
              usage.get("completion_tokens"), usage.get("cost"), json.dumps(content) if content else None, err, payload, purpose))
    if status != "ok":
        raise RuntimeError(err)
    return content, dict(usage, latency_s=round(time.time() - t0, 2))


def _with_ids(schema, ids, path):
    """A copy of the schema whose field at `path` only accepts the listed ids (or empty)."""
    out = json.loads(json.dumps(schema))
    node = out
    for k in path:
        node = node[k]
    node["enum"] = [""] + list(ids)
    return out


def plan(user_id, env_id, session_id, packet, model=None, critic_model=None, effort="high", purpose="plan"):
    """The planner's decision for the next step, reviewed by a second model when critic_model is set. When the critic
    revises, the result carries both and takes the critic's step only for a stop or a candidate the planner listed as an
    alternative or the engine picked (a change of mind backed by the evidence both saw); otherwise the planner's step stands."""
    st = settings(user_id)
    if not st.get("configured"):
        return {"enabled": False, "reason": "LLM assistance is not configured"}
    why = budget_left(user_id)
    if why:
        return {"enabled": False, "reason": why}
    model = model or st.get("planner_model") or PLAN_MODEL
    payload = json.dumps(packet, sort_keys=True, default=str)
    # candidate ids as an enum: a step that is not a listed candidate must be given as concrete changes (pilot: 15 of 60
    # Opus answers named a lighter performance_schema "ps-lite" as an id instead of ps:profile=lite as a change)
    ids = sorted({c.get("id") for c in packet.get("candidates") or [] if c.get("id")})
    plan_schema, critic_schema = _with_ids(PLAN_SCHEMA, ids, ("properties", "choice", "properties", "candidate_id")), \
        _with_ids(CRITIC_SCHEMA, ids, ("properties", "candidate_id"))
    plan_schema = _with_ids(plan_schema, ids, ("properties", "alternatives", "items", "properties", "candidate_id"))
    try:
        p, u = _call(user_id, env_id, session_id, model, PLAN_SYSTEM, plan_schema, "next_step", payload, purpose, effort)
    except Exception as e:
        return {"enabled": True, "error": str(e)[:300]}
    out = {"enabled": True, "model": model, "plan": p, "usage": u}
    if critic_model:
        try:
            c, cu = _call(user_id, env_id, session_id, critic_model, CRITIC_SYSTEM, critic_schema, "plan_review",
                          json.dumps({"packet": packet, "plan": p}, sort_keys=True, default=str), purpose + "-critic", effort)
            out.update(critic=c, critic_model=critic_model, critic_usage=cu)
            if c.get("verdict") == "revise":
                ok_ids = {a.get("candidate_id") for a in p.get("alternatives") or []} | {(packet.get("engine") or {}).get("next")}
                if c.get("action") == "stop" or (c.get("candidate_id") and c["candidate_id"] in ok_ids):
                    out["final"] = {"action": c["action"], "choice": {"candidate_id": c.get("candidate_id") or "", "changes": c.get("changes") or [],
                                                                      "sweep_values": []}, "by": "critic", "reason": c.get("reason")}
        except Exception as e:
            out["critic_error"] = str(e)[:300]
    return out
