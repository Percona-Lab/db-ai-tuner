"""Off-CPU diagnostic probes (eBPF via bpftrace) over the measured window.

The CPU profile shows where mysqld burns CPU, performance_schema shows instrumented waits. Some costs are neither: a
thread blocked on a reverse DNS lookup during connect sits off-CPU in libc, outside any performance_schema wait. These
probes measure such time directly, so a change that acts on it is tried because the time is there, not because a
counter crossed a threshold.

Probes:
* name resolution (whole measured window) - libc getnameinfo / getaddrinfo / gethostbyaddr_r called by mysqld (reverse
  DNS on connect when skip_name_resolve is OFF and the host cache misses). Uprobes cost only when the function is called.
* off-CPU profile (a slice at the end of the warm-up, like the CPU profile) - every time a mysqld thread blocks, the
  time until it runs again, keyed by thread name and the kernel + user stacks where it blocked. Classified into what the
  thread waited for (a free buffer pool page, the redo flush of its commit, a page read, a row lock, a latch, the
  client, a page fault / swap-in, ...). Together with the CPU profile this accounts for all of a connection thread's
  time, including waits performance_schema does not instrument. Scheduler events are frequent, so it runs for a short
  slice outside the measured window.

Needs bpftrace and root; when unavailable the measurement is simply missing (the breakdown does not invent it).
"""
import os
import re
import shutil
import subprocess
import threading
import time

DNS_FUNCS = ("getnameinfo", "getaddrinfo", "gethostbyaddr_r")


def available():
    return bool(shutil.which("bpftrace")) and os.geteuid() == 0


def _libc(pid):
    try:
        for line in open(f"/proc/{pid}/maps"):
            p = line.split()[-1]
            if re.search(r"/libc(\.so\.6|-[\d.]+\.so)$", p):
                return p
    except OSError:
        pass
    return None


def script(pid, libc, seconds):
    probes = ", ".join(f"uprobe:{libc}:{f}" for f in DNS_FUNCS)
    rets = ", ".join(f"uretprobe:{libc}:{f}" for f in DNS_FUNCS)
    return (f"{probes} /pid == {pid}/ {{ @t0[tid] = nsecs; }}\n"
            f"{rets} /pid == {pid} && @t0[tid]/ {{ $d = nsecs - @t0[tid]; @ns[func] = sum($d); @n[func] = count(); "
            f"@mx[func] = max($d); delete(@t0[tid]); }}\n"
            f"interval:s:{int(seconds)} {{ exit(); }}\n"
            "END { clear(@t0); }\n")


_LINE = re.compile(r"^@(ns|n|mx)\[([\w]+)\]:\s*(\d+)")


def parse(out):
    res = {}
    for line in out.splitlines():
        m = _LINE.match(line.strip())
        if m:
            res.setdefault(m.group(2), {})[m.group(1)] = int(m.group(3))
    return res


