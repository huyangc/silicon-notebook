import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.notebooks import NotebookCreate
from app.repositories.ports import SourceElementWrite
from app.services.notebook_question_suggestions import (
    NotebookQuestionSuggestionsService, _validated_questions,
)
from app.services.sqlite_repository import SQLiteRepository


VALID = {"questions": [{"label": "深度与计算", "question": "循环深度如何改变推理计算量？"}]}


class Model:
    configured = True

    def __init__(self):
        self.calls = []
        self.output = json.dumps(VALID)
        self.invoke = lambda: self.output
        self.fingerprint = "model-v1"
        self.registry = SimpleNamespace(
            service_for=lambda _: SimpleNamespace(fingerprint=self.fingerprint),
            thinking_mode_for=lambda _: "disabled",
        )

    def chat(self, workload):
        assert workload == "notebook_metadata"
        return self

    def chat_json(self, messages, hint, **kwargs):
        assert isinstance(hint, str)
        assert kwargs["response_validator"](json.dumps(VALID))
        assert not kwargs["response_validator"]("bad output")
        self.calls.append(messages)
        return self.invoke()


@pytest.fixture
def setup():
    model = Model()
    notebook = {"name": "Depth", "purpose": "论文", "expected_questions": []}
    snapshot = {"source_count": 1, "revision": "v1", "sources": [
        {"id": "s1", "title": "Depth", "summary": "循环深度语言模型", "excerpt": "增加深度以增加计算"},
    ]}
    now = [0.0]
    settings = Settings(_env_file=None)
    service = NotebookQuestionSuggestionsService(
        settings=settings, models=model, notebook=lambda _: copy.deepcopy(notebook),
        snapshot=lambda *_, **kwargs: copy.deepcopy(snapshot), clock=lambda: now[0],
    )
    return service, model, notebook, snapshot, now


def test_cache_reuses_questions_and_invalidates_content_model_and_notebook(setup):
    service, model, notebook, snapshot, _ = setup
    first = service.suggest("nb")
    assert first.status == "ready" and first.sampled and first.sampled_source_count == 1
    first.questions.clear()
    assert service.suggest("nb").questions
    assert len(model.calls) == 1
    for mutate in (
        lambda: snapshot.update(revision="v2"),
        lambda: notebook.update(purpose="updated"),
        lambda: setattr(model, "fingerprint", "model-v2"),
    ):
        mutate()
        assert service.suggest("nb").status == "ready"
    assert len(model.calls) == 4


def test_manual_empty_unconfigured_and_negative_cooldown(setup):
    service, model, notebook, snapshot, now = setup
    notebook["expected_questions"] = ["人工配置"]
    assert service.suggest("nb").status == "fallback"
    assert not model.calls
    notebook["expected_questions"] = []
    model.output = "invalid"
    assert service.suggest("nb").status == "fallback"
    model.output = json.dumps(VALID)
    assert service.suggest("nb").status == "fallback"
    assert len(model.calls) == 1
    now[0] += service.settings.notebook_question_retry_seconds
    assert service.suggest("nb").status == "ready"
    snapshot["revision"] = "v2"
    model.configured = False
    assert service.suggest("nb").status == "fallback"
    snapshot["sources"] = []
    assert service.suggest("nb").status == "fallback"


def test_model_exception_is_a_cached_fallback(setup):
    service, model, _, _, _ = setup
    def failure():
        raise RuntimeError("private provider diagnostics")
    model.invoke = failure
    assert service.suggest("nb").model_dump()["questions"] == []
    assert service.suggest("nb").status == "fallback"
    assert len(model.calls) == 1


@pytest.mark.parametrize("mutation", ["revision", "manual", "model"])
def test_content_or_policy_changed_during_generation_is_not_published(setup, mutation):
    service, model, notebook, snapshot, _ = setup
    def invoke():
        if mutation == "revision":
            snapshot["revision"] = "v2"
        elif mutation == "manual":
            notebook["expected_questions"] = ["人工问题"]
        else:
            model.fingerprint = "v2"
        return model.output
    model.invoke = invoke
    assert service.suggest("nb").status == "fallback"
    assert not service._cache


