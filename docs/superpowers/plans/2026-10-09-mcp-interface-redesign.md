# MCP 接口重设计 + Agent 接入页优化(规格)

状态:规格已定,实施中。日期 2026-10-09。

## 0. 背景与用户裁决

评审结论(会话内):现有 28 个 MCP 工具有三处重复(两套问答、三种按 id 读原文、两种搜索),
22 个工具依赖会话内 `select_notebook` 状态;11 个 scope 过细且签发时可勾出用不了的组合;
缺权限时笔记本级工具的报错只有笔记本 id。

用户裁决(不可再议):

1. 不保留旧工具名(不做别名/过渡期)。
2. 问答走**同步**:一个 `ask` 调用直接返回答案;`get_ask` 只用于续读长结果/断线后重读。
3. Knowhow 4 个工具、`add_observation`、构建工具**保留**(构建合并为一个 `build`)。
4. scope 收为 **5 档**,个人记忆**并进 read**(存量只有 knowledge:read 的 token 迁移后能读主人私有记忆——用户知情接受)。
5. 存量 token 迁移规则:**持有该档主权限就给整档**(见 §1.3)。
6. token 签发后可再次复制:服务端**明文存储** token(用户裁决;存量 token 只有哈希,无法再复制)。
7. `/agents` 页面同步优化:复选框全选/取消全选等便利功能(§3)。

交付:两个 PR,按顺序。PR-1 = §1 + §2 + §3;PR-2 = §4 + §5,叠在 PR-1 之上。

---

## 1. 权限分档(PR-1,后端)

### 1.1 档位(存储与对外的唯一词汇)

| 档位 | 中文名(界面) | 覆盖的内部能力(capability) |
| --- | --- | --- |
| `read` | 读取 | `knowledge:read`、`memory:read`、`memory:read_candidates`、`agent_profile:read` |
| `ask` | 问答 | `ask:execute` |
| `contribute` | 提交 | `memory:propose`、`knowhow:code`、`agent_observation:write` |
| `manage` | 管理 | `sources:write`、`maintenance:execute`(仅限笔记本主人) |
| `delete` | 删除 | `sources:delete`(仅限笔记本主人,只删 Agent 自己加的来源) |

- `backend/app/domain/agent_tools.py`:`AGENT_SCOPES` 变为这 5 个档位;新增
  `AGENT_CAPABILITY_TIER: Mapping[str, str]`(capability → 档位),`AGENT_TIER_LABELS`(中文名)。
  删除无消费者的 `AgentToolAccessPolicy` / `AGENT_TOOL_SCOPE_POLICIES`,用 `AGENT_OWNER_ONLY_TIERS = {"manage","delete"}` 表达 owner-only。
- **代码内部继续用 capability 字符串**请求权限(调用点、调用记录 ledger 的 `capability` 列都不变,
  前端 ledger 标签不受影响)。`MemoryService.require_agent_access(principal, capability, notebook_id)`
  把 capability 映射到档位后检查 token 档位;未知 capability 直接 `ValueError`(编程错误)。
- 直接集合判断的两处同步改:`mcp_tools/global_ask.py::_authorize`(改为只要求 `ask`,读原文要求 `read`)、
  `mcp_tools/citations.py:73`(`"memory:read" in principal.scopes` → 档位判断,抽一个 `principal_has_capability()` helper)。
- `deps.py::user_or_agent_scope`(knowhow HTTP 路由)走同一个 `require_agent_access`,不需额外改。
  注意:`deps.py` 里浏览器 HTTP 能力表的 `sources:write` 等是**另一个命名空间**,不许动。
- 不再有 `ask` 依赖 `read` 的组合要求:全局问答原来要求 knowledge:read + ask:execute,现在只要求 `ask`。

### 1.2 结构化拒绝

`require_agent_access` 抛 `AgentAccessDenied(PermissionError)`,带 `reason`:
`inactive`(token/profile 失效)、`scope_missing`(附 `tier`)、`notebook_not_allowed`、`notebook_unreadable`。
`str(exc)` 仍是笔记本 id 以外的**中文可读文案**(例:`此凭证缺少「读取」权限，请在 Agent 接入页为它勾选后重试`)。
HTTP 侧(knowhow 路由)仍映射 404,不泄露原因;MCP 侧在 PR-2 映射成错误码(§4.6)。PR-1 里 MCP 报错文案即随之变清楚。

