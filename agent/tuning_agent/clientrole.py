"""Client-host role: an agent on an application / benchmark host linked to a database environment.

It never touches MySQL. It reports OS and network metrics (RTT to the MySQL port from the client side) and executes
commands queued by the session driver through the tuner server: os_apply (change OS settings, with originals kept for
rollback) and os_status.
"""
import socket
import time

from . import __version__, hostinfo, ostune
from .state import SampleStore

CLIENT_SERVICE = "tuning-agent-client"  # no mysqld here: allocator / huge-page drop-ins are never written


def facts(cfg):
    hw = hostinfo.hardware("/")
    dev = hostinfo.device_for_path("/")
    return {"hardware": hw, "os": ostune.current("/", dev, CLIENT_SERVICE), "role": "client",
            "agent": {"version": __version__, "mysql_target": f"{cfg.get('mysql_host', '')}:{cfg.get('mysql_port', 3306)}"}}


def hello(c):
    import uuid
    if not c.state.get("agent_uid"):
        c.state.set("agent_uid", str(uuid.uuid4()))
    r = c.client.post("/hosts/hello", {"agent_uid": c.state["agent_uid"], "name": c.cfg.get("name") or socket.gethostname(),
                                        "environment_id": int(c.cfg["environment_id"]), "role": "client", "facts": facts(c.cfg)})
    if c.state.get("host_id") != r["host_id"]:
        c.state.set("host_id", r["host_id"])
    return r


def execute(c, cmd, log=print):
    action, payload = cmd.get("action"), cmd.get("payload") or {}
    dev = hostinfo.device_for_path("/")
    osa = ostune.OSApplier("/", dev, CLIENT_SERVICE, log, originals=c.state.data.setdefault("os_original", {}))
    if action == "os_status":
        return {"settings": ostune.current("/", dev, CLIENT_SERVICE)}
    if action != "os_apply":
        raise RuntimeError(f"unknown action {action}")
    before = ostune.current("/", dev, CLIENT_SERVICE)
    managed = {k: dict(v) for k, v in c.state.get("managed", {}).items()}
    applied, errors = [], []
    for ch in payload.get("changes") or []:
        var, val = ch["variable"], ch.get("value")
        if not var.startswith("os:") or var in ("os:malloc", "os:hugepages", "os:datadir_fs", "os:mount_barrier"):
            errors.append(f"{var}: not applicable on a client host")
            continue
        if var not in before:
            errors.append(f"{var}: not available on this host")
            continue
        if var not in osa.originals:
            osa.originals[var] = before.get(var)
        target = osa.originals.get(var) if val is None else str(val)
        try:
            if str(before.get(var)) != str(target):
                osa.apply_live(var, target)
            applied.append({"variable": var, "old": before.get(var), "new": target})
            if val is None:
                managed.pop(var, None)
            else:
                managed[var] = {"value": str(val), "reason": ch.get("reason", ""), "source": ch.get("source", "driver")}
        except Exception as e:
            errors.append(f"{var}: {e}")
    osa.persist(managed)
    c.state.data["managed"] = managed
    c.state.save()
    after = ostune.current("/", dev, CLIENT_SERVICE)
    for a in applied:
        a["new"] = after.get(a["variable"], a["new"])
    return {"applied": applied, "errors": errors, "settings": after}


def daemon(c, log=print):
    interval = float(c.cfg.get("interval", 10))
    store = SampleStore(c.state.dir)
    counters = hostinfo.OSCounters(hostinfo.device_for_path("/"), int(c.cfg.get("mysql_port", 3306)), role="client")
    prev, last_hello = None, 0
    log(f"tuning-agent {__version__} client daemon for environment #{c.cfg.get('environment_id')}")
    while True:
        t0 = time.time()
        try:
            snap = counters.snapshot()
            if prev:
                store.add({"ts": round(snap["t"], 3), "os": hostinfo.OSCounters.rates(prev, snap), "db": {}, "db_up": True})
            prev = snap
            if time.time() - last_hello > 300:
                hello(c)
                last_hello = time.time()
            hid = c.state.get("host_id")
            batch = store.unsent()
            if batch:
                c.client.post(f"/hosts/{hid}/metrics", {"samples": batch})
                store.mark_sent([b["ts"] for b in batch])
            for cmd in c.client.get(f"/hosts/{hid}/commands").get("commands", []):
                c.client.post(f"/commands/{cmd['id']}", {"status": "running"})
                try:
                    res = execute(c, cmd, log)
                    c.client.post(f"/commands/{cmd['id']}", {"status": "done", "result": res})
                except Exception as e:
                    c.client.post(f"/commands/{cmd['id']}", {"status": "failed", "result": {"error": str(e)}})
                hello(c)  # refresh facts (current OS settings) right away
                last_hello = time.time()
        except Exception as e:
            log(f"client daemon error: {e}")
        while time.time() - t0 < (2 if c.cfg.get("fast_poll") else interval):
            time.sleep(0.5)
