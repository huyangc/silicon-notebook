"""Read side of the navigation-page marker (``chunks.navigation``): tables of
contents and lists of figures / tables are not retrieval evidence.

* every chunk-row builder carries the flag (adapter cases shared with the
  PostgreSQL twin ``tests/postgres/test_navigation_filter_pg.py``, plus two
  static scans that keep the enumerated builder list complete);
* every producer that bypasses ``score_chunks`` drops flagged chunks: PPR,
  KG-source chunks, the exact-identifier channel, the selected-source graph
  lane, the by-id admission hydrate, and the element arm;
* ``RETRIEVAL_NAVIGATION_FILTER_ENABLED=false`` restores each of them.

``score_chunks`` itself, ``_retrieve_chunks`` and the keyword arm are pinned in
``tests/test_chunk_retrieval.py``; which construction sites must choose is
pinned in ``tests/test_retrieved_hit_origin_guard.py``.
"""
from __future__ import annotations

import ast
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievalSupport, RetrievedChunk
from app.services.embedding import FakeEmbedder
from app.services.exact_lookup import ExactLookupDeps, ExactLookupLimits, exact_lookup_chunks
from app.services.navigation_filter import (
    drop_navigation_chunks,
    drop_navigation_rows,
    row_navigation,
)
from app.services.sqlite_repository import SQLiteRepository
from tests import navigation_filter_cases as cases
from tests.model_testkit import bind_all_embedding_clients

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'nav.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


# ------------------------------------------------------------------ helpers
def test_settings_default_drops_navigation_pages(monkeypatch):
    assert Settings(_env_file=None).retrieval_navigation_filter_enabled is True
    monkeypatch.setenv("RETRIEVAL_NAVIGATION_FILTER_ENABLED", "false")
    assert Settings(_env_file=None).retrieval_navigation_filter_enabled is False


def test_helpers_keep_order_and_read_a_missing_flag_as_false():
    import sqlite3

    rows = [{"id": "a", "navigation": 1}, {"id": "b"}, {"id": "c", "navigation": 0}]
    assert [r["id"] for r in drop_navigation_rows(rows, enabled=True)] == ["b", "c"]
    assert drop_navigation_rows(rows, enabled=False) == rows
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    assert row_navigation(db.execute("SELECT 1 AS id").fetchone()) is False
    assert row_navigation(db.execute("SELECT 1 AS navigation").fetchone()) is True
    chunks = [SimpleNamespace(id="a", navigation=True), SimpleNamespace(id="b")]
    assert [c.id for c in drop_navigation_chunks(chunks, enabled=True)] == ["b"]
    assert drop_navigation_chunks(chunks, enabled=False) == chunks


# ------------------------------------------------- builders carry the flag
def test_chunk_row_builders_carry_navigation(repo):
    cases.case_chunk_row_builders_carry_navigation(repo)


def test_element_rows_carry_navigation(repo):
    cases.case_element_rows_carry_navigation(repo)


def test_navigation_version_moves_with_the_flagged_set(repo):
    cases.case_navigation_version_moves_with_the_flagged_set(repo)


def test_service_builders_carry_navigation(repo, monkeypatch):
    cases.case_service_builders_carry_navigation(repo, monkeypatch)


def _function_sources(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = stack + [child.name]
                if not isinstance(child, ast.ClassDef):
                    yield ".".join(qualname), child
                yield from walk(child, qualname)
            else:
                yield from walk(child, stack)

    yield from walk(tree, [])


def _literal_text(node) -> str:
    parts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
             and isinstance(n.value, str)]
    return " ".join(parts)


