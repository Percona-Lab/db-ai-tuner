"""AI planner: a model chooses the next run (or to stop) from the full evidence, and the code checks the choice.

The rule engine still measures, diagnoses, builds the candidate list and scores it; the planner sees all of that plus the
history and decides. The code keeps the last word on what may run: safety policy, settings that exist on this server,
memory budget, prerequisites, already tried. A choice that fails a check falls back to the engine's own pick.
"""
from . import llmassist, mechanisms

PLAN_VERSION = "1"
ACTIONS = ("run", "stop")
_CAND_KEYS = ("id", "title", "kind", "safety", "score", "relevance", "confidence", "addresses", "note", "score_note", "no_mechanism",
              "restart", "risk")


def _cand(i):
    c = {k: i.get(k) for k in _CAND_KEYS if i.get(k) not in (None, "", [], {})}
    c["changes"] = [f"{x['variable']}: {x.get('current')} -> {x['proposed']}" for x in i.get("changes") or []]
    if i.get("sweep"):
        c["sweep_values"] = i["sweep"].get("values")
    if i.get("rationale"):
        c["rationale"] = str(i["rationale"])[:400]
    h = i.get("hypothesis") or {}
    if h.get("text"):
        c["hypothesis"] = str(h["text"])[:400]
    if h.get("track_record"):
        c["track_record"] = h["track_record"]
    e = i.get("experience")
    if e:
        c["experience"] = {k: e.get(k) for k in ("n", "wins", "losses", "mean_delta_pct", "scope") if e.get(k) is not None}
    if i.get("tradeoffs"):
        c["tradeoffs"] = [t.get("lost") for t in i["tradeoffs"]][:2]
    mb = (i.get("evidence") or {}).get("memory_budget")
    if mb:
        c["memory_budget_after"] = mb
    if i.get("insights"):
        c["learned"] = [x.get("statement") for x in i["insights"]][:3]
    b = i.get("basis") or {}
    if b.get("learned"):  # what earlier sessions (all accounts) say about this idea, with the score each part added
        c["learned_basis"] = [f"{x['what']} ({x.get('score', 0):+})" for x in b["learned"]][:4]
    if i.get("known_neutral"):
        c["known_neutral"] = i["known_neutral"]
    return c


def _profile(inp):
    """Top CPU categories (% of on-CPU samples) and off-CPU states (average threads) - the detail behind time_breakdown."""
    out = {}
    cp = (inp.get("cpu_profile") or {}).get("categories") or {}
    if cp:
        out["cpu_profile_pct"] = dict(sorted(cp.items(), key=lambda x: -(x[1] or 0))[:12])
    oc = (inp.get("offcpu") or {}).get("foreground") or {}
    if oc:
        out["offcpu_threads"] = {k: round(v.get("threads") or 0, 2) for k, v in sorted(oc.items(), key=lambda x: -(x[1].get("threads") or 0))[:10]}
    if inp.get("wait_classes"):
        out["wait_classes"] = inp["wait_classes"]
    if inp.get("profile_basis"):
        out["profile_from_run"] = inp["profile_basis"]
    return out


def packet(ctx, res, meta=None):
    """Everything the planner decides from: the assistant context (server facts, breakdown, history, configurations,
    hypotheses) plus the full candidate list with the engine's scores, profiles, noise and the engine's own decision."""
    p = llmassist.context(ctx, res, meta)
    p.pop("rule_candidates", None)
    hist = ctx.get("history") or []
    p["runs_so_far"] = len(hist)
    p["engine"] = {"next": res.get("next"), "phase": res.get("phase"), "stop_reason": res.get("stop_reason")}
    p["candidates"] = [_cand(i) for i in res["items"] if i["kind"] in ("config", "revert", "advice") and i.get("changes")][:30]
    p["profiles"] = _profile(ctx.get("inputs") or {})
    p["noise"] = {"noise_pct": ctx.get("noise_pct"), "model": ctx.get("noise")}
    p["managed_now"] = ctx.get("managed") or {}
    # the tuner's memory budget: the planner sized a 51 GiB pool as "51.4 of 62.8 GiB" while the budget (reserves, every
    # allowed connection) said 69.1 - a memory trade-off it did not know it was choosing (session 294)
    from .engine import STABILITY_FIRST, unbounded_hll
    u = unbounded_hll(ctx.get("summary"))
    if STABILITY_FIRST and u:
        p["stability_first"] = {"unbounded_history": u, "rule": "the current configuration is unsustainable (undo history grows without "
                                "bound): choose a stability remedy (purge priority, purge lag bound, purge threads, purge batch size) before any "
                                "tuning experiment for the first two steps; after two remedies without success rank them like other "
                                "ideas. A change that ends the growth is kept even at some cost in throughput"}
    mb = (ctx.get("summary") or {}).get("memory_budget") or {}
    if mb:
        p["memory_budget_now"] = {"text": mb.get("text"), "over_budget": mb.get("over_budget"),
                                  "rule": "a configuration needing more than RAM is a memory trade-off (not production-safe); more than RAM "
                                          "plus free swap is not run (OOM kill)",
                                  "swap_total_mb": (ctx.get("summary") or {}).get("swap_total_mb")}
    return p


