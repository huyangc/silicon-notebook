"""Agentic Memory P3 (T6) core — user search/answer-style preference document
parse/serialize/merge, and Ask-question language classification (sunk from
app.services.search_profile in B3).

This module is the SOLE parse/serialize/validate point for the
``user_profiles.search_profile_json`` document shape, and the sole
classifier for ``classify_ask_language`` — both IdentityStore implementations
(app.repositories, both backends) import these directly (search-profile
merge/parse/serialize via ``identity_store.py``, language classification via
``ask_state_store.py``'s ``AskStateStorePort.recent_user_ask_languages``).

NOT moved: ``SEARCH_PROFILE_BLOCK_MAX_CHARS`` and ``render_style_block``
(plus its private helpers ``_LANGUAGE_PHRASES``/``_SHAPE_PHRASES``/
``_DETAIL_PHRASES``/``_BLOCK_PREAMBLE``/``_user_field_value``) stay in
``app.services.search_profile`` — ``render_style_block`` depends on
``app.services.agent_profile_block.collapse_prompt_line``, a services-layer
dependency this leaf module must not carry. ``app.services.search_profile``
imports the five names below back for its own internal use (``merge_field``/
``parse_search_profile`` etc.) and re-exports them unchanged for existing
importers.

Pure aside from the ``app.models.identity`` value-domain import (allowed:
app.domain may depend on app.models).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping


#: The document's own schema version. Bumping this is a future concern (no
#: consumer branches on it yet); it rides in every serialized document so a
#: later version can tell old documents apart without guessing from shape.
SEARCH_PROFILE_VERSION = 1

# Value domains are DEFINED in ``app.models.identity`` (the boundary guard
# forbids models → services imports, and the wire models need them too);
# re-exported here so this module stays the one place consumers read the
# document contract from.
#
# ``SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS`` / ``SEARCH_PROFILE_DOMAIN_TERMS_MAX``:
# bounds for the user-authored ``domain_terms`` list (at most this many terms,
# each at most this many characters). Exact values are registered only in
# ``docs/product-and-api*.md`` (numeric-limit red line).
from app.models.identity import (  # noqa: E402  (contract re-export)
    ANSWER_DETAIL_VALUES,
    ANSWER_LANGUAGE_VALUES,
    ANSWER_SHAPE_VALUES,
    SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS,
    SEARCH_PROFILE_DOMAIN_TERMS_MAX,
)


#: Closed field-name vocabulary. Anything else is dropped on read, rejected
#: on write.
SEARCH_PROFILE_FIELDS: frozenset[str] = frozenset(
    {"answer_language", "answer_shape", "answer_detail", "domain_terms"}
)

#: Who wrote a field's current value. See the module docstring's "job never
#: overwrites user" rule.
SEARCH_PROFILE_ORIGINS: frozenset[str] = frozenset({"user", "job"})


def _empty_profile() -> dict:
    return {"version": SEARCH_PROFILE_VERSION, "fields": {}}


def _validate_field_value(field: str, value: Any) -> bool:
    """Is ``value`` a legal value for ``field``'s closed domain?

    Pure predicate, no side effects, shared by both the read path (drop an
    invalid stored entry) and the write path (reject an invalid write).
    """
    if field == "answer_language":
        return value in ANSWER_LANGUAGE_VALUES
    if field == "answer_shape":
        return value in ANSWER_SHAPE_VALUES
    if field == "answer_detail":
        return value in ANSWER_DETAIL_VALUES
    if field == "domain_terms":
        if not isinstance(value, list):
            return False
        if len(value) > SEARCH_PROFILE_DOMAIN_TERMS_MAX:
            return False
        return all(
            isinstance(term, str) and 0 < len(term) <= SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS
            for term in value
        )
    return False


def parse_search_profile(raw: "str | None") -> dict:
    """Parse the persisted ``user_profiles.search_profile_json`` text column.

    ``None``/empty string, malformed JSON, a non-object top level, and a
    missing/malformed ``fields`` map ALL fail-open to "not set"
    (:func:`_empty_profile`) — this is a pure optimization feature layered on
    top of every Ask; a corrupt document must never turn into a broken
    question. Each individual field entry is re-validated against the closed
    value domains and origin vocabulary on READ too, not only on write: a
    document written by a future or rolled-back version must degrade the one
    field it disagrees with, not the whole profile.
    """
    if not raw:
        return _empty_profile()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _empty_profile()
    if not isinstance(data, dict):
        return _empty_profile()
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict):
        return _empty_profile()

    fields: dict[str, dict] = {}
    for field, entry in raw_fields.items():
        if field not in SEARCH_PROFILE_FIELDS:
            continue
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin")
        if origin not in SEARCH_PROFILE_ORIGINS:
            continue
        value = entry.get("value")
        if not _validate_field_value(field, value):
            continue
        updated_at = entry.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            continue
        fields[field] = {"value": value, "origin": origin, "updated_at": updated_at}

    return {"version": SEARCH_PROFILE_VERSION, "fields": fields}


def serialize_search_profile(profile: Mapping[str, Any]) -> str:
    """Serialize a profile dict (as produced by :func:`parse_search_profile`
    / :func:`merge_field`) back to the ``search_profile_json`` text column.

    A profile with an empty ``fields`` map still serializes to a real JSON
    document (never back to ``None``/``NULL``) — ``NULL`` is reserved for
    "this column has never been written", and once a write happens the
    column holds a document even if every field has since been cleared.
    :func:`parse_search_profile` already treats an empty-fields document
    identically to ``NULL`` for display purposes, so nothing downstream can
    tell the two apart, but the column's own history (has anything ever been
    written here) is not this module's concern.
    """
    return json.dumps(
        {
            "version": SEARCH_PROFILE_VERSION,
            "fields": dict(profile.get("fields") or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def merge_field(profile: Mapping[str, Any], field: str, value: Any, origin: str) -> dict:
    """Merge one field write into ``profile``, returning a NEW profile dict
    (the input is never mutated — callers fold this over multiple fields in
    one read-modify-write transaction, and an in-place mutation would make
    that fold order-dependent in surprising ways).

    * ``origin="job"`` MUST skip (return the field untouched, no exception)
      when the field's *current* stored origin is ``"user"`` — the one rule
      that keeps a background inference from ever overwriting a person's own
      edit. This mirrors ``agent_profile_job.user_authoritative``. This guard
      is checked BEFORE the "clear" branch below, and covers it too: a
      job's ``value=None`` is a clear, not an overwrite, but deleting a
      user-authored field without authority is the same laundering this
      rule blocks, not a smaller exception to it.
    * ``value is None`` clears the field (once the guard above lets the call
      through): the stored entry is deleted outright (not written as an
      explicit "auto"), so it falls back to "not set" for both display and
      future re-inference. See the module docstring for why deletion, not a
      stored auto value.
    * ``origin="user"`` unconditionally overwrites or clears — a person's
      explicit choice always wins, including over a field a previous job run
      had filled in.
    * An unknown ``field``/``origin`` name, or a value that fails
      :func:`_validate_field_value`, raises ``ValueError`` — these are all
      programming errors on the caller's side (the wire model already
      enforces the closed enums before this is ever reached from the API;
      the job path constructs its own values from the same closed
      vocabulary), not user-facing data-quality issues to shrug off the way
      :func:`parse_search_profile` does for a pre-existing document on read.
    """
    if field not in SEARCH_PROFILE_FIELDS:
        raise ValueError(f"unknown search-profile field: {field!r}")
    if origin not in SEARCH_PROFILE_ORIGINS:
        raise ValueError(f"unknown search-profile origin: {origin!r}")

    fields = dict(profile.get("fields") or {})

    existing = fields.get(field)
    if origin == "job" and existing is not None and existing.get("origin") == "user":
        # Job writes never launder over a user-authored field — including
        # a job's own CLEAR (``value is None``). This guard must run BEFORE
        # the "value is None" branch below, not after: a job that clears a
        # field it has no authority to touch is the same violation as a job
        # that overwrites it with a new value — deleting a user's explicit
        # choice is exactly the kind of "laundering over a person's own
        # edit" this rule exists to block, not a smaller, safer variant of
        # it. Return the field as-is (not an error — this is the expected,
        # silent outcome of a job batch that happens to touch a field the
        # user has already set).
        return {"version": SEARCH_PROFILE_VERSION, "fields": fields}

    if value is None:
        fields.pop(field, None)
        return {"version": SEARCH_PROFILE_VERSION, "fields": fields}

    if not _validate_field_value(field, value):
        raise ValueError(f"invalid value for search-profile field {field!r}: {value!r}")

    if (
        existing is not None
        and existing.get("value") == value
        and existing.get("origin") == origin
    ):
        # codex #535 R3 P3:值与来源都没变的重写保留原条目(含 updated_at)——
        # 否则归纳 job 每次选出同一语言都会铸新时间戳,序列化结果永远不等,
        # identity store 里那道「serialized == raw 就跳过 UPDATE」的闸形同虚设,
        # 且档案的更新时刻被每个归纳周期虚假推进。
        return {"version": SEARCH_PROFILE_VERSION, "fields": fields}

    fields[field] = {
        "value": value,
        "origin": origin,
        # UTC, not the store's own clock-seam convention (SQLite's naive-local
        # `_now()` / PostgreSQL's `utc_now()`): this module is shared,
        # backend-neutral code, and its ONLY caller-supplied clock input is
        # already backend-specific for the ROW-level `updated_at` column
        # (each store passes its own). This per-FIELD timestamp lives inside
        # the JSON document itself and must read the same regardless of
        # which backend wrote it, so it is generated here rather than
        # threaded in as a parameter.
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    return {"version": SEARCH_PROFILE_VERSION, "fields": fields}


# --------------------------------------------------------------------------- #
# Language classification — the T7 deterministic-inference job's ENTIRE
# reason to be zero-LLM: this is a pure, deterministic character-class ratio
# over already-persisted question text, not a model call. It lives here
# rather than in ``search_profile_job.py`` because it is a property of the
# DOCUMENT's own ``answer_language`` value domain (what counts as "zh" vs
# "en" is the same question whether the caller is the T7 job or some future
# consumer), and because BOTH backends' ``ask_state_store.py`` import it
# directly — the classification happens ON THE STORE SIDE of the read (see
# ``AskStateStorePort.recent_user_ask_languages``), so a second copy living
# next to the job would invite the two to drift apart.
# --------------------------------------------------------------------------- #

#: CJK code point ranges checked by :func:`classify_ask_language`. Deliberately
#: narrow to the ranges that indicate CHINESE specifically (the only
#: non-Latin bucket this v1 classifier writes) — Unified Ideographs plus the
#: Extension A block and CJK punctuation/fullwidth forms a Chinese question
#: commonly mixes in ("，", "：", fullwidth parens). Hiragana/Katakana/Hangul
#: are deliberately NOT included: a Japanese- or Korean-heavy question is not
#: Chinese, and folding their ranges in here would misclassify it as "zh"
#: rather than correctly falling through to "other".
_CJK_RANGES: "tuple[tuple[int, int], ...]" = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x3000, 0x303F),   # CJK Symbols and Punctuation
    # codex #535 R1 P2:整个 Halfwidth and Fullwidth Forms (0xFF00-0xFFEF)
    # 含全角拉丁字母 (0xFF21-0xFF3A/0xFF41-0xFF5A) 与半角片假名
    # (0xFF66-0xFF9F)——OCR 出来的「ＨＥＬＬＯ」或日文半角片假名问题会被
    # 整句判成中文。只保留中文正文高频的全角标点片段(！＂＃…／、：；
    # ＜＝＞？＠),字母与片假名不再计入 CJK。
    (0xFF01, 0xFF0F),   # Fullwidth punctuation: ！ ＂ ＃ … ，(0xFF0C) ／
    (0xFF1A, 0xFF20),   # Fullwidth punctuation: ： ； ＜ ＝ ＞ ？ ＠
)


#: codex #535 R12 P2:平假名/片假名(含半角片假名)。汉字重的日文问题
#: (「これは日本語の質問です」)统一表意区占比一样能过 0.3 阈值——但假名
#: 是日文专属的强信号,中文正文里不出现。任何假名在场即整句不判 zh。
_KANA_RANGES: "tuple[tuple[int, int], ...]" = (
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xFF66, 0xFF9F),   # Halfwidth Katakana
)


def _is_kana(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _KANA_RANGES)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


#: Below this CJK-character ratio (of the question's non-whitespace
#: characters), a question does not count as Chinese even if it contains
#: SOME CJK text — a single borrowed term ("用 API 怎么调") should not flip
#: an otherwise-English question, and the same asymmetry protects the other
#: direction: a Chinese question that quotes a few English words does not
#: flip back either, because it never drops below this ratio in the first
#: place.
_ASK_LANGUAGE_CJK_RATIO = 0.3

#: Below this ASCII-letter ratio, a CJK-free question still does not count
#: as English — a bare identifier (``set_db_verbose_mode``, mostly letters
#: but not prose) can clear this bar and legitimately count as "en" wording,
#: while an id-shaped token (``REQ-2024-08-0091``, mostly digits/punctuation)
#: cannot, and correctly falls through to "other" instead of quietly
#: outvoting a person's real language on the strength of a pasted error code.
_ASK_LANGUAGE_LATIN_ALPHA_RATIO = 0.5


def classify_ask_language(text: "str | None") -> str:
    """Classify one Ask question into the closed three-value bucket
    ``"zh"`` / ``"en"`` / ``"other"``.

    Pure, deterministic, zero I/O and zero model calls — CJK-ratio first
    (a question can legitimately mix scripts, and the CJK share of its
    non-whitespace characters is what settles a mixed question, not which
    script happens to appear first in the string), then a SEPARATE ASCII
    letter-ratio threshold decides "en" for CJK-free text. Whitespace never
    enters either ratio's denominator: it carries no language signal in
    either script's tokenization and would otherwise make identical
    questions classify differently depending on incidental spacing.

    ``None``/empty/whitespace-only input, and any question with zero
    non-whitespace characters, returns ``"other"`` — this is the ONLY value
    this function may return that :func:`merge_field` can never legally
    store (``answer_language``'s domain is ``{"auto", "zh", "en"}``), which
    is precisely the point: "other" means "this row is not evidence FOR
    EITHER CANDIDATE", not "this row is not evidence at all". T9 fix round:
    an "other" row can never itself be the majority winner
    (``search_profile_job._WRITABLE_LANGUAGES`` is the closed ``("zh", "en")``
    tuple the job may ever write) but it DOES stay in the denominator the
    job's ``SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO`` threshold is measured
    against (see that constant's own docstring in ``app.repositories.ports``
    for why the fuller sample is the more conservative reading) — a person
    whose recent questions are half clearly Chinese and half unclassifiable
    id/error-code text should NOT count as "100% Chinese of the evidence
    that mattered" the way an excluded-denominator reading would make them.
    """
    if not text:
        return "other"
    total = 0
    cjk = 0
    latin_alpha = 0
    kana = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if _is_kana(ch):
            kana += 1
        elif _is_cjk(ch):
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            latin_alpha += 1
    if total == 0:
        return "other"
    # codex #535 R12 P2:假名在场即不判 zh——汉字重的日文问题会过统一表意区
    # 阈值,但假名是日文专属信号;这类问题落 "other",绝不归纳成中文偏好。
    if kana == 0 and cjk / total >= _ASK_LANGUAGE_CJK_RATIO:
        return "zh"
    if latin_alpha / total >= _ASK_LANGUAGE_LATIN_ALPHA_RATIO:
        return "en"
    return "other"
