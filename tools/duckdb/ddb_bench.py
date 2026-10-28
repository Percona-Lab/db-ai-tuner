"""SQLite vs DuckDB for the tuner's metrics and runs tables, on a copy of production tuner.db.

usage: PYTHONPATH=pylib python3 ddb_bench.py load|bench <sqlite copy> <duckdb file> [threads] [memory_limit]
"""
import json
import os
import random
import sqlite3
import statistics
import sys
import time

import duckdb

mode, SQ, DD = sys.argv[1], sys.argv[2], sys.argv[3]
THREADS = int(sys.argv[4]) if len(sys.argv) > 4 else 1
MEM = sys.argv[5] if len(sys.argv) > 5 else "1GB"


def dconn(read_only=False):
    c = duckdb.connect(DD, read_only=read_only, config={"threads": THREADS, "memory_limit": MEM, "preserve_insertion_order": False,
                                                        "temp_directory": DD + ".tmp"})
    c.execute("LOAD json")
    return c


if mode == "load":
    resume = os.environ.get("RESUME_RUNS") == "1"
    if os.path.exists(DD) and not resume:
        os.remove(DD)
    c = dconn()
    c.execute("LOAD sqlite")
    t = time.time()
    c.execute(f"ATTACH '{SQ}' AS s (TYPE sqlite, READ_ONLY)")
    # same shape as SQLite: JSON documents in a JSON column, primary keys as in the server
    if not resume:
        c.execute("CREATE TABLE metrics (env_id INTEGER NOT NULL, ts DOUBLE NOT NULL, data JSON NOT NULL, PRIMARY KEY (env_id, ts))")
    envs = [] if resume else [r[0] for r in c.execute("SELECT DISTINCT env_id FROM s.metrics ORDER BY 1").fetchall()]
    for e in envs:  # one transaction per environment: a single 2.6 GB insert ran out of memory at 1 GB
        c.execute("INSERT INTO metrics SELECT env_id, ts, data FROM s.metrics WHERE env_id=?", [e])
        print("  metrics env", e, flush=True)
    t1 = time.time()
    c.execute("DROP TABLE IF EXISTS runs")
    c.execute("""CREATE TABLE runs (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, seq INTEGER, label VARCHAR, notes VARCHAR,
                 status VARCHAR NOT NULL, started DOUBLE, ended DOUBLE, config JSON, managed JSON, changes JSON, summary JSON,
                 external JSON, inputs JSON, diagnosis JSON, goal_value DOUBLE, goal_source VARCHAR, delta_pct DOUBLE,
                 delta_vs_first_pct DOUBLE, verdict VARCHAR, evaluation JSON, config_meta JSON, tradeoffs JSON, abort_reason VARCHAR)""")
    # streamed through Python, 50 runs per transaction: DuckDB's SQLite reader held the whole 1.1 GB table in memory even
    # for 200-row slices and ran out at 1 GB
    cols = ("id, session_id, seq, label, notes, status, started, ended, config, managed, changes, summary, external, inputs, diagnosis, "
            "goal_value, goal_source, delta_pct, delta_vs_first_pct, verdict, evaluation, config_meta, tradeoffs, abort_reason")
    src = sqlite3.connect(SQ)
    cur, batch, n = src.execute(f"SELECT {cols} FROM runs ORDER BY id"), [], 0
    for row in cur:
        batch.append(row)
        if len(batch) == 50:
            c.executemany(f"INSERT INTO runs ({cols}) VALUES ({', '.join('?' * 24)})", batch)
            n, batch = n + len(batch), []
    if batch:
        c.executemany(f"INSERT INTO runs ({cols}) VALUES ({', '.join('?' * 24)})", batch)
    print("  runs loaded", n + len(batch), flush=True)
    c.execute("CREATE INDEX runs_session ON runs(session_id, seq)")
    t2 = time.time()
    # the columnar alternative for metrics: the fields the server reads, typed, next to the document
    c.execute("""CREATE TABLE metrics_cols AS SELECT env_id, ts,
                 CAST(json_extract(data, '$.os.cpu_steal') AS DOUBLE) cpu_steal,
                 CAST(json_extract(data, '$.os.cpu_busy') AS DOUBLE) cpu_busy,
                 CAST(json_extract(data, '$.db.tps') AS DOUBLE) tps,
                 CAST(json_extract(data, '$.db.threads_running') AS DOUBLE) threads_running,
                 CAST(json_extract(data, '$.db.history_list_length') AS DOUBLE) hll,
                 json_extract(data, '$.waits') waits FROM metrics""")
    t3 = time.time()
    c.execute("CHECKPOINT")
    c.close()
    print(json.dumps({"load_metrics_s": round(t1 - t, 1), "load_runs_s": round(t2 - t1, 1), "metrics_cols_s": round(t3 - t2, 1),
                      "duckdb_bytes": os.path.getsize(DD), "sqlite_bytes": os.path.getsize(SQ)}))
    sys.exit(0)

