"""The learner: a background process that turns observations into learnings.

Observations (tuner.db: sessions, runs, metrics, verdicts, hypotheses) stay as measured. Learnings live in a separate
database (learning.db): insights with the conditions they hold in, the sessions behind them and support / contradiction
counts computed from the observations; and proposals for changes to the tuner's code. See docs/design/agentic-saas.md.

Learnings are summaries of experiment data, not truths: a result may not repeat, may hold only in the narrow
conditions it was measured in, and may come from one host. Every insight carries its scope (conditions, sessions, hosts)
and is phrased as what was measured; agents see it as an earlier result to test, never as a reason to skip measuring.

Each pass (every TUNER_LEARNER_HOURS, default 6, or started by the admin):
1. digest every session closed since the last pass (measured bottlenecks, experiments, outcomes, hypotheses);
2. ask the AI (the service key) for insights and code proposals, given what is already known;
3. count each insight against the observations - runs that changed the same settings under the same conditions - and
   set its status from those counts, not from the AI's word: observed (one session), repeated (several sessions agree),
   contested (runs disagree), contradicted (more runs against than for);
4. merge duplicates; log the pass (sessions, calls, cost, errors).
"""
import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time

from . import llm, store

DB_PATH = os.environ.get("TUNER_LEARNING_DB", "/var/lib/aituner/learning.db")
INTERVAL_H = float(os.environ.get("TUNER_LEARNER_HOURS", "6"))
MAX_CALLS_PER_PASS, MAX_CALLS_PER_DAY = 12, 48
LOCK = DB_PATH + ".lock"

SCHEMA = """
CREATE TABLE IF NOT EXISTS insights (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, kind TEXT, statement TEXT NOT NULL,
  settings TEXT, conditions TEXT, direction TEXT, sessions TEXT, support INTEGER DEFAULT 0, contradict INTEGER DEFAULT 0,
  neutral INTEGER DEFAULT 0, confidence REAL, status TEXT NOT NULL DEFAULT 'candidate', source TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, created REAL NOT NULL, title TEXT NOT NULL, kind TEXT, body TEXT, sessions TEXT,
  status TEXT NOT NULL DEFAULT 'open', source TEXT);
CREATE TABLE IF NOT EXISTS learner_runs (
  id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL, trigger TEXT, sessions INTEGER DEFAULT 0, calls INTEGER DEFAULT 0,
  cost REAL DEFAULT 0, insights_new INTEGER DEFAULT 0, insights_updated INTEGER DEFAULT 0, proposals_new INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS processed (session_id INTEGER PRIMARY KEY, processed REAL NOT NULL, learner_run INTEGER);
CREATE TABLE IF NOT EXISTS evidence (
  insight_id INTEGER NOT NULL, session_id INTEGER NOT NULL, support INTEGER DEFAULT 0, contradict INTEGER DEFAULT 0,
  neutral INTEGER DEFAULT 0, counted REAL, env_id INTEGER, PRIMARY KEY (insight_id, session_id));
"""

_local = threading.local()


def db():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(evidence)")}
        if "env_id" not in cols:
            c.execute("ALTER TABLE evidence ADD COLUMN env_id INTEGER")
        if "weight" not in cols:
            c.execute("ALTER TABLE evidence ADD COLUMN weight REAL DEFAULT 1")
        cols = {r[1] for r in c.execute("PRAGMA table_info(insights)")}
        for col, typ in (("hosts", "INTEGER"), ("caveats", "TEXT"), ("support_sessions", "INTEGER")):
            if col not in cols:
                c.execute(f"ALTER TABLE insights ADD COLUMN {col} {typ}")
        cols = {r[1] for r in c.execute("PRAGMA table_info(proposals)")}
        for col in ("proposal", "expected_effect"):
            if col not in cols:
                c.execute(f"ALTER TABLE proposals ADD COLUMN {col} TEXT")
        # earlier proposals kept problem and proposal in one text: split at "Proposal:"
        for r in c.execute("SELECT id, body FROM proposals WHERE proposal IS NULL AND body LIKE '%Proposal:%'").fetchall():
            prob, _, prop = r["body"].partition("Proposal:")
            c.execute("UPDATE proposals SET body=?, proposal=? WHERE id=?", (prob.strip(), prop.strip(), r["id"]))
        # earlier status names (they read like verdicts): observed / repeated / contradicted
        c.execute("UPDATE insights SET status='observed' WHERE status='candidate'")
        c.execute("UPDATE insights SET status='repeated' WHERE status='validated'")
        c.execute("UPDATE insights SET status='contradicted' WHERE status='retired'")
        _local.c = c
    return c


