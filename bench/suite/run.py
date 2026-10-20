#!/usr/bin/env python3
"""Suite runner (local orchestrator): one VM (class S) or a server + client pair (class L) per cell, cell setup, the
tuning session, then the VMs are deleted. State in ops/suite/<run id>/<cell>.json; a failed cell keeps its VMs.

usage:
  run.py launch <run id> [cells...]     start every cell (default: the core cells) as a background process
  run.py cell <run id> <cell>           run one cell in the foreground (what launch starts)
  run.py status <run id>                one line per cell

Needs LINODE_TOKEN in the environment (never printed). Docs: docs/battery/suite-v1.md.
"""
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPS = os.path.join(ROOT, "ops")
SSH = ["ssh", "-F", os.path.join(OPS, "ssh_config"), "-o", "ConnectTimeout=15"]
VERSION = "v1"
REGION = "us-southeast"
CLASSES = {"S": ("g6-dedicated-8", None), "L": ("g6-dedicated-32", "g6-dedicated-16")}
CELLS = {  # cell -> (tier, class)
    "RO-1": ("core", "S"), "RO-2": ("core", "S"), "RO-4": ("core", "S"), "RW-1": ("core", "S"), "RW-2": ("core", "S"),
    "RW-3": ("core", "S"), "RW-4": ("core", "S"), "WI-1": ("core", "S"), "WI-3": ("core", "S"), "KV-1": ("core", "S"),
    "AN-1": ("core", "S"), "AN-2": ("core", "S"), "MX-1": ("core", "S"),
    "HC-1": ("core", "L"), "HC-2": ("core", "L"), "HC-3": ("core", "L"),
}
BENCH_FILES = ["session.sh", "queue-common.sh", "reset_datadir.sh", "tpch_warm.sh", "hdb_run.tcl", "hdb_build200.tcl", "stopall.sh"]
BENCH_DIRS = ["ibench", "benchbase", "job"]


