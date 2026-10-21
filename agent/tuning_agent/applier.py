"""Write the managed option file, apply dynamic changes, restart when needed, verify, roll back on failure."""
import os
import re
import subprocess
import time

import pymysql

from . import ostune, psprofile, resgroups
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
        if ":" in var:  # os:, rg:, client: pseudo-variables are not MySQL options
            continue
        m = managed[var]
        if m.get("reason"):
            lines.append(f"# {m.get('source', 'manual')}: {m['reason'][:200]}".replace("\n", " "))
        lines.append(var if var in OPTIONS else f"{var} = {m['value']}")
    if psprofile.VAR in managed:
        lines += psprofile.cnf_lines(managed[psprofile.VAR]["value"])
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
        self.os_allowed = str(cfg.get("allow_os_tuning", "true")).lower() == "true"
        self.fs_device = cfg.get("fs_device") if str(cfg.get("allow_fs_experiments", "false")).lower() == "true" else None

    def _os(self):
        from .hostinfo import device_for_path
        datadir = (self.db.scalar("SELECT @@datadir") or "/var/lib/mysql").rstrip("/")
        return ostune.OSApplier(datadir, device_for_path(datadir), self.service, self.log, fs_device=self.fs_device)

    def all_settings(self):
        v = self.db.variables()
        osa = self._os()
        v.update(ostune.current(osa.datadir, osa.device, self.service, self.fs_device))
        v.update(resgroups.current(self.db))
        v.update(psprofile.current(self.db))
        return v

    def write_cnf(self, managed):
        os.makedirs(os.path.dirname(self.cnf_path), exist_ok=True)
        tmp = self.cnf_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(render(managed))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.cnf_path)

    def preflush(self, max_wait=900):
        """Flush dirty pages before a restart so the shutdown does not have to (a large dirty buffer pool on slow storage
        can take longer than systemd's stop timeout, which then kills mysqld)."""
        try:
            pct = self.db.scalar("SELECT @@innodb_max_dirty_pages_pct")
            lwm = self.db.scalar("SELECT @@innodb_max_dirty_pages_pct_lwm")
            total = int(self.db.scalar("SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_buffer_pool_pages_total'") or 0)
        except Exception:
            return
        def dirty():
            return int(self.db.scalar("SELECT variable_value FROM performance_schema.global_status WHERE variable_name='Innodb_buffer_pool_pages_dirty'") or 0)
        try:
            d0 = dirty()
            if not total or d0 < max(1000, total * 0.01):
                return
            self.log(f"flushing {d0} dirty pages before the restart ...")
            self.db.execute("SET GLOBAL innodb_max_dirty_pages_pct_lwm = 0")
            self.db.execute("SET GLOBAL innodb_max_dirty_pages_pct = 0")
            t0 = time.time()
            while time.time() - t0 < max_wait and dirty() > max(1000, total * 0.01):
                time.sleep(5)
        except Exception:
            pass
        finally:
            try:  # restore (the restart applies the configured values anyway)
                self.db.execute(f"SET GLOBAL innodb_max_dirty_pages_pct = {float(pct)}")
                self.db.execute(f"SET GLOBAL innodb_max_dirty_pages_pct_lwm = {float(lwm)}")
            except Exception:
                pass

    def restart(self, timeout=1800):
        self.preflush()
        self.log(f"restarting {self.service} ...")
        self.db.close()
        try:
            r = subprocess.run(["systemctl", "restart", self.service], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:  # still shutting down / recovering: keep waiting for it below instead of failing the session
            r = None
        if r is not None and r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[-800:]
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.db.connect()
                self.db.scalar("SELECT 1")
                self.reapply_runtime()
                return True, ""
            except Exception as e:  # noqa
                last = str(e)
                time.sleep(2)
        return False, "server did not accept connections: " + last

    def reapply_runtime(self):
        """Settings that live only in the running server (resource group thread assignments)."""
        if psprofile.VAR in self.state["managed"]:  # runtime consumer settings are also in the option file; re-assert anyway
            try:
                psprofile.apply(self.db, self.state["managed"][psprofile.VAR]["value"])
            except Exception:
                pass
        if resgroups.VAR in self.state["managed"]:
            try:
                resgroups.ensure(self.db)
            except Exception as e:  # noqa
                self.log(f"resource group re-assignment failed: {e}")

    def _verify_huge_pages(self):
        """mysqld must actually use the reserved huge pages; otherwise they only take memory away from it."""
        def meminfo(k):
            for line in open("/proc/meminfo"):
                if line.startswith(k + ":"):
                    return int(line.split()[1])
            return 0
        total, free = meminfo("HugePages_Total"), meminfo("HugePages_Free")
        used = total - free
        if total and used >= total * 0.5:
            return True, ""
        return False, f"mysqld did not use the reserved huge pages ({used} of {total} in use); huge pages reverted"

    def error_log_tail(self, n=40):
        """mysqld's own error log (the reason a start failed is there, not in the systemd journal: e.g. huge-page
        allocation 'errno 13'): warnings and errors among its last lines."""
        try:
            path = (self.db.variables().get("log_error") if self.db else None) or ""
        except Exception:
            path = ""
        for p in [path] + ["/var/log/mysql/error.log", "/var/log/mysqld.log", "/var/lib/mysql/error.log"]:
            if p and p.startswith("/") and os.path.exists(p):
                try:
                    lines = open(p, errors="replace").read().splitlines()[-400:]
                except OSError:
                    continue
                keep = [l for l in lines if re.search(r"\[(ERROR|Warning)\]", l)]
                return "\n".join(keep[-n:])
        return ""

    def journal_tail(self):
        r = subprocess.run(["journalctl", "-u", self.service, "-n", "30", "--no-pager"], capture_output=True, text=True)
        return r.stdout[-3000:]

    def apply(self, changes, allow_restart=True, force_restart=False, defer_restart=False):
        old_managed = {k: dict(v) for k, v in self.state["managed"].items()}
        try:
            return self._apply(changes, allow_restart, force_restart, defer_restart)
        except Exception:
            # never leave an option file the agent's state does not describe
            self.write_cnf(old_managed)
            self.state.data["managed"] = old_managed
            self.state.save()
            raise

    def _apply(self, changes, allow_restart=True, force_restart=False, defer_restart=False):
        """changes: [{variable, value (None = unmanage), reason, source}]
        Returns dict(applied, restarted, needs_restart, mismatches, errors)."""
        before_vars = self.all_settings()
        old_managed = {k: dict(v) for k, v in self.state["managed"].items()}
        managed = {k: dict(v) for k, v in old_managed.items()}
        if any(c["variable"].startswith("os:") and c.get("value") is not None for c in changes) and not self.os_allowed:
            raise RuntimeError("OS tuning is disabled for this agent (allow_os_tuning = false)")
        result = {"applied": [], "restarted": False, "needs_restart": [], "mismatches": [], "errors": [], "cnf_path": self.cnf_path}
        for c in changes:
            var = c["variable"] if c["variable"].startswith("os:") else c["variable"].lower().replace("-", "_")
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
        osa = None
        originals = self.state.data.setdefault("os_original", {})
        applied_os = []
        for c in [c for c in changes if c["variable"].startswith("os:")]:
            var, val = c["variable"], c.get("value")
            osa = osa or self._os()
            osa.originals = originals
            if var not in originals:
                originals[var] = before_vars.get(var)
            target = originals.get(var) if val is None else val
            if target is None or normalize(var, before_vars.get(var)) == normalize(var, target):
                continue
            try:
                if osa.apply_live(var, target):
                    need_restart.append(var)
                applied_os.append(var)
            except Exception:
                for done in reversed(applied_os):  # undo this batch's live OS changes
                    try:
                        osa.apply_live(done, before_vars.get(done))
                    except Exception:
                        pass
                osa.ensure_mysqld()
                raise
        if osa or any(k.startswith("os:") for k in old_managed):
            (osa or self._os()).persist(managed)
            self.state.save()
        for c in [c for c in changes if c["variable"] == psprofile.VAR]:
            var, val = c["variable"], c.get("value")
            if var not in originals:
                originals[var] = before_vars.get(var)
            try:
                psprofile.apply(self.db, originals.get(var) or "full" if val is None else val)
            except pymysql.MySQLError as e:
                result["errors"].append(f"{var}: {e.args[-1] if e.args else e}")
        for c in [c for c in changes if c["variable"].startswith("rg:")]:
            var, val = c["variable"], c.get("value")
            if var not in originals:
                originals[var] = before_vars.get(var)
            try:
                resgroups.apply(self.db, originals.get(var) or "off" if val is None else val)
            except pymysql.MySQLError as e:
                result["errors"].append(f"{var}: {e.args[-1] if e.args else e}")
        for c in [c for c in changes if not c["variable"].startswith(("os:", "rg:", "ps:"))]:
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
        # the next step restores the data set with its own mysqld stop / start (benchmark reset): that start applies the
        # option file, so flushing and restarting now is wasted time. The new configuration is validated without starting.
        deferrable = defer_restart and not applied_os and not any(c["variable"] == "large_pages" for c in changes)
        if (need_restart or force_restart) and allow_restart and deferrable:
            r = subprocess.run(["mysqld", "--validate-config"], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                self.log("new configuration rejected by mysqld --validate-config, restoring the previous one")
                self.write_cnf(old_managed)
                result["errors"].append("invalid configuration: " + (r.stderr or r.stdout).strip()[-400:])
                result["rolled_back"] = True
                return result
            self.log("restart deferred to the data reset (configuration validated)")
            result["needs_restart"] = need_restart
            result["restart_deferred"] = True
        elif (need_restart or force_restart) and allow_restart:
            ok, err = self.restart()
            if ok and any(c["variable"] == "large_pages" and str(c.get("value")).upper() == "ON" for c in changes):
                ok, err = self._verify_huge_pages()
            if not ok:
                self.log("restart FAILED, restoring previous configuration: " + err)
                tail = self.journal_tail()
                if osa is not None:  # undo this batch's OS changes too (e.g. reserved huge pages would starve mysqld)
                    for done in reversed(applied_os):
                        try:
                            osa.apply_live(done, before_vars.get(done))
                        except Exception:
                            pass
                    try:
                        osa.persist(old_managed)
                    except Exception:
                        pass
                self.write_cnf(old_managed)
                ok2, err2 = self.restart()
                result["errors"].append("restart failed, previous configuration restored" + ("" if ok2 else f" (and restart after restore failed: {err2})"))
                result["journal"] = tail
                result["error_log"] = self.error_log_tail()
                if "errno 13" in result["error_log"] and "large_page" in result["error_log"]:
                    result["errors"].append("huge pages: mysqld was denied the huge-page mapping (errno 13) - an AppArmor / SELinux "
                                            "profile for mysqld, or the memlock limit")
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
        after = self.all_settings()
        for c in changes:
            var = c["variable"]
            if var.startswith(("os:", "rg:", "ps:")) and c.get("value") is None:
                c["value"] = originals.get(var)
            if var in OPTIONS:  # verify via the variable the option controls
                shown = after.get(OPTIONS[var])
                entry = {"variable": var, "old": c["old"] or "", "new": "" if c.get("value") is None else "ON",
                         "reason": c.get("reason", ""), "source": c.get("source", "manual"), "effect": f"{OPTIONS[var]}={shown}"}
                result["applied"].append(entry)
                continue
            entry = {"variable": var, "old": c["old"], "new": after.get(var), "reason": c.get("reason", ""), "source": c.get("source", "manual")}
            if result.get("restart_deferred") and var in result["needs_restart"]:
                entry["new"] = c["value"]  # takes effect at the reset's restart
            ok = flags_subset(c["value"], after.get(var)) if KB.get(var, {}).get("kind") == "flags" else \
                normalize(var, after.get(var)) == normalize(var, c["value"])
            if c.get("value") is not None and not ok and var not in result["needs_restart"]:
                result["mismatches"].append({"variable": var, "wanted": c["value"], "actual": after.get(var)})
            if normalize(var, entry["old"]) != normalize(var, entry["new"]) or var in result["needs_restart"]:
                result["applied"].append(entry)
        return result
