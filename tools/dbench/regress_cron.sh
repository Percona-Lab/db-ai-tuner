#!/bin/bash
# Nightly decision regression check: score origin/main's rule engine on the decision corpus (offline, no model calls) in a
# temporary checkout. Report and baseline: ops/dbench/regress/main-<sha>.{md,json}; a line in ops/alerts.log only when
# the check flags a REGRESSION against the previous accepted main baseline. A commit already scored is skipped.
#   crontab: 17 3 * * * $HOME/aituner/tools/dbench/regress_cron.sh
set -u
REPO=${AITUNER_REPO:-$HOME/aituner}
OPS=$REPO/ops
OUT=$OPS/dbench/regress
mkdir -p "$OUT"
log() { echo "$(date -u +%FT%H:%MZ) $*" >> "$OUT/cron.log"; }
exec 9> "$OUT/.cron.lock"
flock -n 9 || { log "previous run still active"; exit 0; }

git -C "$REPO" fetch -q origin main 2>> "$OUT/cron.log" || { log "git fetch failed"; exit 2; }
SHA=$(git -C "$REPO" rev-parse --short origin/main)
BUILD=main-$SHA
[ -f "$OUT/$BUILD.json" ] && { log "$BUILD already scored"; exit 0; }

TMP=$(mktemp -d)
trap 'git -C "$REPO" worktree remove --force "$TMP/src" >/dev/null 2>&1; rm -rf "$TMP"' EXIT
git -C "$REPO" worktree add -q --detach "$TMP/src" "origin/main" 2>> "$OUT/cron.log" || { log "worktree failed"; exit 2; }
# the similarity term's case base, as the server has it tonight (read-only extract on the tuner server); kept if it fails
if [ -f "$TMP/src/tools/dbench/extract_cases.py" ]; then
    mkdir -p "$OPS/dbench/knn"
    scp -qF "$OPS/ssh_config" "$TMP/src/tools/dbench/extract_cases.py" "$TMP/src/tools/dbench/features.py" atweb:/tmp/ 2>> "$OUT/cron.log" \
      && ssh -F "$OPS/ssh_config" atweb "cd /tmp && sudo -n python3 extract_cases.py" > "$OPS/dbench/knn/cases.new" 2>> "$OUT/cron.log" \
      && [ -s "$OPS/dbench/knn/cases.new" ] && mv "$OPS/dbench/knn/cases.new" "$OPS/dbench/knn/cases.jsonl" || log "case base refresh failed; using the previous one"
fi
TOOL=$TMP/src/tools/dbench/regress.py
[ -f "$TOOL" ] || TOOL=$REPO/tools/dbench/regress.py   # main from before the tool existed
python3 "$TOOL" --ops "$OPS" score --agent "$TMP/src/agent" --build "$BUILD" --against 'main-*' > "$OUT/$BUILD.log" 2>&1
rc=$?
RESULT=$(grep '^RESULT: ' "$OUT/$BUILD.log" | tail -1 | cut -c9-)
log "rc=$rc ${RESULT:-no result, see $BUILD.log}"
if [ $rc -eq 1 ]; then
    echo "$(date -u +%FT%H:%MZ) REGRESS $RESULT (report: ops/dbench/regress/$BUILD.md)" >> "$OPS/alerts.log"
fi
exit $rc
