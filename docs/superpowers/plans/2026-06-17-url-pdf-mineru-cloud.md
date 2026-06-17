# 在线 URL → PDF → MinerU 云端解析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户粘贴一个或多个公开 PDF 直链作为来源，非 PDF 立即拒绝，PDF 交给 mineru.net 云端 v4 解析并接入现有 elements→chunks→embed→KG 流水线。

**Architecture:** 新增自包含的云端客户端 `mineru_cloud_client.py`（提交 URL 任务→轮询→下 ZIP→取 content_list）与 PDF 初筛 `remote_sources.py`；新端点 `POST /api/notebooks/{id}/sources/url` 逐 URL 初筛后建 `source_url` 来源并走现有 `process_source`，解析步对 URL 来源短路到云端客户端，其余下游完全复用。云端通道与现有 `MINERU_MODE`(http/cli) 互不影响。

**Tech Stack:** Python / FastAPI / pydantic-settings / sqlite3 / stdlib `urllib`+`zipfile`+`json`；Next.js / React / TypeScript（`node --test` + `tsc --noEmit`）。

**Spec:** `docs/superpowers/specs/2026-06-17-url-pdf-mineru-cloud-design.md`

**运行约定（来自 AGENTS.md / 仓库现状）：**
- Python 解释器：`/opt/homebrew/Caskroom/miniconda/base/bin/python`（**不要**新建 venv/conda）。
- 后端测试：`cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest <path> -v`。
- 前端：`cd frontend && npm run test`（= `node --test app/*.test.mjs`）、`npm run lint`（= `tsc --noEmit`）。
- 每个 Task 末尾提交一次。`MINERU_API_TOKEN` 已由用户预置进主仓 `.env`（gitignore，勿提交）。
- 提交信息用中文，结尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## File Structure

**新建（均自包含、可独立单测）：**
- `backend/app/services/remote_sources.py` — PDF URL 初筛（`probe_pdf`）。
- `backend/app/services/mineru_cloud_client.py` — mineru.net 云端 v4 客户端（`MinerUCloudClient.parse_url` + `MinerUCloudNotConfigured`）。
- `backend/tests/test_mineru_cloud_config.py`、`test_remote_sources.py`、`test_mineru_cloud_client.py`、`test_url_sources.py`、`test_url_sources_api.py`。
- `frontend/app/url-sources.ts` + `frontend/app/url-sources.test.mjs` — URL 文本解析（去重/scheme 轻校验）。

**修改：**
- `backend/app/core/config.py` — 新增 `mineru_*`/`mineru_cloud_*` 字段 + `mineru_cloud_enabled` 属性。
- `.env.example` — 新增云端配置块（占位）。
- `backend/app/models/schemas.py` — `SourceSummary.source_url` + `AddUrlSourcesRequest`/`RejectedUrl`/`AddUrlSourcesResult`。
- `backend/app/services/sqlite_repository.py` — 建表/迁移加 `source_url`、`_source_from_row` 读出、构造云客户端、`add_url_sources`、`process_source` URL 分支。
- `backend/app/services/repository.py` — Protocol 增 `add_url_sources`。
- `backend/app/api/routes.py` — 新端点 + 异常映射。
- `frontend/app/page.tsx` — `SourceSummary` 加字段、「添加链接」按钮/弹窗/提交、URL 卡片外链。

---

## Task 1: 云端配置（Settings + .env.example）

**Files:**
- Modify: `backend/app/core/config.py:185`（`mineru_table_enable` 之后）与 `:252`（`mineru_enabled` 之后）
- Modify: `.env.example:147`（`MINERU_VLM_SERVER_URL=` 之后）
- Test: `backend/tests/test_mineru_cloud_config.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mineru_cloud_config.py
from app.core.config import Settings


def test_cloud_enabled_and_defaults_with_token(monkeypatch):
    monkeypatch.setenv("MINERU_API_TOKEN", "tok-123")
    s = Settings()
    assert s.mineru_cloud_enabled is True
    assert s.mineru_api_base == "https://mineru.net"
    assert s.mineru_cloud_model_version == "vlm"
    assert s.mineru_cloud_language == "ch"
    assert s.mineru_cloud_timeout_seconds == 600
    assert s.mineru_cloud_poll_interval_seconds == 5


def test_cloud_disabled_without_token(monkeypatch):
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    s = Settings()
    assert s.mineru_cloud_enabled is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_mineru_cloud_config.py -v`
Expected: FAIL（`AttributeError: ... mineru_cloud_enabled` 或字段不存在）

- [ ] **Step 3: 加配置字段与属性**

在 `backend/app/core/config.py` 第 185 行 `mineru_table_enable = ...` 之后插入：

