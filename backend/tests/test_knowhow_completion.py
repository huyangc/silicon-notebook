from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.models.knowhow import VALID_ORIGINS
from app.services.knowhow import api as knowhow_api
from app.services.model_work import ModelNotConfiguredError
from tests.model_testkit import bind_chat_client


class _EvidenceContext:
    def __init__(
        self, *, knowledge=None, chunks=None, refs=None, tiers=None, sources=None
    ):
        self.knowledge = knowledge or {}
        self.chunks = chunks or {}
        self.refs = refs or {}
        self.tiers = tiers or {}
        self.sources = sources or {}

    def tier_map(self, notebook_ids):
        return {notebook_id: self.tiers.get(notebook_id, "personal") for notebook_id in notebook_ids}

    def source_metadata(self, source_ids):
        return {source_id: self.sources.get(source_id, {}) for source_id in source_ids}

    def knowhow_refs_for(self, _element_ids):
        return self.refs

    def knowledge_context(self, _notebook_id, _hits):
        return "(none)", self.knowledge if _hits else {}

    def chunk_context(self, _chunks, *, notebook_id, budget_chars):
        return "(none)", self.chunks if _chunks else {}


@pytest.fixture(autouse=True)
def _fake_reasoning(monkeypatch):
    from app.services import reasoning_retrieval

    calls: list[dict] = []
    result = SimpleNamespace(
        top_hits=[], elements=[], chunks=[], chains=[], trace=[]
    )

    class _Retriever:
        def run(self, notebook_id, question, **kwargs):
            calls.append({
                "notebook_id": notebook_id,
                "question": question,
                **kwargs,
            })
            return result

    factory_calls: list[dict] = []

    def _factory(_repo, _settings, **kwargs):
        factory_calls.append(kwargs)
        return _Retriever()

    monkeypatch.setattr(
        reasoning_retrieval, "reasoning_retriever_from_repository", _factory
    )
    return SimpleNamespace(calls=calls, factory_calls=factory_calls, result=result)


class _CompletionClient:
    configured = True
    model = "fake-completion-model"

    def __init__(self, response: object):
        self.response = response
        self.calls: list[tuple[list[dict], str, dict]] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append((messages, schema_hint, kwargs))
        return json.dumps(self.response, ensure_ascii=False)


class _RaisingCompletionClient:
    configured = True
    model = "fake-completion-model"

    def chat_json(self, *args, **kwargs):
        raise RuntimeError("upstream failed")


def _repo_with_client(client, *, evidence_context=None, reasoning_client=None):
    errors: list[tuple[str, str, str]] = []

    def chat(workload_id):
        assert workload_id in {"knowhow_complete", "reasoning_agent"}
        return (
            client if workload_id == "knowhow_complete"
            else reasoning_client or SimpleNamespace(configured=True)
        )

    models = SimpleNamespace(
        chat=chat,
        note_model_error=lambda stage, error, *, workload_id: errors.append(
            (stage, workload_id, type(error).__name__)
        ),
    )
    runtime = SimpleNamespace(
        models=models,
        settings=SimpleNamespace(),
        evidence_context_component=evidence_context or _EvidenceContext(),
    )
    return SimpleNamespace(_runtime=runtime, settings=runtime.settings), errors


def _table(reference_count: int = 2) -> dict:
    columns = [
        {"id": "anchor", "name": "场景", "kind": "attribute", "position": 0},
        {"id": "symptom", "name": "现象", "kind": "attribute", "position": 1},
        {"id": "cause", "name": "根因", "kind": "attribute", "position": 2},
        {"id": "method", "name": "方法", "kind": "procedure", "position": 3},
    ]
    rows = [{
        "id": "current",
        "position": 0,
        "cells": {"anchor": "时序", "symptom": "setup violation at endpoint"},
    }]
    for index in range(reference_count):
        rows.append({
            "id": f"ref-{index}",
            "position": index + 1,
            "cells": {
                "anchor": "时序" if index % 2 == 0 else "功耗",
                "symptom": (
                    "setup violation at endpoint"
                    if index in {0, 3}
                    else f"setup timing issue {index}"
                ),
                "cause": f"参考根因 {index}",
                "method": f"1. 参考方法 {index}",
            },
        })
    return {
        "id": "table",
        "anchor_column_id": "anchor",
        "columns": columns,
        "rows": rows,
    }


