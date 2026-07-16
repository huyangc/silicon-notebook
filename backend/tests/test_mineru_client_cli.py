import base64
import json
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.mineru_client import (
    MinerUClient,
    _decode_data_uri,
    _extract_content_list,
    _extract_images,
)


def _cli_client(monkeypatch):
    # 仓库约定：用 setenv + Settings() 构造（pydantic-settings 读 env，非 kwargs）。
    monkeypatch.setenv("MINERU_MODE", "cli")
    monkeypatch.setenv("MINERU_BACKEND", "pipeline")
    monkeypatch.setenv("MINERU_LANG", "ch")
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "huggingface")
    monkeypatch.setenv("MINERU_TIMEOUT_SECONDS", "30")
    return MinerUClient(Settings())


class FakePopen:
    """假子进程：把 content_list.json 写进命令里 -o / out_dir 指向的目录。"""

    captured_cmd = []

    def __init__(self, cmd, **kwargs):
        FakePopen.captured_cmd = cmd
        out_dir = Path(cmd[cmd.index("-o") + 1]) if "-o" in cmd else _out_dir_from_pdf_cmd(cmd)
        (out_dir / "doc_content_list.json").write_text(
            json.dumps([{"type": "text", "text": "ok", "page_idx": 0}]), encoding="utf-8"
        )

    def communicate(self, timeout=None):
        return (b"", b"")

    @property
    def returncode(self):
        return 0


def _out_dir_from_pdf_cmd(cmd):
    # PDF 路径命令是 [python, script, config.json]；out_dir 写在 config 里。
    config = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
    return Path(config["out_dir"])


