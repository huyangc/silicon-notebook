"""公开 PDF 直链的轻量初筛：判定 URL 是否指向可解析的 PDF。

网络 I/O 经一个可注入的 `fetch` 回调完成，因此单测无需真打网络。判定规则：
Content-Type 以 application/pdf 开头，或响应首字节为 %PDF-，即视为 PDF。
"""
from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional
from urllib.parse import urlparse, ParseResult

MAX_PDF_BYTES = 200 * 1024 * 1024  # 与 mineru.net 单文件上限一致


class FetchResult(NamedTuple):
    status: int
    content_type: str
    content_length: int
    head: bytes


@dataclass
class PdfProbe:
    ok: bool
    reason: str
    content_length: int
    display_name: str


def probe_pdf(
    url: str,
    *,
    timeout: float = 10.0,
    fetch: Optional[Callable[[str, float], FetchResult]] = None,
) -> PdfProbe:
    fetch = fetch or _default_fetch
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return PdfProbe(False, "URL 必须以 http/https 开头", 0, "")
    display = _display_name(parsed)
    try:
        result = fetch(url, timeout)
    except Exception as exc:  # noqa: BLE001 — 收敛为一句可读的拒绝原因
        return PdfProbe(False, f"无法访问 URL：{exc}", 0, display)
    if result.status >= 400:
        return PdfProbe(False, f"无法访问 URL（HTTP {result.status}）", 0, display)
    if result.content_length and result.content_length > MAX_PDF_BYTES:
        return PdfProbe(False, "PDF 超过 200MB 上限", result.content_length, display)
    is_pdf = (
        result.content_type.lower().startswith("application/pdf")
        or result.head.startswith(b"%PDF-")
    )
    if not is_pdf:
        ct = result.content_type or "未知"
        return PdfProbe(False, f"URL 不是 PDF（Content-Type={ct}）", result.content_length, display)
    return PdfProbe(True, "", result.content_length, display)


_DOWNLOAD_CHUNK = 256 * 1024


def download_pdf(
    url: str,
    dest: "os.PathLike[str] | str",
    *,
    timeout: float = 120.0,
    max_bytes: int = MAX_PDF_BYTES,
    opener: Optional[Callable[[str, float], object]] = None,
) -> int:
    """流式下载 URL 到 dest（用于本地优先解析：先落盘再交给本地 MinerU）。

    超过 max_bytes 立即抛错并保证不残留半截文件；网络层经可注入的
    `opener(url, timeout) -> 支持 .read(size) 的上下文管理器` 完成，单测无需打网络。
    """
    opener = opener or _default_opener
    total = 0
    try:
        with opener(url, timeout) as stream, open(dest, "wb") as handle:  # type: ignore[union-attr]
            while True:
                chunk = stream.read(_DOWNLOAD_CHUNK)  # type: ignore[attr-defined]
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("PDF 超过 200MB 上限")
                handle.write(chunk)
    except Exception:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return total


def _default_opener(url: str, timeout: float):
    request = urllib.request.Request(url, method="GET")
    return urllib.request.urlopen(request, timeout=timeout)


def _display_name(parsed: ParseResult) -> str:
    base = os.path.basename(parsed.path) or parsed.netloc or "source"
    if not base.lower().endswith(".pdf"):
        base = f"{base}.pdf"
    return base


def _default_fetch(url: str, timeout: float) -> FetchResult:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Range", "bytes=0-1023")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200) or 200
        content_type = response.headers.get("Content-Type", "") or ""
        content_length = _total_length(response.headers)
        head = response.read(1024)
    return FetchResult(status, content_type, content_length, head)


def _total_length(headers) -> int:
    content_range = headers.get("Content-Range", "")
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    content_length = headers.get("Content-Length", "")
    return int(content_length) if str(content_length).isdigit() else 0
