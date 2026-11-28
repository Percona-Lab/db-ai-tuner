#!/usr/bin/env python3
"""v2 runner (zram/zswap study, 2026-11-28): one benchmark point per call, on the /opt/ps97 instance (Percona Server
9.7 with InnoDB, MyRocks, TidesDB). Root. Protocol: zram-zswap-db-benchmark/v2/PROTOCOL.md.

  run.py load  --engine E --rows N [--pct 10]          load 16 tables into a fresh datadir, keep a pristine copy
  run.py point --engine E --mode M --mem G [--knob K] --rows N --pattern P [--tag T] [--threads 64]
        engine: innodb | rocksdb | tidesdb      mode: normal | zram | zramdisk | zswap | zswapnos
        mem: buffer pool (innodb) or block cache (rocksdb, tidesdb), GB
        knob: zram device GB (zram/zramdisk) or zswap max_pool_percent (zswap*)
        pattern: uniform | zipf08 | zipf10
Each point: disk check (fio) -> swap setup -> reset from the pristine copy -> server start -> sysbench with an adaptive
warm-up (min/max, steady tps and a full cache) -> measured window -> results (JSON line in OUT/results.jsonl).
A memory-pressure watchdog (PSI "full" avg10 > 50% for 60 s) aborts the point as a "stall".
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, threading, time

PS = "/opt/ps97"
CFG = dict(datadir=os.environ.get("V2_DATADIR", "/data/v2/data"),          # data disk
           base=os.environ.get("V2_BASE", "/var/lib/v2base"),              # OS disk: pristine copies
           swapfile=os.environ.get("V2_SWAPFILE", "/data/v2/swap.img"),     # disk swap lives on the data disk
           run=os.environ.get("V2_RUN", "/data/v2/run"),
           out=os.environ.get("V2_OUT", "/root/v2/out"),
           datadev=os.environ.get("V2_DATADEV", "sda"))
SB_LUA = "/root/bench/sysbench-z"
WARM_MIN, WARM_MAX, MEASURE = int(os.environ.get("V2_WARM_MIN", 600)), int(os.environ.get("V2_WARM_MAX", 1800)), int(os.environ.get("V2_MEASURE", 900))


def sh(cmd, check=True, **kw):
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True, **kw)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}: {r.stderr.strip()[:500]}")
    return r.stdout


def wr(path, val):
    with open(path, "w") as f:
        f.write(str(val))


def log(msg):
    line = f"{time.strftime('%FT%T', time.gmtime())} {msg}"
    print(line, flush=True)
    with open(os.path.join(CFG["out"], "run.log"), "a") as f:
        f.write(line + "\n")


# ---------- server ----------
def cnf(engine, mem_gb):
    c = f"""[mysqld]
