from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.schemas import AskResponse, MemoryUpdate, NotebookCreate
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def users(repo):
    return SimpleNamespace(
        alice=repo.create_user("a00123456", "pw"),
        bob=repo.create_user("b00654321", "pw"),
    )


@pytest.fixture
def notebook(repo, users):
    token = set_request_user(users.alice)
    try:
        item = repo.create_notebook(NotebookCreate(name="Memory notebook"))
    finally:
        reset_request_user(token)
    with repo._write() as db:
        db.executemany(
            "INSERT INTO agent_profiles "
            "(id,owner_id,name,description,status,created_at,updated_at) "
            "VALUES (?,?,?,'','active','t','t')",
            [
                ("agent-a", users.alice.id, "Agent A"),
                ("agent-b", users.alice.id, "Agent B"),
                ("agent-bob", users.bob.id, "Agent Bob"),
            ],
        )
    return item


@pytest.fixture
def memory_service(repo):
    return repo._runtime.memory_service


@pytest.fixture
def saved_answer(repo, notebook, users):
    answer_id = repo._runtime.ask_state.save_answer(
        notebook.id,
        None,
        "What is stable?",
        AskResponse(conclusion="Grounded answer", answer="Grounded answer"),
        users.alice.id,
    )
    return SimpleNamespace(id=answer_id, notebook_id=notebook.id)


def _candidate(service, notebook, user, agent, request, title="Title", body="Body"):
    return service.create_candidate(
        notebook.id,
        user.id,
        agent,
        request,
        title,
        body,
        ["analog"],
        "task",
        {"goal": "review"},
        [{"source_id": "source-1"}],
    )


def test_agent_candidate_is_private_to_owner_but_shared_across_owner_profiles(
    memory_service, users, notebook
):
    first = _candidate(memory_service, notebook, users.alice, "agent-a", "req-1")
    second = _candidate(memory_service, notebook, users.alice, "agent-b", "req-2")

    assert memory_service.get(first.id, users.alice.id).id == first.id
    assert {item.id for item in memory_service.list_memories(users.alice.id).items} == {
        first.id,
        second.id,
    }
    with pytest.raises(KeyError):
        memory_service.get(first.id, users.bob.id)
    with pytest.raises(PermissionError):
        _candidate(memory_service, notebook, users.alice, "agent-bob", "req-3")


def test_candidate_provenance_snapshots_agent_and_validates_every_evidence_ref(
    repo, memory_service, users, notebook
):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES ('source-live',?,'Live source','markdown','extracted','parsed',"
            "'live.md','',0,'','','note','t','t')",
            (notebook.id,),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES ('element-live','source-live','paragraph','p1','Evidence','{}','t')"
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,payload,status,source_id,evidence,created_at,updated_at) "
            "VALUES ('knowledge-live',?,'claim','{\"name\":\"Live claim\"}',"
            "'approved','source-live','[]','t','t')",
            (notebook.id,),
        )

    prior = memory_service.create_candidate(
        notebook.id, users.alice.id, "agent-a", "req-prior",
        "Prior", "Prior body", [], "reason", {}, [],
    )
    prior = memory_service.confirm(prior.id, users.alice.id)
    created = memory_service.create_candidate(
        notebook.id,
        users.alice.id,
        "agent-a",
        "safe-request-42",
        "  Candidate title  ",
        "  Candidate body  ",
        [" analog ", "analog"],
        "  reusable finding  ",
        {"task": "review"},
        [
            {"source_id": "source-live", "element_id": "element-live"},
            {"knowledge_id": "knowledge-live"},
            {"memory_id": prior.id},
            {"source_id": "missing-source"},
            {"kind": "url", "url": "https://example.invalid"},
        ],
    )

    assert created.title == "Candidate title"
    assert created.content_md == "Candidate body"
    assert created.tags == ["analog"]
    assert created.provenance["agent_profile"] == {
        "id": "agent-a",
        "name": "Agent A",
    }
    assert created.provenance["client_request_id"] == "safe-request-42"
    assert "token" not in created.provenance
    refs = created.provenance["evidence_refs"]
    assert len(refs) == 5
    assert [ref["validation"]["status"] for ref in refs] == [
        "validated", "validated", "validated", "invalid", "invalid"
    ]
    assert [ref["trusted"] for ref in refs] == [True, True, True, False, False]
    assert refs[0]["type"] == "source_element"
    assert refs[1]["type"] == "knowledge"
    assert refs[2]["type"] == "memory"
    assert refs[3]["validation"]["reason"] == "missing_or_cross_notebook"
    assert refs[4]["validation"]["reason"] == "unsupported_reference"


