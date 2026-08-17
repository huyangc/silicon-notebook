# backend/tests/test_access_sql_contract.py
"""notebook 授权谓词唯一定义点(`repositories/*/access_sql.py`)的行为契约。

P0-T1 把散落在 sharing_store / memory_store / search 的「owner ∨ 只读成员」手写复刻
收进唯一定义点;P1-T2 在同一处把读权扩成「owner ∪ 只读成员 ∪ 有效授权边」。这份
矩阵钉住的是**哪一格该翻、哪一格绝不许翻**:

* 写权恒为 owner-only —— 只读成员与群组被授权者都是**访客**,读权扩了这一列不得
  跟着松(组管理员的写权是 P2 的能力翻转)。
* 不存在的 notebook 两权皆否(无行 → False),不抛异常、不泄露存在性。
* `legacy_read` 一列是 P1 之前的口径(`写权 or is_member`)。它同时钉两件事:老
  主体上新旧口径必须逐格相同(读权没有顺手放宽既有语义),而授权边主体上必须**恰好**
  是它翻了——只断言新口径的话,一个「把所有人都放行」的实现同样能全绿。
* `principal_type` 按四值白名单精确匹配:正向 shadow 停车会给冲突行写哨兵
  `principal_type`,这类行必须谁也匹配不上(裁决 1b)。哨兵用例覆盖三种最可能被
  写出来的推断形态(`principal_id=''`、`principal_id=<某用户>`、`=<某组>`)。

另钉住 service 层 `user_can_read_notebook` 与 store 新方法结果一致:重构把 service
从「写权 or 成员」两次查询改成一跳委托 store 单条查询,两者必须逐格同义。
"""
import re
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import access_sql
from app.services.sqlite_repository import SQLiteRepository, _now


MISSING_NOTEBOOK = "nb-does-not-exist"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _mk_user(repo, uid: str) -> str:
    # users 表有 NOT NULL 无默认列;漏列会静默吞掉整行,后续 notebook_members 的 FK 失败。
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users "
            "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, f"{uid}@t", uid, uid, "x", "user", _now(), _now()),
        )
    return uid


def _mk_nb(repo, owner: str) -> str:
    nb = f"nb-{uuid.uuid4().hex[:10]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (nb, "NB", "", "Semiconductor", "draft", owner, _now(), _now()),
        )
    return nb


def _mk_group(repo, group_id: str, owner: str) -> str:
    with repo._write() as db:
        db.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (group_id, group_id, "project", "", owner, _now(), _now()),
        )
    return group_id


def _add_group_member(repo, group_id: str, user_id: str, role: str = "member") -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES (?,?,?,?,?)",
            (group_id, user_id, role, _now(), user_id),
        )


def _mk_grant(
    repo, notebook_id: str, principal_type: str, principal_id: str, role: str = "viewer"
) -> str:
    grant_id = f"grant-{uuid.uuid4().hex[:10]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (grant_id, notebook_id, principal_type, principal_id, role, "user-owner", _now()),
        )
    return grant_id


# 正向 shadow 在 UNIQUE 冲突时给 principal_type 暂写的哨兵形状(值本身不重要,重要
# 的是它不在四值白名单里)。
PARKED_PRINCIPAL_TYPE = "__shadow_parked__"


