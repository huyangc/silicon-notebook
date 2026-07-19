from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend" / "tests" / "fixtures" / "repository_contract"

REQUIRED_PHASES = {
    "process_source",
    "store_kg",
    "delete_source",
    "parse_source",
    "update_knowledge",
    "merge_knowledge",
    "approve_promotion",
    "confirm_conflict",
    "set_edge_review",
    "copy_notebook",
    "streaming_ask",
    "migration_recovery_seed",
}
REQUIRED_ERROR_POLICIES = {
    "append_ask_trace",
    "report_corpus_map",
    "model_error_recording",
    "update_report_missing",
    "delete_report_missing",
    "source_chunk_build",
    "source_embedding",
    "source_extraction",
}

CORRECTED_PHASES = {
    "approve_promotion": {
        "sequence": [
            "validate candidate",
            "commit base object/provenance and candidate approval in one transaction",
            "embed fresh base object best effort",
            "invalidate unified cache",
            "mark base dirty",
        ],
        "commit_boundaries": [
            "base mutation and candidate approved state commit together",
            "embedding, invalidation and dirty hooks are post-commit",
        ],
        "failure_boundary": (
            "transaction failure rolls back base mutation and candidate state together; "
            "post-commit hook failure cannot revert approval"
        ),
    },
    "confirm_conflict": {
        "sequence": [
            "load conflict candidate",
            "apply resolution through its independently committed mutation primitive",
            "set candidate applied in a separate transaction",
        ],
        "commit_boundaries": [
            "resolution mutation commits before candidate status transaction",
            "candidate status is a later independent commit",
        ],
        "failure_boundary": (
            "candidate-status failure leaves the already committed resolution mutation applied"
        ),
    },
    "set_edge_review": {
        "sequence": [
            "validate review status",
            "update relation review in one write",
            "mark dirty",
            "invalidate cache",
        ],
        "commit_boundaries": [
            "review row commits before dirty and cache side effects"
        ],
        "failure_boundary": (
            "missing relation raises; post-commit side-effect failure does not revert review"
        ),
    },
}


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'phase.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    repo = SQLiteRepository(Settings(_env_file=None))
    repo.embedder = FakeEmbedder(dim=16)
    return repo


def _trace_writes(repo, monkeypatch, events):
    original = repo._write

    @contextmanager
    def traced_write():
        events.append("write.begin")
        with original() as db:
            yield db
        events.append("write.commit")

    monkeypatch.setattr(repo, "_write", traced_write)


def _seed_relation(repo):
    notebook = repo.create_notebook(NotebookCreate(name="phase relation"))
    repo.store_kg(
        notebook.id,
        None,
        [
            {
                "local_id": "a",
                "object_type": "claim",
                "payload": {"name": "claim a"},
                "evidence": [],
            },
            {
                "local_id": "b",
                "object_type": "claim",
                "payload": {"name": "claim b"},
                "evidence": [],
            },
        ],
        [
            {
                "source_local_id": "a",
                "target_local_id": "b",
                "edge_type": "contradicts",
                "evidence": [],
            }
        ],
    )
    with repo._connect() as db:
        relation_id = db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=?", (notebook.id,)
        ).fetchone()["id"]
    return notebook.id, relation_id


def _load(name: str) -> dict[str, object]:
    path = FIXTURES / name
    assert path.is_file(), f"missing repository phase fixture: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_phase_contracts_list_every_required_operation():
    tx = _load("transaction_phases.json")
    err = _load("error_policies.json")

    assert set(tx) == REQUIRED_PHASES
    assert set(err) == REQUIRED_ERROR_POLICIES


def test_transaction_contracts_freeze_order_and_failure_boundaries():
    tx = _load("transaction_phases.json")

    for operation, contract in tx.items():
        assert set(contract) >= {
            "sequence",
            "commit_boundaries",
            "failure_boundary",
        }, operation
        assert contract["sequence"], operation
        assert contract["commit_boundaries"], operation
        assert contract["failure_boundary"], operation


