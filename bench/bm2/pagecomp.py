#!/usr/bin/env python3
"""How well would a database's InnoDB pages compress in zram / zswap?  Reads every .ibd file of a schema in 4 KiB
pieces (the unit the kernel compresses: one InnoDB 16 KiB page = 4 kernel pages) and compresses each piece on its own
with lz4 (zram/zswap default fast mode) and zstd level 1. All-zero pieces (unused extents of freshly created files) are
counted separately: zram/zswap store them as "same-filled" for free, but a working buffer pool holds real pages.
A piece that does not shrink is charged at 4096 bytes (zram keeps it uncompressed; zswap rejects it).
usage: pagecomp.py <name> <schema dir> [mysqldump lz4 ratio]   -> one JSON line"""
import json
import os
import sys

import lz4.block
import zstandard

name, d = sys.argv[1], sys.argv[2]
dump = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
zc = zstandard.ZstdCompressor(level=1)
P = 4096
zero = bytes(P)
n = nz = 0
lz = zs = 0
lz16 = 0
big = []
for root, _, files in os.walk(d):
    for f in files:
        if not f.endswith(".ibd"):
            continue
        tn = tl = 0
        with open(os.path.join(root, f), "rb") as fh:
            while True:
                b = fh.read(16384)
                if not b:
                    break
                if b == bytes(len(b)):
                    nz += len(b) // P
                    continue
                lz16 += min(len(lz4.block.compress(b, store_size=False)), len(b))
                for i in range(0, len(b), P):
                    c = b[i:i + P]
                    if c == zero:
                        nz += 1
                        continue
                    n += 1
                    tn += 1
                    l1 = min(len(lz4.block.compress(c, store_size=False)), P)
                    lz += l1
                    tl += l1
                    zs += min(len(zc.compress(c)), P)
        if tn:
            big.append((tn * P, f, round(tn * P / tl, 2)))
big.sort(reverse=True)
print(json.dumps({"bench": name, "data_mb": round(n * P / 2**20), "zero_mb": round(nz * P / 2**20),
                  "lz4_4k": round(n * P / lz, 2) if lz else None, "zstd1_4k": round(n * P / zs, 2) if zs else None,
                  "lz4_16k": round(n * P / lz16, 2) if lz16 else None, "dump_lz4": dump,
                  "tables": [{"t": f, "mb": round(s / 2**20), "lz4": r} for s, f, r in big[:6]]}))
