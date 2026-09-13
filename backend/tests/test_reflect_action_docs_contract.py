"""Reflective docs contract for the ``ask.reflect_action`` extension point (T6).

Mirrors ``test_gap_consult_docs_contract.py``'s shape and rationale exactly,
narrowed to what this feature's design document (T6, §十) actually asks for:

1. Every ``REFLECT_ACTION_*``/``EXTERNAL_EVIDENCE_*`` numeric constant in
   ``app.domain.reflect_action`` -- this feature's one source of truth for its
   descriptor and result-side rails -- must appear, on the very table row that
   also names its own constant, inside the "Reflect plugin actions" section of
   BOTH ``docs/product-and-api.md`` and ``docs/product-and-api_zh.md``.
   Requiring name-and-value on the SAME *table row* (a line starting with
   ``"| "`` -- not merely anywhere in the section) is what stops a number that
   drifted out of the table from hiding behind a stale prose mention -- the
   row is the one place a reader actually looks a bound up (see
   ``test_gap_consult_docs_contract.py``'s docstring for the same argument
   made in more detail against this repository's own long-single-line-prose
   convention).

2. The three deployment settings this feature introduces
   (``REASONING_MAX_PLUGIN_ACTIONS``, ``REASONING_PLUGIN_ACTION_TIMEOUT_SECONDS``,
   ``EXTERNAL_EVIDENCE_MAX_PER_RUN``) live in ``Settings``, not in the ``domain``
   module's numeric-rail sweep above, so they get their own check: each
   setting's live default (read off ``Settings.model_fields``, never
   re-typed) must appear next to its own name on a table row in both docs.

3. The six stable skip-step reason codes ``_action_plugin`` can write into a
   trace step's ``detail.reason`` (a small, closed, hand-kept list --
   deliberately not reflected off source, because there is no single
   enumerated type to reflect them from; a seventh reason added to the
   dispatch chain without updating this list is exactly the drift this test
   exists to catch) must each be mentioned somewhere in the "Reflect plugin
   actions" section of both product docs.

4. The point id ``ask.reflect_action`` must be mentioned in the
   ``ReflectActionContributor`` red-line guidance this PR adds to §3.6 of both
   ``docs/deployment-extensions-sop.md`` and ``docs/deployment-extensions-sop_zh.md``
   -- the SOP's §3.5 table row itself (Kind/Protocol/Module) is already fully
   covered by ``test_gap_consult_docs_contract.py``'s generic ``*_POINT`` sweep,
   which iterates every capability constant ``app.extension_sdk`` exports, so
   this file does not re-derive that table.

Both numeric sweeps reconcile the FULL constant/setting set every run, not
just the ones this PR happened to add -- a future ``REFLECT_ACTION_*`` or
``EXTERNAL_EVIDENCE_*`` constant that ships without its own doc row goes red
here exactly like a deleted one would.
"""
from __future__ import annotations

import re
from pathlib import Path

import app.domain.reflect_action as reflect_action_domain
from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]

REFLECT_ACTION_EN_SECTION = (
    "### Reflect plugin actions (`ask.reflect_action`)",
    ("\n## ", "\n### "),
)
REFLECT_ACTION_ZH_SECTION = (
    "### Reflect 插件动作（`ask.reflect_action`）",
    ("\n## ", "\n### "),
)

# Setting name -> Settings field name, so the live default is read off the
# model rather than re-typed as a second hand-written literal next to it.
REFLECT_ACTION_SETTINGS = {
    "REASONING_MAX_PLUGIN_ACTIONS": "reasoning_max_plugin_actions",
    "REASONING_PLUGIN_ACTION_TIMEOUT_SECONDS": "reasoning_plugin_action_timeout_seconds",
    "EXTERNAL_EVIDENCE_MAX_PER_RUN": "external_evidence_max_per_run",
}