def _j(x):
    return json.dumps(x, sort_keys=True, default=str)


def _uj(s, d=None):
    try:
        return json.loads(s) if s else d
    except (TypeError, ValueError):
        return d


# ------------------------------------------------------------------------------------------------ session digests
def _cpu_class(n):  # the same classes as the experience endpoint (main.cpu_class)
    n = int(n or 0)
    return None if not n else "1-2" if n <= 2 else "3-8" if n <= 8 else "9-32" if n <= 32 else "33+"


def _conc_class(n):  # main.conc_class
    n = int(float(n or 0))
    return None if not n else "1-16" if n <= 16 else "17-128" if n <= 128 else "129+"


def digest(sid):
    """What a closed session measured and decided, compact enough for one AI call."""
    s = store.q1("SELECT t.*, e.facts FROM tsessions t JOIN environments e ON e.id=t.env_id WHERE t.id=?", (sid,))
    if not s:
        return None
    facts = _uj(s["facts"], {}) or {}
    hw = facts.get("hardware") or {}
    runs = store.q("SELECT seq, label, changes, verdict, goal_value, evaluation, summary, inputs, diagnosis FROM runs "
                   "WHERE session_id=? AND status='done' ORDER BY seq", (sid,))
    out_runs, wl, conns, base_bd = [], None, None, None
    for r in runs:
        ev, sm, inp = _uj(r["evaluation"], {}) or {}, _uj(r["summary"], {}) or {}, _uj(r["inputs"], {}) or {}
        wl = wl or ((inp.get("workload") or {}).get("type"))
        conns = conns or sm.get("threads_connected_avg")
        bd = [f"{p['name']} {p['share_pct']}%" for p in (inp.get("time_breakdown") or [])[:4]]
        if base_bd is None and bd and str(r["label"] or "").startswith("baseline"):
            base_bd = bd
        h = ev.get("hypothesis") or {}
        out_runs.append({
            "run": r["seq"], "label": (r["label"] or "")[:60], "verdict": r["verdict"],
            "goal": r["goal_value"], "delta_vs_parent_pct": ev.get("goal_delta_pct"),
            "changes": [f"{c.get('variable')}={c.get('new')}" for c in _uj(r["changes"], []) or []][:8],
            "hypothesis": {k: h.get(k) for k in ("kind", "target", "outcome") if h.get(k)} or None,
            "top_time": bd or None,
            "flags": [k for k in ("not_steady", "unstable_within_run", "swap_hazard", "client_saturated", "goal_server_mismatch_pct") if ev.get(k)] or None,
            "diagnosis": sorted({d.get("type") for d in _uj(r["diagnosis"], []) or [] if d.get("type") not in (None, "none")})[:6] or None,
        })
    cal = _uj(s["calibration"], {}) if "calibration" in s.keys() else {}
    return {"session": sid, "name": s["name"], "workload_text": s["workload"], "goal": s["goal"], "policy": s["policy"],
            "agent_version": s["agent_version"] if "agent_version" in s.keys() else None,
            "conditions": {"workload": wl, "cpu_class": _cpu_class(hw.get("cpus")), "concurrency_class": _conc_class(conns),
                           "cpus": hw.get("cpus"), "ram_gb": round((hw.get("ram_bytes") or 0) / 2 ** 30), "disk": hw.get("disk_model") or hw.get("disk")},
            "noise_pct": (cal or {}).get("noise_pct"), "baseline_time_breakdown": base_bd, "runs": out_runs}