@pytest.fixture
def world(repo):
    """P0 的 owner/成员/陌生人,加上 P1 的四类授权边主体。

    群组刻意分成两个:`grp-viewers` 走 `principal_type='group'`(整组可读),
    `grp-admins` 走 `principal_type='group_admins'`(只有 role='admin' 可读)。
    `group_plain` 是后者的**普通**成员——它必须读不到,那正是两种主体类型的全部
    区别所在,合成一个组就测不出来了。
    """
    owner = _mk_user(repo, "user-owner")
    member = _mk_user(repo, "user-member")
    stranger = _mk_user(repo, "user-stranger")
    grantee = _mk_user(repo, "user-grantee")
    group_member = _mk_user(repo, "user-group-member")
    group_admin = _mk_user(repo, "user-group-admin")
    group_plain = _mk_user(repo, "user-group-plain")

    viewers = _mk_group(repo, "grp-viewers", owner)
    admins = _mk_group(repo, "grp-admins", owner)
    _add_group_member(repo, viewers, group_member, "member")
    _add_group_member(repo, admins, group_admin, "admin")
    _add_group_member(repo, admins, group_plain, "member")

    notebook = _mk_nb(repo, owner=owner)
    repo.add_member(notebook, member)
    _mk_grant(repo, notebook, "user", grantee)
    _mk_grant(repo, notebook, "group", viewers)
    _mk_grant(repo, notebook, "group_admins", admins, role="admin")

    everyone_nb = _mk_nb(repo, owner=owner)
    _mk_grant(repo, everyone_nb, "everyone", "")

    # 哨兵库:三行停车中的授权边,分别长得像「everyone」「点名 stranger」「点名
    # grp-viewers」。四值精确匹配下它们一行都不生效。
    sentinel_nb = _mk_nb(repo, owner=owner)
    _mk_grant(repo, sentinel_nb, PARKED_PRINCIPAL_TYPE, "")
    _mk_grant(repo, sentinel_nb, PARKED_PRINCIPAL_TYPE, stranger)
    _mk_grant(repo, sentinel_nb, PARKED_PRINCIPAL_TYPE, viewers)

    return {
        "repo": repo,
        "owner": owner,
        "member": member,
        "stranger": stranger,
        "grantee": grantee,
        "group_member": group_member,
        "group_admin": group_admin,
        "group_plain": group_plain,
        "viewers": viewers,
        "admins": admins,
        "notebook": notebook,
        "everyone": everyone_nb,
        "sentinel": sentinel_nb,
        "missing": MISSING_NOTEBOOK,
    }


# (主体键, notebook 键, 期望读权, 期望写权, P1 之前的旧读权口径)
ACCESS_MATRIX = [
    ("owner", "notebook", True, True, True),
    ("member", "notebook", True, False, True),       # 只读成员:能读,绝不能写
    ("stranger", "notebook", False, False, False),
    # ↓ P1 翻的正是这三格,且只有这三格。
    ("grantee", "notebook", True, False, False),     # principal_type='user'
    ("group_member", "notebook", True, False, False),   # principal_type='group'
    ("group_admin", "notebook", True, False, False),    # principal_type='group_admins'
    # 授权给「组管理员」的库,组里的普通成员读不到(除非另有 group 行)。
    ("group_plain", "notebook", False, False, False),
    # everyone 授权:任何登录用户都能读,但一个字的写权都不给。
    ("owner", "everyone", True, True, True),
    ("stranger", "everyone", True, False, False),
    ("group_plain", "everyone", True, False, False),
    # 哨兵停车行:谁也匹配不上,owner 仍按 owner 分支照常可读。
    ("owner", "sentinel", True, True, True),
    ("stranger", "sentinel", False, False, False),
    ("grantee", "sentinel", False, False, False),
    ("group_member", "sentinel", False, False, False),
    ("owner", "missing", False, False, False),       # 不存在的 notebook:两权皆否
    ("member", "missing", False, False, False),
    ("stranger", "missing", False, False, False),
    ("grantee", "missing", False, False, False),
]

# 「片段嵌进更大的查询」类测试统一用这批主体:每种授权路径至少一个,外加一个必须
# 读不到的。只拿 owner/成员/陌生人跑的话,片段少接一条授权臂也照样全绿。
_EMBED_SUBJECTS = (
    "owner",
    "member",
    "grantee",
    "group_member",
    "group_admin",
    "group_plain",
    "stranger",
)


