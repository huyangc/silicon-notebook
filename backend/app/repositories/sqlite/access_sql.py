"""notebook 授权(读权/管理权/写权)的 SQL 谓词 —— 「谁能读/管/写这个 notebook」的
唯一定义点。

镜像 `mount_sql.py` 的模式。理由同款:授权判定散落在 sharing_store、memory_store、
search 三处,各自手写「owner ∨ 只读成员」的 EXISTS 子查询。副本越多,任何一份漂移
就越会造成「A 能读 B 不能读」的不一致,而这种不一致没有任何测试会自然抓到——它不
体现为报错,只体现为某条路径悄悄多给或少给了权限。故谓词只在这里定义一次。

三条谓词是**刻意不对称**的,这是产品的安全边界,不是疏漏:

* **写权(`NOTEBOOK_WRITE_SQL`)= owner-only**。`notebook_members` 里的成员是只读
  访客;放宽这一条会让只读共享变成可写共享。notebook 不存在时同样为假(无行 →
  False),与「不泄露存在性」的既有口径一致。P2 之后它**不再**是全部写入口的判据
  (见下一条),但它自身语义一个字都没变,仍是「恒 owner」那几格的实现。
* **管理权(`NOTEBOOK_ADMIN_SQL`)= owner ∨ `role='admin'` 的有效授权边**(P2 能力
  翻转,已定裁决 P2-1)。这是 P0 以来「写权恒 owner-only」这条边界**第一次有边界地
  放宽**:组管理员对被共享进本组的库获得内容管理能力(加/删/重解析来源、触发图谱与
  检索索引构建、knowhow 写、知识治理写、命令目录写、共享管理)。
  三处**不翻**,各有理由:
    1. `notebook:delete`(删库)恒 owner——爆炸半径是整本库,且 owner 无法撤销;
    2. Agent/MCP 面恒 owner(`mcp_server._writable_notebook`)——那是另一套鉴权模型
       的刻意分歧，`architecture.md` 的 Sharing 边界已登记;
    3. `reports:write` 已在 P1 转成行级 `created_by` 判定,不由这张表表达。
  主体判定与读权**同构**:同一份四值 `principal_type` 白名单,只多一个
  `ng.role='admin'` 条件(见 `_admin_principal_match_expr`)。
* **读权(`NOTEBOOK_READ_SQL`)= owner ∪ `notebook_members` 有行 ∪ 有效授权边**。

三者是严格的包含链:`写权 ⊆ 管理权 ⊆ 读权`。owner 三者皆真;管理边持有者可读可管
不可删库;只读成员与 viewer 边只可读。

## 有效授权边(P1 群组知识共享)

`notebook_grants` 一行即一条授权边。「有效」= 该行的 `principal_type` 精确等于四个
已知值之一,且对应的主体判定为真:

| `principal_type` | 判据 |
| --- | --- |
| `user` | `principal_id` = 该用户 |
| `group` | 该用户在 `group_members` 里属于 `principal_id` 这个组 |
| `group_admins` | 同上,且 `group_members.role = 'admin'` |
| `everyone` | 恒真 |

**管理级授权边**在此之上多一个条件:该行的 `role` 精确等于 `'admin'`。四条臂一条都
不特判——`everyone` + `admin` 这个组合由 app 层的发放口径挡住
(`models/groups.py::GRANTABLE_PRINCIPAL_TYPES` 只收两个群组主体,`everyone` 边根本
没有写入口),谓词这一侧保持与读权同构,免得两份主体判定分叉。

三条**不许动**的写法约束:

1. **四值一律精确匹配,不写 `NOT IN`/`else`/兜底分支**(设计文档已定裁决 1b)。
   正向 shadow 复制在 UNIQUE 冲突时会给 `notebook_grants` 的 `principal_type` 暂写
   哨兵串停车;精确匹配让这类停车行 fail-safe——它谁也匹配不上,而任何形式的
   「不是前三种就当 everyone」都会把一条停车中的行变成全站可读。
2. **`everyone` 只能按 `principal_type='everyone'` 判,绝不能从 `principal_id` 推断**
   (`IS NULL` / `=''` 都不行)。`principal_id` 是 `NOT NULL DEFAULT ''` 的裸列,
   `''` 是它的默认值而不是「everyone」的标记。
3. **`role` 列在读权判定里不消费**。任何有效授权边都 ≥ viewer,而 viewer 即可读;
   区分 viewer/admin 的 `effective_role` 由 API 投影的独立查询提供,不进热读谓词。
   ⚠ P2 之后 `role` 进入**管理权**谓词(且只进它):读权那条 SQL 里仍然一个字都没有
   提到 `role`,两条谓词各读各的。

## 参数约定

用户 id 在读权谓词里出现多次(owner 一次、成员一次、授权边三次),**不要手数**:
一律用 `read_access_params(user_id)` 展开,谓词再长一条也不会漏。

* `NOTEBOOK_WRITE_SQL` —— `(notebook_id, user_id)`
* `NOTEBOOK_READ_SQL` —— `(notebook_id, *read_access_params(user_id))`
* `NOTEBOOK_ADMIN_SQL` —— `(notebook_id, *admin_access_params(user_id))`
* `MEMBER_PROBE_SQL` —— `(notebook_id, user_id)`
* `GRANT_PROBE_SQL` —— `grant_probe_params(notebook_id, user_id)`
* `read_access_clause()` / `read_access_exists_clause()` —— `read_access_params(user_id)`
* `admin_access_clause()` —— `admin_access_params(user_id)`
* 传**列引用**(而非占位符)的形式一个参数都不消费,见下。

`member_exists_expr` / `grant_access_expr` / `read_access_clause` 的 ref 参数既可以是
占位符,也可以是外层查询的列引用(如 `mount_sql.MOUNT_VALID_EXPR` 用 `a.created_by`
表示「挂载方 owner」、`validate_promotion_approval_access_on` 用 `m.created_by`)。
`read_access_clause` 的 `user_ref` 刻意是**关键字参数**:它若挤在 `nb_alias` 之后当
第二个位置参数,既有的 `read_access_clause("n", "nm")`(第二个是 member_alias)会被
静默解释成「owner 是 nm 这一列」——一个不报错、只是把权限判错的形态。

消费者清单(改这里就要一起看):

* `sqlite/sharing_store.py`:`user_can_access_notebook`(写权)、`user_can_admin_notebook`
  (管理权)、`user_can_read_notebook`(读权)、`is_member`(成员探测)。
* `sqlite/memory_store.py`:`_read_access_clause`(嵌进 Memory 各处读查询)、
  `create_candidate_with_initial_revision`(写前判定)、
  `create_answer_with_initial_revision`(答案存 Memory 的范围校验;同名的
  `_answer_save_scope_locked_on` 是不含 SQL 的空接缝,谓词不在那儿)、
  `validate_promotion_approval_access_on`(晋升审批写事务内复核,用列引用形式)。
* `sqlite/mount_sql.py::MOUNT_VALID_EXPR` —— 「读权 ⇒ 可挂载」,用列引用形式嵌入
  (`b` 是被挂库、`a.created_by` 是挂载方 owner),故不消费任何参数。
* `sqlite/memory_store.py::_lock_memory_aggregate_on` —— ⚠ **两段式写法,刻意保留**:
  它先单查 `created_by` 以区分「notebook 不存在」(KeyError)与「无读权」
  (PermissionError/KeyError),再依次探成员、探授权边;PG 侧对应实现还在这几步上
  各挂 `FOR SHARE` 行锁。合并成单条 EXISTS 会丢掉这个三态区分与 PG 的行锁语义。
  该处只复用 `MEMBER_PROBE_SQL` / `GRANT_PROBE_SQL` 这两半,owner 那一半保持原样。

**刻意不收口**(核实过与读权谓词不同义,收进来就是把语义改了):

* `*/query_store.py::joined_notebook_rows` —— 「我加入了哪些笔记本」的**列表**查询,
  不是对某个 notebook 的授权判定:它刻意只要成员那一半(自有库由另一条查询给出,
  合进来会让自有库在「加入的」列表里重复出现),还多一个 `status != 'copying'` 过滤。
  群组库要不要进这个列表是产品决策(P1 的「群组」分区由 T3 单独投影),不是谓词
  一致性问题。
* `*/sharing_store.py` 的 `add_member` / `remove_member` / `kick_all_members` /
  `list_members` —— 成员关系的 CRUD,不是判定。

**双后端同修**:`postgres/access_sql.py` 是本文件的镜像(占位符为 `%s`),两份文件
结构逐条对应,改一侧必须改另一侧。
"""

