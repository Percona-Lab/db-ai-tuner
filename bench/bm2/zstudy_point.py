#!/usr/bin/env python3
"""One result line for zstudy.sh: HammerDB NOPM/TPM, NEW_ORDER latency, InnoDB and swap activity over the measured
window (snap0 = end of ramp-up, snap1 = end of run), averages from vmstat/iostat over the same window.
usage: zstudy_point.py <dir> <id> <mode> <bp GB> <oom count>"""
import json
import re
import sys

o, pid, mode, bp, oom = sys.argv[1:6]
r = {"id": pid, "mode": mode, "bp_gb": float(bp), "outcome": "oom-killed" if int(oom) else "ok"}


def read(p):
    try:
        return open(f"{o}/{p}").read()
    except OSError:
        return ""


h = read(f"hdb-{pid}.log")
m = re.search(r"achieved (\d+) NOPM from (\d+) MySQL TPM", h)
r["nopm"], r["tpm"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
bb = read(f"bb-{pid}.log")
m = re.search(r"BBSUMMARY.*", bb)
if m:  # BenchBase (WL=am): requests/s in the measured window and latency over all transaction types
    kv = dict(x.split("=", 1) for x in m.group(0).split()[1:] if "=" in x)
    for k in ("throughput_rps", "goodput_rps", "p95_ms", "p99_ms", "avg_ms"):
        if k in kv:
            try:
                r[k] = float(kv[k])
            except ValueError:
                pass
prof = read(f"prof-{pid}.log")
prof = prof[prof.rfind("SUMMARY OF"):] if "SUMMARY OF" in prof else prof
for proc, key in (("NEWORD", "neword"), ("PAYMENT", "payment")):
    m = re.search(rf"PROC: {proc}\s+CALLS:[^\n]*\s+P99: ([\d.]+)ms\s+P95: ([\d.]+)ms", prof)
    if m:
        r[f"{key}_p99_ms"], r[f"{key}_p95_ms"] = float(m.group(1)), float(m.group(2))
    m = re.search(rf"PROC: {proc}\s+CALLS:[^\n]*AVG: ([\d.]+)ms", prof)
    if m:
        r[f"{key}_avg_ms"] = float(m.group(1))
try:
    s0, s1 = json.loads(read(f"snap0-{pid}.json")), json.loads(read(f"snap1-{pid}.json"))
    secs = 900.0
    d = {k: s1["status"].get(k, 0) - s0["status"].get(k, 0) for k in s1["status"]}
    r["data_reads_s"] = round(d.get("Innodb_data_reads", 0) / secs)
    r["data_writes_s"] = round(d.get("Innodb_data_writes", 0) / secs)
    rr = d.get("Innodb_buffer_pool_read_requests", 0)
    r["bp_miss_pct"] = round(100.0 * d.get("Innodb_buffer_pool_reads", 0) / rr, 3) if rr else None
    r["bp_wait_free"] = d.get("Innodb_buffer_pool_wait_free")
    for k in ("pswpin", "pswpout", "zswpin", "zswpout", "zswpwb"):
        if s1.get(k) is not None and s0.get(k) is not None:
            r[f"{k}_s"] = round((s1[k] - s0[k]) / secs, 1)   # pages per second (disk swap: pswp*, compressed: zswp*)
    r["mysqld_rss_gb"] = round(s1.get("mysqld_rss_kb", 0) / 1048576, 2)
    r["mysqld_swap_gb"] = round(s1.get("mysqld_swap_kb", 0) / 1048576, 2)
    r["swap_used_gb"] = round(s1.get("swap_used_kb", 0) / 1048576, 2)
    r["mem_avail_gb"] = round(s1.get("mem_avail_kb", 0) / 1048576, 2)
    if s1.get("zswapped_kb"):
        r["zswap_pool_gb"] = round(s1["zswap_kb"] / 1048576, 2)
        r["zswap_stored_gb"] = round(s1["zswapped_kb"] / 1048576, 2)
        r["zswap_ratio"] = round(s1["zswapped_kb"] / max(s1["zswap_kb"], 1), 2)
    if s1.get("zram_mm_stat"):
        orig, compr, used = (int(x) for x in s1["zram_mm_stat"][:3])
        r["zram_stored_gb"], r["zram_used_gb"] = round(orig / 2**30, 2), round(used / 2**30, 2)
        r["zram_ratio"] = round(orig / max(used, 1), 2)
except (ValueError, KeyError):
    r["snap_error"] = True
# vmstat: the rows after the ramp-up (10 s samples; the first line is the since-boot average)
rows = [ln.split() for ln in read(f"vmstat-{pid}.log").splitlines() if ln.strip() and ln.split()[0].isdigit()][1:]
win = rows[30:120]
if win:
    avg = lambda i: round(sum(int(x[i]) for x in win) / len(win), 1)
    r.update({"cpu_us": avg(12), "cpu_sy": avg(13), "cpu_id": avg(14), "cpu_wa": avg(15), "si_kbs": avg(6), "so_kbs": avg(7)})
# iostat: per-device reads/s and r_await over the window (sdb = datadir, sda = /data with the zswap backing file)
dev = {}
for ln in read(f"iostat-{pid}.log").splitlines():
    f = ln.split()
    if f and f[0] in ("sda", "sdb"):
        dev.setdefault(f[0], []).append(f)
hdr = next((ln.split() for ln in read(f"iostat-{pid}.log").splitlines() if ln.startswith("Device")), None)
if hdr:
    for d, samples in dev.items():
        w = samples[31:121]
        if w:
            for col, key in (("r/s", "r_s"), ("w/s", "w_s"), ("r_await", "r_await_ms"), ("%util", "util")):
                if col in hdr:
                    i = hdr.index(col)
                    r[f"{d}_{key}"] = round(sum(float(x[i]) for x in w) / len(w), 1)
if r.get("throughput_rps") and r.get("cpu_us") is not None:
    r["cpu_pct_per_krps"] = round((r["cpu_us"] + r["cpu_sy"]) / (r["throughput_rps"] / 1000), 3)
if r["nopm"] and r.get("cpu_us") is not None:
    r["cpu_pct_per_knopm"] = round((r["cpu_us"] + r["cpu_sy"]) / (r["nopm"] / 1000), 3)
print(json.dumps(r))
