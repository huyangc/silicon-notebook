"""Real SQLite recall for personal notebooks without a scale index."""
import importlib
import json
import time
from threading import Event
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.vector_index import encode_vector
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.source_store import SourceStore
from app.services.global_retrieval import retrieve_global_candidates, GlobalRetrievalSkipped
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context
from tests.test_global_retrieval import _rig, _forbidden


@pytest.fixture
def small_library(tmp_path):
    settings = Settings(sqlite_path="small-library.db", chunk_recall=1)
    database = SqliteDatabase(settings, tmp_path)
    with database.connect() as db:
        db.executescript("""
            CREATE TABLE sources(id TEXT PRIMARY KEY,title TEXT);
            CREATE TABLE chunks(id TEXT PRIMARY KEY,notebook_id TEXT,source_id TEXT,
                text TEXT,section_path TEXT,element_ids TEXT);
            CREATE INDEX chunks_notebook ON chunks(notebook_id,id);
            CREATE TABLE chunk_embeddings(chunk_id TEXT PRIMARY KEY,notebook_id TEXT,vector BLOB);
            CREATE TABLE source_elements(id TEXT PRIMARY KEY,source_id TEXT,text TEXT);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED,notebook_id UNINDEXED,text,tokenize='trigram');
        """)
    candidates, _, cache, warm = _rig(warm=True)
    candidates.settings = settings
    candidates._connect = database.connect
    candidates.sources = SourceStore(database, now=lambda: "")
    candidates.embeddings = EmbeddingStore(write=database.connect)
    candidates.snapshots = SimpleNamespace(get=_forbidden, invalidate=_forbidden)
    candidates._chunk_fts_hits = KnowledgeStore.chunk_fts_search

    def add(nb, chunk, source, text, vector):
        with database.connect() as db:
            db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (source, "English source"))
            db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                       (chunk, nb, source, text, "", json.dumps([chunk+"-element"])))
            db.execute("INSERT INTO source_elements VALUES (?,?,?)", (chunk+"-element", source, text))
            db.execute("INSERT INTO chunk_embeddings VALUES (?,?,?)", (chunk, nb, encode_vector(vector)))
            db.execute("INSERT INTO chunks_fts VALUES (?,?,?)", (chunk, nb, text))
    yield candidates, database, add, cache, warm
    database.close_local()


def ask(candidates, notebook="personal", sources=("source",)):
    question = "电池如何工作"
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding(question, lambda: [1., 0.])
        with source_scope_context(notebook, {"mode": "include", "source_ids": list(sources)}):
            return retrieve_global_candidates(candidates, notebook, question)


def test_english_personal_library_answers_chinese_query_without_fts_matches(small_library):
    candidates, database, add, _, _ = small_library
    add("personal", "battery", "source", "Batteries store energy through reversible chemical reactions.", [1., 0.])
    add("personal", "river", "source", "Rivers flow into oceans.", [0., 1.])
    with database.connect() as db:
        assert KnowledgeStore.chunk_fts_search(db, "personal", "电池如何工作") == []
    result = ask(candidates)
    assert not result.degraded
    assert [chunk.chunk_id for chunk in result.chunks] == ["battery"]
    assert result.chunks[0].text.startswith("Batteries store energy")
    assert result.evidence_fingerprints["battery-element"][0] == "source"


def test_source_filter_precedes_semantic_top_k(small_library):
    candidates, _, add, _, _ = small_library
    add("personal", "allowed", "source", "Battery operating principles.", [.9, .1])
    add("personal", "secret", "private", "Private battery details.", [1., 0.])
    result = ask(candidates)
    assert [chunk.chunk_id for chunk in result.chunks] == ["allowed"]
    assert set(result.evidence_fingerprints) == {"allowed-element"}


