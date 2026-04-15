# Mangarr

A self-hosted manga library manager — the `*arr`-style companion for
manga and light-novel releases. FastAPI + Jinja2 + HTMX + Alpine,
backed by SQLite.

Mangarr watches indexers (Prowlarr / Torznab / Newznab), monitors
your series for new volumes and chapters, hands downloads off to
qBittorrent, SABnzbd, or Suwayomi, imports completed files into
your library, and notifies downstream tools (Komga, Discord, Ntfy,
Gotify, Apprise, Pushover, Pushbullet, Slack, generic webhooks,
email).

## Getting started

```bash
git clone https://github.com/Kha-kis/manga-arr.git
cd manga-arr
cp .env.example .env   # fill in download-client credentials
docker compose up -d
```

Then open <http://127.0.0.1:6789> — the compose file publishes on
loopback by default. For LAN or internet access, see
[`docs/deployment.md`](docs/deployment.md).

## Deployment & security

- **[`docs/deployment.md`](docs/deployment.md)** — why the container
  binds `0.0.0.0`, how to publish it safely (local-only / LAN /
  reverse proxy), and a security checklist covering API-key,
  CSRF cookie flags, `/config` permissions, and the `.env` template.

The default `docker-compose.yml` ships the safest pattern:
`ports: ["127.0.0.1:6789:8000"]` — only the host machine can
reach Mangarr. Move to a reverse proxy before exposing it to a LAN
or the internet.

## Security hardening completed (April 2026)

A full external security audit closed with **15 merged PRs** covering
every Critical, High, and Medium finding from the original report.
See [`CHANGELOG.md`](CHANGELOG.md) for per-PR detail. One-line
summary of what each PR addressed:

| PR | Severity | Finding |
|---:|---|---|
| [#1](https://github.com/Kha-kis/manga-arr/pull/1)  | **C1 + C3**    | Path traversal in import pipeline + XXE in ComicInfo / RSS / Torznab parsing |
| [#2](https://github.com/Kha-kis/manga-arr/pull/2)  | **C2**         | SSRF protection on user-supplied outbound URLs (notifications, RSS, Komga test, indexers, cover URLs) |
| [#3](https://github.com/Kha-kis/manga-arr/pull/3)  | (bonus)        | `init_db` ordering bug — chapters table add_col before CREATE TABLE |
| [#4](https://github.com/Kha-kis/manga-arr/pull/4)  | **C2 follow-up** | Slack webhook validation + deterministic DNS acceptance test |
| [#5](https://github.com/Kha-kis/manga-arr/pull/5)  | **H2**         | API-key middleware fails closed when `api_key` is blank; auto-seed on startup |
| [#6](https://github.com/Kha-kis/manga-arr/pull/6)  | **H3**         | Bounded concurrent imports + atomic queue-row claim |
| [#7](https://github.com/Kha-kis/manga-arr/pull/7)  | **H1**         | Background task lifecycle — tracking, cancel on shutdown, log unexpected exits |
| [#8](https://github.com/Kha-kis/manga-arr/pull/8)  | **M2**         | Batch-atomic multi-file imports (staging dir + SQLite `SAVEPOINT` rollback) |
| [#9](https://github.com/Kha-kis/manga-arr/pull/9)  | (bonus)        | `log_event` accepts the active connection to avoid `SQLITE_BUSY` inside write transactions |
| [#10](https://github.com/Kha-kis/manga-arr/pull/10) | **M1**         | CSRF cookie `SameSite=Strict`, `HttpOnly`, conditional `Secure`; token exposed via `<meta>` for HTMX |
| [#11](https://github.com/Kha-kis/manga-arr/pull/11) | **M3**         | Custom-format / release-profile regex ReDoS protection (nested-quantifier rejection + input-length cap) |
| [#12](https://github.com/Kha-kis/manga-arr/pull/12) | **M4**         | Explicit allowlists for request-controlled `ORDER BY` |
| [#13](https://github.com/Kha-kis/manga-arr/pull/13) | **M5**         | Input-shape guards on `add_col` and `fire_notifications` f-string SQL helpers |
| [#14](https://github.com/Kha-kis/manga-arr/pull/14) | **M7**         | Log (don't silently swallow) at four best-effort exception sites |
| [#15](https://github.com/Kha-kis/manga-arr/pull/15) | **M8**         | Deployment + network-binding documentation |

### One known remaining item

- **H4 — plaintext secrets in the SQLite DB** (download-client
  passwords, Komga credentials, Google Books API key, indexer API
  keys). Needs a Fernet / master-key design plus a migration path
  for existing installs. Explicitly deferred; tracked for a future
  release. Mitigation today: keep `/config` permissions at `0700`
  (noted in the deployment security checklist).

### Tests added by the hardening sweep

The audit started with no Python test harness at all. It ended with
**172 tests** across 10 files:

| File | Tests | What it covers |
|---|--:|---|
| `tests/python/test_api_key_middleware.py`    | 12 | H2 middleware fail-closed + `ensure_api_key` |
| `tests/python/test_background_tasks.py`      |  7 | H1 tracked tasks + graceful cancel + exception logging |
| `tests/python/test_csrf_cookie.py`           | 14 | M1 cookie flags + `_should_secure_cookie` helper |
| `tests/python/test_docs_consistency.py`      |  6 | M8 guards: `Dockerfile` / `compose` / `.env.example` stay in sync with the doc |
| `tests/python/test_fstring_input_shape.py`   | 16 | M5 identifier + typedef validators + event whitelist |
| `tests/python/test_import_atomicity.py`      | 17 | M2 `_ImportStaging` primitives + 5 `_execute_import` integration tests |
| `tests/python/test_import_concurrency.py`    |  9 | H3 semaphore bound + claim race |
| `tests/python/test_init_db.py`               |  3 | #3 fresh-install init + idempotency regression |
| `tests/python/test_log_event.py`             |  6 | #9 `db=` parameter + performance floor |
| `tests/python/test_order_by.py`              | 14 | M4 `build_order_by` + SQL injection payloads |
| `tests/python/test_regex_safety.py`          | 21 | M3 `safe_regex_search` + `compile_user_regex` + integration |
| `tests/python/test_security.py`              | 10 | C1 path traversal + C3 XXE |
| `tests/python/test_silent_except_logging.py` |  7 | M7 log-not-swallow + rollback masking guard |
| `tests/python/test_ssrf.py`                  | 29 | C2 SSRF helper + 11 sink wirings |

Run them with:

```bash
PYTHONPYCACHEPREFIX=/tmp/mangarr-pyc python3 -m pytest tests/python/ -v
```

## Development

The app source is `app/`. Templates are in `app/templates/`. Routers
are in `app/routers/`. Shared helpers (`get_db`, `safe_regex_search`,
`validate_outbound_url`, `validate_sql_identifier`, `build_order_by`,
…) live in `app/shared.py` or `app/security.py`.

Lint and type:

```bash
ruff check app/
mypy --ignore-missing-imports --no-strict-optional --follow-imports=silent app/main.py
```

Both are currently at baseline — the hardening sweep was careful not
to introduce new findings.

## Project instructions for Claude Code

See [`CLAUDE.md`](CLAUDE.md) and [`.claude/skills/`](.claude/skills/)
for the project-specific agent briefings, including the accessibility,
frontend-design, and SEO skill packs used in frontend work.
