#!/usr/bin/env python3
"""Study pipeline runner (local orchestrator) for the bare-metal boxes: keeps every box busy with the highest-priority
pending item it may run (bench/pipeline/pipeline.json). State in ops/pipeline/state.json, logs in ops/pipeline/<item>/.

usage:
  run.py status            one line per item
  run.py tick              collect finished items, start the next ones (one pass)
  run.py loop              tick every 5 minutes (start it detached); ops/pipeline/PAUSE stops new starts
  run.py retry <item>      put a failed item back to pending
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPS = os.path.join(ROOT, "ops")
STATE = os.path.join(OPS, "pipeline", "state.json")
FAST_FAIL_S = 20 * 60  # an item failing this soon after its start holds the other items of the same script
SSH = ["ssh", "-F", os.path.join(OPS, "ssh_config"), "-o", "ConnectTimeout=20", "-o", "BatchMode=yes"]
SYNC = ["bench/pipeline", "bench/bm2", "bench/queue-common.sh", "bench/session.sh", "bench/reset_datadir.sh"]


def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + " " + msg
    print(line)
    with open(os.path.join(OPS, "pipeline", "pipeline.log"), "a") as f:
        f.write(line + "\n")


def load():
    items = json.load(open(os.path.join(ROOT, "bench", "pipeline", "pipeline.json")))["items"]
    try:
        st = json.load(open(STATE))
    except (OSError, ValueError):
        st = {}
    return items, st


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def ssh(host, cmd, timeout=120):
    r = subprocess.run(SSH + [host, cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout


def start(it, hosts, st):
    for h in hosts:
        subprocess.run(["rsync", "-a", "-e", " ".join(SSH)] + [os.path.join(ROOT, p) for p in SYNC] + [f"{h}:/root/bench/"],
                       check=True, capture_output=True, timeout=300)
        args = " ".join(it.get("args") or [])
        # the pid file (not pgrep -f) tells a running item from a dead one
        # the whole background job in a subshell with its output closed: otherwise ssh waits for it to finish
        cmd = (f"mkdir -p /root/bench/pipeline/logs && cd /root/bench && (PIPE_ITEM={it['id']} setsid nohup bash -c "
               f"'echo $$ > pipeline/logs/{it['id']}.pid; bash pipeline/items/{it['script']} {args} > pipeline/logs/{it['id']}.log 2>&1; "
               f"echo PIPEEXIT $? >> pipeline/logs/{it['id']}.log' > /dev/null 2>&1 < /dev/null &)")
        rc, _ = ssh(h, cmd)
        if rc != 0:
            raise RuntimeError(f"start {it['id']} on {h} failed")
    prev = st.get(it["id"]) if (st.get(it["id"]) or {}).get("status") == "running" else None
    st[it["id"]] = {"status": "running", "hosts": sorted(set(hosts) | set((prev or {}).get("hosts") or [])),
                    "started": (prev or {}).get("started") or time.time()}
    log(f"started {it['id']} on {', '.join(hosts)}")


def check(iid, s):
    """Per host: 'ok', 'failed', 'running' or 'lost' (no exit marker and the process is gone)."""
    out = {}
    for h in s["hosts"]:
        try:
            rc, txt = ssh(h, f"tail -3 /root/bench/pipeline/logs/{iid}.log 2>/dev/null; "
                             f"kill -0 $(cat /root/bench/pipeline/logs/{iid}.pid 2>/dev/null) 2>/dev/null && echo ALIVE; "
                             f"grep -c '^PIPEOK' /root/bench/pipeline/logs/{iid}.log 2>/dev/null")
        except subprocess.TimeoutExpired:
            out[h] = "running"
            continue
        if rc != 0 and not txt:
            out[h] = "running"  # unreachable: decide later
        elif "PIPEEXIT" in txt:
            ok = "PIPEEXIT 0" in txt and txt.strip().splitlines()[-1].strip() not in ("0", "")
            out[h] = "ok" if ok else "failed"
        else:
            out[h] = "running" if "ALIVE" in txt else "lost"
    return out


def collect(iid, s):
    d = os.path.join(OPS, "pipeline", iid)
    os.makedirs(d, exist_ok=True)
    for h in s["hosts"]:
        subprocess.run(["rsync", "-a", "-e", " ".join(SSH), f"{h}:/root/bench/pipeline/logs/{iid}.log", os.path.join(d, f"{h}.log")],
                       capture_output=True, timeout=300)
        # the session logs of this item (written by run_session next to the profiles)
        subprocess.run(["rsync", "-a", "-e", " ".join(SSH), "--include=session-pipe-*.log", "--exclude=*",
                        f"{h}:/root/bench/", os.path.join(d, h) + "/"], capture_output=True, timeout=300)


def tick():
    items, st = load()
    busy = set()
    for iid, s in list(st.items()):
        if iid.startswith("_") or s["status"] != "running":
            continue
        res = check(iid, s)
        if all(v in ("ok", "failed", "lost") for v in res.values()):
            collect(iid, s)
            s["status"] = "done" if all(v == "ok" for v in res.values()) else "failed"
            s["ended"], s["per_host"] = time.time(), res
            log(f"{iid}: {s['status']} {res}")
            script = next((i.get("script") for i in items if i["id"] == iid), None)
            if s["status"] == "failed" and script and s["ended"] - s["started"] < FAST_FAIL_S:
                # failed within minutes: most likely the script or the box, not the study - hold the other items using the
                # same script until it is looked at (`retry` releases them), instead of failing them one after another
                st.setdefault("_held", {})[script] = iid
                log(f"HOLD: items using {script} held after {iid} failed in {int((s['ended'] - s['started']) // 60)} min")
        else:
            busy |= set(s["hosts"])
    if os.path.exists(os.path.join(OPS, "pipeline", "PAUSE")):
        save(st)
        return
    free = {h for it in items for h in it["hosts"]} - busy
    for it in items:
        if not it.get("script") or st.get(it["id"], {}).get("status") in ("running", "done", "failed"):
            continue
        if it["script"] in st.get("_held", {}):
            continue
        cand = [h for h in it["hosts"] if h in free]
        if it.get("group"):
            if len(cand) == len(it["hosts"]):
                start(it, it["hosts"], st)
            free -= set(cand)  # held for the group item: nothing lower may take them
        elif cand:
            start(it, [cand[0]], st)
            free.discard(cand[0])
    save(st)


def status():
    items, st = load()
    for it in items:
        s = st.get(it["id"], {})
        if not s and it.get("script") in st.get("_held", {}):
            s = {"status": "held"}
        when = time.strftime("%m-%d %H:%M", time.gmtime(s["started"])) if s.get("started") else ""
        print(f"{it['id']:22} {s.get('status', 'pending' if it.get('script') else 'todo'):8} {','.join(s.get('hosts') or []):26} "
              f"{when:11} ~{it['est_hours']}h  {it['question'][:70]}")


if __name__ == "__main__":
    os.makedirs(os.path.join(OPS, "pipeline"), exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "tick":
        tick()
    elif cmd == "loop":
        while True:
            try:
                tick()
            except Exception as e:  # never die on one bad pass (network, a box rebooting)
                log(f"tick failed: {e}")
            time.sleep(300)
    elif cmd == "retry":
        items, st = load()
        st.pop(sys.argv[2], None)
        held = st.get("_held", {})
        for scr in [k for k, v in held.items() if v == sys.argv[2]]:
            held.pop(scr)  # the item that caused the hold is being retried: release the others
        save(st)
        print("pending:", sys.argv[2])
    else:
        print(__doc__)