# 成员探测:该用户在 notebook_members 里是否有行。供 `is_member` 与两段式带锁探测复用。
MEMBER_PROBE_SQL = (
    "SELECT 1 FROM notebook_members WHERE notebook_id=? AND user_id=?"
)


def member_exists_expr(
    notebook_ref: str,
    user_ref: str,
    member_alias: str = "nm",
) -> str:
    """成员资格的 `EXISTS (...)` 布尔表达式。

    两个 ref 既可以是占位符 `?`,也可以是外层查询的列引用。传列引用时不消费参数。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebook_members {member_alias} "
        f"WHERE {member_alias}.notebook_id={notebook_ref} "
        f"AND {member_alias}.user_id={user_ref})"
    )


def _restricted_principal_arms(
    grant_alias: str,
    user_ref: str,
    group_alias: str,
    group_admin_alias: str,
) -> str:
    """**受限**主体的三条臂(user / group / group_admins),不含 `everyone`。

    「受限」= 受众是一份点名的名单,库主授权时心里有一份具体的人。`everyone` 是
    另一类:受众本来就是全员。两者在**读权**上完全同权,拆开只是为了让挂载有效性
    能对它们区别对待(见 `mount_sql.py` 的借入挂载未共享门)。

    消费三个 `user_ref`。臂的顺序是「便宜的先算」+「与设计文档的枚举顺序一致」的
    折中,语义与顺序无关。
    """
    return (
        f"({grant_alias}.principal_type='user' "
        f"AND {grant_alias}.principal_id={user_ref})"
        f" OR ({grant_alias}.principal_type='group' AND EXISTS ("
        f"SELECT 1 FROM group_members {group_alias} "
        f"WHERE {group_alias}.group_id={grant_alias}.principal_id "
        f"AND {group_alias}.user_id={user_ref}))"
        f" OR ({grant_alias}.principal_type='group_admins' AND EXISTS ("
        f"SELECT 1 FROM group_members {group_admin_alias} "
        f"WHERE {group_admin_alias}.group_id={grant_alias}.principal_id "
        f"AND {group_admin_alias}.user_id={user_ref} "
        f"AND {group_admin_alias}.role='admin'))"
    )


def _everyone_principal_arm(grant_alias: str) -> str:
    """`everyone` 那条臂:只比一个字面量,一个参数都不消费。

    **绝不能**改成看 `principal_id`(裁决 1b)。
    """
    return f"{grant_alias}.principal_type='everyone'"


def _principal_match_expr(
    grant_alias: str,
    user_ref: str,
    group_alias: str,
    group_admin_alias: str,
) -> str:
    """授权边主体判定:四个已知 `principal_type` 各一条臂,没有兜底分支。

    = 受限三臂 ∪ everyone。拆成两个私有片段之后这里必须**逐字**拼回原样:读权语义
    在 P1-T2 的评审修复里一个字都没有变,变的只有挂载有效性。
    """
    return (
        "("
        + _restricted_principal_arms(
            grant_alias, user_ref, group_alias, group_admin_alias
        )
        + " OR "
        + _everyone_principal_arm(grant_alias)
        + ")"
    )


def _admin_principal_match_expr(
    grant_alias: str,
    user_ref: str,
    group_alias: str,
    group_admin_alias: str,
) -> str:
    """**管理级**授权边的主体判定 = **受限三臂**(user/group/group_admins,**不含
    everyone**)∧ `role='admin'`(裁决 P2-1,P2-T2 评审 P2-1 收窄)。

    刻意写成「复用 `_restricted_principal_arms` 外加一个 `role` 条件」而不是另抄一份三臂:
    抄一份的话,某天有人只在读权那份里补了主体、或只在这份里漏掉 `role='admin'` 的子
    条件,两条谓词就静默分叉,而分叉的形态恰好是「管理权比读权更宽」——一条越权通道。
    复用保证「管理权 ⊆ 读权」是**构造性**的(受限三臂 ⊂ 读权四臂),不靠测试逐格覆盖。

    **`everyone` 被排除**是深度防御(设计 §4 明文:everyone 只能 viewer;发放口径
    `GRANTABLE_PRINCIPAL_TYPES` 也只收两个群组主体,根本写不出 everyone+admin 边)。
    不特判 everyone 的旧写法依赖「发放口径永不写 everyone+admin」这个外部前提;正向
    shadow 停车会给冲突行的 `role` 暂写哨兵串,一条 `(everyone,'',<非法role>)` 停车行
    若恰好在某个瞬间被读成 `role='admin'`,就会把管理权发给全站——用受限三臂从谓词侧
    根绝,不再依赖那个前提。读权谓词一个字都没动(它照旧四臂全收、role 不参与)。

    参数消费与 `_restricted_principal_arms` 完全相同(`role` 比的是字面量)。
    """
    return (
        f"({grant_alias}.role='admin' AND ("
        + _restricted_principal_arms(
            grant_alias, user_ref, group_alias, group_admin_alias
        )
        + "))"
    )


def _grant_exists_expr(notebook_ref: str, grant_alias: str, condition: str) -> str:
    """`notebook_grants` 上的 `EXISTS (...)` 骨架,主体判定由 `condition` 决定。"""
    return (
        f"EXISTS (SELECT 1 FROM notebook_grants {grant_alias} "
        f"WHERE {grant_alias}.notebook_id={notebook_ref} AND "
        + condition
        + ")"
    )


def grant_access_expr(
    notebook_ref: str,
    user_ref: str,
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """**任意**有效授权边的 `EXISTS (...)` 布尔表达式(四类主体全收)。

    两个 ref 既可以是占位符 `?`,也可以是外层查询的列引用。传占位符时消费
    **三个** user 参数(见 `_principal_match_expr`),传列引用时不消费参数。
    """
    return _grant_exists_expr(
        notebook_ref,
        grant_alias,
        _principal_match_expr(grant_alias, user_ref, group_alias, group_admin_alias),
    )


def admin_grant_access_expr(
    notebook_ref: str,
    user_ref: str,
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """**管理级**有效授权边(四类主体 ∧ `role='admin'`)的 `EXISTS (...)` 布尔表达式。

    两个 ref 既可以是占位符 `?`,也可以是外层查询的列引用。参数消费与
    `grant_access_expr` 完全相同(多出来的 `role` 条件比的是字面量)。
    """
    return _grant_exists_expr(
        notebook_ref,
        grant_alias,
        _admin_principal_match_expr(
            grant_alias, user_ref, group_alias, group_admin_alias
        ),
    )


def restricted_grant_access_expr(
    notebook_ref: str,
    user_ref: str,
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """**受限**授权边(user / group / group_admins,不含 everyone)的 `EXISTS (...)`。

    只有 `mount_sql.MOUNT_VALID_EXPR` 用它:借入挂载的转手再分享收窄要区分「点名
    授权」与「全员授权」,而读权谓词不区分。参数消费同 `grant_access_expr`。
    """
    return _grant_exists_expr(
        notebook_ref,
        grant_alias,
        "("
        + _restricted_principal_arms(
            grant_alias, user_ref, group_alias, group_admin_alias
        )
        + ")",
    )


def everyone_grant_expr(notebook_ref: str, grant_alias: str = "nge") -> str:
    """`everyone` 授权边的 `EXISTS (...)`——与**谁在问**无关,故不接 user_ref。

    因此它一个参数都不消费,`notebook_ref` 传占位符时才消费一个。
    """
    return _grant_exists_expr(
        notebook_ref, grant_alias, _everyone_principal_arm(grant_alias)
    )


# 授权边探测:该用户在这个 notebook 上是否有一条有效授权边。供两段式三态探测复用
# (成员探测未命中之后的第三段)。参数用 `grant_probe_params()` 展开。
GRANT_PROBE_SQL = (
    "SELECT 1 FROM notebook_grants ng WHERE ng.notebook_id=? AND "
    + _principal_match_expr("ng", "?", "ngm", "nga")
)

# **管理级**授权边探测:该用户在这个 notebook 上是否有一条 `role='admin'` 的有效授权边。
# 与 `GRANT_PROBE_SQL` 同形,只把主体判定换成 `_admin_principal_match_expr`——**复用**
# 那一份而不是另抄一遍主体判定(唯一定义点红线)。参数用 `admin_grant_probe_params()`。
#
# 为什么要一条**顶层**查询而不是给 `NOTEBOOK_ADMIN_SQL` 加锁:授权边行藏在后者的 EXISTS
# 子查询里,PG 侧 `FOR SHARE` 够不着它。SQLite 侧没有行锁(进程写锁已把写事务串起来),
# 这条常量存在的意义是**让两个后端的谓词面对等**——PG 侧的带锁变体必须有个同名同义的
# 对侧,否则 parity 守卫报红(codex #519 R5)。
ADMIN_GRANT_PROBE_SQL = (
    "SELECT 1 FROM notebook_grants ng WHERE ng.notebook_id=? AND "
    + _admin_principal_match_expr("ng", "?", "ngm", "nga")
)


def read_access_clause(
    nb_alias: str = "nb",
    member_alias: str = "nm",
    *,
    user_ref: str = "?",
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """读权谓词,作用在**已经 join 进来**的 notebooks 行上。

    `user_ref` 默认是占位符,此时消费 `read_access_params(user_id)`;传列引用
    (`user_ref="a.created_by"`)则一个参数都不消费。它是关键字参数,理由见模块
    docstring 的「参数约定」。
    """
    return (
        f"({nb_alias}.created_by={user_ref} OR "
        + member_exists_expr(f"{nb_alias}.id", user_ref, member_alias)
        + " OR "
        + grant_access_expr(
            f"{nb_alias}.id", user_ref, grant_alias, group_alias, group_admin_alias
        )
        + ")"
    )


def admin_access_clause(
    nb_alias: str = "nb",
    *,
    user_ref: str = "?",
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """管理权谓词,作用在**已经 join 进来**的 notebooks 行上。

    = owner ∨ 管理级有效授权边。与 `read_access_clause` 的差别只有两处,都刻意:
    没有 `notebook_members` 那一支(只读成员就是只读成员,群组共享不改变这一点),
    授权边那一支换成管理级。

    `user_ref` 默认是占位符,此时消费 `admin_access_params(user_id)`;传列引用则一个
    参数都不消费。它是关键字参数,理由同 `read_access_clause`(见模块 docstring 的
    「参数约定」)。
    """
    return (
        f"({nb_alias}.created_by={user_ref} OR "
        + admin_grant_access_expr(
            f"{nb_alias}.id", user_ref, grant_alias, group_alias, group_admin_alias
        )
        + ")"
    )


def read_access_exists_clause(
    row_alias: str = "m",
    nb_alias: str = "access_nb",
    member_alias: str = "access_nm",
) -> str:
    """读权谓词的自包含形式:自己去 join notebooks。

    供「外层查的是别的表、只带了 `notebook_id` 列」的场景(Memory 各处读查询)使用。
    消费 `read_access_params(user_id)`。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebooks {nb_alias} "
        f"WHERE {nb_alias}.id={row_alias}.notebook_id "
        f"AND {read_access_clause(nb_alias, member_alias, grant_alias='access_ng', group_alias='access_ngm', group_admin_alias='access_nga')})"
    )


