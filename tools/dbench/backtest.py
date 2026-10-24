#!/usr/bin/env python3
"""Decision backtest on real sessions: compare what the tuner did with what today's rules, the current planner and a
reference model would do at the same decision points, graded blind and checked against what was measured later.

usage:
  backtest.py judge-input <points.jsonl> <plans.jsonl> --makers planner=opus,top=fable --out judge-input.json --key key.json
  backtest.py report <points.jsonl> <plans.jsonl> --key key.json --judged a.jsonl,b.jsonl [--data DIR] [--detail out.md] [--json out.json]

Steps: build.py --sessions (decision points + what ACTUAL did + what RULES NOW choose + the planner packet),
run_remote.py (planner variants on the packet), `judge-input` (every distinct option of a decision, anonymised and
shuffled, with the evidence the planner saw minus the engine's own pick and scores), judge_remote.py (one blind judge
per model), `report` (agreement, grades per decision maker and session type, stop rates, hindsight: the measured
verdict of each option's change when that change was run later in the session or in a comparable session; judges
never see it).
"""
import argparse
import collections
import json
import os
import random
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# sessions whose measurements stand in for each other in hindsight: same benchmark and data, other host or client placement
COMPARABLE = [{294, 297}, {299, 300, 303, 304}, {315, 316}, {302, 310, 313, 314}]
EVIDENCE_KEYS = ("server", "server_facts", "hardware", "goal", "policy", "benchmark", "workload", "load", "diagnosis",
                 "time_breakdown", "profiles", "summary", "noise_pct", "hypotheses_tested", "history", "configurations",
                 "stability_first", "memory_budget_now")
CAND_KEYS = ("id", "title", "safety", "changes", "relevance", "note", "experience", "sweep_values", "tradeoffs",
             "memory_budget_after", "known_neutral", "track_record", "no_mechanism")
MAKERS = ("actual", "rules")


def load(path):
    return [json.loads(x) for x in open(path)]


def _num(v):
    s = str(v).strip().strip("'\"").lower()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([kmgt])i?b?", s)
    if m:
        return str(int(float(m.group(1)) * 1024 ** "kmgt".index(m.group(2)) * 1024))
    return s


def parse_changes(lst):
    """['var: cur -> new', ...] or [{'variable','value'|'proposed'}] -> {var: normalised new value}."""
    out = {}
    for c in lst or []:
        if isinstance(c, dict):
            v, new = c.get("variable"), c.get("value", c.get("proposed"))
        else:
            v, _, rest = str(c).partition(": ")
            new = re.split(r"\s*->\s*", rest)[-1]
        if v:
            out[str(v).strip().lower()] = _num(new)
    return out


def option_of(point, maker, plan=None):
    """The option a decision maker chose: {"key", "text", "changes", "id"}; None when it gave no usable answer."""
    pk = point["packet"]
    cands = {c["id"]: c for c in pk.get("candidates") or []}
    if maker == "actual":
        a = point["label"]["actual"]
        if not a["next"]:
            return {"key": "STOP", "text": "STOP", "id": None, "changes": {}}
        cid = a["next"]
        lab = a.get("run_label") or ""
        if lab and not lab.startswith((cid, "exp#")) and a.get("run_changes"):
            # something else ran than the recommendation named (a canary's scripted step, an AI idea queued later):
            # what ran is the actual decision
            cid = lab
            if cid not in cands:
                return _opt(cid, [f"{k}: ? -> {v}" for k, v in a["run_changes"].items()], cands)
        if cid in cands:
            ch = cands[cid]["changes"]
        else:
            it = a.get("next_item") or {}
            ch = [f"{x['variable']}: {x.get('current')} -> {x.get('proposed')}" for x in it.get("changes") or []] or \
                 [f"{k}: ? -> {v}" for k, v in (a.get("run_changes") or {}).items()]
        return _opt(cid, ch, cands)
    if maker == "rules":
        r = point["rules"]
        if not r["next"]:
            return {"key": "STOP", "text": "STOP", "id": None, "changes": {}}
        return _opt(r["next"], (cands.get(r["next"]) or {}).get("changes") or r.get("changes"), cands)
    p = (plan or {}).get("result", {}).get("plan")
    if not p:
        return None
    if p.get("action") == "stop":
        return {"key": "STOP", "text": "STOP", "id": None, "changes": {}}
    ch = p.get("choice") or {}
    cid = (ch.get("candidate_id") or "").strip()
    if cid:
        return _opt(cid, (cands.get(cid) or {}).get("changes"), cands, ch.get("sweep_values"))
    lst = [f"{c['variable']}: -> {c['value']}" for c in ch.get("changes") or []]
    o = _opt(None, lst, cands, ch.get("sweep_values"))
    return o


