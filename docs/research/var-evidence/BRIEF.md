# Brief: evidence signals per tunable variable

Goal (user, 2026-10-31): for every setting the AI DB tuner may change, answer
"how do we know this variable is NOT at its optimal value here, which way to move it, and how much could it matter?" -
from measurements, not rules of thumb. The tuner should try a change because measured evidence shows its mechanism
costs time or resources (and how much), then validate with an experiment whose predicted metric movement can be checked.

Context: the tuner runs on MySQL 8.0/8.4/9.x, Percona Server 8.0/8.4/9.7, MariaDB 10.11/11.4/11.8 and Linux. Per
benchmark run it already collects: SHOW GLOBAL STATUS deltas (all counters, as rates), InnoDB status counters
(buffer pool reads/read_requests/wait_free/pages flushed/dirty/free, read-ahead and read_ahead_evicted, data
reads/writes/fsyncs, os_log_written/fsyncs, log_waits, dblwr writes, row lock waits/time, history list length,
change-buffer size, checkpoint age), performance_schema statement digests (latency, rows examined/sent, tmp tables,
sort merge passes, no-index flags), performance_schema wait summaries (top wait events), file I/O by InnoDB file type,
a CPU profile by category (perf: B-tree, buffer pool, latches/spin, performance_schema, network, statement dispatch...),
eBPF off-CPU wait categories of connection threads (data page read, redo flush, row lock, latch/futex, client),
OS metrics (CPU user/sys/iowait/steal, per-thread run-queue wait, disk IOPS/MB/s/await/util, memory, swap, PSI),
and per-run summaries per transaction (mysqld CPU ms, disk I/Os, disk KiB, fsyncs, redo KiB, rows read).

For EACH variable in your chunk file write one JSON object (list in your output file), fields:
- "variable": name
- "relevant_to": which flavors/releases (or "mariadb only", "os"), and when the variable cannot matter at all
- "signals": list of {"source": status|performance_schema|innodb_metrics|information_schema|engine_status|os|ebpf|derived,
    "metric": exact counter/table/column or formula (e.g. "Innodb_buffer_pool_reads / Innodb_buffer_pool_read_requests"),
    "points_to": "raise" | "lower" | "enable" | "disable" | "a specific value" | "no change needed",
    "condition": when the signal says the value is not optimal (concrete thresholds only where documented/defensible;
                 otherwise relative: "a material share of ...")
    "why": the mechanism in one sentence}
- "impact_bound": how to bound the possible gain FROM MEASUREMENTS (e.g. "at most the share of foreground time spent
    waiting on data page reads (off-CPU); times the fraction of those reads a larger pool would cache"), and the
    typical magnitude seen in practice (none/low/medium/high with a short reason)
- "experiment": {"change": what to try, "predict": which metric(s) must move which way if the hypothesis holds,
    "confirm_or_refute": how to read the result, "cost_or_risk": what it may cost}
- "collected": "yes" if the signals above are in the list of what the tuner already collects, else "partly"/"no" and
    the missing metric(s) to add
- "sources": 1-4 URLs (official docs first: dev.mysql.com, docs.percona.com, mariadb.com/kb, kernel docs; well-known
    engineering blogs for practice)

Rules: be factual; verify counter and table names against documentation (names differ between MySQL and MariaDB);
say "no measurable signal" honestly when a variable can only be validated by an experiment; prefer signals that
measure time or work (waits, reads, CPU share) over ratios of counters. Keep each entry compact.

Write the result to docs/research/var-evidence/out/<chunk-name>.json (a JSON list) and a 10-line summary of the most
useful findings (high-impact variables whose signals the tuner does not collect yet) to
docs/research/var-evidence/out/<chunk-name>.notes.md. Do not modify any other file.
