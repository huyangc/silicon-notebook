# MCP 读取问答记录与深度报告 实施计划

日期：2026-10-21
基线：`origin/master` `1c290955a`（行号以此为准，实现时以符号名为准）

用户反馈：通过 MCP 看不到用户在笔记本里的问答，希望 Agent 能读到对应的问答信息与深度报告。
用户已拍板：

1. 新增 4 个 MCP 只读工具：`list_conversations`、`get_conversation`、`list_reports`、`get_report`。
2. 授权并入现有「读取」（`read`）档，不加开关、不加新档；已发出的 `read` 档凭证立即获得这些能力，
   更新说明与 Agent 接入页「读取」档文案都要写明。
3. 权限沿用现有规则：只看凭证主人自己的会话与报告；笔记本须在白名单内且主人仍可读，每次调用重读凭证；
   全局会话只要有一轮用到白名单外的库就整条不给（列表也不泄露标题）；不暴露他人记忆。
4. `get_ask` 语义不变（仍只认经 MCP 发起的任务）；文档说明「读浏览器里的问答用 `get_conversation`」。
5. 并行的 A 线（问答关联深度报告：`AskRequest.report_id` + 每轮 `AskResponse.report_context`）在本 B 线
   **之后**合入，由 A 线给 `list_conversations` / `get_conversation` 补关联报告字段；B 线只留自然的扩展位。
6. **本计划范围一轮做完，不留延后项。**

下文 `$W` 指**实施 worktree 的根目录**（新建的 linked worktree，路径实施时确定）。本计划**不加任何迁移**。

---

## 待确认

无需产品裁决即可开工。下面两条按 §1 的默认执行，若用户要改请在开工前说：

- **D4**：进行中的提问不算「一轮」（单库：只有已保存的回答；全局：只有已结束的轮次）。
- **D9**：报告里引用的他人记忆，与网页认证详情一致（作者本人能看到存下的摘录），只在标识符上更严。

---

## 0. 每个实现子代理都要遵守的公共约束

- 开工前读：`$W/AGENTS.md`、`$W/CLAUDE.md`、`$W/docs/development_zh.md`（「架构边界」「SQL 里绑定 id 清单」
  「验证」「文档维护」）、`$W/release-notes/README.md`、`$W/docs/agent-mcp-memory-sop_zh.md`、
  `$W/docs/product-and-api_zh.md` 的「Memory 与 Agent MCP」一节（`MCP 问答（ask、get_ask）` 到「库理解」）。
- 本机经验（前缀 `/Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/`）：
  `mcp-interface-redesign-merged.md`（交付前按当下凭证重鉴权、新工具声明 tier 走唯一 add_tool 座位）、
  `mcp-answer-link-merged.md`、`mcp-ask-all-notebooks-star.md`、`mcp-ask-evidence-output-merged.md`、
  `retrieval-owns-permissions.md`（不在呈现层加权限兜底）、`postgres-is-primary-environment.md`、
  `local-heavy-jobs-serialize.md`、`local-pg-test-db.md`、`function-ceiling-counts-to-last-statement.md`、
  `pipeline-exit-code-masking.md`、`no-deferred-feature-parts.md`、`edit-tool-unicode-escape-nul.md`、
  `agent-token-access-edit-merged.md`。
- 只在 `$W` 里工作，所有命令用绝对路径；编辑用 Edit/Write，不用 shell 重定向。注释里描述 NUL 等字符用散文，
  别写 `\u0000` 转义，写完 `grep -nP '\x00' <文件>` 扫一遍。
- `frontend/node_modules`：先 `ls -l $W/frontend/node_modules`；不存在就
  `ln -s /Users/huzhifeng/workspace/silicon-notebook/frontend/node_modules $W/frontend/node_modules`，并把
  `frontend/node_modules` 追加进共享的 `$(git -C $W rev-parse --git-common-dir)/info/exclude`。**不得 `npm install`**。
- 重负载命令（`scripts/check.sh`、整条 PG 泳道、`-n` > 2 的 pytest、`next build`）一律经
  `bash /Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh <标签> -- <命令>`；
  单文件 pytest（`-n` ≤ 2）、单个 PG 测试文件（`-n 1`）、vitest 单文件、`tsc --noEmit` 可直接跑。
- 门禁退出码直接读，不用 `cmd | grep; echo $?`，也不要把「跑 && 贴」串成一条命令。
- PG 测试：本机一次性库（库名须匹配 `silicon_notebook_*_test`，装 `pg_trgm`、`btree_gin`），URL 带用户名，`-n 1`：
  ```bash
  psql -d postgres -c "CREATE DATABASE silicon_notebook_hist_test;"
  psql -d silicon_notebook_hist_test -c "CREATE EXTENSION pg_trgm;" -c "CREATE EXTENSION btree_gin;"
  cd $W/backend && TEST_POSTGRES_URL="postgresql://huzhifeng@localhost/silicon_notebook_hist_test" \
    python3 -m pytest <文件> -m postgres_integration -q -n 1
  ```
- 架构守卫（`python3 $W/scripts/check_architecture_boundaries.py`）：
  - facade **只许缩**：本计划不给 `RepositoryFacade` 加方法（新工具只用已有的 `refresh_agent_principal`、
    `require_agent_access`、`user_can_read_notebook`、`list_conversations`、`get_conversation`、`list_reports`、
    `get_report`）。新模块成为这些方法的新消费点后跑
    `python3 $W/scripts/generate_repository_contract_fixtures.py --rebaseline-surface`（重写 `ownership_manifest.py`
    消费点与 `facade_surface.json`），diff 只能是新增的 `ConsumerSite` 行。
  - `backend/app/repositories/ports.py` 不动，`ports_protocol_method_count` 不变；`global_ask_ports.py` 不在该计数内。
  - `function_length_ceiling` 清单里的函数一个都不碰。
  - 改了 HTTP 响应模型（T1）后跑不带参数的 `python3 $W/scripts/generate_repository_contract_fixtures.py`
    刷新 `backend/tests/fixtures/repository_contract/api_contract.json`；若 `caller_boundaries.json` 变红再跑
    `--rebaseline-callers`，并在报告里写明。
- 用户可见中文文案先过 `docs/ui-vocabulary.md`，改完跑 `python3 $W/scripts/check_ui_vocabulary.py`。
  MCP 错误统一 `AgentToolError(code, 中文)`；不新增 `user_error(...)` 站点（本计划没有新 HTTP 端点）。
- 不写延后项、不加 skip/xfail、不留 TODO。
- **中间态预期**：T4 / T3 注册新工具后，`backend/tests/test_architecture_documentation.py::test_current_mcp_docs_pin_the_complete_public_mcp_tool_surface`
  会红（文档工具表与计数要到 T6 才改），这是预期；T3/T4 的验收只跑各自列出的测试。T6 结束后必须全绿。
- 每个任务结束做一次本地提交（不 push），提交信息结尾带
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。报告里写：改了哪些文件、跑了哪些命令及其退出码、
  阻塞与存疑。

---

## 1. 冲突与落地决定

**D1 并入 `read` 档，新增一个能力名 `history:read`。** `backend/app/domain/agent_tools.py::AGENT_CAPABILITY_TIER`
加一行 `"history:read": "read"`。不复用 `knowledge:read`：调用记录（call ledger）按能力名记账、界面按能力名
翻译（`frontend/features/agent-profile/profile-model.ts::CALL_CAPABILITY_LABELS`），复用会把「读了问答记录」
显示成「查资料」。新增标签「读问答记录和报告」。五档与迁移冻结规则（`test_agent_token_tiers_migration.py`
只核主能力）不受影响；「每档至少开一个工具」不变。

**D2 历史读取与私人记忆同档，是被钉住的不变量，不写永远走不到的记忆闸。** 存下的单库回答可能来自开了
私人记忆通道的运行（`ask_jobs.memory_access`），报告可能用了作者的记忆。`get_ask` 对这类内容要求凭证
此刻有 `memory:read`（`ask.py::_notebook_delivery_gate`）。本计划的四个工具都要求 `history:read`，而
`history:read` 与 `memory:read` 都映射到 `read`：能过工具门的凭证必然能读记忆，记忆闸恒通过。做法：
`notebook_view(..., allow_memory=True)` 并在调用处注释指向新测试
`backend/tests/test_agent_history_capability.py::test_history_reads_share_the_memory_tier`；该测试断言
`capability_tier("history:read") == capability_tier("memory:read") == "read"`，失败信息写明「拆档时必须给
get_conversation / get_report 补上 get_ask 的记忆重放闸」。全局问答从不开私人记忆通道
（`test_global_ask_mcp.py` 已钉），不涉及。

