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
  id INTEGER PRIMARY KEY AUTOINCREMENT, env_id INTEGER NOT NULL, name TEXT, goal TEXT, policy TEXT, noise_pct REAL, workload TEXT, notes TEXT,
  status TEXT NOT NULL DEFAULT 'active', created REAL NOT NULL, ended REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, seq INTEGER, label TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'measuring',
  started REAL, ended REAL, config TEXT, managed TEXT, changes TEXT, summary TEXT, external TEXT, inputs TEXT, diagnosis TEXT,
  goal_value REAL, goal_source TEXT, delta_pct REAL, delta_vs_first_pct REAL, verdict TEXT);
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id, seq);
CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, run_id INTEGER, created REAL NOT NULL, goal TEXT, policy TEXT,
  items TEXT, diagnosis TEXT, next TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, env_id INTEGER NOT NULL, session_id INTEGER, ts REAL NOT NULL, type TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_env ON events(env_id, ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, ts);
CREATE TABLE IF NOT EXISTS param_policy (
  user_id INTEGER NOT NULL, variable TEXT NOT NULL, mode TEXT NOT NULL, note TEXT, updated REAL NOT NULL,
  PRIMARY KEY (user_id, variable));
CREATE TABLE IF NOT EXISTS learning_snapshots (
  id INTEGER PRIMARY KEY, created REAL NOT NULL, created_by INTEGER, label TEXT, runs INTEGER, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, env_id INTEGER, session_id INTEGER, ts REAL NOT NULL, model TEXT, prompt_hash TEXT,
  status TEXT, cached INTEGER DEFAULT 0, latency_s REAL, prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL, response TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS llm_calls_hash ON llm_calls(user_id, prompt_hash);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, created REAL NOT NULL, created_by TEXT, kind TEXT NOT NULL, spec TEXT NOT NULL,
  repeats INTEGER DEFAULT 1, reason TEXT, force INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued', results TEXT, message TEXT, updated REAL);
CREATE INDEX IF NOT EXISTS experiments_session ON experiments(session_id, status);
CREATE TABLE IF NOT EXISTS hosts (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, env_id INTEGER NOT NULL, agent_uid TEXT NOT NULL, name TEXT, role TEXT NOT NULL DEFAULT 'client',
  facts TEXT, created REAL NOT NULL, last_seen REAL, UNIQUE(user_id, agent_uid));
CREATE TABLE IF NOT EXISTS host_metrics (
  host_id INTEGER NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY (host_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT, host_id INTEGER NOT NULL, session_id INTEGER, created REAL NOT NULL, action TEXT NOT NULL, payload TEXT,
  status TEXT NOT NULL DEFAULT 'queued', result TEXT, updated REAL);
CREATE INDEX IF NOT EXISTS commands_host ON commands(host_id, status);
CREATE TABLE IF NOT EXISTS shares (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, object_id INTEGER NOT NULL,
  label TEXT, created REAL NOT NULL, revoked REAL, last_used REAL, uses INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS studies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'compare', goal TEXT,
  target REAL, workload TEXT, notes TEXT, created REAL NOT NULL);
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
    ("tsessions", "goal_metric", "TEXT"),
    ("runs", "abort_reason", "TEXT"),
    ("tsessions", "os_tuning", "INTEGER"),
    ("users", "llm_key_enc", "TEXT"),
    ("users", "llm_model", "TEXT"),
    ("users", "llm_mode", "TEXT"),
    ("recommendations", "llm", "TEXT"),
    ("tsessions", "study_id", "INTEGER"),
    ("environments", "cost_month", "REAL"),
    ("environments", "cost_hourly", "REAL"),
    ("environments", "provisioned_at", "REAL"),
    ("environments", "destroyed_at", "REAL"),
    ("environments", "plan", "TEXT"),
    ("environments", "archive_hold", "INTEGER"),
    ("environments", "agent_state", "TEXT"),
    ("environments", "agent_state_at", "REAL"),
    ("hosts", "destroyed_at", "REAL"),
    ("llm_calls", "context", "TEXT"),
    ("environments", "activity", "TEXT"),
    ("environments", "activity_at", "REAL"),
    ("llm_calls", "purpose", "TEXT"),
    ("llm_calls", "signature", "TEXT"),
    ("users", "is_admin", "INTEGER DEFAULT 0"),
    ("tsessions", "agent_version", "TEXT"),
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
