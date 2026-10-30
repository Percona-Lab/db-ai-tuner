#!/bin/bash
# confirm-post.sh <profile>: in the session the calibration opened, the source session's best configurations measured
# against its start configuration in interleaved rounds; the result is recorded on both sessions (tuning-agent confirm).
PROF=$1
set -x
tuning-agent confirm --source "$CONFIRM_SESSION" --bench "$PROF" --pairs "${CONFIRM_PAIRS:-2}" || exit 1
