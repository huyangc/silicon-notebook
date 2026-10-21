"""Pure logic of asking about a deep report (``app.services.report_followup``).

The digest cases parse bodies the real ``ReportEngine._assemble`` wrote, so a
change of the engine's headings or tail notes turns them red; the edge cases
use hand-written bodies of the same shape.
"""
from __future__ import annotations

import json

import pytest

from app.domain.report_followup import (
    REASON_CLAUSE_MEMORY_CLOSED_CHANNEL,
    REASON_CLAUSE_MEMORY_CLOSED_NARROWED,
    REASON_CLAUSES,
    REASON_ENGINE_UNSUPPORTED,
    REASON_LIBRARY_UNAVAILABLE,
    REASON_MEMORY_CLOSED,
    REASON_NO_SYNTHESIS,
    REASON_PREFIX,
    REASON_READ_FAILED,
    REASON_REPORT_UNAVAILABLE,
    REASON_SCOPE_LIBRARIES,
    REASON_SCOPE_SOURCES,
    REPORT_BLOCK_END,
    REPORT_EVIDENCE_NOT_LOADED,
    LinkedReportNotFound,
    LinkedReportNotReady,
    ReportDigest,
    ReportFollowup,
    ReportLinkVerdict,
    ReportSection,
    RestoredReportEvidence,
    SectionSelection,
)
from app.services.report_followup import (
    REPORT_BLOCK_HEADER,
    SUMMARY_TRUNCATED_SUFFIX,
    ReportAdmissionCounts,
    followup_receipt,
    load_linked_report,
    render_report_block,
    report_digest,
    report_link_verdict,
    report_meta_digest,
    report_outline_text,
    report_owner_ids,
    report_reason_text,
    report_reference_owner,
    report_trace_step,
    rewrite_report_markers,
    select_report_sections,
    skipped_receipt,
    unread_verdict,
)
from tests.model_testkit import RecordingModelProvider

NOT_LOADED = "(body not loaded this turn: over the report-context budget)"
NO_BODY = "(this section has no separate body text in the stored report)"


# ---------------------------------------------------------------------------
# A body the real report engine assembled
# ---------------------------------------------------------------------------


class _SummaryModel:
    configured = True

    def chat_json(self, messages, schema_hint, **kwargs):
        return json.dumps({"summary": "总结第一句[k1]。第二句。"})


@pytest.fixture
def engine_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    provider = RecordingModelProvider(chat_clients={"report_summary": _SummaryModel()})
    return SQLiteRepository(Settings(_env_file=None), model_provider=provider)


def _ctx(object_id, object_type, source_id, title, **extra):
    return {
        "object_id": object_id, "object_type": object_type, "source_id": source_id,
        "source_title": title, "name": title, "tier": "personal", **extra,
    }


def _engine_report(repo, sections):
    """``(row, references)``: a stored-report row around the body the real
    engine assembles for ``sections``."""
    from app.models.schemas import NotebookCreate
    from app.services.report_engine import ReportEngine

    notebook = repo.create_notebook(NotebookCreate(name="t", purpose="p", primary_domain="d"))
    engine = ReportEngine.from_repository(repo, repo.settings)
    outline = [
        {"title": section["title"], "scope": section["scope"], "sub_queries": ["q"]}
        for section in sections
    ]
    report_id = repo.create_report(notebook.id, "带隙基准的温度系数从哪里来")
    content_md, _gaps, references = engine._assemble(
        notebook.id, report_id, "带隙基准的温度系数从哪里来", outline, sections
    )
    row = {
        "question": "带隙基准的温度系数从哪里来",
        "sections": [{"title": section["title"]} for section in sections],
        "content_md": content_md,
        "references": references,
        "memory_used": ["mem-1"],
    }
    return row, references


def test_digest_splits_a_body_the_real_engine_assembled(engine_repo):
    sections = [
        {
            "title": "工作原理", "scope": "a", "grounded": True,
            "markdown": (
                "## 工作原理\n正向压降随温度下降 [k1]。\n### 小结\n"
                "热电压随温度上升 [k2]\n## 模型自带的小标题\n两者相加抵消 [k1, k2]。"
            ),
            "id_map": {
                "k1": _ctx("c-1", "chunk", "s-1", "Razavi"),
                "k2": _ctx("e-1", "element", "s-2", "Gray", element_id="e-1"),
            },
        },
        {
            "title": "失败的一节", "scope": "b", "markdown": "", "grounded": False,
            "failed": True, "error": "boom", "id_map": {},
        },
        {
            "title": "工程取舍", "scope": "c", "grounded": True,
            "markdown": "## 工程取舍\n修调电阻网络 [k1]。",
            "id_map": {"k1": _ctx("ko-1", "concept", "", "修调")},
        },
    ]
    row, references = _engine_report(engine_repo, sections)
    content_md = row["content_md"]
    # The shape this parser relies on, as the engine writes it today.
    for marker in ("## 执行摘要", "## 资料基础", "## 参考文献", "> 引证分布："):
        assert marker in content_md

    digest = report_digest(row)

    assert digest.title == "带隙基准的温度系数从哪里来"
    # The engine stores the summary unnumbered (only section bodies are
    # renumbered to the references' keys): its marker names nothing.
    assert "## 执行摘要\n\n总结第一句[k1]。第二句。" in content_md
    assert digest.summary == "总结第一句。第二句。"
    assert digest.summary_keys == ()
    assert digest.section_titles == ("工作原理", "失败的一节", "工程取舍")
    first, failed, last = digest.sections
    assert [s.index for s in digest.sections] == [1, 2, 3]
    # The model's own "##" heading stays in the section it appears in.
    assert first.body == (
        "正向压降随温度下降 [k1]。\n### 小结\n热电压随温度上升 [k2]\n"
        "## 模型自带的小标题\n两者相加抵消 [k1, k2]。"
    )
    assert first.keys == ("k1", "k2")
    # A failed section keeps the neutral failure note stored in the body.
    assert failed.body.startswith("（本节生成失败") and failed.keys == ()
    assert last.body == "修调电阻网络 [k3]。" and last.keys == ("k3",)
    # Neither the source-basis block nor the references and tail notes leak in.
    everything = "\n".join([digest.summary, *(s.body for s in digest.sections)])
    for leaked in ("资料基础", "参考文献", "引证分布", "范围与证据局限", "深度报告"):
        assert leaked not in everything
    assert [dict(r) for r in digest.references] == references
    assert digest.memory_used == ("mem-1",)

    # The meta digest -- all the verdict, the pre-send check and the
    # understanding step read -- is the same report without its body, and it
    # never looks at content_md.
    class _BodyUnread(dict):
        def get(self, key, default=None):
            assert key != "content_md", "the meta digest must not read the body"
            return super().get(key, default)

        def __getitem__(self, key):
            assert key != "content_md", "the meta digest must not read the body"
            return super().__getitem__(key)

    meta = report_meta_digest(_BodyUnread(row))
    assert (meta.title, meta.section_titles, meta.references, meta.memory_used) == (
        digest.title, digest.section_titles, digest.references, digest.memory_used,
    )
    assert meta.summary == ""
    assert all(section.body == "" and section.keys == () for section in meta.sections)

    # Rendered for a turn that restored every reference: the summary carries no
    # marker at all -- neither the stored one nor a rewrite into a turn key.
    selection = select_report_sections(digest, "温度", 12000)
    assert selection.keys == ("k1", "k2", "k3")
    block = render_report_block(digest, selection, {"k1": "k4001", "k2": "k4002", "k3": "k4003"})
    summary_line = block.split("\n")[3]
    assert summary_line == "总结第一句。第二句。"
    assert "正向压降随温度下降 [k4001]。" in block