def test_candidate_evidence_rejects_cross_notebook_and_cross_owner_records(
    repo, memory_service, users, notebook
):
    token = set_request_user(users.bob)
    try:
        other = repo.create_notebook(NotebookCreate(name="Foreign evidence"))
    finally:
        reset_request_user(token)
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES ('source-foreign',?,'Foreign','markdown','extracted','parsed',"
            "'foreign.md','',0,'','','note','t','t')",
            (other.id,),
        )
    memory_service.notebooks.add_member(other.id, users.alice.id)
    foreign_memory = memory_service.create_candidate(
        other.id, users.bob.id, None, "foreign-memory", "Foreign", "Body", [],
        "reason", {}, [],
    )
    foreign_memory = memory_service.confirm(foreign_memory.id, users.bob.id)

    item = memory_service.create_candidate(
        notebook.id, users.alice.id, "agent-a", "cross-evidence", "Title", "Body",
        [], "reason", {}, [
            {"source_id": "source-foreign"},
            {"memory_id": foreign_memory.id},
        ],
    )

    assert [ref["trusted"] for ref in item.provenance["evidence_refs"]] == [False, False]
    assert item.provenance["evidence_refs"][1]["validation"]["reason"] == "missing_or_cross_owner"


def test_memory_service_enforces_shared_input_limits_and_normalization(
    memory_service, users, notebook
):
    from app.core.memory_inputs import (
        MEMORY_CONTENT_MAX_CHARS,
        MEMORY_EVIDENCE_MAX_COUNT,
        MEMORY_EVIDENCE_MAX_SERIALIZED_BYTES,
        MEMORY_REASON_MAX_CHARS,
        MEMORY_TAG_MAX_COUNT,
        MEMORY_TASK_CONTEXT_MAX_SERIALIZED_BYTES,
        MEMORY_TITLE_MAX_CHARS,
    )

    bad_calls = [
        {"title": " ", "content_md": "Body"},
        {"title": "T" * (MEMORY_TITLE_MAX_CHARS + 1), "content_md": "Body"},
        {"title": "Title", "content_md": "X" * (MEMORY_CONTENT_MAX_CHARS + 1)},
        {"title": "Title", "content_md": "Body", "tags": [f"x{i}" for i in range(MEMORY_TAG_MAX_COUNT + 1)]},
        {"title": "Title", "content_md": "Body", "reason": "r" * (MEMORY_REASON_MAX_CHARS + 1)},
        {"title": "Title", "content_md": "Body", "evidence_refs": [{}] * (MEMORY_EVIDENCE_MAX_COUNT + 1)},
        {"title": "Title", "content_md": "Body", "task_context": {"text": "x" * MEMORY_TASK_CONTEXT_MAX_SERIALIZED_BYTES}},
        {"title": "Title", "content_md": "Body", "evidence_refs": [{"source_id": "x" * MEMORY_EVIDENCE_MAX_SERIALIZED_BYTES}]},
    ]
    for index, values in enumerate(bad_calls):
        with pytest.raises(ValueError):
            memory_service.create_candidate(
                notebook.id,
                users.alice.id,
                "agent-a",
                f"limit-{index}",
                values.get("title", "Title"),
                values.get("content_md", "Body"),
                values.get("tags", []),
                values.get("reason", "reason"),
                values.get("task_context", {}),
                values.get("evidence_refs", []),
            )
    with pytest.raises(ValueError):
        memory_service.create_candidate(
            notebook.id, users.alice.id, "agent-a", "bad-json", "Title", "Body",
            [], "reason", {"not_json": object()}, [],
        )
    assert memory_service.list_memories(users.alice.id).owner_total_count == 0

    valid = memory_service.create_candidate(
        notebook.id, users.alice.id, "agent-a", "valid-after-limits", "Title",
        "Body", [], "reason", {}, [],
    )
    with pytest.raises(ValueError):
        memory_service.update(valid.id, users.alice.id, {"content_md": "   "})
    assert memory_service.get(valid.id, users.alice.id).content_md == "Body"


