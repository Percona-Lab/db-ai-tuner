#!/usr/bin/env python3
"""Decision benchmark, step 3: score each decision maker against the expert labels.

usage: score.py <points.jsonl> [results.jsonl ...] [--exclude-uncertain] [--detail out.md]

A choice matches when it takes the expert's action: the same candidate (or one the audit called a reasonable
alternative), the same settings when the expert named settings, stop when the expert said stop, a trade-off when the
expert said switch to trade-offs. "old rules" is what the tuner actually chose; "new rules" is the current engine on the
same evidence; planner variants are scored on the planner's step, and with a reviewer on the final step.
"""
import argparse
import collections
import json


def vars_of(changes):
    out = set()
    for c in changes or []:
        if isinstance(c, (list, tuple)):
            out.add(str(c[0]).lower())
        elif isinstance(c, dict):
            out.add(str(c.get("variable") or "").lower())
        else:
            out.add(str(c).split(":")[0].split("=")[0].strip().lower())
    return {v for v in out if v}


def matches(choice, lb, cands):
    return bool(_matches(choice, lb, cands))


def _matches(choice, lb, cands):
    """choice: {"action": run|stop, "id": candidate id or None, "vars": set of variables}."""
    ex = lb.get("expert_pick") or {}
    act = ex.get("action") or "run"
    if act == "stop":
        return choice["action"] == "stop"
    if act == "remeasure":
        return choice["action"] == "run" and (choice.get("id") or "").startswith(("revert", "rollback", "re-check"))
    if choice["action"] == "stop":
        return False
    cid = choice.get("id")
    if act == "switch-to-tradeoffs":
        c = cands.get(cid) or {}
        return c.get("safety") == "trade-off" or (ex.get("candidate_id") and cid == ex["candidate_id"])
    ok = {x for x in [ex.get("candidate_id")] + list(lb.get("acceptable_ids") or []) if x}
    if cid and cid in ok:
        return True
    ev = vars_of(ex.get("changes"))
    if ev:
        mine = choice.get("vars") or vars_of((cands.get(cid) or {}).get("changes"))
        return bool(ev & mine)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("points")
    ap.add_argument("results", nargs="*")
    ap.add_argument("--exclude-uncertain", action="store_true")
    ap.add_argument("--detail")
    a = ap.parse_args()
    pts = {}
    for line in open(a.points):
        p = json.loads(line)
        pts[p["label"]["rec_id"]] = p
    res = collections.defaultdict(dict)
    for f in a.results:
        for line in open(f):
            r = json.loads(line)
            res[r["variant"]][r["rec_id"]] = r
    keep = {k: p for k, p in pts.items() if not (a.exclude_uncertain and p["label"].get("uncertain"))}
    makers = {"old rules (what ran)": {}, "new rules (current engine)": {}}
    for k, p in keep.items():
        lb, cands = p["label"], {c["id"]: c for c in p["packet"]["candidates"]}
        tp = lb.get("tuner_pick") or {}
        makers["old rules (what ran)"][k] = lb.get("label") == "agree"
        rn = p["rules"]
        makers["new rules (current engine)"][k] = matches({"action": "run" if rn["next"] else "stop", "id": rn["next"]}, lb, cands)
    for v, rs in res.items():
        for step in ("planner", "final"):
            name = f"{v} ({step})"
            for k, r in rs.items():
                if k not in keep:
                    continue
                R = r["result"]
                p = (R.get("plan") or {})
                if not p:
                    continue
                if step == "final":
                    if not r.get("critic_model"):
                        continue
                    if R.get("final"):
                        p = dict(p, action=R["final"]["action"], choice=R["final"]["choice"])
                ch = p.get("choice") or {}
                cands = {c["id"]: c for c in keep[k]["packet"]["candidates"]}
                makers.setdefault(name, {})[k] = matches({"action": p.get("action"), "id": ch.get("candidate_id") or None,
                                                          "vars": vars_of(ch.get("changes")) or None}, keep[k]["label"], cands)
    cls = lambda k: keep[k]["label"].get("class") or "?"
    print(f"{len(keep)} decision points" + (" (uncertain excluded)" if a.exclude_uncertain else ""))
    print(f"{'decision maker':44} {'n':>4} {'agree':>6}   " + "  ".join(f"{c:>8}" for c in ("OK", "R", "V", "S", "G")))
    for name, m in makers.items():
        if not m:
            continue
        by = collections.defaultdict(list)
        for k, ok in m.items():
            by[cls(k)].append(ok)
        cell = lambda c: f"{sum(by[c])}/{len(by[c])}" if by[c] else "-"
        print(f"{name:44} {len(m):>4} {100 * sum(m.values()) / len(m):5.0f}%   " + "  ".join(f"{cell(c):>8}" for c in ("OK", "R", "V", "S", "G")))
    if a.detail:
        with open(a.detail, "w") as f:
            for k, p in keep.items():
                lb = p["label"]
                f.write(f"## session {lb['session']} rec {k} after run {lb['after_run_seq']} [{lb.get('class')}]\n")
                f.write(f"- tuner: {(lb.get('tuner_pick') or {}).get('id')} · expert: {(lb.get('expert_pick') or {}).get('text')} · new rules: {p['rules']['next']}\n")
                for v, rs in res.items():
                    r = rs.get(k)
                    if r and (r["result"].get("plan")):
                        pl = r["result"]["plan"]
                        ch = pl.get("choice") or {}
                        f.write(f"- {v}: {pl.get('action')} {ch.get('candidate_id') or ch.get('changes')} — {pl.get('diagnosis', '')[:240]}\n")
                        if r["result"].get("critic"):
                            c = r["result"]["critic"]
                            f.write(f"  - reviewer: {c.get('verdict')} {c.get('candidate_id') or c.get('action')} — {c.get('reason', '')[:200]}\n")
                f.write("\n")


if __name__ == "__main__":
    main()
