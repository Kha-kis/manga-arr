FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends unrar-free \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] httpx jinja2 python-multipart \
    rarfile defusedxml cryptography

# HTMX + Alpine are vendored under app/static (committed to the repo with
# upstream SHA256 provenance recorded in app/static/PROVENANCE.md). No
# build-time CDN download — that previously required curl + libcurl4t64
# + libnghttp2-14 in the runtime image, dragging CVE-2026-27135 in along
# with them. The vendored copy bakes in via the COPY below.
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
