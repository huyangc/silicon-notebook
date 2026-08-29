import io

import pytest

from app.services.remote_sources import (
    probe_pdf,
    download_pdf,
    PdfProbe,
    FetchResult,
    _total_length,
)


def _fetch(result):
    return lambda url, timeout: result


def test_pdf_by_content_type_passes():
    p = probe_pdf("https://a/x.pdf",
                  fetch=_fetch(FetchResult(200, "application/pdf", 1000, b"%PDF-1.7")))
    assert p.ok and p.display_name.endswith(".pdf") and p.content_length == 1000


def test_pdf_by_magic_bytes_passes_even_if_octet_stream():
    p = probe_pdf("https://a/download?id=9",
                  fetch=_fetch(FetchResult(206, "application/octet-stream", 5000, b"%PDF-1.5 ...")))
    assert p.ok


def test_html_rejected():
    p = probe_pdf("https://a/page.html",
                  fetch=_fetch(FetchResult(200, "text/html", 100, b"<!DOCTYPE html>")))
    assert not p.ok and "不是 PDF" in p.reason


def test_http_error_rejected():
    p = probe_pdf("https://a/missing.pdf",
                  fetch=_fetch(FetchResult(404, "", 0, b"")))
    assert not p.ok and "404" in p.reason


def test_oversize_rejected():
    big = 300 * 1024 * 1024
    p = probe_pdf("https://a/huge.pdf",
                  fetch=_fetch(FetchResult(200, "application/pdf", big, b"%PDF-")))
    assert not p.ok and "200MB" in p.reason


def test_fetch_exception_rejected():
    def boom(url, timeout):
        raise ConnectionError("dns fail")
    p = probe_pdf("https://a/x.pdf", fetch=boom)
    assert not p.ok and "无法访问" in p.reason


def test_non_http_scheme_rejected():
    p = probe_pdf("ftp://a/x.pdf")
    assert not p.ok and "http" in p.reason


def test_total_length_prefers_content_range_total():
    assert _total_length({"Content-Range": "bytes 0-1023/12345"}) == 12345


def test_total_length_falls_back_to_content_length():
    assert _total_length({"Content-Length": "777"}) == 777


def test_total_length_unknown_or_wildcard_returns_zero():
    assert _total_length({}) == 0
    assert _total_length({"Content-Range": "bytes 0-1023/*"}) == 0


def test_download_pdf_writes_full_bytes(tmp_path):
    dest = tmp_path / "doc.pdf"
    data = b"%PDF-1.7 " + b"x" * 1000
    total = download_pdf("https://a/doc.pdf", dest, opener=lambda u, t: io.BytesIO(data))
    assert total == len(data)
    assert dest.read_bytes() == data


def test_download_pdf_rejects_oversize_and_cleans_up(tmp_path):
    dest = tmp_path / "huge.pdf"
    data = b"%PDF-" + b"y" * 5000
    with pytest.raises(ValueError, match="200MB"):
        download_pdf("https://a/huge.pdf", dest, max_bytes=1024,
                     opener=lambda u, t: io.BytesIO(data))
    assert not dest.exists()


def test_download_pdf_cleans_up_on_network_error(tmp_path):
    dest = tmp_path / "boom.pdf"

    def boom(url, timeout):
        raise ConnectionError("dns fail")

    with pytest.raises(ConnectionError):
        download_pdf("https://a/x.pdf", dest, opener=boom)
    assert not dest.exists()


def test_probe_pdf_default_fetch_carries_allow_private(monkeypatch):
    """不注入 fetch 时，allow_private 必须穿进默认 fetch（受信代理豁免的探测半程）。"""
    from app.services import remote_sources

    seen = []

    def fake_default_fetch(url, timeout, *, allow_private=False):
        seen.append(allow_private)
        return FetchResult(200, "application/pdf", 10, b"%PDF-")

    monkeypatch.setattr(remote_sources, "_default_fetch", fake_default_fetch)
    assert probe_pdf("http://127.0.0.1:8100/a.pdf", allow_private=True).ok
    assert probe_pdf("http://127.0.0.1:8100/a.pdf").ok
    assert seen == [True, False]


def test_download_pdf_default_opener_carries_allow_private(tmp_path, monkeypatch):
    """不注入 opener 时，allow_private 必须穿进默认 opener（受信代理豁免的下载半程）。"""
    from app.services import remote_sources

    seen = []

    def fake_default_opener(url, timeout, *, allow_private=False):
        seen.append(allow_private)
        return io.BytesIO(b"%PDF-")

    monkeypatch.setattr(remote_sources, "_default_opener", fake_default_opener)
    download_pdf("http://127.0.0.1:8100/a.pdf", tmp_path / "a.pdf", allow_private=True)
    download_pdf("http://127.0.0.1:8100/b.pdf", tmp_path / "b.pdf")
    assert seen == [True, False]
