"""Event-table writes, history writes, and SSE fan-out.

Nineteenth module extracted from main.py. Pulls out the three
cross-cutting primitives that essentially every other extracted
module needs, which used to live in main.py and were re-imported
from there via lazy `from main import log_event` (and friends) to
break the cycle:

  log_event              — append a row to the events table, with
                           an optional `db` kwarg so callers already
                           inside a write transaction can reuse the
                           connection instead of opening a second
                           one that would serialize behind the
                           outer writer (issue #31).
  add_history            — append a row to the history table. Takes
                           the caller's open connection; no db=None
                           fallback because every call site is
                           already in a transaction.
  broadcast_queue_event  — fan a JSON payload out to every SSE
                           subscriber on the queue-events stream.
  _sse_subscribers       — list of asyncio.Queue objects, one per
                           active SSE connection. Exposed so the
                           queue_events route handler in main can
                           append / remove on connect / disconnect.

Everything here is framework-free: no FastAPI imports, no router
decorators. The SSE route itself stays in main.py because @app.get
needs `app`, but the route body just appends to the list exposed
here.

Pulling these four symbols out of main eliminates the bulk of the
`from main import log_event` lazy imports sprinkled across the
extracted modules — those now import from `events` at module load
time, which is cleaner and catches typos at import rather than at
first call.
"""
from __future__ import annotations

import asyncio
import json

from shared import get_db


def log_event(event_type: str, message: str, series_id: int | None = None,
              *, db=None):
    """Insert a row into the events table.

    If `db` is provided, the INSERT is executed on that existing connection
    — use this when calling from inside an already-open write transaction
    (e.g. `_execute_import`, `_queue_import`) to avoid opening a second
    connection that would serialize behind the outer writer and burn the
    15-second SQLITE_BUSY timeout.

    If `db` is None, opens a fresh connection as before. Normal callers
    (loops, HTTP handlers, one-shot background tasks) should not pass db.

    Swallows exceptions either way — event logging is best-effort and must
    not break the caller.
    """
    try:
        if db is not None:
            db.execute(
                "INSERT INTO events(event_type, series_id, message) VALUES(?,?,?)",
                (event_type, series_id, message),
            )
        else:
            with get_db() as _db:
                _db.execute(
                    "INSERT INTO events(event_type, series_id, message) VALUES(?,?,?)",
                    (event_type, series_id, message),
                )
    except Exception:
        pass


def add_history(db, event_type: str, series_id: int | None, series_title: str,
                volume_label: str, source_title: str = '',
                indexer: str = '', protocol: str = '', client: str = '',
                download_id: str = '', size_bytes: int = 0,
                release_group: str = '', data: dict | None = None,
                torrent_url: str = ''):
    """Insert a history record."""
    db.execute(
        "INSERT INTO history(event_type, series_id, series_title, volume_label,"
        " source_title, indexer, protocol, client, download_id, size_bytes, release_group, data, torrent_url)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_type, series_id, series_title, volume_label, source_title,
         indexer, protocol, client, download_id, size_bytes or 0, release_group,
         json.dumps(data) if data else None, torrent_url or None)
    )


# Shared with main.queue_events — the route handler appends a new asyncio.Queue
# here on each SSE client connect and removes it on disconnect.
_sse_subscribers: list[asyncio.Queue] = []


async def broadcast_queue_event(event: str, data: dict | None = None):
    """Push a queue update event to all connected SSE clients."""
    payload = json.dumps({'event': event, **(data or {})})
    dead = []
    for q in _sse_subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass
