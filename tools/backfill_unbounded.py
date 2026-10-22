#!/usr/bin/env python3
"""Flag stored runs whose history list / change buffer grew without bound (collector.unbounded_growth), from the 10 s
samples the server keeps, for runs measured by agents older than the detection (c108ee6). Runs on the tuner server.
Writes only summary.unbounded_growth where it is missing (the agent never rewrites a run's summary; the run list reads
it), and logs every changed run id to the undo file.

usage: backfill_unbounded.py [--apply] [--since-days 14] [--undo /root/backfill-unbounded.undo]
"""
import argparse
import json
import sys
import time

sys.path.insert(0, "/opt/aituner/server")
sys.path.insert(0, "/opt/aituner/agent")
import types  # noqa: E402
sys.modules.setdefault("pymysql", types.ModuleType("pymysql"))  # the collector's sample math needs no database driver
from app import store  # noqa: E402
from tuning_agent import collector  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--since-days", type=float, default=14)
    ap.add_argument("--undo", default="/root/backfill-unbounded.undo")
    a = ap.parse_args()
    rows = store.q("SELECT r.id, r.session_id, r.seq, r.label, r.started, r.ended, r.summary, r.inputs, s.env_id FROM runs r "
                   "JOIN tsessions s ON s.id=r.session_id WHERE r.status='done' AND r.started > ? AND r.ended IS NOT NULL "
                   "AND (r.summary NOT LIKE '%unbounded_growth%' OR r.summary IS NULL)", (time.time() - a.since_days * 86400,))
    found = 0
    undo = open(a.undo, "a") if a.apply else None
    for r in rows:
        b = (store.uj(r["inputs"], {}) or {}).get("bench") or {}
        w = int(b["warmup"]) if str(b.get("warmup") or "").isdigit() else 0
        ms = store.q("SELECT ts, data FROM metrics WHERE env_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                     (r["env_id"], r["started"] - max(60, 0.2 * w), r["ended"]))
        sm = [{"ts": m["ts"], "db": (json.loads(m["data"]).get("db") or {})} for m in ms]
        win = [x for x in sm if x["ts"] >= r["started"]]
        tail = [x for x in sm if x["ts"] < r["started"]]
        u = collector.unbounded_growth(win, tail)
        if not u:
            continue
        found += 1
        print(f"run {r['id']} (session {r['session_id']} #{r['seq']} {r['label']}): " +
              "; ".join(f"{x['label']} {x['start']:,} -> {x['end']:,}, {x['per_hour']:,}/h" for x in u))
        if a.apply:
            s = store.uj(r["summary"], {}) or {}
            s["unbounded_growth"] = u
            s["unbounded_growth_source"] = "server backfill from stored samples"
            store.ex("UPDATE runs SET summary=? WHERE id=?", (json.dumps(s), r["id"]))
            undo.write(json.dumps({"run": r["id"], "added": "summary.unbounded_growth"}) + "\n")
    print(f"{found} of {len(rows)} runs grew without bound" + ("" if a.apply else " (dry run: --apply to write)"))


if __name__ == "__main__":
    main()
