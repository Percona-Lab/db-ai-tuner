#!/usr/bin/env python3
"""Check a finished autotune session for the failure modes found in Battery 1 / 2.

usage: canary_check.py <session id> [--log path/to/session.log] [--budget N]

Reads the session through the tuner API (cookie jar ops/cj) and re-checks it with the agent's own evaluate / engine code.
Exit code 0 = all checks passed. Meant to run after bench/canary.sh, before rolling an agent change out to long studies.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from tuning_agent import engine, evaluate  # noqa: E402
from tuning_agent.knowledge import run_tradeoffs as tradeoffs_of_run, tradeoffs_in  # noqa: E402

URL = "https://50-116-44-237.sslip.io"
SKIP = ("server defaults", "baseline", "re-check", "best safe", "best with")


def api(path):
    out = subprocess.run(["curl", "-sf", "-b", os.path.join(ROOT, "ops", "cj"), URL + path], capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"API {path} failed")
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=int)
    ap.add_argument("--log", help="session log (session-*.log) to scan for agent errors")
    ap.add_argument("--budget", type=int, help="max-runs the session was given")
    a = ap.parse_args()
    d = api(f"/api/sessions/{a.session}")
    s, runs = d["session"], [r for r in d["runs"] if r["status"] == "done"]
    noise = s.get("calibration") or {}
    fails, notes = [], []

    def check(ok, what):
        (notes if ok else fails).append(("ok   " if ok else "FAIL ") + what)

    check(s["status"] == "closed", f"session closed (status {s['status']})")
    aborted = [r for r in d["runs"] if r["status"] == "aborted"]
    check(not aborted, f"no aborted runs ({[(r['seq'], (r.get('evaluation') or {}).get('reason')) for r in aborted]})")
    check(bool(noise.get("noise_pct")), f"calibrated noise threshold ({noise.get('noise_pct')}%)")

    # 1) the agent itself: no tracebacks, no stop with budget left
    log = open(a.log).read() if a.log and os.path.exists(a.log) else ""
    if log:
        check("Traceback" not in log, "no Python traceback in the session log")
    stops = re.findall(r"^== (.*(?:stopping|no further experiments).*)$", log, re.M)
    exp = [r for r in runs if not r["label"].startswith(SKIP)]
    if a.budget:
        legit = any("nothing else is left" in x or "no further experiments" in x for x in stops)
        # the agent's own count (sweep points rejected before a run count against the budget too)
        used = [int(x) for x, n in re.findall(r"trade-off budget: (\d+) of (\d+) runs used", log) if int(n) == a.budget]
        legit = legit or (bool(used) and max(used) >= a.budget - 1)
        check(len(exp) >= a.budget or legit, f"budget used: {len(exp)} of {a.budget} experiment runs (stops: {stops or 'no log'})")

    # 2) verdicts: recompute every comparison with the current evaluate code; parents must match
    hist = []
    for r in runs:
        rr = dict(r)
        hist.append(rr)
    mism = []
    for i, r in enumerate(hist):
        ev = r.get("evaluation") or {}
        if r["verdict"] in ("reference", "repeat", "unreliable", "superseded") or not ev.get("parent_runs"):
            continue
        key = evaluate.config_key(r.get("managed"))
        pk = evaluate._parent_key(r, hist[:i], key)
        want = [x["seq"] for x in hist[:i] if evaluate.config_key(x.get("managed")) == pk]
        if pk and not set(ev["parent_runs"]) <= set(want):
            mism.append((r["seq"], r["label"][:30], ev["parent_runs"], want[-3:]))
    check(not mism, f"comparison parents consistent ({len(mism)} mismatches: {mism[:4]})")

    # 3) every improvement confirmed by a repeat (or the budget ended right after it)
    unconf = []
    for r in hist:
        if r["verdict"] != "improved":
            continue
        m = re.match(r"exp#(\d+) ", r["label"])
        if m and any(x["label"].startswith(f"exp#{m.group(1)} ") and "(confirm)" in x["label"] for x in hist):
            continue  # a sweep point the sweep did not pick: the chosen point was confirmed
        k = evaluate.config_key(r.get("managed"))
        reps = [x for x in hist if evaluate.config_key(x.get("managed")) == k and x["seq"] > r["seq"]]
        if not reps and r["seq"] != hist[-1]["seq"]:
            unconf.append(r["seq"])
        if (r.get("evaluation") or {}).get("confirmed") is False:
            unconf.append(r["seq"])
    check(not unconf, f"improvements confirmed by a repeat (unconfirmed: {unconf})")

    # 4) the reported best safe configuration is earned and carries no change that was neutral on its own
    safe = [r for r in hist if not tradeoffs_of_run(r)]
    best = engine.best_config(safe, s["goal"], float(noise.get("noise_pct") or 2.0))
    rep = (s.get("summary") or {}).get("best_safe")  # older agents stored only the overall best (may include trade-offs)
    if best:
        check(not rep or best["key"] == evaluate.config_key(next((x["managed"] for x in hist if x["seq"] == rep.get("seq")), {})) or
              set(rep.get("runs") or []) & set(best["seqs"]),
              f"reported best safe config matches a recomputation (reported {rep and rep.get('runs')}, recomputed {best['seqs']})")
        start = next((x for x in hist if x["label"].startswith("baseline")), hist[0])
        extra = {k for k in best["managed"] if start.get("managed", {}).get(k) != best["managed"][k]}
        kept_vars = set()
        for x in hist:
            ok = x["verdict"] in ("improved", "efficiency-gain") or (x["label"].startswith("best-practices") and x["verdict"] == "neutral")
            if ok:
                kept_vars |= {c["variable"] for c in x.get("changes") or []}
        stray = sorted(extra - kept_vars)
        check(not stray, f"best safe config has no change without a winning run behind it (stray: {stray})")

    # 4b) the best safe configuration is not measurably worse than the starting configuration
    if best:
        start = next((x for x in hist if x["label"].startswith("baseline")), None)
        if start:
            sm = [x["goal_value"] for x in hist if evaluate.config_key(x.get("managed")) == evaluate.config_key(start.get("managed"))
                  and x.get("goal_value") is not None and x["verdict"] not in evaluate.EXCLUDED]
            if sm:
                smean = sum(sm) / len(sm)
                worse = (best["mean"] - smean) / abs(smean) * 100 * (-1 if s["goal"] == "latency" else 1)
                nz = float(noise.get("noise_pct") or 2.0)
                check(worse > -evaluate.mean_noise(nz, best["n"], len(sm)),
                      f"best safe config not worse than the start beyond noise ({worse:+.1f}% vs start, noise ±{nz}%)")

    # 4c) no run judged while the benchmark client was saturated; the session ran a known agent build
    capped = [r["seq"] for r in hist if evaluate.client_saturated(r.get("summary") or {}) and r["verdict"] not in evaluate.EXCLUDED]
    check(not capped, f"no client-saturated run judged ({capped})")
    check(bool(s.get("agent_version")), f"agent build recorded for the session ({s.get('agent_version')})")

    # 5) trade-off phase reached when the budget allowed it
    if s.get("policy") == "benchmark" and a.budget and len([r for r in hist if not r["label"].startswith(SKIP)]) >= a.budget:
        check(any(tradeoffs_of_run(r) for r in hist), "trade-off phase reached")

    for line in notes + fails:
        print(line)
    print(f"\nsession {a.session}: {len(runs)} runs, {len(fails)} failed check(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