def test_resolve_targets_defaults_deduplicates_and_rejects_invalid_targets():
    table = _table()

    row, targets, known = knowhow_api.resolve_completion_request(
        table, "current", None
    )
    assert row["id"] == "current"
    assert [item["id"] for item in targets] == ["cause", "method"]
    assert [item["id"] for item in known] == ["anchor", "symptom"]

    _, targets, _ = knowhow_api.resolve_completion_request(
        table, "current", ["cause", "cause"]
    )
    assert [item["id"] for item in targets] == ["cause"]

    for invalid in (["missing"], ["anchor"], ["symptom"], []):
        with pytest.raises(ValueError):
            knowhow_api.resolve_completion_request(table, "current", invalid)


def test_completion_requires_a_non_empty_condition_cell_and_caps_targets():
    table = _table()
    table["rows"][0]["cells"] = {}
    with pytest.raises(ValueError, match="已知内容"):
        knowhow_api.resolve_completion_request(table, "current", ["cause"])

    many_columns = [
        {"id": f"c-{index}", "name": str(index), "kind": "attribute"}
        for index in range(knowhow_api.MAX_COMPLETION_TARGETS + 1)
    ]
    table = {
        "anchor_column_id": None,
        "columns": [{"id": "known", "name": "已知", "kind": "attribute"}, *many_columns],
        "rows": [{"id": "r", "cells": {"known": "x"}}],
    }
    with pytest.raises(ValueError, match="一次最多"):
        knowhow_api.resolve_completion_request(
            table, "r", [column["id"] for column in many_columns]
        )


def test_whitespace_only_cell_is_not_an_empty_completion_target():
    table = _table()
    table["rows"][0]["cells"]["cause"] = "   \n"

    with pytest.raises(ValueError, match="只能补全"):
        knowhow_api.resolve_completion_request(
            table, "current", ["cause"]
        )

    _, targets, _ = knowhow_api.resolve_completion_request(table, "current", None)
    assert [column["id"] for column in targets] == ["method"]


def test_known_condition_columns_are_capped_with_anchor_first():
    extra_columns = [
        {"id": f"known-{index}", "name": f"K{index}", "kind": "attribute", "position": index}
        for index in range(knowhow_api.MAX_COMPLETION_KNOWN_COLUMNS + 20)
    ]
    target = {"id": "target", "name": "Target", "kind": "attribute", "position": 999}
    table = {
        "anchor_column_id": "anchor",
        "columns": [
            {"id": "anchor", "name": "A", "kind": "attribute", "position": 998},
            *extra_columns,
            target,
        ],
        "rows": [{
            "id": "current",
            "cells": {
                "anchor": "same-group",
                **{column["id"]: "known" for column in extra_columns},
            },
        }],
    }

    _, _, known = knowhow_api.resolve_completion_request(
        table, "current", ["target"]
    )

    assert len(known) == knowhow_api.MAX_COMPLETION_KNOWN_COLUMNS
    assert known[0]["id"] == "anchor"


def test_reference_ranking_is_deterministic_and_bounded_to_eight():
    table = _table(reference_count=12)
    current = table["rows"][0]

    references = knowhow_api.select_completion_references(
        table,
        current,
        ["cause", "method"],
        ["anchor", "symptom"],
        limit=100,
    )

    assert len(references) == knowhow_api.MAX_COMPLETION_REFERENCES == 8
    # ref-0 shares the anchor and exact known cells, so it deterministically
    # outranks every sibling regardless of source insertion order.
    assert references[0]["id"] == "ref-0"
    assert all(
        any(row["cells"].get(target) for target in ("cause", "method"))
        for row in references
    )


