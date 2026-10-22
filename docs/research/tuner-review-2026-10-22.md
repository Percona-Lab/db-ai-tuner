# Tuner review: what it decided, and what it should have (2026-10-22)

Seven reviews read every session on the server (75 sessions, 1,185 runs). Four looked at outcomes:
- the r1 suite;
- bare-metal lab and pipeline sessions;
- the external user's sessions;
- 20 canaries of the same workload.

Three audited the decisions one by one. At each decision point they reconstructed what was known then and asked what an
expert would have tried next. Across ~320 decision points they agreed with roughly 30-40%. The divergences were:
- **V:** about 80 runs on ideas that could not show an effect above the noise given what was measured.
- **S:** about 40 runs after the answer was known; the session should have stopped and advised.
- **R:** about 30 ranking errors.
- **G:** a short list of missing rules.

Fixes below are in agent / server builds from 2026-10-22. Each line gives the evidence, the root cause and the remedy.

## Measurement

| # | Issue (evidence) | Remedy | Status |
|---|---|---|---|
| M1 | **Throughput doubled with the binary log on.** `Handler_commit` counts two commits per transaction (lab 221: 84,579 "tps" for 42,282). Binlog-off runs were marked +100% unreliable twice and re-measured as "CPU steal". | Halve when `log_bin` is on. Re-measure an unreliable run only for transient causes, and name the reason. | fixed |
| M2 | **Huge pages invisible to memory accounting.** RSS/PSS exclude hugetlb (a 188 GB pool measured 17 GB). The hugepage pool was counted as "OS" memory (24 GiB on a 31 GiB host), so every hugepages config was over budget, and baseline/NUMA fit checks went wrong. | mysqld `HugetlbPages` added to its memory. The hugepage pool is excluded from memory used outside mysqld (budget and baseline). | fixed |
| M3 | **Unresolved off-CPU stacks hid row-lock waits.** They were filed as "other futex / concurrency" (KV-1: 45-51% of time, really row locks per InnoDB counters; user 191/169: 33-48%). | Untagged "futex, unresolved stack" component; row-lock counters reconcile it. | fixed |
| M4 | **Decisions on unprofiled runs.** "CPU (not profiled)" was one block matching every CPU lever. 30 of 66 r1 decisions; after a revert the profile lookup used the wrong configuration (user 194: a 79% commit wait hidden). | Profiles come from the configuration in effect, else the most recent profile of any configuration (marked). | fixed |
| M5 | **Queueing read as workload or network properties.** Latency classified a 4096-connection read-write load as OLAP (HC-3: join-buffer and tmp-table runs). The in-load TCP RTT (20-25 ms at 100% CPU) became "network-rtt" (HC-2: slow-start and BBR runs). | OLAP and mixed need rows per statement. Network RTT comes from `minrtt` (now recorded), or the smoothed RTT only with CPUs to spare. | fixed |
| M6 | **Calibration noise re-inflated.** The cold first run that calibration set aside re-entered later noise estimates (user 194: ±5.1% became ±11.3%). | Excluded from every later estimate. | fixed |
| M7 | **Block-latency maxima of 2^64 ns.** | Already bounded by the trace window in current agents. | fixed earlier |
| M8 | **No client-host metrics in client-server cells.** Client saturation could never be detected. sysbench with 4096 threads failed to start in 30 s (HC-2: the thread-pool run never measured). Traffic went over the public network. | Suite: client-role agent on the client, private IPs (grants and firewall follow), `--thread-init-timeout=600` from 1024 threads (suite and pipeline). | fixed (r2) |

## Verdicts and noise

