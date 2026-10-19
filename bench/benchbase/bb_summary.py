#!/usr/bin/env python3
"""bb_summary.py <results dir>: one parseable line from BenchBase's *.summary.json (newest), for the profile [metrics] regexes.

    BBSUMMARY bench=ycsb throughput_rps=12345.6 goodput_rps=12345.6 requests=3703680 errors=0 avg_ms=1.234 p50_ms=0.9 p95_ms=3.1 p99_ms=6.2 max_ms=88.0
throughput_rps = measured requests / measured seconds (warm-up excluded); latencies are over the measured window.
"""
import glob
import json
import os
import sys

d = sys.argv[1]
files = sorted(glob.glob(os.path.join(d, "*.summary.json")), key=os.path.getmtime)
if not files:
    print("BBSUMMARY_MISSING no summary.json in", d)
    sys.exit(1)
s = json.load(open(files[-1]))


def find(obj, *words):
    for k, v in obj.items():
        kl = k.lower()
        if all(w in kl for w in words):
            return v
    return None


lat = s.get("Latency Distribution", {})
us = lambda *w: (find(lat, *w) or 0) / 1000.0
thr = find(s, "throughput") or 0
good = find(s, "goodput") or 0
req = find(s, "measured requests") or 0
# BenchBase goodput counts successes of the warm-up too (it divides by the measured time only): reported for reference, not for tuning
print("BBSUMMARY bench=%s throughput_rps=%.2f goodput_incl_warmup_rps=%.2f requests=%d avg_ms=%.3f p50_ms=%.3f p95_ms=%.3f p99_ms=%.3f max_ms=%.3f" % (
    s.get("Benchmark Type", "?"), thr, good, req, us("average"), us("median"), us("95th"), us("99th"), us("maximum")))
