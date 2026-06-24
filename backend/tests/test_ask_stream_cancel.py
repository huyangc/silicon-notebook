import asyncio
import threading
import time
from types import SimpleNamespace

from app.api.routes import _stream_ask_events
from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.models.schemas import AskRequest
from app.services.cancellation import AskCancelled


class _DisconnectingRequest:
    def __init__(self):
        self.calls = 0

    async def is_disconnected(self):
        self.calls += 1
        return True


def test_ask_stream_disconnect_cancels_backend_handler():
    entered = threading.Event()
    worker_exited = threading.Event()
    received_cancel_event = []

    class Repo:
        def ask_chunk(self, notebook_id, payload, cancel_event=None):
            received_cancel_event.append(cancel_event)
            entered.set()
            while cancel_event is not None and not cancel_event.is_set():
                time.sleep(0.005)
            worker_exited.set()
            raise AskCancelled()

    async def drive_stream():
        stream = _stream_ask_events(
            Repo(),
            "nb-test",
            AskRequest(question="q", mode="chunk"),
            SimpleNamespace(id="chunk", handler="ask_chunk", streaming=False),
            _DisconnectingRequest(),
        )
        first = await stream.__anext__()
        assert '"event": "progress"' in first
        assert entered.wait(1)
        try:
            await stream.__anext__()
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("stream should stop instead of yielding final/error")

    asyncio.run(drive_stream())

    assert received_cancel_event and received_cancel_event[0] is not None
    assert received_cancel_event[0].is_set()
    assert worker_exited.wait(1)


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
