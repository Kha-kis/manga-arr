"""Canonical import classifications shared by discovery, review, and commit."""

VALID_IMPORT_KINDS = frozenset(
    {
        "volume",
        "volume_range",
        "chapter",
        "chapter_range",
        "special",
        "skip",
    }
)


def infer_import_kind(
    *,
    file_type: str | None,
    pack_type: str | None,
    is_special: int | bool,
    volume_range_end: float | None,
    chapter_range_end: float | None,
) -> str:
    """Return the durable import kind for parser output or a legacy row."""
    if is_special or pack_type == "special":
        return "special"
    if chapter_range_end is not None or pack_type == "chapter_range":
        return "chapter_range"
    if volume_range_end is not None or pack_type == "volume_range":
        return "volume_range"
    if file_type == "chapter" or pack_type == "chapter":
        return "chapter"
    return "volume"


def normalize_import_kind(value: str | None, **legacy_fields) -> str:
    """Validate a stored kind, falling back to legacy queue columns."""
    normalized = (value or "").strip().lower()
    if normalized in VALID_IMPORT_KINDS:
        return normalized
    return infer_import_kind(**legacy_fields)
