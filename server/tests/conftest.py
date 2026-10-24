"""Fixtures of the server tests: temporary databases, a TestClient, accounts, API keys and agent data.

The environment is set before the app is imported: every database (tuner.db, learning.db, status.db) lives in a
temporary directory, background threads (status sampler, watchdog, learner) do not start, and nothing reaches the
network (OpenRouter calls fail inside the test unless a test stubs them).
"""
import atexit
import os
import shutil
import sys
import tempfile
import time

from cryptography.fernet import Fernet

_TMP = tempfile.mkdtemp(prefix="aituner-test-")
atexit.register(shutil.rmtree, _TMP, True)
# forced, not defaulted: a TUNER_DB inherited from the service environment must never be the one the tests write to
os.environ["TUNER_DB"] = os.path.join(_TMP, "tuner.db")
os.environ["TUNER_LEARNING_DB"] = os.path.join(_TMP, "learning.db")
os.environ["TUNER_NO_BACKGROUND"] = "1"
os.environ["TUNER_PUBLIC_URL"] = "http://testserver"
os.environ["TUNER_SECRET_KEY"] = Fernet.generate_key().decode()  # a key of these tests only, never the service's
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import learner, llm, main, status, store  # noqa: E402

assert store.DB_PATH.startswith(_TMP) and learner.DB_PATH.startswith(_TMP) and status.PATH.startswith(_TMP)

PASSWORD = "correct horse battery"
BUILD = "1.0.0+" + main._agent_build()  # an agent of the server's build (others are recorded as stale)


