# LLM 日志可视化 debug 页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Next.js 前端加一个只读 debug 页面 `/dev/logs`,配合 FastAPI 只读接口 `/api/debug/logs/...`,让开发者看清"送了什么给 LLM"(system / schema_hint / user 内容 + 模型回复 + token/延迟/错误)。

**Architecture:** 后端新增纯函数模块 `log_reader.py`(读 jsonl、过滤、分页、统计)+ 薄 HTTP 层 `debug_logs.py`(门控、通道白名单、路径解析);前端在 `frontend/app/dev/logs/` 下用 master-detail 布局展示列表与详情,走现有 `API_BASE` 约定,不加新 npm 依赖。v1 只做 LLM 通道,events/requests 留扩展位。

**Tech Stack:** FastAPI + pydantic-settings(后端);Next.js 15 + React 19 + TypeScript + lucide-react + globals.css 设计变量(前端);pytest(后端测试)、node:test(前端纯函数测试)、preview 工作流(端到端验证)。

**约定(全程使用):**
- `PYBIN` = `/opt/homebrew/Caskroom/miniconda/base/bin/python`(项目约定的 Python,3.13 + pytest 9)。
- 后端测试从仓库根运行:`cd backend && $PYBIN -m pytest tests/<file> -v`(`tests/` 是包,`backend/` 自动入 `sys.path`)。
- 前端 lint:`cd frontend && npm run lint`(= `tsc --noEmit`)。
- 仓库根 = 本 worktree 根 `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/trusting-mestorf-f17ddc`。
- 真实日志在 **root master** `/Users/hzf/workspace/silicon_notebook/.local/logs/llm.jsonl`(本 worktree 的 `.local/logs` 为空,属正常;仅最终验证任务 T8 会把它拷进来跑)。

---

## File Structure

**后端(新增)**
- `backend/app/services/log_reader.py` — 纯函数:读 jsonl→带 seq 的记录、过滤、搜索、摘要、preview、统计、分页。无 FastAPI 依赖。
- `backend/app/api/debug_logs.py` — FastAPI router:门控依赖、通道白名单、路径解析,调用 `log_reader`。
- `backend/tests/test_log_reader.py` — `log_reader` 纯函数单测。
- `backend/tests/test_debug_logs.py` — HTTP 层测试(TestClient)。

**后端(改动)**
- `backend/app/core/config.py` — 新增 `debug_logs_enabled` 设置。
- `backend/app/main.py` — include 新 debug router。

**前端(新增,均在 `frontend/app/dev/logs/`)**
- `format.ts` — 纯函数 + `Summary` 类型。
- `format.test.mjs` — `format.ts` 单测(node:test)。
- `types.ts` — 与后端对齐的 TS 类型。
- `api.ts` — fetch 封装(走 `API_BASE`)。
- `logs.css` — 本页样式(复用 globals.css 的 CSS 变量)。
- `components/CopyButton.tsx`、`ChannelTabs.tsx`、`StatsBar.tsx`、`LogRow.tsx`、`LogList.tsx`、`ChatTranscript.tsx`、`LogDetail.tsx`。
- `page.tsx` — 客户端编排:状态、取数、轮询、布局拼装。

---

## Task 1: 后端 `log_reader` 纯函数模块

**Files:**
- Create: `backend/app/services/log_reader.py`
- Test: `backend/tests/test_log_reader.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_log_reader.py`:

```python
from app.services import log_reader


def _write(tmp_path, lines):
    p = tmp_path / "llm.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


CHAT_OK = (
    '{"ts":"2026-01-01T00:00:00","id":"llm-a","kind":"chat","model":"m1",'
    '"request":{"messages":[{"role":"system","content":"SYS"},'
    '{"role":"user","content":"hello world"}],"schema_hint":"H"},'
    '"status":"ok","latency_ms":100,'
    '"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12},'
    '"response":{"content":"{\\"k\\":1}"}}'
)
EMBED_OK = (
    '{"ts":"2026-01-01T00:00:01","id":"llm-b","kind":"embed","model":"e1",'
    '"status":"ok","latency_ms":20,"usage":{"total_tokens":3},'
    '"input_chars":50,"dims":1024}'
)
CHAT_ERR = (
    '{"ts":"2026-01-01T00:00:02","id":"llm-c","kind":"chat","model":"m1",'
    '"request":{"messages":[{"role":"user","content":"boom"}]},'
    '"status":"error","latency_ms":5,"error":"RuntimeError: nope"}'
)


def test_load_assigns_seq_and_skips_malformed(tmp_path):
    p = _write(tmp_path, [CHAT_OK, "NOT JSON", EMBED_OK, ""])
    records, malformed = log_reader.load_records(p)
    assert [r["id"] for r in records] == ["llm-a", "llm-b"]
    assert [r["seq"] for r in records] == [0, 2]  # line index preserved (append-only)
    assert malformed == 1


def test_load_missing_file(tmp_path):
    records, malformed = log_reader.load_records(tmp_path / "nope.jsonl")
    assert records == [] and malformed == 0


def test_filter_by_kind_status_model(tmp_path):
    records, _ = log_reader.load_records(_write(tmp_path, [CHAT_OK, EMBED_OK, CHAT_ERR]))
    assert {r["id"] for r in log_reader.filter_records(records, kind="chat")} == {"llm-a", "llm-c"}
    assert {r["id"] for r in log_reader.filter_records(records, status="error")} == {"llm-c"}
    assert {r["id"] for r in log_reader.filter_records(records, model="e1")} == {"llm-b"}


def test_search_matches_messages_and_error(tmp_path):
    records, _ = log_reader.load_records(_write(tmp_path, [CHAT_OK, CHAT_ERR]))
    assert {r["id"] for r in log_reader.filter_records(records, q="HELLO")} == {"llm-a"}
    assert {r["id"] for r in log_reader.filter_records(records, q="nope")} == {"llm-c"}


def test_summary_and_preview(tmp_path):
    records, _ = log_reader.load_records(_write(tmp_path, [CHAT_OK, EMBED_OK, CHAT_ERR]))
    by_id = {r["id"]: log_reader.to_summary(r) for r in records}
    assert by_id["llm-a"]["total_tokens"] == 12
    assert by_id["llm-a"]["preview"] == "hello world"
    assert "input_chars=50" in by_id["llm-b"]["preview"]
    assert by_id["llm-c"]["preview"] == "RuntimeError: nope"
    assert by_id["llm-c"]["error"] == "RuntimeError: nope"


def test_stats_counts_filtered_facets_full(tmp_path):
    records, malformed = log_reader.load_records(_write(tmp_path, [CHAT_OK, EMBED_OK, CHAT_ERR]))
    filtered = log_reader.filter_records(records, kind="chat")
    stats = log_reader.compute_stats(records, filtered, malformed)
    assert stats["total"] == 3 and stats["filtered"] == 2
    assert stats["by_kind"] == {"chat": 2}              # over filtered set
    assert stats["by_status"] == {"ok": 1, "error": 1}  # over filtered set
    assert stats["total_tokens"] == 12
    assert stats["latency_ms"]["max"] == 100
    assert stats["facets"]["kinds"] == ["chat", "embed"]  # over FULL set, sorted
    assert set(stats["facets"]["models"]) == {"m1", "e1"}


def test_paginate_before_since_limit(tmp_path):
    lines = ['{"id":"llm-%d","kind":"chat","model":"m","status":"ok","latency_ms":1}' % i for i in range(5)]
    records, _ = log_reader.load_records(_write(tmp_path, lines))
    desc = sorted(records, key=lambda r: r["seq"], reverse=True)  # seq 4..0
    page, has_more = log_reader.paginate(desc, before=None, since=None, limit=2)
    assert [r["seq"] for r in page] == [4, 3] and has_more is True
    older, _ = log_reader.paginate(desc, before=3, since=None, limit=10)
    assert [r["seq"] for r in older] == [2, 1, 0]
    newer, _ = log_reader.paginate(desc, before=None, since=2, limit=10)
    assert [r["seq"] for r in newer] == [4, 3]
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_log_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.log_reader'`(或 AttributeError)。