def test_digest_without_references_stops_at_the_limitations_note(engine_repo):
    """No references -> no references heading: the limitations note follows the
    last section directly and must not become part of its body."""
    sections = [
        {"title": "背景", "scope": "a", "grounded": True,
         "markdown": "## 背景\n只用了通识。", "id_map": {}},
        {"title": "推断", "scope": "b", "grounded": False,
         "markdown": "## 推断\n【通识】这一节证据有限。", "id_map": {}},
    ]
    row, references = _engine_report(engine_repo, sections)
    assert references == []
    assert "## 参考文献" not in row["content_md"]
    assert "> **范围与证据局限**" in row["content_md"]

    digest = report_digest(row)

    assert digest.sections[-1].body == "【通识】这一节证据有限。"
    assert digest.sections[0].body == "只用了通识。"


def _body(*blocks: str) -> str:
    return "\n\n".join(["# 深度报告:q", "## 执行摘要\n\n摘要。", "## 资料基础\n\n统计。", *blocks])


def _row(content_md: str, titles, **extra):
    return {"question": "q", "sections": [{"title": t} for t in titles],
            "content_md": content_md, **extra}


def test_a_section_whose_heading_is_missing_keeps_an_empty_body():
    digest = report_digest(_row(
        _body("## A\n甲 [k1]", "## C\n丙 [k2]", "## 参考文献\n\n- [k1] x\n\n> 引证分布：2 处"),
        ["A", "B", "C"],
    ))
    assert [(s.title, s.body) for s in digest.sections] == [
        ("A", "甲 [k1]"), ("B", ""), ("C", "丙 [k2]"),
    ]


def test_heading_spellings_models_vary_on_still_align():
    digest = report_digest(_row(
        _body("## 1. 背景介绍\n一", "## **二、方法：**\n二", "##   第三节 结论 \n三"),
        ["背景介绍", "二、方法", "结论"],
    ))
    assert [s.body for s in digest.sections] == ["一", "二", "三"]


def test_a_heading_inside_the_summary_block_stays_in_the_summary():
    """The summary's own ``##`` (matching no section) neither opens section 1
    early nor lets the source-basis block that follows leak into it."""
    digest = report_digest(_row(
        "# 深度报告:q\n\n## 执行摘要\n\n摘要一 [k9]。\n## 模型写的小标题\n摘要二。\n\n"
        "## 资料基础\n\n统计。\n\n## A\n甲 [k1]\n\n## B\n乙",
        ["A", "B"],
    ))
    assert digest.summary == "摘要一 。\n## 模型写的小标题\n摘要二。"
    assert [s.body for s in digest.sections] == ["甲 [k1]", "乙"]


def test_a_later_section_title_written_inside_a_section_does_not_open_it_early():
    """Section 1 closes with its own ``## 结论`` while section 3 is also called
    结论: jumping to section 3 there would skip 方法, which is still a heading
    further down, so the heading is section 1's text."""
    digest = report_digest(_row(
        _body("## 背景\n正文\n## 结论\n小结论", "## 方法\n方法正文", "## 结论\n真正的结论"),
        ["背景", "方法", "结论"],
    ))
    assert [s.body for s in digest.sections] == [
        "正文\n## 结论\n小结论", "方法正文", "真正的结论",
    ]


def test_an_unmatched_first_heading_opens_the_first_section_not_the_basis_block():
    """Everything after the source-basis block is section text: a first heading
    that matches no title still belongs to the report body, never to the
    dropped block."""
    digest = report_digest(_row(_body("## 完全不同的写法\n正文一", "## B\n正文二"), ["A", "B"]))
    assert [s.body for s in digest.sections] == ["正文一", "正文二"]


def test_a_references_heading_inside_a_section_is_section_text():
    digest = report_digest(_row(
        _body(
            "## A\n正文 [k1]\n## 参考文献\n模型自己列的书目",
            "## B\n后一节 [k2]",
            "## 参考文献\n\n- [k1] x\n- [k2] y",
            "> 引证分布：2 处",
        ),
        ["A", "B"],
    ))
    assert digest.sections[0].body == "正文 [k1]\n## 参考文献\n模型自己列的书目"
    assert digest.sections[1].body == "后一节 [k2]"


def test_headings_inside_a_code_fence_are_body_text():
    digest = report_digest(_row(
        _body("## A\n```\n## B\n> 引证分布：不是尾注\n```\n后文", "## B\n真正的 B"),
        ["A", "B"],
    ))
    assert digest.sections[0].body == "```\n## B\n> 引证分布：不是尾注\n```\n后文"
    assert digest.sections[1].body == "真正的 B"


