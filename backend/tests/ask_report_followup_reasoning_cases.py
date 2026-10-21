"""Backend-neutral scenarios for the reasoning engine reading the deep report
a question attaches (plan 2026-10-21 T8), end to end through ``repo.ask``.

The world is the restore's (``tests/report_reference_restore_cases.py``):
Alice asks in ``nb`` (``src-a`` with passage ``c-a`` and elements ``el-a1`` /
``el-a2``, ``src-b`` with ``c-b`` / ``el-b1``, her Memory ``mem-alice``), which
mounts ``lib`` (knowledge object ``kg-lib``). The reasoning retrieval stage is
replaced by a stub (``ReasoningRetriever.run_stage``) that hands back the
passages a scenario chooses and logs what the stage was given; the turn's own
Memory recall is off. Everything else -- the ceiling, the verdict, the
restore, the evidence partition, the renderers, the citation builders, the
receipt, the trace and the save -- is the production code.

``tests/test_ask_report_followup_reasoning.py`` runs these on SQLite and
``tests/postgres/test_ask_report_followup_reasoning_pg.py`` on PostgreSQL.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from unittest import mock

from app.application.ask_reasoning import ReasoningEvidenceSnapshot
from app.domain.report_followup import REASON_SCOPE_SOURCES, REPORT_BLOCK_END
from app.domain.retrieval import RetrievedChunk
from app.models.ask import AskRequest
from app.models.source_scope import SourceScope
from app.services.ask_service import AskService
from app.services.prompts import ATTACHED_REPORT_ANSWER_RULE
from app.services.reasoning_retrieval import ReasoningRetriever
from app.services.report_followup import REPORT_BLOCK_HEADER
from tests.model_testkit import bind_chat_client
from tests.report_reference_restore_cases import NOW, World, as_request_user, build_world

__all__ = ["SCENARIOS", "build_world"]

TITLE = "两种退火工艺的差异与依据"
SECTIONS = ("工艺甲", "工艺乙")
QUESTION = "两种工艺的依据是什么？"
OUTLINE = f"Attached report: 《{TITLE}》\nReport sections: 1. 工艺甲 | 2. 工艺乙"
TITLE_LINE = (
    f"Attached report: 《{TITLE}》 (it cannot be read in this turn; "
    "answer from the notebook evidence)"
)


def _ref(key: str, object_type: str, object_id: str, *, source_id: str = "",
         element_id: str = "") -> dict:
    return {
        "key": key, "object_type": object_type, "object_id": object_id,
        "source_id": source_id, "element_id": element_id,
    }


#: One reference of every kind the restore reads back (k1 passage, k2
#: element, k3 a mounted library's knowledge object, k4 a relation read back
#: as its evidence element, k5 the asker's Memory), two it skips (k6 external,
#: k7 a whole source) and one whose passage no longer exists (k8).
REFERENCES = [
    _ref("k1", "chunk", "c-a", source_id="src-a"),
    _ref("k2", "element", "el-b1", source_id="src-b", element_id="el-b1"),
    _ref("k3", "concept", "kg-lib", source_id="lib-src"),
    _ref("k4", "relation", "rel-a", source_id="src-a", element_id="el-a2"),
    _ref("k5", "memory", "mem-alice"),
    _ref("k6", "external", "https://example.org/paper"),
    _ref("k7", "source", "src-b", source_id="src-b"),
    _ref("k8", "chunk", "c-gone", source_id="src-a"),
]

SECTION_ONE = "甲的温度窗口较窄 [k1]，其依据见 [k2, k4]。外部资料 [k6]，整份来源 [k7]，已删段落 [k8]。"
SECTION_TWO = "乙沿用参考库的概念 [k3]，并结合个人记忆 [k5]。"


def content_md(summary: str = "两种工艺在温度窗口上不同。",
               bodies: tuple[str, str] = (SECTION_ONE, SECTION_TWO)) -> str:
    return "\n".join([
        f"# {TITLE}", "",
        "## 执行摘要", summary, "",
        "## 资料基础", "本报告依据笔记本中的资料写成。", "",
        f"## {SECTIONS[0]}", bodies[0], "",
        f"## {SECTIONS[1]}", bodies[1], "",
        "## 参考文献", "[k1] 甲段落",
    ])


def insert_report(world: World, report_id: str, *, content: Optional[str] = None,
                  references: Optional[list] = None) -> str:
    """Alice's finished report of ``nb``."""
    sections = json.dumps([{"title": title} for title in SECTIONS], ensure_ascii=False)
    world.execute(
        "INSERT INTO reports (id,notebook_id,question,outline_json,sections_json,"
        "references_json,content_md,status,created_by,understanding_json,"
        "created_at,updated_at) VALUES "
        "({ph},{ph},{ph},{js},{js},{js},{ph},'done',{ph},{js},{ph},{ph})",
        (
            report_id, world.nb, TITLE, sections, sections,
            json.dumps(REFERENCES if references is None else references),
            content if content is not None else content_md(),
            world.alice.id, json.dumps({}), NOW, NOW,
        ),
    )
    return report_id


