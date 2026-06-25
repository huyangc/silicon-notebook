# Per-user 模型服务配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让登录用户配置自己的模型服务（主/推理/改写/构图 LLM + rerank），不配则回退系统 env 默认；一个开关即可切到「不配就用不了」。

**Architecture:** 用户配置以 JSON 存 `user_profiles.model_settings`（明文，API 层只写不回显）。一个 `resolve_model_config(user, role)` 把用户配置叠加在 env 默认上，按 `USER_MODEL_CONFIG_POLICY` 决定缺配回退 env(`fallback`)还是判 `none`(`required`)。仓库的 client 访问器（`llm_client` 等 property）改为按当前用户（ContextVar）解析，按配置指纹缓存 client；env 单例保留为系统默认（且保持测试可替换）。KG 后台 job 经 `copy_context` 拿到真实用户。

**Tech Stack:** FastAPI + pydantic-settings v2 + sqlite3；Next.js/React(app/page.tsx) + fetch；pytest；`node --test`。

参考设计文档：[docs/superpowers/specs/2026-06-25-per-user-model-config-design.md](../specs/2026-06-25-per-user-model-config-design.md)

---

## 关键约定（贯穿全篇）

- 后端根：`backend/`。测试目录 `backend/tests/`，跑 `cd backend && python -m pytest`。
- 5 个角色键：`llm` / `reasoning_llm` / `rewrite_llm` / `kg_llm` / `rerank`。
- `model_settings` JSON 形状（每服务 3 字段，空串=回退）：
  ```json
  {"llm":{"base_url":"","api_key":"","model":""}, "reasoning_llm":{...}, "rewrite_llm":{...}, "kg_llm":{...}, "rerank":{...}}
  ```
- pydantic v2 新环境变量一律用 `validation_alias`（`Field(env=...)` 在本仓库 v2 下失效）。

---

## Task 1: Settings 开关 + `model_settings` 存储列与读写

**Files:**
- Modify: `backend/app/core/config.py:27`（在 `auth_optional` 后加 policy 字段）
- Modify: `backend/app/services/sqlite_repository.py:709`（`_migrate` 守卫式 ALTER）
- Modify: `backend/app/services/sqlite_repository.py:917`（在 `_user_profile` 后加读写方法 + 缓存字段）
- Test: `backend/tests/test_user_model_settings_store.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_user_model_settings_store.py
import json
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st")})
    repo = SQLiteRepository(s)
    repo._migrate(); repo._seed()
    return repo


def test_model_settings_default_empty(tmp_path):
    repo = _repo(tmp_path)
    assert repo.get_user_model_settings("user-local") == {}


def test_model_settings_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    cfg = {"llm": {"base_url": "https://u.example/v1", "api_key": "sk-u", "model": "m-u"}}
    repo.set_user_model_settings("user-local", cfg)
    assert repo.get_user_model_settings("user-local") == cfg
    # 二次读走缓存也一致
    assert repo.get_user_model_settings("user-local")["llm"]["model"] == "m-u"


def test_policy_default_is_fallback():
    assert get_settings().user_model_config_policy == "fallback"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_user_model_settings_store.py -q`
Expected: FAIL（`user_model_config_policy` 不存在 / `get_user_model_settings` 未定义）

- [ ] **Step 3: 加 policy 字段**

在 `backend/app/core/config.py` 第 27 行 `auth_optional` 字段之后插入：

```python
    # 每用户模型配置策略。"fallback"(第一阶段)=用户没配则回退系统 env 默认；
    # "required"(第二阶段)=用户没配则该服务不可用(解析为 none，经 model_error 通道提示)。
    user_model_config_policy: str = Field("fallback", validation_alias="USER_MODEL_CONFIG_POLICY")
```

- [ ] **Step 4: 加迁移列**

在 `backend/app/services/sqlite_repository.py` 的 `_migrate` 内、用户名/密码 ALTER 块之后（第 709 行 `CREATE UNIQUE INDEX ... idx_users_username` 之后）插入：

```python
            # 每用户模型服务配置(JSON;明文存,API 层只写不回显)。
            prof_cols = {r["name"] for r in db.execute("PRAGMA table_info(user_profiles)").fetchall()}
            if "model_settings" not in prof_cols:
                db.execute("ALTER TABLE user_profiles ADD COLUMN model_settings TEXT NOT NULL DEFAULT '{}'")
```

- [ ] **Step 5: 加读写方法 + 缓存**

在 `backend/app/services/sqlite_repository.py` `__init__` 末尾（第 239 行 `self._unified_cache` 附近）加一个缓存字段：

```python
        self._user_model_cfg_cache: Dict[str, dict] = {}
```

在 `_user_profile`（第 917 行）方法之后插入：

