# Planner backtest on the last 24 hours of real decisions (2026-10-24)

This backtest replays the autotune decisions of the last 24 hours. At each decision point it compares three choices:
- **ACTUAL:** what the session did.
- **RULES NOW:** today's rule engine (HEAD `d10814b`).
- **PLANNER NOW:** the current planner, `anthropic/claude-opus-5.5` at high effort, with no reviewer.

**OPUS MAX** is an optional reference: the same model at `xhigh` effort with a fuller packet. That packet lists every
candidate and every tested hypothesis, plus a per-run history with label, parent, verdict, the hypothesis tested and key
metrics.

Following the user's change during the run, no other model generated choices. The Fable 5.1 pilot (5 points) is left
out of this report.

Choices were scored two ways, and the two are reported separately:
- **Blind judge grades,** 0-2. The judge saw the evidence and the distinct options, anonymised and shuffled.
- **Hindsight:** the measured verdict whenever an option's exact settings were run later in the same session or in a
  comparable one. Judges never saw these results.

Raw data: `ops/dbench/backtest-2026-10-24/`. Tooling: `tools/dbench/build.py --sessions`, `run_remote.py --effort/--packet`
and `backtest.py` (`judge-input`, `check`, `report`).

## Scope and sampling

**Sessions (14):**
- 288: h2h planner arm.
- External user sessions on the same two hosts:
  - 294 and 297: read-only, 64 threads;
  - 299, 300, 303 and 304: read-only, 1024 threads (303 and 304 with a remote client);
  - 311: read-write, 1024 threads.
- 315 and 316: suite RO-4.
- 310, 313 and 314: deploy canaries. In 314 the planner decided.
- 302: noise canary. It was added because it is commit-bound under the benchmark policy and its trade-offs were measured.

The scripted study sessions (293, 295, 296, 298, 301) are skipped.

**Decision points: 123 of 244 stored recommendations.**
- A decision point is the last recommendation before a run (the one that chose it), plus a session's final stop.
- Earlier recommendations for the same run were superseded. This drops the revert/rollback half of every
  "undo + next" pair, which is applied before the next choice, and the re-issues after agent-inserted re-checks.
- 26 more were skipped as mechanical: the next run was a confirm, repeat, re-check or baseline (24), or an undo that
  ran as its own run (2).
- The tail recommendations of active sessions 311 and 315 had no following run yet and are not included.
- No near-duplicates were removed: consecutive points with the same option are real repeated choices.

**How each point was rebuilt (fairness rule as before).**
- Context comes from the runs up to that point only, with the engine's own undo steps settled first.
- Cross-session experience, human feedback and the AI-suggestion step are left out, because they would include later
  sessions. This handicaps RULES NOW and PLANNER NOW against ACTUAL. Production had experience, so it is a real
  difference (see finding 3).

**Planner checks:** `planner.check` was run on every planner answer. A rejected choice falls back to the rules' pick,
and the result is reported as "effective".

**Second judge:** not possible. The OpenRouter account ran out of credit mid-run: $150 total credits, $147.0 used,
HTTP 402 on 95 judge calls. The key's own $200 limit was not the constraint.
- GPT-5.6 Sol (OpenAI, a vendor different from the generator) graded 58 decisions:
  - the first 39 in order, which covers all of h2h and nearly all of external-64;
  - plus a stratified random sample of 19 from the canary, external-1024 and suite points.
- Gemini 4 Argon graded only 2 decisions before it was stopped to keep the last credits for production, so it is not
  reported.
- **Production LLM calls (planner, learner, analysis) will fail with 402 until credits are added.**

## Agreement (same settings or same stop; 123 points)

| | actual | rules now | planner now | opus max | planner (effective) | opus max (effective) |
|---|---|---|---|---|---|---|
| actual | – | 20% | 19% | 19% | 21% | 20% |
| rules now | 20% | – | 59% | 55% | 78% | 72% |
| planner now | 19% | 59% | – | 85% | 81% | 74% |
| opus max | 19% | 55% | 85% | – | 74% | 83% |

Planner checks rejected:
- **PLANNER NOW: 23 answers,** all `ps:profile=lite` under the safe policy (294 ×9, 299 ×9, 300 ×5).
- **OPUS MAX: 18 answers** of the same kind, plus 3 rollbacks that were artefacts of the rebuild (the rollback had
  already been applied); those 3 are counted as accepted.

