"""``SourceFinder.find`` (design §5): the source set, both legs, tiering,
ordering, the cursor, coverage flags and card status.

Tier rules are asserted as tiers, never as floats: the semantic leg is a
``FakeRetrieval`` whose relevances sit clearly above / between / below the two
configured lines.  The real ``retrieve_chunks_in_sources`` runs in the lane and
permission cases (baseline, FTS-degraded and ANN lanes; another member's Memory
projection without a run scope).  The metadata leg is always the real SQLite
store (its PostgreSQL twin is ``tests/postgres/test_find_sources_store_pg.py``).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.domain.vector_index import encode_vector
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_finder import (
    FindSourcesCursorError,
    FindSourcesError,
    FindSourcesRequest,
    SourceFinder,
    paper_card_status,
)
from app.services.source_scope import (
    current_source_scope,
    default_ceiling_context,
    run_ceiling_binds,
    source_scope_context,
)
from tests.model_testkit import bind_all_embedding_clients

STRONG, RELATED = 0.7, 0.3
ABOVE, BETWEEN, BELOW = 0.85, 0.5, 0.15
CARD_VERSION = 3


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.services.sqlite_repository import (
        SQLiteRepository,
        reset_request_user,
        set_request_user,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    instance = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    monkeypatch.setattr(instance.settings, "find_sources_strong_relevance", STRONG)
    monkeypatch.setattr(instance.settings, "find_sources_related_relevance", RELATED)
    alice = instance._runtime.identity.create_user("a00123456", "password-12")
    bob = instance._runtime.identity.create_user("b00654321", "password-12")
    token = set_request_user(alice)
    try:
        nb = instance.create_notebook(NotebookCreate(name="papers")).id
        instance._runtime.sharing.add_member(nb, bob.id)
        yield instance, nb, alice.id, bob.id
    finally:
        reset_request_user(token)


class FakeRetrieval:
    """``retrieve_chunks_in_sources`` with scripted relevances per source."""

    def __init__(self, lane: str = "baseline", relevance=None):
        self.lane = lane
        self.relevance = dict(relevance or {})
        self.calls: list = []
        self.flags: list = []

    def retrieve_chunks_in_sources(self, notebook_id, query, recall, allowed, *,
                                   peer=False, narrowed=False):
        self.calls.append((notebook_id, query, recall, tuple(allowed)))
        self.flags.append({"peer": peer, "narrowed": narrowed})
        out = [
            RetrievedChunk(
                chunk_id=f"{source_id}-c{index}", source_id=source_id,
                source_title="", section_path="",
                text=f"passage {index} of {source_id}",
                element_ids=[f"el-{source_id}-{index}"],
                score=value, relevance=value,
            )
            for source_id in allowed
            for index, value in enumerate(self.relevance.get(source_id, ()))
        ]
        out.sort(key=lambda chunk: (-chunk.relevance, chunk.chunk_id))
        return out[:recall], self.lane


def _finder(instance, retrieval=None, *, enumeration=None, sources=None,
            event_log=None) -> SourceFinder:
    runtime = instance._runtime
    return SourceFinder(
        database=runtime.database,
        sources=sources or runtime.source_store,
        retrieval=retrieval or instance.retrieval,
        enumeration=enumeration or runtime.collection_enumeration,
        settings=instance.settings,
        paper_card_version=CARD_VERSION,
        event_log=event_log,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_postgres(instance) -> bool:
    return type(instance).__name__ == "PostgresRepository"


def _execute(db, instance, sql: str, params: tuple) -> None:
    db.execute(sql.replace("?", "%s") if _is_postgres(instance) else sql, params)


def _source(
    instance, nb, sid, *, title="", summary="", doc_type="academic_paper",
    texts=(), source_type="document", memory_id=None,
) -> str:
    now = _now()
    with instance._runtime.database.write() as db:
        _execute(
            db, instance,
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,memory_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb, title or sid, source_type, f"{sid}.md", f"/tmp/{sid}.md", 0,
             sid, summary, doc_type, "extracted", memory_id, now, now),
        )
        for index, text in enumerate(texts, 1):
            _execute(
                db, instance,
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{index:04d}", sid, "paragraph", f"p{index}", text,
                 "{}", now),
            )
    if texts:
        instance._runtime.source_chunking.chunk_and_embed_source(sid)
        instance._runtime.knowledge_query.backfill_chunk_fts(nb)
    return sid


def _meta(instance, nb, sid, *, is_paper=True, year=None, venue="", title="",
          keywords=(), authors=()) -> None:
    instance._runtime.source_store.upsert_paper_meta(sid, nb, {
        "is_paper": is_paper, "paper_title": title or None, "venue": venue or None,
        "pub_year": year, "keywords": list(keywords),
        "authors": [{"position": i, "name": n, "affiliation": ""}
                    for i, n in enumerate(authors)],
        "model": "m",
    })


def _raw_chunk(instance, nb, sid, chunk_id, text) -> None:
    """A chunk row written straight to the table (the write paths refuse a
    Memory source; this stands for a legacy row the read side must still
    keep away from other members)."""
    vector = instance._runtime.models.embedding(
        "retrieval_query_embedding").embed_texts([text])[0]
    encoded = (
        encode_vector([float(v) for v in vector]) if _is_postgres(instance)
        else json.dumps(vector)
    )
    with instance._runtime.database.write() as db:
        _execute(
            db, instance,
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            (chunk_id, nb, sid, text, "", "[]", _now()),
        )
        _execute(
            db, instance,
            "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            (chunk_id, nb, encoded, _now()),
        )
    instance._runtime.knowledge_query.backfill_chunk_fts(nb)


def _tiers(result) -> dict:
    return {item.source_id: item.tier for item in result.items}


# --------------------------------------------------------------- tiering
def test_metadata_tiers_follow_the_field_and_word_boundary_rule(repo):
    instance, nb, *_ = repo
    _source(instance, nb, "s-title", title="A GAN for faces")
    _source(instance, nb, "s-organ", title="Organ transplant outcomes")
    _source(instance, nb, "s-kw")
    _meta(instance, nb, "s-kw", keywords=["GAN", "faces"])
    _source(instance, nb, "s-ptitle")
    _meta(instance, nb, "s-ptitle", title="Progressive GAN training")
    _source(instance, nb, "s-summary", summary="we compare against a GAN baseline")
    _source(instance, nb, "s-author")
    _meta(instance, nb, "s-author", authors=["Gan Wei"])
    _source(instance, nb, "s-none", title="Transformers")
    result = _finder(instance).find(nb, FindSourcesRequest(terms=("GAN",), kind="all"))
    assert _tiers(result) == {
        "s-title": "strong", "s-kw": "strong", "s-ptitle": "strong",
        "s-organ": "related", "s-summary": "related", "s-author": "related",
    }
    reasons = {item.source_id: item.reasons for item in result.items}
    assert reasons["s-kw"] == ("keywords",)
    assert reasons["s-author"] == ("author",)
    assert result.lanes == {}


@pytest.mark.parametrize("lane", ["baseline", "ann"])
def test_semantic_tiers_use_the_configured_lines(repo, lane):
    instance, nb, *_ = repo
    for sid in ("s-above", "s-between", "s-below", "s-none"):
        _source(instance, nb, sid)
    fake = FakeRetrieval(lane, {
        "s-above": [ABOVE, BELOW], "s-between": [BETWEEN], "s-below": [BELOW],
    })
    result = _finder(instance, fake).find(
        nb, FindSourcesRequest(query="diffusion models", kind="all"))
    assert _tiers(result) == {"s-above": "strong", "s-between": "related"}
    above = result.items[0]
    assert above.reasons == ("passages:2",)
    assert above.best_passage.chunk_id == "s-above-c0"
    assert above.best_passage.element_ids == ("el-s-above-0",)
    assert result.lanes == {nb: lane}


def test_the_lexical_only_lane_needs_two_passages_and_ignores_the_lines(repo):
    instance, nb, *_ = repo
    for sid in ("s-one", "s-two", "s-meta"):
        _source(instance, nb, sid)
    _meta(instance, nb, "s-meta", keywords=["diffusion"])
    fake = FakeRetrieval("fts_degraded", {
        "s-one": [ABOVE], "s-two": [BELOW, BELOW], "s-meta": [ABOVE],
    })
    result = _finder(instance, fake).find(nb, FindSourcesRequest(
        query="diffusion", terms=("diffusion",), kind="all"))
    # One high-relevance passage is not "strong" without a semantic signal.
    assert _tiers(result) == {"s-meta": "strong", "s-two": "related"}


def test_the_order_is_total_and_deterministic(repo):
    instance, nb, *_ = repo
    _source(instance, nb, "s-b", title="vision transformer")
    _source(instance, nb, "s-a", title="vision transformer")
    _source(instance, nb, "s-two-fields", title="vision transformer",
            summary="vision transformer study")
    _source(instance, nb, "s-new", title="vision transformer")
    _meta(instance, nb, "s-new", year=2024)
    _source(instance, nb, "s-old", title="vision transformer")
    _meta(instance, nb, "s-old", year=2019)
    _source(instance, nb, "s-sem", title="unrelated")
    fake = FakeRetrieval("baseline", {
        "s-b": [BETWEEN], "s-a": [BETWEEN], "s-sem": [ABOVE],
        "s-new": [BETWEEN], "s-old": [BETWEEN],
    })
    request = FindSourcesRequest(query="vit", terms=("vision transformer",), kind="all")
    first = [item.source_id for item in _finder(instance, fake).find(nb, request).items]
    assert first == [
        "s-two-fields",                        # most matched fields
        "s-new", "s-old", "s-a", "s-b",        # year desc (None last), then id
        "s-sem",                               # passage-only strong: no fields
    ]
    assert [i.source_id for i in _finder(instance, fake).find(nb, request).items] == first


def test_a_pure_filter_lists_every_source_strong_by_year(repo):
    instance, nb, *_ = repo
    for sid, year in (("s-1", 2020), ("s-2", 2023), ("s-3", None), ("s-4", 2023)):
        _source(instance, nb, sid, title=f"title {sid}")
        _meta(instance, nb, sid, year=year, venue="CVPR")
    _source(instance, nb, "s-other")
    _meta(instance, nb, "s-other", year=2023, venue="ACL")
    fake = FakeRetrieval()
    result = _finder(instance, fake).find(nb, FindSourcesRequest(venue="cvpr"))
    assert [(item.source_id, item.tier) for item in result.items] == [
        ("s-2", "strong"), ("s-4", "strong"), ("s-1", "strong"), ("s-3", "strong"),
    ]
    assert fake.calls == []                 # no query: no semantic leg
    assert (result.strong, result.related, result.filtered_scope_total) == (4, 0, 4)
    assert result.lexical_complete is True


# ------------------------------------------------------- paging and flags
def test_the_cursor_pages_and_dies_with_any_parameter_change(repo):
    instance, nb, *_ = repo
    for index in range(5):
        _source(instance, nb, f"s-{index}", title=f"graph paper {index}")
    finder = _finder(instance)
    request = FindSourcesRequest(terms=("graph",), kind="all", limit=2)
    pages, cursor = [], ""
    while True:
        result = finder.find(nb, FindSourcesRequest(**{**request.__dict__, "cursor": cursor}))
        pages.append([item.source_id for item in result.items])
        assert [item.rank for item in result.items] == list(
            range(2 * len(pages) - 1, 2 * len(pages) - 1 + len(result.items)))
        cursor = result.next_cursor
        if not cursor:
            break
    assert pages == [["s-0", "s-1"], ["s-2", "s-3"], ["s-4"]]
    second = finder.find(nb, FindSourcesRequest(terms=("graph",), kind="all", limit=2))
    for changed in (
        {"terms": ("graph", "net")}, {"kind": "papers"}, {"venue": "x"},
        {"year_from": 2000}, {"query": "q"}, {"author": "a"},
    ):
        with pytest.raises(FindSourcesCursorError):
            finder.find(nb, FindSourcesRequest(**{
                **request.__dict__, **changed, "cursor": second.next_cursor,
            }))
    with pytest.raises(FindSourcesCursorError):
        finder.find(nb, FindSourcesRequest(**{**request.__dict__, "cursor": "nonsense"}))
    # Same parameters, another page size: the offset still means the same.
    resumed = finder.find(nb, FindSourcesRequest(
        **{**request.__dict__, "limit": 3, "cursor": second.next_cursor}))
    assert [item.source_id for item in resumed.items] == ["s-2", "s-3", "s-4"]


def test_counts_and_coverage_flags(repo, monkeypatch):
    instance, nb, *_ = repo
    for sid in ("s-a", "s-b", "s-c", "s-d"):
        _source(instance, nb, sid, doc_type="manual")
    monkeypatch.setattr(instance.settings, "find_sources_chunk_recall", 3)
    full = FakeRetrieval("baseline", {"s-a": [ABOVE], "s-b": [BETWEEN, BETWEEN]})
    result = _finder(instance, full).find(nb, FindSourcesRequest(query="q", kind="all"))
    assert (result.strong, result.related, result.filtered_scope_total) == (1, 1, 4)
    assert result.semantic_recall_capped is True     # 3rd passage still >= related
    assert result.lexical_complete is False          # no terms: nothing literal checked
    assert full.calls[0][2] == 3
    low = FakeRetrieval("baseline", {"s-a": [ABOVE], "s-b": [BETWEEN], "s-c": [BELOW]})
    assert _finder(instance, low).find(
        nb, FindSourcesRequest(query="q", kind="all")).semantic_recall_capped is False
    short = FakeRetrieval("baseline", {"s-a": [ABOVE]})
    assert _finder(instance, short).find(
        nb, FindSourcesRequest(query="q", kind="all")).semantic_recall_capped is False
    lexical = FakeRetrieval("fts_degraded", {"s-a": [BELOW, BELOW, BELOW]})
    assert _finder(instance, lexical).find(
        nb, FindSourcesRequest(query="q", kind="all")).semantic_recall_capped is True
    with_terms = _finder(instance, low).find(
        nb, FindSourcesRequest(query="q", terms=("x",), kind="all"))
    assert with_terms.lexical_complete is True


def test_the_filters_decide_the_semantic_allow_list_first(repo):
    instance, nb, *_ = repo
    _source(instance, nb, "s-paper")
    _meta(instance, nb, "s-paper", year=2022)
    _source(instance, nb, "s-old-paper")
    _meta(instance, nb, "s-old-paper", year=2015)
    _source(instance, nb, "s-pending")                      # no meta yet
    _source(instance, nb, "s-manual", doc_type="manual")
    _source(instance, nb, "s-notpaper")
    _meta(instance, nb, "s-notpaper", is_paper=False)
    fake = FakeRetrieval()
    _finder(instance, fake).find(nb, FindSourcesRequest(query="q"))
    assert set(fake.calls[-1][3]) == {"s-paper", "s-old-paper", "s-pending"}
    _finder(instance, fake).find(nb, FindSourcesRequest(query="q", year_from=2020))
    assert set(fake.calls[-1][3]) == {"s-paper"}
    _finder(instance, fake).find(nb, FindSourcesRequest(query="q", kind="all"))
    assert set(fake.calls[-1][3]) == {
        "s-paper", "s-old-paper", "s-pending", "s-manual", "s-notpaper"}


def test_card_status_is_read_in_one_batch_per_library(repo):
    instance, nb, *_ = repo
    store = instance._runtime.source_store
    for sid in ("s-ready", "s-stale", "s-failed", "s-missing"):
        _source(instance, nb, sid, title=f"lidar {sid}", texts=[f"text of {sid}"])
        _meta(instance, nb, sid, authors=["A One", "B Two", "C Three", "D Four"])
    _source(instance, nb, "s-pending", title="lidar pending")
    _source(instance, nb, "s-notpaper", title="lidar not paper")
    _meta(instance, nb, "s-notpaper", is_paper=False)

    def card(sid, **kw):
        args = dict(basis=store.source_card_basis(sid), card_version=CARD_VERSION,
                    status="ready", card={"tldr": {"text": "t"}}, raw_json={}, model="m")
        args.update(kw)
        assert store.upsert_paper_card(sid, nb, **args)

    card("s-ready")
    card("s-stale", card_version=CARD_VERSION - 1)
    card("s-failed", status="failed", card={})
    result = _finder(instance).find(nb, FindSourcesRequest(terms=("lidar",), kind="all"))
    status = {item.source_id: item.card_status for item in result.items}
    assert status == {
        "s-ready": "ready", "s-stale": "missing", "s-failed": "failed",
        "s-missing": "missing", "s-pending": "meta_pending",
        "s-notpaper": "not_paper",
    }
    authors = {item.source_id: item.authors for item in result.items}
    assert authors["s-ready"] == ("A One", "B Two", "C Three")


def test_paper_card_status_is_judged_against_the_current_basis():
    card = {"card_version": 2, "basis": "b1", "status": "ready"}
    assert paper_card_status(True, False, card, "b1", card_version=2) == "ready"
    assert paper_card_status(True, False, card, "b2", card_version=2) == "missing"
    assert paper_card_status(True, False, card, "b1", card_version=3) == "missing"
    assert paper_card_status(True, False, None, "b1", card_version=2) == "missing"
    assert paper_card_status(False, False, card, "b1", card_version=2) == "not_paper"
    assert paper_card_status(None, True, None, "", card_version=2) == "meta_pending"
    assert paper_card_status(None, False, None, "", card_version=2) == "not_paper"


@pytest.mark.parametrize("request_kwargs", [
    {},
    {"kind": "papers"},
    {"terms": tuple(f"t{i}" for i in range(7))},
    {"terms": ("x" * 65,)},
    {"venue": "v" * 65},
    {"query": "q" * 1001},
    {"terms": ("x",), "year_from": 2024, "year_to": 2020},
    {"terms": ("x",), "kind": "everything"},
    {"terms": ("x",), "scope": "mounted"},
    {"terms": ("x",), "limit": 21},
    {"terms": ("x",), "limit": "twenty"},
    {"terms": ("x",), "limit": object()},
])
def test_malformed_requests_are_refused_not_trimmed(repo, request_kwargs):
    instance, nb, *_ = repo
    with pytest.raises(FindSourcesError):
        _finder(instance).find(nb, FindSourcesRequest(**request_kwargs))


# ---------------------------------------------------------- source set
def test_the_run_ceiling_is_the_source_set(repo):
    instance, nb, alice, _bob = repo
    for sid in ("s-in", "s-out", "s-also-in"):
        _source(instance, nb, sid, title=f"radar {sid}")
    fake = FakeRetrieval("baseline", {"s-in": [ABOVE], "s-out": [ABOVE]})
    ceiling = {"mode": "include", "source_ids": ["s-in", "s-also-in"],
               "narrowed": True, "owner_id": alice}
    with source_scope_context(nb, ceiling):
        result = _finder(instance, fake).find(
            nb, FindSourcesRequest(query="radar", terms=("radar",), kind="all"))
    assert {item.source_id for item in result.items} == {"s-in", "s-also-in"}
    assert result.filtered_scope_total == 2
    assert set(fake.calls[0][3]) == {"s-in", "s-also-in"}


def test_a_pushed_down_read_is_verified_and_cut_to_the_plan(repo):
    instance, nb, alice, _bob = repo
    _source(instance, nb, "s-early", title="sonar early")
    with default_ceiling_context(nb, alice, instance._runtime.ceiling_readers()):
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False          # verdict first
        _source(instance, nb, "s-late", title="sonar late")
        result = _finder(instance).find(
            nb, FindSourcesRequest(terms=("sonar",), kind="all"), ceiling_binds=False)
        flipped = nb in scope._ceiling_bound_libraries
    assert [item.source_id for item in result.items] == ["s-early"]
    assert flipped


def test_without_a_run_scope_another_members_memory_never_appears(repo):
    instance, nb, alice, bob = repo
    store = instance._runtime.source_store
    _source(instance, nb, "s-doc", title="doc",
            texts=["quantum annealing schedules for the ising model " * 4])
    now = _now()
    with instance._runtime.database.write() as db:
        for memory_id, owner in (("mem-a", alice), ("mem-b", bob)):
            _execute(
                db, instance,
                "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
                "title,content_md,created_at,updated_at) VALUES "
                "(?,?,?,'ask_answer','confirmed','m','m',?,?)",
                (memory_id, nb, owner, now, now),
            )
    _source(instance, nb, "src-ma", source_type="memory", memory_id="mem-a",
            doc_type="")
    _source(instance, nb, "src-mb", source_type="memory", memory_id="mem-b",
            doc_type="")
    secret = "quantum annealing schedules private note of bob " * 4
    _raw_chunk(instance, nb, "src-mb", "chunk-mb", secret)
    _raw_chunk(instance, nb, "src-ma", "chunk-ma", secret.replace("bob", "alice"))
    allowed = [*store.all_visible_source_ids(nb), *store.hidden_source_ids(nb, alice)]
    assert "src-mb" not in allowed and "src-ma" in allowed
    # Control: with B's projection in the list the leg does read it.
    leaked, _lane = instance.retrieval.retrieve_chunks_in_sources(
        nb, "quantum annealing", 400, [*allowed, "src-mb"])
    assert "chunk-mb" in {chunk.chunk_id for chunk in leaked}
    chunks, _lane = instance.retrieval.retrieve_chunks_in_sources(
        nb, "quantum annealing", 400, allowed)
    assert "chunk-mb" not in {chunk.chunk_id for chunk in chunks}
    result = _finder(instance).find(
        nb, FindSourcesRequest(query="quantum annealing", kind="all"),
        allowed_source_ids=allowed,
    )
    found = {item.source_id for item in result.items}
    passages = {item.best_passage.chunk_id for item in result.items if item.best_passage}
    assert found == {"s-doc"}                  # no Memory projection is a "document"
    assert passages.isdisjoint({"chunk-mb", "chunk-ma"})


@pytest.mark.parametrize("lane", ["baseline", "fts_degraded", "ann"])
def test_every_real_lane_groups_by_source(repo, monkeypatch, lane):
    instance, nb, *_ = repo
    body = "bandgap reference circuit with curvature compensation detail "
    for sid in ("s-a", "s-b"):
        _source(instance, nb, sid, title=f"{sid} doc",
                texts=[f"{body} {sid} part {i} " * 6 for i in range(3)])
    _source(instance, nb, "s-off", title="off topic", texts=["gardening tips " * 20])
    candidates = instance.retrieval.candidates
    if lane == "ann":
        instance.rebuild_unified_kg(nb)
        instance.build_scale_index(nb)
        monkeypatch.setattr(instance.settings, "chunk_ann_enabled", True)
    elif lane == "fts_degraded":
        monkeypatch.setattr(candidates, "notebook_copy_stats",
                            lambda _nb: {"copyable": False, "size": {}})
    result = _finder(instance).find(
        nb, FindSourcesRequest(query="bandgap reference", kind="all"))
    assert result.lanes == {nb: lane}
    by_source = {item.source_id: item for item in result.items}
    assert {"s-a", "s-b"} <= set(by_source)
    for sid in ("s-a", "s-b"):
        item = by_source[sid]
        assert item.best_passage is not None
        assert item.reasons and item.reasons[-1].startswith("passages:")
        assert len(item.best_passage.text) <= 240


def test_ids_are_unique_per_item(repo):
    instance, nb, *_ = repo
    sid = _source(instance, nb, f"s-{uuid.uuid4().hex[:6]}", title="unique title")
    result = _finder(instance).find(nb, FindSourcesRequest(terms=("unique",), kind="all"))
    assert [item.source_id for item in result.items] == [sid]
    assert result.items[0].notebook_id == nb


# ------------------------------------------------- coverage (design §5.4)
def test_lexical_complete_reports_the_unjudgeable_papers(repo):
    instance, nb, *_ = repo
    _source(instance, nb, "s-meta", title="lidar odometry")
    _meta(instance, nb, "s-meta", year=2022, venue="ICRA")
    _source(instance, nb, "s-pending", title="lidar mapping")    # no meta yet
    _source(instance, nb, "s-manual", title="lidar manual", doc_type="manual")
    finder = _finder(instance, FakeRetrieval())
    filtered = finder.find(nb, FindSourcesRequest(year_from=2020))
    assert [item.source_id for item in filtered.items] == ["s-meta"]
    assert (filtered.meta_pending_excluded, filtered.meta_pending_unchecked) == (1, 0)
    assert filtered.lexical_complete is False
    termed = finder.find(nb, FindSourcesRequest(terms=("lidar",)))
    assert {item.source_id for item in termed.items} == {"s-meta", "s-pending"}
    assert (termed.meta_pending_excluded, termed.meta_pending_unchecked) == (0, 1)
    assert termed.lexical_complete is False
    _meta(instance, nb, "s-pending", year=2019)
    done = finder.find(nb, FindSourcesRequest(terms=("lidar",)))
    assert (done.meta_pending_excluded, done.meta_pending_unchecked) == (0, 0)
    assert done.lexical_complete is True
    assert done.skipped_libraries == {}


# ------------------------------------------------ participant libraries
class TwoLibraries:
    """``source_scope`` over the active notebook plus one mounted library."""

    def __init__(self, instance, nb, *refs):
        self.instance, self.nb, self.refs = instance, nb, refs

    def source_scope(self, db, active, *, ceiling_binds, local_only=False):
        from types import SimpleNamespace

        ids = (self.nb,) if local_only else (self.nb, *self.refs)
        store = self.instance._runtime.source_store
        entries = [
            SimpleNamespace(notebook_id=notebook_id, source_id=source_id)
            for notebook_id in ids
            for source_id in store.all_visible_source_ids(notebook_id)
        ]
        return SimpleNamespace(
            notebook_ids=ids, tiers={}, plan=SimpleNamespace(sources=entries),
            ceiling_digest="digest",
        )


class Events:
    def __init__(self):
        self.events: list = []

    def emit(self, event):
        self.events.append(dict(event))


class FailingStore:
    """The real store, except ``find_sources_metadata_rows`` misbehaves for
    one library (``fail``: raise; ``stall``: sleep, then read)."""

    def __init__(self, store, notebook_id, *, fail=None, stall=0.0):
        self._store = store
        self._nb = notebook_id
        self._stalled = set(notebook_id) if isinstance(notebook_id, tuple) else {notebook_id}
        self._fail, self._stall = fail, stall
        self.calls = 0

    def find_sources_metadata_rows(self, db, notebook_id, **kwargs):
        self.calls += 1
        if notebook_id == self._nb and self._fail is not None:
            raise self._fail
        if notebook_id in self._stalled and self._stall:
            import time

            time.sleep(self._stall)
        return self._store.find_sources_metadata_rows(db, notebook_id, **kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _reference_library(instance, name="reference"):
    return instance.create_notebook(NotebookCreate(name=name)).id


def test_a_peer_library_never_cold_loads_its_scale_index(repo, monkeypatch):
    instance, nb, *_ = repo
    ref = _reference_library(instance)
    body = "phase locked loop jitter analysis for clock recovery "
    for notebook_id, sid in ((nb, "s-local"), (ref, "s-ref")):
        _source(instance, notebook_id, sid, title=f"{sid} doc",
                texts=[f"{body} {sid} part {i} " * 6 for i in range(3)])
    candidates = instance.retrieval.candidates
    monkeypatch.setattr(instance.settings, "chunk_ann_enabled", True)
    monkeypatch.setattr(
        candidates, "notebook_copy_stats",
        lambda notebook_id: {"copyable": notebook_id != ref, "size": {}},
    )
    loaded: list = []
    original = candidates._scale_index

    def spy(notebook_id, *args, **kwargs):
        loaded.append(notebook_id)
        return original(notebook_id, *args, **kwargs)

    monkeypatch.setattr(candidates, "_scale_index", spy)
    result = _finder(instance, enumeration=TwoLibraries(instance, nb, ref)).find(
        nb, FindSourcesRequest(query="phase locked loop jitter", kind="all"))
    assert ref not in loaded                        # never a cold scale-index load
    assert result.lanes[ref] == "fts_degraded"      # reported as what ran
    assert {"s-local", "s-ref"} <= {item.source_id for item in result.items}


def test_a_peer_library_that_fails_is_skipped_and_reported(repo):
    instance, nb, *_ = repo
    ref = _reference_library(instance)
    _source(instance, nb, "s-local", title="kalman filter")
    _meta(instance, nb, "s-local", year=2020)       # nothing pending: only the
    _source(instance, ref, "s-ref", title="kalman smoother")    # skip decides
    _meta(instance, ref, "s-ref", year=2021)
    events = Events()
    store = FailingStore(instance._runtime.source_store, ref, fail=RuntimeError("x"))
    result = _finder(
        instance, enumeration=TwoLibraries(instance, nb, ref), sources=store,
        event_log=events,
    ).find(nb, FindSourcesRequest(terms=("kalman",), kind="all"))
    assert [item.source_id for item in result.items] == ["s-local"]
    assert result.skipped_libraries == {ref: "unavailable"}
    assert result.meta_pending_unchecked == 0
    assert result.lexical_complete is False
    assert [e for e in events.events if e["kind"] == "find_sources_library_skipped"] == [{
        "kind": "find_sources_library_skipped", "notebook_id": ref,
        "error_type": "RuntimeError", "latency_ms": events.events[0]["latency_ms"],
        "reason": "unavailable",
    }]
    # The subject library is not isolated: its failure is the call's.
    broken = FailingStore(instance._runtime.source_store, nb, fail=RuntimeError("y"))
    with pytest.raises(RuntimeError):
        _finder(instance, enumeration=TwoLibraries(instance, nb, ref),
                sources=broken).find(nb, FindSourcesRequest(terms=("kalman",)))


def test_a_peer_library_read_runs_under_a_per_library_deadline(repo, monkeypatch):
    instance, nb, *_ = repo
    ref = _reference_library(instance)
    _source(instance, nb, "s-local", title="kalman filter")
    _source(instance, ref, "s-ref", title="kalman smoother")
    monkeypatch.setattr(instance.settings, "global_ask_notebook_timeout_seconds", 0.05)
    store = FailingStore(instance._runtime.source_store, ref, stall=0.2)
    result = _finder(
        instance, enumeration=TwoLibraries(instance, nb, ref), sources=store,
    ).find(nb, FindSourcesRequest(terms=("kalman",), kind="all"))
    assert [item.source_id for item in result.items] == ["s-local"]
    assert result.skipped_libraries == {ref: "timeout"}


# ----------------------------------------------------------- efficiency
def test_a_page_turn_inside_a_run_only_slices_the_ranking(repo):
    from app.services.retrieval_run import retrieval_run

    instance, nb, *_ = repo
    for index in range(5):
        _source(instance, nb, f"s-{index}", title=f"graph paper {index}")
    fake = FakeRetrieval("baseline", {f"s-{i}": [ABOVE] for i in range(5)})
    store = FailingStore(instance._runtime.source_store, "none")
    finder = _finder(instance, fake, sources=store)
    request = FindSourcesRequest(query="graphs", terms=("graph",), kind="all", limit=2)
    with retrieval_run(run_kind="ask_reasoning"):
        first = finder.find(nb, request)
        second = finder.find(nb, FindSourcesRequest(
            **{**request.__dict__, "cursor": first.next_cursor}))
    assert [i.source_id for i in first.items + second.items] == [
        "s-0", "s-1", "s-2", "s-3"]
    assert (len(fake.calls), store.calls) == (1, 1)
    # Without a run (MCP) every call recomputes.
    finder.find(nb, request)
    finder.find(nb, FindSourcesRequest(**{**request.__dict__, "cursor": first.next_cursor}))
    assert (len(fake.calls), store.calls) == (3, 3)


def test_only_a_really_narrow_filtered_list_is_attested_explicit(repo):
    """``producer_explicit`` switches the lexical corpus-language gate off, so
    it is attested only for a scope a year / venue / author filter or an
    explicit list made REALLY narrow (<= 10% of the library) -- never for
    ``kind`` dropping a few non-papers from a near-whole library (ASK-1)."""
    instance, nb, *_ = repo
    _source(instance, nb, "s-new")
    _meta(instance, nb, "s-new", year=2023)
    for index in range(10):
        _source(instance, nb, f"s-old-{index}")
        _meta(instance, nb, f"s-old-{index}", year=2012)
    _source(instance, nb, "s-manual", doc_type="manual")
    fake = FakeRetrieval()
    finder = _finder(instance, fake)
    store = instance._runtime.source_store
    whole = [*store.all_visible_source_ids(nb)]
    cases = [
        (FindSourcesRequest(query="q", kind="all"), None, False),
        (FindSourcesRequest(query="q", kind="all"), whole, False),
        (FindSourcesRequest(query="q"), None, False),        # kind drops s-manual only
        (FindSourcesRequest(query="q"), whole, False),
        (FindSourcesRequest(query="q", kind="all", year_to=2015), whole, False),  # 10/12
        (FindSourcesRequest(query="q", kind="all", year_from=2020), None, True),  # 1/12
        (FindSourcesRequest(query="q", kind="all"), ["s-new"], True),  # explicit 1/12
    ]
    for request, allowed, narrowed in cases:
        finder.find(nb, request, allowed_source_ids=allowed)
        assert fake.flags[-1] == {"peer": False, "narrowed": narrowed}, (request, allowed)


def test_a_filter_narrowed_fts_degraded_search_keeps_its_lexical_candidates(
        repo, monkeypatch):
    """On the FTS-degraded lane the corpus-language gate would drop a CJK
    query against a notebook sampled as English; a list the filters narrowed
    is a bounded scan, so the gate must not apply (``producer_explicit``)."""
    instance, nb, *_ = repo
    _source(instance, nb, "s-zh", title="zh", texts=[f"锁相环抖动分析与时钟恢复电路设计 第{i}部分 " * 40 for i in range(3)])
    _meta(instance, nb, "s-zh", year=2023)
    for index in range(10):          # the 2020+ filter keeps 1 of 11 (<= 10%)
        _source(instance, nb, f"s-en-{index}", title=f"en {index}",
                texts=[f"gardening notes about tomatoes and soil {index} " * 6])
        _meta(instance, nb, f"s-en-{index}", year=2012)
    candidates = instance.retrieval.candidates
    monkeypatch.setattr(candidates, "notebook_copy_stats",
                        lambda _nb: {"copyable": False, "size": {}})
    monkeypatch.setattr(instance.settings, "lexical_language_gate_enabled", True)
    monkeypatch.setattr(candidates, "_notebook_langs", lambda _nb: ["en"])
    finder = _finder(instance)
    narrowed = finder.find(nb, FindSourcesRequest(
        query="哪些论文研究了锁相环抖动问题", kind="all", year_from=2020))
    assert narrowed.lanes == {nb: "fts_degraded"}
    assert [item.source_id for item in narrowed.items] == ["s-zh"]
    assert narrowed.items[0].best_passage is not None
    # Control: the same search over the whole library (nothing narrowed) is
    # gated, so the case above really depends on the attestation.
    whole = finder.find(nb, FindSourcesRequest(query="哪些论文研究了锁相环抖动问题", kind="all"))
    assert whole.items == ()


# ------------------------------------------------- explicit (MCP) sets
def test_a_changed_explicit_list_invalidates_the_cursor(repo):
    instance, nb, *_ = repo
    for index in range(4):
        _source(instance, nb, f"s-{index}", title=f"radar {index}")
    finder = _finder(instance)
    request = FindSourcesRequest(terms=("radar",), kind="all", limit=1)
    listed = ["s-0", "s-1", "s-2"]
    first = finder.find(nb, request, allowed_source_ids=listed)
    resumed = FindSourcesRequest(**{**request.__dict__, "cursor": first.next_cursor})
    # Same set in another order: the same cursor.
    assert finder.find(nb, resumed, allowed_source_ids=listed[::-1]).items
    with pytest.raises(FindSourcesCursorError):
        finder.find(nb, resumed, allowed_source_ids=["s-0", "s-1", "s-3"])


def test_an_explicit_subset_never_shows_a_hit_outside_it(repo):
    instance, nb, *_ = repo
    _source(instance, nb, "s-in", title="sparse attention in")
    _source(instance, nb, "s-out", title="sparse attention out")
    _source(instance, nb, "s-other", title="unrelated")
    fake = FakeRetrieval("baseline", {"s-in": [ABOVE], "s-out": [ABOVE]})
    result = _finder(instance, fake).find(
        nb, FindSourcesRequest(query="sparse", terms=("sparse",), kind="all"),
        allowed_source_ids=["s-in", "s-other"],
    )
    assert [item.source_id for item in result.items] == ["s-in"]
    assert result.filtered_scope_total == 2
    assert set(fake.calls[0][3]) == {"s-in", "s-other"}


# ------------------------------------------- cancellation / semantic failure
class CancellingStore(FailingStore):
    """Sets the run's cancel event in the middle of one library's read, then
    reads (the read budget then raises ``ReadBudgetExceeded``)."""

    def __init__(self, store, notebook_id, cancel):
        super().__init__(store, notebook_id)
        self._cancel = cancel

    def find_sources_metadata_rows(self, db, notebook_id, **kwargs):
        if notebook_id == self._nb:
            self._cancel.set()
        return super().find_sources_metadata_rows(db, notebook_id, **kwargs)


def test_a_stop_during_a_peer_read_is_a_cancellation_not_a_timeout(repo):
    import threading

    from app.services.cancellation import AskCancelled
    from app.services.retrieval_run import retrieval_run

    instance, nb, *_ = repo
    ref = _reference_library(instance)
    _source(instance, nb, "s-local", title="kalman filter")
    _source(instance, ref, "s-ref", title="kalman smoother")
    cancel = threading.Event()
    store = CancellingStore(instance._runtime.source_store, ref, cancel)
    events = Events()
    finder = _finder(instance, enumeration=TwoLibraries(instance, nb, ref), sources=store,
                     event_log=events)
    request = FindSourcesRequest(terms=("kalman",), kind="all")
    with retrieval_run(run_kind="ask_reasoning"):
        with pytest.raises(AskCancelled):
            finder.find(nb, request, cancel_event=cancel)
        # No "timeout" skip receipt for a user's own stop.
        assert events.events == []
        # Nothing was memoized: a resumed search reads both libraries again.
        cancel.clear()
        plain = FailingStore(instance._runtime.source_store, "none")
        result = _finder(instance, enumeration=TwoLibraries(instance, nb, ref),
                         sources=plain).find(nb, request, cancel_event=cancel)
    assert result.skipped_libraries == {}
    assert {item.source_id for item in result.items} == {"s-local", "s-ref"}


def test_a_cancellation_raised_inside_a_peer_read_propagates(repo):
    from app.services.cancellation import AskCancelled

    instance, nb, *_ = repo
    ref = _reference_library(instance)
    _source(instance, nb, "s-local", title="kalman filter")
    _source(instance, ref, "s-ref", title="kalman smoother")
    store = FailingStore(instance._runtime.source_store, ref, fail=AskCancelled())
    with pytest.raises(AskCancelled):
        _finder(instance, enumeration=TwoLibraries(instance, nb, ref),
                sources=store).find(nb, FindSourcesRequest(terms=("kalman",)))


class SemanticFailsFor(FakeRetrieval):
    def __init__(self, notebook_id, relevance):
        super().__init__("baseline", relevance)
        self._nb = notebook_id

    def retrieve_chunks_in_sources(self, notebook_id, *args, **kwargs):
        if notebook_id == self._nb:
            raise RuntimeError("vector store down")
        return super().retrieve_chunks_in_sources(notebook_id, *args, **kwargs)


def test_a_peer_semantic_failure_keeps_its_metadata_hits(repo):
    instance, nb, *_ = repo
    ref = _reference_library(instance)
    _source(instance, nb, "s-local", title="kalman filter")
    _meta(instance, nb, "s-local", year=2020)
    _source(instance, ref, "s-ref", title="kalman smoother")
    _meta(instance, ref, "s-ref", year=2021)
    fake = SemanticFailsFor(ref, {"s-local": [BETWEEN]})
    result = _finder(instance, fake, enumeration=TwoLibraries(instance, nb, ref)).find(
        nb, FindSourcesRequest(query="state estimation", terms=("kalman",), kind="all"))
    assert {item.source_id for item in result.items} == {"s-local", "s-ref"}
    assert result.skipped_libraries == {ref: "semantic:unavailable"}
    assert result.lexical_complete is True          # the metadata leg was complete
    assert result.semantic_recall_capped is True    # semantic recall unknown
    assert result.lanes == {nb: "baseline"}


# ------------------------------------------------- run memo freshness
def test_a_paper_meta_write_inside_the_run_re_ranks(repo):
    from app.services.retrieval_run import note_paper_meta_written, retrieval_run

    instance, nb, *_ = repo
    _source(instance, nb, "s-a", title="graph a")
    _source(instance, nb, "s-b", title="graph b")
    finder = _finder(instance, FakeRetrieval())
    request = FindSourcesRequest(terms=("graph",), year_from=2020)
    with retrieval_run(run_kind="ask_reasoning"):
        before = finder.find(nb, request)
        _meta(instance, nb, "s-b", year=2024)        # a card's lazy extraction
        unchanged = finder.find(nb, request)         # no write noted: memo holds
        note_paper_meta_written()
        after = finder.find(nb, request)
    assert [i.source_id for i in before.items] == []
    assert [i.source_id for i in unchanged.items] == []
    assert [i.source_id for i in after.items] == ["s-b"]


# ------------------------------------------------- lane flags are restored
@pytest.mark.parametrize("fails", [False, True])
def test_find_restores_the_federation_lane_flags(repo, monkeypatch, fails):
    from app.services.chunk_lane import _CHUNK_PEEK_ONLY, _CHUNK_PEER_LEG

    instance, nb, *_ = repo
    ref = _reference_library(instance)
    body = "phase locked loop jitter analysis "
    for notebook_id, sid in ((nb, "s-local"), (ref, "s-ref")):
        _source(instance, notebook_id, sid, title=f"{sid} doc",
                texts=[f"{body} {sid} part {i} " * 6 for i in range(3)])
    candidates = instance.retrieval.candidates
    monkeypatch.setattr(candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": notebook_id != ref, "size": {}})
    seen: list = []
    original = candidates._retrieve_chunks

    def spy(notebook_id, *args, **kwargs):
        seen.append((notebook_id, _CHUNK_PEEK_ONLY.get(), _CHUNK_PEER_LEG.get()))
        if fails and notebook_id == ref:
            raise ZeroDivisionError("boom")   # raised INSIDE the peer lane
        return original(notebook_id, *args, **kwargs)

    monkeypatch.setattr(candidates, "_retrieve_chunks", spy)
    defaults = (_CHUNK_PEEK_ONLY.get(), _CHUNK_PEER_LEG.get())
    finder = _finder(instance, enumeration=TwoLibraries(instance, nb, ref))
    request = FindSourcesRequest(query="phase locked loop", kind="all")
    result = finder.find(nb, request)
    assert (_CHUNK_PEEK_ONLY.get(), _CHUNK_PEER_LEG.get()) == defaults
    assert (ref, True, True) in seen              # the peer did run under the flags
    assert (nb, False, False) in seen             # and the subject did not
    if fails:
        assert result.skipped_libraries == {ref: "semantic:unavailable"}


def test_peers_without_a_run_plan_share_one_budget(repo, monkeypatch):
    """Without a run plan all peers of one call share ``timeout × 2``: each of
    three peers would fit its own budget, the third does not fit the shared
    one."""
    instance, nb, *_ = repo
    refs = tuple(_reference_library(instance, f"ref-{i}") for i in range(3))
    _source(instance, nb, "s-local", title="kalman filter")
    for index, ref in enumerate(refs):
        _source(instance, ref, f"s-ref-{index}", title="kalman smoother")
    monkeypatch.setattr(instance.settings, "global_ask_notebook_timeout_seconds", 0.3)
    store = FailingStore(instance._runtime.source_store, refs, stall=0.25)
    result = _finder(
        instance, enumeration=TwoLibraries(instance, nb, *refs), sources=store,
    ).find(nb, FindSourcesRequest(terms=("kalman",), kind="all"))
    assert {item.source_id for item in result.items} == {"s-local", "s-ref-0", "s-ref-1"}
    assert result.skipped_libraries in (
        {refs[2]: "timeout"}, {refs[2]: "queue_deadline"},
    )
