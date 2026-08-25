"""Fail-closed qBittorrent login-response semantics."""

import re
from enum import Enum, auto


_QBIT_VERSION_PATTERN = re.compile(
    r"v?\d+\.\d+\.\d+(?:\.\d+)?(?:-?(?:alpha|beta|rc)\d+)?(?:\+[0-9a-z]+(?:[.-][0-9a-z]+)*)?",
    re.ASCII | re.IGNORECASE,
)


class QbitLoginMode(Enum):
    """Meaning of one qBittorrent ``/auth/login`` response."""

    NORMAL_AUTH = auto()
    BYPASS_PROBE_REQUIRED = auto()
    REJECTED = auto()


def classify_qbit_login(status_code: int, body: str) -> QbitLoginMode:
    """Classify qBittorrent login without treating arbitrary 2xx as success."""
    stripped_body = body.strip()
    if status_code == 200 and stripped_body == "Ok.":
        return QbitLoginMode.NORMAL_AUTH
    if status_code == 204 and not stripped_body:
        # Authentication bypass is only provisional until a read-only API
        # request succeeds on the same client/session.
        return QbitLoginMode.BYPASS_PROBE_REQUIRED
    return QbitLoginMode.REJECTED


def is_valid_qbit_version_response(status_code: int, body: str) -> bool:
    """Return whether ``/app/version`` returned a qBittorrent version."""
    return status_code == 200 and bool(
        _QBIT_VERSION_PATTERN.fullmatch(body.strip())
    )
