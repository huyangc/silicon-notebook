"""Batch 3·W1 PR-3 Phase A §4.2 option A: the THIRD in-flight-rebuild
checkpoint — buildkg-/rebuildkg-'s batch loop (`_run_notebook_kg_job`,
``services/knowledge_lifecycle.py``). The sibling checkpoints (relinkkg-'s
source loop, unifiedkg-'s ``_stage``) are covered in
``test_notebook_delete_jobization.py``; this one needs the real extraction
harness (``_ControlledKgClient`` / ``bind_chat_client``), so it lives on its
own to avoid mixing two incompatible ``repo`` fixture shapes in one file.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg import scheduler as kg_scheduler
from app.services.kg.run_control import KgBuildAborted
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import (
    RecordingModelProvider,
    bind_all_embedding_clients,
    bind_chat_client,
)
from tests.test_kg_build_circuit_breaker import (
    _ControlledKgClient,
    _seed_three_parsed_sources,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'kg.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings(_env_file=None)
    settings.kg_llm_max_retries = 0
    settings.paper_meta_enabled = False
    settings.kg_refine_enabled = False
    settings.kg_gleaning_enabled = False
    settings.kg_conflict_resolution_enabled = False
    settings.kg_relink_enabled = False
    provider = RecordingModelProvider()
    result = SQLiteRepository(settings, model_provider=provider)
    bind_all_embedding_clients(result, FakeEmbedder(dim=settings.embed_dim))
    kg_scheduler.configure(window_workers=1, job_workers=1)
    try:
        yield result
    finally:
        kg_scheduler.reset()


def test_buildkg_batch_loop_checkpoint_aborts_before_any_source_is_processed(repo):
    """变异钉:删掉批循环里的检查点会让本用例变红——第一批照常提交给模型,
    `client.source_calls` 会 > 0,`completed_sources` 会 > 0,而且不会抛
    `KgBuildAborted`(``notebook_deleting`` 错误码)。

    通过监听式注入 `_notebook_deleting`(而非把 notebook 行真的置为
    'deleting')隔离测「批循环里那一次点查」本身——把行真的置为 deleting
    会在 `prepare_notebook_kg_job`/`execute_notebook_kg_job` 更早的
    `get_notebook()` 处就先 404,够不到本检查点。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    client = _ControlledKgClient()
    bind_chat_client(repo, "kg_extract", client)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle._notebook_deleting = lambda _nid: True

    with pytest.raises(KgBuildAborted) as excinfo:
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")
    assert excinfo.value.failure.code == "notebook_deleting"
    assert client.source_calls == 0

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "notebook_deleting"
    assert saved["completed_sources"] == 0


def test_buildkg_batch_loop_does_not_abort_when_not_deleting(repo):
    """反向对照:notebook 不是 deleting 时,批循环正常提交给模型,不抛出。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    client = _ControlledKgClient()
    bind_chat_client(repo, "kg_extract", client)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    result = repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")
    assert client.source_calls == len(source_ids)
    assert len(result["built"]) == len(source_ids)
