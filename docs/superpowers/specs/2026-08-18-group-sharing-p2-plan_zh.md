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

**T4 交接清单(T2 评审 P0 追加——`notebook:configure` 拆格)**:
- **能力表现在有三档 owner-格**:`notebook:delete`、`reports:write`、以及 T2 评审
  新增的 `notebook:configure`(挂载配置 + 链接分享,**恒 owner**)。product-and-api
  的能力矩阵与 AGENTS/CLAUDE 授权条目都要说清这条边界:内容管理权(六格 admin)
  翻给了组管理员,但**挂载配置与 share_token 链接分享不翻**,它们是 owner 对本库
  检索范围与对外处置的配置,不随内容管理权转移。理由:mount_sql 的「同 owner 候选」
  按被挂库 owner 解析,组管理员若能改挂载就能枚举/挂载库主全部私有库并经代理端点
  读全文;链接分享能替库主铸对外链接让组外人整本 copy。
- **`notebook:manage`(翻 admin)的爆炸半径逐条**——它现在 = 改名(PATCH)+ 授权边
  管理(GET/POST/DELETE /grants)。**不含**任何链接分享:尤其 `DELETE /share`
  (撤链接分享**连带踢掉全部只读成员**,`clear_share` 清 `notebook_members`)刻意
  留在 `notebook:configure`,因为它的爆炸半径超出内容管理。product-and-api 要逐条
  列出 manage 覆盖哪些端点、configure 覆盖哪些端点(bases/mountable/share/
  mounted-by-count),别让读者以为「组管理员能管共享」= 能动链接。
- **「写权恒 owner-only」的文档失效句必须逐处改写**(T1 计划就点了名,这里给出
  待改的锚点供 T4 定位):CLAUDE.md 的 access_sql 红线段(约 :294)与 mount_sql 相关
  段(约 :39)、AGENTS.md 的授权 baseline 段(约 :235 / :208)——这些「写权恒
  owner-only」的表述在 T2 之后只对 `NOTEBOOK_WRITE_SQL`(delete/Agent 面)成立,
  内容管理已是 owner∪admin 边;改写时同时说清「configure 那三格仍恒 owner」。

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

- **P2-6**(T3 codex 第 4 轮评审,**裁决变更**):批准一条共享申请时,必须在同一写事务内
  复核**申请人**此刻仍对那本笔记本拥有管理权(谓词直接取 `access_sql.NOTEBOOK_ADMIN_SQL`),
  不成立 → 409 + 可读原因,**申请行保留**(审计价值大于清理,组管理员可自行驳回);
  `reject` 不做这条复核(驳回是终止、不产生授权)。

  这条**推翻**了第 2 轮评审登记的「approve 不复检申请人当前 manage 权是刻意设计(异步
  审批语义)」。推翻理由:本仓库最反复钉的原则是**授权在生效时刻实时判定、绝不缓存**
  ——挂载边不是授权凭证、撤销即时生效;P1-T3b 也正是按它裁的公开报告页(创建时合法 ≠
  持续有效,创建者失去读权链接即 404)。审批把一次陈旧检查兑现成一条**活的**授权边,与
  该原则正相反。具体形态:Bob 经 `group_admins` 边对库 N 有管理权 → 提交「共享 N 给 G1」
  → 库主撤销 Bob 的管理边 → G1 的组管理员批准 → N 的读权发给整个 G1。批准这一刻**没有
  任何一方**在验「申请人现在还有没有权把它交出去」:组管理员验的是「我的组要不要这个
  库」,库主根本不在回路里。

- **P2-7**(T3 codex 第 6 轮评审):**撤回按「申请归属」授权,不按当前笔记本权限**。
  `DELETE /notebooks/{id}/share-requests/{rid}` 只要求登录,授权判据是 store 的三列谓词
  (`notebook_id` + `request_id` + `requested_by`);非本人与不存在同为 404(无存在性
  泄露),跨库拼 URL 因 `notebook_id` 仍在 WHERE 里同样 404。

  它与 P2-6 互补:P2-6 让批准**拒绝**失权申请人的申请(防止陈旧授权被兑现),所以撤回
  绝不能也要求管理权——否则这类申请**既批不了也撤不掉**,永远卡在组管理员队列里。也不能
  改挂 `require_notebook_read`:读权同样可能随那条授权边一起消失。一句话:一个防止陈旧
  授权生效,一个保证申请人**始终**能收回自己的提议。

