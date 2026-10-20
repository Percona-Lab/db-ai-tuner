"""Off-CPU diagnostic probes (eBPF via bpftrace) over the measured window.

The CPU profile shows where mysqld burns CPU, performance_schema shows instrumented waits. Some costs are neither: a
thread blocked on a reverse DNS lookup during connect sits off-CPU in libc, outside any performance_schema wait. These
probes measure such time directly, so a change that acts on it is tried because the time is there, not because a
counter crossed a threshold.

Probes (each one: calls, total wall time, max):
* name resolution - libc getnameinfo / getaddrinfo / gethostbyaddr_r called by mysqld (reverse DNS on connect when
  skip_name_resolve is OFF and the host cache misses).

Uprobes cost only when the probed function is called, so they run over the whole measured window. Needs bpftrace and
root; when unavailable the measurement is simply missing (the breakdown does not invent it).
"""
import os
import re
import shutil
import subprocess
import threading
import time

DNS_FUNCS = ("getnameinfo", "getaddrinfo", "gethostbyaddr_r")


def available():
    return bool(shutil.which("bpftrace")) and os.geteuid() == 0


def _libc(pid):
    try:
        for line in open(f"/proc/{pid}/maps"):
            p = line.split()[-1]
            if re.search(r"/libc(\.so\.6|-[\d.]+\.so)$", p):
                return p
    except OSError:
        pass
    return None


def script(pid, libc, seconds):
    probes = ", ".join(f"uprobe:{libc}:{f}" for f in DNS_FUNCS)
    rets = ", ".join(f"uretprobe:{libc}:{f}" for f in DNS_FUNCS)
    return (f"{probes} /pid == {pid}/ {{ @t0[tid] = nsecs; }}\n"
            f"{rets} /pid == {pid} && @t0[tid]/ {{ $d = nsecs - @t0[tid]; @ns[func] = sum($d); @n[func] = count(); "
            f"@mx[func] = max($d); delete(@t0[tid]); }}\n"
            f"interval:s:{int(seconds)} {{ exit(); }}\n"
            "END { clear(@t0); }\n")


_LINE = re.compile(r"^@(ns|n|mx)\[([\w]+)\]:\s*(\d+)")


def parse(out):
    res = {}
    for line in out.splitlines():
        m = _LINE.match(line.strip())
        if m:
            res.setdefault(m.group(2), {})[m.group(1)] = int(m.group(3))
    return res


class Probes:
    """Background bpftrace over the measured window; result() afterwards."""

    def __init__(self, pid, seconds):
        self.pid, self.seconds = pid, max(5, int(seconds))
        self.out = self.err = None
        self.t0 = self.t1 = None
        self._p = None
        self._th = None

    def start(self):
        libc = _libc(self.pid)
        if not libc:
            self.err = "libc not found in the mysqld memory map"
            return self
        self.t0 = time.time()
        self._p = subprocess.Popen(["nice", "-n", "10", "bpftrace", "-e", script(self.pid, libc, self.seconds)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def wait():
            try:
                self.out, err = self._p.communicate(timeout=self.seconds + 120)
                if self._p.returncode != 0:
                    self.err = (err or "").strip()[-300:]
            except Exception as e:  # noqa
                self.err = str(e)[:300]
            self.t1 = time.time()
        self._th = threading.Thread(target=wait, daemon=True)
        self._th.start()
        return self

    def stop(self):
        """End early (a run that finished before the planned window) and wait for the output."""
        if self._p and self._p.poll() is None:
            try:
                self._p.send_signal(2)  # SIGINT: bpftrace prints the maps and exits
            except OSError:
                pass
        if self._th:
            self._th.join(60)

    def result(self):
        if self.err or self.out is None:
            return {"error": self.err or "no probe output"}
        window = max(1e-6, (self.t1 or time.time()) - self.t0)
        m = parse(self.out)
        dns = {f: {"calls": v.get("n", 0), "seconds": round(v.get("ns", 0) / 1e9, 4), "max_ms": round(v.get("mx", 0) / 1e6, 2)}
               for f, v in m.items() if f in DNS_FUNCS}
        total = sum(v["seconds"] for v in dns.values())
        return {"window_s": round(window, 1), "name_resolution": {"calls": sum(v["calls"] for v in dns.values()),
                                                                  "seconds": round(total, 4), "by_function": dns,
                                                                  "threads": round(total / window, 4)}}
