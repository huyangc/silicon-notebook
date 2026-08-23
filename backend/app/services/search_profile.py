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

from typing import Any, Mapping

from app.domain.search_profile import (
    ANSWER_DETAIL_VALUES,
    ANSWER_LANGUAGE_VALUES,
    ANSWER_SHAPE_VALUES,
    SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS,
    SEARCH_PROFILE_DOMAIN_TERMS_MAX,
    SEARCH_PROFILE_FIELDS,
    SEARCH_PROFILE_ORIGINS,
    SEARCH_PROFILE_VERSION,
    classify_ask_language,
    merge_field,
    parse_search_profile,
    serialize_search_profile,
)

# T9 fix round (P1): ``render_style_block``'s ``domain_terms`` packing reuses
# the SAME single-line-folding primitive Agentic Memory P1 already exports
# for exactly this purpose (untrusted, model-independent free text landing
# in a rendered prompt line) — see this alias's own docstring in
# ``agent_profile_block.py`` and :func:`render_style_block`'s docstring below
# for why a bare newline in a domain term is a forgery risk, not just a
# cosmetic one. Importing the module-level alias (not reimplementing
# ``" ".join(value.split())`` a third time) is a leaf → leaf import: neither
# module imports the other, so this creates no cycle.
from app.services.agent_profile_block import collapse_prompt_line  # noqa: E402

# --------------------------------------------------------------------------- #
# SEARCH_PROFILE_VERSION / SEARCH_PROFILE_FIELDS / SEARCH_PROFILE_ORIGINS,
# the parse/serialize/merge functions, the ANSWER_*_VALUES / domain-terms
# caps (re-exported from app.models.identity), and classify_ask_language
# sunk to app.domain.search_profile in B3 (imported above, re-exported
# unchanged below for existing importers — both IdentityStore
# implementations, ask_state_store.py, ask_service.py, reasoning_retrieval.py
# and the search-profile test suite). render_style_block below still needs
# SEARCH_PROFILE_VERSION/SEARCH_PROFILE_FIELDS purely for its own docstrings/
# comments (no runtime use), so nothing else in this module changed.
# --------------------------------------------------------------------------- #


#: ``domain_terms`` caps: at most this many terms, each at most this many
#: characters. Registered in docs/product-and-api*.md alongside the other
#: field caps once T8/T9 land the consumer surfaces (T6 only establishes the
#: enforced boundary).


#: Hard cap on :func:`render_style_block`'s output. Consumed starting T8
#: (the plan/answer prompt injection); the constant lands now because T8's
#: renderer is this module's job, not the injection call site's.
# codex #535 R5 P2:预算必须大到「最大合法档案必然完整渲染」——三个封闭
# 字段的最长短语 + 10×32 满额术语 + 框定语合计 < 600,否则 UI/API 报告
# 「已保存」的用户选择会在渲染点被静默丢弃(违反「用户编辑的数据不得静默
# 截断」红线)。逐条装入循环保留,作为对未来加字段时的纵深防御;
# test_the_maximal_legal_profile_renders_completely 钉住「上限与预算不许
# 静默漂移分开」这条不变量。精确值只登记 docs/product-and-api*.md。
SEARCH_PROFILE_BLOCK_MAX_CHARS = 600




# --------------------------------------------------------------------------- #
# Rendering — consumed starting T8 (plan/answer prompt injection). The
# function lands now because it is this document's own concern (what does
# "the user prefers table_first" mean in a sentence?), not the injection call
# site's; T8 only decides WHEN to call it.
# --------------------------------------------------------------------------- #

#: ``answer_language`` is the one field whose rendered phrase must say more
#: than "here is a preference" — ``answer_prompt``'s own rule 4 ("Answer in
#: the question's language") is a DEFAULT the model is told to follow on
#: every call, and a bare "answer in Chinese" reads as just another style
#: tip sitting next to "prefer bullet points". Without an explicit note that
#: this is the user's own stated override, a model that also sees a
#: non-Chinese question has two instructions that look equally weighted and
#: no signal for which one wins. The ", an override" suffix is kept to three
#: words (not the fuller sentence a docstring can afford) because it competes
#: with ``domain_terms`` for the SAME ~200-char
#: :data:`SEARCH_PROFILE_BLOCK_MAX_CHARS` budget (see
#: :func:`render_style_block`'s own docstring) — measured against the
#: existing four-field coverage test (language+shape+detail+two short terms),
#: the phrase has only ~32 chars of headroom before it starts pushing a
#: person's own domain terms out of the block; a longer, more explanatory
#: phrase here would starve the one part of the block a person is most
#: likely to have filled with more than a couple of words.
_LANGUAGE_PHRASES = {
    "zh": "answer in Chinese, an override",
    "en": "answer in English, an override",
}
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


