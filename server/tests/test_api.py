"""Server tests: the HTTP API (accounts, agent flow, session and run pages, share links, feedback), the reference and
trade-off rules behind the headline numbers, and the background processes (dead-session cleanup, stall watchdog,
status page, learner). Run with server/tests/run.sh; fixtures in conftest.py."""
import json
import time

import pytest

from app import learner, llm, main, status, store
from conftest import BUILD, IO, TRX, ago


def sess_row(sid):
    return dict(store.q1("SELECT * FROM tsessions WHERE id=?", (sid,)))


# ============================================================================ accounts and access

def test_register_validates(client):
    assert client.post("/api/auth/register", json={"email": "nope", "password": "longenough"}).status_code == 400
    assert client.post("/api/auth/register", json={"email": "a@b.example", "password": "short"}).status_code == 400
    r = client.post("/api/auth/register", json={"email": "A@B.example", "password": "longenough"})
    assert r.status_code == 200 and r.json()["api_key"].startswith("atk_")
    assert client.post("/api/auth/register", json={"email": "a@b.example", "password": "longenough"}).status_code == 409


def test_login_logout(user, client):
    assert client.get("/api/me").json() == {"user": None}
    assert client.post("/api/auth/login", json={"email": user.email, "password": "wrong password"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": user.email.upper(), "password": "correct horse battery"}).status_code == 200
    me = client.get("/api/me").json()["user"]
    assert me["email"] == user.email and me["is_admin"] is False
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").json()["user"] is None
    assert client.get("/api/environments").status_code == 401


def test_admin_flag_and_admin_only_endpoints(admin, user):
    assert admin.ui.get("/api/me").json()["user"]["is_admin"] is True
    for path in ("/api/admin/learner", "/api/admin/learning", "/status.json"):
        assert user.ui.get(path).status_code == 403, path
        assert admin.ui.get(path).status_code == 200, path
    assert user.ui.put("/api/settings/llm", json={"mode": "off"}).status_code == 403
    assert admin.ui.put("/api/settings/llm", json={"mode": "bogus"}).status_code == 400


def test_other_accounts_objects_are_404(tuned, make_user):
    other = make_user()
    sid, rid, env = tuned["sid"], tuned["runs"][0], tuned["env"]
    for path in (f"/api/environments/{env}", f"/api/sessions/{sid}", f"/api/runs/{rid}", f"/api/runs/{rid}/config",
                 f"/api/sessions/{sid}/experiments", f"/api/events?env={env}"):
        assert other.ui.get(path).status_code == 404, path
    assert other.ui.patch(f"/api/sessions/{sid}", json={"name": "mine now"}).status_code == 404
    assert other.ui.delete(f"/api/sessions/{sid}").status_code == 404
    assert other.ui.post(f"/api/runs/{rid}/feedback", json={"verdict": "reasonable"}).status_code == 404
    assert other.ui.get("/api/environments").json()["environments"] == []
    # the agent API: another account's key cannot touch the session or its runs
    assert other.agent.post("/api/v1/agent/runs", json={"session_id": sid, "seq": 9}).status_code == 404
    assert other.agent.post(f"/api/v1/agent/runs/{rid}/finish", json={"goal_value": 1}).status_code == 404
    assert other.agent.post(f"/api/v1/agent/sessions/{sid}/close", json={}).status_code == 404
    assert other.agent.post("/api/v1/agent/metrics", json={"environment_id": env, "samples": []}).status_code == 404
    assert sess_row(sid)["status"] == "active"


def test_api_key_required_and_revocable(user, client):
    assert client.post("/api/v1/agent/hello", json={"agent_uid": "x"}).status_code == 401
    bad = client.post("/api/v1/agent/hello", json={"agent_uid": "x"}, headers={"Authorization": "Bearer atk_nope"})
    assert bad.status_code == 401
    kid = user.ui.get("/api/keys").json()["keys"][0]["id"]
    assert user.ui.delete(f"/api/keys/{kid}").status_code == 200
    assert user.agent.post("/api/v1/agent/hello", json={"agent_uid": "x"}).status_code == 401
    key = user.ui.post("/api/keys", json={"name": "second"}).json()["api_key"]
    assert client.post("/api/v1/agent/hello", json={"agent_uid": "x"}, headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_change_password(user, client):
    assert user.ui.post("/api/auth/password", json={"old": "wrong", "new": "another password"}).status_code == 400
    assert user.ui.post("/api/auth/password", json={"old": "correct horse battery", "new": "short"}).status_code == 400
    assert user.ui.post("/api/auth/password", json={"old": "correct horse battery", "new": "another password"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": user.email, "password": "another password"}).status_code == 200


# ============================================================================ read-only share links

def test_share_environment_read_only(tuned, client):
    u, env, sid = tuned["user"], tuned["env"], tuned["sid"]
    other_env = u.hello(uid="agent-2", name="db2")
    sh = u.ui.post("/api/shares", json={"kind": "environment", "id": env, "label": "for review"}).json()
    h = {"X-Share-Token": sh["token"]}
    me = client.get("/api/me", headers=h).json()["user"]
    assert me["share"] == {"kind": "environment", "id": env, "name": "db1", "label": "for review"}
    assert client.get(f"/api/environments/{env}", headers=h).status_code == 200
    s = client.get(f"/api/sessions/{sid}", headers=h)
    assert s.status_code == 200 and all(r["starred"] is False for r in s.json()["runs"])
    assert client.get(f"/api/runs/{tuned['runs'][2]}", headers=h).status_code == 200
    assert [e["id"] for e in client.get("/api/environments", headers=h).json()["environments"]] == [env]
    # outside its scope: another environment of the same owner
    assert client.get(f"/api/environments/{other_env}", headers=h).status_code == 404
    # GET only, and only the pages a shared object needs
    assert client.patch(f"/api/sessions/{sid}", json={"name": "x"}, headers=h).status_code == 403
    assert client.post(f"/api/runs/{tuned['runs'][2]}/feedback", json={"verdict": "reasonable"}, headers=h).status_code == 403
    assert client.get("/api/keys", headers=h).status_code == 403
    assert client.get("/api/notes", headers=h).status_code == 403
    assert store.q1("SELECT uses FROM shares WHERE id=?", (sh["id"],))["uses"] >= 4
    # revoked: refused
    u.ui.delete(f"/api/shares/{sh['id']}")
    assert client.get(f"/api/environments/{env}", headers=h).status_code == 401


def test_share_study_scope(tuned, client):
    u, env, sid = tuned["user"], tuned["env"], tuned["sid"]
    env2 = u.hello(uid="agent-2", name="db2")
    sid2 = u.start_session(env2)
    st = u.ui.post("/api/studies", json={"name": "Compare", "session_ids": [sid]}).json()["id"]
    h = {"X-Share-Token": u.ui.post("/api/shares", json={"kind": "study", "id": st}).json()["token"]}
    assert client.get(f"/api/studies/{st}", headers=h).json()["other_sessions"] == []
    assert client.get(f"/api/sessions/{sid}", headers=h).status_code == 200
    assert client.get(f"/api/sessions/{sid2}", headers=h).status_code == 404
    assert client.get(f"/api/environments/{env}", headers=h).status_code == 200
    assert client.get(f"/api/environments/{env2}", headers=h).status_code == 404


def test_share_needs_own_object(tuned, make_user):
    other = make_user()
    assert other.ui.post("/api/shares", json={"kind": "environment", "id": tuned["env"]}).status_code == 404
    assert other.ui.post("/api/shares", json={"kind": "run", "id": 1}).status_code == 400


# ============================================================================ agent API flow

def test_hello_registers_and_updates(user):
    env = user.hello()
    assert user.hello(name="db1", cpus=8) == env  # the same agent: the same environment, facts refreshed
    e = user.ui.get(f"/api/environments/{env}").json()
    assert e["environment"]["cpus"] == 8 and e["environment"]["db_short"] == "Percona Server 8.4.3-3"
    assert e["environment"]["online"] is True
    assert any(ev["message"] == "environment registered" for ev in e["events"])
    assert user.agent.post("/api/v1/agent/hello", json={"facts": {}}).status_code == 400


def test_session_start_abandons_previous(user):
    env = user.hello()
    s1 = user.start_session(env)
    s2 = user.start_session(env, name="second")
    assert sess_row(s1)["status"] == "abandoned" and sess_row(s1)["ended"]
    s = sess_row(s2)
    assert s["status"] == "active" and s["name"] == "second" and s["policy"] == "safe" and s["os_tuning"] == 1


def test_session_start_clock_skew(user):
    env = user.hello()
    r = user.agent.post("/api/v1/agent/sessions", json={"environment_id": env, "agent_time": time.time() + 120})
    assert r.status_code == 400 and "clock" in r.json()["detail"]
    r = user.agent.post("/api/v1/agent/sessions", json={"environment_id": env, "agent_time": time.time() + 5, "ntp_synchronized": False})
    assert r.status_code == 200 and "clock is +5" in r.json()["warning"] and "not synchronised" in r.json()["warning"]


def test_session_start_with_study(user):
    env = user.hello()
    r = user.agent.post("/api/v1/agent/sessions", json={"environment_id": env, "study": "Scaling", "goal": "latency"}).json()
    assert r["study"]["name"] == "Scaling" and r["name"].endswith("(latency)")
    again = user.agent.post("/api/v1/agent/sessions", json={"environment_id": env, "study": "Scaling"}).json()
    assert again["study"]["id"] == r["study"]["id"]


def test_measurement_recommendation_and_stop(user):
    env = user.hello()
    sid = user.start_session(env)
    r1 = user.run(sid, 1, "baseline", 1000.0)
    items = [{"id": "io-capacity", "kind": "config", "confidence": 0.7, "safety": "safe",
              "changes": [{"variable": "innodb_io_capacity", "value": "2000"}], "reason": "flush-bound"}]
    decision = {"by": "engine", "next": "io-capacity", "why": "flush wait 31% of time", "rejected": [{"id": "bp-size", "why": "no memory"}]}
    rec = user.recommend(sid, r1, items=items, decision=decision, nxt="io-capacity")
    rid2 = user.run(sid, 2, "run-2", None, finish=False,
                    changes=[{"variable": "innodb_io_capacity", "old": "200", "new": "2000", "source": "rec:io-capacity"}])
    s = user.ui.get(f"/api/sessions/{sid}").json()
    assert s["recommendations"][0]["id"] == rec
    assert s["recommendations"][0]["items"] == items and s["recommendations"][0]["decision"] == decision
    assert s["session"]["activity"]["phase"] == "measuring"  # derived from the run in progress
    assert [r["status"] for r in s["runs"]] == ["done", "measuring"]
    run = user.ui.get(f"/api/runs/{r1}").json()
    assert run["recommendations"][0]["decision"] == decision and run["recommendations"][0]["next"] == "io-capacity"
    assert any(e["type"] == "recommend" for e in s["events"])
    # an aborted run: kept with its reason, out of the list of comparable runs
    assert user.agent.post(f"/api/v1/agent/runs/{rid2}/abort", json={"reason": "benchmark failed"}).status_code == 200
    assert user.ui.get(f"/api/runs/{rid2}").json()["abort_reason"] == "benchmark failed"
    user.close(sid)
    s = user.ui.get(f"/api/sessions/{sid}").json()["session"]
    assert s["status"] == "closed" and s["summary"] == {"runs": 3} and s["activity"] is None
    act = json.loads(store.q1("SELECT activity FROM environments WHERE id=?", (env,))["activity"])
    assert act["phase"] == "idle"


def test_recommendation_needs_own_run(tuned, make_user):
    other = make_user()
    env = other.hello(uid="other-agent")
    sid = other.start_session(env)
    r = other.agent.post("/api/v1/agent/recommendations", json={"session_id": sid, "run_id": tuned["runs"][0], "items": []})
    assert r.status_code == 404


def test_session_update_calibration_and_metrics(user):
    env = user.hello()
    sid = user.start_session(env)
    cal = {"goal_mean": 1234.5, "goal_cv_pct": 1.1, "noise_pct": 1.5, "runs": [1, 2, 3], "plan": {"action": "ok"},
           "data_state": {"status": "ok"}}
    assert user.agent.post(f"/api/v1/agent/sessions/{sid}/update", json={"noise_pct": 1.5, "goal_metric": "tpm", "calibration": cal}).status_code == 200
    s = user.ui.get(f"/api/sessions/{sid}").json()["session"]
    assert s["noise_pct"] == 1.5 and s["goal_metric"] == "tpm" and s["calibration"] == cal
    t = time.time()
    samples = [{"ts": t - 20, "os": {"cpu_busy": 50.0}, "db": {"tps": 900.0}, "db_up": True}, {"ts": t - 10, "os": {"cpu_busy": 60.0}, "db": {"tps": 1100.0}},
               {"ts": "bad"}]
    assert user.agent.post("/api/v1/agent/metrics", json={"environment_id": env, "samples": samples}).json() == {"ok": True, "stored": 2}
    m = user.ui.get(f"/api/environments/{env}/metrics", params={"start": t - 60, "end": t}).json()
    assert len(m["ts"]) == 2 and m["db"]["tps"] == [900.0, 1100.0]


def test_agent_activity_and_bye(user):
    env = user.hello()
    sid = user.start_session(env)
    until = time.time() + 600
    user.agent.post("/api/v1/agent/activity", json={"environment_id": env, "phase": "warm-up", "detail": "run 1", "until": until, "session_id": sid})
    a = user.ui.get(f"/api/sessions/{sid}").json()["session"]["activity"]
    assert a["phase"] == "warm-up" and a["source"] == "agent" and not a.get("stale")
    user.agent.post("/api/v1/agent/bye", json={"environment_id": env, "host_shutdown": True})
    assert user.ui.get(f"/api/environments/{env}").json()["environment"]["agent_state"] == "shutdown"


def test_activity_of_a_closed_session_belongs_to_the_environment(user):
    """User (em-b212x, 357): a data load for the next session was shown as the closed session's activity."""
    env = user.hello()
    sid = user.start_session(env)
    user.close(sid)
    user.agent.post("/api/v1/agent/activity", json={"environment_id": env, "phase": "loading data", "detail": "sysbench prepare", "session_id": sid})
    a = user.ui.get(f"/api/environments/{env}").json()["activity"]
    assert a["phase"] == "loading data" and not a.get("session_id"), a


def test_experiments_queue(user):
    env = user.hello()
    sid = user.start_session(env)
    assert user.ui.post(f"/api/sessions/{sid}/experiments", json={"kind": "try", "settings": "nonsense"}).status_code == 400
    assert user.ui.post(f"/api/sessions/{sid}/experiments", json={"kind": "sweep", "variable": "x", "values": "1"}).status_code == 400
    eid = user.ui.post(f"/api/sessions/{sid}/experiments", json={"kind": "try", "settings": "innodb_io_capacity=2000, sync_binlog=0",
                                                                  "repeats": 9}).json()["id"]
    q = user.agent.get(f"/api/v1/agent/sessions/{sid}/experiments").json()["experiments"]
    assert [e["id"] for e in q] == [eid] and q[0]["repeats"] == 5
    assert q[0]["spec"] == {"changes": [{"variable": "innodb_io_capacity", "value": "2000"}, {"variable": "sync_binlog", "value": "0"}]}
    user.agent.post(f"/api/v1/agent/experiments/{eid}", json={"status": "done", "message": "measured", "results": {"runs": [3]}})
    assert user.agent.get(f"/api/v1/agent/sessions/{sid}/experiments").json()["experiments"] == []
    assert user.ui.get(f"/api/sessions/{sid}/experiments").json()["experiments"][0]["results"] == {"runs": [3]}


# ============================================================================ reference (start) and trade-offs

def test_session_results_start_is_baseline_and_best_safe(tuned):
    res = main.session_results(sess_row(tuned["sid"]))
    assert res["runs"] == 4
    assert res["first"] == {"value": 800.0, "label": "initial", "seq": 1}
    assert res["start"] == {"value": 1000.0, "basis": "baseline", "runs": [2]}
    # sync_binlog=0 (#4) is a trade-off the start did not have; trx_commit=2 was the benchmark's own (start) trade-off
    assert res["best"]["seq"] == 4 and res["best"]["safe"] is False and res["best"]["gain_pct"] == 30.0
    assert res["best_safe"]["seq"] == 3 and res["best_safe"]["mean"] == 1100.0
    assert res["best_safe"]["gain_pct"] == 10.0 and res["best_safe"]["gain_vs_first_pct"] == 37.5


def test_session_results_start_is_calibration_mean(tuned):
    u, sid = tuned["user"], tuned["sid"]
    u.agent.post(f"/api/v1/agent/sessions/{sid}/update", json={"calibration": {"goal_mean": 1050.0, "runs": [2]}})
    res = main.session_results(sess_row(sid))
    assert res["start"] == {"value": 1050.0, "basis": "calibration", "runs": [2]}
    assert res["best_safe"]["gain_pct"] == round(100 * (1100 - 1050) / 1050, 2)


def test_session_results_start_is_first_run_without_baseline(user):
    env = user.hello()
    sid = user.start_session(env)
    user.run(sid, 1, "initial", 500.0)
    user.run(sid, 2, "run-2", 550.0, managed={"innodb_io_capacity": "2000"}, verdict="improved")
    user.run(sid, 3, "run-3", 9999.0, verdict="unreliable")  # unreliable runs are no evidence for any number
    res = main.session_results(sess_row(sid))
    assert res["start"]["basis"] == "first" and res["start"]["value"] == 500.0 and res["runs"] == 2
    assert res["best_safe"]["seq"] == 2 and res["best_safe"]["gain_pct"] == 10.0


def test_session_results_latency_goal(user):
    env = user.hello()
    sid = user.start_session(env, goal="latency")
    user.run(sid, 1, "baseline", 20.0)
    user.run(sid, 2, "run-2", 15.0, managed={"innodb_io_capacity": "2000"})
    res = main.session_results(sess_row(sid))
    assert res["best"]["mean"] == 15.0 and res["best_safe"]["gain_pct"] == 25.0


def test_repeated_configuration_is_averaged(user):
    env = user.hello()
    sid = user.start_session(env)
    user.run(sid, 1, "baseline", 1000.0)
    user.run(sid, 2, "run-2", 1200.0, managed={"innodb_io_capacity": "2000"})
    user.run(sid, 3, "repeat", 1100.0, managed={"innodb_io_capacity": "2000"})
    bs = main.session_results(sess_row(sid))["best_safe"]
    assert bs["mean"] == 1150.0 and bs["n"] == 2 and bs["seq"] == 3


def test_environment_sessions_table_values(tuned):
    u = tuned["user"]
    s = u.ui.get(f"/api/environments/{tuned['env']}").json()["sessions"][0]
    assert s["runs"] == 4 and s["first_value"] == 800.0
    assert s["start_value"] == 1000.0 and s["start_basis"] == "baseline"
    assert s["best_value"] == 1100.0 and s["best_gain_pct"] == 10.0 and s["best_gain_vs_first_pct"] == 37.5
    assert s["best_tradeoff_value"] == 1300.0 and s["best_tradeoff_gain_pct"] == 30.0


def test_session_page_tradeoffs(tuned):
    d = tuned["user"].ui.get(f"/api/sessions/{tuned['sid']}").json()
    assert [t["variable"] for t in d["session"]["start_tradeoffs"]] == ["innodb_flush_log_at_trx_commit"]
    added = {r["seq"]: [t["variable"] for t in r["tradeoffs_added"]] for r in d["runs"]}
    assert added == {1: [], 2: [], 3: [], 4: ["sync_binlog"]}


def _mem(needed_gib, ram_gib=64, over=True):
    return {"memory_budget": {"over_budget": over, "needed": needed_gib * 2 ** 30, "ram": ram_gib * 2 ** 30,
                              "text": f"{needed_gib} of {ram_gib * 0.98:.1f} GiB needed"}, "mem_available_mb": 4096, "swap_total_mb": 0}


def test_memory_over_budget_at_start_is_a_start_tradeoff():
    runs = [{"seq": 1, "label": "baseline", "status": "done", "tradeoffs": [], "summary": _mem(69)},
            {"seq": 2, "label": "run-2", "status": "done", "tradeoffs": [], "summary": _mem(69.5)},   # +0.5 GiB of 64: within 1%
            {"seq": 3, "label": "run-3", "status": "done", "tradeoffs": [], "summary": _mem(71)},     # +2 GiB: the tuner's trade-off
            {"seq": 4, "label": "run-4", "status": "done", "tradeoffs": [], "summary": _mem(60, over=False)}]
    start = main.mark_added_tradeoffs(runs)
    assert [t["variable"] for t in start] == ["memory"] and "no swap configured" in start[0]["value"]
    assert [[t["variable"] for t in r["tradeoffs_added"]] for r in runs] == [[], [], ["memory"], []]


def test_memory_over_budget_added_by_the_tuner():
    have = set()
    r = {"tradeoffs": [TRX], "summary": _mem(70)}
    assert [t["variable"] for t in main.added_tradeoffs(r, have)] == ["innodb_flush_log_at_trx_commit", "memory"]
    assert main.added_tradeoffs(r, {("innodb_flush_log_at_trx_commit", "2")})[0]["variable"] == "memory"
    assert main.added_tradeoffs({"tradeoffs": [TRX], "summary": {}}, {("innodb_flush_log_at_trx_commit", "2")}) == []


def test_best_safe_excludes_memory_over_budget(user):
    env = user.hello()
    sid = user.start_session(env)
    user.run(sid, 1, "baseline", 1000.0, summary=_mem(30, over=False))
    user.run(sid, 2, "run-2", 1500.0, managed={"innodb_buffer_pool_size": "51G"}, summary=_mem(69))
    user.run(sid, 3, "run-3", 1100.0, managed={"innodb_io_capacity": "2000"}, summary=_mem(30, over=False))
    res = main.session_results(sess_row(sid))
    assert res["best"]["seq"] == 2 and res["best"]["safe"] is False and res["best_safe"]["seq"] == 3


# ============================================================================ run detail

def test_run_detail_vs_baseline_and_start(tuned):
    u, (r1, r2, r3, r4) = tuned["user"], tuned["runs"]
    d = u.ui.get(f"/api/runs/{r4}").json()
    vb = d["vs_baseline"]
    assert vb["baseline"]["seq"] == 2
    by = {s["variable"]: s for s in vb["settings"]}
    assert set(by) == {"innodb_io_capacity", "sync_binlog"}  # trx_commit=2 is the baseline's own
    assert by["innodb_io_capacity"]["set_in"]["seq"] == 3 and by["innodb_io_capacity"]["this_run"] is False
    assert by["sync_binlog"]["set_in"]["seq"] == 4 and by["sync_binlog"]["this_run"] is True
    assert by["sync_binlog"]["baseline"] is None and by["sync_binlog"]["value"] == "0"
    assert d["start"] == {"value": 1000.0, "basis": "baseline", "runs": [2]}
    assert [t["variable"] for t in d["tradeoffs_added"]] == ["sync_binlog"]
    assert [t["variable"] for t in d["start_tradeoffs"]] == ["innodb_flush_log_at_trx_commit"]
    assert d["compared_with"]["seq"] == 3 and [x["variable"] for x in d["diff"]] == ["sync_binlog"]
    assert d["diff"][0]["source"] == "rec:sync-binlog"
    assert u.ui.get(f"/api/runs/{r2}").json()["vs_baseline"] is None
    assert u.ui.get(f"/api/runs/{r3}").json()["tradeoffs_added"] == []
    # compare with any run of the account
    assert u.ui.get(f"/api/runs/{r4}", params={"vs": r1}).json()["compared_with"]["seq"] == 1


BENCH = {"name": "sysbench", "command": "sysbench oltp_read_write --mysql-user=sb --mysql-password=S3cretA --threads=16 run",
         "run": "mysql -usb -pS3cretB -e 'select 1'", "reset": "PGPASSWORD=S3cretC ./reset.sh", "warmup": 60, "duration": 300,
         "profile_text": "[bench]\npassword = S3cretD\n", "scripts": {"load.sh": "mysql -u root -pS3cretE sbtest < dump.sql"}}


def test_passwords_masked_on_run_and_session(user, client):
    env = user.hello()
    sid = user.start_session(env)
    rid = user.run(sid, 1, "baseline", 1000.0, inputs={"bench": BENCH})
    page = json.dumps(user.ui.get(f"/api/runs/{rid}").json())
    sess = user.ui.get(f"/api/sessions/{sid}").json()
    token = user.ui.post("/api/shares", json={"kind": "environment", "id": env}).json()["token"]
    shared = json.dumps(client.get(f"/api/runs/{rid}", headers={"X-Share-Token": token}).json())
    for text in (page, json.dumps(sess["benchmarks"]), shared):
        assert "S3cret" not in text
    b = sess["benchmarks"][0]
    assert b["command"] == "sysbench oltp_read_write --mysql-user=sb --mysql-password=*** --threads=16 run"
    assert b["run"] == "mysql -usb -p*** -e 'select 1'" and b["reset"] == "PGPASSWORD=*** ./reset.sh"
    assert b["scripts"]["load.sh"] == "mysql -u root -p*** sbtest < dump.sql" and b["runs"] == [1]


@pytest.mark.parametrize("text,masked", [
    ("mysql -h db -uroot -pHUNTER2 test", "mysql -h db -uroot -p*** test"),
    ("mariadb -uroot -pHUNTER2 test", "mariadb -uroot -p*** test"),          # the MariaDB client's own name
    ("mysql -uroot -p'my secret' test", "mysql -uroot -p*** test"),          # quoted
    ('mysqldump -uroot -p"x0" db', 'mysqldump -uroot -p*** db'),
    ("mysqladmin --password=x1 status", "mysqladmin --password=*** status"),
    ("mysql --password x2 -e 'q'", "mysql --password *** -e 'q'"),
    ("MYSQL_PWD=x3 mysql", "MYSQL_PWD=*** mysql"),
    ("db_password: x4", "db_password: ***"),
    ("tar -p -f x.tar", "tar -p -f x.tar"),        # -p of other tools and option flags stay
    ("mysql -p -e 'select 1'", "mysql -p -e 'select 1'"),
    (None, None), ("", ""),
])
def test_mask_secrets(text, masked):
    assert main.mask_secrets(text) == masked


def test_mask_bench_keeps_other_fields():
    b = main.mask_bench({"command": "x --password=y", "warmup": 60, "scripts": {"a": "b"}})
    assert b == {"command": "x --password=***", "run": None, "reset": None, "profile_text": None, "warmup": 60, "scripts": {"a": "b"}}
    assert main.mask_bench("not a dict") == "not a dict"


# ============================================================================ cross-account experience for agents

def _outcome(u, uid, idea, verdict, name=None, workload="oltp-write", cpus=4, study=None):
    env = u.hello(uid=uid, cpus=cpus)
    sid = u.start_session(env, **({"name": name} if name else {}), **({"study": study} if study else {}))
    u.run(sid, 1, "baseline", 1000.0)
    u.run(sid, 2, "run-2", 1100.0, managed={"x": "1"}, verdict=verdict, workload=workload,
          changes=[{"variable": "x", "old": "0", "new": "1", "source": f"rec:{idea}"}], evaluation={"goal_delta_pct": 10.0})
    return sid


def test_experience_counts_every_account_without_canaries(make_user):
    a, b = make_user(), make_user()
    _outcome(a, "a1", "io-capacity", "improved")
    _outcome(b, "b1", "io-capacity", "regressed")
    _outcome(a, "a2", "io-capacity", "improved", name="deploy canary 12")
    _outcome(a, "a3", "io-capacity", "improved", study="Canary")  # a canary by its study, not its name
    _outcome(b, "b2", "io-capacity", "improved", workload="olap")
    d = b.agent.get("/api/v1/agent/experience", params={"workload": "oltp-write", "cpus": 4, "conns": 16}).json()
    e = d["experience"]["io-capacity"]
    assert (e["n"], e["wins"], e["losses"]) == (2, 1, 1)
    assert d["experience_all"]["io-capacity"]["n"] == 3  # every workload, still without the canary
    assert d["cpu_class"] == "3-8" and d["concurrency_class"] == "medium"


def test_experience_tiers_by_server_series(make_user):
    a = make_user()
    env = a.hello(uid="m97", flavor="mysql", version="9.7.0")
    sid = a.start_session(env)
    a.run(sid, 1, "run-1", 1100.0, verdict="improved", changes=[{"variable": "x", "new": "1", "source": "rec:idea"}])
    _outcome(a, "p84", "idea", "improved")
    d = a.agent.get("/api/v1/agent/experience", params={"flavor": "percona", "version": "8.4.5", "workload": "oltp-write"}).json()
    assert d["series"] == "percona 8.4"
    assert d["experience"]["idea"]["n"] == 1 and d["experience_other_variant"]["idea"]["n"] == 1


def test_experience_serves_matching_insights(user):
    c = learner.db()
    for i, (wl, status_) in enumerate((("oltp-write", "observed"), ("olap", "repeated"), ("oltp-write", "archived"))):
        c.execute("INSERT INTO insights(key, created, updated, statement, settings, conditions, direction, sessions, status, confidence) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (f"k{i}", time.time(), time.time(), f"insight {i}", json.dumps([{"variable": f"v{i}", "value": "1"}]),
                                                    json.dumps({"workload": wl}), "helps", "[1]", status_, 0.6))
    d = user.agent.get("/api/v1/agent/experience", params={"workload": "oltp-write"}).json()
    assert [x["statement"] for x in d["insights"]] == ["insight 0"]


# ============================================================================ background: dead sessions

def _dead_case(u, uid, created_h, last_seen_h=0.0, activity=None, destroyed=False, run=None):
    env = u.hello(uid=uid)
    sid = u.start_session(env)
    if run == "measuring":
        u.run(sid, 1, "run-1", None, finish=False, started=ago(30 * 3600))
    elif run == "old":
        u.run(sid, 1, "run-1", 1000.0, started=ago(26 * 3600), duration=600)
    store.ex("UPDATE tsessions SET created=? WHERE id=?", (ago(created_h * 3600), sid))
    store.ex("UPDATE environments SET last_seen=?, activity=?, activity_at=?, destroyed_at=? WHERE id=?",
             (ago(last_seen_h * 3600), json.dumps(activity) if activity else None, ago(60) if activity else None,
              ago(60) if destroyed else None, env))
    return sid


def test_auto_close_dead(user):
    cases = {
        "destroyed": (_dead_case(user, "d1", 1, destroyed=True), "abandoned", "host was shut down"),
        "silent": (_dead_case(user, "d2", 8, last_seen_h=7), "abandoned", "silent for 7 h"),
        "idle": (_dead_case(user, "d3", 30, activity={"phase": "idle", "session_id": None}, run="old"), "abandoned", "nothing was measured"),
        "no activity": (_dead_case(user, "d4", 25), "abandoned", "nothing was measured"),
        "overdue phase": (_dead_case(user, "d5", 50, activity={"phase": "warm-up", "until": ago(26 * 3600)}), "abandoned", "nothing was measured"),
        "seasoning": (_dead_case(user, "d6", 50, activity={"phase": "seasoning", "until": time.time() + 3600}, run="old"), "active", None),
        "measuring": (_dead_case(user, "d7", 50, run="measuring"), "active", None),
        "recent": (_dead_case(user, "d8", 2, activity={"phase": "idle"}), "active", None),
        "silent briefly": (_dead_case(user, "d9", 30, last_seen_h=5, run="measuring"), "active", None),
    }
    main.auto_close_dead()
    for name, (sid, want, why) in cases.items():
        assert sess_row(sid)["status"] == want, name
        ev = store.q1("SELECT message FROM events WHERE session_id=? AND message LIKE 'session closed automatically%'", (sid,))
        assert (ev is not None) == (want == "abandoned"), name
        if why:
            assert why in ev["message"], (name, ev["message"])


# ============================================================================ background: stall watchdog and /status

def test_watchdog_alerts(user):
    def setup(uid, last_seen_s=0, created_s=60, activity=None, state=None):
        env = user.hello(uid=uid, name=uid)
        sid = user.start_session(env)
        store.ex("UPDATE tsessions SET created=? WHERE id=?", (ago(created_s), sid))
        store.ex("UPDATE environments SET last_seen=?, activity=?, activity_at=?, agent_state=?, agent_state_at=? WHERE id=?",
                 (ago(last_seen_s), json.dumps(activity) if activity else None, ago(30) if activity else None, state,
                  time.time() if state else None, env))
        return env, sid
    _, silent = setup("silent", last_seen_s=900)
    _, stalled = setup("stalled", created_s=2 * 3600)
    _, overdue = setup("overdue", activity={"phase": "warm-up", "until": ago(1800)})
    _, stopped = setup("stopped", last_seen_s=900, created_s=2 * 3600, state="stopped")
    _, ok = setup("ok", activity={"phase": "measuring", "until": time.time() + 600})
    env, meas = setup("meas", created_s=10 * 3600)
    for i in range(3):  # 10-min runs, then one measuring for 70 min (limit 3 x typical + 30 min = 60 min)
        user.run(meas, i + 1, f"run-{i + 1}", 1000.0, started=ago(9 * 3600 - i * 1200), duration=600)
    user.run(meas, 4, "run-4", None, finish=False, started=ago(70 * 60))
    alerts, checked = main.watchdog_scan()
    kinds = {(a["kind"], a.get("session_id")) for a in alerts}
    assert ("agent-silent", silent) in kinds and next(a for a in alerts if a.get("session_id") == silent)["severity"] == "high"
    assert ("no-progress", stalled) in kinds
    assert ("phase-overdue", overdue) in kinds
    assert ("measure-overdue", meas) in kinds and ("no-progress", meas) not in kinds
    assert ("no-progress", stopped) in kinds and not any(a["kind"] == "agent-silent" and a.get("session_id") == stopped for a in alerts)
    assert "stopped on purpose" in next(a for a in alerts if a.get("session_id") == stopped)["message"]
    assert not any(a.get("session_id") == ok for a in alerts)
    assert checked == {"environments": 6, "client_hosts": 0, "active_sessions": 6}


def test_watchdog_scan_records_alerts_for_status(admin):
    env = admin.hello()
    sid = admin.start_session(env)
    store.ex("UPDATE environments SET last_seen=? WHERE id=?", (ago(900), env))
    c = status._db()
    status._scan(c)
    c.close()
    rep = admin.ui.get("/status.json").json()
    assert rep["watchdog"]["error"] is None and rep["watchdog"]["checked"]["active_sessions"] == 1
    assert [(a["kind"], a["session_id"], a["resolved_at"]) for a in rep["alerts"]] == [("agent-silent", sid, None)]
    assert "watchdog: 1 active alert(s)" in rep["warnings"]
    # the agent reports again: resolved on the next scan
    store.ex("UPDATE environments SET last_seen=? WHERE id=?", (time.time(), env))
    c = status._db()
    status._scan(c)
    c.close()
    assert status.report()["alerts"][0]["resolved_at"] is not None


def test_status_pages(admin, user, client):
    admin.ui.get("/healthz")  # request timings are recorded and flushed by the sampler
    rep = admin.ui.get("/status.json").json()
    for k in ("ok", "warnings", "uptime_s", "last", "requests_last_hour", "slowest_routes", "watchdog", "ai", "alerts", "series"):
        assert k in rep, k
    assert rep["ai"]["settings"]["mode"] == "planner" and rep["ai"]["totals"]["calls"] == 0
    r = admin.ui.get("/status")
    assert r.status_code == 200 and "Tuner server status" in r.text and "Stall watchdog" in r.text and "AI model use" in r.text
    assert user.ui.get("/status").status_code == 403
    assert user.ui.get("/status.json").status_code == 403
    assert client.get("/status", follow_redirects=False).status_code == 303
    assert client.get("/status.json").status_code == 401


def test_status_page_survives_watchdog_error(admin):
    status.set_watchdog(lambda: 1 / 0)
    try:
        c = status._db()
        status._scan(c)
        c.close()
    finally:
        status.set_watchdog(main.watchdog_with_cleanup)
    rep = status.report()
    assert rep["watchdog"]["error"].startswith("ZeroDivisionError") and any("watchdog error" in w for w in rep["warnings"])
    assert admin.ui.get("/status").status_code == 200


# ============================================================================ learner

def _learn_session(u, uid, name=None, study=None, close=True, runs=4, agent_version=BUILD):
    env = u.hello(uid=uid, cpus=4)
    kw = {"agent_version": agent_version}
    if name:
        kw["name"] = name
    if study:
        kw["study"] = study
    sid = u.start_session(env, workload="my secret workload text", **kw)
    u.run(sid, 1, "baseline", 1000.0, inputs={"time_breakdown": [{"name": "redo log flush", "share_pct": 41}]})
    for i in range(2, runs + 1):
        u.run(sid, i, f"run-{i}", 1000.0 + 50 * i, managed={"innodb_io_capacity": str(1000 * i)}, verdict="improved",
              changes=[{"variable": "innodb_io_capacity", "old": "200", "new": str(1000 * i), "source": "rec:io-capacity"}],
              evaluation={"goal_delta_pct": 5.0, "hypothesis": {"kind": "flush", "target": ["redo"], "outcome": "supported"}})
    if close:
        u.close(sid)
    return sid


def test_digest_admin_and_non_admin(admin, user):
    mine = _learn_session(admin, "adm", name="operator session")
    theirs = _learn_session(user, "usr", name="customer acme prod")
    d = learner.digest(mine)
    assert d["name"] == "operator session" and d["workload_text"] == "my secret workload text"
    assert d["conditions"]["workload"] == "oltp-write" and d["conditions"]["cpu_class"] == "3-8"
    assert d["conditions"]["concurrency_class"] == "medium" and d["conditions"]["series"] == "percona 8.4"
    assert d["baseline_time_breakdown"] == ["redo log flush 41%"] and len(d["runs"]) == 4
    assert d["runs"][1]["changes"] == ["innodb_io_capacity=2000"] and d["runs"][1]["hypothesis"]["outcome"] == "supported"
    d = learner.digest(theirs)  # a non-admin session (sqlite Row .get bug: every such digest failed)
    assert d["name"] is None and d["workload_text"] is None and len(d["runs"]) == 4
    assert learner.digest(999999) is None


def test_store_insight_conditions_from_digest(admin):
    sid = _learn_session(admin, "a")
    dg = learner.digest(sid)
    ins = {"statement": "innodb_io_capacity=2000 raised TPS 5% (run 2)", "caveats": "single host", "direction": "helps", "same_as": 0,
           "settings": [{"variable": "innodb_io_capacity", "value": "2000"}], "runs": [2],
           "conditions": {"workload": "sysbench oltp_read_write", "cpu_class": "3-8", "concurrency_class": "any", "components": ["redo"]}}
    c = learner.db()
    assert learner._store_insight(ins, dg, c) is True
    r = c.execute("SELECT * FROM insights").fetchone()
    cond = json.loads(r["conditions"])
    assert cond["workload"] == "oltp-write" and cond["concurrency_class"] == "medium" and cond["components"] == ["redo"]
    assert cond["model_said"] == {"workload": "sysbench oltp_read_write", "concurrency_class": "any"}
    assert r["support"] == 1 and r["status"] == "observed" and r["caveats"] == "single host"
    assert json.loads(r["sessions"]) == [sid]
    # the same insight again: more evidence for the same row, not a new one
    assert learner._store_insight(ins, dg, c) is False
    assert c.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 1


def test_validate_counts_only_the_insights_own_change(user):
    env = user.hello(uid="v", cpus=4)
    s1 = user.start_session(env)
    ch = lambda *pairs, src="rec:io": [{"variable": v, "old": "x", "new": n, "source": src} for v, n in pairs]  # noqa: E731
    user.run(s1, 1, "run-1", 1, verdict="improved", changes=ch(("innodb_io_capacity", "2000")))
    user.run(s1, 2, "run-2", 1, verdict="improved", changes=ch(("innodb_io_capacity", "2000"), ("innodb_io_capacity_max", "4000")))  # companion
    user.run(s1, 3, "run-3", 1, verdict="improved", changes=ch(("innodb_io_capacity", "2000"), ("sync_binlog", "0")))  # a bundle: not evidence
    user.run(s1, 4, "run-4", 1, verdict="regressed", changes=ch(("innodb_io_capacity", "2000"), src="rec:revert-io"))  # a revert
    user.run(s1, 5, "run-5", 1, verdict="improved", changes=ch(("innodb_io_capacity", "4000")))  # another value
    user.run(s1, 6, "run-6", 1, verdict="improved", changes=ch(("innodb_io_capacity", "2000")), evaluation={"not_steady": True})  # flawed
    user.run(s1, 7, "run-7", 1, verdict="neutral", changes=ch(("innodb_io_capacity", "2000")), workload="olap")  # other conditions
    s2 = user.start_session(env, agent_version=None)  # no recorded build: pre-fix evidence, half weight
    user.run(s2, 1, "run-1", 1, verdict="regressed", changes=ch(("innodb_io_capacity", "2000")))
    per = learner.validate([IO], "helps", {"workload": "oltp-write", "cpu_class": "3-8"})
    assert per == {s1: [2, 0, 0, env, 1.0], s2: [0, 1, 0, env, learner.LEGACY_WEIGHT]}
    per = learner.validate([IO], "hurts", {})
    assert per[s1][:3] == [0, 2, 1] and per[s2][:3] == [1, 0, 0]


def test_run_pass_processes_and_records_errors(admin, user, monkeypatch):
    ok = _learn_session(admin, "ok")
    bad = _learn_session(user, "bad")
    short = _learn_session(user, "short", runs=2)  # fewer than 3 runs: nothing to learn, marked processed
    canary = _learn_session(admin, "can", name="deploy canary 3")
    canary2 = _learn_session(admin, "can2", study="Canary")
    _learn_session(admin, "open", close=False)  # active sessions wait until they close
    asked = []

    def ask(dg, known, admin_id):
        asked.append(dg["session"])
        assert admin_id == admin.id
        if dg["session"] == bad:
            raise RuntimeError("model rejected the digest")
        return {"insights": [{"statement": "io_capacity 2000 helped", "caveats": "", "direction": "helps", "same_as": 0, "runs": [2],
                              "settings": [IO], "conditions": {"workload": "oltp", "cpu_class": "3-8", "concurrency_class": "any", "components": []}}],
                "proposals": [{"title": "measure flush waits", "kind": "measurement", "problem": "p", "proposal": "q", "expected_effect": "e",
                               "runs": [2]}]}, 0.02
    monkeypatch.setattr(learner, "_ask", ask)
    r = learner.run_pass("test")
    assert sorted(asked) == sorted([ok, bad])
    assert (r["sessions"], r["calls"], r["insights_new"], r["proposals_new"]) == (1, 1, 1, 1) and r["cost"] == 0.02
    assert f"session {bad}: model rejected the digest" in r["error"]
    c = learner.db()
    processed = {x[0] for x in c.execute("SELECT session_id FROM processed")}
    assert ok in processed and short in processed and bad not in processed
    assert canary not in processed and canary2 not in processed
    assert c.execute("SELECT attempts FROM failures WHERE session_id=?", (bad,)).fetchone()[0] == 1
    # the failing session is retried each pass and given up after MAX_ATTEMPTS
    for i in range(learner.MAX_ATTEMPTS - 1):
        asked.clear()
        learner.run_pass("test")
        assert asked == [bad]
    assert bad in {x[0] for x in c.execute("SELECT session_id FROM processed")}
    asked.clear()
    r = learner.run_pass("test")
    assert asked == [] and r["error"] is None
    ov = admin.ui.get("/api/admin/learner").json()
    assert ov["processed"] == 3 and len(ov["insights"]) == 1 and ov["proposals"][0]["title"] == "measure flush waits"
    assert ov["insights"][0]["conditions"]["workload"] == "oltp-write"


def test_run_pass_without_ai_key_records_error(admin):
    sid = _learn_session(admin, "x")
    r = learner.run_pass("test")  # the real _ask: no service key configured, so no network call either
    assert "no AI key" in r["error"] and f"session {sid}" in r["error"]


def test_matching_weight_by_series():
    c = learner.db()

    def add(key, series, settings, direction="helps", status_="observed", conf=0.5):
        cond = {"workload": "oltp-write"}
        if series:
            cond["series"] = series
        c.execute("INSERT INTO insights(key, created, updated, statement, settings, conditions, direction, sessions, status, confidence) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (key, time.time(), time.time(), key, json.dumps(settings), json.dumps(cond), direction, "[]", status_, conf))
    add("same", "percona 8.4", [IO])
    add("family", "percona 8.0", [IO], conf=0.9)  # the same statement from a farther server: dropped
    add("family-only", "percona 8.0", [{"variable": "a", "value": "1"}])
    add("other", "mysql 9.7", [{"variable": "b", "value": "1"}])
    add("unrecorded", None, [{"variable": "c", "value": "1"}])
    add("archived", "percona 8.4", [{"variable": "d", "value": "1"}], status_="archived")
    got = {x["statement"]: (x["weight"], x["scope"]) for x in learner.matching("oltp-write", series="percona 8.4")}
    assert got == {"same": (1.0, None), "family-only": (0.6, "from percona 8.0"), "other": (0.3, "from mysql 9.7"),
                   "unrecorded": (0.6, "release series not recorded")}
    assert learner.matching("olap", series="percona 8.4") == []
    assert {x["weight"] for x in learner.matching("oltp-write")} == {1.0}


# ============================================================================ AI settings and planner

def test_llm_settings_default_mode(make_user):
    u = make_user()
    assert u.ui.get("/api/settings/llm").json()["mode"] == "off"  # no admin account: no service settings
    a = make_user(admin=True)
    s = u.ui.get("/api/settings/llm").json()
    assert s["mode"] == "planner" and s["configured"] is False and s["admin"] is False and "planner" in s["modes"]
    assert a.ui.put("/api/settings/llm", json={"mode": "always", "model": "x/model-1"}).json()["mode"] == "always"
    assert u.ui.get("/api/settings/llm").json()["model"] == "x/model-1"


def test_llm_key_is_verified_and_encrypted(admin, monkeypatch):
    assert admin.ui.put("/api/settings/llm", json={"key": "sk-or-bad"}).status_code == 400  # verify fails: no network
    monkeypatch.setattr(llm, "verify_key", lambda k: (True, {}))
    assert admin.ui.put("/api/settings/llm", json={"key": "sk-or-good"}).json()["configured"] is True
    enc = store.q1("SELECT llm_key_enc FROM users WHERE id=?", (admin.id,))["llm_key_enc"]
    assert "sk-or-good" not in enc and llm.service_key() == "sk-or-good"
    assert admin.ui.put("/api/settings/llm", json={"remove_key": True}).json()["configured"] is False


def test_planner_endpoint(admin, user, monkeypatch):
    env = user.hello()
    sid = user.start_session(env)
    admin.ui.put("/api/settings/llm", json={"mode": "off"})
    r = user.agent.post("/api/v1/agent/llm/plan", json={"session_id": sid, "packet": {}}).json()
    assert r["enabled"] is False and "planner mode is off" in r["reason"]
    psid = user.start_session(env, planner=True)  # a planner session asks even with the mode off
    r = user.agent.post("/api/v1/agent/llm/plan", json={"session_id": psid, "packet": {}}).json()
    assert r == {"enabled": False, "reason": "LLM assistance is not configured"}
    store.ex("UPDATE users SET llm_key_enc=? WHERE id=?", (llm.encrypt("sk-or-test"), admin.id))
    calls = []

    def fake_call(user_id, env_id, session_id, model, system, schema, name, payload, purpose, effort=None):
        calls.append((user_id, session_id, model, purpose))
        return {"action": "experiment", "choice": {"candidate_id": "io-capacity", "changes": [], "sweep_values": []}, "alternatives": []}, {"cost": 0.1}
    monkeypatch.setattr(llm, "_call", fake_call)
    r = user.agent.post("/api/v1/agent/llm/plan", json={"session_id": psid, "packet": {"candidates": [{"id": "io-capacity"}]}}).json()
    assert r["enabled"] is True and r["plan"]["choice"]["candidate_id"] == "io-capacity" and r["model"] == llm.PLAN_MODEL
    assert calls == [(user.id, psid, llm.PLAN_MODEL, "plan")]
    assert store.q1("SELECT message FROM events WHERE session_id=? AND type='llm'", (psid,))["message"].endswith("io-capacity")


# ============================================================================ feedback, stars, notes

def test_feedback_stars_notes(tuned, make_user, admin):
    u, (_, _, r3, r4) = tuned["user"], tuned["runs"]
    assert u.ui.post(f"/api/runs/{r3}/feedback", json={"verdict": "great"}).status_code == 400
    assert u.ui.post(f"/api/runs/{r3}/feedback", json={"verdict": "questionable"}).status_code == 400  # say why
    assert u.ui.post(f"/api/runs/{r3}/feedback", json={"verdict": "reasonable", "rec_id": 99999}).status_code == 400
    fb = u.ui.post(f"/api/runs/{r3}/feedback", json={"verdict": "questionable", "comment": "io_capacity above the disk's IOPS"}).json()
    assert fb["feedback"][0]["item_id"] == "io-capacity"  # the recommendation the run tried
    assert u.ui.put(f"/api/runs/{r4}/star").json() == {"starred": True}
    assert u.ui.put(f"/api/runs/{r4}/star").status_code == 200  # idempotent
    notes = u.ui.get("/api/notes").json()["runs"]
    assert {(n["seq"], n["starred"], len(n["feedback"])) for n in notes} == {(3, False, 1), (4, True, 0)}
    s = u.ui.get(f"/api/sessions/{tuned['sid']}").json()
    assert [r["seq"] for r in s["runs"] if r["starred"]] == [4] and s["feedback"][0]["verdict"] == "questionable"
    assert u.ui.get(f"/api/runs/{r4}").json()["starred"] is True
    # the agent sees the feedback (without who wrote it)
    af = u.agent.get(f"/api/v1/agent/sessions/{tuned['sid']}/feedback", params={"items": "io-capacity"}).json()
    assert af["session"][0]["verdict"] == "questionable" and "email" not in af["session"][0]
    # deleting: the author or an administrator only
    other = make_user()
    assert other.ui.delete(f"/api/feedback/{fb['id']}").status_code == 404
    assert u.ui.delete(f"/api/feedback/{fb['id']}").status_code == 200
    assert u.ui.delete(f"/api/runs/{r4}/star").json() == {"starred": False}
    assert u.ui.get("/api/notes").json()["runs"] == []


def test_notes_scope_all_for_admin(admin, make_user):
    env = admin.hello()
    sid = admin.start_session(env)
    rid = admin.run(sid, 1, "baseline", 1000.0)
    admin.ui.post(f"/api/runs/{rid}/feedback", json={"verdict": "agree"})
    assert admin.ui.get("/api/notes", params={"scope": "all"}).json()["scope"] == "all"
    u = make_user()
    assert u.ui.get("/api/notes", params={"scope": "all"}).json()["scope"] == "mine"


def test_session_rename_and_delete(tuned):
    u, sid = tuned["user"], tuned["sid"]
    assert u.ui.patch(f"/api/sessions/{sid}", json={"name": "renamed", "notes": "n"}).status_code == 200
    assert sess_row(sid)["name"] == "renamed"
    assert u.ui.delete(f"/api/sessions/{sid}").status_code == 200
    assert store.q1("SELECT COUNT(*) n FROM runs WHERE session_id=?", (sid,))["n"] == 0
    assert u.ui.get(f"/api/sessions/{sid}").status_code == 404


def test_param_policy(user, admin):
    assert admin.ui.put("/api/admin/parameters/sync_binlog/policy", json={"mode": "block", "note": "keep durable"}).status_code == 200
    assert user.agent.get("/api/v1/agent/param-policy").json() == {"blocked": ["sync_binlog"], "notes": {"sync_binlog": "keep durable"}}
    assert user.ui.put("/api/parameters/sync_binlog/policy", json={"mode": "tune"}).json()["mode"] == "tune"
    assert user.agent.get("/api/v1/agent/param-policy").json()["blocked"] == []
    assert user.ui.put("/api/parameters/sync_binlog/policy", json={"mode": "bogus"}).status_code == 400
    assert user.ui.put("/api/admin/parameters/x/policy", json={"mode": "block"}).status_code == 403


def test_healthz_and_index(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 200


def test_session_ai_off_is_stored_and_shown(admin):
    """session start --ai off (the head-to-head's rules arm): the session records that it ran without AI."""
    env_id = admin.hello()
    sid = admin.start_session(env_id, ai="off")
    s = admin.ui.get(f"/api/sessions/{sid}").json()["session"]
    assert s["ai"] == "off"
    sid2 = admin.start_session(env_id)
    assert admin.ui.get(f"/api/sessions/{sid2}").json()["session"]["ai"] is None


def test_home_and_quickstart_carry_agent_instructions(client):
    # agents fetch the page without JavaScript: the instructions must be in the HTML itself, with the real server URL
    for path in ("/", "/quickstart"):
        r = client.get(path)
        assert r.status_code == 200
        assert "/agent-guide.md" in r.text and "__SERVER__" not in r.text
        assert "AI agents reading this page" in r.text
    g = client.get("/agent-guide.md").text
    assert "Fastest path" in g and "autotune --bench" in g and "__SERVER__" not in g
    t = client.get("/llms.txt")
    assert t.status_code == 200 and "agent-guide.md" in t.text and "quickstart" in t.text


def test_apply_events_name_the_settings():
    """User: 'stability-triage: applied 1 change(s)' said nothing about what was tried. Events from older agents are
    shown with the settings and values from their stored result."""
    from app import main
    ev = {"type": "apply", "message": "stability-triage: applied 1 change(s)",
          "data": {"result": {"applied": [{"variable": "innodb_purge_batch_size", "old": "300", "new": "1000",
                                           "reason": "purge triage: purge batch size 1000"}]}}}
    m = main.event_view(ev)["message"]
    assert m == "stability-triage: innodb_purge_batch_size 300 -> 1000 - purge triage: purge batch size 1000", m
    assert main.event_view({"type": "apply", "message": "x: applied 0 change(s)", "data": {}})["message"] == "x: applied 0 change(s)"


def test_steal_profile_from_its_own_table(user):
    """User: environment pages loaded slowly - the steal line read a week of full metric samples (~120 MB) on every
    cold load. Steal values are kept apart as they arrive; samples stored before that are backfilled once."""
    import time
    from app import main, store
    env = user.hello(uid="steal-agent")
    t = time.time()
    samples = [{"ts": t - 600 + 10 * i, "os": {"cpu_steal": 0.5 if i % 10 else 3.0}, "db": {}} for i in range(40)]
    assert user.agent.post("/api/v1/agent/metrics", json={"environment_id": env, "samples": samples}).json()["stored"] == 40
    assert store.q1("SELECT COUNT(*) n FROM steal_samples WHERE env_id=?", (env,))["n"] == 40
    main._STEAL_CACHE.clear()
    sp = user.ui.get(f"/api/environments/{env}").json()["steal"]
    assert sp["samples"] == 40 and sp["max"] == 3.0 and sp["pct_over_1"] == 10.0, sp
    # an environment whose samples predate the table: backfilled from the metrics once
    store.ex("DELETE FROM steal_samples WHERE env_id=?", (env,))
    store.ex("DELETE FROM steal_backfilled WHERE env_id=?", (env,))
    main._STEAL_CACHE.clear()
    assert user.ui.get(f"/api/environments/{env}").json()["steal"]["samples"] == 40
    assert store.q1("SELECT COUNT(*) n FROM steal_backfilled WHERE env_id=?", (env,))["n"] == 1


def test_run_plan_is_shown_with_the_session(user):
    """User: with an automatic run length, where do I see the warm-up and window chosen? The agent records the plan."""
    env = user.hello(uid="plan-agent")
    sid = user.start_session(env)
    plan = {"warmup_s": 2400, "duration_s": 600, "auto": ["warmup", "duration"], "predicted_within_noise_pct": 5.4, "target_pct": 3.0,
            "unstable": True, "settling": {"checkpoint_age_pct": 2003}}
    assert user.agent.post(f"/api/v1/agent/sessions/{sid}/update", json={"run_plan": plan}).status_code == 200
    assert user.ui.get(f"/api/sessions/{sid}").json()["session"]["run_plan"] == plan


def test_waits_share_of_database_time(user):
    """User: 10 threads waiting on disk among ~1024 running is ~1% of database time - the waits view says so, and row
    access (io/table/sql/handler: CPU plus every wait inside it) is not counted as a wait."""
    import time
    env = user.hello(uid="waits-agent")
    t = time.time()
    samples = [{"ts": t - 100 + 10 * i, "db": {"threads_running": 1000},
                "waits": {"wait/io/file/innodb/innodb_data_file": 10.0, "wait/io/table/sql/handler": 900.0, "wait/lock/metadata/sql/mdl": 0.1}}
               for i in range(5)]
    assert user.agent.post("/api/v1/agent/metrics", json={"environment_id": env, "samples": samples}).status_code == 200
    w = user.ui.get(f"/api/environments/{env}/waits", params={"start": t - 200, "end": t}).json()
    assert w["active_threads"] == 1000 and abs(w["wait_threads"] - 10.1) < 0.01, w
    ev = {e["event"]: e for e in w["events"]}
    assert ev["wait/io/table/sql/handler"]["pct"] is None and ev["wait/io/file/innodb/innodb_data_file"]["pct"] > 98


def test_repeat_run_shows_the_profile_of_its_configuration(tuned, user):
    """User (342 run 4833): runs of an already profiled configuration said 'CPU (not profiled)'. The run page shows the
    profile of the latest earlier run of the same configuration, and says which run it is from."""
    from app import store
    rid = tuned["runs"][-1]
    r = store.q1("SELECT session_id, seq, managed FROM runs WHERE id=?", (rid,))
    prev = store.q1("SELECT id, seq FROM runs WHERE session_id=? AND seq<? ORDER BY seq DESC LIMIT 1", (r["session_id"], r["seq"]))
    if not prev:
        return
    store.ex("UPDATE runs SET managed=?, status='done', inputs=? WHERE id=?",
             (r["managed"], '{"cpu_profile": {"categories": {"B-tree / row access": 40}}}', prev["id"]))
    store.ex("UPDATE runs SET inputs='{}' WHERE id=?", (rid,))
    cp = user.ui.get(f"/api/runs/{rid}").json()["inputs"].get("cpu_profile")
    assert cp and cp["borrowed_from"]["seq"] == prev["seq"], cp


def test_learner_cost_cap_is_optional_and_says_why(user):
    """TUNER_LEARNER_MAX_COST_PER_DAY: optional dollars of learner AI cost in any 24 h (unset = unlimited). A capped pass
    records why (passes #24-#27 said only '0 sessions' while 30 finished sessions waited)."""
    import time
    from app import learner, store
    c = learner.db()
    c.execute("DELETE FROM learner_runs")
    c.execute("INSERT INTO learner_runs(started, ended, trigger, calls, cost) VALUES (?,?,?,?,?)", (time.time() - 3600, time.time() - 3500, "admin-backlog", 97, 14.0))
    env = user.hello(uid="learner-agent")
    sid = user.start_session(env)
    store.ex("UPDATE tsessions SET status='closed' WHERE id=?", (sid,))
    saved = learner.MAX_COST_PER_DAY
    try:
        learner.MAX_COST_PER_DAY = None  # unlimited: no cap message
        learner.run_pass("timer")
        row = dict(c.execute("SELECT * FROM learner_runs ORDER BY id DESC LIMIT 1").fetchone())
        assert "cost cap" not in (row.get("error") or ""), row
        learner.MAX_COST_PER_DAY = 10.0
        store.ex("UPDATE tsessions SET status='closed' WHERE id=?", (sid,))
        c.execute("DELETE FROM processed WHERE session_id=?", (sid,))
        learner.run_pass("timer")
        row = dict(c.execute("SELECT * FROM learner_runs ORDER BY id DESC LIMIT 1").fetchone())
        assert (row.get("error") or "").startswith("daily cost cap reached: $14.00 of $10.00"), row
    finally:
        learner.MAX_COST_PER_DAY = saved


def test_learner_write_class():
    """Binary log and sync_binlog insights are counted only against runs of the same write intensity (user: they matter
    with significant writes and a commit backlog; read-only and write-heavy sessions made 'binlog off' look contested)."""
    from app import learner
    assert learner._write_class({"redo_mbs": 0.0}, {}) == "read-only"
    tb = [{"name": "Waiting: binary log writes / fsync (commit)", "share_pct": 12.0, "tags": ["commit"]},
          {"name": "CPU: redo / commit path", "share_pct": 30.0, "tags": ["cpu", "commit"]}]
    assert learner._write_class({"redo_mbs": 40}, {"time_breakdown": tb}) == "write-heavy"
    assert learner._write_class({"redo_mbs": 40}, {"time_breakdown": tb[1:]}) == "writes"  # CPU on the commit path is not a backlog


def test_run_shows_the_decision_that_chose_it(tuned, user):
    """User (2026-10-25, session 353 run 5 spin-loops): why this experiment and not another one. The run page gets the
    recommendation its change came from, with every candidate's score and score terms."""
    from app import store
    rid = tuned["runs"][-1]
    r = store.q1("SELECT session_id, seq, started FROM runs WHERE id=?", (rid,))
    parent = store.q1("SELECT id FROM runs WHERE session_id=? AND seq<? ORDER BY seq DESC LIMIT 1", (r["session_id"], r["seq"]))
    items = [{"id": "spin-loops", "rank": 1, "title": "Shorter spin loops", "kind": "config", "safety": "safe", "score": 11.9, "confidence": 1,
              "score_parts": [["expected impact 2 of 3 (x2)", 4], ["confidence 1 of 3", 1], ["acts on the measured bottleneck", 6.9]],
              "changes": [{"variable": "innodb_sync_spin_loops", "proposed": "10"}], "hypothesis": {"text": "CPU saturated"}},
             {"id": "binlog-off", "rank": 2, "title": "Binary log off", "kind": "config", "safety": "trade-off", "score": 15.4, "confidence": 2}]
    store.ex("INSERT INTO recommendations(session_id, run_id, created, goal, policy, items, next, decision) VALUES (?,?,?,?,?,?,?,?)",
             (r["session_id"], parent["id"] if parent else None, (r["started"] or 0) - 10, "tps", "benchmark", json.dumps(items), "spin-loops",
              '{"by": "rules"}'))
    store.ex("UPDATE runs SET changes=? WHERE id=?", (json.dumps([{"variable": "innodb_sync_spin_loops", "old": "30", "new": "10", "source": "rec:spin-loops"}]), rid))
    cb = user.ui.get(f"/api/runs/{rid}").json()["chosen_by"]
    assert cb["id"] == "spin-loops" and cb["policy"] == "benchmark", cb
    assert [i["id"] for i in cb["items"]] == ["spin-loops", "binlog-off"], cb
    assert cb["items"][0]["score_parts"][0][1] == 4 and cb["items"][0]["why"] == "CPU saturated", cb["items"][0]


def test_agent_adds_existing_sessions_to_a_study(user):
    """User (2026-10-25): an agent running a version study could not attach finished sessions to a study (only the UI could)."""
    env = user.hello()
    a, b = user.start_session(env), None
    user.close(a)
    b = user.start_session(env)
    r = user.agent.post("/api/v1/agent/studies/sessions", json={"study": "PS versions", "sessions": [a, b]})
    assert r.status_code == 200 and r.json()["study"]["name"] == "PS versions", r.text
    st = user.agent.get("/api/v1/agent/studies").json()["studies"]
    assert {s["id"] for s in next(x for x in st if x["name"] == "PS versions")["sessions"]} == {a, b}
    r = user.agent.post("/api/v1/agent/studies/sessions", json={"study": None, "sessions": [a]})
    assert r.status_code == 200 and r.json()["study"] is None
    assert user.agent.post("/api/v1/agent/studies/sessions", json={"study": "x", "sessions": [999999]}).status_code == 404


def test_similarity_core_is_shared_and_other_accounts_stay_anonymous(user):
    """simcore is one module in two places (agent and server); predictions name only the caller's own sessions."""
    import pathlib
    from app import similar, simcore
    root = pathlib.Path(__file__).resolve().parents[2]
    agent_copy = root / "agent" / "tuning_agent" / "simcore.py"
    if agent_copy.exists():
        assert agent_copy.read_text() == (root / "server" / "app" / "simcore.py").read_text(), "server/app/simcore.py differs from the agent's"
    env = user.hello()
    sid = user.start_session(env)
    vec = {n: 0.5 for n in simcore.NAMES}
    mk = lambda session, seq, z_, own: {"session": session, "seq": seq, "label": "spin-loops", "verdict": "improved" if z_ > 1 else "neutral",  # noqa: E731
                                        "rule": "spin-loops", "changes": {"innodb_sync_spin_loops": "10"}, "delta": z_ * 2, "z": z_,
                                        "vec": {n: 0.1 * (i % 7) + (0.3 if i == seq else 0) for i, n in enumerate(simcore.NAMES)}, "facets": {"writes": "writes"}}
    cases = [mk(9001, 1, 2.0, True), mk(9002, 2, 1.5, False), mk(9003, 3, 1.0, False)]
    st = simcore.standardiser(cases)
    for c in cases:
        c["_z"] = simcore.z(c["vec"], st)
    from app import store
    uid = store.q1("SELECT e.user_id FROM tsessions s JOIN environments e ON e.id=s.env_id WHERE s.id=?", (sid,))["user_id"]
    saved = dict(similar._cache)
    try:
        similar._cache.update(t=9e12, cases=cases, st=st, owner={9001: uid, 9002: -1, 9003: -1})
        r = user.agent.post("/api/v1/agent/similar", json={"session_id": sid, "vector": {n: 0.1 * (i % 7) for i, n in enumerate(simcore.NAMES)},
                                                           "candidates": [{"id": "spin-loops", "changes": [{"variable": "innodb_sync_spin_loops", "proposed": "10"}]}]})
        assert r.status_code == 200, r.text
        p = r.json()["predictions"]["spin-loops"]
        assert p["sessions"] == 3 and p["pred"] > 0, p
        own = [n for n in p["neighbours"] if n["own"]]
        assert own and own[0]["session"] == 9001 and all(n["session"] is None for n in p["neighbours"] if not n["own"]), p["neighbours"]
    finally:
        similar._cache.clear()
        similar._cache.update(saved)


def test_incidents_always_reach_the_session_page(user):
    """A mysqld crash or hang (agent serverwatch, session 381) is shown in the session's Server incidents card however
    many events came after it (the page loads the latest 200)."""
    env = user.hello(uid="incident-agent")
    sid = user.start_session(env)
    inc = {"kind": "hang", "phase": "stopping", "diagnosis": "mysqld hung while shutting down", "killed": True}
    assert user.agent.post("/api/v1/agent/events", json={"environment_id": env, "session_id": sid, "type": "incident",
                                                         "message": inc["diagnosis"], "data": inc}).status_code == 200
    for i in range(210):
        user.agent.post("/api/v1/agent/events", json={"environment_id": env, "session_id": sid, "type": "recommend", "message": f"r{i}"})
    evs = user.ui.get(f"/api/sessions/{sid}").json()["events"]
    hit = [e for e in evs if e["type"] == "incident"]
    assert len(hit) == 1 and hit[0]["data"]["kind"] == "hang", [e["type"] for e in evs][:5]
