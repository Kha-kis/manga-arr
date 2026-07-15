# Deployment And Recovery

Mangarr is distributed as a multi-platform container image for
`linux/amd64` and `linux/arm64`. This guide covers installation, network
exposure, permissions, backups, upgrades, rollback, and administrator
recovery.

## Docker Compose

Create a directory for Mangarr and place this in `compose.yaml`:

```yaml
services:
  mangarr:
    image: ghcr.io/kha-kis/manga-arr:latest
    user: "1000:1000"
    environment:
      TZ: Etc/UTC
      MANGARR_UMASK: "0022"
      MANGARR_LIBRARY_PATH: /data/media/manga
      MANGARR_DOWNLOAD_PATH: /data/torrents/manga
      MANGARR_CATEGORY: manga
      MANGARR_RSS_INTERVAL: "900"
    volumes:
      - ./config:/config
      - ./data:/data
    ports:
      - "6789:8000"
    restart: unless-stopped
    init: true
    security_opt:
      - no-new-privileges=true
    cap_drop:
      - ALL
```

Edit the YAML directly for your host paths, UID/GID, timezone, or port. Indexer,
download-client, metadata, notification, and API credentials belong in the
authenticated Mangarr settings UI, where supported secrets are encrypted.

Create the host directories and start the container:

```bash
mkdir -p config data/media/manga data/torrents/manga
chmod 700 config
docker compose up -d
docker compose ps
```

Open `http://<server-ip>:6789`. The first browser visit redirects to
**Create administrator**. Choose the username and password there; no bootstrap
credential or container command is required. The first successful setup request
becomes the administrator, so complete setup before exposing Mangarr outside a
trusted network.

## Configuration Reference

The values most operators change are standard Compose fields:

| Setting | Default | Change it by |
| --- | --- | --- |
| Image channel | `ghcr.io/kha-kis/manga-arr:latest` | Editing `image:` |
| Runtime identity | `1000:1000` | Editing `user:` |
| Created-file permissions | `0022` | Editing `environment.MANGARR_UMASK` |
| Timezone | `Etc/UTC` | Editing `environment.TZ` |
| Web port | Host `6789`, container `8000` | Editing `ports:` |
| Application state | `./config:/config` | Editing the host side of `volumes:` |
| Shared media/download data | `./data:/data` | Editing the host side of `volumes:` |

Useful optional container environment values include:

| Variable | Purpose |
| --- | --- |
| `MANGARR_INSTANCE_NAME` | Name shown in the browser UI |
| `MANGARR_LOG_LEVEL` | Runtime log level, normally `INFO` |
| `MANGARR_URL_BASE` | Path prefix when Mangarr is served below a domain path |
| `MANGARR_LIBRARY_PATH` | Library path visible inside the container |
| `MANGARR_DOWNLOAD_PATH` | Completed-download path visible inside the container |
| `MANGARR_CATEGORY` | Download-client category |
| `MANGARR_RSS_INTERVAL` | RSS polling interval in seconds |
| `MANGARR_UMASK` | Octal process umask applied before Mangarr starts |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | Standard outbound proxy controls |

Add optional values directly under `environment:`. For example:

```yaml
environment:
  TZ: America/Chicago
  MANGARR_INSTANCE_NAME: Manga Library
  HTTP_PROXY: http://proxy.example:8080
  HTTPS_PROXY: http://proxy.example:8080
  NO_PROXY: localhost,127.0.0.1,prowlarr,qbittorrent,sabnzbd,komga
```

Mangarr continues to accept the pre-1.2 names `MANGA_SAVE_PATH`,
`MANGA_TORRENT_PATH`, `MANGA_CATEGORY`, and `RSS_INTERVAL`. The canonical
`MANGARR_*` name wins when both forms are present, so existing installations
can migrate without a flag day.

## Container User And File Ownership

The image runs without root privileges. The Compose `user:` value must match an
account that can write the host bind mounts. Check the intended host account:

```bash
id -u
id -g
```

If both return `1001`, edit the Compose service and fix ownership before start:

```yaml
user: "1001:1001"
```

