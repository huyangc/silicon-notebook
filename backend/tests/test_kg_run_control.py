from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.domain.cancellation import AskCancelled
from app.services.kg.run_control import (
    MODEL_RESPONSE_INVALID_MESSAGE,
    MODEL_UNAVAILABLE_MESSAGE,
    KgBuildAborted,
    KgBuildFailure,
    KgExtractionRunControl,
    TaskScopedKgClient,
    probe_kg_model,
)
from tests.model_testkit import RecordingModelProvider
from app.services.model_work import (
    ModelProviderError,
    ModelQueueFull,
    ModelQueueTimeout,
    ModelServiceUnavailable,
)


def _settings(retries):
    return SimpleNamespace(
        kg_llm_timeout_seconds=60,
        kg_llm_max_retries=retries,
    )


def _connection_error():
    return APIConnectionError(
        request=httpx.Request("POST", "https://model.example/chat/completions")
    )


def _timeout_error():
    return APITimeoutError(
        request=httpx.Request("POST", "https://model.example/chat/completions")
    )


def _status_error(status):
    request = httpx.Request("POST", "https://model.example/chat/completions")
    response = httpx.Response(
        status,
        request=request,
        json={"error": {"message": f"status {status}"}},
    )
    return APIStatusError(
        f"status {status}",
        response=response,
        body=response.json(),
    )


class _SequenceClient:
    configured = True
    model = "test-model"

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0
        self.kwargs = []

    def chat_json(self, messages, response_schema_hint, **kwargs):
        self.kwargs.append(kwargs)
        index = self.calls
        self.calls += 1
        behavior = (
            self.behaviors[index]
            if index < len(self.behaviors)
            else self.behaviors[-1]
        )
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _BlockingFailureClient:
    configured = True
    model = "test-model"

    def __init__(self, entered):
        self.entered = entered
        self.calls = 0

    def chat_json(self, messages, response_schema_hint, **kwargs):
        self.calls += 1
        self.entered.set()
        raise _connection_error()


class _CancellationAwareClient:
    configured = True
    model = "test-model"

    def __init__(self, entered):
        self.entered = entered
        self.cancel_event = None

    def chat_json(
        self, messages, response_schema_hint, *, cancel_event=None, **kwargs
    ):
        self.cancel_event = cancel_event
        self.entered.set()
        assert cancel_event is not None
        assert cancel_event.wait(1)
        raise AskCancelled()


def test_transient_exhaustion_opens_only_its_run(monkeypatch):
    monkeypatch.setattr(
        "app.services.kg.run_control.random.uniform",
        lambda *_args: 0,
    )
    a = KgExtractionRunControl("job-a")
    b = KgExtractionRunControl("job-b")
    backoffs = []
    monkeypatch.setattr(a, "wait_backoff", backoffs.append)
    delegate = _SequenceClient(
        [_connection_error(), _connection_error(), _connection_error()]
    )
    client = TaskScopedKgClient(delegate, _settings(retries=2), a)

    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")

    assert raised.value.failure.code == "model_unavailable"
    assert delegate.calls == 3
    assert backoffs == [1, 2]
    assert all(call["timeout"] == 60 for call in delegate.kwargs)
    assert all(call["max_retries"] == 0 for call in delegate.kwargs)
    assert a.aborted is True
    assert b.aborted is False


def test_success_before_limit_does_not_open_circuit(monkeypatch):
    monkeypatch.setattr(
        "app.services.kg.run_control.random.uniform",
        lambda *_args: 0,
    )
    control = KgExtractionRunControl("job-a")
    backoffs = []
    monkeypatch.setattr(control, "wait_backoff", backoffs.append)
    delegate = _SequenceClient([_timeout_error(), '{"ok":true}'])
    client = TaskScopedKgClient(delegate, _settings(retries=2), control)
    assert (
        client.chat_json([{"role": "user", "content": "x"}], "{}")
        == '{"ok":true}'
    )
    assert backoffs == [1]
    assert control.aborted is False


def test_auth_failure_is_immediate():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([_status_error(401)])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")
    assert raised.value.failure.code == "model_auth_failed"
    assert delegate.calls == 1


def test_other_http_rejection_is_immediate():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([_status_error(404)])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")
    assert raised.value.failure.code == "model_request_rejected"
    assert delegate.calls == 1


def test_malformed_response_outside_probe_remains_locally_isolated():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([
        ModelProviderError("empty content", code="malformed_response")
    ])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)

    with pytest.raises(ModelProviderError) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")

    assert raised.value.code == "malformed_response"
    assert delegate.calls == 1
    assert control.aborted is False


