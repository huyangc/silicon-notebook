"""The failure note a Deep Report body carries for a section without a body.

``ReportEngine._assemble`` writes a failed section as the paragraph
``（本节生成失败:<note>）``, where ``<note>`` is one of the fixed neutral
sentences below (``app.services.reports.section_failure`` builds it).  Bodies
stored before that rule may still carry a section's raw error in the same
paragraph; ``carries_raw_failure_note`` tells the two apart so a read that
only exists to rewrite raw notes -- the public share page's -- costs nothing
for every other body.  Kept here, below the repositories, because both store
adapters gate their public read on it.
"""
from __future__ import annotations

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
    "context": "本节材料超过当前模型可处理范围，请调整模型容量或缩小研究范围后重新生成",
})
NOTE_PREFIX = "（本节生成失败:"
NOTE_SUFFIX = "）"
# A neutral note as the generator writes it: ``_assemble`` puts the note on
# its own paragraph (``[heading, "", note, ""]`` joined with "\n"), so the
# sentence and the closing bracket are followed by a blank line ("\n\n") when
# anything comes after the section, or by one "\n" -- or nothing -- at the
# very end of the body. A single "\n" followed by more text is NOT that
# boundary: a raw error such as ``本节处理过程出错）\nsecret 10.9.8.7`` only
# looks like a neutral note up to that line break.
#
# What this cannot tell apart without reading ``sections_json``: a stored raw
# error that itself begins with one of the four sentences below, then "）",
# then a blank line (or that ends the body right there). That needs an
# exception message whose text starts with this product's own Chinese failure
# copy and closing bracket -- which no library or server error produces; the
# only writers of these sentences are the generator and this module. It is
# accepted rather than paid for with a sections read on every report that has
# a failed section.
_NEUTRAL_NOTES = tuple(f"{copy}{NOTE_SUFFIX}" for copy in SECTION_FAILURE_COPY.values())


def _neutral_note_at(text: str, at: int, note: str) -> bool:
    if not text.startswith(note, at):
        return False
    end = at + len(note)
    rest = len(text) - end
    return (
        rest == 0
        or text.startswith("\n\n", end)
        or (rest == 1 and text[end] == "\n")
    )


def carries_raw_failure_note(content_md: str) -> bool:
    """True when some failure note in the body is not a neutral sentence.

    Walks the note prefixes with ``str.find`` and checks what follows each:
    a neutral sentence closed by the bracket and the generator's paragraph
    boundary (a blank line, or the end of the body) is what ``_assemble``
    writes; anything else -- a stored raw error, including one that merely
    starts with a neutral sentence -- is a note to rewrite. A body with no
    prefix, or only neutral notes, is False.
    """
    start = content_md.find(NOTE_PREFIX)
    while start != -1:
        tail = start + len(NOTE_PREFIX)
        if not any(_neutral_note_at(content_md, tail, note) for note in _NEUTRAL_NOTES):
            return True
        start = content_md.find(NOTE_PREFIX, tail)
    return False


__all__ = [
    "NOTE_PREFIX",
    "NOTE_SUFFIX",
    "SECTION_FAILURE_COPY",
    "carries_raw_failure_note",
]