@pytest.mark.parametrize(
    "subject,target,expect_read,expect_write,legacy_read",
    ACCESS_MATRIX,
    ids=[f"{s}-{t}" for s, t, _r, _w, _l in ACCESS_MATRIX],
)
def test_read_and_write_predicates_match_the_pre_refactor_matrix(
    world, subject, target, expect_read, expect_write, legacy_read
):
    del legacy_read
    repo = world["repo"]
    user_id = world[subject]
    notebook_id = world[target]

    assert repo.user_can_read_notebook(notebook_id, user_id) is expect_read
    assert repo.user_can_access_notebook(notebook_id, user_id) is expect_write


def test_write_predicate_stays_owner_only_for_every_member(world):
    """写权是安全边界:凡不是 owner,一律没有写权——读权扩到群组后这条不得松。"""
    repo, notebook = world["repo"], world["notebook"]
    assert repo.user_can_access_notebook(notebook, world["owner"]) is True
    for key in ("member", "stranger", "grantee", "group_member", "group_admin"):
        non_owner = world[key]
        assert repo.is_member(notebook, non_owner) == (key == "member")
        assert repo.user_can_access_notebook(notebook, non_owner) is False
    # everyone 授权同样一个字的写权都不给。
    assert repo.user_can_access_notebook(world["everyone"], world["stranger"]) is False


@pytest.mark.parametrize(
    "subject,target,expect_read,expect_write,legacy_read",
    ACCESS_MATRIX,
    ids=[f"{s}-{t}" for s, t, _r, _w, _l in ACCESS_MATRIX],
)
def test_service_read_predicate_agrees_with_the_store_predicate(
    world, subject, target, expect_read, expect_write, legacy_read
):
    """service 一跳委托 store 之后,两层必须逐格同义。

    另钉住 P1 的读权扩展**恰好**发生在授权边那几格:旧口径(写权 or 成员)在老主体
    上必须与新谓词逐格相同,只有授权边主体才允许出现「新真旧假」。反过来任何一格
    「新假旧真」都是收窄了既有权限,一律不许。
    """
    del expect_write
    repo = world["repo"]
    user_id = world[subject]
    notebook_id = world[target]
    store = repo._runtime.sharing_store

    store_result = store.user_can_read_notebook(notebook_id, user_id)
    assert store_result is expect_read
    assert repo.user_can_read_notebook(notebook_id, user_id) is store_result
    legacy = store.user_can_access_notebook(notebook_id, user_id) or store.is_member(
        notebook_id, user_id
    )
    assert legacy is legacy_read
    assert not (legacy and not store_result), "读权只许扩,不许收窄既有主体"


def test_predicate_follows_membership_changes(world):
    """读权是实时判定而非一次性授予:踢掉成员即刻失读权,写权全程为假。"""
    repo, notebook, member = world["repo"], world["notebook"], world["member"]
    assert repo.user_can_read_notebook(notebook, member) is True
    repo.remove_member(notebook, member)
    assert repo.user_can_read_notebook(notebook, member) is False
    assert repo.user_can_access_notebook(notebook, member) is False
    repo.add_member(notebook, member)
    assert repo.user_can_read_notebook(notebook, member) is True


def _delete_rows(repo, sql: str, params: tuple) -> None:
    with repo._write() as db:
        db.execute(sql, params)


def test_group_membership_changes_take_effect_immediately(world):
    """踢出组成员即刻失读权,加回即刻恢复——授权边同样是实时判定。"""
    repo, notebook = world["repo"], world["notebook"]
    group_member, viewers = world["group_member"], world["viewers"]
    assert repo.user_can_read_notebook(notebook, group_member) is True
    _delete_rows(
        repo,
        "DELETE FROM group_members WHERE group_id=? AND user_id=?",
        (viewers, group_member),
    )
    assert repo.user_can_read_notebook(notebook, group_member) is False
    _add_group_member(repo, viewers, group_member)
    assert repo.user_can_read_notebook(notebook, group_member) is True


