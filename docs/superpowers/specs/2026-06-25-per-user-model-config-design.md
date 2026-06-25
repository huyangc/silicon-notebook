# Per-user 模型服务配置 — 设计文档

- 日期：2026-06-25
- 状态：待评审
- 关联：[用户系统](2026-06-25-user-accounts-design.md)、[reasoning LLM 配置](2026-06-07-reasoning-llm-config-design.md)

## 背景与目标

有了用户系统（PR#78，owner 隔离 + ContextVar 当前用户）之后，希望**每个用户能配置自己的模型服务**（自带 base_url / api_key / model），而不是全站共用一套环境变量。

两阶段推进：

- **第一阶段（本期）**：用户**不配**就回退到系统默认（环境变量）。已有部署零感知，老用户照常用。
- **第二阶段（后续）**：用户**不配**就**用不了**该服务——解析失败抛错，前端提示"请先配置你的模型服务"。

第二阶段只通过**一个开关**切换，不重构。

## 范围

### 本期开放给用户配置的服务

| 服务 | env 前缀 | 角色 | 数据兼容风险 |
|---|---|---|---|
| 主 LLM | `OPENAI_COMPAT_*` | 抽取/问答/生成主力 | 无 |
| 推理 LLM | `REASONING_LLM_*` | reasoning 模式 | 无 |
| 改写 LLM | `REWRITE_LLM_*` | query 改写/扩展 | 无 |
| 构图 LLM | `KG_LLM_*` | KG 抽取/融合/冲突消解 | 无 |
| Rerank | `RERANK_*` | 交叉编码重排 | 无（纯运行时） |

### 不在本期范围

- **Embedding（`EMBED_*`）保持系统级**。它与已入库向量强绑定：改 model/dim 会让存量向量失效（dim 不符直接报错，同 dim 换模型则相似度失真）。开放它需要额外的"首次入库后锁定 / 换模型则重建向量"机制，留待后续单独设计。
- **MinerU（`MINERU_*`）保持系统级**。属部署环境基础设施，非按用户计费的"模型服务"。

## 当前现状

- 所有模型配置集中在 [`backend/app/core/config.py`](../../../backend/app/core/config.py) 的 pydantic-settings（v2）里，按 env 读取。
- 客户端在 [`SQLiteRepository.__init__`](../../../backend/app/services/sqlite_repository.py)（约 185–232 行）**一次性实例化为进程级单例**，全部读全局 `settings`：
  - `llm_client`（属性）、`_reasoning_llm_client` / `_rewrite_llm_client` / `_kg_llm_client`（私有，经 `@property` 暴露并在未配置时回退到 `llm_client`）。
  - `rerank_client`、`embedder`、`mineru_*`。
- `OpenAICompatibleClient`（[`backend/app/core/llm.py`](../../../backend/app/core/llm.py)）构造时**已支持** `base_url / api_key / model` 覆盖参数 → 建"每用户客户端"几乎零成本。
- `RerankClient`（[`backend/app/services/rerank_client.py`](../../../backend/app/services/rerank_client.py)）目前直接读 `settings.rerank_*`，**需重构为接受显式覆盖**。
- 当前用户经 ContextVar `_REQUEST_USER` 解析（`current_user()` / `get_current_user`，见 [`backend/app/api/deps.py`](../../../backend/app/api/deps.py)）。

## 设计

### 架构选型：按用户解析 + 客户端缓存

候选三方案：

1. **按用户解析 + 客户端缓存（采纳）**：用户配置存 JSON；resolver 把"用户配置叠加在 env 默认上"；客户端访问器按当前用户解析，按配置指纹 get-or-build 缓存。复用 `OpenAICompatibleClient` 已有的覆盖参数，env 路径原样保留作回退，切阶段=改一个开关。
2. 每请求构造 effective Settings、重建客户端：每请求重建 OpenAI SDK client 会毁掉连接池；若按指纹缓存则退化成方案 1。否决。
3. 每用户一个 Repository 实例：repo 还握着 DB 连接 / embedder / mineru 等系统级共享资源，按用户复制是浪费。否决。

