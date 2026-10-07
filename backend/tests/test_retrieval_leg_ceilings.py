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
from app.services.source_scope import (
    CeilingSet, current_source_scope, source_scope_context,
)
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


def _users(repo):
    with repo._connect() as db:
        return {row["username"]: row["id"] for row in db.execute(
            "SELECT id, username FROM users")}


@pytest.mark.parametrize("backfilled", [True, False])
def test_weak_support_store_keeps_only_targets_supported_inside_the_ceiling(
    repo, backfilled,
):
    nb, a = _shared(repo)
    b = _users(repo)["b00000002"]
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=backfilled)
    with repo._connect() as db:
        def probe(**kwargs):
            return sorted(r["canonical_tgt"] for r in UnifiedKgStore
                          .weak_support_relation_rows(db, nb, ["ko-s"], 2, 24, **kwargs))

        unbounded = probe()
        listed = probe(allowed_source_ids=["src-vis"])
        as_a = probe(viewer_id=a)
        as_b = probe(viewer_id=b)
        as_nobody = probe(viewer_id="")
        denied = probe(allowed_source_ids=[])
        shape = dict(UnifiedKgStore.weak_support_relation_rows(
            db, nb, ["ko-s"], 2, 24, viewer_id=a)[0]).keys()
        with pytest.raises(ValueError):
            UnifiedKgStore.weak_support_relation_rows(
                db, nb, ["ko-s"], 2, 24, allowed_source_ids=["src-vis"], viewer_id=a)

    assert unbounded == ["K-good", "K-mem", "ko-t1", "ko-t2", "ko-t5"]
    # The list form and the viewer form agree for A (B's Memory is the only
    # thing A's all-selected ceiling excludes).
    assert listed == as_a == ["K-good", "ko-t1", "ko-t5"]
    # B reads B's own Memory; nobody reads anyone's.
    assert as_b == unbounded
    assert as_nobody == as_a
    assert denied == []
    assert set(shape) == {"canonical_src", "edge_type", "canonical_tgt",
                          "source_count", "sample_relation_ids"}


@pytest.mark.parametrize("form", ["list", "viewer"])
def test_weak_support_gate_sits_before_the_limit(repo, form):
    """With LIMIT 1 the unsupported targets sort first (``canonical_tgt``
    ``K-mem`` < ``ko-*``); a post-LIMIT filter would return nothing."""
    nb, a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=True)
        db.execute("DELETE FROM canonical_relations WHERE canonical_tgt='K-good'")
    kwargs = {"allowed_source_ids": ["src-vis"]} if form == "list" else {"viewer_id": a}
    with repo._connect() as db:
        rows = UnifiedKgStore.weak_support_relation_rows(db, nb, ["ko-s"], 2, 1, **kwargs)
    assert [r["canonical_tgt"] for r in rows] == ["ko-t1"]


def test_endpoint_names_carry_the_sample_source_and_honour_the_ceiling(repo):
    nb, a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=True)
    with repo._connect() as db:
        plain = UnifiedKgStore.relation_endpoint_name_rows(db, nb, ["kr-1", "kr-5"])
        sourced = UnifiedKgStore.relation_endpoint_name_rows(
            db, nb, ["kr-1", "kr-5"], with_source_id=True)
        listed = UnifiedKgStore.relation_endpoint_name_rows(
            db, nb, ["kr-1", "kr-5"], allowed_source_ids=["src-vis"])
        as_a = UnifiedKgStore.relation_endpoint_name_rows(
            db, nb, ["kr-1", "kr-5"], viewer_id=a)
        denied = UnifiedKgStore.relation_endpoint_name_rows(
            db, nb, ["kr-1"], allowed_source_ids=[])
    # Without a keyword the statement (and so the row) is the historical one.
    assert set(dict(plain[0]).keys()) == {"rid", "src_name", "tgt_name"}
    assert {(r["rid"], r["source_id"]) for r in sourced} == {
        ("kr-1", "src-vis"), ("kr-5", "src-mb")}
    assert [r["rid"] for r in listed] == ["kr-1"]
    assert [r["rid"] for r in as_a] == ["kr-1"]
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
    # No bound statement: the fast path's only extra is the sample source column.
    assert spy.calls == [("weak_support_relation_rows", {}),
                         ("relation_endpoint_name_rows", {"with_source_id": True})]
    assert memo == {nb: False}