def _opt(cid, changes, cands, sweep=None):
    chg = parse_changes(changes)
    c = cands.get(cid) or {}
    sweep = sweep or c.get("sweep_values")
    desc = "; ".join(str(x).replace(": None -> ", ": -> ") for x in changes or [])
    text = (f"{cid} — " if cid else "") + (c.get("title") + ": " if c.get("title") else "") + (desc or "(no settings listed)")
    if sweep:
        text += f" [sweep values: {', '.join(map(str, sweep))}]"
    # the same settings are the same option whoever proposed them (a candidate id or concrete changes)
    key = ("CHG:" + json.dumps(sorted(chg.items()))) if chg else ("ID:" + str(cid))
    return {"key": key, "text": text, "id": cid, "changes": chg}


def plans_by(plans_path, makers):
    """{maker: {rec_id: plan row}} for the variants named in makers (maker=variant)."""
    out = {m: {} for m in makers}
    for r in load(plans_path) if plans_path and os.path.exists(plans_path) else []:
        for m, v in makers.items():
            if r["variant"] == v:
                out[m][r["rec_id"]] = r
    return out


def evidence(pk):
    e = {k: pk[k] for k in EVIDENCE_KEYS if k in pk}
    e["candidates"] = [{k: c[k] for k in CAND_KEYS if k in c} for c in pk.get("candidates") or []]
    return e


def cmd_check(a):
    """What the agent would actually run on each planner answer: planner.check on the rebuilt context (a choice that fails
    a check falls back to the rules' pick)."""
    import copy
    sys.path.insert(0, os.path.join(ROOT, "agent"))
    from build import context_at, session_data, settle
    from tuning_agent import planner
    plans = [r for r in load(a.plans) if r["variant"] == a.variant]
    pts = {p["label"]["rec_id"]: p for p in load(a.points)}
    with open(a.out, "w") as out:
        for r in plans:
            p = pts[r["rec_id"]]
            ctx = context_at(session_data(p["label"]["session"], a.data), p["label"]["after_run_seq"])
            res = settle(ctx)
            pl = r["result"].get("plan")
            if not pl:
                out.write(json.dumps({"rec_id": r["rec_id"], "variant": a.variant, "applied": res.get("next"), "rejected": "no plan"}) + "\n")
                continue
            c2 = copy.deepcopy(ctx)
            c2["failed"] = []  # build.settle lists the undo steps it applied there; they did not fail
            nxt, added, why = planner.check(dict(pl, _model=r["model"]), c2, copy.deepcopy(res))
            out.write(json.dumps({"rec_id": r["rec_id"], "variant": a.variant, "action": pl.get("action"), "applied": nxt,
                                  "added": [{"id": i["id"], "changes": i.get("changes"), "safety": i.get("safety")} for i in added],
                                  "rejected": why}, default=str) + "\n")
    print(f"{len(plans)} plans checked -> {a.out}")


def cmd_judge_input(a):
    pts = load(a.points)
    makers = dict(x.split("=", 1) for x in a.makers.split(","))
    pl = plans_by(a.plans, makers)
    rnd = random.Random(20261024)
    items, key = [], {}
    for p in pts:
        rid = p["label"]["rec_id"]
        opts = {"actual": option_of(p, "actual"), "rules": option_of(p, "rules")}
        for m in makers:
            opts[m] = option_of(p, m, pl[m].get(rid))
        distinct = {}
        for m, o in opts.items():
            if o:
                distinct.setdefault(o["key"], o["text"])
        keys = list(distinct)
        rnd.shuffle(keys)
        letters = {k: "ABCDEFGH"[i] for i, k in enumerate(keys)}
        items.append({"decision": rid, "evidence": evidence(p["packet"]), "options": {letters[k]: distinct[k] for k in keys},
                      "auditor_note": "none"})
        key[str(rid)] = {m: (letters[o["key"]] if o else None) for m, o in opts.items()}
    json.dump(items, open(a.out, "w"))
    json.dump(key, open(a.key, "w"), indent=1)
    n = [len(i["options"]) for i in items]
    print(f"{len(items)} decisions, {sum(n)} options (mean {sum(n) / len(n):.2f}) -> {a.out}")


