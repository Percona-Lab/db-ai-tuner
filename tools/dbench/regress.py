#!/usr/bin/env python3
"""Decision regression check: score an agent build on a frozen corpus of real past decisions, measured outcomes first.

usage:
  regress.py add [--points FILE] [--labels FILE] [--sessions 311,315] [--data DIR ...] [--dry-run] [--note TEXT]
  regress.py score [--agent DIR] [--build NAME] [--against BUILD|PREFIX*] [--tolerance 0.10] [--no-save] [--accept]
                   [--planner --budget USD [--model M] [--effort high] [--limit N] [--planner-dry-run]]
  regress.py baselines

Corpus (ops/dbench/corpus/): decision points with the engine context rebuilt from what was known then (build.py's
fairness rule: runs up to the point, no cross-session experience), in append-only parts; manifest.json lists the
versions. `add` without arguments adds every closed session (scripted pipeline studies excluded) that ended since the
last add; points already in the corpus never change. Each add rebuilds the outcome table from production data
(read-only): per session the measured result of every experiment (its changes against its parent configuration,
verdict, goal delta, noise), and which sessions stand in for each other (same benchmark and data, concurrency per vCPU,
CPU class, plus the backtest's explicit groups).

Scoring runs the build's rule engine on every point (offline, free) and scores its choice by what was measured:
- a run pick: the goal delta of the first exact measurement of its settings (same session after the point, else a
  comparable session); unmeasured picks count 0 and lower the coverage. Unreliable runs are not measurements.
- a STOP: good when no later run of the session improved the goal beyond the noise; bad otherwise, scored minus the
  largest missed win. Runs saved: the runs that followed the first STOP of each session.
- regressed picks (count and size), policy violations (a trade-off chosen under policy=safe, an advice item run).
outcome score = sum of (clipped) measured deltas and missed wins / all points: the expected gain per decision.
Secondary: agreement with the previous build, every changed decision with its known outcome, expert-label agreement on
the audited points. A REGRESSION (exit 1) is an outcome-score drop beyond --tolerance, more regressed picks, or new policy
violations against the latest accepted baseline (ops/dbench/regress/<build>.json), compared on the current outcome table.

--planner adds the AI planner's choices (as answered, and effective after planner.check), asked through run_remote.py on
the tuner server with a spending cap; answers are cached per (point, packet hash, model, effort, server prompt).
"""
import argparse
import collections
import copy
import datetime
import glob
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import backtest  # noqa: E402  (no agent imports at module level: the build under test is chosen by --agent)

FORMAT = 1
CLIP = 30.0           # one +65% win must not hide twenty decisions: deltas count up to +-30%
NOT_MEASURED = ("unreliable", "failed", "reference", "repeat")
UNDO = ("revert", "rollback", "drop-unearned")
PLANNER_MODEL = "anthropic/claude-opus-5.5"
PLANNER_COST = 0.11   # USD per planner answer at high effort (backtest 2026-10-24)


# ---------------------------------------------------------------------------------------------------- paths / io
def paths(ops):
    d = os.path.join(ops, "dbench")
    return {"corpus": os.path.join(d, "corpus"), "out": os.path.join(d, "regress"), "data": os.path.join(d, "corpus", "data"),
            "ssh": os.path.join(ops, "ssh_config"), "cache": os.path.join(d, "regress", "planner-cache.jsonl")}


