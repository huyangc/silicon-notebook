import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.chunk_question_index import (
    ChunkQuestionIndexService,
    validate_generated_questions,
)
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievalSupport, RetrievedChunk
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.source_scope import source_scope_context
from tests.model_testkit import bind_all_embedding_clients


class _Chat:
    configured = True

    def chat_json(self, _messages, _schema_hint):
        return json.dumps({
            "questions": [
                "Which clock mode is supported?",
                "  which   clock mode is supported? ",
                "What is the ZX-81 settling time?",
            ]
        })


class _InvalidSchemaChat:
    configured = True

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, _messages, _schema_hint):
        return json.dumps(self.payload)


class _Models:
    def __init__(self):
        self.chat_client = _Chat()
        self.embedder = FakeEmbedder(dim=8)
        self.embedder.configured = True

    def chat(self, workload):
        assert workload == "chunk_question_generation"
        return self.chat_client

    def embedding(self, workload):
        assert workload == "chunk_embedding"
        return self.embedder

    @staticmethod
    def parallelism(_workload):
        return 2


class _Chunks:
    def __init__(self):
        self.replacements = []

    def question_index_chunk_page(
        self, notebook_id, *, after_id, limit, include_existing
    ):
        assert notebook_id == "nb"
        assert limit > 0
        assert include_existing is False
        if after_id:
            return []
        return [{
            "chunk_id": "chunk-1",
            "source_id": "source-1",
            "text": "ZX-81 supports holdover clock mode with 12 ns settling time.",
            "section_path": "Clock",
        }]

    def replace_chunk_questions(
        self, chunk_id, notebook_id, source_id, rows, *, created_at
    ):
        self.replacements.append(
            (chunk_id, notebook_id, source_id, list(rows), created_at)
        )

    @staticmethod
    def question_index_stats(_notebook_id):
        return {"chunks": 1, "question_chunks": 1, "questions": 2}


class _Events:
    def __init__(self):
        self.rows = []

    def emit(self, row):
        self.rows.append(row)


def test_generated_question_validation_is_bounded_and_deduplicated():
    raw = json.dumps({
        "questions": [" Alpha? ", "alpha?", "", "x" * 21, "Beta?", "Gamma?"],
    })

    assert validate_generated_questions(raw, maximum=2, max_chars=20) == [
        "Alpha?",
        "Beta?",
    ]
    assert validate_generated_questions("not-json", maximum=2, max_chars=20) == []


def test_offline_builder_stores_stable_question_rows_and_counts():
    chunks = _Chunks()
    events = _Events()
    service = ChunkQuestionIndexService(
        settings=Settings(generated_question_index_mode="shadow"),
        chunks=chunks,
        models=_Models(),
        event_log=events,
        now=lambda: "2026-08-10T00:00:00+00:00",
    )

    counts = service.build_notebook("nb", workers=9)

    assert counts == {
        "chunks_seen": 1,
        "chunks_stored": 1,
        "questions_stored": 2,
        "empty": 0,
        "skipped": 0,
        "failed": 0,
        "indexed_chunks": 1,
        "question_bearing_chunks": 1,
        "indexed_questions": 2,
    }
    stored = chunks.replacements[0]
    assert stored[:3] == ("chunk-1", "nb", "source-1")
    assert [row[1] for row in stored[3]] == [
        "Which clock mode is supported?",
        "What is the ZX-81 settling time?",
    ]
    assert all(row[0].startswith("cq-") for row in stored[3])
    assert events.rows[-1]["kind"] == "chunk_question_index_build"
    assert "notebook_id" not in events.rows[-1]


@pytest.mark.parametrize(
    "payload",
    [
        {"questions": "not-a-list"},
        {"questions": [None]},
        {"questions": [{"text": "not-a-string"}]},
    ],
)
def test_offline_builder_leaves_schema_invalid_generation_retryable(payload):
    chunks = _Chunks()
    models = _Models()
    models.chat_client = _InvalidSchemaChat(payload)
    service = ChunkQuestionIndexService(
        settings=Settings(generated_question_index_mode="shadow"),
        chunks=chunks,
        models=models,
        event_log=_Events(),
        now=lambda: "2026-08-10T00:00:00+00:00",
    )

    counts = service.build_notebook("nb", workers=1)

    assert counts["failed"] == 1
    assert counts["empty"] == 0
    assert chunks.replacements == []


