"""K5: a failed report section's note in the body is neutral copy only.

``reports.section_failure`` is the single table of failure sentences that both
the generator (``ReportEngine._assemble``) and the anonymous projection read.
These cases pin the read-time normalisation of bodies stored before the rule:
it rewrites only the exact paragraph shape the generator wrote, keeps the
already-neutral sentences byte for byte, and never runs past one paragraph.
"""
from __future__ import annotations

from app.services.reports.section_failure import (
    SECTION_FAILURE_COPY,
    neutralize_failure_notes,
    section_failure_note,
    section_failure_paragraph,
)

_INTERNAL = SECTION_FAILURE_COPY["internal"]


def _section(title: str, note: str) -> str:
    return f"## {title}\n\n（本节生成失败:{note}）\n"


def test_every_neutral_sentence_is_kept_and_anything_else_is_replaced():
    for copy in SECTION_FAILURE_COPY.values():
        assert section_failure_note(copy) == copy
    for raw in ("pool timeout", "", "retrieval failed", None, 3):
        assert section_failure_note(raw) == _INTERNAL
    assert section_failure_paragraph("x") == f"（本节生成失败:{_INTERNAL}）"


def test_stored_raw_notes_are_rewritten_and_neutral_ones_left_alone():
    body = (
        "# 深度报告:q\n\n"
        + _section("A", "ConnectionError: http://10.0.0.1:8000/v1 timed out") + "\n"
        + _section("B", "本节模型调用失败,已重试") + "\n"
        + _section("C（续）", "")
    )
    out = neutralize_failure_notes(body)
    assert "10.0.0.1" not in out
    assert out == (
        "# 深度报告:q\n\n"
        + _section("A", _INTERNAL) + "\n"
        + _section("B", "本节模型调用失败,已重试") + "\n"
        + _section("C（续）", _INTERNAL)
    )


def test_a_multi_line_raw_error_is_rewritten_whole():
    # ``str(exc)`` may span lines and contain a closing bracket; the note ends
    # at the first closing bracket that also ends the paragraph.
    body = _section("A", "boom）\nline two\nline three") + "\n## B\n\n正文\n"
    assert neutralize_failure_notes(body) == (
        _section("A", _INTERNAL) + "\n## B\n\n正文\n"
    )


def test_the_match_never_runs_into_the_next_section():
    body = (
        _section("A", "short error")
        + "\n## B（附录）\n\n正文 B\n"
    )
    assert neutralize_failure_notes(body) == (
        _section("A", _INTERNAL) + "\n## B（附录）\n\n正文 B\n"
    )


def test_text_outside_the_fixed_paragraph_shape_is_untouched():
    bodies = [
        "# 深度报告:q\n\n## A\n\n正文\n",
        # Not right under a section heading.
        "## A\n\n正文\n（本节生成失败:不是生成器写的）\n",
        # Inline, not its own paragraph.
        "## A\n\n前文（本节生成失败:引用）后文\n",
        # Longer than any error the generator stored (300 characters).
        _section("A", "x" * 301),
    ]
    for body in bodies:
        assert neutralize_failure_notes(body) == body