```python
    def get_user_model_settings(self, user_id: str) -> dict:
        """读用户的模型服务配置 JSON(含明文 key;仅供服务端解析,绝不回前端)。进程内缓存。"""
        cached = self._user_model_cfg_cache.get(user_id)
        if cached is not None:
            return cached
        with self._connect() as db:
            row = db.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
        try:
            parsed = json.loads(row["model_settings"]) if row and row["model_settings"] else {}
        except (ValueError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        self._user_model_cfg_cache[user_id] = parsed
        return parsed

    def set_user_model_settings(self, user_id: str, settings: dict) -> None:
        """覆盖写用户模型配置并失效缓存(下次解析重建 client)。"""
        with self._write() as db:
            db.execute(
                "UPDATE user_profiles SET model_settings = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(settings, ensure_ascii=False), _now(), user_id),
            )
        self._user_model_cfg_cache.pop(user_id, None)
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_user_model_settings_store.py -q`
Expected: PASS（3 passed）

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_user_model_settings_store.py
git commit -m "feat(model-config): user_profiles.model_settings 存储 + policy 开关"
```

---

## Task 2: 解析器 + 类型 + 两层回退

**Files:**
- Create: `backend/app/services/model_config.py`
- Modify: `backend/app/services/sqlite_repository.py:917`（加 `resolve_model_config` 方法）
- Test: `backend/tests/test_model_config_resolve.py`

- [ ] **Step 1: 写失败测试（纯函数 + 仓库方法）**

```python
# backend/tests/test_model_config_resolve.py
from app.services.model_config import resolve_effective_config, ResolvedModelConfig

U = {"llm": {"base_url": "https://u/v1", "api_key": "sk-u", "model": "m"}}
VAR = {"reasoning_llm": {"base_url": "https://r/v1", "api_key": "sk-r", "model": "rm"}}


def test_user_full_config_wins():
    r = resolve_effective_config(U, "llm", "fallback")
    assert (r.base_url, r.api_key, r.model, r.source) == ("https://u/v1", "sk-u", "m", "user")


def test_variant_falls_back_to_user_primary():
    r = resolve_effective_config(U, "kg_llm", "fallback")
    assert r.source == "user" and r.model == "m"   # 用了用户自己的主 LLM


def test_variant_own_config_wins_over_primary():
    r = resolve_effective_config({**U, **VAR}, "reasoning_llm", "fallback")
    assert r.source == "user" and r.model == "rm"


def test_unconfigured_fallback_to_system():
    r = resolve_effective_config({}, "llm", "fallback")
    assert r.source == "system" and r.base_url == ""


def test_unconfigured_required_is_none():
    r = resolve_effective_config({}, "llm", "required")
    assert r.source == "none"


def test_rerank_has_no_variant_fallback():
    # rerank 没有"变体回退主 LLM"，没配就走 policy
    assert resolve_effective_config(U, "rerank", "fallback").source == "system"
    assert resolve_effective_config(U, "rerank", "required").source == "none"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_model_config_resolve.py -q`
Expected: FAIL（`app.services.model_config` 不存在）

- [ ] **Step 3: 实现解析器模块**

```python
# backend/app/services/model_config.py
"""每用户模型服务配置解析。纯函数 resolve_effective_config 便于单测；仓库侧
resolve_model_config 注入用户的 model_settings + 全局 policy。"""
from __future__ import annotations
from dataclasses import dataclass

LLM_VARIANTS = ("reasoning_llm", "rewrite_llm", "kg_llm")


class ModelNotConfiguredError(RuntimeError):
    """policy=required 且用户未配置该服务时抛出，经 model_error 通道提示用户。"""


@dataclass(frozen=True)
class ResolvedModelConfig:
    base_url: str
    api_key: str
    model: str
    source: str   # "user" | "system" | "none"


def _full(svc: dict) -> bool:
    return bool(svc.get("base_url") and svc.get("api_key") and svc.get("model"))


def resolve_effective_config(model_settings: dict, role: str, policy: str) -> ResolvedModelConfig:
    svc = (model_settings or {}).get(role) or {}
    if _full(svc):
        return ResolvedModelConfig(svc["base_url"], svc["api_key"], svc["model"], "user")
    # 第 1 层：变体 LLM 未配 → 回退到用户自己的主 LLM
    if role in LLM_VARIANTS:
        primary = (model_settings or {}).get("llm") or {}
        if _full(primary):
            return ResolvedModelConfig(primary["base_url"], primary["api_key"], primary["model"], "user")
    # 第 2 层：用户没配 → 按 policy
    if policy == "required":
        return ResolvedModelConfig("", "", "", "none")
    return ResolvedModelConfig("", "", "", "system")
```

- [ ] **Step 4: 仓库注入方法**

在 `backend/app/services/sqlite_repository.py` 顶部 import 区加（与其它 `from app.services...` 同处）：

```python
from app.services.model_config import resolve_effective_config, ResolvedModelConfig, ModelNotConfiguredError
```

在 `set_user_model_settings`（Task 1 加的）之后插入：

```python
    def resolve_model_config(self, user, role: str) -> ResolvedModelConfig:
        return resolve_effective_config(
            self.get_user_model_settings(user.id), role, self.settings.user_model_config_policy)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_model_config_resolve.py -q`
Expected: PASS（6 passed）

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/model_config.py backend/app/services/sqlite_repository.py backend/tests/test_model_config_resolve.py
git commit -m "feat(model-config): resolve_model_config 两层回退 + 类型"
```

---

## Task 3: LLM client 访问器改按用户解析 + 缓存 + 未配置哨兵

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:191`（`__init__` 改名 `_system_llm_client`）
- Modify: `backend/app/services/sqlite_repository.py:245-267`（改写 4 个 property + setter + 新辅助）
- Test: `backend/tests/test_user_llm_client_resolve.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_user_llm_client_resolve.py
import pytest
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository, set_request_user, reset_request_user


