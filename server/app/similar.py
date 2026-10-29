"""Case base for the similarity ranking (simcore): one case per measured experiment of every account - the parent run's
measured state, the change, the outcome in noise widths. Built from the runs table and cached; rebuilt in the background
when older than REFRESH_S. Predictions are aggregates; neighbours from other accounts are shown without their session."""
import json
import threading
import time

from . import simcore, store

REFRESH_S = 1800
MEASURED = ("improved", "efficiency-gain", "neutral", "regressed", "efficiency-loss")
_cache = {"t": 0, "cases": [], "st": {}, "owner": {}}
_lock = threading.Lock()
_building = threading.Event()


def _j(x, d=None):
    from .codec import unpack
    try:
        return json.loads(unpack(x)) if x else d
    except (TypeError, ValueError):
        return d


def build():
    """All cases (same rules as tools/dbench/extract_cases.py, which the prototype was scored on)."""
    sess = {r["id"]: dict(r) for r in store.q(
        "SELECT s.id, s.name, s.goal, s.noise_pct, s.created, e.facts, e.user_id, COALESCE(st.name,'') study FROM tsessions s "
        "JOIN environments e ON e.id=s.env_id LEFT JOIN studies st ON st.id=s.study_id")}
    cases, owner = [], {}
    for sid, s in sess.items():
        if "canary" in str(s["name"] or "").lower() or str(s["study"]).lower() == "canary":
            continue
        owner[sid] = s["user_id"]
        cpus = ((_j(s["facts"], {}) or {}).get("hardware") or {}).get("cpus")
        runs = {r["seq"]: r for r in store.q("SELECT seq, label, verdict, changes, evaluation, summary, inputs, config, managed FROM runs "
                                             "WHERE session_id=? AND status='done' ORDER BY seq", (sid,))}
        for seq, r in runs.items():
            if r["verdict"] not in MEASURED:
                continue
            ev = _j(r["evaluation"], {}) or {}
            par = (ev.get("parent_runs") or [None])[-1]
            p = runs.get(par)
            if p is None or ev.get("goal_delta_pct") is None:
                continue
            chl = [x for x in (_j(r["changes"], []) or []) if not str(x.get("source") or "").startswith(("rollback", "rec:revert", "rec:drop-"))]
            if not chl or len(chl) > 6:
                continue
            psum, pinp = _j(p["summary"], {}) or {}, _j(p["inputs"], {}) or {}
            if not (pinp.get("cpu_profile") or {}).get("categories"):
                mk = json.dumps(_j(p["managed"], {}) or {}, sort_keys=True)
                for q in sorted((x for x in runs.values() if x["seq"] < par), key=lambda x: -x["seq"]):
                    if json.dumps(_j(q["managed"], {}) or {}, sort_keys=True) == mk:
                        cp = (_j(q["inputs"], {}) or {}).get("cpu_profile")
                        if (cp or {}).get("categories"):
                            pinp = dict(pinp, cpu_profile=cp)
                            break
            pcfg = _j(p["config"], {}) or {}
            noise = ev.get("goal_noise_pct") or s["noise_pct"] or 2.0
            srcs = sorted({str(x.get("source") or "") for x in chl})
            cases.append({"session": sid, "seq": seq, "label": r["label"], "verdict": r["verdict"],
                          "rule": next((x[4:] for x in srcs if x.startswith("rec:")), None),
                          "changes": {str(x["variable"]).lower(): str(x.get("new")).lower() for x in chl},
                          "delta": ev.get("goal_delta_pct"), "z": round(float(ev["goal_delta_pct"]) / float(noise), 3),
                          "vec": simcore.vector(psum, pinp, cpus, pcfg.get("innodb_buffer_pool_size"), pinp.get("data_bytes")),
                          "facets": simcore.facets(psum, pinp, cpus, pcfg.get("innodb_buffer_pool_size"), pinp.get("data_bytes"), s["goal"])})
    st = simcore.standardiser(cases)
    for c in cases:
        c["_z"] = simcore.z(c["vec"], st)
    return cases, st, owner


def _refresh():
    try:
        cases, st, owner = build()
        with _lock:
            _cache.update(t=time.time(), cases=cases, st=st, owner=owner)
    finally:
        _building.clear()


def casebase(wait=False):
    """(cases, standardiser, owner by session). The first call builds it (~10 s); later ones refresh in the background."""
    if not _cache["cases"] and wait:
        if not _building.is_set():
            _building.set()
            _refresh()
        else:
            for _ in range(60):
                if _cache["cases"]:
                    break
                time.sleep(0.5)
    elif time.time() - _cache["t"] > REFRESH_S and not _building.is_set():
        _building.set()
        threading.Thread(target=_refresh, daemon=True).start()
    with _lock:
        return _cache["cases"], _cache["st"], _cache["owner"]


def predict(user_id, session_id, vector, candidates, k=simcore.K):
    """Predictions for the candidates from every account's earlier sessions (not this session)."""
    cases, st, owner = casebase(wait=True)
    pool = [c for c in cases if c["session"] != session_id]
    preds = simcore.predictions(vector or {}, candidates or [], pool, st, k)
    for p in preds.values():
        for nb in p["neighbours"]:
            nb["own"] = owner.get(nb["session"]) == user_id
            if not nb["own"]:
                nb["session"] = nb["seq"] = None  # another account's: only its outcome and conditions
    return {"cases": len(pool), "sessions": len({c["session"] for c in pool}), "predictions": preds}
