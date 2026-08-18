"""notebook 授权(读权/管理权/写权)的 SQL 谓词 —— 「谁能读/管/写这个 notebook」的
唯一定义点。

`sqlite/access_sql.py` 的 PostgreSQL 镜像(占位符 `%s`)。完整理由、参数约定、四值
`principal_type` 白名单的三条写法约束(尤其「`everyone` 只按 `principal_type` 精确
匹配、绝不从 `principal_id` 推断」)、以及 P2 管理权翻转的边界(哪三处**不翻**)都
写在 SQLite 那一份的模块 docstring 里,两份必须同修;这里只登记 PG 侧独有的事实。

三条谓词同样刻意不对称,包含链为 `写权 ⊆ 管理权 ⊆ 读权`:写权 = owner-only;管理权
= owner ∪ `role='admin'` 的有效授权边(P2 能力翻转,裁决 P2-1);读权 = owner ∪
`notebook_members` 有行 ∪ 有效授权边。

消费者清单(改这里就要一起看):

* `postgres/sharing_store.py`:`user_can_access_notebook`(写权)、
  `user_can_admin_notebook`(管理权)、`user_can_read_notebook`(读权)、
  `is_member`(成员探测)。
* `postgres/memory_store.py`:`_read_access_clause`、`_answer_save_scope_exists`、
  `validate_promotion_approval_access_on`(用列引用形式)。
* `postgres/mount_sql.py::MOUNT_VALID_EXPR` —— 「读权 ⇒ 可挂载」,用列引用形式嵌入,
  不消费任何参数。
* `postgres/search.py::_memory_match_predicates` —— Memory 词法检索的读权过滤
  (被 `memory_candidate_ids` / `memory_match_count` / `memory_page_candidate_ids`
  三个入口共用),与 `_read_access_clause` 同形,是本次收口前的第三份独立复刻。
* ⚠ **三段式带锁写法,刻意保留**(`postgres/memory_store.py` 的
  `create_candidate_with_initial_revision`、答案存 Memory 的写事务分支、
  `_lock_memory_aggregate_on`):它们先 `SELECT created_by FROM notebooks ... FOR SHARE`
  锁住 notebooks 行,再依次 `SELECT 1 FROM notebook_members ... FOR SHARE` 锁成员行、
  `GRANT_PROBE_FOR_SHARE_SQL` 锁授权边行。合并成单条 EXISTS 会丢掉行锁(EXISTS
  子查询里的行拿不到 `FOR SHARE`),也会丢掉「notebook 不存在」与「无读权」的三态
  区分。这三处只复用 `MEMBER_PROBE_FOR_SHARE_SQL` / `GRANT_PROBE_FOR_SHARE_SQL`
  这两半,owner 那一半保持原样。
  `GRANT_PROBE_FOR_SHARE_SQL` 写 `FOR SHARE OF ng`:锁的是**授权边行**(防写事务
  进行中授权被撤销),与成员探测锁成员行同理。`OF ng` 不是语法必须——本语句同层
  rangetable 里只有 `notebook_grants ng`,裸 `FOR SHARE` 锁的对象相同;写出来是为了
  让「锁的到底是哪张表的行」不依赖读者去数 FROM 子句。
  ⚠ **组成员资格刻意不锁**,这是一条已登记的取舍而不是遗漏:`group_members` 只在
  EXISTS 子查询里,`FOR SHARE` 够不着它(要锁就得改写成 LEFT JOIN,而 PG 不允许对
  外连接的可空侧加锁)。后果是**一次在飞的写事务可以带着提交时已经失效的组授权
  落地**——用户在 t0 通过组授权拿到读权、t1 被移出组、他 t0 就开始的那个写事务在
  t2 提交成功。残留物是**一条被移除者自己也读不到的私有 Memory 行**(读路径当场
  为假),既不扩散也不可见,代价远小于为它把热路径改成带锁 join。

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


def _restricted_principal_arms(
    grant_alias: str,
    user_ref: str,
    group_alias: str,
    group_admin_alias: str,
) -> str:
    """**受限**主体的三条臂(user / group / group_admins),不含 `everyone`。

    拆分理由与 SQLite 那一份同款(挂载有效性要区别对待,读权不区别对待)。
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
    """`everyone` 那条臂——**绝不能**改成看 `principal_id`(已定裁决 1b)。"""
    return f"{grant_alias}.principal_type='everyone'"