def test_reference_scoring_never_touches_rows_outside_pool_or_unbounded_cell_text(monkeypatch):
    class _ExplodingCells(dict):
        def get(self, *_args, **_kwargs):
            raise AssertionError("row outside bounded candidate pool was scored")

    table = _table(reference_count=0)
    current = table["rows"][0]
    huge = "setup " + ("x" * 200_000)
    table["rows"].extend(
        {
            "id": f"bounded-{index}",
            "position": index + 1,
            "cells": {
                "anchor": "时序",
                "symptom": huge,
                "cause": f"根因 {index}",
            },
        }
        for index in range(knowhow_api.MAX_COMPLETION_CANDIDATES)
    )
    table["rows"].append({
        "id": "outside-pool",
        "position": 999_999,
        "cells": _ExplodingCells(),
    })
    original_normalized = knowhow_api._completion_normalized
    inspected_lengths: list[int] = []

    def bounded_normalized(value):
        inspected_lengths.append(len(value))
        assert len(value) <= knowhow_api.MAX_COMPLETION_SCORE_CELL_CHARS
        return original_normalized(value)

    monkeypatch.setattr(knowhow_api, "_completion_normalized", bounded_normalized)

    references = knowhow_api.select_completion_references(
        table, current, ["cause"], ["anchor", "symptom"], limit=8
    )

    assert len(references) == 8
    assert inspected_lengths
    assert all(reference["id"] != "outside-pool" for reference in references)


def test_prompt_has_a_global_hard_limit_and_never_truncates_rules_or_schema():
    noisy = ('"\\\n' * 20_000) + "尾部"
    known_columns = [
        {
            "id": f"known-{index}",
            "name": "超长列名" * 2_000,
            "kind": "attribute",
            "position": index,
        }
        for index in range(knowhow_api.MAX_COMPLETION_KNOWN_COLUMNS)
    ]
    target_columns = [
        {
            "id": f"target-{index}",
            "name": "目标列名" * 2_000,
            "kind": "attribute",
            "position": 100 + index,
        }
        for index in range(knowhow_api.MAX_COMPLETION_TARGETS)
    ]
    current = {
        "id": "current",
        "cells": {column["id"]: noisy for column in known_columns},
    }
    references = [
        {
            "id": f"ref-{index}",
            "cells": {
                **{column["id"]: noisy for column in known_columns},
                **{column["id"]: noisy for column in target_columns},
            },
        }
        for index in range(knowhow_api.MAX_COMPLETION_REFERENCES)
    ]
    table = {
        "anchor_column_id": None,
        "columns": [*known_columns, *target_columns],
        "rows": [current, *references],
    }

    prompt, included = knowhow_api._completion_prompt(
        table, current, target_columns, known_columns, references
    )

    assert len(prompt) <= knowhow_api.MAX_COMPLETION_PROMPT_CHARS
    assert len(included) < len(references)  # least relevant rows were removed
    assert prompt.startswith(knowhow_api._COMPLETION_PROMPT_PREFIX)
    assert prompt.endswith(knowhow_api._COMPLETION_PROMPT_SUFFIX)
    assert "不得凭空生成数值、器件型号、命令、文件路径、工具参数" in prompt
    payload_text = prompt.removeprefix(
        knowhow_api._COMPLETION_PROMPT_PREFIX
    ).removesuffix(knowhow_api._COMPLETION_PROMPT_SUFFIX)
    payload = json.loads(payload_text)
    assert len(payload["column_schema"]) <= (
        knowhow_api.MAX_COMPLETION_KNOWN_COLUMNS
        + knowhow_api.MAX_COMPLETION_TARGETS
    )
    assert all(
        len(column["name"]) <= knowhow_api._COMPLETION_COLUMN_NAME_CHAR_LIMIT
        for column in payload["column_schema"]
    )


def test_library_evidence_has_independent_budget_and_prompt_stays_globally_bounded():
    table = _table(reference_count=12)
    current = table["rows"][0]
    result = SimpleNamespace(
        top_hits=[],
        chunks=[],
        elements=[
            SimpleNamespace(
                element_id=f"el-{index}",
                source_id=f"source-{index}",
                source_title="文档" + ("名" * 500),
                location_label="章节" + ("路" * 500),
                element_type="paragraph",
                text="证据" * 20_000,
            )
            for index in range(100)
        ],
    )
    repo, _errors = _repo_with_client(_CompletionClient({"suggestions": []}))

    evidence = knowhow_api._completion_library_evidence(
        repo._runtime.evidence_context_component, table, current, result, "nb"
    )
    serialized = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    prompt, _refs = knowhow_api._completion_prompt(
        table,
        current,
        table["columns"][2:],
        table["columns"][:2],
        table["rows"][1:],
        evidence,
    )

    assert len(evidence) <= knowhow_api.MAX_COMPLETION_LIBRARY_EVIDENCE
    assert len(serialized) <= knowhow_api.MAX_COMPLETION_LIBRARY_EVIDENCE_CHARS + 2
    assert len(prompt) <= knowhow_api.MAX_COMPLETION_PROMPT_CHARS


