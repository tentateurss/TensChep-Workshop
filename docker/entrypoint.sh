#!/bin/sh
# First start: download the game data (RePoE export + the game's localized strings) and build the database into
# the /data volume. Later starts reuse it; refresh it for a new league with `docker compose run --rm app update`.
set -e

if [ "$#" -gt 0 ]; then
    # any arguments run a poecraft command instead of the server, e.g. `update`, `prices update`, `bases vaal`
    exec python -m poecraft "$@"
fi

if [ ! -f "${POECRAFT_DATA_DIR:-/data}/poecraft.db" ]; then
    echo "No game database in ${POECRAFT_DATA_DIR:-/data} yet - downloading and building it (a few minutes)..."
    python -m poecraft update
fi

if [ ! -f "${POECRAFT_DATA_DIR:-/data}/timeless/manifest.json" ]; then
    # Timeless Jewel calculator data (added later than the game database): fetched once; the evaluator works without it
    echo "No Timeless Jewel data yet - downloading it (about 35 MB)..."
    python -m poecraft timeless update || echo "Timeless Jewel data download failed - the calculator stays off until it succeeds"
fi

exec python -m poecraft serve --production --no-browser --host "${POECRAFT_HOST:-0.0.0.0}" --port "${POECRAFT_PORT:-8765}"
