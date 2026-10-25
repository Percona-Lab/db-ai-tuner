"""Operating-system tuning: read, apply live, persist across reboots, restore originals.

Settings are exposed to the rest of the agent as pseudo-variables named "os:<name>", so they flow through the
same managed-settings, run snapshots, config diffs, recommendations and rollback as MySQL variables.
"""
import glob
import os
import re
import shutil
import subprocess
import time

SYSCTL_FILE = "/etc/sysctl.d/99-tuning-agent.conf"
UDEV_FILE = "/etc/udev/rules.d/99-tuning-agent.rules"
BOOT_UNIT = "/etc/systemd/system/tuning-agent-os.service"
SWAPFILE = "/swapfile.tuning-agent"
JEMALLOC_CANDIDATES = ["/usr/lib/x86_64-linux-gnu/libjemalloc.so.2", "/usr/lib64/libjemalloc.so.2", "/usr/lib/aarch64-linux-gnu/libjemalloc.so.2"]
ALLOCATORS = {  # name -> (library candidates, apt package, dnf package)
    "jemalloc": (JEMALLOC_CANDIDATES, "libjemalloc2", "jemalloc"),
    "tcmalloc": (["/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4", "/usr/lib64/libtcmalloc_minimal.so.4"], "libtcmalloc-minimal4", "gperftools-libs"),
    "mimalloc": (["/usr/lib/x86_64-linux-gnu/libmimalloc.so.2", "/usr/lib64/libmimalloc.so.2"], "libmimalloc2.0", "mimalloc"),
}


def allocator_lib(name):
    libs = ALLOCATORS.get(name, ([], None, None))[0]
    return next((p for p in libs if os.path.exists(p)), None)


def allocator_env(value):
    """os:malloc value -> systemd Environment= lines."""
    name, _, conf = str(value or "glibc").partition(":")
    lines = [f"# os:malloc={value}"]
    if name == "glibc":
        if conf.startswith("arena_max="):
            lines.append(f"Environment=GLIBC_TUNABLES=glibc.malloc.arena_max={conf.split('=', 1)[1]}")
        return lines if len(lines) > 1 else []
    lib = allocator_lib(name)
    if lib:
        lines.append(f"Environment=LD_PRELOAD={lib}")
    if name == "jemalloc" and conf:
        lines.append(f"Environment=MALLOC_CONF={conf}")
    return lines

SYSCTLS = ["vm.swappiness", "vm.dirty_background_bytes", "vm.dirty_bytes", "vm.dirty_ratio", "vm.dirty_background_ratio", "vm.dirty_expire_centisecs", "vm.dirty_writeback_centisecs",
           "kernel.numa_balancing", "vm.zone_reclaim_mode", "kernel.sched_autogroup_enabled", "fs.aio-max-nr", "vm.min_free_kbytes",
           "net.core.somaxconn", "net.ipv4.tcp_max_syn_backlog", "net.core.netdev_max_backlog", "net.core.netdev_budget",
           "net.core.netdev_budget_usecs", "net.ipv4.ip_local_port_range", "net.ipv4.tcp_tw_reuse", "net.ipv4.tcp_slow_start_after_idle",
           "net.ipv4.tcp_congestion_control", "net.core.default_qdisc", "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem", "net.core.rmem_max",
           "net.core.wmem_max", "net.ipv4.tcp_keepalive_time", "net.ipv4.tcp_keepalive_intvl", "net.ipv4.tcp_keepalive_probes",
           "net.netfilter.nf_conntrack_max", "net.core.rps_sock_flow_entries"]

