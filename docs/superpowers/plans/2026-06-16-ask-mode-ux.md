# 问答 mode 体系 + 前端呈现 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把后端 ask mode 收成单一真源 registry（未知 mode 显式 422，杜绝静默落 fast），前端做两级 mode 菜单（通用问答 / 严格推理→深挖|图谱）+ typed const + 跨端契约校验，所有 mode 统一走 `/ask/stream`，并把所用 mode 落库以精确恢复会话。

**Architecture:** 后端新增 `app/services/ask_modes.py` 作 canonical registry（dispatch + 校验同读）；`ask()` 改查表分发，旧 fast 体抽成 `ask_fast`；`/ask` 与 `/ask/stream` 入口校验 mode 合法性并新增 `GET /ask-modes`。前端新增 `app/ask-modes.ts`（唯一 mode 字面量处）+ `ask-modes.test.mjs`，`page.tsx` 用扁平 `askMode` 状态驱动两级控件、统一 `readAskStream`、按 `response.mode` 恢复会话。`AskResponse.mode` 透明落库（`_save_answer` 存 `model_dump()`）。`NotebookSummary.kg_ready` 驱动严格推理的 KG 门控。`scripts/check_ask_modes_contract.py` 在 `check.sh` 里断言两端 id 集一致。

**Tech Stack:** Python / FastAPI / pydantic / sqlite（后端，pytest + `scripts/check.sh` 的 hermetic smoke）；Next.js / React / TypeScript（前端，`node --test app/*.test.mjs` + `tsc --noEmit`）。

**Spec:** `docs/superpowers/specs/2026-06-16-ask-mode-ux-design.md`

---

## 关键现状锚点（实现时核对，行号会随编辑漂移）

| 位置 | 现状 |
|---|---|
| `backend/app/services/sqlite_repository.py:4230-4525` | `ask()`：4233-4242 是 mode if-链，4243-4525 是 fast fallthrough 体（以 `import time` 开头，以 `return response` 结尾） |
| `…:4155` `ask_chunk` / `:4937` `ask_reasoning(…, on_trace=None)` / `:5012` `ask_graph(…, seed_ids=None)`（无 on_trace） / `:4526` `_ask_global` | 各 mode 的 handler |
| `…:5190` `_save_answer` | 存 `response.model_dump()` 为 JSON 到 `answers.payload`（加字段透明） |
| `…:5291` `AskResponse(**payload)` | 会话重建处（`mode` 自动回流） |
| `…:5858` `NotebookSummary(...)` | 构造处，`db` 在作用域内，已用 `self._count_knowledge(db, ...)` |
| `backend/app/api/routes.py:395` `ask` / `:407` `ask_stream` | 端点；`:422`/`:431` 有 `getattr(payload,"mode","fast")` 雷 |
| `backend/app/models/schemas.py:152` `AskRequest` / `:174` `AskResponse` / `:93` `NotebookSummary` | schema |
| `frontend/app/page.tsx:34` `NotebookSummary` 类型 / `:119` `AskResponse` 类型 / `:458` `api<T>` / `:487` `readAskStream` / `:775` `pendingReasoning` / `:808` `reasoningMode` / `:1541` `runAsk` / `:1584` `openSession` / `:1597` `startNewSession` / `:2470-2483` 输入条+推理 toggle | 前端 mode 相关 |
| `frontend/app/session-reasoning.ts` + `*.test.mjs` + `session-reasoning-ui.test.mjs` | 待退役的猜测启发式 |
| `scripts/check.sh` | py_compile 文件清单 + smoke + 前端 `npm run test`/`lint` |

**运行约定：**
- 后端单测：`cd backend && python -m pytest tests/<file>.py -v`（用带 pytest 的项目 Python，如 check.sh 的 miniconda base）。
- 前端单测：`cd frontend && node --test app/<file>.test.mjs`；全量 `npm run test`；类型 `npm run lint`。
- 全量门禁：`bash scripts/check.sh`（若默认 python 路径不在本机：`PYTHON_BIN=python3 bash scripts/check.sh`）。

---

## P1 — 后端 mode registry + 分发 + 校验

### Task 1: 新增 canonical registry `ask_modes.py`

**Files:**
- Create: `backend/app/services/ask_modes.py`
- Create: `backend/tests/test_ask_modes.py`
- Modify: `scripts/check.sh`（py_compile 清单加新文件）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_ask_modes.py`:

```python
import pytest
from app.services.ask_modes import (
    ASK_MODES, DEFAULT_MODE, AskMode, UnknownAskMode,
    resolve_mode, user_facing_mode_ids,
)


