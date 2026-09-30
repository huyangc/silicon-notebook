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
from app.services.graph_retrieval import (
    _PPR_CEILING_OVERFETCH,
    _PPR_CEILING_WALK_WINDOWS,
    PprRanking,
)
from app.services.source_scope import (
    ceiling_binds,
    current_source_scope,
    source_scope_context,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients

NOW = "2026-09-30T00:00:00"
TOP = 3
# How far the ceiling walk reaches down the ranking: its over-ranked prefix
# plus its budget of 900-candidate windows.
REACH = TOP * _PPR_CEILING_OVERFETCH + _PPR_CEILING_WALK_WINDOWS * 900


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
    ``max_results`` like the real bounded heap, returned as a ``PprRanking``
    whose ``rank_further`` re-cuts the same ranking (no second ``scale_ppr``
    call, like the real continuation over the same score vector).

    ``mapped`` models the scale path's per-library chunk-id sets (read here
    from the ``chunks`` table): with them a library ``refuse_library`` refuses
    leaves the ranking in its first pass, as ``scale_ppr`` does; without them
    (the rustworkx ranking has none) the walk has to refuse that library's
    candidates through the store."""

    def __init__(self, repo, chunk_ids, *, mapped=True):
        n = len(chunk_ids)
        self.repo = repo
        self.mapped = mapped
        self.ranking = [(cid, (n - i) / n) for i, cid in enumerate(chunk_ids)]
        self.calls: list = []
        self.further: list = []

    def _library_chunk_ids(self):
        with self.repo._connect() as db:
            rows = db.execute("SELECT notebook_id, id FROM chunks").fetchall()
        out: dict = {}
        for row in rows:
            out.setdefault(row[0], set()).add(row[1])
        return out

    def __call__(self, notebook_id, question, max_results=None, refuse_library=None):
        self.calls.append(max_results)
        if max_results is None:
            return list(self.ranking)
        dropped: tuple = ()
        ranking = self.ranking
        if self.mapped and refuse_library is not None:
            libraries = self._library_chunk_ids()
            dropped = tuple(nb for nb in libraries if refuse_library(nb))
            gone = set().union(*(libraries[nb] for nb in dropped))
            ranking = [item for item in ranking if item[0] not in gone]

        def rank_further(limit):
            self.further.append(limit)
            return ranking[:limit]

        return PprRanking(
            ranking[:max_results],
            rank_further=rank_further if len(ranking) > max_results else None,
            libraries_dropped=dropped,
        )


class _HydrationSpy:
    def __init__(self, store):
        self.store = store
        self.calls: list = []

    def __call__(self, db, chunk_ids, **kwargs):
        self.calls.append((list(chunk_ids), kwargs))
        return self.store.graph_hydrate_rows(db, chunk_ids, **kwargs)


def _install(repo, monkeypatch, ranking, *, mapped=True):
    graph = repo.retrieval.graph
    fake = _Ranking(repo, ranking, mapped=mapped)
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


def test_window_exhaustion_ranks_further_and_still_fills(repo, monkeypatch):
    """More refused candidates than the over-ranked prefix holds: the same
    ranking is extended once (``rank_further``, no second ``scale_ppr``) as
    far as the walk's budget can reach, and the slots are still filled."""
    active, base = _seed(repo, knowhow_chunks=60)
    ranking = [f"c-kh-{i:03d}" for i in range(60)] + ["c-b1", "c-a1", "c-b2", "c-a2"]
    fake, _ = _install(repo, monkeypatch, ranking)
    with _mounted_scope(active, base):
        out = repo._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-b1", "c-a1", "c-b2"]
    assert fake.calls == [TOP * _PPR_CEILING_OVERFETCH]
    assert fake.further == [REACH + 1]


def test_a_passage_kept_in_the_prefix_takes_one_slot_after_ranking_further(
    repo, monkeypatch,
):
    """The prefix keeps c-a1 and runs out; ``rank_further`` ranks c-a1 again
    at the top.  It must not be hydrated and kept a second time."""
    active, base = _seed(repo, knowhow_chunks=60)
    ranking = ["c-a1"] + [f"c-kh-{i:03d}" for i in range(60)] + ["c-b1", "c-b2"]
    fake, _ = _install(repo, monkeypatch, ranking)
    with _mounted_scope(active, base):
        out = repo.retrieval.graph._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-a1", "c-b1", "c-b2"]
    assert len(fake.further) == 1


def test_an_excluded_library_takes_no_slot(repo, monkeypatch):
    active, base = _seed(repo)
    ranking = ["c-b1", "c-b2", "c-a1", "c-a2"]
    _install(repo, monkeypatch, ranking)
    with source_scope_context(active, None, {"mode": "include", "notebook_ids": []}):
        out = repo._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-a1", "c-a2"]


