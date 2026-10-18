# Linux OS + Network Tuning for MySQL-family Databases and their Clients

*Research survey, October 2026. Scope: MySQL 8.0/8.4/9.x, Percona Server, MariaDB 10.11/11.x on Linux (kernel 5.14 RHEL9 through 6.x/7.0), bare metal and KVM/virtio cloud VMs (Linode/Akamai, AWS, GCP, Azure). Covers the database server host AND client/application/load-generator hosts.*

Prepared for the AI DB Tuner (`/home/claude/aituner`). Each recommendation carries: **side** (S = server, C = client, B = both), **risk class** (none / availability / durability / security / semantics), **apply mode** (runtime / restart mysqld / remount / reboot), **VM applicability**, and **evidence grade**:

- **A** = vendor doc + independent measured benchmark on a DB workload
- **B** = measured on a DB workload by one credible source, or vendor doc with clear mechanism
- **C** = mechanism is sound, measured only on generic network/web workloads; transferable
- **D** = folklore / weak / anecdotal / obsolete

Defaults below were verified on a live Ubuntu kernel 7.0.0 KVM guest (2 vCPU, 4 GB, kvm-clock) in October 2026 unless stated otherwise; distro kernels differ (RHEL 9 = 5.14, Ubuntu 24.04 = 6.8, Debian 13 = 6.12).

<!-- EXEC_SUMMARY_PLACEHOLDER -->

---

## 1. TCP/IP stack

**Headline:** For a MySQL server on a LAN, almost all TCP defaults on a modern kernel are already fine. The settings that matter in practice are (a) accept-queue sizing for connection storms (`somaxconn` + MySQL `back_log`), (b) ephemeral ports / TIME_WAIT on *clients* that open short-lived connections, (c) conntrack on either side when a stateful firewall is loaded, and (d) buffer sizes + congestion control only when RTT is large (cross-region replication, WAN clients). Sources: kernel `ip-sysctl.rst` <https://docs.kernel.org/networking/ip-sysctl.html>, `admin-guide/sysctl/net.rst` <https://docs.kernel.org/admin-guide/sysctl/net.html>.

### 1.1 Connection establishment / accept path