**D3 单库会话的可见规则 = `ask` 接续 `conv-` 的规则，比网页详情严。** 主人 = `conversation_owner(id)`
（`deps.notebook_access_repository()`，与 `ask.py::_conversation` 同一入口）；会话所在笔记本须过
`require_agent_access(principal, "history:read", nb)`（凭证有效、`read` 档、在白名单、主人当前可读）。
网页 `GET /conversations/{id}`（`ask_routes.py:986`）只核创建者、不复核读权——MCP 选更严，理由：凭证是
长期委托，白名单与读权是它的天花板（与 `ask`/`get_ask` 一致）。回答里引用的挂载参考库随该笔记本可读，
不要求挂载库也在白名单里——与 `get_ask`、`read_reference` 口径一致（`read_reference` 打开时仍按参与集复核）。

**D4 「轮」的定义与顺序。** 单库会话：一轮 = 一条已保存的回答（`answers`，按 `CONVERSATION_ANSWERS_ORDER_ASC`），
进行中的提问（`active_job`）不算。全局会话：一轮 = 一个未过期且**已结束**的任务（`done`/`failed`/
`cancelled`/`interrupted`，按 `created_at ASC, id ASC`），`running` 不算——结束后自然追加在末尾，已有轮次的
`turn_offset` 不变。列表的 `turn_count` 与读取的 `turn_count` 同一口径。没有已结束轮次的会话照常列出，
`get_conversation` 返回 `status: "empty"` 页（§2.3）。

**D5 全局会话的可见规则（列表与读取同一谓词，整条会话判）。** 全局会话 C 对凭证 T 可见，当且仅当：
C 属于 T 的主人、至少有一个未过期任务（任意状态），且 C **全部未过期任务**「用到的库」的并集
⊆ T 的白名单 ∩ 主人当前可读的笔记本。「用到的库」逐轮取 `_snapshot_notebook_ids`（`services/global_ask.py:490`，
公开分享复核用的同一函数）：该轮的 `resolved_notebook_ids` ∪ `cited_notebook_ids` ∪ 回答每条锚点与引用上的
`notebook_id`（含旧 `response` 形状）。被删的库一律按「不可读」处理——与 `_check_turns` 对白名单调用方
从严的既有规则一致（`global_ask.py:662-700`：白名单调用方无法证明自己曾被授权那个被删的库）。
不可见 → 列表里不出现（标题、轮数都不出现），读取得到与「不存在」同文案的 `not_found`。
现行 `GlobalAskService.conversation(..., allowed_notebook_ids)` 只核**当前页**各轮的 `resolved`；本计划按用户
要求改成整条会话判、并把证据库也算进去（比现行更严，与公开分享同一函数）。读取时对取出的那一轮再跑一次
`_check(global_job_notebook_ids(job), ...)`，堵住「判定与取轮之间新任务结束」的竞态。

**D6 `submitted_via`（发起方）不加迁移，读时从 `ask_jobs` 推导。** 单库 `conversations` 表没有这列
（`migrations.py:767-774`），任务行 `ask_jobs.submitted_via` 有。核实：现行网页流式、网页同步 `POST /ask`、
MCP `ask` 都经 `begin_durable_job` / `begin_or_attach_durable_job` 先建任务行再存回答，所以新数据每条回答都有
任务行；更早的历史回答、导入数据可能没有。规则：会话级 = 该会话**最早**一条任务的 `submitted_via`，
**但若该会话存在 `created_at` 早于这条最早任务的回答则为 `""`**（老会话的前几轮没有任务行，之后经 MCP 续问才建出
第一条任务，它说明不了会话从哪里发起）；每轮 = `ask_jobs.answer_id` 指向该回答的任务的 `submitted_via`
（同一回答多条任务行取 `created_at` 最早的一条）；找不到任务行或列为空 → `""`（未记录）。
「最早」一律按 `created_at` 的时刻，而不是 id / 插入顺序。
因此 `get_conversation` 读回答**不**走 `_read_notebook_job`/`ask_job_detail`（那条路依赖任务行），而是读
`repo.get_conversation` 的 `ConversationDetail.turns`，再用 `notebook_view` 投影。落地为
`ConversationSummary.submitted_via` 与 `ConversationTurn.submitted_via` 两个新字段（默认 `""`，类型
`StoredSubmittedVia`），两端 store 的 `list_conversations` / `get_conversation` SQL 各加一个子查询/一次查询。
HTTP `GET /notebooks/{nb}/conversations`、`GET /conversations/{id}` 的响应因此多出这两个字段（只增不改）。
全局会话已有会话级（`GlobalConversationSummary.submitted_via`）与每轮（`GlobalAskJob.submitted_via`）。

**D7 `get_conversation` 一次返回一轮，不改 `answer_page`。** `get_ask` 的页（`ask_pages.answer_page`）已有
18–20 个顶层键，再加会话信息会超过 `OUTPUT_MAPPING_LIMIT`（20，`_shared.py:48`），且 `get_ask` 输出必须
逐字节不变。所以新写 `turn_page`：复用 `notebook_view` / `global_view` 产出的 `answer`、`citations`（每条带
`ref`：单库 `el`/`mem`、全局 `gel`），复用 `answer_page` 的「先缩引用、再缩正文、接受前核对游标与身份原样
留存」收敛循环；不带 `trace`、`coverage`、`anchors`、`intent`（需要时 MCP 发起的任务仍可用 `get_ask`）。
会话级信息放在 `conversation` 子对象里，与 `list_conversations` 的列表行**同一个构造函数、同一形状**。

**D8 报告读取需要 `notebook_id`。** 报告 id 不携带笔记本，`ReportStore.get_report(notebook_id, report_id)`
只按 (笔记本, id) 读；不为此加 facade 方法（facade 只许缩），也不用管理员那条带写围栏的
`guarded_report_detail`。所以 `get_report(report_id, notebook_id="")`：省略即凭证默认笔记本（与所有绑定单个
笔记本的工具同一约定），描述里写明「传 `list_reports` 返回的 `notebook_id`」。`ReportStore.get_report`
不查创建者，工具自己核 `row["created_by"] == principal.owner_id`，不等与不存在同一 `not_found`。

**D9 报告里的记忆。** 网页认证详情（`report_routes.py::get_report`）对作者原样给出正文与参考文献，包括
旧报告里可能存下的**他人**记忆摘录；`share_disclosure.py` 的记录也写明「the author already reads the
excerpts inside their own report」，而现行检索已不再让他人私人记忆进入报告。选择：**正文与摘录与网页一致**
（凭证主人本人的读取，按「检索层负责权限」的裁决，不在呈现层二次过滤），**标识符更严**：
- 参考文献永不输出 `memory_id`、`memory_owner_id`（`CITATION_MEMORY_RECORD_FIELDS`）、`provenance`、`family_key`、
  `memory_used` 等内部字段；
- 记录为他人记忆（`memory_id` 非空且 `memory_owner_id != owner`）的条目**不铸 `ref`**；
- 作者本人记忆：`object_type == "memory"` 的条目铸 `mem` ref（`n` = 报告所在笔记本，`m` = `object_id`），
  其余有 `source_id`+`element_id` 的条目铸 `el` ref；`read_reference` 打开时照常按属主与参与集复核
  （他人记忆来源对 `el` 也会被它拒绝）。
- 不套公开页的「引用他人记忆整份拒绝」与「挂载库复核」：那是匿名公开页的规则；认证读取的网页口径没有它们，
  `read_reference` 打开原文时已按当下参与集复核。
不选「更严」（例如整份拒绝）的理由：会让作者经 Agent 读不到网页上照常能读的自己的报告，且与
`retrieval-owns-permissions` 裁决相反；真正的泄漏面（打开原文）已由 `read_reference` 复核。

**D10 失败小节原文不经 MCP 输出。** 报告正文里旧格式的「（本节生成失败:<原始错误>）」用公开投影同一个函数
`neutralize_failure_notes(content_md, failed_errors)` 改写（`failed_errors` = `row["sections"]` 中 `failed is True`
的 `error`，非字符串记 `""`）；顶层 `error` 只在含中文（`_messages.has_cjk`）时原样给出，否则给中性文案。
与 `notebook_view`「失败任务的存储错误从不回显」同一原则。

**D11 列表工具的 `notebook_id` 省略 = 全部。** `list_conversations` / `list_reports` 省略 `notebook_id` 时列出
白名单内、主人当前仍可读的**全部**笔记本（逐个 `user_can_read_notebook`，读不了的静默跳过，同
`list_notebooks`）；传了则走 `_selected_notebook`（白名单外 `notebook_not_allowed`、失权 `notebook_unreadable`，
与其他绑定单个笔记本的工具一致）。这与「绑定单个笔记本的工具省略即默认笔记本」不同，在工具描述与文档
里明写。`list_conversations(notebook_id=X)` 返回 X 的单库会话，以及「用到的库」包含 X 的可见全局会话。

**D12 调用记录。** 带 `notebook_id` 的两个列表调用、`get_report`、单库 `get_conversation` 在全部闸门通过后
各记一条 `history:read`（`_selected_notebook` 自动记，或手动 `_record_agent_call`）；不带 `notebook_id` 的列表
与全局会话读取不记——同 `list_notebooks` 与 `gask-` 的 `get_ask`（没有单个笔记本可记）。

