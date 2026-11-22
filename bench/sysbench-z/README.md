# sysbench with compressible data (`--data-random-pct`)

Stock sysbench 1.0.20 fills `c` (120 chars) and `pad` (60 chars) from templates of random digits, so its InnoDB pages
compress only ~1.14x with lz4 (~2.1x zstd). `oltp_common.lua` here adds `--data-random-pct=N`: in each 11-digit group
of the templates, that share of the characters stays random and the rest becomes fixed text (the same in every row).
The same templates are used for the runtime UPDATE / INSERT statements, so the data stays as compressible while the
workload runs. Default 100 = stock behaviour. `oltp_common.lua.orig` is the stock file for diffing.

Measured (1M rows, 4 KiB pieces of the .ibd, `bench/bm2/pagecomp.py`, 2026-11-22):

| --data-random-pct | lz4 4 KiB | zstd-1 4 KiB | lz4 16 KiB |
|---|---|---|---|
| 100 (stock) | 1.14 | 2.12 | 1.16 |
| 30 | 2.19 | 4.08 | 2.42 |
| 20 | 2.51 | 4.94 | 2.83 |
| 10 | 2.96 | 5.90 | 3.30 |
| 0 | 6.35 | 8.23 | 7.62 |

Usage: copy the stock `oltp_*.lua` next to this `oltp_common.lua` and run with
`LUA_PATH='/root/bench/sysbench-z/?.lua;;' sysbench /root/bench/sysbench-z/oltp_read_write.lua --data-random-pct=10 ...`