def test_active_source_element_evidence_uses_the_active_notebook_tier():
    table = _table(reference_count=0)
    current = table["rows"][0]
    result = SimpleNamespace(
        top_hits=[],
        chunks=[],
        elements=[SimpleNamespace(
            element_id="external-element",
            source_id="uploaded-source",
            source_title="公共规范",
            location_label="第 1 节",
            element_type="paragraph",
            text="证据正文",
        )],
    )
    evidence_context = _EvidenceContext(tiers={"nb": "base"})

    evidence = knowhow_api._completion_library_evidence(
        evidence_context, table, current, result, "nb"
    )

    assert evidence[0]["tier"] == "base"


def test_complete_row_builds_bounded_prompt_and_filters_model_output(
    _fake_reasoning,
):
    table = _table(reference_count=12)
    response = {
        "suggestions": [
            {
                "column_id": "cause",
                "suggestion_md": "数据路径延迟偏大",
                "confidence": "certain",
                "based_on_row_ids": ["ref-0", "unknown", "ref-0"],
                "basis": "与同场景、同现象参考行一致",
                "abstain_reason": "should be removed",
            },
            {
                "column_id": "method",
                "suggestion_md": "   ",
                "confidence": "high",
                "based_on_row_ids": ["ref-1"],
                "basis": "",
                "abstain_reason": "参考方法不一致",
            },
            {
                "column_id": "unknown-column",
                "suggestion_md": "ignore me",
                "confidence": "high",
                "based_on_row_ids": ["ref-0"],
                "basis": "",
                "abstain_reason": "",
            },
        ]
    }
    client = _CompletionClient(response)
    repo, errors = _repo_with_client(client)

    result = knowhow_api.complete_row(repo, "nb", table, "current")

    assert errors == []
    assert result == {
        "suggestions": [
            {
                "column_id": "cause",
                "suggestion_md": "数据路径延迟偏大",
                "confidence": "low",
                "based_on_row_ids": ["ref-0"],
                "evidence_keys": [],
                "basis": "与同场景、同现象参考行一致",
                "abstain_reason": "",
            },
            {
                "column_id": "method",
                "suggestion_md": None,
                "confidence": "low",
                "based_on_row_ids": ["ref-1"],
                "evidence_keys": [],
                "basis": "",
                "abstain_reason": "参考方法不一致",
            },
        ],
        "retrieval_mode": "reasoning",
        "retrieval_scope": "active_and_mounted",
        "retrieval_status": "no_evidence",
        "reasoning_trace": [],
        "evidence": [],
    }
    assert len(client.calls) == 1
    assert len(_fake_reasoning.calls) == 1  # one run covers both target columns
    messages, schema_hint, _kwargs = client.calls[0]
    assert schema_hint == knowhow_api._COMPLETION_SCHEMA_HINT
    assert messages[0] == {
        "role": "system",
        "content": knowhow_api._COMPLETION_SYSTEM_INSTRUCTION,
    }
    prompt = messages[-1]["content"]
    assert "column_schema" in prompt
    assert "current" in prompt and "setup violation at endpoint" in prompt
    assert "cause" in prompt and "method" in prompt
    assert "不得凭空生成数值、器件型号、命令、文件路径、工具参数" in prompt
    assert '"row_id":"ref-0"' in prompt
    assert prompt.count('"row_id":') == knowhow_api.MAX_COMPLETION_REFERENCES + 1
    assert _fake_reasoning.factory_calls == [{"fail_closed": True}]


def _kg_evidence_map(*, tier="personal", excerpt="库内证据"):
    return {
        "k1": {
            "object_id": "ko-other",
            "object_type": "claim",
            "name": "库内结论",
            "definition": excerpt,
            "snippet": excerpt,
            "source_title": "参考文档",
            "location_label": "第 2 节",
            "tier": tier,
            "knowhow": None,
        }
    }


