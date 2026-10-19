# Finch "orders" workload for the AI DB Tuner (2026-10-19)

Finch (github.com/square/finch, Daniel Nichter, Block/Square) is a declarative MySQL benchmark: SQL files ("trx")
plus YAML stage files that set clients, data generators, run time and statistics. It is meant for modelling a real
application's queries instead of a synthetic mix. In the tuner's benchmark set it gives a small, readable
application-style OLTP workload (point reads, a short range scan, an insert, row updates and one hot counter row) and a
connect-per-request variant that no other benchmark in the set covers.

Files: `bench/finch/`

| File | Purpose |
|---|---|
| `install.sh` | Go 1.24.9, clone square/finch @3f4bb5c, apply the connection-churn patch, build `/usr/local/bin/finch`, MySQL user `finch` |
| `apply_reconnect_patch.py` | adds `FINCH_RECONNECT_EVERY=N` (see below); idempotent |
| `orders/_all.yaml` | connection (127.0.0.1, TLS off), params (data size, clients, runtime), stats (10 s intervals, stdout + CSV with P95/P99) |
| `orders/setup.yaml`, `orders/trx/schema.sql`, `load-*.sql`, `indexes.sql` | data set: 1M customers, 100k products, 2M orders, 8 counters (0.6 GB, 35-60 s) |
| `load.sh` | runs `setup.yaml` |
| `orders/oltp.yaml`, `trx/browse.sql`, `trx/place-order.sql` | mixed OLTP stage |
| `orders/churn.yaml`, `trx/churn-request.sql` | connection-churn stage |
| `run_finch.sh <oltp\|churn> <warmup> <duration>` | runs a stage for warm-up + duration, summarises the CSV over the measured window |
| `finch_summary.py` | CSV -> `FINCH tps=.. qps=.. r_qps=.. w_qps=.. p99_ms=.. r_p99_ms=.. w_p99_ms=.. c_p99_ms=.. errors=..` |
| `finch-oltp.ini`, `finch-churn.ini` | benchmark profiles (`data_state = reset`, `reset_datadir.sh`, 60 s warm-up + 240 s) |
| `pilot.sh` | N repeat runs of each profile, data reset before each |

## Workload

Schema `orders`: `customers` (id, name, email UNIQUE, city, balance), `products` (id, name, price, stock),
`orders` (id auto-inc, customer_id, product_id, qty, amount, status, note, created_at; indexes (customer_id,
created_at) and (created_at)), `counters` (8 rows).

**oltp** (16 clients, prepared statements):
- 12 x `browse`: customer by PK; that customer's 10 latest orders (`ORDER BY created_at DESC LIMIT 10` on the
  secondary index); 3 product point selects; one order by PK. Customers and products are drawn from a normal
  distribution (hot middle), orders uniformly.
- 4 x `place-order`: `BEGIN`; product price (saved with `save-columns`); `INSERT` order; product stock `UPDATE`;
  customer balance `UPDATE`; `UPDATE counters SET val = val + 1 WHERE id = 1..8` (8 hot rows shared by all writers);
  `COMMIT`. Customer, product and quantity are the same within one order (`scope: trx`).
- Goal metric `finch_qps` (all statements); also `finch_tps` (commits/s = orders/s), p99 overall / read / write / commit.

**churn** (16 clients, plain statements, `FINCH_RECONNECT_EVERY=1`): every request opens a new TCP connection,
runs 3 selects (customer, recent orders, product) and a small write transaction (order insert + hot counter),
then disconnects. Goal metric `finch_tps` (= requests/s, one commit each).

### Connection churn needs a patch

Upstream Finch keeps one connection per client for the whole stage and reconnects only after an error, with a fixed
200 ms pause, and the connection goes back to Go's `database/sql` pool rather than being closed. There is no option
for connect-per-request. `apply_reconnect_patch.py` adds 34 lines to `client/client.go`: with
`FINCH_RECONNECT_EVERY=N` each client, every N iterations, closes its prepared statements, discards the connection
(`Conn.Raw` returning `driver.ErrBadConn`, so the pool closes it instead of reusing it) and connects again. Verified:
a 30 s churn run raised `Connections` by 25,741 (858/s, equal to the reported TPS). TLS is off
(`disable-auto-tls: true`), so a connect is TCP + `caching_sha2_password` fast auth + thread start.

### Measurement window

