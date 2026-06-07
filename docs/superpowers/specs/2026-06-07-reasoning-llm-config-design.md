# 推理搜索独立模型配置 — 设计 (Spec)

- 日期: 2026-06-07
- 状态: 已批准设计，待实现
- 关联: `mode=reasoning` 路径 (`ask_reasoning`)、`docs/superpowers/plans/2026-06-06-reasoning-mode-kg-retrieval.md`

## 1. 背景与目标

当前全局只有一组 `OPENAI_COMPAT_*`（base_url / api_key / model），由 `SQLiteRepository` 内单一的
`OpenAICompatibleClient`（`self.llm_client`）供**所有** LLM 调用共用：KG 抽取、fast 问答、
followup 改写、推理搜索、文章研究。推理模式（`mode=reasoning`）目前只有专属的**超时/重试**旋钮
（`reasoning_timeout_seconds` / `reasoning_max_retries`），但 base_url/key/model 仍与全局共用。

**目标**：让推理搜索能使用一组**独立配置的模型**（base_url / api_key / model name），与全局
`OPENAI_COMPAT_*` 解耦。典型用途——KG 抽取走便宜快模型，推理走更强的推理/thinking 模型。

**形态**（已与用户确认）：纯后端、环境变量驱动、改 `.env` 重启生效，与现有 `EMBED_*` 同构。
**不做**运行时 REST 配置端点、不做前端设置页、不做 per-request 模型覆盖。

## 2. 配置

### 2.1 新增环境变量（严格对应 base_url / key / model name 三项）

```
# 推理搜索 (mode=reasoning) 专用 LLM。三项全部填写时，推理路径改用此模型；
# 任一为空 → 整体回退到 OPENAI_COMPAT_*（行为与现状一致）。超时/重试沿用
# REASONING_TIMEOUT_SECONDS / REASONING_MAX_RETRIES，不在此另设。
REASONING_LLM_BASE_URL=
REASONING_LLM_API_KEY=
REASONING_LLM_MODEL=
```

加入 `.env.example`，位置紧邻 `OPENAI_COMPAT_*` 与现有 reasoning 旋钮。

### 2.2 Settings（`backend/app/core/config.py`）

- 新增 3 个字段：
  - `reasoning_llm_base_url: str = Field("", env="REASONING_LLM_BASE_URL")`
  - `reasoning_llm_api_key: str = Field("", env="REASONING_LLM_API_KEY")`
  - `reasoning_llm_model: str = Field("", env="REASONING_LLM_MODEL")`
- 新增属性 `reasoning_llm_configured`（与现有 `llm_configured` 同款，全有才算数）：
  ```python
  @property
  def reasoning_llm_configured(self) -> bool:
      return bool(
          self.reasoning_llm_base_url
          and self.reasoning_llm_api_key
          and self.reasoning_llm_model
      )
  ```
- （可选）`reasoning_llm_partially_configured`：3 项中**有些填了但非全填**为真，用于第 5 节 WARN。

### 2.3 回退语义：全有或全无

3 个变量全部非空 → 用专属模型；缺任一 → **整体**回退全局 `OPENAI_COMPAT_*`。
不做单字段混搭（避免"填了 model 但用了旧 key"这类诡异组合）。

## 3. 客户端改造（`OpenAICompatibleClient` 参数化，向后兼容）

现状：`backend/app/core/llm.py` 内 client 直接写死读 `self.settings.openai_compat_*`
（出现在 `configured` / `client()` / `chat_json()` 三处）。

改造：`__init__` 接受可选覆盖参数，**默认值仍取全局 settings**，从而对既有调用零行为变化。

```python
def __init__(self, settings, *, base_url=None, api_key=None, model=None, max_retries=None):
    self.settings = settings
    self.base_url   = base_url   if base_url   is not None else settings.openai_compat_base_url
    self.api_key    = api_key    if api_key    is not None else settings.openai_compat_api_key
    self.model      = model      if model      is not None else settings.openai_compat_model
    self.max_retries = max_retries if max_retries is not None else settings.openai_compat_max_retries
    self._client = None
    self.interaction_logger = LLMInteractionLogger(settings)
```

随之把读取点从 `self.settings.openai_compat_*` 改为实例属性：
- `configured` → `bool(self.base_url and self.api_key and self.model)`
- `client()` → 用 `self.base_url` / `self.api_key`；连接池大小逻辑**不变**（仍用
  `settings.openai_compat_timeout_seconds` 作 httpx 默认超时、`kg_extract_workers + kg_ask_reserve`
  作 max_connections——对推理 client 只是上限、无害）。
- `chat_json()` → `model = self.model`；attempts 用 `self.max_retries`。

