"""Regression test for Kitsu fallback chapter→volume map preserving
half-chapters.

The bug (found during the May-3 audit): `app/metadata.py:434` built
the chapter_vol_map keys as `str(int(float(ch_num)))`. For chapters
like "0.5", "1.5", "168.1" the int() truncated to "0", "1", "168" —
colliding with the integer chapters. Last-write-wins meant the half-
chapters silently overwrote (or got overwritten by) their integer
neighbours.

Production data wasn't affected (the user's library was populated via
the MangaDex path which preserves fractions correctly). But Kitsu IS
the documented fallback when MangaDex has no chapter data, so any
series triggering that path on a manga with half-chapters would hit
the bug — silent metadata corruption.

The fix preserves fractional keys: "0", "0.5", "1", "1.5" all become
distinct dict entries.
"""
import sys
import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


def test_kitsu_map_preserves_half_chapters():
    import asyncio
    asyncio.run(_test_kitsu_map_preserves_half_chapters_async())


async def _test_kitsu_map_preserves_half_chapters_async():
    """Half-chapters must map to fractional keys, not collide on int()."""
    from metadata import fetch_kitsu_chapter_map

    # Stub: kitsu_id=1, then chapter pagination returns half-chapters
    page_data = {
        'data': [
            {'attributes': {'number': '1',    'volumeNumber': '1'}},
            {'attributes': {'number': '1.5',  'volumeNumber': '1'}},
            {'attributes': {'number': '2',    'volumeNumber': '2'}},
            {'attributes': {'number': '2.5',  'volumeNumber': '2'}},
            {'attributes': {'number': '168',  'volumeNumber': '17'}},
            {'attributes': {'number': '168.1','volumeNumber': '17'}},
        ],
        'links': {},
    }
    empty_page = {'data': [], 'links': {}}

    # Mock the manga lookup (returns kitsu_id=1) and the chapter pagination
    async def _fake_get(url, **kw):
        rsp = MagicMock()
        if 'manga' in url:
            rsp.json = lambda: {
                'data': [{'id': '1', 'attributes': {'titles': {'en': 'Test'}}}]
            }
        elif 'chapters' in url:
            offset = kw.get('params', {}).get('page[offset]', 0)
            rsp.json = lambda: page_data if offset == 0 else empty_page
        else:
            rsp.json = lambda: {'data': []}
        return rsp

    with patch('metadata.httpx.AsyncClient') as MockClient:
        mock_inst = MockClient.return_value.__aenter__.return_value
        mock_inst.get = _fake_get
        result = await fetch_kitsu_chapter_map('Test Series', None, None)

    # Distinct keys for half + integer chapters
    assert '1' in result, f"missing integer key '1' in {result}"
    assert '1.5' in result, f"missing half-chapter key '1.5' in {result}"
    assert '2' in result
    assert '2.5' in result
    assert '168' in result
    assert '168.1' in result

    # Each maps to its correct volume
    assert result['1']     == 1
    assert result['1.5']   == 1
    assert result['2']     == 2
    assert result['2.5']   == 2
    assert result['168']   == 17
    assert result['168.1'] == 17


def test_kitsu_map_integer_chapter_uses_integer_string():
    import asyncio
    asyncio.run(_test_kitsu_map_integer_async())


async def _test_kitsu_map_integer_async():
    """Whole-number chapters use the integer-string form ('1' not '1.0')
    so they match the rest of the codebase's lookup format."""
    from metadata import fetch_kitsu_chapter_map

    page_data = {
        'data': [
            {'attributes': {'number': '7',   'volumeNumber': '3'}},
            {'attributes': {'number': '7.0', 'volumeNumber': '3'}},  # equivalent
        ],
        'links': {},
    }
    empty_page = {'data': [], 'links': {}}

    async def _fake_get(url, **kw):
        rsp = MagicMock()
        if 'manga' in url:
            rsp.json = lambda: {'data': [{'id': '1', 'attributes': {'titles': {'en': 'Test'}}}]}
        elif 'chapters' in url:
            offset = kw.get('params', {}).get('page[offset]', 0)
            rsp.json = lambda: page_data if offset == 0 else empty_page
        else:
            rsp.json = lambda: {'data': []}
        return rsp

    with patch('metadata.httpx.AsyncClient') as MockClient:
        mock_inst = MockClient.return_value.__aenter__.return_value
        mock_inst.get = _fake_get
        result = await fetch_kitsu_chapter_map('Test Series', None, None)

    # '7' and '7.0' must collapse to the same '7' key (they're the same chapter)
    assert '7' in result
    assert '7.0' not in result, (
        f"7.0 should normalize to '7'; got {result}"
    )
    assert result['7'] == 3