# ---------------------------------------------------------------- bench
random.seed(7)
s = sqlite3.connect(SQ, isolation_level=None)  # autocommit, as the server (explicit BEGIN for batches)
s.row_factory = sqlite3.Row
s.execute("PRAGMA journal_mode=WAL")
s.execute("PRAGMA synchronous=NORMAL")  # as store.conn()
d = dconn()
runs = [dict(r) for r in s.execute("SELECT r.id, r.session_id, r.started, r.ended, t.env_id FROM runs r JOIN tsessions t ON t.id=r.session_id "
                                     "WHERE r.status='done' AND r.started IS NOT NULL AND r.ended IS NOT NULL AND r.ended - r.started < 7200")]
runs = random.sample(runs, 40)
envs = [r[0] for r in s.execute("SELECT DISTINCT env_id FROM metrics")]
sessions = random.sample([r[0] for r in s.execute("SELECT id FROM tsessions WHERE id IN (SELECT session_id FROM runs) ")], 30)
tmax = s.execute("SELECT MAX(ts) FROM metrics").fetchone()[0]
out = {}


ONLY = [x for x in os.environ.get("ONLY", "").split(",") if x]


def timeit(name, fn_s, fn_d, args, reps=1):
    if ONLY and name.split()[0] not in ONLY:
        return
    res = {}
    for eng, fn, cn in (("sqlite", fn_s, s), ("duckdb", fn_d, d)):
        lat = []
        try:
            for a in args:
                for _ in range(reps):
                    t = time.perf_counter()
                    fn(cn, a)
                    lat.append((time.perf_counter() - t) * 1000)
        except Exception as e:  # noqa - recorded (e.g. DuckDB out of memory at the limit)
            res[eng] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            continue
        lat.sort()
        res[eng] = {"p50_ms": round(statistics.median(lat), 2), "p95_ms": round(lat[int(0.95 * (len(lat) - 1))], 2), "n": len(lat)}
    out[name] = res
    print(name, json.dumps(res), flush=True)


# Q1: a run's metric window (run page charts, main.py:358 / 823)
timeit("Q1 run window: metrics of one run, ordered",
       lambda c, r: c.execute("SELECT ts, data FROM metrics WHERE env_id=? AND ts>=? AND ts<=? ORDER BY ts", (r["env_id"], r["started"], r["ended"])).fetchall(),
       lambda c, r: c.execute("SELECT ts, data FROM metrics WHERE env_id=? AND ts>=? AND ts<=? ORDER BY ts", [r["env_id"], r["started"], r["ended"]]).fetchall(),
       runs)
# Q2: latest sample of every environment (environments list, main.py:531)
timeit("Q2 latest sample per environment (all envs)",
       lambda c, _: [c.execute("SELECT data FROM metrics WHERE env_id=? ORDER BY ts DESC LIMIT 1", (e,)).fetchone() for e in envs],
       lambda c, _: [c.execute("SELECT data FROM metrics WHERE env_id=? ORDER BY ts DESC LIMIT 1", [e]).fetchone() for e in envs],
       [0], reps=5)
# Q3: an hour of samples with waits for the wait chart (main.py:687), parsed in Python
timeit("Q3 env waits: 1 h of samples, parsed",
       lambda c, r: [json.loads(x[0]) for x in c.execute("SELECT data FROM metrics WHERE env_id=? AND ts>=? AND ts<=?", (r["env_id"], r["ended"] - 3600, r["ended"]))],
       lambda c, r: [json.loads(x[0]) for x in c.execute("SELECT data FROM metrics WHERE env_id=? AND ts>=? AND ts<=?", [r["env_id"], r["ended"] - 3600, r["ended"]]).fetchall()],
       runs[:15])
# Q4: analytic - 7 days of steal for one environment (steal backfill, main.py:1367), and the per-env distribution over all
timeit("Q4 steal over 7 days, one env (json_extract)",
       lambda c, e: c.execute("SELECT count(*), avg(json_extract(data, '$.os.cpu_steal')) FROM metrics WHERE env_id=? AND ts>?", (e, tmax - 7 * 86400)).fetchone(),
       lambda c, e: c.execute("SELECT count(*), avg(CAST(json_extract(data, '$.os.cpu_steal') AS DOUBLE)) FROM metrics WHERE env_id=? AND ts>?", [e, tmax - 7 * 86400]).fetchone(),
       envs[:6])
