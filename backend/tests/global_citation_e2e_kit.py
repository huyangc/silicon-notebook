"""Real-engine harness for PR-D's healthy-run proofs (closing round item 18).

Every run goes through ``GlobalAskService`` -- the real ``global_ask_run``
(participant override + frozen ceilings), the job thread, the shared engine,
the terminal citation check -- over a real SQLite repository. Only the models
are scripted: the reasoning agent replays a fixed action list (plan, reflect
actions, a spreadsheet plan) and the answer model cites every evidence key the
synthesis prompt offered, so each producer's cards and anchors are really
checked. Events are captured from the runtime's event log.
"""
from __future__ import annotations

import json
import re
import threading
import time

from app.core.config import Settings
from app.models.global_ask import GlobalAskRequest
from app.models.notebooks import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import (
    RecordingModelProvider, bind_all_embedding_clients, bind_chat_client,
)

NOW = "2026-09-29T00:00:00+08:00"
_KEY = re.compile(r"\bk(\d+)\b")
ANSWER = {"next_action": "answer", "sufficient": True}


def make_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'e2e.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repo = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    repo.settings.graph_ppr_enabled = False
    return repo


class ScriptedAgent:
    """The ``reasoning_agent`` workload: plan, reflect actions, sheet plan."""

    configured = True
    model = "fake"

    def __init__(self, reflects=(), *, plan_query="低温性能", sheet_plan=None):
        self.reflects = list(reflects)
        self.plan_query = plan_query
        self.sheet_plan = sheet_plan

    def chat_json(self, messages, schema_hint, **kwargs):
        if '"operation"' in schema_hint:
            return json.dumps(self.sheet_plan or {"operation": "profile"})
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": self.plan_query}]})
        if "next_action" in schema_hint:
            return json.dumps(self.reflects.pop(0) if self.reflects else ANSWER)
        return json.dumps({})


class CitingAnswerer:
    """Every other chat workload: cites every ``kN`` the prompt offered."""

    configured = True
    model = "fake"

    def __init__(self, text="回答如下。"):
        self.text = text
        self.prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = "\n".join(str(row.get("content", "")) for row in messages)
        self.prompts.append(prompt)
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "低温性能"}]})
        keys = list(dict.fromkeys(match.group(1) for match in _KEY.finditer(prompt)))
        answer = self.text + "".join(f"[k{key}]" for key in keys[:24])
        return json.dumps({
            "conclusion": self.text, "answer": answer, "anchors": [],
            "grounded": True, "summary": self.text, "sections": [],
        })


def bind_models(repo, agent, answerer=None):
    answerer = answerer or CitingAnswerer()
    bind_chat_client(repo, "reasoning_agent", agent)
    for workload in ("ask_answer", "query_rewrite", "evidence_refine", "source_summary"):
        bind_chat_client(repo, workload, answerer)
    return answerer


def capture_events(repo, monkeypatch) -> list:
    events: list = []
    log = repo._runtime.event_log
    original = log.emit

    def emit(event, *args, **kwargs):
        events.append(dict(event))
        return original(event, *args, **kwargs)

    monkeypatch.setattr(log, "emit", emit)
    return events


def global_answer(repo, notebook_ids, question, *, mode="reasoning"):
    service = repo._runtime.global_ask_service()
    job = service.start(
        GlobalAskRequest(
            question=question, mode=mode,
            notebook_scope={"mode": "include", "notebook_ids": list(notebook_ids)},
        ),
        user_id=repo.current_user().id,
    )
    deadline = time.monotonic() + 40
    while job.job_id in service._events and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    return service.get_job(job.job_id, user_id=repo.current_user().id)


def references(answer) -> list:
    return [*answer.citations, *answer.anchors]


def assert_clean(result, events, producer: str) -> None:
    """A healthy run: delivered, no mark anywhere, no summary, the producer's
    registration visible in the events."""
    assert result.status == "done", result.error
    dumped = result.answer.model_dump(mode="json")
    assert "citation_check" not in dumped, dumped.get("citation_check")
    assert "verification" not in json.dumps(dumped, ensure_ascii=False)
    assert any(
        event.get("kind") == "producer_evidence_attested"
        and event.get("producer") == producer
        for event in events
    ), [event for event in events if str(event.get("kind", "")).startswith("producer")]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def notebook(repo, name="库"):
    return repo.create_notebook(NotebookCreate(name=name))


def seed_document(repo, notebook_id, source_id, title, texts, *, element_type="paragraph",
                  summary=""):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,summary,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title, "markdown", "parsed", "parsed",
             summary, NOW, NOW),
        )
        for index, text in enumerate(texts):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"{source_id}-{index:03}", source_id, element_type,
                 f"第{index + 1}节", text,
                 json.dumps({"section_path": f"第{index + 1}节"}), NOW),
            )
    repo.collection_catalog.invalidate()
    return [f"{source_id}-{index:03}" for index in range(len(texts))]


def evidence(source_id, element_id, quote):
    return {"quoted_span": quote, "quote": quote, "element_id": element_id,
            "source_id": source_id, "source_title": "文档", "element_type": "paragraph",
            "location_label": "第1节", "confidence": 1.0}


def object_ids(repo, notebook_id) -> dict:
    with repo._connect() as db:
        return {
            json.loads(row["payload"])["name"]: row["id"]
            for row in db.execute(
                "SELECT id,payload FROM knowledge_objects WHERE notebook_id=?",
                (notebook_id,),
            )
        }


def upload_workbook(repo, notebook_id, tmp_path, rows):
    from openpyxl import Workbook

    from app.repositories.ports import UploadedSourceFile

    path = tmp_path / "sales.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Region", "Amount"])
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    source = repo.upload_sources(notebook_id, [UploadedSourceFile(
        file_name="sales.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content=path.read_bytes(),
    )], scheduler=lambda _source_id: None)[0]
    repo.process_source(source.id)
    return source.id


