"""Built-in custom-format library + profile presets (PR #127).

A fresh Mangarr install gets a TRaSH-Guides-style starter setup the
moment `init_db` runs:

  - 10 built-in custom formats covering source provenance (Official
    Digital / Quality Scanlation / Japanese Raw), edition (Omnibus /
    Deluxe / Tankobon / Kanzenban / Aizoban / Color), and one penalty
    (PDF — usually a worse rip than CBZ).

  - 4 profile presets that wire those CFs together for the four
    realistic manga-collector setups:
      * Best Available (default)  — prefer official, allow scanlation
      * Official Digital Only     — hard-reject scanlations + raws
      * Scanlations OK            — prefer scanlation tier 1, reject raws
      * Japanese Raw              — prefer raw, prefer collector formats

Existing installs (where any custom_format already exists) are NOT
touched — these defaults are seed-only-on-fresh-install. Users who want
the presets later can copy them via the JSON import/export coming in
PR #128.

The score values follow TRaSH-Guides conventions:
  +1500 / +500   — strong preference (clear winner over alternatives)
  +200 / +100    — mild preference (tier rank when several pass)
  +30 / +50      — minor preference (tie-breaker)
  -100           — minor penalty (avoid if anything better exists)
  -10000         — hard reject (do not download under any circumstance)
"""

# ── Built-in custom formats ──────────────────────────────────────────
# Keys mirror the SPEC_TYPES enum in app/routers/custom_formats.py.
# Each format has a name (UNIQUE in DB) and a list of specs that all
# must match for the format to be considered "matched".

BUILTIN_CUSTOM_FORMATS = [
    {
        "name": "Official Digital",
        "specs": [
            {"type": "source_is", "value": "official_digital", "negate": False},
        ],
    },
    {
        "name": "Quality Scanlation",
        "specs": [
            {"type": "source_is", "value": "scanlation", "negate": False},
        ],
    },
    {
        "name": "Japanese Raw",
        "specs": [
            {"type": "source_is", "value": "raw", "negate": False},
        ],
    },
    {
        "name": "Color Edition",
        "specs": [
            {"type": "edition_is", "value": "official_color", "negate": False},
        ],
    },
    {
        "name": "Omnibus",
        "specs": [
            {"type": "edition_is", "value": "omnibus", "negate": False},
        ],
    },
    {
        "name": "Deluxe / Hardcover",
        "specs": [
            {"type": "edition_is", "value": "deluxe", "negate": False},
        ],
    },
    {
        "name": "Tankobon",
        "specs": [
            {"type": "edition_is", "value": "tankobon", "negate": False},
        ],
    },
    {
        "name": "Kanzenban",
        "specs": [
            {"type": "edition_is", "value": "kanzenban", "negate": False},
        ],
    },
    {
        "name": "Aizoban",
        "specs": [
            {"type": "edition_is", "value": "aizoban", "negate": False},
        ],
    },
    {
        "name": "Bunkoban",
        "specs": [
            {"type": "edition_is", "value": "bunkoban", "negate": False},
        ],
    },
    {
        "name": "PDF (avoid)",
        "specs": [
            {"type": "release_title_contains", "value": r"\.pdf\b|\bpdf\b",
             "negate": False},
        ],
    },
]


# ── Profile presets ─────────────────────────────────────────────────
# Each preset wires the built-in CFs to per-profile scores via the
# `scores` dict (CF name → integer score). The seed code looks up the
# CF id by name at insert time.

PROFILE_PRESETS = [
    {
        "name": "Best Available",
        "is_default": True,
        "qualities": '["cbz","epub","cbr","pdf","zip","unknown"]',
        "cutoff": "cbz",
        "upgrades_allowed": 1,
        "cutoff_format_score": 10000,
        "min_upgrade_format_score": 10,
        "minimum_custom_format_score": 0,
        "scores": {
            "Official Digital":   500,
            "Quality Scanlation": 200,
            "Color Edition":      100,
            "Omnibus":             50,
            "Deluxe / Hardcover":  30,
            "PDF (avoid)":       -100,
        },
    },
    {
        "name": "Official Digital Only",
        "is_default": False,
        "qualities": '["cbz","epub","cbr","pdf","zip"]',
        "cutoff": "cbz",
        "upgrades_allowed": 1,
        "cutoff_format_score": 10000,
        "min_upgrade_format_score": 10,
        "minimum_custom_format_score": 0,
        "scores": {
            "Official Digital":     500,
            "Color Edition":        100,
            "Quality Scanlation": -10000,
            "Japanese Raw":       -10000,
            "PDF (avoid)":         -100,
        },
    },
    {
        "name": "Scanlations OK",
        "is_default": False,
        "qualities": '["cbz","epub","cbr","pdf","zip","unknown"]',
        "cutoff": "cbz",
        "upgrades_allowed": 1,
        "cutoff_format_score": 10000,
        "min_upgrade_format_score": 10,
        "minimum_custom_format_score": 0,
        "scores": {
            "Quality Scanlation":  500,
            "Official Digital":    500,
            "Color Edition":       100,
            "Omnibus":              50,
            "Japanese Raw":     -10000,
            "PDF (avoid)":        -100,
        },
    },
    {
        "name": "Japanese Raw",
        "is_default": False,
        "qualities": '["cbz","epub","cbr","pdf","zip","unknown"]',
        "cutoff": "cbz",
        "upgrades_allowed": 1,
        "cutoff_format_score": 10000,
        "min_upgrade_format_score": 10,
        "minimum_custom_format_score": 0,
        "scores": {
            "Japanese Raw":      1000,
            "Aizoban":           1000,
            "Kanzenban":          500,
            "Tankobon":           200,
            "Bunkoban":           100,
            "Quality Scanlation": -10000,
            "Official Digital":  -10000,
        },
    },
]
