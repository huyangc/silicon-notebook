# backend/tests/test_access_sql_contract.py
"""notebook 授权谓词唯一定义点(`repositories/*/access_sql.py`)的行为契约。

P0-T1 把散落在 sharing_store / memory_store / search 的「owner ∨ 只读成员」手写复刻
收进唯一定义点。这份矩阵钉住的是**重构前后逐格相同**的语义,后续群组授权(P1)在
唯一定义点上扩展读权时,这里就是「哪一格该翻、哪一格绝不许翻」的基准:

* 写权恒为 owner-only —— 只读成员是**访客**,群组扩展读权时这一列不得跟着松。
* 不存在的 notebook 两权皆否(无行 → False),不抛异常、不泄露存在性。

另钉住 service 层 `user_can_read_notebook` 与 store 新方法结果一致:重构把 service
从「写权 or 成员」两次查询改成一跳委托 store 单条查询,两者必须逐格同义。
"""
import uuid

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


@pytest.fixture
def world(repo):
    """owner / 只读成员 / 陌生人 三种主体,外加一个真实存在的 notebook。"""
    owner = _mk_user(repo, "user-owner")
    member = _mk_user(repo, "user-member")
    stranger = _mk_user(repo, "user-stranger")
    notebook = _mk_nb(repo, owner=owner)
    repo.add_member(notebook, member)
    return {
        "repo": repo,
        "owner": owner,
        "member": member,
        "stranger": stranger,
        "notebook": notebook,
    }


# (主体键, notebook 键, 期望读权, 期望写权)
ACCESS_MATRIX = [
    ("owner", "notebook", True, True),
    ("member", "notebook", True, False),      # 只读成员:能读,绝不能写
    ("stranger", "notebook", False, False),
    ("owner", "missing", False, False),       # 不存在的 notebook:两权皆否
    ("member", "missing", False, False),
    ("stranger", "missing", False, False),
]


@pytest.mark.parametrize(
    "subject,target,expect_read,expect_write",
    ACCESS_MATRIX,
    ids=[f"{s}-{t}" for s, t, _r, _w in ACCESS_MATRIX],
)
def test_read_and_write_predicates_match_the_pre_refactor_matrix(
    world, subject, target, expect_read, expect_write
):
    repo = world["repo"]
    user_id = world[subject]
    notebook_id = world["notebook"] if target == "notebook" else MISSING_NOTEBOOK

    assert repo.user_can_read_notebook(notebook_id, user_id) is expect_read
    assert repo.user_can_access_notebook(notebook_id, user_id) is expect_write


def test_write_predicate_stays_owner_only_for_every_member(world):
    """写权是安全边界:凡不是 owner,一律没有写权——群组扩展读权时这条不得松。"""
    repo, notebook = world["repo"], world["notebook"]
    assert repo.user_can_access_notebook(notebook, world["owner"]) is True
    for non_owner in (world["member"], world["stranger"]):
        assert repo.is_member(notebook, non_owner) == (non_owner == world["member"])
        assert repo.user_can_access_notebook(notebook, non_owner) is False


@pytest.mark.parametrize(
    "subject,target,expect_read,expect_write",
    ACCESS_MATRIX,
    ids=[f"{s}-{t}" for s, t, _r, _w in ACCESS_MATRIX],
)
def test_service_read_predicate_agrees_with_the_store_predicate(
    world, subject, target, expect_read, expect_write
):
    """service 一跳委托 store 之后,两层必须逐格同义。

    重构前 service 写的是「写权 or 成员」(两次查询)。这里同时断言旧口径的合取式与
    新的单条查询在整个矩阵上一致——若哪天唯一定义点扩了读权而旧口径没跟上,这条会
    在**该翻的那一格**上失败,正是它存在的意义。
    """
    del expect_write
    repo = world["repo"]
    user_id = world[subject]
    notebook_id = world["notebook"] if target == "notebook" else MISSING_NOTEBOOK
    store = repo._runtime.sharing_store

    store_result = store.user_can_read_notebook(notebook_id, user_id)
    assert store_result is expect_read
    assert repo.user_can_read_notebook(notebook_id, user_id) is store_result
    legacy = store.user_can_access_notebook(notebook_id, user_id) or store.is_member(
        notebook_id, user_id
    )
    assert legacy is store_result


def test_predicate_follows_membership_changes(world):
    """读权是实时判定而非一次性授予:踢掉成员即刻失读权,写权全程为假。"""
    repo, notebook, member = world["repo"], world["notebook"], world["member"]
    assert repo.user_can_read_notebook(notebook, member) is True
    repo.remove_member(notebook, member)
    assert repo.user_can_read_notebook(notebook, member) is False
    assert repo.user_can_access_notebook(notebook, member) is False
    repo.add_member(notebook, member)
    assert repo.user_can_read_notebook(notebook, member) is True


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
    for subject in ("owner", "member", "stranger"):
        user_id = world[subject]
        with repo._connect() as db:
            row = db.execute(embedded, (notebook, user_id, user_id)).fetchone()
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
    for subject in ("owner", "member", "stranger"):
        user_id = world[subject]
        with repo._connect() as db:
            row = db.execute(embedded, ("src-access", user_id, user_id)).fetchone()
        assert (row is not None) is repo.user_can_read_notebook(notebook, user_id)


def test_member_probe_matches_is_member(world):
    """两段式带锁写法复用的成员探测常量,必须与 `is_member` 同义。"""
    repo, notebook = world["repo"], world["notebook"]
    for subject in ("owner", "member", "stranger"):
        user_id = world[subject]
        with repo._connect() as db:
            row = db.execute(
                access_sql.MEMBER_PROBE_SQL, (notebook, user_id)
            ).fetchone()
        assert (row is not None) is repo.is_member(notebook, user_id)


def test_backends_declare_the_same_predicate_shape():
    """双后端同修:两份 access_sql 只应差在占位符上。

    群组授权会在这两份文件里扩展同一条读权谓词;一侧漏改就是「PostgreSQL 部署的
    权限与 SQLite 部署不同」,而这种分叉在单后端的测试里永远看不见。
    """
    from app.repositories.postgres import access_sql as pg

    def normalized(text: str) -> str:
        return text.replace("%s", "?")

    assert normalized(pg.NOTEBOOK_READ_SQL) == access_sql.NOTEBOOK_READ_SQL
    assert normalized(pg.NOTEBOOK_WRITE_SQL) == access_sql.NOTEBOOK_WRITE_SQL
    assert normalized(pg.MEMBER_PROBE_SQL) == access_sql.MEMBER_PROBE_SQL
    assert normalized(pg.read_access_clause()) == access_sql.read_access_clause()
    assert (
        normalized(pg.read_access_exists_clause("m"))
        == access_sql.read_access_exists_clause("m")
    )
    # PG 独有:两段式带锁写法用的加锁变体,只应比裸探测多一个 FOR SHARE 后缀。
    assert pg.MEMBER_PROBE_FOR_SHARE_SQL == pg.MEMBER_PROBE_SQL + " FOR SHARE"
