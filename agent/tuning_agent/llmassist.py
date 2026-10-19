"""Optional LLM suggestions (through the tuner server / OpenRouter), sanitized before they can become experiments."""
import re

from .knowledge import KB, normalize, parse_size, tradeoff_of
from .mechanisms import activity as _activity

DENY = {"datadir", "socket", "port", "server_id", "read_only", "super_read_only", "sql_mode", "character_set_server",
        "collation_server", "gtid_mode", "enforce_gtid_consistency", "log_error", "secure_file_priv", "bind_address",
        "basedir", "tmpdir", "innodb_data_home_dir", "innodb_log_group_home_dir", "pid_file", "plugin_dir", "lower_case_table_names",
        "innodb_page_size", "default_authentication_plugin", "authentication_policy", "admin_address", "mysqlx_port"}
SUMMARY_KEYS = ("tps", "qps", "avg_latency_ms", "p95_latency_ms", "cpu_busy", "mysqld_cpu_pct", "cpu_iowait", "bp_hit_pct", "bp_disk_reads",
                "redo_mbs", "checkpoint_age_pct_max", "data_fsyncs", "fsyncs_per_trx", "row_lock_waits", "row_lock_wait_avg_ms",
                "threads_running_avg", "disk_r_iops", "disk_w_iops", "disk_util", "disk_await_ms", "psi_cpu_some", "psi_io_some",
                "psi_memory_some", "mysqld_cpu_ms_per_trx", "disk_ios_per_trx", "mem_available_mb", "swap_in", "swap_out",
                "history_list_length_max", "threads_connected_avg", "rows_changed_per_s", "dbproc_runq_wait_pct")
# pseudo-variables the agent can apply even when the server does not report them yet: name -> allowed values (None = any)
PSEUDO = {"ps:profile": ("full", "lite"), "rg:innodb_bg_cpus": None}


def _pseudo_available(ctx):
    v, flavor, ver = ctx["variables"], str(ctx.get("flavor") or ""), tuple(ctx.get("version") or ())
    out = {}
    if v.get("performance_schema") in ("ON", "1") and flavor != "mariadb":
        out["ps:profile"] = "performance_schema scope: full (stock instruments) or lite (keeps digests, histograms, wait summaries)"
    if flavor != "mariadb" and ver >= (8, 0):
        out["rg:innodb_bg_cpus"] = "CPU list (e.g. 0-1) for InnoDB background threads via a resource group"
    return out


def trigger(res, explicit=False):
    """Why an LLM call would add value now (None = it would not)."""
    if explicit:
        return "explicit"
    nxt = next((i for i in res["items"] if i["id"] == res.get("next")), None)
    if nxt and nxt["kind"] == "revert":  # the next step only returns to a measured configuration: nothing to ask
        return None
    actionable_safe = [i for i in res["items"] if i["kind"] in ("config", "revert") and i["confidence"] > 0 and i.get("safety") == "safe"]
    if not actionable_safe:
        return "rules-exhausted"
    if all(d["type"] == "none" for d in res.get("diagnosis") or []):
        return "unclear-bottleneck"
    return "routine"


