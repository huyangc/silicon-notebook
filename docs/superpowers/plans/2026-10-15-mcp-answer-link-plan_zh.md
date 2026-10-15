# MCP 回答「摘要 + 限时链接」实现计划

日期：2026-10-15
规格：[`docs/superpowers/specs/2026-10-15-mcp-answer-link-design_zh.md`](../specs/2026-10-15-mcp-answer-link-design_zh.md)

用户已拍板：§8.1 选 A（匿名能力链接）；§8.2 默认 TTL 168h、上限 720h、宽限期 168h；§8.3 不做
MCP 会话保留期；§8.4 摘要默认 300 码点，调用方不可传长度。**本计划范围一轮做完，不留延后项。**

下文所有路径相对仓库 worktree 根
`/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/mcp-global-ask-issue-019586`
（简称 `$W`）。行号以本计划写作时的 `35188bb75` 为准，实现时以符号名为准。

---

## 0. 每个实现子代理都要遵守的公共约束

- 只在 `$W` 里工作，所有命令用绝对路径；编辑用 Edit/Write，不用 shell 重定向。
- `frontend/node_modules` 是指向主 checkout 的软链（已核对），**不得 `npm install`**。
- 重负载命令（`scripts/check.sh`、整条 PG 泳道、`-n` > 2 的 pytest、`next build`）一律经
  `bash ~/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh <标签> -- <命令>`；
  单文件 pytest / vitest / `tsc --noEmit` 可直接跑。
- 门禁退出码直接读，不用 `cmd | grep; echo $?`。
- PG 集成测试：本机一次性库（库名须匹配 `silicon_notebook_*_test`，装 `pg_trgm`、`btree_gin`），
  URL 带用户名，`-n 1`：
  ```bash
  psql -d postgres -c "CREATE DATABASE silicon_notebook_alnk_test;"
  psql -d silicon_notebook_alnk_test -c "CREATE EXTENSION pg_trgm;" -c "CREATE EXTENSION btree_gin;"
  cd $W/backend && TEST_POSTGRES_URL="postgresql://huzhifeng@localhost/silicon_notebook_alnk_test" \
    python3 -m pytest <文件> -m postgres_integration -q -n 1
  ```
- 匿名路径纪律（`ask_routes.py:1377-1422` 的 docstring）：匿名路由上**不得**调用任何读
  request-user ContextVar 的方法；一切身份显式传参。
- `scripts/check_architecture_boundaries.py` 的守卫：facade **只许缩**（不加新公开方法；改已有方法签名
  后跑 `python3 $W/scripts/generate_repository_contract_fixtures.py --rebaseline-surface`）；
  `ports_protocol_method_count`（`scripts/architecture_boundary_baseline.json`）加了 Protocol 方法要同 diff
  改成精确新值；`function_length_ceiling` 是固定热函数清单，本计划涉及的函数都不在清单里
  （仅 `MemoryService.transfer` 在，不碰它）。
- 用户可见文案先读 `docs/ui-vocabulary.md`；新的 `user_error(...)` 站点要在
  `backend/tests/test_user_error.py` 登记（动态站点按既有写法）。
- 不写延后项、不加 skip/xfail。

---

## 1. 冲突与落地决定

读代码后与设计稿有出入的地方，以及选择的落地方式（均保持设计意图：匿名、限时、单条回答粒度、
token 吊销即失效、读权复核与会话分享同一实现、记忆披露由 token 事先授权代替握手）。

**D1 权限位存成新列，不做第六档 tier。** Agent token 的五档存在 `agent_access_tokens.scopes_json`
（`postgres/migrations/0069_agent_token_tiers.sql`），而「每一档都至少打开一个工具」是被钉死的不变量
（`backend/tests/test_mcp_bundle_architecture.py:116`：「a tier a token can hold but no tool reads would be a
permission without a door」），v89 冻结规则也与 `AGENT_TIERS` 逐项相等
（`backend/tests/test_agent_token_tiers_migration.py:115`）。「允许签发回答链接」不打开任何工具，只是
`ask` 的附加授权，做成 tier 会同时破坏这两条。落地：新列
`agent_access_tokens.allow_answer_links`（INTEGER NOT NULL DEFAULT 0，两端同形，沿用
`ask_jobs.memory_access` 的整数旗标先例），作为访问配置的第五个字段走签发、`PUT .../access` 整体替换与
`expected` 前置条件。这与设计 §7「Agent token 权限位列」一致；界面上与五档并列显示（§6）。
约束：开此位必须同时有 `ask` 档，否则 422（中文文案见 T2）。

**D2 唯一约束改为 `(job_kind, job_id, agent_token_id)`，且是普通唯一索引。** 设计写「同一作业至多一条
有效链接」的部分唯一索引。两处现实冲突：(a) `expires_at > now()` 不能做部分索引谓词（非 immutable）；
(b) 同一主人的**另一个** token 也能用 `get_ask` 读这个作业（`ask.py:1058-1092` 只核 `created_by` 与
`submitted_via`，不核 token），若按作业唯一，B token 补签会替换掉 A token 已发进 IM 的链接，或反过来让
B 发出的链接随 A 吊销而死。落地：唯一键带上 `agent_token_id`；签发在同一写事务里「先删本键已过期行，
再 `INSERT ... ON CONFLICT DO NOTHING`，再读回」——`client_request_id` 重试与 `get_ask` 补签都复用同一条
未过期链接，设计要的「不重复签发」不变；每个 token 对每个作业至多一条有效链接。

**D3 不在各删除路径写级联，正确性靠读时复核，空间回收靠清扫。** 作业/会话的删除路径至少六处
（`postgres/ask_state_store.py:1328-1361` 会话删除与空会话清理、`notebook_store.py` /
`notebook_delete_job_store.py` 删笔记本、`GlobalAskService.delete_conversation` / `discard_cancelled`、
同步导入全量重建），Agent token 还会随默认笔记本被删而物理删除（`services/notebook_delete_tables.py:244`）。
逐处加级联既脆又与会话分享「活谓词而不是每个撤销点级联」的既定取舍相反（`ask_routes.py:1340-1347`
docstring）。落地：`answer_links` 不建外键（同 `ask_intent_handles`）；打开时作业不存在/token 不存在
即 404（读时就是死链）；清扫顺带删除「作业已不存在」「签发 token 已不存在或已撤销（撤销不可逆）」的行。

**D4 清扫挂在签发写路径上（每进程每小时至多一次），不新开后台线程。** 仓库没有通用维护调度；现有周期
任务（如 `startup_warmup._start_notebook_delete_sweeper`、`notebook_delete.start_notebook_delete_sweeper`）
都是各自一条带 lease/stop 接线的线程。同类小表 `ask_intent_handles` 的先例是在写路径上顺带清
（`postgres/ask_intent_handle_store.py:43`）。落地：`AnswerLinkStore.issue(...)` 在同一写事务开头执行
清扫，节流状态放在 store **实例**上（不放模块级变量，免得触发 `test_world_templates.py` 的模块级缓存
分类守卫），间隔 3600 秒。正确性不依赖清扫（到期只在读时判断）；不签发时表也不增长。

**D5 错误码用现有的 `forbidden`，不新增 `permission_denied`。** `AgentToolError` 的稳定词表
（`api/mcp_tools/_shared.py:73-89`）已有 `forbidden`（`sources.py:551`、`maintenance.py:137` 在用）。

