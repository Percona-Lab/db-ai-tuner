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
# an efficiency metric decides a verdict only when the resource is under load and its per-unit value is material: on an
# in-memory read-only load 0.0014 -> 0.0008 KiB of disk per transaction is "-40%" of nothing (r1 RO-1: 6 such "gains")
MATERIAL = {"disk_ios_per_trx": 0.05, "disk_kb_per_trx": 1.0, "db_mem_mb": 256.0}
DEFERRABLE = ("disk_ios_per_trx", "disk_kb_per_trx")  # I/O a window can push past its end (flushing, purge)
NOISE_K = 2.5  # threshold = K x coefficient of variation of single runs
CONFIRM_K = 1.65  # confirmation of a screened change on its repeats alone: one-sided ~95%
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


def noise_estimate(runs, prior_cv=None, prior_dof=4):
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
        g = sorted(g, key=lambda r: r["seq"])
        if not str(g[0].get("label") or "").startswith(("baseline", "calibration")) and len(g) > 1 and "label" in g[0]:
            # a configuration measured again because its first run looked good (confirmation, re-check of a kept change):
            # that run was selected, so its spread from the repeats is winner's curse, not noise (canaries: confirm runs
            # -2.8% vs their first run on average, baselines -0.8%) - only the unselected repeats count
            g = g[1:]
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
        # the prior and this session's repeats pooled by their degrees of freedom: max() only ever went up (canaries: CV 0.47%
        # in the session, threshold 6% from the prior alone)
        pooled = ((cv * cv * dof + prior_cv * prior_cv * prior_dof) / (dof + prior_dof)) ** 0.5
        pct, basis = max(RESOLUTION_NOISE, NOISE_K * pooled), "back-to-back repeats pooled with earlier sessions on this environment"
    else:
        pct, basis = max(MIN_GOAL_NOISE, NOISE_K * cv), "back-to-back repeats only (no long-term evidence yet)"
    return {"noise_pct": round(pct, 2), "pooled_cv_pct": round(cv, 3), "dof": int(dof), "long_term_dof": long_term,
            "prior_cv_pct": prior_cv, "basis": basis, **({"outlier_groups": outliers} if outliers else {})}


DEBT_KEYS = ("checkpoint_age_pct", "bp_dirty_pct", "history_list_length", "ibuf_size")


def _debt_growth(summary):
    """Relative growth of deferred work from the middle to the last third of a window (steady_state evidence)."""
    ev = ((summary or {}).get("steady_state") or {}).get("evidence") or {}
    g = 0.0
    for k in DEBT_KEYS:
        a, b = (ev.get(k) or [None, None])[:2]
        if a is not None and b is not None and b > a:
            g += (b - a) / max(abs(a), 1e-9)
    return g


def _unsteady_bias(run, history, d, v, goal):
    """Why a not-steady window cannot explain this verdict (None when it can): deferred work piling up flatters the run
    that defers, so it cannot produce a regression; a window still warming up understates the run, so it cannot produce
    an improvement; and a candidate that accumulates no more debt than its parent did is compared on equal terms (lab
    205 redo-grow +19.9%, 221 thread pool -72.7% had been turned neutral)."""
    reasons = d.get("not_steady") or []
    rising_only = all(r.startswith("throughput +") for r in reasons)
    debt = any(not r.startswith("throughput") for r in reasons)
    if goal == "latency":
        return None  # direction of the bias on latency goals is not modelled
    if v == "regressed" and not rising_only:
        return "deferred work flatters the run that defers it, so it cannot explain a regression - the verdict stands."
    if v == "improved" and rising_only:
        return "throughput was still rising, which understates this run - the improvement stands."
    if v == "improved" and debt:
        par = [h for h in history if h.get("seq") in (d.get("parent_runs") or [])]
        pg = [_debt_growth(h.get("summary")) for h in par if (h.get("summary") or {}).get("steady_state")]
        if pg and _debt_growth(run.get("summary")) <= 1.25 * (sum(pg) / len(pg)) + 0.02:
            return "the parent configuration accumulated as much deferred work in its windows, so both sides are compared alike."
    return None


# the largest bias unpaid deferred work (flushing, purge, change-buffer merges) is credited with when a window is not at
# steady state: differences beyond max(4 x noise, this) keep their verdict
NOT_STEADY_MAX_BIAS_PCT = 25.0


def warmup_check(runs, warmup):
    """Calibration runs of the starting configuration: is the warm-up long enough to reach steady state?"""
    sts = [(r.get("summary") or {}).get("steady_state") for r in runs]
    sts = [x for x in sts if x]
    if not sts:
        return None
    bad = [x for x in sts if not x.get("steady")]
    out = {"steady_runs": len(sts) - len(bad), "runs": len(sts), "warmup_s": warmup}
    unb = [u for r in runs for u in ((r.get("summary") or {}).get("unbounded_growth") or [])]
    unb_labels = {u["label"] for u in unb}
    only_unbounded = bool(bad) and all(any(g.startswith(lb) for lb in unb_labels) for x in bad for g in x.get("growing") or [])
    if len(bad) * 2 > len(sts) and only_unbounded:
        # growing at an undiminished rate through the warm-up and the window: a longer warm-up only measures a larger
        # backlog (session 226: history list +1,000/s with 1 purge thread at ~850 running threads, recalibrated to 1200 s)
        u = max(unb, key=lambda x: x["rate_per_s"])
        out.update(adequate=True, unbounded=unb[:3], text=(
            f"Not a warm-up problem: {u['label']} grows without slowing down ({u['start']:,} -> {u['end']:,} in the window, "
            f"~{u['per_hour']:,} per hour) in {len(bad)} of {len(sts)} calibration runs. The starting configuration cannot keep up "
            "with this load and would fail if left running; a longer warm-up only measures a larger backlog. "
            "Fixing it (purge threads / purge lag) comes first."))
        return out
    if len(bad) * 2 > len(sts):  # most calibration runs still accumulate deferred work
        grow = sorted({g.split(" ")[0] + " " + g.split(" ")[1] for x in bad for g in x.get("growing") or []})
        out.update(adequate=False, growing=[g for x in bad[:1] for g in x.get("growing") or []],
                   text=f"Warm-up of {warmup} s is not long enough: at the end of the measured window deferred work was still "
                        f"accumulating in {len(bad)} of {len(sts)} calibration runs ({'; '.join(bad[0].get('growing') or [])}). "
                        "Results would favour settings that postpone flushing, purge or change-buffer merges.")
    else:
        out.update(adequate=True, text=f"Steady state reached in {len(sts) - len(bad)} of {len(sts)} calibration runs.")
    # state that settled only inside the window (checkpoint age, buffer pool filling, ...): the end is steady, but the
    # window's first part measured another regime. A warning with a suggested warm-up; the runs stay valid.
    sig = [w for w in ((r.get("summary") or {}).get("warmup_signals") for r in runs) if w]
    if sig and len(sig) * 2 > len(runs) and out.get("adequate"):
        sug = max((w.get("suggest_warmup_s") or 0) for w in sig) or None
        labels = sorted({x["label"] for w in sig for x in w["settling"]})
        out.update(settling_runs=len(sig), suggest_warmup_s=sug,
                   text=f"Steady by the end of the window, but in {len(sig)} of {len(runs)} calibration runs {', '.join(labels)} settled only "
                        f"inside the measured window ({sig[0]['text'].split(': ', 1)[-1]})."
                        + (f" Consider a warm-up of about {sug} s (now {warmup} s)." if sug and warmup else ""))
    return out


