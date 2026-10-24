"""Offline engine smoke tests: synthetic run summaries for each workload type through every rule path."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tuning_agent.knowledge import parse_size
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
    for k in ("os-jemalloc", "os-autogroup"):
        assert k in it, (k, list(it))
    # dirty page cache limits only with a measured build-up: redo fsync'ed at every commit never piles up
    assert "os-dirty-writeback" not in it
    piled = ids(run(dict(OLTP[0], page_cache_dirty_mb_max=2400.0, page_cache_dirty_mb_avg=900.0), OLTP[1], policy="safe"))
    assert "os-dirty-writeback" in piled and "2,400 MB" in piled["os-dirty-writeback"]["rationale"]
    small = ids(run(dict(OLTP[0], page_cache_dirty_mb_max=40.0), OLTP[1], policy="safe"))
    assert "os-dirty-writeback" not in small
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
                 softirq_avg_cpu_pct=20, rtt_avg_ms=18, rtt_min_ms=17, conntrack_fill_pct=90, threads_running_avg=12)
    # a high connection rate alone is not evidence for skip_name_resolve: measured DNS time is
    res = engine.run_engine({**base, "variables": v, "summary": noisy})
    assert "skip-name-resolve" not in {i["id"] for i in res["items"]}
    dns = dict(OLTP[1], probes={"name_resolution": {"seconds": 12.0, "calls": 4000, "threads": 0.04}})
    res = engine.run_engine({**base, "variables": v, "summary": noisy, "inputs": dns})
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
    res = run(dict(OLTP[0], swap_in=2000, swap_out=50), OLTP[1], policy="safe")  # swapping that costs (>5 MB/s back in)
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
    # swap is extra OS tuning of this experiment: grown to about 1x RAM (the 512 MB partition counts)
    sw = parse_size(ch.get("os:swapfile") or "0")
    assert HW["ram_bytes"] - 1 * GiB <= sw + 512 * 2 ** 20 <= HW["ram_bytes"] and "innodb_buffer_pool_size" in ch, ch


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
        # what the LRU depth and the read threads act on: free-page waits, asynchronous (read-ahead) reads
        inp = dict(OLTP[1], status_rates=dict(OLTP[1].get("status_rates") or {}, Innodb_buffer_pool_wait_free=5, Innodb_buffer_pool_read_ahead=40))
        ctx = {"variables": dict(VARS), "summary": summ, "inputs": inp, "hw": hw, "policy": "safe", "goal": "tps", "flavor": "mysql",
               "history": [{"seq": 1, "managed": {}, "goal_value": 1}], "os_tuning": True}
        it = {i["id"] for i in engine.run_engine(ctx)["items"]}
        assert {"sweep-dirty-lwm", "sweep-lru-depth", "sweep-read-io-threads"} <= it, sorted(it)
        # without them: neither (r1: 7 neutral read-thread runs at read-ahead 0)
        it0 = {i["id"] for i in engine.run_engine(dict(ctx, inputs=OLTP[1]))["items"]}
        assert not {"sweep-lru-depth", "sweep-read-io-threads"} & it0, sorted(it0)
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


def test_variable_preconditions_cover_any_source():
    # session 111: an LLM proposed skip_name_resolve with no evidence of name resolution time
    from tuning_agent import mechanisms as m
    s = {"redo_mbs": 3.0, "bp_disk_reads": 0}
    var = [{"variable": "skip_name_resolve"}]
    assert "not measured" in m.missing_for_variables(var, s, {}, {})
    none = {"probes": {"name_resolution": {"seconds": 0.0, "calls": 0, "threads": 0}}}
    assert "measured none" in m.missing_for_variables(var, s, {}, none)
    some = {"probes": {"name_resolution": {"seconds": 4.2, "calls": 900, "threads": 0.014}}}
    assert m.missing_for_variables(var, s, {}, some) is None
    assert m.missing_for_variables([{"variable": "innodb_read_ahead_threshold"}], s, {}, {})
    assert m.missing_for_variables([{"variable": "thread_pool_size"}], s, {}, {}) is None  # no known precondition
    # measured DNS time becomes a component of the time breakdown that skip-name-resolve acts on
    from tuning_agent import bottleneck
    parts = bottleneck.breakdown({"duration_s": 300, "mysqld_cpu_pct": 100}, some, 4)
    assert any(p["name"].startswith("Connection setup: name resolution") for p in parts)
    assert bottleneck.relevance(("dns",), parts)[1]

def test_offcpu_classification():
    from tuning_agent import probes as pr
    k_futex = "finish_task_switch\nschedule\nfutex_wait_queue\n__futex_wait"
    k_io = "finish_task_switch\nschedule\nio_schedule\nfolio_wait_bit_common"
    k_poll = "finish_task_switch\nschedule\nschedule_hrtimeout_range_clock\nschedule_hrtimeout_range"
    # waiting for the group commit leader, not for the client (do_command is deeper in the same stack)
    u = "pthread_cond_wait\nCommit_stage_manager::enroll_for\nMYSQL_BIN_LOG::ordered_commit\nha_commit_trans\ndispatch_command\ndo_command"
    assert pr.offcpu_category(u, k_futex)[0].startswith("Waiting: commit - group commit queue")
    # the redo fsync inside the binlog group commit is a redo flush wait (nearest meaningful frame), not a page fault
    u = "fdatasync\nos_file_flush_func\nLog_file_handle::fsync\nlog_flush_low\nlog_write_up_to\ninnobase_flush_logs\nMYSQL_BIN_LOG::ordered_commit"
    assert pr.offcpu_category(u, k_io)[0] == "Waiting: commit - redo flush"
    assert pr.offcpu_category("ppoll\nvio_io_wait\nvio_read\nnet_read_packet\nmy_net_read\ndo_command", k_poll)[0] == "Waiting: client (next statement)"
    assert pr.offcpu_category("pread64\nos_file_read_func\nbuf_read_page\nbuf_page_get_gen\nrow_search_mvcc", k_io)[0] == "Waiting: data page read"
    assert pr.offcpu_category("x\nbuf_LRU_get_free_block\nbuf_page_init_for_read", k_futex)[0].startswith("Waiting: free buffer pool page")
    assert pr.offcpu_category("x\ngetnameinfo\nip_to_hostname\nlogin_connection", k_poll)[0].startswith("Connection setup: name resolution")
    assert pr.offcpu_category("memcpy\nrow_sel", "finish_task_switch\nschedule\nio_schedule\nexc_page_fault")[0] == "Waiting: page fault / swap-in"
    out = "@off[connection, \n    k1\n    k2\n, \n    u1\n    u2\n]: 1500000000\n"
    assert pr.parse_offcpu(out) == [("connection", "k1\nk2", "u1\nu2", 1500000000)]


def test_breakdown_sources_and_evidence_gate():
    from tuning_agent import bottleneck
    summ = {"duration_s": 100, "mysqld_cpu_pct": 200, "tps": 1000}
    # file I/O split: data reads are the read path, data writes flushing - a flushing change has no evidence on a read-bound run
    fio = {"data": {"read_s": 300.0, "write_s": 0.5, "sync_s": 0.1}, "redo": {"write_s": 0.2, "sync_s": 0.3}}
    parts = bottleneck.breakdown(summ, {"file_io": fio}, 4)
    names = {p["name"] for p in parts}
    assert "Waiting: data file reads" in names and "Waiting: data file I/O" not in names
    assert bottleneck.acted_on(("io", "io-read", "memory"), parts)  # read-ahead: data reads
    flush = bottleneck.acted_on(("redo", "io", "io-flush"), parts)  # io_capacity: only the tiny write component
    assert [p["name"] for p in flush] == ["Waiting: data file writes / fsync (flushing)"]
    ctx = {"breakdown": [p for p in parts if p["name"] != "Waiting: data file writes / fsync (flushing)"], "diagnosis": []}
    assert engine.no_evidence({"id": "sweep-io-capacity"}, ctx)
    assert engine.no_evidence({"id": "sweep-read-ahead"}, ctx) is None
    # a diagnosis from counters / system statistics is evidence too (combination of sources)
    assert engine.no_evidence({"id": "sweep-io-capacity"}, dict(ctx, diagnosis=[{"type": "io-flush", "severity": "medium"}])) is None
    # the off-CPU profile replaces the performance_schema wait classes
    off = {"foreground": {"Waiting: commit - redo flush": {"threads": 3.0, "seconds": 30, "tags": ["commit"]},
                          "Waiting: client (next statement)": {"threads": 5.0, "seconds": 50, "tags": ["network"]}}}
    parts = bottleneck.breakdown(summ, {"offcpu": off, "wait_classes": {"data_io": 99.0}}, 4)
    names = [p["name"] for p in parts]
    assert "Waiting: commit - redo flush" in names and "Waiting: data file I/O" not in names
    # uncovered components go to the bottleneck analysis
    from tuning_agent import llmassist
    res = {"breakdown": parts, "items": [{"kind": "config", "addresses": ["Waiting: commit - redo flush (30%)"]}]}
    assert "Waiting: commit - redo flush" not in [p["name"] for p in llmassist.uncovered(res)]
    sig = llmassist.signature({"variables": {"log_bin": "OFF"}, "hw": {"cpus": 12}, "goal": "tps", "flavor": "percona",
                               "version": (8, 4, 11), "inputs": {"offcpu": off}}, {"workload": {"type": "oltp-write"}}, parts[0])
    assert sig["version"] == "8.4" and sig["cpus"] == "9-16" and sig["settings"] == {"log_bin": "OFF"}


def test_environment_disturbances():
    # session 88 calibration: mysqld swapping back in and the goal falling run after run
    runs = [{"goal_value": 177826, "summary": {"swap_in": 2374, "dbproc_swap_mb": 2294}},
            {"goal_value": 171427, "summary": {"swap_in": 2639, "dbproc_swap_mb": 2384}},
            {"goal_value": 164090, "summary": {"swap_in": 2417, "dbproc_swap_mb": 2452}}]
    d = evaluate.disturbances(runs + [{"goal_value": 156647, "summary": {"swap_in": 2022, "dbproc_swap_mb": 2407}}])
    assert len(d) == 2 and "swap" in d[0] and "same direction" in d[1]
    assert len(evaluate.disturbances(runs)) == 1  # three runs: a monotonic pair of steps is not evidence
    quiet = [{"goal_value": g, "summary": {"swap_in": 0, "dbproc_swap_mb": 10}} for g in (100, 103, 101)]
    assert evaluate.disturbances(quiet) == []


def test_storage_and_stability_signals():
    from tuning_agent import probes as pr, collector as col
    out = "@h[271581186, WS]: \n[64, 128)   90 |@@@@|\n[128, 256)   9 |@|\n[32K, 64K)   1 |@|\n\n@h[271581186, R]: \n[64, 128)  100 |@@|\n" \
          "@mx[271581186, WS]: 41000\n@mx[271581186, R]: 120\n"
    b = pr.parse_block(out)
    ws = b[(271581186, "WS")]
    assert ws["max_us"] == 41000 and pr._quantile(ws["buckets"], 0.5) == 128 and pr._quantile(ws["buckets"], 0.999) == 65536
    assert pr._op("WS") == "write" and pr._op("FWS") == "flush+write" and pr._op("F") == "flush" and pr._op("R") == "read"
    S = [{"db": {"tps": v}, "os": {"disk_w_await_ms": w}} for v, w in [(100, 1), (98, 1.2), (20, 9), (15, 12), (101, 1), (99, 1.1)]]
    st = col.within_run_stability(S)
    assert st["tput_dip_samples"] == 2 and st["tput_dip_longest_samples"] == 2
    sp = col.sample_spread(S, "os", ("disk_w_await_ms",))
    assert sp["disk_w_await_ms_max"] == 12 and sp["disk_w_await_ms_p50"] == 1.1
    # disturbances: storage latency drifting run after run, stalls inside the windows
    runs = [{"goal_value": g, "summary": {"disk_r_await_ms": r, "tput_dip_samples": d, "tput_dip_longest_samples": d}}
            for g, r, d in ((178, 4.2, 0), (171, 4.5, 0), (164, 4.8, 2), (157, 5.2, 0))]
    txt = " | ".join(evaluate.disturbances(runs))
    assert "data reads" in txt and "below half its median" in txt
    # swap-ins of mysqld memory break ties between configurations; they never promote a worse one (#125 run 8)
    sw = {"dbproc_swap_mb": 2300, "dbproc_workingset_refault_anon_per_s": 500}
    hist = [{"seq": 1, "label": "baseline", "managed": {"a": {"value": "1"}}, "verdict": "reference", "goal_value": 100, "summary": dict(sw)},
            {"seq": 2, "label": "x", "managed": {"a": {"value": "2"}}, "verdict": "efficiency-gain", "goal_value": 100.5,
             "summary": {"dbproc_swap_mb": 10, "dbproc_workingset_refault_anon_per_s": 0}, "evaluation": {"parent_runs": [1], "confirmed": True}}]
    assert engine.best_config(hist, "tps", 2.0)["seqs"] == [2]
    hist[1].update(verdict="regressed", goal_value=65)
    assert engine.best_config(hist, "tps", 2.0)["seqs"] == [1]
    assert engine.swap_hazard(sw) and not engine.swap_hazard({"dbproc_swap_mb": 10, "dbproc_workingset_refault_anon_per_s": 0, "swap_in": 40})


def test_evidence_gate_holes():
    from tuning_agent import mechanisms as m
    # zero counters are evidence that the event did not happen
    assert m.missing_for_variables([{"variable": "thread_cache_size"}], {"redo_mbs": 1}, {"Threads_created": 0})
    assert m.missing_for_variables([{"variable": "thread_cache_size"}], {"redo_mbs": 1}, {"Threads_created": 2.5}) is None
    assert m.missing_for_variables([{"variable": "os:net.ipv4.tcp_max_syn_backlog"}], {"net_tcp_req_q_full_drop": 0}, {})
    # an empty change buffer has size 1
    assert m.missing_mechanism("change-buffer-off", {"ibuf_size_max": 1, "redo_mbs": 1}, [], {}, {})
    assert m.missing_mechanism("change-buffer-off", {"ibuf_size_max": 900, "redo_mbs": 1}, [{"variable": "innodb_change_buffering"}], {}, {}) is None
    # Amdahl: a component below the noise threshold cannot show an effect
    parts = [{"name": "CPU: SQL execution", "threads": 5.0, "share_pct": 99.8, "tags": ["cpu", "cpu-exec"]},
             {"name": "Waiting: data file reads", "threads": 0.01, "share_pct": 0.2, "tags": ["io", "io-read"]}]
    assert "could not exceed" in engine.no_evidence({"id": "sweep-read-ahead"}, {"breakdown": parts, "diagnosis": [], "noise_pct": 2.0})
    parts[1]["share_pct"], parts[0]["share_pct"] = 20.0, 80.0
    assert engine.no_evidence({"id": "sweep-read-ahead"}, {"breakdown": parts, "diagnosis": [], "noise_pct": 2.0}) is None


def test_noise_model_fixes():
    # within-run estimate above the run-to-run CV: an unsteady window, never "longer runs"
    runs = [{"summary": {"within_se_pct": 9.4}} for _ in range(3)]
    p = evaluate.noise_plan(runs, {"goal_cv_pct": 1.8, "noise_pct": 4.6}, 300)
    assert p["action"] == "unsteady-window"
    # a plan never predicts more noise than now
    runs = [{"summary": {"within_se_pct": 1.0}} for _ in range(3)]
    p = evaluate.noise_plan(runs, {"goal_cv_pct": 1.6, "noise_pct": 4.0}, 300, max_duration=300)
    assert p["action"] != "longer-runs"
    # calibration slide beyond the runs' own precision
    sl = [{"goal_value": g, "summary": {"within_se_pct": 0.3}} for g in (100, 97, 94)]
    assert evaluate.calibration_slide(sl)
    assert evaluate.calibration_slide([{"goal_value": g, "summary": {"within_se_pct": 3}} for g in (100, 99.5, 99)]) is None
    # goal vs server throughput
    ratio = evaluate._goal_server_ratio([{"goal_value": 3750, "summary": {"tps": 3750}}, {"goal_value": 3700, "summary": {"tps": 3690}}])
    assert abs(evaluate.goal_server_check({"goal_value": 3135, "summary": {"tps": 3750}}, ratio)) > 10


def test_unsteady_and_window_goal():
    import tempfile
    from tuning_agent import bench
    # a regression measured while deferred work piled up decides nothing
    base = {"seq": 1, "label": "baseline", "managed": {"a": "1"}, "goal_value": 100.0, "verdict": "reference", "summary": {"tps": 100.0}}
    exp = {"seq": 2, "label": "x", "managed": {"a": "2"}, "goal_value": 80.0,
           "summary": {"tps": 80.0, "steady_state": {"steady": False, "growing": ["dirty pages % 40 -> 70"]}},
           "changes": [{"variable": "a", "old": "1", "new": "2", "source": "rec:x"}]}
    v, ev = evaluate.verdict(exp, [base], "tps", {"noise_pct": 2.0})
    # deferred work piling up flatters the run that defers it: it cannot explain a regression, which stands
    assert v == "regressed" and "cannot explain a regression" in ev.get("note", ""), (v, ev.get("note"))
    # an improvement measured while it piles up more debt than its parent did is inconclusive
    gain = dict(exp, goal_value=110.0, summary={"tps": 110.0, "steady_state": {"steady": False, "growing": ["dirty pages % 40 -> 70"],
                                                                              "evidence": {"bp_dirty_pct": [40, 70]}}})
    base_s = dict(base, summary={"tps": 100.0, "steady_state": {"steady": True, "growing": [], "evidence": {"bp_dirty_pct": [40, 41]}}})
    v, ev = evaluate.verdict(gain, [base_s], "tps", {"noise_pct": 2.0})
    assert v == "neutral" and ev.get("unsteady_verdict") == "improved", (v, ev.get("note"))
    # ... unless the parent piled up as much: compared alike, the improvement stands
    base_s["summary"]["steady_state"]["evidence"]["bp_dirty_pct"] = [40, 70]
    v, ev = evaluate.verdict(gain, [base_s], "tps", {"noise_pct": 2.0})
    assert v == "improved" and "compared alike" in ev.get("note", ""), (v, ev.get("note"))
    # ... but not a difference far beyond what unpaid deferred work could explain (session 210: baseline +419% vs defaults)
    huge = dict(exp, goal_value=520.0, summary=dict(exp["summary"], tps=520.0))
    v, ev = evaluate.verdict(huge, [base], "tps", {"noise_pct": 2.0})
    assert v == "improved" and "verdict stands" in ev.get("note", ""), (v, ev.get("note"))
    # sysbench goal from the interval lines inside the measured window
    d = tempfile.mkdtemp(); p = os.path.join(d, "x.ini")
    open(p, "w").write("[bench]\nname=x\nrun=true\nwarmup=20\nduration=30\n[metrics]\nsysbench_tps = transactions:\\s+\\d+\\s+\\(([\\d.]+) per sec\n")
    txt = "\n".join(f"[ {t}s ] thds: 8 tps: {100 if t <= 20 else 200}.00 qps: 1" for t in range(5, 55, 5)) + "\n    transactions:    7000 (140.00 per sec.)\n"
    out = bench.Profile(p).parse(txt)
    assert out["sysbench_tps"] == 200.0 and out["sysbench_tps_whole_run"] == 140.0


def test_param_catalog_and_policy():
    from tuning_agent import knowledge
    cat = knowledge.catalog()
    assert cat["innodb_buffer_pool_size"]["impact"] == "high" and cat["innodb_doublewrite"]["safety"] == "durability"
    assert knowledge.effective_impact("innodb_io_capacity", {"innodb_io_capacity": {"impact": "low"}}) == ("low", "learned")
    assert knowledge.effective_impact("innodb_io_capacity", {}) == ("medium", "shipped")
    # blocked settings never become experiments
    res = engine.run_engine({"goal": "tps", "policy": "benchmark", "hw": HW, "variables": dict(VARS), "summary": OLTP[0], "inputs": OLTP[1],
                             "history": [], "noise_pct": 2.0, "managed": {}, "blocked": ["innodb_buffer_pool_size", "innodb_io_capacity"]})
    assert not [i for i in res["items"] if any(c["variable"] in ("innodb_buffer_pool_size", "innodb_io_capacity") for c in i["changes"])]


def test_memory_budget():
    from tuning_agent import memory
    GiB = 2 ** 30
    hw = {"ram_bytes": 16 * GiB, "cpus": 8}
    v = {"innodb_buffer_pool_size": str(11 * GiB), "max_connections": "151", "sort_buffer_size": str(256 * 1024),
         "join_buffer_size": str(256 * 1024), "tmp_table_size": str(64 * 2 ** 20), "max_heap_table_size": str(64 * 2 ** 20),
         "table_open_cache": "4000", "performance_schema": "ON"}
    b = memory.budget(v, hw)
    assert not b["over_budget"] and b["active_connections"] == 16
    # 64 MB sort buffers for 16 active connections and a bigger pool: over the budget
    b2 = memory.budget(dict(v, sort_buffer_size=str(512 * 2 ** 20)), hw)
    assert b2["over_budget"]
    # a candidate that pushes the configuration over the budget becomes a memory trade-off
    ctx = {"goal": "tps", "policy": "benchmark", "hw": dict(HW, ram_bytes=16 * GiB, cpus=8), "variables": dict(VARS, **v),
           "summary": OLTP[0], "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    res = engine.run_engine(ctx)
    for it in res["items"]:
        for t in it.get("tradeoffs") or []:
            if t["category"] == "memory":
                assert it["safety"] == "trade-off"
    # runs that exceeded the budget are not production-safe
    from tuning_agent.knowledge import run_tradeoffs
    assert run_tradeoffs({"managed": {}, "summary": {"memory_budget": {"over_budget": True, "text": "x"}}})
    assert not run_tradeoffs({"managed": {}, "summary": {"memory_budget": {"over_budget": False}}})


def test_response_shapes():
    from tuning_agent import knowledge
    assert knowledge.response_shape("innodb_buffer_pool_size") == "saturating"
    assert knowledge.response_shape("innodb_thread_concurrency") == "optimum"
    assert knowledge.response_shape("optimizer_switch:mrr") == "binary"
    # saturating: upward only, smallest step first; optimum: both sides, nearest first
    assert engine.shaped_values("innodb_buffer_pool_size", ["8G", "16G", "12G"], "10G") == (["12G", "16G"], "saturating")
    vals, shape = engine.shaped_values("innodb_purge_threads", ["8", "16"], "4")
    assert shape == "optimum" and vals[0] == "2" and vals[1] == "8"


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
    assert rel("spin-loops", spin) >= 3 and rel("bp-grow", spin) == 0 and rel("thread-pool", spin) >= 2
    # half the time waiting for data file reads: buffer pool / read path first
    io = bottleneck.breakdown({"mysqld_cpu_pct": 10, "duration_s": 300}, {"wait_classes": {"data_io": 1500, "redo_io": 60}}, 8)
    assert io[0]["name"] == "Waiting: data file I/O" and rel("bp-grow", io) >= 3 and rel("spin-loops", io) <= 1  # 13% unprofiled CPU
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
    # optimum shape around the default 56: bracket first (32 below, 64 above), then farther (8)
    assert "sweep-read-ahead" in it and it["sweep-read-ahead"]["sweep"]["values"] == ["32", "64", "8"] and "random-read-ahead" in it
    assert not it["sweep-read-ahead"].get("no_mechanism") and it["sweep-read-ahead"]["relevance"] >= 2
    wasted = items(read_ahead_per_s=300, read_ahead_evicted_per_s=200)
    assert "read-ahead-less" in wasted and "random-read-ahead" not in wasted and wasted["sweep-read-ahead"]["sweep"]["values"][0] == "64"


def test_efficiency_gain_needs_its_repeat():
    # canary #141: group-commit-delay kept as an efficiency gain at -5.1% (within ±6.8%), its confirmation measured -10%:
    # the mean of both runs is worse than the parent beyond the threshold -> not kept, the best safe config goes back
    base = {"innodb_log_writer_threads": "OFF"}
    gcd = dict(base, binlog_group_commit_sync_delay="200")
    runs = [{"seq": s, "label": "log-writer-threads-off", "managed": base, "goal_value": g, "verdict": v, "evaluation": {}}
            for s, g, v in ((5, 801, "improved"), (6, 780, "repeat"), (10, 788, "repeat"), (14, 795, "repeat"))]
    runs.append({"seq": 15, "label": "group-commit-delay", "managed": gcd, "goal_value": 749, "verdict": "efficiency-gain",
                 "evaluation": {"parent_runs": [5, 6, 10, 14]}})
    # a single unconfirmed efficiency gain is not the best configuration yet (canary review: #202)
    assert engine.best_config(runs, "tps", 6.76)["seqs"] == [5, 6, 10, 14]
    runs.append({"seq": 21, "label": "best safe config, confirmation", "managed": gcd, "goal_value": 711, "verdict": "repeat", "evaluation": {}})
    ch = evaluate.confirm_pass(runs, "tps", {"noise_pct": 6.76})
    assert (15, "efficiency-gain", "neutral") in ch
    assert engine.best_config(runs, "tps", 6.76)["seqs"] == [5, 6, 10, 14]
    # a repeat that holds keeps it
    runs[-1]["goal_value"], runs[4]["verdict"], runs[4]["evaluation"] = 790, "efficiency-gain", {"parent_runs": [5, 6, 10, 14]}
    evaluate.confirm_pass(runs, "tps", {"noise_pct": 6.76})
    assert runs[4]["verdict"] == "efficiency-gain" and engine.best_config(runs, "tps", 6.76)["seqs"] == [15, 21]


def test_swap_hazard_needs_measured_cost():
    # AMD #136: 754 MB of mysqld in swap, ~20 pages/s faulted back, 0.003 s of 217 s off-CPU waiting on swap-in
    s = {"dbproc_swap_mb": 754, "dbproc_workingset_refault_anon_per_s": 20, "offcpu_swapin_share_pct": 0.001}
    assert not evaluate.swap_cost(s, 3.6) and not engine.swap_hazard(s, 3.6)
    assert evaluate.swap_cost(dict(s, offcpu_swapin_share_pct=12.0), 3.6)
    assert evaluate.swap_cost({k: v for k, v in s.items() if k != "offcpu_swapin_share_pct"}, 3.6)  # no profile: swap-in counts
    assert not evaluate.swap_cost(dict(s, dbproc_workingset_refault_anon_per_s=0), 3.6)


def _sweep_sim(values, goals, parent="0", var="innodb_thread_concurrency", noise=2.0, server=None):
    """Drive _run_experiment for an agent sweep with stubbed measurement: goals maps value -> goal (parent mean 1000)."""
    from tuning_agent import cli
    seen, kept = [], {}

    class St:
        def __init__(self):
            self.d = {"managed": {var: {"value": parent}} if parent is not None else {}}
        def get(self, k, default=None):
            return self.d.get(k, default)

    class Cl:
        def post(self, path, body):
            if body.get("results"):
                kept.update(body)
            return {}

    class Db:
        def variables(self):
            return server or {}

    class C:
        state, client, db = St(), Cl(), Db()
        def session(self):
            return {"goal": "tps", "noise_pct": noise, "runs": [{"seq": 2, "managed": {var: parent} if parent is not None else {},
                                                                 "goal_value": 1000, "verdict": "improved"}]}
    saved = (cli._validate_changes, cli._goto_managed, cli._bench_run, cli.log)
    try:
        cli._validate_changes = lambda c, arm, force=False: (arm, [])
        cli._goto_managed = lambda *a, **k: {}
        def bench(c, prof, label=None, **k):
            seen.append(label.split("=", 1)[1])
            assert len(seen) < 12, seen
            return {"goal_value": goals.get(seen[-1], 1000)}, None
        cli._bench_run, cli.log = bench, (lambda *a, **k: None)
        cli._run_experiment(C(), None, {"id": 1, "kind": "sweep", "created_by": "agent:x", "spec": {"variable": var, "values": values}})
    finally:
        cli._validate_changes, cli._goto_managed, cli._bench_run, cli.log = saved
    return seen, kept.get("results", {}).get("kept")


def test_sweep_measures_each_arm_once():
    # AMD #142: the repeat loop reused the arm index, so a 2-point agent sweep re-ran its second point forever.
    # Thread concurrency 0 = unlimited: 24 is the widest step down from it, screened first; flat -> 48 is not measured
    seen, kept = _sweep_sim(["48", "24"], {"48": 1001, "24": 1001})
    assert seen == ["24"] and kept is None, seen


def test_sweep_screens_then_refines():
    # #27: the optimum at the far end (concurrency 4) - the widest step finds it, nearer values are refined after it
    seen, kept = _sweep_sim(["16", "8", "4"], {"4": 1050, "8": 1003, "16": 1000})
    assert seen[0] == "4" and kept == "innodb_thread_concurrency=4", (seen, kept)
    # a hint (between half the threshold and the threshold) is not called flat: the next value is measured too
    seen, _ = _sweep_sim(["16", "8", "4"], {"4": 1015, "8": 1004, "16": 1000})
    assert seen[:2] == ["4", "16"], seen
    # more is better up to a point (thread_cache_size, saturating): refine upward from the smallest after a sensitive
    # widest step, stop at the knee and keep the cheapest value within noise of the best
    seen, kept = _sweep_sim(["64", "128", "256", "1024"], {"1024": 1060, "64": 1020, "128": 1055, "256": 1061},
                            parent="16", var="thread_cache_size")
    assert seen == ["1024", "64", "128"] and kept == "thread_cache_size=128", (seen, kept)
    # the parent value comes from the server when the setting is not managed yet
    seen, _ = _sweep_sim(["2000", "1000", "4000"], {}, parent=None, var="innodb_io_capacity", server={"innodb_io_capacity": "200"})
    assert seen == ["4000"], seen


def test_flat_sweep_retested_after_bottleneck_shift():
    # a sweep screened flat (only its widest point ran) counts as tried; once the share of time it acts on grows beyond
    # the noise threshold, it is tried again (local optimum guard for interacting settings)
    hist = [{"seq": 9, "label": "exp#5 innodb_thread_concurrency=8", "verdict": "neutral", "managed": {},
             "changes": [{"variable": "innodb_thread_concurrency", "old": "0", "new": "8", "source": "exp:5"}],
             "evaluation": {"hypothesis": {"kind": "bottleneck", "share_pct": 6.0, "target": ["CPU: spinning on latches"]}}}]
    it = {"id": "sweep-thread-concurrency", "kind": "config", "confidence": 2, "sweep": {"variable": "innodb_thread_concurrency", "values": ["32", "8"]},
          "changes": [{"variable": "innodb_thread_concurrency", "current": "0", "proposed": "32"}]}
    it = engine.apply_history([it], hist)[0]
    assert it["tried_in_run"] == 9 and it["confidence"] == 0
    it["hypothesis"] = {"kind": "bottleneck", "share_pct": 7.5, "target": ["CPU: spinning on latches"]}
    engine._retest_after_shift(it, {"history": hist, "noise_pct": 2.0})
    assert it["confidence"] == 0  # +1.5 points: within noise
    it["hypothesis"]["share_pct"] = 31.0
    engine._retest_after_shift(it, {"history": hist, "noise_pct": 2.0})
    assert it["confidence"] == 1 and it["retest"]["share_now_pct"] == 31.0


def test_swap_protection_and_memory_retest():
    # Scaleway hosts come with 0 swap: with OS tuning the agent tests a swap file (~1x RAM, capped by free disk) as a
    # protective change, and memory settings tried before the swap state changed are tried again
    def items(swap_mb, free=200 * GiB, os_tuning=True, ram=16 * GiB):
        ctx = {"goal": "tps", "policy": "safe", "os_tuning": os_tuning, "os_facts": {"os:filesystem": "ext4"},
               "hw": dict(HW, ram_bytes=ram, disk_free_bytes=free), "flavor": "percona", "version": (8, 4, 11),
               "variables": dict(VARS, **{"os:swapfile": "0"}), "summary": dict(OLTP[0], swap_total_mb=swap_mb),
               "inputs": OLTP[1], "history": [{"seq": 1, "label": "baseline", "verdict": "reference", "managed": {}, "changes": []}],
               "noise_pct": 2.0, "managed": {}}
        return {i["id"]: i for i in engine.run_engine(ctx)["items"]}
    it = items(0).get("best-practices-swap")
    assert it and it["changes"][0]["proposed"] == "16G" and it["impact"] == 3, it and it["changes"]
    assert items(0, free=12 * GiB)["best-practices-swap"]["changes"][0]["proposed"] == "5G"  # half of the free disk
    assert "best-practices-swap" not in items(16 * 1024) and "best-practices-swap" not in items(0, os_tuning=False)
    assert engine.is_practice_run({"label": "best-practices-swap"})  # kept unless it regresses
    # large hosts: 64 GiB is enough safety (a 251 GB host was offered a 251 GB file), and 64 GiB of swap already is enough
    assert items(0, free=2000 * GiB, ram=251 * GiB)["best-practices-swap"]["changes"][0]["proposed"] == "64G"
    assert "best-practices-swap" not in items(64 * 1024, free=2000 * GiB, ram=251 * GiB)
    from tuning_agent.memory import MEMORY_VARS
    ranked = items(0)
    mem = [i for i in ranked.values() if i["kind"] == "config" and any(c["variable"] in MEMORY_VARS for c in i["changes"])]
    assert all(i["score"] < ranked["best-practices-swap"]["score"] for i in mem), [(i["id"], i["score"]) for i in mem]
    hist = [{"seq": 6, "label": "bp-grow", "verdict": "neutral", "summary": {"swap_total_mb": 0},
             "changes": [{"variable": "innodb_buffer_pool_size", "old": "8G", "new": "10G", "source": "rec:bp-grow"}]}]
    mk = lambda: {"id": "bp-grow", "kind": "config", "confidence": 2,  # noqa: E731
                  "changes": [{"variable": "innodb_buffer_pool_size", "current": "8G", "proposed": "10G"}]}
    assert engine.apply_history([mk()], hist, 0)[0]["confidence"] == 0                      # same swap state: tried
    again = engine.apply_history([mk()], hist, 16384)[0]
    assert again["confidence"] == 2 and again["retest"]["swap_now_mb"] == 16384           # swap added: try again


def test_over_budget_beyond_swap_is_a_crash_not_a_tradeoff():
    # suite r1 RO-4: bp-grow to 12 GB on a 16 GB VM with 0.5 GB swap was run as a "memory trade-off" and the kernel
    # OOM-killed mysqld. Beyond RAM + free swap the experiment brings a swap file (OS tuning) or is not run.
    import json
    d = json.load(open(os.path.join(os.path.dirname(__file__), "suite_ro4_ctx.json")))
    v = dict(d["variables"], innodb_buffer_pool_size=str(10 * GiB))
    def grow(os_tuning):
        ctx = {"goal": "tps", "policy": "benchmark", "hw": d["hw"], "variables": v, "summary": d["summary"], "inputs": d["inputs"],
               "history": [{"seq": 1, "label": "baseline", "verdict": "reference", "managed": {}, "changes": []}], "noise_pct": 2.0,
               "managed": {}, "os_tuning": os_tuning, "os_facts": {}, "flavor": "percona", "version": (8, 4, 11)}
        return next(i for i in engine.run_engine(ctx)["items"] if i["id"] == "bp-grow")
    with_os = grow(True)
    ch = {c["variable"]: c["proposed"] for c in with_os["changes"]}
    assert ch["innodb_buffer_pool_size"] == "12G" and engine._swapfile_bytes(ch.get("os:swapfile")) >= 15 * GiB
    without = grow(False)
    assert without["confidence"] == 0 and "OOM-killed" in without["note"]
    ctx_ns = {"goal": "tps", "policy": "benchmark", "hw": d["hw"], "variables": v, "summary": d["summary"], "inputs": d["inputs"],
              "history": [{"seq": 1, "label": "baseline", "verdict": "reference", "managed": {}, "changes": []}], "noise_pct": 2.0,
              "managed": {}, "os_tuning": True, "no_swap": True, "os_facts": {}, "flavor": "percona", "version": (8, 4, 11)}
    ns = {i["id"]: i for i in engine.run_engine(ctx_ns)["items"]}
    assert ns["bp-grow"]["confidence"] == 0 and "no swap by design" in ns["bp-grow"]["note"] and "best-practices-swap" not in ns


def test_copy_on_write_check():
    # reflink clones of the pristine copy are always flagged; a copy-on-write file system can be intended by the profile
    from tuning_agent import cowcheck
    import tempfile
    calls = {"findmnt": "zfs tank/mysql", "filefrag": ""}
    saved = cowcheck._run
    try:
        cowcheck._run = lambda cmd, timeout=20: calls.get(cmd[0], "")
        r = cowcheck.check(tempfile.mkdtemp())
        assert r["fs"] == "zfs" and r["fs_issues"] and r["issues"] == r["fs_issues"]
        d = tempfile.mkdtemp()
        open(os.path.join(d, "t.ibd"), "wb").write(b"x" * 4096)
        calls.update(findmnt="xfs /dev/sda2", filefrag="   0:        0..       0:      34816..     34816:      1:             last,shared,eof\n")
        r = cowcheck.check(d)
        assert r["shared_files"] == 1 and not r["fs_issues"] and "reflink" in r["issues"][0]
        calls["filefrag"] = "   0:        0..       0:      34816..     34816:      1:             last,eof\n"
        assert cowcheck.check(d)["issues"] == []
    finally:
        cowcheck._run = saved
    run = {"summary": {"cow_issues": ["x share extents"]}}
    assert any("copy-on-write" in t for t in evaluate.disturbances([run, run]))
    assert not any("copy-on-write" in t for t in evaluate.disturbances([{"summary": {"cow_intended": ["ZFS"]}}] * 2))


def test_buffer_pool_size_kept_exact_and_mismatch_unreliable():
    # Scaleway: 32 instances turned a 45 GB pool into 48 GB (rounded to chunk x instances); the sweep's "win" was the
    # larger pool. A chunk size dividing size / instances keeps it exact; a run whose server values differ is unreliable.
    from tuning_agent.cli import _bp_companions
    cur = {"innodb_buffer_pool_size": str(45 * GiB), "innodb_buffer_pool_instances": "8", "innodb_buffer_pool_chunk_size": str(128 * 1024 ** 2)}
    comp = {c["variable"]: c["value"] for c in _bp_companions({"innodb_buffer_pool_instances": {"value": "32"}}, cur, "exp:1")}
    assert comp == {"innodb_buffer_pool_chunk_size": "120M"}, comp
    assert _bp_companions({"innodb_buffer_pool_size": {"value": "21G"}}, cur, "x") == []
    odd = {c["variable"]: c["value"] for c in _bp_companions({"innodb_buffer_pool_size": {"value": str(int(22.8 * GiB))}}, cur, "x")}
    assert odd["innodb_buffer_pool_chunk_size"] == "128M" and int(odd["innodb_buffer_pool_size"]) == 22 * GiB
    run = {"seq": 5, "label": "exp#9 innodb_buffer_pool_instances=32", "managed": {"innodb_buffer_pool_instances": "32"}, "goal_value": 313000,
           "summary": {"config_mismatch": [{"variable": "innodb_buffer_pool_size", "configured": "45G", "running": "51539607552"}]}}
    v, ev = evaluate.verdict(run, [], "tps", {"noise_pct": 2.0})
    assert v == "unreliable" and "51539607552" in ev["note"]


def test_cpu_profile_categories():
    # suite r1: pfs_spawn_thread (root of every connection thread) booked unmatched stacks as performance_schema (15-30%
    # instead of 2-7%); kernel psi_* (pressure stall info) too
    from tuning_agent.cpuprof import category
    root = ["handle_connection", "pfs_spawn_thread", "start_thread"]
    assert category(["Table_cache::get_table", "open_table", "mysql_execute_command"] + root) == "table open / cache"
    assert category(["mysql_execute_command", "dispatch_command"] + root) == "statement dispatch"
    assert category(["sp_head::execute"] + root) == "stored procedures"
    assert category(["psi_group_change", "psi_task_switch", "__schedule"]) == "scheduler / futex"
    assert category(["insert_events_statements_history", "pfs_end_statement_vc", "dispatch_command"]) == "performance_schema"
    assert category(["unknown_function"] + root) == "other"
    from tuning_agent.probes import _build_id
    assert _build_id(sys.executable) is None or len(_build_id(sys.executable)) >= 16


def test_within_noise_hint_repeat_resolves_small_gains():
    # r1 KV-1: noise +-4.36% asked for ~2 runs per configuration; 1.4-3% gains measured once came out "neutral". A run
    # between half the threshold and the threshold is measured again and the mean judged on the two-run threshold.
    base = {"a": "1"}
    runs = [{"seq": i, "managed": base, "goal_value": v, "verdict": "improved" if i == 2 else "repeat", "evaluation": {}}
            for i, v in ((2, 1000), (3, 1004), (4, 996))]
    runs.append({"seq": 5, "managed": dict(base, b="2"), "goal_value": 1030, "verdict": "neutral",
                 "evaluation": {"parent_runs": [2, 3, 4], "goal_delta_pct": 3.0, "goal_noise_pct": 4.0, "hint_repeat": True}})
    runs.append({"seq": 6, "managed": dict(base, b="2"), "goal_value": 1028, "verdict": "repeat", "evaluation": {}})
    ch = evaluate.confirm_pass(runs, "tps", {"noise_pct": 4.0})
    assert (5, "neutral", "improved") in ch and runs[3]["evaluation"]["confirmed"] is True, ch
    runs[3].update(verdict="neutral", evaluation={"parent_runs": [2, 3, 4], "goal_delta_pct": 3.0, "hint_repeat": True})
    runs[4]["goal_value"] = 1001  # the repeat did not hold
    evaluate.confirm_pass(runs, "tps", {"noise_pct": 4.0})
    assert runs[3]["verdict"] == "neutral"


def _fake_numa(nodes=2, free_gb=(0.5, 40.0), mem_gb=64):
    import tempfile
    root = tempfile.mkdtemp()
    for n in range(nodes):
        d = os.path.join(root, f"node{n}")
        os.makedirs(d)
        open(os.path.join(d, "cpulist"), "w").write(f"{n * 16}-{n * 16 + 15}")
        open(os.path.join(d, "meminfo"), "w").write(f"Node {n} MemTotal:       {mem_gb * 1048576} kB\nNode {n} MemFree:        {int(free_gb[n] * 1048576)} kB\n")
        open(os.path.join(d, "distance"), "w").write(" ".join("10" if m == n else "21" for m in range(nodes)))
        open(os.path.join(d, "numastat"), "w").write(f"numa_hit 1000\nnuma_miss {n * 50}\nnuma_foreign 0\ninterleave_hit 0\nlocal_node 900\nother_node {100 + n * 50}\n")
    return root


def test_numa_detection_and_rules():
    # a client's NUMA host: two nodes, the buffer pool landed on node 0 and filled it while node 1 stays free
    from tuning_agent import numa
    root = _fake_numa()
    topo = numa.topology(root)
    assert topo["nodes"] == 2 and topo["per_node"][0]["cpus"] == 16 and topo["distances"][0] == [10, 21]
    a = numa.counters(root, vmstat="/nonexistent")
    b = {"numastat": dict(a["numastat"], local_node=a["numastat"]["local_node"] + 900, other_node=a["numastat"]["other_node"] + 100,
                          numa_miss=a["numastat"]["numa_miss"] + 300), "free": a["free"], "vm": {}}
    r = numa.rates(a, b, 10)
    assert r["numa_other_node_pct"] == 10.0 and r["numa_miss_per_s"] == 30.0 and r["numa_node_free_min_mb"] == 512.0
    osf = {"os:numa_nodes": 2, "os:numa_node_mem_gb": "64.0,64.0", "os:numa_node_cpus": "16,16", "os:numa_node_cpulists": "0-15;16-31"}
    summ = dict(OLTP[0], numa_mysqld_max_node_pct=92.0, numa_node_free_min_mb=512.0, numa_node_free_max_mb=40960.0,
                numa_other_node_pct=10.0, numa_miss_per_s=30.0, mysqld_cpu_pct=900, db_mem_mb=60000)
    vars_ = dict(VARS, innodb_numa_interleave="OFF", innodb_buffer_pool_size=str(56 * GiB),
                 **{"os:vm.zone_reclaim_mode": "1", "os:kernel.numa_balancing": "1", "os:mysqld_numa_policy": "default"})
    ctx = {"goal": "tps", "policy": "safe", "os_tuning": True, "os_facts": osf, "hw": dict(HW, ram_bytes=128 * GiB, cpus=32),
           "flavor": "percona", "version": (8, 4, 11), "variables": vars_, "summary": summ, "inputs": OLTP[1],
           "history": [{"seq": 1, "label": "baseline", "verdict": "reference", "managed": {}, "changes": []}], "noise_pct": 2.0, "managed": {}}
    res = engine.run_engine(ctx)
    ids = {i["id"]: i for i in res["items"]}
    assert any(d["type"] == "numa" for d in res["diagnosis"])
    assert "numa-interleave" in ids and ids["numa-interleave"]["changes"][0]["proposed"] == "ON"
    assert "numa-interleave-all" in ids and "best-practices" in ids  # zone reclaim + balancing go into the bundle
    bundle = {c["variable"] for c in ids["best-practices"]["changes"]}
    assert {"os:vm.zone_reclaim_mode", "os:kernel.numa_balancing"} <= bundle
    assert "numa-single-node" not in ids  # 9 CPUs / 60 GB do not fit node 0 comfortably
    # uniform host: nothing NUMA
    ctx1 = dict(ctx, os_facts={"os:numa_nodes": 1})
    assert not any(i["id"].startswith(("numa-", "os-zone")) for i in engine.run_engine(ctx1)["items"])
    # a small server that fits one node: the single-node experiment
    # mysqld_cpu_pct is a share of the host: 10% of the host's CPUs; with measured remote allocations
    small = dict(ctx, summary=dict(summ, mysqld_cpu_pct=10, db_mem_mb=20000, numa_other_node_pct=8.0),
                 variables=dict(vars_, innodb_buffer_pool_size=str(16 * GiB)))
    assert "numa-single-node" in {i["id"] for i in engine.run_engine(small)["items"]}
    # no measured cross-node traffic: nothing to remove by binding (user 194: 0.0-0.47%)
    quiet = dict(small, summary=dict(small["summary"], numa_other_node_pct=0.4))
    assert "numa-single-node" not in {i["id"] for i in engine.run_engine(quiet)["items"]}
    # a 188 GiB buffer pool does not fit a 126 GiB node, whatever the PSS says (huge pages hid it)
    big = dict(small, variables=dict(vars_, innodb_buffer_pool_size=str(188 * GiB)))
    assert "numa-single-node" not in {i["id"] for i in engine.run_engine(big)["items"]}


def test_numa_mysql_side():
    # partitioned structures spread evenly over the nodes; latch spinning ranks higher on multi-node hosts
    ctx = {"os_facts": {"os:numa_nodes": 2}}
    assert engine._numa_multiple("innodb_buffer_pool_instances", "3", ctx) == "4"
    assert engine._numa_multiple("innodb_page_cleaners", "8", ctx) == "8"
    assert engine._numa_multiple("innodb_buffer_pool_instances", "3", {"os_facts": {"os:numa_nodes": 1}}) == "3"
    assert engine._numa_multiple("table_open_cache_instances", "63", {"os_facts": {"os:numa_nodes": 4}}) == "64"
    base = {"goal": "tps", "policy": "safe", "os_tuning": True, "hw": HW, "flavor": "percona", "version": (8, 4, 11),
            "variables": dict(VARS), "summary": dict(OLTP[0], mysqld_cpu_pct=90, cpu_busy=95),
            "inputs": dict(OLTP[1], cpu_profile={"categories": {"spin / mutex wait": 35, "B-tree / row access": 65}}),
            "history": [], "noise_pct": 2.0, "managed": {}}
    def spin(nodes):
        its = engine.run_engine(dict(base, os_facts={"os:filesystem": "ext4", "os:numa_nodes": nodes}))["items"]
        return next((i for i in its if i["id"] == "spin-loops"), None)
    one, two = spin(1), spin(2)
    assert one is None or two is None or two["score"] >= one["score"]
    if two:
        assert "NUMA" in (two.get("score_note") or "")


def test_memory_measured_by_mysql():
    from tuning_agent import collector
    M = 2 ** 20
    a = {"events": {"memory/innodb/buf_buf_pool": [21000 * M, 21000 * M], "memory/sql/THD::main_mem_root": [10 * M, 50 * M]}, "fg": 5 * M, "bg": 2 * M}
    b = {"events": {"memory/innodb/buf_buf_pool": [21000 * M, 21000 * M], "memory/sql/THD::main_mem_root": [400 * M, 420 * M],
                    "memory/performance_schema/x": [240 * M, 240 * M], "memory/innodb/os0event": [-5 * M, 3 * M]}, "fg": 380 * M, "bg": 2 * M}
    w = collector.memory_window(a, b)
    assert w["categories_mb"]["InnoDB buffer pool"] == 21000.0 and w["categories_mb"]["performance_schema"] == 240.0
    assert w["total_mb"] == 21640.0 and w["growth_mb"] == 630.0 and w["growing"][0]["event"] == "memory/sql/THD::main_mem_root"
    assert w["connections_mb"] == 380.0
    d = engine.diagnose({"summary": dict(OLTP[0]), "inputs": {"memory_pfs": w}, "hw": HW, "variables": dict(VARS)})
    assert any(x["type"] == "memory-growth" for x in d)


def test_evidence_weight_by_server_closeness():
    # the same wins count less when they come from another release series or another server variant
    base = {"goal": "tps", "policy": "safe", "os_tuning": True, "os_facts": {"os:filesystem": "ext4"}, "hw": HW, "flavor": "percona",
            "version": (8, 4, 11), "variables": dict(VARS), "summary": OLTP[0], "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    first = next(i for i in engine.run_engine(base)["items"] if not i.get("priority") and i["kind"] == "config")
    good = {"n": 6, "wins": 6, "losses": 0}
    def score(exp):
        its = engine.run_engine({**base, "experience": {first["id"]: exp}})["items"]
        return next(i for i in its if i["id"] == first["id"])["score"]
    same, family, other = score(good), score(dict(good, scope="same server variant, other release series", weight=0.6)), \
        score(dict(good, scope="other server variants", weight=0.3))
    assert same > family > other > score({}), (same, family, other)


def test_cluster_and_myrocks():
    # Galera flow control (measured paused time) -> applier threads; MyRocks block cache misses -> a larger cache;
    # durability trade-offs say what they mean in a cluster; MyRocks memory in the budget and the baseline split
    from tuning_agent import memory, baseline
    inputs = dict(OLTP[1], status_rates={"wsrep_flow_control_paused_ns": 0.3e9, "rocksdb_block_cache_data_miss": 900.0,
                                         "rocksdb_block_cache_data_hit": 9000.0})
    vars_ = dict(VARS, wsrep_applier_threads="4", rocksdb_block_cache_size=str(2 * GiB), rocksdb_db_write_buffer_size=str(GiB))
    ctx = {"goal": "tps", "policy": "benchmark", "os_tuning": False, "os_facts": {}, "hw": HW, "flavor": "pxc", "version": (8, 4, 3),
           "variables": vars_, "summary": OLTP[0], "inputs": inputs, "history": [{"seq": 1, "label": "baseline", "verdict": "reference",
           "managed": {}, "changes": []}], "noise_pct": 2.0, "managed": {}, "cluster": {"type": "galera", "size": 3},
           "engines": {"data": {"rocksdb": 40 * GiB, "innodb": 5 * GiB}, "primary": "rocksdb"}}
    res = engine.run_engine(ctx)
    ids = {i["id"]: i for i in res["items"]}
    types = {d["type"] for d in res["diagnosis"]}
    assert "flow-control" in types and "rocksdb-cache" in types
    assert ids["sweep-applier-threads"]["sweep"]["values"][0] == "8"
    assert ids["rocksdb-cache-grow"]["changes"][0]["proposed"] == "4G"
    commit = [i for i in res["items"] if any(c["variable"] == "innodb_flush_log_at_trx_commit" for c in i["changes"])]
    assert all("galera cluster of 3 nodes" in i["rationale"] for i in commit)
    b = memory.budget(vars_, HW)
    assert any(x["name"] == "MyRocks block cache" for x in b["items"])
    items, _ = baseline.generate(dict(HW, ram_bytes=32 * GiB), dict(VARS, rocksdb_block_cache_size="536870912"), "percona", (8, 4, 3),
                                 {"engines": {"data": {"rocksdb": 30 * GiB, "innodb": 10 * GiB}}})
    got = {i["variable"]: i.get("proposed", i.get("value", i.get("new"))) for i in items}
    from tuning_agent.knowledge import parse_size
    assert parse_size(got["rocksdb_block_cache_size"]) > parse_size(got["innodb_buffer_pool_size"])  # 75% of the data is MyRocks


def test_round_values():
    from tuning_agent.knowledge import nice_number, roundness
    assert nice_number("innodb_io_capacity", 1072) == 1000 and nice_number("innodb_io_capacity", 1500) == 1500
    assert nice_number("innodb_thread_concurrency", 24) == 24 and nice_number("table_open_cache", 6311) == 6000
    assert nice_number("sort_buffer_size", 300 * 1024) == 256 * 1024
    assert roundness("innodb_io_capacity", 1000) < roundness("innodb_io_capacity", 1072)
    assert engine.nice_values("innodb_io_capacity", ["1072", "1000", "2150"], "200") == ["1000", "2000"]
    # review 2026-10-20: rounding must not touch computed pseudo settings, fractions, unknown sizes; never crash
    assert nice_number("os:hugepages", 4192.0) == 4192.0 and nice_number("innodb_max_dirty_pages_pct_lwm", 0.5) == 0.5
    assert nice_number("innodb_max_dirty_pages_pct", 99.9) == 99.9 and engine.nice_value("os:swapfile", "7G") == "7G"
    assert roundness("innodb_flush_method", "O_DIRECT") == 2 and roundness("x", "inf") == 2 and roundness("x", "nan") == 2
    assert nice_number("innodb_buffer_pool_size", 22.8 * 1024 ** 3) == 22 * 1024 ** 3  # memory rounds down
    from tuning_agent import cli
    fm = {"arm": "innodb_flush_method=O_DIRECT_NO_FSYNC", "delta_pct": 5.5}
    assert cli._good_enough([fm, {"arm": "innodb_flush_method=O_DIRECT", "delta_pct": 5.0}], fm, 2.0, "innodb_flush_method", None, "choice")
    assert cli._extend_optimum([{"arm": "innodb_thread_concurrency=0", "status": "measured", "delta_pct": 9.0}], 8, 2.0, {}, "0",
                               {"variable": "innodb_thread_concurrency"}) is None
    best = {"arm": "innodb_io_capacity=1072", "delta_pct": 5.0}
    other = {"arm": "innodb_io_capacity=1000", "delta_pct": 4.2}
    assert cli._good_enough([best, other], best, 2.0, "innodb_io_capacity", 200, "optimum") is other

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



def test_runtime_changeability_by_release_series():
    from tuning_agent.knowledge import dynamic_in, series_of
    assert series_of("percona", "8.4.11-11") == "percona 8.4" and series_of("mariadb", "11.4.13-MariaDB") == "mariadb 11.4"
    assert dynamic_in("innodb_log_file_size", "mariadb 11.4") is True     # resized online on MariaDB
    assert dynamic_in("innodb_log_file_size", "percona 8.4") is False     # restart on MySQL / Percona
    assert dynamic_in("innodb_io_capacity", "percona 5.7") is None        # series not in the inventory: knowledge base decides
    assert dynamic_in("innodb_doublewrite", "percona 8.4") is None        # partly dynamic: knowledge base decides
    assert dynamic_in("no_such_variable", "percona 8.4") is None


def test_warmup_signals_catch_state_settling_inside_the_window():
    from tuning_agent import collector, evaluate
    # 15 min window, 10 s samples: checkpoint age 22% -> 29% plateau reached ~9 min in (the case in the chart), throughput flat
    def samples(ck):
        return [{"ts": 1000 + 10 * i, "db": {"checkpoint_age_pct": ck(i), "bp_dirty_pct": 2.5, "tps": 1000 + (i % 3)}} for i in range(90)]
    rising = samples(lambda i: min(29.0, 22.0 + 7.0 * i / 54))
    w = collector.warmup_signals(rising, warmup_s=900)
    assert w and [x["metric"] for x in w["settling"]] == ["checkpoint_age_pct"], w
    assert 360 <= w["settling"][0]["settled_after_s"] <= 600 and w["suggest_warmup_s"] >= 1380, w
    assert collector.steady_state(rising)["steady"]          # the end of the window is steady: no verdict change
    assert collector.warmup_signals(samples(lambda i: 29.0 + (i % 2) * 0.3), warmup_s=900) is None   # settled before the window
    assert collector.warmup_signals(samples(lambda i: min(29.0, 26.0 + 3.0 * i / 6)), warmup_s=900) is None  # settles in the first minute
    # the user's framing: the last 10-20% of the warm-up should look like the window
    tail = [{"ts": 1000 - 10 * i, "db": {"checkpoint_age_pct": 21.0, "bp_dirty_pct": 2.5, "tps": 1000}} for i in range(1, 14)]
    wt = collector.warmup_signals(rising, warmup_s=900, tail=tail)
    assert [x["metric"] for x in wt["tail_vs_window"]] == ["checkpoint_age_pct"] and "end of the warm-up" in wt["text"], wt
    flat = samples(lambda i: 29.0 + (i % 2) * 0.3)
    same = [{"ts": 1000 - 10 * i, "db": {"checkpoint_age_pct": 29.1, "bp_dirty_pct": 2.5, "tps": 1001}} for i in range(1, 14)]
    assert collector.warmup_signals(flat, warmup_s=900, tail=same) is None
    # calibration: a warning with a suggested warm-up, the runs stay valid
    runs = [{"summary": {"steady_state": collector.steady_state(rising), "warmup_signals": w}} for _ in range(3)]
    wu = evaluate.warmup_check(runs, 900)
    assert wu["adequate"] and wu["settling_runs"] == 3 and wu["suggest_warmup_s"] == w["suggest_warmup_s"], wu


def test_drive_write_cache_is_a_durability_tradeoff():
    # client report: turn the drive's write cache on (hdparm -W1) - a trade-off, measured only under the benchmark policy
    def items(policy, wc="OFF", w_ms=4.2):
        ctx = {"goal": "tps", "policy": policy, "os_tuning": True, "os_facts": {"os:filesystem": "ext4"}, "hw": HW,
               "flavor": "percona", "version": (8, 4, 11), "variables": dict(VARS, **{"os:disk_write_cache": wc}),
               "summary": dict(OLTP[0], disk_w_await_ms=w_ms, disk_flush_await_ms=w_ms), "inputs": OLTP[1],
               "history": [{"seq": 1, "label": "baseline", "verdict": "reference", "managed": {}, "changes": []}], "noise_pct": 2.0, "managed": {}}
        return {i["id"]: i for i in engine.run_engine(ctx)["items"]}
    adv = items("safe").get("os-disk-write-cache")
    assert adv and adv["kind"] == "advice", adv
    cfg = items("benchmark")["os-disk-write-cache"]
    assert cfg["kind"] == "config" and cfg["safety"] == "trade-off" and cfg["changes"][0]["proposed"] == "ON", cfg
    assert any(t.get("category") == "durability" for t in cfg.get("tradeoffs") or []), cfg.get("tradeoffs")
    assert "os-disk-write-cache" not in items("benchmark", wc="ON")        # already on
    assert "os-disk-write-cache" not in items("benchmark", w_ms=0.3)       # writes already fast: no evidence
    from tuning_agent.knowledge import TRADEOFFS
    assert TRADEOFFS["os:disk_write_cache"][0]("ON") and not TRADEOFFS["os:disk_write_cache"][0]("OFF")


def test_commit_bound_from_measured_wait_despite_group_commit():
    # session 194 run 2888: 76.5% of thread time waiting for the redo flush, 23 commits per fsync (0.04 fsyncs/trx) and
    # 21 ms writes; the fsyncs-per-transaction rule missed it, and no commit experiment was ranked
    bd = [{"name": "Waiting: commit - redo flush", "threads": 34.5, "tags": ["commit"], "share_pct": 76.5},
          {"name": "CPU: B-tree / row access", "threads": 1.3, "tags": ["cpu", "cpu-btree"], "share_pct": 3.0}]
    summ = dict(OLTP[0], tps=1294.0, data_fsyncs=55.4, log_fsyncs_per_s=37.7, disk_w_await_ms=20.9, disk_await_ms=20.9)
    ctx = {"goal": "tps", "policy": "safe", "os_tuning": True, "os_facts": {}, "hw": HW, "flavor": "percona", "version": (8, 4, 11),
           "variables": dict(VARS, log_bin="OFF", innodb_log_write_ahead_size="8192"), "summary": summ,
           "inputs": dict(OLTP[1], time_breakdown=bd, file_io={"redo": {"reads": 12, "writes": 9000, "syncs": 900}}),
           "history": [{"seq": 1, "label": "baseline", "verdict": "reference", "managed": {}, "changes": []}],
           "noise_pct": 19.7, "managed": {}}
    res = engine.run_engine(ctx)
    d = [x for x in res["diagnosis"] if x["type"] == "commit"]
    assert d and d[0]["severity"] == "high" and "InnoDB redo group commit: 34 commits per redo flush" in d[0]["text"] and "grouping is complete" in d[0]["text"]
    assert "binary log" not in d[0]["text"]  # binlog off: its group commit is irrelevant, res["diagnosis"]
    it = {i["id"]: i for i in res["items"]}
    assert "sweep-log-write-ahead" in it and {c["proposed"] for c in it["sweep-log-write-ahead"]["changes"]} <= {"4096", "16384"}
    adv = it["commit-storage-latency"]
    assert adv["kind"] == "advice" and "20.9 ms" in adv["rationale"] and "binary log" not in adv["rationale"]
    # 76% of the time on commits outranks everything else (relevance 4); measured run-queue time (9.7%) decides for the
    # scheduling-delay levers instead of the diagnosis
    assert it["sweep-log-write-ahead"]["relevance"] == 4
    bd[0]["cpu_queue_share_pct"] = 9.7
    summ2 = dict(summ, dbproc_runq_wait_threads=20.0, dbproc_threads=70.0, threads_running_avg=64.0)
    it2 = {i["id"]: i for i in engine.run_engine(dict(ctx, summary=summ2, inputs=dict(OLTP[1], time_breakdown=bd)))["items"]}
    tp = it2.get("thread-pool")
    assert tp is None or (tp.get("relevance") or 0) <= 1, tp and (tp.get("relevance"), tp.get("addresses"))


def test_group_commit_efficiency_achieved_vs_possible():
    # batches far below the commits waiting: the grouping loses, so a delay sized from the waiters is worth measuring
    from tuning_agent.engine import group_commit
    bd = [{"name": "Waiting: commit - redo flush", "threads": 800.0, "tags": ["commit"], "share_pct": 70.0}]
    s = {"tps": 20000.0, "log_fsyncs_per_s": 1000.0, "threads_running_avg": 1000.0}
    g = group_commit(s, {}, bd)
    assert g["batch"] == 20.0 and g["waiters"] == 800.0 and not g["complete"] and g["of_in_flight_pct"] == 2, g
    assert g["mechanism"] == "innodb"
    gb = group_commit(dict(s, duration_s=100.0), {"file_io": {"binlog": {"syncs": 50000}, "redo": {"syncs": 100000}}}, bd, "ON")
    assert gb["mechanism"] == "binlog" and gb["batch"] == 40.0 and gb["binlog_fsyncs_per_s"] == 500.0, gb
    g2 = group_commit({"tps": 1288.0, "log_fsyncs_per_s": 37.7, "threads_running_avg": 64.75},
                      {}, [{"name": "Waiting: commit - redo flush", "threads": 34.5, "tags": ["commit"]}])
    assert g2["complete"] and round(g2["batch"]) == 34, g2
    ctx = {"goal": "tps", "policy": "safe", "os_tuning": False, "os_facts": {}, "hw": HW, "flavor": "percona", "version": (8, 4, 11),
           "variables": dict(VARS, log_bin="ON", binlog_group_commit_sync_delay="0", binlog_group_commit_sync_no_delay_count="0"),
           "summary": dict(OLTP[0], tps=20000.0, log_fsyncs_per_s=1000.0, threads_running_avg=1000.0, disk_w_await_ms=0.8),
           "inputs": dict(OLTP[1], time_breakdown=bd), "history": [{"seq": 1, "label": "baseline", "verdict": "reference", "managed": {}, "changes": []}],
           "noise_pct": 2.0, "managed": {}}
    it = {i["id"]: i for i in engine.run_engine(ctx)["items"]}["group-commit-delay"]
    ch = {c["variable"]: c["proposed"] for c in it["changes"]}
    assert ch["binlog_group_commit_sync_no_delay_count"] == "800" and 100 <= int(ch["binlog_group_commit_sync_delay"]) <= 2000, ch
    assert "only 2% of the waiting commits" in it["rationale"], it["rationale"]
    # binary log off: no binlog setting; InnoDB's grouping by dedicated log threads instead
    ctx_off = dict(ctx, variables=dict(VARS, log_bin="OFF", innodb_log_writer_threads="OFF", binlog_group_commit_sync_delay="0"))
    it_off = {i["id"]: i for i in engine.run_engine(ctx_off)["items"]}
    assert "group-commit-delay" not in it_off and "log-writer-threads-on" in it_off, list(it_off)
    assert "InnoDB redo group commit" in it_off["log-writer-threads-on"]["rationale"]


def test_memory_budget_reserves_what_the_host_uses_outside_mysqld():
    # swap study 2026-10-21, tuner-vbm-amd: 30 GB RAM, ~6.6 GB held by the kernel outside every named counter; a 24 GB buffer
    # pool (25.4 GB resident) was OOM-killed at startup. 22 GB passes the 10% reserve, but leaves no margin next to the 6.6 GB
    from tuning_agent import memory
    GB = 2 ** 30
    hw = {"ram_bytes": 30 * GB, "cpus": 12}
    v = {"innodb_buffer_pool_size": str(22 * GB), "max_connections": "100"}
    other = memory.host_other_bytes({"mem_available_mb": 23.5 * 1024, "dbproc_rss_mb": 0.66 * 1024}, hw["ram_bytes"])
    assert 5.5 * GB < other < 6.2 * GB, other / GB
    assert not memory.budget(v, hw)["over_budget"]                       # the old 10% reserve let it through
    assert memory.budget(v, hw, host_other=other)["over_budget"]          # with the measured usage it does not fit
    assert memory.budget(v, hw, host_other=1 * GB)["os_reserve"] == int(3 * GB)  # small usage: the 10% floor stays


def test_noise_without_winners_curse_and_confirmation_on_repeats():
    # canary review 2026-10-22: selected first runs inflated the noise, the prior only ratcheted up, and confirmations
    # averaged the lucky first run in (log_writer_threads OFF +7.8% lost in #195)
    base = [{"seq": i, "label": f"baseline ({i}/3)", "goal_value": v, "managed": {}, "verdict": "repeat"} for i, v in ((1, 3300), (2, 3310), (3, 3290))]
    lucky = [{"seq": 4, "label": "lwt-off", "goal_value": 3560, "managed": {"lwt": "OFF"}, "verdict": "improved"},
             {"seq": 5, "label": "lwt-off (confirm)", "goal_value": 3430, "managed": {"lwt": "OFF"}, "verdict": "repeat"}]
    est = evaluate.noise_estimate(base + lucky)
    assert est["dof"] == 2 and est["pooled_cv_pct"] < 0.5, est           # the selected run 4 is not noise
    low = evaluate.noise_estimate(base, prior_cv=2.4, prior_dof=4)
    assert low["noise_pct"] < 2.5 * 2.4, low                               # pooled with the prior, not max()
    runs = base + [dict(lucky[0], evaluation={"parent_runs": [1, 2, 3]}), lucky[1]]
    ch = evaluate.confirm_pass(runs, "tps", {"noise_pct": 4.3})
    ev = runs[3]["evaluation"]
    assert ev["confirmed"] and ev["confirmed_runs"] == [5] and runs[3]["verdict"] == "improved", (ch, ev)
    # the repeat alone at +1% does not confirm
    runs[4]["goal_value"] = 3333
    evaluate.confirm_pass(runs, "tps", {"noise_pct": 4.3})
    assert runs[3]["verdict"] == "neutral" and "repeat run(s) [5]" in runs[3]["evaluation"]["note"]


def test_decision_rules_from_the_audit():
    # 2026-10-22 decision audit: storage-bound stop (user 194), concurrency limiters off commit-bound loads (lab 205 / 217),
    # evidence-backed trade-off first under benchmark policy, ceiling stop
    bd = [{"name": "Waiting: commit - redo flush", "threads": 34.5, "tags": ["commit"], "share_pct": 78.0},
          {"name": "CPU: B-tree / row access", "threads": 2.0, "tags": ["cpu", "cpu-btree"], "share_pct": 5.0}]
    summ = dict(OLTP[0], tps=1294.0, log_fsyncs_per_s=37.7, disk_w_await_ms=20.9, cpu_busy=23, threads_running_avg=64,
                threads_connected_avg=65, dbproc_runq_wait_pct=40, dbproc_runq_wait_threads=40)
    neutral = lambda seq, lab: {"seq": seq, "label": lab, "verdict": "neutral", "managed": {"x": str(seq)}, "changes": []}  # noqa: E731
    hist = [{"seq": 1, "label": "baseline (1/3)", "verdict": "reference", "managed": {}, "changes": []},
            neutral(2, "best-practices"), neutral(3, "os-hugepages"), neutral(4, "os-numa")]
    ctx = {"goal": "tps", "policy": "safe", "os_tuning": True, "os_facts": {}, "hw": dict(HW, cpus=32), "flavor": "percona",
           "version": (8, 4, 11), "variables": dict(VARS, log_bin="OFF"), "summary": summ,
           "inputs": dict(OLTP[1], time_breakdown=bd), "history": hist, "noise_pct": 5.0, "managed": {}}
    res = engine.run_engine(ctx)
    it = {i["id"]: i for i in res["items"]}
    assert res["next"] is None and "Storage-bound commits" in (res.get("stop_reason") or ""), (res["next"], res.get("stop_reason"))
    for q in ("thread-pool", "sweep-thread-concurrency"):
        assert q not in it or it[q]["confidence"] == 0, q
    # benchmark policy: a durability trade-off acting on the 78% commit time goes before a safe idea with no evidence
    calm = dict(summ, checkpoint_age_pct_max=10, bp_dirty_pct_avg=5, disk_util=20, bp_hit_pct=99.9, bp_disk_reads=0, bp_wait_free=0)
    b = engine.run_engine(dict(ctx, policy="benchmark", history=hist[:2], summary=calm))
    nx = {i["id"]: i for i in b["items"]}.get(b["next"]) or {}
    assert nx.get("safety") == "trade-off", (b["next"], nx.get("safety"))


def test_unbounded_growth_vs_settling():
    """Run 3178 (session 226): history list grew linearly 0 -> 4.1 M through warm-up and window with 1 purge thread; the
    settling check called it 'settled 6 min into the window' and calibration was repeated with a 1200 s warm-up."""
    from tuning_agent import collector, evaluate
    def mk(f, n=60, t0=600):
        return [{"ts": t0 + 10 * i, "db": {"history_list_length": f(t0 + 10 * i), "checkpoint_age_pct": 30 - 20 * 0.99 ** (t0 + 10 * i)}} for i in range(n)]
    lin = mk(lambda t: 1000.0 * t)                      # +1000/s, never slowing
    tail = mk(lambda t: 1000.0 * t, n=12, t0=480)
    u = collector.unbounded_growth(lin, tail)
    assert u and u[0]["metric"] == "history_list_length" and 900 <= u[0]["rate_per_s"] <= 1100, u
    sig = collector.warmup_signals(lin, 600, tail, {"history_list_length"}) or {}
    assert "history_list_length" not in [x["metric"] for x in sig.get("settling") or []]
    settle = mk(lambda t: 3e6 * (1 - 0.99 ** (t - 400)))  # saturating: settles
    assert not collector.unbounded_growth(settle, mk(lambda t: 3e6 * (1 - 0.99 ** (t - 400)), n=12, t0=480))
    runs = [{"summary": {"steady_state": {"steady": False, "growing": ["history list length 3,338,285.4 -> 3,865,001.7"]},
                         "unbounded_growth": u}} for _ in range(3)]
    wc = evaluate.warmup_check(runs, 600)
    assert wc["adequate"] and wc.get("unbounded") and "Not a warm-up problem" in wc["text"], wc
    v, d = evaluate.verdict({"superseded": "x", "summary": runs[0]["summary"]}, [], "tps", {})
    assert v == "superseded" and d.get("not_steady") and d.get("unbounded"), d


def test_season_check_noise_aware():
    """Seasoning stop: a fixed 0.5%/h bar over one hour (4+ iterations) almost never passed on a flat but noisy workload;
    the check follows the measured noise of a run, needs evidence of flatness, and stops a flattening curve once little
    change is left - but never calls a real ongoing drift steady."""
    import random
    from tuning_agent import evaluate
    series = lambda rnd, f, cv, hours: [(h / 12, f(h / 12) * (1 + rnd.gauss(0, cv))) for h in range(1, int(hours * 12) + 1)]
    def first_stop(pts):
        for i in range(evaluate.SEASON_MIN_POINTS, len(pts) + 1):
            if pts[i - 1][0] >= 1.0 and evaluate.season_check(pts[:i], 2.0, 10 / 60)["stop"]:
                return pts[i - 1][0]
        return None
    def stops(f, cv, seeds=12):
        return [first_stop(series(random.Random(s), f, cv, 12)) for s in range(seeds)]
    flat = stops(lambda t: 1000.0, 0.02)                         # flat, 2% scatter per 5-minute sample
    assert all(h is not None and h <= 7 for h in flat), flat
    sat = stops(lambda t: 1000.0 * (1.3 - 0.3 * 2.718 ** (-t)), 0.01)  # +30% approaching its level, tau 1 h
    assert all(h is not None and 2.5 <= h <= 6 for h in sat), sat
    dec = stops(lambda t: 1000.0 * (1 - 0.03 * t), 0.01)         # aging data: -3%/h, never steady
    assert sum(h is not None for h in dec) <= 1, dec


def test_start_tradeoffs_are_not_the_tuners():
    """Session 226: the standard benchmark runs without a binary log (disable_log_bin=ON from the start), so every run
    carried that trade-off and none counted as production-safe; only trade-offs added beyond the start count."""
    from tuning_agent.knowledge import added_tradeoffs, start_tradeoffs
    runs = [{"seq": 1, "managed": {"disable_log_bin": "ON"}},
            {"seq": 2, "managed": {"disable_log_bin": "ON", "innodb_buffer_pool_size": "8G"}},
            {"seq": 3, "managed": {"disable_log_bin": "ON", "innodb_flush_log_at_trx_commit": "2"}}]
    start = start_tradeoffs(runs)
    assert [t["variable"] for t in start] == ["disable_log_bin"], start
    assert not added_tradeoffs(runs[1], start)
    assert [t["variable"] for t in added_tradeoffs(runs[2], start)] == ["innodb_flush_log_at_trx_commit"]


def test_client_stall_and_steal_on_idle_host():
    """Session 222: after 270k NOPM the HammerDB clients stalled (259 connected, ~15 running, CPU 1%); the run must say
    so instead of blaming steal, and steal on an idle host has no throughput cost."""
    from tuning_agent import evaluate
    s = {"tps": 13.5, "cpu_busy": 1.0, "mysqld_cpu_pct": 0.65, "threads_running_avg": 15.3, "threads_connected_avg": 259, "cpu_steal": 0.0}
    v, d = evaluate.verdict({"seq": 16, "summary": s, "goal_value": 4140}, [], "tps", {"noise_pct": 12})
    assert v == "unreliable" and d.get("client_stalled") and "client stalled" in d["note"], (v, d)
    busy = {"tps": 1000, "cpu_busy": 70, "mysqld_cpu_pct": 60, "threads_running_avg": 50, "threads_connected_avg": 64}
    assert evaluate.client_stall(busy) is None
    idle_steal = evaluate.steal_check({"cpu_busy": 2, "cpu_steal": 3.0, "cpu_steal_max": 3.5}, 2.0)
    assert idle_steal is None or idle_steal["est_impact_pct"] < 0.1, idle_steal


def test_settle_times_for_warmup_probe():
    """warmup-probe: settling times per metric from one long run; saturating curves settle, a linear history list is
    reported as unbounded, not as a warm-up to wait for."""
    import math
    from tuning_agent import collector
    smp = [{"ts": 10 * i, "db": {"tps": 1000 * (1 - math.exp(-10 * i / 300)) + 5 * math.sin(i),
                                 "checkpoint_age_pct": 60 * (1 - math.exp(-10 * i / 900)),
                                 "history_list_length": 2000.0 * 10 * i}} for i in range(1, 361)]
    r = collector.settle_times(smp, 0)
    m = r["metrics"]
    assert m["tps"]["settled_after_s"] is not None and m["tps"]["settled_after_s"] < 1500, m["tps"]
    assert m["checkpoint_age_pct"]["settled_after_s"] > m["tps"]["settled_after_s"], m
    assert m["history_list_length"]["unbounded"], m["history_list_length"]


def test_client_failures_and_low_disk_space():
    """Session 222: the data disk filled and every HammerDB transaction failed with 'table is full' - a failed run, named
    as such; below 10% free space a run carries a warning."""
    from tuning_agent import evaluate
    s = {"tps": 13.5, "cpu_busy": 1.0, "threads_connected_avg": 259, "threads_running_avg": 15, "mysqld_cpu_pct": 0.6,
         "client_errors": [{"number": 1114, "name": "ER_RECORD_FILE_FULL", "count": 9000, "per_s": 10.0}], "client_errors_per_s": 10.0,
         "datadir_free_gb": 0.0, "datadir_free_pct": 0.0}
    v, d = evaluate.verdict({"seq": 16, "summary": s, "goal_value": 4140}, [], "tps", {"noise_pct": 12})
    assert v == "unreliable" and d["client_failures"]["disk_full"] and "disk-full" in d["note"] and d.get("low_free_space"), d
    ok = {"tps": 1000, "client_errors": [{"number": 1213, "name": "ER_LOCK_DEADLOCK", "count": 30, "per_s": 0.05}], "client_errors_per_s": 0.05,
          "datadir_free_pct": 40.0}
    assert evaluate.client_failures(ok) is None and not evaluate.state_flags(ok).get("low_free_space")
    assert evaluate.state_flags({"datadir_free_pct": 7.5, "datadir_free_gb": 30}).get("low_free_space")


def test_measure_length_from_settled_samples():
    """Run-length probe: plain sample noise needs a short window; a slow flush wave needs several of its periods."""
    import math, random
    from tuning_agent import collector
    rnd = random.Random(3)
    white = [{"ts": 10 * i, "db": {"tps": 1000 * (1 + rnd.gauss(0, 0.02))}} for i in range(120)]
    wave = [{"ts": 10 * i, "db": {"tps": 1000 * (1 + 0.05 * math.sin(2 * math.pi * 10 * i / 240) + rnd.gauss(0, 0.01))}} for i in range(120)]
    a, b = collector.measure_length(white), collector.measure_length(wave)
    assert a["reaches_target"] and a["recommended_s"] <= 300, a
    assert b["recommended_s"] >= 600 and b["recommended_s"] > a["recommended_s"], b
    assert collector.measure_length(white[:20]) is None  # too little settled data


def test_neutral_explained_promising_and_latency_gain():
    """Session 294: os-autogroup +0.67% TPS at calibration CV 0.21% (2% floor until a re-check), avg statement latency
    -24% and p95 -37% - not kept for the goal, but explained, marked promising, and the latency gain reported."""
    from tuning_agent import evaluate
    mk = lambda seq, label, g, lat, p95, managed, changes=(): {"seq": seq, "label": label, "goal_value": g, "verdict": "reference" if seq == 1 else "repeat",
        "managed": managed, "changes": list(changes), "summary": {"tps": g, "avg_latency_ms": lat, "p95_latency_ms": p95, "mysqld_cpu_ms_per_trx": 1.2}}
    base = {"innodb_buffer_pool_size": "8G"}
    hist = [mk(1, "baseline (1/3)", 19685, 0.1815, 0.549, base), mk(2, "baseline (2/3)", 19703, 0.1812, 0.549, base),
            mk(3, "baseline (3/3)", 19624, 0.1822, 0.549, base)]
    run = mk(4, "os-autogroup", 19802, 0.1376, 0.347, dict(base, **{"os:kernel.sched_autogroup_enabled": "0"}),
             [{"variable": "os:kernel.sched_autogroup_enabled", "new": "0", "source": "rec:os-autogroup"}])
    noise = {"noise_pct": 2.0, "pooled_cv_pct": 0.21, "dof": 2, "long_term_dof": 0, "runs": [1, 2, 3]}
    v, d = evaluate.verdict(run, hist, "tps", noise)
    assert v == "neutral" and "within the ±2.0% noise threshold" in d["note"] and d.get("promising"), d
    assert d["secondary_gain"]["avg_latency_ms"]["faster_pct"] > 20 and "faster" in d["note"], d
    v2, d2 = evaluate.verdict(run, hist, "tps", dict(noise, long_term_dof=3, noise_pct=0.53))
    assert v2 == "improved" and not d2.get("promising"), (v2, d2)

def test_steal_steady_level_not_flagged():
    """Session 294: steal held at 0.3-0.46% on every run (CPU ~98% busy) and every run was flagged; a steady level
    lowers every result alike - only steal that differs from the session's usual level can bias a comparison."""
    from tuning_agent import evaluate
    hist = [{"seq": i, "summary": {"cpu_steal": x, "cpu_steal_max": 0.6, "cpu_busy": 98}} for i, x in enumerate((0.41, 0.41, 0.44, 0.42), 1)]
    s = {"cpu_steal": 0.35, "cpu_steal_max": 0.55, "cpu_busy": 98}
    assert evaluate.steal_check(s, 0.72, evaluate.usual_steal(hist)) is None
    assert evaluate.steal_check(s, 0.72)["level"] == "flag"  # against zero, as before three runs exist
    jump = evaluate.steal_check(dict(s, cpu_steal=1.5), 0.72, evaluate.usual_steal(hist))
    assert jump and jump["level"] == "invalid" and jump["usual_pct"] == 0.42, jump


