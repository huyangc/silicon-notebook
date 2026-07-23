"""Task 16 — knowledge governance orchestration extraction.

The facade's governance surface (edge review queue, merge candidates +
merge-review job, conflict candidates + compound conflict resolution,
promotion state machine, knowledge update/merge/dedup and the concept
whitelist) delegates to the SAME KnowledgeGovernanceService instance Task 15
seeded with resolve_notebook_conflicts.  The Task-15 temporary facade ports
for write_conflict_candidate / apply_conflict_resolution are gone — the
service owns those bodies — while the compound conflict flow keeps riding the
facade ``set_conflict_status`` wrapper the frozen phase contracts observe.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_chat_client

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'gov.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_facade_governance_delegates_without_domain_sql(repo, monkeypatch):
    expected = [{"rel_id": "rel-1"}]
    monkeypatch.setattr(
        repo._runtime.knowledge_governance,
        "review_queue",
        lambda notebook_id, limit=200: expected,
    )
    assert repo.review_queue("nb", 9) is expected


def test_facade_governance_methods_delegate_to_the_runtime_instance(repo, monkeypatch):
    governance = repo._runtime.knowledge_governance
    sentinel = object()
    for name, call in (
        ("review_queue", lambda: repo.review_queue("nb")),
        ("set_edge_review", lambda: repo.set_edge_review("nb", "rel", "verified")),
        ("write_merge_candidate", lambda: repo.write_merge_candidate("nb", "a", "b", 0.9)),
        ("pending_merges", lambda: repo.pending_merges("nb")),
        ("_pending_merges_batch", lambda: repo._pending_merges_batch("nb", 5)),
        ("_has_pending_merges", lambda: repo._has_pending_merges("nb")),
        ("set_merge_decision", lambda: repo.set_merge_decision("nb", "c", "confirmed")),
        ("confirm_merge", lambda: repo.confirm_merge("nb", "c")),
        ("reject_merge", lambda: repo.reject_merge("nb", "c")),
        ("review_pending_merges", lambda: repo.review_pending_merges("nb")),
        ("merge_review_job_status", lambda: repo.merge_review_job_status("nb")),
        ("run_merge_review_job", lambda: repo.run_merge_review_job("nb")),
        ("write_conflict_candidate", lambda: repo.write_conflict_candidate("nb", "node", "l", "r")),
        ("pending_conflicts", lambda: repo.pending_conflicts("nb")),
        ("set_conflict_status", lambda: repo.set_conflict_status("nb", "c", "applied")),
        ("get_conflict_candidate", lambda: repo.get_conflict_candidate("nb", "c")),
        ("apply_conflict_resolution", lambda: repo.apply_conflict_resolution(
            "nb", kind="node", left_ref="l", right_ref="r", resolution="keep")),
        ("confirm_conflict", lambda: repo.confirm_conflict("nb", "c")),
        ("reject_conflict", lambda: repo.reject_conflict("nb", "c")),
        ("resolve_notebook_conflicts", lambda: repo.resolve_notebook_conflicts("nb")),
        ("propose_promotion", lambda: repo.propose_promotion("nb", "obj")),
        ("list_promotion_queue", lambda: repo.list_promotion_queue()),
        ("approve_promotion", lambda: repo.approve_promotion("c")),
        ("reject_promotion", lambda: repo.reject_promotion("c")),
        ("update_knowledge", lambda: repo.update_knowledge("nb", "k", None)),
        ("find_duplicates", lambda: repo.find_duplicates("nb", "concept")),
        ("merge_knowledge", lambda: repo.merge_knowledge("nb", "s", None)),
        ("decided_pairs", lambda: repo.decided_pairs("nb")),
        ("decided_seed_pairs", lambda: repo.decided_seed_pairs("nb")),
        ("concept_whitelist_terms", lambda: repo.concept_whitelist_terms()),
        ("concept_whitelist_list", lambda: repo.concept_whitelist_list()),
        ("concept_whitelist_add", lambda: repo.concept_whitelist_add("t")),
        ("concept_whitelist_remove", lambda: repo.concept_whitelist_remove("t")),
    ):
        monkeypatch.setattr(governance, name, lambda *a, **k: sentinel)
        assert call() is sentinel, name


def test_governance_service_owns_the_domain_stores(repo):
    governance = repo._runtime.knowledge_governance
    assert governance.governance_store is repo._runtime.governance
    assert governance.knowledge is repo._runtime.knowledge


def test_task15_conflict_mutation_ports_are_gone(repo):
    """Task 15's TEMPORARY facade ports for the compound conflict mutations
    are replaced by service-owned bodies; only the facade set_conflict_status
    wrapper survives as a port (the frozen confirm_conflict phase contract
    patches that facade method and must keep intercepting the compound flow)."""
    governance = repo._runtime.knowledge_governance
    assert not hasattr(governance, "_write_conflict_candidate")
    assert not hasattr(governance, "_apply_conflict_resolution")
    assert hasattr(governance, "_set_conflict_status")


def test_compound_conflict_status_rides_the_facade_wrapper(repo, monkeypatch):
    """Phase order (frozen): confirm_conflict applies the mutation FIRST, then
    routes the candidate-status transaction through the facade
    set_conflict_status delegate — patching the facade method must intercept
    the compound flow exactly like the frozen phase contracts do."""
    import json as _json

    notebook = repo.create_notebook(NotebookCreate(name="port"))
    left = repo._test_insert_object(notebook.id, "claim", {"name": "before"})
    right = repo._test_insert_object(notebook.id, "claim", {"name": "other"})
    candidate_id = repo.write_conflict_candidate(
        notebook.id,
        "node",
        left,
        right,
        resolution="modify",
        winner_ref=left,
        resolved_payload=_json.dumps({"name": "after"}),
    )
    events = []
    original_status = repo.set_conflict_status

    def traced_status(*args):
        events.append("candidate-status")
        return original_status(*args)

    monkeypatch.setattr(repo, "set_conflict_status", traced_status)

    repo.confirm_conflict(notebook.id, candidate_id)

    assert events == ["candidate-status"]
    with repo._connect() as db:
        status = db.execute(
            "SELECT status FROM kg_conflict_candidates WHERE id=?", (candidate_id,)
        ).fetchone()["status"]
    assert status == "applied"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ---------------------------------------------------------------------------
# Task 4 delegation tests: the governance service holds ZERO SQL — each read it
# used to run inline is now a GovernanceStore method on `repo._runtime.governance`.
# These spies pin every public governance op to its store method (arg-for-arg),
# so re-inlining the SQL or wiring an op to the wrong store method turns red.
# Primary assertions tagged `# MUT` (mutation harness inverts them).
# ---------------------------------------------------------------------------


def test_t4deleg_review_queue_rows_delegate(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="rq"))
    calls = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return ([], [])

    monkeypatch.setattr(repo._runtime.governance, "review_queue_rows", spy)
    result = repo.review_queue(notebook.id)
    assert calls and calls[0] == notebook.id and result == []  # MUT


def test_t4deleg_promotion_object_type_row_delegate(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="promo"))
    base = repo.create_notebook(NotebookCreate(name="promo base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    object_id = repo._test_insert_object(notebook.id, "concept", {"name": "Widget"})
    store = repo._runtime.governance
    original = store.promotion_object_type_row  # staticmethod -> plain function
    calls = []

    def spy(db, notebook_id, obj_id):
        calls.append((notebook_id, obj_id))
        return original(db, notebook_id, obj_id)

    monkeypatch.setattr(store, "promotion_object_type_row", spy)
    repo.propose_promotion(notebook.id, object_id)
    assert calls and calls[0] == (notebook.id, object_id)  # MUT


def test_t4deleg_notebook_tier_row_delegate(repo, monkeypatch):
    # promotion_object_type_row runs for real (real object); only the tier read
    # is spied, so propose_promotion's base-notebook guard reaches the store.
    notebook = repo.create_notebook(NotebookCreate(name="tier"))
    base = repo.create_notebook(NotebookCreate(name="tier base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    object_id = repo._test_insert_object(notebook.id, "concept", {"name": "T"})
    calls = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return {"tier": "personal"}

    monkeypatch.setattr(repo._runtime.governance, "notebook_tier_row", spy)
    repo.propose_promotion(notebook.id, object_id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_promotion_object_rows_delegate(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="pq"))
    base = repo.create_notebook(NotebookCreate(name="pq base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    object_id = repo._test_insert_object(notebook.id, "concept", {"name": "Q"})
    repo.propose_promotion(notebook.id, object_id)  # seeds one 'proposed' candidate
    calls = []

    def spy(db, object_ids):
        calls.append(list(object_ids))
        return []

    monkeypatch.setattr(repo._runtime.governance, "promotion_object_rows", spy)
    repo.list_promotion_queue()
    assert calls and calls[0] == [object_id]  # MUT


def test_t4deleg_object_payload_row_delegate(repo, monkeypatch):
    # object_payload_row is read BEFORE approve_promotion_in_transaction raises
    # for a missing target_base_id — the spy still records the call. Task 7
    # (multi-domain base libraries) moved the "nowhere to promote into" guard
    # from a global "no base notebook exists" check to a per-candidate
    # target_base_id read; propose_promotion itself now refuses to create a
    # candidate with an empty target, so this seeds one directly via raw SQL,
    # mirroring a pre-Task-7 legacy row (see promotion_candidates migration).
    notebook = repo.create_notebook(NotebookCreate(name="approve"))
    object_id = repo._test_insert_object(notebook.id, "concept", {"name": "A"})
    candidate_id = "promo-t4deleg-empty-target"
    with repo._write() as db:
        db.execute(
            "INSERT INTO promotion_candidates "
            "(id, notebook_id, object_id, object_type, status, reason, "
            " reviewed_by, base_match_id, created_at, updated_at, target_base_id) "
            "VALUES (?,?,?,?,'proposed','','','',?,?,'')",
            (candidate_id, notebook.id, object_id, "concept", "t", "t"),
        )
    calls = []

    def spy(db, obj_id):
        calls.append(obj_id)
        return {"payload": "{}"}

    monkeypatch.setattr(repo._runtime.governance, "object_payload_row", spy)
    with pytest.raises(ValueError, match="target_base_id"):
        repo.approve_promotion(candidate_id)
    assert calls and calls[0] == object_id  # MUT


def test_t4deleg_conflict_resolution_rows_delegate(repo, monkeypatch):
    import types

    notebook = repo.create_notebook(NotebookCreate(name="conflict"))
    governance = repo._runtime.knowledge_governance
    # Get past the "LLM not configured -> skip" guard so the compound
    # conflict-resolution read is actually reached.
    bind_chat_client(
        repo,
        "kg_conflict_review",
        types.SimpleNamespace(configured=True),
    )
    calls = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return ([], [], None)

    monkeypatch.setattr(repo._runtime.governance, "conflict_resolution_rows", spy)
    result = repo.resolve_notebook_conflicts(notebook.id)
    assert calls and calls[0] == notebook.id and result["skipped_llm"] is False  # MUT
