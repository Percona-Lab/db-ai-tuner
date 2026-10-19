"""MySQL / Percona Server / MariaDB access helpers."""
import re
import time

import pymysql


class DB:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self, retries=1, delay=2.0):
        last = None
        for _ in range(max(1, retries)):
            try:
                kw = dict(user=self.cfg.get("mysql_user", "root"),
                          password=self.cfg.get("mysql_password", "") or "",
                          connect_timeout=5, read_timeout=60, write_timeout=60,
                          autocommit=True, charset="utf8mb4")
                sock = self.cfg.get("mysql_socket")
                if sock:
                    kw["unix_socket"] = sock
                else:
                    kw["host"] = self.cfg.get("mysql_host", "127.0.0.1")
                    kw["port"] = int(self.cfg.get("mysql_port", 3306))
                self.conn = pymysql.connect(**kw)
                return self
            except pymysql.MySQLError as e:
                last = e
                time.sleep(delay)
        raise last

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def ensure(self):
        if self.conn is None:
            self.connect()
        else:
            try:
                self.conn.ping(reconnect=True)
            except Exception:
                self.conn = None
                self.connect()

    def rows(self, sql, args=None):
        self.ensure()
        with self.conn.cursor() as c:
            c.execute(sql, args)
            return c.fetchall()

    def dict_rows(self, sql, args=None):
        self.ensure()
        with self.conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute(sql, args)
            return c.fetchall()

    def scalar(self, sql, args=None):
        r = self.rows(sql, args)
        return r[0][0] if r else None

    def execute(self, sql, args=None):
        self.ensure()
        with self.conn.cursor() as c:
            return c.execute(sql, args)

    # ---- server information -------------------------------------------------
    def variables(self):
        return {k.lower(): v for k, v in self.rows("SHOW GLOBAL VARIABLES")}

    def status(self):
        out = {}
        for k, v in self.rows("SHOW GLOBAL STATUS"):
            out[k] = v
        return out

    def innodb_status_text(self):
        try:
            r = self.rows("SHOW ENGINE INNODB STATUS")
            return r[0][2] if r else ""
        except pymysql.MySQLError:
            return ""

    def innodb_metrics(self):
        try:
            rows = self.rows("SELECT NAME, COUNT FROM information_schema.INNODB_METRICS WHERE STATUS='enabled'")
            return {n: int(c) for n, c in rows}
        except pymysql.MySQLError:
            return {}

    def ps_enabled(self):
        try:
            return str(self.scalar("SELECT @@performance_schema")) in ("1", "ON")
        except pymysql.MySQLError:
            return False

    def statement_totals(self):
        """Top-level statement count and total latency (picoseconds) from P_S."""
        try:
            r = self.rows(
                "SELECT SUM(COUNT_STAR), SUM(SUM_TIMER_WAIT) FROM performance_schema.events_statements_summary_global_by_event_name "
                "WHERE (EVENT_NAME LIKE 'statement/sql/%' AND EVENT_NAME NOT LIKE 'statement/sql/show%') "
                "OR EVENT_NAME LIKE 'statement/com/Execute%'")
            if r and r[0][0] is not None:
                return int(r[0][0]), int(r[0][1])
        except pymysql.MySQLError:
            pass
        return None

    def statement_histogram(self):
        try:
            return [(int(b), int(c)) for b, c in self.rows(
                "SELECT BUCKET_TIMER_HIGH, COUNT_BUCKET FROM performance_schema.events_statements_histogram_global ORDER BY BUCKET_NUMBER")]
        except pymysql.MySQLError:
            return None

    def digests(self, limit=200):
        try:
            return self.dict_rows(
                "SELECT SCHEMA_NAME, DIGEST, LEFT(DIGEST_TEXT, 400) AS DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, SUM_LOCK_TIME, "
                "SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_ROWS_AFFECTED, SUM_CREATED_TMP_DISK_TABLES, SUM_CREATED_TMP_TABLES, "
                "SUM_SORT_MERGE_PASSES, SUM_NO_INDEX_USED, SUM_SELECT_FULL_JOIN FROM performance_schema.events_statements_summary_by_digest "
                "ORDER BY SUM_TIMER_WAIT DESC LIMIT %s", (limit,))
        except pymysql.MySQLError:
            return []

    def prepared(self):
        """Server-side prepared statements aggregated by text (not visible in the digest table)."""
        try:
            return self.dict_rows(
                "SELECT LEFT(SQL_TEXT, 400) AS DIGEST_TEXT, SUM(COUNT_EXECUTE) AS COUNT_STAR, SUM(SUM_TIMER_EXECUTE) AS SUM_TIMER_WAIT, "
                "SUM(SUM_LOCK_TIME) AS SUM_LOCK_TIME, SUM(SUM_ROWS_EXAMINED) AS SUM_ROWS_EXAMINED, SUM(SUM_ROWS_SENT) AS SUM_ROWS_SENT, "
                "SUM(SUM_ROWS_AFFECTED) AS SUM_ROWS_AFFECTED, SUM(SUM_CREATED_TMP_DISK_TABLES) AS SUM_CREATED_TMP_DISK_TABLES, "
                "SUM(SUM_CREATED_TMP_TABLES) AS SUM_CREATED_TMP_TABLES, SUM(SUM_SORT_MERGE_PASSES) AS SUM_SORT_MERGE_PASSES, "
                "SUM(SUM_NO_INDEX_USED) AS SUM_NO_INDEX_USED, SUM(SUM_SELECT_FULL_JOIN) AS SUM_SELECT_FULL_JOIN, "
                "MIN(OWNER_OBJECT_SCHEMA) AS SCHEMA_NAME FROM performance_schema.prepared_statements_instances "
                "GROUP BY LEFT(SQL_TEXT, 400) ORDER BY SUM(SUM_TIMER_EXECUTE) DESC LIMIT 200")
        except pymysql.MySQLError:
            return []

    def waits(self, limit=300):
        try:
            return {r[0]: (int(r[1]), int(r[2])) for r in self.rows(
                "SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name "
                "WHERE COUNT_STAR > 0 AND EVENT_NAME <> 'idle' ORDER BY SUM_TIMER_WAIT DESC LIMIT %s", (limit,))}
        except pymysql.MySQLError:
            return {}

    def wait_coverage(self):
        """How much of the wait instrumentation is on: {instruments, enabled, sync_enabled}."""
        try:
            r = self.rows("SELECT COUNT(*), SUM(ENABLED='YES' AND TIMED='YES'), SUM(NAME LIKE 'wait/synch/%%' AND ENABLED='YES' AND TIMED='YES') "
                          "FROM performance_schema.setup_instruments WHERE NAME LIKE 'wait/%%'")[0]
            return {"instruments": int(r[0] or 0), "enabled": int(r[1] or 0), "sync_enabled": int(r[2] or 0)}
        except pymysql.MySQLError:
            return None

    def set_sync_waits(self, on):
        """Runtime only (lost at restart): mutex / rwlock / condition wait instruments. Costs some throughput."""
        v = "YES" if on else "NO"
        return self.execute(f"UPDATE performance_schema.setup_instruments SET ENABLED='{v}', TIMED='{v}' WHERE NAME LIKE 'wait/synch/%%'")

    def variables_meta(self):
        """{name: {source, path}} - where each global value came from."""
        out = {}
        try:
            for name, src, path in self.rows("SELECT VARIABLE_NAME, VARIABLE_SOURCE, VARIABLE_PATH FROM performance_schema.variables_info"):
                out[name.lower()] = {"source": src, "path": path or None}
            return out
        except pymysql.MySQLError:
            pass
        try:  # MariaDB
            for name, origin, path, default in self.rows(
                    "SELECT VARIABLE_NAME, GLOBAL_VALUE_ORIGIN, GLOBAL_VALUE_PATH, DEFAULT_VALUE FROM information_schema.SYSTEM_VARIABLES"):
                out[name.lower()] = {"source": origin, "path": path or None, "default": default}
        except pymysql.MySQLError:
            pass
        return out

    def table_sizes(self):
        try:
            try:  # MySQL 8.0+ caches table statistics for 24 h by default: a freshly loaded data set would read as empty
                self.execute("SET SESSION information_schema_stats_expiry = 0")
            except pymysql.MySQLError:
                pass  # MariaDB / older servers: no cache
            r = self.rows(
                "SELECT COALESCE(SUM(data_length+index_length),0), COUNT(*) FROM information_schema.TABLES "
                "WHERE ENGINE='InnoDB' AND TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')")
            return int(r[0][0]), int(r[0][1])
        except pymysql.MySQLError:
            return None, None


