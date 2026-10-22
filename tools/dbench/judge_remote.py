#!/usr/bin/env python3
"""Decision benchmark: a second-vendor blind judge (runs on the tuner server). Same instructions and 0-2 scale as the
Claude judges; every distinct option of a decision graded together, anonymised and shuffled.

usage: judge_remote.py <input.json> <out.jsonl> --model openai/gpt-5.6-sol [--workers 4]
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/opt/aituner/server")
from app import llm, store  # noqa: E402

SYSTEM = """You are a senior MySQL/InnoDB performance engineer grading next-step choices of database tuners, blind.
The input has "evidence": what was known at that moment in an automated tuning session (server, hardware, goal, policy
"safe" = only changes without durability/observability trade-offs may run, "benchmark" = trade-offs may run; benchmark
details; diagnosis; time_breakdown = shares of database time by state; profiles; summary metrics; noise_pct =
measurement noise threshold, a change must beat it to be confirmed; history of runs so far with verdicts;
configurations = mean goal per configuration; hypotheses_tested; candidates = pre-validated possible next changes with
notes, relevance 0-4 = share of measured time the change acts on, experience = results of the same change in comparable
earlier sessions), "options": letters -> the next step a tuner chose (candidate id, concrete settings, or STOP; sources
hidden), and "auditor_note" (an earlier audit's note; may be wrong, one more opinion only).
Each run costs 20-40 minutes. The best next step has the highest expected CONFIRMED gain of the goal per run given ONLY
this evidence (no hindsight): it acts on a component with real measured time, its plausible effect can beat noise_pct,
it respects the policy, it does not repeat a refuted mechanism; stopping is right when nothing left can beat the noise
or the limit is outside the configuration. Revert/rollback items return to a measured configuration.
Grade every option: 2 = good (what a strong engineer would do now, or equally good), 1 = acceptable (defensible but
clearly not the best), 0 = poor (cannot plausibly beat noise, no mechanism, violates policy, repeats a refuted idea, or
should have stopped / should not stop). Name the best option letter. One or two sentences per option citing the evidence."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["grades", "best"],
          "properties": {"best": {"type": "string"},
                         "grades": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                               "required": ["option", "grade", "why"],
                                                               "properties": {"option": {"type": "string"},
                                                                              "grade": {"type": "integer", "enum": [0, 1, 2]},
                                                                              "why": {"type": "string"}}}}}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--model", default="openai/gpt-5.6-sol")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    items = json.load(open(a.inp))
    admin = store.q1("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")["id"]

    def one(it):
        try:
            r, u = llm._call(admin, None, None, a.model, SYSTEM, SCHEMA, "grades", json.dumps(it, sort_keys=True, default=str),
                             "dbench-judge", "high")
            return {"decision": it["decision"], "model": a.model, "result": r, "cost": (u or {}).get("cost")}
        except Exception as e:
            return {"decision": it["decision"], "model": a.model, "error": str(e)[:300]}

    spent = 0.0
    with open(a.out, "w") as f, ThreadPoolExecutor(a.workers) as ex:
        for res in ex.map(one, items):
            spent += float(res.get("cost") or 0)
            f.write(json.dumps(res) + "\n")
            f.flush()
            print(res["decision"], res.get("error") or f"${float(res.get('cost') or 0):.3f}", flush=True)
    print(f"spent ${spent:.2f}")


if __name__ == "__main__":
    main()
