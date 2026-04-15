# Deployment & Network Binding

This document covers how Mangarr listens for HTTP connections and how to
publish (or **not** publish) it to your network. TL;DR at the bottom.

## Why the container binds to `0.0.0.0`

The `Dockerfile` starts the app with:

```
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`0.0.0.0` means "listen on every network interface **inside the
container**". This is the standard pattern for containerised services
and is **not** the same as "expose on every interface of the host
machine". In Docker, the host's exposure is controlled by the
`ports:` (or `-p`) directive in your compose/run command, not by
uvicorn's `--host`.

What `0.0.0.0` inside the container actually reaches:

- The container's own loopback (`127.0.0.1` **inside** the container).
- Other containers on the same Docker network (via Docker DNS / bridge).
- The host, **only if** the host publishes the port with `ports:`.
- The wider LAN or the public internet, **only if** the host publishes
  on `0.0.0.0:PORT` or `HOST_IP:PORT`.

Binding to `127.0.0.1` inside the container would make it unreachable
from other containers too, which breaks reverse proxies. So the
in-container bind stays `0.0.0.0` and exposure is decided at the
`ports:` boundary.

## Three publishing patterns

### 1. Local access only (recommended default)

Only the machine running Docker can reach Mangarr. Nothing on the LAN
or the internet can, period.

```yaml
services:
  mangarr:
    build: .
    ports:
      - "127.0.0.1:6789:8000"   # host_ip:host_port:container_port
```

The `127.0.0.1:` prefix is the critical bit — omit it and Docker
publishes on `0.0.0.0`, which means every LAN device can reach Mangarr
directly.

This is the pattern used in the repo's `docker-compose.yml`.

### 2. LAN access via a specific host IP

If you want Mangarr reachable from other devices on your LAN:

```yaml
    ports:
      - "192.168.1.10:6789:8000"   # replace with the host's LAN IP
```

Still safer than `0.0.0.0` because it pins exposure to one interface.
You're now trusting every device on that LAN; make sure the
`api_key` setting is non-empty (see Security checklist below).

### 3. Internet access — only through a reverse proxy

**Do not publish Mangarr directly to the internet.** The app does not
terminate TLS, has no rate limiting, and its CSRF cookie can't be
`Secure` without HTTPS.

A reverse proxy (Caddy, Nginx, Traefik, …) handles TLS termination,
rate limiting, and (optionally) an extra layer of authentication. Put
Mangarr on an internal Docker network the proxy can reach, and
don't publish Mangarr's port to the host at all:

```yaml
services:
  caddy:
    image: caddy:2
    ports:
      - "443:443"
      - "80:80"
    # … TLS config, volumes …
    networks:
      - public
      - internal
  mangarr:
    build: .
    # NO ports: block — only reachable from the `internal` network
    networks:
      - internal

networks:
  public:
  internal:
    internal: true   # can't reach the outside world
```

Caddyfile snippet:

```
mangarr.example.com {
    reverse_proxy mangarr:8000
}
```

Caddy terminates TLS and forwards requests to Mangarr via the Docker
network. Mangarr sees `X-Forwarded-Proto: https` and `Secure`-flags
the CSRF cookie automatically (see PR #10 / M1).

### Not recommended: `ports: - "6789:8000"`

Without the `127.0.0.1:` prefix, Docker publishes on `0.0.0.0`. Every
LAN device can reach Mangarr directly on port 6789. There is no TLS,
no rate limiting, and no protection against a compromised device on
the same network. Prefer patterns 1 or 3.

## Security checklist

Before pointing anything at Mangarr beyond your local machine, verify:

- [ ] **`api_key` is non-empty** in the settings table. On startup,
      Mangarr auto-generates one if missing (PR #5 / H2). If you ever
      clear it manually, the middleware now fails closed — all `/api/*`
      requests return `401` until a key is regenerated. View the
      current key at `Settings → General`.
- [ ] **CSRF cookie is `Secure` when served over HTTPS.** PR #10 / M1
      added `SameSite=Strict` + `HttpOnly` unconditionally; the
      `Secure` flag is auto-added when the inbound request has
      `X-Forwarded-Proto: https` or direct TLS. If you deploy behind
      a reverse proxy, make sure the proxy sets that header.
- [ ] **The `ports:` block does not publish `0.0.0.0:PORT`** unless you
      deliberately want LAN or internet access (and, for internet, you
      have a reverse proxy in front).
- [ ] **The `/config` directory is not world-readable.** It contains
      the SQLite database, which stores download-client credentials
      (and, until future encryption work lands, stores them in
      plaintext). The repo's `docker-compose.yml` maps it to
      `~/.config/mangarr` on the host — check its permissions with
      `ls -ld ~/.config/mangarr` (should be `drwx------`, i.e. mode
      `0700`).
- [ ] **The `.env` file is in `.gitignore`** and not committed. The
      repo's `.gitignore` already excludes it; `.env.example` is the
      checked-in template.
- [ ] **Indexers and download clients that live on the LAN** (Prowlarr,
      qBittorrent, Komga, …) are accessed via the LAN-aware
      `allow_private=True` path in Mangarr's SSRF guard (PR #2 /
      PR #4). Loopback (`127.0.0.1` inside the container) is always
      blocked because it means "the Mangarr container itself."
- [ ] **A reverse proxy is fronting any internet-facing deployment.**
      See pattern 3 above.

## TL;DR

```yaml
ports:
  - "127.0.0.1:6789:8000"   # safe local-only default
```

For internet access, put a reverse proxy in front and drop the
`ports:` block from the Mangarr service entirely.
