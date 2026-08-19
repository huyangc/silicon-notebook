"""The projection a shared conversation exposes to anonymous readers (T3).

This module is the disclosure boundary for public conversation-share links,
the sibling of ``report_public_view`` and written to the same rule: it is an
explicit **allowlist**, not a redaction pass. Anything a future change adds to
the stored ``AskResponse`` payload stays private until it is named here. Adding
a field to ``Citation``/``AnswerAnchor``/``AskResponse`` never widens this
surface by accident — it has to be pulled through ``public_reference`` /
``public_turn`` on purpose.

What a public reader gets, and why:

* per turn — the question, the answer body, the timing, and the evidence level
  (``grounded``/``overview``/``inferred``): that is the artifact being shared,
  and the evidence level is part of the answer's stated credibility, not an
  internal flag;
* per reference — the display key, title, original file name, location, and the
  stored excerpt, so the ``[k]`` markers in the answer body can actually be
  checked against something.

What never crosses, and why:

* every addressable id — ``source_id`` / ``element_id`` / ``object_id`` /
  ``notebook_id`` / ``memory_id`` / ``provenance`` / ``knowhow`` (which nests a
  ``table_id``/``row_id``). Publishing any of them would let a reader probe the
  authenticated API for material the link was never meant to include, and they
  buy the reader nothing because the public page deliberately cannot open full
  sources. The allowlist reads none of these keys, so they are dropped by
  construction — a *Memory* citation keeps its title/excerpt (self-publishing:
  a user's own answer can only cite that user's own private Memory) but loses
  its ``memory_id``.
* the whole reasoning surface — ``reasoning_trace`` / ``intent`` /
  ``retrieval_scope`` / ``retrieval_query`` / ``top_relevance`` / ``mode`` /
  ``llm_mode`` / ``retrieval_effort`` / ``index_required``. Same "轨迹不外发"
  boundary the report projection draws.
* answer-attached images — the ``images`` list on each citation/anchor. v1 of
  the public page does NOT show them (that is T4's anonymous, token-scoped image
  channel); the allowlist has no image field, so their ``asset_id``/
  ``element_id`` never leak here either.
* collection result cards (``result_sets``) — deliberately out of v1 (design
  C-1). Not silently dropped: ``omitted_result_sets`` carries the *count* only
  (never any content), so T5's page can say "此回答还包含未公开的清单内容"
  rather than let the reader think the answer was always this short.

Numeric caps below are named constants only; the authoritative registration of
their values is T6's job in ``docs/product-and-api*.md``.
"""
from __future__ import annotations

import re
from typing import Any, Sequence

# Mirrors ``report_public_view``'s caps; the report body (``content_md``) is
# left uncapped and the conversation answer body (``answer_md``) follows that
# precedent — a shared answer is the artifact, truncating it mid-sentence would
# be worse than serving it whole.
MAX_REFERENCES = 500
MAX_SNIPPET_CHARS = 1200
MAX_QUESTION_CHARS = 2000
# Safety ceiling on how many turns one public page renders. A conversation's
# turn count is bounded by how many times a user asked, so this almost never
# binds; it exists so a pathological conversation cannot balloon one anonymous
# response. Truncation is disclosed (``truncated_turns``) rather than silent.
MAX_TURNS = 500

# Ported verbatim from ``frontend/app/answer-formatting.ts``
# (``ANCHOR_MARKER_GROUP_RE``). The selection below mirrors that file's
# ``buildAnswerReferences`` so the public page shows the SAME references the
# authenticated reader sees; the two implementations are pinned together by
# ``test_conversation_public_view.py``. Keep them in lockstep.
_ANCHOR_MARKER_GROUP_RE = re.compile(
    r"(?:\[(?:k\d+\s*[,，]\s*)*k\d+\]|【(?:k\d+\s*[,，]\s*)*k\d+】)"
)
_MARKER_KEY_SPLIT_RE = re.compile(r"[,，]")


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _marker_keys(marker: str) -> list[str]:
    """The anchor keys inside one ``[k1, k2]`` / ``【k1】`` marker group.

    Mirrors ``anchorKeysFromMarker``: strip the single-char brackets, split on
    either comma, trim, drop empties."""
    return [part.strip() for part in _MARKER_KEY_SPLIT_RE.split(marker[1:-1]) if part.strip()]