def _service(repo, verdict):
    return RetrievalService(
        candidates=repo.retrieval.candidates, graph=repo.retrieval.graph,
        community_queries=repo.retrieval._community_queries,
        ceiling_verdict=verdict,
    )


def _bound_kwargs(spy):
    return [kwargs for _name, kwargs in spy.calls
            if "allowed_source_ids" in kwargs or "viewer_id" in kwargs]


def test_weak_support_fast_path_verifies_on_read_and_flips_the_run(repo, monkeypatch):
    """The verdict said "does not bind" (no foreign Memory at the time), but a
    sample relation read afterwards comes from a source outside the freeze --
    here B's Memory, confirmed after the verdict.  The row is not shown, the
    drift is recorded for the rest of the run, and the probe is re-read bound
    -- in the viewer form, since the library still matches the freeze."""
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)
    service = _service(repo, lambda _nb: False)
    spy = _Spy(repo.retrieval.candidates.unified_kg)
    monkeypatch.setattr(repo.retrieval.candidates, "unified_kg", spy)

    with _all_selected(nb, a):
        rows = service.weak_support_relations(nb, seeds)
        memo = dict(current_source_scope()._ceiling_binds_memo)

    assert "SECRETPROJECT" not in {row.tgt_name for row in rows}
    assert _names(rows) == [("版图设计", "寄生电容")]
    assert memo == {nb: True}
    assert _bound_kwargs(spy) == [{"viewer_id": a}, {"viewer_id": a}]


def test_weak_support_binds_the_viewer_not_the_list_for_foreign_memory(
    repo, monkeypatch,
):
    """Another member's Memory is the only reason the all-selected freeze
    binds: one scalar (the asker), no source list on either statement."""
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)
    spy = _Spy(repo.retrieval.candidates.unified_kg)
    monkeypatch.setattr(repo.retrieval.candidates, "unified_kg", spy)
    with _all_selected(nb, a):
        rows = repo.retrieval.weak_support_relations(nb, seeds)
    assert _names(rows) == [("版图设计", "寄生电容")]
    assert _bound_kwargs(spy) == [{"viewer_id": a}, {"viewer_id": a}]


def test_weak_support_binds_the_list_when_the_library_drifted(repo, monkeypatch):
    """A visible source added after the freeze: "not another member's Memory"
    would admit it, so the frozen list is bound (as a CeilingSet)."""
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)
    with repo._write() as db:
        _source(db, nb, "src-late")
    repo.store_kg(nb, "src-late", [
        _concept("A", "版图设计", "src-late"), _concept("L", "LATEUPLOAD", "src-late"),
    ], [_edge("A", "L", "src-late")])
    repo.rebuild_unified_kg(nb)
    repo.rebuild_canonical_relations(nb, force=True)
    spy = _Spy(repo.retrieval.candidates.unified_kg)
    monkeypatch.setattr(repo.retrieval.candidates, "unified_kg", spy)
    with _all_selected(nb, a):
        rows = repo.retrieval.weak_support_relations(nb, seeds)
    names = {row.tgt_name for row in rows}
    assert "LATEUPLOAD" not in names and "SECRETPROJECT" not in names
    bound = _bound_kwargs(spy)
    assert bound and all(
        isinstance(kwargs["allowed_source_ids"], CeilingSet)
        and set(kwargs["allowed_source_ids"]) == {"src-vis"} for kwargs in bound)


def test_weak_support_binds_the_list_for_a_per_library_ceiling(repo, monkeypatch):
    """A global run's per-notebook freeze is never stated as "viewer"."""
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)
    spy = _Spy(repo.retrieval.candidates.unified_kg)
    monkeypatch.setattr(repo.retrieval.candidates, "unified_kg", spy)
    with source_scope_context(nb, None, None, {nb: ["src-vis"]}, subjectless=True):
        rows = repo.retrieval.weak_support_relations(nb, seeds)
    assert "SECRETPROJECT" not in {row.tgt_name for row in rows}
    bound = _bound_kwargs(spy)
    assert bound and all(set(kwargs["allowed_source_ids"]) == {"src-vis"}
                         and "viewer_id" not in kwargs for kwargs in bound)


