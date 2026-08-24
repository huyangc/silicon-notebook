"""Reflective docs contract for the ``ask.gap_consult`` extension point (X9 PR-A T4).

Two independent guards, both structural rather than hand-transcribed, so a
number or a table row can drift out of the docs without anyone noticing:

1. Every ``GAP_*`` numeric limit in ``app.domain.gap_consult`` -- this feature's
   one source of truth for its egress/admission rails -- must appear, on the
   very line that also names its own constant, inside the "Gap consultation"
   section of BOTH ``docs/product-and-api.md`` and
   ``docs/product-and-api_zh.md``. Requiring name-and-value on the SAME
   *table row* (a line starting with ``"| "`` -- not merely anywhere in the
   section) is what lets this tell "the number moved to a different doc"
   apart from "the number is still right here, next to its own name": this
   section's prose also spells out some of these constants next to their
   values in long single-line paragraphs (e.g. the "Budget and fail-open"
   paragraph reads "...`ASK_GAP_CONSULT_TIMEOUT_SECONDS`, default 4.0s..."),
   so a check that is not restricted to table rows can go on passing off a
   surviving prose mention even after the *table's own* value has drifted --
   the row is the one place a reader actually looks up a bound.
   One known, accepted exemption this sweep does not (and structurally
   cannot) pin: `_JOIN_SLICE_SECONDS` -- the 50ms join-slice value quoted in
   product-and-api.md's "joined in 50ms slices" prose -- lives in
   ``app/extensions/gap_consult.py`` (the host), not in
   ``app.domain.gap_consult`` (the module this sweep reflects), so it never
   enters ``_gap_consult_constants()`` and is not guarded here.

2. Every ``*_POINT`` capability constant ``app.extension_sdk`` exports for the
   Protocol-based "other contribution kinds" table must appear, as a genuine
   *table row* (again a line starting with ``"| "``), in that exact table
   (SOP §3.5) in BOTH ``docs/deployment-extensions-sop.md`` and
   ``docs/deployment-extensions-sop_zh.md``. Beyond mere presence, each row's
   Kind, Protocol and Module columns are cross-checked against live
   reflection of ``app.extension_sdk``/``app.extensions`` wherever a
   reflection is actually available -- see ``_defining_sdk_submodule`` and
   ``_reflect_declared_kind`` for what is and is not derivable this way; a
   point this file cannot reflect a Kind for is skipped for that one column,
   not asserted on thin air. The table's own row *count* is separately
   pinned to the section's spelled-out number word (EN "six" / ZH "六"),
   because "the docs list every point" and "the docs correctly say how many
   points there are" are two different failure modes -- the second one is
   exactly the off-by-one this guard was hardened after (§3.5's intro
   sentence used to say "five" over a six-row table).
   ``PLUGIN_HTTP_ROUTER_POINT`` is the one deliberate exclusion from that
   table: it registers through a different mechanism (at most one HTTP route
   contribution per plugin, documented in SOP §3.4, not the generic
   Provider/ProviderChain/Contributor/Observer Protocol table) -- but the
   exclusion is not a loophole a forgotten point could hide behind, because
   this file also asserts it is documented *somewhere* in the SOP.

Both guards reconcile the FULL set every run, not just the row this PR added
-- a future point that ships without its own doc row goes red here exactly
like a deleted ``ask.gap_consult`` row would.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import app.domain.gap_consult as gap_consult_domain
import app.extension_sdk as extension_sdk
import app.extensions as extensions_package
from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"

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

# Spelled-out row-count words, EN/ZH, keyed by the live-reflected point
# count.  A small hand-typed dictionary is fine here: this is the guard's own
# vocabulary for asserting against prose, not a product-facing number that
# could drift out of docs unnoticed (see module docstring item 2).
_COUNT_WORDS_EN = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}
_COUNT_WORDS_ZH = {
    1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
    6: "六", 7: "七", 8: "八", 9: "九", 10: "十",
}

# How far past one occurrence of a ``*_POINT`` name to look for a paired
# ``ContributionKind.<X>`` reference when reflecting a table row's Kind
# column -- see ``_reflect_declared_kind``.
_KIND_SEARCH_WINDOW_CHARS = 500


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
    surviving mention in running text even after the table itself drifted --
    see the module docstring for a concrete example.
    """

    return [line for line in section_text.splitlines() if line.startswith("| ")]