def test_demoting_a_group_admin_revokes_a_group_admins_grant(world):
    """`group_admins` 授权只认 role='admin':降级成普通成员即刻失读权。"""
    repo, notebook = world["repo"], world["notebook"]
    group_admin, admins = world["group_admin"], world["admins"]
    assert repo.user_can_read_notebook(notebook, group_admin) is True
    with repo._write() as db:
        db.execute(
            "UPDATE group_members SET role='member' WHERE group_id=? AND user_id=?",
            (admins, group_admin),
        )
    assert repo.user_can_read_notebook(notebook, group_admin) is False


def test_deleting_the_grant_edge_revokes_access(world):
    """删授权边即刻失读权(三种主体各一条)。"""
    repo, notebook = world["repo"], world["notebook"]
    for subject, principal_type in (
        ("grantee", "user"),
        ("group_member", "group"),
        ("group_admin", "group_admins"),
    ):
        user_id = world[subject]
        assert repo.user_can_read_notebook(notebook, user_id) is True
        _delete_rows(
            repo,
            "DELETE FROM notebook_grants WHERE notebook_id=? AND principal_type=?",
            (notebook, principal_type),
        )
        assert repo.user_can_read_notebook(notebook, user_id) is False


def test_deleting_the_group_cascades_membership_and_revokes_access(world):
    """删组:`group_members` 经 ON DELETE CASCADE 消失,组授权当场不生效。

    授权边行本身仍在(`principal_id` 无 FK,清理是 T3 删组事务的事),但谓词 join
    不到成员,所以「孤儿授权边」不会留下一条越权通道。
    """
    repo, notebook = world["repo"], world["notebook"]
    group_member = world["group_member"]
    assert repo.user_can_read_notebook(notebook, group_member) is True
    with repo._write() as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("DELETE FROM groups WHERE id=?", (world["viewers"],))
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebook_grants "
            "WHERE notebook_id=? AND principal_type='group'",
            (notebook,),
        ).fetchone()["c"]
    assert remaining == 1, "授权边行刻意保留:这条测的是谓词侧的 fail-safe"
    assert repo.user_can_read_notebook(notebook, group_member) is False


def test_parked_sentinel_grants_match_nobody(world):
    """哨兵停车行必须 fail-safe —— 裁决 1b 的行为面。

    三行 `principal_type` 都不在四值白名单里,`principal_id` 分别长得像 everyone
    (`''`)、像点名某用户、像点名某组。任何从 `principal_id` 推断主体、或写了
    `NOT IN`/else 兜底分支的实现,都会在这里放行至少一个人。
    """
    repo, sentinel = world["repo"], world["sentinel"]
    for key in ("stranger", "grantee", "group_member", "group_admin", "group_plain", "member"):
        assert repo.user_can_read_notebook(sentinel, world[key]) is False, key
    assert repo.user_can_read_notebook(sentinel, world["owner"]) is True


def test_read_clause_embeds_into_a_larger_query_with_identical_results(world):
    """可嵌片段与完整查询必须给出同一答案 —— 二者是同一谓词的两种取用形式。

    memory_store / search 用的是片段形式(嵌进更大的查询),sharing_store 用的是完整
    查询形式。若两者漂移,Memory 能读到的与「我能读这个 notebook 吗」就会不一致。
    """
    repo, notebook = world["repo"], world["notebook"]
    clause = access_sql.read_access_clause()
    embedded = (
        "SELECT nb.id FROM notebooks nb WHERE nb.id=? AND " + clause
    )
    for subject in _EMBED_SUBJECTS:
        user_id = world[subject]
        with repo._connect() as db:
            row = db.execute(
                embedded, (notebook, *access_sql.read_access_params(user_id))
            ).fetchone()
        assert (row is not None) is repo.user_can_read_notebook(notebook, user_id)