- [ ] **Step 3: 实现 `log_reader.py`**

Create `backend/app/services/log_reader.py`:

```python
"""Read-only helpers for the debug log viewer.

Pure functions over a JSONL log channel: load lines into dicts (each tagged
with a stable `seq` = line index, since logs are append-only), filter, search,
summarize, compute stats, and paginate. No FastAPI / IO side effects beyond
reading the given file. Best-effort: malformed lines are skipped and counted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Channel name -> filename. The HTTP layer validates against this allowlist.
CHANNELS: Dict[str, str] = {
    "llm": "llm.jsonl",
    "events": "events.jsonl",
    "requests": "requests.jsonl",
}


def load_records(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Return (records, malformed_count). Each record gets `seq` = 0-based line
    index. Blank lines are ignored (not malformed). Missing file -> ([], 0)."""
    records: List[Dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return records, malformed
    with path.open("r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                malformed += 1
                continue
            if isinstance(obj, dict):
                obj["seq"] = i
                records.append(obj)
            else:
                malformed += 1
    return records, malformed


def _text_blob(rec: Dict[str, Any]) -> str:
    parts: List[str] = []
    req = rec.get("request") or {}
    for m in req.get("messages") or []:
        parts.append(str(m.get("content", "")))
    parts.append(str(req.get("schema_hint", "")))
    resp = rec.get("response") or {}
    parts.append(str(resp.get("content", "")))
    parts.append(str(rec.get("error", "")))
    return "\n".join(parts)


def filter_records(
    records: List[Dict[str, Any]],
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    model: Optional[str] = None,
    q: Optional[str] = None,
) -> List[Dict[str, Any]]:
    out = records
    if kind:
        out = [r for r in out if r.get("kind") == kind]
    if status:
        out = [r for r in out if r.get("status") == status]
    if model:
        out = [r for r in out if r.get("model") == model]
    if q:
        needle = q.lower()
        out = [r for r in out if needle in _text_blob(r).lower()]
    return out


def _preview(rec: Dict[str, Any]) -> str:
    if rec.get("status") in ("error", "retry") and rec.get("error"):
        return str(rec["error"])[:200]
    kind = rec.get("kind")
    if kind == "chat":
        msgs = (rec.get("request") or {}).get("messages") or []
        for m in reversed(msgs):
            if m.get("role") == "user" and m.get("content"):
                return str(m["content"])[:200]
        if msgs:
            return str(msgs[-1].get("content", ""))[:200]
        return ""
    if kind == "embed":
        bits = []
        if rec.get("input_chars") is not None:
            bits.append(f"input_chars={rec['input_chars']}")
        if rec.get("dims") is not None:
            bits.append(f"dims={rec['dims']}")
        return " ".join(bits)
    return ""


def to_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    usage = rec.get("usage") or {}
    err = rec.get("error")
    return {
        "seq": rec.get("seq"),
        "id": rec.get("id"),
        "ts": rec.get("ts"),
        "kind": rec.get("kind"),
        "model": rec.get("model"),
        "status": rec.get("status"),
        "latency_ms": rec.get("latency_ms"),
        "total_tokens": usage.get("total_tokens"),
        "attempt": rec.get("attempt"),
        "error": (str(err)[:200] if err else None),
        "preview": _preview(rec),
    }


def _counts(records: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    c: Dict[str, int] = {}
    for r in records:
        v = r.get(key)
        if v is None:
            continue
        c[str(v)] = c.get(str(v), 0) + 1
    return c


def compute_stats(
    all_records: List[Dict[str, Any]],
    filtered: List[Dict[str, Any]],
    malformed: int,
) -> Dict[str, Any]:
    """count-类统计基于 filtered;facets(下拉可选项)基于 all_records(全量)。"""
    latencies = [r["latency_ms"] for r in filtered if isinstance(r.get("latency_ms"), (int, float))]
    total_tokens = sum((r.get("usage") or {}).get("total_tokens", 0) or 0 for r in filtered)
    return {
        "total": len(all_records),
        "filtered": len(filtered),
        "by_kind": _counts(filtered, "kind"),
        "by_status": _counts(filtered, "status"),
        "by_model": _counts(filtered, "model"),
        "total_tokens": total_tokens,
        "latency_ms": {
            "avg": round(sum(latencies) / len(latencies)) if latencies else 0,
            "max": max(latencies) if latencies else 0,
        },
        "malformed_lines": malformed,
        "facets": {
            "kinds": sorted(_counts(all_records, "kind").keys()),
            "statuses": sorted(_counts(all_records, "status").keys()),
            "models": sorted(_counts(all_records, "model").keys()),
        },
    }


def paginate(
    records_desc: List[Dict[str, Any]],
    *,
    before: Optional[int],
    since: Optional[int],
    limit: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    """`records_desc` 已按 seq 降序。before→更旧(seq<before);since→更新(seq>since)。
    返回 (page, has_more);has_more 表示截断前还有更多旧记录。"""
    out = records_desc
    if since is not None:
        out = [r for r in out if (r.get("seq") or -1) > since]
    if before is not None:
        out = [r for r in out if (r.get("seq") or -1) < before]
    has_more = len(out) > limit
    return out[:limit], has_more
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_log_reader.py -v`
Expected: PASS(7 passed)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/log_reader.py backend/tests/test_log_reader.py
git commit -m "feat(debug): log_reader 纯函数(读 jsonl/过滤/搜索/统计/分页)"
```

---

## Task 2: 后端设置项 `debug_logs_enabled`

**Files:**
- Modify: `backend/app/core/config.py`(在 `event_log_*` 设置附近,约 73-76 行后)

- [ ] **Step 1: 加设置字段**

在 `backend/app/core/config.py` 中,`slow_request_ms` 那一行(约 76 行)之后、`mineru_mode` 注释之前,插入:

```python
    # Read-only debug log viewer endpoints (/api/debug/logs/...). Local dev tool;
    # set DEBUG_LOGS_ENABLED=false to hide them (every debug endpoint then 404s).
    debug_logs_enabled: bool = Field(True, env="DEBUG_LOGS_ENABLED")
