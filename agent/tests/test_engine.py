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
    # noise 1%: flag from 0.25% estimated impact, unreliable from 0.5%
    v, d = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 130, "summary": {"cpu_steal": 0.6, "cpu_busy": 90}}, [base], "tps", {"noise_pct": 1})
    assert v == "unreliable" and d["steal"]["level"] == "invalid"
    v, d = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 103, "summary": {"cpu_steal": 0.3, "cpu_busy": 90}}, [base], "tps", {"noise_pct": 1})
    assert v == "improved" and d["steal"]["level"] == "flag"
    # an I/O-bound run (20% CPU busy) is hurt less by the same steal
    v, d = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 103, "summary": {"cpu_steal": 0.6, "cpu_busy": 20}}, [base], "tps", {"noise_pct": 1})
    assert v == "improved" and "steal" not in d
    v, d = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 103, "summary": {"cpu_steal": 0.05, "cpu_steal_max": 1.5}}, [base], "tps", {"noise_pct": 1})
    assert "steal" not in d  # normal for these hosts
    v, d = evaluate.verdict({"managed": {"a": "1"}, "goal_value": 103, "summary": {"cpu_steal": 0.1, "cpu_steal_max": 7}}, [base], "tps", {"noise_pct": 1})
    assert d["steal"]["level"] == "flag"
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


def test_stack_small_wins():
    VARS["innodb_adaptive_hash_index"] = "OFF"; VARS["os:malloc"] = "glibc"
    try:
        h = [{"seq": i, "label": "x", "managed": {}, "verdict": "repeat", "goal_value": 100, "changes": []} for i in range(1, 7)]
        h.append({"seq": 7, "label": "ahi-on", "verdict": "neutral", "managed": {"innodb_adaptive_hash_index": "ON"}, "goal_value": 101.2,
                  "evaluation": {"goal_delta_pct": 1.2}, "changes": [{"variable": "innodb_adaptive_hash_index", "source": "rec:ahi-on"}]})
        h.append({"seq": 8, "label": "os-jemalloc", "verdict": "neutral", "managed": {"os:malloc": "jemalloc"}, "goal_value": 100.6,
                  "evaluation": {"goal_delta_pct": 0.6}, "changes": [{"variable": "os:malloc", "source": "rec:os-jemalloc"}]})
        items = engine.stack_small_wins([], {"variables": dict(VARS), "history": h, "noise_pct": 1.5, "goal": "tps"})
        assert items and items[0]["id"] == "stack-small-wins" and {c["variable"] for c in items[0]["changes"]} == {"innodb_adaptive_hash_index", "os:malloc"}
    finally:
        VARS["innodb_adaptive_hash_index"] = "OFF"; VARS.pop("os:malloc", None)


def test_env_shift_only_for_compared_runs_and_beyond_probe_noise():
    from tuning_agent import evaluate
    noise = {"noise_pct": 2, "probe": {"probe_fsync_per_s": 4035}, "probe_cv": {"probe_fsync_per_s": 8.0}}
    v, d = evaluate.verdict({"managed": {}, "goal_value": 100, "summary": {"probe_fsync_per_s": 4887}}, [], "tps", noise)
    assert v == "reference" and "environment_shift" not in d
    base = [{"seq": 1, "managed": {}, "goal_value": 100, "summary": {}, "verdict": "reference"}]
    run = {"managed": {"a": "1"}, "goal_value": 100.5, "summary": {"probe_fsync_per_s": 4887}}  # +21%, below 3 x 8% CV
    assert "environment_shift" not in evaluate.verdict(run, base, "tps", noise)[1]
    run["summary"]["probe_fsync_per_s"] = 6000  # +49%
    assert "environment_shift" in evaluate.verdict(run, base, "tps", noise)[1]