```bash
sudo chown -R 1001:1001 config data
chmod 700 config
```

Do not solve permission failures by running the container as root or making
`/config` world-writable.

`MANGARR_UMASK` controls permissions for files and directories Mangarr creates.
Use `0002` when the container UID shares a writable media group and imported
content should remain group-writable. Hardlinked files retain the source
inode's permissions, so configure the download client with a compatible umask
as well.

## Volume Layout

Mangarr and the download client must agree on the paths reported for completed
downloads. A shared `/data` mount keeps hardlinks and atomic moves available:

```text
data/
├── media/
│   └── manga/
└── torrents/
    └── manga/
```

Use the same container-side paths in Mangarr and the download client whenever
possible. Remote path mappings are a compatibility tool, not a substitute for a
coherent shared mount.

## Network Exposure

The example publishes `6789:8000`, making Mangarr reachable from the host and
LAN. Browser authentication is mandatory, but the application should not be
published directly to the internet.

### Host-only access

For a workstation installation or a reverse proxy running on the same host,
bind only to loopback:

```yaml
ports:
  - "127.0.0.1:6789:8000"
```

### Different LAN port

Change only the host side of the mapping:

```yaml
ports:
  - "6790:8000"
```

### HTTPS reverse proxy

Complete first-run administrator setup before attaching a public hostname.
Terminate TLS at a reverse proxy and forward to Mangarr. A minimal Caddy
configuration for a proxy running on the Docker host is:

```caddyfile
mangarr.example.com {
    reverse_proxy 127.0.0.1:6789
}
```

Use the host-only port mapping with this pattern. Mangarr honors
`X-Forwarded-Proto: https` and marks browser and CSRF cookies `Secure`. Browser
session cookies are `HttpOnly` with `SameSite=Lax`; CSRF cookies are `HttpOnly`
with `SameSite=Strict`.

Serving Mangarr at a dedicated host or subdomain is the simplest option. If a
path prefix is unavoidable, set it directly in Compose and configure the proxy
to strip the prefix before forwarding:

```yaml
environment:
  MANGARR_URL_BASE: /mangarr
```

## First-Run Authentication

Mangarr supports one local browser administrator. Before an administrator
exists, every browser page redirects to `/setup`. The setup form creates the
account atomically, hashes its password with Argon2id, starts a server-side
session, and redirects into the application.

After setup:

- browser sessions expire after seven days and after 24 hours of inactivity;
- password changes revoke other sessions;
- **Settings > Security** can revoke other browser sessions;
- API clients continue to use the separate key from **Settings > General**.

If the administrator password is lost, reset browser access from the container:

```bash
docker compose exec mangarr mangarr admin reset --yes
```

This deletes the browser administrator and revokes all browser sessions without
changing library data, integration settings, or the API key. Open Mangarr and
create the replacement administrator immediately. Access to the Docker host or
container control plane is therefore an administrator security boundary.

## Security Checklist

Before exposing Mangarr beyond a trusted LAN, verify:

- [ ] The browser administrator has been created and an anonymous request is
      redirected to `/login`.
- [ ] The API key under **Settings > General** is non-empty and kept separate
      from browser credentials.
- [ ] `/config` is readable and writable only by the chosen container UID/GID.
- [ ] Remote access uses an HTTPS reverse proxy rather than a direct public
      Docker port.
- [ ] The proxy sends `X-Forwarded-Proto: https` so cookies receive `Secure`.
- [ ] Application backup ZIPs and stopped `/config` snapshots are stored securely.
- [ ] Indexer URLs, passwords, API keys, and private tracker details are not
      stored in Compose or committed to source control.

## Backups

Persistent application state lives under `/config`:

| Path | Purpose |
| --- | --- |
| `/config/manga_arr.db` | SQLite database |
| `/config/.mangarr-secret-key` | Encryption key for stored credentials |
| `/config/covers/` | Cached cover images |
| `/config/backups/` | Application-created self-contained backup archives |

