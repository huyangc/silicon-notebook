"""参考库挂载(notebook_bases)的 SQL 片段 —— 「哪些挂载边有效」的唯一定义点。

参与集解析、KG 可用性门、summary 投影、社区扩展、晋升目标这五处都要按挂载筛选。
若各自手写谓词,任何一份副本漂移都会让「能检索到」与「界面显示挂着」不一致,而且
这种不一致没有任何测试会自然抓到。故谓词只在这里定义一次,五处一律 import。

「有效」是解析时的实时判定而非挂载时的一次性校验:挂载边不是授权凭证。可挂范围=
公共知识库(tier='base') 或与挂载方同 owner 的库;被挂库易主、或公共库被降级后,
边保留但不生效(降级/转让常是临时的,静默删掉用户配置无法撤销),重新满足条件即
自动恢复。owner 取「挂载方笔记本的 created_by」而非请求用户,使参与集与「谁在
提问」无关 —— 只读共享的访客与库主必须看到同一个参与集。

status='copying' 的库(notebook_sharing.copy_notebook 深拷贝期间的哨兵状态)必须
被两个 OR 分支一起挡住:深拷贝落库那一刻 created_by 已经是新 owner,但数据要
跨多个事务才灌完,「同 owner」分支不查 status 的话会在拷贝完成前就先满足——
另一个请求能把它当参考库挂上并从中检索,读到写入中途的半成品,与本仓库既有的
「copying 状态尚不可用」不变量(notebook_catalog.NotebookSummaryQuery.get /
notebook_store.get_row 等处同款排除)矛盾(codex 评审 PR#304 第 3 轮 P2 #1)。
tier='base' 分支同样带上这条检查,不依赖「copy_notebook 只产出 tier='personal'
副本」这个事实性前提才安全。

用法:全部片段都恰好消费**一个**位置参数(挂载方 notebook_id)。
"""

# 挂载边的 join 骨架(不含有效性过滤)—— 需要连失效边一起看的场景直接用它。
MOUNT_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id = e.base_notebook_id "
    "JOIN notebooks a ON a.id = e.notebook_id "
    "WHERE e.notebook_id = ? AND b.id != e.notebook_id"
)

# 有效性谓词。作为布尔表达式单独取用(如 list_mount_edges 的 active 标记)。
# b.status != 'copying':被挂库自己正在深拷贝中(半成品)时,两个 OR 分支都不算数。
MOUNT_VALID_EXPR = (
    "(b.status != 'copying' AND (b.tier = 'base' OR b.created_by = a.created_by))"
)

# 追加到 MOUNT_JOIN 之后的有效性过滤。
MOUNT_VALID = " AND " + MOUNT_VALID_EXPR

# 统一次序:公共知识库在前,组内按名字。tier 只有 'base'/'personal' 两个字面量,
# 'base' < 'personal' 字典序——写成 `tier DESC` 曾经因为这份巧合而被顺手打反
# (DESC 把 'personal' 排到 'base' 前面,与本行注释描述的意图正相反,已被 codex
# 评审抓出并修正)。改成显式 CASE 钉住"base 恒排最前",不再依赖字典序方向这种
# 一旦引入第三个 tier 值就会静默失效的隐式假设。
MOUNT_ORDER = " ORDER BY CASE WHEN b.tier = 'base' THEN 0 ELSE 1 END, b.name"

# 供 `IN (...)` 内联的 id 子查询(子查询里 ORDER BY 无意义,故不带)。
MOUNTED_BASE_IDS_SUBQUERY = "SELECT b.id " + MOUNT_JOIN + MOUNT_VALID
