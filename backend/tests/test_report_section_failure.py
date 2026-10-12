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
    failed_section_errors,
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
    # ``[""]``: a failed section whose stored error is unusable, the only case
    # the paragraph pattern runs for.
    out = neutralize_failure_notes(body, [""])
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
    assert neutralize_failure_notes(body, [""]) == (
        _section("A", _INTERNAL) + "\n## B\n\n正文\n"
    )


def test_the_match_never_runs_into_the_next_section():
    body = (
        _section("A", "short error")
        + "\n## B（附录）\n\n正文 B\n"
    )
    assert neutralize_failure_notes(body, [""]) == (
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
        assert neutralize_failure_notes(body, [""]) == body


def test_only_a_failed_section_without_a_usable_error_falls_back_to_the_pattern():
    """``None`` (not read) and ``[]`` (read, no failed section) serve the body
    as is: in a report with no failed section, ``（本节生成失败:…）`` is the
    author's own text -- a quoted example -- not a note the generator wrote."""
    example = "# 深度报告:q\n\n## 示例\n\n（本节生成失败:这是一条示例）\n\n## B\n\n正文\n"
    assert neutralize_failure_notes(example, None) is example
    assert neutralize_failure_notes(example, []) is example
    # A failed section with its stored error: exact literal only, the example
    # elsewhere in the body stays untouched.
    raw = "OperationalError: 10.9.8.7"
    body = _section("A", raw) + "\n" + example
    assert neutralize_failure_notes(body, [raw]) == (
        _section("A", _INTERNAL) + "\n" + example
    )


def test_a_raw_error_that_starts_with_a_neutral_sentence_is_still_rewritten():
    """codex #849 R1: a stored error ``本节处理过程出错）`` + one line break +
    a diagnostic tail is not the generator's paragraph boundary."""
    raw = f"{_INTERNAL}）\nsecret gateway 10.9.8.7"
    body = _section("A", raw) + "\n## B\n\n正文\n"
    out = neutralize_failure_notes(body, [raw])
    assert "10.9.8.7" not in out
    assert out == _section("A", _INTERNAL) + "\n## B\n\n正文\n"


def test_stored_errors_are_replaced_exactly_longest_first():
    """With the failed sections' stored errors in hand, each note is replaced
    as the literal the generator wrote -- also when the error closes a bracket
    before a blank line, and when one error is a prefix of another."""
    short, long = "boom", "boom）\n\nsecret tail"
    body = _section("A", long) + "\n" + _section("B", short)
    assert neutralize_failure_notes(body, [short, long]) == (
        _section("A", _INTERNAL) + "\n" + _section("B", _INTERNAL)
    )
    # Without them, the paragraph pattern stops at the first bracket that
    # ends a paragraph: this is the residue the exact pass exists for.
    assert "secret tail" in neutralize_failure_notes(body, [""])


def test_failed_section_rows_normalise_both_dialects():
    rows = [{"index": 2, "title": "A", "error": "x"}, {"index": "bad"}, "junk",
            {"index": 0, "title": None, "error": 5}]
    expected = [{"index": 2, "title": "A", "error": "x"},
                {"index": 0, "title": "", "error": ""}]
    assert failed_section_errors(rows) == expected
    import json
    assert failed_section_errors(json.dumps(rows)) == expected
    assert failed_section_errors(None) == []
    assert failed_section_errors("not json") == []


def test_both_store_gates_look_for_the_note_this_module_writes():
    """The token read decides whether a body still carries a raw failure note
    (and the projection then trusts a ``None``). Both stores gate on the one
    predicate, and it reads a body exactly as the generator writes it: every
    neutral sentence -- what every report since K5 carries -- passes for
    free; a raw note, alone or mixed with neutral ones, is read."""
    from app.domain import report_failure_note
    from app.repositories.postgres import report_store as postgres_store
    from app.repositories.sqlite import report_store as sqlite_store

    for store in (postgres_store, sqlite_store):
        assert store.carries_raw_failure_note is (
            report_failure_note.carries_raw_failure_note
        )
    gate = report_failure_note.carries_raw_failure_note

    neutral_body = "# 报告\n\n" + "\n".join(
        _section(f"S{i}", copy) for i, copy in enumerate(SECTION_FAILURE_COPY.values())
    ) + "\n## Z\n\n正文\n"
    assert not gate("# 报告\n\n## A\n\n正文\n")
    assert not gate(neutral_body)
    # The generator's own paragraph, also as the very end of a body.
    assert not gate(f"## A\n\n{section_failure_paragraph('x')}")
    assert gate(neutral_body + _section("R", "OperationalError: 10.9.8.7"))
    assert gate(_section("R", "boom") + "\n" + neutral_body)
    # A raw error that merely starts with a neutral sentence is still raw --
    # also when only a single line break follows it (codex #849 R1).
    assert gate(_section("R", f"{_INTERNAL}）secret"))
    assert gate(_section("R", f"{_INTERNAL}）\nsecret gateway 10.9.8.7"))
    assert gate(f"## R\n\n（本节生成失败:{_INTERNAL}）\nsecret")
    assert gate(_section("R", ""))
    # Accepted residue, documented at ``_NEUTRAL_NOTES``: a stored error that
    # itself is a neutral sentence + "）" + a blank line is indistinguishable
    # from the generator's paragraph without reading the sections.
    assert not gate(_section("R", f"{_INTERNAL}）\n\nsecret"))
    # The projection takes the same view: neutral bodies pass untouched.
    assert neutralize_failure_notes(neutral_body, ["x"]) is neutral_body