Mangarr backup ZIPs are created from SQLite's online backup API, so committed
WAL transactions are included consistently. Current archives contain
`manga_arr.db`, the matching `.mangarr-secret-key`, and `manifest.json`. Treat
every archive as sensitive because it can decrypt saved integration
credentials. Legacy database-only archives remain valid but require the
matching key from their original `/config` directory.

For a complete pre-upgrade snapshot, stop Mangarr and archive the host config
directory:

```bash
docker compose stop mangarr
tar -C . -czf mangarr-config-backup.tgz config
docker compose start mangarr
```

The Backup page creates and validates self-contained backup ZIP files. A full
host snapshot remains the strongest pre-upgrade recovery artifact because it
also includes covers, prior restore snapshots, and backup metadata.

## Restoring

Validate and restore an application backup only while Mangarr is stopped. The
restore command retains the previous database and key as timestamped
`pre-restore` files:

```bash
docker compose stop mangarr
docker compose run --rm --no-deps mangarr \
  mangarr backup validate /config/backups/mangarr_backup_TIMESTAMP.zip
docker compose run --rm --no-deps mangarr \
  mangarr backup restore /config/backups/mangarr_backup_TIMESTAMP.zip --yes
docker compose up -d
```

For a full stopped `/config` snapshot, restore the host archive instead:

```bash
docker compose stop mangarr
mv config config.failed
mkdir config
tar -xzf mangarr-config-backup.tgz
sudo chown -R 1000:1000 config
chmod 700 config
docker compose start mangarr
```

Adjust ownership to the `user:` value in your Compose file. Verify login,
library counts, and stored integration credentials after restore before deleting
the failed directory.

## Upgrading And Rollback

The `latest` tag follows the newest stable Mangarr release. Upgrade by pulling
the image and recreating the container:

### Migrating the public 1.0.x Compose file

Mangarr's public 1.0.x example selected its image through `MANGARR_VERSION` in
an `.env` file. Running `docker compose pull` against that unchanged file keeps
the installation on its old version. Before the first 1.1-or-newer upgrade,
replace the interpolated `image:` line with the stable channel used by the
current example:

```yaml
image: ghcr.io/kha-kis/manga-arr:latest
```

The remaining interpolated user, path, port, and application values can be
replaced with the direct values appropriate for the host. Once no `${...}`
references remain in the Compose file, Mangarr no longer needs its old `.env`
file. Review that file before removing it in case other Compose services still
use it.

### Standard upgrade

```bash
docker compose pull
docker compose up -d
docker compose ps mangarr
```

Before upgrading:

1. Read the target release in `CHANGELOG.md`.
2. Create a stopped `/config` snapshot.
3. Record the currently configured image:

```bash
docker compose images mangarr
```

4. Confirm the new image supports your architecture and review migration notes.

After upgrading, verify `/healthz`, **System > Status**, administrator login,
stored credentials, and a representative search/import workflow.

### Version pins

Operators who value reproducibility over automatic stable-channel updates can
replace `latest` with an exact immutable version:

```yaml
image: ghcr.io/kha-kis/manga-arr:1.1.0
```

### Rollback

Do not point an older image at a database already migrated by a newer version.
Rollback is a matched code-and-data restore:

1. Stop Mangarr.
2. Restore the `/config` snapshot created before the upgrade.
3. Change the Compose `image:` to the exact previous version or digest.
4. Pull and recreate the container.

```bash
docker compose stop mangarr
# Restore the matching config snapshot here.
docker compose pull
docker compose up -d
```

## Troubleshooting

### Container is unhealthy

```bash
docker compose ps
docker compose logs --tail=200 mangarr
curl -i http://127.0.0.1:6789/healthz
```

### Permission denied

Compare the Compose `user:` value with host ownership:

```bash
stat -c '%u:%g %n' config data
```

Correct ownership instead of weakening permissions.

### Browser redirects to setup unexpectedly

Confirm the expected `/config` directory is mounted and contains
`manga_arr.db`. An empty or incorrect bind mount starts a new installation.

### Stored credentials cannot be decrypted

Restore the matching `/config/.mangarr-secret-key`. If the original key is
lost, encrypted values cannot be recovered from the database; re-enter the
affected credentials in the authenticated settings UI.
