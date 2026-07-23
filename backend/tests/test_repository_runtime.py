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
    from app.repositories.sqlite.bundle import SqlitePersistenceBundleFactory
    from app.services.repository_runtime import RepositoryCompatibilitySeams, RepositoryRuntime

    calls = []
    seams = RepositoryCompatibilitySeams(*(
        lambda *args, _name=name: calls.append(_name)
        for name in ("id", "now", "copy-chunk", "remap", "in-chunk")
    ))
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        event_log_enabled=False,
        llm_log_enabled=False,
    )
    RepositoryRuntime(
        settings=settings,
        root_dir=Path("."),
        seams=seams,
        persistence_factory=SqlitePersistenceBundleFactory(),
    )
    assert calls == []


def test_evidence_context_is_composed_once_with_truthful_graph_port(repo):
    import inspect

    from app.models.schemas import NotebookCreate
    from app.repositories.ports import EvidenceKnowledgeContextPort
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
    assert isinstance(context.knowledge, EvidenceKnowledgeContextPort)
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


def test_concurrent_first_use_composes_one_ready_retrieval_graph(repo, monkeypatch):
    import threading

    import app.services.repository_runtime as runtime_module

    original_init = runtime_module.CandidateRetrievalService.__init__
    first_entered = threading.Event()
    second_entered = threading.Event()
    count_lock = threading.Lock()
    construction_count = 0

    def coordinated_init(self, *args, **kwargs):
        nonlocal construction_count
        with count_lock:
            construction_count += 1
            ordinal = construction_count
        if ordinal == 1:
            first_entered.set()
            second_entered.wait(timeout=1)
        else:
            second_entered.set()
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(
        runtime_module.CandidateRetrievalService, "__init__", coordinated_init,
    )

    start = threading.Barrier(3)
    results = []
    errors = []

    def use_retrieval(use_evidence):
        start.wait()
        try:
            if use_evidence:
                repo._chunk_answer_context([], notebook_id="nb")
            service = repo.retrieval
            runtime = repo._runtime
            results.append((
                service, service.candidates, service.graph,
                runtime.evidence_context,
            ))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=use_retrieval, args=(False,)),
        threading.Thread(target=use_retrieval, args=(True,)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert construction_count == 1
    assert len(results) == 2
    assert all(item[3] is not None for item in results)
    for index in range(4):
        assert results[0][index] is results[1][index]
