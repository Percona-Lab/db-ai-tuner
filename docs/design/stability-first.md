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
   - Only after that does it measure the warm-up and window, on the stable configuration.
   - With no stable configuration, the plan is provisional: the warm-up of the metrics that did settle and at most
     a 10-min window. A longer window would measure the drift, not the configuration.
   - (Session 324 shows the old behaviour: a 41-min warm-up and a 60-min window on an unstable configuration.)
2. **Purge triage (`tuning-agent purge-triage --bench P`, or inside the probe).**
   - Online settings, 5 min each, while the benchmark keeps running; the first minute after each change is not counted.
   - A setting is kept only when it at least halves the history growth, at no more than a 30% throughput cost.
   - The goal is safety with a margin: the triage stops when the growth is down to a quarter of the reference, or the
     history is shrinking. Borderline cuts are reverted.
   - Candidates, in order of cost to the workload:
     - purge threads at a higher priority (resource group `rg:purge prio=-10`, when mysqld has CAP_SYS_NICE);
     - more purge threads where this is online (MariaDB 10.5+; the maximum is 32 everywhere);
     - `innodb_purge_batch_size` 1000, then 5000;
     - fewer user threads creating undo at once: `innodb_thread_concurrency` 4x then 2x vCPUs (MySQL / Percona), or
       `thread_pool_oversubscribe=1` with the thread pool on;
     - last, and only when nothing else got there: `innodb_max_purge_lag` at the level reached, with a 0.3 s delay
       cap. It is a stopgap that throttles every writer.
   - The kept settings are recorded as changes (source `stability-triage`) and measured by the calibration that follows.
3. **Restart-only remedies are the session's first experiments** when the triage did not reach stability:
   - more purge threads in MySQL / Percona (not dynamic there);
   - enabling the thread pool (`thread_handling`).

   Ranking (`run_engine`), on busy CPUs:
   - purge priority, fewer threads inside InnoDB, purge threads, batch size;
   - the purge lag bound only after all of these were tried.

   A stabilizing change is kept at up to 30% throughput cost. The concurrency limit is exempt from the
   oversubscription block there, because it is judged by the history, not by throughput alone.
