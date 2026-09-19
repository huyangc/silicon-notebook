"""C3 -- 参与集覆盖在检索层的行为合同(真实 SQLite 夹具)。

``test_retrieval_participants.py`` 测覆盖模块自己(替换 fallback、身份复核、
context 本地性),``test_participant_override_guard.py`` 测结构性隔离。本文件测
**接线**:一次装了覆盖的 run,检索的每个读点是不是真的按覆盖集回答,以及覆盖
不在场时是不是逐字回到今天。

夹具刻意用三个**互不挂载**的笔记本:覆盖表达的正是「搜这些库」,包括挂载谓词
根本给不出的组合。任何一条断言若在挂载关系下也成立,它就证明不了覆盖。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.models.source_scope import BaseNotebookScope
from app.services.embedding import FakeEmbedder
from app.services.retrieval_participants import (
    ParticipantOverride,
    ParticipantOverrideError,
    assert_override_matches_run,
    participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


_ACTOR = "user-local"
_QUERY = "共质心版图 matching"
_NOW = "2026-09-20T00:00:00"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    instance.settings.query_rewrite_enabled = False
    return instance


def _seed_source(repo, notebook_id: str, prefix: str) -> str:
    """一篇真解析过的来源:两个段落元素 → 真 chunk(向量 + FTS)。"""
    source_id = f"src-{prefix}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, f"Doc {prefix}", "markdown", "ready",
             "parsed", _NOW, _NOW),
        )
        for index in (1, 2):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,'paragraph',?,?,'{}',?)",
                (f"el-{prefix}-{index}", source_id, f"p{index}",
                 f"{_QUERY} 的要点在文档 {prefix} 第 {index} 节里展开。", _NOW),
            )
    repo._chunk_and_embed_source(source_id)
    return source_id


@pytest.fixture
def islands(repo):
    """三个**互不挂载**的笔记本,各有一篇来源与一个 KG 对象。

    返回 ``(ids, sources)``,``ids[0]`` 是名义 active。
    """
    ids, sources = [], {}
    for prefix in ("a", "b", "c"):
        notebook = repo.create_notebook(NotebookCreate(name=f"island {prefix}"))
        ids.append(notebook.id)
        sources[notebook.id] = _seed_source(repo, notebook.id, prefix)
        repo.store_kg(notebook.id, None, [
            {"local_id": f"K-{prefix}",
             "object_type": "concept",
             "payload": {"name": f"{_QUERY} 概念 {prefix}",
                         "section_path": "1"},
             "evidence": []},
        ], [])
    # 前提确认:三者之间没有任何挂载边,所以挂载谓词只会返回它自己。
    with repo._connect() as db:
        for notebook_id in ids:
            assert repo._runtime.notebook_store.participant_ids(db, notebook_id) == [
                notebook_id
            ]
    return tuple(ids), sources


def _override(notebook_ids, *, tiers=None, actor=_ACTOR) -> ParticipantOverride:
    return ParticipantOverride(
        notebook_ids=tuple(notebook_ids),
        tiers=dict(tiers or {}),
        attested_actor_id=actor,
    )


# ---------------------------------------------------------------------------
# 1. 座位:覆盖集经每条检索腿真的到达三个互不挂载的库
# ---------------------------------------------------------------------------

def test_seat_returns_the_override_set(repo, islands):
    ids, _sources = islands
    active = ids[0]
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids, tiers={ids[1]: "base"})):
            seated = repo.retrieval.candidates._retrieval_participants(active)
    assert seated == (
        (ids[0], "personal"), (ids[1], "base"), (ids[2], "personal"),
    )


def test_seat_re_attests_on_every_read_inside_one_run(repo, islands):
    """座位在覆盖在场时绝不从 run-local memo 里答。

    复现:同一个 run 里先用一份合法覆盖把座位读热,再装一份**成员相同**却声明给
    另一个用户的覆盖——memo 命中会整段跳过身份复核,读到缓存而不是报错;成员换个
    顺序还能绕过名义 active 的检查(指纹对顺序不敏感)。两条都必须抛。
    """
    from app.domain.retrieval_control import ParticipantOverrideError

    ids, _sources = islands
    active = ids[0]
    seat = repo.retrieval.candidates._retrieval_participants
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            assert [nid for nid, _tier in seat(active)] == list(ids)
        with participant_override(_override(ids, actor="someone-else")):
            with pytest.raises(ParticipantOverrideError):
                seat(active)
        reordered = (ids[1], ids[0], ids[2])
        with participant_override(_override(reordered)):
            with pytest.raises(ParticipantOverrideError):
                seat(active)
        # 合法覆盖重新装回来照常工作:前两次失败没有留下任何缓存状态。
        with participant_override(_override(ids)):
            assert [nid for nid, _tier in seat(active)] == list(ids)


def test_seat_is_unchanged_without_an_override(repo, islands):
    """覆盖不在场 -> 挂载谓词的输出逐字不变(这里就是 active 自己一本)。"""
    ids, _sources = islands
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        seated = repo.retrieval.candidates._retrieval_participants(ids[0])
    assert seated == ((ids[0], "personal"),)


def test_library_scope_can_still_narrow_an_override(repo, islands):
    """覆盖是替换,不是豁免:库维度仍然可以把覆盖集再收窄,但不能扩张。"""
    ids, _sources = islands
    active = ids[0]
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            with source_scope_context(
                active, None,
                BaseNotebookScope(mode="include", notebook_ids=[ids[2]]),
            ):
                seated = repo.retrieval.candidates._retrieval_participants(
                    active
                )
    # active 恒被 covers_notebook 保留;ids[1] 未勾选 -> 掉队;ids[2] 勾着 -> 留下。
    assert [nid for nid, _tier in seated] == [active, ids[2]]


def test_federated_chunk_lane_searches_every_override_library(repo, islands):
    """三个互不挂载的库的**原文段落**经 chunk 通道全部到达。

    这条是「覆盖真的改变了搜索面」的主证据:没有覆盖时挂载谓词只给 active 一
    本,所以任何一个外库的段落出现都只可能来自覆盖集。
    """
    ids, sources = islands
    active = ids[0]
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            scored, _ids, _matrix = repo.retrieval.retrieve_chunk_candidates(
                active, _QUERY,
            )
    # 当前库的命中不打 notebook_id(空 = 当前库,既有唯一判据)。
    origins = {chunk.notebook_id or active for chunk in scored}
    assert origins == set(ids), origins
    assert {chunk.source_id for chunk in scored} == set(sources.values())


def test_federated_kg_lane_searches_every_override_library(repo, islands):
    """知识对象腿同样按覆盖集,且 tier 取覆盖声明的值。"""
    ids, _sources = islands
    active = ids[0]
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids, tiers={ids[1]: "base"})):
            hits = repo.retrieval.federated_retrieve(active, _QUERY)
    assert {hit.notebook_id for hit in hits} == set(ids)
    tiers = {hit.notebook_id: hit.tier for hit in hits}
    assert tiers[ids[1]] == "base"
    assert tiers[ids[2]] == "personal"


def test_kg_owner_table_intersects_the_override_set(repo, islands):
    """KG overlay 的归属表与 chunk 腿的参与集求交,覆盖下按覆盖集成立。

    不在覆盖集里的 owner 落回 active(既有语义),所以这条同时钉住「求交仍在」。
    """
    ids, _sources = islands
    active = ids[0]
    id_map = {
        "k1": {"object_id": "o1", "notebook_id": ids[1]},
        "k2": {"object_id": "o2", "notebook_id": "nb-outside"},
    }
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            owners = repo.retrieval.candidates._kg_object_owners(active, id_map)
    assert owners == {"o1": ids[1], "o2": active}


# ---------------------------------------------------------------------------
# 2. 进程级缓存键
# ---------------------------------------------------------------------------

def _graph_cache_keys(repo) -> set[str]:
    return set(repo.retrieval.candidates._vector_cache.keys())


def test_no_override_keeps_byte_identical_cache_key(repo, islands):
    """无覆盖时 ``_federated_rx_graph`` 的键恰为 ``{active}:fed_rxgraph``。"""
    ids, _sources = islands
    active = ids[0]
    repo.retrieval.graph._federated_rx_graph(active)
    assert f"{active}:fed_rxgraph" in _graph_cache_keys(repo)


def test_fed_rxgraph_cache_key_includes_override_fingerprint(repo, islands):
    """同一个 active、两个不同覆盖集 -> 两个键,互不串。

    键里的指纹放在 family 后缀**之前**:``retrieval_snapshot_cache`` 按
    ``endswith(":fed_rxgraph")`` 扫族做 belt-and-braces 驱逐,追加在尾部会让
    覆盖建出来的图整类逃过 KG 变更时的驱逐。
    """
    from app.services.retrieval_participants import override_fingerprint

    ids, _sources = islands
    active = ids[0]
    wide = _override(ids)
    narrow = _override([ids[0], ids[1]])

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(wide):
            repo.retrieval.graph._federated_rx_graph(active)
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(narrow):
            repo.retrieval.graph._federated_rx_graph(active)

    keys = _graph_cache_keys(repo)
    wide_key = f"{active}:{override_fingerprint(wide)}:fed_rxgraph"
    narrow_key = f"{active}:{override_fingerprint(narrow)}:fed_rxgraph"
    assert wide_key in keys and narrow_key in keys
    assert wide_key != narrow_key
    # 无覆盖的那个键没有被覆盖 run 占用。
    assert f"{active}:fed_rxgraph" not in keys
    # 族扫描仍然认得它们(驱逐靠的就是这一条)。
    assert all(key.endswith(":fed_rxgraph") for key in (wide_key, narrow_key))


def test_override_graph_actually_spans_the_override_libraries(repo, islands):
    """键分开只是第一层;图本身也必须真的覆盖了三个库的节点。"""
    ids, _sources = islands
    active = ids[0]
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            graph, _idx_to_oid, oid_to_idx = (
                repo.retrieval.graph._federated_rx_graph(active)
            )
    assert graph is not None
    owners = {
        graph.get_node_data(index).get("notebook_id")
        for index in oid_to_idx.values()
    }
    assert owners == set(ids), owners


def test_ppr_and_scale_cache_keys_follow_the_same_rule(repo, islands):
    """``_ppr_graph`` 与 scale 组合图用同一个键构造器,所以同一条规则成立。"""
    from app.services.graph_retrieval import _participant_graph_cache_key
    from app.services.retrieval_participants import override_fingerprint

    ids, _sources = islands
    active = ids[0]
    for family in ("ppr_graph", "scale_combined"):
        assert _participant_graph_cache_key(active, family) == (
            f"{active}:{family}"
        )
    override = _override(ids)
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(override):
            for family in ("ppr_graph", "scale_combined"):
                assert _participant_graph_cache_key(active, family) == (
                    f"{active}:{override_fingerprint(override)}:{family}"
                )


# ---------------------------------------------------------------------------
# 3. 集合地图 / 枚举 / 对比库
# ---------------------------------------------------------------------------

def test_collection_map_counts_override_participants(repo, islands):
    ids, _sources = islands
    active = ids[0]
    baseline = repo.collection_catalog.collection_map(active)
    assert baseline.sources == 1, "前提:没有覆盖时只看得见 active 自己"

    repo.collection_catalog.invalidate()
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            mapped = repo.collection_catalog.collection_map(active)
    assert set(mapped.notebook_ids) == set(ids)
    assert mapped.sources == 3
    assert mapped.active_sources == 1


def test_enumeration_rows_and_denominator_come_from_one_predicate(repo, islands):
    """行与分母必须同一个谓词:枚举走完时 ``returned_total == total``。"""
    from app.services.collection_enumeration import EnumerationBudget

    ids, _sources = islands
    active = ids[0]
    budget = EnumerationBudget(
        page_size=25, max_rows=1_000, max_pages=50, max_payload_chars=256_000,
    )
    repo.collection_catalog.invalidate()
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            result = repo.collection_enumeration.enumerate_sources(
                active, budget=budget,
            )
    notebooks = {item.notebook_id or active for item in result.items}
    assert notebooks == set(ids), notebooks
    coverage = result.coverage
    assert coverage.returned_total == coverage.total == 3
    assert coverage.complete is True


def test_comparison_peer_libraries_come_from_the_override(repo, islands):
    """``communities.mounted_base_ids``:覆盖下是覆盖集去掉名义 active。"""
    ids, _sources = islands
    active = ids[0]
    queries = repo._runtime.ask_service().communities()
    assert queries.mounted_base_ids(active) == [], "前提:没挂任何参考库"

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            peers = queries.mounted_base_ids(active)
    assert peers == [ids[1], ids[2]]

    # 库维度仍可再收窄。
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            with source_scope_context(
                active, None,
                BaseNotebookScope(mode="include", notebook_ids=[ids[2]]),
            ):
                assert queries.mounted_base_ids(active) == [ids[2]]


# ---------------------------------------------------------------------------
# 4. 直读 mount 表的判据
# ---------------------------------------------------------------------------

def test_any_base_has_kg_answers_for_the_override_set(repo, islands):
    """覆盖集里有图的库被取消勾选 -> False;勾着 -> True。"""
    ids, _sources = islands
    active = ids[0]
    gate = repo.retrieval

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            # 没提交 base_scope:走 `_any_base_notebook_has_kg` 的覆盖臂。
            assert gate.any_base_has_kg(active) is True
    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            with source_scope_context(
                active, None,
                BaseNotebookScope(mode="include", notebook_ids=[]),
            ):
                assert gate.any_base_has_kg(active) is False


def test_any_base_has_kg_is_unchanged_without_an_override(repo, islands):
    """覆盖不在场 -> 仍是那条 mount-join EXISTS,互不挂载所以 False。"""
    ids, _sources = islands
    assert repo.retrieval.any_base_has_kg(ids[0]) is False


def test_graph_size_guard_answers_for_the_override_set(repo, islands, monkeypatch):
    """覆盖在场时守卫读座位 —— 因为那时两张图也读座位,守卫与建图口径同源。"""
    ids, _sources = islands
    active = ids[0]
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(
        candidates, "notebook_copy_stats",
        lambda notebook_id: {"copyable": notebook_id != ids[1]},
    )

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            # 覆盖集里含那本大库 -> 判 large(两张图也会把它建进去)。
            assert candidates._federated_graph_is_large(active) is True
            # 库维度把它滤掉 -> 覆盖下的两张图同样不会建它,守卫跟着关。
            with source_scope_context(
                active, None,
                BaseNotebookScope(mode="include", notebook_ids=[ids[2]]),
            ):
                assert candidates._federated_graph_is_large(active) is False


def test_graph_size_guard_stays_scope_blind_without_an_override(repo, monkeypatch):
    """⛔ 无覆盖时**不许**跟着库勾选走:取消勾选的大库仍然判 large。

    这不是漏改。守卫守的那两张图在无覆盖时按**全部挂载库**建
    (`_federated_rx_graph` 读 `participant_rows`、`_ppr_graph` 读
    `participant_ids`),只把守卫收窄就会放行它本来要拒绝的那次构建:小笔记本挂
    一本超大参考库、用户取消勾选它 -> 守卫关 -> 逐库跑
    `graph_object_rows`/`graph_relation_rows`/`cluster_member_rows`,正是
    `retrieval_service.ppr_retrieve` 记着的那条数十分钟、数 GB 的路。

    **变异锚点**:让无覆盖分支改读座位 -> 第二条断言从 True 变 False。
    """
    base = repo.create_notebook(NotebookCreate(name="huge reference"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="small notebook"))
    repo.replace_notebook_bases(active.id, [base.id], _ACTOR)
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(
        candidates, "notebook_copy_stats",
        lambda notebook_id: {"copyable": notebook_id != base.id},
    )

    assert candidates._federated_graph_is_large(active.id) is True
    with source_scope_context(
        active.id, None, BaseNotebookScope(mode="include", notebook_ids=[]),
    ):
        assert candidates._federated_graph_is_large(active.id) is True, (
            "无覆盖时守卫必须与建图口径同源:两张图仍按全部挂载库建"
        )


# ---------------------------------------------------------------------------
# 5. follow_chain 起点鉴权按覆盖集解析(C4)
# ---------------------------------------------------------------------------

def test_follow_start_row_resolves_a_start_in_any_override_participant(repo, islands):
    """覆盖集里另一个库的对象可以作为 follow_chain 的起点。"""
    ids, _sources = islands
    active, peer = ids[0], ids[1]
    with repo._connect() as db:
        peer_object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (peer,),
        ).fetchone()["id"]

    from app.services.knowledge_contracts import USABLE_STATUSES

    with repo._connect() as db:
        # 不传 participant_ids:挂载子查询逐字保留,互不挂载 -> 起点不合法。
        assert repo.retrieval.candidates.knowledge.follow_start_row(
            db, peer_object_id, active, USABLE_STATUSES,
        ) is None
        # 传覆盖集 -> 合法。
        row = repo.retrieval.candidates.knowledge.follow_start_row(
            db, peer_object_id, active, USABLE_STATUSES,
            participant_ids=list(ids),
        )
        assert row is not None and row["notebook_id"] == peer
        # 空清单是 fail-closed,不是「无限制」。
        assert repo.retrieval.candidates.knowledge.follow_start_row(
            db, peer_object_id, active, USABLE_STATUSES, participant_ids=[],
        ) is None


def test_absent_participant_ids_is_byte_identical(repo):
    """``participant_ids=None`` 与不传该参数走同一条语句、同一个结果。"""
    from app.services.knowledge_contracts import USABLE_STATUSES

    base = repo.create_notebook(NotebookCreate(name="mounted base"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="active"))
    repo.replace_notebook_bases(active.id, [base.id], _ACTOR)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "mounted concept", "section_path": "1"},
         "evidence": []},
    ], [])
    with repo._connect() as db:
        object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (base.id,),
        ).fetchone()["id"]
        implicit = repo.retrieval.candidates.knowledge.follow_start_row(
            db, object_id, active.id, USABLE_STATUSES,
        )
        explicit_none = repo.retrieval.candidates.knowledge.follow_start_row(
            db, object_id, active.id, USABLE_STATUSES, participant_ids=None,
        )
    assert implicit is not None
    assert dict(implicit) == dict(explicit_none)


# ---------------------------------------------------------------------------
# 6. 身份复核的 raise 不许被 fail-soft 吞掉
# ---------------------------------------------------------------------------

def _reasoning_retriever(repo):
    from app.services.reasoning_retrieval import ReasoningRetriever

    ask = repo._runtime.ask_service()
    return ReasoningRetriever(
        retrieval=repo.retrieval,
        model_clients=ask.model_clients,
        communities=ask.communities(),
        settings=repo.settings,
    )


@pytest.mark.parametrize("entry", [
    "chunks", "kg", "collection_map", "seat",
    "search_chunks", "mix_retrieve", "no_kg_early_exit",
])
def test_actor_mismatch_raises_out_of_every_entry_point(repo, islands, entry):
    """run 的 actor 与覆盖的 attested actor 不符 -> 整条检索抛,不是静默空手。

    静默回落会产出一个「本次检索未命中」的正常答案,越权上下文泄漏因此永远不会
    被发现——检索层遍地 ``except Exception`` 的 fail-soft 纪律正是这个风险的来源,
    所以每个入口都要各钉一次。
    """
    ids, _sources = islands
    active = ids[0]
    entries = {
        "chunks": lambda: repo.retrieval.retrieve_chunk_candidates(
            active, _QUERY,
        ),
        "kg": lambda: repo.retrieval.federated_retrieve(active, _QUERY),
        "collection_map": lambda: repo.collection_catalog.collection_map(active),
        "seat": lambda: repo.retrieval.candidates._retrieval_participants(
            active,
        ),
        # reasoning 的原文动作:``_chunk_seed_search`` 的 ``except Exception:
        # return []`` 是这条路上最典型的 fail-soft 吞点。
        "search_chunks": lambda: _reasoning_retriever(repo).search_chunks(
            active, _QUERY,
        ),
        # chunk 模式三路 mix:向量腿 + KG overlay(``_federated_graph_is_large``
        # 与 ``_any_base_notebook_has_kg`` 都在这条路上读座位)。
        "mix_retrieve": lambda: repo.retrieval.candidates._mix_retrieve(
            active, _QUERY, "", [_QUERY],
        ),
        # 无图早退的放行判据读 collection_map,而 ``AskService`` 对它是 fail-open。
        "no_kg_early_exit": lambda: (
            repo._runtime.ask_service()._no_kg_scope_admits_run(active)
        ),
    }
    with retrieval_run(run_kind="ask_global", actor_id="somebody-else"):
        with participant_override(_override(ids, actor=_ACTOR)):
            with pytest.raises(ParticipantOverrideError):
                entries[entry]()


def test_assert_override_matches_run_is_the_loud_pre_check(repo, islands):
    """写入方的预先复核:无副作用、无覆盖时是 no-op、错配时响亮失败。"""
    ids, _sources = islands
    # 无覆盖 -> no-op,连 run 都不要求。
    assert_override_matches_run() is None

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            assert_override_matches_run() is None
    with retrieval_run(run_kind="ask_global", actor_id="somebody-else"):
        with participant_override(_override(ids, actor=_ACTOR)):
            with pytest.raises(ParticipantOverrideError):
                assert_override_matches_run()


def test_reasoning_fail_open_seed_does_not_swallow_the_attestation_failure(
    repo, islands,
):
    """reasoning 的 fail-open 播种臂必须把身份复核的 raise 放出去。

    ``_chunk_seed_search`` 是这一族 handler 的代表:``fail_closed`` 在 Ask 路径
    恒为 False,所以「吞掉、返回 []」是它的**正常**行为——一条子查询炸掉不该拖垮
    整轮播种。但覆盖的身份复核失败不是子查询炸了,吞掉它就等于把一次越权上下文
    泄漏包装成一句「本次检索未命中」。

    **变异锚点**:删掉 ``except RetrievalControlError: raise`` -> 这里返回 ``[]``。
    """
    ids, _sources = islands
    active = ids[0]
    retriever = _reasoning_retriever(repo)
    assert retriever.fail_closed is False, "前提:Ask 路径是 fail-open 的"

    with retrieval_run(run_kind="ask_global", actor_id="somebody-else"):
        with participant_override(_override(ids, actor=_ACTOR)):
            with pytest.raises(ParticipantOverrideError):
                retriever._chunk_seed_search(active, _QUERY, 4)

    # 对照臂:普通的检索故障仍然被吞掉,fail-open 语义没有被顺手改掉。
    class _Boom(RuntimeError):
        pass

    original = retriever.search_chunks
    try:
        retriever.search_chunks = lambda *a, **k: (_ for _ in ()).throw(_Boom())
        assert retriever._chunk_seed_search(active, _QUERY, 4) == []
    finally:
        retriever.search_chunks = original


def test_report_probe_loader_does_not_swallow_the_attestation_failure():
    """报告的覆盖探针 ``_safe`` 同款:普通故障吞掉,控制异常放出去。

    直接调真方法(`ReportEngine._load_probe_query_results`)而不是搭一台完整报告
    引擎:被测的是它内部那个 ``_safe`` 闭包,替身只需要提供它读的三个协作者。
    """
    from types import SimpleNamespace

    from app.services.report_engine import ReportEngine

    load = ReportEngine._load_probe_query_results

    class _Engine:
        settings = SimpleNamespace(
            report_retrieval_fanout=1, report_probe_channel_concurrency=1,
        )
        _bounded_probe_queries = ReportEngine._bounded_probe_queries

        def __init__(self, error) -> None:
            self._error = error

        def _probe_knowledge_hits(self, _notebook_id, _query):
            raise self._error

        def _probe_element_hits(self, _notebook_id, _query):
            return []

    # 普通故障:吞掉,并把 ok=False 记进结果(既有 fail-open 语义)。
    groups = load(_Engine(RuntimeError("boom")), "nb", [["q"]], max_queries=2)
    assert groups and groups[0] and groups[0][0][0] == []

    # 控制异常:向上抛。
    with pytest.raises(ParticipantOverrideError):
        load(
            _Engine(ParticipantOverrideError("attestation failed")),
            "nb", [["q"]], max_queries=2,
        )


# ---------------------------------------------------------------------------
# 7. knowledge_context 的 canonical 折叠范围(D0-3)
# ---------------------------------------------------------------------------

def _fold_fixture(repo, islands):
    """在**第二个**库(``ids[1]``)里造四个知识对象。

    ``X``/``Y``:同一个 canonical 簇的两个成员(折叠腿)。
    ``P``/``Q``:一条 ``supports`` 关系的两端(关系腿)。

    两条腿都必须落在第二个库:名义 active 与它互不挂载,所以挂载表口径下
    ``cluster_fold``/``in_network_relations`` 根本不会去查这个库——任何一条断言在
    挂载关系下也成立的话,它就证明不了覆盖。
    """
    import json

    peer = islands[0][1]
    repo.store_kg(peer, None, [
        {"local_id": "X", "object_type": "concept",
         "payload": {"name": "共质心 版图 别名甲", "section_path": "1"},
         "evidence": []},
        {"local_id": "Y", "object_type": "concept",
         "payload": {"name": "共质心 版图 别名乙", "section_path": "1"},
         "evidence": []},
        {"local_id": "P", "object_type": "claim",
         "payload": {"name": "匹配对必须共质心", "section_path": "2"},
         "evidence": []},
        {"local_id": "Q", "object_type": "claim",
         "payload": {"name": "共质心降低失配", "section_path": "2"},
         "evidence": []},
    ], [
        {"source_local_id": "P", "target_local_id": "Q",
         "edge_type": "supports", "evidence": []},
    ])
    with repo._connect() as db:
        by_name = {
            json.loads(row["payload"])["name"]: row["id"]
            for row in db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
                (peer,),
            ).fetchall()
        }
    members = (by_name["共质心 版图 别名甲"], by_name["共质心 版图 别名乙"])
    with repo._write() as db:
        for index, member in enumerate(members):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                "member_object_id,canonical_name,object_type,"
                "canonical_description,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"cc-{index}", peer, members[0], member,
                 "共质心版图", "concept", "", _NOW),
            )
        repo._bump_cluster_mutation_seq(db, peer)
    return peer, by_name


_FOLD_HIT_NAMES = ("共质心 版图 别名甲", "共质心 版图 别名乙",
                   "匹配对必须共质心", "共质心降低失配")


def _fold_hits(peer, by_name):
    """四条命中,全部显式打上第二个库的 ``notebook_id``。

    装配本身(``_admit`` 的 ``node_context(origin, ...)``,``origin`` 取的就是
    这个字段)因此与参与集无关——被测的只剩 ``_canonical()`` 的折叠范围和
    ``in_network_relations`` 的取数范围。
    """
    from app.services.retrieval import RetrievedKnowledge

    types = ("concept", "concept", "claim", "claim")
    return [
        RetrievedKnowledge(
            object_id=by_name[name], object_type=object_type,
            payload={"name": name}, evidence=[], notebook_id=peer,
        )
        for name, object_type in zip(_FOLD_HIT_NAMES, types)
    ]


def _evidence_context(repo):
    return repo._runtime.evidence_context_component


def test_canonical_fold_uses_override_set(repo, islands):
    """覆盖在场 -> 折叠范围与关系取数范围都是覆盖集,不是名义 active 的挂载表。

    两条独立的证据,都只可能来自覆盖:

    * **折叠**:第二个库里同簇的 ``X``/``Y`` 被折到一起,``seen_clusters`` 因此
      只放行一条(挂载表口径下 ``cluster_fold`` 不查这个库,``_canonical()``
      两次都 miss,两条各占一个 ``k{n}``);
    * **关系**:``P -[supports]-> Q`` 这一行出现在 ``relations:`` 里(挂载表
      口径下 ``in_network_relations`` 只遍历名义 active,这条行压根取不回来,
      两端也就无从在 ``object_to_key`` 里解析)。

    **变异锚点**:把 ``knowledge_context`` 的 ``participants`` 改回直调
    ``self.notebooks.participant_notebook_ids`` -> 两条断言各自回到"挂载表"的
    那一侧。
    """
    ids, _sources = islands
    active = ids[0]
    peer, by_name = _fold_fixture(repo, islands)
    hits = _fold_hits(peer, by_name)
    evidence = _evidence_context(repo)

    # 前提:没有覆盖时,折叠与关系都拿不到第二个库的东西。
    baseline_block, baseline_map = evidence.knowledge_context(active, hits)
    assert len(baseline_map) == 4, baseline_map
    assert "relations:" not in baseline_block

    with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
        with participant_override(_override(ids)):
            block, id_map = evidence.knowledge_context(active, hits)

    assert len(id_map) == 3, (
        "同一 canonical 簇的两个成员没有折到一起", sorted(id_map)
    )
    folded = {value["name"] for value in id_map.values()}
    assert "共质心 版图 别名乙" not in folded, folded
    assert "relations:" in block, block
    keys = {value["name"]: key for key, value in id_map.items()}
    assert (
        f"{keys['匹配对必须共质心']} -[supports]-> {keys['共质心降低失配']}"
        in block
    ), block


def test_absent_override_keeps_mount_table(repo, islands):
    """无覆盖 -> 逐值回到挂载表,且不多问一次谓词。

    三半:传给两个端口的参与集与 ``participant_notebook_ids`` 的返回值逐值相等
    (str 归一后仍是同一组 id);谓词恰好被调用一次(``resolve_*`` 无覆盖时直通
    ``fallback()``,包一层不许变成两次查询);输出与直调谓词时逐字相同。
    """
    ids, _sources = islands
    active = ids[0]
    peer, by_name = _fold_fixture(repo, islands)
    hits = _fold_hits(peer, by_name)
    evidence = _evidence_context(repo)
    expected = list(evidence.notebooks.participant_notebook_ids(active))
    assert expected == [active], "前提:互不挂载,挂载表只给 active 自己"

    reference_block, reference_map = evidence.knowledge_context(active, hits)

    predicate_calls: list[str] = []
    fold_scopes: list[str] = []
    relation_scopes: list[list[str]] = []
    notebooks_port, knowledge_port = evidence.notebooks, evidence.knowledge
    original_predicate = notebooks_port.participant_notebook_ids
    original_fold = knowledge_port.cluster_fold
    original_relations = knowledge_port.in_network_relations

    def _spy_predicate(notebook_id):
        predicate_calls.append(notebook_id)
        return original_predicate(notebook_id)

    def _spy_fold(notebook_id, object_ids):
        fold_scopes.append(notebook_id)
        return original_fold(notebook_id, object_ids)

    def _spy_relations(participant_ids, object_ids):
        relation_scopes.append(list(participant_ids))
        return original_relations(participant_ids, object_ids)

    evidence.notebooks = SimpleNamespace(
        **{
            name: getattr(evidence.notebooks, name)
            for name in dir(evidence.notebooks) if not name.startswith("__")
        }
    )
    evidence.notebooks.participant_notebook_ids = _spy_predicate
    evidence.knowledge = SimpleNamespace(
        **{
            name: getattr(evidence.knowledge, name)
            for name in dir(evidence.knowledge) if not name.startswith("__")
        }
    )
    evidence.knowledge.cluster_fold = _spy_fold
    evidence.knowledge.in_network_relations = _spy_relations
    try:
        block, id_map = evidence.knowledge_context(active, hits)
    finally:
        evidence.notebooks, evidence.knowledge = notebooks_port, knowledge_port

    assert predicate_calls == [active], predicate_calls
    assert fold_scopes == expected, fold_scopes
    assert relation_scopes == [expected], relation_scopes
    assert block == reference_block
    assert id_map == reference_map
    assert len(id_map) == 4 and "relations:" not in block


def test_canonical_fold_raises_on_actor_mismatch(repo, islands):
    """覆盖在场 + actor 错配 -> ``knowledge_context`` 向上抛,不被吞成空折叠。

    折叠范围静默回落到挂载表会得到一份"看起来正常、只是少了点关联"的证据块,
    正是覆盖模块 docstring 点名要避免的那种失败。
    """
    ids, _sources = islands
    active = ids[0]
    peer, by_name = _fold_fixture(repo, islands)
    hits = _fold_hits(peer, by_name)
    evidence = _evidence_context(repo)

    with retrieval_run(run_kind="ask_global", actor_id="somebody-else"):
        with participant_override(_override(ids, actor=_ACTOR)):
            with pytest.raises(ParticipantOverrideError):
                evidence.knowledge_context(active, hits)


def test_answer_synthesis_retry_does_not_swallow_the_attestation_failure(repo):
    """``AskService._answer_with_retry`` 必须把身份复核的 raise 放出去。

    ``synth()`` 里装 KG 证据(``_answer_context`` -> ``knowledge_context`` ->
    折叠范围读座位)。宽 handler 吞掉它 -> 重试一次、记一条 answer 报警、返回
    ``ok=False`` 的空答案,也就是一句"检索到却答不出"。

    **变异锚点**:把 ``except (AskCancelled, RetrievalControlError)`` 改回
    ``except AskCancelled`` -> 第一条断言从 raise 变成返回空答案。
    """
    ask = repo._runtime.ask_service()

    def _boom():
        raise ParticipantOverrideError("attestation failed")

    with pytest.raises(ParticipantOverrideError):
        ask._answer_with_retry(_boom, "m", service="ask_answer")

    # 对照臂:普通合成故障仍然被吞成 ok=False(fail-soft 语义没被顺手改掉)。
    def _ordinary():
        raise RuntimeError("boom")

    answer, grounded, anchors, ok = ask._answer_with_retry(
        _ordinary, "m", service="ask_answer",
    )
    assert (answer, grounded, anchors, ok) == ("", False, [], False)


def test_follow_chain_only_passes_participant_ids_under_an_override(repo, islands):
    """无覆盖时**连关键字都不传**,端口调用形状逐字不变。

    ``participant_ids`` 是给两个内置适配器加的可选参数。每次都显式传
    ``participant_ids=None`` 会把它变成调用形状的一部分,任何仓库外/插件侧实现
    该端口的替身都会在每一次 ``follow_chain`` 上 ``TypeError``。
    """
    ids, _sources = islands
    active, peer = ids[0], ids[1]
    seen: list[dict] = []
    graph = repo.retrieval.graph
    original = graph.knowledge.follow_start_row

    def _spy(db, object_id, active_notebook_id, statuses, **kwargs):
        seen.append(dict(kwargs))
        return original(db, object_id, active_notebook_id, statuses, **kwargs)

    graph.knowledge = SimpleNamespace(
        **{
            name: getattr(graph.knowledge, name)
            for name in dir(graph.knowledge)
            if not name.startswith("__")
        }
    )
    graph.knowledge.follow_start_row = _spy
    try:
        graph.follow_chain(active, "ko-missing")
        assert seen == [{}], "无覆盖时不得出现 participant_ids 关键字"

        seen.clear()
        with retrieval_run(run_kind="ask_global", actor_id=_ACTOR):
            with participant_override(_override(ids)):
                graph.follow_chain(active, "ko-missing")
        assert len(seen) == 1
        assert seen[0]["participant_ids"] == [active, peer, ids[2]]
    finally:
        graph.knowledge = repo.retrieval.candidates.knowledge
