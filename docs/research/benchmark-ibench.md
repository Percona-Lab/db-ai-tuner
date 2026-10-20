# Insert Benchmark (iibench) as a tuner benchmark (pilot 2026-10-19)

Mark Callaghan's Insert Benchmark (github.com/mdcallag/mytools `bench/ibench`, results on smalldatum.blogspot.com),
integrated as two benchmark profiles and piloted on `atvbm2` (Vultr bare metal Intel E-2286G 6c/12t, 32 GB,
Percona Server 9.7.2, binlog off). **Correction to the host notes:** the data disk (`/dev/sdb`, `/var/lib/mysql`) is an
Intel D3-S4610 960 GB **SATA SSD** (`INTEL SSDSC2KG96`), not NVMe. The pristine copy `/var/lib/mysql_base` is on `/dev/sda`
(the same SSD model).

Files: `bench/ibench/` - `install.sh` (iibench.py pinned to mytools e1ba3d9, python3-mysqldb, user `ib`), `load.sh`
(l.i0 + l.x + snapshot), `ibench_run.sh` + `ibench_summary.py` (time-limited step + window metrics + InnoDB sampler),
`ibench-li1.ini` (profile A), `ibench-qr100.ini` (profile B), `pilot.sh` (queue: load / measure / tune / cbab).

## How the benchmark works (from the code)

Each client owns one table `piN` (bigint auto-increment PK, datetime, 3 int ids, price, 10-20 byte varchar) and runs
iibench.py, a set of Python processes: a statement maker, an inserter connection, with `--delete_per_insert` a deleter
connection that deletes the same number of the oldest rows (`DELETE ... WHERE transactionid >= min ORDER BY transactionid
LIMIT n`, so each table is a queue of constant size), and optional query connections. Mark's driver (`iq.sh` -> `np.sh`)
runs the steps:

