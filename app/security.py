"""Security helpers.

Two unrelated primitives live here:

1. SSRF protection — validate_outbound_url(), for any user-supplied URL
   that the server is about to fetch.
2. ReDoS protection — compile_user_regex() / safe_regex_search(), for
   user-supplied regex patterns in custom formats and release profiles.

Both use lightweight static validation with zero runtime dependencies.
"""
import ipaddress
import logging
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
