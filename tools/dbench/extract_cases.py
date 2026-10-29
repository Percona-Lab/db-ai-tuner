#!/usr/bin/env python3
"""Case base for the similarity-ranking prototype: one case per measured experiment = (the parent run's measured state,
the change, the outcome). Runs on the tuner server against the database read-only; writes JSON lines.

usage: extract_cases.py [--db /var/lib/aituner/tuner.db] > cases.jsonl
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features  # noqa: E402

MEASURED = ("improved", "efficiency-gain", "neutral", "regressed", "efficiency-loss")


_DB = None
_DICTS = {}


def _text(x):
    """JSON text of a stored value: run documents are zstd-compressed since 2026-10-29 (server/app/codec.py)."""
    if x is None or isinstance(x, str) or not bytes(x).startswith(b"\x28\xb5\x2f\xfd"):
        return x.decode() if isinstance(x, (bytes, memoryview)) else x
    import zstandard as zstd
    b = bytes(x)
    did = zstd.get_frame_parameters(b).dict_id
    if did and did not in _DICTS:
        _DICTS[did] = zstd.ZstdCompressionDict(bytes(_DB.execute("SELECT data FROM zdicts WHERE id=?", (did,)).fetchone()[0]))
    return (zstd.ZstdDecompressor(dict_data=_DICTS[did]) if did else zstd.ZstdDecompressor()).decompress(b).decode()


def J(x, d=None):
    try:
        return json.loads(_text(x)) if x else d
    except (TypeError, ValueError):
        return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/var/lib/aituner/tuner.db")
    a = ap.parse_args()
    global _DB
    c = _DB = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    sess = {r["id"]: r for r in c.execute(
        "SELECT s.id, s.name, s.goal, s.policy, s.noise_pct, s.created, s.env_id, e.facts, COALESCE(st.name,'') study "
        "FROM tsessions s JOIN environments e ON e.id=s.env_id LEFT JOIN studies st ON st.id=s.study_id")}
    n = 0
    for sid, s in sess.items():
        if "canary" in str(s["name"] or "").lower() or str(s["study"]).lower() == "canary":
            continue
        facts = J(s["facts"], {}) or {}
        cpus = (facts.get("hardware") or {}).get("cpus")
        runs = {r["seq"]: r for r in c.execute("SELECT seq, label, verdict, changes, evaluation, summary, inputs, config, managed, started "
                                               "FROM runs WHERE session_id=? AND status='done' ORDER BY seq", (sid,))}
        for seq, r in runs.items():
            if r["verdict"] not in MEASURED:
                continue
            ev = J(r["evaluation"], {}) or {}
            par = (ev.get("parent_runs") or [None])[-1]
            p = runs.get(par)
            if p is None or ev.get("goal_delta_pct") is None:
                continue
            chl = [x for x in (J(r["changes"], []) or []) if not str(x.get("source") or "").startswith(("rollback", "rec:revert", "rec:drop-"))]
            if not chl or len(chl) > 6:
                continue  # reverts measure the way back; big bundles are not one change
            psum, pinp = J(p["summary"], {}) or {}, J(p["inputs"], {}) or {}
            if not (pinp.get("cpu_profile") or {}).get("categories"):
                # profiled once per configuration: the latest earlier run of the parent's configuration has it
                mk = json.dumps(J(p["managed"], {}) or {}, sort_keys=True)
                for q in sorted((x for x in runs.values() if x["seq"] < par), key=lambda x: -x["seq"]):
                    if json.dumps(J(q["managed"], {}) or {}, sort_keys=True) == mk:
                        cp = (J(q["inputs"], {}) or {}).get("cpu_profile")
                        if (cp or {}).get("categories"):
                            pinp = dict(pinp, cpu_profile=cp)
                            break
            pcfg = J(p["config"], {}) or {}
            vec = features.vector(psum, pinp, cpus, pcfg.get("innodb_buffer_pool_size"), pinp.get("data_bytes"))
            noise = ev.get("goal_noise_pct") or s["noise_pct"] or 2.0
            srcs = sorted({str(x.get("source") or "") for x in chl})
            rule = next((x[4:] for x in srcs if x.startswith("rec:")), None)
            print(json.dumps({
                "session": sid, "seq": seq, "parent": par, "env": s["env_id"], "goal": s["goal"], "policy": s["policy"],
                "session_start": s["created"], "started": r["started"], "label": r["label"], "verdict": r["verdict"],
                "rule": rule, "sources": srcs, "changes": {str(x["variable"]).lower(): str(x.get("new")).lower() for x in chl},
                "delta": ev.get("goal_delta_pct"), "noise": noise, "z": round(float(ev["goal_delta_pct"]) / float(noise), 3),
                "not_steady": bool(ev.get("not_steady")), "cpus": cpus, "vec": vec,
                "wl": (pinp.get("workload") or {}).get("type"), "conns": psum.get("threads_connected_avg")}))
            n += 1
    print(f"{n} cases", file=sys.stderr)


if __name__ == "__main__":
    main()
