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
    for k in ("os-dirty-writeback", "os-jemalloc", "os-autogroup"):
        assert k in it, (k, list(it))
    # best practices are bundled
    assert {"os-swappiness", "os-thp-off", "os-scheduler", "os-noatime", "os-aio"} <= set(it["best-practices"]["evidence"]["rules"])
    assert it["best-practices"]["priority"] and not {"os-swappiness", "os-noatime"} & set(it)
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


def test_filesystem_scope_and_allocator_variants():
    v = dict(VARS, **{"os:datadir_fs": "ext4"})
    base = {"goal": "tps", "policy": "safe", "hw": HW, "flavor": "percona", "version": (8, 4, 11), "summary": OLTP[0], "inputs": OLTP[1],
            "noise_pct": 2.0, "managed": {}, "os_tuning": True, "os_facts": {}}
    ids0 = {i["id"] for i in engine.run_engine({**base, "variables": v, "history": []})["items"]}
    assert not any(i.startswith("fs-") for i in ids0)  # filesystem not in scope -> no fs experiments
    ids1 = {i["id"] for i in engine.run_engine({**base, "variables": v, "history": [], "scope": ["knobs", "os", "filesystem"],
                                                "fs_device": "/dev/sdc"})["items"]}
    assert {"fs-xfs", "fs-zfs", "fs-btrfs"} <= ids1
    zres = engine.run_engine({**base, "variables": dict(v, **{"os:datadir_fs": "zfs:recordsize=16k"}), "history": [],
                              "scope": ["filesystem"], "fs_device": "/dev/sdc"})
    z = {i["id"]: i for i in zres["items"]}["zfs-doublewrite-off"]
    assert z["safety"] == "safe"
    hist = [{"seq": 5, "changes": [{"variable": "os:malloc", "new": "jemalloc"}], "verdict": "improved", "managed": {}}]
    ids2 = {i["id"] for i in engine.run_engine({**base, "variables": dict(v, **{"os:malloc": "jemalloc"}), "history": hist})["items"]}
    assert {"malloc-tcmalloc", "malloc-mimalloc", "malloc-jemalloc-tuned", "malloc-glibc-tuned"} <= ids2
    from tuning_agent import ostune
    assert ostune.allocator_env("glibc:arena_max=4")[-1] == "Environment=GLIBC_TUNABLES=glibc.malloc.arena_max=4"
    assert ostune.allocator_env("jemalloc:background_thread:true")[0] == "# os:malloc=jemalloc:background_thread:true"


def test_sweeps():
    res = run(*OLTP, policy="safe")
    it = {i["id"]: i for i in res["items"]}
    assert "sweep-redo" in it and len(it["sweep-redo"]["sweep"]["values"]) == 2
    items, _ = llmassist.sanitize([{"title": "s", "changes": [{"variable": "innodb_lru_scan_depth", "value": "512"}],
                                    "sweep_values": ["256", "512", "2048"], "risk": "none"}],
                                  {"variables": dict(VARS, innodb_lru_scan_depth="1024"), "hw": HW, "history": []}, {"items": []}, "m")
    assert items[0]["sweep"]["values"] == ["256", "512", "2048"]


def test_network_rules():
    v = dict(VARS, **{"os:net.core.somaxconn": "4096", "back_log": "151", "skip_name_resolve": "OFF", "os:rps": "off",
                      "os:net.core.netdev_max_backlog": "1000", "os:net.ipv4.tcp_slow_start_after_idle": "1",
                      "os:net.ipv4.tcp_congestion_control": "cubic", "os:net.netfilter.nf_conntrack_max": "65536"})
    base = {"goal": "tps", "policy": "safe", "hw": dict(HW, kernel="6.8.0-134-generic"), "flavor": "percona", "version": (8, 4, 11),
            "inputs": OLTP[1], "noise_pct": 2.0, "managed": {}, "os_tuning": True, "os_facts": {}, "history": []}
    quiet = engine.run_engine({**base, "variables": v, "summary": OLTP[0]})
    assert not [i for i in quiet["items"] if i["id"].startswith(("net-", "client-", "skip-name"))]  # no network signal -> nothing
    noisy = dict(OLTP[0], net_listen_overflows=3.0, net_conn_per_s=400, net_softnet_drops=2, softirq_max_cpu_pct=70,
                 softirq_avg_cpu_pct=20, rtt_avg_ms=18, conntrack_fill_pct=90, threads_running_avg=12)
    res = engine.run_engine({**base, "variables": v, "summary": noisy})
    it = {i["id"]: i for i in res["items"]}
    for k in ("net-listen-backlog", "skip-name-resolve", "client-pooling", "net-netdev-backlog", "net-rps", "net-conntrack",
              "net-no-slow-start-idle", "net-bbr", "client-compression", "eevdf-oversubscription"):
        assert k in it, (k, sorted(it))
    assert it["skip-name-resolve"]["safety"] == "trade-off" and it["skip-name-resolve"]["kind"] == "advice"  # semantics, safe policy
    assert {d["type"] for d in res["diagnosis"]} >= {"connections", "softirq", "network-rtt"}
    assert it["net-listen-backlog"]["relevance"] == 3