def test_registry_has_expected_modes_and_flags():
    assert set(ASK_MODES) == {"chunk", "reasoning", "graph", "fast", "global"}
    assert ASK_MODES["chunk"].handler == "ask_chunk"
    assert ASK_MODES["chunk"].requires_kg is False
    assert ASK_MODES["reasoning"].handler == "ask_reasoning"
    assert ASK_MODES["reasoning"].streaming is True
    assert ASK_MODES["reasoning"].requires_kg is True
    assert ASK_MODES["graph"].handler == "ask_graph"
    assert ASK_MODES["graph"].streaming is False        # ask_graph 暂无 on_trace
    assert ASK_MODES["fast"].handler == "ask_fast"


def test_user_facing_subset_is_chunk_and_strict_engines():
    assert user_facing_mode_ids() == ["chunk", "reasoning", "graph"]
    assert ASK_MODES["fast"].user_facing is False
    assert ASK_MODES["global"].user_facing is False


def test_resolve_known_default_and_unknown():
    assert resolve_mode("graph") is ASK_MODES["graph"]
    assert resolve_mode(None) is ASK_MODES[DEFAULT_MODE]   # 缺省 → chunk
    assert resolve_mode("") is ASK_MODES[DEFAULT_MODE]
    with pytest.raises(UnknownAskMode) as exc:
        resolve_mode("bogus")
    assert exc.value.mode == "bogus"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ask_modes.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.ask_modes'`

- [ ] **Step 3: 实现 registry**

`backend/app/services/ask_modes.py`:

```python
"""Canonical registry of ask() retrieval modes — the single source of truth for
which modes exist, where each dispatches, and how the API/UI must treat them.

SQLiteRepository.ask() (dispatch) and the API layer (validation + /ask-modes)
both read this module, so a mode is added/renamed in exactly one place; the
cross-stack check scripts/check_ask_modes_contract.py keeps the frontend mode
list (frontend/app/ask-modes.ts) in lock-step.
"""
from __future__ import annotations

from dataclasses import dataclass


class UnknownAskMode(ValueError):
    """An ask() mode string not in the registry. The API layer maps this to HTTP
    422 — there is no silent fall-through to the legacy KG path."""

    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.mode = mode


@dataclass(frozen=True)
class AskMode:
    id: str
    handler: str        # method name on SQLiteRepository
    group: str          # "general" | "strict" | "legacy" | "global"
    streaming: bool     # handler accepts on_trace + emits progress over the stream
    requires_kg: bool
    user_facing: bool


# Insertion order = display order for user_facing modes.
ASK_MODES: dict[str, AskMode] = {
    "chunk":     AskMode("chunk",     "ask_chunk",     "general", False, False, True),
    "reasoning": AskMode("reasoning", "ask_reasoning", "strict",  True,  True,  True),
    "graph":     AskMode("graph",     "ask_graph",     "strict",  False, True,  True),
    "fast":      AskMode("fast",      "ask_fast",      "legacy",  False, True,  False),
    "global":    AskMode("global",    "_ask_global",   "global",  False, True,  False),
}

DEFAULT_MODE = "chunk"


def resolve_mode(mode: str | None) -> AskMode:
    """Return the AskMode for `mode` (DEFAULT_MODE when None/empty).
    Raise UnknownAskMode for anything not registered."""
    key = mode or DEFAULT_MODE
    try:
        return ASK_MODES[key]
    except KeyError as exc:
        raise UnknownAskMode(key) from exc


def user_facing_mode_ids() -> list[str]:
    """Mode ids the UI may expose, in registry order."""
    return [m.id for m in ASK_MODES.values() if m.user_facing]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ask_modes.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 把新文件加入 check.sh 的 py_compile 清单**

`scripts/check.sh`：在 py_compile 的文件清单里（`sqlite_repository.py` 那一行附近）加一行：

```bash
  "$ROOT_DIR/backend/app/services/ask_modes.py" \
```

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/ask_modes.py backend/tests/test_ask_modes.py scripts/check.sh
git commit -m "feat(ask): mode registry 单一真源(ask_modes.py)"
```

---

### Task 2: `ask()` 改查表分发 + 抽出 `ask_fast`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`ask()` 4230-4525）
- Test: `backend/tests/test_ask_modes.py`（追加）

- [ ] **Step 1: 写失败测试（追加到 test_ask_modes.py）**

```python
def test_ask_dispatches_by_registry(monkeypatch, tmp_path):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.models.schemas import AskRequest, AskResponse, NotebookCreate

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="nb"))

    calls = {}
    for mid in ("ask_chunk", "ask_reasoning", "ask_graph", "ask_fast", "_ask_global"):
        def make(mid):
            return lambda notebook_id, payload: calls.__setitem__("hit", mid) or AskResponse(conclusion=mid)
        monkeypatch.setattr(repo, mid, make(mid))

    assert repo.ask(nb.id, AskRequest(question="q")).conclusion == "ask_chunk"       # 缺省
    assert repo.ask(nb.id, AskRequest(question="q", mode="graph")).conclusion == "ask_graph"
    assert repo.ask(nb.id, AskRequest(question="q", mode="fast")).conclusion == "ask_fast"

    from app.services.ask_modes import UnknownAskMode
    with pytest.raises(UnknownAskMode):
        repo.ask(nb.id, AskRequest(question="q", mode="bogus"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ask_modes.py::test_ask_dispatches_by_registry -v`
