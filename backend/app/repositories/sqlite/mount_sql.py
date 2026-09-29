"""参考库挂载(notebook_bases)的 SQL 片段 —— 「哪些挂载边有效」的唯一定义点。

参与集解析、KG 可用性门、summary 投影、社区扩展、晋升目标这五处都要按挂载筛选。
若各自手写谓词,任何一份副本漂移都会让「能检索到」与「界面显示挂着」不一致,而且
这种不一致没有任何测试会自然抓到。故谓词只在这里定义一次,五处一律 import。

「有效」是解析时的实时判定而非挂载时的一次性校验:挂载边不是授权凭证。可挂范围
共四支:

1. 公共知识库(`tier='base'`);
2. 与挂载方同 owner 的库;
3. 被挂库上有 **`everyone` 授权边**的库;
4. 挂载方 owner 对被挂库有**受限读权**(只读成员,或 user/group/group_admins 授权
   边),**且挂载方笔记本自身尚未被共享**——见下面的「借入挂载」。

被挂库易主、公共库被降级、共享被撤销或用户被移出群组后,边保留但不生效(降级/
转让/撤销常是临时的,静默删掉用户配置无法撤销),重新满足条件即自动恢复。上面四支
里的「挂载方 owner」一律取挂载方笔记本的 `a.created_by`,不取请求用户——四支回答的
是「这条边本身还站不站得住」,与谁在提问无关。**「参与集与谁在提问无关」这条旧契约
已被 M3 作废**,见下面「挂载仅对挂载人生效」:边站得住之后,还要问「对这位查看者
生效吗」。

## 挂载仅对挂载人生效(M3,带查看者的片段)

用户裁决 M3:共享笔记本 A 的主人把自己的**私有**库 B 挂到 A 上,这条挂载只对挂载人
自己生效;A 的其他成员不能经 A 检索 B,除非他们自己本来就能读 B。旧片段按
`a.created_by` 判第 2 支(同 owner),于是 A 的每个只读成员都经参与集读到了 B——
主人把 A 分享出去,并没有把 B 分享出去。

因此另起一组**带查看者**的片段,有效边 = `MOUNT_VALID_EXPR` **且**下面两支之一:

1. `b.tier = 'base'`——公共知识库的受众本来就是全员;
2. 查看者自己对 B 有读权——`access_sql.read_access_clause` 的列引用形式(owner ∨
   只读成员 ∨ 有效授权边),不另抄一份读权。`everyone` 授权边就在它的授权边臂里,
   且那条臂不看 `user_ref`,所以不单列一支:空查看者照样命中它。

「挂载人自己一律生效」不是一条单独的支,而是由这两支加 `MOUNT_VALID_EXPR` 蕴含出来
的:边站得住时,挂载人要么是被挂库的 owner(同 owner 支),要么对它有受限读权(借入
支),要么它是公共库/`everyone`——每一种挂载人自己都读得到 B。行为矩阵里「挂载人
看到的集合逐行等于旧片段」那条断言守着这个蕴含:将来 `MOUNT_VALID_EXPR` 多出一支
挂载人自己读不到的可挂范围,它会先红。

⚠ **别把 `v.uid = a.created_by` 作为一支加回来**,哪怕它看起来只是一次廉价的短路。
查看者支现在只引用被挂库 `b` 和查看者常量,计划器把它当作 `b` 上的限制条件下推到
`b` 的扫描上,于是顺序扫描 `notebooks b` 要为每一行求值读权子查询,代价远高于按主键
逐条取挂载边,计划器稳定选索引。一旦 OR 里出现引用挂载方 `a` 的一支,整个查看者支
就变成 join 条件,不再下推,几千本笔记本的规模上计划器会翻成「hash join + 顺序扫描
notebooks」(PG 实测:5k 本时 0.2 ms → 1.2 ms)。EXPLAIN pin 在
`tests/postgres/test_mount_sql_viewer_pg.py`,加回这一支会让它变红。

挂载人 = `a.created_by` 的依据:挂载写入口 `PUT /notebooks/{id}/bases` 挂的是
`notebook:mount`,解析到 **owner 档**(`api/deps.py::_CAPABILITY_LEVELS`);应用层
没有任何改写 `notebooks.created_by` 的语句(没有笔记本转让),深拷贝产出的是新行、
挂载边按新主人重判(`notebook_sharing.py`);唯一的另一写者是同步导入对镜像行的
upsert,它照搬源环境那一行(同样不可转让)的 owner 映射结果,而镜像上的挂载配置
本身也随源环境同步、目标端不可写。`notebook_bases.created_by` 不采用:它
可空、`replace_mounts` 每次整批重插时盖上当时的编辑者、同步导入映射不到时置为导入
执行者——它记的是「谁最后写了这批配置」,不是一个可信的挂载人。

两种「查不到人」都失败即关,且都不需要单独的分支:

* **挂载人未知**:`notebooks.created_by` 在两个后端的 schema 里都可空、没有回填迁移,
  同步导入把源环境的空值原样带过来(「空即无人」)。查看者支本来就不看挂载人,于是
  只剩公共库与读权两支——边只对自己能读 B 的人生效。(今天的 `MOUNT_VALID_EXPR` 在挂载人为
  NULL 时本来就只放行公共库与 `everyone` 两支,这一条是不依赖那个事实的第二道保证。)
* **空查看者**(没有 actor 的后台路径):查看者行写成 `NULLIF(?, '')`,空串与
  `None` 都归一成 NULL,于是空查看者不等于任何人——只剩第 1 支和读权谓词里与人无关
  的 `everyone` 臂。归一不能省:若空查看者保持 `''`,只要库里有任何一行把 `''` 当
  用户 id(外键今天只在 `users` 里真有一个 `id=''` 时才放得进来,谓词不该寄生在这个
  外部事实上),它就会与那行的 `created_by` / 成员行 / 点名授权边相等,凭空打开读权支。

参数:`MOUNT_VIEWER_JOIN` 恰好消费**两个**位置参数,顺序 `(viewer_id, notebook_id)`
(查看者行在 FROM 子句里,先于 WHERE 出现)。新起名字而不是给旧片段加参数:漏改的
调用点绑一个参数会当场报错,而不是静默错绑。错序绑定时 `e.notebook_id` 比的是一个
用户 id,结果为空,不会串到别的库。其余新片段都是纯列引用、零参数。

旧的单参数片段原样保留:`list_mount_edges` / `mountable_notebooks`(路由 owner 专属,
查看者恒为挂载人)、深拷贝重判(按新主人)、治理侧只看 `tier='base'` 的清单都不随
查看者变化;检索参与集等调用点在后续波次切换到带查看者的片段。

## 借入挂载(第 4 支)与它的未共享门

⚠ **读权 ⇒ 可挂载是 P1 群组知识共享登记的显式行为变更**(设计文档 §6);而它带的
**未共享门**是同一轮质量评审真机复现出来的收窄,两半必须一起读:

    Carol 只读分享 Y 给 Alice → Alice 把 Y 挂进自己的 X → Alice 把 X 分享给 Bob
    → Bob 经 X 的代理读取与联邦检索读到 Y 的全文,而 Carol 从未授权 Bob。

历史上 `mountable_notebooks` 刻意排除只读分享进来的库,真实动机就是这条**转手再
分享**通道。它与「撤销」是两个不同的漏法,只有前者被开头那句「实时判定」吸收:撤销
的下一次解析里第 4 支当场为假,与公共库被降级完全同构;而转手不需要任何撤销,是
挂载方**新增**一次共享就凭空多出一批读者。所以第 4 支额外要求挂载方笔记本自身
没有任何 `notebook_members` 行、也没有任何 `notebook_grants` 行——挂载方一旦被
共享,借入边即刻失效;取消共享后自动恢复。这与「挂载不传递」的产品决策同向:借来
的东西不转借。

三支不受此限,各有理由:`tier='base'` 与 `everyone` 授权的受众本来就是全员,转手
不增加任何暴露面;同 owner 支是挂载方 owner 自己的内容,他共享 X 就是在处置自己的
内容。**只有「点名给了某几个人」的受限授权**才需要这道门,因此 `access_sql` 把授权
边拆成 `restricted_grant_access_expr` 与 `everyone_grant_expr` 两个片段——读权谓词
本身仍然不区分这两类(`grant_access_expr` 逐字未变),区别只存在于挂载有效性。

第 4 支被挡住时边**保留、置灰**,与既有失效边惯例完全一致(`list_mount_edges` 的
`active=False`),不静默删用户配置。

全部新增判定都是列引用或关联子查询(被挂库 `b`、挂载方 `a` / `a.created_by`、以及
`a.id` 上的两个 `NOT EXISTS`),因而不消费任何参数——本模块「每个片段恰好一个位置
参数」的契约由此保住。同 owner 那一支刻意保留、没有被读权片段吸收:它是一次纯列
比较,能在最常见的自有库场景上把后面几层 EXISTS 整个短路掉。

被 `NOTEBOOK_LIVE_SQL` 挡在外面的库(`status='copying'`——notebook_sharing.
copy_notebook 深拷贝期间的哨兵状态)必须被全部 OR 分支一起挡住:深拷贝落库那一刻
created_by 已经是新 owner,但数据要跨多个事务才灌完,「同 owner」分支不查 status
的话会在拷贝完成前就先满足——另一个请求能把它当参考库挂上并从中检索,读到写入
中途的半成品,与本仓库既有的「copying 状态尚不可用」不变量(notebook_catalog.
NotebookSummaryQuery.get / notebook_store.get_row 等处同款排除)矛盾(codex 评审
PR#304 第 3 轮 P2 #1)。
tier='base' 分支同样带上这条检查,不依赖「copy_notebook 只产出 tier='personal'
副本」这个事实性前提才安全。

用法:不带查看者的片段里,带 FROM 子句的(`MOUNT_JOIN`、`MOUNTED_BASE_IDS_SUBQUERY`)
恰好消费**一个**位置参数(挂载方 notebook_id),其余是纯列引用、零参数;带查看者的
片段见上一节。

**双后端同修**:`postgres/mount_sql.py` 是本文件的镜像。
"""