**D13 错误与鉴权顺序。** 参数形状错误（负 offset）在任何仓库工作之前 `invalid_argument`；然后
`_live_principal`（失效 `token_inactive`）→ 能力检查（缺档 `scope_missing`，点名「读取」）→ 存在性/归属/
白名单/读权。缺档必须先于任何存在性探测，避免无 `read` 的凭证借 id 探测。`get_conversation` 里会话隐含的
笔记本被拒（不在白名单/失权）一律 `not_found`（同 `ask` 接续 `conv-`）；`inactive`/`scope_missing` 原样上抛。

**D14 A 线扩展位。** `conversation_row(...)`（列表行与 `get_conversation.conversation` 共用）当前 8 个键；
`turn_page` 顶层 17 个键（加 `_budget_response` 自动加的 `truncation` 共 18），给 A 线留 2 个顶层键位（如每轮
`report_context`）。A 线加字段时要同时把新字段加进 `turn_page` 的接受条件（原样留存核对）。报告工具不受 A 线影响。

**D15 不变的东西。** `get_ask`（含它对浏览器任务的 `not_found`）、`answer_page`、`read_reference` 的鉴权、
HTTP 端点集合、前端交互（只改两处文案）、数据库 schema 全部不变。

---

## 2. 接口契约（各任务共同遵守）

### 2.1 能力与档位

```python
# backend/app/domain/agent_tools.py :: AGENT_CAPABILITY_TIER
"history:read": "read",   # list/get_conversation, list/get_report（D1/D2）
```

### 2.2 `list_conversations`

```text
list_conversations(ctx, notebook_id: str = "", limit: int = 20, offset: int = 0)   tier="read"
```
- `offset < 0` → `invalid_argument`「分页位置不能小于零，请从第一页重新读取」；`limit` 夹到 `1..RESULT_LIMIT`（同 `list_notebooks`）。
- 鉴权：`notebook_id` 非空 → `_selected_notebook(repo, notebook_id, "history:read")`；为空 → `list_pages.history_principal(repo)`
  （`_live_principal` + `principal_has_capability(p, "history:read")`，否则
  `AgentAccessDenied("scope_missing", tier=capability_tier("history:read"))`）。
- 数据：
  - 单库：对目标笔记本集合（D11）逐个在 `_owner_request_context(principal)` 里调 `repo.list_conversations(nb)`
    （facade 按当前用户 = 主人过滤；`KeyError` 跳过）。
  - 全局：`global_ask_service().agent_conversations(user_id=owner, allowed_notebook_ids=principal.notebook_ids)`（§2.7），
    有 `notebook_id` 时只留 `notebook_id in item.notebook_ids` 的。
  - 合并排序：`updated_at` 解析成 UTC 时刻降序（`app.core.activity_time.parse_activity_instant`，解析失败用
    `UNRESOLVED_INSTANT`），并列按 `conversation_id` 降序。
- 返回（经 `_budget_response`，与 `list_notebooks` 相同的「只按实际交付的条目前进」循环）：
  ```json
  {"items": [<conversation_row>...], "total": 37, "next_offset": 20, "truncation": {...}}
  ```
- `conversation_row`（一个构造函数，T3 定义在 `mcp_tools/conversations.py`）：
  ```json
  {"conversation_id": "conv-…|gconv-…", "kind": "notebook|global",
   "title": "<≤200 码点>", "notebook_ids": ["nb-…"], "notebook_count": 1,
   "turn_count": 3, "updated_at": "<ISO>", "submitted_via": "web|mcp|"}
  ```
  `notebook_ids`：单库 = `[会话所在笔记本]`；全局 = D5 的并集（排序，最多前 20 个），`notebook_count` 为全集大小。

### 2.3 `get_conversation`

```text
get_conversation(conversation_id: str, ctx, turn_offset: int = 0,
                 answer_offset: int = 0, citation_offset: int = 0)        tier="read"
```
- 任一 offset < 0 → `invalid_argument`（同上文案）。`conversation_id` 去首尾空白后不以 `conv-`/`gconv-` 开头、
  或超过 `ask.CONVERSATION_ID_MAX_LENGTH`（200）→ `not_found`（同 `get_ask` 对未知前缀）。以上都在仓库工作前。
- `conv-`（单库）：`history_principal` → `conversation_owner(cid) == owner` 否则 `not_found` →
  `repo.get_conversation(cid)`（`KeyError` → `not_found`）→ `repo.require_agent_access(p, "history:read", nb)`
  （`inactive`/`scope_missing` 原样抛，其余 `not_found`）→ 范围检查（下）→ `_record_agent_call(repo, p, nb, "history:read")`
  → 第 `turn_offset` 条回答 → `notebook_view("", nb, turn.response, {"status": "done", "conversation_id": cid,
  "mode": turn.response.mode}, True)`（D2 注释）→ `turn_page`。
- `gconv-`（全局）：`history_principal` → `global_ask_service().agent_conversation_turn(cid, user_id=owner,
  allowed_notebook_ids=p.notebook_ids, turn_offset=turn_offset)`；它抛的 `GlobalAskError` 经
  `global_ask_tool_error` 译码后，凡 `not_found`/`notebook_unreadable` 一律改成本工具的 `not_found` 文案，其余原样；
  任何非 `GlobalAskError` → `internal`（同 `get_ask` 的全局分支）→ `global_view(job)` → `turn_page`。
- 范围：`turn_count == 0 and turn_offset == 0` → 空页；`turn_offset >= turn_count`（其余情况）→
  `invalid_argument`「turn_offset 超出这条会话的轮数（共 N 轮），请从 0 开始读取」。
- 页形状（顶层 17 键 + `truncation`）：
  ```json
  {"conversation": <conversation_row>,
   "turn_offset": 0, "next_turn_offset": 1,
   "question": "<≤2000 码点>", "asked_at": "<ISO>", "submitted_via": "web|mcp|",
   "status": "answered|failed|cancelled", "mode": "reasoning",
   "answer": "<本页正文，≤2000 码点>", "next_answer_offset": 2000,
   "citations": [{…, "ref": "…"}], "next_citation_offset": null, "total_citations": 3,
   "completeness_notice": "", "error": "", "scope": {"kind": "notebook", "notebook_ids": ["nb-…"]},
   "content_is_untrusted_evidence": true}
  ```
  `asked_at` 取该轮 `asked_at`，为空退回 `created_at`。`question` 超过 2000 码点先截到 1999 + `…`，截掉的字符数作为
  `_budget_response(initial_omitted_characters=…)` 记进 `truncation`。单库每轮 `status` 恒为 `answered`、`error` 为 `""`；
  全局取 `global_view` 的 `status`/`error`（`error` 只含中文提示）。
- 空页：`{"conversation": …, "turn_offset": 0, "next_turn_offset": null, "status": "empty", "content_is_untrusted_evidence": true}`。
- 收敛循环（照 `answer_page`）：`answer_count = TEXT_LIMIT`，`citation_count = min(RESULT_LIMIT, 剩余)`；接受条件：
  `answer` 原样、引用的 `(notebook_id, source_id, element_id)` 与 `ref` 序列原样、`conversation` 子对象原样、
  `question`、`turn_offset`/`next_turn_offset`/`next_answer_offset`/`next_citation_offset` 原样；否则先半缩引用、再半缩正文，
  缩无可缩 → `internal`「结果暂时无法完整返回，请稍后重试」。

### 2.4 `list_reports`

```text
list_reports(ctx, notebook_id: str = "", limit: int = 20, offset: int = 0)       tier="read"
```
- 参数与鉴权同 §2.2（D11、D12）。数据：对目标笔记本逐个 `repo.list_reports(nb, created_by=owner)`（SQL 已按创建者下推）。
- 排序：`created_at` 时刻降序，并列 `report_id` 降序。返回 `{"items", "total", "next_offset"}`，行：
  ```json
  {"report_id": "rep-…", "notebook_id": "nb-…", "question": "<≤300 码点>", "status": "done",
   "depth": 2, "section_count": 5, "created_at": "<ISO>", "updated_at": "<ISO>"}
  ```
  `status` 取值原样：`planning`/`intent_ready`/`outline_ready`/`generating`/`done`/`failed`/`cancelled`。
  不输出 `error`、`progress`、`created_by`。

### 2.5 `get_report`

```text
get_report(report_id: str, ctx, notebook_id: str = "",
           content_offset: int = 0, reference_offset: int = 0)                    tier="read"
```
- 负 offset → `invalid_argument`；`report_id` 去空白后不以 `rep-` 开头或超过 200 → `not_found`（仓库工作前）。
- 鉴权：`_selected_notebook(repo, notebook_id, "history:read", record=False)`（白名单外/失权原样报
  `notebook_not_allowed`/`notebook_unreadable`；省略且无默认笔记本 → 既有 `invalid_argument`）→
  `repo.get_report(nb, report_id)`（`KeyError` → `not_found`）→ `created_by != owner` → `not_found`（同文案）→
  `_record_agent_call(repo, p, nb, "history:read")`。
