# 群组知识共享设计(v3,讨论定稿)

> 状态:v3。两轮讨论(2026-08-17)的决策已全部吸收(§11);仅剩实现期核对项(§12)。

## 1. 背景与目标

三个真实场景:

1. **项目**:一群人属于一个项目,项目共享一个知识库(notebook)。项目成员都能看到
   知识库的来源、在库内提问、写自己的深度报告;项目管理员管理来源(新增与删除);
   成员能建自己的 notebook 并**挂载**这个项目知识库做检索。
2. **部门**:同一部门的人共享若干 notebook。部门管理员管理「哪些库共享给部门」,
   也管这些库的构建(来源、图谱)。
3. **领域**:范围更大的领域知识库。领域管理员给用户账号配上领域,再单向指定
   notebook 在领域内可见;无审批流(指可见范围本身;成员**贡献**库另有审批,见
   §4)。

目标:**一套模型兼容三个场景**——三者的差异只落在配置上,不落在机制上。

## 2. 现状盘点(地基)

现有共享原语恰好是三块,全部保留并作为兼容基线:

| 原语 | 表/字段 | 语义 |
| --- | --- | --- |
| 单 owner | `notebooks.created_by` | 写权 owner-only(`user_can_access_notebook`);读权 owner∪成员 |
| 只读成员 | `notebook_members(role='reader')` | share_token 加入;读=owner∪成员,写仍 owner-only |
| 公共知识库 | `notebooks.tier='base'` | 仅系统管理员发布;任何人可挂载;常规列表隐藏;经代理端点只读 |
| 挂载 | `notebook_bases` + `mount_sql.py` | 检索层组合;有效性**实时判定**(「挂载边不是授权凭证」),可挂=公共库∨同 owner |

关键既有原则(本设计全部继承):

- 挂载有效性是解析时实时判定,授权消失边自动失效、恢复即自动生效;
- 参与集解析唯一定义点 `mount_sql.py`,检索与权限共用;
- 挂载库经 active-notebook 代理端点只读,不授予被挂库的直接成员权;
- Memory 按创建者私有(检索侧 SQL 级隔离);
- 挂载**不传递**:参与集 = 本库 + 直接挂载的 base,不递归(决策 8:保持)。

## 3. 核心抽象:三场景归一

统一为三个原语:

1. **群组(Group)**:一组用户 + 组内角色(`admin` / `member`)。
   `kind ∈ {project, department, domain}` 只是**分类标签**——影响 UI 文案、谁能
   建组、目录归类,**不影响权限机制**。领域也是普通群组:领域管理员「给用户账号
   配上领域」= 把用户加进 kind=domain 的组(决策 7)。
2. **授权边(Grant)**:`(notebook, principal, role)`。
   - `principal ∈ { user:U, group:G(全体成员), group_admins:G(仅组管理员), everyone }`
   - `role ∈ { viewer, admin }`(两级;`editor` 不引入,枚举保留扩展位,决策 4)
   - 所有「谁能对这个库做什么」由授权边 + owner 决定;owner(`created_by`)是
     隐含的最高授权。`everyone` 仅用于 `tier='base'` 兼容映射与可选的全员发布,
     领域发布不用它(领域走组授权)。
3. **挂载(Mount)**:机制不变,只改有效性谓词——从「公共库 ∨ 同 owner」扩展为
   「挂载方 notebook 的 owner 当前对 base 有读权(有效角色 ≥ viewer)」。
   **读权 ⇒ 可挂载**成为一致规则。

**有效角色** = max(owner, 直接 user 授权, 各群组授权[按本人组内身份取 group /
group_admins 行], everyone 授权)。实时判定,撤销即时生效。

### 三场景 = 同一个授权模板

三个场景在库上都是同样两条授权边:

```
(group_admins:G, admin)   组管理员:管来源(增/删/重解析)、触发构建、管理共享
(group:G,        viewer)  组成员:打开库、看来源/图谱、提问、写自己的深度报告、挂载
```

