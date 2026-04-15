"""Security helpers.

Three unrelated primitives live here:

1. SSRF protection — validate_outbound_url(), for any user-supplied URL
   that the server is about to fetch.
2. ReDoS protection — compile_user_regex() / safe_regex_search(), for
   user-supplied regex patterns in custom formats and release profiles.
3. Secret encryption (H4 PR #1) — encrypt_secret() / decrypt_secret() /
   is_encrypted_secret() / load_or_create_secret_cipher(). Authenticated
   encryption (Fernet) for credential values stored in the SQLite DB.
   This PR ships only the primitives + master-key resolution; no DB
   migration or read/write paths are wired yet — that's H4 PR #2-#4.

The first two use only stdlib. The third needs `cryptography`
(added to the Dockerfile pip install in this PR).
"""
import ipaddress
import logging
import os
import re
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """Raised when an outbound URL fails SSRF validation.

    Message is intentionally short and UI-safe; server-side log entries
    (emitted by validate_outbound_url) carry the full reason.
    """


def _classify_blocked(ip, *, allow_private: bool) -> str | None:
    """Return a reason string if the IP is blocked, else None.

    Loopback, link-local (incl. 169.254.169.254), multicast, reserved
    and unspecified are ALWAYS blocked. Private/RFC1918 is blocked unless
    allow_private=True. IPv4-mapped IPv6 addresses are unwrapped before
    classification so that ::ffff:10.0.0.1 is recognised as private.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _classify_blocked(ip.ipv4_mapped, allow_private=allow_private)
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    if ip.is_unspecified:
        return "unspecified"
    if not allow_private and ip.is_private:
        return "private/RFC1918"
    return None


def validate_outbound_url(url: str, *, allow_private: bool = False) -> str:
    """Validate that an outbound URL is safe to fetch.

    Returns the URL on success; raises UnsafeURLError otherwise.

    Rejects:
      - empty / non-string / unparseable URLs
      - schemes other than http/https (file, gopher, ftp, data, unix:, …)
      - URLs containing userinfo (user:pass@host)
      - missing host
      - host == "localhost" or "*.localhost"
      - hostnames that resolve to any blocked address (loopback,
        link-local, multicast, reserved, unspecified; plus private
        unless allow_private=True). If a hostname resolves to multiple
        addresses, ALL must pass — a mixed pool is rejected.

    With allow_private=True some LAN-targeted callers (Komga, Prowlarr,
    Torznab/Newznab) can reach RFC1918 addresses. Loopback is still
    blocked because the app runs in a container where 127.0.0.1 means
    the container itself, not the host.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("empty URL")

    try:
        parts = urlsplit(url.strip())
    except Exception:
        raise UnsafeURLError("malformed URL")

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {scheme or '(none)'}")

    # urlsplit parses "http://user:pass@host" → username/password populated.
    if parts.username or parts.password:
        raise UnsafeURLError("userinfo in URL is not allowed")

    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    host_lower = host.lower().rstrip(".")
    if host_lower == "localhost" or host_lower.endswith(".localhost"):
        raise UnsafeURLError("localhost is not a permitted destination")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        logger.info("SSRF: DNS lookup failed for %r: %s", host, e)
        raise UnsafeURLError("could not resolve hostname")

    seen: set[str] = set()
    for info in infos:
        # info[4] is (host, port) for AF_INET or (host, port, flow, scope)
        # for AF_INET6; the host element is always str.
        ip_str: str = str(info[4][0])
        # Strip IPv6 zone id if present (e.g. "fe80::1%eth0")
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            logger.warning("SSRF: unparseable resolved address %r for host %r", ip_str, host)
            raise UnsafeURLError("URL not allowed")
        reason = _classify_blocked(ip, allow_private=allow_private)
        if reason:
            logger.warning(
                "SSRF blocked: host=%r resolved=%s reason=%s allow_private=%s",
                host, ip_str, reason, allow_private,
            )
            raise UnsafeURLError("URL not allowed")

    return url