Finch only reports per stage or per `stats.freq` interval, and it resets its histograms every interval.
`run_finch.sh` runs the stage for warm-up + duration with 10 s intervals; `finch_summary.py` averages the rates
over the intervals that start after the warm-up and reports the p99 as the mean of the per-interval P99s (Finch's
histogram buckets are not in the CSV, so an exact window p99 is not available).

## Pilot on atdb3 (2026-10-19)

Host: Linode `atdb3`, 2 vCPU, 4 GB RAM, Percona Server 8.4.11, binary log ON (`sync_binlog = 1`,
`innodb_flush_log_at_trx_commit = 1`), buffer pool 2.3 GB (agent baseline), Finch on the same host.
The data directory also holds the JOB data set (9 GB, untouched by Finch). Pristine copy `/var/lib/mysql_base`
(13 GB) taken after `load.sh`; `reset_datadir.sh` before every run takes 10-15 s (rsync copies only the files that
changed: the orders tablespaces, undo, redo, binlogs).

Three runs per profile, 60 s warm-up + 240 s measured, data reset before each:

| Profile | Run 1 | Run 2 | Run 3 | Mean | CV |
|---|---|---|---|---|---|
| oltp `finch_qps` (goal) | 15,990 | 16,011 | 15,609 | 15,870 | 1.4% |
| oltp `finch_tps` (orders/s) | 369.3 | 368.8 | 350.7 | 362.9 | 2.9% |
| oltp `finch_p99_ms` | 4.99 | 4.88 | 5.40 | 5.09 | 5.4% |
| oltp `finch_commit_p99_ms` | 10.8 | 12.3 | 13.5 | 12.2 | 10.8% |
| churn `finch_tps` (goal, requests/s) | 861.2 | 848.6 | 846.2 | 852.0 | 0.9% |
| churn `finch_p99_ms` | 25.4 | 25.3 | 25.4 | 25.4 | 0.3% |

All metrics parse with the agent's profile parser (`bench.Profile.parse`); no query errors.
oltp mix: 13.8k reads/s, 1.5k writes/s, ~370 commits/s. churn: ~850 new connections/s, 6k statements/s.

## Bottleneck kinds

| Profile | Main limits | Knobs it should respond to |
|---|---|---|
| oltp | CPU (16 clients + Finch on 2 vCPU; reads are buffer-pool hits, the data set fits); commit latency for the 4 writers (`sync_binlog = 1` + redo fsync at every commit: commit p99 11-13 ms); hot-row contention on 8 counter rows | `sync_binlog`, `innodb_flush_log_at_trx_commit`, `binlog_group_commit_sync_delay`, `innodb_log_writer_threads`, `innodb_thread_concurrency`, performance_schema overhead |
| churn | connection setup (thread create/destroy, authentication, session init) and commit-bound writes; 3 of 5 statements per connection are reads | `thread_cache_size` (default 9 for 151 max_connections, far below 16 clients churning), `thread_handling`, `performance_schema` (per-thread instrumentation cost), `back_log`, durability knobs |

Neither profile is I/O-read-bound (0.6 GB of hot data in a 2.3 GB pool) or flush-bound at this rate.

## Problems found

- **Finch has no connect-per-request mode** (patched, see above). Upstream also re-uses a client's prepared
  statements after an error-triggered reconnect even though `*sql.Stmt` from `Conn.PrepareContext` is bound to the
  old connection; the patch drops and re-prepares them on its own reconnects.
- **Data keys are global per stage**: the same key name (`@cust`) in two trx files must have the same `scope`,
  otherwise the stage fails validation ("scope mismatch"). The browse trx uses `@bcust`/`@bprod`.
- **Setup trx without a row limit loops forever** on a duplicate-key error (the counters insert); `iter: 1` fixes it.
- `mysql.db` in a stage cannot be overridden with an empty value; schemas are fully qualified and the run stages use
  `workload.db`.
- At stage end the churn stage logs "N clients did not stop, statistics are not accurate" (clients in the middle of a
  reconnect miss Finch's 10 ms stop window). Only the last partial interval is affected, which the summary does not use.
- Finch only reports per interval; the window p99 is approximated as the mean of interval P99s.

No tuning session was run for Finch in the pilot (the JOB session used the host for 5.5 h). Next step:
`STUDY="Pilot: Finch" SCOPE=knobs,os /root/bench/session.sh /root/bench/finch/finch-oltp.ini "Finch orders OLTP" tps 8`
(and the same for `finch-churn.ini`); each run is ~5.5 min incl. reset.