差异只剩两处配置:

| | 项目 | 部门 | 领域 |
| --- | --- | --- | --- |
| 谁能建组 | 人人可建 | 仅系统管理员 | 仅系统管理员 |
| 成员怎么来 | 组管理员邀请/加人 | 组管理员按部门维护 | 领域管理员给账号配域 |

**库在成员界面的呈现三场景一致**(决策 10):群组共享的库进成员笔记本列表的
「群组」分区,按 kind 标注(项目/部门/领域)。遗留 `tier='base'` 公共库维持
现状隐藏惯例(只在挂载选择器出现)。

## 4. 角色与能力矩阵

| 能力 | 成员(viewer) | 组管理员(admin 授权) | owner |
| --- | :-: | :-: | :-: |
| 打开库、看来源/图谱、提问(会话按提问者隔离),存自己的 Memory | ✓ | ✓ | ✓ |
| 在库内创建**自己的**深度报告(计入自己的用量;他人不可见,分享走既有公开链接) | ✓ | ✓ | ✓ |
| 挂载本库到自己的 notebook | ✓ | ✓ | ✓ |
| 申请把**自己的**库共享给组(待组管理员审批,见下) | ✓ | ✓(直接生效) | — |
| 添加/删除/重新解析来源,触发图谱与检索索引构建 | | ✓ | ✓ |
| 管理授权边(共享给组/发布/撤回/审批申请)、改名、图谱 Schema 覆盖 | | ✓ | ✓ |
| 删库、转让 owner | | | ✓ |

- 成员 == 现在的 reader 行为 + 「建自己的深度报告」(现状 reader 能否建报告需
  实现时核对;目标行为以本表为准)。问答会话成员间**不可见**(决策 1)。
- 系统管理员可转移 `created_by`(人员离职交接),是 owner 的唯一旁路。
- **配额**:文档数上限记 notebook owner 的个人有效上限,不随操作者变(组管理员
  往别人 owner 的库加来源,仍占该 owner 的额度);领域库的 owner 即领域管理员
  本人(决策 5)。
- **Agent/MCP 面 v1 不放开**:`sources:write` / `sources:delete` /
  `maintenance:execute` 保持 owner-only 红线(CLAUDE.md「MCP 工具面」);群组
  admin 授权只在浏览器 UI 生效。后续如放开,单独一件事过评审。

### 授权边的管理策略(决策 9)

- **创建组授权边**(`group` / `group_admins` 行)的直接路径:同时满足「对库有
  admin/owner 权」且「是目标组的组管理员」。项目/部门/领域管理员对组内库天然
  两者兼备。
- **成员贡献路径(审批流)**:普通成员把**自己的**库共享给组 = 提交共享申请,
  组管理员审批;批准即在同一事务插入 `(group:G, viewer)` 授权边,驳回即终结。
  申请不授予任何权限(pending 不进判定谓词)。
- **撤销不对称**:删除指向组 G 的授权边,「库 admin/owner」或「G 的组管理员」
  **任一方**即可(组管理员管理共享给本组的全部内容;库主随时可收回自己的库)。
- **user 授权边**(share_token 加入)沿用现状:库 owner/admin 授权持有者管理。
- **everyone 授权边**仅系统管理员(即 `set_notebook_tier` 现状口径)。

## 5. 数据模型(追加式迁移)

SQLite `_migration_49` / PG v27(示意,字段名可再议):

