# CPU profiling in the tuner

Goal: for CPU-bound workloads, show where the database spends CPU and turn that into tuning hypotheses, without the
measurement itself changing the results the tuner compares.

## How others do it

| Tool | Mechanism | Stack unwinding | Notes |
|---|---|---|---|
| [Coroot node agent](https://docs.coroot.com/profiling/ebpf-based-profiling) | eBPF program on perf-event sampling, always on for every process on the node; uses Pyroscope's eBPF profiler | frame pointers; perf maps for JIT runtimes (JVM `-XX:+PreserveFramePointer`, Node.js `--perf-basic-prof`) | CPU profiles only; per-app opt-out with `COROOT_EBPF_PROFILING=disabled`; profiles stored in ClickHouse, shown as flame graphs |
| OpenTelemetry eBPF profiler (ex Elastic Universal Profiling) | eBPF on perf events, ~20-100 Hz | unwinds in the kernel from `.eh_frame` data, so it works without frame pointers | heavier agent; good fallback for binaries built without frame pointers |
| Parca agent, Pyroscope eBPF | eBPF on perf events | frame pointers, Parca also DWARF-based | continuous-profiling backends |
| `perf record` | perf-event sampling, user-space buffer | frame pointers (`-g`), DWARF (`--call-graph dwarf`, expensive: copies stack pages per sample), LBR (bare metal only) | no daemon, available on every distribution |

All of them sample with the kernel's perf_event subsystem; the difference is where stacks are unwound and aggregated.
eBPF profilers aggregate in the kernel (no per-sample copy to user space), which matters for always-on profiling at
100 Hz on many processes; for a short, occasional slice the per-sample cost of `perf record` is just as small.

## What we do

* `tuning_agent/cpuprof.py`: `perf record -F 49 -a -g` for 20 s (system-wide, one sampling event per CPU, so the cost
  does not grow with the number of threads); falls back to the `cpu-clock` software event on VMs without a PMU.
* Percona Server / Ubuntu MySQL builds keep frame pointers (verified on PS 9.7.2: full stacks from `do_command` down to
  `log_write_up_to`), so `-g` is cheap. Without frame pointers the profile is leaf-only and the category breakdown still
  works.
* **Timing: the profile covers the last 20 s of the benchmark warm-up, not the measured window**, so every measured run
  is unaffected regardless of whether it was profiled. Symbolisation (`perf script`) runs after the measurement, at nice 19.
* Throughput during the profiling slice is compared with the measured window and reported only when the difference
  exceeds the 10 s sample noise; beyond 2% the next profiles use half the frequency.
* `cpu_profiling = auto` (default): once per configuration, only when the previous run was CPU-bound (>= 60% busy).
  `always` / `off` in agent.ini. Ad hoc: `tuning-agent profile --seconds 20 [--folded out.txt]`.

## From profile to tuning

Samples are classified from the innermost frame outwards into categories that map to mechanisms the tuner already
experiments with:

| Category (threshold) | Diagnosis | Experiments that become more relevant |
|---|---|---|
| spin / mutex wait (10%) | cpu-spin | innodb_sync_spin_loops / spin_wait_delay, thread pool |
| memory allocator (8%) | cpu-allocator | jemalloc / tcmalloc (os:malloc) |
| performance_schema (6%) | cpu-pfs | instrumentation reductions (observability trade-off) |
| scheduler / futex (12%) | cpu-futex | thread pool, innodb_thread_concurrency |
| memory management, kernel (6%) | cpu-mm | THP off |
| parser / optimizer (15%) | cpu-parse | advice: prepared statements / query changes (outside knob scope) |
| network / protocol (20%) | cpu-network | network rules (RPS, IRQ affinity) |

The run page shows the category bar, a zoomable flame graph and top functions (self and inclusive).

## Possible next steps

* Off-CPU profiling (where threads block) with eBPF `sched_switch` sampling - complements performance_schema waits.
* OpenTelemetry eBPF profiler as an optional backend for binaries without frame pointers (MariaDB / MySQL builds from
  other vendors).
* Profile diff between parent and child configuration (which functions got cheaper) to explain efficiency verdicts.
