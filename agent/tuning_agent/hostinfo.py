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


PSI_RES = ("cpu", "memory", "io")


def read_psi(base):
    """Cumulative stall time (microseconds) from a PSI file set: {('cpu','some'): total, ...}."""
    out = {}
    for res in PSI_RES:
        for line in _read(f"{base}/{res}.pressure" if base != "/proc/pressure" else f"{base}/{res}").splitlines():
            parts = line.split()
            if parts and parts[0] in ("some", "full"):
                for kv in parts[1:]:
                    if kv.startswith("total="):
                        out[f"{res}_{parts[0]}"] = int(kv[6:])
    return out


def cgroup_dir(pid):
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        if line.startswith("0::"):
            path = "/sys/fs/cgroup" + line[3:].strip()
            if os.path.exists(path + "/cpu.pressure"):
                return path
    return None


def mysqld_pid():
    for name in ("mysqld", "mariadbd"):
        for pid in os.listdir("/proc"):
            if pid.isdigit() and _read(f"/proc/{pid}/comm").strip() == name:
                return int(pid)
    return None


SMAPS_KEYS = {"Pss": "pss", "Pss_Anon": "pss_anon", "Pss_File": "pss_file", "Pss_Shmem": "pss_shmem", "SwapPss": "swap_pss"}
STATUS_KEYS = {"VmRSS": "rss", "VmSize": "vsz", "VmSwap": "swap", "RssShmem": "rss_shmem"}


def service_pids(pid, cgroup=None, limit=1024):
    """All processes of the database service: the cgroup members (a PostgreSQL postmaster plus its backends and
    workers), or the process with its descendants when it is not in a dedicated cgroup."""
    if cgroup and not cgroup.rstrip("/").endswith((".slice", "/sys/fs/cgroup")):
        pids = [int(x) for x in _read(os.path.join(cgroup, "cgroup.procs")).split() if x.isdigit()]
        if pid in pids:
            return pids[:limit]
    children, todo, out = {}, [pid], []
    for d in os.listdir("/proc"):
        if d.isdigit():
            st = _read(f"/proc/{d}/stat")
            if st:
                children.setdefault(int(st.rsplit(")", 1)[1].split()[1]), []).append(int(d))
    while todo and len(out) < limit:
        p = todo.pop()
        out.append(p)
        todo.extend(children.get(p, []))
    return out


def process_memory(pids):
    """Memory of a process group in bytes. RSS / VSZ are summed per process, so pages shared between processes
    (shared buffers, libraries) count once per process; PSS splits each shared page between the processes mapping it,
    so the PSS total is the group's real share of RAM. SwapPss does the same for swapped-out pages."""
    tot = {v: 0 for v in list(SMAPS_KEYS.values()) + list(STATUS_KEYS.values())}
    n = 0
    for pid in pids:
        roll = _read(f"/proc/{pid}/smaps_rollup")  # needs root (or the same user) for other processes
        if not roll:
            continue
        n += 1
        for line in roll.splitlines():
            k, _, v = line.partition(":")
            if k in SMAPS_KEYS:
                tot[SMAPS_KEYS[k]] += int(v.split()[0]) * 1024
        for line in _read(f"/proc/{pid}/status").splitlines():
            k, _, v = line.partition(":")
            if k in STATUS_KEYS:
                tot[STATUS_KEYS[k]] += int(v.split()[0]) * 1024
    if not n:
        return None
    tot["procs"] = n
    return tot


def cgroup_memory(cgroup):
    out = {}
    for f, k in (("memory.current", "cg_mem"), ("memory.swap.current", "cg_swap")):
        v = _read(os.path.join(cgroup, f)).strip()
        if v.isdigit():
            out[k] = int(v)
    for line in _read(os.path.join(cgroup, "memory.stat")).splitlines():
        k, _, v = line.partition(" ")
        if k in ("anon", "file", "shmem") and v.isdigit():
            out["cg_" + k] = int(v)
    return out


