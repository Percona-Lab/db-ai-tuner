# Tuning engine test suite v1

A fixed, versioned set of workloads ("cells") for testing the tuning engine as it improves. Each engine milestone runs
the core cells. Results are compared with the previous suite run on the same cells, hardware and protocol, so a change
in the engine shows as a change in the scorecard.

Iterate by adding or removing cells in a new version (v2, ...). Never change a cell in place: a changed cell is a new
cell.

## Dimensions covered

| Dimension | Levels |
|---|---|
| Workload | OLTP read-only, OLTP read-write, write-intensive ingest, key-value / many small queries, analytical, mixed (HTAP) |
| Data : RAM | in memory (≈0.25x), boundary (≈1x), I/O-bound (≈2–3x), heavy I/O (≈10x) |
| Concurrency | 1 stream (analytics), 16, 64, 256, 1024, 4096 connections |
| Topology | single server with a co-located client (most cells); client-server (all high-concurrency cells) |
| Statement shape | many small statements (point selects, TATP, YCSB) vs complex queries (TPC-H, JOB, CH) |
| Goal | throughput for most cells; tail latency for one OLTP cell; total query time for analytical cells |

## Hardware classes

| Class | Server | Client | Used for |
|---|---|---|---|
| S | Linode g6-dedicated-8: 8 vCPU, 16 GB, local SSD | co-located | single-server cells; comparable with Battery 1 and 2 |
| L | Linode g6-dedicated-32: 32 vCPU, 64 GB | g6-dedicated-16 (16 vCPU), same data centre | client-server cells, 1024–4096 connections |
| BM | bare metal (EPYC 4245P NVMe, E-2286G NVMe / SATA) | co-located | low-noise storage reference (extended) |

4096 connections need 32 vCPU or more on the server. The cells raise `ulimit -n`, `max_connections`, `back_log` and
`somaxconn` as benchmark prerequisites (`[requires]`); these are never tuned.

## Cells

Core cells run at every engine milestone. Extended cells run before a release, or when a change targets their area.

### Single server (class S, client co-located)

| ID | Tier | Benchmark | Data : RAM | Conc. | Goal | Exercises |
|---|---|---|---|---|---|---|
| RO-1 | core | sysbench oltp_point_select, 16 x 1M | 0.25x | 64 | tps | CPU path: performance_schema, AHI, allocator, spin |
| RO-2 | core | sysbench oltp_read_only, 32 x 4M | 3x | 64 | tps | read I/O: buffer pool, read-ahead, I/O threads; flushing settings must be validation-only |
| RO-3 | ext | sysbench oltp_read_only, pareto, 64 x 8M | 10x | 16 | tps | heavy random reads, low concurrency |
| RW-1 | core | sysbench oltp_read_write, 16 x 1M, binlog on | 0.25x | 16 | tps | commit path: redo / binlog fsync, group commit |
| RW-2 | core | sysbench oltp_read_write, 32 x 4M | 3x | 64 | tps | flushing plus reads together |
| RW-3 | core | HammerDB TPROC-C 80W | ≈0.5x | 16 | NEW_ORDER p99 | tail latency, CPU-bound OLTP |
| RW-4 | core | HammerDB TPROC-C 800W, all warehouses | ≈5x | 64 | NOPM | I/O-bound OLTP, flush plus read |
| WI-1 | core | Insert Benchmark l.i1, steady state | ≈2x | 8 | inserts/s | flush-bound ingest, secondary indexes, change buffer, purge |
| WI-2 | ext | sysbench oltp_update_index, sync_binlog=1 | 1x | 64 | tps | write-only at the memory boundary |
| KV-1 | core | BenchBase YCSB-A, zipfian | 2x | 64 | tps | skewed key-value reads and updates |
| KV-2 | ext | BenchBase TATP | 0.25x | 64 | tps | many tiny transactions |
| AN-1 | core | TPC-H SF10 (go-tpc), 22 queries | ≈1x | 1 | total time | scans, sorts, joins, read-ahead |
| AN-2 | core | Join Order Benchmark, 113 queries | 0.5x | 1 | total time | optimizer-bound queries |
| AN-3 | ext | TPC-H SF30, 4 streams | ≈3x | 4 | total time | concurrent analytics on I/O |
| MX-1 | core | CH-benCHmark 500W: 32 TPC-C + 1 analytical | ≈3x | 33 | NOPM and Q time | mixed OLTP plus analytics |

