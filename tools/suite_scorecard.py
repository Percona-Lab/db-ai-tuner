#!/usr/bin/env python3
"""Scorecard of a suite run (docs/battery/suite-v1.md): per cell, what the tuner found and how well it worked.

usage: suite_scorecard.py <run id> [--compare <earlier run id>]
Reads ops/suite/<run>/<cell>.json (session ids from the runner), the sessions through the tuner API (cookie jar ops/cj)
and the session logs the runner kept; writes ops/suite/<run>/scorecard.json and prints a table.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from tuning_agent import engine, evaluate  # noqa: E402
from tuning_agent.knowledge import run_tradeoffs as tradeoffs_of_run, tradeoffs_in  # noqa: E402

URL = "https://50-116-44-237.sslip.io"
FLAGS = ("not_steady", "unsteady_verdict", "client_saturated", "goal_server_mismatch_pct", "unstable_within_run", "swap_hazard")


def api(path):
    r = subprocess.run(["curl", "-sf", "-b", os.path.join(ROOT, "ops", "cj"), URL + path], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None


def gain(goal, new, base):
    if new is None or not base:
        return None
    return round(100.0 * ((base - new) / base if goal == "latency" else (new - base) / base), 2)


def cell_card(run, st):
    sid = st.get("session")
    out = {"cell": st["cell"], "status": st["status"], "session": sid}
    if not sid:
        return out
    d = api(f"/api/sessions/{sid}")
    if not d:
        return out
    s, runs = d["session"], [r for r in d["runs"] if r["status"] == "done"]
    goal, noise = s["goal"], float((s.get("calibration") or {}).get("noise_pct") or 2.0)
    mean = lambda rs: sum(r["goal_value"] for r in rs) / len(rs) if rs else None  # noqa: E731
    defaults = mean([r for r in runs if r["label"].startswith("server defaults")])
    start = [r for r in runs if r["label"].startswith("baseline")]
    smean = mean(start)
    safe = engine.best_config([r for r in runs if not tradeoffs_of_run(r) and r["verdict"] not in evaluate.EXCLUDED], goal, noise)
    best = engine.best_config([r for r in runs if r["verdict"] not in evaluate.EXCLUDED], goal, noise)
    # search efficiency: the first run whose configuration already reached 90% of the best safe gain
    g_best = gain(goal, safe["mean"], smean) if safe else None
    to90 = None
    if g_best and g_best > 0:
        for i, r in enumerate(runs):
            if r["goal_value"] is not None and (gain(goal, r["goal_value"], smean) or 0) >= 0.9 * g_best:
                to90 = r["seq"]
                break
    hyp = {}
    for r in runs:
        h = (r.get("evaluation") or {}).get("hypothesis") or {}
        if h.get("outcome"):
            hyp[h["outcome"]] = hyp.get(h["outcome"], 0) + 1
    experiments = [r for r in runs if not r["label"].startswith(("server defaults", "baseline", "re-check", "best safe", "best with"))]
    validation = sum(1 for r in experiments if (((r.get("evaluation") or {}).get("hypothesis") or {}).get("kind") == "validation"))
    verdicts = {}
    for r in experiments:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    flags = {f: sum(1 for r in runs if (r.get("evaluation") or {}).get(f)) for f in FLAGS}
    bd = None
    if start:
        rr = api(f"/api/runs/{start[0]['id']}") or {}
        bd = [f"{p['name']} {p['share_pct']}%" for p in ((rr.get("run") or rr).get("inputs") or {}).get("time_breakdown", [])[:3]]
    log = os.path.join(ROOT, "ops", "suite", run, f"{st['cell']}-session-suite.log")
    chk = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "canary_check.py"), str(sid)] + (["--log", log] if os.path.exists(log) else []),
                         capture_output=True, text=True)
    fails = [l[5:] for l in chk.stdout.splitlines() if l.startswith("FAIL")]
    out.update({
        "goal": goal, "agent_version": s.get("agent_version"), "noise_pct": noise, "runs": len(runs), "experiments": len(experiments),
        "defaults": defaults, "start": smean, "best_safe": safe and safe["mean"], "best_safe_runs": safe and safe["seqs"],
        "best": best and best["mean"], "best_tradeoffs": best and bool(tradeoffs_in(best["managed"])),
        "gain_safe_vs_start_pct": gain(goal, safe and safe["mean"], smean), "gain_best_vs_start_pct": gain(goal, best and best["mean"], smean),
        "gain_start_vs_defaults_pct": gain(goal, smean, defaults), "runs_to_90pct": to90,
        "hypotheses": hyp, "validation_only": validation, "verdicts": verdicts, "flags": {k: v for k, v in flags.items() if v},
        "measured_bottleneck": bd, "checker_failures": fails,
    })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--compare")
    a = ap.parse_args()
    d = os.path.join(ROOT, "ops", "suite", a.run)
    cards = [cell_card(a.run, json.load(open(os.path.join(d, f)))) for f in sorted(os.listdir(d)) if f.endswith(".json") and f != "scorecard.json"]
    prev = {}
    if a.compare:
        p = os.path.join(ROOT, "ops", "suite", a.compare, "scorecard.json")
        prev = {c["cell"]: c for c in json.load(open(p))["cells"]} if os.path.exists(p) else {}
    with open(os.path.join(d, "scorecard.json"), "w") as f:
        json.dump({"run": a.run, "cells": cards}, f, indent=1, default=str)
    print(f"{'cell':<6} {'status':<7} {'sess':>5} {'noise':>6} {'safe':>7} {'+trade':>7} {'start/def':>9} {'to90':>5} {'valid.':>6} {'checker':>7}  hypotheses / flags")
    for c in cards:
        p = prev.get(c["cell"]) or {}
        delta = (f" (was {p.get('gain_safe_vs_start_pct')})" if p else "")
        print(f"{c['cell']:<6} {c['status']:<7} {c.get('session') or '':>5} {c.get('noise_pct') or '':>6} {c.get('gain_safe_vs_start_pct') or '':>7} "
              f"{c.get('gain_best_vs_start_pct') or '':>7} {c.get('gain_start_vs_defaults_pct') or '':>9} {c.get('runs_to_90pct') or '':>5} "
              f"{c.get('validation_only', ''):>6} {len(c.get('checker_failures') or []):>7}  {c.get('hypotheses') or ''} {c.get('flags') or ''}{delta}")


if __name__ == "__main__":
    main()
