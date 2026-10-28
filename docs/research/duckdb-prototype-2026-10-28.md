# DuckDB for metrics and runs: prototype (2026-10-28)

**Question.** Should the server keep `metrics` (raw 10 s agent samples) and `runs` in DuckDB instead of SQLite?

**Answer.** Not as the primary store. DuckDB is 3–250x faster for whole-table analytics and stores the data 4x
smaller. But it is 3–170x slower on the page-serving point lookups and writes the server makes all the time. It also
cannot be shared by the server's processes, and it runs out of memory at tuner-web's size on the large run documents.
The gains are available without the costs:
* typed columns for the few metric fields we aggregate (SQLite does the 7-day steal query in 5 ms that way already);
* a periodic Parquet export for heavy analytics, read by DuckDB.

## Setup

* A copy of production `tuner.db` (VACUUM INTO, 2026-10-28 01:00 UTC, 4.7 GB): `metrics` 695,462 samples (3.1 GB of
  JSON), `runs` 3,676 rows (1.1 GB, mostly `inputs`: 804 MB of JSON, up to 830 KB per run).
* Same schema in DuckDB 2.0.0: JSON in JSON columns, the same primary keys, plus `metrics_cols` with six typed fields.
* SQLite 3.46 as the server opens it (WAL, synchronous=NORMAL, autocommit, explicit BEGIN for batches).
* This VM (2 vCPU, 3 GB RAM); DuckDB limited to 1 thread and 1 GB, as on tuner-web (1 vCPU, ~2 GB RAM).
* Script: `tools/duckdb/ddb_bench.py` (`load`, then `bench`). The queries are those of `server/app/main.py` and
  `similar.py`. Parameters are 40 random runs, 30 random sessions and all 53 environments.

## Results (median latency)

| Query (where in the server) | SQLite | DuckDB | |
|---|---|---|---|
| Q1 a run's metric window, ordered (run page charts) | 3.5 ms | 10.7 ms | 3x slower |
| Q2 latest sample of each of 53 environments (environments list) | 1.7 ms | 83 ms | 49x slower |
| Q3 1 h of samples, parsed (wait chart) | 20 ms | 17 ms | same |
| Q4 7 days of steal, one environment, from JSON | 1.2 s | 0.55 s | 2x faster |
| Q4b same, typed column (SQLite: `steal_samples`) | 5.5 ms | 2.2 ms | both fine |
| Q4c whole table: tps / steal per env and day, from JSON | 27.4 s | 9.7 s | 3x faster |
| Q4d same on typed columns | 0.55 s | 0.11 s | 5x faster |
| R1 session page: all runs of a session | 26 ms | 612 ms | 24x slower |
| R2 run page: one run by id | 1.8 ms | 53 ms (p95 191) | 29x slower |
| R3 case-base scan: every run's summary, parsed (similarity) | 3.2 s | 0.35 s | 9x faster |
| R4 verdicts by workload type, `inputs` JSON of all runs | 9.7 s | out of memory at 1 GB; 2.5 s at 2 GB | |
| W1 agent ingest: 6-sample upsert, one transaction | 0.15 ms | 10.7 ms | 70x slower |
| W2 finishing a run: update its documents | 3 ms | 500 ms | 170x slower |

| Storage | SQLite | DuckDB |
|---|---|---|
| metrics (JSON) | 3.1 GB | ~0.98 GB |
| metrics, 6 typed fields | (only steal, `steal_samples`) | 30 MB (Parquet: 8.8 MB) |
| runs | 1.1 GB | under 0.1 GB (ZSTD on the JSON columns) |

## Concerns and limitations

1. **One writing process per file.** DuckDB lets one process open a database read-write; while it has it open, no
   other process can open it at all (not even read-only). Every process that uses `tuner.db` today would be affected:
   * the server runs 2 uvicorn workers, and 4 during a blue/green deploy;
   * the cron watchers and the alert scripts query it with the `sqlite3` CLI;
   * the nightly backup reads it.

   A switch needs a single writer process that serves the others (an internal API), or a single-worker server.
2. **Memory.** The large run documents are what DuckDB struggles with:
   * loading the runs failed at 1 GB, and so did DuckDB's own SQLite reader, which held the whole 1.1 GB table even
     for 200-row slices;
   * an analytic query over `inputs` (R4) needs about 2 GB, the whole RAM of tuner-web.

   SQLite works in a few MB of page cache.
3. **Point reads and small writes.** Columnar storage decompresses a whole column segment to read one row:
   * a run page's row is in a ZSTD-compressed segment of 800 MB of documents, found by a scan, hence 53–190 ms for one run;
   * every commit syncs the write-ahead log (10 ms), against SQLite's WAL with synchronous=NORMAL (0.15 ms);
   * an update of a large document rewrites it (500 ms).
4. **Indexes.** Checked with EXPLAIN: both the session page (`session_id = ?` with the `(session_id, seq)` index) and
   the run page (`id = ?`, the primary key) are sequential scans with filter pushdown, not index lookups. The "latest
   sample per environment" query is a top-N scan, not an index seek. DuckDB's indexes enforce keys; they do little for
   reads like these, and they live in memory.
5. **Upserts.** `INSERT OR REPLACE` becomes `INSERT ... ON CONFLICT DO UPDATE`: every write statement of the server would
   need review, not only the two benchmarked here.
6. **Migration and tooling.**
   * Two engines to back up: DuckDB has no online `VACUUM INTO`, so the backup would need `EXPORT DATABASE` or a
     copy while no process has the file open.
   * The ops scripts and tests that use SQLite would need rewriting.
   * The storage format is newer than SQLite's.

## Recommendation

* **Keep SQLite** for both tables (OLTP: agent ingest, run pages, session pages).
* **Typed columns for what we aggregate:** a narrow `metric_points` table (env_id, ts, cpu_steal, tps,
  threads_running, history list length, ...). It is written with the sample, as `steal_samples` already is. Q4d-style
  queries then take 0.5 s in SQLite instead of 27 s.
* **Downsample old raw samples** (proposed separately): it removes the growth that made storage a question.
* **For heavy analytics** (learner, cross-session studies, the similarity case base), export Parquet nightly (20 s for
  all metrics, 8.8 MB typed) and read it with DuckDB in a separate process. That gives the 5–100x scan speed without
  sharing the live file.
