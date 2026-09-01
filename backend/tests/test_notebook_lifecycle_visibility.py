"""行为面守卫:`notebooks.status` 的可见性谓词在**可观察行为**上恒等
(批 3·W1 T-1;codex PR#653 第 1 轮 P2)。

`test_notebook_live_status_literal_guard.py` 的文本扫描(regex over
``ast.Constant``)防的是「常量之外又长出第二份拼写」这一类具体回归,但正则天然
与拼写耦合——语义等价的改写(比如把 ``!=`` 换成 ``NOT (... = ...)`` 这种双重否定)
可能漏检,对无害的合法重构也可能假红,违反 AGENTS.md「测可观察行为与语义恒等,
不测抄写的实现」。

这份文件反过来:不管背后是常量、拼接、还是别的什么写法,只**播种真实数据**——
把一本种子笔记本直接 UPDATE 成 ``status='deleting'``(生产目前没有任何写入路径
会产出这个值,但测试可以直接播种,不受此限;``copying`` 同理),再逐一调用
40 处收敛所覆盖的**读方法**,断言 deleting/copying 均不可见、active 对照组可见。
这才是这条不变量真正要保的东西——字面拼写只是达成它的手段之一。

**覆盖清单**(按方法清点,SQLite 侧全量):
``notebook_store.get_row``、``notebook_store.indexing_pipeline_state``、
``query_store.summary_notebook_row``(单库详情行,俗称「notebook_row」)、
``query_store.owned_notebook_rows``、``query_store.joined_notebook_rows``、
``query_store.granted_notebook_rows``、``query_store.list_user_notebooks``、
``query_store.list_user_usage``、``query_store.list_user_activity``(点查与
无界两种调用形都覆盖)、``query_store.notebook_exists_for_owner``、
``query_store.notebook_analytics``、``query_store.pending_actions_projection_rows``、
``query_store.search_notebook``、``group_store.list_group_shared_notebooks``、
``knowledge_counts_cache.warm_all``(选取面)、mount 闸(``MOUNT_VALID_EXPR`` 实际
走到的挂载参与集解析入口 ``notebook_store.participant_ids``)。

**PG 侧的取舍**:两个后端共享同一份 ``NOTEBOOK_LIVE_SQL`` 常量字符串(逐字相等已由
``test_notebook_live_status_literal_guard.py::
test_diag_db_notebook_live_predicate_matches_access_sql`` 钉住),行为差异只可能
来自各自 SQL 方言的拼接是否正确套用了这份常量——所以 PG 侧不必把 16 个方法全量
重复一遍,只在 ``tests/postgres/test_notebook_lifecycle_visibility_pg.py`` 抽查
``query_store`` 的 4-5 个代表性方法 + mount 闸,理由与取舍写在那份文件的模块
docstring 里。

**变异验证**(见 PR 报告,已实测):把任一方法内部的谓词改回旧式
``status != 'copying'``(即放行 deleting)会让对应的行为断言变红。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.sqlite.knowledge_counts_cache import (
    type_status_counts as _real_type_status_counts,
)
from app.services.sqlite_repository import SQLiteRepository


NOW = "2026-09-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _insert_user(db, user_id: str) -> None:
    db.execute(
        "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (user_id, f"{user_id}@x", user_id.upper(), "user", "active", user_id, NOW, NOW),
    )


def _insert_notebook(db, nid: str, owner: str, status: str = "ready") -> None:
    db.execute(
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (nid, f"NB-{nid}", owner, status, NOW, NOW),
    )


@pytest.fixture
def lifecycle(repo):
    """一套贯穿全部 16 个读方法的种子数据。

    三本「被观测」的笔记本(active/copying/deleting)全部**先以正常状态**建好、
    挂满全部辅助数据(成员、群组授权边、挂载边、ask_jobs、sources),**最后**才
    把 copying/deleting 两本直接 UPDATE 翻转状态——这正是真实场景:一本已经建
    好、有成员有分享有挂载的库,进入删除流程后不该再从任何读路径冒出来。
    """
    owner_id = "u-owner"
    member_id = "u-member"
    active_id, copying_id, deleting_id = "nb-active", "nb-copying", "nb-deleting"
    viewer_id = "nb-viewer"
    group_id = "grp-lifecycle"

    with repo._write() as db:
        _insert_user(db, owner_id)
        _insert_user(db, member_id)
        for nid in (active_id, copying_id, deleting_id, viewer_id):
            _insert_notebook(db, nid, owner_id)

        # 只读成员(joined_notebook_rows)。
        for nid in (active_id, copying_id, deleting_id):
            db.execute(
                "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
                "VALUES (?,?,?,?)",
                (nid, member_id, "viewer", NOW),
            )

        # 群组授权边(granted_notebook_rows / list_group_shared_notebooks)。
        db.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,owner_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (group_id, "lifecycle-group", "project", "", owner_id, owner_id, NOW, NOW),
        )
        db.execute(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES (?,?,?,?,?)",
            (group_id, member_id, "member", NOW, owner_id),
        )
        for i, nid in enumerate((active_id, copying_id, deleting_id)):
            db.execute(
                "INSERT INTO notebook_grants "
                "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"gnt-{i}", nid, "group", group_id, "viewer", owner_id, NOW),
            )

        # 挂载边(mount 闸):viewer 库把三本都挂成 base,三者与 viewer 同 owner,
        # 只有 status 是唯一的差异变量。
        for nid in (active_id, copying_id, deleting_id):
            db.execute(
                "INSERT INTO notebook_bases (notebook_id,base_notebook_id,created_at,created_by) "
                "VALUES (?,?,?,?)",
                (viewer_id, nid, NOW, owner_id),
            )

        # ask_jobs(list_user_activity 点查形态)。
        for i, nid in enumerate((active_id, copying_id, deleting_id)):
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,created_by,mode,question,status,asked_at,answer_id,error,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"ask-{i}", nid, owner_id, "chunk", "q?", "completed", "", "", "", NOW, NOW),
            )

        # sources(list_user_activity 无界形态)。
        for i, nid in enumerate((active_id, copying_id, deleting_id)):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,parse_status,file_name,"
                "error_message,created_at,updated_at,uploaded_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"src-{i}", nid, "Doc", "pdf", "parsed", "parsed", "doc.pdf", "",
                 NOW, NOW, owner_id),
            )

        # 全部辅助数据挂完之后,才把两本翻转成半拷贝/删除中——真实的时序。
        db.execute("UPDATE notebooks SET status='copying' WHERE id=?", (copying_id,))
        db.execute("UPDATE notebooks SET status='deleting' WHERE id=?", (deleting_id,))

    return {
        "owner_id": owner_id,
        "member_id": member_id,
        "active_id": active_id,
        "copying_id": copying_id,
        "deleting_id": deleting_id,
        "viewer_id": viewer_id,
        "group_id": group_id,
    }


def test_get_row(repo, lifecycle):
    store = repo._runtime.notebook_store
    row = store.get_row(lifecycle["active_id"])
    assert row["id"] == lifecycle["active_id"]
    with pytest.raises(KeyError):
        store.get_row(lifecycle["copying_id"])
    with pytest.raises(KeyError):
        store.get_row(lifecycle["deleting_id"])


def test_indexing_pipeline_state(repo, lifecycle):
    store = repo._runtime.notebook_store
    state = store.indexing_pipeline_state(lifecycle["active_id"])
    assert isinstance(state, dict)
    with pytest.raises(KeyError):
        store.indexing_pipeline_state(lifecycle["copying_id"])
    with pytest.raises(KeyError):
        store.indexing_pipeline_state(lifecycle["deleting_id"])


def test_summary_notebook_row(repo, lifecycle):
    queries = repo._runtime.queries
    with repo._connect() as db:
        assert queries.summary_notebook_row(db, lifecycle["active_id"]) is not None
        assert queries.summary_notebook_row(db, lifecycle["copying_id"]) is None
        assert queries.summary_notebook_row(db, lifecycle["deleting_id"]) is None


def test_owned_notebook_rows(repo, lifecycle):
    # owner_id 名下除 active/copying/deleting 三本观测对象外还有 viewer(挂载测试
    # 专用,同样是正常状态、理应可见)——期望集合是 {active, viewer},不是单例。
    queries = repo._runtime.queries
    with repo._connect() as db:
        ids = {r["id"] for r in queries.owned_notebook_rows(db, lifecycle["owner_id"])}
    assert ids == {lifecycle["active_id"], lifecycle["viewer_id"]}


def test_joined_notebook_rows(repo, lifecycle):
    queries = repo._runtime.queries
    with repo._connect() as db:
        ids = {r["id"] for r in queries.joined_notebook_rows(db, lifecycle["member_id"])}
    assert ids == {lifecycle["active_id"]}


def test_granted_notebook_rows(repo, lifecycle):
    queries = repo._runtime.queries
    with repo._connect() as db:
        ids = {r["id"] for r in queries.granted_notebook_rows(db, lifecycle["member_id"])}
    assert ids == {lifecycle["active_id"]}


def test_list_user_notebooks(repo, lifecycle):
    rows = repo.list_user_notebooks(lifecycle["owner_id"])
    assert {r["id"] for r in rows} == {lifecycle["active_id"], lifecycle["viewer_id"]}


def test_list_user_usage(repo, lifecycle):
    rows = repo.list_user_usage()
    mine = next(r for r in rows if r["id"] == lifecycle["owner_id"])
    # active + viewer 两本可见笔记本;copying/deleting 不计入。
    assert mine["notebooks"] == 2


def test_list_user_activity_scoped(repo, lifecycle):
    active = repo.list_user_activity(
        lifecycle["owner_id"], notebook_id=lifecycle["active_id"], activity_type="ask"
    )
    assert [item["id"] for item in active["items"]] == ["ask-0"]

    copying = repo.list_user_activity(
        lifecycle["owner_id"], notebook_id=lifecycle["copying_id"], activity_type="ask"
    )
    assert copying == {"items": [], "has_more": False, "next_cursor": None}

    deleting = repo.list_user_activity(
        lifecycle["owner_id"], notebook_id=lifecycle["deleting_id"], activity_type="ask"
    )
    assert deleting == {"items": [], "has_more": False, "next_cursor": None}


def test_list_user_activity_unscoped(repo, lifecycle):
    """无界形态(不传 notebook_id):走 owned_notebook_ids 聚合那条腿,非 ask 类型
    以避开「无界 ask-only 总览」那条刻意不同的口径(见 list_user_activity
    docstring)。"""
    result = repo.list_user_activity(lifecycle["owner_id"], activity_type="source")
    ids = {item["id"] for item in result["items"]}
    assert ids == {"src-0"}, ids


def test_notebook_exists_for_owner(repo, lifecycle):
    assert repo.notebook_exists_for_owner(lifecycle["active_id"], lifecycle["owner_id"]) is True
    assert repo.notebook_exists_for_owner(lifecycle["copying_id"], lifecycle["owner_id"]) is False
    assert repo.notebook_exists_for_owner(lifecycle["deleting_id"], lifecycle["owner_id"]) is False


def test_notebook_analytics(repo, lifecycle):
    repo.notebook_analytics(lifecycle["active_id"])
    with pytest.raises(KeyError):
        repo.notebook_analytics(lifecycle["copying_id"])
    with pytest.raises(KeyError):
        repo.notebook_analytics(lifecycle["deleting_id"])


def test_pending_actions_projection_rows(repo, lifecycle):
    result = repo.pending_actions_projection_rows(lifecycle["owner_id"])
    assert set(result["notebook_ids"]) == {lifecycle["active_id"], lifecycle["viewer_id"]}


def test_search_notebook(repo, lifecycle):
    repo.search_notebook(lifecycle["active_id"], "")
    with pytest.raises(KeyError):
        repo.search_notebook(lifecycle["copying_id"], "")
    with pytest.raises(KeyError):
        repo.search_notebook(lifecycle["deleting_id"], "")


def test_list_group_shared_notebooks(repo, lifecycle):
    rows = repo._runtime.groups.list_group_shared_notebooks(lifecycle["group_id"])
    assert {r["notebook_id"] for r in rows} == {lifecycle["active_id"]}


def test_warm_all_selection_face(repo, lifecycle, monkeypatch):
    """`warm_all` 的选取面(SELECT id 那条语句)——用一个记录型替身拦截四个
    被暖机函数之一实际会被调用到的 notebook_id 集合,不依赖 total 计数(种子库里
    还有一本 viewer_id 也是可见的,总数不是 1)。"""
    from app.repositories.sqlite import knowledge_counts_cache

    seen: list[str] = []

    def _recording(db, notebook_id):
        seen.append(notebook_id)
        return _real_type_status_counts(db, notebook_id)

    monkeypatch.setattr(knowledge_counts_cache, "type_status_counts", _recording)
    with repo._connect() as db:
        knowledge_counts_cache.warm_all(db)

    assert lifecycle["active_id"] in seen
    assert lifecycle["viewer_id"] in seen
    assert lifecycle["copying_id"] not in seen
    assert lifecycle["deleting_id"] not in seen


def test_mount_gate_participant_resolution(repo, lifecycle):
    store = repo._runtime.notebook_store
    with repo._connect() as db:
        ids = set(store.participant_ids(db, lifecycle["viewer_id"]))
    assert ids == {lifecycle["viewer_id"], lifecycle["active_id"]}