def _select_references(
    answer_md: str,
    anchors: Sequence[Any],
    citations: Sequence[Any],
) -> list[tuple[str, dict]]:
    """Pick the reference list the reader actually sees, as (key, ref) pairs.

    Faithful port of ``frontend/app/answer-formatting.ts::buildAnswerReferences``
    — the ONE selection the authenticated conversation view renders:

    * Anchors are authoritative: scan the answer body for ``[k]``/``【k】``
      marker groups; a group binds only when EVERY key in it resolves to an
      anchor (all-or-nothing per group — a grouped marker is one evidence
      claim). Bound anchors are collected in marker-appearance order, deduped by
      key, and carry their own ``key`` (``"k1"``).
    * If not one marker resolved, fall back to the flat ``citations`` list, in
      order. Citations carry no ``[k]`` key, so their key is the 1-based
      position (``"1"``, ``"2"``, …) — exactly what ``buildAnswerReferences``'s
      fallback uses for its ``displayLabel``, and what the chunk-mode answer
      body's ``[1]`` markers bind against.

    The key is preserved (not recomputed positionally) so the public page can
    derive each ``[k]`` marker's number from the key: the reference filter in
    ``public_turn`` drops entries with neither title nor snippet, and positional
    numbering would then misalign the body's ``[12]`` from the list's 11th row
    (design §七 item 4).
    """
    anchors_by_key: dict[str, dict] = {}
    for anchor in anchors:
        if isinstance(anchor, dict):
            key = str(anchor.get("key") or "").strip()
            if key and key not in anchors_by_key:
                anchors_by_key[key] = anchor

    selected: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for match in _ANCHOR_MARKER_GROUP_RE.finditer(answer_md or ""):
        keys = _marker_keys(match.group(0))
        matched = [anchors_by_key.get(key) for key in keys]
        # Never bind only a known subset: if any key in the group is unknown,
        # leave the whole group unbound (mirrors buildAnswerReferences).
        if not matched or any(anchor is None for anchor in matched):
            continue
        for key, anchor in zip(keys, matched):
            if key in seen:
                continue
            seen.add(key)
            selected.append((key, anchor))

    if selected:
        return selected

    return [
        (str(index + 1), citation)
        for index, citation in enumerate(citations)
        if isinstance(citation, dict)
    ]


def public_reference(key: str, reference: Any) -> dict[str, str]:
    """One reference as an anonymous reader sees it: nothing addressable.

    Handles both wire shapes with one allowlist — ``AnswerAnchor`` (title in
    ``source_title``/``label``/``name``, excerpt in ``snippet``) and
    ``Citation`` (title in ``label``, excerpt in ``quoted_span``). Reads no id
    key from either, so ``source_id``/``element_id``/``object_id``/
    ``notebook_id``/``memory_id``/``provenance``/``knowhow``/``images`` are
    dropped by construction."""
    row = reference if isinstance(reference, dict) else {}
    return {
        "key": _text(key, 24),
        "title": _text(
            row.get("source_title") or row.get("label") or row.get("name"), 400
        ),
        "file_name": _text(row.get("source_file_name"), 400),
        "location": _text(row.get("location_label"), 200),
        # Anchor excerpt is ``snippet``; citation excerpt is ``quoted_span``.
        "snippet": _text(row.get("snippet") or row.get("quoted_span"), MAX_SNIPPET_CHARS),
    }


def public_turn(turn: Any) -> dict[str, Any]:
    """One Q&A turn projected from its stored ``AskResponse`` payload."""
    row = turn if isinstance(turn, dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    # Same body the authenticated view renders: ``answer.answer || answer.conclusion``
    # (frontend answer-panel.tsx). References are selected against this exact text.
    answer_md = str(payload.get("answer") or payload.get("conclusion") or "")
    selected = _select_references(
        answer_md,
        payload.get("anchors") or [],
        payload.get("citations") or [],
    )
    visible = [
        public_reference(key, reference)
        for key, reference in selected[:MAX_REFERENCES]
    ]
    return {
        "question": _text(row.get("question"), MAX_QUESTION_CHARS),
        "answer_md": answer_md,
        "asked_at": _text(payload.get("asked_at"), 64),
        "answered_at": _text(payload.get("answered_at"), 64),
        # Pessimistic default matches AskResponse.evidence_level for legacy
        # payloads written before the field existed.
        "evidence_level": _text(payload.get("evidence_level"), 40) or "inferred",
        "references": [
            item for item in visible if item["title"] or item["snippet"]
        ],
        "reference_count": len(visible),
        "truncated_references": len(selected) > MAX_REFERENCES,
        # C-1: collection cards are out of v1, but the count (content-free) lets
        # the page disclose that something was withheld here.
        "omitted_result_sets": len(payload.get("result_sets") or []),
    }


def public_conversation_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Assemble the anonymous view from a token-resolved conversation row.

    The caller (the anonymous route) has already run the live creator
    re-authorization and popped the GATE fields (``notebook_id``/``created_by``);
    this allowlist would ignore them regardless, but they must not reach here."""
    turns = row.get("turns") if isinstance(row.get("turns"), list) else []
    return {
        "title": _text(row.get("title"), 400),
        "created_at": _text(row.get("created_at"), 64),
        # The read watermark: "内容截至何时". Comes from ``shared_through_at``.
        "shared_at": _text(row.get("shared_through_at"), 64),
        "turns": [public_turn(turn) for turn in list(turns)[:MAX_TURNS]],
        "truncated_turns": len(turns) > MAX_TURNS,
    }
