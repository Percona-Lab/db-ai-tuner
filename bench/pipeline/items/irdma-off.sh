#!/bin/bash
# irdma-off.sh: the AMD box's Intel E810 NIC (ice) pulls in the RDMA driver irdma, which reserves ~5-6 GiB that no
# process or kernel counter shows (MemTotal 30.4 GiB, ~24.9 accounted). Nothing here uses RDMA: unload it, keep it off
# after a reboot, and record the memory it gave back. Networking (ice) is not affected.
set -x
before=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
lsmod | grep -q '^irdma ' && { modprobe -r irdma || { echo "QUEUEFAILED: irdma still in use"; exit 1; }; }
echo "blacklist irdma" > /etc/modprobe.d/blacklist-irdma.conf
update-initramfs -u >/dev/null 2>&1 || true
sleep 5
after=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
echo "MemAvailable before $((before / 1024)) MiB, after $((after / 1024)) MiB, freed $(((after - before) / 1024)) MiB"
lsmod | grep -q '^irdma ' && { echo "QUEUEFAILED: irdma loaded"; exit 1; }
ping -c 2 -W 2 1.1.1.1 >/dev/null || { echo "QUEUEFAILED: network check after unloading irdma"; exit 1; }
echo PIPEOK