@pytest.mark.parametrize(
    "error",
    [
        ModelQueueFull(support_id="mdl-kg-full"),
        ModelQueueTimeout(support_id="mdl-kg-timeout"),
        ModelServiceUnavailable(support_id="mdl-kg-unavailable"),
    ],
)
def test_scheduler_admission_failures_pause_kg_as_model_unavailable(error):
    control = KgExtractionRunControl("job-queue")
    delegate = _SequenceClient([error])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)

    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")

    assert raised.value.failure.code == "model_unavailable"
    assert raised.value.failure.user_message == MODEL_UNAVAILABLE_MESSAGE
    assert delegate.calls == 1


def test_abort_wakes_retry_backoff(monkeypatch):
    monkeypatch.setattr(
        "app.services.kg.run_control.random.uniform",
        lambda *_args: 30,
    )
    control = KgExtractionRunControl("job-a")
    entered = threading.Event()
    delegate = _BlockingFailureClient(entered)
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.chat_json,
            [{"role": "user", "content": "x"}],
            "{}",
        )
        assert entered.wait(1)
        control.abort(
            KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)
        )
        with pytest.raises(KgBuildAborted):
            future.result(timeout=1)
    assert delegate.calls == 1


def test_abort_interrupts_stream_and_preserves_kg_failure():
    control = KgExtractionRunControl("job-a")
    entered = threading.Event()
    delegate = _CancellationAwareClient(entered)
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    expected = KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.chat_json,
            [{"role": "user", "content": "x"}],
            "{}",
        )
        assert entered.wait(1)
        assert delegate.cancel_event is control.cancel_event
        control.abort(expected)
        with pytest.raises(KgBuildAborted) as raised:
            future.result(timeout=1)

    assert raised.value.failure is expected


def test_first_abort_failure_wins():
    control = KgExtractionRunControl("job-a")
    first = KgBuildFailure("first", "first message")
    second = KgBuildFailure("second", "second message")
    assert control.abort(first) is first
    assert control.abort(second) is first
    with pytest.raises(KgBuildAborted) as raised:
        control.raise_if_aborted()
    assert raised.value.failure is first


def test_abort_is_not_published_until_first_callback_finishes():
    callback_started = threading.Event()
    release_callback = threading.Event()
    second_started = threading.Event()
    observer_started = threading.Event()
    callbacks = []

    def on_abort(failure):
        callbacks.append(failure)
        callback_started.set()
        assert release_callback.wait(2)

    control = KgExtractionRunControl("job-a", on_abort=on_abort)
    first = KgBuildFailure("first", "first message")
    second = KgBuildFailure("second", "second message")

    def abort_second():
        second_started.set()
        return control.abort(second)

    def observe_abort():
        observer_started.set()
        control.raise_if_aborted()

    with ThreadPoolExecutor(max_workers=3) as executor:
        first_future = executor.submit(control.abort, first)
        assert callback_started.wait(1)
        assert control.aborted is False
        assert control.failure is None

        second_future = executor.submit(abort_second)
        observer_future = executor.submit(observe_abort)
        assert second_started.wait(1)
        assert observer_started.wait(1)
        assert second_future.done() is False
        assert observer_future.done() is False

        release_callback.set()
        assert first_future.result(timeout=1) is first
        assert second_future.result(timeout=1) is first
        with pytest.raises(KgBuildAborted) as raised:
            observer_future.result(timeout=1)

    assert callbacks == [first]
    assert control.aborted is True
    assert raised.value.failure is first


def test_abort_callback_failure_preserves_classified_failure():
    failure = KgBuildFailure("model_unavailable", "safe message")

    def reject_publication(_failure):
        raise RuntimeError("status persistence failed")

    control = KgExtractionRunControl(
        "job-a",
        on_abort=reject_publication,
    )

    assert control.abort(failure) is failure
    assert control.aborted is True
    with pytest.raises(KgBuildAborted) as raised:
        control.raise_if_aborted()
    assert raised.value.failure is failure


def test_unclassified_failure_does_not_open_circuit():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([ValueError("malformed model payload")])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with pytest.raises(ValueError, match="malformed model payload"):
        client.chat_json([{"role": "user", "content": "x"}], "{}")
    assert delegate.calls == 1
    assert control.aborted is False


def test_malformed_json_result_remains_soft():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient(["not json"])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    assert (
        client.chat_json([{"role": "user", "content": "x"}], "{}")
        == "not json"
    )
    assert control.aborted is False


