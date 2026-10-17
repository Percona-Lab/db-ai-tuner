# DB AI Tuner

An AI-assisted database tuner for MySQL-family servers (MySQL, Percona Server, MariaDB), inspired by
Releem, OtterTune and DBtune. Version 1 focuses on two use cases:

* **Benchmark tuning** – find the configuration that maximises throughput (TPS) or minimises average
  query response time for a given workload, with honest measurement (same windows, noise threshold,
  external benchmark metrics, verdict per run).
* **Agentic use** – every agent command is non-interactive, has `--json` output and clear exit codes,
  so Claude Code (or any coding agent) can drive a tuning session end to end.

```
 ┌────────────── database host ──────────────┐          ┌──────── web app ────────┐
 │ mysqld ◄── tuning-agent CLI (Claude Code) │  HTTPS   │ accounts, API keys      │
 │              │  baseline / measure /       │ ───────► │ environments, sessions, │
 │              │  recommend / apply          │          │ runs, config diffs,     │
 │ tuning-agent daemon (10 s OS+DB samples) ──┼────────► │ graphs, decision inputs │
 └────────────────────────────────────────────┘          └─────────────────────────┘
```

## Concepts

| Term | Meaning |
|---|---|
| Environment | One database server with a connected agent (hardware, OS and server facts, live metrics). |
| Session | One tuning effort with a **goal** (`tps` or `latency`) and a **policy** (`safe` = production-safe changes only; `benchmark` = durability/observability trade-offs allowed). |
| Run | One configuration measured over one window. Stores the full `SHOW GLOBAL VARIABLES`, the changes since the previous run (with source and reason), summary metrics, decision inputs (status-counter rates, top waits, top statement digests, InnoDB status), diagnosis and verdict. |
| Recommendation | Ranked list of changes produced from the last run: rationale, evidence, risk, restart need, impact/confidence, plus history awareness (already-tried changes, revert of regressions). |

## Repository layout

* `agent/` – `tuning-agent` Python package (only dependency: PyMySQL)
  * `baseline.py` – hardware + version based starting configuration
  * `collector.py` – OS (`/proc`) and DB sampling, measurement-window analysis
  * `engine.py` – diagnosis (CPU / IO / memory / redo / commit / locks / swap / concurrency) and rules
  * `applier.py` – managed option file (`zz-tuning-agent.cnf`), `SET GLOBAL`, restart, verification, automatic rollback when the server fails to start
  * `cli.py` – commands
* `server/` – FastAPI + SQLite web app and single-page UI (no build step)
* `docs/claude-code-skill/SKILL.md` – instructions for Claude Code (served at `/agent-guide.md`)

## Agent commands

```
tuning-agent init --server URL --api-key KEY [--name NAME] [--mysql-user U --mysql-password P] [--no-restart]
tuning-agent facts | status | config | guide
tuning-agent baseline [--apply] [--reserve-mem 1G] [--max-connections N]
tuning-agent session start --goal tps|latency [--policy safe|benchmark] [--name N] [--workload W] [--noise-pct 2]
tuning-agent measure start|stop|abort [--label L] [--external key=value ...] [--goal-value X]
tuning-agent measure run --duration 300 [--warmup 60] [--label L]
tuning-agent recommend
tuning-agent apply --next | --rec ID ... | --set var=value ... | --unset var  [--reason "..."] [--no-restart]
tuning-agent rollback --best | --to-run N | --reset
tuning-agent session stop
```

## Install the agent

From the web app's *Connect agent* page:

```
curl -fsSL https://<server>/install.sh | sudo bash -s -- --api-key KEY --name my-db
```

## Deploying the web app

Ubuntu 24.04, Python 3.12, nginx; see `server/deploy/` (systemd unit, nginx site, `update.sh`).
Environment: `/etc/aituner.env` with `TUNER_PUBLIC_URL` and `TUNER_DB`.

## Status

v0.1 – MySQL family only; goals TPS and average query response time. Planned: more goals (p95/p99,
throughput at latency SLO), PostgreSQL, web-issued commands to the agent, Bayesian search over the
experiment space, workload-replay safety checks for production.
