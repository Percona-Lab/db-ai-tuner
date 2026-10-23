# Linux OS + Network Tuning for MySQL-family Databases and their Clients

*Research survey, October 2026. Scope: MySQL 8.0/8.4/9.x, Percona Server, MariaDB 10.11/11.x on Linux (kernel 5.14 RHEL9 through 6.x/7.0), bare metal and KVM/virtio cloud VMs (Linode/Akamai, AWS, GCP, Azure). Covers the database server host AND client/application/load-generator hosts.*

Prepared for the AI DB Tuner (`/home/claude/aituner`). Each recommendation carries: **side** (S = server, C = client, B = both), **risk class** (none / availability / durability / security / semantics), **apply mode** (runtime / restart mysqld / remount / reboot), **VM applicability**, and **evidence grade**:

- **A** = vendor doc + independent measured benchmark on a DB workload
- **B** = measured on a DB workload by one credible source, or vendor doc with clear mechanism
- **C** = mechanism is sound, measured only on generic network/web workloads; transferable
- **D** = folklore / weak / anecdotal / obsolete

Defaults below were verified on a live Ubuntu kernel 7.0.0 KVM guest (2 vCPU, 4 GB, kvm-clock) in October 2026 unless stated otherwise; distro kernels differ (RHEL 9 = 5.14, Ubuntu 24.04 = 6.8, Debian 13 = 6.12).

## Executive summary

**Bottom line.** On a modern kernel (≥ 5.10, ideally 6.x) the Linux network defaults are already good for a MySQL server on a LAN. The large, well-documented wins come from **how connections are made** (pooling, Unix sockets, batching), **how many threads are runnable** (thread pool; the EEVDF regression on 6.6+), **storage write-path choices** (doublewrite off on torn-write-protected storage; a filesystem without the ext4 O_DIRECT regression; I/O scheduler), and **memory hygiene** (allocator, THP, swap/NUMA). Network sysctls mostly matter for failure modes: connection storms, ephemeral-port or conntrack exhaustion, single-CPU softirq bottlenecks, and long-RTT (cross-region) links. On cloud VMs most CPU power-management knobs do nothing; check `steal` and the provider's NIC/volume limits first.

### Top 25 settings, ranked by expected impact × evidence quality

