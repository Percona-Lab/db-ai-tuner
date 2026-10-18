# Results review: what the tuner has learned so far (2026-10-18)

Scope: 48 sessions, 394 measured runs (39 h of measurement windows), 12 environments (2-vCPU shared Linodes up to a
16-vCPU dedicated Linode and Vultr bare metal), workloads sysbench (1 / 10 / 20 GB, uniform / skewed), go-tpc TPC-C and
TPC-H, CH-benCHmark, HammerDB 10W / 200W, network-bound point selects, and a compatibility matrix
(Percona Server 8.0 / 8.4 / 9.7, MySQL 8.4 / 9.7, MariaDB 10.11 / 11.4 / 11.8 / 12.3 / 13.0).

## 1. Where the gains come from

| Stage | Typical gain | Notes |
|---|---|---|
| Server defaults → generated starting config | **+4% … +1300%** (median ≈ +60%) | Buffer pool sizing dominates (HammerDB 200W on 32 GB: 10.3k → 145k NOPM). Small when data ≫ RAM (sysbench 10 GB on 1 GB RAM: +2.5%). |
| Starting config → best production-safe config | **0 … +12%** typical, outliers +72% (TPC-H) | Median about +4%. Larger on 2-vCPU shared VMs (+8 … +12%) than on 12-16 thread boxes (0 … +2%). |
| Best safe → best with trade-offs | +10% (`innodb_flush_log_at_trx_commit=2`, go-tpc) | Reached in only 3 sessions: the safe phase used the whole budget. |

Takeaway: the starting-configuration generator delivers most of the value; experiments add single-digit percent on
OLTP, large wins only where the optimizer or a specific bottleneck is involved (TPC-H `subquery_to_derived`, +72%).

## 2. Which experiments work

| Experiment | Tried | Improved / neutral / regressed | Median Δ | Verdict |
|---|---|---|---|---|
| `innodb_log_writer_threads=OFF` | 13 | 10 / 3 / 0 | **+8.2%** | Best general rule up to 16 vCPU; now fires up to 16 vCPU (was ≤ 8). |
| `optimizer_switch subquery_to_derived` (OLAP) | 2 | 2 / 0 / 0 | +63% | Workload-specific; keep. |
| Adaptive hash index ON | 21 | 4 / 15 / 2 | +1.3% | Small but consistent on read-heavy work; was lost to the 2% noise floor. |
| Spin loops 10 | 20 | 4 / 15 / 1 | −0.2% | Mostly noise. Keep only on spin-heavy CPU profiles. |
| `innodb_thread_concurrency` sweep | 20 runs | 4 / 12 / 4 | 0.0% | Costly and rarely useful. Needs a stronger trigger. |
| jemalloc / tcmalloc / mimalloc | 21 | 4 / 15 / 2 | ≈ 0 | Up to +2% on bare metal; again below the 2% noise floor. |
| Redo log grow | 13 | 0 / 12 / 1 | +0.3% | Rarely the bottleneck at the tested sizes; −6.7% at 32 GB on bare metal. |
| Thread pool | 9 | 0 / 0 / 8 | **−9.8%** | Consistently harmful on these benchmarks; it was still retried on new machines. |
| Best-practice bundle | 11 | 0 / 11 / 0 | +0.6% | Behaves as intended: kept unless it regresses. |
| Filesystems | 8 | ext4 ≈ best; XFS −3%; ZFS −8 … −18%; btrfs −9 … −20% | | |

The LLM made 8 suggestion calls. One suggestion (`log_writer_threads`) was right but late; one sweep was useless (binlog
group commit with the binlog off).

## 3. Method problems found, and what changed

| Problem | Evidence | Fix (deployed) |
|---|---|---|
| Fixed 2% minimum noise threshold | Bare metal CV 0.2%: AHI +1.2% and jemalloc +0.6% were reverted as noise | Threshold measured per environment + workload from all repeats; drift from time-separated re-checks; prior from earlier sessions; 0.5% floor |
| Re-checks never ran | blocked by pending reverts | Re-check also when only a revert is pending; early re-check while there is no long-term evidence |
| Wrong comparison parent after a combined revert + experiment | "+10%" / "+24%" improvements that changed nothing (session #43 runs 15, 23) | Parent = measured configuration differing only in the experiment's own settings |
| Stale diagnosis after a reverted experiment | buffer pool shrink because btrfs had caused swapping | Diagnose from the configuration in effect |
| Wasted re-measures | storage probe "shifted" because the experiment changed the filesystem | No environment re-measure after storage experiments |
| Winner's curse | many sessions' "best" was a single run a repeat did not reproduce (conf. gain ≈ 0) | Every "improved" run is confirmed immediately by a repeat |
| Small gains thrown away | +0.5 … +1.5% neutral results reverted one by one | `stack-small-wins` combines neutral-but-positive changes and measures them together |
| Trade-offs never reached | 3 of 33 benchmark-policy sessions | Last 30% of the budget is reserved for trade-offs |
| Experience did not transfer | thread pool tried after 8 regressions elsewhere | Fallback to all-workload experience (half weight), consistently harmful changes moved to the end |
| Irrelevant LLM suggestions | binlog settings with the binlog off | Sanitizer rejects them |

Harness quality: performance_schema costs **4.5%** (stock instruments) on HammerDB bare metal; the agent's own sampling is
not measurable (+0.2% vs P_S alone, within ±0.4%). A `ps:profile=lite` mode is being measured on the same box.

## 4. Budget

About a quarter of all runs went to calibration (94 of 394) and another 10% to confirmations and re-measures. With measured
noise priors per environment + benchmark, calibration can drop to 2 runs on known environments.

## 5. Open questions for the test battery (for discussion)

A representative battery should cover the dimensions that changed the answer in this review:

1. **Working set vs RAM**: in-memory, ~RAM, 10x RAM (the starting config matters most here; experiments matter least
   when I/O-bound on small RAM).
2. **Read/write mix and commit rate**: point selects, read-only ranges, read-write OLTP, write-heavy inserts
   (log_writer_threads, redo, flushing rules live here).
3. **Contention**: hot rows (TPC-C district / warehouse) vs uniform keys.
4. **Analytical queries**: TPC-H / CH-benCHmark (optimizer switches, join / sort / temp-table sizing).
5. **Connection behaviour**: pooled vs connection storms; client co-located vs remote, LAN vs WAN.
6. **Machine classes**: 2 vCPU shared, 4-8 vCPU, 16 vCPU dedicated, bare metal Intel / AMD, SSD vs NVMe.
7. **Versions / flavours**: one representative per supported line.

Proposed shape: a short "smoke" battery (5-6 workloads × 1 machine, 2 h each) to validate every engine change, and a
long "scaling" battery (the same workloads on 3-4 machine classes) for releases. The decision we need: which benchmarks
are canonical (sysbench / HammerDB / go-tpc / custom), run lengths, and success criteria (gain over the starting config,
time-to-best, no regressions in production-safe mode).