```

- [ ] **Step 2: 快速验证设置可读**

Run:
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -c "from app.core.config import Settings; s=Settings(); print('default:', s.debug_logs_enabled)"
```
Expected: `default: True`

- [ ] **Step 3: 提交**

```bash
git add backend/app/core/config.py
git commit -m "feat(debug): 新增 debug_logs_enabled 设置(默认开)"
```

---

## Task 3: 后端 `debug_logs` router + 接入 main

**Files:**
- Create: `backend/app/api/debug_logs.py`
- Modify: `backend/app/main.py`(import + include_router)
- Test: `backend/tests/test_debug_logs.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_debug_logs.py`:

```python
import json

from fastapi.testclient import TestClient


def _make_client(tmp_path, monkeypatch, *, enabled=True, lines=None, channel="llm"):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    if lines is not None:
        (logs / f"{channel}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("EVENT_LOG_DIR", str(logs))  # absolute -> used as-is
    monkeypatch.setenv("DEBUG_LOGS_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


CHAT = json.dumps({
    "ts": "2026-01-01T00:00:00", "id": "llm-a", "kind": "chat", "model": "m1",
    "request": {"messages": [{"role": "system", "content": "SYS"},
                             {"role": "user", "content": "hello world"}], "schema_hint": "H"},
    "status": "ok", "latency_ms": 100, "usage": {"total_tokens": 12},
    "response": {"content": "{}"},
})
EMB = json.dumps({
    "ts": "2026-01-01T00:00:01", "id": "llm-b", "kind": "embed", "model": "e1",
    "status": "ok", "latency_ms": 20, "usage": {"total_tokens": 3},
    "input_chars": 50, "dims": 1024,
})
ERR = json.dumps({
    "ts": "2026-01-01T00:00:02", "id": "llm-c", "kind": "chat", "model": "m1",
    "request": {"messages": [{"role": "user", "content": "boom"}]},
    "status": "error", "latency_ms": 5, "error": "RuntimeError: nope",
})


def test_disabled_returns_404(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, enabled=False, lines=[CHAT])
    assert c.get("/api/debug/logs").status_code == 404
    assert c.get("/api/debug/logs/llm").status_code == 404


def test_unknown_channel_404(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT])
    assert c.get("/api/debug/logs/secrets").status_code == 404


def test_list_channels(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB])
    chans = {ch["name"]: ch for ch in c.get("/api/debug/logs").json()["channels"]}
    assert chans["llm"]["exists"] is True and chans["llm"]["count"] == 2
    assert chans["events"]["exists"] is False


def test_list_records_and_stats(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    body = c.get("/api/debug/logs/llm").json()
    assert body["file_exists"] is True
    assert [r["id"] for r in body["records"]] == ["llm-c", "llm-b", "llm-a"]  # newest seq first
    assert body["stats"]["total"] == 3
    assert body["newest_seq"] == 2
    assert sorted(body["stats"]["facets"]["kinds"]) == ["chat", "embed"]


def test_filters_and_search(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    assert {r["id"] for r in c.get("/api/debug/logs/llm?kind=embed").json()["records"]} == {"llm-b"}
    assert {r["id"] for r in c.get("/api/debug/logs/llm?status=error").json()["records"]} == {"llm-c"}
    assert {r["id"] for r in c.get("/api/debug/logs/llm?q=HELLO").json()["records"]} == {"llm-a"}


def test_pagination(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    first = c.get("/api/debug/logs/llm?limit=2").json()
    assert [r["seq"] for r in first["records"]] == [2, 1] and first["has_more"] is True
    older = c.get("/api/debug/logs/llm?before=1").json()
    assert [r["seq"] for r in older["records"]] == [0]
    newer = c.get("/api/debug/logs/llm?since=1").json()
    assert [r["seq"] for r in newer["records"]] == [2]


def test_detail_by_id_and_404(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB])
    rec = c.get("/api/debug/logs/llm/llm-a").json()
    assert rec["request"]["messages"][0]["content"] == "SYS"
    assert c.get("/api/debug/logs/llm/llm-zzz").status_code == 404


def test_missing_file_empty(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    body = c.get("/api/debug/logs/llm").json()
    assert body["file_exists"] is False and body["records"] == []
    assert body["stats"]["total"] == 0


def test_malformed_line_skipped(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, "NOT JSON", EMB])
    body = c.get("/api/debug/logs/llm").json()
    assert body["stats"]["malformed_lines"] == 1
    assert len(body["records"]) == 2
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_debug_logs.py -v`
Expected: FAIL — debug 路由不存在(404 出现在不该出现的地方,或导入 create_app 后无 `/api/debug/logs` 路由 → 404 全线,断言失败)。

- [ ] **Step 3: 实现 router**

Create `backend/app/api/debug_logs.py`:

```python
"""Read-only debug endpoints for viewing the JSONL log channels.

Gated by `debug_logs_enabled` (every endpoint 404s when off). Channels are
validated against `log_reader.CHANNELS` (no path traversal). Never writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import Settings, get_settings
from app.core.event_logging import _ROOT_DIR
from app.services import log_reader

router = APIRouter(prefix="/debug/logs", tags=["debug"])


def require_enabled(settings: Settings = Depends(get_settings)) -> Settings:
    if not getattr(settings, "debug_logs_enabled", False):
        raise HTTPException(status_code=404, detail="debug logs disabled")
    return settings


def _channel_path(settings: Settings, channel: str) -> Path:
    filename = log_reader.CHANNELS.get(channel)
    if filename is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = _ROOT_DIR / log_dir
    return log_dir / filename


@router.get("")
def list_channels(settings: Settings = Depends(require_enabled)):
    out = []
    for name, filename in log_reader.CHANNELS.items():
        path = _channel_path(settings, name)
        exists = path.exists()
        count = 0
        if exists:
            with path.open("r", encoding="utf-8") as fh:
                count = sum(1 for line in fh if line.strip())
        out.append({"name": name, "file": filename, "exists": exists, "count": count})
    return {"channels": out}


@router.get("/{channel}")
def list_records(
    channel: str,
    settings: Settings = Depends(require_enabled),
    limit: int = Query(200, ge=1, le=2000),
    before: Optional[int] = None,
    since: Optional[int] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    model: Optional[str] = None,
    q: Optional[str] = None,
):
    path = _channel_path(settings, channel)
    file_exists = path.exists()
    records, malformed = log_reader.load_records(path)
    filtered = log_reader.filter_records(records, kind=kind, status=status, model=model, q=q)
    stats = log_reader.compute_stats(records, filtered, malformed)
    filtered_desc = sorted(filtered, key=lambda r: r.get("seq") or -1, reverse=True)
    page, has_more = log_reader.paginate(filtered_desc, before=before, since=since, limit=limit)
    newest_seq = filtered_desc[0]["seq"] if filtered_desc else None
    return {
        "channel": channel,
        "file_exists": file_exists,
        "records": [log_reader.to_summary(r) for r in page],
        "stats": stats,
        "has_more": has_more,
        "newest_seq": newest_seq,
    }


@router.get("/{channel}/{record_id}")
def get_record(
    channel: str,
    record_id: str,
    settings: Settings = Depends(require_enabled),
):
    path = _channel_path(settings, channel)
    records, _ = log_reader.load_records(path)
    matches = [r for r in records if r.get("id") == record_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"record not found: {record_id}")
    return matches[-1]
```