- 正文：`neutralize_failure_notes(row["content_md"], failed_errors)`（D10）。参考文献：`row["references"]` 逐条投影：
  ```json
  {"key": "k3", "label": "<source_title|label|name，≤300>", "location_label": "<≤300>",
   "snippet": "<≤500>", "object_type": "element", "source_id": "src-…", "element_id": "el-…",
   "tier": "personal", "notebook_id": "<仅当来自挂载库且 ≠ 报告笔记本>", "ref": "<见 D9>",
   "content_is_untrusted_evidence": true}
  ```
- 页形状（顶层 16 键 + `truncation`）：
  ```json
  {"report_id", "notebook_id", "question": "<≤2000>", "status", "depth", "created_at", "updated_at",
   "content": "<本页正文>", "content_offset": 0, "next_content_offset": 6000, "total_characters": 23810,
   "references": [...], "next_reference_offset": 20, "total_references": 45,
   "error": "", "content_is_untrusted_evidence": true}
  ```
  `error`：`status == "failed"` 时，存储值含中文则原样，否则「这份报告没有生成完成，请在网页上重新生成」；其余状态 `""`。
- 收敛循环：`content_count` 初值 `6_000`（同 `citations._ELEMENT_PAGE_CHARS`），`reference_count = min(RESULT_LIMIT, 剩余)`；
  缩减顺序：正文半缩直到 ≤ 1_500 → 参考文献半缩到 1 → 正文继续半缩到 1 → `internal`。接受条件：`content` 原样、
  参考文献 `(key, source_id, element_id)` 与 `ref` 序列原样、`report_id`/`notebook_id`/`question`/各游标原样。
  传 `reference_offset >= total_references` 得到空参考文献页（只读正文时这样传），`content_offset` 同理。

### 2.6 错误文案（新常量，各放在所属工具模块里）

| 场景 | code | 文案 |
| --- | --- | --- |
| 会话不存在/非主人/不可见 | `not_found` | 没有找到这个会话（不存在、不属于此凭证的主人，或它用到的笔记本不在此凭证的白名单里、主人已无权读取） |
| 报告不存在/非创建者/笔记本不对 | `not_found` | 没有找到这份报告（不存在、不是此凭证的主人创建的，或不在这个笔记本里；请传 list_reports 返回的 notebook_id） |
| `turn_offset` 越界 | `invalid_argument` | turn_offset 超出这条会话的轮数（共 N 轮），请从 0 开始读取 |
| 负 offset | `invalid_argument` | 分页位置不能小于零，请从第一页重新读取（既有文案） |
| 列表缩无可缩 | `internal` | 会话列表暂时无法完整返回，请稍后重试 / 报告列表暂时无法完整返回，请稍后重试 |
| 失败报告的中性提示 | （字段值） | 这份报告没有生成完成，请在网页上重新生成 |

`mcp_tool_host._NOT_FOUND_RESOURCE` 同步加：`list_conversations`/`list_reports` → 「笔记本」，`get_conversation` →
「会话」，`get_report` → 「报告」（兜底裸 `KeyError` 的文案）。

### 2.7 store / service 新增

**单库会话（T1）**：`ConversationSummary.submitted_via`、`ConversationTurn.submitted_via`（D6）。

**`GlobalAskStore`（T2，`backend/app/repositories/global_ask_store.py`，一个实现服务两端，用 `self._sql` 与 `_dialect`）**：
```python
def all_conversations(self, user_id) -> list[GlobalConversationSummary]
    # list_conversations 的不分页版：同一 SELECT（含 shown_title 与 conversation_live），ORDER BY c.updated_at DESC, c.id DESC
def job_scope_rows(self, user_id, conversation_id=None) -> list[dict]
    # 每个未过期任务一行：{"conversation_id", "job_id", "status", "payload": <窄化 payload>}
    # ORDER BY conversation_id, created_at ASC, id ASC；只取 _snapshot_notebook_ids 读的字段，不取正文/轨迹/摘录
def finished_job_at(self, conversation_id, user_id, index) -> GlobalAskJob | None
    # 第 index 个（0 起、按 GLOBAL_JOBS_ORDER_ASC）status <> 'running' 的未过期任务；没有返回 None
```
`global_ask_ports.py` 的 `GlobalAskStorePort` 同步声明这三个方法（附一行注释）。

**`GlobalAskService`（T2，`backend/app/services/global_ask.py`）**：
```python
@dataclass(frozen=True)
class AgentGlobalConversation:
    summary: GlobalConversationSummary
    notebook_ids: tuple[str, ...]   # D5 的并集，排序
    turn_count: int                 # 已结束的未过期轮次数（D4）

def agent_conversations(self, *, user_id, allowed_notebook_ids) -> list[AgentGlobalConversation]
def agent_conversation_turn(self, conversation_id, *, user_id, allowed_notebook_ids, turn_offset)
    -> tuple[AgentGlobalConversation, GlobalAskJob | None]
```

---

## 3. 任务列表

依赖与并行：
- 第一波（文件集两两不相交，可同时起）：**T1**、**T2**、**T5**。
- **T4** 依赖 T1（能力名、`agent_tools.py`）；可在 T1 完成、T2 仍在跑时开始（与 T2 文件不相交）。
- **T3** 依赖 T1、T2、T4（T4 先改了 `mcp_tool_host.py`、`test_mcp_bundle_architecture.py`、`ownership_manifest.py`
  与公共分页助手，T3 在其上追加，必须串行）。
- **T6** 依赖 T3、T4、T5。**T7** 由主 agent 收口。

---

### T1 地基：能力名 + 单库会话的发起方字段

**目标**：D1、D2 的能力与不变量测试；D6 的两个模型字段与两端 SQL；HTTP 契约夹具刷新。

**改/建文件**（`$W/` 下）：
- `backend/app/domain/agent_tools.py`：`AGENT_CAPABILITY_TIER` 在 `"agent_profile:read": "read",` 后加
  `"history:read": "read",`，上方一行注释「读凭证主人自己的问答记录与深度报告（MCP list/get_conversation、
  list/get_report）；必须与 memory:read 同档，见 test_agent_history_capability.py」。
- 新建 `backend/tests/test_agent_history_capability.py`：
  - `test_history_reads_share_the_memory_tier`（D2 文案见上）；
  - `test_history_read_is_granted_by_read_alone`：`scopes_grant(["read"], "history:read")` 为真；`["ask","contribute","manage","delete"]` 为假。
- `backend/app/models/ask.py`：`ConversationSummary` 加 `submitted_via: StoredSubmittedVia = ""`（注释写 D6 的会话级规则），
  `ConversationTurn` 加同名字段（注释写每轮规则）。`ConversationDetail` 继承即可。
- `backend/app/repositories/sqlite/ask_state_store.py`：
  - `list_conversations`（`:1447`）SELECT 加模块级常量 `CONVERSATION_ENTRY_POINT_SQL`（D6：最早任务的
    `submitted_via`，`ORDER BY julianday(j.created_at), j.created_at, j.rowid LIMIT 1`；若有回答的 `created_at`
    早于最早任务则 `''`）`AS submitted_via`，构造时 `submitted_via=row["submitted_via"] or ""`。
  - `get_conversation`（`:1383`）：会话行查询同样带该常量；在同一连接里加一次
    `SELECT answer_id, submitted_via FROM ask_jobs WHERE conversation_id = ? AND answer_id <> '' ORDER BY julianday(created_at), created_at, rowid`
    建 `{answer_id: submitted_via}`（同一 answer 多行取第一行），每个 `ConversationTurn(..., submitted_via=…)`。
- `backend/app/repositories/postgres/ask_state_store.py`：`list_conversations`（`:1291`）与 `get_conversation`（`:1233`）
  同样两处，PG 排序写 `ORDER BY j.created_at, j.id COLLATE "C"`（与本文件既有写法一致）。走 `idx_ask_jobs_conv`，
  不绑定 id 清单。
- 刷新 `backend/tests/fixtures/repository_contract/api_contract.json`（不带参数跑生成器）；报告里列出生成器改动的全部文件，
  除 `api_contract.json` 外若有别的变化要说明原因。

**测试**：
- SQLite：`backend/tests/test_conversations.py` 加 `test_conversation_submitted_via_comes_from_its_jobs`：
  ① 一条会话第一轮任务 `web`、第二轮任务 `mcp` → 列表与详情的会话级为 `web`，两轮分别 `web`/`mcp`；
  ② 有回答但没有任务行的会话（直接写 `conversations` + `answers`）→ 会话级与该轮都是 `""`；
  ③ `HTTP GET /api/conversations/{id}` 的 JSON 带这两个字段（若该文件没有 HTTP 夹具，就在
  `backend/tests/test_ask_jobs.py` 里已有 `TestClient` 的地方加一条）。