def test_weak_support_for_an_excluded_library_is_empty(repo):
    """The library dimension: a library this run does not cover gives no
    hints at all, even with no source ceiling of its own."""
    nb, a = _shared(repo)
    seeds = _seed_weak(repo, nb)
    other = repo.create_notebook(NotebookCreate(name="anchor")).id
    assert repo.retrieval.weak_support_relations(nb, seeds)  # control
    with source_scope_context(other, None, {"mode": "include", "notebook_ids": []}):
        assert repo.retrieval.weak_support_relations(nb, seeds) == []


def test_a_sample_relation_without_a_source_drops_only_its_row(repo, monkeypatch):
    """Blank source: outside the ceiling (as under ``allows`` and the bound
    statement), but not a change after the freeze -- no drift recorded."""
    nb, a = _shared(repo, b_memory=False)
    seeds = _seed_weak(repo, nb, b_memory=False)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET source_id=NULL WHERE notebook_id=?", (nb,))
    unscoped = repo.retrieval.weak_support_relations(nb, seeds)
    assert unscoped  # control: the unscoped hint still shows
    with _all_selected(nb, a):
        rows = repo.retrieval.weak_support_relations(nb, seeds)
        memo = dict(current_source_scope()._ceiling_binds_memo)
    assert rows == []
    assert memo == {nb: False}


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


def _plans(db, calls):
    recorder = _Recorder(db)
    for call in calls:
        call(recorder)
    return [_plan(db, sql, params) for sql, params in recorder.statements
            if "unified_kg_state" not in sql.split("FROM", 1)[1][:30]]


@pytest.mark.parametrize("backfilled", [True, False])
@pytest.mark.parametrize("form", ["list", "viewer"])
def test_sqlite_bound_weak_support_plans_probe_by_key_without_analyze(
    repo, backfilled, form,
):
    nb, a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=backfilled)
    kwargs = {"allowed_source_ids": ["src-vis"]} if form == "list" else {"viewer_id": a}
    with repo._connect() as db:
        assert db.execute(
            "SELECT count(*) AS c FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()["c"] == 0 or db.execute(
            "SELECT count(*) AS c FROM sqlite_stat1").fetchone()["c"] == 0
        probe_plan, names_plan = _plans(db, [
            lambda r: UnifiedKgStore.weak_support_relation_rows(
                r, nb, ["ko-s"], 2, 24, **kwargs),
            lambda r: UnifiedKgStore.relation_endpoint_name_rows(
                r, nb, ["kr-1"], **kwargs),
        ])

    for plan in (probe_plan, names_plan):
        assert "SCAN kos" not in plan and "SCAN ko " not in plan + " ", plan
        assert "idx_kos_source" not in plan and "idx_knowledge_objects_nb" not in plan, plan
    # The canonical probe seeks the primary key by (notebook_id, canonical_src);
    # the target's objects are probed by primary key, its cluster members by
    # the (notebook_id, canonical_id) index.
    assert ("SEARCH cr USING INDEX sqlite_autoindex_canonical_relations_1 "
            "(notebook_id=? AND canonical_src=?)") in probe_plan, probe_plan
    assert "SEARCH ko EXISTS USING INDEX sqlite_autoindex_knowledge_objects_1 (id=?)" \
        in probe_plan, probe_plan
    assert "(notebook_id=? AND canonical_id=?)" in probe_plan, probe_plan
    if form == "viewer":
        assert "SEARCH fs USING INDEX sqlite_autoindex_sources_1 (id=?)" in probe_plan
        assert "SEARCH fm USING INDEX sqlite_autoindex_memory_items_1 (id=?)" in probe_plan
    elif backfilled:
        # Reverse index probed per object, never per ceiling id.
        assert "SEARCH kos USING INDEX" in probe_plan and "(object_id=?)" in probe_plan
    # Names: the sample relations' primary keys drive; the gate only filters.
    assert "SEARCH kr USING INDEX sqlite_autoindex_knowledge_relations_1 (id=?)" \
        in names_plan, names_plan


