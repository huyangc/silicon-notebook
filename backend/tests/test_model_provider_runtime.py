from __future__ import annotations

import asyncio
from functools import lru_cache

import pytest

from app.api import deps
from app.core import readiness
from app.core.config import Settings
from app.services import model_provider as provider_mod
from app.services.sqlite_repository import SQLiteRepository


class _InjectedProvider:
    def __init__(self) -> None:
        self.closed = 0

    def embedding(self, workload_id: str):
        assert workload_id == "retrieval_query_embedding"
        from app.services.embedding import FakeEmbedder

        return FakeEmbedder(dim=8)

    def close(self) -> None:
        self.closed += 1


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'provider.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
    )


def test_repository_uses_injected_provider_before_service_composition(tmp_path):
    provider = _InjectedProvider()

    repo = SQLiteRepository(_settings(tmp_path), model_provider=provider)

    assert repo._runtime.models is provider
    assert repo.embedder.dim == 8
    repo.close()
    repo.close()
    assert provider.closed == 1


@pytest.mark.parametrize("name", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
def test_process_local_scheduler_rejects_multi_worker_launch(name):
    with pytest.raises(RuntimeError) as caught:
        provider_mod.validate_process_local_scheduler_deployment({name: "2"})

    message = str(caught.value)
    assert name in message
    assert "process-local" in message
    assert "URL" not in message and "key" not in message.lower()


@pytest.mark.parametrize("value", [None, "", "1", " 1 "])
def test_process_local_scheduler_accepts_single_or_unset_workers(value):
    environ = {} if value is None else {"WEB_CONCURRENCY": value}
    provider_mod.validate_process_local_scheduler_deployment(environ)


def test_shutdown_helper_does_not_construct_an_unused_repository(monkeypatch):
    constructed = []

    @lru_cache
    def fake_repository():
        constructed.append(True)
        return _InjectedProvider()

    monkeypatch.setattr(deps, "repository", fake_repository)
    deps.shutdown_repository_if_initialized()
    assert constructed == []


def test_shutdown_helper_closes_initialized_repository_once(monkeypatch):
    provider = _InjectedProvider()

    @lru_cache
    def fake_repository():
        return provider

    monkeypatch.setattr(deps, "repository", fake_repository)
    assert fake_repository() is provider
    deps.shutdown_repository_if_initialized()
    assert provider.closed == 1


def test_application_lifespan_always_shuts_down_initialized_repository(monkeypatch):
    import app.main as main

    calls = []
    monkeypatch.setattr(main, "shutdown_repository_if_initialized", lambda: calls.append("close"))
    readiness.mark_ready()

    async def exercise() -> None:
        async with main._lifespan(object()):
            assert calls == []

    asyncio.run(exercise())
    assert calls == ["close"]


def test_create_app_validates_single_process_before_other_startup_work(monkeypatch):
    import app.main as main

    class Rejected(RuntimeError):
        pass

    monkeypatch.setattr(
        main,
        "validate_process_local_scheduler_deployment",
        lambda: (_ for _ in ()).throw(Rejected("process-local")),
        raising=False,
    )
    with pytest.raises(Rejected, match="process-local"):
        main.create_app()