def test_shared_tag_normalizer_caps_raw_input_and_rejects_blanks_before_dedup(
    memory_service, users, notebook
):
    from app.core.memory_inputs import MEMORY_TAG_MAX_COUNT, normalize_tags

    with pytest.raises(ValueError, match="at most 20"):
        normalize_tags(["duplicate"] * (MEMORY_TAG_MAX_COUNT + 1))
    with pytest.raises(ValueError, match="blank"):
        normalize_tags(["valid", "  "])

    raw = [" analog ", "analog"] + [f"tag-{index}" for index in range(18)]
    normalized = normalize_tags(raw)
    assert len(raw) == MEMORY_TAG_MAX_COUNT
    assert normalized == ["analog", *[f"tag-{index}" for index in range(18)]]

    accepted = memory_service.create_candidate(
        notebook.id,
        users.alice.id,
        "agent-a",
        "raw-tag-cap-valid",
        "Valid tags",
        "Twenty raw tags are accepted",
        raw,
        "regression",
        {},
        [],
    )
    assert accepted.tags == normalized
    for request_id, tags in (
        ("raw-tag-cap-duplicate-overflow", ["duplicate"] * 21),
        ("raw-tag-cap-blank", ["valid", " "]),
    ):
        with pytest.raises(ValueError):
            memory_service.create_candidate(
                notebook.id,
                users.alice.id,
                "agent-a",
                request_id,
                "Invalid tags",
                "Must not persist",
                tags,
                "regression",
                {},
                [],
            )
    assert memory_service.list_memories(users.alice.id).owner_total_count == 1


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["task_context", "evidence_refs"])
def test_memory_service_rejects_nested_nonfinite_json_without_persisting(
    repo, memory_service, users, notebook, nonfinite, field
):
    task_context = {"optional": None, "nested": [{"score": nonfinite}]}
    evidence_refs = [{"source_id": "missing", "score": nonfinite}]
    if field == "task_context":
        evidence_refs = [{"source_id": "missing", "score": None}]
    else:
        task_context = {"optional": None, "nested": [{"score": None}]}

    with pytest.raises(ValueError, match="JSON serializable|non-finite"):
        memory_service.create_candidate(
            notebook.id,
            users.alice.id,
            "agent-a",
            f"nonfinite-{field}-{nonfinite!r}",
            "Nonfinite",
            "Must not persist",
            [],
            "regression",
            task_context,
            evidence_refs,
        )

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS count FROM memory_items WHERE created_by=?",
            (users.alice.id,),
        ).fetchone()["count"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS count FROM memory_provenance"
        ).fetchone()["count"] == 0
    assert memory_service.list_memories(users.alice.id).items == []


def test_memory_service_nested_json_null_roundtrips_through_sqlite(
    memory_service, users, notebook
):
    created = memory_service.create_candidate(
        notebook.id,
        users.alice.id,
        "agent-a",
        "nested-null-core",
        "Nested null",
        "Null is legitimate JSON",
        [],
        "regression",
        {"optional": None, "nested": [{"value": None}]},
        [{"source_id": "missing", "score": None}],
    )

    hydrated = memory_service.get(created.id, users.alice.id)
    assert hydrated.provenance["task_context"] == {
        "optional": None,
        "nested": [{"value": None}],
    }
    assert hydrated.provenance["evidence_refs"][0]["trusted"] is False
    assert memory_service.list_memories(users.alice.id).items[0].id == created.id