@pytest.mark.parametrize(
    ("row", "titles", "references"),
    [
        ({}, (), 0),
        ({"content_md": None, "sections": None, "references": None, "memory_used": None},
         (), 0),
        ({"content_md": 7, "sections": "x", "references": ["bad", 3, {"key": "k1"}],
          "memory_used": "m", "question": None}, (), 1),
        ({"content_md": "", "sections": [None, {"title": None}, {"title": "A"}],
          "memory_used": ["m-1", 3, ""]}, ("", "", "A"), 0),
    ],
)
def test_a_malformed_or_empty_row_digests_without_raising(row, titles, references):
    digest = report_digest(row)
    assert digest.title == "" and digest.summary == ""
    assert digest.section_titles == titles
    assert all(section.body == "" for section in digest.sections)
    assert len(digest.references) == references
    assert digest.memory_used == (("m-1",) if row.get("memory_used") == ["m-1", 3, ""] else ())


def test_digest_references_are_read_only_copies():
    stored = [{"key": "k1", "object_type": "chunk"}]
    digest = report_digest(_row("", [], references=stored))
    stored[0]["key"] = "changed"
    assert digest.references[0]["key"] == "k1"
    with pytest.raises(TypeError):
        digest.references[0]["key"] = "x"


# ---------------------------------------------------------------------------
# The report gate
# ---------------------------------------------------------------------------


def _reader(row=None):
    def read(notebook_id, report_id):
        if row is None:
            raise KeyError(report_id)
        return row
    return read


def test_gate_returns_the_callers_own_finished_report():
    row = {"id": "r1", "notebook_id": "nb", "created_by": "u1", "status": "done"}
    assert load_linked_report(_reader(row), "nb", "r1", "u1") is row


@pytest.mark.parametrize(
    "row",
    [
        None,  # does not exist (or another notebook's: get_report filters by notebook)
        {"notebook_id": "nb", "created_by": "u2", "status": "done"},
        {"notebook_id": "other", "created_by": "u1", "status": "done"},
        {"notebook_id": "nb", "created_by": "", "status": "done"},
    ],
    ids=["missing", "another-member", "another-notebook", "no-creator"],
)
def test_gate_answers_not_found_for_every_report_that_is_not_yours(row):
    with pytest.raises(LinkedReportNotFound):
        load_linked_report(_reader(row), "nb", "r1", "u1")


@pytest.mark.parametrize("status", ["pending", "outline_ready", "generating", "failed", "cancelled"])
def test_gate_answers_not_ready_for_your_unfinished_report(status):
    row = {"notebook_id": "nb", "created_by": "u1", "status": status}
    with pytest.raises(LinkedReportNotReady):
        load_linked_report(_reader(row), "nb", "r1", "u1")


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class _Scope:
    """A stand-in for ``ActiveSourceScope``: the three faces the verdict reads."""

    def __init__(self, *, unticked=(), excluded=(), withheld=()):
        self._unticked = set(unticked)
        self._excluded = set(excluded)
        self.withheld_hidden_source_ids = frozenset(withheld)

    def covers_notebook(self, notebook_id):
        return notebook_id not in self._unticked

    def allows(self, notebook_id, source_id):
        return self.covers_notebook(notebook_id) and (notebook_id, source_id) not in self._excluded


def _ref(key, object_type, object_id, source_id=""):
    return {"key": key, "object_type": object_type, "object_id": object_id, "source_id": source_id}


def _digest(references=(), memory_used=()):
    return ReportDigest(
        title="t", summary="", sections=(), references=tuple(references),
        memory_used=tuple(memory_used),
    )


_OWNERS = {"s-own": "nb", "s-lib": "lib-a", "s-lib2": "lib-b", "s-gone-lib": "lib-off",
           "s-mem": "nb"}


def _verdict(digest, **overrides):
    arguments = dict(
        notebook_id="nb",
        scope=_Scope(),
        participants={"nb", "lib-a", "lib-b"},
        skipped_libraries={},
        source_owners=_OWNERS,
        object_owners={"ko-1": "nb", "ko-lib": "lib-a"},
        memory_open=True,
        memory_channel_open=True,
        mode_supported=True,
        library_names={"lib-a": "模拟电路库", "lib-b": "工艺库"},
    )
    arguments.update(overrides)
    return report_link_verdict(digest, **arguments)


_EVERYTHING = [
    _ref("k1", "chunk", "c-1", "s-own"),
    _ref("k2", "element", "e-1", "s-lib"),
    _ref("k3", "concept", "ko-1"),
    _ref("k4", "relation", "r-1", "s-lib2"),
]


def test_a_report_inside_the_scope_is_readable():
    verdict = _verdict(_digest(_EVERYTHING))
    assert verdict.readable and verdict.reason_code == "" and verdict.reason == ""
    assert verdict.gone == 0 and not verdict.uses_memory


def test_deleted_items_only_count_as_gone():
    """A deleted item counts as gone and is no conflict. A whole-source or
    external reference names no item the turn could read back, so it is
    never gone -- the same rule the restore applies
    (``RESTORE_SKIPPED_OBJECT_TYPES``)."""
    digest = _digest([
        *_EVERYTHING,
        _ref("k5", "chunk", "c-x", "s-deleted"),
        _ref("k6", "concept", "ko-deleted"),
        _ref("k7", "external", "plugin-item-1"),
        _ref("k8", "chunk", "", ""),
        _ref("k9", "source", "s-deleted", "s-deleted"),
    ])
    verdict = _verdict(digest)
    assert verdict.readable
    assert verdict.gone == 3  # k5, k6 and k8


def test_a_live_whole_source_reference_is_still_judged_by_the_scope():
    verdict = _verdict(
        _digest([_ref("k1", "source", "s-own", "s-own")]),
        scope=_Scope(excluded={("nb", "s-own")}),
    )
    assert verdict.reason_code == REASON_SCOPE_SOURCES and verdict.gone == 0


