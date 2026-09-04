import json
import concurrent.futures
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
from app.services.kg.run_control import (
    MODEL_RESPONSE_INVALID_MESSAGE,
    MODEL_UNAVAILABLE_MESSAGE,
    KgBuildAborted,
    KgBuildFailure,
)
from app.services.model_work import ModelProviderError
from app.services.knowledge_lifecycle import ModelSkipPolicy
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import (
    RecordingModelProvider,
    bind_chat_client,
    bind_all_embedding_clients,
)


class _ControlledKgClient:
    configured = True
    model = "test-kg"

    def __init__(
        self,
        *,
        fail_after_successful_sources=None,
        fail_probe=False,
        probe_error=None,
    ):
        self.fail_after_successful_sources = fail_after_successful_sources
        self.fail_probe = fail_probe
        self.probe_error = probe_error
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
            if self.probe_error is not None:
                raise self.probe_error
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

    def tracked_delete(notebook_id, **kwargs):
        delete_calls.append(notebook_id)
        return real_delete(notebook_id, **kwargs)

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


def test_empty_probe_response_is_persisted_as_actionable_model_failure(repo):
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(
        repo,
        "kg_extract",
        _ControlledKgClient(
            probe_error=ModelProviderError(
                "empty content", code="malformed_response"
            )
        ),
    )
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, job["id"], "incremental"
        )

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["stage"] == "finished"
    assert saved["error_code"] == "model_response_invalid"
    assert saved["error_message"] == MODEL_RESPONSE_INVALID_MESSAGE
    assert saved["completed_sources"] == 0


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
        control, controlled_client, progress=None, on_abort=None, **_kwargs,
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


class _InterruptOneDrainOther:
    """一个来源抛 KeyboardInterrupt,另一个仍卡在模型调用里。后者只在看到熔断标志
    后再多睡一小会儿才返回,于是「上层是否等它排空」变成可判定的事实。"""

    configured = True
    model = "test-kg"

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.blocked_started = threading.Event()
        self.blocked_returned = threading.Event()
        self.control = None

    def chat_json(self, messages, response_schema_hint, **kwargs):
        prompt = messages[0]["content"]
        if prompt.startswith('Return {"ok":true}'):
            return '{"ok":true}'
        with self.lock:
            self.calls += 1
            first = self.calls == 1
        if first:
            # 先等另一个 worker 真的进到模型调用里再抛中断。否则(尤其在机器繁忙、
            # 线程启动被拖慢时)它可能还没启动就被 cancel() 正确地取消掉,于是「有没有
            # 排空」根本无从判定——那测的是取消而不是排空。
            self.blocked_started.wait(10)
            raise KeyboardInterrupt("ctrl-c during extraction")
        self.blocked_started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.control is not None and self.control.aborted:
                break
            time.sleep(0.01)
        time.sleep(0.15)  # 若上层不排空,主线程会在这段时间里抢先返回
        self.blocked_returned.set()
        raise self._connection_error()

    @staticmethod
    def _connection_error():
        return APIConnectionError(
            request=httpx.Request(
                "POST", "https://model.example/chat/completions"
            )
        )