def test_library_only_evidence_can_suggest_and_single_channel_caps_high(
    _fake_reasoning,
):
    table = _table(reference_count=0)
    _fake_reasoning.result.top_hits = [SimpleNamespace(
        object_id="ko-other",
        payload={"name": "库内结论"},
    )]
    completion = _CompletionClient({
        "suggestions": [{
            "column_id": "cause",
            "suggestion_md": "库内支持的根因",
            "confidence": "high",
            "based_on_row_ids": [],
            "evidence_keys": ["e1"],
            "basis": "来自参考库",
            "abstain_reason": "",
        }]
    })
    repo, _errors = _repo_with_client(
        completion,
        evidence_context=_EvidenceContext(knowledge=_kg_evidence_map(tier="base")),
    )

    result = knowhow_api.complete_row(
        repo, "nb", table, "current", ["cause"]
    )

    suggestion = result["suggestions"][0]
    assert suggestion["suggestion_md"] == "库内支持的根因"
    assert suggestion["confidence"] == "medium"
    assert suggestion["evidence_keys"] == ["e1"]
    assert result["retrieval_status"] == "succeeded"
    assert result["evidence"][0]["tier"] == "base"
    assert len(_fake_reasoning.calls) == 1
    assert _fake_reasoning.calls[0]["top_n"] == 12
    assert _fake_reasoning.calls[0]["max_steps"] == 6
    assert "不可信数据" in _fake_reasoning.calls[0]["question"]


def test_table_and_library_evidence_allow_high_confidence(_fake_reasoning):
    table = _table(reference_count=1)
    _fake_reasoning.result.top_hits = [SimpleNamespace(
        object_id="ko-other", payload={"name": "库内结论"}
    )]
    completion = _CompletionClient({
        "suggestions": [{
            "column_id": "cause",
            "suggestion_md": "双通道根因",
            "confidence": "high",
            "based_on_row_ids": ["ref-0"],
            "evidence_keys": ["e1"],
            "basis": "表内与库内一致",
            "abstain_reason": "",
        }]
    })
    repo, _errors = _repo_with_client(
        completion,
        evidence_context=_EvidenceContext(knowledge=_kg_evidence_map()),
    )

    result = knowhow_api.complete_row(
        repo, "nb", table, "current", ["cause"]
    )

    assert result["suggestions"][0]["confidence"] == "high"
    assert result["suggestions"][0]["based_on_row_ids"] == ["ref-0"]
    assert result["suggestions"][0]["evidence_keys"] == ["e1"]


def test_forged_evidence_key_without_valid_row_forces_abstention(_fake_reasoning):
    table = _table(reference_count=0)
    _fake_reasoning.result.top_hits = [SimpleNamespace(
        object_id="ko-other", payload={"name": "库内结论"}
    )]
    completion = _CompletionClient({
        "suggestions": [{
            "column_id": "cause",
            "suggestion_md": "伪造支持",
            "confidence": "high",
            "based_on_row_ids": ["made-up-row"],
            "evidence_keys": ["made-up-key"],
            "basis": "伪造引用",
            "abstain_reason": "",
        }]
    })
    repo, _errors = _repo_with_client(
        completion,
        evidence_context=_EvidenceContext(knowledge=_kg_evidence_map()),
    )

    result = knowhow_api.complete_row(
        repo, "nb", table, "current", ["cause"]
    )

    suggestion = result["suggestions"][0]
    assert suggestion["suggestion_md"] is None
    assert suggestion["based_on_row_ids"] == []
    assert suggestion["evidence_keys"] == []
    assert "有效证据" in suggestion["abstain_reason"]