**D6 读时复核 = get_ask 的投递规则 + 会话分享的公开谓词，两者都是复用而非重写。**
- 单库作业：先按签发 token 的**此刻**状态跑 get_ask 的同一组检查（主人一致、`submitted_via="mcp"`、
  `require_agent_access(principal, "ask:execute", notebook_id)`——即 ask 档 + 白名单 + 读权、
  运行时开过个人记忆通道的作业要求 token 此刻仍有 `memory:read`），再跑会话分享单库分支的谓词
  （`user_can_read_notebook` + `mounts_still_effective(conversation_library_ids(turns))`），后者从
  `_public_conversation_or_404`（`ask_routes.py:1358-1375`）**抽成一个函数**两路共用。
- 全局作业：`GlobalAskService.public_conversation`（`services/global_ask.py:2318-2370`）里的
  「`_snapshot_notebook_ids` → `_check(scope, creator)`」抽成私有助手，`cshr`/`gshr` 不变地调用它；
  链接分支调用同一助手并多传 `allowed_notebook_ids=token 白名单`（`_check` 已支持，`global_ask.py:623-638`）。
  全局作业从不打开个人记忆通道（`backend/tests/test_global_ask_mcp.py:604` 钉住），无需记忆规则。

**D7 投影与快照形状。** 单库：一轮 = `ask_answer_detail(answer_id)` 的返回
（`{answer_id, question, payload, created_at}`，正是 `public_conversation_by_token` 的 turn 形状）。
全局：一轮 = `_public_turn_row(job)`（`global_ask.py:518-537`）。两路都交给
`conversation_public_view.public_conversation_payload`（`:629-662`）原样投影，`title=""`、
`shared_through_at=""`；`PublicConversation` 模型加一个可选 `expires_at`（仅链接分支填）。
图片别名用链接 token 做 HMAC 键（`conversation_asset_alias`），所以过期/失效后别名随页面一起 404。

**D8 链接表在 sync / shadow 里是本地表。** `agent_access_tokens` 在增量同步里是 LOCAL
（`migration/sync/manifest.py:575-578`），绑在它上面的链接不能跨环境；登记为 `SyncClass.LOCAL` 与
shadow `TableClass.LOCAL_EPHEMERAL`（同 `ask_intent_handles`，`shadow/manifest.py:858`），`merge_dbs.py`
只保 primary。代价：shadow 切换前签发的链接在新库上 404，机器人可用 `get_ask(delivery="link")` 重签。

**D9 `PUBLIC_WEB_BASE_URL` 允许 http。** 生产前端是 `http://<IP>:3000`（非 secure context），
不能套用 `AUTH_*_BASE_URL` 的 HTTPS 要求（`core/config.py:1953-1995`）。校验：scheme ∈ {http, https}、
有 host、无用户名密码/query/fragment、无控制字符；允许路径前缀（子路径部署），去尾斜杠。
未配置时 `delivery="link"` 在任何仓库工作前返回 `unavailable`。

**D10 `link_ttl_hours` 只作用于新签发。** 复用已存在的未过期链接时返回它原有的 `expires_at`
（如实报告），不改写；`link_ttl_hours` 越界（< 1 或 > `MCP_ANSWER_LINK_TTL_HOURS`）→
`invalid_argument`，不夹紧；`delivery="inline"` 时传 `link_ttl_hours` → `invalid_argument`（不静默忽略）。

**D11 410 只给页面端点。** `GET /api/public/conversations/alnk-…` 过期且行仍在 → 410
「链接已过期，请重新提问」；图片端点任何失败（含过期）一律 404。判定顺序：行不存在 → 404；
已过期 → 410；其余任一复核失败 → 404。

---

## 2. 接口与数据形状（各任务共同遵守的契约）

### 2.1 表 `answer_links`（SQLite v94 / PG `0074_answer_links.sql`；落地前复核号，见 T1a）

PG：
```sql
CREATE TABLE answer_links (
  token text COLLATE "C" NOT NULL,
  job_kind text COLLATE "C" NOT NULL,          -- 'notebook' | 'global'
  job_id text COLLATE "C" NOT NULL,            -- askjob-… | gask-…
  owner_id text COLLATE "C" NOT NULL,
  agent_token_id text COLLATE "C" NOT NULL,
  created_at timestamp with time zone NOT NULL,
  expires_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_answer_links PRIMARY KEY (token),
  CONSTRAINT ck_answer_links_job_kind CHECK (job_kind IN ('notebook', 'global'))
);
CREATE UNIQUE INDEX uq_answer_links_job_token ON answer_links (job_kind, job_id, agent_token_id);
CREATE INDEX idx_answer_links_expires ON answer_links (expires_at);
ALTER TABLE agent_access_tokens ADD COLUMN IF NOT EXISTS allow_answer_links integer NOT NULL DEFAULT 0;
```
SQLite 同列同索引（TEXT/INTEGER，时间存规范化 UTC ISO `…Z` 文本，与 `ask_intent_handles` 一致）。

### 2.2 store 端口（`repositories/ports.py`，紧挨 `AskIntentHandleStorePort`，`:527-544`）

```python
class AnswerLinkStorePort(Protocol):
    def issue_answer_link(
        self, *, job_kind: str, job_id: str, owner_id: str, agent_token_id: str,
        ttl_seconds: int, sweep_grace_seconds: int,
    ) -> dict: ...            # {"token", "expires_at"(ISO), "reused": bool}
    def answer_link(self, token: str) -> "dict | None": ...
        # {"token","job_kind","job_id","owner_id","agent_token_id","created_at","expires_at","expired": bool}
    def sweep_answer_links(self, *, grace_seconds: int) -> int: ...
```
`issue_answer_link` 在一个写事务里：节流到期则先 `sweep`；删本键（三元组）`expires_at <= now` 的行；
`INSERT … ON CONFLICT (job_kind, job_id, agent_token_id) DO NOTHING`（token 由
`new_capability_token(ANSWER_LINK_PREFIX)` 生成）；读回本键的行（自己插的或并发赢家的），`reused`
= 读回 token ≠ 本次生成的 token。`answer_link` 的 `expired` 由 store 的 `now` 计算
（`expires_at <= now`），不读时钟以外的东西。`sweep` 删除：`expires_at < now - grace`；
签发 token 行不存在或 `revoked_at IS NOT NULL`；`job_kind='notebook'` 且 `ask_jobs` 无此 id；
`job_kind='global'` 且 `global_ask_jobs` 无此 id。

### 2.3 Agent token 访问配置

- `AgentTokenCreate.allow_answer_links: bool = False`
- `AgentTokenAccess.allow_answer_links: bool = False`（`expected` 前置条件；`matches` 比较它）
- `AgentTokenAccessUpdate.allow_answer_links: bool`（**必填**，整体替换语义不留默认）
- `AgentTokenSummary` / `AgentTokenIssued` / `AgentPrincipal` 各加 `allow_answer_links: bool = False`
- 422 文案常量（`domain/agent_tools.py`，与 `OWNER_ONLY_TIERS_MESSAGE` 并列）：
  `ANSWER_LINKS_REQUIRE_ASK_MESSAGE = "允许签发回答链接需要同时勾选「问答」权限"`，异常
  `AgentAnswerLinksNeedAskError`（`services/memory_service.py` 里与 `AgentOwnerOnlyTierError` 同处定义）。

### 2.4 配置（`core/config.py`，放在 `auth_*` 字段附近）

| 环境变量 | 字段 | 默认 | 约束 |
| --- | --- | --- | --- |
| `PUBLIC_WEB_BASE_URL` | `public_web_base_url: str` | `""` | D9 校验，去尾 `/` |
| `MCP_ANSWER_LINK_TTL_HOURS` | `mcp_answer_link_ttl_hours: int` | 168 | `ge=1, le=720` |
| `MCP_ANSWER_LINK_GRACE_HOURS` | `mcp_answer_link_grace_hours: int` | 168 | `ge=0, le=720` |
| `MCP_ANSWER_PREVIEW_MAX_CHARS` | `mcp_answer_preview_max_chars: int` | 300 | `ge=40, le=2000` |

