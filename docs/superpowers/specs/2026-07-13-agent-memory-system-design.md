# Agent Memory System：回答沉淀、用户私有记忆与 MCP 接入

- 日期：2026-07-13
- 状态：设计修订待用户复核
- 产品：`silicon-notebook`
- 范围：Ask 回答保存为 Memory、用户级与 notebook 级 Memory 页面、Memory 检索融合、Agent MCP 接入、候选审核、Memory → KG 晋升

## 1. 背景

`silicon-notebook` 已具备 source 解析、chunk/KG 检索、带引用 Ask、多轮会话、知识治理、两层 KB、用户隔离与分享。当前缺少两个闭环：

1. 用户无法把一条有价值的模型回答编辑并确认成可长期复用的 Memory；
2. Claude Code、Codex 等外部 Agent 无法通过稳定协议使用 notebook 的 source、KG 与 Memory，也无法把任务经验提交回来。

本设计把产品从静态文档知识库扩展成受控的长期 Memory 服务。Memory 不是原始 source，也不是未经审核直接写入 KG 的事实。它有独立的形成、确认、版本、检索、弃用和晋升生命周期。

## 2. 已确认的产品决策

### 2.1 Memory 必须绑定 notebook

第一版不支持游离的全局 Memory。每条 Memory 必须同时绑定：

- 一个创建者 `created_by`；
- 一个且只有一个 `notebook_id`。

总 Memory 页面只是聚合当前用户在所有 notebook 下创建的 Memory。Notebook Memory 页面是同一数据按 notebook 的局部视图。

### 2.2 Memory 归创建者私有

Memory 的可见性由 `(created_by, notebook_id)` 共同约束。即使多个用户访问同一个共享 notebook，各自保存或由各自 Agent 创建的 Memory 也互不可见。Notebook owner、reader 和管理员不会仅凭 notebook 权限自动获得他人的 Memory。

唯一的共享出口是显式的 Memory → KG 晋升流程。管理员批准后生成或合并的 KG 对象遵循现有 notebook/base 权限与治理规则；原 Memory 仍是创建者私有记录。

### 2.3 两种形成来源

- **Ask Memory**：用户从 notebook 的某条模型回答发起，系统自动提炼草稿，用户编辑并确认后保存。
- **Agent Memory**：外部 Agent 通过 MCP 提交，只能进入 `candidate`，必须回到 `silicon-notebook` 由用户确认。

第一版不做无确认的正式 Memory 自动写入。Agent 只有在用户显式开启 `memory:propose` 后才能主动提交候选。

Agent candidate 形成后立即进入**当前用户、当前 notebook 的 Agent 共享候选记忆平面**。同一用户在该 notebook 下获授权的所有 Agent 都可以跨任务检索和消费。它不会进入 Notebook 正式检索平面；只有用户确认成 `confirmed` 后，网页 Ask、Notebook 搜索、Deep Report 和 `search_notebook_context` 才能使用。

`agent_profile` 仍是用户账号下稳定的 Agent 身份，用于记录 Candidate 创建者、token 轮换和审计，但不再作为同一用户内部的读取隔离边界。Candidate 的隔离键是 `(created_by, notebook_id)`；其他用户的 Agent 不能读取。

### 2.4 Memory 是独立产品页面

Memory 不再作为 Knowledge 中的 `note` 类型重复展示：

- 外层增加用户级总 Memory 页面；
- notebook 卡片与看板显示当前用户的 Memory 数量；
- 点击数量进入对应 notebook 的 Memory 页面；
- notebook 工作区主标签变为 `Ask | Knowledge | Memory | Deep Report`；
- 晋升成功产生的正式 KG 对象才进入 Knowledge。

这项决策覆盖早期“只在 Knowledge 中增加 note 类型”的探索方案。

## 3. 总体架构

采用“原生 Memory 层 + 检索投影 + MCP 适配器”，不把 Memory 伪装成 Markdown source，也不把候选直接写入 `knowledge_objects`。

```text
Ask 回答 ──预览/编辑/确认──┐
                            ├── Memory Service ── Memory Store / Embeddings
外部 Agent ──MCP candidate──┘          │
                                      ├── 用户级与 notebook 级 Memory UI
                                      ├── Retrieval Fusion → Ask / MCP
                                      └── Promotion → 管理员审核 → Base KG
```

Memory service 负责生命周期与权限；retrieval service 负责把合格 Memory 与 source chunk、KG hit 融合；MCP adapter 调用 consumer-specific service/repository ports，不拼 SQL，也不通过 HTTP 回调本服务的 REST API。