| # | Setting | Side | Typical effect (source) | Evidence | Risk | When |
|---|---|---|---|---|---|---|
| 1 | **Connection pooling** instead of connect-per-request (plus `thread_cache_size`) | C (+S) | Removes TCP+TLS+auth+thread setup per request (ms each); avoids TIME_WAIT, port exhaustion and conntrack churn; SSL connect rate collapse shown by Percona | A | none | `Connections`/s high, `Threads_created` rising |
| 2 | **Unix socket for co-located clients/proxies** instead of TCP 127.0.0.1 | C | +33-35% throughput on PK lookups, mysqldump 11% faster (Percona 2020) | A | none | app/proxy/bench on the DB host |
| 3 | **`innodb_doublewrite=OFF` on storage with 16 KiB atomic writes**: AWS EBS Torn Write Prevention, GCP Hyperdisk (ext4 `bigalloc -C 16384`), ZFS recordsize 16K | S | AWS "up to 30%" TPS; GCP: doublewrite costs "up to 25%" on write transactions | A | **durability** if any precondition is wrong | write-heavy OLTP on cloud |
| 4 | **Limit runnable threads: thread pool (Percona/MariaDB), right-sized app pools**; on kernels ≥ 6.6 test EEVDF `NO_PLACE_LAG`+`NO_RUN_TO_PARITY` / `SCHED_BATCH` | S | AWS HammerDB: 6.6+ EEVDF −11 to −18% vs 6.5 when users > CPUs; debugfs flags recover 2-4 points; thread pool prevents collapse at high concurrency | A/B | semantics (thread pool) | `Threads_running` ≫ cores |
| 5 | **jemalloc (or tcmalloc) for mysqld + THP `never`/`madvise`** | S | Fixes steady RSS growth/fragmentation (MySQL Bug #100704, Percona PS-5312); jemalloc+THP `always` → memory not returned → OOM | A | availability | RSS ≫ buffer pool + tracked memory, growing |
| 6 | **XFS, or ext4 with `dioread_nolock` (default since 5.6)** for O_DIRECT InnoDB on NVMe | S | ext4 on 4.9-5.5 without it: 30-80% loss on NVMe (Percona 2019) | A | none | old RHEL/Ubuntu kernels |
| 7 | **`net.core.somaxconn` + `back_log`** (MariaDB default back_log only 150) | S | Eliminates 1-3 s SYN-retry stalls during reconnect storms / failover | A | none | `ListenOverflows` Δ > 0 |
| 8 | **Client ephemeral ports**: `ip_local_port_range=1024 65535`, `tcp_tw_reuse=1` | C | ~470 → ~1,070 new conns/s per destination before `EADDRNOTAVAIL` (60 s TIME_WAIT) | A | minor semantics | per-request connects, load generators |
| 9 | **conntrack**: size `nf_conntrack_max`/hashsize, shorter TIME_WAIT timeout, or `notrack` the DB port | B | Prevents silent packet drops ("table full") — a common outage on Docker/K8s hosts | A | security (notrack needs stateless rules) | conntrack count > 80% of max |
| 10 | **Spread NIC queues/IRQs**: `ethtool -L combined <cores>`, irqbalance; RPS/RFS only if queues < cores | S | Removes single-core softirq ceiling on pps (kernel scaling.rst; AWS ENA guide) | A | none | one CPU at high `%soft`, `time_squeeze` |
| 11 | **CPU governor `performance` + shallow C-states** (tuned `latency-performance` / `postgresql`) — bare metal only | S (+bench C) | Percona: `ondemand`/`powersave` noticeably slower at low/mid concurrency; µs-level wake-up latency | A/B | none (power) | bare metal; no-op on most VMs |
| 12 | **`skip_name_resolve=ON`** | S | Removes DNS lookups per connect (ms to 5 s timeouts) | A | semantics (IP grants) | DNS errors in host_cache |
| 13 | **Swap/NUMA hygiene**: `swappiness=1..10`, `zone_reclaim_mode=0`, `innodb_numa_interleave=ON`, `numa_balancing=0` on multi-socket | S | Prevents "swap insanity" (buffer pool swapped with free RAM on other node) | A | availability if swappiness 0 | NUMA hosts, `pswpout` > 0 |
| 14 | **zstd protocol/replication compression on bandwidth-limited or WAN links** (never on LAN) | B | 1 Gbit bandwidth-bound sysbench: ~2.9k → ~5.0k ops/s (Percona 2019) | A | none (CPU) | link ≥ 70% utilised, RTT > 10 ms |
| 15 | **Batching + server-side prepared statements** (`rewriteBatchedStatements`, multi-row INSERT, statement cache) | C | Removes N−1 round trips and commits; 5-50× for bulk inserts | A | none | many small statements |
| 16 | **Long-RTT links: BBR + fq on the sender, `tcp_rmem`/`tcp_wmem` max ≥ 2×BDP (16-64 MB), `tcp_slow_start_after_idle=0`** | B | BBR: large gains under loss/shallow buffers (Google; YouTube +4% median, far more on lossy paths); newer kernels already default rmem max 32 MB | A (generic) / C (MySQL) | none | cross-region replication, remote clients |
| 17 | **I/O scheduler `none` on NVMe / cloud NVMe** (`mq-deadline` acceptable) | S | Phoronix: `none` best/tied, "big benefit" for MariaDB on PCIe4 NVMe; BFQ/Kyber slower | A | none | scheduler ≠ none on NVMe |
| 18 | **ZFS layout**: recordsize 16K, lz4/zstd, `atime=off`, `logbias=throughput`, `primarycache=metadata`, cap ARC; ≥ 2.3: `direct=standard` + `O_DIRECT` | S | Throughput parity with ext4 and 2.8× less space (Percona 2021); +22% vs md RAID10/ext4 on EBS (Shattered Silicon 2020) | A | none | if ZFS chosen |
| 19 | **Bound dirty page cache**: `dirty_background_bytes` 64-256 MB, `dirty_bytes` 0.5-4 GB | S | Avoids multi-second binlog fsync stalls on big-RAM hosts (tuned `postgresql`: 64 MB / 512 MB) | B | none | `Dirty` > 1 GB, periodic stalls |
| 20 | **Keepalive below middlebox idle timeouts** (`tcp_keepalive_time` 60-300 s, intvl 10-30, probes 3-6) + pool `maxLifetime` | B | Avoids broken pooled connections after idle (AWS NLB 350 s, Azure LB 4 min) | A | none | "link failure" after idle |
| 21 | **CPU vulnerability mitigations** (`mitigations=off` or targeted) | S | CPU-dependent: noise (Percona 2018, I/O bound) to ~10-20% (older Intel/AMD SRSO, Phoronix); ~0% on Zen 5 / Panther Lake | A | **security** | dedicated bare metal only, policy opt-in |
| 22 | **Clocksource pathologies**: never `hpet`; `tsc` on bare metal and old Xen; `kvm-clock` on KVM/Nitro | B | Removes µs-scale `clock_gettime` cost (heavy in P_S timers) | A when present | availability (unstable TSC) | `read_hpet`/`xen` in perf |
| 23 | **Accelerated cloud NIC & placement**: ENA (Express), gVNIC, Azure Accelerated Networking; same AZ / placement group | B | Lower p99 RTT and jitter; avoids pps caps (check ENA `*_allowance_exceeded`) | A | none | cloud |
| 24 | **`vm.min_free_kbytes` 1-3% RAM / `watermark_scale_factor`** on high-pps, big-RAM hosts | S | Prevents GFP_ATOMIC allocation failures/packet drops and direct-reclaim stalls | B | availability if too high | dmesg allocation failures, `allocstall` |
| 25 | **Explicit huge pages for the buffer pool** (`large_pages`, `nr_hugepages`, `hugetlb_shm_group`, `LimitMEMLOCK`) | S | 0-10% on very large in-memory buffer pools; silent fallback if misconfigured | C | availability (reserved RAM) | BP > 100 GB, CPU-bound reads |

**Things that are almost never worth changing for MySQL:** `tcp_fastopen`, `busy_poll/busy_read` on thread-per-connection servers, `tcp_autocorking`, disabling GRO/TSO, `rmem_default/wmem_default`, `vfs_cache_pressure`, ext4 `commit=`, timer slack, legacy CFS scheduler sysctls (gone), `tcp_tw_recycle` (removed 4.12). See §12.

**Kernel-version facts a tuner must know** (verified on Ubuntu kernel 7.0 KVM guest, Oct 2026): `somaxconn` default 4096 (since 5.4); `tcp_tw_reuse` default 2 = loopback only (since 4.20); `rmem_max/wmem_max` default now 4 MB (was 212992); `tcp_rmem` max now up to 32 MB RAM-scaled (was 6 MB); `default_qdisc=fq_codel`, CC `cubic`; `netdev_max_backlog=1000`, `netdev_budget=300/2000 µs`; THP `madvise` on Ubuntu, `always` on RHEL; `tcp_tw_reuse_delay` and `tcp_rto_min_us` sysctls exist only on recent kernels.

**Contents:** 1 TCP/IP · 2 NIC/IRQ · 3 CPU/scheduler/memory · 4 Storage/filesystems · 5 Allocators/huge pages · 6 MySQL network variables · 7 Client side · 8 tuned profiles · 9 Cloud vendors · 10 Signals→settings table · 11 How to measure · 12 Folklore · 13 References

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
| `net.ipv4.ip_local_port_range` | `32768 60999` (28,232 ports) | `1024 65535` (or `10000 65535`) on **clients/proxies**; reserve service ports with `ip_local_reserved_ports` (e.g. 3306,4567,33060,33061,6032,6033) | `connect()` returns `EADDRNOTAVAIL` ("Cannot assign requested address"); `ss -tan state time-wait \| wc -l` near 28 k per dst ip:port; load generators opening a connection per transaction | C (and proxies / ProxySQL backend side) | Max new-conn rate to one `dst ip:port` ≈ ports / TIME_WAIT(60 s) ≈ 470/s with default range -> ~1,070/s with full range. Cloudflare 2022 <https://blog.cloudflare.com/how-to-stop-running-out-of-ephemeral-ports-and-start-to-love-long-lived-connections/>. Grade A | none (ensure no listener inside the range) | yes | runtime |
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


### 1.7 Historical/“copy-paste” sysctl lists — treat with care

Brendan Gregg's Netflix EC2 list (re:Invent 2017, <https://brendangregg.com/blog/2017-12-31/reinvent-netflix-ec2-tuning.html>) is still the most-copied list: `somaxconn=1000`, `netdev_max_backlog=5000`, `rmem_max/wmem_max=16777216`, `tcp_rmem/tcp_wmem=4096 12582912 16777216`, `tcp_max_syn_backlog=8096`, `tcp_slow_start_after_idle=0`, `tcp_tw_reuse=1`, `ip_local_port_range=10240 65535`, `tcp_abort_on_overflow=1 # maybe`. Gregg's own caveat: "developed in late 2017, for Ubuntu Xenial instances on EC2". Note `somaxconn=1000` is now *lower* than the 4096 kernel default (5.4+), and a 12 MB *default* `tcp_rmem/wmem` per socket is wasteful for a DB with thousands of mostly idle connections. A tuner should never blindly apply these.

---

## 2. NIC, interrupts and packet steering

**Headline:** For OLTP MySQL (small packets, high pps, many connections) the classic failure mode is *all network softirq processing landing on one CPU* (single-queue virtio-net, or more queues than are actually enabled, or IRQs all pinned to CPU0). Signal: one CPU at 100% `%soft` in `mpstat -P ALL 1`, `ksoftirqd/N` hot in `top`, `/proc/net/softnet_stat` column 2 (dropped) or column 3 (time_squeeze) increasing. Fix order: (1) enable all hardware queues (`ethtool -L`), (2) make sure IRQs are spread (irqbalance or manual), (3) RPS/RFS only if queues < CPUs. Source: kernel `scaling.rst` <https://docs.kernel.org/networking/scaling.html>; Red Hat RHEL 9 "Monitoring and managing system status and performance" network chapter; AWS ENA best practices <https://github.com/amzn/amzn-drivers/blob/master/kernel/linux/ena/ENA_Linux_Best_Practices.rst>.

**Observation from a live Linode (Akamai) 2-vCPU KVM guest, kernel 7.0:** `ethtool -l eth0` → pre-set max `Combined: 8`, current `Combined: 2`; rings fixed at 256/256 (`ethtool -g`, virtio max); coalescing `rx-usecs 0 rx-frames 1` (no coalescing); TSO/GSO/GRO on, `rx-gro-hw on`, LRO off [fixed]. I.e. on Linode the queue count already equals vCPU count by default; on larger plans verify it.

| Setting | Default | Recommended | When (signal) | Side | Effect / evidence | Risk | VM / virtio? | Apply |
|---|---|---|---|---|---|---|---|---|
| RSS queues `ethtool -L <dev> combined N` | driver-dependent; virtio-net = min(vCPUs, host queues) on modern kernels; ENA = vCPUs up to 32 | N = number of physical cores (or vCPUs) up to NIC max. Kernel doc: "for low latency... as many queues as CPUs"; "for interrupt handling, HT has shown no benefit... limit queues to number of cores" | One CPU saturated in `%soft`; `softnet_stat` time_squeeze/drops on one CPU; `/proc/interrupts` shows only one `virtio0-input.N` incrementing | B | Grade A (kernel doc). Big effect when it was mis-set (single queue): DB pps limited to one core | none | yes: virtio-net multiqueue requires host `queues=N` (most clouds enable it; Linode yes, see above) | runtime (brief traffic blip) |
| IRQ affinity: `irqbalance` vs manual `/proc/irq/<n>/smp_affinity_list` | irqbalance running on most distros | irqbalance is fine for most DB hosts; manual pinning (queue i -> core i, and keep mysqld off those cores only in extreme low-latency setups) | `/proc/interrupts` shows IRQs concentrated; irqbalance in "powersave"/one-shot mode | B | Grade B. Older irqbalance versions packed IRQs onto one core under low load; Red Hat recommends irqbalance unless doing explicit isolation. tuned `cpu-partitioning` bans IRQs from isolated cores. AWS ENA guide: irqbalance OK; consider pinning for latency. | none | yes (virtio MSI-X per queue) | runtime |
| RPS `/sys/class/net/<dev>/queues/rx-<n>/rps_cpus` | 0 (off) | CPU mask of cores in same NUMA node / sharing L3, only when HW queues < cores | Fewer queues than cores and softirq on few CPUs | S (also busy clients) | Grade A (kernel doc). Costs an IPI per packet batch; on hosts with queues == cores RPS adds overhead. | none | yes — very useful on single-queue virtio/older Xen VMs | runtime |
| RFS `net.core.rps_sock_flow_entries` + `rps_flow_cnt` | 0 | `rps_sock_flow_entries=32768..65536` (kernel doc: "65536 works fairly well on a moderately loaded server"), per-queue `rps_flow_cnt` = entries / N | Same as RPS; improves cache locality by steering to the CPU where mysqld thread last ran | S | Grade B (AWS EC2 latency guide recommends RPS+RFS for ENA <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ena-improve-network-latency-linux.html>). Accelerated RFS needs NIC support (mlx5, not virtio) | none | yes | runtime |
| XPS `/sys/class/net/<dev>/queues/tx-<n>/xps_cpus` | set by driver for many (virtio-net sets 1:1 XPS) | each CPU -> one TX queue (kernel doc) | TX lock contention (`perf` shows `_raw_spin_lock` in `sch_direct_xmit`) | B | Grade B | none | yes | runtime |
| Interrupt coalescing `ethtool -C <dev> adaptive-rx on` / `rx-usecs N` | driver: ixgbe/i40e/mlx5 adaptive on; ENA adaptive (DIM) on recent drivers; virtio: off (needs `VIRTIO_NET_F_NOTF_COAL`, kernel 6.2+, host support) | Latency-critical OLTP: low (`rx-usecs 0-8`) or adaptive; throughput/backup/replication: higher (`rx-usecs 50-100`) | High IRQ rate (> 50-100 k/s per core) eating CPU -> raise; p99 latency floor from coalescing -> lower | B | Grade B: Red Hat low-latency guide; Intel ADQ guide. Typical ±10-50 µs per RTT, i.e. measurable for sub-ms point selects but irrelevant once queries take > 1 ms | none | limited on virtio | runtime |
| Ring sizes `ethtool -G <dev> rx 4096 tx 4096` | e.g. 512-1024 (ENA 1024; ixgbe 512; virtio 256 fixed by host) | raise to max when `ethtool -S` shows `rx_missed_errors`, `rx_no_buffer_count`, `rx_fifo_errors`, ENA `rx_drops`/`bw_in_allowance_exceeded`=no (that's a cloud cap, not ring) | Drops at NIC under bursts | S | Grade B. Larger rings = more bufferbloat/cache misses, so only raise on evidence | none | virtio: not changeable by guest on most clouds | runtime (link reset) |
| `net.core.netdev_max_backlog` | 1000 | 5000-30000 | `/proc/net/softnet_stat` col 2 ("dropped") > 0 — applies to RPS / non-NAPI backlog queue | S | Grade B. Gregg 2017 used 5000 | none | yes | runtime |
| `net.core.netdev_budget` / `netdev_budget_usecs` | 300 / 2000 µs | 600 / 4000-8000 | `/proc/net/softnet_stat` col 3 ("time_squeeze") increasing steadily | S | Grade B (Red Hat; Cloudflare "How to receive a million packets"). Moderate effect only at very high pps | small latency fairness cost | yes | runtime |
| GRO / GSO / TSO `ethtool -K <dev> gro on gso on tso on` | on | **keep on** | — | B | Disabling (folklore for "lower latency") costs CPU per byte; with small OLTP packets GRO has little to merge anyway. Grade D for disabling. Exception: GRO + some tunnels/IPVS bugs | — | yes | runtime |
| LRO | off on most | off when forwarding/bridging (k8s, routing) | — | — | Breaks forwarding; irrelevant for OLTP | semantics | usually fixed off on virtio | — |
| HW GRO / header-data split / BIG TCP (`gso_max_size` 185 k, 5.19+/6.3+ IPv4) | off | only for > 25 Gbit bulk flows | backups/clone over 100G | S | Grade C (Google/Meta Netdev talks) | none | rarely | runtime |
| Accelerated networking / SR-IOV / ENA Express / gVNIC | cloud-specific | always use the accelerated NIC type: AWS ENA (+ ENA Express for same-AZ ~ lower p99.9), GCP gVNIC (Tier_1 networking), Azure Accelerated Networking (MANA) | p99 network latency; pps caps | B | Azure: Accelerated Networking reduces latency/jitter and CPU <https://learn.microsoft.com/en-us/azure/virtual-network/accelerated-networking-overview>. Grade A for latency | none | cloud only | VM stop/start |
| Cloud pps / conntrack allowances (AWS ENA `ethtool -S`: `pps_allowance_exceeded`, `conntrack_allowance_exceeded`, `bw_out_allowance_exceeded`, `linklocal_allowance_exceeded`) | — | upsize instance; reduce connection churn; use pooling | Counters > 0 = packets shaped/dropped by hypervisor, not fixable by sysctl | B | AWS docs <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-network-performance-ena.html>. Grade A | — | AWS | — |


---

## 3. CPU, scheduler, kernel and memory management

**Headline:** For MySQL on bare metal the two largest, best-documented CPU-side wins are (1) running at full frequency without deep C-state exit latency (governor `performance` + C-state limit, i.e. tuned `latency-performance`/`throughput-performance`), and (2) not losing 10-15% to the EEVDF scheduler regression on kernels 6.6+ under oversubscription. On cloud VMs most power-management knobs are owned by the hypervisor and do nothing in the guest; check `steal` time instead.

### 3.1 CPU power management

| Setting | Default | Recommended | When (signal) | Side | Effect / evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| cpufreq governor `cpupower frequency-set -g performance` or `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` | `powersave` (intel_pstate active) / `schedutil` (amd-pstate, acpi-cpufreq) | `performance` | Low-to-moderate load OLTP with poor latency; `turbostat` shows low Bzy_MHz while busy; latency improves when load increases (frequency ramp-up artefact) | B | Percona (Tkachenko 2016, sysbench OLTP RO): `ondemand` notably below `performance`; intel_pstate `powersave` worse at low thread counts <https://www.percona.com/blog/cpu-governor-performance/>. Typical 5-20% at low concurrency, ~0% at saturation. Grade A | none (power/cost on-prem) | **No effect on most cloud VMs** (no cpufreq driver exposed; KVM guests show no `/sys/.../cpufreq`) | runtime |
| `energy_performance_preference` (`/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference`) and `energy_perf_bias` (`x86_energy_perf_policy`) | `balance_performance` / 6 | `performance` / 0 | as above, HWP CPUs | B | tuned sets both. Grade B | none | bare metal only | runtime |
| C-state limit: `/dev/cpu_dma_latency` (hold fd open with value, e.g. tuned `force_latency=cstate.id_no_zero:1\|3`), or boot `intel_idle.max_cstate=1 processor.max_cstate=1`, or `cpupower idle-set -D 10` | all C-states enabled (C6 exit ~100+ µs on servers) | Allow C1 (and C1E) only on latency-sensitive DB hosts | p99 latency of point selects poor at moderate load; `turbostat` shows high C6 residency on DB cores; network IRQ wake-up latency | B (clients/load generators too: a slow-waking client inflates measured latency) | Grade B: Red Hat low-latency tuning guide; Intel/Mellanox low-latency guides report tens of µs per wakeup. For DB the effect matters for sub-ms queries. **Note:** limiting C-states can reduce max turbo on some CPUs (fewer idle cores -> less turbo headroom) — measure | none (power + possibly lower turbo) | **no effect on most VMs** (guest uses `haltpoll` or hlt; KVM `cpuidle-haltpoll` with `guest_halt_poll_ns` exists) | runtime (pm_qos) / reboot (boot param) |
| `cpuidle-haltpoll` (KVM guest) `/sys/module/haltpoll/parameters/guest_halt_poll_ns` | 200000 ns when driver loaded (enabled on RHEL/Fedora guests; `cpuidle_haltpoll` module) | keep enabled for latency; disable on oversubscribed hosts | High guest wake-up latency in VMs | B | Grade C: Red Hat claims lower IPI wake-up latency for KVM guests; burns host CPU | none | KVM only | runtime |
| Turbo/boost (`/sys/devices/system/cpu/intel_pstate/no_turbo`=0, `cpufreq/boost`=1) | enabled | enabled (disable only for benchmark repeatability) | Benchmark noise | B | Disabling reduces run-to-run variance in tuning experiments; costs peak throughput | none | bare metal | runtime |
| SMT `nosmt` / `/sys/devices/system/cpu/smt/control` | on | on for throughput; off only for security isolation or consistent latency | — | S | Grade B: SMT typically +15-30% throughput for MySQL OLTP at high concurrency (various vendor tests); costs single-thread latency slightly | security if on (cross-HT side channels) | host-decided in VMs | runtime (off) |

### 3.2 Scheduler (CFS -> EEVDF)

| Setting | Default | Recommended | When | Side | Effect / evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| **EEVDF (kernel ≥ 6.6) feature flags** `/sys/kernel/debug/sched/features`: `NO_PLACE_LAG`, `NO_RUN_TO_PARITY` | `PLACE_LAG RUN_TO_PARITY` enabled | Test `echo NO_PLACE_LAG > /sys/kernel/debug/sched/features; echo NO_RUN_TO_PARITY > ...` on hosts where runnable threads > CPUs | Kernel upgraded 5.x/6.5 -> 6.6+ and throughput dropped at high concurrency (users > CPUs); high `nr_running` / run-queue latency (`runqlat`, `perf sched`) | S | AWS (Prundeanu, LKML Nov 2024) HammerDB TPC-C vs 6.5: 6.8 = -15.3% (MySQL 8.0.30) / -18.1% (8.4.2); 6.12-rc7 = -11.4% / -14.0%; with SCHED_BATCH -11.6% / -12.7%; with NO_PLACE_LAG+NO_RUN_TO_PARITY -9.2% / -10.2% <https://lkml.iu.edu/hypermail/linux/kernel/2411.3/01578.html>. Proposal to make these sysctls rejected by Zijlstra ("you have knobs in debugfs, and that's where they'll stay") <https://lkml.rescloud.iu.edu/2502.1/05730.html>. Grade B (one vendor, reproducible). **Remaining ~10% regression not recoverable via knobs**; thread pool (fewer runnable threads) is the structural fix | none (debugfs, needs root + debugfs mounted; not persistent) | yes | runtime; reset on reboot |
| `SCHED_BATCH` for mysqld (`chrt -b -p 0 <pid>`, or systemd `CPUSchedulingPolicy=batch`) | SCHED_OTHER | candidate under EEVDF | as above | S | Recovered ~1-3 points in the AWS test above. Grade B | none | yes | restart (systemd) / runtime (chrt per thread) |
| `/sys/kernel/debug/sched/base_slice_ns` (EEVDF; replaced `min_granularity_ns`) | 0.75 ms × (1 + ilog2(min(ncpu, 8))) = up to 3 ms | 3-6 ms test | CPU-bound oversubscribed | S | Grade C | none | yes | runtime (debugfs) |
| Legacy CFS knobs `kernel.sched_min_granularity_ns`, `sched_wakeup_granularity_ns`, `sched_migration_cost_ns`, `sched_latency_ns` | moved to debugfs in 5.13; `wakeup_granularity`/`latency` removed with EEVDF in 6.6 | **Obsolete as sysctls.** Older tuned `throughput-performance` set min_granularity 10 ms / wakeup 15 ms; current upstream tuned no longer does (verified tuned master Oct 2026) | — | — | Postgres community folklore `sched_migration_cost_ns=5000000`, `sched_autogroup_enabled=0` came from 3.x/4.x kernels. Grade D on 6.6+ | — | — | — |
| `kernel.sched_autogroup_enabled` | 1 (Ubuntu/Debian), 0 on RHEL server kernels | 0 on DB servers | Only matters when processes from different TTY sessions compete (e.g. backup tool started from ssh session competes with mysqld as equal "groups") | S | Grade C: Postgres/Gregg list; harmless | none | yes | runtime |
| `kernel.timer_migration` | 1 | 0 (tuned network-latency) | Jitter | S | Grade C | none | yes | runtime |
| `kernel.nmi_watchdog` | 1 | 0 (tuned network-latency; saves a PMU counter + periodic NMI) | perf PMU counters exhausted; jitter | S | Grade C | availability (no hard-lockup detection) | VMs often no NMI watchdog | runtime |
| `skew_tick=1` boot param | 0 | 1 on large-core-count latency-sensitive hosts | jiffies lock contention, jitter | S | tuned network-latency adds it (with `tsc=reliable`). Grade C | none | yes | reboot |
| Timer slack `prctl(PR_SET_TIMERSLACK)` / `/proc/<pid>/timerslack_ns` | 50 µs | leave | — | — | No DB evidence. Grade D | — | — | — |
| CPU affinity / isolation for mysqld (`systemd CPUAffinity=`, `isolcpus`, cgroup cpusets) | none | avoid by default; consider separating NIC IRQ cores from mysqld on > 32-core boxes | IRQ and query threads thrash same cores | S | Grade C | availability if too few cores | partial | restart |

### 3.3 Clocksource, mitigations, audit

| Setting | Default | Recommended | When | Side | Effect / evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| clocksource `/sys/devices/system/clocksource/clocksource0/current_clocksource` | `tsc` bare metal; `kvm-clock` KVM (incl. Linode, AWS Nitro, GCP); `hyperv_clocksource_tsc_page` Azure; `xen` old Xen | bare metal: `tsc`; KVM: keep `kvm-clock` (vDSO-capable, AWS-recommended for Nitro) unless host exposes invariant TSC, then `tsc` (boot `tsc=reliable`) is marginally faster; **old Xen: switch `xen`->`tsc`** | `perf top` shows `read_hpet`/`pvclock`/`xen_clocksource_read` hot, or dmesg "clocksource tsc unstable; switching to hpet" (catastrophic: `hpet` reads are µs-slow syscalls) | B | Netflix/Gregg: xen->tsc on EC2 Xen saved substantial CPU; AWS: on Nitro use kvm-clock <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/set-time.html>. MySQL calls `clock_gettime` heavily (performance_schema timers, `my_timer`). Grade A for hpet/xen pathologies, D for kvm-clock -> tsc on modern KVM | availability (time jumps if TSC unstable) | yes | runtime |
| `mitigations=off` boot param (or per-vuln `spectre_v2=off`, `retbleed=off`, `spec_rstack_overflow=off`, `gather_data_sampling=off`, `nopti`) | `auto` | keep on in multi-tenant / cloud; consider off only on dedicated bare-metal DB hosts on older CPUs after measuring | High `%sys`, high syscall/context switch rate (`perf stat -e syscalls`), older CPUs (Intel Skylake-Cascade Lake, AMD Zen 1-4) | B | Percona Jan 2018 Meltdown/PTI on bare metal sysbench: within 3-4% noise for InnoDB (I/O dominated) <https://www.percona.com/blog/does-the-meltdown-fix-affect-performance-for-mysql-on-bare-metal/>; PostgreSQL PTI 10-12% in-memory read-only (2ndQuadrant/EDB 2018). Phoronix: databases among most-affected workloads on older Intel and on AMD Inception/SRSO safe-RET (MariaDB big penalty) <https://www.phoronix.com/review/amd-inception-benchmarks/4>, but **Zen 5 and Intel Panther Lake: mitigations=off gives ~no benefit** <https://www.phoronix.com/review/amd-zen5-mitigations-off>, <https://www.phoronix.com/review/intel-panther-lake-mitigations>. Grade A (CPU-dependent: 0-20%) | **security** | **guest-only effect; host mitigations still apply; never on shared tenancy** | reboot |
| auditd syscall rules (`auditctl -l`), boot `audit=0` | distro-dependent; CIS/STIG profiles add many `-a always,exit -S ...` rules | minimal rule set on DB hosts; exclude mysqld syscalls if compliance permits | High `%sys`, `perf top` shows `audit_*`, `__audit_syscall_exit` | S | Grade C: each syscall rule adds per-syscall cost; known large overheads with CIS hardening on high-syscall workloads (Red Hat audit docs) | security/compliance | yes | runtime (rules) |
| SELinux/AppArmor | enforcing | keep; tuned network-latency sets SELinux `avc_cache_threshold=8192` | AVC cache misses (`/sys/fs/selinux/avc/cache_stats`) | S | Grade C | security if disabled | yes | runtime |

### 3.4 Memory management

| Setting | Default | Recommended | When | Side | Effect / evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| THP `/sys/kernel/mm/transparent_hugepage/enabled` and `/defrag` | RHEL: `always`/`madvise`; Ubuntu/Debian: `madvise`/`madvise` (verified Ubuntu 7.0 kernel) | `never` or `madvise` for mysqld (Percona: `never`); **must not be `always` with jemalloc** | RSS growth beyond buffer pool, `khugepaged` CPU, latency spikes from compaction (`compact_stall` in `/proc/vmstat`, `thp_fault_alloc`) | S | Percona 2023 OS checklist: THP never, swappiness 1, jemalloc, noatime <https://www.percona.com/blog/dont-start-using-your-mysql-server-until-youve-configured-your-os/>; Percona (Ahmed 2019, PG OLTP): THP on/off "almost overlapping" — no gain, no loss <https://www.percona.com/blog/settling-the-myth-of-transparent-hugepages-for-databases/>; jemalloc + THP -> memory not returned, OOM over time (Percona jemalloc posts). Counterpoint: MongoDB 8.0 now *recommends enabling* THP with new per-CPU tcmalloc. Grade A for "never with jemalloc", B for "no perf gain" | none (perf); availability with jemalloc+always | yes | runtime (affects new allocations; restart mysqld) |
| `vm.swappiness` | 60 | 1-10 (Percona 1; tuned 10). Avoid 0 on kernels ≥ 3.5 (OOM before any swap) | Buffer pool pages swapped (`si/so` in vmstat, `pswpout`), "swap insanity" | S | Grade A (Percona, MySQL docs, Jeremy Cole NUMA swap-insanity) | availability if 0 + no headroom (OOM) | yes | runtime |
| Swap device presence | distro | small swap (or zswap) as safety net; size so mysqld OOM is preferred over thrash per policy | — | S | Opinion split; Grade C | availability | yes | runtime (swapon) |
| `vm.dirty_background_bytes` / `vm.dirty_bytes` (or `_ratio`) | ratio 10 / 20 | with `innodb_flush_method=O_DIRECT` page-cache dirt is mostly binlog, relay log, redo (unless redo O_DIRECT), tmp files, backups -> bound it: `dirty_background_bytes=64-256 MB`, `dirty_bytes=1-4 GB`. tuned latency-performance: `dirty_bytes=10%`, `dirty_background_bytes=3%` (sic, %-form); throughput-performance: 40% / 10% | Periodic write stalls (fsync of binlog takes seconds; `Dirty` in `/proc/meminfo` in GBs; `writeback` spikes in `iostat`) on large-RAM hosts | S | Grade B: large ratio-based dirty limits on 256 GB+ hosts let tens of GB accumulate, then `sync_binlog=1` fsync stalls | none | yes | runtime |
| `vm.dirty_expire_centisecs` / `dirty_writeback_centisecs` | 3000 / 500 | leave or 1000-1500 / 100-500 | same | S | Gregg 2017 used 12000 (Netflix non-DB). Grade C | none | yes | runtime |
| `vm.min_free_kbytes` | ~ sqrt(16×RAM_kB), e.g. 67584 on 4 GB; ~90 MB on 64 GB | 1-3% of RAM on hosts with high network rates / large RAM (e.g. 256 MB-2 GB), keep < 5% | dmesg `page allocation failure: order:0, mode:GFP_ATOMIC` from NIC driver / skb alloc; direct reclaim stalls (`allocstall` in `/proc/vmstat`) | S | Grade B (Red Hat KCS, Cloudflare). Too high -> OOM/less cache | availability if too high | yes | runtime |
| `vm.watermark_scale_factor` | 10 | 100-500 on big-RAM DB hosts with allocstall | `allocstall_*` rising, kswapd behind | S | Grade C (kernel doc; used by large shops) | none | yes | runtime |
| `vm.vfs_cache_pressure` | 100 | leave (50 only for many-file workloads, e.g. thousands of tables with `innodb_file_per_table`) | dentry/inode slab dominating | S | Grade D for typical InnoDB | none | yes | runtime |
| `vm.zone_reclaim_mode` | 0 (since 3.16 on most) | **0** | NUMA host reclaiming local pages while remote nodes free -> swap | S | Jeremy Cole "swap insanity" (2010) and zone_reclaim posts; Grade A | none | yes (multi-node VMs only) | runtime |
| `kernel.numa_balancing` | 1 on NUMA hw (0 when single node) | 0 for mysqld on multi-socket (tuned network-latency sets 0); or test | `numa_hint_faults`, `numa_pages_migrated` high in `/proc/vmstat`; `%sys` in `task_numa_work` | S | Grade B. Some vendor tests show AutoNUMA neutral-to-negative for large shared buffer pools | none | multi-node VMs only | runtime |
| NUMA interleave of buffer pool: MySQL `innodb_numa_interleave=ON` (5.7.9+, needs NUMA-enabled build; Percona/MySQL packages yes), MariaDB 10.2+ same; or `numactl --interleave=all` in systemd `ExecStart` / `mysqld_safe --numa-interleave` (Percona) | OFF | ON on ≥ 2 NUMA nodes when buffer pool > one node's RAM | Node imbalance in `numastat -p mysqld`, swap while free memory exists | S | Grade A (Cole; Percona; MySQL Bug #72811 fix) | none | multi-node VMs | restart |
| `vm.overcommit_memory` | 0 | 0 (or 2 with careful ratio) | — | S | Grade C | availability | yes | runtime |
| `vm.max_map_count` | 65530 (1048576 on newer distros) | raise only if many mmaps (jemalloc with many arenas, huge table counts) | `mmap: ENOMEM` | S | Grade C | none | yes | runtime |
| `oom_score_adj` for mysqld (systemd `OOMScoreAdjust=-800`) | 0 | -800 to -1000 | OOM killer picks mysqld over smaller processes | S | Percona 2023 checklist. Grade B | availability (another process killed) | yes | restart |


---

## 4. Storage stack and filesystems

**Headline:** With `innodb_flush_method=O_DIRECT` (default on Linux in MySQL 8.4 is `O_DIRECT` for data files; MariaDB 10.6+ defaults `O_DIRECT`), most block-layer read tunables (readahead) do not affect InnoDB data files. What matters: (1) the filesystem choice/mount on fast NVMe (XFS or ext4 ≥ 5.6 with `dioread_nolock`), (2) I/O scheduler `none` on NVMe, (3) not letting buffered binlog/redo/temp writes build up GBs of dirty page cache, (4) the device's fsync latency (write cache + power-loss protection) and cloud volume IOPS/throughput caps. ZFS is competitive when configured correctly and wins on space via compression.

### 4.1 Block layer

| Setting | Default | Recommended | When (signal) | Side | Effect / evidence | Risk | VM? | Apply |
|---|---|---|---|---|---|---|---|---|
| I/O scheduler `/sys/block/<dev>/queue/scheduler` | NVMe: `none`; SCSI/virtio-blk/SATA: `mq-deadline` (some distros `bfq` on rotational) | NVMe: `none`; SATA SSD / cloud block (virtio, EBS-nvme, PD): `none` or `mq-deadline`; HDD: `mq-deadline`. Avoid `bfq`/`kyber` for DB. tuned profiles leave it; udev rule to persist | `iostat -x` shows high `aqu-sz` with low `%util` CPU in scheduler; `perf` shows `dd_dispatch_request` | S | Phoronix 5.6 (NVMe): `none` best or tied for random I/O and "big benefit" for MySQL/MariaDB on PCIe4 SSD; BFQ/Kyber slower <https://www.phoronix.com/review/linux-56-nvme/2>. Gregg 2017: `noop` (pre-blk-mq name). Grade A | none | yes (virtio-blk exposes `none`/`mq-deadline`) | runtime |
| `/sys/block/<dev>/queue/nr_requests` | 2× queue depth (often 256-1023 for NVMe, 64 for mq-deadline/virtio) | 256-1024 for SAN/cloud volumes with high IOPS | Saturated device queue at `nr_requests` (`aqu-sz` flat at value) | S | Gregg 2017: 256. Grade C | none | yes | runtime |
| Queue depth `/sys/block/sdX/device/queue_depth` (SCSI) / NVMe `io_queue_depth` module param | driver (SCSI 32-254, NVMe 1023) | raise for SAN/HBA only | — | S | Grade C | none | rarely | runtime / reboot |
| `/sys/block/<dev>/queue/read_ahead_kb` (`blockdev --setra`) | 128 KB (tuned throughput-performance raises to ≥ 4096 KB!) | 16-128 KB for OLTP data volumes; larger (≥ 1 MB) only for backup/restore/sequential scan volumes | Read throughput (`rkB/s`) ≫ logical reads, page cache churn; matters only for **buffered** I/O (binlog, `innodb_flush_method=fsync` incl. ZFS, MyISAM, xtrabackup) — ignored by O_DIRECT | S | Grade B. Note tuned `throughput-performance` 4 MB readahead can hurt buffered random-read DBs (e.g. MySQL on ZFS/`fsync`). | none | yes | runtime |
| `/sys/block/<dev>/queue/rq_affinity` | 1 | 2 (complete on the submitting CPU) | Completion IRQ CPU hot (`%irq`/`%soft` on one CPU from `nvme`/`virtio_blk`) | S | Gregg 2017: 2. Grade C | none | yes | runtime |
| `/sys/block/<dev>/queue/nomerges` | 0 | 0; 2 only on NVMe with CPU-bound submit path | — | S | Grade D for DB | none | yes | runtime |
| `/sys/block/<dev>/queue/add_random` | 0 on NVMe/SSD already | 0 | — | S | Grade D (already default) | none | yes | runtime |
| `/sys/block/<dev>/queue/write_cache` | reported by device: `write back` (volatile cache) or `write through` | Leave as reported. Setting `write through` on a device with volatile cache to skip FLUSH = **data loss on power failure**. On enterprise NVMe/RAID with power-loss protection (PLP / BBU), device may already report `write through` -> cheap fsync | fsync latency high (`ioping -D -W`, `fio --fsync=1`, `Innodb_os_log_fsyncs` vs `Innodb_data_fsyncs` time) | S | Grade A for the risk. The big durability-safe win is **hardware with PLP**: fsync 20-50 µs vs 1-5 ms on consumer NVMe | **durability** | VM: hypervisor decides; virtio often `write back` | runtime |
| NVMe `io_poll` / `nvme.poll_queues=N` boot param | 0 poll queues | not for MySQL | — | S | Only used by `RWF_HIPRI`/io_uring IOPOLL; neither MySQL nor MariaDB issues polled I/O. Grade D | — | — | reboot |
| `discard` mount option vs `fstrim.timer` | distro: `fstrim.timer` weekly (Ubuntu/RHEL); no `discard` | `fstrim.timer` weekly; avoid online `discard` on older SSDs/ext4 (sync TRIM stalls). XFS/ext4 online discard is async on newer kernels but still costs | Write latency spikes coincide with deletes/DROP TABLE | S | Gregg 2017 used `discard` (EC2 instance store). Grade B | none | cloud block volumes often ignore TRIM | remount |
| MariaDB io_uring (`innodb_use_native_aio=1` with liburing build) | ON (MariaDB 10.6+ uses io_uring when built with liburing; disabled on kernels 5.11-5.15 with known regression) | default | — | S | MariaDB 10.6.5 release notes: io_uring write-loss kernel regression, native AIO off until kernel 5.16 <https://mariadb.com/docs/release-notes/community-server/10.6/10.6.5>. MySQL/Percona use libaio. Grade B | durability on bad kernels | yes | restart |
| Block atomic writes (`RWF_ATOMIC`, kernel 6.11+; ext4 6.13; XFS 6.13+ / multi-block 6.16) | n/a | emerging: allows `innodb_doublewrite=OFF` safely if the device advertises atomic write unit ≥ 16 KB (NVMe AWUPF) and the DB uses it | Doublewrite I/O overhead (write amplification ~2× on data pages) | S | Grade C: kernel support exists; MySQL upstream does not use RWF_ATOMIC as of 2026; MariaDB uses vendor-specific atomic writes on some hardware (ScaleFlux, FusionIO legacy). Do not disable doublewrite on ext4/XFS without it | **durability** | — | — |

### 4.2 ext4

| Option | Default | Recommended | When | Effect / evidence | Risk | Apply |
|---|---|---|---|---|---|---|
| `noatime` (implies `nodiratime`) | `relatime` | `noatime` | Read-heavy workloads on buffered files; minimal effect on InnoDB O_DIRECT files (mtime updates still happen) | Percona 2023 checklist <https://www.percona.com/blog/dont-start-using-your-mysql-server-until-youve-configured-your-os/>. With `relatime` default the gain vs `noatime` is small. Grade B | none | remount |
| `lazytime` | off | optional | Reduces inode timestamp writes for frequent mtime updates (InnoDB writes update mtime -> with fdatasync less important) | Grade C | none (timestamps lag in memory) | remount |
| `dioread_nolock` | **default since kernel 5.6** | ensure on (older RHEL 8 4.18 kernels: add explicitly) | ext4 + O_DIRECT + NVMe + high concurrency: 30-80% throughput loss on kernel 4.9-5.5 without it | Percona (Stroganov, Nov 2019): ext4 parallel DIO regression 30-80% on NVMe, fixed with kernel ≥ 5.3 + `dioread_nolock` or by using XFS <https://www.percona.com/blog/watch-out-for-disk-i-o-performance-issues-when-running-ext4/>. Grade A | none | remount |
| `data=ordered` | default | keep | — | `data=writeback` gains little for O_DIRECT InnoDB (data not journaled anyway), risks stale data in files after crash for buffered files (binlog). Grade B | durability/security (writeback) | remount (not allowed to change on remount for root) |
| `barrier=0` / `nobarrier` | barriers on | **never** unless storage has PLP and you've verified | — | Large fsync gain on volatile-cache devices precisely because it's unsafe. XFS removed `nobarrier` in 4.19 (ignored). Grade A for risk | **durability** | remount |
| `commit=N` | 5 s | leave (InnoDB/binlog fsync explicitly) | — | Affects only non-fsynced metadata/buffered data. Grade D | durability (higher N) | remount |
| `journal_async_commit` | off | no | — | Incompatible with `data=ordered` (mount refused on modern kernels); Grade D | durability | — |
| `stride=`, `stripe_width=` (mkfs.ext4 -E) | auto from md/lvm geometry | set to RAID chunk / full stripe in 4K blocks on HW RAID that doesn't export geometry | Write amplification on RAID5/6 | Grade B (mkfs docs) | none | mkfs |
| mkfs: `-O ^has_journal` | — | no | — | Removes crash safety | durability | — |

### 4.3 XFS

| Option | Default | Recommended | When | Effect / evidence | Risk | Apply |
|---|---|---|---|---|---|---|
| `inode64` | default since kernel 3.7 | (default) | — | Folklore to set explicitly. Grade D | — | — |
| `logbsize=256k` | 32k (or stripe unit) | 256k | Metadata-heavy workloads: many CREATE/DROP/TRUNCATE tables, many `.ibd` file extends | Grade B (XFS FAQ, Dave Chinner) | none | remount |
| `logbufs=8` | 8 | (default) | — | Grade D | — | — |
| `allocsize=` | dynamic speculative preallocation | leave; `allocsize=64m` only for append-heavy binlog/relay dirs if fragmentation | fragmented binlogs | Grade C | none (temporary space overuse) | remount |
| `agcount` (mkfs) | auto (4 for small; scales with size) | default; more AGs (e.g. 32-64) for very parallel file creation | — | Grade C | none | mkfs |
| `noatime` | relatime | noatime | as ext4 | Grade B | none | remount |
| `nobarrier` | — | removed in 4.19 | — | Obsolete | — | — |
| External log device `logdev=` | internal | only on HDD arrays | — | Grade C | none | mkfs |

**ext4 vs XFS for InnoDB:** On modern kernels (≥ 5.6) both are within a few percent for O_DIRECT InnoDB; XFS historically scales better with concurrent O_DIRECT writes to the same file (ext4 inode lock), which is why Percona/MySQL/Oracle documentation often prefer XFS. Phoronix Linux 7.0 filesystem benchmarks (2026): XFS narrowly first for MariaDB read/write; Btrfs with `nodatacow` fastest for TigerBeetle; CoW filesystems slowest for SQLite <https://www.phoronix.com/review/linux-70-filesystems/4>, Linux 6.11 round <https://www.phoronix.com/review/linux-611-filesystems/2>. Grade B.

### 4.4 ZFS (OpenZFS 2.2-2.4) for MySQL/MariaDB

| Setting | Default | Recommended | Why / evidence | Risk | Apply |
|---|---|---|---|---|---|
| `recordsize` (data dataset) | 128K | `16K` (= `innodb_page_size`) | Avoids read-modify-write of 128K records for 16K page writes. Percona and Shattered Silicon <https://shatteredsilicon.net/mysql-mariadb-innodb-on-zfs/>, OpenZFS workload tuning <https://openzfs.github.io/openzfs-docs/Performance%20and%20Tuning/Workload%20Tuning.html#innodb>. Grade A | none | new files only (`zfs set`, then rewrite) |
| `recordsize` (redo/binlog dataset) | 128K | 128K (keep) | Sequential append | none | — |
| `compression` | lz4 (2.x default `on` = lz4) | `lz4` (or `zstd-1..3` for capacity) | Percona (Trudeau 2021, Azure, PS 8.0 TPCC ~200 GB): throughput within margin of error vs ext4, **2.8× space reduction** (191 GB -> 69 GB) <https://www.percona.com/blog/mysql-zfs-performance-update/>. Shattered Silicon (Bobic 2020): ZFS mirror stripe vs md RAID10+ext4 on EBS: +22% overall, inserts much faster. Grade A | none | new writes |
| `atime` | on | `off` | standard | none | runtime |
| `logbias` | latency | `throughput` for data dataset (avoid double-writing large sync writes to ZIL); keep `latency` for redo/binlog if a fast SLOG exists | OpenZFS docs | none | runtime |
| `primarycache` | all | `metadata` for data dataset when buffer pool is sized properly (avoid double caching); `all` if RAM spare and compression ratio high (ARC holds compressed pages) | Percona/Shattered Silicon; Klara 2026 discusses trade-off <https://klarasystems.com/articles/managing-cache-and-direct-io-for-databases-on-zfs/> | none | runtime |
| `direct` (OpenZFS ≥ 2.3) | `standard` | `standard` + `innodb_flush_method=O_DIRECT` on 16K-recordsize dataset: aligned 16K writes bypass ARC; misaligned writes fall back to buffered | OpenZFS 2.3 release (Jan 2025) Direct I/O <https://github.com/openzfs/zfs/releases/tag/zfs-2.3.0>; Klara: writes must be page- **and recordsize**-aligned, else buffered; 2.4 adds "uncached" I/O. Before 2.3, O_DIRECT was accepted but ignored (data still went through ARC). Grade B (no published MySQL benchmark of 2.3 direct I/O found as of Oct 2026) | none | runtime |
| `sync` | standard | `standard`. `disabled` only for bulk loads / replicas you can rebuild | `disabled` = lose last ~5 s (txg) on crash — but ZFS stays consistent | **durability** | runtime |
| `xattr=sa`, `ashift=12` (or 13 for some NVMe) | xattr=on (sa default in 2.2+ on Linux), ashift auto | as listed | Shattered Silicon | none | ashift at pool create |
| SLOG (separate ZIL device) | none | fast PLP NVMe/Optane if `sync_binlog=1` + `innodb_flush_log_at_trx_commit=1` and pool on slower disks | fsync latency | none | runtime |
| `zfs_arc_max` (module param) | 50% RAM (Linux, pre-2.3) / ~all minus 1 GB (2.3+) | cap explicitly so buffer pool + ARC + OS fit (e.g. 10-20% RAM with `primarycache=metadata`) | Buffer pool + ARC contention -> swapping/OOM | availability | runtime |
| `zfs_txg_timeout` | 5 | leave | Grade D | — | runtime |
| InnoDB side: `innodb_doublewrite=0` | ON | **OFF is safe on ZFS** because records are written atomically CoW, provided recordsize ≥ page size and `innodb_log_write_ahead_size`=16K (MySQL) | Shattered Silicon, Percona, MariaDB list discussion <https://lists.mariadb.org/hyperkitty/list/discuss@lists.mariadb.org/thread/COW2CCKGSS2VA6Q53LZQQRF5XROHRSPF>. Grade A (widely used) | durability only if misconfigured (recordsize > page size is still atomic; smaller is not) | restart |
| InnoDB side: `innodb_flush_method` | O_DIRECT (8.4) | ZFS < 2.3: `fsync` (O_DIRECT not honoured); ZFS ≥ 2.3: `O_DIRECT` | | none | restart |
| InnoDB side: `innodb_use_native_aio=0`, `innodb_checksum_algorithm=none`, `innodb_flush_neighbors=0` | — | Shattered Silicon recommendations; checksum=none is debated (keeps end-to-end detection if left on) | | durability (checksum off: ZFS still checksums) | restart |

### 4.5 btrfs

`nodatacow` (mount) or `chattr +C` on the empty datadir: disables CoW **and checksums/compression** for those files; Percona 2012 "btrfs probably not ready yet" <https://www.percona.com/blog/2012/05/22/btrfs-probably-not-ready-yet/> found poor InnoDB results with CoW; Phoronix 2024-2026 still shows default btrfs slowest for DB workloads, nodatacow competitive (TigerBeetle). With CoW on, you must keep `innodb_doublewrite=ON` (btrfs CoW extents are not guaranteed 16K-atomic for O_DIRECT overwrite). Grade B: **not recommended for MySQL datadir** unless snapshots are a hard requirement; prefer XFS, or ZFS for compression/snapshots.

### 4.6 Cloud volumes and torn-write protection (the biggest storage-side win available on cloud VMs)

**Disabling the InnoDB doublewrite buffer is safe on storage that guarantees 16 KiB atomic writes, if the filesystem doesn't split the write.** Both AWS and GCP document this:

| Platform | Mechanism | Required OS config | MySQL config | Reported gain | Grade |
|---|---|---|---|---|---|
| AWS EBS on Nitro (all regions) + Im4gn/Is4gen instance store | Torn Write Prevention (TWP), 16 KiB atomic | `mkfs.ext4 -O bigalloc -C 16384` (XFS not supported for this) | `innodb_flush_method=O_DIRECT`, `innodb_doublewrite=0` | "up to 30%" TPS <https://aws.amazon.com/about-aws/whats-new/2023/02/aws-expands-torn-write-prevention-ec2-im4gn-is4gen-instances-ebs-regions>, config <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-twp.html>; RDS "Optimized Writes" is the managed version | A |
| GCP Hyperdisk | built-in torn write protection | ext4 with `bigalloc`, cluster 16 KiB (or larger power-of-2 multiple); "avoid XFS if disabling doublewrite" | `innodb_doublewrite=0`, `innodb_flush_method=O_DIRECT_NO_FSYNC` or `O_DIRECT`; `innodb_numa_interleave=1` on C-series | doublewrite "performance overhead of up to 25% for MySQL write transactions" <https://cloud.google.com/compute/docs/instances/mysql/mysql-workloads> | A |
| ZFS (any) | CoW records | recordsize=16K | `innodb_doublewrite=0` | see §4.4 | A |
| Linux block atomic writes (`RWF_ATOMIC`, NVMe AWUPF) | kernel 6.11+ | ext4/XFS support landing 6.13-6.16 | not yet used by MySQL | — | C |

Risk class: **durability** if any precondition is wrong (wrong cluster size, XFS, LVM/md splitting I/O, volume type without TWP). A tuner must verify, not assume.

**Other cloud volume facts.** The OS rarely is the bottleneck on cloud block storage; the provisioned IOPS/throughput and per-I/O latency dominate (EBS gp3 ~0.5-1 ms, io2 Block Express sub-ms, GCP Hyperdisk Balanced default 3,000 IOPS / 140 MiBps up to 160k IOPS / 2,400 MBps, Azure Premium SSD v2 performance decoupled from size; Azure Premium SSD (v1) host caching: `ReadOnly` for data disks, `None` for redo/binlog disks <https://learn.microsoft.com/en-us/azure/virtual-machines/premium-storage-performance>). GCP recommends `relatime,lazytime` mounts, ext4 or XFS, THP off. Signals: `iostat -x` `r_await`/`w_await` near provider's documented latency; `%util` is meaningless on multi-queue devices; AWS CloudWatch `VolumeQueueLength`, EBS NVMe stats. Use local NVMe (instance store) for replicas you can rebuild.



---

## 5. Memory allocators and huge pages for mysqld

**Headline:** Allocator choice rarely changes steady-state OLTP throughput by more than a few percent on modern glibc (≥ 2.26 with tcache), but it is the #1 fix for **RSS that keeps growing beyond `buffer_pool + per-thread buffers`** (fragmentation / arenas not returned to the OS) and for allocator lock contention at > 1000 threads. Percona's current OS checklist recommends jemalloc + THP `never` (Subbota, Sept 2023 <https://www.percona.com/blog/dont-start-using-your-mysql-server-until-youve-configured-your-os/>). Older Percona allocator benchmark (Stroganov) <https://www.percona.com/blog/impact-of-memory-allocators-on-mysql-performance/>: jemalloc/tcmalloc best scalability; glibc variants dropped "almost two times at high threads" (old glibc, pre-tcache, so partly obsolete).

### 5.1 Allocator comparison

| Allocator | How to enable | Key knobs | Evidence (DB) | Pitfalls | Grade |
|---|---|---|---|---|---|
| **glibc malloc** (ptmalloc2) | default | `GLIBC_TUNABLES=glibc.malloc.arena_max=N` (= legacy `MALLOC_ARENA_MAX`; default 8 × cores on 64-bit), `glibc.malloc.trim_threshold`, `glibc.malloc.mmap_threshold`, `glibc.malloc.tcache_count`, `glibc.malloc.hugetlb=1` (2.35+: THP madvise for heap) <https://www.gnu.org/software/libc/manual/html_node/Memory-Allocation-Tunables.html> | MySQL Bug #100704 (8.0.19-8.0.21): RSS climbs to 83% RAM on idle servers; "Not a bug", allocator retention; users fixed with tcmalloc <https://bugs.mysql.com/bug.php?id=100704>. Percona (Smirnova 2019) PS-5312 FTS memory growth fixed by jemalloc <https://www.percona.com/blog/mysql-memory-management-memory-allocators-and-operating-system/> | Many arenas -> fragmentation and RSS growth with thousands of threads; `arena_max=2..4` reduces RSS but can add lock contention. `malloc_trim()` not called by mysqld | B |
| **jemalloc** 5.x (5.3.0 = last tagged release 2022; repo archived June 2025, **unarchived and maintained by Meta again from ~March 2026** <https://urandom.io/blog/2026-03-17-jemalloc-the-infrastructure-you-never-think-about>) | systemd drop-in `Environment="LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"` (Debian/Ubuntu) or `/usr/lib64/libjemalloc.so.2` (RHEL); for `mysqld_safe`/`mariadbd-safe`: `[mysqld_safe] malloc-lib=/usr/lib64/libjemalloc.so.2`. Percona: RHEL `/etc/sysconfig/mysql` `LD_PRELOAD=...` <https://www.percona.com/blog/enabling-jemalloc-on-percona-server-for-mysql-5-7-and-8-0-series/>. Percona Server 8.4 packages depend on jemalloc. Verify: `grep jemalloc /proc/$(pidof mysqld)/maps` | `MALLOC_CONF=background_thread:true,dirty_decay_ms:...,muzzy_decay_ms:...,narenas:N,percpu_arena:percpu,metadata_thp:auto\|disabled,thp:never`. jemalloc TUNING.md: memory-priority apps -> `background_thread:true,tcache_max:4096` + shorter decay (`dirty_decay_ms:5000,muzzy_decay_ms:5000`) + lower arena count <https://github.com/jemalloc/jemalloc/blob/dev/TUNING.md> | Percona: less fragmentation, recommended; "Percona Cluster processed nearly twice as many queries" (third-party, Grade C). | **jemalloc + THP `always` = RSS never shrinks -> OOM** (Percona). 3.x vs 4.x vs 5.x: 4.x introduced decay-based purge (`decay_time`); 5.0 replaced `lg_dirty_mult`/`decay_time` with `dirty_decay_ms` (default 10 s) + `muzzy_decay_ms` (default 0 since 5.2 / 10 s in 5.0-5.1) and needs `background_thread:true` to purge when idle (otherwise purge only happens on allocation activity → idle server keeps RSS). Ubuntu 18.04 shipped jemalloc 3.6 (`libjemalloc.so.1`), newer ship 5.x (`.so.2`) — old guides reference `.so.1` | A (RSS), B (perf) |
| **tcmalloc (gperftools)** | `LD_PRELOAD=/usr/lib64/libtcmalloc.so.4` or `malloc-lib=tcmalloc` (MariaDB doc <https://mariadb.com/kb/en/using-mariadb-with-tcmalloc-or-jemalloc/>) | `TCMALLOC_RELEASE_RATE` (default 1.0; higher returns memory faster), `TCMALLOC_MAX_TOTAL_THREAD_CACHE_BYTES` (default 32 MB; raise to 128-256 MB+ with thousands of threads), `TCMALLOC_AGGRESSIVE_DECOMMIT` | MariaDB KB: case of 10.1->10.3 upgrade OOM resolved by tcmalloc; InMotion Hosting 2025-26 fleet report: MySQL memory growth solved with tcmalloc <https://www.inmotionhosting.com/support/news/solving-mysql-memory-leaks-with-tcmalloc/> | Per-thread caches with many threads; small default thread cache limit -> central-list contention | B |
| **tcmalloc (Google, new)** | link-time (Bazel), not LD_PRELOAD-friendly | per-CPU caches need rseq; set `GLIBC_TUNABLES=glibc.pthread.rseq=0` so glibc ≥ 2.35 doesn't grab rseq first; MongoDB 8.0 pairs it with THP `always`, `defrag=defer+madvise`, `khugepaged/max_ptes_none=0` <https://www.mongodb.com/docs/manual/administration/tcmalloc-performance/> | No MySQL/MariaDB production builds use it | Transferable lesson: *THP advice depends on allocator* | C |
| **mimalloc** (Microsoft) | `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libmimalloc.so.2` | `MIMALLOC_PURGE_DELAY`, `MIMALLOC_ARENA_EAGER_COMMIT`, `MIMALLOC_ALLOW_LARGE_OS_PAGES` | No published MySQL benchmark of note; generic benchmarks competitive | Unproven for mysqld | D |

**Allocator guidance for a tuner:** detect allocator via `/proc/<pid>/maps`; signal for change = `RSS - (innodb_buffer_pool_size + sum(performance_schema.memory_summary_global_by_event_name current))` growing over days with stable connection count, or `perf top` showing `_int_malloc`/`malloc_consolidate`/`arena_get2`/`__lll_lock_wait_private` > 3-5% CPU. Apply needs mysqld restart. Risk class: availability (memory) — always A/B test RSS for ≥ 24 h.

### 5.2 Process limits (systemd)

| Setting | Default | Recommended | Why | Apply |
|---|---|---|---|---|
| `LimitNOFILE` (systemd unit) | MySQL 8 RPM unit: 10000; mysqld computes `open_files_limit` from max_connections / table_open_cache | ≥ max(10 + max_connections + 2×table_open_cache, 65535) | `open_files_limit` lower than requested -> table cache thrash, "Too many open files" | restart |
| `LimitMEMLOCK` | 8 MB (64 KB older) | `infinity` when using `large-pages` without `hugetlb_shm_group`, or `memlock` | MAP_HUGETLB permission falls back to RLIMIT_MEMLOCK check | restart |
| `LimitNPROC` / `TasksMax` | 4915 / 15% of pid_max on some distros | ≥ max_connections + background threads | "Can't create a new thread (errno 11)" at high connection counts | restart |
| `OOMScoreAdjust` | 0 | -800 | Percona checklist | restart |
| `Environment=` | — | `LD_PRELOAD=...`, `GLIBC_TUNABLES=...`, `MALLOC_CONF=...` | allocator selection | restart |

### 5.3 Explicit huge pages for the buffer pool (`large_pages=ON`)

| Item | Detail |
|---|---|
| Mechanism | MySQL ≥ 8.0.32 (and later 8.x/9.x) allocates the buffer pool with `mmap(MAP_PRIVATE\|MAP_ANONYMOUS\|MAP_HUGETLB)`; older 5.6/5.7 used SysV `shmget(SHM_HUGETLB)` with mmap fallback, which is why docs still mention `kernel.shmmax/shmall` (now irrelevant) — MySQL Bug #110404 <https://bugs.mysql.com/bug.php?id=110404>, source `large_page_alloc-linux.h` <https://dev.mysql.com/doc/dev/mysql-server/8.0.46/large__page__alloc-linux_8h_source.html>. MariaDB 10.5+ also mmap-based with multiple page sizes (`large_pages`, `large_page_size`) |
| OS config | `vm.nr_hugepages = ceil(innodb_buffer_pool_size × ~1.03 / 2 MB)` + margin (Bug #115130: typo `nr_hugepages`, need extra pages beyond BP size, e.g. 66 pages for default 128 MB <https://bugs.mysql.com/bug.php?id=115130>); allocate at boot (`hugepages=N` kernel arg or sysctl early) to avoid fragmentation preventing allocation; NUMA: per-node `/sys/devices/system/node/node*/hugepages/...` |
| Permissions (pitfall) | `mmap` with `MAP_HUGETLB` returns `EPERM`/`ENOMEM`/`EACCES` unless the process has `CAP_IPC_LOCK`, is in `vm.hugetlb_shm_group` (set to mysql's GID), or the RLIMIT_MEMLOCK allows it (deprecated path, kernel warns). Fix: `vm.hugetlb_shm_group=$(id -g mysql)` + `LimitMEMLOCK=infinity` |
| Failure mode | If allocation fails MySQL logs a warning and **silently falls back to normal pages** (check error log + `HugePages_Free` unchanged in `/proc/meminfo`). Reserved-but-unused huge pages are wasted RAM |
| Pitfalls | Huge pages aren't swappable or reclaimable; `innodb_buffer_pool_size` online resize needs free huge pages; RSS reporting excludes hugetlb (`HugetlbPages` in `/proc/<pid>/status`) — monitoring that alerts on RSS will under-report; `innodb_buffer_pool_in_core_file=OFF` interactions; not available in some containers |
| Effect size | TLB-miss reduction; reported gains 0-10% for CPU-bound read workloads with very large buffer pools (> 100 GB). Ahmed/Percona PG tests found THP no gain; explicit huge pages for PostgreSQL shared_buffers commonly 5-10% on large hosts. MySQL-specific modern measurements are scarce → **Grade C** |
| VM applicability | Works in KVM guests; benefit larger when host also backs guest with huge pages (two-dimensional page walks are expensive: guest+host TLB misses) |
| Apply | reboot (to reserve pages reliably) + mysqld restart |

---

## 6. MySQL/MariaDB variables that interact with the OS network stack

| Variable | Default | Recommended | When / signal | Interaction / evidence | Risk | Apply |
|---|---|---|---|---|---|---|
| `back_log` | MySQL 8: = max_connections (autosized); MariaDB: min(150, max_connections) | ≥ burst connects; ≤ `net.core.somaxconn` (kernel silently caps) | `ListenOverflows`; MariaDB with high max_connections still capped at 150 by default → raise explicitly | §1.1 | none | restart |
| `skip_name_resolve` | OFF | **ON** (use IP-based grants) | Connect latency spikes, `Connection_errors_*`, slow DNS, `performance_schema.host_cache` | Avoids reverse+forward DNS per new connection (each can be 10-1000+ ms or 5 s timeouts on resolver failure). Grade A (MySQL docs: "DNS lookup optimization and the host cache") | semantics (hostname grants stop working) | restart |
| `host_cache_size` | 128 + 1 per max_connections up to 653 | 0 when `skip_name_resolve=ON` | — | | none | runtime |
| `max_allowed_packet` | 64 MB (8.0+); 16 MB (MariaDB 10.2+) | ≥ largest row/blob/batch statement; 256 MB-1 GB for bulk loaders | `ER_NET_PACKET_TOO_LARGE`, replication "Got a packet bigger than" | Memory per connection grows only when needed | availability (memory) | runtime (new sessions) |
| `net_buffer_length` | 16 KB | leave | — | Grows to max_allowed_packet automatically | — | — |
| `net_read_timeout` / `net_write_timeout` | 30 / 60 s | raise (300-600) for WAN dumps / slow clients | "Lost connection to MySQL server during query" on large result sets to slow clients | Grade B | none | runtime |
| `wait_timeout` | 28800 s | < LB/NAT idle timeout or rely on keepalive; pools should validate connections | Pool errors after idle | Grade A | availability | runtime |
| `thread_cache_size` | autosized 8 + max_connections/100 (≤100) | ≥ steady new-connection rate burst, e.g. 100-500 | `Threads_created` / `Connections` > ~1% or rising per second | Avoids thread creation (~tens of µs) per connect. Grade A | none | runtime |
| Thread pool: Percona `thread_handling=pool-of-threads`, MariaDB same, MySQL Enterprise thread pool; `thread_pool_size` = cores | one-thread-per-connection (MySQL community) | enable when active (running) threads ≫ cores: hundreds-thousands of concurrent statements, many short connects | `Threads_running` ≫ cores, high context switch rate (`vmstat cs`), throughput collapse past N threads; also mitigates EEVDF regression (fewer runnable threads) | Percona 2023 (PS 8.0.34, 8 vCPU): see <https://www.percona.com/blog/is-thread-pool-plugin-the-right-choice-for-your-workload/>; Percona docs: little benefit below very high concurrency. Grade A for collapse prevention, B for absolute gain | semantics (long queries can block a group; tune `thread_pool_stall_limit`, `thread_pool_oversubscribe`) | restart |
| `innodb_thread_concurrency` | 0 | 0, or ≈ cores (Percona 2023 best = 8 on 8 vCPU at high concurrency) | as above | Grade B | none | runtime |
| Protocol compression: client `--compression-algorithms=zstd --zstd-compression-level=1..3` (MySQL 8.0.18+), server `protocol_compression_algorithms` (default `zlib,zstd,uncompressed`); replication `SOURCE_COMPRESSION_ALGORITHMS='zstd'`; `binlog_transaction_compression=ON` (8.0.20+) | uncompressed | zstd for WAN / bandwidth-limited links (cross-region replication, remote analytics pulls); off on LAN | `ss -ti` shows send buffer / bandwidth-limited, `bytes_sent` rate near link capacity, cross-region egress cost | Percona 2019 (1 Gbit link, bandwidth-bound sysbench): compression raised throughput ~2,867 -> ~5,030 at 128 threads <https://www.percona.com/blog/2019/02/19/how-network-bandwidth-affects-mysql-performance>; AWS RDS blog on connection compression <https://aws.amazon.com/blogs/database/reduce-network-transfer-time-with-connection-compression-in-amazon-rds-for-mysql-and-amazon-rds-for-mariadb>; ProxySQL 3.0.7 adds zstd (2026) <https://www.proxysql.com/blog/proxysql-3-0-7-zstd-compression>. MySQL docs <https://dev.mysql.com/doc/refman/8.4/en/connection-compression-control.html>. On LAN compression costs CPU and latency. Grade A (WAN), A (don't on LAN) | none (CPU) | client reconnect / `CHANGE REPLICATION SOURCE` |
| TLS (`require_secure_transport`, `tls_version`, `ssl_cipher`) | TLS on by default in 8.0 (auto-generated certs) | keep TLS; use pooling so the handshake is amortised; TLS 1.3 + AES-GCM (AES-NI) | Connect rate limited by handshake CPU | Percona 2013: SSL connect rates collapse vs non-SSL (2701 -> 77 conn/s at 1024-bit; old OpenSSL/RSA) <https://www.percona.com/blog/mysql-ssl-performance-overhead/> — modern ECDHE + TLS1.3 far cheaper but still ≫ plain TCP connect. Grade B | security if disabled | restart |
| Unix socket vs TCP for co-located clients | — | use the Unix socket (`localhost` in libmysqlclient) for same-host app/proxy/load generator | App/proxy on DB host | Percona (Zaitsev, Apr 2020, PS 8.0.19): TCP/IP loopback costs **~35% throughput** single-thread, ~33% at 64 threads on PK lookups; mysqldump 11% slower <https://www.percona.com/blog/need-to-connect-to-a-local-mysql-server-use-unix-domain-socket/>. Grade A | none | client config |
| `admin_address` / `admin_port` (8.0.14+) | off | enable — separate listener with its own backlog for DBA access during connection storms | max_connections exhausted | Grade B | none | restart |
| `max_connections` | 151 | sized to pool totals; not "10000 just in case" | per-thread memory, scheduler | Grade A | availability | runtime |


---

## 7. Client / application / load-generator hosts

**Headline:** On the client side, the application's connection discipline dominates every OS knob: a connection pool with prepared statements and batched writes typically gives 2-10× more than any sysctl. OS settings on clients matter for (a) ephemeral ports + TIME_WAIT with per-request connections, (b) conntrack on container hosts, (c) keepalive vs middlebox idle timeouts, (d) CPU power state / softirq on load generators so the *client* isn't the bottleneck in benchmarks, (e) Unix socket when co-located.

### 7.1 Application-level practices (largest effects)

| Practice | Effect / evidence | Grade |
|---|---|---|
| **Connection pooling** (HikariCP, Go `database/sql` `SetMaxOpenConns/SetMaxIdleConns/SetConnMaxLifetime`, SQLAlchemy pool, ProxySQL/MaxScale multiplexing) instead of connect-per-request | New MySQL 8 connection = TCP handshake + server greeting + auth (caching_sha2_password; full auth needs TLS or RSA) + TLS handshake (default on) + thread create/cache → ~1-5 ms and significant server CPU; Percona 2013 SSL connect-rate test 2,701 -> 77 conn/s (old RSA) <https://www.percona.com/blog/mysql-ssl-performance-overhead/>. Also eliminates TIME_WAIT/ephemeral port exhaustion and conntrack churn. Signal: `Connections`/s high, `Threads_created` rising, `Aborted_connects`, client `EADDRNOTAVAIL` | A |
| Pool size ≈ (cores × 2-4) per DB, not hundreds per app instance | Avoids oversubscription (EEVDF regression, context switches); HikariCP "About Pool Sizing" | B |
| **Server-side prepared statements** + statement cache (Connector/J `useServerPrepStmts=true&cachePrepStmts=true&prepStmtCacheSize=250+`; Go driver uses server-side by default; PHP PDO `ATTR_EMULATE_PREPARES=false`) | Saves parse/optimize for repeated point queries; Percona Unix-socket test used prepared PK lookups; typical 5-20% CPU on parse-heavy OLTP. Caveat: ProxySQL multiplexing disabled on some prepared-statement paths in older versions; MySQL `max_prepared_stmt_count` | B |
| **Batching**: multi-row `INSERT ... VALUES (...),(...)`; Connector/J `rewriteBatchedStatements=true`; transaction grouping | Cuts round trips (RTT × N) and commits (fsync × N). 5-50× for bulk insert over a 1 ms RTT link (common reports). | A |
| Pipelining (MariaDB Connector/J 3.x pipelines prepare+execute; MySQL X Protocol pipelining) | Saves RTTs on WAN | C |
| Compression (`zstd`) only over WAN/bandwidth-limited links | see §6 | A |
| **Unix socket** when client is co-located | ~33-35% throughput vs TCP loopback (Percona 2020) | A |
| `TCP_NODELAY` | already default in all mainstream MySQL drivers (Connector/J `tcpNoDelay=true`, libmysqlclient, Go net) | A |
| Driver keepalive / idle validation (`keepaliveTime` HikariCP 4.0+, `maxLifetime` < `wait_timeout` and < NAT/LB idle timeout) | Avoids "Communications link failure" after idle; AWS NLB 350 s idle, Azure LB 4 min default | A |
| Read/write splitting at proxy | Offloads reads; network hop adds RTT (~50-200 µs same AZ) and proxy overhead: own measurements in this lab (MaxScale ≈ 0-2% on separate host, ~11% co-located) | B |

### 7.2 Client OS settings

| Setting | Recommended on client / load generator | Signal | Grade |
|---|---|---|---|
| `net.ipv4.ip_local_port_range` | `1024 65535` (reserve listener ports) | `EADDRNOTAVAIL`, `ss -s` timewait ≈ 28k | A |
| `net.ipv4.tcp_tw_reuse` | 1 | TIME_WAIT accumulation on client | A |
| `net.netfilter.nf_conntrack_max` (or no conntrack) | ≥ 4× sockets incl. TIME_WAIT; on Kubernetes nodes consider kube-proxy IPVS/nftables or eBPF (Cilium) | `conntrack -S` drops/insert_failed, dmesg "table full" | A |
| Keepalive `tcp_keepalive_time/intvl/probes` | 60-300 / 10-30 / 3-6 | idle pooled conns dropped by middleboxes | A |
| `ulimit -n` / `LimitNOFILE` | ≥ 2× connections | `EMFILE` in sysbench/HammerDB with thousands of threads | A |
| CPU governor `performance`, C-states limited | on bare-metal load generators | Client CPU wake-up latency inflates measured latency | B |
| Client has enough CPU | keep load generator < 70% CPU; HammerDB/sysbench single-process limits | `mpstat` on client; otherwise you're benchmarking the client | A |
| Same AZ / placement group, accelerated NIC | — | RTT ≥ 0.5 ms dominates point-select latency | A |
| `tcp_slow_start_after_idle=0` | for WAN clients with long-lived pooled conns | first response after idle slow | B |
| BBR on client | when client receives large result sets over WAN (receiver doesn't choose CC — the **sender** (server) does; client BBR only matters for client→server uploads/bulk loads) | — | B |
| `tcp_rmem` max on client | ≥ BDP for large result sets over WAN | `ss -ti` rcv_space pinned | B |
| Benchmark tool specifics | sysbench: `--db-ps-mode=auto`, avoid `--reconnect` unless testing connects; HammerDB: use `-` key-and-thinking time off for throughput; go-tpc: `--conn-params` for `interpolateParams`; run client on a separate VM of same size class | — | B |

---

## 8. What the tuned profiles change (upstream tuned master, verified Oct 2026)

Source: <https://github.com/redhat-performance/tuned/tree/master/profiles>

| Profile | Inherits | CPU | VM / memory | Disk | sysctl | Boot / other |
|---|---|---|---|---|---|---|
| `throughput-performance` | — | `governor=performance`, `energy_perf_bias=performance`, `energy_performance_preference=performance`, `min_perf_pct=100`, `boost=1`; ACPI `platform_profile=performance` | `dirty_bytes=40%`, `dirty_background_bytes=10%` (ratios); THP `never` only on ThunderX aarch64 | `readahead=>4096` (KB, i.e. raise to at least 4 MB) | `vm.swappiness=10`, `net.core.somaxconn=>2048`; `kernel.numa_balancing=0` on ThunderX only | — |
| `latency-performance` | — | as above + `force_latency=cstate.id_no_zero:1\|3` (allow only C1/shallow, via `/dev/cpu_dma_latency`) | `dirty_bytes=10%`, `dirty_background_bytes=3%` | — | `vm.swappiness=10` | — |
| `network-latency` | latency-performance | — | THP `never` | — | `net.core.busy_read=50`, `net.core.busy_poll=50`, `net.ipv4.tcp_fastopen=3`, `kernel.numa_balancing=0`, `kernel.hung_task_timeout_secs=600`, `kernel.nmi_watchdog=0`, `vm.stat_interval=10`, `kernel.timer_migration=0` | cmdline `skew_tick=1 tsc=reliable rcupdate.rcu_normal_after_boot=1 rcutree.nohz_full_patience_delay=1000`; SELinux `avc_cache_threshold=8192` |
| `network-throughput` | throughput-performance | — | — | — | `net.ipv4.tcp_notsent_lowat=131072`, `net.ipv4.tcp_slow_start_after_idle=0`, `net.ipv4.tcp_rmem="4096 131072 16777216"`, `net.ipv4.tcp_wmem="4096 16384 16777216"` | summary: "generally only necessary on older CPUs or 40G+ networks" |
| `virtual-guest` | throughput-performance | — | `dirty_bytes=30%` | — | `vm.swappiness=30` | default on RHEL guests |
| **`postgresql`** (closest stock analogue for an OLTP RDBMS) | throughput-performance | `force_latency=1` (C1 max) | THP `never`; `dirty_background_bytes=67108864` (64 MB), `dirty_bytes=536870912` (512 MB) | (inherits readahead ≥4 MB) | `vm.swappiness=3`, `kernel.sched_autogroup_enabled=0`; `[scheduler] sched_min_granularity_ns=10000000`, `sched_migration_cost_ns=50000000` (no-ops on EEVDF kernels) | — |
| `oracle` | throughput-performance | — | THP `never`; `dirty_background_bytes=3%`, `dirty_bytes=40%` | — | `swappiness=10`, `dirty_expire_centisecs=500`, `dirty_writeback_centisecs=100`, `ip_local_port_range=9000 65499`, `rmem_default/wmem_default=262144`, `rmem_max=4194304`, `wmem_max=1048576`, `numa_balancing=0`, `fs.aio-max-nr=1048576`, shm/sem limits | `kernel.panic_on_oops=1` |
| `mssql` | throughput-performance | `force_latency=5` | THP **`always`**; `dirty_background_bytes=3%`, `dirty_bytes=80%` | — | `swappiness=1`, `dirty_expire_centisecs=500`, `dirty_writeback_centisecs=100`, `max_map_count=1600000`, `numa_balancing=0`, buffer sizes as oracle | scheduler latency/granularity knobs (CFS-era) |
| `aws` | throughput-performance | — | — | — | — | cmdline `nvme_core.io_timeout=4294967295 net.ifnames=0` |

Notes for a tuner:
- Older tuned versions (RHEL 7/8) also set `kernel.sched_min_granularity_ns=10000000`, `kernel.sched_wakeup_granularity_ns=15000000`, `sched_migration_cost_ns=5000000`; these sysctls don't exist on 5.13+ / EEVDF kernels and upstream removed them.
- `throughput-performance`'s 4 MB readahead is **bad for buffered random reads** (MySQL on ZFS/`fsync`, MyISAM); harmless for O_DIRECT InnoDB.
- For a MySQL host the most appropriate stock profile is `latency-performance` (+ THP never, + swappiness 1-10); on cloud VMs most CPU directives are no-ops. Red Hat performance guide: <https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/monitoring_and_managing_system_status_and_performance/getting-started-with-tuned_monitoring-and-managing-system-status-and-performance>. There is no upstream `mysql`/`mariadb` tuned profile; the upstream `postgresql` profile (C1 cap, THP never, 64 MB/512 MB dirty bytes, swappiness 3, autogroup off) is the best stock template for an InnoDB host — a custom `mysql` profile = `include=postgresql` minus the obsolete `[scheduler]` section is a reasonable baseline. `mssql` uses THP `always` (allocator-specific, not transferable).
- `busy_read/busy_poll=50` in `network-latency` costs CPU on every socket read; with mysqld's hundreds of threads this is usually a **net loss** for throughput — evaluate only for low-concurrency latency-critical setups.

---

## 9. Cloud vendor recommendations for database VMs (summary)

| Vendor | Recommendations relevant to MySQL hosts | Source |
|---|---|---|
| AWS | ENA driver up to date; consider ENA Express for same-AZ; monitor ENA allowance counters (`ethtool -S eth0 \| grep allowance`); RPS/RFS + IRQ tuning in latency guide; jumbo frames (9001) inside VPC only; clocksource `kvm-clock` on Nitro (tsc on old Xen); EBS Torn Write Prevention -> ext4 bigalloc 16K + doublewrite off; RDS "Optimized Writes"/"Optimized Reads" are the managed equivalents | <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ena-improve-network-latency-linux.html>, <https://github.com/amzn/amzn-drivers/blob/master/kernel/linux/ena/ENA_Linux_Best_Practices.rst>, <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/network_mtu.html>, <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-twp.html>, whitepaper <https://docs.aws.amazon.com/whitepapers/latest/optimizing-mysql-on-ec2-using-amazon-ebs/> |
| GCP | THP off; ext4 or XFS with `relatime,lazytime`; Hyperdisk Balanced; doublewrite off on Hyperdisk with ext4 bigalloc 16K; `innodb_flush_method=O_DIRECT_NO_FSYNC`; `innodb_numa_interleave=1` on C-series; gVNIC + Tier_1 only for very high bandwidth; MTU up to 8896 per VPC | <https://cloud.google.com/compute/docs/instances/mysql/mysql-workloads> |
| Azure | Accelerated Networking (MANA) always; MTU 9000 with MANA intra-VNet (3900 with Mellanox); Premium SSD v2 / Ultra for logs; Premium SSD host caching ReadOnly (data) / None (logs); `O_DIRECT` | <https://learn.microsoft.com/en-us/azure/virtual-network/accelerated-networking-overview>, <https://learn.microsoft.com/en-us/azure/virtual-network/how-to-virtual-machine-mtu>, <https://learn.microsoft.com/en-us/azure/virtual-machines/premium-storage-performance> |
| Akamai / Linode | multi-queue virtio-net enabled by default on 2+ vCPU Linodes deployed after June 2021 (queues = vCPUs, verify `ethtool -l eth0`); irqbalance "not required, no performance difference in testing"; Dedicated CPU plans for DBs (no CPU steal); MTU 1500; kvm-clock | <https://techdocs.akamai.com/cloud-computing/docs/multi-queue-nics> |


---

## 10. Signals -> setting decision table (for the automated tuner)

Conventions: rates are per second unless noted; "Δ" = increase over the observation window (sample counters twice, ≥ 60 s apart). Risk: N = none, A = availability, D = durability, S = security, M = semantics. Apply: R = runtime sysctl/sysfs, RS = restart mysqld, RM = remount, RB = reboot, APP = application/driver change.

| # | Signal (metric & threshold) | Where to read it | Candidate setting(s) | Side | Risk | Apply | Priority |
|---|---|---|---|---|---|---|---|
| 1 | `TcpExt.ListenOverflows` or `ListenDrops` Δ > 0 on :3306/:6033 | `nstat -az TcpExtListenOverflows`; `ss -lnt sport = :3306` (Recv-Q vs Send-Q) | raise `net.core.somaxconn` (≥ 4096) **and** `back_log`; then consider thread cache / thread pool | S | N | R + RS | high |
| 2 | `TcpExt.TCPReqQFullDrop` / `TCPReqQFullDoCookies` Δ > 0, dmesg "SYN flooding" | nstat, dmesg | `net.ipv4.tcp_max_syn_backlog` 4096-65535 | S | N | R | high |
| 3 | Client `connect()` errors `EADDRNOTAVAIL`; `ss -tan state time-wait '( dport = :3306 )' \| wc -l` > 20k | client `ss`, app logs | pooling (APP); `ip_local_port_range=1024 65535`; `tcp_tw_reuse=1`; reserve listener ports | C | N/M | R / APP | high |
| 4 | dmesg "nf_conntrack: table full"; `conntrack -S` drop/insert_failed Δ > 0; `nf_conntrack_count / nf_conntrack_max` > 0.8 | `/proc/sys/net/netfilter/nf_conntrack_count`, `conntrack -S` | raise `nf_conntrack_max` + hashsize; lower `nf_conntrack_tcp_timeout_time_wait`; NOTRACK DB port; pooling | B | N / S (notrack) | R | high |
| 5 | One CPU `%soft` > 50% while others idle; `/proc/net/softnet_stat` col3 (time_squeeze) Δ concentrated on 1 CPU | `mpstat -P ALL 1`, `/proc/softirqs` NET_RX per CPU, `/proc/interrupts` per queue | `ethtool -L combined <ncores>`; ensure IRQ spread (irqbalance); if queues < cores: RPS + RFS | S (and busy C) | N | R | high |
| 6 | `/proc/net/softnet_stat` col2 (dropped) Δ > 0 | softnet_stat | `net.core.netdev_max_backlog` 5000-30000 (+ fix #5) | S | N | R | med |
| 7 | softnet col3 (time_squeeze) Δ steadily > 0 with spread IRQs | softnet_stat | `netdev_budget=600`, `netdev_budget_usecs=4000-8000` | S | N | R | low |
| 8 | NIC drops: `ethtool -S` `rx_missed_errors`, `rx_no_buffer`, `rx_queue_N_drops` Δ > 0 | `ethtool -S` | `ethtool -G rx <max>`; more queues | S | N | R (link blip) | med |
| 9 | AWS ENA `*_allowance_exceeded` Δ > 0 | `ethtool -S eth0` | not OS-fixable: bigger instance, fewer connects (conntrack allowance), ENA Express | S | — | — | info |
| 10 | Long-RTT peer (`ss -ti` rtt > 10 ms) **and** throughput ≈ cwnd/rtt or rwnd-limited (`rcv_space` ≈ tcp_rmem max; `sndbuf_limited`) | `ss -tin dst <peer>` | `tcp_rmem`/`tcp_wmem` max ≥ 2×BDP (16-64 MB); `tcp_congestion_control=bbr` + `default_qdisc=fq` on sender; `tcp_slow_start_after_idle=0`; zstd protocol/replication compression | B (sender for CC) | N | R | med (WAN only) |
| 11 | `TcpExt.TCPTimeouts`/`TcpRetransSegs` ratio > 0.1% on LAN; p99 spikes at ~200 ms multiples | nstat, `ss -ti retrans`, latency histogram | check NIC/host drops first; per-route `rto_min 10ms` / `tcp_rto_min_us`; `tcp_mtu_probing=1` if MTU mismatch | B | A | R | med |
| 12 | Pooled connections dropped after idle ("Communications link failure", RST after N minutes) | app logs | keepalive 60-300/10-30/3-6 on clients and server; pool `maxLifetime`/`keepaliveTime` < middlebox idle; `wait_timeout` | B | N | R / APP | med |
| 13 | `Connections`/s > ~50 per core sustained or `Threads_created`/`Connections` > 1% | `SHOW GLOBAL STATUS` | pooling (APP); `thread_cache_size` ↑; thread pool | S/C | N | R / RS | high |
| 14 | `Threads_running` ≫ cores (e.g. > 4× cores) with high `cs` in vmstat and throughput dropping as concurrency rises; kernel ≥ 6.6 | status, `vmstat 1`, `perf sched latency` / `runqlat` | thread pool (Percona/MariaDB); smaller app pools; test EEVDF `NO_PLACE_LAG`,`NO_RUN_TO_PARITY`; `SCHED_BATCH` for mysqld | S | N/M | RS / R(debugfs) | high |
| 15 | Kernel upgraded across 6.6 boundary and HammerDB/sysbench throughput dropped 10-18% at users > CPUs | baseline comparison | as #14 (AWS-measured partial recovery) | S | N | R | med |
| 16 | Bare metal: `turbostat` Bzy_MHz ≪ max under load, or governor ≠ performance; high C6 residency with sub-ms query latency targets | `cpupower frequency-info`, `turbostat`, `/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` | tuned `latency-performance` or `postgresql`-like (governor performance, EPP performance, `force_latency` C1) | S (and bench C) | N | R | high (bare metal), n/a VM |
| 17 | VM: `steal` > 5% (`mpstat %steal`) | mpstat | not OS-tunable: dedicated CPU plan / bigger instance | S | — | — | info |
| 18 | `perf top` shows `read_hpet`/`xen_clocksource_read`; dmesg "clocksource tsc unstable" | perf, dmesg, `current_clocksource` | switch to `tsc` (bare metal/Xen), keep `kvm-clock` on KVM | B | A | R | high when present |
| 19 | High `%sys` with heavy syscall rate on pre-2020 Intel / Zen1-4; dedicated single-tenant host | `perf stat -e raw_syscalls:sys_enter -a`, `/sys/devices/system/cpu/vulnerabilities/*` | (opt-in only) `mitigations=off` or targeted (e.g. `spec_rstack_overflow=off`) | S | **S** | RB | low (policy-gated) |
| 20 | `perf top` `audit_*` > 2% | perf, `auditctl -l` | reduce audit syscall rules | S | S (compliance) | R | low |
| 21 | mysqld RSS − (buffer pool + P_S tracked memory) grows > 10% / day at stable load; or `perf` shows `_int_malloc`, `arena_get2`, `__lll_lock_wait_private` > 3% | `/proc/<pid>/status` VmRSS, `sys.memory_global_total`, perf | jemalloc (`LD_PRELOAD`, `background_thread:true`), or tcmalloc; with glibc try `glibc.malloc.arena_max=4`; THP `never`/`madvise` | S | A | RS | high |
| 22 | THP = `always` and (`khugepaged` CPU, `thp_collapse_alloc` high, `compact_stall` Δ > 0) or jemalloc in use | `/sys/kernel/mm/transparent_hugepage/enabled`, `/proc/vmstat` | THP `never` (or `madvise`) | S | N | R + RS | high |
| 23 | `pswpout` Δ > 0 / `si,so` > 0 while `MemAvailable` > 10% | `/proc/vmstat`, vmstat | `vm.swappiness=1..10`; NUMA: `zone_reclaim_mode=0`, `innodb_numa_interleave=ON` | S | A (if 0 without headroom) | R / RS | high |
| 24 | NUMA hosts: `numastat -p mysqld` imbalance > 20%, `numa_hint_faults` high | numastat, `/proc/vmstat` | `innodb_numa_interleave=ON`; `kernel.numa_balancing=0` | S | N | RS / R | med |
| 25 | dmesg `page allocation failure: order:0` (GFP_ATOMIC in net/driver); `allocstall` Δ > 0 | dmesg, `/proc/vmstat` | `vm.min_free_kbytes` → 1-3% RAM (cap ~2 GB); `watermark_scale_factor` 100-500 | S | A | R | med |
| 26 | `Dirty` in `/proc/meminfo` > 1 GB and periodic binlog fsync stalls (commit latency spikes every ~5-30 s) | meminfo, `iostat -x 1` write bursts, P_S `wait/io/file/sql/binlog` | `vm.dirty_background_bytes=64-256 MB`, `vm.dirty_bytes=0.5-4 GB` | S | N | R | med |
| 27 | Block device scheduler ≠ `none` on NVMe / cloud NVMe | `/sys/block/*/queue/scheduler` | `none` (udev rule) | S | N | R | med |
| 28 | `read_ahead_kb` ≥ 1024 on data volume and buffered I/O path (ZFS / `innodb_flush_method=fsync`) with rkB/s ≫ Innodb_data_read rate | sysfs, iostat, status | `read_ahead_kb` 16-128 | S | N | R | med |
| 29 | ext4 + O_DIRECT + kernel < 5.6 without `dioread_nolock`, NVMe | `/proc/mounts`, `uname -r` | add `dioread_nolock` or migrate to XFS | S | N | RM | high when present |
| 30 | Volume supports 16 KiB atomic writes (AWS EBS Nitro TWP, GCP Hyperdisk, ZFS recordsize ≥16K) and `innodb_doublewrite=ON`, write-heavy | cloud metadata, `mkfs`/`tune2fs -l` (bigalloc, cluster 16384) | `innodb_doublewrite=OFF` (+ `O_DIRECT`) — **only after verifying all preconditions** | S | **D** | RS (+ mkfs) | high (write-heavy) |
| 31 | fsync latency > 1 ms on local NVMe (`fio --fsync=1` / `ioping -W`) | fio | hardware with PLP; do **not** fake `write_cache` | S | D | — | info |
| 32 | `skip_name_resolve=OFF` and P_S `host_cache` shows DNS errors or connect latency high | P_S, status `Connection_errors_*` | `skip_name_resolve=ON` (grants by IP) | S | M | RS | high |
| 33 | Co-located client using TCP 127.0.0.1 | P_S `threads.CONNECTION_TYPE` = TCP/IP with host localhost | Unix socket | C | N | APP | high (co-located) |
| 34 | Bytes sent/received rate ≥ 70% of link bandwidth or WAN egress cost; RTT > 10 ms | status `Bytes_sent`, NIC counters | zstd protocol compression (client), `SOURCE_COMPRESSION_ALGORITHMS='zstd'`, `binlog_transaction_compression=ON` | B | N | APP / replication restart | med |
| 35 | `open_files_limit` < requested; `Opened_tables` Δ high with `table_open_cache` large | error log, status | `LimitNOFILE=65535+` | S | N | RS | med |
| 36 | `large_pages=ON` but `HugePages_Free == HugePages_Total` after start; or error log "Warning: Failed to allocate ... with large pages" | `/proc/meminfo`, error log | `vm.nr_hugepages` sizing, `vm.hugetlb_shm_group`, `LimitMEMLOCK=infinity` | S | A (reserved RAM) | RB/RS | low |

---

## 11. How to measure (counters and tools)

**Network (TCP layer)**
- `nstat -az` (deltas since last call; `-a` absolute): `TcpExtListenOverflows`, `TcpExtListenDrops`, `TcpExtTCPReqQFullDrop`, `TcpExtTCPReqQFullDoCookies`, `TcpExtSyncookiesSent`, `TcpRetransSegs`/`TcpOutSegs` (retrans ratio), `TcpExtTCPTimeouts`, `TcpExtTCPLossProbes`, `TcpExtTCPSynRetrans`, `TcpExtTCPRcvQDrop`, `TcpExtTCPBacklogDrop`, `TcpExtTCPMemoryPressures`, `TcpExtTCPAbortOnMemory`, `TcpExtTCPAbortOnTimeout`, `TcpExtTW`, `TcpExtTWRecycled` (should be 0 post-4.12), `TcpExtTCPDelivered`, `TcpExtPAWSEstabRejected`, `TcpEstabResets`, `TcpAttemptFails`. Raw source: `/proc/net/netstat`, `/proc/net/snmp`.
- `ss -s` (totals incl. timewait); `ss -lnt` (listen queues: Recv-Q = current accept queue, Send-Q = backlog); `ss -tinm dst <ip>` per-connection: `rtt:avg/var`, `rto`, `cwnd`, `ssthresh`, `retrans:cur/total`, `rcv_space`, `rcv_ssthresh`, `notsent`, `delivery_rate`, `busy/rwnd_limited/sndbuf_limited` times, `skmem` (r,rb,t,tb) buffers.
- `/proc/net/sockstat` (TCP inuse/orphan/tw/alloc/mem pages vs `tcp_mem`).
- conntrack: `/proc/sys/net/netfilter/nf_conntrack_count` vs `_max`; `conntrack -S` (found, invalid, insert_failed, drop, early_drop, search_restart).
- MySQL: `SHOW GLOBAL STATUS` `Connections`, `Threads_created`, `Threads_connected`, `Threads_running`, `Aborted_connects`, `Aborted_clients`, `Connection_errors_accept|internal|max_connections|peer_address|select|tcpwrap`, `Bytes_sent/received`; `performance_schema.host_cache`, `performance_schema.threads` (CONNECTION_TYPE).

**Packet path / softirq / NIC**
- `/proc/net/softnet_stat`: one row per CPU, hex columns: 1 processed, **2 dropped** (backlog full → `netdev_max_backlog`), **3 time_squeeze** (budget exhausted → `netdev_budget(_usecs)`), 10 received_rps, 11 flow_limit_count (newer kernels add more columns).
- `/proc/softirqs` (NET_RX/NET_TX per CPU — check balance), `/proc/interrupts` (per-queue IRQ counts: `virtio0-input.N`, `ena-…`, `mlx5_comp…`), `mpstat -P ALL 1` (`%soft`, `%irq`, `%steal`), `top -H` for `ksoftirqd/N`.
- `ethtool -l` (queues), `-g` (rings), `-c` (coalescing), `-k` (offloads), `-S` (driver stats: drops, `*_allowance_exceeded` on ENA, `rx_queue_N_*`), `-i` (driver). `ip -s -s link` (drops/overruns). `tc -s qdisc` (qdisc drops, backlog, requeues).
- `bpftrace`/BCC: `tcpretrans`, `tcpdrop`, `tcplife` (connection durations → per-request connections), `tcpconnlat`, `softirqs`, `runqlat`, `offcputime`.

**CPU / scheduler**
- `turbostat` (Bzy_MHz, C-state residency; bare metal), `cpupower frequency-info`/`idle-info`, `perf stat -a` (IPC, context switches, cpu-migrations), `perf top`/`perf record -g` (look for `_raw_spin_lock`, `native_queued_spin_lock_slowpath`, `nf_conntrack_*`, `__inet_hash_connect`/`__inet_check_established` (port search cost), `audit_*`, `read_hpet`, `_int_malloc`, `clear_page_erms` (THP zeroing), `compaction`), `perf sched latency`, `/proc/schedstat`, `/proc/pressure/{cpu,memory,io}` (PSI — excellent single-number signals), `vmstat 1` (`cs`, `in`, `r`, `si/so`), `pidstat -w -t -p $(pidof mysqld)`.
- Vulnerabilities/mitigations state: `grep . /sys/devices/system/cpu/vulnerabilities/*`.
- EEVDF features: `cat /sys/kernel/debug/sched/features` (root, debugfs).

**Memory**
- `/proc/meminfo` (`Dirty`, `Writeback`, `AnonHugePages`, `HugePages_*`, `MemAvailable`), `/proc/vmstat` (`pswpin/out`, `allocstall_*`, `compact_stall`, `thp_*`, `numa_*`, `pgscan_direct`, `kswapd`), `/proc/<pid>/status` (VmRSS, HugetlbPages), `/proc/<pid>/smaps_rollup`, `numastat -p mysqld`, `sys.memory_global_total` / P_S `memory_summary_global_by_event_name`, `/proc/<pid>/maps | grep -E 'jemalloc|tcmalloc'` (allocator check), `MALLOC_CONF=stats_print:true` (jemalloc stats at exit), `PSI memory`.

**Storage**
- `iostat -x 1` (`r_await`, `w_await`, `aqu-sz`, `rareq-sz`, `wareq-sz`; ignore `%util` on NVMe), `/sys/block/*/queue/{scheduler,nr_requests,read_ahead_kb,rq_affinity,write_cache}`, `biolatency`/`biosnoop` (BCC), `fio` (`--direct=1 --fsync=1 --bs=16k` to measure fsync/commit latency floor), `ioping`, `findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS`, `tune2fs -l` (bigalloc, cluster size), `xfs_info`, `zfs get recordsize,compression,logbias,primarycache,direct,sync`, `arcstat`/`arc_summary`; MySQL: `Innodb_data_fsyncs`, `Innodb_os_log_fsyncs`, P_S `file_summary_by_event_name` (wait/io/file/innodb/innodb_log_file, sql/binlog).

**Method (noise)** — per this lab's tuning-noise protocol: run baseline ≥ 3× to establish noise; accept an OS change only if gain > noise band, or if equal throughput at measurably lower CPU/IO per transaction. Reboot-scoped changes need A/B on two identical VMs or before/after with repeated baselines. Disable turbo / pin governor on bare metal benchmarks for repeatability; on VMs record `steal`.

---

## 12. Obsolete folklore and weak evidence (do not auto-apply)

| Claim | Status | Why |
|---|---|---|
| `net.ipv4.tcp_tw_recycle=1` | **Removed in 4.12 (2017)** | Broke NATed clients (per-host timestamp checks); sysctl no longer exists |
| `tcp_fin_timeout` shortens TIME_WAIT | **False** | TIME_WAIT is fixed at 60 s (`TCP_TIMEWAIT_LEN`); fin_timeout governs orphaned FIN_WAIT_2 only |
| Lower `tcp_max_tw_buckets` to "clean up" TIME_WAIT | Harmful | Kernel doc: must not be lowered artificially; destroys TIME_WAIT protection |
| `tcp_timestamps=0` / `tcp_sack=0` "for performance/security" | Obsolete | Breaks PAWS/tw_reuse/RTT estimation; SACK CVE fixed in 2019 |
| `tcp_fastopen=3` helps MySQL | No benefit | Server-speaks-first protocol; no MySQL client sends data in SYN |
| `kernel.sched_migration_cost_ns`, `sched_min_granularity_ns`, `sched_wakeup_granularity_ns` sysctls | Gone (5.13 moved to debugfs; EEVDF 6.6 removed most) | Still in many copy-paste guides and in tuned `postgresql`/`mssql` `[scheduler]` sections (no-ops on new kernels) |
| `elevator=deadline/noop` boot param | Obsolete since blk-mq-only (5.0) | Use sysfs/udev `none`/`mq-deadline` |
| XFS `nobarrier` / ext4 `barrier=0` "for speed" | Removed (XFS 4.19) / unsafe | Durability loss on volatile caches |
| `inode64` must be set on XFS | Default since 3.7 | No-op |
| `vm.swappiness=0` to "disable swap" | Risky | Since 3.5, 0 means OOM before swapping anonymous memory; use 1 |
| Disable GRO/TSO for lower latency | Mostly wrong | Costs CPU per byte; OLTP packets are small anyway |
| `numactl --interleave=all` needed for every MySQL | Superseded | `innodb_numa_interleave` (5.7.9+); only matters on multi-node hosts |
| Raise `net.core.rmem_default/wmem_default` for TCP | No effect for TCP | TCP uses `tcp_rmem[1]`/`tcp_wmem[1]` |
| Huge 12-16 MB *default* (`tcp_rmem[1]`) buffers (Netflix 2017 list) | Wasteful for DBs | Thousands of idle connections × large initial buffers; autotuning grows when needed |
| `mitigations=off` gives big DB gains | CPU-dependent | Up to ~10-20% on older Intel / AMD SRSO; ~0% on Zen 5 / Panther Lake (Phoronix) |
| THP helps databases | Generally no for MySQL/PG (Percona 2019); allocator-dependent (MongoDB 8 + new tcmalloc says yes) | Test, don't assume |
| `busy_poll`/`busy_read` for DB servers | Weak | Benefits single-thread low-latency apps; CPU burn with thread-per-connection |
| `MALLOC_ARENA_MAX=2` always | Weak | Reduces RSS but can add contention; jemalloc is the better-evidenced fix |

---

## 13. Key references

Kernel: ip-sysctl <https://docs.kernel.org/networking/ip-sysctl.html>; net sysctl <https://docs.kernel.org/admin-guide/sysctl/net.html>; scaling (RSS/RPS/RFS/XPS) <https://docs.kernel.org/networking/scaling.html>; tuned profiles <https://github.com/redhat-performance/tuned/tree/master/profiles>; Red Hat RHEL 9 performance docs <https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/monitoring_and_managing_system_status_and_performance/>.
Percona: OS checklist 2023 <https://www.percona.com/blog/dont-start-using-your-mysql-server-until-youve-configured-your-os/>; ext4 O_DIRECT regression 2019 <https://www.percona.com/blog/watch-out-for-disk-i-o-performance-issues-when-running-ext4/>; MySQL/ZFS update 2021 <https://www.percona.com/blog/mysql-zfs-performance-update/>; Unix socket vs TCP 2020 <https://www.percona.com/blog/need-to-connect-to-a-local-mysql-server-use-unix-domain-socket/>; network bandwidth + compression 2019 <https://www.percona.com/blog/2019/02/19/how-network-bandwidth-affects-mysql-performance>; CPU governor 2016 <https://www.percona.com/blog/cpu-governor-performance/>; Meltdown 2018 <https://www.percona.com/blog/does-the-meltdown-fix-affect-performance-for-mysql-on-bare-metal/>; THP myth 2019 <https://www.percona.com/blog/settling-the-myth-of-transparent-hugepages-for-databases/>; allocators <https://www.percona.com/blog/impact-of-memory-allocators-on-mysql-performance/>, <https://www.percona.com/blog/mysql-memory-management-memory-allocators-and-operating-system/>, <https://www.percona.com/blog/enabling-jemalloc-on-percona-server-for-mysql-5-7-and-8-0-series/>; thread pool <https://www.percona.com/blog/is-thread-pool-plugin-the-right-choice-for-your-workload/>; SSL overhead <https://www.percona.com/blog/mysql-ssl-performance-overhead/>.
Scheduler: AWS EEVDF/MySQL numbers <https://lkml.iu.edu/hypermail/linux/kernel/2411.3/01578.html>; sysctl proposal rejected <https://lkml.rescloud.iu.edu/2502.1/05730.html>.
Cloud: AWS ENA latency <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ena-improve-network-latency-linux.html>; AWS TWP <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-twp.html>; GCP MySQL workloads <https://cloud.google.com/compute/docs/instances/mysql/mysql-workloads>; Azure MTU <https://learn.microsoft.com/en-us/azure/virtual-network/how-to-virtual-machine-mtu>; Akamai multi-queue <https://techdocs.akamai.com/cloud-computing/docs/multi-queue-nics>.
Other: Gregg Netflix EC2 tunables 2017 <https://brendangregg.com/blog/2017-12-31/reinvent-netflix-ec2-tuning.html>; Cloudflare ephemeral ports 2022 <https://blog.cloudflare.com/how-to-stop-running-out-of-ephemeral-ports-and-start-to-love-long-lived-connections/>; Cloudflare TCP buffers 2022 <https://blog.cloudflare.com/optimizing-tcp-for-high-throughput-and-low-latency/>; BBR (ACM Queue 2016) <https://queue.acm.org/detail.cfm?id=3022184>; Phoronix filesystems 7.0 <https://www.phoronix.com/review/linux-70-filesystems/4>, I/O schedulers <https://www.phoronix.com/review/linux-56-nvme/2>, mitigations Zen 5 <https://www.phoronix.com/review/amd-zen5-mitigations-off>; Shattered Silicon ZFS <https://shatteredsilicon.net/mysql-mariadb-innodb-on-zfs/>; Klara ZFS direct I/O 2026 <https://klarasystems.com/articles/managing-cache-and-direct-io-for-databases-on-zfs/>; MongoDB tcmalloc/THP <https://www.mongodb.com/docs/manual/administration/tcmalloc-performance/>; jemalloc TUNING.md <https://github.com/jemalloc/jemalloc/blob/dev/TUNING.md>; glibc tunables <https://www.gnu.org/software/libc/manual/html_node/Tunables.html>; MariaDB allocators <https://mariadb.com/kb/en/using-mariadb-with-tcmalloc-or-jemalloc/>; MariaDB 10.6.5 io_uring note <https://mariadb.com/docs/release-notes/community-server/10.6/10.6.5>; MySQL Bug #100704, #110404, #115130.

## Client-side findings from the r2 connection storm (HC-6, 2026-10-23)

A 256-thread connect-per-request storm, from a 16 vCPU client to a 16 vCPU server over the public network, MySQL 8.4,
caching_sha2_password.

1. **TLS per connection saturated the client.** The client ran Ubuntu's sysbench 1.0.20. Its `--mysql-ssl=off` does not
   disable TLS: the server's `Ssl_accepts` grew with every connection. libmysqlclient with OpenSSL 3 re-reads the system
   CA bundle on every TLS connect (`PEM_X509_INFO_read_bio_ex`), and 57% of the client's CPU was spent in OpenSSL's global
   lock (`CRYPTO_THREAD_unlock`). All 16 cores were busy for about 60 new connections/s (313 queries/s).
   - sysbench 1.1 with `--mysql-ssl=DISABLED` makes no TLS handshakes; `Ssl_accepts` stayed unchanged over 5,206
     connections.
   - Without TLS, caching_sha2_password accepts a plain connection only once the server has cached the password. Log in
     once over TLS after every restart.
2. **Then ephemeral ports.** Without TLS the client reached about 2,300 connections/s and spent 97% of its CPU in the
   kernel looking for free ports. The kernel default `tcp_tw_reuse=2` reuses TIME_WAIT ports for loopback only.
   - With `tcp_tw_reuse=1`: 18,669 queries/s (about 3,700 connections/s), with the client 84% idle.
3. **Gaps in the tuner (to do):**
   - The client-saturation guard stops the session before client-side rules (client-ports) can act. It should apply an
     available client remedy and recalibrate, and stop only when none is left.
   - It needs a client CPU diagnosis: system CPU plus TIME_WAIT points to ports; user CPU in libcrypto on connect points
     to per-connection TLS (remedies: pooling, TLS session reuse; TLS off is a security trade-off).
   - The name-resolution and authentication path on the server is what HC-6 is meant to measure once the client is out of
     the way.