def test_client_rules():
    v = dict(VARS, **{"client:os:net.ipv4.tcp_tw_reuse": "2", "client:os:net.ipv4.ip_local_port_range": "32768\t60999",
                      "client:os:net.ipv4.tcp_slow_start_after_idle": "1", "client:os:rps": "off",
                      "client:os:net.ipv4.tcp_congestion_control": "cubic"})
    summ = dict(OLTP[0], client_net_time_wait=30000, client_net_conn_per_s=500, client_rtt_avg_ms=15, client_softirq_max_cpu_pct=80,
                client_softirq_avg_cpu_pct=20, client_cpu_busy=95)
    res = engine.run_engine({"goal": "tps", "policy": "safe", "hw": HW, "flavor": "percona", "version": (8, 4, 11), "variables": v,
                             "summary": summ, "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}, "os_tuning": True})
    it = {i["id"]: i for i in res["items"]}
    assert {"client-ports", "client-no-slow-start-idle", "client-rps"} <= set(it)
    assert [c["variable"] for c in it["client-ports"]["changes"]] == ["client:os:net.ipv4.tcp_tw_reuse", "client:os:net.ipv4.ip_local_port_range"]
    assert not it["client-ports"]["restart"]
    assert any(d["type"] == "client-bound" for d in res["diagnosis"])


def test_oom():
    res = run(dict(OLTP[0], oom_kills=1), OLTP[1], policy="safe")
    assert any(d["type"] == "oom" for d in res["diagnosis"])
    assert "bp-shrink" in {i["id"] for i in res["items"]}


def test_bp_rounding_and_mariadb_chunk():
    v = dict(VARS, innodb_buffer_pool_chunk_size=str(128 * 2 ** 20), innodb_buffer_pool_instances="2")
    assert engine.bp_round(v, 1920 * 2 ** 20) == 1792 * 2 ** 20 and engine.bp_round(v, 1920 * 2 ** 20, up=True) == 2048 * 2 ** 20
    res = run(dict(OLTP[0], swap_in=500, swap_out=50), OLTP[1], policy="safe")
    it = {i["id"]: i for i in res["items"]}
    assert int(it["bp-shrink"]["changes"][0]["proposed"].rstrip("M")) % 256 == 0
    from tuning_agent import baseline
    items, calc = baseline.generate(HW, {"innodb_buffer_pool_size": "134217728", "innodb_buffer_pool_chunk_size": "0", "innodb_log_file_size": "1"},
                                    "mariadb", (11, 8, 0))
    assert calc["buffer_pool"] > 0


def test_swap_cold_is_not_harmful():
    from tuning_agent.engine import harmful_swap
    assert not harmful_swap({"swap_out": 300, "swap_in": 2, "swap_used_mb": 400})
    assert harmful_swap({"swap_in": 500})
    d = engine.diagnose({"summary": {"swap_out": 300, "swap_in": 2, "swap_used_mb": 400}, "hw": HW})
    assert {x["type"] for x in d} >= {"swap-cold"} and "swap" not in {x["type"] for x in d}


def test_bp_overcommit_with_cold_memory():
    summ = dict(OLTP[0], bp_hit_pct=97.0, bp_disk_reads=400, cpu_busy=40, mem_available_mb=300, inactive_anon_mb=600,
                swap_total_mb=512, swap_used_mb=20, swap_in=0)
    inp = dict(OLTP[1], data_bytes=8 * GiB)
    VARS["os:swapfile"] = "0"
    try:
        it = {i["id"]: i for i in run(summ, inp, policy="safe")["items"]}
    finally:
        VARS.pop("os:swapfile")
    assert "bp-overcommit" in it, sorted(it)
    ch = dict((c["variable"], c["proposed"]) for c in it["bp-overcommit"]["changes"])
    assert ch.get("os:swapfile") == "1G" and "innodb_buffer_pool_size" in ch


def test_practice_bundle_bisect_and_keep():
    from tuning_agent.engine import practice_bundle, best_config, revert_suggestions
    items = [{"id": f"practice-{x}", "kind": "config", "safety": "safe", "confidence": 2, "risk": "low", "restart": False,
              "changes": [{"variable": x, "current": "a", "proposed": "b"}], "rationale": x} for x in ("v1", "v2", "v3", "v4")]
    def run_(seq, label, vs, verdict, managed, goal=100):
        return {"seq": seq, "label": label, "verdict": verdict, "goal_value": goal, "managed": managed,
                "changes": [{"variable": x, "new": "b", "source": "rec:" + label} for x in vs]}
    base = {"seq": 1, "label": "calibration", "verdict": "reference", "goal_value": 100, "managed": {}, "changes": []}
    first = practice_bundle(items, [base])
    assert first[0]["id"] == "best-practices" and len(first[0]["changes"]) == 4
    # neutral bundle is kept: no within-noise revert, and the best config is the one with the practices
    allb = {x: "b" for x in ("v1", "v2", "v3", "v4")}
    h = [base, dict(base, seq=2), dict(base, seq=3), run_(4, "best-practices", allb, "neutral", allb, 99.5)]
    assert not revert_suggestions(h, allb, "tps", 2.0, allb)
    assert best_config(h, "tps", 2.0)["managed"] == allb
    # regressed bundle: bisect, then exclude the culprit and keep the rest
    h = [base, run_(2, "best-practices", allb, "regressed", allb, 90)]
    nxt = practice_bundle(items, h)[0]
    assert nxt["id"] == "best-practices-bisect" and [c["variable"] for c in nxt["changes"]] == ["v1", "v2"]
    h.append(run_(3, "best-practices-bisect", ["v1", "v2"], "neutral", {"v1": "b", "v2": "b"}))
    nxt = practice_bundle(items, h)[0]
    assert [c["variable"] for c in nxt["changes"]] == ["v3", "v4"]
    h.append(run_(4, "best-practices-bisect", ["v3", "v4"], "regressed", {"v3": "b", "v4": "b"}, 90))
    nxt = practice_bundle(items, h)[0]
    assert [c["variable"] for c in nxt["changes"]] == ["v3"]
    h.append(run_(5, "best-practices-bisect", ["v3"], "neutral", {"v3": "b"}))
    nxt = practice_bundle(items, h)[0]
    assert [c["variable"] for c in nxt["changes"]] == ["v4"] or nxt["id"] == "best-practices", nxt


def test_option_file_has_only_mysql_options():
    from tuning_agent.applier import render
    out = render({"innodb_io_capacity": {"value": "2000"}, "os:thp": {"value": "never"}, "rg:innodb_bg_cpus": {"value": "1"},
                  "client:os:net.ipv4.tcp_slow_start_after_idle": {"value": "0"}})
    assert "innodb_io_capacity = 2000" in out and ":" not in out.replace("# ", "").split("[mysqld]")[1].replace("Managed", "")


def test_wait_profile_diagnosis():
    d = engine.diagnose({"summary": {"tps": 1000, "duration_s": 300}, "hw": HW,
                         "inputs": {"wait_classes": {"table_io": 900, "redo_io": 400, "data_io": 30}}})
    assert any(x["type"] == "commit" and "redo" in x["text"] for x in d), d


def test_steal_flags_and_invalidates():
    from tuning_agent import evaluate
    base = {"seq": 1, "managed": {}, "goal_value": 100, "summary": {}, "verdict": "reference"}
    v, d = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 130, "summary": {"cpu_steal": 9.5}}, [base], "tps", {"noise_pct": 2})
    assert v == "unreliable" and d["steal"]["level"] == "invalid"
    v, d = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 103, "summary": {"cpu_steal": 1, "cpu_steal_max": 20}}, [base], "tps", {"noise_pct": 2})
    assert v == "improved" and d["steal"]["level"] == "flag"
    # unreliable runs never win
    h = [base, {"seq": 2, "managed": {"a": "1"}, "goal_value": 200, "verdict": "unreliable", "summary": {}}]
    assert engine.best_config(h, "tps", 2.0)["managed"] == {}


