"""performance_schema instrumentation profile: ps:profile = full | lite.

lite keeps exactly what the tuner reads - statement digests and the latency histogram, prepared statement stats,
file / table I/O and lock wait summaries, thread information - and turns off what costs CPU for every statement or
allocation without being used by it: statement and transaction current/history tables, transaction and stage
instruments, and memory instrumentation (several hundred instruments that account every allocation).
Applied at runtime (no restart) and persisted in the option file.
"""
VAR = "ps:profile"
PS_KB = {VAR: ("performance_schema instrumentation: full (server default) or lite (only what the tuner reads: digests, "
               "latency histogram, wait summaries); lite loses statement history and memory accounting.", False, "observability")}
CONSUMERS = ("events_statements_current", "events_statements_history", "events_transactions_current", "events_transactions_history")
INSTRUMENTS = ("memory/%", "transaction", "stage/%")


def current(db):
    try:
        if str(db.scalar("SELECT @@performance_schema")) not in ("1", "ON"):
            return {}
        on = db.scalar("SELECT ENABLED FROM performance_schema.setup_consumers WHERE NAME='events_statements_history'")
        # memory/performance_schema/* instruments are built in and cannot be disabled
        mem = db.scalar("SELECT COUNT(*) FROM performance_schema.setup_instruments WHERE NAME LIKE 'memory/%%' "
                        "AND NAME NOT LIKE 'memory/performance_schema/%%' AND ENABLED='YES'")
    except Exception:
        return {}
    return {VAR: "lite" if on == "NO" and not int(mem or 0) else "full"}


def apply(db, value):
    v = "NO" if value == "lite" else "YES"
    names = ", ".join(f"'{c}'" for c in CONSUMERS)
    db.execute(f"UPDATE performance_schema.setup_consumers SET ENABLED='{v}' WHERE NAME IN ({names})")
    for pat in INSTRUMENTS:
        op = "LIKE" if "%" in pat else "="
        timed = v if not pat.startswith("memory") else "NO"  # memory instruments are never timed
        db.execute(f"UPDATE performance_schema.setup_instruments SET ENABLED='{v}', TIMED='{timed}' WHERE NAME {op} '{pat}'")


def cnf_lines(value):
    if value != "lite":
        return []
    lines = ["# tuning-agent ps:profile=lite"]
    lines += [f"performance-schema-consumer-{c.replace('_', '-')} = OFF" for c in CONSUMERS]
    lines += [f"performance-schema-instrument = '{p}=OFF'" for p in INSTRUMENTS]
    return lines