#: Repository functions that read chunk rows with their section coordinates
#: but feed no retrieval producer.
_SQL_READERS_EXEMPT = {
    # the generated-question index build (write side; its hits are hydrated
    # through ``retrieval_contribution_rows``)
    "ChunkStore.question_index_chunk_page",
    # knowhow projection's own prior-cell lookup
    "ChunkStore.rows_by_id_prefix",
    # the navigation backfill's own read, and the build / publish writes
    "ChunkStore.navigation_scan_rows",
    "ChunkStore.replace_source_chunks",
    "KgBuildJobStore.publish_indexing_pipeline_success",
    "SqliteMigrator._migration_1",
}
#: The functions behind ``navigation_filter_cases.CHUNK_ROW_BUILDERS`` and the
#: other cases (PostgreSQL's ``chunks_by_section`` / exact probe SQL lives in
#: ``postgres/search.py``; the source-subgraph projection is pinned by
#: ``test_source_subgraph_snapshot_*`` below).
_SQL_READERS_COVERED = {
    "ChunkStore.retrieval_rows", "ChunkStore.hydrate_rows",
    "ChunkStore.graph_hydrate_rows", "ChunkStore.retrieval_contribution_rows",
    "ChunkStore.rows_by_ids", "ChunkStore.chunks_by_section",
    "KnowledgeStore.chunk_exact_search", "chunk_exact_candidate_rows",
    "chunk_section_rows", "source_subgraph_rows_on",
}


def test_every_chunk_row_reader_is_enumerated_and_selects_navigation():
    """A repository function whose SQL reads chunk rows with their section
    coordinates is either an enumerated retrieval builder -- and then its SQL
    names ``navigation`` -- or exempt with a reason above."""
    found = {}
    for path in sorted((APP / "repositories").rglob("*.py")):
        for qualname, node in _function_sources(path):
            text = _literal_text(node)
            if (re.search(r"\bSELECT\b", text) and "section_path" in text
                    and re.search(r"\b(FROM|JOIN) chunks\b", text)):
                found.setdefault(qualname, []).append(
                    (path.name, re.search(r"\bnavigation\b", text) is not None))
    unknown = sorted(set(found) - _SQL_READERS_EXEMPT - _SQL_READERS_COVERED)
    assert unknown == [], f"chunk-row readers neither enumerated nor exempt: {unknown}"
    missing = sorted(
        f"{name}::{qualname}" for qualname in _SQL_READERS_COVERED
        for name, has in found.get(qualname, []) if not has
    )
    assert missing == [], f"enumerated builders whose SQL does not select navigation: {missing}"
    assert _SQL_READERS_COVERED <= set(found)


#: The modules that read chunk rows for retrieval: every repository adapter,
#: and the services that turn rows into candidates (writers such as the build
#: or the sharing copy are out of scope: they write the flag, not read it).
_RETRIEVAL_SERVICE_PATTERNS = (
    "retrieval*.py", "graph_retrieval.py", "reasoning_retrieval.py", "exact_lookup.py",
    "chunk_federation.py", "source_graph_*.py", "source_subgraph*.py",
    "navigation_filter.py",
)


def _retrieval_modules() -> list:
    modules = set((APP / "repositories").rglob("*.py"))
    for pattern in _RETRIEVAL_SERVICE_PATTERNS:
        modules.update((APP / "services").glob(pattern))
    assert APP / "services" / "retrieval_candidates.py" in modules
    return sorted(modules)


