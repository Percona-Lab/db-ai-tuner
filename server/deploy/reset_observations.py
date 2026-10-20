#!/usr/bin/env python3
"""Reset the observations of a tuner installation, keeping accounts and learnings.

usage (as root, with the web app stopped): reset_observations.py [--db /var/lib/aituner/tuner.db] [--yes]

1. Online backup of tuner.db and learning.db (sqlite backup API) next to them: backup-before-reset-<ts>.db /
   learning-before-reset-<ts>.db. Nothing is deleted unless both backups completed and verify.
2. Kept: users, logins, api_keys, environments, hosts, param_policy, learning_snapshots (and learning.db untouched).
   Emptied: sessions, runs, recommendations, events, llm_calls, experiments, commands, shares, studies, metrics,
   host_metrics.
3. Emptied tables with ids are recreated with AUTOINCREMENT and their id sequence starts above the old maximum:
   learning.db keys evidence by session id, so new sessions must never reuse an id of a session it already counted.
"""
import argparse
import os
import sqlite3
import sys
import time

KEEP = {"users", "logins", "api_keys", "environments", "hosts", "param_policy", "learning_snapshots"}
EMPTY = ["tsessions", "runs", "recommendations", "events", "llm_calls", "experiments", "commands", "shares", "studies", "metrics",
         "host_metrics"]


def backup(src, dst):
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    ok = d.execute("PRAGMA quick_check").fetchone()[0]
    d.close()
    if ok != "ok":
        sys.exit(f"backup {dst} failed its integrity check: {ok}")
    return os.path.getsize(dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/var/lib/aituner/tuner.db")
    ap.add_argument("--learning", default="/var/lib/aituner/learning.db")
    ap.add_argument("--yes", action="store_true", help="do it (otherwise: show what would happen)")
    a = ap.parse_args()
    c = sqlite3.connect(a.db, isolation_level=None)
    tables = {r[0]: r[1] for r in c.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    unknown = set(tables) - KEEP - set(EMPTY)
    if unknown:
        sys.exit(f"tables neither kept nor emptied: {sorted(unknown)} - decide first")
    for t in EMPTY:
        if t in tables:
            n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  empty {t:<16} {n:>10} rows")
    for t in sorted(KEEP & set(tables)):
        print(f"  keep  {t:<16} {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>10} rows")
    if not a.yes:
        print("dry run: pass --yes to back up and reset")
        return
    ts = time.strftime("%Y%m%dT%H%M", time.gmtime())
    d = os.path.dirname(a.db)
    for src, name in ((a.db, f"backup-before-reset-{ts}.db"), (a.learning, f"learning-before-reset-{ts}.db")):
        if os.path.exists(src):
            print(f"backup {src} -> {name}: {backup(src, os.path.join(d, name)) / 2 ** 20:.0f} MiB, integrity ok")
    c.execute("BEGIN IMMEDIATE")
    try:
        for t in EMPTY:
            if t not in tables:
                continue
            sql = tables[t]
            has_id = "id INTEGER PRIMARY KEY" in sql
            top = c.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0] if has_id else 0
            if has_id and "AUTOINCREMENT" not in sql:
                idx = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,))]
                c.execute(f"DROP TABLE {t}")
                c.execute(sql.replace("id INTEGER PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT", 1))
                for s in idx:
                    c.execute(s)
            else:
                c.execute(f"DELETE FROM {t}")
            if has_id:
                c.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))
                c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (t, top))
                print(f"  {t}: emptied, ids continue after {top}")
            else:
                print(f"  {t}: emptied")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    c.execute("VACUUM")
    print(f"done: {os.path.getsize(a.db) / 2 ** 20:.0f} MiB")


if __name__ == "__main__":
    main()