def flavor_of(variables):
    ver = variables.get("version", "")
    comment = variables.get("version_comment", "")
    if "MariaDB" in ver or "mariadb" in comment.lower():
        flavor = "mariadb"
    elif "Percona XtraDB Cluster" in comment:
        flavor = "pxc"
    elif "Percona" in comment:
        flavor = "percona"
    else:
        flavor = "mysql"
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", ver)
    vt = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return flavor, vt, ver, comment


def parse_innodb_status(text):
    """Pull a few numbers from SHOW ENGINE INNODB STATUS."""
    out = {}
    if not text:
        return out
    m = re.search(r"Log sequence number\s+(\d+)", text)
    if m:
        out["lsn"] = int(m.group(1))
    m = re.search(r"Last checkpoint at\s+(\d+)", text)
    if m:
        out["checkpoint_lsn"] = int(m.group(1))
    m = re.search(r"History list length\s+(\d+)", text)
    if m:
        out["history_list_length"] = int(m.group(1))
    m = re.search(r"OS WAIT ARRAY INFO: reservation count (\d+)", text)
    if m:
        out["os_wait_reservations"] = int(m.group(1))
    m = re.search(r"(\d+) queries inside InnoDB, (\d+) queries in queue", text)
    if m:
        out["queries_inside"] = int(m.group(1))
        out["queries_queue"] = int(m.group(2))
    return out


