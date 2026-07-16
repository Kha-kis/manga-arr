"""File-system, filename, and release-metadata helpers.

Fifth module extracted from main.py. Groups together every pure
function that parses a title/filename or builds a path:
  - sanitize_filename, safe_join_under          — path safety
  - parse_release_group, parse_revision         — title metadata
  - detect_quality_from_title                   — extension parsing
  - build_volume_label, build_chapter_label     — display labels
  - build_filename, _apply_format_tokens        — templated file naming
  - quality_from_filename, quality_rank         — quality tiering
  - detect_edition_type, detect_language        — release classification
  - is_official_release, classify_source_type   — source type
  - detect_file_type_magic                      — magic-byte file ID
  - convert_cbr_to_cbz, _maybe_convert_to_cbz   — archive conversion
  - MANGA_EXTENSIONS, QUALITY_RANK, pattern lists — module constants

Deliberately NOT here:
  - add_history (DB write; stays in main.py until import_pipeline extracts)
  - read/build/inject comicinfo (zip I/O + xml; separate concern, could
    move to files_comicinfo.py later)
  - _series_library_dir, rescan_series_folder (DB-coupled)

Pure move. main.py re-exports every symbol.
"""
from __future__ import annotations

import os
import re
import zipfile

from parsing import vol_num_to_display
from shared import get_cfg
from events import log_event


MANGA_EXTENSIONS = {'.cbz', '.cbr', '.zip', '.rar', '.pdf', '.epub', '.mobi'}


# ── Path safety ──────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """Convert a series title to a safe directory name."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    safe = safe.strip('. ')
    return safe or 'Unknown'


def safe_join_under(dst_dir: str, filename: str) -> str:
    """Join filename under dst_dir, rejecting unsafe input.

    Raises ValueError if filename:
      - is empty,
      - contains a path separator (/ or \\),
      - is absolute,
      - has any '..' path component,
      - sanitizes to the placeholder 'Unknown'.

    Defence-in-depth: also verifies the resolved candidate lives under
    realpath(dst_dir), catching symlink escapes inside dst_dir.
    """
    if not filename:
        raise ValueError("empty filename")
    if '/' in filename or '\\' in filename:
        raise ValueError(f"path separator in filename: {filename!r}")
    if os.path.isabs(filename):
        raise ValueError(f"absolute path rejected: {filename!r}")
    parts = filename.replace('\\', '/').split('/')
    if any(p == '..' for p in parts):
        raise ValueError(f"path traversal component in filename: {filename!r}")

    safe_name = sanitize_filename(filename)
    if safe_name == 'Unknown':
        raise ValueError(f"unusable filename after sanitize: {filename!r}")

    candidate = os.path.join(dst_dir, safe_name)
    base_real = os.path.realpath(dst_dir)
    cand_real = os.path.realpath(candidate)
    if cand_real != base_real and not cand_real.startswith(base_real + os.sep):
        raise ValueError(f"resolved path escapes dst_dir: {filename!r}")
    return candidate


# ── Release metadata parsers ─────────────────────────────────────────────────

def parse_release_group(title: str) -> str:
    """Extract release group from a manga release title. Empty string if none.

    Tries three strategies in order:
      1. First bracketed token that looks like a group name
      2. Last bracketed token at end of title (many releases put group last)
      3. Any bracketed token with 2-30 chars

    Skips tokens that are clearly metadata: file extensions, quality tags,
    hash strings (8+ hex chars), resolution markers."""
    _skip = re.compile(
        r'^(?:CBZ|CBR|EPUB|PDF|ZIP|MOBI|DIGITAL|SCAN|HQ|LQ|WEB|RAW|'
        r'\d{3,4}P|[0-9A-F]{8,}|V\d{1,2}|FIXED|REPACK|PROPER)$',
        re.IGNORECASE
    )
    brackets = re.findall(r'[\[\(]([^\[\]()]{2,30})[\]\)]', title)
    candidates = [b.strip() for b in brackets if not _skip.match(b.strip())]
    if candidates:
        return candidates[0]
    return ''