def test_research_rules():
    VARS.update({"innodb_max_purge_lag": "0", "innodb_spin_wait_delay": "6"})
    try:
        summ = dict(OLTP[0], history_list_length_max=900000)
        it = ids(run(summ, dict(OLTP[1], cpu_profile={"categories": {"spin / mutex wait": 18}}), policy="benchmark"))
        assert "purge-lag" in it and "sweep-spin-wait-delay" in it, sorted(it)
    finally:
        for k in ("innodb_max_purge_lag", "innodb_spin_wait_delay"):
            VARS.pop(k)


def test_noise_is_measured_per_environment():
    from tuning_agent import evaluate
    mk = lambda seq, g, cfg="a": {"seq": seq, "goal_value": g, "managed": {"x": cfg}}
    # back-to-back calibration only: conservative until there is long-term evidence or history
    est = evaluate.noise_estimate([mk(1, 148367), mk(2, 148354), mk(3, 148368)])
    assert est["noise_pct"] == evaluate.MIN_GOAL_NOISE and not est["long_term_dof"]
    # a very stable earlier session on this environment + benchmark is enough to trust it
    assert evaluate.noise_estimate([mk(1, 148367), mk(2, 148354), mk(3, 148368)], prior_cv=0.05)["noise_pct"] == evaluate.RESOLUTION_NOISE
    # a time-separated re-check that drifted raises the threshold
    runs = [mk(1, 100), mk(2, 100.5), mk(3, 99.8), mk(4, 105, "b"), mk(5, 104, "c"), mk(6, 94)]
    est = evaluate.noise_estimate(runs)
    assert est["long_term_dof"] > 0 and est["noise_pct"] > 5, est
    # stable re-checks over time: threshold follows the measured variance
    runs = [mk(1, 1000), mk(2, 1001), mk(3, 999), mk(4, 1050, "b"), mk(5, 1000.5)]
    assert evaluate.noise_estimate(runs)["noise_pct"] < 1


