"""PPR slots go only to passages inside the run's source ceilings (audit B-2).

The PPR graph is cached per participant set, so its ranking spans every
participant's chunks: a mounted library's Knowhow rows and the sources
uploaded to it after the run froze its ceiling included.  These tests pin that
the ceiling is applied BEFORE the ``ppr_top_chunks`` cut (the output reaches
``top_chunks`` when enough in-ceiling candidates exist), that the store's
``graph_hydrate_rows(allowed_source_ids=...)`` is what filters a listed
library, and that an all-selected run whose sources did not change issues the
historical hydration statement.  The PostgreSQL twin and its plan pins are in
``tests/postgres/test_ppr_hydration_ceiling_pins.py``.

The ranking is injected (``scale_ppr`` replaced by a fixed list that honours
``max_results`` like the real one) so which chunks outrank which is a fixture
fact, not a property of the graph build.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.chunk_store import ChunkStore
from app.services.embedding import FakeEmbedder
from app.services.source_scope import (
    ceiling_binds,
    current_source_scope,
    source_scope_context,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients

NOW = "2026-09-30T00:00:00"
TOP = 3


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    monkeypatch.setattr(r.retrieval.graph.settings, "ppr_top_chunks", TOP)
    return r


def _source(db, notebook_id, source_id, source_type="md"):
    db.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (source_id, notebook_id, source_id, source_type, "ready", NOW, NOW),
    )


def _chunk(db, notebook_id, source_id, chunk_id):
    db.execute(
        "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (chunk_id, notebook_id, source_id, f"text of {chunk_id}", "S",
         json.dumps([f"el-{chunk_id}"]), NOW),
    )


def _seed(repo, *, knowhow_chunks=2):
    """Active library A (a1, a2) with mounted library B.

    B's ceiling (frozen when the run started) is b1, b2.  B also holds a
    Knowhow projection (hidden, never in a mounted ceiling) with
    ``knowhow_chunks`` chunks and a source uploaded after the freeze."""
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="active"))
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    with repo._write() as db:
        for sid in ("a1", "a2"):
            _source(db, active.id, sid)
            _chunk(db, active.id, sid, f"c-{sid}")
        for sid in ("b1", "b2"):
            _source(db, base.id, sid)
            _chunk(db, base.id, sid, f"c-{sid}")
        _source(db, base.id, "b-knowhow", "knowhow")
        for index in range(knowhow_chunks):
            _chunk(db, base.id, "b-knowhow", f"c-kh-{index:03d}")
        _source(db, base.id, "b-late")
        _chunk(db, base.id, "b-late", "c-b-late")
    return active.id, base.id


def _mounted_scope(active_id, base_id):
    return source_scope_context(
        active_id,
        {"mode": "include", "source_ids": ["a1", "a2"], "narrowed": False,
         "owner_id": "user-local"},
        None,
        {base_id: ["b1", "b2"]},
    )


class _Ranking:
    """Stands in for ``scale_ppr``: a fixed descending ranking, cut at
    ``max_results`` like the real bounded heap."""

    def __init__(self, chunk_ids):
        n = len(chunk_ids)
        self.ranking = [(cid, (n - i) / n) for i, cid in enumerate(chunk_ids)]
        self.calls: list = []

    def __call__(self, notebook_id, question, max_results=None):
        self.calls.append(max_results)
        return list(self.ranking if max_results is None else self.ranking[:max_results])


class _HydrationSpy:
    def __init__(self, store):
        self.store = store
        self.calls: list = []

    def __call__(self, db, chunk_ids, **kwargs):
        self.calls.append((list(chunk_ids), kwargs))
        return self.store.graph_hydrate_rows(db, chunk_ids, **kwargs)


def _install(repo, monkeypatch, ranking):
    graph = repo.retrieval.graph
    fake = _Ranking(ranking)
    monkeypatch.setattr(graph, "scale_ppr", fake)
    spy = _HydrationSpy(ChunkStore)
    monkeypatch.setattr(graph.chunks, "graph_hydrate_rows", spy)
    return fake, spy


def test_mounted_knowhow_and_late_uploads_never_take_a_slot(repo, monkeypatch):
    """The acceptance case: the three best-ranked candidates are B's Knowhow
    rows and B's post-freeze upload; the output still fills all three slots,
    only with in-ceiling passages, in rank order.

    Catches: the ceiling moved back after the cut (the three slots go to the
    refused candidates, the boundary filter empties them: 0 results), and
    the store ignoring ``allowed_source_ids`` (the refused rows come back
    for the listed library and take the slots the same way)."""
    active, base = _seed(repo)
    ranking = ["c-kh-000", "c-kh-001", "c-b-late", "c-b1", "c-a1", "c-b2", "c-a2"]
    _install(repo, monkeypatch, ranking)
    with _mounted_scope(active, base):
        out = repo._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-b1", "c-a1", "c-b2"]
    assert len(out) == TOP
    assert {c.notebook_id for c in out} == {active, base}


def test_the_mounted_ceiling_is_pushed_into_hydration(repo, monkeypatch):
    """The listed library's ceiling reaches the store; nothing else is listed
    for an all-selected, unchanged active library."""
    active, base = _seed(repo)
    ranking = ["c-kh-000", "c-b-late", "c-b1", "c-a1", "c-b2"]
    _, spy = _install(repo, monkeypatch, ranking)
    with _mounted_scope(active, base):
        repo._ppr_retrieve(active, "q")
    assert spy.calls, "hydration never ran"
    for _ids, kwargs in spy.calls:
        assert set(kwargs["allowed_source_ids"]) == {base}
        assert set(kwargs["allowed_source_ids"][base]) == {"b1", "b2"}


def test_window_exhaustion_re_ranks_everything_and_still_fills(repo, monkeypatch):
    """More refused candidates than the over-ranked prefix holds: the scale
    ranking is fetched in full once and the slots are still filled."""
    active, base = _seed(repo, knowhow_chunks=60)
    ranking = [f"c-kh-{i:03d}" for i in range(60)] + ["c-b1", "c-a1", "c-b2", "c-a2"]
    fake, _ = _install(repo, monkeypatch, ranking)
    with _mounted_scope(active, base):
        out = repo._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-b1", "c-a1", "c-b2"]
    assert fake.calls == [TOP * 16, None]


def test_an_excluded_library_takes_no_slot(repo, monkeypatch):
    active, base = _seed(repo)
    ranking = ["c-b1", "c-b2", "c-a1", "c-a2"]
    _install(repo, monkeypatch, ranking)
    with source_scope_context(active, None, {"mode": "include", "notebook_ids": []}):
        out = repo._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-a1", "c-a2"]


def test_all_selected_unchanged_run_issues_the_historical_hydration(repo, monkeypatch):
    """No mounted library, every source selected and nothing uploaded since:
    one hydration of the first ``top_chunks`` ids without a ceiling, and the
    same output as the run without any scope."""
    notebook = repo.create_notebook(NotebookCreate(name="solo"))
    with repo._write() as db:
        for sid in ("s1", "s2", "s3", "s4"):
            _source(db, notebook.id, sid)
            _chunk(db, notebook.id, sid, f"c-{sid}")
    ranking = ["c-s3", "c-s1", "c-s4", "c-s2"]
    _, spy = _install(repo, monkeypatch, ranking)
    unscoped = repo._ppr_retrieve(notebook.id, "q")
    unscoped_calls = list(spy.calls)
    spy.calls.clear()
    with source_scope_context(
        notebook.id,
        {"mode": "include", "source_ids": ["s1", "s2", "s3", "s4"],
         "narrowed": False, "owner_id": "user-local"},
    ):
        scoped = repo._ppr_retrieve(notebook.id, "q")
    assert spy.calls == unscoped_calls == [(["c-s3", "c-s1", "c-s4"], {})]
    assert [(c.chunk_id, c.relevance) for c in scoped] == [
        (c.chunk_id, c.relevance) for c in unscoped]


def test_active_drift_is_verified_on_read_and_binds_the_rest(repo, monkeypatch):
    """A source uploaded to the active library after the freeze outranks the
    frozen ones: the row is refused on read, the drift lands on the run's
    verdict, later windows list the active ceiling, and the slots still fill."""
    notebook = repo.create_notebook(NotebookCreate(name="solo"))
    with repo._write() as db:
        for sid in ("s1", "s2", "s3", "late"):
            _source(db, notebook.id, sid)
            _chunk(db, notebook.id, sid, f"c-{sid}")
    ranking = ["c-late", "c-s1", "c-s2", "c-s3"]
    _, spy = _install(repo, monkeypatch, ranking)
    with source_scope_context(
        notebook.id,
        {"mode": "include", "source_ids": ["s1", "s2", "s3"],
         "narrowed": False, "owner_id": "user-local"},
    ):
        out = repo._ppr_retrieve(notebook.id, "q")
        scope = current_source_scope()
        verdict = ceiling_binds(
            scope, notebook.id,
            drifted=lambda: pytest.fail("the memo must answer"),
            foreign_hidden=lambda: pytest.fail("the memo must answer"),
        )
    assert [c.chunk_id for c in out] == ["c-s1", "c-s2", "c-s3"]
    assert verdict is True
    assert spy.calls[0][1] == {}
    assert set(spy.calls[1][1]["allowed_source_ids"][notebook.id]) == {"s1", "s2", "s3"}


# --------------------------------------------------------------- the store

@pytest.fixture
def seeded(repo):
    active, base = _seed(repo)
    return repo, active, base


def _hydrate(repo, ids, **kwargs):
    with repo._connect() as db:
        return {row["id"] for row in ChunkStore.graph_hydrate_rows(db, ids, **kwargs)}


ALL = ["c-a1", "c-a2", "c-b1", "c-b2", "c-kh-000", "c-kh-001", "c-b-late"]


def test_store_keeps_only_listed_sources_of_a_listed_library(seeded):
    repo, active, base = seeded
    assert _hydrate(repo, ALL, allowed_source_ids={base: frozenset({"b1"})}) == {
        "c-a1", "c-a2", "c-b1"}


def test_store_empty_list_denies_the_library_and_none_lists_nothing(seeded):
    repo, active, base = seeded
    assert _hydrate(repo, ALL, allowed_source_ids={base: frozenset()}) == {"c-a1", "c-a2"}
    assert _hydrate(repo, ALL, allowed_source_ids={base: None}) == set(ALL)
    assert _hydrate(repo, ALL, allowed_source_ids={}) == set(ALL)
    assert _hydrate(repo, ALL) == set(ALL)


def test_store_pairs_each_list_with_its_own_library(seeded):
    """A's list naming B's source must not admit B's chunk: B is listed with
    its own (empty) list."""
    repo, active, base = seeded
    got = _hydrate(repo, ALL, allowed_source_ids={
        active: frozenset({"a1", "b1"}), base: frozenset()})
    assert got == {"c-a1"}


def test_store_unlisted_statement_is_byte_identical(seeded):
    repo, _active, base = seeded
    statements: list = []
    with repo._connect() as db:
        db.set_trace_callback(statements.append)
        ChunkStore.graph_hydrate_rows(db, ["c-a1", "c-b1"])
        ChunkStore.graph_hydrate_rows(db, ["c-a1", "c-b1"], allowed_source_ids={})
        db.set_trace_callback(None)
    historical = (
        "SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
        "c.notebook_id AS chunk_notebook_id, s.title AS source_title "
        "FROM chunks c JOIN sources s ON s.id=c.source_id "
        "WHERE c.id IN ('c-a1','c-b1')"
    )
    assert [s for s in statements if s.startswith("SELECT c.id")] == [historical, historical]


def test_store_binds_a_49k_ceiling_as_one_parameter(seeded):
    """Far past the deployment variable limit (32,766): still one parameter."""
    repo, _active, base = seeded
    big = frozenset({"b2", *(f"x-{i:05d}" for i in range(49_000))})
    assert _hydrate(repo, ALL, allowed_source_ids={base: big}) == {"c-a1", "c-a2", "c-b2"}


def test_store_plan_is_driven_by_candidate_keys_without_statistics(seeded):
    """Production SQLite has no ``sqlite_stat1``: the candidate primary keys
    must drive, never the notebook or source index."""
    repo, _active, base = seeded
    captured: list = []
    with repo._connect() as db:
        has_stats = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()[0]
        if has_stats:
            db.execute("DELETE FROM sqlite_stat1")
        original = db.execute

        class _Capture:
            def execute(self, sql, params=()):
                captured.append((sql, list(params)))
                return original(sql, params)

        ChunkStore.graph_hydrate_rows(
            _Capture(), ALL,
            allowed_source_ids={base: frozenset({"b1", "b2"})},
        )
        sql, params = captured[0]
        assert len(params) == len(ALL) + 3
        plan = [str(row[3]) for row in original("EXPLAIN QUERY PLAN " + sql, params)]
    joined = "\n".join(plan)
    assert any(
        line.startswith("SEARCH c USING INDEX sqlite_autoindex_chunks_1 (id=?)")
        for line in plan
    ), joined
    assert "idx_chunks_nb" not in joined and "idx_chunks_source" not in joined, joined
