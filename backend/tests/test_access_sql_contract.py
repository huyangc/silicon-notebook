# backend/tests/test_access_sql_contract.py
"""notebook 授权谓词唯一定义点(`repositories/*/access_sql.py`)的行为契约。

P0-T1 把散落在 sharing_store / memory_store / search 的「owner ∨ 只读成员」手写复刻
收进唯一定义点;P1-T2 在同一处把读权扩成「owner ∪ 只读成员 ∪ 有效授权边」;
P2-T2 在同一处新增第三条谓词——**管理权** = owner ∪ `role='admin'` 的有效授权边。
这份矩阵钉住的是**哪一格该翻、哪一格绝不许翻**:

* 写权(`user_can_access_notebook`)恒为 owner-only —— 只读成员、viewer 边、
  **乃至持管理边的组管理员**都不得为真。P2 翻的是能力表(`deps._CAPABILITY_LEVELS`
  把六个能力从 owner 档挪到 admin 档),**不是**这条谓词:它仍然是
  `notebook:delete` 与 Agent/MCP 面的实现,松了它就等于把删库一起送出去。
* 管理权(`user_can_admin_notebook`)= owner ∪ 管理边。这一列是 P2 唯一的新列。
* 包含链 `写权 ⊆ 管理权 ⊆ 读权` 逐格成立(`test_predicate_levels_are_nested`)。
* 不存在的 notebook 三权皆否(无行 → False),不抛异常、不泄露存在性。
* `legacy_read` 一列是 P1 之前的口径(`写权 or is_member`)。它钉的是**旧口径没被
  顺手改**,外加「只许扩、不许收窄既有主体」这条单调性。它并**不**是防「全放行」的
  那道闸——那由矩阵里 `expect_read=False` 的格子(陌生人、`group_plain`、哨兵库)
  承担,`legacy_read` 在那些格子上本来就是 False。两者一起看才完整。
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

    # everyone + role='admin' 的库(P2-T2 评审 P2-1):这条边**发放口径根本写不出来**
    # (GRANTABLE_PRINCIPAL_TYPES 只收群组主体),这里手插它是为了钉住谓词侧的深度
    # 防御——管理级主体判定排除 everyone,所以即便这样一条边存在(比如正向 shadow
    # 停车把某行的 role 暂写成 'admin'),它也**绝不**授予任何人管理权。读权那一半照旧
    # 全员放行(everyone 是 viewer)。
    everyone_admin_nb = _mk_nb(repo, owner=owner)
    _mk_grant(repo, everyone_admin_nb, "everyone", "", role="admin")

    # 管理库(P2-T2):把「主体类型」与「边的 role」这**两根轴**拆开测。
    # 上面那本 `notebook` 里两者恰好同向(group→viewer、group_admins→admin),
    # 于是「组管理员可管理」既可能是主体判对了,也可能是把 group_admins 主体当成了
    # 管理权——两种实现在那本库上给出完全相同的答案。这本库把它们交叉过来:
    #   * `group` 边发成 `admin`  → 整组人(含普通成员 group_member)都可管理;
    #   * `group_admins` 边发成 `viewer` → 组管理员(group_admin)可读但**不可管理**。
    # 任何「按 principal_type 推断管理权」的实现都会在这本库上把两格都判反。
    admin_nb = _mk_nb(repo, owner=owner)
    _mk_grant(repo, admin_nb, "group", viewers, role="admin")
    _mk_grant(repo, admin_nb, "group_admins", admins, role="viewer")

    # 哨兵库:四行停车中的授权边,分别长得像「everyone」「点名 stranger」「点名
    # grp-viewers」,外加一行**带 `role='admin'`** 的。四值精确匹配下它们一行都不
    # 生效;最后那行专钉管理权谓词——它比读权多一个 `role='admin'` 条件,一个
    # 「role 对了就放行、主体白名单写漏了」的实现只会在这一行上暴露。
    sentinel_nb = _mk_nb(repo, owner=owner)
    _mk_grant(repo, sentinel_nb, PARKED_PRINCIPAL_TYPE, "")
    _mk_grant(repo, sentinel_nb, PARKED_PRINCIPAL_TYPE, stranger)
    _mk_grant(repo, sentinel_nb, PARKED_PRINCIPAL_TYPE, viewers)
    _mk_grant(repo, sentinel_nb, PARKED_PRINCIPAL_TYPE, admins, role="admin")

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
        "everyone_admin": everyone_admin_nb,
        "managed": admin_nb,
        "sentinel": sentinel_nb,
        "missing": MISSING_NOTEBOOK,
    }


# (主体键, notebook 键, 期望读权, 期望**管理权**, 期望写权, P1 之前的旧读权口径)
#
# 管理权那一列是 P2-T2 新增的。写权那一列**逐格未变**——这份矩阵在 P2 的改动里
# 一个 `expect_write` 都没有翻,那正是「写权谓词本身没被顺手放宽」的可执行证据。
ACCESS_MATRIX = [
    ("owner", "notebook", True, True, True, True),
    ("member", "notebook", True, False, False, True),   # 只读成员:能读,不能管、不能写
    ("stranger", "notebook", False, False, False, False),
    # ↓ P1 翻的正是这三格,且只有这三格(读权那一列)。
    ("grantee", "notebook", True, False, False, False),      # principal_type='user'
    ("group_member", "notebook", True, False, False, False),  # principal_type='group'
    # ↓ P2-T2 翻的**唯一**一格:group_admins 主体 + role='admin' 的边 → 管理权为真。
    #   写权那一列仍是 False —— 组管理员**不是** owner,删库与 Agent 面照旧拒绝。
    ("group_admin", "notebook", True, True, False, False),
    # 授权给「组管理员」的库,组里的普通成员读不到(除非另有 group 行)。
    ("group_plain", "notebook", False, False, False, False),
    # everyone 授权:任何登录用户都能读,但一个字的写权/管理权都不给
    # (那批边的 role 是 viewer;everyone+admin 由 app 层发放口径挡住)。
    ("owner", "everyone", True, True, True, True),
    ("stranger", "everyone", True, False, False, False),
    ("group_plain", "everyone", True, False, False, False),
    # ↓ everyone + role='admin'(手插的非法边,P2-T2 评审 P2-1):读权照旧全员放行
    #   (everyone 是 viewer),但**管理权对谁都是 False**——谓词侧排除 everyone,
    #   即便这条边存在也不授予管理权。owner 仍按 owner 分支可管(与这条边无关)。
    ("stranger", "everyone_admin", True, False, False, False),
    ("group_plain", "everyone_admin", True, False, False, False),
    ("owner", "everyone_admin", True, True, True, True),
    # ↓ 管理库:两根轴交叉,专拆「principal_type 与 role 谁决定管理权」。
    ("owner", "managed", True, True, True, True),
    # group 边发成 admin:整组人(含**普通**组成员)都可管理。
    ("group_member", "managed", True, True, False, False),
    # group_admins 边发成 viewer:组管理员可读,**不可管理**。
    ("group_admin", "managed", True, False, False, False),
    # 该组的普通成员连读都不该有(group_admins 边只到管理员)。
    ("group_plain", "managed", False, False, False, False),
    ("stranger", "managed", False, False, False, False),
    # 哨兵停车行:谁也匹配不上(含那行 role='admin' 的),owner 仍按 owner 分支照常。
    ("owner", "sentinel", True, True, True, True),
    ("stranger", "sentinel", False, False, False, False),
    ("grantee", "sentinel", False, False, False, False),
    ("group_member", "sentinel", False, False, False, False),
    ("group_admin", "sentinel", False, False, False, False),
    ("owner", "missing", False, False, False, False),  # 不存在的 notebook:三权皆否
    ("member", "missing", False, False, False, False),
    ("stranger", "missing", False, False, False, False),
    ("grantee", "missing", False, False, False, False),
    ("group_admin", "missing", False, False, False, False),
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
    "subject,target,expect_read,expect_admin,expect_write,legacy_read",
    ACCESS_MATRIX,
    ids=[f"{s}-{t}" for s, t, _r, _a, _w, _l in ACCESS_MATRIX],
)
def test_read_admin_and_write_predicates_match_the_matrix(
    world, subject, target, expect_read, expect_admin, expect_write, legacy_read
):
    del legacy_read
    repo = world["repo"]
    user_id = world[subject]
    notebook_id = world[target]

    assert repo.user_can_read_notebook(notebook_id, user_id) is expect_read
    assert repo.user_can_admin_notebook(notebook_id, user_id) is expect_admin
    assert repo.user_can_access_notebook(notebook_id, user_id) is expect_write


@pytest.mark.parametrize(
    "subject,target,expect_read,expect_admin,expect_write,legacy_read",
    ACCESS_MATRIX,
    ids=[f"{s}-{t}" for s, t, _r, _a, _w, _l in ACCESS_MATRIX],
)
def test_predicate_levels_are_nested(
    world, subject, target, expect_read, expect_admin, expect_write, legacy_read
):
    """`写权 ⊆ 管理权 ⊆ 读权` 逐格成立。

    这条不是重复上面那份矩阵,而是钉住三条谓词之间的**结构关系**:管理权谓词是
    「读权那四条臂 ∧ role='admin'」拼出来的,所以它**永远**不可能比读权宽——真出现
    「能管却不能读」的格子,只可能是有人另抄了一份主体判定并在里面漏了一条臂
    (那正是唯一定义点要防的形态,而它在单条谓词的行为矩阵里看不出来)。
    """
    del legacy_read
    repo = world["repo"]
    user_id, notebook_id = world[subject], world[target]

    can_read = repo.user_can_read_notebook(notebook_id, user_id)
    can_admin = repo.user_can_admin_notebook(notebook_id, user_id)
    can_write = repo.user_can_access_notebook(notebook_id, user_id)
    assert (can_read, can_admin, can_write) == (expect_read, expect_admin, expect_write)
    assert not (can_write and not can_admin), "写权跑到管理权之外了"
    assert not (can_admin and not can_read), "管理权跑到读权之外了"


def test_write_predicate_stays_owner_only_for_every_member(world):
    """写权是安全边界:凡不是 owner,一律没有写权。

    ⚠ P2 的能力翻转发生在 `deps._CAPABILITY_LEVELS`(六个能力从 owner 档挪到 admin
    档),**不在这条谓词上**:它仍然是 `notebook:delete` 与 Agent/MCP 面的实现。所以
    持管理边的 `group_admin` 在这里必须仍为 False —— 顺手把它放宽就等于把「删掉整本
    库」和「Agent token 写别人的库」一起送出去。
    """
    repo, notebook = world["repo"], world["notebook"]
    assert repo.user_can_access_notebook(notebook, world["owner"]) is True
    for key in ("member", "stranger", "grantee", "group_member", "group_admin"):
        non_owner = world[key]
        assert repo.is_member(notebook, non_owner) == (key == "member")
        assert repo.user_can_access_notebook(notebook, non_owner) is False
    # everyone 授权同样一个字的写权都不给。
    assert repo.user_can_access_notebook(world["everyone"], world["stranger"]) is False
    # 管理库上「整组可管理」的那条边同样不给写权。
    assert repo.user_can_access_notebook(world["managed"], world["group_member"]) is False


def test_admin_predicate_tracks_the_grant_role_not_the_principal_type(world):
    """管理权判的是**边自己的 `role`**,不是 `principal_type`。

    这条把 `managed` 库那两格单独说清楚:`group_admins` 主体 + viewer 边 → 不可管理;
    `group` 主体 + admin 边 → 连组里的普通成员都可管理。把 `group_admins` 主体当成
    「管理权」的实现(一个非常自然的误读——名字里就带 admins)会把两格同时判反,而
    在 `notebook` 那本库上它给出的答案与正确实现**逐格相同**。
    """
    repo, managed = world["repo"], world["managed"]
    assert repo.user_can_admin_notebook(managed, world["group_admin"]) is False
    assert repo.user_can_read_notebook(managed, world["group_admin"]) is True
    assert repo.user_can_admin_notebook(managed, world["group_member"]) is True


def test_downgrading_the_grant_role_revokes_admin_but_keeps_read(world):
    """把管理边改成 viewer:管理权当场消失,读权原样保留(实时判定,不是一次性授予)。"""
    repo, notebook = world["repo"], world["notebook"]
    group_admin = world["group_admin"]
    assert repo.user_can_admin_notebook(notebook, group_admin) is True
    with repo._write() as db:
        db.execute(
            "UPDATE notebook_grants SET role='viewer' "
            "WHERE notebook_id=? AND principal_type='group_admins'",
            (notebook,),
        )
    assert repo.user_can_admin_notebook(notebook, group_admin) is False
    assert repo.user_can_read_notebook(notebook, group_admin) is True


def test_deleting_the_admin_grant_edge_revokes_admin(world):
    """删掉管理边即刻失管理权(读权也一并没了——那条边是他唯一的读权来源)。"""
    repo, notebook = world["repo"], world["notebook"]
    group_admin = world["group_admin"]
    assert repo.user_can_admin_notebook(notebook, group_admin) is True
    _delete_rows(
        repo,
        "DELETE FROM notebook_grants WHERE notebook_id=? AND principal_type='group_admins'",
        (notebook,),
    )
    assert repo.user_can_admin_notebook(notebook, group_admin) is False
    assert repo.user_can_read_notebook(notebook, group_admin) is False


def test_demoting_a_group_admin_revokes_admin_capability(world):
    """组内降级(role='member')同样即刻失管理权——两根轴任一为假即为假。"""
    repo, notebook = world["repo"], world["notebook"]
    group_admin, admins = world["group_admin"], world["admins"]
    assert repo.user_can_admin_notebook(notebook, group_admin) is True
    with repo._write() as db:
        db.execute(
            "UPDATE group_members SET role='member' WHERE group_id=? AND user_id=?",
            (admins, group_admin),
        )
    assert repo.user_can_admin_notebook(notebook, group_admin) is False


def test_admin_grant_pointing_at_a_deleted_group_fails_closed(world):
    """管理边指向已删组:`group_members` 经 CASCADE 消失,管理权当场为假。

    与读权那条同款的 fail-safe:授权边行本身还在(`principal_id` 无 FK),但谓词
    join 不到成员,所以孤儿管理边不会留下一条越权写通道。
    """
    repo, notebook = world["repo"], world["notebook"]
    group_admin = world["group_admin"]
    assert repo.user_can_admin_notebook(notebook, group_admin) is True
    with repo._write() as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("DELETE FROM groups WHERE id=?", (world["admins"],))
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebook_grants "
            "WHERE notebook_id=? AND principal_type='group_admins'",
            (notebook,),
        ).fetchone()["c"]
    assert remaining == 1, "授权边行刻意保留:这条测的是谓词侧的 fail-safe"
    assert repo.user_can_admin_notebook(notebook, group_admin) is False


def test_parked_sentinel_admin_grant_matches_nobody(world):
    """带 `role='admin'` 的哨兵停车行同样谁也匹配不上。

    管理权谓词比读权多一个 `role='admin'` 条件,所以它有一种读权没有的失守形态:
    实现者先判 role、再「反正 role 对了」放宽主体白名单。哨兵库那行
    (`principal_type` 是停车串、`principal_id` 指向 grp-admins、`role='admin'`)
    专钉这一形态。
    """
    repo, sentinel = world["repo"], world["sentinel"]
    for key in ("stranger", "grantee", "group_member", "group_admin", "group_plain", "member"):
        assert repo.user_can_admin_notebook(sentinel, world[key]) is False, key
    assert repo.user_can_admin_notebook(sentinel, world["owner"]) is True


def test_everyone_grant_never_confers_admin(world):
    """`everyone` 主体**绝不**授予管理权,无论边的 role 写成什么(P2-T2 评审 P2-1)。

    设计 §4 明文:everyone 只能 viewer。`world` 里 `everyone_admin` 那本库手插了一条
    `(everyone,'',admin)` 边(发放口径根本写不出来,但正向 shadow 停车可能把某行 role
    暂写成 'admin')。管理级主体判定排除 everyone,所以这条边对**任何人**都不授予管理
    权;读权那一半照旧全员放行(everyone 是 viewer)。owner 仍按 owner 分支可管。
    """
    repo, nb = world["repo"], world["everyone_admin"]
    for key in ("stranger", "grantee", "group_member", "group_admin", "group_plain", "member"):
        assert repo.user_can_admin_notebook(nb, world[key]) is False, key
        assert repo.user_can_read_notebook(nb, world[key]) is True, key  # everyone=viewer
    assert repo.user_can_admin_notebook(nb, world["owner"]) is True  # owner 分支


def test_admin_predicate_reuses_the_restricted_arms_and_excludes_everyone():
    """结构守卫(P2-T2 评审 P2-2):管理级 SQL **逐字内含**受限三臂、且**不含** everyone。

    行为矩阵证明的是「当前数据上答案对」,这条证明的是「谓词是怎么拼出来的」——管理级
    复用读权的受限三臂(user/group/group_admins)而不是另抄一份,所以「管理权 ⊆ 读权」
    构造性成立;手抄一份再丢一条臂的变异会让这条断言当场红,而它在行为矩阵里可能恰好
    照不到(丢的那条臂在矩阵里没有对应主体时)。双后端各一份。
    """
    from app.repositories.postgres import access_sql as pg

    for mod, ph in ((access_sql, "?"), (pg, "%s")):
        restricted = mod._restricted_principal_arms("ng", ph, "ngm", "nga")
        assert restricted in mod.NOTEBOOK_ADMIN_SQL, (
            f"{mod.__name__}: 管理级 SQL 没有逐字复用受限三臂(疑似另抄了一份)"
        )
        # everyone 那条臂绝不出现在管理级里(它出现在读权里,是刻意的)。
        assert "principal_type='everyone'" not in mod.NOTEBOOK_ADMIN_SQL, (
            f"{mod.__name__}: 管理级 SQL 收下了 everyone(应只 viewer)"
        )
        assert "principal_type='everyone'" in mod.read_access_clause(), (
            f"{mod.__name__}: 读权谓词丢了 everyone 臂(它必须留在读权里)"
        )
        # 受限三臂本身是读权四臂的逐字子串(管理权主体 ⊂ 读权主体)。
        assert restricted in mod._principal_match_expr("ng", ph, "ngm", "nga")


def test_admin_grant_role_literal_agrees_across_its_three_spellings():
    """`'admin'` 这个字面量在三处独立出现,必须是同一个值。

    `models/groups.py::GRANT_ROLES`(API 校验)、`group_rows.ADMIN_GRANT_ROLE`
    (行整形)、两份 `access_sql` 的谓词文本(SQL)刻意不互相 import(会成环),
    所以由这条断言把三处钉在一起——改其中一处而漏掉另一处,表现是「API 收下了这个
    role,但谓词/投影认不出它」,不报错、只是权限静默失效。
    """
    from app.models.groups import GRANT_ROLES
    from app.repositories.group_rows import ADMIN_GRANT_ROLE
    from app.repositories.postgres import access_sql as pg

    assert ADMIN_GRANT_ROLE in GRANT_ROLES
    for mod in (access_sql, pg):
        assert f"ng.role='{ADMIN_GRANT_ROLE}'" in mod.NOTEBOOK_ADMIN_SQL
        # 反向:读权谓词里**绝不许**出现授权边(别名 `ng`)自己的 role 条件——读权
        # 对 viewer 边必须照样为真。`nga.role='admin'` 是**组成员**角色,另一根轴,
        # 读权那条臂本来就有它,所以判据必须精确到别名。
        assert "ng.role=" not in mod.read_access_clause(), (
            "读权谓词里出现了授权边的 role 条件——读权对 viewer 边必须照样为真"
        )
        assert "nga.role='admin'" in mod.read_access_clause(), (
            "读权谓词丢了 group_admins 那条臂的组成员角色条件"
        )


@pytest.mark.parametrize(
    "subject,target,expect_read,expect_admin,expect_write,legacy_read",
    ACCESS_MATRIX,
    ids=[f"{s}-{t}" for s, t, _r, _a, _w, _l in ACCESS_MATRIX],
)
def test_service_predicates_agree_with_the_store_predicates(
    world, subject, target, expect_read, expect_admin, expect_write, legacy_read
):
    """service 一跳委托 store 之后,两层必须逐格同义(读权与管理权各一条)。

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

    store_admin = store.user_can_admin_notebook(notebook_id, user_id)
    assert store_admin is expect_admin
    assert repo.user_can_admin_notebook(notebook_id, user_id) is store_admin

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


