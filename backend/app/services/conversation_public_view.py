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
  which reverses the alias against exactly the assets THIS projection aliased —
  the same selected references, no superset (codex #522 R5), so the endpoint can
  only ever serve an image the page itself disclosed.
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
from typing import Any, Iterator, Sequence

# Mirrors ``report_public_view``'s caps; the report body (``content_md``) is
# left uncapped and the conversation answer body (``answer_md``) AND the
# question follow that precedent — a shared Q&A is the user's own artifact.
# Ask accepts questions up to ``app.models.ask.ASK_QUESTION_MAX_CHARS`` (4,000);
# capping the public question (this module once did, at 2,000) silently dropped
# the tail of the very text that produced the answer, with no disclosure — the
# same "用户编辑的数据不得静默截断" violation ``answer_md`` already avoids. Both
# are served whole (codex #522 R1).
MAX_REFERENCES = 500
MAX_SNIPPET_CHARS = 1200
# Per-reference title / original-file-name cap. Unlike the question and the
# conversation title (the user's own artifacts, served whole), a reference title
# is evidence metadata and stays bounded — but truncation is DISCLOSED via
# ``title_truncated`` rather than dropped silently (codex #522 R3; AGENTS.md
# 用户编辑的数据不得静默截断). Registered in ``docs/product-and-api*.md`` (T6).
MAX_REFERENCE_TITLE_CHARS = 400
# Sunk to app.domain.conversation_public_view in B3 (app.repositories'
# ask_state_store imports it directly there); re-exported here unchanged.
from app.domain.conversation_public_view import MAX_TURNS  # noqa: F401

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
# Since R5 the endpoint enumerates EXACTLY the projected selection (not the old
# anchors ∪ citations superset), so its distinct-asset count can only be <= the
# projection's; the projection's own max is ``MAX_TURNS`` turns x the upstream
# per-answer image cap (``evidence_context.CITATION_IMAGES_PER_ANSWER`` = 12) =
# 6000 distinct assets. This is set to that bound;
# ``test_endpoint_scan_cap_covers_every_alias_the_projection_can_emit`` imports
# both constants and fails if a future cap bump breaks the invariant, so the two
# can never silently drift. Registered in docs (T6).
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
    """Distinct ``asset_id``s the public projection actually aliases across the
    snapshot's watermark-bounded turns, in stable first-seen order, bounded.

    The set the endpoint can serve is EXACTLY the set the page discloses — not a
    superset (codex #522 R5). Each turn is resolved through the SAME
    ``_turn_body_and_references`` / ``_selected_images`` walk the projection uses,
    so an image attached only to an UNSELECTED reference (a citation when anchors
    won, or an anchor past ``MAX_REFERENCES``) is never enumerated here and its
    alias 404s.

    Why not the old superset of (anchors ∪ citations): the share token is public
    (it is in the image URL), so ``conversation_asset_alias`` is not a secret —
    a collaborator who already knows a raw ``asset_id`` can compute
    ``HMAC(token, asset_id)`` themselves. Enumerating an un-projected asset here
    would then let that collaborator fetch an image the public page never showed
    (an image from a dropped/unselected reference). Restricting the endpoint to
    the projected selection closes that: the endpoint can only ever serve bytes
    the page itself disclosed."""
    out: list[str] = []
    seen: set[str] = set()
    turns = row.get("turns") if isinstance(row.get("turns"), list) else []
    for turn in list(turns)[:MAX_TURNS]:
        payload = turn.get("payload") if isinstance(turn, dict) else None
        if not isinstance(payload, dict):
            continue
        _answer_md, selected, _total = _turn_body_and_references(payload)
        for asset_id, _image in _selected_images(selected):
            if asset_id in seen:
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


def _text_flag(value: Any, limit: int) -> tuple[str, bool]:
    """Trimmed value capped at ``limit``, plus whether the cap actually bit.

    The public projection must DISCLOSE truncation of an evidence field, never
    drop the tail silently (codex #522 R3; AGENTS.md 用户编辑的数据不得静默截断).
    The bool lets the page mark a reference whose title/excerpt was clipped."""
    text = str(value or "").strip()
    return text[:limit], len(text) > limit


def _question_text(value: Any) -> str:
    """The question, served WHOLE — never truncated (codex #522 R1).

    Ask bounds a submitted question at ``app.models.ask.ASK_QUESTION_MAX_CHARS``
    (4,000); the public projection used to cap this at 2,000, silently dropping
    the tail of the very text that produced the answer. Like ``answer_md``, the
    question is the user's own artifact: serving it whole beats truncating it
    with no disclosure.

    "Whole" is only a *bounded* promise because of that write-side rail — an
    anonymous response is otherwise unbounded by client input (the finding codex
    #525 R1 P2 raised against the report projection, closed for Ask by
    ``AskRequest.question``'s ``max_length`` and by ``ask_notebook``'s matching
    refusal on the MCP surface).
    ``test_public_question_is_bounded_by_the_write_side_rail`` pins the two
    halves together so neither can be relaxed without the other failing.

    One knowingly-unbounded leftover, recorded rather than papered over: turns
    written *before* that rail. Bounding one here would need a disclosure field
    on the turn plus a public-page change, so it is tracked as separate work
    rather than fixed by a silent clip. (The conversation title, the other
    leftover this note used to carry, now has its own write-side rail — see
    ``_title_text``.)"""
    return str(value or "").strip()


def _title_text(value: Any) -> str:
    """The conversation title, served WHOLE — never truncated (codex #522 R2).

    Truncating it only here would silently drop the tail of the user's own
    title, past the retired 400-char public cap. Like the question and
    ``answer_md``, the title is the user's artifact — serve it whole.

    Same two-halves guardrail as ``_question_text``: "whole" is a *bounded*
    promise only because the write side refuses an over-length title.
    ``ConversationRenameRequest.title`` carries
    ``max_length=CONVERSATION_TITLE_MAX_CHARS`` (200) — renaming is the only way
    a title grows past the 60 characters ``ensure_conversation`` slices off the
    first question, so that one endpoint is the whole write side.
    ``test_public_title_is_bounded_by_the_write_side_rail`` pins the two halves
    together so neither can be relaxed without the other failing.

    A title renamed *before* that rail can still be longer, exactly like a
    pre-rail question; it is rendered faithfully rather than clipped."""
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


def public_reference(key: str, reference: Any) -> dict[str, Any]:
    """One reference as an anonymous reader sees it: nothing addressable.

    Handles both wire shapes with one allowlist — ``AnswerAnchor`` (title in
    ``source_title``/``label``/``name``, excerpt in ``snippet``) and
    ``Citation`` (title in ``label``, excerpt in ``quoted_span``). Reads no id
    key from either, so ``source_id``/``element_id``/``object_id``/
    ``notebook_id``/``memory_id``/``provenance``/``knowhow``/``images`` are
    dropped by construction.

    ``title``/``snippet`` stay bounded (evidence metadata, not the user's own
    artifact the way the question/conversation-title are), but an over-length
    value sets ``title_truncated``/``snippet_truncated`` so the page can DISCLOSE
    the clip rather than drop the tail silently (codex #522 R3)."""
    row = reference if isinstance(reference, dict) else {}
    title, title_truncated = _text_flag(
        row.get("source_title") or row.get("label") or row.get("name"),
        MAX_REFERENCE_TITLE_CHARS,
    )
    # Anchor excerpt is ``snippet``; citation excerpt is ``quoted_span``.
    snippet, snippet_truncated = _text_flag(
        row.get("snippet") or row.get("quoted_span"), MAX_SNIPPET_CHARS
    )
    # The original uploaded filename is client-supplied user data too, so it gets
    # the same truncation disclosure as the title/excerpt rather than being
    # silently clipped to a prefix (codex #522 R4; AGENTS.md 数值上限与截断).
    file_name, file_name_truncated = _text_flag(
        row.get("source_file_name"), MAX_REFERENCE_TITLE_CHARS
    )
    return {
        "key": _text(key, 24),
        "title": title,
        "file_name": file_name,
        "location": _text(row.get("location_label"), 200),
        "snippet": snippet,
        "title_truncated": title_truncated,
        "snippet_truncated": snippet_truncated,
        "file_name_truncated": file_name_truncated,
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
    # ONE derivation of both the rendered body and the selected references,
    # shared with ``referenced_asset_ids`` so the image endpoint can serve
    # exactly the aliases this projection emits (codex #522 R5). ``selected`` is
    # already bounded to ``MAX_REFERENCES``; ``total`` is the pre-bound count so
    # truncation can still be disclosed.
    answer_md, selected, total = _turn_body_and_references(payload)
    visible = [public_reference(key, reference) for key, reference in selected]
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
        "truncated_references": total > MAX_REFERENCES,
        # C-1: collection cards are out of v1, but the count (content-free) lets
        # the page disclose that something was withheld here.
        "omitted_result_sets": len(_as_list(payload.get("result_sets"))),
        # T4 — images the reader actually sees: those attached to the SELECTED
        # references (same ``selected`` slice ``visible`` was taken from),
        # projected to an opaque alias. Never the raw ``asset_id``/``element_id``.
        # Off when the deployment stores no images.
        "images": _public_images(selected, share_token, images_enabled),
    }


def _turn_body_and_references(
    payload: Any,
) -> tuple[str, list[tuple[str, dict]], int]:
    """``(answer_md, selected references bounded to MAX_REFERENCES, pre-bound
    total)`` for one turn payload.

    The ONE derivation of both the rendered answer body AND the selected
    references, consumed by ``public_turn`` (which renders them + aliases their
    images) and ``referenced_asset_ids`` (which the image endpoint uses to decide
    what it may serve). Because both go through this, the two can never compute a
    different body or a different selection, so the set of image aliases the
    endpoint can resolve is EXACTLY the set the page emits — by construction, not
    by two copies of the selection happening to agree (codex #522 R5).

    ``answer_md`` uses the SAME ``answer or conclusion`` expression the
    authenticated view renders (frontend answer-panel.tsx), so references are
    always selected against the exact text the reader sees. ``total`` is returned
    so ``public_turn`` can still disclose truncation past ``MAX_REFERENCES``."""
    row = payload if isinstance(payload, dict) else {}
    answer_md = str(row.get("answer") or row.get("conclusion") or "")
    selected = _select_references(
        answer_md,
        _as_list(row.get("anchors")),
        _as_list(row.get("citations")),
    )
    return answer_md, selected[:MAX_REFERENCES], len(selected)


def _selected_images(
    selected: Sequence[tuple[str, Any]]
) -> Iterator[tuple[str, dict]]:
    """Distinct ``(asset_id, image)`` attached to a turn's SELECTED references,
    in first-seen order, deduped by ``asset_id`` (an image cited twice yields
    once).

    The ONE image walk both the projection (``_public_images``, which aliases
    them) and the endpoint reverse-index (``referenced_asset_ids``, which decides
    what bytes it may serve) consume — so the endpoint serves exactly the asset
    set the page discloses, never a superset (codex #522 R5). Reads only
    ``asset_id`` (for dedup / alias); the image dict is yielded so the caller can
    pull its public ``caption``, and every other key is dropped by
    construction."""
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
            yield asset_id, image


def _public_images(
    selected: Sequence[tuple[str, Any]], share_token: str, images_enabled: bool
) -> list[dict[str, str]]:
    """The token-aliased images for a turn's SELECTED references, deduped by
    ``asset_id`` (an image cited twice shows once) in first-seen order.

    Reuses ``_selected_images`` — the SAME walk ``referenced_asset_ids`` consumes
    — so every alias emitted here is resolvable by the endpoint and no other
    alias is (codex #522 R5). Reads only ``asset_id`` (to derive the alias) and
    ``caption`` (public); ``element_id`` and every other key are dropped by
    construction. Returns ``[]`` when the deployment stores no images."""
    if not images_enabled:
        return []
    return [
        {
            "alias": conversation_asset_alias(share_token, asset_id),
            "caption": _text(image.get("caption"), MAX_CAPTION_CHARS),
        }
        for asset_id, image in _selected_images(selected)
    ]


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