### 2.5 MCP

`ask(..., delivery: str = "inline", link_ttl_hours: int | None = None)`；
`get_ask(..., delivery: str = "inline", link_ttl_hours: int | None = None)`。

`delivery="link"` 且 `status == "answered"` 时的响应（经 `_budget_response`，≤ 20 个顶层键，
`TOTAL_TEXT_LIMIT` 12 000 字节内）：
```json
{"job_id","conversation_id","status":"answered","mode",
 "preview","preview_truncated",
 "link":{"url":"<PUBLIC_WEB_BASE_URL>/c/alnk-…","expires_at":"2026-10-22T08:00:00+00:00"},
 "grounded","total_citations","completeness_notice","scope",
 "error":"","content_is_untrusted_evidence":true}
```
其余状态（`needs_clarification` / `failed` / `cancelled` / get_ask 读到的 `running`）返回与 inline 完全
相同的现行形状，不签链接。

### 2.6 公开端点

- `GET /api/public/conversations/{token}`：`alnk-` 前缀走第三分支；`PublicConversation` 多一个
  `expires_at: Optional[str] = None`（仅链接分支非空）；过期 410，`detail` 为中文
  「链接已过期，请重新提问」（经 `user_error(410, …)`）。
- `GET /api/public/conversations/{token}/assets/{alias}`：同一复核，任何失败 404。

### 2.7 前端

- `fetchPublicConversation` 返回 `{kind:"ready", conversation} | {kind:"missing"} | {kind:"expired"}`。
- `PublicConversationT.expires_at?: string`；页面头部在有值时加「此链接将于 <时间> 失效」。
- `/agents`：`AgentTokenDraft.allow_answer_links: boolean`；签发与编辑共用的 `AgentAccessFields`
  加一个复选框。

---

## 3. 任务列表

依赖图：`T1a → T1b → T2 → T5 → T6 → T7 → T9`；`T3`、`T4` 与 `T1a` 并行（文件集不相交），在 `T5` 前完成；
`T8` 在 `T2` 合入后可与 `T5–T7` 并行（只碰 `frontend/`）；`T9` 最后；`T10` 由主 agent 收口。
（`T1b` 与 `T2` 都改 `repositories/ports.py`，必须串行。）

---

### T1a 迁移与全部登记清单（无业务代码）

**目标**：新增表 `answer_links` 与列 `agent_access_tokens.allow_answer_links`，两端迁移、全部 schema
登记与版本钉一次到位；仓库其余代码行为不变。

**第零步**：`git -C $W fetch origin master`，确认
`git -C $W ls-tree --name-only origin/master backend/app/repositories/postgres/migrations/ | tail -1`
仍是 `0073_…`、`git -C $W show origin/master:backend/app/repositories/sqlite/migrations.py | grep -n '^SCHEMA_VERSION'`
仍是 93。若已前进，顺延号并在报告里写明实际号；下文 94/0074 同步替换。

**改/建文件**（`$W/` 下）：
- 新建 `backend/app/repositories/postgres/migrations/0074_answer_links.sql`：§2.1 的 DDL，文件头注释照
  `0071_ask_intent_handles.sql` 写法（配对 SQLite v94；适配器本地记账、sync LOCAL / shadow
  LOCAL_EPHEMERAL；不建外键的理由 = D3）。
- `backend/app/repositories/sqlite/migrations.py`：`SCHEMA_VERSION = 94`（`:299`）；`:270-298` 注释块追加
  v94 段；在 `_migration_93`（`:5773`）后加 `_migration_94`：`CREATE TABLE IF NOT EXISTS answer_links …`、
  两个索引、`self.add_column_if_missing(db, "agent_access_tokens", "allow_answer_links", "INTEGER NOT NULL DEFAULT 0")`。
- `backend/app/repositories/postgres/schema_manifest.py`：表清单（`:37-41` 附近）加 `answer_links`；
  `:433-457` 注释追加 v94/0074 段；`sqlite_version=94, postgres_version=74`。
- `backend/app/migration/shadow/manifest.py`：`RUNNING_SCHEMA_PAIR = SchemaPair(sqlite_version=94, postgres_version=74, epoch=1)`；
  `_TABLES` 末尾（`:858` 之后）`TableSpec("answer_links", TableClass.LOCAL_EPHEMERAL, (), ReplicationKeyKind.DECLARED_PK, 110)` + 注释。
  若 `agent_access_tokens` 的 `_table(...)` 声明了列/类型标签，按 `c76309cac` 对 `agent_profiles.deleted_at`
  的处理方式补齐新列（以守卫测试为准）。
- `backend/app/migration/sync/manifest.py`：`_LOCAL` 末尾（`:599` 后）
  `TableSyncSpec("answer_links", SyncClass.LOCAL, notes="MCP 限时回答链接，绑定本环境 Agent token")`。
- `scripts/merge_dbs.py`：`:265-268` 的只保 primary 清单加 `"answer_links"` 与注释。
- `backend/app/repositories/user_references.py`：`USER_REFERENCE_COLUMNS` 加
  `UserReference("answer_links", "owner_id", "scaffolding")`（紧挨 `ask_intent_handles`，`:62`）。
- `scripts/verify_repository_snapshot.py`：`MIGRATION_MANIFEST` 加 (93, 94) 块，表/索引/列 SQL 原文照
  `:5392-5420` 的 v91 写法。
- `backend/tests/sqlite_migration_testkit.py`：加 `rollback_v94`（DROP 两个索引、DROP TABLE answer_links、
  `ALTER TABLE agent_access_tokens DROP COLUMN allow_answer_links`），并按 newest-first 接进既有回滚链
  （`rollback_v93` 的 docstring 写法）。
- 版本钉扫荡：`grep -rn "== 73\b\|migrate() == 73\|postgres_version=73\|sqlite_version=93\|SCHEMA_VERSION == 93\|, 73,\? *\]" $W/backend/tests $W/scripts`
  逐个改为新号（参照 `git -C $W show c76309cac --stat` 列出的约 45 个测试文件；
  `backend/tests/postgres/test_migrations.py` 的有序列表追加 74）。
- `backend/tests/postgres/test_postgres_schema.py`：`(74, "answer_links")`（`:261` 旁）与
  `index_declarations(74) == [(False, "idx_answer_links_expires"), (True, "uq_answer_links_job_token")]`
  （按该测试实际顺序/形状写，`:756` 旁）。
- `backend/tests/test_repository_snapshot_verifier.py`：照 `:239-250` 加「已部署 v93 库升级后验证
  answer_links」用例与 `_rollback_v94`，并插进全部回滚链。
- `backend/tests/postgres/test_memory_isolation_migration_pg.py`：`:43` 旁同样撤销 74 建的表/列以便重放。
- `backend/tests/auth_store_contract.py`：`:567` 旁照 `ask_intent_handles` 加一条 `answer_links` 插入，
  证明账号合并把 scaffolding 行一并删除。
- 重生成：`python3 $W/scripts/generate_repository_contract_fixtures.py`（更新
  `backend/tests/fixtures/repository_v9/expected_snapshot.json`、`manifest.json` 的 sha/size）。

**测试**：`backend/tests/test_sqlite_migrations.py` 加 `test_v94_adds_answer_links_and_the_token_flag`
（新库与 v93 升级库都有表、两个索引、列默认 0、旧 token 行读出 0）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_sqlite_migrations.py tests/test_repository_snapshot_verifier.py \
  tests/test_user_references.py tests/test_persistence_bundle_contract.py -q -n 2
