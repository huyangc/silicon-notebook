"""The confirmed-intent path's lexical seed arm, fed by the understanding call.

A confirmed intent skips ``plan()`` (the reviewed contract is authoritative),
and ``plan()`` was the only producer of the whole-question keywords the
first-round lexical seed arm reads -- so every run behind the review card ran
on the semantic arm alone. The fix (user ruling 2026-10-19): the SAME single
understanding call also writes the ``expand_query``-shaped high/low-level
keyword pair, bilingual by the corpus languages, and confirmation hands it to
the seed arm. No extra model call.

Pinned here: the contract shape and its fallbacks, the edit rule (any edit of
the understanding drops the keywords -- the arm then stays off, exactly as
before), and that all three entry points (notebook HTTP, MCP ``ask``, global
Ask) deliver the keywords to the arm.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.models.ask import AskIntentConfirmation, QueryIntentContract
from app.services import prompts
from app.services.query_intent import (
    INTENT_KEYWORD_MAX_CHARS,
    INTENT_KEYWORDS_MAX_ITEMS,
    confirmed_intent_keywords,
    finalize_query_intent,
    intent_keyword_basis,
    plan_query_intent,
)
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

RARE = "QZV9381"
HIGH = ["沟槽隔离", "trench isolation"]
LOW = [RARE, "浅沟槽隔离", "STI"]
KEYWORDS = " ".join(HIGH + LOW)


def _understanding(**overrides) -> dict:
    data = {
        "normalized_question": "浅沟槽隔离的关键参数是什么？",
        "intent_type": "explain",
        "result_scope": "ranked",
        "completeness_required": False,
        "entities": ["浅沟槽隔离"],
        "mandatory_topics": [{
            "title": "关键参数",
            "question": "浅沟槽隔离的关键参数有哪些？",
            "retrieval_queries": ["浅沟槽隔离 关键参数", "STI key parameters"],
        }],
        "high_level_keywords": list(HIGH),
        "low_level_keywords": list(LOW),
        "ambiguities": [],
        "confidence": 0.9,
        "needs_clarification": False,
    }
    data.update(overrides)
    return data


class _IntentModel:
    """Records every understanding call; answers the rest of a reasoning run."""

    configured = True
    model = "fake"

    def __init__(self, **overrides):
        self.overrides = overrides
        self.intent_calls: list[tuple[str, str]] = []
        self.plan_calls = 0

    def chat_json(self, messages, schema_hint="", **kwargs):
        schema_hint = schema_hint or ""
        if "normalized_question" in schema_hint:
            self.intent_calls.append((messages[-1]["content"], schema_hint))
            return json.dumps(_understanding(**self.overrides), ensure_ascii=False)
        if "sub_queries" in schema_hint:
            self.plan_calls += 1
            return json.dumps({"sub_queries": [{"query": "浅沟槽隔离"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "浅沟槽隔离参数见文档 [k1]。", "grounded": True,
                           "conclusion": "见文档。", "anchors": []})


# --------------------------------------------------------------------------- #
# The contract: shape, bilingual rule, fallbacks
# --------------------------------------------------------------------------- #

def test_keywords_come_from_the_one_understanding_call_in_expand_querys_shape():
    model = _IntentModel()
    contract = plan_query_intent(
        model, "浅沟槽隔离有哪些关键参数", keywords=True, corpus_langs=["zh", "en"],
    )
    assert len(model.intent_calls) == 1          # no second model call
    prompt, hint = model.intent_calls[0]
    assert hint == prompts.QUERY_INTENT_KEYWORDS_SCHEMA_HINT
    assert prompt.endswith(f"Return JSON only: {hint}")
    # The bilingual rule is expand_query's own sentence, not a second copy.
    rule = prompts.keyword_language_rule(["zh", "en"])
    assert "EACH of these corpus languages: zh, en" in rule
    assert prompt.count(rule) == 1
    expand = prompts.expand_query_prompt("q", corpus_langs=["zh", "en"])
    assert rule in expand
    # The two field definitions are expand_query's own phrases.
    for phrase in ("themes / relationship types / abstract topics (used to "
                   "retrieve RELATIONS)",
                   "concrete entities / names / specifics (used to retrieve ENTITIES)"):
        assert phrase in prompt and phrase in expand
    # The scope sentence bounds concepts but REQUIRES the other-language forms
    # (they are the cross-language matches this pair exists for); nothing tells
    # the model to drop a form the user did not literally type.
    assert ("the other-language forms, abbreviations and full names of what it "
            "does ask about are required") in prompt
    assert "does not name" not in prompt
    assert "Write [] (never null)" in prompt
    assert contract["high_level_keywords"] == HIGH
    assert contract["low_level_keywords"] == LOW
    assert contract["keyword_basis"] == intent_keyword_basis(contract)
    assert confirmed_intent_keywords(contract) == KEYWORDS
    # Round-trips through the wire model unchanged.
    assert QueryIntentContract(**contract).model_dump()["high_level_keywords"] == HIGH


def test_single_language_corpus_asks_for_single_language_keywords():
    model = _IntentModel()
    plan_query_intent(model, "q", keywords=True, corpus_langs=["en"])
    prompt, _ = model.intent_calls[0]
    assert "provide terms in the corpus language (en)" in prompt
    assert "EACH of these corpus languages" not in prompt


def test_keywords_off_is_the_contract_and_prompt_reports_and_old_callers_get():
    model = _IntentModel()
    contract = plan_query_intent(model, "浅沟槽隔离有哪些关键参数")
    prompt, hint = model.intent_calls[0]
    assert hint == prompts.QUERY_INTENT_SCHEMA_HINT
    assert "high_level_keywords" not in prompt
    assert prompt == prompts.query_intent_prompt(
        "浅沟槽隔离有哪些关键参数", max_topics=6, history_block="",
        purpose="evidence-grounded answer",
    )
    for key in ("high_level_keywords", "low_level_keywords", "keyword_basis"):
        assert key not in contract
    assert confirmed_intent_keywords(contract) == ""


@pytest.mark.parametrize(("value", "expected"), [
    (None, []),                                    # absent / null
    ({"a": "b"}, []),                              # off-type container
    ("沟槽隔离, trench isolation;STI", ["沟槽隔离", "trench isolation", "STI"]),
    (["STI", 3, {"x": 1}, " ", "  浅沟槽  "], ["STI", "浅沟槽"]),
])
def test_keyword_lists_absorb_harmless_drift_and_drop_the_rest(value, expected):
    model = _IntentModel(high_level_keywords=value)
    contract = plan_query_intent(model, "q", keywords=True)
    assert contract["high_level_keywords"] == expected
    QueryIntentContract(**contract)               # always constructible


def test_a_reply_without_the_keyword_keys_is_todays_contract_with_empty_lists():
    model = _IntentModel()
    data = _understanding()
    del data["high_level_keywords"], data["low_level_keywords"]
    model.chat_json = lambda messages, schema_hint="", **kw: json.dumps(data)
    contract = plan_query_intent(model, "q", keywords=True)
    assert contract["high_level_keywords"] == [] == contract["low_level_keywords"]
    assert confirmed_intent_keywords(finalize_query_intent(contract)) == ""


def test_keyword_lists_are_bounded_to_the_contract_caps():
    long_item = "x" * (INTENT_KEYWORD_MAX_CHARS + 50)
    model = _IntentModel(low_level_keywords=[long_item] + [f"k{i}" for i in range(30)])
    contract = plan_query_intent(model, "q", keywords=True)
    assert len(contract["low_level_keywords"]) == INTENT_KEYWORDS_MAX_ITEMS
    assert contract["low_level_keywords"][0] == "x" * INTENT_KEYWORD_MAX_CHARS
    QueryIntentContract(**contract)
    # The wire model enforces the same caps on what a client sends back.
    with pytest.raises(ValidationError):
        QueryIntentContract(objective="q", resolved_question="q",
                            low_level_keywords=["k"] * (INTENT_KEYWORDS_MAX_ITEMS + 1))
    with pytest.raises(ValidationError):
        QueryIntentContract(objective="q", resolved_question="q",
                            high_level_keywords=[long_item])


def test_a_malformed_understanding_keeps_no_keywords():
    """Keywords written for an understanding the server discarded (invalid reply:
    its topics were replaced by the fallback) describe nothing the run searches."""
    model = _IntentModel(intent_type="not-a-type")
    contract = plan_query_intent(model, "浅沟槽隔离有哪些关键参数", keywords=True)
    assert contract["high_level_keywords"] == []
    assert contract["low_level_keywords"] == []
    assert confirmed_intent_keywords(contract) == ""


# --------------------------------------------------------------------------- #
# The edit rule
# --------------------------------------------------------------------------- #

def _seed() -> dict:
    return QueryIntentContract(**plan_query_intent(
        _IntentModel(), "浅沟槽隔离有哪些关键参数", keywords=True,
    )).model_dump()


def test_an_unedited_confirmation_keeps_the_keywords():
    seed = _seed()
    # The browser's auto-confirm / untouched review card: resolved wording as is.
    final = finalize_query_intent(seed, resolved_question=seed["resolved_question"])
    assert confirmed_intent_keywords(final) == KEYWORDS
    assert confirmed_intent_keywords(finalize_query_intent(seed)) == KEYWORDS


def test_an_edited_wording_drops_the_keywords():
    seed = _seed()
    final = finalize_query_intent(seed, resolved_question="改成问栅氧厚度的影响")
    assert final["high_level_keywords"] == [] and final["low_level_keywords"] == []
    assert final["keyword_basis"] == ""
    assert confirmed_intent_keywords(final) == ""


def test_a_clarification_answer_drops_the_keywords():
    seed = _seed()
    seed["ambiguities"] = [{"id": "a1", "question": "哪种工艺？", "reason": "",
                            "required": True, "options": []}]
    final = finalize_query_intent(seed, answers=[{"id": "a1", "answer": "28nm"}])
    assert confirmed_intent_keywords(final) == ""


@pytest.mark.parametrize("edit", ["topic_question", "topic_direction", "objective",
                                  "seed_wording", "basis_missing", "basis_forged"])
def test_any_change_to_the_understanding_the_keywords_describe_drops_them(edit):
    """The review card cannot edit topics, but any other client can: the basis
    digest catches a sub-question edit wherever it came from."""
    seed = _seed()
    if edit == "topic_question":
        seed["mandatory_topics"][0]["question"] = "栅氧厚度如何影响漏电？"
    elif edit == "topic_direction":
        seed["mandatory_topics"][0]["retrieval_queries"][1] = "gate oxide"
    elif edit == "objective":
        seed["objective"] = "另一个问题"
    elif edit == "seed_wording":
        seed["resolved_question"] = "另一种理解"
    elif edit == "basis_missing":
        seed["keyword_basis"] = ""
    else:
        seed["keyword_basis"] = "0" * 64
    final = finalize_query_intent(seed)
    assert confirmed_intent_keywords(final) == ""


# --------------------------------------------------------------------------- #
# Entry 1: notebook HTTP (/ask/intent -> /ask), the browser's path
# --------------------------------------------------------------------------- #

def _http_app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setenv("GRAPH_PPR_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from app.api import ask_routes
    from app.core.config import get_settings
    from app.main import create_app
    from app.services.embedding import FakeEmbedder

    get_settings.cache_clear()
    ask_routes.repository.cache_clear()
    client = TestClient(create_app())
    repo = ask_routes.repository()
    bind_all_embedding_clients(repo, FakeEmbedder(dim=get_settings().embed_dim))
    model = _IntentModel()
    for workload in ("reasoning_agent", "ask_answer", "evidence_refine",
                     "query_rewrite", "source_summary"):
        bind_chat_client(repo, workload, model)
    return client, repo, model


def _seed_source(repo, notebook_id, name="手册.md"):
    from app.repositories.ports import UploadedSourceFile

    source = repo.upload_sources(notebook_id, [UploadedSourceFile(
        file_name=name, content_type="text/markdown",
        content=(
            "# 隔离工艺\n\n## 浅沟槽隔离\n\n"
            f"Shallow trench isolation depth is set by the {RARE} rule; "
            "沟槽深度与侧壁角度决定隔离效果。"
        ).encode("utf-8"),
    )], scheduler=lambda _source_id: None)[0]
    repo.process_source(source.id)


def _spy_keyword_arm(repo, monkeypatch) -> list[str]:
    retrieval = repo._runtime.ask_component.retrieval
    seen: list[str] = []
    original = retrieval.keyword_chunk_candidates

    def spy(notebook_id, keywords):
        seen.append(keywords)
        return original(notebook_id, keywords)

    monkeypatch.setattr(retrieval, "keyword_chunk_candidates", spy)
    return seen


def _confirmed(contract: dict, resolved: str | None = None) -> dict:
    return {"contract": contract,
            "resolved_question": contract["resolved_question"] if resolved is None
            else resolved,
            "answers": []}


def test_notebook_http_confirmed_run_seeds_the_keyword_arm(tmp_path, monkeypatch):
    client, repo, model = _http_app(tmp_path, monkeypatch)
    notebook_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    _seed_source(repo, notebook_id)
    seen = _spy_keyword_arm(repo, monkeypatch)
    question = "浅沟槽隔离有哪些关键参数"

    preview = client.post(f"/api/notebooks/{notebook_id}/ask/intent",
                          json={"question": question})
    assert preview.status_code == 200
    contract = preview.json()
    assert contract["high_level_keywords"] == HIGH
    assert contract["low_level_keywords"] == LOW
    prompt, hint = model.intent_calls[-1]
    assert hint == prompts.QUERY_INTENT_KEYWORDS_SCHEMA_HINT
    # The notebook's corpus is bilingual: the probe the plan path uses says so.
    assert prompts.keyword_language_rule(["zh", "en"]) in prompt
    assert seen == []                             # the preview searches nothing

    # The untouched review card / the simplified UI's auto-confirm.
    answer = client.post(f"/api/notebooks/{notebook_id}/ask", json={
        "question": question, "mode": "reasoning", "intent": _confirmed(contract),
    })
    assert answer.status_code == 200, answer.text
    assert seen == [KEYWORDS]
    assert model.plan_calls == 0
    seed = next(step for step in answer.json()["reasoning_trace"]
                if step["step_type"] == "search_chunks")
    # The arm ran (written even at zero: this one-chunk corpus is already in the
    # vector arm's hits, so the lexical arm adds nothing new after dedup).
    assert "keyword_found" in seed["detail"]

    # An edited wording: the keywords describe another understanding -> no arm.
    seen.clear()
    edited = client.post(f"/api/notebooks/{notebook_id}/ask", json={
        "question": question, "mode": "reasoning",
        "intent": _confirmed(contract, "浅沟槽隔离的侧壁角度如何影响隔离效果"),
    })
    assert edited.status_code == 200, edited.text
    assert seen == []


def test_notebook_http_keywords_absent_when_the_arm_cannot_run(tmp_path, monkeypatch):
    """Passage search switched off: the understanding prompt is the plain one
    (no field without a reader) and the run has no lexical arm -- today's run."""
    client, repo, model = _http_app(
        tmp_path, monkeypatch, REASONING_CHUNK_SEARCH_ENABLED="false")
    notebook_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    _seed_source(repo, notebook_id)
    preview = client.post(f"/api/notebooks/{notebook_id}/ask/intent",
                          json={"question": "浅沟槽隔离有哪些关键参数"})
    assert preview.status_code == 200
    prompt, hint = model.intent_calls[-1]
    assert hint == prompts.QUERY_INTENT_SCHEMA_HINT
    assert "high_level_keywords" not in prompt
    contract = preview.json()
    assert contract["high_level_keywords"] == [] == contract["low_level_keywords"]


