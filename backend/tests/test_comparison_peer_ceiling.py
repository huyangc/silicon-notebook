"""PR-D0·D0-2 — 对比题兄弟实体名的**来源级闸**(逐库冻结来源天花板)。

被关的缺口:``communities.mounted_base_ids`` 只答库维度。一本仍在参与集里的
库,若某实体**只由该库天花板之外的来源**(典型是隐藏 Memory / Knowhow 投影)
支撑,它的名字照样经共提 / 社区成员行被取出来,进 ``ask_chunk`` 的
``sub_queries`` 与 reasoning 的 ``_action_expand_community``,并原样进
``used_queries`` 与可见轨迹。``mounted_base_ids`` 的 docstring 写明这条通道
泄漏的是**查询词本身**——结果侧过滤补救不了,所以闸必须在 SQL 侧、在名字被
取出来之前。

夹具是真 SQLite(真迁移、真 ``store_kg`` / ``rebuild_unified_kg``),因为被测的
正是「实体 → 支撑来源」这条两跳关系在数据层的真实表达:
``concept_clusters.member_object_id`` → ``knowledge_object_sources.source_id``
(evidence[].source_id 打平出来的 P0-4 反向索引)。共提对与社区成员行是**关系**
不是被测对象,直接按真实表结构播种。

生产上今天不可达:没有任何地方构造 ``notebook_source_ceilings``(PR-D 才会),
所以这里一律经 ``source_scope_context(..., notebook_source_ceilings=...)`` 进入。
"""
from __future__ import annotations

import ast
import contextlib
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.communities import CommunityQueryService
from app.services.embedding import FakeEmbedder
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


OPEN_SOURCE = "s-open"          # 天花板之内
HIDDEN_SOURCE = "s-hidden"      # 天花板之外(隐藏投影的替身)

FOCAL = "Focal Widget"          # 焦点实体,只由 s-open 支撑
MIXED = "Mixed Widget"          # 天花板内外共同支撑 → 必须保留
HIDDEN = "Hidden Widget"        # 只由 s-hidden 支撑 → 必须裁掉

