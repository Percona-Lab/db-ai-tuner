"""Run evaluation: per-transaction efficiency, noise calibration and verdicts.

Protocol
  1. Calibrate: measure the starting configuration several times (default 3). The spread of the goal and of
     each efficiency metric across these repeats becomes the session's noise thresholds.
  2. Every later run is compared with the MEAN of the runs of its parent configuration (the configuration it
     was changed from), not with a single earlier run.
  3. Verdicts
       improved          goal better by more than the goal noise
       regressed         goal worse by more than the goal noise
       efficiency-gain   goal within noise, but CPU or I/O per transaction or memory measurably lower (and nothing measurably worse)
       efficiency-loss   goal within noise, but CPU or I/O per transaction or memory measurably higher
       neutral           nothing measurable changed
       repeat            same configuration as the previous run (adds to that configuration's mean)
       reference         first run of the session
"""
import math
import re
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
    "db_mem_mb": "database memory (PSS incl. swapped, MiB)",
}
# the ones allowed to promote a within-noise result
# tie-breakers when the goal is within noise: using less CPU, I/O or memory for the same result is better
KEY_EFFICIENCY = ("mysqld_cpu_ms_per_trx", "host_cpu_ms_per_trx", "disk_ios_per_trx", "disk_kb_per_trx", "db_mem_mb")

DEFAULT_GOAL_NOISE = 2.0
MIN_GOAL_NOISE = 2.0  # used only until the session (or this environment's history) has measured long-term variance
RESOLUTION_NOISE = 0.5  # below this a difference is not worth acting on, however stable the environment
MIN_EFF_NOISE = 3.0
NOISE_K = 2.5  # threshold = K x coefficient of variation of single runs
EXCLUDED = ("unreliable", "superseded")  # never compared, averaged, kept or used for calibration
NOISE_TARGET = 3.0  # noise threshold (%) above which typical tuning gains (2-8% on OLTP) are hard to see


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


PROBE_KEYS = ("probe_cpu_events_s", "probe_read_iops", "probe_write_iops", "probe_fsync_per_s")
PROBE_SHIFT_PCT = 15.0


def environment_shift(run_summary, noise):
    """Compare a run's environment probe (quick CPU / storage benchmark before the run) with the calibration runs.
    A probe counts as shifted only beyond its own run-to-run variation during calibration (3 x CV, at least 15%):
    e.g. fsync rates on shared storage vary by 10% between identical runs."""
    ref = (noise or {}).get("probe") or {}
    cvs = (noise or {}).get("probe_cv") or {}
    out = {}
    for k in PROBE_KEYS:
        a, b = ref.get(k), (run_summary or {}).get(k)
        if a and b:
            d = round(100.0 * (b - a) / a, 1)
            if abs(d) > max(PROBE_SHIFT_PCT, 3 * (cvs.get(k) or 0)):
                out[k[6:]] = d
    return out


