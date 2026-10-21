"""Low-overhead CPU profiling of a measurement run (where does CPU go when the workload is CPU-bound?).

Mechanism: kernel perf-event sampling (the same perf_event facility eBPF profilers such as Coroot's node agent or the
OpenTelemetry eBPF profiler attach their programs to), driven by `perf record`:

* system-wide, one timer-based sampling event per CPU at a low frequency (default 49 Hz, i.e. ~50 stack samples per
  CPU per second regardless of the number of threads) for a short slice of the run (default 20 s);
* call stacks via frame pointers when the binary has them (Ubuntu / Percona builds do), so the kernel only walks a
  linked list; without frame pointers the profile is leaf-only (still enough for the category breakdown);
* symbolisation (`perf script`) runs after the measurement window has ended, at nice 19, so it never competes with
  the benchmark;
* the slice is taken at the end of the benchmark warm-up, so the measured window is never profiled.

Results are aggregated into categories that map to tuning mechanisms (spinning, allocator, performance_schema, redo,
parser/optimizer, ...), top functions, and folded stacks for a flame graph.
"""
import collections
import os
import re
import shutil
import subprocess
import threading
import time

CATEGORIES = (  # (category, regex on one frame) - frames are checked from the leaf upwards, first match wins
    ("spin / mutex wait", r"ut_delay|PolicyMutex|TTASEventMutex|spin_and_try|rw_lock_[xs]_lock_(spin|func)|sync_array|::spin|lock_word|Spin_lock"),
    ("memory allocator", r"^(malloc|free|calloc|realloc|_int_malloc|_int_free|cfree|operator new|operator delete|je_|tc_|mi_|arena_|tcache)"),
    # performance_schema: its own code only - not pfs_spawn_thread (the root of every connection thread: everything that
    # matched nothing else used to land here, 15-30% instead of 2-7%) and not the kernel's psi_* (pressure stall info)
    ("performance_schema", r"^(pfs_(?!spawn_thread)|PFS_|PSI_|insert_events_|aggregate_(thread|all)|find_or_create_digest|start_(mutex|rwlock|cond|statement|stage)_wait)|pfs_.*_v[0-9]|_noop$"),
    ("compression / checksums", r"crc32|ut_crc|zlib|deflate|inflate|lz4|zstd|buf_calc_page|page_zip"),
    ("parser / optimizer", r"MYSQLparse|my_sql_parser|THD::sql_parser|JOIN::optimize|Optimize_table_order|make_join_plan|find_best_ref|test_quick_select|Item::fix_fields|setup_fields|Query_block::prepare|bind_fields"),
    ("redo / commit", r"^log_|mtr_t::commit|mtr_commit|trx_commit|ha_commit|log_buffer|redo"),
    ("row locks", r"^lock_rec|^lock_table|lock_sys|RecLock|lock_clust|lock_sec"),
    ("buffer pool", r"buf_page_get|buf_LRU|buf_flush|buf_pool|buf_read|Buf_fetch|buf_block"),
    ("B-tree / row access", r"row_search|btr_cur|btr_pcur|page_cur|rec_get_offsets|cmp_dtuple|row_sel|row_ins|row_upd|row_purge|Row_sel|row_mysql"),
    ("table open / cache", r"open_table|Table_cache|open_tables|close_thread_tables|mysql_lock_tables|^MDL_|MDL_context|MDL_lock"),
    ("stored procedures", r"^sp_|sp_head::|sp_instr|sp_lex_instr|sp_rcontext"),
    ("charset / collation", r"my_strnxfrm|my_strnncoll|_uca_|my_ismbchar|my_well_formed|my_charpos|utf8mb4|my_mb_wc"),
    ("SQL executor", r"Iterator::Read|Query_expression::execute|do_select|evaluate_join|handler::ha_|ha_innobase::|Item_.*::val_|Field_.*::(val_|store)|Protocol_.*::(store|send)"),
    ("statement dispatch", r"dispatch_command|do_command|mysql_execute_command|Prepared_statement|launch_hook|Session_sysvars_tracker|check_event_subscribers|THD::send_|Sql_cmd_"),
    ("TLS encryption", r"^(SSL_|EVP_|ssl3_|tls13_|aesni_|gcm_|ChaCha20|Poly1305)|libssl|libcrypto"),
    ("network / protocol", r"^(vio_|net_|my_net|tcp_|sock_|inet_|__sys_(send|recv)|ip_|dev_queue|napi_|net_rx|__netif)"),
    ("disk I/O (kernel)", r"io_submit|blk_|ext4_|xfs_|btrfs_|zfs|nvme|scsi|iomap|filemap_|fsync|__x64_sys_(pwrite|pread|fdatasync)"),
    ("scheduler / futex", r"futex|schedule|__switch_to|finish_task_switch|pick_next_task|try_to_wake_up|ttwu|^psi_(group_change|task_switch|task_change|account_irqtime|flags_change)"),
    ("memory management (kernel)", r"handle_mm_fault|do_page_fault|exc_page_fault|clear_page|compact_zone|khugepaged|folio|lru_|shrink_|swap"),
)
_CAT_RE = [(c, re.compile(r)) for c, r in CATEGORIES]
_ARGS = re.compile(r"\(.*$")


