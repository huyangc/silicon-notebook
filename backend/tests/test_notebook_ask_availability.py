"""NotebookSummary.ask_available —— 该库能否在任一可用问答模式下产出有据回答。

前端据此禁用"空库"的问答输入框(codex PR#334 评审:判定所需的隐藏 knowhow chunk、
confirmed memory、base+overlay 配置前端都看不到,故由后端权威计算)。这里钉住每条
证据线索各自都能让 ask_available 为真,尤其 P1-1:无可见来源、零 knowledge_objects
但有可检索 chunk 的 knowhow 表 —— 它可对话,绝不能被判为不可用。
"""
from __future__ import annotations

import re

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository

NOW = "2026-07-20T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ask_available.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.request_context import reset_request_user, set_request_user

    instance = SQLiteRepository(Settings(_env_file=None))
    # The summary is the requesting user's (Memory counts, mounts): the
    # seeded local account asks, explicitly -- a summary read with no request
    # user has no viewer.
    marker = set_request_user(instance.current_user())
    try:
        yield instance
    finally:
        reset_request_user(marker)


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


# ---------------------------------------------------------------------------
# 参考库那一半的**原文**判据 —— 无图参考库的可见原文也撑得起 ask_available。
#
# 联邦 chunk 通道给参与集里每个库各发一条召回腿,所以「空笔记本 + 只挂了一个无图
# 参考库」的原文本来就检索得到;旧判据只认「参考库有可用知识图谱」,于是前端锁死输入
# 框、直连 POST /ask 被拦成 409。新判据与联邦同行腿同口径:对查看者生效的挂载库、
# 落在 CHUNK_FEDERATION_MAX_PARTICIPANTS 上界之内、只数可见来源的 chunk。
# ---------------------------------------------------------------------------


def _graphless_library(repo, name, *, chunk_source_type="document", chunked=True):
    """一个**无图**的私有参考库(与挂载方同 owner,不是公共库):一篇来源,
    ``chunked`` 时带一条 chunk。"""
    library = repo.create_notebook(NotebookCreate(name=name))
    with repo._write() as db:
        _add_source(db, library.id, f"s-{name}", chunk_source_type)
        if chunked:
            _add_chunk(db, library.id, f"s-{name}", f"c-{name}")
    return library


def _empty_notebook_mounting(repo, libraries, name="mounts-graphless"):
    nb = repo.create_notebook(NotebookCreate(name=name))
    repo.replace_notebook_bases(
        nb.id, [library.id for library in libraries], repo.current_user().id
    )
    return nb


def test_graphless_library_with_visible_chunks_is_ask_available(repo):
    """缺口本体:本库零证据、参考库无图但有可见原文 —— 必须可提问。"""
    library = _graphless_library(repo, "ref-text")
    nb = _empty_notebook_mounting(repo, [library])
    summary = repo.get_notebook(nb.id)
    assert summary.base_kg_available is False        # 前提:参考库确实无图
    assert summary.local_evidence_available is False  # 前提:本地确实什么都没有
    assert summary.ask_available is True


def test_graphless_library_without_chunks_is_not_ask_available(repo):
    """参考库有来源、却一条 chunk 都没有(解析中 / 解析失败)—— 仍拦。"""
    library = _graphless_library(repo, "ref-unparsed", chunked=False)
    nb = _empty_notebook_mounting(repo, [library])
    assert repo.get_notebook(nb.id).ask_available is False


def test_library_with_only_hidden_projection_chunks_is_not_ask_available(repo):
    """参考库只有 Knowhow 格子的 chunk:联邦同行腿只取可见来源的原文,隐藏投影到不了
    跨库通道,所以它撑不起本库的可用性(本库自己的 Knowhow 格子才算本地证据)。"""
    library = _graphless_library(repo, "ref-knowhow", chunk_source_type="knowhow")
    nb = _empty_notebook_mounting(repo, [library])
    assert repo.get_notebook(nb.id).ask_available is False


def test_library_the_viewer_cannot_read_does_not_make_ask_available(repo):
    """权限只在检索层审:A 的主人把自己的**私有**无图库挂到共享笔记本 A 上,这条挂载
    只对他自己生效(M3)。读不到那个库的成员 Bob 不能因为它有 chunk 就被放行。"""
    from app.api.ask_routes import _require_ask_available
    from app.core.request_context import reset_request_user, set_request_user
    from fastapi import HTTPException

    library = _graphless_library(repo, "ref-private")
    nb = _empty_notebook_mounting(repo, [library], name="shared-a")
    bob = repo._runtime.identity.create_user("c00140001", "pw")
    repo._runtime.sharing.add_member(nb.id, bob.id)

    # 挂载人自己:生效,可提问(正对照,证明下面的「拦」不是别的原因)。
    assert repo.get_notebook(nb.id).ask_available is True

    marker = set_request_user(bob)
    try:
        summary = repo.get_notebook(nb.id)
        assert summary.base_notebooks == []
        assert summary.ask_available is False
        with pytest.raises(HTTPException) as refused:
            _require_ask_available(summary)
        assert refused.value.status_code == 409
    finally:
        reset_request_user(marker)