def noise_estimate(runs, prior_cv=None):
    """Goal noise of THIS environment + workload, re-estimated from every configuration measured more than once.

    Back-to-back repeats (calibration) only show short-term noise; repeats separated by other runs (re-checks,
    confirmations) also include drift. Pooled over all such groups:
      * with time-separated repeats: threshold = K x pooled CV (at least RESOLUTION_NOISE);
      * otherwise: K x max(calibration CV, prior long-term CV of earlier sessions on this environment + benchmark),
        and MIN_GOAL_NOISE when there is no history yet.
    """
    groups = {}
    for r in runs:
        if r.get("goal_value") is None or r.get("verdict") in EXCLUDED:
            continue
        groups.setdefault(config_key(r.get("managed")), []).append(r)
    stats = []  # (cv of the group, its degrees of freedom, separated in time, seqs)
    for g in groups.values():
        if len(g) < 2:
            continue
        vals = [r["goal_value"] for r in g]
        m = _mean(vals)
        if not m:
            continue
        seqs = sorted(r["seq"] for r in g)
        cvg = (sum(((v - m) / m * 100) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
        stats.append((cvg, len(vals) - 1, seqs[-1] - seqs[0] >= len(seqs), seqs))
    if not stats:
        return None
    # one wild pair must not set the threshold for the whole session (Battery 2 O1: a 14% repeat gap took the
    # threshold from 3.7% to 12.6%): with 3+ groups, each group's spread is capped at 3x the median group spread
    outliers = []
    if len(stats) >= 3:
        med = sorted(x[0] for x in stats)[len(stats) // 2]
        cap = max(3 * med, RESOLUTION_NOISE / NOISE_K)
        outliers = [x[3] for x in stats if x[0] > cap]
        stats = [(min(x[0], cap), x[1], x[2], x[3]) for x in stats]
    ss = sum(c * c * d for c, d, _, _ in stats)
    dof = sum(d for _, d, _, _ in stats)
    long_term = sum(d for _, d, sep, _ in stats if sep)
    cv = (ss / dof) ** 0.5
    if long_term:
        pct, basis = max(RESOLUTION_NOISE, NOISE_K * cv), f"{int(dof)} repeat(s), {long_term} separated in time"
    elif prior_cv is not None:
        pct, basis = max(RESOLUTION_NOISE, NOISE_K * max(cv, prior_cv)), "back-to-back repeats + earlier sessions on this environment"
    else:
        pct, basis = max(MIN_GOAL_NOISE, NOISE_K * cv), "back-to-back repeats only (no long-term evidence yet)"
    return {"noise_pct": round(pct, 2), "pooled_cv_pct": round(cv, 3), "dof": int(dof), "long_term_dof": long_term,
            "prior_cv_pct": prior_cv, "basis": basis, **({"outlier_groups": outliers} if outliers else {})}


def noise_plan(runs, cal, duration, max_duration=None, target=NOISE_TARGET):
    """Split the calibrated run-to-run noise into a within-run part (what each run's own 10 s samples predict for its
    length: batch-means standard error) and a between-run part (shifts from run to run: data state, restarts,
    neighbours), and pick the remedy that actually reduces it:

    * within-run dominated -> longer runs (the within part shrinks with sqrt(length));
    * between-run dominated -> longer runs do not help: more repeats per configuration / a data reset per run;
    * otherwise fine.
    """
    cv = cal.get("goal_cv_pct")
    ses = [(r.get("summary") or {}).get("within_se_pct") for r in runs]
    ses = [x for x in ses if x]
    out = {"target_pct": target, "noise_pct": cal.get("noise_pct"), "run_seconds": duration}
    if cv is None or not ses:
        return dict(out, action="unknown", text="No per-run sample data to separate within-run and between-run noise.")
    within = (sum(x * x for x in ses) / len(ses)) ** 0.5
    between = max(0.0, cv * cv - within * within) ** 0.5
    out.update(within_cv_pct=round(within, 2), between_cv_pct=round(between, 2))
    if (cal.get("noise_pct") or 0) <= target:
        return dict(out, action="ok", text=f"Noise ±{cal.get('noise_pct')}% is within the ±{target}% target.")
    cv_t = target / NOISE_K
    if between >= 0.8 * cv_t or within <= between:
        reps = max(2, math.ceil((NOISE_K * between / target) ** 2)) if between else 2
        return dict(out, action="between-runs", repeats=min(reps, 9),
                    text=f"Mostly between-run noise (between {between:.2f}% vs within {within:.2f}% CV): longer runs would not help. "
                         f"Changes smaller than ~{cal.get('noise_pct')}% cannot be seen; about {min(reps, 9)} runs per configuration would be "
                         "needed for the target, or remove the source of run-to-run shifts (reset the data set before each run, "
                         "avoid restarts between runs, a quieter host).")
    need_se = (cv_t * cv_t - between * between) ** 0.5
    new = int(math.ceil(duration * (within / need_se) ** 2 / 60.0) * 60) if duration else None
    cap = max_duration or (min(4 * duration, 1800) if duration else None)
    if not new or not cap:
        return dict(out, action="unknown", text="Run length cannot be changed for this benchmark (runs until it finishes).")
    capped = new > cap
    sec = min(new, cap)
    exp_cv = (between ** 2 + within ** 2 * duration / sec) ** 0.5
    return dict(out, action="longer-runs", seconds=sec, capped=capped, expected_noise_pct=round(max(RESOLUTION_NOISE, NOISE_K * exp_cv), 2),
                text=f"Mostly within-run noise (within {within:.2f}% vs between {between:.2f}% CV): {sec // 60} min runs instead of "
                     f"{duration // 60} min should bring the threshold from ±{cal.get('noise_pct')}% to about ±{max(RESOLUTION_NOISE, NOISE_K * exp_cv):.1f}%"
                     + (" (capped by max_duration)" if capped else "") + ".")


def trend_pct(values, xs=None):
    """Least-squares slope as % of the mean per unit of x (default: per run)."""
    n = len(values)
    if n < 3:
        return None
    xs = xs or list(range(n))
    mx, my = sum(xs) / n, sum(values) / n
    den = sum((x - mx) ** 2 for x in xs)
    if not den or not my:
        return None
    return 100.0 * sum((x - mx) * (y - my) for x, y in zip(xs, values)) / den / my


def data_state_check(runs, data_state):
    """Writing benchmark without a data reset or seasoning: every run changes the data the next one starts from, so
    differences between runs can come from the data, not the configuration."""
    w = [(r.get("summary") or {}).get("rows_changed_per_s") for r in runs]
    w = [x for x in w if x is not None]
    writes = bool(w) and sum(w) / len(w) > 1
    vals = [r["goal_value"] for r in runs if r.get("goal_value") is not None]
    t = trend_pct(vals)
    out = {"data_state": data_state, "writes": writes, "rows_changed_per_s": round(sum(w) / len(w), 1) if w else None,
           "trend_pct_per_run": round(t, 2) if t is not None else None}
    if writes and data_state not in ("reset", "seasoned"):
        out["status"] = "drifting"
        out["text"] = ("The benchmark changes data and the profile neither restores it before each run (reset) nor seasons it to a "
                       "steady state first: each run starts from the data the previous one left, so results can move with the data "
                       "rather than the configuration" + (f" (calibration trend {t:+.1f}% per run)" if t is not None else "") + ".")
    else:
        out["status"] = "ok"
    return out


def calibrate(runs):
    """Noise thresholds from repeated runs of one configuration."""
    runs = [r for r in runs if r.get("verdict") not in EXCLUDED] or runs
    goal_vals = [r["goal_value"] for r in runs if r.get("goal_value") is not None]
    if len(goal_vals) < 2:
        return None
    cv = _cv(goal_vals)
    m = _mean(goal_vals)
    out = {
        "runs": [r["seq"] for r in runs], "goal_mean": round(m, 4), "goal_cv_pct": round(cv, 2),
        "goal_range_pct": round(100.0 * (max(goal_vals) - min(goal_vals)) / abs(m), 2),
        "noise_pct": round(max(MIN_GOAL_NOISE, NOISE_K * cv), 2), "k": NOISE_K, "efficiency": {},
        "_goal_values": goal_vals,
    }
    probe = {}
    probe_cv = {}
    for key in PROBE_KEYS:
        vals = [r["summary"][key] for r in runs if (r.get("summary") or {}).get(key)]
        if vals:
            probe[key] = round(_mean(vals), 2)
            if len(vals) >= 2:
                probe_cv[key] = round(_cv(vals), 2)
    out["probe"] = probe
    out["probe_cv"] = probe_cv
    for key in EFFICIENCY:
        vals = [r["summary"][key] for r in runs if (r.get("summary") or {}).get(key) is not None]
        c = _cv(vals)
        if c is not None:
            out["efficiency"][key] = {"mean": round(_mean(vals), 4), "cv_pct": round(c, 2), "noise_pct": round(max(MIN_EFF_NOISE, NOISE_K * c), 2)}
    return out


RECENT = 3  # parent configuration = mean of its most recent runs (protects against slow drift)


def config_means(runs, key, recent=None):
    sel = [r for r in runs if config_key(r.get("managed")) == key and r.get("goal_value") is not None
           and r.get("verdict") not in EXCLUDED]
    if recent:
        sel = sel[-recent:]
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
MEM_METRICS = ("db_mem_mb",)


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


STEAL_FLAG_PEAK = 5.0  # any 10 s sample; measured normal across our hosts: mean < 0.05%, p99 < 1.5%, max < 2%


def steal_check(summary, noise_pct=None):
    """Hypervisor CPU steal on the database host or a client during the run. Steal takes CPU away from a CPU-bound
    workload roughly one for one, so its possible effect on the result is about the average steal, scaled down when the
    host was not CPU-bound. That effect must stay well inside the noise threshold, otherwise a verdict may reflect the
    neighbours rather than the configuration: flag from a quarter of the threshold (at least 0.25%), or a 5% peak in any
    10 s sample; unreliable (excluded and re-measured) from half of it (at least 0.5%). Normal on our hosts: < 0.05%."""
    s = summary or {}
    avg, peak, cli = s.get("cpu_steal") or 0, s.get("cpu_steal_max") or 0, s.get("client_cpu_steal") or 0
    noise = float(noise_pct or DEFAULT_GOAL_NOISE)
    cpu_factor = min(1.0, max(0.2, (s.get("cpu_busy") or 100) / 70.0))
    impact = max(avg, cli) * cpu_factor
    flag_at, invalid_at = max(0.25, noise / 4), max(0.5, noise / 2)
    if impact >= invalid_at:
        level = "invalid"
    elif impact >= flag_at or peak >= STEAL_FLAG_PEAK:
        level = "flag"
    else:
        return None
    return {"level": level, "avg_pct": round(avg, 2), "peak_pct": round(peak, 2), "client_avg_pct": round(cli, 2) if cli else None,
            "est_impact_pct": round(impact, 2), "flag_at_pct": round(flag_at, 2), "invalid_at_pct": round(invalid_at, 2)}


def verdict(run, history, goal, noise):
    if run.get("superseded"):
        return "superseded", {"note": run["superseded"]}
    st = steal_check(run.get("summary"), (noise or {}).get("noise_pct"))
    if st and st["level"] == "invalid":
        return "unreliable", {"steal": st, "note": f"UNRELIABLE: hypervisor CPU steal averaged {max(st['avg_pct'], st['client_avg_pct'] or 0):.1f}% "
                                                   "during the run (noisy neighbours on a shared host). The result is kept for reference "
                                                   "but excluded from comparisons."}
    v, d = _verdict(run, history, goal, noise)
    if st:
        d = dict(d)
        d["steal"] = st
    # only meaningful for a run compared with another configuration (not the first run, not calibration repeats)
    shift = environment_shift(run.get("summary"), noise) if v in ("improved", "regressed", "neutral", "efficiency-gain", "efficiency-loss") else None
    if shift:
        d = dict(d)
        d["environment_shift"] = shift
    return v, d


def mean_noise(noise, n1, n2):
    """Noise threshold for comparing two means of n1 and n2 runs. `noise` is calibrated for one run vs one run; the
    spread of a difference of means shrinks with sqrt(1/n1 + 1/n2), so 3 runs vs 2 runs use ~65% of it."""
    return noise * math.sqrt((1.0 / max(n1, 1) + 1.0 / max(n2, 1)) / 2.0)


def confirm_pass(runs, goal, noise):
    """Re-judge improvements once their configuration has been measured again: the mean of all its runs must still beat
    the parent mean beyond the (run-count scaled) threshold, else the improvement was a lucky run and becomes neutral.
    Updates runs in place; returns [(seq, old_verdict, new_verdict)]."""
    noise_pct = float((noise or {}).get("noise_pct") or DEFAULT_GOAL_NOISE)
    changed = []
    for r in runs:
        ev = r.get("evaluation") or {}
        if r.get("verdict") not in ("improved",) and not (r.get("verdict") == "neutral" and ev.get("confirmed") is False):
            continue
        key = config_key(r.get("managed"))
        mine = [x for x in runs if config_key(x.get("managed")) == key and x.get("goal_value") is not None and x.get("verdict") not in EXCLUDED]
        par = [x for x in runs if x["seq"] in (ev.get("parent_runs") or []) and x.get("goal_value") is not None]
        if len(mine) < 2 or not par:
            continue
        m = _mean([x["goal_value"] for x in mine])
        pm = _mean([x["goal_value"] for x in par])
        d = improvement(goal, m, pm)
        thr = max(RESOLUTION_NOISE, mean_noise(noise_pct, len(mine), len(par)))
        ok = d is not None and d > thr
        new = "improved" if ok else "neutral"
        ev = dict(ev, confirmed=ok, confirmed_runs=[x["seq"] for x in mine], confirmed_delta_pct=d, confirmed_threshold_pct=round(thr, 2))
        if not ok:
            ev["note"] = (f"Not confirmed: the mean of runs {ev['confirmed_runs']} is {d:+.2f}% vs the parent, within the ±{thr:.2f}% "
                          "threshold for these run counts (the first run was lucky).")
        if new != r.get("verdict") or ev != r.get("evaluation"):
            changed.append((r["seq"], r.get("verdict"), new))
            r["verdict"], r["evaluation"] = new, ev
    return changed


def _is_revert(change, run):
    """A change that returns a setting to an earlier configuration rather than being part of this run's experiment:
    explicit rollbacks, reverts merged into the next experiment, and a sweep's restore applied with a later run."""
    src = str(change.get("source") or "")
    if src == "rollback" or re.match(r"rec:(rollback|revert)-to-run", src):
        return True
    if src.startswith("exp:"):  # a sweep's own points carry its label (exp#N ...); anything else is its restore
        return not str(run.get("label") or "").startswith("exp#" + src[4:] + " ")
    return False


def _parent_key(run, history, key):
    """The configuration this run was derived from: the measured configuration that equals this run in every setting
    except the run's own experiment changes (reverts merged into the run are not part of the experiment). Own settings
    prefer their previous values; for sweep points the value before the sweep started (the sweep base), so every point
    is compared with the base rather than with the previous point."""
    own_ch = [c for c in run.get("changes") or [] if not _is_revert(c, run)]
    own = {c["variable"] for c in own_ch}
    if own:
        cur = run.get("managed") or {}
        rest = lambda m: {k: normalize(k, v) for k, v in (m or {}).items() if k not in own}
        want = rest(cur)
        pref = {}
        for c in own_ch:
            base = c.get("old")
            src = str(c.get("source") or "")
            if src.startswith("exp:"):
                first = next((c2 for r in history for c2 in (r.get("changes") or [])
                              if c2.get("source") == src and c2.get("variable") == c["variable"]), None)
                if first is not None:
                    base = first.get("old")
            pref[c["variable"]] = base
        cands = [r for r in history if r.get("goal_value") is not None and r.get("verdict") not in EXCLUDED
                 and config_key(r.get("managed")) != key and rest(r.get("managed")) == want]
        if cands:
            def at_pref(r):
                m = r.get("managed") or {}
                return all(m.get(v) is None or normalize(v, m.get(v)) == normalize(v, pv) for v, pv in pref.items())
            exact = [r for r in cands if at_pref(r)]
            return config_key((exact or cands)[-1].get("managed"))
    for r in reversed(history):  # fallback: last different configuration measured
        if config_key(r.get("managed")) != key:
            return config_key(r.get("managed"))
    return None


def _verdict(run, history, goal, noise):
    """run: the new run dict (managed, goal_value, summary); history: earlier runs of the session.
    noise: session noise dict (calibration) or None."""
    if not history:
        return "reference", {}
    key = config_key(run.get("managed"))
    same = config_means(history, key, recent=RECENT)
    if same:  # this configuration was measured before: a repeat, and a drift check
        goal_noise = (noise or {}).get("noise_pct") or DEFAULT_GOAL_NOISE
        dev = improvement(goal, run.get("goal_value"), same["goal"])
        d = {"note": "configuration measured before; averaged into its mean", "same_config_runs": same["seqs"],
             "same_config_mean": round(same["goal"], 4), "deviation_pct": dev, "goal_noise_pct": goal_noise}
        if dev is not None and abs(dev) > goal_noise and (noise or {}).get("runs"):  # only once noise is calibrated
            d["drift"] = True
            d["note"] = (f"DRIFT: this configuration measured {dev:+.1f}% versus its earlier mean (runs {same['seqs']}), beyond the "
                         f"±{goal_noise}% noise. The environment changed (data growth, noisy neighbour, caches?) — "
                         "re-calibrate or reset the data set before trusting small differences.")
        return "repeat", d
    parent = _parent_key(run, history, key)
    pm = config_means(history, parent, recent=RECENT)
    if not pm:
        return "neutral", {}
    goal_noise = (noise or {}).get("noise_pct") or DEFAULT_GOAL_NOISE
    d_goal = improvement(goal, run.get("goal_value"), pm["goal"])
    last_parent = max(pm["seqs"])
    details = {"parent_runs": pm["seqs"], "parent_goal_mean": round(pm["goal"], 4), "goal_delta_pct": d_goal,
               "runs_since_parent_measured": len([r for r in history if r["seq"] > last_parent]),
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
        # every resource counts as a tie-breaker (less is better even when it is not the bottleneck); `relevant` is
        # kept for the explanation only. Thresholds stay noise-based, so only meaningful differences count.
        relevant = (k in CPU_METRICS and rel["cpu"]) or (k in IO_METRICS and rel["io"]) or k in MEM_METRICS
        details["efficiency"][k]["counts"] = k in KEY_EFFICIENCY
        details["efficiency"][k]["under_load"] = relevant
        if k in KEY_EFFICIENCY:
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


KEEP_VERDICTS = ("improved", "efficiency-gain", "reference")  # EXCLUDED runs are never kept or compared


# ---- per-query impact (query-level view of an experiment, after CMU's Booster) -------------
QUERY_NOISE_PCT = 10.0  # single statements are noisier than the whole workload


def query_impact(stmts_now, stmts_parent, top=12):
    """Compare per-statement average latency of this run with the parent configuration's run.
    stmts_*: inputs["top_statements"] lists. Returns rows sorted by time share plus a winners/losers summary."""
    if not stmts_now or not stmts_parent:
        return None
    prev = {(s.get("schema"), s.get("digest")): s for s in stmts_parent}
    rows = []
    for q in stmts_now[:top]:
        p = prev.get((q.get("schema"), q.get("digest")))
        if not p or not p.get("avg_ms") or q.get("avg_ms") is None:
            continue
        ch = round(100.0 * (p["avg_ms"] - q["avg_ms"]) / p["avg_ms"], 1)  # positive = faster
        rows.append({"digest": q.get("digest"), "query": (q.get("query") or "")[:160], "pct_time": q.get("pct_time"),
                     "avg_ms_parent": p["avg_ms"], "avg_ms": q["avg_ms"], "faster_pct": ch})
    if not rows:
        return None
    winners = [r for r in rows if r["faster_pct"] > QUERY_NOISE_PCT]
    losers = [r for r in rows if r["faster_pct"] < -QUERY_NOISE_PCT]
    return {"rows": rows, "winners": len(winners), "losers": len(losers), "noise_pct": QUERY_NOISE_PCT,
            "mixed": bool(winners and losers)}
