# TidesDB (tidesdb-mysql v0.5.0) hangs every commit when the binary log is off (2026-11-26)

Build: Percona Server 9.7.2-3 from source (RelWithDebInfo) with storage/tidesdb from EvgeniyPatlan/tidesdb-mysql v0.5.0
(8a0b1a0) and TidesDB v10.0.1 + the port's engine patch; MyRocks built in. `bench/v2/build-ps97-tidesdb.sh`.

Reproduce: `disable_log_bin`, plugins InnoDB + TidesDB (with or without MyRocks), sysbench oltp_read_write on
ENGINE=TIDESDB, 4 x 200k rows, 16 threads. Within seconds every connection waits in COMMIT
("waiting for handler commit") forever; mysqld at 0% CPU.

Stacks (`tidesdb-tc-log-mmap-hang-stacks.txt`): all 16 client threads in
`TC_LOG_MMAP::overflow() <- TC_LOG_MMAP::log_xid() <- TC_LOG_MMAP::commit() <- ha_commit_trans()`. With the binary log
off and more than one 2PC engine loaded the server uses the mmap transaction coordinator log (tc.log); a page is freed
only when every participating engine has finished the commit and `unlog()` runs. The log fills and never drains, so
TidesDB appears not to complete its side for commits logged there (InnoDB and MyRocks on the same build and settings do
not hang).

| Variant | Result |
|---|---|
| binlog off, InnoDB + MyRocks + TidesDB | hang (16 stuck commits) |
| binlog off, InnoDB + TidesDB | hang (16 stuck commits) |
| **binlog on** (`log_bin`, `sync_binlog = 0`, ROW), all three engines | **runs: 1,724 tps, 46 ignored errors/s (write conflicts)** |

Side effect: after a kill the leftover tc.log names 3 engines; starting with 2 fails ("You must enable exactly 3 storage
engines that support two-phase commit protocol") until tc.log is removed.

Workaround: run TidesDB with the binary log on (the binlog becomes the transaction coordinator).