def test_owner_ids_are_batched_once_each_and_skip_memory_and_external():
    digest = _digest([
        _ref("k1", "chunk", "c-1", "s-own"),
        _ref("k2", "element", "e-1", "s-own"),
        _ref("k3", "concept", "ko-1"),
        _ref("k4", "memory", "m-1", "s-mem"),
        _ref("k5", "external", "item"),
        _ref("k6", "concept", "ko-1"),
    ])
    assert report_owner_ids(digest) == (("s-own",), ("ko-1",))


def test_reference_owner_prefers_the_source_and_falls_back_to_the_object():
    owners = dict(source_owners={"s": "lib"}, object_owners={"o": "nb"})
    assert report_reference_owner(_ref("k", "chunk", "o", "s"), **owners) == "lib"
    assert report_reference_owner(_ref("k", "concept", "o"), **owners) == "nb"
    assert report_reference_owner(_ref("k", "chunk", "o", "s-gone"), **owners) is None
    assert report_reference_owner(_ref("k", "external", "o"), **owners) is None


@pytest.mark.parametrize(
    ("overrides", "references"),
    [
        ({"participants": {"nb", "lib-b"}}, [_ref("k1", "chunk", "c", "s-lib")]),
        ({"skipped_libraries": {"lib-a": "timeout"}}, [_ref("k1", "chunk", "c", "s-lib")]),
        ({}, [_ref("k1", "chunk", "c", "s-gone-lib"), _ref("k2", "concept", "ko-lib")]),
    ],
    ids=["unmounted", "frozen-empty", "not-a-participant"],
)
def test_library_unavailable(overrides, references):
    verdict = _verdict(_digest(references), **overrides)
    assert verdict.reason_code == REASON_LIBRARY_UNAVAILABLE and not verdict.readable
    count = len(verdict.unavailable_libs)
    assert verdict.reason == REASON_PREFIX + f"它引用的 {count} 个参考库本轮无法使用"


def test_scope_libraries_names_the_unticked_libraries_in_citation_order():
    digest = _digest([
        _ref("k1", "chunk", "c", "s-lib2"),
        _ref("k2", "chunk", "c", "s-lib"),
        _ref("k3", "chunk", "c", "s-lib2"),
    ])
    verdict = _verdict(digest, scope=_Scope(unticked={"lib-a", "lib-b"}))
    assert verdict.reason_code == REASON_SCOPE_LIBRARIES
    assert verdict.unchecked_libs == frozenset({"lib-a", "lib-b"})
    assert verdict.reason == REASON_PREFIX + "它引用的参考库《工艺库》《模拟电路库》没有勾选"


def test_scope_sources_counts_distinct_out_of_scope_sources():
    digest = _digest([
        _ref("k1", "chunk", "c-1", "s-own"),
        _ref("k2", "element", "e-1", "s-own"),
        _ref("k3", "chunk", "c-2", "s-lib"),
    ])
    verdict = _verdict(digest, scope=_Scope(excluded={("nb", "s-own"), ("lib-a", "s-lib")}))
    assert verdict.reason_code == REASON_SCOPE_SOURCES
    assert verdict.out_of_scope_count == 2
    assert verdict.reason == REASON_PREFIX + "它引用的 2 份资料不在当前检索范围内"


@pytest.mark.parametrize(
    ("channel_open", "clause"),
    [(True, REASON_CLAUSE_MEMORY_CLOSED_NARROWED), (False, REASON_CLAUSE_MEMORY_CLOSED_CHANNEL)],
)
@pytest.mark.parametrize(
    "digest",
    [
        _digest([_ref("k1", "memory", "m-1")]),
        _digest(_EVERYTHING, memory_used=["m-1"]),
        _digest([_ref("k1", "chunk", "c", "s-mem")]),
    ],
    ids=["memory-reference", "recorded-memory-use", "withheld-memory-projection"],
)
def test_memory_closed_has_two_wordings(digest, channel_open, clause):
    scope = _Scope(withheld={"s-mem"})
    verdict = _verdict(digest, scope=scope, memory_open=False, memory_channel_open=channel_open)
    assert verdict.reason_code == REASON_MEMORY_CLOSED and verdict.uses_memory
    assert verdict.reason == REASON_PREFIX + clause
    # The withheld projection is Memory, not an out-of-scope source or a gone item.
    assert verdict.out_of_scope_count == 0 and verdict.gone == 0


def test_memory_use_is_fine_when_the_turn_reads_memory():
    verdict = _verdict(_digest([_ref("k1", "memory", "m-1")], memory_used=["m-1"]))
    assert verdict.readable and verdict.uses_memory


def test_reasons_are_taken_in_priority_order():
    references = [
        _ref("k1", "chunk", "c", "s-gone-lib"),   # library unavailable
        _ref("k2", "chunk", "c", "s-lib"),        # library unticked
        _ref("k3", "chunk", "c", "s-own"),        # source out of scope
        _ref("k4", "memory", "m"),                # memory
    ]
    scope = _Scope(unticked={"lib-a"}, excluded={("nb", "s-own")})
    common = dict(scope=scope, memory_open=False)
    order = []
    for drop in range(4):
        verdict = _verdict(_digest(references[drop:]), **common)
        order.append(verdict.reason_code)
    assert order == [
        REASON_LIBRARY_UNAVAILABLE, REASON_SCOPE_LIBRARIES,
        REASON_SCOPE_SOURCES, REASON_MEMORY_CLOSED,
    ]
    # Every conflict is still counted, whichever reason is shown.
    verdict = _verdict(_digest(references), **common)
    assert verdict.unavailable_libs == {"lib-off"} and verdict.unchecked_libs == {"lib-a"}
    assert verdict.out_of_scope_count == 1 and verdict.uses_memory