def section(text, title, maxlen=4000):
    """Extract one section of the InnoDB status text."""
    if not text:
        return ""
    m = re.search(r"-+\n" + re.escape(title) + r"\n-+\n(.*?)(?:\n-{3,}\n[A-Z ]+\n-{3,}|\Z)", text, re.S)
    return m.group(1)[:maxlen].strip() if m else ""


def compiled_defaults(binary_candidates=("/usr/sbin/mysqld", "/usr/sbin/mariadbd", "/usr/libexec/mysqld", "/usr/bin/mysqld")):
    """Compiled-in defaults from `mysqld --no-defaults --verbose --help` (no server start)."""
    import os
    import subprocess
    for b in binary_candidates:
        if not os.path.exists(b):
            continue
        try:
            r = subprocess.run([b, "--no-defaults", "--verbose", "--help"], capture_output=True, text=True, timeout=60)
        except Exception:
            continue
        out, started = {}, False
        for line in r.stdout.splitlines():
            if line.startswith("Variables (--variable-name=value)"):
                started = True
                continue
            if not started or line.startswith("---") or not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                v = parts[1].strip()
                v = {"TRUE": "ON", "FALSE": "OFF", "(No default value)": ""}.get(v, v)
                out[parts[0].replace("-", "_").lower()] = v
            elif len(parts) == 1:
                out[parts[0].replace("-", "_").lower()] = ""
        if out:
            return out
    return {}


def redo_capacity(variables):
    cap = variables.get("innodb_redo_log_capacity")
    if cap:
        return int(cap)
    fs = int(variables.get("innodb_log_file_size") or 0)
    n = int(variables.get("innodb_log_files_in_group") or 1)
    return fs * n
