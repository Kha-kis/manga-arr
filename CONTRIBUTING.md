# Contributing to Mangarr

Mangarr is a solo-maintained, self-hosted manga and light-novel library
manager. Contributions are welcome when they are focused, tested, and preserve
the application's data-safety guarantees.

## Before You Start

- Use GitHub Issues for reproducible bugs and scoped feature proposals.
- Use GitHub Discussions for setup help and broader design conversations.
- Report security problems privately as described in `SECURITY.md`.
- Do not include API keys, passwords, private tracker URLs, library paths, or
  unredacted logs in public issues.

## Development Setup

The supported development range is Python 3.11 through 3.14, with Docker and
Node.js available for the browser suite. Release containers use Python 3.14.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-test.txt
cd tests && npm ci && cd ..
make test
```

Use the isolated browser environment for changes that affect routes,
templates, HTMX behavior, authentication, or user workflows:

```bash
make test-release-safe
```

## Engineering Requirements

- Keep SQLite transactions short and never hold a write transaction across
  network or filesystem I/O.
- Convert `sqlite3.Row` objects to dictionaries before leaving a `get_db()`
  context; rows do not implement `.get()`.
- Use `vol_num_to_display()` for display and `vol_num_to_search()` for search.
- Preserve both HTMX and plain-form behavior for action endpoints.
- Put literal routes before parameterized siblings.
- Use `submitted_subset()` for editor POST handlers and preserve the
  hidden-input-first pattern for HTML checkbox fields.
- Preserve both URL and release-GUID grab deduplication, and honor disabled
  Prowlarr sub-indexers.
- Use the design tokens in `app/templates/base.html` for colors, radii,
  typography, and z-index values.
- Add focused tests for every behavior change and broader coverage for shared
  import, metadata, API, or authentication contracts.

## Pull Requests

Keep each pull request limited to one coherent change. Include the user-visible
behavior, risk, migration impact, and exact validation commands in the
description. Do not mix unrelated refactors into a functional fix.

Before opening a pull request:

```bash
make test
git diff --check
```

GitHub runs `make test-fast` as the short required PR check. It supplements the
local full suite; it does not replace `make test`.

Run `make test-release-safe` before requesting merge for changes affecting
critical workflows or public releases. The local release process is documented
in `docs/releases.md`; the test layers and fixture boundaries are documented in
[`tests/README.md`](tests/README.md).

By contributing, you agree that your contribution is licensed under the
repository's `AGPL-3.0-only` license.