# 读权/管理权片段消费几个位置参数——从谓词自己的占位符数**推导**而不是手写常量:
# 谓词长一条臂,这个数就自动跟着长,不存在「改了 SQL 忘了改常量」的漂移形态。
_READ_ACCESS_PARAM_COUNT = read_access_clause().count("?")
_ADMIN_ACCESS_PARAM_COUNT = admin_access_clause().count("?")
_GRANT_PROBE_USER_PARAM_COUNT = GRANT_PROBE_SQL.count("?") - 1
_ADMIN_GRANT_PROBE_USER_PARAM_COUNT = ADMIN_GRANT_PROBE_SQL.count("?") - 1


def admin_access_params(user_id: str) -> tuple[str, ...]:
    """`admin_access_clause()` 要消费的位置参数。

    全是同一个 user_id(owner 比一次、管理级授权边三条臂各比一次)。数目**必然**比
    `read_access_params` 少一个(没有成员那一支),但仍然从占位符数推导而不是写成
    `_READ_ACCESS_PARAM_COUNT - 1`:后者会把两条谓词的形状绑在一起,读权某天多一条臂
    就会把管理权的参数数也悄悄带偏。
    """
    return (user_id,) * _ADMIN_ACCESS_PARAM_COUNT


def read_access_params(user_id: str) -> tuple[str, ...]:
    """`read_access_clause()` / `read_access_exists_clause()` 要消费的位置参数。

    全是同一个 user_id(owner 比一次、成员比一次、授权边三条臂各比一次)。调用点
    一律 `*read_access_params(uid)` 展开,不要手写 `(uid, uid, ...)`。
    """
    return (user_id,) * _READ_ACCESS_PARAM_COUNT


