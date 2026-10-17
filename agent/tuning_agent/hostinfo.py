"""Host hardware facts and OS metric counters from /proc and /sys (Linux)."""
import os
import platform
import re
import shutil
import socket
import time


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def meminfo():
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[k] = int(parts[0]) * 1024
    return out


def cpu_model():
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def os_release():
    d = {}
    for line in _read("/etc/os-release").splitlines():
        k, _, v = line.partition("=")
        d[k] = v.strip('"')
    return d.get("PRETTY_NAME", platform.platform())


def device_for_path(path):
    """Block device (e.g. 'sda', 'nvme0n1') that holds path."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    link = f"/sys/dev/block/{major}:{minor}"
    if not os.path.exists(link):
        return None
    real = os.path.realpath(link)
    name = os.path.basename(real)
    # partition -> parent disk
    if os.path.exists(os.path.join(real, "partition")):
        name = os.path.basename(os.path.dirname(real))
    # device-mapper -> first slave
    slaves = f"/sys/block/{name}/slaves"
    if os.path.isdir(slaves):
        s = os.listdir(slaves)
        if s:
            base = s[0]
            par = os.path.realpath(f"/sys/class/block/{base}")
            if os.path.exists(os.path.join(par, "partition")):
                base = os.path.basename(os.path.dirname(par))
            return name if not base else base
    return name


def mount_fs(path):
    best, fs, opts = "", "", ""
    for line in _read("/proc/mounts").splitlines():
        p = line.split()
        if len(p) >= 4 and path.startswith(p[1]) and len(p[1]) > len(best):
            best, fs, opts = p[1], p[2], p[3]
    return best, fs, opts


def hardware(datadir="/var/lib/mysql"):
    mi = meminfo()
    dev = device_for_path(datadir) or device_for_path("/")
    rot = _read(f"/sys/block/{dev}/queue/rotational", "0").strip() if dev else "0"
    sched = _read(f"/sys/block/{dev}/queue/scheduler", "").strip() if dev else ""
    m = re.search(r"\[(\w+[-\w]*)\]", sched)
    try:
        du = shutil.disk_usage(datadir)
    except OSError:
        du = shutil.disk_usage("/")
    mnt, fs, opts = mount_fs(datadir)
    virt = _read("/sys/class/dmi/id/sys_vendor").strip() or None
    return {
        "hostname": socket.gethostname(),
        "cpus": os.cpu_count(),
        "cpu_model": cpu_model(),
        "ram_bytes": mi.get("MemTotal", 0),
        "swap_bytes": mi.get("SwapTotal", 0),
        "os": os_release(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "vendor": virt,
        "datadir": datadir,
        "data_device": dev,
        "disk_rotational": rot == "1",
        "disk_scheduler": m.group(1) if m else sched,
        "disk_total_bytes": du.total,
        "disk_free_bytes": du.free,
        "filesystem": fs,
        "mount": mnt,
        "mount_options": opts,
        "thp": (re.search(r"\[(\w+)\]", _read("/sys/kernel/mm/transparent_hugepage/enabled")) or [None, None])[1],
        "swappiness": _read("/proc/sys/vm/swappiness").strip(),
    }


class OSCounters:
    """Takes raw counters; rates computed between two snapshots."""

    def __init__(self, device=None):
        self.device = device

    def snapshot(self):
        s = {"t": time.time()}
        cpu = _read("/proc/stat").splitlines()
        for line in cpu:
            if line.startswith("cpu "):
                vals = [int(x) for x in line.split()[1:]]
                vals += [0] * (10 - len(vals))
                s["cpu"] = vals  # user nice system idle iowait irq softirq steal guest guest_nice
            elif line.startswith("ctxt "):
                s["ctxt"] = int(line.split()[1])
            elif line.startswith("procs_running"):
                s["procs_running"] = int(line.split()[1])
            elif line.startswith("procs_blocked"):
                s["procs_blocked"] = int(line.split()[1])
        la = _read("/proc/loadavg").split()
        s["load1"] = float(la[0]) if la else 0.0
        vm = {}
        for line in _read("/proc/vmstat").splitlines():
            k, _, v = line.partition(" ")
            if k in ("pswpin", "pswpout", "pgmajfault"):
                vm[k] = int(v)
        s["vm"] = vm
        mi = meminfo()
        s["mem"] = {k: mi.get(k, 0) for k in ("MemTotal", "MemAvailable", "Cached", "SwapTotal", "SwapFree", "Dirty")}
        if self.device:
            for line in _read("/proc/diskstats").splitlines():
                p = line.split()
                if len(p) >= 14 and p[2] == self.device:
                    s["disk"] = [int(x) for x in p[3:14]]
                    break
        rx = tx = 0
        for line in _read("/proc/net/dev").splitlines()[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == "lo":
                continue
            p = rest.split()
            if len(p) >= 9:
                rx += int(p[0])
                tx += int(p[8])
        s["net"] = (rx, tx)
        return s

    @staticmethod
    def rates(a, b):
        dt = max(b["t"] - a["t"], 1e-6)
        out = {}
        if "cpu" in a and "cpu" in b:
            d = [y - x for x, y in zip(a["cpu"], b["cpu"])]
            tot = sum(d[:8]) or 1
            out["cpu_user"] = round(100.0 * (d[0] + d[1]) / tot, 2)
            out["cpu_sys"] = round(100.0 * (d[2] + d[5] + d[6]) / tot, 2)
            out["cpu_iowait"] = round(100.0 * d[4] / tot, 2)
            out["cpu_steal"] = round(100.0 * d[7] / tot, 2)
            out["cpu_idle"] = round(100.0 * d[3] / tot, 2)
            out["cpu_busy"] = round(100.0 - out["cpu_idle"] - out["cpu_iowait"], 2)
        if "ctxt" in a:
            out["ctx_switches"] = round((b["ctxt"] - a["ctxt"]) / dt, 1)
        out["procs_running"] = b.get("procs_running", 0)
        out["procs_blocked"] = b.get("procs_blocked", 0)
        out["load1"] = b.get("load1", 0)
        va, vb = a.get("vm", {}), b.get("vm", {})
        for k, name in (("pswpin", "swap_in"), ("pswpout", "swap_out"), ("pgmajfault", "major_faults")):
            if k in va and k in vb:
                out[name] = round((vb[k] - va[k]) / dt, 2)
        m = b.get("mem", {})
        if m.get("MemTotal"):
            out["mem_used_pct"] = round(100.0 * (1 - m["MemAvailable"] / m["MemTotal"]), 2)
            out["mem_available_mb"] = round(m["MemAvailable"] / 2 ** 20, 1)
            out["page_cache_mb"] = round(m["Cached"] / 2 ** 20, 1)
            out["swap_used_mb"] = round((m["SwapTotal"] - m["SwapFree"]) / 2 ** 20, 1)
        if "disk" in a and "disk" in b:
            d = [y - x for x, y in zip(a["disk"], b["disk"])]
            # fields: reads, rmerged, rsect, rms, writes, wmerged, wsect, wms, inflight, io_ms, wio_ms
            ios = d[0] + d[4]
            out["disk_r_iops"] = round(d[0] / dt, 1)
            out["disk_w_iops"] = round(d[4] / dt, 1)
            out["disk_r_mbs"] = round(d[2] * 512 / dt / 2 ** 20, 2)
            out["disk_w_mbs"] = round(d[6] * 512 / dt / 2 ** 20, 2)
            out["disk_util"] = round(min(100.0, d[9] / (dt * 10.0)), 1)
            out["disk_await_ms"] = round((d[3] + d[7]) / ios, 2) if ios else 0.0
            out["disk_queue"] = b["disk"][8]
        if "net" in a:
            out["net_rx_mbs"] = round((b["net"][0] - a["net"][0]) / dt / 2 ** 20, 3)
            out["net_tx_mbs"] = round((b["net"][1] - a["net"][1]) / dt / 2 ** 20, 3)
        return out
