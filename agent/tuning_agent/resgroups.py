"""CPU binding and priority of InnoDB background threads with MySQL resource groups (MySQL / Percona Server 8.0+, not
MariaDB). Settings applied after mysqld starts: no option file can carry them.

Two pseudo-variables:
- rg:innodb_bg_cpus = off | CPU list. A SYSTEM resource group `ta_bg` with that VCPU set holds every InnoDB background
  thread (page cleaners, purge, log writer/flusher, I/O threads), so flushing bursts stay off the CPUs that serve queries.
- rg:purge = off | cpus=<list> | prio=<-20..0> | cpus=<list>,prio=<n>. A SYSTEM group `ta_purge` for the purge coordinator
  and workers only: their own CPUs, a higher priority, or both. Raising purge's priority keeps it running when user
  threads saturate the CPUs (a history list that grows without bound); starving it would make that worse. A purge group
  takes the purge threads out of `ta_bg`.

Thread assignments do not survive a restart: the agent re-applies them after its own restarts and the daemon checks them
every minute (ensure). THREAD_PRIORITY needs CAP_SYS_NICE for mysqld (systemd AmbientCapabilities, os:mysqld_cap_sys_nice);
without it Linux ignores the priority silently and only the CPU binding takes effect.
"""
import os
import re

GROUP = "ta_bg"
VAR = "rg:innodb_bg_cpus"
PURGE_GROUP = "ta_purge"
PURGE_VAR = "rg:purge"
VARS = (VAR, PURGE_VAR)
RG_KB = {VAR: ("CPUs for InnoDB background threads via a MySQL resource group (off / CPU list); keeps flushing bursts off the "
               "query CPUs.", False, "cpu"),
         PURGE_VAR: ("Purge threads in their own MySQL resource group (off / cpus=list / prio=-20..0 / both): own CPUs or a higher "
                     "priority, so purge keeps up while user threads saturate the CPUs. Re-applied after every mysqld start.",
                     False, "cpu")}
PURGE_THREADS = ("thread/innodb/srv_purge_thread", "thread/innodb/srv_worker_thread")


def available(db):
    try:
        db.scalar("SELECT COUNT(*) FROM information_schema.RESOURCE_GROUPS")
        return True
    except Exception:
        return False


def _row(db, group):
    r = db.dict_rows(f"SELECT VCPU_IDS v, THREAD_PRIORITY p, RESOURCE_GROUP_ENABLED e FROM information_schema.RESOURCE_GROUPS "
                     f"WHERE RESOURCE_GROUP_NAME='{group}'")
    return r[0] if r else None


def _s(v):
    return v.decode() if isinstance(v, bytes) else str(v)


def parse_purge(value):
    """'cpus=6-7,prio=-5' -> {'cpus': '6-7', 'prio': -5}; 'off' -> {}."""
    out = {}
    for part in str(value or "").replace(" ", "").split(","):
        k, _, x = part.partition("=")
        if k == "cpus" and x:
            out["cpus"] = x
        elif k == "prio" and x:
            out["prio"] = max(-20, min(0, int(x)))
    return out


def format_purge(cpus=None, prio=None):
    parts = ([f"cpus={cpus}"] if cpus else []) + ([f"prio={prio}"] if prio else [])
    return ",".join(parts) or "off"


def current(db):
    if not available(db):
        return {}
    out = {}
    try:
        r = _row(db, GROUP)
        out[VAR] = _s(r["v"]) if r else "off"
        p = _row(db, PURGE_GROUP)
        if not p:
            out[PURGE_VAR] = "off"
        else:
            all_cpus = f"0-{(os.cpu_count() or 1) - 1}"
            cpus = _s(p["v"])
            out[PURGE_VAR] = format_purge(None if cpus == all_cpus else cpus, int(p["p"] or 0))
    except Exception:
        return out
    return out


def _bg_threads(db):
    return [r["THREAD_ID"] for r in db.dict_rows("SELECT THREAD_ID FROM performance_schema.threads "
                                              "WHERE TYPE='BACKGROUND' AND NAME LIKE 'thread/innodb/%'")]


def _purge_threads(db):
    names = ", ".join(f"'{n}'" for n in PURGE_THREADS)
    return [r["THREAD_ID"] for r in db.dict_rows(f"SELECT THREAD_ID FROM performance_schema.threads WHERE NAME IN ({names})")]


def apply(db, value, var=VAR):
    """Create / alter / drop the group of `var`. Returns a note when part of it cannot take effect (priority without
    CAP_SYS_NICE), else None."""
    group = PURGE_GROUP if var == PURGE_VAR else GROUP
    off = str(value).lower() in ("off", "", "none")
    if off:
        if _row(db, group):
            db.execute(f"DROP RESOURCE GROUP {group} FORCE")  # FORCE moves its threads back to SYS_default
        ensure(db)  # purge threads go back to ta_bg when that group exists
        return None
    if var == PURGE_VAR:
        p = parse_purge(value)
        spec = (f" VCPU = {p['cpus']}" if p.get("cpus") else "") + (f" THREAD_PRIORITY = {p['prio']}" if p.get("prio") else "")
    else:
        spec = f" VCPU = {str(value).replace(' ', '')}"
    if _row(db, group):
        db.execute(f"ALTER RESOURCE GROUP {group}{spec}")
    else:
        db.execute(f"CREATE RESOURCE GROUP {group} TYPE = SYSTEM{spec}")
    ensure(db)
    want = parse_purge(value).get("prio") if var == PURGE_VAR else None
    if want and has_cap_sys_nice() is False:
        return ("mysqld lacks CAP_SYS_NICE: Linux ignores the purge threads' priority (only their CPUs apply). "
                "os:mysqld_cap_sys_nice=ON adds it (takes effect at the next mysqld start).")
    if want:
        got = thread_nice(db, PURGE_THREADS)
        off = [n for n in got if n != want]
        if got and off:
            return f"the purge threads' nice values are {sorted(set(got))}, not {want}: the priority did not take effect"
    return None


