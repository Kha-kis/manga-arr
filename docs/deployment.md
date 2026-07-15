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
    image: ghcr.io/kha-kis/manga-arr:latest
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
Mangarr's local administrator login protects the UI, but devices on that
LAN can still reach the login surface. Complete first-run setup before
changing the bind address and review the checklist below.

### 3. Internet access — only through a reverse proxy

**Do not publish Mangarr directly to the internet.** The app does not
terminate TLS and has no general request rate limiter. Browser login is
throttled, but its session and CSRF cookies cannot be `Secure` without
HTTPS.

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
    image: ghcr.io/kha-kis/manga-arr:latest
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
the browser session and CSRF cookies automatically.

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

## First-run browser authentication

Mangarr has one local administrator account for the browser UI. On first
boot it creates a one-time setup token at
`/config/.mangarr-setup-token` with mode `0600`. Read it from the
container, then open Mangarr and complete the setup form:

```bash
docker compose exec mangarr cat /config/.mangarr-setup-token
```

The setup token is removed after the administrator is created. Passwords
are hashed with Argon2id. Browser sessions are revocable server-side,
expire after seven days, and expire after 24 hours without activity.
The Security page can change the password or revoke other sessions.

Browser login does not replace API-key authentication. `/api/*` clients
should continue to use the API key from `Settings → General`; API keys do
not create browser sessions.

If the administrator password is lost, reset browser access from the
container. This deletes the administrator, revokes every browser session,
and creates a new one-time setup token without changing library data or
the API key:

```bash
docker compose exec mangarr python /app/auth_cli.py reset-admin --yes
docker compose exec mangarr cat /config/.mangarr-setup-token
```

Complete the setup form immediately after a reset. Access to Docker or the
host `/config` directory is therefore an administrator security boundary.

## Security checklist

Before pointing anything at Mangarr beyond your local machine, verify:

- [ ] **The local administrator is configured.** Complete setup with the
      one-time token before changing the default loopback bind. Confirm
      that an unauthenticated browser is redirected to `/login`.
