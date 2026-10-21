# NUMA hosts

Multi-socket servers, and single sockets split into nodes (AMD EPYC NPS2 / NPS4, Intel sub-NUMA clustering), give each
group of CPUs its own local memory. Local memory is faster than remote memory, and each node fills up on its own. The
tuner treats NUMA as a dimension of its own: it is detected, measured in every run, and it changes both OS settings
and MySQL settings.

## What the agent detects

| Fact | Source |
|---|---|
| Nodes, CPUs and memory per node, distances | `/sys/devices/system/node/node*/{cpulist,meminfo,distance}` |
| Kernel policy: automatic NUMA balancing, zone reclaim | `kernel.numa_balancing`, `vm.zone_reclaim_mode` |
| Where mysqld's memory lives, and under which policy | `/proc/<pid>/numa_maps` (MiB per node, default / interleave / bind) |

## What every run measures (multi-node hosts)

| Summary field | Meaning |
|---|---|
| `numa_other_node_pct` | share of allocations served by another node than the requesting one |
| `numa_miss_per_s` | allocations that wanted a node but got another (the wanted node was full) |
| `numa_pages_migrated_per_s`, `numa_hint_faults_per_s` | automatic balancing at work (sampling faults, page copies) |
| `numa_node_free_min_mb` / `_max_mb` | the tightest and the freest node at the end of the window |
| `numa_mysqld_max_node_pct` | share of mysqld's memory on its busiest node |

A CPU profile cannot see remote memory access; these counters are the evidence. A run whose memory is concentrated
on one node, a node out of memory while another has plenty, a buffer pool larger than one node, or remote allocations
produce a **NUMA diagnosis** that the evidence gate and the experiment ranking use.

## What it changes

OS (with OS tuning allowed):

* `vm.zone_reclaim_mode = 0` (best practice): a full node allocates from another node instead of dropping cache or
  swapping while memory is free elsewhere.
* `kernel.numa_balancing = 0` (best practice): no page sampling / migration under a database's access pattern; its
  measured activity raises the priority.
* `os:mysqld_numa_policy` (systemd `NUMAPolicy=` / `NUMAMask=` for the mysqld service): `interleave` spreads all of
  mysqld's memory over the nodes (`numa-interleave-all`); `bind:N` keeps it on one node.
* `numa-single-node` experiment: mysqld's CPUs and memory on node 0 when the server needs fewer than 70% of the node's
  CPUs and memory - every access local. Kept only if the goal improves.

MySQL:

* `innodb_numa_interleave = ON`: in the baseline when the buffer pool is larger than one node (it cannot be local
  anyway); as an experiment (`numa-interleave`) when a run shows imbalance.
* Partitioned structures as multiples of the node count: `innodb_buffer_pool_instances` (baseline and experiments),
  `innodb_page_cleaners`, `table_open_cache_instances`.
* Latch contention: on multi-node hosts measured spinning ranks the changes acting on it higher (a contended latch
  moves its cache line across the interconnect at every handover).
* Huge pages and the memory budget apply per host as before; `bind:N` is proposed only while mysqld fits the node.

Learning: insights carry a `numa` condition (`uniform` / `multi-node`) and are served only to hosts of the same kind.

## Checklist for a NUMA host

1. `numactl --hardware` / `lscpu`: how many nodes, CPUs and memory per node (BIOS NPS / SNC settings change this).
2. `vm.zone_reclaim_mode = 0`.
3. Automatic NUMA balancing off for a dedicated database host.
4. Buffer pool larger than a node: `innodb_numa_interleave = ON` (or the whole process interleaved).
5. A server that fits a node: try binding it to one node - measure, do not assume.
6. Compare `numa_maps` placement before and after: the point is even placement or full locality, not either by default.