# ---------------------------------------------------------------------------------------------------- hindsight
def measured_runs(data_dir, sid):
    from build import J, exp_changes, session_data
    d = session_data(sid, data_dir)
    out = []
    for r in d["runs"]:
        if r["status"] != "done":
            continue
        ev = J(r["evaluation"], {})
        delta = ev.get("goal_delta_pct") if ev.get("goal_delta_pct") is not None else r.get("delta_pct")
        if delta is None or not (r["verdict"] or "") or r["verdict"] in ("reference", "repeat") or r["label"].lower().startswith(("baseline", "re-check")):
            continue
        ch = exp_changes(d, r)
        if ch:
            out.append({"session": sid, "seq": r["seq"], "label": r["label"], "verdict": r["verdict"],
                        "delta": delta, "changes": {k.lower(): _num(v) for k, v in ch.items()},
                        "confirm": _confirm(d, r)})
    return out


def _confirm(d, run):
    """The verdict of the run's confirmation, when one followed (confirm / repeat runs of the same configuration)."""
    for r in d["runs"]:
        if r["seq"] > run["seq"] and r["seq"] <= run["seq"] + 2 and r["label"].startswith(run["label"]) and ("confirm" in r["label"] or "repeat" in r["label"]):
            return r["verdict"]
    return None


def hindsight(opt, sid, seq, runs_by_sess):
    """Measured results of the option's change: same session at/after the point first, then comparable sessions."""
    if not opt or opt["key"] == "STOP" or not opt["changes"]:
        return None
    want = {k: v for k, v in opt["changes"].items()}
    vars_ = set(want)
    found = []
    group = next((g for g in COMPARABLE if sid in g), {sid})
    for s2 in sorted(group, key=lambda x: x != sid):
        for r in runs_by_sess.get(s2, []):
            if s2 == sid and r["seq"] <= seq:
                continue
            rv = set(r["changes"])
            # the option's settings are what the run changed (a revert of an earlier experiment may ride along)
            if vars_ <= rv and all(r["changes"].get(k) == v for k, v in want.items()):
                found.append(dict(r, match="exact", where="same" if s2 == sid else "comparable"))
            elif vars_ <= rv:
                found.append(dict(r, match="same-variables", where="same" if s2 == sid else "comparable"))
    found.sort(key=lambda r: (r["where"] != "same", r["match"] != "exact"))
    return found[:3] or None


def stop_hindsight(sid, seq, runs_by_sess):
    """For a stop: did a later run in the same session still find a confirmed improvement of the goal?"""
    later = [r for r in runs_by_sess.get(sid, []) if r["seq"] > seq]
    wins = [r for r in later if r["verdict"] == "improved" and (r["confirm"] in (None, "repeat"))]
    return {"later_runs": len(later), "later_wins": [(r["seq"], r["label"], r["delta"]) for r in wins]}


def diff_kind(p, act, rul):
    """Why today's rules differ from what ran: a new stop, the old choice no longer proposed (ruled out / gated), an AI
    idea the rebuild does not regenerate, a ranking change, a scripted step, or the planner deciding."""
    if act["key"] == rul["key"]:
        return "same"
    a = p["label"]["actual"]
    if a["by"].startswith("planner"):
        return "planner decided"
    if act["key"] == "STOP":
        return "actual stopped, rules run"
    if rul["key"] == "STOP":
        return "rules now stop"
    cands = {c["id"]: c for c in p["packet"].get("candidates") or []}
    cid = act["id"] or ""
    if a.get("run_label") and a["next"] and not a["run_label"].startswith((a["next"], "exp#")):
        return "scripted / queued step ran"
    if cid.startswith("llm-") and cid not in cands:
        return "AI idea (not rebuilt)"
    c = cands.get(cid)
    if not c:
        return "no longer proposed"
    if (c.get("confidence") or 0) <= 0:
        return "ruled out now"
    return "re-ranked"