```python
    # MinerU.net cloud (v4) — parse a public PDF URL via the hosted service.
    # 独立于上面的 MINERU_MODE(off/http/cli)；仅用于 URL 来源的 PDF。
    mineru_api_token: str = Field("", env="MINERU_API_TOKEN")
    mineru_api_base: str = Field("https://mineru.net", env="MINERU_API_BASE")
    mineru_cloud_model_version: str = Field("vlm", env="MINERU_CLOUD_MODEL_VERSION")
    mineru_cloud_language: str = Field("ch", env="MINERU_CLOUD_LANGUAGE")
    mineru_cloud_formula_enable: bool = Field(True, env="MINERU_CLOUD_FORMULA_ENABLE")
    mineru_cloud_table_enable: bool = Field(True, env="MINERU_CLOUD_TABLE_ENABLE")
    mineru_cloud_timeout_seconds: int = Field(600, env="MINERU_CLOUD_TIMEOUT_SECONDS")
    mineru_cloud_poll_interval_seconds: int = Field(5, env="MINERU_CLOUD_POLL_INTERVAL_SECONDS")
```

在 `mineru_enabled` 属性（约第 246–252 行）之后插入：

```python
    @property
    def mineru_cloud_enabled(self) -> bool:
        return bool(self.mineru_api_token)
```

- [ ] **Step 4: 更新 .env.example**

在 `.env.example` 第 147 行 `MINERU_VLM_SERVER_URL=` 之后追加：

```bash
# MinerU.net cloud (v4) — parse public PDF URLs via the hosted service.
# 独立于上面的 MINERU_MODE；仅用于「添加链接」加入的 PDF 来源。
# 从 https://mineru.net/apiManage/docs 申请 token 后填入：
MINERU_API_TOKEN=
MINERU_API_BASE=https://mineru.net
MINERU_CLOUD_MODEL_VERSION=vlm
MINERU_CLOUD_LANGUAGE=ch
MINERU_CLOUD_FORMULA_ENABLE=true
MINERU_CLOUD_TABLE_ENABLE=true
MINERU_CLOUD_TIMEOUT_SECONDS=600
MINERU_CLOUD_POLL_INTERVAL_SECONDS=5
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_mineru_cloud_config.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: 提交**

```bash
git add backend/app/core/config.py .env.example backend/tests/test_mineru_cloud_config.py
git commit -m "feat(config): MinerU 云端(v4) 配置项 + mineru_cloud_enabled

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: PDF URL 初筛 `remote_sources.probe_pdf`

**Files:**
- Create: `backend/app/services/remote_sources.py`
- Test: `backend/tests/test_remote_sources.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_remote_sources.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_remote_sources.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.remote_sources`）

- [ ] **Step 3: 实现 `remote_sources.py`**

```python
# backend/app/services/remote_sources.py
"""公开 PDF 直链的轻量初筛：判定 URL 是否指向可解析的 PDF。

网络 I/O 经一个可注入的 `fetch` 回调完成，因此单测无需真打网络。判定规则：
Content-Type 以 application/pdf 开头，或响应首字节为 %PDF-，即视为 PDF。
"""
from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional
from urllib.parse import urlparse

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


def _display_name(parsed) -> str:
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_remote_sources.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/remote_sources.py backend/tests/test_remote_sources.py
git commit -m "feat(sources): PDF URL 初筛 probe_pdf(可注入 fetch, 不打真网)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 云端客户端 `MinerUCloudClient.parse_url`

**Files:**
- Create: `backend/app/services/mineru_cloud_client.py`
- Test: `backend/tests/test_mineru_cloud_client.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mineru_cloud_client.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_mineru_cloud_client.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.mineru_cloud_client`）

- [ ] **Step 3: 实现 `mineru_cloud_client.py`**

```python
# backend/app/services/mineru_cloud_client.py
"""MinerU.net 云端(v4) 适配器：把一个公开 PDF URL 解析为 content_list。

与 mineru_client.py(自建 http / 本地 cli) 区分开：云端是异步流程——提交 URL
任务→轮询至终态→下载结果 ZIP→读取其中的 content_list.json——故独立成模块，
入口接收 URL 而非本地文件。网络 I/O 经 `_http_json` / `_http_bytes` / `_sleep`
三个可覆写的接缝完成，编排逻辑可在不打网络的情况下单测。Bearer token 不入日志。
"""
from __future__ import annotations

import io
import json
import math
import time
import urllib.request
import zipfile
from typing import List, Optional

from app.core.config import Settings


class MinerUCloudNotConfigured(RuntimeError):
    """URL 来源被请求但未配置 MINERU_API_TOKEN 时抛出。"""


class MinerUCloudClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return self.settings.mineru_cloud_enabled

    def parse_url(self, url: str, *, data_id: str = "") -> List[dict]:
        """提交 PDF URL 给云端并返回 content_list。

        未配置 token → MinerUCloudNotConfigured；云端失败/超时/结果不可用 → RuntimeError。
        """
        self.last_error = ""
        if not self.configured:
            raise MinerUCloudNotConfigured("未配置 MinerU 云端凭证 (MINERU_API_TOKEN)")
        try:
            task_id = self._submit(url, data_id)
            zip_url = self._poll(task_id)
            zip_bytes = self._http_bytes(zip_url)
            content_list = self._content_list_from_zip(zip_bytes)
            if not content_list:
                raise RuntimeError("MinerU 云端结果为空 content_list")
            return content_list
        except Exception as exc:
            if not self.last_error:
                self.last_error = str(exc)
            raise

    # -- steps -----------------------------------------------------------------

    def _submit(self, url: str, data_id: str) -> str:
        body = {
            "url": url,
            "model_version": self.settings.mineru_cloud_model_version,
            "is_ocr": False,
            "enable_formula": self.settings.mineru_cloud_formula_enable,
            "enable_table": self.settings.mineru_cloud_table_enable,
            "language": self.settings.mineru_cloud_language,
        }
        if data_id:
            body["data_id"] = data_id
        payload = self._http_json("POST", self._api("/api/v4/extract/task"), body)
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(f"MinerU 云端提交失败: {payload.get('msg') or payload}")
        task_id = (payload.get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError("MinerU 云端未返回 task_id")
        return str(task_id)

    def _poll(self, task_id: str) -> str:
        interval = max(1, int(self.settings.mineru_cloud_poll_interval_seconds))
        timeout = max(interval, int(self.settings.mineru_cloud_timeout_seconds))
        max_polls = max(1, math.ceil(timeout / interval))
        url = self._api(f"/api/v4/extract/task/{task_id}")
        for _ in range(max_polls):
            payload = self._http_json("GET", url)
            data = payload.get("data") or {}
            state = str(data.get("state", "")).lower()
            if state == "done":
                zip_url = data.get("full_zip_url")
                if not zip_url:
                    raise RuntimeError("MinerU 云端完成但缺少 full_zip_url")
                return str(zip_url)
            if state == "failed":
                raise RuntimeError(f"MinerU 云端解析失败: {data.get('err_msg') or '未知错误'}")
            self._sleep(interval)
        raise RuntimeError(f"MinerU 云端轮询超时 (>{timeout}s)")

    def _content_list_from_zip(self, zip_bytes: bytes) -> List[dict]:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            for name in sorted(names):
                if name.endswith("_content_list.json"):
                    data = json.loads(archive.read(name).decode("utf-8"))
                    if isinstance(data, list):
                        return data
            # 回退：从合并 markdown 合成最小 content_list（段落/标题，page 0）。
            for name in sorted(names):
                if name.endswith(".md") and not name.endswith(("_content_list.md", "_model.md")):
                    text = archive.read(name).decode("utf-8", "replace")
                    return _content_list_from_markdown(text)
        return []

    # -- network seams (tests override these) ----------------------------------

    def _http_json(self, method: str, url: str, payload: Optional[dict] = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.settings.mineru_api_token}")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def _http_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(
            request, timeout=self.settings.mineru_cloud_timeout_seconds
        ) as response:
            return response.read()

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _api(self, path: str) -> str:
        return self.settings.mineru_api_base.rstrip("/") + path


def _content_list_from_markdown(text: str) -> List[dict]:
    blocks: List[dict] = []
    for chunk in text.split("\n\n"):
        body = chunk.strip()
        if not body:
            continue
        if body.startswith("#"):
            blocks.append({"type": "text", "text": body.lstrip("#").strip(),
                           "text_level": 1, "page_idx": 0})
        else:
            blocks.append({"type": "text", "text": body, "page_idx": 0})
    return blocks
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_mineru_cloud_client.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/mineru_cloud_client.py backend/tests/test_mineru_cloud_client.py
git commit -m "feat(mineru): 云端(v4) URL 任务客户端 parse_url(提交/轮询/取 content_list)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Schemas（source_url + 请求/结果模型）

**Files:**
- Modify: `backend/app/models/schemas.py:44`（`SourceSummary`）与 `:62`（`SourceImportRequest` 之后）
- Test: `backend/tests/test_url_sources_schemas.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_url_sources_schemas.py
from app.models.schemas import (
    SourceSummary, AddUrlSourcesRequest, RejectedUrl, AddUrlSourcesResult,
)


def test_source_summary_source_url_defaults_empty():
    s = SourceSummary(id="s", notebook_id="n", title="t", type="pdf",
                      status="queued", summary="", element_count=0)
    assert s.source_url == ""


def test_add_url_sources_models():
    req = AddUrlSourcesRequest(urls=["https://a/x.pdf"])
    res = AddUrlSourcesResult(created=[], rejected=[RejectedUrl(url="u", reason="非 PDF")])
    assert req.urls == ["https://a/x.pdf"]
    assert res.rejected[0].reason == "非 PDF"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources_schemas.py -v`
Expected: FAIL（`ImportError` / `source_url` 不存在）

- [ ] **Step 3: 加字段与模型**

在 `backend/app/models/schemas.py` 的 `SourceSummary` 内 `file_hash: str = ""`（第 44 行）之后插入：

```python
    source_url: str = ""  # 非空表示这是「在线 URL」来源，由 mineru.net 云端解析
```