def test_ps_lite_before_ps_off():
    VARS["ps:profile"] = "full"
    try:
        it = ids(run(*OLTP, policy="benchmark"))
        assert "ps-lite" in it and "ps-off" not in it and it["ps-lite"]["safety"] == "trade-off"
    finally:
        VARS.pop("ps:profile")
    from tuning_agent.applier import render
    out = render({"ps:profile": {"value": "lite"}, "innodb_io_capacity": {"value": "2000"}})
    assert "performance-schema-instrument = 'memory/%=OFF'" in out and "ps:profile" not in out.replace("# tuning-agent ps:profile=lite", "")


def test_io_bound_advanced_sweeps():
    VARS.update({"innodb_max_dirty_pages_pct_lwm": "10", "innodb_lru_scan_depth": "1024", "innodb_read_io_threads": "4"})
    try:
        hw = dict(HW, cpus=16, ram_bytes=32 * GiB)
        summ = dict(OLTP[0], bp_hit_pct=93.0, bp_disk_reads=3000, cpu_busy=55)
        ctx = {"variables": dict(VARS), "summary": summ, "inputs": OLTP[1], "hw": hw, "policy": "safe", "goal": "tps", "flavor": "mysql",
               "history": [{"seq": 1, "managed": {}, "goal_value": 1}], "os_tuning": True}
        it = {i["id"] for i in engine.run_engine(ctx)["items"]}
        assert {"sweep-dirty-lwm", "sweep-lru-depth", "sweep-read-io-threads"} <= it, sorted(it)
    finally:
        for k in ("innodb_max_dirty_pages_pct_lwm", "innodb_lru_scan_depth", "innodb_read_io_threads"):
            VARS.pop(k)


def test_parent_after_combined_revert_and_experiment():
    from tuning_agent import evaluate
    base = {"bp": "1G"}
    h = [{"seq": 1, "managed": base, "goal_value": 100, "verdict": "reference", "summary": {}},
         {"seq": 2, "managed": base, "goal_value": 100.2, "verdict": "repeat", "summary": {}},
         {"seq": 3, "managed": dict(base, fs="btrfs"), "goal_value": 80, "verdict": "regressed", "summary": {}}]
    run = {"managed": dict(base, wb="64M"), "goal_value": 100.1, "summary": {},
           "changes": [{"variable": "fs", "source": "rollback"}, {"variable": "wb", "source": "rec:os-dirty-writeback"}]}
    v, d = evaluate.verdict(run, h, "tps", {"noise_pct": 1.0})
    assert d["parent_runs"] == [1, 2] and v == "neutral", (v, d.get("parent_runs"), d.get("goal_delta_pct"))


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