def test_every_chunk_shaped_dict_literal_carries_navigation():
    """The dicts ``score_chunks`` / the exact channel read are rebuilt by key,
    so a column the SQL returns is lost unless the literal names it."""
    exempt = {"CatalogStore._candidate_row"}  # a catalog row, not a chunk
    offenders = []
    for path in _retrieval_modules():
        for qualname, node in _function_sources(path):
            if qualname in exempt:
                continue
            for item in ast.walk(node):
                if not isinstance(item, ast.Dict):
                    continue
                keys = {k.value for k in item.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if ({"source_id", "section_path"} <= keys and keys & {"chunk_id", "id"}
                        and "navigation" not in keys):
                    offenders.append(f"{path.relative_to(APP)}::{qualname}")
    assert offenders == []


# --------------------------------------------------- bypass producers drop
def _exact_deps(hits, sections, rows):
    @contextmanager
    def connect():
        yield None

    return ExactLookupDeps(
        connect=connect,
        exact_search=lambda _db, _nb, _term, _k: hits,
        section_rows=lambda _db, _nb, _source, path, _limit: sections.get(path, []),
        chunk_rows=lambda _db, ids: [rows[i] for i in ids if i in rows],
    )


def _row(chunk_id, section_path, text, *, navigation=False):
    return {"id": chunk_id, "source_id": "src", "source_title": "Manual",
            "section_path": section_path, "text": text, "element_ids": "[]",
            "navigation": navigation}


def test_exact_lookup_navigation_hit_takes_no_section_slot():
    """With one slot, a table of contents naming ``set_db`` (shorter
    breadcrumb, so it would win the tie) must not take it from ``set_db``."""
    toc = _row("toc", "Manual > Contents", "set_db .... 12 report_timing .... 14",
               navigation=True)
    main = _row("main", "Manual > Commands > set_db", "[set_db] set_db sets a property.")
    index = _row("index", "Manual > Commands > set_db > Index",
                 "set_db options .... 13", navigation=True)
    hits = [
        {"chunk_id": "toc", "source_id": "src", "section_path": toc["section_path"],
         "navigation": True},
        {"chunk_id": "main", "source_id": "src", "section_path": main["section_path"],
         "navigation": False},
    ]
    deps = _exact_deps(hits, {main["section_path"]: [main, index]},
                       {"toc": toc, "main": main})
    limits = ExactLookupLimits(max_sections=1, drop_navigation=True)
    assert [c.chunk_id for c in exact_lookup_chunks(deps, "nb", "set_db 怎么用", limits)] == [
        "main"]
    off = ExactLookupLimits(max_sections=1, drop_navigation=False)
    assert [c.chunk_id for c in exact_lookup_chunks(deps, "nb", "set_db 怎么用", off)] == [
        "toc"]


def test_exact_lookup_through_the_store_drops_navigation(repo, monkeypatch):
    seeded = cases.seed(repo)
    candidates = repo.retrieval.candidates
    query = "ZXQV_9000 怎么配置"
    with repo._runtime.database.write() as db:
        db.execute("UPDATE chunks SET text=text || ' ZXQV_9000' WHERE source_id=?",
                   (seeded.source_id,))
        db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (?,?)",
                   (seeded.toc_id, seeded.body_id))
        for row in db.execute("SELECT id, notebook_id, text FROM chunks WHERE source_id=?",
                              (seeded.source_id,)).fetchall():
            db.execute("INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                       (row["id"], row["notebook_id"], row["text"]))
    found = {c.chunk_id for c in candidates._exact_lookup_chunks(seeded.notebook_id, query)}
    assert found == {seeded.body_id}
    monkeypatch.setattr(candidates.settings, "retrieval_navigation_filter_enabled", False)
    found = {c.chunk_id for c in candidates._exact_lookup_chunks(seeded.notebook_id, query)}
    assert found == {seeded.toc_id, seeded.body_id}


def _seed_ppr(repo):
    from tests.test_ppr_retrieve import _seed_two_doc_moe

    notebook = _seed_two_doc_moe(repo)
    with repo._write() as db:
        db.execute("UPDATE chunks SET navigation=1 WHERE id='cA'")
    return notebook


def test_ppr_output_drops_navigation_chunks(repo, monkeypatch):
    notebook = _seed_ppr(repo)
    graph = repo.retrieval.graph
    monkeypatch.setattr(graph, "scale_ppr", lambda *_a, **_k: [("cA", 0.9), ("cB", 0.5)])
    assert [c.chunk_id for c in graph._ppr_retrieve(notebook.id, "MoE")] == ["cB"]
    monkeypatch.setattr(graph.settings, "retrieval_navigation_filter_enabled", False)
    assert [c.chunk_id for c in graph._ppr_retrieve(notebook.id, "MoE")] == ["cA", "cB"]


def test_ppr_ceiling_walk_output_drops_navigation_chunks(repo, monkeypatch):
    from app.services.source_scope import source_scope_context

    notebook = _seed_ppr(repo)
    graph = repo.retrieval.graph
    monkeypatch.setattr(graph, "scale_ppr", lambda *_a, **_k: [("cA", 0.9), ("cB", 0.5)])
    scope = {"mode": "include", "source_ids": ["src-A", "src-B"], "narrowed": True}
    from app.services.graph_retrieval import _PprCeiling

    with source_scope_context(notebook.id, scope):
        assert _PprCeiling.for_run(notebook.id) is not None  # the walk path runs
        assert [c.chunk_id for c in graph._ppr_retrieve(notebook.id, "MoE")] == ["cB"]
        # One slot: the navigation page ranked first is refused like an
        # out-of-ceiling candidate and spends none, so the passage still fills it.
        monkeypatch.setattr(graph.settings, "ppr_top_chunks", 1)
        assert [c.chunk_id for c in graph._ppr_retrieve(notebook.id, "MoE")] == ["cB"]
        monkeypatch.setattr(graph.settings, "retrieval_navigation_filter_enabled", False)
        assert [c.chunk_id for c in graph._ppr_retrieve(notebook.id, "MoE")] == ["cA"]


def test_kg_source_chunks_drop_navigation_chunks(repo, monkeypatch):
    notebook = _seed_ppr(repo)
    graph = repo.retrieval.graph
    assert [c.chunk_id for c in graph._kg_source_chunks(notebook.id, ["e1", "e2"])] == ["cB"]
    monkeypatch.setattr(graph.settings, "retrieval_navigation_filter_enabled", False)
    assert [c.chunk_id for c in graph._kg_source_chunks(notebook.id, ["e1", "e2"])] == [
        "cA", "cB"]


def test_element_arm_drops_elements_of_navigation_chunks(repo, monkeypatch):
    """Both element paths: the whole-notebook fallback and the bounded path
    that hydrates the elements of ranked chunks -- including an element the
    surviving body chunk shares with the navigation chunk."""
    seeded = cases.seed(repo)
    candidates = repo.retrieval.candidates
    with candidates._connect() as db:
        whole = {e["element_id"] for e in candidates._gather_elements(db, seeded.notebook_id)}
    assert whole == {seeded.element("body")}

    body = RetrievedChunk(
        chunk_id=seeded.body_id, source_id=seeded.source_id, source_title="spec",
        section_path="5.7.3 Standard Package", text=cases.BODY_TEXT,
        element_ids=[seeded.element("body"), seeded.element("shared")], relevance=0.9,
    )
    found = candidates._retrieve_elements_from_chunks(
        "Standard Package loss", [body], limit=8, notebook_id=seeded.notebook_id)
    assert {e.element_id for e in found} == {seeded.element("body")}

    # Fail-closed without the reverse index (a notebook not backfilled yet):
    # both paths read the flagged chunks' own element_ids.
    with repo._write() as db:
        db.execute("DELETE FROM chunk_elements WHERE notebook_id=?", (seeded.notebook_id,))
    with candidates._connect() as db:
        whole = {e["element_id"] for e in candidates._gather_elements(db, seeded.notebook_id)}
    assert whole == {seeded.element("body")}
    found = candidates._retrieve_elements_from_chunks(
        "Standard Package loss", [body], limit=8, notebook_id=seeded.notebook_id)
    assert {e.element_id for e in found} == {seeded.element("body")}

    # Switch off: everything back, and neither path reads the flags at all.
    monkeypatch.setattr(candidates.settings, "retrieval_navigation_filter_enabled", False)
    monkeypatch.setattr(candidates, "_navigation_element_ids", lambda *_a: pytest.fail(
        "the navigation lookup ran with the switch off"))
    with candidates._connect() as db:
        rows = candidates.sources.retrieval_element_rows(
            db, seeded.notebook_id, with_navigation=False)
        assert all("navigation" not in row.keys() for row in rows)
        whole = {e["element_id"] for e in candidates._gather_elements(db, seeded.notebook_id)}
    assert whole == {seeded.element(name) for name in ("toc", "shared", "body")}
    found = candidates._retrieve_elements_from_chunks(
        "Standard Package loss", [body], limit=8, notebook_id=seeded.notebook_id)
    assert {e.element_id for e in found} == {seeded.element("body"), seeded.element("shared")}


@pytest.mark.parametrize("enabled", [True, False], ids=["switch-on", "switch-off"])
def test_fts_degraded_path_drops_navigation_chunks(repo, monkeypatch, enabled):
    """A notebook over ``chunk_bruteforce_max_chunks`` scores FTS hits only
    (``_retrieve_chunks_fts_degraded``); the flagged hit is dropped there too."""
    seeded = cases.seed(repo)
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates.settings, "chunk_bruteforce_max_chunks", 1)
    monkeypatch.setattr(candidates.settings, "retrieval_navigation_filter_enabled", enabled)
    events = []
    monkeypatch.setattr(candidates.event_log, "emit", events.append)
    scored, _ids, _matrix = candidates._retrieve_chunks(
        seeded.notebook_id, "Standard Package Loss")
    assert any(event.get("kind") == "chunk_bruteforce_skipped" for event in events)
    found = {chunk.chunk_id for chunk in scored}
    assert seeded.body_id in found
    assert (seeded.toc_id in found) is (not enabled)


