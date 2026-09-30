"""PR-E2·E2-1: the service-layer retrieval legs obey the run's source ceiling
(audit B-3, B-4, B-5 service side).

Scenario: user A owns a shared notebook, user B is a member.  One visible
source (``src-vis``) and B's confirmed Memory (``src-mb``).  A asks with the
browser's all-selected freeze -- ``include [src-vis]``, A's own hidden half
(empty), ``narrowed=False`` -- which is also the shape of the default ceiling
every entry point installs (PR-E1).  Nothing derived from B's Memory may reach
A's prompt:

* ``follow_chain`` (B-3): a hop's evidence is filtered on every run; a hop
  left without evidence drops its chain, and B's Memory quote never becomes a
  chain anchor;
* weak-support relations (B-4): no target name supported only by B's Memory,
  and no relation extracted only from it;
* the chunk-mix KG overlay (B-5): ``kg_block`` / ``kg_id_map`` keep only the
  nodes that survive.

A notebook without mounted libraries and without anyone else's Memory gets
exactly the unscoped output, and no statement binds a source list.

PostgreSQL twin (store statements, EXPLAIN pins):
``tests/postgres/test_retrieval_leg_ceilings_pg.py``.
"""
from __future__ import annotations

import datetime
import json
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore
from app.services.embedding import FakeEmbedder
from app.services.kg.graph_reason import render_subgraph_context
from app.services.retrieval_service import RetrievalService
from app.services.source_scope import current_source_scope, source_scope_context
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    return instance


def _now(offset: int = 0) -> str:
    return (datetime.datetime(2026, 9, 1) + datetime.timedelta(seconds=offset)).isoformat()


def _source(db, nb, sid, *, memory_id=None):
    db.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
        "file_name,file_path,file_size,file_hash,summary,doc_type,memory_id,"
        "created_at,updated_at) VALUES (?,?,?,?,'extracted','parsed','d.md','',0,'',"
        "'','',?,?,?)",
        (sid, nb, sid, "memory" if memory_id else "markdown", memory_id, _now(), _now()),
    )


def _memory(db, nb, memory_id, owner):
    db.execute(
        "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES (?,?,?,'ask_answer','confirmed',"
        "'t','c',?,?)",
        (memory_id, nb, owner, _now(), _now()),
    )


def _shared(repo, *, b_memory=True):
    """(notebook id, A's user id) -- A owns, B is a member with a Memory."""
    a = repo.create_user("a00000001", "pw123456")
    b = repo.create_user("b00000002", "pw123456")
    token = set_request_user(a)
    try:
        nb = repo.create_notebook(NotebookCreate(name="shared")).id
        repo.add_member(nb, b.id)
    finally:
        reset_request_user(token)
    with repo._write() as db:
        _source(db, nb, "src-vis")
        if b_memory:
            _memory(db, nb, "mem-b", b.id)
            _source(db, nb, "src-mb", memory_id="mem-b")
    return nb, a.id


@contextmanager
def _all_selected(nb, owner, visible=("src-vis",)):
    """The browser's all-selected freeze (and PR-E1's default ceiling shape)."""
    with source_scope_context(nb, {
        "mode": "include", "source_ids": list(visible),
        "hidden_source_ids": [], "narrowed": False, "owner_id": owner,
    }):
        yield


def _ev(sid, quote):
    return {"source_id": sid, "source_title": sid, "element_id": f"el-{sid}",
            "element_type": "paragraph", "location_label": "p",
            "quote": quote, "quoted_span": quote, "confidence": 1.0}


def _ids_by_name(repo, nb):
    with repo._connect() as db:
        return {
            json.loads(row["payload"])["name"]: row["id"]
            for row in db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?", (nb,))
        }


# --------------------------------------------------------------- B-3 chains

