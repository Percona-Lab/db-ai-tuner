"""Run evaluation: per-transaction efficiency, noise calibration and verdicts.

Protocol
  1. Calibrate: measure the starting configuration several times (default 3). The spread of the goal and of
     each efficiency metric across these repeats becomes the session's noise thresholds.
  2. Every later run is compared with the MEAN of the runs of its parent configuration (the configuration it
     was changed from), not with a single earlier run.
  3. Verdicts
       improved          goal better by more than the goal noise
       regressed         goal worse by more than the goal noise
       efficiency-gain   goal within noise, but CPU or I/O per transaction measurably lower (and nothing measurably worse)
       efficiency-loss   goal within noise, but CPU or I/O per transaction measurably higher
       neutral           nothing measurable changed
       repeat            same configuration as the previous run (adds to that configuration's mean)
       reference         first run of the session
"""
import math
import os

from .knowledge import normalize

# metric -> label; all are "lower is better"
EFFICIENCY = {
    "mysqld_cpu_ms_per_trx": "mysqld CPU ms per transaction",
    "host_cpu_ms_per_trx": "host CPU ms per transaction",
    "disk_ios_per_trx": "disk I/O operations per transaction",
    "disk_kb_per_trx": "disk KiB per transaction",
    "fsyncs_per_trx": "fsyncs per transaction",
    "redo_kb_per_trx": "redo KiB per transaction",
    "rows_read_per_trx": "rows read per transaction",
}
# the ones allowed to promote a within-noise result
KEY_EFFICIENCY = ("mysqld_cpu_ms_per_trx", "host_cpu_ms_per_trx", "disk_ios_per_trx", "disk_kb_per_trx")

DEFAULT_GOAL_NOISE = 2.0
MIN_GOAL_NOISE = 1.0
MIN_EFF_NOISE = 3.0
NOISE_K = 2.5  # threshold = K x coefficient of variation of single runs


def add_efficiency(summary, db_rates=None, cpus=None):
    cpus = cpus or os.cpu_count() or 1
    trx = summary.get("tps") or summary.get("qps")
    if not trx:
        return summary
    summary["efficiency_unit"] = "transaction" if summary.get("tps") else "query"
    if summary.get("mysqld_cpu_pct") is not None:
        summary["mysqld_cpu_ms_per_trx"] = round(summary["mysqld_cpu_pct"] / 100 * cpus * 1000 / trx, 4)
    if summary.get("cpu_busy") is not None:
        summary["host_cpu_ms_per_trx"] = round(summary["cpu_busy"] / 100 * cpus * 1000 / trx, 4)
    ios = (summary.get("disk_r_iops") or 0) + (summary.get("disk_w_iops") or 0)
    summary["disk_ios_per_trx"] = round(ios / trx, 4)
    summary["disk_kb_per_trx"] = round(((summary.get("disk_r_mbs") or 0) + (summary.get("disk_w_mbs") or 0)) * 1024 / trx, 4)
    if summary.get("data_fsyncs") is not None:
        summary["fsyncs_per_trx"] = round(summary["data_fsyncs"] / trx, 4)
    if summary.get("redo_mbs") is not None:
        summary["redo_kb_per_trx"] = round(summary["redo_mbs"] * 1024 / trx, 4)
    if db_rates and db_rates.get("rows_read") is not None:
        summary["rows_read_per_trx"] = round(db_rates["rows_read"] / trx, 3)
    return summary


def config_key(managed):
    return tuple(sorted((k, normalize(k, v)) for k, v in (managed or {}).items()))


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


def _cv(vals):
    if len(vals) < 2:
        return None
    m = _mean(vals)
    if not m:
        return None
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
    return 100.0 * sd / abs(m)


def calibrate(runs):
    """Noise thresholds from repeated runs of one configuration."""
    goal_vals = [r["goal_value"] for r in runs if r.get("goal_value") is not None]
    if len(goal_vals) < 2:
        return None
    cv = _cv(goal_vals)
    m = _mean(goal_vals)
    out = {
        "runs": [r["seq"] for r in runs], "goal_mean": round(m, 4), "goal_cv_pct": round(cv, 2),
        "goal_range_pct": round(100.0 * (max(goal_vals) - min(goal_vals)) / abs(m), 2),
        "noise_pct": round(max(MIN_GOAL_NOISE, NOISE_K * cv), 2), "k": NOISE_K, "efficiency": {},
    }
    for key in EFFICIENCY:
        vals = [r["summary"][key] for r in runs if (r.get("summary") or {}).get(key) is not None]
        c = _cv(vals)
        if c is not None:
            out["efficiency"][key] = {"mean": round(_mean(vals), 4), "cv_pct": round(c, 2), "noise_pct": round(max(MIN_EFF_NOISE, NOISE_K * c), 2)}
    return out