def test_exists_form_agrees_with_the_joined_form(world):
    """自包含 EXISTS 形式(外层只带 notebook_id 列)与 join 形式必须同义。"""
    repo, notebook = world["repo"], world["notebook"]
    # 借 sources 表当「外层只带 notebook_id 的行」:与 Memory 各处读查询同形。
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,file_name,file_path,file_size,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("src-access", notebook, "S", "document", "s.md", "", 0, _now(), _now()),
        )
    embedded = (
        "SELECT s.id FROM sources s WHERE s.id=? AND "
        + access_sql.read_access_exists_clause("s")
    )
    for subject in _EMBED_SUBJECTS:
        user_id = world[subject]
        with repo._connect() as db:
            row = db.execute(
                embedded, ("src-access", *access_sql.read_access_params(user_id))
            ).fetchone()
        assert (row is not None) is repo.user_can_read_notebook(notebook, user_id)


def test_member_probe_matches_is_member(world):
    """两段式带锁写法复用的成员探测常量,必须与 `is_member` 同义。"""
    repo, notebook = world["repo"], world["notebook"]
    for subject in _EMBED_SUBJECTS:
        user_id = world[subject]
        with repo._connect() as db:
            row = db.execute(
                access_sql.MEMBER_PROBE_SQL, (notebook, user_id)
            ).fetchone()
        assert (row is not None) is repo.is_member(notebook, user_id)


def test_grant_probe_matches_the_grant_half_of_the_read_predicate(world):
    """三段式带锁写法复用的授权边探测常量,必须与读权谓词的授权边那一半同义。

    「那一半」= 完整读权减去 owner 与成员两支;所以这里拿 owner/成员之外的主体比。
    两条 SQL 由同一个 `_principal_match_expr` 拼出,这条测的是它们在真数据上给出
    同一答案(以及参数元组的形状对得上)。
    """
    repo = world["repo"]
    for target in ("notebook", "everyone", "sentinel", "missing"):
        notebook_id = world[target]
        for subject in ("grantee", "group_member", "group_admin", "group_plain", "stranger"):
            user_id = world[subject]
            with repo._connect() as db:
                row = db.execute(
                    access_sql.GRANT_PROBE_SQL,
                    access_sql.grant_probe_params(notebook_id, user_id),
                ).fetchone()
            expected = repo.user_can_read_notebook(notebook_id, user_id)
            assert (row is not None) is expected, (target, subject)


def test_read_access_param_helper_matches_the_predicate_placeholders():
    """参数元组由谓词自己的占位符数推导,双后端必须一致。

    手数占位符是这次扩展最大的机械风险面(每个 user 占位符从 2 个变 5 个)。这条把
    「数对了」变成一个不可能失守的推导:片段再长一条臂,helper 自动跟着长。
    """
    from app.repositories.postgres import access_sql as pg

    assert len(access_sql.read_access_params("u")) == access_sql.read_access_clause().count("?")
    assert len(pg.read_access_params("u")) == pg.read_access_clause().count("%s")
    assert len(access_sql.read_access_params("u")) == len(pg.read_access_params("u"))
    assert set(access_sql.read_access_params("u")) == {"u"}

    assert len(access_sql.grant_probe_params("n", "u")) == access_sql.GRANT_PROBE_SQL.count("?")
    assert len(pg.grant_probe_params("n", "u")) == pg.GRANT_PROBE_SQL.count("%s")
    assert access_sql.grant_probe_params("n", "u")[0] == "n"
    assert set(access_sql.grant_probe_params("n", "u")[1:]) == {"u"}


# ---------------------------------------------------------------------------
# 双后端同修守卫
# ---------------------------------------------------------------------------
#
# 手写逐符号断言挡得住「改了同一个符号」,挡不住「只在一侧**新增**符号」——而 P1
# 群组授权要做的恰是后者(grants/group_members 的新片段)。故改为模块自省驱动:
# 先断言两侧 public 符号集合相等,再对交集逐项 normalized 比对,新增符号自动进入
# 比对范围,单侧新增当场报红。

