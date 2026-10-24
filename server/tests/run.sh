#!/bin/bash
# Server tests (API and background processes) on temporary databases; exit code = the result.
# usage: server/tests/run.sh [pytest args]     PY=/opt/aituner/venv/bin/python server/tests/run.sh   (default: python3)
# Needs pytest and httpx next to the server's requirements. Writes nothing into the tree (no bytecode, no pytest cache);
# the databases live in a temporary directory removed at exit (conftest.py).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-python3}
cd "$HERE/.."
PYTHONDONTWRITEBYTECODE=1 exec "$PY" -m pytest -q -p no:cacheprovider -W ignore::DeprecationWarning --rootdir "$HERE" "$@" "$HERE"