在 `SourceImportRequest`（第 60–61 行）之后插入：

```python
class AddUrlSourcesRequest(BaseModel):
    urls: List[str]


class RejectedUrl(BaseModel):
    url: str
    reason: str


class AddUrlSourcesResult(BaseModel):
    created: List[SourceSummary]
    rejected: List[RejectedUrl]
```

> 说明：`SourceDetail(SourceSummary)` 自动继承 `source_url`，无需改动。`List` 已在文件顶部导入。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources_schemas.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/tests/test_url_sources_schemas.py
git commit -m "feat(schemas): SourceSummary.source_url + AddUrlSources 请求/结果模型

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: DB 迁移 + `_source_from_row` 读出 source_url

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:293`（建表）、`:584`（迁移）、`:5636`（`_source_from_row`）
- Test: `backend/tests/test_source_url_persist.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_source_url_persist.py
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_source_url_persists_and_reads_back(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    sid, now = "src-urltest01", _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, status, "
            "parse_status, file_name, file_path, source_url, file_size, file_hash, "
            "summary, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pdf', 'queued', 'queued', ?, '', ?, 0, '', '', ?, ?)",
            (sid, nb.id, "paper.pdf", "paper.pdf", "https://x/paper.pdf", now, now),
        )
    assert repo.get_source(sid).source_url == "https://x/paper.pdf"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_source_url_persist.py -v`
Expected: FAIL（`sqlite3.OperationalError: table sources has no column named source_url`）

- [ ] **Step 3: 建表加列**

在 `sqlite_repository.py` 建表语句第 293 行 `file_path TEXT NOT NULL DEFAULT '',` 之后插入：

```python
                  source_url TEXT NOT NULL DEFAULT '',