def test_interrupt_drains_in_flight_sources_before_releasing_the_guard(
    repo, monkeypatch
):
    """跨进程单飞守卫在 job 落终态那一刻就放开,所以必须先排空在飞 worker:否则
    活着的后端能立刻为同一 notebook 起新分析,而本进程的旧 worker 还在写图/改来源
    状态,把新任务的状态冲掉(codex 评审 P1)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    client = _InterruptOneDrainOther()
    bind_chat_client(repo, "kg_extract", client)
    kg_scheduler.reset()
    kg_scheduler.configure(window_workers=2, job_workers=3)
    lifecycle = repo._runtime.knowledge_lifecycle
    original = lifecycle._extract_targets

    def _capture(*args, **kwargs):
        # 位置 6 是本次运行的 control(见 _extract_targets 签名)。
        client.control = args[5]
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_extract_targets", _capture)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    assert client.blocked_started.is_set(), (
        "前提未成立:没有第二个来源真的进到模型调用里,本例无法判定排空"
    )
    assert client.blocked_returned.is_set(), (
        "在飞来源尚未排空就放开了单飞守卫"
    )
    assert repo._runtime.kg_build_jobs.get(job["id"])["status"] == "failed"


class _BlockUntilAbortedKgClient:
    """进到模型调用就停住,直到本次运行被 abort;之后再多睡一会儿才返回,于是
    「上层有没有等它排空」是可判定的。"""

    configured = True
    model = "test-kg"

    def __init__(self):
        self.blocked_started = threading.Event()
        self.blocked_returned = threading.Event()
        self.control = None

    def chat_json(self, messages, response_schema_hint, **kwargs):
        if messages[0]["content"].startswith('Return {"ok":true}'):
            return '{"ok":true}'
        self.blocked_started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.control is not None and self.control.aborted:
                break
            time.sleep(0.01)
        time.sleep(0.15)
        self.blocked_returned.set()
        raise APIConnectionError(
            request=httpx.Request(
                "POST", "https://model.example/chat/completions"
            )
        )


def _capture_run_control(repo, monkeypatch, client):
    """把本次运行的 control 交给测试用的模型客户端(位置 6,见 _extract_targets)。"""
    lifecycle = repo._runtime.knowledge_lifecycle
    original = lifecycle._extract_targets

    def _capture(*args, **kwargs):
        client.control = args[5]
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_extract_targets", _capture)


def test_interrupt_while_still_submitting_drains_what_was_submitted(
    repo, monkeypatch
):
    """任务已入池、submit_job 尚未把 executor Future 返回时也必须可排空。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    client = _BlockUntilAbortedKgClient()
    bind_chat_client(repo, "kg_extract", client)
    kg_scheduler.reset()
    kg_scheduler.configure(window_workers=2, job_workers=3)
    _capture_run_control(repo, monkeypatch, client)
    real_submit = kg_scheduler.submit_job
    def _submit(fn, /, *args, **kwargs):
        # real_submit 已接受任务，故 wrapper token 会被 worker 认领；但用中断
        # 代替 executor Future 的正常返回，精确覆盖 enqueue→return 的缝隙。
        real_submit(fn, *args, **kwargs)
        client.blocked_started.wait(10)
        raise KeyboardInterrupt("ctrl-c after accept before submit returned")

    monkeypatch.setattr(kg_scheduler, "submit_job", _submit)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    assert client.blocked_started.is_set(), "前提未成立:第一个来源没进到模型调用"
    assert client.blocked_returned.is_set(), (
        "提交期间被中断时,已提交的在飞来源没有被排空"
    )
    assert repo._runtime.kg_build_jobs.get(job["id"])["status"] == "failed"


def test_interrupt_before_executor_accept_does_not_wait_on_bare_token(
    repo, monkeypatch
):
    """提交尚未入池时的裸 token 只能 cancel，不能交给 Future.wait。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    kg_scheduler.reset()
    kg_scheduler.configure(window_workers=2, job_workers=3)

    def _interrupt_before_accept(fn, /, *args, **kwargs):
        raise KeyboardInterrupt("ctrl-c before executor accepted the wrapper")

    def _must_not_wait(futures, *args, **kwargs):
        raise AssertionError("a pre-accept cancelled token must not be waited")

    monkeypatch.setattr(kg_scheduler, "submit_job", _interrupt_before_accept)
    monkeypatch.setattr(concurrent.futures, "wait", _must_not_wait)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "worker_interrupted"


def test_interrupt_after_the_insert_but_before_create_job_returns_settles(
    repo, monkeypatch
):
    """create_job 先 INSERT 再 get(job_id) 返回。信号落在这两者之间时 job 未赋值,
    调用方的守卫也进不去,行永久留在 running(评审 P2)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    store = repo._runtime.kg_build_jobs
    real_get = store.get
    fired = {"n": 0}

    def _get(job_id):
        if not fired["n"]:
            fired["n"] = 1  # 行已提交,但 create_job 还没返回
            raise KeyboardInterrupt("ctrl-c between insert and return")
        return real_get(job_id)

    monkeypatch.setattr(store, "get", _get)

    with pytest.raises(KeyboardInterrupt):
        repo.prepare_notebook_kg_job(notebook.id, "incremental")

    saved = store.latest(notebook.id)
    assert saved is not None and saved["status"] == "failed"
    assert saved["error_code"] == "worker_interrupted"
    assert repo.get_notebook(notebook.id).kg_building is False
    assert repo.prepare_notebook_kg_job(notebook.id, "incremental")["id"]


