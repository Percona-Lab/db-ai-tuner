#!/usr/bin/env python3
"""Similarity ranking prototype (offline): re-rank the rule engine's candidates at each corpus decision point by what
the same change did from the most similar measured states in other sessions, and score the choices with the decision
regression check's outcome table (regress.metrics), next to the rules' own choices.

usage: knn.py [--cases ops/dbench/knn/cases.jsonl] [--agent agent] [--k 15] [--mode loo|temporal] [--json out.json]

Modes: loo = every other session group (optimistic: later sessions too); temporal = only sessions started before the
point's session (what a tuner could have known then).
Variants scored:
  rules       the engine's choice (baseline)
  rerank      the candidate with the best predicted outcome when one is predicted >= +0.5 noise widths with support from
              2+ sessions; else the rules' choice
  veto        the rules' order, skipping candidates predicted <= -0.5 noise widths with support from 2+ sessions
  blend       rules score + 4 x predicted outcome (noise widths), candidates with support only
"""
import argparse
import collections
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import features  # noqa: E402
import regress  # noqa: E402


def load_cases(path):
    return [json.loads(x) for x in open(path)]


def standardiser(cases):
    vals = collections.defaultdict(list)
    for c in cases:
        for k, v in c["vec"].items():
            if v is not None:
                vals[k].append(v)
    st = {}
    for k, xs in vals.items():
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1)) or 1.0
        st[k] = (m, sd)
    return st


def z(vec, st):
    return {k: (v - st[k][0]) / st[k][1] for k, v in vec.items() if v is not None and k in st}


def cosine(a, b, min_shared=10):
    ks = set(a) & set(b)
    if len(ks) < min_shared:
        return None
    dot = sum(a[k] * b[k] for k in ks)
    na = math.sqrt(sum(a[k] ** 2 for k in ks))
    nb = math.sqrt(sum(b[k] ** 2 for k in ks))
    return dot / (na * nb) if na and nb else None


def change_key(ch):
    return tuple(sorted((str(k).lower(), str(v).lower()) for k, v in (ch or {}).items()))


def item_changes(it):
    return {str(c["variable"]).lower(): str(c.get("proposed", c.get("value"))).lower() for c in it.get("changes") or []}


def matches(case, it):
    if case["rule"] and case["rule"] == it["id"]:
        return True
    ic = item_changes(it)
    return bool(ic) and change_key(case["changes"]) == change_key(ic)


def predict(qz, it, pool, k):
    """Similarity-weighted outcome (noise widths) of this change from the k most similar states it was tried in;
    one vote per session (its cases averaged first). (pred, support sessions, neighbours)"""
    sims = []
    for c in pool:
        if not matches(c, it):
            continue
        s = cosine(qz, c["_z"])
        if s is not None and s > 0:
            sims.append((s, c))
    sims.sort(key=lambda x: -x[0])
    top = sims[:k]
    if not top:
        return None, 0, []
    per = collections.defaultdict(list)
    for s, c in top:
        per[c["session"]].append((s, max(-5.0, min(5.0, c["z"]))))
    num = den = 0.0
    for xs in per.values():
        w = max(s for s, _ in xs)
        num += w * sum(v for _, v in xs) / len(xs)
        den += w
    return (num / den if den else None), len(per), [(round(s, 3), c["session"], c["seq"], c["z"]) for s, c in top[:5]]


def cpu_class(n):
    n = int(n or 0)
    return "1-2" if n <= 2 else "3-8" if n <= 8 else "9-32" if n <= 32 else "33+"


def conc_class(conns, cpus):
    n, c = float(conns or 0), float(cpus or 0)
    if not n or not c:
        return None
    r = n / c
    return "low" if r <= 1 else "medium" if r <= 6 else "high" if r <= 64 else "extreme"


def experience(pool, wl, cpus, conns):
    """As the server's /experience 'experience' tier: per rule id, outcomes in comparable sessions (same workload class,
    CPU class and concurrency class): n, wins, losses, mean delta."""
    out = {}
    for c in pool:
        if not c["rule"]:
            continue
        if wl and c.get("wl") and c["wl"] != wl:
            continue
        if cpus and c.get("cpus") and cpu_class(c["cpus"]) != cpu_class(cpus):
            continue
        cc = conc_class(conns, cpus)
        if cc and c.get("conns") and conc_class(c["conns"], c["cpus"]) != cc:
            continue
        e = out.setdefault(c["rule"], {"n": 0, "wins": 0, "losses": 0, "_d": []})
        e["n"] += 1
        e["wins"] += c["verdict"] in ("improved", "efficiency-gain")
        e["losses"] += c["verdict"] in ("regressed", "efficiency-loss")
        e["_d"].append(c["delta"])
    for e in out.values():
        d = e.pop("_d")
        e["mean_delta_pct"] = round(sum(d) / len(d), 2) if d else None
    return out


