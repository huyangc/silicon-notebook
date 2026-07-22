from __future__ import annotations

import asyncio
from functools import lru_cache
import threading

import pytest

from app.api import deps
from app.core import readiness
from app.core.config import Settings
from app.services import model_provider as provider_mod
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider, bind_embedding_client


class _InjectedProvider(RecordingModelProvider):
    def __init__(self) -> None:
        super().__init__(
            embedding_clients={
                "retrieval_query_embedding": FakeEmbedder(dim=8),
            },
            parallelism_by_workload={"kg_extract": 1},
        )


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
    assert repo._runtime.models.embedding("retrieval_query_embedding").dim == 8
    repo.close()
    repo.close()
    assert provider.closed == 1


def test_binding_chunk_embedding_does_not_configure_or_replace_cached_query_seats(
    tmp_path,
):
    provider = _InjectedProvider()
    repo = SQLiteRepository(_settings(tmp_path), model_provider=provider)
    retrieval = repo.retrieval
    original_query = provider.embedding("retrieval_query_embedding")
    original_memory = provider.embedding("memory_embedding")
    assert provider.configured("retrieval_query_embedding") is True
    assert provider.configured("memory_embedding") is False
    assert retrieval.candidates.embedder is original_query
    assert retrieval.graph.embedder is original_query
    assert repo._runtime.memory_retriever.embedder is original_query
    assert repo._runtime.memory_service.embedder is original_memory

    chunk = FakeEmbedder(dim=8)
    bind_embedding_client(repo, chunk, workload_ids=("chunk_embedding",))

    assert provider.embedding("chunk_embedding") is chunk
    assert provider.configured("chunk_embedding") is True
    assert provider.embedding("retrieval_query_embedding") is original_query
    assert provider.configured("retrieval_query_embedding") is True
    assert provider.embedding("memory_embedding") is original_memory
    assert provider.configured("memory_embedding") is False
    assert retrieval.candidates.embedder is original_query
    assert retrieval.graph.embedder is original_query
    assert repo._runtime.memory_retriever.embedder is original_query
    assert repo._runtime.memory_service.embedder is original_memory
    assert ("embedding", "chunk_embedding") in provider.calls
    repo.close()


def test_embedding_binding_refreshes_only_the_selected_cached_workload_seats(tmp_path):
    provider = _InjectedProvider()
    repo = SQLiteRepository(_settings(tmp_path), model_provider=provider)
    retrieval = repo.retrieval
    original_memory = repo._runtime.memory_service.embedder

    query = FakeEmbedder(dim=8)
    bind_embedding_client(
        repo, query, workload_ids=("retrieval_query_embedding",)
    )
    assert provider.embedding("retrieval_query_embedding") is query
    assert retrieval.candidates.embedder is query
    assert retrieval.graph.embedder is query
    assert repo._runtime.memory_retriever.embedder is query
    assert repo._runtime.memory_service.embedder is original_memory

    memory = FakeEmbedder(dim=8)
    bind_embedding_client(repo, memory, workload_ids=("memory_embedding",))
    assert provider.embedding("memory_embedding") is memory
    assert repo._runtime.memory_service.embedder is memory
    assert retrieval.candidates.embedder is query
    assert retrieval.graph.embedder is query
    assert repo._runtime.memory_retriever.embedder is query
    repo.close()


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


def test_lifespan_waits_off_loop_for_startup_before_shutdown_check(monkeypatch):
    import app.main as main
    import app.services.startup_warmup as startup_warmup

    readiness.reset()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    order = []

    def fake_startup() -> None:
        started.set()
        assert release.wait(2)
        order.append("startup-finished")
        finished.set()

    monkeypatch.setattr(startup_warmup, "run_startup", fake_startup)
    monkeypatch.setattr(
        main,
        "shutdown_repository_if_initialized",
        lambda: order.append("shutdown-checked"),
    )

    async def exercise() -> None:
        manager = main._lifespan(object())
        await manager.__aenter__()
        assert await asyncio.to_thread(started.wait, 1)
        exit_task = asyncio.create_task(manager.__aexit__(None, None, None))
        await asyncio.sleep(0.02)
        assert not exit_task.done()
        assert order == []
        release.set()
        await exit_task

    try:
        asyncio.run(exercise())
        assert finished.is_set()
        assert order == ["startup-finished", "shutdown-checked"]
    finally:
        release.set()