## 4. Memory 生命周期

### 4.1 状态

`memory_items.status`：

- `candidate`：Agent 已提交、用户未确认；仅当前用户在该 notebook 下的 Agent 共享候选检索可见，不参与 Notebook 正式检索；
- `confirmed`：用户已确认，可参与正式检索；
- `rejected`：用户拒绝，保留审计但永不参与检索；
- `deprecated`：曾确认但已弃用，不再参与检索。

草稿预览不落库，因此不存在持久化 `draft` 状态。

### 4.2 晋升状态独立

`memory_items.promotion_state`：

- `none`
- `proposed`
- `approved`
- `rejected`

晋升状态不能替代 Memory 生命周期。管理员批准后，Memory 仍是创建者私有的 `confirmed` 记录，另行关联生成或合并后的 KG object IDs。

### 4.3 版本

所有用户编辑、确认、拒绝、弃用与晋升动作写入 `memory_revisions`。当前正文保存在 `memory_items`，历史快照保存在 revision 表。删除式覆盖不允许；第一版提供弃用而非直接物理删除正式 Memory，父 notebook 删除时的生命周期级联除外。

## 5. 数据模型

通过下一版 `SqliteMigrator` 增加 version-gated migration，并保持 v9/v10 fixture 可升级。

### 5.1 `memory_items`

核心字段：

- `id`
- `notebook_id NOT NULL`
- `created_by NOT NULL`
- `title`
- `content_md`
- `tags_json`
- `origin`：`ask_answer | external_agent`
- `status`
- `promotion_state`
- `source_answer_id`：Ask 来源时非空
- `agent_profile_id`：Agent 来源时非空，用于记录创建者与审计
- `confirmed_by`、`confirmed_at`
- `created_at`、`updated_at`
- `embedding_status`、`embedding_error`

约束：

- 所有查询先限定 `created_by=current_user`，再校验 notebook 可读权限；
- Ask 来源建立用户级 `source_answer_id` 唯一保护，使同一用户重复保存同一回答返回已有 Memory；
- reader 可以在共享 notebook 下创建自己的私有 Memory，因为该动作不修改 notebook owner 的数据。

### 5.2 `memory_revisions`

保存：

- `memory_id`
- revision 序号
- 标题、正文、标签、状态和 promotion_state 快照
- `changed_by`
- `change_reason`
- `created_at`

### 5.3 `memory_provenance`

保存不可由前端伪造的来源快照：

- Ask 的 question、answer text、answer id、conversation id、mode、model 信息；
- Answer anchors、citations 与 evidence level；
- Agent 名称、token/client id、任务标识、任务上下文、提案理由；
- Agent 提交的 evidence references 及其服务端验证结果。

即使原 conversation 或 answer 后续删除，Memory 的 provenance 仍可审计。

### 5.4 `memory_embeddings`

Memory 不写入 `chunks` 或 `source_elements`。短卡片第一版每条生成一个 embedding，保存 model、dimension、vector 和更新时间。关键词索引与 embedding 独立，embedding 失败时仍可使用关键词检索。

### 5.5 Agent profile 与 token 表

`agent_profiles` 是用户创建的稳定 Agent 身份，保存名称、说明、owner 和状态。`agent_access_tokens` 绑定一个 profile，保存 token hash、scopes、默认 notebook、过期、撤销和 last-used 时间；`agent_token_notebooks` 保存 allowlist。明文 token 只在签发时显示一次。Profile 用于 provenance 和审计；同一用户、同一 notebook 下具备候选读取 scope 的其他 profile 也能读取 Candidate。

### 5.6 Agent 候选召回配置

Notebook 正式检索没有“包含 candidate”开关：candidate 在用户确认前始终禁止进入。Agent 接入配置通过 token scope 控制是否允许读取当前用户、当前 notebook 的 Agent 候选池；授予 `memory:read_candidates` 后允许，撤销该 scope 后立即停止返回，不需要另建用户级 Candidate 检索设置表。

## 6. Ask 回答保存流程

回答底部现有 👍、👎、复制动作旁增加“保存到 Memory”。