def test_current_table_projection_is_excluded_from_library_channel(_fake_reasoning):
    from app.services.knowhow.projection import cell_chunk_id, element_id

    table = _table(reference_count=0)
    table["hidden_source_id"] = "source-current-table"
    _fake_reasoning.result.top_hits = [SimpleNamespace(
        object_id="ko-current",
        payload={"table_id": "table", "rows": ["ref-elsewhere"]},
    )]
    _fake_reasoning.result.chunks = [
        SimpleNamespace(
            chunk_id="chunk-other",
            source_id="source-current-table",
            element_ids=["element-other"],
        ),
        SimpleNamespace(
            chunk_id=cell_chunk_id("current", 0),
            source_id="legacy-source",
            element_ids=[],
        ),
    ]
    _fake_reasoning.result.elements = [
        SimpleNamespace(
            element_id="element-other",
            source_id="source-current-table",
        ),
        SimpleNamespace(
            element_id=element_id("current", "symptom"),
            source_id="legacy-source",
        ),
    ]
    completion = _CompletionClient({"suggestions": []})
    repo, _errors = _repo_with_client(
        completion,
        evidence_context=_EvidenceContext(
            knowledge=_kg_evidence_map(),
            chunks={
                "k1": {
                    "object_id": "chunk-other", "name": "同表块",
                    "snippet": "不应进入", "source_title": "同表",
                    "location_label": "", "tier": "personal", "knowhow": None,
                }
            },
        ),
    )

    result = knowhow_api.complete_row(
        repo, "nb", table, "current", ["cause"]
    )

    assert result["retrieval_status"] == "no_evidence"
    assert result["evidence"] == []
    assert result["suggestions"][0]["suggestion_md"] is None
    assert completion.calls == []


def test_reasoning_candidate_filter_excludes_memory_and_current_table_before_model():
    table = _table(reference_count=0)
    table["hidden_source_id"] = "source-current-table"
    context = _EvidenceContext(
        sources={
            "source-memory": {"source_type": "memory"},
            "source-normal": {"source_type": "file"},
            "source-current-table": {"source_type": "knowhow"},
        },
        refs={"element-current-table": {"table_id": "table", "row_id": "r2"}},
    )
    candidate_filter = knowhow_api._completion_reasoning_candidate_filter(
        context, table
    )
    memory_knowledge = SimpleNamespace(
        object_id="memory-ko",
        payload={"name": "private memory"},
        evidence=[SimpleNamespace(source_id="source-memory", element_id="memory-el")],
    )
    current_table_knowledge = SimpleNamespace(
        object_id="current-ko",
        payload={"name": "self", "table_id": "table"},
        evidence=[],
    )
    normal_knowledge = SimpleNamespace(
        object_id="normal-ko",
        payload={"name": "normal"},
        evidence=[SimpleNamespace(source_id="source-normal", element_id="normal-el")],
    )

    assert candidate_filter(
        "knowledge", [memory_knowledge, current_table_knowledge, normal_knowledge]
    ) == [normal_knowledge]

    memory_element = SimpleNamespace(
        element_id="memory-el", source_id="source-memory"
    )
    current_element = SimpleNamespace(
        element_id="element-current-table", source_id="legacy-source"
    )
    normal_element = SimpleNamespace(
        element_id="normal-el", source_id="source-normal"
    )
    assert candidate_filter(
        "element", [memory_element, current_element, normal_element]
    ) == [normal_element]

    memory_chain = SimpleNamespace(hops=(SimpleNamespace(evidence=[{
        "source_id": "source-memory", "element_id": "memory-el"
    }]),))
    normal_chain = SimpleNamespace(hops=(SimpleNamespace(evidence=[{
        "source_id": "source-normal", "element_id": "normal-el"
    }]),))
    assert candidate_filter("chain", [memory_chain, normal_chain]) == [normal_chain]


def test_reasoning_model_unconfigured_and_execution_failure_are_safe(
    monkeypatch,
):
    from app.services import reasoning_retrieval

    table = _table()
    completion = _CompletionClient({"suggestions": []})
    repo, errors = _repo_with_client(
        completion,
        reasoning_client=SimpleNamespace(configured=False),
    )
    with pytest.raises(ModelNotConfiguredError):
        knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])
    assert errors == []

    class _BrokenRetriever:
        def run(self, *_args, **_kwargs):
            raise RuntimeError("reasoning unavailable")

    monkeypatch.setattr(
        reasoning_retrieval,
        "reasoning_retriever_from_repository",
        lambda _repo, _settings, **_kwargs: _BrokenRetriever(),
    )
    repo, errors = _repo_with_client(completion)
    with pytest.raises(
        knowhow_api.KnowhowCompletionUnavailable, match="逐步推理检索"
    ):
        knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])
    assert errors == [("reasoning_agent", "reasoning_agent", "RuntimeError")]


