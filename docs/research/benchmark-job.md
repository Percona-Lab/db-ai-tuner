# Join Order Benchmark (JOB) for the AI DB Tuner (2026-10-19)

JOB (Leis et al., "How Good Are Query Optimizers, Really?", VLDB 2015) runs 113 analytical join queries (33 templates,
4-17 joins each, `MIN()` aggregates) over the 2013 IMDb snapshot (21 tables, 74M rows). It is the standard test for
cardinality estimation and join ordering. In the tuner's benchmark set it is the **optimizer-bound, read-only** case:
the knobs that matter are `optimizer_switch` flags, `join_buffer_size`, `sort_buffer_size`, `tmp_table_size`,
`optimizer_search_depth`/`optimizer_prune_level`, plus the buffer pool when the data set does not fit.

Files: `bench/job/`

| File | Purpose |
|---|---|
| `install.sh` | download `imdb.tgz` (1.2 GB, event.cwi.nl/da/job), clone gregrahn/join-order-benchmark @a396036, make the MySQL query copy |
| `mysql_queries.sh` | copy the 113 queries to `/root/job/queries`, backquoting the one reserved-word alias |
| `schema.sql` | MySQL schema (generated from the repository's `schema.sql`, see below) |
| `gen_load.py`, `load.sh` | `LOAD DATA` per table, the paper's 23 foreign-key indexes (`fkindexes.sql`), `ANALYZE TABLE`; optional histograms |
| `histograms.sql` | optional (`JOB_HISTOGRAMS=1 load.sh`): histograms on the filtered non-indexed columns |
| `run_job.sh [cap_s]` | all 113 queries once, sequentially, `max_execution_time` cap, per-query lines + summary |
| `job_warm.sh` | profile `reset` hook: reads title's PK, the `movie_id` join indexes and the small tables (~22 s) |
| `job.ini` | benchmark profile (goal `job_total_s`, latency, `data_state = read-only`) |
| `pilot.sh`, `queue-pilot-atdb3.sh` | pilot: 2 repeat runs, then `session.sh job.ini ... latency 8` |

## MySQL port

An existing port exists (github.com/olros/join-order-benchmark). It was not used: its `LOAD DATA` has only
`FIELDS TERMINATED BY ','` (no `ENCLOSED BY '"'`, no backslash escapes, no NULL handling), so quoted titles with
commas split into wrong columns and empty integers load as 0 instead of NULL; it keeps the default
case-insensitive collation; the queries are only reformatted to one line. Our port:

- **Queries**: the original text runs unchanged on MySQL 8.4 (implicit comma joins, `LIKE`, `IN`, `BETWEEN`, `MIN`).
  The only change: `AS character` (10b, 10c) is reserved in MySQL and is backquoted.
- **Schema**: `integer` -> `INT`, `character varying(n)` -> `VARCHAR(n)`, `text` -> `VARCHAR(n)` sized from the data
  (maximum length measured over the CSVs, rounded up), `MEDIUMTEXT` for `movie_info.info` and `person_info.info`
  (up to 19.8k / 55.7k characters). Primary keys as in the original.
- **Collation**: database `utf8mb4_0900_bin`. With the default `utf8mb4_0900_ai_ci`, `LIKE '%Bros%'` and `=` become
  case- and accent-insensitive, which changes the result sets and selectivities compared to PostgreSQL.
- **CSV format**: PostgreSQL COPY CSV with backslash escapes; an empty unquoted field is NULL. `LOAD DATA ...
  FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' ESCAPED BY '\\'`, nullable columns through `@var` +
  `NULLIF(@var, '')`. Two rows have unquoted `\,` escapes (title 2522636 `Frag'ile,`, person_info 2671660); MySQL's
  escape handling reads them correctly. Row counts match the PostgreSQL load (title 2,528,312, cast_info 36,244,344,
  movie_info 14,835,720, movie_info_idx 1,380,035, person_info 2,963,664).
- **Indexes**: primary keys plus the 23 foreign-key indexes of the paper (`fkindexes.sql`).
- **Statistics**: `ANALYZE TABLE` on all tables. **Histograms are off by default**: most MySQL servers have none, and
  without them the optimizer uses fixed guesses for predicates on non-indexed columns (`kind = 'movie'`,
  `info LIKE '%sequel%'`, `production_year > 2005`). That estimation error is exactly what JOB exposes, and the tuner
  cannot change it with knobs (histograms are part of the data set, not configuration). `histograms.sql` builds them
  for a "well-maintained statistics" variant; use a separate profile name if a study needs it.
- **Load**: not binlogged (`sql_log_bin = 0`), redo log disabled during the load (`ALTER INSTANCE DISABLE INNODB
  REDO_LOG`, re-enabled on exit). 527 s on atdb3 (2 vCPU). Size on disk 9.0 GB (cast_info 4.1 GB, movie_info 1.9 GB).

## Run and metrics

`run_job.sh 60` runs the queries in order (1a ... 33c), one `mysql` client per query, each with
`SET SESSION max_execution_time = 60000`. A capped query counts as the cap. Output:

    JOB 17a time=27.819 status=ok
    JOB total_s=1230.07 geomean_s=2.7526 capped=4 errors=0 queries=113

Profile metrics: `job_total_s` (goal, latency: lower is better), `job_geomean_s`, `job_capped`, `job_errors`,
`job_queries_over_10s`. `warmup = 0`, `duration = 0` (until the query set ends), `data_state = read-only`,
`reset = job_warm.sh`.

## Pilot on atdb3 (2026-10-19)

Host: Linode `atdb3`, 2 vCPU, 4 GB RAM (3.8 GiB usable, 0.5 GB swap), SSD block storage, Percona Server 8.4.11,
binary log ON. The data set (9.0 GB) is about 4x the agent's baseline buffer pool (2.3 GB).

### Query-time profile (60 s cap, baseline configuration)

- 113 queries in ~21.5 min; median query 5.2 s; 30 queries < 1 s, 47 >= 10 s.
- Time is concentrated in the slow third: queries of 10-30 s sum to ~595 s, 30-60 s to ~563 s, 1-10 s to ~136 s,
  < 1 s to ~10 s. The 1-second queries barely move the total; the goal is dominated by ~25 queries.
- Capped at 60 s in every run: 10c, 16b, 18c, 25a, 25c (5 queries = 300 s of the total).
- Cap choice (from the first full run): cap 15 s -> 774 s total / 31 capped; 20 s -> 899 / 20; 30 s -> 1061 / 11;
  45 s -> 1166 / 5; 60 s -> 1230 / 4. 60 s keeps the capped set small and stable; a 30 s cap would make runs
  ~4 min shorter but hide improvements on 11 queries.

### Bottleneck on this host

Not purely optimizer-bound here: the data does not fit in memory. During 17a `vmstat` showed 30-37% iowait and
40-55 MB/s reads with one CPU busy (single stream). The agent's own diagnosis of the server-defaults run (128 MB
pool): "Read-bound I/O: 3043 page reads/s, reads are 100% of disk operations", PSI io stall 43%, iowait 33%.
So on a small box JOB is **read-I/O bound + single-thread CPU + optimizer**: buffer pool size, read-ahead
(`innodb_read_ahead_threshold`, `innodb_random_read_ahead`), `innodb_read_io_threads` and join algorithm choices
that change the access pattern (BKA/MRR, hash join vs. index nested loop) all matter. On a host with RAM >= 12 GB it
would become the CPU/optimizer benchmark the paper intends. Single stream: at most one of the two vCPUs is used.