cd $W/backend && python3 -m pytest tests/test_test_architecture_policy.py -q -n 2
# 需要本机 PG：
cd $W/backend && TEST_POSTGRES_URL=... python3 -m pytest tests/postgres/test_migrations.py \
  tests/postgres/test_postgres_schema.py tests/postgres/test_memory_isolation_migration_pg.py -m postgres_integration -q -n 1
```

---

### T1b `AnswerLinkStore` 两端实现与接线（依赖 T1a）

**目标**：§2.2 端口的 SQLite 与 PG 实现、bundle/runtime/deps 接线，store 级契约测试两端同一份用例。

**改/建文件**：
- 新建 `backend/app/repositories/sqlite/answer_link_store.py`、`backend/app/repositories/postgres/answer_link_store.py`
  （照 `*/ask_intent_handle_store.py`：`__init__(database, now)`；节流字段 `self._last_sweep_monotonic`
  + `threading.Lock`；`_SWEEP_INTERVAL_SECONDS = 3600`）。SQLite 写事务用既有 `database.write()`（BEGIN
  IMMEDIATE 围栏），PG 用 `ON CONFLICT … DO NOTHING` 后同事务读回。
- `backend/app/core/capability_tokens.py`：加 `ANSWER_LINK_PREFIX = "alnk"` 与
  `is_answer_link_token(token) -> bool`（带分隔符判定，同 `is_global_conversation_share_token`，
  `:38-47`）；模块注释从「两个命名空间」改为三个。
- `backend/app/repositories/ports.py`：§2.2 `AnswerLinkStorePort`；
  `scripts/architecture_boundary_baseline.json :: ports_protocol_method_count` 改成精确新值。
- `backend/app/repositories/bundle.py`（`:74` 旁）加 `answer_links: AnswerLinkStorePort`；
  `backend/app/repositories/sqlite/bundle.py`（`:42/:88/:166/:197`）与 `postgres/bundle.py`（`:45/:190/:298/:329`）照 `ask_intent_handles` 接线。
- `backend/app/services/repository_runtime.py`（`:305-306`、`:409`、`:1100`）加座位 `answer_links`。
- `backend/app/api/deps.py`（`:80-82` 旁）加 `answer_link_repository()`，返回 `repository()._runtime.answer_links`。
- `backend/app/repositories/ownership_manifest.py`（`:638` 旁）登记新的 `ConsumerSite`
  （`deps.answer_link_repository` → `_runtime`）。
- `backend/tests/test_repository_runtime_composition.py:151` 的 `RUNTIME_ATTRIBUTES`、
  `backend/tests/test_persistence_bundle_contract.py:53` 加 `"answer_links": "AnswerLinkStorePort"`。
- 若 `backend/tests/fixtures/repository_contract/caller_boundaries.json` 因新 deps 访问变红：
  `python3 $W/scripts/generate_repository_contract_fixtures.py --rebaseline-callers`，报告里说明。

**测试**（新建 `backend/tests/answer_link_store_cases.py`，两端共用；照 `ask_intent_handle_store_cases.py`）：
- `issue_then_read_back`（字段齐全、`expired=False`、token 前缀 `alnk-`、长度 = 5 + 43）；
- `reissue_same_key_reuses_unexpired`（`reused=True`、token/expires_at 不变，即使 ttl 不同）；
- `reissue_after_expiry_replaces`（推进假钟过期后再签：新 token，旧 token 读回 None）；
- `different_agent_token_gets_its_own_link`（D2）；
- `expired_flag_at_exact_boundary`（`now == expires_at` → `expired=True`）；
- `sweep_drops_past_grace_keeps_within_grace`；
- `sweep_drops_revoked_or_missing_token_rows`、`sweep_drops_rows_whose_job_is_gone`（notebook 与 global 各一）；
- `issue_sweeps_at_most_once_per_interval`（同实例两次签发只清一次；推进单调钟后再清）。
SQLite 端：`backend/tests/test_answer_link_store.py`；PG 端：`backend/tests/postgres/test_answer_link_store_pg.py`
（`PostgresMigrator(database).migrate() == 74`）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_answer_link_store.py tests/test_repository_runtime_composition.py \
  tests/test_persistence_bundle_contract.py -q -n 2
python3 $W/scripts/check_architecture_boundaries.py
# 需要本机 PG：
cd $W/backend && TEST_POSTGRES_URL=... python3 -m pytest tests/postgres/test_answer_link_store_pg.py -m postgres_integration -q -n 1
```

---

### T2 Agent token「允许签发回答链接」权限位（后端，依赖 T1b）

**目标**：§2.3。签发、列表、`PUT .../access`（含 `expected` CAS）、鉴权行与 `AgentPrincipal` 全链路带上
`allow_answer_links`；无 `ask` 档时开启 → 422 中文文案。

**改文件**：
- `backend/app/models/identity.py`：`AgentTokenCreate`（`:138-145`）、`AgentTokenAccess`（`:161-176`，含
  `matches`）、`AgentTokenAccessUpdate`（`:179-203`，必填）、`AgentTokenSummary`（`:206-221`）、
  `AgentTokenIssued`（`:228-236`）、`AgentPrincipal`（`:238-245`）。
- `backend/app/domain/agent_tools.py`：`ANSWER_LINKS_REQUIRE_ASK_MESSAGE`。
- `backend/app/services/memory_service.py`：`_validate_agent_access`（`:548-588`）多收 `allow_answer_links`，
  开启而 `"ask" not in clean_scopes` → `AgentAnswerLinksNeedAskError`；`issue_agent_token`（`:590-627`）、
  `update_agent_token_access`（`:652-672`）透传；`_principal_of`（`:741-753`）读
  `bool(row.get("allow_answer_links"))`。
- `backend/app/repositories/sqlite/memory_store.py`（`_token :102`、`create_agent_token :200`、
  `update_agent_token_access :306`、`agent_token_auth_row :364`）与 `postgres/memory_store.py`
  （`_token :135-148`、`create :242-291`、`update :353-410`、`auth_row :412-445`——它是显式列清单，要加列）。
  `update` 的 `UPDATE … SET` 加列；`expected.matches` 用到的 `_token` 要带上新字段。
- `backend/app/repositories/ports.py`：`MemoryRepository.issue_agent_token` / `update_agent_token_access`
  （`:699-715`）与 store 端口 `create_agent_token` / `update_agent_token_access`（`:4074-4095`）签名加
  keyword `allow_answer_links: bool = False`（数量不变则 `ports_protocol_method_count` 不动）。
- `backend/app/services/repository_facade.py`（`:1280-1330`）两个方法签名同步（不新增方法），跑
  `--rebaseline-surface`。
- `backend/app/api/memory_routes.py`：签发路由（`:169` 起）与 `PUT` 路由（`:239` 起）透传，并把
  `AgentAnswerLinksNeedAskError` 映射为 `user_error(422, ANSWER_LINKS_REQUIRE_ASK_MESSAGE)`；
  在 `backend/tests/test_user_error.py` 登记。
- 重生成 `backend/tests/fixtures/repository_contract/api_contract.json`（`generate_repository_contract_fixtures.py`）。

**测试**（`backend/tests/test_agent_tokens.py` 追加）：
- `test_issue_defaults_answer_links_off_and_lists_it`；
- `test_answer_links_need_the_ask_tier_on_issue_and_edit`（422 + 中文文案）；
- `test_put_access_requires_allow_answer_links_field`（缺字段 422）；
- `test_put_access_toggles_answer_links_and_principal_reflects_it`（`refresh_agent_principal` 读到新值）；
- `test_expected_mismatch_on_answer_links_is_409`；
- `test_unrelated_edit_keeps_answer_links`（只改白名单时原样回传 → 不变）。
PG 侧：`backend/tests/postgres/test_agent_profile_store_conformance.py` 或其 token 合同用例里加
同样的往返断言（按该文件现有组织方式）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_agent_tokens.py tests/test_user_error.py \
  tests/test_repository_api_contract.py tests/test_mcp_bundle_architecture.py -q -n 2