def grant_probe_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`GRANT_PROBE_SQL`(及 PG 的 FOR SHARE 变体)要消费的位置参数。"""
    return (notebook_id,) + (user_id,) * _GRANT_PROBE_USER_PARAM_COUNT


def admin_grant_probe_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`ADMIN_GRANT_PROBE_SQL`(及 PG 的 FOR SHARE 变体)要消费的位置参数。

    单独一份而不是复用 `grant_probe_params`:两条查询的主体判定不同(管理级排除了
    `everyone` 那一臂),占位符数各自**推导**——今天恰好相等,明天任一侧的臂变了就会
    分叉,而共用一份参数展开会静默错位。
    """
    return (notebook_id,) + (user_id,) * _ADMIN_GRANT_PROBE_USER_PARAM_COUNT


# 写权(owner-only)的完整查询:有行即有写权。notebook 不存在 → 无行 → 无写权。
NOTEBOOK_WRITE_SQL = "SELECT 1 FROM notebooks WHERE id=? AND created_by=?"

# 管理权(owner ∪ 管理级有效授权边)的完整查询:有行即有管理权。notebook 不存在 →
# 无行 → 无管理权,与另外两条同口径。
NOTEBOOK_ADMIN_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=? AND " + admin_access_clause()
)

# 读权(owner ∪ 只读成员 ∪ 有效授权边)的完整查询:有行即有读权。
NOTEBOOK_READ_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=? AND " + read_access_clause()
)
