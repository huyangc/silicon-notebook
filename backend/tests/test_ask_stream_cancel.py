import asyncio
import logging
import threading
from types import SimpleNamespace

from app.api.ask_routes import _stream_ask_events
from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.models.schemas import AskRequest
from app.services import background_jobs
from app.services.ask_execution import AskCancellationRegistry, AskExecutionCoordinator
from app.services.cancellation import AskCancelled


class _DisconnectingRequest:
    async def is_disconnected(self):
        return True


class _StubAskState:
    """最小 AskStateStorePort 假件:记录 finish 终态,轨迹/清理 no-op。"""

    def __init__(self):
        self.finished = []
        self.done = threading.Event()

    def begin_durable_job(self, notebook_id, payload, mode, user_id):
        payload.conversation_id = "conv-1"
        return "askjob-1", "conv-1"

    def append_trace(self, notebook_id, job_id, step, user_id):
        pass

    def finish_job(self, job_id, status, *, answer_id="", error=""):
        self.finished.append((status, answer_id))
        if status == "done":
            self.done.set()
        return "conv-1"

    def cleanup_empty_conversation(self, conversation_id):
        pass


def test_disconnect_does_not_cancel_worker_runs_to_completion():
    """WS2a: 客户端断连不再 set cancel_event;worker 脱离连接跑到完、finish(done)
    ——完成的答案在传输关闭后幸存。Task 23 后执行编排在 AskExecutionCoordinator
    (repo._runtime.ask_execution);_stream_ask_events 冻结签名,只剩启动编排、
    队列消费与断连轮询。"""
    entered = threading.Event(); saw_cancel = []
    state = _StubAskState()

    class _StubAskService:
        """Task 24: 协调器的执行体是 AskService(注册表派发在服务内)。"""

        def ask(
            self, nb, payload, *, user_id, job_id="", on_trace=None,
            cancel_event=None,
        ):
            entered.set()
            saw_cancel.append(cancel_event)
            from app.models.schemas import AskResponse
            return AskResponse(answer_id="ans-1", conversation_id="conv-1", conclusion="",
                               answer="a", grounded=True, anchors=[], related_knowledge=[],
                               citations=[], llm_mode="x")

    service = _StubAskService()
    coordinator = AskExecutionCoordinator(
        ask_state=state,
        cancellations=AskCancellationRegistry(),
        job_submitter=background_jobs,
        event_log=SimpleNamespace(logger=logging.getLogger("test-stream-cancel")),
        ask=lambda: service,
    )

    class Stub:
        def current_user(self):
            return SimpleNamespace(id="user-x")

        def start_ask_stream(self, notebook_id, payload, mode, *, user_id):
            return coordinator.start(
                notebook_id, payload, mode, user_id=user_id
            )

    async def drive():
        stream = _stream_ask_events(Stub(), "nb", AskRequest(question="q", mode="chunk"),
            SimpleNamespace(id="chunk", handler="ask_chunk", streaming=False), _DisconnectingRequest())
        # 首事件应是 started(带 job_id);随后断连使 stream 提前结束
        first = await stream.__anext__()
        assert '"event": "started"' in first and '"job_id"' in first
        try:
            while True: await stream.__anext__()
        except StopAsyncIteration:
            pass
    asyncio.run(drive())
    assert entered.wait(1) and state.done.wait(1)    # worker 跑到完,答案幸存(done 终态)
    assert ("done", "ans-1") in state.finished
    assert saw_cancel and not saw_cancel[0].is_set() # cancel_event 未被断连 set


def test_cancelable_llm_stream_closes_on_cancel(tmp_path):
    cancel_event = threading.Event()

    class Delta:
        def __init__(self, content):
            self.content = content

    class Choice:
        def __init__(self, content):
            self.delta = Delta(content)

    class Chunk:
        def __init__(self, content):
            self.choices = [Choice(content)]

    class Stream:
        closed = False

        def __iter__(self):
            yield Chunk('{"answer":')
            cancel_event.set()
            yield Chunk('"late"}')

        def close(self):
            self.closed = True

    stream = Stream()

    class Completions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return stream

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    settings = Settings(
        database_url=f"sqlite:///{tmp_path/'t.db'}",
        storage_dir=str(tmp_path / "s"),
        llm_log_enabled=False,
    )
    client = OpenAICompatibleClient(
        settings,
        base_url="https://llm.example.test/v1",
        api_key="test-key",
        model="test-model",
    )
    client._client = Client()

    try:
        client.chat_json(
            [{"role": "user", "content": "q"}],
            '{"answer": ""}',
            cancel_event=cancel_event,
        )
    except AskCancelled:
        pass
    else:
        raise AssertionError("cancelled LLM stream should raise AskCancelled")

    assert stream.closed is True