def test_engine_smoke_all_branches():
    """Run every rule branch once: binlog on and off, CPU-bound with a profile, I/O-bound, OLAP; no exceptions."""
    for extra_v in ({"log_bin": "ON", "binlog_transaction_dependency_history_size": "25000"}, {"log_bin": "OFF"}):
        for summ, inp in (OLTP, OLAP, (dict(OLTP[0], cpu_busy=97, bp_hit_pct=90, bp_disk_reads=900, history_list_length_max=500000,
                                            checkpoint_age_pct_max=85, dbproc_runq_wait_pct=30, swap_in=500), dict(OLTP[1], cpu_profile={"categories": {"spin / mutex wait": 20, "memory allocator": 10}}))):
            for policy in ("safe", "benchmark"):
                for goal in ("tps", "latency"):
                    ctx = {"variables": dict(VARS, **extra_v), "summary": summ, "inputs": inp, "hw": dict(HW, cpus=16), "policy": policy,
                           "goal": goal, "flavor": "mysql", "history": [{"seq": 1, "managed": {}, "goal_value": 1}], "os_tuning": True}
                    engine.run_engine(ctx)


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


def test_benchmark_requirements_not_lowered():
    req = {"max_connections": "1100", "max_prepared_stmt_count": "1048576"}
    assert engine._breaks_requirement({"changes": [{"variable": "max_connections", "proposed": "500"}]}, req)
    assert not engine._breaks_requirement({"changes": [{"variable": "max_connections", "proposed": "2000"}]}, req)
    assert not engine._breaks_requirement({"changes": [{"variable": "innodb_log_writer_threads", "proposed": "OFF"}]}, req)


