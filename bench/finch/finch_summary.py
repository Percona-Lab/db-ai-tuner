#!/usr/bin/env python3
"""Summarise a Finch CSV stats file (stats.freq intervals) over the measured window.
Usage: finch_summary.py <csv> <warmup seconds>
Prints: FINCH tps=.. qps=.. r_qps=.. w_qps=.. p99_ms=.. r_p99_ms=.. w_p99_ms=.. c_p99_ms=.. errors=.. intervals=..
Rates are averaged over the intervals that start at or after the warm-up (weighted by interval length);
p99 values are the mean of the per-interval P99 (Finch resets its histograms every interval)."""
import csv, sys
rows = [r for r in csv.DictReader(open(sys.argv[1])) if r.get('interval')]
w = float(sys.argv[2])
def f(r, k):
    v = (r.get(k) or '0').replace(',', '')
    return float(v) if v else 0.0
# with several computes Finch also writes per-compute rows; keep the combined ones when present
comb = [r for r in rows if 'combined' in r.get('compute', '')]
rows = comb or rows
win = [r for r in rows if f(r, 'runtime') - f(r, 'duration') >= w - 1]
if not win:
    sys.exit("FINCH no intervals after warm-up")
T = sum(f(r, 'duration') for r in win)
avg = lambda k: sum(f(r, k) * f(r, 'duration') for r in win) / T
mean = lambda k: sum(f(r, k) for r in win) / len(win)
def p(k):
    for c in (k, k.replace('P', 'p')):
        if c in win[0]:
            return mean(c) / 1000.0
    return -1
print("FINCH tps=%.1f qps=%.1f r_qps=%.1f w_qps=%.1f p99_ms=%.3f r_p99_ms=%.3f w_p99_ms=%.3f c_p99_ms=%.3f errors=%d intervals=%d seconds=%.0f"
      % (avg('TPS'), avg('QPS'), avg('r_QPS'), avg('w_QPS'), p('P99'), p('r_P99'), p('w_P99'), p('c_P99'),
         sum(f(r, 'errors') for r in win), len(win), T))
