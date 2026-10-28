"""Benchmark profiles: let the agent drive a benchmark end to end.

Profile (INI):

    [bench]
    name = HammerDB TPROC-C 10W, 10 VU
    reset = /root/bench/reset.sh        ; optional: restore the data set before each run (may restart mysqld)
    run = /root/bench/hammerdb.sh       ; started in the background; output is captured
    warmup = 120                        ; seconds after start before measuring; not set or "auto": found by a run-length probe
    duration = 300                      ; measured seconds; 0 = until the run command exits
    timeout = 1200                      ; hard limit for the run command
    goal_metric = nopm                  ; external metric used as the goal value (optional)

    [metrics]                           ; name = regex (first group); prefix sum:/geomean:/count:/last: (default last)
    nopm = achieved (\\d+) NOPM
    tpm = from (\\d+) MySQL TPM

    data_state = reset                  ; how runs start from a comparable data set: reset (the reset command restores it),
                                        ; seasoned (one long pre-run to steady state, then runs continue on it), read-only
    season_hours = 12                   ; seasoned: maximum seasoning time (stops earlier once throughput is steady)
    adaptive_duration = auto            ; auto: lengthen runs when calibration shows mostly within-run noise | off
    max_duration = 1200                 ; upper limit for that (default: 4 x duration, at most 30 min)

    The run command may use {duration} {warmup} {total} (seconds) and {duration_min} {warmup_min} {total_min}
    (whole minutes); they follow run-length changes. Without them the run length is fixed. {warmup} can be 0 - the
    seasoning and run-length probes are one continuous run of {total} with no separate warm-up - and must then mean
    "no warm-up step", never "no time limit" (sysbench --time=0 runs forever). A run that goes well past its planned end
    is stopped with a warning.

    mode = continuous                   ; a workload the agent does not drive (always running, reconnects after a restart):
                                        ; no run / reset; duration = the measured window; latency goals usually
                                        ; (session start --latency-stat p99); min_qps / min_connections: the load is
                                        ; running (20 / 4); reconnect_max: seconds its client may take to come back
                                        ; after a restart (3900)

    [requires]                          ; benchmark prerequisites: set before every run (numbers are minimums), never tuned
    max_connections = 1100

    [fixed]                             ; settings fixed for the session: applied before the runs, never tuned
    innodb_buffer_pool_size = 4G        ; e.g. an I/O-bound load on a small data set
    innodb_redo_log_capacity = 2G
"""
import configparser
import math
import os
import re
import subprocess
import time