# ------------------------------------------------------------------------------------------------ AI call
SYSTEM = """You review finished MySQL tuning sessions of an automated, evidence-driven tuner and extract what they teach.
Input: one session digest (conditions, measured time breakdown, every experiment with its verdict against its parent
configuration and the noise threshold, hypothesis outcomes, measurement flags) and a list of insights already known.
These are summaries of experiment data, not general truths: one session on one host, with noise, in narrow conditions.
Return:
- insights: what was measured about a setting (or small group of settings) and its effect, phrased as an observation
  ("on a 2-CPU host at 16 connections, X raised TPS 10% (runs 5-6)"), never as a general rule. State the scope, and in
  caveats what limits it (single host, noise level, unsteady runs, a confound, a narrow workload),
  grounded in this session's runs (cite run numbers). Use direction helps / hurts / no-effect. Conditions: workload
  type, CPU class, concurrency class, and the measured bottleneck components that made it work or not. If an existing
  insight says the same, return it with same_as = its id (this session then counts as more evidence). Do not state
  what the runs do not show; neutral results within noise are "no-effect" only when the session could have seen the
  effect (low noise).
- proposals: changes to the tuner itself that this session shows are needed (a missing measurement, a wrong decision,
  a rule that fired without evidence, a harness problem). Keep the parts apart: problem = what the runs showed (with
  run numbers); proposal = the concrete change to the tuner, one or two sentences, actionable; expected_effect = what
  would be better and how one would see it.
Runs flagged not_steady, client_saturated, goal_server_mismatch or unreliable are weak evidence: say so or skip them."""

SCHEMA_OUT = {
    "type": "object", "additionalProperties": False, "required": ["insights", "proposals"],
    "properties": {
        "insights": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["statement", "caveats", "settings", "direction", "conditions", "runs", "same_as"],
            "properties": {
                "statement": {"type": "string"}, "caveats": {"type": "string"},
                "settings": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                    "type": "object", "additionalProperties": False, "required": ["variable", "value"],
                    "properties": {"variable": {"type": "string"}, "value": {"type": "string"}}}},
                "direction": {"type": "string", "enum": ["helps", "hurts", "no-effect"]},
                "conditions": {"type": "object", "additionalProperties": False,
                               "required": ["workload", "cpu_class", "concurrency_class", "components"],
                               "properties": {"workload": {"type": "string"}, "cpu_class": {"type": "string"},
                                              "concurrency_class": {"type": "string"},
                                              "components": {"type": "array", "items": {"type": "string"}}}},
                "runs": {"type": "array", "items": {"type": "integer"}},
                "same_as": {"type": "integer"}}}},
        "proposals": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "kind", "problem", "proposal", "expected_effect", "runs"],
            "properties": {"title": {"type": "string"}, "kind": {"type": "string", "enum": ["rule", "measurement", "decision", "harness", "bug"]},
                           "problem": {"type": "string"}, "proposal": {"type": "string"}, "expected_effect": {"type": "string"},
                           "runs": {"type": "array", "items": {"type": "integer"}}}}}},
}


def _ask(dg, known, admin_id):
    st = llm.settings()
    key = llm.service_key()
    if not st.get("configured") or not key:
        raise RuntimeError("the service has no AI key (admin: Account page)")
    payload = _j({"session_digest": dg, "known_insights": known})
    body = {"model": st["model"], "temperature": 0.1, "usage": {"include": True},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "learnings", "strict": True, "schema": SCHEMA_OUT}}}
    t0 = time.time()
    try:
        d = llm._request("POST", llm.OPENROUTER + "/chat/completions", key, body, timeout=240)
        content = json.loads(d["choices"][0]["message"]["content"])
        usage, status, err = d.get("usage") or {}, "ok", None
    except Exception as e:  # noqa
        content, usage, status, err = {"insights": [], "proposals": []}, {}, "error", str(e)[:500]
    store.ex("INSERT INTO llm_calls(user_id, env_id, session_id, ts, model, prompt_hash, status, cached, latency_s, prompt_tokens, "
             "completion_tokens, cost, response, error, context, purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (admin_id, None, dg["session"], time.time(), st["model"], "learn:" + hashlib.sha256(payload.encode()).hexdigest(), status, 0,
              round(time.time() - t0, 2), usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost"),
              json.dumps(content), err, payload, "learn"))
    if status != "ok":
        raise RuntimeError(err)
    return content, float(usage.get("cost") or 0)