def test_benchmark_startup_not_warmup():
    """Session 299: warm-up and measurement are two sysbench invocations; the measuring one reconnects 1024 threads
    and the first 10 s sample of every window was 30% low - flagged as a warm-up still settling. That start-up is left
    out of the sample analyses; a single invocation that started before the window keeps every sample."""
    from tuning_agent import collector
    tps = [11774, 16845, 16900, 17118, 17077, 16707, 17220, 17143] + [17050] * 22
    smp = [{"ts": 1006 + 10 * i, "db": {"tps": x}} for i, x in enumerate(tps)]
    assert collector.startup_samples(smp, 999, 300) == 1
    assert collector.startup_samples(smp, 700, 300) == 0 and collector.startup_samples(smp, None, 300) == 0
    slow = [{"ts": 1006 + 10 * i, "db": {"tps": 5000 + 400 * i}} for i in range(30)]  # a real warm-up: not cut
    assert collector.startup_samples(slow, 999, 300) <= 3



def test_basis_shows_rule_vs_learned():
    """Every recommendation says what it rests on: the rule that generated it, built-in parts (mechanism, shipped
    impact) and learned parts (experience across accounts, learned impact, insights) with the score each added."""
    from tuning_agent import engine
    it = {"id": "spin-loops", "relevance": 1, "addresses": ["cpu-spin"], "param_impact": {"impact": "high", "source": "learned"},
          "experience": {"n": 4, "wins": 0, "losses": 1}, "known_neutral": {"n": 4, "losses": 1},
          "insights": [{"id": 7, "status": "repeated", "direction": "helps", "statement": "x", "weight": 0.6}]}
    b = engine.basis(it, -3.0)
    assert b["origin"] == "rule" and b["rule"] == "spin-loops"
    assert [p["score"] for p in b["built_in"]] == [2]
    assert {p.get("insight") for p in b["learned"]} >= {7} and any("known neutral" in p["what"] for p in b["learned"])
    assert engine.basis({"id": "x", "source": "llm"})["origin"] == "llm"