def test_mutation_matrix_freezes_every_semantic_side_effect():
    mutation = _load("mutation_phases.json")

    assert set(mutation) == REQUIRED_PHASES
    for operation, contract in mutation.items():
        assert set(contract) == {
            "semantic_mutation",
            "cache_invalidation",
            "unified_dirty",
            "version_bump",
            "index_scheduling",
            "exemption",
        }, operation
        assert all(
            isinstance(contract[key], bool)
            for key in {
                "semantic_mutation",
                "cache_invalidation",
                "unified_dirty",
                "version_bump",
                "index_scheduling",
            }
        ), operation
        assert isinstance(contract["exemption"], str), operation


def test_error_contracts_distinguish_raise_record_and_best_effort_paths():
    policies = _load("error_policies.json")

    for operation, contract in policies.items():
        assert set(contract) >= {"policy", "observable_result"}, operation
        assert contract["policy"] in {
            "raise",
            "return_none",
            "record_and_continue",
            "best_effort",
        }, operation
        assert contract["observable_result"], operation


def test_reviewed_phase_contracts_match_the_baseline_runtime():
    phases = _load("transaction_phases.json")

    for operation, expected in CORRECTED_PHASES.items():
        assert phases[operation] == expected, operation


def test_set_edge_review_commits_then_marks_dirty_then_invalidates(
    repository, monkeypatch
):
    notebook_id, relation_id = _seed_relation(repository)
    events = []
    _trace_writes(repository, monkeypatch, events)
    monkeypatch.setattr(
        repository, "_mark_unified_kg_dirty", lambda _notebook_id: events.append("dirty")
    )
    monkeypatch.setattr(
        repository,
        "_invalidate_unified_cache",
        lambda _notebook_id: events.append("invalidate"),
    )

    repository.set_edge_review(notebook_id, relation_id, "rejected")

    assert events == ["write.begin", "write.commit", "dirty", "invalidate"]


def test_set_edge_review_dirty_failure_keeps_committed_review(repository, monkeypatch):
    notebook_id, relation_id = _seed_relation(repository)
    monkeypatch.setattr(
        repository,
        "_mark_unified_kg_dirty",
        lambda _notebook_id: (_ for _ in ()).throw(RuntimeError("dirty failed")),
    )

    with pytest.raises(RuntimeError, match="dirty failed"):
        repository.set_edge_review(notebook_id, relation_id, "rejected")

    with repository._connect() as db:
        status = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?", (relation_id,)
        ).fetchone()["review_status"]
    assert status == "rejected"


def test_approve_promotion_commits_candidate_and_base_before_hooks(
    repository, monkeypatch
):
    personal = repository.create_notebook(NotebookCreate(name="personal"))
    base = repository.create_notebook(NotebookCreate(name="base"))
    repository.mark_notebook_base(base.id)
    repository.replace_notebook_bases(personal.id, [base.id], "user-local")
    object_id = repository._test_insert_object(
        personal.id, "claim", {"name": "promotion phase"}
    )
    candidate = repository.propose_promotion(personal.id, object_id)
    events = []
    _trace_writes(repository, monkeypatch, events)

    def observe_embed(base_object_id, base_notebook_id, _payload):
        with repository._connect() as db:
            candidate_status = db.execute(
                "SELECT status FROM promotion_candidates WHERE id=?", (candidate["id"],)
            ).fetchone()["status"]
            base_count = db.execute(
                "SELECT COUNT(*) AS n FROM knowledge_objects WHERE id=? AND notebook_id=?",
                (base_object_id, base_notebook_id),
            ).fetchone()["n"]
        events.append(f"embed:{candidate_status}:{base_count}")

    monkeypatch.setattr(repository, "_embed_knowledge", observe_embed)
    monkeypatch.setattr(
        repository,
        "_invalidate_unified_cache",
        lambda _notebook_id: events.append("invalidate"),
    )
    monkeypatch.setattr(
        repository, "_mark_unified_kg_dirty", lambda _notebook_id: events.append("dirty")
    )

    repository.approve_promotion(candidate["id"])

    assert events == [
        "write.begin",
        "write.commit",
        "embed:approved:1",
        "invalidate",
        "dirty",
    ]