## Grades (GPT-5.6 Sol, blind, 58 decisions)

| Decision maker | Mean | Good | Poor | Judged best | STOP picks (all 123) |
|---|---|---|---|---|---|
| ACTUAL | 0.72 | 26% | 53% | 29% | 4 |
| RULES NOW | 1.45 | 64% | 19% | 69% | 33 |
| PLANNER NOW (as answered) | 1.43 | 66% | 22% | 66% | 30 |
| PLANNER NOW (effective, after checks) | 1.55 | 71% | 16% | 76% | 41 |
| OPUS MAX (as answered) | 1.33 | 60% | 28% | 59% | 35 |
| OPUS MAX (effective) | 1.48 | 67% | 19% | 71% | 44 |

Paired differences, bootstrap 95% CI:
- planner vs rules: −0.02 (−0.21..+0.17);
- planner effective vs rules: +0.10 (−0.02..+0.24);
- OPUS MAX vs planner: −0.10 (−0.28..+0.05);
- planner vs actual: +0.71 (+0.36..+1.05);
- rules vs actual: +0.72 (+0.36..+1.05).

Mean grade per session type (n = decisions graded):

| Decision maker | canary (13) | external 1024-thr (11) | external 64-thr (24) | h2h (8) | suite (2) |
|---|---|---|---|---|---|
| ACTUAL | 0.77 | 0.27 | 0.96 | 0.50 | 1.00 |
| RULES NOW | 1.77 | 1.82 | 1.08 | 1.38 | 2.00 |
| PLANNER NOW | 1.77 | 1.27 (effective 1.73) | 1.17 (effective 1.25) | 1.75 | 2.00 |
| OPUS MAX | 1.77 | 1.27 (effective 1.82) | 1.12 (effective 1.21) | 1.12 | 2.00 |

**Where the decision makers stopped.**
- Every STOP by today's makers falls in the external sessions: rules 28/59 at 1024 threads and 5/25 at 64 threads; the
  planner 25/59 and 5/25.
- None of the three new decision makers ever stops in the canary, h2h or suite sessions.
- ACTUAL stopped 4 times, each time to end a session.

### The judge does not predict outcomes

Of the 80 judged options whose exact settings were measured in the same session:

| Measured verdict | Options | Mean judge grade |
|---|---|---|
| improved | 18 | 0.89 |
| regressed | 15 | 1.53 |
| neutral / other | 47 | 0.85 |

Regressions were graded better than improvements.

**Overrated: generic CPU levers.**
- `os-autogroup` at 300 r13 got 2 and measured **−27.5%**.
- `ahi-on` at 294 got 2 three times and measured **−7.5%**.
- The CPU-governor bundle at 313 rec 3665 got 2 and measured +0.35%.

**Underrated: durability and observability trade-offs, and anything the packet's stability-first rule forbids.**
- 288: trx-commit-2 **+25.7%**, log-writer-threads OFF **+22.8%** and ps-off **+6.8%** all got 0.
- 302 r10: trx-commit-2 **+29.5%** got 1.

**Grades are an opinion on the packet, not evidence of gain.** Use hindsight as the primary score, and change the
packet where it misleads both planner and judge (findings 2 and 3).

## Hindsight: measured results of each maker's choices

The table below counts options whose exact settings were measured later in the same session or in a comparable session
(294/297, 299/300/303/304, 315/316, canaries). ACTUAL is measured by construction.

| Decision maker | Run picks | Measured | Improved | Regressed | Mean measured delta | STOPs with a later confirmed win |
|---|---|---|---|---|---|---|
| ACTUAL | 119 | 114 | 20 | 14 | +2.07% | 0/4 |
| RULES NOW | 90 | 76 | 2 | 9 | +0.04% | 3/33 |
| PLANNER NOW | 93 | 68 | 7 | 12 | +1.60% | 3/30 |
| OPUS MAX | 88 | 65 | 9 | 9 | +1.99% | 3/35 |
| PLANNER NOW (effective) | 82 | 74 | 8 | 12 | +1.53% | 4/41 |

