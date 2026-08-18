# 群组知识共享 P2 实现计划(组管理员写权 + 审批流)

> 依据:设计稿 `2026-08-17-group-knowledge-sharing-design_zh.md` §4/§5/§10;
> P0(#516)、P1(#517)已合入。P1 计划(`2026-08-17-group-sharing-p1-plan_zh.md`)
> 的「已定裁决」全部继续有效。

## 交付目标

1. **组管理员写权**:持有 `admin` 角色有效授权边的用户(经 `group_admins` 行,
   或直接 `group` 行 role=admin)获得该库的内容管理能力——添加/删除/重新解析
   来源、触发图谱与检索索引构建、knowhow 写、知识治理写、命令目录写、共享管理
   (`notebook:manage`)。`notebook:delete` 恒 owner。Agent/MCP 面**不放开**
   (红线不变)。
2. **成员贡献审批流**:普通成员把自己的库共享给组 = 提交申请,组管理员审批;
   批准同事务写 `(group, viewer)` 边(设计 §4 决策 9)。
3. **group_admins 边发放**:分享弹窗补「组管理员可管理」勾选(P1 裁决 4 留下的
   另一半),发 `(group_admins, admin)` 行;撤销同组两行一起。

## 任务切分(顺序执行,每任务双评审)

### T1 schema v50/PG28:notebook_share_requests(照 P1-T1 全链路模板)

设计 §5 的 DDL(status 默认 pending,app 层校验取值,无 CHECK;两个 FK 均
CASCADE;`requested_by` → users 无 ON DELETE 子句)。改动面照 P1-T1 清单逐项:
SQLite `_migration_50` + SCHEMA_VERSION=50;PG `0028_share_requests.sql`;
schema_manifest 50/28;shadow manifest `_TABLES` 追加(copy_rank 83,
DECLARED_PK,timestamptz;PK 列显式 NOT NULL 免 null-guard);replicator 零改动
(无条件唯一索引;`idx_share_requests_group` 非唯一);v9 fixture 重生成 +
`user_version==50`;snapshot verifier `(49,50)` + `_rollback_v50` + 新用例;
merge_dbs 分类(**notebook-scoped**,随 notebook 走——申请挂在库上;group FK
在合并序里先于它,天然满足);深拷贝**不带**申请(与授权边同理,Deliberately
absent 注释);构造期探针预期:78 表 / 105 surface(+1 PK)/ FK 闭包重算(申请
表挂 notebooks+groups+users,浅层,预计仍 12——**必须实证**,变了就同步文档
数字)。文档数字留 T4 统一改。

### T2 能力翻转(安全核心,opus)

1. `access_sql` 新增 **admin 级判定**:`NOTEBOOK_ADMIN_SQL` / 可嵌片段——
   owner ∨ 存在 `role='admin'` 的有效授权边(principal 四臂同读权,只多
   `ng.role='admin'` 条件;everyone 行按设计不会有 admin role,但谓词不特判,
   app 层发放口径挡)。双后端镜像 + `_CALLABLE_PROBES` + parity。
2. `deps.py`:`_CAPABILITY_LEVELS` 五个能力(`sources:write`/`kg:write`/
   `knowhow:write`/`knowledge:write`/`catalog:write` + `notebook:manage`)从
   `"owner"` 翻成 `"admin"`;工厂与 `notebook_capability_allowed` 增加
   `"admin"` 级分支(解析到新谓词);`notebook:delete`/`reports:write` 不动。
   值域冻结测试同步为 {"owner","admin"}。
3. **表外消费点清单**(deps.py 注释登记的,逐项核对):
   - `knowledge_routes.can_edit` 投影 → 改用 admin 判定(组管理员按钮要亮);
   - `user_or_agent_scope` Agent 面 → **不动**(owner-only 红线);
   - knowhow transfer 写半已走能力表 → 自动跟随;parse/delete_source 体内已走
     `notebook_capability_allowed` → 自动跟随;
   - P0 守卫测试里「写权恒 owner-only」的矩阵格与 docstring 按新语义改写
     (这是**刻意翻格**,测试要点名翻的是哪几格,仿 T2 的 legacy_read 手法)。
4. **前端写权感知**:`NotebookSummary` 新增 `can_manage_content: bool`(有效
   admin 判定,列表与详情都回填;裁决 7 的 access 枚举仍不动)。
   `workspace-transitions.workspaceCapabilities` 改吃它(canWrite 之外的内容
   管理能力位);组管理员打开共享库:顶栏仍标「来自群组」,但来源添加/删除/
   重解析/构建/knowhow 写入口可用。⚠ MCP/Agent 面与浏览器面的分歧在 UI 不
   体现(Agent 用不了是 token 层的事)。
5. 测试矩阵:组管理员(经 group_admins 边)可上传/删除来源、触发构建、写
   knowhow;普通成员仍 404;组管理员对 `notebook:delete` 仍 404;撤边即失效;
   admin 边指向已删组 fail-closed;挂载/读权行为零变化(legacy 矩阵不动)。
   PG conformance 同扩。配额红线用例:组管理员上传计 owner 配额(现状即如此,
   钉住)。

### T3 审批流 API + 分享弹窗补全(含 group_admins 发放)

- API(group_routes 扩展):`POST /notebooks/{id}/share-requests`(读权者对
  **自己 owner 的库**……不对:申请者是库 owner 本人、目标组的普通成员——
  校验「请求者对库有 manage 权 ∧ 是目标组成员(非管理员也可)」;组管理员
  直接走既有 grants 端点不必申请);`GET /groups/{id}/share-requests`
  (组管理员,pending 清单);`POST /groups/{id}/share-requests/{rid}/approve`
  (组管理员;同写事务:复核申请 pending + 组在 + 写 `(group, viewer)` 边,
  重复授权按 409 语义转 already-shared 终态)与 `/reject`;申请者可撤回
  (`DELETE /notebooks/{id}/share-requests/{rid}`,pending 时)。孤儿治理:
  删组 CASCADE 带走申请(FK 已有);删库同理。
- 分享弹窗:「共享给群组」对我是组管理员的组直接发(现状),对我只是成员的组
  显示「提交共享申请」;申请状态(待审批/已驳回)在弹窗可见;组详情(组管理员
  视角)加「待审批申请」区。
- group_admins 发放:分享弹窗加「组管理员可管理这本笔记本」勾选 → 追加
  `(group_admins, admin)` 行;撤销共享同组两行一起删(P1 已按组撤);共享清单
  把两行折叠成一条并标注管理权。
- 铃铛:组管理员的 pending 申请数进待确认中心(复用 pending_actions,读谓词
  同口径)。
- 错误文案 user_error;api_contract/新增守卫测试;UI 词表(「共享申请」「审批」
  「驳回」)。
- `decided_at` 只允许写 SQL `NULL`,绝不能写 `''`:该列是本表唯一进入 shadow
  正向复制的可空时间列,且刻意**不**登记进 `POSTGRES_EMPTY_TIME_SENTINELS`
  (T1 已定)——一旦 store 层某处手滑写了 `''` 而非 `None`/`NULL`,PostgreSQL
  侧的 `timestamptz` 列收到空串会直接类型报错,poison 整条正向复制通道(而
  不是被两套哨兵表悄悄兼容掉)。T3 两侧 store 落地时须为 `decided_at` 补一条
  覆盖「未决定→NULL」「已决定→ISO 时间戳」两态的断言(单测或写路径内联校验
  二选一),不能只靠人工审阅。
- `status` 精确匹配红线(T1 已在两份迁移注释写明,T3 消费方必须遵守):只可用
  `status == 'pending'`/`'approved'`/`'rejected'` 精确匹配已知取值,绝不能写
  `status != 'pending'` 这类否定式当「已决定」判据——shadow 停车会给冲突行的
  `status` 暂写哨兵串,否定式判据会把停车行误判成任意一种正常状态;所有读
  路径(pending 清单、审批/驳回前置校验、铃铛计数)统一按白名单精确匹配。
- 创建端点(`POST /notebooks/{id}/share-requests`)撞上
  `uq_share_requests_one_pending` 时**不是**普通 409:必须捕获该唯一索引冲突
  并返回已存在的那条 `pending` 申请(幂等终态),而不是把「同一笔记本再次
  申请同一个组」当成失败——申请者刷新页面重复提交是常见操作,不应该弹错误。

### T4 文档 + 门禁 + PR

版本数字(50/28、78 表、105 surface、闭包实证值)、product-and-api 群组章扩
P2 段(能力矩阵翻转、审批流端点、数值上限:申请无分页口径)、AGENTS/CLAUDE
授权条目更新(写权不再恒 owner-only——**这句是 P0 以来反复钉的安全边界表述,
所有出现处逐一改写并说明翻转边界:admin 级仍不含删库/Agent 面**)、README 一句、
G1/G2/G3、PR + codex 闭环。

**T4 交接清单(把 T1 评审修复轮的实际产出接进文档,不得照抄本计划文档 T1
段落里写的预测值——那段数字是实现前的预测,已被评审修复轮的追加改动超越)**:
- schema 版本:SQLite v50 / PostgreSQL v28(不变)。
- 探针实证的最终值:78 表 / **106** unique surface(评审修复轮新增
  `uq_share_requests_one_pending` 之后,从 T1 落地时的 105 升到 106)/ FK 闭包
  12(不变,`uq_share_requests_one_pending` 停车走 SENTINEL_TEXT/`status`,
  不影响闭包深度)。
- `notebook_share_requests` 的两处评审修复必须体现在 product-and-api 与
  CLAUDE.md/AGENTS.md 的对应条目里:①申请方向的轴——请求者是笔记本 manage
  权(owner/admin)持有者、对目标组只是普通成员(不是反过来);组管理员分享
  进自己管理的组永远不走这张表。②`uq_share_requests_one_pending` 防重复
  pending 与 `status` 精确匹配红线(措辞平移 v49 `principal_type` 的同款
  红线句式)。
- `decided_at` 只写 SQL NULL 的纪律、以及创建端点撞唯一索引返回既有 pending
  (幂等)这条产品行为,一并写进 product-and-api 群组章节 P2 段。
- `schema_manifest.POSTGRES_EMPTY_TIME_SENTINELS` 与 shadow
  `transform._EMPTY_TIME_SENTINELS` 分叉的遗留登记(见下「遗留登记」一节)
  转成独立跟踪项,不在本特性 PR 内解决,T4 只需确认它已被记录、不必修。

## 裁决(沿用 + 新增)

- P1 裁决全部有效(四值白名单、未共享门、公开页 fail-closed、创建者隔离等)。
- **P2-1**:admin 级 = owner ∨ role='admin' 的有效授权边;`notebook:delete`
  与 Agent/MCP 面不翻转。
- **P2-2**:审批流状态机收窄为 pending→approved/rejected 单向(**撤回不是
  第三个状态**——申请者撤回走 `DELETE` 整行,不写 `withdrawn`;`decided_by`/
  `decided_at` 语义因此保持纯粹的「组管理员做出的决定」,撤回是申请者自己
  的动作、不是决定,不写这两列;已审批/已驳回的申请不可再撤回——`DELETE`
  只在 `status='pending'` 时允许,由 T3 端点校验把关)。申请不授予任何权限;
  审批写边与状态更新同一写事务。
- **P2-3**:`NotebookSummary.can_manage_content` 新布尔承载 UI 写权感知,
  access 枚举仍不扩。
- **P2-4**:组管理员的内容操作沿用既有审计惯例(identity id 与 actor label
  拆分);配额仍记 owner。
- **P2-5**(T1 评审修复轮新增):`notebook_share_requests` 加防重复 pending
  的部分唯一索引 `uq_share_requests_one_pending`
  (`(notebook_id, group_id, status) WHERE status = 'pending'`,三列版——
  第三列 `status` 买 shadow 停车的 SENTINEL_TEXT 候选,不把「本表永不能有
  入向 FK」变成硬约束)。`status` 消费方只许精确匹配已知取值,绝不能用
  `!=` 当「已决定」判据(见上「已定决策 P2-2」与 T3 简报的同款红线)。

## 遗留登记(不阻塞本特性,独立跟踪)

- `schema_manifest.POSTGRES_EMPTY_TIME_SENTINELS`
  (`backend/app/repositories/postgres/schema_manifest.py`)与 shadow
  `transform._EMPTY_TIME_SENTINELS`
  (`backend/app/migration/shadow/transform.py`)两份哨兵列表已经分叉——这
  是**基线既有**的分歧,不是本特性引入的:`catalog_jobs.finished_at` 只登记
  在前者,后者没有它。P2-T1 复核 `notebook_share_requests.decided_at` 该不该
  进这两份清单时顺带发现这个分叉,但两份清单各自服务不同的读写路径(前者是
  双后端行映射的语义修复表,后者是 shadow 正向复制的值转换表),统一它们是
  **独立一件事**,不在本特性范围内——登记于此待后续任务处理,不得因为发现
  了就在本特性 PR 里顺手改。
