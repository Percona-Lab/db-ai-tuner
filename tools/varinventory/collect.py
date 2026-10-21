#!/usr/bin/env python3
"""Inventory of server settings across MySQL / Percona Server / MariaDB / PXC release series.

Runs every image in a throwaway Docker container with no configuration (so values are the series' defaults), and
records per variable: value (default), type, min / max, whether it can change at runtime, plus the status counters.
Runtime changeability: MariaDB reports it (information_schema.SYSTEM_VARIABLES.READ_ONLY); for MySQL / Percona each
variable gets SET GLOBAL <v> = @@GLOBAL.<v> in the throwaway server (error 1238 = read only). Output: one JSON per image.
usage: collect.py <outdir> image[=series] ...
"""
import json
import os
import subprocess
import sys
import time

import pymysql

PW = "inventory-pw"


def run(cmd, **k):
    return subprocess.run(cmd, capture_output=True, text=True, **k)


def start(image, port, name):
    env = ["-e", f"MYSQL_ROOT_PASSWORD={PW}", "-e", f"MARIADB_ROOT_PASSWORD={PW}"]
    if "xtradb-cluster" in image:
        env += ["-e", "CLUSTER_NAME=inventory", "-e", "PXC_ENCRYPT_CLUSTER_TRAFFIC=0"]
    run(["docker", "rm", "-f", name])
    r = run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:3306"] + env + [image])
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-300:])
    for _ in range(120):
        try:
            return pymysql.connect(host="127.0.0.1", port=port, user="root", password=PW, autocommit=True, connect_timeout=3)
        except pymysql.MySQLError:
            time.sleep(3)
    raise RuntimeError("server did not come up: " + run(["docker", "logs", "--tail", "20", name]).stderr[-500:])


def collect(con):
    cur = con.cursor()
    cur.execute("SELECT @@version, @@version_comment")
    version, comment = cur.fetchone()
    cur.execute("SHOW GLOBAL VARIABLES")
    values = {k.lower(): v for k, v in cur.fetchall()}
    meta = {}
    try:  # MariaDB: types, ranges, read-only, default, from the server itself
        cur.execute("SELECT VARIABLE_NAME, VARIABLE_TYPE, NUMERIC_MIN_VALUE, NUMERIC_MAX_VALUE, READ_ONLY, DEFAULT_VALUE, VARIABLE_SCOPE, "
                    "ENUM_VALUE_LIST FROM information_schema.SYSTEM_VARIABLES")
        for n, t, lo, hi, ro, d, scope, enum in cur.fetchall():
            meta[n.lower()] = {"type": t, "min": lo, "max": hi, "dynamic": ro == "NO", "default": d, "scope": scope,
                               "values": enum.split(",") if enum else None}
    except pymysql.MySQLError:
        pass
    if not meta:  # MySQL / Percona: ranges from variables_info, dynamic by trying a same-value SET GLOBAL
        try:
            cur.execute("SELECT VARIABLE_NAME, MIN_VALUE, MAX_VALUE FROM performance_schema.variables_info")
            for n, lo, hi in cur.fetchall():
                meta[n.lower()] = {"min": lo if lo not in ("0", None) or hi not in ("0", None) else None,
                                   "max": hi if hi not in ("0", None) else None}
        except pymysql.MySQLError:
            pass
        skip = {"gtid_mode", "enforce_gtid_consistency", "super_read_only", "read_only", "offline_mode", "wsrep_on", "wsrep_cluster_address",
                "wsrep_provider", "init_connect", "init_slave", "init_replica", "event_scheduler", "general_log", "slow_query_log",
                "innodb_buffer_pool_size", "keyring_operations", "persisted_globals_load", "sql_log_off"}
        for n in values:
            m = meta.setdefault(n, {})
            if n in skip:
                continue
            try:
                cur.execute(f"SET GLOBAL `{n}` = @@GLOBAL.`{n}`")
                m["dynamic"] = True
            except pymysql.MySQLError as e:
                code = e.args[0] if e.args else 0
                if code == 1238:
                    m["dynamic"] = False
                elif code == 1228:  # session-only variable
                    m["scope"] = "SESSION"
    cur.execute("SHOW GLOBAL STATUS")
    status = sorted(k.lower() for k, _ in cur.fetchall())
    cur.execute("SELECT ENGINE, SUPPORT FROM information_schema.ENGINES")
    engines = {e.lower(): s for e, s in cur.fetchall()}
    return {"version": version, "version_comment": comment, "variables": {n: dict(meta.get(n, {}), value=values[n]) for n in values},
            "status": status, "engines": engines}


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for i, spec in enumerate(sys.argv[2:]):
        image, _, series = spec.partition("=")
        name, port = f"inv{i}", 33100 + i
        print(f"== {image}", flush=True)
        try:
            con = start(image, port, name)
            d = collect(con)
            d["image"], d["series"] = image, series or image
            path = os.path.join(out, (series or image).replace("/", "_").replace(":", "_").replace(" ", "_") + ".json")
            json.dump(d, open(path, "w"), indent=1, default=str)
            print(f"   {d['version']} ({d['version_comment']}): {len(d['variables'])} variables, {len(d['status'])} status counters", flush=True)
        except Exception as e:  # noqa
            print(f"   FAILED: {e}", flush=True)
        finally:
            run(["docker", "rm", "-f", name])


if __name__ == "__main__":
    main()
