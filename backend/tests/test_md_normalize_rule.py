import json, pathlib, re
import pytest
from app.services.knowhow.md_normalize import (
    rule_normalize,
    classify_line,
    BULLET_GLYPHS,
    content_invariant,
    is_rich_markdown,
)

_GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "knowhow_normalize_golden.json").read_text("utf-8"))

@pytest.mark.parametrize("case", _GOLDEN, ids=[c["name"] for c in _GOLDEN])
def test_rule_normalize_golden(case):
    out = rule_normalize(case["raw"])
    lines = out.split("\n")
    for needle in case["expect_contains"]:
        assert needle in lines, f"{case['name']}: expected line {needle!r} in:\n{out}"
    for absent in case["expect_absent"]:
        assert absent not in out, f"{case['name']}: {absent!r} should be gone in:\n{out}"
    # I2 groundwork: every golden case also pins the FULL exact output string
    # (not just contains/absent) -- a later task ports this same field to the
    # TypeScript side so both implementations are machine-checked for parity.
    assert out == case["expect_exact"], f"{case['name']}: exact mismatch:\n{out!r}\n!=\n{case['expect_exact']!r}"

def test_no_leading_tab_ever():
    out = rule_normalize("\t• a\n\t\tb. nested")
    assert not any(l.startswith("\t") for l in out.split("\n"))

def test_bullet_glyph_becomes_dash():
    assert rule_normalize("• foo").split("\n")[0] == "- foo"

def test_section_header_alpha_at_col0_is_bolded():
    assert "**A. 考量**" in rule_normalize("A. 考量\n\t• x").split("\n")

def test_never_raises_on_garbage():
    # must return a str, never throw, even on pathological input
    assert isinstance(rule_normalize("\t\t\t)(*&^%\n\x00\n•••"), str)

def test_empty_stays_empty():
    assert rule_normalize("") == ""
    assert rule_normalize("   \n  ") == ""

def test_idempotent():
    once = rule_normalize(_GOLDEN[0]["raw"])
    assert rule_normalize(once) == once


# ---------------------------------------------------------------------------
# C1 (Critical, historical): fenced code blocks / markdown tables must pass
# through byte-identically -- before this fix, every line inside a ``` fence
# (or a `|`-table) was independently classified as a prose line, stripping
# its indentation and injecting a blank line between every single line (the
# same "two prose lines stay separate" rule that's correct for actual prose
# paragraphs, but catastrophic for code/table interiors).
#
# Since the architectural fix above, these three scenarios are caught by
# `is_rich_markdown` before a single line gets classified -- the whole cell
# now comes back byte-identical to `raw`, not merely "verbatim interior with
# reformatted padding around it" the way the old `_find_verbatim_spans`
# mechanism produced. Each test below now also pins that stronger claim
# (`out == raw`) and asserts the gate is what's actually doing the work,
# while keeping the original interior-preservation assertions so this still
# fails loudly if a future change reintroduces line-by-line reformatting
# for these shapes.
# ---------------------------------------------------------------------------


def test_fenced_code_block_interior_preserved_byte_for_byte():
    raw = (
        "说明文字\n"
        "```python\n"
        "def f():\n"
        "\tif x:\n"
        "\t\treturn 1\n"
        "```\n"
        "结束"
    )
    assert is_rich_markdown(raw) is True
    out = rule_normalize(raw)
    assert out == raw
    # the fence's own interior lines (including their tab indentation) must
    # appear verbatim, unindented/unsplit/unseparated by injected blank lines.
    assert "def f():\n\tif x:\n\t\treturn 1" in out
    assert "```python" in out.split("\n")
    # closing fence immediately follows the last code line -- no blank line
    # was injected INSIDE the fence.
    lines = out.split("\n")
    close_idx = lines.index("```", lines.index("```python") + 1)
    assert lines[close_idx - 1] == "\t\treturn 1"


def test_markdown_table_preserved_byte_for_byte():
    raw = "说明\n| A | B |\n| - | - |\n| 1 | 2 |\n结束"
    assert is_rich_markdown(raw) is True
    out = rule_normalize(raw)
    assert out == raw
    lines = out.split("\n")
    # all three table rows survive verbatim, as a contiguous run (not
    # scattered/blank-line-separated the way independent prose lines would be).
    idx = lines.index("| A | B |")
    assert lines[idx:idx + 3] == ["| A | B |", "| - | - |", "| 1 | 2 |"]


def test_unclosed_fence_runs_to_end_of_input_untouched():
    raw = "开头\n```\ncode line 1\n\tcode line 2 indented"
    assert is_rich_markdown(raw) is True
    out = rule_normalize(raw)
    assert out == raw
    # no closing fence in the input -- the remainder must be treated as
    # inside the fence (preserved as-is) rather than reformatted as prose.
    assert "code line 1\n\tcode line 2 indented" in out
    assert out.endswith("code line 2 indented")


def test_invariant_holds_for_normalized_fence_and_table():
    """The whole point of the C1 root fix (now subsumed by the gate): since
    rule_normalize no longer mangles fence/table interiors,
    content_invariant(raw, rule_normalize(raw)) must now hold for cells
    containing them (previously it would have correctly REJECTED the
    mangled output -- but the real bug was that the mangled,
    content-destroying output was what got saved to the database in the
    first place)."""
    fence_raw = "说明文字\n```python\ndef f():\n\tif x:\n\t\treturn 1\n```\n结束"
    assert content_invariant(fence_raw, rule_normalize(fence_raw)) is True

    table_raw = "说明\n| A | B |\n| - | - |\n| 1 | 2 |\n结束"
    assert content_invariant(table_raw, rule_normalize(table_raw)) is True

    unclosed_raw = "开头\n```\ncode line 1\n\tcode line 2 indented"
    assert content_invariant(unclosed_raw, rule_normalize(unclosed_raw)) is True


# ---------------------------------------------------------------------------
# Architectural change: `rule_normalize` used to be opt-out -- any line it
# didn't explicitly recognize was treated as prose and blank-line-separated.
# That destroyed any rich-markdown structure it wasn't taught about; three
# separate review rounds each found a new instance (fenced code, GFM tables,
# list continuation lines), and `content_invariant` is structurally BLIND to
# all of them (the text is unchanged, only the structure is destroyed), so
# the guard can't catch them either.
#
# `is_rich_markdown` flips this to opt-in: if a cell already contains real
# markdown structure, `rule_normalize` returns it completely untouched
# (byte-identical) instead of best-effort reformatting it. Unknown structure
# now defaults to "leave alone" -- the safe failure mode -- rather than
# "assume it's Excel prose and reformat it".
# ---------------------------------------------------------------------------

# One representative raw string per gate signal, plus the two repros
# confirmed directly against the pre-fix code:
#   rule_normalize("A | B\n--- | ---\n1 | 2") injected blank lines between
#   every table row (content_invariant still returned True -- structurally
#   blind); rule_normalize("- parent\n  continuation text") detached the
#   continuation into its own paragraph (content_invariant still True).
RICH_MARKDOWN_SIGNALS = [
    ("fence_backtick_closed", "```\ncode\n```"),
    ("fence_tilde_closed", "~~~\ncode\n~~~"),
    ("fence_unclosed", "```\ncode line"),
    ("pipe_table_leading_pipe", "| A | B |\n| - | - |"),
    ("pipe_table_no_leading_pipe", "A | B\n--- | ---\n1 | 2"),  # confirmed repro
    ("list_continuation", "- parent\n  continuation text"),  # confirmed repro
    ("blockquote", "> 引用文字"),
    ("html_block", "<div>\nhello\n</div>"),
    ("reference_link_definition", "[label]: https://example.com"),
    ("setext_underline", "Title\n---"),
]


@pytest.mark.parametrize(
    "raw", [raw for _, raw in RICH_MARKDOWN_SIGNALS], ids=[name for name, _ in RICH_MARKDOWN_SIGNALS]
)
def test_is_rich_markdown_detects_each_signal(raw):
    assert is_rich_markdown(raw) is True


@pytest.mark.parametrize(
    "raw", [raw for _, raw in RICH_MARKDOWN_SIGNALS], ids=[name for name, _ in RICH_MARKDOWN_SIGNALS]
)
def test_rule_normalize_leaves_rich_markdown_byte_identical(raw):
    assert rule_normalize(raw) == raw


