"""`rebuild_communities` wired to `kg_partition` (design
docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md §五 S2).

The contract that replaces design 2026-07-25 §3.32 invariant 1 ("the Louvain
output is byte-identical"): **the same input graph and the same previous
partition give a byte-identical partition, board_key included** -- whatever order
the relations arrive in.

Cold / warm criteria (``KnowledgeLifecycleService._partition_boards_for_rebuild``):
no published partition, a published partition built with another
``PARTITION_ALGO_VERSION`` (0 = the algorithm before this one, and what
``recluster_kg --cold-partition`` writes back), and no igraph start cold;
everything else warm-starts from the published generation. ``force`` is NOT a
criterion: every real re-merge calls with ``force=True``. A cold start with a
published partition still reads it, to carry board keys.

Real igraph / networkx throughout (no fake partition): every expectation below
is what the real libraries produce.
"""
from __future__ import annotations

import itertools
import json
import sys

import pytest

from app.core.config import Settings
from app.domain.kg_analysis_contracts import ARTIFACT_COMMUNITY_EDGES
from app.models.schemas import NotebookCreate
from app.services import kg_partition
from app.services.embedding import FakeEmbedder
from app.services.kg_partition import (
    PARTITION_ALGO_VERSION,
    PreviousPartitionBuilder,
    mint_board_key,
)
from app.services.knowledge_lifecycle import _fold_previous_partition
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients

NOW = "2026-10-21T00:00:00"

# Two 4-cliques joined through one bridge node x (one edge to each side: a
# modularity tie, x may sit on either side), plus a separate triangle.
CLIQUE_A = ["K-a1", "K-a2", "K-a3", "K-a4"]
CLIQUE_B = ["K-b1", "K-b2", "K-b3", "K-b4"]
TRIANGLE = ["K-c1", "K-c2", "K-c3"]
BRIDGE = "K-x"
EDGES = (
    list(itertools.combinations(CLIQUE_A, 2))
    + list(itertools.combinations(CLIQUE_B, 2))
    + [(BRIDGE, "K-a1"), (BRIDGE, "K-b1")]
    + list(itertools.combinations(TRIANGLE, 2))
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


@pytest.fixture
def events(repo, monkeypatch):
    emitted: list = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", emitted.append)
    return emitted


def _graph(repo, edges=EDGES, *, name: str = "nb", flip: bool = False) -> str:
    """One notebook whose canonical graph is ``edges``: each canonical has one
    object of its own (ids prefixed by the notebook, so two notebooks can carry
    the same canonical ids), every edge one relation, inserted in the given
    order (``flip`` swaps each relation's endpoints)."""
    nb = repo.create_notebook(NotebookCreate(name=name)).id
    _seed_graph(repo, nb, edges, flip=flip)
    return nb


def _seed_graph(repo, nb: str, edges, *, flip: bool = False) -> None:
    nodes = sorted({node for edge in edges for node in edge})
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (f"src-{nb}", nb, "doc", "md", "ready", NOW, NOW))
        for node in nodes:
            oid = f"o-{nb}-{node}"
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nb, "concept", "approved", "", json.dumps({"name": node}), "[]",
                 f"src-{nb}", NOW, NOW))
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,object_type,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"cl-{oid}", nb, node, oid, node, "concept", NOW))
        for n, (a, b) in enumerate(edges):
            if flip:
                a, b = b, a
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"r-{nb}-{n}", nb, f"src-{nb}", f"o-{nb}-{a}", f"o-{nb}-{b}",
                 "related_to", "[]", NOW))