```

- [ ] **Step 4: 旧库迁移**

在 `_migrate` 的 doc_type 迁移（第 582–584 行）之后插入（复用同一 `src_cols`）：

```python
            if "source_url" not in src_cols:
                db.execute("ALTER TABLE sources ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
```

- [ ] **Step 5: `_source_from_row` 读出**

在 `_source_from_row` 的 `SourceSummary(...)` 构造里（第 5636 行 `doc_type=...` 那行）之后插入：

```python
            source_url=row["source_url"] if "source_url" in row.keys() else "",
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_source_url_persist.py -v`
Expected: PASS（1 passed）

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_source_url_persist.py
git commit -m "feat(db): sources.source_url 列(建表+旧库迁移)+ 读出到 SourceSummary

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `add_url_sources`（初筛 + 建来源 + token 闸）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:76-78`（imports）、`:194`（构造云客户端）、`import_sources` 之后（新方法）
- Modify: `backend/app/services/repository.py`（Protocol 增声明）
- Test: `backend/tests/test_url_sources.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_url_sources.py
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import remote_sources
from app.services.remote_sources import PdfProbe
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.sqlite_repository import SQLiteRepository


def _base_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")


@pytest.fixture
def cloud_repo(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok-test")
    return SQLiteRepository(Settings())


@pytest.fixture
def notoken_repo(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    return SQLiteRepository(Settings())


def test_add_url_sources_creates_and_rejects(cloud_repo, monkeypatch):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))

    def fake_probe(url, **kw):
        if url.endswith(".pdf"):
            return PdfProbe(True, "", 123, "doc.pdf")
        return PdfProbe(False, "URL 不是 PDF（Content-Type=text/html）", 0, "x.pdf")

    monkeypatch.setattr(remote_sources, "probe_pdf", fake_probe)
    scheduled = []
    result = cloud_repo.add_url_sources(
        nb.id, ["https://a/doc.pdf", "https://b/page.html"],
        scheduler=lambda sid: scheduled.append(sid),
    )
    assert len(result.created) == 1
    assert result.created[0].source_url == "https://a/doc.pdf"
    assert result.created[0].parse_status == "queued"
    assert result.created[0].type == "pdf"
    assert len(result.rejected) == 1
    assert "不是 PDF" in result.rejected[0].reason
    assert scheduled == [result.created[0].id]


def test_add_url_sources_requires_token(notoken_repo):
    nb = notoken_repo.create_notebook(NotebookCreate(name="n"))
    with pytest.raises(MinerUCloudNotConfigured):
        notoken_repo.add_url_sources(nb.id, ["https://a/doc.pdf"])


def test_add_url_sources_unknown_notebook_raises_keyerror(cloud_repo):
    with pytest.raises(KeyError):
        cloud_repo.add_url_sources("nb-missing", ["https://a/doc.pdf"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources.py -v`
Expected: FAIL（`AttributeError: 'SQLiteRepository' object has no attribute 'add_url_sources'`）

- [ ] **Step 3: 加 imports 与云客户端实例**

`sqlite_repository.py` 第 76–78 行的 imports 调整为：

```python
from app.services.mineru_client import MinerUClient
from app.services.mineru_cloud_client import MinerUCloudClient, MinerUCloudNotConfigured
from app.services import remote_sources
from app.services.parsers import parse_source_file, mineru_content_list_to_elements
```

并在第 67 行附近（与其他 schema import 一起）确保导入新模型：

```python
    AddUrlSourcesResult,
    RejectedUrl,
```

> 这两个名字加进 `from app.models.schemas import (...)` 的现有括号列表里（与 `SourceImportRequest` 同块）。

第 194 行 `self.mineru_client = MinerUClient(settings)` 之后插入：

```python
        self.mineru_cloud_client = MinerUCloudClient(settings)
```

- [ ] **Step 4: 实现 `add_url_sources`**

在 `import_sources` 方法（约第 1031 行结束）之后插入：

```python
    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: Optional[Callable[[str], None]] = None,
    ) -> AddUrlSourcesResult:
        """逐 URL 初筛(非 PDF/不可达/超限→rejected,不建来源);通过的建 source_url
        来源并交由现有 process_source(有 scheduler 则后台,否则同步)。未配置 token→报错。"""
        self.get_notebook(notebook_id)  # KeyError if missing
        if not self.mineru_cloud_client.configured:
            raise MinerUCloudNotConfigured("未配置 MinerU 云端凭证 (MINERU_API_TOKEN)")
        created: List[SourceSummary] = []
        rejected: List[RejectedUrl] = []
        for raw in urls:
            url = (raw or "").strip()
            if not url:
                continue
            probe = remote_sources.probe_pdf(url)
            if not probe.ok:
                rejected.append(RejectedUrl(url=url, reason=probe.reason))
                continue
            source_id = f"src-{uuid4().hex[:10]}"
            now = _now()
            with self._write() as db:
                db.execute(
                    """
                    INSERT INTO sources
                    (id, notebook_id, title, source_type, status, parse_status,
                     file_name, file_path, source_url, file_size, file_hash, summary,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id, notebook_id, probe.display_name, "pdf",
                        "queued", "queued", probe.display_name, "", url,
                        probe.content_length, "", "链接已添加，解析排队中。", now, now,
                    ),
                )
            if scheduler is not None:
                scheduler(source_id)
            else:
                self.process_source(source_id)
            created.append(self.get_source(source_id))
        return AddUrlSourcesResult(created=created, rejected=rejected)
```

- [ ] **Step 5: Protocol 声明**

在 `backend/app/services/repository.py` 的 `import_sources` 声明（第 70 行）之后插入：

```python
    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: Optional[Callable[[str], None]] = None,
    ) -> "AddUrlSourcesResult": ...
```

并把 `AddUrlSourcesResult` 加进该文件顶部 `from app.models.schemas import (...)` 列表（与 `SourceImportRequest` 同块）。`Iterable`/`Optional`/`Callable` 已在该文件顶部导入。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources.py -v`
Expected: PASS（3 passed）

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/app/services/repository.py backend/tests/test_url_sources.py
git commit -m "feat(sources): add_url_sources 初筛建 source_url 来源 + token 闸

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `process_source` URL 分支（云端解析→elements）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:1189`（parse 步）
- Test: `backend/tests/test_url_sources.py`（追加）

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_url_sources.py` 末尾追加：

```python
def _make_url_source(repo, monkeypatch, nb_id):
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(True, "", 10, "doc.pdf"),
    )
    res = repo.add_url_sources(nb_id, ["https://a/doc.pdf"], scheduler=lambda sid: None)
    return res.created[0].id


def test_process_source_url_branch_parses_via_cloud(cloud_repo, monkeypatch):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_url_source(cloud_repo, monkeypatch, nb.id)
    monkeypatch.setattr(
        cloud_repo.mineru_cloud_client, "parse_url",
        lambda url, **kw: [{"type": "text", "text": "Hello world", "page_idx": 0}],
    )
    cloud_repo.process_source(sid)
    detail = cloud_repo.get_source(sid)
    assert detail.parse_status == "extracted"
    assert any("Hello world" in e.text for e in cloud_repo.source_elements(sid))


def test_process_source_url_branch_failure_marks_failed(cloud_repo, monkeypatch):
    nb = cloud_repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_url_source(cloud_repo, monkeypatch, nb.id)

    def boom(url, **kw):
        raise RuntimeError("MinerU 云端解析失败: 超过页数")

    monkeypatch.setattr(cloud_repo.mineru_cloud_client, "parse_url", boom)
    cloud_repo.process_source(sid)
    detail = cloud_repo.get_source(sid)
    assert detail.parse_status == "failed"
    assert "超过页数" in detail.error_message
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources.py -k url_branch -v`
Expected: FAIL（URL 来源走了文件解析分支 → `parse_status` 非 extracted，或解析空）

- [ ] **Step 3: 加 URL 分支**

在 `process_source` 的 parse 步，将第 1189–1192 行：

```python
            elements = parse_source_file(
                source_id, source.file_path, source.file_name, self.mineru_client
            )
            mineru_error = str(getattr(self.mineru_client, "last_error", "") or "")
