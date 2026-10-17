"""Write the managed option file, apply dynamic changes, restart when needed, verify, roll back on failure."""
import os
import subprocess
import time

import pymysql

from .knowledge import KB, OPTIONS, flags_subset, normalize, parse_size

HEADER = """# Managed by tuning-agent. Do not edit by hand: changes are overwritten.
# Use `tuning-agent apply --set var=value` / `tuning-agent rollback` instead.
"""

CNF_DIRS = ["/etc/mysql/mysql.conf.d", "/etc/mysql/mariadb.conf.d", "/etc/my.cnf.d", "/etc/mysql/conf.d"]


def detect_cnf_path():
    for d in CNF_DIRS:
        if os.path.isdir(d):
            return os.path.join(d, "zz-tuning-agent.cnf")
    return "/etc/mysql/conf.d/zz-tuning-agent.cnf"


def detect_service():
    for name in ("mysql", "mysqld", "mariadb"):
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
        if r.stdout.strip() in ("active", "activating", "failed"):
            return name
    return "mysql"


def render(managed):
    lines = [HEADER, "[mysqld]"]
    for var in sorted(managed):
        m = managed[var]
        if m.get("reason"):
            lines.append(f"# {m.get('source', 'manual')}: {m['reason'][:200]}".replace("\n", " "))
        lines.append(var if var in OPTIONS else f"{var} = {m['value']}")
    return "\n".join(lines) + "\n"


def sql_value(var, value):
    if str(value).upper() == "DEFAULT":
        return "DEFAULT"
    kind = KB.get(var, {}).get("kind")
    if kind == "size":
        n = parse_size(value)
        if n is not None:
            return str(n)
    s = str(value)
    if s.upper() in ("ON", "OFF", "DEFAULT") or s.lstrip("-").replace(".", "", 1).isdigit():
        return s
    return "'" + s.replace("'", "''") + "'"


class Applier:
    def __init__(self, cfg, state, db, log=print):
        self.cfg = cfg
        self.state = state
        self.db = db
        self.log = log
        self.cnf_path = cfg.get("cnf_path") or detect_cnf_path()
        self.service = cfg.get("service_name") or detect_service()

    def write_cnf(self, managed):
        os.makedirs(os.path.dirname(self.cnf_path), exist_ok=True)
        tmp = self.cnf_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(render(managed))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.cnf_path)

    def restart(self, timeout=600):
        self.log(f"restarting {self.service} ...")
        self.db.close()
        r = subprocess.run(["systemctl", "restart", self.service], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[-800:]
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.db.connect()
                self.db.scalar("SELECT 1")
                return True, ""
            except Exception as e:  # noqa
                last = str(e)
                time.sleep(2)
        return False, "server did not accept connections: " + last

    def journal_tail(self):
        r = subprocess.run(["journalctl", "-u", self.service, "-n", "30", "--no-pager"], capture_output=True, text=True)
        return r.stdout[-3000:]

    def apply(self, changes, allow_restart=True, force_restart=False):
        old_managed = {k: dict(v) for k, v in self.state["managed"].items()}
        try:
            return self._apply(changes, allow_restart, force_restart)
        except Exception:
            # never leave an option file the agent's state does not describe
            self.write_cnf(old_managed)
            self.state.data["managed"] = old_managed
            self.state.save()
            raise

    def _apply(self, changes, allow_restart=True, force_restart=False):
        """changes: [{variable, value (None = unmanage), reason, source}]
        Returns dict(applied, restarted, needs_restart, mismatches, errors)."""
        before_vars = self.db.variables()
        old_managed = {k: dict(v) for k, v in self.state["managed"].items()}
        managed = {k: dict(v) for k, v in old_managed.items()}
        result = {"applied": [], "restarted": False, "needs_restart": [], "mismatches": [], "errors": [], "cnf_path": self.cnf_path}
        for c in changes:
            var = c["variable"].lower().replace("-", "_")
            c["variable"] = var
            if var not in before_vars and var not in OPTIONS:
                result["errors"].append(f"{var}: unknown to this server, skipped")
                continue
            if c.get("value") is None:
                managed.pop(var, None)
            else:
                managed[var] = {"value": str(c["value"]), "reason": c.get("reason", ""), "source": c.get("source", "manual")}
            c["old"] = before_vars.get(var) if var not in OPTIONS else ("OFF" if var in old_managed else "")
        changes = [c for c in changes if "old" in c]
        if not changes:
            return result
        self.write_cnf(managed)
        need_restart = []
        for c in changes:
            var, val = c["variable"], c.get("value")
            if val is not None and (flags_subset(val, before_vars.get(var)) if KB.get(var, {}).get("kind") == "flags"
                                    else normalize(var, before_vars.get(var)) == normalize(var, val)):
                continue
            meta = KB.get(var)
            if var in OPTIONS or (meta is not None and not meta.get("dynamic")):
                need_restart.append(var)
                continue
            try:
                self.db.execute(f"SET GLOBAL {var} = {sql_value(var, 'DEFAULT' if val is None else val)}")
            except pymysql.MySQLError as e:
                code = e.args[0] if e.args else 0
                if code in (1238, 1621):  # read only / read only, restart needed
                    need_restart.append(var)
                else:
                    result["errors"].append(f"{var}: {e.args[-1] if e.args else e}")
        if (need_restart or force_restart) and allow_restart:
            ok, err = self.restart()
            if not ok:
                self.log("restart FAILED, restoring previous configuration: " + err)
                tail = self.journal_tail()
                self.write_cnf(old_managed)
                ok2, err2 = self.restart()
                result["errors"].append("restart failed, previous configuration restored" + ("" if ok2 else f" (and restart after restore failed: {err2})"))
                result["journal"] = tail
                result["rolled_back"] = True
                return result
            result["restarted"] = True
        elif need_restart:
            result["needs_restart"] = need_restart
        # persist + verify
        self.state.data["managed"] = managed
        self.state.save()
        # buffer pool resize is asynchronous; give it a moment
        if any(c["variable"] == "innodb_buffer_pool_size" for c in changes) and not result["restarted"]:
            for _ in range(60):
                cur = self.db.scalar("SELECT @@innodb_buffer_pool_size")
                want = parse_size(managed.get("innodb_buffer_pool_size", {}).get("value"))
                if want is None or int(cur) >= want * 0.95 and int(cur) <= want * 1.05:
                    break
                time.sleep(2)
        after = self.db.variables()
        for c in changes:
            var = c["variable"]
            if var in OPTIONS:  # verify via the variable the option controls
                shown = after.get(OPTIONS[var])
                entry = {"variable": var, "old": c["old"] or "", "new": "" if c.get("value") is None else "ON",
                         "reason": c.get("reason", ""), "source": c.get("source", "manual"), "effect": f"{OPTIONS[var]}={shown}"}
                result["applied"].append(entry)
                continue
            entry = {"variable": var, "old": c["old"], "new": after.get(var), "reason": c.get("reason", ""), "source": c.get("source", "manual")}
            ok = flags_subset(c["value"], after.get(var)) if KB.get(var, {}).get("kind") == "flags" else \
                normalize(var, after.get(var)) == normalize(var, c["value"])
            if c.get("value") is not None and not ok and var not in result["needs_restart"]:
                result["mismatches"].append({"variable": var, "wanted": c["value"], "actual": after.get(var)})
            if normalize(var, entry["old"]) != normalize(var, entry["new"]) or var in result["needs_restart"]:
                result["applied"].append(entry)
        return result
