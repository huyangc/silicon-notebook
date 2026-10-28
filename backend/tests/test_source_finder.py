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

    def retrieve_chunks_in_sources(self, notebook_id, query, recall, allowed):
        self.calls.append((notebook_id, query, recall, tuple(allowed)))
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


def _finder(instance, retrieval=None) -> SourceFinder:
    runtime = instance._runtime
    return SourceFinder(
        database=runtime.database,
        sources=runtime.source_store,
        retrieval=retrieval or instance.retrieval,
        enumeration=runtime.collection_enumeration,
        settings=instance.settings,
        paper_card_version=CARD_VERSION,
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
        _source(instance, nb, sid)
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
    row = {"is_paper": True, "source_type": "document", "doc_type": "academic_paper",
           "parse_status": "extracted"}
    card = {"card_version": 2, "basis": "b1", "status": "ready"}
    assert paper_card_status(row, card, "b1", card_version=2) == "ready"
    assert paper_card_status(row, card, "b2", card_version=2) == "missing"
    assert paper_card_status(row, card, "b1", card_version=3) == "missing"
    assert paper_card_status(row, None, "b1", card_version=2) == "missing"


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