def test_corpus_language_probe_failure_falls_back_to_zh_en(tmp_path, monkeypatch):
    """Same fail-open as plan(): a probe failure is one event, never a failed preview."""
    client, repo, model = _http_app(tmp_path, monkeypatch)
    notebook_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    _seed_source(repo, notebook_id)
    retrieval = repo._runtime.ask_component.retrieval

    def broken(_notebook_id):
        raise OSError("probe down")

    monkeypatch.setattr(retrieval, "keyword_corpus_languages", broken)
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.ask_component.event_log, "emit", events.append)
    preview = client.post(f"/api/notebooks/{notebook_id}/ask/intent",
                          json={"question": "浅沟槽隔离有哪些关键参数"})
    assert preview.status_code == 200
    prompt, _ = model.intent_calls[-1]
    assert prompts.keyword_language_rule(None) in prompt
    assert {"kind": "ask_stage", "stage": "reasoning_corpus_langs",
            "status": "failed_open", "error_type": "OSError"} in events


# --------------------------------------------------------------------------- #
# Entry 2: MCP ``ask`` (in-call understanding, auto-confirmed)
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_mcp_ask_in_call_understanding_seeds_the_keyword_arm(tmp_path, monkeypatch):
    from app.api.deps import repository
    from tests.test_mcp_memory_channel_e2e import (
        FULL_SCOPES, _ask, build_mcp_app, seed_shared_notebook, token,
    )
    from tests.test_mcp_memory_channel_e2e import TERM

    env = build_mcp_app(f"sqlite:///{tmp_path / 'mcp.db'}", tmp_path, monkeypatch)
    seed_shared_notebook(env, "?")
    repo = repository()
    model = _IntentModel(high_level_keywords=[f"{TERM} bandwidth"],
                         low_level_keywords=[TERM])
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload, model)
    seen = _spy_keyword_arm(repo, monkeypatch)
    app = env["app"]
    async with app.router.lifespan_context(app):
        answer = await _ask(app, token(env, "alice", FULL_SCOPES), env["notebook"].id,
                            f"What does {TERM} say about bandwidth?")
    assert answer.get("status") != "needs_clarification", answer
    assert model.intent_calls and model.plan_calls == 0
    assert model.intent_calls[-1][1] == prompts.QUERY_INTENT_KEYWORDS_SCHEMA_HINT
    assert seen == [f"{TERM} bandwidth {TERM}"]