def _wipe(c):
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        c.execute(f"DELETE FROM {t}")
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        c.execute("DELETE FROM sqlite_sequence")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Every test starts from empty databases and caches, with the network cut off."""
    _wipe(store.conn())
    _wipe(learner.db())
    sc = status._db()
    _wipe(sc)
    sc.close()
    main._STEAL_CACHE.clear()
    main._nb_cache.clear()
    llm._key_cache.update(t=0, data=None)

    def no_network(*a, **k):
        raise RuntimeError("network disabled in tests")
    monkeypatch.setattr(llm, "_request", no_network)
    monkeypatch.setattr(llm, "models", lambda: [])
    yield


def ago(seconds):
    return time.time() - seconds


class User:
    """An account: .ui is logged in with the session cookie, .agent sends its API key."""

    def __init__(self, email, uid, key, admin):
        self.email, self.id, self.key, self.admin = email, uid, key, admin
        self.ui = TestClient(main.app)
        r = self.ui.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.agent = TestClient(main.app, headers={"Authorization": f"Bearer {key}"})

    # ------------------------------------------------------------------ agent side, as tuning_agent/cli.py sends it
    def hello(self, uid="agent-1", name="db1", cpus=4, ram_gb=16, flavor="percona", version="8.4.3-3", **facts):
        f = {"hardware": {"hostname": name, "cpus": cpus, "ram_bytes": ram_gb * 2 ** 30, "disk_rotational": False, "numa_nodes": 1},
             "db": {"flavor": flavor, "version": version, "version_comment": "Percona Server (GPL)", "data_bytes": 10 * 2 ** 30}}
        f.update(facts)
        r = self.agent.post("/api/v1/agent/hello", json={"agent_uid": uid, "name": name, "agent_version": "1.0.0", "facts": f})
        assert r.status_code == 200, r.text
        return r.json()["environment_id"]

    def start_session(self, env_id, **kw):
        body = {"environment_id": env_id, "goal": "tps", "policy": "safe", "agent_version": BUILD, "agent_time": time.time(),
                "ntp_synchronized": True, "noise_pct": 2.0, "workload": "sysbench oltp_read_write", "os_tuning": True, "planner": False}
        body.update(kw)
        r = self.agent.post("/api/v1/agent/sessions", json=body)
        assert r.status_code == 200, r.text
        return r.json()["session_id"]

    def run(self, sid, seq, label, goal_value, managed=None, changes=None, tradeoffs=None, verdict="neutral", summary=None,
            evaluation=None, inputs=None, started=None, duration=600, finish=True, workload="oltp-write"):
        """One measurement: POST /runs (start) and POST /runs/{id}/finish, like `measure start` / `measure stop`."""
        started = started or ago(10000 - seq * 700)
        r = self.agent.post("/api/v1/agent/runs", json={
            "session_id": sid, "seq": seq, "label": label, "notes": None, "started": started,
            "config": {"innodb_buffer_pool_size": "4294967296", **(managed or {})}, "managed": managed or {}, "changes": changes or [],
            "config_meta": {}, "tradeoffs": tradeoffs or []})
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]
        if finish:
            inp = {"workload": {"type": workload}}
            inp.update(inputs or {})
            r = self.agent.post(f"/api/v1/agent/runs/{rid}/finish", json={
                "ended": started + duration, "summary": {"threads_connected_avg": 16, **(summary or {})}, "external": {}, "inputs": inp,
                "diagnosis": [], "goal_value": goal_value, "goal_source": "bench", "delta_pct": None, "delta_vs_first_pct": None,
                "verdict": verdict, "evaluation": evaluation or {}, "notes": None})
            assert r.status_code == 200, r.text
        return rid

    def recommend(self, sid, run_id, items=None, decision=None, nxt=None):
        r = self.agent.post("/api/v1/agent/recommendations", json={
            "session_id": sid, "run_id": run_id, "goal": "tps", "policy": "safe", "items": items or [], "diagnosis": [],
            "next": nxt, "workload": {"type": "oltp-write"}, "phase": "explore", "decision": decision, "llm": None})
        assert r.status_code == 200, r.text
        return r.json()["recommendation_id"]

    def close(self, sid):
        r = self.agent.post(f"/api/v1/agent/sessions/{sid}/close", json={"summary": {"runs": 3}})
        assert r.status_code == 200, r.text


_n = [0]


@pytest.fixture
def make_user():
    """make_user(admin=False) -> User (registered, logged in, with an API key)."""
    def make(admin=False, email=None):
        _n[0] += 1
        email = email or f"user{_n[0]}@test.example"
        c = TestClient(main.app)
        r = c.post("/api/auth/register", json={"email": email, "password": PASSWORD, "name": f"User {_n[0]}"})
        assert r.status_code == 200, r.text
        uid = store.q1("SELECT id FROM users WHERE email=?", (email,))["id"]
        if admin:
            store.ex("UPDATE users SET is_admin=1 WHERE id=?", (uid,))
        return User(email, uid, r.json()["api_key"], admin)
    return make


@pytest.fixture
def admin(make_user):
    return make_user(admin=True)


@pytest.fixture
def user(make_user):
    return make_user()


@pytest.fixture
def client():
    return TestClient(main.app)


IO = {"variable": "innodb_io_capacity", "value": "2000"}
TRX = {"variable": "innodb_flush_log_at_trx_commit", "value": "2", "category": "durability",
       "lost": "Durability: up to 1 s of transactions lost on an OS crash."}
SYNC = {"variable": "sync_binlog", "value": "0", "category": "durability", "lost": "Binary log may lose transactions on a crash."}


@pytest.fixture
def tuned(user):
    """A typical session: server defaults (#1), a baseline with innodb_flush_log_at_trx_commit=2 set up by the benchmark
    (#2, a start trade-off), an io_capacity experiment (#3, safe) and sync_binlog=0 on top (#4, an added trade-off)."""
    env = user.hello()
    sid = user.start_session(env)
    base = {"innodb_flush_log_at_trx_commit": "2"}
    r1 = user.run(sid, 1, "initial", 800.0)
    r2 = user.run(sid, 2, "baseline", 1000.0, managed=base, tradeoffs=[TRX])
    r3 = user.run(sid, 3, "run-3", 1100.0, managed=dict(base, innodb_io_capacity="2000"), tradeoffs=[TRX], verdict="improved",
                  changes=[{"variable": "innodb_io_capacity", "old": "200", "new": "2000", "source": "rec:io-capacity", "reason": "flush-bound"}],
                  evaluation={"goal_delta_pct": 10.0})
    r4 = user.run(sid, 4, "run-4", 1300.0, managed=dict(base, innodb_io_capacity="2000", sync_binlog="0"), tradeoffs=[TRX, SYNC],
                  verdict="improved", changes=[{"variable": "sync_binlog", "old": "1", "new": "0", "source": "rec:sync-binlog", "reason": "commit-bound"}],
                  evaluation={"goal_delta_pct": 18.2})
    return {"user": user, "env": env, "sid": sid, "runs": [r1, r2, r3, r4]}
