# 6-os: most useful findings (47 os: variables)

1. Highest-impact OS knobs are durability/clock ones: os:disk_write_cache, os:mount_barrier (commit-bound, consumer drives without PLP) and os:cpu_governor (bare metal). None of their state is collected: add /sys/block/<dev>/queue/write_cache, per-device flush latency, scaling_governor + scaling_cur_freq, and perf cycles/task-clock (effective MHz) of mysqld.
2. os:malloc (medium at high concurrency): the CPU profile should have an explicit allocator category (malloc/free/arena lock, je_/tc_ frames); add RSS vs performance_schema memory_summary totals for fragmentation.
3. os:hugepages / os:thp: add /proc/meminfo HugePages_* (detect silent fallback to 4K pages), /proc/vmstat thp_* and compact_stall, and perf dTLB page-walk cycles - the only signal that bounds the gain.
4. NUMA (os:mysqld_numa_policy, kernel.numa_balancing, vm.zone_reclaim_mode): add node count, numastat -p mysqld, per-node vmstat and numa_hint_faults/numa_pages_migrated. No effect on single-node VMs - gate these variables on node count > 1.
5. Swap family (vm.swappiness, swapfile, zswap): add pswpin/pswpout rates, mysqld VmSwap and major-fault off-CPU time; with no swap-in the variables cannot matter.
6. Network: one cheap collector covers most net.* knobs - /proc/net/softnet_stat (dropped, time_squeeze per CPU), nstat TcpExt ListenOverflows/ListenDrops/TCPReqQFull*, TcpRetransSegs, per-CPU %soft and /proc/interrupts. Zero drops/squeeze/overflows => skip netdev_*, somaxconn, syn_backlog, rps, net_irq_cpus.
7. tcp_keepalive_*, rmem_max/wmem_max (mysqld never sets SO_*BUF), ip_local_port_range and tcp_tw_reuse (client side) have no measurable performance signal for mysqld on a LAN; treat as no-op/availability settings.
8. vm.dirty_* only affect buffered files (binlog, redo, tmp); add /proc/meminfo Dirty/Writeback series, fsync latency histogram per file, and the writeback:balance_dirty_pages tracepoint for writer throttling.
9. fs.aio-max-nr is binary: check /proc/sys/fs/aio-nr and the error log for io_setup EAGAIN; otherwise skip. read_ahead_kb is a no-op for O_DIRECT data files.
10. os:cpu_affinity / os:net_irq_cpus need per-CPU softirq time plus mysqld run-queue wait (already collected) to bound gains; restricting CPUs costs capacity, so the predicted gain is run-queue wait recovered minus capacity lost.