def test_duplicate_agent_request_is_idempotent(
    memory_service, users, notebook, monkeypatch
):
    first = _candidate(memory_service, notebook, users.alice, "agent-a", "req-same")
    # Simulate two callers whose service-level preflight both missed.  The
    # serialized store write is the final idempotency boundary.
    monkeypatch.setattr(
        memory_service.store, "memory_by_agent_request", lambda *args: None
    )
    second = _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-a",
        "req-same",
        title="Changed",
        body="Changed body",
    )

    assert second.id == first.id
    assert second.title == "Title"


def test_agent_request_idempotency_is_scoped_to_notebook(
    repo, memory_service, users, notebook
):
    token = set_request_user(users.alice)
    try:
        other = repo.create_notebook(NotebookCreate(name="Other memory notebook"))
    finally:
        reset_request_user(token)

    first = _candidate(
        memory_service, notebook, users.alice, "agent-a", "req-cross-notebook"
    )
    second = _candidate(
        memory_service, other, users.alice, "agent-a", "req-cross-notebook"
    )
    duplicate = _candidate(
        memory_service,
        other,
        users.alice,
        "agent-a",
        "req-cross-notebook",
        title="Ignored duplicate",
    )

    assert first.id != second.id
    assert first.notebook_id == notebook.id
    assert second.notebook_id == other.id
    assert duplicate.id == second.id
    assert duplicate.title == "Title"


def test_confirm_writes_revision_and_duplicate_answer_is_idempotent(
    memory_service, saved_answer, users, repo
):
    # This test asserts the durable embedding result, so do not race the
    # production background scheduler. Other tests cover queued/stale jobs.
    memory_service.embedding_scheduler = lambda fn, job: fn(job)
    first = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "T",
        "B",
        [],
    )
    second = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "T2",
        "B2",
        [],
    )

    assert second.id == first.id
    assert second.title == "T"
    assert memory_service.revisions(first.id, users.alice.id)[0].status == "confirmed"
    assert memory_service.get(first.id, users.alice.id).embedding_status == "ready"
    with repo._connect() as db:
        embedded = db.execute(
            "SELECT model, dimension, length(vector) AS size FROM memory_embeddings "
            "WHERE memory_id=?",
            (first.id,),
        ).fetchone()
    assert embedded is not None
    assert embedded["dimension"] == repo._runtime.models.embedding("retrieval_query_embedding").dim
    assert embedded["size"] == repo._runtime.models.embedding("retrieval_query_embedding").dim * 4


def test_stale_embedding_job_cannot_overwrite_newer_memory_version(
    memory_service, saved_answer, users, repo
):
    scheduled = []
    memory_service.embedding_scheduler = lambda fn, job: scheduled.append((fn, job))

    class ContentEmbedder:
        dim = 2

        def embed_texts(self, texts):
            return [[1.0, 0.0] if "Old body" in texts[0] else [0.0, 1.0]]

    memory_service.embedder = ContentEmbedder()
    created = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "Versioned",
        "Old body",
        [],
    )
    memory_service.update(
        created.id, users.alice.id, MemoryUpdate(content_md="New body")
    )
    assert len(scheduled) == 2

    newest_fn, newest_job = scheduled[1]
    oldest_fn, oldest_job = scheduled[0]
    newest_fn(newest_job)
    oldest_fn(oldest_job)

    from app.services.vector_index import decode_vector

    with repo._connect() as db:
        row = db.execute(
            "SELECT vector FROM memory_embeddings WHERE memory_id=?", (created.id,)
        ).fetchone()
    assert decode_vector(row["vector"]).tolist() == [0.0, 1.0]
    current = memory_service.get(created.id, users.alice.id)
    assert current.embedding_status == "ready"
    assert current.content_md == "New body"