def parse_revision(title: str) -> dict:
    """Detect REPACK / PROPER / version-fix markers in a manga release title.

    Returns {'is_repack': bool, 'is_proper': bool, 'version': int}.

    Manga-specific rules (differs from Sonarr's video approach):
      1. REPACK / PROPER keywords: unambiguous.
      2. Bracketed [v2]/(v2): standard manga scene convention.
      3. Bare v2/v3: ONLY a version marker when a separate volume indicator
         exists (vol., volume, #, Japanese/Korean kanji). Prevents "v02.cbz"
         from being flagged as a repack of volume 1.
      4. FIXED keyword: common in manga — treated as a repack.
    """
    t = title.upper()

    is_proper = bool(re.search(r'\bPROPER\b', t))
    is_repack = bool(re.search(r'\bREPACK\b', t)) or is_proper
    is_repack = is_repack or bool(re.search(r'\bFIXED\b', t))
    version   = 1

    bm = re.search(r'[\[\(]V(\d{1,2})[\]\)]', t)
    if bm:
        v = int(bm.group(1))
        if v > 1:
            version   = v
            is_repack = True

    if not is_repack:
        has_other_vol = bool(re.search(
            r'\bVOL(?:UME)?\.?\s*\d|\b#\s*\d|\d\s*巻|\d\s*권', t
        ))
        v_tokens = list(re.finditer(r'\bV(\d{1,2})\b(?!\d)', t))

        if has_other_vol and v_tokens:
            for tok in v_tokens:
                v = int(tok.group(1))
                if v > 1:
                    version   = v
                    is_repack = True
                    break
        elif len(v_tokens) >= 2:
            for tok in v_tokens[1:]:
                v = int(tok.group(1))
                if v > 1:
                    version   = v
                    is_repack = True
                    break

    return {'is_repack': is_repack, 'is_proper': is_proper, 'version': version}


def detect_quality_from_title(title: str) -> str:
    """Return the quality key for a release based on its file extension in
    the title. Returns 'unknown' when no recognisable extension is found."""
    t = title.lower()
    for ext, quality in (
        ('.cbz',  'cbz'),
        ('.cbr',  'cbr'),
        ('.epub', 'epub'),
        ('.pdf',  'pdf'),
        ('.zip',  'zip'),
    ):
        if ext in t:
            return quality
    return 'unknown'


# ── Display labels ───────────────────────────────────────────────────────────

def build_volume_label(vol_num, vol_range, pack_type) -> str:
    """Human-readable label like 'Vol 5', 'Vol 1–5', 'Complete Series'."""
    if vol_num is not None:
        return f"Vol {vol_num_to_display(vol_num)}"
    if pack_type == 'complete':
        return "Complete Series"
    if pack_type == 'chapter':
        return "Chapter"
    if vol_range:
        return f"Vol {vol_num_to_display(vol_range[0])}–{vol_num_to_display(vol_range[1])}"
    return "Pack"


def _format_chapter_num(n: float) -> str:
    """Render a chapter number for display. Integers padded to 3 digits
    (Ch.001), decimals kept as-is (Ch.1.5)."""
    if n == int(n):
        return f"{int(n):03d}"
    return f"{n:g}"


def build_chapter_label(chapter_num: float | None,
                        chapter_range_end: float | None = None) -> str:
    """Build a human-readable chapter label.

    Examples:
      chapter_num=1, range_end=None  →  'Ch.001'
      chapter_num=1, range_end=2     →  'Ch.001-002'
      chapter_num=1.5, range_end=None →  'Ch.1.5'
    """
    if chapter_num is None:
        return ""
    start = _format_chapter_num(chapter_num)
    if chapter_range_end is not None and chapter_range_end > chapter_num:
        end = _format_chapter_num(chapter_range_end)
        return f"Ch.{start}-{end}"
    return f"Ch.{start}"


# ── Templated filename building ──────────────────────────────────────────────