# ------------------------------------------- the selected-source graph lane
def _flip_navigation(repo, chunk_id, source_id, flag):
    """What every flag writer does in one transaction: the chunk's flag and
    the source's ``navigation_version`` (``app.repositories.navigation_version``;
    the ``_seed`` sources carry no ``chunked_at``, so the store's
    compare-and-set write does not apply to them)."""
    with repo._write() as db:
        db.execute("UPDATE chunks SET navigation=? WHERE id=?", (int(flag), chunk_id))
        db.execute(
            "UPDATE sources SET navigation_version=navigation_version+1 WHERE id=?",
            (source_id,))


def test_source_subgraph_snapshot_carries_the_flag_and_rekeys_on_a_flag_change(repo):
    """The backfill writes ``chunks.navigation`` and nothing the generation
    signature reads; the snapshot cache re-keys on ``navigation_version``,
    which every flag writer bumps in the same transaction."""
    from app.services.source_subgraph import SourceSubgraphService
    from tests.test_source_subgraph import _seed

    notebook_id, source_a, _source_b = _seed(repo)
    service = SourceSubgraphService(
        projections=repo._runtime.index_projections, settings=repo.settings)
    first = service.snapshot(notebook_id, [source_a])
    assert [c.navigation for c in first.chunks] == [False]
    assert service.snapshot(notebook_id, [source_a]) is first
    _flip_navigation(repo, f"chunk-{source_a}", source_a, True)
    second = service.snapshot(notebook_id, [source_a])
    assert second is not first
    assert [c.navigation for c in second.chunks] == [True]