**Rules.** RULES NOW picks things that later measured flat: +0.04% on average, 2 wins in 76. It misses the canary and
h2h trade-offs that won (findings 2 and 3).

**Planner.** It takes some of those trade-offs (trx-commit-2 at 288 rec 3417 and 302/313; log-writer-threads OFF at
310/313), hence +1.6%. It also repeats rules' flat picks (ahi-on, best-practices).

**Stopping.** Each stop rule ends the 1024-thread external sessions early. The first STOP comes at:

| Session | Rules now stop after | Runs that followed | Planner stops after | Runs that followed |
|---|---|---|---|---|
| 299 | r11 | 22 | r13 | 20 |
| 303 | r11 | 25 | r6 | 30 |
| 304 | r8 | 4 | r3 | 9 |
| 300 | r22 | 11 | r21 | 12 |
| 297 | r17 | 4 | r14 | 7 |
| 294 | r20 | 9 | – | – |
| **Total** | | **75** | | **78** |

The runs that followed contain two confirmed wins:
- 300 r25 malloc-mimalloc, +2.4%. In 299 the same change measured −0.4% and +0.5%.
- 294 r21 `llm-innodb-buffer-pool-size-os-hugepages`, +3.7%. This was an AI idea, which the rebuild does not regenerate.

Everything else after those stops was neutral or unreliable.

## What today's fixes changed (ACTUAL vs RULES NOW, 123 points)

| Why they differ | Points | What ACTUAL measured there |
|---|---|---|
| same choice | 24 | – |
| rules now stop (fruitless streak / ceiling stop) | 32 | 16 neutral, 10 unreliable, 2 regressed, 1+1 efficiency, **2 improved** (294 r21 +3.7%, 300 r25 +2.4%) |
| re-ranked (old choice still a candidate) | 41 | 23 neutral, 6 regressed, 3 unreliable, **9 improved** (mostly canary trade-offs the rebuild lost without experience) |
| planner decided (288, 314) | 11 | 6 improved, incl. binlog-off +65.6%, trx-commit-2 +25.7% / +22.8%, LWT-off +22.8% / +11.0% |
| a scripted / queued step ran instead of the recommendation (canaries) | 6 | – |
| AI idea not regenerated by the rebuild | 4 | – |
| no longer proposed (numa-interleave-all: needs remote allocations now, P9) | 3 | neutral −0.7%, regressed −3.8%, neutral −0.0% |
| ACTUAL stopped, rules would run (294 best-practices-swap, 300 table-open-cache) | 2 | – |

The stop rules (P7) and the numa gate (P9) work as intended. The re-ranking differences are dominated by the missing
experience, so they say little about the rule changes themselves.

## Worst decisions of each decision maker

**ACTUAL**

| Session / run | Decision | Measured | Evidence |
|---|---|---|---|
| 300 r13 | os-autogroup | **−27.5%** | At 1024 threads. On the comparable host it had already measured −3.2% (299 r8). |
| 299 r27 | AI idea innodb_thread_concurrency=24 | **−17.5%** | A concurrency limiter at 1024 connections. 294 r18 (−0.8%) and 288 r19 (−8.3%) had already lost with the same limiter. |
| 299 r4 | thread-pool | −13.2% | |
| 303 r11-r36 | 25 more runs | – | Every run "unreliable": hypervisor steal 2.0-2.7% against an invalid-at threshold of 1.56%. Each experiment was re-measured, so every idea cost two runs and no verdict came out. |
| 288 r19 | sweep-thread-concurrency | −8.3% | |

**RULES NOW**

| Session / decision | Pick | Evidence |
|---|---|---|
| 288, 6 of 8 points | purge-lag / sweep-purge-threads | Forced by the stability-first override (purge-lag scores 103). The purge sweeps measured −1.2% and −0.75% (288) and −0.3% (311). The trade-offs it passed over measured +65.6%, +25.7%, +22.8% and +6.8%, all confirmed. |
| Canaries 310/313/314 (4 points) | best-practices before trx-commit-2 and log-writer-threads OFF | Then ahi-on at every later canary point. best-practices measured +0.1%, +0.35% and +0.37%; trx-commit-2 +19.0%, +24.3% and +22.8%; LWT-off +11.0% and +11.2%. |
| 294 (7 points) | best-practices-swap | No memory pressure (PSI memory 0, 12 GiB free). Judge 0 each time. Measured −0.3% (297), +1.5% neutral (300), −0.0% (304). |
| 302 rec 3539 | ahi-on | ACTUAL ran trx-commit-2 there: **+29.5%**. |