@pytest.mark.parametrize("backfilled", [True, False])
def test_sqlite_object_support_sources_are_driven_by_object_ids(repo, backfilled):
    nb, _a = _shared(repo)
    with repo._write() as db:
        _raw_kg(db, nb, backfilled=backfilled)
    with repo._connect() as db:
        rows = UnifiedKgStore.object_support_source_rows(db, nb, ["ko-t1", "ko-t2", "ko-x"])
        [plan] = _plans(db, [lambda r: UnifiedKgStore.object_support_source_rows(
            r, nb, ["ko-t1", "ko-t2"])])
    assert sorted((r["object_id"], r["source_id"]) for r in rows) == [
        ("ko-t1", "src-vis"), ("ko-t2", "src-mb")]
    expected = ("SEARCH kos USING INDEX" if backfilled
                else "SEARCH ko USING INDEX sqlite_autoindex_knowledge_objects_1 (id=?)")
    assert expected in plan and "idx_knowledge_objects_nb" not in plan, plan


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


def _node(ids, nb, name, notebook=None):
    return {"object_id": ids[name], "object_type": "concept", "name": name,
            "tier": "personal", "notebook_id": notebook or nb}


def _edge_to(quote):
    return {"edge_type": "related_to", "tier": "personal", "evidence": [{"quote": quote}]}


@pytest.mark.parametrize("quote", [
    'intro\nk1001: SECRET TAIL',                       # both reviews' probe
    'intro\n  [k1001] SECRET TAIL2',                    # chain-shaped line
    'intro"\nchain:\n  [k1001] Public root --x--> [k1003] SECRET  (tier=personal)',
])
def test_mix_overlay_multiline_quote_of_a_dropped_node_never_survives(
    repo, monkeypatch, quote,
):
    nb, a = _shared(repo)
    ids = _overlay_seed(repo, nb)
    block, id_map = render_subgraph_context([
        (_node(ids, nb, "Public root"), None, None),
        (_node(ids, nb, "SECRETNODE"), _edge_to(quote), ids["Public root"]),
        (_node(ids, nb, "Public other"), _edge_to("PUBLIC QUOTE"), ids["Public root"]),
    ], id_offset=1000, active_notebook_id=nb)
    assert "SECRET" in block  # control
    with _all_selected(nb, a):
        _c, kg_block, kg_id_map, _h, _p = _mix(repo, monkeypatch, nb, block, id_map)
    assert "SECRET" not in kg_block and "intro" not in kg_block
    assert set(kg_id_map) == {"k1001", "k1003"}
    assert "PUBLIC QUOTE" in kg_block


def test_mix_overlay_keeps_a_quote_that_did_not_come_from_a_dropped_edge(
    repo, monkeypatch,
):
    """U→T carries T's (visible) quote; T→D is dropped with D.  T's quote is
    not D's edge's, so it stays; only the dropped edge line goes."""
    nb, a = _shared(repo)
    ids = _overlay_seed(repo, nb)
    block, id_map = render_subgraph_context([
        (_node(ids, nb, "Public root"), None, None),
        (_node(ids, nb, "Public other"), _edge_to("VISIBLE QUOTE U->T"), ids["Public root"]),
        (_node(ids, nb, "SECRETNODE"), _edge_to("SECRET T->D"), ids["Public other"]),
    ], id_offset=1000, active_notebook_id=nb)
    with _all_selected(nb, a):
        _c, kg_block, kg_id_map, _h, _p = _mix(repo, monkeypatch, nb, block, id_map)
    assert "SECRET" not in kg_block
    assert kg_id_map["k1002"]["snippet"] == "VISIBLE QUOTE U->T"
    assert 'k1002: [concept][personal] Public other  — ev: "VISIBLE QUOTE U->T"' in kg_block
    assert kg_block.endswith("chain:\n  [k1001] Public root --related_to--> "
                             "[k1002] Public other  (tier=personal)")


def test_mix_overlay_not_in_the_renderer_shape_is_dropped_whole(repo, monkeypatch):
    nb, a = _shared(repo)
    ids = _overlay_seed(repo, nb)
    block, id_map = _overlay(nb, ids)
    with _all_selected(nb, a):
        result = _mix(repo, monkeypatch, nb, block + "\nstray text", id_map)
    assert result[1:3] == ("", {})