def _apply_format_tokens(fmt: str, series_title: str,
                         volume_num: float | None = None,
                         chapter_num: float | None = None,
                         pub_year: int | None = None) -> str:
    """Apply supported template tokens to a format string.

    Supported tokens:
      {Series Title}, {Series.Title}, {Year},
      {Volume}, {Volume:NNd} (zero-padded),
      {Chapter}, {Chapter:NNd} (zero-padded).
    """
    safe_title  = sanitize_filename(series_title)
    dot_title   = safe_title.replace(' ', '.')
    year_str    = str(pub_year) if pub_year else ''

    name = fmt
    name = name.replace('{Series Title}', safe_title)
    name = name.replace('{Series.Title}', dot_title)
    name = name.replace('{Year}',         year_str)

    if volume_num is not None:
        name = re.sub(r'\{Volume:(\d+)d\}',
                      lambda m: vol_num_to_display(volume_num).zfill(int(m.group(1))), name)
        name = name.replace('{Volume}', vol_num_to_display(volume_num))

    if chapter_num is not None:
        ch_int = int(chapter_num) if chapter_num == int(chapter_num) else chapter_num
        name = re.sub(r'\{Chapter:(\d+)d\}',
                      lambda m: str(ch_int).zfill(int(m.group(1))), name)
        name = name.replace('{Chapter}', str(ch_int))

    return name.strip()


def build_filename(series_title: str, volume_num: float | None,
                   original_filename: str,
                   pub_year: int | None = None,
                   chapter_num: float | None = None) -> str:
    """Apply the configured file_format (or chapter_format for chapter
    files) template. Falls back to sanitize(basename(original_filename))
    when no template is set or on any exception."""
    ext = os.path.splitext(original_filename)[1]

    if chapter_num is not None:
        chapter_fmt = get_cfg('chapter_format', '').strip()
        file_fmt = get_cfg('file_format', '').strip()
        if chapter_fmt:
            fmt = chapter_fmt
        elif '{Chapter' in file_fmt and '{Volume' not in file_fmt:
            fmt = file_fmt
        else:
            fmt = ''
    else:
        fmt = get_cfg('file_format', '').strip()

    if not fmt:
        # Untrusted basename: strip to sanitized basename, never trust paths
        return sanitize_filename(os.path.basename(original_filename))

    try:
        name = _apply_format_tokens(fmt, series_title, volume_num, chapter_num, pub_year)
        return name + ext
    except Exception:
        return sanitize_filename(os.path.basename(original_filename))


def derive_special_title(series_title: str, original_filename: str) -> str:
    """Build an editable special title without duplicating the series prefix."""
    stem = os.path.splitext(os.path.basename(original_filename))[0].strip()
    prefix = re.compile(rf"^{re.escape(series_title)}(?:\s*[-:–—]\s*|\s+)", re.IGNORECASE)
    title = prefix.sub("", stem, count=1).strip(" -:–—")
    return sanitize_filename(title or stem or "Special")


def build_special_filename(
    series_title: str, special_title: str, original_filename: str
) -> str:
    """Return a stable filename that cannot be mistaken for mainline content."""
    ext = os.path.splitext(os.path.basename(original_filename))[1]
    safe_series = sanitize_filename(series_title)
    safe_title = sanitize_filename(special_title.strip() or "Special")
    return sanitize_filename(f"{safe_series} - Special - {safe_title}{ext}")


# ── Quality tiering ──────────────────────────────────────────────────────────

QUALITY_RANK: dict[str, int] = {
    'cbz':  5,
    'zip':  5,   # zip = cbz functionally
    'cbr':  4,
    'rar':  4,   # rar = cbr functionally
    'epub': 3,
    'mobi': 2,
    'pdf':  1,
}


def quality_from_filename(filename: str) -> str | None:
    """Return the quality tier string for a file. For files on disk,
    uses magic bytes (more reliable than extension). Falls back to
    extension-based detection for filenames without a path."""
    if filename and os.path.isfile(filename):
        magic_type = detect_file_type_magic(filename)
        if magic_type and magic_type in QUALITY_RANK:
            return magic_type
    ext = os.path.splitext(filename)[1].lstrip('.').lower()
    return ext if ext in QUALITY_RANK else None


def quality_rank(q: str | None) -> int:
    """Return numeric rank for a quality string. None/unknown = 0."""
    return QUALITY_RANK.get((q or '').lower(), 0)


# ── Edition / language / source-type classification ──────────────────────────

