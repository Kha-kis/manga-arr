"""SSRF protection helpers.

Single chokepoint for validating user-supplied outbound URLs before any
httpx request or webhook send. Callers should wrap calls in a try/except
on UnsafeURLError and surface a generic message to the user — the helper
logs detailed reasons (resolved IP, classification) server-side.
"""
import ipaddress
import logging
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