def test_library_passages_do_not_count_when_federation_is_off(repo):
    """``CHUNK_FEDERATION_ENABLED=0``:参与集收成当前库一本,参考库原文根本不进召回,
    判据跟着同一把开关回到改动前。"""
    library = _graphless_library(repo, "ref-text")
    nb = _empty_notebook_mounting(repo, [library])
    assert repo._runtime.notebook_summaries.settings is repo.settings
    repo.settings.chunk_federation_enabled = False
    assert repo.get_notebook(nb.id).ask_available is False


@pytest.mark.parametrize(
    ("with_text", "expected"),
    [
        pytest.param("a-first", True, id="inside-the-bound"),
        pytest.param("b-second", False, id="past-the-bound"),
    ],
)
def test_only_libraries_inside_the_federation_bound_count(repo, with_text, expected):
    """参与上界 2 = 当前库 + 一个参考库(按 MOUNT_ORDER,私有库按名字排)。排在上界
    之外的库联邦通道检索不到,也就不能替这个库撑起可用性。"""
    first = _graphless_library(repo, "a-first", chunked=with_text == "a-first")
    second = _graphless_library(repo, "b-second", chunked=with_text == "b-second")
    nb = _empty_notebook_mounting(repo, [second, first])
    repo.settings.chunk_federation_max_participants = 2
    assert [ref.id for ref in repo.get_notebook(nb.id).base_notebooks] == [
        first.id, second.id
    ]
    assert repo.get_notebook(nb.id).ask_available is expected


def _traced_statements(repo, monkeypatch):
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
    return sql


def test_library_passage_probe_costs_one_statement_whatever_the_mount_count(
    repo, monkeypatch,
):
    """新判据是**一条** EXISTS,名单绑成一个参数:挂 1 / 5 / 50 个无图空库时
    ``get_notebook`` 的语句序列逐条相同(50 个被联邦上界截成 7 个,语句文本不变),
    不存在逐库追问。"""
    notebooks = {}
    for count in (1, 5, 50):
        libraries = [
            _graphless_library(repo, f"r{count}-{i:02d}", chunked=False)
            for i in range(count)
        ]
        notebooks[count] = _empty_notebook_mounting(
            repo, libraries, name=f"mounts-{count}"
        )
        repo.get_notebook(notebooks[count].id)   # 预热进程内计数缓存
    sql = _traced_statements(repo, monkeypatch)
    traces = {}
    for count, nb in notebooks.items():
        sql.clear()
        assert repo.get_notebook(nb.id).ask_available is False
        traces[count] = list(sql)
    probes = [s for s in traces[5] if "FROM chunks c" in s]
    assert len(probes) == 1, probes
    assert traces[1] == traces[5] == traces[50]


