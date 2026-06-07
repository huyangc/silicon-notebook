# 推理搜索独立模型配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让推理搜索（`mode=reasoning`）能用一组独立配置的 LLM（`REASONING_LLM_BASE_URL`/`_API_KEY`/`_MODEL`），与全局 `OPENAI_COMPAT_*` 解耦；未配齐时整体回退全局，行为与现状字节级等价。

**Architecture:** 把 `OpenAICompatibleClient` 由"写死读 `settings.openai_compat_*`"改为"可选覆盖参数、默认取全局"（向后兼容）。`SQLiteRepository` 持一个 `reasoning_llm_client` **属性**：配齐 `REASONING_LLM_*` 时返回独立 client，否则**动态**回退到当前 `self.llm_client`（含测试运行时替换的 fake）。推理路径 4 处调用点改读该属性，其余链路（抽取/fast 问答/followup/文章研究）一律不动。

**Tech Stack:** Python, pydantic-settings (`Settings`), openai SDK, FastAPI, pytest（`monkeypatch.setenv` + `SQLiteRepository(Settings())` 范式）。

**Spec:** `docs/superpowers/specs/2026-06-07-reasoning-llm-config-design.md`

---

## File Structure

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `backend/app/core/config.py` | Modify | 加 `reasoning_llm_base_url/_api_key/_model` 三字段 + `reasoning_llm_configured` 属性（Task 6 再加 `reasoning_llm_partially_configured`） |
| `backend/app/core/llm.py` | Modify | `OpenAICompatibleClient.__init__` 加可选覆盖参数；`configured`/`client()`/`chat_json()` 改读实例属性 |
| `backend/app/services/sqlite_repository.py` | Modify | `__init__` 建 `self._reasoning_llm_client`；加 `reasoning_llm_client` 属性；`_answer_reasoning`(:3279) 与 `ask_reasoning` 门控(:3333) 改读该属性 |
| `backend/app/services/reasoning_retrieval.py` | Modify | `plan()`/`reflect()` 内 `self.repo.llm_client` → `self.repo.reasoning_llm_client`（共 4 处引用） |
| `backend/app/api/routes.py` | Modify | `/health` 响应加 `reasoning_llm_configured` |
| `.env.example` | Modify | 在 `OPENAI_COMPAT_*` 块后加 `REASONING_LLM_*` 注释段 |
| `backend/tests/test_reasoning_llm_config.py` | Create | 本特性新测试（config / 回退 / 独立 / 路由 / health / 可选 WARN） |
| `backend/tests/test_llm_client.py` | Modify | 加"覆盖参数生效 / 默认回退全局"两条测试 |

**执行顺序依赖**：Task 1 独立 → Task 2 → Task 3（依赖 2 的 ctor 参数）→ Task 4（依赖 3 的属性）→ Task 5（依赖 1 的属性）→ Task 6（可选，依赖 1/3）。

**测试运行约定**：所有命令在 `backend/` 目录下用 `python -m pytest`（确保 `app` 包可导入）。

---

## Task 1: Settings 加 REASONING_LLM_* 字段与 configured 属性

**Files:**
- Modify: `backend/app/core/config.py`（字段插在 :37 之后，属性插在 `llm_configured` :152 之后）
- Modify: `.env.example`
- Test: `backend/tests/test_reasoning_llm_config.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_reasoning_llm_config.py`：

```python
"""推理搜索独立模型配置 (REASONING_LLM_*) 的回归测试。"""
import pytest
from app.core.config import Settings


def test_reasoning_llm_configured_true_when_all_set(monkeypatch):
    monkeypatch.setenv("REASONING_LLM_BASE_URL", "https://reason")
    monkeypatch.setenv("REASONING_LLM_API_KEY", "rk")
    monkeypatch.setenv("REASONING_LLM_MODEL", "reason-model")
    assert Settings().reasoning_llm_configured is True


def test_reasoning_llm_configured_false_when_partial(monkeypatch):
    monkeypatch.setenv("REASONING_LLM_BASE_URL", "https://reason")
    monkeypatch.delenv("REASONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("REASONING_LLM_MODEL", raising=False)
    assert Settings().reasoning_llm_configured is False


def test_reasoning_llm_configured_false_when_none(monkeypatch):
    monkeypatch.delenv("REASONING_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("REASONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("REASONING_LLM_MODEL", raising=False)
    assert Settings().reasoning_llm_configured is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py -v`
