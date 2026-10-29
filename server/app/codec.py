"""Compressed JSON in tuner.db (2026-10-29): the run documents (runs.inputs / config / diagnosis, ~220 KB a run) and the
metric samples (metrics.data, host_metrics.data, ~3.7 KB each, the same ~150 field names every 10 s) are stored zstd-
compressed - the samples with a dictionary trained on our own samples (7.8x on 3,000 samples), the run documents
without one (10x). Lossless: unpack() gives back the exact JSON text.

Values stay readable in both forms: a TEXT value is JSON as before, a BLOB starting with the zstd frame magic is
compressed (its frame names the dictionary it needs). So old rows, new rows and rows converted in the background
(migrate_batch) can be read side by side. SQL cannot look inside a compressed value: what SQL needs is a column of its
own (runs.workload_type, steal_samples)."""
import json
import threading
import time

import zstandard as zstd

MAGIC = b"\x28\xb5\x2f\xfd"
LEVEL = 3
DICT_SIZE = 64 * 1024
_lock = threading.Lock()
_dicts = {}       # dictionary id -> ZstdCompressionDict
_metrics_dict = None
_plain_c = zstd.ZstdCompressor(level=LEVEL)
_checked = [0.0]  # when this process last looked for a trained dictionary
_comp = {}        # dictionary id -> compressor (one per thread would be safer; calls are short and serialised by _lock)


def _store():
    from . import store
    return store


def init(c):
    """Load the stored dictionaries (fast: at startup). Training happens in the background (train_if_missing)."""
    global _metrics_dict
    c.execute("CREATE TABLE IF NOT EXISTS zdicts (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL, data BLOB NOT NULL)")
    for r in c.execute("SELECT id, kind, data FROM zdicts ORDER BY created"):
        d = zstd.ZstdCompressionDict(bytes(r[2]))
        _dicts[d.dict_id()] = d
        if r[1] == "metrics":
            _metrics_dict = d


def train_if_missing(c):
    """Train the samples' dictionary from recent samples when there is none (~15 s of CPU on tuner-web: never at startup -
    it made the new version miss the deploy's 30 s health check). Until then samples are compressed without one."""
    global _metrics_dict
    init(c)
    if _metrics_dict is None:
        # the newest samples of the most recently active environments, by primary key (sorting the whole table by time
        # read all of it)
        samples = []
        for (env,) in c.execute("SELECT id FROM environments ORDER BY last_seen DESC LIMIT 10").fetchall():
            samples += [r[0].encode() if isinstance(r[0], str) else None
                        for r in c.execute("SELECT data FROM metrics WHERE env_id=? ORDER BY ts DESC LIMIT 300", (env,))]
        samples = [s for s in samples if s]
        if len(samples) >= 500:
            d = zstd.train_dictionary(DICT_SIZE, samples[:3000])
            try:
                c.execute("INSERT INTO zdicts(id, kind, created, data) VALUES (?,?,?,?)", (d.dict_id(), "metrics", time.time(), d.as_bytes()))
            except Exception:  # another worker trained one at the same moment: use the stored one
                r = c.execute("SELECT data FROM zdicts WHERE kind='metrics' ORDER BY created LIMIT 1").fetchone()
                d = zstd.ZstdCompressionDict(bytes(r[0]))
            _dicts[d.dict_id()] = d
            _metrics_dict = d


def pack(value, kind="doc"):
    """A Python value (or JSON text) -> compressed bytes. kind 'metrics' uses the samples' dictionary when there is one."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    raw = text.encode()
    if kind == "metrics" and _metrics_dict is None and time.time() - _checked[0] > 300:
        # trained by the background job of another worker: pick it up (until then, compressed without one)
        _checked[0] = time.time()
        try:
            init(_store().conn())
        except Exception:
            pass
    with _lock:
        d = _metrics_dict if kind == "metrics" else None
        if d is None:
            return _plain_c.compress(raw)
        cz = _comp.get(d.dict_id())
        if cz is None:
            cz = _comp[d.dict_id()] = zstd.ZstdCompressor(level=LEVEL, dict_data=d)
        return cz.compress(raw)


def unpack(v):
    """A stored value -> JSON text (TEXT as it is; a compressed BLOB decompressed)."""
    if v is None or isinstance(v, str):
        return v
    b = bytes(v)
    if not b.startswith(MAGIC):
        return b.decode()
    did = zstd.get_frame_parameters(b).dict_id
    if did:
        d = _dicts.get(did)
        if d is None:  # trained by another worker after this one started
            r = _store().conn().execute("SELECT data FROM zdicts WHERE id=?", (did,)).fetchone()
            d = _dicts[did] = zstd.ZstdCompressionDict(bytes(r[0]))
        return zstd.ZstdDecompressor(dict_data=d).decompress(b).decode()
    return zstd.ZstdDecompressor().decompress(b).decode()


def migrate_batch(c, table, key_cols, cols, kind, after=None, batch=200):
    """Compress the next `batch` rows of `table` (in key order, after the key `after`) that still hold TEXT in any of
    `cols`. Returns (rows converted, the last key read - None when the table is done). Walking by key keeps every batch
    cheap: a "WHERE typeof(x)='text'" scan would re-read the converted rows each time."""
    keys = ", ".join(key_cols)
    where = f"WHERE ({keys}) > ({', '.join('?' * len(key_cols))})" if after else ""
    rows = c.execute(f"SELECT {keys}, {', '.join(cols)} FROM {table} {where} ORDER BY {keys} LIMIT ?",
                     (*(after or ()), batch)).fetchall()
    if not rows:
        return 0, None
    nk = len(key_cols)
    upd = [[pack(v, kind) if isinstance(v, str) else v for v in r[nk:]] + list(r[:nk])
           for r in rows if any(isinstance(v, str) for v in r[nk:])]
    if upd:
        c.execute("BEGIN")
        try:
            c.executemany(f"UPDATE {table} SET {', '.join(x + '=?' for x in cols)} WHERE {' AND '.join(k + '=?' for k in key_cols)}", upd)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return len(upd), tuple(rows[-1][:nk])


def migrate(log=print, pause=0.2):
    """Background conversion of everything stored before compression: runs first (the cross-run queries), then the
    samples. Restartable (converted rows are skipped); each batch its own short transaction."""
    c = _store().conn()
    for table, key_cols, cols, kind in (("runs", ("id",), ("inputs", "config", "diagnosis"), "doc"),
                                        ("host_metrics", ("host_id", "ts"), ("data",), "metrics"),
                                        ("metrics", ("env_id", "ts"), ("data",), "metrics")):
        after, done = None, 0
        while True:
            n, after = migrate_batch(c, table, key_cols, cols, kind, after)
            done += n
            if after is None:
                break
            time.sleep(pause)
        if done:
            log(f"codec: {table}: {done} rows compressed")