def test_interrupt_before_the_insert_never_touches_a_foreign_running_row(
    repo, monkeypatch
):
    """反向护栏:中断若落在 INSERT **之前**,最新那行不是本次建的——它可能属于一个仍
    活着的后端进程。离线进程无权裁决它,必须原样不动。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    store = repo._runtime.kg_build_jobs
    foreign = store.create_job(notebook.id, "someone-else", "incremental", 5)
    monkeypatch.setattr(
        store, "create_job",
        lambda *a, **k: (_ for _ in ()).throw(
            KeyboardInterrupt("ctrl-c before the insert")
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        repo.prepare_notebook_kg_job(notebook.id, "incremental")

    assert store.get(foreign["id"])["status"] == "running", (
        "动了别人的进行中任务"
    )


@pytest.mark.parametrize(
    "step", ["kg_build_started", "publish_pending"],
)
def test_interrupt_inside_preparation_after_the_row_exists_settles(
    repo, monkeypatch, step
):
    """create_job 之后 prepare 还要走三步(登记进程内标志、写事件、推送待办)。信号落在
    这里会直接逃出 prepare,连调用方的兜底都进不去,行就永久留在 running(评审 P1)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    lifecycle = repo._runtime.knowledge_lifecycle
    # 只打断第一次(收尾本身以及末尾「能否再次发起」的复查都还要走这些方法)。
    fired = {"n": 0}
    if step == "kg_build_started":
        original = lifecycle._emit_kg_build_event

        def _emit(kind, job, **kwargs):
            if kind == "kg_build_started" and not fired["n"]:
                fired["n"] = 1
                raise KeyboardInterrupt("ctrl-c right after the row was created")
            return original(kind, job, **kwargs)

        monkeypatch.setattr(lifecycle, "_emit_kg_build_event", _emit)
    else:
        original_publish = lifecycle._publish_pending_started

        def _publish():
            if not fired["n"]:
                fired["n"] = 1
                raise KeyboardInterrupt("ctrl-c while publishing pending state")
            return original_publish()

        monkeypatch.setattr(lifecycle, "_publish_pending_started", _publish)

    with pytest.raises(KeyboardInterrupt):
        repo.prepare_notebook_kg_job(notebook.id, "incremental")

    saved = repo._runtime.kg_build_jobs.latest(notebook.id)
    assert saved is not None and saved["status"] == "failed"
    assert saved["error_code"] == "worker_interrupted"
    # 守卫已释放:数据库层与界面层都要放开。
    assert repo.get_notebook(notebook.id).kg_building is False
    assert repo.prepare_notebook_kg_job(notebook.id, "incremental")["id"]