**向后兼容保证**：现有 `OpenAICompatibleClient(settings)` 调用所有默认参数取全局值，
`configured`/`client()`/`chat_json()` 行为字节级等价于改造前。

## 4. 接线（repo 持双 client，推理 4 处切换）

### 4.1 `SQLiteRepository.__init__`（`backend/app/services/sqlite_repository.py:138`）

```python
self.llm_client = OpenAICompatibleClient(settings)
self.reasoning_llm_client = (
    OpenAICompatibleClient(
        settings,
        base_url=settings.reasoning_llm_base_url,
        api_key=settings.reasoning_llm_api_key,
        model=settings.reasoning_llm_model,
    )
    if settings.reasoning_llm_configured
    else self.llm_client          # 未配置 → 同一对象，行为等价
)
```

### 4.2 推理路径 4 处由 `llm_client` 改指 `reasoning_llm_client`

| 位置 | 文件 | 现状 | 改为 |
| --- | --- | --- | --- |
| `plan()` 门控 + 调用 | `reasoning_retrieval.py` | `self.repo.llm_client` | 推理 client |
| `reflect()` 门控 + 调用 | `reasoning_retrieval.py` | `self.repo.llm_client` | 推理 client |
| `_answer_reasoning()` 调用 | `sqlite_repository.py:3279` | `self.llm_client.chat_json` | `self.reasoning_llm_client.chat_json` |
| `ask_reasoning()` 门控 | `sqlite_repository.py:3333` | `self.llm_client.configured` | `self.reasoning_llm_client.configured` |

`ReasoningRetriever` 改为**构造参数显式接收** reasoning client（`ReasoningRetriever(repo, settings, llm_client)`），
不再 `self.repo.llm_client` 穿透——更利于单测注入。`ask_reasoning()` 内实例化处
（`sqlite_repository.py:3306`）相应传入 `self.reasoning_llm_client`。

**其余所有调用点一律不动**：KG 抽取、fast `ask`、`_rewrite_followup_query`（followup 改写仅在
fast 路径，推理路径在分流前不触发它）、文章研究，全部继续用 `self.llm_client`。

## 5. 可观测性

- `/health`（`routes.py:83` 一带）响应增 `reasoning_llm_configured: bool`，一眼看出专属模型是否生效。
- LLM 交互日志已逐条记录 `model` 字段，推理调用自然记成推理模型名，无需改动。
- （可选，可砍）`SQLiteRepository.__init__` 若检测到 `REASONING_LLM_*` **部分**填写，打一条 WARN
  （提示配漏、当前在回退），走项目既有日志通道。

## 6. 测试

新增/调整测试（`backend/tests/`）：

1. `settings.reasoning_llm_configured`：3 项全填 → True；缺任一 → False。
2. 回退：未配置 `REASONING_LLM_*` 时 `repo.reasoning_llm_client is repo.llm_client`（同一对象）。
3. 配置后：`repo.reasoning_llm_client` 是独立实例，且 `base_url` / `model` 取自 `REASONING_LLM_*`。
4. `OpenAICompatibleClient` 覆盖参数：传入覆盖后 `configured`、`client()` 用覆盖 base_url、
   `chat_json` 发覆盖 model（可对 client/mock 断言）。
5. 既有 `backend/tests/test_reasoning_ask.py` 保持绿（靠回退等价性——测试只配 `OPENAI_COMPAT_*` 时
   推理走的就是同一个 `llm_client`）。

## 7. 验收标准

- [ ] 仅配 `OPENAI_COMPAT_*`（不配 `REASONING_LLM_*`）：推理行为与改造前完全一致，既有测试全绿。
- [ ] 配齐 `REASONING_LLM_*`：推理路径的 plan/reflect/answer 三类调用使用推理模型，
      `llm.jsonl` 中对应记录的 `model` 为 `REASONING_LLM_MODEL`；抽取/fast 问答仍用 `OPENAI_COMPAT_MODEL`。
- [ ] `REASONING_LLM_*` 部分填写：整体回退全局；若实现了第 5 节 WARN，则有一条提示日志。
- [ ] `/health` 暴露 `reasoning_llm_configured`。
- [ ] 抽取、fast 问答、followup、文章研究链路无任何行为变化。

## 8. 不做什么 (YAGNI)

- 不做运行时 REST 配置端点（GET/PATCH config）。
- 不做前端设置页。
- 不做 per-request 模型覆盖（`AskRequest` 不加模型字段）。
- 不为推理新增 timeout/retry 环境变量（复用 `REASONING_TIMEOUT_SECONDS` / `REASONING_MAX_RETRIES`）。
- 不动 embedding（`EMBED_*`）与 KG 抽取链路。