### Client-server, high concurrency (class L)

| ID | Tier | Benchmark | Data : RAM | Conc. | Goal | Exercises |
|---|---|---|---|---|---|---|
| HC-1 | core | sysbench oltp_point_select, plain statements | 0.25x | 1024 | tps | contention, thread pool, scheduler |
| HC-2 | core | sysbench oltp_point_select, plain statements | 0.25x | 4096 | tps | connection scaling, thread pool, memory per connection |
| HC-3 | core | sysbench oltp_read_write | 0.5x | 4096 | tps | write contention at extreme concurrency |
| HC-4 | ext | sysbench oltp_read_only | 2x | 1024 | tps | I/O plus contention |
| HC-5 | ext | BenchBase SmallBank | 0.25x | 1024 | tps | many small read-write transactions over the network |
| HC-6 | ext | connection storm: new connection plus 5 point selects | 0.25x | 256 | tps | connect path: DNS, auth, thread creation |
| HC-7 | ext | HammerDB TPROC-C 1000W | ≈1.5x | 512 | NOPM | OLTP at high concurrency over the network |

Before tuning, each HC cell also measures a concurrency ladder at the baseline configuration, without tuning:
64 → 256 → 1024 → 4096, 5 minutes each. The scaling curve shows where the server starts losing throughput, which
explains what the tuning session finds.

## Protocol (every cell)

- **Server:** Percona Server 9.7 (current LTS) unless the cell says otherwise. Binary log on with full durability by
  default (production-like); cells that measure without binlog say so.
- **Data:** writing cells restore a pristine copy before every run (`data_state = reset`); read-only cells keep the data
  (`data_state = read-only`). Data sizes are set relative to the class's RAM.
- **Runs:** steady-state warm-up, checked at calibration and raised automatically. Measured windows are 10–15 minutes,
  or the full query set for analytical cells.
- **Session:** server defaults (reference) → formula baseline → 3 calibration runs → environment check → autotune,
  16 experiments → trade-off phase → final confirmations.
- **Instrumentation:** default performance_schema, CPU and off-CPU profiles, file I/O split, block-latency tails,
  within-run stability, DNS probe.
- **Checks:** `tools/canary_check.py` after every session; a cell with failed checks counts as an engine failure.

## Scorecard (per cell and suite total)

| Metric | What it tells about the engine |
|---|---|
| Gain vs formula baseline: safe / with trade-offs | how much tuning finds |
| Gain vs server defaults | context: how much the baseline alone gives |
| Runs to reach 90% of the session's best | search efficiency |
| Experiments with evidence vs validation-only, share of measured time they targeted | evidence discipline |
| Hypothesis outcomes: supported / moved / refuted / improved by another mechanism | how well the engine understands the bottleneck |
| Wasted runs: neutral and reverted, failed applies, rejected sweep points | cost |
| Calibrated noise, environment-check result, within-run stalls, storage drift | measurement quality: is the cell trustworthy |
| Checker failures, tracebacks, aborted runs | engine robustness |
| Repeatability: 3 core cells (RO-1, RW-4, HC-2) run twice per suite version | is the result the engine's or luck |

A suite run is compared cell by cell with the previous run. A gain or loss counts only beyond the cell's noise. The
web app shows a cell's history across engine builds as a study per suite version.

## Cost and time

- **Core cells:** 13 single-server and 3 client-server cells, about 8 hours each, all run in parallel.
- **Wall clock:** about 10 hours for the core suite.
- **Core suite:** about 13 x 8 h x $0.216 (class S) plus 3 x 8 h x $1.30 (class L pair), roughly $55 per suite run.
- **Extended cells:** add roughly $50.
- **VMs:** created per suite run and deleted when their cells finish, as in Battery 1 and 2.

## Open items for v1

- Implement the runner (`bench/suite/run.sh <version> <cells...>`): it provisions per hardware class, sets up each
  cell from a cell definition (profile plus data build), runs the session, collects the scorecard, and deletes the VMs.
- Build the cells that are new: RO-3, RW-2 at 3x, WI-2, AN-3, HC-2/3 (4096 connections), HC-5, and the ladder.
- Scorecard page in the web app, comparing suite runs.
