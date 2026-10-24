#!/usr/bin/env python3
"""Decision benchmark, step 1: rebuild each labelled decision point from stored runs and record what the CURRENT rule
engine would choose there, plus the planner packet (the evidence an AI planner gets).

usage: build.py <labels.json> [--out ops/dbench/points.jsonl] [--sessions 191,194]
       build.py --sessions 288,294 [--out ...] [--data DIR] [--all]     (backtest: no labels file)

Labels come from the decision audits (one object per decision: session, rec_id, after_run_seq, tuner_pick, expert_pick,
acceptable_ids, class). Without a labels file the decision points are the sessions' own recommendations: the last
recommendation before each run (it chose that run), plus a final stop. What actually happened is recorded as "actual"
(the recommendation's next, the run's label and its changes vs the parent configuration, and who decided: rules or
planner). Mechanical steps are skipped unless --all: runs the agent inserts itself (confirm, repeat, re-check, baseline)
and undo steps that ran as runs (revert / rollback / drop-unearned). Session data is fetched read-only from the server database once and cached in ops/dbench/data/.
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


def session_data(sid, data=DATA):
    f = os.path.join(data, f"s{sid}.json")
    if os.path.exists(f):
        return json.load(open(f))
    os.makedirs(data, exist_ok=True)
    d = {"session": q(f"SELECT s.id, s.name, s.status, s.planner, s.policy, s.goal, s.noise_pct, s.calibration, e.facts FROM tsessions s "
                      f"JOIN environments e ON e.id=s.env_id WHERE s.id={sid}")[0],
         "runs": q(f"SELECT id, seq, label, verdict, goal_value, delta_pct, status, started, ended, config, managed, changes, summary, inputs, "
                   f"evaluation FROM runs WHERE session_id={sid} ORDER BY seq"),
         "recs": q(f"SELECT id, run_id, created, next, phase, decision, llm, items FROM recommendations WHERE session_id={sid} ORDER BY id")}
    for r in d["recs"]:  # keep only the chosen item of the stored list (the rest is rebuilt), and the planner record
        items = J(r.pop("items", None), [])
        r["next_item"] = next(({k: i.get(k) for k in ("id", "title", "kind", "changes", "safety", "source")} for i in items
                               if i.get("id") == r.get("next")), None)
        r["planner"] = (J(r.pop("llm", None), {}) or {}).get("planner")
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
            "blocked": [], "failed": [], "scope": ["knobs", "os"], "managed": dict(hist[-1]["managed"]),
            "bench": {k: b.get(k) for k in ("name", "run", "warmup", "duration", "goal_metric") if b.get(k) is not None}}


UNDO = ("revert", "rollback", "drop-unearned")


def settle(ctx, rounds=4):
    """Like the agent: a revert / rollback / drop of an unearned change is applied first and the engine asked again, so
    the decision is the one made on the configuration then in effect."""
    res = engine.run_engine(ctx)
    for _ in range(rounds):
        nxt = res.get("next") or ""
        if not nxt.startswith(UNDO):
            break
        it = next(i for i in res["items"] if i["id"] == nxt)
        for c in it.get("changes") or []:
            v, new = c["variable"], c.get("proposed")
            if new in (None, "(default)", "DEFAULT"):
                ctx["managed"].pop(v, None)
            else:
                ctx["managed"][v] = new
                ctx["variables"][v] = new
        ctx.setdefault("failed", []).append(nxt)  # not proposed again in this rebuilt decision
        res = engine.run_engine(ctx)
    return res


RUN_METRICS = ("tps", "p95_latency_ms", "cpu_busy", "mysqld_cpu_pct", "threads_running_avg", "mysqld_cpu_ms_per_trx", "disk_util",
               "bp_hit_pct", "dbproc_runq_wait_pct", "psi_cpu_some")


def fuller_packet(ctx, res, pk):
    """The planner packet with more of the session in it (a reference variant): every candidate, every tested hypothesis,
    and per run its label, what it changed against its parent, the verdict vs the parent, the hypothesis it tested and key
    metrics. The production packet's history lists raw changes (reverts mixed in)."""
    f = dict(pk)
    f["candidates"] = [planner._cand(i) for i in res["items"] if i["kind"] in ("config", "revert", "advice") and i.get("changes")]
    f["hypotheses_tested"] = [{k: h.get(k) for k in ("id", "run", "target", "outcome")} for h in ctx.get("hypotheses") or []]
    runs = []
    for r in ctx["history"]:
        ev, sm = r.get("evaluation") or {}, r.get("summary") or {}
        h = ev.get("hypothesis") or {}
        if isinstance(h, str):
            h = {"text": h}
        runs.append({"run": r["seq"], "label": r["label"], "verdict": r["verdict"], "goal": r["goal_value"],
                     "vs_parent_runs": ev.get("parent_runs"), "goal_delta_pct": ev.get("goal_delta_pct"),
                     "efficiency_better": ev.get("efficiency_better"), "efficiency_worse": ev.get("efficiency_worse"),
                     "tested": (h.get("text") or "")[:300] or None, "outcome": h.get("outcome"),
                     "metrics": {k: sm.get(k) for k in RUN_METRICS if sm.get(k) is not None}})
    f["runs_detail"] = runs
    return f


MECHANICAL = ("(confirm", "(repeat", "re-check", "baseline", "server defaults", "calibration", "confirmation")


def session_type(name):
    n = (name or "").lower()
    return ("h2h" if "h2h" in n else "suite" if "suite" in n else "canary" if "canary" in n
            else "external-1024" if "1024thr" in n else "external-64" if "64thr" in n else "other")