### 1.3 存量迁移(SQLite v89 / PG `0069_agent_token_tiers.sql`)

对 `agent_access_tokens.scopes_json` 逐行改写(含已撤销行,统一词汇):

- `read` ← 持有 `knowledge:read` 或 `memory:read`
- `ask` ← `ask:execute`
- `contribute` ← `memory:propose`
- `manage` ← `sources:write` 或 `maintenance:execute`
- `delete` ← `sources:delete`
- 只持有非主权限(如仅 `agent_profile:read`)的 token 迁移后档位为空 `[]`:它在运行时一律 `scope_missing`;
  编辑页要求至少勾一档才能保存。迁移幂等(已是档位词汇的值原样保留)。
- 同一迁移给 `agent_access_tokens` 加列 `token_plain`(SQLite `TEXT NULL`,PG `text NULL`),存量为 NULL。
- SQLite 写法参照 `_migration_87`(Python 读改写 JSON、`BEGIN IMMEDIATE`、自盖 `user_version`);
  PG 用 `jsonb` 表达式或 `CASE` 改写。两侧行为由共享用例 + SQLite/PG 双测试钉住
  (参照 `tests/promotion_provenance_migration_cases.py` 的模式)。
- 同步清单/影子清单/`verify_repository_snapshot.py` DDL 副本/`schema_manifest.py` 按新列更新。

### 1.4 token 可再复制

- 签发时把明文写入 `token_plain`。
- `AgentTokenSummary` 增加 `copyable: bool`(`token_plain` 非空 且 未撤销)。列表**不**返回明文。
- 新端点 `GET /agent-tokens/{token_id}/secret` → `{ "token": "..." }`:仅 token 主人;不是自己的返回 404;
  已撤销返回 409(`detail` 中文:已撤销的 token 不能再复制);`token_plain` 为空返回 409
  (`detail`:这个 token 签发于旧版本，无法再次复制，如需请重新签发)。响应头 `Cache-Control: no-store`。
- 撤销时同时把 `token_plain` 置 NULL。
- 鉴权仍按哈希比对(不改认证路径)。

### 1.5 签发/编辑校验

`_validate_agent_access` 增加:勾了 `manage` 或 `delete` 时,白名单里必须至少有一个笔记本是 token 主人**拥有**的
(`user_can_access_notebook`),否则 `ValueError("管理和删除权限只对你拥有的笔记本生效，所选笔记本里没有你拥有的")`
→ 422。旧 scope 字符串一律 `unsupported agent scopes` 422(现有行为)。

### 1.6 401 细分(放 PR-1,MCP 中间件)

`AgentBearerMiddleware` 在哈希**比对成功**后若 token 不可用,返回 401 且
`{"detail": <中文>, "code": "token_revoked"|"token_expired"|"profile_disabled"|"owner_ineligible"}`;
哈希不匹配/格式错仍是 `{"detail":"invalid or expired Agent token","code":"token_invalid"}`(不泄露 token 是否存在)。
`resolve_agent_token` 拆出能返回失效原因的变体;`touch` 只在成功时做。

---

## 2. 文档(PR-1)

- `docs/agent-mcp-memory-sop.md` / `_zh.md`:scope 表改为 5 档表(两表档位集合一致,测试钉住)。
- `docs/product-and-api.md` / `_zh.md`:token 段落、`GET /agent-tokens/{id}/secret`、`copyable`、401 code、
  迁移语义;工具表的 Scope 列改用档位名(工具名本 PR 不动)。
- `architecture.md` §3.4 owner-only 段落改用档位。
- `backend/tests/test_architecture_documentation.py`、`test_mcp_bundle_architecture.py` 的 scope 正则改为档位词汇。
- `release-notes/agent-access-simpler-permissions.md`(中文、普通用户口吻,按 release-notes/README.md):
  Agent 接入页的权限合并为五项、已签发的凭证可以随时再次复制、笔记本与权限支持一键全选。

---

## 3. `/agents` 页面优化(PR-1,前端)

文件:`frontend/app/agent-token-model.ts`、`frontend/app/agent-access-manager.tsx`、`frontend/app/agent-access.css`、
对应单测/组件测试。遵守 AGENTS.md 的 Interactive feedback(按下有可见变化,结果落在按钮自身或紧邻处)。

