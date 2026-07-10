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

If the public URL is mounted below a path prefix, set the same prefix
as `MANGARR_URL_BASE` or in `Settings → General → URL Base`:

```env
MANGARR_URL_BASE=/mangarr
```

The reverse proxy must strip that prefix before forwarding traffic to
the container. Mangarr stores the prefix for status/API clients and
operator visibility; the simplest and best-tested deployment remains a
dedicated host or subdomain with Mangarr served at `/`.

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
      the SQLite database and the Mangarr secret-key file. The repo's
      `docker-compose.yml` maps it to `~/.config/mangarr` on the host —
      check its permissions with `ls -ld ~/.config/mangarr` (should be
      `drwx------`, i.e. mode `0700`).
- [ ] **`/config` is writable by the container user.** The image runs
      as UID 1000 by default; host bind mounts must be writable by
      that UID. See the "Container user and file ownership" section
      below for the override pattern when your host user differs.
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

## Environment overrides

Environment variables seed startup defaults. Once a value is saved in
the settings table, the database value takes precedence on later boots.
Use the UI for normal changes; use env vars for first-boot defaults and
container-managed deployment values.

Common deployment-facing overrides:

| Setting | Environment variable | Default |
| --- | --- | --- |
| Instance name | `MANGARR_INSTANCE_NAME` | `Mangarr` |
| Log level | `MANGARR_LOG_LEVEL` | `INFO` |
| URL base | `MANGARR_URL_BASE` | empty |
| Library path | `MANGA_SAVE_PATH` | `/manga` |
| Download category | `MANGA_CATEGORY` | `manga` |
| RSS interval | `RSS_INTERVAL` | `900` |

`MANGARR_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or
`CRITICAL`. `MANGARR_URL_BASE` should be empty or a path prefix such as
`/mangarr`; absolute URLs are rejected.

## Container user and file ownership

The image runs as a non-root user: **`mangarr`, UID/GID 1000**. This is
a plain user with no shell — the only thing it does is run uvicorn on
port 8000 (>1024, so no capability is needed). Running as root in a
container is a real risk: a container-escape CVE becomes instant root
on the host's namespace.

### Default behavior

If the host user creating the bind-mount directories is UID 1000 (the
default on most single-user Linux machines — `id -u` to check), you
don't need to do anything. `/config` files will be created as UID 1000
both inside the container and on the host.

### When your host user is not UID 1000

Override at runtime — `docker-compose.yml` already has the line ready
to uncomment:

```yaml
services:
  mangarr:
    # Default is uid/gid 1000. Override if your host user differs:
    # user: "${UID:-1000}:${GID:-1000}"
```

Or from the CLI:

```bash
docker run --user "$(id -u):$(id -g)" ... mangarr
```

Whatever UID you pick, **the host-side bind mounts must be writable by
that UID**. Fix ownership if needed:

```bash
sudo chown -R "$(id -u):$(id -g)" ~/.config/mangarr /data/media/manga
```

### Migrating from a pre-hardening install

If you upgraded from an image that ran as root, your `/config` files
are owned by UID 0. A new image running as UID 1000 cannot write to
them until you reassign ownership:

```bash
sudo chown -R 1000:1000 ~/.config/mangarr
```

(Or match whatever UID you set via the `user:` override.)

## Secret-key, backup, and recovery guidance

Mangarr now encrypts stored secrets at rest. That includes secrets in
the `settings` table, `indexers.api_key`, `download_clients.password`,
and secret fields inside `notification_connections.settings`.

### Master-key resolution order

On startup, Mangarr resolves the encryption key in this order:

1. `MANGARR_SECRET_KEY` environment variable.
2. `/config/.mangarr-secret-key`.
3. If neither exists, Mangarr auto-generates
   `/config/.mangarr-secret-key` on first boot and logs a warning
   telling you to back it up separately from the database.

The key file is created with mode `0600`. Mangarr never logs the key
value itself.

### What to back up

Back up these together:

- `/config/manga.db`
- `/config/.mangarr-secret-key`

Do not assume one can recover the other. Restoring the database without
the matching secret key leaves encrypted credentials unreadable.
Restoring the key without the matching database is harmless, but it does
not recover lost data.

If you supply `MANGARR_SECRET_KEY` from your orchestrator instead of the
file, that environment secret becomes part of the restore requirement.
Use one source consistently.

### Wrong-key behavior

If Mangarr starts with the wrong key, or with encrypted rows but no
usable key, it does not overwrite those rows. Instead, encrypted values
that cannot be decrypted are treated as unavailable:

- API-key-backed routes fail closed.
- External integrations behave as if the credential is blank.
- Warnings are logged naming the affected field, but not the secret.

This is a recoverable state as long as you still have the original key.

### Recovery

Recovery options:

1. Restore the correct `MANGARR_SECRET_KEY` or
   `/config/.mangarr-secret-key` and restart Mangarr.
2. If the original key is gone, re-enter the affected credentials in the
   UI. Mangarr will store the newly entered values under the currently
   active key.

If the original key is lost, previously encrypted values are not
recoverable from the database alone.

### Key rotation

Key rotation is not yet supported. There is no built-in workflow to
re-encrypt all stored secrets under a new master key. If you must change
keys today, plan on re-entering stored credentials after switching to
the new key.

## TL;DR

```yaml
ports:
  - "127.0.0.1:6789:8000"   # safe local-only default
```

For internet access, put a reverse proxy in front and drop the
`ports:` block from the Mangarr service entirely.
