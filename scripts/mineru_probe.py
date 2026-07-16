#!/usr/bin/env python3
"""MinerU 单文件自检探针（诊断用）。

给一个 PDF（或 docx/pptx）路径，用**项目自己的配置和真实代码路径**把它发给
已配置的 MinerU 服务端，判断能否正确解析并给出可诊断的结论。

它复用的是 app 上传 PDF 时的同一条链路——`app.services.mineru_client.MinerUClient`
（`MINERU_MODE=http` 走内网 `mineru-api` 的 `/file_parse`；`MINERU_MODE=cli` 走本地
子进程）+ `app.services.parsers.mineru_content_list_to_elements` 的映射。因此：
探针通过，等价于「同样的文件在 app 里也能被这台服务端解析出结构化内容」；探针
失败，则原样复现 app 会遇到的失败。

用法：

    python scripts/mineru_probe.py /path/to/paper.pdf
    python scripts/mineru_probe.py /path/to/paper.pdf --dump /tmp/content_list.json

配置一律来自仓库根 `.env`（经 `app.core.config`，锚定到仓库根、与 CWD 无关）。
本脚本**不**测 mineru.net 云端（那是 URL 来源的独立 token 路径），也不测异步
`/tasks` 批量端点（见 `scripts/mineru_batch_parse.py`）。

退出码：0 = 解析成功（映射出 >=1 个元素）；1 = 未配置 / 参数错 / 文件不存在
（根本没发起请求）；2 = 已发起但失败（异常，或返回内容为空 / 映射为 0 个元素）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# app-importing 脚本惯例（同 scripts/kg_product_smoke.py）：把主 checkout 的
# backend/ 插进 sys.path，再 import app.*。Settings 自身会读仓库根 .env，无需
# load_dotenv。⚠ 请从主 checkout 根跑，别在 worktree 里跑（worktree .env 多为空）。
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_BACKEND = _REPO / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx"}


# ---------------------------------------------------------------------------
# 纯函数（不 import app，便于单测）
# ---------------------------------------------------------------------------


def summarize_content_list(content_list: List[Any]) -> Dict[str, Any]:
    """把 MinerU 的 content_list 归一成一份体检报告：块数 / 类型直方图 / 页数。"""
    by_type: Dict[str, int] = {}
    pages: set[int] = set()
    for block in content_list:
        if not isinstance(block, dict):
            by_type["<non-dict>"] = by_type.get("<non-dict>", 0) + 1
            continue
        block_type = str(block.get("type", "") or "<none>").lower()
        by_type[block_type] = by_type.get(block_type, 0) + 1
        if "page_idx" in block:
            try:
                pages.add(int(block.get("page_idx", 0)))
            except (TypeError, ValueError):
                pass
    return {"blocks": len(content_list), "by_type": by_type, "pages": len(pages)}


def preview_texts(content_list: List[Any], limit: int, width: int = 100) -> List[str]:
    """取前 limit 个非空 text/title 块的文本，各截断到 width 字符，供肉眼核对。"""
    out: List[str] = []
    for block in content_list:
        if len(out) >= limit:
            break
        if not isinstance(block, dict):
            continue
        if str(block.get("type", "")).lower() not in {"text", "title"}:
            continue
        text = " ".join(str(block.get("text", "")).split()).strip()
        if not text:
            continue
        out.append(text if len(text) <= width else text[:width] + "…")
    return out


def classify_error(exc: BaseException) -> str:
    """按异常类型给一句可操作的排障提示（HTTPError 是 URLError 子类，先判它）。"""
    import urllib.error

    msg = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return (
            f"服务端返回 HTTP {exc.code}：请求到达了 MinerU 服务但被拒绝/出错。"
            "检查服务端日志，以及 backend / parse_method / lang 等参数是否被该服务支持。"
        )
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        low = str(reason).lower()
        if isinstance(reason, TimeoutError) or "timed out" in low:
            return "连接超时：服务端不可达或响应过慢。检查网络连通与 MINERU_TIMEOUT_SECONDS。"
        if isinstance(reason, ConnectionRefusedError) or "refused" in low:
            return "连接被拒绝：该地址上没有 MinerU 服务在监听。确认服务已启动、MINERU_API_URL 正确（含端口）。"
        if "getaddrinfo" in low or "name or service not known" in low or "servname" in low:
            return "主机名解析失败：MINERU_API_URL 的主机名 DNS 解析不了。检查地址是否正确/可达。"
        return f"网络错误：{reason}。检查 MINERU_API_URL 是否可达。"
    if isinstance(exc, TimeoutError):
        return "超时：调大 MINERU_TIMEOUT_SECONDS，或检查服务端负载。"
    if "did not contain a content_list" in msg:
        return "服务端有响应但返回体里没有 content_list：mineru-api 版本/契约不匹配，或该文件解析失败。"
    if "MinerU Python API" in msg:
        return "本地 CLI 模式（MINERU_MODE=cli）解析失败：确认已安装 mineru Python 包及其模型权重。"
    return "未分类错误，详见上面的原始报错。"


def _http_error_body(exc: BaseException) -> str:
    """尽力读出 HTTPError 的响应体（可能已关闭，读不到就算了）。"""
    try:
        import urllib.error

        if isinstance(exc, urllib.error.HTTPError):
            return exc.read().decode("utf-8", "replace").strip()[:500]
    except Exception:
        pass
    return ""


def resolve_proxy(url: str) -> Dict[str, Any]:
    """报告 urllib 对该 URL 的代理决策：环境代理表 / no_proxy / 是否绕过 / 实际会用的代理。

    用 urllib 自己的 `getproxies()` + `proxy_bypass()`，与内网调用默认 opener 同口径，
    因此能一眼看出「环境里有代理、且 no_proxy 没覆盖这个主机」这类会导致 504 的配置——
    尤其是 `no_proxy` 写成 CIDR 网段（如 `10.0.0.0/8`）时 urllib 并不识别、不会绕过。
    """
    import urllib.request
    from urllib.parse import urlsplit

    proxies = urllib.request.getproxies()
    parts = urlsplit(url)
    host = parts.hostname or ""
    scheme = parts.scheme or "http"
    bypassed = bool(host) and bool(urllib.request.proxy_bypass(host))
    effective = None
    if not bypassed:
        effective = proxies.get(scheme) or proxies.get("http")
    return {
        "env_proxies": {k: v for k, v in proxies.items() if k != "no"},
        "no_proxy": proxies.get("no", ""),
        "host": host,
        "bypassed": bypassed,
        "effective_proxy": effective,
    }


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------


def _kv(label: str, value: Any) -> None:
    print(f"  {label:<18} {value}")


def _rule(title: str = "") -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)) if title else "─" * 60)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _print_config(settings: Any) -> None:
    _rule("生效的 MinerU 配置（来自仓库根 .env）")
    mode = (settings.mineru_mode or "off").lower()
    _kv("MINERU_MODE", mode)
    if mode == "http":
        _kv("MINERU_API_URL", settings.mineru_api_url or "(空)")
        if settings.mineru_vlm_server_url:
            _kv("VLM_SERVER_URL", settings.mineru_vlm_server_url)
    _kv("backend", settings.mineru_backend)
    _kv("parse_method", settings.mineru_parse_method)
    _kv("lang", settings.mineru_lang or "(默认/auto)")
    _kv("formula_enable", settings.mineru_formula_enable)
    _kv("table_enable", settings.mineru_table_enable)
    _kv("timeout_seconds", settings.mineru_timeout_seconds)
    if mode == "cli":
        _kv("model_source", settings.mineru_model_source)

    # http 模式：暴露 urllib 对该 URL 的环境代理决策。内网调用被正向代理接管、
    # 又没被 no_proxy 绕过时会回 504（no_proxy 写成 CIDR 网段 urllib 不认）。
    if mode == "http" and settings.mineru_api_url:
        info = resolve_proxy(settings.mineru_api_url)
        if not info["env_proxies"]:
            _kv("环境代理", "(无 — 直连)")
        else:
            _kv("环境代理", ", ".join(f"{k}={v}" for k, v in sorted(info["env_proxies"].items())))
            _kv("no_proxy", info["no_proxy"] or "(空)")
            if info["effective_proxy"]:
                _kv("⚠ 该URL会走代理", f"{info['effective_proxy']}（host={info['host']} 未被 no_proxy 绕过）")
                print("      ↑ 内网调用经正向代理是 504 的典型根因；本探针已在代码层绕过，")
                print("        但请把 no_proxy 改成裸 IP（CIDR 网段 urllib 不识别）以修正环境。")
            else:
                _kv("该URL代理", f"绕过直连（host={info['host']}）")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用项目配置测试一个 PDF 能否被 MinerU 服务端正确解析。",
    )
    parser.add_argument("pdf", help="待测文件路径（.pdf/.docx/.pptx）")
    parser.add_argument(
        "--dump",
        metavar="PATH",
        help="把服务端返回的原始 content_list 写成 JSON 到该路径，供肉眼核对。",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=3,
        metavar="N",
        help="预览前 N 段正文（默认 3，设 0 关闭）。",
    )
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.services.mineru_client import MinerUClient
    from app.services.parsers import mineru_content_list_to_elements

    settings = get_settings()
    _print_config(settings)

    # ── 预检：文件 + 是否已配置（都属于「根本没发起请求」，退出码 1）──────────
    pdf = Path(args.pdf).expanduser()
    _rule("预检")
    if not pdf.is_file():
        print(f"✗ 文件不存在或不是普通文件：{pdf}")
        return 1
    size = pdf.stat().st_size
    if size == 0:
        print(f"✗ 文件为空（0 字节）：{pdf}")
        return 1
    _kv("文件", pdf)
    _kv("大小", f"{size / 1024:.1f} KiB")
    if pdf.suffix.lower() not in _SUPPORTED_SUFFIXES:
        print(f"  ⚠ 后缀 {pdf.suffix!r} 不在 {sorted(_SUPPORTED_SUFFIXES)} 内，仍会尝试发送。")

    client = MinerUClient(settings)
    if not client.configured:
        mode = (settings.mineru_mode or "off").lower()
        print(f"✗ MinerU 未启用（mode={mode}）。")
        if mode == "off":
            print("  → 在仓库根 .env 设 MINERU_MODE=http 且 MINERU_API_URL=http://<gpu-host>:8000，")
            print("    或 MINERU_MODE=cli（需本机装 mineru 包）。")
        elif mode == "http":
            print("  → http 模式缺 MINERU_API_URL，请在 .env 填服务端地址（含端口）。")
        return 1
    print(f"✓ 已配置（mode={client.mode}），准备发送。")

    # ── 真正发送并计时（app 上传 PDF 的同一条路径）──────────────────────────
    _rule("发送并解析")
    started = time.monotonic()
    try:
        content_list = client.parse(str(pdf), pdf.name)
    except Exception as exc:  # noqa: BLE001 — 探针要吞下并分类所有失败
        elapsed = time.monotonic() - started
        print(f"✗ 解析失败（耗时 {elapsed:.1f}s）")
        _kv("原始报错", client.last_error or repr(exc))
        body = _http_error_body(exc)
        if body:
            _kv("服务端响应体", body)
        print(f"  → {classify_error(exc)}")
        return 2
    elapsed = time.monotonic() - started

    # ── 体检报告：原始 content_list ────────────────────────────────────────
    summary = summarize_content_list(content_list)
    if summary["blocks"] == 0:
        print(f"✗ 服务端有响应但 content_list 为空（耗时 {elapsed:.1f}s）——等价于解析失败。")
        return 2
    print(f"✓ 服务端返回 content_list（耗时 {elapsed:.1f}s）")
    _kv("原始块数", summary["blocks"])
    _kv("覆盖页数", summary["pages"])
    _kv("块类型分布", ", ".join(f"{k}={v}" for k, v in sorted(summary["by_type"].items())))

    # ── 映射为 app 真正消费的结构化元素（app 的成功判据）──────────────────────
    elements = mineru_content_list_to_elements("probe", content_list)
    by_el: Dict[str, int] = {}
    for el in elements:
        et = str(getattr(el, "element_type", "?"))
        by_el[et] = by_el.get(et, 0) + 1
    _kv("映射后元素数", len(elements))
    if elements:
        _kv("元素类型分布", ", ".join(f"{k}={v}" for k, v in sorted(by_el.items())))

    previews = preview_texts(content_list, args.preview) if args.preview > 0 else []
    if previews:
        _rule("正文预览")
        for i, text in enumerate(previews, 1):
            print(f"  [{i}] {text}")

    if args.dump:
        dump_path = Path(args.dump).expanduser()
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(
            json.dumps(content_list, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _rule()
        print(f"已写出原始 content_list → {dump_path}")

    _rule("结论")
    if not elements:
        print("✗ 解析出了原始块，但映射后为 0 个结构化元素——app 会当作解析失败拒绝。")
        print("  → 检查该文件是否为扫描件/纯图无文字，或服务端 parse_method 是否合适。")
        return 2
    print(f"✓ 通过：这台 MinerU 服务端能正确解析该文件（{len(elements)} 个结构化元素）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