EXCLUDED = {"mode": "include", "notebook_ids": []}


def test_rows_of_a_library_listed_mid_window_are_judged_row_by_row(repo, monkeypatch):
    """B is excluded and the ranking carries no chunk map, so B is refused
    through the walk: c-b1 lists B, and c-b2 -- in the SAME window, whose
    statement was built before B was listed -- must be refused too.  Read on
    the unfiltered ``graph._ppr_retrieve``: the facade's backstop filter
    would hide a leaked row (and leave a slot empty instead)."""
    active, base = _seed(repo)
    with repo._write() as db:
        _source(db, active, "a3")
        _chunk(db, active, "a3", "c-a3")
    _install(repo, monkeypatch, ["c-b1", "c-b2", "c-a1", "c-a2", "c-a3"], mapped=False)
    with source_scope_context(active, None, EXCLUDED):
        out = repo.retrieval.graph._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-a1", "c-a2", "c-a3"]


def test_two_late_uploads_in_one_window_are_both_refused(repo, monkeypatch):
    """The active-library twin: the first late upload lists the active
    ceiling, the second one in the same window is judged against it."""
    notebook = repo.create_notebook(NotebookCreate(name="solo"))
    with repo._write() as db:
        for sid in ("s1", "s2", "s3", "late", "late2"):
            _source(db, notebook.id, sid)
            _chunk(db, notebook.id, sid, f"c-{sid}")
    _install(repo, monkeypatch, ["c-late", "c-late2", "c-s1", "c-s2", "c-s3"])
    with source_scope_context(
        notebook.id,
        {"mode": "include", "source_ids": ["s1", "s2", "s3"],
         "narrowed": False, "owner_id": "user-local"},
    ):
        out = repo.retrieval.graph._ppr_retrieve(notebook.id, "q")
    assert [c.chunk_id for c in out] == ["c-s1", "c-s2", "c-s3"]


def _bulk_rows(repo, notebook_id, count):
    """``count`` chunks of one source of ``notebook_id``, returned in id order."""
    ids = [f"c-bx-{i:05d}" for i in range(count)]
    with repo._write() as db:
        _source(db, notebook_id, "bx")
        db.executemany(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            [(cid, notebook_id, "bx", "t", "S", "[]", NOW) for cid in ids],
        )
    return ids


def _trace_hydrations(repo):
    """Every hydration statement the walk sends from now on (the SQLite
    connection is per thread and reused, so one trace callback sees them)."""
    statements: list = []
    with repo.retrieval.graph._connect() as db:
        db.set_trace_callback(statements.append)
    return statements


def _hydrations(statements):
    return [s for s in statements if "chunk_notebook_id" in s]


def _events(repo, monkeypatch):
    events: list = []
    original = repo.event_log.emit

    def emit(event, **kwargs):
        events.append(event)
        return original(event, **kwargs)

    monkeypatch.setattr(repo.event_log, "emit", emit)
    return events


def test_a_wholly_refused_library_is_dropped_in_memory_before_the_walk(
    repo, monkeypatch,
):
    """20,000 chunks of an unticked library outrank the active library's two.
    The ranking leaves that library out in its first pass: one hydration
    statement, both in-ceiling passages, no second ranking pass."""
    active, base = _seed(repo)
    ahead = _bulk_rows(repo, base, 20_000)
    fake, _ = _install(repo, monkeypatch, ahead + ["c-a1", "c-a2"])
    statements = _trace_hydrations(repo)
    with source_scope_context(active, None, EXCLUDED):
        out = repo.retrieval.graph._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-a1", "c-a2"]
    assert len(_hydrations(statements)) == 1
    assert fake.further == []


def test_without_a_chunk_map_the_walk_stops_at_its_budget(repo, monkeypatch):
    """The same 20,000 refused candidates on a ranking that cannot leave the
    library out (the rustworkx ranking has no per-library chunk sets): the walk spends
    its prefix windows (3, 6, 12, 24 and 48 ids at TOP=3: five statements)
    and then exactly ``_PPR_CEILING_WALK_WINDOWS`` windows of 900, gives up
    on the slots it could not fill, and says so."""
    active, base = _seed(repo)
    ahead = _bulk_rows(repo, base, 20_000)
    fake, _ = _install(repo, monkeypatch, ahead + ["c-a1", "c-a2"], mapped=False)
    events = _events(repo, monkeypatch)
    statements = _trace_hydrations(repo)
    with source_scope_context(active, None, EXCLUDED):
        out = repo.retrieval.graph._ppr_retrieve(active, "q")
    assert out == []
    assert len(_hydrations(statements)) == 5 + _PPR_CEILING_WALK_WINDOWS
    assert fake.calls == [TOP * _PPR_CEILING_OVERFETCH]
    exhausted = [
        {k: v for k, v in e.items() if k not in ("ts", "channel")}
        for e in events if e.get("kind") == "ppr_ceiling_walk_exhausted"
    ]
    assert exhausted == [{
        "kind": "ppr_ceiling_walk_exhausted",
        "notebook_id": active,
        "top_chunks": TOP,
        "kept": 0,
        "windows": _PPR_CEILING_WALK_WINDOWS,
        "candidates_walked": REACH,
        "libraries_dropped": 0,
    }]


