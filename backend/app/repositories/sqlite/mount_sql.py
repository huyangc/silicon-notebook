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
转让/撤销常是临时的,静默删掉用户配置无法撤销),重新满足条件即自动恢复。owner 取
「挂载方笔记本的 created_by」而非请求用户,使参与集与「谁在提问」无关 —— 只读共享
的访客与库主必须看到同一个参与集。

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

用法:全部片段都恰好消费**一个**位置参数(挂载方 notebook_id)。

**双后端同修**:`postgres/mount_sql.py` 是本文件的镜像。
"""

from app.repositories.sqlite.access_sql import (
    NOTEBOOK_LIVE_SQL,
    everyone_grant_expr,
    member_exists_expr,
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
