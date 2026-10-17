"""Operating-system tuning: read, apply live, persist across reboots, restore originals.

Settings are exposed to the rest of the agent as pseudo-variables named "os:<name>", so they flow through the
same managed-settings, run snapshots, config diffs, recommendations and rollback as MySQL variables.
"""
import glob
import os
import re
import shutil
import subprocess

SYSCTL_FILE = "/etc/sysctl.d/99-tuning-agent.conf"
UDEV_FILE = "/etc/udev/rules.d/99-tuning-agent.rules"
BOOT_UNIT = "/etc/systemd/system/tuning-agent-os.service"
JEMALLOC_CANDIDATES = ["/usr/lib/x86_64-linux-gnu/libjemalloc.so.2", "/usr/lib64/libjemalloc.so.2", "/usr/lib/aarch64-linux-gnu/libjemalloc.so.2"]

SYSCTLS = ["vm.swappiness", "vm.dirty_background_bytes", "vm.dirty_bytes", "vm.dirty_ratio", "vm.dirty_background_ratio", "vm.dirty_expire_centisecs", "vm.dirty_writeback_centisecs",
           "kernel.numa_balancing", "kernel.sched_autogroup_enabled", "fs.aio-max-nr"]

# name -> (description, needs mysqld restart, category)
OS_KB = {
    "os:vm.swappiness": ("Kernel preference for swapping anonymous memory (1 = only under real pressure).", False, "memory"),
    "os:vm.dirty_ratio": ("Dirty page cache (% of memory) at which writers are throttled (used when dirty_bytes is 0).", False, "io"),
    "os:vm.dirty_background_ratio": ("Dirty page cache (% of memory) that starts background writeback (used when dirty_background_bytes is 0).", False, "io"),
    "os:vm.dirty_expire_centisecs": ("Age at which dirty page cache must be written back.", False, "io"),
    "os:vm.dirty_writeback_centisecs": ("Interval of the background writeback thread.", False, "io"),
    "os:kernel.numa_balancing": ("Automatic NUMA page migration (multi-socket hosts).", False, "cpu"),
    "os:kernel.sched_autogroup_enabled": ("Group CPU time per terminal session (desktop feature).", False, "cpu"),
    "os:fs.aio-max-nr": ("System-wide limit of in-flight asynchronous I/O requests (InnoDB native AIO).", False, "io"),
    "os:thp": ("Transparent Huge Pages (never / madvise / always).", False, "memory"),
    "os:scheduler": ("Block I/O scheduler of the data device.", False, "io"),
    "os:read_ahead_kb": ("OS read-ahead of the data device (no effect on InnoDB data files opened with O_DIRECT).", False, "io"),
    "os:vm.dirty_background_bytes": ("Dirty page cache that starts background writeback (affects buffered binlog/redo writes).", False, "io"),
    "os:vm.dirty_bytes": ("Dirty page cache at which writers are throttled.", False, "io"),
    "os:mount_noatime": ("Mount the data directory filesystem with noatime.", False, "io"),
    "os:mount_barrier": ("ext4 write barriers (OFF = nobarrier: DURABILITY TRADE-OFF).", False, "durability"),
    "os:malloc": ("Memory allocator for mysqld (glibc / jemalloc via LD_PRELOAD).", True, "memory"),
    "os:hugepages": ("Number of 2 MiB huge pages reserved (used by the buffer pool when large_pages=ON).", True, "memory"),
    "os:cpu_governor": ("CPU frequency governor.", False, "cpu"),
}


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def _bracket(s):
    m = re.search(r"\[([^\]]+)\]", s or "")
    return m.group(1) if m else (s or "")


def _mount_of(path):
    best = None
    for line in _read("/proc/mounts").splitlines():
        p = line.split()
        if len(p) >= 4 and (path + "/").startswith(p[1].rstrip("/") + "/") and (best is None or len(p[1]) > len(best[1])):
            best = p
    return best  # [dev, mountpoint, fstype, opts, ...]


