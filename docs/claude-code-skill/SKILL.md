---
name: mysql-tuning-agent
description: Tune a MySQL / Percona Server / MariaDB server for throughput or latency with the AI DB Tuner tuning-agent (benchmark profile + unattended autotune, or step by step: baseline, measured runs, recommendations, apply/rollback). Use when asked to tune or benchmark-optimise a MySQL-family database on a Linux host.
---

# Tuning MySQL with tuning-agent

These instructions are for AI coding agents (Claude Code, Codex CLI, Gemini CLI, Cursor, Aider, or any other
agent that can run shell commands on the database host) and for people.

`tuning-agent` runs on the database host (as root). It measures the server, recommends
configuration changes with evidence, applies them (writing its own option file and
restarting mysqld when required) and reports everything to the AI DB Tuner web app
(__SERVER__), where people see sessions, runs, config diffs, graphs and decision inputs.
The person you work for needs an account there and an API key (__SERVER__/quickstart);
ask them for the key if you do not have it. Never print the key back in full or store it outside the agent's config.

## Fastest path: unattended autotune of a sysbench workload

Use this when asked to "tune MySQL on this VM with sysbench" (or any benchmark) and no other workflow is given.
Run everything as root on the database host. `autotune` takes hours, so start it detached and poll (step 6).

1. **Agent.** `tuning-agent status --json` works → installed. Otherwise:
   `curl -fsSL __SERVER__/install.sh | sudo bash -s -- --api-key KEY --name "$(hostname)"`
   (add `--mysql-user U --mysql-password P` when root cannot log in over the local socket).
2. **sysbench.** `apt-get install -y sysbench` (Debian / Ubuntu) or `dnf install -y sysbench` (EPEL).
3. **Data set, sized for this host.** In memory by default: about a quarter of RAM, 1M rows ≈ 240 MB.
   ```bash
   RAM_GB=$(awk '/MemTotal/ {print int($2/1048576)}' /proc/meminfo); CPUS=$(nproc)
   TABLES=8; ROWS=$(( RAM_GB * 1024 / 4 / 240 / TABLES * 1000000 )); [ $ROWS -lt 100000 ] && ROWS=100000
   THREADS=$(( CPUS * 2 )); [ $THREADS -lt 8 ] && THREADS=8
   mysql -e "CREATE DATABASE IF NOT EXISTS sbtest; CREATE USER IF NOT EXISTS sbtest@localhost IDENTIFIED BY 'sbtest';
             GRANT ALL ON sbtest.* TO sbtest@localhost"
   sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-db=sbtest \
     --tables=$TABLES --table-size=$ROWS --threads=$CPUS prepare
   ```
   Use the socket path of this server if it is not the default (`--mysql-socket=...`). Larger than memory (I/O-bound):
   size the data at 2-3x RAM instead.
   Loading can take hours on large data sets. The agent's daemon reports common loaders (sysbench / go-tpc `prepare`,
   BenchBase, myloader, dump loads) on the web page by itself; for other steps say what is happening:
   `tuning-agent activity "loading data" "TPC-C 1000 warehouses" --seconds 7200`.
