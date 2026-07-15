<div align="center">

# Mangarr

**Sonarr-style automation for manga and light-novel libraries.**

[![Release](https://img.shields.io/github/v/release/Kha-kis/manga-arr?display_name=tag&sort=semver&style=flat-square&color=f08428)](https://github.com/Kha-kis/manga-arr/releases/latest)
[![Container](https://img.shields.io/badge/GHCR-linux%2Famd64%20%7C%20linux%2Farm64-242434?style=flat-square&logo=docker&logoColor=white)](https://github.com/Kha-kis/manga-arr/pkgs/container/manga-arr)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/github/license/Kha-kis/manga-arr?style=flat-square&color=22c87a)](LICENSE)

[Quick start](#quick-start) · [Features](#features) · [Documentation](#documentation) · [Support](SUPPORT.md)

</div>

![Mangarr library dashboard](docs/assets/mangarr-library.webp)

Mangarr monitors manga and light-novel series, searches indexers and direct
download sources, sends releases to a download client, and imports completed
files into an organized library. It understands volumes, chapters, editions,
omnibuses, specials, and multi-volume packs instead of treating manga like a
generic TV or book collection.

The current stable release is **1.0.1**. Mangarr is self-hosted, designed for a
single administrator, and distributed as a multi-platform container image.

## Features

- **Manga-aware monitoring:** volumes, chapters, half chapters, editions,
  omnibuses, specials, one-shots, and packs.
- **Automated acquisition:** RSS and backlog search through Prowlarr, Torznab,
  and Newznab, plus direct-download search and handoff through Suwayomi.
- **Download clients:** qBittorrent and SABnzbd with queue tracking, health
  checks, retry handling, and import handoff.
- **Import pipeline:** automatic and manual import, copy/move/hardlink modes,
  CBZ/CBR handling, split RAR support, duplicate-quality checks, and
  `ComicInfo.xml` metadata.
- **Release control:** quality profiles, custom formats, release profiles,
  delay profiles, language profiles, blocklists, and upgrade cutoffs.
- **Metadata lifecycle:** AniList, MangaDex, MangaUpdates, and Kitsu discovery,
  reconciliation, health reporting, and operator-controlled repair tools.
- **Library operations:** existing-library adoption, rescans, rename previews,
  organization, bulk editing, history, wanted lists, and calendar views.
- **Notifications and media servers:** Komga, Discord, Ntfy, Gotify, Apprise,
  Pushover, Pushbullet, Slack, email, and generic webhooks.
- **Automation API:** native endpoints plus Sonarr-style `/api/v1` and
  `/api/v3` compatibility surfaces.

See the [Sonarr parity inventory](docs/sonarr-parity.md) for the exact
compatibility scope and intentional non-goals.

## How It Works

1. Add a series and choose what Mangarr should monitor.
2. Mangarr searches configured sources or evaluates new RSS releases.
3. Matching releases are scored against profiles and sent to a download
   client.
4. Completed files are validated, staged, named, and imported into the library.
5. Metadata is written, history is recorded, and downstream services are
   notified.

## Quick Start

Requirements: Docker Engine with the Compose plugin and host directories that
are writable by UID/GID `1000`, or the UID/GID configured in `.env`.

```bash
git clone https://github.com/Kha-kis/manga-arr.git
cd manga-arr
cp .env.example .env
mkdir -p config data/media/manga data/torrents/manga
chmod 700 config
docker compose up -d
docker compose exec mangarr cat /config/.mangarr-setup-token
```

Open <http://127.0.0.1:6789> and create the administrator account with the
one-time setup token. The default Compose configuration binds to host loopback,
runs as a non-root user, and pins the stable image:

```text
ghcr.io/kha-kis/manga-arr:1.0.1
```

Configure indexers, download clients, metadata providers, root folders, and
notifications from the Mangarr settings UI. Keep the loopback bind for initial
setup; use an HTTPS reverse proxy before exposing Mangarr beyond a trusted LAN.

### Persistent Paths

| Container path | Purpose |
| --- | --- |
| `/config` | SQLite database, encryption key, cached covers, and backups |
| `/data/media/manga` | Organized manga library in the example Compose layout |
| `/data/torrents/manga` | Completed downloads in the example Compose layout |

The database and `/config/.mangarr-secret-key` are one recovery unit. A database
restored without its matching key cannot decrypt stored integration
credentials. Back up the entire `/config` directory before upgrades.

## Upgrading

Set `MANGARR_VERSION` in `.env` to the release you want, back up `/config`, then
pull and recreate only Mangarr:

```bash
docker compose pull mangarr
docker compose up -d --no-deps mangarr
docker compose ps mangarr
```

Verify `/healthz`, System Status, and a representative search/import workflow.
Do not run an older image against a database migrated by a newer release; use
the matching pre-upgrade `/config` backup for rollback. The complete procedure
is in [Deployment and recovery](docs/deployment.md#upgrading-and-rollback).

## Security

- Browser sessions use the local administrator account; integrations use the
  separate API key from **Settings > General**.
- Stored integration credentials are encrypted with the key under `/config`.
- The public Compose file binds to `127.0.0.1` by default and runs without root
  privileges.
- Administrator recovery is an offline operation that revokes existing browser
  sessions.

```bash
docker compose exec mangarr python /app/auth_cli.py reset-admin --yes
```

Report vulnerabilities privately through the process in [SECURITY.md](SECURITY.md).
Never publish API keys, setup tokens, passwords, private tracker URLs, or
encryption keys.

## Documentation

| Area | Reference |
| --- | --- |
| Install, networking, backup, upgrade, and recovery | [Deployment and recovery](docs/deployment.md) |
| Supported Sonarr workflows and compatibility limits | [Sonarr parity](docs/sonarr-parity.md) |
| Versioning and release procedure | [Releases and versioning](docs/releases.md) |
| Stable-release acceptance gate | [Release qualification](docs/release-qualification.md) |
| User-visible changes | [Changelog](CHANGELOG.md) |
| Development workflow | [Contributing](CONTRIBUTING.md) |
| Test architecture and commands | [Test guide](tests/README.md) |
| Community expectations | [Code of Conduct](CODE_OF_CONDUCT.md) |

## Development

Mangarr uses FastAPI, Starlette, Jinja2, HTMX, Alpine.js, SQLite, and Docker
Compose. Application code lives in `app/`, templates in `app/templates/`, and
tests in `tests/`.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-test.txt
make test
```

Use `make test-release-safe` for changes to routes, templates, authentication,
imports, metadata, or browser workflows. Read [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a pull request.

## Support

Use [GitHub Discussions](https://github.com/Kha-kis/manga-arr/discussions) for
setup and workflow help. Use the
[issue forms](https://github.com/Kha-kis/manga-arr/issues/new/choose) for
reproducible bugs and scoped feature proposals. Support expectations are in
[SUPPORT.md](SUPPORT.md).

## License

Mangarr is licensed under the [GNU Affero General Public License v3.0 only](LICENSE)
(`AGPL-3.0-only`). If you modify Mangarr and make it available to users over a
network, you must offer those users the corresponding source code under the
same license.

Copyright (C) 2026 Kha-kis.
