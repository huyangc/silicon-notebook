"""notebook 授权(读权/写权)的 SQL 谓词 —— 「谁能读/写这个 notebook」的唯一定义点。

`sqlite/access_sql.py` 的 PostgreSQL 镜像(占位符 `%s`)。完整理由、参数约定、四值
`principal_type` 白名单的三条写法约束(尤其「`everyone` 只按 `principal_type` 精确
匹配、绝不从 `principal_id` 推断」)都写在 SQLite 那一份的模块 docstring 里,两份必须
同修;这里只登记 PG 侧独有的事实。

两条谓词同样刻意不对称:写权 = owner-only(只读成员与群组被授权者都不得写,写权
扩展是 P2 的能力翻转),读权 = owner ∪ `notebook_members` 有行 ∪ 有效授权边。

消费者清单(改这里就要一起看):

* `postgres/sharing_store.py`:`user_can_access_notebook`(写权)、
  `user_can_read_notebook`(读权)、`is_member`(成员探测)。
* `postgres/memory_store.py`:`_read_access_clause`、`_answer_save_scope_exists`、
  `validate_promotion_approval_access_on`(用列引用形式)。
* `postgres/mount_sql.py::MOUNT_VALID_EXPR` —— 「读权 ⇒ 可挂载」,用列引用形式嵌入,
  不消费任何参数。
* `postgres/search.py::_memory_match_predicates` —— Memory 词法检索的读权过滤
  (被 `memory_candidate_ids` / `memory_match_count` / `memory_page_candidate_ids`
  三个入口共用),与 `_read_access_clause` 同形,是本次收口前的第三份独立复刻。
* ⚠ **两段式带锁写法,刻意保留**(`postgres/memory_store.py` 的
  `create_candidate_with_initial_revision`、答案存 Memory 的写事务分支、
  `_lock_memory_aggregate_on`):它们先 `SELECT created_by FROM notebooks ... FOR SHARE`
  锁住 notebooks 行,再依次 `SELECT 1 FROM notebook_members ... FOR SHARE` 锁成员行、
  `GRANT_PROBE_FOR_SHARE_SQL` 锁授权边行。合并成单条 EXISTS 会丢掉行锁(EXISTS
  子查询里的行拿不到 `FOR SHARE`),也会丢掉「notebook 不存在」与「无读权」的三态
  区分。这三处只复用 `MEMBER_PROBE_FOR_SHARE_SQL` / `GRANT_PROBE_FOR_SHARE_SQL`
  这两半,owner 那一半保持原样。
  `GRANT_PROBE_FOR_SHARE_SQL` 刻意只写 `FOR SHARE OF ng`:它锁的是**授权边行**
  (防写事务进行中被撤销),与成员探测锁成员行同理。组成员资格是二阶输入、经
  EXISTS 子查询判定,拿不到也不需要这把锁——正如既有实现从不锁 `users` 行。

**刻意不收口**(与 SQLite 侧同款,理由写在那份 docstring 里):
`postgres/query_store.py::joined_notebook_rows`(成员列表查询,只要成员那一半且多一个
`status != 'copying'` 过滤)、`postgres/sharing_store.py` 的成员关系 CRUD。

**双后端同修**:改本文件必须同改 `sqlite/access_sql.py`。
"""

# 成员探测:该用户在 notebook_members 里是否有行。
MEMBER_PROBE_SQL = (
    "SELECT 1 FROM notebook_members WHERE notebook_id=%s AND user_id=%s"
)

# 上面那条加行锁的变体,供两段式带锁写法使用(见模块 docstring 的 ⚠ 条目)。
MEMBER_PROBE_FOR_SHARE_SQL = MEMBER_PROBE_SQL + " FOR SHARE"