class _Untouchable:
    """Any read of it is recorded; the verdict must not read it at all."""

    def __init__(self, reads):
        self._reads = reads

    def __getattr__(self, name):
        self._reads.append(name)
        raise AssertionError(f"read {name}")

    def __contains__(self, item):
        self._reads.append("__contains__")
        raise AssertionError("read")

    def __iter__(self):
        self._reads.append("__iter__")
        raise AssertionError("read")

    def __getitem__(self, item):
        self._reads.append("__getitem__")
        raise AssertionError("read")

    def __len__(self):
        self._reads.append("__len__")
        raise AssertionError("read")


def test_an_engine_that_cannot_read_reports_is_decided_before_any_read():
    reads: list[str] = []
    verdict = _verdict(
        _digest(_EVERYTHING, memory_used=["m"]),
        mode_supported=False,
        scope=_Untouchable(reads),
        participants=_Untouchable(reads),
        skipped_libraries=_Untouchable(reads),
        source_owners=_Untouchable(reads),
        object_owners=_Untouchable(reads),
        library_names=_Untouchable(reads),
    )
    assert reads == []
    assert verdict.reason_code == REASON_ENGINE_UNSUPPORTED
    assert verdict.reason == REASON_PREFIX + REASON_CLAUSES[REASON_ENGINE_UNSUPPORTED]
    assert verdict.gone == 0 and not verdict.uses_memory


def test_reason_text_is_worded_once():
    assert report_reason_text(REASON_SCOPE_SOURCES, n=3) == (
        "本轮不读取这份报告：它引用的 3 份资料不在当前检索范围内"
    )
    assert report_reason_text(REASON_SCOPE_LIBRARIES, names="《甲》") == (
        "本轮不读取这份报告：它引用的参考库《甲》没有勾选"
    )
    for code in (REASON_REPORT_UNAVAILABLE, REASON_READ_FAILED, REASON_NO_SYNTHESIS,
                 REASON_ENGINE_UNSUPPORTED):
        assert unread_verdict(code) == ReportLinkVerdict(
            reason_code=code, reason=REASON_PREFIX + REASON_CLAUSES[code]
        )
    with pytest.raises(ValueError):
        report_reason_text("made_up")


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("甲 [k1]。", "甲 [k11]。"),
        ("乙 [k1, k3]。", "乙 [k11, k13]。"),
        ("丙 [k1，k2、k3]。", "丙 [k11, k13]。"),
        ("丁 [k2]。", "丁 。"),
        ("戊 [k2, k4]。", "戊 。"),
        ("己【k3】与（k1）", "己[k13]与[k11]"),
        # Never renumbered by the engine (strict MARKER_RE only): removed, not rewritten.
        ("庚 [ k1 , k3 ]", "庚 "),
        ("癸 [k1 ] 与 [k3]", "癸  与 [k13]"),
        ("辛 [k1, k5]", "辛 [k11]"),
        ("无标记的文本", "无标记的文本"),
    ],
    ids=["single", "compound", "localised-separators", "unknown", "all-unknown",
         "localised-brackets", "inner-spaces", "trailing-inner-space",
         "two-keys-one-item", "none"],
)
def test_markers_point_at_this_turns_keys_or_disappear(text, expected):
    key_map = {"k1": "k11", "k3": "k13", "k5": "k11"}
    assert rewrite_report_markers(text, key_map) == expected


def test_only_renumbered_markers_count_as_section_keys():
    digest = report_digest(_row(_body("## A\n甲 [k1]，乙 [ k2 ]，丙 [k3 ]，丁【k4】"), ["A"]))
    assert digest.sections[0].keys == ("k1", "k4")
    selection = select_report_sections(digest, "", 12000)
    assert selection.keys == ("k1", "k4")
    block = render_report_block(digest, selection, {f"k{i}": f"k{i}0" for i in range(1, 5)})
    assert "甲 [k10]，乙 ，丙 ，丁[k40]" in block


# ---------------------------------------------------------------------------
# Section selection and the report block
# ---------------------------------------------------------------------------


def _section(index, title, body):
    return ReportSection(
        index=index, title=title, body=body,
        keys=tuple(dict.fromkeys(
            key.strip() for chunk in body.split("[")[1:]
            for key in chunk.split("]")[0].split(",")
        )),
    )


def _report(summary="执行摘要第一句[k1]。第二句[k2]。", sections=None):
    sections = sections if sections is not None else (
        _section(1, "背景", "背景正文" * 40 + " [k3]"),
        _section(2, "温度系数", "温度系数正文" * 40 + " [k4]"),
        _section(3, "修调", "修调正文" * 40 + " [k5]"),
    )
    # A hand-built digest may still carry summary markers; selection and
    # rendering must drop them anyway (``report_digest`` already does).
    return ReportDigest(title="带隙基准", summary=summary, sections=tuple(sections))


_WIDE = {f"k{i}": "k4999" for i in range(1, 50)}


def test_everything_fits_so_everything_is_read():
    digest = _report()
    selection = select_report_sections(digest, "无关的问题", 12000)
    assert selection.read_indices == (1, 2, 3)
    assert not selection.summary_truncated
    assert selection.summary == "执行摘要第一句。第二句。"
    assert selection.keys == ("k3", "k4", "k5")
    block = render_report_block(digest, selection, {"k1": "k7", "k4": "k8"})
    assert block.startswith(REPORT_BLOCK_HEADER + "\nTitle: 《带隙基准》\nExecutive summary:\n")
    assert "\n执行摘要第一句。第二句。\n" in block and "k7" not in block
    assert "Sections (3 in total):\n## 1. 背景\n" in block
    assert NOT_LOADED not in block


def _budget_for(digest, read_count):
    """A budget that holds the skeleton plus ``read_count`` section bodies."""
    skeleton = len(render_report_block(digest, SectionSelection(summary=digest.summary), _WIDE))
    extra = max(len(s.body) for s in digest.sections) + 20 - len(NOT_LOADED)
    return skeleton + read_count * extra


def test_over_budget_keeps_whole_sections_by_relevance_in_report_order():
    digest = _report()
    budget = _budget_for(digest, 1)
    selection = select_report_sections(digest, "温度系数从哪里来", budget)
    assert selection.read_indices == (2,)
    assert selection.keys == ("k4",)
    block = render_report_block(digest, selection, _WIDE)
    assert len(block) <= budget
    # Whole bodies or the placeholder, never half a section; report order kept.
    assert digest.sections[1].body.replace("[k4]", "[k4999]") in block
    assert block.count(NOT_LOADED) == 2
    assert block.index("## 1. 背景") < block.index("## 2. 温度系数") < block.index("## 3. 修调")