def _gap_consult_constants() -> dict[str, int | float]:
    """Every ``GAP_*`` module-level constant -- the domain module's own
    ``__all__`` already names them all, but re-deriving the set from
    ``vars()`` means a newly added ``GAP_*`` constant is covered automatically
    without anyone remembering to extend a hand-written list here.

    ``int | float`` (not just ``int``): a future budget expressed as a ratio
    or a fractional-second value is just as much a "numeric limit" as an
    integer character cap, and ``type(value) in (int, float)`` structurally
    excludes ``bool`` (``type(True) is bool``, never ``int`` or ``float``,
    even though ``bool`` subclasses ``int``) without an explicit carve-out.
    """

    return {
        name: value
        for name, value in vars(gap_consult_domain).items()
        if name.startswith("GAP_") and type(value) in (int, float)
    }


def _formatted_literal(value: int | float) -> str:
    """Mirrors how a value is written in prose in this repo's docs: an int
    ``>= 1,000`` is comma-grouped (``GAP_SUGGESTION_URL_MAX_CHARS``, 2,048);
    anything else -- including every float, since none of this section's
    docs comma-group a decimal -- is written via plain ``str()``."""

    if type(value) is int and value >= 1000:
        return f"{value:,}"
    return str(value)


def _row_has_literal(section_text: str, name: str, literal: str) -> bool:
    """True when some *table row* line (one starting with ``"| "``) inside
    ``section_text`` carries both ``name`` and ``literal`` -- e.g. a Markdown
    table row ``| `NAME` | 2,048 |``.  The numeric literal is bounded on both
    sides against a longer run of digits or thousands separators, so ``"4"``
    cannot false-positive inside ``"40"`` or ``"2,048"``."""

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


def test_every_gap_consult_constant_is_documented_in_english_product_docs():
    constants = _gap_consult_constants()
    assert constants, "app.domain.gap_consult exposes no GAP_* constants to check"
    start, ends = GAP_CONSULT_EN_SECTION
    section = _section(_read("docs/product-and-api.md"), start, ends)
    missing = _missing_constants(section, constants)
    assert not missing, (
        "docs/product-and-api.md's Gap consultation section is missing (or "
        f"has drifted from) these GAP_* values, each expected on the same "
        f"table row as its own constant name: {missing}"
    )


def test_every_gap_consult_constant_is_documented_in_chinese_product_docs():
    constants = _gap_consult_constants()
    start, ends = GAP_CONSULT_ZH_SECTION
    section = _section(_read("docs/product-and-api_zh.md"), start, ends)
    missing = _missing_constants(section, constants)
    assert not missing, (
        "docs/product-and-api_zh.md's 缺口外扩检索 section is missing (or has "
        f"drifted from) these GAP_* values, each expected on the same table "
        f"row as its own constant name: {missing}"
    )


