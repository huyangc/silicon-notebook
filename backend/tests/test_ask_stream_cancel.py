import asyncio
import threading
from types import SimpleNamespace

from app.api.routes import _stream_ask_events
from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.models.schemas import AskRequest
from app.services.cancellation import AskCancelled


class _DisconnectingRequest:
    async def is_disconnected(self):
        return True


def test_disconnect_does_not_cancel_worker_runs_to_completion():
    """WS2a: 客户端断连不再 set cancel_event;worker 照跑到完、finish_ask_job(done)。"""
    entered = threading.Event(); completed = threading.Event(); saw_cancel = []

    class Repo:
        def begin_ask_job(self, nb, payload, mode, ev):
            saw_cancel.append(ev); return "askjob-1", "conv-1"
        def finish_ask_job(self, job_id, status, *, answer_id="", error=""):
            if status == "done": completed.set()
        def ask_chunk(self, nb, payload, cancel_event=None):
            entered.set()
            from app.models.schemas import AskResponse
            return AskResponse(answer_id="ans-1", conversation_id="conv-1", conclusion="",
                               answer="a", grounded=True, anchors=[], related_knowledge=[],
                               citations=[], llm_mode="x")

    async def drive():
        stream = _stream_ask_events(Repo(), "nb", AskRequest(question="q", mode="chunk"),
            SimpleNamespace(id="chunk", handler="ask_chunk", streaming=False), _DisconnectingRequest())
        # 首事件应是 started(带 job_id);随后断连使 stream 提前结束
        first = await stream.__anext__()
        assert '"event": "started"' in first and '"job_id"' in first
        try:
            while True: await stream.__anext__()
        except StopAsyncIteration:
            pass
    asyncio.run(drive())
    assert entered.wait(1) and completed.wait(1)     # worker 跑到完
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
