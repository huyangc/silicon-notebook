"""Reflective docs contract for the ``source.element_enricher`` extension point.

Same shape as ``test_gap_consult_docs_contract.py`` (T5 of the
``source.element_enricher`` implementation plan), scoped to this point's own
two numeric sources of truth:

1. The three structural ``*_MAX_*``/``*_DEPTH`` constants in
   ``app.domain.element_enrichment`` -- protocol bounds, not
   deployment-configurable -- must appear, on the very line that also names
   its own constant, inside the "Source element enrichment" section of BOTH
   ``docs/product-and-api.md`` and ``docs/product-and-api_zh.md``. As in the
   gap-consult contract, "same *table row*" (a line starting with ``"| "``)
   is what lets this catch a value that drifted out of its own table row even
   while surviving in prose elsewhere in the section.
2. The four ``source_element_enricher_*`` deployment settings in
   ``app.core.config.Settings`` -- read via ``Settings.model_fields[...]``,
   never re-typed -- must have their *default* documented the same way, AND
   their env var name (the field's ``validation_alias``) must appear in
   ``docs/deployment-and-configuration.md``, ``_zh.md``, and ``.env.example``.

Both guards reconcile the full set every run, not just whichever constant or
field this PR happened to touch.
"""
from __future__ import annotations

from pathlib import Path

import app.domain.element_enrichment as element_enrichment_domain
from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]

EN_SECTION = (
    "### Source element enrichment (`source.element_enricher`)",
    ("\n## ", "\n### "),
)
ZH_SECTION = (
    "### 来源元素补全（`source.element_enricher`）",
    ("\n## ", "\n### "),
)

# The four deployment-configurable settings this point registers, keyed by
# their ``Settings`` field name.
_SETTINGS_FIELDS = (
    "source_element_enricher_timeout_seconds",
    "source_element_enricher_max_proposals",
    "source_element_enricher_max_metadata_bytes",
    "source_element_enricher_max_description_chars",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _section(text: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    assert start_marker in text, f"section heading not found: {start_marker!r}"
    _, _, tail = text.partition(start_marker)
    cut = len(tail)
    for marker in end_markers:
        idx = tail.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return tail[:cut]


def _table_row_lines(section_text: str) -> list[str]:
    """Lines that are genuine Markdown table rows -- see the gap-consult
    contract test's identical helper for why this is stricter than "mentioned
    anywhere in the section"."""

    return [line for line in section_text.splitlines() if line.startswith("| ")]


def _domain_constants() -> dict[str, int]:
    """Every ``*_MAX_*``/``*_DEPTH`` module-level int constant -- re-derived
    from ``vars()`` rather than hand-listed, so a future constant is covered
    automatically."""

    return {
        name: value
        for name, value in vars(element_enrichment_domain).items()
        if name.isupper() and type(value) is int
    }


def _formatted_literal(value: int | float) -> str:
    """Mirrors this repo's docs convention (see the gap-consult contract
    test): an int >= 1,000 is comma-grouped; anything else is ``str()``."""

    if type(value) is int and value >= 1000:
        return f"{value:,}"
    return str(value)


def _row_has(text: str, name: str, literal: str) -> bool:
    """True when some table-row line in ``text`` names ``name`` and carries
    ``literal`` as a distinct token (not embedded in a longer digit run)."""

    for line in _table_row_lines(text):
        if name not in line:
            continue
        idx = line.find(literal)
        while idx != -1:
            before = line[idx - 1] if idx > 0 else ""
            after = line[idx + len(literal)] if idx + len(literal) < len(line) else ""
            if before not in "0123456789," and after not in "0123456789,":
                return True
            idx = line.find(literal, idx + 1)
    return False


def test_domain_constants_are_documented_in_english_product_docs():
    constants = _domain_constants()
    assert constants, "app.domain.element_enrichment exposes no *_MAX_*/_DEPTH constants"
    start, ends = EN_SECTION
    section = _section(_read("docs/product-and-api.md"), start, ends)
    missing = sorted(
        name
        for name, value in constants.items()
        if not _row_has(section, name, _formatted_literal(value))
    )
    assert not missing, (
        "docs/product-and-api.md's Source element enrichment section is "
        f"missing (or has drifted from) these constants, each expected on "
        f"the same table row as its own name: {missing}"
    )


def test_domain_constants_are_documented_in_chinese_product_docs():
    constants = _domain_constants()
    start, ends = ZH_SECTION
    section = _section(_read("docs/product-and-api_zh.md"), start, ends)
    missing = sorted(
        name
        for name, value in constants.items()
        if not _row_has(section, name, _formatted_literal(value))
    )
    assert not missing, (
        "docs/product-and-api_zh.md's 来源元素补全 section is missing (or has "
        f"drifted from) these constants, each expected on the same table "
        f"row as its own name: {missing}"
    )


def test_settings_defaults_are_documented_in_both_product_docs():
    for relative, (start, ends) in (
        ("docs/product-and-api.md", EN_SECTION),
        ("docs/product-and-api_zh.md", ZH_SECTION),
    ):
        section = _section(_read(relative), start, ends)
        for field in _SETTINGS_FIELDS:
            info = Settings.model_fields[field]
            default = info.default
            literal = _formatted_literal(default)
            alias = info.validation_alias
            assert type(alias) is str and alias, (
                f"Settings.{field} has no plain string validation_alias to "
                "check against the docs"
            )
            assert _row_has(section, alias, literal), (
                f"{relative} does not document {alias}'s default "
                f"({literal!r}) next to its own name, on its own table row"
            )


def test_settings_env_names_are_documented_in_deployment_reference():
    for relative in (
        "docs/deployment-and-configuration.md",
        "docs/deployment-and-configuration_zh.md",
        ".env.example",
    ):
        text = _read(relative)
        missing = sorted(
            Settings.model_fields[field].validation_alias
            for field in _SETTINGS_FIELDS
            if Settings.model_fields[field].validation_alias not in text
        )
        assert not missing, f"{relative} is missing these env var names: {missing}"