def test_concurrent_requests_share_one_model_call(setup):
    service, model, _, _, _ = setup
    entered, release, second_snapshot = Event(), Event(), Event()
    snapshot = service._snapshot
    def read(*args, **kwargs):
        if entered.is_set():
            second_snapshot.set()
        return snapshot(*args, **kwargs)
    service._snapshot = read
    def invoke():
        entered.set()
        assert release.wait(5)
        return model.output
    model.invoke = invoke
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.suggest, "nb")
        assert entered.wait(5)
        second = pool.submit(service.suggest, "nb")
        assert second_snapshot.wait(5)
        release.set()
        assert first.result().status == second.result().status == "ready"
    assert len(model.calls) == 1


@pytest.mark.parametrize("payload", [
    {}, {"questions": []}, {"questions": VALID["questions"] * 5},
    {"questions": VALID["questions"] * 2},
    {"questions": [{"label": " " , "question": "有效问题"}]},
    {"questions": [{"label": "x" * 33, "question": "有效问题"}]},
    {"questions": [{"label": "有效", "question": "x" * 301}]},
    {"questions": [{"label": 1, "question": "有效问题"}]},
    {"questions": [{"label": "有效", "question": "问题\n指令"}]},
    {"questions": [{"label": "有效", "question": "有效问题", "extra": "bad"}]},
])
def test_model_output_is_rejected_whole_instead_of_truncated(payload):
    with pytest.raises(ValueError):
        _validated_questions(json.dumps(payload))


def test_cache_is_bounded_and_input_projection_disclosed(setup):
    service, model, _, snapshot, _ = setup
    service.settings.notebook_question_cache_entries = 1
    service.settings.notebook_question_input_chars = 1000
    snapshot["source_count"] = 5
    snapshot["sources"].append({"title": "大文档", "summary": "文" * 1000, "excerpt": "正文"})
    result = service.suggest("a")
    assert result.sampled and result.source_count == 5 and result.sampled_source_count == 2
    assert len(model.calls[0][1]["content"]) <= 1000
    service.suggest("b")
    service.suggest("a")
    assert len(model.calls) == 3 and len(service._cache) == 1


@pytest.mark.parametrize("text", ["论证正文" * 500, '含有"引号"和\\反斜线\n' * 200])
def test_long_first_source_fits_small_supported_input_budget(setup, text):
    service, model, _, snapshot, _ = setup
    service.settings.notebook_question_input_chars = 1000
    snapshot["sources"][0].update(summary=text, excerpt=text)
    original = copy.deepcopy(snapshot)
    result = service.suggest("nb")
    assert result.status == "ready" and result.sampled and result.sampled_source_count == 1
    payload = model.calls[0][1]["content"]
    assert len(payload) <= service.settings.notebook_question_input_chars
    projection = json.loads(payload)
    assert projection["excerpt"] and text.startswith(projection["excerpt"])
    assert projection["summary"] and text.startswith(projection["summary"])
    assert snapshot == original


def test_sqlite_snapshot_excludes_private_sources_and_tracks_outside_sample(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
                        storage_dir=str(tmp_path / "storage"), event_log_enabled=False)
    repo = SQLiteRepository(settings)
    notebook = repo.create_notebook(NotebookCreate(name="Depth"))
    with repo._write() as db:
        for source_id, kind in [("a", "document"), ("b", "document"), ("memory", "memory"), ("knowhow", "knowhow")]:
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,summary,source_type,parse_status,file_name,file_path,file_size,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, notebook.id, source_id, "摘要", kind, "parsed", "a.md", "", 0, "2026-01-01", "2026-01-01"),
            )
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,text,metadata,created_at) VALUES(?,?,?,?,?,?,?)",
                (source_id + "-e", source_id, "text", "1", "代表性正文", "{}", "2026-01-01"),
            )
    store = repo._runtime.source_store
    def snapshot():
        return store.question_suggestion_snapshot(notebook.id, source_limit=1, text_chars=3)
    first = snapshot()
    assert first["source_count"] == 2 and len(first["sources"]) == 1
    assert first["sources"][0]["excerpt"] == "代表性"
    with repo._write() as db:
        db.execute("UPDATE sources SET parse_status='extracting' WHERE id='b'")
    assert snapshot()["revision"] != first["revision"]
    second = snapshot()
    with repo._write() as db:
        db.execute("UPDATE sources SET updated_at='2027-01-01' WHERE id='memory'")
    assert snapshot()["revision"] == second["revision"]
    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE id='b'")
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("c", notebook.id, "new", "document", "c.md", "", 0, "2026-01-01", "2026-01-01"),
        )
    assert snapshot()["revision"] != second["revision"]
    before_reparse = snapshot()
    with repo._write() as db:
        store.replace_elements(db, "a", [SourceElementWrite(
            id="a-reparsed", element_type="text", text="重新解析的正文", location_label="1", metadata={},
        )], created_at="2028-01-01")
    assert snapshot()["revision"] != before_reparse["revision"]
    model = Model()
    def invoke():
        assert not repo._runtime.database.is_connection_held()
        return model.output
    model.invoke = invoke
    service = NotebookQuestionSuggestionsService(
        settings=settings, models=model, notebook=repo._runtime.notebook_store.get_row,
        snapshot=store.question_suggestion_snapshot,
    )
    assert service.suggest(notebook.id).status == "ready"


