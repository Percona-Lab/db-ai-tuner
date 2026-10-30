"""Tuning a workload the agent does not drive: a third-party benchmark that runs all the time and reconnects by itself
after a restart (Parma Ham's permanent HammerDB load) - profile `mode = continuous` (user, 2026-10-28).

* Goal from the server: latency (session start --latency-stat p99: the interpolated p99 of performance_schema's statement
  histogram) or throughput; every window records a fingerprint of the workload (statement mix, connections) and a window
  whose workload changed is flagged, not compared.
* Online changes only during tuning, measured A-B-A: the current configuration, the change, the current configuration
  again; the change is judged against the mean of its two neighbours, so slow drift (data growing, caches, the load's own
  phases) cancels out. A change that helps beyond the noise is kept and becomes the next A.
* After every change: the metrics have to settle (each one level and holding for SETTLE_HOLD_S, at least SETTLE_MIN_S,
  at most SETTLE_MAX_S) and background work to finish (a buffer pool resize) before a window is measured.
* Restart probe: how long the client takes to reconnect and the server to reach steady state after a restart.
* Final validation with full restarts: the start configuration, the tuned one, the start again - each after a restart and
  the measured restart warm-up - because a setting changed online can act differently from the same setting at startup,
  and a value that only lived in memory would be gone."""
import statistics
import time

SETTLE_MIN_S = 120
SETTLE_HOLD_S = 120
SETTLE_MAX_S = 1800
RECONNECT_FRAC = 0.8  # connections back to this share of the level before the restart = the client has reconnected
RECONNECT_MAX_S = 900
CALIBRATION_WINDOWS = 3
NOISE_TARGET_PCT = 5.0
WINDOW_MAX_S = 1800  # a tail percentile needs long windows: p99 over 300 s of Parma Ham moved ±19.6% (session 412); 30 min = two purge cycles
SETTLE_KEYS = ("tps", "avg_latency_ms", "bp_hit_pct", "bp_dirty_pct", "checkpoint_age_pct", "history_list_length")


def _cli():
    from . import cli
    return cli


def _status(c, *names):
    st = c.db.status()
    return {n: float(st.get(n) or 0) for n in names}


def workload_alive(c, min_qps=20, min_conns=4, seconds=10):
    """The external workload is running: statements executed and clients connected - more than a dashboard or a monitor
    polling the server (session 413 calibrated 3.4 statements/s from 2 connections: Parma Ham's HammerDB users had died
    with a restart). (ok, info)"""
    a = _status(c, "Questions", "Threads_connected")
    time.sleep(seconds)
    b = _status(c, "Questions", "Threads_connected")
    qps = (b["Questions"] - a["Questions"]) / seconds
    return qps >= min_qps and b["Threads_connected"] >= min_conns, {"qps": round(qps, 1), "connections": int(b["Threads_connected"])}


def wait_client(c, t0, want, max_s, label):
    """After a restart begun at t0: wait until the client's connections are back (RECONNECT_FRAC of want). Seconds taken;
    Fail when they are not back within max_s - a load that did not come back is never measured as a configuration."""
    cli = _cli()
    c.db.connect(retries=60, delay=2)
    while True:
        n = _status(c, "Threads_connected")["Threads_connected"]
        if n >= RECONNECT_FRAC * want:
            return int(time.time() - t0)
        if time.time() - t0 > max_s:
            raise cli.Fail(f"{label}: the workload's client did not come back within {max_s // 60} min of the restart ({int(n)} of "
                           f"{int(want)} connections) - check the benchmark; reconnect_max in the profile sets the wait")
        c.activity("waiting for the client", f"{label}: {int(n)} of {int(want)} connections back, {int(time.time() - t0) // 60} min")
        time.sleep(10)


def fingerprint(run):
    """What the load looked like in a window: connections, statements per transaction, the read / write mix."""
    s = run.get("summary") or {}
    tps, qps = s.get("tps") or 0, s.get("qps") or 0
    return {"connections": s.get("threads_connected_avg"), "stmts_per_trx": round(qps / tps, 2) if tps else None,
            "rows_changed_per_s": s.get("rows_changed_per_s"), "qps": qps}


def workload_changed(fa, fb, tol=0.15):
    """The load itself differs between two windows beyond tol (relative), e.g. the benchmark switched phase. Reason or None."""
    for k in ("connections", "stmts_per_trx"):
        a, b = fa.get(k), fb.get(k)
        if a and b and abs(b - a) / max(abs(a), 1e-9) > tol:
            return f"{k} {a} -> {b}"
    return None