SEASON_SKIP_H = 0.25      # ramp-up of the seasoning run: not part of the trend
SEASON_MIN_POINTS = 8     # samples in the window before any stop decision
SEASON_MAX_TOL = 5.0      # %/h: never call a data set steady that drifts faster than this


def _fit(pts):
    """Least-squares line through [(h, v)]: (slope per h, mean, residual sd, standard error of the slope)."""
    n = len(pts)
    mh, mv = sum(h for h, _ in pts) / n, sum(v for _, v in pts) / n
    sxx = sum((h - mh) ** 2 for h, _ in pts)
    if not sxx or not mv or n < 3:
        return None
    b = sum((h - mh) * (v - mv) for h, v in pts) / sxx
    sd = (sum((v - (mv + b * (h - mh))) ** 2 for h, v in pts) / (n - 2)) ** 0.5
    return b, mv, sd, sd / sxx ** 0.5


def _quad(pts):
    """Least-squares parabola through [(h, v)]: (a, b, c, standard error of c, residual sd) or None."""
    n = len(pts)
    if n < 12:
        return None
    S = [sum(h ** k for h, _ in pts) for k in range(5)]
    T = [sum(v * h ** k for h, v in pts) for k in range(3)]
    M = [[S[0], S[1], S[2]], [S[1], S[2], S[3]], [S[2], S[3], S[4]]]
    det = lambda m: (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                     + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    D = det(M)
    if not D:
        return None
    col = lambda k: [[T[r] if cc == k else M[r][cc] for cc in range(3)] for r in range(3)]
    a, b, c = (det(col(k)) / D for k in range(3))
    sd = (sum((v - (a + b * h + c * h * h)) ** 2 for h, v in pts) / (n - 3)) ** 0.5
    inv22 = (M[0][0] * M[1][1] - M[0][1] * M[1][0]) / D  # (M^-1)[2][2]
    return a, b, c, sd * max(inv22, 0) ** 0.5, sd


def _expfit(pts, end):
    """Fit v = A + B exp(-t / tau) over a grid of time constants (linear least squares in A, B for each). Returns
    (best (sse, tau, A, B), upper bound of the change still to come as % of the current level, from the time constants
    the data cannot rule out: SSE within the 95% profile bound) or None."""
    n = len(pts)
    if n < 12:
        return None
    fits = []
    for k in range(48):
        tau = 0.1 * 1.15 ** k  # 0.1 .. ~70 h
        xs = [2.718281828 ** (-h / tau) for h, _ in pts]
        mx, mv = sum(xs) / n, sum(v for _, v in pts) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx < 1e-12:
            continue
        B = sum((x - mx) * (v - mv) for x, (_, v) in zip(xs, pts)) / sxx
        A = mv - B * mx
        sse = sum((v - A - B * x) ** 2 for x, (_, v) in zip(xs, pts))
        fits.append((sse, tau, A, B))
    if not fits:
        return None
    best = min(fits)
    ok = [f for f in fits if f[0] <= best[0] * (1 + 3.84 / max(n - 3, 1))]
    lvl = best[2] + best[3] * 2.718281828 ** (-end / best[1])
    if not lvl:
        return None
    rest = max(abs(f[3] * 2.718281828 ** (-end / f[1])) for f in ok) / abs(lvl) * 100
    return best, rest


def season_check(pts, span_h, run_h=None, floor_pct_h=0.5):
    """Is the seasoned data set steady enough to start tuning? `pts`: [(hours since start, throughput)] samples of one
    continuous benchmark run (or of repeated iterations); `span_h`: hours between re-checks of the current configuration
    (~4 runs), the span over which drift has to stay small compared with the noise.

    A fixed bar (|trend| < 0.5%/h over one hour, from 4+ iterations) never passed: 25-minute iterations gave 2-3 points
    per hour, and with 2% scatter the one-hour trend estimate is +/-2.9%/h, so a flat workload passed ~14% of the checks.
    Here the noise is measured (scatter around the trend), the allowed trend follows from it - drift over span_h within
    half a noise threshold (2.5 x the noise of a run of run_h, from the samples' scatter): 1.25 x noise / span_h, at
    least floor_pct_h - and the stop needs evidence:
      flat       - trend + 1.65 SE below the allowed trend (last 2 h);
      plateau    - the mean of the window's second half equals the first half's within the allowed trend (+ 1.65 SE);
      flattening - still moving, but approaching a level (A + B exp(-t / tau)) and the change still to come - upper bound
                   over the time constants the data allows - fits within the allowed drift over span_h."""
    use = [p for p in pts if p[0] >= SEASON_SKIP_H]
    if len(use) < SEASON_MIN_POINTS:
        return {"stop": False, "reason": "too few samples"}
    end = use[-1][0]
    # the window grows with the seasoning (half of it, 2-6 h): a noisy but flat workload needs more samples to show that
    # it is flat; a curve that still bends is caught by the flattening check on the same window
    L = max(2.0, min(6.0, 0.5 * (end - SEASON_SKIP_H)))
    win = [p for p in use if p[0] >= end - L]
    if len(win) < SEASON_MIN_POINTS:
        win = use[-SEASON_MIN_POINTS:]
    f = _fit(win)
    if not f:
        return {"stop": False, "reason": "no trend fit"}
    b, m, sd, se = f
    q = _quad(win)
    if q and q[4] < sd:  # scatter around a bending curve: the bend is not noise
        sd = q[4]
    slope, se_pct, noise = 100 * b / m, 100 * se / m, 100 * sd / m
    # what matters is a tuning run's noise: a run averages run_h / sample_h samples
    gaps = sorted(win[k + 1][0] - win[k][0] for k in range(len(win) - 1))
    sample_h = gaps[len(gaps) // 2] if gaps else None
    run_noise = noise * (sample_h / run_h) ** 0.5 if run_h and sample_h and run_h > sample_h else noise
    tol = min(SEASON_MAX_TOL, max(floor_pct_h, 1.25 * run_noise / max(span_h, 0.5)))
    out = {"stop": False, "trend_pct_per_hour": round(slope, 2), "trend_se_pct": round(se_pct, 2), "noise_pct": round(noise, 2),
           "run_noise_pct": round(run_noise, 2),
           "allowed_pct_per_hour": round(tol, 2), "window_h": round(win[-1][0] - win[0][0], 2), "samples": len(win)}
    if abs(slope) + 1.645 * se_pct < tol:
        return dict(out, stop=True, reason="flat", text=f"trend {slope:+.2f}%/h (+/-{se_pct:.2f}) within the allowed {tol:.2f}%/h")
    recent = [p for p in use if p[0] >= end - 2.0]
    fr = _fit(recent) if len(recent) >= SEASON_MIN_POINTS and len(recent) < len(win) else None
    if fr:  # the last 2 h alone: a long window still holds the earlier climb
        s2, se2 = 100 * fr[0] / fr[1], 100 * fr[3] / fr[1]
        if abs(s2) + 1.645 * se2 < tol:
            return dict(out, stop=True, reason="flat", trend_pct_per_hour=round(s2, 2), trend_se_pct=round(se2, 2),
                        text=f"trend over the last 2 h {s2:+.2f}%/h (+/-{se2:.2f}) within the allowed {tol:.2f}%/h")
    half = L / 2
    last = [v for h, v in use if h > end - half]
    prev = [v for h, v in use if end - L < h <= end - half]
    if len(last) >= 4 and len(prev) >= 4:
        ml, mp = sum(last) / len(last), sum(prev) / len(prev)
        d = 100 * (ml - mp) / mp / half  # per hour
        se_d = noise * (1 / len(last) + 1 / len(prev)) ** 0.5 / half
        out["half_change_pct_per_hour"] = round(d, 2)
        if abs(d) + 1.645 * se_d < tol:
            return dict(out, stop=True, reason="plateau", text=f"last {half:.1f} h {d * half:+.2f}% vs the {half:.1f} h before, within the allowed {tol:.2f}%/h")
    ef = _expfit(use, end)
    if ef:
        # still moving, but approaching a level: the change still to come, upper bound over the time constants the data
        # allows, has to fit within the drift allowed between re-checks (a straight trend has no level: never passes)
        (sse, tau, A, B), rest = ef
        out.update(remaining_pct=round(rest, 2), time_constant_h=round(tau, 2))
        if rest < tol * span_h:
            return dict(out, stop=True, reason="flattening", text=f"approaching a level: at most about {rest:.2f}% still to come, within the allowed drift")
    return out


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
    if within > cv:
        # each run's own estimate says it is noisier than repeats of it are: the in-window samples trend or oscillate
        # (throughput rising or falling inside the window), which longer runs do not cure (#125: a plan that predicted
        # a higher threshold, 4.6% -> 11.7%)
        out.update(within_cv_pct=round(within, 2), between_cv_pct=None)
        return dict(out, action="unsteady-window",
                    text=f"The runs' own samples vary more ({within:.2f}%) than repeated runs do ({cv:.2f}% CV): throughput trends "
                         "or oscillates inside the window. Longer runs would not lower the threshold; check steady state.")
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
    if sec <= duration or max(RESOLUTION_NOISE, NOISE_K * exp_cv) >= (cal.get("noise_pct") or 0):
        return dict(out, action="between-runs", repeats=2,
                    text=f"Longer runs would not lower the ±{cal.get('noise_pct')}% threshold (expected ±{max(RESOLUTION_NOISE, NOISE_K * exp_cv):.1f}%): "
                         "keep the run length; repeats per configuration lower it.")
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


def _goal_server_ratio(runs):
    xs = []
    for r in runs:
        s = r.get("summary") or {}
        srv = s.get("tps") or s.get("qps")
        if srv and r.get("goal_value"):
            xs.append(r["goal_value"] / srv)
    return round(sorted(xs)[len(xs) // 2], 6) if xs else None


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
        "goal_server_ratio": _goal_server_ratio(runs),
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


DISK_FULL = (1114, 1021, 3, 1030)
LOW_FREE_PCT = 10.0


def client_failures(s):
    """The server returned errors instead of doing the work: disk-full errors (the table / disk is full), or failures
    amounting to more than a fifth of the transactions. Session 222 (HammerDB 2000W on seasoned data): the data disk
    filled, every transaction failed with 'table order_line is full', and the run was reported as CPU steal."""
    errs = s.get("client_errors") or []
    per_s = s.get("client_errors_per_s") or 0
    disk = [e for e in errs if e["number"] in DISK_FULL]
    work = max(s.get("tps") or 0, 1.0)
    if not disk and per_s < 0.2 * work:
        return None
    top = "; ".join(f"{e['name']} ({e['number']}) {e['per_s']}/s" for e in errs[:3])
    space = (f" Data directory: {s['datadir_free_gb']} GiB free ({s.get('datadir_free_pct')}%)." if s.get("datadir_free_gb") is not None else "")
    text = ("UNRELIABLE: the server returned disk-full errors - " if disk else
            f"UNRELIABLE: {per_s:.0f} failed statements/s for {s.get('tps') or 0:.0f} transactions/s - ") + top + "." + space \
        + (" Free space on the data disk, or reset the data set between runs." if disk else " The run measured failures, not work.")
    return {"disk_full": bool(disk), "errors_per_s": per_s, "top": errs[:3], "text": text}


def client_stall(s):
    """The clients stopped sending work: most connections stay open but idle and the server has nothing to do (session
    222: 259 HammerDB connections, ~15 running, CPU 1%, 14 commits/s after 270k NOPM - reported as 'CPU steal')."""
    con, run_, busy = s.get("threads_connected_avg") or 0, s.get("threads_running_avg") or 0, s.get("cpu_busy")
    if con >= 8 and busy is not None and busy < 10 and run_ < 0.25 * con and (s.get("mysqld_cpu_pct") or 0) < 5:
        return {"cpu_busy": busy, "threads_running": round(run_, 1), "threads_connected": round(con, 1)}
    return None


def steal_check(summary, noise_pct=None, ref=None):
    """Hypervisor CPU steal on the database host or a client during the run. Steal takes CPU away from a CPU-bound
    workload roughly one for one, so its possible effect on the result is about the average steal, scaled down when the
    host was not CPU-bound. A verdict compares runs, so what can bias it is steal that differs from what the compared
    runs had: ref is the session's usual steal (median of its earlier runs) - a steady 0.4% on every run lowers every
    result alike and is part of the calibrated noise (session 294: every run flagged at 0.3-0.46%). That difference must
    stay well inside the noise threshold: flag from a quarter of the threshold (at least 0.25%), or a 5% peak in any 10 s
    sample; unreliable (excluded and re-measured) from half of it (at least 0.5%). Normal on our hosts: < 0.05%."""
    s = summary or {}
    avg, peak, cli = s.get("cpu_steal") or 0, s.get("cpu_steal_max") or 0, s.get("client_cpu_steal") or 0
    r = ref or {}
    noise = float(noise_pct or DEFAULT_GOAL_NOISE)
    # steal costs throughput only when the CPUs are needed: scale by how busy the host was (no floor - on a mostly idle
    # host steal is mostly the hypervisor's accounting at low clock speeds, not lost work)
    cpu_factor = min(1.0, (s.get("cpu_busy") if s.get("cpu_busy") is not None else 100) / 70.0)
    impact = max(abs(avg - (r.get("avg") or 0)), abs(cli - (r.get("client") or 0))) * cpu_factor
    # a peak is unusual only well above the peaks the compared runs had
    peak_flag = peak >= max(STEAL_FLAG_PEAK, 2 * (r.get("peak") or 0))
    flag_at, invalid_at = max(0.25, noise / 4), max(0.5, noise / 2)
    if impact >= invalid_at:
        level = "invalid"
    elif impact >= flag_at or peak_flag:
        level = "flag"
    else:
        return None
    return {"level": level, "avg_pct": round(avg, 2), "peak_pct": round(peak, 2), "client_avg_pct": round(cli, 2) if cli else None,
            "usual_pct": round(r["avg"], 2) if r.get("avg") else None,
            "est_impact_pct": round(impact, 2), "flag_at_pct": round(flag_at, 2), "invalid_at_pct": round(invalid_at, 2)}


def usual_steal(history):
    """The session's usual steal (median of its earlier runs, unreliable ones included - they measured the host too):
    what a steady level on this host looks like. None before three runs, so the first runs compare with zero."""
    ss = [(h.get("summary") or {}) for h in history or [] if not h.get("superseded")]
    ss = [s for s in ss if s.get("cpu_steal") is not None]
    if len(ss) < 3:
        return None
    med = lambda xs: sorted(xs)[len(xs) // 2]
    return {"avg": med([s.get("cpu_steal") or 0 for s in ss]), "peak": med([s.get("cpu_steal_max") or 0 for s in ss]),
            "client": med([s.get("client_cpu_steal") or 0 for s in ss])}


def swap_cost(s, noise_pct=None):
    """mysqld's pages come back from swap during the window AND that costs measurable time. With an off-CPU profile, the
    measured share of connection-thread waiting on swap-in must be large enough to move the goal beyond noise (AMD #136:
    754 MB in swap, ~20 pages/s back, 0.003 s of 217 s waiting - no cost); without a profile, the swap-in itself counts."""
    if (s.get("dbproc_swap_mb") or 0) <= 0:
        return False
    if "dbproc_workingset_refault_anon_per_s" in s:
        back = (s.get("dbproc_workingset_refault_anon_per_s") or 0) > 0
    else:
        back = (s.get("dbproc_pswpin_per_s") or 0) > 0 or (s.get("swap_in") or 0) > 0
    if not back:
        return False
    share = s.get("offcpu_swapin_share_pct")
    return share is None or share >= float(noise_pct or DEFAULT_GOAL_NOISE)


STABILITY_MAX_COST_PCT = 30.0  # a stabilizing change is kept unless it costs more of the goal than this


def stabilized(run, history, d, goal):
    """The parent configuration's undo history grew without bound and this run's did not: stability first, so the change
    is kept even when it costs some throughput (up to STABILITY_MAX_COST_PCT). None otherwise."""
    from .engine import STABILITY_FIRST, unbounded_hll
    par = [h for h in history if h.get("seq") in ((d or {}).get("parent_runs") or [])]
    s = run.get("summary") or {}
    if not STABILITY_FIRST or not par or not all(unbounded_hll(h.get("summary")) for h in par) or unbounded_hll(s) or "unbounded_growth" not in s:
        return None
    dg = (d or {}).get("goal_delta_pct")
    if dg is not None and dg < -STABILITY_MAX_COST_PCT:
        return None
    return {"parent_runs": [h["seq"] for h in par], "goal_delta_pct": dg}


def state_flags(s):
    """What the run's own samples say about the state it measured (kept even when the run is superseded or unreliable,
    so the run list still shows it): deferred work still growing at the end, state that settled only inside the window,
    and deferred work growing without bound (a configuration that cannot keep up)."""
    out = {}
    st = (s or {}).get("steady_state") or {}
    if st and not st.get("steady"):
        out["not_steady"] = st.get("growing")
    if (s or {}).get("warmup_signals"):
        out["warmup_short"] = s["warmup_signals"]
    if (s or {}).get("unbounded_growth"):
        out["unbounded"] = s["unbounded_growth"]
    fp = (s or {}).get("datadir_free_pct")
    if fp is not None and fp < LOW_FREE_PCT:
        # below ~10% free, SSDs (less spare area for garbage collection) and filesystems (fragmented allocation) slow down:
        # a warning - results may drift for reasons that are not the configuration
        out["low_free_space"] = {"free_pct": fp, "free_gb": s.get("datadir_free_gb"), "full_in_h": s.get("datadir_full_in_h")}
    return out


def verdict(run, history, goal, noise):
    """The run's verdict and its details; every verdict - reference and repeat runs included - carries what the run's
    own samples say about its state (session 231: a 60 s warm-up was flagged on the first baseline only)."""
    v, d = _run_verdict(run, history, goal, noise)
    return v, dict(state_flags(run.get("summary")), **(d or {}))


def _run_verdict(run, history, goal, noise):
    if run.get("superseded"):
        return "superseded", dict(state_flags(run.get("summary")), note=run["superseded"])
    st = steal_check(run.get("summary"), (noise or {}).get("noise_pct"), usual_steal(history))
    if st and st["level"] == "invalid":
        return "unreliable", {"steal": st, "note": f"UNRELIABLE: hypervisor CPU steal averaged {max(st['avg_pct'], st['client_avg_pct'] or 0):.1f}% "
                                                   + (f"(usually {st['usual_pct']:.1f}% in this session) " if st.get("usual_pct") else "")
                                                   + "during the run (noisy neighbours on a shared host). The result is kept for reference "
                                                   "but excluded from comparisons."}
    mm = (run.get("summary") or {}).get("config_mismatch")
    if mm:
        return "unreliable", {"config_mismatch": mm, "note": "UNRELIABLE: the server did not run the configuration this run is labelled "
                              "with (" + "; ".join(f"{x['variable']} {x['running']} instead of {x['configured']}" for x in mm[:3])
                              + "). The result is kept for reference but excluded from comparisons."}
    fail = client_failures(run.get("summary") or {})
    if fail:
        return "unreliable", {"client_failures": fail, "note": fail["text"]}
    stall = client_stall(run.get("summary") or {})
    if stall:
        return "unreliable", {"client_stalled": stall,
                              "note": f"UNRELIABLE: the benchmark client stalled - the server was idle (CPU {stall['cpu_busy']:.0f}% busy, "
                                      f"{stall['threads_running']:.0f} of {stall['threads_connected']:.0f} connections running) while the "
                                      "clients sent almost no work. Nothing in this run says anything about the configuration; check the "
                                      "benchmark client (errors, dead virtual users)."}
    if goal != "latency":
        dev = goal_server_check(run, (noise or {}).get("goal_server_ratio"))
        if dev is not None and abs(dev) > max(3.0, float((noise or {}).get("noise_pct") or 0)):
            return "unreliable", {"goal_server_mismatch_pct": dev,
                                  "note": f"UNRELIABLE: the benchmark's result and the server's own throughput in the measured window "
                                          f"disagree by {dev:+.1f}% compared with calibration - the goal measured something else "
                                          "(warm-up or tail, a client hiccup). Kept for reference, excluded from comparisons."}
    cl = client_saturated(run.get("summary") or {})
    if cl:
        # the benchmark client, not the database, set the pace: a loss or no change says nothing (C3 / C6: 16 runs each).
        # A gain beyond noise is real and at least that large - the client capped it (273: thread pool +18%, ps-off +11%;
        # 275: thread pool +12% were thrown away and the sessions ended at -0.6% / +0.9%)
        v0, d0 = _verdict(run, history, goal, noise)
        if v0 == "improved" and goal != "latency":
            return v0, dict(d0 or {}, client_limited=cl, note=f"Improved by at least {(d0 or {}).get('goal_delta_pct')}%: {cl}, so the "
                                                               "server could have gone further - a lower bound.")
        return "unreliable", {"client_saturated": cl, "note": f"UNRELIABLE: {cl}. The measured throughput is the client's limit, "
                                                              "so the run is kept for reference but excluded from comparisons."}
    v, d = _verdict(run, history, goal, noise)
    gn = float((d or {}).get("goal_noise_pct") or 0)
    big = abs(float((d or {}).get("goal_delta_pct") or 0)) > max(4 * gn, NOT_STEADY_MAX_BIAS_PCT)
    if (d or {}).get("not_steady") and v in ("improved", "regressed") and not big:
        why = _unsteady_bias(run, history, d, v, goal)
        if why:  # the unsteadiness cannot explain this difference: the verdict stands, with the reason
            d = dict(d, not_steady_beyond_bias=True, note=f"{v} by {d.get('goal_delta_pct')}% although the window was not at steady "
                                                          f"state ({'; '.join(d['not_steady'])}): {why}")
            big = None  # handled
    if big is None:
        pass
    elif (d or {}).get("not_steady") and v in ("improved", "regressed") and big:
        # unpaid deferred work can bias a window by a few percent, not by multiples: a difference this large stands
        # (session 210: the calibrated baseline +419% over server defaults had been cancelled, so the defaults stayed "best")
        d = dict(d, not_steady_beyond_bias=True, note=f"{v} by {d.get('goal_delta_pct')}% although the window was not at steady state "
                         f"({'; '.join(d['not_steady'])}): far beyond what unpaid deferred work could explain, so the verdict stands.")
    elif (d or {}).get("not_steady") and v in ("improved", "regressed"):
        # deferred work (flushing, purge, change-buffer merges) was still piling up or the window still warming at its
        # end: the difference may come from what the window did not pay for, so it decides nothing either way
        d = dict(d, unsteady_verdict=v,
                 note=f"Inconclusive: {v} by {d.get('goal_delta_pct')}%, but the window was not at steady state "
                      f"({'; '.join(d['not_steady'])}). Treated as no measurable effect.")
        v = "neutral"
    if st:
        d = dict(d)
        d["steal"] = st
    stab = stabilized(run, history, d, goal)
    if stab:
        d = dict(d, stabilized=stab)
        d["note"] = (f"Stabilized: the undo history list no longer grows without bound (it did with the parent configuration, runs "
                     f"{stab['parent_runs']}). Kept for stability" + (f" at a cost of {-stab['goal_delta_pct']:.1f}% on the goal." if (stab.get("goal_delta_pct") or 0) < 0 else ".")
                     + (" " + d["note"] if d.get("note") else ""))
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
        if r.get("verdict") not in ("improved", "efficiency-gain") and not (r.get("verdict") == "neutral" and (ev.get("confirmed") is False
                                                                                                         or ev.get("hint_repeat"))):
            continue
        # an efficiency gain was kept with the goal within noise of the parent: its repeats must keep it there
        eff = r.get("verdict") == "efficiency-gain" or (r.get("verdict") == "neutral" and ev.get("confirmed_kind") == "efficiency-gain")
        key = config_key(r.get("managed"))
        mine = [x for x in runs if config_key(x.get("managed")) == key and x.get("goal_value") is not None and x.get("verdict") not in EXCLUDED]
        par = [x for x in runs if x["seq"] in (ev.get("parent_runs") or []) and x.get("goal_value") is not None]
        if len(mine) < 2 or not par:
            continue
        # two-stage test: the first run screened the change (and was selected for looking good - winner's curse), the
        # repeats test it independently: judged on the repeats alone, one-sided at ~95% (CONFIRM_K sigma, not the 2.5
        # sigma of the screening)
        rep = [x for x in mine if x["seq"] != r["seq"]] or mine
        m = _mean([x["goal_value"] for x in rep])
        pm = _mean([x["goal_value"] for x in par])
        d = improvement(goal, m, pm)
        thr = max(RESOLUTION_NOISE, CONFIRM_K / NOISE_K * mean_noise(noise_pct, len(rep), len(par)))
        ok = d is not None and (d >= -0.5 * thr if eff else d > thr)
        if ok and eff:
            # the saving itself must hold on the repeats, not only the goal (lab 177 / 180: a disk-KiB "gain" confirmed by
            # repeats that only checked the goal)
            held = []
            for k, e in ((ev.get("efficiency") or {}).items()):
                if not e.get("counts") or (e.get("improvement_pct") or 0) <= (e.get("noise_pct") or 0):
                    continue
                rv = [x["summary"][k] for x in rep if (x.get("summary") or {}).get(k) is not None]
                pv = [x["summary"][k] for x in par if (x.get("summary") or {}).get(k) is not None]
                if rv and pv and _mean(pv):
                    held.append(100.0 * (_mean(pv) - _mean(rv)) / _mean(pv) > CONFIRM_K / NOISE_K * (e.get("noise_pct") or MIN_EFF_NOISE))
            ok = any(held) if held else ok
        new = ("efficiency-gain" if eff else "improved") if ok else "neutral"
        ev = dict(ev, confirmed=ok, confirmed_runs=[x["seq"] for x in rep], confirmed_delta_pct=d, confirmed_threshold_pct=round(thr, 2))
        if ok:
            ev["ever_confirmed"] = True
        if eff:
            ev["confirmed_kind"] = "efficiency-gain"
        if not ok and d is None:
            ev["note"] = "Not confirmed: no comparable parent mean."
        elif not ok:
            ev["note"] = (f"Efficiency gain not confirmed: the mean of runs {ev['confirmed_runs']} is {d:+.2f}% vs the parent, worse than "
                          f"the ±{thr:.2f}% threshold for these run counts." if eff else
                          f"Not confirmed: the repeat run(s) {ev['confirmed_runs']} measured {d:+.2f}% vs the parent, within the ±{thr:.2f}% "
                          "threshold for these run counts (the first run was lucky).")
        elif ev.get("note", "").startswith(("Not confirmed", "Efficiency gain not confirmed")):
            ev.pop("note")
        if new != r.get("verdict") or ev != r.get("evaluation"):
            changed.append((r["seq"], r.get("verdict"), new))
            r["verdict"], r["evaluation"] = new, ev
    return changed


def _is_revert(change, run):
    """A change that returns a setting to an earlier configuration rather than being part of this run's experiment:
    explicit rollbacks, reverts merged into the next experiment, and a sweep's restore applied with a later run.
    For a run that belongs to a known experiment (label 'exp#N ...' or a rule id), only that experiment's own changes
    count; anything else applied before it (restores, manual applies) is context, not the experiment."""
    src = str(change.get("source") or "")
    label = str(run.get("label") or "")
    m = re.match(r"exp#(\d+) ", label)
    if m:
        return src != f"exp:{m.group(1)}"
    rid = label.split(" ")[0]
    if rid and any(str(c.get("source") or "") == f"rec:{rid}" for c in run.get("changes") or []):
        return src != f"rec:{rid}"
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
    hint = run.get("parent_hint") or (run.get("evaluation") or {}).get("parent_hint")
    if hint:
        # set by the caller when the changes cannot tell: "best safe + X" goes back to the best safe configuration and
        # re-applies X, which nets out against the trade-off configuration before it - judged against that, 17 such runs
        # showed -20% to -79% "regressions" (237: -78.4%, really +1.0% against best safe)
        hr = next((r for r in reversed(history) if r.get("seq") in hint and r.get("goal_value") is not None), None)
        if hr is not None:
            return config_key(hr.get("managed"))
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
        earlier = [h for h in history if h.get("seq") in same["seqs"]]
        selected = len(earlier) == 1 and not str(earlier[0].get("label") or "").startswith(("baseline", "calibration"))
        if dev is not None and abs(dev) > goal_noise and (noise or {}).get("runs") and selected:
            # the only earlier run is the experiment run that got this configuration repeated: it was selected for
            # passing the threshold, so a lower repeat is regression to the mean, not a changed environment
            d["note"] = (f"This repeat measured {dev:+.1f}% versus the run that triggered it (run {same['seqs'][0]}): that run was "
                         "likely lucky - it was repeated because it looked good - rather than the environment changing.")
        elif dev is not None and abs(dev) > goal_noise and (noise or {}).get("runs"):  # only once noise is calibrated
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
        material = pm[k] >= MATERIAL.get(k, 0.0)
        details["efficiency"][k]["counts"] = k in KEY_EFFICIENCY and relevant and material
        details["efficiency"][k]["under_load"] = relevant
        if k in KEY_EFFICIENCY and relevant and material:
            if ch > thr:
                better.append(label)
            elif ch < -thr:
                worse.append(label)
    details["efficiency_better"], details["efficiency_worse"] = better, worse
    # not steady: deferred work still accumulating at the end of the window, the result may be optimistic; warm-up short:
    # state settled only inside the window (a warning about the warm-up, not about this change); unbounded: deferred work
    # growing at an undiminished rate - the configuration cannot keep up
    details.update(state_flags(s))
    if s.get("tput_dip_samples"):  # the average hides stalls inside the window
        details["unstable_within_run"] = {k: s.get(k) for k in ("tput_dip_samples", "tput_dip_longest_samples", "tput_samples",
                                                                "tput_min_vs_median_pct", "blk_write_max_ms", "blk_flush_max_ms",
                                                                "disk_w_await_ms_max", "disk_r_await_ms_max")}
    if swap_cost(s, goal_noise):
        details["swap_hazard"] = {"dbproc_swap_mb": s.get("dbproc_swap_mb"), "swap_in": s.get("swap_in"),
                                  "offcpu_swapin_share_pct": s.get("offcpu_swapin_share_pct")}
    v = _goal_verdict(d_goal, goal_noise, better, worse, details)
    _secondary_latency(details, s, parent_runs, d_goal, goal_noise)
    if v == "neutral" and d_goal is not None:
        _explain_neutral(details, d_goal, goal_noise, noise)
    return v, details


def _goal_verdict(d_goal, goal_noise, better, worse, details):
    if d_goal is None:
        return "neutral"
    if d_goal > goal_noise:
        return "improved"
    if d_goal < -goal_noise:
        return "regressed"
    if better and not worse:
        if d_goal < -0.5 * goal_noise:
            # the same result for less work - not a loss in the goal's point estimate beyond half the threshold
            # (user 174: purge_threads=2 at -10.5% and redo-grow at -6% kept as "gains" on disk I/O)
            details["note"] = (f"Uses less ({', '.join(better)}) but the goal is {d_goal:+.2f}%, more than half the ±{goal_noise}% "
                               "threshold below the parent: not kept as an efficiency gain.")
            return "neutral"
        if details.get("not_steady") and any(EFFICIENCY.get(k) in better for k in DEFERRABLE):
            details["note"] = ("Less I/O per transaction while deferred work was still piling up at the end of the window: the "
                               "saving may be I/O pushed past the window. Not kept as an efficiency gain.")
            return "neutral"
        return "efficiency-gain"
    if worse and not better:
        return "efficiency-loss"
    return "neutral"


LATENCY_KEYS = (("avg_latency_ms", "avg statement latency"), ("p95_latency_ms", "p95 statement latency"))
MIN_LATENCY_NOISE = 10.0  # %: statement latencies scatter more than throughput


def _secondary_latency(details, s, parent_runs, d_goal, goal_noise):
    """The goal did not move, but statements got clearly faster at no extra CPU or I/O per transaction (session 294:
    os-autogroup +0.7% TPS, avg statement latency -24%, p95 -37% - with a co-located closed-loop client the time saved
    in the server moved to the client, so TPS could not show it). Reported, and offered as a latency improvement."""
    if d_goal is None or d_goal < -0.5 * goal_noise or details.get("efficiency_worse"):
        return
    gains = {}
    for k, label in LATENCY_KEYS:
        pv = [(r.get("summary") or {}).get(k) for r in parent_runs]
        pv = [x for x in pv if x]
        now = s.get(k)
        if not pv or not now:
            continue
        m = sum(pv) / len(pv)
        cv = (sum((x - m) ** 2 for x in pv) / (len(pv) - 1)) ** 0.5 / m * 100 if len(pv) > 1 else 0
        thr = max(MIN_LATENCY_NOISE, NOISE_K * cv)
        ch = round(100.0 * (m - now) / m, 1)  # positive = faster
        if ch > thr:
            gains[k] = {"label": label, "parent": round(m, 4), "now": now, "faster_pct": ch, "noise_pct": round(thr, 1)}
    if gains:
        details["secondary_gain"] = gains
        txt = ", ".join(f"{g['label']} {-g['faster_pct']:+.0f}%" for g in gains.values())
        details["note"] = ((details.get("note") + " ") if details.get("note") else "") + \
            f"Statements got faster ({txt}, beyond their ±{max(g['noise_pct'] for g in gains.values()):.0f}% spread) at no extra CPU or I/O per transaction."


def _explain_neutral(details, d_goal, goal_noise, noise):
    """Say why a change was not kept, and keep a promising one for a re-test: it beats what the measured variation alone
    allows (K x the repeats' CV) but not the conservative floor that holds until a re-check measures drift."""
    n = noise or {}
    point = max(RESOLUTION_NOISE, NOISE_K * float(n.get("pooled_cv_pct") or n.get("goal_cv_pct") or 0)) if (n.get("pooled_cv_pct") or n.get("goal_cv_pct")) else None
    floor_held = point is not None and point < goal_noise and not n.get("long_term_dof")
    base = f"{d_goal:+.2f}% is within the ±{goal_noise}% noise threshold"
    if floor_held:
        base += (f" (the floor until a re-check of the same configuration measures drift; the calibration repeats alone "
                 f"suggest ±{point:.1f}%)")
    base += ": not kept."
    if floor_held and d_goal >= point:
        details["promising"] = {"goal_delta_pct": d_goal, "point_threshold_pct": round(point, 2), "threshold_pct": goal_noise}
        base += " Promising: it beats the measured variation, so it is measured again once a re-check has lowered the threshold."
    details["note"] = base + ((" " + details["note"]) if details.get("note") else "")

    if d_goal > goal_noise:
        return "improved", details
    if d_goal < -goal_noise:
        return "regressed", details


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


def client_saturated(s):
    """The benchmark client limits the measurement: a remote client host at full CPU (its agent reports it), or a host
    shared with the client running out of CPU while mysqld gets less than half of what is used."""
    cb = s.get("client_cpu_busy")
    if cb is not None and cb >= 90:
        return f"the client host was {cb:.0f}% CPU busy"
    busy, my = s.get("cpu_busy"), s.get("mysqld_cpu_pct")
    if busy is not None and my is not None and busy >= 95 and my < 0.5 * busy:
        return f"the host was {busy:.0f}% CPU busy and mysqld used only {my:.0f}% of it: the co-located client competes for the CPUs"
    return None


def disturbances(runs):
    """Measured sources of run-to-run shifts in a set of runs of one configuration (evidence, not guesses):
    mysqld memory swapped back in during the window, hypervisor CPU steal, the goal drifting run after run, an
    unsteady window."""
    out = []
    ss = [(r.get("summary") or {}) for r in runs]
    # swap only when mysqld's pages coming back cost measurable time (swap_cost: the off-CPU swap-in share when profiled).
    # Suite RW-2: 0.4 GB of idle mysqld pages swapped out while a 46 GB restore streamed through the page cache, ~2 pages/s
    # read back - blamed for the noise without evidence and the session stopped
    # profiles run once per configuration: runs without one take the measured share of the profiled run of the set
    known = [s.get("offcpu_swapin_share_pct") for s in ss if s.get("offcpu_swapin_share_pct") is not None]
    ss = [s if s.get("offcpu_swapin_share_pct") is not None or not known else dict(s, offcpu_swapin_share_pct=max(known)) for s in ss]
    sw = [s for s in ss if swap_cost(s)]
    if sw:
        share = [s.get("offcpu_swapin_share_pct") for s in sw if s.get("offcpu_swapin_share_pct") is not None]
        out.append(f"mysqld has {max(s['dbproc_swap_mb'] for s in sw):.0f} MB in swap and pages are read back during the window "
                   f"(up to {max(s.get('swap_in') or 0 for s in sw):.0f} pages/s in {len(sw)} of {len(ss)} runs"
                   + (f", connection threads waited on swap-in {max(share):.1f}% of their off-CPU time" if share else "") + ")")
    cow = sorted({t for s in ss for t in (s.get("cow_issues") or [])})
    if cow:  # first writes copy blocks: runs start slow and speed up (rising throughput, biased towards postponed writes)
        out.append("copy-on-write under the data: " + "; ".join(cow))
    # storage latency moving run after run at a similar load (SSD garbage collection, cloud volume throttling / burst
    # credits): reads and writes apart, as either path alone can drift
    for k, what in (("disk_r_await_ms", "data reads"), ("disk_w_await_ms", "writes"), ("blk_flush_p99_ms", "device flushes (p99)")):
        vals = [s.get(k) for s in ss if s.get(k) is not None]
        if len(vals) >= 4:
            d = [b - a for a, b in zip(vals, vals[1:])]
            if all(x > 0 for x in d) or all(x < 0 for x in d):
                out.append(f"storage latency of {what} moved in the same direction every run ({', '.join(f'{v:.2f}' for v in vals)} ms)")
    # instability inside the windows: throughput below half its median (stalls)
    dips = [(s.get("tput_dip_samples") or 0, s.get("tput_dip_longest_samples") or 0) for s in ss if s.get("tput_dip_samples")]
    if dips:
        out.append(f"throughput fell below half its median inside {len(dips)} of {len(ss)} runs (longest stall {max(x[1] for x in dips)} samples)")
    cs = [client_saturated(s) for s in ss]
    cs = [x for x in cs if x]
    if cs:
        out.append(f"client saturated in {len(cs)} of {len(ss)} runs ({cs[0]}) - use a bigger client host or a lighter client")
    st = [s.get("cpu_steal") or 0 for s in ss]
    if st and max(st) >= 3:
        out.append(f"hypervisor CPU steal up to {max(st):.1f}%")
    goals = [r.get("goal_value") for r in runs if r.get("goal_value") is not None]
    if len(goals) >= 4:  # with 3 runs, two steps in the same direction happen by chance half the time
        diffs = [b - a for a, b in zip(goals, goals[1:])]
        if all(d < 0 for d in diffs) or all(d > 0 for d in diffs):
            out.append(f"the goal moved in the same direction every run ({', '.join(f'{g:.4g}' for g in goals)})")
    ns = [r for r in runs if ((r.get("summary") or {}).get("steady_state") or {}).get("steady") is False]
    if ns:
        out.append(f"{len(ns)} of {len(runs)} runs not at steady state")
    return out


def calibration_slide(runs):
    """Every calibration run moved the same way, and by more than the runs' own precision explains (None = no slide)."""
    goals = [r.get("goal_value") for r in runs if r.get("goal_value") is not None]
    if len(goals) < 3:
        return None
    d = [b - a for a, b in zip(goals, goals[1:])]
    if not (all(x > 0 for x in d) or all(x < 0 for x in d)):
        return None
    m = sum(goals) / len(goals)
    total = 100.0 * abs(goals[-1] - goals[0]) / abs(m) if m else 0
    ses = [(r.get("summary") or {}).get("within_se_pct") for r in runs]
    ses = [x for x in ses if x]
    prec = 2 * (sum(x * x for x in ses) / len(ses)) ** 0.5 if ses else 1.0
    if total <= prec:
        return None
    return f"{', '.join(f'{g:.4g}' for g in goals)} ({total:.1f}% total, the runs' own precision explains {prec:.1f}%)"


def goal_server_check(run, ref_ratio):
    """Does the benchmark's goal agree with the server's own throughput in the measured window? A different ratio than
    in calibration means the goal measured something else (warm-up or tail included, client hiccup): C2 run 15 scored
    3,135 while the server measured 3,750 and set the session's threshold to 12%."""
    s = run.get("summary") or {}
    srv = s.get("tps") or s.get("qps")
    g = run.get("goal_value")
    if not (ref_ratio and srv and g):
        return None
    return round(100.0 * ((g / srv) / ref_ratio - 1), 2)
