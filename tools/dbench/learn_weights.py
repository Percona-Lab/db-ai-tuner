#!/usr/bin/env python3
"""Learn how much each feature group counts in the similarity search (step 2 of the similarity ranking, 2026-10-26).

For every case (a measured experiment), predict its outcome from the k most similar states the same change was tried in,
using only sessions that started before its session (what the tuner could have known), and compare with what it measured.
Coordinate search over per-group weights (0 = the group is ignored) for the best agreement: Spearman correlation between
predicted and measured outcome plus the share of predictions with the right sign (|pred| >= 0.5 noise widths).

usage: learn_weights.py [--cases ops/dbench/knn/cases.jsonl] [--agent agent] [--k 15] [--json out.json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GRID = (0.0, 0.5, 1.0, 2.0, 3.0)


def rank(xs):
    o = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    for n, i in enumerate(o):
        r[i] = float(n)
    return r


def spearman(a, b):
    if len(a) < 3:
        return 0.0
    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def evaluate(sc, cases, by_change, start, weights, k):
    sim = lambda a, b: sc.cosine(a, b, weights=weights)  # noqa: E731
    preds, acts, right, called = [], [], 0, 0
    for c in cases:
        t0 = start[c["session"]]
        pool = [x for x in by_change[c["_key"]] if x["session"] != c["session"] and start[x["session"]] < t0]
        if not pool:
            continue
        p, nsess, _, _ = sc.predict(c["_z"], {"id": c["rule"] or "", "changes": [{"variable": k_, "proposed": v} for k_, v in c["changes"].items()]},
                                    pool, k, sim=sim)
        if p is None or nsess < sc.MIN_SESSIONS:
            continue
        a = max(-5.0, min(5.0, c["z"]))
        preds.append(p)
        acts.append(a)
        if abs(p) >= 0.5:
            called += 1
            right += (p > 0) == (a > 0)
    rho = spearman(preds, acts)
    sign = right / called if called else 0.0
    return {"objective": round(rho + 0.5 * sign, 4), "spearman": round(rho, 3), "sign_right": round(sign, 3), "called": called, "n": len(preds)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=os.path.join(ROOT, "ops/dbench/knn/cases.jsonl"))
    ap.add_argument("--agent", default=os.path.join(ROOT, "agent"))
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--json")
    a = ap.parse_args()
    sys.path.insert(0, a.agent)
    from tuning_agent import simcore as sc
    cases = [json.loads(x) for x in open(a.cases)]
    st = sc.standardiser(cases)
    start, by_change = {}, {}
    for c in cases:
        c["_z"] = sc.z(c["vec"], st)
        start[c["session"]] = min(start.get(c["session"], c["session_start"]), c["session_start"])
        c["_key"] = c["rule"] or json.dumps(sorted(c["changes"].items()))
        by_change.setdefault(c["_key"], []).append(c)
    groups = sorted(set(sc.FEATURE_GROUP.values()))
    w = {g: 1.0 for g in groups}
    best = evaluate(sc, cases, by_change, start, sc.expand_weights(w), a.k)
    base = dict(best)
    print("all groups at 1:", base, file=sys.stderr)
    for rnd in range(a.rounds):
        for g in groups:
            for val in GRID:
                if val == w[g]:
                    continue
                trial = dict(w, **{g: val})
                r = evaluate(sc, cases, by_change, start, sc.expand_weights(trial), a.k)
                if r["objective"] > best["objective"] + 0.002:
                    w, best = trial, r
                    print(f"round {rnd + 1}: {g}={val} -> {r}", file=sys.stderr)
    out = {"weights": w, "result": best, "baseline": base, "cases": len(cases), "k": a.k}
    print(json.dumps(out, indent=1))
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
