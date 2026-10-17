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
                "SELECT SUM(COUNT_STAR), SUM(SUM_TIMER_WAIT) FROM performance_schema.events_statements_summary_global_by_event_type "
                "WHERE EVENT_NAME LIKE 'statement/sql/%' OR EVENT_NAME LIKE 'statement/com/Execute%'")
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

    def waits(self, limit=300):
        try:
            return {r[0]: (int(r[1]), int(r[2])) for r in self.rows(
                "SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name "
                "WHERE COUNT_STAR > 0 AND EVENT_NAME <> 'idle' ORDER BY SUM_TIMER_WAIT DESC LIMIT %s", (limit,))}
        except pymysql.MySQLError:
            return {}

    def table_sizes(self):
        try:
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


def redo_capacity(variables):
    cap = variables.get("innodb_redo_log_capacity")
    if cap:
        return int(cap)
    fs = int(variables.get("innodb_log_file_size") or 0)
    n = int(variables.get("innodb_log_files_in_group") or 1)
    return fs * n
