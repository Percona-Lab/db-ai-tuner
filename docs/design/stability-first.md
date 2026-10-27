# Stability first: an unbounded undo history

**The problem.** At high concurrency, many user threads create undo faster than the purge threads remove it. The
history list then grows for as long as the load runs. Such a configuration cannot run in production, and it never
reaches a steady state in a benchmark.

## When the history counts as unbounded

- **Unbounded:** it grows at an undiminished rate through the window **and** passes 1,000,000 undo records
  (`engine.PURGE_UNBOUNDED_MIN`).
- **Not a stability problem:** growth that stays below that is a level still settling.
- **Held back, not slow:** a transaction open for 5 min or more (`oldest_trx_s`, sampled from
  `information_schema.innodb_trx`) holds a read view that purge cannot pass. In that case no purge setting is tried, and
  the advice `purge-blocked` names the cause.

## Order of work

1. **Find a stable configuration before sizing the runs.**
   - The run-length probe watches the history from minute 10. Once it is unbounded, the probe stops sizing and runs
     the purge triage inside the same benchmark run.
   - Only after that does it measure the warm-up and window, on the stable configuration - from a clean start: the
     benchmark is stopped and the profile's reset (or a restart with the page cache dropped) runs as before every
     measured run, with the triage's settings kept. Measured on the server the triage left behind, the warm-up came out
     wrong both ways: 101 min in session 381 (the triage's ~10M history backlog draining), 1 min in session 394 (every
     metric already at its working level after the triage's restart).
   - Unbounded again after the clean start (session 388: stable during the probe, not after the reset) means not
     stable: the provisional plan below, and the triage again at the calibration.
   - With no stable configuration, the plan is provisional: the warm-up of the metrics that did settle and at most
     a 10-min window. A longer window would measure the drift, not the configuration.
   - (Session 324 shows the old behaviour: a 41-min warm-up and a 60-min window on an unstable configuration.)
2. **Purge triage (`tuning-agent purge-triage --bench P`, or inside the probe).**
   - Online settings, 5 min each, while the benchmark keeps running; the first minute after each change is not counted.
   - A setting that alone makes the history stable is kept **whatever it costs**: first a configuration without
     unbounded growth, then performance tuning from it (user, 2026-10-25).
   - A partial cut is kept only when it at least halves the growth at no more than a 30% throughput cost.
   - The goal is safety with a margin: the triage stops when the growth is down to a quarter of the reference, or the
     history is shrinking. Borderline cuts are reverted.
   - Candidates, in this order (the cheap online ones first, then the biggest lever, so that what it combines with
     fits the budget - matrix P5, session 392: 32 purge threads cut the growth 88% as the last step, with no time left
     to combine it):
     - online, 5 min each: purge threads at a higher priority (resource group `rg:purge prio=-10`, when mysqld has
       CAP_SYS_NICE); `innodb_purge_batch_size` 1000, then 5000; fewer user threads creating undo at once:
       `innodb_thread_concurrency` at the vCPU count, then half of it (MySQL / Percona);
     - more purge threads, most first: 32 (the cap) at the highest priority (-20) - the fastest purge InnoDB can run,
       the upper bound of what purge alone can do - then 8 and 4 at a higher priority (a restart in MySQL / Percona;
       online in MariaDB 10.5+);
     - the thread pool and its settings (restart to enable): on, oversubscribe 1, half the vCPUs as groups,
       `thread_pool_max_threads` = 2x vCPUs, `thread_pool_stall_limit` 1000 ms;
     - purge threads on their own CPUs (the last 2) at the highest priority;
     - combinations: once a remedy cuts the growth per transaction substantially (partial), time for 3 combinations is
       reserved; it is combined with the other partial ones, with any remedy that cut the growth by 10% or more, and with
       the purge-speed remedies (a concurrency cap frees CPU for more purge threads);
     - last, only when nothing else is left: `innodb_max_purge_lag` at the level reached, with a 0.3 s delay cap
       (`innodb_max_purge_lag_delay`; with its default 0 InnoDB applies no delay at all). It is a stopgap that
       throttles every writer.
   - The kept settings are recorded as changes (source `stability-triage`) and measured by the calibration that follows.
3. **Restart-only remedies are the session's first experiments** when the triage did not reach stability:
   - the thread pool (`purge-threadpool`: `thread_handling=pool-of-threads`, `thread_pool_size` = vCPUs; Percona
     Server, MariaDB). It caps the threads running at once, and so the undo they create, for any number of
     connections. It is likely to cost throughput, and it is preferred to the cruder `innodb_thread_concurrency` cap;
   - more purge threads in MySQL / Percona (not dynamic there; at most 32).

   Ranking (`run_engine`, the first 3 tries), on busy CPUs:
   - purge priority, the thread pool, the concurrency cap, more purge threads, batch size;
   - the purge lag bound only after all of these were tried.

   A change that makes the history bounded is kept whatever it costs (`evaluate.stabilized`), and the best
   configuration is chosen among stable ones. Tuning for performance continues from it. The thread pool and the
   concurrency cap are exempt from the oversubscription block there, because they are judged by the history, not
   by throughput.

## After a stable start: the remedy's own settings are tuned

The triage's remedy is a starting point. Its value and companions are swept, and only configurations that keep the
history bounded can win:

- **`innodb_thread_concurrency`:** values around the cap (half, 1.5x, 2x), plus `innodb_concurrency_tickets` (500 /
  2000 / 20000) and `innodb_thread_sleep_delay`.
- **The thread pool:** `thread_pool_size` (half, 1.5x, 2x) and `thread_pool_oversubscribe` (1 / 2 / 3 / 5).

The oversubscription block (no new concurrency limiter at 16+ connections per vCPU) does not stop tuning a limiter
already in place. The sweep includes no cap (0), first: other changes since, such as a larger buffer pool, may have
made the cap unnecessary.

**Caveat for the table below.** These canary runs (2026-10-25, 1024 threads on 8 vCPUs) had a 128 MB buffer pool
(server defaults) for about 1 GB of data. Purge waited on reads, and with an 896 MB pool the same load ran at 26.8k tps
with a history of 244. The remedies' ranking is being re-measured from a hardware-sized start. Autotune now warns
before measuring from such a start.

| Setting | Throughput | History |
|---|---|---|
| `innodb_thread_concurrency=8` | +20% | growth -94% |
| `innodb_thread_concurrency=4` | +2% | shrinking |
| thread pool, oversubscribe 1 | +18% | growth -65% |

## Clean canary rerun (2026-10-25, session 349)

The rerun used a hardware-baseline buffer pool (21 GB, 99.99% hit rate), 1024 update threads on 8 vCPUs, and
`trx_commit=2`. The reference grew the history by +2.69M/min at 46.1k tps, about one undo log per transaction, so purge
removed almost nothing.

| Remedy | History | Throughput |
|---|---|---|
| `innodb_thread_concurrency=8` | flat | +43.8% (the winner) |
| `innodb_thread_concurrency=4` | flat | +34.6% |
| Thread pool (defaults / oversubscribe 1 / half the groups / 16 threads / stall 1000 ms) | flat | +18% to +29% |
| `innodb_purge_batch_size=5000` | shrinking 1M/min | -1.7% |
| `innodb_purge_batch_size=1000`, purge priority, 4 / 8 / 32 purge threads at nice -10 / -20 | no effect | about 0 |

- **More purge threads or priority never help.** Purge is not short of CPU; it waits on per-batch coordination, and a
  larger batch amortises that.
- **Limiting concurrency is both the stability remedy and the throughput gain.**
- **The runs with a 128 MB buffer pool were I/O-bound and misleading.** There batch 5000 showed no effect, and the
  thread pool did not bound the history.