def test_confirmed_repro_pipe_less_table_is_gated():
    raw = "A | B\n--- | ---\n1 | 2"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_confirmed_repro_list_continuation_is_gated():
    raw = "- parent\n  continuation text"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


# Gate must NOT over-skip: every existing golden case is genuine Excel-idiom
# input (tab-indented bullets/ordered/alpha, plain prose) that still needs
# normalizing -- none of it should trip any of the rich-markdown signals
# above, or rule_normalize would silently stop cleaning up real Excel paste
# junk the moment it shares a cell with, say, a middle dot or a lone dash.
@pytest.mark.parametrize("case", _GOLDEN, ids=[c["name"] for c in _GOLDEN])
def test_gate_does_not_skip_excel_idiom_golden_cases(case):
    assert is_rich_markdown(case["raw"]) is False


# ---------------------------------------------------------------------------
# P2 (wrong nesting indentation): a flat "2 spaces per level" indent is only
# correct when the PARENT's own rendered marker is 2 columns wide (i.e. a
# bullet/alpha parent, which always renders as "- "). CommonMark actually
# requires a child to be indented to the parent's marker width -- for an
# ordered parent that width varies with digit count ("1. " -> 3, "10. " -> 4).
#
# Manually confirmed against the real remark/GFM parser (`cd frontend &&
# node -e` with remark-parse + remark-gfm loaded from frontend/node_modules):
#   "1. parent\n  - child"  (2 spaces) -> list(ol)>item, list(ul)>item  --
#     TWO SEPARATE top-level lists, not nested.
#   "1. parent\n   - child" (3 spaces) -> list(ol)>item>list(ul)>item  --
#     genuinely nested.
#   "- parent\n  - child"   (2 spaces, bullet parent) -> already correct,
#     since "- " itself is exactly 2 columns wide.
# ---------------------------------------------------------------------------


def _content_start_column(marker_line: str) -> int:
    """Independent (does not reuse md_normalize's own marker regexes) check
    of the CommonMark column where a rendered list item's own content
    begins: leading spaces, plus a bullet/ordered marker up to and including
    its trailing space(s)."""
    leading = len(marker_line) - len(marker_line.lstrip(" "))
    rest = marker_line[leading:]
    m = re.match(r"^(?:[-*+]|\d+[.)])[ \t]+", rest)
    assert m, f"not a rendered list-marker line: {marker_line!r}"
    return leading + m.end()


def test_ordered_parent_child_indented_by_marker_width_not_flat_two_spaces():
    # the task's own confirmed P2 example: single-digit ordered parent ("1. "
    # is 3 columns wide) -- child must get 3 spaces, not a flat 2.
    out = rule_normalize("1. 父\n\ta. 子")
    assert out.split("\n") == ["1. 父", "   - 子"]


def test_ordered_child_is_genuinely_nested_per_commonmark_indent_rule():
    """Structural equivalent of 'parse it and confirm real nesting' without
    a full markdown parser in the loop: CommonMark nests a child under a
    list item iff the child's indentation >= the parent's content-start
    column -- exactly the property manually verified against remark above."""
    out = rule_normalize("1. 父\n\ta. 子")
    parent_line, child_line = out.split("\n")
    child_indent = len(child_line) - len(child_line.lstrip(" "))
    assert child_indent >= _content_start_column(parent_line)


def test_bullet_parent_child_still_indented_two_spaces():
    # regression guard: a bullet/alpha parent renders "- " (2 columns wide),
    # so flat-2-spaces and marker-width-indent agree here -- this case was
    # already correct before the fix and must stay that way.
    out = rule_normalize("- 父\n\ta. 子")
    assert out.split("\n") == ["- 父", "  - 子"]


def test_double_digit_ordered_parent_indents_child_by_four():
    out = rule_normalize("10. 父\n\ta. 子")
    assert out.split("\n") == ["10. 父", "    - 子"]


def test_three_level_nesting_sums_ancestor_marker_widths():
    # top (ordered, "1. " width 3) > mid (alpha/bullet-rendered, "- " width
    # 2) > leaf (ordered again, "1. "): leaf's indent must be the SUM of
    # its ancestors' widths (3 + 2 = 5), not its own width and not a flat
    # per-level constant.
    raw = "1. top\n\ta. mid\n\t\t1. leaf"
    out = rule_normalize(raw)
    lines = out.split("\n")
    assert lines == ["1. top", "   - mid", "     1. leaf"]
    # and each line's indent is still >= the column where its immediate
    # parent's own content starts (genuinely nested at every level).
    assert _content_start_column(lines[1]) <= len(lines[2]) - len(lines[2].lstrip(" "))


# ---------------------------------------------------------------------------
# Idempotence must survive marker-width indentation. `_indent_depth`
# (backend/app/services/knowhow/md_normalize.py's shared depth heuristic:
# leading tabs, plus leading-spaces // 2) is the single source of truth for
# "how deep is this line" -- but it is lossy for widths that are not a clean
# multiple of 2. A naive "level = info.depth - group_base" (subtracting raw
# depth numbers) re-derives the WRONG level on a second pass whenever a
# rendered indent isn't a multiple of 2: a double-digit ordered parent's
# child is rendered 4 spaces wide, but 4 leading spaces // 2 == 2, not the
# original level (1) -- so re-normalizing keeps adding a phantom extra level
# of indent every pass. Same failure mode compounds across an all-ordered
# multi-level chain (each "1. " ancestor contributes width 3, and two of
# them sum to 6, which likewise does not divide back to "2 levels").
#
# The fix must compare depths ORDINALLY against the stack of currently-open
# levels (deeper/same/shallower than the innermost open level) rather than
# by subtracting against a single fixed baseline -- ordinal comparisons stay
# correct even though `_indent_depth` is lossy, because it is still
# monotonic (more actual indentation never maps to a smaller depth number).
# ---------------------------------------------------------------------------


def test_idempotent_across_double_digit_ordered_nesting():
    once = rule_normalize("10. 父\n\ta. 子")
    twice = rule_normalize(once)
    assert twice == once, f"not idempotent:\n{once!r}\n!=\n{twice!r}"


def test_idempotent_across_multi_level_all_ordered_nesting():
    once = rule_normalize("1. top\n\t1. mid\n\t\t1. leaf")
    twice = rule_normalize(once)
    assert twice == once, f"not idempotent:\n{once!r}\n!=\n{twice!r}"


# ---------------------------------------------------------------------------
# Deny-list -> allow-list inversion: the newly-closed holes.
#
# The old `is_rich_markdown` enumerated dangerous signals ("has a fence / a
# pipe table / a continuation line / ..."), and four review rounds each found
# a newly-mangled structure the enumeration missed -- the last was 4-space
# indented code, which the deny-list missed ENTIRELY:
# `rule_normalize("    def f():\n        return 1")` returned
# "def f():\n\nreturn 1", destroying the code, and `content_invariant` (blind
# to "text unchanged, structure destroyed") accepted it.
#
# The inverted gate refuses ANY line with leading whitespace that is not a
# list marker -- one rule that closes indented code blocks, list continuation
# lines and indented prose generically (the exact class the deny-list kept
# leaking), plus ATX headings (already-markdown; leaving them untouched is the
# right product behavior).
# ---------------------------------------------------------------------------


def test_four_space_indented_code_block_refused():
    # THE hole the deny-list missed today: 4-space indented code.
    raw = "    def f():\n        return 1"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- code preserved


def test_tab_indented_non_marker_line_refused():
    raw = "\tsome indented prose that is not a list item"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_indented_prose_refused():
    raw = "普通说明\n  这一行有前导空格但不是列表项"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_atx_heading_refused():
    raw = "# 标题\n正文"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_idempotent_on_real_shape_normalized_cell():
    # A previously-normalized real-shape cell: intro prose, **A.**/**B.**
    # section headers, a `- ` bullet list, a `1.` ordered list, and a 3-space
    # nested child under the ordered parent. The gate must ALLOW it (it is a
    # target-shaped cell, not rich markdown) and rule_normalize must be an
    # exact no-op on it (idempotence on the real output shape).
    s = (
        "说明文字\n\n"
        "**A. 考量**\n\n"
        "- 一\n- 二\n\n"
        "**B. 步骤**\n\n"
        "1. 第一步\n2. 第二步\n   - 子步骤"
    )
    assert is_rich_markdown(s) is False
    assert rule_normalize(s) == s