def test_offline_builder_is_disabled_by_default():
    service = ChunkQuestionIndexService(
        settings=Settings(),
        chunks=_Chunks(),
        models=_Models(),
        event_log=_Events(),
        now=lambda: "2026-08-10T00:00:00+00:00",
    )

    with pytest.raises(RuntimeError, match="GENERATED_QUESTION_INDEX_MODE=off"):
        service.build_notebook("nb", workers=1)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'question.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("EMBED_DIM", "16")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _seed_original_chunk(repo):
    notebook = repo.create_notebook(NotebookCreate(name="question-index"))
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("source-1", notebook.id, "Original", "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "chunk-1",
                notebook.id,
                "source-1",
                "The original passage names a precision timing controller.",
                "Timing",
                "[]",
                now,
            ),
        )
    return notebook


def test_sqlite_index_marker_makes_empty_results_idempotent(repo):
    notebook = _seed_original_chunk(repo)
    store = repo._runtime.chunk_store

    assert [row["chunk_id"] for row in store.question_index_chunk_page(
        notebook.id, after_id="", limit=10, include_existing=False
    )] == ["chunk-1"]
    store.replace_chunk_questions(
        "chunk-1", notebook.id, "source-1", (), created_at=_now()
    )

    assert store.question_index_chunk_page(
        notebook.id, after_id="", limit=10, include_existing=False
    ) == []
    assert store.question_index_stats(notebook.id) == {
        "questions": 0,
        "chunks": 1,
        "question_chunks": 0,
    }


def test_shadow_observes_but_on_returns_only_the_original_chunk(repo, monkeypatch):
    notebook = _seed_original_chunk(repo)
    store = repo._runtime.chunk_store
    question = "How long does ZX-81 holdover acquisition take?"
    vector = repo._runtime.models.embedding("chunk_embedding").embed_texts([question])[0]
    store.replace_chunk_questions(
        "chunk-1",
        notebook.id,
        "source-1",
        (("question-1", question, vector),),
        created_at=_now(),
    )
    candidates = repo.retrieval.candidates
    baseline = ([], [], None)

    monkeypatch.setattr(repo.settings, "generated_question_index_mode", "shadow")
    assert candidates._generated_question_supplement(
        notebook.id, question, baseline
    ) is baseline

    monkeypatch.setattr(repo.settings, "generated_question_index_mode", "on")
    scored, ids, matrix = candidates._generated_question_supplement(
        notebook.id, question, baseline
    )

    assert [chunk.chunk_id for chunk in scored] == ["chunk-1"]
    assert scored[0].text == "The original passage names a precision timing controller."
    assert question not in scored[0].text
    assert {support.origin for support in scored[0].retrieval_supports} == {
        "generated_question"
    }
    assert ids == []
    assert matrix.shape[0] == 0  # no original chunk embedding was fabricated


def test_question_rows_honor_the_source_ceiling(repo):
    notebook = _seed_original_chunk(repo)
    store = repo._runtime.chunk_store
    vector = [0.25] * 16
    store.replace_chunk_questions(
        "chunk-1",
        notebook.id,
        "source-1",
        (("question-1", "Question?", vector),),
        created_at=_now(),
    )

    assert store.question_index_rows(
        notebook.id, allowed_source_ids=(), limit=10
    ) == []
    assert [row["chunk_id"] for row in store.question_index_rows(
        notebook.id, allowed_source_ids=("source-1",), limit=10
    )] == ["chunk-1"]