1. 前端调用 `POST /api/answers/{answer_id}/memory-preview`；
2. 后端用问题、回答和引用生成标题、Markdown 正文与标签建议；
3. LLM 不可用或失败时，使用问题作为标题、清理显示引用后的回答作为确定性正文；
4. 前端打开编辑弹窗，展示草稿与只读 provenance 摘要；
5. 用户确认后调用 `POST /api/notebooks/{id}/memories/from-answer`；
6. 服务端重新读取原 answer 并生成可信 provenance，写入 `confirmed` Memory 与首个 revision；
7. 关键词索引立即可用，embedding 后台生成；
8. 保存按钮显示“已保存”并可打开 Memory。

一个 Ask 回答对同一用户对应一条 Memory。用户明确确认，因此不经过 candidate。删除原会话不删除已保存 Memory。

若预览后原回答被删除，保存返回冲突错误；前端保留用户已编辑草稿，允许复制，但不能伪造一个失去可信来源的 Ask Memory。

## 7. 页面设计

### 7.1 总 Memory 页面

外层导航增加 `Memory`，显示当前用户全部 Memory：

- 总数与待确认数量；
- notebook、来源、状态、标签和关键词筛选；
- Ask/Agent provenance 摘要；
- 待确认队列；
- 编辑、确认、拒绝、弃用、提升 KG；
- Agent 接入、token 创建、撤销与 scope 管理；
- Agent profile 与“允许回忆自身候选记忆”设置。

### 7.2 Notebook Memory 页面

Notebook 卡片和 notebook 看板显示当前用户的 Memory 数量。点击后原子打开 notebook，并切换到 `Memory` 主标签。页面只展示当前用户在该 notebook 下的 Memory，提供与总页面一致的治理动作。

Notebook 列表的 Memory 数量由 notebook summary query 批量聚合，不能为每张卡片单独查询，避免集合页 N+1。

### 7.3 候选审核

Agent candidate 详情必须展示：

- Agent/client 名称；
- 任务上下文与提案理由；
- 引用的 source、KG 或既有 Memory；
- 标题、Markdown 正文和标签编辑器。

确认转为 `confirmed`；拒绝转为 `rejected`。MCP 不能执行确认、拒绝、弃用或晋升。

## 8. REST API

页面 API：

- `GET /api/memories`
- `GET /api/notebooks/{id}/memories`
- `GET /api/memories/{id}`
- `PATCH /api/memories/{id}`
- `POST /api/memories/{id}/confirm`
- `POST /api/memories/{id}/reject`
- `POST /api/memories/{id}/deprecate`
- `POST /api/memories/{id}/promote`
- `POST /api/answers/{answer_id}/memory-preview`
- `POST /api/notebooks/{id}/memories/from-answer`
- Agent token 的 list/create/revoke endpoints

所有列表支持有界分页。总列表只聚合当前用户数据；notebook 列表还要求用户当前可读该 notebook。API schema 必须禁止客户端写入 `created_by`、确认者、provenance、promotion result 等服务端字段。

## 9. 检索与权威

### 9.1 两个检索平面

**Agent 共享候选记忆平面**：

- `candidate` 可由同一 `created_by`、同一 notebook 下的任一获授权 Agent profile 读取；
- 要求 token 具备 `memory:read_candidates`，并且当前 notebook 在 allowlist；
- `confirmed` 也可作为该 Agent 的长期记忆返回；
- `rejected/deprecated` 永远排除。

**Notebook 正式检索平面**：

- 只有 `confirmed` 可进入网页 Ask、Notebook 搜索、Deep Report 和 MCP notebook context；
- `candidate/rejected/deprecated` 始终排除，不提供单次绕过参数；
- 所有 Memory 必须匹配当前用户与当前 notebook；
- 第一版不做跨 notebook Memory 检索。

### 9.2 相关性与权威性分离

相关性先决定候选是否与问题有关。不能因为 Memory 权威更高就把无关内容排到相关原始证据之前。权威性只用于合格候选的重排信号与冲突合成。

冲突优先级：

```text
candidate（仅 Agent 共享候选记忆平面）
  < personal 原始证据
  < confirmed Memory
  < base KG / base 原始证据
```

Confirmed Memory 表示用户明确接受的个人结论，因此在 personal 层冲突时优先于原始文档；base 仍拥有最终权威。引用详情同时展示 Memory 当前版本与原始 answer/source provenance，防止模型回答自我强化后失去来源。

### 9.3 索引与缓存

- 创建/确认立即更新关键词索引；
- embedding 通过现有后台调度边界生成，不阻塞保存；
- 修改、确认、拒绝和弃用更新 Memory 检索版本并使相关缓存失效；
- embedding 失败记录诊断但不回滚 Memory；
- Ask 不执行整库 Memory embedding backfill。