# name -> (description, needs mysqld restart, category)
OS_KB = {
    "os:vm.swappiness": ("Kernel preference for swapping anonymous memory (1 = only under real pressure).", False, "memory"),
    "os:vm.dirty_ratio": ("Dirty page cache (% of memory) at which writers are throttled (used when dirty_bytes is 0).", False, "io"),
    "os:vm.dirty_background_ratio": ("Dirty page cache (% of memory) that starts background writeback (used when dirty_background_bytes is 0).", False, "io"),
    "os:vm.min_free_kbytes": ("Free memory the kernel keeps for atomic (network/driver) allocations.", False, "memory"),
    "os:net.core.somaxconn": ("Upper bound of listen backlogs (MySQL back_log is capped by it).", False, "network"),
    "os:net.ipv4.tcp_max_syn_backlog": ("Half-open connection queue.", False, "network"),
    "os:net.core.netdev_max_backlog": ("Per-CPU packet backlog before softnet drops.", False, "network"),
    "os:net.core.netdev_budget": ("Packets processed per softirq round.", False, "network"),
    "os:net.core.netdev_budget_usecs": ("Time budget per softirq round.", False, "network"),
    "os:net.ipv4.ip_local_port_range": ("Ephemeral ports for outgoing connections (clients).", False, "network"),
    "os:net.ipv4.tcp_tw_reuse": ("Reuse TIME_WAIT sockets for new outgoing connections (clients).", False, "network"),
    "os:net.ipv4.tcp_slow_start_after_idle": ("Shrink the congestion window after idle (hurts pooled connections over WAN).", False, "network"),
    "os:net.ipv4.tcp_congestion_control": ("TCP congestion control algorithm (cubic / bbr).", False, "network"),
    "os:net.core.default_qdisc": ("Default queueing discipline (fq pairs with bbr).", False, "network"),
    "os:net.ipv4.tcp_rmem": ("TCP receive buffer min/default/max (autotuning ceiling).", False, "network"),
    "os:net.ipv4.tcp_wmem": ("TCP send buffer min/default/max.", False, "network"),
    "os:net.core.rmem_max": ("Max receive buffer an application may set.", False, "network"),
    "os:net.core.wmem_max": ("Max send buffer an application may set.", False, "network"),
    "os:net.ipv4.tcp_keepalive_time": ("Idle time before TCP keepalive probes.", False, "network"),
    "os:net.ipv4.tcp_keepalive_intvl": ("Interval between keepalive probes.", False, "network"),
    "os:net.ipv4.tcp_keepalive_probes": ("Keepalive probes before the connection is dropped.", False, "network"),
    "os:net.netfilter.nf_conntrack_max": ("Connection tracking table size (firewalled hosts).", False, "network"),
    "os:net.core.rps_sock_flow_entries": ("RFS global flow table.", False, "network"),
    "os:rps": ("Receive Packet Steering over all CPUs for every RX queue (spreads softirq).", False, "network"),
    "os:vm.dirty_expire_centisecs": ("Age at which dirty page cache must be written back.", False, "io"),
    "os:vm.dirty_writeback_centisecs": ("Interval of the background writeback thread.", False, "io"),
    "os:kernel.numa_balancing": ("Automatic NUMA page migration (multi-socket hosts).", False, "cpu"),
    "os:vm.zone_reclaim_mode": ("Reclaim memory on the local NUMA node (drop cache, swap) before allocating on another node (0 = allocate remotely).", False, "memory"),
    "os:mysqld_numa_policy": ("NUMA memory policy of mysqld (default / interleave across all nodes / bind:N or preferred:N to one node), via systemd NUMAPolicy.", True, "memory"),
    "os:kernel.sched_autogroup_enabled": ("Group CPU time per terminal session (desktop feature).", False, "cpu"),
    "os:fs.aio-max-nr": ("System-wide limit of in-flight asynchronous I/O requests (InnoDB native AIO).", False, "io"),
    "os:thp": ("Transparent Huge Pages (never / madvise / always).", False, "memory"),
    "os:scheduler": ("Block I/O scheduler of the data device.", False, "io"),
    "os:read_ahead_kb": ("OS read-ahead of the data device (no effect on InnoDB data files opened with O_DIRECT).", False, "io"),
    "os:vm.dirty_background_bytes": ("Dirty page cache that starts background writeback (affects buffered binlog/redo writes).", False, "io"),
    "os:vm.dirty_bytes": ("Dirty page cache at which writers are throttled.", False, "io"),
    "os:mount_noatime": ("Mount the data directory filesystem with noatime.", False, "io"),
    "os:mount_barrier": ("ext4 write barriers (OFF = nobarrier: DURABILITY TRADE-OFF).", False, "durability"),
    "os:disk_write_cache": ("Volatile write cache of the data drive (hdparm -W / SCSI WCE / NVMe feature 6; none: the drive has "
                            "none). ON without power-loss protection: DURABILITY TRADE-OFF.", False, "durability"),
    "os:malloc": ("Memory allocator for mysqld: glibc[:arena_max=N], jemalloc[:MALLOC_CONF], tcmalloc, mimalloc (LD_PRELOAD).", True, "memory"),
    "os:mysqld_cap_sys_nice": ("Give mysqld CAP_SYS_NICE (systemd AmbientCapabilities): needed for resource-group thread priorities "
                               "(rg:purge prio); without it Linux ignores them. ON / OFF.", True, "cpu"),
    "os:datadir_fs": ("Filesystem of the data directory on the experiment device (scope 'filesystem' only).", True, "io"),
    "os:hugepages": ("Number of 2 MiB huge pages reserved (used by the buffer pool when large_pages=ON).", True, "memory"),
    "os:cpu_governor": ("CPU frequency governor.", False, "cpu"),
    "os:swapfile": ("Extra swap file managed by the agent (size, 0 = none): room for cold memory so caches can grow.", False, "memory"),
    "os:zswap": ("Compressed in-memory cache for swapped pages (off / on[:compressor]): cheaper swap-ins.", False, "memory"),
    "os:cpu_affinity": ("CPUs the database process may run on (all / CPU list such as 1-3), e.g. to keep it off the CPUs handling network interrupts.", False, "cpu"),
    "os:net_irq_cpus": ("CPUs that handle the network card interrupts (auto / CPU list); irqbalance is stopped while managed.", False, "network"),
}


