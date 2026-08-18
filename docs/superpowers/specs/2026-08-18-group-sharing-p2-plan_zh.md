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

### T4 文档 + 门禁 + PR

版本数字(50/28、78 表、105 surface、闭包实证值)、product-and-api 群组章扩
P2 段(能力矩阵翻转、审批流端点、数值上限:申请无分页口径)、AGENTS/CLAUDE
授权条目更新(写权不再恒 owner-only——**这句是 P0 以来反复钉的安全边界表述,
所有出现处逐一改写并说明翻转边界:admin 级仍不含删库/Agent 面**)、README 一句、
G1/G2/G3、PR + codex 闭环。

## 裁决(沿用 + 新增)

- P1 裁决全部有效(四值白名单、未共享门、公开页 fail-closed、创建者隔离等)。
- **P2-1**:admin 级 = owner ∨ role='admin' 的有效授权边;`notebook:delete`
  与 Agent/MCP 面不翻转。
- **P2-2**:审批流状态机只有 pending→approved/rejected/withdrawn 单向;
  申请不授予任何权限;审批写边与状态更新同一写事务。
- **P2-3**:`NotebookSummary.can_manage_content` 新布尔承载 UI 写权感知,
  access 枚举仍不扩。
- **P2-4**:组管理员的内容操作沿用既有审计惯例(identity id 与 actor label
  拆分);配额仍记 owner。