# ─────────────────────────── ReDoS protection ──────────────────────────────
# User-supplied regex patterns (custom formats, release profiles) can cause
# catastrophic backtracking. Classic demo: `(a+)+$` against a 30-char input
# runs for minutes. This section validates patterns statically and caps
# input length as defense in depth — zero runtime dependencies.

_MAX_REGEX_PATTERN_LEN = 256
_MAX_REGEX_INPUT_LEN = 2048

# Detects the classic "nested unbounded quantifier" shape:
#     (a+)+   (a*)*   (a+)*   (a*)+   ([abc]+)+   (.+)*
# i.e. a group whose contents contain `+` or `*` AND the group itself is
# quantified by `+` or `*`. This is the structure required for a regex
# engine to explore exponentially many backtrack paths. Alternation-based
# catastrophes (e.g. `(a|a)+`) aren't caught by this check; the input-length
# cap below is the fallback for those.
_NESTED_UNBOUNDED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*]")


class UnsafeRegexError(ValueError):
    """Raised when a user-supplied regex is rejected by compile_user_regex.

    Reasons: empty, too long, syntactically invalid, or contains a nested
    unbounded quantifier (classic ReDoS shape). Callers are expected to
    fall back to substring matching (or skip) rather than propagate.
    """


def compile_user_regex(pattern: str, flags: int = 0) -> "re.Pattern[str]":
    """Compile a user-supplied regex, rejecting empty/malformed/dangerous
    patterns. Raises UnsafeRegexError on any rejection.

    Safety measures, in order:
      1. Length cap (_MAX_REGEX_PATTERN_LEN) — no 10KB regex war crimes.
      2. Static detection of nested unbounded quantifiers (see above).
      3. Standard re.compile() validation — catches syntax errors.

    Does NOT implement a match-time timeout; stdlib re lacks the hook.
    Use safe_regex_search() for an input-length-capped evaluator.
    """
    if not isinstance(pattern, str):
        raise UnsafeRegexError("pattern must be a string")
    p = pattern.strip()
    if not p:
        raise UnsafeRegexError("empty pattern")
    if len(p) > _MAX_REGEX_PATTERN_LEN:
        raise UnsafeRegexError(
            f"pattern too long ({len(p)} chars > {_MAX_REGEX_PATTERN_LEN})"
        )
    if _NESTED_UNBOUNDED_QUANTIFIER.search(p):
        raise UnsafeRegexError(
            "pattern contains a nested unbounded quantifier (ReDoS risk); "
            "replace with a bounded construct (e.g. `[abc]+` instead of `(a+)+`)"
        )
    try:
        return re.compile(p, flags)
    except re.error as e:
        raise UnsafeRegexError(f"invalid regex: {e}")


def safe_regex_search(pattern: str, text: str, flags: int = 0) -> "bool | None":
    """Run a user-supplied regex against text, safely.

    Returns:
      True  — pattern matched
      False — pattern compiled cleanly but did not match
      None  — pattern was rejected (unsafe / invalid / empty). Callers
              should fall back to substring matching or skip the spec.

    Text is truncated to _MAX_REGEX_INPUT_LEN before matching as a final
    safety net against any dangerous pattern that slipped past validation.
    Typical release titles are under 200 chars; the cap is never hit in
    practice.
    """
    try:
        compiled = compile_user_regex(pattern, flags)
    except UnsafeRegexError as e:
        logger.info("safe_regex_search: rejected pattern %r: %s", pattern, e)
        return None
    if text and len(text) > _MAX_REGEX_INPUT_LEN:
        text = text[:_MAX_REGEX_INPUT_LEN]
    try:
        return bool(compiled.search(text or ""))
    except Exception as e:
        logger.warning("safe_regex_search: unexpected error on %r: %s", pattern, e)
        return None


