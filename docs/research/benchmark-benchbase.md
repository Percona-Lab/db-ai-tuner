# BenchBase in the AI DB Tuner benchmark set (pilot 2026-10-19)

BenchBase (CMU, github.com/cmu-db/benchbase, formerly OLTPBench) is a Java/JDBC driver with ~20 OLTP workloads. It adds
workload shapes the battery (sysbench, HammerDB TPROC-C, go-tpc TPC-H/CH) does not cover well: key-value point access on a
data set several times RAM (YCSB), very short telecom transactions at high concurrency (TATP), web read-mostly mixes
(Twitter, Wikipedia) and tiny write transactions on hot rows (SmallBank).

Pilot host: `atvbm` (Vultr bare metal Intel E-2286G 6c/12t, 32 GB, NVMe), Percona Server 9.7.2, binary log off, tuner
baseline with `--reserve-mem 4G` (23 GB buffer pool), BenchBase client co-located (JVM `-Xmx3g`).

## Files (bench/benchbase/)

| File | What |
|---|---|
| `install.sh` | openjdk-25 (apt), clone + `./mvnw clean package -P mysql`, unpack to `/opt/benchbase`, copy helpers to `/root/bench/benchbase` |
| `bb_load.sh <workload>` | create the database and load it (`--create=true --load=true`), ANALYZE, print `LOADED ... seconds= size_gb=` |
| `load_all.sh [workloads]` | all loads in a row with `innodb_flush_log_at_trx_commit = 0` during the load |
| `<workload>.xml.tpl` | BenchBase config per workload; `@WARMUP@ @TIME@ @TERMINALS@` filled per run |
| `bb_run.sh <workload> <warmup> <duration> [terminals]` | the profiles' run command: renders the config, runs BenchBase, prints one `BBSUMMARY` line |
| `bb_summary.py <dir>` | `BBSUMMARY bench= throughput_rps= ... avg_ms= p50_ms= p95_ms= p99_ms= max_ms=` from `*.summary.json` |
| `bb-*.ini` | tuner profiles (metrics `bb_tps`, `bb_avg_ms`, `bb_p95_ms`, `bb_p99_ms`; goal `bb_tps`) |
| `pilot.sh` | metric checks (N measurement runs per profile in a session each, timed reset) and optionally one tuning session (`TUNE=`) |

Setup on a new host: `install.sh`, copy the directory to `/root/bench/benchbase`, `load_all.sh`, move any old
`/var/lib/mysql_base` aside, then `pilot.sh` / `session.sh` (which snapshots the pristine copy).

## Integration details

- **Measurement window.** Each run is one BenchBase work phase with `<warmup>{warmup}</warmup><time>{duration}</time>`:
  requests during the warm-up are not counted, so `throughput_rps` and the latency percentiles cover the measured window
  only, and they follow the agent's run-length changes (`{warmup}`/`{duration}` placeholders, `can_resize` true).
- **Use `throughput_rps`, not goodput.** BenchBase's `Goodput (requests/second)` divides successes *including the warm-up*
  by the measured time (TATP: 52k "goodput" vs 43k throughput). The summary line labels it `goodput_incl_warmup_rps`.