# 单侧独有符号的显式豁免名单:每一项都要写清楚为什么只该在一侧存在。
_PG_ONLY_SYMBOLS = {
    # 三段式带锁写法的加锁变体;SQLite 没有行锁概念,不应有对应物。
    "MEMBER_PROBE_FOR_SHARE_SQL",
    "GRANT_PROBE_FOR_SHARE_SQL",
}
_SQLITE_ONLY_SYMBOLS: set[str] = set()

# 可调用符号的比对探针:新增可调用符号必须在此登记调用参数,否则下面的守卫响亮
# 失败(逼着登记,而不是静默跳过比对)。探针一律返回字符串——返回别的类型的符号
# (如 `read_access_params` 的元组)在这里 repr 成字符串再比,形状差一位就报红。
_CALLABLE_PROBES = {
    "member_exists_expr": lambda mod: mod.member_exists_expr(
        "outer.notebook_id", "outer.user_id"
    ),
    "grant_access_expr": lambda mod: mod.grant_access_expr(
        "outer.notebook_id", "outer.user_id"
    ),
    "read_access_clause": lambda mod: mod.read_access_clause(),
    "read_access_exists_clause": lambda mod: mod.read_access_exists_clause("m"),
    "read_access_params": lambda mod: repr(mod.read_access_params("U")),
    "grant_probe_params": lambda mod: repr(mod.grant_probe_params("N", "U")),
}


def _public_symbols(mod) -> dict[str, tuple[str, object]]:
    """模块的 public 面:str 常量 + 本模块定义的可调用。"""
    symbols: dict[str, tuple[str, object]] = {}
    for name in dir(mod):
        if name.startswith("_"):
            continue
        value = getattr(mod, name)
        if isinstance(value, str):
            symbols[name] = ("const", value)
        elif callable(value) and getattr(value, "__module__", None) == mod.__name__:
            symbols[name] = ("callable", value)
    return symbols


def _probe_value(mod, name: str, kind: str, value) -> str:
    if kind == "const":
        return value
    assert name in _CALLABLE_PROBES, (
        f"可调用符号 {name} 未在 _CALLABLE_PROBES 登记比对参数——"
        "新增片段函数时必须同时登记,双后端同修守卫才看得见它"
    )
    return _CALLABLE_PROBES[name](mod)


def test_backends_declare_the_same_predicate_surface():
    """双后端同修:符号集合相等,交集逐项只差占位符。

    群组授权会在这两份文件里扩展同一条读权谓词;一侧漏改(改了同一符号)或漏加
    (只在一侧新增符号)都是「PostgreSQL 部署的权限与 SQLite 部署不同」,而这种
    分叉在单后端的测试里永远看不见。
    """
    from app.repositories.postgres import access_sql as pg

    sqlite_syms = _public_symbols(access_sql)
    pg_syms = _public_symbols(pg)

    assert set(sqlite_syms) - _SQLITE_ONLY_SYMBOLS == set(pg_syms) - _PG_ONLY_SYMBOLS, (
        "两份 access_sql 的 public 符号集合不等——单侧新增/删除了符号。"
        "要么补齐另一侧,要么在豁免名单里写明为什么只该一侧有。"
    )

    for name in sorted(set(sqlite_syms) & set(pg_syms)):
        s_kind, s_raw = sqlite_syms[name]
        p_kind, p_raw = pg_syms[name]
        assert s_kind == p_kind, f"{name} 在两侧的形态不同({s_kind} vs {p_kind})"
        s_val = _probe_value(access_sql, name, s_kind, s_raw)
        p_val = _probe_value(pg, name, p_kind, p_raw)
        assert p_val.replace("%s", "?") == s_val, name

    # PG 独有:三段式带锁写法用的加锁变体,只应比裸探测多一个 FOR SHARE 后缀。
    # 授权边那条必须写 `OF ng`:被 EXISTS 子查询引用的 group_members 不在 FROM
    # 列表里,裸 FOR SHARE 锁不到它,写成裸的只会让人误以为它锁了。
    assert pg.MEMBER_PROBE_FOR_SHARE_SQL == pg.MEMBER_PROBE_SQL + " FOR SHARE"
    assert pg.GRANT_PROBE_FOR_SHARE_SQL == pg.GRANT_PROBE_SQL + " FOR SHARE OF ng"


