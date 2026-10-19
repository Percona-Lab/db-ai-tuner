# Measurement overhead of the tuner (2026-10-18/19)

Question: how much does the instrumentation the tuner relies on cost, and does the agent's own sampling change the results
it compares?

Setup: Vultr bare metal `tuner-vbm2` (Intel E-2286G 6c/12t, 32 GB, NVMe), Percona Server 9.7, binary log off, HammerDB
TPROC-C co-located, 30 min runs, data set restored before every run, conditions interleaved, 3 repeats each.

| Condition | What is on |
|---|---|
| none | `performance_schema = OFF`, no agent |
| ps | `performance_schema = ON` with the server's stock instruments and consumers, no agent |
| agent | ps + the tuning-agent daemon sampling every 10 s (status counters, wait / statement summaries, OS counters) |
| lite | agent + `ps:profile = lite` (statement / transaction history consumers, memory, transaction and stage instruments off; digests, histograms and wait summaries stay) |

## Results (NOPM, mean of 3; run-to-run CV 0.1-0.6%)

| | none | ps | agent | lite | ps vs none | agent vs ps | lite vs agent |
|---|---|---|---|---|---|---|---|
| 200W | 159,254 | 152,150 | 152,413 / 151,769* | 153,570 | **-4.5%** | +0.2% | **+1.2%** |
| 2000W | 161,474 | 155,437 | 155,129 / 155,061* | 157,158 | **-3.7%** | -0.2% | **+1.4%** |

\* second series, interleaved with `lite`.

Agent CPU: about 20 CPU-seconds per 30 min run, 0.1% of mysqld's CPU time.

## Conclusions

1. **performance_schema with stock instruments costs 3.7-4.5%** on this CPU-bound OLTP workload. This is the server's
   own cost, present in any production server that keeps it on.
2. **The agent's sampling is not measurable** (within ±0.2%, inside run-to-run noise): reading the summary tables every
   10 s adds nothing the comparisons could see.
3. **`ps:profile = lite` recovers about a third of the performance_schema cost** (+1.2 to +1.4%) while keeping what the
   tuner uses (digests, latency histograms, wait summaries). It is offered as a safe experiment; `performance_schema = OFF`
   stays an observability trade-off.
4. The CPU profile (perf at 49 Hz for 20 s) runs at the end of the warm-up, outside the measured window, and therefore
   does not enter these numbers; while active it cost about 3.4% on a saturated 2-vCPU VM.

Raw data: `docs/research/data/overhead-*.csv`.