def _seed_chain(repo, nb, *, second_hop_evidence):
    objects = [
        {"local_id": local, "object_type": "claim",
         "payload": {"name": name, "section_path": local},
         "evidence": [_ev("src-vis", f"{name} text")]}
        for local, name in (("A", "Premise A"), ("B", "Bridge B"),
                            ("C", "Conclusion C"))
    ]
    relations = [
        {"source_local_id": "A", "target_local_id": "B", "edge_type": "derived_from",
         "evidence": [_ev("src-vis", "Premise A leads to Bridge B")]},
        {"source_local_id": "B", "target_local_id": "C", "edge_type": "derived_from",
         "evidence": list(second_hop_evidence)},
    ]
    repo.store_kg(nb, "src-vis", objects, relations)
    return _ids_by_name(repo, nb)


def _chain(repo, nb, start):
    return repo.retrieval.follow_chain(
        nb, start, edge_type="derived_from", direction="out")


def test_a_hop_evidenced_only_by_another_members_memory_drops_the_chain(repo):
    nb, a = _shared(repo)
    ids = _seed_chain(repo, nb, second_hop_evidence=[
        _ev("src-mb", "B-PRIVATE says Bridge B yields Conclusion C")])

    unscoped = _chain(repo, nb, ids["Premise A"])
    assert len(unscoped.inferences) == 1  # the fixture reaches the leak

    with _all_selected(nb, a):
        scoped = _chain(repo, nb, ids["Premise A"])

    assert scoped.inferences == []
    quotes = [hop.quote for chain in scoped.inferences for hop in chain.hops]
    assert not any("B-PRIVATE" in quote for quote in quotes)


def test_another_members_memory_quote_never_becomes_the_chain_anchor(repo):
    """The hop survives on its in-ceiling evidence; the anchor quote
    (``primary_evidence``, what ``render_follow_chain_context`` cites) is the
    visible one, never B's Memory quote that the store listed first."""
    nb, a = _shared(repo)
    ids = _seed_chain(repo, nb, second_hop_evidence=[
        _ev("src-mb", "B-PRIVATE says Bridge B yields Conclusion C"),
        _ev("src-vis", "Bridge B leads to Conclusion C"),
    ])

    unscoped = _chain(repo, nb, ids["Premise A"])
    assert "B-PRIVATE" in unscoped.inferences[0].hops[1].quote

    with _all_selected(nb, a):
        scoped = _chain(repo, nb, ids["Premise A"])

    assert len(scoped.inferences) == 1
    hop = scoped.inferences[0].hops[1]
    assert hop.quote == "Bridge B leads to Conclusion C"
    assert [e["source_id"] for e in hop.evidence] == ["src-vis"]


def test_chains_are_value_identical_without_foreign_memory_or_without_scope(repo):
    nb, a = _shared(repo, b_memory=False)
    ids = _seed_chain(repo, nb, second_hop_evidence=[
        _ev("src-vis", "Bridge B leads to Conclusion C")])
    graph_result = repo.retrieval.graph.follow_chain(
        nb, ids["Premise A"], edge_type="derived_from", direction="out")

    unscoped = _chain(repo, nb, ids["Premise A"])
    with _all_selected(nb, a):
        scoped = _chain(repo, nb, ids["Premise A"])

    assert unscoped.inferences == graph_result.inferences
    assert scoped.inferences == graph_result.inferences
    assert [n.object_id for n in scoped.nodes] == [n.object_id for n in graph_result.nodes]


# ------------------------------------------------ B-4 weak-support (store)