def test_placeholder_styles_are_not_cross_contaminated():
    """PG 串不得含 `?`、SQLite 串不得含 `%s`。

    normalized 比对是单向替换(`%s`→`?`),分不出「PG 用 %s」与「PG 被整份复制成
    SQLite 版」——后者会让 psycopg 在每次授权判定上抛语法错误。方向性断言把这类
    复制粘贴当场拦下。
    """
    from app.repositories.postgres import access_sql as pg

    for name, (kind, raw) in sorted(_public_symbols(access_sql).items()):
        assert "%s" not in _probe_value(access_sql, name, kind, raw), (
            f"sqlite/access_sql.py::{name} 含 %s 占位符"
        )
    for name, (kind, raw) in sorted(_public_symbols(pg).items()):
        assert "?" not in _probe_value(pg, name, kind, raw), (
            f"postgres/access_sql.py::{name} 含 ? 占位符"
        )


# ---------------------------------------------------------------------------
# 结构性守卫:谓词不许离开唯一定义点
# ---------------------------------------------------------------------------

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"


def _collapsed(text: str) -> str:
    """去掉引号与全部空白:让跨行字符串拼接与源码换行都现出连续的 SQL 形状。"""
    return re.sub(r"[\s'\"]+", "", text)


def _scan_for_shape(pattern: re.Pattern[str]) -> list[str]:
    """全仓扫描某个 SQL 形状,排除唯一定义点自身。"""
    offenders = []
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        if path.name == "access_sql.py":
            continue
        if pattern.search(_collapsed(path.read_text(encoding="utf-8"))):
            offenders.append(str(path.relative_to(_BACKEND_APP)))
    return offenders


def test_owner_or_member_shape_lives_only_in_access_sql():
    """「owner ∨ 成员」的内联形状只许出现在 access_sql.py。

    唯一定义点的价值在于没有第二份:重新手写一份逐字相同的复刻,今天语义相同,
    P1 扩展群组授权那天就是一条静默分叉的授权路径。这条移动变异守卫保证「把定义
    搬回消费点」会报红,而不是靠 docstring 清单的自觉。
    """
    pattern = re.compile(r"created_by=(\?|%s)OREXISTS\(SELECT1FROMnotebook_members")
    offenders = _scan_for_shape(pattern)
    assert offenders == [], (
        f"授权谓词形状出现在唯一定义点之外:{offenders}。"
        "请改用 access_sql 的片段函数,不要手写内联复刻。"
    )


def test_grant_principal_shape_lives_only_in_access_sql():
    """授权边主体判定的内联形状同样只许出现在 access_sql.py。

    比 owner∨成员 那条更要紧:四值白名单里任何一条臂被复刻出去,今天语义相同,
    某天有人只在一份里加了「everyone 也看 principal_id」或漏掉 role='admin',就是
    一条静默多给权限的授权路径。这条钉的是形状(`principal_type='...' AND EXISTS
    (SELECT 1 FROM group_members`),T3 的授权边 CRUD 不含它,不会被误伤。
    """
    # `_collapsed` 连引号一起去掉,所以形状里的 'group' 写成裸 group。
    pattern = re.compile(
        r"principal_type=group(_admins)?ANDEXISTS\(SELECT1FROMgroup_members"
    )
    offenders = _scan_for_shape(pattern)
    assert offenders == [], (
        f"授权边主体判定出现在唯一定义点之外:{offenders}。"
        "请改用 access_sql.grant_access_expr / GRANT_PROBE_SQL。"
    )