def _principal_match_expr(
    grant_alias: str,
    user_ref: str,
    group_alias: str,
    group_admin_alias: str,
) -> str:
    """授权边主体判定:四个已知 `principal_type` 各一条臂,没有兜底分支。

    = 受限三臂 ∪ everyone,逐字拼回拆分之前的原样。
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
    """**管理级**授权边的主体判定 = **受限三臂**(不含 everyone)∧ `role='admin'`
    (裁决 P2-1,P2-T2 评审 P2-1 收窄)。

    结构上复用 `_restricted_principal_arms` 使「管理权 ⊆ 读权」构造性成立;`everyone`
    被排除是深度防御(设计 §4:everyone 只能 viewer),不再依赖「发放口径永不写
    everyone+admin」这个外部前提。完整理由详见 SQLite 那一份。
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

    两个 ref 既可以是占位符 `%s`,也可以是外层查询的列引用。传占位符时消费
    **三个** user 参数,传列引用时不消费参数。
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

    两个 ref 既可以是占位符 `%s`,也可以是外层查询的列引用。参数消费与
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

    只有 `mount_sql.MOUNT_VALID_EXPR` 用它。参数消费同 `grant_access_expr`。
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
    """`everyone` 授权边的 `EXISTS (...)`——与**谁在问**无关,故不接 user_ref。"""
    return _grant_exists_expr(
        notebook_ref, grant_alias, _everyone_principal_arm(grant_alias)
    )


# 授权边探测:该用户在这个 notebook 上是否有一条有效授权边。参数用
# `grant_probe_params()` 展开。
GRANT_PROBE_SQL = (
    "SELECT 1 FROM notebook_grants ng WHERE ng.notebook_id=%s AND "
    + _principal_match_expr("ng", "%s", "ngm", "nga")
)

# 上面那条加行锁的变体,供三段式带锁写法使用。`OF ng` 是显式化而非语法必须(本语句
# 同层 rangetable 里只有 ng);组成员资格刻意不锁,取舍见模块 docstring。
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


def admin_access_clause(
    nb_alias: str = "nb",
    *,
    user_ref: str = "%s",
    grant_alias: str = "ng",
    group_alias: str = "ngm",
    group_admin_alias: str = "nga",
) -> str:
    """管理权谓词,作用在**已经 join 进来**的 notebooks 行上。

    = owner ∨ 管理级有效授权边(没有 `notebook_members` 那一支)。`user_ref` 默认是
    占位符,此时消费 `admin_access_params(user_id)`;传列引用则一个参数都不消费。
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

    消费 `read_access_params(user_id)`。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebooks {nb_alias} "
        f"WHERE {nb_alias}.id={row_alias}.notebook_id "
        f"AND {read_access_clause(nb_alias, member_alias, grant_alias='access_ng', group_alias='access_ngm', group_admin_alias='access_nga')})"
    )


# 读权/管理权片段消费几个位置参数——从谓词自己的占位符数**推导**而不是手写常量。
_READ_ACCESS_PARAM_COUNT = read_access_clause().count("%s")
_ADMIN_ACCESS_PARAM_COUNT = admin_access_clause().count("%s")
_GRANT_PROBE_USER_PARAM_COUNT = GRANT_PROBE_SQL.count("%s") - 1


def admin_access_params(user_id: str) -> tuple[str, ...]:
    """`admin_access_clause()` 要消费的位置参数(从占位符数推导,理由见 SQLite 那份)。"""
    return (user_id,) * _ADMIN_ACCESS_PARAM_COUNT


def read_access_params(user_id: str) -> tuple[str, ...]:
    """`read_access_clause()` / `read_access_exists_clause()` 要消费的位置参数。"""
    return (user_id,) * _READ_ACCESS_PARAM_COUNT


def grant_probe_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`GRANT_PROBE_SQL`(及 FOR SHARE 变体)要消费的位置参数。"""
    return (notebook_id,) + (user_id,) * _GRANT_PROBE_USER_PARAM_COUNT


# 写权(owner-only)的完整查询:有行即有写权。notebook 不存在 → 无行 → 无写权。
NOTEBOOK_WRITE_SQL = "SELECT 1 FROM notebooks WHERE id=%s AND created_by=%s"

# 管理权(owner ∪ 管理级有效授权边)的完整查询:有行即有管理权。
NOTEBOOK_ADMIN_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND " + admin_access_clause()
)

# 读权(owner ∪ 只读成员 ∪ 有效授权边)的完整查询:有行即有读权。
NOTEBOOK_READ_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND " + read_access_clause()
)
