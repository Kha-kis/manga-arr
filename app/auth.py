"""Single-administrator browser authentication primitives.

Passwords use Argon2id. Browser sessions are opaque random tokens whose
SHA-256 digests are stored in SQLite, which makes logout and password-change
revocation immediate without putting identity data in the cookie.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from shared import get_db


AUTH_COOKIE_NAME = "mangarr_session"
SESSION_ABSOLUTE_SECONDS = 7 * 24 * 60 * 60
SESSION_IDLE_SECONDS = 24 * 60 * 60
SESSION_TOUCH_SECONDS = 5 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH: str | None = None
_CONFIG_DIR = os.environ.get("MANGARR_CONFIG_DIR", "/config")
_LEGACY_SETUP_TOKEN_FILENAME = ".mangarr-setup-token"
_TEST_AUTH_BYPASS = False


def set_test_auth_bypass(enabled: bool) -> None:
    """Internal test-harness switch; no environment or HTTP control exists."""
    global _TEST_AUTH_BYPASS
    _TEST_AUTH_BYPASS = bool(enabled)


def test_auth_bypass_enabled() -> bool:
    return _TEST_AUTH_BYPASS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_username(username: str) -> str | None:
    value = str(username or "").strip()
    if not _USERNAME_RE.fullmatch(value):
        return None
    return value


def validate_password(password: str) -> str | None:
    value = str(password or "")
    if len(value) < 12:
        return "Password must contain at least 12 characters."
    if len(value) > 128:
        return "Password must contain no more than 128 characters."
    return None


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def _verify_hash(password_hash: str, password: str) -> bool:
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def _dummy_password_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password("mangarr-dummy-password-value")
    return _DUMMY_PASSWORD_HASH


def get_admin() -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT id, username, password_hash, created_at, updated_at "
            "FROM auth_admin WHERE id=1"
        ).fetchone()
        return dict(row) if row else None


def is_admin_configured() -> bool:
    return get_admin() is not None


def create_admin(username: str, password_hash: str) -> dict:
    normalized = validate_username(username)
    if normalized is None:
        raise ValueError("invalid username")
    now = _timestamp(_now())
    with get_db() as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO auth_admin"
            "(id,username,password_hash,created_at,updated_at) "
            "VALUES(1,?,?,?,?)",
            (normalized, password_hash, now, now),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("administrator already configured")
    return {
        "id": 1,
        "username": normalized,
        "password_hash": password_hash,
        "created_at": now,
        "updated_at": now,
    }


def verify_admin_credentials(username: str, password: str) -> dict | None:
    admin = get_admin()
    candidate_hash = admin["password_hash"] if admin else _dummy_password_hash()
    password_matches = _verify_hash(candidate_hash, password)
    username_matches = bool(
        admin
        and hmac.compare_digest(
            str(admin["username"]).casefold().encode("utf-8"),
            str(username or "").strip().casefold().encode("utf-8"),
        )
    )
    if not (admin and password_matches and username_matches):
        return None
    if _PASSWORD_HASHER.check_needs_rehash(admin["password_hash"]):
        replacement = hash_password(password)
        with get_db() as db:
            db.execute(
                "UPDATE auth_admin SET password_hash=?, updated_at=? WHERE id=1",
                (replacement, _timestamp(_now())),
            )
        admin["password_hash"] = replacement
    return admin


def update_admin_password(password_hash: str) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE auth_admin SET password_hash=?, updated_at=? WHERE id=1",
            (password_hash, _timestamp(_now())),
        )
        db.execute("DELETE FROM auth_sessions")


def reset_admin_for_recovery() -> None:
    """Remove browser credentials so the next browser can create an administrator."""
    with get_db() as db:
        db.execute("DELETE FROM auth_sessions")
        db.execute("DELETE FROM auth_admin WHERE id=1")
    remove_legacy_setup_token()
    logging.getLogger(__name__).warning(
        "local administrator reset; complete browser setup at /setup before exposure",
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(admin_id: int = 1) -> str:
    token = secrets.token_urlsafe(48)
    now = _now()
    with get_db() as db:
        db.execute(
            "INSERT INTO auth_sessions(token_hash,admin_id,created_at,last_seen_at,expires_at) "
            "VALUES(?,?,?,?,?)",
            (
                _token_hash(token),
                admin_id,
                _timestamp(now),
                _timestamp(now),
                _timestamp(now + timedelta(seconds=SESSION_ABSOLUTE_SECONDS)),
            ),
        )
    return token


def validate_session(token: str) -> dict | None:
    if not token or len(token) > 256:
        return None
    digest = _token_hash(token)
    now = _now()
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT s.token_hash,s.created_at,s.last_seen_at,s.expires_at,"
                " a.id AS admin_id,a.username "
                "FROM auth_sessions s JOIN auth_admin a ON a.id=s.admin_id "
                "WHERE s.token_hash=?",
                (digest,),
            ).fetchone()
            if not row:
                return None
            session = dict(row)
            last_seen = _parse_timestamp(session["last_seen_at"])
            expires = _parse_timestamp(session["expires_at"])
            if (
                now >= expires
                or (now - last_seen).total_seconds() >= SESSION_IDLE_SECONDS
            ):
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest,))
                return None
            if (now - last_seen).total_seconds() >= SESSION_TOUCH_SECONDS:
                db.execute(
                    "UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?",
                    (_timestamp(now), digest),
                )
                session["last_seen_at"] = _timestamp(now)
            return session
    except Exception:
        logging.getLogger(__name__).exception("browser session validation failed")
        return None


def delete_session(token: str) -> None:
    if not token:
        return
    with get_db() as db:
        db.execute(
            "DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),)
        )


def delete_other_sessions(token: str) -> int:
    digest = _token_hash(token)
    with get_db() as db:
        cursor = db.execute(
            "DELETE FROM auth_sessions WHERE token_hash != ?", (digest,)
        )
        return max(0, cursor.rowcount)


def count_sessions() -> int:
    with get_db() as db:
        return int(db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0])


def purge_expired_sessions() -> int:
    now = _timestamp(_now())
    with get_db() as db:
        cursor = db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
        return max(0, cursor.rowcount)


def remove_legacy_setup_token() -> None:
    """Remove the bootstrap file created by Mangarr versions before 1.1."""
    path = os.path.join(_CONFIG_DIR, _LEGACY_SETUP_TOKEN_FILENAME)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logging.getLogger(__name__).exception("could not remove legacy setup token")


class LoginThrottle:
    """Small in-memory sliding window keyed by the direct peer address."""

    def __init__(self):
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, client_id: str, now: float) -> deque[float]:
        entries = self._failures[client_id]
        cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
        while entries and entries[0] <= cutoff:
            entries.popleft()
        return entries

    def retry_after(self, client_id: str) -> int:
        now = time.monotonic()
        with self._lock:
            entries = self._prune(client_id, now)
            if len(entries) < LOGIN_FAILURE_LIMIT:
                return 0
            return max(1, math.ceil(LOGIN_FAILURE_WINDOW_SECONDS - (now - entries[0])))

    def record_failure(self, client_id: str) -> int:
        now = time.monotonic()
        with self._lock:
            entries = self._prune(client_id, now)
            entries.append(now)
            if len(entries) < LOGIN_FAILURE_LIMIT:
                return 0
            return max(1, math.ceil(LOGIN_FAILURE_WINDOW_SECONDS - (now - entries[0])))

    def record_success(self, client_id: str) -> None:
        with self._lock:
            self._failures.pop(client_id, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


LOGIN_THROTTLE = LoginThrottle()