_EDITION_PATTERNS = [
    (r'\bofficial[\s-]?colou?r\b',                      'official_color'),
    (r'\bfull[\s-]?colou?r\b',                          'official_color'),
    (r'\bviz[\s-]?(?:full[\s-]?)?colou?r\b',           'official_color'),
    (r'\bdigital\s+colou?red?\b',                       'official_color'),
    (r'\bcolou?red?\s+edition\b',                       'colored'),
    (r'\bin\s+colou?r\b',                               'colored'),
    (r'\bcolou?red\b',                                  'colored'),
    (r'\bcoloredmanga\b',                               'colored'),
    (r'\bdeluxe\b',                                     'deluxe'),
    (r'\bhardcover\b|\bhc\b(?!\w)',                     'deluxe'),
    (r'\banniversary\s+edition\b',                      'deluxe'),
    (r'\bomnibus\b',                                    'omnibus'),
    (r'\bvizbig\b',                                     'omnibus'),
    (r'\bgrand\s+edition\b',                            'omnibus'),
    (r'\bperfect\s+edition\b',                          'omnibus'),
    (r'\bcollected\s+edition\b',                        'omnibus'),
    (r'\bcomplete\s+collection\b',                      'omnibus'),
    (r'\b3-in-1\b|\bthree-in-one\b',                    'omnibus'),
    (r'\b2-in-1\b|\btwo-in-one\b',                      'omnibus'),
    (r'\bcollector(?:\'?s)?\b',                         'collector'),
    (r'\bspecial\b(?:\s+edition)?',                     'special'),
    (r'\blimited\b(?:\s+edition)?',                     'special'),
    (r'\bcanonical\s+edition\b',                        'special'),
    (r'\bremaster(?:ed)?\b',                            'remaster'),
    (r'\bhd\s+edition\b',                               'remaster'),
    # Japanese print formats (PR #126). Tankobon = standard volume, Bunkoban
    # = pocket-size, Kanzenban / Aizoban = "complete" / collector reprints
    # roughly equivalent to omnibus but distinct in collector circles.
    (r'\baizoban\b|\b愛蔵版\b',                            'aizoban'),
    (r'\bkanzenban\b|\b完全版\b',                          'kanzenban'),
    (r'\bbunkoban\b|\b文庫版\b',                           'bunkoban'),
    (r'\btankoubon\b|\btankobon\b|\b単行本\b',             'tankobon'),
]

_LANGUAGE_PATTERNS = [
    (r'\b(?:english|eng)\b',              'en'),
    (r'\b(?:japanese?|jpn?)\b',           'ja'),
    (r'\b(?:french|fran[çc]ais|fre?)\b', 'fr'),
    (r'\b(?:german|deutsch|ger)\b',       'de'),
    (r'\b(?:spanish?|espa[ñn]ol|spa)\b', 'es'),
    (r'\b(?:italian[oe]?|ita)\b',        'it'),
    (r'\b(?:portuguese?|portugu[eê]s|por)\b', 'pt'),
    (r'\b(?:korean?|kor)\b',             'ko'),
    (r'\b(?:chinese?|mandarin|chi|chs|cht)\b', 'zh'),
    (r'\b(?:russian?|rus)\b',             'ru'),
    (r'\b(?:arabic|ara)\b',               'ar'),
    (r'\b(?:polish?|pol)\b',              'pl'),
    (r'\b(?:dutch|nederlanden?|dut)\b',  'nl'),
    (r'\b(?:thai|tha)\b',                'th'),
    (r'\b(?:vietnamese?|vie)\b',          'vi'),
    (r'\b(?:indonesian?|ind)\b',          'id'),
]


def detect_edition_type(title: str) -> str | None:
    """Detect edition type from a release title (Deluxe, Omnibus, etc.)."""
    tl = title.lower()
    for pattern, edition in _EDITION_PATTERNS:
        if re.search(pattern, tl):
            return edition
    return None


def detect_language(title: str) -> str | None:
    """Detect language code from a release title."""
    tl = title.lower()
    for pattern, lang in _LANGUAGE_PATTERNS:
        if re.search(pattern, tl):
            return lang
    return None


_OFFICIAL_PUBLISHER_PATTERNS: list[str] = [
    r'\bviz\s*(?:media|digital|big)?\b',
    r'\bkodansha\b',
    r'\bseven\s+seas\b',
    r'\byen\s+press\b',
    r'\bdark\s+horse\b',
    r'\bsquare\s+enix\b',
    r'\bj[-\s]?novel\s*(?:club)?\b',
    r'\bvertical\s+(?:comics?|inc\.?)\b',
    r'\btokyopop\b',
    r'\bshogakukan\b',
    r'\bshueisha\b',
    r'\bmanga\s*plus\b',
    r'\bone\s+peace\s+books\b',
    r'\bghost\s+ship\b',
    r'\bairship\b',
    r'\blezhin\b',
    r'\bwebtoons?\s+(?:official|originals?|canvas)\b',
    r'\btapas\s+media\b',
    r'\bcrunchyroll\s+manga\b',
    r'\bazuki\s+(?:digital|comics?|manga)\b',
]
_OFFICIAL_RE = re.compile('|'.join(_OFFICIAL_PUBLISHER_PATTERNS), re.IGNORECASE)

