# Benchmark harness: best practices

The tuner compares configurations by measuring them. Anything in the harness that makes one run differ from another
for reasons other than the configuration ends up in the result: as noise that hides real differences, or as a bias
that favours the wrong settings. The agent checks what it can on the host and warns; these rules keep the harness
from creating the problem in the first place.

## Restore the data set with a full copy

Writing benchmarks change their data, so each run starts from a pristine copy (`reset = ...` in the profile; the
agent restarts mysqld and drops the OS page cache for read-only benchmarks).

* **Copy every block.** Use `rsync -a --delete base/ datadir/`, `cp -a --reflink=never`, or reload the data. Do not
  use reflink or snapshot clones: `cp --reflink`, XFS / btrfs reflinks, ZFS clones, LVM thin snapshots, cloud
  volume snapshots, container overlay layers. A clone is instant, but the first write to every block copies it: each
  run starts slow and speeds up as the copies are done. Throughput rises during the window, and settings that
  postpone writes (less flushing, larger redo) look better than they are.
* **Keep the pristine copy as a plain directory** on the same kind of storage, not as a snapshot of the data volume.
* **Stop mysqld before the copy and start it after**; skip the shutdown flush when the data is replaced anyway
  (`SET GLOBAL innodb_fast_shutdown = 2`), see `bench/reset_datadir.sh`.

## No copy-on-write under the data

* **btrfs:** `chattr +C` on the empty data directory before loading the data (or mount with `nodatacow`).
* **ZFS:** copy-on-write by design, with its own tuning (recordsize, logbias, primarycache). Compare engine settings
  on ext4 or xfs, and tune ZFS separately.
* **Containers:** put the data on a volume / bind mount, never in the container's overlay layer.

### Measuring a copy-on-write setup on purpose

Copy-on-write file systems are a production choice (snapshots, compression, checksums), and their cost for a workload
is worth knowing. Declare it in the profile and the agent records it as a note on each run instead of a warning or a
noise source:

```ini
[bench]
copy_on_write = intended
```

Results then include that cost, by design; for the most stable comparison of engine settings, measure on a file
system without copy-on-write. A reflink / snapshot clone made by the data restore stays an artifact either way: the
data is still restored with a full copy. To compare file systems directly, use the `filesystem` scope on a dedicated
device (`tuning-agent init --fs-device ... --allow-fs-experiments`).

## Storage that warms up

* **Cloud volumes restored from a snapshot** load blocks lazily on first access: read the whole volume once
  (`fio --rw=read --bs=1M` over the device) before the first run.
* **Thin-provisioned volumes** allocate on first write: load the data once and reset with a full copy within the
  allocated space.
* **SSD maintenance:** `fstrim` and similar timers running during a measurement show up as storage stalls; the agent
  lists active timers at session start. Disable them for the study.

## Same conditions for every run

* Same run length, same warm-up, same client threads; a warm-up long enough to reach a steady state (the agent
  checks the window and lengthens the warm-up when deferred work is still growing).
* The benchmark client must not saturate its own CPU (the agent flags a saturated client); for many connections put
  it on a separate host.
* Keep other work off the host (backups, package updates, other benchmarks).

## What the agent checks

| Check | When | Effect |
|---|---|---|
| Reflink / snapshot clone: shared extents of the largest data files (`filefrag`) | after each data reset | warning, event, calibration disturbance |
| Copy-on-write file system (btrfs without `+C`, ZFS, overlay) and device-mapper thin / snapshot targets | after each data reset | warning, event, calibration disturbance; a note only with `copy_on_write = intended` |
| Throughput rising or deferred work growing in the window | every run | run marked not steady; warm-up lengthened in calibration |
| Swap with measured cost, CPU steal, storage latency drift, in-run stalls, client saturation | every run / calibration | flags; calibration stops in an unfit environment |
| Scheduled timers (fstrim, apt, logrotate) | session start | warning |
