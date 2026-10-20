#!/usr/bin/env python3
"""Summarize one time-limited Insert Benchmark step over the measured window only.

ibench_summary.py <outdir> <clients> <warmup_s> <duration_s> [target_inserts_per_s_per_client]

iibench.py prints one line per second per client (17 tab-separated fields):
 i_sec t_sec i_ips t_ips i_dps t_dps i_qps t_qps max_i max_d max_q t_ins t_del t_query retry_i retry_d retry_q
(max_* = largest statement response time in that interval, microseconds; t_* = cumulative counts).
Rates come from the cumulative counters at the window edges; latency from the per-interval maxima (the response-time
metric Mark Callaghan charts). The sampler file adds the InnoDB state behind the result.
"""
import os
import statistics
import sys


def rows(path):
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 17:
                continue
            try:
                out.append([float(x) for x in p])
            except ValueError:
                pass
    return out


def at(rs, t):
    """last row with t_sec <= t (cumulative counters at that time)"""
    best = None
    for r in rs:
        if r[1] <= t:
            best = r
    return best


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def main():
    outdir, n, w, d = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    target = float(sys.argv[5]) if len(sys.argv) > 5 else 0
    ins = dels = qs = 0.0
    per_sec = {}          # second -> [inserts/s sum, queries/s sum, max insert us, max query us]
    ok_clients = 0
    for c in range(1, n + 1):
        rs = rows(os.path.join(outdir, f"o.ib.{c}"))
        a, b = at(rs, w), at(rs, w + d)
        if not rs or a is None or b is None or b[1] <= a[1]:
            print(f"client {c}: no data for the measured window")
            continue
        ok_clients += 1
        span = b[1] - a[1]
        ins += (b[11] - a[11]) / span
        dels += (b[12] - a[12]) / span
        qs += (b[13] - a[13]) / span
        for r in rs:
            if w < r[1] <= w + d:
                k = int(round(r[1]))
                e = per_sec.setdefault(k, [0.0, 0.0, 0.0, 0.0])
                e[0] += r[2]
                e[1] += r[6]
                e[2] = max(e[2], r[8])
                e[3] = max(e[3], r[10])
    if ok_clients < n:
        print(f"ibench: only {ok_clients} of {n} clients produced data: no result")
        return 1
    secs = [per_sec[k] for k in sorted(per_sec)][1:-1]   # drop partial edge seconds
    ips_s = [e[0] for e in secs]
    print(f"ibench_inserts_per_s = {ins:.1f}")
    print(f"ibench_deletes_per_s = {dels:.1f}")
    if qs > 0:
        print(f"ibench_queries_per_s = {qs:.1f}")
    if target:
        print(f"ibench_insert_target_per_s = {target * n:.0f} (achieved {100 * ins / (target * n):.1f}%)")
    if len(ips_s) > 2 and statistics.mean(ips_s) > 0:
        print(f"ibench_insert_rate_cv_pct = {100 * statistics.pstdev(ips_s) / statistics.mean(ips_s):.1f}")
        print(f"ibench_insert_rate_p1 = {pct(ips_s, 0.01):.0f}")
        print(f"ibench_seconds_below_half_rate = {sum(1 for v in ips_s if v < 0.5 * statistics.mean(ips_s))}")
    mi = [e[2] / 1000.0 for e in secs if e[2] > 0]
    if mi:
        print(f"ibench_insert_max_ms_p50 = {pct(mi, 0.50):.1f}")
        print(f"ibench_insert_max_ms_p99 = {pct(mi, 0.99):.1f}")
        print(f"ibench_insert_max_ms_max = {max(mi):.1f}")
    mq = [e[3] / 1000.0 for e in secs if e[3] > 0]
    if mq and qs > 0:
        print(f"ibench_query_max_ms_p50 = {pct(mq, 0.50):.1f}")
        print(f"ibench_query_max_ms_p99 = {pct(mq, 0.99):.1f}")

    # server state during the window (sampler: t hll dirty total wait_free data_reads data_writes iowait_jiffies all_jiffies)
    sp = os.path.join(outdir, "samples.tsv")
    smp = []
    if os.path.exists(sp):
        with open(sp) as f:
            for line in f:
                p = line.split()
                if len(p) in (9, 12):
                    if len(p) == 9:   # older sampler format: no checkpoint age / change buffer size
                        p = p[:7] + ["0", "0", "0"] + p[7:]
                    try:
                        smp.append([float(x) for x in p])
                    except ValueError:
                        pass
    win = [s for s in smp if w <= s[0] <= w + d + 5]
    if len(win) >= 2:
        f0, f1 = win[0], win[-1]
        dt = max(1.0, f1[0] - f0[0])
        print(f"ibench_hll_start = {f0[1]:.0f}")
        print(f"ibench_hll_end = {f1[1]:.0f}")
        print(f"ibench_hll_max = {max(s[1] for s in win):.0f}")
        print(f"ibench_dirty_pct_avg = {statistics.mean(100 * s[2] / s[3] for s in win if s[3]):.1f}")
        print(f"ibench_wait_free_per_s = {(f1[4] - f0[4]) / dt:.1f}")
        print(f"ibench_data_reads_per_s = {(f1[5] - f0[5]) / dt:.0f}")
        print(f"ibench_data_writes_per_s = {(f1[6] - f0[6]) / dt:.0f}")
        jt = f1[11] - f0[11]
        if jt > 0:
            print(f"ibench_iowait_pct = {100 * (f1[10] - f0[10]) / jt:.1f}")
        # steady state: compare the first and last third of the window; growth = deferred work accumulating
        k = len(win) // 3
        if k >= 2:
            a, b = win[:k], win[-k:]
            m = lambda xs, f: statistics.mean(f(x) for x in xs)
            series = [("dirty_pct", lambda x: 100 * x[2] / x[3] if x[3] else 0),
                      ("hll", lambda x: x[1]),
                      ("ckpt_age_pct", lambda x: 100 * x[7] / x[8] if x[8] else 0),
                      ("ibuf_pages", lambda x: x[9])]
            for name, f in series:
                va, vb = m(a, f), m(b, f)
                print(f"ibench_steady_{name} = first third {va:.1f} last third {vb:.1f}")
    # change buffer: "Ibuf: size S, free list len F, seg size G, M merges" + "merged operations: insert I, delete mark DM, delete D"
    import re
    def ibuf(name):
        try:
            t = open(os.path.join(outdir, name)).read()
        except OSError:
            return None
        m = re.search(r"Ibuf: size (\d+), free list len (\d+), seg size (\d+), (\d+) merges", t)
        o = re.search(r"merged operations:\s*insert (\d+), delete mark (\d+), delete (\d+)", t)
        if not m:
            return None
        return [int(x) for x in m.groups()] + ([int(x) for x in o.groups()] if o else [0, 0, 0])
    i0, i1 = ibuf("ibuf.start"), ibuf("ibuf.end")
    if i1:
        print(f"ibench_ibuf_size_pages_end = {i1[0]}")
        print(f"ibench_ibuf_seg_size_pages_end = {i1[2]}")
    if i0 and i1:
        print(f"ibench_ibuf_merges_per_s = {(i1[3] - i0[3]) / d:.1f}")
        print(f"ibench_ibuf_merged_ops_per_s = {(i1[4] + i1[5] + i1[6] - i0[4] - i0[5] - i0[6]) / d:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