Expected: FAIL —`AttributeError: 'Settings' object has no attribute 'reasoning_llm_configured'`

- [ ] **Step 3: 加字段**

在 `backend/app/core/config.py` 第 37 行（`openai_compat_max_retries` 的 `)` 结束）之后插入：

```python

    # 推理搜索 (mode=reasoning) 专用 LLM 端点（可选）。三项全部非空时推理路径改用此
    # 模型，与全局 OPENAI_COMPAT_* 解耦；任一为空 → 整体回退全局。超时/重试沿用
    # reasoning_timeout_seconds / reasoning_max_retries，此处不另设。
    reasoning_llm_base_url: str = Field("", env="REASONING_LLM_BASE_URL")
    reasoning_llm_api_key: str = Field("", env="REASONING_LLM_API_KEY")
    reasoning_llm_model: str = Field("", env="REASONING_LLM_MODEL")
```

- [ ] **Step 4: 加 configured 属性**

在 `llm_configured` 属性之后（:152 `)` 与 `return` 块结束、`embedder_configured` 属性之前）插入：

```python

    @property
    def reasoning_llm_configured(self) -> bool:
        return bool(
            self.reasoning_llm_base_url
            and self.reasoning_llm_api_key
            and self.reasoning_llm_model
        )
```

- [ ] **Step 5: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: 更新 .env.example**

在 `.env.example` 的 `OPENAI_COMPAT_TIMEOUT_SECONDS=60` 行之后插入：

```

# 推理搜索 (mode=reasoning) 专用 LLM（可选）。三项全部填写时推理路径改用此模型，
# 与全局 OPENAI_COMPAT_* 解耦（典型：抽取走快模型、推理走更强的推理/thinking 模型）。
# 任一为空 → 整体回退到上面的 OPENAI_COMPAT_*。推理的超时/重试沿用代码中的
# reasoning 默认（REASONING_TIMEOUT_SECONDS / REASONING_MAX_RETRIES），此处不单列。
REASONING_LLM_BASE_URL=
REASONING_LLM_API_KEY=
REASONING_LLM_MODEL=
```

- [ ] **Step 7: 提交**

```bash
git add backend/app/core/config.py .env.example backend/tests/test_reasoning_llm_config.py
git commit -m "$(cat <<'EOF'
feat(config): 推理搜索独立模型配置 REASONING_LLM_*(base_url/key/model) + reasoning_llm_configured

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: OpenAICompatibleClient 参数化（可选覆盖、默认取全局）

**Files:**
- Modify: `backend/app/core/llm.py`（`__init__` :37-40；`configured` :42-44；`client()` :47/:63/:64；`chat_json()` model :95 与 attempts 内 :126 一带）
- Test: `backend/tests/test_llm_client.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_llm_client.py` 末尾追加：

```python
def test_override_params_win_over_global(monkeypatch):
    """显式覆盖参数应优先于全局 OPENAI_COMPAT_*，并驱动 configured/base_url/model。"""
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://global")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "gk")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "global-model")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    create = _FakeCreate([_Resp()])
    c = OpenAICompatibleClient(Settings(), base_url="https://reason",
                               api_key="rk", model="reason-model")
    assert c.configured is True
    assert c.base_url == "https://reason" and c.model == "reason-model"
    monkeypatch.setattr(c, "client", lambda: _FakeOpenAI(create))
    out = c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert out == '{"ok":1}'
    assert create.calls[0]["model"] == "reason-model"  # 发出的是覆盖后的 model


def test_default_params_fall_back_to_global(monkeypatch):
    """不传覆盖参数 → 三项取全局 OPENAI_COMPAT_*（向后兼容）。"""
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://global")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "gk")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "global-model")
    c = OpenAICompatibleClient(Settings())
    assert c.base_url == "https://global"
    assert c.api_key == "gk"
    assert c.model == "global-model"
    assert c.configured is True
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_llm_client.py::test_override_params_win_over_global -v`
Expected: FAIL —`TypeError: __init__() got an unexpected keyword argument 'base_url'`

- [ ] **Step 3: 改 `__init__`**

`backend/app/core/llm.py` 把：

```python
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[OpenAI] = None
        self.interaction_logger = LLMInteractionLogger(settings)