def _raw_kg(db, nb, *, backfilled):
    """Hand-built canonical layer (both backends seed the same rows).

    Seed ``ko-s`` (visible).  Out-edges of it, all ``source_count`` 1:

    * ``ko-t1``  -- unclustered target, visible evidence: kept;
    * ``ko-t2``  -- unclustered target, ONLY Memory evidence, relation row
      stamped with the visible source: the target test alone must drop it;
    * ``K-good`` -- cluster with one visible member: kept;
    * ``K-mem``  -- cluster whose only member has Memory evidence: dropped;
    * ``ko-t5``  -- visible target, but the sample relation comes from the
      Memory source: the name read must drop it.
    """
    def obj(oid, sid, evidence_sids):
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,'concept','approved',"
            "?,?,?,?,?)",
            (oid, nb, json.dumps({"name": f"name {oid}"}),
             json.dumps([{"source_id": s, "quote": "q"} for s in evidence_sids]),
             sid, _now(), _now()),
        )
        for s in evidence_sids:
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                "VALUES (?,?,?)", (oid, s, nb))

    def rel(rid, target, sid):
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,'kind_of','[]',?)",
            (rid, nb, sid, "ko-s", target, _now()))

    obj("ko-s", "src-vis", ["src-vis"])
    obj("ko-t1", "src-vis", ["src-vis"])
    obj("ko-t2", "src-mb", ["src-mb"])
    obj("ko-g", "src-vis", ["src-vis"])
    obj("ko-m", "src-mb", ["src-mb"])
    obj("ko-t5", "src-vis", ["src-vis"])
    for cid, member, name in (("cc-g", "ko-g", "Good cluster"),
                              ("cc-m", "ko-m", "Memory cluster")):
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at) VALUES (?,?,?,?,?,'concept',?)",
            (cid, nb, "K-good" if member == "ko-g" else "K-mem", member, name, _now()))
    rel("kr-1", "ko-t1", "src-vis")
    rel("kr-2", "ko-t2", "src-vis")
    rel("kr-3", "ko-g", "src-vis")
    rel("kr-4", "ko-m", "src-mb")
    rel("kr-5", "ko-t5", "src-mb")
    for rid, target in (("kr-1", "ko-t1"), ("kr-2", "ko-t2"), ("kr-3", "K-good"),
                        ("kr-4", "K-mem"), ("kr-5", "ko-t5")):
        db.execute(
            "INSERT INTO canonical_relations (notebook_id,canonical_src,edge_type,"
            "canonical_tgt,support_count,source_count,sample_relation_ids,updated_at) "
            "VALUES (?,?,'kind_of',?,1,1,?,?)",
            (nb, "ko-s", target, json.dumps([rid]), _now()))
    db.execute(
        "INSERT INTO unified_kg_state (notebook_id,source_index_backfilled,updated_at) "
        "VALUES (?,?,?) ON CONFLICT(notebook_id) DO UPDATE SET "
        "source_index_backfilled=excluded.source_index_backfilled",
        (nb, 1 if backfilled else 0, _now()))


@pytest.mark.parametrize("backfilled", [True, False])
def test_weak_support_store_keeps_only_targets_supported_inside_the_ceiling(
    repo, backfilled,
):
    nb, _a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=backfilled)
    with repo._connect() as db:
        unbounded = UnifiedKgStore.weak_support_relation_rows(db, nb, ["ko-s"], 2, 24)
        bound = UnifiedKgStore.weak_support_relation_rows(
            db, nb, ["ko-s"], 2, 24, allowed_source_ids=["src-vis"])
        denied = UnifiedKgStore.weak_support_relation_rows(
            db, nb, ["ko-s"], 2, 24, allowed_source_ids=[])

    assert sorted(r["canonical_tgt"] for r in unbounded) == [
        "K-good", "K-mem", "ko-t1", "ko-t2", "ko-t5"]
    assert sorted(r["canonical_tgt"] for r in bound) == ["K-good", "ko-t1", "ko-t5"]
    assert denied == []
    # Same row shape as the unbounded read.
    assert set(dict(bound[0]).keys()) == set(dict(unbounded[0]).keys())


def test_weak_support_gate_sits_before_the_limit(repo):
    """With LIMIT 1 the unsupported targets sort first (``canonical_tgt``
    ``K-mem`` < ``ko-*``); a post-LIMIT filter would return nothing."""
    nb, _a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=True)
        db.execute("DELETE FROM canonical_relations WHERE canonical_tgt='K-good'")
    with repo._connect() as db:
        rows = UnifiedKgStore.weak_support_relation_rows(
            db, nb, ["ko-s"], 2, 1, allowed_source_ids=["src-vis"])
    assert [r["canonical_tgt"] for r in rows] == ["ko-t1"]


