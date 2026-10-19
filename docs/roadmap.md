
## Parameter interactions (agreed 2026-10-19; build with the Battery 1 improvements)

Problem: the tuner changes one setting at a time from the best known configuration. A change rejected in one context
(e.g. `join_buffer_size=8M` with AHI off) is never retried after the context changes (AHI kept later). Documentation lists
no interactions; research (OtterTune: Lasso with pairwise knob products, GP surrogates; fANOVA / SHAP importance studies)
learns them from many measured configurations.

1. **Context re-test**: interaction groups from the mechanism map (latching/concurrency, redo/flush, memory,
   allocator/THP, optimizer). When a change is kept, rejected experiments in the same group get one more run.
2. **Fractional factorial at the end**: top 4-7 candidates (kept + near-noise) in a 2-level fractional factorial
   (8 runs, resolution III; 16 runs resolution IV) -> main effects + strongest 2-way interactions, best combination confirmed.
3. **Interaction memory across sessions (learn from data)**: store the state of related settings with every experiment;
   accumulate per workload type the effect of X with Y on vs off; use as priors for (1). Grows with every battery.
   First look at existing data (2026-10-19): 41 experiment x context pairs seen in both states, confounded by
   workload / machine (e.g. thread pool -11..-18% with log_writer_threads=OFF vs ~-2% with ON) - suggestive only.
4. **Battery 2 targeted factorials**: join_buffer x AHI (TPC-H), log_writer_threads x flush_log_at_trx_commit (OLTP write),
   allocator x THP (1024 connections) to measure interaction sizes on clean data.

## Diagnostic runs (proposed 2026-10-19; decide with the Battery 1 evaluation)

Today instrumentation is fixed: default performance_schema (mutex / rwlock wait timing off), passive OS sampling, and a
20 s CPU profile at the end of warm-up. Nothing drills deeper automatically; `tuning-agent diag waits on` is manual.

Idea: when the diagnosis names a broad bottleneck but not its mechanism, spend one run on a **diagnostic run**: the current
best configuration with extra instrumentation for that area. Its throughput is never compared: it is excluded from means,
verdicts, noise and the best configuration, and the instrumentation is removed (and checked) before the next measured run.

| Broad finding | Drill-down | Narrows the next experiment to |
|---|---|---|
| CPU busy, spin / futex / scheduler share high, or "sync" waits untimed | wait/synch/% timed + `events_waits_summary_by_instance`, `SHOW ENGINE INNODB MUTEX`, innodb_metrics latch modules | which latch: lock_sys, trx_sys, log, buf_pool LRU, fil_system, dict → spin settings, thread pool / concurrency, purge, BP instances, table cache instances |
| Threads waiting but CPU not saturated | off-CPU profile (eBPF `sched_switch`, e.g. bcc `offcputime`) | blocking cause: I/O, locks, fsync, network |
| I/O-bound | per-file I/O latency (`file_summary_by_instance`, eBPF `biolatency` / `fileslower`), innodb_metrics buffer / flush / log modules | reads vs flushing vs redo vs temp files → BP size, flushing, redo size, tmp settings |
| Row-lock waits | `data_lock_waits` sampling, `innodb_print_all_deadlocks` | hot rows (schema advice, outside knob scope) vs lock-holding time (commit path) |
| Slow analytical statements | `EXPLAIN ANALYZE` + optimizer trace of the top digests | join / sort / temp-table sizes, optimizer switches |
| Memory / allocator in the CPU profile | memory/% instruments, allocator statistics | allocator choice, per-session buffers |

Rules: only when the candidate experiments for the finding address different mechanisms (the drill-down must be able to
change the plan); at most one per area per session and ≤ 10% of the run budget; shorter run allowed (no comparison needed).
The drill-down result is passed to the engine (mechanism-specific diagnoses) and to the LLM context.

Evaluate before building: in Battery 1, count sessions where (a) the diagnosis was broad and (b) experiments for several
mechanisms were tried before one helped, e.g. the `innodb_thread_concurrency` sweep (20 runs, median 0%) and spin-loop
experiments (mostly noise). If that wasted budget exceeds the cost of a diagnostic run, build it, starting with latch
contention and off-CPU profiling.

## Results depend on machine class and concurrency (noted 2026-10-19)

Most experiments so far ran on 2-8 vCPU VMs at 4-64 client threads, where latch and lock contention stays mostly hidden.
Contention-related changes (thread pool, spin settings, `innodb_thread_concurrency`, buffer pool instances, log writer
threads, CPU / IRQ affinity, allocators) may behave differently on high-core servers with hundreds or thousands of
connections. Conclusions from the 2026-10-18 review (e.g. "thread pool consistently harmful") hold for that range only.

Done: the experience prior now matches workload, CPU class **and** concurrency class (1-16 / 17-128 / 129+ connections);
evidence from other classes counts half and never demotes a change to the end of the queue.

To do in Battery 2: run the contention-sensitive workloads (O2/O3-style sysbench, HammerDB high VU) on the 16+ core
machines as well, at 64 and 1024 connections, and report each rule's outcome per machine class x concurrency class.

## Three kinds of I/O-bound InnoDB (user guidance, 2026-10-19)

Principle (user guidance): workload classes - I/O-bound and its kind, CPU-bound, concurrency class, OLTP/analytical -
are a convenience. Which settings are best is more complicated (hardware, data, access pattern, interactions), so
classes may only order experiments (relevance, experience weight, sweep size) and never exclude a candidate; every
conclusion comes from measurement.

An I/O-bound InnoDB workload is bound by one of: **commits** (redo fsync per transaction), **reads** (working set
misses the buffer pool) or **flushing dirty pages** (page cleaners cannot keep up: free-page waits, checkpoint age,
dirty pages at the limit). Each calls for different experiments.

Done: diagnoses `commit`, `io-read` and `io-flush`; rules carry the kind they address (mechanisms.py), and once a kind
is diagnosed, rules that only address generic "io" stop counting as relevant (bp-grow / read-ahead / read threads for
reads; io_capacity / LRU depth / redo size / dirty thresholds / doublewrite for flushing; trx_commit / log writer /
fdatasync for commits).

Next: report the kind per run on the run page and per study; design Battery 3 I/O tests so each kind is represented
(e.g. sysbench read-only on 10x RAM = read-bound; update-heavy on 2-3x RAM with small redo = flush-bound;
trx_commit=1 small transactions on slow fsync = commit-bound).

## Mechanism preconditions (user guidance, 2026-10-19)

Battery 1 and 2 TPC-H SF10 swept `innodb_io_capacity` (3 x 75 min) on a workload with 2946 read IOPS, 13 write IOPS
and no redo: the knob only paces flushing, so it could not act. Done: `mechanisms.missing_mechanism()` - settings whose
mechanism has nothing to act on (writes for flushing / redo / commit / purge / change buffer, disk reads for the read
path) move to the end as one-point validation checks with a note why; the LLM context carries the same activity facts.
Next: the run page shows "what limited this run" and "what each candidate acts on" side by side.