```

替换为：

```python
    def __init__(self, settings: Settings, *, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, model: Optional[str] = None,
                 max_retries: Optional[int] = None):
        self.settings = settings
        # 默认取全局 openai_compat_*；显式传入则覆盖（推理专用 client 走此路）。
        self.base_url = base_url if base_url is not None else settings.openai_compat_base_url
        self.api_key = api_key if api_key is not None else settings.openai_compat_api_key
        self.model = model if model is not None else settings.openai_compat_model
        self.max_retries = (max_retries if max_retries is not None
                            else settings.openai_compat_max_retries)
        self._client: Optional[OpenAI] = None
        self.interaction_logger = LLMInteractionLogger(settings)
```

- [ ] **Step 4: 改 `configured` 属性**

把：

```python
    @property
    def configured(self) -> bool:
        return self.settings.llm_configured
```

替换为：

```python
    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)
```

- [ ] **Step 5: 改 `client()` 读实例属性**

把第 47 行：

```python
        if not (self.settings.openai_compat_base_url and self.settings.openai_compat_api_key):
```

替换为：

```python
        if not (self.base_url and self.api_key):
```

把第 63-64 行：

```python
                api_key=self.settings.openai_compat_api_key,
                base_url=self.settings.openai_compat_base_url,
```

替换为：

```python
                api_key=self.api_key,
                base_url=self.base_url,
```

（注意：`timeout = self.settings.openai_compat_timeout_seconds` 与连接池大小逻辑**不改**——推理 client 的 per-call timeout 由调用方传入，client 级默认沿用全局即可。）

- [ ] **Step 6: 改 `chat_json()` 读实例属性**

把第 95 行：

```python
        model = self.settings.openai_compat_model
```

替换为：

```python
        model = self.model
```

把 attempts 计算里的：

```python
                max_retries if max_retries is not None
                else self.settings.openai_compat_max_retries
```

替换为：

```python
                max_retries if max_retries is not None
                else self.max_retries
```

- [ ] **Step 7: 运行新测试 + 全量 client 回归**

Run: `cd backend && python -m pytest tests/test_llm_client.py -v`
Expected: PASS（含原有 13 条 + 新增 2 条；证明改造对既有 fail-fast/retry/timeout 行为零影响）

- [ ] **Step 8: 提交**

```bash
git add backend/app/core/llm.py backend/tests/test_llm_client.py
git commit -m "$(cat <<'EOF'
refactor(llm): OpenAICompatibleClient 参数化(可选覆盖 base_url/key/model/retries, 默认取全局)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: SQLiteRepository.reasoning_llm_client 属性（动态回退）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`__init__` :138 后建 `_reasoning_llm_client`；`__init__` 结束 :149 后加属性）
- Test: `backend/tests/test_reasoning_llm_config.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_reasoning_llm_config.py` 顶部 import 区补充，并追加测试：

```python
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("REASONING_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("REASONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("REASONING_LLM_MODEL", raising=False)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_reasoning_client_is_llm_client_when_unconfigured(repo):
    # 未配 REASONING_LLM_* → 推理 client 即全局 client（同一对象）。
    assert repo.reasoning_llm_client is repo.llm_client


def test_reasoning_client_follows_llm_client_reassignment(repo):
    # 未配置时回退是动态的：运行时替换 llm_client（既有推理测试就这么注入），
    # 推理 client 必须跟随——这正是既有推理测试零改动保持绿的保证。
    sentinel = object()
    repo.llm_client = sentinel
    assert repo.reasoning_llm_client is sentinel


def test_reasoning_client_distinct_and_uses_reasoning_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("REASONING_LLM_BASE_URL", "https://reason")
    monkeypatch.setenv("REASONING_LLM_API_KEY", "rk")
    monkeypatch.setenv("REASONING_LLM_MODEL", "reason-model")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    assert r.reasoning_llm_client is not r.llm_client
    assert r.reasoning_llm_client.base_url == "https://reason"
    assert r.reasoning_llm_client.model == "reason-model"
    assert r.reasoning_llm_client.configured is True
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py -k reasoning_client -v`
Expected: FAIL —`AttributeError: 'SQLiteRepository' object has no attribute 'reasoning_llm_client'`

