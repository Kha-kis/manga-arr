# Mangarr

Solo-dev manga library manager (Sonarr/Readarr-equivalent). FastAPI + Starlette + Jinja2 + HTMX + Alpine, SQLite, Docker compose deploy. Source in `app/` (split across `app/routers/`), templates in `app/templates/`, tests in `tests/`. ~85% Sonarr parity, active development.

## Hard invariants — silent breakage if violated

- **`sqlite3.Row` has no `.get()`.** Use `row['key']` or `dict(row)`. The RSS grab loop silently died for weeks from one `.get()` call — caught by outer try/except, no torrents ever matched.
- **Volume display uses `vol_num_to_display(vol_num)`** (`app/shared.py`); volume search queries use `vol_num_to_search(vol_num)`; never `int(vol_num)` for either. Floats like 3.01→3a, 3.5→3½ are intentional, not rounding errors. Search-side `3.5` must produce `"3.5"`, not `"3"`, or half-volumes are silently unreachable.
- **`get_db()` is a context manager** (`app/shared.py`). Connection closes at `with` exit. Convert Rows to dicts before exiting; variables holding the connection cannot outlive the block. Re-open for any later DB work.
- **Prowlarr manga categories: 7000 + 7010 + 7020** (Books/General, Mags, EBook — see `app/routers/indexers.py`). Nyaa manga lives in 7000 specifically.
- **Route order in `app/routers/`:** literal paths must precede parameterized siblings in the same module (e.g. `/import-lists/sync` before `/import-lists/{id}`). Starlette first-match wins. Grep before adding any new path.
- **Action endpoints are dual-mode**: each handles HTMX (returns partial template, or `HX-Trigger` / `HX-Refresh` / `HX-Redirect` header) **and** plain form fallback. Don't break the plain-form path — server stays usable without JS.
- **CSRF**: `starlette-csrf` middleware in `app/middleware.py`. `base.html` injects the token for HTMX + plain forms. `/api/` routes bypass CSRF (they auth via `X-Api-Key`).
- **Two-layer grab dedup**: `seen.torrent_url` (URL match) AND `seen.release_guid` (cross-URL same-content match). Both checked before `grab_url` fires. Don't bypass either.

## Verify before claiming done

- `make test` — fast loop (Python + confirm-flow + route-sweep). Default for any code change.
- `make test-release-safe` — adds isolated browser tests.
- `make test-release` — full e2e (slow, only before tagged releases).

Paste the relevant tail of test output into the response. "Tests passed" without evidence doesn't count.

## Scope per session

- Bug fix: ≤50 LOC. Feature: ≤300 LOC. Files >500 LOC: extract a new module rather than growing further.
- One commit per logical change. Don't bundle unrelated work.

## Tools — use them, don't bypass them

- **rtk** auto-rewrites Bash output via the global hook (`PreToolUse` → `rtk hook claude`). Trust the compressed output. If something looks wrong or you need raw text, use `rtk proxy <cmd>` to bypass filtering for that one call. Never edit settings to disable the hook.

- **Serena auto-activates here** (`.serena/project.yml` present). For any symbol-level work in `app/`, use `find_symbol` / `find_referencing_symbols` / `replace_symbol_body` instead of reading whole files. `app/routers/series_.py` (~110KB) and similar large modules are exactly what Serena is for — **never `Read` them whole**.

- **ast-grep** for structural patterns. Concrete mangarr examples:
  - `ast-grep -p '$X.get($_)' app/` — audit possible `sqlite3.Row` violations
  - `ast-grep -p 'int($X)' app/` near `vol_num` references — find spots that should use `vol_num_to_display` (UI) or `vol_num_to_search` (indexer queries)
  - Faster + tighter than grep when the question is structural, not textual.

- **Subagents** — dispatch instead of doing it inline when the task is broad enough that intermediate exploration would bloat the main context:
  - **Explore** — codebase questions that'd take >3 searches ("where is `score_release` called from?", "how does the import pipeline flow?").
  - **Plan** — designing a new feature from the parity roadmap before touching code. Hand it the relevant memory file as context.
  - **general-purpose** — parallel test runs, multi-file refactors, or anything where the main session shouldn't carry the intermediate tokens.

- **Memory**: `~/.claude/projects/-opt-manga-arr/memory/` holds prior-session context. Check `mangarr-bugs.md`, `mangarr-htmx-migration.md`, and `mangarr-feature-parity-roadmap.md` (and the auto-curated `MEMORY.md` index) before guessing on patterns or planning new work — they're authoritative for past decisions.

## Model selection

Default to **Opus 4.7** in this project. Mangarr's failure modes are mostly silent-correctness bugs (sqlite3.Row, route order, dual-mode HTMX, get_db scope) where a wrong call passes tests but breaks at runtime — careful reasoning > speed. Drop to **Sonnet** only for narrow in-file edits (single-line fixes, CSS tweaks, copy-paste boilerplate, template-only changes). Avoid Haiku here unless the task is a one-shot lookup.

## Project documentation hazard — autoskills

The `autoskills` skill auto-generates a stub of available skills between `<!-- autoskills:start -->` / `<!-- autoskills:end -->` markers. **It will REPLACE the entire CLAUDE.md if the file does not contain those markers**, clobbering all project-specific guidance. The skills block at the end of this file preserves the markers so autoskills can update its content without nuking the rest. If you're editing CLAUDE.md, do NOT remove those markers; add new sections above them.

<!-- autoskills:start -->

Summary generated by `autoskills`. Check the full files inside `.claude/skills`.

## Accessibility (a11y)

Audit and improve web accessibility following WCAG 2.2 guidelines. Use when asked to "improve accessibility", "a11y audit", "WCAG compliance", "screen reader support", "keyboard navigation", or "make accessible".

- `.claude/skills/accessibility/SKILL.md`
- `.claude/skills/accessibility/references/A11Y-PATTERNS.md`: Practical, copy-paste-ready patterns for common accessibility requirements. Each pattern is self-contained and linked from the main [SKILL.md](../SKILL.md).
- `.claude/skills/accessibility/references/WCAG.md`

## Design Thinking

Create distinctive, production-grade frontend interfaces with high design quality. Use this skill when the user asks to build web components, pages, artifacts, posters, or applications (examples include websites, landing pages, dashboards, React components, HTML/CSS layouts, or when styling/beautifying any web UI).

- `.claude/skills/frontend-design/SKILL.md`

## SEO optimization

Optimize for search engine visibility and ranking. Use when asked to "improve SEO", "optimize for search", "fix meta tags", "add structured data", "sitemap optimization", or "search engine optimization".

- `.claude/skills/seo/SKILL.md`

<!-- autoskills:end -->