def _user_field_value(fields: Mapping[str, Any], field: str) -> Any:
    """Return ``field``'s value IFF its current stored origin is
    ``"user"`` — ``None`` for a missing entry AND for a present
    ``origin="job"`` entry alike.

    T9 fix round (main-agent ruling, "job 推断值 v1 不注入"): a T7-inferred
    value must never reach a model prompt on its own. This mirrors the P2
    experience library's own "attach the machinery, gate the injection
    behind its own switch until the behavior is validated" posture — except
    here there IS no separate injection switch to add, because the field's
    ``origin`` already carries exactly the bit this needs (``merge_field``
    never lets a job write launder over a user's own choice, so "origin is
    still job" already means "no person has looked at this value yet").
    Concretely: ``answer_language`` is the one field the T7 job can write,
    and an inferred language directly CONTRADICTS ``answer_prompt``'s rule 4
    ("Answer in the question's language") the instant it is wrong — a job
    that mis-classifies one noisy sample streak would silently start
    overriding a rule the user never asked to override. The settings UI
    still shows the inferred value (with an "inferred" badge) so a person
    can review and explicitly ACCEPT it — accepting is exactly the
    ``origin="user"`` write ``PATCH /me/search-profile`` already performs,
    which is what flips this predicate from ``False`` to ``True`` for that
    field. Nothing else about the document changes: the value stays exactly
    where the job wrote it, this function just refuses to hand it to
    :func:`render_style_block`'s caller until a person has taken ownership
    of it.
    """
    entry = fields.get(field)
    if not isinstance(entry, Mapping) or entry.get("origin") != "user":
        return None
    return entry.get("value")


def render_style_block(profile: Mapping[str, Any]) -> str:
    """Render ``profile``'s SET, USER-AUTHORED fields as one bounded English
    prompt line.

    This is model-facing scaffolding text (an internal prompt fragment), not
    user-facing UI copy — the UI-vocabulary guard does not apply to it. It
    carries ONLY organization/wording preferences: never a user id, notebook
    name, or source word (a reverse guard at the T8 injection surface
    asserts this structurally, mirroring the retrieval-experience action
    vocabulary's own scope-word guard).

    A field with no entry, whose value is the sentinel ``"auto"``, or whose
    CURRENT stored origin is ``"job"`` (see :func:`_user_field_value`) is
    silently omitted rather than rendered as "no preference" — an absent
    field, an explicit "auto", and an unreviewed job inference are all the
    same thing to a reader of the block: nothing a person has actually
    chosen.

    Bounded to :data:`SEARCH_PROFILE_BLOCK_MAX_CHARS`. ``domain_terms`` is
    the single most likely overflow source (it alone can carry up to 10 x 32
    characters against a ~200-char budget, dwarfing the other three fields
    combined), so it does NOT get dropped as one all-or-nothing chunk the way
    the other three fields do. Terms are packed in ONE AT A TIME and kept as
    long as they fit — a profile with ten terms and a 96-character remaining
    budget still surfaces however many whole terms fit, rather than losing
    every one of them because the full list didn't. Only once the term list
    is down to zero does the fallback continue dropping the other
    (single-sentence, rarely oversized) parts from the least-specific end —
    detail, then shape, then language — the same one-chunk-at-a-time
    trimming they always used. A term is never truncated mid-word: it is
    either whole in the block or entirely absent from it, but each term IS
    collapsed to one line before packing (:func:`app.services.
    agent_profile_block.collapse_prompt_line` — the same untrusted-free-text
    forgery concern that function's own docstring documents applies here
    unchanged: ``domain_terms`` is user-supplied free text, and a term
    containing a literal newline could otherwise forge a fake extra
    ``"; "``-joined clause, or even something that reads like a second
    ``[...]``-style block header, inside this single rendered prompt line).
    An empty profile (or one where nothing survives the budget) renders to
    ``""`` — a call site must treat that identically to "the feature never
    ran" (the historical, pre-feature call shape).
    """
    fields = profile.get("fields") or {}
    fixed_parts: list[str] = []

    language = _user_field_value(fields, "answer_language")
    if language and language != "auto" and language in _LANGUAGE_PHRASES:
        fixed_parts.append(_LANGUAGE_PHRASES[language])

    shape = _user_field_value(fields, "answer_shape")
    if shape and shape != "auto" and shape in _SHAPE_PHRASES:
        fixed_parts.append(_SHAPE_PHRASES[shape])

    detail = _user_field_value(fields, "answer_detail")
    if detail and detail != "auto" and detail in _DETAIL_PHRASES:
        fixed_parts.append(_DETAIL_PHRASES[detail])

    terms = _user_field_value(fields, "domain_terms")
    remaining_terms: list[str] = (
        [cleaned for term in terms if (cleaned := collapse_prompt_line(term))]
        if isinstance(terms, list)
        else []
    )

    while True:
        parts = list(fixed_parts)
        if remaining_terms:
            parts.append("familiar terms: " + ", ".join(remaining_terms))
        if not parts:
            return ""
        block = _BLOCK_PREAMBLE + "; ".join(parts) + "."
        if len(block) <= SEARCH_PROFILE_BLOCK_MAX_CHARS:
            return block
        if remaining_terms:
            # Drop exactly one term (the least-specific unit available) and
            # retry before ever touching a fixed part.
            remaining_terms.pop()
        else:
            fixed_parts.pop()  # detail, then shape, then language