def context(ctx, res, meta=None):
    v = ctx["variables"]
    hw = ctx.get("hw") or {}
    s = ctx.get("summary") or {}
    inp = ctx.get("inputs") or {}
    nondefault = {k for k, m in (meta or {}).items() if m.get("source") not in ("COMPILED", "OS")}
    keep = {k: v[k] for k in v if (k in KB or k in nondefault) and not k.startswith("os:") and k not in DENY}
    facts = {k: v.get(k) for k in ("log_bin", "binlog_format", "gtid_mode", "innodb_flush_log_at_trx_commit", "sync_binlog",
                                     "transaction_isolation", "performance_schema", "ps:profile", "max_connections", "thread_handling",
                                     "innodb_buffer_pool_size", "innodb_redo_log_capacity", "innodb_flush_method", "version_comment") if k in v}
    return {
        "server": {"flavor": ctx.get("flavor"), "version": ".".join(str(x) for x in (ctx.get("version") or ()))},
        # facts that decide whether whole families of knobs can have any effect (e.g. binlog_* when log_bin=OFF)
        "server_facts": facts,
        "tuner_constraints": [
            "performance_schema=OFF is an observability trade-off the tuner already tests itself (rule ps-off); "
            + ("to reduce its overhead safely propose ps:profile=lite. " if "ps:profile" in _pseudo_available(ctx) else "")
            + "Do not propose performance_schema=OFF.",
            "binlog_*, sync_binlog and replication settings have no effect when server_facts.log_bin is OFF.",
            "Each proposal is measured against its parent configuration; differences smaller than noise_pct are treated as noise.",
            "Only variables listed in current_variables, os_settings or available_pseudo_variables can be changed.",
            "Propose only settings whose mechanism is active in this workload (see load): no flushing, redo, commit or purge "
            "settings when writes_active is false; no read-path settings when disk_reads_active is false.",
        ],
        "available_pseudo_variables": dict(_pseudo_available(ctx), **({"os:*": "operating-system settings listed in os_settings"}
                                                                        if ctx.get("os_tuning") else {})),
        "load": {"threads_running_per_cpu": round((s.get("threads_running_avg") or 0) / max(hw.get("cpus") or 1, 1), 2),
                 "history_list_length_max": s.get("history_list_length_max"),
                 # what settings can act on: flushing/redo/commit settings need writes, read-path settings need disk reads
                 "writes_active": _activity(s)["writes"], "disk_reads_active": _activity(s)["reads"],
                 "redo_mbs": s.get("redo_mbs"), "page_reads_from_disk_per_s": s.get("bp_disk_reads")},
        "benchmark": (ctx.get("bench") or {}),
        "hardware": {"cpus": hw.get("cpus"), "ram_gib": round((hw.get("ram_bytes") or 0) / 2 ** 30, 1),
                     "disk": "HDD" if hw.get("disk_rotational") else "SSD", "filesystem": hw.get("filesystem")},
        "goal": ctx.get("goal"), "policy": ctx.get("policy"), "workload": res.get("workload"),
        "diagnosis": [{"type": d["type"], "severity": d["severity"], "text": d["text"]} for d in res.get("diagnosis") or []],
        "summary": {k: s.get(k) for k in SUMMARY_KEYS if s.get(k) is not None},
        "top_waits": (inp.get("top_waits") or [])[:8],
        "top_statements": [{"query": q.get("query", "")[:200], "pct_time": q.get("pct_time"), "avg_ms": q.get("avg_ms"),
                            "rows_examined_avg": q.get("rows_examined_avg")} for q in (inp.get("top_statements") or [])[:5]],
        "current_variables": keep,
        "os_settings": {k: v[k] for k in v if k.startswith("os:")} if ctx.get("os_tuning") else {},
        "history": [{"run": r.get("seq"), "changes": [f"{c['variable']}={c.get('new')}" for c in r.get("changes") or []],
                     "verdict": r.get("verdict"), "goal_delta_pct": (r.get("evaluation") or {}).get("goal_delta_pct"),
                     "drift_on_repeat": bool((r.get("evaluation") or {}).get("drift")),
                     "environment_shift": (r.get("evaluation") or {}).get("environment_shift")}
                    for r in ctx.get("history") or []],
        # per-configuration means across all runs: more reliable than single-run verdicts (repeats expose drift)
        "configurations": _configs(ctx),
        "rule_candidates": [{"id": i["id"], "changes": [f"{c['variable']}={c['proposed']}" for c in i["changes"]], "kind": i["kind"]}
                            for i in res["items"] if i["changes"]],
        "noise_pct": ctx.get("noise_pct"),
    }


def _configs(ctx):
    from .engine import config_scores
    hist = ctx.get("history") or []
    if not hist:
        return []
    start = hist[0].get("managed") or {}
    out = []
    for c in config_scores(hist, ctx.get("goal", "tps")):
        diff = {k: v for k, v in c["managed"].items() if start.get(k) != v}
        diff.update({k: "(default)" for k in start if k not in c["managed"]})
        out.append({"changes_vs_start": diff, "goal_mean": round(c["mean"], 3), "runs": c["seqs"]})
    return out