def test_cross_class_experience_does_not_demote():
    # losses from other workloads / machine sizes / concurrency count half and never send a change to the end
    base = {"goal": "tps", "policy": "safe", "os_tuning": True, "os_facts": {"os:filesystem": "ext4"}, "hw": HW, "flavor": "percona",
            "version": (8, 4, 11), "variables": dict(VARS), "summary": OLTP[0], "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    first = next(i for i in engine.run_engine(base)["items"] if not i.get("priority") and i["kind"] == "config")
    bad = {"n": 6, "wins": 0, "losses": 6}

    def score(exp):
        its = engine.run_engine({**base, "experience": {first["id"]: exp}})["items"]
        return next(i for i in its if i["id"] == first["id"])["score"]
    plain, comparable = score({}), score(bad)
    other = score(dict(bad, scope="other workloads / machine sizes / concurrency"))
    assert comparable < other < plain
    assert plain - other <= 1.5  # half-weight evidence only, no demotion


def test_best_config_confirmed_improvement_wins():
    # session #58: noisy calibration (877 / 818 / 799, threshold 9.95%); log_writer_threads=OFF confirmed at 907 / 911
    import json
    h = json.load(open(os.path.join(os.path.dirname(__file__), "session58.json")))
    assert engine.best_config(h, "tps", 9.95)["seqs"] == [6, 7]
    assert round(engine.mean_noise(9.95, 3, 2), 1) == 6.4 and engine.mean_noise(2.0, 1, 1) == 2.0
    # within the scaled threshold, the configuration kept over its (grand)parent still wins
    assert engine.best_config(h, "tps", 20.0)["seqs"] == [6, 7]


def test_noise_plan_picks_the_remedy():
    runs = lambda se: [{"summary": {"within_se_pct": se}} for _ in range(3)]
    # session #58: CV 3.98% but within-run noise predicts only ~0.9% -> between-run shifts, longer runs do not help
    p = evaluate.noise_plan(runs(0.92), {"goal_cv_pct": 3.98, "noise_pct": 9.95}, 300)
    assert p["action"] == "between-runs" and p["repeats"] >= 9
    # within-run dominated: longer runs, capped at 4x
    p = evaluate.noise_plan(runs(2.0), {"goal_cv_pct": 2.1, "noise_pct": 5.25}, 300)
    assert p["action"] == "longer-runs" and 300 < p["seconds"] <= 1200 and p["expected_noise_pct"] < 5.25
    assert evaluate.noise_plan(runs(0.3), {"goal_cv_pct": 0.5, "noise_pct": 1.25}, 300)["action"] == "ok"
    assert evaluate.verdict({"superseded": "x", "seq": 2}, [], "tps", {"noise_pct": 2})[0] == "superseded"


def test_profile_run_length_placeholders(tmp_path=None):
    import tempfile
    from tuning_agent import bench
    d = tempfile.mkdtemp()
    p = os.path.join(d, "b.ini")
    open(p, "w").write("[bench]\nname=x\nrun = sysbench --time={total} run; hdb {warmup_min} {duration_min}\nwarmup = 120\nduration = 300\n")
    b = bench.Profile(p)
    assert b.can_resize() and b.command() == "sysbench --time=450 run; hdb 2 5"
    b.duration = 900
    assert b.command() == "sysbench --time=1050 run; hdb 2 15"
    open(p, "w").write("[bench]\nname=x\nrun = sysbench --time=430 run\nwarmup = 120\nduration = 300\n")
    assert not bench.Profile(p).can_resize()


def test_data_state():
    runs = [{"goal_value": v, "summary": {"rows_changed_per_s": 900}} for v in (877, 818, 799)]
    d = evaluate.data_state_check(runs, "unspecified")
    assert d["status"] == "drifting" and d["trend_pct_per_run"] < -4
    assert evaluate.data_state_check(runs, "reset")["status"] == "ok"
    assert evaluate.data_state_check([dict(r, summary={"rows_changed_per_s": 0}) for r in runs], "unspecified")["status"] == "ok"
    assert abs(evaluate.trend_pct([100, 101, 102, 103], [0, 1, 2, 3]) - 0.985) < 0.01
    import tempfile
    from tuning_agent import bench
    p = os.path.join(tempfile.mkdtemp(), "b.ini")
    open(p, "w").write("[bench]\nname=x\nrun=true\ndata_state = seasoned\n")
    b = bench.Profile(p)
    assert b.data_state == "seasoned" and b.season_hours == 12 and b.season_min_hours == 1
    open(p, "w").write("[bench]\nname=x\nrun=true\nreset=/r.sh\n")
    assert bench.Profile(p).data_state == "reset" and bench.Profile(p).season_hours == 0


def _b1(sid):
    import json
    d = json.load(open(os.path.join(os.path.dirname(__file__), "battery1.json")))[str(sid)]
    return d


def _rejudge(sid, noise=None):
    d = _b1(sid)
    runs, out = d["runs"], {}
    nz = {"noise_pct": noise or d["noise_pct"], "runs": [1]}
    for i, r in enumerate(runs):
        v, ev = evaluate.verdict(r, runs[:i], d["goal"], nz)
        r["verdict"], r["evaluation"] = v, ev
        out[r["seq"]] = (v, ev.get("parent_runs"), ev.get("goal_delta_pct"))
    return runs, out


def test_battery1_parent_after_merged_revert():
    # M1 run 16: thread pool rolled back (rec:rollback-to-run-14) together with dirty-writeback: parent is run 14, not 15
    _, out = _rejudge(63)
    assert out[16][1] == [14] and out[16][0] == "neutral"
    # O3: a sweep point is compared with the sweep base; the sweep's restore applied with spin-loops is not its experiment
    _, out = _rejudge(73)
    assert out[9][1] == [7] and out[9][0] == "regressed"
    assert out[10][1] == [7] and out[10][0] == "neutral"


def test_battery1_confirmation_rejudges():
    # O2 run 7 jemalloc: +2.15% single run, the mean with its repeat is ~+0.5% -> not confirmed
    runs, out = _rejudge(56, noise=2.0)
    evaluate.confirm_pass(runs, "tps", {"noise_pct": 2.0})
    r7 = next(r for r in runs if r["seq"] == 7)
    assert r7["verdict"] == "neutral" and r7["evaluation"]["confirmed"] is False


def test_battery1_best_config_needs_kept_config():
    # M2: the best-safe was a single neutral sweep point (innodb_thread_concurrency=32) whose confirmation measured -2.2%
    d = _b1(67)
    runs, _ = _rejudge(67)
    evaluate.confirm_pass(runs, d["goal"], {"noise_pct": d["noise_pct"]})
    best = engine.best_config([r for r in runs if "trx_commit" not in json_dumps(r["managed"]) and "performance_schema" not in json_dumps(r["managed"])],
                              d["goal"], d["noise_pct"])
    assert "innodb_thread_concurrency" not in best["managed"] or best["n"] >= 2


def json_dumps(x):
    import json
    return json.dumps(x)


def test_io_diagnosis_needs_real_waiting():
    # O3-like: 4 of 829 running threads waiting on data I/O, disk 8.5% busy -> no I/O diagnosis
    ctx = {"summary": {"cpu_busy": 97, "threads_running_avg": 829, "disk_util": 8.5, "duration_s": 300, "tps": 2400},
           "inputs": {"wait_classes": {"data_io": 1200, "sync": 100, "table_io": 9000}}, "hw": {"cpus": 8, "ram_bytes": 16 * GiB},
           "variables": dict(VARS)}
    assert not any(d["type"] == "io" for d in engine.diagnose(ctx))
    ctx["summary"].update(disk_util=70, threads_running_avg=20)
    ctx["inputs"]["wait_classes"]["data_io"] = 3000
    assert any(d["type"] == "io" for d in engine.diagnose(ctx))


def test_llm_pseudo_variables_accepted():
    ctx = {"variables": dict(VARS, performance_schema="ON"), "flavor": "percona", "version": (9, 7, 2), "hw": HW, "os_tuning": True, "history": []}
    items, rej = llmassist.sanitize([{"title": "lite", "changes": [{"variable": "ps:profile", "value": "lite"}]},
                                     {"title": "bad", "changes": [{"variable": "ps:profile", "value": "off"}]}], ctx, {"items": []}, "m")
    assert [i["changes"][0]["proposed"] for i in items] == ["lite"] and "accepts" in rej[0]["reason"]


def test_profile_has_all_attributes():
    # a profile must carry every attribute _bench_run uses (a misplaced edit once dropped `probe`)
    import tempfile
    from tuning_agent import bench
    p = os.path.join(tempfile.mkdtemp(), "b.ini")
    open(p, "w").write("[bench]\nname=x\nrun=sysbench --time={total} run\nwarmup=120\nduration=300\n\n[requires]\nmax_connections=1100\n")
    b = bench.Profile(p)
    for attr in ("probe", "requires", "data_state", "adaptive", "max_duration", "season_hours", "timeout", "reset", "metrics"):
        assert hasattr(b, attr), attr
    assert b.probe is True and b.describe()["command"] == "sysbench --time=450 run"


def test_io_bound_kinds():
    from tuning_agent.mechanisms import relevance
    base = {"cpu_busy": 30, "cpu_iowait": 25, "disk_util": 95, "disk_await_ms": 3, "tps": 900, "duration_s": 600, "threads_running_avg": 12}
    read = dict(base, bp_disk_reads=4000, bp_hit_pct=93, disk_r_iops=5000, disk_w_iops=900, bp_wait_free=0, checkpoint_age_pct_max=30)
    flush = dict(base, bp_disk_reads=300, bp_hit_pct=99.5, disk_r_iops=400, disk_w_iops=6000, bp_wait_free=40, checkpoint_age_pct_max=85,
                 bp_dirty_pct_avg=88)
    ctx = lambda s: {"summary": s, "inputs": {}, "hw": {"cpus": 8, "ram_bytes": 16 * GiB}, "variables": dict(VARS)}
    dr, df = engine.diagnose(ctx(read)), engine.diagnose(ctx(flush))
    assert "io-read" in {d["type"] for d in dr} and "io-flush" not in {d["type"] for d in dr}
    assert "io-flush" in {d["type"] for d in df} and "io-read" not in {d["type"] for d in df}
    # read-bound: buffer pool growth is relevant, LRU depth (flushing) is not; flush-bound: the other way round
    assert relevance("bp-grow", dr)[0] > 0 and relevance("lru-depth", dr)[0] == 0
    assert relevance("lru-depth", df)[0] > 0 and relevance("read-ahead", df)[0] == 0


def test_sweep_point_parent_with_the_fields_the_agent_passes():
    # canary #90 run 9: the live verdict got a run without its label and compared the 2nd sweep point with the 1st
    base = {"innodb_log_writer_threads": "OFF"}
    hist = [
        {"seq": 6, "label": "log-writer-threads-off", "managed": dict(base), "goal_value": 1000, "verdict": "neutral", "changes": []},
        {"seq": 7, "label": "log-writer-threads-off (confirm)", "managed": dict(base), "goal_value": 1002, "verdict": "repeat", "changes": []},
        {"seq": 8, "label": "exp#51 innodb_thread_concurrency=4", "managed": dict(base, innodb_thread_concurrency="4"), "goal_value": 990,
         "verdict": "neutral", "changes": [{"variable": "innodb_thread_concurrency", "old": "0", "new": "4", "source": "exp:51"}]},
    ]
    run = {"seq": 9, "label": "exp#51 innodb_thread_concurrency=8", "managed": dict(base, innodb_thread_concurrency="8"), "goal_value": 995,
           "changes": [{"variable": "innodb_thread_concurrency", "old": "4", "new": "8", "source": "exp:51"}]}
    v, ev = evaluate.verdict(run, hist, "tps", {"noise_pct": 2.0, "runs": [1]})
    assert ev["parent_runs"] == [6, 7]


def test_change_buffering_both_ways():
    # like AHI: write-heavy workloads test the opposite of the current setting (8.4 default none, 9.7 default all)
    def ids(cb, hit):
        ctx = {"goal": "tps", "policy": "safe", "os_tuning": True, "os_facts": {"os:filesystem": "ext4"}, "hw": HW, "flavor": "percona",
               "version": (9, 7, 2), "variables": dict(VARS, innodb_change_buffering=cb), "summary": dict(OLTP[0], bp_hit_pct=hit),
               "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
        return {i["id"] for i in engine.run_engine(ctx)["items"]}
    assert "change-buffer-off" in ids("all", 99.9) and "change-buffer" not in ids("all", 99.9)
    assert "change-buffer" in ids("none", 97.0) and "change-buffer-off" not in ids("none", 97.0)
    assert "change-buffer" not in ids("none", 99.9)  # all pages in memory: nothing to buffer


def test_resource_tiebreak():
    # two configurations tie on throughput (within noise), neither was kept over the other: the leaner one wins
    base = {"summary": {"mysqld_cpu_ms_per_trx": 1.0, "disk_ios_per_trx": 4.0, "db_mem_mb": 20000}}
    lean = {"summary": {"mysqld_cpu_ms_per_trx": 0.8, "disk_ios_per_trx": 4.0, "db_mem_mb": 20000}}
    h = [dict(base, seq=1, label="baseline (1/2)", managed={"a": "1"}, goal_value=1000, verdict="improved", evaluation={}),
         dict(base, seq=2, label="baseline (2/2)", managed={"a": "1"}, goal_value=1002, verdict="repeat", evaluation={}),
         dict(lean, seq=3, label="x", managed={"a": "1", "b": "2"}, goal_value=1001, verdict="efficiency-gain",
              evaluation={"parent_runs": []}),
         dict(lean, seq=4, label="x (confirm)", managed={"a": "1", "b": "2"}, goal_value=999, verdict="repeat", evaluation={})]
    assert engine.best_config(h, "tps", 2.0)["seqs"] == [3, 4]
    # within 3% the resource use is not a reason: more runs / higher mean decide as before
    for r in h[2:]:
        r["summary"] = dict(r["summary"], mysqld_cpu_ms_per_trx=0.99)
        r["verdict"] = "neutral" if r["seq"] == 3 else "repeat"
    assert engine.best_config(h, "tps", 2.0)["seqs"] == [1, 2]


def test_no_mechanism_goes_last():
    # TPC-H SF10 (Battery 2 A2): 2946 read IOPS, 13 write IOPS, no redo - flushing knobs have nothing to act on
    from tuning_agent.mechanisms import missing_mechanism
    s = {"redo_mbs": 0.0, "bp_disk_reads": 2900, "disk_r_iops": 2946, "disk_w_iops": 13}
    assert missing_mechanism("sweep-io-capacity", s) and missing_mechanism("trx-commit-2", s)
    assert missing_mechanism("sweep-read-io-threads", s) is None and missing_mechanism("bp-grow", s) is None
    assert missing_mechanism("read-ahead", {"redo_mbs": 2.0, "bp_disk_reads": 0})
    ctx = {"goal": "latency", "policy": "safe", "os_tuning": True, "os_facts": {"os:filesystem": "ext4"}, "hw": HW, "flavor": "percona",
           "version": (9, 7, 2), "variables": dict(VARS), "summary": dict(OLAP[0], redo_mbs=0.0, bp_disk_reads=2900, cpu_iowait=8,
           disk_r_iops=2946, disk_w_iops=13, disk_util=70, bp_hit_pct=97.6), "inputs": OLAP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    items = engine.run_engine(ctx)["items"]
    flush = [i for i in items if i["id"] in ("sweep-io-capacity", "io-capacity-up", "redo-grow", "sweep-dirty-lwm")]
    others = [i for i in items if i["kind"] == "config" and not i.get("no_mechanism") and not i.get("priority")]
    for f in flush:
        assert f.get("no_mechanism") and "sweep" not in f
        assert all(o["score"] > f["score"] for o in others), (f["id"], f["score"])


def test_noise_estimate_robust_to_one_wild_pair():
    # Battery 2 O1 re-run: calibration ~1.4% CV, then one config repeated 1588 / 1385 (14% apart)
    runs = [{"seq": i, "managed": {}, "goal_value": v} for i, v in ((2, 1318), (3, 1288), (4, 1283))]
    runs += [{"seq": 5, "managed": {"a": "1"}, "goal_value": 1588}, {"seq": 6, "managed": {"a": "1"}, "goal_value": 1385}]
    runs += [{"seq": 9, "managed": {"b": "1"}, "goal_value": 1380}, {"seq": 12, "managed": {"b": "1"}, "goal_value": 1395}]
    est = evaluate.noise_estimate(runs)
    assert est["noise_pct"] < 7 and est.get("outlier_groups") == [[5, 6]], est  # was 12.6% without the cap
    plan = evaluate.noise_plan([{"summary": {"within_se_pct": 0.9}}] * 3, {"goal_cv_pct": 1.49, "noise_pct": 3.73}, 300)
    assert plan["repeats"] >= 2


def test_time_breakdown_drives_relevance():
    from tuning_agent import bottleneck
    from tuning_agent.mechanisms import MECHANISMS
    rel = lambda item, parts: bottleneck.relevance(MECHANISMS[item][0], parts)[0]
    # CPU-bound, but a third of the CPU is spinning on latches: contention levers first, not memory or I/O
    spin = bottleneck.breakdown({"mysqld_cpu_pct": 90, "duration_s": 300, "dbproc_runq_wait_threads": 0.3},
                                {"cpu_profile": {"categories": {"spin / mutex wait": 35, "B-tree / row access": 40, "SQL executor": 25}}}, 8)
    assert spin[0]["name"].startswith("CPU: B-tree") or spin[0]["name"].startswith("CPU: spinning")
    assert rel("spin-loops", spin) == 3 and rel("bp-grow", spin) == 0 and rel("thread-pool", spin) >= 2
    # half the time waiting for data file reads: buffer pool / read path first
    io = bottleneck.breakdown({"mysqld_cpu_pct": 10, "duration_s": 300}, {"wait_classes": {"data_io": 1500, "redo_io": 60}}, 8)
    assert io[0]["name"] == "Waiting: data file I/O" and rel("bp-grow", io) == 3 and rel("spin-loops", io) <= 1  # 13% unprofiled CPU
    # the server mostly waits for the client: no server-side change gets credit for that time
    cl = bottleneck.breakdown({"mysqld_cpu_pct": 5, "duration_s": 300}, {"wait_classes": {"socket": 3000}}, 8)
    assert cl[0]["name"].startswith("Client / network") and rel("spin-loops", cl) == 0 and rel("bp-grow", cl) == 0
    # in the engine: the spin-heavy profile puts spin-loops ahead of an equally scored memory change
    ctx = {"goal": "tps", "policy": "safe", "os_tuning": True, "os_facts": {"os:filesystem": "ext4"}, "hw": HW, "flavor": "percona",
           "version": (8, 4, 11), "variables": dict(VARS), "summary": dict(OLTP[0], mysqld_cpu_pct=90, cpu_busy=95),
           "inputs": dict(OLTP[1], cpu_profile={"categories": {"spin / mutex wait": 35, "B-tree / row access": 65}}),
           "history": [], "noise_pct": 2.0, "managed": {}}
    res = engine.run_engine(ctx)
    assert res["breakdown"] and any(p["name"] == "CPU: spinning on latches" for p in res["breakdown"])
    sl = next((i for i in res["items"] if i["id"] == "spin-loops"), None)
    assert sl is None or sl["relevance"] >= 2


def test_hypotheses_rescore_and_learn():
    from tuning_agent.engine import hypothesis, rescore, credibility
    parts = [{"name": "CPU: spinning on latches", "threads": 3.0, "share_pct": 35.0, "tags": ["cpu-spin", "concurrency"]},
             {"name": "CPU: B-tree / row access", "threads": 5.0, "share_pct": 58.0, "tags": ["cpu"]}]
    it = {"id": "spin-loops", "kind": "config", "rationale": "Spinning wastes CPU. More text.", "title": "Spin loops"}
    h = hypothesis(it, {"breakdown": parts, "diagnosis": []})
    assert h["kind"] == "bottleneck" and "CPU: spinning on latches" in h["target"] and "Prediction" in h["text"]
    it["hypothesis"] = h
    # in-session: an earlier run shrank the spinning time without a gain -> ideas aimed at it rank lower
    d, why = rescore(it, [{"id": "thread-pool", "run": 7, "target": ["CPU: spinning on latches"], "tags": ["concurrency"], "outcome": "moved"}])
    assert d < 0 and "not what limits" in why[0]
    # across sessions: the same idea against the same component held 3 of 4 times in similar sessions
    d, note = credibility(it, {"similar": [{"id": "spin-loops", "target": "CPU: spinning on latches", "n": 4, "supported": 3, "refuted": 1}]})
    assert d > 0 and "3 of 4" in note


def test_read_ahead_experiments():
    def items(**kw):
        ctx = {"goal": "latency", "policy": "safe", "os_tuning": True, "os_facts": {"os:filesystem": "ext4"}, "hw": HW, "flavor": "percona",
               "version": (9, 7, 2), "variables": dict(VARS, innodb_read_ahead_threshold="56", innodb_random_read_ahead="OFF"),
               "summary": dict(OLAP[0], redo_mbs=0.0, bp_disk_reads=2900, disk_r_iops=2946, disk_w_iops=13, cpu_iowait=8, **kw),
               "inputs": OLAP[1], "history": [], "noise_pct": 2.0, "managed": {}}
        return {i["id"]: i for i in engine.run_engine(ctx)["items"]}
    it = items(read_ahead_per_s=300, read_ahead_evicted_per_s=10)
    assert "sweep-read-ahead" in it and it["sweep-read-ahead"]["sweep"]["values"][:2] == ["8", "32"] and "random-read-ahead" in it
    assert not it["sweep-read-ahead"].get("no_mechanism") and it["sweep-read-ahead"]["relevance"] >= 2
    wasted = items(read_ahead_per_s=300, read_ahead_evicted_per_s=200)
    assert "read-ahead-less" in wasted and "random-read-ahead" not in wasted and wasted["sweep-read-ahead"]["sweep"]["values"][0] == "64"


def test_apply_failure_is_a_failed_experiment():
    # EPYC 1000W: huge pages could not be reserved -> the applier raised and autotune crashed; it must come back as
    # a rolled-back result that autotune records as a failed experiment
    import inspect
    from tuning_agent import cli
    src = inspect.getsource(cli._apply_and_record)
    assert "except Exception" in src and '"rolled_back": True' in src


def test_experiment_run_ignores_restores_applied_before_it():
    # SATA validation: after a whole-config try (#82, kept), the script restored each variable with `tuning-agent apply`;
    # those restores were counted as part of #83 and it was compared with #82 instead of the baseline
    base = {"innodb_io_capacity": "2000"}
    mark = {"innodb_io_capacity": "10000", "innodb_max_dirty_pages_pct_lwm": "80"}
    hist = [{"seq": 2, "label": "baseline (1/3)", "managed": base, "goal_value": 92000, "verdict": "improved", "changes": []},
            {"seq": 3, "label": "baseline (2/3)", "managed": base, "goal_value": 92500, "verdict": "repeat", "changes": []},
            {"seq": 12, "label": "exp#82 combo", "managed": mark, "goal_value": 100000, "verdict": "improved",
             "changes": [{"variable": "innodb_io_capacity", "old": "2000", "new": "10000", "source": "exp:82"},
                         {"variable": "innodb_max_dirty_pages_pct_lwm", "old": None, "new": "80", "source": "exp:82"}]}]
    run = {"seq": 15, "label": "exp#83 innodb_change_buffering=none (1/2)", "managed": dict(base, innodb_change_buffering="none"),
           "goal_value": 92900, "changes": [
               {"variable": "innodb_io_capacity", "old": "10000", "new": "2000", "source": "cli"},
               {"variable": "innodb_max_dirty_pages_pct_lwm", "old": "80", "new": None, "source": "cli"},
               {"variable": "innodb_change_buffering", "old": "all", "new": "none", "source": "exp:83"}]}
    v, ev = evaluate.verdict(run, hist, "tps", {"noise_pct": 0.84, "runs": [2, 3]})
    assert ev["parent_runs"] == [2, 3] and v == "neutral", (v, ev.get("parent_runs"), ev.get("goal_delta_pct"))


def test_steady_state_and_warmup_check():
    from tuning_agent import collector
    def window(dirty_from, dirty_to, tps_from=1000, tps_to=1000, n=30):
        smp = [{"db": {"bp_dirty_pct": dirty_from + (dirty_to - dirty_from) * i / (n - 1), "tps": tps_from + (tps_to - tps_from) * i / (n - 1),
                       "checkpoint_age_pct": 40, "history_list_length": 1000}, "os": {}} for i in range(n)]
        return collector.steady_state(smp)
    growing = window(20, 60)
    assert growing["steady"] is False and any("dirty pages" in g for g in growing["growing"])
    assert window(40, 41)["steady"] is True
    assert window(40, 40, 1000, 900)["steady"] is False  # throughput sliding
    runs = [{"summary": {"steady_state": growing}} for _ in range(3)]
    w = evaluate.warmup_check(runs, 120)
    assert w["adequate"] is False and "120 s" in w["text"]
    assert evaluate.warmup_check([{"summary": {"steady_state": {"steady": True, "growing": []}}}] * 3, 120)["adequate"]