# ---------------------------------------------------------------------------
# Adversarial hole (found in review): the allow-list gate accepted list-marker
# lines at ANY indentation. A valid CommonMark INDENTED CODE BLOCK whose content
# lines merely LOOK like markers --
#     "    - literal\n    1. literal"   (4-space indent)
# -- slipped through the gate as "marker lines", so rule_normalize converted
# real code into a top-level list, and content_invariant (text-blind) accepted
# it.
#
# Fix: adopt CommonMark's own 4-space disambiguation for marker-classified
# lines. A marker line's LEADING WHITESPACE is acceptable to the gate iff it
#   - contains at least one TAB  -> allowed (the Excel fingerprint: the feature's
#     entire target corpus is tab-indented), OR
#   - is spaces-only with count <= 3 -> allowed (CommonMark still treats 1-3
#     leading spaces as a list item; 3 spaces is also exactly what our own
#     emitter produces for one level of nesting under an ordered parent).
# A marker line indented by spaces-only, count >= 4, is a CommonMark indented
# code block: intent is unknowable, so we FAIL CLOSED and refuse the whole cell.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker_line",
    ["- literal", "* literal", "+ literal", "• literal", "1. literal", "10. literal", "a. literal", "B. literal"],
)
def test_four_space_indented_marker_line_refused_as_indented_code(marker_line):
    # spaces-only, count >= 4 -> CommonMark indented code block -> refuse.
    raw = "    " + marker_line
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- code preserved


def test_four_space_marker_code_block_two_lines_refused():
    # THE confirmed repro from review: two marker-looking lines at 4-space indent
    # form a valid indented code block; the whole cell must come back
    # byte-unchanged rather than be rewritten into a top-level list.
    raw = "    - literal\n    1. literal"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


@pytest.mark.parametrize("marker_line", ["- x", "• x", "1. x", "a. x"])
def test_tab_indented_marker_line_still_normalizes(marker_line):
    # any indent containing a TAB is the Excel fingerprint -> still allowed (the
    # feature's entire target corpus is tab-indented; must not regress).
    raw = "\t" + marker_line
    assert is_rich_markdown(raw) is False
    out = rule_normalize(raw)
    assert out != raw                          # actually gets cleaned up
    assert "\t" not in out


def test_core_tab_targets_still_normalize():
    # the task's two named core targets must not regress.
    assert is_rich_markdown("\t• 增大 R") is False
    assert rule_normalize("\t• 增大 R").split("\n")[0] == "- 增大 R"
    assert is_rich_markdown("\ta. 子项") is False
    assert rule_normalize("\ta. 子项") == "**a. 子项**"


def test_three_space_emitted_nesting_still_passes_and_round_trips():
    # our own emitter's one-level ordered nesting is exactly 3 spaces; the gate
    # must still ALLOW it and rule_normalize must be an exact no-op (idempotence).
    raw = "1. 父\n   - 子"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


@pytest.mark.parametrize("marker_line", ["- 子", "1. 子", "a. 子"])
def test_marker_indent_boundary_three_allowed_four_refused(marker_line):
    # exact CommonMark boundary under a plain parent line: 3 leading spaces is
    # allowed (gate normalizes), 4 leading spaces is refused (byte-identical).
    allowed = "父\n   " + marker_line
    refused = "父\n    " + marker_line
    assert is_rich_markdown(allowed) is False
    assert is_rich_markdown(refused) is True
    assert rule_normalize(refused) == refused


def test_deep_normalized_structure_with_ge4_space_child_refuses_as_no_op():
    # A DEEP already-normalized structure whose child indent reaches >= 4 spaces
    # (our emitter really does produce these -- a second level of nesting sums two
    # ancestor marker widths). Under the new rule the gate now REFUSES it, and
    # that is CORRECT: refusal returns the input byte-for-byte, which is itself a
    # fixed point, so idempotence still holds. Refusal-as-no-op here is the
    # intended safe behavior, NOT a bug -- once an indent reaches 4 spaces we can
    # no longer tell a deep nested list from an indented code block, so we fail
    # closed and leave the already-clean structure exactly as it is.
    raw = "1. a\n   1. b\n      - c"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical no-op


def test_mixed_tab_and_space_indent_marker_allowed():
    # "contains at least one TAB" -> allowed regardless of surrounding spaces,
    # in either order (tab-then-spaces, spaces-then-tab) even when the space
    # run alone would exceed 3.
    assert is_rich_markdown("\t  - x") is False
    assert is_rich_markdown("  \t- x") is False
    assert is_rich_markdown("    \t- x") is False


# ---------------------------------------------------------------------------
# Batch F — two context-aware gate rules (P2). Both are GATE rules: refusal
# returns the input byte-identically; neither may change the normalization of
# any currently-passing input (the goldens / real-shape cells above stay put).
#
# Rule 1 (lazy list continuation): a column-0 PROSE line DIRECTLY following a
# list-marker line (no blank line between) is a CommonMark LAZY CONTINUATION of
# that item. The per-line grammar accepts both lines, but `_normalize` injects a
# blank line and DETACHES the continuation, and `content_invariant` (char-blind)
# passes the corruption. The gate becomes context-aware: refuse when a
# prose-classified line immediately follows a marker-classified line. A blank
# line between them breaks the continuation per CommonMark, so
# marker -> blank -> prose stays ALLOWED (that shape pervades the already-
# normalized corpus and must keep passing).
#
# Rule 2 (thematic break): `* * *` matches the bullet regex (`*` + space + body
# `* *`) and is "normalized" to `- * *`, destroying the horizontal rule; the
# symbol-blind invariant passes. Same class: `- - -`, `_ _ _`, and the unspaced
# `***`/`___` forms at column 0. The gate detects CommonMark thematic-break
# syntax -- a line whose content after removing ALL spaces/tabs is >=3 of the
# SAME char from `* - _` -- BEFORE marker acceptance, and refuses the cell. The
# existing `| : - =`-only delim rule already caught plain `---`; this
# generalizes to the spaced and `*`/`_` forms without merging the two rules.
# ---------------------------------------------------------------------------


def test_lazy_continuation_after_bullet_refused():
    # the task's confirmed repro: column-0 prose right after a bullet item.
    raw = "- parent\ncontinuation"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- not detached


def test_lazy_continuation_after_ordered_refused():
    raw = "1. parent\ncontinuation"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_lazy_continuation_after_indented_alpha_refused():
    # an indented alpha renders as a nested list item, so a column-0 prose line
    # right after it is likewise a lazy continuation -- all three
    # marker-classified kinds (bullet/ordered/alpha) behave uniformly.
    raw = "1. 父\n\ta. 子\ncontinuation"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_blank_separated_prose_after_bullet_still_normalizes():
    # a blank line between the marker and the prose breaks the continuation per
    # CommonMark -> the cell stays ALLOWED and normalizes (idempotent no-op here).
    raw = "- parent\n\nprose"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "- parent\n\nprose"


def test_prose_then_marker_is_not_a_continuation():
    # DIRECTION matters: prose FOLLOWED BY a marker (intro line then list) is the
    # normal Excel shape, not a lazy continuation -> must stay ALLOWED.
    raw = "实际操作：\n\t1. 遍历各个corner"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "实际操作：\n\n1. 遍历各个corner"


@pytest.mark.parametrize(
    "raw",
    ["* * *", "- - -", "_ _ _", "***", "___", "- - - -", "* * * *"],
)
def test_thematic_break_refused(raw):
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- HR preserved


def test_thematic_break_embedded_between_prose_refused():
    # the confirmed repro: `* * *` matched the bullet regex and became `- * *`.
    raw = "before\n* * *\nafter"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_unspaced_thematic_break_between_prose_refused():
    raw = "a\n***\nb"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_genuine_bullet_star_still_normalizes():
    # `* item` is a real bullet: stripping spaces gives `*item`, not >=3 of the
    # same char -> not a thematic break; still normalizes to `- item`.
    raw = "* item"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw).split("\n")[0] == "- item"


def test_two_char_dash_run_is_not_a_thematic_break():
    # boundary: `- -` strips to `--` (2 chars, below the >=3 threshold) -> not a
    # thematic break; it stays a bullet whose body is `-` and normalizes to itself.
    raw = "- -"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "- -"


