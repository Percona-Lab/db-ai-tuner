# Setting classes: where a tuned setting lives and when it is applied

The tuner changes four kinds of settings. They differ in how a setting is applied, how it persists and how a person
reproduces it without the agent.

| Class | Prefix | Applied | Persists through a restart | Reproduce without the agent |
|---|---|---|---|---|
| MySQL option | (none) | `SET PERSIST` / option file, restart when static | yes (option file) | copy the option file into my.cnf |
| Operating system | `os:` | sysctl, mount options, systemd drop-in for mysqld (allocator, CPU affinity, NUMA policy, CAP_SYS_NICE, memlock), swap file | yes (sysctl.d, fstab, drop-in, boot unit) | the files the agent writes, listed per setting |
| After MySQL starts | `rg:` | SQL against the running server: MySQL resource groups (CPU binding, thread priority of background / purge threads) | group definitions yes, **thread assignments no** | the SQL shown with the configuration, run after every start (for example from an init_file) |
| Client host | `client:` | the benchmark client's host, through its agent | as for `os:` on that host | as for `os:` |

## Settings applied after MySQL starts

- **What they are:** MySQL resource groups bind threads to CPUs and set their priority. They are the only way to treat
  background threads (purge, page cleaners, log threads) differently from user threads inside one mysqld.
- **They need re-applying:** the agent re-applies the assignments after its own restarts, and the daemon checks them
  every minute, which covers restarts the agent did not make (a data reset, a crash).

The two settings:

- **`rg:innodb_bg_cpus`:** a CPU list for every InnoDB background thread (group `ta_bg`). Flushing bursts then stay off
  the CPUs that serve queries.
- **`rg:purge`:**
  - What it does: the purge coordinator and workers get their own group, `ta_purge`, with `cpus=…`, `prio=-20..0` or
    both. A purge group takes the purge threads out of `ta_bg`.
  - When it is used: it is a stability remedy for a history list that grows without bound because purge competes with
    user threads for the CPUs. Raising purge's priority is safer than starving it.
  - CAP_SYS_NICE: without it in mysqld's capabilities, Linux ignores thread priorities and only the CPU binding applies.
    The agent detects this and reports it on apply. With OS tuning, the experiment adds `os:mysqld_cap_sys_nice=ON`
    (systemd `AmbientCapabilities=CAP_SYS_NICE`, takes effect at the next mysqld start).
  - Where it is not available:
    - MariaDB has no resource groups; the tuner gives advice instead: renice or taskset by `THREAD_OS_ID`, after every
      start.
    - Containers may restrict the usable VCPU set.
    - The behaviour with Percona's thread pool is untested.