def jload(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as f:
        return json.load(f)


def jdump(obj, path):
    op = gzip.open if path.endswith(".gz") else open
    tmp = path + ".tmp"
    with op(tmp, "wt") as f:
        json.dump(obj, f, default=str)
    os.replace(tmp, path)


def manifest(cdir):
    f = os.path.join(cdir, "manifest.json")
    if os.path.exists(f):
        return json.load(open(f))
    return {"format": FORMAT, "versions": [], "explicit_groups": [sorted(g) for g in backtest.COMPARABLE], "last_add_at": 0}


def load_points(cdir, man):
    pts = []
    for v in man["versions"]:
        with gzip.open(os.path.join(cdir, v["part"]), "rt") as f:
            pts += [json.loads(x) for x in f]
    return pts


def load_outcomes(cdir, man):
    return jload(os.path.join(cdir, man["versions"][-1]["outcomes"]))


# ---------------------------------------------------------------------------------------------------- choices
def norm_changes(changes):
    """Item changes ([{variable, proposed}] or ['var: a -> b']) -> {variable: normalised value} (backtest's matching)."""
    return backtest.parse_changes(changes)


def choice_key(cid, chg):
    return ("CHG:" + json.dumps(sorted(chg.items()))) if chg else ("ID:" + str(cid))


def stop_choice(reason=None):
    return {"key": "STOP", "id": None, "changes": {}, "raw": [], "safety": None, "kind": None, "reason": reason}


def item_choice(it):
    raw = [[c["variable"], c.get("proposed", c.get("value"))] for c in it.get("changes") or []]
    chg = norm_changes(it.get("changes"))
    return {"key": choice_key(it["id"], chg), "id": it["id"], "changes": chg, "raw": raw, "safety": it.get("safety"), "kind": it.get("kind")}


def text_of(ch):
    if not ch or ch["key"] == "STOP":
        return "STOP"
    s = ", ".join(f"{k}={v}" for k, v in sorted(ch["changes"].items()))
    return f"{ch.get('id') or 'changes'}" + (f" ({s})" if s else "")


def policy_violation(ch, policy, tradeoff_of=None):
    """Why a choice could not run under the session's policy, or None. Trade-offs run only under policy=benchmark."""
    if not ch or ch["key"] == "STOP":
        return None
    if ch.get("kind") not in (None, "config", "revert"):
        return f"{ch['id']} is {ch['kind']}, not runnable"
    if policy == "benchmark":
        return None
    if ch.get("safety") == "trade-off":
        return f"{ch['id']} is a trade-off under policy={policy}"
    for var, val in ch.get("raw") or []:
        t = tradeoff_of(var, val) if tradeoff_of else None
        if t:
            return f"{var}={val} is a {t['category']} trade-off under policy={policy}"
    return None


# ---------------------------------------------------------------------------------------------------- outcomes
def measured_of(chg, sid, after, out):
    """The measured result of exactly these settings: same session after the point first, else the first comparable
    session (backtest hindsight; a run may carry a revert along). None when never measured."""
    if not chg:
        return None
    want = set(chg)
    sessions = out["sessions"]
    group = next((g for g in out["groups"] if sid in g), [sid])
    for s2 in sorted(group, key=lambda x: (x != sid, x)):
        for r in (sessions.get(str(s2)) or {}).get("runs", []):
            if s2 == sid and r["seq"] <= after:
                continue
            if r["verdict"] in NOT_MEASURED:
                continue
            if want <= set(r["changes"]) and all(r["changes"].get(k) == v for k, v in chg.items()):
                return dict(r, session=s2, where="same" if s2 == sid else "comparable")
    return None


def stop_outcome(sid, after, out):
    """What followed a STOP in its session: later runs, and later confirmed wins beyond the noise (the missed gain)."""
    s = out["sessions"].get(str(sid)) or {}
    later = [r for r in s.get("runs", []) if r["seq"] > after and r["verdict"] not in NOT_MEASURED]
    wins = [r for r in later if r["verdict"] == "improved" and r["delta"] > (r.get("noise") or s.get("noise") or 2.0)]
    return {"runs_after": sum(1 for q in s.get("done_seqs", []) if q > after),
            "wins": [{"seq": r["seq"], "label": r["label"], "delta": r["delta"]} for r in sorted(wins, key=lambda r: -r["delta"])]}


def clip(x):
    return max(-CLIP, min(CLIP, x))


def evaluate(ch, pt, out):
    """Outcome of one choice at one point: {value, known, measured | stop}."""
    sid, after = pt["session"], pt["after"]
    if ch is None:
        return {"value": 0.0, "known": False, "kind": "none"}
    if ch["key"] == "STOP":
        so = stop_outcome(sid, after, out)
        miss = so["wins"][0]["delta"] if so["wins"] else 0.0
        return {"value": -clip(miss), "known": True, "kind": "stop", "good": not so["wins"], "missed": miss, **so}
    m = measured_of(ch["changes"], sid, after, out)
    if not m:
        return {"value": 0.0, "known": False, "kind": "run"}
    return {"value": clip(m["delta"]), "known": True, "kind": "run", "verdict": m["verdict"], "delta": m["delta"],
            "where": m["where"], "at": f"{m['session']} r{m['seq']}", "label": m["label"]}


def metrics(choices, pts, out):
    """Scores of one decision maker's choices ({point id: choice}) on the points given."""
    ev = {}
    for p in pts:
        ch = choices.get(str(p["id"]))
        ev[str(p["id"])] = evaluate(ch, p, out) if str(p["id"]) in choices else None
    rows = [(p, choices[str(p["id"])], ev[str(p["id"])]) for p in pts if ev[str(p["id"])] is not None]
    n = len(rows)
    runs = [(p, c, e) for p, c, e in rows if c and c["key"] != "STOP"]
    stops = [(p, c, e) for p, c, e in rows if c and c["key"] == "STOP"]
    meas = [e for _, _, e in runs if e["known"]]
    regs = [(p, c, e) for p, c, e in runs if e["known"] and e["verdict"] == "regressed"]
    bad = [(p, c, e) for p, c, e in stops if not e["good"]]
    # session view: the agent ends a session at its first STOP
    first = {}
    for p, c, e in sorted(stops, key=lambda x: (x[0]["session"], x[0]["after"])):
        first.setdefault(p["session"], (p, e))
    viol = [(p, c) for p, c, e in rows if c and c.get("violation")]
    exp = [c["expert_ok"] for _, c, _ in rows if c and c.get("expert_ok") is not None]
    src = collections.defaultdict(list)
    for p, _, e in rows:
        src[p.get("source") or "?"].append(e["value"])
    return {
        "points": n, "run_picks": len(runs), "stops": len(stops), "no_answer": sum(1 for _, c, _ in rows if c is None),
        "outcome_score": round(sum(e["value"] for _, _, e in rows) / max(1, n), 3),
        "coverage_pct": round(100 * (len(meas) + len(stops)) / max(1, n), 1),
        "measured_picks": len(meas), "mean_measured_delta": round(sum(e["delta"] for e in meas) / len(meas), 2) if meas else None,
        "improved": sum(1 for e in meas if e["verdict"] == "improved"),
        "regressed": len(regs), "regressed_sum": round(sum(e["delta"] for _, _, e in regs), 2),
        "stop_good": len(stops) - len(bad), "stop_bad": len(bad), "stop_missed_sum": round(sum(e["missed"] for _, _, e in bad), 2),
        "sessions_stopped": len(first), "runs_saved": sum(e["runs_after"] for _, e in first.values()),
        "first_stop_missed": [[p["session"], p["after"], w["seq"], w["label"], w["delta"]] for p, e in first.values() for w in e["wins"][:1]],
        "violations": len(viol), "violation_list": [[p["session"], p["id"], c["violation"]] for p, c in viol],
        "regressed_list": [[p["session"], p["id"], text_of(c), e["delta"], e["at"]] for p, c, e in regs],
        "expert": [sum(exp), len(exp)] if exp else None,
        "by_source": {k: round(sum(v) / len(v), 3) for k, v in sorted(src.items())},
    }


# ---------------------------------------------------------------------------------------------------- engine (build under test)
def load_agent(agent_dir):
    agent_dir = os.path.abspath(agent_dir)
    if not os.path.isdir(os.path.join(agent_dir, "tuning_agent")):
        sys.exit(f"no tuning_agent package in {agent_dir}")
    sys.path.insert(0, agent_dir)
    import tuning_agent
    from tuning_agent import bottleneck, engine, knowledge, planner
    if not os.path.abspath(tuning_agent.__file__).startswith(agent_dir):
        sys.exit(f"tuning_agent imported from {tuning_agent.__file__}, not {agent_dir}")
    return {"engine": engine, "planner": planner, "bottleneck": bottleneck, "tradeoff_of": knowledge.tradeoff_of}


def restore(ctx, ag):
    c = copy.deepcopy(ctx)
    c["version"] = tuple(c.get("version") or (8, 4, 0))
    inp = c.get("inputs") or {}
    try:  # the build's own time breakdown (part of the build, like the agent computes it)
        inp["time_breakdown"] = ag["bottleneck"].breakdown(c.get("summary"), inp, (c.get("hw") or {}).get("cpus"))
    except Exception:
        pass
    return c


def settle(ctx, engine, rounds=4):
    """As build.settle / the agent: an undo step is applied first and the engine asked again."""
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
        ctx.setdefault("failed", []).append(nxt)
        res = engine.run_engine(ctx)
    return res


def rules_choice(pt, ag):
    ctx = restore(pt["ctx"], ag)
    res = settle(ctx, ag["engine"])
    if not res.get("next"):
        ch = stop_choice((res.get("stop_reason") or "")[:200] or None)
    else:
        ch = item_choice(next(i for i in res["items"] if i["id"] == res["next"]))
    ch["violation"] = policy_violation(ch, ctx.get("policy"), ag["tradeoff_of"])
    return ch, ctx, res


def expert_ok(pt, ch, res):
    """Agreement with the audit's expert label (score.py's rule) for the audited points; None elsewhere."""
    if not pt.get("expert") or pt["expert"].get("uncertain") or ch is None:
        return None
    import score as expert
    cands = {i["id"]: i for i in res["items"]}
    return bool(expert._matches({"action": "stop" if ch["key"] == "STOP" else "run", "id": ch.get("id"), "vars": set(ch["changes"]) or None},
                                pt["expert"], cands))


# ---------------------------------------------------------------------------------------------------- planner (optional, paid)
def server_prompt_tag(P):
    r = subprocess.run(["ssh", "-F", P["ssh"], "atweb", "sha256sum /opt/aituner/server/app/llm.py"], capture_output=True, text=True, check=True)
    return r.stdout.split()[0][:12]


def ask_planner(P, todo, model, effort, budget):
    """Points without a cached answer -> run_remote.py on the tuner server (the key stays there), capped at budget USD."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rdir = "/root/dbench/regress"
    with tempfile.TemporaryDirectory() as tmp:
        pf = os.path.join(tmp, f"pts-{stamp}.jsonl")
        with open(pf, "w") as f:
            for pid, sid, pk in todo:
                f.write(json.dumps({"label": {"rec_id": pid, "session": sid}, "packet": pk}, default=str) + "\n")
        subprocess.run(["ssh", "-F", P["ssh"], "atweb", f"mkdir -p {rdir}"], check=True)
        subprocess.run(["scp", "-q", "-F", P["ssh"], pf, os.path.join(HERE, "run_remote.py"), f"atweb:{rdir}/"], check=True)
        cmd = (f"cd {rdir} && set -a && . /etc/aituner.env && set +a && /opt/aituner/venv/bin/python run_remote.py pts-{stamp}.jsonl "
               f"out-{stamp}.jsonl --variant regress={model} --effort {effort} --budget {budget:.2f} --workers 4")
        r = subprocess.run(["ssh", "-F", P["ssh"], "atweb", cmd], capture_output=True, text=True)
        print("\n".join(x for x in r.stdout.splitlines() if x.startswith("spent")), file=sys.stderr)
        of = os.path.join(tmp, "out.jsonl")
        if subprocess.run(["scp", "-q", "-F", P["ssh"], f"atweb:{rdir}/out-{stamp}.jsonl", of]).returncode != 0:
            print(f"planner: no answers ({r.stderr.strip()[-300:]})", file=sys.stderr)
            return []
        return [json.loads(x) for x in open(of)]


def planner_choices(pts, ag, P, a, ctxs):
    """{point id: (as answered, effective)}; answers cached per (point, packet hash, model, effort, server prompt)."""
    planner = ag["planner"]
    tag = server_prompt_tag(P)
    cache = {}
    if os.path.exists(P["cache"]):
        for line in open(P["cache"]):
            r = json.loads(line)
            cache[r["key"]] = r
    packets, todo = {}, []
    for p in pts:
        ctx, res = ctxs[str(p["id"])]
        pk = json.loads(json.dumps(planner.packet(ctx, res), default=str))
        h = hashlib.sha256(json.dumps(pk, sort_keys=True).encode()).hexdigest()[:16]
        k = f"{p['id']}|{h}|{a.model}|{a.effort}|{tag}"
        packets[str(p["id"])] = (pk, k)
        if k not in cache:
            todo.append((p["id"], p["session"], pk))
    missing = len(todo)
    if a.limit is not None:
        todo = todo[:a.limit]
    print(f"planner: {len(pts) - missing} cached, {missing} not; asking {0 if a.planner_dry_run else len(todo)} "
          f"(~${PLANNER_COST * len(todo):.2f} at ${PLANNER_COST}/answer, cap ${a.budget:.2f})", file=sys.stderr)
    if todo and not a.planner_dry_run:
        if a.budget <= 0:
            sys.exit("--planner needs --budget USD (> 0) to ask the planner")
        keys = {pid: packets[str(pid)][1] for pid, _, _ in todo}
        os.makedirs(os.path.dirname(P["cache"]), exist_ok=True)
        with open(P["cache"], "a") as f:
            for r in ask_planner(P, todo, a.model, a.effort, a.budget):
                if r["result"].get("plan") and not r["result"].get("error"):
                    row = {"key": keys[r["rec_id"]], "id": r["rec_id"], "model": a.model, "effort": a.effort, "plan": r["result"]["plan"],
                           "cost": r.get("cost"), "at": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())}
                    cache[row["key"]] = row
                    f.write(json.dumps(row) + "\n")
                else:
                    print(f"planner rec {r['rec_id']}: {(r['result'].get('error') or 'no plan')[:120]}", file=sys.stderr)
    out = {}
    for p in pts:
        pid = str(p["id"])
        pk, k = packets[pid]
        row = cache.get(k)
        if not row:
            continue
        ctx, res = ctxs[pid]
        plan = row["plan"]
        o = backtest.option_of({"packet": pk, "label": {}}, "planner", {"result": {"plan": plan}})
        items = {i["id"]: i for i in res["items"]}
        if o is None:
            asked = None
        elif o["key"] == "STOP":
            asked = stop_choice("planner")
        elif o["id"] in items:
            asked = item_choice(items[o["id"]])
        else:
            raw = [[c.get("variable"), c.get("value")] for c in ((plan.get("choice") or {}).get("changes") or [])]
            asked = {"key": o["key"], "id": o["id"] or "plan", "changes": o["changes"], "raw": raw, "safety": None, "kind": None}
        if asked:
            asked["violation"] = policy_violation(asked, ctx.get("policy"), ag["tradeoff_of"])
            asked["expert_ok"] = expert_ok(p, asked, res)
        c2, r2 = copy.deepcopy(ctx), copy.deepcopy(res)
        c2["failed"] = [f for f in c2.get("failed") or [] if not f.startswith(UNDO)]  # settle's undo steps did not fail
        try:
            nxt, added, why = planner.check(dict(plan, _model=a.model), c2, r2)
        except Exception as e:  # a check that crashes is reported as a rejection
            nxt, added, why = r2.get("next"), [], f"check failed: {type(e).__name__}: {e}"
        its = {i["id"]: i for i in r2["items"] + added}
        eff = stop_choice("planner") if nxt is None else item_choice(its[nxt]) if nxt in its else stop_choice()
        eff["violation"] = policy_violation(eff, ctx.get("policy"), ag["tradeoff_of"])
        eff["rejected"] = why
        eff["expert_ok"] = expert_ok(p, eff, res)
        out[pid] = (asked, eff)
    return out


# ---------------------------------------------------------------------------------------------------- baselines / comparison
def git_build(agent_dir):
    d = os.path.dirname(os.path.abspath(agent_dir))
    try:
        sha = subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", d, "status", "--porcelain", "--", "agent"], capture_output=True, text=True).stdout.strip()
        return sha, sha + ("-dirty" if dirty else "")
    except (subprocess.CalledProcessError, OSError):
        return None, "unknown"


def baselines(out_dir):
    res = []
    for f in glob.glob(os.path.join(out_dir, "*.json")):
        try:
            b = json.load(open(f))
        except ValueError:
            continue
        if b.get("kind") == "regress-baseline":
            res.append(b)
    return sorted(res, key=lambda b: b["created"])


def pick_baseline(out_dir, build, against):
    bs = [b for b in baselines(out_dir) if b["build"] != build]
    if against and not against.endswith("*"):  # a baseline named explicitly, flagged or not
        bs = [b for b in bs if b["build"] == against]
        return bs[-1] if bs else None
    bs = [b for b in bs if b.get("accepted") and (not against or b["build"].startswith(against[:-1]))]
    return bs[-1] if bs else None


def compare(cur, prev, pts, out, tol, label="rules"):
    """Both builds' choices scored on the current outcome table over the points both answered."""
    common = [p for p in pts if str(p["id"]) in cur and str(p["id"]) in prev]
    mc, mp = metrics(cur, common, out), metrics(prev, common, out)
    flags = []
    if mc["outcome_score"] < mp["outcome_score"] - tol:
        flags.append(f"{label}: outcome score {mp['outcome_score']:+.3f} -> {mc['outcome_score']:+.3f} (drop beyond tolerance {tol})")
    if mc["regressed"] > mp["regressed"]:
        flags.append(f"{label}: picks of options measured as regressions {mp['regressed']} -> {mc['regressed']}")
    if mc["violations"] > mp["violations"]:
        flags.append(f"{label}: policy violations {mp['violations']} -> {mc['violations']}")
    changed = []
    for p in common:
        a, b = prev[str(p["id"])], cur[str(p["id"])]
        if (a or {}).get("key") != (b or {}).get("key"):
            ea, eb = evaluate(a, p, out), evaluate(b, p, out)
            changed.append({"session": p["session"], "id": p["id"], "after": p["after"], "class": p.get("class"),
                            "prev": text_of(a), "prev_outcome": outcome_text(ea), "cur": text_of(b), "cur_outcome": outcome_text(eb),
                            "gain": round(eb["value"] - ea["value"], 2)})
    agree = 100 * (len(common) - len(changed)) / max(1, len(common))
    return {"common": len(common), "agreement_pct": round(agree, 1), "cur": mc, "prev": mp, "flags": flags, "changed": changed}


def outcome_text(e):
    if not e or e["kind"] == "none":
        return "no answer"
    if e["kind"] == "stop":
        return (f"good stop ({e['runs_after']} runs after, no win)" if e["good"]
                else f"missed {e['wins'][0]['delta']:+.1f}% (r{e['wins'][0]['seq']} {e['wins'][0]['label'][:40]}; {e['runs_after']} runs after)")
    if not e["known"]:
        return "not measured"
    return f"{e['verdict']} {e['delta']:+.1f}% ({e['where']} {e['at']})"


# ---------------------------------------------------------------------------------------------------- report
ROWS = [("outcome score (pp / decision)", "outcome_score", "{:+.3f}"), ("  by source", "by_source", None), ("coverage (measured or stop)", "coverage_pct", "{:.1f}%"),
        ("run picks / STOPs", None, None), ("measured picks: mean delta", "mean_measured_delta", "{:+.2f}%"),
        ("measured picks: improved / regressed", None, None), ("regressed picks: sum of deltas", "regressed_sum", "{:+.1f}%"),
        ("STOPs good / bad", None, None), ("missed by bad STOPs (sum)", "stop_missed_sum", "{:.1f}%"),
        ("sessions stopped / runs saved", None, None), ("policy violations", "violations", "{}"), ("expert agreement (audited points)", "expert", None)]


def _cell(m, key, fmt, label):
    if m is None:
        return "-"
    if label.startswith("run picks"):
        return f"{m['run_picks']} / {m['stops']}"
    if label.startswith("measured picks: improved"):
        return f"{m['improved']} / {m['regressed']} of {m['measured_picks']}"
    if label.startswith("STOPs good"):
        return f"{m['stop_good']} / {m['stop_bad']}"
    if label.startswith("sessions stopped"):
        return f"{m['sessions_stopped']} / {m['runs_saved']}"
    if key == "by_source":
        return ", ".join(f"{k.split('-')[0]} {v:+.2f}" for k, v in (m.get("by_source") or {}).items())
    if key == "expert":
        e = m.get("expert")
        return f"{e[0]}/{e[1]} ({100 * e[0] / max(1, e[1]):.0f}%)" if e else "-"
    v = m.get(key)
    return fmt.format(v) if v is not None else "-"


def report(summary, cmp_, pcmp, cols):
    L = [f"# Decision regression check: {summary['build']}", "",
         f"Corpus v{summary['corpus_version']} ({summary['points']} decision points, {summary['sessions']} sessions), outcome table "
         f"v{summary['outcomes_version']}; agent {summary['agent_dir']}; {summary['created']}."]
    if cmp_:
        L.append(f"Compared with **{cmp_['against']}** on {cmp_['common']} common points: {cmp_['agreement_pct']:.0f}% same decisions.")
    L += ["", "| | " + " | ".join(n for n, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    for label, key, fmt in ROWS:
        L.append(f"| {label} | " + " | ".join(_cell(m, key, fmt, label) for _, m in cols) + " |")
    flags = (cmp_ or {}).get("flags", []) + (pcmp or {}).get("flags", [])
    L += ["", "**Result: " + ("REGRESSION** - " + "; ".join(flags) if flags else "OK**" + ("" if cmp_ else " (no earlier baseline)"))]
    m = summary["rules"]
    if m["violation_list"]:
        L += ["", "## Policy violations", ""] + [f"- s{s} rec {i}: {v}" for s, i, v in m["violation_list"]]
    if m["regressed_list"]:
        L += ["", "## Picks measured as regressions", ""] + [f"- s{s} rec {i}: {t} -> {d:+.1f}% ({at})" for s, i, t, d, at in m["regressed_list"]]
    if m["first_stop_missed"]:
        L += ["", "## Wins after a session's first STOP", ""] + [f"- s{s} stop after r{a}: r{q} {lab} {d:+.1f}%" for s, a, q, lab, d in m["first_stop_missed"]]
    for name, c in (("rules", cmp_), ("planner (effective)", pcmp)):
        if not c or not c["changed"]:
            continue
        ch = sorted(c["changed"], key=lambda x: (x["gain"] == 0, x["gain"]))
        known = [x for x in ch if x["gain"] != 0]
        L += ["", f"## Changed decisions, {name} ({len(ch)}; {len(known)} with a measured difference, net {sum(x['gain'] for x in ch):+.1f} pp)", "",
              f"| session / rec | after | {c['against']} | outcome | this build | outcome |", "|---|---|---|---|---|---|"]
        for x in ch:
            L.append(f"| s{x['session']} / {x['id']} | r{x['after']} | {x['prev'][:60]} | {x['prev_outcome']} | {x['cur'][:60]} | {x['cur_outcome']} |")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------------- score
def cmd_score(a):
    P = paths(a.ops)
    man = manifest(P["corpus"])
    if not man["versions"]:
        sys.exit("empty corpus: run `regress.py add` first")
    ag = load_agent(a.agent)
    commit, auto = git_build(a.agent)
    build = re.sub(r"[^A-Za-z0-9._-]", "_", a.build or auto)
    pts = load_points(P["corpus"], man)
    out = load_outcomes(P["corpus"], man)
    rules, ctxs, errors = {}, {}, []
    t0 = time.time()
    for p in pts:
        try:
            ch, ctx, res = rules_choice(p, ag)
        except Exception as e:  # a point the build cannot decide on is reported (and counted as no answer)
            errors.append(f"s{p['session']} rec {p['id']}: {type(e).__name__}: {e}")
            rules[str(p["id"])] = None
            continue
        rules[str(p["id"])] = ch
        ctxs[str(p["id"])] = (ctx, res)
        ch["expert_ok"] = expert_ok(p, ch, res)
    m = metrics(rules, pts, out)
    actual = {str(p["id"]): p["actual"] for p in pts}
    ma = metrics(actual, pts, out)
    summary = {"kind": "regress-baseline", "build": build, "commit": commit, "agent_dir": os.path.abspath(a.agent),
               "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "corpus_version": man["versions"][-1]["version"],
               "outcomes_version": out["version"], "points": len(pts), "sessions": len({p["session"] for p in pts}),
               "seconds": round(time.time() - t0, 1), "errors": errors, "rules": m, "actual": ma}
    prev = pick_baseline(P["out"], build, a.against)
    cmp_ = compare(rules, prev["choices"]["rules"], pts, out, a.tolerance) if prev else None
    if cmp_:
        cmp_["against"] = prev["build"]
    cols = [(build, m)] + ([(prev["build"] + " (rescored)", cmp_["prev"])] if cmp_ else []) + [("ACTUAL (what ran)", ma)]
    choices = {"rules": rules}
    pcmp = None
    if a.planner:
        pc = planner_choices([p for p in pts if str(p["id"]) in ctxs], ag, P, a, ctxs)
        asked = {k: v[0] for k, v in pc.items()}
        eff = {k: v[1] for k, v in pc.items()}
        ids = [p for p in pts if str(p["id"]) in pc]
        summary["planner"] = {"model": a.model, "effort": a.effort, "answered": len(pc), "asked": metrics(asked, ids, out),
                              "effective": metrics(eff, ids, out), "rejected_by_checks": sum(1 for v in eff.values() if v.get("rejected"))}
        choices["planner"] = eff
        choices["planner_asked"] = asked
        if pc:
            cols.insert(1, (f"planner as answered ({len(pc)} points)", summary["planner"]["asked"]))
            cols.insert(2, ("planner effective", summary["planner"]["effective"]))
        if prev and (prev.get("planner_meta") or {}).get("model") == a.model and prev["choices"].get("planner"):
            pcmp = compare(eff, prev["choices"]["planner"], pts, out, a.tolerance, "planner")
            pcmp["against"] = prev["build"]
    flags = (cmp_ or {}).get("flags", []) + (pcmp or {}).get("flags", [])
    summary["comparison"] = {k: v for k, v in (cmp_ or {}).items() if k not in ("cur", "prev")} or None
    summary["regression"] = bool(flags)
    summary["accepted"] = not flags or a.accept
    text = report(summary, cmp_, pcmp, cols)
    if errors:
        text += "\n## Points the build could not decide on\n\n" + "\n".join(f"- {e}" for e in errors[:30]) + "\n"
    print(text)
    if not a.no_save:
        os.makedirs(P["out"], exist_ok=True)
        base = dict(summary, choices=choices, planner_meta={"model": a.model, "effort": a.effort} if a.planner else None)
        jdump(base, os.path.join(P["out"], f"{build}.json"))
        open(os.path.join(P["out"], f"{build}.md"), "w").write(text)
    line = (f"REGRESSION {build} vs {cmp_['against'] if cmp_ else '-'}: " + "; ".join(flags)) if flags else \
        f"OK {build}: outcome score {m['outcome_score']:+.3f}, regressed picks {m['regressed']}, violations {m['violations']}" + \
        (f", {cmp_['agreement_pct']:.0f}% same as {cmp_['against']}" if cmp_ else "")
    print("RESULT: " + line)
    return 1 if flags else 0


# ---------------------------------------------------------------------------------------------------- add
def session_file(sid, dirs):
    for d in dirs:
        f = os.path.join(d, f"s{sid}.json")
        if os.path.exists(f):
            return f
    return None


_FETCHED = set()


def fresh_session(sid, P, dirs, force=False):
    """Session data for the corpus: a cached copy unless it was cached while the session was still running (fetched
    again once per add)."""
    import build
    if sid in _FETCHED:
        return P["data"]
    f = session_file(sid, dirs)
    if f and not force:
        s = json.load(open(f))["session"]
        if s.get("status") in (None, "closed", "abandoned"):  # older caches have no status: closed sessions
            return os.path.dirname(f)
    os.makedirs(P["data"], exist_ok=True)
    tgt = os.path.join(P["data"], f"s{sid}.json")
    if os.path.exists(tgt):
        os.remove(tgt)
    build.session_data(sid, P["data"])
    _FETCHED.add(sid)
    return P["data"]


def cmp_key(d):
    """Which sessions measure the same thing: benchmark and data, concurrency per vCPU, CPU class."""
    s = d["session"]
    facts = backtest_J(s.get("facts"))
    cpus = int((facts.get("hardware") or {}).get("cpus") or 0)
    b = {}
    for r in d["runs"]:
        b = (backtest_J(r.get("inputs")) or {}).get("bench") or {}
        if b:
            break
    argv = " ".join((b.get("observed") or [{}])[0].get("argv") or []) or str(b.get("command") or "")
    name = str(b.get("name") or "")
    txt = argv + " " + name
    sb = re.search(r"\b(oltp_\w+|select_random_\w+|bulk_insert)\b", txt)
    thr = re.search(r"--threads[= ](\d+)", argv) or re.search(r"(\d+)\s*(?:threads|thr|VU)\b", name) or re.search(r"HDB_VU=(\d+)", argv) \
        or re.search(r"\s-T\s+(\d+)", argv)
    if sb:
        tables, size = re.search(r"--tables[= ](\d+)", argv), re.search(r"--table-size[= ](\d+)", argv)
        wl = f"sysbench:{sb.group(1)}:{tables.group(1) if tables else '?'}x{size.group(1) if size else '?'}"
    elif "hammerdb" in txt.lower() or "TPROC-C" in name:
        w = re.search(r"(\d+)\s*W\b", name)
        wl = f"tprocc:{w.group(1) if w else '?'}w"
    else:
        wl = re.sub(r"\s+", " ", re.sub(r"\(.*?\)|\d+\s*(?:threads|VU)", "", name.lower())).strip() or f"session{s['id']}"
    t = int(thr.group(1)) if thr else 0
    per = t / cpus if cpus and t else 0
    conc = "?" if not per else "low" if per <= 4 else "mid" if per <= 16 else "high"
    cpu = "small" if cpus <= 4 else "medium" if cpus <= 16 else "large"
    return f"{wl}|{conc}|{cpu}"


def backtest_J(s):
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s) if s else {}
    except ValueError:
        return {}


def session_runs(d):
    """Measured experiments of a session (backtest hindsight) with the noise each verdict was judged against."""
    import build
    s = d["session"]
    snoise = s.get("noise_pct") or (backtest_J(s.get("calibration")) or {}).get("noise_pct") or 2.0
    out = []
    for r in d["runs"]:
        if r["status"] != "done":
            continue
        ev = backtest_J(r.get("evaluation"))
        delta = ev.get("goal_delta_pct") if ev.get("goal_delta_pct") is not None else r.get("delta_pct")
        lab = r["label"] or ""
        if delta is None or not r.get("verdict") or r["verdict"] in ("reference", "repeat") or lab.lower().startswith(("baseline", "re-check")):
            continue
        ch = build.exp_changes(d, r)
        if ch:
            out.append({"seq": r["seq"], "label": lab, "verdict": r["verdict"], "delta": round(float(delta), 2),
                        "noise": ev.get("goal_noise_pct") or snoise, "changes": {k.lower(): backtest._num(v) for k, v in ch.items()}})
    return {"noise": snoise, "done_seqs": [r["seq"] for r in d["runs"] if r["status"] == "done"], "runs": out}


def build_outcomes(sids, P, dirs, man, version):
    sessions, keys = {}, {}
    for sid in sorted(sids):
        d = json.load(open(session_file(sid, dirs)))
        sessions[str(sid)] = session_runs(d)
        keys[sid] = sessions[str(sid)]["key"] = cmp_key(d)
    parent = {s: s for s in sids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by = collections.defaultdict(list)
    for s, k in keys.items():
        by[k].append(s)
    for g in list(by.values()) + [[s for s in g if s in parent] for g in man["explicit_groups"]]:
        for s in g[1:]:
            parent[find(s)] = find(g[0])
    groups = collections.defaultdict(list)
    for s in sids:
        groups[find(s)].append(s)
    return {"version": version, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sessions": sessions,
            "groups": sorted(sorted(g) for g in groups.values() if len(g) > 1)}


def frozen_point(lb, d, source, version):
    """A corpus point: the context then (build.context_at, before the engine's undo steps, which belong to the build) and
    what actually ran."""
    import build
    ctx = build.context_at(d, lb["after_run_seq"])
    if "tuner_pick" in lb:  # audited points (labels.json): the tuner's pick, and the expert's
        tp = lb.get("tuner_pick") or {}
        raw = [[c[0], c[1]] for c in tp.get("changes") or []]
        chg = norm_changes([{"variable": v, "value": x} for v, x in raw])
        act = {"key": choice_key(tp.get("id"), chg), "id": tp.get("id"), "changes": chg, "raw": raw, "executed": lb.get("next_executed")}
        expert = {k: lb.get(k) for k in ("expert_pick", "acceptable_ids", "uncertain", "class")}
        cls = "audit"
    else:
        res = build.settle(copy.deepcopy(ctx))
        o = backtest.option_of({"label": lb, "packet": {"candidates": [build.planner._cand(i) for i in res["items"]]}}, "actual")
        act = {"key": o["key"], "id": o["id"], "changes": o["changes"], "raw": [[k, v] for k, v in o["changes"].items()],
               "by": lb["actual"].get("by"), "run_seq": lb["actual"].get("run_seq"), "run_label": lb["actual"].get("run_label")}
        expert, cls = None, lb.get("class")
    return {"id": lb["rec_id"], "session": lb["session"], "after": lb["after_run_seq"], "class": cls, "source": source, "added": version,
            "actual": act, "expert": expert, "ctx": json.loads(json.dumps(ctx, default=str))}


def new_sessions(P, since):
    import build
    rows = build.q(f"SELECT s.id, s.ended FROM tsessions s LEFT JOIN studies st ON st.id=s.study_id WHERE s.status='closed' "
                   f"AND s.ended > {float(since)} AND (st.name IS NULL OR st.name NOT LIKE 'pipeline:%') ORDER BY s.id")
    return [r["id"] for r in rows], max([r["ended"] for r in rows] + [since])


def cmd_add(a):
    import build
    P = paths(a.ops)
    man = manifest(P["corpus"])
    old = load_points(P["corpus"], man) if man["versions"] else []
    have = {p["id"] for p in old}
    version = len(man["versions"]) + 1
    dirs = [P["data"]] + (a.data or []) + [os.path.join(a.ops, "dbench", "data")] + sorted(glob.glob(os.path.join(a.ops, "dbench", "backtest-*", "data")))
    todo = []  # (label, source)
    last = man.get("last_add_at") or 0
    if a.points:
        m = re.search(r"\d{4}-\d\d-\d\d", a.points)
        todo += [(json.loads(x)["label"], "backtest-" + m.group(0) if m else "points") for x in open(a.points)]
        fetched = glob.glob(os.path.join(os.path.dirname(a.points), "data", "*.json"))
        last = max([last] + [os.path.getmtime(f) for f in fetched])  # sessions closed after its data was fetched are new
    if a.labels:
        todo += [(lb, "audit-labels") for lb in json.load(open(a.labels))]
    sids = [int(x) for x in a.sessions.split(",")] if a.sessions else []
    if not (a.points or a.labels or a.sessions):
        sids, last = new_sessions(P, last)
        print(f"closed sessions since {datetime.datetime.fromtimestamp(man.get('last_add_at') or 0, datetime.timezone.utc):%Y-%m-%d %H:%M}Z: "
              f"{sids or 'none'}", file=sys.stderr)
    for sid in sids:
        ddir = fresh_session(sid, P, dirs, force=True)
        lbs, sk = build.auto_labels(sid, ddir)
        print(f"session {sid}: {len(lbs)} decision points" + (f", skipped {sk}" if sk else ""), file=sys.stderr)
        todo += [(lb, "sessions") for lb in lbs]
    todo = [(lb, s) for lb, s in todo if lb["rec_id"] not in have]
    new, failed = [], []
    for lb, src in todo:
        try:
            d = json.load(open(session_file(lb["session"], [fresh_session(lb["session"], P, dirs)])))
            new.append(frozen_point(lb, d, src, version))
        except Exception as e:  # reported, not silently dropped
            failed.append(f"s{lb['session']} rec {lb['rec_id']}: {type(e).__name__}: {e}")
    for f in failed:
        print("not added: " + f, file=sys.stderr)
    allsids = sorted({p["session"] for p in old + new})
    print(f"{len(new)} new decision points from {len({p['session'] for p in new})} sessions; corpus {len(old)} -> {len(old) + len(new)}",
          file=sys.stderr)
    if a.dry_run or (not new and man["versions"]):
        return 0
    for sid in allsids:  # outcome table from current production data: sessions cached while active are fetched again
        fresh_session(sid, P, dirs)
    outc = build_outcomes(allsids, P, dirs, man, version)
    os.makedirs(P["corpus"], exist_ok=True)
    part, of = f"part-{version:03d}.jsonl.gz", f"outcomes-v{version:03d}.json.gz"
    with gzip.open(os.path.join(P["corpus"], part + ".tmp"), "wt") as f:
        for p in new:
            f.write(json.dumps(p, default=str) + "\n")
    os.replace(os.path.join(P["corpus"], part + ".tmp"), os.path.join(P["corpus"], part))
    jdump(outc, os.path.join(P["corpus"], of))
    man["versions"].append({"version": version, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "part": part, "outcomes": of,
                            "points": len(new), "sessions": sorted({p["session"] for p in new}), "note": a.note,
                            "not_added": failed})
    man["last_add_at"] = last
    jdump(man, os.path.join(P["corpus"], "manifest.json"))
    print(f"corpus v{version}: {len(old) + len(new)} points, outcome table over {len(allsids)} sessions, "
          f"{len(outc['groups'])} comparable groups -> {P['corpus']}")
    return 0


def cmd_baselines(a):
    for b in baselines(paths(a.ops)["out"]):
        m = b["rules"]
        print(f"{b['created']}  {b['build']:24} corpus v{b['corpus_version']}  score {m['outcome_score']:+.3f}  regressed {m['regressed']}  "
              f"violations {m['violations']}  stops {m['stops']}  {'accepted' if b.get('accepted') else 'REGRESSION'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ops", default=os.path.join(ROOT, "ops"), help="ops directory (corpus, baselines, ssh config)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add", help="add decision points to the corpus and rebuild the outcome table")
    ad.add_argument("--points", help="a build.py points file (e.g. a backtest's points.jsonl)")
    ad.add_argument("--labels", help="audit labels (labels.json)")
    ad.add_argument("--sessions", help="these sessions' decision points (default: closed sessions since the last add)")
    ad.add_argument("--data", action="append", help="extra session-data cache directories")
    ad.add_argument("--dry-run", action="store_true")
    ad.add_argument("--note")
    sc = sub.add_parser("score", help="score a build")
    sc.add_argument("--agent", default=os.path.join(ROOT, "agent"), help="agent directory of the build (default: this working tree)")
    sc.add_argument("--build", help="baseline name (default: git short sha, -dirty with uncommitted agent changes)")
    sc.add_argument("--against", help="baseline to compare with (a build name, or a prefix ending in *); default: latest accepted")
    sc.add_argument("--tolerance", type=float, default=0.10, help="outcome-score drop (pp per decision) still accepted")
    sc.add_argument("--no-save", action="store_true")
    sc.add_argument("--accept", action="store_true", help="save as an accepted baseline even when flagged")
    sc.add_argument("--planner", action="store_true", help="also score the AI planner (paid calls through the tuner server)")
    sc.add_argument("--budget", type=float, default=0.0, help="USD cap for planner calls in this run")
    sc.add_argument("--model", default=PLANNER_MODEL)
    sc.add_argument("--effort", default="high")
    sc.add_argument("--limit", type=int, help="ask the planner about at most this many uncached points")
    sc.add_argument("--planner-dry-run", action="store_true", help="count the uncached planner calls, ask none")
    sub.add_parser("baselines", help="list saved baselines")
    a = ap.parse_args()
    sys.exit({"add": cmd_add, "score": cmd_score, "baselines": cmd_baselines}[a.cmd](a))


if __name__ == "__main__":
    main()