### Repeatability

| Run | config | job_total_s | geomean_s | capped |
|---|---|---|---|---|
| exploratory | baseline (from earlier sessions) | 1230.1 (+ 10b/10c failed, before the alias fix) | 2.75 | 4 |
| pilot 1 | baseline | 1304.1 | 3.226 | 5 |
| pilot 2 | baseline | 1284.0 | 3.181 | 5 |
| session #107 server defaults | 128 MB pool | 2569.8 | | |
| session #107 calibration (3 runs) | agent baseline | 1250.8 / 1254.0 / 1260.5 | | |

Pilot runs: CV 1.1% (n=2). Agent calibration inside the session: mean 1255 s, **CV 0.39%**, range 0.77%;
agent noise threshold ±2.0%. Largest per-query differences between the two pilot runs: 6f (36.8 vs 31.9 s),
31a, 30c, 22c (2-3 s each) - all I/O-heavy queries. Warm-up (`job_warm.sh`) takes 21 s. All profile metrics parse
(`bench.Profile.parse` on the run logs).

### Tuning session #113 (agent 45f171c, goal latency, max_runs 8, scope knobs,os)

A first session (#107, agent 3bceadc) was stopped after its calibration (mean 1255 s, CV 0.39%) to rerun on the
build with the read-ahead experiments.

| Run | Experiment | job_total_s | Verdict |
|---|---|---|---|
| 1 | server defaults (128 MB pool) | 2560.3 | reference |
| 2-4 | agent baseline (2.3 GB pool, ...) | 1254.0 / 1254.3 / 1257.3 (CV 0.15%) | improved -51% |
| 5 | best-practices | 1265.9 | neutral |
| 6 | subquery-to-derived | 1256.7 | neutral, reverted |
| 7-9 | sweep `innodb_read_ahead_threshold` = 8 / 32 / 64 | 1238.2 / 1232.5 / 1265.0 | 32 kept (+2.6%) |
| 10 | threshold 32, confirm | 1228.1 | repeat |
| 11 | bka-mrr (BKA + MRR) | 1292.7 | regressed (+5.1%), rolled back |
| 12 | `join_buffer_size` 256K -> 8M | 1195.9 | improved |
| 13 | best safe config, confirmation | 1186.5 | repeat |

Best: read-ahead threshold 32 + 8 MB join buffer, mean 1191 s = **-5.1% vs baseline** (noise ±0.5% after the
pooled CV). The diagnosis drove the right experiments: data-file I/O waits ~40% of database time -> read-ahead;
hash joins spilling -> join buffer. BKA/MRR hurts on this data (more random reads).

`canary_check.py 113 --budget 8`: 8 ok, 2 FAIL:
- "improvements confirmed by a repeat (unconfirmed: [7])": run 7 is a sweep point (threshold 8) that was not
  kept; the chosen point 32 was confirmed (run 10). Looks like a checker false positive for sweeps.
- "trade-off phase reached": the 8-run budget ended at run 13 before trade-offs; JOB runs of ~21 min make 8 runs
  a 5.5 h session. Use max_runs >= 12 or a bigger host if the trade-off phase matters.

### Notes / problems

- Run length: ~21 min per run on this host (45 min at server defaults); a full session with 8 experiment runs takes
  ~5.5 h. The 60 s cap keeps 5 queries capped at baseline (10c, 16b, 18c, 25a, 25c); 25 capped at server defaults.
- Not a pure optimizer benchmark on 4 GB RAM (see above). For optimizer focus use a host with >= 16 GB RAM.
- Histograms: off by default (see above); not tested in the pilot.
- `mysql -e "SOURCE file"` returns exit code 1 on success in this client version; load.sh pipes files instead.