Expected: FAIL（`ask()` 仍是 if-链，`mode="bogus"` 不抛而是静默走 fast 体 → 断言或 AttributeError 失败）

- [ ] **Step 3: 抽出 `ask_fast` 并重写 `ask()`**

在 `sqlite_repository.py`：
1. 把 `ask()` 中 4243-4525 的整段 fast fallthrough 体（自 `import time` 起，至该方法结尾的 `return response` 止）**原样移动**到一个新方法：

```python
    def ask_fast(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Legacy KG-native ask over the 4 KG object types (claim/formula/
        procedure/concept) + 1-hop relation expansion. Non-default; reachable
        only via explicit mode="fast" (eval/back-compat). See ask_chunk for the
        default path."""
        import time
        ask_started = time.perf_counter()
        # …（移动过来的原 fallthrough 体，逐字不改）…
        return response
```

2. 把 `ask()` 的方法体整体替换为查表分发：

```python
    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Dispatch to the retrieval handler named by payload.mode, resolved
        through the ask_modes registry. Unknown modes raise UnknownAskMode (the
        API layer returns 422) — never a silent fall-through to the legacy path."""
        from app.services.ask_modes import resolve_mode
        spec = resolve_mode(getattr(payload, "mode", None))
        return getattr(self, spec.handler)(notebook_id, payload)
```

（顶部已有的 `import time`/`from app.services.vector_index import query_sims` 等若仅 fast 体用到，随体一起留在 `ask_fast` 内即可。）

- [ ] **Step 4: 跑测试确认通过 + 回归原 fast 测试**

Run: `cd backend && python -m pytest tests/test_ask_modes.py tests/test_ask_redesign.py -v`
Expected: PASS（含原 `test_ask_global_topn_not_fixed_quota` 用 `mode="fast"` 仍走旧体）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ask_modes.py
git commit -m "refactor(ask): ask() 查表分发 + 抽出 ask_fast,未知 mode 抛 UnknownAskMode"
```

---

### Task 3: 路由层校验 422 + `GET /ask-modes` + 清雷

**Files:**
- Modify: `backend/app/api/routes.py`（`ask` 395、`ask_stream` 407、新增 `/ask-modes`）
- Test: `backend/tests/test_ask_modes_api.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_ask_modes_api.py`:

```python
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    from app.core.config import get_settings
    from app.api import routes
    from app.main import create_app
    get_settings.cache_clear()
    routes.repository.cache_clear()
    return TestClient(create_app())


def test_ask_modes_endpoint_lists_user_facing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/ask-modes").json()
    assert [m["id"] for m in body] == ["chunk", "reasoning", "graph"]
    assert {m["id"]: m["requires_kg"] for m in body} == {
        "chunk": False, "reasoning": True, "graph": True}


def test_unknown_mode_returns_422_not_silent_fast(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q", "mode": "bogus"})
    assert r.status_code == 422
    assert "bogus" in str(r.json()["detail"])
    rs = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "bogus"})
    assert rs.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ask_modes_api.py -v`
Expected: FAIL（`/api/ask-modes` 404；bogus mode 走 fast 返回 200）

- [ ] **Step 3: 实现路由变更**

`backend/app/api/routes.py`：
1. 顶部 import 区加：

```python
from app.services.ask_modes import resolve_mode, user_facing_mode_ids, UnknownAskMode, ASK_MODES
```

2. `ask` 端点（395）改为捕获 UnknownAskMode：

```python
@router.post("/notebooks/{notebook_id}/ask", response_model=AskResponse)
def ask(notebook_id: str, payload: AskRequest) -> AskResponse:
    try:
        return repository().ask(notebook_id, payload)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

3. 新增 `/ask-modes`（放在 `ask` 端点附近）：

```python
@router.get("/ask-modes")
def ask_modes() -> list[dict[str, Any]]:
    """User-facing ask modes (single source: app/services/ask_modes.py).
    Copy/labels live in the frontend; this exposes ids + behavioural flags."""
    return [
        {"id": m.id, "group": m.group,
         "requires_kg": m.requires_kg, "streaming": m.streaming}
        for m in ASK_MODES.values() if m.user_facing
    ]
```

4. `ask_stream`（407）：进入即校验 mode，worker 按 spec 分发，清掉 `"fast"` 默认：

