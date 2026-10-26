#!/bin/bash
# Nightly copy of the tuner's databases (tuner.db: studies, sessions, runs; learning.db: the learner) from tuner-web to this
# VM, next to Linode's own backups of tuner-web (enabled 2026-10-26). VACUUM INTO: one read transaction, consistent while the
# app writes (the .backup command restarted on every write and never finished on the 3.9 GB tuner.db), at low CPU / I/O priority;
# compressed, the last KEEP days kept.   crontab: 41 2 * * * $HOME/aituner/tools/db_backup.sh
set -u
REPO=${AITUNER_REPO:-$HOME/aituner}
OPS=$REPO/ops
OUT=$OPS/backups
KEEP=${KEEP:-7}
mkdir -p "$OUT"
log() { echo "$(date -u +%FT%H:%MZ) $*" >> "$OUT/backup.log"; }
exec 9> "$OUT/.lock"
flock -n 9 || { log "previous backup still running"; exit 0; }
STAMP=$(date -u +%Y%m%dT%H%M)
ok=1
for db in tuner learning; do
  if ssh -F "$OPS/ssh_config" atweb "sudo -n rm -f /tmp/$db-backup.db /tmp/$db-backup.db.gz && sudo -n nice -n 10 ionice -c3 sqlite3 /var/lib/aituner/$db.db \"VACUUM INTO '/tmp/$db-backup.db'\" && sudo -n nice -n 10 gzip -1 /tmp/$db-backup.db && sudo -n chmod 644 /tmp/$db-backup.db.gz" \
     && scp -qF "$OPS/ssh_config" "atweb:/tmp/$db-backup.db.gz" "$OUT/$db-$STAMP.db.gz.part" \
     && gzip -t "$OUT/$db-$STAMP.db.gz.part" && mv "$OUT/$db-$STAMP.db.gz.part" "$OUT/$db-$STAMP.db.gz"; then
    log "$db: $(du -h "$OUT/$db-$STAMP.db.gz" | cut -f1)"
  else
    ok=0
    rm -f "$OUT/$db-$STAMP.db.gz.part"
    log "$db: FAILED"
    echo "$(date -u +%FT%H:%MZ) BACKUP of $db.db failed (see ops/backups/backup.log)" >> "$OPS/alerts.log"
  fi
  ssh -F "$OPS/ssh_config" atweb "sudo -n rm -f /tmp/$db-backup.db /tmp/$db-backup.db.gz" 2>/dev/null
  ls -1t "$OUT"/$db-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
done
[ $ok = 1 ]