python3 $W/scripts/check_architecture_boundaries.py
# 需要本机 PG：相关 conformance 文件 -m postgres_integration -n 1
```

---

### T3 配置项与纯文本摘要模块（与 T1a 并行）

**目标**：§2.4 四个配置；确定性纯函数 `answer_preview`。

**改/建文件**：
- `backend/app/core/config.py`：四个字段（`:128` 旁），`public_web_base_url` 的 `field_validator`
  按 D9（不复用 `_validate_auth_base_url`，它要求裸 origin、且模型校验器要求 HTTPS）。
- 新建 `backend/app/services/answer_preview.py`：
  ```python
  def answer_preview(markdown: str, max_chars: int) -> tuple[str, bool]:
      """(plain_text, truncated)"""
  ```
  规则（规格 §4.2）：用 `app.services.citation_markers.MARKER_RE` 去 `[kN]`/`【kN】` 引用组；去图片
  `![..](..)`、HTML 标签；标题去 `#`；粗/斜/删除线/行内代码去标记留文字；无序/有序列表项改 `• `；
  链接留文字；围栏代码块保留内容（去围栏行）；GFM 表格整块替换为一行「（表格，见链接）」；`$$…$$` 与
  `\[…\]` 公式块整块替换为「（公式，见链接）」，行内 `$…$` 保留内容；空行折叠为段落分隔 `\n`；
  按码点截取 ≤ `max_chars`：超长时在上限内最后一个段落边界或句末标点（`。！？；.!?`）处截断，找不到就
  硬截，末尾加 `…`（`…` 计入上限），`truncated=True`；空答案返回 `("", False)`。
- 不改前端渲染器，不调模型。

**测试**：
- `backend/tests/test_answer_preview.py`：每种结构一条（`test_citation_markers_removed`、
  `test_heading_emphasis_code_unwrapped`、`test_lists_become_bullets`、`test_links_keep_text_images_dropped`、
  `test_table_block_replaced_once`、`test_display_math_replaced_inline_math_kept`、`test_fenced_code_kept`、
  `test_html_stripped`、`test_truncates_at_sentence_end_and_marks`、`test_hard_cut_when_no_boundary`、
  `test_counts_code_points_not_bytes`（中文 + emoji）、`test_short_answer_not_truncated`、`test_empty`）。
- `backend/tests/test_public_web_base_url_config.py`：http/https/子路径/去尾斜杠合法；query、fragment、
  用户名、非 http(s)、无 host 拒绝；三个整数配置的边界（0/1/720/721 等）。

**验证**：`cd $W/backend && python3 -m pytest tests/test_answer_preview.py tests/test_public_web_base_url_config.py tests/test_env_aliases.py -q`

---

### T4 全局作业的公开快照助手（与 T1a 并行）

**目标**：按作业 id + 主人取一条已完成 MCP 全局作业的公开快照行，复核谓词与会话分享**同一实现**。

**改文件**：
- `backend/app/repositories/global_ask_store.py`（单文件两端共用，`self._sql`）：加
  `public_job(job_id, user_id) -> dict | None`，只返回 `status='done'` 且 `submitted_via='mcp'` 的行：
  `{"job_id", "payload": self._payload(row["payload_json"]), "created_at"}`（与 `public_conversation_by_token`
  的 `jobs[]` 元素同形，`:735-748`）。
- `backend/app/repositories/global_ask_ports.py`（`:126` 旁）加同名协议方法。
- `backend/app/services/global_ask.py`：
  - 抽私有助手 `_public_snapshot(self, jobs, creator, allowed_notebook_ids=None) -> tuple[list, frozenset] | None`：
    `scope = _snapshot_notebook_ids([job.get("payload") for job in jobs])`；`self._check(scope, creator, allowed_notebook_ids)`，
    `GlobalAskError` → `None`；返回 `([_public_turn_row(job) for job in jobs], frozenset(scope))`。
  - `public_conversation`（`:2318-2370`）改为调用它，行为逐字不变。
  - 新增 `public_answer_job(self, job_id, *, owner_id, allowed_notebook_ids) -> dict | None`：
    `store.public_job(job_id, owner_id)` → 助手（传白名单）→ 返回
    `{"title": "", "created_at": job["created_at"], "shared_through_at": "", "turns": turns,
      "notebook_ids": scope, "created_by": owner_id}`；任何失败 `None`。docstring 写明「无 request user」纪律。

**测试**（`backend/tests/test_global_ask_share_api.py` 或新建 `backend/tests/test_global_answer_snapshot.py`，
按既有夹具）：
- `test_public_answer_job_returns_one_turn_for_a_done_mcp_job`；
- `test_browser_job_running_job_and_foreign_owner_are_none`；
- `test_lost_read_on_a_cited_library_is_none`（引用库失权 → None，与 gshr 同谓词）；
- `test_scope_outside_the_token_allowlist_is_none`（resolved ⊆ 白名单但引用库不在 → None）；
- `test_public_conversation_unchanged`（既有 gshr 用例全绿即证明）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_global_answer_snapshot.py tests/test_global_ask_share_api.py \
  tests/test_global_ask_share_store.py -q -n 2
# 需要本机 PG：tests/postgres/test_global_ask_share_store.py tests/postgres/test_global_ask_store.py -m postgres_integration -n 1
```

---

### T5 回答链接服务：签发与公开解析（依赖 T1b、T2、T3、T4）

**目标**：一个服务模块承载「签发/复用 + 拼 URL」与「匿名打开时的解析与复核（D6、D11）」，MCP 与
公开路由都只调它。

**新建** `backend/app/services/answer_links.py`：
```python
@dataclass(frozen=True)
class IssuedAnswerLink:
    url: str
    expires_at: str
    reused: bool

class AnswerLinkUnavailable(Exception): ...   # PUBLIC_WEB_BASE_URL 未配置

def issue_answer_link(store, settings, *, job_id: str, principal, ttl_hours: int | None) -> IssuedAnswerLink
    # job_kind 由前缀判定：askjob- → notebook，gask- → global，其余 ValueError
    # ttl_seconds = (ttl_hours or settings.mcp_answer_link_ttl_hours) * 3600
    # sweep_grace_seconds = settings.mcp_answer_link_grace_hours * 3600
    # url = f"{settings.public_web_base_url}/c/{token}"

class AnswerLinkExpired(Exception): ...

def resolve_answer_link(token: str, *, store, repo, global_service) -> dict | None
    # 返回公开快照行（带 gate 字段 notebook_id/created_by 或 notebook_ids/created_by，
    # 以及 "expires_at"），None = 404；过期 raise AnswerLinkExpired（D11 顺序）