def check(plan, ctx, res):
    """Apply a planner decision to the engine result. Returns (next_id, items_added, rejected_reason)."""
    if not plan or plan.get("action") not in ACTIONS:
        return res.get("next"), [], "no usable plan"
    benchmark = ctx.get("policy") == "benchmark"
    if plan["action"] == "stop":
        return None, [], None
    ch = plan.get("choice") or {}
    cid = (ch.get("candidate_id") or "").strip()
    items = {i["id"]: i for i in res["items"]}
    done = set(ctx.get("failed") or [])
    if cid and cid not in items and "=" in cid and not ch.get("changes"):
        # a setting written where a candidate id belongs ("ps:profile=lite"): read it as the change it names
        var, _, val = cid.partition("=")
        ch = dict(ch, candidate_id="", changes=[{"variable": var.strip(), "value": val.split()[0].strip() if val.strip() else ""}])
        cid = ""
    if cid:
        it = items.get(cid)
        if not it:
            return res.get("next"), [], f"candidate {cid} is not in the list"
        if it["kind"] not in ("config", "revert") and not (it["kind"] == "advice" and benchmark and it.get("safety") == "trade-off"):
            return res.get("next"), [], f"candidate {cid} is {it['kind']}, not runnable"
        if it.get("safety") == "trade-off" and not benchmark:
            return res.get("next"), [], f"candidate {cid} is a trade-off and the policy is safe"
        if it.get("confidence", 0) <= 0 or cid in done:
            return res.get("next"), [], f"candidate {cid} is ruled out ({(it.get('note') or 'confidence 0')[:120]})"
        if it["kind"] == "advice":
            it["kind"] = "config"
        return cid, [], None
    changes = [{"variable": c.get("variable"), "value": c.get("value")} for c in ch.get("changes") or [] if c.get("variable")]
    if not changes:
        return res.get("next"), [], "the plan names neither a candidate nor changes"
    sg = {"title": plan.get("title") or "planner proposal", "changes": changes, "mechanism": plan.get("mechanism"),
          "rationale": plan.get("rationale") or plan.get("diagnosis"), "risk": plan.get("risk") or "low",
          "expected_effect": "moderate", "sweep_values": ch.get("sweep_values") or []}
    its, rej = llmassist.sanitize([sg], ctx, res, plan.get("_model") or "planner")
    if not its:
        return res.get("next"), [], "; ".join(r["reason"] for r in rej) or "rejected"
    it = its[0]
    nomech = mechanisms.missing_for_variables(it["changes"], ctx.get("summary"), (ctx.get("inputs") or {}).get("status_rates"), ctx.get("inputs"))
    if nomech:
        return res.get("next"), [], f"no mechanism: {nomech}"
    if it["safety"] == "trade-off" and not benchmark:
        return res.get("next"), [], "the proposed change is a trade-off and the policy is safe"
    from .engine import oversubscription_block
    ob = oversubscription_block(dict(it, changes=[dict(c, proposed=c.get("proposed", c.get("value"))) for c in it["changes"]]), ctx)
    if ob:
        return res.get("next"), [], ob
    it["id"] = "plan-" + it["id"][4:]
    it["source"] = "planner"
    it["confidence"] = 2
    return it["id"], [it], None
