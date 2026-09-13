"""Exercise the real synthesis boundary, not a stub's ability to follow prose.

The fake returns fixed citation-bearing output. These tests protect prompt wiring,
evidence identities/conditions and existing call budgets; model comparison quality
and output granularity still require the recorded live evaluation.
"""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.models.ask import QueryIntentContract
from app.models.schemas import NotebookCreate
from app.services.collection_enumeration import EnumerationCoverage, SourceItem
from app.services.collection_enumeration_answer import enumeration_prompt_block
from app.services.embedding import FakeEmbedder
from app.services.prompt_layers import L1_FRAGMENTS, fragment_text
from app.services.query_intent import auto_ask_mode_from_intent, plan_query_intent
from app.services.reasoning_retrieval import CollectionEnumerationOutcome
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


class _RecordingAnswer:
    configured = True

    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.settings = SimpleNamespace(answer_max_tokens=913)

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append((messages, schema_hint, kwargs))
        return json.dumps({"answer": self.answer, "grounded": True})


@pytest.fixture
def repo(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=str(tmp_path / "storage"),
        embed_dim=16,
        llm_log_enabled=False,
        kg_query_refine_enabled=False,
    )
    result = SQLiteRepository(settings)
    bind_all_embedding_clients(result, FakeEmbedder(dim=16))
    yield result
    result.close()


# Deliberately synthetic, including a ranking reversal and differently named
# protocols. A comparison must retain those associations all the way to the LLM.
_BENCHMARK_EVIDENCE = (
    "Table: matched evaluations; accuracy in %, higher is better.\n"
    "| Model | Configuration | Accuracy strict | Accuracy flexible | Error % (lower better) |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| System A | 8 steps, no system prompt | 63.20 | 68.10 | 7.50 |\n"
    "| System B-v1 | 8 steps, no system prompt | 61.10 | 70.30 | 6.20 |\n"
    "| System B-v2 | 8 steps, no system prompt | 69.50 | 74.00 | 7.50 |\n"
    "| System A | 16 steps, with system prompt | 71.40 | 75.20 | 5.80 |\n"
    "Table: Prompt variants, accuracy %. Slash order is without/with system prompt.\n"
    "| Model | Accuracy |\n"
    "| System A | 48.20 / 51.40 |\n"
    "| System B-v1 | 49.75 / 50.10 |\n"
    "Table: unlabeled paired scores, accuracy %. The source gives no label order.\n"
    "| System C | 46.30 / 52.90 |\n"
    "Author's qualitative claim: System A outperforms the B family."
)


@pytest.mark.parametrize("surface", ["chunk", "mix", "reasoning", "sectioned"])
def test_answer_fragments_and_numeric_conditions_reach_real_synthesis(
    repo, monkeypatch, surface,
):
    notebook = repo.create_notebook(NotebookCreate(name="Evaluation fixture"))
    service = repo._runtime.ask_component
    # Replacing a registered fragment at its owning seam proves the caller reads
    # that seam; merely searching a rendered default string would miss a copy.
    sentinels = []
    for fragment_id in (
        "answer.style_language", "answer.mechanism_organization",
        "answer.numeric_attribution",
    ):
        sentinel = f"[test-fragment:{fragment_id}]"
        sentinels.append(sentinel)
        fragment = L1_FRAGMENTS[fragment_id]
        monkeypatch.setitem(
            L1_FRAGMENTS, fragment_id,
            replace(fragment, text=fragment.text.rstrip("\n") + sentinel + "\n"),
        )
    sectioned = surface == "sectioned"
    key = "k10001" if sectioned else "k1"
    client = _RecordingAnswer(f"The table provides the measured values. [{key}]")
    bind_chat_client(repo, "ask_answer", client)
    chunk = RetrievedChunk(
        "benchmark-chunk", "benchmark-source", "Measured results", "Table",
        _BENCHMARK_EVIDENCE,
    )
    baseline = {}
    if surface == "chunk":
        result = service._answer_chunks(
            "Compare the measured results and retain exceptions.", [chunk],
            notebook_id=notebook.id, baseline_sink=baseline,
        )
    elif surface == "mix":
        result = service._answer_mix(
            "Compare the measured results and retain exceptions.", [chunk], "", {},
            notebook_id=notebook.id, baseline_sink=baseline,
        )
    else:
        result = service._answer_reasoning(
            notebook.id, "Compare the measured results and retain exceptions.", [], [],
            chunks=[chunk], baseline_sink=baseline, chunk_context_chars=1200,
            kg_context_chars=500, sectioned=sectioned,
            key_offset=10000 if sectioned else 0,
            section_title="Measured results" if sectioned else "",
        )
        assert baseline["budget_chars"] == 1700
    assert len(client.calls) == 1
    messages, schema_hint, kwargs = client.calls[0]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    prompt = messages[0]["content"]
    for sentinel in sentinels:
        assert prompt.count(sentinel) == 1
    assert _BENCHMARK_EVIDENCE in baseline["context_block"]
    assert baseline["context_block"] in prompt
    assert len(baseline["context_block"]) <= baseline["budget_chars"]
    assert json.loads(schema_hint) == {"answer": "", "grounded": True}
    assert kwargs["max_tokens"] == 913
    if surface in {"reasoning", "sectioned"}:
        assert kwargs["timeout"] == repo.settings.reasoning_timeout_seconds
        assert kwargs["max_retries"] == repo.settings.reasoning_max_retries
    assert result[1] is True
    assert [(anchor.key, anchor.object_id, anchor.source_id) for anchor in result[2]] == [
        (key, "benchmark-chunk", "benchmark-source"),
    ]


