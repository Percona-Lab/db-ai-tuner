"""Filesystem experiments on a dedicated block device (opt-in: agent `fs_device` + `allow_fs_experiments`, session scope 'filesystem').

The data directory is moved onto the experiment device formatted with the candidate filesystem and options, the pristine
data set is restored onto it, and mysqld is started. Value syntax for the pseudo-knob os:datadir_fs:

    rootfs                                   original data directory on the root disk (no experiment device)
    ext4[:mount,options]                     e.g. ext4:noatime,commit=60
    xfs[:mount,options]                      e.g. xfs:noatime,logbsize=256k
    btrfs[:mount,options]                    e.g. btrfs:noatime,nodatacow
    zfs[:prop=value,...]                     e.g. zfs:recordsize=16k,compression=lz4,atime=off,logbias=throughput
"""
import os
import shutil
import subprocess

DATADIR = "/var/lib/mysql"
BASE = "/var/lib/mysql_base"
POOL = "tafslab"
PACKAGES = {"xfs": "xfsprogs", "btrfs": "btrfs-progs", "zfs": "zfsutils-linux"}
CANDIDATES = [  # (value, why) - tried in this order when the session scope includes 'filesystem'
    ("xfs:noatime", "XFS allocation groups allow more parallel metadata/IO; common recommendation for InnoDB."),
    ("ext4:noatime,commit=60", "Longer journal commit interval: fewer metadata journal writes (InnoDB fsyncs its own files)."),
    ("zfs:recordsize=16k,compression=lz4,atime=off,logbias=throughput,primarycache=metadata",
     "ZFS with 16K records matching InnoDB pages, lz4 compression and metadata-only ARC (InnoDB caches data)."),
    ("btrfs:noatime,nodatacow", "btrfs without copy-on-write for data files."),
]


def _run(cmd, check=True, timeout=1800):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def _mount_source(path):
    for line in open("/proc/mounts"):
        p = line.split()
        if p[1] == path:
            return p[0], p[2], p[3]
    return None


def current(device):
    """Current os:datadir_fs value, or None when no experiment device is configured."""
    if not device:
        return None
    m = _mount_source(DATADIR)
    if not m:
        return "rootfs"
    src, fstype, opts = m
    if fstype == "zfs":
        props = _run(["zfs", "get", "-H", "-o", "property,value", "recordsize,compression,atime,logbias,primarycache,sync", src], check=False).stdout
        kv = dict(line.split("\t") for line in props.strip().splitlines() if "\t" in line)
        return "zfs:" + ",".join(f"{k}={kv[k]}" for k in ("recordsize", "compression", "atime", "logbias", "primarycache") if k in kv)
    real = os.path.realpath(device)
    if os.path.realpath(src) != real:
        return f"other:{fstype}"
    keep = [o for o in opts.split(",") if o in ("noatime", "relatime", "lazytime", "nodatacow", "nobarrier", "barrier=0")
            or o.startswith(("commit=", "logbsize=", "data=", "allocsize="))]
    return fstype + (":" + ",".join(keep) if keep else "")


def _teardown(device):
    if _mount_source(DATADIR):
        _run(["umount", DATADIR])
    if shutil.which("zpool") and _run(["zpool", "list", POOL], check=False).returncode == 0:
        _run(["zpool", "destroy", "-f", POOL])
    _run(["wipefs", "-a", device], check=False)


def apply(value, device, service, log=print):
    """Move the data directory to the requested filesystem. Stops and starts mysqld itself."""
    fstype, _, opts = value.partition(":")
    if fstype != "rootfs" and fstype not in ("ext4", "xfs", "btrfs", "zfs"):
        raise RuntimeError(f"unsupported filesystem {fstype}")
    if fstype in PACKAGES and not shutil.which({"xfs": "mkfs.xfs", "btrfs": "mkfs.btrfs", "zfs": "zpool"}[fstype]):
        log(f"installing {PACKAGES[fstype]} ...")
        _run(["apt-get", "install", "-yq", PACKAGES[fstype]], timeout=900)
    log(f"filesystem experiment: moving the data directory to {value}")
    _run(["systemctl", "stop", service])
    try:
        if not os.path.isdir(BASE) and not _mount_source(DATADIR):  # first move: keep a pristine copy of the original datadir
            log(f"saving the original data directory to {BASE} ...")
            _run(["rsync", "-a", DATADIR + "/", BASE + "/"], timeout=7200)
        if not os.path.isdir(BASE):
            raise RuntimeError(f"no pristine copy at {BASE}")
        _teardown(device)
        if fstype == "rootfs":
            pass
        elif fstype == "zfs":
            props = [x for x in opts.split(",") if "=" in x]
            _run(["zpool", "create", "-f", "-o", "ashift=12", "-O", "mountpoint=none", POOL, device])
            cmd = ["zfs", "create", "-o", f"mountpoint={DATADIR}"]
            for p in props:
                cmd += ["-o", p]
            _run(cmd + [f"{POOL}/data"])
        else:
            mk = {"ext4": ["mkfs.ext4", "-F", "-q", "-E", "lazy_itable_init=0,lazy_journal_init=0"],
                  "xfs": ["mkfs.xfs", "-f", "-q"], "btrfs": ["mkfs.btrfs", "-f", "-q"]}[fstype]
            _run(mk + [device], timeout=1800)
            os.makedirs(DATADIR, exist_ok=True)
            _run(["mount", "-o", opts or "defaults", device, DATADIR])
        log("restoring the pristine data set ...")
        _run(["rsync", "-a", "--delete", BASE + "/", DATADIR + "/"], timeout=7200)
        _run(["chown", "-R", "mysql:mysql", DATADIR])
        os.chmod(DATADIR, 0o750)
    finally:
        _run(["systemctl", "start", service], check=False)
    return False  # mysqld already restarted


def tradeoff(value):
    v = str(value or "")
    if any(x in v for x in ("barrier=0", "nobarrier", "data=writeback", "sync=disabled")):
        return "Filesystem option that lets the device or kernel acknowledge writes before they are durable."
    return None