# ---------------------------------------------------------------------------------------------------- report
def cmd_report(a):
    pts = load(a.points)
    makers = dict(x.split("=", 1) for x in a.makers.split(","))
    pl = plans_by(a.plans, makers)
    key = json.load(open(a.key))
    grades = collections.defaultdict(dict)  # judge -> rec -> {letter: grade}, best
    for f in (a.judged or "").split(","):
        if not f:
            continue
        for line in open(f):
            r = json.loads(line)
            if r.get("result"):
                grades[r["model"]][r["decision"]] = ({g["option"].strip()[:1]: g["grade"] for g in r["result"]["grades"]}, r["result"].get("best"))
    allm = list(MAKERS) + list(makers)
    sessions = sorted({p["label"]["session"] for p in pts})
    runs_by_sess = {s: measured_runs(a.data, s) for s in sessions}
    rows = []
    for p in pts:
        lb = p["label"]
        rid = lb["rec_id"]
        opts = {"actual": option_of(p, "actual"), "rules": option_of(p, "rules")}
        for m in makers:
            opts[m] = option_of(p, m, pl[m].get(rid))
        row = {"rec": rid, "session": lb["session"], "class": lb["class"], "after": lb["after_run_seq"], "by": lb["actual"]["by"],
               "actual_run": [lb["actual"].get("run_seq"), lb["actual"].get("run_label")], "opts": {}, "cost": {}}
        for m in allm:
            o = opts.get(m)
            g = {}
            for j, gr in grades.items():
                if str(rid) in key and rid in gr:
                    letter = key[str(rid)].get(m)
                    if letter and letter in gr[rid][0]:
                        g[j] = gr[rid][0][letter]
            hs = stop_hindsight(lb["session"], lb["after_run_seq"], runs_by_sess) if o and o["key"] == "STOP" else \
                hindsight(o, lb["session"], lb["after_run_seq"], runs_by_sess)
            row["opts"][m] = {"key": o["key"] if o else None, "text": o["text"] if o else None, "grades": g,
                              "best": {j: (key.get(str(rid), {}).get(m) == gr[rid][1]) for j, gr in grades.items() if rid in gr},
                              "hindsight": hs}
            if m in makers and pl[m].get(rid):
                row["cost"][m] = pl[m][rid]["cost"]
                pp = pl[m][rid]["result"].get("plan") or {}
                row["opts"][m]["why"] = (pp.get("diagnosis", "")[:300] + " | " + pp.get("rationale", "")[:300]) if pp else pl[m][rid]["result"].get("error")
        row["actual_vs_rules"] = diff_kind(p, opts["actual"], opts["rules"])
        # the actual run's own measured result (the hindsight for ACTUAL at this exact point)
        ar = next((r for r in runs_by_sess.get(lb["session"], []) if r["seq"] == lb["actual"].get("run_seq")), None)
        row["actual_measured"] = {"verdict": ar["verdict"], "delta": ar["delta"], "confirm": ar["confirm"]} if ar else None
        rows.append(row)
    # what the agent would run: a planner answer that fails planner.check falls back to the rules' pick (--checked)
    eff = []
    for spec in (a.checked or "").split(","):
        if not spec:
            continue
        m, f = spec.split("=", 1)
        chk = {json.loads(x)["rec_id"]: json.loads(x) for x in open(f)}
        name = m + "-effective"
        eff.append(name)
        for row in rows:
            c = chk.get(row["rec"]) or {}
            row["opts"][name] = dict(row["opts"]["rules"] if c.get("rejected") else row["opts"][m], rejected=c.get("rejected"))
    json.dump(rows, open(a.json, "w"), indent=1, default=str)
    _print(rows, allm + eff, sorted(grades))


def _cls(c):
    return {"external-1024": "external 1024-thr", "external-64": "external 64-thr"}.get(c, c)


def _mean(v):
    return sum(v) / len(v) if v else float("nan")


def _avg_grade(o):
    g = o["grades"]
    return sum(g.values()) / len(g) if g else None


def _measured(o):
    """The first measured result of the option's exact settings (same session preferred); None when never measured."""
    hs = o.get("hindsight")
    if not hs or isinstance(hs, dict):
        return None
    ex = [x for x in hs if x["match"] == "exact"]
    return ex[0] if ex else None


