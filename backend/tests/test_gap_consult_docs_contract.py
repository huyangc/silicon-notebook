"""Reflective docs contract for the ``ask.gap_consult`` extension point (X9 PR-A T4).

Two independent guards, both structural rather than hand-transcribed, so a
number or a table row can drift out of the docs without anyone noticing:

1. Every ``GAP_*`` numeric limit in ``app.domain.gap_consult`` -- this feature's
   one source of truth for its egress/admission rails -- must appear, on the
   very line that also names its own constant, inside the "Gap consultation"
   section of BOTH ``docs/product-and-api.md`` and
   ``docs/product-and-api_zh.md``. Requiring name-and-value on the SAME line
   (rather than checking each independently anywhere in the section) is what
   lets this tell "the number moved to a different doc" apart from "the
   number is still right here, next to its own name" -- a bare numeric
   coincidence elsewhere in the section does not make this pass.

2. Every ``*_POINT`` capability constant ``app.extension_sdk`` exports for the
   Protocol-based "other contribution kinds" table must appear in that exact
   table (SOP §3.5) in BOTH ``docs/deployment-extensions-sop.md`` and
   ``docs/deployment-extensions-sop_zh.md``. ``PLUGIN_HTTP_ROUTER_POINT`` is
   the one deliberate exclusion from that table: it registers through a
   different mechanism (at most one HTTP route contribution per plugin,
   documented in SOP §3.4, not the generic Provider/ProviderChain/
   Contributor/Observer Protocol table) -- but the exclusion is not a
   loophole a forgotten point could hide behind, because this file also
   asserts it is documented *somewhere* in the SOP.

Both guards reconcile the FULL set every run, not just the row this PR added
-- a future point that ships without its own doc row goes red here exactly
like a deleted ``ask.gap_consult`` row would.
"""
from __future__ import annotations

import re
from pathlib import Path

import app.domain.gap_consult as gap_consult_domain
import app.extension_sdk as extension_sdk
from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]

# (section start heading, headings that close the section) -- the same
# "from this marker to the next one of these" shape as
# ``test_architecture_documentation.py``'s ``_between`` helper, reimplemented
# locally so this contract file carries no import-time coupling to that
# module's unrelated document bundles.
GAP_CONSULT_EN_SECTION = (
    "### Gap consultation (`ask.gap_consult`)",
    ("\n## ", "\n### "),
)
GAP_CONSULT_ZH_SECTION = (
    "### 缺口外扩检索",
    ("\n## ", "\n### "),
)
SOP_TABLE_EN_SECTION = (
    "### 3.5 Other contribution kinds",
    ("\n### 3.6",),
)
SOP_TABLE_ZH_SECTION = (
    "### 3.5 其它 contribution 类型",
    ("\n### 3.6",),
)

# ``PLUGIN_HTTP_ROUTER_POINT`` is not a row in the §3.5 Protocol table by
# design -- see the module docstring.  Keeping the exclusion here, as a
# single named constant rather than an inline skip, is what a reviewer
# changing this set will actually see.
POINTS_OUTSIDE_THE_PROTOCOL_TABLE = frozenset({"PLUGIN_HTTP_ROUTER_POINT"})


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


def _gap_consult_constants() -> dict[str, int]:
    """Every ``GAP_*`` module-level constant -- the domain module's own
    ``__all__`` already names them all, but re-deriving the set from
    ``vars()`` means a newly added ``GAP_*`` constant is covered automatically
    without anyone remembering to extend a hand-written list here."""

    return {
        name: value
        for name, value in vars(gap_consult_domain).items()
        if name.startswith("GAP_") and type(value) is int
    }


def _formatted_int(value: int) -> str:
    """Mirrors how a value >= 1,000 is written in prose in this repo's docs
    (comma-grouped), so the guard matches the literal characters a reader
    sees rather than a Python ``str(int)`` rendering that would never equal
    the doc text for ``GAP_SUGGESTION_URL_MAX_CHARS`` (2,048)."""

    return f"{value:,}" if value >= 1000 else str(value)


def _row_has_literal(section_text: str, name: str, literal: str) -> bool:
    """True when some line inside ``section_text`` carries both ``name`` and
    ``literal`` -- e.g. a Markdown table row ``| `NAME` | 2,048 |``.  The
    numeric literal is bounded on both sides against a longer run of digits
    or thousands separators, so ``"4"`` cannot false-positive inside ``"40"``
    or ``"2,048"``."""

    pattern = re.compile(
        rf"^.*{re.escape(name)}.*(?<![\d,]){re.escape(literal)}(?![\d,]).*$",
        re.MULTILINE,
    )
    return pattern.search(section_text) is not None


def _missing_constants(section_text: str, constants: dict[str, int]) -> list[str]:
    return sorted(
        name
        for name, value in constants.items()
        if not _row_has_literal(section_text, name, _formatted_int(value))
    )