timeit("Q4b steal over 7 days, one env (typed column)",
       lambda c, e: c.execute("SELECT count(*), avg(v) FROM steal_samples WHERE env_id=? AND ts>?", (e, tmax - 7 * 86400)).fetchone(),
       lambda c, e: c.execute("SELECT count(*), avg(cpu_steal) FROM metrics_cols WHERE env_id=? AND ts>?", [e, tmax - 7 * 86400]).fetchone(),
       envs[:6])
timeit("Q4c whole-table analytic: avg tps / steal per env and day (json)",
       lambda c, _: c.execute("SELECT env_id, date(ts,'unixepoch'), avg(json_extract(data,'$.db.tps')), avg(json_extract(data,'$.os.cpu_steal')) FROM metrics GROUP BY 1, 2").fetchall(),
       lambda c, _: c.execute("SELECT env_id, CAST(to_timestamp(ts) AS DATE), avg(CAST(json_extract(data,'$.db.tps') AS DOUBLE)), avg(CAST(json_extract(data,'$.os.cpu_steal') AS DOUBLE)) FROM metrics GROUP BY 1, 2").fetchall(),
       [0])
timeit("Q4d whole-table analytic: same on typed columns (duckdb only; sqlite json)",
       lambda c, _: c.execute("SELECT env_id, date(ts,'unixepoch'), avg(json_extract(data,'$.db.tps')), avg(json_extract(data,'$.os.cpu_steal')) FROM metrics GROUP BY 1, 2").fetchall(),
       lambda c, _: c.execute("SELECT env_id, CAST(to_timestamp(ts) AS DATE), avg(tps), avg(cpu_steal) FROM metrics_cols GROUP BY 1, 2").fetchall(),
       [0])
# R1: session page (main.py:765) - every run of a session with its documents
timeit("R1 session page: all runs of a session",
       lambda c, sid: c.execute("SELECT * FROM runs WHERE session_id=? ORDER BY seq, id", (sid,)).fetchall(),
       lambda c, sid: c.execute("SELECT * FROM runs WHERE session_id=? ORDER BY seq, id", [sid]).fetchall(),
       sessions)
# R2: one run (run page)
timeit("R2 run page: one run by id",
       lambda c, r: c.execute("SELECT * FROM runs WHERE id=?", (r["id"],)).fetchone(),
       lambda c, r: c.execute("SELECT * FROM runs WHERE id=?", [r["id"]]).fetchone(),
       runs)
# R3: the similarity case base (similar.py:35) - documents of every run, parsed
timeit("R3 case base scan: all runs' documents, parsed",
       lambda c, _: [json.loads(x[0] or "null") for x in c.execute("SELECT summary FROM runs WHERE status='done'")],
       lambda c, _: [json.loads(x[0] or "null") for x in c.execute("SELECT summary FROM runs WHERE status='done'").fetchall()],
       [0])
# R4: cross-session analytic over runs (llm.py:172 / main.py:1578 style)
timeit("R4 analytic: verdicts by workload type over all runs",
       lambda c, _: c.execute("SELECT json_extract(inputs,'$.workload.type') wl, verdict, count(*) FROM runs WHERE status='done' GROUP BY 1, 2").fetchall(),
       lambda c, _: c.execute("SELECT json_extract_string(inputs,'$.workload.type') wl, verdict, count(*) FROM runs WHERE status='done' GROUP BY 1, 2").fetchall(),
       [0])

# W1: agent ingest - a batch of 6 samples (one minute), upsert, own transaction each (main.py:1330)
sample = s.execute("SELECT data FROM metrics ORDER BY ts DESC LIMIT 1").fetchone()[0]
base = tmax + 10_000


def ins_s(c, i):
    c.execute("BEGIN")
    c.executemany("INSERT OR REPLACE INTO metrics(env_id, ts, data) VALUES (?,?,?)", [(999, base + i * 60 + k * 10, sample) for k in range(6)])
    c.execute("COMMIT")


def ins_d(c, i):
    c.execute("BEGIN")
    c.executemany("INSERT INTO metrics VALUES (?,?,?) ON CONFLICT DO UPDATE SET data = excluded.data", [(999, base + i * 60 + k * 10, sample) for k in range(6)])
    c.execute("COMMIT")


timeit("W1 ingest: 6-sample upsert batch, one transaction", ins_s, ins_d, list(range(300)))


# W2: finishing a run - one UPDATE of a run row with its documents (main.py:1998)
blob = s.execute("SELECT inputs FROM runs WHERE inputs IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()[0]
timeit("W2 run update: summary/inputs of one run",
       lambda c, r: c.execute("UPDATE runs SET inputs=?, verdict='neutral' WHERE id=?", (blob, r["id"])),
       lambda c, r: c.execute("UPDATE runs SET inputs=?, verdict='neutral' WHERE id=?", [blob, r["id"]]),
       runs[:20])
print("RESULT " + json.dumps(out))