- PG：`backend/tests/postgres/test_content_store_conformance.py` 加同名用例（用 `content_harness.ask`，
  `begin_durable_job(..., submitted_via=…)` + `save_answer` + `finish_job`；无任务行的那条直接写 SQL）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_agent_history_capability.py tests/test_conversations.py \
  tests/test_ask_jobs.py tests/test_repository_api_contract.py tests/test_agent_token_tiers_migration.py -q -n 2
cd $W/backend && TEST_POSTGRES_URL=... python3 -m pytest tests/postgres/test_content_store_conformance.py -m postgres_integration -q -n 1
python3 $W/scripts/check_architecture_boundaries.py
```

**验收**：两个字段在两端行为一致；HTTP 响应只增字段；能力映射与不变量测试在位。

---

### T2 全局会话：整条会话可见性 + 按序取轮（store + service，两端）

**目标**：§2.7 的三个 store 方法与两个 service 方法，实现 D4、D5。

**改文件**：
- `backend/app/repositories/global_ask_store.py`：
  - `all_conversations(user_id)`：复制 `list_conversations`（`:145`）的 SELECT，去掉 `LIMIT ? OFFSET ?`。
  - `job_scope_rows(user_id, conversation_id=None)`：只读下列 10 个 JSON 投影 + `conversation_id, id, status`，
    `WHERE user_id=? [AND conversation_id=?] AND {self._live}`，`ORDER BY conversation_id, created_at ASC, id ASC`。
    投影（`X` 为 `answer` 或 `response`）：顶层 `resolved_notebook_ids`、`cited_notebook_ids`；`X.resolved_notebook_ids`、
    `X.cited_notebook_ids`；`X.anchors[*].notebook_id`、`X.citations[*].notebook_id`（仅当该路径是数组，否则空数组）。
    - SQLite：`json_extract(payload_json,'$.resolved_notebook_ids')` 等；数组元素用
      `CASE json_type(payload_json,'$.answer.anchors') WHEN 'array' THEN (SELECT json_group_array(json_extract(e.value,'$.notebook_id')) FROM json_each(payload_json,'$.answer.anchors') e) ELSE '[]' END`。
    - PG：子查询里 `payload_json::jsonb AS p` 只解析一次（同 `attribution_sql` 的 `document="p"` 思路）；
      `p->'resolved_notebook_ids'` 等；数组元素用
      `(SELECT COALESCE(jsonb_agg(e->'notebook_id'), '[]'::jsonb) FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p->'answer'->'anchors')='array' THEN p->'answer'->'anchors' ELSE '[]'::jsonb END) e)`。
      不用 `jsonb_path_*`（仓库里没有先例）。
    - Python 侧重建窄化 payload：值是 JSON 文本就 `json.loads`，**是 list 才保留（元素原样）、否则记 `None`**（让
      `_payload_notebook_ids` 的「顶层为空就退到嵌套形状」行为不变）；锚点/引用的每个元素 `x`（含 null）原样重建为
      `{"notebook_id": x}`，过滤交给 `_turn_evidence_notebook_ids` 自己做——store 里不另写过滤规则。
      ```python
      {"resolved_notebook_ids": r0, "cited_notebook_ids": c0,
       "answer":   {"resolved_notebook_ids": r1, "cited_notebook_ids": c1, "anchors": a1, "citations": z1},
       "response": {"resolved_notebook_ids": r2, "cited_notebook_ids": c2, "anchors": a2, "citations": z2}}
      ```
    - docstring 写明：这是 `_snapshot_notebook_ids` 的窄化输入，parity 测试钉住两者相等；为什么不取整个 payload（正文与轨迹）。
  - `finished_job_at(conversation_id, user_id, index)`：
    `SELECT * FROM global_ask_jobs WHERE conversation_id=? AND user_id=? AND status<>'running' AND {self._live} {GLOBAL_JOBS_ORDER_ASC} LIMIT 1 OFFSET ?`，
    `self._job(row)`；`index < 0` 由调用方保证不会传入。
- `backend/app/repositories/global_ask_ports.py`：`GlobalAskStorePort` 声明三方法 + 注释。
- `backend/app/services/global_ask.py`：
  - `AgentGlobalConversation` 数据类（§2.7）放在 `GlobalAskService` 之前。
  - 私有 `_agent_scopes(rows)` → `{conversation_id: (set[notebook_id], finished_count)}`，逐行
    `_snapshot_notebook_ids([row["payload"]])` 求并（**直接调用既有函数，不另写规则**）；`status != "running"` 计入 finished。
  - 私有 `_agent_readable(ids, user_id) -> set[str]`：与 `_check` 同一读法（`can_read_many` 优先，否则逐个 `can_read`），一次批量。
  - `agent_conversations`：`all_conversations` + `job_scope_rows(user_id)` → 所有并集一次 `_agent_readable` →
    可见 = 有 ≥1 个未过期任务 且 并集 ⊆ `set(allowed)` 且 ⊆ readable；按 `all_conversations` 的顺序返回。
  - `agent_conversation_turn`：`self._conversation(cid, user_id)`（不存在/非主人 404）→ `job_scope_rows(user_id, conversation_id=cid)`
    → 不可见 → `GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。")` → `0 <= turn_offset < finished_count`
    时 `finished_job_at`，取到后 `self._check(global_job_notebook_ids(job), user_id, allowed_notebook_ids)`（D5 竞态）→ 返回。
  - 两个方法 docstring 写「无 request user：身份全部显式传入」与 D5 规则摘要；不改 `conversation()`、`get_job()` 等既有方法。

**测试**（场景写在共享模块，两端各跑一遍）。播种注意：全局会话 id 一律用 `gconv-` 前缀（`insert_conversation`
默认的 `conv-g` 会被 MCP 工具当成单库会话路由）；`insert_job` 要传**完整**的 `GlobalAskJob` JSON（`job_id`、
`conversation_id`、`status`、`question`、`created_at`、`notebook_scope`、`resolved_notebook_ids`，按需 `answer`），
它的默认 payload 过不了 `GlobalAskJob.model_validate_json`。在共享模块里写一个 `global_job_payload(...)` 小工厂。
- store 级：新建 `backend/tests/global_ask_history_store_cases.py`（`CASES` 列表，入参 `store`；用
  `tests.global_ask_share_cases.insert_conversation/insert_job` 播种），SQLite `backend/tests/test_global_ask_history_store.py`
  （夹具照 `test_global_ask_share_store.py`）、PG `backend/tests/postgres/test_global_ask_history_store_pg.py`
  （照 `postgres/test_global_ask_share_store.py`，`migrate()` 断言写当前最新号）：
  - `scope_projection_matches_the_snapshot_rule`：新形状（`answer`，锚点+引用各带库）、旧 `response` 形状、顶层空数组
    退嵌套、锚点无 `notebook_id`（外部证据）、`answer` 为 null、`anchors` 是对象而非数组 → 每种
    `_snapshot_notebook_ids([row["payload"]]) == _snapshot_notebook_ids([原 payload])`；
  - `scope_rows_skip_expired_turns_and_filter_by_conversation`；
  - `finished_job_at_counts_oldest_first_and_skips_running`（含 `interrupted`、并列 `created_at` 按 id）；
  - `finished_job_at_past_the_end_is_none`；
  - `all_conversations_is_the_unpaged_list`（与 `list_conversations(user, 100, 0)` 同序同集，过期会话不在）。
- service 级：新建 `backend/tests/global_ask_agent_history_cases.py`（场景写成 `async def case(env)`，经
  `tests.mcp_answer_link_cases.run_case` 在一个 app lifespan 内跑——PG 连接池随 lifespan 关闭；夹具照
  `test_mcp_answer_links.py` / `postgres/test_mcp_answer_links_pg.py` 用 `build_env`；用户、笔记本、组授权用
  `tests.answer_link_service_cases._user/_notebook/_write`；全局任务用 `insert_conversation/insert_job` 写进
  `env.repo._runtime.global_ask_store`，被测对象是 `env.global_service`），SQLite
  `backend/tests/test_global_ask_agent_history.py`、PG `backend/tests/postgres/test_global_ask_agent_history_pg.py`：
  - `conversation_inside_the_allowlist_is_listed_with_its_libraries_and_finished_count`（含一个 running 任务不计数）；
  - `one_turn_outside_the_allowlist_hides_the_whole_conversation`（第 1 轮在白名单内、第 2 轮 resolved 含白名单外库：
    列表不含，`agent_conversation_turn(turn_offset=0)` 也 404）；
  - `evidence_library_outside_the_allowlist_hides_it`（resolved ⊆ 白名单，但一条锚点的 `notebook_id` 在外）；
  - `owner_losing_read_hides_it_and_restoring_shows_it`（组授权撤销/恢复，照 `answer_link_service_cases.owner_lost_read_is_none`）；
  - `deleted_library_hides_it_for_an_allowlist_reader`（被删库 + `retained_until` 未过期的轮次）；
  - `another_users_conversation_is_invisible`；
  - `only_running_turn_is_visible_with_zero_turns_and_no_job`；
  - `turn_is_read_oldest_first`。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_global_ask_history_store.py tests/test_global_ask_agent_history.py \
  tests/test_global_ask_share_store.py tests/test_global_ask.py -q -n 2