def test_everyone_is_never_inferred_from_principal_id():
    """`everyone` 只按 `principal_type` 判(裁决 1b)——全仓不许出现按
    `principal_id` 推断主体的形状。

    行为面已由哨兵用例覆盖;这条是**静态**面,而且**不豁免 access_sql.py 自己**:
    一个只在某个未被矩阵覆盖的分支里写了 `principal_id IS NULL` / `principal_id=''`
    的实现,行为测试可能恰好照不到。判据要求两个 token **紧邻**,所以散文里说明
    「`IS NULL` / `=''` 都不行」的 docstring 不会被误伤。
    """
    pattern = re.compile(r"principal_id\s*(IS\s+(NOT\s+)?NULL|=\s*'')")
    offenders = []
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(_BACKEND_APP)))
    assert offenders == [], (
        f"按 principal_id 推断主体的形状:{offenders}。"
        "principal_id 是 NOT NULL DEFAULT '' 的裸列,'' 是它的默认值而不是 "
        "everyone 的标记;正向 shadow 的停车行也靠四值精确匹配才 fail-safe。"
    )


def test_two_step_locked_sites_stay_pinned():
    """分段式带锁/三态站点的 allowlist:数量一变就必须回来更新这里与 docstring。

    这些站点手写 owner 那一半(`SELECT created_by`),另外两半各自复用唯一定义点的
    探测常量——`read_access_clause` 里扩了什么,它们**不会自动跟随**,必须逐处手改
    (P1 群组授权就是这么加上第三段授权边探测的)。这条守卫在「有人新增/删除/搬动
    这类站点」时逼人回来看清单,而不是让新站点静默游离在扩展范围之外。

    三个计数缺一不可:owner 半的条数说明有几个站点,`MEMBER_PROBE` 与 `GRANT_PROBE`
    的条数说明每个站点是不是三段都齐了。只数 owner 半的话,某个站点漏接授权边探测
    (= 被授权者在写事务里被拒)照样全绿。
    """
    pg_store = _collapsed(
        (_BACKEND_APP / "repositories/postgres/memory_store.py").read_text(
            encoding="utf-8"
        )
    )
    assert pg_store.count("SELECTcreated_byFROMnotebooksWHEREid=%sFORSHARE") == 3, (
        "postgres/memory_store.py 的分段式带锁站点数量变了——"
        "同步更新本 allowlist 与两份 access_sql 的 docstring 清单"
    )
    # 3 个站点各 1 次,加 import 行 1 次 = 4。
    assert pg_store.count("MEMBER_PROBE_FOR_SHARE_SQL") == 4, (
        "postgres/memory_store.py 的成员探测半支数量与站点数不符"
    )
    assert pg_store.count("GRANT_PROBE_FOR_SHARE_SQL") == 4, (
        "postgres/memory_store.py 有站点没接上授权边探测半支——"
        "被授权者会在写事务里被拒,而读路径放行,两条路径当场分叉"
    )
    sqlite_store = _collapsed(
        (_BACKEND_APP / "repositories/sqlite/memory_store.py").read_text(
            encoding="utf-8"
        )
    )
    assert sqlite_store.count("SELECTcreated_byFROMnotebooksWHEREid=?") == 1, (
        "sqlite/memory_store.py 的分段式三态站点数量变了——"
        "同步更新本 allowlist 与两份 access_sql 的 docstring 清单"
    )
    # 1 个站点各 1 次,加 import 行 1 次 = 2。
    assert sqlite_store.count("MEMBER_PROBE_SQL") == 2, (
        "sqlite/memory_store.py 的成员探测半支数量与站点数不符"
    )
    assert sqlite_store.count("GRANT_PROBE_SQL") == 2, (
        "sqlite/memory_store.py 的三态站点没接上授权边探测半支"
    )