def _published(repo, nb: str, level: int = 0) -> list[tuple[str, tuple[str, ...]]]:
    """The published generation's boards as ``(board_key, members)``, sorted."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT c.board_key AS board_key, m.canonical_id AS canonical_id "
            "FROM communities c JOIN community_members m ON m.community_id = c.id "
            "AND m.generation = c.generation "
            "WHERE c.notebook_id=? AND c.level=? AND c.generation = "
            "(SELECT community_generation FROM unified_kg_state WHERE notebook_id=?)",
            (nb, level, nb)).fetchall()
    boards: dict = {}
    for row in rows:
        boards.setdefault(row["board_key"], []).append(row["canonical_id"])
    return sorted((key, tuple(sorted(members))) for key, members in boards.items())


def _state(repo, nb: str) -> tuple[int, str]:
    with repo._connect() as db:
        row = db.execute(
            "SELECT community_partition_version, community_partition_mode "
            "FROM unified_kg_state WHERE notebook_id=?", (nb,)).fetchone()
    return int(row[0]), row[1]


def _rebuilt(events: list) -> dict:
    [event] = [e for e in events if e.get("kind") == "communities_rebuilt"]
    events.clear()
    return event


def _edges_payload(repo, nb: str) -> dict:
    with repo._connect() as db:
        row = db.execute(
            "SELECT payload FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
            (nb, ARTIFACT_COMMUNITY_EDGES)).fetchone()
    return json.loads(row["payload"])


def _publish(repo, nb: str, boards, *, version: int = PARTITION_ALGO_VERSION,
             mode: str = "warm") -> None:
    """Publish a crafted partition ``[(board_key, members), ...]`` through the
    generation protocol, stamped with ``version`` / ``mode``."""
    rt = repo._runtime
    members = sorted({m for _key, ms in boards for m in ms})
    with rt.database.write() as db:
        claim = rt.unified_kg.claim_derived_generation(db, nb, ttl_seconds=3600)
    assert claim is not None
    with rt.database.write() as db:
        rt.unified_kg.write_communities_generation(
            db, nb, 0, [(f"cm-craft-{claim['generation']}-{i}", sorted(ms))
                        for i, (_k, ms) in enumerate(boards)],
            {m: m for m in members}, {m: 1.0 for m in members}, NOW, claim["generation"],
            board_keys=[key for key, _ms in boards])
        assert rt.unified_kg.flip_community_generation(
            db, nb, published_from=claim["community_generation"],
            generation=claim["generation"], now=NOW)
        rt.unified_kg.set_community_partition(db, nb, version=version, mode=mode)


def _rebuild(repo, nb: str, **kwargs) -> int:
    return repo._runtime.knowledge_lifecycle.rebuild_communities(nb, **kwargs)


def _side_of_bridge(boards) -> str:
    [(_key, members)] = [(k, ms) for k, ms in boards if BRIDGE in ms]
    return "a" if "K-a1" in members else "b"


def _boards_with_bridge_on(side: str):
    a = CLIQUE_A + ([BRIDGE] if side == "a" else [])
    b = CLIQUE_B + ([BRIDGE] if side == "b" else [])
    return [tuple(sorted(a)), tuple(sorted(b)), tuple(sorted(TRIANGLE))]


# ----------------------------------------------------------------- cold start


def test_the_first_build_is_cold_and_stamps_the_partition(repo, events):
    nb = _graph(repo)
    assert _rebuild(repo, nb) == 3
    boards = _published(repo, nb)
    side = _side_of_bridge(boards)
    # Fresh keys are the sha256 of the members (kg_partition.mint_board_key).
    assert boards == sorted((mint_board_key(ms), ms) for ms in _boards_with_bridge_on(side))
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "cold")
    partition = {
        "partition_mode": "cold", "partition_version": PARTITION_ALGO_VERSION,
        "partition_version_read": 0,
        "partition_cold_reason": "no_published_partition",
        "board_keys_carried": 0, "board_keys_minted": 3, "board_keys_rehashed": 0,
        "previous_boards": 0, "unstable_components": 0,
    }
    event = _rebuilt(events)
    assert {key: event[key] for key in partition} == partition
    assert event["communities"] == 3
    assert _edges_payload(repo, nb)["partition"] == partition


# ------------------------------------------------------------------ warm start


def test_a_forced_rebuild_with_a_published_partition_warm_starts(repo, events):
    """``force`` is what every real re-merge passes; it must not throw the
    partition history away."""
    nb = _graph(repo)
    _rebuild(repo, nb)
    before = _published(repo, nb)
    events.clear()
    assert _rebuild(repo, nb, force=True) == 3
    assert _published(repo, nb) == before
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"]) == ("warm", None)
    assert (event["board_keys_carried"], event["board_keys_minted"],
            event["previous_boards"]) == (3, 0, 3)
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "warm")


def test_a_warm_start_keeps_a_tied_alternative_and_carries_the_published_keys(repo, events):
    """The bridge node sits on either side at equal modularity. Publish the side
    the cold start did NOT pick, under keys of our own: the warm start keeps it
    (and the keys), a cold start would move the bridge back."""
    nb = _graph(repo)
    _rebuild(repo, nb)
    cold_side = _side_of_bridge(_published(repo, nb))
    other = "b" if cold_side == "a" else "a"
    crafted = list(zip(("bk-one", "bk-two", "bk-three"), _boards_with_bridge_on(other)))
    _publish(repo, nb, crafted)
    events.clear()
    _rebuild(repo, nb, force=True)
    assert _published(repo, nb) == sorted(crafted)
    event = _rebuilt(events)
    assert event["partition_mode"] == "warm"
    assert event["board_keys_carried"] == 3


def test_canonicals_gone_from_the_graph_still_count_toward_their_old_board(repo, events):
    """A published board with a member no longer in the graph: the member is
    passed as ``None`` (no KeyError) and still counts toward the old board's
    size, so the Jaccard is 4/5 -- carried -- not 4/4."""
    nb = _graph(repo, list(itertools.combinations(CLIQUE_A, 2))
                + list(itertools.combinations(CLIQUE_B, 2)))
    _publish(repo, nb, [("bk-a", tuple(CLIQUE_A) + ("K-gone",)), ("bk-b", tuple(CLIQUE_B))])
    _rebuild(repo, nb, force=True)
    assert _published(repo, nb) == [("bk-a", tuple(CLIQUE_A)), ("bk-b", tuple(CLIQUE_B))]
    event = _rebuilt(events)
    assert (event["partition_mode"], event["board_keys_carried"]) == ("warm", 2)


def test_keys_are_carried_from_the_published_generation_only(repo, events):
    """The same board under one key in a retired generation and another in the
    published one: the previous partition is the published generation (not the
    one before it, nor the one being built)."""
    nb = _graph(repo, list(itertools.combinations(CLIQUE_A, 2)))
    _publish(repo, nb, [("bk-retired", tuple(CLIQUE_A))])
    _publish(repo, nb, [("bk-published", tuple(CLIQUE_A))])
    _rebuild(repo, nb, force=True)
    assert _published(repo, nb) == [("bk-published", tuple(CLIQUE_A))]
    assert _rebuilt(events)["board_keys_carried"] == 1


# ------------------------------------------------------- back to a cold start


def test_a_partition_from_another_algorithm_version_starts_cold_and_carries_keys(repo, events):
    """Version 0 is every deployment's partition before this one: the next build
    starts cold -- the one-time re-partition -- and still carries the keys of
    boards whose members did not change."""
    nb = _graph(repo)
    cold_side = _side_of_bridge(_published_after(repo, nb))
    other = "b" if cold_side == "a" else "a"
    crafted = list(zip(("bk-one", "bk-two", "bk-three"), _boards_with_bridge_on(other)))
    _publish(repo, nb, crafted, version=0, mode="")
    events.clear()
    _rebuild(repo, nb, force=True)
    boards = _published(repo, nb)
    assert _side_of_bridge(boards) == cold_side          # cold: the tie went back
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"]) == (
        "cold", "partition_version_changed")
    # The triangle's members did not move: its key is carried; the two cliques
    # each changed by the bridge (J = 4/5 >= 1/2): carried too.
    assert dict((ms, key) for key, ms in boards)[tuple(TRIANGLE)] == "bk-three"
    assert event["board_keys_carried"] == 3
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "cold")


def test_a_legacy_generation_zero_partition_starts_cold_and_carries_its_ids(repo, events):
    """A notebook last built before the generational swap (or imported by
    merge_dbs) has its published boards in generation 0 under pointer 0, and
    v102/0083 backfilled each board's key with its id. Pointer 0 is still a
    published partition: the one-time cold start reads it and carries the old
    ids as keys."""
    nb = _graph(repo)
    cold_side = _side_of_bridge(_published_after(repo, nb))
    with repo._write() as db:
        db.execute("DELETE FROM community_members WHERE notebook_id=?", (nb,))
        db.execute("DELETE FROM communities WHERE notebook_id=?", (nb,))
        db.execute("UPDATE unified_kg_state SET community_generation=0, "
                   "community_partition_version=0, community_partition_mode='' "
                   "WHERE notebook_id=?", (nb,))
        legacy = list(zip(("cm-legacy-a", "cm-legacy-b", "cm-legacy-c"),
                          _boards_with_bridge_on(cold_side)))
        repo._runtime.unified_kg.write_communities_generation(
            db, nb, 0, [(cid, list(ms)) for cid, ms in legacy], {}, {}, NOW, 0,
            board_keys=[cid for cid, _ms in legacy])
    events.clear()
    _rebuild(repo, nb, force=True)
    assert _published(repo, nb) == sorted(legacy)
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"],
            event["previous_boards"], event["board_keys_carried"]) == (
        "cold", "partition_version_changed", 3, 3)


def test_a_previous_board_without_a_key_makes_the_previous_partition_unusable(repo, events):
    nb = _graph(repo, list(itertools.combinations(CLIQUE_A, 2)))
    _publish(repo, nb, [("", tuple(CLIQUE_A))])
    events.clear()
    _rebuild(repo, nb, force=True)
    [unusable] = [e for e in events if e.get("kind") == "community_partition_previous_unusable"]
    assert unusable["reason"] == "board_without_key"
    event = _rebuilt(events)
    assert (event["partition_cold_reason"], event["board_keys_minted"]) == (
        "previous_partition_unusable", 1)


def _published_after(repo, nb: str):
    _rebuild(repo, nb)
    return _published(repo, nb)


def test_request_cold_community_partition_makes_the_next_build_cold(repo, events):
    """``recluster_kg --cold-partition``: the request is the version column
    written back to 0 (the mode stays: it still says how the published
    partition was built), and it holds until a build publishes."""
    nb = _graph(repo)
    _rebuild(repo, nb)
    _rebuild(repo, nb, force=True)
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "warm")
    repo.request_cold_community_partition(nb)
    assert _state(repo, nb) == (0, "warm")
    events.clear()
    _rebuild(repo, nb, force=True)
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"]) == (
        "cold", "partition_version_changed")
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "cold")


def _request_cold_after_the_read(repo, monkeypatch) -> None:
    """Interleave a ``--cold-partition`` request into a build in flight: after
    the build has claimed its generation and read the partition version, before
    it publishes."""
    lifecycle = repo._runtime.knowledge_lifecycle
    original = lifecycle._partition_boards_for_rebuild

    def partition_then_request(*args, **kwargs):
        out = original(*args, **kwargs)
        repo.request_cold_community_partition(args[0])
        return out

    monkeypatch.setattr(lifecycle, "_partition_boards_for_rebuild", partition_then_request)


def test_a_cold_request_during_a_build_survives_its_publish(repo, events, monkeypatch):
    """The build in flight read version 1 and warm-starts; the operator's
    request writes 0 before it publishes. The publish CASes the version against
    what it read: the boards publish, the mode says how (warm), the version stays
    0, an event says why -- and the next build starts cold."""
    nb = _graph(repo)
    _rebuild(repo, nb)
    with monkeypatch.context() as patched:
        _request_cold_after_the_read(repo, patched)
        events.clear()
        _rebuild(repo, nb, force=True)
    [held] = [e for e in events if e.get("kind") == "community_partition_version_held"]
    assert held["reason"] == "version_changed_during_build"
    assert _rebuilt(events)["partition_mode"] == "warm"
    assert _state(repo, nb) == (0, "warm")
    _rebuild(repo, nb, force=True)
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"]) == (
        "cold", "partition_version_changed")
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "cold")
    assert not [e for e in events if e.get("kind") == "community_partition_version_held"]


def test_without_igraph_the_partition_falls_back_cold_and_is_stamped_version_0(
    repo, events, monkeypatch,
):
    """The networkx fallback's boards are not the current algorithm's partition:
    they are published with version 0 (mode ``cold_fallback``), so the next
    build with igraph starts cold rather than warm-starting from them."""
    nb = _graph(repo)
    _rebuild(repo, nb)
    with monkeypatch.context() as patched:
        patched.setitem(sys.modules, "igraph", None)
        events.clear()
        _rebuild(repo, nb, force=True)
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"],
            event["partition_version"]) == ("cold_fallback", "igraph_unavailable", 0)
    assert _state(repo, nb) == (0, "cold_fallback")
    assert not [e for e in events if e.get("kind") == "community_partition_version_held"]
    _rebuild(repo, nb, force=True)
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"]) == (
        "cold", "partition_version_changed")
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "cold")


def test_an_unusable_previous_partition_falls_back_cold(repo, events):
    """One canonical in two boards of the published generation is not a
    partition: the rebuild does not fail, it reports the fact and starts cold."""
    nb = _graph(repo, list(itertools.combinations(CLIQUE_A, 2)))
    _publish(repo, nb, [("bk-1", tuple(CLIQUE_A)), ("bk-2", ("K-a1",))])
    events.clear()
    assert _rebuild(repo, nb, force=True) == 1
    [unusable] = [e for e in events if e.get("kind") == "community_partition_previous_unusable"]
    assert unusable["reason"] == "canonical_in_two_boards"
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"]) == (
        "cold", "previous_partition_unusable")


def test_a_non_default_level_starts_cold_and_leaves_the_partition_columns(repo, events):
    """The two columns belong to the default level, like ``community_seq``. The
    precondition is a value a level-1 build would NOT write (version 0, mode
    'warm'), so a build that wrote them anyway shows."""
    nb = _graph(repo)
    _rebuild(repo, nb)
    _rebuild(repo, nb, force=True)
    repo.request_cold_community_partition(nb)
    assert _state(repo, nb) == (0, "warm")
    events.clear()
    _rebuild(repo, nb, level=1)
    event = _rebuilt(events)
    assert (event["partition_mode"], event["partition_cold_reason"]) == (
        "cold", "non_default_level")
    assert _state(repo, nb) == (0, "warm")


# ------------------------------------------------------------- determinism


def test_the_same_graph_and_previous_partition_give_byte_identical_boards(repo):
    """The contract replacing design 2026-07-25 §3.32 invariant 1: two notebooks
    with the same graph, relations inserted in opposite orders with flipped
    endpoints, get the same boards under the same keys -- cold, and warm."""
    first = _graph(repo, name="first")
    second = _graph(repo, list(reversed(EDGES)), name="second", flip=True)
    for nb in (first, second):
        _rebuild(repo, nb)
    assert _published(repo, first) == _published(repo, second)
    for nb in (first, second):
        _rebuild(repo, nb, force=True)
    assert _published(repo, first) == _published(repo, second)


def test_min_size_below_one_is_clamped(repo, events):
    """``COMMUNITY_MIN_SIZE`` has no lower bound; 0 still builds (as 1)."""
    nb = _graph(repo, [("K-p", "K-q")])
    repo.settings.community_min_size = 0
    repo._runtime.knowledge_lifecycle.settings.community_min_size = 0
    assert _rebuild(repo, nb) == 1
    assert _published(repo, nb) == [(mint_board_key(("K-p", "K-q")), ("K-p", "K-q"))]


def test_a_component_out_of_rounds_is_reported(repo, events, monkeypatch):
    monkeypatch.setattr(kg_partition, "LEIDEN_MAX_ITERATIONS", 0)
    nb = _graph(repo)
    _rebuild(repo, nb)
    [unstable] = [e for e in events if e.get("kind") == "community_partition_unstable"]
    assert unstable["unstable_components"] >= 1
    assert _rebuilt(events)["unstable_components"] == unstable["unstable_components"]


# ------------------------------------------------------- ledger-only backfill


def test_the_ledger_backfill_writes_no_partition(repo, events):
    """The backfill path reuses the published boards: it neither partitions nor
    touches the two state columns, and its ``community_edges`` payload carries no
    partition statistics (nothing was partitioned)."""
    nb = _graph(repo)
    _rebuild(repo, nb)
    before = _published(repo, nb)
    with repo._write() as db:
        db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (nb,))
    events.clear()
    _rebuild(repo, nb)
    assert [e["kind"] for e in events if e.get("kind", "").startswith(("communit", "kg_analysis_b"))] == [
        "kg_analysis_backfilled"]
    assert _published(repo, nb) == before
    assert _state(repo, nb) == (PARTITION_ALGO_VERSION, "cold")
    assert "partition" not in _edges_payload(repo, nb)


def test_the_board_overview_returns_the_board_keys(repo):
    nb = _graph(repo)
    _rebuild(repo, nb)
    overview = repo._runtime.unified_kg.community_overview(nb, limit=10, top_k=5)
    assert sorted(c["board_key"] for c in overview["communities"]) == [
        key for key, _ms in _published(repo, nb)]


def test_the_kg_analysis_route_returns_the_board_keys(tmp_path, monkeypatch):
    """``GET /notebooks/{id}/kg-analysis``: every board of the list carries its
    ``board_key`` beside its re-minted ``id`` (a read-only member included)."""
    from app.api import deps
    from tests.test_notebook_capability_guard import _client, _login

    client = _client(tmp_path, monkeypatch)
    owner = _login(client, "k00000001")
    nb = client.post("/api/notebooks", json={"name": "boards"}, headers=owner).json()["id"]
    member = _login(client, "k00000002")
    member_id = client.get("/api/me", headers=member).json()["id"]
    repository = deps.repository()
    repository._runtime.sharing.add_member(nb, member_id)
    _seed_graph(repository, nb, EDGES)
    repository._runtime.knowledge_lifecycle.rebuild_communities(nb)
    response = client.get(f"/api/notebooks/{nb}/kg-analysis", headers=member)
    assert response.status_code == 200, response.text
    boards = response.json()["boards"]["payload"]["communities"]
    assert sorted(board["board_key"] for board in boards) == [
        key for key, _ms in _published(repository, nb)]
    assert all(board["board_key"].startswith("bk-") for board in boards)


def test_the_previous_partition_fold_closes_the_row_stream_on_an_error():
    """On PostgreSQL the rows are a ``cursor().stream()`` generator: one left
    suspended keeps the connection ACTIVE and the caller's rollback blocks
    (see ``tests/test_fold_canonical_edges.py``). The fold closes it when a row
    makes the previous partition unusable."""
    closed: list = []

    def rows():
        try:
            yield {"canonical_id": "K-1", "board_key": "bk-1"}
            yield {"canonical_id": "K-2", "board_key": ""}
            yield {"canonical_id": "K-3", "board_key": "bk-3"}
        finally:
            closed.append(True)

    stream = rows()  # the test frame keeps referencing it: only the fold's close() runs finally
    with pytest.raises(ValueError):
        _fold_previous_partition(stream, PreviousPartitionBuilder(3),
                                 {"K-1": 0, "K-2": 1, "K-3": 2})
    assert closed == [True]