def test_a_body_that_does_not_fit_is_skipped_for_the_next_that_does():
    digest = _report(sections=(
        _section(1, "长节", "长" * 900),
        _section(2, "短节", "短" * 100),
    ))
    skeleton = len(render_report_block(digest, SectionSelection(summary=digest.summary), {}))
    budget = skeleton + 300
    selection = select_report_sections(digest, "长节", budget)
    assert selection.read_indices == (2,)


def test_ties_go_to_the_earlier_section():
    digest = _report()
    selection = select_report_sections(digest, "完全无关", _budget_for(digest, 1))
    assert selection.read_indices == (1,)


def test_a_question_naming_a_section_title_selects_it():
    digest = _report()
    selection = select_report_sections(digest, "修调", _budget_for(digest, 1))
    assert selection.read_indices == (3,)


def test_a_section_without_body_is_listed_but_never_read():
    digest = _report(sections=(_section(1, "空节", ""), _section(2, "有正文", "正文 [k3]")))
    selection = select_report_sections(digest, "", 12000)
    assert selection.read_indices == (2,)
    block = render_report_block(digest, selection, {})
    assert f"## 1. 空节\n{NO_BODY}\n## 2. 有正文\n正文 " in block
    # Even a selection that names it does not make it read.
    forced = SectionSelection(read_indices=(1, 2), summary="摘要")
    followup = ReportFollowup(
        report_id="r", title="t", digest=digest, verdict=ReportLinkVerdict(),
    )
    receipt = followup_receipt(
        followup, selection=forced, admission_counts=ReportAdmissionCounts(), read=True,
    )
    assert [(s.index, s.read) for s in receipt.sections] == [(1, False), (2, True)]


def test_an_overlong_summary_is_clipped_at_a_sentence_end_and_marked_once():
    summary = "".join(f"第{i}句话说明一个事实[k{i}]。" for i in range(1, 400))
    plain = "".join(f"第{i}句话说明一个事实。" for i in range(1, 400))
    digest = _report(summary=summary)
    budget = 2000
    selection = select_report_sections(digest, "温度系数", budget)
    assert selection.summary_truncated
    assert selection.summary.endswith("。" + SUMMARY_TRUNCATED_SUFFIX)
    assert selection.read_indices == () and selection.keys == ()
    clipped = selection.summary.removesuffix(SUMMARY_TRUNCATED_SUFFIX)
    assert plain.startswith(clipped) and "[" not in clipped
    block = render_report_block(digest, selection, _WIDE)
    assert len(block) <= budget
    assert block.count(SUMMARY_TRUNCATED_SUFFIX) == 1
    assert block.count(NOT_LOADED) == 3


def test_a_summary_without_sentence_ends_is_cut_hard_at_the_budget():
    summary = "没有标点的长摘要" * 4300   # 34,400 characters, no sentence end at all
    digest = _report(summary=summary, sections=(_section(1, "节", "正文"),))
    overhead = len(render_report_block(digest, SectionSelection(summary="x"), {})) - 1
    for budget in (2000, 12000, 60000 - 1):
        selection = select_report_sections(digest, "", budget)
        room = budget - overhead - len(SUMMARY_TRUNCATED_SUFFIX)
        if room >= len(summary):
            assert not selection.summary_truncated and selection.summary == summary
            continue
        assert selection.summary_truncated
        assert selection.summary == summary[:room] + SUMMARY_TRUNCATED_SUFFIX
        assert len(render_report_block(digest, selection, _WIDE)) <= budget


def test_unread_sections_show_a_placeholder_and_the_block_rewrites_markers():
    digest = _report()
    selection = SectionSelection(read_indices=(1,), summary=digest.summary, keys=("k1", "k3"))
    block = render_report_block(digest, selection, {"k3": "k4001"})
    lines = block.split("\n")
    assert lines[:4] == [
        REPORT_BLOCK_HEADER, "Title: 《带隙基准》", "Executive summary:", "执行摘要第一句。第二句。",
    ]
    assert lines[4] == "Sections (3 in total):"
    assert lines[5:] == [
        "## 1. 背景", "背景正文" * 40 + " [k4001]",
        "## 2. 温度系数", NOT_LOADED,
        "## 3. 修调", NOT_LOADED,
        REPORT_BLOCK_END,
    ]


def test_markers_whose_evidence_was_not_loaded_say_so_instead_of_vanishing():
    """A key whose item was read back but left out by the evidence budget is
    not a deleted claim: its marker shows ``REPORT_EVIDENCE_NOT_LOADED``,
    after any mapped keys of the same marker; a key that is neither mapped
    nor not-loaded (its item is gone) is still removed."""
    key_map = {"k1": "k11"}
    not_loaded = {"k2"}
    cases = {
        "甲 [k2]。": f"甲 {REPORT_EVIDENCE_NOT_LOADED}。",
        "乙 [k1, k2]。": f"乙 [k11]{REPORT_EVIDENCE_NOT_LOADED}。",
        "丙 [k2, k3]。": f"丙 {REPORT_EVIDENCE_NOT_LOADED}。",
        "丁 [k3]。": "丁 。",
        "戊 [ k2 ]。": "戊 。",
    }
    for text, expected in cases.items():
        assert rewrite_report_markers(text, key_map, not_loaded=not_loaded) == expected
    digest = _report(sections=(_section(1, "背景", "背景正文 [k3]，另见 [k4]。"),))
    selection = select_report_sections(digest, "", 12000)
    block = render_report_block(digest, selection, {"k3": "k2"}, not_loaded={"k4"})
    assert f"背景正文 [k2]，另见 {REPORT_EVIDENCE_NOT_LOADED}。" in block