def test_online_ppr_graph_cache_cannot_resurrect_or_hide_a_flag(repo):
    """The online PPR graph is cached on the generation key alone, so its chunk
    objects keep the flag they were built with. The lane judges a hit by the
    CURRENT snapshot's flagged ids instead: one real PPR service, a flag set
    after the graph was cached is honoured, and a flag cleared afterwards
    (``--clear``) no longer drops the chunk."""
    from app.services.retrieval_enrichment import BaselineProtectedEnrichmentService
    from app.services.source_graph_activation import SelectedSourceGraphActivationService
    from app.services.source_graph_rollout import SourceGraphRolloutDecision
    from app.services.source_scope import source_scope_context
    from app.services.source_subgraph import SourceSubgraphService
    from app.services.source_subgraph_ppr import SourceSubgraphPprService
    from tests.test_source_subgraph import _seed

    notebook_id, source_a, _source_b = _seed(repo)
    graph_chunk = f"chunk-{source_a}"
    subgraphs = SourceSubgraphService(
        projections=repo._runtime.index_projections, settings=repo.settings)
    ppr = SourceSubgraphPprService(settings=SimpleNamespace(
        source_subgraph_ppr_enabled=True, ppr_damping=0.5, ppr_tol=1e-6,
        ppr_passage_node_weight=0.05))
    service = SelectedSourceGraphActivationService(
        settings=SimpleNamespace(
            selected_source_graph_attestation_path="",
            selected_source_graph_expected_model_json="",
            selected_source_graph_notebook_allowlist="",
            selected_source_graph_rollout_mode="off",
            selected_source_graph_rollout_percent=0,
            selected_source_graph_expected_corpus_signature="",
            selected_source_graph_enrichment_tokens=1000,
            retrieval_navigation_filter_enabled=True,
        ),
        snapshots=subgraphs,
        primitives=SimpleNamespace(expand_graph=lambda *_a, **_k: SimpleNamespace(
            capability=SimpleNamespace(enabled=True, reason=""), nodes=())),
        online_ppr=ppr,
        partitioned_ppr=SimpleNamespace(),
        enrichment=BaselineProtectedEnrichmentService(),
        event_log=SimpleNamespace(emit=lambda _row: None),
    )
    service._decision = lambda _nb: SourceGraphRolloutDecision(True, False, "quality_approved")

    def lane_chunks():
        with source_scope_context(notebook_id, {
            "mode": "include", "source_ids": [source_a], "narrowed": True,
        }):
            result = service.run(notebook_id, _baseline(), object_seeds={"ko-a1": 1.0},
                                 source_titles=lambda _ids: {source_a: "A"})
        return [chunk.chunk_id for chunk in result.chunks], result.status

    ids, status = lane_chunks()
    assert graph_chunk in ids and status.cache_hit is False
    _flip_navigation(repo, graph_chunk, source_a, True)
    ids, status = lane_chunks()
    assert graph_chunk not in ids
    assert status.cache_hit is True  # the cached graph still holds the unflagged chunk
    _flip_navigation(repo, graph_chunk, source_a, False)
    ids, status = lane_chunks()
    assert graph_chunk in ids and status.cache_hit is True