def test_stale_embedding_error_cannot_mark_newer_memory_failed(
    memory_service, saved_answer, users
):
    scheduled = []
    memory_service.embedding_scheduler = lambda fn, job: scheduled.append((fn, job))

    class OldFailureEmbedder:
        dim = 2

        def embed_texts(self, texts):
            if "Old body" in texts[0]:
                raise RuntimeError("old job failed")
            return [[0.0, 1.0]]

    memory_service.embedder = OldFailureEmbedder()
    created = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "Versioned",
        "Old body",
        [],
    )
    memory_service.update(
        created.id, users.alice.id, MemoryUpdate(content_md="New body")
    )

    scheduled[1][0](scheduled[1][1])
    scheduled[0][0](scheduled[0][1])

    current = memory_service.get(created.id, users.alice.id)
    assert current.embedding_status == "ready"
    assert current.embedding_error == ""


def test_embedding_completion_stays_ready_when_notebook_access_is_revoked(
    repo, memory_service, users
):
    token = set_request_user(users.bob)
    try:
        shared = repo.create_notebook(NotebookCreate(name="Embedding revoke"))
    finally:
        reset_request_user(token)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,'reader','t')",
            (shared.id, users.alice.id),
        )
    answer_id = repo._runtime.ask_state.save_answer(
        shared.id,
        None,
        "Question",
        AskResponse(conclusion="Answer", answer="Answer"),
        users.bob.id,
    )
    scheduled = []
    memory_service.embedding_scheduler = lambda fn, job: scheduled.append((fn, job))

    class RevokeEmbedder:
        dim = 2

        def embed_texts(self, texts):
            return [[0.25, 0.75]]

    memory_service.embedder = RevokeEmbedder()
    created = memory_service.create_from_answer(
        shared.id, users.alice.id, answer_id, "Remember", "Body", []
    )
    with repo._write() as db:
        db.execute(
            "DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
            (shared.id, users.alice.id),
        )

    scheduled[0][0](scheduled[0][1])
    with repo._connect() as db:
        row = db.execute(
            "SELECT m.embedding_status,m.embedding_error,e.vector "
            "FROM memory_items m LEFT JOIN memory_embeddings e ON e.memory_id=m.id "
            "WHERE m.id=?",
            (created.id,),
        ).fetchone()
    assert row["embedding_status"] == "ready"
    assert row["embedding_error"] == ""
    assert row["vector"] is not None

    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,'reader','t')",
            (shared.id, users.alice.id),
        )
    assert memory_service.get(created.id, users.alice.id).embedding_status == "ready"


def test_candidate_lifecycle_updates_snapshots_and_rejects_invalid_transition(
    memory_service, users, notebook
):
    item = _candidate(memory_service, notebook, users.alice, "agent-a", "req-life")
    updated = memory_service.update(
        item.id,
        users.alice.id,
        MemoryUpdate(title="Edited", tags=["checked"]),
    )
    confirmed = memory_service.confirm(
        item.id,
        users.alice.id,
        MemoryUpdate(content_md="Confirmed body"),
    )
    deprecated = memory_service.deprecate(item.id, users.alice.id)

    assert updated.title == "Edited"
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_by == users.alice.id
    assert deprecated.status == "deprecated"
    assert [revision.status for revision in memory_service.revisions(item.id, users.alice.id)] == [
        "candidate",
        "candidate",
        "confirmed",
        "deprecated",
    ]
    with pytest.raises(ValueError):
        memory_service.confirm(
            item.id, users.alice.id, MemoryUpdate(title="must not be saved")
        )
    assert memory_service.get(item.id, users.alice.id).title == "Edited"