@pytest.mark.parametrize("question", [
    "比较评测表现，给出总体判断和重要例外。",
    "逐项比较全部指标，输出包含每个版本和配置的完整数值矩阵。",
])
def test_comparison_granularity_never_projects_away_requested_matrix(repo, question):
    """Both output requests reach synthesis with the same full evidence matrix.

    Prompt retuning must neither pre-trim evidence for a concise request nor
    rewrite an explicit full-matrix request. The fake does not grade the answer.
    """
    notebook = repo.create_notebook(NotebookCreate(name="Mixed configurations"))
    client = _RecordingAnswer("Values are supplied in the evaluation tables. [k1]")
    baseline = {}
    repo._runtime.ask_component._answer_reasoning(
        notebook.id, question, [], [], answer_client=client,
        chunks=[RetrievedChunk(
            "comparison-chunk", "comparison-source", "Mixed configurations", "Tables",
            _BENCHMARK_EVIDENCE,
        )],
        baseline_sink=baseline, chunk_context_chars=1200, kg_context_chars=500,
    )
    assert len(client.calls) == 1
    prompt = client.calls[0][0][0]["content"]
    assert f"Question: {question}\n" in prompt
    assert _BENCHMARK_EVIDENCE in baseline["context_block"]
    assert baseline["context_block"] in prompt
    assert fragment_text("answer.numeric_attribution") in prompt
    assert baseline["budget_chars"] == 1700
    assert client.calls[0][2]["max_tokens"] == 913


@pytest.mark.parametrize("complete", [True, False])
def test_title_list_synthesis_keeps_duplicate_source_rows_and_coverage(repo, complete):
    notebook = repo.create_notebook(NotebookCreate(name="Title fixture"))
    items = [
        SourceItem(
            source_id=source_id, source_title="Same paper", doc_type_label="学术论文",
            summary="Supplementary material is available.",
            notebook_id=notebook.id, tier="personal",
        )
        for source_id in ("source-a", "source-b")
    ]
    outcome = CollectionEnumerationOutcome(
        collection="sources", kind="", source_id="", items=items,
        coverage=EnumerationCoverage(
            returned=2, returned_total=2, scanned=2, total=2 if complete else 3,
            has_more=not complete, complete=complete,
            truncated_reason="" if complete else "budget", overflow_semantics="",
        ),
    )
    preview = enumeration_prompt_block([outcome], inline_rows=10, budget_chars=2000)
    client = _RecordingAnswer("1. Same paper [k5001]\n2. Same paper [k5002]")
    baseline = {}
    result = repo._runtime.ask_component._answer_reasoning(
        notebook.id, "仅按标题逐一列出文章，保留同文的独立来源记录。", [], [],
        answer_client=client, structured_block=preview.text,
        structured_map=preview.evidence_by_id, chunk_context_chars=2000,
        kg_context_chars=500, baseline_sink=baseline,
    )
    assert len(client.calls) == 1
    prompt = client.calls[0][0][0]["content"]
    assert preview.text in prompt
    assert fragment_text("answer.style_language") in prompt
    assert baseline["context_block"].count("[enumerated-source] Same paper") == 2
    expected_coverage = "listed 2/2, complete" if complete else "listed 2/3, partial"
    assert expected_coverage in baseline["context_block"]
    assert [(anchor.key, anchor.object_type, anchor.object_id) for anchor in result[2]] == [
        ("k5001", "source", "source-a"), ("k5002", "source", "source-b"),
    ]
    assert result[3]["included_collections"] == 2


@pytest.mark.parametrize(("question", "scope"), [
    ("概述库中文献的主要观点", "ranked"),
    ("简要说明当前库中文章的方法", "ranked"),
    ("逐一列出库中全部文章标题", "complete"),
    ("逐篇分析库中文献的方法和区别", "hybrid"),
    ("不用逐篇分析库中文献，概述主要观点", "ranked"),
    ("无需逐篇列出，只选最相关的文章", "ranked"),
    ("不要逐篇分析，只概述主要观点", "ranked"),
    ("不需逐篇介绍，挑两篇代表作即可", "ranked"),
    ("不用逐篇分析，逐一列出标题", "complete"),
    ("逐项比较全部指标，输出包含每个版本和配置的完整数值矩阵。", "hybrid"),
])
def test_intent_caller_preserves_overview_vs_explicit_inventory(question, scope):
    class Client:
        configured = True

        def __init__(self):
            self.calls = []

        def chat_json(self, messages, schema_hint, **kwargs):
            self.calls.append((messages, schema_hint))
            return json.dumps({
                "normalized_question": question, "intent_type": "explain",
                "result_scope": scope, "completeness_required": scope != "ranked",
                "mandatory_topics": [{"id": "main", "title": "请求内容", "question": question}],
                "constraints": [], "needs_clarification": False, "ambiguities": [],
            })

    client = Client()
    contract = plan_query_intent(client, question)
    assert len(client.calls) == 1
    prompt = client.calls[0][0][0]["content"]
    assert "The open library resolves WHICH library" in prompt
    assert "without an all-documents constraint" in prompt
    assert contract["objective"] == question
    assert contract["resolved_question"] == question
    assert contract["result_scope"] == scope
    assert contract["completeness_required"] is (scope != "ranked")
    assert contract["constraints"] == []
    assert contract["mandatory_topics"][0]["question"] == question
    assert auto_ask_mode_from_intent(QueryIntentContract(**contract)) == (
        "reasoning" if scope != "ranked" else "chunk"
    )