def test_cli_office_uses_mineru_command(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    docx = tmp_path / "doc.docx"
    docx.write_bytes(b"PK\x03\x04stub")
    out = _cli_client(monkeypatch).parse(str(docx), "doc.docx")
    assert FakePopen.captured_cmd[0] == "mineru"
    assert "-p" in FakePopen.captured_cmd and "-o" in FakePopen.captured_cmd
    assert out == [{"type": "text", "text": "ok", "page_idx": 0}]


_BLOCKS = [{"type": "text", "text": "ok", "page_idx": 0}]


def test_extract_v34_results_wrapper_with_json_string():
    # MinerU v3.4：{"results": {"file.pdf": {"content_list": "<json string>"}}}
    payload = {"results": {"doc.pdf": {"content_list": json.dumps(_BLOCKS)}}}
    assert _extract_content_list(payload) == _BLOCKS


def test_extract_v34_results_wrapper_with_list():
    payload = {"results": {"doc.pdf": {"content_list": _BLOCKS}}}
    assert _extract_content_list(payload) == _BLOCKS


def test_extract_top_level_json_string():
    assert _extract_content_list({"content_list": json.dumps(_BLOCKS)}) == _BLOCKS


def test_extract_legacy_keyed_by_filename():
    assert _extract_content_list({"doc.pdf": {"content_list": _BLOCKS}}) == _BLOCKS


def test_extract_bare_list():
    assert _extract_content_list(_BLOCKS) == _BLOCKS


def test_extract_missing_content_list_raises():
    with pytest.raises(RuntimeError, match="did not contain a content_list"):
        _extract_content_list({"results": {"doc.pdf": {"md": "x"}}})


def test_cli_pdf_still_uses_do_parse_script(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    out = _cli_client(monkeypatch).parse(str(pdf), "doc.pdf")
    assert FakePopen.captured_cmd[0] != "mineru"  # [python, run_mineru_parse.py, config.json]
    assert FakePopen.captured_cmd[1].endswith("run_mineru_parse.py")
    assert out == [{"type": "text", "text": "ok", "page_idx": 0}]


def test_http_parse_bypasses_environment_proxy(monkeypatch, tmp_path):
    """内网 MinerU 调用必须绕过环境代理(http_proxy/no_proxy),否则正向代理够不到
    内网主机会回 504。见 mineru-504-noproxy-cidr。"""
    import urllib.request

    payload = json.dumps([{"type": "text", "text": "ok", "page_idx": 0}])
    captured: dict = {}

    class _Resp:
        def read(self):
            return payload.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeOpener:
        def __init__(self, proxies):
            captured["proxies"] = proxies

        def open(self, request, timeout=None):
            captured["via"] = "opener"
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return _Resp()

    def _fake_build_opener(*handlers):
        proxies = None
        for handler in handlers:
            if isinstance(handler, urllib.request.ProxyHandler):
                proxies = handler.proxies
        return _FakeOpener(proxies)

    def _default_urlopen(*a, **k):
        # 默认 opener 会读环境代理 —— 内网调用绝不该落到这里。
        captured["via"] = "urlopen"
        return _Resp()

    monkeypatch.setattr(urllib.request, "build_opener", _fake_build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", _default_urlopen)
    # 即便环境里配了代理,也必须绕过。
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("MINERU_MODE", "http")
    monkeypatch.setenv("MINERU_API_URL", "http://10.155.112.15:8000")
    monkeypatch.setenv("MINERU_TIMEOUT_SECONDS", "42")

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = MinerUClient(Settings()).parse(str(pdf), "x.pdf")

    assert out == [{"type": "text", "text": "ok", "page_idx": 0}]
    assert captured.get("via") == "opener", "http 解析必须走显式 opener,而非会读环境代理的默认 urlopen"
    assert captured.get("proxies") == {}, "opener 必须带空 ProxyHandler({}) 以绕过所有环境代理"
    assert captured["url"].endswith("/file_parse")
    assert captured["timeout"] == 42
def test_decode_data_uri_roundtrip():
    raw = b"\x89PNG\r\n_binary_"
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    assert _decode_data_uri(uri) == raw
    assert _decode_data_uri("not-a-data-uri") is None


def test_extract_images_from_http_payload():
    raw = b"JPEGBYTES"
    uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    payload = {"results": {"doc.pdf": {"content_list": [], "images": {"images/abc.jpg": uri}}}}
    images = _extract_images(payload)
    assert images == {"abc.jpg": raw}  # 以 basename 为键


def test_parse_with_images_http_gated_off(monkeypatch):
    # return_images=False 时不请求图片、images 为空，content_list 照常。
    from app.core.config import Settings
    from app.services.mineru_client import MinerUClient
    s = Settings(MINERU_MODE="http", MINERU_API_URL="http://x", MINERU_RETURN_IMAGES="0")
    client = MinerUClient(s)
    monkeypatch.setattr(client, "_post_file_parse",
                        lambda fields, fp, fn: {"results": {"d.pdf": {"content_list": [{"type": "text", "text": "hi"}]}}})
    cl, images = client.parse_with_images("/tmp/d.pdf", "d.pdf")
    assert images == {}
    assert cl and cl[0]["text"] == "hi"


class FakePopenWithImages:
    """假子进程：除 content_list.json 外，还在 out_dir 下建 images/ 子目录写一张图。"""

    captured_cmd = []

    def __init__(self, cmd, **kwargs):
        FakePopenWithImages.captured_cmd = cmd
        out_dir = Path(cmd[cmd.index("-o") + 1]) if "-o" in cmd else _out_dir_from_pdf_cmd(cmd)
        (out_dir / "doc_content_list.json").write_text(
            json.dumps([{"type": "text", "text": "ok", "page_idx": 0}]), encoding="utf-8"
        )
        images_dir = out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "fig1.jpg").write_bytes(b"JPEGDATA")

    def communicate(self, timeout=None):
        return (b"", b"")

    @property
    def returncode(self):
        return 0


def test_cli_parse_with_images_copies_images_dir_before_cleanup(monkeypatch, tmp_path):
    # return_images=True 时，images/ 目录里的文件要在临时目录销毁前拷成 basename->bytes。
    monkeypatch.setattr(subprocess, "Popen", FakePopenWithImages)
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "1")
    client = _cli_client(monkeypatch)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    content_list, images = client.parse_with_images(str(pdf), "doc.pdf")
    assert content_list == [{"type": "text", "text": "ok", "page_idx": 0}]
    assert images == {"fig1.jpg": b"JPEGDATA"}


def test_cli_parse_with_images_gated_off_skips_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", FakePopenWithImages)
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "0")
    client = _cli_client(monkeypatch)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    content_list, images = client.parse_with_images(str(pdf), "doc.pdf")
    assert content_list == [{"type": "text", "text": "ok", "page_idx": 0}]
    assert images == {}