def test_probe_reuses_configured_short_output_budget_and_bypasses_cache():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient(['{"ok":true}'])
    settings = _settings(retries=2)
    client = TaskScopedKgClient(delegate, settings, control)
    probe_kg_model(client)
    assert len(delegate.kwargs) == 1
    assert delegate.kwargs[0] == {
        "bypass_cache": True,
        "timeout": 60,
        "max_retries": 0,
        "cancel_event": control.cancel_event,
    }
    assert client.configured is True
    assert client.model == "test-model"
    assert client.settings is settings


def test_probe_classifies_malformed_response_without_widening_client_policy():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([
        ModelProviderError("empty content", code="malformed_response")
    ])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)

    with pytest.raises(KgBuildAborted) as raised:
        probe_kg_model(client)

    assert raised.value.failure.code == "model_response_invalid"
    assert raised.value.failure.user_message == MODEL_RESPONSE_INVALID_MESSAGE
    assert delegate.calls == 1
    assert control.aborted is True


def test_kg_ingest_resolves_extract_refine_and_glean_before_window_submit(
    monkeypatch,
):
    from app.services import kg_ingest

    clients = {
        workload_id: object()
        for workload_id in ("kg_extract", "kg_refine", "kg_glean")
    }
    provider = RecordingModelProvider(chat_clients=clients)
    monkeypatch.setattr(
        kg_ingest, "windows_with_elements",
        lambda *args, **kwargs: [(SimpleNamespace(section_path="s"), [object()])],
    )
    monkeypatch.setattr(
        kg_ingest, "should_extract_window", lambda *args, **kwargs: (True, "")
    )
    seen = {}

    def fake_extract_window(
        extract_client, elements, section_path, doc_type, index, *,
        refine, gleaning_rounds, base_filter, refine_client, glean_client,
    ):
        seen.update(
            extract=extract_client, refine=refine_client, glean=glean_client,
            calls=list(provider.calls),
        )
        return [], []

    class ImmediateFuture:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    monkeypatch.setattr(kg_ingest, "extract_window", fake_extract_window)
    monkeypatch.setattr(
        kg_ingest, "submit_window",
        lambda fn, *args, **kwargs: ImmediateFuture(fn(*args, **kwargs)),
    )

    kg_ingest.extract_graph(
        provider, "body", "doc.md", "academic_paper",
        refine=True, gleaning_rounds=1,
    )

    assert seen["extract"] is clients["kg_extract"]
    assert seen["refine"] is clients["kg_refine"]
    assert seen["glean"] is clients["kg_glean"]
    assert seen["calls"] == [
        ("chat", "kg_extract"),
        ("chat", "kg_refine"),
        ("chat", "kg_glean"),
    ]


def test_child_abort_does_not_stop_the_job_or_its_siblings():
    """来源级中止只掐自己这一路:兄弟来源与任务本身不受影响。"""
    published = []
    job = KgExtractionRunControl("job-1", on_abort=published.append)
    first = job.child()
    second = job.child()
    failure = KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)

    first.abort(failure)

    with pytest.raises(KgBuildAborted):
        first.raise_if_aborted()
    assert first.aborted is True
    assert second.aborted is False
    assert job.aborted is False
    second.raise_if_aborted()
    job.raise_if_aborted()
    # 来源级中止绝不公布任务状态(不发 stopping / circuit_opened)。
    assert published == []


def test_job_abort_propagates_into_every_live_child():
    """任务级熔断必须穿透子作用域:否则 Ctrl-C / 升级熔断会被当成"跳过这一个源"。"""
    job = KgExtractionRunControl("job-1")
    sub = job.child()
    failure = KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)

    job.abort(failure)

    assert sub.aborted is True
    assert sub.failure is failure
    with pytest.raises(KgBuildAborted) as raised:
        sub.raise_if_aborted()
    assert raised.value.failure is failure
    # 事件也要被置上,否则子作用域里 wait_backoff 会睡满退避才发现任务已经停了。
    waited = threading.Event()

    def _wait():
        try:
            sub.wait_backoff(30)
        except KgBuildAborted:
            waited.set()

    thread = threading.Thread(target=_wait)
    thread.start()
    thread.join(timeout=5)
    assert waited.is_set()


def test_child_created_after_the_job_aborted_is_born_aborted():
    """熔断之后才派生的来源控制器不得再发一次请求。"""
    job = KgExtractionRunControl("job-1")
    failure = KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)
    job.abort(failure)

    sub = job.child()

    assert sub.aborted is True
    with pytest.raises(KgBuildAborted):
        sub.raise_if_aborted()


def test_released_children_do_not_accumulate_on_the_job_control():
    """注册表只为"父熔断唤醒谁"存在,来源收尾即摘除——否则上千个源会让它无界增长。"""
    job = KgExtractionRunControl("job-1")
    for _ in range(100):
        sub = job.child()
        job.release_child(sub)
    assert job._children == set()