@pytest.mark.parametrize("mode", ["incremental", "rebuild"])
def test_interrupt_between_job_creation_and_run_still_settles(
    repo, monkeypatch, mode
):
    """行已建、但 _run_notebook_kg_job 还没进到自己的保护区之间也有窗口(事件落盘、
    待办推送、一次 DB 读)。信号落在这里时若没人收尾,行就永久留在 running,该 notebook
    之后的分析全被挡死——正是本改动要消灭的状态(评审 P1)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(
        lifecycle, "_run_notebook_kg_job",
        lambda *a, **k: (_ for _ in ()).throw(
            KeyboardInterrupt("ctrl-c right after the row was created")
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        if mode == "incremental":
            repo.build_notebook_kg(notebook.id)
        else:
            repo.rebuild_notebook_kg(notebook.id)

    saved = repo._runtime.kg_build_jobs.latest(notebook.id)
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["error_code"] == "worker_interrupted"
    # 进程内的构建标志也必须清掉:否则 get_notebook()/索引状态会在该进程余生都报
    # 「构建中」,界面上的分析入口一直禁用,尽管数据库层已经允许新任务(评审 P2)。
    assert repo.get_notebook(notebook.id).kg_building is False
    # 真正的回归点:守卫已释放,同一 notebook 能再次发起分析。
    assert repo.prepare_notebook_kg_job(notebook.id, "incremental")["id"]


def test_repeated_interrupt_cannot_skip_the_drain(repo, monkeypatch):
    """排空那次等待可能长到一次模型超时,运维会「再按一次 Ctrl-C」。第二个信号若能
    逃出去,外层就会在 future 未排空时落终态、放开跨进程守卫(评审 P1)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    # 必须走真实的 _extract_targets:排空(以及它对重复中断的抵抗)就在那里面。
    client = _InterruptOneDrainOther()
    bind_chat_client(repo, "kg_extract", client)
    kg_scheduler.reset()
    kg_scheduler.configure(window_workers=2, job_workers=3)
    _capture_run_control(repo, monkeypatch, client)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    waits = {"n": 0}
    real_wait = concurrent.futures.wait

    def _wait(fs, *args, **kwargs):
        waits["n"] += 1
        if waits["n"] == 1:  # 模拟排空等待期间第二次 Ctrl-C
            raise KeyboardInterrupt("second ctrl-c during drain")
        return real_wait(fs, *args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "wait", _wait)

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    # 断言不变量本身(在飞来源确实排空完了),而不是「wait 被调了几次」这种代理指标
    # ——后者会因为别处也调 wait 而为错误的原因通过。
    assert client.blocked_started.is_set(), "前提未成立:没有来源真的在飞"
    assert client.blocked_returned.is_set(), (
        "第二次中断绕过了排空:守卫已放开而在飞来源还在跑"
    )
    assert waits["n"] >= 2, "排空等待没有被重试"
    assert repo._runtime.kg_build_jobs.get(job["id"])["status"] == "failed"


def test_repeated_interrupt_during_settlement_still_settles(repo, monkeypatch):
    """第二个信号也可能落在**落终态**这一段(公布 stopping / finish)。若它逃出去,行就
    永久留在 running,该 notebook 之后的分析全被挡死(评审 P1:整段收尾都要顶住)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    _interrupt_during_extraction(repo, monkeypatch)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    real_finish = repo._runtime.kg_build_jobs.finish
    calls = {"n": 0}

    def _finish(job_id, status, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 模拟落终态期间第二次 Ctrl-C
            raise KeyboardInterrupt("second ctrl-c while settling")
        return real_finish(job_id, status, **kwargs)

    monkeypatch.setattr(repo._runtime.kg_build_jobs, "finish", _finish)

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    # 效果断言:那行真的落到了终态(而不是「finish 被调了几次」这种代理指标)。
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed", "重复中断把行留在了 running"
    assert saved["error_code"] == "worker_interrupted"
    assert repo.prepare_notebook_kg_job(notebook.id, "incremental")["id"]


def test_failed_stopping_publication_never_retries_as_a_circuit_event(
    repo, monkeypatch
):
    """若「正在停止」那次写库撞上瞬时错误,stopping_marked 仍为 False,默认
    notify=True 的 abort 会经 on_abort 以 circuit=True 重试一次——一次人工中断就被
    记成模型熔断。宁可不重试那个瞬时中间态,也不能记错(评审 P2)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    _interrupt_during_extraction(repo, monkeypatch)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    real_set_stage = repo._runtime.kg_build_jobs.set_stage
    stopping_attempts = {"n": 0}

    def _set_stage(job_id, stage, **kwargs):
        if stage != "stopping":
            return real_set_stage(job_id, stage, **kwargs)
        # 「正在停止」第一次瞬时失败,**第二次成功**——只有这样才会走到评审描述的
        # 场景:重试成功了,于是那条 circuit=True 的回调把人工中断记成熔断。
        stopping_attempts["n"] += 1
        if stopping_attempts["n"] == 1:
            raise RuntimeError("database is locked")
        return real_set_stage(job_id, stage, **kwargs)

    monkeypatch.setattr(repo._runtime.kg_build_jobs, "set_stage", _set_stage)

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    kinds = [str(event.get("kind", "")) for event in events]
    assert "kg_build_circuit_opened" not in kinds
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "worker_interrupted"