class _RevisionInsertFailure:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO memory_revisions" in sql:
            raise RuntimeError("injected revision failure")
        return self.connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _ProvenanceInsertFailure:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO memory_provenance" in sql:
            from sqlite3 import IntegrityError

            raise IntegrityError("injected provenance failure")
        return self.connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_revision_failure_rolls_back_confirm_patch_and_status(
    memory_service, users, notebook, repo, monkeypatch
):
    item = _candidate(
        memory_service, notebook, users.alice, "agent-a", "req-rollback"
    )
    real_write = repo._runtime.database.write

    @contextmanager
    def failing_write():
        with real_write() as db:
            yield _RevisionInsertFailure(db)

    monkeypatch.setattr(repo._runtime.database, "write", failing_write)
    with pytest.raises(RuntimeError, match="injected revision failure"):
        memory_service.confirm(
            item.id,
            users.alice.id,
            MemoryUpdate(title="must roll back", content_md="must roll back"),
        )

    current = memory_service.get(item.id, users.alice.id)
    assert current.status == "candidate"
    assert current.title == "Title"
    assert current.content_md == "Body"


def test_initial_revision_failure_rolls_back_agent_candidate_and_retry_is_idempotent(
    memory_service, users, notebook, repo, monkeypatch
):
    real_write = repo._runtime.database.write

    @contextmanager
    def failing_write():
        with real_write() as db:
            yield _RevisionInsertFailure(db)

    monkeypatch.setattr(repo._runtime.database, "write", failing_write)
    with pytest.raises(RuntimeError, match="injected revision failure"):
        _candidate(
            memory_service,
            notebook,
            users.alice,
            "agent-a",
            "req-create-rollback",
        )

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM memory_items WHERE created_by=? AND notebook_id=?",
            (users.alice.id, notebook.id),
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_provenance").fetchone()[0] == 0

    monkeypatch.setattr(repo._runtime.database, "write", real_write)
    retry = _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-a",
        "req-create-rollback",
    )
    duplicate = _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-a",
        "req-create-rollback",
        title="ignored retry",
    )
    assert duplicate.id == retry.id
    assert len(memory_service.revisions(retry.id, users.alice.id)) == 1


def test_initial_revision_failure_rolls_back_answer_memory_and_retry_is_idempotent(
    memory_service, saved_answer, users, repo, monkeypatch
):
    real_write = repo._runtime.database.write

    @contextmanager
    def failing_write():
        with real_write() as db:
            yield _RevisionInsertFailure(db)

    monkeypatch.setattr(repo._runtime.database, "write", failing_write)
    with pytest.raises(RuntimeError, match="injected revision failure"):
        memory_service.create_from_answer(
            saved_answer.notebook_id,
            users.alice.id,
            saved_answer.id,
            "Answer memory",
            "Answer body",
            [],
        )

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM memory_items WHERE created_by=? AND source_answer_id=?",
            (users.alice.id, saved_answer.id),
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_provenance").fetchone()[0] == 0

    monkeypatch.setattr(repo._runtime.database, "write", real_write)
    retry = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "Answer memory",
        "Answer body",
        [],
    )
    duplicate = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "ignored retry",
        "ignored retry",
        [],
    )
    assert duplicate.id == retry.id
    assert len(memory_service.revisions(retry.id, users.alice.id)) == 1


def test_provenance_failure_rolls_back_answer_memory(
    memory_service, saved_answer, users, repo, monkeypatch
):
    from sqlite3 import IntegrityError

    real_write = repo._runtime.database.write

    @contextmanager
    def failing_write():
        with real_write() as db:
            yield _ProvenanceInsertFailure(db)

    monkeypatch.setattr(repo._runtime.database, "write", failing_write)
    with pytest.raises(IntegrityError, match="injected provenance failure"):
        memory_service.create_from_answer(
            saved_answer.notebook_id,
            users.alice.id,
            saved_answer.id,
            "Answer memory",
            "Answer body",
            [],
        )

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM memory_items WHERE created_by=? AND source_answer_id=?",
            (users.alice.id, saved_answer.id),
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_provenance").fetchone()[0] == 0


