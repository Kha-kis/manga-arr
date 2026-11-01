FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS sqlite-builder

# Canonical source generation (including Lemon), never a prebuilt amalgamation.
# Build tools and the source tree stay in this stage. See docs/sqlite-runtime.md.
ARG MANGARR_VERSION=dev
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends build-essential tcl-dev binutils ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY scripts/sqlite_runtime.py /build/scripts/sqlite_runtime.py
COPY runtime/sqlite-baseline.json /build/runtime/sqlite-baseline.json
RUN python -B /build/scripts/sqlite_runtime.py build --output /out --work /build/sqlite

FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS runtime

WORKDIR /app

# A new release must refresh OS fixes even when the pinned base is unchanged.
ARG MANGARR_VERSION=dev

RUN test "$(sed -n '/^Components:/p' /etc/apt/sources.list.d/debian.sources | sort -u)" = 'Components: main' \
 && sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && test "$(sed -n '/^Components:/p' /etc/apt/sources.list.d/debian.sources | sort -u)" = 'Components: main non-free' \
 && apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends 7zip 7zip-rar \
 && rm -rf /var/lib/apt/lists/*

# Keep the Debian package intact. Only this image's loader cache selects the
# versioned Mangarr-owned library; no process environment injection is used.
COPY --from=sqlite-builder /out/ /opt/mangarr/sqlite/3.54.1/
RUN printf '%s\n' '/opt/mangarr/sqlite/3.54.1/lib' > /etc/ld.so.conf.d/00-mangarr-sqlite.conf \
 && ldconfig \
 && python -B /opt/mangarr/sqlite/3.54.1/sqlite_runtime.py verify

# Pinned Python deps. Copied before the app source so layer caching
# only re-installs when requirements.txt actually changes — code-only
# diffs reuse the cached install layer.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && python -m pip uninstall --yes pip \
 && rm /tmp/requirements.txt

# HTMX + Alpine are vendored under app/static (committed to the repo with
# upstream SHA256 provenance recorded in app/static/PROVENANCE.md). No
# build-time CDN download — that previously required curl + libcurl4t64
# + libnghttp2-14 in the runtime image, dragging CVE-2026-27135 in along
# with them. The vendored copy bakes in via the COPY below.
COPY LICENSE /app/LICENSE
COPY app/ /app/
COPY bin/mangarr /usr/local/bin/mangarr

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
 && chmod 0755 /usr/local/bin/mangarr \
 && chown -R mangarr:mangarr /app /config

USER mangarr
# Repeat after dependencies/application installation, as the final runtime UID.
RUN python -B /opt/mangarr/sqlite/3.54.1/sqlite_runtime.py verify

# Volatile metadata is declared after dependency and source layers so changing
# build timestamps or commit identities does not invalidate those caches.
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Mangarr" \
      org.opencontainers.image.description="Self-hosted manga library manager" \
      org.opencontainers.image.source="https://github.com/Kha-kis/manga-arr" \
      org.opencontainers.image.documentation="https://github.com/Kha-kis/manga-arr/blob/master/docs/deployment.md" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${MANGARR_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONPATH=/app \
    MANGARR_UMASK=0022

ENTRYPOINT ["python", "/app/docker_entrypoint.py"]

# Healthcheck hits the unauthenticated liveness endpoint after lifespan finishes
# DB init. compose.yaml declares the same probe; this line ensures
# users running `docker run` directly still get it.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