def wait_settled(c, t0, label, min_s=SETTLE_MIN_S, max_s=SETTLE_MAX_S, hold_s=SETTLE_HOLD_S):
    """Wait until the live metrics have settled after a change at t0 (and a buffer pool resize has finished). Returns the
    seconds waited and whether it settled within max_s."""
    from . import collector
    from .state import SampleStore
    cli = _cli()
    while True:
        time.sleep(30)
        el = time.time() - t0
        try:
            st_ = c.db.status()
            resize = str(st_.get("Innodb_buffer_pool_resize_status") or "")
            redo = str(st_.get("Innodb_redo_log_resize_status") or "")
        except Exception:
            resize = redo = ""
        busy = resize and not resize.lower().startswith(("completed", "size did not change")) and "resize" in resize.lower()
        if redo and redo.strip().upper() not in ("OK", ""):
            # an online redo resize (down: InnoDB flushes until the checkpoint age fits) - session 416's revert of redo-grow
            busy, resize = True, f"redo log resize: {redo}"
        res = collector.settle_times(SampleStore(c.state.dir).window(t0, time.time()), t0)
        m = res.get("metrics") or {}
        pending = [k for k in SETTLE_KEYS if k in m and not m[k].get("inert") and not m[k].get("unbounded")
                   and (m[k]["settled_after_s"] is None or m[k]["plateau_s"] < hold_s)]
        c.activity("settling", f"{label}: {int(el)} s" + (f", waiting for {', '.join(pending)}" if pending else "")
                   + (f", buffer pool resize: {resize}" if busy else ""))
        if el >= min_s and not pending and not busy:
            return int(el), True
        if el >= max_s:
            cli.log(f"   {label}: not settled after {int(el)} s ({', '.join(pending) or resize}): measuring anyway, flagged")
            return int(el), False


CLIENT_DIP_FRAC = 0.5  # the connected clients fell below this share of their median during the window: the client restarted