```

替换为：

```python
            if source.source_url:
                content_list = self.mineru_cloud_client.parse_url(
                    source.source_url, data_id=source_id
                )
                elements = mineru_content_list_to_elements(source_id, content_list)
                mineru_error = str(getattr(self.mineru_cloud_client, "last_error", "") or "")
            else:
                elements = parse_source_file(
                    source_id, source.file_path, source.file_name, self.mineru_client
                )
                mineru_error = str(getattr(self.mineru_client, "last_error", "") or "")
```

> `mineru_content_list_to_elements` 已在 Task 6 Step 3 一并 import。云端 `parse_url` 抛错会被 `process_source` 既有的 `except Exception` 捕获并落 `failed`（写入 err_msg）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_url_sources.py
git commit -m "feat(sources): process_source 对 URL 来源短路到云端解析

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: 端点 `POST /api/notebooks/{id}/sources/url`

**Files:**
- Modify: `backend/app/api/routes.py:12-58`（imports）、`import_sources` 之后（新端点）
- Test: `backend/tests/test_url_sources_api.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_url_sources_api.py
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import remote_sources
from app.services.remote_sources import PdfProbe
from app.services.sqlite_repository import SQLiteRepository


def _env(tmp_path, monkeypatch, token=None):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    if token:
        monkeypatch.setenv("MINERU_API_TOKEN", token)
    else:
        monkeypatch.delenv("MINERU_API_TOKEN", raising=False)


def _client(repo, monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.routes as routes_mod
    from app.main import app
    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    monkeypatch.setattr(routes_mod.kg_scheduler, "submit_job", lambda fn, *a, **k: None)
    return TestClient(app)


def test_endpoint_partial_created_and_rejected(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token="tok")
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(url.endswith(".pdf"),
                                   "" if url.endswith(".pdf") else "URL 不是 PDF（Content-Type=text/html）",
                                   1, "d.pdf"),
    )
    client = _client(repo, monkeypatch)
    resp = client.post(f"/api/notebooks/{nb.id}/sources/url",
                       json={"urls": ["https://a/d.pdf", "https://b/p.html"]})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["source_url"] == "https://a/d.pdf"
    assert len(body["rejected"]) == 1
    assert "不是 PDF" in body["rejected"][0]["reason"]


def test_endpoint_no_token_returns_400(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token=None)
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    client = _client(repo, monkeypatch)
    resp = client.post(f"/api/notebooks/{nb.id}/sources/url", json={"urls": ["https://a/d.pdf"]})
    assert resp.status_code == 400


def test_endpoint_unknown_notebook_returns_404(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, token="tok")
    repo = SQLiteRepository(Settings())
    client = _client(repo, monkeypatch)
    resp = client.post("/api/notebooks/nb-missing/sources/url", json={"urls": ["https://a/d.pdf"]})
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources_api.py -v`
Expected: FAIL（404，路由不存在 / 422）

- [ ] **Step 3: 加 imports**

`routes.py` 第 12–58 行的 `from app.models.schemas import (...)` 列表里加上：

```python
    AddUrlSourcesRequest,
    AddUrlSourcesResult,
```

在第 60 行 `from app.services.kg import scheduler as kg_scheduler` 附近加：

```python
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
```

- [ ] **Step 4: 加端点**

在 `import_sources` 端点（第 188–198 行）之后插入：

```python
@router.post("/notebooks/{notebook_id}/sources/url", response_model=AddUrlSourcesResult)
def add_url_sources(
    notebook_id: str,
    payload: AddUrlSourcesRequest,
) -> AddUrlSourcesResult:
    repo = repository()
    try:
        return repo.add_url_sources(
            notebook_id,
            payload.urls,
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
        )
    except MinerUCloudNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_url_sources_api.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_url_sources_api.py
git commit -m "feat(api): POST /notebooks/{id}/sources/url(返回 created/rejected)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: 前端 URL 解析 helper + 单测

**Files:**
- Create: `frontend/app/url-sources.ts`
- Test: `frontend/app/url-sources.test.mjs`

- [ ] **Step 1: 写失败测试**

```js
// frontend/app/url-sources.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import { parseUrlLines } from "./url-sources.ts";

test("parseUrlLines: 保留 http/https、trim、去重、丢空行与非 URL", () => {
  const input = "  https://a/x.pdf \n\nhttp://b/y.pdf\nftp://c/z.pdf\nnot a url\nhttps://a/x.pdf\n";
  assert.deepEqual(parseUrlLines(input), ["https://a/x.pdf", "http://b/y.pdf"]);
});

test("parseUrlLines: 纯空白 -> []", () => {
  assert.deepEqual(parseUrlLines("   \n  \n"), []);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && node --test app/url-sources.test.mjs`
Expected: FAIL（无法解析模块 `./url-sources.ts`）

- [ ] **Step 3: 实现 helper**

