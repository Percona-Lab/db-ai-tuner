#!/bin/bash
# tuner-vbm-sata: update-only, data in memory - flush / log bound on SATA SSDs; then doublewrite files on the second
# (otherwise idle) SSD: a production-safe alternative to doublewrite OFF (#117 halved p99 with it off)
. /root/bench/bm2/common.sh
set -x
D=/root/bench/bm2
rm -f /root/bench/GO   # set only after the deploy canary passes on the current build
bash $D/prep84.sh || { echo "QUEUEFAILED: prep84"; exit 1; }
# second SSD for the doublewrite files: only if it carries no partition table / file system yet
if ! mountpoint -q /dw; then
  [ -z "$(lsblk -no FSTYPE /dev/sdb | tr -d '[:space:]')" ] && [ "$(lsblk -no NAME /dev/sdb | wc -l)" = 1 ] || { echo "QUEUEFAILED: /dev/sdb is not empty"; exit 1; }
  mkfs.ext4 -q -F /dev/sdb && mkdir -p /dw && echo "/dev/sdb /dw ext4 noatime 0 2" >> /etc/fstab && mount /dw || { echo "QUEUEFAILED: /dw"; exit 1; }
fi
chown mysql:mysql /dw
[ -f /etc/apparmor.d/usr.sbin.mysqld ] && { echo "/dw/ r, /dw/** rwk," >> /etc/apparmor.d/local/usr.sbin.mysqld; apparmor_parser -r /etc/apparmor.d/usr.sbin.mysqld; }
prepare 16 2000000
wait_go
cat > $D/post-dw.sh <<'P'
#!/bin/bash
tuning-agent experiment add --try innodb_doublewrite_dir=/dw --reason "doublewrite files on the second SSD: doublewrite writes off the data / redo device" || exit 1
tuning-agent experiment run --bench $1 || exit 1
P
chmod +x $D/post-dw.sh
export SCOPE=knobs,os CALIBRATE=3 BASELINE_ARGS="--reserve-mem 2G" STUDY="Bare metal: write-heavy on SATA SSD"
POST=$D/post-dw.sh run_session $D/update-heavy.ini "Update-only 8 GB in memory, 64 threads, flush / log bound (SATA SSD)" tps 14
echo QUEUEDONE