def client_restarted(c, t0, t1):
    """The client restarted inside [t0, t1]: the connected count dipped (from the daemon's 10 s samples). Counting new
    connections does not do: dashboards and monitors open one per poll (session 414: 258 in a steady 20-min window)."""
    from .state import SampleStore
    vals = [((x.get("db") or {}).get("threads_connected")) for x in SampleStore(c.state.dir).window(t0, t1)]
    vals = sorted(v for v in vals if v is not None)
    if len(vals) < 6:
        return None
    med = vals[len(vals) // 2]
    if med >= 4 and vals[0] < CLIENT_DIP_FRAC * med:
        return f"the client reconnected during the window (connections fell to {int(vals[0])} from about {int(med)}): not compared"
    return None


def measure(c, label, window_s, notes=None, attempts=3):
    """One measured window of the live workload; the run as recorded (goal from the server). A window during which the
    client restarted (Parma Ham starts a new HammerDB run every hour: 64 users disconnect, reconnect and ramp up - session
    414's third calibration window: throughput spread 10.8% vs 0.4-2%, latency +13%) measured the client, not the
    configuration: flagged unreliable and measured again, up to `attempts` windows."""
    for i in range(attempts):
        t0 = time.time()
        run = _measure_once(c, label + (" (again: the client reconnected)" if i else ""), window_s, notes)
        why = client_restarted(c, t0, time.time())
        if not why:
            return run
        _cli().log(f"   {label}: {why}")
        run["verdict"] = "unreliable"
        run["evaluation"] = dict(run.get("evaluation") or {}, note=why, client_restart=True)
        try:
            c.client.post(f"/runs/{run['id']}/evaluation", {"verdict": "unreliable", "evaluation": run["evaluation"]})
        except Exception:
            pass
        s_ = c.session()
        for r in s_["runs"]:
            if r["seq"] == run["seq"]:
                r["verdict"], r["evaluation"] = "unreliable", run["evaluation"]
        c.state.set("session", s_)
    return run


PERIOD_MIN_S, PERIOD_MAX_S = 300, 3600
PERIOD_MIN_CORR = 0.3


def load_period(samples, key="avg_latency_ms"):
    """The load's own cycle (s), from 1-minute means of the daemon samples: the lag with the highest autocorrelation
    between 5 and 60 minutes, if clearly periodic (>= 0.3) - Parma Ham's purge every 15 min. Windows that span whole
    cycles see the same share of every phase; 20-min windows saw one purge or two (latency spread +-11% to +-86%)."""
    pts = [((x.get("ts") or 0), (x.get("db") or {}).get(key)) for x in samples]
    pts = [(t, v) for t, v in pts if v is not None]
    if len(pts) < 60:
        return None
    t0 = pts[0][0]
    mins = {}
    for t, v in pts:
        mins.setdefault(int((t - t0) // 60), []).append(v)
    n = max(mins) + 1
    if n < 3 * PERIOD_MIN_S // 60:
        return None
    series = [statistics.mean(mins[i]) if i in mins else None for i in range(n)]
    known = [v for v in series if v is not None]
    m = statistics.mean(known)
    var = sum((v - m) ** 2 for v in known) / len(known)
    if not var:
        return None
    corr = {}
    for lag in range(PERIOD_MIN_S // 60, min(PERIOD_MAX_S // 60, n // 2) + 1):
        xs = [(series[i] - m) * (series[i + lag] - m) for i in range(n - lag) if series[i] is not None and series[i + lag] is not None]
        if len(xs) >= 10:
            corr[lag] = sum(xs) / len(xs) / var
    if not corr or max(corr.values()) < PERIOD_MIN_CORR:
        return None
    # every multiple of the cycle correlates too: the shortest lag nearly as strong as the best is the cycle itself
    top = max(corr.values())
    lag = min(k for k, r in corr.items() if r >= 0.8 * top and r >= PERIOD_MIN_CORR
              and r >= corr.get(k - 1, -1) and r >= corr.get(k + 1, -1))
    return lag * 60


def cycle_window(window, period):
    """The window rounded up to whole cycles of the load (at least one)."""
    if not period:
        return window
    return int(max(1, -(-window // period)) * period)


def _measure_once(c, label, window_s, notes=None):
    cli = _cli()
    cli._measure_start(c, label, notes, warmed=True)
    c.activity("measuring", label, window_s)
    t = time.time()
    try:
        while time.time() - t < window_s:
            time.sleep(5)
            cli._incident_check(c)
    except BaseException:
        m0 = c.state.get("measurement")
        if m0:
            try:
                c.client.post(f"/runs/{m0['run_id']}/abort", {"reason": "interrupted"})
            except Exception:
                pass
            c.state.data["measurement"] = None
            c.state.save()
        raise
    run, _ = cli._measure_stop(c, {}, None, notes)
    run["fingerprint"] = fingerprint(run)
    return run


def noise_pct(values):
    """Threshold from the A windows: twice their coefficient of variation, at least 1%."""
    vals = [v for v in values if v]
    if len(vals) < 2:
        return 3.0
    return round(max(1.0, 2 * 100.0 * statistics.stdev(vals) / statistics.mean(vals)), 2)


def aba_verdict(goal, a_before, b, a_after, thr):
    """B against the mean of its neighbours: (verdict, delta %, neighbour mean)."""
    cli = _cli()
    ref = statistics.mean([a_before, a_after]) if a_after is not None else a_before
    d = cli._improvement(goal, b, ref)
    if d is None:
        return "neutral", None, ref
    return ("improved" if d > thr else "regressed" if d < -thr else "neutral"), round(d, 2), ref


# resource use per transaction that judges a continuous load (user, 2026-10-30, after ResTune, SIGMOD 2021: on a load that
# does not saturate the server, "the same work for less" is the gain). Measured on pmh-ps97 (Parma Ham, 30-min windows
# whole purge cycles): neighbouring unchanged windows differed by 1.7% in CPU per commit, 2.7% fsyncs, 2.9% redo, 4.9%
# write I/Os - and by 10% (28% worst) in statement latency, which drifted 1.4 -> 2.6 ms over the day
EFF_KEYS = ("mysqld_cpu_ms_per_trx", "fsyncs_per_trx", "redo_kb_per_trx", "disk_ios_per_trx", "disk_kb_per_trx", "db_mem_mb")
EFF_MIN_NOISE = 1.0  # % - below this a difference is not worth a decision however steady the windows
EFF_DEFAULT_NOISE = 5.0


def _val(run, key):
    if key == "goal":
        return run.get("goal_value")
    return (run.get("summary") or {}).get(key)


def pair_noise(pairs, key):
    """Noise threshold (%) of a metric from pairs of unchanged windows (the A windows around each experiment and the
    calibration's consecutive ones - an A/A test each): 2.5 x the standard deviation of a single window, estimated from
    the pairs' relative differences (a difference of two windows has sqrt(2) x their spread)."""
    rel = []
    for x, y in pairs:
        a, b = _val(x, key), _val(y, key)
        if a and b:
            rel.append(100.0 * (b - a) / ((a + b) / 2))
    if len(rel) < 2:
        return None
    sd = (sum(r * r for r in rel) / len(rel)) ** 0.5 / 2 ** 0.5
    return round(max(EFF_MIN_NOISE, 2.5 * sd), 2)


def a_pairs(sess):
    """Consecutive unchanged windows of the same configuration (no experiment between them, or the A windows on both sides
    of one): the A/A evidence the noise thresholds are learned from."""
    runs = [r for r in sess.get("runs") or [] if r.get("goal_value") is not None and r.get("verdict") != "unreliable"]
    ev = cli_eval()
    out = []
    for i, r in enumerate(runs):
        if not str(r.get("label", "")).startswith(("baseline", "A ")):
            continue
        prev = next((x for x in reversed(runs[:i]) if str(x.get("label", "")).startswith(("baseline", "A "))
                     and ev.config_key(x.get("managed")) == ev.config_key(r.get("managed"))), None)
        if prev is not None:
            out.append((prev, r))
    return out


def cli_eval():
    return _cli().evaluate


def noise_table(sess, pairs=None):
    """{metric: noise %} for the goal, throughput and the efficiency metrics, from the session's A/A pairs."""
    pairs = pairs if pairs is not None else a_pairs(sess)
    out = {}
    for k in ("goal", "tps") + EFF_KEYS:
        n = pair_noise(pairs, k)
        if n is not None:
            out[k] = n
    return out


def eff_verdict(goal, a1, b, a2, noise):
    """A-B-A judged on resource use per transaction under latency / throughput guards. Returns (verdict, details):
    improved - the goal better beyond its noise, or some resource per transaction lower beyond its noise and none higher,
               with the goal and throughput not worse beyond theirs;
    regressed - the goal or throughput worse beyond noise, or resources only higher;
    neutral   - nothing beyond noise, or one resource lower and another higher (a trade, listed)."""
    cli = _cli()
    ev = cli.evaluate
    def ref(key):
        vals = [v for v in (_val(a1, key), _val(a2, key) if a2 else None) if v]
        return statistics.mean(vals) if vals else None
    det = {"metrics": {}}
    gref, gb = ref("goal"), _val(b, "goal")
    g = cli._improvement(goal, gb, gref) if gref and gb is not None else None
    gn = noise.get("goal") or EFF_DEFAULT_NOISE
    t_ref, tb = ref("tps"), _val(b, "tps")
    t = 100.0 * (tb - t_ref) / t_ref if t_ref and tb is not None else None
    tn = max(noise.get("tps") or 2.0, 1.0)
    better, worse = [], []
    for k in EFF_KEYS:
        r_, v_ = ref(k), _val(b, k)
        if not r_ or v_ is None or r_ < ev.MATERIAL.get(k, 0.0):
            continue
        ch = round(100.0 * (r_ - v_) / r_, 2)  # positive = less per transaction
        n_ = noise.get(k) or EFF_DEFAULT_NOISE
        det["metrics"][k] = {"label": ev.EFFICIENCY.get(k, k), "improvement_pct": ch, "noise_pct": n_}
        if ch > n_:
            better.append(k)
        elif ch < -n_:
            worse.append(k)
    det.update(goal_delta_pct=None if g is None else round(g, 2), goal_noise_pct=gn,
               tps_delta_pct=None if t is None else round(t, 2), tps_noise_pct=tn,
               efficiency_better=[ev.EFFICIENCY.get(k, k) for k in better], efficiency_worse=[ev.EFFICIENCY.get(k, k) for k in worse])
    goal_bad = g is not None and g < -gn
    tput_bad = t is not None and t < -tn
    if goal_bad or tput_bad:
        det["why"] = ("the goal" if goal_bad else "throughput") + " worse beyond its noise"
        return "regressed", det
    if g is not None and g > gn:
        det["why"] = "the goal better beyond its noise"
        return "improved", det
    if better and not worse:
        det["why"] = "less " + ", ".join(ev.EFFICIENCY.get(k, k) for k in better) + " for the same goal and throughput"
        return "improved", det
    if worse and not better:
        det["why"] = "more " + ", ".join(ev.EFFICIENCY.get(k, k) for k in worse) + " for the same goal and throughput"
        return "regressed", det
    if better and worse:
        det["why"] = ("a trade: less " + ", ".join(ev.EFFICIENCY.get(k, k) for k in better) + ", more "
                      + ", ".join(ev.EFFICIENCY.get(k, k) for k in worse))
    return "neutral", det


def borderline(det):
    """The deciding difference is near its noise (within 0.5x - 2x): one more interleaved B / A pair decides (RMIT)."""
    xs = []
    if det.get("goal_delta_pct") is not None:
        xs.append(abs(det["goal_delta_pct"]) / (det.get("goal_noise_pct") or 1))
    xs += [abs(m["improvement_pct"]) / (m["noise_pct"] or 1) for m in (det.get("metrics") or {}).values()]
    top = max(xs or [0])
    return 0.5 <= top <= 2.0


def judge(c, sess, run_b, run_a1, run_a2, thr):
    """Record the A-B-A verdict on run B (server and local session)."""
    v, d, ref = aba_verdict(sess["goal"], run_a1["goal_value"], run_b["goal_value"], run_a2["goal_value"] if run_a2 else None, thr)
    noise = noise_table(sess)
    noise.setdefault("goal", thr)
    ev_v, det = eff_verdict(sess["goal"], run_a1, run_b, run_a2, noise)
    v = ev_v
    changed = workload_changed(run_a1.get("fingerprint") or {}, run_b.get("fingerprint") or {})
    a1, a2 = run_a1.get("goal_value"), (run_a2 or {}).get("goal_value")
    if not changed and a1 and a2 and abs(a2 - a1) / min(abs(a1), abs(a2)) * 100 > 3 * thr:
        # the two neighbours disagree far beyond the noise: the server changed between them, and their mean is no reference
        # (session 414: A windows of 1.47 and 30.8 ms - the revert had left a 128 MB buffer pool - made a regression "+86%")
        changed = f"the two neighbouring windows of the current configuration disagree ({a1:.4g} vs {a2:.4g}, beyond 3x the noise)"
        run_b["_server_changed"] = changed
    if changed:
        v = "unreliable"
    ev = dict(run_b.get("evaluation") or {}, method="A-B-A, resources per transaction under goal / throughput guards",
              goal_delta_pct=d, goal_noise_pct=noise.get("goal", thr), neighbours_mean=round(ref, 4),
              parent_runs=[x for r in (run_a1, run_a2) if r for x in (r["seq"] if isinstance(r["seq"], list) else [r["seq"]])],
              efficiency=det.get("metrics"),
              efficiency_better=det.get("efficiency_better"), efficiency_worse=det.get("efficiency_worse"),
              tps_delta_pct=det.get("tps_delta_pct"), why=det.get("why"),
              **({"note": f"not compared: {changed}"} if changed else {"note": det["why"]} if det.get("why") else {}))
    run_b["_det"] = det
    run_b["verdict"], run_b["evaluation"] = v, ev
    try:
        c.client.post(f"/runs/{run_b['id']}/evaluation", {"verdict": v, "evaluation": ev})
    except Exception:
        pass
    s = c.session()
    for r in s["runs"]:
        if r["seq"] == run_b["seq"]:
            r["verdict"], r["evaluation"] = v, ev
    c.state.set("session", s)
    return v, d


def pooled(runs):
    """Several windows of one configuration as one: means of the goal and the summary metrics (an interleaved repeat)."""
    runs = [r for r in runs if r]
    summ = {}
    for k in ("tps",) + EFF_KEYS:
        vals = [(r.get("summary") or {}).get(k) for r in runs]
        vals = [v for v in vals if v is not None]
        if vals:
            summ[k] = statistics.mean(vals)
    goals = [r["goal_value"] for r in runs if r.get("goal_value") is not None]
    return {"goal_value": statistics.mean(goals) if goals else None, "summary": summ, "seq": [r["seq"] for r in runs],
            "fingerprint": runs[0].get("fingerprint"), "id": runs[0].get("id"), "evaluation": runs[0].get("evaluation")}


def restart_probe(c, want, max_s):
    """Restart mysqld, then: seconds until the client is back (connections), and until the metrics settle. Returns the
    warm-up to wait after a restart (both + 20%)."""
    from .applier import Applier
    cli = _cli()
    cli.log(f"== restart probe: {int(want)} connections before the restart")
    t0 = time.time()
    ok, err = Applier(c.cfg, c.state, c.db, log=cli.log, activity=c.activity).restart()
    if not ok:
        raise cli.ServerDown(f"restart failed: {err}")
    back = wait_client(c, t0, want, max_s, "restart probe")
    settle, ok_ = wait_settled(c, time.time(), "after the restart", min_s=300, max_s=3600)
    # the warm-up counts from the client's return: when it comes back depends on the client (a HammerDB run starting on
    # the hour), how long the server needs then does not
    warm = int(settle * 1.2)
    msg = (f"restart probe: the client was back after {back} s, the server settled {settle} s later"
           + ("" if ok_ else " (not fully)") + f": warm-up {warm} s after the client is back")
    cli.log("   " + msg)
    c.event("run_length", msg, {"reconnect_s": back, "settle_s": settle, "restart_warmup_s": warm})
    return warm


def _candidates(res, tried, tradeoffs, restart=False):
    """Ideas not tried yet: config items whose changes apply online (restart=False) or need a restart (restart=True)."""
    out = []
    for it in res.get("items") or []:
        if it.get("kind") != "config" or (it.get("confidence") or 0) <= 0 or it["id"] in tried or bool(it.get("restart")) != restart:
            continue
        if it.get("safety") == "trade-off" and not tradeoffs:
            continue
        out.append(it)
    return out


def after_restart(c, t0, before, warm, label, max_s=3900):
    """After a restart begun at t0: the client back (connections), the measured restart warm-up over, the metrics settled."""
    cli = _cli()
    back = wait_client(c, t0, before, max_s, label)
    # the warm-up counts from the client's return: a client that took an hour to come back starts its own warm-up then
    left = warm - (time.time() - (t0 + back))
    if left > 0:
        c.activity("warm-up after the restart", label, left)
        time.sleep(left)
    wait_settled(c, time.time() - 60, label, min_s=0, max_s=1200)
    cli.log(f"   {label}: ready {int(time.time() - t0)} s after the restart began")


def _cfg_key(c):
    return _cli().evaluate.config_key({k: v["value"] for k, v in c.state.get("managed", {}).items()})


def _next_item(c, a, tried, restart):
    """The next idea of the kind asked for: the rules first, then (--explore) AI suggestions."""
    cli = _cli()
    res = cli._recommend(c, c.session())
    cands = _candidates(res, tried, a.tradeoffs, restart)
    if not cands and c.session().get("explore"):
        cli.log("== explore: asking for AI suggestions" + (" (restart ideas)" if restart else " (online ideas)"))
        res = cli._recommend(c, c.session(), llm=True)
        cands = _candidates(res, tried, a.tradeoffs, restart)
    return cands[0] if cands else None


def autotune(c, prof, a):
    """The continuous-workload tuning loop (see the module doc): online ideas A-B-A, then restart ideas A-B-A with every
    window after a restart, then the final validation."""
    cli = _cli()
    log = cli.log
    sess = c.session()
    sess["bench"] = sess.get("bench") or getattr(prof, "path", None)
    sess["continuous"] = True
    if getattr(a, "explore", False):
        sess["explore"] = True  # the rules first, then AI suggestions, within --max-runs
    c.state.set("session", sess)
    window = int(prof.duration or 300)
    ok, info = workload_alive(c, prof.min_qps, prof.min_connections)
    if not ok:
        raise cli.Fail(f"no workload running (qps {info['qps']}, {info['connections']} connections): a continuous profile tunes a "
                       "load that is already running - start the benchmark first")
    log(f"== continuous workload: {info['qps']} statements/s, {info['connections']} connections; goal {sess['goal']}"
        + (f" ({sess['latency_stat']})" if sess.get("latency_stat") else "") + f"; changes measured A-B-A")
    # calibration: the current configuration, CALIBRATION_WINDOWS windows after it settled; the window grows until the
    # noise is within NOISE_TARGET_PCT (noise falls ~ 1/sqrt(window): 4x longer halves it), at most WINDOW_MAX_S
    window = int(sess.get("cont_window") or window)
    if not sess.get("runs"):
        wait_settled(c, time.time(), "before the calibration", min_s=60)
    while True:
        sess = c.session()
        a_runs = [r for r in sess["runs"] if str(r.get("label", "")).startswith(("baseline", "A "))
                  and r.get("verdict") != "unreliable" and r.get("goal_value") is not None
                  and int((r.get("summary") or {}).get("duration_s") or window) >= 0.9 * window]
        for i in range(len(a_runs), CALIBRATION_WINDOWS):
            a_runs.append(measure(c, f"baseline {window // 60} min ({i + 1}/{CALIBRATION_WINDOWS})", window))
        thr = noise_pct([r["goal_value"] for r in a_runs[-CALIBRATION_WINDOWS:]])
        cal = a_runs[-CALIBRATION_WINDOWS:]
        eff = noise_table(c.session(), list(zip(cal, cal[1:])))
        best_eff = min([v for k, v in eff.items() if k in EFF_KEYS] or [thr])
        log(f"   noise from {CALIBRATION_WINDOWS} calibration windows of {window} s: goal ±{thr}%; "
            + ", ".join(f"{k} ±{v}%" for k, v in eff.items() if k in EFF_KEYS))
        sess = c.session()
        if not sess.get("cont_period_checked"):
            # the load's own cycle (a purge job, a batch every N minutes): windows of whole cycles, then calibrate again
            from .state import SampleStore
            per = load_period(SampleStore(c.state.dir).window(min(r.get("started") or time.time() for r in cal), time.time()))
            sess["cont_period_checked"], sess["cont_period_s"] = True, per
            c.state.set("session", sess)
            if per:
                new_w = min(max(WINDOW_MAX_S, per), cycle_window(max(window, 1200), per))
                c.event("noise-plan", f"continuous: the load repeats every {per // 60} min - windows of {new_w // 60} min "
                        "(whole cycles), calibration measured again", {"period_s": per, "window_s": new_w})
                log(f"   the load repeats every {per // 60} min: windows of {new_w // 60} min (whole cycles), calibrating again")
                window = new_w
                sess["cont_window"] = window
                c.state.set("session", sess)
                continue
        if min(thr, best_eff) <= NOISE_TARGET_PCT or window >= WINDOW_MAX_S:
            break
        window = min(WINDOW_MAX_S, int(window * min(16.0, (min(thr, best_eff) / NOISE_TARGET_PCT) ** 2)) // 60 * 60 or 60)
        window = cycle_window(window, c.session().get("cont_period_s"))
        log(f"   above the ±{NOISE_TARGET_PCT}% target: windows of {window} s from now, calibration measured again")
        c.event("noise-plan", f"continuous: noise ±{thr}% with the goal measured over windows that short; windows of {window} s", {"window_s": window})
        sess = c.session()
        sess["cont_window"] = window
        c.state.set("session", sess)
    if thr > NOISE_TARGET_PCT:
        log(f"   NOTE: still ±{thr}% at the longest window ({WINDOW_MAX_S} s): only larger effects can be resolved")
    restart_warm = c.session().get("restart_warmup_s")  # measured once per session (an autotune run again reuses it)
    want = info["connections"]  # the load's own connection count: what "the client is back" means after a restart
    if not restart_warm and str(c.cfg.get("allow_restart", "true")).lower() == "true":
        restart_warm = restart_probe(c, want, prof.reconnect_max)
        sess = c.session()
        sess["restart_warmup_s"] = restart_warm
        c.state.set("session", sess)
        wait_settled(c, time.time(), "after the restart probe", min_s=60)
        a_runs.append(measure(c, "A (after the restart probe)", window))
    start_managed = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    tried = set(c.session().get("cont_tried") or [])
    measured = 0
    can_restart = str(c.cfg.get("allow_restart", "true")).lower() == "true"
    last_a = a_runs[-1] if a_runs else None
    last_ar = None  # the latest window of the current configuration measured after a restart (restart experiments' A)

    def mark(it):
        tried.add(it["id"])
        s_ = c.session()
        s_["cont_tried"] = sorted(tried)
        c.state.set("session", s_)

    def changes_of(it):
        return [{"variable": ch["variable"], "value": ch["proposed"], "reason": it.get("rationale") or it.get("title"),
                 "source": f"rec:{it['id']}"} for ch in it["changes"]]

    # 1. online ideas: A - B - A without restarts
    while measured < a.max_runs:
        it = _next_item(c, a, tried, restart=False)
        if not it:
            log("== no online idea left")
            break
        mark(it)
        if last_a is None or time.time() - (last_a.get("ended") or 0) > 2 * window + SETTLE_MAX_S:
            last_a = measure(c, "A (current configuration)", window)
        before = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
        changes = changes_of(it)
        log(f"== {it['id']} (online): " + ", ".join(f"{ch['variable']}={ch['value']}" for ch in changes))
        t0 = time.time()
        r = cli._apply_and_record(c, changes, "continuous", restart=False)
        if r.get("needs_restart") or r.get("errors") or r.get("rolled_back"):
            log(f"   not applied online ({r.get('needs_restart') or r.get('errors')}): back to the current configuration")
            cli._goto_managed(c, before, f"{it['id']} not applicable online", "continuous")
            continue
        wait_settled(c, t0, it["id"])
        b = measure(c, it["id"], window)
        measured += 1
        t1 = time.time()
        cli._goto_managed(c, before, f"A-B-A: back to the current configuration after {it['id']}", "continuous")
        wait_settled(c, t1, f"after reverting {it['id']}")
        a2 = measure(c, "A (current configuration)", window)
        v, d = judge(c, c.session(), b, last_a, a2, thr)
        if b.get("_server_changed"):
            # the second neighbour may have caught a transient (an online resize still paying off): settle once more and
            # measure a third window - the two that agree decide; still apart, the server did change
            log(f"   {b['_server_changed']}: settling and measuring the current configuration once more")
            b.pop("_server_changed", None)
            wait_settled(c, time.time(), "after a disagreeing window", min_s=300)
            a3 = measure(c, "A (current configuration, third)", window)
            v, d = judge(c, c.session(), b, last_a, a3, thr)
            if not b.get("_server_changed"):
                a2 = a3
        if b.get("_server_changed"):
            # the current configuration itself measured very differently before and after: something changed the server
            # (an online revert that did not restore the start - session 414) - stop rather than tune a changed server
            raise cli.Fail(f"stopped: {b['_server_changed']} - check the server's configuration and state before going on")
        if borderline(b.get("_det") or {}) and measured < a.max_runs:
            # near the noise: one more B / A pair, interleaved, and the verdict from all windows (randomized multiple
            # interleaved trials, Abedi & Brecht 2017: a single A-B-A can catch a regular interference pattern in phase)
            log(f"   {it['id']}: near the noise ({b['_det'].get('why') or 'no clear difference'}) - one more interleaved B / A pair")
            t3 = time.time()
            cli._apply_and_record(c, changes, "continuous", restart=False)
            wait_settled(c, t3, f"{it['id']} (interleaved repeat)")
            b2 = measure(c, f"{it['id']} (interleaved repeat)", window)
            t4 = time.time()
            cli._goto_managed(c, before, f"A-B-A-B-A: back to the current configuration after {it['id']}", "continuous")
            wait_settled(c, t4, f"after reverting {it['id']} again")
            a3 = measure(c, "A (current configuration)", window)
            bb = dict(pooled([b, b2]), seq=b["seq"], id=b["id"])
            v, d = judge(c, c.session(), bb, pooled([last_a, a2, a3]), None, thr)
            b["evaluation"] = bb.get("evaluation")
            a2 = a3
        why_ = (b.get("evaluation") or {}).get("why")
        log(f"   {it['id']}: goal {d:+.2f}% vs the neighbours' mean: {v}" + (f" - {why_}" if why_ else "") if d is not None
            else f"   {it['id']}: {v}")
        if v == "improved":
            t2 = time.time()
            cli._apply_and_record(c, changes, "continuous", restart=False)
            wait_settled(c, t2, f"{it['id']} (kept)")
            last_a = measure(c, f"A (kept {it['id']})", window)
        else:
            last_a = a2
    # 2. restart ideas: every window after a restart and the measured warm-up, the A windows too (a restart empties the
    # caches: a restarted B compared with an A that was not restarted would measure the restart)
    warm = restart_warm or 900
    while can_restart and measured < a.max_runs:
        it = _next_item(c, a, tried, restart=True)
        if not it:
            log("== no restart idea left")
            break
        mark(it)
        from .applier import Applier
        if last_ar is None or last_ar.get("_key") != _cfg_key(c):
            before_n = _status(c, "Threads_connected")["Threads_connected"]
            t0 = time.time()
            ok, err = Applier(c.cfg, c.state, c.db, log=log, activity=c.activity).restart()
            if not ok:
                raise cli.ServerDown(f"restart failed: {err}")
            after_restart(c, t0, max(before_n, want), warm, "current configuration", prof.reconnect_max)
            last_ar = measure(c, "A after restart (current configuration)", window)
            last_ar["_key"] = _cfg_key(c)
        before = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
        changes = changes_of(it)
        log(f"== {it['id']} (restart): " + ", ".join(f"{ch['variable']}={ch['value']}" for ch in changes))
        before_n = _status(c, "Threads_connected")["Threads_connected"]
        t0 = time.time()
        r = cli._apply_and_record(c, changes, "continuous", restart=True)
        if r.get("errors") or r.get("rolled_back"):
            log(f"   not applied ({r.get('errors')}): skipped")
            continue
        after_restart(c, t0, max(before_n, want), warm, it["id"], prof.reconnect_max)
        b = measure(c, f"{it['id']} (after restart)", window)
        measured += 1
        before_n = _status(c, "Threads_connected")["Threads_connected"]
        t1 = time.time()
        # reverting a restart-only setting restarts mysqld (the applier does when a change needs it)
        cli._goto_managed(c, before, f"A-B-A: back to the current configuration after {it['id']}", "continuous")
        after_restart(c, t1, max(before_n, want), warm, f"after reverting {it['id']}", prof.reconnect_max)
        a2 = measure(c, "A after restart (current configuration)", window)
        a2["_key"] = _cfg_key(c)
        v, d = judge(c, c.session(), b, last_ar, a2, thr)
        if b.get("_server_changed"):
            # the second neighbour may have caught a transient (an online resize still paying off): settle once more and
            # measure a third window - the two that agree decide; still apart, the server did change
            log(f"   {b['_server_changed']}: settling and measuring the current configuration once more")
            b.pop("_server_changed", None)
            wait_settled(c, time.time(), "after a disagreeing window", min_s=300)
            a3 = measure(c, "A (current configuration, third)", window)
            v, d = judge(c, c.session(), b, last_ar, a3, thr)
            if not b.get("_server_changed"):
                a2 = a3
        if b.get("_server_changed"):
            # the current configuration itself measured very differently before and after: something changed the server
            # (an online revert that did not restore the start - session 414) - stop rather than tune a changed server
            raise cli.Fail(f"stopped: {b['_server_changed']} - check the server's configuration and state before going on")
        log(f"   {it['id']}: {d:+.2f}% vs the neighbours' mean (±{thr}%): {v}" if d is not None else f"   {it['id']}: {v}")
        if v == "improved":
            before_n = _status(c, "Threads_connected")["Threads_connected"]
            t2 = time.time()
            cli._apply_and_record(c, changes, "continuous", restart=True)
            after_restart(c, t2, max(before_n, want), warm, f"{it['id']} (kept)", prof.reconnect_max)
            last_ar = measure(c, f"A after restart (kept {it['id']})", window)
            last_ar["_key"] = _cfg_key(c)
        else:
            last_ar = a2
    final_validation(c, start_managed, window, restart_warm, want, prof.reconnect_max)


def final_validation(c, start_managed, window, restart_warm, want=None, max_s=3900):
    """The tuned configuration against the start, each after a full restart: start - tuned - start."""
    from .applier import Applier
    cli = _cli()
    log = cli.log
    tuned = {k: v["value"] for k, v in c.state.get("managed", {}).items()}
    if cli.evaluate.config_key(tuned) == cli.evaluate.config_key(start_managed):
        log("== final validation: nothing was kept, nothing to validate")
        return None
    if str(c.cfg.get("allow_restart", "true")).lower() != "true":
        log("== final validation skipped: restarts are not allowed on this host")
        return None
    warm = restart_warm or 900
    log(f"== final validation with full restarts (warm-up {warm} s after each): start - tuned - start")
    runs = []
    for name, cfg in (("start", start_managed), ("tuned", tuned), ("start", start_managed)):
        before_n = _status(c, "Threads_connected")["Threads_connected"]
        t0 = time.time()
        cli._goto_managed(c, cfg, f"final validation: {name} configuration", "continuous")
        ok, err = Applier(c.cfg, c.state, c.db, log=log, activity=c.activity).restart()
        if not ok:
            raise cli.ServerDown(f"restart failed in the final validation: {err}")
        after_restart(c, t0, max(before_n, want or 0), warm, f"final validation: {name} configuration", max_s)
        runs.append(measure(c, f"validation: {name} configuration (after restart)", window))
    sess = c.session()
    a_vals = [r["goal_value"] for r in sess["runs"] if str(r.get("label", "")).startswith(("baseline", "A "))]
    thr = noise_pct(a_vals[-CALIBRATION_WINDOWS:])
    v, d = judge(c, sess, runs[1], runs[0], runs[2], thr)
    msg = (f"final validation after full restarts: tuned configuration {d:+.2f}% vs the start (±{thr}%): {v}" if d is not None
           else f"final validation: {v}")
    log("== " + msg)
    c.event("validation", msg, {"runs": [r["seq"] for r in runs], "verdict": v, "delta_pct": d})
    if v == "regressed":
        # measured worse after a restart than online: the start configuration stays, and the report says so
        log("   the tuned configuration lost after full restarts: the start configuration stays")
        return v, d
    cli._goto_managed(c, tuned, "final validation done: the tuned configuration stays", "continuous")
    return v, d
