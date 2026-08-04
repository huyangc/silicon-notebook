"""NotebookSummary.ask_available —— 该库能否在任一可用问答模式下产出有据回答。

前端据此禁用"空库"的问答输入框(codex PR#334 评审:判定所需的隐藏 knowhow chunk、
confirmed memory、base+overlay 配置前端都看不到,故由后端权威计算)。这里钉住每条
证据线索各自都能让 ask_available 为真,尤其 P1-1:无可见来源、零 knowledge_objects
但有可检索 chunk 的 knowhow 表 —— 它可对话,绝不能被判为不可用。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository

NOW = "2026-07-20T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ask_available.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _add_source(db, notebook_id, source_id, source_type):
    db.execute(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (source_id, notebook_id, source_id, source_type, "ready", NOW, NOW),
    )


def _add_chunk(db, notebook_id, source_id, chunk_id):
    db.execute(
        "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
        "VALUES (?,?,?,?,?)",
        (chunk_id, notebook_id, source_id, "some retrievable text", NOW),
    )


def _add_kg_object(db, notebook_id, object_id, status="approved"):
    db.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (object_id, notebook_id, "concept", status, NOW, NOW),
    )


def _add_memory(db, notebook_id, user_id, memory_id, status):
    db.execute(
        "INSERT INTO memory_items "
        "(id,notebook_id,created_by,origin,status,title,content_md,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (memory_id, notebook_id, user_id, "external_agent", status,
         "t", "c", NOW, NOW),
    )


def test_empty_notebook_is_not_ask_available(repo):
    """报告的 bug 本体:全空的新库 —— 无来源/无 chunk/无 KG/无参考库/无 memory —— 该禁。"""
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo.get_notebook(nb.id).ask_available is False


def test_visible_source_with_chunk_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="doc"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-doc", "document")
        _add_chunk(db, nb.id, "s-doc", "c-doc")
    assert repo.get_notebook(nb.id).ask_available is True


def test_knowhow_only_chunks_are_ask_available(repo):
    """P1-1 反向护栏:无锚点列的 knowhow 表产出可检索 chunk 但零 knowledge_objects,
    且其源是 source_type='knowhow' 的隐藏合成源(被 visible_source_count 排除)。
    visible_sources=0 且 kg_ready=False,但 chunk 模式能答 —— 必须 ask_available=True。"""
    nb = repo.create_notebook(NotebookCreate(name="knowhow-only"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-knowhow", "knowhow")
        _add_chunk(db, nb.id, "s-knowhow", "c-knowhow")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["sources"] == 0   # 可见来源确实为 0
    assert summary.kg_ready is False        # 确实无 knowledge_objects
    assert summary.ask_available is True     # 但仍可对话


def test_kg_ready_notebook_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="kg"))
    with repo._write() as db:
        _add_kg_object(db, nb.id, "ko-1")
    summary = repo.get_notebook(nb.id)
    assert summary.kg_ready is True
    assert summary.ask_available is True


def test_deprecated_only_kg_is_not_ask_available(repo):
    """P2-1 反向护栏:本库只有 deprecated 的 knowledge_objects(无 chunk/参考库/memory)。
    检索排除 deprecated,故没有可用证据——kg_ready 为真(既有全应用口径含 deprecated)但
    ask_available 必须为假,不能只因"建过图"就放行。"""
    nb = repo.create_notebook(NotebookCreate(name="deprecated-kg"))
    with repo._write() as db:
        _add_kg_object(db, nb.id, "ko-dep", status="deprecated")
    summary = repo.get_notebook(nb.id)
    assert summary.kg_ready is True          # 既有口径:建过图
    assert summary.ask_available is False     # 但无可用证据,禁止对话


def test_deprecated_only_base_kg_is_not_ask_available(repo):
    """P2-1 反向护栏(参考库侧):挂载的参考库只有 deprecated KG —— base_kg_available
    为真(含 deprecated)但没有可用证据,ask_available 必须为假。"""
    base = repo.create_notebook(NotebookCreate(name="dep-ref"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base-dep", status="deprecated")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-dep-base"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    summary = repo.get_notebook(nb.id)
    assert summary.base_kg_available is True   # 既有口径:挂的参考库建过图
    assert summary.ask_available is False       # 但只有 deprecated,无可用证据


def test_mounted_base_with_kg_is_ask_available(repo):
    """本库无自有内容,但挂载了一个有 KG 的参考库 —— 严格模式可借用,故可对话。"""
    base = repo.create_notebook(NotebookCreate(name="ref"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-base"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    summary = repo.get_notebook(nb.id)
    assert summary.base_kg_available is True
    assert summary.ask_available is True


def test_confirmed_memory_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-1", "confirmed")
    assert repo.get_notebook(nb.id).ask_available is True


def test_candidate_only_memory_is_not_ask_available(repo):
    """只有 candidate memory(无来源/KG/参考库)—— candidate 不作证据,该禁。
    钉住 ask_available 用的是 confirmed-only 判定,而非 counts["memories"](含候选)。"""
    nb = repo.create_notebook(NotebookCreate(name="candidate-mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-cand", "candidate")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["memories"] == 1   # 计数看得到候选
    assert summary.ask_available is False     # 但候选不让对话可用


# ---------------------------------------------------------------------------
# local_evidence_available —— ask_available 的**本地那一半**。
#
# ask_available 是合并后的单个布尔,分不出「有得可搜」是本地撑起来的还是参考库撑
# 起来的;而「本次请求把参考库全部取消勾选后还剩不剩东西」恰恰只能问本地那半。少
# 了它,后端只能退化成拿**可见导入来源数**代表本地证据宇宙,于是一个只有 Knowhow
# 表(或只有已确认 Memory)的库会被误判成空范围。
# ---------------------------------------------------------------------------


def test_local_evidence_available_is_false_on_an_empty_notebook(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    summary = repo.get_notebook(nb.id)
    assert summary.ask_available is False
    assert summary.local_evidence_available is False


def test_knowhow_only_notebook_has_local_evidence_without_a_visible_source(repo):
    """判据的存在理由:可见来源为 0,本地证据仍为真。"""
    nb = repo.create_notebook(NotebookCreate(name="knowhow-only"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-knowhow", "knowhow")
        _add_chunk(db, nb.id, "s-knowhow", "c-knowhow")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["sources"] == 0
    assert summary.local_evidence_available is True


def test_confirmed_memory_alone_counts_as_local_evidence(repo):
    nb = repo.create_notebook(NotebookCreate(name="mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-1", "confirmed")
    assert repo.get_notebook(nb.id).local_evidence_available is True


def test_candidate_only_memory_is_not_local_evidence(repo):
    """便宜预过滤(counts["memories"] > 0)是 confirmed 的严格超集,不能替代它。"""
    nb = repo.create_notebook(NotebookCreate(name="candidate-mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-cand", "candidate")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["memories"] == 1
    assert summary.local_evidence_available is False


def test_mounted_base_kg_alone_is_ask_available_but_not_local_evidence(repo):
    """本条就是整个字段存在的场景:ask_available 为真、本地却什么都没有。

    「把参考库全部取消勾选」之后这个库确实无据可答,而 ask_available 一个人答不出
    这件事。
    """
    base = repo.create_notebook(NotebookCreate(name="ref"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-base"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    summary = repo.get_notebook(nb.id)
    assert summary.ask_available is True
    assert summary.local_evidence_available is False


def test_local_evidence_defaults_false_on_the_list_projection(repo):
    """列表投影不精确回填,默认必须是 **False** —— 与 ask_available 的默认 True 方向
    相反,但遵循同一条规则:「未回填时逐字复现该字段存在之前的行为」。消费侧写成
    「local_evidence_available 或 可见来源数>0」,所以 False 就等于回落到旧判据、只增
    不减;默认 True 反而会让列表投影凭空放行一次空范围检索。"""
    nb = repo.create_notebook(NotebookCreate(name="listed"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-doc", "document")
        _add_chunk(db, nb.id, "s-doc", "c-doc")
    listed = {row.id: row for row in repo.list_notebooks()}
    assert listed[nb.id].local_evidence_available is False
    assert repo.get_notebook(nb.id).local_evidence_available is True


# ---------------------------------------------------------------------------
# base_kg_notebook_ids —— base_kg_available 的**分解**。
#
# 参考库现在可以按库取消勾选,而聚合布尔分不出「这次勾选的库里有没有带图的」:本库
# 无图、用户又恰好取消勾选了唯一带图的那个参考库时,聚合布尔仍为真,界面就会放行一个
# 这轮根本取不到图的模式,还会点名一个本轮不参与的库。后端的知识图谱可用性闸早已按库
# 维度收窄,界面必须能做同样的判断。
#
# 两条硬约束:①与 base_kg_available **自洽**(非空 ⟺ 为真);②**零新增查询**——数据来自
# mounted_bases_row 本来就返回的每行 has_kg。
# ---------------------------------------------------------------------------


def _mounted_base_with_kg(repo, name: str, *, with_kg: bool):
    base = repo.create_notebook(NotebookCreate(name=name))
    if with_kg:
        with repo._write() as db:
            _add_kg_object(db, base.id, f"ko-{name}")
    repo.mark_notebook_base(base.id)
    return base


def test_base_kg_notebook_ids_names_only_the_bases_that_have_a_graph(repo):
    with_kg = _mounted_base_with_kg(repo, "ref-with-kg", with_kg=True)
    without_kg = _mounted_base_with_kg(repo, "ref-without-kg", with_kg=False)
    nb = repo.create_notebook(NotebookCreate(name="mounts-both"))
    repo.replace_notebook_bases(nb.id, [with_kg.id, without_kg.id], "user-local")

    summary = repo.get_notebook(nb.id)
    assert {b.id for b in summary.base_notebooks} == {with_kg.id, without_kg.id}
    assert summary.base_kg_notebook_ids == [with_kg.id]


@pytest.mark.parametrize(
    "mounted",
    [
        pytest.param([], id="no-mount"),
        pytest.param([False], id="one-without-kg"),
        pytest.param([True], id="one-with-kg"),
        pytest.param([True, False], id="mixed"),
        pytest.param([False, False], id="none-with-kg"),
    ],
)
def test_base_kg_notebook_ids_is_self_consistent_with_base_kg_available(repo, mounted):
    """非空 ⟺ base_kg_available 为真。两者出自同一次读取的同一批行,分叉即矛盾 ——
    消费侧(前端严格推理门控)把新字段当成旧布尔的分解在读,一旦不自洽,门控与提示
    文案就会各说各的。"""
    bases = [
        _mounted_base_with_kg(repo, f"ref-{i}", with_kg=has_kg)
        for i, has_kg in enumerate(mounted)
    ]
    nb = repo.create_notebook(NotebookCreate(name="mounts"))
    repo.replace_notebook_bases(nb.id, [b.id for b in bases], "user-local")

    summary = repo.get_notebook(nb.id)
    assert bool(summary.base_kg_notebook_ids) is summary.base_kg_available
    assert set(summary.base_kg_notebook_ids) <= {b.id for b in summary.base_notebooks}


def test_base_kg_notebook_ids_is_filled_on_the_list_projection_too(repo):
    """回填路径与 base_kg_available **逐字相同**(都在 from_row 里),因为它们是同一份
    事实的两种形状 —— 一个在列表里有值、另一个没有,自洽约束就会在列表投影上破掉。"""
    base = _mounted_base_with_kg(repo, "ref", with_kg=True)
    nb = repo.create_notebook(NotebookCreate(name="listed"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")

    listed = {row.id: row for row in repo.list_notebooks()}[nb.id]
    assert listed.base_kg_available is True
    assert listed.base_kg_notebook_ids == [base.id]


def test_base_kg_notebook_ids_costs_no_extra_query(repo, monkeypatch):
    """零新增查询:数据来自 mounted_bases_row **本来就返回的每行 has_kg**。

    两条判据:
      * 整个 get_notebook 里只有**一条**语句携带 has_kg —— 没有为「哪几个库有图」
        另开一次查询;
      * 挂 1 个库与挂 3 个库执行的语句**逐条相同** —— 代价与挂载数无关,不存在
        per-base 的 N+1 追问。
    """
    one = repo.create_notebook(NotebookCreate(name="mounts-1"))
    repo.replace_notebook_bases(
        one.id, [_mounted_base_with_kg(repo, "r1", with_kg=True).id], "user-local"
    )
    three = repo.create_notebook(NotebookCreate(name="mounts-3"))
    repo.replace_notebook_bases(
        three.id,
        [
            _mounted_base_with_kg(repo, "r2", with_kg=True).id,
            _mounted_base_with_kg(repo, "r3", with_kg=False).id,
            _mounted_base_with_kg(repo, "r4", with_kg=True).id,
        ],
        "user-local",
    )
    # 预热开放路径的进程内计数缓存,让下面两次追踪只剩稳定语句。
    repo.get_notebook(one.id)
    repo.get_notebook(three.id)

    sql: list[str] = []
    database = repo._runtime.database
    orig_connect = database.connect

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, *a, **kw):
            sql.append(statement)
            return self._inner.execute(statement, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    monkeypatch.setattr(database, "connect", lambda: _SpyConn(orig_connect()))

    summary_one = repo.get_notebook(one.id)
    trace_one = list(sql)
    sql.clear()
    summary_three = repo.get_notebook(three.id)
    trace_three = list(sql)

    assert len(summary_one.base_kg_notebook_ids) == 1
    assert len(summary_three.base_kg_notebook_ids) == 2   # r3 无图
    carriers = [s for s in trace_three if "has_kg" in s]
    assert len(carriers) == 1, (
        f"只应有 mounted_bases_row 一条语句携带 has_kg,实际 {len(carriers)} 条:{carriers}"
    )
    assert trace_one == trace_three, (
        "挂 1 个库与挂 3 个库的语句序列必须逐条相同 —— 不相同就说明有 per-base 的追问"
    )