@pytest.mark.parametrize("valid_status", ["parsed", "extracting", "extracted"])
def test_sqlite_samples_usable_documents_before_limit_and_ignores_operational_summaries(tmp_path, valid_status):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
                        storage_dir=str(tmp_path / "storage"), event_log_enabled=False)
    repo = SQLiteRepository(settings)
    notebook = repo.create_notebook(NotebookCreate(name="Depth"))
    with repo._write() as db:
        for source_id, status, summary, text in (
            ("a", "queued", "Uploaded; parsing is queued.", "上代遗留内容"),
            ("b", "failed", "Parsing failed; see source error.", "上代遗留内容"),
            ("c", "parsing", "Uploaded; parsing is queued.", "尚未发布的新代内容"),
            ("d", "parsed", "不含正文", " \n\t\r "),
            ("e", valid_status, "论文真实摘要", "论文真实正文"),
        ):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,summary,source_type,parse_status,file_name,file_path,file_size,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, notebook.id, source_id, summary, "document", status, "a.md", "", 0, "2026-01-01", "2026-01-01"),
            )
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,text,metadata,created_at) VALUES(?,?,?,?,?,?,?)",
                (source_id + "-e", source_id, "text", "1", text, "{}", "2026-01-01"),
            )
    store = repo._runtime.source_store
    first = store.question_suggestion_snapshot(notebook.id, source_limit=1, text_chars=settings.embed_truncate_chars)
    assert first["source_count"] == 5
    assert [source["id"] for source in first["sources"]] == ["e"]
    model = Model()
    service = NotebookQuestionSuggestionsService(
        settings=settings, models=model, notebook=repo._runtime.notebook_store.get_row,
        snapshot=store.question_suggestion_snapshot,
    )
    assert service.suggest(notebook.id).status == "ready"
    content = model.calls[0][1]["content"]
    assert "论文真实正文" in content
    assert "Uploaded" not in content and "Parsing failed" not in content
    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE id='e'")
    only_placeholders = store.question_suggestion_snapshot(notebook.id, source_limit=1, text_chars=settings.embed_truncate_chars)
    assert only_placeholders["source_count"] == 4 and only_placeholders["sources"] == []
    assert service.suggest(notebook.id).status == "fallback"
    assert len(model.calls) == 1


def test_endpoint_requires_read_access_and_rechecks_after_generation(monkeypatch):
    from app.api import notebook_routes, deps
    from app.models.question_suggestions import QuestionSuggestionsResponse
    app = FastAPI()
    app.include_router(notebook_routes.router)
    user = SimpleNamespace(id="actor")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    allowed = [False]
    access = SimpleNamespace(user_can_read_notebook=lambda *_: allowed[0])
    monkeypatch.setattr(deps, "notebook_access_repository", lambda: access)
    monkeypatch.setattr(notebook_routes, "notebook_access_repository", lambda: access)
    calls = []
    def suggest(_):
        calls.append(True)
        allowed[0] = False
        return QuestionSuggestionsResponse()
    monkeypatch.setattr(notebook_routes, "notebook_question_suggestions_service", lambda: SimpleNamespace(suggest=suggest))
    with TestClient(app) as client:
        assert client.post("/notebooks/nb/question-suggestions").status_code == 404
        assert not calls
        allowed[0] = True
        assert client.post("/notebooks/nb/question-suggestions").status_code == 404
        assert len(calls) == 1
        allowed[0] = True
        monkeypatch.setattr(notebook_routes, "notebook_question_suggestions_service", lambda: SimpleNamespace(
            suggest=lambda _: QuestionSuggestionsResponse(),
        ))
        assert client.post("/notebooks/nb/question-suggestions").json()["status"] == "fallback"