def member_exists_expr(
    notebook_ref: str,
    user_ref: str,
    member_alias: str = "nm",
) -> str:
    """成员资格的 `EXISTS (...)` 布尔表达式。

    两个 ref 既可以是占位符 `%s`,也可以是外层查询的列引用;传列引用时不消费参数。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebook_members {member_alias} "
        f"WHERE {member_alias}.notebook_id={notebook_ref} "
        f"AND {member_alias}.user_id={user_ref})"
    )


def _principal_match_expr(
    grant_alias: str,
    user_ref: str,
    group_alias: str,
    group_admin_alias: str,
) -> str:
    """授权边主体判定:四个已知 `principal_type` 各一条臂,没有兜底分支。

    消费三个 `user_ref`;`everyone` 那条臂只比一个字面量,一个参数都不消费——
    **绝不能**改成看 `principal_id`(设计文档已定裁决 1b)。
    """
    return (
        "("
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
        f" OR {grant_alias}.principal_type='everyone'"
        ")"
    )


def grant_access_expr(
    notebook_ref: str,
    user_ref: str,
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """有效授权边的 `EXISTS (...)` 布尔表达式。

    两个 ref 既可以是占位符 `%s`,也可以是外层查询的列引用。传占位符时消费
    **三个** user 参数,传列引用时不消费参数。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebook_grants {grant_alias} "
        f"WHERE {grant_alias}.notebook_id={notebook_ref} AND "
        + _principal_match_expr(
            grant_alias, user_ref, group_alias, group_admin_alias
        )
        + ")"
    )


# 授权边探测:该用户在这个 notebook 上是否有一条有效授权边。参数用
# `grant_probe_params()` 展开。
GRANT_PROBE_SQL = (
    "SELECT 1 FROM notebook_grants ng WHERE ng.notebook_id=%s AND "
    + _principal_match_expr("ng", "%s", "ngm", "nga")
)

# 上面那条加行锁的变体,供两段式带锁写法使用。`OF ng` 是必须的:被 EXISTS 子查询
# 引用的 group_members 不在 FROM 列表里,裸 `FOR SHARE` 也锁不到它。
GRANT_PROBE_FOR_SHARE_SQL = GRANT_PROBE_SQL + " FOR SHARE OF ng"


def read_access_clause(
    nb_alias: str = "nb",
    member_alias: str = "nm",
    *,
    user_ref: str = "%s",
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """读权谓词,作用在**已经 join 进来**的 notebooks 行上。

    `user_ref` 默认是占位符,此时消费 `read_access_params(user_id)`;传列引用则一个
    参数都不消费。它是关键字参数,理由见 SQLite 那份的「参数约定」。
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


def read_access_exists_clause(
    row_alias: str = "m",
    nb_alias: str = "access_nb",
    member_alias: str = "access_nm",
) -> str:
    """读权谓词的自包含形式:自己去 join notebooks。

    消费 `read_access_params(user_id)`。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebooks {nb_alias} "
        f"WHERE {nb_alias}.id={row_alias}.notebook_id "
        f"AND {read_access_clause(nb_alias, member_alias, grant_alias='access_ng', group_alias='access_ngm', group_admin_alias='access_nga')})"
    )


# 读权片段消费几个位置参数——从谓词自己的占位符数**推导**而不是手写常量。
_READ_ACCESS_PARAM_COUNT = read_access_clause().count("%s")
_GRANT_PROBE_USER_PARAM_COUNT = GRANT_PROBE_SQL.count("%s") - 1


def read_access_params(user_id: str) -> tuple[str, ...]:
    """`read_access_clause()` / `read_access_exists_clause()` 要消费的位置参数。"""
    return (user_id,) * _READ_ACCESS_PARAM_COUNT


def grant_probe_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`GRANT_PROBE_SQL`(及 FOR SHARE 变体)要消费的位置参数。"""
    return (notebook_id,) + (user_id,) * _GRANT_PROBE_USER_PARAM_COUNT


# 写权(owner-only)的完整查询:有行即有写权。notebook 不存在 → 无行 → 无写权。
NOTEBOOK_WRITE_SQL = "SELECT 1 FROM notebooks WHERE id=%s AND created_by=%s"

# 读权(owner ∪ 只读成员 ∪ 有效授权边)的完整查询:有行即有读权。
NOTEBOOK_READ_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND " + read_access_clause()
)
