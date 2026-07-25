import json
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx
import pytest
from openai import APIConnectionError

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.kg_build_job_store import KgBuildAlreadyRunning
from app.services.embedding import FakeEmbedder
from app.services.kg import scheduler as kg_scheduler
from app.services.kg.run_control import KgBuildAborted
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import (
    RecordingModelProvider,
    bind_chat_client,
    bind_all_embedding_clients,
)


class _ControlledKgClient:
    configured = True
    model = "test-kg"

    def __init__(self, *, fail_after_successful_sources=None, fail_probe=False):
        self.fail_after_successful_sources = fail_after_successful_sources
        self.fail_probe = fail_probe
        self.lock = threading.Lock()
        self.probes = 0
        self.source_calls = 0

    @staticmethod
    def _connection_error():
        return APIConnectionError(
            request=httpx.Request(
                "POST", "https://model.example/chat/completions"
            )
        )

    def chat_json(self, messages, response_schema_hint, **kwargs):
        prompt = messages[0]["content"]
        if prompt.startswith('Return {"ok":true}'):
            with self.lock:
                self.probes += 1
            if self.fail_probe:
                raise self._connection_error()
            return '{"ok":true}'

        with self.lock:
            self.source_calls += 1
            source_call = self.source_calls
        if (
            self.fail_after_successful_sources is not None
            and source_call > self.fail_after_successful_sources
        ):
            raise self._connection_error()
        return json.dumps(
            {
                "nodes": [
                    {
                        "local_id": "engram",
                        "type": "Concept",
                        "name": "Engram",
                        "ev": 0,
                    }
                ],
                "edges": [],
            }
        )


class _DrainVisibilityClient:
    configured = True
    model = "test-kg"

    def __init__(self):
        self.lock = threading.Lock()
        self.source_calls = 0
        self.blocked = threading.Event()
        self.failed = threading.Event()
        self.release = threading.Event()

    def chat_json(self, messages, response_schema_hint, **kwargs):
        prompt = messages[0]["content"]
        if prompt.startswith('Return {"ok":true}'):
            return '{"ok":true}'
        with self.lock:
            self.source_calls += 1
            call = self.source_calls
        if call == 1:
            self.blocked.set()
            assert self.release.wait(5)
            return json.dumps(
                {
                    "nodes": [
                        {
                            "local_id": "engram",
                            "type": "Concept",
                            "name": "Engram",
                            "ev": 0,
                        }
                    ],
                    "edges": [],
                }
            )
        assert self.blocked.wait(1)
        self.failed.set()
        raise _ControlledKgClient._connection_error()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'kg.db'}")
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage")
    )
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
    result.recording_model_provider = provider
    kg_scheduler.configure(window_workers=1, job_workers=1)
    try:
        yield result
    finally:
        kg_scheduler.reset()