class Chat:
    """A configured chat fake: records each prompt, answers ``reply``."""

    configured = True
    model = "fake"

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return json.dumps(self.reply, ensure_ascii=False)


def chunk(chunk_id: str, source_id: str, text: str, element_ids: list,
          relevance: float = 0.9) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=source_id, source_title=f"{source_id} 标题",
        section_path="节", text=text, element_ids=list(element_ids),
        relevance=relevance,
    )


C_A = ("c-a", "src-a", "甲段落原文", ["el-a1", "el-a2"])
C_B = ("c-b", "src-b", "乙段落原文", ["el-b1"])


@contextmanager
def stubbed_retrieval(*chunks: tuple, outline: tuple = ()) -> Iterator[list[dict]]:
    """The reasoning retrieval stage answers ``chunks`` (and ``outline``);
    each call logs the history it was given (what its planning reads) and
    the retriever's report outline (what its reflect rounds read). The
    turn's own Memory recall is off, so the report's Memory entry is the only
    one in the prompt."""
    calls: list[dict] = []
    selected = tuple(chunk(*value) for value in chunks)

    def run_stage(self, stage, runtime):
        calls.append({
            "question": stage.question, "history": stage.history,
            "report_outline": self.report_outline,
        })
        return ReasoningEvidenceSnapshot(
            top_hits=(), elements=(), trace=(), chunks=selected, chains=(),
            attempted=(), enumerations=(), collection_map_text="",
            outline=tuple(outline), outline_evidence=(), baseline_manifest=None,
        )

    with mock.patch.object(ReasoningRetriever, "run_stage", run_stage), \
            mock.patch.object(AskService, "_memory_hits", lambda self, *a, **k: []):
        yield calls


def bind_answer(world: World, answer: str) -> Chat:
    client = Chat({"answer": answer, "grounded": True})
    bind_chat_client(world.repo, "ask_answer", client)
    return client


def ask(world: World, report_id: Optional[str], *, question: str = QUESTION,
        output: str = "answer", **payload: Any):
    with as_request_user(world.alice):
        return world.repo.ask(
            world.nb,
            AskRequest(question=question, mode="reasoning", report_id=report_id, **payload),
            output=output,
        )


def report_block_of(prompt: str) -> str:
    """The report block, header through end line, each there once, before
    the question."""
    end_line = "\n" + REPORT_BLOCK_END + "\n"
    assert prompt.count(REPORT_BLOCK_HEADER + "\n") == 1 and prompt.count(end_line) == 1
    start = prompt.index(REPORT_BLOCK_HEADER + "\n")
    end = prompt.index(end_line) + len(end_line) - 1
    assert start < end < prompt.index("Question: ")
    return prompt[start:end]


def keys_in(text: str) -> set[str]:
    return set(re.findall(r"\bk\d+\b", text))


def steps_of(response) -> list:
    return list(response.reasoning_trace or [])