- **Isolation.** The BenchBase MySQL samples use `TRANSACTION_SERIALIZABLE`, which makes every InnoDB read a locking read;
  the templates use REPEATABLE READ (MySQL's default).
- **Java.** BenchBase main needs Java 23 (`pom.xml`); Ubuntu 24.04 has openjdk 21 and 25 only. With JDK 25 the build needs
  `-Dfmt.skip=true` (google-java-format 1.24 crashes) and `-Werror` removed (new JDK 25 lint warnings). Built commit is in
  `/opt/benchbase/COMMIT`.
- **Raw files.** BenchBase writes per-transaction CSVs; `bb_run.sh` keeps the summary/config/per-window CSVs (200 newest runs).
- **Data sets share one datadir** (`bb_*` databases) and one pristine copy. `reset_datadir.sh` (rsync) only copies files
  whose size/mtime changed, so a TATP reset does not copy the 120 GB YCSB-C table.

## Workloads and data sets

| Profile | Data set | Load time | Client | Bottleneck it exercises |
|---|---|---|---|---|
| `bb-ycsbc` | YCSB 100M rows x 10 x 100 B, 119 GB (3.7x RAM) | 23 min | 64 terminals, 100% read, zipfian 0.99 | read-bound (buffer pool hit rate, read I/O, read-ahead, LRU) |
| `bb-ycsba` | YCSB 60M rows, 71.5 GB (2.2x RAM) | 13.5 min | 64 terminals, 50% read / 50% update, zipfian 0.99 | flush-bound (dirty page flushing, redo, I/O capacity) + reads from disk |
| `bb-tatp` | 1M subscribers, 0.5 GB | 1 min | 256 terminals | commit-bound + CPU / concurrency (very short transactions) |
| `bb-twitter` | 100k users, 4M tweets, 0.9 GB | 13 s | 64 terminals, 90% GetUserTweets | CPU / optimizer (in-memory index range reads, ORDER BY LIMIT) |
| `bb-wikipedia` | 100k pages, 25 GB | 12 min | 64 terminals, 90% anonymous page reads, 7% edits | web mixed: CPU + large-row reads, some writes |
| `bb-smallbank` | 1M accounts, 0.2 GB | 6 s | 128 terminals, 85% write transactions | commit-bound + row-lock contention |

## Pilot: metric checks (3 runs per profile at the baseline configuration)

`tuning-agent measure run --bench` per profile (agent resets, runs, parses), sessions #101 #104 #105 #108 #110 #112
(study "Pilot: BenchBase"). All metrics parsed on every run.

| Profile | bb_tps (3 runs) | mean | CV | p95 ms (CV) | p99 ms | Reset | What the agent saw |
|---|---|---|---|---|---|---|---|
| bb-tatp | 43,281 / 43,228 / 43,360 | 43,290 | 0.15% | 22.5 (0.6%) | 33 | 54 s | CPU 100%, mysqld 61% of it, 175 running threads |
| bb-smallbank | 20,396 / 20,371 / 20,344 | 20,370 | 0.13% | 11.8 (0.2%) | 14.5 | 52 s | CPU-bound + commit |
| bb-twitter | 45,918 / 46,194 / 46,213 | 46,109 | 0.36% | 2.85 (0.5%) | 4.3 | 52 s | CPU 100%, mysqld 68%, bp hit 99.98% |
| bb-wikipedia | 2,428 / 2,491 / 2,470 | 2,463 | 1.3% | 54.7 (2.1%) | 187 | 179 s | **client-bound**: mysqld 26% of CPU, the JVM ~10 cores |
| bb-ycsba | 30,812 / 31,364 / 31,314 | 31,163 | 0.98% | 4.5 (6.2%) | 14.7-19.4 | 407 s | CPU 66%, iowait 16%, bp hit 98.6%, 4.2k reads/s, checkpoint age 64% |
| bb-ycsbc | 64,425 / 64,187 / 64,245 | 64,286 | 0.19% | 1.95 (0.6%) | 3.3 | none (read-only) | CPU 100% (mysqld 57%), bp hit 96.3%, iowait ~0 |

Reset time is the rsync of the files a run changed plus mysqld restart and cold start: ~50 s for the small sets,
3 min for Wikipedia (26 GB `text.ibd`), ~7 min for YCSB-A (the whole 72 GB table changes; rsync copies ~200 MB/s on the
same NVMe). The first snapshot of all six sets (228 GB) took 19 min.

Observations:
- YCSB-C at zipfian 0.99 is **not read-I/O-bound** on this box: the hot set fits the 23 GB pool (96% hit, iowait ~0) and
  the run is CPU-bound with the client taking ~40% of the CPU. For a read-I/O-bound profile use a lower skew
  (`<skewFactor>0.5</skewFactor>` or so) or a larger table; not yet piloted.
- YCSB-A is the most I/O-shaped profile (reads from disk + dirty page flushing, checkpoint age up to 64%).
- TATP, SmallBank, Twitter and YCSB-C are CPU-bound with a co-located client taking 30-45% of the CPU: tuning gains
  there will be small and partly client-side; a separate client host would make them DB-bound.

## Pilot: tuning session on YCSB-A (session #114, max_runs 8)

`TUNE=bb-ycsba MAX=8 pilot.sh` -> `session.sh` with `SCOPE=knobs,os`, `BASELINE_ARGS="--reserve-mem 4G"`, agent 45f171c.
15 runs, 3 h 15 min (each run = ~7 min reset + 2 min warm-up + 5 min measured).

| Run | Configuration | bb_tps | Verdict |
|---|---|---|---|
| 1 | server defaults | 15,987 | reference |
| 2-4 | baseline (calibration) | 31,185 / 31,393 / 31,777 | noise ±1.83% (calibrated) |
| 5 | best-practices | 31,588 | neutral |
| 6 | bp-grow | 31,836 | efficiency loss, reverted |
| 7 | log writer threads off | 29,681 | regressed (-6%) |
| 8 | redo-grow | 30,582 | regressed (-3.2%) |
| 9 | back to run 5 | 31,936 | repeat |
| 10 | adaptive hash index on | 31,580 | neutral |
| 11-12 | doublewrite OFF (trade-off) | 35,462 / 35,401 | improved +12% |
| 13 | O_DIRECT_NO_FSYNC (trade-off) | 35,473 | neutral |
| 14 | best with trade-offs, confirmation | 35,650 | repeat |
| 15 | best safe, confirmation | 31,857 | repeat |

Result: baseline doubles server defaults (+97%, mainly the buffer pool); no production-safe change beat noise on top of
the baseline; disabling the doublewrite buffer gives +12-13% (35,505 mean) as a durability trade-off, as expected for a
flush-bound update workload.

`canary_check.py 114 --budget 8`: all 10 checks ok (closed, no aborted runs, calibrated noise 1.83%, no tracebacks,
8/8 budget, parents consistent, improvements confirmed, best safe config recomputed [5, 9, 15], trade-off phase reached).

## Problems found

1. **BenchBase build**: main needs Java 23 (Ubuntu 24.04 has 21 and 25). With openjdk-25: `-Dfmt.skip=true`
   (fmt-maven-plugin NoSuchMethodError) and `-Werror` removed from `pom.xml` (new lint warnings). `install.sh` does both.
2. **Goodput includes the warm-up** (BenchBase `Results.requestsPerSecondGoodput` divides all successes by the measured
   time). Use `throughput_rps`.
3. **Wikipedia is client-bound**: `WikipediaWorker.executeWork` builds `new Zipf(rng, 1, num_pages, ...)` for every
   transaction, an O(num_pages) table; at sf 100 the JVM takes ~10 of 12 cores and mysqld 26% (thread dump: most
   runnable worker threads in `RandomDistribution$Zipf.<init>`). Fix upstream (build the Zipf once per worker) or use a
   separate client; until then `bb-wikipedia` measures the client, not the server. Twitter runs cleanly.
4. **YCSB-C is not read-bound at zipfian 0.99** on 32 GB: 96% buffer pool hit, iowait ~0, CPU-bound. Lower `skewFactor`
   (e.g. 0.5) or a larger table is needed for a read-I/O profile.
5. **Co-located client**: the JVM uses 30-45% of the CPU on the CPU-bound profiles (TATP, SmallBank, Twitter, YCSB-C);
   tuning gains there are capped. Sessions use `--reserve-mem 4G` for the JVM heap.
6. **YCSB-A reset is slow** (~7 min: the whole 72 GB table changes and rsync copies ~200 MB/s on the same NVMe), so a
   max_runs 8 session takes ~3 h. A smaller table (1.5x RAM) or a faster restore would halve that.
7. **First reset after the snapshot re-copied the 120 GB YCSB-C table** (~10 min) although no workload wrote to it
   (cause not determined; the live file changed between the snapshot and the first TATP run). Later resets skipped it.
8. **Tuner (minor)**: on a configuration change the agent logs "flushing ~386k dirty pages before the restart" and then
   runs the profile's reset, which replaces the data set anyway (`reset_datadir.sh` uses `innodb_fast_shutdown = 2`).
   That flush costs time on every config change of a reset profile. Evidence: `ops/benchbase/session-bb-ycsba.log`.
9. Host note: `tuning-agent rollback --reset` at the start moved the datadir back from the filesystem-experiment device
   (`/dev/sda`, now unmounted, its datadir untouched) to the root disk. The 2000W `tpcc` database in that root datadir
   was dropped for space; its pristine copy is kept in `/root/old-hdb2000w-base` (171 GB).