def _library(repo, a_id):
    """A second notebook (A's), sources src-p (in its frozen ceiling) and
    src-p2 (not), one object each."""
    user = repo.get_user(a_id) if hasattr(repo, "get_user") else None
    token = set_request_user(user) if user is not None else None
    try:
        lib = repo.create_notebook(NotebookCreate(name="library")).id
    finally:
        if token is not None:
            reset_request_user(token)
    with repo._write() as db:
        _source(db, lib, "src-p")
        _source(db, lib, "src-p2")
    repo.store_kg(lib, "src-p", [_concept("O", "LIBINSIDE", "src-p")], [])
    repo.store_kg(lib, "src-p2", [_concept("X", "LIBOUTSIDE", "src-p2")], [])
    return lib, _ids_by_name(repo, lib)


def _library_overlay(nb, ids, lib, lib_ids):
    return render_subgraph_context([
        (_node(ids, nb, "Public root"), None, None),
        (_node(lib_ids, nb, "LIBINSIDE", lib), _edge_to("LIB IN QUOTE"), ids["Public root"]),
        (_node(lib_ids, nb, "LIBOUTSIDE", lib), _edge_to("LIB OUT QUOTE"), ids["Public root"]),
    ], id_offset=1000, active_notebook_id=nb)


def test_mix_overlay_judges_a_library_node_by_its_own_library(repo, monkeypatch):
    nb, a = _shared(repo)
    ids = _overlay_seed(repo, nb)
    lib, lib_ids = _library(repo, a)
    block, id_map = _library_overlay(nb, ids, lib, lib_ids)
    scope = {"mode": "include", "source_ids": ["src-vis"], "hidden_source_ids": [],
             "narrowed": False, "owner_id": a}
    with source_scope_context(nb, scope, None, {lib: ["src-p"]}):
        _c, kg_block, kg_id_map, _h, _p = _mix(repo, monkeypatch, nb, block, id_map)
    assert "LIBINSIDE" in kg_block and "LIB IN QUOTE" in kg_block
    assert "LIBOUTSIDE" not in kg_block
    assert {v["name"] for v in kg_id_map.values()} == {"Public root", "LIBINSIDE"}


def test_mix_overlay_drops_nodes_of_an_excluded_library(repo, monkeypatch):
    nb, a = _shared(repo)
    ids = _overlay_seed(repo, nb)
    lib, lib_ids = _library(repo, a)
    block, id_map = _library_overlay(nb, ids, lib, lib_ids)
    scope = {"mode": "include", "source_ids": ["src-vis"], "hidden_source_ids": [],
             "narrowed": False, "owner_id": a}
    with source_scope_context(nb, scope, {"mode": "include", "notebook_ids": []}):
        _c, kg_block, kg_id_map, _h, _p = _mix(repo, monkeypatch, nb, block, id_map)
    assert "LIB" not in kg_block
    assert {v["name"] for v in kg_id_map.values()} == {"Public root"}


# ------------------------------------- B-5 overlay through the real seam

SEAM_QUERY = "Mixture-of-Experts MoE"


def _seed_seam(repo, nb, *, b_memory: bool):
    """Written the way production writes it (``store_kg`` fills the reverse
    index): a paper with three concepts, ``Capacity factor`` WITHOUT evidence,
    and -- optionally -- B's Memory linking the same concept to a private one."""
    def concept(local, name, sid, evidence=True):
        return {"local_id": local, "object_type": "concept",
                "payload": {"name": name, "section_path": "1"},
                "evidence": [_ev(sid, f"{name} in {sid}")] if evidence else []}

    repo.store_kg(nb, "src-vis", [
        concept("A", "Mixture-of-Experts (MoE)", "src-vis"),
        concept("B", "Router balance", "src-vis"),
        concept("C", "Capacity factor", "src-vis", evidence=False),
    ], [_edge("A", "B", "src-vis"), _edge("A", "C", "src-vis")])
    if b_memory:
        repo.store_kg(nb, "src-mb", [
            concept("A", "Mixture-of-Experts (MoE)", "src-mb"),
            concept("Z", "ZEBRAQUARTZ plan", "src-mb"),
        ], [_edge("A", "Z", "src-mb")])
    with repo._connect() as db:
        assert db.execute(
            "SELECT count(*) AS c FROM knowledge_object_sources WHERE notebook_id=?", (nb,)
        ).fetchone()["c"] > 0  # the reverse index is real, not empty