```
`resolve_answer_link` 步骤：
1. `link = store.answer_link(token)`；None → None；`link["expired"]` → `AnswerLinkExpired`。
2. `principal = repo.refresh_agent_principal(link["agent_token_id"])`（`PermissionError` 视为 None）；
   None、`owner_id` 不等、`not principal.allow_answer_links`、`not principal_has_capability(principal, "ask:execute")` → None。
3. notebook：`origin = repo.ask_job_origin(job_id)`；`created_by`/`submitted_via` 不符 → None；
   `repo.require_agent_access(principal, "ask:execute", origin["notebook_id"])`（`PermissionError` → None）；
   `origin["memory_access"]` 为真时 `repo.require_agent_access(principal, "memory:read", nb)` 失败 → None；
   `detail = repo.ask_job_detail(job_id)`（KeyError → None），`status != "done"` 或 `output == "evidence"` → None；
   `answer = repo.ask_answer_detail(detail["answer_id"])`，None → None；返回
   `{"title": "", "created_at": answer["created_at"], "shared_through_at": "", "turns": [answer],
     "notebook_id": nb, "created_by": owner, "expires_at": link["expires_at"]}`。
   （会话分享单库谓词由 T6 的共用函数在路由里统一执行，见 T6。）
4. global：`row = global_service.public_answer_job(job_id, owner_id=owner, allowed_notebook_ids=principal.notebook_ids)`；
   None → None；`row["expires_at"] = link["expires_at"]`。

以上每一步只用显式 id 的仓库方法（`refresh_agent_principal`、`require_agent_access`、`ask_job_origin`、
`ask_job_detail`、`ask_answer_detail` 都是纯 store 委托，已核对 `repository_facade.py:1339-1360、3717-3730`）。

**测试** `backend/tests/test_answer_link_service.py`（SQLite 真仓库 + 真 store；单库作业按
`test_public_conversation_api._seed_shared_conversation` 的 SQL 方式种 `ask_jobs`（`submitted_via='mcp'`,
`status='done'`, `memory_access`）+ `answers`；token 经 `issue_agent_token` 真签）：
- `test_issue_builds_url_from_public_web_base_url_and_reuses`；
- `test_issue_without_public_web_base_url_is_unavailable`；
- `test_resolve_valid_notebook_link_returns_one_turn`；
- `test_expired_raises_expired_even_if_other_gates_fail`（D11 顺序）；
- `test_revoked_token_expired_token_disabled_profile_are_none`；
- `test_answer_links_switched_off_or_ask_tier_removed_is_none`；
- `test_allowlist_narrowed_is_none`；`test_owner_lost_read_is_none`；
- `test_memory_job_without_memory_read_now_is_none`；
- `test_browser_job_running_job_evidence_job_deleted_job_are_none`；
- `test_global_link_delegates_to_public_answer_job_with_allowlist`（假 global_service 断言入参）。

**验证**：`cd $W/backend && python3 -m pytest tests/test_answer_link_service.py -q -n 2`

---

### T6 公开路由第三分支、410 与图片（依赖 T5）

**目标**：`/api/public/conversations/{token}` 与图片端点按 `alnk-` 分派到 T5；单库分支的公开谓词与
`cshr` 共用一个函数；页面过期 410、图片一律 404；`Cache-Control: no-store` 与 gate 字段弹出全部继承。

**改文件**：
- `backend/app/api/ask_routes.py`：
  - 从 `_public_conversation_or_404`（`:1311-1375`）抽出 `_notebook_share_recheck(repo, row) -> None`
    （空创建者 / `user_can_read_notebook` / `mounts_still_effective(... conversation_library_ids(turns) ...)`，
    失败 404），`cshr` 分支改为调用它。
  - `_public_conversation_or_404(repo, token, *, expired_status: int = 404)`：
    `is_answer_link_token(token)` → `resolve_answer_link(token, store=answer_link_repository(), repo=repo, global_service=global_ask_service())`；
    `AnswerLinkExpired` → `expired_status == 410` 时 `raise user_error(410, "链接已过期，请重新提问")`，否则 404；
    None → 404；行无 `notebook_ids` 时（单库）再跑 `_notebook_share_recheck`。
  - 页面路由（`:1377-1422`）传 `expired_status=410`；弹出 gate 字段时一并 `pop("expires_at")`，投影后把它
    填进 `PublicConversation(expires_at=...)`。图片路由（`:1425-1486`）不改调用（默认 404）；全局链接行带
    `notebook_ids`，沿用 `:1466-1469` 的按库范围检查。
  - 在 `backend/tests/test_user_error.py` 登记 410 站点。
- `backend/app/models/ask.py`：`PublicConversation`（`:1333`）加 `expires_at: Optional[str] = None`。
- `backend/app/services/conversation_public_view.py`：模块 docstring 补一段「第三个命名空间 `alnk-`：单条
  回答、限时、同一白名单」；`public_conversation_payload` 不改。

**测试**（新建 `backend/tests/test_public_answer_link_api.py`，`TestClient` + 夹具照
`test_public_conversation_api.py:28-80`；全局作业种子照 `test_global_ask_share_api.py`）：
- `test_anonymous_reader_gets_one_whitelisted_turn_with_expires_at`（无 session；不含 `notebook_id`/`created_by`/
  `notebook_ids`/`source_id`/`memory_id`；`Cache-Control: no-store`）；
- `test_expired_link_is_410_with_chinese_detail_within_grace`；
- `test_swept_link_is_404`（推进时钟过宽限期后触发一次签发清扫，再打开 → 404）；
- `test_unknown_alnk_token_is_404`；
- `test_revoking_the_agent_token_kills_the_link_immediately`；
- `test_narrowing_allowlist_or_losing_read_kills_and_restoring_revives`；
- `test_unmounting_a_quoted_library_kills_the_notebook_link`（共用的 D-3 谓词）；
- `test_global_link_rechecks_every_cited_library`（全局作业跨库：撤掉一个被引用库的读权 → 404）；
- `test_image_alias_serves_while_valid_and_404s_once_expired`（`MINERU_RETURN_IMAGES` 开；别名由 alnk token
  派生；过期后图片 404 而页面 410）；
- `test_alias_from_a_cshr_share_of_the_same_image_does_not_resolve_under_alnk`；
- `test_cshr_and_gshr_behaviour_unchanged`（既有 `test_public_conversation_api.py`、
  `test_public_conversation_asset_api.py`、`test_global_ask_share_api.py` 全绿）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_public_answer_link_api.py tests/test_public_conversation_api.py \
  tests/test_public_conversation_asset_api.py tests/test_global_ask_share_api.py tests/test_user_error.py -q -n 2
```

---

### T7 MCP `ask` / `get_ask` 的 `delivery` 与 `link_ttl_hours`（依赖 T5；T3 的配置）

**目标**：§2.5。inline 行为逐字不变；link 只在 `answered` 时签发并返回摘要页。