```python
@router.post("/notebooks/{notebook_id}/ask/stream")
def ask_stream(notebook_id: str, payload: AskRequest) -> StreamingResponse:
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        spec = resolve_mode(payload.mode)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})

    def stream_events():
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        events.put({"event": "progress", "step": {
            "step_type": "start", "summary": "启动检索",
            "detail": {"mode": payload.mode}}})

        def on_trace(step) -> None:
            events.put({"event": "progress", "step": step.model_dump()})

        def worker() -> None:
            try:
                handler = getattr(repo, spec.handler)
                response = handler(notebook_id, payload, on_trace=on_trace) \
                    if spec.streaming else handler(notebook_id, payload)
                events.put({"event": "final", "response": response.model_dump()})
            except Exception as exc:  # noqa: BLE001
                events.put({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield _ndjson_line(event)

    return StreamingResponse(stream_events(), media_type="application/x-ndjson")
```

（确认 `Any` 已在 `routes.py` import；若未，补 `from typing import Any`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ask_modes_api.py tests/test_reasoning_stream.py -v`
Expected: PASS（含原 reasoning 流式回归）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_ask_modes_api.py
git commit -m "feat(api): /ask-modes + 未知 mode 422 + ask_stream 按 registry 分发(清 fast 默认雷)"
```

---

## P2 — 端点统一：chunk 经 stream 返回

> P1 Task 3 已让 `ask_stream` 按 registry 分发任意 mode；本 phase 验证非 streaming mode（chunk）经 stream 走 `start→final`。

### Task 4: chunk 经 `/ask/stream` 的 start→final 验证

**Files:**
- Test: `backend/tests/test_ask_modes_api.py`（追加）

- [ ] **Step 1: 写失败/回归测试（追加）**

```python
def test_chunk_mode_streams_start_then_final(tmp_path, monkeypatch):
    import json
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/ask/stream",
                    json={"question": "q", "mode": "chunk"})
    assert r.status_code == 200
    events = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "progress" and events[0]["step"]["step_type"] == "start"
    assert events[0]["step"]["detail"]["mode"] == "chunk"
    assert kinds[-1] == "final"
    assert "reasoning_trace" not in events[-1]["response"] or \
        not events[-1]["response"]["reasoning_trace"]   # chunk 无 trace
```

- [ ] **Step 2: 跑测试**