def test_confirm_patch_cannot_land_after_expected_state_is_rejected(
    memory_service, users, notebook
):
    item = _candidate(
        memory_service, notebook, users.alice, "agent-a", "req-rejected-race"
    )
    memory_service.reject(item.id, users.alice.id)

    with pytest.raises(ValueError, match="rejected -> confirmed"):
        memory_service.confirm(
            item.id,
            users.alice.id,
            MemoryUpdate(title="must not land"),
        )

    current = memory_service.get(item.id, users.alice.id)
    assert current.status == "rejected"
    assert current.title == "Title"


def test_reject_is_owner_scoped_and_list_filters_search_and_paginates(
    memory_service, users, notebook
):
    first = _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-a",
        "req-filter-1",
        title="环路稳定性",
        body="phase margin",
    )
    _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-b",
        "req-filter-2",
        title="Noise",
        body="thermal noise",
    )
    rejected = memory_service.reject(first.id, users.alice.id)

    page = memory_service.list_memories(
        users.alice.id,
        notebook_id=notebook.id,
        status="rejected",
        origin="external_agent",
        query="稳定性",
        offset=0,
        limit=1,
    )
    assert rejected.status == "rejected"
    assert page.total_count == 1
    assert [item.id for item in page.items] == [first.id]
    assert memory_service.list_memories(users.bob.id).total_count == 0
    with pytest.raises(KeyError):
        memory_service.reject(first.id, users.bob.id)


def test_global_memory_access_filter_uses_fixed_query_count(
    repo, memory_service, users, notebook
):
    token = set_request_user(users.bob)
    try:
        shared = repo.create_notebook(NotebookCreate(name="Revoked shared notebook"))
    finally:
        reset_request_user(token)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,'reader','t')",
            (shared.id, users.alice.id),
        )

    accessible = _candidate(
        memory_service, notebook, users.alice, "agent-a", "req-accessible"
    )
    _candidate(memory_service, shared, users.alice, "agent-a", "req-revoked")
    with repo._write() as db:
        db.execute(
            "DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
            (shared.id, users.alice.id),
        )

    statements = []
    real_connect = repo._runtime.database.connect

    @contextmanager
    def traced_connect():
        with real_connect() as db:
            db.set_trace_callback(statements.append)
            yield db
            db.set_trace_callback(None)

    repo._runtime.database.connect = traced_connect
    try:
        page = memory_service.list_memories(users.alice.id)
    finally:
        repo._runtime.database.connect = real_connect

    memory_queries = [
        sql for sql in statements if sql.startswith("SELECT") and "memory_items m" in sql
    ]
    per_notebook_queries = [
        sql for sql in statements if sql.startswith("SELECT created_by FROM notebooks")
    ]
    assert [item.id for item in page.items] == [accessible.id]
    assert page.total_count == 1
    # Two filtered page queries plus one owner-wide aggregate and one bounded
    # grouped notebook-option query; this remains fixed as notebook count grows.
    assert len(memory_queries) == 4
    assert all("EXISTS (SELECT 1 FROM notebooks access_nb" in sql for sql in memory_queries)
    assert per_notebook_queries == []


def test_global_memory_totals_and_notebook_options_ignore_current_filters(
    repo, memory_service, users, notebook
):
    token = set_request_user(users.alice)
    try:
        other = repo.create_notebook(NotebookCreate(name="Other Memory notebook"))
    finally:
        reset_request_user(token)
    first = _candidate(memory_service, notebook, users.alice, "agent-a", "stats-1")
    memory_service.confirm(first.id, users.alice.id)
    _candidate(memory_service, notebook, users.alice, "agent-a", "stats-2")
    _candidate(memory_service, other, users.alice, None, "stats-3")

    page = memory_service.list_memories(
        users.alice.id,
        notebook_id=notebook.id,
        status="candidate",
        query="Title",
    )

    assert page.total_count == 1
    assert page.owner_total_count == 3
    assert page.owner_pending_count == 2
    assert {
        option.notebook_id: (option.name, option.memory_count, option.pending_count)
        for option in page.notebook_options
    } == {
        notebook.id: (notebook.name, 2, 1),
        other.id: (other.name, 1, 1),
    }