**改文件**：
- `backend/app/api/mcp_tools/ask.py`：
  - 新增 `_validate_delivery(delivery, output, link_ttl_hours) -> None`，在 `ask` 里紧跟
    `_validate_output`（`:1164`）调用、任何仓库工作之前：`delivery ∉ {"inline","link"}` → `invalid_argument`；
    `output == "evidence"` 且 `link` → `invalid_argument`（「仅检索不产生回答，不能签发回答链接」）；
    `inline` 却传了 `link_ttl_hours` → `invalid_argument`；`link_ttl_hours` 不是 `1..settings.mcp_answer_link_ttl_hours`
    → `invalid_argument`（文案写出上限）；`link` 且 `settings.public_web_base_url == ""` → `unavailable`
    （「部署未配置 PUBLIC_WEB_BASE_URL，暂不能签发回答链接，请联系部署管理员」）。
  - `run()` 里 `principal = _live_principal(repo)` 之后：`delivery == "link"` 且
    `not principal.allow_answer_links` → `forbidden`（「此凭证未开启『允许签发限时回答链接』，请在 Agent 接入页为它打开后重试」）。
  - `_run_notebook`（`:780-881`）与 `_run_global`（`:933-1055`）各加 keyword 参数
    `delivery: str = "inline"`、`link_ttl_hours: int | None = None`，末尾的 `return answer_page(view)`
    （`:881`、`:1055`）换成 `return _deliver(repo, live, view, delivery, link_ttl_hours)`；
    `:1198-1206` 两处调用一律改成关键字传参（顺带修正 `:1203-1206` 漏传 `output` 的位置参数写法——
    该路径 `output` 必为 `answer`，行为不变，但改成关键字以免新参数错位）。
  - 新增 `_deliver(repo, live, view, delivery, link_ttl_hours)`：`delivery == "inline"` 或
    `view["status"] != "answered"` → `answer_page(view)`；否则复核 `live.allow_answer_links`（交付时刻，
    运行途中被关掉 → `forbidden`，回答已保存可 inline 读），调 `issue_answer_link(answer_link_repository(),
    get_settings(), job_id=view["job_id"], principal=live, ttl_hours=link_ttl_hours)`，再 `link_page(...)`。
  - `get_ask`（`:1213-1262`）加同名两个参数：先 `_validate_delivery(delivery, "answer", link_ttl_hours)`；
    `link` 时任一 offset 非 0 → `invalid_argument`；`link` 时用 `_live_principal` / `_global._authorize` 得到的
    principal 检查 `allow_answer_links`；最后统一走 `_deliver`。
  - 两个工具的 `description`（`:1106-1151`、`:1216-1224`）补一段英文说明（`delivery="link"`：只返回
    `preview` 纯文本摘要与 `link.url`/`link.expires_at`，任何人拿到链接无需登录即可查看完整回答，链接到期失效；
    需要 token 开启 answer links；`link_ttl_hours` 可缩短不可超过部署上限；同一作业重试复用同一条链接；
    不能与 `output="evidence"` 同用）。
- 新建 `backend/app/api/mcp_tools/answer_link_page.py`：
  `link_page(view, preview, preview_truncated, link: IssuedAnswerLink) -> dict`，组装 §2.5 形状经
  `_budget_response`；接受条件：`preview`、`link.url`、`link.expires_at`、`job_id`、`status` 原样留存，否则
  `AgentToolError("internal", ...)`（`url` 已在 `WHOLE_OR_NOTHING_FIELDS`，`_shared.py:66`）。摘要用
  `answer_preview(view["answer"], settings.mcp_answer_preview_max_chars)`（全文，不是分页后的 2000 字）。
- `backend/tests/test_mcp_bundle_architecture.py`：若有逐工具参数/描述钉，按新参数更新（不放宽断言）。

**测试**（新建 `backend/tests/test_mcp_answer_links.py`，复用 `test_memory_mcp.py:249` 的 `mcp_env`
夹具写法（必要时复制最小夹具，不从 7000 行文件 import 私有 helper）；全局用 `test_global_ask_mcp.py:277`
的 `adapter` 夹具）：
- `test_inline_default_is_byte_identical`（不传 delivery 与传 `inline` 的响应完全相等）；
- `test_link_returns_preview_url_and_expiry_without_answer_or_citations`（无 `answer`/`citations`/`trace` 键；
  `link.url` 以 `PUBLIC_WEB_BASE_URL + "/c/alnk-"` 开头；`expires_at` 带时区；≤ 20 键）；
- `test_link_preview_is_built_from_the_full_answer`（答案前 2500 字是表格 → 摘要含「（表格，见链接）」及其后正文）；
- `test_keyed_retry_reuses_the_same_link`（同 `client_request_id` 两次 → 同 URL）；
- `test_get_ask_link_issues_then_reuses_and_rejects_offsets`；
- `test_evidence_with_link_is_invalid_before_any_work`；
- `test_ttl_over_deployment_cap_or_below_one_is_invalid`；`test_ttl_with_inline_is_invalid`；
- `test_missing_public_web_base_url_is_unavailable_before_any_work`；
- `test_token_without_answer_links_is_forbidden`；
- `test_switch_turned_off_mid_run_is_forbidden_at_delivery_and_answer_is_saved`；
- `test_clarification_failed_and_cancelled_return_the_inline_shape_and_issue_nothing`（store 中无行）；
- `test_global_ask_link_and_star_scope`（2 库与 `["*"]` 各一条）；
- `test_plugin_engine_answer_gets_a_link`（单库插件引擎模式）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_mcp_answer_links.py tests/test_global_ask_mcp.py \
  tests/test_mcp_bundle_architecture.py -q -n 2
cd $W/backend && python3 -m pytest tests/test_memory_mcp.py -q -n 2 -k "ask"
```

---

### T8 前端：公开页过期态与失效时间、`/agents` 开关（依赖 T2 的契约；可与 T5–T7 并行）

**目标**：§2.6/§2.7 的界面落地；文案过 `docs/ui-vocabulary.md`；控件反馈符合 AGENTS.md「Interactive feedback」。

**改文件**：
- `frontend/app/public-conversation.ts`：`PublicConversationT.expires_at?: string`；`fetchPublicConversation`
  （`:118-142`）改返回 `PublicConversationResult`，404 → `{kind:"missing"}`、410 → `{kind:"expired"}`、
  其余错误继续抛出。
- `frontend/app/c/[token]/page.tsx`（`:207-282`）：`LoadState` 加 `expired`，渲染
  `<h1>链接已过期</h1><p className="public-report-note">链接已过期，请重新提问。</p>`；ready 且
  `conversation.expires_at` 时：eyebrow 改「问答回答 · 只读分享」，`h1` 兜底「问答」，meta 里加
  `<span>此链接将于 <PublicTime value={conversation.expires_at} /> 失效</span>`（`PublicTime :580` 的格式即
  `YYYY-MM-DD HH:mm`，若不是则在此处按该格式渲染）。会话分享（无 `expires_at`）的页面逐字不变。
- `frontend/app/agent-token-model.ts`：`AgentTokenDraft.allow_answer_links`（新建草稿默认 `false`）；
  `AgentTokenAccess.allow_answer_links?: boolean`；`agentTokenEditDraft`、`agentTokenAccessSnapshot`、
  `agentTokenAccessRequest`（`:226-290`，PUT 体必带该字段）、`agentTokenAccessChanged`（`:296-301`）、
  签发请求 `agentTokenRequest`（`:97`）都带上；新增纯函数
  `withScopes(draft, scopes)`：去掉 `ask` 时同时把 `allow_answer_links` 置 `false`；
  `isCompleteAgentTokenDraft`：开关开而无 `ask` → 不完整。
- `frontend/app/agent-access-manager.tsx`：`AgentAccessFields`（`:110-205`）在权限 fieldset 之后加一个
  复选框「允许签发限时回答链接」，未勾选「问答」时禁用并提示「需要先勾选问答」；勾选时在其正下方显示告知：
  「开启后，此 Agent 可以为它的回答生成限时公开链接：任何拿到链接的人无需登录即可查看完整回答，可能包含
  引用到的个人记忆摘录。链接按部署设置的有效期自动失效（默认 7 天）。」；token 列表行（`:951` 旁）在开启时
  追加「可签发回答链接」。保存结果沿用既有行内反馈（`saveTokenAccess :779`），不加页顶横幅。
- `frontend/app/globals.css`：仅当新增元素需要样式时追加，不动 `button:…:active` 基线规则。

**测试**：
- `frontend/tests/unit/agent-token-model.test.mjs`：草稿默认关；去掉 ask 自动关；请求体与 expected 快照都带
  字段；`agentTokenAccessChanged` 只改开关为真；无 ask 时不完整。
- `frontend/tests/component/agent-access-manager.component.test.tsx`：签发表单勾选后 POST 体带
  `allow_answer_links: true`；未选问答时开关禁用；编辑保存 PUT 体带字段；告知文案出现在开关旁；列表标记。
- `frontend/tests/component/public-conversation-page.component.test.tsx`：410 → 过期文案；带 `expires_at`
  显示「此链接将于 … 失效」；无 `expires_at` 的会话分享页面不变。

**验证**：
```bash
cd $W/frontend && npx vitest run tests/component/agent-access-manager.component.test.tsx \
  tests/component/public-conversation-page.component.test.tsx
