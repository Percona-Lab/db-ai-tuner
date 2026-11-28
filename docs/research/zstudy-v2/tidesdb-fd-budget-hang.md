# TidesDB (tidesdb-mysql v0.5.0): readers and compaction deadlock on the open-SSTable budget (2026-11-28)

Reported upstream: https://github.com/EvgeniyPatlan/tidesdb-mysql/issues/12 (2026-11-28).

Same build as `tidesdb-tc-log-mmap-hang.md` (binary log on). sysbench oltp_read_write (compressible data), 16 x 1M rows,
64 threads, TidesDB block cache 1 GB. After ~240 s at ~1,250 tps, throughput fell to 0 and stayed there; mysqld ~10% CPU.

`tidesdb_max_open_sstables` defaulted to **256** (not the 1024 the MariaDB docs describe); the column families had **647**
SSTables (klog files). Stacks (`tidesdb-fd-budget-hang-stacks.txt`): 64 client threads loop in
`fd_manager_reader_budget_ok() <- sstable_get_at_seq() <- cf_source_scan_level()` (usleep, retry); compaction loops in
the same `fd_manager_reader_budget_ok() <- sstable_iter_new() <- ce_merge_inputs() <- compaction_exec()`. Compaction is
what would reduce the file count, but it needs the same budget, so nothing progresses.

Workaround: `tidesdb_max_open_sstables = 16384` and `open_files_limit = 65535`. With it a 13-minute run did not hang,
but throughput drifted down (~1,300 -> ~950 tps) as SSTables grew to 1,557: compaction falls behind at 64 writers.