def config_means(runs, key):
    sel = [r for r in runs if config_key(r.get("managed")) == key and r.get("goal_value") is not None]
    if not sel:
        return None
    out = {"n": len(sel), "seqs": [r["seq"] for r in sel], "goal": _mean([r["goal_value"] for r in sel])}
    for k in EFFICIENCY:
        vals = [r["summary"][k] for r in sel if (r.get("summary") or {}).get(k) is not None]
        if vals:
            out[k] = _mean(vals)
    return out


def improvement(goal, new, old):
    """Percent change where positive = better."""
    if new is None or old in (None, 0):
        return None
    return round(100.0 * ((old - new) if goal == "latency" else (new - old)) / abs(old), 2)


CPU_METRICS = ("mysqld_cpu_ms_per_trx", "host_cpu_ms_per_trx")
IO_METRICS = ("disk_ios_per_trx", "disk_kb_per_trx")


def relevant_resources(*summaries):
    """Efficiency only counts for resources that are actually under load in the compared runs."""
    cpu = io = False
    for s in summaries:
        s = s or {}
        if (s.get("cpu_busy") or 0) >= 50 or (s.get("psi_cpu_some") or 0) >= 10:
            cpu = True
        psi_io = s.get("mysqld_psi_io_some") if s.get("mysqld_psi_io_some") is not None else s.get("psi_io_some")
        if (psi_io or 0) >= 10 or (s.get("disk_util") or 0) >= 60 or (s.get("cpu_iowait") or 0) >= 10:
            io = True
    return {"cpu": cpu, "io": io}


def verdict(run, history, goal, noise):
    """run: the new run dict (managed, goal_value, summary); history: earlier runs of the session.
    noise: session noise dict (calibration) or None."""
    if not history:
        return "reference", {}
    key = config_key(run.get("managed"))
    prev = history[-1]
    if config_key(prev.get("managed")) == key:
        return "repeat", {"note": "same configuration as the previous run; averaged into its mean"}
    parent = None
    for r in reversed(history):  # the configuration this run was derived from = last different config measured
        if config_key(r.get("managed")) != key:
            parent = config_key(r.get("managed"))
            break
    pm = config_means(history, parent)
    if not pm:
        return "neutral", {}
    goal_noise = (noise or {}).get("noise_pct") or DEFAULT_GOAL_NOISE
    d_goal = improvement(goal, run.get("goal_value"), pm["goal"])
    details = {"parent_runs": pm["seqs"], "parent_goal_mean": round(pm["goal"], 4), "goal_delta_pct": d_goal,
               "goal_noise_pct": goal_noise, "calibrated": bool(noise and noise.get("runs")), "efficiency": {}}
    s = run.get("summary") or {}
    parent_runs = [r for r in history if config_key(r.get("managed")) == parent]
    rel = relevant_resources(s, *[r.get("summary") for r in parent_runs])
    details["relevant"] = rel
    better, worse = [], []
    for k, label in EFFICIENCY.items():
        if s.get(k) is None or not pm.get(k):
            continue
        ch = round(100.0 * (pm[k] - s[k]) / pm[k], 2)  # positive = less work per transaction
        thr = ((noise or {}).get("efficiency", {}).get(k) or {}).get("noise_pct") or max(MIN_EFF_NOISE, 2 * goal_noise)
        details["efficiency"][k] = {"label": label, "parent": round(pm[k], 4), "now": s[k], "improvement_pct": ch, "noise_pct": thr}
        relevant = (k in CPU_METRICS and rel["cpu"]) or (k in IO_METRICS and rel["io"])
        details["efficiency"][k]["counts"] = relevant
        if k in KEY_EFFICIENCY and relevant:
            if ch > thr:
                better.append(label)
            elif ch < -thr:
                worse.append(label)
    details["efficiency_better"], details["efficiency_worse"] = better, worse
    if d_goal is None:
        return "neutral", details
    if d_goal > goal_noise:
        return "improved", details
    if d_goal < -goal_noise:
        return "regressed", details
    if better and not worse:
        return "efficiency-gain", details
    if worse and not better:
        return "efficiency-loss", details
    return "neutral", details


KEEP_VERDICTS = ("improved", "efficiency-gain", "reference")