# ------------------------------------------------------------------------------------------------ validation
GOOD, BAD = ("improved", "efficiency-gain"), ("regressed", "efficiency-loss")
FLAWED = ("not_steady", "unsteady_verdict", "client_saturated", "goal_server_mismatch_pct", "unstable_within_run")
LEGACY_WEIGHT = 0.5      # evidence from sessions without a recorded agent build (before the fixes of 2026-10-20)
EXPIRE_OBSERVED_DAYS = 14  # an observation that never repeated is archived after this long without new support
EXPIRE_CONTRADICTED_DAYS = 7


def _norm(v):
    return str(v or "").strip().lower().strip("'\"")


def validate(settings, direction, cond):
    """Count runs in the observations that changed these settings to these values under these conditions:
    (support, contradict, neutral, sessions)."""
    names = {_norm(s["variable"]): _norm(s["value"]) for s in settings}
    rows = store.q("SELECT r.session_id, s.env_id, s.agent_version, r.evaluation, r.changes, r.verdict, json_extract(r.inputs, '$.workload.type') wl, "
                   "json_extract(e.facts, '$.hardware.cpus') cpus, json_extract(r.summary, '$.threads_connected_avg') conns "
                   "FROM runs r JOIN tsessions s ON s.id=r.session_id JOIN environments e ON e.id=s.env_id "
                   "WHERE r.status='done' AND r.verdict IN ('improved','efficiency-gain','neutral','regressed','efficiency-loss')")
    per = {}
    for r in rows:
        ch = {_norm(c.get("variable")): _norm(c.get("new")) for c in _uj(r["changes"], []) or []}
        if not names or any(k not in ch for k in names):
            continue
        if any(v and ch.get(k) != v for k, v in names.items()):
            continue
        if cond.get("workload") and r["wl"] and cond["workload"] != r["wl"]:
            continue
        if cond.get("cpu_class") and r["cpus"] and _cpu_class(r["cpus"]) != cond["cpu_class"]:
            continue
        if cond.get("concurrency_class") and r["conns"] and _conc_class(r["conns"]) != cond["concurrency_class"]:
            continue
        ev = _uj(r["evaluation"], {}) or {}
        if any(ev.get(k) for k in FLAWED):
            continue  # the run's own measurement was flawed: it is evidence of nothing
        # sessions from agent builds before build pinning (2026-10-20) ran with the bugs today's reviews found
        e = per.setdefault(r["session_id"], [0, 0, 0, r["env_id"], 1.0 if r["agent_version"] else LEGACY_WEIGHT])
        if direction == "helps":
            e[0] += r["verdict"] in GOOD
            e[1] += r["verdict"] in BAD
            e[2] += r["verdict"] == "neutral"
        elif direction == "hurts":
            e[0] += r["verdict"] in BAD
            e[1] += r["verdict"] in GOOD
            e[2] += r["verdict"] == "neutral"
        else:  # no-effect
            e[0] += r["verdict"] == "neutral"
            e[1] += r["verdict"] in GOOD + BAD
    return per


def _count(c, iid, per):
    """Record this insight's evidence per session in learning.db and return the totals. Sessions that are no longer in
    the observations (a reset, another installation's data) keep the counts recorded when they were seen: learnings
    outlive the observations they came from."""
    for sid, (a, b, n, env, w) in per.items():
        prev = c.execute("SELECT support, contradict, neutral FROM evidence WHERE insight_id=? AND session_id=?", (iid, sid)).fetchone()
        changed = not prev or tuple(prev) != (a, b, n)
        c.execute("INSERT OR REPLACE INTO evidence(insight_id, session_id, support, contradict, neutral, counted, env_id, weight) "
                  "VALUES (?,?,?,?,?,?,?,?)", (iid, sid, a, b, n, time.time(), env, w))
        if changed and a > b:
            c.execute("UPDATE insights SET updated=? WHERE id=?", (time.time(), iid))  # new support keeps it alive
    r = c.execute("SELECT COALESCE(SUM(support * weight),0) s, COALESCE(SUM(contradict * weight),0) c, COALESCE(SUM(neutral * weight),0) n, "
                  "GROUP_CONCAT(session_id) ids, SUM(CASE WHEN support > contradict THEN weight ELSE 0 END) ssess, COUNT(DISTINCT env_id) hosts "
                  "FROM evidence WHERE insight_id=? AND (support + contradict + neutral) > 0", (iid,)).fetchone()
    ids = sorted(int(x) for x in (r["ids"] or "").split(",") if x)
    c.execute("UPDATE insights SET support_sessions=?, hosts=? WHERE id=?", (r["ssess"] or 0, r["hosts"] or 0, iid))
    return round(r["s"], 2), round(r["c"], 2), round(r["n"], 2), ids


