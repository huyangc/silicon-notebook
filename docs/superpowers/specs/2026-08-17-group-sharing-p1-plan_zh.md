# 群组知识共享 P1 实现计划(群组 + 成员侧)

> 依据:`2026-08-17-group-knowledge-sharing-design_zh.md`(§3-§6、§10 P1)。
> P0(授权层重构)已合入(PR #516)。本计划是 P1 各实现任务的规格真源。
> P1 交付:群组 CRUD/成员管理(项目人人可建,部门/领域仅系统管理员)、组管理员
> 共享库给组、笔记本列表「群组」分区、挂载有效性扩展(读权⇒可挂)、成员在共享
> 库内提问与建**自己的**深度报告。P2 才做:组管理员写权(能力翻转)、成员贡献
> 审批流。

## 任务切分(顺序执行,每任务后双评审)

### T1 schema + shadow 全链路(零行为变化)

SQLite `_migration_49` + PG `0027_group_sharing.sql`(manifest 49/27):

```sql
CREATE TABLE groups (
  id          TEXT NOT NULL PRIMARY KEY,     -- 显式 NOT NULL:免进 _SQLITE_NULL_GUARD_KEYS
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'project',   -- project|department|domain(app 层校验,不加 CHECK)
  description TEXT NOT NULL DEFAULT '',
  created_by  TEXT REFERENCES users(id),         -- 同 notebooks.created_by:无 ON DELETE 子句
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE group_members (
  group_id  TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id   TEXT NOT NULL REFERENCES users(id),
  role      TEXT NOT NULL DEFAULT 'member',      -- member|admin(app 层校验)
  added_at  TEXT NOT NULL,
  added_by  TEXT REFERENCES users(id),
  PRIMARY KEY (group_id, user_id)                -- 两列显式 NOT NULL(仿 notebook_members)
);
CREATE INDEX idx_group_members_user ON group_members(user_id);
CREATE TABLE notebook_grants (
  id             TEXT NOT NULL PRIMARY KEY,
  notebook_id    TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  principal_type TEXT NOT NULL,      -- user|group|group_admins|everyone(app 层校验,不加 CHECK:
                                     -- 保住两个 SENTINEL_TEXT 停车候选,见盘点 §3.3)
  principal_id   TEXT,               -- 多态引用,刻意无 FK(仿 catalog_candidates.job_id 先例)
  role           TEXT NOT NULL,      -- viewer|admin
  created_by     TEXT REFERENCES users(id),
  created_at     TEXT NOT NULL,
  UNIQUE (notebook_id, principal_type, principal_id)
);
CREATE INDEX idx_notebook_grants_nb ON notebook_grants(notebook_id);
CREATE INDEX idx_notebook_grants_principal ON notebook_grants(principal_type, principal_id);
```

PG 侧全部文本列 `COLLATE "C"`,FK 显式 `ON DELETE CASCADE`(notebook_grants→notebooks、
group_members→groups)/`NO ACTION`(→users,仿 notebook_members)。

改动面速查(逐项照盘点执行,见本文件同日的盘点结论):
- SQLite:`_migration_49` + `SCHEMA_VERSION=49` + 头部版本注释。
- v9 fixture:`scripts/generate_repository_contract_fixtures.py` 默认模式重生成
  `expected_snapshot.json`/`manifest.json`;`test_repository_v9_fixture.py` 的
  `user_version==49` 断言 + 中文历史注释手改。
- `scripts/verify_repository_snapshot.py`:`GROUP_SHARING_TABLES`/`_INDEXES` +
  `MIGRATION_MANIFEST[(48,49)]`(仿 v39 `(38,39)`,不动 SPECIAL_TABLES);
  `test_repository_snapshot_verifier.py` 新增 `_rollback_v49` +
  `test_deployed_v48_database_verifies_group_sharing_tables`,`_rollback_v34`
  聚合函数补三张表。
- PG:新文件 `0027_group_sharing.sql`(绝不改旧文件——checksum 账本);
  `schema_manifest.py` 的 `POSTGRES_BUSINESS_TABLES` 按字母序加三表名,版本 49/27。
- shadow `manifest.py`:`RUNNING_SCHEMA_PAIR=SchemaPair(49,27,epoch=1)`;`_TABLES`
  加三条 `_table(...)`(copy_rank 80/81/82,`DECLARED_PK`;`group_members` 照抄
  `notebook_members` 那条的形状;transform 用 `"timestamptz"`——三表无 JSON 列)。
  PK 列全部显式 NOT NULL,因此**不进** `_SQLITE_NULL_GUARD_KEYS`。
- shadow `replicator.py`:**零改动**(UNIQUE 是非条件约束,自动发现;
  `notebook_grants` 的停车列自动落在 `principal_type`(SENTINEL_TEXT),
  `principal_id` 保持裸 text 列、全程不得加 CHECK/FK)。
- 深拷贝:`notebook_grants` 照 `notebook_members` 先例**不进**
  `_COPY_SNAPSHOT_QUERIES`/`_COPY_VALIDATED_TABLES`,在清单前补
  「Deliberately absent」注释(访问控制状态不是知识,副本由新 owner 重新授权);
  `groups`/`group_members` 不挂 notebook,天然不涉及。
- 文档数字(T5 统一改,T1 别动):48/26→49/27;74 表→77;100 surface→104;
  锁 ledger+74→77;**12 row slots 不变**(盘点已重算:三表都是浅层)。

验收:全量后端泳道绿;`test_schema_version_pairing`/`test_shadow_schema_wording_guard`
绿(构造期断言自动验证可停车与 FK 闭包);v9 回放绿;snapshot verifier 新用例绿。

### T2 授权谓词扩展 + 挂载有效性(后端核心,安全关键)

在 P0 的唯一定义点上扩展,**每一处都双后端同修**:

1. `access_sql.py`:读权从「owner ∨ notebook_members」扩为
   「owner ∨ notebook_members ∨ **有效授权边**」。有效授权边 = notebook_grants
   存在一行使得:
   - `(principal_type='user' AND principal_id=:user)`,或
   - `(principal_type='group' AND EXISTS group_members(gm.group_id=principal_id AND gm.user_id=:user))`,或
   - `(principal_type='group_admins' AND EXISTS group_members(... AND gm.role='admin'))`,或
   - `(principal_type='everyone')`。
   role 维度 P1 只消费 viewer/admin 的**读**含义(任何有效授权边都 ≥ viewer=可读);
   `effective_role`(区分 viewer/admin)以独立查询提供给 API 投影,不进热读谓词。
   写权谓词**不动**(owner-only,能力翻转是 P2)。
2. 片段函数签名保持(`read_access_clause` 等),参数个数会变——**全部消费者自动
   跟随**,但两类站点必须手工同步(P0 已登记):PG 三处两段式 FOR SHARE、SQLite
   `_lock_memory_aggregate_on`(它们的成员探测半支要扩成「成员∨授权边」,锁语义
   不变);`test_access_sql_contract.py` 的两段式 allowlist 数字随之核对。
3. `mount_sql.py` `MOUNT_VALID_EXPR`:`b.status != 'copying' AND (b.tier='base'
   OR b.created_by=a.created_by OR <a.created_by 对 b 有读权(嵌入片段)>)`。
   读权⇒可挂载(设计 §6 的显式行为变更,PR 里登记)。挂载方 owner 判定口径不变
   (`a.created_by`,与请求用户无关)。`mountable_notebooks` 同步(它复用
   MOUNT_VALID_EXPR)。
4. `postgres/search.py` 与 memory store 嵌入片段:随片段自动跟随,逐一核对参数
   元组(参数个数变化是本任务最大的机械风险面——每个调用点的占位符数必须重数)。
5. 测试:`test_access_sql_contract.py` 矩阵扩展(新主体:user-grant 持有者/
   组成员经 group 授权/组管理员经 group_admins 授权/非组成员/everyone;
   踢出组成员即刻失读权;删授权边即刻失读权);挂载有效性新用例(成员挂共享库、
   撤授权边失效、恢复即生效——仿 test_multi_domain_bases 既有写法);PG 侧
   conformance 矩阵同扩。守卫自检:自省 parity 自动覆盖新片段(新增可调用符号
   要登记 `_CALLABLE_PROBES`)。

⚠ 性能:读谓词多两层 EXISTS(grants 按 (notebook_id) 或 (principal_type,
principal_id) 索引点查、group_members 按 PK/(user_id) 索引),单组几百人规模
(设计决策 11)不需要缓存;不得引入全表扫描形状。

### T3 群组与授权 API(后端)

新 router `backend/app/api/group_routes.py`(挂进 routes.py 组合;新端点跑默认
模式刷 `api_contract.json`):

- `POST /groups` {name, kind, description}:kind=project 人人可建;
  department/domain 内联 `user.role != "admin"` → 403(仿 set_notebook_tier)。
  创建者自动成为组管理员(group_members role=admin)。
- `GET /groups`:我所在的组(含我的角色、成员数);`?scope=all` 仅系统管理员
  (全局管理面)。
- `GET /groups/{id}`:组详情 + 成员清单(仅组成员可见;404 不泄露存在性)。
- `PATCH /groups/{id}` {name, description} / `DELETE /groups/{id}`:组管理员。
  删除组:同一写事务里清掉 `notebook_grants` 中 principal 指向该组的行
  (principal_id 无 FK,不清就是孤儿授权行——谓词侧因 join group_members 而
  失效,但共享管理列表会残留)。
- `PUT /groups/{id}/members/{user_id}` {role} / `DELETE /groups/{id}/members/{user_id}`:
  组管理员;不得移除/降级最后一名管理员(app 层校验,409)。
  `DELETE /groups/{id}/membership`:自助退出(最后一名管理员不得退出)。
- `GET /users/resolve?username=`:精确用户名 → {id, username, display_name},
  供组管理员加人;仅登录用户可调(内部部署,接受用户名可探测,PR 登记)。
- 授权边管理(挂 notebook 维度,`notebook:manage` 能力守卫——P1 即 owner):
  - `GET /notebooks/{id}/grants`:该库全部授权边(含组名解析)。
  - `POST /notebooks/{id}/grants` {principal_type: group|group_admins,
    principal_id, role}:策略校验 = 请求者对库有 manage 权 **且** 是目标组组
    管理员(设计决策 9);(group, viewer) 与 (group_admins, admin) 由前端按
    「共享给群组」一次发两条。everyone 仅系统管理员(现阶段仍走 set_notebook_tier
    兼容,不在 UI 暴露)。user 主体继续走既有 share_token 流(不经此端点)。
  - `DELETE /notebooks/{id}/grants/{grant_id}`:撤销不对称——库 manage 权持有者
    **或** 目标组组管理员任一即可(后者经组维度入口:
    `DELETE /groups/{gid}/shared-notebooks/{notebook_id}` 删除指向本组的两行)。
  - `GET /groups/{id}/shared-notebooks`:组管理员视角「共享给本组的库」清单。
- 列表投影:`list_notebooks` 新增「群组」分区——经有效组授权边可读、且非 owner
  非 member 的库,`NotebookSummary.access="reader"`(沿用现枚举,P1 不新增
  access 值;`shared_from` 显示 owner 用户名,另加 `granted_via: [{group_id,
  group_name, kind}]` 新字段供前端标注「来自群组《X》」)。默认全选检索范围、
  快照隐藏参与者等 Ask 管线按 reader 同口径,零新分支。
- 成员建自己的报告(设计 §4):`reports:write` 能力对**创建**放宽为
  「读权 + 行级」:create/confirm/outline/generate/cancel/delete/share 改为
  `require_notebook_read` + 体内行级校验 `reports.created_by == user.id`
  (owner 对自己库里别人的报告仍可读不可改;列表/详情读端点按读权 + 全部可见?
  ——**不**:会话不可见的决策不延伸到报告,报告库内成员互相可见吗?设计决策 1
  说「他人不可见,分享走公开链接」→ 列表/详情也按 `created_by` 过滤,只有
  notebook owner 与创建者本人可见自己的(owner 只见自己的;不引入「owner 看
  全部」的新披露)。`reports:write` 能力名保留给 P2 的管理动作。)
  ⚠ 这是 P1 唯一放宽写面的点,单独测试矩阵:成员可建/生成/取消/删/分享自己的
  报告;不可动他人的(404);陌生人全 404;owner 行为逐字不变。
- Ask:reader 已可提问(现状),组授权 reader 走同一读守卫,自动生效——
  验证用例即可,零代码。

错误文案:全部经 `user_error()`;前端翻译进 `frontend/app/errors.ts`。

### T4 前端(全栈对等)

- **群组页/弹窗**(入口:顶栏或设置区「群组」):我的群组列表(按 kind 标注
  项目/部门/领域)、建组(kind 选择,非管理员隐藏部门/领域选项)、组详情
  (成员清单、加人[用户名精确查找]、改角色、移除、退出)、共享给本组的库清单
  (组管理员可撤销)。浮动弹窗复用 `FloatingModalCard`/`use-floating-window`。
- **分享弹窗**:既有「只读共享」旁新增「共享给群组」——选组(仅列出我是组管理员
  的组)、勾选「组管理员可管理」(→ 追加 (group_admins, admin) 行,P1 提示
  「管理权将在后续版本生效」或先不勾选默认只发 viewer——**取后者**:P1 只发
  (group, viewer),group_admins 行留给 P2 一起上,避免发出一条当前无效果的边)。
  已共享条目列表 + 撤销。
- **笔记本列表**:「群组」分区,卡片标注「来自群组《X》」(granted_via);
  reader 行为(隐藏写按钮)复用既有 `isReader` 派生,零新分支。
- **挂载选择器**:mountable 自动包含组授权可读的库(后端已扩),前端零改动,
  验证即可。
- **报告**:成员在共享库可见「深度报告」入口并可创建(移除 reader 隐藏逻辑中
  报告入口那一条);报告列表只显示自己的。
- 界面词:群组/成员/组管理员/项目/部门/领域/共享给群组——过
  `scripts/check_ui_vocabulary.py`;不得出现 grant/principal/viewer 等内部词。
- 组件测试:群组弹窗、分享给群组流、列表分区标注(进 frontend/tests/
  {unit,component},生产代码只进 app/features)。

### T5 文档 + 门禁 + PR

- 文档数字与版本(T1 留下的):CLAUDE.md(schema 红线段 + shadow 大段)、
  AGENTS.md(3 处)、docs/development*.md(各 2 处):49/27、77 表、104 surface、
  锁 ledger+77;12 不变。逐版本历史各接一句 v49/v27。
- 产品文档:docs/product-and-api*.md 群组章节(端点、角色矩阵、数值上限:组名
  长度、每组成员上限?——v1 不设硬上限,只登记分页页大小);README 两份的共享
  能力句;AGENTS.md Product Flow 相应段;CLAUDE.md 红线段按需。
- `fangan_done.md` 不动(群组不在 silicon_notebook_fangan.md 范围)。
- G1 + G2 扩展门按需;PR + codex 闭环(评审非阻塞 + CI 绿 + verify 三闸)。

## 已定裁决(实现子代理不得自行更改)

1. `principal_id` 全程保持裸 `COLLATE "C"` text 列——不加 CHECK/FK(停车方案
   依赖它或 principal_type 至少一个可停车)。
2. `principal_type`/`kind`/`role` 的取值校验全在 app 层(Pydantic/服务层),
   schema 不加 CHECK。
3. 深拷贝不带授权边;删组同事务清孤儿授权行。
4. P1 分享 UI 只发 (group, viewer) 行;(group_admins, admin) 行的发放随 P2 上。
5. 报告可见性按创建者隔离(owner 也只见自己的),分享走既有公开链接。
6. Agent/MCP 面零改动(owner-only 红线)。
7. `NotebookSummary.access` 不新增枚举值;群组来源经 `granted_via` 新字段表达。