def test_start_is_the_baseline_not_server_defaults():
    """210/212/224/282/286: the start's trade-offs came from the server-defaults run, so the baseline's own setup became
    an added trade-off and best safe fell back to server defaults."""
    from tuning_agent import knowledge
    runs = [{"seq": 1, "label": "server defaults", "managed": {}},
            {"seq": 2, "label": "baseline (1/3)", "managed": {"innodb_flush_log_at_trx_commit": "2"}},
            {"seq": 3, "label": "ps-off", "managed": {"innodb_flush_log_at_trx_commit": "2", "performance_schema": "OFF"}}]
    st = knowledge.start_tradeoffs(runs)
    assert any(x["variable"] == "innodb_flush_log_at_trx_commit" for x in st), st
    assert knowledge.added_tradeoffs(runs[1], st) == []


def test_drop_unearned_keeps_big_gains():
    from tuning_agent import engine
    assert engine._big_gain({"verdict": "improved", "evaluation": {"goal_delta_pct": 24.6, "goal_noise_pct": 2.0}})
    assert not engine._big_gain({"verdict": "improved", "evaluation": {"goal_delta_pct": 2.5, "goal_noise_pct": 2.0}})


def test_parent_hint_overrides_netted_changes():
    """'best safe + X' nets out to rollbacks only: judged against the hinted best safe runs, not the trade-off config."""
    from tuning_agent import evaluate
    hist = [{"seq": 1, "label": "baseline (1/3)", "managed": {"a": "1"}, "goal_value": 100.0, "verdict": "reference"},
            {"seq": 2, "label": "x", "managed": {"a": "1", "b": "2"}, "goal_value": 200.0, "verdict": "improved"}]
    run = {"seq": 3, "label": "best safe + spin-loops (1/2)", "managed": {"a": "1", "c": "3"}, "goal_value": 101.0,
           "changes": [{"variable": "b", "old": "2", "new": None, "source": "rollback"}], "parent_hint": [1]}
    assert evaluate._parent_key(run, hist, evaluate.config_key(run["managed"])) == evaluate.config_key({"a": "1"})