- [ ] **Step 4: 接入 main.py**

在 `backend/app/main.py` 顶部 import 区(已有 `from app.api.routes import router` 那一行下面)加:

```python
from app.api.debug_logs import router as debug_logs_router
```

在 `create_app()` 里 `app.include_router(router, prefix="/api")` 那一行(约 79 行)之后、`return app` 之前加:

```python
    app.include_router(debug_logs_router, prefix="/api")
```

- [ ] **Step 5: 运行测试,确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_debug_logs.py -v`
Expected: PASS(9 passed)。

- [ ] **Step 6: 跑后端冒烟,确认没破坏现有流程**

Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python ../scripts/smoke_backend.py`
Expected: 正常结束(脚本导入 `app.main` 会连带导入新 router;无 traceback)。

- [ ] **Step 7: 提交**

```bash
git add backend/app/api/debug_logs.py backend/app/main.py backend/tests/test_debug_logs.py
git commit -m "feat(debug): /api/debug/logs 只读接口(通道白名单+门控+分页+详情)"
```

---

## Task 4: 前端 `format.ts` 纯函数 + 测试

**Files:**
- Create: `frontend/app/dev/logs/format.ts`
- Test: `frontend/app/dev/logs/format.test.mjs`

- [ ] **Step 1: 写失败测试**

Create `frontend/app/dev/logs/format.test.mjs`:

```js
import test from "node:test";
import assert from "node:assert/strict";

import { statusClass, formatLatency, formatTokens, prettyJson, shortId } from "./format.ts";

test("statusClass maps known statuses", () => {
  assert.equal(statusClass("ok"), "ok");
  assert.equal(statusClass("retry"), "retry");
  assert.equal(statusClass("error"), "error");
  assert.equal(statusClass("weird"), "muted");
});

test("formatLatency", () => {
  assert.equal(formatLatency(null), "—");
  assert.equal(formatLatency(250), "250ms");
  assert.equal(formatLatency(1500), "1.5s");
});

test("formatTokens", () => {
  assert.equal(formatTokens(null), "—");
  assert.equal(formatTokens(42), "42");
  assert.equal(formatTokens(3896), "3.9k");
});

test("prettyJson pretty-prints valid and passes through invalid", () => {
  const a = prettyJson('{"k":1}');
  assert.equal(a.ok, true);
  assert.match(a.pretty, /\n {2}"k": 1/);
  const b = prettyJson("not json");
  assert.equal(b.ok, false);
  assert.equal(b.pretty, "not json");
});

test("shortId strips llm- prefix", () => {
  assert.equal(shortId("llm-abc123"), "abc123");
  assert.equal(shortId(null), "—");
});
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `cd frontend && node --experimental-strip-types --test app/dev/logs/format.test.mjs`
Expected: FAIL — `Cannot find module './format.ts'`。
（备注:本仓库已有 `app/answer-formatting.test.mjs` 用同样方式 `import "./x.ts"`。若你的 Node ≥ 22.18,类型剥离已默认开启,`--experimental-strip-types` 为兼容写法;若该 flag 报 "unknown option",去掉它再跑。)

- [ ] **Step 3: 实现 `format.ts`**

Create `frontend/app/dev/logs/format.ts`:

```ts
// Pure presentation helpers + the list-row Summary shape. Imported by both the
// React components (compiled by Next) and format.test.mjs (run by Node).

export type Summary = {
  seq: number;
  id: string;
  ts: string;
  kind: string;
  model: string;
  status: string;
  latency_ms: number | null;
  total_tokens: number | null;
  attempt: number | null;
  error: string | null;
  preview: string;
};

export function statusClass(status: string): string {
  if (status === "ok") return "ok";
  if (status === "retry") return "retry";
  if (status === "error") return "error";
  return "muted";
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

export function prettyJson(text: string): { pretty: string; ok: boolean } {
  try {
    return { pretty: JSON.stringify(JSON.parse(text), null, 2), ok: true };
  } catch {
    return { pretty: text, ok: false };
  }
}

export function shortId(id: string | null | undefined): string {
  return id ? id.replace(/^llm-/, "") : "—";
}
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `cd frontend && node --experimental-strip-types --test app/dev/logs/format.test.mjs`
Expected: PASS(5 tests passed)。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/dev/logs/format.ts frontend/app/dev/logs/format.test.mjs
git commit -m "feat(debug-ui): format.ts 纯函数(状态配色/延迟/token/JSON 美化)"
```

---

## Task 5: 前端 `types.ts` + `api.ts`

**Files:**
- Create: `frontend/app/dev/logs/types.ts`
- Create: `frontend/app/dev/logs/api.ts`

- [ ] **Step 1: 实现 `types.ts`**

Create `frontend/app/dev/logs/types.ts`:

```ts
export type { Summary } from "./format";

export type Facets = { kinds: string[]; statuses: string[]; models: string[] };

export type Stats = {
  total: number;
  filtered: number;
  by_kind: Record<string, number>;
  by_status: Record<string, number>;
  by_model: Record<string, number>;
  total_tokens: number;
  latency_ms: { avg: number; max: number };
  malformed_lines: number;
  facets: Facets;
};

export type ChannelInfo = { name: string; file: string; exists: boolean; count: number };
export type ChannelsResponse = { channels: ChannelInfo[] };

import type { Summary } from "./format";
export type ListResponse = {
  channel: string;
  file_exists: boolean;
  records: Summary[];
  stats: Stats;
  has_more: boolean;
  newest_seq: number | null;
};

export type Message = { role: string; content: string };
export type FullRecord = {
  seq: number;
  id: string;
  ts: string;
  kind: string;
  model: string;
  status: string;
  latency_ms?: number;
  usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
  request?: { messages?: Message[]; schema_hint?: string };
  response?: { content?: string };
  input_chars?: number;
  dims?: number;
  error?: string;
  attempt?: number;
  [k: string]: unknown;
};
```

- [ ] **Step 2: 实现 `api.ts`**

Create `frontend/app/dev/logs/api.ts`:

```ts
import type { ChannelsResponse, FullRecord, ListResponse } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.clone().json();
      detail = (body && (body.detail || body.message)) || "";
    } catch {
      detail = (await res.text().catch(() => "")) || "";
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? ` - ${detail}` : ""}`);
  }
  return res.json();
}

export function fetchChannels(): Promise<ChannelsResponse> {
  return get<ChannelsResponse>(`/debug/logs`);
}

export type RecordQuery = {
  limit?: number;
  before?: number;
  since?: number;
  kind?: string;
  status?: string;
  model?: string;
  q?: string;
};

export function fetchRecords(channel: string, params: RecordQuery): Promise<ListResponse> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return get<ListResponse>(`/debug/logs/${channel}${suffix}`);
}

export function fetchRecord(channel: string, id: string): Promise<FullRecord> {
  return get<FullRecord>(`/debug/logs/${channel}/${encodeURIComponent(id)}`);
}
```