# ───────────────────── Secret-at-rest encryption (H4 PR #1) ─────────────────────
# Authenticated encryption for credentials stored in the SQLite DB. This PR
# ships only the primitives + master-key resolution; no DB migration or
# read/write paths are wired yet (those are H4 PRs #2-#4). The lifespan
# call to load_or_create_secret_cipher() makes the cipher available for
# subsequent PRs to consume.
#
# Wire format: enc:v1:<urlsafe-base64-fernet-token>
#   - The "enc:v1:" prefix is the version-and-detection marker.
#   - The Fernet token already includes its own IV + HMAC-SHA256 + timestamp.
#   - is_encrypted_secret() is an O(1) prefix check; no try/except needed
#     for routine plaintext-vs-ciphertext discrimination.
#
# Master key resolution order (load_or_create_secret_cipher):
#   1. MANGARR_SECRET_KEY environment variable
#   2. <config_dir>/.mangarr-secret-key file (mode 0600)
#   3. Auto-generate a fresh Fernet key, write to (2), log a one-time
#      WARNING with the file path and backup guidance.
#
# The key value itself is NEVER logged. Tests guard this.

_ENC_PREFIX = "enc:v1:"
_KEY_FILENAME = ".mangarr-secret-key"
_KEY_ENV_VAR = "MANGARR_SECRET_KEY"

# Module-level cache populated by load_or_create_secret_cipher(). Stays
# None until startup explicitly initialises it. Subsequent PRs will look
# at this to decide whether to encrypt-on-write / decrypt-on-read.
_SECRET_CIPHER = None   # type: ignore[var-annotated]


class SecretCipherUnavailable(RuntimeError):
    """Raised by code paths that require the cipher when it has not been
    loaded (e.g. encrypt_secret called before lifespan startup). Also
    raised at startup if the configured key fails to parse — caller
    decides whether to abort boot."""


class SecretDecryptionError(ValueError):
    """Raised by decrypt_secret when an enc:v1:-prefixed value cannot be
    decrypted with the active cipher (wrong key, corrupted token).

    The exception message NEVER contains the ciphertext or the key —
    only a generic identifier ("token") so per-field WARNING logs can
    name the field without leaking its value."""


def is_encrypted_secret(value):
    """O(1) prefix check. True iff value is a string starting with the
    enc:v1: wire-format prefix. None / empty / non-string return False."""
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def encrypt_secret(plaintext):
    """Wrap a plaintext credential into the enc:v1:<token> format.

    Empty string and None pass through unchanged — encrypting "" would
    surprise callers that check truthiness for "no credential set".

    Raises SecretCipherUnavailable if the cipher hasn't been initialised
    yet (lifespan startup must call load_or_create_secret_cipher first).
    """
    if plaintext is None or plaintext == "":
        return plaintext
    if _SECRET_CIPHER is None:
        raise SecretCipherUnavailable(
            "secret cipher not initialised; call load_or_create_secret_cipher first"
        )
    if not isinstance(plaintext, str):
        raise TypeError(f"encrypt_secret expects str, got {type(plaintext).__name__}")
    token = _SECRET_CIPHER.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt_secret(value):
    """Decrypt a enc:v1:-prefixed value. Plaintext (no prefix) passes
    through unchanged for back-compat during the H4 PR sequence and for
    env-supplied secrets that never get encrypted.

    Empty string and None pass through unchanged.

    Raises SecretDecryptionError on prefix-present-but-invalid (wrong
    key, corrupted, or truncated). The exception message contains NO
    ciphertext or key material — callers should log "<field> failed to
    decrypt" naming the field, not the value.
    """
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise TypeError(f"decrypt_secret expects str, got {type(value).__name__}")
    if not value.startswith(_ENC_PREFIX):
        return value   # already plaintext, return as-is
    if _SECRET_CIPHER is None:
        raise SecretCipherUnavailable(
            "secret cipher not initialised; cannot decrypt enc:v1: value"
        )
    token = value[len(_ENC_PREFIX):]
    try:
        from cryptography.fernet import InvalidToken   # local import: zero cost when unused
        try:
            return _SECRET_CIPHER.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise SecretDecryptionError(
                "encrypted token failed to decrypt (wrong key or corruption)"
            )
        except Exception as e:
            # Any other crypto-layer failure: same generic surface — never
            # interpolate the token or key bytes.
            raise SecretDecryptionError(
                f"encrypted token decode failed: {type(e).__name__}"
            )
    except ImportError:
        # cryptography not installed — caller's environment is misconfigured.
        # Surface as cipher-unavailable so the calling code can disable the
        # affected integration without crashing the app.
        raise SecretCipherUnavailable("cryptography package not available")


