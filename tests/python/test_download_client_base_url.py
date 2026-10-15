"""Direct, hermetic coverage for download-client host and port composition."""
from urllib.parse import urlsplit

import pytest

from routers.download_clients import client_base_url


@pytest.mark.parametrize(
    "host, expected",
    [
        ("http://[2001:db8::1]", "http://[2001:db8::1]:4567"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]:4567"),
        ("http://[2001:db8::1]/api/", "http://[2001:db8::1]:4567/api"),
        ("https://[2001:db8::1]/nested/api///", "https://[2001:db8::1]:4567/nested/api"),
    ],
)
def test_ipv6_separate_port_preserves_brackets_and_path(host: str, expected: str) -> None:
    result = client_base_url({"host": host, "port": 4567})

    assert result == expected
    parsed = urlsplit(result)
    assert parsed.hostname == "2001:db8::1"
    assert parsed.port == 4567


@pytest.mark.parametrize(
    "host, expected",
    [
        ("http://[2001:db8::1]:8080/", "http://[2001:db8::1]:8080"),
        ("https://[2001:db8::1]:8443/api/", "https://[2001:db8::1]:8443/api"),
        ("http://client.example:8080/api/", "http://client.example:8080/api"),
        ("https://192.0.2.1:8443/api/", "https://192.0.2.1:8443/api"),
    ],
)
def test_embedded_port_takes_precedence(host: str, expected: str) -> None:
    assert client_base_url({"host": host, "port": 4567}) == expected


@pytest.mark.parametrize(
    "host, expected",
    [
        ("http://[2001:db8::1]/", "http://[2001:db8::1]"),
        ("https://[2001:db8::1]/api/", "https://[2001:db8::1]/api"),
        ("http://client.example/", "http://client.example"),
        ("https://192.0.2.1/nested/api///", "https://192.0.2.1/nested/api"),
    ],
)
def test_no_separate_port_keeps_existing_authority(host: str, expected: str) -> None:
    assert client_base_url({"host": host, "port": None}) == expected


@pytest.mark.parametrize(
    "host, expected",
    [
        ("http://client.example/", "http://client.example:4567"),
        ("https://client.example/api/", "https://client.example:4567/api"),
        ("http://192.0.2.1", "http://192.0.2.1:4567"),
        ("https://192.0.2.1/nested/api///", "https://192.0.2.1:4567/nested/api"),
    ],
)
def test_dns_and_ipv4_separate_port_compatibility(host: str, expected: str) -> None:
    assert client_base_url({"host": host, "port": 4567}) == expected