def test_every_gap_consult_constant_is_documented_in_english_product_docs():
    constants = _gap_consult_constants()
    assert constants, "app.domain.gap_consult exposes no GAP_* constants to check"
    start, ends = GAP_CONSULT_EN_SECTION
    section = _section(_read("docs/product-and-api.md"), start, ends)
    missing = _missing_constants(section, constants)
    assert not missing, (
        "docs/product-and-api.md's Gap consultation section is missing (or "
        f"has drifted from) these GAP_* values, each expected on the same "
        f"line as its own constant name: {missing}"
    )


def test_every_gap_consult_constant_is_documented_in_chinese_product_docs():
    constants = _gap_consult_constants()
    start, ends = GAP_CONSULT_ZH_SECTION
    section = _section(_read("docs/product-and-api_zh.md"), start, ends)
    missing = _missing_constants(section, constants)
    assert not missing, (
        "docs/product-and-api_zh.md's 缺口外扩检索 section is missing (or has "
        f"drifted from) these GAP_* values, each expected on the same line "
        f"as its own constant name: {missing}"
    )


def test_ask_gap_consult_timeout_default_is_documented_in_both_product_docs():
    """The deployment-configurable deadline default lives in ``Settings``, not
    in ``app.domain.gap_consult``, so it sits outside the ``GAP_*`` sweep
    above -- but it is the one other number this section's own contract table
    promises, and dropping it silently would leave that table's default
    column completely unchecked."""

    default = Settings.model_fields["ask_gap_consult_timeout_seconds"].default
    assert default == 4.0
    literal = "4.0"
    for relative, (start, ends) in (
        ("docs/product-and-api.md", GAP_CONSULT_EN_SECTION),
        ("docs/product-and-api_zh.md", GAP_CONSULT_ZH_SECTION),
    ):
        section = _section(_read(relative), start, ends)
        assert _row_has_literal(
            section, "ASK_GAP_CONSULT_TIMEOUT_SECONDS", literal
        ), (
            f"{relative} does not document the ASK_GAP_CONSULT_TIMEOUT_SECONDS "
            "default next to its own name"
        )


def _extension_point_constants() -> dict[str, str]:
    """Every ``*_POINT`` capability constant ``app.extension_sdk`` exports,
    the same live-registry-derived approach ``PUBLIC_TOOLS``/``CORE_TOOLS``
    use elsewhere in this repo: read from the module rather than re-typed
    here, so a future point is covered the moment it is exported."""

    return {
        name: getattr(extension_sdk, name)
        for name in dir(extension_sdk)
        if name.endswith("_POINT")
    }


def _missing_points(section_text: str, names: list[str]) -> list[str]:
    return sorted(name for name in names if name not in section_text)


def test_every_protocol_extension_point_is_in_the_english_sop_table():
    points = _extension_point_constants()
    assert points, "app.extension_sdk exposes no *_POINT constants to check"
    table_names = sorted(set(points) - POINTS_OUTSIDE_THE_PROTOCOL_TABLE)
    assert table_names, "no *_POINT constants belong in the Protocol table"
    start, ends = SOP_TABLE_EN_SECTION
    table = _section(_read("docs/deployment-extensions-sop.md"), start, ends)
    missing = _missing_points(table, table_names)
    assert not missing, (
        "docs/deployment-extensions-sop.md §3.5's contribution-point table is "
        f"missing these constants: {missing}"
    )


def test_every_protocol_extension_point_is_in_the_chinese_sop_table():
    points = _extension_point_constants()
    table_names = sorted(set(points) - POINTS_OUTSIDE_THE_PROTOCOL_TABLE)
    start, ends = SOP_TABLE_ZH_SECTION
    table = _section(_read("docs/deployment-extensions-sop_zh.md"), start, ends)
    missing = _missing_points(table, table_names)
    assert not missing, (
        "docs/deployment-extensions-sop_zh.md §3.5's contribution-point table "
        f"is missing these constants: {missing}"
    )


def test_the_http_router_point_exclusion_is_still_documented_elsewhere():
    """The one point deliberately excluded from the §3.5 table must not be
    forgotten outright -- it is asserted present in each SOP document as a
    whole (it lives in §3.4's code sample), so the exclusion above can never
    silently swallow a real omission."""

    for excluded in POINTS_OUTSIDE_THE_PROTOCOL_TABLE:
        assert excluded in _read("docs/deployment-extensions-sop.md"), (
            f"{excluded} is excluded from the §3.5 table but is not "
            "documented anywhere else in docs/deployment-extensions-sop.md"
        )
        assert excluded in _read("docs/deployment-extensions-sop_zh.md"), (
            f"{excluded} is excluded from the §3.5 table but is not "
            "documented anywhere else in docs/deployment-extensions-sop_zh.md"
        )