- [ ] **Step 3: `__init__` 建 _reasoning_llm_client**

`backend/app/services/sqlite_repository.py` 把第 138 行：

```python
        self.llm_client = OpenAICompatibleClient(settings)
```

替换为：

```python
        self.llm_client = OpenAICompatibleClient(settings)
        # 推理搜索专用 client：配齐 REASONING_LLM_* → 独立模型实例；否则 None，
        # 由 reasoning_llm_client 属性动态回退到 self.llm_client。
        self._reasoning_llm_client = (
            OpenAICompatibleClient(
                settings,
                base_url=settings.reasoning_llm_base_url,
                api_key=settings.reasoning_llm_api_key,
                model=settings.reasoning_llm_model,
            )
            if settings.reasoning_llm_configured
            else None
        )
```

- [ ] **Step 4: 加 reasoning_llm_client 属性**

在 `__init__` 结束（:149 `self._seed()`）之后、`_resolve_path`（:151）之前插入：

```python

    @property
    def reasoning_llm_client(self):
        """推理路径专用 LLM client。配齐 REASONING_LLM_* → 独立模型；否则动态回退到
        当前 self.llm_client（含测试运行时替换的 fake），未配置时与全局行为完全一致。"""
        if self._reasoning_llm_client is not None:
            return self._reasoning_llm_client
        return self.llm_client
```

- [ ] **Step 5: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py -v`
Expected: PASS（Task 1 的 3 条 + 本任务 3 条）

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_reasoning_llm_config.py
git commit -m "$(cat <<'EOF'
feat(repo): SQLiteRepository.reasoning_llm_client 属性(配齐则独立模型, 否则动态回退全局)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 推理路径 4 处调用点切到 reasoning_llm_client

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`（`plan()` :93/:96，`reflect()` :123/:126）
- Modify: `backend/app/services/sqlite_repository.py`（`_answer_reasoning` :3279，`ask_reasoning` 门控 :3333）
- Test: `backend/tests/test_reasoning_llm_config.py`

- [ ] **Step 1: 写失败测试（路由断言）**

在 `backend/tests/test_reasoning_llm_config.py` 顶部 import 区补充，并追加测试：

```python
from app.models.schemas import NotebookCreate, AskRequest
import json


class _SeqLLM:
    """按 schema_hint 顺序返回预置 JSON，并记录调用次数。"""
    configured = True
    def __init__(self, plan, reflects, answer):
        self._plan, self._reflects, self._answer = plan, list(reflects), answer
        self.calls = 0
    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls += 1
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps(self._answer)


def test_reasoning_path_routes_through_reasoning_client(repo):
    # 注入"独立推理 client"为记录型 fake；全局 llm_client 设为一调用即爆，
    # 证明推理路径全程只走 reasoning_llm_client、绝不碰全局 client。
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    ], [])
    reasoning_llm = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "答案 [k1].", "grounded": True})

    class _BoomLLM:
        configured = True
        def chat_json(self, *a, **k):
            raise AssertionError("reasoning 路径不得使用全局 llm_client")

    repo.llm_client = _BoomLLM()
    repo._reasoning_llm_client = reasoning_llm   # 模拟已配置独立推理模型
    resp = repo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.answer.startswith("答案")
    assert reasoning_llm.calls >= 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py::test_reasoning_path_routes_through_reasoning_client -v`
Expected: FAIL — 改前推理仍走全局 `_BoomLLM`，其异常被各层 try/except 吞掉 → `reasoning_llm.calls == 0` 且 `resp.answer == ""`，两条断言均失败。

- [ ] **Step 3: 改 reasoning_retrieval.py（plan/reflect 共 4 处）**

`backend/app/services/reasoning_retrieval.py` 中 `self.repo.llm_client` 在 `plan()`/`reflect()` 出现 4 次，全部改为 `self.repo.reasoning_llm_client`。用两次"全替换"：

