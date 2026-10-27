#!/bin/bash
# serverwatch_live.sh [cases]: the agent's crash / hang detection (serverwatch, 2026-10-27) against a real mysqld - until
# then it was tested only with simulated ones. Cases (default 123), each through the agent's own code, in a session of
# their own so the incidents show on its page:
#   1. crash while running: SIGSEGV to mysqld (its handler writes "got signal 11"; systemd restarts it)
#   2. hang while running: SIGSTOP - no answer to SELECT 1 for 10 min -> evidence, SIGKILL, systemd restarts it
#   3. hung shutdown: every mysqld thread held in a ptrace stop, then the agent's restart -> 'deactivating' without I/O
#      -> evidence, kill, the restart reports the hang. (Not SIGSTOP: systemd sends SIGCONT with SIGTERM, so a stopped
#      service wakes up and shuts down normally - the first live run, 2026-10-27. SIGCONT does not resume a traced thread.)
# Each case must produce one incident of the expected kind and phase, with an evidence directory, and leave mysqld
# running. ~25 min for all three. No data is changed (no benchmark runs).
CASES=${1:-123}
. /root/bench/pipeline/lib.sh
set -x
ensure_ps84
curl -fsSL https://50-116-44-237.sslip.io/install.sh | bash >/dev/null && systemctl restart tuning-agent
tuning-agent session stop >/dev/null 2>&1 || true
tuning-agent session start --goal tps --policy benchmark --ai off --name "serverwatch live test ($(hostname))" \
  --workload "no benchmark: mysqld crashed / hung on purpose (SIGSEGV, SIGSTOP, ptrace stop) to check the agent's detection" || exit 1
CASES=$CASES /opt/tuning-agent/venv/bin/python3 - <<'PY'
import os, signal, subprocess, sys, time
from tuning_agent import cli, serverwatch
from tuning_agent.applier import Applier

c = cli.Ctx()
svc = cli._service(c)
cli._watch_setup(c, "serverwatch live test")
fails = []

TRACER = r"""
import ctypes, os, sys, time
libc = ctypes.CDLL(None, use_errno=True)
PTRACE_SEIZE, PTRACE_INTERRUPT = 0x4206, 0x4207
pid = int(sys.argv[1])
held = set()
for _ in range(5):  # threads started meanwhile too
    for t in os.listdir(f"/proc/{pid}/task"):
        tid = int(t)
        if tid not in held and libc.ptrace(PTRACE_SEIZE, tid, None, None) == 0:
            libc.ptrace(PTRACE_INTERRUPT, tid, None, None)
            held.add(tid)
    time.sleep(0.2)
print(f"holding {len(held)} threads of {pid}", flush=True)
while os.path.exists(f"/proc/{pid}"):  # the hold ends when the tracer exits: stay until mysqld is gone
    try:
        os.waitpid(-1, os.WNOHANG | 0x40000000)  # __WALL: reap the tracees' stop notifications
    except ChildProcessError:
        pass
    time.sleep(1)
"""


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
    time.sleep(30)


def crash_while_running():
    with serverwatch.watching(svc) as w:
        p0 = pid()
        # no core file: LimitCORE=infinity would dump the whole buffer pool to disk
        subprocess.run(["prlimit", "--pid", str(p0), "--core=0"], check=True)
        os.kill(p0, signal.SIGSEGV)
        t = time.time()
        while not w.incident and time.time() - t < 180:
            time.sleep(5)
        check("crash while running", w.incident, "crash", "running")


def hang_while_running():
    with serverwatch.watching(svc) as w:
        os.kill(pid(), signal.SIGSTOP)
        t = time.time()
        while not w.incident and time.time() - t < serverwatch.UNRESPONSIVE_S + 600:
            time.sleep(10)
        if not w.incident:
            os.kill(pid(), signal.SIGCONT)
        check("hang while running", w.incident, "hang", "running")


def hung_shutdown():
    tr = subprocess.Popen([sys.executable, "-c", TRACER, str(pid())])
    time.sleep(5)
    ap = Applier(c.cfg, c.state, c.db, log=print)
    ap.preflush = lambda *a, **k: None  # the server is held: nothing to flush, and no connection
    seen = []
    orig = serverwatch.Watch._found

    def spy(self, *a, **k):
        s = orig(self, *a, **k)
        seen.append(s)
        return s
    serverwatch.Watch._found = spy
    try:
        ok, err = ap.restart(timeout=1800)
    finally:
        serverwatch.Watch._found = orig
        if tr.poll() is None:
            tr.kill()
    print(f"   restart returned ok={ok}: {err}", flush=True)
    check("hung shutdown", seen[0] if seen else None, "hang", "stopping")
    if ok:
        fails.append("hung shutdown: restart reported success")


cases = os.environ.get("CASES") or "123"
for n, fn in (("1", crash_while_running), ("2", hang_while_running), ("3", hung_shutdown)):
    if n in cases:
        fn()
print("== serverwatch live test: " + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)), flush=True)
sys.exit(1 if fails else 0)
PY
rc=$?
tuning-agent session stop >/dev/null 2>&1 || true
[ $rc = 0 ] && echo PIPEOK
exit $rc
