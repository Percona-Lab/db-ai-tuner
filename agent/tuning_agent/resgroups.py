"""CPU binding of InnoDB background threads with MySQL resource groups (MySQL / Percona Server 8.0+, not MariaDB).

Pseudo-variable rg:innodb_bg_cpus = off | CPU list. A SYSTEM resource group `ta_bg` with that VCPU set is created and
every InnoDB background thread (page cleaners, purge, log writer/flusher, I/O threads) is assigned to it, so flushing
bursts stay off the CPUs that serve queries. Thread assignments do not survive a restart: the agent re-applies them
after its own restarts and the daemon checks them every minute.
"""
GROUP = "ta_bg"
VAR = "rg:innodb_bg_cpus"
RG_KB = {VAR: ("CPUs for InnoDB background threads via a MySQL resource group (off / CPU list); keeps flushing bursts off the "
               "query CPUs.", False, "cpu")}


def available(db):
    try:
        db.scalar("SELECT COUNT(*) FROM information_schema.RESOURCE_GROUPS")
        return True
    except Exception:
        return False


def current(db):
    if not available(db):
        return {}
    try:
        row = db.dict_rows(f"SELECT VCPU_IDS FROM information_schema.RESOURCE_GROUPS WHERE RESOURCE_GROUP_NAME='{GROUP}'")
    except Exception:
        return {}
    if not row:
        return {VAR: "off"}
    v = list(row[0].values())[0]
    return {VAR: v.decode() if isinstance(v, bytes) else str(v)}


def _bg_threads(db):
    return [r["THREAD_ID"] for r in db.dict_rows("SELECT THREAD_ID FROM performance_schema.threads "
                                              "WHERE TYPE='BACKGROUND' AND NAME LIKE 'thread/innodb/%'")]


def apply(db, value):
    if str(value).lower() in ("off", "", "none"):
        if db.dict_rows(f"SELECT 1 FROM information_schema.RESOURCE_GROUPS WHERE RESOURCE_GROUP_NAME='{GROUP}'"):
            db.execute(f"DROP RESOURCE GROUP {GROUP} FORCE")  # FORCE moves its threads back to SYS_default
        return
    cpus = str(value).replace(" ", "")
    if db.dict_rows(f"SELECT 1 FROM information_schema.RESOURCE_GROUPS WHERE RESOURCE_GROUP_NAME='{GROUP}'"):
        db.execute(f"ALTER RESOURCE GROUP {GROUP} VCPU = {cpus}")
    else:
        db.execute(f"CREATE RESOURCE GROUP {GROUP} TYPE = SYSTEM VCPU = {cpus}")
    ensure(db)


def ensure(db):
    """(Re)assign InnoDB background threads to the group; returns how many were moved.
    mysqld disables a group at startup when its CPUs are not all usable (e.g. the process affinity was narrowed)."""
    row = db.dict_rows(f"SELECT RESOURCE_GROUP_ENABLED e, VCPU_IDS v FROM information_schema.RESOURCE_GROUPS WHERE RESOURCE_GROUP_NAME='{GROUP}'")
    if not row:
        return 0
    if not int(row[0]["e"]):
        try:
            db.execute(f"ALTER RESOURCE GROUP {GROUP} ENABLE")
        except Exception as e:
            raise RuntimeError(f"resource group {GROUP} (CPUs {row[0]['v']}) is disabled and cannot be enabled: {e}. "
                               "Its CPUs must be within the CPUs mysqld may use (os:cpu_affinity).")
    ids = _bg_threads(db)
    if not ids:
        return 0
    try:
        inside = {r["THREAD_ID"] for r in db.dict_rows(f"SELECT THREAD_ID FROM performance_schema.threads WHERE RESOURCE_GROUP='{GROUP}'")}
    except Exception:
        inside = set()
    todo = [i for i in ids if i not in inside]
    if todo:
        db.execute(f"SET RESOURCE GROUP {GROUP} FOR {', '.join(str(i) for i in todo)}")
    return len(todo)