from app.repositories.sqlite.access_sql import (
    NOTEBOOK_LIVE_SQL,
    everyone_grant_expr,
    member_exists_expr,
    read_access_clause,
    restricted_grant_access_expr,
)

# 挂载边的 join 骨架(不含有效性过滤)—— 需要连失效边一起看的场景直接用它。
MOUNT_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id = e.base_notebook_id "
    "JOIN notebooks a ON a.id = e.notebook_id "
    "WHERE e.notebook_id = ? AND b.id != e.notebook_id"
)

# 挂载方 owner 对被挂库的**受限**读权(只读成员 ∨ 点名授权边)。同 owner 那半不在
# 这里——它已经是 MOUNT_VALID_EXPR 的独立 OR 支,且不受未共享门约束。
_BORROWED_READ_EXPR = (
    "("
    + member_exists_expr("b.id", "a.created_by", "nm")
    + " OR "
    + restricted_grant_access_expr("b.id", "a.created_by")
    + ")"
)

# 未共享门:挂载方笔记本自身没有任何只读成员、也没有任何授权边。**借来的东西不
# 转借**——Alice 把 Carol 分享给她的 Y 挂进 X 之后再把 X 分享给 Bob,Bob 就经 X 读到
# 了 Carol 从未授权他的 Y(质量评审真机复现)。谓词侧只认「有没有被共享」这个事实,
# 不去比对两边的受众:即使 Bob 恰好也在 Y 的受众里,X 一旦被共享也一律关闭借入边,
# 宁可保守——受众比对要跨库展开成员/组/授权边三张表,而这是每次参与集解析都要跑的
# 热路径。用户想恢复,取消 X 的共享即可。
_MOUNTER_NOT_SHARED_EXPR = (
    "(NOT EXISTS (SELECT 1 FROM notebook_members xm WHERE xm.notebook_id = a.id)"
    " AND NOT EXISTS (SELECT 1 FROM notebook_grants xg WHERE xg.notebook_id = a.id))"
)

