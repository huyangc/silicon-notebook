"""Shared seeding and a fake ``paper_metadata`` chat client for the paper card
service tests (SQLite: ``tests/test_paper_card_service.py``; PostgreSQL:
``tests/postgres/test_paper_card_service_pg.py``). Backend-neutral: everything
goes through the runtime's ports. No real model is ever called."""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.repositories.ports import SourceElementWrite
from app.services.model_provider import RuntimeModelProvider
from app.services.model_registry import ModelServiceDefinition, SystemModelServiceRegistry
from app.services.paper_meta import PAPER_META_SCHEMA_HINT
from tests.model_testkit import bind_chat_client

TITLE = "Gated Widgets for Sparse Vision"
OPENING = (
    f"{TITLE}\nAbstract: We study sparse vision backbones and propose gated widgets, "
    "a routing block that skips redundant tokens."
)
TABLE = "| Model | Top-1 (%) |\n| Ours | 85.20 |\n| ViT-B | 81.8 |"
CONCLUSION = "Conclusion: gated widgets cut compute by 3.1x at equal accuracy."
CREATED_AT = "2026-01-01T00:00:00+00:00"


def seed_source(
    repo: Any,
    notebook_id: str,
    source_id: str,
    *,
    doc_type: str = "",
    source_type: str = "document",
    created_at: str = CREATED_AT,
    filler: int = 3,
) -> None:
    store = repo._runtime.source_store
    store.insert_source(
        source_id=source_id, notebook_id=notebook_id, title=f"Doc {source_id}",
        source_type=source_type, status="parsed", parse_status="parsed",
        file_name=f"{source_id}.pdf", file_path=f"/nonexistent/{source_id}.pdf",
        file_size=0, file_hash=f"h-{source_id}", summary="", doc_type=doc_type,
    )
    replace_elements(repo, source_id, created_at=created_at, filler=filler)


def replace_elements(repo: Any, source_id: str, *, created_at: str, filler: int = 3) -> None:
    texts = [("text", OPENING)]
    texts += [("text", f"Section {i}: background paragraph number {i}.") for i in range(filler)]
    texts += [("table", TABLE), ("text", CONCLUSION)]
    elements = [
        SourceElementWrite(
            id=f"el-{source_id}-{index:04d}", element_type=kind,
            location_label=f"p{index}", text=text, metadata={},
        )
        for index, (kind, text) in enumerate(texts)
    ]
    with repo._write() as db:
        repo._runtime.source_store.replace_elements(db, source_id, elements, created_at=created_at)


def set_paper_meta(repo: Any, notebook_id: str, source_id: str, *, is_paper: bool) -> None:
    repo._runtime.source_store.upsert_paper_meta(source_id, notebook_id, {
        "is_paper": is_paper, "paper_title": TITLE if is_paper else None,
        "venue": None, "pub_year": None, "doi": None, "keywords": [], "authors": [],
        "model": "seed", "raw_json": "{}",
    })


_HANDLE_RE = re.compile(r"^\[(e\d+)\] (\w+)", re.MULTILINE)


def card_reply(prompt: str, *, value: str = "85.2") -> dict:
    handles = _HANDLE_RE.findall(prompt)
    first = handles[0][0]
    table = next((h for h, kind in handles if kind == "table"), first)
    return {
        "tldr": {"text": "Gated widgets skip redundant tokens.", "anchors": [first]},
        "problem": {"text": "Sparse vision backbones waste compute.", "anchors": [first]},
        "method": {"text": None, "anchors": []},
        "contributions": [{"text": "A routing block for token skipping.", "anchors": [first]}],
        "metrics": [{
            "name": "top-1", "value": value, "unit": "%", "setting": "ImageNet",
            "compared_to": "ViT-B 81.8", "anchor": table,
        }],
        "datasets": [{"name": "ImageNet", "anchors": [table]}],
    }


class FakePaperModel:
    """Answers the paper-metadata prompt with a grounded paper verdict and the
    card prompt with ``card_reply``. ``on_card`` runs inside each card call
    (before the reply) so a test can block, race or raise there."""

    def __init__(self, *, is_paper: bool = True) -> None:
        self.configured = True
        self.model = "fake-paper"
        self.is_paper = is_paper
        self.meta_calls = 0
        self.card_calls = 0
        self.cancel_events: list[Any] = []
        self.on_card: Callable[[str], None] | None = None
        self.on_meta: Callable[[str], None] | None = None
        self.meta_reply: Callable[[str], Any] | None = None
        self.reply: Callable[[str], Any] = card_reply
        self._lock = threading.Lock()

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[-1]["content"]
        with self._lock:
            self.cancel_events.append(kwargs.get("cancel_event"))
            if schema_hint == PAPER_META_SCHEMA_HINT:
                self.meta_calls += 1
            else:
                self.card_calls += 1
        if schema_hint == PAPER_META_SCHEMA_HINT:
            if self.on_meta is not None:
                self.on_meta(prompt)
            if self.meta_reply is not None:
                reply = self.meta_reply(prompt)
                return reply if isinstance(reply, str) else json.dumps(reply)
            return json.dumps({
                "is_paper": self.is_paper, "title": TITLE if self.is_paper else None,
                "authors": [], "venue": None, "year": None, "doi": None, "keywords": [],
            })
        if self.on_card is not None:
            self.on_card(prompt)
        reply = self.reply(prompt)
        return reply if isinstance(reply, str) else json.dumps(reply)


class StatusError(Exception):
    """A transport error carrying an HTTP status (429 / 5xx / 401 …)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@contextmanager
def scheduled_paper_model(repo: Any, fake: Any, *, max_concurrency: int = 4) -> Iterator[Any]:
    """Bind ``fake`` behind the real scheduled adapter (``RuntimeModelProvider``):
    every failure the fake raises reaches the service wrapped in a
    ``ModelInvocationError`` with a stable ``.code``, exactly as in production."""
    definition = ModelServiceDefinition(
        id="paper", display_name="论文测试服务", kind="chat", protocol="openai",
        base_url="https://paper.example/v1", model="fake-paper",
        api_key_env="PAPER_TEST_KEY", api_key="test", max_concurrency=max_concurrency,
        fingerprint="paper-test",
        context_window_tokens=1048576, max_output_tokens=131072,
    )
    provider = RuntimeModelProvider(
        repo.settings, repo._runtime.event_log,
        registry=SystemModelServiceRegistry({"paper": definition}, {"paper_metadata": "paper"}),
        chat_factory=lambda _service: fake,
    )
    try:
        bind_chat_client(repo, "paper_metadata", provider.chat("paper_metadata"))
        yield provider
    finally:
        provider.close()