def _repo(tmp_path, **over):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st"), **over})
    repo = SQLiteRepository(s); repo._migrate(); repo._seed()
    return repo


def test_user_config_drives_llm_client(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings("user-local",
        {"llm": {"base_url": "https://u/v1", "api_key": "sk-u", "model": "m-u"}})
    user = repo.current_user()
    tok = set_request_user(user)
    try:
        c = repo.llm_client
        assert c.base_url == "https://u/v1" and c.model == "m-u"
    finally:
        reset_request_user(tok)


def test_no_user_config_fallback_to_system_and_setter(tmp_path):
    repo = _repo(tmp_path)
    sentinel = object()
    repo.llm_client = sentinel   # setter 设系统默认(测试替身)
    assert repo.llm_client is sentinel   # 无用户配置 → 系统默认


def test_variant_uses_user_primary(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings("user-local",
        {"llm": {"base_url": "https://u/v1", "api_key": "sk-u", "model": "m-u"}})
    tok = set_request_user(repo.current_user())
    try:
        assert repo.kg_llm_client.model == "m-u"   # kg 未配 → 用户主 LLM
    finally:
        reset_request_user(tok)


def test_required_policy_unconfigured_is_sentinel(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    tok = set_request_user(repo.current_user())
    try:
        assert repo.llm_client.configured is False   # 未配 + required → 哨兵
    finally:
        reset_request_user(tok)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_user_llm_client_resolve.py -q`
Expected: FAIL（`repo.llm_client = sentinel` 触发 AttributeError，或 base_url 不匹配）

- [ ] **Step 3: `__init__` 改名 + 缓存/哨兵字段**

把 `backend/app/services/sqlite_repository.py:191`：
```python
        self.llm_client = OpenAICompatibleClient(settings)
```
改为：
```python
        self._system_llm_client = OpenAICompatibleClient(settings)
        self._user_llm_clients: Dict[str, OpenAICompatibleClient] = {}
```

- [ ] **Step 4: 加哨兵类（模块级，放在 `class SQLiteRepository` 之前，第 184 行附近）**

```python
class _UnconfiguredLLMClient:
    """policy=required 且用户未配置时的占位 client：configured=False 让调用点跳过；
    若硬调 chat_json 则抛 ModelNotConfiguredError。"""
    configured = False
    base_url = ""
    api_key = ""
    model = ""

    def chat_json(self, *a, **k):
        raise ModelNotConfiguredError("请先在设置中配置你的模型服务")


_UNCONFIGURED_LLM = _UnconfiguredLLMClient()
```

- [ ] **Step 5: 改写 4 个 property（替换第 245-267 行整块）**

```python
    def _system_llm_for(self, role: str):
        if role == "reasoning_llm":
            return self._reasoning_llm_client or self._system_llm_client
        if role == "rewrite_llm":
            return self._rewrite_llm_client or self._system_llm_client
        if role == "kg_llm":
            return self._kg_llm_client or self._system_llm_client
        return self._system_llm_client

    def _user_llm_cached(self, cfg: ResolvedModelConfig):
        fp = f"{cfg.base_url}|{cfg.api_key}|{cfg.model}"
        client = self._user_llm_clients.get(fp)
        if client is None:
            client = OpenAICompatibleClient(
                self.settings, base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model)
            self._user_llm_clients[fp] = client
        return client

    def _llm_for_role(self, role: str):
        cfg = self.resolve_model_config(self.current_user(), role)
        if cfg.source == "user":
            return self._user_llm_cached(cfg)
        if cfg.source == "none":
            return _UNCONFIGURED_LLM
        return self._system_llm_for(role)

    @property
    def llm_client(self):
        return self._llm_for_role("llm")

    @llm_client.setter
    def llm_client(self, client):
        # 测试/运行时替换系统默认主 LLM(无用户配置时即此 client)。
        self._system_llm_client = client

    @property
    def reasoning_llm_client(self):
        return self._llm_for_role("reasoning_llm")

    @property
    def rewrite_llm_client(self):
        return self._llm_for_role("rewrite_llm")

    @property
    def kg_llm_client(self):
        return self._llm_for_role("kg_llm")
```

- [ ] **Step 6: 跑新测试 + 全量回归**

Run: `cd backend && python -m pytest tests/test_user_llm_client_resolve.py -q`
Expected: PASS（4 passed）

Run: `cd backend && python -m pytest tests/test_reasoning_llm_config.py -q`
Expected: PASS（回归：变体回退语义未破）

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_user_llm_client_resolve.py
git commit -m "feat(model-config): LLM client 按用户解析+缓存+未配置哨兵(保留 setter)"
```

---

## Task 4: Rerank client 支持覆盖参数 + 按用户解析

**Files:**
- Modify: `backend/app/services/rerank_client.py:13-18`（构造加覆盖参数）
- Modify: `backend/app/services/sqlite_repository.py:229`（改名 `_system_rerank_client` + property）
- Test: `backend/tests/test_user_rerank_resolve.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_user_rerank_resolve.py
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository, set_request_user, reset_request_user
from app.services.rerank_client import RerankClient


def _repo(tmp_path, **over):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st"), **over})
    repo = SQLiteRepository(s); repo._migrate(); repo._seed()
    return repo


def test_rerank_overrides():
    c = RerankClient(get_settings(), model="rm", base_url="https://r/v1/", api_key="sk-r")
    assert c.model == "rm" and c.base_url == "https://r/v1" and c.api_key == "sk-r"
    assert c.configured is True


def test_user_rerank_drives_client(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings("user-local",
        {"rerank": {"base_url": "https://r/v1", "api_key": "sk-r", "model": "rm"}})
    tok = set_request_user(repo.current_user())
    try:
        assert repo.rerank_client.base_url == "https://r/v1" and repo.rerank_client.model == "rm"
    finally:
        reset_request_user(tok)


def test_required_policy_rerank_disabled(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    tok = set_request_user(repo.current_user())
    try:
        assert repo.rerank_client.configured is False   # 未配 + required → 禁用(原序)
    finally:
        reset_request_user(tok)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_user_rerank_resolve.py -q`
Expected: FAIL（RerankClient 不接受 model= 等 kwargs）

- [ ] **Step 3: RerankClient 加覆盖参数**

替换 `backend/app/services/rerank_client.py:13-18` 的 `__init__`：

```python
    def __init__(self, settings, *, model=None, base_url=None, api_key=None, max_docs=None):
        self.settings = settings
        self.model = ((model if model is not None else getattr(settings, "rerank_model", "")) or "").strip()
        self.base_url = ((base_url if base_url is not None else getattr(settings, "rerank_base_url", "")) or "").rstrip("/")
        self.api_key = (api_key if api_key is not None else getattr(settings, "rerank_api_key", "")) or ""
        self.max_docs = max(1, max_docs if max_docs is not None else getattr(settings, "rerank_max_docs", 500))
```

- [ ] **Step 4: 仓库 rerank property**

把 `backend/app/services/sqlite_repository.py:228-229`：
```python
        from app.services.rerank_client import RerankClient
        self.rerank_client = RerankClient(settings)
```
改为：
```python
        from app.services.rerank_client import RerankClient
        self._system_rerank_client = RerankClient(settings)
        self._user_rerank_clients: Dict[str, RerankClient] = {}
```

在 `kg_llm_client` property（Task 3 改写块）之后插入：

```python
    @property
    def rerank_client(self):
        from app.services.rerank_client import RerankClient
        cfg = self.resolve_model_config(self.current_user(), "rerank")
        if cfg.source == "user":
            fp = f"{cfg.base_url}|{cfg.api_key}|{cfg.model}"
            client = self._user_rerank_clients.get(fp)
            if client is None:
                client = RerankClient(self.settings, model=cfg.model,
                                      base_url=cfg.base_url, api_key=cfg.api_key)
                self._user_rerank_clients[fp] = client
            return client
        if cfg.source == "none":
            return RerankClient(self.settings, model="", base_url="", api_key="")  # configured=False → 原序
        return self._system_rerank_client

    @rerank_client.setter
    def rerank_client(self, client):
        self._system_rerank_client = client
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_user_rerank_resolve.py -q`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/rerank_client.py backend/app/services/sqlite_repository.py backend/tests/test_user_rerank_resolve.py
git commit -m "feat(model-config): rerank 支持覆盖参数 + 按用户解析"
```

---

## Task 5: KG 后台 job 经 copy_context 传播当前用户

**Files:**
- Modify: `backend/app/services/kg/scheduler.py:15`（import）与 `:55-62`（`submit_job` 包 context）
- Test: `backend/tests/test_kg_job_user_context.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_kg_job_user_context.py
from app.services.kg import scheduler
from app.services.sqlite_repository import _REQUEST_USER


def test_submit_job_propagates_contextvar():
    scheduler.reset()
    token = _REQUEST_USER.set("USER-X")   # 用裸值即可验证传播
    try:
        fut = scheduler.submit_job(lambda: _REQUEST_USER.get())
        assert fut.result(timeout=5) == "USER-X"
    finally:
        _REQUEST_USER.reset(token)
        scheduler.reset()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_kg_job_user_context.py -q`
Expected: FAIL（worker 线程未传播 → 取到 default `None`）

- [ ] **Step 3: scheduler import + 包裹**

在 `backend/app/services/kg/scheduler.py` 第 15 行 `import concurrent.futures as cf` 后加：
```python
import contextvars
```

把 `submit_job`（第 55-62 行）函数体改为：
```python
def submit_job(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> cf.Future:
    """Submit one document-extraction job to the job pool (fire-and-forget;
    callee handles its own errors/status). 在提交线程(请求线程)抓取 ContextVar
    快照并在 worker 内重放，使后台 job 的 current_user() 拿到真实用户(每用户 KG_LLM)。"""
    _ensure()
    ctx = contextvars.copy_context()
    fut = _job_pool.submit(ctx.run, fn, *args, **kwargs)
    fut.add_done_callback(_log_job_exception)
    return fut
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_kg_job_user_context.py -q`
Expected: PASS（1 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg/scheduler.py backend/tests/test_kg_job_user_context.py
git commit -m "feat(model-config): KG 后台 job 经 copy_context 传播当前用户"
```

---

## Task 6: 第二阶段守卫——主 LLM 未配置时 ask 经 model_error 提示

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:5585`（sink 建好后插守卫）
- Test: `backend/tests/test_ask_requires_model_config.py`

> 说明：`fallback`(第一阶段默认)下 source 永不为 `none`，此守卫不触发；`required` 下触发。本任务让开关翻到第二阶段时有清晰提示，第一阶段零影响。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ask_requires_model_config.py
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository, set_request_user, reset_request_user
from app.models.schemas import AskRequest


def _repo(tmp_path, **over):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st"), **over})
    repo = SQLiteRepository(s); repo._migrate(); repo._seed()
    return repo


def test_required_unconfigured_ask_surfaces_model_error(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    nb = repo.create_notebook_for_test() if hasattr(repo, "create_notebook_for_test") else None
    tok = set_request_user(repo.current_user())
    try:
        from app.models.schemas import NotebookCreate
        nb = repo.create_notebook(NotebookCreate(name="t", purpose=""))
        resp = repo.ask(nb.id, AskRequest(question="hi", mode="chunk"))
        assert any(e.stage == "answer" for e in resp.model_errors)
        assert resp.answer == ""
    finally:
        reset_request_user(tok)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ask_requires_model_config.py -q`
Expected: FAIL（无 model_error）

- [ ] **Step 3: 插守卫**

在 `backend/app/services/sqlite_repository.py` 第 5585 行 `_err_token = _ASK_MODEL_ERRORS.set(_err_sink)` 之后、`try:` 之前插入：

```python
        if self.resolve_model_config(self.current_user(), "llm").source == "none":
            self._note_model_error(
                "answer", "", ModelNotConfiguredError("请先在设置中配置你的模型服务"))
```

（`_note_model_error` 已存在，会写 events.jsonl 并 append 到 `_err_sink`；后续 `if self.llm_client.configured` 处哨兵 configured=False 自动跳过 LLM，answer 留空。）

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && python -m pytest tests/test_ask_requires_model_config.py tests/test_ask_modes.py -q`
Expected: PASS（新用例过；ask_modes 回归不破）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ask_requires_model_config.py
git commit -m "feat(model-config): required 阶段主 LLM 未配置经 model_error 提示"
```

---

## Task 7: API 端点 GET/PUT/test + schema(打码)

**Files:**
- Modify: `backend/app/models/schemas.py`（末尾加请求/响应模型）
- Modify: `backend/app/api/routes.py`（加 3 个路由 + 打码/合并辅助）
- Test: `backend/tests/test_model_settings_api.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_model_settings_api.py
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.api.deps import repository


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.core.config import get_settings
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st"), "auth_optional": True})
    from app.services.sqlite_repository import SQLiteRepository
    repo = SQLiteRepository(s); repo._migrate(); repo._seed()
    monkeypatch.setattr("app.api.deps.repository", lambda: repo)
    monkeypatch.setattr("app.api.deps.get_settings", lambda: s)
    return TestClient(create_app())


def test_get_defaults_masked(client):
    r = client.get("/api/me/model-settings")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank"}
    assert body["llm"]["has_key"] is False and body["llm"].get("api_key") is None


def test_put_then_get_masks_key(client):
    client.put("/api/me/model-settings", json={
        "llm": {"base_url": "https://u/v1", "api_key": "sk-secret123", "model": "m"}})
    body = client.get("/api/me/model-settings").json()
    assert body["llm"]["base_url"] == "https://u/v1" and body["llm"]["model"] == "m"
    assert body["llm"]["has_key"] is True
    assert "secret" not in (body["llm"].get("key_hint") or "")   # 不回显全 key
    assert "api_key" not in body["llm"] or body["llm"]["api_key"] is None


def test_put_omit_key_preserves_clear_empties(client):
    client.put("/api/me/model-settings", json={
        "llm": {"base_url": "https://u/v1", "api_key": "sk-secret123", "model": "m"}})
    # 省略 api_key → 保留；改 model
    client.put("/api/me/model-settings", json={"llm": {"base_url": "https://u/v1", "model": "m2"}})
    body = client.get("/api/me/model-settings").json()
    assert body["llm"]["model"] == "m2" and body["llm"]["has_key"] is True
    # 显式空串 → 清除 base_url
    client.put("/api/me/model-settings", json={"llm": {"base_url": ""}})
    body = client.get("/api/me/model-settings").json()
    assert body["llm"]["base_url"] == ""


def test_test_endpoint_incomplete_returns_not_ok(client):
    r = client.post("/api/me/model-settings/test", json={"service": "llm", "base_url": "", "model": ""})
    assert r.status_code == 200 and r.json()["ok"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_model_settings_api.py -q`
Expected: FAIL（路由 404）

- [ ] **Step 3: schema 模型**

在 `backend/app/models/schemas.py` 末尾追加：

```python
class ModelServiceView(BaseModel):
    base_url: str = ""
    model: str = ""
    has_key: bool = False
    key_hint: str = ""          # 打码尾段，如 "…t123"；绝不含完整 key
    source: str = "system"      # user | system | none

class ModelServiceUpdate(BaseModel):
    # 三态：字段缺省=不变；""=清除；非空=设置。api_key 同理(缺省=保留原 key)。
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None

class ModelSettingsUpdate(BaseModel):
    llm: Optional[ModelServiceUpdate] = None
    reasoning_llm: Optional[ModelServiceUpdate] = None
    rewrite_llm: Optional[ModelServiceUpdate] = None
    kg_llm: Optional[ModelServiceUpdate] = None
    rerank: Optional[ModelServiceUpdate] = None

class ModelTestRequest(BaseModel):
    service: str
    base_url: str = ""
    api_key: Optional[str] = None   # 省略 → 用已存 key
    model: str = ""

class ModelTestResult(BaseModel):
    ok: bool
    latency_ms: int = 0
    error: str = ""
```

- [ ] **Step 4: 路由 + 辅助**

在 `backend/app/api/routes.py` 顶部 import 区补：
```python
from app.models.schemas import (
    ModelServiceView, ModelSettingsUpdate, ModelTestRequest, ModelTestResult,
)
```

在 `routes.py` 内（与 `/me` 同一 router，`get_current_user` 已是 router 级依赖）加：

```python
_MODEL_ROLES = ("llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank")


def _mask_key(key: str) -> str:
    key = key or ""
    return f"…{key[-4:]}" if len(key) >= 4 else ("…" if key else "")


@router.get("/me/model-settings")
def get_model_settings(user: UserProfile = Depends(get_current_user)):
    repo = repository()
    stored = repo.get_user_model_settings(user.id)
    out = {}
    for role in _MODEL_ROLES:
        svc = stored.get(role) or {}
        out[role] = ModelServiceView(
            base_url=svc.get("base_url", ""),
            model=svc.get("model", ""),
            has_key=bool(svc.get("api_key")),
            key_hint=_mask_key(svc.get("api_key", "")),
            source=repo.resolve_model_config(user, role).source,
        )
    return out


@router.put("/me/model-settings")
def put_model_settings(payload: ModelSettingsUpdate, user: UserProfile = Depends(get_current_user)):
    repo = repository()
    stored = dict(repo.get_user_model_settings(user.id))
    for role in _MODEL_ROLES:
        upd = getattr(payload, role)
        if upd is None:
            continue
        svc = dict(stored.get(role) or {})
        for field in ("base_url", "api_key", "model"):
            val = getattr(upd, field)
            if val is None:          # 不变
                continue
            if val == "":            # 清除
                svc.pop(field, None)
            else:                    # 设置
                svc[field] = val
        if svc:
            stored[role] = svc
        else:
            stored.pop(role, None)
    repo.set_user_model_settings(user.id, stored)
    return get_model_settings(user)


@router.post("/me/model-settings/test", response_model=ModelTestResult)
def test_model_service(payload: ModelTestRequest, user: UserProfile = Depends(get_current_user)):
    import time
    if payload.service not in _MODEL_ROLES:
        return ModelTestResult(ok=False, error="未知服务")
    repo = repository()
    stored = repo.get_user_model_settings(user.id).get(payload.service) or {}
    api_key = payload.api_key if payload.api_key else stored.get("api_key", "")
    base_url, model = payload.base_url.strip(), payload.model.strip()
    if not (base_url and model and api_key):
        return ModelTestResult(ok=False, error="缺少 base_url / model / api_key")
    started = time.perf_counter()
    try:
        if payload.service == "rerank":
            from app.services.rerank_client import RerankClient
            RerankClient(repo.settings, model=model, base_url=base_url, api_key=api_key)._rerank_batch(
                "ping", ["a", "b"])
        else:
            from app.core.llm import OpenAICompatibleClient
            OpenAICompatibleClient(repo.settings, base_url=base_url, api_key=api_key, model=model).chat_json(
                [{"role": "user", "content": "ping"}], "{}", timeout=10, max_retries=0)
        return ModelTestResult(ok=True, latency_ms=round((time.perf_counter() - started) * 1000))
    except Exception as exc:
        return ModelTestResult(ok=False, latency_ms=round((time.perf_counter() - started) * 1000),
                               error=f"{type(exc).__name__}: {exc}"[:200])
```

> 注：`UserProfile` / `Depends` / `get_current_user` / `repository` / `router` 在 routes.py 已 import（`/me` 端点同款）。若 import 缺失按文件现状补齐。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_model_settings_api.py -q`
Expected: PASS（4 passed）

- [ ] **Step 6: 全量回归**

Run: `cd backend && python -m pytest -q`
Expected: PASS（全绿；含既有 ~950 用例）

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/schemas.py backend/app/api/routes.py backend/tests/test_model_settings_api.py
git commit -m "feat(model-config): /me/model-settings GET/PUT/test 端点(key 打码只写)"
```

---

## Task 8: 前端模型服务设置面板 + fetcher + 测试按钮

**Files:**
- Create: `frontend/app/model-settings.ts`（类型 + fetcher）
- Modify: `frontend/app/page.tsx`（设置面板 state/JSX + 入口按钮）
- Test: `frontend/app/model-settings.test.mjs`（合并/脏标逻辑单测）

- [ ] **Step 1: 写 fetcher + 纯逻辑（含可单测的 buildPutPayload）**

```typescript
// frontend/app/model-settings.ts
import { API_BASE, authHeaders } from "./auth";

export const MODEL_ROLES = ["llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank"] as const;
export type ModelRole = (typeof MODEL_ROLES)[number];

export type ModelServiceView = {
  base_url: string; model: string; has_key: boolean; key_hint: string; source: string;
};
export type ModelSettingsView = Record<ModelRole, ModelServiceView>;

// 表单态：api_key 用单独的「已改动」标记，未改动则 PUT 时省略以保留原 key。
export type ServiceForm = { base_url: string; model: string; api_key: string; keyDirty: boolean };

export function buildPutPayload(forms: Record<ModelRole, ServiceForm>) {
  const out: Record<string, { base_url: string; model: string; api_key?: string }> = {};
  for (const role of MODEL_ROLES) {
    const f = forms[role];
    const svc: { base_url: string; model: string; api_key?: string } = {
      base_url: f.base_url.trim(), model: f.model.trim(),
    };
    if (f.keyDirty) svc.api_key = f.api_key;   // 改动了才发；"" 表示清除
    out[role] = svc;
  }
  return out;
}

export async function fetchModelSettings(): Promise<ModelSettingsView> {
  const res = await fetch(`${API_BASE}/me/model-settings`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

export async function saveModelSettings(payload: ReturnType<typeof buildPutPayload>): Promise<ModelSettingsView> {
  const res = await fetch(`${API_BASE}/me/model-settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

export async function testModelService(
  service: ModelRole, base_url: string, model: string, api_key: string | null,
): Promise<{ ok: boolean; latency_ms: number; error: string }> {
  const res = await fetch(`${API_BASE}/me/model-settings/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ service, base_url, model, api_key }),
  });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}
```

- [ ] **Step 2: 纯逻辑单测**

```javascript
// frontend/app/model-settings.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { buildPutPayload, MODEL_ROLES } from "./model-settings.ts";

test("buildPutPayload omits api_key when not dirty", () => {
  const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r,
    { base_url: " https://u/v1 ", model: " m ", api_key: "x", keyDirty: false }]));
  const p = buildPutPayload(forms);
  assert.equal(p.llm.base_url, "https://u/v1");
  assert.equal(p.llm.model, "m");
  assert.equal("api_key" in p.llm, false);
});

test("buildPutPayload includes api_key (even empty) when dirty", () => {
  const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r,
    { base_url: "", model: "", api_key: "", keyDirty: true }]));
  assert.equal(buildPutPayload(forms).llm.api_key, "");
});
```

> 若 `node --test` 不直接吃 `.ts` import，按本仓库既有 `*.test.mjs` 的做法（参照 `frontend/app/*.test.mjs` 现有用例的 import 方式）调整为它们用的同款方式；逻辑断言不变。

Run: `cd frontend && node --test app/model-settings.test.mjs`
Expected: PASS（与仓库既有 mjs 测试同款运行）

- [ ] **Step 3: page.tsx 加面板 state**

在 `frontend/app/page.tsx` 现有 state 区（`const [toast, setToast]` 第 843 行附近）加：

```typescript
  const [modelPanelOpen, setModelPanelOpen] = useState(false);
  const [modelForms, setModelForms] = useState<Record<ModelRole, ServiceForm> | null>(null);
  const [modelTesting, setModelTesting] = useState<Record<string, string>>({});
```

文件顶部 import 区补：
```typescript
import {
  MODEL_ROLES, ModelRole, ServiceForm, ModelSettingsView,
  buildPutPayload, fetchModelSettings, saveModelSettings, testModelService,
} from "./model-settings";
```

- [ ] **Step 4: 打开面板时拉取 + 落表单**

在 page.tsx 加一个打开函数（toast effect 附近）：

```typescript
  const ROLE_LABELS: Record<ModelRole, string> = {
    llm: "主 LLM", reasoning_llm: "推理 LLM", rewrite_llm: "改写 LLM",
    kg_llm: "构图 LLM", rerank: "重排 Rerank",
  };

  async function openModelPanel() {
    setModelPanelOpen(true);
    try {
      const view = await fetchModelSettings();
      const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r, {
        base_url: view[r].base_url, model: view[r].model,
        api_key: "", keyDirty: false,
      }])) as Record<ModelRole, ServiceForm>;
      setModelForms(forms);
    } catch (e) { reportError(e); }
  }

  async function saveModelPanel() {
    if (!modelForms) return;
    try {
      await saveModelSettings(buildPutPayload(modelForms));
      setModelPanelOpen(false);
      setToast("模型服务配置已保存");
    } catch (e) { reportError(e); }
  }

  async function runModelTest(role: ModelRole) {
    if (!modelForms) return;
    const f = modelForms[role];
    setModelTesting((m) => ({ ...m, [role]: "测试中…" }));
    try {
      const r = await testModelService(role, f.base_url.trim(), f.model.trim(),
        f.keyDirty ? f.api_key : null);
      setModelTesting((m) => ({ ...m, [role]: r.ok ? `通 ${r.latency_ms}ms` : `失败：${r.error}` }));
    } catch (e) {
      setModelTesting((m) => ({ ...m, [role]: "失败" })); reportError(e);
    }
  }
```

- [ ] **Step 5: 入口按钮 + 面板 JSX**

在工作区设置按钮处（page.tsx 第 2401 行附近）加一个入口（与现有 nav button 同款 class）：
```tsx
        <button className="workspace-nav-button" onClick={() => openModelPanel()}>
          <span>模型服务</span>
        </button>
```

在 toast 渲染（page.tsx 第 3500 行附近）旁加面板，复用既有 modal class：
```tsx
        {modelPanelOpen && modelForms && (
          <section className="utility-modal" role="dialog" aria-modal="true"
            onClick={(e) => { if (e.currentTarget === e.target) setModelPanelOpen(false); }}>
            <div className="utility-modal-card">
              <div className="source-modal-header">
                <div><h2>模型服务</h2><p>留空则使用系统默认；API Key 只写不回显</p></div>
                <button className="icon-button" onClick={() => setModelPanelOpen(false)}>×</button>
              </div>
              <div className="source-detail-body">
                {MODEL_ROLES.map((role) => (
                  <fieldset key={role} className="edit-form" style={{ marginBottom: 12 }}>
                    <legend>{ROLE_LABELS[role]}</legend>
                    <label>Base URL
                      <input value={modelForms[role].base_url}
                        onChange={(e) => setModelForms((s) => s && ({ ...s, [role]: { ...s[role], base_url: e.target.value } }))} />
                    </label>
                    <label>Model
                      <input value={modelForms[role].model}
                        onChange={(e) => setModelForms((s) => s && ({ ...s, [role]: { ...s[role], model: e.target.value } }))} />
                    </label>
                    <label>API Key
                      <input type="password" placeholder="未改动则保留原 key"
                        value={modelForms[role].api_key}
                        onChange={(e) => setModelForms((s) => s && ({ ...s, [role]: { ...s[role], api_key: e.target.value, keyDirty: true } }))} />
                    </label>
                    <div className="modal-actions">
                      <button type="button" className="sort-button" onClick={() => runModelTest(role)}>测试</button>
                      <span style={{ fontSize: 12, opacity: 0.8 }}>{modelTesting[role] || ""}</span>
                    </div>
                  </fieldset>
                ))}
                <div className="modal-actions">
                  <button className="sort-button" onClick={() => setModelPanelOpen(false)}>取消</button>
                  <button className="new-pill" onClick={() => saveModelPanel()}>保存</button>
                </div>
              </div>
            </div>
          </section>
        )}
```

- [ ] **Step 6: 类型检查**

Run: `cd frontend && npm run lint`
Expected: tsc --noEmit 无错误

- [ ] **Step 7: Commit**

```bash
git add frontend/app/model-settings.ts frontend/app/model-settings.test.mjs frontend/app/page.tsx
git commit -m "feat(model-config): 前端模型服务设置面板 + 测试按钮"
```

---

## Task 9: .env.example 文档化开关

**Files:**
- Modify: `.env.example`（加 `USER_MODEL_CONFIG_POLICY`）

- [ ] **Step 1: 加注释项**

在 `.env.example` 模型相关段落（`OPENAI_COMPAT_*` 附近）加：

```bash
# 每用户模型配置策略：fallback=用户没配则回退下面这些系统默认(第一阶段);
# required=用户没配则该服务不可用(第二阶段)。
USER_MODEL_CONFIG_POLICY=fallback
```

- [ ] **Step 2: 确认后端能读到默认**

Run: `cd backend && python -c "from app.core.config import get_settings; print(get_settings().user_model_config_policy)"`
Expected: 打印 `fallback`

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(model-config): .env.example 记录 USER_MODEL_CONFIG_POLICY"
```

---

## 收尾验证（全部任务后）

- [ ] 后端全量：`cd backend && python -m pytest -q` → 全绿
- [ ] 前端类型：`cd frontend && npm run lint` → 无错
- [ ] 前端逻辑测试：`cd frontend && node --test app/*.test.mjs` → 全绿
- [ ] 真机走查（preview）：登录 → 打开「模型服务」→ 填主 LLM(base_url/key/model) → 点测试看「通 Nms」→ 保存 → 重开面板确认 key 打码、base_url/model 回显 → 问答用上自己的模型。
- [ ] 提 PR（按仓库流程：rebase 到 master → push → `gh pr create --base master`）。

---

## Self-Review 记录

- **Spec 覆盖**：存储(T1)/解析两层回退(T2)/按用户 client 缓存(T3·T4)/policy 两阶段开关(T1·T2·T6)/ContextVar×线程(T5)/API 打码只写(T7)/测试连接全服务(T7)/前端面板(T8)/.env(T9) —— 逐条有任务。embedding & MinerU 明确不在范围。
- **占位符**：无 TBD/TODO；每步含真实代码与命令。两处「按仓库现状微调」标注(routes.py import 兜底、mjs 吃 .ts import 方式)是对既有约定的对齐，非占位。
- **类型一致**：`ResolvedModelConfig`/`ModelNotConfiguredError`(T2) 在 T3·T4·T6 复用；`resolve_model_config`/`get_user_model_settings`/`set_user_model_settings`(T1·T2) 全篇同名；前端 `ServiceForm`/`buildPutPayload`/`MODEL_ROLES`(T8) 一致。
- **风险**：第一阶段(`fallback`)下 KG 后台 job 即便没 T5 也只会回退 env(良性)；T5 让第二阶段也正确。`llm_client` 改 property 保留 setter → 既有「测试替换 fake」不破(T3 已含回归 `test_reasoning_llm_config.py`)。
