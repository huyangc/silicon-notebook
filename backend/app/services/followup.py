"""Follow-up question detection for multi-turn ask().

A pure heuristic gate: only when a question "looks like a follow-up" do we pay
for an LLM query rewrite (coreference resolution). Kept dependency-free and
side-effect-free so it is trivially unit-testable.
"""

from __future__ import annotations

import re

# CJK anaphora / continuation markers. Substring match is fine for CJK.
_ANAPHORA_MARKERS = (
    "这个", "那个", "这些", "那些", "这一", "那一", "这种", "这样",
    "这块", "这部分", "这章", "这节", "上面", "上述", "前面", "刚才",
    "继续", "接着", "它", "该", "此",
)
# English anaphora markers, matched on word tokens (not substrings).
_EN_MARKERS = {"it", "this", "that", "these", "those", "above", "former", "latter"}


def looks_like_followup(question: str, max_len: int) -> bool:
    """True when `question` is likely an elliptical follow-up that needs the
    conversation history to be understood (short, or carrying an anaphor)."""
    q = (question or "").strip()
    if not q:
        return False
    if len(q) < max_len:
        return True
    if any(m in q for m in _ANAPHORA_MARKERS):
        return True
    tokens = set(re.findall(r"[a-z]+", q.lower()))
    return bool(tokens & _EN_MARKERS)
