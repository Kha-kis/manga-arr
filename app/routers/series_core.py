"""Core series helpers - no DB context, no FastAPI routes.

This module contains shared series manipulation logic used by series_.py
and other routers. It has no database calls DB must be passed explicitly
by the caller.

Extracted from app/routers/series_.py lines 1-250.
"""
from collections import defaultdict
import json


def _chapter_map_to_ranges(chapter_vol_map_json: str | None) -> str:
    """Convert {ch_str: vol_int} JSON to human-readable 'one range per line' format."""
    if not chapter_vol_map_json:
        return ''
    try:
        cvm = json.loads(chapter_vol_map_json)
    except Exception:
        return ''
    vol_to_chs: dict[int, list[int]] = defaultdict(list)
    for ch_str, vol_num in cvm.items():
        try:
            vol_to_chs[int(vol_num)].append(int(float(ch_str)))
        except (ValueError, TypeError):
            pass
    lines = []
    for vol_num in sorted(vol_to_chs.keys()):
        chs = sorted(vol_to_chs[vol_num])
        if not chs:
            continue
        if len(chs) == 1:
            lines.append(str(chs[0]))
        elif chs[-1] - chs[0] + 1 == len(chs):
            lines.append(f"{chs[0]}-{chs[-1]}")
        else:
            lines.append(', '.join(str(c) for c in chs))
    return '\n'.join(lines)


def _parse_chapter_ranges(text: str) -> dict[str, int] | None:
    """Parse 'one range per line' chapter map into {ch_str: vol_int}."""
    mapping: dict[str, int] = {}
    vol_num = 0
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        vol_num += 1
        for part in line.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                halves = part.split('-', 1)
                try:
                    start, end = int(halves[0].strip()), int(halves[1].strip())
                    if start > end or end - start > 500:
                        return None
                    for ch in range(start, end + 1):
                        mapping[str(ch)] = vol_num
                except ValueError:
                    return None
            else:
                try:
                    mapping[str(int(part))] = vol_num
                except ValueError:
                    return None
    return mapping if mapping else None