- **P2-8**(T3 codex 第 6 轮评审,**通用规则**):**能力守卫的 TOCTOU 窗口不是每个写端点
  都要堵**,判据是「这次写入产生的是什么」——内容写入(来源/knowhow/图谱构建/治理)在窗口
  内落库只是普通竞态,那些内容本就在库主掌控下,他撤权后照样能删改重建;但**创建持久授权
  状态**的写入会把访问权授予**他人**、且效力**超出发起人自身权限的存续**。规则:**凡是写
  `notebook_grants`(或未来任何授予他人访问权的行)的路径,必须在同一写事务内复检并锁住
  发起人的笔记本侧权限**,其余写端点不加。规则正文写在 `backend/app/api/deps.py` 的能力表
  注释里(新增端点时最先读到的地方);落地形态是
  `repositories/*/group_store.py::_require_notebook_manage_on`(两段式:owner 半普通查 +
  `ADMIN_GRANT_PROBE_FOR_SHARE_SQL` 锁授权边行)。当前两个消费点:`create_grant`(发起人)
  与 `approve_share_request`(申请人)。

  ⚠ **这段落地形态描述已被 P2-11 取代,规则本身不变**:R6 定下规则时只锁了授权边行,
  而让那条边生效的成员资格行没锁——链只堵了一端。R8 把它换成生效链两环一起锁,
  `ADMIN_GRANT_PROBE_FOR_SHARE_SQL` 这个常量已删除(并有反向护栏钉住它不许回来)。
  读到这里请直接看 P2-11。

- **P2-9**(T4 codex 第 7 轮评审):**外键父行的存在性不是 P2-8 说的那种权限复检**,两者
  是正交的两件事,别指望前一条规则顺带覆盖它。P2-8 问「发起人现在还有没有权」,这一条问
  「被引用的那一行还在不在」——`notebooks.created_by` 不可变(产品没有转让 owner 的功能)
  保证了 owner 身份不会在事务执行期间被撤销,但**保证不了那一行还存在**。窗口是能力守卫
  通过之后、INSERT 之前的一次并发删库;后果是外键违例冒泡成 **500**,而正确答案是 404。

  规则:**凡是 INSERT 一行带外键的记录的写路径,它的每一个父行都要在同一写事务内复核**
  (不只是复核其中一个)。`create_share_request` 此前只复核了 `groups`、漏了 `notebooks`,
  正是这种「补了一半」的形态——两个父行的理由逐字相同,漏掉的那半没有任何独立论证。

  两个后端的形态刻意不同,与 `delete_group` / `approve_share_request` 的既有分叉一致:
  SQLite 的 `database.write()` 是进程级写锁,复核与插入之间插不进第三个事务,所以**只查
  存在性、不加锁**;PG 必须显式锁行,用 **`FOR KEY SHARE`** 而不是 `FOR SHARE`——前者
  恰好就是 PostgreSQL 自己在随后那次 INSERT 上为外键检查取的锁,所以显式加锁**不新增
  任何冲突边**(这个顺序今天就已经在走),而 `FOR SHARE` 会额外与 `FOR NO KEY UPDATE`
  冲突,即与任何一次普通 `UPDATE notebooks SET …`(改名、改状态、推 `updated_at`)互相
  阻塞,凭空造出一整类新的死锁面;防住并发删库只需与 `FOR UPDATE` 冲突,`FOR KEY SHARE`
  已经做到。

  **锁序 `groups → notebooks`**,论证是「`groups` 是根锁」:全仓只有
  `postgres/group_store.py` 会锁 `groups` 行,而它的每个调用点都把 `_lock_group_on` 写成
  写事务的第一条语句,因此没有任何事务会持着别的锁去等 `groups`,成环的必要条件不成立。
  反方向不存在:删库持 `notebooks` 行锁后级联删的是**子**行,不需要 `groups` 锁;
  `memory_store` 的三段式是 `notebooks → notebook_members/notebook_grants`,同样不碰
  `groups`。新增会同时锁这两类行的路径时,必须回来复核这个论证仍然成立。

  **收口范围(同轮次补记):`create_grant` 也在这条规则里。** 它同样 INSERT 一行引用
  `notebooks` 的 `notebook_grants`,而 `_require_notebook_manage_on` 只有**非 owner** 那半
  自带锁(`FOR SHARE OF ng` 锁住授权边行,删库要 CASCADE 掉它就得先拿同一把锁);**owner
  半是一条无锁 SELECT 且当场短路**,库主自己发边时那条 SELECT 与 INSERT 之间可以插进一次
  已提交的删库 → 同款 500。复核**不按分支收窄**(existence 是先决条件,谁发起都一样),
  位置放在 `_require_notebook_manage_on` **之前**、`_lock_group_on` 之后:笔记本维度内部
  「存在性在前、权限在后」,而群组维度的错误优先级逐字不变。顺带把非 owner 分支原有的
  `notebook_grants → notebooks` 反向获取抹平(它与删库级联的 `notebooks → notebook_grants`
  互为逆序,是一个窄死锁面)。

  ⚠ **SQLite 侧补这一条的动机与 PG 不同,别读成同一件事**(已实测):这一侧**本来就不会
  500**——库被删时 `_require_notebook_manage_on` 的 owner 半查不到行、授权边半的行也被外键
  级联带走,于是抛 `NotebookManageRequiredError`,根本走不到 INSERT。补它是为了**两个后端
  答同一句话**:PG 修好之后答 404「笔记本不存在」,SQLite 不补就答 403「你已不再拥有这本
  笔记本的管理权」——后者在库已经不存在时纯属误导。所以「双后端同修」这里的理由是响应对等,
  不是各自都在修同一个 500。

  ⚠ **`_require_notebook_manage_on` 里「owner 半刻意不加锁」那段论证仍然成立,不要删**:
  它论的是「**身份**不会变」(产品没有转让 owner 的功能),而本条问的是「那**一行**还在
  吗」——两者正交,`DELETE FROM notebooks` 与「谁是 owner」毫无关系。两条并列写在那个
  docstring 里,连同「将来真加了转让功能就得回来补 `FOR SHARE`」的提醒一起保留。

  **尚未收口(独立跟踪)**:`approve_share_request` 不在本条范围内——它靠对申请行的
  `FOR UPDATE` 挡住了删库(CASCADE 要拿同一把锁),**没有** 500 风险;但它的获取顺序仍是
  `share_request → notebook_grants → notebooks`,与删库级联的 `notebooks → 子行` 互为逆序,
  理论上构成一个窄死锁面(PG 会检测并中止其中一方)。它是**另一种**失败形态(死锁而非外键
  违例、且不产生坏数据),需要自己的分析与用例,故未随本轮改动。