def linode(method, path, body=None):
    req = urllib.request.Request("https://api.linode.com/v4" + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": "Bearer " + os.environ["LINODE_TOKEN"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def log(st, msg):
    line = f"{time.strftime('%H:%M:%S', time.gmtime())} {st['cell']} {msg}"
    print(line, flush=True)
    st.setdefault("log", []).append(line)
    save(st)


def save(st):
    d = os.path.join(OPS, "suite", st["run"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, st["cell"] + ".json"), "w") as f:
        json.dump(st, f, indent=1)


def ssh(alias, cmd, timeout=600, check=False):
    r = subprocess.run(SSH + [alias, cmd], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{alias}: {cmd[:80]} -> {r.returncode}: {r.stderr[-300:]}")
    return r


def create(st, role, typ):
    label = f"suite-{VERSION}-{st['cell'].lower()}-{role}-{st['run']}"[:60]
    key = open(os.path.join(OPS, "keys", "at_key.pub")).read().strip()
    d = linode("POST", "/linode/instances", {"type": typ, "region": REGION, "image": "linode/ubuntu24.04", "label": label,
                                              "tags": ["aituner", f"suite-{VERSION}"], "authorized_keys": [key],
                                              "root_pass": secrets.token_urlsafe(24), "booted": True})
    alias = f"su-{st['cell'].lower()}-{role}"
    with open(os.path.join(OPS, "ssh_config"), "a") as f:
        f.write(f"\nHost {alias}\n  HostName {d['ipv4'][0]}\n  User root\n  IdentityFile ~/aituner/ops/keys/at_key\n"
                f"  UserKnownHostsFile ~/aituner/ops/known_hosts\n  StrictHostKeyChecking accept-new\n  ServerAliveInterval 30\n")
    st.setdefault("vms", []).append({"role": role, "id": d["id"], "label": label, "ip": d["ipv4"][0], "alias": alias, "type": typ})
    log(st, f"created {label} ({typ}, {d['ipv4'][0]})")
    for _ in range(80):
        if ssh(alias, "true", timeout=30).returncode == 0:
            return alias
        time.sleep(15)
    raise RuntimeError(f"{label}: no ssh")


def delete(st):
    for vm in st.get("vms", []):
        try:
            linode("DELETE", f"/linode/instances/{vm['id']}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(st, f"delete {vm['label']} failed: {e.code}")
    time.sleep(10)
    left = {i["id"] for i in linode("GET", "/linode/instances?page_size=500")["data"]}
    gone = [vm["label"] for vm in st.get("vms", []) if vm["id"] not in left]
    st["deleted"] = gone
    log(st, f"deleted (verified by listing): {', '.join(gone) or 'none'}" + ("" if len(gone) == len(st.get("vms", [])) else " - SOME VMs REMAIN"))


def cell(run, cell_id):
    st = {"run": run, "cell": cell_id, "version": VERSION, "status": "starting", "started": time.time()}
    tier, cls = CELLS[cell_id]
    try:
        srv = create(st, "db", CLASSES[cls][0])
        cli = create(st, "cli", CLASSES[cls][1]) if CLASSES[cls][1] else None
        key = ssh("atdb", "sed -n 's/^api_key *= *//p' /etc/tuning-agent/agent.ini", check=True).stdout.strip()
        ssh(srv, "mkdir -p /root/bench /opt", check=True)
        tools = subprocess.Popen(SSH + ["atdb", "tar czf - -C /opt HammerDB-6.0 mysqlclient go-tpc"], stdout=subprocess.PIPE)
        subprocess.run(SSH + [srv, "tar xzf - -C /opt"], stdin=tools.stdout, check=True, timeout=1800)
        bench = os.path.join(ROOT, "bench")
        subprocess.run(["tar", "czf", "-", "-C", bench] + BENCH_FILES + BENCH_DIRS + ["suite"], stdout=open(f"/tmp/suite-{run}-{cell_id}.tgz", "wb"), check=True)
        subprocess.run(SSH + [srv, "tar xzf - -C /root/bench && chmod +x /root/bench/*.sh /root/bench/*/*.sh"],
                       stdin=open(f"/tmp/suite-{run}-{cell_id}.tgz", "rb"), check=True, timeout=600)
        os.remove(f"/tmp/suite-{run}-{cell_id}.tgz")
        cip = ""
        if cli:
            # the server drives the client over ssh: a key of its own, sysbench and high limits on the client
            pub = ssh(srv, "mkdir -p /root/.ssh; [ -f /root/.ssh/bench_key ] || ssh-keygen -q -t ed25519 -N '' -f /root/.ssh/bench_key; cat /root/.ssh/bench_key.pub", check=True).stdout.strip()
            ssh(cli, f"echo '{pub}' >> /root/.ssh/authorized_keys; export DEBIAN_FRONTEND=noninteractive; apt-get update -q >/dev/null; "
                     "apt-get install -yq sysbench >/dev/null; printf '* soft nofile 1048576\\n* hard nofile 1048576\\n' >> /etc/security/limits.conf; "
                     "sysctl -w net.ipv4.ip_local_port_range='1024 65000' net.core.somaxconn=8192 >/dev/null", check=True, timeout=1800)
            ssh(srv, "sysctl -w net.core.somaxconn=8192 net.ipv4.tcp_max_syn_backlog=8192 >/dev/null", check=True)
            cip = st["vms"][1]["ip"]
        study = f"Suite {VERSION} {run}"
        name = f"suite-{cell_id.lower()}-{run}"
        st["status"] = "setup"
        log(st, "setup (data load) started")
        smoke = "SMOKE=1 " if os.environ.get("SUITE_SMOKE") else ""
        ssh(srv, f"cd /root/bench && ({smoke}setsid nohup ./suite/node.sh {VERSION} {cell_id} {name} {key} '{study}' {cip} > setup.log 2>&1 < /dev/null &)", check=True)
        while True:
            time.sleep(120)
            r = ssh(srv, "grep -E '^(SETUPDONE|SETUPFAILED)' /root/bench/setup.log; grep -E 'QUEUEDONE|QUEUEFAILED' /root/bench/queue.log 2>/dev/null; "
                         "grep -hoE '^Session #[0-9]+' /root/bench/session-suite.log 2>/dev/null | tail -1", timeout=60)
            out = r.stdout
            if "SETUPFAILED" in out:
                raise RuntimeError("setup failed: " + ssh(srv, "grep -E 'SETUPFAILED' /root/bench/setup.log; tail -5 /root/bench/setup.log").stdout[-500:])
            if "SETUPDONE" in out and st["status"] == "setup":
                st["status"] = "tuning"
                log(st, "data loaded; tuning session running")
            if "Session #" in out and not st.get("session"):
                st["session"] = int(out.split("Session #")[1].split()[0])
                log(st, f"session #{st['session']}")
            if "QUEUEFAILED" in out:
                raise RuntimeError("tuning queue failed: " + ssh(srv, "tail -5 /root/bench/queue.log").stdout[-500:])
            if "QUEUEDONE" in out:
                break
        st["status"] = "done"
        st["ended"] = time.time()
        log(st, "finished")
        delete(st)
    except Exception as e:  # noqa - the VMs stay for inspection
        st["status"] = "failed"
        st["error"] = str(e)[:800]
        log(st, f"FAILED - VMs kept for inspection: {e}")
    save(st)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, run = sys.argv[1], sys.argv[2]
    if cmd == "cell":
        cell(run, sys.argv[3])
    elif cmd == "launch":
        cells = sys.argv[3:] or [c for c, (t, _) in CELLS.items() if t == "core"]
        d = os.path.join(OPS, "suite", run)
        os.makedirs(d, exist_ok=True)
        for c in cells:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "cell", run, c], stdout=open(os.path.join(d, c + ".log"), "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
            print("launched", c)
            time.sleep(5)
    elif cmd == "status":
        d = os.path.join(OPS, "suite", run)
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                st = json.load(open(os.path.join(d, f)))
                print(f"{st['cell']:<6} {st['status']:<8} session={st.get('session')} vms={len(st.get('vms', []))} "
                      f"deleted={len(st.get('deleted', []))} {(st.get('error') or '')[:100]}")


if __name__ == "__main__":
    main()
