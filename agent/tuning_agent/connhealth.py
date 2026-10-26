"""Connection health while a benchmark runs: refused connections (max_connections reached) and other failed connects,
from the server's own counters (user report, pop2 session 371: 1024 threads reconnecting every 100 transactions at
max_connections 1100 - 8.9 million 'Too many connections' and 488,000 aborted connects in 11 minutes; the 161-minute
run-length probe and the first calibration run measured the storm, and the slow run was put down to CPU steal).

Watched during warm-ups and the run-length probe, so a storm stops the run within a minute instead of being measured."""
import time

COUNTERS = ("Connections", "Aborted_connects", "Connection_errors_max_connections", "Connection_errors_accept",
            "Connection_errors_internal", "Connection_errors_select", "Connection_errors_tcpwrap", "Connection_errors_peer_address")
CHECK_S = 60


def judge(rates, max_connections=None):
    """{kind, text, ...} when connects fail noticeably, else None. kind 'refused': max_connections reached (the benchmark's
    prerequisite is too low - fixable by the agent); 'failing': other failed connects (handshake timeouts, accept / resource
    errors, clients aborting) - reported with what to check."""
    conns = rates.get("Connections") or 0
    refused = rates.get("Connection_errors_max_connections") or 0
    aborted = rates.get("Aborted_connects") or 0
    if refused >= max(1.0, 0.01 * conns):
        return {"kind": "refused", "refused_per_s": round(refused, 1), "aborted_per_s": round(aborted, 1), "connections_per_s": round(conns, 1),
                "max_connections": max_connections,
                "text": f"{refused:,.0f} connections/s refused ('Too many connections', max_connections {max_connections}) for {conns:,.0f} "
                        "accepted: the clients reconnect faster than closing connections free their slots"}
    errs = {k[len("Connection_errors_"):]: round(v, 1) for k, v in rates.items() if k.startswith("Connection_errors_") and v and k != "Connection_errors_max_connections"}
    if aborted >= max(1.0, 0.05 * conns) or sum(errs.values()) >= 1.0:
        hint = ("accept errors: the listen backlog (back_log, net.core.somaxconn) or the open-files limit" if errs.get("accept") else
                "internal errors: out of memory or threads (ulimit -u, thread_stack)" if errs.get("internal") else
                "handshakes not completed: connect_timeout, authentication failures, or clients giving up while the server is slow")
        return {"kind": "failing", "aborted_per_s": round(aborted, 1), "connections_per_s": round(conns, 1), "errors": errs,
                "text": f"{aborted:,.0f} failed connects/s for {conns:,.0f} accepted" + (f" (errors/s: {errs})" if errs else "") + f" - check {hint}"}
    return None


class Watch:
    """Rates of the connection counters since the last check (at most every CHECK_S seconds)."""

    def __init__(self, db):
        self.db = db
        self.t, self.v = time.time(), self._read()

    def _read(self):
        try:
            st = self.db.status()
        except Exception:
            return None
        return {k: float(st.get(k) or 0) for k in COUNTERS}

    def check(self, max_connections=None, min_s=CHECK_S):
        now = time.time()
        if now - self.t < min_s:
            return None
        cur = self._read()
        if cur is None or self.v is None:
            self.t, self.v = now, cur
            return None
        dt = now - self.t
        rates = {k: max(0.0, cur[k] - self.v[k]) / dt for k in COUNTERS}
        self.t, self.v = now, cur
        return judge(rates, max_connections)