def sanitize(suggestions, ctx, res, model):
    """Turn LLM suggestions into engine items, or drop them. Returns (items, rejected)."""
    v = ctx["variables"]
    ram = int((ctx.get("hw") or {}).get("ram_bytes") or 0)
    tried = {(c["variable"], normalize(c["variable"], c.get("new"))) for r in ctx.get("history") or [] for c in r.get("changes") or []}
    known = {(c["variable"], normalize(c["variable"], c["proposed"])) for i in res["items"] for c in i["changes"]}
    items, rejected = [], []
    for sg in suggestions or []:
        ch, why = [], None
        for c in sg.get("changes") or []:
            var = c["variable"].strip()
            var = var if var.startswith("os:") else var.lower().replace("-", "_")
            val = str(c["value"]).strip().strip("'\"")
            if var in DENY:
                why = f"{var} is not tunable by the agent"
            elif var not in v and not (var in PSEUDO and var in _pseudo_available(ctx)):
                why = f"{var} does not exist on this server"
            elif var in PSEUDO and PSEUDO[var] and val.lower() not in PSEUDO[var]:
                why = f"{var} accepts {', '.join(PSEUDO[var])}"
            elif var.startswith("os:") and not ctx.get("os_tuning"):
                why = "OS tuning is disabled"
            elif v.get("log_bin") in ("OFF", "0") and (var.startswith(("binlog_", "sync_binlog", "max_binlog")) or var == "log_replica_updates"):
                why = f"{var} has no effect: the binary log is disabled"
            elif normalize(var, v.get(var)) == normalize(var, val):
                why = f"{var} already {val}"
            elif (var, normalize(var, val)) in tried:
                why = f"{var}={val} was already tried in this session"
            elif (var, normalize(var, val)) in known:
                why = f"{var}={val} is already a rule-based candidate"
            else:
                n = parse_size(val) if KB.get(var, {}).get("kind") == "size" or re.fullmatch(r"\d+[KMGkmg]", val) else None
                if n is not None and ram and (n > ram * (0.85 if var == "innodb_buffer_pool_size" else 0.25)):
                    why = f"{var}={val} exceeds the memory budget"
            if why:
                break
            ch.append({"variable": var, "current": v.get(var), "proposed": val})
        if why or not ch:
            rejected.append({"title": sg.get("title"), "reason": why or "no changes"})
            continue
        trade = [t for t in (tradeoff_of(c["variable"], c["proposed"]) for c in ch) if t]
        if not trade and sg.get("risk") in ("durability", "replication", "observability", "semantics"):
            trade = [{"variable": ch[0]["variable"], "value": ch[0]["proposed"], "category": sg["risk"],
                      "lost": "Flagged as a trade-off by the model."}]
        slug = "-".join(re.sub(r"[^a-z0-9]+", "-", c["variable"].lower()).strip("-") for c in ch)[:48]
        sv = [str(x) for x in (sg.get("sweep_values") or []) if str(x).strip()]
        items.append({
            "id": f"llm-{slug}",
            **({"sweep": {"variable": ch[0]["variable"], "values": sv[:6]}} if len(ch) == 1 and len(sv) >= 2 else {}), "title": sg.get("title") or "LLM suggestion", "kind": "config", "changes": ch,
            "rationale": f"{sg.get('rationale', '')} (suggested by {model})", "mechanism": sg.get("mechanism"),
            "evidence": {"model": model, "expected_effect": sg.get("expected_effect")}, "source": "llm",
            "impact": {"large": 2, "moderate": 2, "small": 1}.get(sg.get("expected_effect"), 1), "confidence": 1,
            "risk": trade[0]["category"] if trade else "low", "safety": "trade-off" if trade else "safe", "tradeoffs": trade,
            "restart": any(not KB.get(c["variable"], {}).get("dynamic", True) for c in ch), "goals": ["tps", "latency"],
        })
    return items, rejected