| Parameter | Default (kernel 7.0) | Recommended | When it helps (signal) | Side | Effect size / evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| `net.core.somaxconn` | 4096 (since 5.4; was 128 before, so RHEL 8 / 4.18 = 128) | 4096-65535 | `TcpExt.ListenOverflows` / `ListenDrops` increasing; `ss -lnt` Recv-Q at Send-Q for :3306; clients see 1 s/3 s connect stalls (SYN retransmit) during reconnect storms or failover | S (and any proxy: ProxySQL/MaxScale/HAProxy) | Eliminates connect stalls of 1 s+ during storms; no steady-state throughput change. Grade A (kernel doc, Percona, MySQL docs). tuned `throughput-performance` sets `>2048` <https://github.com/redhat-performance/tuned/blob/master/profiles/throughput-performance/tuned.conf> | none (memory only when used) | yes | runtime; listener must re-`listen()` -> **restart mysqld** (effective backlog = min(back_log, somaxconn)) |
| MySQL `back_log` | MySQL 8.0+: `-1` -> autosized = `max_connections` (WL#9704 <https://dev.mysql.com/worklog/task/?id=9704>); MySQL 5.6/5.7: 50 + max_connections/5, cap 900; MariaDB: min(150, `max_connections`) <https://mariadb.com/docs/server/ref/mdb/system-variables/back_log> | >= expected burst of simultaneous connects; must also raise `somaxconn` | same as above | S | MySQL doc: "the value is capped by somaxconn" <https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_back_log> | none | yes | restart |
| `net.ipv4.tcp_max_syn_backlog` | 128-1024+, RAM-scaled (256 on a 4 GB VM) | 4096-65535 | `TcpExt.TCPReqQFullDrop`, `TCPReqQFullDoCookies`, "possible SYN flooding" in dmesg | S | Grade B. Only matters with SYN bursts > backlog; with `tcp_syncookies=1` connections still succeed but lose TCP options (wscale/SACK) | none | yes | runtime |
| `net.ipv4.tcp_syncookies` | 1 | 1 (keep) | — | S | Safety net; if cookies fire on a DB host, raise backlogs instead | security if disabled | yes | runtime |
| `net.ipv4.tcp_abort_on_overflow` | 0 | 0 (keep; set 1 only to make overload fast-fail visible) | — | S | 1 converts silent drop to RST -> clients error instead of retrying | availability | yes | runtime |
| `net.ipv4.tcp_fastopen` | 1 (client only) | 1 (leave). tuned `network-latency` sets 3 | Many short connections across a WAN | B | **Not useful for MySQL**: the MySQL protocol is server-speaks-first (handshake packet), and no mainstream MySQL client sends data in SYN, so TFO saves nothing. Grade D for MySQL. | none | yes | runtime |
| `net.ipv4.ip_local_port_range` | `32768 60999` (28,232 ports) | `1024 65535` (or `10000 65535`) on **clients/proxies**; reserve service ports with `ip_local_reserved_ports` (e.g. 3306,4567,33060,33061,6032,6033) | `connect()` returns `EADDRNOTAVAIL` ("Cannot assign requested address"); `ss -tan state time-wait | wc -l` near 28 k per dst ip:port; load generators opening a connection per transaction | C (and proxies / ProxySQL backend side) | Max new-conn rate to one `dst ip:port` ≈ ports / TIME_WAIT(60 s) ≈ 470/s with default range -> ~1,070/s with full range. Cloudflare 2022 <https://blog.cloudflare.com/how-to-stop-running-out-of-ephemeral-ports-and-start-to-love-long-lived-connections/>. Grade A | none (ensure no listener inside the range) | yes | runtime |
| `net.ipv4.tcp_tw_reuse` | 2 (loopback only; since 4.20, commit 79e9fed46038; previously 0) | 1 on clients/proxies that make many outbound short connections | Same as above; TIME_WAIT count high on the *active closer* | C | Lets `connect()` reuse a TIME_WAIT 4-tuple after `tcp_tw_reuse_delay` (1000 ms default; this sysctl exists only on recent 2025+ mainline kernels, older kernels hard-code 1 s). Requires `tcp_timestamps=1`. Grade A (kernel doc; widely used). Kernel doc still says "should not be changed without advice of experts". | semantics (minor; PAWS-protected) | yes | runtime |
| `net.ipv4.tcp_tw_recycle` | **removed in 4.12** | do not use | — | — | **Obsolete folklore.** Broke clients behind NAT; removed in 2017 (commit 4396e46187ca). Any guide recommending it is pre-2017. Grade D | — | — | — |
| `net.ipv4.tcp_fin_timeout` | 60 | 15-30 (optional) | Many sockets in FIN_WAIT_2 orphaned (`ss -tan state fin-wait-2`) | B | **Does NOT shorten TIME_WAIT** (hard-coded 60 s `TCP_TIMEWAIT_LEN`); only orphaned FIN_WAIT_2. Common folklore confuses the two. Grade C/D | none | yes | runtime |
| `net.ipv4.tcp_max_tw_buckets` | RAM-scaled (16384 on 4 GB; 262144 on big hosts) | raise on busy clients; never lower | dmesg "TCP: time wait bucket table overflow" | C | Kernel doc: "you must not lower the limit artificially". | semantics if lowered | yes | runtime |
| Socket option `IP_BIND_ADDRESS_NO_PORT` / `IP_LOCAL_PORT_RANGE` (6.3+) | off | set in connection-pool/proxy code that `bind()`s a source IP before `connect()` | multi-IP egress port exhaustion | C | Cloudflare (link above). App-level change. | none | yes | app |

**Better fix than sysctls for ephemeral port exhaustion:** stop opening a connection per request (pooling; see §7). TIME_WAIT lands on whichever side closes first; MySQL clients normally send `COM_QUIT` and close first, so TIME_WAIT accumulates on **clients**, not on mysqld.

### 1.2 Keepalive / dead-peer detection

| Parameter | Default | Recommended | When | Side | Evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| `net.ipv4.tcp_keepalive_time` | 7200 s | 60-300 s | Idle pooled connections silently dropped by NAT gateways / cloud LBs / firewalls (AWS NLB idle timeout 350 s, GCP firewall conntrack 10 min, Azure LB 4 min default); clients hang on half-open sockets after a failover | B (client most important) | Grade A for symptom fix. MySQL server enables `SO_KEEPALIVE` on client sockets; Connector/J `tcpKeepAlive=true` default; MySQL Connector/Python, Go driver (net.Dialer KeepAlive 15 s default since Go 1.13) | none (slightly more packets) | yes | runtime (new sockets) |
| `net.ipv4.tcp_keepalive_intvl` | 75 s | 10-30 s | same | B | | none | yes | runtime |
| `net.ipv4.tcp_keepalive_probes` | 9 | 3-6 | same | B | | none | yes | runtime |
| `net.ipv4.tcp_retries2` | 15 (~924 s with RTO backoff) | 5-8 on clients for faster failover detection of a dead primary when traffic is in flight | Client hangs ~15 min after primary host dies with data in flight | C | Grade B (Patroni/PG and MySQL HA community practice; `TCP_USER_TIMEOUT` per-socket is the better fix) | availability (false positives on lossy links) | yes | runtime |
| `TCP_USER_TIMEOUT` socket option | 0 | e.g. 30 s in driver | as above | C | Go MySQL driver lacks it natively; can be set via custom dialer | availability | yes | app |

MySQL server-side counterparts: `wait_timeout` / `interactive_timeout` (default 28800 s) and `net_read_timeout`/`net_write_timeout` (30/60 s).

### 1.3 Buffers, window, congestion control (matter mainly when RTT is large)

Bandwidth-delay product (BDP) rule: needed window ≈ bandwidth × RTT. 1 Gbit/s × 1 ms (same-AZ) = 125 KB — defaults are plenty. 1 Gbit/s × 70 ms (cross-region) = 8.75 MB, which **exceeds** the old `tcp_wmem` max (4 MB) and the old `tcp_rmem` max (6 MB). Relevant for: cross-region async replication (binlog/IO thread), Galera WAN, mysqldump/clone/xtrabackup streaming over WAN, large result sets to remote apps.

| Parameter | Default | Recommended | When | Side | Evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| `net.ipv4.tcp_rmem` | `4096 131072 6291456` (≤6.x older) ; newer mainline (2025+) max up to 32 MB RAM-scaled (`4096 131072 30668352` observed on 7.0) | max 16-64 MB when RTT > 10 ms and bandwidth ≥ 1 Gbit/s | `ss -ti` shows `rcv_space`/`rcv_ssthresh` pinned at max, `TcpExt.TCPRcvQDrop`, replica IO thread / clone throughput ≈ rmem_max/RTT | B (receiver side matters: replica, client receiving results) | Dumazet commit raising default max to 32 MB: single flow 73.6 -> 122.5 Gbit/s on 200G NIC; Google ran 15 MB for 8 years. Cloudflare 2022 on tcp_collapse trade-off <https://blog.cloudflare.com/optimizing-tcp-for-high-throughput-and-low-latency/>. Grade A (generic), C for MySQL specifically | none/minor memory; very large values can cause `tcp_collapse` latency spikes (Cloudflare) | yes | runtime (new sockets) |
| `net.ipv4.tcp_wmem` | `4096 16384 4194304` | max 16-64 MB for high-BDP senders (source of replication, server sending big result sets cross-region) | `ss -ti` shows `notsent` large, cwnd-limited vs `sndbuf_limited` (`ss -tin` "sndbuf_limited" in `tcpi_sndbuf_limited`) | B (sender side: primary) | tuned `network-throughput` sets `4096 16384 16777216` | none/minor memory | yes | runtime |
| `net.core.rmem_max` / `wmem_max` | 212992 on older kernels; **4 MB on recent mainline** (Dumazet, "set net.core.rmem_max and net.core.wmem_max to 4 MB") | 16 MB+ only if an app uses `SO_RCVBUF/SO_SNDBUF` (MySQL doesn't; it relies on autotuning) | — | B | Setting `SO_RCVBUF` disables autotuning; most MySQL tooling doesn't, so `tcp_rmem`/`tcp_wmem` are the knobs that matter. Grade B | none | yes | runtime |
| `net.core.rmem_default` / `wmem_default` | 212992 | leave | — | — | Ignored by TCP (TCP uses `tcp_rmem[1]`) | — | — | — |
| `net.ipv4.tcp_mem` | auto (pages, RAM-scaled) | leave unless `TcpExt.TCPMemoryPressures` > 0 | `/proc/net/sockstat` "TCP: mem" near 2nd value | B | | availability if too high (OOM) | yes | runtime |
| `net.ipv4.tcp_window_scaling` | 1 | 1 (never disable) | — | B | Disabling caps window at 64 KB: catastrophic on WAN | — | — | — |
| `net.ipv4.tcp_timestamps` | 1 | 1 | — | B | Required for `tcp_tw_reuse`, PAWS, RTT measurement. Disabling for "security/CPU" is folklore (Grade D). | — | — | — |
| `net.ipv4.tcp_sack` | 1 | 1 | — | B | Disabling as SACK-panic (CVE-2019-11477) workaround is obsolete after patch | — | — | — |
| `net.ipv4.tcp_slow_start_after_idle` | 1 | **0** for long-lived pooled connections over WAN / high-BDP links | Bursty request/response on idle-then-burst pooled connections; replication after idle | B | After idle > RTO, cwnd resets to IW10 -> first large response after idle takes extra RTTs. tuned `network-throughput` sets 0. Grade B (generic; widely used by CDNs/Netflix). On same-AZ LAN (RTT < 1 ms) effect is negligible. | none (slightly less polite to network) | yes | runtime |
| `net.ipv4.tcp_notsent_lowat` | UINT_MAX | 16384-131072 (tuned `network-throughput`: 131072) | Senders using epoll with large writes (proxies) that want lower memory and latency; Cloudflare/Google use 16 KB for HTTP/2 prioritisation | B (proxies mainly) | Grade C for MySQL: mysqld writes blocking per-thread, gain unproven | none | yes | runtime |
| `net.ipv4.tcp_congestion_control` | cubic | **bbr** for WAN / lossy / cross-region paths (replication, remote clients, backups); cubic fine on LAN | `ss -ti` shows retransmits + throughput collapse on long-RTT paths; shallow-buffer cloud networks | B (sender matters) | Google BBR paper: up to 2700x throughput vs cubic at 1% loss; YouTube 4% median throughput gain (Cardwell et al., ACM Queue 2016 <https://queue.acm.org/detail.cfm?id=3022184>). Spotify, Dropbox, Cloudflare reports. BBRv3 (2023) not in mainline as of 2026 (out-of-tree). On a LAN with sub-ms RTT: no measurable DB gain. Grade A (generic), C (MySQL) | none (BBRv1 can be unfair to cubic flows) | yes (needs `tcp_bbr` module) | runtime (new connections) |
| `net.core.default_qdisc` | `fq_codel` (most distros), `pfifo_fast` kernel default | `fq` when using BBR on kernels < 4.20 (BBR needs pacing; since 4.13 TCP has internal pacing fallback, so fq is preferred but not required) | — | B | Grade B. On virtio-net with mq root, leaves use this. | none | yes | runtime for new devices; `tc qdisc replace dev eth0 root fq` for existing |
| `net.ipv4.tcp_autocorking` | 1 | 1 (leave) | — | B | Coalesces small writes only when a previous packet is still queued in qdisc/NIC; does not add Nagle-style delay. Disabling sometimes suggested for latency; no DB evidence. Grade D | — | — | — |
| `net.ipv4.tcp_shrink_window` | 0 | leave | — | — | | — | — | — |
| `net.ipv4.tcp_rto_min_us` (6.x) / `ip route ... rto_min` | 200000 µs | 5-50 ms inside a datacenter (Google uses ~5 ms internally) | Tail latency from 200 ms RTO on lossy LAN: p99.9 spikes at ≥200 ms, `TcpExt.TCPTimeouts` rising | B | Grade C: well-known datacenter practice (Google, Meta) but not DB-specific; per-route via `ip route change ... rto_min 10ms` works on older kernels | availability (spurious retransmits) | yes | runtime |
| `net.ipv4.tcp_mtu_probing` | 0 | 1 on paths with PMTU black holes (VPN/overlay) | Connections hang on large result sets but small queries work | B | Grade B (AWS/GCP docs for MTU 1460/8896 paths) | none | yes | runtime |

**TCP_NODELAY.** The MySQL server sets `TCP_NODELAY` on every client socket (vio layer, `vio_fastsend()`), Connector/J has `tcpNoDelay=true` by default (<https://dev.mysql.com/doc/connector-j/en/connector-j-connp-props-networking.html>), libmysqlclient/Connector/C sets it, Go's `net` package sets it by default on all TCP conns, Node `mysql2`/`mysqljs` set it (mysqljs PR #2560). Nagle + delayed-ACK (40 ms) interaction is therefore rare with MySQL; check custom proxies/drivers. Grade A.

### 1.4 Busy polling

| Parameter | Default | Recommended | When | Side | Evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| `net.core.busy_read` | 0 | 50 (µs) per kernel doc / tuned `network-latency` | Ultra-low-latency point selects where network RTT dominates (< 50 µs targets), dedicated cores | B | Needs NAPI on the NIC (works on virtio-net since 5.x with napi_tx; real benefit mostly bare-metal mlx5/ice). Burns CPU; with mysqld's thread-per-connection (hundreds of threads) busy polling steals CPU from query execution. Grade C/D for MySQL | none (CPU/power cost) | partial | runtime |
| `net.core.busy_poll` | 0 | 50 | same (poll/select/epoll) | B | Applies to epoll-based proxies (ProxySQL, MaxScale) more than mysqld | none | partial | runtime |
| `SO_PREFER_BUSY_POLL`, `napi_defer_hard_irqs` + `gro_flush_timeout` (5.11+) | 0 | e.g. `napi_defer_hard_irqs=2`, `gro_flush_timeout=200000` | Very high pps on proxies; reduces IRQ rate | S/proxy | Grade C (Meta/Google talks, Netdev 0x14). Per-device sysfs `/sys/class/net/<dev>/napi_defer_hard_irqs` | latency trade-off | partial | runtime |

### 1.5 Conntrack and firewall

| Parameter | Default | Recommended | When | Side | Evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| `net.netfilter.nf_conntrack_max` | RAM-scaled (often 65536-262144) | ≥ 4 × peak connections (incl. TIME_WAIT); or bypass for DB port | dmesg "nf_conntrack: table full, dropping packet"; `conntrack -S` `drop`/`insert_failed` > 0; random connect timeouts | B | Very common outage cause on client/app hosts with Docker/Kubernetes (kube-proxy iptables) and on DB hosts running firewalld/ufw. Each entry ≈ 300-350 bytes. Alibaba Cloud KB <https://www.alibabacloud.com/help/en/ecs/user-guide/the-application-on-the-ecs-instance-occasionally-suffers-packet-loss-and-the-kernel-log-contains-the-error-message-kernel-nf-conntrack-table-full-dropping-packet>. Grade A | availability if undersized | yes | runtime (module loaded) |
| `nf_conntrack_buckets` (`/sys/module/nf_conntrack/parameters/hashsize`) | conntrack_max/4 (kernel ≥ 5.x sets max = buckets on some) | conntrack_max / 4..1 | same; hash chain length | B | | none | yes | runtime via sysfs |
| `nf_conntrack_tcp_timeout_time_wait` | 120 | 30-60 | conntrack full of TIME_WAIT from short-lived DB connects | B | Grade B | none | yes | runtime |
| `nf_conntrack_tcp_timeout_established` | 432000 (5 days) | 3600-86400 | conntrack full of dead established entries | B | | availability (entries for idle pooled conns dropped -> if NAT, conn breaks; set > `tcp_keepalive_time`) | yes | runtime |
| NOTRACK / `ct notrack` for DB port | not set | nftables: `table inet raw { chain pre { type filter hook prerouting priority raw; tcp dport 3306 notrack } chain out { type filter hook output priority raw; tcp sport 3306 notrack } }` (iptables: `-t raw -j CT --notrack`) | High connection rate (thousands of connects/s) through stateful firewall; conntrack lock contention visible in `perf top` (`nf_conntrack_*`, `__nf_conntrack_find_get`) | S (and C for dport) | Removes per-packet conntrack lookup; reported 5-15% packet-processing CPU savings at very high pps (generic). Grade C for DB. Must add explicit stateless accept rules since `ct state established` no longer matches. | security (need stateless rules) | yes | runtime |
| Unload conntrack entirely | — | If no NAT/stateful rules needed on dedicated DB host, don't load firewalld/ufw/docker; use cloud security groups | | S | | security (rely on cloud FW) | yes | reboot/service |

### 1.6 MTU / jumbo frames

| Setting | Default | Recommended | When | Side | Evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| `ip link set dev eth0 mtu 9000` (AWS 9001 inside VPC; GCP up to 8896 per VPC setting; Azure: 9000 with MANA adapter, 3900 with Mellanox CX-3/4/5, intra-VNet only <https://learn.microsoft.com/en-us/azure/virtual-network/how-to-virtual-machine-mtu>; Linode/Akamai: 1500) | 1500 | 9000 only if *every* hop (incl. replicas, proxies) supports it and traffic stays inside VPC/placement | Large result sets, replication, backups: fewer packets/softirq per MB. OLTP point selects (< 1 KB) see **no** benefit | B | AWS: jumbo frames inside VPC, 1500 across IGW/VPN/inter-region peering <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/network_mtu.html>. Generic throughput +5-15% CPU efficiency for bulk; Grade B generic, D for OLTP | availability (PMTU black holes -> hangs; enable `tcp_mtu_probing=1`) | cloud-dependent | runtime (brief link reset) |

