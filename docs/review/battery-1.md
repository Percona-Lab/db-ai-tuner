# Battery 1 review (2026-10-19)

Study #4, 10 Linode dedicated 8 vCPU / 16 GB VMs, Percona Server 9.7, binlog off, client co-located, scope knobs + OS,
policy benchmark (trade-offs in the last 30% of the budget). 204 measured runs. Data: server DB copy
`~/aituner-review/tuner-b1.db` (08:47 UTC). O4 (#72) and A2 (#60) were still running and are **partial**. The aborted sessions #55, #66 and #71
are excluded.

Gains are mean-of-runs against the starting configuration (the calibration mean). For TPC-H, gain means less total time. "Significant" means
the gain is above the session's final noise threshold.

## 1. Results per test

| Test | Session | Runs / h | Noise thr. / cal. CV | Defaults → start | Best safe vs start (n runs) | Signif. | Best with trade-offs vs start (n) | Trade-off phase | First run of the winning change |
|---|---|---|---|---|---|---|---|---|---|
| O1 sysbench RW mem, 4 thr | #54 | 16 / 2.2 | 0.83 / 0.40 | +66% | **+6.5%** (3): log_writer_threads=OFF | yes | +27.1% (3): trx_commit=2 | yes (early: rules exhausted) | 0.7 h, run 6 |
| O2 sysbench RW mem, 64 thr | #56 | 27 / 3.9 | 2.54 / 0.43 | +59% | +1.2% (4): LWT OFF + jemalloc | **no** | +8.2% (3): PS off + spin 10 (+ neutral doublewrite OFF, O_DIRECT_NO_FSYNC) | yes | – |
| O3 sysbench RW mem, 1024 thr | #73 | 28 / 3.9 | 3.33 / 1.71 | +98% | +2.5% (3): LWT OFF + spin + jemalloc | **no** | +7.3% (3): PS off | yes | – |
| O4 sysbench RW I/O-bound, 64 thr (partial) | #72 | 19+1 / 6.9 | 1.99 / 0.24 | +117% | +3.4% (2): LWT OFF | yes | +25% (2): trx_commit=2; +31% (1, confirmation running): + PS off | yes | 1.75 h, run 6 |
| O5 HammerDB 80W, 64 VU | #65 | 29 / 4.4 | 4.18 / 1.26 | +190% | **+7.5%** (4): LWT OFF + spin 10 | yes | +14.1% (3): PS off | yes | 0.7 h, run 6 |
| O6 HammerDB 800W, 64 VU | #70 | 20 / 6.4 | 2.11 / 0.41 | +229% | **+7.4%** (3): LWT OFF + spin 10 | yes | +12.2% (3): PS off | yes | 1.6 h, run 6 |
| A1 TPC-H SF1 (total s) | #53 | 12 / 1.0 | 2.0 / 0.19 | 1067 → 333 s | **333 → 91 s (3.65x)** (3): subquery_to_derived (−65%), then AHI ON (−21%) | yes | – | no (no trade-off items; stopped with 8 runs of budget unused) | 0.7 h, run 6 |
| A2 TPC-H SF10 (partial) | #60 | 6+1 / 7.8 | 2.0 / 0.25 | 5507 → 4413 s | none yet (bp-grow −1.9%, 1 run) | – | – | no | – (subquery_to_derived running at 7.8 h) |
| M1 CH 50W | #63 | 26 / 3.8 | 1.44 / 0.36 | +67% | **+31.0%** (3): LWT OFF (+27%); 5 neutral changes ride along | yes | +58.4% (3): trx_commit=2 + PS off | yes | 0.7 h, run 6 |
| M2 CH 500W | #67 | 21 / 6.4 | 0.83 / 0.22 | +59% | **+33.7%** (2): LWT OFF; reported config also carries 3 neutral changes | yes | +57.2% (2): trx_commit=2 + PS off | yes | 1.5 h, run 6 |

Run budget across the 204 runs: defaults 10, calibration 30, experiments 86, sweep points 33, confirmations (including the final ones) 37,
re-checks 8. About 50 of the 119 experiment and sweep runs were spent on changes with no plausible link to the diagnosed bottleneck, on
retries, or on configurations that leftover neutral changes had contaminated (section 3). In every finished session the winning safe change
came from the first one or two experiments (run 6).

## 2. What worked

- **The starting configuration** gave +59% to +229% on OLTP and 3.2x on TPC-H SF1, which is again most of the value.
- **`innodb_log_writer_threads=OFF`** was positive in 8 of 8 sessions: improved in 6, with an efficiency gain in O2 and O3. Gains were +4.7% (O1),
  +2.8% (O4), +5.2% (O5), +5.8% (O6), **+27% (M1) and +36% (M2)**. On 8 vCPUs with 32 or more client threads the dedicated log writer and flusher
  threads cost more than they save. It is still the best general rule at this size.
- **Trade-offs reached** in 8 of 8 finished OLTP and mixed sessions (3 of 33 before). `performance_schema=OFF` gave +3.0 … +5.9% in 7 of 7 sessions
  (median +4.4%), which matches the earlier measurement of 4.5%. `trx_commit=2` gave +14 … +21% in 4 of 4.
- **Immediate confirmation** caught drift: O6 spin-loops went from +4.0% to a +2.6% mean, and O4 and M2 trx-commit-2 dropped 0.8 … 2.4%.
- **TPC-H**: the rules found the two levers that matter. `subquery_to_derived` gives −65%, and AHI ON gives −21% on SF1. Both are confirmed, and the AHI result is new.
- **Calibration** CV was 0.2 … 0.4% on 8 of 10 sessions, with O3 at 1.7% and O5 at 1.3%. The dedicated 8 vCPU Linodes are quiet enough for gains of about 1%, once the threshold
  uses that (section 3, M8).

## 3. Method problems (evidence → fix)

### M1. Parent selection misses reverts. Fixed on paper in the last review, still broken.
`evaluate._parent_key` excludes only changes whose `source == "rollback"`. Reverts merged into the next experiment carry
`rec:revert-to-run-N` or `rec:rollback-to-run-N`, and the value a sweep restores carries `exp:N`. Those reverts count as the run's own changes, so the
"parent" becomes the just-reverted, often regressed, configuration. **34 of 129 compared runs had a wrong parent.** Verdicts that flip:

| Run | Recorded | Against the real parent |
|---|---|---|
| M1 #16 os-dirty-writeback (merged with the thread-pool revert) | **improved +19.3%** (confirmed by repeat) | −0.5%. The engine then reports "best safe = os-dirty-writeback". |
| O3 #10 spin-loops (merged with the tc sweep restore) | **improved +8.4%** | +2.5% (noise 3.3%) |
| O4 #11 tc=32 (sweep point vs the previous point) | improved +2.4% (never confirmed) | +0.8% |
| O3 #9 tc=32 | neutral +0.9% | **−5.5% (a regression)** |
| O5 #18 purge_threads=4 | neutral +3.0% | −1.3% |
| AMD #57 run 16 redo-grow (merged with the ZFS → ext4 revert) | **improved +23.4%** (confirmed) | about **−5.6%** against run 12/13 |

A repeat cannot catch this, because it measures the same configuration against the same wrong parent.
**Fix:** mark revert changes explicitly. In `cli._apply_and_record` and `cmd_autotune`, set `"revert": True` on changes from `kind == "revert"` items and on a sweep's
restore. In `evaluate._parent_key`, take `own` = changes without `revert`. Better still, compute the intended parent directly as `managed` with the own variables set back to their `old` values,
and look up that configuration. For sweeps, the parent of every point is the sweep base, not the previous point. Then run `reevaluate` on all
sessions so that the experience priors are rebuilt.

### M2. Neutral experiments are not reverted, so the configuration accumulates unproven changes
In `engine.revert_suggestions`, the revert after a neutral or efficiency-loss result is non-priority. It scores 6, below almost every experiment, which score 8 … 14. It is offered only while that
configuration was the last one measured, so after one more experiment it is gone for good. Evidence:
- M2's final best safe has spin 10 (+0.9%, neutral), tc=32 (a neutral sweep point) and jemalloc (+0.3%, neutral). M1's has dirty-writeback, spin, autogroup=0,
  purge_threads=2 and jemalloc, all neutral. O3, O4, O2 and #58 carry neutral jemalloc and/or bp 12G.
- O5 #9–#13 (tc sweep, AHI, spin, thread pool) were measured with neutral jemalloc (−2.1%) and AHI still on. O2 #12–#17 had the same problem.
- Trade-off phase: O2's best keeps **doublewrite OFF (+0.4%) and O_DIRECT_NO_FSYNC (−0.6%)**, which gives up durability for nothing.

**Fix:** make the revert after a neutral or efficiency-loss result mandatory. In `revert_suggestions`, set `priority=True`. It costs no run, because `cmd_autotune`
applies it, finds the configuration already known and merges it into the next experiment's run. Combined with M1 this keeps every comparison
one variable wide. A small win is still collected later by `stack_small_wins`.

### M3. A confirmation does not re-judge the verdict
After `(confirm)`, the first run keeps `improved` even when the mean of both runs falls inside the noise. O2 #7 jemalloc was +2.15%, but the mean of #7 and #8 is
**+0.5%** against the parent. It stayed "improved", was kept, and in `best_config` its "kept over" edge beats the LWT-only configuration. O3 #10, O4 #17 and O6 #8 drifted
in the same direction, but stayed above the threshold.
**Fix:** in `cmd_autotune`, after the confirm run, re-evaluate the experiment as the mean of n=2 against the parent mean with `mean_noise(noise, 2, n_parent)`. Store
`evaluation.confirmed = True/False`, and set the verdict to `neutral` (followed by the mandatory revert) if it fails. `best_config` builds edges only from confirmed
improvements, so the new tie-break needs this to mean anything.

### M4. `best_config` can choose a configuration that was never better than its parent
M2's final best safe was the tc=32 sweep point: 1 run, neutral, 1809.9. Its confirmation measured 1769.5, a drift of −2.2%. O1's summary "best" with trade-offs is run 14 (one
neutral run), which the final confirmation never measured. The final confirmation switched configurations, and `_update_best` then moved the best to a different, unconfirmed one.
I checked the new `best_config` against M2: it still picks tc=32, because the neutral points tie and are sorted by mean.
**Fix:** in `engine.best_config`, the candidates are the configurations with a kept verdict (confirmed improved, efficiency gain, reference or accepted practice) plus any
configuration with n ≥ 2 that beats them beyond `mean_noise`. In `cli.cmd_autotune`, freeze `best` and `best_safe` to the configurations that the final
confirmations measured. For trade-offs, report the **minimal** set: drop every trade-off setting whose own verdict was neutral.

### M5. A rollback to the best configuration drops later efficiency gains, and the engine re-tests them
`rollback-to-run-N` returns to `best_config`. That discards changes kept later with an efficiency-gain verdict. `apply_history` does not block those changes, so they run again:
O5 #14 rolled back spin 10 (efficiency gain at #12), and #15 measured it again. O2 #19 rolled back spin (efficiency gain at #15). It came back in the trade-off phase (#24, +3.7% confirmed, on top of PS off),
but the best safe configuration never received it.
**Fix:** in `revert_suggestions` (rollback branch), re-apply the kept or efficiency-gain changes made after the best configuration that are not responsible for the gap. In
`apply_history`, treat `efficiency-gain` as tried. When a **safe** item first runs in the trade-off phase, re-measure it on the best safe configuration before
the final confirmation.

### M6. The `io` diagnosis comes from the share of waits alone
`engine.diagnose` (wait profile) adds `io` when data-file waits are at least 40% of non-row waits and at least 0.5 threads are waiting. In O3, 4.0 of **829 running threads** were waiting, with disk util
8.5% and 78 reads/s. That still produced "I/O", and the result was **8 runs**: an io_capacity sweep (3), a dirty_pages_pct_lwm sweep (3) and 2 re-checks, all neutral. M2 and O4 are
legitimately I/O-heavy (6.6k reads/s, util 91–95%).
**Fix:** require waiting threads ≥ max(0.5, 0.05 × threads_running, 0.25 × CPUs) **and** corroboration, either psi_io_some ≥ 5% or disk_util ≥ 30%.

### M7. Experience transfers across concurrency classes. The new engine addresses this.
The thread pool was tried at 64 VU in O2, O5, M1 and O6 and lost −20%, −9.5%, −17% and −4% (the last in the trade-off phase), despite 9 earlier losses. **It was never tried in O3**: 1024 threads, 829 running,
5.6 ms scheduling delay. That is the one test where it should win, and the AMD box (#57, 16 VU on 12 threads) found +2.2% confirmed. In O3 it scored 8 and ranked 9th, behind
sweeps. The concurrency-class prior deployed today addresses this. It should also gain a positive gate: running threads > 4 × CPUs makes it a high-relevance item.

### M8. The noise threshold sits at the 2.0% default for most of the session. The noise plan partly addresses this.
Until the first re-check (long-term dof = 0), the verdicts in O1, O4, M2 and O6 used 2.0%. Their final thresholds were 0.83, 1.99, 0.83 and 2.11%, with calibration CV
0.2 … 0.4%. In O1 and M2, best-practices (+1.7%), spin (+0.9 … +1.5%) and no-fsync (+1.2%) were judged against 2.0. The noise plan helps. In addition, when the
threshold drops, `_reevaluate` should re-judge the earlier neutral runs and let `stack_small_wins` use the new value. O4's trx-commit-2 confirmation was judged against 0.5%
(the threshold collapsed after one re-check, with dof = 1), so a floor tied to the degrees of freedom is needed.

### M9. Sweeps
Sweep points are compared with the previous point (M1). A sweep point's "improved" result is never confirmed: O4 #11. Sweeps without a mechanism linked to the diagnosis
used **33 runs**, and 0 of the 33 points were a real gain (table 4). **Fix:** `cmd_autotune` should run a sweep only when its rule's relevance is ≥ 2 *and* the diagnosis that triggers it is
corroborated (M6). Use 2 points by default (half and double the current value), and widen only if one beats the base.

### M10. Analytical tests: weak diagnosis, per-second thresholds, budget left unused
- A1: the diagnosis was "none" at 12.6% CPU, which is one of 8 cores saturated by a single stream. `diagnose` needs a per-core check, with mysqld CPU ≈ threads_running / CPUs.
- The OLAP rules use rates per second, and a single stream produces tiny rates. `sort-buffer-olap` needs Sort_merge_passes > 0.05/s, and A1 had 0.043/s. Created_tmp_files at
  12.5/s (filesort and hash-join spill files) is ignored. **Fix:** use counts per statement or per run in the OLAP branch of `recommend`.
- Queries hit the 120 s cap (Q17 and Q20 before subquery_to_derived, according to the LLM's reading of the waits). Capped queries make the goal a lower bound. Record
  `Max_execution_time_exceeded` per run and show it next to the verdict.
- A1 stopped after 8 experiments with "rules exhausted" (budget 16). Three accepted LLM items (sort_buffer, temptable_max_ram, parallel_read_threads) were
  never measured (L2).
- A2: 73 min per run, so calibration took **3.7 h** for a CV of 0.25%. Use 2 calibration runs when the run takes more than 30 min (the noise plan),
  and consider a geomean or per-query goal so that one capped query does not dominate.

### M11. CH-benCHmark measures only the OLTP side
M1 and M2 are classified `oltp-write` (writes are 67% of statements), so no OLAP rule ever fires. **QphH is constant across every run**: 65.5 in all 25 M1 runs
and 31.3 in all 20 M2 runs. The metric extraction (the profile regex, or the go-tpc output being parsed) is broken, and the goal ignores the analytical stream. **Fix:** repair
the `qphh` metric in `bench/battery/setup.sh` and the profile. Make the goal composite (geometric mean of tpmC and QphH), or at least give QphH a
regression guard.

### M12. A purge backlog is detected but ranked out
O4 had history list length **1.68 M** with `innodb_purge_threads=1`, the 9.7 default on 8 CPUs. `sweep-purge-threads` (impact 1, score 8) and `purge-lag` (score 7) ranked
12th to 15th and never ran. **Fix:** scale impact with the history list length in `recommend`: above 1 M, impact 3 and confidence 2.

### Minor
- `top_statements` lists prepared statements twice, as text and as `EXECUTE …` (M1: `UPDATE stock` 7.1% + 7.1%), so the LLM context double-counts them.
- The O1 summary "best" after the final confirmation is a single unconfirmed run (M4).
- Method-check #58 (sysbench 1 GB, no data reset) trended about −5% per calibration run, giving a threshold of 7.9% that makes it useless below 8%. The `data_state` check covers this.

## 4. Rule outcomes (verdicts against the corrected parent, mean including the confirmation)

| Rule | OLTP mem 4 / 64 / 1024 (O1–O3) | IO-bound (O4) | TPC-C (O5, O6) | CH (M1, M2) | TPC-H (A1, A2) | Tried / impr / neutral / regr | Median Δ |
|---|---|---|---|---|---|---|---|
| log_writer_threads=OFF | +4.7 / +1.7e / −1.0e | +2.8 | +5.2 / +5.8 | +27.1 / +35.9 | – | 8 / 6 (+2 eff) / 0 / 0 | **+5.0%** |
| performance_schema=OFF (trade-off) | – / +3.0 / +4.3 | +4.8 | +5.9 / +5.5 | +4.4 / +3.2 | – | 7 / 7 / 0 / 0 | **+4.4%** |
| trx_commit=2 (trade-off) | +17.9 / – / – | +20.6 | – | +15.6 / +14.0 | – | 4 / 4 / 0 / 0 | +16.8% |
| spin_loops=10 | – / +1.0e, +3.7(TO) / +2.4 | +1.2e | +2.7e, +0.2 / +2.6 | +1.5 / +0.9 | – | 9 / 2 / 4 (+3 eff) / 0 | +1.5% |
| AHI ON | +0.4 / +0.7 / −1.0 | – | +0.2 / −0.3 | – | **−21% time** | 6 / 1 / 5 / 0 | +0.3% (OLTP) |
| subquery_to_derived | – | – | – | – | **−65% time** | 1 / 1 / 0 / 0 | |
| jemalloc | – / +0.5 / +1.2 | −0.4 | −2.1 / −0.9 | 0.0 / +0.3 | – | 7 / 0* / 7 / 0 | 0.0% |
| tcmalloc, mimalloc, tuned jemalloc and glibc | O2 only: +1.1, −1.0, −0.6, −1.0 | | | | | 4 / 0 / 4 / 0 | −0.8% |
| thread pool | – / −19.6 / not tried | – | −9.5 / −4.2 | −16.6 / – | – | 4 / 0 / 0 / 4 | **−13%** |
| innodb_thread_concurrency sweep (16, 32) | – / −1.6, −0.7 / **−6.3, −5.5** | −1.6, +0.8 | −1.0, −0.9 / −0.8, −2.1 | +0.5, +0.8 / −0.4, +0.2 | – | 14 points / 0 / 12 / 2 | −0.8% |
| io_capacity sweep | O3: −0.8, −1.0, −1.1 | +0.7, −0.5, +0.5 | – | M2: +0.2, −0.4, −0.1 | – | 9 / 0 / 9 / 0 | −0.4% |
| dirty_pages_pct_lwm, lru_scan_depth sweeps, lru-depth | O3: −0.4, −0.3, −1.5 | – | O5: −1.9, +0.6, −0.7 | M2: −1.3 | – | 7 / 0 / 7 / 0 | −0.7% |
| purge_threads sweep, purge-lag | – | – | O5: **−4.2**, −1.3 | +1.4, +2.9, −1.5 | – | 5 / 0 / 4 / 1 | −1.3% |
| os-dirty-writeback | +1.3 | – | +0.2 | −0.5 (recorded +19.3) | – | 3 / 0 / 3 / 0 | +0.2% |
| best-practices bundle (swappiness, noatime) | +1.7 / −1.0 / −0.1 | +0.6 | +2.0 / −1.1 | −0.1 / −1.8 | 0.0 / −1.1 time | 10 / 0 / 10 / 0 | −0.1% |
| doublewrite OFF, O_DIRECT_NO_FSYNC (trade-off) | −0.3, +1.0 / +0.4, −0.6 / −1.0, +0.3 | – | +1.1, −1.8 / +0.2, −0.9 | −0.4 / – | – | 11 / 0 / 11 / 0 | −0.2% |
| autogroup, table_open_cache, bp-grow, join-buffer, BKA/MRR, deadlock-detect (LLM) | | | | | | 9 / 0 / 9 / 0 | |

Notes: `e` = efficiency gain. `*` O2 jemalloc was recorded as "improved" (M3). For TPC-H, negative = faster.

Rule-level conclusions:
1. On 8 vCPU with a co-located client, the safe levers are **LWT OFF, then spin 10** (+1 … +3%, consistent, usually below the threshold). PS off and trx_commit=2 are the
   trade-off levers. Everything else in the safe set is noise at this size.
2. **Retire or gate** these on ≤ 8 vCPU: the 4 malloc variants beyond jemalloc (gate on a cpu-mm profile above 6%), autogroup, the io_capacity and lwm sweeps without
   corroborated I/O waits, lru_scan_depth (gate on `Innodb_buffer_pool_wait_free > 0`), and the thread pool below 4 × CPUs running threads.
3. **Missing rules** given the diagnosis:
   - O3: thread pool (M7) and `thread_pool_size` / `thread_pool_oversubscribe` sweeps at 1024 connections.
   - O4: purge threads at a history list of 1.68 M (M12), `innodb_old_blocks_time` / LRU midpoint for 98% hit with uniform keys, and BP growth up to the free memory left by the co-located sysbench.
   - TPC-H: sort_buffer, temptable_max_ram and tmp files (M10). Histograms are advice only.
   - CH: OLAP rules for the analytical stream (M11).
   - Every CPU-bound OLTP test: `ps:profile=lite` once agents support it, as a safe alternative to PS off.

## 5. LLM audit

12 calls (Sonnet 5.5) in A1 (3), O1 (3), O5 (3) and M1 (3). The per-session limit of 3 was hit in all four sessions. 16 suggestions were accepted
(some repeat across calls) and 3 were measured: a purge_threads sweep (−4.2%, a regression), an lru_scan_depth sweep (neutral) and deadlock_detect (neutral).

| Problem | Evidence | Model or context? | Fix |
|---|---|---|---|
| L1. Suggestions for settings that do not exist | 8 of 12 calls proposed `ps:profile=lite` and/or `rg:innodb_bg_cpus`, and the sanitizer rejected all of them ("does not exist on this server"). The constraints also say "do not propose performance_schema=OFF", while ps-off is a trade-off item. | **Context.** `llmassist.context` lists these pseudo-variables unconditionally. A replay of #16 with Opus 5.5 also proposed ps:profile. A replay of #22 with Sonnet and the constraints patched gave 0 invalid suggestions. | Build the pseudo-variable list from `ctx["variables"]`. Under `policy=benchmark`, say that PS off is allowed as a trade-off. |
| L2. Accepted suggestions are lost | A1: sort_buffer, temptable_max_ram, parallel_read_threads and an optimizer_switch item were accepted but never run. The session ended "rules exhausted" with 8 runs unused. O1: purge and thread_cache were dropped, and O1 ended with 9 unused. M1: page_cleaners, O5: thread_cache. | Agent | Persist accepted LLM items in session state until they are tried or invalidated, and merge them in `_recommend`. |
| L3. Calls wasted on revert-only steps | O1: two calls at 01:38:46 and 01:39:01 (#17 and #18) after the same run, one for `revert-to-run-7` and one for the next experiment. | Agent | No call when `next` is a revert. Cache per run_id. |
| L4. Missing facts | O1: the LLM proposed purge threads "as history may lag", but the history list was 335. The context has no `history_list_length_max` and no running threads / CPUs. | Context | Add the history list length, threads_running / CPUs, capped queries and the corrected parent deltas (M1). |
| L5. `hypergraph_optimizer=on` for TPC-H (#15) | A replay with Opus proposed it too. `optimizer_switch` shows `hypergraph_optimizer=off`, so both models read it as settable. It is usually rejected in release builds. | Model knowledge + context (no build facts) | The apply validation rolls it back at no run cost. Add a "SET is accepted" probe to `sanitize`. |
| L6. Low-value proposals after rules run out (thread_cache_size with persistent connections, lru_scan_depth in memory, deadlock_detect) | O5 and M1 | Mostly the problem space. Sonnet and Opus both propose similar small levers on a CPU-saturated 8 vCPU box. | Let the LLM state "no relevant lever" (an empty list), and stop or start trade-offs instead of spending runs. |

Overall the model's reasoning is sound: it read the A1 bottleneck correctly (correlated subqueries at the cap) and the O1 commit path. The failures are context (L1, L4) and agent plumbing
(L2, L3), not the model. Opus offers no clear advantage for these contexts.

## 6. Comparison with the 2026-10-18 review

| 2026-10-18 claim or fix | Battery 1 |
|---|---|
| The starting configuration delivers most of the value | Confirmed (+59 … +229%, 3.2x on TPC-H) |
| LWT OFF is the best rule | Confirmed and stronger (+27 / +36% on CH) |
| PS overhead 4.5% | Confirmed: median +4.4% |
| "Parent = config differing only in the experiment's settings" (fixed) | **Not effective** for named reverts and sweeps: 34 of 129 wrong parents (M1) |
| Winner's curse fixed by immediate confirmation | Partly: the repeat runs, but the verdict is not re-judged (M3), and neutral leftovers contaminate the next runs (M2) |
| Trade-offs never reached | Fixed: 8 of 8 finished OLTP and mixed sessions |
| Thread pool moved to the end after harmful history | Still tried 4 times (−4 … −20%) and **not** where it matters (O3). The concurrency-class prior deployed today addresses this |
| Small wins stacked | `stack-small-wins` fired once (O1, −0.7%). The 2% default threshold (M8) hides most +1% wins |

## 7. Recommendations for Battery 2

Before the run, the engine fixes in priority order: M1 (parent), M2 (mandatory revert), M3 (re-judge after confirmation), M4 (best only from kept configurations), L1 + L2,
M6, M12, M10, M11. Then run `reevaluate` on all past sessions so that the experience priors no longer carry the false "improved" results (M1 #16, AMD #57/16, O3 #10).

Same 10 tests with the new engine, with these changes:
- **Acceptance checks**, computed by a script such as `~/aituner-review/analyze.py`: zero parent mismatches; no neutral change in a final best-safe or trade-off
  configuration; every "improved" result confirmed by its n=2 mean; time to the best safe configuration ≤ 1.5 h on 5-minute runs.
- **O6** is not I/O-bound (99.9% hit, 1k reads/s, CPU 96%). Use about 2000W, or cap the BP through `[requires]`, so that I/O rules get tested. O4 is I/O-heavy but
  still CPU-bound with the co-located client. Keep it, and add a separate client host so that the I/O behaviour is visible.
- **A2**: 2 calibration runs, max 10 runs, cap 600 s, report capped queries. Or replace it with SF10 restricted to the 8 longest queries.
- **M1/M2**: fix QphH and use a composite goal.
- **O3**: keep `--db-ps-mode=disable` and 1024 threads. This is the thread-pool test, so check that the pool is tried early.

New contention-sensitive tests on 16+ core machines (a 32 vCPU dedicated Linode or bare metal with 16C/32T), with the **client on a separate host on the same LAN**,
binlog off, at **64 and 1024 connections**:

| ID | Workload | What it should exercise |
|---|---|---|
| C1/C2 | sysbench oltp_point_select, in memory, 64 / 1024 | thread pool, `thread_pool_size`, AHI and its partitions, PS overhead, `table_open_cache_instances` |
| C3/C4 | sysbench oltp_read_write, in memory, 64 / 1024 | LWT ON vs OFF at 16+ cores (the result should flip), spin loops / `spin_wait_delay`, `innodb_thread_concurrency`, thread pool |
| C5/C6 | sysbench oltp_update_index or a zipfian/hot-row write, 64 / 1024 | lock-sys and redo contention, log_writer threads, `innodb_sync_array_size`, deadlock detection |
| C7 | HammerDB TPROC-C 100W (hot warehouses), 64 / 512 VU | row-lock contention vs thread pool, purge |
| C8 | Connection storm (connect/disconnect, the existing `connstorm.lua`) at 1024 | thread cache, `back_log`, thread pool, `max_connections` |

For each, record per-core CPU, threads_running / cores and the CPU profile, so that the rules for running threads above 4 × cores can be validated against the 8 vCPU results
of Battery 1.
