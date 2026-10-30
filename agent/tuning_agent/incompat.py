"""Setting combinations a server refused, shared through the tuner server by flavor and release series (user, 2026-10-30:
session 432 kept trying resource groups with Percona Server's thread pool, error 3658). The agent reports a refusal whose
error says a setting is unsupported / incompatible, with the settings changed in the batch that made it fail; the engine
then never proposes an idea that would produce that combination again, on any host with the same release series."""
import re
import time

REFUSAL_RE = re.compile(r"unsupported|not supported|incompatible|cannot be (used|enabled|set|changed) (when|while|with)|"
                        r"conflicts? with|while .{0,40} (is )?enabled|requires? .{0,40} to be (disabled|off|enabled|on)", re.I)
REFRESH_S = 6 * 3600


def is_refusal(error):
    return bool(error) and bool(REFUSAL_RE.search(str(error)))


def _series(c):
    from .mysqldb import flavor_of
    from .knowledge import series_of
    v = c.variables or {}
    fl = flavor_of(v)[0]
    return fl, series_of(fl, v.get("version")) if v.get("version") else None


def _cond_value(var, val):
    # a resource group or OS setting fails as a kind, whatever its exact value: any active value matches
    return "*" if str(var).startswith(("rg:", "os:")) else str(val)


def report(c, var, val, error, trigger):
    """Record var=val refused because of the settings in `trigger` ([{variable, value}] changed with or just before it)."""
    others = [t for t in trigger or [] if t.get("variable") != var and t.get("value") is not None][:4]
    if not others:
        return None
    fl, series = _series(c)
    if not fl or not series:
        return None
    conds = [{"variable": var, "value": _cond_value(var, val)}] + [{"variable": t["variable"], "value": str(t["value"])} for t in others]
    entry = {"conditions": conds, "error": str(error)[:300]}
    local = [e for e in c.state.get("incompatible") or [] if e.get("conditions") != conds] + [entry]
    c.state.set("incompatible", local[-50:])
    try:
        c.client.post("/incompat", {"environment_id": c.env_id(), "flavor": fl, "series": series, "conditions": conds, "error": error})
    except Exception:
        pass
    return entry


def known(c):
    """Known refused combinations for this server's release series: the shared list (refreshed every 6 h) and this
    host's own."""
    fl, series = _series(c)
    cache = c.state.get("incompatible_shared") or {}
    if fl and series and (cache.get("series") != f"{fl}/{series}" or time.time() - (cache.get("t") or 0) > REFRESH_S):
        try:
            from urllib.parse import quote
            r = c.client.get(f"/incompat?flavor={quote(fl)}&series={quote(series)}")
            cache = {"series": f"{fl}/{series}", "t": time.time(), "items": r.get("incompatible") or []}
            c.state.set("incompatible_shared", cache)
        except Exception:
            pass
    out, seen = [], set()
    for e in (cache.get("items") or []) + (c.state.get("incompatible") or []):
        k = repr(sorted((x["variable"], str(x["value"]).lower()) for x in e.get("conditions") or []))
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _holds(cond, cfg):
    from .knowledge import normalize
    var, want = cond["variable"], cond["value"]
    have = cfg.get(var)
    if want == "*":
        return have is not None and str(have).strip().lower() not in ("", "off", "0", "none", "default")
    return have is not None and normalize(var, have) == normalize(var, want)


def blocking(entries, variables, changes):
    """The known refused combination an idea would produce (its changes on top of the running configuration), or None.
    Only when the idea touches one of the combination's settings: it is the idea's doing, not the current state's."""
    cfg = dict(variables or {})
    touched = set()
    for ch in changes or []:
        touched.add(ch["variable"])
        if ch.get("unset") or ch.get("proposed") is None:
            cfg.pop(ch["variable"], None)
        else:
            cfg[ch["variable"]] = ch["proposed"]
    for e in entries or []:
        conds = e.get("conditions") or []
        if conds and any(x["variable"] in touched for x in conds) and all(_holds(x, cfg) for x in conds):
            return e
    return None
