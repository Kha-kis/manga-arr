"""Title parsing, matching, and volume/chapter extraction — pure functions.

Second module extracted from main.py as part of the structural split.
Everything here is stateless: no DB access, no CONFIG reads. That's
what lets this module sit low in the dependency graph — grab, import,
and metadata layers can all import from parsing without creating
cycles.

What's NOT in this module (and why):
  - score_release / evaluate_release — DB-coupled; stay in main.py
    until the grab layer is extracted.
  - parse_release_group / parse_revision / detect_quality_from_title
    — pure, but tightly clustered with file-naming helpers in main.py.
    Move with files.py in a later PR.
  - build_volume_label / build_chapter_label — pure formatters but
    clustered with history/filename helpers; move with files.py.

Pure move: no behaviour changes. main.py re-exports everything here.
"""
from __future__ import annotations

import difflib
import re


# ── Normalisation ────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    text = re.sub(r'\[.*?\]|\(.*?\)', ' ', text)
    text = re.sub(r'[^\w\s]', ' ', text.lower())
    text = text.replace('_', ' ')   # treat underscores as word separators
    return re.sub(r'\s+', ' ', text).strip()


# ── Language rejection ───────────────────────────────────────────────────────

_LANG_REJECT_RE = re.compile(
    r'\b(?:french|francais|fran[çc]ais|vostfr|español|espanol|spanish|'
    r'italian[eo]?|german|deutsch|portuguese|portugu[eê]s|russian|'
    r'polish|dutch|indonesian|malay|vietnamese|thai|arabic|turkish|'
    r'japanese|unlocalized)\b'
    r'|\[(?:fr|es|de|it|pt|ru|pl|nl|id|ms|vi|th|ar|tr|jp|jpn|raw)\]'
    r'|\((?:jp|jpn|raw)\)'        # (Raw), (JPN), (JP) in parens
    r'|(?<!\w)vf(?!\w)',          # VF as standalone token (French)
    re.IGNORECASE,
)


def is_foreign_language(title: str) -> bool:
    """Return True if the release title contains non-English language markers."""
    return bool(_LANG_REJECT_RE.search(title))


# ── Fuzzy title matching ─────────────────────────────────────────────────────

FUZZY_MATCH_THRESHOLD = 0.75  # minimum SequenceMatcher ratio


def _extract_series_portion(torrent_title: str) -> str:
    """Strip volume/chapter numbers and trailing metadata from a torrent
    title to isolate the series-name portion for fuzzy comparison.
    e.g. "[Group] One Piece v01 [Digital]" → "one piece"
    """
    t = normalize(torrent_title)
    t = re.sub(
        r'\s*(?:v|vol\.?|volume|ch\.?|chapter|#)\s*\d.*$', '', t,
        flags=re.IGNORECASE
    )
    return t.strip()


def matches(pattern: str, torrent_title: str,
            threshold: float = FUZZY_MATCH_THRESHOLD,
            pub_year: int | None = None) -> bool:
    """Fuzzy title match using difflib SequenceMatcher ratio with word-
    boundary guard for short patterns (prevents "Vagabond" matching
    "Vagabonde"). If pub_year is provided and the torrent title contains
    a (YYYY) year token, reject if the years differ by more than 1."""
    if not pattern or not torrent_title:
        return False

    norm_pattern = normalize(pattern)
    series_portion = _extract_series_portion(torrent_title)

    if not norm_pattern or not series_portion:
        return False

    ratio = difflib.SequenceMatcher(None, norm_pattern, series_portion).ratio()

    pattern_words = norm_pattern.split()
    torrent_words = set(series_portion.split())

    # Word-boundary guard: all pattern words must appear verbatim as whole
    # words in the torrent title word set.
    if not all(w in torrent_words for w in pattern_words if len(w) > 2):
        return False

    if ratio < threshold:
        return False

    # Year tolerance: if the torrent title has an explicit (YYYY) token and
    # the series has a known pub_year, reject if they differ by more than 1.
    if pub_year:
        yr_m = re.search(r'\b((?:19|20)\d{2})\b', torrent_title)
        if yr_m:
            torrent_year = int(yr_m.group(1))
            if abs(torrent_year - pub_year) > 1:
                return False

    return True


# ── Volume number suffix parsing ─────────────────────────────────────────────

