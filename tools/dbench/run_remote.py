#!/usr/bin/env python3
"""Decision benchmark, step 2 (runs on the tuner server, where the OpenRouter key is): ask planner variants for each
decision point. Calls go through app.llm (logged in llm_calls with purpose 'dbench', so the status page counts them).

usage: run_remote.py <points.jsonl> <out.jsonl> --variant name=planner_model[,critic_model] ... [--ids rec_ids]
                     [--effort high|xhigh] [--packet packet|packet_full]
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/opt/aituner/server")
from app import llm, store  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("points")
    ap.add_argument("out")
    ap.add_argument("--variant", action="append", required=True)
    ap.add_argument("--ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--budget", type=float, default=20.0, help="stop when this many USD were spent by this run")
    ap.add_argument("--effort", default="high", help="reasoning effort passed to the model")
    ap.add_argument("--packet", default="packet", help="which packet of the point to send (packet_full: build.py --full)")
    a = ap.parse_args()
    pts = [json.loads(x) for x in open(a.points)]
    if a.ids:
        want = {int(x) for x in a.ids.split(",")}
        pts = [p for p in pts if p["label"].get("rec_id") in want]
    admin = store.q1("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")["id"]
    done = set()
    try:
        for line in open(a.out):
            r = json.loads(line)
            done.add((r["rec_id"], r["variant"]))
    except OSError:
        pass
    spent = [0.0]
    jobs = []
    for v in a.variant:
        name, models = v.split("=", 1)
        pm, cm = (models.split(",") + [None])[:2]
        for p in pts:
            if (p["label"]["rec_id"], name) not in done:
                jobs.append((name, pm, cm, p))

    def one(job):
        name, pm, cm, p = job
        if spent[0] >= a.budget:
            return None
        t0 = time.time()
        r = llm.plan(admin, None, p["label"]["session"], p.get(a.packet) or p["packet"], model=pm, critic_model=cm,
                     effort=a.effort, purpose="dbench")
        cost = sum(float((r.get(k) or {}).get("cost") or 0) for k in ("usage", "critic_usage"))
        spent[0] += cost
        return {"rec_id": p["label"]["rec_id"], "session": p["label"]["session"], "variant": name, "model": pm, "critic_model": cm, "effort": a.effort, "packet": a.packet,
                "result": {k: r.get(k) for k in ("plan", "critic", "final", "error", "critic_error", "enabled", "reason")},
                "cost": round(cost, 4), "s": round(time.time() - t0, 1)}

    with open(a.out, "a") as out, ThreadPoolExecutor(a.workers) as ex:
        for res in ex.map(one, jobs):
            if res:
                out.write(json.dumps(res) + "\n")
                out.flush()
                print(f"{res['variant']} rec {res['rec_id']}: ${res['cost']:.3f} {res['s']}s {(res['result'].get('error') or '')[:80]}", flush=True)
    print(f"spent ${spent[0]:.2f}")


if __name__ == "__main__":
    main()
