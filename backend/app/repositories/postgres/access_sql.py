"""notebook 授权(读权/写权)的 SQL 谓词 —— 「谁能读/写这个 notebook」的唯一定义点。

`sqlite/access_sql.py` 的 PostgreSQL 镜像(占位符 `%s`)。完整理由、参数约定与
P1 群组授权的扩展点写在 SQLite 那一份的模块 docstring 里,两份必须同修;这里只登记
PG 侧独有的事实。

两条谓词同样刻意不对称:写权 = owner-only(只读成员不得写),读权 = owner ∪
`notebook_members` 有行。

消费者清单(改这里就要一起看):

* `postgres/sharing_store.py`:`user_can_access_notebook`(写权)、
  `user_can_read_notebook`(读权)、`is_member`(成员探测)。
* `postgres/memory_store.py`:`_read_access_clause`、`_answer_save_scope_exists`、
  `validate_promotion_approval_access_on`。
* `postgres/search.py::memory_lexical_candidates` —— Memory 词法候选的读权过滤,
  与 `_read_access_clause` 同形,是本次收口前的第三份独立复刻。
* ⚠ **两段式带锁写法,刻意保留**(`postgres/memory_store.py` 的
  `create_candidate_with_initial_revision`、答案存 Memory 的写事务分支、
  `_lock_memory_aggregate_on`):它们先 `SELECT created_by FROM notebooks ... FOR SHARE`
  锁住 notebooks 行,再单独 `SELECT 1 FROM notebook_members ... FOR SHARE` 锁成员行。
  合并成单条 EXISTS 会丢掉行锁(EXISTS 子查询里的行拿不到 `FOR SHARE`),也会丢掉
  「notebook 不存在」与「不是成员」的三态区分。这三处只复用
  `MEMBER_PROBE_FOR_SHARE_SQL` 这一半,owner 那一半保持原样。群组授权(P1)扩展
  读权时,这三处必须同步。

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


def read_access_clause(nb_alias: str = "nb", member_alias: str = "nm") -> str:
    """读权谓词,作用在**已经 join 进来**的 notebooks 行上。

    消费两个参数 `(user_id, user_id)`。
    """
    return (
        f"({nb_alias}.created_by=%s OR "
        + member_exists_expr(f"{nb_alias}.id", "%s", member_alias)
        + ")"
    )


def read_access_exists_clause(
    row_alias: str = "m",
    nb_alias: str = "access_nb",
    member_alias: str = "access_nm",
) -> str:
    """读权谓词的自包含形式:自己去 join notebooks。

    消费两个参数 `(user_id, user_id)`。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebooks {nb_alias} "
        f"WHERE {nb_alias}.id={row_alias}.notebook_id "
        f"AND {read_access_clause(nb_alias, member_alias)})"
    )


# 写权(owner-only)的完整查询:有行即有写权。notebook 不存在 → 无行 → 无写权。
NOTEBOOK_WRITE_SQL = "SELECT 1 FROM notebooks WHERE id=%s AND created_by=%s"

# 读权(owner ∪ 只读成员)的完整查询:有行即有读权。
NOTEBOOK_READ_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND " + read_access_clause()
)
