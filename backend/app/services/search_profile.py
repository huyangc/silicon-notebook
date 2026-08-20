"""Agentic Memory P3 (T6) — the per-user "search/answer style" preference
document, ``user_profiles.search_profile_json``.

This module is the SOLE parse/serialize/validate point for that document —
every reader and writer (both ``IdentityStore`` implementations, the
``PATCH /me/search-profile`` endpoint, and the T7 deterministic-inference job
that lands later) must go through the functions here rather than touching the
JSON shape directly. A second hand-rolled copy of this validation is exactly
the kind of thing that drifts: one path accepts a value the other rejects, or
one path forgets the "job never overwrites a user edit" rule.

**Document shape** (``NULL`` on the column means "never touched" — this
module never sees that case; callers pass ``None`` straight through)::

    {"version": 1, "fields": {
        "answer_language": {"value": "zh", "origin": "job", "updated_at": "..."},
        "answer_shape": {"value": "table_first", "origin": "user", "updated_at": "..."},
        "domain_terms": {"value": ["PPA"], "origin": "user", "updated_at": "..."}
    }}

**Fields are a closed set** (:data:`SEARCH_PROFILE_FIELDS`); each has its own
closed value domain. An unknown field name is dropped on *read* (fail-open —
a document written by a future/rolled-back version must degrade one field,
not the whole profile) and rejected on *write* (a caller passing an unknown
field name is a programming error, not a data-quality issue to shrug off).

**Per-field provenance is the whole point of ``origin``.** ``origin="user"``
means a person explicitly chose this value; ``origin="job"`` means the T7
inference job filled it in. A job write MUST NOT overwrite a field whose
*current* stored origin is ``"user"`` — this is the same rule
``agent_profile_job.user_authoritative``/``retire_disposition`` already
enforce for the notebook-understanding blocks (Agentic Memory P1), applied
here to preference fields instead of prose blocks: a background inference
must never launder over a person's own explicit choice. The check is
:func:`merge_field`'s one load-bearing branch.

**Clearing is deletion, not a stored "auto".** A user setting a field back to
"automatic" removes that field's entry from ``fields`` entirely — it is NOT
stored as ``{"value": "auto", "origin": "user", ...}``. Storing an explicit
"auto" would freeze the field: it means "the user has an opinion and that
opinion is auto", which a job would then (correctly, per the rule above)
never touch again. An absent field is what makes the field re-inferable, and
it is also what :func:`render_style_block` treats as "say nothing about this
field" — the block never mentions "auto" for anything, since it only lists
fields with an actual preference.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# Closed vocabularies. These are protocol boundaries (CLAUDE.md "数值上限"),
# not implementation details. They are DEFINED in app/models/identity.py
# (the domain-model boundary guard forbids models → services imports, and
# the SearchProfileUpdate wire model needs them too) and re-exported here;
# both IdentityStore implementations import the FUNCTIONS in this module
# rather than reimplementing validation.
# --------------------------------------------------------------------------- #

#: The document's own schema version. Bumping this is a future concern (no
#: consumer branches on it yet); it rides in every serialized document so a
#: later version can tell old documents apart without guessing from shape.
SEARCH_PROFILE_VERSION = 1

# Value domains are DEFINED in ``app.models.identity`` (the boundary guard
# forbids models → services imports, and the wire models need them too);
# re-exported here so this module stays the one place consumers read the
# document contract from.
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

#: ``domain_terms`` caps: at most this many terms, each at most this many
#: characters. Registered in docs/product-and-api*.md alongside the other
#: field caps once T8/T9 land the consumer surfaces (T6 only establishes the
#: enforced boundary).


#: Hard cap on :func:`render_style_block`'s output. Consumed starting T8
#: (the plan/answer prompt injection); the constant lands now because T8's
#: renderer is this module's job, not the injection call site's.
SEARCH_PROFILE_BLOCK_MAX_CHARS = 200


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
# Rendering — consumed starting T8 (plan/answer prompt injection). The
# function lands now because it is this document's own concern (what does
# "the user prefers table_first" mean in a sentence?), not the injection call
# site's; T8 only decides WHEN to call it.
# --------------------------------------------------------------------------- #

_LANGUAGE_PHRASES = {"zh": "answer in Chinese", "en": "answer in English"}
_SHAPE_PHRASES = {
    "bullets": "prefer bullet points",
    "table_first": "prefer tables where the content fits one",
    "prose": "prefer flowing prose over lists",
}
_DETAIL_PHRASES = {"concise": "be concise", "detailed": "be thorough and detailed"}
#: Fixed framing text, kept short on purpose: it eats into the same
#: SEARCH_PROFILE_BLOCK_MAX_CHARS budget as the rendered preference parts,
#: and the domain-terms part alone can already carry up to 10 x 32
#: characters. A more verbose framing sentence would starve that part on
#: every profile that sets more than one or two fields.
_BLOCK_PREAMBLE = (
    "User style preferences (wording/organization only; "
    "not evidence, not retrieval scope): "
)


def render_style_block(profile: Mapping[str, Any]) -> str:
    """Render ``profile``'s SET fields as one bounded English prompt line.

    This is model-facing scaffolding text (an internal prompt fragment), not
    user-facing UI copy — the UI-vocabulary guard does not apply to it. It
    carries ONLY organization/wording preferences: never a user id, notebook
    name, or source word (a reverse guard at the T8 injection surface
    asserts this structurally, mirroring the retrieval-experience action
    vocabulary's own scope-word guard).

    A field with no entry, or whose value is the sentinel ``"auto"``, is
    silently omitted rather than rendered as "no preference" — an absent
    field and an explicit "auto" are the same thing to a reader of the
    block (compare :func:`merge_field`, which never stores "auto" for this
    exact reason).

    Bounded to :data:`SEARCH_PROFILE_BLOCK_MAX_CHARS`: if the naive render
    would exceed it, parts are dropped from the least-specific end
    (``domain_terms`` first — the single most likely overflow source, since
    it alone can carry up to 10 × 32 characters) until it fits, rather than
    truncating mid-word. An empty profile (or one where nothing survives the
    budget) renders to ``""`` — a call site must treat that identically to
    "the feature never ran" (the historical, pre-feature call shape).
    """
    fields = profile.get("fields") or {}
    parts: list[str] = []

    language_entry = fields.get("answer_language") or {}
    language = language_entry.get("value")
    if language and language != "auto" and language in _LANGUAGE_PHRASES:
        parts.append(_LANGUAGE_PHRASES[language])

    shape_entry = fields.get("answer_shape") or {}
    shape = shape_entry.get("value")
    if shape and shape != "auto" and shape in _SHAPE_PHRASES:
        parts.append(_SHAPE_PHRASES[shape])

    detail_entry = fields.get("answer_detail") or {}
    detail = detail_entry.get("value")
    if detail and detail != "auto" and detail in _DETAIL_PHRASES:
        parts.append(_DETAIL_PHRASES[detail])

    terms_entry = fields.get("domain_terms") or {}
    terms = terms_entry.get("value")
    if isinstance(terms, list) and terms:
        parts.append("familiar terms: " + ", ".join(terms))

    while parts:
        block = _BLOCK_PREAMBLE + "; ".join(parts) + "."
        if len(block) <= SEARCH_PROFILE_BLOCK_MAX_CHARS:
            return block
        parts.pop()  # drop the least-specific (rightmost) part and retry

    return ""
