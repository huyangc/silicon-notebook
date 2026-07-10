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
