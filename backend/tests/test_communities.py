import json
from contextlib import contextmanager

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.services.communities import CommunityQueryService
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _claim(lid, name):
    return {"local_id": lid, "object_type": "claim",
            "payload": {"name": name, "section_path": "1"}, "evidence": []}


def _rel(s, t):
    return {"source_local_id": s, "target_local_id": t, "edge_type": "supports", "evidence": []}


def test_rebuild_communities_detects_two_groups(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # group1: A-B-C triangle; group2: D-E; no cross edges -> 2 communities.
    # community_min_size default is now 3 (drops noise/singletons); this test
    # exercises the grouping (incl. the size-2 group) so set it to 2 explicitly.
    repo.settings.community_min_size = 2
    repo.store_kg(nb.id, None,
        [_claim("A", "a"), _claim("B", "b"), _claim("C", "c"),
         _claim("D", "d"), _claim("E", "e")],
        [_rel("A", "B"), _rel("B", "C"), _rel("A", "C"), _rel("D", "E")])
    n = repo.rebuild_communities(nb.id)
    assert n == 2
    comms = repo.list_communities(nb.id)            # List[List[str]] member-id lists
    sizes = sorted(len(c) for c in comms)
    assert sizes == [2, 3]


def test_rebuild_communities_empty_graph(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.rebuild_communities(nb.id) == 0
    assert repo.list_communities(nb.id) == []


def test_rebuild_communities_is_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.settings.community_min_size = 2            # size-2 {A,B} must survive here
    repo.store_kg(nb.id, None, [_claim("A", "a"), _claim("B", "b")], [_rel("A", "B")])
    repo.rebuild_communities(nb.id)
    repo.rebuild_communities(nb.id)                 # rerun must not duplicate
    assert len(repo.list_communities(nb.id)) == 1   # one community {A,B}, not two


def test_community_query_service_uses_explicit_sibling_threshold():
    calls = []

    class _Unified:
        def resolve_focal(self, notebook_id, key):
            return "cluster:focal"

        def comention_peers(self, notebook_id, focal, min_bridges, top_k):
            calls.append((notebook_id, focal, min_bridges, top_k))
            return [("Peer", 5)] if min_bridges == 5 else []

    service = CommunityQueryService(
        notebooks=object(), unified_kg=_Unified(),
        event_log=type("Log", (), {"emit": lambda *args, **kwargs: None})(),
        sibling_min_bridge=5,
    )

    assert service.resolve_comparison_peers(
        "base", "Focal", "compare", top_k=3, candidates=10,
    ) == (["Peer"], "comention")
    assert calls == [("base", "cluster:focal", 5, 3)]


def _peer_service(peers_by_library):
    """``resolve_comparison_peers`` 定死成每库一份名单的替身。"""

    class _Unified:
        def resolve_focal(self, notebook_id, key):
            return "cluster:focal"

        def comention_peers(self, notebook_id, focal, min_bridges, top_k):
            return [(name, 5) for name in peers_by_library.get(notebook_id, ())]

    return CommunityQueryService(
        notebooks=object(), unified_kg=_Unified(),
        event_log=type("Log", (), {"emit": lambda *args, **kwargs: None})(),
        sibling_min_bridge=5,
    )


def _peer_run(ids):
    """把进程切进对等模式(参与集覆盖),轮转与帽只在这里面生效。"""
    from app.services.retrieval_participants import (
        ParticipantOverride, participant_override,
    )
    from app.services.retrieval_run import retrieval_run

    @contextmanager
    def _installed():
        with retrieval_run(run_kind="ask_global", actor_id="user-1"):
            with participant_override(ParticipantOverride(
                notebook_ids=tuple(ids), tiers={},
                attested_actor_id="user-1",
            )):
                yield

    return _installed()


def test_comparison_peer_names_truncates_by_rotating_over_libraries():
    """对等模式:总量帽按库轮转,不是逐库拼完切前 N 条。

    直接切前 N 条会把整个配额发给遍历顺序最靠前的那一两个库,后面的库一个名字
    都进不了子查询——多选一个库反而让它自己的兄弟实体被挤掉。
    """
    service = _peer_service({
        "nb-a": ["a1", "a2", "a3"],
        "nb-b": ["b1", "b2", "b3"],
        "nb-c": ["c1", "c2", "c3"],
    })

    with _peer_run(["nb-a", "nb-b", "nb-c"]):
        names = service.comparison_peer_names(
            ["nb-a", "nb-b", "nb-c"], "Focal", "compare",
            top_k=3, candidates=10, cap_factor=1,
        )

    assert names == ["a1", "b1", "c1"]


def test_comparison_peer_names_keeps_single_library_behaviour_value_identical():
    """单库(哪怕挂了三个参考库)逐值不变:顺序追加、无帽、一个也不少。

    轮转与帽是为参与集带来的拥挤发明的;把它们加到单库模式上会重排一个既有
    笔记本的子查询顺序、截掉它本来拿得到的名字,那是一次独立的产品行为改动。
    """
    service = _peer_service({
        "nb-a": ["a1", "a2", "a3"],
        "nb-b": ["b1", "b2", "b3"],
        "nb-c": ["c1", "c2", "c3"],
    })

    names = service.comparison_peer_names(
        ["nb-a", "nb-b", "nb-c"], "Focal", "compare",
        top_k=3, candidates=10, cap_factor=1,
    )

    assert names == ["a1", "a2", "a3", "b1", "b2", "b3", "c1", "c2", "c3"]


def test_comparison_peer_names_stops_querying_once_the_cap_is_full():
    """对等模式下达到帽即停止解析后面的库:装不下的查询不发。"""
    asked: list = []

    class _Unified:
        def resolve_focal(self, notebook_id, key):
            asked.append(notebook_id)
            return "cluster:focal"

        def comention_peers(self, notebook_id, focal, min_bridges, top_k):
            return [(f"{notebook_id}-1", 5)]

    service = CommunityQueryService(
        notebooks=object(), unified_kg=_Unified(),
        event_log=type("Log", (), {"emit": lambda *args, **kwargs: None})(),
        sibling_min_bridge=5,
    )

    ids = ["nb-a", "nb-b", "nb-c", "nb-d"]
    with _peer_run(ids):
        names = service.comparison_peer_names(
            ids, "Focal", "compare", top_k=2, candidates=10, cap_factor=1,
        )

    assert names == ["nb-a-1", "nb-b-1"]
    assert asked == ["nb-a", "nb-b"]


def test_comparison_peer_names_dedupes_and_keeps_a_deterministic_order():
    service = _peer_service({
        "nb-a": ["shared", "a2"],
        "nb-b": ["shared", "b2"],
    })

    with _peer_run(["nb-a", "nb-b"]):
        names = service.comparison_peer_names(
            ["nb-a", "nb-b"], "Focal", "compare",
            top_k=4, candidates=10, cap_factor=2,
        )

    assert names == ["shared", "a2", "b2"]


def test_comparison_peer_names_is_value_identical_for_a_single_library():
    """一个库时轮转退化成原来的顺序,帽 >= ``top_k`` 所以恒不触发。"""
    service = _peer_service({"nb-a": ["a1", "a2", "a3", "a4"]})

    for factor in (1, 2):
        with _peer_run(["nb-a"]):
            assert service.comparison_peer_names(
                ["nb-a"], "Focal", "compare", top_k=4, candidates=10,
                cap_factor=factor,
            ) == ["a1", "a2", "a3", "a4"]


def test_comparison_peer_names_without_any_library_is_empty():
    service = _peer_service({})
    assert service.comparison_peer_names(
        [], "Focal", "compare", top_k=8, candidates=10, cap_factor=2,
    ) == []