# ----------------------------------------------------------------- scenarios
def scenario_a_reasoning_turn_reads_the_report_end_to_end(world: World) -> None:
    """The final synthesis prompt carries rule 15 and the report block (its
    markers rewritten to this turn's keys, the restored items in their native
    bands after the turn's own passage); the planning history and the
    retriever's reflect seat carry the outline; the answer binds restored
    items, which get cards and ground it; the trace has a ``report`` step
    between the intent step and the Memory step; the receipt is ``read`` and
    is saved with the answer."""
    report = insert_report(world, "rep-reasoning")
    answer_llm = bind_answer(world, "甲的温度窗口较窄[k2]。关系证据见[k4002]。")
    with stubbed_retrieval(("c-b", "src-b", "乙段落原文", ["el-b1"], 0.05)) as calls:
        response = ask(world, report)

    assert len(answer_llm.prompts) == 1
    prompt = answer_llm.prompts[0]
    assert ATTACHED_REPORT_ANSWER_RULE in prompt
    block = report_block_of(prompt)
    assert f"Title: 《{TITLE}》" in block
    assert "## 1. 工艺甲\n甲的温度窗口较窄 [k2]，其依据见 [k4001, k4002]。" in block
    assert "外部资料 ，整份来源 ，已删段落 。" in block
    assert "## 2. 工艺乙\n乙沿用参考库的概念 [k1001]，并结合个人记忆 [k3001]。" in block
    assert keys_in(block) == {"k2", "k4001", "k4002", "k1001", "k3001"}
    assert "[Evidence the attached report cited]" in prompt
    assert "k1: 乙段落原文" in prompt and "k2: 甲段落原文" in prompt
    assert "参考文献" not in block and "资料基础" not in block

    # Planning and reflection know the report by its outline -- never its body.
    assert [call["history"] for call in calls] == [OUTLINE]
    assert [call["report_outline"] for call in calls] == [OUTLINE]

    assert [(a.key, a.object_type, a.object_id) for a in response.anchors] == [
        ("k2", "chunk", "c-a"), ("k4002", "element", "el-a2"),
    ]
    assert [(c.source_id, c.element_id) for c in response.citations] == [
        ("src-b", "el-b1"), ("src-a", "el-a1"), ("src-a", "el-a2"),
    ]
    assert response.grounded is True and response.evidence_level == "grounded"

    receipt = response.linked_report
    assert (receipt.report_id, receipt.title, receipt.status) == (report, TITLE, "read")
    assert [(s.index, s.read) for s in receipt.sections] == [(1, True), (2, True)]
    assert (receipt.evidence_restored, receipt.evidence_gone,
            receipt.evidence_over_budget, receipt.summary_truncated) == (5, 1, 0, False)

    steps = steps_of(response)
    kinds = [step.step_type for step in steps]
    assert kinds[:3] == ["intent", "report", "skip"], kinds
    report_step = steps[1]
    assert report_step.summary == (
        f"已读取关联报告《{TITLE}》全文；读回报告引用 5 条，1 条已失效"
    )
    assert report_step.detail["sections_read"] == [1, 2]
    assert report_step.detail["evidence_read_back"] == 5
    assert isinstance(report_step.duration_ms, int)
    synthesis = steps[-1]
    assert synthesis.step_type == "synthesis"
    assert synthesis.detail["included_report_evidence"] == 5

    store = world.repo._runtime.ask_state
    stored = store.ask_answer_detail(response.answer_id)["payload"]
    assert stored["linked_report"] == receipt.model_dump(mode="json")


def scenario_a_conflicting_reasoning_turn_contributes_the_title_alone(world: World) -> None:
    """A local scope that leaves out a cited source: the report is not read.
    No report block, rule 15 or section title reaches the answer prompt; the
    planning history and the reflect seat hold the title line alone; the
    turn answers from its own retrieval and its receipt and trace say why."""
    report = insert_report(world, "rep-reasoning-conflict")
    answer_llm = bind_answer(world, "甲[k1]。")
    with stubbed_retrieval(C_A) as calls:
        response = ask(world, report, source_scope=SourceScope(
            mode="include", source_ids=["src-a"], narrowed=True,
        ))

    prompt = answer_llm.prompts[0]
    assert REPORT_BLOCK_HEADER not in prompt and ATTACHED_REPORT_ANSWER_RULE not in prompt
    assert "Evidence the attached report cited" not in prompt
    assert not any(title in prompt for title in SECTIONS)
    assert [call["history"] for call in calls] == [TITLE_LINE]
    assert [call["report_outline"] for call in calls] == [TITLE_LINE]
    assert response.answer
    receipt = response.linked_report
    assert (receipt.status, receipt.reason_code) == ("skipped", REASON_SCOPE_SOURCES)
    assert receipt.sections == [] and receipt.evidence_restored == 0
    report_step = next(step for step in steps_of(response) if step.step_type == "report")
    assert report_step.summary.startswith(f"未读取关联报告《{TITLE}》：")
    assert report_step.detail["reason_code"] == REASON_SCOPE_SOURCES


def scenario_the_retrieval_only_reasoning_output_carries_the_partition(world: World) -> None:
    """``output="evidence"``: the context the synthesis would have received,
    with the report partition's items under their own kinds, and the ``read``
    receipt -- and no answer model call."""
    report = insert_report(world, "rep-reasoning-evidence")
    answer_llm = bind_answer(world, "unused")
    with stubbed_retrieval(C_B):
        evidence = ask(world, report, output="evidence")

    assert answer_llm.prompts == []
    kinds = {item.key: item.kind for item in evidence.items if item.key}
    assert kinds == {
        "k1": "chunk", "k2": "chunk", "k4001": "element", "k4002": "element",
        "k1001": "kg", "k3001": "memory",
    }
    assert evidence.counts.by_kind["chunk"].selected == 2
    assert evidence.counts.by_kind["kg"].delivered == 1
    assert evidence.counts.by_kind["memory"].delivered == 1
    assert evidence.context_chars <= evidence.budget_chars
    receipt = evidence.linked_report
    assert receipt is not None and receipt.status == "read"
    assert (receipt.evidence_restored, receipt.evidence_gone) == (5, 1)


SCENARIOS = (
    scenario_a_reasoning_turn_reads_the_report_end_to_end,
    scenario_a_conflicting_reasoning_turn_contributes_the_title_alone,
    scenario_the_retrieval_only_reasoning_output_carries_the_partition,
)
