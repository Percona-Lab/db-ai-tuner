#!/usr/bin/env python3
"""Replay the engine on the last run of finished sessions (read-only tuner DB) and show what the evidence gate does.

usage: evidence_replay.py <tuner.db> <agent dir> <session id>...
For each session: the time breakdown, the candidates with evidence and the ones demoted to validation (with the reason).
"""
import json
import sqlite3
import sys

db, agent_dir, sids = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3:]]
sys.path.insert(0, agent_dir)
from tuning_agent import engine  # noqa: E402

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
J = lambda x: json.loads(x) if x else {}  # noqa: E731
for sid in sids:
    s = con.execute("SELECT t.*, e.facts FROM tsessions t JOIN environments e ON e.id=t.env_id WHERE t.id=?", (sid,)).fetchone()
    runs = con.execute("SELECT * FROM runs WHERE session_id=? AND status='done' ORDER BY seq", (sid,)).fetchall()
    if not s or not runs:
        continue
    last = runs[-1]
    facts = J(s["facts"])
    hist = [{"seq": r["seq"], "label": r["label"], "managed": J(r["managed"]), "changes": J(r["changes"]), "verdict": r["verdict"],
             "goal_value": r["goal_value"], "evaluation": J(r["evaluation"]), "summary": J(r["summary"])} for r in runs]
    cfg = J(last["config"])
    v = cfg.get("variables") or cfg
    ctx = {"goal": s["goal"], "policy": s["policy"], "hw": facts.get("hw") or facts, "variables": v, "summary": J(last["summary"]),
           "inputs": J(last["inputs"]), "history": hist, "noise_pct": 2.0, "managed": J(last["managed"]), "os_tuning": True,
           "flavor": "percona", "version": (8, 4, 0)}
    try:
        res = engine.run_engine(ctx)
    except Exception as e:  # noqa
        print(f"#{sid}: engine error {e}")
        continue
    print(f"\n#{sid} {s['name'][:70]}  (run {last['seq']})")
    for p in res.get("breakdown", [])[:6]:
        print(f"   {p['share_pct']:5.1f}%  {p['name']}")
    cfg_items = [i for i in res["items"] if i["kind"] == "config"]
    ev = [i for i in cfg_items if not i.get("no_mechanism")]
    no = [i for i in cfg_items if i.get("no_mechanism")]
    print(f"   candidates: {len(cfg_items)}, with evidence {len(ev)}, validation only {len(no)}")
    for i in ev[:8]:
        print(f"     + {i['id']:<28} {', '.join(i.get('addresses') or [])[:90]}")
    for i in no[:12]:
        print(f"     - {i['id']:<28} {i['no_mechanism'][:110]}")