APPARMOR_MYSQLD = "/etc/apparmor.d/usr.sbin.mysqld"
APPARMOR_SAVED = "/var/lib/tuning-agent/usr.sbin.mysqld.orig"


def apparmor_hugetlb(on):
    """Ubuntu's mysqld AppArmor profile blocks InnoDB's huge-page buffer pool: the anonymous hugetlbfs mapping has no
    path, AppArmor fails the name lookup ("disconnected path", errno 13) even in complain mode, InnoDB falls back to
    normal memory while the reserved pages stay pinned, and mysqld cannot start (tuner-vbm2, Scaleway, 2026-10-21).
    attach_disconnected added to the profile's flags (others kept) fixes it; the original profile comes back with
    huge pages off. Returns a note when it changed something."""
    if not os.path.exists(APPARMOR_MYSQLD) or not shutil.which("apparmor_parser"):
        return None
    if not on:
        if os.path.exists(APPARMOR_SAVED):
            shutil.copy(APPARMOR_SAVED, APPARMOR_MYSQLD)
            os.remove(APPARMOR_SAVED)
            subprocess.run(["apparmor_parser", "-r", APPARMOR_MYSQLD], capture_output=True)
            return "AppArmor profile for mysqld restored"
        return None
    text = open(APPARMOR_MYSQLD).read()
    m = re.search(r"^(\s*/usr/sbin/mysqld)(\s+flags=\(([^)]*)\))?(\s*\{)", text, re.M)
    if not m:
        return None
    flags = [f.strip() for f in (m.group(3) or "").split(",") if f.strip()]
    if "attach_disconnected" in flags:
        return None
    os.makedirs(os.path.dirname(APPARMOR_SAVED), exist_ok=True)
    if not os.path.exists(APPARMOR_SAVED):
        shutil.copy(APPARMOR_MYSQLD, APPARMOR_SAVED)
    new = text[:m.start()] + f"{m.group(1)} flags=({','.join(flags + ['attach_disconnected'])}){m.group(4)}" + text[m.end():]
    with open(APPARMOR_MYSQLD, "w") as f:
        f.write(new)
    r = subprocess.run(["apparmor_parser", "-r", APPARMOR_MYSQLD], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy(APPARMOR_SAVED, APPARMOR_MYSQLD)
        subprocess.run(["apparmor_parser", "-r", APPARMOR_MYSQLD], capture_output=True)
        return f"AppArmor profile for mysqld could not be adjusted for huge pages: {r.stderr.strip()[:200]}"
    return "AppArmor profile for mysqld: attach_disconnected added (huge-page buffer pool; restored with huge pages off)"


def parse_bytes(v):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", str(v), re.I)
    if not m:
        raise ValueError(f"bad size {v}")
    return int(float(m.group(1)) * 1024 ** "bkmgt".index((m.group(2) or "b").lower()))


def online_cpus():
    return _read("/sys/devices/system/cpu/online") or f"0-{(os.cpu_count() or 1) - 1}"


def cpu_list(spec):
    """'0-2,5' -> {0,1,2,5}"""
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def nic_irqs():
    """IRQ numbers of the network interfaces (MSI vectors of the NIC; virtio-net queues by name)."""
    irqs = set()
    names = []
    for dev in glob.glob("/sys/class/net/*/device"):
        real = os.path.realpath(dev)
        names.append(os.path.basename(real))  # e.g. virtio1 (its queues are named virtio1-input.0 / -output.0)
        msi = os.path.join(real, "msi_irqs")
        if not os.path.basename(real).startswith("virtio") and os.path.isdir(msi):
            irqs.update(int(x) for x in os.listdir(msi) if x.isdigit())
    for line in _read("/proc/interrupts").splitlines():
        p = line.split()
        if p and p[0].rstrip(":").isdigit() and any(re.search(rf"\b{n}-(input|output)", line) for n in names if n.startswith("virtio")):
            irqs.add(int(p[0].rstrip(":")))
    return sorted(i for i in irqs if os.path.exists(f"/proc/irq/{i}/smp_affinity_list"))


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


def current(datadir, device, service, fs_device=None):
    """{os:<name>: value} as currently in effect."""
    out = {}
    if fs_device:
        from . import fslab
        out["os:datadir_fs"] = fslab.current(fs_device)
    for k in SYSCTLS:
        val = _read("/proc/sys/" + k.replace(".", "/"))
        if val != "":
            out["os:" + k] = val
    out["os:thp"] = _bracket(_read("/sys/kernel/mm/transparent_hugepage/enabled"))
    if device:
        out["os:scheduler"] = _bracket(_read(f"/sys/block/{device}/queue/scheduler"))
        out["os:read_ahead_kb"] = _read(f"/sys/block/{device}/queue/read_ahead_kb")
        wc = _read(f"/sys/block/{device}/queue/write_cache")  # the drive's cache as the kernel knows it
        if wc in ("write back", "write through"):
            # NVMe: the driver reports "write through" when the drive has no volatile write cache at all (typical of
            # datacenter drives with power-loss protection): nothing to turn on. SCSI / SATA: it is the drive's WCE bit.
            out["os:disk_write_cache"] = "ON" if wc == "write back" else "none" if device.startswith("nvme") else "OFF"
    m = _mount_of(datadir)
    if m:
        opts = m[3].split(",")
        out["os:mount_noatime"] = "ON" if "noatime" in opts else "OFF"
        if m[2] == "ext4":
            out["os:mount_barrier"] = "OFF" if ("nobarrier" in opts or "barrier=0" in opts) else "ON"
    q = sorted(glob.glob("/sys/class/net/*/queues/rx-*/rps_cpus"))
    q = [p for p in q if "/lo/" not in p]
    if q:
        out["os:rps"] = "on" if any(_read(p).strip("0,\n ") for p in q) else "off"
    dropin = _read(service_dropin(service))
    out["os:mysqld_cap_sys_nice"] = "ON" if re.search(r"^AmbientCapabilities=.*CAP_SYS_NICE", dropin, re.M) else "OFF"
    m = re.search(r"^# os:malloc=(.+)$", dropin, re.M)
    out["os:malloc"] = m.group(1).strip() if m else ("jemalloc" if "jemalloc" in dropin else "glibc")
    out["os:hugepages"] = _read("/proc/sys/vm/nr_hugepages")
    dropin = _read(service_dropin(service))
    mp = re.search(r"^NUMAPolicy=(\w+)", dropin, re.M)
    mm = re.search(r"^NUMAMask=(\S+)", dropin, re.M)
    out["os:mysqld_numa_policy"] = "default" if not mp else (mp.group(1) if mp.group(1) == "interleave" or not mm else f"{mp.group(1)}:{mm.group(1)}")
    swaps = _read("/proc/swaps")
    if SWAPFILE in swaps and os.path.exists(SWAPFILE):
        mb = os.path.getsize(SWAPFILE) // 2 ** 20
        out["os:swapfile"] = f"{mb // 1024}G" if mb % 1024 == 0 else f"{mb}M"
    else:
        out["os:swapfile"] = "0"
    if os.path.exists("/sys/module/zswap/parameters/enabled"):
        on = _read("/sys/module/zswap/parameters/enabled") in ("Y", "1")
        out["os:zswap"] = f"on:{_read('/sys/module/zswap/parameters/compressor')}" if on else "off"
    from .hostinfo import mysqld_pid
    pid = mysqld_pid()
    if pid:
        allowed = re.search(r"^Cpus_allowed_list:\s*(\S+)", _read(f"/proc/{pid}/status"), re.M)
        if allowed:
            out["os:cpu_affinity"] = "all" if cpu_list(allowed.group(1)) == cpu_list(online_cpus()) else allowed.group(1)
    irqs = nic_irqs()
    if irqs:
        aff = {_read(f"/proc/irq/{i}/smp_affinity_list") for i in irqs}
        managed_irq = os.path.exists(BOOT_UNIT) and "smp_affinity_list" in _read(BOOT_UNIT)
        out["os:net_irq_cpus"] = aff.pop() if managed_irq and len(aff) == 1 else "auto"
    gov = sorted(set(_read(p) for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")))
    if gov:
        out["os:cpu_governor"] = ",".join(gov)
    return {k: v for k, v in out.items() if v != ""}


def facts(datadir, device, service, fs_device=None):
    f = current(datadir, device, service, fs_device)
    m = _mount_of(datadir)
    f["os:filesystem"] = m[2] if m else None
    f["os:jemalloc_available"] = any(os.path.exists(p) for p in JEMALLOC_CANDIDATES)
    f["os:numa_nodes"] = len(glob.glob("/sys/devices/system/node/node[0-9]*"))
    if f["os:numa_nodes"] > 1:
        from . import numa
        topo = numa.topology()
        f["os:numa_node_cpus"] = ",".join(str(x["cpus"]) for x in topo["per_node"])
        f["os:numa_node_mem_gb"] = ",".join(str(round(x["mem_bytes"] / 2 ** 30, 1)) for x in topo["per_node"])
        f["os:numa_node_cpulists"] = ";".join(x["cpulist"] for x in topo["per_node"])
    f["os:aio_in_use"] = _read("/proc/sys/fs/aio-nr")
    # CPU vulnerability mitigations that cost every syscall / kernel entry (PTI, IBRS, buffer clearing): what a
    # kernel-heavy profile pays for them (user, 2026-10-25: 20%+ system CPU)
    costly = []
    for path in sorted(glob.glob("/sys/devices/system/cpu/vulnerabilities/*")):
        txt = _read(path) or ""
        for mark in ("PTI", "IBRS", "Clear CPU buffers", "IBPB before exit", "retpoline"):
            if mark in txt and "Not affected" not in txt:
                costly.append(f"{os.path.basename(path)}: {mark}")
    cmd = _read("/proc/cmdline") or ""
    f["os:cpu_mitigations"] = "off" if "mitigations=off" in cmd else ("; ".join(costly) or "none costly")
    return f


class OSApplier:
    def __init__(self, datadir, device, service, log=print, originals=None, fs_device=None):
        self.datadir, self.device, self.service, self.log = datadir, device, service, log
        self.fs_device = fs_device
        self.originals = originals if originals is not None else {}

    def ensure_mysqld(self):
        subprocess.run(["systemctl", "start", self.service], capture_output=True)

    def _disk_write_cache(self, on):
        """Turn the drive's own volatile write cache on or off. Never by writing /sys/block/<dev>/queue/write_cache:
        that only changes what the kernel believes and makes it stop sending cache flushes - fsync would then return
        before data is on stable media even with the drive cache off (worse than nobarrier, and invisible)."""
        dev = self.device or ""
        if not dev:
            raise RuntimeError("no data device known")
        if dev.startswith("nvme"):
            r = subprocess.run(["nvme", "set-feature", f"/dev/{dev}", "--feature-id=6", f"--value={1 if on else 0}"],
                               capture_output=True, text=True) if shutil.which("nvme") else None
        else:
            # SCSI / SATA (through libata): cache_type sends a MODE SELECT that sets the drive's WCE bit; hdparm for plain ATA
            ct = glob.glob(f"/sys/block/{dev}/device/scsi_disk/*/cache_type")
            r = None
            if ct:
                try:
                    _write(ct[0], "write back" if on else "write through")
                except OSError as e:
                    r = subprocess.CompletedProcess([], 1, "", str(e))
            if (r is not None or not ct) and shutil.which("hdparm"):
                r = subprocess.run(["hdparm", f"-W{1 if on else 0}", f"/dev/{dev}"], capture_output=True, text=True)
        if r is not None and r.returncode != 0:
            raise RuntimeError(f"cannot change the write cache of /dev/{dev}: {(r.stderr or r.stdout).strip()[:200]}")
        got = _read(f"/sys/block/{dev}/queue/write_cache")
        if got != ("write back" if on else "write through"):
            raise RuntimeError(f"/dev/{dev} reports '{got}' after the change: the device (or the hypervisor behind a virtual disk) "
                               "does not let the guest change its write cache")
        # not persisted: drives come back with their own setting after a power cycle; current() reads the real state

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
        if name == "os:net.ipv4.tcp_congestion_control" and value not in _read("/proc/sys/net/ipv4/tcp_available_congestion_control").split():
            subprocess.run(["modprobe", f"tcp_{value}"], capture_output=True)
        if name == "os:rps":
            self._rps(value == "on")
            return False
        if name[3:] in SYSCTLS:
            p = "/proc/sys/" + name[3:].replace(".", "/")
            if not os.path.exists(p):
                raise RuntimeError(f"{name[3:]} is not available on this kernel")
            _write(p, value)
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
        if name == "os:disk_write_cache":
            self._disk_write_cache(str(value).upper() == "ON")
            return False
        if name in ("os:mount_noatime", "os:mount_barrier"):
            m = _mount_of(self.datadir)
            if not m:
                raise RuntimeError("cannot find the data directory mount")
            opt = {"os:mount_noatime": {"ON": "noatime", "OFF": "relatime"},
                   "os:mount_barrier": {"ON": "barrier=1", "OFF": "barrier=0"}}[name][value]
            # --options-mode=ignore: do not merge in /etc/fstab options (they are rewritten afterwards by persist())
            r = subprocess.run(["mount", "--options-mode=ignore", "-o", f"remount,{opt}", m[1]], capture_output=True, text=True)
            if r.returncode != 0 and "options-mode" in (r.stderr or ""):
                r = subprocess.run(["mount", "-o", f"remount,{opt}", m[1]], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"remount {m[1]} with {opt} failed: {r.stderr.strip()}")
            return False
        if name == "os:hugepages":
            want = int(value)
            if want == 0:
                _write("/proc/sys/vm/nr_hugepages", 0)
                apparmor_hugetlb(False)
                return True
            # reserve with mysqld stopped and memory compacted, otherwise fragmentation leaves us short
            self.log("stopping mysqld to reserve huge pages ...")
            subprocess.run(["systemctl", "stop", self.service], capture_output=True)
            subprocess.run(["sync"])
            _write("/proc/sys/vm/drop_caches", 3)
            if os.path.exists("/proc/sys/vm/compact_memory"):
                _write("/proc/sys/vm/compact_memory", 1)
            _write("/proc/sys/vm/nr_hugepages", want)
            got = int(_read("/proc/sys/vm/nr_hugepages") or 0)
            for _ in range(3):  # each pass compacts more: close misses (e.g. 96%) usually complete
                if got >= want:
                    break
                _write("/proc/sys/vm/drop_caches", 3)
                if os.path.exists("/proc/sys/vm/compact_memory"):
                    _write("/proc/sys/vm/compact_memory", 1)
                time.sleep(2)
                _write("/proc/sys/vm/nr_hugepages", want)
                got = int(_read("/proc/sys/vm/nr_hugepages") or 0)
            _write("/proc/sys/vm/hugetlb_shm_group", self.mysql_gid())
            if got < want:
                _write("/proc/sys/vm/nr_hugepages", 0)
                subprocess.run(["systemctl", "start", self.service], capture_output=True)
                raise RuntimeError(f"only {got} of {want} huge pages could be reserved (not enough contiguous memory); "
                                   "huge pages not enabled")
            note = apparmor_hugetlb(True)
            if note:
                self.log(note)
            return True
        if name == "os:mysqld_cap_sys_nice":
            return True  # takes effect at the next mysqld start (drop-in written by persist())
        if name == "os:malloc":
            alloc = str(value).partition(":")[0]
            if alloc != "glibc":
                if alloc not in ALLOCATORS:
                    raise RuntimeError(f"unknown allocator {alloc}")
                if not allocator_lib(alloc):
                    _, apt, dnf = ALLOCATORS[alloc]
                    if shutil.which("apt-get"):
                        subprocess.run(["apt-get", "install", "-yq", apt], capture_output=True)
                    elif shutil.which("dnf"):
                        subprocess.run(["dnf", "install", "-yq", dnf], capture_output=True)
                    if not allocator_lib(alloc):
                        raise RuntimeError(f"{alloc} library not available")
            return True  # takes effect at the next mysqld start (drop-in written by persist())
        if name == "os:datadir_fs":
            from . import fslab
            if not self.fs_device:
                raise RuntimeError("no filesystem experiment device configured (fs_device / allow_fs_experiments)")
            return fslab.apply(value, self.fs_device, self.service, self.log)
        if name == "os:swapfile":
            size = 0 if str(value) in ("0", "", "off") else parse_bytes(value)
            if SWAPFILE in _read("/proc/swaps"):
                r = subprocess.run(["swapoff", SWAPFILE], capture_output=True, text=True, timeout=1800)
                if r.returncode != 0:
                    raise RuntimeError(f"swapoff failed (not enough free memory to take swapped pages back?): {r.stderr.strip()}")
            if os.path.exists(SWAPFILE):
                os.remove(SWAPFILE)
            if size:
                st = os.statvfs("/")
                if st.f_bavail * st.f_frsize < size + 2 * 2 ** 30:
                    raise RuntimeError(f"not enough free disk space on / for a {value} swap file")
                for cmd in (["fallocate", "-l", str(size), SWAPFILE], ["chmod", "600", SWAPFILE], ["mkswap", "-q", SWAPFILE], ["swapon", SWAPFILE]):
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode != 0:
                        if os.path.exists(SWAPFILE) and SWAPFILE not in _read("/proc/swaps"):
                            os.remove(SWAPFILE)
                        raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
            return False
        if name == "os:zswap":
            if not os.path.exists("/sys/module/zswap/parameters/enabled"):
                raise RuntimeError("zswap is not available in this kernel")
            on, _, comp = str(value).partition(":")
            if on == "on" and comp:
                subprocess.run(["modprobe", comp], capture_output=True)
                _write("/sys/module/zswap/parameters/compressor", comp)
            _write("/sys/module/zswap/parameters/enabled", "Y" if on == "on" else "N")
            return False
        if name == "os:mysqld_numa_policy":
            m = re.fullmatch(r"(default|interleave)|(bind|preferred):(\d+(?:[-,]\d+)*)", str(value or "default"))
            if not m:
                raise RuntimeError(f"NUMA policy {value}: default, interleave, bind:<nodes> or preferred:<node>")
            from . import numa
            if len(numa.nodes()) < 2 and str(value) != "default":
                raise RuntimeError("a NUMA memory policy needs a host with more than one NUMA node")
            return True  # systemd applies it when mysqld starts (drop-in written by persist)
        if name == "os:cpu_affinity":
            from .hostinfo import mysqld_pid
            cpus = online_cpus() if value in ("all", "", None) else str(value)
            if not cpu_list(cpus) <= cpu_list(online_cpus()):
                raise RuntimeError(f"CPU list {cpus} is not within the online CPUs {online_cpus()}")
            pid = mysqld_pid()
            if pid:  # all existing threads now; new threads inherit from their creator; the drop-in covers restarts
                r = subprocess.run(["taskset", "-a", "-cp", cpus, str(pid)], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(f"taskset failed: {r.stderr.strip()}")
            return False
        if name == "os:net_irq_cpus":
            irqs = nic_irqs()
            if not irqs:
                raise RuntimeError("no network interface interrupts found")
            cpus = online_cpus() if value in ("auto", "", None) else str(value)
            if value not in ("auto", "", None):
                subprocess.run(["systemctl", "stop", "irqbalance"], capture_output=True)
            for i in irqs:
                try:
                    _write(f"/proc/irq/{i}/smp_affinity_list", cpus)
                except OSError:
                    pass  # some vectors (e.g. config) cannot be moved
            return False
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

    @staticmethod
    def _rps(on):
        ncpu = os.cpu_count() or 1
        mask = format((1 << ncpu) - 1, "x") if on else "0"
        qs = [p for p in glob.glob("/sys/class/net/*/queues/rx-*") if "/lo/" not in p]
        for q in qs:
            _write(os.path.join(q, "rps_cpus"), mask)
            _write(os.path.join(q, "rps_flow_cnt"), str(32768 // max(len(qs), 1)) if on else "0")
        _write("/proc/sys/net/core/rps_sock_flow_entries", "32768" if on else "0")

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
        if osm.get("os:rps") == "on":
            ncpu = os.cpu_count() or 1
            cmds.append("/bin/sh -c 'for q in /sys/class/net/*/queues/rx-*; do case $q in */lo/*) continue;; esac; "
                        f"echo {format((1 << ncpu) - 1, 'x')} > $q/rps_cpus; echo 8192 > $q/rps_flow_cnt; done; echo 32768 > /proc/sys/net/core/rps_sock_flow_entries'")
        if "os:zswap" in osm:
            on, _, comp = osm["os:zswap"].partition(":")
            if on == "on" and comp:
                cmds.append(f"/bin/sh -c 'modprobe {comp}; echo {comp} > /sys/module/zswap/parameters/compressor'")
            cmds.append(f"/bin/sh -c 'echo {'Y' if on == 'on' else 'N'} > /sys/module/zswap/parameters/enabled'")
        if osm.get("os:net_irq_cpus") not in (None, "auto"):
            irqs = " ".join(str(i) for i in nic_irqs())
            cmds.append(f"/bin/sh -c 'systemctl stop irqbalance; for i in {irqs}; do echo {osm['os:net_irq_cpus']} > /proc/irq/$i/smp_affinity_list; done; true'")
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
        cc = osm.get("os:net.ipv4.tcp_congestion_control")
        self._file("/etc/modules-load.d/tuning-agent.conf", f"tcp_{cc}\n" if cc and cc not in ("cubic", "reno") else None)
        # mysqld service drop-in: allocator, memlock for huge pages
        lines = []
        if osm.get("os:malloc") and osm["os:malloc"] != "glibc":
            lines += allocator_env(osm["os:malloc"])
        if int(osm.get("os:hugepages", 0) or 0) > 0:
            lines.append("LimitMEMLOCK=infinity")
        if str(osm.get("os:mysqld_cap_sys_nice") or "OFF").upper() == "ON":
            lines.append("AmbientCapabilities=CAP_SYS_NICE")  # resource-group thread priorities (rg:purge)
        if osm.get("os:cpu_affinity") not in (None, "all"):
            lines.append(f"CPUAffinity={osm['os:cpu_affinity'].replace(',', ' ')}")
        pol = str(osm.get("os:mysqld_numa_policy") or "default")
        if pol != "default":
            kind, _, mask = pol.partition(":")
            lines.append(f"NUMAPolicy={kind}")
            if kind == "interleave":
                from . import numa
                lines.append("NUMAMask=" + ",".join(str(n) for n in numa.nodes()))
            elif mask:
                lines.append(f"NUMAMask={mask}")
        self._file(service_dropin(self.service), "# Managed by tuning-agent\n[Service]\n" + "\n".join(lines) + "\n" if lines else None)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        # fstab mount options for the data directory filesystem; settings no longer managed go back to their originals
        fs = dict(osm)
        for k in ("os:mount_noatime", "os:mount_barrier"):
            if k not in fs and self.originals.get(k) is not None:
                fs[k] = self.originals[k]
        self._fstab(fs)
        self._fstab_swap(osm.get("os:swapfile") not in (None, "0", "off", ""))

    def _file(self, path, content):
        if content is None:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write(path, content)

    @staticmethod
    def _fstab_swap(on):
        if not os.path.exists("/etc/fstab"):
            return
        cur = _read("/etc/fstab").splitlines()
        lines = [l for l in cur if not l.startswith(SWAPFILE + " ")]
        if on:
            lines.append(f"{SWAPFILE} none swap sw 0 0")
        if lines == cur:
            return
        tmp = "/etc/fstab.tuning-agent.tmp"
        _write(tmp, "\n".join(lines) + "\n")
        os.replace(tmp, "/etc/fstab")

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