| Step | What it does (InnoDB, Mark's options) |
|---|---|
| l.i0 | `--setup`, no secondary indexes, PK-order inserts, 100 rows/commit, X rows per table |
| l.x | `--secondary_at_end`: one ALTER per table adds 3 secondary indexes: (productid, customerid, price), (cashregisterid, customerid, price), (price, dateandtime, customerid) |
| l.i1 | inserts + deletes as fast as possible, 50 rows/commit; 4/5 of the "nr2" writes (IO-bound: 4M rows/table) |
| l.i2 | the same with 5 rows/commit; 1/5 of the writes (IO-bound: 1M rows/table) |
| (vac) | after l.i2: ANALYZE, `innodb_max_dirty_pages_pct(_lwm)=1` and `idle_flush_pct=100` to flush, sleep 60 s + 1 s per 1M rows |
| qrN / qpN | 1 query connection per client as fast as possible (range: 10 rows from a random point of one of the 3 covering secondary indexes; point: 10 PK lookups), background inserts + deletes rate-limited to N/s per client, N = 100, 500, 1000, each for a fixed time (1800 or 3600 s) |

Common options: `--unique_checks=1 --bulk_load=0 --data_length_min=10 --data_length_max=20`, one client per table. Clients
by server size in recent posts: 1 (8-core small servers), 8 (15-core), 12 (32-core), 20 (48-core).

## Integration choices

- **8 clients x 8 tables x 50M rows** (400M rows): data 3.2 GB + indexes 4.2 GB per table = **59 GB** after l.x (1.9x RAM,
  2.6x the 23 GB buffer pool). 8 clients (Mark's medium-server count) rather than 1: one client per table is the only
  way the load parallelises, and the tuner needs concurrency for flushing to matter. Load: l.i0 18 min (366K rows/s),
  l.x 20 min, total 45 min with the snapshot.
- **Pristine copy = state right after l.x** (clean shutdown; l.i0 leaves no purge work). Every run of both profiles
  starts from it, cold (caches dropped). Profile A is therefore exactly Mark's l.i1 (which also starts right after l.x),
  truncated to warm-up + 5 min; profile B starts from compact, unaged indexes instead of after l.i1/l.i2/vac.
- **Time-limited steps**: `--max_seconds` = warm-up + duration + 5; `--max_rows` effectively unlimited. Metrics come from
  the per-second counters at the window edges, so they cover exactly the agent's measured window.
- **Fixed seeds** per client (Mark uses start time + client id), so every run inserts the same values.
- Latency: iibench reports the per-second maximum statement time; the summary gives its median and p99 over the window
  (`insert_max_ms_p50/p99`) - the response-time measure Mark charts. No mean/percentile latency exists in iibench.
- A sampler (5 s) records history list length, dirty pages, `Innodb_buffer_pool_wait_free`, data reads/writes and iowait;
  the change-buffer section of SHOW ENGINE INNODB STATUS is captured at the window start and end.
- **Profile B uses qr100, not qr500**: l.i1 sustains only ~3,100 inserts/s in total on this disk, so qr500 (8 x 500 =
  4,000/s) cannot hold its write rate. At qr100 (800 + 800/s) the target is met 100.0% in every run.

## Pilot results

### Data load and reset

| | |
|---|---|
| l.i0 (8 x 50M rows, 100 rows/commit) | 1,091 s, 366K rows/s |
| l.x (3 secondary indexes per table) | 1,178 s |
| Size after load | 59 GB of tables+indexes (92 GB datadir incl. redo, undo, doublewrite) |
| Reset (stop, rsync 92 GB from the other SSD, start, cold) | 50 s right after the load; in sessions the reset-to-reset cadence is 11.7 min for a 7.5 min run, so **about 4 min per run** including the agent's pre-restart flush of the previous run's dirty pages (up to 1.4M pages) |

### Profile A (`ibench-li1.ini`, l.i1: 8 clients, inserts+deletes, 50 rows/commit; 2 min warm-up + 5 min)

Metrics parse (inserts/s, deletes/s, insert max-latency p50/p99, history list, dirty %, wait_free, iowait; the goal is `ibench_inserts_per_s`).

| Run | inserts/s (= deletes/s) | insert max-latency p50 / p99 | HLL end | dirty % | iowait |
|---|---|---|---|---|---|
| 1 | 3,160.8 | 366 / 564 ms | 18,728 | 86 | 34 % |
| 2 | 3,030.0 | 369 / 580 ms | 18,548 | 86 | 34 % |
| 3 | 3,043.2 | 367 / 580 ms | 18,246 | 86 | 34 % |

Mean 3,078, **CV 2.3 %**. Server defaults (session #106, run 1): 475 inserts/s, 6x below the baseline configuration.

**Bottleneck: flush-bound on a SATA SSD.** Dirty pages 86 % of the pool, the device 100 % busy (about 6,000 write IOPS, 300 MB/s,
iowait 34 %), `Innodb_buffer_pool_wait_free` = 0 (page cleaners keep free pages available), history list 1K -> 19K (purge keeps
up over 5 min). Index maintenance of the 3 random secondary indexes reads (3,400 reads/s) and writes (10,000 page writes/s) leaf pages.
Per-insert cost is ~100 page writes/s per 1,000 inserts/s: the data set is 2.6x the pool, so almost every secondary index insert
touches an uncached page.

### Is the 7-minute window steady state? No for profile A (a regime change at ~10 min)

A 20-minute run (session 106, run 5-7) measured 15,600-15,980 inserts/s (CV 1.35 %), **5.2x the 7-minute number**. Timeline
of one 20-min run (sum over clients, inserts/s per 60 s): 1.5K, 2.2K, 2.3K, 1.9K, 2.8K, 2.7K, 3.4K, 4.4K, 7.1K, **12.3K at 10 min**,
24K, **30K at 12 min**, 27K, 23K, 20K, 18K, then ~21.7K. The first ~9 minutes are the cold-buffer-pool fill (the pristine copy is restored with dropped caches and the
59 GB set is read in at the rate of the secondary index random reads); after that most index pages are cached
and the rate is limited by flushing (dirty 92 %, checkpoint age 85 % of max, history list growing to 230K by minute 20).
So the 2+5 minute window measures the **cold-cache transient** (flush-bound, 3K inserts/s), the 20-minute window measures
a **warm cache with deferred work accumulating** (also not a true steady state; see below).

`ibench-li1-steady.ini` (12 min warm-up + 5 min, `adaptive_duration = off`) was measured twice: **23,190 and 22,458 inserts/s**
(CV 2.3 %), 25 min per run + 4 min reset. Over its measured window the deferred work is still growing: history list first third 114-122K vs last third 205-211K
(rising, 85-94K -> 224-228K), checkpoint age 81-82 % -> 85-86 % of max, dirty pages flat at 93-94 %, change buffer 61-65K -> 75K pages
(its segment is full: 75K of 113K pages), merges 2,570/s and 41K merged operations/s. 12-27 s per window had inserts below half the mean rate
(stalls: insert-rate CV 27 % within a run). **The result is optimistic: purge (history list) and checkpoint age accumulate.** Mark's own l.i1
runs 4M rows/table (IO-bound) which here would be minutes: his l.i1 does not reach steady state either, it is a fixed-work step; for the tuner
a fixed-duration window is a fixed-time comparison of configurations that all start from the same state, which is still valid for A/B
decisions but absolute numbers depend on window position.

### Profile B (`ibench-qr100.ini`, qr100: range queries as fast as possible, 100 inserts/s + 100 deletes/s per client)

| Run | queries/s | inserts/s (target 800) | insert max-latency p50/p99 | query max-latency p50/p99 |
|---|---|---|---|---|
| 1 | 61,646 | 799.7 (100.0 %) | 117 / 340 ms | 24 / 83 ms |
| 2 | 61,868 | 799.7 | 112 / 295 ms | 22 / 66 ms |
| 3 | 61,691 | 799.7 | 113 / 308 ms | 22 / 71 ms |

Mean 61,735, **CV 0.19 %** (the most stable of all profiles). Bottleneck: **CPU + contention**, not I/O: mysqld at 525 % of 12 vCPU, 8 python clients at 44 % each,
iowait 3 %, wait_free 0, history list 250-450 (purge keeps up), dirty 63 %. Reads 1,570/s are cache misses on the secondary indexes.
The write rate is held: 100.0 % of target in every run. A later run with the extended sampler (after the tuning session, same data and seeds): 60,803 queries/s (-1.5 %), so the practical noise level is about 1.5 %.

Steady state of B: not yet. Over its window (extended sampler): dirty pages 53 % -> 69 % (first -> last third), checkpoint age 22 % -> 42 % of max,
history list flat (243 -> 174), change buffer empty (1 page), so flushing debt accumulates (the window starts right after the cold restore with a clean pool)
but nothing that limits throughput: the queries are CPU-bound and the result is optimistic only in the sense that after 30+ min dirty pages would
reach the limit (a 1800-3600 s window is what Mark runs).

### Tuning session (profile A, session #106, max_runs 8; canary_check: 17 runs, 0 failed checks)

Noise threshold calibrated at 4.07 % (3 x 20-min baseline runs, CV 1.35 %: the agent's adaptive duration lengthened the runs from 7.5 to 22 min, which also moved
the measurement to the warm regime: server defaults 475, baseline 2,970 with 7.5 min, 15,736 with 20 min).

| Run | Change | inserts/s | Verdict |
|---|---|---|---|
| 5-7 | baseline, 20 min | 15,597 / 15,981 / 15,631 | calibration |
| 8 | best-practices (cpu_governor=powersave, noatime off, swappiness 60) | 14,987 (-4.1 %) | regressed |
| 9-10 | + innodb_redo_log_capacity 27 GB ("redo-grow") | 16,388, 16,934 | improved, confirmed |
| 11 | innodb_io_capacity 1000 | 17,769 (+6.7 %) | improved |
| 12 | innodb_io_capacity 4000 | 14,771 (-11 %) | regressed |
| 13 | innodb_io_capacity 8000 | 14,261 (-14 %) | efficiency-loss (see problems) |
| 14 | io_capacity 1000 confirmation | 17,474 | repeat |
| 15-16 | innodb_doublewrite OFF (trade-off: durability) | 29,471, 28,778 (+69 %) | improved |
| 17 | best safe config (redo 27 GB + io_capacity 1000, doublewrite ON) | 17,834 (+13 % vs baseline) | confirmed |

Doublewrite OFF (+69 %) is the largest lever: every dirty page is written twice (5,733 pages/s double-written) on a device that is 100 % busy.
Fewer, slower page flushes (io_capacity 1000, large redo = fewer forced checkpoints) help because aggressive flushing at 4000-8000 IOPS saturates the SATA SSD and
starves the foreground reads of secondary index pages.

### Change buffering A/B (`innodb_change_buffering` all vs none, profile A, 2+5 min windows, interleaved, reset before each run, baseline otherwise; session #118)

| Pair | all | none | none / all |
|---|---|---|---|
| 1 | 3,175.5 | 2,274.1 | 0.716 |
| 2 | 2,813.5 | 2,207.5 | 0.785 |
| 3 | 2,566.2 | 2,120.7 | 0.826 |
| mean (CV) | 2,851.7 (10.7 %) | 2,200.8 (3.5 %) | **0.772 (-22.8 %)** |

Without the change buffer inserts are lower in every pair (-17 to -28 %, mean -23 %). The `all` arm drifts downward over the series (3,176 -> 2,814 -> 2,566), which is the main noise and is why its CV is 10.7 %; the `none` arm is stable. The direction is consistent, the size of the effect is uncertain (0.72-0.83). Mark's 0.48x was on a longer, more I/O-bound run
(his l.i1 IO-bound: 4M rows/table with a data set >> RAM); here the cold window sees 0.77x and the buffer holds back the 3 secondary index
random reads. Change-buffer activity (SHOW ENGINE INNODB STATUS, over the window): with `all`: ibuf size grows 1 -> 1,000-2,800 pages (segment 3.7-4K pages), 1,000-1,190 merges/s; with `none`: 1 page, 0 merges.
History list at the end: all 15.6-20K, none 11.2-12.9K (fewer, more expensive inserts). `none` shows iowait 49-50 % (vs 34-35 %), checkpoint age 54-65 % of max, data reads 3,550-3,680/s vs 3,235-3,500/s, wait_free 0 in both.
In the 20-min steady-state regime the change buffer is at its limit (75K pages of the 113K page segment, 41K merged ops/s), which the A/B at 7 minutes does not reach.
A 20-minute A/B would test the regime where Mark's 0.48x applies; not run.


## Problems found

Benchmark:
- The 2+5 min window measures the cold-cache fill, not a steady state (see above); the 20-min window shows 5-7x higher rates. The tuner's adaptive duration
  moved profile A from 7.5 to 22 min after calibration, so session runs 5+ are not comparable with runs 1-4 (the agent treats them as "superseded", correctly).
- iibench reports only the per-second maximum statement time, no latency percentiles; `insert_max_ms_p50/p99` are percentiles of those maxima.
- 59 GB is 1.9x RAM (a few times RAM would need 100+ GB; l.x alone takes 20 min at this size; 8x100M rows would take ~2 h to load).
- iibench's delete is `ORDER BY transactionid LIMIT n` with a periodic `SELECT MIN(transactionid)`: with a history list of 200K+ in the warm regime each
  min() scans delete-marked rows (Mark's posts describe the same effect).

Tuner:
- `innodb_io_capacity=8000` is silently capped to `innodb_io_capacity_max` (4000): MySQL logs "innodb_io_capacity cannot be set higher than innodb_io_capacity_max. Setting innodb_io_capacity to 4000"
  (error log 20:47). Run 13 (labelled 8000, -14 %) therefore measured 4000 again and is a repeat of run 12 (-11 %), not a separate point; the sweep did not raise
  `innodb_io_capacity_max` together with it. Evidence: session #106 log lines 181-199 and the server error log.
- Session log shows the sweep verdict as "(-14.4%)" while the status table says "efficiency-loss"; the table label hides that the point was invalid.
- The first two best-practice experiments changed os:cpu_governor to powersave and mount_noatime to OFF "to roll back" to run 7: a rollback recommendation whose reason text is about a regressed run; harmless here (-4 % from the OS changes) but the tuner re-recommends powersave on a benchmark host.
