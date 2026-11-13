#!/usr/bin/env sh
# Starts the PoE crafting planner web UI. Builds the database on first run.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python
if [ ! -f data/poecraft.db ]; then
  echo "First run: downloading game data from RePoE and building the database..."
  "$PY" -m poecraft update
fi
exec "$PY" -m poecraft serve "$@"
