"""Copy-on-write in the data directory: a benchmark measured on it is biased.

A data set restored as a reflink / snapshot clone (cp --reflink, XFS / btrfs reflinks, ZFS clones, LVM thin snapshots,
container overlay layers) is instant, but the first write to every block copies it. Each run then starts slow and
speeds up as the copies are done: throughput rises during the window, and settings that postpone writes (less
flushing, larger redo) look better than they are. The same holds for a copy-on-write file system itself (btrfs
without nodatacow, ZFS). Evidence from the host, not from the harness's description of itself: file system type,
shared extents of the largest data files (filefrag), the device-mapper target under the data directory.
"""
import os
import re
import subprocess


def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _largest_files(datadir, n=6):
    out = []
    for root, _, files in os.walk(datadir):
        for f in files:
            if f.endswith((".ibd", "ibdata1")) or f.startswith("undo_"):
                p = os.path.join(root, f)
                try:
                    out.append((os.path.getsize(p), p))
                except OSError:
                    pass
    return [p for _, p in sorted(out, reverse=True)[:n]]


def _shared(path):
    """Shared extents of a file (reflink / snapshot clone): (shared extents, all extents)."""
    out = _run(["filefrag", "-v", path], timeout=30)
    ext = [l for l in out.splitlines() if re.match(r"^\s*\d+:\s", l)]
    return sum(1 for l in ext if "shared" in l), len(ext)


def check(datadir):
    """{"fs", "device", "issues": [text], "fs_issues": [text: the file system's own copy-on-write], "shared_files", "sampled"}"""
    res = {"fs": None, "device": None, "issues": [], "fs_issues": [], "shared_files": 0, "sampled": 0}
    if not datadir or not os.path.isdir(datadir):
        return res
    line = _run(["findmnt", "-no", "FSTYPE,SOURCE", "-T", datadir]).split()
    fs, dev = (line + [None, None])[:2]
    res["fs"], res["device"] = fs, dev
    if fs == "btrfs":
        attrs = _run(["lsattr", "-d", datadir]).split()
        if not attrs or "C" not in attrs[0]:
            res["fs_issues"].append("btrfs copy-on-write is on for the data directory: every overwrite copies the block "
                                 "(chattr +C on the empty directory before loading the data, or mount with nodatacow)")
    elif fs == "zfs":
        res["fs_issues"].append("ZFS is copy-on-write by design: overwrites allocate new blocks and results depend on its "
                             "own settings (recordsize, logbias, primarycache); compare engine settings on ext4 / xfs")
    elif fs == "overlay":
        res["fs_issues"].append("the data directory is on an overlay file system (container layer): a file is copied up on its "
                             "first write - use a volume / bind mount for the data")
    if dev and dev.startswith("/dev/mapper/"):
        table = _run(["dmsetup", "table", os.path.basename(dev)])
        if re.search(r"\b(thin|snapshot)\b", table):
            res["issues"].append(f"the data directory is on a device-mapper {'thin volume' if 'thin' in table else 'snapshot'}: "
                                 "blocks shared with a snapshot are copied on their first write")
    if fs in ("xfs", "btrfs", "bcachefs"):
        files = _largest_files(datadir)
        shared = [p for p in files if _shared(p)[0] > 0]
        res["sampled"], res["shared_files"] = len(files), len(shared)
        if shared:
            res["issues"].append(f"{len(shared)} of the {len(files)} largest data files share extents with another file "
                                 "(a reflink / snapshot clone of the pristine copy): the first write to every block copies "
                                 "it - restore with a full copy (cp --reflink=never, rsync, or a fresh load)")
    # the file-system kind (btrfs / ZFS / overlay) can be intended (copy_on_write = intended in the profile); clones and
    # device-mapper snapshots of the pristine copy are artifacts of the restore
    res["issues"] = res["fs_issues"] + res["issues"]
    return res