1. **权限 5 档**:每档一行 = 复选框 + 中文名 + 一句说明(写清它能做什么,如「添加、重新解析来源，触发图谱/索引构建；只对你拥有的笔记本生效」)。
   `manage`/`delete` 标注「仅对你拥有的笔记本生效」;当白名单里没有用户拥有的笔记本时,这两档禁用并在旁边说明原因
   (笔记本列表的 `access` 字段判断 owner)。默认草稿 = `["read"]`。
2. **全选 / 取消全选**:权限组、笔记本白名单组各一对按钮(或一个三态「全选」复选框),组标题旁显示「已选 N / M」。
   笔记本「取消全选」保留默认笔记本(它不可取消)。被禁用的档位不参与全选。
3. **笔记本白名单筛选**:笔记本超过 8 个时显示筛选输入框(按名称过滤,只影响显示;全选只作用于当前可见项)。
4. **过期时间快捷项**:7 天 / 30 天 / 90 天 按钮,点后填入输入框(仍可手改);当前值命中时按钮呈选中态。
5. **已签发 token 可再复制**:每行「复制 token」按钮(`copyable` 为真时),点击调用 secret 端点后复制;
   按钮自身显示 复制中…/已复制/失败(失败时在紧邻处显示可选中的 token 文本或错误);
   `copyable` 为假时显示灰色说明「旧版本签发，无法再复制」(已撤销行不显示)。
   签发回执去掉「仅显示这一次」文案,改为「可以之后在下方列表再次复制」。
6. **列表可读性**:行内显示白名单笔记本名称(超过 3 个折叠为「等 N 个」,悬停/展开看全)、
   状态标签(有效/已过期/已撤销);列表上方「隐藏已撤销和已过期」开关(默认开,记在 localStorage,读写包 try/catch)。
7. **Profile 选择**:只有一个启用中的 Profile 时自动选中。
8. 旧 scope(迁移后不应出现)仍按「已下线的权限」显示,不进编辑草稿(现有逻辑保留)。

---

## 4. MCP 工具目录重设计(PR-2)

### 4.1 无状态

- 删除 `select_notebook` 与会话内选择/`_PENDING_INTENTS_ATTR`。所有单库工具加可选参数
  `notebook_id: str = ""`,为空时用 token 的 `default_notebook_id`。
- `_selected_notebook(ctx, ...)` / `_writable_notebook(ctx, ...)` 改为接收显式 `notebook_id`
  (`_resolve_notebook(repo, principal, notebook_id)`),鉴权/记账/镜像围栏语义不变。

### 4.2 新目录(17 个,顺序即注册顺序)

| 工具 | 档位 | 取代 |
| --- | --- | --- |
| `list_notebooks(query?, offset?, limit?)` | 无 | 同名 |
| `get_notebook(notebook_id?)` | read | `select_notebook`、`get_notebook_profile`、`get_build_status` |
| `search(query, notebook_id?, include="formal"\|"memory", limit?)` | read | `search_notebook_context`、`search_agent_memory` |
| `read_reference(ref, offset?)` | read | `get_cited_element`、`get_global_cited_element`、`get_memory` |
| `ask(question, notebooks?, conversation_id?, mode?, intent?, client_request_id?)` | ask | `ask_notebook`、`ask_global` |
| `get_ask(job_id, answer_offset?, citation_offset?, coverage_offset?, trace_offset?)` | ask | `get_global_ask`(`cancel_global_ask` 删除) |
| `propose_memory(..., notebook_id?)` | contribute | 同名 |
| `add_observation(text, client_request_id, notebook_id?)` | contribute | 同名 |
| `list_sources(notebook_id?, source_id?, offset?, limit?)` | read | `list_sources`、`get_source_status` |
| `add_source(notebook_id?, title?, content_md?, file_name?, content_base64?, url?)` | manage | `add_source_text/_file/_url` |
| `reparse_source(source_id, notebook_id?)` | manage | 同名 |
| `delete_source(source_id, notebook_id?)` | delete | 同名 |
| `build(target="kg"\|"index", when?, notebook_id?)` | manage | `build_kg`、`build_retrieval_index` |
| `list_knowhow_tables`、`get_knowhow_discrimination`、`get_knowhow_row`、`put_knowhow_cell_code`(各加 `notebook_id?`) | read / contribute | 同名 |