- [ ] **`api_key` is non-empty** in the settings table. On startup,
      Mangarr auto-generates one if missing (PR #5 / H2). If you ever
      clear it manually, the middleware now fails closed — all `/api/*`
      requests return `401` until a key is regenerated. View the
      current key at `Settings → General`.
- [ ] **Browser cookies are `Secure` when served over HTTPS.** The
      session cookie is `HttpOnly` + `SameSite=Lax`; the CSRF cookie is
      `HttpOnly` + `SameSite=Strict`. Both add `Secure` when the inbound
      request has `X-Forwarded-Proto: https` or direct TLS. If you deploy
      behind a reverse proxy, make sure the proxy sets that header.
- [ ] **The `ports:` block does not publish `0.0.0.0:PORT`** unless you
      deliberately want LAN or internet access (and, for internet, you
      have a reverse proxy in front).
- [ ] **The `/config` directory is not world-readable.** It contains
      the SQLite database, the Mangarr secret-key file, and the one-time
      browser setup token until the administrator is created. The repo's
      `docker-compose.yml` maps it to `./config` on the host by default —
      check its permissions with `ls -ld ./config` (should be
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
| Library path | `MANGA_SAVE_PATH` | `/data/media/manga` |
| Download path | `MANGA_TORRENT_PATH` | `/data/torrents/manga` |
| Download category | `MANGA_CATEGORY` | `manga` |
| RSS interval | `RSS_INTERVAL` | `900` |

The public Compose file also accepts deployment-only controls through
`.env`: `MANGARR_VERSION`, `MANGARR_BIND_ADDRESS`, `MANGARR_PORT`,
`MANGARR_UID`, `MANGARR_GID`, `MANGARR_CONFIG_PATH`, and
`MANGARR_DATA_PATH`. These select the image, host exposure, runtime user,
and bind-mount sources; they are not stored in Mangarr's settings table.

Both the library and download directories live below the same `/data`
mount by default. Keep them on one host filesystem when using hardlink
imports, otherwise the hardlink operation cannot cross the filesystem
boundary. Configure indexers, download clients, notifications, Komga,
and their credentials through the UI after first boot.

`MANGARR_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or
`CRITICAL`. `MANGARR_URL_BASE` should be empty or a path prefix such as
`/mangarr`; absolute URLs are rejected.

### Outbound HTTP proxies

Mangarr's outbound HTTP clients use the standard proxy environment variables
supported by the Python HTTP stack. Set these only when your network requires
metadata, indexer, notification, or integration requests to traverse a proxy:

```env
HTTP_PROXY=http://proxy.example:8080
HTTPS_PROXY=http://proxy.example:8080
NO_PROXY=localhost,127.0.0.1,prowlarr,qbittorrent,sabnzbd,komga
```

Keep Docker service names and private in-stack integrations in `NO_PROXY` when
they should stay on the Docker network instead of traversing the proxy. This is
deployment guidance, not an in-app setting; the Settings UI remains the source
of truth for Mangarr-specific configuration.

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

Override at runtime with `.env`; `docker-compose.yml` applies these values
to the service user:

```env
MANGARR_UID=1001
MANGARR_GID=1001
```

Or from the CLI:

```bash
docker run --user "$(id -u):$(id -g)" ... mangarr
```

Whatever UID you pick, **the host-side bind mounts must be writable by
that UID**. Fix ownership if needed:

```bash
sudo chown -R "$(id -u):$(id -g)" ./config ./data
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

- `/config/manga_arr.db`
- `/config/.mangarr-secret-key`

Do not assume one can recover the other. Restoring the database without
the matching secret key leaves encrypted credentials unreadable.
Restoring the key without the matching database is harmless, but it does
not recover lost data.

If you supply `MANGARR_SECRET_KEY` from your orchestrator instead of the
file, that environment secret becomes part of the restore requirement.
Use one source consistently.

The Backup page can validate server-side backup ZIP files before a restore
window. Validation checks that the ZIP contains `manga_arr.db` and that the
embedded database opens successfully. It does not replace the live database;
stop the container before restoring the database file.

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

## Upgrading And Rollback

Use immutable version tags for normal deployments. Set the target version in
`.env` before pulling:

```env
MANGARR_VERSION=1.0.0-rc.1
```

### Before an upgrade

1. Read the target entry in `CHANGELOG.md`, especially its upgrade notes.
2. Confirm the current version on **System > Status**.
3. Create and validate a database backup from **System > Backup**.
4. Back up `/config/.mangarr-secret-key` with that database backup.
5. Record the currently deployed image tag or digest.

For the strongest rollback point, stop Mangarr and take a filesystem-level copy
of the entire host directory mounted at `/config`. A stopped copy keeps the
database, secret key, covers, and backup inventory in one consistent snapshot.

### Deploy the upgrade

```bash
docker compose pull mangarr
docker compose up -d --no-deps mangarr
docker compose ps mangarr
```

Wait for the service to report `healthy`, then verify:

- `GET /healthz` returns `{"status":"ok"}`;
- **System > Status** shows the target version;
- the library and queue load without health blockers;
- one representative metadata search and download-client connection test pass.

Database migrations run during startup. Do not interrupt the container while a
migration is active.

### Roll back

Do not point an older image at a database already migrated by a newer release.
Application code can be reverted by changing `MANGARR_VERSION`, but a complete
rollback must restore the matching pre-upgrade `/config` snapshot:

1. Stop Mangarr.
2. Preserve the failed-upgrade `/config` directory for diagnosis.
3. Restore the pre-upgrade database and its matching secret key, or restore the
   complete pre-upgrade `/config` snapshot.
4. Set `MANGARR_VERSION` back to the recorded tag or digest.
5. Start Mangarr and verify health, version, library counts, and credentials.

Never mix a restored database with a different `.mangarr-secret-key`. If the
key does not match, encrypted integration credentials remain unavailable until
the correct key is restored or each credential is re-entered.

## TL;DR

Create the host-visible directories before first boot so the non-root
container user can write them:

```bash
mkdir -p config data/media/manga data/torrents/manga
chmod 700 config
docker compose up -d
docker compose exec mangarr cat /config/.mangarr-setup-token
```

The public Compose file pulls `ghcr.io/kha-kis/manga-arr:latest`. Pin
`MANGARR_VERSION` in `.env` to deploy a specific release. Its defaults are:

```yaml
volumes:
  - "./config:/config"
  - "./data:/data"
ports:
  - "127.0.0.1:6789:8000"   # safe local-only default
```

For internet access, put a reverse proxy in front and drop the
`ports:` block from the Mangarr service entirely.
