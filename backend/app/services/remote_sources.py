"""公开 PDF 直链的轻量初筛：判定 URL 是否指向可解析的 PDF。

网络 I/O 经一个可注入的 `fetch` 回调完成，因此单测无需真打网络。判定规则：
Content-Type 以 application/pdf 开头，或响应首字节为 %PDF-，即视为 PDF。
"""
from __future__ import annotations

import os
import ipaddress
import socket
import urllib.request
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional
from urllib.parse import urlparse, ParseResult

MAX_PDF_BYTES = 200 * 1024 * 1024  # 与 mineru.net 单文件上限一致


class UnsafeRemoteSourceURL(ValueError):
    """The URL is not safe for a server-side outbound request."""


def validate_public_http_url(
    url: str,
    *,
    resolver: Callable = socket.getaddrinfo,
    allow_private: bool = False,
) -> ParseResult:
    """Validate an outbound URL and require every resolved address to be public.

    The check is intentionally applied immediately before the initial request
    and again for every redirect.  Rejecting a hostname when *any* answer is
    private prevents round-robin DNS from mixing public and internal targets.

    ``allow_private=True`` skips ONLY the public-address rule (returning before
    the resolver runs — a trusted proxy host may not resolve at all); the URL
    shape checks (scheme / host / credentials / port validity) still apply. It
    disables the SSRF guard for the whole request chain, redirects the remote
    server answers included, so it may only ever be passed down the
    deployment-configured trusted-proxy origin whitelist path — never from
    anything reachable by browser or MCP request input.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeRemoteSourceURL("URL 必须以 http/https 开头")
    if not parsed.hostname:
        raise UnsafeRemoteSourceURL("URL 缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeRemoteSourceURL("URL 不允许包含用户名或密码")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeRemoteSourceURL("URL 端口无效") from exc
    if allow_private:
        return parsed
    try:
        answers = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeRemoteSourceURL(f"无法解析 URL 主机：{exc}") from exc
    if not answers:
        raise UnsafeRemoteSourceURL("URL 主机没有可用地址")
    for answer in answers:
        raw_ip = answer[4][0]
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise UnsafeRemoteSourceURL("URL 主机解析结果无效") from exc
        if not address.is_global:
            raise UnsafeRemoteSourceURL("URL 指向本机、内网或保留地址")
    return parsed


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-apply the public-address policy to every redirect target."""

    def __init__(self, *, allow_private: bool = False) -> None:
        super().__init__()
        self._allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_http_url(newurl, allow_private=self._allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_urlopen(
    request: urllib.request.Request, timeout: float, *, allow_private: bool = False
):
    validate_public_http_url(request.full_url, allow_private=allow_private)
    opener = urllib.request.build_opener(
        _PublicOnlyRedirectHandler(allow_private=allow_private)
    )
    return opener.open(request, timeout=timeout)


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
    allow_private: bool = False,
) -> PdfProbe:
    """初筛 URL 是否指向可解析的 PDF（规则见模块 docstring）。

    ``allow_private`` 只作用于默认网络层（透传 ``_safe_urlopen`` 的 SSRF 检查，
    语义见 ``validate_public_http_url``）；注入了 ``fetch`` 时它不参与——网络
    策略由注入方自负，与既有单测语义一致。
    """
    fetch = fetch or (lambda u, t: _default_fetch(u, t, allow_private=allow_private))
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
    allow_private: bool = False,
) -> int:
    """流式下载 URL 到 dest（用于本地优先解析：先落盘再交给本地 MinerU）。

    超过 max_bytes 立即抛错并保证不残留半截文件；网络层经可注入的
    `opener(url, timeout) -> 支持 .read(size) 的上下文管理器` 完成，单测无需打网络。
    ``allow_private`` 只作用于默认 opener（透传 ``_safe_urlopen`` 的 SSRF 检查）；
    注入了 ``opener`` 时它不参与，网络策略由注入方自负。
    """
    opener = opener or (lambda u, t: _default_opener(u, t, allow_private=allow_private))
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


def _default_opener(url: str, timeout: float, *, allow_private: bool = False):
    request = urllib.request.Request(url, method="GET")
    return _safe_urlopen(request, timeout, allow_private=allow_private)


def _display_name(parsed: ParseResult) -> str:
    base = os.path.basename(parsed.path) or parsed.netloc or "source"
    if not base.lower().endswith(".pdf"):
        base = f"{base}.pdf"
    return base


def _default_fetch(
    url: str, timeout: float, *, allow_private: bool = False
) -> FetchResult:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Range", "bytes=0-1023")
    with _safe_urlopen(request, timeout, allow_private=allow_private) as response:
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