# ---------------------------------------------------------------------------
# F1 — the gate must recognize ALL CommonMark HTML-block openers, not just
# `<` + letter/`/`. The opener detection matched `<[A-Za-z/]` only, so a line
# opening with `<!--` (comment), `<?` (processing instruction), `<!` + letter
# (declaration, e.g. `<!DOCTYPE html>`) or `<![CDATA[` fell through to prose;
# a MULTI-line one then got blank lines injected INSIDE it and the char-blind
# content_invariant accepted the corruption. The fix widens the opener class
# to `<` followed by an ASCII letter, `/`, `!`, or `?` (that single char-class
# covers comment/PI/declaration/CDATA -- no HTML parser). A mid-line `<`
# (`a < b`) or a lone `<3` (`<` + digit, NOT an opener) must still normalize.
# ---------------------------------------------------------------------------

HTML_BLOCK_OPENERS = [
    ("comment", "<!-- 注释 -->"),
    ("processing_instruction", '<?xml version="1.0" ?>'),
    ("declaration_doctype", "<!DOCTYPE html>"),
    ("cdata", "<![CDATA[x]]>"),
]


@pytest.mark.parametrize(
    "raw", [r for _, r in HTML_BLOCK_OPENERS], ids=[n for n, _ in HTML_BLOCK_OPENERS]
)
def test_html_block_opener_variants_gated(raw):
    # each opener shape, alone on a line, must trip the gate byte-identically.
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


@pytest.mark.parametrize(
    "raw", [r for _, r in HTML_BLOCK_OPENERS], ids=[n for n, _ in HTML_BLOCK_OPENERS]
)
def test_html_block_opener_followed_by_prose_not_corrupted(raw):
    # THE corruption repro: opener line + a plain prose line. Without the widened
    # opener class the gate misses the opener, _normalize inserts a blank line
    # between the two ("adjacent prose lines each become their own paragraph"),
    # and the char-blind invariant passes the mangled output. With the fix the
    # whole cell is refused byte-identically.
    cell = raw + "\n内容行"
    assert is_rich_markdown(cell) is True
    assert rule_normalize(cell) == cell


def test_midline_less_than_still_normalizable():
    # `<` in the MIDDLE of a line is not a block opener -> stays prose, no refusal.
    assert is_rich_markdown("a < b") is False


def test_lone_less_than_digit_not_over_refused():
    # `<3` is `<` + digit, which is NOT a CommonMark HTML-block opener -> must NOT
    # be over-refused; it stays a normalizable prose line (byte-identical no-op).
    assert is_rich_markdown("<3") is False
    assert rule_normalize("<3") == "<3"


# ---------------------------------------------------------------------------
# F1 (this review) — the gate must refuse MID-LINE inline-HTML tag openings, not
# only line-START HTML blocks. `_starts_block_construct`'s `_HTML_BLOCK_RE` is
# `^`-anchored, so `prefix <span>\ncontinued</span>` -- a `<span>` opening
# MID-line and continuing across a soft line break -- slipped through: `_normalize`
# injects a blank line between the two prose lines, landing it INSIDE the span,
# and the symbol-blind `content_invariant` (`<`/`>` are punctuation) misses the
# corruption. Fix: refuse the cell when ANY line contains an unescaped `<`
# immediately followed by an ASCII letter or `/` (tag-shaped) ANYWHERE in the
# line. Fail-closed / over-refusal accepted (a miss is cheap): `a < b` (space),
# `x<0`/`i<3` (digit) stay normalizable; `x<y` (letter) and `包含 <em>标签` refuse.
# ---------------------------------------------------------------------------


def test_midline_inline_html_span_across_lines_refused_byte_identical():
    # THE codex repro: mid-line `<span>` opening, continuing across a soft break.
    cell = "prefix <span>\ncontinued</span>"
    assert is_rich_markdown(cell) is True
    assert rule_normalize(cell) == cell   # byte-identical no-op


def test_midline_inline_html_letter_tag_refused():
    # `x<y`: `<` + ASCII letter = tag-shaped -> refused (fail-closed).
    assert is_rich_markdown("x<y") is True
    assert rule_normalize("x<y") == "x<y"


def test_midline_inline_html_cjk_prose_em_tag_refused():
    # `包含 <em>标签`: a mid-line `<em>` opener in CJK prose -> refused.
    assert is_rich_markdown("包含 <em>标签") is True


def test_midline_inline_html_closing_slash_tag_refused():
    # `</span>` (`<` + `/`) is equally tag-shaped -> refused wherever it appears.
    assert is_rich_markdown("尾 </span> 收") is True


@pytest.mark.parametrize("raw", ["a < b", "x<0", "i<3"])
def test_midline_less_than_non_tag_still_normalizable(raw):
    # `<` + space (`a < b`) or `<` + digit (`x<0`, `i<3`) are NOT tag-shaped ->
    # must stay normalizable (byte-identical no-op for these single prose lines).
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F2 (this review) — the mid-line inline-HTML rule must also refuse HTML COMMENT
# (`<!--`) and PROCESSING-INSTRUCTION (`<?`) openers, not only `<`+letter/`/`. The
# line-START HTML-block class already accepts `<` + letter/`/`/`!`/`?` (`_HTML_BLOCK_RE
# = ^<[A-Za-z/!?]`), but `_has_midline_inline_html` matched only `<`+letter/`/`, so a
# MID-line `foo <!-- x\ny -->` or `foo <?pi\nx?>` slipped through: `_normalize` injects
# a blank line INSIDE the comment/PI, and the symbol-blind `content_invariant` (`<`,`!`,
# `?`,`-` are punctuation) misses the corruption. Fix: extend the mid-line tag shape to
# `<` followed by letter, `/`, `!`, or `?` (mirroring the line-START class). `a < b`,
# `x<0`, `i<3` stay normalizable.
# ---------------------------------------------------------------------------


def test_midline_inline_html_comment_opener_across_lines_refused_byte_identical():
    # THE codex repro: a mid-line HTML comment opener (`<!`) crossing a soft break.
    cell = "foo <!-- x\ny -->"
    assert is_rich_markdown(cell) is True
    assert rule_normalize(cell) == cell   # byte-identical no-op


def test_midline_inline_html_pi_opener_across_lines_refused_byte_identical():
    # a mid-line processing-instruction opener (`<?`) crossing a soft break -- same
    # class, `<`+`?` now tag-shaped.
    cell = "foo <?pi\nx?>"
    assert is_rich_markdown(cell) is True
    assert rule_normalize(cell) == cell


def test_midline_inline_html_bang_and_question_tags_refused():
    # `<!` (declaration/comment) and `<?` (PI) are tag-shaped WHEREVER they appear.
    assert is_rich_markdown("尾 <!DOCTYPE 收") is True
    assert is_rich_markdown("尾 <?php 收") is True


# ---------------------------------------------------------------------------
# F3 — marker WIDTH must be counted in CODE POINTS, not UTF-16 units. An astral
# ordered marker like 𝟙. (U+1D7D9 MATHEMATICAL DOUBLE-STRUCK DIGIT ONE -- a
# surrogate pair in UTF-16, category Nd so it matches the ordered regex on both
# sides) renders as "𝟙. ", 3 CODE POINTS wide. A child's indent is the sum of
# its ancestors' marker widths, so the child must get 3 spaces. Python's len()
# already counts code points (this side is correct); the assertion locks it and
# mirrors the TS twin's fix, where JS `marker.length` counts UTF-16 units (the
# surrogate pair -> width 4) and must be replaced with `[...marker].length`.
# ---------------------------------------------------------------------------

_ASTRAL_ORDERED_MARKER = "\U0001d7d9"  # 𝟙: 1 code point, but a surrogate pair in UTF-16


def test_astral_ordered_marker_child_indented_by_codepoint_width():
    raw = _ASTRAL_ORDERED_MARKER + ". 父\n\ta. 子"
    out = rule_normalize(raw)
    parent_line, child_line = out.split("\n")
    assert out.split("\n") == [_ASTRAL_ORDERED_MARKER + ". 父", "   - 子"]
    # genuinely nested: 3-space child indent >= parent's content-start column
    # (marker "𝟙. " is 3 code points wide), not the 4 a UTF-16-unit count gives.
    child_indent = len(child_line) - len(child_line.lstrip(" "))
    assert child_indent == 3
    assert child_indent >= _content_start_column(parent_line)


