# Canaries

Two canaries, two jobs.

| | Deploy canary | Noise canary |
|---|---|---|
| Host | **tuner-canary** (Vultr bare metal, E3-1270 4c/8t, 32 GB, 2 x SSD; was tuner-vbm4c) | **tuner-os** (Linode g6-standard-2, shared 2 vCPU) |
| When | after every agent deploy, before long studies use the build | nightly (cron on tuner-os, 03:17 UTC) |
| Script | `bench/canary/run-bm.sh [max_runs] [minutes]` (default 10 runs, 75 min limit) | `bench/canary/run-noise.sh [max_runs]` (150 min limit) |
| Workload | sysbench oltp_read_write 4 x 1M, 16 threads, 80 s warm-up + 30 s, data reset per run (`bench/canary/bm.ini`) | sysbench oltp_read_write 4 x 1M, 8 threads, 100 s + 60 s (`bench/canary.ini`) |
| Noise | run to run ~0.1-0.5%: verdicts are meaningful | 4-11%: verdicts are mostly "neutral" |
| Checks | mechanics + **expected outcomes**: `tools/canary_check.py <id> --log <log> --budget 10 --expect docs/canary/expect-bm.json` | mechanics + **noise handling**: `tools/canary_check.py <id> --log <log> --noise` |

The deploy canary answers "does this build still make the right decisions?": on a quiet host the same workload gives
the same answers, so `expect-bm.json` (written from the reference session) pins the baseline throughput range, the
noise level, what the best safe configuration must and must not contain. A change of expectation is a decision (a
better engine may find more): update the file in the same commit and say why.

The noise canary answers "does the tuner still behave sensibly where real users run?": calibration splits within- and
between-run noise, the noise plan picks a remedy, disturbances (steal, swap, storage drift, unsteady windows) are
detected and flagged, and noisy single runs are not kept.

Checks every session gets (also the suite scorecard): session closed, no aborted runs, consistent comparison parents,
improvements and efficiency gains confirmed by a repeat, best safe config earned and not worse than the start, no
client-saturated verdicts, agent build recorded, no experiment measured more than 3 times (loop), the server left on the
best safe configuration after its confirmation.