def test_later_vector_page_can_replace_the_earlier_top_k(small_library, monkeypatch):
    candidates, _, add, _, _ = small_library
    monkeypatch.setattr("app.services.global_retrieval._GLOBAL_VECTOR_PAGE_SIZE", 2)
    for index, vector in enumerate(([0., 1.], [.1, .9], [1., 0.])):
        add("personal", f"chunk-{index}", "source", "Battery storage mechanisms.", vector)
    result = ask(candidates)
    assert not result.degraded
    assert [chunk.chunk_id for chunk in result.chunks] == ["chunk-2"]


def test_wrong_dimension_first_row_does_not_hide_valid_vectors(small_library, monkeypatch):
    candidates, _, add, _, _ = small_library
    add("personal", "a-invalid", "source", "Battery malformed embedding.", [1., 0., 0.])
    add("personal", "b-valid", "source", "Battery operating principles.", [1., 0.])
    # Lexical hydration also returns the malformed candidate first. The final
    # candidate matrix must preserve the same query dimension as recall.
    candidates._chunk_fts_hits = lambda *args, **kwargs: [{"chunk_id": "a-invalid"}]
    result = ask(candidates)
    assert not result.degraded
    assert [chunk.chunk_id for chunk in result.chunks] == ["b-valid"]
    assert result.ids == ["b-valid"]


def test_runtime_dimension_is_applied_before_expected_query_dimension(small_library):
    candidates, _, add, _, _ = small_library
    candidates.settings.embed_runtime_dim = 2
    add("personal", "battery", "source", "Battery energy conversion.", [1., 0., 10.])
    result = ask(candidates)
    assert not result.degraded
    assert [chunk.chunk_id for chunk in result.chunks] == ["battery"]
    assert result.matrix.shape == (1, 2)