### 1. 数据存储

- 在已有 `user_profiles` 表加一列 `model_settings TEXT NOT NULL DEFAULT '{}'`，走 `_migrate()` 的 `ALTER TABLE ADD COLUMN` 习惯（与 `domain_focus` 的 JSON 列一致）。
- JSON 形状与 env 一一对应；**空串 / 缺字段 = 回退**：

```json
{
  "llm":           {"base_url": "", "api_key": "", "model": ""},
  "reasoning_llm": {"base_url": "", "api_key": "", "model": ""},
  "rewrite_llm":   {"base_url": "", "api_key": "", "model": ""},
  "kg_llm":        {"base_url": "", "api_key": "", "model": ""},
  "rerank":        {"base_url": "", "api_key": "", "model": ""}
}
```

- **明文存储**（本期决策）。安全底线靠 API 层"只写不回显 + 打码展示"保证（见 §4）。不引入加密依赖 / 主密钥。

### 2. 配置解析与两阶段回退

新增解析器（建议放 `backend/app/services/model_config.py` 或 repo 方法）：

```
resolve_model_config(user, role) -> ResolvedModelConfig(base_url, api_key, model, source)
    role   ∈ {llm, reasoning_llm, rewrite_llm, kg_llm, rerank}
    source ∈ {user, system, none}
```

**两层回退**：

- **第 1 层（变体 → 主）**：`reasoning_llm` / `rewrite_llm` / `kg_llm` 用户未填 → 用**该用户自己的主 LLM**（保留现有"变体不配回退主力"语义，但锚定到用户自身，而非 env 的主 LLM）。
- **第 2 层（主 → env 或报错）**：用户主 LLM 未填 → 由开关决定：
  - `USER_MODEL_CONFIG_POLICY = fallback`（**第一阶段默认**）→ 用 env 默认（`source=system`）。
  - `USER_MODEL_CONFIG_POLICY = required`（**第二阶段**）→ 返回 `source=none`，调用点抛 `ModelNotConfiguredError`。

**开关实现**：`config.py` 新增 `user_model_config_policy: str = "fallback"`，env 映射用 `validation_alias`（避开 pydantic-settings v2 的 `Field(env=...)` 失效坑）。

**报错呈现**：`ModelNotConfiguredError` **复用现有 model_error 通道**——写 `events.jsonl` 的 `model_error` 事件、塞进 `AskResponse.model_errors`、前端横幅展示，文案"请先配置你的 {服务} 模型"。无需新增前端机制。

**rerank 的第二阶段**：rerank 本就优雅降级（未配=按原序/MMR）。第二阶段用户未配 rerank → 该用户无重排，不算硬错，与现有降级一致。

### 3. 按用户客户端缓存

- LLM：`OpenAICompatibleClient` 按解析出的 `(base_url, api_key, model)` 构造。缓存 `{指纹: client}`，指纹 = hash(base_url, api_key, model)。配置变 → 指纹变 → 懒建新 client，旧的可逐出（用户数量小，简单 dict + 上限即可）。
- rerank：`RerankClient` 重构为接受 `(model, base_url, api_key, max_docs)` 覆盖，同样按指纹缓存。
- 访问器改造：
  - `llm_client` 由**属性**改为**按当前用户解析**（确认无外部赋值点）。
  - `reasoning_llm_client` / `rewrite_llm_client` / `kg_llm_client` / `rerank_client` 的 `@property` 改为按当前用户解析（内部走第 1 层回退到用户主 LLM）。
- env 单例保留为"系统默认"路径，供第一阶段回退命中。

### 4. API 端点

挂在主 router（已有 `Depends(get_current_user)`）：

