from pathlib import Path
import pytest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runtime.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services import sqlite_repository
    return sqlite_repository.SQLiteRepository(Settings())


def test_runtime_seams_are_late_bound(repo, monkeypatch):
    from app.services import sqlite_repository

    assert repo._runtime.settings is repo.settings
    monkeypatch.setattr(sqlite_repository, "_now", lambda: "clock-sentinel")
    assert repo._runtime.seams.now() == "clock-sentinel"


def test_runtime_construction_does_not_evaluate_seams():
    from app.core.config import Settings
    from app.services.repository_runtime import RepositoryCompatibilitySeams, RepositoryRuntime

    calls = []
    seams = RepositoryCompatibilitySeams(*(lambda *args, _name=name: calls.append(_name) for name in ("id", "now", "chunk", "remap")))
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        event_log_enabled=False,
        llm_log_enabled=False,
    )
    RepositoryRuntime(settings=settings, root_dir=Path("."), seams=seams)
    assert calls == []


def test_evidence_context_is_composed_once_with_truthful_graph_port(repo):
    import inspect

    from app.models.schemas import NotebookCreate
    from app.services.repository_runtime import RepositoryRuntime
    from app.services.retrieval import RetrievedKnowledge

    runtime = repo._runtime
    assert runtime.evidence_context is None

    notebook = repo.create_notebook(NotebookCreate(name="evidence"))
    object_id = repo._test_insert_object(
        notebook.id, "concept", {"name": "Cascode"},
    )
    block, evidence = repo._answer_context(
        notebook.id,
        [RetrievedKnowledge(
            object_id=object_id, object_type="concept",
            payload={"name": "Cascode"}, evidence=[],
        )],
    )

    context = runtime.evidence_context
    assert context is not None
    assert context.knowledge is repo.retrieval.graph
    assert all(callable(getattr(context.knowledge, name)) for name in (
        "cluster_map", "node_context", "in_network_relations",
        "relation_support_count",
    ))
    assert block.startswith("k1: [concept][personal] Cascode")
    assert evidence["k1"]["object_id"] == object_id

    first_context = context
    assert repo.retrieval is runtime.retrieval
    assert runtime.evidence_context is first_context
    assert "evidence_context.knowledge" not in inspect.getsource(
        RepositoryRuntime.wire_retrieval
    )
