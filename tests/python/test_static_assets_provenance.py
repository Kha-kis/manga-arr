"""Static-assets provenance lock.

The frontend deps (HTMX, Alpine.js) are vendored under app/static/
rather than downloaded at container build time. app/static/PROVENANCE.md
documents the upstream URL and SHA256 of each file.

This test catches three classes of mistake:

  1. A vendored file is replaced (intentionally or by accident) without
     updating PROVENANCE.md — the on-disk SHA won't match the table.
  2. PROVENANCE.md is edited (e.g. a version bump in the table) but the
     on-disk file isn't replaced — same mismatch, opposite direction.
  3. A new file is added to app/static/ without an entry in
     PROVENANCE.md.

The intent is that a "version bump" is always exactly one PR with three
synchronised changes: the file, the table row, and any download command
in the PROVENANCE update guide.
"""
import hashlib
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "app" / "static"
PROVENANCE = STATIC / "PROVENANCE.md"


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_provenance_table() -> dict[str, str]:
    """Extract {filename: sha256} from the markdown table in PROVENANCE.md."""
    text = PROVENANCE.read_text()
    # Each row looks like: | `htmx.min.js`  | HTMX | 2.0.4 | <...> | `<sha256>` |
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        # cells[0] = `htmx.min.js` style cell with backticks
        m_name = re.match(r"`([^`]+\.js)`", cells[0])
        m_sha = re.match(r"`([0-9a-f]{64})`", cells[-1])
        if m_name and m_sha:
            out[m_name.group(1)] = m_sha.group(1)
    return out


def test_provenance_file_exists():
    """Sanity: the provenance doc must exist next to the vendored files."""
    assert PROVENANCE.exists(), \
        f"missing {PROVENANCE.relative_to(REPO_ROOT)} — see commit history " \
        "for the original; never delete this without retiring vendoring."


def test_every_vendored_js_has_a_provenance_row():
    """Every .min.js under app/static must appear in PROVENANCE.md."""
    expected = {p.name for p in STATIC.glob("*.min.js")}
    documented = set(_parse_provenance_table().keys())
    missing = expected - documented
    assert not missing, (
        f"app/static/PROVENANCE.md is missing rows for: {sorted(missing)}\n"
        f"Add a row with the upstream URL + SHA256 before merging."
    )


def test_vendored_sha_matches_provenance_table():
    """For each documented file, the on-disk SHA256 must match the table.
    Catches both a stale table after a binary update and a stale file
    after a table-only edit."""
    table = _parse_provenance_table()
    mismatches = []
    for fname, expected_sha in table.items():
        path = STATIC / fname
        if not path.exists():
            mismatches.append(f"{fname}: documented but not on disk")
            continue
        actual = _sha256(path)
        if actual != expected_sha:
            mismatches.append(
                f"{fname}: documented {expected_sha[:12]}…, "
                f"on-disk {actual[:12]}… — sync PROVENANCE.md or the file"
            )
    assert not mismatches, "\n  ".join(["SHA mismatch:"] + mismatches)
