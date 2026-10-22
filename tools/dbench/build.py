#!/usr/bin/env python3
"""Decision benchmark, step 1: rebuild each labelled decision point from stored runs and record what the CURRENT rule
engine would choose there, plus the planner packet (the evidence an AI planner gets).

usage: build.py <labels.json> [--out ops/dbench/points.jsonl] [--sessions 191,194]

Labels come from the decision audits (one object per decision: session, rec_id, after_run_seq, tuner_pick, expert_pick,
acceptable_ids, class). Session data is fetched read-only from the server database once and cached in ops/dbench/data/.
What the planner sees is limited to what was known then: runs up to the decision, their hypotheses and noise. Experience
from other sessions is left out (it would include later sessions); the engine gets the same, so the comparison is fair.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from tuning_agent import bottleneck, engine, planner  # noqa: E402

DATA = os.path.join(ROOT, "ops", "dbench", "data")


def q(sql):
    out = subprocess.run(["ssh", "-F", os.path.join(ROOT, "ops", "ssh_config"), "atweb",
                          f"sqlite3 -readonly -json /var/lib/aituner/tuner.db \"{sql}\""], capture_output=True, text=True, check=True).stdout
    return json.loads(out or "[]")


def session_data(sid):
    f = os.path.join(DATA, f"s{sid}.json")
    if os.path.exists(f):
        return json.load(open(f))
    d = {"session": q(f"SELECT s.id, s.policy, s.goal, s.noise_pct, s.calibration, e.facts FROM tsessions s JOIN environments e ON e.id=s.env_id WHERE s.id={sid}")[0],
         "runs": q(f"SELECT id, seq, label, verdict, goal_value, status, config, managed, changes, summary, inputs, evaluation FROM runs "
                   f"WHERE session_id={sid} ORDER BY seq"),
         "recs": q(f"SELECT id, run_id, next FROM recommendations WHERE session_id={sid} ORDER BY id")}
    json.dump(d, open(f, "w"))
    return d


def J(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def context_at(d, upto):
    """The engine context after run `upto`, from what was stored then (like the agent's _engine_ctx)."""
    s = d["session"]
    facts = J(s["facts"], {})
    rows = [r for r in d["runs"] if r["status"] == "done" and r["seq"] <= upto]
    hist, hyps = [], []
    for r in rows:
        ev = J(r["evaluation"], {})
        hist.append({"id": r["id"], "seq": r["seq"], "label": r["label"], "verdict": r["verdict"], "goal_value": r["goal_value"],
                     "managed": J(r["managed"], {}), "changes": J(r["changes"], []), "evaluation": ev, "summary": J(r["summary"], {})})
        h = ev.get("hypothesis") or {}
        if h.get("id") and h.get("outcome"):
            hyps.append({"id": h["id"], "run": r["seq"], "kind": h.get("kind"), "target": h.get("target"), "tags": h.get("tags"), "outcome": h["outcome"]})
    last = rows[-1]
    inp = J(last["inputs"], {})
    for r in reversed(rows):  # profiles missing from the last run come from the most recent profiled run (as the agent does)
        ri = J(r["inputs"], {})
        for k, f in (("offcpu", "foreground"), ("cpu_profile", "categories")):
            if not (inp.get(k) or {}).get(f) and (ri.get(k) or {}).get(f):
                inp[k] = ri[k]
                inp.setdefault("profile_basis", {})[k] = r["seq"]
    summ = J(last["summary"], {})
    hw = facts.get("hardware") or {}
    inp["time_breakdown"] = bottleneck.breakdown(summ, inp, hw.get("cpus"))
    noise = (J(last["evaluation"], {}) or {}).get("goal_noise_pct") or s["noise_pct"] or (J(s["calibration"], {}) or {}).get("noise_pct") or 2.0
    variables = J(last["config"], {})
    ver = tuple(int(x) for x in str(variables.get("version") or (facts.get("db") or {}).get("version") or "8.4.0").split("-")[0].split(".")[:3])
    flavor = (facts.get("db") or {}).get("flavor") or "percona"
    b = inp.get("bench") or {}
    return {"goal": s["goal"] or "tps", "policy": s["policy"] or "safe", "os_tuning": True, "os_facts": {}, "hw": hw, "flavor": flavor,
            "version": ver, "variables": variables, "summary": summ, "inputs": inp, "history": hist, "noise_pct": float(noise),
            "noise": None, "hypotheses": hyps, "hypothesis_experience": {}, "experience": {}, "insights": [], "param_impact": {},
            "blocked": [], "failed": [], "scope": ["knobs", "os"], "managed": hist[-1]["managed"],
            "bench": {k: b.get(k) for k in ("name", "run", "warmup", "duration", "goal_metric") if b.get(k) is not None}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels")
    ap.add_argument("--out", default=os.path.join(ROOT, "ops", "dbench", "points.jsonl"))
    ap.add_argument("--sessions")
    a = ap.parse_args()
    labels = json.load(open(a.labels))
    only = {int(x) for x in a.sessions.split(",")} if a.sessions else None
    n = 0
    with open(a.out, "w") as out:
        for lb in labels:
            if only and lb["session"] not in only:
                continue
            d = session_data(lb["session"])
            try:
                ctx = context_at(d, lb["after_run_seq"])
                res = engine.run_engine(ctx)
            except Exception as e:  # a decision the current engine cannot rebuild is reported, not silently dropped
                print(f"session {lb['session']} after run {lb['after_run_seq']}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            pk = planner.packet(ctx, res)
            out.write(json.dumps({"label": lb, "rules": {"next": res.get("next"), "stop_reason": res.get("stop_reason"),
                                                          "top": [i["id"] for i in res["items"] if i["kind"] in ("config", "revert") and i.get("confidence", 0) > 0][:5]},
                                  "packet": pk}, default=str) + "\n")
            n += 1
    print(f"{n} decision points -> {a.out}")


if __name__ == "__main__":
    main()