def test_retrieval_contribution_authority_applies_notebook_and_source_in_sql(
    repo,
):
    notebook = _seed_original_chunk(repo)
    other_notebook = repo.create_notebook(NotebookCreate(name="other"))
    now = _now()
    with repo._write() as db:
        db.executemany(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                ("source-2", notebook.id, "Excluded", "md", "ready", now, now),
                (
                    "source-other", other_notebook.id, "Other", "md", "ready",
                    now, now,
                ),
            ),
        )
        db.executemany(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                ("chunk-2", notebook.id, "source-2", "excluded", "", "[]", now),
                (
                    "chunk-other", other_notebook.id, "source-other", "other",
                    "", "[]", now,
                ),
            ),
        )

    with source_scope_context(
        notebook.id,
        {"mode": "include", "source_ids": ["source-1"], "narrowed": True},
    ):
        rows = repo.retrieval.hydrate_retrieval_contribution_chunks(
            notebook.id, ("chunk-1", "chunk-2", "chunk-other")
        )

    assert [chunk.chunk_id for chunk in rows] == ["chunk-1"]
    assert rows[0].notebook_id == notebook.id


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_question_index_store_failure_returns_exact_baseline(repo, monkeypatch, mode):
    candidates = repo.retrieval.candidates
    original = RetrievedChunk(
        chunk_id="baseline",
        source_id="source-1",
        source_title="Original",
        section_path="",
        text="baseline evidence",
        retrieval_supports=(
            RetrievalSupport("semantic", "chunk", "baseline", 0.8),
        ),
    )
    baseline = ([original], ["baseline"], None)
    monkeypatch.setattr(repo.settings, "generated_question_index_mode", mode)
    monkeypatch.setattr(repo.settings, "generated_question_trigger_hits", 2)
    monkeypatch.setattr(
        candidates.chunks,
        "question_index_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    result = candidates._generated_question_supplement("nb", "question", baseline)

    assert result is baseline
    assert original.retrieval_supports == (
        RetrievalSupport("semantic", "chunk", "baseline", 0.8),
    )


def test_question_index_hydration_failure_does_not_mutate_baseline(
    repo, monkeypatch
):
    notebook = _seed_original_chunk(repo)
    candidates = repo.retrieval.candidates
    store = repo._runtime.chunk_store
    question = "How long does ZX-81 holdover acquisition take?"
    vector = repo._runtime.models.embedding("chunk_embedding").embed_texts([question])[0]
    store.replace_chunk_questions(
        "chunk-1",
        notebook.id,
        "source-1",
        (("question-1", question, vector),),
        created_at=_now(),
    )
    original = RetrievedChunk(
        chunk_id="chunk-1",
        source_id="source-1",
        source_title="Original",
        section_path="Timing",
        text="baseline evidence",
        retrieval_supports=(
            RetrievalSupport("semantic", "chunk", "chunk-1", 0.8),
        ),
    )
    baseline = ([original], ["chunk-1"], None)
    calls = 0
    real_hydrate = candidates._hydrate_chunk_candidates

    def fail_second_hydrate(chunk_ids):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("hydrate failed")
        return real_hydrate(chunk_ids)

    monkeypatch.setattr(repo.settings, "generated_question_index_mode", "on")
    monkeypatch.setattr(repo.settings, "generated_question_trigger_hits", 2)
    monkeypatch.setattr(candidates, "_hydrate_chunk_candidates", fail_second_hydrate)

    result = candidates._generated_question_supplement(
        notebook.id, question, baseline
    )

    assert result is baseline
    assert [support.origin for support in original.retrieval_supports] == ["semantic"]


def test_question_index_event_failure_is_ignored(repo, monkeypatch):
    candidates = repo.retrieval.candidates
    baseline = ([], [], None)
    monkeypatch.setattr(repo.settings, "generated_question_index_mode", "shadow")
    monkeypatch.setattr(
        candidates.chunks, "question_index_rows", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        candidates.event_log,
        "emit",
        lambda _event: (_ for _ in ()).throw(RuntimeError("telemetry failed")),
    )

    assert candidates._generated_question_supplement(
        "nb", "question", baseline
    ) is baseline


def test_question_index_query_event_is_content_free(repo, monkeypatch):
    candidates = repo.retrieval.candidates
    events = []
    monkeypatch.setattr(candidates.event_log, "emit", events.append)

    candidates._emit_generated_question_query_event({
        "kind": "chunk_question_index_query",
        "notebook_id": "nb-secret",
        "source_id": "source-secret",
        "query": "user-authored secret",
        "mode": "shadow",
        "status": "evaluated",
        "baseline_hits": 2,
        "question_rows": 3,
        "matched_chunks": 1,
        "added_chunks": 1,
    })

    assert events == [{
        "kind": "chunk_question_index_query",
        "mode": "shadow",
        "status": "evaluated",
        "baseline_hits": 2,
        "question_rows": 3,
        "matched_chunks": 1,
        "added_chunks": 1,
    }]


def test_bounded_element_retrieval_spends_capacity_on_baseline_first(
    repo, monkeypatch
):
    candidates = repo.retrieval.candidates
    rows = {
        "element-shared": {
            "element_id": "element-shared",
            "source_id": "source-baseline",
            "location_label": "p1",
            "element_type": "paragraph",
            "text": "shared baseline evidence",
        },
        "element-unique": {
            "element_id": "element-unique",
            "source_id": "source-supplement",
            "location_label": "p2",
            "element_type": "paragraph",
            "text": "shared unique supplemental evidence",
        },
    }
    monkeypatch.setattr(
        candidates.sources,
        "evidence_elements",
        lambda element_ids: {
            element_id: rows[element_id] for element_id in element_ids
        },
    )
    monkeypatch.setattr(
        candidates.sources,
        "source_metadata",
        lambda source_ids: {
            source_id: {"title": source_id} for source_id in source_ids
        },
    )
    baseline = RetrievedChunk(
        chunk_id="chunk-baseline",
        source_id="source-baseline",
        source_title="Baseline",
        section_path="",
        text="baseline",
        element_ids=["element-shared"],
        relevance=0.2,
        retrieval_supports=(
            RetrievalSupport("semantic", "chunk", "chunk-baseline", 0.2),
        ),
    )
    supplement = RetrievedChunk(
        chunk_id="chunk-supplement",
        source_id="source-supplement",
        source_title="Supplement",
        section_path="",
        text="supplement",
        element_ids=["element-shared", "element-unique"],
        relevance=0.99,
        retrieval_supports=(
            RetrievalSupport(
                "generated_question", "chunk", "chunk-supplement", 0.99
            ),
        ),
    )

    elements = candidates._retrieve_elements_from_chunks(
        "shared", [supplement, baseline], limit=2
    )

    assert [item.element_id for item in elements] == [
        "element-shared",
        "element-unique",
    ]


def test_bounded_element_retrieval_shares_one_hydration_cap(repo, monkeypatch):
    candidates = repo.retrieval.candidates
    hydrated_batches = []
    monkeypatch.setattr(
        candidates.sources,
        "evidence_elements",
        lambda element_ids: hydrated_batches.append(list(element_ids)) or {},
    )
    monkeypatch.setattr(
        candidates.sources, "source_metadata", lambda _source_ids: {}
    )
    baseline = RetrievedChunk(
        chunk_id="chunk-baseline",
        source_id="source-baseline",
        source_title="Baseline",
        section_path="",
        text="baseline",
        element_ids=[f"element-baseline-{index}" for index in range(64)],
        relevance=0.2,
        retrieval_supports=(
            RetrievalSupport("semantic", "chunk", "chunk-baseline", 0.2),
        ),
    )
    supplement = RetrievedChunk(
        chunk_id="chunk-supplement",
        source_id="source-supplement",
        source_title="Supplement",
        section_path="",
        text="supplement",
        element_ids=[f"element-supplement-{index}" for index in range(64)],
        relevance=0.99,
        retrieval_supports=(
            RetrievalSupport(
                "generated_question", "chunk", "chunk-supplement", 0.99
            ),
        ),
    )

    assert candidates._retrieve_elements_from_chunks(
        "shared", [supplement, baseline], limit=8
    ) == []
    assert sum(len(batch) for batch in hydrated_batches) == 64