def test_approve_promotion_transaction_failure_rolls_back_base_and_candidate(
    repository, monkeypatch
):
    personal = repository.create_notebook(NotebookCreate(name="personal rollback"))
    base = repository.create_notebook(NotebookCreate(name="base rollback"))
    repository.mark_notebook_base(base.id)
    repository.replace_notebook_bases(personal.id, [base.id], "user-local")
    object_id = repository._test_insert_object(
        personal.id, "claim", {"name": "rollback promotion"}
    )
    candidate = repository.propose_promotion(personal.id, object_id)
    original = repository._write

    class FailingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, *args, **kwargs):
            if "UPDATE promotion_candidates" in sql and "status='approved'" in sql:
                raise RuntimeError("candidate update failed")
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    @contextmanager
    def failing_write():
        with original() as db:
            yield FailingConnection(db)

    monkeypatch.setattr(repository, "_write", failing_write)

    with pytest.raises(RuntimeError, match="candidate update failed"):
        repository.approve_promotion(candidate["id"])

    with repository._connect() as db:
        candidate_status = db.execute(
            "SELECT status FROM promotion_candidates WHERE id=?", (candidate["id"],)
        ).fetchone()["status"]
        base_count = db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects "
            "WHERE notebook_id=? AND source_candidate_id=?",
            (base.id, candidate["id"]),
        ).fetchone()["n"]
    assert candidate_status == "proposed"
    assert base_count == 0


def test_confirm_conflict_commits_mutation_before_candidate_status(
    repository, monkeypatch
):
    notebook = repository.create_notebook(NotebookCreate(name="conflict phase"))
    left = repository._test_insert_object(
        notebook.id, "claim", {"name": "before", "section_path": "1"}
    )
    right = repository._test_insert_object(
        notebook.id, "claim", {"name": "other", "section_path": "1"}
    )
    candidate_id = repository.write_conflict_candidate(
        notebook.id,
        "node",
        left,
        right,
        resolution="modify",
        winner_ref=left,
        resolved_payload=json.dumps({"name": "after"}),
    )
    events = []
    _trace_writes(repository, monkeypatch, events)
    monkeypatch.setattr(repository, "_embed_knowledge", lambda *_args: None)
    monkeypatch.setattr(repository, "_invalidate_unified_cache", lambda *_args: None)
    monkeypatch.setattr(repository, "_mark_unified_kg_dirty", lambda *_args: None)
    original_status = repository.set_conflict_status

    def traced_status(*args):
        events.append("candidate-status")
        return original_status(*args)

    monkeypatch.setattr(repository, "set_conflict_status", traced_status)

    repository.confirm_conflict(notebook.id, candidate_id)

    assert events == [
        "write.begin",
        "write.commit",
        "candidate-status",
        "write.begin",
        "write.commit",
    ]


def test_confirm_conflict_status_failure_leaves_mutation_committed(
    repository, monkeypatch
):
    notebook = repository.create_notebook(NotebookCreate(name="conflict failure"))
    left = repository._test_insert_object(
        notebook.id, "claim", {"name": "before", "section_path": "1"}
    )
    right = repository._test_insert_object(
        notebook.id, "claim", {"name": "other", "section_path": "1"}
    )
    candidate_id = repository.write_conflict_candidate(
        notebook.id,
        "node",
        left,
        right,
        resolution="modify",
        winner_ref=left,
        resolved_payload=json.dumps({"name": "after"}),
    )
    monkeypatch.setattr(repository, "_embed_knowledge", lambda *_args: None)
    monkeypatch.setattr(repository, "_invalidate_unified_cache", lambda *_args: None)
    monkeypatch.setattr(repository, "_mark_unified_kg_dirty", lambda *_args: None)
    monkeypatch.setattr(
        repository,
        "set_conflict_status",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("status failed")),
    )

    with pytest.raises(RuntimeError, match="status failed"):
        repository.confirm_conflict(notebook.id, candidate_id)

    with repository._connect() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM knowledge_objects WHERE id=?", (left,)
            ).fetchone()["payload"]
        )
        candidate_status = db.execute(
            "SELECT status FROM kg_conflict_candidates WHERE id=?", (candidate_id,)
        ).fetchone()["status"]
    assert payload["name"] == "after"
    assert candidate_status == "pending"
