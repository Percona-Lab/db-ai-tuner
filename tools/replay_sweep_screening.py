"""Replay historical agent / LLM sweeps through screening rules: runs saved vs gains missed (local optima)."""
import json, sys, collections
sys.path.insert(0, "agent")
from tuning_agent.engine import _numval
from tuning_agent import knowledge

S = json.load(open("docs/research/data/sweeps-2026-10-20.json"))
rows = []
for o in S:
    arms = [a for a in (o["arms"] or []) if a.get("status") == "measured" and a.get("delta_pct") is not None]
    pv = _numval(o["variable"], o["parent_value"]) if o["parent_value"] is not None else None
    pts = []
    for a in arms:
        x = _numval(o["variable"], a["arm"].split("=", 1)[1])
        if x is not None:
            pts.append((x, a["delta_pct"]))
    if len(pts) < 2 or pv is None or not o.get("noise"):
        continue
    nz = float(o["noise"])
    up = sorted([p for p in pts if p[0] > pv]); dn = sorted([p for p in pts if p[0] < pv], reverse=True)
    better = [p for p in pts if p[1] > nz]
    rows.append(dict(o=o, nz=nz, pv=pv, up=up, dn=dn, better=better, shape=knowledge.response_shape(o["variable"])))

def sim(rule):
    used = missed = 0; miss = []
    for r in rows:
        meas = []
        for side in (r["up"], r["dn"]):
            if not side:
                continue
            if rule == "nearest":      # nearest point on each side screens it; farther points only if it moved beyond noise
                order = side
            elif rule == "widest":     # widest point on each side screens it
                order = [side[-1]] + side[:-1]
            seen = [order[0]]
            if abs(order[0][1]) > r["nz"]:
                seen = order       # sensitive: refine (here: everything the sweep measured)
            meas += seen
        used += len(meas)
        if r["better"] and not any(p[1] > r["nz"] for p in meas):
            missed += 1; miss.append((r["o"]["session"], r["o"]["variable"], r["pv"], [(x, round(d, 1)) for x, d in r["up"] + r["dn"]], r["nz"]))
    return used, missed, miss

total = sum(len(r["up"]) + len(r["dn"]) for r in rows)
cls = collections.Counter()
for r in rows:
    if not any(abs(d) > r["nz"] for _, d in r["up"] + r["dn"]):
        cls["flat"] += 1
    elif r["better"]:
        # interior peak: the best point is not the farthest on its side, and the farthest is not better beyond noise
        bx, bd = max(r["better"], key=lambda p: p[1])
        side = r["up"] if bx > r["pv"] else r["dn"]
        cls["peak at the far end" if bx == side[-1][0] else "interior peak" if side[-1][1] <= r["nz"] else "better, rising"] += 1
    else:
        cls["only worse"] += 1
print(f"{len(rows)} numeric sweeps with a parent value, {total} runs; shapes: {dict(cls)}")
for rule in ("nearest", "widest"):
    u, m, miss = sim(rule)
    print(f"\nscreen by {rule} point per side: {u} runs ({total - u} saved, {100 * (total - u) / total:.0f}%), gains missed in {m} sweeps")
    for x in miss:
        print("   missed:", x)


def sim2(first, hint):
    """first: 'nearest' / 'widest' screening point per side; hint: a screen result above hint x noise (either sign)
    is not called flat - the next point on that side is measured too."""
    used = 0; miss = []
    for r in rows:
        meas = []
        for side in (r["up"], r["dn"]):
            if not side:
                continue
            order = side if first == "nearest" else [side[-1]] + side[:-1]
            seen = [order[0]]
            if abs(order[0][1]) > r["nz"]:
                seen = order
            elif abs(order[0][1]) > hint * r["nz"] and len(order) > 1:
                seen = order[:2] if abs(order[1][1]) <= r["nz"] else order
            meas += seen
        used += len(meas)
        if r["better"] and not any(p[1] > r["nz"] for p in meas):
            miss.append((r["o"]["session"], r["o"]["variable"]))
    return used, miss


print("\nwith a 'hint' band (screen result above hint x noise -> measure the next point too):")
for first in ("nearest", "widest"):
    for hint in (0.25, 0.5, 0.75):
        u, m = sim2(first, hint)
        print(f"  {first:<8} hint {hint}: {u} runs ({100 * (total - u) / total:.0f}% saved), missed {m}")
dup = collections.Counter((r["o"]["session"], r["o"]["variable"]) for r in rows)
print("\nsame variable swept more than once in a session:", {k: v for k, v in dup.items() if v > 1})