# --------------------------------------------------------------------------- #
# Entry 3: global Ask (preview over the participants -> confirmed run)
# --------------------------------------------------------------------------- #

def _global_setup(tmp_path, monkeypatch):
    """A real repository with one Chinese-only and one English-only library."""
    from app.models.notebooks import NotebookCreate
    from app.repositories.ports import UploadedSourceFile
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    from tests.model_testkit import RecordingModelProvider

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'g.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    model = _IntentModel()
    for workload in ("ask_answer", "query_rewrite", "reasoning_agent",
                     "evidence_refine", "source_summary"):
        bind_chat_client(repo, workload, model)
    # One Chinese-only and one English-only library: the keyword languages
    # must be the UNION over the participants, not the nominal active's.
    texts = ("浅沟槽隔离的沟槽深度决定隔离效果。",
             f"Shallow trench isolation depth follows the {RARE} rule.")
    notebooks = []
    for index, text in enumerate(texts):
        notebook = repo.create_notebook(NotebookCreate(name=f"库{index}"))
        source = repo.upload_sources(notebook.id, [UploadedSourceFile(
            file_name=f"m{index}.md", content_type="text/markdown",
            content=f"# 隔离\n\n{text}".encode("utf-8"),
        )], scheduler=lambda _source_id: None)[0]
        repo.process_source(source.id)
        notebooks.append(notebook)
    scope = {"mode": "include", "notebook_ids": [nb.id for nb in notebooks]}
    return repo, model, scope, repo.current_user().id