# ---------------------------------------------------------------------------
# F1 — the gate must refuse CommonMark HARD LINE BREAKS. A non-blank line whose
# RAW form ends with TWO-OR-MORE trailing spaces, or with a single trailing
# backslash, is a deliberate CommonMark hard break (a <br> INSIDE one paragraph).
# `_normalize` strips the trailing marker (prose bodies are `.strip()`-ed) and
# then blank-line-separates adjacent prose lines -- so the hard break silently
# becomes a PARAGRAPH break, and the char-blind `content_invariant` accepts the
# corruption (no content char changed, only structure). The gate must fail
# CLOSED and refuse the whole cell byte-identically.
#
# Real Excel pastes carry at most a SINGLE trailing space (the only trailing-
# whitespace shape in the production corpus), which stays allowed -- one space
# is not a hard break. A refused cell is merely left untouched; the miss-vs-
# corruption cost asymmetry says refuse.
# ---------------------------------------------------------------------------


def test_two_trailing_spaces_hard_break_refused():
    # 第一行 followed by TWO spaces = CommonMark hard break; must not be turned
    # into a paragraph break.
    raw = "第一行  \n第二行"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- hard break preserved


def test_trailing_backslash_hard_break_refused():
    # a single trailing backslash is the other CommonMark hard-break spelling.
    raw = "第一行\\\n第二行"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_single_trailing_space_still_normalizes():
    # THE real-corpus shape: exactly ONE trailing space (not a hard break) must
    # keep normalizing -- refusing it would stop cleaning genuine Excel paste junk.
    raw = "4. 计算充足)： \n\ta. x"
    assert is_rich_markdown(raw) is False
    out = rule_normalize(raw)
    assert out != raw                          # actually gets cleaned up
    assert out.split("\n") == ["4. 计算充足)：", "   - x"]


def test_blank_line_with_trailing_spaces_does_not_trigger_hard_break_refusal():
    # a blank line (all-whitespace) that happens to hold >=2 trailing spaces must
    # NOT trip the hard-break rule -- it is not content, just spacing. If the check
    # ran before the blank-line short-circuit this cell would be wrongly refused.
    raw = "第一行\n   \n第二行"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "第一行\n\n第二行"


def test_two_trailing_spaces_on_a_marker_line_refused():
    # the hard break can sit on a list-marker line too ("- item  " with a trailing
    # <br>); classify_line's `.strip()` would silently drop it -> refuse the cell.
    raw = "- 第一项  \n- 第二项"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F2 — the gate must refuse INLINE constructs that span a SOFT newline. The
# renderer has remark-math enabled, so `$a\n+b$` is a single inline formula soft-
# wrapped across two lines. The old gate classifies it as two plain prose lines
# and passes; `_normalize` blank-line-separates adjacent prose -> `$a\n\n+b$`, which
# no longer parses as a formula, and the symbol-blind `content_invariant` (no
# content char changed, only structure) approves the corruption. Same shape for a
# soft-wrapped inline code span `` `code\nspan` ``.
#
# Fix (run pairing, fail CLOSED): while scanning gate lines, track, per line, the
# run pairing of UNESCAPED `$` (skip `\$`) and of inline-code backtick runs (a run
# of N is closed only by a later run of N; length-mismatched runs are literal). Any
# line ending inside an OPEN inline span (an unclosed `$` run, or an unclosed
# backtick run) means the construct crosses the newline -> refuse the whole cell. A
# line with paired `$...$` / `` `...` `` stays fine. This deliberately also refuses a
# lone `价格 $100` line -- a lone open `$` run is ambiguous to the renderer too, and
# a refused cell is merely left untouched (miss, not corruption). The `$$` display
# form is caught by the same run pairing -- see the F1 section further below.
# ---------------------------------------------------------------------------


def test_math_formula_spanning_soft_newline_refused():
    # RED before the fix: two prose lines `$a` / `+b$`, each 1 unescaped `$` -> the
    # gate lets it through and `_normalize` splits the formula with a blank line.
    raw = "$a\n+b$"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- formula preserved


def test_inline_code_span_spanning_soft_newline_refused():
    raw = "`code\nspan`"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_paired_dollar_math_on_one_line_still_normalizes():
    # a closed inline formula on a single line: the `$` run opens and a same-length
    # run closes it -> not open at EOL -> still allowed.
    raw = "公式 $x+y$ 正常"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "公式 $x+y$ 正常"


def test_escaped_dollar_is_not_a_delimiter_still_normalizes():
    # `\$` is an escaped literal dollar (remark-math ignores it) -> 0 unescaped `$`,
    # no `$` run -> still normalizable.
    raw = "价格 \\$100 转义"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "价格 \\$100 转义"


def test_paired_inline_code_on_one_line_still_normalizes():
    raw = "行内 `ok` 正常"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "行内 `ok` 正常"


def test_lone_dollar_on_one_line_refused_fail_closed():
    # a single unescaped `$` (a lone open run) is ambiguous to the renderer too -> the
    # fail-closed direction refuses it. Left untouched, not corrupted.
    raw = "价格 $100 元"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F1 (this batch) — `$` is tracked as delimiter RUNS, not single-char parity.
# remark-math's DISPLAY form `$$x\n+y$$` spans a soft newline. The old per-char
# `$` parity sees `$$` as two dollars (even = "closed") and passes the cell, so
# blank-line insertion breaks the formula and the symbol-blind content_invariant
# approves the corruption. Fix (mirror the backtick handling): a run of N `$`
# opens a math span closed only by a LATER run of the SAME length (CommonMark-math
# convention; same-length matching is enough for the gate's fail-closed purpose).
# A line ending inside an OPEN `$`-run span -> refuse the whole cell. Single-`$`
# behavior is unchanged (a lone open `$` run still refuses).
# ---------------------------------------------------------------------------


def test_display_math_spanning_soft_newline_refused():
    # RED before the fix: line 0 `before $$x` has an OPEN `$$` run at EOL. Per-char
    # parity counted 2 dollars (even) and passed; run-based pairing refuses.
    raw = "before $$x\n+y$$ after"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- formula preserved


def test_paired_display_math_on_one_line_still_normalizes():
    # a closed `$$...$$` on a single line: the `$$` run opens and a later same-length
    # `$$` run closes it -> not open at EOL -> still normalizable (no regression).
    raw = "$$x+y$$ 同行"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


def test_lone_double_dollar_run_open_refused():
    # RED before the fix: `$$` alone on line 0 opens a display-math run closed only
    # by the `$$` on the last line. Per-line, line 0 ends inside an open `$$` run ->
    # refuse. (Old parity saw 2 dollars per line, all even -> passed.)
    raw = "$$\nx + y\n$$"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_escaped_double_dollar_is_not_a_delimiter_still_normalizes():
    # both `$` escaped (`\$\$`) -> no `$` run at all -> normalizable (no regression).
    raw = "价格\\$\\$转义"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F1 (this batch) — cross-line EMPHASIS is now detected by a CELL-GLOBAL pairing
# stack, replacing the old per-line neutrality. `*first\nsecond*` still refuses
# (a can-open-only run on line 1 pairs with a can-close-only run on line 2), but
# the real hole was the INTRAWORD (both-flanking) shape: `这是*跨行\n强调*内容`
# has two `*` runs each word-flanked on BOTH sides. The per-line rule judged a
# BOTH run NEUTRAL and passed the cell, yet CommonMark pairs the two `*` ACROSS
# the soft newline; `_normalize` injects a blank line and breaks the emphasis,
# and the symbol-blind `content_invariant` (the `*` is stripped as punctuation)
# approves the corruption.
#
# Fix: `is_rich_markdown` runs ONE cell-global stack over `*`/`_` delimiter runs,
# each entry tagged with its line. CAN-OPEN-only pushes; CAN-CLOSE-only pops
# (forming a pair); a BOTH run pops if the stack is non-empty (pair) else pushes.
# REFUSE iff any formed pair spans two DIFFERENT lines. Unmatched leftovers form
# no pair and never refuse by themselves — this keeps the flagship intraword
# multiplication `R*C` normalizable (its lone BOTH run pushes and never pairs),
# even when a LATER line carries a fully same-line-paired `**bold**` (which pushes
# then pops ITSELF, leaving the R*C entry untouched). A consequence of dropping
# per-line neutrality: a DANGLING opener that never pairs anywhere (`first *middle
# \nmore`, `_emph\ntext`) no longer refuses — it forms no emphasis in CommonMark,
# so a blank line cannot break a non-existent span (content_invariant-verified
# safe). Bracket/`$`/backtick spans stay per-line in `_ends_with_open_inline_span`.
# ---------------------------------------------------------------------------


