---
name: mysql-tuning-agent
description: Tune a MySQL / Percona Server / MariaDB server for throughput or latency with the AI DB Tuner tuning-agent (baseline, measured runs, recommendations, apply/rollback). Use when asked to tune or benchmark-optimise a MySQL-family database on a host where tuning-agent is installed.
---

# Tuning MySQL with tuning-agent

`tuning-agent` runs on the database host (as root). It measures the server, recommends
configuration changes with evidence, applies them (writing its own option file and
restarting mysqld when required) and reports everything to the AI DB Tuner web app
(__SERVER__), where people see sessions, runs, config diffs, graphs and decision inputs.

Add `--json` to any command for machine-readable output. Exit code 0 = ok, 2 = usage/state
error (message in `error`), 3 = server/API error.

## Install (once per host)

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
6. Measure: `tuning-agent measure run --warmup 60 --duration 300 --label NAME`
   or `measure start` ... `measure stop [--external tps=1234 --external p95_ms=12]`.
   If the benchmark prints its own throughput/latency, pass it with `--external`; a key named like
   the goal (`tps`, `nopm`, `latency`) becomes the goal value.
7. `tuning-agent recommend --json` — diagnosis + ranked items (`id`, `changes`, `rationale`,
   `evidence`, `risk`, `restart`, `impact`, `confidence`) and `next` (suggested experiment).
8. `tuning-agent apply --next` or `--rec ID` or `--set var=value --reason "why"` (your own idea;
   always give a reason, it is shown in the web UI). `--unset var` stops managing a variable.
9. Measure again with the same duration and load. Look at `verdict` (improved / neutral / regressed vs
   the previous run, using the session noise threshold) and `delta_vs_first_pct`.
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
* A restart empties the buffer pool: warm up again before measuring.
* Differences below ~2-3% are usually noise on cloud VMs; repeat runs before claiming a win.
* Do not run `measure` while applying changes; `apply` refuses during a measurement.
* The server restarting during a window invalidates it (`measure stop` fails; use `measure abort`).
