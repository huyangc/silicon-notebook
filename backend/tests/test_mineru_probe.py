"""单元测试：`scripts/mineru_probe.py`（部署侧诊断脚本，不属于 app 包）。

按文件路径直接 import（同 test_mineru_batch_parse.py）。只测其纯函数，不触网、
不 import app（app 仅在脚本 main() 内部懒加载）。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "mineru_probe.py"
_spec = importlib.util.spec_from_file_location("mineru_probe", _SCRIPT_PATH)
probe = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["mineru_probe"] = probe
_spec.loader.exec_module(probe)


def test_resolve_proxy_flags_unbypassed_host(monkeypatch):
    """环境有代理、no_proxy 用 CIDR 没命中 → 报告"会走代理"(即 504 隐患)。"""
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"http": "http://p:8080", "https": "http://p:8080", "no": "10.0.0.0/8"},
    )
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)

    info = probe.resolve_proxy("http://10.155.112.15:8000/file_parse")
    assert info["host"] == "10.155.112.15"
    assert info["bypassed"] is False
    assert info["effective_proxy"] == "http://p:8080"
    assert info["no_proxy"] == "10.0.0.0/8"


def test_resolve_proxy_bypassed_host_has_no_effective_proxy(monkeypatch):
    """no_proxy 用裸 IP 命中 → 报告"直连、无代理"。"""
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"http": "http://p:8080", "no": "10.155.112.15"},
    )
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)

    info = probe.resolve_proxy("http://10.155.112.15:8000/file_parse")
    assert info["bypassed"] is True
    assert info["effective_proxy"] is None


def test_resolve_proxy_no_proxy_env_at_all(monkeypatch):
    """完全没配代理 → 直连、无代理。"""
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)

    info = probe.resolve_proxy("http://10.155.112.15:8000/file_parse")
    assert info["env_proxies"] == {}
    assert info["effective_proxy"] is None


def test_summarize_content_list_histogram():
    blocks = [
        {"type": "text", "text": "t", "page_idx": 0},
        {"type": "equation", "text": "$$x$$", "page_idx": 0},
        {"type": "table", "table_body": "<table></table>", "page_idx": 1},
        "not-a-dict",
    ]
    summary = probe.summarize_content_list(blocks)
    assert summary["blocks"] == 4
    assert summary["pages"] == 2
    assert summary["by_type"] == {"text": 1, "equation": 1, "table": 1, "<non-dict>": 1}