def test_interrupt_after_success_keeps_the_succeeded_row_and_event(
    repo, monkeypatch
):
    """信号落在 finish(succeeded) 之后、函数返回之前:三次写都命中 0 行,任务其实
    已成功提交,不能再发一条带 status="succeeded" 的 kg_build_failed(评审 P2)。"""
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    lifecycle = repo._runtime.knowledge_lifecycle
    kinds = []
    original = lifecycle._emit_kg_build_event

    def _emit(kind, job, **kwargs):
        kinds.append(kind)
        original(kind, job, **kwargs)
        if kind == "kg_build_succeeded":
            raise KeyboardInterrupt("ctrl-c right after commit")

    monkeypatch.setattr(lifecycle, "_emit_kg_build_event", _emit)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KeyboardInterrupt):
        repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["error_code"] == ""
    assert "kg_build_succeeded" in kinds
    assert "kg_build_failed" not in kinds


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


class _PerSourceFailingKgClient:
    """按提示词内容决定失败的抽取 client：用来区分"某一篇的模型调用挂了"与"服务挂了"。"""

    configured = True
    model = "test-kg"

    def __init__(self, failing_marker):
        self.failing_marker = failing_marker
        self.lock = threading.Lock()
        self.source_calls = 0

    def chat_json(self, messages, response_schema_hint, **kwargs):
        rendered = "".join(message["content"] for message in messages)
        if rendered.startswith('Return {"ok":true}'):
            return '{"ok":true}'
        with self.lock:
            self.source_calls += 1
        if self.failing_marker in rendered:
            raise _ControlledKgClient._connection_error()
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


def test_skip_mode_skips_the_failing_source_and_builds_the_rest(repo):
    """离线跳过模式:模型只在某一篇上不可用时,跳过它继续跑完其余来源。

    默认(不传 skip_policy)仍是整任务熔断——由
    test_model_outage_preserves_completed_source_and_stops_remaining 钉住。
    """
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(
        repo, "kg_extract", _PerSourceFailingKgClient("for source 1."),
    )
    policy = ModelSkipPolicy(max_consecutive=32)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    result = repo.build_notebook_kg(
        notebook.id, job_id=job["id"], skip_policy=policy
    )

    assert result["model_skipped"] == [source_ids[1]]
    assert policy.skipped == [source_ids[1]]
    assert sorted(result["built"]) == [source_ids[0], source_ids[2]]
    # model_skipped 是 failed 的子集:任务账目照旧把它算作一次未建成。
    assert result["failed"] == [source_ids[1]]
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "succeeded"
    statuses = _source_statuses(repo, source_ids)
    assert "extracting" not in statuses.values()
    # 被跳过的来源退回未分析状态 → 重跑本命令即自动重试它。
    assert statuses[source_ids[1]] == "parsed"
    assert _kg_source_ids(repo, notebook.id) == {
        source_ids[0], source_ids[2],
    }