def _seed_three_parsed_sources(repo):
    notebook = repo.create_notebook(NotebookCreate(name="KG circuit"))
    now = "2026-07-20T00:00:00"
    source_ids = [f"source-{index}" for index in range(3)]
    with repo._write() as db:
        for index, source_id in enumerate(source_ids):
            db.execute(
                """
                INSERT INTO sources
                (id, notebook_id, title, source_type, status, parse_status,
                 file_name, file_path, file_size, file_hash, summary, doc_type,
                 created_at, updated_at)
                VALUES (?, ?, ?, 'markdown', 'parsed', 'parsed',
                        ?, '', 0, ?, '', 'academic_paper', ?, ?)
                """,
                (
                    source_id,
                    notebook.id,
                    f"Source {index}",
                    f"source-{index}.md",
                    f"hash-{index}",
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO source_elements
                (id, source_id, element_type, location_label, text, metadata,
                 created_at)
                VALUES (?, ?, 'paragraph', 'p1', ?, '{}', ?)
                """,
                (
                    f"element-{index}",
                    source_id,
                    f"Engram is a technical memory architecture for source {index}.",
                    now,
                ),
            )
    return notebook, source_ids


def _source_statuses(repo, source_ids):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, status FROM sources WHERE id IN (?, ?, ?)",
            tuple(source_ids),
        ).fetchall()
    return {row["id"]: row["status"] for row in rows}


def _kg_source_ids(repo, notebook_id):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT source_id FROM knowledge_objects "
            "WHERE notebook_id=? AND source_id!=''",
            (notebook_id,),
        ).fetchall()
    return {row["source_id"] for row in rows}


def test_model_outage_preserves_completed_source_and_stops_remaining(repo):
    notebook, source_ids = _seed_three_parsed_sources(repo)
    client = _ControlledKgClient(fail_after_successful_sources=1)
    bind_chat_client(repo, "kg_extract", client)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, job["id"], "incremental"
        )

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["stage"] == "finished"
    assert saved["error_code"] == "model_unavailable"
    assert saved["completed_sources"] == 1
    assert saved["failed_sources"] == 0
    assert repo.get_notebook(notebook.id).kg_pending_sources == 2
    statuses = _source_statuses(repo, source_ids)
    assert "extracting" not in statuses.values()
    assert statuses[source_ids[0]] == "extracted"
    assert statuses[source_ids[1]] == "parsed"
    assert statuses[source_ids[2]] == "parsed"
    assert _kg_source_ids(repo, notebook.id) == {source_ids[0]}


def test_failed_rebuild_continues_incrementally_without_second_delete(
    repo, monkeypatch
):
    notebook, source_ids = _seed_three_parsed_sources(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    real_delete = lifecycle.delete_notebook_kg
    delete_calls = []

    def tracked_delete(notebook_id):
        delete_calls.append(notebook_id)
        return real_delete(notebook_id)

    monkeypatch.setattr(lifecycle, "delete_notebook_kg", tracked_delete)
    bind_chat_client(
        repo, "kg_extract",
        _ControlledKgClient(fail_after_successful_sources=1),
    )
    rebuild = repo.prepare_notebook_kg_job(notebook.id, "rebuild")
    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, rebuild["id"], "rebuild"
        )
    assert delete_calls == [notebook.id]
    assert _kg_source_ids(repo, notebook.id) == {source_ids[0]}

    delete_calls.clear()
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    continuation = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    result = repo.execute_notebook_kg_job(
        notebook.id, continuation["id"], "incremental"
    )

    assert delete_calls == []
    assert result["job_id"] == continuation["id"]
    assert sorted(result["built"]) == sorted(source_ids[1:])
    assert _kg_source_ids(repo, notebook.id) == set(source_ids)


def test_rebuild_probe_failure_happens_before_delete(repo, monkeypatch):
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient(fail_probe=True))
    lifecycle = repo._runtime.knowledge_lifecycle
    delete_calls = []
    monkeypatch.setattr(
        lifecycle,
        "delete_notebook_kg",
        lambda notebook_id: delete_calls.append(notebook_id),
    )
    job = repo.prepare_notebook_kg_job(notebook.id, "rebuild")

    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, job["id"], "rebuild"
        )

    assert delete_calls == []
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "model_unavailable"


def test_job_enters_stopping_before_running_windows_are_drained(
    repo, monkeypatch
):
    from types import SimpleNamespace

    from app.services import kg_ingest
    from app.services.kg.parsing import SourceElementQ

    notebook, _source_ids = _seed_three_parsed_sources(repo)
    client = _DrainVisibilityClient()
    bind_chat_client(repo, "kg_extract", client)
    elements = [
        SourceElementQ(
            id=f"window-element-{index}",
            type="paragraph",
            file="source.md",
            line_start=index + 1,
            line_end=index + 1,
            char_start=index * 20,
            char_end=index * 20 + 12,
            text=f"technical fact {index}",
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        kg_ingest,
        "windows_with_elements",
        lambda *_args, **_kwargs: [
            (SimpleNamespace(section_path=f"section-{index}"), [element])
            for index, element in enumerate(elements)
        ],
    )
    monkeypatch.setattr(
        kg_ingest,
        "should_extract_window",
        lambda *_args, **_kwargs: (True, ""),
    )
    kg_scheduler.configure(window_workers=2, job_workers=1)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            repo.execute_notebook_kg_job,
            notebook.id,
            job["id"],
            "incremental",
        )
        assert client.failed.wait(2)
        try:
            deadline = time.monotonic() + 2
            stage = ""
            while time.monotonic() < deadline:
                stage = repo._runtime.kg_build_jobs.get(job["id"])["stage"]
                if stage == "stopping":
                    break
                time.sleep(0.01)
            assert stage == "stopping"
            assert future.done() is False
        finally:
            client.release.set()
        with pytest.raises(KgBuildAborted):
            future.result(timeout=5)


def test_duplicate_preparation_never_enters_executor(repo):
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    first = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAlreadyRunning):
        repo.prepare_notebook_kg_job(notebook.id, "incremental")

    assert first["status"] == "running"
    assert repo._runtime.kg_build_jobs.get(first["id"])["status"] == "running"


def _interrupt_during_extraction(repo, monkeypatch, exc_type=KeyboardInterrupt):
    """让抽取阶段抛出 exc_type,并把该次运行的 run control 交给调用方检查。
    模拟操作者 Ctrl-C / SIGTERM:中断只在主线程抛出,此时线程池里的窗口仍在飞。"""
    lifecycle = repo._runtime.knowledge_lifecycle
    seen = {}

    def _raise(
        notebook_id, targets, skipped, skipped_no_elements, job_id,
        control, controlled_client, progress=None, on_abort=None,
    ):
        seen["control"] = control
        raise exc_type("interrupted")

    monkeypatch.setattr(lifecycle, "_extract_targets", _raise)
    return seen


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_interrupt_settles_job_and_frees_the_notebook(
    repo, monkeypatch, exc_type
):
    """Ctrl-C / SIGTERM 必须把 job 落成终态。否则 kg_build_jobs 那行永久停在
    'running',条件唯一索引会把该 notebook 之后的每次分析都挡成
    KgBuildAlreadyRunning——而离线 CLI 刻意无权自行清理,只能等后端重启。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    _interrupt_during_extraction(repo, monkeypatch, exc_type)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(exc_type):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["stage"] == "finished"
    assert saved["error_code"] == "worker_interrupted"
    assert saved["error_message"] != ""
    assert saved["finished_at"] != ""
    # 真正的回归点:单飞守卫已释放,同一 notebook 能再次发起分析。
    again = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    assert again["status"] == "running"
    assert again["id"] != job["id"]


def test_interrupt_aborts_in_flight_windows_without_faking_a_model_outage(
    repo, monkeypatch
):
    """中断要合作式停掉在飞窗口(否则它们在 job 落终态后继续调模型继续写图,
    而线程池的 atexit join 会把进程按住不退——用户只会看到「Ctrl-C 没反应」),
    但它不是模型熔断,不得记 kg_build_circuit_opened。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    seen = _interrupt_during_extraction(repo, monkeypatch)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    control = seen["control"]
    assert control.aborted is True
    assert control.failure.code == "worker_interrupted"
    kinds = [str(event.get("kind", "")) for event in events]
    assert "kg_build_stopping" in kinds
    assert "kg_build_failed" in kinds
    assert "kg_build_circuit_opened" not in kinds


def test_failed_settlement_never_replaces_the_interrupt(repo, monkeypatch):
    """收尾自身失败(如抢写锁超时)不得把中断换成别的异常:否则操作者拿到的是
    无关 traceback,而这行仍留在 'running'。记日志 + 原样抛出中断。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    _interrupt_during_extraction(repo, monkeypatch)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    logged = []
    monkeypatch.setattr(
        repo._runtime.kg_build_jobs, "finish",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )
    monkeypatch.setattr(
        repo.event_log.logger, "exception",
        lambda *a, **k: logged.append(a),
    )

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    assert logged, "收尾失败必须留下日志,不能静默"


class _InterruptingKgClient(_ControlledKgClient):
    """抽取过程中真的抛 KeyboardInterrupt(不打桩 _extract_targets),验证它确实能
    穿过 per-source 的 `except Exception`、future 和 as_completed 抵达收尾处。"""

    def chat_json(self, messages, response_schema_hint, **kwargs):
        prompt = messages[0]["content"]
        if prompt.startswith('Return {"ok":true}'):
            return super().chat_json(messages, response_schema_hint, **kwargs)
        raise KeyboardInterrupt("ctrl-c during extraction")


def test_interrupt_inside_real_extraction_reaches_the_settlement(repo):
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _InterruptingKgClient())
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "worker_interrupted"
    assert repo.prepare_notebook_kg_job(notebook.id, "incremental")["id"]
    # 被中断打断的来源必须退回可重试终态,否则界面上它一直「分析中」。
    assert "extracting" not in _source_statuses(repo, source_ids).values()


def test_model_outage_still_records_the_circuit_event(repo, monkeypatch):
    """反向护栏:真正的模型故障仍要记 kg_build_circuit_opened(上面那条不能
    把这个事件整体删掉当成「通过」)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(
        repo, "kg_extract",
        _ControlledKgClient(fail_after_successful_sources=0),
    )
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    kinds = [str(event.get("kind", "")) for event in events]
    assert "kg_build_circuit_opened" in kinds
    assert "kg_build_stopping" in kinds


def test_successful_job_emits_safe_started_progress_and_success_events(
    repo, monkeypatch
):
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)

    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    repo.execute_notebook_kg_job(
        notebook.id, job["id"], "incremental"
    )

    kg_events = [
        event for event in events
        if str(event.get("kind", "")).startswith("kg_build_")
    ]
    kinds = [event["kind"] for event in kg_events]
    assert kinds[0] == "kg_build_started"
    assert kinds.count("kg_build_progress") == len(source_ids)
    assert kinds[-1] == "kg_build_succeeded"
    allowed = {
        "kind", "job_id", "notebook_id", "mode", "status", "stage",
        "total_sources", "completed_sources", "failed_sources",
        "error_code", "latency_ms",
    }
    assert all(set(event) <= allowed for event in kg_events)
    assert all(event["job_id"] == job["id"] for event in kg_events)


def test_model_failure_emits_circuit_stopping_and_failed_without_diagnostics(
    repo, monkeypatch
):
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient(fail_probe=True))
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, job["id"], "incremental"
        )

    kg_events = [
        event for event in events
        if str(event.get("kind", "")).startswith("kg_build_")
    ]
    assert [event["kind"] for event in kg_events] == [
        "kg_build_started",
        "kg_build_circuit_opened",
        "kg_build_stopping",
        "kg_build_failed",
    ]
    for event in kg_events:
        rendered = json.dumps(event, ensure_ascii=False)
        assert "model.example" not in rendered
        assert "APIConnectionError" not in rendered
        assert "Return" not in rendered
        assert "source 0" not in rendered
    assert kg_events[-1]["error_code"] == "model_unavailable"


def test_stopping_publication_failure_does_not_replace_model_failure(
    repo, monkeypatch
):
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient(fail_probe=True))
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    monkeypatch.setattr(
        repo._runtime.kg_build_jobs,
        "set_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("status persistence failed")
        ),
    )

    with pytest.raises(KgBuildAborted) as raised:
        repo.execute_notebook_kg_job(
            notebook.id, job["id"], "incremental"
        )

    assert raised.value.failure.code == "model_unavailable"
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "model_unavailable"
