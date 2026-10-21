"""NUMA: topology, where memory comes from, and where mysqld's memory lives.

On a multi-node host (multi-socket, or one socket split into nodes: AMD NPS2/NPS4, Intel SNC) memory local to a CPU's
node is faster than remote memory, and each node fills up on its own. The failure modes for MySQL are well known and
measurable:
* the buffer pool lands mostly on one node (the thread that allocated it ran there): that node runs out while others
  stay free - remote allocations, page cache thrashing, even swapping with free memory on the host;
* zone_reclaim_mode = 1 reclaims (drops page cache, swaps) on the local node instead of allocating remotely;
* automatic NUMA balancing migrates pages back and forth under a database's access pattern (hint faults, migrations);
* threads scheduled across nodes touch remote memory (no direct counter; local/remote allocation shares and the
  placement of mysqld's memory are the evidence).
Everything here reads /sys and /proc only; `root` makes it testable on a synthetic tree.
"""
import glob
import os
import re

NODE_ROOT = "/sys/devices/system/node"
NUMASTAT_KEYS = ("numa_hit", "numa_miss", "numa_foreign", "interleave_hit", "local_node", "other_node")
VMSTAT_KEYS = ("numa_pages_migrated", "pgmigrate_success", "numa_hint_faults", "numa_hint_faults_local", "numa_pte_updates")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _cpulist_count(s):
    n = 0
    for part in filter(None, s.split(",")):
        lo, _, hi = part.partition("-")
        n += (int(hi) - int(lo) + 1) if hi else 1
    return n


def nodes(root=NODE_ROOT):
    return sorted(int(m.group(1)) for p in glob.glob(os.path.join(root, "node[0-9]*"))
                  if (m := re.search(r"node(\d+)$", p)))


def _node_mem(root, n):
    """(total, free) bytes of one node from its meminfo."""
    tot = free = 0
    for line in _read(os.path.join(root, f"node{n}", "meminfo")).splitlines():
        m = re.search(r"Node \d+ (MemTotal|MemFree):\s+(\d+) kB", line)
        if m:
            if m.group(1) == "MemTotal":
                tot = int(m.group(2)) * 1024
            else:
                free = int(m.group(2)) * 1024
    return tot, free


def topology(root=NODE_ROOT):
    """{"nodes": n, "per_node": [{node, cpus, cpulist, mem_bytes, free_bytes}], "distances": [[..]]} - nodes: 1 on a
    uniform host."""
    ns = nodes(root)
    per, dist = [], []
    for n in ns:
        cl = _read(os.path.join(root, f"node{n}", "cpulist"))
        tot, free = _node_mem(root, n)
        per.append({"node": n, "cpus": _cpulist_count(cl), "cpulist": cl, "mem_bytes": tot, "free_bytes": free})
        d = _read(os.path.join(root, f"node{n}", "distance")).split()
        dist.append([int(x) for x in d if x.isdigit()])
    return {"nodes": len(ns) or 1, "per_node": per, "distances": dist}


def counters(root=NODE_ROOT, vmstat="/proc/vmstat"):
    """Cumulative allocation counters summed over nodes, free memory per node, and the kernel's page-migration
    counters: {"numastat": {...}, "free": {node: bytes}, "vm": {...}}."""
    tot = {k: 0 for k in NUMASTAT_KEYS}
    free = {}
    for n in nodes(root):
        for line in _read(os.path.join(root, f"node{n}", "numastat")).splitlines():
            k, _, v = line.partition(" ")
            if k in tot and v.strip().isdigit():
                tot[k] += int(v)
        free[n] = _node_mem(root, n)[1]
    vm = {}
    for line in _read(vmstat).splitlines():
        k, _, v = line.partition(" ")
        if k in VMSTAT_KEYS and v.isdigit():
            vm[k] = int(v)
    return {"numastat": tot, "free": free, "vm": vm}


def rates(a, b, dt):
    """Window rates from two counters() snapshots: remote share of allocations, misses, migrations, the tightest node."""
    out = {}
    if not a or not b or dt <= 0:
        return out
    na, nb = a.get("numastat") or {}, b.get("numastat") or {}
    d = {k: max(0, nb.get(k, 0) - na.get(k, 0)) for k in NUMASTAT_KEYS}
    loc, oth = d["local_node"], d["other_node"]
    if loc + oth:
        out["numa_other_node_pct"] = round(100.0 * oth / (loc + oth), 2)
    out["numa_miss_per_s"] = round(d["numa_miss"] / dt, 1)
    va, vb = a.get("vm") or {}, b.get("vm") or {}
    for k, name in (("numa_pages_migrated", "numa_pages_migrated_per_s"), ("numa_hint_faults", "numa_hint_faults_per_s")):
        if k in va and k in vb:
            out[name] = round(max(0, vb[k] - va[k]) / dt, 1)
    if "numa_hint_faults" in va and "numa_hint_faults_local" in vb and vb["numa_hint_faults"] > va["numa_hint_faults"]:
        hf = vb["numa_hint_faults"] - va["numa_hint_faults"]
        out["numa_hint_faults_remote_pct"] = round(100.0 * (hf - (vb["numa_hint_faults_local"] - va.get("numa_hint_faults_local", 0))) / hf, 1)
    fr = b.get("free") or {}
    if fr:
        out["numa_node_free_min_mb"] = round(min(fr.values()) / 2 ** 20, 1)
        out["numa_node_free_max_mb"] = round(max(fr.values()) / 2 ** 20, 1)
    return out


def placement(pid, page_size=4096):
    """Where a process's memory lives: {"per_node_mb": {node: MiB}, "policies_mb": {policy: MiB}, "max_node_pct"}, from
    /proc/<pid>/numa_maps (N<node>=<pages> per mapping; huge-page mappings count with their page size)."""
    per, pol = {}, {}
    try:
        lines = open(f"/proc/{pid}/numa_maps").read().splitlines()
    except OSError:
        return {}
    for line in lines:
        f = line.split()
        if len(f) < 2:
            continue
        policy = f[1].split(":")[0]
        kps = page_size
        m = re.search(r"kernelpagesize_kB=(\d+)", line)
        if m:
            kps = int(m.group(1)) * 1024
        for tok in f[2:]:
            mm = re.match(r"N(\d+)=(\d+)", tok)
            if mm:
                b = int(mm.group(2)) * kps
                per[int(mm.group(1))] = per.get(int(mm.group(1)), 0) + b
                pol[policy] = pol.get(policy, 0) + b
    tot = sum(per.values())
    if not tot:
        return {}
    return {"per_node_mb": {n: round(b / 2 ** 20, 1) for n, b in sorted(per.items())},
            "policies_mb": {p: round(b / 2 ** 20, 1) for p, b in pol.items()},
            "max_node_pct": round(100.0 * max(per.values()) / tot, 1)}
