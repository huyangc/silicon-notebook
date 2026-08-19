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
* answer-attached images — the ``images`` list on each citation/anchor (T4).
  v1 DOES surface them, but only as an opaque, token-scoped ``alias`` +
  ``caption`` (``PublicImage``): the raw ``asset_id`` is HMAC'd under the share
  token (``conversation_asset_alias``) and the ``element_id`` is dropped
  entirely, so no addressable handle crosses. The alias is meaningless once the
  token is revoked, and the SAME image shared through two conversations gets two
  different aliases (no cross-link correlation). The bytes are served by the
  sibling anonymous endpoint ``/public/conversations/{token}/assets/{alias}``,
  which reverses the alias against the snapshot's referenced assets only.
* collection result cards (``result_sets``) — deliberately out of v1 (design
  C-1). Not silently dropped: ``omitted_result_sets`` carries the *count* only
  (never any content), so T5's page can say "此回答还包含未公开的清单内容"
  rather than let the reader think the answer was always this short.

Numeric caps below are named constants only; the authoritative registration of
their values is T6's job in ``docs/product-and-api*.md``.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Sequence

# Mirrors ``report_public_view``'s caps; the report body (``content_md``) is
# left uncapped and the conversation answer body (``answer_md``) AND the
# question follow that precedent — a shared Q&A is the user's own artifact.
# Ask accepts questions up to 4,000 chars; capping the public question (this
# module once did, at 2,000) silently dropped the tail of the very text that
# produced the answer, with no disclosure — the same "用户编辑的数据不得静默截断"
# violation ``answer_md`` already avoids. Both are served whole (codex #522 R1).
MAX_REFERENCES = 500
MAX_SNIPPET_CHARS = 1200
# Safety ceiling on how many turns one public page renders. A conversation's
# turn count is bounded by how many times a user asked, so this almost never
# binds; it exists so a pathological conversation cannot balloon one anonymous
# response. Truncation is disclosed (``truncated_turns``) rather than silent.
MAX_TURNS = 500

# T4 — the anonymous image channel.
#
# Length of the token-derived image alias, in hex characters. 32 hex = 128 bits
# of an HMAC-SHA256 digest: unguessable, collision-free across one
# conversation's bounded image set, and short enough to sit in a URL path
# segment. Exact value is registered in ``docs/product-and-api*.md`` (T6).
ASSET_ALIAS_HEX_CHARS = 32
# Bound on one image's projected caption. Registered in docs (T6).
MAX_CAPTION_CHARS = 500
# Safety ceiling on how many DISTINCT referenced assets one endpoint request
# scans while reversing an alias. It must stay >= the MOST aliases the
# projection can ever emit, or a well-formed but very long conversation could
# show an image whose alias the endpoint stops scanning before it reaches —
# a broken image for a reader legitimately entitled to it (codex T4 review P2).
# The projection's max is ``MAX_TURNS`` turns x the upstream per-answer image cap
# (``evidence_context.CITATION_IMAGES_PER_ANSWER`` = 12) = 6000 distinct assets.
# This is set to that bound; ``test_endpoint_scan_cap_covers_every_alias_the_projection_can_emit``
# imports both constants and fails if a future cap bump breaks the invariant, so
# the two can never silently drift. Registered in docs (T6).
MAX_REFERENCED_ASSETS = 6000