```ts
// frontend/app/url-sources.ts
// 把多行文本解析成去重后的 http/https 列表（前端只做轻校验；
// 是否真的是 PDF 由后端 probe_pdf 判定）。
export function parseUrlLines(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const url = raw.trim();
    if (!url) continue;
    if (!/^https?:\/\//i.test(url)) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    out.push(url);
  }
  return out;
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && node --test app/url-sources.test.mjs`
Expected: PASS（2 tests passed）

- [ ] **Step 5: 提交**

```bash
git add frontend/app/url-sources.ts frontend/app/url-sources.test.mjs
git commit -m "feat(fe): url-sources 解析 helper(http/https/去重)+ 单测

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: 前端接线（类型 + 「添加链接」按钮/弹窗/提交 + 卡片外链）

**Files:**
- Modify: `frontend/app/page.tsx`（类型 `:53`、import `:28` 附近、状态 `:791` 附近、函数 `confirmUpload` 之后、按钮 `:2268`、弹窗 `:2582` 附近、卡片 `:2285` 附近）

- [ ] **Step 1: 类型加字段**

在 `type SourceSummary = { ... }`（第 53–67 行）的 `file_size: number;` 之后加：

```ts
  source_url?: string;
```

- [ ] **Step 2: import helper**

在第 28 行 `import { setNotebookTier, ... } from "./notebook-tier";` 附近加：

```ts
import { parseUrlLines } from "./url-sources";
```

- [ ] **Step 3: 加状态**

在第 791 行 `const [sourceModalOpen, setSourceModalOpen] = useState(false);` 之后加：

```ts
  const [urlModalOpen, setUrlModalOpen] = useState(false);
  const [urlText, setUrlText] = useState("");
  const [urlBusy, setUrlBusy] = useState(false);
  const [urlRejected, setUrlRejected] = useState<Array<{ url: string; reason: string }>>([]);
```

- [ ] **Step 4: 加提交函数**

在 `confirmUpload`（第 1465–1480 行）之后插入：

```ts
  async function submitUrlSources() {
    if (!currentNotebookId) return;
    const urls = parseUrlLines(urlText);
    if (urls.length === 0) {
      setToast("请粘贴至少一个 http/https 链接");
      return;
    }
    setUrlBusy(true);
    setUrlRejected([]);
    try {
      const result = await api<{ created: SourceSummary[]; rejected: Array<{ url: string; reason: string }> }>(
        `/notebooks/${currentNotebookId}/sources/url`,
        { method: "POST", body: JSON.stringify({ urls }) }
      );
      if (result.created.length > 0) {
        setSources((previous) => [
          ...previous.filter((source) => !result.created.some((item) => item.id === source.id)),
          ...result.created,
        ]);
        await loadNotebookCollection();
      }
      setUrlRejected(result.rejected);
      setToast(`已添加 ${result.created.length} 个，被拒 ${result.rejected.length} 个`);
      if (result.rejected.length === 0) {
        setUrlText("");
        setUrlModalOpen(false);
      }
    } catch (error) {
      reportError(error);
    } finally {
      setUrlBusy(false);
    }
  }
```

- [ ] **Step 5: 加「添加链接」按钮**

将第 2266–2269 行的 `add-source-button` label 之后（紧邻其后）插入一个并列按钮：

```tsx
                <button type="button" className="add-source-button" onClick={() => { setUrlRejected([]); setUrlModalOpen(true); }}>
                  <ExternalLink size={20} strokeWidth={2.7} /> 添加链接
                </button>
