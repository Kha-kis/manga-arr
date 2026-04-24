"""Architecture-invariant tests that bookend the 19-PR main.py split.

These aren't behavior tests — they're constraints on the shape of the
codebase. They exist to catch regressions that are easy to introduce
accidentally but hard to detect via normal test failures:

  - main.py slowly re-accreting definitions until it's monolithic again
  - extracted modules gaining a `from main import X` for a symbol that
    was moved OUT of main (meaning someone re-added it to main instead
    of the owning module)
  - the re-export bridge in main.py growing new runtime logic instead
    of just re-exporting

The line-count ceiling is deliberately generous (1100) so this test
doesn't fire on small, genuine additions (a new route handler, a new
middleware). Its job is to catch drift on the order of hundreds of
lines, not to police every commit.
"""
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
MAIN_PY = APP / "main.py"


# Extracted modules — every symbol these expose MUST be imported directly
# from them (or from a downstream module that re-exports them), not via
# `from main import X`. Routers are exempt from this rule: they touch many
# symbols and `import main as _m` remains ergonomic there.
_EXTRACTED_MODULES = (
    "clients", "comicinfo", "config", "events", "evaluation", "files",
    "grab", "helpers", "import_pipeline", "metadata", "metadata_enrichment",
    "middleware", "notifications", "parsing", "rescan", "schema", "tasks",
    "volumes",
)

# Symbols these modules define. Any non-main, non-router module that does
# `from main import <symbol>` for one of these is a regression — the import
# should point at the owning module instead.
_EXTRACTED_SYMBOLS = {
    "log_event", "add_history", "broadcast_queue_event",
    "grab_item", "grab_existing", "poll_rss",
    "check_download_status", "_execute_import", "_queue_import",
    "_process_auto_import", "_guarded_execute_import",
    "rss_loop", "status_loop", "refresh_ongoing_loop",
    "rescan_series_folder", "_resolve_series_dest_root",
    "score_release", "evaluate_release",
    "_check_volume_completion", "_cascade_chapters",
    "anilist_search", "mu_search", "fetch_mangadex_id",
    "read_comic_info", "_try_inject_comicinfo",
    "refresh_mangadex_map", "chapters_to_volume_set",
    "create_volume_stubs", "populate_chapters",
    "NOTIFICATION_SECRET_KEYS_BY_TYPE", "SETTINGS_SECRET_KEYS",
}


def test_main_py_stays_entry_point_sized():
    """main.py should stay entry-point-shaped, not re-accrete to monolithic.

    Hard ceiling 1100 lines. Current is ~780 after the 19-PR split chain.
    If this fires, either the addition belongs in an existing extracted
    module, or a new module needs to be extracted — don't raise the
    ceiling casually.
    """
    lines = MAIN_PY.read_text().splitlines()
    assert len(lines) <= 1100, (
        f"main.py has grown to {len(lines)} lines (ceiling 1100). "
        f"Either move the new code into an existing module under app/, "
        f"extract a new module, or if the addition is genuinely entry-"
        f"point logic (route handler, middleware, lifespan wiring) and "
        f"the ceiling is wrong, raise it in this test with a one-line "
        f"rationale."
    )


def test_extracted_modules_do_not_import_their_own_symbols_from_main():
    """An extracted module must not do `from main import X` for a symbol
    that was extracted OUT of main. Either the import should point at
    the owning module, or (regression!) X has been re-added to main.

    Routers are allowed to `import main as _m` — they straddle many
    extracted modules and wholesale-importing main is ergonomic. This
    test only polices the app/ modules, not app/routers/.
    """
    offenders = []
    for mod in _EXTRACTED_MODULES:
        path = APP / f"{mod}.py"
        if not path.exists():
            continue
        src = path.read_text()
        for m in re.finditer(r"^\s*from main import\s+(.+?)(?:\s*#.*)?$",
                             src, re.MULTILINE):
            imported = [s.strip() for s in m.group(1).split(",")]
            for name in imported:
                if name in _EXTRACTED_SYMBOLS:
                    offenders.append(f"{mod}.py imports {name!r} from main")
    assert not offenders, (
        "Extracted modules importing their own symbols from main:\n  "
        + "\n  ".join(offenders)
        + "\nThese should import from the owning module directly, not "
          "through the main.py re-export bridge."
    )


def test_main_py_re_exports_have_no_new_runtime_logic():
    """main.py's bridge to extracted modules should stay as re-exports,
    not grow ad-hoc helpers next to them.

    Heuristic: count top-level `def` / `async def` / `class` definitions
    in main.py. The current set is the entry-point shape (load_config,
    ensure_api_key, queue_events, backfill_pack_ranges, lifespan, and
    a handful of framework-level wrappers). A hard ceiling of 20
    top-level definitions gives room for small additions (a new route,
    a new helper) but fires loudly if main.py starts growing real
    domain logic again.
    """
    src = MAIN_PY.read_text()
    top_defs = re.findall(
        r"^(?:async\s+def|def|class)\s+[A-Za-z_]",
        src, re.MULTILINE,
    )
    assert len(top_defs) <= 20, (
        f"main.py has {len(top_defs)} top-level definitions (ceiling 20). "
        f"Either the addition belongs in an existing module, or — if it's "
        f"genuine entry-point logic — raise the ceiling in this test with "
        f"a rationale."
    )