替换全部出现的：

```python
        if not getattr(self.repo.llm_client, "configured", False):
```

为：

```python
        if not getattr(self.repo.reasoning_llm_client, "configured", False):
```

替换全部出现的：

```python
            raw = self.repo.llm_client.chat_json(
```

为：

```python
            raw = self.repo.reasoning_llm_client.chat_json(
```

（两处文本各出现 2 次，分别命中 `plan()` 与 `reflect()`。）

- [ ] **Step 4: 改 sqlite_repository.py（_answer_reasoning + ask_reasoning 门控）**

把第 3279 行：

```python
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
```

替换为：

```python
        raw = self.reasoning_llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
```

把第 3333 行：

```python
        if self.llm_client.configured and (top_hits or elements):
```

替换为：

```python
        if self.reasoning_llm_client.configured and (top_hits or elements):
```

- [ ] **Step 5: 运行新测试 + 既有推理全套回归**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py tests/test_reasoning_ask.py tests/test_reasoning_retrieval.py tests/test_reasoning_stream.py -v`
Expected: PASS — 新路由测试通过；既有推理测试（靠未配置时 `reasoning_llm_client` 动态回退到被重赋的 `llm_client`）全绿、零改动。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/reasoning_retrieval.py backend/app/services/sqlite_repository.py backend/tests/test_reasoning_llm_config.py
git commit -m "$(cat <<'EOF'
feat(reasoning): 推理路径 plan/reflect/answer/门控 改走 reasoning_llm_client

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: /health 暴露 reasoning_llm_configured

**Files:**
- Modify: `backend/app/api/routes.py`（`health()` :84-89）
- Test: `backend/tests/test_reasoning_llm_config.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_reasoning_llm_config.py` 追加：

```python
def test_health_exposes_reasoning_llm_configured():
    from app.api.routes import health
    from app.core.config import get_settings
    get_settings.cache_clear()
    body = health()
    assert "reasoning_llm_configured" in body
    # 与 settings 口径一致（对环境是否配置鲁棒）。
    assert body["reasoning_llm_configured"] == get_settings().reasoning_llm_configured
    get_settings.cache_clear()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py::test_health_exposes_reasoning_llm_configured -v`
Expected: FAIL —`assert 'reasoning_llm_configured' in {...}`（键缺失）

- [ ] **Step 3: 改 health()**

`backend/app/api/routes.py` 把：

```python
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_configured": settings.llm_configured,
        "embedding_configured": settings.embedder_configured,
    }
```

替换为：

```python
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_configured": settings.llm_configured,
        "reasoning_llm_configured": settings.reasoning_llm_configured,
        "embedding_configured": settings.embedder_configured,
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py::test_health_exposes_reasoning_llm_configured -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_reasoning_llm_config.py
git commit -m "$(cat <<'EOF'
feat(health): /health 增 reasoning_llm_configured 字段

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6 (可选)：REASONING_LLM_* 部分配置时 WARN

> 可砍。仅在用户希望"配漏时有提示日志"时做。沿用 `self.event_log.logger`（项目既有日志通道），故须放在 `self.event_log` 创建（:142）之后。

**Files:**
- Modify: `backend/app/core/config.py`（加 `reasoning_llm_partially_configured` 属性）
- Modify: `backend/app/services/sqlite_repository.py`（`__init__` :142 后加 WARN）
- Test: `backend/tests/test_reasoning_llm_config.py`

- [ ] **Step 1: 写失败测试**

追加：

```python
def test_partial_reasoning_config_warns_and_falls_back(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("REASONING_LLM_BASE_URL", "https://reason")  # 只填 1/3
    monkeypatch.delenv("REASONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("REASONING_LLM_MODEL", raising=False)
    with caplog.at_level(logging.WARNING):
        r = SQLiteRepository(Settings())
        r.embedder = FakeEmbedder(dim=16)
    assert any("REASONING_LLM" in rec.message for rec in caplog.records)
    assert r.reasoning_llm_client is r.llm_client   # 部分配置 → 仍整体回退
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py::test_partial_reasoning_config_warns_and_falls_back -v`
Expected: FAIL — 无 WARN 记录 → `assert any(...)` 失败。

