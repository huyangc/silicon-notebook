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
* `postgres/ask_state_store.py::guarded_ask_detail`:先锁 notebook root，再用
  `MEMBER_PROBE_FOR_SHARE_SQL` 或本文件的 direct/group grant-chain probe 锁住一条
  当前有效的 self-service 读权链，直到详情响应对象组装完。
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
  ⚠ **读级探测的组成员资格刻意不锁**,这是一条已登记的取舍而不是遗漏:`group_members`
  只在 EXISTS 子查询里,`FOR SHARE` 够不着它(要锁就得改写成 LEFT JOIN,而 PG 不允许对
  外连接的可空侧加锁)。后果是**一次在飞的写事务可以带着提交时已经失效的组授权
  落地**——用户在 t0 通过组授权拿到读权、t1 被移出组、他 t0 就开始的那个写事务在
  t2 提交成功。残留物是**一条被移除者自己也读不到的私有 Memory 行**(读路径当场
  为假),既不扩散也不可见,代价远小于为它把热路径改成带锁 join。

  ⚠⚠ **这条取舍只覆盖读级**(`GRANT_PROBE_FOR_SHARE_SQL`,消费者是上面那三个
  memory_store 站点)。**管理级已经收口**:`ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL` +
  `ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL` 用内连接把成员行提到顶层一并锁住(codex
  #519 R8 P1)。两者的判据是**爆炸半径**而不是技术难度:管理级探测的下游是
  `create_grant` / `approve_share_request`,它们落的是一条把整组读权发出去的**持久
  授权边**;读级那条的残留物只是一行谁也读不到的私有 Memory。别拿上面那段去论证
  管理级也可以不锁——它们不是同一件事。

**刻意不收口**(与 SQLite 侧同款,理由写在那份 docstring 里):
`postgres/query_store.py::joined_notebook_rows`(成员列表查询,只要成员那一半且多一个
`NOTEBOOK_LIVE_SQL` 过滤)、`postgres/sharing_store.py` 的成员关系 CRUD。

**双后端同修**:改本文件必须同改 `sqlite/access_sql.py`。
"""

# 「这行还不算存在」的可见性谓词单点(批 3·W1 T-1,摸底 5)。折的是 40 处读侧站点
# (`postgres/` 20 + `sqlite/` 20,逐行枚举见守卫测试),供裸列名或带别名前缀
# (如 `"nb." + NOTEBOOK_LIVE_SQL`)两种引用形式拼接。
# ⚠ 写侧 6 处 copying 哨兵(sharing_store.py 的 compensate_copy/sweep_stale_copies)
# 与生产者 1 处(notebook_sharing.py 的 `status="copying"` kwarg)绝不折进这里——
# 语义是「专指半拷贝去物理删掉它」/「置位」,和这条「还不算存在」的读侧谓词不同义。
# `deleting` 目前没有任何行会命中(批 3·W1 T-2 之前没有代码会写这个值),
# 所以本次折叠是纯粹的单点化,行为零变化。
NOTEBOOK_LIVE_SQL = "status NOT IN ('copying','deleting')"

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

# **管理级**授权边探测:该用户在这个 notebook 上是否有一条 `role='admin'` 的有效授权边。
# 与 `GRANT_PROBE_SQL` 同形,只把主体判定换成 `_admin_principal_match_expr`——**复用**
# 那一份而不是另抄一遍主体判定(唯一定义点红线)。参数用 `admin_grant_probe_params()`。
#
# ⚠ 为什么需要这条**顶层**查询,而不能给 `NOTEBOOK_ADMIN_SQL` 加锁了事:后者的形状是
# `SELECT 1 FROM notebooks nb WHERE nb.id=%s AND (nb.created_by=%s OR EXISTS(...))`,
# 授权边行藏在 EXISTS 子查询里,`FOR SHARE` 够不着它(只会锁 notebooks 那一行)——
# 与模块 docstring 里三段式带锁写法的理由逐字相同。审批共享申请时要锁住的恰恰是那条
# 授权边行(codex #519 R5)。
ADMIN_GRANT_PROBE_SQL = (
    "SELECT 1 FROM notebook_grants ng WHERE ng.notebook_id=%s AND "
    + _admin_principal_match_expr("ng", "%s", "ngm", "nga")
)

# ⚠ 这里**刻意没有** `ADMIN_GRANT_PROBE_FOR_SHARE_SQL`(裸探测 + `FOR SHARE OF ng`)。
# 它曾经存在(codex #519 R5),R8 P1 证明它只锁住了生效链的一端、留着就是给那个洞留一个
# 看起来正规的入口——凡是要在写事务里认管理权的地方,都必须用下面那两条**整链**加锁的
# 语句。裸的 `ADMIN_GRANT_PROBE_SQL`(不加锁,两侧都有)仍然保留:SQLite 侧靠进程写锁
# 串行,不需要也不存在行锁变体。


def _group_chain_join_condition(
    grant_alias: str,
    member_alias: str,
    user_ref: str,
) -> str:
    """`group` / `group_admins` 两条臂的**成员资格**,改写成可加锁的 JOIN 条件。

    与 `_restricted_principal_arms` 里那两条 `EXISTS (...)` 逐格等价,只换了一种
    PostgreSQL **锁得住**的写法:`FOR SHARE` 够不着 EXISTS 子查询里的行,内连接的
    两侧却都锁得住(外连接的可空侧不行,那正是模块 docstring 里那句「要锁就得改写成
    LEFT JOIN,而 PG 不允许」讲的另一半)。

    等价性不靠肉眼比对——`tests/postgres/test_admin_grant_chain_lock.py` 用数据驱动
    矩阵逐格比对「本条 + user 臂」与唯一定义点 `ADMIN_GRANT_PROBE_SQL` 的结果。
    """
    return (
        f"{member_alias}.group_id={grant_alias}.principal_id "
        f"AND {member_alias}.user_id={user_ref} "
        f"AND (({grant_alias}.principal_type='group') "
        f"OR ({grant_alias}.principal_type='group_admins' "
        f"AND {member_alias}.role='admin'))"
    )


# ---------------------------------------------------------------- 生效链加锁
#
# 管理级授权边的**生效链有两环**:①那条 `notebook_grants` 边还在;②让它生效的那条
# `group_members` 行还在。只锁 ①(codex #519 R5 当时的做法)等于只堵了一半——并发的
# 移出组/降级可以提交在探测快照之后、`create_grant` / `approve_share_request` 插入
# 持久边之前,于是一个管理权**刚刚被撤销**的人仍然发出了新的访问权(codex #519 R8 P1)。
#
# ⚠ 与模块 docstring 里「组成员资格刻意不锁」那条**已登记取舍**并不矛盾,两者的对象
# 不同,别读成互相推翻:那条说的是**读级**探测 `GRANT_PROBE_FOR_SHARE_SQL`(消费者是
# `memory_store` 的三段式热路径),它的残留物是「一条被移除者自己也读不到的私有
# Memory 行」,既不扩散也不可见;这里是**管理级**探测,残留物是一条把整组读权发出去
# 的持久授权边,爆炸半径完全不是一个量级。所以读级保持原样、管理级收口。
#
# 拆成两条语句是因为 `user` 臂的链只有一环(没有 `group_members` 行可言),而带锁的
# `UNION` 在 PostgreSQL 里是语法错误(`FOR UPDATE is not allowed with UNION`)。

# user 臂:主体就是这个人自己,链只有边行这一环,锁它即完整。
ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL = (
    "SELECT 1 FROM notebook_grants ng "
    "WHERE ng.notebook_id=%s AND ng.role='admin' "
    "AND ng.principal_type='user' AND ng.principal_id=%s "
    "FOR SHARE OF ng"
)

# group / group_admins 两臂:内连接把成员行提到顶层,`FOR SHARE OF ng, ngm` 在**同一条
# 语句**里把整条链锁住。原子性是要点:分成「先探测拿主体、再单独锁成员行」两步,两步
# 之间那条成员行可以被删掉,而此时是否还有**别的**链成立又要重新判断——单条语句没有
# 这个中间态,任一条链仍然成立就返回行、并且返回的正是被锁住的那条。
ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL = (
    "SELECT 1 FROM notebook_grants ng "
    "JOIN group_members ngm ON "
    + _group_chain_join_condition("ng", "ngm", "%s")
    + " WHERE ng.notebook_id=%s AND ng.role='admin' "
    "FOR SHARE OF ng, ngm"
)


def admin_grant_user_arm_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL` 的位置参数。"""
    return (notebook_id, user_id)


def admin_grant_group_chain_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL` 的位置参数。

    ⚠ 顺序**与形参相反**:JOIN ON 写在 WHERE 之前,所以 `user_id` 的占位符先出现。
    存在这个 helper 就是为了让调用方永远不必自己数占位符——手写 `(notebook_id,
    user_id)` 会把两个 id 对调,而两者都是不透明字符串,查询只会安静地返回空集
    (= 管理权判假),不会报错。
    """
    return (user_id, notebook_id)


# Ask admin-detail reads return answer/trace content, so their self-service
# authority must remain valid through response projection. These two probes
# lock one complete currently-valid grant chain. Direct-user/everyone grants
# have one row; group grants require both the edge and the membership row.
READ_GRANT_DIRECT_FOR_SHARE_SQL = (
    "SELECT 1 FROM notebook_grants ng "
    "WHERE ng.notebook_id=%s AND ("
    "(ng.principal_type='user' AND ng.principal_id=%s) "
    "OR ng.principal_type='everyone') "
    'ORDER BY ng.id COLLATE "C" LIMIT 1 FOR SHARE OF ng'
)

READ_GRANT_GROUP_CHAIN_FOR_SHARE_SQL = (
    "SELECT 1 FROM notebook_grants ng "
    "JOIN group_members ngm ON ngm.group_id=ng.principal_id "
    "AND ngm.user_id=%s "
    "WHERE ng.notebook_id=%s AND (ng.principal_type='group' OR "
    "(ng.principal_type='group_admins' AND ngm.role='admin')) "
    'ORDER BY ng.id COLLATE "C", ngm.group_id COLLATE "C" '
    "LIMIT 1 FOR SHARE OF ng, ngm"
)


def read_grant_direct_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    return (notebook_id, user_id)


def read_grant_group_chain_params(
    notebook_id: str, user_id: str
) -> tuple[str, ...]:
    return (user_id, notebook_id)


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
_ADMIN_GRANT_PROBE_USER_PARAM_COUNT = ADMIN_GRANT_PROBE_SQL.count("%s") - 1


def admin_access_params(user_id: str) -> tuple[str, ...]:
    """`admin_access_clause()` 要消费的位置参数(从占位符数推导,理由见 SQLite 那份)。"""
    return (user_id,) * _ADMIN_ACCESS_PARAM_COUNT


def read_access_params(user_id: str) -> tuple[str, ...]:
    """`read_access_clause()` / `read_access_exists_clause()` 要消费的位置参数。"""
    return (user_id,) * _READ_ACCESS_PARAM_COUNT


def grant_probe_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`GRANT_PROBE_SQL`(及 FOR SHARE 变体)要消费的位置参数。"""
    return (notebook_id,) + (user_id,) * _GRANT_PROBE_USER_PARAM_COUNT


def admin_grant_probe_params(notebook_id: str, user_id: str) -> tuple[str, ...]:
    """`ADMIN_GRANT_PROBE_SQL`(及 FOR SHARE 变体)要消费的位置参数。

    单独一份而不是复用 `grant_probe_params`:两条查询的主体判定不同(管理级排除了
    `everyone` 那一臂),占位符数是各自**推导**出来的——今天恰好相等,明天任一侧的臂
    变了就会分叉,而共用一份参数展开会静默错位。
    """
    return (notebook_id,) + (user_id,) * _ADMIN_GRANT_PROBE_USER_PARAM_COUNT


# 写权(owner-only)的完整查询:有行即有写权。notebook 不存在 → 无行 → 无写权。
# ⚠ 直连资源端点(/sources/{id}、/elements 等)靠这三条谓词授权,不经过
# get_notebook 的目录寻址闸(codex #653 R2)——「deleting 后入口已 404」只对目录寻址
# 成立,这三条必须自己把生命周期挡住,否则半拷贝/删除中的库仍能被直连端点读写。
# 单点引用 NOTEBOOK_LIVE_SQL(批 3·W1 T-1),不折进 read_access_clause()/
# admin_access_clause() 内部——那两个函数还喂 Memory 读查询、group_store 列表投影等
# 更大范围的消费者,折进去会把改动面扩大到未经审视的地方(取舍与逐消费者排查
# 见规格 T-1「授权谓词并入」小节)。
NOTEBOOK_WRITE_SQL = (
    f"SELECT 1 FROM notebooks WHERE id=%s AND created_by=%s AND {NOTEBOOK_LIVE_SQL}"
)

# 管理权(owner ∪ 管理级有效授权边)的完整查询:有行即有管理权。
NOTEBOOK_ADMIN_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND " + admin_access_clause()
    + f" AND nb.{NOTEBOOK_LIVE_SQL}"
)

# 读权(owner ∪ 只读成员 ∪ 有效授权边)的完整查询:有行即有读权。
NOTEBOOK_READ_SQL = (
    "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND " + read_access_clause()
    + f" AND nb.{NOTEBOOK_LIVE_SQL}"
)