```sql
CREATE TABLE groups (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'project',   -- project|department|domain
  description TEXT NOT NULL DEFAULT '',
  created_by  TEXT REFERENCES users(id),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE group_members (
  group_id  TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id   TEXT NOT NULL REFERENCES users(id),
  role      TEXT NOT NULL DEFAULT 'member',      -- member|admin
  added_at  TEXT NOT NULL,
  added_by  TEXT REFERENCES users(id),
  PRIMARY KEY (group_id, user_id)
);
CREATE INDEX idx_group_members_user ON group_members(user_id);

CREATE TABLE notebook_grants (
  id             TEXT NOT NULL PRIMARY KEY,
  notebook_id    TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  principal_type TEXT NOT NULL,      -- user|group|group_admins|everyone
  principal_id   TEXT NOT NULL DEFAULT '',  -- user_id | group_id | ''(everyone)
                                     -- NOT NULL:NULL 不参与唯一比较,会让 everyone
                                     -- 逃出 UNIQUE(实现期评审发现,已定裁决)
  role           TEXT NOT NULL,      -- viewer|admin(editor 保留不启用)
  created_by     TEXT REFERENCES users(id),
  created_at     TEXT NOT NULL,
  UNIQUE (notebook_id, principal_type, principal_id)
);
-- UNIQUE 隐式索引覆盖 notebook_id 前缀,不另建 nb 单列索引
CREATE INDEX idx_notebook_grants_principal ON notebook_grants(principal_type, principal_id);

-- 成员贡献审批流。刻意独立于 notebook_grants:grants 表的每一行都是「生效中的
-- 授权」,判定谓词零 status 过滤(deny by default——不存在「忘了滤 pending」这类
-- 漏洞形态)。批准 = 同一事务写 grants + 更新本表状态。
CREATE TABLE notebook_share_requests (
  id           TEXT PRIMARY KEY,
  notebook_id  TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  group_id     TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  requested_by TEXT NOT NULL REFERENCES users(id),
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
  decided_by   TEXT REFERENCES users(id),
  decided_at   TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_share_requests_group ON notebook_share_requests(group_id, status);
```

Principal 编码取舍:「成员 viewer + 管理员 admin」用**两行**表达(`group` +
`group_admins`),而不是单行双角色列——grant 行语义保持单一,UI 把同组两行渲染成
一个「共享给群组 G」条目。

### 兼容映射(不迁旧表)

- `notebook_members(role='reader')` ≙ `grants(user:U, viewer)`。v1 判定谓词取
  **旧表 ∪ 新表**并集,share_token 加入流程改写新表;旧表只读保留,零迁移风险。
- `tier='base'` ≙ `grants(everyone, viewer)`。`set_notebook_tier` 保留为兼容
  入口、同事务双写;`MOUNT_VALID_EXPR` 过渡期 `tier='base' ∨ grant` 并集。
  领域发布 UI 直接操作组授权边,不再依赖 tier。

## 6. 判定唯一定义点

新增 `access_sql.py`(镜像 `mount_sql.py` 的模式;SQLite/PG 各一份、语义对等,
双后端同修):

- 产出「有效角色」谓词片段,全部消费点 import,不许手写副本:
  1. `user_can_read_notebook` → 有效角色 ≥ viewer;
  2. 写守卫从单一 `user_can_access_notebook` 拆成**能力守卫**
     (`sources:write` ≥ admin 授权、`grants:manage` ≥ admin 授权、
     `notebook:delete` = owner …),API 层按端点声明所需能力;
  3. `MOUNT_VALID_EXPR` 扩展(base 对挂载方 owner 有效角色 ≥ viewer);
  4. `mountable_notebooks` 目录(发现入口);
  5. `list_notebooks` 分区投影(「我的」「与我共享」「群组」)。

性能:单次判定 = 若干带索引 EXISTS(grants 按 notebook_id 点查、group_members
按 PK 点查),与现状同量级;热路径不新增全表扫描。规模按单组**几百人**设计
(决策 11):不需要判定缓存,成员管理 UI 用普通分页即可。

历史决定的显式修订:`mountable_notebooks` 曾刻意排除「只读分享进来的库」,理由
是「撤销分享后边仍在会成为越权通道」。本设计下该顾虑由实时有效性谓词吸收
(撤销 → 谓词不满足 → 边失效),因此**读权 ⇒ 可挂载**成为一致规则——这正是
「项目成员挂载项目知识库」的需求本体。此条要在实现 PR 里显式登记为行为变更。