def test_endpoint_names_carry_the_sample_source_and_honour_the_ceiling(repo):
    nb, _a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=True)
    with repo._connect() as db:
        unbounded = UnifiedKgStore.relation_endpoint_name_rows(
            db, nb, ["kr-1", "kr-5"])
        bound = UnifiedKgStore.relation_endpoint_name_rows(
            db, nb, ["kr-1", "kr-5"], allowed_source_ids=["src-vis"])
        denied = UnifiedKgStore.relation_endpoint_name_rows(
            db, nb, ["kr-1"], allowed_source_ids=[])
    assert {(r["rid"], r["source_id"]) for r in unbounded} == {
        ("kr-1", "src-vis"), ("kr-5", "src-mb")}
    assert [r["rid"] for r in bound] == ["kr-1"]
    assert denied == []


# ---------------------------------------------- B-4 weak-support (service)

def _concept(local, name, sid):
    return {"local_id": local, "object_type": "concept",
            "payload": {"name": name, "section_path": "1"},
            "evidence": [_ev(sid, f"{name} in {sid}")]}


def _edge(src, tgt, sid):
    return {"source_local_id": src, "target_local_id": tgt, "edge_type": "kind_of",
            "evidence": [_ev(sid, f"{src} kind of {tgt}")]}


def _seed_weak(repo, nb, *, b_memory=True):
    repo.store_kg(nb, "src-vis", [
        _concept("A", "版图设计", "src-vis"), _concept("B", "寄生电容", "src-vis"),
    ], [_edge("A", "B", "src-vis")])
    if b_memory:
        repo.store_kg(nb, "src-mb", [
            _concept("A", "版图设计", "src-mb"), _concept("X", "SECRETPROJECT", "src-mb"),
        ], [_edge("A", "X", "src-mb")])
    repo.rebuild_unified_kg(nb)
    repo.rebuild_canonical_relations(nb, force=True)
    with repo._connect() as db:
        return [
            row["id"] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id=?",
                (nb, "src-vis"))
        ]


def _names(rows):
    return sorted((row.src_name, row.tgt_name) for row in rows)


def test_weak_support_under_all_selected_freeze_hides_memory_only_targets(repo):
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)

    unscoped = repo.retrieval.weak_support_relations(nb, seeds)
    assert "SECRETPROJECT" in {row.tgt_name for row in unscoped}  # fixture control

    with _all_selected(nb, a):
        scoped = repo.retrieval.weak_support_relations(nb, seeds)

    assert "SECRETPROJECT" not in {row.tgt_name for row in scoped}
    assert _names(scoped) == [("版图设计", "寄生电容")]