def test_no_table_or_library_evidence_returns_abstentions_after_one_reasoning_run(
    _fake_reasoning,
):
    table = _table(reference_count=0)
    completion = _CompletionClient({"suggestions": []})
    repo, _errors = _repo_with_client(completion)

    result = knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])

    assert result["suggestions"][0]["suggestion_md"] is None
    assert result["suggestions"][0]["confidence"] == "low"
    assert result["suggestions"][0]["abstain_reason"]
    assert result["retrieval_status"] == "no_evidence"
    assert len(_fake_reasoning.calls) == 1
    assert completion.calls == []


def test_unconfigured_and_bad_responses_have_distinct_failure_semantics():
    table = _table()
    repo, errors = _repo_with_client(SimpleNamespace(configured=False))
    with pytest.raises(ModelNotConfiguredError, match="配置"):
        knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])
    assert errors == []

    for client in (
        _RaisingCompletionClient(),
        _CompletionClient({"unexpected": []}),
    ):
        repo, errors = _repo_with_client(client)
        with pytest.raises(knowhow_api.KnowhowCompletionUnavailable):
            knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])
        assert errors == [("knowhow_complete", "knowhow_complete", "RuntimeError")] or errors == [
            ("knowhow_complete", "knowhow_complete", "ValueError")
        ]


def test_model_resolver_and_configured_property_failures_are_not_leaked():
    table = _table()
    errors: list[tuple[str, str, str]] = []

    def note(stage, error, *, workload_id):
        errors.append((stage, workload_id, type(error).__name__))

    models = SimpleNamespace(
        chat=lambda _workload_id: (_ for _ in ()).throw(RuntimeError("provider closed")),
        note_model_error=note,
    )
    repo = SimpleNamespace(_runtime=SimpleNamespace(models=models))
    with pytest.raises(knowhow_api.KnowhowCompletionUnavailable):
        knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])
    assert errors == [("reasoning_agent", "reasoning_agent", "RuntimeError")]

    errors.clear()

    class _BrokenConfigured:
        @property
        def configured(self):
            raise RuntimeError("client was closed")

    models.chat = lambda _workload_id: _BrokenConfigured()
    with pytest.raises(knowhow_api.KnowhowCompletionUnavailable):
        knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])
    assert errors == [("reasoning_agent", "reasoning_agent", "RuntimeError")]

    errors.clear()

    class _LateUnconfigured:
        @property
        def configured(self):
            raise ModelNotConfiguredError("尚未配置模型，无法智能补全")

    models.chat = lambda _workload_id: _LateUnconfigured()
    with pytest.raises(ModelNotConfiguredError):
        knowhow_api.complete_row(repo, "nb", table, "current", ["cause"])
    assert errors == []


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _app_repo():
    from app.api.deps import repository

    return repository()


def _seed_http(client, headers):
    notebook_id = client.post(
        "/api/notebooks", json={"name": "N"}, headers=headers
    ).json()["id"]
    table = client.post(
        f"/api/notebooks/{notebook_id}/knowhow",
        headers=headers,
        json={
            "title": "T",
            "columns": [
                {"name": "场景", "kind": "attribute"},
                {"name": "现象", "kind": "attribute"},
                {"name": "根因", "kind": "attribute"},
            ],
            "anchor_index": 0,
        },
    ).json()
    anchor_id, symptom_id, cause_id = [column["id"] for column in table["columns"]]

    def add(cells):
        response = client.post(
            f"/api/notebooks/{notebook_id}/knowhow/{table['id']}/rows",
            headers=headers,
            json={"cells": cells},
        )
        assert response.status_code == 200, response.text
        return response.json()

    current = add({anchor_id: "时序", symptom_id: "setup violation"})
    reference = add({
        anchor_id: "时序",
        symptom_id: "setup violation",
        cause_id: "数据路径延迟偏大",
    })
    return notebook_id, table, current, reference, anchor_id, symptom_id, cause_id


def _complete(client, headers, notebook_id, table_id, row_id, body):
    return client.post(
        f"/api/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/complete",
        headers=headers,
        json=body,
    )


