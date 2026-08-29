from __future__ import annotations

import socket
import urllib.request

import pytest

from app.services.remote_sources import (
    UnsafeRemoteSourceURL,
    _PublicOnlyRedirectHandler,
    validate_public_http_url,
)


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


def _resolver_must_not_run(host, port, *, type=0):
    raise AssertionError("allow_private path must not resolve DNS")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://user:password@proxy.internal/a.pdf",
    "ftp://proxy.internal/a.pdf",
    "https:///missing-host.pdf",
    "http://proxy.internal:99999/a.pdf",
])
def test_allow_private_still_rejects_unsafe_url_shapes(url):
    """豁免只跳过「公网地址」这一条：形态检查在 allow_private=True 下照常拒。"""
    with pytest.raises(UnsafeRemoteSourceURL):
        validate_public_http_url(
            url, resolver=_resolver_must_not_run, allow_private=True
        )


def test_allow_private_accepts_loopback_without_touching_dns():
    """豁免路径直接返回，不触发 DNS 解析（受信代理 host 可能根本无法解析）。"""
    parsed = validate_public_http_url(
        "http://127.0.0.1:8100/export/file.pdf",
        resolver=_resolver_must_not_run,
        allow_private=True,
    )
    assert parsed.hostname == "127.0.0.1"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.8", "::1"])
def test_default_path_without_allow_private_still_rejects_private(ip):
    """回归钉：不传 allow_private（所有既有调用形态）时私网一律拒。"""
    with pytest.raises(UnsafeRemoteSourceURL):
        validate_public_http_url(
            "http://proxy.internal/file.pdf", resolver=_resolver(ip)
        )


def test_redirect_handler_default_rejects_private_redirect_target():
    """默认（非豁免）实例对每次重定向重施公网检查——私网 newurl 当场拒。
    这个类此前零直接覆盖；带上 allow_private 旗标后必须钉死默认 fail-closed。"""
    handler = _PublicOnlyRedirectHandler()
    request = urllib.request.Request("https://papers.example/a.pdf")
    with pytest.raises(UnsafeRemoteSourceURL):
        handler.redirect_request(
            request, None, 302, "Found", {}, "http://10.0.0.8/internal.pdf"
        )


def test_redirect_handler_allow_private_instance_follows_private_redirect():
    """allow_private=True 的实例整链豁免：私网 newurl 照常构造重定向请求。"""
    handler = _PublicOnlyRedirectHandler(allow_private=True)
    request = urllib.request.Request("http://127.0.0.1:8100/a.pdf")
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "http://127.0.0.1:8100/b.pdf"
    )
    assert redirected is not None
    assert redirected.full_url == "http://127.0.0.1:8100/b.pdf"