- **P2-10**(T4 codex 第 7 轮评审):**撤销折叠的群组共享时,带管理权的那条边必须最后删。**
  共享给群组的标准模板是两条边(`(group_admins:G, admin)` + `(group:G, viewer)`),界面把
  它们折成一项、撤销时逐条 DELETE。而三个 grant 端点的守卫是 `notebook:manage`(admin 级),
  所以**组管理员**(非 owner)也走这条路——按 `grantIds` 的自然顺序删,第一次 DELETE 就
  删掉了他自己的管理权,第二次 404,viewer 边留着:界面报「已撤销」,群组其实还读得到。
  发放顺序正是 admin 在前,所以这是**默认路径**而非边角情形;owner 因为 owner 臂恒成立
  不受影响,也正因如此这个洞只在 P2 新开的非 owner 路径上发作。排序判据必须与
  `foldGroupShares` 的 `manage` 判据**共用同一个** `confersManage`(只看 `role`,对
  `group` 与 `group_admins` 一视同仁,见 P1/R1/R4 那串论证)——分成两份写法迟早会出现
  「标着可管理、却没被排到最后」的边。

- **P2-11**(T4 codex 第 8 轮评审):**授权的「生效链」有两环,写事务里认它就要两环都锁。**
  管理权来自 `group` / `group_admins` 边时,链是①那条 `notebook_grants` 边 + ②让它生效的
  那行 `group_members`。P2-8/R5 只锁了①(成员行藏在 `EXISTS (...)` 里,`FOR SHARE` 够不着),
  于是并发的移出组/降级可以提交在探测快照之后、持久边落库之前——一个管理权**刚刚被撤销**
  的人仍然发出了新的访问权。**这不是新规则,是 P2-8 在这条链上的完整兑现。**

  解法与当年把授权边提到顶层是同一招:**内连接**让成员行也进顶层 rangetable,
  `FOR SHARE OF ng, ngm` 在**同一条语句**里锁住两环。刻意不用「先探测拿主体、再单独锁
  成员行」两步——两步之间那行可以被删,而此时是否还有**别的**链成立又要重新判断;单条
  语句没有这个中间态。代价是判定拆成两条语句(`user` 臂的链只有一环,而带锁的 `UNION`
  在 PG 里是语法错误),因此**两条合起来必须与唯一定义点 `ADMIN_GRANT_PROBE_SQL` 逐格
  等价**,由 `tests/postgres/test_admin_grant_chain_lock.py` 的数据驱动矩阵钉住(那份矩阵
  每格还断言期望值,免得两种写法一起坏掉时空转)。

  **锁序**:本条**不**引入新的受锁资源顺序——获取序是 `notebook_grants → group_members`,
  与删组(`groups → 清 notebook_grants → 级联 group_members`)、移出组
  (`groups → group_members`)都同向或无交集,没有反向环。刻意**没有**选「对赋予管理权的
  那个组取 `_lock_group_on`」那条路:`create_grant` 已经锁了**目标组**,而赋权组可能是
  另一行,同表多行加锁需要确定性顺序,而赋权组的身份要探测之后才知道——排不出来。

  ⚠ **与 `access_sql.py` 里「组成员资格刻意不锁」那条已登记取舍不矛盾,别互相引用**:
  那条只覆盖**读级**探测(消费者是 `memory_store` 热路径,残留物是一行谁也读不到的私有
  Memory);本条是**管理级**,下游落的是把整组读权发出去的持久授权边。判据是爆炸半径。

  R5 那条只锁边行的 `ADMIN_GRANT_PROBE_FOR_SHARE_SQL` 已**删除**(留着就是给「只堵一端」
  留一个看起来正规的入口),守卫里有一条 `not hasattr(...)` 钉住它不许回来。
  **SQLite 侧不改**(已实测:并发移出组无法在 `create_grant` 的写事务内完成,进程写锁把
  两者串起来了)。