细则:

- `get_notebook`:返回笔记本概况与计数、知识图谱/检索索引构建状态(原 `get_build_status` 全部字段)、
  `profile`(原 `get_notebook_profile` 的 shared/mine,仍标注不可引用、不可信)。
- `search`:`include="formal"` = 原 `search_notebook_context` 语义;`"memory"` = 原 `search_agent_memory`
  (候选+已确认)。每条结果带 `ref`。
- `add_source`:三组输入恰好一组(`content_md`[+`title`] | `file_name`+`content_base64`[+`title`] | `url`),
  否则 `invalid_argument`。原有各自校验、容量检查、去重语义不变。
- `list_sources(source_id=...)`:返回单条的完整状态(原 `get_source_status` 字段)。
- `build`:`target="kg"` 原 `build_kg` 语义(`when` 不适用,传了报 `invalid_argument`);`"index"` 原 `build_retrieval_index`。
- Knowhow 工具:行为不变,仅加 `notebook_id?`。

### 4.3 `ref`(不透明引用)

- 编码:`base64url(json)` 无填充,JSON 为 `{"k":"el","n":<notebook_id>,"s":<source_id>,"e":<element_id>}`、
  `{"k":"gel","j":<job_id>,"e":<element_id>}`、`{"k":"mem","n":<notebook_id>,"m":<memory_id>}`。
  不签名:`read_reference` 每次按 kind 走原工具的全部鉴权(原 `get_cited_element` 的读权/挂载/memory 规则、
  原 `get_global_cited_element` 的引用成员资格+核验失败不可开、原 `get_memory` 的候选需 read 档)。
- `search` 结果、`ask`/`get_ask` 的每条 citation 都给 `ref`;原字段(source_id/element_id/memory_id)保留。
- 解析失败 → `invalid_argument`。

### 4.4 `ask`(同步、一套)

路由(按顺序):

1. `conversation_id` 以 `gconv-` 开头 → 全局路径(`notebooks` 可省略,继承会话范围)。
2. 以 `conv-` 开头 → 单库路径,笔记本取自会话;会话不属于 token 主人、不在白名单、或与显式 `notebooks` 冲突 →
   `not_found`(不再静默新开会话)。
3. 否则 `notebooks` 省略 → `[default_notebook_id]`;长度 1 → 单库路径;2–8 → 全局路径;>8 → `invalid_argument`。
4. 插件引擎 mode 只允许单库路径,否则 `invalid_argument`。默认 `mode="reasoning"`。`retrieval_effort` 参数删除。

两条路径都**同步**跑到终态再返回(`_run_with_progress` 心跳):

- 单库路径:沿用 `ask_current` 全部语义(记忆通道、挂载库、插件引擎、`ask_available` 门、`submitted_via="mcp"`),
  但**返回 job id**(`askjob-…`;加一个返回 `(response, job_id)` 的变体,不改 HTTP 协议)。
  `client_request_id`:用 `begin_or_attach_durable_job` 语义;命中仍在运行的同 key 任务时轮询其终态(0.5s 起退避到 5s)。
- 全局路径:`GlobalAskService.start` 后用新公开方法 `wait(job_id, *, user_id, allowed_notebook_ids)`
  (基于 `attach()` 排空队列到终态,最后 `get_job` 兜底)。客户端断开不取消任务(任务分离继续)。
- 统一返回页(两种 job 同一形状,由泛化后的 `_job_page` 生成):
  `status`(answered/failed/cancelled/needs_clarification)、`job_id`、`conversation_id`、`answer_id`、
  `answer` + `next_answer_offset`、`citations`(每条带 `ref`)+ `next_citation_offset`、
  `coverage`(全局:原 coverage;单库:`skipped_libraries` 收进 coverage.skipped)+ `next_coverage_offset`、
  `trace`(分页)、`intent`(reasoning 摘要)、`anchors`(单库)。单库路径的 memory 剥离(无 read 档)在分页前做。
- `get_ask(job_id)`:按前缀分派 `askjob-` / `gask-`;单库 job 鉴权复制 `ask_routes.py` 的 job 详情检查
  (创建者 = token 主人、笔记本在白名单、可读)。

澄清(一套协议,无状态):

