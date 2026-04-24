FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends unrar-free curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] httpx jinja2 python-multipart \
    rarfile defusedxml cryptography

# Self-host HTMX + Alpine so the app has no CDN runtime dependency
RUN mkdir -p /app/static \
 && curl -sLo /app/static/htmx.min.js \
    https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js \
 && curl -sLo /app/static/alpine.min.js \
    https://unpkg.com/alpinejs@3.14.3/dist/cdn.min.js

COPY app/ /app/

# Non-root runtime user. UID 1000 matches the typical self-hosted default
# and is overridable at runtime via `docker run --user UID:GID` or a
# compose `user:` directive when the host mount owner differs (CI runners
# use UID 1001, handled via docker-compose.test.yml).
#
# /config is the expected runtime volume (db, covers, secret key, backups).
# Created empty here so its ownership is correct before a bind mount masks
# it. When a host directory is bind-mounted over /config, that directory's
# ownership takes over — hosts should ensure their /config is writable by
# the container user.
RUN useradd --uid 1000 --user-group \
      --home-dir /home/mangarr --create-home --shell /usr/sbin/nologin mangarr \
 && mkdir -p /config \
 && chown -R mangarr:mangarr /app /config

USER mangarr

# Healthcheck hits the root page — served by the app once lifespan finishes
# DB init. docker-compose.yml declares the same probe; this line ensures
# users running `docker run` directly still get it.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
