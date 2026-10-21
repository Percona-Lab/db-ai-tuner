# Server variables across variants and LTS series

Collected 2026-10-21 from the official container images, with no option file changes (`tools/varinventory/collect.py`;
merged by `tools/varinventory/build.py` into `agent/tuning_agent/knowledge_data/server_variables.json`).

| Series | Version | Variables | Status counters |
|---|---|---|---|
| MySQL 8.0 / 8.4 / 9.7 | 8.0.46 / 8.4.11 / 9.7.2 | 631 / 623 / 631 | 494 / 497 / 517 |
| Percona Server 8.0 / 8.4 / 9.7 | 8.0.46-37 / 8.4.11-11 / 9.7.2-2 | 693 / 687 / 693 | 543 / 552 / 569 |
| MariaDB 10.11 / 11.4 / 11.8 | 10.11.19 / 11.4.13 / 11.8.9 | 655 / 674 / 684 | 562 / 555 / 571 |
| Percona XtraDB Cluster 8.4 | 8.4.11-11.1 | 744 | 634 |

1,036 distinct variables, 372 of them in every series. 261 exist only in MariaDB, 17 only in PXC (`pxc_*`, some
`wsrep_*`), and 2 only in Percona Server. Plugin variables (MyRocks `rocksdb_*`, Galera outside PXC) appear only with
the plugin loaded and are not in this inventory.

## What is recorded per variable

* the series that have it;
* the compiled-in default, once when equal everywhere, otherwise per series (sizes and thread counts derived from the
  host are kept as `observed`, for reference only);
* whether `SET GLOBAL` accepts it, per series (MySQL / Percona: a no-op `SET GLOBAL v = @@GLOBAL.v`; MariaDB:
  `information_schema.SYSTEM_VARIABLES.READ_ONLY`);
* type, range and allowed values where the server reports them.

## Differences that matter for tuning

Defaults that changed with 8.4 (MySQL, Percona Server and PXC), with 8.0 values in parentheses:
`innodb_io_capacity` 10000 (200), `innodb_io_capacity_max` 20000 (2000), `innodb_log_buffer_size` 64 MiB (16 MiB),
`innodb_flush_method` O_DIRECT (fsync), `innodb_adaptive_hash_index` OFF (ON), `innodb_use_fdatasync` ON (OFF),
`innodb_read_io_threads` 6 (4), `innodb_change_buffering` none (all). In the 9.7 images `innodb_change_buffering` is
`all` again and `innodb_log_writer_threads` defaults to OFF.

MariaDB keeps `innodb_io_capacity` 200, `innodb_flush_neighbors` 1 and `table_open_cache` 2000. It has no
`innodb_redo_log_capacity`, `innodb_page_cleaners` or `innodb_buffer_pool_instances`, and resizes
`innodb_log_file_size`, the I/O threads and the purge threads at runtime, where MySQL and Percona need a restart.
`innodb_log_buffer_size` is the other way round: runtime in MySQL / Percona, restart in MariaDB.

Percona Server's `innodb_numa_interleave` defaults to ON in 8.4 and 9.7 (OFF in 8.0); MySQL builds in these images do not
have the variable.

## How the tuner uses it

* **Applying a change:** the agent decides between `SET GLOBAL` and a restart from the measured flag for the server's
  series; the knowledge base decides for series outside the inventory and for `innodb_doublewrite`, which accepts only
  some transitions online.
* **Parameters page:** a Releases column (where a variable exists, defaults and restart needs that differ) and a release
  filter.
* **Impact priors:** 127 performance-relevant variables without measurements yet carry an impact / safety estimate
  from the manuals (`prior_source: documentation` in the catalog). Measured impact replaces it once enough
  single-setting experiments exist. Variables that are limits or safety caps rather than performance settings
  (`max_allowed_packet`, `connection_memory_limit`) have impact `none`.

Re-collect when a new LTS release appears: `collect.py <outdir> mysql:8.4 percona/percona-server:8.4 ...`, copy the
JSON files to `tools/varinventory/data/`, then run `build.py` and `tools/build_parameter_catalog.py`.