- 新表 `ask_intent_handles`(SQLite v90 / PG `0070_ask_intent_handles.sql`):`token`(≥128 bit 随机)主键、
  `owner_id`、`scope_key`(单库:notebook_id;全局:排序后的 resolved ids + conversation_id)、`question_sha256`、
  `contract_json`、`understanding_ms`、`created_at`、`expires_at`(1 小时)。非消费性(失败可重试)。过期行在写入时顺手清理。
- reasoning 模式下需要澄清 → 返回 `status="needs_clarification"`、`intent_token`、审阅视图(沿用 `_review_view`
  分层与预算)、`next_step`。再次调用 `ask(..., intent={"intent_token","answers",...})` 时两条路径都从表中取 contract
  构造 `AskIntentConfirmation`(全局路径经 `GlobalAskRequest.intent`)。
- `test_user_error.py:252` 等引用旧测试名的地方随测试改名同步。

### 4.5 `tools/list` 按档位过滤

列工具时只列当前 token 档位够用的工具(`list_notebooks` 恒列)。调用未列出的工具仍返回 `scope_missing`。
档位由每个工具注册时声明(`_CoreToolCapture.tool(description=..., tier=...)`),同一份声明供过滤与文档测试使用。

### 4.6 错误模型

- 统一 `AgentToolError(code, message)`;工具里抛出的 `AgentAccessDenied`、`PermissionError`、`KeyError`、
  `ValueError`、`MirroredNotebookError`、`GlobalAskError` 等在 `mcp_tool_host` 注册包装层统一映射。
- 文本格式:`[<code>] <中文说明>`(FastMCP 会再加 `Error executing tool <name>: ` 前缀,接受)。
- code 词表:`token_inactive`、`scope_missing`、`notebook_not_allowed`、`notebook_unreadable`、`owner_only`、
  `not_found`、`invalid_argument`、`busy`(构建中/重复)、`mirrored`、`unavailable`(模型/引擎不可用、容量)、`internal`。
- `scope_missing` 文案写出档位中文名;`not_found` 写出资源类别(不回显内部 repr);`internal` 不回显异常细节
  (沿用 `_safe_errors` 的做法)。所有面向 Agent 的文案改为中文。

### 4.7 联动面(PR-2 全部更新)

`mcp_tool_host.py`、`mcp_server.py`、`agent_mcp_onboarding.py`(及测试)、`scripts/smoke_memory_mcp.py`、
`scripts/example_mcp_memory_client.py`、`scripts/README.md`、`docs/product-and-api*.md`(工具表、"28 tools" 计数
改 17)、`docs/agent-mcp-memory-sop*.md`、`architecture.md` §3.4、`ownership_manifest.py` +
`facade_surface.json`(用 `scripts/generate_repository_contract_fixtures.py` 再生成)、`caller_boundaries.json`、
`scripts/architecture_boundary_baseline.json`(`ask_notebook` 天花板条目随函数改名/改长更新,零松弛)、
所有 MCP 测试(`test_memory_mcp.py`、`test_global_ask_mcp.py`、`test_mcp_memory_channel_e2e.py`、
`test_default_ceiling_entrypoints.py`、`test_search_concurrency_gate.py`、`test_memory_kg_readers_e2e.py`、
`test_mount_viewer_e2e.py`、`test_memory_source_endpoints.py`、`test_mcp_bundle_architecture.py`、
guard 测试里按函数路径登记的键、PG 孪生测试)。release note:`release-notes/agent-mcp-simpler-tools.md`。

## 5. 验收

- PR-1:迁移双后端测试(含幂等、空档、撤销行、`token_plain` 列);签发/编辑 manage/delete 校验;secret 端点
  (主人/非主人/撤销/旧 token);401 code;MCP 缺档报错为中文可读文案(不再只有笔记本 id);前端单测+组件测试
  覆盖全选/取消全选/禁用档/快捷过期/复制按钮状态;`scripts/check.sh` 全绿。
- PR-2:新目录 17 个且 `tools/list` 按档过滤;不 select 即可调用;`ask` 单库/多库/会话续问/澄清往返/插件引擎/
  `client_request_id` 重放;`get_ask` 两种 job 分页;`read_reference` 三种 ref 及各自拒绝;错误码逐类用例;
  文档测试与守卫全绿;`scripts/check.sh` 全绿;本地实际起服务用 MCP 客户端走一遍主流程。
