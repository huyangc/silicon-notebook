"""Shared cases: the raw-element arm reads back the run's chunk recall.

Driven on SQLite by ``tests/test_chunk_recall_memo.py`` and on PostgreSQL by
``tests/postgres/test_chunk_recall_memo_pg.py``.  Each case runs the SAME
production run twice -- once with the run's recall memo switched off
(``RetrievalRunState.keeps_chunk_recalls`` -> False, the code path before
the memo existed) and once with it on -- and requires the two to be
byte-identical in everything the run hands on: every element search's
candidates (ids, text, scores, order), every chunk recall the semantic legs
made (chunks, scores, order, the MMR matrix), and the finished answer
(citations, anchors, conclusion, trace).  What may differ is the number of
physical recalls, which is what the memo exists to cut.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.models.global_ask import GlobalAskRequest
from app.models.schemas import AskRequest, NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.embedding import FakeEmbedder
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_run import RetrievalRunState
from tests.ask_testkit import ask_engine_as_current_user
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

EMBED_DIM = 16
LIBRARIES = 8
QUERY = "低温性能"
OTHER_QUERY = "常温基线"
QUESTION = "低温性能怎么样"
ANSWER = {"next_action": "answer", "sufficient": True}


@dataclass
class World:
    repo: Any
    notebook_ids: list


def build(repo, *, libraries: int = LIBRARIES) -> World:
    """``libraries`` notebooks, one markdown source each, chunked and embedded
    through the production ingestion path."""
    bind_all_embedding_clients(repo, FakeEmbedder(dim=EMBED_DIM))
    repo.settings.graph_ppr_enabled = False
    notebook_ids = []
    for index in range(libraries):
        notebook = repo.create_notebook(NotebookCreate(name=f"库{index}"))
        body = "\n\n".join(
            f"## 第{part}节\n\n低温性能 实验 {index}-{part} 增益 上升 记录;"
            f"常温基线 {part} 稳定。" * 3
            for part in range(10)
        )
        source = repo.upload_sources(notebook.id, [UploadedSourceFile(
            file_name=f"doc-{index}.md", content_type="text/markdown",
            content=body.encode("utf-8"))], scheduler=lambda _source_id: None)[0]
        repo.process_source(source.id)
        notebook_ids.append(notebook.id)
    return World(repo, notebook_ids)


class _Agent:
    """``reasoning_agent``: one sub-query, then the scripted reflect actions."""

    configured = True
    model = "fake"

    def __init__(self, reflects):
        self.reflects = list(reflects)

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": QUERY}]})
        if "next_action" in schema_hint:
            return json.dumps(self.reflects.pop(0) if self.reflects else ANSWER)
        return json.dumps({})


class _Answerer:
    configured = True
    model = "fake"

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": QUERY}]})
        prompt = "\n".join(str(row.get("content", "")) for row in messages)
        keys = sorted({word for word in prompt.replace("[", " ").replace("]", " ")
                       .split() if word[:1] == "k" and word[1:].isdigit()})
        answer = "回答如下。" + "".join(f"[{key}]" for key in keys[:24])
        return json.dumps({
            "conclusion": "回答如下。", "answer": answer, "anchors": [],
            "grounded": True, "summary": "回答如下。", "sections": [],
        })


def _bind(repo, reflects) -> None:
    bind_chat_client(repo, "reasoning_agent", _Agent(reflects))
    answerer = _Answerer()
    for workload in ("ask_answer", "query_rewrite", "evidence_refine",
                     "source_summary"):
        bind_chat_client(repo, workload, answerer)


#: search_chunks(Q), then element searches Q, Q', Q: the first and the last
#: element search can read back the semantic legs' recall of Q.
REFLECTS = (
    {"next_action": "search_chunks", "reason": "查原文", "query": QUERY},
    {"next_action": "search_elements", "reason": "查元素", "query": QUERY},
    {"next_action": "search_elements", "reason": "查元素", "query": OTHER_QUERY},
    {"next_action": "search_elements", "reason": "查元素", "query": QUERY},
)


@dataclass
class Observed:
    recalls: int
    elements: list
    chunk_recalls: list
    answer: Any
    stats: list


def _chunk_bytes(result) -> tuple:
    scored, ids, matrix = result
    return (
        [(c.chunk_id, c.source_id, c.notebook_id, c.text, list(c.element_ids),
          c.score, c.relevance, tuple(c.retrieval_supports)) for c in scored],
        list(ids or []),
        None if matrix is None else (matrix.shape, matrix.tobytes()),
    )


def observe(monkeypatch, run, *, memo: bool) -> Observed:
    """Run ``run()`` with the memo on or off and record what it handed on."""
    recalls: list = []
    elements: list = []
    chunk_recalls: list = []
    stats: list = []
    lock = threading.Lock()
    original_once = CandidateRetrievalService._retrieve_chunks_once
    original_chunks = CandidateRetrievalService._retrieve_chunks
    original_elements = CandidateRetrievalService._retrieve_elements
    original_event = RetrievalRunState.event

    def once(self, *args, **kwargs):
        with lock:
            recalls.append(args[:2])
        return original_once(self, *args, **kwargs)

    def chunks(self, *args, **kwargs):
        from app.services import chunk_recall_memo

        light = chunk_recall_memo.serves_light()
        result = original_chunks(self, *args, **kwargs)
        if not light:
            with lock:
                chunk_recalls.append((args[:2], _chunk_bytes(result)))
        return result

    def element_search(self, notebook_id, query, *args, **kwargs):
        found = original_elements(self, notebook_id, query, *args, **kwargs)
        with lock:
            elements.append((notebook_id, query, [repr(hit) for hit in found]))
        return found

    def event(self):
        payload = original_event(self)
        stats.append(payload)
        return payload

    with monkeypatch.context() as patch:
        patch.setattr(CandidateRetrievalService, "_retrieve_chunks_once", once)
        patch.setattr(CandidateRetrievalService, "_retrieve_chunks", chunks)
        patch.setattr(CandidateRetrievalService, "_retrieve_elements",
                      element_search)
        patch.setattr(RetrievalRunState, "event", event)
        if not memo:
            patch.setattr(RetrievalRunState, "keeps_chunk_recalls",
                          lambda self: False)
        answer = run()
    return Observed(
        len(recalls), sorted(elements, key=repr), sorted(chunk_recalls, key=repr),
        answer, stats,
    )


def _global_answer(world: World):
    repo = world.repo
    service = repo._runtime.global_ask_service()
    job = service.start(GlobalAskRequest(
        question=QUESTION, mode="reasoning",
        notebook_scope={"mode": "include", "notebook_ids": world.notebook_ids},
    ), user_id=repo.current_user().id)
    deadline = time.monotonic() + 60
    while job.job_id in service._events and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    result = service.get_job(job.job_id, user_id=repo.current_user().id)
    assert result.status == "done", result.error
    return _stable_answer(result.answer.model_dump(mode="json"))


def _stable_answer(dumped: dict) -> dict:
    """The answer minus what differs between two identical runs (ids and
    clocks the job mints, step latencies)."""
    volatile = {"answer_id", "answered_at", "conversation_id", "created_at", "job_id",
                "latency_ms", "duration_ms", "elapsed_ms", "started_at",
                "finished_at", "updated_at"}

    def strip(value):
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items()
                    if key not in volatile}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(dumped)


def _assert_same(on, off, path="answer"):
    """Equal, or the first path where they differ (a readable failure)."""
    if isinstance(on, dict) and isinstance(off, dict):
        assert sorted(on) == sorted(off), (path, sorted(set(on) ^ set(off)))
        for key in on:
            _assert_same(on[key], off[key], f"{path}.{key}")
        return
    if isinstance(on, list) and isinstance(off, list):
        assert len(on) == len(off), (path, len(on), len(off))
        for index, (left, right) in enumerate(zip(on, off)):
            _assert_same(left, right, f"{path}[{index}]")
        return
    assert on == off, (path, on, off)


def global_run_reuses_the_recall_byte_identically(world: World, monkeypatch) -> None:
    """8 libraries: the element searches of Q read back the semantic legs'
    recall of Q (seed and ``search_chunks``) instead of recalling again."""
    libraries = len(world.notebook_ids)

    def run():
        _bind(world.repo, REFLECTS)
        return _global_answer(world)

    observe(monkeypatch, run, memo=False)   # warm the process caches first
    off = observe(monkeypatch, run, memo=False)
    on = observe(monkeypatch, run, memo=True)
    assert on.elements == off.elements and on.elements
    assert on.chunk_recalls == off.chunk_recalls and on.chunk_recalls
    _assert_same(on.answer, off.answer)
    assert on.answer["citations"], "a run citing nothing proves nothing"
    # seed + search_chunks + three element searches, one recall per library
    # each; with the memo the two element searches of Q recall nothing.
    assert off.recalls == 5 * libraries
    assert on.recalls == 3 * libraries
    assert sum(event.get("chunk_recall_hits", 0) for event in on.stats) == (
        2 * libraries)
    assert sum(event.get("chunk_recall_hits", 0) for event in off.stats) == 0


def single_library_run_is_byte_identical(world: World, monkeypatch) -> None:
    """One notebook's own reasoning run (no participant set): a large library
    takes the element arm's bounded chunk route, which reads back the
    semantic leg's recall of the same text; nothing else changes."""
    repo = world.repo
    notebook_id = world.notebook_ids[0]
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "notebook_copy_stats",
                        lambda _nb: {"copyable": False, "size": {}})

    def run():
        _bind(repo, REFLECTS)
        response = ask_engine_as_current_user(
            repo._runtime.ask_service(), notebook_id,
            AskRequest(question=QUESTION, mode="reasoning"),
        )
        return _stable_answer(response.model_dump(mode="json"))

    observe(monkeypatch, run, memo=False)   # warm the process caches first
    off = observe(monkeypatch, run, memo=False)
    on = observe(monkeypatch, run, memo=True)
    assert on.elements == off.elements and on.elements
    assert on.chunk_recalls == off.chunk_recalls and on.chunk_recalls
    _assert_same(on.answer, off.answer)
    assert on.recalls < off.recalls
    assert off.recalls - on.recalls == sum(
        event.get("chunk_recall_hits", 0) for event in on.stats)