def thread_nice(db, names):
    """The kernel's nice value of each thread with one of these performance_schema names (THREAD_OS_ID ->
    /proc/<mysqld>/task/<tid>/stat): what the scheduler really uses, whatever the resource group says."""
    from .hostinfo import mysqld_pid
    pid = mysqld_pid()
    if not pid:
        return []
    out = []
    names_ = ", ".join(f"'{n}'" for n in names)
    for r in db.dict_rows(f"SELECT THREAD_OS_ID t FROM performance_schema.threads WHERE NAME IN ({names_})"):
        try:
            st = open(f"/proc/{pid}/task/{int(r['t'])}/stat").read()
            out.append(int(st[st.rindex(")") + 2:].split()[16]))  # field 19: nice
        except (OSError, ValueError, TypeError, IndexError):
            continue
    return out


def _same(var, want, now):
    """The group as wanted? The purge group by priority and (when given) CPUs - the server lists all CPUs its own way."""
    if var == PURGE_VAR:
        w, n = parse_purge(want), parse_purge(now)
        return w.get("prio") == n.get("prio") and (not w.get("cpus") or w["cpus"] == n.get("cpus"))
    return str(now or "off") == str(want)


def restore(db, managed):
    """The managed resource groups as the agent recorded them, whatever happened to the server: a data directory restored
    from a copy (the benchmark guide's reset) brings back a data dictionary without ta_purge / ta_bg, and reassigning
    threads to groups that no longer exist did nothing (user report, 2026-10-27). Creates or alters each group, then
    assigns the threads. Returns [(variable, wanted, now)] for what still differs afterwards (empty: in effect)."""
    wrong = []
    for var in VARS:
        want = ((managed or {}).get(var) or {}).get("value") if isinstance((managed or {}).get(var), dict) else (managed or {}).get(var)
        if want is None:
            continue
        cur = current(db).get(var)
        if not _same(var, want, cur):
            apply(db, want, var)
        now = current(db).get(var)
        if not _same(var, want, now):
            wrong.append((var, str(want), str(now)))
    ensure(db)
    p = parse_purge(((managed or {}).get(PURGE_VAR) or {}).get("value") if isinstance((managed or {}).get(PURGE_VAR), dict)
                    else (managed or {}).get(PURGE_VAR)).get("prio")
    if p:
        got = thread_nice(db, PURGE_THREADS)
        if got and any(n != p for n in got):
            wrong.append(("purge threads' nice", str(p), str(sorted(set(got)))))
    return wrong


def ensure(db):
    """(Re)assign threads to the groups that exist; returns how many were moved. Purge threads go to ta_purge when it
    exists, else with the other InnoDB background threads to ta_bg. mysqld disables a group at startup when its CPUs are
    not all usable (e.g. the process affinity was narrowed)."""
    moved = 0
    purge = set(_purge_threads(db)) if _row(db, PURGE_GROUP) else set()
    for group, ids in ((PURGE_GROUP, sorted(purge)), (GROUP, None)):
        row = _row(db, group)
        if not row:
            continue
        if not int(row["e"]):
            try:
                db.execute(f"ALTER RESOURCE GROUP {group} ENABLE")
            except Exception as e:
                raise RuntimeError(f"resource group {group} (CPUs {_s(row['v'])}) is disabled and cannot be enabled: {e}. "
                                   "Its CPUs must be within the CPUs mysqld may use (os:cpu_affinity).")
        if ids is None:
            ids = [i for i in _bg_threads(db) if i not in purge]
        if not ids:
            continue
        try:
            inside = {r["THREAD_ID"] for r in db.dict_rows(f"SELECT THREAD_ID FROM performance_schema.threads WHERE RESOURCE_GROUP='{group}'")}
        except Exception:
            inside = set()
        todo = [i for i in ids if i not in inside]
        if todo:
            db.execute(f"SET RESOURCE GROUP {group} FOR {', '.join(str(i) for i in todo)}")
            moved += len(todo)
    return moved


def has_cap_sys_nice(pid=None):
    """Whether mysqld may raise thread priorities (CAP_SYS_NICE in its effective capabilities); None when unknown."""
    try:
        if pid is None:
            from .hostinfo import mysqld_pid
            pid = mysqld_pid()
        if not pid:
            return None
        m = re.search(r"^CapEff:\s*([0-9a-f]+)", open(f"/proc/{pid}/status").read(), re.M)
        return bool(int(m.group(1), 16) & (1 << 23)) if m else None
    except OSError:
        return None