def _seam(repo, nb, scope):
    with source_scope_context(nb, scope):
        _c, block, id_map, _hits, _p = repo.retrieval.mixed_chunk_candidates(
            nb, SEAM_QUERY, SEAM_QUERY, [SEAM_QUERY])
    return block, id_map


def _freeze(owner, hidden=()):
    return {"mode": "include", "source_ids": ["src-vis"], "hidden_source_ids": list(hidden),
            "narrowed": False, "owner_id": owner}


# The blocks master (effa7e9ac) renders for these two runs, captured by running
# the same scenario on an export of that commit.  The overlay leg there is
# ``_mix_retrieve`` alone; the comparison below also replays this branch with
# the backstop switched off, so a drift of the fixture shows up as a failure
# of that half rather than as a silent pass.
_MASTER_PLAIN = (
    "k1001: [concept][personal] Mixture-of-Experts (MoE)\n"
    "k1002: [concept][personal] Router balance\n"
    'k1003: [concept][personal] Capacity factor  — ev: "A kind of C"\n'
    "chain:\n"
    "  [k1001] Mixture-of-Experts (MoE) --kind_of--> [k1003] Capacity factor  (tier=personal)"
)
_MASTER_OWN = (
    "k1001: [concept][personal] Mixture-of-Experts (MoE)\n"
    "k1002: [concept][personal] Mixture-of-Experts (MoE)\n"
    "k1003: [concept][personal] ZEBRAQUARTZ plan\n"
    "k1004: [concept][personal] Router balance\n"
    'k1005: [concept][personal] Capacity factor  — ev: "A kind of C"\n'
    "chain:\n"
    "  [k1001] Mixture-of-Experts (MoE) --kind_of--> [k1005] Capacity factor  (tier=personal)"
)


def _assert_as_on_master(repo, monkeypatch, nb, scope, expected):
    block, id_map = _seam(repo, nb, scope)
    assert block == expected
    with monkeypatch.context() as patch:
        patch.setattr(RetrievalService, "_scoped_overlay",
                      lambda _self, _nb, kg_block, kg_id_map: (kg_block, kg_id_map))
        assert _seam(repo, nb, scope) == (block, id_map)


def test_mix_seam_is_byte_identical_without_anyone_elses_memory(repo, monkeypatch):
    """No Memory at all: the all-selected run's overlay is master's, byte for
    byte -- evidence-less ``Capacity factor`` and its chain line included --
    and the verdict does not bind."""
    repo.settings.graph_ppr_enabled = False
    nb, a = _shared(repo, b_memory=False)
    _seed_seam(repo, nb, b_memory=False)
    _assert_as_on_master(repo, monkeypatch, nb, _freeze(a), _MASTER_PLAIN)


def test_mix_seam_is_byte_identical_with_only_the_askers_own_memory(repo, monkeypatch):
    repo.settings.graph_ppr_enabled = False
    nb, _a = _shared(repo)
    b = _users(repo)["b00000002"]
    _seed_seam(repo, nb, b_memory=True)
    _assert_as_on_master(repo, monkeypatch, nb, _freeze(b, hidden=["src-mb"]), _MASTER_OWN)


def test_mix_seam_keeps_another_members_memory_out(repo):
    """Positive control on the same seam: A's all-selected run loses B's
    Memory node (and only that); the verdict binds, so evidence-less nodes go
    too -- the rule ``filter_retrieval_items`` applies to a KG hit."""
    repo.settings.graph_ppr_enabled = False
    nb, a = _shared(repo)
    _seed_seam(repo, nb, b_memory=True)
    unscoped = _seam(repo, nb, None)
    assert "ZEBRAQUARTZ" in unscoped[0]
    block, id_map = _seam(repo, nb, _freeze(a))
    assert "ZEBRAQUARTZ" not in block
    assert "Router balance" in block and "Mixture-of-Experts (MoE)" in block
    assert {v["name"] for v in id_map.values()} <= {
        v["name"] for v in unscoped[1].values()} - {"ZEBRAQUARTZ plan"}


