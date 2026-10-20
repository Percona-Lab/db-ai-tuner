# Memory budget

Memory is one pool shared by every option. The tuner keeps each configuration inside the host's memory budget
(`agent/tuning_agent/memory.py`). A configuration over the budget can swap or be OOM-killed under load: it is a
"memory" trade-off, never chosen as the best production-safe configuration. The `bp-overcommit` experiment is one
on purpose.

## The model

| Part | Counted as |
|---|---|
| Buffer pool | its size, plus ~3% for control blocks, page hash and AHI |
| Log buffer, key buffer | their size |
| In-memory temporary tables | min(`temptable_max_ram`, active connections x min(`tmp_table_size`, `max_heap_table_size`)) |
| performance_schema | ~192 MiB, plus ~128 KiB per allowed connection, when ON |
| Table cache, table definition cache | count x ~8 KiB / ~6 KiB |
| Prepared statements | min(`max_prepared_stmt_count`, 20 per allowed connection) x ~16 KiB; the setting is a cap, not an allocation |
| Connections | `max_connections` x ~768 KiB base (thread stack resident part, session state, network buffers) |
| Per-query buffers | **active** connections x (sort + join + read + read_rnd + binlog cache) |
| mysqld fixed | ~400 MiB (code, data dictionary, background threads) |
| OS | max(1 GiB, 10% of RAM), plus the declared reserve for a co-located client |

**Active = 2 x CPU cores** (at most `max_connections`). Only statements that are running hold their per-query buffers.
The cores bound how many can really be running, so budgeting every connection at every buffer gives absurdly small
settings. Counting each active connection's buffers once is the middle ground: queries that hold several sort or join
buffers at once are covered by the margin of the per-entry estimates and the OS reserve.

## How it is used

- **Baseline:** the buffer pool gets what is left after the OS, a co-located client and everything else at the
  baseline's own settings (capped at 75% of RAM). When an earlier run on the host measured more mysqld memory than
  modelled, the measurement wins.
- **Candidates:** a change to any memory-related setting is checked: if it would push the configuration over the
  budget, it becomes a memory trade-off (benchmark policy only).
- **Runs:** every run records the budget of its configuration and mysqld's measured memory (PSS + swap). Runs over
  the budget are not production-safe.
- **Catalog:** the Parameters page shows each setting's memory cost.

## Sources and caveats

- **Sources for the global, per-thread and per-connection model:**
  - [Percona: MySQL server memory usage](https://www.percona.com/blog/mysql-server-memory-usage/), on global vs
    per-thread buffers, buffers allocated more than once per query, and the worst-case formula's overestimate (simple
    OLTP often uses under 1 MB per connection);
  - [Pythian: MySQL memory consumption](https://www.pythian.com/blog/mysql-memory-consumption);
  - [MySQL 8.4 manual: how MySQL uses memory](https://dev.mysql.com/doc/refman/8.4/en/memory-use.html).
- **TempTable:** global cap `temptable_max_ram` (1 GiB default) and the per-table limit `tmp_table_size`
  ([manual](https://dev.mysql.com/doc/refman/8.4/en/internal-temporary-tables.html)).
- **Prepared statements:** about 2 KiB to tens of KiB each, depending on the statement
  ([lefred](https://lefred.be/content/mysql-memory-consumption-and-open-prepare-statements/)).
  Measured on Battery 1 O3: 1024 connections x 16 tables of sysbench statements took about 10 GB.
- **Caveat:** per-entry costs are estimates. Every run reports mysqld's measured memory next to the model; the
  learner can refine the constants from that data (see `docs/design/code-data-separation.md`).
- **Possible later step:** MySQL 8.0.28+ can enforce connection memory with `global_connection_memory_limit` /
  `connection_memory_limit`
  ([memory accounting](https://www.alibabacloud.com/blog/mysql-memory-allocation-and-management-part-iii_600994)),
  as a production guardrail the tuner could propose.

## Validation (tools/memprobe, tuner-vbm, Percona Server 9.7, 2026-10-20)

Measured mysqld memory (PSS + swap PSS) under controlled load; raw data in
`docs/research/data/memprobe-tuner-vbm-2026-10-20.json`.

| Part | Measured | Model before | Model now |
|---|---|---|---|
| Buffer pool | mysqld = 0.57 GiB + 1.077 x pool (2G: 2.72 GiB, 6G: 7.03 GiB) | +3% | +8% |
| Idle connection, performance_schema off | ~130 KiB | 768 KiB | 160 KiB |
| performance_schema | +242 MiB idle; +250-280 KiB per connection | 192 MiB + 128 KiB | 240 MiB + 256 KiB |
| Sort, 32 connections sorting | ~1.5 MiB per connection at 256K, 8M and 64M sort_buffer_size (allocated as needed) | full size | full size as an upper bound, + 4 MiB execution per active statement |
| Join (hash), 16 connections | 7-9 MiB per connection at 256K and 64M join_buffer_size | full size | as above |
| In-memory temporary table | 20 MiB at 16M, 66 MiB at 64M tmp_table_size (the full size + ~4 MiB) | full size | unchanged |
| Opened table (definition + instance) | ~49 KiB | 8 + 6 KiB | 32 + 17 KiB |
| Prepared statement | ~25 KiB | 16 KiB | 25 KiB |

- **InnoDB makes the whole buffer pool resident at startup:** the idle mysqld at a 1 GiB pool measured 1.61 GiB,
  against 0.57 + 1.077 x 1 GiB from the fitted line.
- **Biggest corrections:** connections were overestimated about 6x (it mattered at 4096 connections), and table
  caches were underestimated about 3.5x.