def test_emphasis_open_star_spanning_soft_newline_refused():
    # the codex example: `*first` opens emphasis unclosed on line 1.
    raw = "*first\nsecond*"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- emphasis preserved


def test_link_text_bracket_spanning_soft_newline_refused():
    # the codex example: `[label` opens link text unclosed on line 1.
    raw = "[label\ncontinued](url)"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_intraword_star_multiplication_still_normalizes():
    # THE load-bearing neutrality case (flagship real-corpus line): `R*C` has a
    # word char on both sides -> NEUTRAL -> not an open span -> still normalizable.
    raw = "增加RC时间常数，delay正比于R*C"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw          # single prose line -> no-op


def test_intraword_star_multiplication_in_bullet_still_normalizes():
    # the same shape as it really appears in the golden corpus (tab bullet).
    raw = "\t• 增加RC时间常数，delay正比于R*C"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw).split("\n")[0] == "- 增加RC时间常数，delay正比于R*C"


def test_paired_emphasis_same_line_still_normalizes():
    # `**A. 头**` / `*x*` fully paired on one line -> open-count returns to 0.
    assert is_rich_markdown("**A. 头**") is False
    assert is_rich_markdown("这是 *重点* 内容") is False


def test_paired_bold_section_header_round_trips():
    # the emitter's own bolded section header must keep passing the gate.
    raw = "**A. 考量**\n\n- 一\n- 二"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


def test_dangling_open_star_no_pair_now_normalizes():
    # F1 cell-global change: `first *middle` is a can-open-only run that NEVER
    # pairs with a closer anywhere in the cell -> an unmatched leftover. It forms
    # no emphasis in CommonMark, so injecting a blank line cannot break a
    # (non-existent) span. The old per-line rule over-refused it; the cell-global
    # stack correctly normalizes it (and content_invariant confirms it is safe).
    raw = "first *middle\nmore"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


def test_dangling_open_underscore_no_pair_now_normalizes():
    raw = "_emph\ntext"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


def test_escaped_bracket_is_not_an_open_span():
    # `\[` is an escaped literal bracket -> depth unaffected -> normalizable.
    raw = "\\[转义"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == "\\[转义"


def test_fullwidth_brackets_are_not_ascii_brackets():
    # 【】 (the real-corpus bracket) is a different character than ASCII [] -> the
    # bracket-depth rule ignores it -> the cell still normalizes.
    raw = "【注意】这是一段说明"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


def test_balanced_link_text_same_line_still_normalizes():
    # balanced [..] on one line -> depth returns to 0; an inline link on a prose
    # line is allowed (it does not cross the newline).
    assert is_rich_markdown("见 [文档](url) 说明") is False


# ---------------------------------------------------------------------------
# F3 (this review) — the open-span tracker must also refuse a line that ends with an
# OPEN link DESTINATION. `[docs](/url\n"title")`: bracket depth returns to zero at `]`,
# but the `(`-destination opened by `](` is unclosed at line end, so the gate passed and
# `_normalize` split the destination/title across the injected blank line, turning the
# link to plain text -- symbol-blind `content_invariant` misses it. Fix: after a `](`
# is seen on a line, track the unclosed destination paren (same escape rules); a line
# ending inside an open destination refuses. Scoped strictly to link context: a bare
# `(` in prose WITHOUT a preceding `](` does NOT refuse (plain parens are common).
# ---------------------------------------------------------------------------


def test_link_destination_open_paren_spanning_soft_newline_refused():
    # THE codex example: the `(`-destination opened by `](` is unclosed on line 1.
    raw = '[docs](/url\n"title")'
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- link preserved


def test_link_destination_closed_same_line_still_normalizes():
    # `[docs](/url "title")` fully closed on one line -> dest paren returns to zero ->
    # an inline link on a prose line is allowed (it does not cross the newline).
    assert is_rich_markdown('[docs](/url "title")') is False


def test_bare_open_paren_without_link_bracket_does_not_refuse():
    # a bare unclosed `(` in prose WITHOUT a preceding `](` must NOT refuse -- the
    # destination tracker only counts parens AFTER a `](` (strict link scope), so plain
    # prose parens stay normalizable.
    assert is_rich_markdown("文本 (未闭合") is False
    assert rule_normalize("文本 (未闭合") == "文本 (未闭合"


def test_link_destination_escaped_close_paren_keeps_it_open():
    # an escaped `\)` inside the destination does NOT close it (CommonMark escape) -> a
    # line ending after it is still an open destination -> refused.
    raw = "[docs](/url\\)\ncont)"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F3 round-2 (this review) — the `](`-destination tracker armed on EVERY `](`,
# including one that sits INSIDE an already-CLOSED inline code / math span on the
# same line. `A. header\n`x](`` closes the backtick span (the `](` is code-span
# content, not a link opener), yet the tracker armed and never disarmed -> the cell
# was classified rich and returned byte-identical, so `A. header` was never bolded.
# Fix: the rule-4 scan now also tracks the code-span / `$`-span state left-to-right;
# a `](` seen while inside a closed span does NOT arm the tracker. A `](` in plain
# text still arms it (the cross-line link regression locks below stay refused).
# ---------------------------------------------------------------------------


def test_link_destination_bracket_paren_inside_closed_code_span_not_refused():
    # THE codex example line: a `](` INSIDE a backtick code span closed on its own line is
    # span CONTENT, not a link opener -> it must NOT arm the destination tracker. On the
    # single line (the only rule that could refuse it) the cell now normalizes:
    assert is_rich_markdown("`x](`") is False
    assert rule_normalize("`x](`") == "`x](`"    # code span preserved verbatim (idempotent)
    # End-to-end "the header gets bolded": a top-level alpha header above that code-span
    # line normalizes to a bold section header. NB the review's raw `A. header\n`x](``
    # (marker directly followed by prose, no blank) is refused for a SEPARATE, pre-existing
    # reason -- the lazy-continuation rule (test_lazy_continuation_after_* pin it) -- which
    # is independent of this `](` fix; a blank line breaks that continuation and isolates
    # the fix, so the header below bolds while the code span stays verbatim.
    raw = "A. header\n\n`x](`"
    assert is_rich_markdown(raw) is False
    out = rule_normalize(raw).split("\n")
    assert "**A. header**" in out                # header bolded
    assert "`x](`" in out                        # code span preserved verbatim


def test_link_destination_bracket_paren_adjacent_alpha_refused_by_lazy_continuation():
    # regression pin documenting the confound: the review's literal `A. header\n`x](``
    # (marker directly followed by prose) STAYS rich -- NOT because of the `](` (that line
    # no longer refuses on its own, asserted above) but because column-0 prose directly
    # after a marker is a lazy continuation. This is orthogonal to the `](` fix and must
    # not regress into normalizing (which would blank-line-split a soft-break paragraph).
    raw = "A. header\n`x](`"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_link_destination_bracket_paren_inside_closed_code_span_same_line():
    # `](` inside a code span closed on the SAME line (with trailing prose) -> not rich.
    assert is_rich_markdown("`](` 同行闭合") is False


def test_link_destination_bracket_paren_inside_closed_math_span_not_refused():
    # `](` inside a `$`-math span closed on the same line -> the math-span state disarms
    # the tracker just like the code-span state -> not rich.
    assert is_rich_markdown("$a](b$ 同行") is False


def test_link_destination_plain_text_bracket_paren_still_arms_after_closed_span():
    # regression lock: a `](` in PLAIN text (after a closed code span) with an unclosed
    # destination must STILL refuse -- the closed-span skip must not swallow real links.
    raw = '`code` [docs](/url\n"title")'
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F3 round-3 (this review) — the destination tracker decremented paren depth on
# EVERY `)`, so a valid inline link whose QUOTED TITLE contains `)` before a soft
# newline -- `[x](url "title )\ncontinued")` -- had the title's `)` mistaken for
# the destination's closing paren: line 1 was judged closed/safe, so the gate
# passed and `_normalize` split the title with a blank line, breaking the link
# (symbol-blind `content_invariant` misses it). Fix: model CommonMark's inline-link
# grammar -- after the destination (which runs to whitespace or `)`), an OPTIONAL
# title opens with `"`/`'`/`(` (closing with `"`/`'`/`)`, escape-aware); a `)`
# INSIDE an open title does NOT decrement destination depth. A line ending with the
# link still open (open destination OR open title) refuses (fail-closed unchanged --
# the bug was only the title's `)` prematurely CLOSING it).
# ---------------------------------------------------------------------------