def _status(sup, con, support_sessions=0):
    """How well the experiment data repeats - not whether the statement is true:
    observed: seen, but not repeated in another session; repeated: supported in at least 2 sessions and by at least
    2:1 of the runs; contested: runs disagree; contradicted: more runs against than for."""
    n = sup + con
    conf = round((sup + 1) / (n + 2), 3)
    if n >= 3 and con > sup:
        return "contradicted", conf
    if sup >= 2 and support_sessions >= 2 and conf >= 0.67:
        return "repeated", conf
    if sup and con:
        return "contested", conf
    return "observed", conf


def _key(settings, direction, cond):
    st = sorted(f"{_norm(s['variable'])}={_norm(s['value'])}" for s in settings)
    return "|".join(st + [direction, cond.get("workload") or "*", cond.get("cpu_class") or "*", cond.get("concurrency_class") or "*"])


# ------------------------------------------------------------------------------------------------ a pass
def _known(limit=60):
    return [{"id": r["id"], "statement": r["statement"], "direction": r["direction"], "status": r["status"],
             "settings": _uj(r["settings"], []), "conditions": _uj(r["conditions"], {})}
            for r in db().execute("SELECT * FROM insights WHERE status != 'retired' ORDER BY updated DESC LIMIT ?", (limit,))]


def run_pass(trigger="timer", max_sessions=None):
    """One learner pass. Returns the learner_runs row as a dict."""
    with open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"skipped": "another pass is running"}
        c = db()
        rid = c.execute("INSERT INTO learner_runs(started, trigger) VALUES (?,?)", (time.time(), trigger)).lastrowid
        stats = {"sessions": 0, "calls": 0, "cost": 0.0, "insights_new": 0, "insights_updated": 0, "proposals_new": 0}
        err = None
        try:
            admin = store.q1("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")
            day = c.execute("SELECT COALESCE(SUM(calls),0) n FROM learner_runs WHERE started > ?", (time.time() - 86400,)).fetchone()["n"]
            done = {r["session_id"] for r in c.execute("SELECT session_id FROM processed")}
            todo = [r["id"] for r in store.q("SELECT id, name FROM tsessions WHERE status='closed' ORDER BY id")
                    if r["id"] not in done and not str(r["name"] or "").lower().startswith("canary")]
            limit = min(max_sessions or MAX_CALLS_PER_PASS, MAX_CALLS_PER_PASS, max(0, MAX_CALLS_PER_DAY - day))
            for sid in todo[:limit]:
                dg = digest(sid)
                if not dg or len(dg["runs"]) < 3:
                    c.execute("INSERT OR REPLACE INTO processed VALUES (?,?,?)", (sid, time.time(), rid))
                    continue
                res, cost = _ask(dg, _known(), admin["id"] if admin else None)
                stats["calls"] += 1
                stats["cost"] += cost
                stats["sessions"] += 1
                for ins in res.get("insights") or []:
                    new = _store_insight(ins, dg, c)
                    stats["insights_new" if new else "insights_updated"] += 1
                for p in res.get("proposals") or []:
                    c.execute("INSERT INTO proposals(created, title, kind, body, proposal, expected_effect, sessions, status, source) "
                              "VALUES (?,?,?,?,?,?,?,?,?)",
                              (time.time(), p["title"][:300], p.get("kind"), p.get("problem"), p.get("proposal"), p.get("expected_effect"),
                               _j([{"session": sid, "runs": p.get("runs")}]), "open", llm.settings().get("model")))
                    stats["proposals_new"] += 1
                c.execute("INSERT OR REPLACE INTO processed VALUES (?,?,?)", (sid, time.time(), rid))
            revalidate(c)
            stats["archived"] = prune(c)
        except Exception as e:  # noqa - logged in the pass record
            err = str(e)[:500]
        c.execute("UPDATE learner_runs SET ended=?, sessions=?, calls=?, cost=?, insights_new=?, insights_updated=?, proposals_new=?, error=? "
                  "WHERE id=?", (time.time(), stats["sessions"], stats["calls"], round(stats["cost"], 4), stats["insights_new"],
                                 stats["insights_updated"], stats["proposals_new"], err, rid))
        return dict(c.execute("SELECT * FROM learner_runs WHERE id=?", (rid,)).fetchone())


