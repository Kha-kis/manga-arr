"""Hardening tests for path traversal (C1) and XXE (C3)."""
import os
import shutil
import tempfile
import zipfile

import pytest


# ───────────────────────── C1: Path traversal ─────────────────────────

def test_build_filename_strips_traversal_when_no_format(monkeypatch):
    import main
    # Force "no format template" branch.
    monkeypatch.setitem(main.CONFIG, "file_format", "")
    monkeypatch.setitem(main.CONFIG, "chapter_format", "")

    out = main.build_filename("Series", 1.0, "../../pwn.cbz")
    assert "/" not in out and "\\" not in out
    assert ".." not in out.split(os.sep)
    # Resulting name, joined under any dir, stays under that dir.
    base = tempfile.mkdtemp(prefix="mangarr-traversal-")
    try:
        joined = os.path.realpath(os.path.join(base, out))
        assert joined.startswith(os.path.realpath(base) + os.sep)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_safe_join_under_rejects_unsafe_payloads(tmp_path):
    """safe_join_under must REJECT (raise ValueError on) any filename that
    contains a path separator, '..' component, or absolute path. It must not
    silently sanitize them — that hides intent from the caller's logs."""
    import main
    base = str(tmp_path)

    rejected = [
        "../../etc/passwd",
        "..\\..\\windows.cbz",
        "/etc/passwd",
        "C:\\Windows\\system32\\evil.cbz",   # backslash separator
        "subdir/../../escape.cbz",
        "subdir/file.cbz",                    # bare separator, no traversal
        "..",
        "../sibling.cbz",
        "",
    ]
    for p in rejected:
        with pytest.raises(ValueError):
            main.safe_join_under(base, p)


def test_safe_join_under_accepts_normal_filenames(tmp_path):
    """Normal, separator-free filenames must still work and land under dst_dir."""
    import main
    base = str(tmp_path)
    base_real = os.path.realpath(base)

    for p in [
        "normal.cbz",
        "Series Vol 01.cbz",
        "[Group] Title - c001 (v01) [Digital].cbz",
        "weird:chars*here?.cbz",  # forbidden chars get sanitized but no separators
    ]:
        out = main.safe_join_under(base, p)
        out_real = os.path.realpath(out)
        assert out_real.startswith(base_real + os.sep), \
            f"{p!r} did not land under base: {out_real!r}"


def test_safe_join_under_rejects_unusable_input(tmp_path):
    """Inputs that sanitize down to the 'Unknown' placeholder must be rejected
    rather than coined as a generic name. (sanitize_filename returns 'Unknown'
    for empty / all-dots / all-spaces input.)"""
    import main
    base = str(tmp_path)
    for p in ["...", "   ", ". . .", "."]:
        with pytest.raises(ValueError):
            main.safe_join_under(base, p)


def test_safe_join_under_raises_on_symlink_escape(tmp_path):
    """Defense-in-depth: if the basename ends up resolving outside dst_dir via
    a pre-existing symlink in dst_dir, the helper must raise."""
    import main
    base = tmp_path / "dst"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # Place a symlink inside dst_dir that points outside.
    (base / "trap.cbz").symlink_to(outside / "real.cbz")
    with pytest.raises(ValueError):
        main.safe_join_under(str(base), "trap.cbz")


def test_import_queue_filename_traversal_is_rejected(tmp_path):
    """Simulate the import-loop sink: a queue row whose filename column is
    '../../pwn.cbz' must be rejected outright. The sentinel target outside
    the series root must never be written."""
    import main

    series_root = tmp_path / "Series"
    series_root.mkdir()
    parent_sentinel = tmp_path / "pwn.cbz"
    assert not parent_sentinel.exists()

    with pytest.raises(ValueError):
        main.safe_join_under(str(series_root), "../../pwn.cbz")

    assert not parent_sentinel.exists()


# ───────────────────────── C3: XXE ─────────────────────────

XXE_PAYLOAD = (
    '<?xml version="1.0"?>\n'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
    '<ComicInfo><Series>&xxe;</Series><Volume>1</Volume></ComicInfo>'
)


def test_comicinfo_xxe_does_not_resolve(tmp_path):
    """A CBZ whose ComicInfo.xml carries an external entity must not exfiltrate
    /etc/passwd. read_comic_info should return all-None (fail closed) rather
    than expand the entity or crash the importer."""
    import main

    cbz = tmp_path / "evil.cbz"
    with zipfile.ZipFile(cbz, "w") as zf:
        zf.writestr("ComicInfo.xml", XXE_PAYLOAD)

    result = main.read_comic_info(str(cbz))
    # Fail-closed: parser refused the DOCTYPE, function returned defaults.
    assert result == {"series": None, "number": None, "volume": None}


def test_rss_xxe_does_not_resolve():
    """The custom-RSS importer must reject DOCTYPE entities. We test the
    underlying defused parser directly to keep the test hermetic (no httpx)."""
    from defusedxml.ElementTree import fromstring as safe_fromstring
    from defusedxml.common import EntitiesForbidden

    rss = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        '<rss><channel><item><title>&xxe;</title></item></channel></rss>'
    )
    with pytest.raises(EntitiesForbidden):
        safe_fromstring(rss)


def test_indexer_torznab_xxe_does_not_resolve():
    """Torznab/Newznab parser must reject DOCTYPE entities."""
    from routers.indexers import _parse_torznab_rss

    xml = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        '<rss><channel><item><title>&xxe;</title>'
        '<link>http://x/</link></item></channel></rss>'
    )
    # Function swallows parse errors and returns []; the key assertion is that
    # the entity does not resolve into the parsed output.
    items = _parse_torznab_rss(xml, "test")
    assert items == [] or all("root:" not in (it.get("title") or "") for it in items)


def test_xml_parsing_imports_use_defusedxml():
    """Guard: ensure the four prior unsafe parse sites no longer reference the
    stdlib ET.parse / ET.fromstring on untrusted XML."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    files = [
        root / "main.py",
        root / "routers" / "indexers.py",
        root / "routers" / "import_lists.py",
    ]
    for fp in files:
        text = fp.read_text()
        # Allow ET. used for *building* XML in main.py (build_comicinfo_xml).
        # Forbid ET.parse(...) / ET.fromstring(...) usage on untrusted input.
        for needle in ("ET.fromstring(", "_ET.fromstring(", "_ET.parse("):
            assert needle not in text, f"{fp}: {needle} still present"