def test_link_title_close_paren_spanning_soft_newline_refused():
    # THE codex example: the quoted title contains `)` then a soft newline. The title's
    # `)` must NOT read as the destination's closing paren -> line 1 ends INSIDE the open
    # title -> refuse byte-identically (the link is preserved, not blank-line-split).
    raw = '[x](url "title )\ncontinued")'
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- link preserved


def test_link_title_close_paren_complete_same_line_not_rich():
    # over-refusal lock: a same-line COMPLETE link whose title contains `)` -> the dest
    # correctly stays open through the title, the real closing `)` closes the link -> not
    # rich (no cross-line span; the trailing prose is normalizable).
    assert is_rich_markdown('[x](url "title )") 同行完整') is False


def test_link_single_quote_title_spanning_soft_newline_refused():
    # single-quote title variant: the title closes on line 1 (`'ti)tle'`, its inner `)`
    # ignored), but the destination's closing `)` sits on line 2 -> line 1 ends after the
    # title with the link still open -> refuse.
    raw = "[x](url 'ti)tle'\n)"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_link_paren_title_complete_same_line_not_rich():
    # paren-style title `(title)` complete on one line -> opens on the post-destination
    # `(` (close `)`), the first `)` closes the title, the link's `)` closes the link ->
    # not rich. (Nested paren titles parse oddly; only the simple form is exercised.)
    assert is_rich_markdown('[x](url (title))') is False


# ---------------------------------------------------------------------------
# F1 cell-global emphasis pairing — the intraword (both-flanking) hole and the
# load-bearing R*C survival. See the section comment above `test_emphasis_open_
# star_spanning_soft_newline_refused` for the algorithm.
# ---------------------------------------------------------------------------


def test_intraword_star_pair_spanning_soft_newline_refused():
    # THE codex hole: both `*` are word-flanked on both sides (BOTH runs) that the
    # old per-line neutrality passed; the cell-global stack pairs them across the
    # newline (BOTH pushes line 0, BOTH pops at line 1) -> the pair spans two lines
    # -> refuse byte-identically (the emphasis would otherwise be blank-line-split).
    raw = "这是*跨行\n强调*内容"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- emphasis preserved


def test_intraword_underscore_pair_spanning_soft_newline_refused():
    # `_` twin of the above -- a cross-line pair of underscore emphasis.
    raw = "_下划线跨行\n结束_"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_flanking_only_star_pair_spanning_soft_newline_still_refused():
    # regression: the pre-existing flanking-only cross-line pair (`*开头` can-open
    # only on line 0, `结尾*` can-close only on line 1) must STILL refuse under the
    # cell-global stack -- same pair-spans-two-lines verdict as before.
    raw = "*开头\n结尾*"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_intraword_star_survives_with_later_same_line_bold():
    # THE accounting case the fix must get right: the lone intraword `R*C` (BOTH,
    # pushes and dangles) must NOT be stolen by a LATER fully same-line-paired
    # `**参考信息**` (its opener pushes then its own closer pops ITSELF). The R*C
    # entry stays an unmatched leftover -> no cross-line pair -> still normalizable.
    raw = "增大RC时间常数，delay正比于R*C\n\n**参考信息**\n正文"
    assert is_rich_markdown(raw) is False


def test_escaped_star_ignored_in_cross_line_scan():
    # `\*` is an escaped literal star -> skipped by the run scanner -> no delimiter
    # run on either line -> nothing to pair -> normalizable (the cell is two plain
    # prose lines with a literal backslash-star).
    raw = "开头\\*文字\n第二行\\*更多"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


def test_same_line_paired_emphasis_across_multiple_lines_normalizes():
    # each line self-pairs its own emphasis (`*a*` and `**b**`); no pair spans two
    # lines -> normalizable. Guards against a naive global counter that would let
    # line 1's opener pair with line 2's closer.
    raw = "前 *a* 中\n后 **b** 尾"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


# ---------------------------------------------------------------------------
# F2 (this batch) — the cross-line emphasis pairing stack is TYPED by delimiter
# char. The old single untyped stack let an intraword `_` (BOTH run) POP a `*`
# opener (CommonMark never pairs `*` with `_`), so the true `*...*` cross-line
# pair went undetected and the cell passed -> corruption persisted. Fix: maintain
# ONE independent stack PER delimiter char (`*` and `_`); a `*`-run interacts only
# with the `*` stack, a `_`-run only with the `_` stack. Refuse iff EITHER stack
# forms a pair spanning two different lines. Two stacks sidestep interleaving
# entirely and are strictly more precise than the single stack.
# ---------------------------------------------------------------------------


def test_star_pair_crosses_line_despite_intervening_underscore_refused():
    # RED before the fix: `*open R_C\nclose*`. The `*` opens on line 0 and closes on
    # line 1 (a real cross-line pair). The intraword `_` on line 0 (a BOTH run) must
    # NOT pop the `*` opener. The old single stack let `_` steal it -> the `*` pair
    # went undetected and the cell passed. Two per-char stacks catch the `*` pair.
    raw = "*open R_C\nclose*"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- emphasis preserved


def test_intraword_underscore_alone_still_normalizes():
    # `R_C` is a lone intraword `_` (BOTH run) that never pairs -> unmatched leftover
    # -> normalizable; the following prose line is unaffected (no regression).
    raw = "R_C\n正常"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


def test_underscore_and_star_pairs_same_line_independent_still_normalizes():
    # `_a_` and `*b*` each fully pair on the SAME line, each in its OWN char stack ->
    # both stacks return to empty -> no cross-line pair -> normalizable.
    raw = "_a_ 与 *b* 同行"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F1 (this batch) — the cross-line emphasis stack must count DELIMITER UNITS,
# not RUNS. A can-close run of length N carries N closing delimiters and can pop
# up to N openers, EACH forming its own (opener, closer) pair. The old code
# treated a whole run as ONE pop: `*outer\n*inner**` popped the `**` closer once,
# left one opener dangling, registered NO cross-line pair, and passed the cell;
# `_normalize` then split the emphasis across the injected blank line -- a
# corruption the symbol-blind content_invariant misses. Fix: a can-open run of
# length N pushes N units (each tagged with its line); a can-close run pops up to
# N units, each popped unit forming a pair (refuse iff any pair spans lines);
# BOTH runs pop up to N if the stack is non-empty, else push N.
# ---------------------------------------------------------------------------


def test_double_closer_run_pops_two_units_cross_line_refused():
    # THE P1 hole: `*outer\n*inner**`. line 0 `*` opens (unit A). line 1 `*` opens
    # (unit B), then `**` is a length-2 can-close run carrying TWO closing
    # delimiters: pop 1 takes B (same-line pair, ok), pop 2 takes A (pair spans
    # line 0 -> line 1 => cross-line). The old run-as-one-pop left A dangling and
    # passed the cell. Unit counting pops both units and refuses byte-identically.
    raw = "*outer\n*inner**"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- emphasis preserved


def test_double_run_same_line_pairs_across_lines_still_normalize():
    # `**a**\n*b*`: line 0's `**` opens 2 units, its own `**` closer pops both
    # (both same-line); line 1's `*` opens 1 unit, its own `*` closer pops it (same
    # line). No pair spans two lines -> normalizable. Guards the unit-count fix
    # against over-refusing legitimate same-line strong/emphasis on adjacent lines.
    raw = "**a**\n*b*"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


def test_triple_run_same_line_bold_italic_normalizes():
    # `***bold italic***`: a length-3 can-open run pushes 3 units; the length-3
    # can-close run pops all 3, every pair on the same line -> normalizable (single
    # prose line, byte round-trip). Unit counting must not mis-pair the extra
    # delimiters across a (nonexistent) newline.
    raw = "***bold italic***"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


def test_triple_open_run_spanning_soft_newline_refused():
    # `***bold\n收尾***`: three can-open delimiters on line 0, three can-close on
    # line 1 -> three pairs, ALL spanning line 0 -> line 1. Unit counting forms and
    # inspects every pair (not just the first) -> refuse byte-identically.
    raw = "***bold\n收尾***"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F1 (review) — GFM STRIKETHROUGH `~` spanning a soft newline. `~~old\ntext~~`