def test_mix_overlay_drift_after_the_verdict_is_judged_bound(repo, monkeypatch):
    """The verdict said "does not bind", but a rendered node names a source
    outside the freeze (a source added after it): the drift is recorded and
    the library is judged as bound -- the late node goes, and so does an
    evidence-less one."""
    nb, a = _shared(repo, b_memory=False)
    with repo._write() as db:
        _source(db, nb, "src-late")
    repo.store_kg(nb, "src-vis", [
        _concept("P", "Public root", "src-vis"),
        {"local_id": "E", "object_type": "concept",
         "payload": {"name": "Bare node", "section_path": "1"}, "evidence": []},
    ], [])
    repo.store_kg(nb, "src-late", [_concept("L", "LATE NODE", "src-late")], [])
    ids = _ids_by_name(repo, nb)
    block, id_map = render_subgraph_context([
        (_node(ids, nb, "Public root"), None, None),
        (_node(ids, nb, "Bare node"), None, None),
        (_node(ids, nb, "LATE NODE"), _edge_to("LATE QUOTE"), ids["Public root"]),
    ], id_offset=1000, active_notebook_id=nb)
    monkeypatch.setattr(repo.retrieval.candidates, "_mix_retrieve",
                        lambda *_a: ([], block, dict(id_map), [], 0))
    service = _service(repo, lambda _nb: False)
    with source_scope_context(nb, _freeze(a)):
        _c, kg_block, kg_id_map, _h, _p = service.mixed_chunk_candidates(
            nb, "q", "", ["q"])
        memo = dict(current_source_scope()._ceiling_binds_memo)
    assert "LATE" not in kg_block and "Bare node" not in kg_block
    assert {v["name"] for v in kg_id_map.values()} == {"Public root"}
    assert memo == {nb: True}


def test_mix_overlay_unbound_keeps_an_evidence_less_node(repo, monkeypatch):
    """The same overlay without the late node: nothing outside the freeze,
    the verdict does not bind, the evidence-less node stays (vacuously
    within the ceiling, as in ``node_context_row_within_ceiling``)."""
    nb, a = _shared(repo, b_memory=False)
    repo.store_kg(nb, "src-vis", [
        _concept("P", "Public root", "src-vis"),
        {"local_id": "E", "object_type": "concept",
         "payload": {"name": "Bare node", "section_path": "1"}, "evidence": []},
    ], [])
    ids = _ids_by_name(repo, nb)
    block, id_map = render_subgraph_context([
        (_node(ids, nb, "Public root"), None, None),
        (_node(ids, nb, "Bare node"), _edge_to("BARE QUOTE"), ids["Public root"]),
    ], id_offset=1000, active_notebook_id=nb)
    monkeypatch.setattr(repo.retrieval.candidates, "_mix_retrieve",
                        lambda *_a: ([], block, dict(id_map), [], 0))
    with source_scope_context(nb, _freeze(a)):
        _c, kg_block, kg_id_map, _h, _p = repo.retrieval.mixed_chunk_candidates(
            nb, "q", "", ["q"])
        memo = dict(current_source_scope()._ceiling_binds_memo)
    assert (kg_block, kg_id_map) == (block, id_map)
    assert memo == {nb: False}


def test_merge_keeps_relation_endpoints(repo):
    """The overlay's node quote is its incoming edge's evidence, not checked
    against the ceiling (``RetrievalService._scoped_overlay``): sound only
    while no writer re-points a relation's endpoints across sources.  A manual
    merge of an object into one from another source moves evidence and
    deprecates the merged object; its relations keep their endpoints."""
    from app.models.schemas import MergeRequest

    nb, _a = _shared(repo)
    with repo._write() as db:
        _source(db, nb, "src-vis2")
    repo.store_kg(nb, "src-vis2", [_concept("A", "OtherA", "src-vis2")], [])
    repo.store_kg(nb, "src-vis", [
        _concept("M", "MergedM", "src-vis"), _concept("N", "NeighbourN", "src-vis"),
    ], [_edge("N", "M", "src-vis")])
    ids = _ids_by_name(repo, nb)
    repo.merge_knowledge(nb, ids["MergedM"], MergeRequest(into_id=ids["OtherA"]))
    with repo._connect() as db:
        rows = db.execute(
            "SELECT source_object_id, target_object_id, source_id FROM knowledge_relations "
            "WHERE notebook_id=?", (nb,)).fetchall()
    assert [(r["source_object_id"], r["target_object_id"], r["source_id"]) for r in rows] \
        == [(ids["NeighbourN"], ids["MergedM"], "src-vis")]

