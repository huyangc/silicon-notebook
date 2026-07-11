"""Task 25: ReportStore —— reports 表行级持久化。

golden 逐字对齐 master 行为:CRUD 载荷键集/默认值/排序、缺行 update/delete
静默 no-op、export 的 done-only/顺序/同名后缀/跨库跳过/IN 分批,以及
SourceStore.report_source_rows(报告语料侦察的标题投影)。facade 冻结签名
委托到 runtime 所有的同一个 store。
"""
import pytest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
               "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    return SQLiteRepository(Settings(_env_file=None))


def _mk_nb(repo):
    from app.models.schemas import NotebookCreate
    return repo.create_notebook(NotebookCreate(name="t", purpose="p", primary_domain="d"))


def _golden_store(repo, ids, now):
    """确定性种子的独立 ReportStore(同一 database 边界):id/时钟/用户可控。"""
    from app.repositories.sqlite.report_store import ReportStore
    it = iter(ids)
    return ReportStore(
        repo._runtime.database,
        new_id=lambda prefix: next(it),
        now=lambda: now["v"],
        current_user_id=lambda: "user-golden",
    )


# ---------------------------------------------------------------------------
# runtime 所有权 + facade 冻结签名委托
# ---------------------------------------------------------------------------

def test_runtime_owns_report_store_and_facade_delegates(repo):
    from app.repositories.sqlite.report_store import ReportStore
    store = repo._runtime.report_store
    assert isinstance(store, ReportStore)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "为什么?", depth=3)
    assert store.get_report(nb.id, rid)["depth"] == 3          # facade 写 → store 读同一行
    repo.update_report(nb.id, rid, status="done", content_md="# R")
    assert store.get_report(nb.id, rid)["content_md"] == "# R"
    assert [r["id"] for r in repo.list_reports(nb.id)] == [rid]
    repo.delete_report(nb.id, rid)
    with pytest.raises(KeyError):
        store.get_report(nb.id, rid)


def test_facade_report_row_to_dict_delegates_to_store(repo):
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    with repo._runtime.database.connect() as db:
        row = db.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
    assert repo._report_row_to_dict(row, full=False)["section_count"] == 0
    assert repo._report_row_to_dict(row, full=True)["content_md"] == ""


# ---------------------------------------------------------------------------
# CRUD 载荷/排序 golden(逐字对齐 master)
# ---------------------------------------------------------------------------

def test_report_crud_payload_matches_master_golden(repo):
    nb = _mk_nb(repo)
    now = {"v": "2026-01-01T00:00:00"}
    store = _golden_store(repo, ["rep-a", "rep-b"], now)

    rid = store.create_report(nb.id, "Q one", depth=4)
    assert rid == "rep-a"
    assert store.get_report(nb.id, rid) == {
        "id": "rep-a", "notebook_id": nb.id, "question": "Q one",
        "status": "pending", "progress": "", "error": "",
        "created_by": "user-golden",
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        "depth": 4, "section_count": 0,
        "outline": [], "sections": [], "gaps": [], "references": [],
        "section_status": [], "content_md": "",
    }

    now["v"] = "2026-01-02T00:00:00"
    store.update_report(
        nb.id, rid, status="done", progress="完成", error="",
        outline=[{"title": "机理", "scope": "s", "sub_queries": ["q1"]}],
        sections=[{"title": "机理", "markdown": "md", "grounded": True}],
        gaps=["缺 X"], references=[{"key": "k1", "label": "Razavi"}],
        content_md="# 报告", section_status=[{"title": "机理", "phase": "完成", "step": 0}])
    full = store.get_report(nb.id, rid)
    assert full["created_at"] == "2026-01-01T00:00:00"          # 创建时间不动
    assert full["updated_at"] == "2026-01-02T00:00:00"          # 每次 update 走时钟
    assert full["status"] == "done" and full["content_md"] == "# 报告"
    assert full["section_count"] == 1 and full["gaps"] == ["缺 X"]
    assert full["references"][0]["label"] == "Razavi"
    assert full["section_status"][0]["phase"] == "完成"

    now["v"] = "2026-01-03T00:00:00"
    store.create_report(nb.id, "Q two")                          # 默认 depth=2
    rows = store.list_reports(nb.id)
    assert [r["id"] for r in rows] == ["rep-b", "rep-a"]         # created_at DESC
    assert rows[1]["depth"] == 4 and rows[0]["depth"] == 2
    # 摘要行 golden:无 full 载荷键(content_md/outline/...),含 section_count
    assert set(rows[0]) == {
        "id", "notebook_id", "question", "status", "progress", "error",
        "created_by", "created_at", "updated_at", "depth", "section_count",
    }


