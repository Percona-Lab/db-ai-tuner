"""Local agent state: JSON document + SQLite metric buffer."""
import json
import os
import sqlite3
import time

DEFAULT_DIR = os.environ.get("TUNING_AGENT_STATE", "/var/lib/tuning-agent")


class State:
    def __init__(self, dirpath=DEFAULT_DIR):
        self.dir = dirpath
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "state.json")
        self.data = {}
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("managed", {})
        self.data.setdefault("pending_changes", [])
        return self

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, default=str)
        os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.data.get(k)

    def get(self, k, d=None):
        return self.data.get(k, d)

    def set(self, k, v):
        self.data[k] = v
        self.save()


class SampleStore:
    def __init__(self, dirpath=DEFAULT_DIR):
        os.makedirs(dirpath, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(dirpath, "samples.db"), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, data TEXT, sent INTEGER DEFAULT 0)")
        self.db.execute("CREATE INDEX IF NOT EXISTS samples_sent ON samples(sent, ts)")
        self.db.commit()

    def add(self, sample):
        self.db.execute("INSERT OR REPLACE INTO samples(ts, data, sent) VALUES (?,?,0)", (sample["ts"], json.dumps(sample)))
        self.db.commit()

    def unsent(self, limit=500):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM samples WHERE sent=0 ORDER BY ts LIMIT ?", (limit,))]

    def mark_sent(self, ts_list):
        self.db.executemany("UPDATE samples SET sent=1 WHERE ts=?", [(t,) for t in ts_list])
        self.db.commit()

    def window(self, t0, t1):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM samples WHERE ts>=? AND ts<=? ORDER BY ts", (t0, t1))]

    def latest_ts(self):
        r = self.db.execute("SELECT MAX(ts) FROM samples").fetchone()
        return r[0] if r else None

    def prune(self, keep_s=3 * 86400):
        self.db.execute("DELETE FROM samples WHERE ts < ?", (time.time() - keep_s,))
        self.db.commit()
