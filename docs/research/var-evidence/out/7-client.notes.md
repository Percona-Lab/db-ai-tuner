# 7-client: most useful findings

1. The universal bound for every client setting is the DB-side eBPF off-CPU share "Waiting: client (next statement)" minus the wire part (client rtt_min_ms per round trip); if that share is small, no client setting can move throughput.
2. Not collected yet: a per-run split of that client wait into network RTT vs client-side processing (wait per statement vs client_rtt_min_ms); add it to the summary - it is the gate for all client experiments.
3. 11 of 47 entries cannot matter on a client host: cpu_affinity, datadir_fs, disk_write_cache, hugepages, malloc, mount_barrier, mysqld_numa_policy (no mysqld/data dir; the client role rejects most), plus aio-max-nr, somaxconn, tcp_max_syn_backlog, scheduler/read_ahead (no listeners, no measured disk path). Do not propose them.
4. rps and net_irq_cpus are the most likely real wins (VMs with few RX queues at high statement rates); signals (client softirq max/avg) are collected and summarised.
5. client net_softnet_squeeze and net_softnet_drops are in the client window but NOT in the summary keys; add them (they gate netdev_budget/_usecs and netdev_max_backlog).
6. cpu_governor on bare-metal clients can be medium impact; client cpu_mhz is in the window but not summarised - add it (plus max frequency from facts).
7. ip_local_port_range / tcp_tw_reuse / nf_conntrack_max are functional (failures, not speed): connect EADDRNOTAVAIL and conntrack "table full" drops are not collected (need the benchmark's error log and `conntrack -S`); conntrack_fill_pct exists in the window but is not summarised.
8. WAN-only levers (tcp_congestion_control+fq, slow_start_after_idle, tcp_rmem/wmem, rmem/wmem_max) are nil on LAN OLTP; per-socket window/buffer data (ss -tmi) is not collected, so buffer limits can only be inferred from BDP vs per-connection MB/s.
9. Client memory/dirty/THP/swap settings matter only if the client is under memory or I/O pressure; add client psi_memory_some/psi_io_some and oom_kills to the summary (today only psi_cpu_some is) to rule them out cheaply.
10. Keepalive settings have no performance signal; only validate them against idle-connection resets (server Aborted_clients, client net_resets_per_s).