def test_growth_beyond_size_budget_discards_partial_semantic_results(small_library, monkeypatch):
    candidates, _, add, _, _ = small_library
    candidates.settings.global_ask_small_notebook_max_chunks = 2
    monkeypatch.setattr("app.services.global_retrieval._GLOBAL_VECTOR_PAGE_SIZE", 1)
    add("personal", "chunk-a", "source", "Battery energy conversion.", [1., 0.])
    add("personal", "chunk-b", "source", "Battery storage mechanisms.", [.9, .1])
    original = candidates.embeddings.global_small_chunk_vector_page
    calls = []
    def page(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 1:
            # This test adapter returns an already materialized first page;
            # its following SQL page must re-evaluate library-size admission.
            add("personal", "chunk-c", "source", "Battery charging.", [1., 0.])
        return result
    monkeypatch.setattr(candidates.embeddings, "global_small_chunk_vector_page", page)
    result = ask(candidates)
    assert result.degraded and result.chunks == []
    assert len(calls) == 2


def test_scan_cap_does_not_publish_a_prefix_after_concurrent_replacement(small_library, monkeypatch):
    candidates, database, add, _, _ = small_library
    candidates.settings.global_ask_small_notebook_max_chunks = 2
    monkeypatch.setattr("app.services.global_retrieval._GLOBAL_VECTOR_PAGE_SIZE", 1)
    add("personal", "chunk-a", "source", "Battery energy conversion.", [1., 0.])
    add("personal", "chunk-b", "source", "Battery storage mechanisms.", [.9, .1])
    original = candidates.embeddings.global_small_chunk_vector_page
    calls = []
    def page(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 1:
            with database.connect() as db:
                db.execute("DELETE FROM chunks WHERE id='chunk-a'")
                db.execute("DELETE FROM chunk_embeddings WHERE chunk_id='chunk-a'")
            add("personal", "chunk-c", "source", "Battery charging.", [1., 0.])
        return result
    monkeypatch.setattr(candidates.embeddings, "global_small_chunk_vector_page", page)
    result = ask(candidates)
    assert result.degraded and result.chunks == []
    assert len(calls) == 3


@pytest.mark.parametrize("unavailable", ["missing", "wrong_dimension"])
def test_no_usable_vectors_remains_degraded(small_library, unavailable):
    candidates, database, add, _, _ = small_library
    add("personal", "battery", "source", "Battery energy conversion.", [1., 0., 0.])
    if unavailable == "missing":
        with database.connect() as db:
            db.execute("DELETE FROM chunk_embeddings")
    result = ask(candidates)
    assert result.degraded and result.chunks == []


def test_existing_positive_bruteforce_guard_can_tighten_the_global_budget(small_library, monkeypatch):
    candidates, _, add, _, _ = small_library
    candidates.settings.global_ask_small_notebook_max_chunks = 20000
    candidates.settings.chunk_bruteforce_max_chunks = 1
    add("personal", "battery", "source", "Battery energy conversion.", [1., 0.])
    add("personal", "river", "source", "River water flows.", [0., 1.])
    monkeypatch.setattr("app.services.global_retrieval.build_matrix", _forbidden)
    result = ask(candidates)
    assert result.degraded and result.chunks == []


def test_twenty_four_small_libraries_recall_semantically_without_shared_cache_writes(small_library, monkeypatch):
    candidates, _, add, cache, warm = small_library
    writes = []
    original_set = type(cache).__setitem__
    monkeypatch.setattr(type(cache), "__setitem__", lambda self, key, value: (
        writes.append(key), original_set(self, key, value)
    )[-1])
    for number in range(24):
        notebook = f"personal-{number}"
        add(notebook, f"battery-{number}", "source", "Battery energy conversion.", [1., 0.])
        result = ask(candidates, notebook)
        assert not result.degraded
        assert result.chunks[0].chunk_id == f"battery-{number}"
    assert writes == []
    assert cache.get("warm") is warm


@pytest.mark.parametrize("brute_guard", [0, 1, 20000])
def test_over_budget_libraries_never_decode_vectors(small_library, monkeypatch, brute_guard):
    candidates, _, add, _, _ = small_library
    candidates.settings.global_ask_small_notebook_max_chunks = 1
    candidates.settings.chunk_bruteforce_max_chunks = brute_guard
    add("personal", "battery", "source", "Battery energy conversion.", [1., 0.])
    add("personal", "river", "source", "River water flows.", [0., 1.])
    monkeypatch.setattr("app.services.global_retrieval.build_matrix", _forbidden)
    result = ask(candidates)
    assert result.degraded and result.chunks == []


def test_zero_small_library_budget_disables_vector_reads(small_library, monkeypatch):
    candidates, _, add, _, _ = small_library
    candidates.settings.global_ask_small_notebook_max_chunks = 0
    add("personal", "battery", "source", "Battery energy conversion.", [1., 0.])
    monkeypatch.setattr(candidates.embeddings, "global_small_chunk_vector_page", _forbidden)
    result = ask(candidates)
    assert result.degraded and result.chunks == []


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_vector_decode_stops_before_the_next_row_after_interruption(small_library, monkeypatch, interrupt):
    candidates, _, add, _, _ = small_library
    add("personal", "battery", "source", "Battery energy conversion.", [1., 0.])
    add("personal", "river", "source", "River water flows.", [0., 1.])
    module = importlib.import_module("app.domain.vector_index")
    budget_module = importlib.import_module("app.repositories.read_budget")
    original = module.decode_vector
    calls = []
    cancelled = Event()
    clock = [time.monotonic()]
    deadline = clock[0] + 10
    monkeypatch.setattr(budget_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def decode(raw):
        calls.append(True)
        if interrupt == "cancel":
            cancelled.set()
        else:
            clock[0] = deadline + 1
        return original(raw)
    monkeypatch.setattr(module, "decode_vector", decode)
    question = "电池如何工作"
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding(question, lambda: [1., 0.])
        with source_scope_context("personal", {"mode": "include", "source_ids": ["source"]}):
            with pytest.raises(GlobalRetrievalSkipped, match="timeout"):
                retrieve_global_candidates(candidates, "personal", question,
                                           deadline=deadline, cancel_event=cancelled)
    assert calls == [True]