- [ ] **Step 3: 类型检查通过(无运行时测试,tsc 即门禁)**

Run: `cd frontend && npm run lint`
Expected: 无错误(`tsc --noEmit` 通过)。

- [ ] **Step 4: 提交**

```bash
git add frontend/app/dev/logs/types.ts frontend/app/dev/logs/api.ts
git commit -m "feat(debug-ui): types.ts 与 api.ts(对齐后端 + fetch 封装)"
```

---

## Task 6: 前端组件 + 样式

**Files:**
- Create: `frontend/app/dev/logs/logs.css`
- Create: `frontend/app/dev/logs/components/CopyButton.tsx`
- Create: `frontend/app/dev/logs/components/ChannelTabs.tsx`
- Create: `frontend/app/dev/logs/components/StatsBar.tsx`
- Create: `frontend/app/dev/logs/components/LogRow.tsx`
- Create: `frontend/app/dev/logs/components/LogList.tsx`
- Create: `frontend/app/dev/logs/components/ChatTranscript.tsx`
- Create: `frontend/app/dev/logs/components/LogDetail.tsx`

- [ ] **Step 1: 写样式 `logs.css`**

Create `frontend/app/dev/logs/logs.css`:

```css
.logview { display: grid; grid-template-rows: auto auto auto 1fr; height: 100vh; background: var(--bg); color: var(--ink); }
.logview-top { display: flex; align-items: center; gap: 16px; padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--panel); flex-wrap: wrap; }
.logview-tabs { display: flex; gap: 6px; }
.logview-tab { border: 1px solid var(--line); background: var(--soft); color: var(--ink); border-radius: 8px; padding: 5px 12px; font-size: 13px; }
.logview-tab.active { background: var(--blue); color: #fff; border-color: var(--blue); }
.logview-tab.disabled { opacity: 0.45; cursor: not-allowed; }
.tab-count { opacity: 0.7; font-size: 11px; margin-left: 4px; }

.logview-stats { display: flex; gap: 8px; flex-wrap: wrap; }
.stat-chip { display: inline-flex; gap: 5px; align-items: baseline; background: var(--soft); border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; font-size: 12px; }
.stat-chip .k { color: var(--muted); }
.stat-chip .v { font-weight: 600; }

.logview-filters { display: flex; gap: 8px; align-items: center; padding: 8px 16px; border-bottom: 1px solid var(--line); background: var(--panel); flex-wrap: wrap; }
.logview-filters select, .logview-search { border: 1px solid var(--line); border-radius: 7px; padding: 5px 9px; font-size: 13px; background: #fff; color: var(--ink); }
.logview-search { flex: 1; min-width: 200px; }
.auto-toggle { display: inline-flex; gap: 5px; align-items: center; font-size: 13px; color: var(--muted); }

.errorbar { margin: 8px 16px; padding: 8px 12px; border-radius: 8px; background: #fdeaea; color: var(--red); border: 1px solid #f3c9c9; font-size: 13px; }

.logview-body { display: grid; grid-template-columns: minmax(340px, 420px) 1fr; overflow: hidden; }
.logview-list { overflow-y: auto; border-right: 1px solid var(--line); background: var(--panel); }
.logview-detail { overflow-y: auto; padding: 16px; }

.logrow { display: block; width: 100%; text-align: left; border: 0; border-bottom: 1px solid var(--line); background: transparent; padding: 10px 14px; }
.logrow:hover { background: var(--soft); }
.logrow.selected { background: #eaf1ff; box-shadow: inset 3px 0 0 var(--blue); }
.logrow-head { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.logrow-model { color: var(--muted); }
.logrow-spacer { flex: 1; }
.logrow-num { color: var(--muted); font-variant-numeric: tabular-nums; }
.logrow-preview { margin-top: 5px; font-size: 12.5px; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.logrow-ts { margin-top: 3px; font-size: 11px; color: var(--muted); }

.badge { border-radius: 6px; padding: 1px 7px; font-size: 11px; font-weight: 600; border: 1px solid transparent; }
.badge.ok { background: #e7f4ee; color: var(--green); }
.badge.retry { background: #fbf0dd; color: var(--amber); }
.badge.error { background: #fdeaea; color: var(--red); }
.badge.muted { background: var(--soft); color: var(--muted); }
.badge.kind { background: var(--soft); color: var(--ink); }

.newbar, .loadmore { display: block; width: 100%; border: 0; background: #eaf1ff; color: var(--blue); padding: 8px; font-size: 13px; font-weight: 600; }
.loadmore { background: var(--soft); color: var(--ink); border-top: 1px solid var(--line); }
.empty { padding: 24px; text-align: center; color: var(--muted); font-size: 13px; }

.detail-meta { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding-bottom: 12px; border-bottom: 1px solid var(--line); font-size: 12.5px; color: var(--muted); }
.detail-meta-item { color: var(--ink); }
.detail-error { margin: 12px 0; padding: 10px 12px; border-radius: 8px; background: #fdeaea; color: var(--red); border: 1px solid #f3c9c9; font-size: 13px; word-break: break-word; }

.transcript-section-title { margin: 14px 0 8px; font-size: 12px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.msg { border: 1px solid var(--line); border-radius: 10px; margin-bottom: 10px; overflow: hidden; background: var(--panel); }
.msg-role { display: flex; align-items: center; gap: 8px; padding: 6px 10px; font-size: 12px; font-weight: 700; background: var(--soft); border-bottom: 1px solid var(--line); }
.msg.system .msg-role { background: #eef4ff; color: var(--blue); }
.msg.schema .msg-role { background: #f1ecff; color: #5b3fb5; }
.msg.assistant .msg-role, .detail-response .msg-role { background: #e7f4ee; color: var(--green); }
.msg-body { margin: 0; padding: 10px 12px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; max-height: 420px; overflow: auto; }
.detail-response { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; margin-top: 6px; }

.copy-btn { display: inline-flex; align-items: center; gap: 4px; border: 1px solid var(--line); background: #fff; color: var(--ink); border-radius: 6px; padding: 2px 8px; font-size: 11px; margin-left: auto; }
.copy-btn:hover { background: var(--soft); }
```

