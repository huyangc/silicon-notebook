"""max_tokens plumbing: config defaults + env, the cap_kwargs splat helper, and
that the client itself plus the answer-synthesis and KG-extraction call sites
actually put max_tokens on the request. The guard (cap_kwargs → {} when a client
exposes no Settings) is what keeps the many hand-rolled test doubles working."""
import json as _j
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.llm import cap_kwargs, OpenAICompatibleClient
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedChunk
from app.services.kg.extract import extract_window
from app.services.kg.parsing import SourceElementQ
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


# ---- config: defaults + env override --------------------------------------
def test_max_tokens_defaults(monkeypatch):
    for k in ("OPENAI_COMPAT_MAX_TOKENS", "ANSWER_MAX_TOKENS", "KG_EXTRACT_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.openai_compat_max_tokens == 8192
    assert s.answer_max_tokens == 16384
    assert s.kg_extract_max_tokens == 51200


def test_max_tokens_env_override(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_MAX_TOKENS", "4096")
    monkeypatch.setenv("ANSWER_MAX_TOKENS", "20000")
    monkeypatch.setenv("KG_EXTRACT_MAX_TOKENS", "0")
    s = Settings(_env_file=None)
    assert s.openai_compat_max_tokens == 4096
    assert s.answer_max_tokens == 20000
    assert s.kg_extract_max_tokens == 0


# ---- cap_kwargs helper -----------------------------------------------------
def test_cap_kwargs_reads_client_settings():
    client = SimpleNamespace(settings=SimpleNamespace(answer_max_tokens=16384,
                                                      kg_extract_max_tokens=0))
    assert cap_kwargs(client, "answer_max_tokens") == {"max_tokens": 16384}
    # non-positive budget → omit (fall back to chat_json's own default)
    assert cap_kwargs(client, "kg_extract_max_tokens") == {}
    # attribute absent on settings → omit
    assert cap_kwargs(client, "nope") == {}


def test_cap_kwargs_no_settings_is_noop():
    # duck-typed clients (test doubles) without .settings must not break splatting
    assert cap_kwargs(SimpleNamespace(), "answer_max_tokens") == {}
    assert cap_kwargs(object(), "answer_max_tokens") == {}


# ---- chat_json emits max_tokens into the request ---------------------------
class _Msg:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _Completions:
    def __init__(self, sink): self.sink = sink
    def create(self, **kwargs):
        self.sink.append(kwargs)
        return _Resp('{"answer": "x"}')


class _Chat:
    def __init__(self, sink): self.completions = _Completions(sink)


class _OpenAI:
    def __init__(self, sink): self.chat = _Chat(sink)


def _wired_client(monkeypatch, **over):
    for k in ("OPENAI_COMPAT_MAX_TOKENS", "ANSWER_MAX_TOKENS", "KG_EXTRACT_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_CACHE_ENABLED", "false")
    s = Settings(_env_file=None)
    for k, v in over.items():
        setattr(s, k, v)
    c = OpenAICompatibleClient(s, base_url="http://x", api_key="k", model="m")
    sink: list = []
    c._client = _OpenAI(sink)         # bypass the real httpx/OpenAI transport
    return c, sink


def test_chat_json_sends_global_default(monkeypatch):
    c, sink = _wired_client(monkeypatch)
    c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert sink[-1]["max_tokens"] == 8192


def test_chat_json_per_call_override_wins(monkeypatch):
    c, sink = _wired_client(monkeypatch)
    c.chat_json([{"role": "user", "content": "hi"}], "{}", max_tokens=16384)
    assert sink[-1]["max_tokens"] == 16384


def test_chat_json_omits_when_zero(monkeypatch):
    c, sink = _wired_client(monkeypatch, openai_compat_max_tokens=0)
    c.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert "max_tokens" not in sink[-1]


# ---- answer synthesis requests answer_max_tokens ---------------------------
@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


class _RecordingLLM:
    configured = True
    def __init__(self, settings):
        self.settings = settings
        self.kwargs = None
    def chat_json(self, messages, schema_hint, **kw):
        self.kwargs = kw
        return _j.dumps({"answer": "Cascode raises rout [k1].", "grounded": True})


def test_answer_synthesis_requests_answer_cap(repo):
    repo.settings.answer_max_tokens = 12345          # deterministic, distinct value
    llm = _RecordingLLM(repo.settings)
    bind_chat_client(repo, "ask_answer", llm)
    chunks = [RetrievedChunk(chunk_id="c1", source_id="s", source_title="D",
                             section_path="1", text="cascode raises rout", relevance=0.8)]
    repo._answer_mix("q", chunks, "(none)", {}, "")
    assert llm.kwargs.get("max_tokens") == 12345


# ---- KG extraction requests kg_extract_max_tokens --------------------------
def _se(idx, text, start):
    return SourceElementQ(id=f"SE-{idx}", type="paragraph", file="doc.md",
                          line_start=idx + 1, line_end=idx + 1,
                          char_start=start, char_end=start + len(text), text=text)


class _RecordingKG:
    configured = True
    def __init__(self, settings):
        self.settings = settings
        self.kwargs = None
    def chat_json(self, messages, response_schema_hint, **kw):
        self.kwargs = kw
        return _j.dumps({"nodes": [{"local_id": "a", "type": "Concept",
                                    "name": "analog signal", "ev": 0}], "edges": []})


def test_extraction_requests_kg_cap():
    # distinct kg/answer values prove extract_window reads the KG attr, not answer's
    settings = SimpleNamespace(kg_extract_max_tokens=16384, answer_max_tokens=1)
    client = _RecordingKG(settings)
    elements = [_se(0, "An analog signal is defined over a continuous range.", 0)]
    extract_window(client, elements, "1", "textbook", win_idx=0)
    assert client.kwargs.get("max_tokens") == 16384
