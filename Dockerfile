# PoE 1 crafting planner - public server image. Standard library only: no pip install step.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POECRAFT_ENV=production \
    POECRAFT_HOST=0.0.0.0 \
    POECRAFT_PORT=8765 \
    POECRAFT_DATA_DIR=/data

WORKDIR /app
COPY poecraft ./poecraft
COPY config ./config
COPY docker/entrypoint.sh /usr/local/bin/poecraft-entrypoint
# strip stray Windows carriage returns (a CRLF shebang makes the container fail with 'no such file or directory')
RUN sed -i 's/\r$//' /usr/local/bin/poecraft-entrypoint

# run as an unprivileged user; /data is the volume with the game data, caches and live prices
RUN useradd --system --uid 10001 --home-dir /app poecraft \
    && mkdir -p /data && chown poecraft:poecraft /data \
    && chmod 0755 /usr/local/bin/poecraft-entrypoint
USER poecraft

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"

ENTRYPOINT ["poecraft-entrypoint"]