def candidates(res):
    out = [i for i in res["items"] if i["kind"] in ("config", "revert") and i.get("confidence", 0) > 0]
    return sorted(out, key=lambda i: -(i.get("score") or 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=os.path.join(ROOT, "ops/dbench/knn/cases.jsonl"))
    ap.add_argument("--agent", default=os.path.join(ROOT, "agent"))
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--mode", choices=["loo", "temporal"], default="loo")
    ap.add_argument("--json")
    ap.add_argument("--with-experience", action="store_true", help="the engine gets cross-session experience from the pool (production-like)")
    ap.add_argument("--nosim", action="store_true", help="ablation: every matching case counts the same (the change's average outcome)")
    a = ap.parse_args()
    if a.nosim:
        global cosine
        cosine = lambda x, y, min_shared=10: 1.0  # noqa: E731
    P = regress.paths(os.path.join(ROOT, "ops"))
    man = regress.manifest(P["corpus"])
    pts = regress.load_points(P["corpus"], man)
    out = regress.load_outcomes(P["corpus"], man)
    ag = regress.load_agent(a.agent)
    cases = load_cases(a.cases)
    st = standardiser(cases)
    for c in cases:
        c["_z"] = z(c["vec"], st)
    start = {}
    for c in cases:
        start[c["session"]] = min(start.get(c["session"], c["session_start"]), c["session_start"])
    groups = {}
    for g in out["groups"]:
        for s in g:
            groups[s] = set(g)
    choices = {v: {} for v in ("rules", "rerank", "veto", "blend")}
    detail = []
    for pt in pts:
        pid = str(pt["id"])
        sid = pt["session"]
        excl = groups.get(sid, {sid}) | {sid}
        t0 = start.get(sid)
        pool = [c for c in cases if c["session"] not in excl and (a.mode == "loo" or (t0 is not None and c["session_start"] < t0))]
        ch0, _, _ = regress.rules_choice(pt, ag)
        choices["rules"][pid] = ch0
        if a.with_experience:
            # production-like baseline: the engine with cross-session experience built from the same pool
            c0 = pt["ctx"]
            wl = ((c0.get("inputs") or {}).get("workload") or {}).get("type")
            pt = dict(pt, ctx=dict(c0, experience=experience(pool, wl, (c0.get("hw") or {}).get("cpus"),
                                                              (c0.get("summary") or {}).get("threads_connected_avg"))))
        ch, ctx, res = regress.rules_choice(pt, ag)
        choices.setdefault("rules+exp", {})[pid] = ch
        cpus = (ctx.get("hw") or {}).get("cpus")
        qv = features.vector(ctx.get("summary"), ctx.get("inputs"), cpus, (ctx.get("variables") or {}).get("innodb_buffer_pool_size"),
                             (ctx.get("inputs") or {}).get("data_bytes"))
        qz = z(qv, st)
        cands = candidates(res)
        allowed = [i for i in cands if not regress.policy_violation(regress.item_choice(i), ctx.get("policy"), ag["tradeoff_of"])]
        preds = {i["id"]: predict(qz, i, pool, a.k) for i in allowed}
        # rerank
        good = [(p[0], i) for i in allowed for p in [preds[i["id"]]] if p[0] is not None and p[1] >= 2 and p[0] >= 0.5]
        choices["rerank"][pid] = regress.item_choice(max(good, key=lambda x: x[0])[1]) if good else ch
        # veto
        if ch["key"] != "STOP":
            nxt = next((i for i in allowed if not (preds[i["id"]][0] is not None and preds[i["id"]][1] >= 2 and preds[i["id"]][0] <= -0.5)), None)
            choices["veto"][pid] = regress.item_choice(nxt) if nxt else regress.stop_choice("all candidates predicted to lose")
        else:
            choices["veto"][pid] = ch
        # blend
        if allowed and ch["key"] != "STOP":
            sc = lambda i: (i.get("score") or 0) + (4 * preds[i["id"]][0] if preds[i["id"]][0] is not None and preds[i["id"]][1] >= 2 else 0)  # noqa: E731
            choices["blend"][pid] = regress.item_choice(max(allowed, key=sc))
        else:
            choices["blend"][pid] = ch
        detail.append({"point": pt["id"], "session": sid, "rules": regress.text_of(ch), "rerank": regress.text_of(choices["rerank"][pid]),
                       "preds": {k: [round(v[0], 2) if v[0] is not None else None, v[1]] for k, v in preds.items()}})
    rep = {}
    for v, chs in choices.items():
        m = regress.metrics(chs, pts, out)
        rep[v] = {k: m.get(k) for k in ("outcome_score", "coverage_pct", "measured_picks", "mean_measured_delta", "improved", "regressed",
                                        "regressed_sum", "stops", "stop_good", "stop_bad", "violations")}
        rep[v]["changed_vs_rules"] = sum(1 for p in pts if chs[str(p["id"])]["key"] != choices["rules"][str(p["id"])]["key"])
    print(json.dumps({"mode": a.mode, "k": a.k, "cases": len(cases), "points": len(pts), "variants": rep}, indent=1))
    if a.json:
        json.dump({"report": rep, "detail": detail}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