cd $W/backend && TEST_POSTGRES_URL=... python3 -m pytest tests/postgres/test_global_ask_history_store_pg.py \
  tests/postgres/test_global_ask_agent_history_pg.py tests/postgres/test_global_ask_store.py -m postgres_integration -q -n 1
python3 $W/scripts/check_architecture_boundaries.py
```

**验收**：D5 规则只有一处实现（`_snapshot_notebook_ids`），列表与取轮共用；两端用例全绿；既有全局问答用例不变。

---

### T3 MCP 会话工具：`list_conversations`、`get_conversation`（依赖 T1、T2、T4）

**目标**：§2.2、§2.3。

**改/建文件**：
- 新建 `backend/app/api/mcp_tools/conversations.py`：
  - 常量：§2.6 的会话文案、`_QUESTION_CHARS = 2_000`、`_TITLE_CHARS = 200`、`_LIST_NOTEBOOK_IDS = 20`、`_TURN_FIELD_LIMITS`
    （在 `ask_pages._FIELD_LIMITS` 基础上把 `question` 设为 2_000、加 `title: 200`）。
  - 复用 T4 建的 `mcp_tools/list_pages.py`：`history_principal`、`readable_allowlist`、`instant_key`、`paged_items`，
    不另写一份。
  - `conversation_row(...)`（§2.2 形状，D14 的唯一构造点）；`_notebook_rows(...)`、`_global_rows(...)`；排序键
    `(instant_key(updated_at), conversation_id)` 降序。
  - `turn_page(conversation, view, *, question, asked_at, submitted_via, turn_offset, turn_count, answer_offset, citation_offset)`
    与 `empty_turn_page(conversation)`（§2.3），从 `ask_pages` 引 `_FIELD_LIMITS`、从 `global_ask` 引 `_citation_keys`。
  - `register_conversation_tools(server, repository_provider)`：两个工具，`tier="read"`，每个 handler 体内恰好一次
    `_run_with_progress`（`test_each_public_handler_has_one_progress_wrapped_main_body`）；全部同步仓库工作放在 `load()` 里。
  - 英文描述（照现有工具的写法，各写清）：
    - `list_conversations`：你自己的会话（网页发起的与 Agent 经 MCP 发起的都在），单库 `conv-` 与跨库 `gconv-`，最近活动在前；
      每行 title、notebook_ids、turn_count、updated_at、submitted_via（web/mcp/空=未记录）；`notebook_id` 省略=全部白名单内
      仍可读的笔记本（不同于绑定单笔记本工具的「省略=默认笔记本」），传了只列该笔记本的会话及用到它的跨库会话；跨库会话
      仅当每一轮用到的笔记本都在白名单内且仍可读才列出；`limit`(≤20)/`offset`，沿 `next_offset` 到 null；用 `get_conversation`
      读内容；Requires the read permission。
    - `get_conversation`：一次一轮、旧的在前：问题、回答（`answer_offset`/`next_answer_offset`）、引用（每条带 `ref` 给
      `read_reference`，`citation_offset`/`next_citation_offset`），然后 `turn_offset=next_turn_offset` 且两个 offset 归零；
      浏览器里发起的会话同样可读（`get_ask` 只重读 MCP 发起的任务）；进行中的提问结束后才出现；每次调用重查凭证、白名单与读权；
      Requires the read permission。
- `backend/app/api/mcp_tool_host.py`：`_CORE_REGISTRARS` 在 `register_ask_tools` 之后插入 `register_conversation_tools`
  （T4 已在其后放了 `register_report_tools` 时，顺序为 ask → conversations → reports）；`_NOT_FOUND_RESOURCE` 加两项（§2.6）。
- `backend/tests/test_mcp_bundle_architecture.py`：import 并把 `register_conversation_tools` 放进 `_REGISTRARS` 同一位置。
- `backend/app/api/mcp_tools/refs.py`：`_INVALID_REF` 改为「ref 无法解析：请原样传入 search、ask、get_ask、get_conversation 或
  get_report 返回的 ref」；`backend/app/api/mcp_tools/citations.py::read_reference` 描述首句同步列出这两个工具
  （若有测试钉了旧文案，一并更新断言，不放宽）。
- `--rebaseline-surface` 刷新 `ownership_manifest.py` / `facade_surface.json`（只增 `ConsumerSite`）。

**测试**：
- 纯函数：新建 `backend/tests/test_mcp_conversation_pages.py`：
  - `test_turn_page_fits_the_budget_and_cursors_resume_exactly`（5,000 字中文正文 + 45 条引用：按游标读完，拼回正文逐字相等、
    引用不重不漏，每页 `_serialized_size` ≤ 12,000）；
  - `test_turn_page_keeps_every_ref_whole`；
  - `test_long_question_is_clipped_and_reported`（4,000 字问题 → 2,000 码点含 `…`，`truncation.omitted_characters` 计入）；
  - `test_turn_page_has_room_for_two_more_keys`（顶层键数 ≤ 18，含 `truncation`）；
  - `test_conversation_row_is_one_shape_for_both_kinds`；`test_list_order_parses_mixed_timestamp_spellings`。
- 行为（两端）：新建 `backend/tests/mcp_conversation_cases.py`（`CASES` dict；复用 `tests.mcp_answer_link_cases` 的
  `build_env/run_case/_clients/_payload/_error`、`tests.answer_link_service_cases` 的 `_user/_notebook/_write/_seed_job`，
  全局用 `insert_conversation/insert_job` + T2 的 `global_job_payload`（`gconv-` 前缀）；引用要能被 `read_reference`
  打开时，用 `tests.citation_check_testkit.seed_source(db, "?", notebook_id=…, source_id=…, elements={…})` 在
  `with _write(env) as db:` 里种真实来源元素（`_write` 会把 `?` 译成 PG 占位符）；凭证用
  `env.repo.issue_agent_token(..., ["read"], ...)`），SQLite
  `backend/tests/test_mcp_conversations.py`、PG `backend/tests/postgres/test_mcp_conversations_pg.py`（照
  `postgres/test_mcp_answer_links_pg.py`）：
  - `browser_conversation_is_listed_and_readable_through_refs`：`_seed_job(..., via="web")` 的会话出现在列表
    （`kind="notebook"`、`submitted_via="web"`、`turn_count`），`get_conversation` 读到问题、正文、引用；引用的 `ref`
    交给 `read_reference` 能打开；同一任务 `get_ask` 仍 `not_found`（D15 回归钉）。
  - `submitted_via_tells_web_from_mcp`。
  - `another_users_conversation_is_invisible_and_indistinguishable`：同一共享笔记本里成员 B 的会话不在 A 的列表；
    `get_conversation(B 的 id)` 与 `get_conversation("conv-nonexistent")` 的错误码与文案完全相同。
  - `notebook_outside_the_allowlist`：不在列表；`get_conversation` → `not_found`；`list_conversations(notebook_id=该库)` → `notebook_not_allowed`。
  - `owner_lost_read`：撤组授权后不在列表；`get_conversation` → `not_found`；`list_conversations(notebook_id=…)` → `notebook_unreadable`；恢复后回来。
  - `global_conversation_inside_the_allowlist`：列表 `kind="global"`、`notebook_ids`；读到的引用带 `gel` ref，`read_reference` 能打开（浏览器发起的全局任务）。
  - `global_conversation_crossing_the_allowlist_never_shows_its_title`：越界会话的标题字符串不出现在 `list_conversations`
    任何一页的序列化结果里；`get_conversation(turn_offset=0)` → `not_found`（即使第 0 轮本身在白名单内）。
  - `downgrade_to_no_read`：`update_agent_token_access` 把档位改成只剩 `ask` → 四个工具中两个会话工具都 `scope_missing` 且文案点名「读取」；
    `tools/list` 不再列出它们；改回后恢复。
  - `list_pages_are_exact`：7 条会话 `limit=3` → `next_offset` 3/6/null、`total=7`、不重不漏；`notebook_id` 过滤只留该库会话与用到它的全局会话。
  - `turn_paging_is_exact`：3 轮会话按 `next_turn_offset` 读完；长正文（5,000 字）按 `next_answer_offset` 拼回逐字相等；
    `turn_offset=3` → `invalid_argument` 且文案含「共 3 轮」；负 offset 在仓库工作前 `invalid_argument`。
  - `empty_conversation_returns_the_empty_page`（只有 running 任务的单库会话：`answers` 为空）。
  - `own_memory_citation_opens_through_mem_ref`：回答带主人本人记忆引用（`memory_id`）→ 引用带 `mem` ref，`read_reference` 打开。
  - `notebook_read_books_one_history_call`：开着调用记录（默认配置）时单库 `get_conversation` 在该库记一条 `history:read`，
    全局读取与不带 `notebook_id` 的列表不记（读 `agent_observations` 的 `kind='call'` 行计数）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_mcp_conversation_pages.py tests/test_mcp_conversations.py \
  tests/test_mcp_bundle_architecture.py tests/test_agent_mcp_onboarding.py tests/test_mcp_answer_links.py -q -n 2
cd $W/backend && python3 -m pytest tests/test_memory_mcp.py tests/test_global_ask_mcp.py -q -n 2 -k "ask or reference"
cd $W/backend && TEST_POSTGRES_URL=... python3 -m pytest tests/postgres/test_mcp_conversations_pg.py -m postgres_integration -q -n 1
python3 $W/scripts/check_architecture_boundaries.py
```