def test_completion_http_permissions_validation_no_write_and_no_schedule(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner = _login(client, "a00003101")
    notebook_id, table, current, reference, anchor_id, symptom_id, cause_id = _seed_http(
        client, owner
    )
    fake = _CompletionClient({
        "suggestions": [{
            "column_id": cause_id,
            "suggestion_md": "建议根因",
            "confidence": "high",
            "based_on_row_ids": [reference["id"]],
            "basis": "同场景同现象",
            "abstain_reason": "",
        }]
    })
    bind_chat_client(
        _app_repo(), "reasoning_agent", SimpleNamespace(configured=True)
    )
    bind_chat_client(_app_repo(), "knowhow_complete", fake)
    scheduled: list[str] = []
    monkeypatch.setattr(
        knowhow_api,
        "get_scheduler",
        lambda _repo: SimpleNamespace(schedule=lambda table_id: scheduled.append(table_id)),
    )

    response = _complete(
        client, owner, notebook_id, table["id"], current["id"], {}
    )
    assert response.status_code == 200, response.text
    assert response.json()["suggestions"][0]["suggestion_md"] == "建议根因"
    detail = client.get(
        f"/api/notebooks/{notebook_id}/knowhow/{table['id']}", headers=owner
    ).json()
    live_current = next(row for row in detail["rows"] if row["id"] == current["id"])
    assert live_current["cells"].get(cause_id, "") == ""
    assert scheduled == []

    for target in (["missing"], [anchor_id], [symptom_id]):
        invalid = _complete(
            client,
            owner,
            notebook_id,
            table["id"],
            current["id"],
            {"target_column_ids": target},
        )
        assert invalid.status_code == 400

    reader = _login(client, "b00003101")
    reader_id = client.get("/api/me", headers=reader).json()["id"]
    _app_repo().add_member(notebook_id, reader_id)
    denied = _complete(
        client,
        reader,
        notebook_id,
        table["id"],
        current["id"],
        {"target_column_ids": [cause_id]},
    )
    assert denied.status_code == 404


def test_completion_http_model_configuration_and_failure_statuses(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner = _login(client, "a00003103")
    notebook_id, table, current, _reference, _anchor, _symptom, cause_id = _seed_http(
        client, owner
    )
    body = {"target_column_ids": [cause_id]}

    bind_chat_client(
        _app_repo(), "reasoning_agent", SimpleNamespace(configured=True)
    )

    bind_chat_client(
        _app_repo(), "knowhow_complete", SimpleNamespace(configured=False)
    )
    unconfigured = _complete(
        client, owner, notebook_id, table["id"], current["id"], body
    )
    assert unconfigured.status_code == 400
    assert unconfigured.json()["detail"] == "尚未配置模型，无法智能补全"

    bind_chat_client(_app_repo(), "knowhow_complete", _RaisingCompletionClient())
    failed = _complete(
        client, owner, notebook_id, table["id"], current["id"], body
    )
    assert failed.status_code == 502


def test_llm_complete_origin_is_accepted_recorded_and_guarded(tmp_path, monkeypatch):
    assert "llm_complete" in VALID_ORIGINS
    client = _client(tmp_path, monkeypatch)
    owner = _login(client, "a00003102")
    notebook_id, table, current, _reference, _anchor, _symptom, cause_id = _seed_http(
        client, owner
    )
    cell_url = (
        f"/api/notebooks/{notebook_id}/knowhow/{table['id']}/rows/"
        f"{current['id']}/cells/{cause_id}"
    )

    accepted = client.patch(
        cell_url,
        headers=owner,
        json={
            "content_md": "已接受的补全",
            "expected_before": "",
            "origin": "llm_complete",
        },
    )
    assert accepted.status_code == 200, accepted.text
    history = client.get(
        f"{cell_url}/history", headers=owner
    )
    assert history.status_code == 200, history.text
    assert history.json()[0]["origin"] == "llm_complete"
    assert history.json()[0]["before"] is None

    stale = client.patch(
        cell_url,
        headers=owner,
        json={
            "content_md": "不应覆盖",
            "expected_before": "",
            "origin": "llm_complete",
        },
    )
    assert stale.status_code == 409