- **P2-12**(T4 codex 第 8 轮评审):**共享申请只对目标组的「普通成员」开放。** 判据从
  「有没有成员行」收窄成 `role == 'member'` 的**正向精确匹配**——组管理员分享进自己管理
  的组永远走 `POST /notebooks/{id}/grants`、不经这张表(§4 决策 9,v49/v50 迁移 docstring
  与前端 `requestableGroups` 早就是这个口径)。放宽的后果是组管理员能建出一条 pending
  申请再**自己批自己**。这是**实现没兑现已写明的契约**,不是新增约束。

  两种不合格给**不同**的异常与响应,不能合并:非成员 → `GroupMembershipRequiredError`
  → 404(群组维度的「看不见」);组管理员 → `GroupAdminShouldShareDirectlyError` → **403 +
  可操作说明**(他是管理员,组的存在性对他不是秘密,404 只会让他去查一个没问题的组)。
  正向匹配让未知取值(正向 shadow 停车写进 `role` 的哨兵串)落进后者、fail closed;写成
  `!= 'admin'` 就反了。承重判定在**store 的写事务内**(普通成员可以在路由前置检查之后被
  提升为组管理员);路由那半与紧邻的 `role is None` 同形,是友好前置检查而非守卫——摘掉它
  不改变任何可观测响应(变异实测),留着只为不给一个必然失败的请求开写事务。

- **P2-13**(T4 codex 第 9 轮评审):**已存在的共享必须能增减管理权,而不是只能整项撤销。**
  `shareableGroups` 把已共享的组从选择器里排除,于是新建路径那个「组管理员可管理」勾选对
  已有共享**够不着**。受影响的不只是存量数据:`approve_share_request` 写的就是
  `(group, viewer)` **单边**,所以 P2 的招牌流程(成员申请 → 组管理员批准)走完之后,那条
  共享**永远**停在「没有管理权」且无路可改。后端两个端点都是现成的,缺的纯粹是 UI——正是
  「全栈对等」红线说的那件事。

  两个入口都挂在已有条目上:补发走 `grantGroupAdminsManage`(与新建路径**同一个**函数发
  同一条边,请求体只写一处);取消**只删授予管理权的那几条边**、读权边原样保留。选哪条边
  删的判据必须**共用** `confersManage`(R7 P2 立的规矩)——⚠ 这条**行为测试拦不住**:手抄
  一份 `role === "admin"` 行为逐位相同、全绿(已实测),所以另立语义守卫
  `frontend/tests/guards/group-manage-predicate-guard.test.mjs`(走 `semantic-source` 的 AST,
  不读裸文本——`static-source-policy` 明令禁止直接读生产源码)。

  **一条孤零零的 `(group, admin)` 边不给「取消管理」入口**:它既是读权又是管理权,删了这个
  组什么都看不到了,那不叫取消管理权、那叫撤销共享。界面创建的共享永远是标准两条边,批准
  写的也是 `(group, viewer)`,所以这个形态只可能来自 API 直调或历史数据。

  **自伤场景**(组管理员对赋予他自己管理权的那个组点「取消管理权」)选**优雅降级 + 条件式
  提醒**,不做二次确认:本组件只拿得到 `notebookId`,分不出「我的权限来自这个群组」与「我
  本来就是库主」(库主取消后毫发无损),任何**阻塞式**确认都会在库主这条主路径上误报。所以
  按钮上挂条件式 `title`(「若……则……」,永远为真),动作成功后若重取被拒就清空清单并给一句
  中性说明——它是**结果**不是故障,不走红色错误条。