def test_ask_gap_consult_timeout_default_is_documented_in_both_product_docs():
    """The deployment-configurable deadline default lives in ``Settings``, not
    in ``app.domain.gap_consult``, so it sits outside the ``GAP_*`` sweep
    above -- but it is the one other number this section's own contract table
    promises, and dropping it silently would leave that table's default
    column completely unchecked."""

    default = Settings.model_fields["ask_gap_consult_timeout_seconds"].default
    assert default == 4.0
    # Derived, not re-typed: a second hand-written "4.0" next to the real
    # default is exactly the kind of stale-copy risk this file exists to
    # catch everywhere else.
    literal = _formatted_literal(default)
    for relative, (start, ends) in (
        ("docs/product-and-api.md", GAP_CONSULT_EN_SECTION),
        ("docs/product-and-api_zh.md", GAP_CONSULT_ZH_SECTION),
    ):
        section = _section(_read(relative), start, ends)
        assert _row_has_literal(
            section, "ASK_GAP_CONSULT_TIMEOUT_SECONDS", literal
        ), (
            f"{relative} does not document the ASK_GAP_CONSULT_TIMEOUT_SECONDS "
            "default next to its own name, on its own table row"
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
    rows_text = "\n".join(_table_row_lines(section_text))
    return sorted(name for name in names if name not in rows_text)


def _parse_protocol_table(section_text: str) -> dict[str, dict[str, str]]:
    """Parse §3.5's ``| Point | Kind | Protocol | Module |`` rows into
    ``{point_name: {"kind": ..., "protocol": ..., "module": ...}}``.

    Only lines that are genuine table rows (``_table_row_lines``) and that
    parse into exactly four cells, each carrying a backtick-quoted token in
    the shape this table actually uses, become an entry -- the header row and
    the ``| --- | --- |`` separator carry no backticks in their first cell
    and are silently skipped, not misparsed.
    """

    rows: dict[str, dict[str, str]] = {}
    for line in _table_row_lines(section_text):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            continue
        point_match = re.search(r"`([A-Za-z0-9_]+_POINT)`", cells[0])
        kind_match = re.search(r"`([A-Za-z0-9_]+)`", cells[1])
        protocol_match = re.search(r"`([A-Za-z0-9_]+)`", cells[2])
        module_match = re.search(r"`([\w./]+)`", cells[3])
        if not (point_match and kind_match and protocol_match and module_match):
            continue
        rows[point_match.group(1)] = {
            "kind": kind_match.group(1),
            "protocol": protocol_match.group(1),
            "module": module_match.group(1),
        }
    return rows


def _protocol_names(module: object) -> frozenset[str]:
    return frozenset(
        name
        for name, obj in vars(module).items()
        if isinstance(obj, type) and getattr(obj, "_is_protocol", False)
    )


def _defining_sdk_submodule(point_name: str, point_value: str):
    """The one ``app.extension_sdk.<x>`` submodule whose own namespace
    defines this ``*_POINT`` constant.

    Matched by *value* rather than by name lookup on the aggregator package:
    the constant itself carries no ``__module__`` (it is a plain ``str``), so
    the only way to find where it is really defined is to check each
    submodule's own namespace for it -- the same "read from where it's
    actually defined" spirit as ``_gap_consult_constants()`` above, one layer
    deeper into the SDK.
    """

    for info in pkgutil.iter_modules(extension_sdk.__path__):
        submodule = importlib.import_module(f"app.extension_sdk.{info.name}")
        if getattr(submodule, point_name, None) == point_value:
            return submodule
    return None


def _reflect_declared_kind(point_name: str) -> str | None:
    """Best-effort: find where this point's own constant and its
    ``ContributionKind`` are written in the same breath somewhere under
    ``app.extensions`` -- a built-in contributor's own
    ``ContributionDeclaration(..., point=X, kind=ContributionKind.Y)``, or a
    point's dedicated Host re-validating the kind of what got registered --
    and read the kind name off of that pairing.

    Returns ``None`` when no such pairing is discoverable by this static
    scan, in which case the caller skips that one row's Kind check rather
    than assert against nothing.  This is expected, not a bug in the scan:
    ``RETRIEVAL_CONTRIBUTOR_POINT``'s own dedicated host
    (``app/extensions/retrieval.py``) does not re-check
    ``ContributionKind`` itself -- it leans entirely on the registry's own
    generic ``add_contributor`` gate at registration time -- so for that one
    point this reflects a *built-in contributor's* declaration
    (``app/extensions/builtin/*.py``) instead, which is exactly why the scan
    walks the whole ``app.extensions`` tree rather than one fixed file per
    point.
    """

    root = Path(extensions_package.__file__).resolve().parent
    kind_pattern = re.compile(r"ContributionKind\.(\w+)")
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for point_match in re.finditer(re.escape(point_name), text):
            window = text[point_match.end() : point_match.end() + _KIND_SEARCH_WINDOW_CHARS]
            kind_match = kind_pattern.search(window)
            if kind_match:
                return kind_match.group(1)
    return None


def _assert_table_rows_match_reflection(section_text: str, relative_doc: str) -> None:
    """Cross-check each §3.5 row's Kind/Protocol/Module columns against live
    reflection wherever a reflection is available (see
    ``_defining_sdk_submodule``/``_reflect_declared_kind`` for what is and is
    not derivable this way)."""

    points = _extension_point_constants()
    table_names = sorted(set(points) - POINTS_OUTSIDE_THE_PROTOCOL_TABLE)
    rows = _parse_protocol_table(section_text)
    for name in table_names:
        row = rows.get(name)
        assert row is not None, (
            f"{relative_doc} §3.5 has no parseable table row for {name!r}"
        )
        submodule = _defining_sdk_submodule(name, points[name])
        assert submodule is not None, (
            "could not find the app.extension_sdk submodule that actually "
            f"defines {name!r} (value {points[name]!r}) to check "
            f"{relative_doc} against"
        )
        module_path = (
            Path(submodule.__file__).resolve().relative_to(BACKEND_ROOT).as_posix()
        )
        assert row["module"] == module_path, (
            f"{relative_doc} §3.5 lists {name!r}'s Module as "
            f"{row['module']!r}, but it is actually defined in "
            f"{module_path!r}"
        )
        assert row["protocol"] in _protocol_names(submodule), (
            f"{relative_doc} §3.5 lists {name!r}'s Protocol as "
            f"{row['protocol']!r}, but no such Protocol class is defined in "
            f"{module_path!r}"
        )
        reflected_kind = _reflect_declared_kind(name)
        if reflected_kind is None:
            # Not every point's Kind is discoverable by this static scan --
            # see _reflect_declared_kind's docstring.  Skip this one row's
            # Kind check rather than assert against nothing.
            continue
        assert row["kind"] == reflected_kind, (
            f"{relative_doc} §3.5 lists {name!r}'s Kind as {row['kind']!r}, "
            f"but app.extensions pairs it with "
            f"ContributionKind.{reflected_kind} instead"
        )


def _assert_extension_point_count_is_documented(
    section_text: str, table_names: list[str], *, relative_doc: str, chinese: bool
) -> None:
    """Pair §3.5's spelled-out row count with the table's *actual* parsed row
    count -- not merely with the live-reflected point count -- so a stray or
    a missing row goes red here exactly like the wording bug this guard was
    hardened after (§3.5's intro sentence once said "five" over a table that
    had always had six rows)."""

    rows = _parse_protocol_table(section_text)
    expected = len(table_names)
    assert len(rows) == expected, (
        f"{relative_doc} §3.5's table has {len(rows)} parseable row(s) but "
        f"the live SDK registers {expected} Protocol-table point(s) "
        f"({table_names}) -- a stray or a missing row"
    )
    words = _COUNT_WORDS_ZH if chinese else _COUNT_WORDS_EN
    word = words.get(expected)
    assert word is not None, (
        f"no spelled-out number word registered for {expected} rows; extend "
        "_COUNT_WORDS_EN/_COUNT_WORDS_ZH"
    )
    pattern = (
        rf"其余{word}个生产扩展点"
        if chinese
        else rf"The {word} remaining production extension points"
    )
    assert re.search(pattern, section_text), (
        f"{relative_doc} §3.5's intro sentence does not spell the row count "
        f"as {word!r} ({expected} table rows)"
    )


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
        f"missing these constants as genuine table rows: {missing}"
    )
    _assert_table_rows_match_reflection(table, "docs/deployment-extensions-sop.md")
    _assert_extension_point_count_is_documented(
        table,
        table_names,
        relative_doc="docs/deployment-extensions-sop.md",
        chinese=False,
    )


