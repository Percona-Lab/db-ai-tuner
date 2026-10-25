"""Is the host quiet before a run? (user, 2026-10-25: in session 361 a software RAID resync ran in the background and took
disk and CPU from the runs; once it ended the "configuration" won.) Between runs the benchmark is stopped and mysqld is
idle, so anything else using CPU or the disks is outside work the runs will share with it: a RAID resync or check, a
filesystem scrub, package updates, an indexer, another tenant's job. Measured for a few seconds after the reset; a busy
host is flagged on the run (never excluded - the effect on the result is not known)."""
import os
import re
import time

SAMPLE_S = 10
CPU_BUSY_PCT = 5.0     # other processes' CPU, % of all CPUs
DISK_MBS = 10.0        # other processes' disk traffic
DISK_UTIL_PCT = 10.0   # busiest disk's time busy


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""


def md_activity():
    """Software RAID arrays resyncing / recovering / checking / reshaping now: [{array, action, progress_pct}]."""
    out, cur = [], None
    for line in _read("/proc/mdstat").splitlines():
        m = re.match(r"^(md\S+)\s*:", line)
        if m:
            cur = m.group(1)
            continue
        m = re.search(r"\b(resync|recovery|check|reshape|repair)\s*=\s*([\d.]+)%", line)
        if m and cur:
            out.append({"array": cur, "action": m.group(1), "progress_pct": float(m.group(2))})
    for d in os.listdir("/sys/block") if os.path.isdir("/sys/block") else []:
        act = _read(f"/sys/block/{d}/md/sync_action").strip()
        if act and act not in ("idle", "frozen") and not any(x["array"] == d for x in out):
            out.append({"array": d, "action": act, "progress_pct": None})
    return out


def _cpu_total():
    f = _read("/proc/stat").split("\n", 1)[0].split()[1:]
    v = [int(x) for x in f[:8]]
    return sum(v), v[3] + v[4]  # total, idle + iowait


def _procs():
    """{pid: (name, cpu ticks, read bytes, write bytes)}"""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        st = _read(f"/proc/{d}/stat")
        if not st:
            continue
        try:
            name = st[st.index("(") + 1:st.rindex(")")]
            rest = st[st.rindex(")") + 2:].split()
            ticks = int(rest[11]) + int(rest[12])
        except (ValueError, IndexError):
            continue
        rb = wb = 0
        for line in _read(f"/proc/{d}/io").splitlines():
            if line.startswith("read_bytes:"):
                rb = int(line.split()[1])
            elif line.startswith("write_bytes:"):
                wb = int(line.split()[1])
        out[int(d)] = (name, ticks, rb, wb)
    return out


def _disks():
    """{disk: (sectors read, sectors written, ms busy)} for whole disks (no partitions, no virtual devices but md)."""
    out = {}
    for line in _read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if name.startswith(("loop", "ram", "zram", "dm-", "sr")) or not os.path.exists(f"/sys/block/{name}"):
            continue
        out[name] = (int(f[5]), int(f[9]), int(f[12]))
    return out


def check(exclude_pids=(), seconds=SAMPLE_S):
    """Samples the host for `seconds` with the benchmark stopped. Returns {busy, why, cpu_other_pct, top, disk_other_mbs,
    disk_util_pct, md}; busy only when something outside mysqld and the agent uses the host noticeably."""
    me = {os.getpid(), os.getppid()} | set(exclude_pids or ())
    ncpu = os.cpu_count() or 1
    hz = os.sysconf("SC_CLK_TCK")
    t0, c0, p0, d0 = time.time(), _cpu_total(), _procs(), _disks()
    time.sleep(seconds)
    t1, c1, p1, d1 = time.time(), _cpu_total(), _procs(), _disks()
    dt = max(t1 - t0, 0.1)
    host_busy = 100.0 * (1 - (c1[1] - c0[1]) / max(c1[0] - c0[0], 1))
    ignore = {"mysqld", "mariadbd", "postgres", "tuning-agent", "python3"}  # the database and the agent itself
    other_io = own_io = 0
    kthreads = {}
    for pid, (name, ticks, rb, wb) in p1.items():
        if pid not in p0:
            continue
        if pid in me or name in ignore:
            own_io += max((rb - p0[pid][2]) + (wb - p0[pid][3]), 0)  # the database flushing after the last run is its own work
            continue
        cpu = 100.0 * (ticks - p0[pid][1]) / hz / dt / ncpu
        io = (rb - p0[pid][2]) + (wb - p0[pid][3])
        other_io += max(io, 0)
        if cpu >= 0.2:
            # kernel worker names carry a per-CPU suffix (md0_resync, kworker/3:1): grouped by their stem
            stem = re.sub(r"[/:]\d.*$", "", name)
            kthreads[stem] = kthreads.get(stem, 0) + cpu
    per = sorted(({"name": k, "cpu_pct": round(v, 1)} for k, v in kthreads.items()), key=lambda x: -x["cpu_pct"])
    cpu_other = sum(x["cpu_pct"] for x in per)
    disk_mbs = sum(((d1[k][0] - d0[k][0]) + (d1[k][1] - d0[k][1])) * 512 for k in d1 if k in d0) / dt / 1e6
    outside_mbs = max(0.0, disk_mbs - own_io / dt / 1e6)
    util = max((100.0 * (d1[k][2] - d0[k][2]) / (dt * 1000) for k in d1 if k in d0), default=0.0)
    md = md_activity()
    why = []
    if md:
        why.append("software RAID " + ", ".join(f"{x['array']} {x['action']}" + (f" at {x['progress_pct']:.0f}%" if x["progress_pct"] is not None else "") for x in md))
    if cpu_other >= CPU_BUSY_PCT:
        why.append(f"other processes used {cpu_other:.0f}% of the CPUs (" + ", ".join(f"{x['name']} {x['cpu_pct']}%" for x in per[:3]) + ")")
    # disk traffic of processes (page cache writeback and RAID resync are kernel work: the disks' own counters show them)
    if other_io / dt / 1e6 >= DISK_MBS or (util >= DISK_UTIL_PCT and outside_mbs >= DISK_MBS):
        why.append(f"disks busy outside the database: {outside_mbs:.0f} MB/s, busiest disk {util:.0f}% of the time")
    return {"busy": bool(why), "why": "; ".join(why), "seconds": round(dt, 1), "host_busy_pct": round(host_busy, 1),
            "cpu_other_pct": round(cpu_other, 1), "top": per[:5], "disk_mbs": round(disk_mbs, 1), "disk_outside_mbs": round(outside_mbs, 1), "disk_util_pct": round(util, 1),
            "other_proc_io_mbs": round(other_io / dt / 1e6, 1), "md": md}
