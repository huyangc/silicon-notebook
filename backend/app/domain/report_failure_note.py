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
})
NOTE_PREFIX = "（本节生成失败:"
NOTE_SUFFIX = "）"
# A neutral note as the generator writes it: the sentence and the closing
# bracket, then the paragraph's line break or the end of the body.
_NEUTRAL_NOTES = tuple(f"{copy}{NOTE_SUFFIX}" for copy in SECTION_FAILURE_COPY.values())


def _neutral_note_at(text: str, at: int, note: str) -> bool:
    end = at + len(note)
    return text.startswith(note, at) and (end == len(text) or text[end] == "\n")


def carries_raw_failure_note(content_md: str) -> bool:
    """True when some failure note in the body is not a neutral sentence.

    Walks the note prefixes with ``str.find`` and checks what follows each:
    a neutral sentence closed by the bracket and a line break (or the end of
    the body) is what the generator writes; anything else -- a stored raw
    error, including one that merely starts with a neutral sentence -- is a
    note to rewrite. A body with no prefix, or only neutral notes, is False.
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