Run: `cd backend && python -m pytest tests/test_ask_modes_api.py::test_chunk_mode_streams_start_then_final -v`
Expected: PASS（P1 Task 3 的 worker 已支持）。若 FAIL，回查 Task 3 worker 的 `spec.streaming` 分支。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_ask_modes_api.py
git commit -m "test(api): chunk 经 /ask/stream 走 start→final 回归"
```

---

## P3 — 持久化所用 mode（精确恢复会话）

### Task 5: `AskResponse.mode` 回填 + 落库 + 回流

**Files:**
- Modify: `backend/app/models/schemas.py`（`AskResponse` 174）
- Modify: `backend/app/services/sqlite_repository.py`（各 `ask_*` 回填 `response.mode`）
- Test: `backend/tests/test_ask_mode_persist.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_ask_mode_persist.py`:

```python
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "m")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_chunk_response_carries_mode_and_round_trips(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    resp = repo.ask(nb.id, AskRequest(question="q", mode="chunk"))
    assert resp.mode == "chunk"
    detail = repo.get_conversation(resp.conversation_id)
    assert detail.turns[-1].response.mode == "chunk"   # 经 answers.payload JSON 回流
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ask_mode_persist.py -v`
Expected: FAIL（`AskResponse` 无 `mode` 字段 → AttributeError / 默认 ""）

- [ ] **Step 3: 实现**

1. `schemas.py` `AskResponse` 加字段（放在 `llm_mode` 附近）：

```python
    # 本轮实际使用的检索 mode（chunk/reasoning/graph/fast/global）。
    # 落库供 openSession 精确恢复引擎，替代旧的 reasoning_trace 猜测。
    mode: str = ""
```

2. `sqlite_repository.py`：每个 handler 在构造/返回 `AskResponse` 前回填自身 id。最稳妥统一做法——在 `_save_answer` 之前由各 handler 设置 `response.mode = payload.mode`。具体在每个 `ask_*` 里、`self._save_answer(...)` 调用之前加一行：

```python
        response.mode = getattr(payload, "mode", "") or "chunk"
```

   涉及方法：`ask_chunk`（~4226 处 `response.answer_id = self._save_answer(...)` 之前）、`ask_fast`、`ask_reasoning`、`ask_graph`、`_ask_global`。对 `ask_fast` 显式写 `response.mode = "fast"`（其 payload.mode 恒为 "fast"，但显式更清晰）。

   > 实现提示：若某 handler 不直接持有 `response` 变量名，定位其 `self._save_answer(notebook_id, question, <resp>, ...)` 调用，在该行前对 `<resp>.mode` 赋值。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && python -m pytest tests/test_ask_mode_persist.py tests/test_conversations.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/tests/test_ask_mode_persist.py
git commit -m "feat(ask): AskResponse.mode 落库,会话精确恢复引擎"
```

---

## P4 — 前端 typed const + 两级控件 + 统一 stream

### Task 6: 新增 `ask-modes.ts`（唯一 mode 真源）+ 纯逻辑测试

**Files:**
- Create: `frontend/app/ask-modes.ts`
- Create: `frontend/app/ask-modes.test.mjs`

- [ ] **Step 1: 写失败测试**

`frontend/app/ask-modes.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import {
  ASK_MODES, DEFAULT_ASK_MODE, ASK_MODE_GROUPS,
  askModeIds, groupOf, defaultModeForGroup, requiresKg, canUseMode, modeFromTurn,
} from "./ask-modes.ts";

test("user-facing ids and default", () => {
  assert.deepEqual(askModeIds(), ["chunk", "reasoning", "graph"]);
  assert.equal(DEFAULT_ASK_MODE, "chunk");
  assert.deepEqual(ASK_MODE_GROUPS.map((g) => g.id), ["general", "strict"]);
});

test("grouping + default engine per group", () => {
  assert.equal(groupOf("chunk"), "general");
  assert.equal(groupOf("graph"), "strict");
  assert.equal(defaultModeForGroup("general"), "chunk");
  assert.equal(defaultModeForGroup("strict"), "reasoning");   // groupDefault
});

test("kg gating", () => {
  assert.equal(requiresKg("chunk"), false);
  assert.equal(requiresKg("reasoning"), true);
  assert.equal(canUseMode("chunk", false), true);     // 通用问答无需 KG
  assert.equal(canUseMode("reasoning", false), false);
  assert.equal(canUseMode("graph", true), true);
});

test("restore mode from a prior turn (exact engine, safe fallback)", () => {
  assert.equal(modeFromTurn({ response: { mode: "graph" } }), "graph");
  assert.equal(modeFromTurn({ response: { mode: "reasoning" } }), "reasoning");
  assert.equal(modeFromTurn({ response: { mode: "fast" } }), "chunk");   // 非 user-facing → 兜底
  assert.equal(modeFromTurn({ response: {} }), "chunk");
  assert.equal(modeFromTurn(undefined), "chunk");
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && node --test app/ask-modes.test.mjs`
Expected: FAIL（`Cannot find module './ask-modes.ts'`）

- [ ] **Step 3: 实现 `ask-modes.ts`**

`frontend/app/ask-modes.ts`:

```typescript
// 前端用户可选 ask mode 的单一真源（镜像后端 app/services/ask_modes.py 的
// user_facing 子集；由 scripts/check_ask_modes_contract.py 锁同步）。
// 全前端唯一出现 mode 字面量的地方——其余代码只引用本文件。

export type AskModeId = "chunk" | "reasoning" | "graph";
export type AskModeGroup = "general" | "strict";

export interface AskModeDef {
  id: AskModeId;
  group: AskModeGroup;
  label: string;
  desc: string;
  requiresKg: boolean;
  groupDefault?: boolean; // 组内默认引擎
}

export const ASK_MODES: AskModeDef[] = [
  { id: "chunk", group: "general", label: "通用问答",
    desc: "默认。大范围检索原文，适合综述、对比、找事实。", requiresKg: false, groupDefault: true },
  { id: "reasoning", group: "strict", label: "深挖推理",
    desc: "agent 多轮深挖，展示思考轨迹。", requiresKg: true, groupDefault: true },
  { id: "graph", group: "strict", label: "图谱多跳",
    desc: "沿知识图谱多跳遍历，展示关联子图。", requiresKg: true },
];

export const DEFAULT_ASK_MODE: AskModeId = "chunk";

export const ASK_MODE_GROUPS: { id: AskModeGroup; label: string }[] = [
  { id: "general", label: "通用问答" },
  { id: "strict", label: "严格推理" },
];

export function askModeIds(): AskModeId[] {
  return ASK_MODES.map((m) => m.id);
}

function defOf(id: AskModeId): AskModeDef {
  const d = ASK_MODES.find((m) => m.id === id);
  if (!d) throw new Error(`unknown ask mode: ${id}`);
  return d;
}

export function groupOf(id: AskModeId): AskModeGroup {
  return defOf(id).group;
}

export function modesInGroup(group: AskModeGroup): AskModeDef[] {
  return ASK_MODES.filter((m) => m.group === group);
}

export function defaultModeForGroup(group: AskModeGroup): AskModeId {
  const d = modesInGroup(group).find((m) => m.groupDefault) ?? modesInGroup(group)[0];
  if (!d) throw new Error(`no mode in group: ${group}`);
  return d.id;
}

export function requiresKg(id: AskModeId): boolean {
  return defOf(id).requiresKg;
}

export function canUseMode(id: AskModeId, kgReady: boolean): boolean {
  return kgReady || !requiresKg(id);
}

// 按上一轮 turn.response.mode 精确恢复（含引擎）；非 user-facing/缺失 → 兜底默认。
export function modeFromTurn(
  turn: { response?: { mode?: string } } | undefined,
): AskModeId {
  const m = turn?.response?.mode;
  return m && (askModeIds() as string[]).includes(m) ? (m as AskModeId) : DEFAULT_ASK_MODE;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && node --test app/ask-modes.test.mjs`
Expected: PASS（4 tests）

- [ ] **Step 5: 提交**

```bash
git add frontend/app/ask-modes.ts frontend/app/ask-modes.test.mjs
git commit -m "feat(fe): ask-modes.ts 单一真源(两级菜单+KG门控+恢复逻辑)"
```

---

### Task 7: `page.tsx` 接线两级控件 + 统一 stream + 退役猜测启发式

**Files:**
- Modify: `frontend/app/page.tsx`（imports 17/19、类型 34/119、状态 775/808、`runAsk` 1541、`openSession` 1584、`startNewSession` 1597、控件 2470-2483、`pendingReasoning` 用法 2441）
- Delete: `frontend/app/session-reasoning.ts`、`frontend/app/session-reasoning.test.mjs`、`frontend/app/session-reasoning-ui.test.mjs`

> 本任务以 `npm run lint`(tsc) + Task 6 的 `ask-modes.test.mjs` + 手动 UI 走查验证（仓库前端测试为纯函数，JSX 不做单测）。

- [ ] **Step 1: 替换 imports**

`page.tsx:17-19`，把 `import { lastTurnUsedReasoning } from "./session-reasoning";`（19）删除，并补：

```typescript
import {
  ASK_MODES, ASK_MODE_GROUPS, DEFAULT_ASK_MODE, type AskModeId,
  groupOf, modesInGroup, defaultModeForGroup, requiresKg, canUseMode, modeFromTurn,
} from "./ask-modes";
```

- [ ] **Step 2: 类型加字段**

`page.tsx:34` `NotebookSummary` 类型（`tier?: string;` 47 附近）加：

```typescript
  kg_ready?: boolean;
```

`page.tsx:119` `AskResponse` 类型（`llm_mode: string;` 附近）加：

```typescript
  mode?: AskModeId;
```

- [ ] **Step 3: 状态从 reasoningMode 改 askMode**

- `page.tsx:808` `const [reasoningMode, setReasoningMode] = useState(false);` → `const [askMode, setAskMode] = useState<AskModeId>(DEFAULT_ASK_MODE);`
- `page.tsx:775` `const [pendingReasoning, setPendingReasoning] = useState(false);` → `const [pendingMode, setPendingMode] = useState<AskModeId>(DEFAULT_ASK_MODE);`

- [ ] **Step 4: `runAsk` 统一走 stream + KG 门控**

`page.tsx:1541-1576` `runAsk` 改为（保留其余结构，替换 mode/分支/pending 相关行）：

```typescript
  async function runAsk(nextQuestion = question) {
    if (!currentNotebookId) return;
    const q = nextQuestion.trim();
    if (!q) return;
    if (requiresKg(askMode) && !currentNotebook?.kg_ready) {
      setToast("严格推理需先为该 notebook 构建知识图谱");
      return;
    }
    setChatMode("ask");
    setQuestion("");
    setPendingQuestion(q);
    setPendingMode(askMode);
    setPendingTrace([]);
    setAsking(true);
    try {
      const payload = { question: q, conversation_id: conversationId ?? undefined, mode: askMode };
      const response = await readAskStream<AskResponse>(
        `/notebooks/${currentNotebookId}/ask/stream`,
        payload,
        (step) => setPendingTrace((previous) => [...previous, step]),
      );
      setTurns((prev) => [...prev, { question: q, response }]);
      setConversationId(response.conversation_id);
    } catch (error) {
      setQuestion(q);
      reportError(error);
    } finally {
      setPendingQuestion("");
      setPendingMode(DEFAULT_ASK_MODE);
      setPendingTrace([]);
      setAsking(false);
    }
    await loadSessions(currentNotebookId);
  }
```

- [ ] **Step 5: `openSession` / `startNewSession` 精确恢复**

- `page.tsx:1587` `setReasoningMode(lastTurnUsedReasoning(detail.turns));` → `setAskMode(modeFromTurn(detail.turns[detail.turns.length - 1]));`
- 同函数内 `setPendingReasoning(false);`（1590）→ `setPendingMode(DEFAULT_ASK_MODE);`
- `page.tsx:1600` `setReasoningMode(false);`（`startNewSession`）→ `setAskMode(DEFAULT_ASK_MODE);`
- `page.tsx:1601`/`1615` 等其它 `setPendingReasoning(false);` 全部 → `setPendingMode(DEFAULT_ASK_MODE);`

- [ ] **Step 6: `pendingReasoning` 读取处改用 pendingMode**

`page.tsx:2441` 附近 `{pendingReasoning ? (` → `{groupOf(pendingMode) === "strict" ? (`（其展示「思考中/trace」占位的条件，从「是否推理」泛化为「是否严格推理组」）。

- [ ] **Step 7: 替换输入条控件为两级 mode 控件**

`page.tsx:2474-2480` 的 `<button className="reasoning-toggle"…>✦ 推理</button>` 整块替换为：

```tsx
                  <div className="ask-mode-control" role="group" aria-label="问答模式">
                    {ASK_MODE_GROUPS.map((g) => (
                      <button
                        key={g.id}
                        type="button"
                        className={`mode-tab${groupOf(askMode) === g.id ? " active" : ""}`}
                        onClick={() => setAskMode(defaultModeForGroup(g.id))}
                      >
                        {g.label}
                      </button>
                    ))}
                    {groupOf(askMode) === "strict" && (
                      <span className="mode-engines">
                        {modesInGroup("strict").map((m) => (
                          <button
                            key={m.id}
                            type="button"
                            className={`mode-engine${askMode === m.id ? " active" : ""}`}
                            title={m.desc}
                            onClick={() => setAskMode(m.id)}
                          >
                            {m.label}
                          </button>
                        ))}
                      </span>
                    )}
                    {groupOf(askMode) === "strict" && !currentNotebook?.kg_ready && (
                      <span className="mode-hint">该 notebook 尚无知识图谱，严格推理需先构建</span>
                    )}
                  </div>
```

> 「构建 KG」实际触发按钮待 chunk-native spec P4「KG 抽取开关化」上线后接入（届时把 `.mode-hint` 内补一个调用建 KG 入口的按钮）；本任务先给提示文案 + send 门控（Step 4），不伪造不存在的构建端点。

- [ ] **Step 8: 删除退役文件**

```bash
git rm frontend/app/session-reasoning.ts frontend/app/session-reasoning.test.mjs frontend/app/session-reasoning-ui.test.mjs
```

- [ ] **Step 9: 类型检查 + 前端全量测试**

Run: `cd frontend && npm run lint && npm run test`
Expected: tsc 无错（确认无残留 `reasoningMode`/`pendingReasoning`/`lastTurnUsedReasoning` 引用）；`node --test` 全绿。
排错：`grep -n "reasoningMode\|pendingReasoning\|lastTurnUsedReasoning\|session-reasoning" frontend/app/page.tsx` 应为空。

- [ ] **Step 10: 提交**

```bash
git add frontend/app/page.tsx
git commit -m "feat(fe): 两级 mode 控件+统一 readAskStream+按 response.mode 恢复,退役 session-reasoning 猜测"
```

---

## P5 — 严格推理的 KG 门控

### Task 8: 后端 `NotebookSummary.kg_ready`

**Files:**
- Modify: `backend/app/models/schemas.py`（`NotebookSummary` 93）
- Modify: `backend/app/services/sqlite_repository.py`（新增 `_has_kg` + 构造处 5858）
- Test: `backend/tests/test_kg_ready.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_kg_ready.py`:

```python
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    return SQLiteRepository(Settings())


def test_kg_ready_false_before_kg_true_after(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.get_notebook(nb.id).kg_ready is False
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"}, "evidence": []},
    ], [])
    assert repo.get_notebook(nb.id).kg_ready is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_kg_ready.py -v`
Expected: FAIL（`NotebookSummary` 无 `kg_ready`）

- [ ] **Step 3: 实现**

1. `schemas.py` `NotebookSummary` 加（`tier: str = "personal"` 附近）：

```python
    # 该 notebook 是否已构建知识图谱（有任意 knowledge_objects）。
    # 驱动前端严格推理(reasoning/graph)的可用门控。
    kg_ready: bool = False
```

2. `sqlite_repository.py` 加 helper（放在 `_count_knowledge` 附近）：

```python
    def _has_kg(self, db: sqlite3.Connection, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id = ?)",
            (notebook_id,),
        ).fetchone()
        return bool(row[0])
```

3. `NotebookSummary(...)` 构造处（5858）加参数：

```python
            kg_ready=self._has_kg(db, row["id"]),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_kg_ready.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/tests/test_kg_ready.py
git commit -m "feat(api): NotebookSummary.kg_ready 驱动严格推理门控"
```

> 前端门控（send 拦截 + 提示）已在 Task 7 Step 4/Step 7 用 `currentNotebook?.kg_ready` 接好；`canUseMode` 单测在 Task 6。本 phase 后端补齐信号即闭环。

---

## P6 — 跨端契约校验

### Task 9: `check_ask_modes_contract.py` + 接入 check.sh

**Files:**
- Create: `scripts/check_ask_modes_contract.py`
- Modify: `scripts/check.sh`

- [ ] **Step 1: 实现契约脚本**

`scripts/check_ask_modes_contract.py`:

```python
#!/usr/bin/env python3
"""Cross-stack contract: the frontend's user-facing ask-mode ids
(frontend/app/ask-modes.ts) must exactly equal the backend registry's
user_facing ids (backend/app/services/ask_modes.py). Adding/renaming a mode on
one side without the other fails here. Run by scripts/check.sh."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.services.ask_modes import user_facing_mode_ids  # noqa: E402


def frontend_ids() -> list[str]:
    text = (ROOT / "frontend/app/ask-modes.ts").read_text(encoding="utf-8")
    m = re.search(r"export const ASK_MODES[^\[]*\[(.*?)\];", text, re.S)
    if not m:
        raise SystemExit("ask-modes.ts: ASK_MODES array not found")
    return re.findall(r'id:\s*"([A-Za-z0-9_]+)"', m.group(1))


def main() -> int:
    backend = set(user_facing_mode_ids())
    frontend = set(frontend_ids())
    if backend != frontend:
        print("ask-mode contract MISMATCH", file=sys.stderr)
        print(f"  backend user_facing : {sorted(backend)}", file=sys.stderr)
        print(f"  frontend ASK_MODES  : {sorted(frontend)}", file=sys.stderr)
        print(f"  only backend: {sorted(backend - frontend)} | "
              f"only frontend: {sorted(frontend - backend)}", file=sys.stderr)
        return 1
    print(f"ask-mode contract OK: {sorted(backend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 手动跑一次（应通过）**

Run: `PYTHONPATH=backend python scripts/check_ask_modes_contract.py`
Expected: `ask-mode contract OK: ['chunk', 'graph', 'reasoning']`，退出码 0

- [ ] **Step 3: 手动制造漂移验证会红**

临时把 `frontend/app/ask-modes.ts` 的 `graph` 那条注释掉，重跑：
Run: `PYTHONPATH=backend python scripts/check_ask_modes_contract.py; echo "exit=$?"`
Expected: `MISMATCH … only backend: ['graph']`，`exit=1`。**随后恢复 ask-modes.ts**。

- [ ] **Step 4: 接入 check.sh**

`scripts/check.sh`：在 `smoke_backend.py` 那行之后、前端块之前，加：

```bash
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" "$ROOT_DIR/scripts/check_ask_modes_contract.py"
```

- [ ] **Step 5: 提交**

```bash
git add scripts/check_ask_modes_contract.py scripts/check.sh
git commit -m "test(ci): ask-mode 跨端契约校验接入 check.sh"
```

---

## Task 10: 全量门禁 + 收尾

- [ ] **Step 1: 后端全量相关测试**

Run: `cd backend && python -m pytest tests/test_ask_modes.py tests/test_ask_modes_api.py tests/test_ask_mode_persist.py tests/test_kg_ready.py tests/test_ask_redesign.py tests/test_reasoning_stream.py tests/test_conversations.py -v`
Expected: 全 PASS

- [ ] **Step 2: 全量门禁**

Run: `bash scripts/check.sh`（必要时 `PYTHON_BIN=python3 bash scripts/check.sh`）
Expected: py_compile OK、smoke OK、`ask-mode contract OK`、前端 `node --test` 全绿、`tsc --noEmit` 无错。

- [ ] **Step 3: 残留字面量/引用扫描**

Run: `grep -rn "\"fast\"\|'fast'\|reasoningMode\|lastTurnUsedReasoning" frontend/app/ backend/app/api/ | grep -v ask_modes`
Expected: 无前端内联 mode 字面量、无残留 reasoningMode/启发式引用（后端 `ask_fast`/registry 内的 "fast" 属正常）。

- [ ] **Step 4: 手动四态走查（需重启后端，交用户）**

通用问答 / 严格推理-深挖 / 严格推理-图谱 / 无 KG 选严格推理（提示+send 拦截）四态行为正确；切会话后引擎精确恢复（reasoning vs graph 可区分）。

---

## Self-Review（写完计划后自查）

**Spec 覆盖**：§3 模式总览→Task1/6；§4.1 registry→Task1；§4.2 校验+/ask-modes→Task3；§4.3 端点统一→Task3/4；§4.4 前端 const+控件→Task6/7；§4.5 KG 门控→Task7/8；§4.6 持久化恢复→Task5/7；§4.7 文案→Task6；§10 P1-P6→Task1-9；§11 验证→Task10。全部有任务承接。

**已知边界/确认点（spec 标注，实现时核）**：
- `ask_graph` 无 `on_trace` → registry `streaming=False`，chunk/graph 同走 start→final（Task1 已固化为 False）。
- `ask_reasoning` 对 KG 的硬依赖：`requires_kg=True` 仅驱动前端门控；若后端 reasoning 实际不强依赖 KG，门控偏保守（可接受，宁紧勿误答）。
- `_save_answer` 存 `model_dump()` JSON → `AskResponse.mode` 透明落库/回流（Task5 已据 5291 重建路径验证）。
- 「构建 KG」CTA 实触发待 chunk-native P4，Task7 先提示文案 + send 门控，不伪造端点。

**类型一致**：后端 `mode`/`kg_ready` 字段名与前端 `AskModeId`/`kg_ready` 对齐；registry handler 名（`ask_fast`/`_ask_global`）与 `getattr(self, …)` 一致。