- `GET /api/me/model-settings`
  - 返回每个服务的 `base_url / model / has_key(bool) / key_hint(打码，如 sk-…abcd) / source(user|system|none)`。
  - **绝不回显完整 key**。
- `PUT /api/me/model-settings`
  - **字段三态语义**（消除歧义）：
    - 字段**未出现在 payload** → 保持不变。
    - 字段 = `""`（显式空串）→ 清除，回退到默认。
    - 字段 = 非空值 → 设为该值。
  - `api_key` 额外规则：等于 `GET` 返回的打码占位（如 `sk-…abcd`）→ 视为"未改动"保留原 key（让用户不重输 key 也能保存其它字段）；其余按上面三态。
- `POST /api/me/model-settings/test`
  - body：`{service, base_url, api_key?, model}`（**测当前表单里的候选值**，不必先保存）。
  - `api_key` 省略时回退到已存 key（支持"不重输 key 直接测"）。
  - 返回 `{ok, latency_ms, error?}`。
  - LLM 类：一次极小 `chat_json`（一句闲聊）。rerank：1 query + 2 短文档的极小 rerank。覆盖全部 5 个服务。

### 5. 前端设置面板

- 现有设置区新增「模型服务」块，5 个服务各一组：`base_url` / `api_key`（打码、只写）/ `model`，加「测试」按钮 + 测试结果（通/不通 + 延迟/错误）。
- 空字段显示"使用系统默认"；展示每服务 `source` 状态。
- 复用现有带鉴权的 fetcher（注入 Bearer token）。

### 6. 边界与风险（实现期盯死）

- **ContextVar × 工作线程**：KG 抽取、rerank 内部走线程池。`current_user()` 在裸 worker 线程取到的是 seeded 兜底用户（已知坑）。**原则：在请求线程上解析好每用户 client 对象，再传进 worker**，不在 worker 里调 `current_user()` / `resolve_model_config()`。
- **后台 / 离线操作**（如 Tier3 全量重建 KG）无请求上下文 → 用户须从 `notebook.created_by` 解析，而非 ContextVar。
- **embedding 不动** → 本期无向量兼容问题。
- 客户端缓存上限，避免用户量增长后无界增长。

## 关键改动点（文件清单）

| 文件 | 改动 |
|---|---|
| `backend/app/core/config.py` | 加 `user_model_config_policy`（validation_alias） |
| `backend/app/services/sqlite_repository.py` | `user_profiles` 加 `model_settings` 列 + 迁移；`model_settings` 读写方法；客户端访问器改按用户解析 + 缓存 |
| `backend/app/services/model_config.py`（新） | `resolve_model_config` + `ResolvedModelConfig` + `ModelNotConfiguredError` |
| `backend/app/services/rerank_client.py` | 构造接受显式覆盖 |
| `backend/app/api/routes.py` | `GET/PUT /me/model-settings`、`POST /me/model-settings/test` |
| `backend/app/models/schemas.py` | 请求/响应模型（打码字段） |
| `.env.example` | 加 `USER_MODEL_CONFIG_POLICY=fallback` 说明 |
| 前端（page.tsx / 设置组件 + fetcher） | 「模型服务」设置面板 + 测试按钮 |
| tests | 解析两层回退、policy 开关两态、打码不回显、test 端点 |

## 验收标准

1. 用户未配任何服务、`policy=fallback` → 行为与现状完全一致（env 默认）。
2. 用户配了自己的主 LLM → 问答走用户的 base_url/key/model；变体未配时回退到用户自己的主 LLM（不是 env）。
3. `GET /me/model-settings` 任何情况都不回显完整 key；`PUT` 不传 key 时保留原 key。
4. `POST /me/model-settings/test` 对 5 个服务都能返回通/不通。
5. `policy=required` 且用户未配主 LLM → 问答经 model_error 通道返回"请先配置"，前端横幅可见。
6. 前端面板可增改清各服务配置并测试。
7. 后端测试全绿（含上述用例），tsc clean。
