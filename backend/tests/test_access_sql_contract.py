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
    # 两段式带锁写法的加锁变体;SQLite 没有行锁概念,不应有对应物。
    "MEMBER_PROBE_FOR_SHARE_SQL",
}
_SQLITE_ONLY_SYMBOLS: set[str] = set()

# 可调用符号的比对探针:新增可调用符号必须在此登记调用参数,否则下面的守卫响亮
# 失败(逼着登记,而不是静默跳过比对)。
_CALLABLE_PROBES = {
    "member_exists_expr": lambda mod: mod.member_exists_expr(
        "outer.notebook_id", "outer.user_id"
    ),
    "read_access_clause": lambda mod: mod.read_access_clause(),
    "read_access_exists_clause": lambda mod: mod.read_access_exists_clause("m"),
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

    # PG 独有:两段式带锁写法用的加锁变体,只应比裸探测多一个 FOR SHARE 后缀。
    assert pg.MEMBER_PROBE_FOR_SHARE_SQL == pg.MEMBER_PROBE_SQL + " FOR SHARE"


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


def test_owner_or_member_shape_lives_only_in_access_sql():
    """「owner ∨ 成员」的内联形状只许出现在 access_sql.py。

    唯一定义点的价值在于没有第二份:重新手写一份逐字相同的复刻,今天语义相同,
    P1 扩展群组授权那天就是一条静默分叉的授权路径。这条移动变异守卫保证「把定义
    搬回消费点」会报红,而不是靠 docstring 清单的自觉。
    """
    pattern = re.compile(r"created_by=(\?|%s)OREXISTS\(SELECT1FROMnotebook_members")
    offenders = []
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        if path.name == "access_sql.py":
            continue
        if pattern.search(_collapsed(path.read_text(encoding="utf-8"))):
            offenders.append(str(path.relative_to(_BACKEND_APP)))
    assert offenders == [], (
        f"授权谓词形状出现在唯一定义点之外:{offenders}。"
        "请改用 access_sql 的片段函数,不要手写内联复刻。"
    )


def test_two_step_locked_sites_stay_pinned():
    """两段式带锁/三态站点的 allowlist:数量一变就必须回来更新这里与 docstring。

    这些站点只复用了成员探测那一半,owner 半是手写的 `SELECT created_by`——群组
    授权(P1)在 read_access_clause 里扩进群组成员时,它们**不会自动跟随**,必须
    逐处手改。这条守卫在「有人新增/删除/搬动这类站点」时逼人回来看清单,而不是
    让新站点静默游离在扩展范围之外。
    """
    pg_store = _collapsed(
        (_BACKEND_APP / "repositories/postgres/memory_store.py").read_text(
            encoding="utf-8"
        )
    )
    assert pg_store.count("SELECTcreated_byFROMnotebooksWHEREid=%sFORSHARE") == 3, (
        "postgres/memory_store.py 的两段式带锁站点数量变了——"
        "同步更新本 allowlist 与两份 access_sql 的 docstring 清单"
    )
    sqlite_store = _collapsed(
        (_BACKEND_APP / "repositories/sqlite/memory_store.py").read_text(
            encoding="utf-8"
        )
    )
    assert sqlite_store.count("SELECTcreated_byFROMnotebooksWHEREid=?") == 1, (
        "sqlite/memory_store.py 的两段式三态站点数量变了——"
        "同步更新本 allowlist 与两份 access_sql 的 docstring 清单"
    )