def test_every_protocol_extension_point_is_in_the_chinese_sop_table():
    points = _extension_point_constants()
    table_names = sorted(set(points) - POINTS_OUTSIDE_THE_PROTOCOL_TABLE)
    start, ends = SOP_TABLE_ZH_SECTION
    table = _section(_read("docs/deployment-extensions-sop_zh.md"), start, ends)
    missing = _missing_points(table, table_names)
    assert not missing, (
        "docs/deployment-extensions-sop_zh.md §3.5's contribution-point table "
        f"is missing these constants as genuine table rows: {missing}"
    )
    _assert_table_rows_match_reflection(
        table, "docs/deployment-extensions-sop_zh.md"
    )
    _assert_extension_point_count_is_documented(
        table,
        table_names,
        relative_doc="docs/deployment-extensions-sop_zh.md",
        chinese=True,
    )


def test_the_http_router_point_exclusion_is_still_documented_elsewhere():
    """The one point deliberately excluded from the §3.5 table must not be
    forgotten outright -- it is asserted present in each SOP document as a
    whole (it lives in §3.1's minimal-bundle sample,
    docs/deployment-extensions-sop.md:74,80 -- the mechanism contract itself
    is written up in §3.4), so the exclusion above can never silently
    swallow a real omission."""

    for excluded in POINTS_OUTSIDE_THE_PROTOCOL_TABLE:
        assert excluded in _read("docs/deployment-extensions-sop.md"), (
            f"{excluded} is excluded from the §3.5 table but is not "
            "documented anywhere else in docs/deployment-extensions-sop.md"
        )
        assert excluded in _read("docs/deployment-extensions-sop_zh.md"), (
            f"{excluded} is excluded from the §3.5 table but is not "
            "documented anywhere else in docs/deployment-extensions-sop_zh.md"
        )