basedir = {PS}
datadir = {CFG['datadir']}
port = 3307
socket = {CFG['run']}/mysqld.sock
pid-file = {CFG['run']}/mysqld.pid
log-error = {CFG['run']}/mysqld.err
user = mysql
mysqlx = OFF
max_connections = 300
open_files_limit = 65535
plugin-load-add = ha_rocksdb.so
plugin-load-add = ha_tidesdb.so
# binary log on for every engine (TidesDB hangs without it), not synced, purged quickly
log_bin = {CFG['run']}/binlog
sync_binlog = 0
binlog_format = ROW
binlog_expire_logs_seconds = 60
max_binlog_size = 256M
innodb_redo_log_capacity = 8G
innodb_io_capacity = 2000
innodb_io_capacity_max = 4000
innodb_buffer_pool_instances = 8
innodb_buffer_pool_load_at_startup = OFF
innodb_buffer_pool_dump_at_shutdown = OFF
"""
    if engine == "innodb":
        c += f"innodb_buffer_pool_size = {mem_gb}G\n"
    else:
        c += "innodb_buffer_pool_size = 1G\n"
    if engine == "rocksdb":
        c += f"loose_rocksdb_block_cache_size = {mem_gb}G\nloose_rocksdb_default_cf_options = compression=kLZ4Compression;bottommost_compression=kLZ4Compression\n"
    if engine == "tidesdb":
        c += f"loose_tidesdb_block_cache_size = {int(mem_gb * 2**30)}\n"
        # default 256 open SSTables: once a data set has more files, readers and compaction deadlock on the fd budget
        c += "loose_tidesdb_max_open_sstables = 16384\n"
    c += f"[client]\nsocket = {CFG['run']}/mysqld.sock\n"
    p = os.path.join(CFG["run"], "my.cnf")
    wr(p, c)
    return p


def mysql(q, cnf_path):
    return sh(f"{PS}/bin/mysql --defaults-file={cnf_path} -uroot -N -e \"{q}\"")


def stop_server(cnf_path):
    pidf = os.path.join(CFG["run"], "mysqld.pid")
    if not os.path.exists(pidf):
        return
    pid = open(pidf).read().strip()
    sh(f"timeout 600 {PS}/bin/mysqladmin --defaults-file={cnf_path} -uroot shutdown", check=False)
    for _ in range(300):
        if not os.path.exists(f"/proc/{pid}"):
            break
        time.sleep(2)
    else:
        sh(f"kill -9 {pid}", check=False)
    if os.path.exists(pidf):
        os.remove(pidf)


def start_server(cnf_path):
    sh(f"{PS}/bin/mysqld --defaults-file={cnf_path} --daemonize", check=False)
    for _ in range(600):
        if subprocess.run(f"{PS}/bin/mysqladmin --defaults-file={cnf_path} -uroot ping", shell=True, capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("mysqld did not start: " + sh(f"tail -5 {CFG['run']}/mysqld.err", check=False))


# ---------- swap modes ----------
def swap_mode(mode, knob):
    sh("swapoff -a", check=False)
    if os.path.exists("/dev/zram0"):
        sh("swapoff /dev/zram0", check=False)
        wr("/sys/block/zram0/reset", 1)
    z = "/sys/module/zswap/parameters/"
    wr(z + "enabled", "N")
    if not os.path.exists(CFG["swapfile"]):
        sh(f"fallocate -l 64G {CFG['swapfile']} && chmod 600 {CFG['swapfile']} && mkswap -q {CFG['swapfile']}")
    if mode == "normal":
        sh("sysctl -q vm.swappiness=60 vm.page-cluster=3 vm.watermark_boost_factor=15000 vm.watermark_scale_factor=10")
        sh(f"swapon {CFG['swapfile']}")          # protection, on the data disk
        return
    sh("sysctl -q vm.page-cluster=0 vm.watermark_boost_factor=0 vm.watermark_scale_factor=125")
    if mode in ("zram", "zramdisk"):
        sh("sysctl -q vm.swappiness=180; modprobe zram num_devices=1")
        wr("/sys/block/zram0/comp_algorithm", "lz4")
        wr("/sys/block/zram0/disksize", f"{knob}G")
        sh("mkswap -q /dev/zram0 && swapon -p 100 /dev/zram0")
        if mode == "zramdisk":
            sh(f"swapon -p 10 {CFG['swapfile']}")
    elif mode in ("zswap", "zswapnos"):
        sh("sysctl -q vm.swappiness=100")
        for k, v in (("compressor", "lz4"), ("zpool", "zsmalloc"), ("max_pool_percent", knob),
                     ("shrinker_enabled", "N" if mode == "zswapnos" else "Y"), ("enabled", "Y")):
            wr(z + k, v)
        sh(f"swapon {CFG['swapfile']}")
    else:
        raise ValueError(mode)


# ---------- measurements ----------
def fio_check():
    f = os.path.join(os.path.dirname(CFG["datadir"]), "fio.tmp")
    out = sh(f"fio --name=chk --filename={f} --size=2G --rw=randwrite --bs=16k --direct=1 --ioengine=libaio --iodepth=16 "
             f"--runtime=30 --time_based --output-format=json", check=False)
    try:
        j = json.loads(out)["jobs"][0]["write"]
        os.remove(f)
        return {"iops": round(j["iops"]), "lat_p50_us": round(j["clat_ns"]["percentile"]["50.000000"] / 1000),
                "lat_p99_us": round(j["clat_ns"]["percentile"]["99.000000"] / 1000)}
    except Exception as e:
        return {"error": str(e)[:200]}


def snap(cnf_path):
    s = {}
    for line in open("/proc/vmstat"):
        k, v = line.split()
        if k in ("pswpin", "pswpout", "zswpin", "zswpout", "zswpwb", "pgmajfault", "pgfault"):
            s[k] = int(v)
    mi = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    s.update(mem_avail_kb=mi["MemAvailable"], swap_used_kb=mi["SwapTotal"] - mi["SwapFree"], zswap_kb=mi.get("Zswap"), zswapped_kb=mi.get("Zswapped"))
    try:
        s["zram_mm_stat"] = open("/sys/block/zram0/mm_stat").read().split()
    except OSError:
        pass
    try:
        st = sh(f"{PS}/bin/mysql --defaults-file={cnf_path} -uroot -N -e \"SHOW GLOBAL STATUS WHERE variable_name IN "
                f"('Innodb_buffer_pool_pages_free','Innodb_buffer_pool_pages_total','Innodb_data_reads','Innodb_data_writes')\"", check=False)
        s["status"] = {l.split()[0]: int(l.split()[1]) for l in st.splitlines() if l.strip()}
    except Exception:
        pass
    try:
        pid = open(os.path.join(CFG["run"], "mysqld.pid")).read().strip()
        p = {l.split(":")[0]: int(l.split()[1]) for l in open(f"/proc/{pid}/status") if l.startswith(("VmRSS", "VmSwap"))}
        s["mysqld_rss_kb"], s["mysqld_swap_kb"] = p.get("VmRSS"), p.get("VmSwap")
    except Exception:
        pass
    s["t"] = time.time()
    return s


class Sampler(threading.Thread):
    """Every 5 s: PSI memory, CPU, disk; flags a stall (PSI full avg10 > 50 for 60 s)."""
    def __init__(self):
        super().__init__(daemon=True)
        self.rows, self.stall, self.stop_ev = [], False, threading.Event()

    def run(self):
        high_since = None
        prev_cpu = prev_disk = None
        while not self.stop_ev.wait(5):
            psi = {}
            for l in open("/proc/pressure/memory"):
                f = l.split()
                psi[f[0]] = float(f[1].split("=")[1])
            cpu = [int(x) for x in open("/proc/stat").readline().split()[1:8]]
            disk = next((l.split() for l in open("/proc/diskstats") if l.split()[2] == CFG["datadev"]), None)
            row = {"t": time.time(), "psi_some": psi.get("some"), "psi_full": psi.get("full")}
            if prev_cpu:
                d = [a - b for a, b in zip(cpu, prev_cpu)]
                tot = sum(d) or 1
                row.update(us=round(100 * (d[0] + d[1]) / tot, 1), sy=round(100 * (d[2] + d[5] + d[6]) / tot, 1), wa=round(100 * d[4] / tot, 1))
            if disk and prev_disk:
                dt = 5.0
                reads, writes = int(disk[3]) - int(prev_disk[3]), int(disk[7]) - int(prev_disk[7])
                wms = int(disk[10]) - int(prev_disk[10])
                row.update(r_s=round(reads / dt), w_s=round(writes / dt), w_await_ms=round(wms / writes, 2) if writes else None)
            prev_cpu, prev_disk = cpu, disk
            self.rows.append(row)
            if (psi.get("full") or 0) > 50:
                high_since = high_since or time.time()
                if time.time() - high_since >= 60:
                    self.stall = True
            else:
                high_since = None


def sysbench_cmd(rows, pattern, threads, secs, cnf_path):
    rt = {"uniform": "--rand-type=uniform", "zipf08": "--rand-type=zipfian --rand-zipfian-exp=0.8",
          "zipf10": "--rand-type=zipfian --rand-zipfian-exp=1.0"}[pattern]
    return (f"LUA_PATH='{SB_LUA}/?.lua;;' sysbench {SB_LUA}/oltp_read_write.lua --mysql-user=sbtest --mysql-password=sbtest "
            f"--mysql-socket={CFG['run']}/mysqld.sock --mysql-db=sbtest --tables=16 --table-size={rows} --data-random-pct=10 "
            f"{rt} --threads={threads} --time={secs} --report-interval=10 --mysql-ignore-errors=all run")


def point(a):
    os.makedirs(CFG["out"], exist_ok=True); os.makedirs(CFG["run"], exist_ok=True)
    pid = f"{a.engine}-{a.mode}-{a.mem}" + (f"-k{a.knob}" if a.knob else "") + f"-{a.pattern}-r{a.rows // 1000000}m" + (f"-{a.tag}" if a.tag else "")
    base = os.path.join(CFG["base"], f"{a.engine}-r{a.rows // 1000000}m")
    if not os.path.isdir(base):
        raise SystemExit(f"no pristine copy {base}: run load first")
    r = {"id": pid, "engine": a.engine, "mode": a.mode, "mem_gb": a.mem, "knob": a.knob, "rows": a.rows, "pattern": a.pattern,
         "threads": a.threads, "host": os.uname().nodename, "kernel": os.uname().release}
    cnf_path = cnf(a.engine, a.mem)
    stop_server(cnf_path)
    r["fio"] = fio_check()
    swap_mode(a.mode, a.knob)
    t0 = time.time()
    sh(f"rsync -a --delete {base}/ {CFG['datadir']}/")
    sh(f"rm -f {CFG['run']}/binlog.*; sync; echo 3 > /proc/sys/vm/drop_caches")
    r["reset_s"] = round(time.time() - t0)
    start_server(cnf_path)
    samp = Sampler(); samp.start()
    s_start = snap(cnf_path)
    sb = subprocess.Popen(sysbench_cmd(a.rows, a.pattern, a.threads, WARM_MAX + MEASURE, cnf_path), shell=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
    iv, warm_end, s0, outcome = [], None, None, "ok"
    logf = open(os.path.join(CFG["out"], f"sb-{pid}.log"), "w")
    for line in sb.stdout:
        logf.write(line)
        m = re.search(r"\[ (\d+)s \].*?tps: ([\d.]+).*?lat \(ms,95%\): ([\d.]+).*?err/s: ([\d.]+)", line)
        if not m:
            continue
        t, tps, p95, err = int(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
        iv.append((t, tps, p95, err))
        if samp.stall:
            outcome = "stall"
            break
        if warm_end is None and t >= WARM_MIN:
            last = [x[1] for x in iv if x[0] > t - 120]
            prev = [x[1] for x in iv if t - 240 < x[0] <= t - 120]
            steady = prev and abs(sum(last) / len(last) / (sum(prev) / len(prev)) - 1) <= 0.03
            st = snap(cnf_path).get("status", {})
            full = a.engine != "innodb" or (st.get("Innodb_buffer_pool_pages_free", 1) / max(st.get("Innodb_buffer_pool_pages_total", 1), 1) < 0.01)
            if (steady and full) or t >= WARM_MAX:
                warm_end = t
                r["warm_s"], r["warm_capped"] = t, t >= WARM_MAX and not (steady and full)
                s0 = snap(cnf_path)
        if warm_end is not None and t >= warm_end + MEASURE:
            break
    os.killpg(sb.pid, signal.SIGTERM); sb.wait(); logf.close()
    s1 = snap(cnf_path)
    samp.stop_ev.set(); samp.join()
    if outcome == "stall":
        sh(f"kill -9 $(cat {CFG['run']}/mysqld.pid)", check=False)
    win = [x for x in iv if warm_end is not None and warm_end < x[0] <= warm_end + MEASURE]
    r["outcome"] = outcome if win or outcome != "ok" else "no-window"
    if win and outcome == "ok" and sum(1 for x in win if x[1] < 1) >= 6:     # a minute or more at ~0 tps: a hang
        r["outcome"] = "hang"
    if win:
        r["tps"] = round(sum(x[1] for x in win) / len(win), 1)
        r["p95_ms"] = sorted(x[2] for x in win)[len(win) // 2]
        r["err_s"] = round(sum(x[3] for x in win) / len(win), 2)
        k3 = max(len(win) // 3, 1)        # drift over the measured window: last third vs first third (LSM compaction debt, etc.)
        first, last = sum(x[1] for x in win[:k3]) / k3, sum(x[1] for x in win[-k3:]) / k3
        r["window_drift_pct"] = round(100 * (last / first - 1), 1) if first else None
        rows_w = [x for x in samp.rows if s0 and s0["t"] <= x["t"] <= s1["t"]]
        for k in ("us", "sy", "wa", "r_s", "w_s", "w_await_ms", "psi_some", "psi_full"):
            v = [x[k] for x in rows_w if x.get(k) is not None]
            r[k] = round(sum(v) / len(v), 2) if v else None
        secs = max(s1["t"] - s0["t"], 1)
        for k in ("pswpin", "pswpout", "zswpin", "zswpout", "zswpwb", "pgmajfault"):
            if k in s1 and k in s0:
                r[k + "_s"] = round((s1[k] - s0[k]) / secs, 1)
        r["mysqld_rss_gb"] = round((s1.get("mysqld_rss_kb") or 0) / 2**20, 2)
        r["mysqld_swap_gb"] = round((s1.get("mysqld_swap_kb") or 0) / 2**20, 2)
        if s1.get("zram_mm_stat"):
            o, c = int(s1["zram_mm_stat"][0]), int(s1["zram_mm_stat"][2])
            r["zram_ratio"] = round(o / max(c, 1), 2)
        if s1.get("zswapped_kb"):
            r["zswap_ratio"] = round(s1["zswapped_kb"] / max(s1["zswap_kb"], 1), 2)
        st0, st1 = s0.get("status", {}), s1.get("status", {})
        if st1.get("Innodb_buffer_pool_pages_total"):
            r["bp_free_pct_start"] = round(100 * st0.get("Innodb_buffer_pool_pages_free", 0) / st1["Innodb_buffer_pool_pages_total"], 2)
        r["binlog_mb"] = round(sum(os.path.getsize(os.path.join(CFG["run"], f)) for f in os.listdir(CFG["run"]) if f.startswith("binlog.0")) / 2**20)
    with open(os.path.join(CFG["out"], "results.jsonl"), "a") as f:
        f.write(json.dumps(r) + "\n")
    log(f"{pid}: {r.get('outcome')} tps={r.get('tps')} p95={r.get('p95_ms')} warm={r.get('warm_s')} fio_p99={r['fio'].get('lat_p99_us')}us")
    return r


def load(a):
    os.makedirs(CFG["out"], exist_ok=True); os.makedirs(CFG["run"], exist_ok=True); os.makedirs(CFG["base"], exist_ok=True)
    cnf_path = cnf(a.engine, 8)
    stop_server(cnf_path)
    if os.path.isdir(CFG["datadir"]):
        shutil.rmtree(CFG["datadir"])
    os.makedirs(CFG["datadir"]); sh(f"chown -R mysql:mysql {os.path.dirname(CFG['datadir'])}")
    sh(f"{PS}/bin/mysqld --defaults-file={cnf_path} --initialize-insecure")
    start_server(cnf_path)
    mysql("CREATE USER sbtest@localhost IDENTIFIED BY 'sbtest'; GRANT ALL ON *.* TO sbtest@localhost; CREATE DATABASE sbtest", cnf_path)
    eng = {"innodb": "innodb", "rocksdb": "rocksdb", "tidesdb": "tidesdb"}[a.engine]
    t0 = time.time()
    # TidesDB: one loader connection (concurrent bulk loads hit false write conflicts in v0.5.0)
    thr = 1 if a.engine == "tidesdb" else 16
    sh(f"LUA_PATH='{SB_LUA}/?.lua;;' sysbench {SB_LUA}/oltp_read_write.lua --mysql-user=sbtest --mysql-password=sbtest "
       f"--mysql-socket={CFG['run']}/mysqld.sock --mysql-db=sbtest --tables=16 --table-size={a.rows} --data-random-pct={a.pct} "
       f"--mysql-storage-engine={eng} --threads={thr} prepare > {CFG['out']}/load-{a.engine}-r{a.rows // 1000000}m.log 2>&1")
    if a.engine == "rocksdb":
        mysql("SET GLOBAL rocksdb_force_flush_memtable_now = 1", cnf_path)
    stop_server(cnf_path)
    base = os.path.join(CFG["base"], f"{a.engine}-r{a.rows // 1000000}m")
    sh(f"rm -rf {base}; rsync -a {CFG['datadir']}/ {base}/; rm -f {base}/binlog.* {CFG['run']}/binlog.*")
    size = sh(f"du -s --block-size=1M {base}").split()[0]
    log(f"loaded {a.engine} {a.rows} rows x 16 in {round(time.time() - t0)} s: {size} MB on disk -> {base}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["load", "point"])
    ap.add_argument("--engine", required=True, choices=["innodb", "rocksdb", "tidesdb"])
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--pct", type=int, default=10)
    ap.add_argument("--mode", default="normal")
    ap.add_argument("--mem", type=float, default=8)
    ap.add_argument("--knob", default="")
    ap.add_argument("--pattern", default="uniform")
    ap.add_argument("--threads", type=int, default=64)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.mem == int(a.mem):
        a.mem = int(a.mem)
    (load if a.cmd == "load" else point)(a)
