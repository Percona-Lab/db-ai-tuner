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
MODES = ("off", "when-needed", "always")
MAX_CALLS_PER_SESSION = 3
MAX_ANALYSES_PER_SESSION = 6
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
    return store.q1("SELECT id, llm_key_enc, llm_model, llm_mode FROM users WHERE is_admin=1 "
                    "ORDER BY (llm_key_enc IS NULL), id LIMIT 1")


def settings(user_id=None):
    """Service-wide AI settings (the same for every user)."""
    r = _service_row()
    if not r:
        return {"configured": False, "model": DEFAULT_MODEL, "mode": "off"}
    return {"configured": bool(r["llm_key_enc"]), "model": r["llm_model"] or DEFAULT_MODEL, "mode": r["llm_mode"] or "when-needed"}


def service_key():
    r = _service_row()
    return decrypt(r["llm_key_enc"]) if r and r["llm_key_enc"] else None


def _request(method, url, key, body=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Title", "AI DB Tuner")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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
    rows = store.q("SELECT r.changes, r.verdict, json_extract(r.inputs, '$.workload.type') wl FROM runs r JOIN tsessions s ON s.id=r.session_id "
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
    if st["mode"] == "when-needed" and trigger == "routine" and not force:
        return {"enabled": False, "reason": "rule-based recommendations are sufficient right now"}
    wl = (context.get("workload") or {}).get("type")
    tries, wins = gate(user_id, wl)
    if not force and tries >= GATE_MIN_TRIES and wins <= GATE_MAX_WINS:
        return {"enabled": False, "reason": f"LLM suggestions have not helped for {wl or 'this'} workloads ({tries} experiments, {wins} improvements)"}
    n = store.q1("SELECT COUNT(*) n FROM llm_calls WHERE session_id=? AND status='ok' AND cached=0 AND COALESCE(purpose,'suggest')='suggest'", (session_id,))["n"]
    if not force and n >= MAX_CALLS_PER_SESSION:
        return {"enabled": False, "reason": f"per-session limit of {MAX_CALLS_PER_SESSION} LLM calls reached"}
    payload = json.dumps(context, sort_keys=True, default=str)
    h = hashlib.sha256((st["model"] + payload).encode()).hexdigest()
    hit = store.q1("SELECT response FROM llm_calls WHERE user_id=? AND prompt_hash=? AND status='ok' ORDER BY id DESC LIMIT 1", (user_id, h))
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
        d = _request("POST", OPENROUTER + "/chat/completions", key, body)
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
    sig = json.dumps(signature, sort_keys=True, default=str)
    h = "analyze:" + hashlib.sha256(sig.encode()).hexdigest()
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
    payload = json.dumps(context, sort_keys=True, default=str)
    key = service_key()
    body = {"model": st["model"], "temperature": 0.2, "usage": {"include": True},
            "messages": [{"role": "system", "content": ANALYZE_SYSTEM}, {"role": "user", "content": payload}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "bottleneck_analysis", "strict": True, "schema": ANALYZE_SCHEMA}}}
    t0 = time.time()
    try:
        d = _request("POST", OPENROUTER + "/chat/completions", key, body)
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
    payload = json.dumps(ctx, sort_keys=True, default=str)
    body = {"model": model, "temperature": 0.2, "usage": {"include": True},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "knob_suggestions", "strict": True, "schema": SCHEMA}}}
    t0 = time.time()
    d = _request("POST", OPENROUTER + "/chat/completions", key, body)
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