_LETTER_SUFFIX_MAP = {'a': 0.01, 'b': 0.02, 'c': 0.03, 'd': 0.04}
_FRAC_SUFFIX_MAP   = {'½': 0.5, '¼': 0.25, '¾': 0.75}

# Roman numerals — supports I through MMMCMXCIX (3999)
_ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50,
                 'C': 100, 'D': 500, 'M': 1000}


def _roman_to_int(s: str) -> int | None:
    """Convert a Roman numeral string to int. Returns None if not valid or > 30."""
    s = s.upper().strip()
    if not s or not all(c in _ROMAN_VALUES for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        val = _ROMAN_VALUES[c]
        total = total - val if val < prev else total + val
        prev = val
    # Sanity check: manga/comic volumes rarely exceed 30 in Roman numerals
    return total if 0 < total <= 30 else None


def _parse_vol_suffix(raw: str) -> float | None:
    """Convert raw volume token to float, handling letter/fraction suffixes.
      '1'  -> 1.0   '3a' -> 3.01   '3b' -> 3.02
      '3½' -> 3.5   '3¼' -> 3.25   '3¾' -> 3.75
    Returns None on parse failure."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    # Trailing letter suffix  e.g. "3a"
    m = re.match(r'^(\d+(?:\.\d+)?)([a-d])$', raw, re.IGNORECASE)
    if m:
        return float(m.group(1)) + _LETTER_SUFFIX_MAP.get(m.group(2).lower(), 0)
    # Unicode fraction suffix  e.g. "3½"
    for frac, offset in _FRAC_SUFFIX_MAP.items():
        if raw.endswith(frac):
            base_part = raw[:-len(frac)]
            try:
                return (float(base_part) if base_part else 0.0) + offset
            except ValueError:
                pass
    return None


def vol_num_to_display(vol_num) -> str:
    """Format a float volume number for human display.
    None->''  3.0->3  3.01->3a  3.02->3b  3.5->3½  3.25->3¼  3.75->3¾  3.14->3.14
    """
    if vol_num is None:
        return ''
    _INT_TO_LETTER = {1: 'a', 2: 'b', 3: 'c', 4: 'd'}
    _INT_TO_FRAC   = {50: '½', 25: '¼', 75: '¾'}
    try:
        base = int(vol_num)
        frac = round((float(vol_num) - base) * 100)
    except (TypeError, ValueError):
        return str(vol_num)
    if frac == 0:
        return str(base)
    if frac in _INT_TO_LETTER:
        return f"{base}{_INT_TO_LETTER[frac]}"
    if frac in _INT_TO_FRAC:
        return f"{base}{_INT_TO_FRAC[frac]}"
    return f"{float(vol_num):g}"


# ── Volume / chapter number extraction ───────────────────────────────────────

def extract_volume_num(title: str) -> float | None:
    """Extract a single volume number from a release title. See main.py's
    original docstring for the full derivation — this is a pure move."""
    if not title:
        return None

    # Volume ranges belong to extract_volume_range. Don't also hand back
    # the start as if it were a single-volume release. (D9)
    if extract_volume_range(title):
        return None

    # Omnibus / box set markers
    m = re.search(r'\b(?:omnibus|box[\s-]?set)\s+(\d{1,3})\b', title, re.IGNORECASE)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val

    # Asian language markers (highest priority, unambiguous)
    m = re.search(r'(?:第\s*)?(\d{1,3})\s*巻', title)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val
    m = re.search(r'(\d{1,3})\s*권', title)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val

    # Underscore-separated volume: Volume_0001, Vol_001
    m = re.search(r'(?<![A-Za-z])v(?:ol(?:ume)?)?_(\d{1,4})(?!\d)', title, re.IGNORECASE)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val

    # Roman numeral volumes: "Volume III", "Vol. X"
    m = re.search(r'\bv(?:ol(?:ume)?)?\.?\s+([IVXLCDM]{1,6})\b', title, re.IGNORECASE)
    if m:
        val = _roman_to_int(m.group(1))
        if val is not None:
            return float(val)

    # Standard numeric vol markers
    marker_match = re.search(
        r'\b(?:vol(?:ume)?\.?|v(?=\s*\d))\s*\d|#\s*\d',
        title, re.IGNORECASE
    )
    marker_pos = marker_match.start() if marker_match else len(title)

    _num = r'(\d{1,3}(?:\.\d+)?[a-d½¼¾]?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)'
    patterns = [
        rf'\bv(?:ol(?:ume)?)?\.?\s*{_num}',
        rf'\bvolume\s+{_num}',
        rf'\b#{_num}',
    ]
    for pat in patterns:
        for m in re.finditer(pat, title, re.IGNORECASE):
            if m.start() < marker_pos and m.end() <= marker_pos:
                continue
            val = _parse_vol_suffix(m.group(1))
            if val is not None:
                return val
    return None


def extract_volume_range(title: str) -> tuple[float, float] | None:
    """Extract a volume range from a pack title. Returns (start, end) or None.
    Supports letter/fraction suffixes: v1a-v5b, v3½-v7.

    A chapter range is NOT a volume range — extract_chapter_range owns those."""
    if not title:
        return None
    if extract_chapter_range(title):
        return None
    _sfx = r'[a-d½¼¾]?'
    patterns = [
        rf'\bv(?:ol(?:ume)?)?\.?\s*(\d{{1,4}}(?:\.\d+)?{_sfx})\s*[-–—~]\s*(?:v(?:ol(?:ume)?)?\.?\s*)?(\d{{1,4}}(?:\.\d+)?{_sfx})\b',
        rf'\[(\d{{1,4}}(?:\.\d+)?{_sfx})\s*[-–—~]\s*(\d{{1,4}}(?:\.\d+)?{_sfx})\]',
        r'(?:^|[\s(])\b(\d{1,3})\s*[-–—]\s*(\d{1,3})\b(?=[\s),\[]|$)',
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            start = _parse_vol_suffix(m.group(1))
            end   = _parse_vol_suffix(m.group(2))
            if start is not None and end is not None:
                if start < end and (end - start) < 600:
                    return (start, end)
    return None


def extract_chapter_range(title: str) -> tuple[float, float] | None:
    """Extract a chapter range from a release/file name. Returns (start, end) or None.

    Mirrors extract_volume_range but pinned to chapter prefixes (`c`,
    `ch`, `chapter`) so a bare `1-2` doesn't get misread as chapters."""
    if not title:
        return None
    _sfx = r'[a-d½¼¾]?'
    pat = (
        rf'\bc(?:h(?:a(?:p(?:ter)?)?)?)?s?\.?\s*'
        rf'(\d{{1,4}}(?:\.\d+)?{_sfx})'
        rf'\s*[-–—~]\s*'
        rf'(?:c(?:h(?:a(?:p(?:ter)?)?)?)?s?\.?\s*)?'
        rf'(\d{{1,4}}(?:\.\d+)?{_sfx})\b'
    )
    m = re.search(pat, title, re.IGNORECASE)
    if not m:
        return None
    start = _parse_vol_suffix(m.group(1))
    end   = _parse_vol_suffix(m.group(2))
    if start is None or end is None:
        return None
    if start >= end:
        return None
    if (end - start) > 2000:
        return None
    return (start, end)


def extract_chapter_num(title: str) -> float | None:
    """Extract a single chapter number from a release name.
    Returns None for ranges, non-chapter titles, or when no number is found."""
    if not title:
        return None
    if extract_chapter_range(title):
        return None
    if extract_volume_range(title):
        return None

    # Japanese episode marker: 第3話
    m = re.search(r'第\s*(\d{1,4}(?:\.\d+)?)\s*話', title)
    if m:
        val = _parse_vol_suffix(m.group(1))
        if val is not None:
            return val

    _num = r'(\d{1,4}(?:\.\d+)?[a-d½¼¾]?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)'
    patterns = [
        rf'\bch(?:a(?:p(?:ter)?)?)?s?\.?\s*{_num}',
        rf'\bep(?:isode)?\.?\s*{_num}',
        rf'\bc(\d{{2,4}}(?:\.\d+)?[a-d½¼¾]?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)',
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            val = _parse_vol_suffix(m.group(1))
            if val is not None:
                return val

    # Bare-number fallback — only when no explicit vol/chapter prefix present.
    has_vol = (
        bool(re.search(r'\bv(?:ol(?:ume)?)?\.?\s*\d', title, re.IGNORECASE))
        or bool(re.search(r'(?<![A-Za-z])v(?:ol(?:ume)?)?_\d', title, re.IGNORECASE))
        or bool(re.search(r'(?:第\s*)?\d+\s*[巻券]|\d+\s*권', title))
        or bool(re.search(r'\b(?:omnibus|box[\s-]?set)\b', title, re.IGNORECASE))
    )
    has_chap = bool(re.search(
        r'\bch(?:a(?:p(?:ter)?)?)?s?\.?\s*\d|\bep(?:isode)?\.?\s*\d|\bc\d{2,}',
        title, re.IGNORECASE,
    ))
    if not has_vol and not has_chap:
        clean = re.sub(
            r'\b(?:720|1080|2160|480|360|4k|8k)\s*p\b'
            r'|\b\d+\s*(?:MB|GB|KB|MiB|GiB|KiB)\b'
            r'|\b(?:19|20)\d{2}\b',
            '', title, flags=re.IGNORECASE
        )
        m = re.search(r'(?<![.\d])(\d{1,4}(?:\.\d+)?)(?!\d)(?![gGmMkK][bBiI]|[pP]\b)', clean)
        if m:
            val = _parse_vol_suffix(m.group(1))
            if val is not None and val <= 9999:
                return val
    return None


# ── Pack detection ───────────────────────────────────────────────────────────

def is_complete_pack(title: str, total_volumes: int | None = None) -> bool:
    """Returns True if the title indicates a complete/full series pack.
    Pass total_volumes to also detect range packs that span the whole series."""
    markers = [
        'complete series', 'complete collection', 'complete pack', 'full series',
        'entire series', 'all volumes', 'complete set', 'omnibus complete',
        'complete manga', 'whole series',
    ]
    t = title.lower()
    if any(m in t for m in markers):
        return True
    m = re.search(r'\((\d{4})\s*[-–]\s*(\d{4})\)', title)
    if m:
        try:
            if int(m.group(2)) - int(m.group(1)) >= 3:
                return True
        except ValueError:
            pass
    if total_volumes and total_volumes > 0:
        rng = extract_volume_range(title)
        if rng and rng[0] <= 1 and rng[1] >= total_volumes * 0.9:
            return True
    return False


def detect_pack_type(title: str, vol_range: tuple | None,
                     total_volumes: int | None = None) -> str:
    """Returns 'complete', 'chapter', or 'volume' for a pack release.
    Full ordering contract documented in main.py's original site (still
    authoritative — this is a pure move)."""
    if is_complete_pack(title, total_volumes):
        return 'complete'
    if extract_chapter_range(title):
        return 'chapter'
    t = title.lower()
    if re.search(r'\bch(?:apter)?s?[\s.]', t) or re.search(r'\bc\d{2,}', t):
        return 'chapter'
    vol_num = extract_volume_num(title)
    if vol_num is not None and not vol_range:
        return 'volume'
    if not vol_range:
        single_m = re.search(r'(?:^|[\s\[({])(\d{2,4})(?:[\s\])}]|$)', title)
        if single_m and total_volumes and total_volumes > 0:
            num = float(single_m.group(1))
            if num > total_volumes * 1.5:
                return 'chapter'
        if single_m:
            num = float(single_m.group(1))
            if num > 60 and not re.search(r'\bv(?:ol)?[\s.]', t):
                return 'chapter'
        return 'volume'
    start, end = vol_range
    if total_volumes and total_volumes > 0:
        if start > total_volumes * 1.5 or end > total_volumes * 2:
            return 'chapter'
    if end > 60 and not re.search(r'\bv(?:ol)?[\s.]', t):
        return 'chapter'
    return 'volume'


# ── Special / side-story detection ───────────────────────────────────────────

_SPECIAL_RELEASE_PATTERNS = (
    r'\bspecial\b',
    r'\bextras?\b',
    r'\bone[-\s]?shot\b',
    r'\bgaiden\b',
    r'\bside[-\s]?stor(?:y|ies)\b',
    r'\bsidestory\b',
    r'\bshort[-\s]+stor(?:y|ies)\b',
    r'\bbonus\b',
    r'\bomake\b',
)


def is_special_release(title: str) -> bool:
    """Detect titles that look like side-stories / oneshots / specials.
    Detection-only — a True here means "operator review is needed
    before this satisfies mainline volumes/chapters"."""
    if not title:
        return False
    t = title.lower()
    for pat in _SPECIAL_RELEASE_PATTERNS:
        if re.search(pat, t):
            return True
    if '外伝' in title:
        return True
    return False