```

> `ExternalLink` 已在第 4 行的 lucide-react import 中（无需新增）。

- [ ] **Step 6: 加 URL 弹窗**

在 `sourceModalOpen` 弹窗 `</section>`（第 2580 行 `)}` 之后）插入：

```tsx
      {urlModalOpen && (
        <section className="source-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setUrlModalOpen(false); }}>
          <div className="source-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>添加链接</h2>
                <p>每行一个公开可直链的 PDF；非 PDF 会被直接拒绝。由 mineru.net 云端解析。</p>
              </div>
              <button className="icon-button" onClick={() => setUrlModalOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              <textarea
                rows={6}
                value={urlText}
                placeholder={"https://arxiv.org/pdf/2401.00001\nhttps://example.com/paper.pdf"}
                onChange={(event) => setUrlText(event.target.value)}
              />
              {urlRejected.length > 0 && (
                <div className="stack" style={{ marginTop: 8 }}>
                  <span className="section-title">被拒链接</span>
                  {urlRejected.map((item, index) => (
                    <div className="checklist-row" key={`${item.url}-${index}`}>
                      <span style={{ flex: 1, wordBreak: "break-all" }}>{item.url}</span>
                      <small style={{ color: "var(--danger, #c0392b)" }}>{item.reason}</small>
                    </div>
                  ))}
                </div>
              )}
              <div className="tag-row">
                <button className="new-pill" disabled={urlBusy} onClick={() => submitUrlSources()}>
                  {urlBusy ? "添加中…" : "添加并解析"}
                </button>
                <button className="sort-button" onClick={() => setUrlModalOpen(false)}>取消</button>
              </div>
            </div>
          </div>
        </section>
      )}
```

- [ ] **Step 7: 卡片外链（可选小点缀）**

在来源卡片 `compact-source-row`（第 2285–2289 行）的 `title={source.title}` 处不便插入时，可在卡片标题旁条件渲染外链；最小实现：在 `source.source_url` 存在时于卡片内加一个图标链接。找到该卡片渲染标题的 `<span>`/`<strong>` 后插入：

```tsx
                        {source.source_url ? (
                          <a href={source.source_url} target="_blank" rel="noreferrer" title={source.source_url} onClick={(e) => e.stopPropagation()} style={{ marginLeft: 6 }}>
                            <ExternalLink size={13} />
                          </a>
                        ) : null}
```

> 若卡片结构不便插入，此步可跳过（非必需）；类型与提交流程已完整。务必保证 `tsc` 通过。

- [ ] **Step 8: 类型检查 + 前端测试**

Run: `cd frontend && npm run lint && npm run test`
Expected: `tsc --noEmit` 无报错；`node --test` 全绿（含新 `url-sources.test.mjs`）。

- [ ] **Step 9: 提交**

```bash
git add frontend/app/page.tsx
git commit -m "feat(fe): Source Stack 新增「添加链接」入口(粘贴 URL→云解析, 展示被拒原因)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: 全量验证与收尾

- [ ] **Step 1: 后端相关测试全绿**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_mineru_cloud_config.py tests/test_remote_sources.py tests/test_mineru_cloud_client.py tests/test_url_sources_schemas.py tests/test_source_url_persist.py tests/test_url_sources.py tests/test_url_sources_api.py -v`
Expected: 全部 PASS。

- [ ] **Step 2: 回归——确认未破坏既有用例**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_repository.py tests/test_parsers_markdown.py -q`
Expected: 仍全绿（摄取流水线/解析未回归）。

- [ ] **Step 3: 前端**

Run: `cd frontend && npm run lint && npm run test`
Expected: 全绿。

- [ ] **Step 4: 真机走查（preview，非自动）**

启动后端（用户侧；按记忆我不自动重启服务）+ 前端 dev，走查：「添加链接」粘贴一个公开 arXiv PDF 直链 + 一个 html 链接 → html 即时被拒并显示原因；PDF 进入 queued→…→extracted；失败链接落 failed 且可重解析。

> ⚠️ 后端需重启以加载新 `MINERU_*` 环境变量与新代码——仅提示用户，不自动重启。

- [ ] **Step 5: 收尾提 PR**

按仓库惯例（先与 master 三方合并并解冲突 → push → `gh pr create --base master`）。使用 superpowers:finishing-a-development-branch 完成。PR 描述涵盖：新「添加链接」入口、mineru.net 云端 URL 解析、初筛+失败态、`MINERU_*` 配置、测试清单。

---

## Self-Review（plan ↔ spec）

- **Spec §1 公开直链/URL 直传**：Task 3 `parse_url` 直接提交 URL（不下载）→ 覆盖。
- **Spec §1 初筛+失败态**：Task 2 `probe_pdf`（拒非 PDF）+ Task 6 rejected[] 不建来源 + Task 7 云端失败落 failed → 覆盖。
- **Spec §1 入口并列「添加链接」**：Task 10 Step 5 → 覆盖。
- **Spec §1 vlm / 返回 {created,rejected}**：Task 1（默认 vlm）、Task 4/6/8 结构体 → 覆盖。
- **Spec §2 云端契约（submit/poll/zip/state/err_msg）**：Task 3 完整实现 → 覆盖。
- **Spec §3 数据流（process_source 短路 + 全下游复用）**：Task 7 → 覆盖。
- **Spec §4.1/4.2 两个自包含新模块**：Task 2/3 → 覆盖。
- **Spec §4.6 DB 迁移 source_url**：Task 5 → 覆盖。
- **Spec §4.7 配置独立于 MINERU_MODE**：Task 1 → 覆盖。
- **Spec §5 无本地回退（token 缺失明确报错）**：Task 6 token 闸 + Task 8 400 → 覆盖。
- **Spec §6 测试均不打真网（注入/monkeypatch）**：所有后端测试经 `fetch`/`_http_*`/`probe_pdf` 接缝注入 → 覆盖。
- **类型/命名一致性**：`PdfProbe(ok,reason,content_length,display_name)`、`FetchResult(status,content_type,content_length,head)`、`MinerUCloudClient.parse_url`、`MinerUCloudNotConfigured`、`AddUrlSourcesRequest/RejectedUrl/AddUrlSourcesResult`、`add_url_sources(notebook_id,urls,scheduler)`、`source_url` 全程一致。
- **占位符扫描**：无 TBD/TODO；每个代码步均给出完整代码。
- **已知取舍**：境外 URL 可能超时 → 由轮询超时→failed 兜底（Spec §2 已记）；markdown 回退仅在 ZIP 缺 content_list.json 时触发（Task 3）。
