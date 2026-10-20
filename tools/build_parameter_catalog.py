#!/usr/bin/env python3
"""Build the shipped parameter catalog (agent/tuning_agent/knowledge/parameters.json) from the agent's knowledge tables
plus curated impact priors. Re-run after editing the priors below or the knowledge tables; review the diff like code.

Each entry: scope (mysql | os | pseudo), category, description, impact (high | medium | low | unknown: how much it can
move results when it counts), when (the conditions / measured bottleneck in which it counts), precondition (what must
happen in the workload for it to act at all), safety (safe | trade-off category), restart, risk notes, mechanism tags,
and the tuner rules that change it. The learned layer (learning.db, hosted service) adds measured impact per workload
class on top; see docs/design/code-data-separation.md.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
from tuning_agent import engine, knowledge, mechanisms, ostune  # noqa: E402

# curated priors: impact when it counts, and when that is (from docs, the studies of 2026-10-17..20 and field experience)
PRIORS = {
    "innodb_buffer_pool_size": ("high", "the working set misses memory (data page reads); too large causes swapping / OOM"),
    "innodb_redo_log_capacity": ("high", "write-heavy load with checkpoint age near capacity (forced flushing); measured +2-10%"),
    "innodb_log_file_size": ("high", "as innodb_redo_log_capacity on older versions"),
    "innodb_flush_log_at_trx_commit": ("high", "commit-bound load (redo fsync per transaction); measured +26-34%"),
    "sync_binlog": ("high", "binary log on and commit-bound (binlog fsync per group)"),
    "disable_log_bin": ("high", "binary log on and commit or CPU bound; measured +18%"),
    "skip_log_bin": ("high", "as disable_log_bin"),
    "innodb_doublewrite": ("high", "write / flush-bound load; measured +12% (YCSB-A) to +35-69% (ingest)"),
    "innodb_change_buffering": ("high", "insert / update-heavy with secondary indexes and data larger than memory; off measured -23% to -88% on ingest"),
    "performance_schema": ("medium", "CPU-bound, many short statements; measured 3-5% (up to 25% of CPU on 2 vCPU)"),
    "ps:profile": ("medium", "as performance_schema, keeping digests and wait summaries"),
    "thread_handling": ("medium", "connections far above CPU cores with run-queue waits; hurt at low concurrency (-9%)"),
    "innodb_io_capacity": ("medium", "flush-bound with disk headroom; raising it on a saturated disk measured -12% to -35%"),
    "innodb_io_capacity_max": ("medium", "as innodb_io_capacity"),
    "innodb_flush_method": ("medium", "double buffering in the page cache; O_DIRECT_NO_FSYNC is a trade-off"),
    "innodb_adaptive_hash_index": ("medium", "point lookups on a hot set (helps) vs latch contention or analytics (JOB 88% slower with it ON)"),
    "innodb_log_writer_threads": ("medium", "few CPUs under pressure (+10% on 2 vCPU); lost 2-13% at 12-16 vCPU"),
    "innodb_buffer_pool_instances": ("medium", "many cores with buffer pool mutex contention"),
    "innodb_max_dirty_pages_pct_lwm": ("medium", "flush-bound; depends on update locality"),
    "innodb_lru_scan_depth": ("medium", "free-page waits (single-page flushes in user threads)"),
    "innodb_purge_threads": ("medium", "history list growing under write load"),
    "innodb_read_ahead_threshold": ("medium", "scans with disk reads (analytics)"),
    "innodb_random_read_ahead": ("low", "partly cached extents read randomly"),
    "innodb_read_io_threads": ("low", "many concurrent disk reads"),
    "innodb_write_io_threads": ("low", "flush-bound with many outstanding writes"),
    "innodb_flush_neighbors": ("low", "rotating disks only (high there)"),
    "innodb_thread_concurrency": ("low", "extreme oversubscription; lost in every measured case so far"),
    "innodb_spin_wait_delay": ("low", "latch spinning in the CPU profile"),
    "innodb_sync_spin_loops": ("low", "latch spinning in the CPU profile"),
    "binlog_group_commit_sync_delay": ("medium", "binary log on, commit-bound at high concurrency"),
    "table_open_cache": ("low", "tables opened during the run (Opened_tables > 0)"),
    "table_definition_cache": ("low", "table definitions loaded during the run"),
    "thread_cache_size": ("low", "connections created during the run"),
    "skip_name_resolve": ("low", "measured time in host name resolution on connect"),
    "tmp_table_size": ("medium", "analytics spilling temporary tables to disk"),
    "max_heap_table_size": ("medium", "as tmp_table_size"),
    "sort_buffer_size": ("low", "sort merge passes during the run"),
    "join_buffer_size": ("medium", "joins without usable indexes / hash joins spilling (analytics)"),
    "optimizer_switch": ("high", "analytics with correlated subqueries (subquery_to_derived: TPC-H +70%)"),
    "innodb_log_buffer_size": ("low", "large transactions waiting for log buffer space"),
    "innodb_max_purge_lag": ("low", "history list growth hurting reads; slows writers"),
    "max_connections": ("low", "a benchmark prerequisite; too high wastes memory"),
    "back_log": ("low", "bursts of new connections dropped by the accept queue"),
    "large_pages": ("medium", "memory-bound work with a large buffer pool (TLB misses); needs measured headroom"),
    "os:hugepages": ("medium", "as large_pages"),
    "os:malloc": ("medium", "allocator time in the CPU profile, many threads"),
    "os:vm.swappiness": ("medium", "memory pressure with mysqld memory swapped out"),
    "os:swapfile": ("low", "safety net for memory experiments; no effect without memory pressure"),
    "os:transparent_hugepage": ("low", "page-fault / compaction stalls in the kernel profile"),
    "os:scheduler": ("low", "I/O-bound on SSD / NVMe"),
    "os:read_ahead_kb": ("low", "sequential scans read from disk"),
    "os:mount_noatime": ("low", "metadata writes on file access"),
    "os:mount_barrier": ("medium", "commit-bound on disks with a volatile write cache; durability trade-off"),
    "os:vm.dirty_bytes": ("low", "fsync stalls from large page-cache writeback"),
    "os:vm.dirty_background_bytes": ("low", "as vm.dirty_bytes"),
    "os:cpu_governor": ("medium", "CPU-bound on hosts that scale the clock down"),
    "os:kernel.sched_autogroup_enabled": ("low", "run-queue waits with other process groups on the host"),
    "os:kernel.numa_balancing": ("low", "multi-socket hosts"),
    "os:net.core.somaxconn": ("low", "accept-queue overflows"),
    "os:net.ipv4.tcp_max_syn_backlog": ("low", "SYN-queue drops"),
    "os:datadir_fs": ("medium", "I/O or commit-bound: file system choice changes allocation and journaling"),
}


# optimizer_switch is a container: each flag is its own setting with its own impact and safety. None of them changes
# query results; the risk of every flag is plan regressions (some queries faster, others slower), so they are "safe"
# for correctness but need the whole workload measured. (flag, server default, impact, when it counts, note)
OPTIMIZER_FLAGS = [
    ("subquery_to_derived", "off", "high", "analytics with correlated scalar subqueries in SELECT / WHERE (TPC-H +70%)", "can slow queries the default plan handled well"),
    ("batched_key_access", "off", "medium", "joins doing many random index lookups into data larger than memory (needs mrr=on and a join buffer)", "with mrr_cost_based=off it is used even when not cheaper"),
    ("mrr", "on", "medium", "range / ref reads of secondary indexes on disk-bound data (reads in primary-key order)", ""),
    ("mrr_cost_based", "on", "low", "forcing MRR / BKA on regardless of cost", "off forces MRR even where it is slower"),
    ("block_nested_loop", "on", "low", "joins without usable indexes (8.0.20+: controls hash join use)", ""),
    ("hash_join", "on", "low", "equi-joins without usable indexes (no-op on 8.0.20+, see block_nested_loop)", ""),
    ("index_merge", "on", "low", "OR / AND conditions over several indexed columns", ""),
    ("index_merge_union", "on", "low", "OR over several indexed columns", ""),
    ("index_merge_sort_union", "on", "low", "OR over ranges on several indexes", ""),
    ("index_merge_intersection", "on", "medium", "AND over several indexes; known to pick slow intersections on correlated columns", "turning it off is a common fix for intersect plans"),
    ("engine_condition_pushdown", "on", "low", "NDB only", ""),
    ("index_condition_pushdown", "on", "medium", "range scans with extra conditions on index columns (fewer row reads)", ""),
    ("materialization", "on", "medium", "IN-subqueries (semijoin materialization)", ""),
    ("semijoin", "on", "medium", "IN / EXISTS subqueries", ""),
    ("loosescan", "on", "low", "semijoin strategy choice", ""),
    ("firstmatch", "on", "low", "semijoin strategy choice", ""),
    ("duplicateweedout", "on", "low", "semijoin strategy choice", ""),
    ("subquery_materialization_cost_based", "on", "low", "choice between materialization and EXISTS rewrite", ""),
    ("use_index_extensions", "on", "low", "secondary indexes used with their implicit primary-key columns", ""),
    ("condition_fanout_filter", "on", "medium", "join order estimates on filtered columns (JOB-like multi-joins)", "estimates change join orders"),
    ("derived_merge", "on", "medium", "derived tables / views merged into the outer query", ""),
    ("derived_condition_pushdown", "on", "medium", "conditions pushed into derived tables", ""),
    ("use_invisible_indexes", "off", "low", "testing invisible indexes", "on makes the optimizer use indexes marked invisible"),
    ("skip_scan", "on", "low", "range on the second column of a composite index", ""),
    ("prefer_ordering_index", "on", "low", "ORDER BY ... LIMIT choosing an ordering index over a better filter", "off is a common fix for slow ORDER BY LIMIT plans"),
    ("hypergraph_optimizer", "off", "high", "complex multi-way joins (experimental optimizer in 8.0-9.x builds that include it)", "experimental: plans and supported features differ; debug builds only in many versions"),
    ("hash_set_operations", "on", "low", "UNION / INTERSECT / EXCEPT with large inputs", ""),
    ("favor_range_scan", "off", "low", "range scans vs ref access choice (9.x)", ""),
]


# how performance responds to the value (to be validated per workload; the learner records sweep curves):
#   saturating: more is better up to a point, then flat (or slightly worse through its cost) - climb, stop at saturation
#   optimum:    too little and too much both hurt - bracket both sides, refine towards the better one
#   binary / choice: compare the alternatives
#   tradeoff:   monotonic in speed, paid with durability or safety - each step is a decision, not a search
RESPONSE = {
    "innodb_buffer_pool_size": ("saturating", "up to the working set / data size; capped by the memory budget"),
    "innodb_redo_log_capacity": ("saturating", "until checkpoint age stays well below capacity; recovery time grows"),
    "innodb_log_file_size": ("saturating", "as innodb_redo_log_capacity"),
    "innodb_log_buffer_size": ("saturating", "until no log buffer waits"),
    "table_open_cache": ("saturating", "until no tables are opened during the run"),
    "table_definition_cache": ("saturating", "until no definitions are loaded during the run"),
    "tmp_table_size": ("saturating", "until temporary tables stop spilling to disk; capped by the memory budget"),
    "max_heap_table_size": ("saturating", "as tmp_table_size"),
    "sort_buffer_size": ("optimum", "large enough to avoid merge passes; larger allocations cost time per sort"),
    "join_buffer_size": ("optimum", "large enough for the hash join build side; larger allocations cost time"),
    "read_buffer_size": ("optimum", "scan buffering; larger allocations cost time per scan"),
    "read_rnd_buffer_size": ("optimum", "as read_buffer_size"),
    "thread_cache_size": ("saturating", "until no threads are created during the run"),
    "innodb_thread_concurrency": ("optimum", "0 = unlimited; too low starves CPUs, too high lets contention through"),
    "thread_pool_size": ("optimum", "around the number of CPUs; too few starves, too many brings contention back"),
    "innodb_purge_threads": ("optimum", "enough to keep the history list bounded; more compete with user threads"),
    "innodb_buffer_pool_instances": ("optimum", "enough to split mutex contention; more add overhead"),
    "innodb_page_cleaners": ("optimum", "as innodb_buffer_pool_instances for flushing"),
    "innodb_io_capacity": ("optimum", "matched to what the storage sustains: too low falls behind, too high steals I/O from reads"),
    "innodb_io_capacity_max": ("optimum", "as innodb_io_capacity, for catch-up flushing"),
    "innodb_lru_scan_depth": ("optimum", "enough free pages; deeper scans cost CPU"),
    "innodb_max_dirty_pages_pct_lwm": ("optimum", "trade between write combining and flushing smoothness"),
    "innodb_spin_wait_delay": ("optimum", "depends on the CPU: too short burns CPU, too long delays wake-ups"),
    "innodb_sync_spin_loops": ("optimum", "as innodb_spin_wait_delay"),
    "innodb_read_io_threads": ("optimum", "enough outstanding reads for the device; more add context switches"),
    "innodb_write_io_threads": ("optimum", "as innodb_read_io_threads for writes"),
    "innodb_read_ahead_threshold": ("optimum", "earlier read-ahead helps scans, wastes I/O on random access"),
    "binlog_group_commit_sync_delay": ("optimum", "longer batches fewer fsyncs but adds commit latency"),
    "max_connections": ("saturating", "a prerequisite: enough for the clients; more only cost memory"),
    "innodb_flush_log_at_trx_commit": ("tradeoff", "1 (durable) / 2 / 0: faster, less durable"),
    "sync_binlog": ("tradeoff", "1 (durable) / N / 0"),
    "innodb_doublewrite": ("binary", ""), "innodb_adaptive_hash_index": ("binary", ""), "innodb_change_buffering": ("choice", ""),
    "innodb_flush_method": ("choice", ""), "performance_schema": ("binary", ""), "thread_handling": ("choice", ""),
    "innodb_log_writer_threads": ("binary", ""), "large_pages": ("binary", ""), "os:malloc": ("choice", ""),
}

# memory cost per setting (agent/tuning_agent/memory.py budgets with these): memory is one pool shared by all options
MEMORY_COST = {
    "innodb_buffer_pool_size": "direct: its size, plus ~3% for control blocks / page hash / AHI",
    "innodb_log_buffer_size": "direct: its size",
    "key_buffer_size": "direct: its size",
    "tmp_table_size": "per active connection (one in-memory temporary table), capped by temptable_max_ram",
    "max_heap_table_size": "per active connection (with tmp_table_size, the lower applies)",
    "temptable_max_ram": "global cap on in-memory temporary tables",
    "sort_buffer_size": "per active connection, per sort - possibly more than once per query",
    "join_buffer_size": "per active connection, per join - possibly more than once per query",
    "read_buffer_size": "per active connection, per sequential scan",
    "read_rnd_buffer_size": "per active connection, per sorted read",
    "binlog_cache_size": "per active connection with a transaction (binary log on)",
    "table_open_cache": "~8 KiB per cached table instance",
    "table_definition_cache": "~6 KiB per cached table definition",
    "max_prepared_stmt_count": "~16 KiB per prepared statement held (a cap; budgeted at up to 20 per connection)",
    "max_connections": "~768 KiB base per allowed connection (+ performance_schema ~128 KiB); 2 x CPUs of them assumed active with all per-query buffers",
    "performance_schema": "~192 MiB fixed + ~128 KiB per allowed connection",
    "thread_cache_size": "~256 KiB per cached idle thread",
    "os:hugepages": "pins 2 MiB per page: unusable by anything else, even when the buffer pool does not use it",
    "os:swapfile": "disk, not memory: lets cold memory be swapped instead of OOM",
}


def main():
    # which rules change which variables: b.add("<id>", "<title>", [("<var>", ...), ...] and sweep("<id>", "<title>", "<var>", ...)
    import re
    src = open(engine.__file__).read()
    rules_by_var = {}
    for m in re.finditer(r'b\.add\(\s*"([a-z0-9-]+)",\s*(?:f?"[^"]*"|\([^)]*\)),\s*\[((?:\("[^"]+",[^)]*\),?\s*)+)', src):
        for var in re.findall(r'\("([a-z_:.0-9-]+)",', m.group(2)):
            rules_by_var.setdefault(var, set()).add(m.group(1))
    for m in re.finditer(r'sweep\(\s*"([a-z0-9-]+)",\s*"[^"]*",\s*"([a-z_:.0-9-]+)"', src):
        rules_by_var.setdefault(m.group(2), set()).add(m.group(1))
    out = {}
    names = set(knowledge.KB) | set(ostune.OS_KB) | set(knowledge.TRADEOFFS) | set(PRIORS)
    for name in sorted(names):
        kb = knowledge.KB.get(name) or {}
        okb = ostune.OS_KB.get(name)
        trade = knowledge.TRADEOFFS.get(name)
        rules = sorted(rules_by_var.get(name, set()))
        tags = sorted({t for r in rules for t in (mechanisms.MECHANISMS.get(r) or ((),))[0]})
        need = mechanisms.VAR_NEEDS.get(name)
        cnt = mechanisms.VAR_COUNTERS.get(name)
        impact, when = PRIORS.get(name, ("unknown", ""))
        out[name] = {
            "scope": "os" if name.startswith("os:") else "pseudo" if ":" in name else "mysql",
            "category": kb.get("cat") or (okb[2] if okb else "") or "",
            "description": kb.get("desc") or (okb[0] if okb else ""),
            "impact": impact, "when": when,
            "precondition": ({"writes": "the workload writes (redo / flushing active)", "reads": "pages are read from disk",
                              "dns": "measured time in host name resolution"}.get(need) if need else None)
                            or (f"{cnt[1]} during the run ({cnt[0].lstrip('_')} > 0)" if cnt else None),
            "safety": trade[1] if trade else ("safe" if kb.get("risk") in (None, "none", "low") else kb.get("risk")),
            "safety_note": trade[2] if trade and len(trade) > 2 else None,
            "restart": (not kb.get("dynamic", True)) if kb else (bool(okb[1]) if okb else None),
            "mechanism_tags": tags, "rules": rules, "memory_cost": MEMORY_COST.get(name),
            "response": {"shape": RESPONSE[name][0], "note": RESPONSE[name][1]} if name in RESPONSE else None,
        }
    if "optimizer_switch" in out:
        out["optimizer_switch"].update(impact="container", when="a list of independent optimizer flags: see the optimizer_switch:<flag> entries",
                                       description="Optimizer flags (on / off each); the tuner changes them one flag at a time.")
    for flag, default, impact, when, note in OPTIMIZER_FLAGS:
        out[f"optimizer_switch:{flag}"] = {
            "scope": "mysql", "category": "optimizer", "parent": "optimizer_switch", "server_default": default,
            "description": f"optimizer_switch flag {flag} (server default {default}).", "impact": impact, "when": when,
            "precondition": "statements whose plans use this strategy", "safety": "safe",
            "safety_note": ("Does not change query results; plans change, so some queries may get slower. " + note[:1].upper() + note[1:]).strip(),
            "restart": False, "mechanism_tags": ["cpu-exec", "io-read"],
            "rules": sorted(r for r in rules_by_var.get("optimizer_switch", set()) if flag.replace("_", "-") in r or r in ("bka-mrr",) and flag in ("batched_key_access", "mrr", "mrr_cost_based"))}
    path = os.path.join(ROOT, "agent", "tuning_agent", "knowledge_data", "parameters.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"version": 1, "generated_from": "tools/build_parameter_catalog.py", "parameters": out}, f, indent=1, sort_keys=True)
    print(f"{len(out)} parameters -> {path}; with an impact prior: {sum(1 for v in out.values() if v['impact'] != 'unknown')}")


if __name__ == "__main__":
    main()
