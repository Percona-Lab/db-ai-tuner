# Evidence scope: server variant and release series

MySQL, Percona Server and MariaDB differ in options, defaults and behaviour, and release series of one variant can
differ too (8.0 / 8.4 / 9.x; MariaDB 10.11 / 11.4). What a setting did on one server is strongest evidence for the
same variant and series, weaker for other series of the variant, weaker still for other variants - but still worth
something when nothing closer exists.

**Series** = variant + major.minor: `percona 8.4`, `mysql 9.7`, `mariadb 11.4` (from the agent's server facts).

| Evidence | From | Weight |
|---|---|---|
| Experience (outcomes of each idea) | same series, similar workload / CPU class / concurrency | 1.0 |
| | same variant, other series | 0.6 |
| | other variants | 0.3 |
| | any conditions, any server | 0.3 |
| Learner insights | same series | 1.0 |
| | same variant, other series, or series not recorded (insights before 2026-10-21) | 0.6 |
| | other variant | 0.3 |
| Learned parameter impact | per row, the same weights | |

The agent uses the most specific experience tier with enough runs; strong penalties (an idea pushed to the end after
repeated losses) come only from same-series evidence. Insights about the same settings and direction from a closer
server replace less specific ones. Learned parameter impact reports how many of its runs came from the same series.

Rules stay version-aware through the running server: a change is proposed only for settings the server has
(`SHOW GLOBAL VARIABLES`), values and defaults are read from it, and features that exist only in some variants are
checked by their own variables (e.g. the thread pool by `thread_pool_size`: MySQL Community has `thread_handling`
but not `pool-of-threads`). A setting the server rejects is a failed experiment, rolled back.