# 有效性谓词。作为布尔表达式单独取用(如 list_mount_edges 的 active 标记)。
# b.NOTEBOOK_LIVE_SQL:被挂库自己正在深拷贝中(半成品)时,四个 OR 分支都不算数。
MOUNT_VALID_EXPR = (
    "(b." + NOTEBOOK_LIVE_SQL + " AND (b.tier = 'base' OR b.created_by = a.created_by"
    " OR " + everyone_grant_expr("b.id")
    + " OR (" + _BORROWED_READ_EXPR + " AND " + _MOUNTER_NOT_SHARED_EXPR + ")"
    "))"
)

# 借入边被「未共享门」关上(而非授权消失/公共库降级/深拷贝中)的判别式——
# list_mount_edges 用它给失效边选对解释文案与名字可见性:此形态下挂载方 owner
# 对被挂库**仍有合法读权**,名字不该被遮蔽,文案要给出「取消共享即可恢复」的
# 出口。与 MOUNT_VALID_EXPR 同源派生,消费点不许手拼。
MOUNT_GATE_CLOSED_EXPR = (
    "(b." + NOTEBOOK_LIVE_SQL + " AND " + _BORROWED_READ_EXPR
    + " AND NOT " + _MOUNTER_NOT_SHARED_EXPR + ")"
)

# 追加到 MOUNT_JOIN 之后的有效性过滤。
MOUNT_VALID = " AND " + MOUNT_VALID_EXPR

