"""notebook 授权(读权/写权)的 SQL 谓词 —— 「谁能读/写这个 notebook」的唯一定义点。

镜像 `mount_sql.py` 的模式。理由同款:授权判定散落在 sharing_store、memory_store、
search 三处,各自手写「owner ∨ 只读成员」的 EXISTS 子查询。副本越多,任何一份漂移
就越会造成「A 能读 B 不能读」的不一致,而这种不一致没有任何测试会自然抓到——它不
体现为报错,只体现为某条路径悄悄多给或少给了权限。故谓词只在这里定义一次。

两条谓词是**刻意不对称**的,这是产品的安全边界,不是疏漏:

* **写权 = owner-only**。`notebook_members` 里的成员是只读访客;放宽这一条会让只读
  共享变成可写共享。notebook 不存在时同样为假(无行 → False),与「不泄露存在性」
  的既有口径一致。
* **读权 = owner ∪ notebook_members 有行**。

参数约定(全部谓词都只消费位置参数,顺序即出现顺序):

* `NOTEBOOK_WRITE_SQL` —— `(notebook_id, user_id)`
* `NOTEBOOK_READ_SQL` —— `(notebook_id, user_id, user_id)`,后两个是同一个人:
  owner 分支比一次、成员分支再比一次。
* `MEMBER_PROBE_SQL` —— `(notebook_id, user_id)`
* `read_access_clause()` / `read_access_exists_clause()` —— 各消费两个参数
  `(user_id, user_id)`,理由同上。

消费者清单(改这里就要一起看):

* `sqlite/sharing_store.py`:`user_can_access_notebook`(写权)、`user_can_read_notebook`
  (读权)、`is_member`(成员探测)。
* `sqlite/memory_store.py`:`_read_access_clause`(嵌进 Memory 各处读查询)、
  `create_candidate_with_initial_revision`(写前判定)、
  `create_answer_with_initial_revision`(答案存 Memory 的范围校验;同名的
  `_answer_save_scope_locked_on` 是不含 SQL 的空接缝,谓词不在那儿)、
  `validate_promotion_approval_access_on`(晋升审批写事务内复核)。
* `sqlite/memory_store.py::_lock_memory_aggregate_on` —— ⚠ **两段式写法,刻意保留**:
  它先单查 `created_by` 以区分「notebook 不存在」(KeyError)与「不是成员」
  (PermissionError/KeyError),再单独探成员;PG 侧对应实现还在这两步上各挂 `FOR SHARE`
  行锁。合并成单条 EXISTS 会丢掉这个三态区分与 PG 的行锁语义。该处只复用
  `MEMBER_PROBE_SQL` 这一半,owner 那一半保持原样。群组授权(P1)扩展读权时,这里
  必须同步。

**刻意不收口**(核实过与「owner ∨ 成员」不同义,收进来就是把语义改了):

* `*/query_store.py::joined_notebook_rows` —— 「我加入了哪些笔记本」的**列表**查询,
  不是对某个 notebook 的授权判定:它刻意只要成员那一半(自有库由另一条查询给出,
  合进来会让自有库在「加入的」列表里重复出现),还多一个 `status != 'copying'` 过滤。
  群组扩展时它需不需要跟着列出群组库,是产品决策而非谓词一致性问题。
* `*/sharing_store.py` 的 `add_member` / `remove_member` / `kick_all_members` /
  `list_members` —— 成员关系的 CRUD,不是判定。

P1(群组知识共享)将在此扩展读权谓词:届时读权变为「owner ∪ 成员 ∪ 群组成员」,
只需改 `read_access_clause` 一处,上面全部消费者自动跟随。写权是否随之扩展是独立
决策,不要顺手一起改。

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

    两个 ref 既可以是占位符 `?`,也可以是外层查询的列引用(如
    `validate_promotion_approval_access_on` 用 `m.notebook_id` / `m.created_by`
    在 SELECT 列表里算 `is_member`)。传列引用时不消费参数。
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
        f"({nb_alias}.created_by=? OR "
        + member_exists_expr(f"{nb_alias}.id", "?", member_alias)
        + ")"
    )


def read_access_exists_clause(
    row_alias: str = "m",
    nb_alias: str = "access_nb",
    member_alias: str = "access_nm",
) -> str:
    """读权谓词的自包含形式:自己去 join notebooks。

    供「外层查的是别的表、只带了 `notebook_id` 列」的场景(Memory 各处读查询)使用。
    消费两个参数 `(user_id, user_id)`。
    """
    return (
        f"EXISTS (SELECT 1 FROM notebooks {nb_alias} "
        f"WHERE {nb_alias}.id={row_alias}.notebook_id "
        f"AND {read_access_clause(nb_alias, member_alias)})"
    )


# 写权(owner-only)的完整查询:有行即有写权。notebook 不存在 → 无行 → 无写权。
NOTEBOOK_WRITE_SQL = "SELECT 1 FROM notebooks WHERE id=? AND created_by=?"

# 读权(owner ∪ 只读成员)的完整查询:有行即有读权。
NOTEBOOK_READ_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=? AND " + read_access_clause()
)