**PLANNER NOW**

| Session / decision | Pick | Evidence |
|---|---|---|
| 294, 299, 300 (23 points) | `ps:profile=lite` under the safe policy | Rejected by `planner.check` every time; judge 0. In production each would waste the planner call and fall back to rules. |
| 288 (5 points) | Purge remedies | Same stability-first trap as the rules. |
| 310 rec 3650; 314 rec 3679 and 3680 | best-practices | Instead of trx-commit-2 (+19.0%, +22.8%) or LWT-off (+11.0%). In production at 314 the same model with cross-session experience chose trx-commit-2 and LWT-off. |

**OPUS MAX**
- 288 rec 3443/3450: `rg:purge prio=-20` (never measured; judge 0).
- 315 rec 3689: `thread_handling=pool-of-threads` at 64 connections on 8 CPUs (never measured).
- 18 × `ps:profile=lite` under the safe policy.

The extra effort and the per-run history did not help:
- −0.10 grade vs the planner;
- the same hindsight;
- +51% cost.

In h2h it was worse: 1.12 against 1.75.

## Cost

| Item | Calls | Cost | Per decision |
|---|---|---|---|
| PLANNER NOW (Opus 5.5, high) | 123 | $13.02 | $0.106 |
| OPUS MAX (Opus 5.5, xhigh, fuller packet) | 123 | $19.65 | $0.160 |
| GPT-5.6 Sol judge | 63 ok + 95 refused (402) | $2.22 | $0.035 |
| Gemini 4 Argon judge (stopped) | 12 | $0.47 | $0.039 |
| Fable 5.1 pilot (excluded on request) | 5 | $2.07 | $0.41 |
| Total logged as `dbench*` | | **$37.44** | |

The figures include earlier pilot calls that were re-run. A few Fable requests in flight when the run was stopped may
have been billed without being logged. For comparison, production planner calls cost $0.13 per decision today (11 calls).

## Improvements, prioritised

### Planner (prompt, packet, checks, model)

1. **Fix the `ps:profile=lite` contradiction.**
   - Problem: `llmassist.context` tells the planner "to reduce its overhead safely propose ps:profile=lite", but
     `knowledge.TRADEOFFS["ps:profile"]` and the engine's `ps-lite` item treat lite as an observability trade-off.
   - Evidence: 23/123 Opus answers (19%) were rejected by `planner.check` (294 ×9, 299 ×9, 300 ×5), and the judge gave
     0 to all of them. Accepting the fallback raises the planner's grade from 1.43 to 1.55.
   - Remedy:
     - Make that constraint policy-aware: under safe, lite is advice, not a run.
     - Put the planner's rejected choices for this session in the packet (`planner_rejected: [{choice, reason}]`) so it
       does not ask again every step.
2. **Keep cross-session experience in the packet, and make it prominent for the top candidates.**
   - Evidence: in production, canary 314 (same model, with experience) chose trx-commit-2 (+22.8%) and LWT-off (+11.0%).
     Rebuilt without experience it chose best-practices twice (measured +0.37%). The canary trade-offs won in 4 of 4
     canaries, and best-practices won in 0 of 10 sessions (finding R2).
   - Remedy:
     - A compact `measured_elsewhere` line per candidate: sessions, wins, mean delta.
     - Prompt rule 3 should say that 0 wins in ≥3 comparable sessions outweighs a relevance score.
3. **Soften `stability_first` in the packet.**
   - Problem: the rule text ("choose a stability remedy before any tuning experiment") made planner, OPUS MAX and the
     judge all chase purge in 288.
   - Evidence: purge remedies measured −1.2%, −0.75% and −0.3%; the passed-over trade-offs measured +65.6%, +25.7%,
     +22.8% and +6.8%.
   - Remedy:
     - One stability experiment.
     - If it does not end the growth without a TPS loss, report the growth as a finding and continue normal ranking.
     - Under the benchmark policy, rank evidence-backed trade-offs alongside it.