def test_skip_mode_still_stops_the_job_when_the_service_is_really_down(repo):
    """兜底闸:连续失败到阈值仍走原来的任务级熔断,不会对着挂掉的服务跑完全库。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(
        repo, "kg_extract", _PerSourceFailingKgClient("Engram is"),
    )
    policy = ModelSkipPolicy(max_consecutive=2)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted) as raised:
        repo.build_notebook_kg(
            notebook.id, job_id=job["id"], skip_policy=policy
        )

    assert raised.value.failure.code == "model_unavailable"
    assert policy.skipped == source_ids[:2]
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["stage"] == "finished"
    assert saved["error_code"] == "model_unavailable"
    statuses = _source_statuses(repo, source_ids)
    assert "extracting" not in statuses.values()
    assert _kg_source_ids(repo, notebook.id) == set()


class _JobAbortingKgClient:
    """模拟"别的 worker 已经把任务级熔断拉起来了"：抽取时先熔断任务再抛模型错误。"""

    configured = True
    model = "test-kg"
    control = None

    def chat_json(self, messages, response_schema_hint, **kwargs):
        rendered = "".join(message["content"] for message in messages)
        if rendered.startswith('Return {"ok":true}'):
            return '{"ok":true}'
        self.control.abort(
            KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE),
            notify=False,
        )
        raise _ControlledKgClient._connection_error()


def test_skip_mode_never_downgrades_a_job_level_abort_into_a_skip(
    repo, monkeypatch
):
    """并发下另一个 worker 升级熔断/Ctrl-C 时,本来源的 KgBuildAborted 必须原样上抛。

    没有 `control.aborted` 这道复核,任务级熔断会被当成"跳过这一个源"记账,
    跳过模式下的任务就再也停不下来了。
    """
    notebook, source_ids = _seed_three_parsed_sources(repo)
    client = _JobAbortingKgClient()
    bind_chat_client(repo, "kg_extract", client)
    _capture_run_control(repo, monkeypatch, client)
    policy = ModelSkipPolicy(max_consecutive=32)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.build_notebook_kg(
            notebook.id, job_id=job["id"], skip_policy=policy
        )

    # 阈值远未到(32),停下来的唯一原因只能是任务级熔断被原样传导。
    assert policy.skipped == []
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    statuses = _source_statuses(repo, source_ids)
    assert "extracting" not in statuses.values()


def test_skip_mode_still_fails_fast_when_the_startup_probe_fails(repo):
    """反向护栏:起始探测失败仍然停掉整个任务,跳过模式**不**放行(codex 第 1 轮 P2 驳回)。

    探测回答的是"服务现在活着吗"。失败即此刻不可用,而用户尚无任何投入——停下来
    与重跑的代价都是零。放行反而更坏:会对着已知挂掉的服务逐个来源白烧超时,凑满
    连续阈值后照样死,只是多留下几十个被标记跳过的来源。改动此行为请先改这条用例。
    """
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient(fail_probe=True))
    policy = ModelSkipPolicy(max_consecutive=32)
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.build_notebook_kg(
            notebook.id, job_id=job["id"], skip_policy=policy
        )

    assert policy.skipped == []
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "model_unavailable"
    # 一个来源都没被动过:探测在抽取开始之前就把任务停住了。
    assert set(_source_statuses(repo, source_ids).values()) == {"parsed"}
    assert _kg_source_ids(repo, notebook.id) == set()


# ---------------------------------------------------------------------------
# 批 3·W4 T-W4-1(WR-3):「全部重新分析」同步半程瘦身。
# 准入半程不再枚举整库(create_job 落 total_sources=0 + stage='probing'),
# worker 起跑后用同一套谓词自算并回填,且必须落在 set_stage("extracting")
# 之前——否则大库用户会盯着「正在分析 0/0 项内容」几十秒。
# ---------------------------------------------------------------------------


def _record_target_enumerations(lifecycle, monkeypatch, sink):
    """把 ``_kg_target_batches`` 的每次调用(含 ``_kg_target_count`` 那次)记进
    ``sink``,并保持真实行为。"""
    real_batches = lifecycle._kg_target_batches

    def spy(notebook_id, mode, **kwargs):
        sink.append(("batches", mode))
        return real_batches(notebook_id, mode, **kwargs)

    monkeypatch.setattr(lifecycle, "_kg_target_batches", spy)


def _record_job_writes(jobs, monkeypatch, sink):
    """记录 ``extend_total_sources`` / ``set_stage`` 的调用**顺序**。"""
    real_extend = jobs.extend_total_sources
    real_stage = jobs.set_stage

    def extend(job_id, extra):
        sink.append(("extend", extra))
        return real_extend(job_id, extra)

    def set_stage(job_id, stage, **kwargs):
        sink.append(("stage", stage))
        return real_stage(job_id, stage, **kwargs)

    monkeypatch.setattr(jobs, "extend_total_sources", extend)
    monkeypatch.setattr(jobs, "set_stage", set_stage)


def test_prepare_does_not_enumerate_targets_and_starts_at_zero_total(
    repo, monkeypatch
):
    """准入半程(202 请求路径)一次目标枚举都不做:create_job 收到 0,
    kg_build_started 的 total_sources 也是 0,分母由 worker 起跑后回填。
    整库枚举每行带 5 个相关子查询,压在同步半程上就是大库「全部重新分析」
    按下去之后几十秒没有响应。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    lifecycle = repo._runtime.knowledge_lifecycle
    enumerations = []
    _record_target_enumerations(lifecycle, monkeypatch, enumerations)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)

    job = repo.prepare_notebook_kg_job(
        notebook.id, "incremental", retry_partial=True
    )

    assert enumerations == []
    assert job["total_sources"] == 0
    assert job["stage"] == "probing"
    started = [
        event for event in events if event.get("kind") == "kg_build_started"
    ]
    assert [event["total_sources"] for event in started] == [0]

    repo.execute_notebook_kg_job(
        notebook.id, job["id"], "incremental", retry_partial=True
    )

    # worker 侧才枚举:一次计数 + 主循环,加上链 b 补漏轮。
    assert [mode for _label, mode in enumerations] == ["incremental"] * 3
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["total_sources"] == len(source_ids)
    assert saved["completed_sources"] == len(source_ids)