# The closed set of stable reason codes ``_action_plugin``
# (``backend/app/services/reasoning_retrieval.py``) writes into a skip step's
# ``detail.reason`` -- see the module docstring for why this is a hand-kept
# list rather than a reflected one.
REFLECT_ACTION_SKIP_REASONS = (
    "plugin_action_disabled",
    "plugin_action_missing_argument",
    "plugin_action_cap",
    "duplicate_plugin_action",
    "plugin_action_last_turn",
    "plugin_action_failed",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _section(text: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    """Text from ``start_marker`` up to the first following occurrence of any
    marker in ``end_markers`` (or end of file if none occurs)."""

    assert start_marker in text, f"section heading not found: {start_marker!r}"
    _, _, tail = text.partition(start_marker)
    cut = len(tail)
    for marker in end_markers:
        idx = tail.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return tail[:cut]


def _table_row_lines(section_text: str) -> list[str]:
    """Lines that are genuine Markdown table rows: they start with ``"| "``.

    Prose in this repo's docs is written as long single-line paragraphs, so a
    check that does not restrict itself to this shape can be satisfied by a
    surviving mention in running text even after the table itself drifted.
    """

    return [line for line in section_text.splitlines() if line.startswith("| ")]


def _reflect_action_constants() -> dict[str, int | float]:
    """Every ``REFLECT_ACTION_*``/``EXTERNAL_EVIDENCE_*`` module-level
    constant -- re-derived from ``vars()`` rather than a hand-written list, so
    a newly added constant is covered automatically.

    ``int | float`` (not just ``int``): ``type(value) in (int, float)``
    structurally excludes ``bool`` (``type(True) is bool``, never ``int`` or
    ``float``, even though ``bool`` subclasses ``int``) without an explicit
    carve-out, and admits a future fractional-second rail the same way
    ``test_gap_consult_docs_contract.py`` does.
    """

    return {
        name: value
        for name, value in vars(reflect_action_domain).items()
        if (name.startswith("REFLECT_ACTION_") or name.startswith("EXTERNAL_EVIDENCE_"))
        and type(value) in (int, float)
    }


def _formatted_literal(value: int | float) -> str:
    """Mirrors how a value is written in prose in this repo's docs: an int
    ``>= 1,000`` is comma-grouped (``EXTERNAL_EVIDENCE_URL_MAX_CHARS``,
    2,048); anything else is written via plain ``str()``."""

    if type(value) is int and value >= 1000:
        return f"{value:,}"
    return str(value)


def _row_has_literal(section_text: str, name: str, literal: str) -> bool:
    """True when some *table row* line (one starting with ``"| "``) inside
    ``section_text`` carries both ``name`` and ``literal``. The numeric
    literal is bounded on both sides against a longer run of digits or
    thousands separators, so ``"4"`` cannot false-positive inside ``"40"`` or
    ``"2,048"``."""

    pattern = re.compile(
        rf"^\| .*{re.escape(name)}.*(?<![\d,]){re.escape(literal)}(?![\d,]).*$",
        re.MULTILINE,
    )
    return pattern.search(section_text) is not None


def _missing_constants(
    section_text: str, constants: dict[str, int | float]
) -> list[str]:
    return sorted(
        name
        for name, value in constants.items()
        if not _row_has_literal(section_text, name, _formatted_literal(value))
    )


def test_every_reflect_action_constant_is_documented_in_english_product_docs():
    constants = _reflect_action_constants()
    assert constants, (
        "app.domain.reflect_action exposes no REFLECT_ACTION_*/EXTERNAL_EVIDENCE_* "
        "constants to check"
    )
    start, ends = REFLECT_ACTION_EN_SECTION
    section = _section(_read("docs/product-and-api.md"), start, ends)
    missing = _missing_constants(section, constants)
    assert not missing, (
        "docs/product-and-api.md's Reflect plugin actions section is missing "
        "(or has drifted from) these values, each expected on the same table "
        f"row as its own constant name: {missing}"
    )


def test_every_reflect_action_constant_is_documented_in_chinese_product_docs():
    constants = _reflect_action_constants()
    start, ends = REFLECT_ACTION_ZH_SECTION
    section = _section(_read("docs/product-and-api_zh.md"), start, ends)
    missing = _missing_constants(section, constants)
    assert not missing, (
        "docs/product-and-api_zh.md's Reflect 插件动作 section is missing (or "
        f"has drifted from) these values, each expected on the same table row "
        f"as its own constant name: {missing}"
    )


def test_every_reflect_action_setting_default_is_documented_in_both_product_docs():
    """The three deployment settings live in ``Settings``, not in the domain
    module's numeric-rail sweep above, so they get their own reflective check
    -- but they are exactly the other numbers this section's own contract
    table promises, and dropping them silently would leave that table's
    default column completely unchecked."""

    for setting_name, field_name in REFLECT_ACTION_SETTINGS.items():
        default = Settings.model_fields[field_name].default
        literal = _formatted_literal(default)
        for relative, (start, ends) in (
            ("docs/product-and-api.md", REFLECT_ACTION_EN_SECTION),
            ("docs/product-and-api_zh.md", REFLECT_ACTION_ZH_SECTION),
        ):
            section = _section(_read(relative), start, ends)
            assert _row_has_literal(section, setting_name, literal), (
                f"{relative} does not document {setting_name}'s default "
                f"({literal!r}) next to its own name, on its own table row"
            )


def test_every_skip_reason_is_mentioned_in_both_product_docs():
    for relative, (start, ends) in (
        ("docs/product-and-api.md", REFLECT_ACTION_EN_SECTION),
        ("docs/product-and-api_zh.md", REFLECT_ACTION_ZH_SECTION),
    ):
        section = _section(_read(relative), start, ends)
        missing = [
            reason for reason in REFLECT_ACTION_SKIP_REASONS if reason not in section
        ]
        assert not missing, (
            f"{relative}'s Reflect plugin actions section does not mention "
            f"these skip reasons: {missing}"
        )


# Phrases the §3.6 red-line bullet itself must carry -- not merely appear
# somewhere in the file, which the §3.5 table row alone already satisfies
# (that row spells out both "ReflectActionContributor" and "ask.reflect_action"
# in its Point/Protocol columns, so a whole-file ``in`` check is a false
# positive: it would still pass if the §3.6 bullet were deleted outright).
# One phrase per load-bearing fact a plugin author needs from this specific
# bullet: the return type (not a raw tuple, not ``ContributorResult``), the
# deadline setting's name, and the URL scheme restriction.
REFLECT_ACTION_SOP_BULLET_PHRASES = (
    "ReflectActionResult",
    "REASONING_PLUGIN_ACTION_TIMEOUT_SECONDS",
    "http",
)


def _sop_bullet_lines(text: str) -> list[str]:
    """Lines that are genuine SOP bullet points: they start with ``"- "``.

    Mirrors ``_table_row_lines``'s reasoning above for table rows: this repo's
    SOP prose is written as long single-line paragraphs too, so scoping to the
    bullet's own line (rather than the whole file, or even the whole §3.6
    section) is what stops a mention anywhere else in the document from
    standing in for the bullet actually existing.
    """

    return [line for line in text.splitlines() if line.startswith("- ")]


def test_reflect_action_point_is_documented_in_both_sop_backend_red_lines():
    """The ``ReflectActionContributor`` §3.6 red-line bullet itself -- not
    merely the point id or the class name appearing anywhere in the file,
    which the §3.5 table row alone already satisfies -- must exist in both
    SOP documents and carry the load-bearing facts a plugin author needs
    straight from it. The §3.5 table row's own Kind/Protocol/Module columns
    are already fully covered by ``test_gap_consult_docs_contract.py``'s
    generic ``*_POINT`` sweep, so this only pins the guidance prose this file
    is responsible for.

    Verified by mutation (documented in the session/PR report, not run
    automatically here — deleting a line from a tracked doc mid-suite would
    leave the repo dirty if the run were interrupted before the line is
    restored): with the English bullet's line removed, this test fails with
    "docs/deployment-extensions-sop.md has no §3.6 bullet line naming
    ReflectActionContributor"; restoring the line makes it pass again. Before
    this fix, the equivalent whole-file ``"ReflectActionContributor" in text``
    check passed even with that line removed, satisfied entirely by the §3.5
    table row's own Point/Protocol columns — a false-positive guard.
    """

    for relative in (
        "docs/deployment-extensions-sop.md",
        "docs/deployment-extensions-sop_zh.md",
    ):
        text = _read(relative)
        bullets = [
            line for line in _sop_bullet_lines(text)
            if "ReflectActionContributor" in line
        ]
        assert bullets, (
            f"{relative} has no §3.6 bullet line naming ReflectActionContributor "
            "(a mention elsewhere in the file, such as the §3.5 table row, "
            "does not satisfy this -- see the module docstring)"
        )
        bullet = bullets[0]
        missing = [
            phrase for phrase in REFLECT_ACTION_SOP_BULLET_PHRASES
            if phrase not in bullet
        ]
        assert not missing, (
            f"{relative}'s ReflectActionContributor bullet is missing: "
            f"{missing}"
        )