@pytest.mark.parametrize("beyond_reach, announced", [(0, False), (1, True)])
def test_exhaustion_is_announced_only_when_candidates_were_left(
    repo, monkeypatch, beyond_reach, announced,
):
    """A ranking whose last candidate is the last one the budget reaches has
    been walked completely: nothing to announce.  One candidate more, and the
    walk stopped short of it."""
    active, base = _seed(repo)
    ranking = _bulk_rows(repo, base, REACH + beyond_reach)
    _install(repo, monkeypatch, ranking, mapped=False)
    events = _events(repo, monkeypatch)
    with source_scope_context(active, None, EXCLUDED):
        out = repo.retrieval.graph._ppr_retrieve(active, "q")
    assert out == []
    kinds = [e.get("kind") for e in events]
    assert ("ppr_ceiling_walk_exhausted" in kinds) is announced


def test_ranking_further_reuses_the_score_vector(repo, monkeypatch):
    """The real scale path: the only admitted passage is ranked LAST, so the
    one-candidate prefix runs out and the walk ranks further.  That must
    reuse the PPR already computed -- one power iteration, one
    ``scale_ppr_done`` event -- and keep the score the passage has in the
    complete ranking."""
    import app.services.graph_retrieval as graph_module
    import app.services.kg.scale_index as scale_index
    from tests.test_ppr_retrieve import _seed_two_doc_moe

    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))
    active = _seed_two_doc_moe(repo, suffix="-act")
    repo.rebuild_unified_kg(active.id)
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    graph = repo.retrieval.graph
    question = "Mixture of Experts"
    # Fixed chunk seeds: the real ones are retrieved under the run's scope,
    # and the ranking must be the same with and without it here.
    from types import SimpleNamespace

    seeds = [
        SimpleNamespace(chunk_id=cid, relevance=weight)
        for cid, weight in (("cA", 0.9), ("cB-act", 0.7), ("cA-act", 0.5), ("cB", 0.3))
    ]
    monkeypatch.setattr(graph, "_retrieve_chunks", lambda *a, **k: (seeds, [], None))
    complete = graph.scale_ppr(active.id, question)
    assert len(complete) >= 3
    last = complete[-1][0]
    with repo._connect() as db:
        owner, source = db.execute(
            "SELECT notebook_id, source_id FROM chunks WHERE id=?", (last,)
        ).fetchone()
    monkeypatch.setattr(graph.settings, "ppr_top_chunks", 1)
    monkeypatch.setattr(graph_module, "_PPR_CEILING_OVERFETCH", 1)
    iterations = {"n": 0}
    real_ppr = scale_index.personalized_ppr

    def counting_ppr(*args, **kwargs):
        iterations["n"] += 1
        return real_ppr(*args, **kwargs)

    monkeypatch.setattr(scale_index, "personalized_ppr", counting_ppr)
    events = _events(repo, monkeypatch)
    with source_scope_context(
        active.id,
        {"mode": "include", "source_ids": [source] if owner == active.id else [],
         "narrowed": False, "owner_id": "user-local"},
        None,
        {base.id: [source] if owner == base.id else []},
    ):
        out = graph._ppr_retrieve(active.id, question)
    assert [(c.chunk_id, c.relevance) for c in out] == [(last, complete[-1][1])]
    assert iterations["n"] == 1
    assert [e["kind"] for e in events if e.get("kind") == "scale_ppr_done"] == [
        "scale_ppr_done"]