class Profile:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        if not cp.read(path):
            raise FileNotFoundError(path)
        b = cp["bench"]
        self.path = path
        self.name = b.get("name", os.path.basename(path))
        self.reset = b.get("reset")
        # apples to apples: every run starts from the same state. Without a reset command (read-only or seasoned data)
        # the agent restarts mysqld and drops the OS page cache before each run, so no run inherits the previous one's
        # caches, swapped-out memory or fragmentation. restart_before_run = false keeps the state (opt-out, stated).
        self.restart_before_run = str(b.get("restart_before_run", "true")).lower() not in ("false", "0", "no", "off")
        # mode = continuous: a workload the agent does not drive (a third-party benchmark that runs all the time and reconnects
        # after a restart, e.g. Parma Ham): no run / reset commands; autotune measures windows of the live load and applies
        # online changes in A-B-A order, restarts only for a final validation (continuous.py)
        self.mode = str(b.get("mode", "driven")).strip().lower()
        self.run = b.get("run", "") if self.mode == "continuous" else b["run"]
        # continuous: what counts as the load running, and how long its client may take to come back after a restart
        # (Parma Ham's HammerDB users die with the connection; the next hourly run brings the load back - up to ~61 min)
        self.min_qps = float(b.get("min_qps", "20"))
        self.min_connections = int(b.get("min_connections", "4"))
        self.reconnect_max = int(b.get("reconnect_max", "3900"))
        # run length: warm-up and measured window. Not set (or "auto"): the session finds them once with a run-length probe
        # (warm-up until every state metric settles, a window long enough for the noise target); numbers are used as given
        w, d = b.get("warmup"), b.get("duration")
        self.auto_warmup = w is None or str(w).strip().lower() == "auto"
        self.auto_duration = d is None or str(d).strip().lower() == "auto"
        self.warmup = "600" if self.auto_warmup else w  # placeholders until the probe has run
        self.duration = 600 if self.auto_duration else int(d)
        self.run_length_max = int(b.get("run_length_max", "3600"))
        self.noise_target = float(b["noise_target"]) if b.get("noise_target") else None
        self.timeout = int(b.get("timeout", str(max(self.duration * 3, 1800))))
        self.goal_metric = b.get("goal_metric")
        self.goal_scale = float(b.get("goal_scale", "1"))
        self.metrics = dict(cp["metrics"]) if cp.has_section("metrics") else {}
        # settings the benchmark needs just to run (e.g. max_connections for 1024 client threads): applied before every
        # run, including the server-defaults run; numeric values are minimums. Never tuned, shown apart from tuning.
        self.requires = dict(cp["requires"]) if cp.has_section("requires") else {}
        # settings the benchmark fixes for the whole session (user, 2026-10-26: tune with a fixed buffer pool to force an I/O-
        # bound load on a small data set, or with a fixed redo size): applied before the runs - restart-only ones too, through
        # the option file - and never tuned, sized by the baseline or changed by an experiment
        self.fixed = {k: v.strip() for k, v in cp["fixed"].items()} if cp.has_section("fixed") else {}
        ds = b.get("data_state", "").strip().lower()
        self.data_state = ds if ds in ("reset", "seasoned", "read-only") else ("reset" if self.reset else "unspecified")
        self.season_hours = float(b.get("season_hours", "12" if self.data_state == "seasoned" else "0"))
        self.season_min_hours = float(b.get("season_min_hours", str(min(1.0, self.season_hours))))
        self.season_slope = float(b.get("season_slope_pct_per_hour", "0.5"))  # steady: |trend| below this
        self.adaptive = b.get("adaptive_duration", "auto").lower() not in ("off", "false", "no", "0")
        # copy_on_write = intended: the data lives on a copy-on-write file system on purpose (ZFS / btrfs in production) and
        # its cost is part of what is measured - the agent records it as a note instead of warning / counting it as noise.
        # A reflink / snapshot clone made by the data restore stays a harness artifact either way.
        self.copy_on_write = b.get("copy_on_write", "avoid").strip().lower()
        self.max_duration = int(b["max_duration"]) if b.get("max_duration") else None
        # shape of a run (user, 2026-10-26): window - one continuous process ({total}), the window measured inside it;
        # split - the benchmark splits warm-up and measurement itself ({warmup} + {duration}, its report covers the
        # measured part); passes - fixed work (a query set, a batch), the goal is its elapsed time: cold (each measured
        # pass from a restart and empty caches) or warm (passes repeated until the pass time is steady, then one measured)
        sh = str(b.get("shape", "")).strip().lower()
        if sh not in ("window", "split", "passes"):
            sh = ("passes" if self.duration == 0 and not self.auto_duration else "window" if "{total" in self.run
                  else "split" if "{warmup" in self.run and "{duration" in self.run else "window")
        self.shape = sh
        pv = str(b.get("passes", "cold")).strip().lower()
        self.passes = pv if pv in ("cold", "warm") else "cold"
        wp = str(b.get("warm_passes", "auto")).strip().lower()
        self.warm_passes = int(wp) if wp.isdigit() else None  # None: until the pass time is steady (at most WARM_PASSES_MAX)
        self.base_duration = self.duration
        self.base_warmup = self.warmup
        # probe = false (default) | auto (calibration + after unusual steal) | always: the ~20 s sysbench cpu / fio micro-benchmarks
        # before a run. Off by default (user, 2026-10-26): steal (judged by consistency), the CPU clock, the quiet check before
        # each run and the run's own disk latencies show a changed environment without spending time on it, and on a VM with
        # steal fio's fsync rate mostly measured its own noise (session 372: an 'environment shift' of +22% fsync/s)
        pv = b.get("probe", "false").lower()
        self.probe = "always" if pv == "always" else (pv in ("auto", "true", "yes", "1"))
        # quiet_check = true (default): ~10 s with the benchmark stopped before each run - is the host idle? (outside work
        # such as a RAID resync is flagged on the run)
        self.quiet_check = str(b.get("quiet_check", "true")).strip().lower() in ("true", "yes", "1", "on")

    PLACEHOLDERS = ("{duration}", "{warmup}", "{total}", "{duration_min}", "{warmup_min}", "{total_min}")

    def shape_check(self):
        """What the run shape means for the goal: [(level, text)] - 'error' when the goal cannot cover the measured part,
        'note' for what to know (user, 2026-10-26)."""
        out = []
        gm = self.goal_metric
        spec = self.metrics.get(gm, "") if gm else ""
        if self.shape == "window" and gm and not spec.startswith("window:") and not gm.startswith("sysbench_") and self.duration > 0:
            out.append(("error", f"the goal {gm} comes from the benchmark's own report, which in one continuous run covers the warm-up "
                                 "too. Use a goal the server measures (no goal_metric), interval reports restricted to the window "
                                 f"({gm} = window:<regex with (time) and (value)>), or let the benchmark split warm-up and measurement "
                                 "itself ({warmup} + {duration})."))
        if self.shape == "split":
            out.append(("note", "{warmup} and {duration} go to the benchmark, which must measure only the second part in ONE process "
                                "(sysbench --warmup-time, HammerDB rampup + duration). A wrapper that starts a second process for the "
                                "measurement measures a cold client (run 5251): use {total} and one invocation instead."))
        if self.shape == "passes" and self.passes == "warm":
            out.append(("note", "warm passes: the pass is repeated until its time is steady, then measured once"
                                + (f" ({self.warm_passes} warm-up passes)" if self.warm_passes is not None else "")))
        return out

    def can_resize(self):
        """Run length can follow a duration change: fixed-length measurement and a run command that uses it."""
        return self.duration > 0 and str(self.warmup).isdigit() and any(p in self.run for p in self.PLACEHOLDERS)

    def command(self, total_s=None):
        """The run command; total_s: one continuous run of that length instead (no separate warm-up), for seasoning."""
        w = int(self.warmup) if str(self.warmup).isdigit() else 0
        d = self.duration
        if total_s:
            w, d = 0, int(total_s)
        vals = {"duration": d, "warmup": w, "total": w + d + 30,
                "duration_min": max(1, -(-d // 60)), "warmup_min": -(-w // 60), "total_min": -(-(w + d + 30) // 60)}
        cmd = self.run
        for k, v in vals.items():
            cmd = cmd.replace("{" + k + "}", str(v))
        return cmd

    def describe(self):
        return {"name": self.name, "profile": self.path, "reset": self.reset, "restart_before_run": self.restart_before_run,
                "run": self.run, "warmup": self.warmup,
                "duration": self.duration, "goal_metric": self.goal_metric, "metrics": list(self.metrics), "command": self.command(),
                "data_state": self.data_state, "copy_on_write": self.copy_on_write,
                "requires": self.requires}

    def spec(self):
        """What actually ran, for the record: describe() plus the profile file as written, the contents of scripts the
        run and reset commands name (text files up to 32 KiB each, 96 KiB in all) and a fingerprint of all of it, so a
        session shows which benchmark - and which version of it - each run used."""
        import hashlib
        import json
        out = self.describe()
        try:
            out["profile_text"] = open(self.path).read()[:32768] if self.path else None
        except OSError:
            out["profile_text"] = None
        scripts, total = {}, 0
        for cmd in (self.command(), self.reset or ""):
            for tok in re.findall(r"(?:^|[\s=;&|(])((?:/|\./)[\w./-]+\.(?:sh|tcl|lua|py|sql|ini|cnf|js))", cmd):
                path = tok if tok.startswith("/") else os.path.join(os.path.dirname(self.path or "."), tok[2:])
                if path in scripts or not os.path.isfile(path):
                    continue
                try:
                    text = open(path, errors="replace").read(32768)
                except OSError:
                    continue
                if total + len(text) > 98304:
                    break
                scripts[path] = text
                total += len(text)
        out["scripts"] = scripts
        out["phases"] = {"warmup_s": int(self.warmup) if str(self.warmup).isdigit() else None,
                         "measured_s": self.duration or None, "window": self.window()}
        out["spec_sha"] = hashlib.sha256(json.dumps({k: out.get(k) for k in ("command", "reset", "profile_text", "scripts")},
                                                    sort_keys=True).encode()).hexdigest()[:12]
        return out

    def window(self):
        """(start, end) seconds of the measured window from the benchmark's own start (warm-up, then duration)."""
        try:
            w = int(self.warmup)
        except (TypeError, ValueError):
            return None
        return (w, w + self.duration) if self.duration > 0 else None

    def parse(self, text):
        out = {}
        win = self.window()
        for name, spec in self.metrics.items():
            mode, _, rx = spec.partition(":") if spec.split(":", 1)[0] in ("sum", "geomean", "count", "last", "max", "window") else ("last", "", spec)
            if mode == "window":
                # (time, value) pairs, e.g. sysbench interval lines: the mean of the intervals inside the measured window
                pairs = []
                for m in re.finditer(rx.strip(), text, re.M):
                    try:
                        pairs.append((float(m.group(1)), float(m.group(2))))
                    except (ValueError, IndexError):
                        pass
                inside = [v for t, v in pairs if win and win[0] < t <= win[1]]
                if inside:
                    out[name] = round(sum(inside) / len(inside), 4)
                continue
            vals = []
            for m in re.finditer(rx.strip(), text):
                try:
                    vals.append(float(m.group(1)) if m.groups() else 1.0)
                except (ValueError, IndexError):
                    pass
            if not vals:
                continue
            if mode == "sum":
                out[name] = round(sum(vals), 4)
            elif mode == "count":
                out[name] = len(vals)
            elif mode == "max":
                out[name] = max(vals)
            elif mode == "geomean":
                pos = [v for v in vals if v > 0]
                out[name] = round(math.exp(sum(math.log(v) for v in pos) / len(pos)), 4) if pos else None
            else:
                out[name] = vals[-1]
        # sysbench's own summary covers the whole run (warm-up included): with interval reports, the goal comes from
        # the intervals inside the measured window (C2: a whole-run 3,135 vs 3,750 measured by the server)
        if "sysbench_tps" in out and "sysbench_tps" not in [n for n, sp in self.metrics.items() if sp.startswith("window:")] and win:
            iv = [(float(m.group(1)), float(m.group(2))) for m in re.finditer(r"^\[ *(\d+)s \].*?tps: ([\d.]+)", text, re.M)]
            inside = [v for t, v in iv if win[0] < t <= win[1]]
            if len(inside) >= 3:
                out["sysbench_tps_whole_run"] = out["sysbench_tps"]
                out["sysbench_tps"] = round(sum(inside) / len(inside), 4)
        return out


# processes of a benchmark run that only launch or plumb the real tool (their command lines are the harness, not the benchmark)
HELPERS = {"bash", "sh", "dash", "zsh", "timeout", "sleep", "tee", "env", "nohup", "setsid", "cat", "grep", "awk", "sed", "xargs",
           "time", "nice", "ionice", "taskset", "numactl", "stdbuf", "sudo", "su", "flock", "head", "tail", "date", "wait"}
SECRET = re.compile(r"pass|pwd|secret|token|key", re.I)


def mask(s):
    """Passwords out of command lines and scripts (--mysql-password=x, -pX, 'diset ... mysql_pass x', PASSWORD=x)."""
    s = re.sub(r"(--?[\w-]*pass(?:word)?[= ])\S+", r"\1***", s, flags=re.I)
    s = re.sub(r"(?m)(\b\w*pass(?:word)?\w*\s*[=:]?\s+)(\S+)\s*$", r"\1***", s, flags=re.I)
    s = re.sub(r"(\b\w*(?:PASSWORD|PASSWD|PWD)\w*=)\S+", r"\1***", s)
    return s


def tool_version(exe):
    """A known tool's version: sysbench / pgbench / mysqlslap report it; HammerDB is named by its install directory."""
    m = re.search(r"HammerDB-([\d.]+)", exe)
    if m:
        return "HammerDB " + m.group(1)
    if os.path.basename(exe) in ("sysbench", "pgbench", "mysqlslap", "go-tpc", "ycsb"):
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
            return (r.stdout or r.stderr).strip().splitlines()[0][:80]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return None
    return None


def run_shell(cmd, timeout, log=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, preexec_fn=_raise_nofile)
    if log:
        with open(log, "a") as f:
            f.write(f"$ {cmd}\n{r.stdout}{r.stderr}\n")
    return r.returncode, (r.stdout + r.stderr)[-2000:]


def _proc_start(stat):
    """A process's start time (epoch seconds) from its /proc/<pid>/stat line, or None."""
    try:
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        btime = next(int(x.split()[1]) for x in open("/proc/stat") if x.startswith("btime"))
        return round(btime + ticks / os.sysconf("SC_CLK_TCK"), 1)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


BENCH_NOFILE = 1048576


def _raise_nofile():
    """Benchmarks open a socket (and more) per client connection: the default 1024 open files made a 1024-thread sysbench
    fail with "Can't create UNIX socket (24)", then misleadingly "Cannot find benchmark" (canary, 2026-10-25). Run commands
    get the hard limit (raised to BENCH_NOFILE when the agent may)."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for want in ((BENCH_NOFILE, BENCH_NOFILE), (hard, hard)):
        try:
            if want[0] == resource.RLIM_INFINITY or want[0] > soft:
                resource.setrlimit(resource.RLIMIT_NOFILE, want)
            return
        except (ValueError, OSError):
            continue


class Running:
    """A benchmark run command in the background."""

    def __init__(self, cmd, logfile):
        self.logfile = logfile
        self.fh = open(logfile, "w")
        self.p = subprocess.Popen(cmd, shell=True, stdout=self.fh, stderr=subprocess.STDOUT, start_new_session=True,
                                  preexec_fn=_raise_nofile)
        self.t0 = time.time()

    def alive(self):
        return self.p.poll() is None

    def wait(self, timeout):
        try:
            self.p.wait(timeout=max(1, timeout))
        except subprocess.TimeoutExpired:
            self.kill()
        self.fh.close()
        return self.p.returncode

    def kill(self):
        import signal
        try:
            os.killpg(self.p.pid, signal.SIGTERM)
            time.sleep(3)
            if self.p.poll() is None:
                os.killpg(self.p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def observe(self):
        """The benchmark as it actually runs, whatever wrapper scripts launched it: every process of the run's session
        (start_new_session) that is a tool, not a shell or helper - its full command line, executable, version, the
        script files it was given (e.g. HammerDB's Tcl) and the environment variables those scripts read. Passwords are
        masked. A list of {argv, exe, cwd, version, scripts, env}."""
        out, seen = [], set()
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                st = open(f"/proc/{d}/stat").read()
                if int(st[st.rindex(")") + 2:].split()[3]) != self.p.pid:  # session id
                    continue
                argv = [a.decode(errors="replace") for a in open(f"/proc/{d}/cmdline", "rb").read().split(b"\0") if a]
                exe = os.readlink(f"/proc/{d}/exe")
                cwd = os.readlink(f"/proc/{d}/cwd")
                env = dict(x.decode(errors="replace").split("=", 1) for x in open(f"/proc/{d}/environ", "rb").read().split(b"\0") if b"=" in x)
            except (OSError, ValueError, IndexError):
                continue
            if not argv or os.path.basename(exe).rstrip("0123456789.") in HELPERS or tuple(argv) in seen:
                continue
            seen.add(tuple(argv))
            scripts = {}
            for a in argv[1:]:
                path = a if a.startswith("/") else os.path.join(cwd, a)
                if re.search(r"\.(tcl|lua|py|sql|js|sh|ini|cfg|conf|xml|json|yaml|yml)$", a) and os.path.isfile(path):
                    try:
                        scripts[path] = mask(open(path, errors="replace").read(32768))
                    except OSError:
                        pass
            text = " ".join(scripts.values())
            used = {k: v for k, v in env.items() if re.search(r"\b" + re.escape(k) + r"\b", text) and not SECRET.search(k)}
            used.update({k: v for k, v in env.items() if k.startswith(("HDB_", "SB_", "SYSBENCH_", "BENCH_")) and not SECRET.search(k)})
            out.append({"argv": [mask(a) for a in argv], "exe": exe, "cwd": cwd, "version": tool_version(exe),
                        "scripts": scripts, "env": dict(sorted(used.items())), "started_at": _proc_start(st)})
        return out

    def output(self):
        try:
            with open(self.logfile) as f:
                return f.read()
        except OSError:
            return ""


# ---- environment probe ------------------------------------------------------------------
def _run_json(cmd, timeout=60):
    import json as _json
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return _json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip().startswith("{") else None


def probe(datadir, log=print):
    """~20 s of micro-benchmarks with the database idle: single-thread CPU speed and storage IOPS / fsync rate on the
    data filesystem. Comparing probes between runs separates "the VM got slower" from "the configuration got worse"."""
    import shutil
    out = {}
    if shutil.which("sysbench"):
        r = subprocess.run(["sysbench", "cpu", "--threads=1", "--time=5", "run"], capture_output=True, text=True, timeout=30)
        m = re.search(r"events per second:\s+([\d.]+)", r.stdout)
        if m:
            out["cpu_events_s"] = float(m.group(1))
    if not shutil.which("fio") and shutil.which("apt-get"):
        subprocess.run(["apt-get", "install", "-yq", "fio"], capture_output=True, timeout=300)
    if shutil.which("fio"):
        f = os.path.join(datadir, ".tuning-agent-probe")
        base = ["fio", "--name=probe", f"--filename={f}", "--size=256M", "--direct=1", "--time_based", "--runtime=5",
                "--output-format=json", "--ioengine=libaio"]
        try:
            for name, extra, key in (("read_iops", ["--rw=randread", "--bs=4k", "--iodepth=16"], "read"),
                                     ("write_iops", ["--rw=randwrite", "--bs=4k", "--iodepth=16"], "write"),
                                     ("fsync_per_s", ["--rw=write", "--bs=4k", "--iodepth=1", "--fsync=1"], "write")):
                j = _run_json(base + extra)
                if j:
                    out[name] = round(j["jobs"][0][key]["iops"], 1)
        finally:
            try:
                os.remove(f)
            except OSError:
                pass
    return out