def test_notebook_with_local_evidence_never_runs_the_library_probe(
    repo, monkeypatch,
):
    """绝大多数库本地就有 chunk:短路在前,打开路径一条新语句都不多付。"""
    library = _graphless_library(repo, "ref-text")
    nb = repo.create_notebook(NotebookCreate(name="own-doc"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-own", "document")
        _add_chunk(db, nb.id, "s-own", "c-own")
    repo.replace_notebook_bases(nb.id, [library.id], repo.current_user().id)
    repo.get_notebook(nb.id)
    sql = _traced_statements(repo, monkeypatch)
    assert repo.get_notebook(nb.id).ask_available is True
    assert not [s for s in sql if "FROM chunks c" in s]


def test_list_projection_never_runs_the_library_probe(repo, monkeypatch):
    """笔记本列表是热路径:列表投影不计算 ask_available(恒为默认 True),新判据不得
    把它拉回逐库查询。"""
    library = _graphless_library(repo, "ref-text")
    nb = _empty_notebook_mounting(repo, [library])
    repo.list_notebooks()
    sql = _traced_statements(repo, monkeypatch)
    listed = {row.id: row for row in repo.list_notebooks()}
    assert listed[nb.id].ask_available is True
    assert not [s for s in sql if "FROM chunks c" in s]


@pytest.mark.parametrize(
    ("chunked", "expected_status"),
    [
        pytest.param(True, 200, id="graphless-library-with-text"),
        pytest.param(False, 409, id="graphless-library-without-text"),
    ],
)
@pytest.mark.parametrize("mode", ["chunk", "reasoning"])
@pytest.mark.parametrize("enum_tools", [False, True], ids=["enum-off", "enum-on"])
def test_http_ask_on_an_empty_notebook_follows_the_library_text(
    tmp_path, monkeypatch, chunked, expected_status, mode, enum_tools,
):
    """HTTP 端到端:详情里的 ``ask_available``(前端 ``isAskBlocked`` 读的那个字段)
    与 ``POST /ask`` 的 409 预检给出同一个答案——参考库有可见原文就放行,没有就仍拦。

    枚举工具开关两态都跑:它只决定 reasoning 内部的无图早退放行理由,与这道 HTTP 闸
    无关;改动前两态在这里都是 409。"""
    from tests.test_ask_modes_api import _client

    monkeypatch.setenv("REASONING_ENUM_TOOLS_ENABLED", str(enum_tools).lower())
    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository

    repo = repository()
    assert repo.settings.reasoning_enum_tools_enabled is enum_tools
    library = client.post("/api/notebooks", json={"name": "ref"}).json()["id"]
    nb = client.post("/api/notebooks", json={"name": "empty"}).json()["id"]
    with repo._write() as db:
        _add_source(db, library, "s-ref", "document")
        if chunked:
            _add_chunk(db, library, "s-ref", "c-ref")
    repo.replace_notebook_bases(nb, [library], repo.current_user().id)

    detail = client.get(f"/api/notebooks/{nb}").json()
    assert detail["kg_ready"] is False and detail["base_kg_available"] is False
    assert detail["ask_available"] is (expected_status == 200)
    asked = client.post(
        f"/api/notebooks/{nb}/ask", json={"question": "q", "mode": mode}
    )
    assert asked.status_code == expected_status, asked.text


@pytest.mark.parametrize("analyzed", [False, True], ids=["no-stats", "analyzed"])
def test_library_text_probe_plan_seeks_chunks_by_source(repo, analyzed):
    """计划钉:内层 EXISTS 必须按 ``idx_chunks_source`` 探,不能拿 ``idx_chunks_nb``
    把整库 chunk 逐篇来源扫一遍(无统计时计划器恰好会这么选,2 万来源 / 40 万 chunk
    的库实测 127 ms)。生产库不跑 ANALYZE,两种统计状态都钉。"""
    library = _graphless_library(repo, "ref-text")
    # Statistics only mean something over more than a handful of rows: three
    # libraries x 100 sources x 5 chunks (the no-stats state does not care).
    bulk = [repo.create_notebook(NotebookCreate(name=f"bulk-{lib}")).id
            for lib in range(3)]
    with repo._write() as db:
        for lib, other in enumerate(bulk):
            for src in range(100):
                _add_source(db, other, f"s-bulk-{lib}-{src}", "document")
                for chunk in range(5):
                    _add_chunk(db, other, f"s-bulk-{lib}-{src}",
                               f"c-bulk-{lib}-{src}-{chunk}")
    statements: list[tuple[str, tuple]] = []

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, params=()):
            statements.append((statement, tuple(params)))
            return self._inner.execute(statement, params)

    with repo._runtime.database.connect() as db:
        if analyzed:
            db.execute("ANALYZE")
        assert repo._runtime.queries.notebooks_have_visible_chunk(
            _Recorder(db), [library.id]
        ) is True
        (statement, params), = statements
        plan = "\n".join(
            str(row[-1])
            for row in db.execute("EXPLAIN QUERY PLAN " + statement, params).fetchall()
        )
    assert "idx_chunks_source" in plan, plan
    assert "idx_chunks_nb" not in plan, plan
    if not analyzed:
        # The library list drives the outer read (``drive_by``): one seek per
        # listed library on a ``sources(notebook_id, ...)`` index.  A
        # ``member_of`` (``+s.notebook_id``) would turn it into a scan of every
        # source in the database.  Plan wording differs across SQLite builds
        # (3.45 in CI, 3.53 locally), hence the tolerant pattern.
        assert re.search(
            r"SEARCH s USING (?:COVERING )?INDEX \w+ \(notebook_id=\?", plan
        ), plan


@pytest.mark.parametrize(
    ("enabled", "cap"),
    [(True, 8), (True, 3), (True, 1), (False, 8)],
    ids=["default", "cap-3", "cap-1", "switch-off"],
)
def test_availability_counts_exactly_the_libraries_the_chunk_lane_searches(
    repo, enabled, cap,
):
    """同源钉:``_chunk_lane_base_ids`` 与联邦通道 ``federation_participant_ids`` 读的是
    同一个纯函数(``app.domain.chunk_lane``),对同一份挂载给出同一批参考库。"""
    from app.services.chunk_federation import federation_participant_ids

    libraries = [_graphless_library(repo, f"lib-{i}") for i in range(5)]
    nb = _empty_notebook_mounting(repo, libraries)
    repo.settings.chunk_federation_enabled = enabled
    repo.settings.chunk_federation_max_participants = cap
    summary = repo.get_notebook(nb.id)
    counted = repo._runtime.notebook_summaries._chunk_lane_base_ids(summary)
    searched = federation_participant_ids(repo.retrieval.candidates, nb.id) - {nb.id}
    assert set(counted) == searched
    assert counted == [ref.id for ref in summary.base_notebooks][: len(counted)]