def _print(rows, allm, judges):
    print(f"{len(rows)} decision points\n\n## Agreement (same settings or same stop)\n")
    print("| | " + " | ".join(allm) + " |\n|---|" + "---|" * len(allm))
    for m1 in allm:
        cells = []
        for m2 in allm:
            both = [r for r in rows if r["opts"][m1]["key"] and r["opts"][m2]["key"]]
            cells.append(f"{100 * sum(r['opts'][m1]['key'] == r['opts'][m2]['key'] for r in both) / max(1, len(both)):.0f}%")
        print(f"| {m1} | " + " | ".join(cells) + " |")
    print("\n## Judge grades: mean / good / poor / judged best\n")
    print("| maker | " + " | ".join(j.split("/")[-1] for j in judges) + " | both judges | STOP picks |\n|---|" + "---|" * (len(judges) + 2))
    for m in allm:
        cells = []
        for j in judges:
            g = [r["opts"][m]["grades"][j] for r in rows if j in r["opts"][m]["grades"]]
            b = [r["opts"][m]["best"].get(j) for r in rows if j in r["opts"][m].get("best", {})]
            cells.append(f"{_mean(g):.2f} / {100 * g.count(2) / max(1, len(g)):.0f}% / {100 * g.count(0) / max(1, len(g)):.0f}% / "
                         f"{100 * sum(bool(x) for x in b) / max(1, len(b)):.0f}%" if g else "-")
        avg = [x for x in (_avg_grade(r["opts"][m]) for r in rows) if x is not None]
        stops = [r["opts"][m]["key"] == "STOP" for r in rows if r["opts"][m]["key"]]
        print(f"| {m} | " + " | ".join(cells) + f" | {_mean(avg):.2f} (n={len(avg)}) | {sum(stops)}/{len(stops)} |")
    classes = sorted({r["class"] for r in rows})
    print("\n## Mean grade (both judges) per session type\n")
    print("| maker | " + " | ".join(f"{_cls(c)} (n={sum(r['class'] == c for r in rows)})" for c in classes) + " |\n|---|" + "---|" * len(classes))
    for m in allm:
        cells = []
        for c in classes:
            v = [x for x in (_avg_grade(r["opts"][m]) for r in rows if r["class"] == c) if x is not None]
            cells.append(f"{_mean(v):.2f}" if v else "-")
        print(f"| {m} | " + " | ".join(cells) + " |")
    print("\n## Hindsight: options whose exact settings were measured later (same or comparable session)\n")
    print("| maker | run picks | measured | improved | regressed | mean delta % | STOP picks with a later confirmed win |\n|---|---|---|---|---|---|---|")
    for m in allm:
        runs = [r for r in rows if r["opts"][m]["key"] not in (None, "STOP")]
        ms = [x for x in (_measured(r["opts"][m]) for r in runs) if x]
        st = [r for r in rows if r["opts"][m]["key"] == "STOP"]
        late = [r for r in st if isinstance(r["opts"][m]["hindsight"], dict) and r["opts"][m]["hindsight"]["later_wins"]]
        print(f"| {m} | {len(runs)} | {len(ms)} | {sum(x['verdict'] == 'improved' for x in ms)} | {sum(x['verdict'] == 'regressed' for x in ms)} | "
              f"{_mean([x['delta'] for x in ms]):+.2f} | {len(late)}/{len(st)} |")
    print("\n## Why today's rules differ from what ran\n")
    for k, n in collections.Counter(r["actual_vs_rules"] for r in rows).most_common():
        print(f"- {k}: {n}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("judge-input")
    j.add_argument("points")
    j.add_argument("plans")
    j.add_argument("--makers", required=True, help="maker=variant,... (variants in the plans file)")
    j.add_argument("--out", required=True)
    j.add_argument("--key", required=True)
    c = sub.add_parser("check")
    c.add_argument("points")
    c.add_argument("plans")
    c.add_argument("--variant", required=True)
    c.add_argument("--data", default=os.path.join(ROOT, "ops", "dbench", "data"))
    c.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("points")
    r.add_argument("plans")
    r.add_argument("--makers", required=True)
    r.add_argument("--key", required=True)
    r.add_argument("--judged")
    r.add_argument("--checked", help="maker=checked.jsonl,... from the check step")
    r.add_argument("--data", default=os.path.join(ROOT, "ops", "dbench", "data"))
    r.add_argument("--json", required=True)
    a = ap.parse_args()
    {"judge-input": cmd_judge_input, "report": cmd_report, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    main()
