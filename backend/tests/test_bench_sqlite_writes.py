from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models.schemas import NotebookCreate
from app.services.model_registry import WORKLOADS


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench_sqlite_writes.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("bench_sqlite_writes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_repo_is_offline_when_ambient_system_models_are_configured(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "MODEL_SERVICES_CONFIG", str(ROOT / "model-services.example.toml")
    )
    monkeypatch.setenv("GENERAL_LLM_API_KEY", "general-secret")
    monkeypatch.setenv("REASONING_LLM_API_KEY", "reasoning-secret")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-secret")
    monkeypatch.setenv("RERANK_API_KEY", "rerank-secret")
    benchmark = _load_benchmark()

    repo = benchmark._make_repo(str(tmp_path / "bench.db"))
    try:
        assert all(not repo.configured(workload_id) for workload_id in WORKLOADS)

        notebook = repo.create_notebook(NotebookCreate(name="bench"))
        repo.store_kg(notebook.id, None, benchmark._objs(0, 2), [])
        with repo._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM knowledge_embeddings"
            ).fetchone()[0] == 0
    finally:
        repo.close_local()