- **P2-14**(T4 codex 第 9 轮评审):`docs/product-and-api*.md` 的群组端点能力表与实现对账
  (方法:从路由源码 AST 提取每个端点**实际**的 `require_notebook_capability(...)` 声明与体内
  守卫,再与表逐行比,而不是肉眼扫)。21 行里 3 行错,均已修正并中英对仗:撤回申请(表写
  `notebook:manage`,实际**刻意无能力依赖**、授权轴是申请归属——见 P2-7,写错会让客户端把
  唯一的清理入口藏起来)、`GET /notebooks/{id}/share`(表写 `notebook:manage`,实际
  `notebook:configure` 恒 owner——**安全相关的错误指引**,照表实现会把链接分享控件暴露给组
  管理员)、提交共享申请(表写「目标组成员」,P2-12 之后是「目标组**普通成员**」)。

- **P2-15**(T4 codex 第 10 轮评审):**「`notebook:manage` = 改名」是简写被当成了契约——
  修文档,不拆端点。** codex 指出的分叉是**真的**:能力翻转让 `PATCH /notebooks/{id}` 对组
  管理员开放,而 `NotebookUpdate` 收的是八个字段(`name` + `purpose` / `primary_domain` /
  `target_users` / `expected_questions` / `source_types` / `taxonomy` / `access_scope`),仓库
  契约却写成「改名 + 授权边管理」。两个修法方向:①拆端点或逐字段校验,让非 owner 只能改名;
  ②把契约写准。**选 ②**。

  裁决理由:这些字段是**内容邻接**的(描述这个库讲什么),正落在内容管理权里;一个已经能
  增删/重解析全部来源、触发图谱重建、写 knowhow 与知识治理的组管理员,却不许改「这个库是
  干什么的」这行描述,不自洽。为纯描述性元数据增加一道真实的接缝不划算。

  **两条实证(独立复核过,其中一条修正了交接时的说法)**:
  - ✅ **`access_scope` 不参与任何授权判定**——名字像权限、行为不是。全仓只有两个消费者:
    `notebook_store` 的 update 写它、`notebook_catalog` 读回投影;授权全部在 `access_sql.py`
    的三条谓词(外加 `mount_sql.py`),引用的是 `created_by`/`tier`/`notebook_members`/
    `notebook_grants`。**不存在提权。**
  - ⚠ **「七个字段全仓只有 `notebook_templates.py` 用到」不准确**,实际还有两个消费者(都
    **不是** prompt、也**不是**授权):`primary_domain` 是库内搜索框(`GET /notebooks/{id}/search`)
    的可匹配字段;`purpose` 会经 MCP `list_notebooks` / `select_notebook` 提供给外部 Agent
    (截断 500 字符)。两者都是内容邻接的读,不改变裁决——但文档必须写这条**准确**的性质,
    不能写「它们哪儿也不去」:本轮要修的毛病正是「宽泛的简写被当成契约」,反方向再犯一次
    就没意思了。(`prompts.py` 里的 `purpose` 参数是**同名局部量**,取值是 `"deep report"` /
    `"step-by-step evidence-grounded answer"` 这类字面量,与笔记本字段无关;`catalog_job` /
    `collection_enumeration` / `knowhow/api` / `memory_service` 的命中全是散文里的
    "on purpose" 或另一个局部变量。)

  承重的不是文档而是**反向护栏** `backend/tests/test_notebook_update_authorization_free.py`
  (三条):①冻结字段集合,失败信息**告诉后来者该想什么**(会不会进谓词/是不是生命周期
  状态/会不会喂进 prompt),而不是只报「集合变了」;②把字段名与授权谓词的**实际 SQL** 比对
  ——冻结集合通知不了「有人把某个既有字段引进了谓词」,那种改动在另一个文件里发生、字段集合
  一个字不变;③`extra="forbid"` 仍然挡得住 `status`/`tier`/`created_by`/`is_shared`。判据
  用 Pydantic 运行时自省而不是解析源码文本(模型自己就是真源)。

  ⚠ 那句简写在**五处**(两份 product-and-api、`AGENTS.md`、`CLAUDE.md`、`deps.py` 注释)
  逐字重复,已一并改准——只改被点名的两处,另外三处会继续把同一个误解教给下一个人。

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
