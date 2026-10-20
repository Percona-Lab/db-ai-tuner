#!/usr/bin/env python3
"""Validate the tuner's memory model (agent/tuning_agent/memory.py) on a real server.

Run on a test host as root, with the tuning agent's venv (PyMySQL):
  /opt/tuning-agent/venv/bin/python mem_validate.py [tests...]  > results.json
Tests: connections pfs sort join tmp tables prepared bp (default: all). Each test writes its own config drop-in
(/etc/mysql/mysql.conf.d/zzz-memtest.cnf), restarts mysqld, measures mysqld memory (PSS + swap PSS from
/proc/<pid>/smaps_rollup) idle and under a controlled load, and reports the cost per unit next to the model's constant.
Uses its own database `memtest`; removes the drop-in at the end. Never run it on a host with a running study.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pymysql

CNF = "/etc/mysql/mysql.conf.d/zzz-memtest.cnf"
SOCK = "/var/run/mysqld/mysqld.sock"
KiB, MiB, GiB = 1024, 1024 ** 2, 1024 ** 3
MODEL = {"connection_base": 768 * KiB, "pfs_per_connection": 128 * KiB, "pfs_base": 192 * MiB, "table_open_cache": 8 * KiB,
         "table_definition_cache": 6 * KiB, "prepared_stmt": 16 * KiB, "bp_control": 0.03, "mysqld_fixed": 400 * MiB}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def pid():
    return int(subprocess.check_output(["pidof", "mysqld"]).split()[0])


def mem():
    """mysqld PSS + swap PSS in bytes."""
    tot = 0
    for line in open(f"/proc/{pid()}/smaps_rollup"):
        k, _, v = line.partition(":")
        if k in ("Pss", "SwapPss"):
            tot += int(v.split()[0]) * KiB
    return tot


def conn(db=None):
    return pymysql.connect(unix_socket=SOCK, user="root", database=db, autocommit=True)


def restart(settings):
    body = "[mysqld]\n" + "".join(f"{k} = {v}\n" for k, v in settings.items())
    open(CNF, "w").write(body)
    subprocess.run(["systemctl", "restart", "mysql"], check=True)
    for _ in range(120):
        try:
            conn().close()
            break
        except Exception:
            time.sleep(1)
    time.sleep(5)


BASE = {"max_connections": 5000, "innodb_buffer_pool_size": "1G", "performance_schema": "ON", "table_open_cache": 4000,
        "table_definition_cache": 2000, "max_prepared_stmt_count": 200000, "thread_cache_size": 0}


VARS = ("innodb_buffer_pool_size", "max_connections", "performance_schema", "sort_buffer_size", "join_buffer_size", "read_buffer_size",
        "read_rnd_buffer_size", "tmp_table_size", "max_heap_table_size", "temptable_max_ram", "table_open_cache", "table_definition_cache",
        "thread_stack", "net_buffer_length")


def variables():
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT variable_name, variable_value FROM performance_schema.global_variables WHERE variable_name IN (" +
                ",".join(f"'{v}'" for v in VARS) + ")")
    out = dict(cur.fetchall())
    c.close()
    return out


def settled():
    vals = []
    for _ in range(5):
        vals.append(mem())
        time.sleep(1)
    return sorted(vals)[2]


def peak_during(fn, seconds):
    """Run fn() in threads until done; sample mysqld memory every 0.2 s; return the peak."""
    stop, pk = [False], [0]

    def sample():
        while not stop[0]:
            pk[0] = max(pk[0], mem())
            time.sleep(0.2)
    t = threading.Thread(target=sample, daemon=True)
    t.start()
    fn(seconds)
    stop[0] = True
    t.join()
    return pk[0]


def run_threads(n, work, seconds):
    def loop():
        c = conn("memtest")
        cur = c.cursor()
        end = time.time() + seconds
        while time.time() < end:
            cur.execute(work)
            cur.fetchall()
        c.close()
    ts = [threading.Thread(target=loop) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def ensure_data():
    c = conn()
    cur = c.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS memtest")
    c.select_db("memtest")
    cur.execute("CREATE TABLE IF NOT EXISTS big (id INT PRIMARY KEY AUTO_INCREMENT, k INT, c CHAR(120), pad CHAR(60))")
    cur.execute("SELECT COUNT(*) FROM big")
    if cur.fetchone()[0] < 2_000_000:
        log("filling memtest.big (2M rows)")
        cur.execute("INSERT INTO big (k, c, pad) VALUES " + ",".join(f"({i % 1000}, REPEAT(MD5({i}), 3), MD5({i + 7}))" for i in range(1000)))
        while True:
            cur.execute("SELECT COUNT(*) FROM big")
            if cur.fetchone()[0] >= 2_000_000:
                break
            cur.execute("INSERT INTO big (k, c, pad) SELECT FLOOR(RAND() * 100000), REPEAT(MD5(RAND()), 3), MD5(RAND()) FROM big LIMIT 500000")
    c.close()


# ---------------------------------------------------------------------------------------------------------- tests
def t_connections(pfs="ON"):
    restart(dict(BASE, performance_schema=pfs))
    base = settled()
    out, conns = [], []
    for n in (500, 1000, 2000, 4000):
        while len(conns) < n:
            conns.append(conn())
        m = settled()
        out.append({"connections": n, "delta_bytes": m - base, "per_connection": (m - base) / n})
        log(f"connections pfs={pfs} n={n}: {(m - base) / n / KiB:.0f} KiB each")
    for c in conns:
        c.close()
    return {"performance_schema": pfs, "idle_bytes": base, "points": out}


def t_buffer(var, sizes, query, threads):
    res = []
    for size in sizes:
        restart(dict(BASE, **{var: size}))
        idle = []
        for _ in range(threads):  # the connections themselves, open before the baseline
            idle.append(conn("memtest"))
        base = settled()
        for c in idle:
            c.close()
        pk = peak_during(lambda s: run_threads(threads, query, s), 25)
        res.append({var: size, "threads": threads, "peak_delta_bytes": pk - base, "per_active_connection": (pk - base) / threads})
        log(f"{var}={size}: {(pk - base) / threads / MiB:.2f} MiB per active connection")
    return res


def t_tables(n=5000):
    restart(dict(BASE, table_open_cache=8000, table_definition_cache=8000))
    c = conn("memtest")
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='memtest' AND table_name LIKE 't%'")
    have = cur.fetchone()[0]
    for i in range(have, n):
        cur.execute(f"CREATE TABLE IF NOT EXISTS t{i} (id INT PRIMARY KEY, a INT, b VARCHAR(64), KEY(a))")
    c.close()
    restart(dict(BASE, table_open_cache=8000, table_definition_cache=8000))
    base = settled()
    c = conn("memtest")
    cur = c.cursor()
    for i in range(n):
        cur.execute(f"SELECT * FROM t{i} LIMIT 1")
        cur.fetchall()
    m = settled()
    c.close()
    log(f"tables: {(m - base) / n / KiB:.1f} KiB per opened table (definition + instance)")
    return {"tables": n, "delta_bytes": m - base, "per_table": (m - base) / n}


def t_prepared(conns=100, per=200):
    restart(BASE)
    cs = [conn("memtest") for _ in range(conns)]
    base = settled()
    for c in cs:
        cur = c.cursor()
        for j in range(per):
            cur.execute(f"PREPARE s{j} FROM 'SELECT id, k, c FROM big WHERE id = ? AND k > {j} ORDER BY id LIMIT 10'")
    m = settled()
    for c in cs:
        c.close()
    n = conns * per
    log(f"prepared: {(m - base) / n / KiB:.1f} KiB per statement")
    return {"statements": n, "delta_bytes": m - base, "per_statement": (m - base) / n}


def t_bp(sizes=("2G", "6G")):
    res = []
    for s in sizes:
        restart(dict(BASE, innodb_buffer_pool_size=s))
        c = conn("memtest")
        cur = c.cursor()
        # touch the whole pool: scan a table larger than it (the BenchBase YCSB set on the test host, else memtest.big)
        cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN "
                    "('mysql','sys','information_schema','performance_schema') ORDER BY data_length DESC LIMIT 1")
        sch, tab = cur.fetchone()
        cur.execute(f"SELECT COUNT(*) FROM `{sch}`.`{tab}` FORCE INDEX (PRIMARY)")
        cur.fetchall()
        cur.execute("SELECT @@innodb_buffer_pool_size, (SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_buffer_pool_pages_data')")
        bps, pages = cur.fetchone()
        c.close()
        m = settled()
        res.append({"buffer_pool": int(bps), "pages_data": int(pages), "mysqld_bytes": m})
        log(f"bp {s}: mysqld {m / GiB:.2f} GiB, {int(pages) * 16 / 1024 / 1024:.2f} GiB of pages in use")
    return res


def main():
    tests = sys.argv[1:] or ["pfs", "connections", "sort", "join", "tmp", "tables", "prepared", "bp"]
    ensure_data()
    restart(BASE)
    out = {"model": MODEL, "host": os.uname().nodename, "variables_at_start": variables(), "results": {}}
    try:
        for t in tests:
            log(f"== {t}")
            if t == "connections":
                out["results"]["connections_pfs_on"] = t_connections("ON")
            elif t == "pfs":
                out["results"]["connections_pfs_off"] = t_connections("OFF")
            elif t == "sort":
                out["results"]["sort_buffer"] = t_buffer("sort_buffer_size", ("256K", "8M", "64M"),
                                                         "SELECT id, c FROM big WHERE id < 300000 ORDER BY pad, c LIMIT 10", 32)
            elif t == "join":
                out["results"]["join_buffer"] = t_buffer("join_buffer_size", ("256K", "64M"),
                                                         "SELECT COUNT(*) FROM big a STRAIGHT_JOIN big b IGNORE INDEX (PRIMARY) ON a.pad = b.pad "
                                                         "WHERE a.id < 20000 AND b.id < 20000", 16)
            elif t == "tmp":
                out["results"]["tmp_table"] = t_buffer("tmp_table_size", ("16M", "64M"),
                                                       "SELECT pad, COUNT(*) FROM big WHERE id < 400000 GROUP BY pad ORDER BY NULL LIMIT 1", 16)
            elif t == "tables":
                out["results"]["tables"] = t_tables()
            elif t == "prepared":
                out["results"]["prepared"] = t_prepared()
            elif t == "bp":
                out["results"]["buffer_pool"] = t_bp()
            print(json.dumps(out, indent=1), file=open("/root/memprobe-partial.json", "w"))
    finally:
        if os.path.exists(CNF):
            os.remove(CNF)
        subprocess.run(["systemctl", "restart", "mysql"])
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