def conversation_asset_alias(token: str, asset_id: str) -> str:
    """The opaque, token-scoped handle an anonymous reader gets for one image.

    ``alias = HMAC-SHA256(key=share_token, msg=asset_id)`` truncated to
    ``ASSET_ALIAS_HEX_CHARS`` hex chars. This is the ONE definition of the
    derivation — both the projection (which emits it) and the image endpoint
    (which reverses it) call this, so the two can never compute a different
    alias for the same (token, asset). Three properties fall out (design §六):
    the raw ``asset_id`` never crosses (no handle into the authenticated API);
    revoking the token makes every alias meaningless; and the same image shared
    through two conversations gets two different aliases (no cross-link
    correlation). The token is public (it is in the image URL itself), so this
    is not a secret-comparison — the alias exists to keep ``asset_id`` in, not
    to keep the token out."""
    digest = hmac.new(
        str(token or "").encode("utf-8"),
        str(asset_id or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:ASSET_ALIAS_HEX_CHARS]


def referenced_asset_ids(
    row: dict[str, Any], *, limit: int = MAX_REFERENCED_ASSETS
) -> list[str]:
    """Distinct ``asset_id``s referenced by ANY anchor/citation image across the
    snapshot's watermark-bounded turns, in stable first-seen order, bounded.

    This is deliberately a SUPERSET of what the projection reveals as aliases:
    the projection only aliases the *selected* references (anchors win, else the
    citation fallback), whereas this walks both lists on every turn. That keeps
    the endpoint from re-deriving the fragile ``_select_references`` logic, and
    is safe — an alias the projection never emitted cannot be guessed (128-bit
    HMAC), so a reader can only ever fetch what the projection showed. Every
    alias the projection DID emit is resolvable here, since selected refs are a
    subset of (anchors ∪ citations)."""
    out: list[str] = []
    seen: set[str] = set()
    turns = row.get("turns") if isinstance(row.get("turns"), list) else []
    for turn in list(turns)[:MAX_TURNS]:
        payload = turn.get("payload") if isinstance(turn, dict) else None
        if not isinstance(payload, dict):
            continue
        for group in (payload.get("anchors"), payload.get("citations")):
            for reference in _as_list(group):
                if not isinstance(reference, dict):
                    continue
                for image in _as_list(reference.get("images")):
                    if not isinstance(image, dict):
                        continue
                    asset_id = str(image.get("asset_id") or "")
                    if not asset_id or asset_id in seen:
                        continue
                    seen.add(asset_id)
                    out.append(asset_id)
                    if len(out) >= limit:
                        return out
    return out


def resolve_conversation_asset_alias(
    row: dict[str, Any],
    token: str,
    alias: str,
    *,
    limit: int = MAX_REFERENCED_ASSETS,
) -> str | None:
    """Reverse a token-derived alias back to its ``asset_id``, or ``None``.

    Recomputes the alias for each referenced asset (bounded) and returns the
    first match. ``None`` means "no match" — the endpoint 404s on it, not
    distinguishing "never existed" from "not referenced in this share". The
    ``token`` is load-bearing: the same alias resolves under the sharing token
    and to nothing under any other token."""
    target = str(alias or "").strip()
    if not target:
        return None
    for asset_id in referenced_asset_ids(row, limit=limit):
        if conversation_asset_alias(token, asset_id) == target:
            return asset_id
    return None

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


def _question_text(value: Any) -> str:
    """The question, served WHOLE — never truncated (codex #522 R1).

    Ask accepts questions up to 4,000 chars; the public projection used to cap
    this at 2,000, silently dropping the tail of the very text that produced the
    answer. Like ``answer_md``, the question is the user's own artifact: serving
    it whole beats truncating it with no disclosure."""
    return str(value or "").strip()


def _title_text(value: Any) -> str:
    """The conversation title, served WHOLE — never truncated (codex #522 R2).

    ``ConversationRenameRequest.title`` has no length cap, so the authenticated
    UI can show a title far past the retired 400-char public cap; truncating it
    only here would silently drop the tail of the user's own title. Like the
    question and ``answer_md``, the title is the user's artifact — serve it
    whole."""
    return str(value or "").strip()


def _as_list(value: Any) -> list:
    """A stored payload field coerced to a list, or [] for anything else.

    ``X or []`` only guards falsy values; a TRUTHY scalar (``5``, ``1.5``,
    ``true``) slips through and then crashes ``for x in value`` / ``len(value)``.
    A stored ``AskResponse`` always serializes ``anchors``/``citations``/
    ``result_sets`` as lists, so a scalar here can only come from a hand-edited
    or ancient row — but this is the anonymous surface, and it must DEGRADE
    (empty list) rather than 500 (codex T3 review, P1). Same ``isinstance``
    discipline this module already uses for ``payload``/``turns``."""
    return value if isinstance(value, list) else []


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
    # Last-write-wins on a duplicate key, matching the frontend real source
    # ``new Map(anchors.map(...))`` (answer-formatting.ts:93). Anchor keys are
    # unique per answer (k1..kN), so a duplicate is malformed and never happens
    # in a well-formed payload — but if it did, the public page must resolve the
    # SAME anchor the author sees, not the opposite one (codex T3 review, P2).
    anchors_by_key: dict[str, dict] = {}
    for anchor in anchors:
        if isinstance(anchor, dict):
            key = str(anchor.get("key") or "").strip()
            if key:
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


def public_turn(
    turn: Any, *, share_token: str, images_enabled: bool
) -> dict[str, Any]:
    """One Q&A turn projected from its stored ``AskResponse`` payload.

    ``share_token`` is threaded in (not read from Settings — this stays a pure
    function) so each answer-attached image can be projected as a token-derived
    ``alias`` rather than its raw ``asset_id`` (T4). ``images_enabled`` mirrors
    the deployment's ``MINERU_RETURN_IMAGES``: when the deployment stores no
    images, the public page must not hand out aliases that would resolve to
    bytes it decided not to serve, so NO ``PublicImage`` is emitted."""
    row = turn if isinstance(turn, dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    # Same body the authenticated view renders: ``answer.answer || answer.conclusion``
    # (frontend answer-panel.tsx). References are selected against this exact text.
    answer_md = str(payload.get("answer") or payload.get("conclusion") or "")
    selected = _select_references(
        answer_md,
        _as_list(payload.get("anchors")),
        _as_list(payload.get("citations")),
    )
    visible = [
        public_reference(key, reference)
        for key, reference in selected[:MAX_REFERENCES]
    ]
    return {
        "question": _question_text(row.get("question")),
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
        "omitted_result_sets": len(_as_list(payload.get("result_sets"))),
        # T4 — images the reader actually sees: those attached to the SELECTED
        # references (same slice ``visible`` was taken from), projected to an
        # opaque alias. Never the raw ``asset_id``/``element_id``. Off when the
        # deployment stores no images.
        "images": _public_images(
            selected[:MAX_REFERENCES], share_token, images_enabled
        ),
    }


def _public_images(
    selected: Sequence[tuple[str, Any]], share_token: str, images_enabled: bool
) -> list[dict[str, str]]:
    """The token-aliased images for a turn's SELECTED references, deduped by
    ``asset_id`` (an image cited twice shows once) in first-seen order.

    Reads only ``asset_id`` (to derive the alias) and ``caption`` (public) from
    each image dict; ``element_id`` and every other key are dropped by
    construction. Returns ``[]`` when the deployment stores no images."""
    if not images_enabled:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for _key, reference in selected:
        row = reference if isinstance(reference, dict) else {}
        for image in _as_list(row.get("images")):
            if not isinstance(image, dict):
                continue
            asset_id = str(image.get("asset_id") or "")
            if not asset_id or asset_id in seen:
                continue
            seen.add(asset_id)
            out.append({
                "alias": conversation_asset_alias(share_token, asset_id),
                "caption": _text(image.get("caption"), MAX_CAPTION_CHARS),
            })
    return out


def public_conversation_payload(
    row: dict[str, Any], *, share_token: str, images_enabled: bool
) -> dict[str, Any]:
    """Assemble the anonymous view from a token-resolved conversation row.

    ``share_token`` and ``images_enabled`` are threaded down to every turn so
    answer-attached images can be projected as token-derived aliases (T4). The
    token is passed in rather than read from the row so this stays a pure
    function; the route (which has the raw token) supplies it.

    The caller (the anonymous route) has already run the live creator
    re-authorization and popped the GATE fields (``notebook_id``/``created_by``);
    this allowlist would ignore them regardless, but they must not reach here."""
    turns = row.get("turns") if isinstance(row.get("turns"), list) else []
    return {
        "title": _title_text(row.get("title")),
        "created_at": _text(row.get("created_at"), 64),
        # The read watermark: "内容截至何时". Comes from ``shared_through_at``.
        "shared_at": _text(row.get("shared_through_at"), 64),
        "turns": [
            _safe_turn(turn, share_token=share_token, images_enabled=images_enabled)
            for turn in list(turns)[:MAX_TURNS]
        ],
        "truncated_turns": len(turns) > MAX_TURNS,
    }


def _safe_turn(
    turn: Any, *, share_token: str, images_enabled: bool
) -> dict[str, Any]:
    """``public_turn`` with a belt-and-suspenders fallback (codex T3 review).

    After the ``_as_list``/``isinstance`` guards ``public_turn`` is total for any
    DATA shape, so this catch only fires on a future code regression. When it
    does, one bad turn must not 500 the whole anonymous page — degrade to a
    minimal turn that keeps the question (so turn count/order stay aligned) and
    empties everything else, rather than dropping the row (which would misalign
    the numbering the way position-based reference numbering was avoided)."""
    try:
        return public_turn(
            turn, share_token=share_token, images_enabled=images_enabled
        )
    except Exception:
        row = turn if isinstance(turn, dict) else {}
        return {
            "question": _question_text(row.get("question")),
            "answer_md": "",
            "asked_at": "",
            "answered_at": "",
            "evidence_level": "inferred",
            "references": [],
            "reference_count": 0,
            "truncated_references": False,
            "omitted_result_sets": 0,
            "images": [],
        }
