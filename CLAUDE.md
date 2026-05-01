# Mangarr

Solo-dev manga library manager (Sonarr/Readarr-equivalent). FastAPI + Starlette + Jinja2 + HTMX + Alpine, SQLite, Docker compose deploy. Source in `app/` (split across `app/routers/`), templates in `app/templates/`, tests in `tests/`. ~85% Sonarr parity, active development.

## Hard invariants — silent breakage if violated

- **`sqlite3.Row` has no `.get()`.** Use `row['key']` or `dict(row)`. The RSS grab loop silently died for weeks from one `.get()` call — caught by outer try/except, no torrents ever matched.
- **Volume display uses `vol_num_to_display(vol_num)`** (`app/shared.py`); volume search queries use `vol_num_to_search(vol_num)`; never `int(vol_num)` for either. Floats like 3.01→3a, 3.5→3½ are intentional, not rounding errors. Search-side `3.5` must produce `"3.5"`, not `"3"`, or half-volumes are silently unreachable.
- **`get_db()` is a context manager** (`app/shared.py`). Connection closes at `with` exit. Convert Rows to dicts before exiting; variables holding the connection cannot outlive the block. Re-open for any later DB work.
- **Prowlarr manga categories: 7000 + 7010 + 7020** (Books/General, Mags, EBook — see `app/routers/indexers.py`). Nyaa manga lives in 7000 specifically.
- **Prowlarr per-sub-indexer `enable` flag is honored** — `_get_prowlarr_indexers` (`app/routers/indexers.py:483`) skips any sub-indexer where Prowlarr's `/api/v1/indexer` response has `enable: false`. Toggling off in Prowlarr's UI is sufficient; you don't also need to disable Mangarr-side. Removing this filter would silently start polling indexers the user explicitly disabled.
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
  - **When NOT to dispatch**: 1–3 file reads, a single targeted edit, or anything that fits in a couple of Bash commands — the dispatch overhead (briefing, parsing the report) costs more tokens than the inline work would. Subagents earn their keep on bounded mechanical fan-out, not on quick lookups.

- **Memory**: `~/.claude/projects/-opt-manga-arr/memory/` holds prior-session context. The auto-curated `MEMORY.md` index lists all entries; current files include `project_overview.md`, `project_ui_audit.md`, `project_scripted_api_access.md`, `project_editor_form_clobbers.md`, `feedback_token_economy.md`, `feedback_branch_lifecycle.md`, `feedback_preview_before_apply.md`, `reference_git_remotes.md`. Read the index first, then dive into the specific files relevant to your task — they're authoritative for past decisions.

## Model selection

Default to **Opus 4.7** in this project. Mangarr's failure modes are mostly silent-correctness bugs (sqlite3.Row, route order, dual-mode HTMX, get_db scope) where a wrong call passes tests but breaks at runtime — careful reasoning > speed. Drop to **Sonnet** only for narrow in-file edits (single-line fixes, CSS tweaks, copy-paste boilerplate, template-only changes). Avoid Haiku here unless the task is a one-shot lookup.

## Design tokens — use the variables, don't hardcode values

The `<style>` block in `app/templates/base.html` `:root` defines a complete design-token vocabulary. New CSS (in `base.html`, in template `<style>` blocks, or inline `style="..."` attrs) must reference these vars rather than hardcode raw values — see PRs #94–#101 for the migration history and `tests/python/test_hard_invariants.py` for the enforcement.

- **Radius scale**: `--radius-xs` 4px, `--radius-sm` 6px, `--radius-md` 8px, `--radius-lg` 10px, `--radius-xl` 12px. Plus `50%` (circles), `999px` (pills), and the deliberately tighter `2px`/`3px` for thin progress bars / scrollbar — those stay raw.
- **Type scale**: `--text-2xs` 0.65rem, `--text-xs` 0.7rem, `--text-sm` 0.75rem, `--text-base` 0.82rem, `--text-lg` 0.95rem, `--text-xl` 1rem, `--text-2xl` 1.1rem. Display sizes (`1.25rem+`, `clamp(...)`, `14px`/`15px` foundation) stay raw.
- **Color tokens** for ember / ruby / jade / gold / iris / sky (and `--ember-hi` for the brand-bright accent):
  - solid: `var(--ember)` etc.
  - 5-step alpha ladder per color: `--{color}-tint` (0.07), `--{color}-bg` (0.10), `--{color}-soft` (0.20), `--{color}-medium` (0.30), `--{color}-strong` (0.40)
  - rose / amber have only solid + `-bg` so far.
- **Z-index scale**: `--z-base` through `--z-progress` (0–9500). Don't hardcode magic numbers — `var(--z-tooltip)` etc.
- **Text-color contrast**: `--text-3` is `#80806e` (WCAG AA over `--ink-2`); don't darken without re-checking the contrast ratio. PR #101 has the math.

## Test backbone — where to look + extend

Mangarr has 79 Python test files; the load-bearing integration tests cluster around the production-readiness work:

| File | What it covers |
|---|---|
| `tests/python/test_e2e_grab_to_library.py` | Core search → grab → seen dedup (URL + GUID) → import → library. Stubs at I/O boundaries only. |
| `tests/python/test_route_destructive_ops.py` | Series delete + blocklist mutations (cascade correctness). |
| `tests/python/test_route_state_changes.py` | Volume actions, chapter map, history, queue actions, tag rename/delete, import-list CRUD. |
| `tests/python/test_route_profile_crud.py` | Quality / delay / release / language / custom-format / remote-path-mappings CRUD. |
| `tests/python/test_route_backup_and_import_queue.py` | Backup zip integrity + import-queue actions (skip / dismiss / retry / clear-old). |
| `tests/python/test_hard_invariants.py` | Tripwires for the silent-correctness invariants in this CLAUDE.md. |
| `tests/python/test_import_atomicity.py` + `test_import_mapping.py` | Deep coverage of the file-staging / chapter-volume mapping logic. |
| `tests/python/test_route_sweep.py` | Auto-renders every parameter-free GET page. |
| `tests/python/test_main_py_split_invariants.py` | Architecture invariants (main.py size ceiling, no `from main import X` for extracted symbols). |

Adding new mutation routes? Either extend the most appropriate file above or create a new `test_route_<area>.py` following the same `TestClient` + CSRF + DB-state-assertion pattern.

## Skill relevance for this project

- **accessibility** — relevant; we've used it for a11y sweeps and WCAG contrast (PRs #91, #101). Reach for it on UI work.
- **frontend-design** — marginally relevant; the skill biases toward "distinctive new UI from scratch", but Mangarr's aesthetic is established and recent work has been *cohesion* (token migration). Use it as inspiration for patterns, not as a license to redesign.
- **seo** — N/A for this project. Mangarr is self-hosted behind auth; no public surface to optimize. Ignore the autoskills listing for SEO.

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
