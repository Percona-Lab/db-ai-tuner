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


def settings(user_id):
    r = store.q1("SELECT llm_key_enc, llm_model, llm_mode FROM users WHERE id=?", (user_id,))
    if not r:
        return {}
    return {"configured": bool(r["llm_key_enc"]), "model": r["llm_model"] or DEFAULT_MODEL, "mode": r["llm_mode"] or "when-needed"}


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
    n = store.q1("SELECT COUNT(*) n FROM llm_calls WHERE session_id=? AND status='ok' AND cached=0", (session_id,))["n"]
    if not force and n >= MAX_CALLS_PER_SESSION:
        return {"enabled": False, "reason": f"per-session limit of {MAX_CALLS_PER_SESSION} LLM calls reached"}
    payload = json.dumps(context, sort_keys=True, default=str)
    h = hashlib.sha256((st["model"] + payload).encode()).hexdigest()
    hit = store.q1("SELECT response FROM llm_calls WHERE user_id=? AND prompt_hash=? AND status='ok' ORDER BY id DESC LIMIT 1", (user_id, h))
    if hit:
        store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, response) VALUES (?,?,?,?,?,?,?,?,?)",
                 (user_id, env_id, session_id, time.time(), st["model"], h, "ok", 1, hit["response"]))
        return {"enabled": True, "cached": True, "model": st["model"], **json.loads(hit["response"])}
    key = decrypt(store.q1("SELECT llm_key_enc FROM users WHERE id=?", (user_id,))["llm_key_enc"])
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
             "completion_tokens, cost, response, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (user_id, env_id, session_id, time.time(), st["model"], h, status, 0, round(time.time() - t0, 2), usage.get("prompt_tokens"),
              usage.get("completion_tokens"), usage.get("cost"), json.dumps(content), err))
    if status != "ok":
        return {"enabled": True, "error": err, "suggestions": []}
    return {"enabled": True, "cached": False, "model": st["model"], "usage": usage, **content}


def usage_summary(user_id):
    r = store.q1("SELECT COUNT(*) calls, SUM(cached) cached, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) tokens, "
                 "SUM(COALESCE(cost,0)) cost FROM llm_calls WHERE user_id=?", (user_id,))
    tries, wins = gate(user_id, None)
    return {"calls": r["calls"] or 0, "cached": r["cached"] or 0, "tokens": r["tokens"] or 0, "cost_usd": round(r["cost"] or 0, 4),
            "experiments": tries, "improvements": wins}