def _graph_chunk(chunk_id, *, navigation):
    return SimpleNamespace(chunk_id=chunk_id, source_id="a", section_path="G",
                           text=f"text-{chunk_id}", element_ids=(f"e-{chunk_id}",),
                           navigation=navigation)


def _activation(snapshot, ppr, *, enabled=True, partitioned=None):
    from app.services.retrieval_enrichment import BaselineProtectedEnrichmentService
    from app.services.source_graph_activation import SelectedSourceGraphActivationService
    from app.services.source_graph_rollout import SourceGraphRolloutDecision

    settings = SimpleNamespace(
        selected_source_graph_attestation_path="",
        selected_source_graph_expected_model_json="",
        selected_source_graph_notebook_allowlist="",
        selected_source_graph_rollout_mode="off",
        selected_source_graph_rollout_percent=0,
        selected_source_graph_expected_corpus_signature="",
        selected_source_graph_enrichment_tokens=1000,
        retrieval_navigation_filter_enabled=enabled,
    )
    service = SelectedSourceGraphActivationService(
        settings=settings,
        snapshots=SimpleNamespace(snapshot=lambda _nb, _sources: snapshot),
        primitives=SimpleNamespace(expand_graph=lambda *_a, **_k: SimpleNamespace(
            capability=SimpleNamespace(enabled=True, reason=""),
            nodes=(SimpleNamespace(object_id="o1"),))),
        online_ppr=SimpleNamespace(retrieve=lambda *_a, **_k: ppr),
        partitioned_ppr=partitioned or SimpleNamespace(),
        enrichment=BaselineProtectedEnrichmentService(),
        event_log=SimpleNamespace(emit=lambda _row: None),
    )
    service._decision = lambda _nb: SourceGraphRolloutDecision(True, False, "quality_approved")
    return service