def test_an_index_loaded_without_preload_still_drops_a_refused_library(
    repo, monkeypatch,
):
    """The base library's scale index is loaded lazily (no startup preload,
    so it carries no ``_ppr_chunk_ids``) and the run unticks that library,
    whose chunks outrank the active library's.  The scale ranking computes
    the chunk-id set once, caches it on the index and leaves the library out
    in its single ranking pass: one hydration statement, both active
    passages kept, no second ranking pass."""
    import app.services.graph_retrieval as graph_module
    from types import SimpleNamespace
    from tests.test_ppr_retrieve import _seed_two_doc_moe

    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))
    active = _seed_two_doc_moe(repo, suffix="-act")
    repo.rebuild_unified_kg(active.id)
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    graph = repo.retrieval.graph
    index = graph._scale_index(base.id, allow_stale=True)
    assert getattr(index, "_ppr_chunk_ids", None) is None
    seeds = [
        SimpleNamespace(chunk_id=cid, relevance=weight)
        for cid, weight in (("cA", 0.9), ("cB", 0.8), ("cA-act", 0.2), ("cB-act", 0.1))
    ]
    monkeypatch.setattr(graph, "_retrieve_chunks", lambda *a, **k: (seeds, [], None))
    monkeypatch.setattr(graph.settings, "ppr_top_chunks", 2)
    monkeypatch.setattr(graph_module, "_PPR_CEILING_OVERFETCH", 1)
    passes = {"n": 0}
    real_ranking = graph_module._normalized_score_ranking

    def counting_ranking(*args, **kwargs):
        passes["n"] += 1
        return real_ranking(*args, **kwargs)

    monkeypatch.setattr(graph_module, "_normalized_score_ranking", counting_ranking)
    statements = _trace_hydrations(repo)
    with source_scope_context(active.id, None, EXCLUDED):
        out = graph._ppr_retrieve(active.id, "Mixture of Experts")
    assert {c.chunk_id for c in out} == {"cA-act", "cB-act"}
    assert len(_hydrations(statements)) == 1
    assert passes["n"] == 1
    assert graph._scale_index(base.id, allow_stale=True)._ppr_chunk_ids == {"cA", "cB"}


def test_a_ranking_left_empty_by_a_refused_library_is_not_a_scale_failure(
    repo, monkeypatch,
):
    """The active library has no passages of its own; the only other
    participant is unticked.  The scale ranking succeeds and leaves nothing
    admissible: PPR returns nothing, reports no scale bailout, and never
    falls back to building the rustworkx graph."""
    from types import SimpleNamespace
    from tests.test_ppr_retrieve import _seed_two_doc_moe

    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))
    active = repo.create_notebook(NotebookCreate(name="empty"))
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    graph = repo.retrieval.graph
    seeds = [SimpleNamespace(chunk_id="cA", relevance=0.9)]
    monkeypatch.setattr(graph, "_retrieve_chunks", lambda *a, **k: (seeds, [], None))
    monkeypatch.setattr(
        graph, "_ppr_graph",
        lambda *a, **k: pytest.fail("the rustworkx fallback must not run"))
    assert graph.scale_ppr(active.id, "Mixture of Experts"), "the base must rank"
    events = _events(repo, monkeypatch)
    with source_scope_context(active.id, None, EXCLUDED):
        out = graph._ppr_retrieve(active.id, "Mixture of Experts")
    assert out == []
    kinds = [e.get("kind") for e in events]
    assert "scale_ppr_bailout" not in kinds
    assert kinds.count("scale_ppr_done") == 1


def test_a_skipped_chunk_still_sets_the_score_range():
    """``rank_further`` with a dropped library must give every kept chunk the
    score it has in the prefix already walked: a skipped chunk leaves the
    selection, never the min/max the scores are normalised by."""
    from app.services.graph_retrieval import _normalized_score_ranking

    scores = [("low", 1.0), ("top", 5.0), ("mid", 3.0), ("dropped", 9.0)]
    unskipped, _ = _normalized_score_ranking(scores, limit=4)
    skipped, considered = _normalized_score_ranking(
        scores, limit=2, skip=lambda chunk_id: chunk_id == "dropped")
    assert skipped == [("top", 0.5), ("mid", 0.25)]
    assert skipped == [item for item in unskipped if item[0] != "dropped"][:2]
    assert considered == 4


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


@pytest.mark.parametrize("statistics", ["none", "notebook_index_looks_unique"])
def test_store_plan_is_driven_by_candidate_keys(seeded, statistics):
    """Production SQLite has no ``sqlite_stat1``: the candidate primary keys
    must drive, never the notebook or source index.  The second case forges
    statistics that make ``idx_chunks_nb`` / ``idx_chunks_nb_created`` look
    unique per value: the plan must not move either, which is why the
    per-library notebook equality carries no unary ``+`` (it sits in an OR no
    index can answer, see ``_library_source_ceiling_clause``)."""
    repo, _active, base = seeded
    captured: list = []
    with repo._write() as db:
        db.execute("ANALYZE")
        db.execute("DELETE FROM sqlite_stat1")
        if statistics != "none":
            db.executemany(
                "INSERT INTO sqlite_stat1(tbl,idx,stat) VALUES ('chunks',?,?)",
                [("idx_chunks_nb", "1000000 1"),
                 ("idx_chunks_nb_created", "1000000 1 1"),
                 ("sqlite_autoindex_chunks_1", "1000000 1")],
            )
    with repo._connect() as db:
        # Re-read (or forget) the statistics on this very connection.
        db.execute("ANALYZE sqlite_master")
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