def test_worker_counts_after_delete_and_backfills_before_extracting_stage(
    repo, monkeypatch
):
    """回填的位置有三条硬约束,这条用例把它们钉成调用序:
    delete_notebook_kg → 目标计数 → extend_total_sources → set_stage
    ("extracting")。计数放在 delete 之前会与主循环看到两个不同的世界;
    放在 set_stage 之后,前端就从「正在连接模型服务…」直接跳到
    「正在分析 0/0 项内容」。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    lifecycle = repo._runtime.knowledge_lifecycle
    jobs = repo._runtime.kg_build_jobs
    calls = []
    real_delete = lifecycle.delete_notebook_kg

    def tracked_delete(notebook_id, **kwargs):
        calls.append(("delete", notebook_id))
        return real_delete(notebook_id, **kwargs)

    monkeypatch.setattr(lifecycle, "delete_notebook_kg", tracked_delete)
    _record_target_enumerations(lifecycle, monkeypatch, calls)
    _record_job_writes(jobs, monkeypatch, calls)

    job = repo.prepare_notebook_kg_job(notebook.id, "rebuild")
    repo.execute_notebook_kg_job(notebook.id, job["id"], "rebuild")

    prefix = [entry for entry in calls if entry[0] != "batches"][:2]
    assert calls[0] == ("delete", notebook.id)
    # 非 preserve 的 rebuild 删完再数,谓词降级成 incremental(与主循环同款)。
    assert calls[1] == ("batches", "incremental")
    assert prefix == [
        ("delete", notebook.id),
        ("extend", len(source_ids)),
    ]
    extend_at = calls.index(("extend", len(source_ids)))
    extracting_at = calls.index(("stage", "extracting"))
    assert extend_at < extracting_at
    assert calls.index(("batches", "incremental")) < extend_at


@pytest.mark.parametrize(
    "shape", ["incremental", "rebuild_preserve", "rebuild_replace"]
)
def test_worker_total_matches_the_main_loop_for_every_target_shape(repo, shape):
    """三形态的分母都必须等于主循环真正跑掉的那批来源。
    incremental 只数未分析的;rebuild+preserve 用 rebuild 谓词数全部有元素的;
    rebuild 非 preserve 删完之后按 incremental 数,同样是全部。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    # 先分析掉一个来源,让 incremental 与 rebuild 的目标集真的不同。
    seed = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    seeded = repo.build_notebook_kg(
        notebook.id, job_id=seed["id"], target_limit=1
    )
    assert len(seeded["built"]) == 1

    if shape == "incremental":
        job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
        result = repo.execute_notebook_kg_job(
            notebook.id, job["id"], "incremental"
        )
        expected = len(source_ids) - 1
    else:
        # preserve_existing_rebuild 不在 facade 签名上(索引管线专用),
        # 走 lifecycle service 本身。
        job = repo.prepare_notebook_kg_job(notebook.id, "rebuild")
        result = repo._runtime.knowledge_lifecycle.execute_notebook_kg_job(
            notebook.id,
            job["id"],
            "rebuild",
            preserve_existing_rebuild=(shape == "rebuild_preserve"),
        )
        expected = len(source_ids)

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert len(result["built"]) == expected
    assert saved["total_sources"] == expected
    assert saved["completed_sources"] == expected


