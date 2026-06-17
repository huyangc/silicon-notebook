from app.services.remote_sources import probe_pdf, PdfProbe, FetchResult


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