def test_stability_first():
    """Unbounded undo history (high-concurrency update-only): the best configuration is a stable one when one exists,
    and the change that ended the growth is kept as stabilized even though it cost throughput."""
    from tuning_agent import engine, evaluate
    unb = {"unbounded_growth": [{"metric": "history_list_length", "start": 600000, "end": 1250000, "per_hour": 1e6}]}
    ok = {"unbounded_growth": None}
    hist = [{"seq": 1, "label": "baseline (1/3)", "managed": {"a": "1"}, "goal_value": 100.0, "verdict": "reference", "summary": unb},
            {"seq": 2, "label": "baseline (2/3)", "managed": {"a": "1"}, "goal_value": 100.0, "verdict": "repeat", "summary": unb}]
    run = {"seq": 3, "label": "purge-lag", "managed": {"a": "1", "innodb_max_purge_lag": "600000"}, "goal_value": 90.0, "summary": ok,
           "changes": [{"variable": "innodb_max_purge_lag", "old": "0", "new": "600000", "source": "rec:purge-lag"}]}
    v, d = evaluate.verdict(run, hist, "tps", {"noise_pct": 1.0, "runs": [1, 2]})
    assert d.get("stabilized"), (v, d)
    b = engine.best_config(hist + [dict(run, verdict=v, evaluation=d)], "tps", 1.0)
    assert b["managed"].get("innodb_max_purge_lag") == "600000", b
    engine.STABILITY_FIRST = False
    try:
        assert engine.best_config(hist + [dict(run, verdict=v, evaluation=d)], "tps", 1.0)["managed"] == {"a": "1"}
    finally:
        engine.STABILITY_FIRST = True