def _store_insight(ins, dg, c):
    cond = dict(ins.get("conditions") or {})
    for k in ("workload", "cpu_class", "concurrency_class"):
        cond[k] = cond.get(k) or dg["conditions"].get(k)
    settings = [s for s in ins.get("settings") or [] if s.get("variable")]
    if not settings:
        return False
    key = _key(settings, ins["direction"], cond)
    row = None
    if ins.get("same_as"):
        row = c.execute("SELECT * FROM insights WHERE id=?", (ins["same_as"],)).fetchone()
    row = row or c.execute("SELECT * FROM insights WHERE key=?", (key,)).fetchone()
    per = validate(settings, ins["direction"], cond)
    new = row is None
    if new:
        iid = c.execute("INSERT INTO insights(key, created, updated, kind, statement, settings, conditions, direction, sessions, status, source) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (key, time.time(), time.time(), "observation", ins["statement"][:1000], _j(settings),
                                                         _j(cond), ins["direction"], "[]", "candidate", llm.settings().get("model"))).lastrowid
    else:
        iid = row["id"]
    sup, con, neu, ids = _count(c, iid, per)
    status, conf = _status(sup, con, c.execute("SELECT support_sessions FROM insights WHERE id=?", (iid,)).fetchone()[0] or 0)
    if ins.get("caveats"):
        c.execute("UPDATE insights SET caveats=? WHERE id=?", (ins["caveats"][:1000], iid))
    if row is not None and row["status"] == "implemented":
        status = "implemented"
    c.execute("UPDATE insights SET updated=?, support=?, contradict=?, neutral=?, confidence=?, status=?, sessions=? WHERE id=?",
              (time.time(), sup, con, neu, conf, status, _j(sorted(set(ids) | {dg["session"]})), iid))
    return new


def prune(c=None, now=None):
    """Keep the learnings from going stale or wrong: archive observations that never repeated, contradicted results,
    and results that rest only on flawed or pre-fix evidence. Archived insights stay for review, never reach agents."""
    c = c or db()
    now = now or time.time()
    n = 0
    for r in c.execute("SELECT id, status, updated, support, support_sessions FROM insights WHERE status IN ('observed','contested','contradicted')").fetchall():
        why = None
        if r["status"] == "contradicted" and now - r["updated"] > EXPIRE_CONTRADICTED_DAYS * 86400:
            why = "contradicted by later runs"
        elif r["status"] in ("observed", "contested") and now - r["updated"] > EXPIRE_OBSERVED_DAYS * 86400:
            why = f"no new support for {EXPIRE_OBSERVED_DAYS} days"
        elif (r["support"] or 0) < 1:
            why = "no usable supporting run (flawed or pre-fix evidence only)"
        if why:
            c.execute("UPDATE insights SET status='archived', notes=? WHERE id=?", (f"archived: {why}", r["id"]))
            n += 1
    return n


def revalidate(c=None):
    """Recount every live insight against the observations (they grow between passes)."""
    c = c or db()
    for r in c.execute("SELECT * FROM insights WHERE status NOT IN ('implemented', 'retired-by-admin', 'archived')").fetchall():
        sup, con, neu, ids = _count(c, r["id"], validate(_uj(r["settings"], []), r["direction"], _uj(r["conditions"], {})))
        status, conf = _status(sup, con, c.execute("SELECT support_sessions FROM insights WHERE id=?", (r["id"],)).fetchone()[0] or 0)
        c.execute("UPDATE insights SET support=?, contradict=?, neutral=?, confidence=?, status=? WHERE id=?",
                  (sup, con, neu, conf, status, r["id"]))


