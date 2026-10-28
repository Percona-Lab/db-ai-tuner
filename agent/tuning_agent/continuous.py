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
WINDOW_MAX_S = 1200  # a tail percentile needs long windows: p99 over 300 s of Parma Ham moved ±19.6% (session 412)
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
            resize = str(c.db.status().get("Innodb_buffer_pool_resize_status") or "")
        except Exception:
            resize = ""
        busy = resize and not resize.lower().startswith(("completed", "size did not change")) and "resize" in resize.lower()
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


def judge(c, sess, run_b, run_a1, run_a2, thr):
    """Record the A-B-A verdict on run B (server and local session)."""
    v, d, ref = aba_verdict(sess["goal"], run_a1["goal_value"], run_b["goal_value"], run_a2["goal_value"] if run_a2 else None, thr)
    changed = workload_changed(run_a1.get("fingerprint") or {}, run_b.get("fingerprint") or {})
    if changed:
        v = "unreliable"
    ev = dict(run_b.get("evaluation") or {}, method="A-B-A", goal_delta_pct=d, goal_noise_pct=thr, neighbours_mean=round(ref, 4),
              parent_runs=[r["seq"] for r in (run_a1, run_a2) if r],
              **({"note": f"the workload changed between the windows ({changed}): not compared"} if changed else {}))
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
        log(f"   noise from {CALIBRATION_WINDOWS} calibration windows of {window} s: ±{thr}%")
        if thr <= NOISE_TARGET_PCT or window >= WINDOW_MAX_S:
            break
        window = min(WINDOW_MAX_S, int(window * min(16.0, (thr / NOISE_TARGET_PCT) ** 2)) // 60 * 60 or 60)
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
        log(f"   {it['id']}: {d:+.2f}% vs the neighbours' mean (±{thr}%): {v}" if d is not None else f"   {it['id']}: {v}")
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
