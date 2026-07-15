# Mangarr

Mangarr is a self-hosted manga and light-novel library manager. It applies the
`*arr` workflow to volumes, chapters, editions, and multi-volume packs: monitor
a series, search indexers, send a release to a download client, import the
completed files, and notify the rest of your media stack.

**Current release:** `1.0.0-rc.1`. This is a release candidate. Back up
`/config` before upgrading and pin the image version while evaluating it.

## Features

- Manga-aware volume, chapter, edition, omnibus, and pack tracking
- Prowlarr, Torznab, and Newznab indexers
- qBittorrent and SABnzbd download clients
- Suwayomi direct-download search and handoff
- Quality profiles, custom formats, release profiles, delay profiles, and
  language profiles
- Automatic and manual import with CBZ/CBR handling and `ComicInfo.xml`
- Existing-library discovery, adoption, rescan, rename preview, and organize
  workflows
- AniList, MangaDex, MangaUpdates, and Kitsu metadata reconciliation
- Komga, Discord, Ntfy, Gotify, Apprise, Pushover, Pushbullet, Slack, email,
  and generic webhook notifications
- Sonarr-style `/api/v1` and `/api/v3` compatibility surfaces for automation
- Single-administrator browser login plus separate API-key authentication

The detailed compatibility inventory and intentional non-goals are in
[`docs/sonarr-parity.md`](docs/sonarr-parity.md).

## Quick Start

Requirements: Docker Engine with the Compose plugin and host directories that
are writable by UID/GID 1000, or the UID/GID configured in `.env`.

```bash
git clone https://github.com/Kha-kis/manga-arr.git
cd manga-arr
mkdir -p config data/media/manga data/torrents/manga
chmod 700 config
docker compose up -d
docker compose exec mangarr cat /config/.mangarr-setup-token
```

Open <http://127.0.0.1:6789> and create the local administrator with the
one-time setup token. The token file is mode `0600` and is removed after setup.

The public Compose file:

- pulls `ghcr.io/kha-kis/manga-arr:${MANGARR_VERSION:-latest}`;
- publishes only on host loopback by default;
- runs the container without root privileges;
- stores persistent state in `./config` and media/download data in `./data`.

Copy [`.env.example`](.env.example) to `.env` to pin a release or change the
bind address, port, runtime UID/GID, paths, timezone, or initial settings.
Configure indexers, download clients, metadata providers, and notifications in
the Mangarr UI.

For LAN or internet access, reverse-proxy configuration, file ownership,
backup, upgrade, and recovery instructions, read
[`docs/deployment.md`](docs/deployment.md).

## Authentication

Browser and API authentication are deliberately separate:

- Use the local administrator account for the browser UI.
- Use the API key from **Settings > General** for clients and automation.
- Keep the default loopback bind until first-run administrator setup is done.
- Put Mangarr behind an HTTPS reverse proxy before exposing it beyond a trusted
  LAN. Do not publish the application directly to the internet.

If the administrator password is lost, the offline recovery command revokes all
browser sessions and creates a new setup token without changing library data or
the API key:

```bash
docker compose exec mangarr python /app/auth_cli.py reset-admin --yes
```

See [`SECURITY.md`](SECURITY.md) for supported versions and private
vulnerability reporting.

## Upgrading

Pin `MANGARR_VERSION` to an immutable release tag in `.env`, back up the SQLite
database and `/config/.mangarr-secret-key` together, then pull and recreate only
Mangarr:

```bash
docker compose pull mangarr
docker compose up -d --no-deps mangarr
docker compose ps mangarr
```

Verify `/healthz`, the System Status page, and a representative search/import
workflow after an upgrade. Do not run an older image against a database already
migrated by a newer release; restore the matching pre-upgrade `/config` backup
when rolling back. The full procedure is in
[`docs/deployment.md`](docs/deployment.md#upgrading-and-rollback).

## Data And Backups

Persistent application state lives under `/config`:

- `manga_arr.db`: SQLite database
- `.mangarr-secret-key`: key used to encrypt stored integration credentials
- `covers/`: cached cover images
- `backups/`: application-created database backups

The database and secret key are one recovery unit. A database restored without
its matching key cannot decrypt saved credentials. The Backup page validates
database backup ZIP files, but restore remains an offline maintenance action.

## Development

Application code is in `app/`, Jinja templates are in `app/templates/`, and
tests are in `tests/`.

```bash
make test
```

Before a release candidate or merge that changes a critical workflow, run the
isolated browser gate as well:

```bash
make test-release-safe
```

`make test-release` additionally exercises the operator's live database and is
manual-only. Do not use it as a normal development gate.

Additional project references:

- [`docs/deployment.md`](docs/deployment.md): deployment and recovery
- [`docs/releases.md`](docs/releases.md): versioning and release process
- [`docs/sonarr-parity.md`](docs/sonarr-parity.md): compatibility scope
- [`CHANGELOG.md`](CHANGELOG.md): release notes
- [`tests/README.md`](tests/README.md): test architecture
- [`CLAUDE.md`](CLAUDE.md): codebase invariants and contributor guidance

## Support

Use [GitHub Issues](https://github.com/Kha-kis/manga-arr/issues) for reproducible
bugs and focused feature requests. Include the Mangarr version shown on
**System > Status**, deployment method, relevant sanitized logs, and exact
reproduction steps. Do not post API keys, setup tokens, passwords, private
tracker URLs, or encrypted-secret keys.