def test_list_reports_same_created_at_tie_breaks_by_id(repo):
    nb = _mk_nb(repo)
    now = {"v": "2026-01-01T00:00:00"}
    store = _golden_store(repo, ["rep-b", "rep-a"], now)
    store.create_report(nb.id, "第一个建的 id 反而大")            # rep-b
    store.create_report(nb.id, "第二个建的 id 小")                # rep-a,同秒
    assert [r["id"] for r in store.list_reports(nb.id)] == ["rep-a", "rep-b"]


def test_missing_update_and_delete_remain_silent_noops(repo):
    nb = _mk_nb(repo)
    repo.update_report(nb.id, "rep-none", status="cancelled", progress="已取消")  # 不抛
    repo.delete_report(nb.id, "rep-none")                                          # 不抛
    assert repo.list_reports(nb.id) == []               # zero-row UPDATE 不凭空建行
    with pytest.raises(KeyError):
        repo.get_report(nb.id, "rep-none")


def test_create_report_missing_notebook_raises_keyerror(repo):
    with pytest.raises(KeyError):
        repo.create_report("nb-none", "q")              # facade 的 notebook 存在性守卫


def test_update_report_scopes_to_notebook(repo):
    nb_a, nb_b = _mk_nb(repo), _mk_nb(repo)
    rid = repo.create_report(nb_a.id, "q")
    repo.update_report(nb_b.id, rid, status="done")     # 跨库 rid → no-op
    assert repo.get_report(nb_a.id, rid)["status"] == "pending"
    with pytest.raises(KeyError):
        repo.get_report(nb_b.id, rid)


# ---------------------------------------------------------------------------
# export golden:done-only/传入顺序/同名后缀/跨库与空 id 跳过/IN 分批
# ---------------------------------------------------------------------------

def test_export_reports_matches_master_golden(repo, monkeypatch):
    nb, other = _mk_nb(repo), _mk_nb(repo)
    now = {"v": "2026-01-01T00:00:00"}
    store = _golden_store(repo, ["rep-1", "rep-2", "rep-3", "rep-4", "rep-5"], now)
    r1 = store.create_report(nb.id, "同名/问:题?")
    store.update_report(nb.id, r1, status="done", content_md="# 一")
    r2 = store.create_report(nb.id, "同名/问:题?")               # 同 stem → 后缀去重
    store.update_report(nb.id, r2, status="done", content_md="# 二")
    r3 = store.create_report(nb.id, "未完成")                    # 仍 pending → 跳过
    foreign = store.create_report(other.id, "别人的")
    store.update_report(other.id, foreign, status="done", content_md="# F")

    monkeypatch.setattr(store, "IN_CHUNK", 1)                    # 逼出多批 IN 查询
    out = store.export_reports(nb.id, [r2, r1, r3, foreign, "", "rep-不存在"])
    assert out == [
        ("同名_问_题_-rep-2.md", "# 二"),                        # 传入顺序在前 → 原名
        ("同名_问_题_-rep-1.md", "# 一"),
    ]
    # 同一 rid 传两次:文件名 -1 后缀(master 现行为)
    assert store.export_reports(nb.id, [r1, r1]) == [
        ("同名_问_题_-rep-1.md", "# 一"),
        ("同名_问_题_-rep-1-1.md", "# 一"),
    ]
    # done 但 content_md 空 → 跳过;全部无效 → []
    r4 = store.create_report(nb.id, "空正文")
    store.update_report(nb.id, r4, status="done")
    assert store.export_reports(nb.id, [r4]) == []
    assert store.export_reports(nb.id, []) == []
    # facade 委托同一实现
    assert repo.export_reports(nb.id, [r1])[0][1] == "# 一"


# ---------------------------------------------------------------------------
# SourceStore.report_source_rows:语料侦察标题投影(建档顺序,LIMIT 20)
# ---------------------------------------------------------------------------

def test_source_store_report_source_rows_projection(repo):
    nb = _mk_nb(repo)
    with repo._runtime.database.write() as db:
        for i in range(25):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (f"s{i:02d}", nb.id, f"Title {i:02d}", "file", "uploaded", "parsed",
                 f"2026-01-01T00:00:{i:02d}", "2026"))
    rows = repo._runtime.source_store.report_source_rows(nb.id)
    assert len(rows) == 20                                       # LIMIT 20 侦察上限
    assert [r["title"] for r in rows] == [f"Title {i:02d}" for i in range(20)]
    assert repo._runtime.source_store.report_source_rows("nb-none") == []