# ------------------------------------------------------------------------------------------------ for agents and admins
def matching(workload=None, cpu_class=None, concurrency_class=None):
    """Live insights whose conditions match (unset conditions match anything)."""
    out = []
    for r in db().execute("SELECT * FROM insights WHERE status IN ('repeated','observed','contested') ORDER BY confidence DESC"):
        cond = _uj(r["conditions"], {}) or {}
        if workload and cond.get("workload") and cond["workload"] != workload:
            continue
        if cpu_class and cond.get("cpu_class") and cond["cpu_class"] != cpu_class:
            continue
        if concurrency_class and cond.get("concurrency_class") and cond["concurrency_class"] != concurrency_class:
            continue
        out.append({"id": r["id"], "statement": r["statement"], "settings": _uj(r["settings"], []), "direction": r["direction"],
                    "conditions": cond, "status": r["status"], "confidence": r["confidence"], "support": r["support"],
                    "contradict": r["contradict"], "sessions": len(_uj(r["sessions"], []) or []), "hosts": r["hosts"],
                    "caveats": r["caveats"]})
    return out[:80]


def overview():
    c = db()
    return {"insights": [dict(r, settings=_uj(r["settings"], []), conditions=_uj(r["conditions"], {}), sessions=_uj(r["sessions"], []))
                         for r in c.execute("SELECT * FROM insights ORDER BY status IN ('contradicted','retired-by-admin','archived'), status = 'archived', "
                                            "status != 'repeated', confidence DESC, updated DESC")],
            "proposals": [dict(r, sessions=_uj(r["sessions"], [])) for r in c.execute("SELECT * FROM proposals ORDER BY status!='open', id DESC")],
            "runs": [dict(r) for r in c.execute("SELECT * FROM learner_runs ORDER BY id DESC LIMIT 20")],
            "processed": c.execute("SELECT COUNT(*) n FROM processed").fetchone()["n"],
            "interval_hours": INTERVAL_H, "db_path": DB_PATH}


RESTRUCTURE = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["id", "problem", "proposal", "expected_effect"],
    "properties": {"id": {"type": "integer"}, "problem": {"type": "string"}, "proposal": {"type": "string"},
                   "expected_effect": {"type": "string"}}}}}}


def restructure_proposals(batch=15):
    """Proposals written before the structured format: split each text into problem / proposed change / expected
    effect (wording only - the same content, nothing added)."""
    c = db()
    rows = c.execute("SELECT id, title, body FROM proposals WHERE proposal IS NULL").fetchall()
    key, st = llm.service_key(), llm.settings()
    if not rows or not key:
        return 0
    done = 0
    for i in range(0, len(rows), batch):
        part = [{"id": r["id"], "title": r["title"], "text": r["body"]} for r in rows[i:i + batch]]
        body = {"model": st["model"], "temperature": 0, "messages": [
            {"role": "system", "content": "Split each proposal text into: problem (what the runs showed, with run numbers), proposal "
                                          "(the concrete change to the tuner, one or two sentences), expected_effect (what would improve). "
                                          "Use only what the text says; do not add claims. If the text has no expected effect, say what "
                                          "the proposal itself implies in a few words."},
            {"role": "user", "content": json.dumps(part)}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "split", "strict": True, "schema": RESTRUCTURE}}}
        try:
            d = llm._request("POST", llm.OPENROUTER + "/chat/completions", key, body, timeout=240)
            for it in json.loads(d["choices"][0]["message"]["content"])["items"]:
                c.execute("UPDATE proposals SET body=?, proposal=?, expected_effect=? WHERE id=? AND proposal IS NULL",
                          (it["problem"], it["proposal"], it["expected_effect"], it["id"]))
                done += 1
        except Exception:
            break
    return done


# ------------------------------------------------------------------------------------------------ background thread
def _loop():
    time.sleep(120)
    try:
        restructure_proposals()
    except Exception:
        pass
    while True:
        try:
            last = db().execute("SELECT MAX(started) t FROM learner_runs WHERE trigger='timer'").fetchone()["t"] or 0
            if time.time() - last >= INTERVAL_H * 3600 and llm.settings().get("configured"):
                run_pass("timer")
        except Exception:
            pass
        time.sleep(300)


def start():
    try:  # a pass cut short by a service restart (deploy) is closed as interrupted; its unread sessions are read next time
        db().execute("UPDATE learner_runs SET ended=?, error=COALESCE(error, 'interrupted (service restart)') "
                     "WHERE ended IS NULL AND started < ?", (time.time(), time.time() - 5))
    except Exception:
        pass
    if INTERVAL_H > 0:
        threading.Thread(target=_loop, name="learner", daemon=True).start()