4. **Model and effort: keep Opus 5.5 at high effort with the current packet.**
   - xhigh with the fuller history was not better (−0.10, CI −0.28..+0.05) and cost 51% more.
   - The per-run history did not change choices in 85% of points.
5. **Stopping: keep.** The planner stopped where the rules did, in the 1024-thread external sessions, and would have
   saved ~78 runs. It missed one confirmed +2.4% (300 mimalloc) that did not reproduce on the comparable host (299).

### Rules

1. **R1 · Stability-first override.**
   - Problem: under the benchmark policy it hid four confirmed trade-off wins in 288 (see P3 above); purge-lag scores
     103 and runs before everything else.
   - Remedy: cap it at one purge experiment, then normal ranking with growth reported as advice. In 311 the purge sweep
     was neutral (−0.3%).
2. **R2 · Gate the `best-practices` bundle's CPU-governor relevance on evidence.**
   - Evidence: it measured 0 wins in 10 sessions: 288 +0.29%, 294 −0.08%, 297 −0.72%, 299 −1.19%, 300 −3.43%
     (regressed), 303 +0.61% (unreliable), 304 0.0%, 310 +0.1%, 313 +0.35%, 314 +0.37%. Yet it got relevance 4 on
     CPU-bound VMs and outranked trx-commit-2 at canary points 310 rec 3650, 313 rec 3664 and 314 rec 3679.
   - Remedy: count the governor only when measured CPU frequency under load is below the maximum and the scaling driver
     can change it. On VMs it is cosmetic.
3. **R3 · Trade-off first on commit-bound benchmark loads.**
   - Remedy: under the benchmark policy, when commit wait plus the redo-path CPU is ≥20% of database time, trx-commit-2
     and log-writer-threads OFF go before generic safe ideas.
   - Evidence: trx-commit-2 measured +19.0%, +24.3%, +22.8% and +29.5%; LWT-off +11.0%, +11.2% and +22.8%. Rules now
     put best-practices or ahi-on first.
4. **R4 · `best-practices-swap` should be advice unless there is memory pressure or OOM risk.**
   - Evidence: ranked top 7 times in 294 with PSI memory 0 (judge 0 every time); measured −0.3%, +1.5% and −0.0%.
5. **R5 · Persistent hypervisor steal (303).**
   - Evidence: steal of 2.0-2.7% in every run, including the baseline, against an invalid-at threshold of 1.56% made 32
     of 36 runs unreliable, and each experiment was re-measured, using 2 runs per idea.
   - Remedy:
     - When the baseline has the same steal, treat it as the environment level: compare runs at similar steal, or raise
       the threshold to steal + noise.
     - After 3 unreliable runs in a row, stop with a "measurement environment unsuitable" finding instead of
       re-measuring.
