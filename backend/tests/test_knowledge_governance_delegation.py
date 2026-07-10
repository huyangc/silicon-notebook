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


def test_governance_module_never_imports_the_facade():
    modules = _imports(BACKEND / "app/services/knowledge_governance.py")
    assert not any(
        m.startswith("app.services.sqlite_repository") for m in modules
    )
    assert not any(
        m.startswith("app.services.repository_runtime") for m in modules
    )
