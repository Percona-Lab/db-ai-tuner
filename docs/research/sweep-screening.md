# Sensitivity screening before refinement (2026-10-20)

**Question.** Sweeps measure 2-3 values of a setting. Most of them find nothing: can a cheaper sensitivity test come
first, with refinement only where the goal responds - without stopping at a local optimum?

**Data.** 105 numeric agent / LLM sweeps with a known parent value from sessions #1-#144 (241 sweep runs;
`docs/research/data/sweeps-2026-10-20.json`, replay: `tools/replay_sweep_screening.py`).

| Response | Sweeps |
|---|---|
| flat: every value within noise of the parent | 77 (73%) |
| only worse | 20 |
| a value better beyond noise | 8 (5 at the far end, 2 interior, 1 rising) |

innodb_thread_concurrency was swept 48 times (34 flat); innodb_max_dirty_pages_pct_lwm 5 of 5 flat, innodb_lru_scan_depth
4 of 4, innodb_read_io_threads 2 of 2.

**Screening rules replayed** (a side whose screening point is within noise is not refined further):

| Rule | Runs | Saved | Gains missed |
|---|---|---|---|
| nearest value per side | 155 | 36% | #27 thread_concurrency=4 (+5%, confirmed), #93, #94 (both not confirmed by their repeat) |
| widest value per side | 155 | 36% | #63 purge_threads=2 (never repeated), #111 thread_cache_size=256 (+1.4% at ±1.2%) |
| widest + hint band (½ noise < effect <= noise: measure the next value too) | 173 | 28% | #111 only |

Nearest-first stops at a local optimum when the optimum lies at the far end (#27: concurrency 8 flat, 4 +5%). The widest
step gives the strongest signal and catches it. #111 is not a real miss: thread_cache_size 256 and 1100 act the same way
(more cached threads), their mean (+0.85%) is within noise. No session swept the same setting twice, so there is no data
yet on interactions.

**Implemented** (agent sweeps; sweeps a person asks for still measure every value):

1. Sensitivity first: on each side of the parent value the widest step, then the rest nearest first.
2. Within half the noise threshold -> the side is flat, the rest is skipped. Between half and the full threshold -> a
   hint: the next value is measured too. Beyond -> refine.
3. A worse far point does not rule out nearer values (optimum shape): they are still measured. The optimum extension
   (one more step beyond a best value at the far end, at most twice) runs after all points.
4. More-is-better settings (saturating): after a sensitive widest step, refine upward from the smallest value and stop
   at the knee - the first value within noise of the best; larger values only cost more.
5. Good enough over exact: among values within noise of the best, keep the cheapest (smaller for memory-costing /
   saturating settings), then the roundest, then the smallest change.
6. Round values: agent, rule and LLM values with three or more significant digits snap to round ones (1072 -> 1000,
   6311 -> 6000; sizes to powers of two / 512 MiB / whole GiB; thread-like counts to powers of two). Two-digit values
   (24 = 2 x 12 cores) are kept. Benchmark requirements and max_connections are never rounded.
7. Interactions (local optimum across settings): a change tried without effect is tried again when the measured share
   of time it acts on grew by more than the noise threshold since that run.

**Next validation:** ground-truth curves on bare metal (buffer pool 2-24 GB uniform and skewed; thread concurrency /
thread pool size under contention) to compare runs used, distance to the knee / optimum and goal reached, old vs new.