# was invisible to the gate (no `~` tracking at all): the blank line `_normalize`
# injects splits the strikethrough into two literal-`~~` paragraphs, and the
# symbol-blind `content_invariant` (the `~` is stripped as punctuation) approves
# the corruption. `~` is added to the SAME cell-global pairing stack as `*`/`_`
# (its OWN typed stack), with the SAME BOTH semantics as `*` (NOT the `_`
# intraword-literal rule below) -- because in remark-gfm 4.0.1 (the frontend's
# renderer, `remarkGfm` default options) a single tilde IS strikethrough and it
# pairs INTRAWORD across a soft newline. Verified in a node repl against that
# exact pipeline (unified().use(remarkParse).use(remarkGfm), inspecting the mdast
# `delete` node):
#     "~x~"              => delete? true   content "x"
#     "~a\nb~"           => delete? true   content "a\nb"   (single tilde crosses \n)
#     "~~old\ntext~~"    => delete? true   content "old\ntext"
#     "~~完整~~ 同行"     => delete? true   content "完整"   (same line, no cross)
#     "约~5ns"           => delete? false                    (lone `~`, no partner)
#     "~5ns\n延迟~"       => delete? true   content "5ns\n延迟" (two lone `~` pair across \n)
#     "~a\n\nb~"         => delete? false                    (BLANK line breaks it)
#     "foo~bar"          => delete? false                    (lone intraword `~`)
#     "foo~bar\nbaz~qux" => delete? true   content "bar\nbaz" (intraword `~` IS active)
# So: refuse iff two `~` pair across two lines (like emphasis); a LONE `~` (no
# partner anywhere) is literal and stays normalizable -- which per-line `$`-style
# run tracking could NOT express (it would refuse lone `约~5ns` too), forcing the
# cross-line stack. `~~~+` line-start fences are already refused elsewhere.
# ---------------------------------------------------------------------------


def test_double_tilde_strikethrough_spanning_soft_newline_refused():
    # RED before the fix: no `~` tracking -> gate passes -> blank-line split breaks
    # the strikethrough; content_invariant (symbol-blind) approves the corruption.
    raw = "~~old\ntext~~"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- strikethrough preserved


def test_double_tilde_strikethrough_same_line_still_normalizes():
    # a closed `~~...~~` on one line: opens and closes on the SAME line -> no pair
    # spans two lines -> still normalizable (no regression).
    raw = "文字~~删除~~更多"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


def test_escaped_tilde_is_not_a_delimiter_still_normalizes():
    # `\~\~` are escaped literal tildes -> no `~` run -> normalizable.
    raw = "价格\\~\\~转义"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


def test_lone_single_tilde_in_prose_still_normalizes():
    # `约~5ns` (~= "about 5ns"): a lone single `~` with no partner anywhere forms
    # NO strikethrough in remark-gfm (repl: delete? false) -> literal -> a blank
    # line cannot break a non-existent span -> normalizable. Per-line `$`-run
    # tracking would wrongly refuse this lone `~`; the cross-line stack keeps it.
    raw = "延迟约~5ns"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw          # single prose line -> no-op


def test_lone_single_tilde_run_alone_still_normalizes():
    raw = "~单个"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


def test_two_lone_tildes_pair_across_soft_newline_refused():
    # `~5ns\n延迟~`: a can-open-only `~` on line 0 and a can-close-only `~` on line 1
    # PAIR across the newline (repl: delete? true, content "5ns\n延迟") -> a blank line
    # would split the strikethrough -> refuse byte-identically.
    raw = "~5ns\n延迟~"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_intraword_tilde_pair_spanning_soft_newline_refused():
    # `foo~bar\nbaz~qux`: both `~` are word-flanked on BOTH sides (BOTH runs). Unlike
    # `_`, GFM strikethrough pairs intraword `~` ACROSS the newline (repl: delete?
    # true, content "bar\nbaz") -> refuse, exactly like intraword `*`.
    raw = "foo~bar\nbaz~qux"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_lone_intraword_tilde_still_normalizes():
    # `foo~bar` alone (lone intraword `~`, no partner) -> no strikethrough (repl:
    # delete? false) -> normalizable, mirroring the load-bearing `R*C`.
    raw = "foo~bar"
    assert is_rich_markdown(raw) is False
    assert rule_normalize(raw) == raw


def test_tilde_does_not_pair_with_star_or_underscore():
    # `~` has its OWN typed stack: a `~` run must never pop a `*`/`_` opener. Here a
    # real `*...*` pair crosses the newline while a lone `~` sits between -> the `*`
    # pair is still caught (the `~` cannot steal the `*` opener).
    raw = "*开头~中\n结尾*"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


# ---------------------------------------------------------------------------
# F2 (review) — CommonMark UNDERSCORE flanking: an intraword `_` (word char on
# BOTH sides) can NEITHER open nor close (unlike `*`). `_outer foo_bar\nclose_`:
# the real opener is the leading `_`, the real closer the trailing `_` -- they
# form ONE emphasis pair ACROSS the soft newline. The `_` inside `foo_bar` is
# intraword and LITERAL. The BOTH branch wrongly treated that intraword `_` as a
# real closer and POPPED the true opener, so the genuine cross-line `_...._` pair
# went undetected, the cell passed, and the injected blank line broke the
# emphasis -- a corruption the symbol-blind content_invariant misses. Verified
# in a node repl (unified().use(remarkParse).use(remarkGfm), mdast `emphasis`):
#     "foo_bar"                  => em? false                       (intraword literal)
#     "R_C"                      => em? false
#     "_same line_"              => em? true  content "same line"
#     "_outer foo_bar\nclose_"   => em? true  content "outer foo_bar\nclose"  (crosses \n)
#     "*open R_C\nclose*"        => em? true  content "open R_C\nclose"
#     "some_file\nother_file"    => em? false (two intraword `_`, NO emphasis)
# Fix: for `_` runs ONLY, when word chars flank BOTH sides, skip the run entirely
# (no push, no pop). `*`/`~` keep the BOTH semantics. Only-`_` is CommonMark's
# intraword-underscore rule; `*`/`~` legitimately pair intraword.
# ---------------------------------------------------------------------------


def test_intraword_underscore_literal_lets_real_pair_cross_line_refused():
    # THE codex hole: `_outer foo_bar\nclose_`. The intraword `_` in `foo_bar` must
    # NOT pop the leading `_` opener; the leading `_` then pairs with the trailing
    # `_` ACROSS the newline -> refuse byte-identically. RED before the fix (the
    # BOTH branch popped the opener, the cell passed, the emphasis got split).
    raw = "_outer foo_bar\nclose_"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw          # byte-identical -- emphasis preserved


def test_intraword_underscore_word_path_alone_still_normalizes():
    # `foo_bar` alone: a single intraword `_` (literal) -> no emphasis -> normalizable.
    assert is_rich_markdown("foo_bar") is False
    assert rule_normalize("foo_bar") == "foo_bar"


def test_two_intraword_underscores_across_lines_now_normalize():
    # `some_file\nother_file`: two intraword `_` form NO emphasis in CommonMark
    # (repl: em? false). Before the fix the BOTH branch pushed line 0's `_` then
    # popped it at line 1 -> a phantom cross-line pair -> WRONGLY refused. The
    # intraword-literal skip lets file-path-like prose normalize (content-safe).
    raw = "some_file\nother_file"
    assert is_rich_markdown(raw) is False
    assert content_invariant(raw, rule_normalize(raw)) is True


def test_star_intraword_still_pairs_across_line_regression():
    # regression: `*` is NOT subject to the intraword-literal rule. `*open R_C\nclose*`
    # -- the `*` opener/closer pair across the newline is still caught (the intraword
    # `_` between them is skipped, never interfering).
    raw = "*open R_C\nclose*"
    assert is_rich_markdown(raw) is True
    assert rule_normalize(raw) == raw


def test_underscore_emphasis_same_line_still_normalizes():
    # `_same line_` fully pairs on ONE line (leading `_` can-open, trailing `_`
    # can-close, both flanked by boundary/space, NOT intraword) -> normalizable.
    assert is_rich_markdown("_同一行_ 强调") is False
