"""Offline engine smoke tests: synthetic run summaries for each workload type through every rule path."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tuning_agent import engine, evaluate, llmassist  # noqa: E402

GiB = 1024 ** 3
VARS = {
    "innodb_buffer_pool_size": str(2 * GiB), "innodb_buffer_pool_instances": "2", "innodb_redo_log_capacity": str(GiB),
    "innodb_log_buffer_size": str(64 * 2 ** 20), "innodb_io_capacity": "2000", "innodb_io_capacity_max": "4000",
    "innodb_flush_neighbors": "0", "innodb_flush_method": "O_DIRECT", "innodb_log_writer_threads": "ON",
    "innodb_adaptive_hash_index": "OFF", "innodb_sync_spin_loops": "30", "thread_handling": "one-thread-per-connection",
    "max_connections": "151", "table_open_cache": "4000", "table_definition_cache": "2000", "thread_cache_size": "9",
    "tmp_table_size": str(16 * 2 ** 20), "max_heap_table_size": str(16 * 2 ** 20), "temptable_max_ram": str(GiB),
    "sort_buffer_size": "262144", "join_buffer_size": "262144", "innodb_read_ahead_threshold": "56",
    "innodb_stats_persistent_sample_pages": "20", "optimizer_switch": "index_merge=on,mrr_cost_based=on,batched_key_access=off,subquery_to_derived=off",
    "innodb_flush_log_at_trx_commit": "1", "sync_binlog": "1", "log_bin": "ON", "binlog_row_image": "FULL",
    "innodb_doublewrite": "ON", "performance_schema": "ON", "binlog_group_commit_sync_delay": "0",
    "binlog_group_commit_sync_no_delay_count": "0", "innodb_change_buffering": "none", "innodb_lru_scan_depth": "1024",
}
VARS.update({"os:vm.swappiness": "60", "os:thp": "always", "os:scheduler": "bfq", "os:mount_noatime": "OFF", "os:mount_barrier": "ON",
             "os:vm.dirty_bytes": "0", "os:vm.dirty_background_bytes": "0", "os:kernel.sched_autogroup_enabled": "1", "os:fs.aio-max-nr": "65536", "os:malloc": "glibc", "os:hugepages": "0", "large_pages": "OFF"})
HW = {"cpus": 2, "ram_bytes": 4 * GiB, "disk_rotational": False, "disk_free_bytes": 50 * GiB}

OLTP = ({"tps": 600, "qps": 1800, "statements_per_s": 1800, "avg_latency_ms": 5, "cpu_busy": 95, "mysqld_cpu_pct": 70,
         "bp_hit_pct": 98, "bp_disk_reads": 500, "data_fsyncs": 700, "fsyncs_per_trx": 1.2, "threads_running_avg": 10,
         "checkpoint_age_pct_max": 70, "redo_mbs": 5, "row_lock_waits": 60, "row_lock_wait_avg_ms": 7, "disk_util": 40,
         "disk_await_ms": 1, "mem_available_mb": 600, "psi_cpu_some": 80, "duration_s": 300},
        {"status_rates": {"Com_insert": 300, "Com_update": 500, "Com_select": 900, "Com_commit": 600,
                          "Innodb_dblwr_pages_written": 400, "Innodb_rows_read": 30000}, "data_bytes": 3 * GiB,
         "top_statements": [{"digest": "a1", "query": "UPDATE district SET x=x+1", "pct_time": 30, "rows_examined_avg": 1,
                             "rows_sent_avg": 0, "no_index_used": 0, "lock_ms_avg": 5, "avg_ms": 6, "per_s": 60}]})
SP_OLTP = ({**OLTP[0], "statements_per_s": 300}, {**OLTP[1], "status_rates": {"Com_call_procedure": 300, "Innodb_rows_read": 5000}})
OLAP = ({"tps": 0.1, "qps": 0.1, "statements_per_s": 0.07, "avg_latency_ms": 15000, "cpu_busy": 55, "bp_hit_pct": 99.5,
         "bp_disk_reads": 200, "threads_running_avg": 1, "duration_s": 335, "mem_available_mb": 900},
        {"status_rates": {"Com_select": 0.07, "Select_full_join": 0.05, "Select_scan": 0.5, "Sort_merge_passes": 0.2,
                          "Created_tmp_tables": 0.1, "Created_tmp_disk_tables": 0.05, "Handler_read_rnd_next": 200000,
                          "Handler_read_key": 5000, "Innodb_rows_read": 2000000, "Max_execution_time_exceeded": 0.006},
         "data_bytes": 2 * GiB,
         "top_statements": [{"digest": "q17", "query": "SELECT SUM(l_extendedprice) FROM lineitem, part WHERE p_partkey = l_partkey "
                             "AND l_quantity < (SELECT 0.2 * AVG(l_quantity) FROM lineitem WHERE l_partkey = p_partkey)",
                             "pct_time": 40, "rows_examined_avg": 6000000, "rows_sent_avg": 1, "no_index_used": 1,
                             "lock_ms_avg": 0, "avg_ms": 120000, "per_s": 0.008}]})


def run(summary, inputs, policy, goal="tps", history=None, os_tuning=True):
    ctx = {"goal": goal, "policy": policy, "os_tuning": os_tuning, "os_facts": {"os:filesystem": "ext4", "os:aio_in_use": "60000"}, "hw": HW, "flavor": "percona", "version": (8, 4, 11), "variables": dict(VARS),
           "summary": summary, "inputs": inputs, "history": history or [], "noise_pct": 2.0, "managed": {}}
    return engine.run_engine(ctx)


def ids(res):
    return {i["id"]: i for i in res["items"]}


def test_oltp_safe_has_no_applicable_tradeoffs():
    res = run(*OLTP, policy="safe")
    assert res["workload"]["type"] == "oltp-write"
    for i in res["items"]:
        if i["safety"] == "trade-off":
            assert i["kind"] == "advice", i["id"]
    assert res["next"] and ids(res)[res["next"]]["safety"] == "safe"


def test_oltp_benchmark_offers_tradeoffs_after_safe():
    res = run(*OLTP, policy="benchmark")
    it = ids(res)
    assert "trx-commit-2" in it and it["trx-commit-2"]["kind"] == "config"
    assert "doublewrite-off" in it and "binlog-off" in it
    assert it[res["next"]]["safety"] == "safe"
    assert res["phase"] == "safe"


def test_stored_procedure_oltp_classified():
    assert engine.classify_workload(*SP_OLTP)["type"] == "oltp-write"
    run(*SP_OLTP, policy="benchmark")


def test_olap_rules():
    res = run(*OLAP, policy="benchmark", goal="latency")
    it = ids(res)
    assert res["workload"]["type"] == "olap"
    for k in ("join-buffer", "sort-buffer-olap", "tmp-olap", "subquery-to-derived", "bka-mrr", "stats-histograms"):
        assert k in it, (k, list(it))
    assert it["subquery-to-derived"]["changes"][0]["proposed"] == "subquery_to_derived=on"


def test_os_rules():
    it = ids(run(*OLTP, policy="safe"))
    for k in ("os-swappiness", "os-thp-off", "os-scheduler", "os-noatime", "os-dirty-writeback", "os-jemalloc", "os-autogroup", "os-aio"):
        assert k in it, (k, list(it))
    assert it["os-nobarrier"]["kind"] == "advice" and it["os-nobarrier"]["safety"] == "trade-off"
    assert "os-hugepages" not in it  # only proposed on >= 16 GB hosts with >= 8 GB buffer pool
    assert it["os-jemalloc"]["restart"]
    assert ids(run(*OLTP, policy="benchmark"))["os-nobarrier"]["kind"] == "config"
    assert not [k for k in ids(run(*OLTP, policy="safe", os_tuning=False)) if k.startswith("os-")]


def test_no_revert_chain():
    # a(base) -> b neutral -> reverted to a (no new run): no further "revert" suggestion for a
    h = [{"seq": 1, "managed": {}, "goal_value": 100, "verdict": "reference", "evaluation": {}},
         {"seq": 2, "managed": {"x": "1"}, "goal_value": 101, "verdict": "neutral", "evaluation": {"parent_runs": [1], "goal_delta_pct": 1}},
         {"seq": 3, "managed": {"x": "1", "y": "1"}, "goal_value": 100.5, "verdict": "neutral", "evaluation": {"parent_runs": [2], "goal_delta_pct": -0.5}}]
    v = {"x": "1", "y": "0"}
    assert engine.revert_suggestions(h, v, "tps", 3.0, {"x": "1", "y": "1"})  # right after run 3: revert to run 2
    assert not engine.revert_suggestions(h, v, "tps", 3.0, {"x": "1"})       # already reverted to run 2's config: stop


def test_per_query_hint():
    a = [{"schema": "t", "digest": "q1", "avg_ms": 100, "query": "SELECT q1"}, {"schema": "t", "digest": "q2", "avg_ms": 50, "query": "SELECT q2"}]
    b = [{"schema": "t", "digest": "q1", "avg_ms": 40, "query": "SELECT q1", "pct_time": 30}, {"schema": "t", "digest": "q2", "avg_ms": 80, "query": "SELECT q2", "pct_time": 30}]
    qi = evaluate.query_impact(b, a)
    assert qi["mixed"] and qi["winners"] == 1 and qi["losers"] == 1
    h = [{"seq": 1, "managed": {}, "goal_value": 100, "verdict": "reference"},
         {"seq": 2, "managed": {"join_buffer_size": "8M"}, "goal_value": 101, "verdict": "neutral",
          "changes": [{"variable": "join_buffer_size", "old": "262144", "new": "8388608"}], "evaluation": {"query_impact": qi, "parent_runs": [1]}}]
    items = engine.per_query_hints(h, VARS)
    assert items and "SET_VAR(join_buffer_size=8388608)" in items[0]["rationale"]


def test_llm_trigger_and_sanitize():
    res = run(*OLTP, policy="safe")
    assert llmassist.trigger(res) == "routine" and llmassist.trigger(res, explicit=True) == "explicit"
    assert llmassist.trigger({"items": [], "diagnosis": []}) == "rules-exhausted"
    ctx = {"variables": dict(VARS, innodb_lru_scan_depth="1024"), "hw": HW, "history": [
        {"seq": 2, "changes": [{"variable": "innodb_sync_spin_loops", "new": "10"}]}], "os_tuning": False, "policy": "safe"}
    sugg = [{"title": "deny", "changes": [{"variable": "datadir", "value": "/x"}]},
            {"title": "missing", "changes": [{"variable": "innodb_made_up", "value": "1"}]},
            {"title": "tried", "changes": [{"variable": "innodb_sync_spin_loops", "value": "10"}]},
            {"title": "too big", "changes": [{"variable": "innodb_buffer_pool_size", "value": "8G"}]},
            {"title": "os off", "changes": [{"variable": "os:vm.swappiness", "value": "1"}]},
            {"title": "durable", "changes": [{"variable": "innodb_flush_log_at_trx_commit", "value": "2"}], "risk": "durability"},
            {"title": "ok", "changes": [{"variable": "innodb_lru_scan_depth", "value": "256"}], "expected_effect": "small", "risk": "none"}]
    items, rejected = llmassist.sanitize(sugg, ctx, {"items": []}, "test-model")
    assert [r["title"] for r in rejected] == ["deny", "missing", "tried", "too big", "os off"], rejected
    by = {i["title"]: i for i in items}
    assert by["durable"]["safety"] == "trade-off" and by["ok"]["safety"] == "safe" and by["ok"]["source"] == "llm"
    assert by["ok"]["id"] == "llm-innodb-lru-scan-depth"
    ctxt = llmassist.context({"variables": VARS, "hw": HW, "summary": OLTP[0], "inputs": OLTP[1], "history": [], "goal": "tps"}, res)
    assert "datadir" not in ctxt["current_variables"] and ctxt["summary"]["tps"] == 600


def test_no_run_yet():
    res = run({}, {}, policy="safe")
    assert isinstance(res["items"], list)


def test_verdicts():
    base = [{"seq": i, "managed": {}, "goal_value": v, "summary": {"cpu_busy": 95, "mysqld_cpu_ms_per_trx": 2.0}} for i, v in ((1, 560), (2, 565), (3, 558))]
    noise = evaluate.calibrate(base)
    v, _ = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 600, "summary": {"cpu_busy": 95}}, base, "tps", noise)
    assert v == "improved"
    v, _ = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 562, "summary": {"cpu_busy": 95, "mysqld_cpu_ms_per_trx": 1.7}}, base, "tps", noise)
    assert v == "efficiency-gain"
    v, ev = evaluate.verdict({"managed": {}, "goal_value": 520, "summary": {}}, base, "tps", noise)
    assert v == "repeat" and ev.get("drift")


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa
                fails += 1
                import traceback
                print("FAIL", name, repr(e))
                traceback.print_exc()
    sys.exit(1 if fails else 0)