def test_the_budget_holds_the_widest_rewrite_including_the_not_loaded_mark():
    """Selection measures each marker at its widest rewrite -- all but one key
    mapped to the widest key and the last one not loaded -- so the block
    stays within budget whatever the partition admits."""
    body = "正文 [k1, k2, k3]。" * 30
    digest = _report(sections=(_section(1, "背景", body), _section(2, "其他", body)))
    budget = len(render_report_block(digest, SectionSelection(summary=digest.summary), {})) + 1500
    selection = select_report_sections(digest, "背景", budget)
    assert selection.read_indices == (1,)
    widest = {"k1": "k99999", "k2": "k99999"}
    block = render_report_block(digest, selection, widest, not_loaded={"k3"})
    assert len(block) <= budget


def test_evidence_keys_follow_section_relevance_even_when_everything_fits():
    """Every body fits, so every section is read in report order -- but the
    evidence partition admits the cited items of the section the query is
    about first, each section's keys in first-appearance order."""
    digest = _report()
    selection = select_report_sections(digest, "修调", 12000)
    assert selection.read_indices == (1, 2, 3)
    assert selection.keys == ("k3", "k4", "k5")
    assert selection.evidence_keys[0] == "k5"
    assert sorted(selection.evidence_keys) == ["k3", "k4", "k5"]
    over = select_report_sections(digest, "温度系数从哪里来", _budget_for(digest, 1))
    assert over.evidence_keys == over.keys == ("k4",)
    assert SectionSelection(keys=("k1", "k2")).evidence_keys == ("k1", "k2")
    with pytest.raises(ValueError):
        SectionSelection(keys=("k1",), evidence_keys=("k2",))


def test_the_block_is_bounded_by_its_header_and_end_line():
    digest = _report()
    block = render_report_block(digest, select_report_sections(digest, "", 12000), {})
    assert block.startswith(REPORT_BLOCK_HEADER + "\n")
    assert block.endswith("\n" + REPORT_BLOCK_END)
    assert block.count(REPORT_BLOCK_END) == 1


def test_outline_lines():
    digest = _report()
    assert report_outline_text(digest, True) == (
        "Attached report: 《带隙基准》\nReport sections: 1. 背景 | 2. 温度系数 | 3. 修调"
    )
    unreadable = (
        "Attached report: 《带隙基准》 (it cannot be read in this turn; "
        "answer from the notebook evidence)"
    )
    assert report_outline_text(digest, False) == unreadable
    assert report_outline_text("带隙基准", False) == unreadable
    assert report_outline_text("带隙基准", True) == "Attached report: 《带隙基准》"
    # No title to show (the report is gone or could not be read): no empty brackets.
    assert report_outline_text("", False) == (
        "Attached report (it cannot be read in this turn; answer from the notebook evidence)"
    )


# ---------------------------------------------------------------------------
# Receipts and the trace step
# ---------------------------------------------------------------------------


def _followup(verdict=None, *, restored=None, digest=None, memory_used=("m-1",)):
    digest = digest or ReportDigest(
        title="带隙基准", summary="摘要", memory_used=tuple(memory_used),
        sections=tuple(_section(i, f"第{i}节", f"正文{i}") for i in range(1, 7)),
    )
    return ReportFollowup(
        report_id="rep-1", title="带隙基准", digest=digest,
        verdict=verdict or ReportLinkVerdict(gone=1), restored=restored,
    )


def test_a_read_receipt_reports_sections_counts_and_memory():
    followup = _followup(restored=RestoredReportEvidence(gone=2))
    selection = SectionSelection(read_indices=(2, 3), summary="摘要")
    receipt = followup_receipt(
        followup, selection=selection,
        admission_counts=ReportAdmissionCounts(restored=18, over_budget=4), read=True,
    )
    assert receipt.status == "read" and receipt.reason_code == ""
    assert [(s.index, s.read) for s in receipt.sections] == [
        (1, False), (2, True), (3, True), (4, False), (5, False), (6, False),
    ]
    assert receipt.evidence_restored == 18
    assert receipt.evidence_gone == 3          # verdict 1 + restore 2
    assert receipt.evidence_over_budget == 4
    assert receipt.memory_used == ["m-1"]
    assert not receipt.summary_truncated


def test_a_turn_that_did_not_read_gets_a_skipped_receipt():
    readable = _followup()
    receipt = followup_receipt(
        readable, selection=SectionSelection(),
        admission_counts=ReportAdmissionCounts(restored=3), read=False,
    )
    assert receipt.status == "skipped" and receipt.reason_code == REASON_NO_SYNTHESIS
    assert receipt.reason == REASON_PREFIX + REASON_CLAUSES[REASON_NO_SYNTHESIS]
    assert receipt.sections == [] and receipt.evidence_restored == 0 and receipt.memory_used == []

    conflict = _followup(ReportLinkVerdict(
        reason_code=REASON_SCOPE_SOURCES,
        reason=report_reason_text(REASON_SCOPE_SOURCES, n=2),
        gone=5, out_of_scope_count=2,
    ))
    receipt = followup_receipt(
        conflict, selection=SectionSelection(), admission_counts=ReportAdmissionCounts(),
        read=False,
    )
    assert receipt.reason_code == REASON_SCOPE_SOURCES
    assert receipt.reason == "本轮不读取这份报告：它引用的 2 份资料不在当前检索范围内"
    assert receipt.evidence_gone == 0


def test_a_report_the_verdict_refused_cannot_be_receipted_as_read():
    conflict = _followup(unread_verdict(REASON_READ_FAILED))
    with pytest.raises(ValueError):
        followup_receipt(
            conflict, selection=SectionSelection(),
            admission_counts=ReportAdmissionCounts(), read=True,
        )