def _snapshot(*chunks, memberships=()):
    return SimpleNamespace(allowed_source_ids=("a",), scope_hash="scope", nodes=(),
                           relations=(), chunks=chunks, memberships=memberships,
                           degraded_reasons=())


def _baseline():
    return [RetrievedChunk(chunk_id="b", source_id="a", source_title="a", section_path="S",
                           text="text-b", element_ids=["e-b"], relevance=0.8)]


@pytest.mark.parametrize("enabled", [True, False], ids=["switch-on", "switch-off"])
def test_selected_graph_ppr_and_neighbours_drop_navigation_chunks(enabled):
    from app.services.source_scope import source_scope_context

    toc, page, member = (_graph_chunk("toc", navigation=True),
                         _graph_chunk("g", navigation=False),
                         _graph_chunk("toc2", navigation=True))
    support = RetrievalSupport("ppr", "ppr", "", 0.9)
    ppr = SimpleNamespace(
        hits=(SimpleNamespace(chunk=toc, score=0.95, support=support),
              SimpleNamespace(chunk=page, score=0.9, support=support)),
        cache_hit=False, capability=SimpleNamespace(enabled=True, reason=""),
    )
    snapshot = _snapshot(toc, page, member, memberships=(("o1", "toc2"),))
    service = _activation(snapshot, ppr, enabled=enabled)
    with source_scope_context("nb", {"mode": "include", "source_ids": ["a"],
                                     "narrowed": True}):
        result = service.run("nb", _baseline(), object_seeds={"o1": 1.0},
                             source_titles=lambda _ids: {"a": "A"})
    ids = [chunk.chunk_id for chunk in result.chunks]
    assert ids == (["b", "g"] if enabled else ["b", "toc", "g", "toc2"])


@pytest.mark.parametrize("enabled", [True, False], ids=["switch-on", "switch-off"])
def test_selected_graph_partition_hits_drop_navigation_after_the_hydration_check(enabled):
    """The partition path checks that every hit came back; a navigation page
    must not fail that check (the whole lane would close) -- it is hydrated
    with its flag and dropped afterwards."""
    from app.services.source_graph_activation import hydrate_selected_graph_chunk_rows
    from app.services.source_scope import source_scope_context

    ppr = SimpleNamespace(hits=(), cache_hit=False,
                          capability=SimpleNamespace(enabled=False, reason="off"))
    support = RetrievalSupport("ppr", "ppr", "", 0.9)
    partitioned = SimpleNamespace(retrieve=lambda *_a, **_k: SimpleNamespace(
        capability=SimpleNamespace(enabled=True, reason=""), cache_hit=False,
        hits=tuple(SimpleNamespace(chunk_id=cid, source_id="a", score=0.9, support=support)
                   for cid in ("toc", "g")),
    ))
    rows = [
        {"chunk_id": "toc", "source_id": "a", "text": "toc", "section_path": "C",
         "element_ids": ["e-toc"], "navigation": True},
        {"chunk_id": "g", "source_id": "a", "text": "g", "section_path": "G",
         "element_ids": ["e-g"], "navigation": False},
    ]
    service = _activation(_snapshot(), ppr, enabled=enabled, partitioned=partitioned)
    with source_scope_context("nb", {"mode": "include", "source_ids": ["a"],
                                     "narrowed": True}):
        result = service.run(
            "nb", _baseline(), parent_version="v",
            hydrate_chunk_ids=lambda _ids: hydrate_selected_graph_chunk_rows(rows))
    assert result.status.state == "active"
    assert [c.chunk_id for c in result.chunks] == (["b", "g"] if enabled else ["b", "toc", "g"])
