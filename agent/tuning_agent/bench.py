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
        self.run = b["run"]
        self.warmup = b.get("warmup", "0")
        self.duration = int(b.get("duration", "300"))
        self.timeout = int(b.get("timeout", str(max(self.duration * 3, 1800))))
        self.goal_metric = b.get("goal_metric")
        self.goal_scale = float(b.get("goal_scale", "1"))
        self.metrics = dict(cp["metrics"]) if cp.has_section("metrics") else {}

    def describe(self):
        return {"name": self.name, "profile": self.path, "reset": self.reset, "run": self.run, "warmup": self.warmup,
                "duration": self.duration, "goal_metric": self.goal_metric, "metrics": list(self.metrics)}

    def parse(self, text):
        out = {}
        for name, spec in self.metrics.items():
            mode, _, rx = spec.partition(":") if spec.split(":", 1)[0] in ("sum", "geomean", "count", "last", "max") else ("last", "", spec)
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