def test_skipped_receipt_uses_the_verdicts_wording_only_for_its_own_code():
    verdict = ReportLinkVerdict(
        reason_code=REASON_LIBRARY_UNAVAILABLE,
        reason=report_reason_text(REASON_LIBRARY_UNAVAILABLE, n=2),
    )
    followup = ReportFollowup(report_id="r", title="", digest=None, verdict=verdict)
    assert skipped_receipt(followup, REASON_LIBRARY_UNAVAILABLE).reason.endswith("它引用的 2 个参考库本轮无法使用")
    receipt = skipped_receipt(followup, REASON_READ_FAILED)
    assert receipt.reason == REASON_PREFIX + "读取报告时出错，请重试"
    assert receipt.title == "" and receipt.status == "skipped"
    # Serialisation keeps only what a skipped receipt carries.
    assert set(json.loads(receipt.model_dump_json())) == {
        "report_id", "title", "status", "reason_code", "reason",
        "evidence_restored", "evidence_gone", "evidence_over_budget",
    }


def _read_turn(read=(), *, truncated=False, restored=0, gone=0, over=0, total=6, blank=()):
    """``(receipt, digest)`` of a turn that read the report; ``blank`` are the
    sections without body text."""
    digest = ReportDigest(
        title="带隙基准", summary="摘要",
        sections=tuple(
            _section(i, f"第{i}节", "" if i in blank else f"正文{i}")
            for i in range(1, total + 1)
        ),
    )
    receipt = followup_receipt(
        _followup(ReportLinkVerdict(gone=gone), digest=digest),
        selection=SectionSelection(read_indices=tuple(read), summary_truncated=truncated),
        admission_counts=ReportAdmissionCounts(restored=restored, over_budget=over),
        read=True,
    )
    return receipt, digest


@pytest.mark.parametrize(
    ("turn", "summary"),
    [
        (_read_turn(range(1, 7)), "已读取关联报告《带隙基准》全文"),
        (_read_turn(range(1, 7), restored=18, gone=2),
         "已读取关联报告《带隙基准》全文；复原引用证据 18 条，2 条已失效"),
        (_read_turn((2, 3), restored=18, gone=2),
         "已读取关联报告《带隙基准》：执行摘要与第 2、3 节（共 6 节，按与问题的相关度选取）；"
         "复原引用证据 18 条，2 条已失效"),
        (_read_turn((), truncated=True, restored=5, over=3),
         "已读取关联报告《带隙基准》：执行摘要（已截断）与各节标题（共 6 节，正文超出篇幅未读入）；"
         "复原引用证据 5 条，3 条超出篇幅未读入"),
        (_read_turn(range(1, 7), truncated=True),
         "已读取关联报告《带隙基准》：执行摘要（已截断）与全部 6 节"),
        (_read_turn((), truncated=True, total=0), "已读取关联报告《带隙基准》：执行摘要（已截断）"),
        # A section without body text is never listed as read, and never
        # described as left out by the budget.
        (_read_turn(range(1, 7), blank=(2,)),
         "已读取关联报告《带隙基准》全文（第 2 节没有单独的正文）"),
        (_read_turn((1, 3), blank=(2,)),
         "已读取关联报告《带隙基准》：执行摘要与第 1、3 节"
         "（共 6 节，按与问题的相关度选取，第 2 节没有单独的正文）"),
        (_read_turn(range(1, 7), truncated=True, blank=(2, 5)),
         "已读取关联报告《带隙基准》：执行摘要（已截断）与第 1、3、4、6 节"
         "（共 6 节，第 2、5 节没有单独的正文）"),
    ],
    ids=["full", "full-with-evidence", "partial", "headings-only", "summary-clipped",
         "no-sections", "full-with-blank", "partial-with-blank", "clipped-with-blank"],
)
def test_trace_step_says_what_was_read(turn, summary):
    receipt, digest = turn
    step = report_trace_step(receipt, digest=digest)
    assert step.step_type == "report"
    assert step.summary == summary
    assert step.detail["status"] == "read"
    assert step.detail["sections_total"] == len(receipt.sections)
    assert step.detail["sections_read"] == [s.index for s in receipt.sections if s.read]
    blank = {s.index for s in digest.sections if not s.body}
    assert not blank & set(step.detail["sections_read"])
    assert "reason_code" not in step.detail
    assert "memory_used" not in step.detail


def test_admission_counts_are_a_typed_value():
    followup = _followup()
    with pytest.raises(TypeError):
        followup_receipt(
            followup, selection=SectionSelection(),
            admission_counts={"restored": 1, "over_budget": 0}, read=True,
        )
    for bad in ({"restored": -1}, {"over_budget": 1.5}, {"restored": True}):
        with pytest.raises(ValueError):
            ReportAdmissionCounts(**bad)


def test_trace_step_says_why_nothing_was_read():
    followup = _followup(ReportLinkVerdict(
        reason_code=REASON_MEMORY_CLOSED,
        reason=report_reason_text(REASON_MEMORY_CLOSED, memory_channel_open=True),
    ))
    step = report_trace_step(skipped_receipt(followup, REASON_MEMORY_CLOSED), digest=None)
    assert step.summary == (
        "未读取关联报告《带隙基准》：它用到了你的个人记忆，而收窄检索范围后本轮不读取个人记忆"
    )
    assert step.detail["reason_code"] == REASON_MEMORY_CLOSED
    assert step.detail["sections_read"] == [] and step.detail["evidence_restored"] == 0

    gone = ReportFollowup(
        report_id="rep-x", title="", digest=None,
        verdict=unread_verdict(REASON_REPORT_UNAVAILABLE),
    )
    step = report_trace_step(skipped_receipt(gone, REASON_REPORT_UNAVAILABLE), digest=None)
    assert step.summary == "未读取关联报告：这份报告已删除或不再是已完成状态"


def test_no_internal_detail_leaks_into_the_trace():
    receipt, digest = _read_turn((1,), restored=1)
    step = report_trace_step(receipt, digest=digest)
    assert set(step.detail) == {
        "report_id", "status", "sections_total", "sections_read", "summary_truncated",
        "evidence_restored", "evidence_gone", "evidence_over_budget",
    }


def test_scope_protocol_matches_the_real_scope():
    """The verdict reads exactly three faces of ``ActiveSourceScope``."""
    from app.services.source_scope import ActiveSourceScope

    for name in ("covers_notebook", "allows"):
        assert callable(getattr(ActiveSourceScope, name))
    assert "withheld_hidden_source_ids" in ActiveSourceScope.__dataclass_fields__