def test_global_ask_with_its_keyword_arm_off_pays_no_keyword_tokens(
    tmp_path, monkeypatch,
):
    """``GLOBAL_ASK_KEYWORD_ARM_ENABLED=false``: the peer keyword arm can return
    nothing, so the understanding call sends the plain hint and prompt -- no
    output tokens are spent on a field the run would never read."""
    from app.models.global_ask import GlobalAskIntentPreviewRequest

    monkeypatch.setenv("GLOBAL_ASK_KEYWORD_ARM_ENABLED", "false")
    repo, model, scope, user_id = _global_setup(tmp_path, monkeypatch)
    try:
        contract = repo._runtime.global_ask_service().preview_intent(
            GlobalAskIntentPreviewRequest(
                question="浅沟槽隔离有哪些关键参数", notebook_scope=scope),
            user_id=user_id,
        )
        prompt, hint = model.intent_calls[-1]
        assert hint == prompts.QUERY_INTENT_SCHEMA_HINT
        assert "high_level_keywords" not in prompt
        assert contract.high_level_keywords == [] == contract.low_level_keywords
    finally:
        repo.close()


def test_global_ask_preview_and_run_seed_the_keyword_arm(tmp_path, monkeypatch):
    from app.models.global_ask import GlobalAskIntentPreviewRequest, GlobalAskRequest

    repo, model, scope, user_id = _global_setup(tmp_path, monkeypatch)
    try:
        service = repo._runtime.global_ask_service()
        question = "浅沟槽隔离有哪些关键参数"

        contract = service.preview_intent(
            GlobalAskIntentPreviewRequest(question=question, notebook_scope=scope),
            user_id=user_id,
        )
        prompt, _ = model.intent_calls[-1]
        assert prompts.keyword_language_rule(["zh", "en"]) in prompt
        assert contract.high_level_keywords == HIGH

        seen = _spy_keyword_arm(repo, monkeypatch)
        job = service.start(GlobalAskRequest(
            question=question, notebook_scope=scope, mode="reasoning",
            intent=AskIntentConfirmation(
                contract=contract, resolved_question=contract.resolved_question,
                answers=[]),
        ), user_id=user_id)
        deadline = time.monotonic() + 40
        while job.job_id in service._events and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        result = service.get_job(job.job_id, user_id=user_id)
        assert result.status == "done", result.error
        assert seen == [KEYWORDS]
        assert model.plan_calls == 0
    finally:
        repo.close()