| # | Issue (evidence) | Remedy | Status |
|---|---|---|---|
| E1 | **Noise threshold ratcheted up.** Repeats of configurations selected for looking good (confirmations, re-checks) were pooled as noise, and the prior used `max()`. Canaries: CV 0.47% gave a 6% threshold; the real +7.8% winner was lost in #195. | Selected first runs excluded from noise. Prior pooled with the session by degrees of freedom. Old prior entries (v1) ignored. | fixed |
| E2 | **Winner's curse in confirmations.** Repeats averaged with the lucky first run (log-writer-threads confirms −2.8% on average; 11 runs called "DRIFT"). | Two-stage test: confirmation judged on the repeats alone, one-sided ~95%. "Likely lucky" instead of DRIFT when the only earlier run is the one under test. | fixed |
| E3 | **"Not steady" cancelled any verdict.** Session 210: baseline +419% vs defaults became neutral, so stock defaults stayed "best". Lab: redo-grow +19.9%, binlog-off +13%; regressions of −22% to −75% became "neutral". | Kept beyond max(4 × noise, 25%). Direction-aware: unpaid deferred work cannot explain a regression, and a still-warming window cannot explain an improvement. Relative: a candidate accumulating no more debt than its parent is compared alike. | fixed |
| E4 | **Efficiency gains invented.** Near-zero metrics (RO-1 control: 0.0014 → 0.0008 KiB/trx as "−40%", 6 runs + 6 confirms). Goal losses up to −10% kept. Not-steady ignored; confirmations checked only the goal. | Metric must be under load and material. Goal no worse than half the threshold. Not kept when deferrable I/O "saved" while debt grew. The saving must hold on the repeats. | fixed |
| E5 | **Best configuration below the start.** User 169/174 −3.8%/−4.5%, HC-2 −2.5%, RW-4 −1.3%. Single unconfirmed runs became best (#202). | Best never more than 0.5% below the calibrated start. Improvements count once confirmed. Sweep efficiency points get a confirmation run. | fixed |
| E6 | **Hypothesis outcomes inflated.** "Supported" was recorded for goal losses and efficiency-only results, and never updated when a confirmation reversed the verdict. This taught the learner. | Supported = goal improved AND targeted time shrank. New outcomes: improved-unverified, efficiency, not-confirmed (on reversal). | fixed |
| E7 | **Experience ignored effect size.** The thread pool at −21% to −73% in 8 sessions was counted "neutral" (after not-steady), and efficiency gains counted as wins. | Measured direction (`unsteady_verdict`) counted. Penalty grows with the mean loss. Efficiency counted separately. Hypothesis track record up to ±4 points, more after 3+ failures. | fixed |
| E8 | **Confirmed winner dropped.** drop-unearned removed it when the threshold moved (#198 −6.2%, #201 −4.6%). | Hysteresis: once confirmed and still above its parent, kept. | fixed |

## Planning and ranking

| # | Issue (evidence) | Remedy | Status |
|---|---|---|---|
| P1 | **Trade-off phase starved.** 10 canaries in a row never reached a trade-off, because every relevance-3 safe item was "targeted". | Override only at relevance 4, at most twice. | fixed |
| P2 | **Benchmark policy ran evidence-free safe ideas before evidence-backed trade-offs.** trx-commit-2, ps-off and doublewrite-off had the top scores in 5 sessions and ran last or never. | Trade-off first when it scores 4+ higher, or the safe idea has no evidence. | fixed |
| P3 | **Concurrency limiters on commit-bound loads.** 9 picks, 0 wins, −15% to −75%. | Not run when commits take ≥40% of the time and CPU is below 90%. | fixed |
| P4 | **Thread pool / concurrency limits at low concurrency.** Lost in every 64-connection co-located cell; won only at 1024 connections. Limits ≥ connections were no-ops. | Thread pool only at ≥16 × vCPUs connections. Concurrency sweep values below the connection count; skipped after the pool regressed. | fixed |
| P5 | **Broad tags.** "cpu"/"io"/"commit" gave join-buffer, tmp-tables, hugepages, bp-instances, noatime (the bundle), doublewrite-off and dirty writeback 80%+ shares. The run-queue boost went to every "sched-delay" lever. | Specific tags. Queue share only for queue levers. | fixed |
| P6 | **Ideas with no mechanism ran anyway** (validation-only, mechanism provably absent, `[requires]` settings such as max_connections), plus 14 AI ideas with 0 wins. | Mechanism absent → advice, never a run. Prerequisites never proposed. AI ideas capped at 3 per session unless one wins. | fixed |
| P7 | **No stop.** User 194 ran 27 runs (~13.5 h) after the answer was known; 174 and 191 similar. | Storage-bound stop under safe policy (commits ≥50%, grouping complete, writes ≥5 ms, after the bundle). Ceiling stop (half the share an idea acts on below the noise, after 3 fruitless runs). Fruitless-streak stop. Benchmark policy switches to trade-offs instead. | fixed |
| P8 | **Mechanism gates on thresholds instead of measured cost.** Write-ahead sweep without redo reads (10 runs, 0 wins). Read-io-threads at read-ahead 0 (7 runs). LRU depth without free-page waits (6). bp-shrink on 0.3 MB/s swap-in. log-writer-threads-off on commit-bound / high-concurrency loads (4 picks, −6% to −46%). ps levers gated on busy ≥ 80%. jemalloc without allocator share. | Each gated on what it acts on. | fixed |
| P9 | **numa-single-node fit check.** mysqld CPU% is a share of the host (read as "0.2 CPUs" for 6.8). PSS ignored huge pages (17 GiB for a 188 GiB pool). No remote-access evidence (0.0-0.47%). | Fixed fit checks; needs ≥5% remote allocations. | fixed |
| P10 | **Scheduling delay diagnosed on an idle host** (13% of runnable time, 1 thread waiting, 77% idle). | Severity needs a queue that matters. | fixed |
| P11 | **Missing diagnoses: purge falling behind** (history list 23M → 43M) **and redo pressure at 50-70% checkpoint age** with a busy disk. | Purge and redo-pressure diagnoses; purge levers tagged. | fixed |
| P12 | **Flush-bound without redo pressure got no io_capacity increase** (WI-1: 94.6% dirty, 34 free-page waits/s). | Second io-capacity-up trigger. | fixed |
| P13 | **Memory model assumed 2 × vCPUs active sessions** (one TPC-H stream treated as 16). The only AN-1 win (bigger buffer pool) and a measured, swap-free +58% (lab 167/178) became memory trade-offs. | Active connections from measured peak running threads. | fixed |
| P14 | **Buffer-pool chunk companion lost on restores / drop-unearned** (user 191: pool 45 → 48 GiB). | Recomputed on any change to instances, size or chunk; dropped only with them. | fixed |

## Not yet done (follow-ups)

- **Large differences in unsteady windows:** re-measure once with a longer warm-up rather than deciding.
- **Baseline drift:** A/B/A re-checks that alternate the start and the best configuration.
- **`apply_history` keyed by variable and value across rule / experiment / AI ids,** with a feasibility pre-check: allocator library present, resource groups available, disk space for a swap file.
- **New rules:**
  - lighter performance_schema without table-handler instruments (`ps-table-io`);
  - `binlog_order_commits` and the no-delay count as safe group-commit levers;
  - `innodb_purge_batch_size`;
  - device placement for doublewrite / redo / binlog on a second drive;
  - log-thread CPU priority (resource groups) on commit-bound, CPU-starved hosts;
  - write volume competing with reads on a saturated device.
- **Analytics:** detect goal saturation at the per-query cap and rank on uncapped time.
- **Experience:** learn from sweeps; penalise allocator variants after a jemalloc loss.
- **LLM analysis cache:** add storage latency, CPU busy, NUMA and share bucket to the signature.
- **Canary:** 30 s windows are too short for steady-state checks (3 samples); sample faster or lengthen the windows.
- **Calibration:** 4-5 runs, or pool CV across sessions of the same build and benchmark.
