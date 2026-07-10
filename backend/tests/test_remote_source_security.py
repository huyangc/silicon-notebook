from __future__ import annotations

import socket

import pytest

from app.services.remote_sources import UnsafeRemoteSourceURL, validate_public_http_url


def _resolver(ip: str):
    def resolve(host, port, *, type=0):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]
    return resolve


@pytest.mark.parametrize("url,ip", [
    ("http://localhost/file.pdf", "127.0.0.1"),
    ("http://metadata.internal/file.pdf", "169.254.169.254"),
    ("http://service.internal/file.pdf", "10.0.0.8"),
    ("http://service.internal/file.pdf", "::1"),
])
def test_private_and_local_targets_are_rejected(url, ip):
    with pytest.raises(UnsafeRemoteSourceURL):
        validate_public_http_url(url, resolver=_resolver(ip))


def test_public_target_is_allowed():
    parsed = validate_public_http_url(
        "https://papers.example/document.pdf",
        resolver=_resolver("93.184.216.34"),
    )
    assert parsed.hostname == "papers.example"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://user:password@papers.example/a.pdf",
    "https:///missing-host.pdf",
])
def test_unsafe_url_shapes_are_rejected(url):
    with pytest.raises(UnsafeRemoteSourceURL):
        validate_public_http_url(url, resolver=_resolver("93.184.216.34"))