### 6.1 借入挂载与「未共享门」(P1-T2 质量评审补入)

上一段只覆盖了历史顾虑的**一半**。P1-T2 的质量评审真机复现出另一半:

    Carol 只读分享 Y 给 Alice → Alice 把 Y 挂进自己的 X → Alice 把 X 分享给 Bob
    → Bob 经 X 的代理读取与联邦检索读到 Y 的全文,而 Carol 从未授权 Bob。

这是**转手再分享**,不是撤销。实时判定治不了它:全程没有任何授权被撤销,是挂载方
**新增**一次共享就凭空多出一批读者。历史上排除只读分享的真实动机就是这条通道。

**收窄规则**(`MOUNT_VALID_EXPR` 第 4 支):受限读权(只读成员 ∨
`user`/`group`/`group_admins` 授权边)的借入挂载,**仅在挂载方笔记本自身没有任何
`notebook_members` 行、也没有任何 `notebook_grants` 行时**有效。挂载方一旦被共享,
借入边即刻失效(边保留置灰,与既有失效边惯例一致);取消共享后自动恢复。

三支不受此限,各有理由:

- `tier='base'`(公共知识库)与 **`everyone` 授权**:受众本来就是全员,转手不增加
  任何暴露面。这也是 `access_sql` 要把授权边拆成 `restricted_grant_access_expr` 与
  `everyone_grant_expr` 两个片段的唯一理由——**读权谓词本身不区分这两类**。
- 同 owner 支:挂载方 owner 共享 X 就是在处置自己的内容。

谓词侧只认「有没有被共享」这个事实,不比对两边的受众:即使受益人恰好也在被挂库的
受众里,也一律关闭借入边。受众比对要跨库展开成员/组/授权边三张表,而这是每次参与集
解析都要跑的热路径;宁可保守,恢复手段(取消共享)在用户手上。

产品含义与「挂载不传递」同向:**借来的东西不转借**。

## 7. 问答会话分享(规划,P4 独立排期)

决策 1:会话成员间不可见维持不变;报告已有公开链接分享;会话分享按以下形态规划。

**语义(决策 12)**:分享的是**到分享时刻为止的整段会话**——发放 token 时冻结
截止点(当时最后一条已完成回答的 id);此后新增的问答**不**出现在既有链接里。
再次点分享 = 同一 token、截止点前移到新的当前(报告分享「重发同一 token 不失效
已发链接」的既有语义顺延:链接不变,内容范围由 owner 显式更新)。允许包含挂载
参考库的证据(与报告公开分享对齐)。

**架构:逐字复用报告公开分享的红线**:

- 独立匿名 router(主 router 带 router 级登录依赖,挂上去会 401);
- token 发放幂等、撤销后与从未存在同为 404;生成中的 run 不落在截止点内;
- 投影是**白名单**:问题/回答正文/时间 + 每条引用的标题/原始文件名/位置/摘录;
  `source_id`/`element_id`/`notebook_id`/conversation 内部 id 一律不出
  (公开页打不开原文,给 id 只是让人拿去探测已认证接口);
- 渲染复用 `remarkCitations`(含 `remarkGfmPlugin` 单波浪线口径)、
  `.answer-table-wrap`/`.answer-code`、自带 `katex/dist/katex.min.css`——公开页
  不经 `app/page.tsx`,KaTeX 样式守卫同款要求;
- 守卫:katex-stylesheet-guard 扩展覆盖 + 会话公开页组件测试。

## 8. 明确不变的部分

检索管线、参与集解析入口、代理读取合同(含 `no-store`、404 不泄露存在性)、
来源范围勾选两维、Memory 按创建者私有、knowhow 变更历史与成员写例外、报告公开
分享、agent token 白名单 —— 全部不动。群组只替换「授权判定」这一层的输入。

## 9. 备选方案与取舍(已定)

