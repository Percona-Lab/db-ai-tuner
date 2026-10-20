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
            "mechanism_tags": tags, "rules": rules,
        }
    path = os.path.join(ROOT, "agent", "tuning_agent", "knowledge_data", "parameters.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"version": 1, "generated_from": "tools/build_parameter_catalog.py", "parameters": out}, f, indent=1, sort_keys=True)
    print(f"{len(out)} parameters -> {path}; with an impact prior: {sum(1 for v in out.values() if v['impact'] != 'unknown')}")


if __name__ == "__main__":
    main()