## 10. MCP 接入

### 10.1 用户配置

总 Memory 页面提供“Agent 接入”：

- 选择默认 notebook；
- 选择 token notebook allowlist，第一版默认只允许默认 notebook；
- 创建或选择稳定的 Agent profile；
- 选择 `knowledge:read`、`memory:read`、`memory:read_candidates`、`memory:propose`、`ask:execute` scopes；
- “允许 Agent 主动提交候选 Memory”默认关闭；开启后才授予 propose scope；
- 设置过期时间并签发可撤销 token。

### 10.2 工具

- `list_notebooks`：仅列 allowlist 内 notebook；
- `select_notebook`：明确选中当前 notebook，返回摘要、Memory 数量、KG 状态与检索设置；
- `search_agent_memory`：检索当前用户在当前 notebook 下由任一 Agent profile 创建的 candidate，以及当前用户已确认的 Memory；candidate 结果显示创建 Agent，并明确标记为未确认且不代表 notebook 正式结论；
- `search_notebook_context`：搜索当前 notebook 的 source、KG 与 confirmed Memory，返回类型、权威层、引用和 provenance；永不返回 candidate；
- `get_memory`：读取当前用户的 confirmed Memory；读取 candidate 时要求同一用户、同一 notebook 且具备 `memory:read_candidates`；
- `ask_notebook`：需要 `ask:execute`，复用现有 Ask，支持 `chunk` / `reasoning`，不默认使用实验性 `graph`；
- `propose_memory`：提交标题、正文、理由、任务上下文、evidence references 与 `client_request_id`，只能创建 candidate；同一 token 重试同一 request id 返回已有候选，不重复写入。

`propose_memory` 成功后，该 candidate 立即可由同一用户、同一 notebook 下获授权 Agent 的 `search_agent_memory` 找到，不等待用户确认。用户确认后，它同时进入 Notebook 正式检索平面；拒绝后立即从 Agent 候选检索消失。

每个数据工具在服务端继续携带并校验 notebook id，不能只相信 MCP session 中的 active notebook。远程重连恢复默认 notebook，但第一笔数据操作仍必须经过有效 notebook 选择。禁止单次跨库搜索。

### 10.3 Transport

- 本机与远程共用同一工具契约；
- 远程使用 HTTPS Streamable HTTP `/mcp`；
- 本机可直接连接本地 Streamable HTTP；如需要 stdio，只提供调用同一 service contract 的薄适配器；
- HTTP transport 校验 Origin、Bearer token、scope 与 notebook allowlist；
- MCP 输出有结果数量和文本长度上限；Memory 文本标记为数据/证据，不作为 Agent 系统指令。

Claude Code 官方支持 MCP 的 stdio、HTTP 与 SSE 配置；MCP 当前标准传输为 stdio 与 Streamable HTTP。实现文档应分别提供 Claude Code 与 Codex 配置示例，但服务器工具契约只有一份。

## 11. Memory → KG 晋升

只有 `confirmed` Memory 可以发起晋升：

1. 用户点击“提升到 KG”；
2. 系统从 Memory 提取 Concept / Claim / Formula / Procedure 候选，并携带 Memory revision 与原始 provenance；
3. 进入现有 promotion queue 的管理员审核路径；
4. 管理员审查 Memory、原回答和原始 evidence，执行批准或拒绝；
5. 批准时复用现有 dedupe/merge，生成或合并 base KG 对象；
6. Memory 记录 `promotion_state=approved` 与结果 object IDs。

批准不会把私有 Memory 行改成 base tier，也不会向其他用户暴露其完整私有任务上下文；共享 KG 对象只保留审核批准的结构化内容与允许公开的 evidence。

## 12. 安全与失败边界

