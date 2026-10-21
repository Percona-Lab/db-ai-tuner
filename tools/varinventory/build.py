#!/usr/bin/env python3
"""Merge the per-series inventories (collect.py output in tools/varinventory/data/) into the shipped
agent/tuning_agent/knowledge_data/server_variables.json: for every server variable, which release series have it, its
compiled-in default per series, whether it can be changed at runtime per series, its type and range.

Series keys match the server's evidence scope ("percona 8.4", "mariadb 11.4"; see server_series in server/app/main.py).
Values that are the same in every series that has the variable are stored once; otherwise per series.
usage: build.py   (re-run after collecting new inventories; review the diff like code)
"""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "tools", "varinventory", "data")
OUT = os.path.join(ROOT, "agent", "tuning_agent", "knowledge_data", "server_variables.json")
# values that depend on the host or the container rather than on the release (paths, sizes derived from RAM / CPUs)
HOST_DEPENDENT = ("dir", "file", "path", "socket", "hostname", "pid_file", "server_uuid", "server_id_bits", "version",
                  "basedir", "plugin_dir", "tmpdir", "log_error", "general_log_file", "slow_query_log_file", "relay_log",
                  "log_bin_basename", "log_bin_index", "innodb_buffer_pool_size", "innodb_buffer_pool_chunk_size",
                  "innodb_buffer_pool_instances", "open_files_limit", "table_open_cache", "max_connections", "thread_cache_size",
                  "innodb_page_cleaners", "innodb_purge_threads", "innodb_read_io_threads", "innodb_write_io_threads",
                  "innodb_parallel_read_threads", "innodb_log_writer_threads", "innodb_ddl_threads", "thread_pool_size",
                  "gtid_executed", "gtid_purged", "wsrep_cluster_address", "wsrep_node_address", "wsrep_provider_options",
                  "ssl_", "tls_", "large_page_size", "innodb_redo_log_capacity", "innodb_log_file_size",
                  "innodb_log_files_in_group", "innodb_numa_interleave", "report_", "secure_file_priv", "timestamp",
                  "system_time_zone", "warning_count", "error_count", "insert_id", "last_insert_id", "identity",
                  "pseudo_", "rand_seed", "lc_messages_dir", "character_sets_dir", "datadir", "innodb_data_home_dir")


def series_key(d):
    s = d["series"]  # file name: percona-8.4
    fam, _, ver = s.partition("-")
    return f"{fam} {ver}"


def host_dependent(name):
    return any(name == h or (h.endswith(("_", "dir", "file", "path")) and h in name) for h in HOST_DEPENDENT)


def main():
    inv = {}
    for p in sorted(glob.glob(os.path.join(DATA, "*.json"))):
        d = json.load(open(p))
        d.setdefault("series", os.path.basename(p)[:-5])
        inv[series_key(d)] = d
    series = sorted(inv, key=lambda s: (s.split()[0], [int(x) for x in s.split()[1].split(".")]))
    names = sorted({v for d in inv.values() for v in d["variables"]})
    out = {}
    for n in names:
        have = [s for s in series if n in inv[s]["variables"]]
        e = {"series": have if len(have) < len(series) else "all"}
        for field in ("value", "dynamic", "type", "min", "max", "values", "scope"):
            per = {s: inv[s]["variables"][n][field] for s in have if field in inv[s]["variables"][n]}
            if not per:
                continue
            key = "default" if field == "value" else field
            if field == "value" and host_dependent(n):
                key = "observed"  # depends on the host / container it was collected on: for reference only
            vals = {json.dumps(v, sort_keys=True) for v in per.values()}
            e[key] = next(iter(per.values())) if len(vals) == 1 and len(per) == len(have) else per
        out[n] = e
    meta = {s: {"version": inv[s].get("version"), "version_comment": inv[s].get("version_comment"), "image": inv[s].get("image"),
                "variables": len(inv[s]["variables"]), "status_counters": len(inv[s].get("status") or []),
                "engines": inv[s].get("engines")} for s in series}
    with open(OUT, "w") as f:
        json.dump({"version": 1, "generated_from": "tools/varinventory/build.py", "collected": "2026-10-21",
                   "note": "Compiled-in defaults of the official container images with no option file changes; 'observed' "
                           "values depend on the collecting host. Plugin variables (MyRocks rocksdb_*, Galera outside PXC) "
                           "appear only with the plugin loaded.",
                   "series": meta, "variables": out}, f, indent=0, sort_keys=True)
    print(f"{len(out)} variables over {len(series)} series -> {OUT} ({os.path.getsize(OUT) // 1024} KiB)")


if __name__ == "__main__":
    main()