def test_startup_probe_still_runs_on_a_nonempty_incremental_build(repo):
    """反向护栏(设计稿约束 4):``total_targets`` 若还读 ``job["total_sources"]``
    的快照,落 0 之后 ``mode != "rebuild" and total_targets`` 恒假,增量构建的
    起始探测会被**静默**跳过。分母改读 worker 自算值后,有目标就照常探测,
    没目标就照常不探测。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    client = _ControlledKgClient()
    bind_chat_client(repo, "kg_extract", client)

    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    repo.execute_notebook_kg_job(notebook.id, job["id"], "incremental")
    assert client.probes == 1
    assert client.source_calls >= len(source_ids)

    idle_client = _ControlledKgClient()
    bind_chat_client(repo, "kg_extract", idle_client)
    idle = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    repo.execute_notebook_kg_job(notebook.id, idle["id"], "incremental")

    assert idle_client.probes == 0
    assert repo._runtime.kg_build_jobs.get(idle["id"])["total_sources"] == 0


def test_repair_run_counts_partial_sources_and_still_probes(repo):
    """``retry_partial`` 直传计数的反向钉子(T1 双内评 P2):全-partial 笔记本
    上「分析新增」恒传 ``retry_partial=True``——计数若丢掉这个直传,会数出
    0:起始探测被**静默**跳过(约束 4 的失败形态)、持久 total 停在 0 而
    completed 走到 1,前端渲染「已完成 1/0 项内容」的不可能进度。"""
    notebook, source_ids = _seed_three_parsed_sources(repo)
    bind_chat_client(repo, "kg_extract", _ControlledKgClient())
    seed = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    seeded = repo.execute_notebook_kg_job(
        notebook.id, seed["id"], "incremental"
    )
    assert len(seeded["built"]) == len(source_ids)

    # 把一个已分析源改判成 partial(status 仍 completed):普通 incremental
    # 谓词跳过它,repair 谓词(retry_partial=True)选中它。
    with repo._write() as db:
        db.execute(
            "UPDATE extraction_runs SET error_message='windows_failed=1/3' "
            "WHERE source_id=?",
            (source_ids[0],),
        )
    from app.repositories.sqlite import knowledge_counts_cache
    knowledge_counts_cache.invalidate(notebook.id)

    repair_client = _ControlledKgClient()
    bind_chat_client(repo, "kg_extract", repair_client)
    job = repo.prepare_notebook_kg_job(
        notebook.id, "incremental", retry_partial=True
    )
    repo.execute_notebook_kg_job(
        notebook.id, job["id"], "incremental", retry_partial=True
    )

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert repair_client.probes == 1
    assert saved["total_sources"] == 1
    assert saved["completed_sources"] == 1