class OSCounters:
    """Takes raw counters; rates computed between two snapshots."""

    def __init__(self, device=None, mysql_port=3306, role="server"):
        self.device = device
        self.pid = None
        self.mysql_port = mysql_port
        self.role = role

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
        if not self.pid or not os.path.exists(f"/proc/{self.pid}"):
            self.pid = mysqld_pid()
            self.cgroup = cgroup_dir(self.pid) if self.pid else None
        s["psi"] = read_psi("/proc/pressure")
        if getattr(self, "cgroup", None):
            s["psi_mysqld"] = read_psi(self.cgroup)
        if self.pid:
            st = _read(f"/proc/{self.pid}/stat")
            if st:
                f = st.rsplit(")", 1)[1].split()
                s["mysqld"] = (self.pid, int(f[11]) + int(f[12]))  # utime + stime (ticks)
                s["mysqld_rss"] = int(f[21]) * os.sysconf("SC_PAGE_SIZE")
            # PSS needs a page-table walk per process: every 30 s is plenty for a memory gauge
            if time.time() - getattr(self, "_mem_t", 0) >= 30 or getattr(self, "_mem_pid", None) != self.pid:
                self._mem_t, self._mem_pid = time.time(), self.pid
                try:
                    self._mem = process_memory(service_pids(self.pid, self.cgroup))
                    if self._mem is not None and self.cgroup:
                        self._mem.update(cgroup_memory(self.cgroup))
                except Exception:  # best effort
                    self._mem = None
            if getattr(self, "_mem", None):
                s["dbmem"] = self._mem
        la = _read("/proc/loadavg").split()
        s["load1"] = float(la[0]) if la else 0.0
        vm = {}
        for line in _read("/proc/vmstat").splitlines():
            k, _, v = line.partition(" ")
            if k in ("pswpin", "pswpout", "pgmajfault", "oom_kill"):
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
        try:
            from . import netmon
            s["netmon"] = netmon.snapshot()
            s["ss"] = netmon.ss_probe(self.mysql_port, self.role)
        except Exception:  # network observability is best effort
            pass
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
        if "mysqld" in a and "mysqld" in b and a["mysqld"][0] == b["mysqld"][0]:
            ticks = os.sysconf("SC_CLK_TCK")
            out["mysqld_cpu_pct"] = round(100.0 * (b["mysqld"][1] - a["mysqld"][1]) / ticks / dt / (os.cpu_count() or 1), 2)
        if "mysqld_rss" in b:
            out["mysqld_rss_mb"] = round(b["mysqld_rss"] / 2 ** 20, 1)
        for k, v in (b.get("dbmem") or {}).items():
            out["dbproc_procs" if k == "procs" else f"dbproc_{k}_mb"] = v if k == "procs" else round(v / 2 ** 20, 1)
        # pressure stall information: % of wall time some (or all) tasks were stalled on the resource
        for key, prefix in (("psi", "psi_"), ("psi_mysqld", "mysqld_psi_")):
            pa, pb = a.get(key) or {}, b.get(key) or {}
            for k, v in pb.items():
                if k in pa and v >= pa[k] and k != "cpu_full":
                    out[prefix + k] = round(min(100.0, (v - pa[k]) / dt / 1e4), 2)
        if "ctxt" in a:
            out["ctx_switches"] = round((b["ctxt"] - a["ctxt"]) / dt, 1)
        out["procs_running"] = b.get("procs_running", 0)
        out["procs_blocked"] = b.get("procs_blocked", 0)
        out["load1"] = b.get("load1", 0)
        va, vb = a.get("vm", {}), b.get("vm", {})
        for k, name in (("pswpin", "swap_in"), ("pswpout", "swap_out"), ("pgmajfault", "major_faults")):
            if k in va and k in vb:
                out[name] = round((vb[k] - va[k]) / dt, 2)
        if "oom_kill" in va and "oom_kill" in vb:
            out["oom_kills"] = vb["oom_kill"] - va["oom_kill"]
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
        if a.get("netmon") and b.get("netmon"):
            from . import netmon
            out.update(netmon.rates(a["netmon"], b["netmon"], dt))
        out.update({k: v for k, v in (b.get("ss") or {}).items()})
        return out
