import io
import json
import zipfile

import pytest

from app.core.config import Settings
from app.services.mineru_cloud_client import MinerUCloudClient, MinerUCloudNotConfigured


def _client(monkeypatch, **env):
    monkeypatch.setenv("MINERU_API_TOKEN", env.get("token", "tok"))
    monkeypatch.setenv("MINERU_CLOUD_POLL_INTERVAL_SECONDS", env.get("interval", "1"))
    monkeypatch.setenv("MINERU_CLOUD_TIMEOUT_SECONDS", env.get("timeout", "600"))
    c = MinerUCloudClient(Settings())
    c._sleep = lambda s: None  # 不真睡
    return c


def _zip_with(name, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def test_not_configured_raises(monkeypatch):
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    c = MinerUCloudClient(Settings())
    assert c.configured is False
    with pytest.raises(MinerUCloudNotConfigured):
        c.parse_url("https://a/x.pdf")


def test_happy_path_returns_content_list(monkeypatch):
    c = _client(monkeypatch)
    content = [{"type": "text", "text": "Hello world", "page_idx": 0}]
    responses = iter([
        {"code": 0, "data": {"task_id": "t1"}},          # submit
        {"data": {"state": "pending"}},                   # poll 1
        {"data": {"state": "done", "full_zip_url": "https://z/r.zip"}},  # poll 2
    ])
    c._http_json = lambda method, url, payload=None: next(responses)
    c._http_bytes = lambda url: _zip_with("out/abc_content_list.json", json.dumps(content).encode())
    assert c.parse_url("https://a/x.pdf", data_id="src-1") == content


def test_failed_state_raises_with_err_msg(monkeypatch):
    c = _client(monkeypatch)
    responses = iter([
        {"code": 0, "data": {"task_id": "t1"}},
        {"data": {"state": "failed", "err_msg": "超过页数限制"}},
    ])
    c._http_json = lambda method, url, payload=None: next(responses)
    with pytest.raises(RuntimeError) as exc:
        c.parse_url("https://a/x.pdf")
    assert "超过页数限制" in str(exc.value)
    assert "超过页数限制" in c.last_error


def test_poll_timeout_raises(monkeypatch):
    c = _client(monkeypatch, interval="1", timeout="2")  # 最多 2 次轮询
    seq = iter([{"code": 0, "data": {"task_id": "t1"}}])  # 提交一次；其后轮询恒 running
    c._http_json = lambda method, url, payload=None: next(seq, {"data": {"state": "running"}})
    with pytest.raises(RuntimeError) as exc:
        c.parse_url("https://a/x.pdf")
    assert "超时" in str(exc.value)


def test_markdown_fallback_when_no_content_list(monkeypatch):
    c = _client(monkeypatch)
    responses = iter([
        {"code": 0, "data": {"task_id": "t1"}},
        {"data": {"state": "done", "full_zip_url": "https://z/r.zip"}},
    ])
    c._http_json = lambda method, url, payload=None: next(responses)
    c._http_bytes = lambda url: _zip_with("out/full.md", b"# Title\n\nFirst para.\n\nSecond para.")
    out = c.parse_url("https://a/x.pdf")
    texts = [b["text"] for b in out]
    assert "Title" in texts[0] and any("First para." in t for t in texts)