def test_admin_clause_embeds_into_a_larger_query_with_identical_results(world):
    """管理权的可嵌片段与完整查询同义 —— 与读权那条同款,理由也一样。

    片段形式是给未来的消费点(投影/列表查询)准备的;它与 `NOTEBOOK_ADMIN_SQL` 一旦
    漂移,「界面画不画写按钮」与「守卫放不放行」就会给出不同答案。
    """
    repo = world["repo"]
    clause = access_sql.admin_access_clause()
    embedded = "SELECT nb.id FROM notebooks nb WHERE nb.id=? AND " + clause
    for target in ("notebook", "managed", "everyone", "sentinel"):
        notebook_id = world[target]
        for subject in _EMBED_SUBJECTS:
            user_id = world[subject]
            with repo._connect() as db:
                row = db.execute(
                    embedded, (notebook_id, *access_sql.admin_access_params(user_id))
                ).fetchone()
            assert (row is not None) is repo.user_can_admin_notebook(
                notebook_id, user_id
            ), (target, subject)


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


def test_grant_expr_is_exactly_the_restricted_and_everyone_halves():
    """`grant_access_expr` = 受限三臂 ∪ everyone,两个半支不重不漏。

    拆出这两个 public 片段是为了让**挂载**有效性区别对待点名授权与全员授权
    (`mount_sql` 的借入挂载未共享门);**读权**必须完全不受影响。这条钉的就是那句
    「不受影响」的可执行形式:任何一臂在拆分中被漏掉或被两边同时收下,这里当场红。
    """
    from app.repositories.postgres import access_sql as pg

    for mod, ph in ((access_sql, "?"), (pg, "%s")):
        whole = mod.grant_access_expr("b.id", ph)
        restricted = mod.restricted_grant_access_expr("b.id", ph)
        everyone = mod.everyone_grant_expr("b.id", "ng")
        for arm in ("principal_type='user'", "principal_type='group'",
                    "principal_type='group_admins'"):
            assert arm in whole and arm in restricted, arm
            assert arm not in everyone, arm
        assert "principal_type='everyone'" in whole
        assert "principal_type='everyone'" in everyone
        assert "principal_type='everyone'" not in restricted, (
            "受限片段收下了 everyone —— 借入挂载的未共享门会连全员授权一起挡掉"
        )
        # 受限片段的参数消费必须与整体一致(everyone 那臂本来就零参数)。
        assert restricted.count(ph) == whole.count(ph)
        assert everyone.count(ph) == 0


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
    # 管理级授权边**整条生效链**的加锁探测(codex #519 R5 立、R8 P1 收口成两条)。
    # 同上:SQLite 的进程写锁已把写事务串起来,不需要也不存在行锁变体;裸的
    # ADMIN_GRANT_PROBE_SQL 两侧都有。R5 那条只锁边行的 ADMIN_GRANT_PROBE_FOR_SHARE_SQL
    # 已**删除**——留着就是给「只堵一端」留一个看起来正规的入口。
    "ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL",
    "ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL",
    "admin_grant_user_arm_params",
    "admin_grant_group_chain_params",
    # Ask detail 自助读取会投影回答/轨迹内容，因此同样需要把读权链锁到投影结束。
    "READ_GRANT_DIRECT_FOR_SHARE_SQL",
    "READ_GRANT_GROUP_CHAIN_FOR_SHARE_SQL",
    "read_grant_direct_params",
    "read_grant_group_chain_params",
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
    "restricted_grant_access_expr": lambda mod: mod.restricted_grant_access_expr(
        "outer.notebook_id", "outer.user_id"
    ),
    "everyone_grant_expr": lambda mod: mod.everyone_grant_expr("outer.notebook_id"),
    "admin_grant_access_expr": lambda mod: mod.admin_grant_access_expr(
        "outer.notebook_id", "outer.user_id"
    ),
    "read_access_clause": lambda mod: mod.read_access_clause(),
    "admin_access_clause": lambda mod: mod.admin_access_clause(),
    "read_access_exists_clause": lambda mod: mod.read_access_exists_clause("m"),
    "read_access_params": lambda mod: repr(mod.read_access_params("U")),
    "admin_access_params": lambda mod: repr(mod.admin_access_params("U")),
    "grant_probe_params": lambda mod: repr(mod.grant_probe_params("N", "U")),
    "admin_grant_probe_params": lambda mod: repr(
        mod.admin_grant_probe_params("N", "U")
    ),
    # PG 独有(整条生效链的加锁探测,codex #519 R8 P1)。登记在这里不是为了双后端比对
    # ——它们在 `_PG_ONLY_SYMBOLS` 里、进不了交集——而是因为占位符方向守卫会遍历**全部**
    # public 符号并经 `_probe_value` 取值,漏登记就响亮失败。
    "admin_grant_user_arm_params": lambda mod: repr(
        mod.admin_grant_user_arm_params("N", "U")
    ),
    "admin_grant_group_chain_params": lambda mod: repr(
        mod.admin_grant_group_chain_params("N", "U")
    ),
    "read_grant_direct_params": lambda mod: repr(
        mod.read_grant_direct_params("N", "U")
    ),
    "read_grant_group_chain_params": lambda mod: repr(
        mod.read_grant_group_chain_params("N", "U")
    ),
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
    # 授权边那条写 `OF ng`:显式化「锁的是哪张表的行」。它不是语法必须(同层
    # rangetable 里只有 ng),钉住它是为了让「组成员资格刻意不锁」这条已登记取舍
    # 在代码里始终看得见——见 postgres/access_sql.py 的模块 docstring。
    assert pg.MEMBER_PROBE_FOR_SHARE_SQL == pg.MEMBER_PROBE_SQL + " FOR SHARE"
    assert pg.GRANT_PROBE_FOR_SHARE_SQL == pg.GRANT_PROBE_SQL + " FOR SHARE OF ng"
    # 管理级:R8 P1 之后锁的是**整条生效链**,不再是「裸探测 + FOR SHARE OF ng」。
    # 钉住两件事:①两条语句都真的带锁;②group 链那条必须把成员行也锁进去
    # (`OF ng, ngm`)——只写 `OF ng` 就退回了 R5 那个只堵一端的形态,而它不报任何错。
    assert pg.ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL.endswith(" FOR SHARE OF ng")
    assert pg.ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL.endswith(" FOR SHARE OF ng, ngm")
    assert "JOIN group_members ngm ON " in pg.ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL, (
        "成员行必须经**内连接**提到顶层才锁得住——EXISTS 子查询里的行拿不到 FOR SHARE,"
        "那正是 R8 P1 那个洞的成因"
    )
    # R5 那条只锁边行的常量必须保持删除状态:留着就是给「只堵生效链一端」留一个
    # 看起来正规的入口(它当年正是被这么用的)。
    assert not hasattr(pg, "ADMIN_GRANT_PROBE_FOR_SHARE_SQL"), (
        "ADMIN_GRANT_PROBE_FOR_SHARE_SQL 回来了——它只锁授权边行、锁不住让边生效的"
        "组成员行,写事务里认管理权必须用整链加锁的那两条"
    )
    # 管理级探测必须是「裸探测 + role='admin'」的收窄,而不是另抄一份主体判定:
    # 两条查询只差 role 那个条件,everyone 那条臂在管理级里不存在(裁决 P2-1 收窄)。
    assert "ng.role='admin'" in pg.ADMIN_GRANT_PROBE_SQL
    assert "ng.role='admin'" in access_sql.ADMIN_GRANT_PROBE_SQL
    assert "everyone" not in pg.ADMIN_GRANT_PROBE_SQL
    assert "everyone" not in access_sql.ADMIN_GRANT_PROBE_SQL


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