def exp_changes(d, run):
    """What a run changed against its parent configuration (the configuration its verdict compares with)."""
    ev = J(run.get("evaluation"), {})
    par = (ev.get("parent_runs") or [None])[0]
    pr = next((r for r in d["runs"] if r["seq"] == par), None)
    m, pm = J(run.get("managed"), {}), J((pr or {}).get("managed"), {})
    out = {k: str(v) for k, v in m.items() if str(pm.get(k)) != str(v)}
    out.update({k: "(default)" for k in pm if k not in m})
    return out


def auto_labels(sid, data, keep_all=False):
    """Decision points of a session from its own recommendations (see the module docstring)."""
    d = session_data(sid, data)
    s = d["session"]
    runs = [r for r in d["runs"] if r["started"]]
    groups = {}
    for rec in d["recs"]:
        if rec["run_id"] is None:
            continue
        nxt = next((r for r in runs if r["started"] > rec["created"]), None)
        groups[nxt["id"] if nxt else None] = (rec, nxt)  # the last recommendation before a run is the one that chose it
    seq_of = {r["id"]: r["seq"] for r in d["runs"]}
    out, skipped = [], {}
    for key, (rec, nxt) in sorted(groups.items(), key=lambda kv: kv[1][0]["id"]):
        if nxt is None and (rec.get("next") or s["status"] != "closed"):
            continue  # nothing followed yet (an active session) - not a decision we can see the outcome of
        lab = (nxt or {}).get("label") or ""
        kind = "stop" if nxt is None else ("mechanical" if lab.lower().startswith(MECHANICAL) or any(m in lab.lower() for m in MECHANICAL[:3])
                                          else "undo" if lab.startswith(UNDO) else "decision")
        if kind in ("mechanical", "undo") and not keep_all:
            skipped[kind] = skipped.get(kind, 0) + 1
            continue
        pl = rec.get("planner") or {}
        dec = J(rec.get("decision"), {}) or {}
        rejected = pl.get("rejected") if str(pl.get("rejected")) not in ("None", "") else None
        # older builds stored no "decision": the planner decided when it answered and its choice passed the checks
        by = dec.get("by") or (f"planner:{pl.get('model')}" if pl.get("plan") and not pl.get("error") and not rejected else "rules")
        out.append({"session": sid, "rec_id": rec["id"], "after_run_seq": seq_of.get(rec["run_id"]), "class": session_type(s["name"]),
                    "point_kind": kind,
                    "actual": {"next": rec.get("next") or None, "next_item": rec.get("next_item"), "by": by,
                               "engine_choice": dec.get("engine_choice") or pl.get("engine_next"),
                               "planner_applied": pl.get("applied"), "planner_rejected": rejected or dec.get("planner_rejected"),
                               "run_seq": (nxt or {}).get("seq"), "run_label": lab or None,
                               "run_changes": exp_changes(d, nxt) if nxt else None}})
    return out, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="?")
    ap.add_argument("--out", default=os.path.join(ROOT, "ops", "dbench", "points.jsonl"))
    ap.add_argument("--sessions")
    ap.add_argument("--data", default=DATA, help="cache directory for the session data fetched from the server")
    ap.add_argument("--all", action="store_true", help="keep mechanical steps (confirm / re-check / undo runs) as decision points")
    ap.add_argument("--full", action="store_true", help="also store a fuller packet (packet_full) for a reference planner run")
    a = ap.parse_args()
    only = {int(x) for x in a.sessions.split(",")} if a.sessions else None
    if a.labels:
        labels = json.load(open(a.labels))
    elif only:
        labels = []
        for sid in sorted(only):
            lbs, sk = auto_labels(sid, a.data, a.all)
            labels += lbs
            print(f"session {sid}: {len(lbs)} decision points" + (f", skipped {sk}" if sk else ""), file=sys.stderr)
    else:
        ap.error("a labels file or --sessions is needed")
    n = 0
    with open(a.out, "w") as out:
        for lb in labels:
            if only and lb["session"] not in only:
                continue
            d = session_data(lb["session"], a.data)
            try:
                ctx = context_at(d, lb["after_run_seq"])
                res = settle(ctx)
            except Exception as e:  # a decision the current engine cannot rebuild is reported, not silently dropped
                print(f"session {lb['session']} after run {lb['after_run_seq']}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            pk = planner.packet(ctx, res)
            try:
                full = fuller_packet(ctx, res, pk) if a.full else None
            except Exception as e:  # the reference packet is optional
                print(f"session {lb['session']} rec {lb['rec_id']}: fuller packet: {type(e).__name__}: {e}", file=sys.stderr)
                full = None
            it = next((i for i in res["items"] if i["id"] == res.get("next")), None) or {}
            out.write(json.dumps({"label": lb, "rules": {"next": res.get("next"), "stop_reason": res.get("stop_reason"),
                                                          "changes": [f"{x['variable']}: {x.get('current')} -> {x['proposed']}" for x in it.get("changes") or []],
                                                          "settled": [f for f in ctx.get("failed") or [] if f.startswith(UNDO)],
                                                          "top": [i["id"] for i in res["items"] if i["kind"] in ("config", "revert") and i.get("confidence", 0) > 0][:5]},
                                  "packet": pk, **({"packet_full": full} if full else {})}, default=str) + "\n")
            n += 1
    print(f"{n} decision points -> {a.out}")


if __name__ == "__main__":
    main()