def service_dropin(service):
    return f"/etc/systemd/system/{service}.service.d/zz-tuning-agent.conf"


def current(datadir, device, service):
    """{os:<name>: value} as currently in effect."""
    out = {}
    for k in SYSCTLS:
        val = _read("/proc/sys/" + k.replace(".", "/"))
        if val != "":
            out["os:" + k] = val
    out["os:thp"] = _bracket(_read("/sys/kernel/mm/transparent_hugepage/enabled"))
    if device:
        out["os:scheduler"] = _bracket(_read(f"/sys/block/{device}/queue/scheduler"))
        out["os:read_ahead_kb"] = _read(f"/sys/block/{device}/queue/read_ahead_kb")
    m = _mount_of(datadir)
    if m:
        opts = m[3].split(",")
        out["os:mount_noatime"] = "ON" if "noatime" in opts else "OFF"
        if m[2] == "ext4":
            out["os:mount_barrier"] = "OFF" if ("nobarrier" in opts or "barrier=0" in opts) else "ON"
    dropin = _read(service_dropin(service))
    out["os:malloc"] = "jemalloc" if "jemalloc" in dropin else "glibc"
    out["os:hugepages"] = _read("/proc/sys/vm/nr_hugepages")
    gov = sorted(set(_read(p) for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")))
    if gov:
        out["os:cpu_governor"] = ",".join(gov)
    return {k: v for k, v in out.items() if v != ""}


def facts(datadir, device, service):
    f = current(datadir, device, service)
    m = _mount_of(datadir)
    f["os:filesystem"] = m[2] if m else None
    f["os:jemalloc_available"] = any(os.path.exists(p) for p in JEMALLOC_CANDIDATES)
    f["os:numa_nodes"] = len(glob.glob("/sys/devices/system/node/node[0-9]*"))
    f["os:aio_in_use"] = _read("/proc/sys/fs/aio-nr")
    return f


class OSApplier:
    def __init__(self, datadir, device, service, log=print, originals=None):
        self.datadir, self.device, self.service, self.log = datadir, device, service, log
        self.originals = originals or {}

    def ensure_mysqld(self):
        subprocess.run(["systemctl", "start", self.service], capture_output=True)

    # -- live changes ------------------------------------------------------------
    def apply_live(self, name, value):
        """Apply one setting now. Returns True when mysqld must be restarted for it to take effect."""
        if name in ("os:vm.dirty_bytes", "os:vm.dirty_background_bytes") and str(value) in ("0", ""):
            # 0 is not writable: returning to ratio mode means writing the ratio (which resets the bytes value to 0)
            ratio = "os:vm.dirty_ratio" if name == "os:vm.dirty_bytes" else "os:vm.dirty_background_ratio"
            default = "20" if name == "os:vm.dirty_bytes" else "10"
            r = self.originals.get(ratio)
            _write("/proc/sys/" + ratio[3:].replace(".", "/"), r if r not in (None, "", "0") else default)
            return False
        if name[3:] in SYSCTLS:
            _write("/proc/sys/" + name[3:].replace(".", "/"), value)
            return False
        if name == "os:thp":
            for f in ("enabled", "defrag"):
                p = f"/sys/kernel/mm/transparent_hugepage/{f}"
                if os.path.exists(p):
                    _write(p, value)
            return False
        if name == "os:scheduler":
            _write(f"/sys/block/{self.device}/queue/scheduler", value)
            return False
        if name == "os:read_ahead_kb":
            _write(f"/sys/block/{self.device}/queue/read_ahead_kb", value)
            return False
        if name in ("os:mount_noatime", "os:mount_barrier"):
            m = _mount_of(self.datadir)
            if not m:
                raise RuntimeError("cannot find the data directory mount")
            opt = {"os:mount_noatime": {"ON": "noatime", "OFF": "relatime"},
                   "os:mount_barrier": {"ON": "barrier=1", "OFF": "barrier=0"}}[name][value]
            r = subprocess.run(["mount", "-o", f"remount,{opt}", m[1]], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"remount {m[1]} with {opt} failed: {r.stderr.strip()}")
            return False
        if name == "os:hugepages":
            want = int(value)
            if want == 0:
                _write("/proc/sys/vm/nr_hugepages", 0)
                return True
            # reserve with mysqld stopped and memory compacted, otherwise fragmentation leaves us short
            self.log("stopping mysqld to reserve huge pages ...")
            subprocess.run(["systemctl", "stop", self.service], capture_output=True)
            subprocess.run(["sync"])
            _write("/proc/sys/vm/drop_caches", 3)
            if os.path.exists("/proc/sys/vm/compact_memory"):
                _write("/proc/sys/vm/compact_memory", 1)
            _write("/proc/sys/vm/nr_hugepages", want)
            _write("/proc/sys/vm/hugetlb_shm_group", self.mysql_gid())
            got = int(_read("/proc/sys/vm/nr_hugepages") or 0)
            if got < want:
                _write("/proc/sys/vm/nr_hugepages", 0)
                subprocess.run(["systemctl", "start", self.service], capture_output=True)
                raise RuntimeError(f"only {got} of {want} huge pages could be reserved (not enough contiguous memory); "
                                   "huge pages not enabled")
            return True
        if name == "os:malloc":
            if value == "jemalloc" and not any(os.path.exists(p) for p in JEMALLOC_CANDIDATES):
                if shutil.which("apt-get"):
                    subprocess.run(["apt-get", "install", "-yq", "libjemalloc2"], capture_output=True)
                elif shutil.which("dnf"):
                    subprocess.run(["dnf", "install", "-yq", "jemalloc"], capture_output=True)
                if not any(os.path.exists(p) for p in JEMALLOC_CANDIDATES):
                    raise RuntimeError("jemalloc library not available")
            return True  # takes effect at the next mysqld start (drop-in written by persist())
        if name == "os:cpu_governor":
            for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
                _write(p, value)
            return False
        raise RuntimeError(f"unknown OS setting {name}")

    @staticmethod
    def mysql_gid():
        import grp
        for g in ("mysql", "mariadb"):
            try:
                return grp.getgrnam(g).gr_gid
            except KeyError:
                pass
        return 0

    # -- persistence ---------------------------------------------------------------
    def persist(self, managed):
        """Write boot-time configuration for every managed os:* setting (and remove what is no longer managed)."""
        osm = {k: v["value"] if isinstance(v, dict) else v for k, v in managed.items() if k.startswith("os:")}
        # sysctl
        sysctl = [f"{k[3:]} = {v}" for k, v in osm.items() if k[3:] in SYSCTLS]
        if "os:hugepages" in osm:
            sysctl.append(f"vm.nr_hugepages = {osm['os:hugepages']}")
            if int(osm["os:hugepages"] or 0) > 0:
                sysctl.append(f"vm.hugetlb_shm_group = {self.mysql_gid()}")
        self._file(SYSCTL_FILE, "# Managed by tuning-agent\n" + "\n".join(sysctl) + "\n" if sysctl else None)
        # udev: scheduler / read-ahead for the data device
        rules = []
        if self.device and "os:scheduler" in osm:
            rules.append(f'ACTION=="add|change", KERNEL=="{self.device}", ATTR{{queue/scheduler}}="{osm["os:scheduler"]}"')
        if self.device and "os:read_ahead_kb" in osm:
            rules.append(f'ACTION=="add|change", KERNEL=="{self.device}", ATTR{{queue/read_ahead_kb}}="{osm["os:read_ahead_kb"]}"')
        self._file(UDEV_FILE, "# Managed by tuning-agent\n" + "\n".join(rules) + "\n" if rules else None)
        # boot unit: THP and CPU governor
        cmds = []
        if "os:thp" in osm:
            cmds += [f"/bin/sh -c 'echo {osm['os:thp']} > /sys/kernel/mm/transparent_hugepage/enabled'",
                     f"/bin/sh -c 'echo {osm['os:thp']} > /sys/kernel/mm/transparent_hugepage/defrag'"]
        if "os:cpu_governor" in osm:
            cmds.append(f"/bin/sh -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo {osm['os:cpu_governor']} > $g; done'")
        if cmds:
            unit = ("[Unit]\nDescription=tuning-agent OS settings\nBefore=mysql.service mysqld.service mariadb.service\n\n"
                    "[Service]\nType=oneshot\n" + "".join(f"ExecStart={c}\n" for c in cmds) + "\n[Install]\nWantedBy=multi-user.target\n")
            self._file(BOOT_UNIT, unit)
            subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "enable", "tuning-agent-os.service"], capture_output=True)
        elif os.path.exists(BOOT_UNIT):
            subprocess.run(["systemctl", "disable", "tuning-agent-os.service"], capture_output=True)
            self._file(BOOT_UNIT, None)
        # mysqld service drop-in: allocator, memlock for huge pages
        lines = []
        if osm.get("os:malloc") == "jemalloc":
            lib = next((p for p in JEMALLOC_CANDIDATES if os.path.exists(p)), JEMALLOC_CANDIDATES[0])
            lines.append(f"Environment=LD_PRELOAD={lib}")
        if int(osm.get("os:hugepages", 0) or 0) > 0:
            lines.append("LimitMEMLOCK=infinity")
        self._file(service_dropin(self.service), "# Managed by tuning-agent\n[Service]\n" + "\n".join(lines) + "\n" if lines else None)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        # fstab mount options for the data directory filesystem; settings no longer managed go back to their originals
        fs = dict(osm)
        for k in ("os:mount_noatime", "os:mount_barrier"):
            if k not in fs and self.originals.get(k) is not None:
                fs[k] = self.originals[k]
        self._fstab(fs)

    def _file(self, path, content):
        if content is None:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write(path, content)

    def _fstab(self, osm):
        m = _mount_of(self.datadir)
        if not m or not os.path.exists("/etc/fstab"):
            return
        want_add, want_remove = set(), set()
        if "os:mount_noatime" in osm:
            (want_add if osm["os:mount_noatime"] == "ON" else want_remove).add("noatime")
        if "os:mount_barrier" in osm:
            (want_add if osm["os:mount_barrier"] == "OFF" else want_remove).add("barrier=0")
        if not want_add and not want_remove and "tuning-agent" not in _read("/etc/fstab"):
            return
        lines = _read("/etc/fstab").splitlines()
        out, changed = [], False
        for line in lines:
            p = line.split()
            if len(p) >= 4 and not line.lstrip().startswith("#") and p[1] == m[1]:
                opts = [o for o in p[3].split(",") if o not in want_remove]
                for o in want_add:
                    if o not in opts:
                        opts.append(o)
                opts = [o for o in opts if o != "defaults"] or ["defaults"]
                new = " ".join(p[:3] + [",".join(opts)] + p[4:])
                changed = changed or new != line
                out.append(new)
            else:
                out.append(line)
        if changed:
            shutil.copy2("/etc/fstab", "/etc/fstab.tuning-agent.bak")
            _write("/etc/fstab", "\n".join(out) + "\n")
            r = subprocess.run(["findmnt", "--verify", "--tab-file", "/etc/fstab"], capture_output=True, text=True)
            if r.returncode != 0 and "error" in (r.stdout + r.stderr).lower():
                shutil.copy2("/etc/fstab.tuning-agent.bak", "/etc/fstab")
                raise RuntimeError("fstab verification failed; original restored: " + (r.stdout + r.stderr)[-300:])