4. **Benchmark profile** `/root/sysbench.ini`. `warmup = auto` and `duration = auto` let the agent find the run length
   this benchmark needs (one probe run); `{total}` is filled in by the agent.
   ```ini
   [bench]
   name = sysbench oltp_read_write 8 x ROWS, THREADS threads
   run = sysbench oltp_read_write --mysql-user=sbtest --mysql-password=sbtest --mysql-db=sbtest --tables=8 --table-size=ROWS --threads=THREADS --time={total} --report-interval=5 run
   warmup = auto
   duration = auto
   goal_metric = sysbench_tps

   [metrics]
   sysbench_tps = transactions:\s+\d+\s+\(([\d.]+) per sec
   sysbench_p95_ms = 95th percentile:\s+([\d.]+)

   [requires]
   max_connections = THREADS + 50
   ```
   Write the numbers in (no shell variables in the file). `[requires]` holds settings the benchmark needs just to run;
   they are applied before every run and never tuned. A writing benchmark that must start each run from the same data
   adds `reset = /path/to/restore.sh` (see __SERVER__/agent-guide.md#benchmark-hygiene-important).
5. **Session and autotune.**
   ```bash
   tuning-agent baseline --apply --reserve-mem 1G   # hardware-sized start; 1G kept for the co-located sysbench
   tuning-agent session start --goal tps --policy safe --name "sysbench oltp_read_write on $(hostname)"
   nohup tuning-agent autotune --bench /root/sysbench.ini --close > /root/autotune.log 2>&1 &
   ```
   `--policy safe` never applies durability or observability trade-offs. Use `--policy benchmark` with
   `autotune --tradeoffs` only when the person asks for benchmark-only settings (results are then not
   production-equivalent: say so). Default limits: 3 calibration runs, then up to 10 experiments (`--max-runs N`).
6. **Watch.** `tuning-agent status --json` (current phase and time left) and `tail /root/autotune.log` every
   few minutes; the session page in the web app shows every run as it lands. Do not run other load or apply
   changes while it runs.
7. **Report.** When autotune ends, `tuning-agent config` shows the settings it kept (it leaves the best
   production-safe configuration applied). Report the gain over the calibrated start and the noise threshold from
   `tuning-agent status --json` / the session page, and link the session page.

The rest of this guide is the step-by-step workflow, for when you drive each experiment yourself.

Add `--json` to any command for machine-readable output. Exit code 0 = ok, 2 = usage/state
error (message in `error`), 3 = server/API error.

## Install (once per host)

API keys: __SERVER__/#/connect (an account is free; __SERVER__/quickstart walks through it).

    curl -fsSL __SERVER__/install.sh | sudo bash -s -- --api-key KEY --name NAME [--mysql-user U --mysql-password P]

## Workflow

1. `tuning-agent status --json` — check the daemon is running (`daemon_running`), whether a
   session/measurement is active and what is pending.
2. `tuning-agent facts` — hardware, OS, server version, data size.
3. `tuning-agent baseline` (preview) then `tuning-agent baseline --apply` — hardware/version-based
   starting configuration. Use `--reserve-mem 1G` if the benchmark client runs on the same host.
4. `tuning-agent session start --goal tps|latency --policy safe|benchmark --name "..." --workload "..."`
   * `safe`: never applies durability/observability trade-offs (shown as advice).
   * `benchmark`: may apply them (innodb_flush_log_at_trx_commit=2, sync_binlog, performance_schema=OFF, ...).
     Say so explicitly in any report: such results are not production-equivalent.
5. Start the workload. Warm up (caches, buffer pool) before measuring.
6. **Calibrate noise (standard protocol):** `tuning-agent calibrate --runs 3 --warmup auto --duration 300`
   measures the starting configuration three times. The run-to-run variation sets the session's noise
   thresholds for the goal (2.5 x CV, at least 1%) and for each work-per-transaction metric.
   Afterwards measure each experiment once: `tuning-agent measure run --warmup 60 --duration 300`
   or `measure start` ... `measure stop [--external tps=1234 --external p95_ms=12]`.
   If the benchmark prints its own throughput/latency, pass it with `--external`; a key named like
   the goal (`tps`, `nopm`, `latency`) becomes the goal value.
7. `tuning-agent recommend --json` — diagnosis + ranked items (`id`, `changes`, `rationale`,
   `evidence`, `risk`, `restart`, `impact`, `confidence`) and `next` (suggested experiment).
   Add `--llm` to also ask the account's configured LLM (OpenRouter, set in the web app) for knob ideas. Without it, the
   model is consulted automatically only when it adds value (rule-based experiments exhausted, or no clear bottleneck);
   its suggestions are validated and appear with an "LLM" badge and ids `llm-...`. When you are the LLM driving the agent,
   you usually do not need this: propose your own ideas with `apply --set var=value --reason "..."`.
   To validate a hypothesis across several values, queue an experiment instead of single changes:
   `tuning-agent experiment add --sweep innodb_buffer_pool_size=1G,2G,3G --repeats 2 --reason "..."` or
   `tuning-agent experiment add --try var=value --try var2=value2`; `experiment run --bench PROFILE` (or the next
   `autotune` iteration) measures every value from the same parent configuration and keeps the best one only if it
   beats the noise threshold. `experiment list --json` shows per-value results. The engine and the LLM queue sweeps
   themselves when a numeric knob's best value is uncertain.
8. `tuning-agent apply --next` or `--rec ID` or `--set var=value --reason "why"` (your own idea;
   always give a reason, it is shown in the web UI). `--unset var` stops managing a variable.
9. Measure again with the same duration and load. The run is compared with the MEAN of its parent
   configuration's runs. `verdict`:
   * `improved` / `regressed` — goal changed by more than the noise threshold;
   * `efficiency-gain` — goal within noise but mysqld/host CPU per transaction or disk I/O per transaction
     is measurably lower (keep it: same speed, less work, more headroom);
   * `efficiency-loss` — goal within noise but more work per transaction (revert);
   * `neutral` — nothing measurable (the engine will suggest reverting it); `repeat` — same config again.
   If a result is borderline, measure the same configuration again (`measure run`): repeats are averaged.
10. If regressed: `tuning-agent rollback --to-run N` (or apply the `revert-run-N` recommendation).
11. Finish: `tuning-agent rollback --best`, re-measure the best and the first configuration once more
    to confirm the gain is above noise, then `tuning-agent session stop`.

## Long-running commands

`measure run` blocks for warm-up + duration. Coding-agent shell tools usually time out after a few
minutes, so either run it in the background (and wait for completion), or use
`measure start`, wait with your own timer while the benchmark runs, then `measure stop`.
`baseline --apply` / `apply` may restart mysqld (buffer pool resize, read-only variables); a restart of
a multi-GB buffer pool can take a minute.

## Reading the results

* `diagnosis` classifies the bottleneck: cpu, cpu-pressure, io, memory, memory-pressure, redo, commit,
  locks, swap, concurrency. Pressure-stall numbers (PSI, `% of time stalled`) come from mysqld's own cgroup
  when available, so a co-located benchmark client does not distort them.
* Run summaries include host CPU and `mysqld_cpu_pct` (mysqld's share of the host).

## Benchmark hygiene (important)

* One change (or one coherent group) per run; identical duration, concurrency and data set.
* If the benchmark modifies data (TPC-C, sysbench write), restore the data set or accept drift;
  never compare a run on a grown database with one on a fresh one without saying so.
* A restart empties the buffer pool: warm up again before measuring. `--warmup auto` waits (60-900 s) until the
  buffer pool load has completed and throughput is stable; `measure run` uses it automatically if mysqld restarted
  in the last 10 minutes.
* Differences below ~2-3% are usually noise on cloud VMs; repeat runs before claiming a win.
* Do not run `measure` while applying changes; `apply` refuses during a measurement.
* The server restarting during a window invalidates it (`measure stop` fails; use `measure abort`).