- [ ] **Step 2: `CopyButton.tsx`**

Create `frontend/app/dev/logs/components/CopyButton.tsx`:

```tsx
"use client";
import { useState } from "react";
import { Check, Copy } from "lucide-react";

export function CopyButton({ text, label = "复制" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="copy-btn"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch {
          /* clipboard blocked; ignore */
        }
      }}
    >
      {done ? <Check size={13} /> : <Copy size={13} />} {done ? "已复制" : label}
    </button>
  );
}
```

- [ ] **Step 3: `ChannelTabs.tsx`**

Create `frontend/app/dev/logs/components/ChannelTabs.tsx`:

```tsx
"use client";
import type { ChannelInfo } from "../types";

const ORDER = ["llm", "events", "requests"];

export function ChannelTabs({
  channels,
  active,
  onSelect,
}: {
  channels: ChannelInfo[];
  active: string;
  onSelect: (name: string) => void;
}) {
  const sorted = [...channels].sort((a, b) => ORDER.indexOf(a.name) - ORDER.indexOf(b.name));
  return (
    <div className="logview-tabs">
      {sorted.map((ch) => {
        const disabled = ch.name !== "llm"; // v1: only LLM is interactive
        return (
          <button
            key={ch.name}
            className={`logview-tab${ch.name === active ? " active" : ""}${disabled ? " disabled" : ""}`}
            disabled={disabled}
            title={disabled ? "v1 仅支持 LLM 通道" : ""}
            onClick={() => !disabled && onSelect(ch.name)}
          >
            {ch.name.toUpperCase()} <span className="tab-count">{ch.count}</span>
          </button>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 4: `StatsBar.tsx`**

Create `frontend/app/dev/logs/components/StatsBar.tsx`:

```tsx
"use client";
import type { Stats } from "../types";
import { formatTokens } from "../format";

export function StatsBar({ stats }: { stats: Stats | null }) {
  if (!stats) return <div className="logview-stats" />;
  const chip = (k: string, v: string | number) => (
    <span className="stat-chip" key={k}>
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </span>
  );
  return (
    <div className="logview-stats">
      {chip("总数", stats.total)}
      {chip("命中", stats.filtered)}
      {chip("ok", stats.by_status.ok ?? 0)}
      {chip("retry", stats.by_status.retry ?? 0)}
      {chip("error", stats.by_status.error ?? 0)}
      {chip("tokens", formatTokens(stats.total_tokens))}
      {chip("延迟avg", `${stats.latency_ms.avg}ms`)}
      {chip("延迟max", `${stats.latency_ms.max}ms`)}
      {stats.malformed_lines > 0 ? chip("坏行", stats.malformed_lines) : null}
    </div>
  );
}
```

- [ ] **Step 5: `LogRow.tsx`**

Create `frontend/app/dev/logs/components/LogRow.tsx`:

```tsx
"use client";
import type { Summary } from "../types";
import { formatLatency, formatTokens, statusClass } from "../format";

export function LogRow({
  rec,
  selected,
  onSelect,
}: {
  rec: Summary;
  selected: boolean;
  onSelect: (rec: Summary) => void;
}) {
  return (
    <button className={`logrow${selected ? " selected" : ""}`} onClick={() => onSelect(rec)}>
      <div className="logrow-head">
        <span className={`badge ${statusClass(rec.status)}`}>{rec.status}</span>
        <span className="badge kind">{rec.kind}</span>
        <span className="logrow-model">{rec.model}</span>
        <span className="logrow-spacer" />
        <span className="logrow-num">{formatLatency(rec.latency_ms)}</span>
        <span className="logrow-num">{formatTokens(rec.total_tokens)} tok</span>
      </div>
      <div className="logrow-preview">{rec.preview || "（无预览）"}</div>
      <div className="logrow-ts">{rec.ts}</div>
    </button>
  );
}
```

- [ ] **Step 6: `LogList.tsx`**

Create `frontend/app/dev/logs/components/LogList.tsx`:

```tsx
"use client";
import type { Summary } from "../types";
import { LogRow } from "./LogRow";

