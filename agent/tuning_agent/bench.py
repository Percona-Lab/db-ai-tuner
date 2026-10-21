"""Benchmark profiles: let the agent drive a benchmark end to end.

Profile (INI):

    [bench]
    name = HammerDB TPROC-C 10W, 10 VU
    reset = /root/bench/reset.sh        ; optional: restore the data set before each run (may restart mysqld)
    run = /root/bench/hammerdb.sh       ; started in the background; output is captured
    warmup = 120                        ; seconds after start before measuring, or "auto"
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
    (whole minutes); they follow run-length changes. Without them the run length is fixed.

    [requires]                          ; benchmark prerequisites: set before every run (numbers are minimums), never tuned
    max_connections = 1100
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
        self.run = b["run"]
        self.warmup = b.get("warmup", "0")
        self.duration = int(b.get("duration", "300"))
        self.timeout = int(b.get("timeout", str(max(self.duration * 3, 1800))))
        self.goal_metric = b.get("goal_metric")
        self.goal_scale = float(b.get("goal_scale", "1"))
        self.metrics = dict(cp["metrics"]) if cp.has_section("metrics") else {}
        # settings the benchmark needs just to run (e.g. max_connections for 1024 client threads): applied before every
        # run, including the server-defaults run; numeric values are minimums. Never tuned, shown apart from tuning.
        self.requires = dict(cp["requires"]) if cp.has_section("requires") else {}
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
        self.base_duration = self.duration
        self.base_warmup = self.warmup
        # probe = auto (calibration + after unusual steal; default) | always | false
        pv = b.get("probe", "auto").lower()
        self.probe = "always" if pv == "always" else (pv in ("auto", "true", "yes", "1"))

    PLACEHOLDERS = ("{duration}", "{warmup}", "{total}", "{duration_min}", "{warmup_min}", "{total_min}")

    def can_resize(self):
        """Run length can follow a duration change: fixed-length measurement and a run command that uses it."""
        return self.duration > 0 and str(self.warmup).isdigit() and any(p in self.run for p in self.PLACEHOLDERS)

    def command(self):
        w = int(self.warmup) if str(self.warmup).isdigit() else 0
        vals = {"duration": self.duration, "warmup": w, "total": w + self.duration + 30,
                "duration_min": max(1, -(-self.duration // 60)), "warmup_min": -(-w // 60), "total_min": -(-(w + self.duration + 30) // 60)}
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


def run_shell(cmd, timeout, log=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if log:
        with open(log, "a") as f:
            f.write(f"$ {cmd}\n{r.stdout}{r.stderr}\n")
    return r.returncode, (r.stdout + r.stderr)[-2000:]


class Running:
    """A benchmark run command in the background."""

    def __init__(self, cmd, logfile):
        self.logfile = logfile
        self.fh = open(logfile, "w")
        self.p = subprocess.Popen(cmd, shell=True, stdout=self.fh, stderr=subprocess.STDOUT, start_new_session=True)
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