_FAN_GROUP_PATTERNS: list[str] = [
    r'\blucaz\b', r'\b1r0n\b', r'\bdanke\b', r'\bstick\b', r'\bjcafe\b',
    r'\bathena\b', r'\bdbs\b', r'\bcxc\b', r'\bhabanero\b', r'\btnt[-\s]empire\b',
    r'\bkc\b',
    r'\bclover\b', r'\bempire\b', r'\blostnere?varine\b',
]
_FAN_GROUP_RE = re.compile('|'.join(_FAN_GROUP_PATTERNS), re.IGNORECASE)


def is_official_release(title: str) -> bool:
    """Return True if the title contains a known licensed-publisher name."""
    return bool(_OFFICIAL_RE.search(title))


def is_quality_fan_release(title: str) -> bool:
    """Return True if the title matches a known quality fan-scanlation group."""
    return bool(_FAN_GROUP_RE.search(title))


def classify_source_type(title: str) -> str:
    """Classify a release title as 'official' or 'fan'."""
    return 'official' if is_official_release(title) else 'fan'


# ── Magic-byte file-type detection ───────────────────────────────────────────

_MAGIC_ZIP  = b'PK\x03\x04'       # ZIP / CBZ / EPUB
_MAGIC_RAR4 = b'Rar!\x1a\x07\x00' # RAR v4
_MAGIC_RAR5 = b'Rar!\x1a\x07\x01' # RAR v5
_MAGIC_PDF  = b'%PDF'


def detect_file_type_magic(path: str) -> str | None:
    """Detect the actual file type by reading magic bytes.
    Returns 'cbz', 'cbr', 'epub', 'pdf', or None."""
    try:
        with open(path, 'rb') as f:
            header = f.read(8)
    except OSError:
        return None
    if header[:4] == _MAGIC_ZIP:
        try:
            with zipfile.ZipFile(path, 'r') as zf:
                if 'mimetype' in zf.namelist():
                    mt = zf.read('mimetype').decode('ascii', errors='ignore').strip()
                    if 'epub' in mt:
                        return 'epub'
        except Exception:
            pass
        return 'cbz'
    if header[:8] in (_MAGIC_RAR4, _MAGIC_RAR5):
        return 'cbr'
    if header[:7] in (_MAGIC_RAR4[:7], _MAGIC_RAR5[:7]):
        return 'cbr'   # partial header match
    if header[:4] == _MAGIC_PDF:
        return 'pdf'
    return None


# ── CBR → CBZ conversion ─────────────────────────────────────────────────────

def convert_cbr_to_cbz(cbr_path: str) -> str | None:
    """Convert a CBR (RAR) archive to a CBZ (ZIP) file.
    Creates a new .cbz file alongside the original .cbr.
    Returns the path to the new CBZ on success, None on failure.
    The original CBR is NOT removed — caller decides."""
    try:
        import rarfile as _rarfile
    except ImportError:
        log_event("error", "[CBR→CBZ] rarfile not available; cannot convert CBR")
        return None

    cbz_path = os.path.splitext(cbr_path)[0] + '.cbz'
    try:
        with _rarfile.RarFile(cbr_path, 'r') as rf:
            entries = [
                (name, rf.read(name))
                for name in rf.namelist()
                if not rf.getinfo(name).is_dir()
                and not name.lower().endswith('comicinfo.xml')
            ]
        if not entries:
            return None
        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return cbz_path
    except Exception as e:
        log_event("error", f"[CBR→CBZ] Failed to convert {cbr_path}: {e}")
        if os.path.exists(cbz_path):
            try:
                os.remove(cbz_path)
            except OSError:
                pass
        return None