**验收**：§2.2/§2.3 形状与错误逐条兑现；D3/D4/D5/D12/D13 都有用例；`get_ask` 输出不变（`test_mcp_answer_links.py` 的
`inline_default_matches_the_pre_link_shape` 等既有用例全绿）。

---

### T4 MCP 报告工具：`list_reports`、`get_report`（依赖 T1）

**目标**：§2.4、§2.5，D8、D9、D10。

**改/建文件**：
- 新建 `backend/app/api/mcp_tools/list_pages.py`（两个工具模块共用的小助手，T3 复用）：
  - `history_principal(repo)`：§2.2 的「无 `notebook_id`」鉴权（`_live_principal` + 能力检查）。
  - `readable_allowlist(repo, principal) -> list[str]`：白名单内 `user_can_read_notebook(nb, owner)` 为真的笔记本（保持白名单顺序）。
  - `instant_key(value) -> datetime`（`parse_activity_instant`，失败给 `UNRESOLVED_INSTANT`）。
  - `paged_items(rows, *, limit, offset, id_key, field_limits, failure)`：把 `session.py:55-82` 的「只按实际交付的身份前进」
    循环抽成函数（`list_notebooks` 本身**不改**，避免碰与本特性无关的代码）。
- 新建 `backend/app/api/mcp_tools/reports.py`：
  - 常量：§2.6 的报告文案、失败中性提示、`_REPORT_PAGE_CHARS = 6_000`、`_CONTENT_FLOOR = 1_500`、字段上限（question 2,000、
    label/location_label 300、snippet 500）。
  - `_report_row(row)`（§2.4 行形状）、`_reference_row(reference, *, notebook_id, owner_id)`（§2.5 + D9：`MEMORY_OBJECT_TYPE`
    取自 `app.services.share_disclosure`，记录字段名取自 `app.domain.share_disclosure.CITATION_MEMORY_RECORD_FIELDS`；
    `ref` 用 `refs.memory_ref` / `refs.element_ref`）、`_failed_errors(sections)`、`_report_error(row)`、`report_page(...)`（§2.5 收敛循环）。
  - `register_report_tools(server, repository_provider)`：两个工具，`tier="read"`，各一次 `_run_with_progress`。
  - 英文描述：
    - `list_reports`：你自己创建的深度报告（只列凭证主人创建的），新的在前，每行 status、question、depth、section_count、时间；
      `notebook_id` 省略=全部白名单内仍可读的笔记本，传了只列该库；`limit`/`offset`；Requires the read permission。
    - `get_report`：按 `report_id`（rep-…）与 `notebook_id`（传 `list_reports` 给的；省略=凭证默认笔记本）读一份你的报告：Markdown
      全文按 `content_offset`/`next_content_offset` 分页，正文里 `[kN]` 对应的参考文献按 `reference_offset`/`next_reference_offset`
      分页，能打开原文的条目带 `ref` 给 `read_reference`；只读其中一部分时把另一个 offset 设为对应 total；Requires the read permission。
- `backend/app/api/mcp_tool_host.py`：`_CORE_REGISTRARS` 在 `register_ask_tools` 之后插入 `register_report_tools`；`_NOT_FOUND_RESOURCE`
  加两项（§2.6）。
- `backend/tests/test_mcp_bundle_architecture.py`：`_REGISTRARS` 同步。
- `--rebaseline-surface` 刷新 `ownership_manifest.py` / `facade_surface.json`。

**测试**：
- 纯函数：新建 `backend/tests/test_mcp_report_pages.py`：
  - `test_report_page_reads_the_whole_body_by_cursor`（20,000 字中文正文 + 45 条参考文献：拼回逐字相等，每页 ≤ 12,000 字节，
    首页正文不少于 1,500 码点时才开始缩参考文献）；
  - `test_reference_projection_never_carries_memory_identity_or_internals`（输出键集合 ⊆ §2.5 列出的键）；
  - `test_foreign_memory_reference_gets_no_ref_but_keeps_its_excerpt`；`test_own_memory_object_gets_a_mem_ref`；
  - `test_mounted_library_reference_names_its_library`；
  - `test_raw_failure_note_is_neutralized_and_error_is_neutral`（正文含「（本节生成失败:Traceback …）」、`sections` 带
    `failed: True, error: "Traceback …"` → 输出不含 `Traceback`，含 `SECTION_FAILURE_COPY` 的中性句；英文 `error` → 中性提示）；
  - `test_paged_items_advances_only_past_delivered_rows`。
- 行为（两端）：新建 `backend/tests/mcp_report_cases.py` + SQLite `backend/tests/test_mcp_reports.py` + PG
  `backend/tests/postgres/test_mcp_reports_pg.py`（复用 `tests.mcp_answer_link_cases` 的 `build_env/run_case/_clients/_payload/_error`
  与 `tests.answer_link_service_cases` 的 `_user/_notebook/_write`；被引来源用 `tests.citation_check_testkit.seed_source`
  在 `with _write(env) as db:` 里种；报告用 `_write(env)` 直接 `INSERT INTO reports(...)`，
  `created_by`、`content_md`、`references_json`、`sections_json`、`status` 显式给出；PG 夹具照 `postgres/test_mcp_answer_links_pg.py`）：
  - `own_report_is_listed_and_readable_through_refs`（`el` ref 能被 `read_reference` 打开）；
  - `another_members_report_is_invisible`：共享笔记本里成员 B 的报告不在 A 的列表；`get_report(B 的 id, notebook_id=该库)`
    → `not_found`，且与不存在的 id 同码同文案（钉住 `ReportStore.get_report` 不查创建者、由工具自己核）；
  - `report_in_a_notebook_outside_the_allowlist`：不在列表；`get_report(..., notebook_id=该库)` → `notebook_not_allowed`；
  - `owner_lost_read`：不在列表；`get_report` → `notebook_unreadable`；恢复后回来；
  - `wrong_notebook_id_is_not_found`；`default_notebook_is_used_when_omitted`；
  - `downgrade_to_no_read`：两个报告工具 `scope_missing`（点名「读取」）、`tools/list` 不再列出；
  - `list_pages_are_exact`（5 份报告 `limit=2`；`notebook_id` 过滤）；`content_and_references_page_exactly`；
  - `report_get_books_one_history_call`。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_mcp_report_pages.py tests/test_mcp_reports.py \
  tests/test_mcp_bundle_architecture.py tests/test_agent_mcp_onboarding.py -q -n 2
