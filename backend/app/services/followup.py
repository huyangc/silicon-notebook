"""Follow-up question detection for multi-turn ask().

A pure heuristic gate: only when a question "looks like a follow-up" do we pay
for an LLM query rewrite (coreference resolution). Kept dependency-free and
side-effect-free so it is trivially unit-testable.
"""

from __future__ import annotations

import re

# CJK anaphora markers (refer back to something prior). Substring match is fine for CJK.
_ANAPHORA_MARKERS = (
    "这个", "那个", "这些", "那些", "这一", "那一", "这种", "这样",
    "这块", "这部分", "这章", "这节", "上面", "上述", "前面", "刚才",
    "继续", "接着", "它", "该", "此",
)
# CJK additive / continuation markers: the question EXTENDS a prior turn (e.g.
# "加上 X" = add X to the previous comparison). Elliptical — the baseline being
# extended lives in the conversation history, not in the question itself, so
# these still need coreference resolution even when the question isn't short.
_CONTINUATION_MARKERS = (
    "加上", "加入", "再加", "还有", "此外", "另外", "补充", "顺便",
    "再说", "再讲", "再聊", "还想", "接下来",
)
# English anaphora + additive markers, matched on word tokens (not substrings).
_EN_MARKERS = {
    "it", "this", "that", "these", "those", "above", "former", "latter",
    "also", "additionally", "plus", "furthermore", "besides", "moreover",
}


def looks_like_followup(question: str, max_len: int) -> bool:
    """True when `question` is likely an elliptical follow-up that needs the
    conversation history to be understood: it is short, carries an anaphor, or
    extends/continues a prior turn (additive phrasing like "加上 X")."""
    q = (question or "").strip()
    if not q:
        return False
    if len(q) < max_len:
        return True
    if any(m in q for m in _ANAPHORA_MARKERS):
        return True
    if any(m in q for m in _CONTINUATION_MARKERS):
        return True
    tokens = set(re.findall(r"[a-z]+", q.lower()))
    return bool(tokens & _EN_MARKERS)