cd $W/frontend && node --test tests/unit/agent-token-model.test.mjs
cd $W/frontend && npx tsc --noEmit
```

---

### T9 文档、SOP 与更新说明（依赖 T6、T7、T8）

**目标**：只写已落地且验证过的行为；中英文配对；文档守卫全绿。

**改/建文件**：
- `docs/product-and-api_zh.md` / `docs/product-and-api.md`：
  - MCP `ask` 节（zh `:1300-1440` 一带，紧接「只检索：`output="evidence"`」那段；en 对应段）：`delivery`、
    `link_ttl_hours`、响应形状、组合规则、重试复用（D2/D10）、`forbidden`/`unavailable` 错误；`get_ask` 同名参数。
  - 「全局会话分享」（zh `:392-400`）与「问答会话公开分享护栏」（zh `:2942`、en `:3904`）：第三个命名空间
    `alnk-`、单条回答、过期 410/宽限期/清扫后 404、读时复核清单（D6）、图片 404、`expires_at` 字段。
  - Agent token 访问配置（`PUT /agent-tokens/{id}/access` 段）：`allow_answer_links` 字段、422 条件。
  - `answer_links` 表（SQLite v94 / PG `0074_answer_links.sql`）一句话登记。
- `docs/deployment-and-configuration_zh.md` / `.md`：MCP 部署段（zh `:222-240`）加 `PUBLIC_WEB_BASE_URL`
  （为何不从请求头推导、允许 http、子路径）与三个 `MCP_ANSWER_*` 配置及边界。
- `docs/agent-mcp-memory-sop_zh.md` / `.md`：§3（Token 创建：开关与告知）、§5 新增「发到聊天工具：
  `delivery="link"`」小节、§9「错误码」加 `forbidden` 场景、§10「撤销与轮换」说明撤销 token 即让其链接失效。
- `docs/user-manual_zh.md`：§12.1 Agent 接入加开关说明；§10.7 公开链接加「机器人发来的限时回答链接」。
- 新建 `release-notes/mcp-answer-link.md`：`level: feature`、`audience: all`、标题如「Agent 可以把回答发成
  限时链接」，正文用界面措辞描述开关与链接到期（按 `release-notes/README.md`）。

**验证**：
```bash
cd $W/backend && python3 -m pytest tests/test_architecture_documentation.py tests/test_cross_doc_merge.py -q -n 2
```

---

### T10 全量门禁与交付（主 agent）

见第 4 节；之后按 CLAUDE.md 的 codex 评审闭环开 PR、贴评审、`verify` 后合入。开 PR 前
`git log --merges origin/master..HEAD` 必须为空；每次推送前 `git fetch` 复核迁移号（风险 R8）。

---

## 4. 全量门禁

来自 `docs/development_zh.md`（G1 / G3）：
```bash
# G1 标准门（经全局锁）
bash ~/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh alnk-g1 -- \
  bash $W/scripts/check.sh
# G3 PostgreSQL（经全局锁；本机一次性库，见第 0 节）
bash ~/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh alnk-g3 -- \
  bash -c 'cd /Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/mcp-global-ask-issue-019586/backend && TEST_POSTGRES_URL=postgresql://huzhifeng@localhost/silicon_notebook_alnk_test python3 -m pytest tests/postgres -m "postgres_integration or postgres_lane_contract" -q -n 1'
# 架构守卫（秒级，可直接跑）
python3 $W/scripts/check_architecture_boundaries.py
```
`check.sh` 末尾的退出码直接读；已知本机恒红的 packaging/dotenv 用例按既有基线证据在 PR 说明，不当通过。
G2 扩展门（`scripts/check_extended.sh`）含 `legacy_upgrade_contract`（快照 verifier 的已部署升级用例），
T1a 新增的升级用例属于它，本地至少单独跑 `tests/test_repository_snapshot_verifier.py`。

---

## 5. 风险清单

- **R1 签发并发**：同一 `(作业, token)` 两个并发签发（重试与 get_ask 同时）。由唯一索引 +
  `ON CONFLICT DO NOTHING` + 同事务读回保证只有一条且双方拿到同一 URL；T1b 的 PG 用例用两个线程/连接
  实测。SQLite 由 `BEGIN IMMEDIATE` 串行。
- **R2 过期边界与时钟**：有效当且仅当 `expires_at > now`；宽限期内 410，`expires_at < now - grace` 才可删；
  SQLite 用规范化 UTC `…Z` 字符串比较（不同格式会比错，store 写入与比较都走 `normalize_timestamp`）；
  所有时间取 store 的 `now` 接缝，测试注入假钟，不用墙钟断言（避免已知的计时 flake 类）。
- **R3 白名单收窄 / 权限位关闭 / tier 被撤**：每次打开都按签发 token 的**当前**行复核（`refresh_agent_principal`
  一条语句读出整份访问配置，`postgres/memory_store.py:412-445` 的既有保证），任一失败 404；关闭开关后再打开
  会让旧链接复活（与会话分享「恢复读权即复活」一致），在文档中写明。
- **R4 全局作业跨库**：复核集合是 resolved ∪ 引用/锚点库（比 get_ask 的 resolved 更严），且必须 ⊆ token
  白名单；任一库失权整页 404；图片端点只服务本次复核过的库里的资产（既有 `:1466-1469`）。
- **R5 图片别名随过期失效**：别名 = HMAC(alnk token, asset_id)，过期/失效后页面与图片同时不可用；
  同一图片在 cshr 与 alnk 下别名不同（T6 用例钉住），不能互通。
- **R6 记忆披露**：单库作业运行时开过个人记忆通道而 token 此刻没有 `memory:read` → 404（与 MCP 重放规则同）；
  公开投影里记忆引用照会话分享显示为「作者的个人记忆」，不带 `memory_id`。开关的告知文案是唯一一次确认，
  T8 测试钉住文案出现在开关旁。
- **R7 匿名路径误用 request user**：`resolve_answer_link` 与 `public_answer_job` 只调显式 id 方法；T6 加一条
  用例在无 session 下打开链接并断言没有走 seeded admin（例如管理员可读而主人不可读的库 → 仍 404）。
- **R8 迁移号撞车**：主线约每几小时进一个迁移。T1a 第零步与每次推送前 `git fetch` 比对
  `origin/master` 最新号；撞了立即顺延并重跑 T1a 的版本钉扫荡（PG 同号文件并存不报 git 冲突，会静默重号）。
- **R9 删除路径不级联**（D3）：作业/会话/token 删除后链接立刻 404（读时复核），残行由清扫回收；清扫在
  签发写事务里，单次删除量随表大小线性——表上界 ≈ 签发速率 ×（TTL + 宽限期），量级可忽略。
- **R10 shadow/sync 本地表**（D8）：切换 shadow 或跨环境同步后旧链接 404，机器人用 `get_ask(delivery="link")`
  重签；写进部署文档。
- **R11 输出预算**：link 页只有十余个键、摘要 ≤ 2000 码点，远低于 12 000 字节；仍经 `_budget_response`
  并验证 `url`/`preview` 原样留存，失败报 `internal` 而不是发出半截 URL。
- **R12 已有调用方兼容**：`AgentTokenAccessUpdate.allow_answer_links` 必填，旧前端标签页 PUT 会 422（而不是
  静默关掉开关）；同 PR 更新前端，发布说明不涉及。