def test_answer_save_rechecks_live_membership_inside_atomic_store_write(
    repo, memory_service, users, monkeypatch
):
    token = set_request_user(users.bob)
    try:
        shared = repo.create_notebook(NotebookCreate(name="Concurrent revoke"))
    finally:
        reset_request_user(token)
    memory_service.notebooks.add_member(shared.id, users.alice.id)
    answer_id = repo._runtime.ask_state.save_answer(
        shared.id,
        None,
        "Shared answer?",
        AskResponse(conclusion="Shared", answer="Shared"),
        users.bob.id,
    )
    store = repo._runtime.memory_store
    original = store.create_answer_with_initial_revision

    def revoke_before_atomic_create(write, changed_by, reason):
        memory_service.notebooks.remove_member(shared.id, users.alice.id)
        return original(write, changed_by, reason)

    monkeypatch.setattr(
        store, "create_answer_with_initial_revision", revoke_before_atomic_create
    )
    with pytest.raises((KeyError, PermissionError)):
        memory_service.create_from_answer(
            shared.id, users.alice.id, answer_id, "Saved", "Body", []
        )

    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_provenance").fetchone()[0] == 0


def test_legacy_store_mutations_do_not_land_after_membership_revocation(
    repo, memory_service, users, notebook
):
    token = set_request_user(users.bob)
    try:
        shared = repo.create_notebook(NotebookCreate(name="Legacy mutation revoke"))
    finally:
        reset_request_user(token)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,'reader','t')",
            (shared.id, users.alice.id),
        )
    update_item = _candidate(
        memory_service, shared, users.alice, "agent-a", "req-legacy-update"
    )
    transition_item = _candidate(
        memory_service, shared, users.alice, "agent-a", "req-legacy-transition"
    )
    with repo._write() as db:
        db.execute(
            "DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
            (shared.id, users.alice.id),
        )

    store = repo._runtime.memory_store
    with pytest.raises(KeyError):
        store.update_fields(update_item.id, users.alice.id, {"title": "Must not land"})
    with pytest.raises(KeyError):
        store.transition(
            transition_item.id, users.alice.id, {"candidate"}, "confirmed"
        )

    with repo._connect() as db:
        updated = db.execute(
            "SELECT title FROM memory_items WHERE id=?", (update_item.id,)
        ).fetchone()
        transitioned = db.execute(
            "SELECT status FROM memory_items WHERE id=?", (transition_item.id,)
        ).fetchone()
    assert updated["title"] == "Title"
    assert transitioned["status"] == "candidate"


def test_notebook_summary_memory_counts_are_grouped_once(
    repo, users, notebook, memory_service
):
    token = set_request_user(users.alice)
    try:
        other = repo.create_notebook(NotebookCreate(name="Other"))
    finally:
        reset_request_user(token)
    _candidate(memory_service, notebook, users.alice, "agent-a", "req-count-1")
    memory_service.create_candidate(
        other.id,
        users.alice.id,
        None,
        "req-count-2",
        "Manual agent item",
        "Body",
        [],
        "task",
        {},
        [],
    )

    statements = []
    with repo._connect() as db:
        db.set_trace_callback(statements.append)
        counts = repo._runtime.notebook_summaries.list_for_user(users.alice.id)
        db.set_trace_callback(None)
    by_id = {item.id: item.counts["memories"] for item in counts}
    grouped = [
        sql
        for sql in statements
        if "FROM memory_items" in sql and "GROUP BY created_by, notebook_id" in sql
    ]
    assert by_id[notebook.id] == 1
    assert by_id[other.id] == 1
    assert len(grouped) == 1