export function LogList({
  records,
  selectedId,
  onSelect,
  hasMore,
  onLoadMore,
  newCount,
  onShowNew,
  loading,
}: {
  records: Summary[];
  selectedId: string | null;
  onSelect: (rec: Summary) => void;
  hasMore: boolean;
  onLoadMore: () => void;
  newCount: number;
  onShowNew: () => void;
  loading: boolean;
}) {
  return (
    <div className="logview-list">
      {newCount > 0 ? (
        <button className="newbar" onClick={onShowNew}>
          ↑ {newCount} 条新记录
        </button>
      ) : null}
      {records.length === 0 && !loading ? <div className="empty">没有匹配的记录</div> : null}
      {records.map((r) => (
        <LogRow key={`${r.seq}-${r.id}`} rec={r} selected={r.id === selectedId} onSelect={onSelect} />
      ))}
      {loading ? <div className="empty">加载中…</div> : null}
      {hasMore ? (
        <button className="loadmore" onClick={onLoadMore}>
          加载更多
        </button>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 7: `ChatTranscript.tsx`(详情核心)**

Create `frontend/app/dev/logs/components/ChatTranscript.tsx`:

```tsx
"use client";
import { useState } from "react";
import type { FullRecord, Message } from "../types";
import { prettyJson } from "../format";
import { CopyButton } from "./CopyButton";

function MessageBlock({ msg }: { msg: Message }) {
  const role = msg.role || "?";
  const cls = role === "system" ? "system" : role === "assistant" ? "assistant" : "user";
  return (
    <div className={`msg ${cls}`}>
      <div className="msg-role">
        {role}
        <CopyButton text={msg.content || ""} />
      </div>
      <pre className="msg-body">{msg.content}</pre>
    </div>
  );
}

export function ChatTranscript({ rec }: { rec: FullRecord }) {
  const [raw, setRaw] = useState(false);
  const messages = rec.request?.messages ?? [];
  const schemaHint = rec.request?.schema_hint ?? "";
  const responseText = rec.response?.content ?? "";
  const pretty = prettyJson(responseText);
  return (
    <div className="transcript">
      <div className="transcript-section-title">发送给 LLM 的对话（{messages.length} 条）</div>
      {messages.map((m, i) => (
        <MessageBlock key={i} msg={m} />
      ))}
      {schemaHint ? (
        <div className="msg schema">
          <div className="msg-role">
            schema_hint
            <CopyButton text={schemaHint} />
          </div>
          <pre className="msg-body">{schemaHint}</pre>
        </div>
      ) : null}
      {responseText ? (
        <>
          <div className="transcript-section-title">模型回复</div>
          <div className="detail-response">
            <div className="msg-role">
              response.content
              <button className="copy-btn" onClick={() => setRaw((v) => !v)}>
                {raw ? "美化" : "raw"}
              </button>
              <CopyButton text={responseText} />
            </div>
            <pre className="msg-body">{raw || !pretty.ok ? responseText : pretty.pretty}</pre>
          </div>
        </>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 8: `LogDetail.tsx`**

Create `frontend/app/dev/logs/components/LogDetail.tsx`:

```tsx
"use client";
import type { FullRecord } from "../types";
import { formatLatency, shortId, statusClass } from "../format";
import { ChatTranscript } from "./ChatTranscript";
import { CopyButton } from "./CopyButton";

function Meta({ rec }: { rec: FullRecord }) {
  const u = rec.usage ?? {};
  return (
    <div className="detail-meta">
      <span className={`badge ${statusClass(rec.status)}`}>{rec.status}</span>
      <span className="badge kind">{rec.kind}</span>
      <span className="detail-meta-item">model: {rec.model}</span>
      <span className="detail-meta-item">延迟: {formatLatency(rec.latency_ms)}</span>
      {u.total_tokens != null ? (
        <span className="detail-meta-item">
          tokens: {u.prompt_tokens ?? "?"}/{u.completion_tokens ?? "?"}/{u.total_tokens}
        </span>
      ) : null}
      <span className="detail-meta-item">id: {shortId(rec.id)}</span>
      <span className="detail-meta-item">{rec.ts}</span>
      <span className="logrow-spacer" />
      <CopyButton text={JSON.stringify(rec, null, 2)} label="复制整条 JSON" />
    </div>
  );
}

export function LogDetail({ record, loading }: { record: FullRecord | null; loading: boolean }) {
  if (loading) return <div className="logview-detail empty">加载中…</div>;
  if (!record) return <div className="logview-detail empty">← 选择左侧一条记录查看详情</div>;
  return (
    <div className="logview-detail">
      <Meta rec={record} />
      {record.error ? (
        <div className="detail-error">
          <strong>error{record.attempt != null ? `（attempt ${record.attempt}）` : ""}:</strong> {record.error}
        </div>
      ) : null}
      {record.kind === "chat" ? <ChatTranscript rec={record} /> : null}
      {record.kind === "embed" ? (
        <div className="transcript">
          <div className="transcript-section-title">embedding 调用</div>
          <div className="detail-meta-item">input_chars: {record.input_chars ?? "—"}</div>
          <div className="detail-meta-item">dims: {record.dims ?? "—"}</div>
        </div>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 9: 类型检查通过**

Run: `cd frontend && npm run lint`
Expected: 无错误。

- [ ] **Step 10: 提交**

```bash
git add frontend/app/dev/logs/logs.css frontend/app/dev/logs/components
git commit -m "feat(debug-ui): 列表/详情/对话/统计组件 + 样式"
```

---

## Task 7: 前端页面编排 `page.tsx`

**Files:**
- Create: `frontend/app/dev/logs/page.tsx`

- [ ] **Step 1: 实现 `page.tsx`**

Create `frontend/app/dev/logs/page.tsx`:

```tsx
"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import "./logs.css";
import type { ChannelInfo, FullRecord, Stats, Summary } from "./types";
import { fetchChannels, fetchRecord, fetchRecords } from "./api";
import { ChannelTabs } from "./components/ChannelTabs";
import { StatsBar } from "./components/StatsBar";
import { LogList } from "./components/LogList";
import { LogDetail } from "./components/LogDetail";

const PAGE = 200;
const POLL_MS = 5000;

export default function LogsPage() {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const channel = "llm"; // v1: fixed channel
  const [records, setRecords] = useState<Summary[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [newestSeq, setNewestSeq] = useState<number | null>(null);
  const [pending, setPending] = useState<Summary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<FullRecord | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(false);

  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [model, setModel] = useState("");
  const [q, setQ] = useState("");
  const [qInput, setQInput] = useState("");

  const filterParams = useMemo(() => ({ kind, status, model, q }), [kind, status, model, q]);

  useEffect(() => {
    fetchChannels()
      .then((r) => setChannels(r.channels))
      .catch((e) => setError(String(e)));
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await fetchRecords(channel, { limit: PAGE, ...filterParams });
      setRecords(r.records);
      setStats(r.stats);
      setHasMore(r.has_more);
      setNewestSeq(r.newest_seq);
      setPending([]);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [filterParams]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // debounce search box -> q
  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(t);
  }, [qInput]);

  // auto-refresh polling: pull records newer than newestSeq into `pending`
  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(async () => {
      if (newestSeq == null) return;
      try {
        const r = await fetchRecords(channel, { since: newestSeq, ...filterParams });
        if (r.records.length) {
          setPending((prev) => {
            const seen = new Set(prev.map((x) => x.seq));
            const fresh = r.records.filter((x) => !seen.has(x.seq));
            return [...fresh, ...prev];
          });
          if (r.newest_seq != null) setNewestSeq(r.newest_seq);
          setStats(r.stats);
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, POLL_MS);
    return () => clearInterval(t);
  }, [autoRefresh, newestSeq, filterParams]);

  const showNew = useCallback(() => {
    setRecords((prev) => {
      const seen = new Set(prev.map((x) => x.seq));
      return [...pending.filter((x) => !seen.has(x.seq)), ...prev];
    });
    setPending([]);
  }, [pending]);

  const loadMore = useCallback(async () => {
    if (!records.length) return;
    const oldest = records[records.length - 1].seq;
    setLoading(true);
    try {
      const r = await fetchRecords(channel, { before: oldest, limit: PAGE, ...filterParams });
      setRecords((prev) => [...prev, ...r.records]);
      setHasMore(r.has_more);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [records, filterParams]);

  const select = useCallback(async (rec: Summary) => {
    setSelectedId(rec.id);
    setDetail(null);
    setDetailLoading(true);
    try {
      setDetail(await fetchRecord(channel, rec.id));
    } catch (e) {
      setError(String(e));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const facets = stats?.facets;

  return (
    <div className="logview">
      <div className="logview-top">
        <ChannelTabs channels={channels} active={channel} onSelect={() => undefined} />
        <StatsBar stats={stats} />
      </div>

      <div className="logview-filters">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">kind: 全部</option>
          {(facets?.kinds ?? []).map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">status: 全部</option>
          {(facets?.statuses ?? []).map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">model: 全部</option>
          {(facets?.models ?? []).map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <input
          className="logview-search"
          placeholder="搜索 prompt / response / error…"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
        />
        <button className="copy-btn" onClick={() => void reload()}>
          <RefreshCw size={13} /> 刷新
        </button>
        <label className="auto-toggle">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} /> 自动刷新
        </label>
      </div>

      {error ? <div className="errorbar">{error}</div> : null}

      <div className="logview-body">
        <LogList
          records={records}
          selectedId={selectedId}
          onSelect={select}
          hasMore={hasMore}
          onLoadMore={() => void loadMore()}
          newCount={pending.length}
          onShowNew={showNew}
          loading={loading}
        />
        <LogDetail record={detail} loading={detailLoading} />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 类型检查 + 构建通过**

Run: `cd frontend && npm run lint && npm run build`
Expected: lint 无错误;build 成功(出现 `/dev/logs` 路由)。

- [ ] **Step 3: 提交**

```bash
git add frontend/app/dev/logs/page.tsx
git commit -m "feat(debug-ui): /dev/logs 页面编排(列表/详情/过滤/分页/自动刷新)"
```

---

## Task 8: 端到端验证(真实日志 + preview)

**Files:** 无新增(仅运行验证)。

> 说明:真实 `llm.jsonl` 在 root master,而新代码在本 worktree。为用**真实数据**验证**worktree 的新代码**,这里把 root master 的 `llm.jsonl` 拷一份到 worktree 的 `.local/logs/`(隔离,不污染 root master;后端默认 `EVENT_LOG_DIR=.local/logs`)。

- [ ] **Step 1: 准备真实数据**

Run:
```bash
mkdir -p .local/logs && cp /Users/hzf/workspace/silicon_notebook/.local/logs/llm.jsonl .local/logs/llm.jsonl && wc -l .local/logs/llm.jsonl
```
Expected: 打印行数(约 1295)。

- [ ] **Step 2: 启动后端(worktree 新代码,后台)**

Run(后台):
```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app --port 8000
```
确认:`curl -s "http://127.0.0.1:8000/api/debug/logs" | head -c 300` 返回含 `"channels"` 且 llm 的 `count` > 0。

- [ ] **Step 3: 启动前端并打开页面**

用 preview 工作流:`preview_start`(在 `frontend/` 跑 `npm run dev`),然后导航到 `http://localhost:3000/dev/logs`(端口若被占用用 3001)。

- [ ] **Step 4: 验证列表与统计**

- `preview_console_logs` / `preview_logs`:无报错。
- `preview_snapshot`:左侧出现交互列表(最新在上),顶部统计 chips 有数值(总数≈1295、ok/retry/error、tokens、延迟)。

- [ ] **Step 5: 验证"送了什么给 LLM"详情**

- `preview_click` 一条 `chat` 行 → `preview_snapshot`:右侧出现 system / user 块、schema_hint 高亮块、模型回复(JSON 美化)。
- 点 `embed` 行 → 显示 input_chars / dims。
- 若有 `error` 行 → 顶部红条显示 error。

- [ ] **Step 6: 验证过滤 / 搜索 / 加载更多**

- 用 `preview_fill` 在搜索框输入一个词(如 `package`)→ `preview_snapshot`:列表收敛、命中数变化。
- 选 `status=error` → 列表只剩 error。
- 滚到底点"加载更多" → 追加更旧记录。

- [ ] **Step 7: 截图留证**

`preview_screenshot`:分别截"列表+某条 chat 详情"一张,作为完成证据贴给用户。

- [ ] **Step 8: 全量门禁**

Run:
```bash
bash scripts/check.sh
```
Expected: 后端 py_compile + smoke 通过、前端 lint 通过(green)。

- [ ] **Step 9: 清理验证数据并提交(如有)**

`.local/` 已被 `.gitignore` 忽略,拷入的 `llm.jsonl` 不会进版本库,无需删除;如需保持干净可 `rm .local/logs/llm.jsonl`。本任务通常无代码改动、无需提交;若验证中发现并修了 bug,按对应任务补测后再提交。

---

## Self-Review(plan 对 spec 的覆盖核对)

**1. Spec coverage**
- 后端只读接口 + 门控 `debug_logs_enabled` + 通道白名单 → T2/T3 ✅
- `seq` 稳定游标、坏行跳过、文件缺失不报错 → T1(load/paginate)+ T3(file_exists)✅
- 列表(limit/before/since/kind/status/model/q)+ 摘要 + 统计(by_*/total_tokens/latency/facets/malformed)→ T1/T3 ✅
- facets 基于全量、count 基于过滤 → `compute_stats` + 测试 `test_stats_counts_filtered_facets_full` / `test_list_records_and_stats` ✅
- 单条完整记录详情 → T3 `get_record` ✅
- 前端 `/dev/logs` master-detail、chat 对话渲染(system/schema_hint/user/response)、embed、error/retry、复制、JSON 美化 → T6/T7 ✅
- 手动刷新 + 可选自动轮询(since)、加载更多(before)、"N 条新" → T7 ✅
- 错误处理(门控关 404/非法 channel 404/文件缺失空态/fetch 失败 banner)→ T3 + T7 ✅
- 测试:后端 pytest(纯函数 + HTTP)、前端 node:test、preview E2E → T1/T3/T4/T8 ✅
- 不加新依赖、复用 globals.css 变量与 API_BASE → T5/T6/T7 ✅

**2. Placeholder scan:** 无 TBD/TODO;每个改代码的步骤均给出完整代码与确切命令/预期。

**3. Type consistency(关键名一致性核对):**
- 后端响应字段 `file_exists` / `newest_seq` / `has_more` / `records` / `stats` 与前端 `ListResponse` 一致 ✅
- 摘要字段 `seq/id/ts/kind/model/status/latency_ms/total_tokens/attempt/error/preview` 与 `Summary` 一致 ✅
- `stats` 字段 `total/filtered/by_kind/by_status/by_model/total_tokens/latency_ms{avg,max}/malformed_lines/facets{kinds,statuses,models}` 后端与 `Stats` 一致 ✅
- 函数名 `load_records/filter_records/to_summary/compute_stats/paginate` 在实现与测试中一致 ✅
- 前端 helper `statusClass/formatLatency/formatTokens/prettyJson/shortId` 在 format.ts/测试/组件中一致 ✅
- API 函数 `fetchChannels/fetchRecords/fetchRecord` 在 api.ts 与 page.tsx 一致 ✅
- 查询参数 `before`(更旧)/`since`(更新)语义在后端 `paginate`、HTTP 测试、前端 loadMore/poll 中一致 ✅