6. **R6 · Scheduler and concurrency levers at ≥16× vCPU connections.**
   - Evidence: os-autogroup measured −27.5% (300) and −3.2% (299) at 1024 threads, against +0.7%, +1.7%, +1.4%, +0.2%
     and −0.1% at 64 threads. Concurrency limiters lost −17.5% (299, an AI idea) and −8.3% (288, the rule's sweep).
   - Remedy: os-autogroup only when other processes compete for the CPUs, never at 1024 connections. Extend P3/P4 to
     AI-proposed `innodb_thread_concurrency` (`llmassist.sanitize`).
7. **R7 · `ahi-on`.**
   - Evidence: RULES NOW picked it at 33 points; measured in 6 sessions with 0 wins: 294 −7.5% (regressed), 297 −0.1%,
     299 +0.6% (efficiency loss), 300 +2.9% (neutral), 304 +0.0%, 313 +1.0%.
   - Remedy: require B-tree search CPU above the noise threshold and secondary-index lookups. Let measured experience
     (already in production) weigh more.

### Method

- **Hindsight first.** Report measured outcomes first and judges second. A blind judge grading packets rewarded the
  options that later measured regressions (finding above).
- **Fairness rule:** allow experience from sessions that ended before the decision point (needs a `before=` filter on
  `/experience`). Without it, the backtest penalises both rules and planner on repeat workloads such as canaries.
- **Budget:** check OpenRouter account credits (`/credits`), not only the key limit, before a backtest.

## Follow-up: standing regression check (`tools/dbench/regress.py`)

The backtest is now a check that scores every agent build on the same real decisions, measured outcomes first.

**Corpus** (`ops/dbench/corpus/`, versioned, append-only):
- v1 holds 409 decision points: the 123 above plus the 286 audited points of `labels.json` (sessions 148-282).
- Each point stores its engine context as known then (`build.context_at`).
- `regress.py add` appends every closed session that ended since the last add. Scripted `pipeline:` studies are left
  out, and points already in the corpus never change.
- Each add rebuilds the outcome table from production (read-only): every measured experiment's changes against its
  parent, verdict, goal delta and noise.
- Comparable sessions are grouped by benchmark and data, threads per vCPU and CPU class. The four groups above are kept
  explicitly.

**Score** (rule engine, offline, about 10 s for 409 points):
- A run pick scores the delta of the first exact measurement of its settings: same session after the point, else a
  comparable session. Deltas are clipped at ±30%. Unreliable runs and unmeasured picks count 0, and the report shows
  coverage.
- A STOP scores minus the largest later confirmed win beyond the noise in its session. Runs saved are counted after
  each session's first STOP.
- The report also lists picks of options measured as regressions and policy violations (a trade-off chosen under
  `safe`, or advice run).
- Outcome score = mean value per decision.
- The comparison is against the latest accepted baseline (`ops/dbench/regress/<build>.json`). Both builds' choices are
  rescored on the current outcome table, and every changed decision is listed with its known outcome.
- A REGRESSION (exit 1) is any one of: an outcome-score drop of more than 0.10 pp, more regressed picks, or new policy
  violations.
- `--planner --budget USD` adds the AI planner through `run_remote.py` on the server. Answers are cached per point,
  packet hash, model, effort and server prompt.

**Today's fixes in measured terms** (HEAD `7e8164b` vs `220de7e`, 409 points; 52 decisions changed, 87% the same):

| | before fixes (`220de7e`) | after (`7e8164b`) |
|---|---|---|
| outcome score, pp per decision | −0.07 | **+0.44** (backtest points +0.89, audited +0.25) |
| measured picks: mean delta | −1.25% (154) | **+1.33%** (147) |
| improved / regressed picks | 31 / 25 | 36 / 22 |
| sum of regressed picks | −166% | −145% |
| STOPs good / bad (missed) | 54 / 3 (8.5%) | 54 / 5 (15.9%) |
| sessions stopped / runs saved | 9 / 116 | 9 / 123 |
| policy violations | 0 | 0 |
| expert agreement (253 audited) | 28% | 26% |

The 35 changed decisions with a measured difference net +209 pp:

- **trx-commit-2 first on commit-bound benchmark loads:**
  - It now goes first at 310, 313, 314 and 288, where it measured +19.0%, +24.3%, +22.8% and +25.7%. Before:
    best-practices (+0.1 to +0.4%) and a purge sweep (−1.2%).
  - It is also chosen at 25 audited points in 148-221, where it was never run. These are unmeasured, so coverage is
    lower. Two of them had measured wins before: 205 redo-grow +19.3% (verdict neutral) and 220 +12.8%.
- **ahi-on gated:**
  - 294: ahi-on (−7.5%) is replaced by spin-loops (−0.2%), os-autogroup (+0.7%) and huge pages (+2.9%).
  - 299 and 300: other ideas within ±2%, including one regression: 299 os-jemalloc −2.1%.
- **Swap experiment only with memory pressure:**
  - 303: replaced by huge pages (+8.7%, neutral), then a good STOP. 28 runs followed with no win.
  - 294: the session now stops after r16 instead of r20. It misses the AI idea at r21 (+3.7%), which accounts for the 2
    extra bad STOPs.

Run:
- `python3 tools/dbench/regress.py score` for the working tree, or `--agent <checkout>/agent --build <name>` for
  another build.
- `regress.py add` weekly.
- `regress.py baselines` lists saved baselines.
- Unit tests: `python3 tools/dbench/test_regress.py`.
- Nightly cron: `17 3 * * * $HOME/aituner/tools/dbench/regress_cron.sh`. It scores origin/main in a temporary checkout
  and writes a line to `ops/alerts.log` only on a regression.