# ── Auto-pack image-only chapter directories into CBZs ─────────────────────
# Some torrents arrive as a directory of raw page images (001.jpg, 002.jpg,
# ...) instead of a CBZ archive. Mangarr's import scanner only looks at
# MANGA_EXTENSIONS (cbz/cbr/zip/...), so these directories produce
# "No manga files found" and the import never completes.
#
# Auto-pack: detect leaf directories that contain only image files (no
# archives, no subdirs) and pack them into CBZs in a staging area. The
# staged CBZ is then handed to the normal import flow.

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}
_PACK_SKIP_FILES = {'.ds_store', 'thumbs.db', 'desktop.ini', 'comicinfo.xml'}


def _is_pageable_image(fname: str) -> bool:
    """True iff fname is an image file we'd include in a packed CBZ."""
    if fname.lower() in _PACK_SKIP_FILES:
        return False
    return os.path.splitext(fname)[1].lower() in _IMAGE_EXTS


def find_image_only_chapter_dirs(src_dir: str) -> list[str]:
    """Walk src_dir and return every LEAF directory (no subdirectories)
    that contains only image files — no archive files matching
    MANGA_EXTENSIONS, no further subdirs.

    Empty dirs are skipped. Dirs containing both an archive and images
    are skipped (the archive path covers them; images would duplicate).
    Dirs with junk metadata files (Thumbs.db, .DS_Store, ComicInfo.xml)
    plus images count as image-only.
    """
    leafs: list[str] = []
    for root, dirs, files in os.walk(src_dir):
        if dirs:
            # Not a leaf — children will be visited separately.
            continue
        archive_count = 0
        image_count = 0
        for f in files:
            if f.lower() in _PACK_SKIP_FILES:
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in MANGA_EXTENSIONS:
                archive_count += 1
            elif ext in _IMAGE_EXTS:
                image_count += 1
        if archive_count == 0 and image_count > 0:
            leafs.append(root)
    return leafs


def pack_image_dir_to_cbz(src_dir: str, dst_cbz: str) -> int | None:
    """Pack all image files from src_dir into a CBZ at dst_cbz.

    Sorts filenames lexicographically (matches the natural page order
    for typical naming schemes like 001.jpg, 002.jpg, ...). Junk
    metadata files are skipped. ZIP_STORED (no compression) — page
    images are already JPEG/PNG-compressed; ZIP_DEFLATED would burn
    CPU for ~0.1% size reduction.

    Creates parent directory of dst_cbz if it doesn't exist.

    Returns the byte size of the resulting CBZ, or None on failure.
    """
    try:
        import zipfile as _zf
        files = sorted(
            f for f in os.listdir(src_dir)
            if _is_pageable_image(f)
            and os.path.isfile(os.path.join(src_dir, f))
        )
        if not files:
            return None
        os.makedirs(os.path.dirname(dst_cbz), exist_ok=True)
        with _zf.ZipFile(dst_cbz, 'w', _zf.ZIP_STORED) as zf:
            for f in files:
                zf.write(os.path.join(src_dir, f), arcname=f)
        return os.path.getsize(dst_cbz)
    except Exception as e:
        log_event("error", f"[auto-pack] Failed to pack {src_dir} → {dst_cbz}: {e}")
        # Clean up partial archive on failure
        try:
            if os.path.exists(dst_cbz):
                os.remove(dst_cbz)
        except OSError:
            pass
        return None


def _maybe_convert_to_cbz(path: str) -> str:
    """If path is a CBR file (detected by magic bytes), convert to CBZ,
    remove the original, and return the new .cbz path. For other types
    (CBZ, EPUB, PDF) returns path unchanged. Non-fatal on conversion
    failure — returns original path."""
    if not path or not os.path.isfile(path):
        return path
    file_type = detect_file_type_magic(path)
    if file_type != 'cbr':
        return path
    cbz_path = convert_cbr_to_cbz(path)
    if cbz_path and os.path.isfile(cbz_path):
        if os.path.abspath(cbz_path) != os.path.abspath(path):
            try:
                os.remove(path)
                log_event(
                    "import",
                    f"[CBR→CBZ] Converted and removed original: {os.path.basename(path)}",
                )
            except OSError as e:
                log_event("error", f"[CBR→CBZ] Converted but could not remove original: {e}")
        else:
            log_event(
                "import",
                f"[CBR→CBZ] Converted in-place (was CBR with .cbz extension): {os.path.basename(path)}",
            )
        return cbz_path
    return path
