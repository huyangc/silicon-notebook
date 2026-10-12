"""How a failed Deep Report section reads in the report body.

A section that produced no body is still written into ``content_md`` as a
short note, so the gap is visible instead of the section silently vanishing.
That body is what the anonymous share page and the export serve, so the note
is always one of the fixed neutral sentences below.  The raw exception text
behind a failure never enters it: it stays in the section's own
``sections[].error``, which only the report's author can read (the report
detail route answers its creator alone), and the stage that failed is recorded
by the content-free ``report_stage_timing`` event (``stage`` + ``failed``).

``neutralize_failure_notes`` applies the same rule to bodies stored before
this boundary existed, at read time and only on the anonymous projection: it
matches nothing but the exact paragraph shape ``_assemble`` wrote, so a report
with no failed section is returned byte for byte.
"""
from __future__ import annotations

import re
from types import MappingProxyType


#: Per failure class, what the section note says. Only the empty class keeps
#: its historical budget hint; the others state what happened, and the reason
#: code goes to an event, not the copy. ``internal`` covers every failure that
#: is not a model reply -- retrieval or drafting raising -- whose raw text is
#: kept for the author in ``sections[].error`` only.
SECTION_FAILURE_COPY = MappingProxyType({
    "empty": "答案合成未产出内容(模型可能把输出预算耗在思维链上),已重试",
    "malformed": "模型返回的本节内容无法解析,已重试",
    "error": "本节模型调用失败,已重试",
    "internal": "本节处理过程出错",
})
_NEUTRAL_NOTES = frozenset(SECTION_FAILURE_COPY.values())

_NOTE_PREFIX = "（本节生成失败:"
_NOTE_SUFFIX = "）"
# ``_assemble`` writes a failed section as the three lines
# ``## <title>`` / blank / ``（本节生成失败:<error>）`` followed by a blank line
# (or the end of the body), with ``<error>`` cut to 300 characters by the
# retrieval/draft handlers. Only that exact shape is rewritten; the lazy,
# bounded inner match stops at the first closing bracket that ends the
# paragraph, so it can never run into the next section.
_STORED_NOTE_RE = re.compile(
    r"(?m)^(## [^\n]*\n\n)（本节生成失败:([\s\S]{0,300}?)）(?=\n\n|\n\Z|\Z)"
)


def section_failure_note(error: object) -> str:
    """The neutral sentence a failed section shows; never the raw error."""
    text = error if isinstance(error, str) else ""
    return text if text in _NEUTRAL_NOTES else SECTION_FAILURE_COPY["internal"]


def section_failure_paragraph(error: object) -> str:
    """The body paragraph ``_assemble`` writes for one failed section."""
    return f"{_NOTE_PREFIX}{section_failure_note(error)}{_NOTE_SUFFIX}"


def neutralize_failure_notes(content_md: str) -> str:
    """Rewrite stored failure notes that still carry raw error text."""
    if _NOTE_PREFIX not in content_md:
        return content_md
    return _STORED_NOTE_RE.sub(
        lambda match: match.group(1) + section_failure_paragraph(match.group(2)),
        content_md,
    )


__all__ = [
    "SECTION_FAILURE_COPY",
    "neutralize_failure_notes",
    "section_failure_note",
    "section_failure_paragraph",
]