- [ ] **Step 3: 加 partial 属性**

在 `config.py` 的 `reasoning_llm_configured` 属性之后插入：

```python

    @property
    def reasoning_llm_partially_configured(self) -> bool:
        """有些 REASONING_LLM_* 填了但非全填（疑似配漏，将整体回退全局）。"""
        vals = [self.reasoning_llm_base_url, self.reasoning_llm_api_key, self.reasoning_llm_model]
        return any(vals) and not all(vals)
```

- [ ] **Step 4: 加 WARN**

`backend/app/services/sqlite_repository.py` 把第 142 行：

```python
        self.event_log = EventLogger(settings, channel="events")
```

替换为：

```python
        self.event_log = EventLogger(settings, channel="events")
        if settings.reasoning_llm_partially_configured:
            self.event_log.logger.warning(
                "REASONING_LLM_* 仅部分配置(base_url/api_key/model 需全填)，"
                "推理搜索将回退到全局 OPENAI_COMPAT_* 模型。")
```

- [ ] **Step 5: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py -v`
Expected: PASS（全文件）

- [ ] **Step 6: 提交**

```bash
git add backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_reasoning_llm_config.py
git commit -m "$(cat <<'EOF'
feat(config): REASONING_LLM_* 部分配置时 WARN 提示(回退全局)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## 收尾：全量回归 + 提 PR

- [ ] **全量后端测试**

Run: `cd backend && python -m pytest -q`
Expected: 全绿（重点确认 `test_llm_client` / `test_reasoning_*` / `test_reasoning_llm_config` 全通过，其余链路无回归）。

- [ ] **手测验收（按 spec §7）**

1. 不配 `REASONING_LLM_*` 起服务 → `/health` 的 `reasoning_llm_configured=false`，推理问答正常（走全局模型）。
2. 配齐 `REASONING_LLM_*` 起服务 → `/health` 该字段 `true`；发一条 `mode=reasoning` 提问，查 `.local/logs/llm.jsonl`：推理三类调用（plan/reflect/answer）的 `model` 为 `REASONING_LLM_MODEL`；同时做一次抽取或 fast 问答，其 `model` 仍为 `OPENAI_COMPAT_MODEL`。

- [ ] **提 PR**（按用户开发流程约定：先 3-way 并 master → push → gh pr create --base master）

```bash
git fetch origin && git merge --no-ff origin/master   # 3-way 并入最新 master
git push -u origin reasoning-llm-config
gh pr create --base master --title "feat: 推理搜索独立模型配置 (REASONING_LLM_*)" --body "$(cat <<'EOF'
## 摘要
- 推理搜索 (mode=reasoning) 可用独立 LLM（REASONING_LLM_BASE_URL/_API_KEY/_MODEL），与全局 OPENAI_COMPAT_* 解耦
- 未配齐时整体回退全局，既有行为字节级等价；/health 增 reasoning_llm_configured

## 测试
- backend: python -m pytest -q 全绿（新增 test_reasoning_llm_config.py + test_llm_client 覆盖参数两条）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review（写plan后自查）

- **Spec 覆盖**：§2 配置→Task 1；§3 客户端参数化→Task 2；§4.1 双 client→Task 3、§4.2 四处切换→Task 4；§5 /health→Task 5、§5 可选 WARN→Task 6；§6 测试散落各 Task；§7 验收→收尾节。全覆盖。
- **占位符**：无 TBD/TODO；每个改动均给出确切 old/new 代码块与命令。
- **类型/命名一致**：`reasoning_llm_configured`、`reasoning_llm_client`、`_reasoning_llm_client`、`reasoning_llm_base_url/_api_key/_model`、`reasoning_llm_partially_configured` 全文一致；`OpenAICompatibleClient` 覆盖参数名 `base_url/api_key/model/max_retries` 与 Task 2/Task 3 调用一致。
- **回退正确性**：未配置 → `_reasoning_llm_client is None` → 属性返回 `self.llm_client`（动态，跟随测试重赋）；既有推理测试零改动保持绿（Task 4 Step 5 验证）。