cd $W/backend && TEST_POSTGRES_URL=... python3 -m pytest tests/postgres/test_mcp_reports_pg.py -m postgres_integration -q -n 1
python3 $W/scripts/check_architecture_boundaries.py
```

**验收**：§2.4/§2.5 形状与错误逐条兑现；D8/D9/D10/D12/D13 都有用例；未改 `list_notebooks`。

---

### T5 前端：「读取」档说明与调用记录标签（可与 T1/T2 并行）

**目标**：Agent 接入页「读取」档文案写进问答记录与深度报告；调用记录认得 `history:read`。

**改文件**：
- `frontend/app/agent-token-model.ts:9`：`description` 改为
  「读取笔记本里的资料、你的私人记忆、AI 对这个库的理解，以及你的问答记录和深度报告」。
- `frontend/features/agent-profile/profile-model.ts::CALL_CAPABILITY_LABELS`：加 `"history:read": "读问答记录和报告"`。
- `frontend/tests/component/agent-access-manager.component.test.tsx:703`：在原断言后加一条断言描述同时含「问答记录和深度报告」。
- `frontend/tests/unit/agent-profile-observations-model.test.mjs`：加 `callCapabilityLabel("history:read") === "读问答记录和报告"`。

**验证**：
```bash
cd $W/frontend && node --test tests/unit/agent-profile-observations-model.test.mjs
cd $W/frontend && npx vitest run tests/component/agent-access-manager.component.test.tsx
cd $W/frontend && npx tsc --noEmit
python3 $W/scripts/check_ui_vocabulary.py
cd $W/backend && python3 -m pytest tests/test_mcp_bundle_architecture.py -q -k frontend_scope
```

**验收**：两处文案上屏正确；`AGENT_SCOPE_OPTIONS` 的 value 集合不变（后端守卫仍绿）。

---

### T6 文档、SOP、接入说明、smoke 与更新说明（依赖 T3、T4、T5）

**目标**：只写已落地且验证过的行为；中英文配对；文档守卫全绿。

**改/建文件**：
- `docs/product-and-api_zh.md` / `docs/product-and-api.md`：
  - 五档定义（zh `:1264` 一带、en 对应句）：`read` 加「主人本人的问答记录（单库与全局会话）与深度报告」。
  - 工具表（zh `:1379-1391`、en `:1850-1864`）加一行：`问答记录与报告（只读，仅凭证主人自己的）` |
    `list_conversations`、`get_conversation`、`list_reports`、`get_report` | `read`。
  - 计数：「17 个 core 工具」「十七个工具」「这 17 条」「17 个工具，由九个 core registrar」改为 21 / 二十一 / 十一个 registrar
    （zh `:1316`、`:1375-1376`、`:1394`；en `:1768`、`:1847`、`:1865`）。
  - 在「限时回答链接」一段之后（zh `:1575-1600` 之后、`ref` 段之前；en 同位置）新增
    「**问答记录与深度报告（`list_conversations`、`get_conversation`、`list_reports`、`get_report`）。**」一段：§2.2–§2.5 的
    参数、形状、分页、`notebook_id` 省略语义（D11）、D3/D4/D5 可见规则、D6 发起方含义、D8 报告需 `notebook_id`、D9 记忆与
    标识符、D10 失败小节、D12 调用记录、错误码；明写「`get_ask` 只重读经 MCP 发起的任务；浏览器里的问答用 `get_conversation`」。
  - `get_ask` 段（zh `:1471-1475` 一带）补一句同上的指路。
  - `ref` 段（zh `:1602`）：带 `ref` 的来源加上 `get_conversation`、`get_report`。
  - HTTP 清单（zh `:3212`、en `:4219`）：会话列表与详情的摘要带 `submitted_via`（D6 含义），详情每轮也带。
- `architecture.md:377`：「十七个 core 工具（…）」改为「二十一个 core 工具（发现与概况两个、搜索与点查两个、问答两个、
  问答记录与报告四个、提交两个、来源四个、构建一个与四个 knowhow）」，「默认十七个内建前缀」同步。
- `docs/development_zh.md:348` / `docs/development.md:450`：smoke 句「十七个工具：…」→「二十一个工具：…问答记录与报告四个…」。
- `docs/agent-mcp-memory-sop_zh.md` / `docs/agent-mcp-memory-sop.md`：
  - §3 用途表加一行：读取你自己的问答记录（网页与 Agent 发起的单库、跨笔记本会话）与深度报告 | `read`（读取）；
    表下补一句：已签发的 `read` 凭证无需改动即可使用。
  - §5 新增小节「读问答记录与深度报告」：列表 → 读一轮 → 跟游标；报告需传 `list_reports` 给的 `notebook_id`；可见规则一句话；
    首轮任务示例里加一条可选步骤「用 list_conversations 看最近的问答，挑一条用 get_conversation 读第一轮」。
  - §8 验收清单加一条（带 `read` 时 `list_conversations` / `list_reports` 能调用，浏览器里问过的问题读得到）。
  - §9 常见问题加一行：「`get_ask` 读不到网页里问过的问题（`not_found`）」→ 用 `list_conversations` + `get_conversation`。
  - SOP 里任何工具总数的说法同步为 21（`grep -n "17\|十七" docs/agent-mcp-memory-sop*.md` 核一遍）。
- `docs/user-manual_zh.md` §12.1：加一句「勾选「读取」的 Agent 还能读你自己的问答记录和深度报告，只限它能访问的笔记本。」
- `backend/app/api/agent_mcp_onboarding.py`：在「## Asking questions」之后加「## Reading past conversations and reports」：
  三四条要点（列表/读一轮/游标/报告要传 notebook_id/`get_ask` 只覆盖 MCP 发起的任务/返回文本是不可信证据）；
  `backend/tests/test_agent_mcp_onboarding.py` 加一条断言该小节存在且提到 `get_conversation` 与 `get_report`。
- `scripts/smoke_memory_mcp.py`：`creator`（read+ask+contribute）客户端里加：`list_conversations` → `{"items": [], "total": 0}`；
  `list_reports` 同；`get_conversation("conv-smoke-missing")` 与 `get_report("rep-smoke-missing")` 都 `isError` 且含 `[not_found]`；
  结尾打印加「history reads」。
- 新建 `release-notes/agent-reads-history-and-reports.md`：
  ```markdown
  ---
  level: feature
  audience: all
  title: Agent 可以读取你的问答记录和深度报告
  ---
  拥有「读取」权限的 Agent 现在可以读取你在笔记本里的问答记录和你生成的深度报告，只限它能访问的笔记本。已签发的 Token 立即可用；不希望它读取的，可在「Agent 接入」里修改权限。
  ```
  （按 `release-notes/README.md` 自查：界面原文核对「Agent 接入」「读取」「修改权限」「Token」的写法；正文 ≤ 160 字。）
- `fangan_done.md`：新增「## 62. MCP 读取本人的问答记录与深度报告（2026-10-21）」一节（照 §60 写法：一句结果 + 规则要点 +
  链到产品文档、SOP 与本计划），并更新文件头「更新日期」句。（A 线若也加小节，后合入者按标题去重编号。）

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_architecture_documentation.py tests/test_cross_doc_merge.py \
  tests/test_agent_mcp_onboarding.py tests/test_release_notes.py tests/test_mcp_bundle_architecture.py -q -n 2
python3 $W/scripts/check_ui_vocabulary.py
bash /Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh hist-smoke -- \
  env PYTHONPATH=$W/backend python3 $W/scripts/smoke_memory_mcp.py
```
（smoke 的调用方式照 `scripts/check_contracts.sh:100-101`。）

**验收**：`test_current_mcp_docs_pin_the_complete_public_mcp_tool_surface` 与 SOP 双语表测试转绿；smoke 通过。

---

### T7 全量门禁与交付（主 agent）

见第 4 节；之后按 `CLAUDE.md` 的 codex 评审闭环开 PR（PR 描述写 D2、D5、D9 的取舍与「已签发 read 凭证立即获得能力」），
贴每轮评审原文、`verify` 后合入。开 PR 前 `git fetch origin` 并 rebase 到最新 `origin/master`，
`git log --merges origin/master..HEAD` 必须为空；rebase 后重跑门禁。

---

## 4. 全量门禁

```bash
# G1 标准门（经全局锁）
bash /Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh hist-g1 -- \
  bash $W/scripts/check.sh
# G3 PostgreSQL（经全局锁；本机一次性库，见第 0 节）
bash /Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh hist-g3 -- \
  bash -c "cd $W/backend && TEST_POSTGRES_URL=postgresql://huzhifeng@localhost/silicon_notebook_hist_test python3 -m pytest tests/postgres -m 'postgres_integration or postgres_lane_contract' -q -n 1"
# 架构守卫（秒级）
python3 $W/scripts/check_architecture_boundaries.py
```
`check.sh` 退出码直接读；本机恒红的 packaging/dotenv 用例按既有基线证据在 PR 说明，不当通过。
本地另起一次真服务，用官方 MCP 客户端以 `read` 凭证走一遍：网页里问一个问题（单库）与一个全局问题、生成一份报告 →
`list_conversations` / `get_conversation` / `list_reports` / `get_report` / `read_reference`，结果写进 PR。

---

## 5. 风险清单

- **R1 全局列表成本**：`agent_conversations` 每次读主人全部未过期全局任务的窄化投影（不含正文与轨迹）+ 一次批量读权。
  量级 = 该用户的全局任务数；正文不过线。若实测一个重度用户（数千个全局任务）列表超过 1 s，在 PR 里给数据，
  再议是否加持久化归属表——本计划不预先做。
- **R2 规则漂移**：D5 的「用到的库」只经 `_snapshot_notebook_ids` 一处；store 的窄化投影由 parity 用例钉住两端与原 payload 等价。
- **R3 取轮竞态**：判定与取轮之间新任务结束只会追加在末尾；删除/过期会让后面的 `turn_offset` 前移（文档写明重读 `turn_count`）。
  取出的那一轮再 `_check` 一次，越界即 `not_found`。
- **R4 记忆**：D2 的同档不变量由测试钉住；他人记忆按 D9 只给摘录不给句柄与身份；`read_reference` 仍是打开原文的唯一闸。
- **R5 HTTP 响应多字段**：`submitted_via` 只增；前端不读它，旧标签页无影响；`api_contract.json` 同 PR 刷新。
- **R6 A 线冲突**：A 线后合入，会碰 `models/ask.py`（`AskRequest`/`AskResponse`）与本计划的 `conversations.py`；本计划不碰
  `AskRequest`/`AskResponse`，冲突面只在 `ConversationSummary`/`ConversationTurn` 附近与 `fangan_done.md`，按三方合并处理。
- **R7 中间态红测**：T3/T4 后文档工具表守卫红到 T6，属预期；T7 前必须全绿。
- **R8 迁移号**：本计划无迁移；新 PG 测试文件里 `migrate() == N` 写实施时的最新号，rebase 后若主线进了迁移要同步改。
