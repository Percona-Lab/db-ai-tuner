"""SQLite storage."""
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("TUNER_DB", "/var/lib/aituner/tuner.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, pw_hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS logins (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
  created REAL NOT NULL, last_used REAL, revoked REAL);
CREATE TABLE IF NOT EXISTS environments (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, agent_uid TEXT NOT NULL, name TEXT, hostname TEXT, agent_version TEXT,
  facts TEXT, created REAL NOT NULL, last_seen REAL, api_key_id INTEGER, UNIQUE(user_id, agent_uid));
CREATE TABLE IF NOT EXISTS tsessions (
  id INTEGER PRIMARY KEY, env_id INTEGER NOT NULL, name TEXT, goal TEXT, policy TEXT, noise_pct REAL, workload TEXT, notes TEXT,
  status TEXT NOT NULL DEFAULT 'active', created REAL NOT NULL, ended REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, seq INTEGER, label TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'measuring',
  started REAL, ended REAL, config TEXT, managed TEXT, changes TEXT, summary TEXT, external TEXT, inputs TEXT, diagnosis TEXT,
  goal_value REAL, goal_source TEXT, delta_pct REAL, delta_vs_first_pct REAL, verdict TEXT);
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id, seq);
CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, run_id INTEGER, created REAL NOT NULL, goal TEXT, policy TEXT,
  items TEXT, diagnosis TEXT, next TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, env_id INTEGER NOT NULL, session_id INTEGER, ts REAL NOT NULL, type TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_env ON events(env_id, ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, ts);
CREATE TABLE IF NOT EXISTS metrics (
  env_id INTEGER NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY (env_id, ts)) WITHOUT ROWID;
"""

_local = threading.local()


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


MIGRATIONS = [
    ("runs", "evaluation", "TEXT"),
    ("tsessions", "calibration", "TEXT"),
    ("runs", "config_meta", "TEXT"),
    ("runs", "tradeoffs", "TEXT"),
    ("recommendations", "workload", "TEXT"),
    ("recommendations", "phase", "TEXT"),
]


def init():
    c = conn()
    c.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:  # another worker migrated first
                pass


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    return conn().execute(sql, args).fetchone()


def ex(sql, args=()):
    cur = conn().execute(sql, args)
    return cur.lastrowid


def j(v):
    return json.dumps(v, default=str) if v is not None else None


def uj(v, default=None):
    if v is None:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


def row(r, json_fields=()):
    if r is None:
        return None
    d = dict(r)
    for f in json_fields:
        if f in d:
            d[f] = uj(d[f])
    return d