- **owner + grants 叠加层**(取):notebooks 保持单 owner;配额、用量统计、
  agent token、深拷贝、Memory 归属全部锚定 `created_by`,叠加授权层爆炸半径
  最小;owner 离职走管理员转让。
- group-owned notebooks(不取):概念更纯但要重写所有锚定 owner 的不变量;
  若未来需要,可在叠加层之上加「名义归属群组」的展示层先行。
- editor 三级角色(不取,决策 4):两级(成员/管理员)覆盖全部三场景;
  grants.role 枚举保留扩展位。
- 部门层级(不取,决策 6):v1 扁平;`groups` 无 parent 字段,将来要做层级再
  追加迁移。
- 领域可见范围审批流(不取,决策 7):管理员单向指定;成员**贡献**库的审批流
  另行保留(决策 9,两者不同轴:前者是「谁能看」,后者是「什么进入共享集」)。
- pending 授权行(不取):审批流独立成 `notebook_share_requests` 表,grants 表
  恒为纯生效授权,判定谓词零 status 过滤。

## 10. 分阶段落地(每片全栈对等)

- **P0 授权层重构(无行为变化)**:引入 `access_sql` + 能力守卫,把
  owner/member/tier 三套判定收进唯一定义点;parity 测试钉「重构前后全部行为
  逐字一致」。
- **P1 群组 + 成员侧(viewer 授权)**:群组 CRUD/成员管理 UI(项目人人可建,
  部门/领域仅系统管理员建)、组管理员直接共享库给组、列表「群组」分区(按 kind
  标注)、挂载有效性扩展、成员在共享库内提问/建自己的报告。
- **P2 管理员侧(admin 授权边 + 审批流)**:非 owner 的来源增删/重解析/构建
  触发/共享管理;成员「申请共享给组」+ 组管理员审批;`NotebookSummary.access`
  扩枚举(`owner|admin|reader`),前端按角色显隐写按钮;项目模板(建组即建
  知识库 + 默认两条授权边)。
- **P3 领域**:账号配域 UI(用户管理页 + 领域组成员管理两个入口)、发布/撤回;
  领域库与项目/部门库同样进成员列表「群组」分区;`set_notebook_tier` 兼容双写。
- **P4(规划)问答会话分享**:见 §7,独立排期。

## 11. 已定决策(2026-08-17 两轮讨论)

1. 问答会话成员间**不可见**;报告分享走既有公开链接;会话分享单独规划(§7)。
2. 项目组人人可建;部门/领域仅系统管理员可建。
3. 部门管理员既管「哪些库共享给部门」,也管这些库的构建(来源、图谱)。
4. **不引入 editor**:两级角色——组管理员管来源(新增和删除),其余成员在库内
   提问、看来源、写自己的深度报告。
5. 配额记个人 owner;领域库 owner 即领域管理员。
6. 部门 v1 扁平,无层级。
7. 领域无审批流:给用户账号配上领域后,单向指定 notebook 领域内可见。
8. 挂载保持不传递。
9. 组授权边管理:「库 admin/owner 且组管理员」直接生效;普通成员共享自己的库
   给组走**申请→组管理员审批**。
10. 领域库出现在域内成员的笔记本列表里(与项目/部门一致)。
11. 规模:单组最多几百人;不做判定缓存。
12. 会话分享 = 到分享时刻为止的之前全部会话;允许含挂载参考库的证据。

## 12. 实现期核对项(不阻塞设计)

1. 现状 reader 能否在共享库建深度报告/触发检索——实现 P1 时核对现有守卫,目标
   行为以 §4 矩阵为准。
2. 会话分享「再次分享前移截止点」是同 token 更新(§7 现案)——实现 P4 时如需
   「每次分享独立快照」再议,不影响 token/撤销骨架。
3. `NotebookSummary.access` 扩枚举对前端旧值消费方的兼容(未知值按 reader 收)。
4. 组管理员操作别人 owner 的库时的审计标注(复用「拆 identity id 与 actor
   label」惯例)。