def short(sym):
    """C++ symbol without argument list / offsets: keeps flame graphs readable."""
    s = sym.split("+0x")[0]
    s = _ARGS.sub("", s) if "(" in s and not s.startswith("(") else s
    return s[:120] or "[unknown]"


# thread roots: on every stack, they say nothing about where the time went
_ROOT = re.compile(r"^(pfs_spawn_thread|handle_connection|start_thread|clone3?|__clone3?|thread_start|std::thread::_State|mysqld_main|main)$")


def category(frames):
    for f in frames:  # leaf first
        if _ROOT.match(short(f)):
            continue
        for c, r in _CAT_RE:
            if r.search(f):
                return c
    return "other"


def available():
    return bool(shutil.which("perf"))


def ensure_perf(log=print):
    if available():
        return True
    if shutil.which("apt-get"):
        rel = os.uname().release
        log("installing perf (linux-tools) for CPU profiling ...")
        subprocess.run(["apt-get", "install", "-yq", f"linux-tools-{rel}", "linux-tools-common"], capture_output=True, timeout=600)
    return available()


class Profiler:
    """Background `perf record` over a slice of the run; process() afterwards."""

    def __init__(self, workdir, seconds=20, hz=49):
        self.workdir, self.seconds, self.hz = workdir, int(seconds), int(hz)
        self.data = os.path.join(workdir, "cpuprofile.data")
        self.t0 = self.t1 = None
        self.err = None
        self._th = None

    def start(self, delay=0):
        def run():
            try:
                time.sleep(delay)
                self.t0 = time.time()
                r = subprocess.run(["perf", "record", "-F", str(self.hz), "-a", "-g", "-o", self.data, "--", "sleep", str(self.seconds)],
                                   capture_output=True, text=True, timeout=self.seconds + 60)
                self.t1 = time.time()
                if r.returncode != 0:
                    # VMs without a hardware PMU: fall back to the software timer event
                    self.t0 = time.time()
                    r = subprocess.run(["perf", "record", "-e", "cpu-clock", "-F", str(self.hz), "-a", "-g", "-o", self.data, "--",
                                        "sleep", str(self.seconds)], capture_output=True, text=True, timeout=self.seconds + 60)
                    self.t1 = time.time()
                    if r.returncode != 0:
                        self.err = (r.stderr or r.stdout).strip()[-300:]
            except Exception as e:  # noqa
                self.err = str(e)
        self._th = threading.Thread(target=run, daemon=True)
        self._th.start()

    def join(self, timeout=None):
        if self._th:
            self._th.join(timeout)

    def process(self, db_pids, max_stacks=800):
        """Symbolise at low priority and aggregate. Returns a dict for the run inputs."""
        if self.err or not os.path.exists(self.data):
            return {"error": self.err or "no profile data"}
        r = subprocess.run(["nice", "-n", "19", "perf", "script", "-i", self.data, "-F", "comm,pid,tid,ip,sym,dso"],
                           capture_output=True, text=True, timeout=900)
        try:
            os.remove(self.data)
        except OSError:
            pass
        by_proc = collections.Counter()
        folded = collections.Counter()
        self_fn = collections.Counter()
        incl_fn = collections.Counter()
        cats = collections.Counter()
        total = db_total = 0
        has_fp = 0
        cur = None
        frames = []

        def flush():
            nonlocal total, db_total, has_fp
            if cur is None:
                return
            comm, pid = cur
            total += 1
            is_db = pid in db_pids  # thread names (connection, ib_log_writer, ...) differ from the process name
            name = "idle" if comm == "swapper" else ("database" if is_db else comm)
            by_proc[name] += 1
            if not is_db:
                return
            db_total += 1
            fs = [short(f) for f in frames] or ["[unknown]"]
            if len(fs) > 3:
                has_fp += 1
            self_fn[fs[0]] += 1
            for f in set(fs):
                incl_fn[f] += 1
            cats[category(fs)] += 1
            folded[";".join(reversed(fs[:48]))] += 1

        db_pids = set(db_pids or ())
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            if not line.startswith((" ", "\t")):
                flush()
                frames = []
                m = re.match(r"^\s*(\S.*?)\s+(\d+)/(\d+)\s*$", line) or re.match(r"^\s*(\S.*?)\s+(\d+)\s", line)
                if m:
                    cur = (m.group(1).strip(), int(m.group(2)))
                else:
                    cur = None
                continue
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                sym = parts[1].rsplit(" (", 1)[0]
                if sym == "[unknown]":
                    dso = parts[1].rsplit("(", 1)[-1].rstrip(")")
                    sym = f"[{os.path.basename(dso)}]"
                frames.append(sym)
        flush()
        if not total:
            return {"error": "no samples"}
        n = max(db_total, 1)
        return {
            "hz": self.hz, "seconds": self.seconds, "window": [self.t0, self.t1], "samples": total, "db_samples": db_total,
            "call_stacks": has_fp > 0.5 * n,
            "by_process": {k: round(100 * v / total, 1) for k, v in by_proc.most_common(8)},
            "categories": {k: round(100 * v / n, 1) for k, v in cats.most_common()},
            "top_self": [{"fn": k, "pct": round(100 * v / n, 2)} for k, v in self_fn.most_common(30)],
            "top_inclusive": [{"fn": k, "pct": round(100 * v / n, 2)} for k, v in incl_fn.most_common(40) if v < n],
            "folded": [[k, v] for k, v in folded.most_common(max_stacks)],
        }