def test_stability_remedies_rank_first():
    """Unbounded undo history on busy CPUs: fewer user threads creating undo ranks first (stability first), purge batch
    size and the purge-thread step are stability candidates, the purge lag bound only once they are tried (user: a
    stopgap that severely limits throughput), pinning purge threads is advice; with stability off they rank as usual."""
    s = dict(OLTP[0], history_list_length_max=1250000, cpu_busy=95, threads_running_avg=200,
             unbounded_growth=[{"metric": "history_list_length", "label": "history list", "start": 600000, "end": 1250000, "per_hour": 1000000}])
    v = dict(VARS, innodb_purge_threads="4", innodb_max_purge_lag="0", innodb_max_purge_lag_delay="0", innodb_purge_batch_size="300",
             innodb_thread_concurrency="0")
    ctx = {"goal": "tps", "policy": "benchmark", "os_tuning": True, "os_facts": {}, "hw": dict(HW, cpus=16), "flavor": "percona",
           "version": (8, 4, 11), "variables": v, "summary": s, "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    res = engine.run_engine(ctx)
    it = ids(res)
    assert not it["purge-lag"].get("stability"), it["purge-lag"]  # last resort: other remedies are left
    actionable = [i for i in res["items"] if i["kind"] in ("config", "revert") and i["confidence"] > 0]
    assert actionable[0]["id"] == "purge-concurrency" and actionable[0].get("stability"), [i["id"] for i in actionable[:4]]
    assert actionable[0]["changes"][0]["variable"] == "innodb_thread_concurrency"  # 200 threads / 16 vCPUs: not blocked
    # Percona Server with its thread pool available: the pool before the cruder concurrency cap (user), even at 1024
    # connections where the plain thread-pool experiment had lost
    v_tp = dict(v, thread_handling="one-thread-per-connection", thread_pool_size="16")
    act = [i for i in engine.run_engine(dict(ctx, variables=v_tp, summary=dict(s, threads_connected_avg=1024)))["items"]
           if i["kind"] in ("config", "revert") and i["confidence"] > 0]
    assert act[0]["id"] == "purge-threadpool" and act[0].get("stability"), [i["id"] for i in act[:4]]
    assert {c["variable"]: c["proposed"] for c in act[0]["changes"]}.get("thread_handling") == "pool-of-threads", act[0]["changes"]
    assert "thread-pool" not in [i["id"] for i in act]
    # every other remedy tried: now the purge lag bound
    tried = [{"seq": i, "verdict": "neutral", "changes": [{"variable": "x", "new": "1", "source": f"rec:{rid}"}]}
             for i, rid in enumerate(("purge-concurrency", "sweep-purge-threads", "purge-batch"), 5)]
    it_l = ids(engine.run_engine(dict(ctx, history=tried)))
    assert it_l["purge-lag"].get("stability") and it_l["purge-lag"]["score"] > 100, it_l["purge-lag"]
    assert it.get("purge-priority", {}).get("kind") == "advice"  # no resource groups in this server's variables: advice
    # resource groups and OS tuning: a higher purge priority (with CAP_SYS_NICE for mysqld) ranks before the lag bound
    v_rg = dict(v, **{"rg:purge": "off", "os:mysqld_cap_sys_nice": "OFF"})
    res_rg = engine.run_engine(dict(ctx, variables=v_rg))
    first = [i for i in res_rg["items"] if i["kind"] in ("config", "revert") and i["confidence"] > 0][0]
    assert first["id"] == "purge-priority" and first.get("stability"), first["id"]
    assert {c["variable"]: c["proposed"] for c in first["changes"]} == {"rg:purge": "prio=-10", "os:mysqld_cap_sys_nice": "ON"}
    engine.STABILITY_FIRST = False
    try:
        it2 = ids(engine.run_engine(dict(ctx, variables=dict(v))))
        assert not it2["purge-lag"].get("stability")
    finally:
        engine.STABILITY_FIRST = True


def test_log_writer_threads_off_without_fsync_at_any_concurrency():
    """h2h W1: 250 running threads on 12 CPUs at trx_commit=2 - the old gate (<= 8 x CPUs) kept it out of the rules arm."""
    s = dict(OLTP[0], cpu_busy=97, threads_running_avg=250)
    v = dict(VARS, innodb_flush_log_at_trx_commit="2")
    ctx = {"goal": "tps", "policy": "benchmark", "os_tuning": True, "os_facts": {}, "hw": dict(HW, cpus=12), "flavor": "percona",
           "version": (8, 4, 11), "variables": v, "summary": s, "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    it = ids(engine.run_engine(ctx))
    assert "log-writer-threads-off" in it and it["log-writer-threads-off"]["impact"] == 3, it.get("log-writer-threads-off")
    ctx1 = dict(ctx, variables=dict(VARS))  # fsync per commit, 250 threads: not proposed
    assert "log-writer-threads-off" not in ids(engine.run_engine(ctx1))


def test_commit_path_change_retests_log_writer_threads():
    item = {"id": "log-writer-threads-off", "kind": "config", "confidence": 2,
            "changes": [{"variable": "innodb_log_writer_threads", "current": "ON", "proposed": "OFF"}]}
    hist = [{"seq": 5, "verdict": "regressed", "managed": {"innodb_flush_log_at_trx_commit": "1"},
             "changes": [{"variable": "innodb_log_writer_threads", "new": "OFF", "source": "rec:log-writer-threads-off"}]}]
    out = engine.apply_history([dict(item)], hist, None, {"innodb_flush_log_at_trx_commit": "2"})
    assert out[0].get("retest") and out[0]["confidence"] == 2, out[0]
    same = engine.apply_history([dict(item)], hist, None, {"innodb_flush_log_at_trx_commit": "1"})
    assert same[0]["confidence"] == 0


def test_client_limited_gain_is_kept():
    """273 / 275: gains under a saturated client were thrown away; a gain beyond noise is a lower bound and kept."""
    hist = [{"seq": 1, "label": "baseline (1/3)", "managed": {"a": "1"}, "goal_value": 100.0, "verdict": "reference", "summary": {}},
            {"seq": 2, "label": "baseline (2/3)", "managed": {"a": "1"}, "goal_value": 100.0, "verdict": "repeat", "summary": {}}]
    sat = {"client_cpu_busy": 97}
    up = {"seq": 3, "label": "thread-pool", "managed": {"a": "1", "b": "2"}, "goal_value": 118.0, "summary": sat,
          "changes": [{"variable": "b", "old": None, "new": "2", "source": "rec:thread-pool"}]}
    v, d = evaluate.verdict(up, hist, "tps", {"noise_pct": 2.0, "runs": [1, 2]})
    assert v == "improved" and d.get("client_limited"), (v, d)
    v2, _ = evaluate.verdict(dict(up, goal_value=97.0), hist, "tps", {"noise_pct": 2.0, "runs": [1, 2]})
    assert v2 == "unreliable"


def test_fruitless_streak_since_last_gain():
    from tuning_agent import cli
    runs = [{"seq": 1, "label": "baseline (1/3)", "verdict": "reference"}, {"seq": 2, "label": "x", "verdict": "improved"},
            {"seq": 3, "label": "x (confirm)", "verdict": "repeat"}] + \
           [{"seq": 4 + i, "label": f"idea{i}", "verdict": "neutral"} for i in range(5)] + [{"seq": 9, "label": "re-check (run 2 config)", "verdict": "repeat"}]
    f = cli._fruitless_since_gain({"runs": runs})
    assert [r["seq"] for r in f] == [4, 5, 6, 7, 8], f


def test_learning_state_counts_loaded_learnings():
    st = engine.learning_state({"experience": {"a": {"n": 4}, "b": {"n": 2}}, "hypothesis_experience": [1, 2],
                                "param_impact": {"x": {"impact": "high"}, "y": {}}, "insights": [{}]})
    assert st == {"experience_ideas": 2, "experience_runs": 6, "hypothesis_outcomes": 2, "learned_parameters": 1, "insights": 1}, st
    assert engine.learning_state({}) == {"experience_ideas": 0, "experience_runs": 0, "hypothesis_outcomes": 0, "learned_parameters": 0, "insights": 0}


class _FakeCtx:
    def __init__(self, sess=None, cfg=None, osv=None):
        self._sess, self.cfg, self._osv = sess or {}, cfg or {}, osv if osv is not None else {"os:swapfile": "0"}

    def session(self, required=False):
        return self._sess

    def os_tuning_allowed(self):
        return str(self.cfg.get("allow_os_tuning", "true")) == "true"

    def os_settings(self):
        return self._osv


def test_baseline_adds_swap_unless_opted_out():
    from tuning_agent import cli
    hw = {"ram_bytes": 32 * GiB, "swap_bytes": 0, "disk_free_bytes": 500 * GiB}
    sw = cli._baseline_swap(_FakeCtx({"os_tuning": True}), hw)
    assert sw and sw["variable"] == "os:swapfile" and parse_size(sw["value"]) == 32 * GiB, sw
    assert cli._baseline_swap(_FakeCtx({"os_tuning": True}, {"swap": "none"}), hw) is None  # host without swap by design
    assert cli._baseline_swap(_FakeCtx({"os_tuning": False}), hw) is None  # no OS tuning
    assert cli._baseline_swap(_FakeCtx({"os_tuning": True}), dict(hw, swap_bytes=31 * GiB)) is None  # already about 1x RAM
    small = cli._baseline_swap(_FakeCtx({"os_tuning": True}), dict(hw, disk_free_bytes=20 * GiB))
    assert small and parse_size(small["value"]) <= 9 * GiB, small  # at most half the free disk


def test_rollback_fails_loudly_and_starts_mysqld():
    """Deploy canary #308: mysqld down, rollback --reset could not connect, printed 'Rolled back' and exited 0."""
    import argparse
    from tuning_agent import cli
    calls = {"connect": 0, "systemctl": []}

    class DB:
        def connect(self, retries=1, delay=0):
            calls["connect"] += 1
            if calls["connect"] == 1:
                raise OSError("Can't connect to MySQL server")

    class State:
        def get(self, k, d=None):
            return {"managed": {"innodb_flush_log_at_trx_commit": {"value": "2"}}}.get(k, d)

    class C:
        state, db, cfg = State(), DB(), {}

        def session(self, required=False):
            return None

    class Ap:
        def __init__(self, *a, **k):
            self.service = "mysql"
    saved = (cli.Ctx, cli._apply_and_record, cli.Applier, cli.subprocess.run, cli.emit)
    cli.Ctx, cli.Applier = (lambda: C()), Ap
    cli.subprocess.run = lambda args, **k: calls["systemctl"].append(args)
    cli.emit = lambda *a, **k: None
    try:
        cli._apply_and_record = lambda c, ch, src: {"errors": ["OperationalError: 2003"], "rolled_back": True}
        try:
            cli.cmd_rollback(argparse.Namespace(reset=True))
            raise AssertionError("a failed rollback must not succeed")
        except cli.Fail as e:
            assert "rollback" in str(e)
        assert calls["systemctl"] and calls["systemctl"][0][:2] == ["systemctl", "start"], calls
        calls["connect"] = 1
        cli._apply_and_record = lambda c, ch, src: {"errors": [], "applied": []}
        cli.cmd_rollback(argparse.Namespace(reset=True))  # works once mysqld answers
    finally:
        cli.Ctx, cli._apply_and_record, cli.Applier, cli.subprocess.run, cli.emit = saved


def test_memory_sweep_values_checked_against_ram_and_swap():
    """r2 RO-4.ns: only the first sweep value was checked; 12G on a 15.6 GiB host without swap OOM-killed mysqld.
    Every value must fit in RAM + free swap: none fit -> not run; some fit -> only those; all fit (with swap) -> all."""
    s = dict(OLTP[0], bp_hit_pct=90, bp_disk_reads=5000, cpu_iowait=30, cpu_busy=40, mem_available_mb=3000, swap_used_mb=0, disk_util=90)

    def sweep(swap_mb):
        ctx = {"goal": "tps", "policy": "benchmark", "os_tuning": False, "os_facts": {}, "hw": HW, "flavor": "percona", "version": (8, 4, 11),
               "variables": dict(VARS), "summary": dict(s, swap_total_mb=swap_mb), "inputs": dict(OLTP[1], data_bytes=20 * GiB), "history": [],
               "noise_pct": 2.0, "managed": {}}
        return ids(engine.run_engine(ctx))["sweep-bp"]
    none_ = sweep(0)
    assert none_["confidence"] == 0 and "OOM" in none_["note"], none_
    some = sweep(1024)
    assert not some.get("sweep") and [c["proposed"] for c in some["changes"]] == ["2560M"] and "Left out: 3G" in some["rationale"], some
    assert sweep(8192)["sweep"]["values"] == ["2560M", "3G"]



def test_resource_groups_purge_and_background():
    """rg:purge puts the purge threads in their own SYSTEM group (priority / CPUs), the other InnoDB background threads stay
    in ta_bg; assignments are re-applied by ensure() (they do not survive a restart)."""
    from tuning_agent import resgroups as rg
    assert rg.parse_purge("cpus=6-7,prio=-5") == {"cpus": "6-7", "prio": -5} and rg.parse_purge("prio=-30") == {"prio": -20}
    assert rg.format_purge(None, -10) == "prio=-10" and rg.format_purge() == "off"

    class DB:
        def __init__(self):
            self.groups, self.sql = {}, []
            self.threads = {1: "thread/innodb/srv_purge_thread", 2: "thread/innodb/srv_worker_thread", 3: "thread/innodb/page_flush_coordinator_thread"}
            self.member = {}

        def execute(self, q):
            self.sql.append(q)
            if q.startswith("CREATE RESOURCE GROUP"):
                self.groups[q.split()[3]] = {"v": b"0-7", "p": 0, "e": 1}
            elif q.startswith("SET RESOURCE GROUP"):
                g = q.split()[3]
                for i in q.split(" FOR ")[1].split(","):
                    self.member[int(i)] = g
            elif q.startswith("DROP RESOURCE GROUP"):
                g = q.split()[3]
                self.groups.pop(g, None)
                self.member = {k: v for k, v in self.member.items() if v != g}

        def dict_rows(self, q):
            if "RESOURCE_GROUPS" in q:
                g = q.split("RESOURCE_GROUP_NAME='")[1].split("'")[0]
                return [self.groups[g]] if g in self.groups else []
            if "TYPE='BACKGROUND'" in q:
                return [{"THREAD_ID": i} for i in self.threads]
            if "NAME IN" in q:
                return [{"THREAD_ID": i} for i, n in self.threads.items() if n.endswith(("srv_purge_thread", "srv_worker_thread"))]
            if "WHERE RESOURCE_GROUP=" in q:
                g = q.split("RESOURCE_GROUP='")[1].split("'")[0]
                return [{"THREAD_ID": i} for i, x in self.member.items() if x == g]
            return []
    db = DB()
    rg.apply(db, "6-7", rg.VAR)
    assert db.member == {1: "ta_bg", 2: "ta_bg", 3: "ta_bg"}
    rg.apply(db, "prio=-10", rg.PURGE_VAR)
    assert any("THREAD_PRIORITY = -10" in q for q in db.sql)
    assert db.member[1] == db.member[2] == "ta_purge" and db.member[3] == "ta_bg", db.member
    db.member = {}  # mysqld restarted: assignments are gone
    assert rg.ensure(db) == 3 and db.member == {1: "ta_purge", 2: "ta_purge", 3: "ta_bg"}
    rg.apply(db, "off", rg.PURGE_VAR)
    assert db.member == {1: "ta_bg", 2: "ta_bg", 3: "ta_bg"}, db.member



def test_seasoning_stops_on_a_failing_benchmark():
    """User report (POP2): seasoning retried a failing benchmark for an hour with zero throughput and never said so; it
    also ignored the benchmark's prerequisites. Now: prerequisites first, and three quick / failed exits in a row stop it
    with the benchmark's own error output."""
    from tuning_agent import cli
    calls = {"req": 0, "events": []}

    class R:
        def __init__(self, cmd, log):
            self.t0 = __import__("time").time()
            self.p = type("P", (), {"returncode": 1})()

        def alive(self):
            return False

        def output(self):
            return "FATAL: Can't create more than max_prepared_stmt_count statements"

        def kill(self):
            pass

    class Prof:
        season_hours, season_min_hours, warmup, duration, reset, requires = 1, 0, "10", 10, None, {"max_prepared_stmt_count": "100000"}

        def can_resize(self):
            return False

        def command(self, total_s=None):
            return "sysbench run"

    class C:
        class state:
            dir = "/tmp"

        class db:
            @staticmethod
            def status():
                return {"Com_commit": 0}

        def event(self, *a, **k):
            calls["events"].append(a[1])

        def activity(self, *a, **k):
            pass

        def session(self):
            return {}

        def datadir(self):
            return "/tmp"
    saved = (cli.bench.Running, cli._ensure_requirements)
    cli.bench.Running = R
    cli._ensure_requirements = lambda c, prof: calls.__setitem__("req", calls["req"] + 1) or []
    try:
        try:
            cli._season(C(), Prof())
            raise AssertionError("a benchmark that cannot run must stop seasoning")
        except cli.Fail as e:
            assert "exited 3 times" in str(e) and "max_prepared_stmt_count" in str(e), e
        assert calls["req"] == 1 and calls["events"] and "seasoning stopped" in calls["events"][-1]
    finally:
        cli.bench.Running, cli._ensure_requirements = saved



def test_baseline_redo_in_whole_mib():
    """r2 RO-4: redo sized at 5% of the free disk (993309900 bytes) was rounded by the server -> config mismatch."""
    from tuning_agent import baseline
    hw = {"ram_bytes": 16 * GiB, "cpus": 8, "disk_rotational": False, "disk_free_bytes": 19866198000}
    items, _ = baseline.generate(hw, dict(VARS), "percona", "8.4.11")
    redo = next(parse_size(i["value"]) for i in items if i["variable"] == "innodb_redo_log_capacity")
    assert redo % (1024 * 1024) == 0, redo



def test_lost_mysqld_during_a_run_is_a_failed_configuration():
    """r2 RO-4.ns: an OOM kill during the warm-up surfaced as a pymysql read timeout at the measurement start and ended the
    session; it must come back as BenchFailed (OOM) / ServerDown so autotune reverts the change and continues."""
    import pymysql
    from tuning_agent import cli

    class St:
        data = {}

        def get(self, k, d=None):
            return self.data.get(k, d)

        def save(self):
            pass

    class C:
        state = St()

        class db:
            @staticmethod
            def connect(retries=1, delay=0):
                return None

    def boom(*a, **k):
        raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query (The read operation timed out)")
    saved = (cli._bench_run_once, cli._oom_killed_since)
    cli._bench_run_once = boom
    try:
        cli._oom_killed_since = lambda t0: "Oct 24 kernel: Out of memory: Killed process 63351 (mysqld)"
        try:
            cli._bench_run(C(), None, "sweep-bp")
            raise AssertionError("must raise")
        except cli.BenchFailed as e:
            assert "OOM-killed" in str(e)
        cli._oom_killed_since = lambda t0: None
        try:
            cli._bench_run(C(), None, "x")
            raise AssertionError("must raise")
        except cli.ServerDown as e:
            assert "lost the connection" in str(e)
    finally:
        cli._bench_run_once, cli._oom_killed_since = saved



def test_preflush_reports_progress():
    """User session 311: a restart of a large dirty buffer pool showed as 'measuring' going stale for 15+ min. The
    pre-restart flush reports what it does, how much is left and how long it may take."""
    from tuning_agent import applier
    seen = []

    class DB:
        dirty = [65536 * 8, 65536 * 4, 5]
        sql = []

        def scalar(self, q):
            if "pages_dirty" in q:
                return self.dirty.pop(0) if len(self.dirty) > 1 else self.dirty[0]
            return {"SELECT @@innodb_max_dirty_pages_pct": 90, "SELECT @@innodb_max_dirty_pages_pct_lwm": 10,
                    "SELECT @@innodb_page_size": 16384}.get(q, 655360)

        def execute(self, q):
            self.sql.append(q)
    ap = applier.Applier({"cnf_path": "/tmp/x.cnf", "service_name": "mysql"}, None, DB(), log=lambda *a: None,
                         activity=lambda phase, detail=None, seconds=None: seen.append((phase, detail, seconds)))
    saved = applier.time.sleep
    applier.time.sleep = lambda s: None
    try:
        ap.preflush()
    finally:
        applier.time.sleep = saved
    assert seen and seen[0][0] == "stopping mysqld" and "8.0 GB" in seen[0][1] and seen[0][2], seen
    assert any("SET GLOBAL innodb_max_dirty_pages_pct = 90" in q for q in ap.db.sql)  # restored afterwards



def test_backtest_rule_fixes():
    """Backtest 2026-10-24: limiters blocked at 16+ connections per vCPU (rules, AI, planner); AHI only with B-tree time in
    the CPU profile; the CPU governor only on bare metal; stability remedies first for two tries only; trx-commit-2 first
    on commit-bound loads under the benchmark policy (still a trade-off, never under safe)."""
    from tuning_agent import planner
    ctx = {"summary": {"threads_connected_avg": 1024}, "hw": {"cpus": 16}}
    it = {"changes": [{"variable": "innodb_thread_concurrency", "proposed": "24"}]}
    assert engine.oversubscription_block(it, ctx) and not engine.oversubscription_block(it, dict(ctx, summary={"threads_connected_avg": 64}))
    assert not engine.oversubscription_block({"changes": [{"variable": "innodb_thread_concurrency", "proposed": "0"}]}, ctx)
    assert engine.is_vm({"vendor": "QEMU"}) and engine.is_vm({"vendor": "Linode"}) and not engine.is_vm({"vendor": "Supermicro"})
    # AHI: hot in-memory reads without / with B-tree time
    s = dict(OLTP[0], bp_hit_pct=99.5)
    inp = dict(OLTP[1], status_rates={"Com_select": 5000, "Com_update": 10})
    assert "ahi-on" in ids(run(s, dict(inp, cpu_profile={"categories": {"B-tree / row access": 13}}), "safe"))  # no cut-off
    assert "ahi-on" not in ids(run(s, dict(inp, cpu_profile={"categories": {"spin / mutex wait": 40}}), "safe"))  # no B-tree time
    assert "ahi-on" in ids(run(s, inp, "safe"))  # no profile: CPU busy (95%) stands in
    # stability: priority for two tries, then by score
    unb = {"unbounded_growth": [{"metric": "history_list_length", "label": "h", "start": 600000, "end": 1200000, "per_hour": 1e6}]}
    v = dict(VARS, innodb_purge_threads="4", innodb_max_purge_lag="0", innodb_max_purge_lag_delay="0", innodb_purge_batch_size="300")
    base = {"goal": "tps", "policy": "benchmark", "os_tuning": True, "os_facts": {}, "hw": dict(HW, cpus=16), "flavor": "percona",
            "version": (8, 4, 11), "variables": v, "summary": dict(OLTP[0], cpu_busy=95, history_list_length_max=1200000, **unb),
            "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    assert any(i.get("stability") for i in engine.run_engine(base)["items"])
    tried = [{"seq": i, "verdict": "neutral", "changes": [{"variable": "x", "new": "1", "source": f"rec:{rid}"}]}
             for i, rid in ((5, "sweep-purge-threads"), (6, "purge-batch"))]
    left = [i["id"] for i in engine.run_engine(dict(base, history=tried))["items"] if i.get("stability")]
    assert left in ([], ["purge-lag"]), left  # two tries used; the lag bound alone may follow as the last resort
    # the planner is told what its checks refused
    ctx2 = {"summary": {}, "variables": {}, "history": []}
    assert planner  # packet field is added by cli._plan from sess["planner_rejected"]



def test_requirements_survive_restarts():
    """User report: the run-length probe restarted mysqld and started a 1024-thread sysbench with max_connections back at
    1000. The probe applies the prerequisites before the benchmark connects, and every agent restart re-applies them."""
    from tuning_agent import applier, cli
    log = []

    class DB:
        vals = {"max_connections": "1000", "max_prepared_stmt_count": "16382"}

        def scalar(self, q):
            return self.vals[q.split(".")[-1]]

        def execute(self, q, args=None):
            log.append((q, args))
            self.vals[q.split()[2]] = str(args[0])

    class St:
        data = {"requirements": {"max_connections": "1100", "max_prepared_stmt_count": "1048576"}, "managed": {}}

        def get(self, k, d=None):
            return self.data.get(k, d)

        def __getitem__(self, k):
            return self.data[k]
    ap = applier.Applier({"cnf_path": "/tmp/x.cnf", "service_name": "mysql"}, St(), DB(), log=lambda *a: None)
    ap.reapply_runtime()
    assert ap.db.vals == {"max_connections": "1100", "max_prepared_stmt_count": "1048576"}, ap.db.vals
    # the probe calls _ensure_requirements before starting the benchmark
    import inspect
    src = inspect.getsource(cli._run_length_probe)
    assert src.index("_ensure_requirements(c, prof)") < src.index("bench.Running(")




def test_purge_unbounded_threshold_and_blocked_purge():
    """User: a history list past 1M and still growing is unbounded; below that it is still settling. A transaction open
    for minutes holds purge back: no purge setting can help, so none is tried and the advice names the cause."""
    from tuning_agent import engine
    g = lambda end: {"unbounded_growth": [{"metric": "history_list_length", "start": 100000, "end": end, "per_hour": 1e6}]}  # noqa: E731
    assert engine.unbounded_hll(g(900000)) is None and engine.unbounded_hll(g(1100000))
    assert engine.purge_blocked({"oldest_trx_s_max": 20}) is None and engine.purge_blocked({"oldest_trx_s_max": 900}) == 900
    s = dict(OLTP[0], history_list_length_max=3000000, cpu_busy=95, threads_running_avg=200, oldest_trx_s_max=1800,
             **g(3000000))
    v = dict(VARS, innodb_purge_threads="4", innodb_max_purge_lag="0", innodb_max_purge_lag_delay="0", innodb_purge_batch_size="300",
             innodb_thread_concurrency="0")
    ctx = {"goal": "tps", "policy": "benchmark", "os_tuning": True, "os_facts": {}, "hw": dict(HW, cpus=16), "flavor": "percona",
           "version": (8, 4, 11), "variables": v, "summary": s, "inputs": OLTP[1], "history": [], "noise_pct": 2.0, "managed": {}}
    it = ids(engine.run_engine(ctx))
    assert it["purge-blocked"]["kind"] == "advice"
    assert not any(k in it for k in ("purge-lag", "purge-batch", "purge-concurrency"))
    assert not it.get("sweep-purge-threads", {}).get("stability")


def test_purge_triage_keeps_substantial_cuts_and_tries_the_lag_bound_last():
    """Online purge remedies, 5 min each while the benchmark runs: a setting is kept only when it at least halves the
    history growth at an acceptable throughput cost; stable once growth is down to a quarter (or shrinking); the purge
    lag bound only when nothing else got there."""
    from tuning_agent import cli
    applied, events = [], []

    class C:
        state = type("S", (), {"d": {"managed": {}}, "get": lambda self, k, d=None: self.d.get(k, d),
                                "set": lambda self, k, v: self.d.__setitem__(k, v), "__getitem__": lambda self, k: self.d[k]})()
        activity = staticmethod(lambda *a: None)
        event = staticmethod(lambda *a: events.append(a))

    class R:
        alive = staticmethod(lambda: True)

    saved = {k: getattr(cli, k) for k in ("TRIAGE_SEG_S", "_apply_and_record", "_triage_candidates", "_segment")}
    saved_sleep = cli.time.sleep
    cli.time.sleep = lambda s: None
    cli.TRIAGE_SEG_S = 0
    cli._apply_and_record = lambda c, ch, src, restart=None: applied.append([(x["variable"], x["value"]) for x in ch])

    def run(cands, segs, last=()):
        applied.clear()
        it = iter(segs)
        cli._triage_candidates = lambda c: (cands, list(last))
        cli._segment = lambda c, a, b: dict(next(it))
        return cli._purge_triage(C(), R(), cli.time.time() + 3600)

    try:
        # batch 1000 cuts growth by 60% (kept), thread concurrency cuts the rest below a quarter: stable, lag never tried
        out = run([("batch 1000", [("innodb_purge_batch_size", "1000")]), ("prio", [("rg:purge", "prio=-10")]),
                   ("conc", [("innodb_thread_concurrency", "64")])],
                  [{"hll_per_min": 10000, "tps": 1000, "hll_end": 2e6}, {"hll_per_min": 4000, "tps": 990, "hll_end": 2.1e6},
                   {"hll_per_min": 3500, "tps": 980, "hll_end": 2.2e6}, {"hll_per_min": 1500, "tps": 900, "hll_end": 2.2e6}],
                  last=[("lag", [("innodb_max_purge_lag", "__HLL__")])])
        assert out["stable"] and out["kept"] == [("innodb_purge_batch_size", "1000"), ("innodb_thread_concurrency", "64")], out
        assert [("rg:purge", None)] in applied  # the 12% cut was reverted (borderline is not a remedy)
        assert not any(k == "innodb_max_purge_lag" for a in applied for k, _ in a)
        # a partial cut that costs half the throughput is reverted; the lag bound last, capped at the history level reached
        out = run([("conc", [("innodb_thread_concurrency", "64")])],
                  [{"hll_per_min": 10000, "tps": 1000, "hll_end": 2e6}, {"hll_per_min": 4000, "tps": 500, "hll_end": 2.1e6},
                   {"hll_per_min": -500, "tps": 800, "hll_end": 2.1e6}],
                  last=[("lag", [("innodb_max_purge_lag", "__HLL__"), ("innodb_max_purge_lag_delay", "300000")])])
        assert out["stable"] and out["kept"] == [("innodb_max_purge_lag", "2000000"), ("innodb_max_purge_lag_delay", "300000")], out
        # a step that alone makes the history stable is kept whatever it costs (stability first, then performance)
        out = run([("conc", [("innodb_thread_concurrency", "32")])],
                  [{"hll_per_min": 10000, "tps": 1000, "hll_end": 2e6}, {"hll_per_min": -100, "tps": 450, "hll_end": 2e6}])
        assert out["stable"] and out["kept"] == [("innodb_thread_concurrency", "32")], out
        # the history is not growing now: nothing to do
        out = run([("conc", [("innodb_thread_concurrency", "64")])], [{"hll_per_min": -20, "tps": 1000, "hll_end": 5e5}])
        assert out["stable"] and not out["kept"] and not applied
    finally:
        cli.time.sleep = saved_sleep
        for k, x in saved.items():
            setattr(cli, k, x)


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