CANONICAL = {
    FOCAL: "K-focal widget",
    MIXED: "K-mixed widget",
    HIDDEN: "K-hidden widget",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _objects(source_id, names):
    return [
        {"local_id": f"{source_id}-{index}", "object_type": "concept",
         "payload": {"name": name, "section_path": "1"},
         # evidence[].source_id 是 knowledge_object_sources 的唯一来源,
         # 也是 KG 词法臂来源闸读的同一个关系。
         "evidence": [{"source_id": source_id, "quoted_span": name,
                       "element_id": ""}]}
        for index, name in enumerate(names)
    ]


@pytest.fixture
def library(repo):
    """一本库:FOCAL 与 MIXED 由 s-open 支撑,MIXED 与 HIDDEN 由 s-hidden 支撑。

    共提对与社区成员行按真实表结构播种(代次取 ``unified_kg_state`` 的当前
    published 值,与 ``_PUBLISHED_*_GEN`` 谓词同源)。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for source_id in (OPEN_SOURCE, HIDDEN_SOURCE):
        with repo._write() as db:
            db.execute(
                "INSERT INTO sources (id, notebook_id, title, source_type, "
                "created_at, updated_at) VALUES (?,?,?,'markdown',"
                "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                (source_id, notebook.id, source_id))
    repo.store_kg(notebook.id, OPEN_SOURCE,
                  _objects(OPEN_SOURCE, [FOCAL, MIXED]), [])
    repo.store_kg(notebook.id, HIDDEN_SOURCE,
                  _objects(HIDDEN_SOURCE, [MIXED, HIDDEN]), [])
    repo.rebuild_unified_kg(notebook.id)
    with repo._write() as db:
        row = db.execute(
            "SELECT cluster_generation, community_generation FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook.id,)).fetchone()
        for peer in (MIXED, HIDDEN):
            first, second = sorted((CANONICAL[FOCAL], CANONICAL[peer]))
            db.execute(
                "INSERT INTO concept_comentions "
                "(notebook_id, canonical_a, canonical_b, bridge_claims) VALUES (?,?,?,?)",
                (notebook.id, first, second, 4))
        for name in (FOCAL, MIXED, HIDDEN):
            db.execute(
                "INSERT INTO community_members (canonical_id, notebook_id, level, "
                "community_id, canonical_name, centrality, generation) VALUES (?,?,?,?,?,?,?)",
                (CANONICAL[name], notebook.id, 0, "cm-1", name, 1.0,
                 row["community_generation"]))
        assert row["cluster_generation"] is not None
    return notebook


@pytest.fixture
def service(repo):
    runtime = object.__getattribute__(repo, "_runtime")
    settings = object.__getattribute__(repo, "settings")
    return CommunityQueryService(
        notebooks=runtime.notebook_store,
        unified_kg=runtime.unified_kg,
        event_log=_RecordingLog(runtime.event_log),
        sibling_min_bridge=settings.sibling_min_bridge,
    )


class _RecordingLog:
    def __init__(self, inner):
        self._inner = inner
        self.events: list[dict] = []

    def emit(self, event):
        self.events.append(dict(event))
        self._inner.emit(event)


def _ceilings(notebook_id, *source_ids):
    return {notebook_id: frozenset(source_ids)}


@contextlib.contextmanager
def _traced(repo):
    """记下这条只读连接上真正发出的每一条 SQL。

    ``UnifiedKgStore`` 的社区对比原语自开只读连接,SQLite 侧是本线程复用的那
    一条,所以单线程用例里装一次 trace 回调即可覆盖全部读。
    """
    statements: list[str] = []
    connection = object.__getattribute__(repo, "_runtime").unified_kg.database.connect()
    connection.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        connection.set_trace_callback(None)


def _community_peers(service, notebook_id):
    return service.community_peers(
        notebook_id, FOCAL, "compare", top_k=8, candidates=16)


def _sibling_names(service, notebook_id):
    return [name for name, _claims in service.sibling_peers(notebook_id, FOCAL)]


# --------------------------------------------------------------------------- #
# 1. 只由天花板外来源支撑的名字被裁掉(共提 / 社区成员各一条)
# --------------------------------------------------------------------------- #
def test_peer_name_supported_only_by_hidden_projection_is_dropped_comention(
    repo, library, service
):
    assert _sibling_names(service, library.id) == [HIDDEN, MIXED]
    with source_scope_context(
        library.id, None, None,
        notebook_source_ceilings=_ceilings(library.id, OPEN_SOURCE),
    ):
        assert _sibling_names(service, library.id) == [MIXED]


def test_peer_name_supported_only_by_hidden_projection_is_dropped_community(
    repo, library, service
):
    assert sorted(_community_peers(service, library.id)) == [HIDDEN, MIXED]
    with source_scope_context(
        library.id, None, None,
        notebook_source_ceilings=_ceilings(library.id, OPEN_SOURCE),
    ):
        assert _community_peers(service, library.id) == [MIXED]


# --------------------------------------------------------------------------- #
# 2. 天花板内外共同支撑的实体保留
# --------------------------------------------------------------------------- #
def test_entity_with_mixed_support_survives(repo, library, service):
    """MIXED 的两个成员对象分属两个来源:只要**任一**在天花板内就保留。

    闸问的是「还有没有天花板内来源支撑」,不是「有没有天花板外来源沾过」——
    后者的方向是少给,会把真实召回连坐掉。
    """
    with source_scope_context(
        library.id, None, None,
        notebook_source_ceilings=_ceilings(library.id, OPEN_SOURCE),
    ):
        assert MIXED in _sibling_names(service, library.id)
        assert MIXED in _community_peers(service, library.id)
    with source_scope_context(
        library.id, None, None,
        notebook_source_ceilings=_ceilings(library.id, HIDDEN_SOURCE),
    ):
        assert _sibling_names(service, library.id) == [HIDDEN, MIXED]
        assert sorted(_community_peers(service, library.id)) == [HIDDEN, MIXED]


# --------------------------------------------------------------------------- #
# 3. 无天花板 → 逐值相等,且 SQL 与调用次数逐条不变
# --------------------------------------------------------------------------- #
def test_absent_ceiling_is_byte_identical(repo, library, service):
    """三种「该库没有天花板」的处境必须发出**同一批** SQL。

    第三种是本任务真正的短路判据:别的库有天花板、这一库没有——
    ``source_ceiling_for(owner) is None`` 必须按 ``is not None`` 分,不能被
    「scope 在场」顺手带上闸。
    """
    with _traced(repo) as bare:
        bare_names = (_sibling_names(service, library.id),
                      _community_peers(service, library.id))
    with source_scope_context(library.id, None, None):
        with _traced(repo) as empty_scope:
            empty_names = (_sibling_names(service, library.id),
                           _community_peers(service, library.id))
    with source_scope_context(
        "nb-somewhere-else", None, None,
        notebook_source_ceilings=_ceilings("nb-somewhere-else", OPEN_SOURCE),
    ):
        with _traced(repo) as other_library:
            other_names = (_sibling_names(service, library.id),
                           _community_peers(service, library.id))

    assert bare_names == ([HIDDEN, MIXED], [HIDDEN, MIXED])
    assert empty_names == bare_names and other_names == bare_names
    assert bare and empty_scope == bare and other_library == bare
    for statement in bare:
        assert "knowledge_object_sources" not in statement
        assert "source_index_backfilled" not in statement


# --------------------------------------------------------------------------- #
# 4. 空天花板 = 显式 deny,不是「不限」
# --------------------------------------------------------------------------- #
def test_empty_ceiling_denies_the_library(repo, library, service):
    with source_scope_context(
        library.id, None, None,
        notebook_source_ceilings=_ceilings(library.id),
    ):
        assert _sibling_names(service, library.id) == []
        assert _community_peers(service, library.id) == []
        assert service.resolve_comparison_peers(
            library.id, FOCAL, "compare", top_k=8, candidates=16) == ([], "community")


def test_nominal_active_is_judged_by_its_own_ceiling(repo, library, service):
    """名义 active 没有特权:闸按**传进来的那一库**的天花板判。

    全局问答会给每个参与库(含名义 active)都装一份天花板,而
    ``mounted_base_ids`` 在对等模式下不再剥掉首项——那时名义 active 自己也会
    被当作对比兄弟库传进来。
    """
    with source_scope_context(
        library.id, None, None,
        notebook_source_ceilings={
            library.id: frozenset({OPEN_SOURCE}),
            "nb-peer": frozenset({HIDDEN_SOURCE}),
        },
    ):
        assert _sibling_names(service, library.id) == [MIXED]


# --------------------------------------------------------------------------- #
# 5/6. 两个消费点共用同一道闸;被裁掉的名字不进任何查询 / 事件 / 轨迹
# --------------------------------------------------------------------------- #
def _reasoning_run(repo, library, monkeypatch, ceilings):
    """真跑一次 reasoning 的 ``expand_community``,取回它实际发出的查询与轨迹。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    class _SeqLLM:
        configured = True

        def __init__(self):
            self._reflects = [
                {"next_action": "expand_community", "community_focal": FOCAL,
                 "reason": "需要同类"},
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": FOCAL}]})
            if self._reflects:
                return json.dumps(self._reflects.pop(0))
            return json.dumps({"next_action": "answer", "sufficient": True})

    bind_chat_client(repo, "reasoning_agent", _SeqLLM())
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    # 库维度不是本用例的被测面:固定成「就这一本」,让来源维度单独说话。
    monkeypatch.setattr(
        retriever.communities, "mounted_base_ids", lambda *args: [library.id])
    seen: list[tuple[list[str], str]] = []
    real_resolve = retriever.communities.resolve_comparison_peers

    def _spy(*args, **kwargs):
        result = real_resolve(*args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(
        retriever.communities, "resolve_comparison_peers", _spy)
    # 真正发给检索(进而发给 embedding / 模型)的查询串:泄漏面就是它。
    issued: list[str] = []
    real_search = retriever.search

    def _search(notebook_id, query, *args, **kwargs):
        issued.append(query)
        return real_search(notebook_id, query, *args, **kwargs)

    monkeypatch.setattr(retriever, "search", _search)
    with source_scope_context(
        library.id, None, None, notebook_source_ceilings=ceilings,
    ):
        result = retriever.run(library.id, f"{FOCAL} 相比其他", "")
    return result, seen, issued


def test_chunk_and_reasoning_paths_share_one_gate(repo, library, service, monkeypatch):
    """两个消费点拿到同一份裁剪结果——因为闸在名字的唯一出口。

    reasoning 侧真跑 ``expand_community``;chunk 侧 ``ask_chunk`` 调的是同一个
    ``resolve_comparison_peers``(见下面的静态断言:生产里读那两个 store 方法的
    只有 ``communities.py`` 一个文件),所以在同一 scope 下直调它即可对账。
    """
    ceilings = _ceilings(library.id, OPEN_SOURCE)
    _result, seen, _issued = _reasoning_run(repo, library, monkeypatch, ceilings)
    assert seen, "expand_community 没有真的跑起来,用例失去意义"
    with source_scope_context(
        library.id, None, None, notebook_source_ceilings=ceilings,
    ):
        chunk_side = service.resolve_comparison_peers(
            library.id, FOCAL, f"{FOCAL} 相比其他", top_k=8, candidates=16)
    assert all(names == chunk_side[0] for names, _source in seen)
    assert chunk_side[0] == [MIXED]


def test_only_communities_reads_the_two_peer_store_methods():
    """闸只有一处的**结构**证据:生产里没有第二个读这两个 store 方法的地方。

    两个消费点因此不可能各拿一份口径——绕过 ``communities.py`` 就是新开一条
    未过闸的通道,这条断言会响亮失败。
    """
    app_root = Path(__file__).resolve().parents[1] / "app"
    readers = set()
    for path in app_root.rglob("*.py"):
        if path.parts[-2:] in {("sqlite", "unified_kg_store.py"),
                               ("postgres", "unified_kg_store.py")}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"comention_peers",
                                           "community_member_peers"}):
                readers.add(path.relative_to(app_root.parent).as_posix())
    assert readers == {"app/services/communities.py"}


def test_dropped_name_never_reaches_a_query_or_event(repo, library, service, monkeypatch):
    """被裁掉的名字不出现在发出的查询、事件 payload 与可见轨迹里。

    这是本任务的验收判据本身:泄漏面是**查询词**,所以「答案里没有它」不算数,
    必须是「它从来没被当成查询发出去过」。
    """
    events: list[dict] = []
    runtime = object.__getattribute__(repo, "_runtime")
    monkeypatch.setattr(
        runtime.event_log, "emit", lambda event: events.append(dict(event)))
    result, _seen, issued = _reasoning_run(
        repo, library, monkeypatch, _ceilings(library.id, OPEN_SOURCE))

    attempted = [entry["query"] for entry in result.attempted]
    assert MIXED in attempted, "闸不该把还有天花板内支撑的兄弟一起裁掉"
    assert HIDDEN not in attempted
    assert MIXED in issued and HIDDEN not in issued
    trace_text = json.dumps(
        [step.model_dump() for step in result.trace], ensure_ascii=False)
    assert HIDDEN not in trace_text
    assert HIDDEN not in json.dumps(events, ensure_ascii=False, default=str)