def load_or_create_secret_cipher(config_dir="/config"):
    """Resolve the master key (env → file → auto-generate), instantiate
    a Fernet cipher, and cache it at module level.

    Called once during lifespan startup. Idempotent — calling again
    returns the cached cipher without re-reading anything. Returns the
    Fernet instance.

    Raises SecretCipherUnavailable on:
      - cryptography package missing
      - MANGARR_SECRET_KEY set but format invalid
      - key file present but format invalid
      - auto-generation failed (filesystem error)

    The key value is NEVER logged. Source / file path / mode bits ARE
    logged (no secrets in those).
    """
    global _SECRET_CIPHER
    if _SECRET_CIPHER is not None:
        return _SECRET_CIPHER

    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise SecretCipherUnavailable(f"cryptography package not available: {e}")

    log = logging.getLogger(__name__)

    # 1. Env var
    env_key = os.environ.get(_KEY_ENV_VAR)
    if env_key:
        try:
            cipher = Fernet(env_key.encode("ascii") if isinstance(env_key, str) else env_key)
        except (ValueError, Exception) as e:
            # Fernet raises ValueError on bad base64 / wrong length. Don't
            # include the key bytes (env_key) in the message.
            raise SecretCipherUnavailable(
                f"{_KEY_ENV_VAR} format invalid (expected 44-char urlsafe-base64 Fernet key): {type(e).__name__}"
            )
        log.info("loaded MANGARR_SECRET_KEY from environment")
        _SECRET_CIPHER = cipher
        return cipher

    # 2. Key file
    key_path = os.path.join(config_dir, _KEY_FILENAME)
    if os.path.isfile(key_path):
        try:
            with open(key_path, "rb") as f:
                file_key = f.read().strip()
            cipher = Fernet(file_key)
        except (ValueError, OSError, Exception) as e:
            raise SecretCipherUnavailable(
                f"key file at {key_path} could not be loaded: {type(e).__name__}"
            )
        # Permission warning if the file is more permissive than 0600
        try:
            mode = os.stat(key_path).st_mode & 0o777
            if mode & 0o077:
                log.warning(
                    "Mangarr secret key file %s has permissive mode %o — recommend `chmod 600 %s`",
                    key_path, mode, key_path,
                )
        except OSError:
            pass
        log.info("loaded Mangarr secret key from %s", key_path)
        _SECRET_CIPHER = cipher
        return cipher

    # 3. Auto-generate
    try:
        os.makedirs(config_dir, exist_ok=True)
        new_key = Fernet.generate_key()
        # Write atomically: write to .tmp, fsync, rename. Mode 0600 set
        # via os.open + O_CREAT + O_EXCL where supported.
        tmp_path = key_path + ".tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new_key)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # Make sure we don't leave a stray .tmp behind on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.replace(tmp_path, key_path)
        # Belt-and-braces: set 0600 again after rename in case the umask
        # masked off the open() mode flags on some filesystems.
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        cipher = Fernet(new_key)
    except Exception as e:
        raise SecretCipherUnavailable(
            f"could not auto-generate secret key at {key_path}: {type(e).__name__}: {e}"
        )

    log.warning(
        "generated a new Mangarr secret key at %s — "
        "back this file up SEPARATELY from the database; "
        "losing it makes encrypted credentials unrecoverable",
        key_path,
    )
    _SECRET_CIPHER = cipher
    return cipher