class _Spy:
    """Wraps the unified-KG store and records each call's keyword arguments."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if name not in {"weak_support_relation_rows", "relation_endpoint_name_rows"}:
            return target

        def call(*args, **kwargs):
            self.calls.append((name, dict(kwargs)))
            return target(*args, **kwargs)
        return call


def test_weak_support_without_foreign_memory_is_identical_and_binds_no_list(
    repo, monkeypatch,
):
    nb, a = _shared(repo, b_memory=False)
    seeds = _seed_weak(repo, nb, b_memory=False)
    unscoped = repo.retrieval.weak_support_relations(nb, seeds)
    spy = _Spy(repo.retrieval.candidates.unified_kg)
    monkeypatch.setattr(repo.retrieval.candidates, "unified_kg", spy)

    with _all_selected(nb, a):
        scoped = repo.retrieval.weak_support_relations(nb, seeds)
        memo = dict(current_source_scope()._ceiling_binds_memo)

    assert scoped == unscoped and scoped
    assert spy.calls and all(kwargs == {} for _name, kwargs in spy.calls)
    assert memo == {nb: False}


def test_weak_support_fast_path_verifies_on_read_and_flips_the_run(repo, monkeypatch):
    """The verdict said "does not bind" (no foreign Memory at the time), but a
    sample relation read afterwards comes from a source outside the freeze --
    here B's Memory, confirmed after the verdict.  The row is not shown, the
    drift is recorded for the rest of the run, and the probe is re-read bound."""
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)
    service = RetrievalService(
        candidates=repo.retrieval.candidates, graph=repo.retrieval.graph,
        community_queries=repo.retrieval._community_queries,
        ceiling_verdict=lambda _nb: False,
    )
    spy = _Spy(repo.retrieval.candidates.unified_kg)
    monkeypatch.setattr(repo.retrieval.candidates, "unified_kg", spy)

    with _all_selected(nb, a):
        rows = service.weak_support_relations(nb, seeds)
        memo = dict(current_source_scope()._ceiling_binds_memo)

    assert "SECRETPROJECT" not in {row.tgt_name for row in rows}
    assert _names(rows) == [("版图设计", "寄生电容")]
    assert memo == {nb: True}
    bound = [kwargs for _name, kwargs in spy.calls if kwargs]
    assert bound and all(
        list(kwargs["allowed_source_ids"]) == ["src-vis"] for kwargs in bound)


def test_weak_support_is_off_for_a_narrowed_run(repo):
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)
    with source_scope_context(nb, {"mode": "include", "source_ids": ["src-vis"],
                                   "narrowed": True, "owner_id": a}):
        assert repo.retrieval.weak_support_relations(nb, seeds) == []


# ----------------------------------------------- SQLite plans (no ANALYZE)

class _Recorder:
    def __init__(self, db):
        self.db = db
        self.statements: list[tuple[str, list]] = []

    def execute(self, sql, params=()):
        self.statements.append((sql, list(params)))
        return self.db.execute(sql, params)


def _plan(db, sql, params) -> str:
    rows = db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return "\n".join(str(row["detail"]) for row in rows)


@pytest.mark.parametrize("backfilled", [True, False])
def test_sqlite_bound_weak_support_plans_probe_by_key_without_analyze(
    repo, backfilled,
):
    nb, _a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=backfilled)
    with repo._connect() as db:
        assert db.execute(
            "SELECT count(*) AS c FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()["c"] == 0 or db.execute(
            "SELECT count(*) AS c FROM sqlite_stat1").fetchone()["c"] == 0
        recorder = _Recorder(db)
        UnifiedKgStore.weak_support_relation_rows(
            recorder, nb, ["ko-s"], 2, 24, allowed_source_ids=["src-vis"])
        UnifiedKgStore.relation_endpoint_name_rows(
            recorder, nb, ["kr-1"], allowed_source_ids=["src-vis"])
        probe, names = [s for s in recorder.statements if "json_each" in s[0]]
        probe_plan = _plan(db, *probe)
        names_plan = _plan(db, *names)

    # The canonical probe seeks the primary key by (notebook_id, canonical_src).
    assert "canonical_relations" in probe_plan
    assert "SCAN cr" not in probe_plan, probe_plan
    # Cluster support: the (notebook_id, canonical_id) index, never a scan.
    assert "SCAN kc" not in probe_plan, probe_plan
    if backfilled:
        # Reverse index probed per object (``object_id=?``), never per
        # ceiling id (``idx_kos_source``) and never scanned.
        assert "SEARCH kos" in probe_plan and "(object_id=?)" in probe_plan, probe_plan
        assert "idx_kos_source" not in probe_plan, probe_plan
        assert "SCAN kos" not in probe_plan, probe_plan
    else:
        assert "SCAN ko " not in probe_plan + " ", probe_plan
    # Names: relation primary keys drive; the ceiling only filters.
    assert "SCAN kr" not in names_plan, names_plan
    assert "idx_knowledge_relations_source" not in names_plan, names_plan


# ------------------------------------------------------ B-5 overlay (mix)

def _overlay_seed(repo, nb):
    repo.store_kg(nb, "src-vis", [
        _concept("P", "Public root", "src-vis"),
        _concept("Q", "Public leaf", "src-vis"),
        _concept("R", "Public other", "src-vis"),
    ], [])
    repo.store_kg(nb, "src-mb", [_concept("S", "SECRETNODE", "src-mb")], [])
    return _ids_by_name(repo, nb)


def _overlay(nb, ids):
    def node(name):
        return {"object_id": ids[name], "object_type": "concept", "name": name,
                "tier": "personal", "notebook_id": nb}

    def edge(quote):
        return {"edge_type": "related_to", "tier": "personal",
                "evidence": [{"quote": quote}]}

    subgraph = [
        (node("Public root"), None, None),
        (node("SECRETNODE"), edge("SECRET EDGE QUOTE"), ids["Public root"]),
        (node("Public leaf"), edge("VIA SECRET QUOTE"), ids["SECRETNODE"]),
        (node("Public other"), edge("PUBLIC QUOTE"), ids["Public root"]),
    ]
    return render_subgraph_context(subgraph, id_offset=1000, active_notebook_id=nb)


def _mix(repo, monkeypatch, nb, block, id_map):
    monkeypatch.setattr(
        repo.retrieval.candidates, "_mix_retrieve",
        lambda *_args: ([], block, dict(id_map), [], 0),
    )
    return repo.retrieval.mixed_chunk_candidates(nb, "q", "", ["q"])


def test_mix_overlay_keeps_only_surviving_nodes(repo, monkeypatch):
    nb, a = _shared(repo)
    ids = _overlay_seed(repo, nb)
    block, id_map = _overlay(nb, ids)
    assert "SECRETNODE" in block and "SECRET EDGE QUOTE" in block  # control

    with _all_selected(nb, a):
        _chunks, kg_block, kg_id_map, _hits, _ppr = _mix(
            repo, monkeypatch, nb, block, id_map)

    assert "SECRETNODE" not in kg_block
    assert "SECRET EDGE QUOTE" not in kg_block
    assert "VIA SECRET QUOTE" not in kg_block
    assert ids["SECRETNODE"] not in {v["object_id"] for v in kg_id_map.values()}
    assert {v["name"] for v in kg_id_map.values()} == {
        "Public root", "Public leaf", "Public other"}
    assert all("SECRET" not in v["snippet"] for v in kg_id_map.values())
    # What is unrelated to the dropped node is untouched.
    assert "PUBLIC QUOTE" in kg_block
    assert "Public root --related_to--> [k1004] Public other" in kg_block
    for key in kg_id_map:
        assert f"{key}: " in kg_block


def test_mix_overlay_is_returned_as_is_without_scope_or_foreign_memory(
    repo, monkeypatch,
):
    nb, a = _shared(repo, b_memory=False)
    repo.store_kg(nb, "src-vis", [
        _concept("P", "Public root", "src-vis"), _concept("S", "SECRETNODE", "src-vis"),
        _concept("Q", "Public leaf", "src-vis"), _concept("R", "Public other", "src-vis"),
    ], [])
    ids = _ids_by_name(repo, nb)
    block, id_map = _overlay(nb, ids)

    unscoped = _mix(repo, monkeypatch, nb, block, id_map)
    with _all_selected(nb, a):
        scoped = _mix(repo, monkeypatch, nb, block, id_map)

    assert unscoped[1] == block and unscoped[2] == id_map
    assert scoped[1] == block and scoped[2] == id_map


def test_mix_overlay_drops_everything_when_nothing_survives(repo, monkeypatch):
    nb, a = _shared(repo)
    ids = _overlay_seed(repo, nb)
    block, id_map = render_subgraph_context(
        [({"object_id": ids["SECRETNODE"], "object_type": "concept",
           "name": "SECRETNODE", "tier": "personal", "notebook_id": nb}, None, None)],
        id_offset=1000, active_notebook_id=nb)
    with _all_selected(nb, a):
        result = _mix(repo, monkeypatch, nb, block, id_map)
    assert result[1:3] == ("", {})