class Probes:
    """Background bpftrace over the measured window; result() afterwards."""

    def __init__(self, pid, seconds):
        self.pid, self.seconds = pid, max(5, int(seconds))
        self.out = self.err = None
        self.t0 = self.t1 = None
        self._p = None
        self._th = None

    def start(self):
        libc = _libc(self.pid)
        if not libc:
            self.err = "libc not found in the mysqld memory map"
            return self
        self.t0 = time.time()
        self._p = subprocess.Popen(["nice", "-n", "10", "bpftrace", "-e", script(self.pid, libc, self.seconds)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def wait():
            try:
                self.out, err = self._p.communicate(timeout=self.seconds + 120)
                if self._p.returncode != 0:
                    self.err = (err or "").strip()[-300:]
            except Exception as e:  # noqa
                self.err = str(e)[:300]
            self.t1 = time.time()
        self._th = threading.Thread(target=wait, daemon=True)
        self._th.start()
        return self

    def stop(self):
        """End early (a run that finished before the planned window) and wait for the output."""
        if self._p and self._p.poll() is None:
            try:
                self._p.send_signal(2)  # SIGINT: bpftrace prints the maps and exits
            except OSError:
                pass
        if self._th:
            self._th.join(60)

    def result(self):
        if self.err or self.out is None:
            return {"error": self.err or "no probe output"}
        window = max(1e-6, (self.t1 or time.time()) - self.t0)
        m = parse(self.out)
        dns = {f: {"calls": v.get("n", 0), "seconds": round(v.get("ns", 0) / 1e9, 4), "max_ms": round(v.get("mx", 0) / 1e6, 2)}
               for f, v in m.items() if f in DNS_FUNCS}
        total = sum(v["seconds"] for v in dns.values())
        return {"window_s": round(window, 1), "name_resolution": {"calls": sum(v["calls"] for v in dns.values()),
                                                                  "seconds": round(total, 4), "by_function": dns,
                                                                  "threads": round(total / window, 4)}}


# ---------------- off-CPU profile ----------------
# (category, tags, regex on the user stack, regex on the kernel stack). User frames say WHY the thread blocked (the
# InnoDB / server function), kernel frames HOW (I/O, futex, page fault, socket). When both are given both must match.
PAGE_FAULT = re.compile(r"exc_page_fault|asm_exc_page_fault|handle_mm_fault|do_swap_page|swap_readpage|swapin_readahead|do_fault\b|filemap_fault")
CLIENT_USER = re.compile(r"vio_io_wait|vio_read|net_read_raw_loop|net_read_packet|my_net_read|Protocol_classic::read_packet|Protocol_classic::get_command")
CLIENT_KERNEL = re.compile(r"do_sys_poll|do_poll|__x64_sys_ppoll|__x64_sys_poll|ep_poll|sk_wait_data|tcp_recvmsg|unix_stream_read|sock_recvmsg|schedule_hrtimeout_range")
OFFCPU_CATEGORIES = (
    ("Connection setup: name resolution (DNS)", ("dns",), r"getnameinfo|getaddrinfo|gethostbyaddr|ip_to_hostname|Host_cache|hostname_cache", None),
    ("Connection setup: authentication / TLS", ("connections",), r"acl_authenticate|login_connection|check_connection|native_password|caching_sha2|sha256_password|ssl_accept|SSL_do_handshake|ssl_do|Ssl_acceptor|server_mpvio|do_auth_once", None),
    ("Waiting: free buffer pool page (LRU flush)", ("io-flush", "io"), r"buf_LRU_get_free_block|buf_LRU_get_free_only|buf_flush_single_page|buf_LRU_scan_and_free|buf_flush_free_margin", None),
    ("Waiting: redo log capacity (checkpoint)", ("redo", "io-flush"), r"log_free_check|log_wait_for_space|log_sync_flush|log_consider_checkpoint|log_wait_for_free_space|log_margin", None),
    ("Waiting: commit - binary log fsync", ("commit",), r"MYSQL_BIN_LOG::sync_binlog_file|MYSQL_BIN_LOG::flush_and_sync", None),
    ("Waiting: commit - group commit queue (waiting for the leader)", ("commit",), r"Commit_stage_manager::enroll_for|Stage_manager::enroll_for|Commit_order_manager", None),
    ("Waiting: commit - binary log group commit", ("commit",), r"MYSQL_BIN_LOG::ordered_commit|Commit_stage_manager|Stage_manager", None),
    ("Waiting: commit - redo flush", ("commit",), r"log_write_up_to|log_wait_for_flush|log_wait_for_write|log_flush_low|trx_flush_log_if_needed|innobase_flush_logs|Log_file_handle::fsync", None),
    ("Waiting: row lock", ("locks",), r"lock_wait_suspend_thread|lock_wait\b|lock_wait_|lock_rec_enqueue_waiting|lock_table_enqueue_waiting", None),
    ("Waiting: metadata / table lock", ("locks",), r"MDL_context::acquire_lock|MDL_wait::timed_wait|thr_lock|wait_for_lock", None),
    ("Waiting: InnoDB concurrency queue", ("concurrency",), r"srv_conc_enter_innodb|srv_conc_enter|innobase_srv_conc", None),
    ("Waiting: data page read", ("io-read", "io"), r"buf_read_page|buf_page_get|Buf_fetch|buf_wait_for_read|fil_io|os_aio|os_file_read|buf_read_ahead|btr_cur_search_to_nth_level|row_search|btr_", r"io_schedule|blk_|submit_bio|ext4_|xfs_|filemap_read|folio_wait"),
    ("Waiting: data file write / fsync", ("io-flush", "io"), r"fil_flush|Fil_shard::flush|buf_dblwr|Double_write|fil_write|buf_flush_write|buf_flush_page", None),
    ("Waiting: latch (mutex / rw-lock)", ("concurrency",), r"rw_lock_|sync_array|os_event_wait|PolicyMutex|TTASEventMutex|ib_mutex|mutex_enter|Mutex|rwlock|Block_lock|sx_lock|::lock\b|lock_sys|Lock_mutex", r"futex"),
    ("Waiting: temporary file / sort I/O", ("io",), r"filesort|Filesort|temptable|create_ondisk|MY_TMP|my_write|my_read|my_pwrite|my_pread", r"io_schedule|blk_|ext4_|xfs_"),
    ("Waiting: async I/O completion (I/O threads)", ("io",), r"LinuxAIOHandler|os_aio_handler|fil_aio_wait|io_getevents", None),
)
KERNEL_FALLBACK = (
    ("Waiting: disk I/O (other)", ("io",), re.compile(r"io_schedule|blk_mq|submit_bio|jbd2|ext4_sync|xfs_file_fsync|file_write_and_wait|folio_wait|__x64_sys_(fsync|fdatasync|pwrite|pread)")),
    ("Waiting: other futex / condition", ("concurrency",), re.compile(r"futex")),
)
_OFF_RE = [(c, t, re.compile(u), re.compile(k) if k else None) for c, t, u, k in OFFCPU_CATEGORIES]


def offcpu_category(ustack, kstack):
    """What a blocked thread waited for: the category of the frame nearest to the leaf that says why (a redo fsync inside
    the binlog group commit is a redo flush wait; waiting for the group leader is a group commit wait)."""
    if PAGE_FAULT.search(kstack):
        return "Waiting: page fault / swap-in", ("memory", "swap")
    frames = ustack.splitlines()
    if CLIENT_USER.search("\n".join(frames[:8])):  # the network read path sits right at the leaf
        return "Waiting: client (next statement)", ("network",)
    best = None
    for n, (cat, tags, ur, kr) in enumerate(_OFF_RE):
        if kr is not None and not kr.search(kstack):
            continue
        idx = next((i for i, f in enumerate(frames) if ur.search(f)), None)
        if idx is not None and (best is None or (idx, n) < best[0]):
            best = ((idx, n), cat, tags)
    if best:
        return best[1], best[2]
    if CLIENT_KERNEL.search(kstack):
        return "Waiting: client (next statement)", ("network",)
    for cat, tags, kr in KERNEL_FALLBACK:
        if kr.search(kstack):
            return cat, tags
    return "Waiting: other", ()


def offcpu_script(pid, seconds):
    # finish_task_switch(prev) runs in the context of the task being switched in: its stacks are where it blocked
    return (f"kprobe:finish_task_switch* {{ $prev = (struct task_struct *)arg0; "
            f"if ($prev->tgid == {pid} && $prev->__state != 0) {{ @start[$prev->pid] = nsecs; }} "
            f"if (pid == {pid}) {{ $s = @start[tid]; if ($s) {{ @off[comm, kstack(8), ustack(24)] = sum(nsecs - $s); delete(@start[tid]); }} }} }}\n"
            f"interval:s:{int(seconds)} {{ exit(); }}\n"
            "END { clear(@start); }\n")


FOREGROUND = ("connection", "mysqld", "tp_worker", "thread_pool", "one_connection", "my-thread")


_OFF_KEY = re.compile(r"^@off\[(.*?),\s*$")


def parse_offcpu(out):
    """bpftrace map dump -> [(comm, kstack text, ustack text, ns)]"""
    res = []
    lines = out.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@off["):
            comm = line[5:].split(",")[0].strip()
            kst, ust, part = [], [], 0
            i += 1
            while i < len(lines) and not lines[i].startswith("]:"):
                l = lines[i].strip()
                if l == "," or l.endswith(", ") or l == "":
                    part += 1 if l == "," else 0
                elif part == 0:
                    kst.append(l)
                else:
                    ust.append(l)
                i += 1
            if i < len(lines):
                try:
                    res.append((comm, "\n".join(kst), "\n".join(ust), int(lines[i][2:].strip())))
                except ValueError:
                    pass
        i += 1
    return res


class OffCpu:
    """Background bpftrace off-CPU profile over a slice; result() afterwards."""

    def __init__(self, pid, seconds=15, cache_dir="/tmp"):
        self.pid, self.seconds, self.cache_dir = pid, max(3, int(seconds)), cache_dir
        self.out = self.err = None
        self.t0 = self.t1 = None
        self._th = None

    def start(self, delay=0):
        def run():
            try:
                time.sleep(delay)
                self.t0 = time.time()
                # nice: bpftrace prints (symbolises) its maps at exit - keep that off the database's CPUs' priority
                r = subprocess.run(["nice", "-n", "19", "bpftrace", "-e", offcpu_script(self.pid, self.seconds)], capture_output=True, text=True,
                                   timeout=self.seconds + 600)
                self.t1 = time.time()
                if r.returncode != 0 or "@off[" not in r.stdout:
                    self.err = (r.stderr or "no off-CPU events").strip()[-300:]
                self.out = r.stdout
            except Exception as e:  # noqa
                self.err = str(e)[:300]
        self._th = threading.Thread(target=run, daemon=True)
        self._th.start()
        return self

    def join(self, timeout=None):
        if self._th:
            self._th.join(timeout)

    def result(self, top=12):
        if self.err and not self.out:
            return {"error": self.err}
        window = max(1e-6, (self.t1 or time.time()) - (self.t0 or time.time()))
        cats = {}
        stacks = []
        try:
            sym = Symbolizer(self.pid, self.cache_dir)
        except Exception:  # noqa - unresolved frames only make the classification coarser
            sym = None
        for comm, kst, ust, ns in parse_offcpu(self.out or ""):
            if sym:
                ust = sym.stack(ust)
            fg = comm.startswith(FOREGROUND)
            cat, tags = offcpu_category(ust, kst)
            key = ("foreground" if fg else "background", cat)
            c = cats.setdefault(key, {"seconds": 0.0, "tags": list(tags)})
            c["seconds"] += ns / 1e9
            if fg:
                stacks.append((ns, cat, _frames(ust, 6), _frames(kst, 3)))
        stacks.sort(reverse=True)
        out = {"window_s": round(window, 1), "foreground": {}, "background": {},
               "top_stacks": [{"category": c, "seconds": round(ns / 1e9, 3), "user": u, "kernel": k} for ns, c, u, k in stacks[:top]]}
        for (side, cat), c in sorted(cats.items(), key=lambda x: -x[1]["seconds"]):
            out[side][cat] = {"seconds": round(c["seconds"], 3), "threads": round(c["seconds"] / window, 3), "tags": c["tags"]}
        return out


def _frames(stack, n):
    fr = []
    for l in stack.splitlines():
        f = re.sub(r"\+\d+$", "", l.strip())
        f = re.sub(r"\(.*$", "", f)
        if f and f not in fr:
            fr.append(f[:80])
        if len(fr) >= n:
            break
    return fr


# ---------------- symbols ----------------
class Symbolizer:
    """Resolves raw user addresses of the mysqld binary through its separate debug file (/usr/lib/debug/.build-id), which
    bpftrace does not read. The symbol table is extracted once per build and cached next to the agent state."""

    def __init__(self, pid, cache_dir):
        self.ranges, self.addrs, self.names = [], [], []
        exe = os.path.realpath(f"/proc/{pid}/exe")
        try:
            for line in open(f"/proc/{pid}/maps"):
                f = line.split()
                if len(f) >= 6 and os.path.realpath(f[5]) == exe:
                    lo, hi = (int(x, 16) for x in f[0].split("-"))
                    self.ranges.append((lo, hi, int(f[2], 16)))
        except OSError:
            return
        if not self.ranges:
            return
        self.base = min(lo - off for lo, hi, off in self.ranges)
        bid = _build_id(exe)
        dbg = f"/usr/lib/debug/.build-id/{bid[:2]}/{bid[2:]}.debug" if bid else None
        src = dbg if dbg and os.path.exists(dbg) else exe
        cache = os.path.join(cache_dir, f"syms-{bid or 'nobuildid'}.tsv")
        if not os.path.exists(cache):
            r = subprocess.run(["nice", "-n", "19", "nm", "-n", "-C", "--defined-only", src], capture_output=True, text=True, timeout=1800)
            rows = []
            for l in r.stdout.splitlines():
                parts = l.split(" ", 2)
                if len(parts) == 3 and parts[1] in "tTwW":
                    rows.append(f"{parts[0]}\t{parts[2]}")
            if rows:
                tmp = cache + ".tmp"
                with open(tmp, "w") as fh:
                    fh.write("\n".join(rows))
                os.replace(tmp, cache)
        try:
            for l in open(cache):
                a, _, n = l.rstrip("\n").partition("\t")
                self.addrs.append(int(a, 16))
                self.names.append(n)
        except OSError:
            pass

    def resolve(self, frame):
        f = frame.strip()
        if not f.startswith("0x") or not self.addrs:
            return f
        try:
            a = int(f.split()[0], 16)
        except ValueError:
            return f
        if not any(lo <= a < hi for lo, hi, _ in self.ranges):
            return f
        import bisect
        i = bisect.bisect_right(self.addrs, a - self.base) - 1
        return self.names[i] if i >= 0 else f

    def stack(self, text):
        return "\n".join(self.resolve(l) for l in text.splitlines())


def _build_id(path):
    try:
        out = subprocess.run(["readelf", "-n", path], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"Build ID:\s*([0-9a-f]+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


DBG_PACKAGES = (("percona-server-server", "percona-server-dbg"), ("mysql-server-core", "mysql-server-core-dbgsym"),
                ("mariadb-server-core", "mariadb-server-core-dbgsym"))


def ensure_debug_symbols(log=print, min_free_gb=20):
    """Debug symbols for mysqld (internal InnoDB functions in CPU / off-CPU profiles). Installed once when the package is
    available and the root disk keeps min_free_gb free; otherwise profiles use the exported symbols only."""
    exe = shutil.which("mysqld") or "/usr/sbin/mysqld"
    bid = _build_id(exe) if os.path.exists(exe) else None
    if not bid or os.path.exists(f"/usr/lib/debug/.build-id/{bid[:2]}/{bid[2:]}.debug") or not shutil.which("apt-get"):
        return bool(bid)
    st = os.statvfs("/")
    if st.f_bavail * st.f_frsize < min_free_gb * 2 ** 30:
        log(f"debug symbols not installed: less than {min_free_gb} GB free on /")
        return False
    inst = subprocess.run(["dpkg-query", "-W", "-f", "${Package}\\n"], capture_output=True, text=True).stdout
    for srv, dbg in DBG_PACKAGES:
        if any(l.startswith(srv) for l in inst.splitlines()):
            log(f"installing {dbg} (mysqld debug symbols for the CPU / off-CPU profiles) ...")
            r = subprocess.run(["apt-get", "install", "-yq", dbg], capture_output=True, text=True, timeout=1800,
                               env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
            if r.returncode != 0:
                log(f"   {dbg} not installed: {(r.stderr or r.stdout).strip()[-200:]}")
            return r.returncode == 0
    return False


# ---------------- block I/O latency (tail) ----------------
# Device service time of every block request (issue -> complete), as a log2 histogram per device and operation (R read,
# W write, F flush / FUA, D discard). Averages hide stalls: a write path with p50 0.1 ms and p99.9 40 ms (e.g. stalled
# log writes, SSD garbage collection, cloud volume throttling) makes commit latency and throughput unstable.
BLOCK_SCRIPT = (
    "tracepoint:block:block_rq_issue {{ @s[args->dev, args->sector] = nsecs; }}\n"
    "tracepoint:block:block_rq_complete /@s[args->dev, args->sector]/ {{ $us = (nsecs - @s[args->dev, args->sector]) / 1000; "
    "$op = args->rwbs; @h[args->dev, $op] = hist($us); @mx[args->dev, $op] = max($us); delete(@s[args->dev, args->sector]); }}\n"
    "interval:s:{secs} {{ exit(); }}\n"
    "END {{ clear(@s); }}\n")
_UNIT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
_BUCKET = re.compile(r"^\[(\d+)([KMG]?),\s*(\d+)([KMG]?)\)\s+(\d+)")
_OPS = {"R": "read", "W": "write", "D": "discard", "N": "other"}


def _op(rwbs):
    """rwbs flags -> operation: a leading F is a cache flush (alone, or before a write: FWS), a trailing F is FUA."""
    if not rwbs:
        return "other"
    if rwbs[0] == "F":
        return "flush" if len(rwbs) == 1 or rwbs[1] not in "RW" else "flush+write"
    return _OPS.get(rwbs[0], rwbs)


def parse_block(out):
    """{(dev, op): {"buckets": [(lo_us, hi_us, count)], "max_us": n}}"""
    res, key = {}, None
    for line in out.splitlines():
        m = re.match(r"^@(h|mx)\[(\d+), ([A-Z]*)\]:\s*(\d+)?", line)
        if m:
            k = (int(m.group(2)), m.group(3))
            if m.group(1) == "mx":
                res.setdefault(k, {"buckets": []})["max_us"] = int(m.group(4) or 0)
                key = None
            else:
                key = k
                res.setdefault(k, {"buckets": []})
            continue
        b = _BUCKET.match(line.strip())
        if b and key is not None:
            lo = int(b.group(1)) * _UNIT[b.group(2)]
            hi = int(b.group(3)) * _UNIT[b.group(4)]
            res[key]["buckets"].append((lo, hi, int(b.group(5))))
    return res


def _quantile(buckets, q):
    n = sum(c for _, _, c in buckets)
    if not n:
        return None
    acc = 0
    for lo, hi, c in buckets:
        acc += c
        if acc >= q * n:
            return hi  # upper bound of the log2 bucket (pessimistic within 2x)
    return buckets[-1][1]


def _devname(dev):
    maj, mn = dev >> 20, dev & 0xFFFFF
    try:
        for line in open(f"/sys/dev/block/{maj}:{mn}/uevent"):
            if line.startswith("DEVNAME="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return f"{maj}:{mn}"


class BlockLatency:
    """Background bpftrace block latency histogram over the measured window; result() afterwards."""

    def __init__(self, seconds):
        self.seconds = max(5, int(seconds))
        self.out = self.err = None
        self._p = self._th = None

    def start(self):
        self._p = subprocess.Popen(["nice", "-n", "10", "bpftrace", "-e", BLOCK_SCRIPT.format(secs=self.seconds)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def wait():
            try:
                self.out, err = self._p.communicate(timeout=self.seconds + 120)
                if self._p.returncode != 0 and not self.out:
                    self.err = (err or "").strip()[-300:]
            except Exception as e:  # noqa
                self.err = str(e)[:300]
        self._th = threading.Thread(target=wait, daemon=True)
        self._th.start()
        return self

    def stop(self):
        if self._p and self._p.poll() is None:
            try:
                self._p.send_signal(2)
            except OSError:
                pass
        if self._th:
            self._th.join(60)

    def result(self):
        if self.err or not self.out:
            return {"error": self.err or "no block I/O traced"}
        out = {}
        for (dev, op), v in parse_block(self.out).items():
            n = sum(c for _, _, c in v["buckets"])
            if not n:
                continue
            name = f"{_devname(dev)}:{_op(op)}"
            prev = out.get(name)
            if prev:  # several rwbs variants (WS, W, WM...) of one operation: merge the histograms
                v = {"buckets": prev["_b"] + v["buckets"], "max_us": max(prev["max_us"] or 0, v.get("max_us") or 0)}
                n = sum(c for _, _, c in v["buckets"])
            v["buckets"] = _merge(v["buckets"])
            out[name] = {"_b": v["buckets"], "count": n, "p50_us": _quantile(v["buckets"], 0.5), "p99_us": _quantile(v["buckets"], 0.99),
                         "p999_us": _quantile(v["buckets"], 0.999), "max_us": v.get("max_us"),
                         "slow_10ms": sum(c for lo, _, c in v["buckets"] if lo >= 8192)}
        for x in out.values():
            x.pop("_b", None)
        return {"by_device_op": out}


def _merge(buckets):
    acc = {}
    for lo, hi, c in buckets:
        acc[(lo, hi)] = acc.get((lo, hi), 0) + c
    return sorted((lo, hi, c) for (lo, hi), c in acc.items())