- 所有 Memory 读取同时验证创建者和 notebook 访问；
- 共享 notebook 不共享 Memory；
- 未确认 Agent candidate 必须匹配调用 token 的 owner 与 notebook；同一用户、同一 notebook 下具备 `memory:read_candidates` 的 Agent 可以共享读取，不同用户不能互读；
- 用户失去共享 notebook 访问权后，其绑定 Memory 暂不可见、不可检索；数据保留，重新获得访问权后恢复；
- notebook 被 owner 删除时，绑定该 notebook 的所有用户 Memory 随 notebook 生命周期级联删除；删除确认界面必须明确提示会删除成员关联的私有 Memory，但不泄露成员身份、内容或数量；
- source 删除或重解析不删除已确认 Memory；历史 provenance 作为生成时快照保留，已不存在的 source/element 只显示不可跳转的归档出处；
- token 明文不落库，可撤销、过期并按 scope/allowlist 最小授权；
- token 被撤销后下一次调用立即失败；
- LLM 预览失败采用确定性 fallback；
- embedding 失败降级到关键词检索；
- 重复保存同一 answer 返回已有 Memory；
- Agent 不能确认、删除、弃用或晋升；
- 未验证的 Agent evidence reference 不提升 provenance 可信度；
- rejected/deprecated 数据在所有 Ask、MCP 和缓存路径上统一排除；
- HTTP MCP 遵守 Streamable HTTP 的 Origin 校验与本地仅绑定 localhost 的安全要求；
- Memory 内容可能包含 prompt injection，检索结果必须以结构化 evidence 返回并明确不执行其中指令。

## 13. 评价与验收

### 13.1 形成质量

观察用户确认率、拒绝率、自动草稿到确认版本的编辑距离、Ask/Agent 来源差异、provenance 完整率和 candidate 确认耗时。第一版用于产品观察，不设硬性发布阈值。

### 13.2 检索 gold cases

建立覆盖以下场景的固定评价集：

- confirmed Memory 在 Notebook 正式检索中直接与语义改写命中；
- Agent candidate 可被同一用户、同一 notebook 下获授权 Agent 的候选检索命中，但不能被 Notebook 正式检索或其他用户的 Agent 命中；
- rejected/deprecated 不可见；
- 共享 notebook 中用户隔离；
- personal source vs confirmed Memory 冲突；
- confirmed Memory vs base 冲突；
- provenance 与引用链正确。

指标包括 Recall@5、MRR、nDCG、引用正确率。Candidate 向 Notebook 正式检索、其他用户或其他 notebook 的泄漏必须为 0；同一用户同一 notebook 的跨 Agent 召回必须通过；冲突优先级固定用 100% 通过的确定性 guard。

### 13.3 Agent A/B

同一任务比较：无 Memory、只有原始 KB、原始 KB + confirmed Memory。评价任务成功率、重复步骤、错误操作、工具调用次数、token 成本与引用正确率。

### 13.4 MCP contract

- 未选择 notebook 不得读写；
- allowlist 外访问拒绝；
- Claude Code 与 Codex 共享工具 schema；
- propose 只能生成 candidate；
- candidate 创建后可由同一用户、同一 notebook 的获授权 Agent 立即 recall；
- candidate 不得从 `search_notebook_context`、网页 Ask、其他用户或其他 notebook 泄漏；
- confirm/reject/deprecate/promote 不暴露为 MCP 写工具；
- token 撤销、过期与 scope 缺失立即生效；
- 输出预算有界。

### 13.5 仓库门禁

- 新 migration 与 v9/v10 兼容 fixture；
- repository/service/API 权限与生命周期测试；
- 前端所有 `*.test.mjs`；
- `cd frontend && npm run build`；
- `bash scripts/check.sh`。

功能完成时必须同步 `README.md`、`README_zh.md`、`AGENTS.md`、`silicon_notebook_fangan.md` 和 `fangan_done.md`，且只有完整门禁通过后才能标记完成。

## 14. 实施分期

1. Memory migration、stores、ports、service、REST 与用户隔离；
2. 总 Memory 页面、Notebook Memory 页面与 Ask 保存闭环；
3. Memory 检索融合、权威冲突规则与 gold eval；
4. Agent token 管理与 MCP read tools；
5. `propose_memory`、Agent 私有 recall 与候选审核；
6. Memory → KG 晋升、管理员审核和完整端到端验证。

每一期必须保持后端与前端一致，不允许只提供 endpoint 或只提供 UI。

## 15. 非目标

- 不做 notebook 外的全局游离 Memory；
- 不做跨 notebook Memory 搜索；
- 不向 notebook 其他成员共享私有 Memory；
- 不允许 Agent 直接确认、修改正式 Memory、删除或晋升；
- 不把 Memory 伪装成 source；
- 不把 candidate 直接写入 KG；
- 不做自动遗忘、时间衰减或无用户 opt-in 的自动正式记忆；
- 不在第一版引入 PostgreSQL、pgvector 或 Docker；
- 不改变 base KG 的管理员审核权。

## 16. 参考资料

- Claude Code MCP：<https://docs.anthropic.com/en/docs/claude-code/mcp>
- MCP Transport：<https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- MCP Authorization：<https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization>
