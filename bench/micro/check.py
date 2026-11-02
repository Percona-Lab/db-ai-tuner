#!/usr/bin/env python3
"""check.py <case.json>: the expectations of a mechanism micro-benchmark (docs/design/micro-benchmarks.md) against the
runs of the open session - the sweep's runs, by the value in their label (`exp#N variable=value`), with the summary and
inputs the agent saved for each run (state dir, run<seq>_inputs.json). Prints PASS / FAIL per expectation and for the
case, records the result as a session event (micro_check), exits 0 when the case passes, 1 when it fails, 2 when it
could not be judged."""
import json
import os
import re
import statistics
import sys

from tuning_agent import cli
from tuning_agent.knowledge import normalize


def metric(name, rec):
    """A metric of one run from its saved record ({summary, inputs})."""
    s, inp = rec.get("summary") or {}, rec.get("inputs") or {}
    rates = inp.get("status_rates") or {}
    if ":" not in name:
        return s.get(name)
    kind, key = name.split(":", 1)
    if kind == "cpu":
        return ((inp.get("cpu_profile") or {}).get("categories") or {}).get(key)
    if kind == "off":
        return (((inp.get("offcpu") or {}).get("foreground") or {}).get(key) or {}).get("threads", 0.0)
    if kind == "im":
        return (inp.get("innodb_metrics_rates") or {}).get(key, 0.0)
    if kind == "st":
        return rates.get(key, 0.0)
    if kind == "trx":
        trx = s.get("tps") or s.get("qps")
        return rates.get(key, 0.0) / trx if trx else None
    return None


def runs_by_value(c, variable):
    """{value: [records]} of the sweep's measured runs in the open session."""
    sess = c.session()
    out = {}
    for r in sess.get("runs") or []:
        m = re.match(r"exp#\d+ .*?\b" + re.escape(variable) + r"=([^\s,()]+)", str(r.get("label") or ""))
        if not m or r.get("verdict") in ("unreliable", "superseded") or r.get("goal_value") is None:
            continue
        try:
            with open(os.path.join(c.state.dir, f"run{r['seq']}_inputs.json")) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            rec = {"summary": r.get("summary") or {}}
        rec["summary"] = dict(rec.get("summary") or {}, tps=(rec.get("summary") or {}).get("tps") or r.get("goal_value"))
        out.setdefault(normalize(variable, m.group(1)), []).append(rec)
    return out, float(((sess.get("noise") or {}).get("noise_pct")) or 2.0)


def mean(recs, name):
    vals = [v for v in (metric(name, r) for r in recs) if v is not None]
    return statistics.mean(vals) if vals else None


def judge(e, by, var, noise):
    """(ok or None when it cannot be judged, text)."""
    at = lambda v: by.get(normalize(var, v)) or []  # noqa: E731
    if e["kind"] == "response":
        a, b = mean(at(e["from"]), e["metric"]), mean(at(e["to"]), e["metric"])
        if a is None or b is None or not a:
            return None, f"{e['metric']}: not measured at {e['from']} or {e['to']}"
        d = 100.0 * (b - a) / abs(a)
        thr_up = max(e.get("min_pct", 0), noise)
        flat = max(e.get("max_pct", 0), noise)
        ok = {"up": d > thr_up, "down": d < -thr_up, "flat": abs(d) <= flat, "not_up": d <= flat, "not_down": d >= -flat}[e["direction"]]
        return ok, f"{e['metric']} {e['from']} -> {e['to']}: {a:.4g} -> {b:.4g} ({d:+.1f}%), expected {e['direction']}" + (
            f" by >= {thr_up:.1f}%" if e["direction"] in ("up", "down") else f" within ±{flat:.1f}%" if e["direction"] == "flat" else "")
    if e["kind"] in ("signal", "ratio"):
        recs = at(e["at"])
        if e["kind"] == "signal":
            v, what = mean(recs, e["metric"]), e["metric"]
        else:
            n, d = mean(recs, e["num"]), mean(recs, e["den"])
            v, what = (n / d if n is not None and d else (float("inf") if n else None)), f"{e['num']} / {e['den']}"
        if v is None:
            return None, f"{what} at {e['at']}: not measured"
        ok = {">=": v >= e["value"], "<=": v <= e["value"], ">": v > e["value"], "<": v < e["value"]}[e["op"]]
        return ok, f"{what} at {e['at']}: {v:.4g}, expected {e['op']} {e['value']}"
    return None, f"unknown expectation kind {e['kind']}"


def main(path):
    case = json.load(open(path))
    c = cli.Ctx()
    var = case["sweep"]["variable"]
    by, noise = runs_by_value(c, var)
    lines, oks = [], []
    for e in case["expect"]:
        ok, txt = judge(e, by, var, noise)
        oks.append(ok)
        lines.append(("PASS " if ok else "FAIL " if ok is False else "N/A  ") + txt)
    verdict = "PASS" if oks and all(o is True for o in oks) else "FAIL" if any(o is False for o in oks) else "NOT JUDGED"
    name = os.path.relpath(path, os.path.dirname(os.path.dirname(os.path.abspath(path))))
    head = f"micro-benchmark {name} ({case['mechanism']}, {case['role']}): {verdict} (noise ±{noise:.1f}%, runs per value: " + \
        ", ".join(f"{k} {len(v)}" for k, v in by.items()) + ")"
    print(head)
    for l in lines:
        print("   " + l)
    try:
        c.event("micro_check", head + " - " + "; ".join(lines), {"case": name, "verdict": verdict, "claim": case.get("claim"),
                                                                "results": lines, "noise_pct": noise})
    except Exception as ex:  # the verdict stands without the event
        print(f"   (event not recorded: {ex})")
    return 0 if verdict == "PASS" else 1 if verdict == "FAIL" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
