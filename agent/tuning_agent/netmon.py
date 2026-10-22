"""Network observability for the tuner (server and client hosts).

Counters come from /proc/net/{netstat,snmp,sockstat,softnet_stat}, /proc/stat (per-CPU softirq) and conntrack; per
connection RTT / retransmits from `ss -tin` for the MySQL port (server: sport, client: dport).
"""
import os
import re
import subprocess

TCPEXT = ("ListenOverflows", "ListenDrops", "TCPReqQFullDrop", "TCPReqQFullDoCookies", "TCPTimeouts", "TCPSynRetrans",
          "TCPBacklogDrop", "TCPAbortOnTimeout", "TCPAbortOnMemory", "TCPLossProbes")
TCP = ("RetransSegs", "OutSegs", "ActiveOpens", "PassiveOpens", "EstabResets", "AttemptFails")


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""


def _pairs(text, prefix):
    lines = [l.split() for l in text.splitlines() if l.startswith(prefix + ":")]
    out = {}
    for i in range(0, len(lines) - 1, 2):
        for k, v in zip(lines[i][1:], lines[i + 1][1:]):
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


def snapshot():
    s = {}
    ext = _pairs(_read("/proc/net/netstat"), "TcpExt")
    tcp = _pairs(_read("/proc/net/snmp"), "Tcp")
    s["tcpext"] = {k: ext.get(k, 0) for k in TCPEXT}
    s["tcp"] = {k: tcp.get(k, 0) for k in TCP}
    s["curr_estab"] = tcp.get("CurrEstab", 0)
    m = re.search(r"TCP: inuse (\d+) orphan (\d+) tw (\d+)", _read("/proc/net/sockstat"))
    if m:
        s["tw"] = int(m.group(3))
    dropped = squeeze = 0
    for line in _read("/proc/net/softnet_stat").splitlines():
        p = line.split()
        if len(p) >= 3:
            dropped += int(p[1], 16)
            squeeze += int(p[2], 16)
    s["softnet_dropped"], s["softnet_squeeze"] = dropped, squeeze
    per = []
    for line in _read("/proc/stat").splitlines():
        if re.match(r"cpu\d+ ", line):
            f = [int(x) for x in line.split()[1:]]
            per.append((f[6] if len(f) > 6 else 0, sum(f[:8])))  # softirq, total
    s["cpu_softirq"] = per
    cnt, mx = _read("/proc/sys/net/netfilter/nf_conntrack_count").strip(), _read("/proc/sys/net/netfilter/nf_conntrack_max").strip()
    if cnt and mx:
        s["conntrack"] = (int(cnt), int(mx))
    return s


def rates(a, b, dt):
    out = {}
    if not a:
        return out
    for k in ("ListenOverflows", "ListenDrops", "TCPReqQFullDrop", "TCPTimeouts", "TCPSynRetrans", "TCPBacklogDrop"):
        out["net_" + re.sub(r"(?<!^)([A-Z])", r"_\1", k).lower().replace("t_c_p_", "tcp_")] = round(max(b["tcpext"][k] - a["tcpext"][k], 0) / dt, 3)
    d = {k: max(b["tcp"][k] - a["tcp"][k], 0) for k in TCP}
    out["net_retrans_pct"] = round(100.0 * d["RetransSegs"] / d["OutSegs"], 3) if d["OutSegs"] else 0.0
    out["net_conn_per_s"] = round((d["ActiveOpens"] + d["PassiveOpens"]) / dt, 2)
    out["net_resets_per_s"] = round(d["EstabResets"] / dt, 3)
    out["net_estab"] = b.get("curr_estab", 0)
    if "tw" in b:
        out["net_time_wait"] = b["tw"]
    out["net_softnet_drops"] = round(max(b["softnet_dropped"] - a["softnet_dropped"], 0) / dt, 3)
    out["net_softnet_squeeze"] = round(max(b["softnet_squeeze"] - a["softnet_squeeze"], 0) / dt, 3)
    if a.get("cpu_softirq") and len(a["cpu_softirq"]) == len(b["cpu_softirq"]):
        pct = []
        for (sa, ta), (sb, tb) in zip(a["cpu_softirq"], b["cpu_softirq"]):
            pct.append(100.0 * (sb - sa) / max(tb - ta, 1))
        out["softirq_max_cpu_pct"] = round(max(pct), 2)
        out["softirq_avg_cpu_pct"] = round(sum(pct) / len(pct), 2)
    if "conntrack" in b:
        out["conntrack_fill_pct"] = round(100.0 * b["conntrack"][0] / max(b["conntrack"][1], 1), 2)
    return out


def ss_probe(port=3306, role="server"):
    """RTT / retransmits of established connections on the MySQL port."""
    flt = f"( sport = :{port} )" if role == "server" else f"( dport = :{port} )"
    try:
        r = subprocess.run(["ss", "-tinH", "state", "established", flt], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    rtts, retrans, local = [], 0, 0
    for m in re.finditer(r"(?<!min)rtt:([\d.]+)/[\d.]+", r.stdout):
        rtts.append(float(m.group(1)))
    # the lowest RTT each connection ever saw: the path itself, without the server's queueing (a saturated server adds its
    # run-queue wait to every smoothed RTT)
    mins = sorted(float(x) for x in re.findall(r"minrtt:([\d.]+)", r.stdout))
    for m in re.finditer(r"retrans:\d+/(\d+)", r.stdout):
        retrans += int(m.group(1))
    if not rtts:
        return {"mysql_conns": 0}
    rtts.sort()
    return {"mysql_conns": len(rtts), "rtt_avg_ms": round(sum(rtts) / len(rtts), 3), "rtt_p95_ms": rtts[int(0.95 * (len(rtts) - 1))],
            **({"rtt_min_ms": mins[len(mins) // 2]} if mins else {}),
            "rtt_max_ms": rtts[-1], "conn_retrans_total": retrans}
