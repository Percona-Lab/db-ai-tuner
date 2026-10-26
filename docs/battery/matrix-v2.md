# Case-base matrix v2 (draft for review, 2026-10-26)

**Purpose.** The similarity ranking learns from measured experiments (case base: 1,180 cases, 129 sessions). Most of them
are sysbench in memory at medium-high concurrency, so a new workload's "most similar states" are often sysbench ones. This
matrix fills the facet space the ranking and the rules need: **write class x concurrency x memory fit x goal**, with more
than one benchmark per class so no class is "what sysbench does".

Unlike suite v1 (a scorecard of the rule engine, one session per cell, fixed checks), every cell here is an ordinary tuning
session whose runs become cases: autotune with run length chosen automatically, rules + learnings + similarity (AI off,
except the explore cells), trade-offs after the safe phase.

## Facets and levels

| Facet | Levels (on the 12-vCPU / 32 GB pipeline boxes, buffer pool ~22 GB after the baseline) |
|---|---|
| Write class | read-only · read-mostly · writes · commit-heavy · analytics (latency goal) |
| Concurrency (per vCPU) | low 8 threads (0.7) · medium 48 (4) · high 256 (21) · extreme 1024 (85) |
| Memory fit (data / buffer pool) | in memory ~8 GB (0.35x) · partly cached ~60 GB (2.7x) · mostly on disk ~220 GB (10x) |
| Goal | throughput (most) · latency (analytics, TPC-C p99) |
| Storage | NVMe (atvbm, atvbm2, atvbmamd) · SATA SSD (atvbmmia) |
| CPU | Intel E-2286G (3 boxes) · AMD EPYC 4245P (atvbmamd) |

The full product is 5 x 4 x 3 = 60 cells (x2 storage x2 CPU). Instead: every pair of levels of write class, concurrency
and memory fit at least once (pairwise), plus the cells the rules know matter (extreme concurrency writes, commit-heavy on
SATA). **26 cells.**

## Cells

Grouped by box and data set, so each data set is loaded once and serves several cells (reset between runs from the
pristine copy; ~1 GB/min to load).

### atvbm (NVMe, Intel) - sysbench, in memory (8 x 4M rows, ~8 GB)

| Cell | Workload | Threads | Write class | Goal |
|---|---|---|---|---|
| M1 | oltp_point_select | 1024 | read-only | tps |
| M2 | oltp_read_only | 48 | read-only | tps |
| M3 | oltp_read_write | 256 | read-mostly | tps |
| M4 | oltp_write_only | 8 | writes | tps |
| M5 | oltp_update_non_index | 1024, max_connections 2048 | commit-heavy | tps |
| M6 | oltp_read_write, pareto (hot rows) | 48 | read-mostly | p95 latency |

### atvbm2 (NVMe, Intel) - sysbench, partly cached (16 x 16M, ~60 GB)

| Cell | Workload | Threads | Write class | Goal |
|---|---|---|---|---|
| P1 | oltp_read_only | 256 | read-only | tps |
| P2 | oltp_read_write | 48 | read-mostly | tps |
| P3 | oltp_write_only | 1024 | writes | tps |
| P4 | oltp_read_write | 8 | read-mostly | tps |
| P5 | oltp_update_index | 48 | commit-heavy | tps |

### atvbmamd (NVMe, AMD) - sysbench, mostly on disk (32 x 28M, ~220 GB) and TPC-H

| Cell | Workload | Threads | Write class | Goal |
|---|---|---|---|---|
| D1 | oltp_read_only, uniform | 48 | read-only | tps |
| D2 | oltp_read_write | 256 | read-mostly | tps |
| D3 | oltp_read_only, pareto | 1024 | read-only | tps |
| D4 | oltp_write_only | 8 | writes | tps |
| D5 | go-tpc TPC-H SF10 (~15 GB), 22 queries | 1 | analytics | total time |
| D6 | go-tpc TPC-H SF30 (~45 GB), 4 streams | 4 | analytics | total time |

### atvbmmia (SATA, Intel) - TPC-C and key-value

| Cell | Workload | Threads | Write class | Goal |
|---|---|---|---|---|
| C1 | go-tpc TPC-C 100 W (~10 GB, in memory) | 48 | commit-heavy | tpmC |
| C2 | go-tpc TPC-C 100 W | 256 | commit-heavy | tpmC |
| C3 | go-tpc TPC-C 1000 W (~100 GB, 4.5x) | 48 | commit-heavy | tpmC |
| C4 | go-tpc TPC-C 1000 W | 1024, max_connections 2048 | commit-heavy | tpmC |
| C5 | BenchBase YCSB-A (zipfian 50/50), 60 GB | 256 | writes | tps |
| C6 | BenchBase YCSB-C (read-only, zipfian), 60 GB | 8 | read-only | p99 latency |

### tuner-canary (8 vCPU) - HammerDB, plus the explore cells

| Cell | Workload | Threads | Write class | Goal |
|---|---|---|---|---|
| H1 | HammerDB TPROC-C 200 W | 64 | commit-heavy | NOPM |
| H2 | HammerDB TPROC-C 200 W | 512 | commit-heavy | NOPM |
| X1 | M3 again with `--explore` (AI ideas once the rules run out) | 256 | read-mostly | tps |

`--explore` also on D2 and C3 (one per class of bottleneck: CPU, read I/O, commit). The rest run with AI off: the cases
should show what the rules and the case base pick.

## Protocol (every cell)

* `tuning-agent autotune --bench P --max-runs 16 --tradeoffs --close` on a session started with `--ai off` (the three
  explore cells: `--explore`, AI on), `warmup = auto`, `duration = auto`, `reset` from the pristine copy, quiet check on.
* Hardware baseline (`baseline --apply`), not compiled defaults - the cases describe tuning from a sensible start.
* `[requires] max_connections` = 2x the threads for the 1024-thread cells (refusals make runs unreliable).
* Policy `benchmark` with trade-offs after the safe phase: both kinds of case. The session closes before the next data
  set is loaded (one session = one server version, benchmark, data set).
* Same server everywhere: Percona Server 8.4 (latest), so version is not a confounder; a later round repeats a subset
  on MySQL 8.4 and MariaDB 11.8.

## Time and cost

About 7 h per cell (run-length probe, calibration, 16 runs), plus data loads (~4 h for the 220 GB set, ~2 h for TPC-C
1000 W). Per box 5-6 cells: **about 2.5 days** with all four pipeline boxes and the canary in parallel. No AI cost
outside the 3 explore cells (a few dollars). After the head-to-head finishes (atvbm is free now).

## What it is checked against

* Coverage: every facet pair has cases from at least 2 sessions (the similarity term needs 2).
* The decision corpus and the nightly regression check gain the new sessions' decision points (`regress.py add`).
* Re-learn the feature weights (`learn_weights.py`) on the larger case base and re-check against the decision corpus.

## Open questions for review

1. 26 cells / ~2.5 days - or a first wave of the 12 cells that fill the biggest gaps (read-only on disk, writes at low
   concurrency, analytics, YCSB) and the rest after a look?
2. Percona Server 8.4 only in this round (versions later), or spread versions across cells now?
3. Latency goals on 4 cells (M6, C6, D5, D6): enough, or a latency twin for more throughput cells?
