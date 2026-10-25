"""Data loading on the database host, seen from outside: the benchmark's own prepare / load step (sysbench prepare, go-tpc
prepare, BenchBase --load, a dump restore) runs for minutes to hours before any session, and the host looked idle in the
UI apart from its CPU and disk graphs (user, 2026-10-25). The daemon reports it as the environment's activity: what is
loading, how much the data file system grew and how fast, and an estimate of the time left when the loader says how much
it will write (sysbench tables x rows)."""
import os
import re

# argv patterns of loaders: (name, regex on the joined command line)
LOADERS = (
    ("sysbench prepare", re.compile(r"(^|/)sysbench\s.*\sprepare(\s|$)")),
    ("go-tpc prepare", re.compile(r"(^|/)go-tpc\s.*\sprepare(\s|$)")),
    ("BenchBase load", re.compile(r"benchbase.*--load[= ](true|1)", re.I)),
    ("YCSB load", re.compile(r"(^|/)ycsb(\.sh)?\s+load\s")),
    ("myloader", re.compile(r"(^|/)myloader\s")),
    ("mysqlimport", re.compile(r"(^|/)mysqlimport\s")),
    ("MySQL Shell dump load", re.compile(r"(^|/)mysqlsh\s.*load(Dump|_dump)", re.I)),
    ("HammerDB schema build", re.compile(r"hammerdb.*build|buildschema", re.I)),
    ("tpcc-mysql load", re.compile(r"(^|/)tpcc_load\s")),
)
SYSBENCH_ROW_BYTES = 240  # on-disk bytes per sysbench row with its secondary index (measured: 16 x 1M rows ~ 3.8 GB)


def _arg(argv, name):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


def processes():
    """[(pid, argv)] of every process (cheap: /proc cmdline only)."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            argv = [x.decode(errors="replace") for x in open(f"/proc/{d}/cmdline", "rb").read().split(b"\0") if x]
        except OSError:
            continue
        if argv:
            out.append((int(d), argv))
    return out


def loaders(procs=None):
    """Loader processes running now: [{name, pid, cmd, expected_bytes?}]."""
    found = []
    for pid, argv in procs if procs is not None else processes():
        line = " ".join(argv)
        for name, rx in LOADERS:
            if rx.search(line):
                x = {"name": name, "pid": pid, "cmd": line[:200]}
                if name == "sysbench prepare":
                    try:
                        t, n = int(_arg(argv, "tables") or 1), int(_arg(argv, "table-size") or _arg(argv, "table_size") or 10000)
                        x["expected_bytes"] = t * n * SYSBENCH_ROW_BYTES
                        x["detail"] = f"{t} table{'s' if t > 1 else ''} x {n:,} rows"
                    except ValueError:
                        pass
                found.append(x)
                break
    return found


def statements(db):
    """Long-running loading statements in the server (LOAD DATA, CREATE INDEX, ALTER TABLE, INSERT ... SELECT) - loads
    driven by a client this host cannot see (a remote loader, a `mysql < dump.sql`)."""
    try:
        rows = db.dict_rows("SELECT TIME t, LEFT(INFO, 120) q FROM information_schema.PROCESSLIST WHERE COMMAND='Query' AND TIME >= 30 "
                            "AND (INFO LIKE 'LOAD DATA%' OR INFO LIKE 'CREATE%INDEX%' OR INFO LIKE 'ALTER TABLE%' OR INFO LIKE 'INSERT%SELECT%')")
    except Exception:
        return []
    return [{"seconds": int(r["t"]), "query": r["q"]} for r in rows]


def used_bytes(path):
    try:
        st = os.statvfs(path)
        return (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        return None


def gb(b):
    return f"{b / 1e9:,.1f} GB"


class Tracker:
    """Keeps the data file system's growth between daemon rounds; report() returns (phase, detail) while a load runs,
    ("idle", ...) once when it ends, else None."""

    def __init__(self, datadir):
        self.datadir = datadir
        self.start = None  # (ts, used bytes) when the load was first seen
        self.last = None
        self.active = False
        self.warned = None  # the open session a data load was reported against (once)

    def report(self, now, db=None, procs=None):
        ld = loaders(procs)
        sts = statements(db) if db is not None and not ld else []
        if not ld and not sts:
            if self.active:
                self.active = False
                grew = (self.last[1] - self.start[1]) if self.start and self.last else None
                self.start = self.last = None
                return ("idle", "data load finished" + (f": the data file system grew {gb(grew)}" if grew and grew > 0 else ""))
            return None
        used = used_bytes(self.datadir)
        if not self.active or self.start is None:
            self.start = (now, used)
        rate = None
        if self.last and used is not None and self.last[1] is not None and now > self.last[0]:
            rate = (used - self.last[1]) / (now - self.last[0])
        self.last = (now, used)
        self.active = True
        if ld:
            x = ld[0]
            what = x["name"] + (f" ({x['detail']})" if x.get("detail") else "") + (f" and {len(ld) - 1} more" if len(ld) > 1 else "")
        else:
            what = f"{len(sts)} loading statement(s) in the server, longest {max(s['seconds'] for s in sts) // 60} min: {sts[0]['query']}"
        grew = used - self.start[1] if used is not None and self.start[1] is not None else None
        parts = [what]
        if grew is not None:
            parts.append(f"data file system +{gb(max(0, grew))} since {int((now - self.start[0]) // 60)} min ago")
        if rate is not None and rate > 0:
            parts.append(f"{rate * 60 / 1e9:.2f} GB/min")
            exp = ld[0].get("expected_bytes") if ld else None
            if exp and grew is not None and exp > grew:
                parts.append(f"~{(exp - grew) / rate / 60:.0f} min left of ~{gb(exp)} expected")
        return ("loading data", "; ".join(parts))