# 可挂候选的**来源**投影(群组知识共享 P1-T4)。回答的是「这个候选凭什么能挂」,
# 供挂载选择器给候选分组。三值与 MOUNT_VALID_EXPR 的分支一一对应,但刻意**不**
# 把第 3/4 支分开:`everyone` 授权与受限读权对用户是同一句话(「别人共享给我的」),
# 而它们的区别(转手再分享的门)只体现在候选在不在列表里,不体现在标签上。
#
# 优先级 base → mine → shared:自己 owner 的公共知识库仍判 `base`,这样本列出现
# 之前前端按 `tier` 分「公共知识库 / 我的笔记本」的结果逐字不变,新准入的那批库
# 才落进第三组。不消费任何位置参数(纯列引用),与本模块其余片段同款。
MOUNT_ORIGIN_COLUMN = (
    "CASE WHEN b.tier = 'base' THEN 'base'"
    " WHEN b.created_by = a.created_by THEN 'mine'"
    " ELSE 'shared' END AS origin"
)

# 统一次序:公共知识库在前,组内按名字、同名按 id。tier 只有
# 'base'/'personal' 两个字面量,
# 'base' < 'personal' 字典序——写成 `tier DESC` 曾经因为这份巧合而被顺手打反
# (DESC 把 'personal' 排到 'base' 前面,与本行注释描述的意图正相反,已被 codex
# 评审抓出并修正)。改成显式 CASE 钉住"base 恒排最前",不再依赖字典序方向这种
# 一旦引入第三个 tier 值就会静默失效的隐式假设。
MOUNT_ORDER = (
    " ORDER BY CASE WHEN b.tier = 'base' THEN 0 ELSE 1 END, b.name, b.id"
)

# 供 `IN (...)` 内联的 id 子查询(子查询里 ORDER BY 无意义,故不带)。
MOUNTED_BASE_IDS_SUBQUERY = "SELECT b.id " + MOUNT_JOIN + MOUNT_VALID


# ---------------------------------------------------------------- 带查看者(M3)
#
# 理由见模块 docstring「挂载仅对挂载人生效」。

# 带查看者的 join 骨架:恰好两个位置参数,顺序 (viewer_id, notebook_id)。查看者行
# 只有一行,`NULLIF` 把空串与 None 一起归一成 NULL(空查看者失败即关)。
MOUNT_VIEWER_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id = e.base_notebook_id "
    "JOIN notebooks a ON a.id = e.notebook_id "
    "CROSS JOIN (SELECT NULLIF(?, '') AS uid) v "
    "WHERE e.notebook_id = ? AND b.id != e.notebook_id"
)

# 「这条站得住的边对查看者 v 生效吗」:公共库 ∨ v 自己能读 b(挂载人由此与
# MOUNT_VALID_EXPR 蕴含,见模块 docstring)。读权复用 access_sql.read_access_clause
# 的列引用形式(零参数);别名与 MOUNT_VALID_EXPR 里的 nm/ng/nge/xm/xg 错开,嵌套
# 子查询里不互相遮蔽。⚠ 只许引用 b 与 v:引用挂载方 a 会让它变成 join 条件、不再
# 下推到 b 的扫描上,计划器随之翻成顺序扫描 notebooks(理由与 EXPLAIN pin 见模块
# docstring)。
_VIEWER_REACHES_MOUNT_EXPR = (
    "(b.tier = 'base' OR "
    + read_access_clause(
        "b",
        "vm",
        user_ref="v.uid",
        grant_alias="vg",
        group_alias="vgm",
        group_admin_alias="vga",
    )
    + ")"
)

# 带查看者的有效性谓词:既有有效性(四支 + 未共享门 + NOTEBOOK_LIVE_SQL)原样在前,
# 于是「借来的不转借」与 copying 哨兵对所有查看者照旧成立。需要 v 已经 join 进来。
MOUNT_EFFECTIVE_FOR_VIEWER_EXPR = (
    "(" + MOUNT_VALID_EXPR + " AND " + _VIEWER_REACHES_MOUNT_EXPR + ")"
)

# 追加到 MOUNT_VIEWER_JOIN 之后的有效性过滤。
MOUNT_EFFECTIVE_FOR_VIEWER = " AND " + MOUNT_EFFECTIVE_FOR_VIEWER_EXPR

# 供 `IN (...)` 内联的 id 子查询,两个位置参数 (viewer_id, notebook_id)。
MOUNTED_BASE_IDS_FOR_VIEWER_SUBQUERY = (
    "SELECT b.id " + MOUNT_VIEWER_JOIN + MOUNT_EFFECTIVE_FOR_VIEWER
)
