"""Command-manual catalog extraction, the pure layer (v2: windows).

The fixtures are real command reference sections from the OpenROAD project
documentation (Apache-2.0), trimmed to the parts the assertions need. Using the
genuine article matters more here than usual: every threshold in
`command_catalog` was calibrated against this corpus, and the shapes that break
naive implementations — a plain English heading whose body opens with a code
block, a command documented in one line with a positional argument, a parameter
table living in a sibling section — are all shapes a hand-written fixture would
have quietly omitted.

Elements are written out directly rather than parsed from markdown, in the
spirit of test_exact_lookup's seeded chunks: the dicts here are exactly what
`source_elements_for_chunking` hands the production caller, so a failure points
at this module rather than at the markdown parser.

v2 replaced rule-based sectioning with lossless windowing, so the grouping and
shape-detection suites are gone. Everything below the geometry — grounding,
attribution, coverage, the ledger — is unchanged code and its cases are carried
over deliberately: those assertions are the regression asset, and the only
thing that moved under them is which object carries the text.
"""
from __future__ import annotations

import pytest

from app.services.command_catalog import (
    MAX_CANDIDATES,
    MAX_WINDOW_REJECTIONS,
    SLICE_PARAM_LIMIT,
    SPLIT_BOUNDARY_LOOKBACK_CHARS,
    WINDOW_CHARS,
    WINDOW_SPLIT_FLOOR_CHARS,
    ExtractionWindow,
    WindowElement,
    carry_candidates,
    catalog_stats,
    extraction_slices,
    extraction_windows,
    normalize_syntax,
    parameter_names,
    validate_entry,
    window_candidates,
    window_needs_model,
    window_outcome,
    window_segments,
)
from app.services.parsers import parse_markdown_text


# ------------------------------------------------------------------ fixtures
# OpenROAD `gpl` — the "plain heading, body opens with a syntax block" shape
# that a heading-only detector scores at zero. Raw strings throughout: the
# manual's line continuations are literal backslashes, and a cooked string
# would splice those lines together and destroy the very shape under test.
GPL_INTRO = (
    "When using the `-timing_driven` flag, `gpl` does a virtual "
    "`repair_design` to find slacks and weight nets with low slack. Use the "
    "`set_wire_rc` command to set resistance and capacitance of estimated "
    "wires used for timing."
)

GPL_SYNTAX = r"""global_placement
    [-skip_initial_place]\
    [-force_center_initial_place]\
    [-skip_nesterov_place]\
    [-timing_driven]\
    [-routability_driven]\
    [-incremental]\
    [-skip_io]\
    [-bin_grid_count grid_count]\
    [-density target_density]\
    [-init_density_penalty init_density_penalty]\
    [-init_wirelength_coef init_wirelength_coef]\
    [-min_phi_coef min_phi_coef]\
    [-max_phi_coef max_phi_coef]\
    [-reference_hpwl reference_hpwl]\
    [-overflow overflow]\
    [-initial_place_max_iter initial_place_max_iter]\
    [-initial_place_max_fanout initial_place_max_fanout]\
    [-routability_use_grt]\
    [-routability_target_rc_metric routability_target_rc_metric]\
    [-routability_check_overflow routability_check_overflow]\
    [-routability_snapshot_overflow routability_snapshot_overflow]\
    [-routability_max_density routability_max_density]\
    [-routability_inflation_ratio_coef routability_inflation_ratio_coef]\
    [-routability_max_inflation_ratio routability_max_inflation_ratio]\
    [-routability_rc_coefficients routability_rc_coefficients]\
    [-keep_resize_below_overflow keep_resize_below_overflow]\
    [-timing_driven_net_reweight_overflow timing_driven_net_reweight_overflow]\
    [-timing_driven_net_weight_max timing_driven_net_weight_max]\
    [-timing_driven_nets_percentage timing_driven_nets_percentage]\
    [-timing_driven_repair_timing]\
    [-timing_driven_repair_tns_end_percent timing_driven_repair_tns_end_percent]\
    [-pad_left pad_left]\
    [-pad_right pad_right]\
    [-disable_revert_if_diverge]\
    [-disable_pin_density_adjust]\
    [-enable_routing_congestion]\
    [-virtual_cts]\
    [-virtual_cts_max_skew_fraction virtual_cts_max_skew_fraction]\
    [-random_seed random_seed]\
    [-perturb_dist perturb_dist]"""

GPL_OPTIONS = (
    "| Switch Name | Description |\n"
    "| ----- | ----- |\n"
    "| `-timing_driven` | Enable timing-driven mode. |\n"
    "| `-density` | Set target density. The default value is `0.7` "
    "(i.e., 70%). Allowed values are floats `[0, 1]`. |\n"
    "| `-overflow` | Set target overflow for termination condition. The "
    "default value is `0.1`. |\n"
    "| `-pad_left` | Set left padding in terms of number of sites. The "
    "default value is `0`. |"
)

# OpenROAD `gpl` — a second command, so a window has more than one to offer.
CLUSTER_FLOPS_INTRO = "This command does flop clustering based on parameters."

CLUSTER_FLOPS_SYNTAX = r"""cluster_flops
    [-tray_weight tray_weight]\
    [-timing_weight timing_weight]\
    [-max_split_size max_split_size]\
    [-num_paths num_paths]\
    [-clock_power_weight clock_power_weight]"""

CLUSTER_FLOPS_OPTIONS = (
    "| Switch Name | Description |\n"
    "| ----- | ----- |\n"
    "| `-tray_weight` | Tray weight, default value is 32.0, type `float`. |\n"
    "| `-timing_weight` | Timing weight, default value is 0.1, type `float`. |\n"
    "| `-max_split_size` | Maximum split size, default value is 500 "
    "(-1 for no decomposition), type `int`.|\n"
    "| `-num_paths` | KIV, default value is 0, type `int`. |\n"
    "| `-clock_power_weight` | Credits clock-tree power saved by banking. "
    "Default value is 0.0, type `float`. |"
)

# OpenROAD `rsz` — a command documented in one line with a positional
# argument and no flags at all.
SET_DONT_USE_INTRO = (
    "The `set_dont_use` command removes library cells from consideration by "
    "the `resizer` engine and the `CTS` engine."
)
SET_DONT_USE_SYNTAX = "set_dont_use lib_cells "

# OpenROAD `gpl` — a command whose syntax block carries no flags either.
PLACEMENT_CLUSTER_SYNTAX = "placement_cluster\n    instance_patterns"


def _elements(*specs, source_id="src-manual"):
    """(element_type, section_path, text) rows, ids in document order.

    The shape `source_elements_for_chunking` returns on both backends.
    """
    return [
        {
            "id": f"el-{source_id}-{index:04d}",
            "element_type": element_type,
            "section_path": section_path,
            "text": text,
        }
        for index, (element_type, section_path, text) in enumerate(specs, 1)
    ]


def _dense_rows(count, *, description="A" * 80):
    """`count` commands in one document, each a usage line plus a paragraph.

    Sized so the whole thing fits one `WINDOW_CHARS` window on the character
    budget alone — which is exactly the shape that used to serve only the
    first `MAX_CANDIDATES` of them and lose the rest for good.
    """
    specs = []
    for index in range(count):
        specs.append(
            ("code_block", "", f"command_{index:03d} -flag_{index:03d} value")
        )
        specs.append(("paragraph", "", f"{description} number {index:03d}."))
    return _elements(*specs)


def _name_index_rows(count):
    """A bare index of `count` command names, under the split floor.

    The pathological density the splitter deliberately stops at: no
    documentation to divide, just names, so every piece of it would still
    over-serve and each piece would cost its own model call.
    """
    return _elements(
        ("code_block", "", "\n".join(f"cmd_{index:02d} -d" for index in range(count)))
    )


GPL_PATH = "Global Placement > Commands > Global Placement"
GPL_OPTIONS_PATH = f"{GPL_PATH} > Options"
FLOPS_PATH = "Global Placement > Commands > Cluster Flops"
FLOPS_OPTIONS_PATH = f"{FLOPS_PATH} > Options"


def _gpl_elements():
    return _elements(
        ("heading", GPL_PATH, "Global Placement"),
        ("paragraph", GPL_PATH, GPL_INTRO),
        ("code_block", GPL_PATH, GPL_SYNTAX),
        ("heading", GPL_OPTIONS_PATH, "Options"),
        ("table", GPL_OPTIONS_PATH, GPL_OPTIONS),
    )


def _flops_elements():
    # A different source_id, so ids stay unique when the two are concatenated
    # into one document — window packing reads ids, and two elements sharing
    # one would make the continuation/join boundary unreadable.
    return _elements(
        ("heading", FLOPS_PATH, "Cluster Flops"),
        ("paragraph", FLOPS_PATH, CLUSTER_FLOPS_INTRO),
        ("code_block", FLOPS_PATH, CLUSTER_FLOPS_SYNTAX),
        ("heading", FLOPS_OPTIONS_PATH, "Options"),
        ("table", FLOPS_OPTIONS_PATH, CLUSTER_FLOPS_OPTIONS),
        source_id="src-flops",
    )


def _window(text, *, heading="", provenance="", ordinal=0):
    """A hand-made window, for tests that need no packing.

    `heading` is a real heading ELEMENT rather than a label, because that is
    what `validate_entry`'s `suspect_related` reads — `provenance` is display
    only and must never reach a decision.
    """
    elements = []
    if heading:
        elements.append(
            WindowElement(
                id="el-head",
                element_type="heading",
                text=heading,
                section_path=provenance or heading,
            )
        )
    elements.append(
        WindowElement(id="el-0001", element_type="paragraph", text=text)
    )
    return ExtractionWindow(
        ordinal=ordinal,
        elements=tuple(elements),
        text="\n".join(element.text for element in elements),
        provenance=provenance or heading,
    )


@pytest.fixture
def flops_window():
    windows = extraction_windows(_flops_elements())
    assert len(windows) == 1
    return windows[0]


def _pack_only(rows, **kwargs):
    """The character-budget packing alone, with the density split turned off.

    A bound of zero means "no list to overflow", so the second pass never
    fires and this is what the packer alone produces. Used to state a
    fixture's PRECONDITION ("this is one window on size alone, so what follows
    is about density") rather than to assert behaviour.
    """
    return [
        window.text
        for window in extraction_windows(rows, max_candidates=0, **kwargs)
    ]


def _expected_text(rows):
    """What the whole element stream reads as, joined the way windows join."""
    return "\n".join(
        str(row["text"]).strip() for row in rows if str(row["text"]).strip()
    )


def _rebuild(windows):
    """Concatenate window texts back into the original stream's text.

    Every window boundary is one of two things, and `element_ids` says which:
    a CONTINUATION of one oversized element cut in two (the previous window's
    last contributing element is this window's first), or an ELEMENT JOIN — the
    single newline `extraction_windows` puts between two elements, which lands
    at a window boundary instead of inside a window's text. Reconstructing with
    that rule makes "nothing is lost" an exact string equality rather than a
    whitespace-insensitive comparison, which would also pass if a boundary had
    eaten a real character or duplicated one.
    """
    if not windows:
        return ""
    parts = [windows[0].text]
    for previous, current in zip(windows, windows[1:]):
        continuation = previous.element_ids[-1] == current.element_ids[0]
        parts.append("" if continuation else "\n")
        parts.append(current.text)
    return "".join(parts)


# ----------------------------------------------------------- 1. window packing
def test_document_packs_into_ordered_windows():
    windows = extraction_windows(_gpl_elements() + _flops_elements())

    assert [window.ordinal for window in windows] == list(range(len(windows)))
    # Small enough to share one window, which is the ordinary case and the
    # reason `window_candidates` has to offer more than one command.
    assert len(windows) == 1
    assert len(windows[0].element_ids) == 10


def test_window_text_is_exactly_its_own_elements(flops_window):
    """A grounding check that searched text the model never saw — or missed
    text it did — would pass hallucinations, so the two must agree."""
    assert flops_window.text == "\n".join(
        element.text for element in flops_window.elements
    )


def test_windows_respect_the_character_budget():
    rows = _gpl_elements() + _flops_elements()

    windows = extraction_windows(rows, max_chars=500)

    assert len(windows) > 1
    assert all(len(window.text) <= 500 for window in windows)


def test_elements_that_fit_are_never_cut_in_half():
    """"Whole element or next window" is the packing rule: a table split
    mid-row grounds worse than the same table one window later."""
    rows = _elements(
        ("paragraph", "", "a" * 60),
        ("paragraph", "", "b" * 60),
        ("paragraph", "", "c" * 60),
    )

    windows = extraction_windows(rows, max_chars=100)

    # Three windows of one whole element each — never 60+40 / 20+60 / 60.
    assert [window.text for window in windows] == ["a" * 60, "b" * 60, "c" * 60]
    assert [len(window.element_ids) for window in windows] == [1, 1, 1]


def test_oversized_element_is_split_across_windows_and_keeps_its_id():
    """v1 dropped an over-budget element whole. Measured, that turned a
    120-parameter table into 36 characters of heading text and a
    command-reject ratio of 0.0 — the failure had nothing left to show up in.
    Splitting is the fix, and each piece keeps the element's own id so a
    citation anchor still points at something a person can open."""
    rows = _elements(
        ("heading", "M > Big", "big_command options"),
        ("table", "M > Big", "z" * 250),
    )

    windows = extraction_windows(rows, max_chars=100)

    assert [window.element_ids for window in windows] == [
        ("el-src-manual-0001",),
        ("el-src-manual-0002",),
        ("el-src-manual-0002",),
        ("el-src-manual-0002",),
    ]
    assert "".join(window.text for window in windows[1:]) == "z" * 250


def test_a_split_inside_an_element_lands_between_tokens():
    """A cut at the raw character budget is the one boundary that can destroy
    a name outright.

    `global_placement` cut into `global_pl` + `acement` is in NEITHER window,
    so it is a candidate in neither, every entry claiming it is vetoed in both,
    and the command is gone — with no rejection or ratio anywhere to show it,
    because a name that was never served can never be answered wrongly. Backing
    the cut up to the nearest whitespace closes that: no command name and no
    flag contains whitespace, so a cut that lands on one cannot be inside
    either.
    """
    body = (
        "a" * 90
        + " global_placement -density 0.7\n"
        + "b" * 80
        + " cluster_flops -tray_weight 2\n"
    )
    rows = _elements(("code_block", "", body))
    # The precondition this test exists for: the naive cut at the budget has a
    # name character on both sides of it, i.e. it falls INSIDE a token.
    assert body[99].isalnum() and body[100].isalnum()

    windows = extraction_windows(rows, max_chars=100)

    assert _rebuild(windows) == _expected_text(rows)
    # Every token lands complete in exactly one window — never half in each.
    for token in ("global_placement", "-density", "cluster_flops", "-tray_weight"):
        holders = [window for window in windows if token in window.text]
        assert len(holders) == 1, token
    # And a token that survived whole is a token the scanners can still see:
    # the window that got `global_placement` opens on its usage line, so the
    # command is claimable there. Under a raw cut that window opens on
    # `acement -density 0.7` and offers nothing at all.
    assert "global_placement" in [
        name for window in windows for name in window_candidates(window)
    ]
    assert "-density" in [
        name for window in windows for name in parameter_names(window.text)
    ]


def test_a_split_with_no_boundary_within_reach_falls_at_the_budget():
    """Best effort, not a promise. A long run with no whitespace in it at all
    (a base64 blob, a minified line) has no boundary to find, and refusing to
    cut would break the budget the model call is sized by instead."""
    rows = _elements(("paragraph", "", "z" * 250))

    windows = extraction_windows(rows, max_chars=100)

    assert [len(window.text) for window in windows] == [100, 100, 50]
    assert _rebuild(windows) == _expected_text(rows)


def test_the_search_for_a_boundary_is_bounded():
    """The lookback is a bound, not a scan to the start of the element: a
    whitespace character further back than `SPLIT_BOUNDARY_LOOKBACK_CHARS`
    must not drag the cut — and the window it would leave behind — back with
    it."""
    budget = 1_000
    # One space, right at the start, then nothing but token characters.
    rows = _elements(("paragraph", "", "q " + "z" * 1_500))
    assert budget - SPLIT_BOUNDARY_LOOKBACK_CHARS > 2

    windows = extraction_windows(rows, max_chars=budget)

    assert len(windows[0].text) == budget
    assert _rebuild(windows) == _expected_text(rows)


@pytest.mark.parametrize(
    "rows, max_chars",
    [
        # Nothing at all.
        ([], 100),
        # Everything in one window.
        (_gpl_elements() + _flops_elements(), WINDOW_CHARS),
        # A budget that forces boundaries between elements.
        (_gpl_elements() + _flops_elements(), 500),
        # A budget smaller than every single element: every boundary is a cut.
        (_gpl_elements() + _flops_elements(), 40),
        # The degenerate budget.
        (_gpl_elements(), 1),
        # An element that is exactly the budget, flanked by others.
        (
            _elements(
                ("paragraph", "", "a" * 30),
                ("paragraph", "", "b" * 100),
                ("paragraph", "", "c" * 30),
            ),
            100,
        ),
        # Blank elements interleaved — they carry no characters to lose.
        (
            _elements(
                ("paragraph", "", "   "),
                ("paragraph", "", "kept text"),
                ("paragraph", "", ""),
            ),
            100,
        ),
        # Dense enough that the SECOND pass fires — the split that keeps a
        # candidate list from truncating moves boundaries too, and it must
        # lose no more than the first pass does. Element-boundary split…
        (_dense_rows(40), WINDOW_CHARS),
        # …and the same pass forced to cut INSIDE one element, because the
        # whole document is one code block.
        (
            _elements(
                (
                    "code_block",
                    "",
                    "\n".join(
                        f"command_{index:03d} -flag value" for index in range(60)
                    ),
                )
            ),
            WINDOW_CHARS,
        ),
    ],
)
def test_windowing_loses_and_duplicates_nothing(rows, max_chars):
    """The invariant the whole geometry change rests on.

    v1's packer had a discard branch, and silent loss there was invisible
    downstream: an entry can only be wrong about text it was shown, so text
    that was never shown produces no rejection, no ratio movement and no
    report line. This asserts the property directly, over shapes that exercise
    every branch of the packer — whole-element placement, "does not fit, open
    a new window", and "does not fit anywhere, cut it".
    """
    windows = extraction_windows(rows, max_chars=max_chars)

    assert _rebuild(windows) == _expected_text(rows)


@pytest.mark.parametrize("pad", ["　", "\xa0"])
def test_whitespace_normalisation_is_pinned_to_the_stores_trim_set(pad):
    """The packer must count exactly the characters SQL counted.

    `str.strip()` removes every Unicode space; the store's `TRIM`/`BTRIM`
    removes four ASCII ones. Strip more here and a document padded with
    U+3000 (the ideographic space CJK typesetting is full of) or NBSP has a
    smaller real total than the SQL sum reports — and the cost preview's
    window count, published as a LOWER bound, ends up above the truth. So
    these characters are CONTENT on both sides.
    """
    rows = _elements(
        ("paragraph", "", f"{pad * 5}set_thing_0 -density value{pad * 5}"),
        ("paragraph", "", pad * 40),  # nothing but wide spaces: still content
    )

    windows = extraction_windows(rows)

    assert len(windows) == 1
    # Both elements survive, padding included — nothing was trimmed away that
    # the SQL total still counts.
    assert len(windows[0].element_ids) == 2
    assert windows[0].text.startswith(pad)
    assert sum(len(window.text) for window in windows) >= sum(
        len(str(row["text"])) for row in rows
    )


def test_blank_elements_are_not_windowed():
    """No characters, nothing to ground against, nothing to cite — dropping
    them is not a content loss, and keeping them would put empty lines and
    dangling anchors in every prompt."""
    rows = _elements(
        ("paragraph", "", "   "),
        ("paragraph", "", "cluster_flops [-tray_weight tray_weight]"),
    )

    windows = extraction_windows(rows)

    assert len(windows) == 1
    assert windows[0].element_ids == ("el-src-manual-0002",)


def test_empty_stream_yields_no_windows():
    assert extraction_windows([]) == []


def test_provenance_follows_the_last_heading_and_is_inherited():
    rows = _elements(
        ("heading", GPL_PATH, "Global Placement"),
        ("paragraph", GPL_PATH, "a" * 90),
        ("paragraph", GPL_PATH, "b" * 90),
        ("heading", FLOPS_PATH, "Cluster Flops"),
        ("paragraph", FLOPS_PATH, "c" * 90),
    )

    windows = extraction_windows(rows, max_chars=100)

    assert [window.provenance for window in windows] == [
        # The heading's own breadcrumb…
        GPL_PATH,
        # …inherited by every window that carries its body…
        GPL_PATH,
        GPL_PATH,
        # …until the next heading arrives.
        FLOPS_PATH,
        FLOPS_PATH,
    ]


def test_a_continuation_inherits_the_previous_windows_LAST_heading():
    """A window that holds several headings labels ITSELF with the first and
    hands the LAST one forward.

    Both halves are the same question asked about different positions: the
    window opens in `set_a`, so that is what it is called; it ENDS in `set_e`,
    so a continuation of it is in `set_e`. Inheriting the first heading again
    (which "the window's own label" naively does) labels the continuation of a
    `set_e` options table `set_a` — pointing a reviewer several commands back,
    at a command whose parameters those are not.
    """
    rows = _elements(
        ("heading", "M > set_a", "set_a"),
        ("paragraph", "M > set_a", "set_a -one value"),
        ("heading", "M > set_e", "set_e"),
        ("paragraph", "M > set_e", "set_e -two value"),
        # A continuation with no heading of its own: the options table.
        ("table", "", "| `-three` | the third option |"),
    )

    windows = extraction_windows(rows, max_chars=45)

    assert [window.provenance for window in windows] == [
        # Two headings in the first window: it is called by the first…
        "M > set_a",
        # …and its continuation, which has no heading of its own, is called by
        # the LAST heading the previous window held.
        "M > set_e",
    ]


def test_provenance_falls_back_to_the_title_without_breadcrumbs():
    """The MinerU shape: headings, no section paths."""
    rows = _elements(
        ("heading", "", "Cluster Flops"),
        ("paragraph", "", CLUSTER_FLOPS_INTRO),
    )

    assert extraction_windows(rows)[0].provenance == "Cluster Flops"


def test_document_without_any_heading_has_empty_provenance():
    rows = _elements(("paragraph", "", CLUSTER_FLOPS_INTRO))

    assert extraction_windows(rows)[0].provenance == ""


# ------------------------------------------------------------- 2. candidates
def test_candidates_offer_every_command_the_window_documents():
    """The reason `MAX_CANDIDATES` grew: a window is a slab of the document,
    not one command's section, so several commands routinely share one and all
    of them must be legally claimable."""
    window = extraction_windows(_gpl_elements() + _flops_elements())[0]

    candidates = window_candidates(window)

    assert "global_placement" in candidates
    assert "cluster_flops" in candidates


def test_heading_identifiers_lead_the_candidate_list():
    rows = _elements(
        ("heading", "Manual > set_db", "set_db"),
        ("code_block", "Manual > set_db", "report_timing -digits 3"),
    )

    window = extraction_windows(rows)[0]

    assert window_candidates(window)[0] == "set_db"


def test_code_block_usage_lines_outrank_prose():
    """Measured on OpenROAD `rsz`: the section documenting
    `replace_arith_modules` opens with prose citing `link_design top -hier` (a
    cross-reference to a different command), and the code block naming the
    documented command comes after it in document order. Plain document order
    puts the wrong name first, where truncation is most likely to spare it."""
    rows = _elements(
        ("paragraph", "", "link_design top -hier"),
        ("code_block", "", "replace_arith_modules\n    [-path_count paths]"),
    )

    window = extraction_windows(rows)[0]

    assert window_candidates(window) == ["replace_arith_modules", "link_design"]


def test_flag_names_never_become_candidates(flops_window):
    """A syntax block's flags would crowd the real names off the list."""
    candidates = window_candidates(flops_window)

    assert "tray_weight" not in candidates
    assert "-tray_weight" not in candidates
    assert "max_split_size" not in candidates


def test_inline_code_identifiers_are_offered():
    window = extraction_windows(_gpl_elements())[0]

    candidates = window_candidates(window)

    assert "global_placement" in candidates
    # Named only in the prose, via inline code.
    assert "repair_design" in candidates
    assert "set_wire_rc" in candidates


def test_candidate_list_is_capped():
    """32, not v1's 16: with one command per section 16 was ample, and with a
    12k slab of a reference manual it silently vetoes whatever is documented
    after the sixteenth name."""
    assert MAX_CANDIDATES == 32
    rows = _elements(
        ("code_block", "", "\n".join(f"command_{index} -flag" for index in range(40)))
    )
    window = extraction_windows(rows)[0]

    assert len(window_candidates(window)) == MAX_CANDIDATES
    assert len(window_candidates(window, limit=3)) == 3


def test_a_window_naming_more_commands_than_the_list_holds_is_split():
    """Truncating the list is a PERMANENT loss, so the window is split instead.

    Windows do not overlap and are never revisited, so a name cut off the
    thirty-third position is not served later — it is served nowhere, in this
    run or any other, and nothing downstream can see it go (an entry can only
    be wrong about a name it was offered). Splitting costs one more model call
    and loses nothing: the pieces are ordinary windows and every character
    still lands in exactly one of them.
    """
    rows = _dense_rows(40)
    # One window on the character budget alone — the split is about DENSITY.
    assert len(_pack_only(rows)) == 1

    windows = extraction_windows(rows)

    assert len(windows) > 1
    assert all(
        len(window_candidates(window)) <= MAX_CANDIDATES for window in windows
    )
    assert all(window.candidates_overflowed == 0 for window in windows)
    # Every one of the forty is claimable somewhere. That is the property; the
    # number of windows it takes is not.
    offered = {name for window in windows for name in window_candidates(window)}
    assert {f"command_{index:03d}" for index in range(40)} <= offered
    assert _rebuild(windows) == _expected_text(rows)


def test_a_single_element_too_dense_to_serve_is_split_inside_itself():
    """A document that is one big code block has no element boundary to split
    on, so the split falls inside the element — at a token-safe point, like
    every other in-element cut."""
    rows = _elements(
        (
            "code_block",
            "",
            "\n".join(f"command_{index:03d} -flag value" for index in range(60)),
        )
    )

    windows = extraction_windows(rows)

    assert len(windows) > 1
    assert all(
        len(window_candidates(window)) <= MAX_CANDIDATES for window in windows
    )
    offered = {name for window in windows for name in window_candidates(window)}
    assert {f"command_{index:03d}" for index in range(60)} <= offered
    assert _rebuild(windows) == _expected_text(rows)


def test_a_name_index_below_the_floor_truncates_and_counts_what_it_dropped():
    """Where splitting stops being worth it, and what is owed when it does.

    Below `WINDOW_SPLIT_FLOOR_CHARS` a window that still names more commands
    than the list can carry is not documentation whose commands got crowded
    out — it is a bare index of names (a "see also" block, a summary table).
    Splitting one buys nothing (each piece is still an index) and costs a model
    call per piece. So the list truncates there, and the residue is DISCLOSED:
    it is the one remaining place a command is dropped without any downstream
    trace at all.
    """
    rows = _name_index_rows(40)
    assert len(_pack_only(rows)[0]) <= WINDOW_SPLIT_FLOOR_CHARS

    windows = extraction_windows(rows)

    assert len(windows) == 1
    assert len(window_candidates(windows[0])) == MAX_CANDIDATES
    assert windows[0].candidates_overflowed == 40 - MAX_CANDIDATES


def test_the_dropped_names_reach_the_window_ledger(flops_window):
    """`candidates_overflowed` is only useful if it travels: the job layer
    reads outcomes, not windows."""
    dense = extraction_windows(_name_index_rows(40))[0]

    assert window_outcome(dense, window_candidates(dense), []).candidates_overflowed == (
        40 - MAX_CANDIDATES
    )
    # …and stays 0 on every ordinary window, so a non-zero value in a job
    # report means what it says.
    assert window_outcome(
        flops_window, window_candidates(flops_window), []
    ).candidates_overflowed == 0


def _long_element(*, prose_lines, tail):
    """One element of `prose_lines` non-command lines followed by `tail`.

    A single element is the point: v1's 200-line scan cap applied per TEXT, so
    one flattened options table or one long code block was enough to push a
    real command past it. The prose lines carry no `_`/`.`-joined leading
    token, so nothing before the tail can be mistaken for a usage line.
    """
    prose = "\n".join(
        f"just some ordinary prose on line {index}" for index in range(prose_lines)
    )
    return _elements(("code_block", "", f"{prose}\n{tail}"))


def test_a_command_documented_past_line_200_is_still_offered():
    """v1 capped the usage-line scan at 200 lines per text. In v2 that cap was
    a silent, permanent loss: a window is a slab of a document rather than one
    command's section, one element can be 300 lines of flattened table, and a
    name the scan never reaches is never served — so it can never be claimed,
    and it produces no rejection, no ratio movement and no report line to show
    it went missing. The character budget is the real bound; the line cap only
    ever subtracted from it."""
    rows = _long_element(prose_lines=250, tail="late_command -flag value")
    window = extraction_windows(rows)[0]
    # The command really is past the old cut, in one element.
    assert window.text.count("\n") > 200

    assert window_candidates(window) == ["late_command"]


def test_the_density_split_sees_the_commands_past_line_200_too():
    """The cap did not just hide names from the prompt — it hid them from
    `_dense_overflow`, so the window that should have been SPLIT to make room
    for them was not split either. Both halves of that failure are one fix."""
    tail = "\n".join(f"command_{index:03d} -flag value" for index in range(40))
    rows = _long_element(prose_lines=210, tail=tail)

    windows = extraction_windows(rows)

    assert len(windows) > 1
    offered = {name for window in windows for name in window_candidates(window)}
    assert {f"command_{index:03d}" for index in range(40)} <= offered
    assert all(window.candidates_overflowed == 0 for window in windows)
    assert _rebuild(windows) == _expected_text(rows)


def test_prose_sentences_do_not_become_candidates():
    """A flag inside a sentence must not let the flag-carrying branch skip the
    sentence-punctuation and snake_case guards: every name those guards let
    through is a name a hallucinated entry could then legally claim.

    Four failing shapes, two mechanisms: `Fig.2 shows the -density sweep.`
    and `config.yaml lists the -density defaults` are dotted, not snake_case
    (rejected regardless of punctuation — `config.yaml`'s sentence
    deliberately has no trailing period, so only that guard can catch it);
    `global_placement accepts -density values.` and its Chinese-punctuation
    twin are snake_case but end in a sentence (English and Chinese
    respectively — "(中英文句读)").
    """
    rows = _elements(
        ("paragraph", "", "Fig.2 shows the -density sweep."),
        ("paragraph", "", "global_placement accepts -density values."),
        ("paragraph", "", "global_placement 会读取 -density 参数。"),
        ("paragraph", "", "config.yaml lists the -density defaults"),
        ("paragraph", "", "global_placement performs the placement step."),
    )

    window = extraction_windows(rows)[0]

    assert window_candidates(window) == []


def test_numbered_headings_are_not_offered_as_commands():
    """A survey's `A.1.2 Notation` clears the ASCII-letter, minimum-length and
    dotted-separator gates; only the numbering filter keeps a paper's appendix
    out of the candidate list."""
    rows = _elements(
        ("heading", "", "A.1.2 Notation"),
        ("heading", "", "B.2.1 Hyperparameters"),
        ("paragraph", "", "Table A.1 lists the symbols used in the paper."),
    )

    window = extraction_windows(rows)[0]

    assert window_candidates(window) == []


def test_one_line_command_with_a_positional_argument_is_offered():
    """`set_dont_use lib_cells` — no flags, no bracket list, still a command.

    Omitting this shape is not a corner case: it lost five real commands in
    one OpenROAD module.
    """
    rows = _elements(
        ("heading", "", "Set Don't Use"),
        ("paragraph", "", SET_DONT_USE_INTRO),
        ("code_block", "", SET_DONT_USE_SYNTAX),
    )

    window = extraction_windows(rows)[0]

    assert "set_dont_use" in window_candidates(window)


# ------------------------------------------------------ 2b. candidate relay
def _carry_chain(windows):
    """The relay as the job layer runs it: one `carry_candidates` per window."""
    chain = []
    carry: list[str] = []
    for window in windows:
        chain.append(list(carry))
        carry = carry_candidates(window_candidates(window), carry)
    return chain


def test_carry_hands_the_previous_windows_own_candidates_forward():
    assert carry_candidates(["cluster_flops"], []) == ["cluster_flops"]


def test_carry_passes_through_a_window_that_names_nothing():
    """The case the relay exists for: a multi-window parameter table names no
    command in any window after the first, so a relay that stopped at the first
    nameless window would stop exactly where it was needed."""
    carry = carry_candidates(["big_command"], [])

    # Three nameless windows in a row — the table just keeps going.
    for _ in range(3):
        carry = carry_candidates([], carry)
        assert carry == ["big_command"]


def test_carry_resets_when_a_window_names_something_new():
    """A new command's heading is the old command's end. Without the reset,
    the manual's first command stays claimable on its last page, and every
    stray parameter in the book can be keyed to it."""
    carry = carry_candidates([], ["big_command"])
    assert carry == ["big_command"]

    carry = carry_candidates(["other_command"], carry)

    assert carry == ["other_command"]
    assert "big_command" not in carry


def test_carry_truncation_keeps_the_nearest_windows_names():
    """`limit` is the same prompt-borne constraint the candidate list is capped
    by, and the names that just went out of view are the better evidence."""
    own = [f"command_{index}" for index in range(40)]

    carried = carry_candidates(own, ["long_ago_command"])

    assert carried == own[:MAX_CANDIDATES]
    assert "long_ago_command" not in carried
    assert carry_candidates(own, [], limit=3) == own[:3]


def test_carry_chain_runs_across_a_real_document():
    rows = _elements(
        ("heading", "M > set_db", "set_db"),
        ("code_block", "M > set_db", "set_db -name value"),
        ("table", "M > set_db", "| `-name` | The property name. |"),
        ("table", "M > set_db", "| `-value` | The property value. |"),
    )
    # Heading + usage line, then one window per table row.
    windows = extraction_windows(rows, max_chars=40)
    assert len(windows) == 3

    chain = _carry_chain(windows)

    # Nothing to relay into the first window…
    assert chain[0] == []
    # …and `set_db` reaches every window after the one that named it.
    assert all("set_db" in carried for carried in chain[1:])


# ------------------------------------------------------------- 3. skip gate
def test_prose_window_needs_no_model_call():
    """The deterministic cost gate that replaces v1's sectioning: no
    claimable name and no flag means the grounded output is provably empty,
    so the call is pure spend. A manual's introduction, its licence and any
    paper bound into the same PDF go by free."""
    rows = _elements(
        ("heading", "", "Introduction"),
        ("paragraph", "", "Placement algorithms trade wirelength against timing."),
        ("paragraph", "", "This chapter motivates the approach and its history."),
    )

    window = extraction_windows(rows)[0]

    assert window_candidates(window) == []
    assert parameter_names(window.text) == []
    assert window_needs_model(window) is False


def test_window_with_a_claimable_command_needs_a_call(flops_window):
    assert window_needs_model(flops_window) is True


def test_parameter_table_with_a_relayed_name_needs_a_call():
    """The half that is easy to drop and expensive to lose: a parameter table
    whose command heading fell in the PREVIOUS window has no candidate name of
    its own, and it is exactly what the cross-window merge exists to fold onto
    the command it belongs to. Gating on candidates alone would throw away
    half of every large command."""
    rows = _elements(("table", "", GPL_OPTIONS))

    window = extraction_windows(rows)[0]

    assert window_candidates(window) == []
    assert parameter_names(window.text)
    assert window_needs_model(window, ["global_placement"]) is True


def test_parameter_table_with_nothing_to_claim_is_skipped():
    """The other half of the same shape, and the one the earlier "any of the
    three" rule got wrong: parameters with NO claimable name — a table opening
    the document, or one whose relay an intervening command already reset —
    can produce nothing. Every entry a model returned would name something off
    the served list, and the served list is empty, so grounding vetoes all of
    it before the call is even made."""
    rows = _elements(("table", "", GPL_OPTIONS))

    window = extraction_windows(rows)[0]

    assert parameter_names(window.text)
    assert window_needs_model(window) is False
    assert window_needs_model(window, []) is False


def test_numbering_only_window_is_skipped():
    rows = _elements(
        ("heading", "", "A.1.2 Notation"),
        ("paragraph", "", "Table A.1 lists the symbols used in the paper."),
    )

    assert window_needs_model(extraction_windows(rows)[0]) is False


def test_a_relay_alone_does_not_keep_the_gate_open_over_prose():
    """A relay is a NAME, not evidence there is anything to extract.

    The relay never empties once a command has been seen, so "any relay keeps
    the gate open" bills a call for every prose page in the rest of the book —
    a manual is mostly prose, and the second half of it costs full price for
    windows that document nothing. The cost of that rule is the occasional
    worked example or prose description of a continuing command; the cost of
    keeping it is the whole document.
    """
    rows = _elements(
        ("paragraph", "", "The command reads the design and reports slack."),
    )
    window = extraction_windows(rows)[0]

    assert parameter_names(window.text) == []
    assert window_needs_model(window) is False
    assert window_needs_model(window, ["big_command"]) is False


def test_the_gate_is_the_two_condition_formula_in_all_four_shapes():
    """`needs_model ⟺ own or (flags and carried)`, enumerated.

    Each row is a different real shape, and three of the four are cases an
    "any of the three signals" rule gets wrong in one direction or the other.
    """
    named = extraction_windows(
        _elements(("code_block", "", "cluster_flops -tray_weight 32"))
    )[0]
    flagged = extraction_windows(_elements(("table", "", GPL_OPTIONS)))[0]
    prose = extraction_windows(
        _elements(("paragraph", "", "The command reads the design."))
    )[0]

    assert window_candidates(named) and parameter_names(named.text)
    assert not window_candidates(flagged) and parameter_names(flagged.text)
    assert not window_candidates(prose) and not parameter_names(prose.text)

    # own non-empty -> call, whatever the relay holds.
    assert window_needs_model(named, []) is True
    assert window_needs_model(named, ["other_command"]) is True
    # own empty + flags + relay -> call (a continuation window).
    assert window_needs_model(flagged, ["big_command"]) is True
    # own empty + flags + no relay -> skip (nothing claimable).
    assert window_needs_model(flagged, []) is False
    # own empty + no flags -> skip, relay or not (prose).
    assert window_needs_model(prose, ["big_command"]) is False
    assert window_needs_model(prose, []) is False


def test_the_gate_accepts_a_precomputed_candidate_list():
    """The job layer needs the window's own candidates anyway (for the prompt
    and to advance the relay), and re-scanning a 12k window per window is
    measurable on a long manual. Passing them must not change the answer."""
    window = extraction_windows(_flops_elements())[0]
    own = window_candidates(window)

    assert window_needs_model(window, own=own) is True
    assert window_needs_model(window, own=[]) is False
    assert window_needs_model(window, ["relayed_command"], own=[]) is True


# ---------------------------------------------------------------- 4. slicing
def test_small_window_is_one_slice_carrying_the_overview(flops_window):
    slices = extraction_slices(flops_window)

    assert len(slices) == 1
    assert slices[0].include_overview is True
    assert slices[0].param_names == (
        "-tray_weight",
        "-timing_weight",
        "-max_split_size",
        "-num_paths",
        "-clock_power_weight",
    )


def test_window_without_parameters_still_gets_a_slice():
    window = _window(PLACEMENT_CLUSTER_SYNTAX)

    slices = extraction_slices(window)

    assert len(slices) == 1
    assert slices[0].param_names == ()
    assert slices[0].include_overview is True


def test_hundred_parameter_window_splits_into_at_least_five_slices():
    """C0 measured a ~100-parameter section hitting `finish_reason=length`
    and then returning empty content on retry — silent loss, not an error."""
    body = "big_command\n" + "\n".join(
        f"    [-param_{index:02d} value_{index:02d}]" for index in range(100)
    )
    window = _window(body)

    slices = extraction_slices(window)

    assert len(slices) >= 5
    assert all(len(item.param_names) <= SLICE_PARAM_LIMIT for item in slices)
    # Only the first slice carries syntax/description/examples, so they are
    # asked for exactly once.
    assert [item.include_overview for item in slices] == [True] + [False] * (
        len(slices) - 1
    )
    assert all(item.total == len(slices) for item in slices)


def test_slice_parameter_subsets_partition_the_windows_parameter_list():
    body = "big_command\n" + "\n".join(
        f"    [-param_{index:02d} value_{index:02d}]" for index in range(100)
    )
    window = _window(body)

    slices = extraction_slices(window)
    flattened = [name for item in slices for name in item.param_names]

    assert flattened == parameter_names(window.text)
    assert len(flattened) == len(set(flattened)) == 100


def test_a_multi_command_window_assigns_every_command_s_flags():
    """The assignment is the WINDOW's flag list, not one command's — the model
    keys each parameter to the entry it belongs to, and attribution stays
    exact because `validate_entry` checks against this same list."""
    window = extraction_windows(_gpl_elements() + _flops_elements())[0]

    assigned = [name for item in extraction_slices(window) for name in item.param_names]

    assert "-density" in assigned  # global_placement's
    assert "-tray_weight" in assigned  # cluster_flops'


def test_real_large_window_engages_slicing():
    window = extraction_windows(_gpl_elements())[0]

    slices = extraction_slices(window)

    assert len(slices) >= 2
    assert "-density" in slices[0].param_names


def test_every_slice_sees_the_whole_window():
    """Each slice's prompt view is the WHOLE window, not a narrowed one.

    The narrowed view (an overview head plus the lines mentioning this slice's
    own parameters) was right for v1, where a slice was one command's section,
    and is systematically wrong for v2, where it is a chunk of a multi-command
    slab: the head holds the FIRST command and everything after it is asked
    about but never shown. `test_every_served_candidate_is_visible_in_the_view`
    below is the property that matters; this pins the mechanism.
    """
    body = "big_command\n" + "\n".join(
        f"    [-param_{index:02d} value_{index:02d}]" for index in range(100)
    )
    window = _window(body)

    slices = extraction_slices(window)

    assert len(slices) >= 5
    assert all(item.text_window == window.text for item in slices)
    # Every slice can still see every parameter — including the ones assigned
    # to its siblings, which is what makes attribution the ASSIGNMENT's job
    # rather than the view's.
    for item in slices:
        for name in parameter_names(window.text):
            assert name in item.text_window


@pytest.mark.parametrize("candidate_count", [30, 40])
def test_every_served_candidate_is_visible_in_the_view(candidate_count):
    """The guard the narrowed view failed, in both shapes that broke it.

    A name in the candidate list is a name the prompt invites the model to
    claim, and `validate_entry` then demands it appear verbatim in the window.
    A view that does not show it therefore leaves exactly two outcomes: the
    model says nothing about that command, or it fabricates and is vetoed.
    Either way the command is lost, and nothing downstream can tell — a
    command that was never really offered produces no rejection and no ledger
    line.

    Both shapes are measured failures of the 600-char head + parameter-lines
    view: a window of `candidate_count` flagless commands (empty assignment,
    so the view was the head and nothing else — only the first two or three
    commands visible), and a window whose commands DO carry flags (the
    parameter lines came back, but the command names above them did not).
    """
    flagless = _elements(
        *(
            ("code_block", "", f"command_{index:02d} operand_{index:02d}")
            for index in range(candidate_count)
        )
    )
    flagged = _elements(
        *(
            ("code_block", "", f"command_{index:02d} -flag_{index:02d} value")
            for index in range(candidate_count)
        )
    )

    for rows in (flagless, flagged):
        window = extraction_windows(rows)[0]
        served = window_candidates(window)
        assert served, "fixture must actually serve candidates"

        for item in extraction_slices(window):
            for name in served:
                assert name in item.text_window


def test_first_slice_view_carries_the_overview_and_syntax_block(flops_window):
    slices = extraction_slices(flops_window)

    assert len(slices) == 1
    assert slices[0].text_window
    assert "cluster_flops" in slices[0].text_window


def test_window_without_parameters_still_gets_a_nonempty_view():
    window = _window(PLACEMENT_CLUSTER_SYNTAX)

    slices = extraction_slices(window)

    assert slices[0].text_window


def test_parameter_names_keep_their_dash_and_skip_prose():
    text = (
        "Use -timing_driven for state-of-the-art results.\n"
        "- A markdown bullet is not a flag.\n"
        "The non-virtual repair runs at -0.5 slack.\n"
        "[-density target_density]"
    )

    assert parameter_names(text) == ["-timing_driven", "-density"]


# ------------------------------------------- 4b. per-command evidence segments
# The two-command window the whole segmentation exists for: `foo_cmd` takes a
# POSITIONAL `density`, `bar_cmd` takes the FLAG `-density`. Both names are in
# the same window, so a whole-window haystack cannot tell the two apart —
# see `WindowSegments`.
_FOO_BAR_ROWS = (
    ("heading", "M > foo_cmd", "foo_cmd"),
    ("code_block", "M > foo_cmd", "foo_cmd density"),
    ("paragraph", "M > foo_cmd", "`density` is the target density, positional."),
    ("heading", "M > bar_cmd", "bar_cmd"),
    ("code_block", "M > bar_cmd", "bar_cmd -density value"),
    ("paragraph", "M > bar_cmd", "| `-density` | Set target density. |"),
)


@pytest.fixture
def foo_bar_window():
    windows = extraction_windows(_elements(*_FOO_BAR_ROWS))
    assert len(windows) == 1  # one window, two commands: the shape under test
    return windows[0]


def test_a_flag_from_another_commands_table_is_not_grounded(foo_bar_window):
    """codex's counter-example, direction 1.

    `-density` is really in the window — in `bar_cmd`'s options table — so a
    whole-window haystack accepts it filed under `foo_cmd`, which does not
    take it. The name is real and the flag is real; only the ATTRIBUTION is
    invented, and that is exactly what no amount of "is this string present"
    can catch.
    """
    candidates = window_candidates(foo_bar_window)
    assert {"foo_cmd", "bar_cmd"} <= set(candidates)

    result = validate_entry(
        {"command_name": "foo_cmd", "args": [{"name": "-density"}]},
        foo_bar_window,
        candidates,
    )

    assert result.accepted is True  # the entry itself is fine
    assert [arg.name for arg in result.entry.args] == []
    assert [(item.field, item.value) for item in result.rejections] == [
        ("arg", "-density")
    ]
    # …and it still grounds where it belongs.
    kept = validate_entry(
        {"command_name": "bar_cmd", "args": [{"name": "-density"}]},
        foo_bar_window,
        candidates,
    )
    assert [arg.name for arg in kept.entry.args] == ["-density"]


def test_a_commands_own_positional_argument_survives_a_neighbours_flag(
    foo_bar_window,
):
    """codex's counter-example, direction 2 — the same defect, more damaging.

    `foo_cmd density` is a real positional argument documented in the window.
    Against the whole window, `_check_arg_name` finds `-density` (bar_cmd's)
    and reports the dropped-dash infidelity it was written to catch, deleting
    a correct parameter. Segmenting removes the evidence that was never
    foo_cmd's to begin with.
    """
    candidates = window_candidates(foo_bar_window)

    result = validate_entry(
        {"command_name": "foo_cmd", "args": [{"name": "density"}]},
        foo_bar_window,
        candidates,
    )

    assert result.rejections == ()
    assert [arg.name for arg in result.entry.args] == ["density"]


def test_each_command_in_a_shared_window_grounds_inside_its_own_segment():
    """Several commands, several segments, no leakage in either direction."""
    rows = _elements(
        ("heading", "M > set_a", "set_a"),
        ("code_block", "M > set_a", "set_a -alpha value"),
        ("heading", "M > set_b", "set_b"),
        ("code_block", "M > set_b", "set_b -beta value"),
    )
    window = extraction_windows(rows)[0]
    candidates = window_candidates(window)

    segments = window_segments(window, candidates)

    assert "-alpha" in segments.evidence("set_a")
    assert "-beta" not in segments.evidence("set_a")
    assert "-beta" in segments.evidence("set_b")
    assert "-alpha" not in segments.evidence("set_b")
    assert segments.prelude == ""


def test_an_inline_cross_reference_does_not_open_a_segment():
    """A SEE ALSO line is how a manual points at its neighbours. Treating it
    as an anchor would hand the parameter table that follows to the
    cross-referenced command — recreating, inside one window, the very
    mis-attribution segmenting removes."""
    rows = _elements(
        ("heading", "M > set_a", "set_a"),
        ("code_block", "M > set_a", "set_a -alpha value"),
        ("paragraph", "M > set_a", "See also `set_b` for the beta options."),
        ("paragraph", "M > set_a", "| `-gamma` | Another set_a option. |"),
    )
    window = extraction_windows(rows)[0]
    candidates = window_candidates(window)
    assert "set_b" in candidates  # offered, because inline code names it

    segments = window_segments(window, candidates)

    # The table after the cross-reference is still set_a's.
    assert "-gamma" in segments.evidence("set_a")
    # And set_b, merely mentioned, has no evidence at all — so an entry
    # claiming it keeps its name and loses its body.
    assert segments.evidence("set_b") == ""
    result = validate_entry(
        {"command_name": "set_b", "args": [{"name": "-gamma"}]},
        window,
        candidates,
    )
    assert result.accepted is True
    assert result.entry.args == ()
    assert result.entry.suspect_related is True


def test_a_continuation_window_is_all_prelude_and_still_grounds():
    """The relay's own shape: a window with no anchor at all is one prelude,
    and a carried claim grounds against it. Without that the segmentation
    would silently delete every multi-window options table — the case the
    relay exists for."""
    rows = _elements(
        ("paragraph", "", "| `-tray_weight` | Tray weight, default 32.0. |"),
        ("paragraph", "", "| `-timing_weight` | Timing weight. |"),
    )
    window = extraction_windows(rows)[0]
    assert window_candidates(window) == []

    segments = window_segments(window, [], ["cluster_flops"])
    assert "-tray_weight" in segments.prelude
    assert segments.by_command == {}

    result = validate_entry(
        {
            "command_name": "cluster_flops",
            "args": [{"name": "-tray_weight", "default": "32.0"}],
        },
        window,
        [],
        carried=["cluster_flops"],
    )

    assert result.accepted is True
    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-tray_weight", "32.0")
    ]


def test_a_command_documented_twice_owns_both_of_its_runs():
    """Manuals interleave: a command's `Examples` section comes back after a
    neighbour's. Taking only the first run would drop the second half of a
    command's own documentation."""
    rows = _elements(
        ("heading", "M > set_a", "set_a"),
        ("code_block", "M > set_a", "set_a -alpha value"),
        ("heading", "M > set_b", "set_b"),
        ("code_block", "M > set_b", "set_b -beta value"),
        ("heading", "M > set_a examples", "set_a"),
        ("paragraph", "M > set_a examples", "| `-omega` | A late option. |"),
    )
    window = extraction_windows(rows)[0]
    candidates = window_candidates(window)

    segments = window_segments(window, candidates)

    evidence = segments.evidence("set_a")
    assert "-alpha" in evidence and "-omega" in evidence
    assert "-beta" not in evidence


def test_the_window_assignment_and_coverage_stay_window_level(foo_bar_window):
    """Segmenting decides where an answer must GROUND, never what was ASKED.

    The slice's assignment is the window's whole flag list — the model keys
    each parameter onto the entry it belongs to — and the coverage ledger
    counts against that same list. Segmenting either would turn "the model
    never answered this parameter" into "the model filed it elsewhere", which
    is a different fact and the one `args_keep_ratio` must not lose.
    """
    assert parameter_names(foo_bar_window.text) == ["-density"]
    assert [
        slice_.param_names for slice_ in extraction_slices(foo_bar_window)
    ] == [("-density",)]


# ------------------------------------------------------------- 5. validation
def _flops_entry(**overrides):
    entry = {
        "command_name": "cluster_flops",
        "syntax": CLUSTER_FLOPS_SYNTAX,
        "description": "Clusters flops into multi-bit trays.",
        "args": [
            {"name": "-tray_weight", "required": False, "desc": "Tray weight.",
             "default": "32.0"},
            {"name": "-timing_weight", "required": False, "desc": "Timing.",
             "default": "0.1"},
            {"name": "-max_split_size", "required": False, "desc": "Split.",
             "default": "500"},
            {"name": "-num_paths", "required": False, "desc": "KIV.",
             "default": "0"},
            {"name": "-clock_power_weight", "required": False,
             "desc": "Clock power credit.", "default": "0.0"},
        ],
        "examples": ["cluster_flops -tray_weight 32.0"],
    }
    entry.update(overrides)
    return entry


def test_every_real_parameter_survives_validation(flops_window):
    """Zero false kills on a genuine entry — the precondition for the veto
    being worth having at all."""
    candidates = window_candidates(flops_window)

    result = validate_entry(_flops_entry(), flops_window, candidates)

    assert result.accepted is True
    assert result.rejections == ()
    assert [arg.name for arg in result.entry.args] == [
        "-tray_weight",
        "-timing_weight",
        "-max_split_size",
        "-num_paths",
        "-clock_power_weight",
    ]
    assert result.stats.args_seen == result.stats.args_kept == 5
    assert result.entry.suspect_related is False


def test_each_entry_in_a_windows_reply_is_judged_on_its_own():
    """A window's reply lists every command it documents, so one hallucinated
    entry must veto itself and leave its neighbours untouched — the multi-entry
    shape must not turn one bad command name into a lost window."""
    window = extraction_windows(_gpl_elements() + _flops_elements())[0]
    candidates = window_candidates(window)

    good = validate_entry(_flops_entry(), window, candidates)
    invented = validate_entry(
        {"command_name": "detailed_placement", "args": []}, window, candidates
    )
    other = validate_entry(
        {"command_name": "global_placement",
         "args": [{"name": "-density", "default": "0.7"}]},
        window,
        candidates,
    )

    assert [result.accepted for result in (good, invented, other)] == [
        True, False, True
    ]
    assert invented.rejections[0].reason == "not_in_candidates"
    assert [arg.name for arg in other.entry.args] == ["-density"]


def test_invented_parameter_is_rejected_with_a_complete_record(flops_window):
    entry = _flops_entry()
    entry["args"] = entry["args"] + [
        {"name": "-tray_capacity", "desc": "Invented.", "default": ""}
    ]

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.accepted is True
    assert [arg.name for arg in result.entry.args] == [
        "-tray_weight", "-timing_weight", "-max_split_size", "-num_paths",
        "-clock_power_weight",
    ]
    rejection = next(item for item in result.rejections if item.field == "arg")
    assert rejection.value == "-tray_capacity"
    assert rejection.reason == "not_in_text"
    assert rejection.window and len(rejection.window) <= 200
    assert result.stats.args_seen == 6
    assert result.stats.args_kept == 5


def test_dropped_dash_is_rejected_even_though_the_bare_word_appears(
    flops_window,
):
    """C0's measured infidelity: the prompt asks for `-tray_weight`, the model
    answers `tray_weight`, and the manual's own prose contains the bare word —
    so plain containment restores the dash the model dropped."""
    entry = _flops_entry(
        args=[{"name": "tray_weight", "desc": "Tray weight.", "default": ""}]
    )

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.entry.args == ()
    assert [(item.value, item.reason) for item in result.rejections] == [
        ("tray_weight", "dash_stripped")
    ]


def test_positional_argument_without_a_dash_is_kept():
    """The dash test is evidence-based, not shape-based: a command that really
    does take a positional argument must not be gutted."""
    window = _window(SET_DONT_USE_SYNTAX, heading="Set Don't Use")
    entry = {
        "command_name": "set_dont_use",
        "args": [{"name": "lib_cells", "desc": "Cells to exclude."}],
    }

    result = validate_entry(entry, window, ["set_dont_use"])

    assert [arg.name for arg in result.entry.args] == ["lib_cells"]
    assert result.rejections == ()


def test_the_attribution_exemption_is_scoped_to_an_empty_assignment(flops_window):
    """The reverse guard, pinned in one place because the two halves are one
    rule: an EMPTY assignment exempts a returned name from attribution (a
    flagless command's positional argument was never on any list, and the
    prompt asks for it outright), while a NON-EMPTY assignment must keep
    rejecting a name that belongs to another slice.

    Both directions in one test on purpose: the fix for the first is a
    one-token change away from gutting the second (`if not reason and
    assignment and ...` -> `if not reason and ...`), and a test that only
    proved the exemption would go green on that mutation.
    """
    flagless = _window(SET_DONT_USE_SYNTAX, heading="Set Don't Use")
    positional = {
        "command_name": "set_dont_use",
        "args": [{"name": "lib_cells", "desc": "Cells to exclude."}],
    }

    exempt = validate_entry(positional, flagless, ["set_dont_use"], assigned=())

    assert [arg.name for arg in exempt.entry.args] == ["lib_cells"]
    assert exempt.rejections == ()

    # Same window text, same grounding — only the assignment differs. Every
    # name below is verbatim in `flops_window`, so nothing but attribution can
    # reject them.
    off_assignment = _flops_entry(
        args=[
            {"name": "-num_paths", "desc": "Another slice's.", "default": ""},
            {"name": "-tray_weight", "desc": "This slice's.", "default": ""},
        ]
    )

    constrained = validate_entry(
        off_assignment,
        flops_window,
        window_candidates(flops_window),
        assigned=("-tray_weight",),
    )

    assert [arg.name for arg in constrained.entry.args] == ["-tray_weight"]
    assert [(item.value, item.reason) for item in constrained.rejections] == [
        ("-num_paths", "arg_outside_slice")
    ]


def test_near_miss_command_name_is_vetoed():
    """`set_d` must not be found inside `set_db`.

    A truncated or half-hallucinated name is exactly the failure the candidate
    list plus verbatim check exists to stop, and substring containment accepts
    it silently.
    """
    window = _window("set_db -name value sets a database property.",
                     heading="set_db")

    result = validate_entry({"command_name": "set_d"}, window,
                            ["set_d", "set_db"])

    assert result.accepted is False
    assert result.entry is None
    assert [(item.field, item.reason) for item in result.rejections] == [
        ("command_name", "not_in_text")
    ]
    assert result.stats.command_rejected is True


def test_relayed_name_may_be_claimed_without_appearing_in_this_window():
    """The relay's whole point: a continuation window holds parameters and no
    command name at all, so the verbatim check — which is right for every other
    claim — is the one thing standing between the model and the options table
    it was shown. The witness still exists; it is one window back."""
    window = _window("| `-tray_weight` | Tray weight, default value is 32.0. |")
    entry = {
        "command_name": "cluster_flops",
        "args": [{"name": "-tray_weight", "default": "32.0"}],
    }

    vetoed = validate_entry(entry, window, [])
    relayed = validate_entry(entry, window, [], carried=["cluster_flops"])

    assert vetoed.accepted is False
    assert vetoed.rejections[0].reason == "not_in_candidates"
    assert relayed.accepted is True
    assert [arg.name for arg in relayed.entry.args] == ["-tray_weight"]
    assert relayed.rejections == ()


def test_a_relay_never_lets_an_invented_name_through():
    """List membership does not relax with the relay — it is still the only
    veto that matters, and the model can still only pick from names some window
    actually contained."""
    window = _window("| `-tray_weight` | Tray weight, default value is 32.0. |")

    result = validate_entry(
        {"command_name": "detailed_placement"},
        window,
        [],
        carried=["cluster_flops"],
    )

    assert result.accepted is False
    assert result.rejections[0].reason == "not_in_candidates"


def test_a_relayed_claim_still_grounds_its_own_body_in_this_window():
    """The exemption is scoped to the NAME. Parameters, defaults and syntax are
    what this window is being asked about, and a relay that let those through
    unchecked would turn every continuation window into a free-text channel."""
    window = _window("| `-tray_weight` | Tray weight, default value is 32.0. |")
    entry = {
        "command_name": "cluster_flops",
        "syntax": "cluster_flops [-tray_weight tray_weight]",
        "args": [
            {"name": "-tray_weight", "default": "64.0"},
            {"name": "-invented_flag"},
        ],
    }

    result = validate_entry(entry, window, [], carried=["cluster_flops"])

    assert result.accepted is True
    # Syntax lives in the window that named the command, not this one: cleared,
    # never fatal.
    assert result.entry.syntax == ""
    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-tray_weight", "")
    ]
    assert sorted(item.reason for item in result.rejections) == [
        "not_contiguous", "not_in_text", "not_in_text",
    ]


def test_a_relayed_claim_is_not_flagged_suspect():
    """`suspect_related` means "possibly a MENTIONED command rather than the
    documented one". A continuation window mentions nothing — it is still
    documenting the same command — so flagging it would put a warning on
    exactly the entries the relay exists to produce."""
    window = _window("| `-tray_weight` | Tray weight, default value is 32.0. |")

    result = validate_entry(
        {"command_name": "cluster_flops"}, window, [], carried=["cluster_flops"]
    )

    assert result.entry.suspect_related is False


def test_a_relayed_claim_says_so_because_its_clean_mark_is_an_exemption():
    """…and BECAUSE it is not flagged suspect, it has to say it was relayed.

    `suspect_related=False` on a continuation window is the absence of
    evidence, not evidence of absence: the window has no heading and no usage
    line for the command, it simply is not being asked to. The cross-window
    merge folds this flag with AND, so without the marker one parameter table
    would silently clear the warning an earlier window earned.
    """
    window = _window("| `-tray_weight` | Tray weight, default value is 32.0. |")

    result = validate_entry(
        {"command_name": "cluster_flops"}, window, [], carried=["cluster_flops"]
    )

    assert result.entry.relayed is True


def test_a_claim_with_this_windows_own_evidence_is_not_relayed():
    """The marker is about EVIDENCE, not about the relay's membership list.

    A window that names the command in its heading has judged it on its own
    merits, and a clean mark it earned that way is a finding the merge should
    honour — even though the relay would also have let the claim through.
    """
    window = _window(
        "cluster_flops [-tray_weight tray_weight]", heading="cluster_flops"
    )

    result = validate_entry(
        {"command_name": "cluster_flops"},
        window,
        ["cluster_flops"],
        carried=["cluster_flops"],
    )

    assert result.entry.suspect_related is False
    assert result.entry.relayed is False


def test_a_carried_name_mentioned_only_in_prose_here_is_still_relayed():
    """The in-between shape: the name IS in this window's text, but only in
    passing — no heading, no usage line. Nothing was judged, so nothing was
    cleared, and the merge must not read the exemption as a finding."""
    window = _window(
        "See cluster_flops for the tray options used by this command."
    )

    result = validate_entry(
        {"command_name": "cluster_flops"},
        window,
        [],
        carried=["cluster_flops"],
    )

    assert result.accepted is True
    assert result.entry.suspect_related is False
    assert result.entry.relayed is True


def test_an_ordinary_entry_is_never_marked_relayed(flops_window):
    result = validate_entry(
        _flops_entry(), flops_window, window_candidates(flops_window)
    )

    assert result.entry.relayed is False


def test_command_name_outside_the_candidate_list_vetoes_the_entry(flops_window):
    result = validate_entry(
        _flops_entry(command_name="detailed_placement"),
        flops_window,
        window_candidates(flops_window),
    )

    assert result.accepted is False
    assert result.rejections[0].reason == "not_in_candidates"
    # The veto is whole-entry: nothing from an entry about the wrong command
    # is trustworthy.
    assert result.entry is None
    assert result.stats.args_seen == 0


def test_missing_command_name_vetoes_the_entry(flops_window):
    result = validate_entry({"syntax": "cluster_flops"}, flops_window,
                            window_candidates(flops_window))

    assert result.accepted is False
    assert result.rejections[0].reason == "empty"


def test_line_wrapped_syntax_normalises_and_passes(flops_window):
    """The manual wraps with backslashes; the model answers on one line."""
    flat = (
        "cluster_flops [-tray_weight tray_weight] [-timing_weight timing_weight] "
        "[-max_split_size max_split_size] [-num_paths num_paths] "
        "[-clock_power_weight clock_power_weight]"
    )

    result = validate_entry(
        _flops_entry(syntax=flat), flops_window, window_candidates(flops_window)
    )

    assert result.stats.syntax_kept is True
    assert result.entry.syntax == flat
    assert not [item for item in result.rejections if item.field == "syntax"]


def test_syntax_that_is_not_in_the_window_is_cleared_but_not_fatal(
    flops_window,
):
    result = validate_entry(
        _flops_entry(syntax="cluster_flops [-tray_capacity capacity]"),
        flops_window,
        window_candidates(flops_window),
    )

    assert result.accepted is True
    assert result.entry.syntax == ""
    assert result.stats.syntax_seen is True
    assert result.stats.syntax_kept is False
    assert [item.reason for item in result.rejections if item.field == "syntax"] == [
        "not_contiguous"
    ]


def test_ungrounded_default_is_cleared_while_the_parameter_survives(
    flops_window,
):
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "default": "64.0"}]
    )

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-tray_weight", "")
    ]
    assert [item.field for item in result.rejections] == ["default"]
    assert result.stats.defaults_seen == 1
    assert result.stats.defaults_kept == 0


def test_default_digit_is_rejected_though_the_longer_number_contains_it(
    flops_window,
):
    """The manual states "default value is 500"; a hallucinated "5" must not
    ground just because it is a bare substring of 500 — the bug a plain `in`
    check had."""
    entry = _flops_entry(
        args=[{"name": "-max_split_size", "desc": "Split.", "default": "5"}]
    )

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-max_split_size", "")
    ]
    assert [item.field for item in result.rejections] == ["default"]


def test_default_matching_the_full_number_still_grounds(flops_window):
    entry = _flops_entry(
        args=[{"name": "-max_split_size", "desc": "Split.", "default": "500"}]
    )

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-max_split_size", "500")
    ]
    assert result.rejections == ()


def test_decimal_default_grounds_at_its_own_token_boundaries():
    """"0.5" must ground when the window text genuinely says `0.5`."""
    window = _window(
        "cluster_flops [-density density]\n"
        "`-density` default value is `0.5`."
    )
    entry = {
        "command_name": "cluster_flops",
        "args": [{"name": "-density", "default": "0.5"}],
    }

    result = validate_entry(entry, window, ["cluster_flops"])

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-density", "0.5")
    ]
    assert result.rejections == ()


def test_command_named_only_in_prose_is_flagged_suspect_not_rejected():
    """Grounded, but possibly a *mentioned* command rather than the documented
    one. A veto here would drop correct entries."""
    window = _window(
        "This section explains placement. See also repair_design for timing.",
        heading="Placement Notes",
        provenance="Manual > Placement Notes",
    )

    result = validate_entry({"command_name": "repair_design"}, window,
                            ["repair_design"])

    assert result.accepted is True
    assert result.entry.suspect_related is True


def test_suspect_reads_the_windows_own_headings_not_its_provenance():
    """`provenance` is a display label inherited from an earlier window, so a
    command that only appears there is NOT documented in this window's text —
    letting the label answer "is it in a heading" would launder a
    cross-reference into a documented command."""
    inherited = _window(
        "See also repair_design for timing.",
        provenance="Manual > repair_design",
    )

    result = validate_entry({"command_name": "repair_design"}, inherited,
                            ["repair_design"])

    assert result.entry.suspect_related is True


def test_description_and_examples_pass_through_with_an_anchor_seat(
    flops_window,
):
    result = validate_entry(_flops_entry(), flops_window,
                            window_candidates(flops_window))

    assert result.entry.description == "Clusters flops into multi-bit trays."
    assert result.entry.examples == ("cluster_flops -tray_weight 32.0",)
    assert result.entry.anchor_element_ids == ()


# ------------------------------------------------ 5b. field-type coercion
def test_required_string_false_is_coerced_not_truthy(flops_window):
    """The reported bug: `bool("false")` is `True`, so a model that quotes its
    booleans (`"required": "false"`) would previously flip every such
    parameter to required. Coercion must go by MEANING, not by truthiness."""
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "required": "false"}]
    )

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.entry.args[0].required is False
    assert result.rejections == ()


def test_required_string_true_is_coerced(flops_window):
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "required": "TRUE"}]
    )

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.entry.args[0].required is True
    assert result.rejections == ()


def test_required_non_boolean_value_degrades_and_is_reported(flops_window):
    """A shape that is neither a bool nor a recognisable true/false string
    (a bare number, here) cannot be trusted either way, so it degrades to
    `False` — the same "cleared, never fatal" treatment `default` gets — and
    the degrade is recorded rather than silent."""
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "required": 1}]
    )

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.accepted is True
    assert result.entry.args[0].required is False
    rejection = next(item for item in result.rejections if item.field == "required")
    assert rejection.value == "1"
    assert rejection.reason == "not_boolean"


def test_args_field_returned_as_an_object_is_not_iterated_as_keys(flops_window):
    """A model returning `args` as an object rather than a list used to be
    walked with a bare `for`, which iterates a dict's KEYS — plain strings
    that then failed the `isinstance(..., Mapping)` check and vanished with
    no record. The whole field must instead be treated as malformed."""
    entry = _flops_entry(args={"name": "-tray_weight", "required": False})

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.accepted is True
    assert result.entry.args == ()
    assert result.stats.args_seen == 0
    rejection = next(item for item in result.rejections if item.field == "args")
    assert rejection.reason == "model_response_unusable"


def test_args_list_item_that_is_not_an_object_is_dropped_and_reported(flops_window):
    entry = _flops_entry(args=["-tray_weight"])

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.accepted is True
    assert result.entry.args == ()
    rejection = next(item for item in result.rejections if item.field == "arg")
    assert rejection.reason == "not_object"


def test_examples_string_scalar_is_coerced_to_a_single_element_list(flops_window):
    """A model answering "one example" with a bare string instead of a
    one-element list got the wrapper wrong, not the content — coalescing is
    the reasonable read, not a rejection-worthy failure. Iterating the raw
    string directly, as the old code did, would have shredded it into
    one-character "examples" instead."""
    entry = _flops_entry(examples="cluster_flops -tray_weight 32.0")

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.entry.examples == ("cluster_flops -tray_weight 32.0",)
    assert result.rejections == ()


def test_examples_non_string_item_is_dropped_and_reported(flops_window):
    entry = _flops_entry(examples=["cluster_flops -tray_weight 32.0", 5])

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.entry.examples == ("cluster_flops -tray_weight 32.0",)
    rejection = next(item for item in result.rejections if item.field == "examples")
    assert rejection.reason == "not_string"
    assert rejection.value == "5"


def test_examples_non_list_non_string_field_is_dropped_and_reported(flops_window):
    entry = _flops_entry(examples={"one": "cluster_flops -tray_weight 32.0"})

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    assert result.entry.examples == ()
    rejection = next(item for item in result.rejections if item.field == "examples")
    assert rejection.reason == "not_list"


def test_rejection_diagnostics_stay_bounded(flops_window):
    entry = _flops_entry(args=[{"name": "-" + "x" * 400, "desc": ""}])

    result = validate_entry(entry, flops_window, window_candidates(flops_window))

    rejection = result.rejections[0]
    assert len(rejection.value) <= 120
    assert len(rejection.window) <= 200


def test_rejection_window_centers_on_a_real_hit_in_long_text():
    """`_reject_window`'s hit branch (a near-miss, like C0's dropped-dash
    case) must centre on where the value was actually found in a genuinely
    long window, not just return the window's head — the miss branch is
    already covered by `test_rejection_diagnostics_stay_bounded`, whose
    invented value is never in the text at all."""
    padding_before = "Unrelated padding text. " * 20
    padding_after = " More unrelated trailing text." * 20
    # The usage line opens the window so `cluster_flops` has a real evidence
    # segment (see `window_segments`) — the padding then lives INSIDE it,
    # which is what puts the near-miss far from the segment's head.
    text = (
        "cluster_flops\n"
        f"{padding_before}\n"
        "Options: [-tray_weight tray_weight]\n"
        f"{padding_after}"
    )
    window = _window(text)
    entry = {
        "command_name": "cluster_flops",
        "args": [{"name": "tray_weight", "desc": "Tray weight."}],
    }

    result = validate_entry(entry, window, ["cluster_flops"])

    rejection = next(item for item in result.rejections if item.field == "arg")
    assert rejection.reason == "dash_stripped"
    assert len(text) > 400
    assert len(rejection.window) <= 200
    # Centred on the hit, not the head of a 400+-char window.
    assert rejection.window != text[:200]
    assert "tray_weight" in rejection.window


def test_normalize_syntax_erases_continuations_and_collapses_space():
    assert normalize_syntax("a \\\n   b\\\nc") == "a b c"
    assert normalize_syntax("  a   b  ") == "a b"


# ----------------------------------------------------------------- 6. ledger
def test_outcome_lists_the_candidates_nothing_was_extracted_for(flops_window):
    candidates = window_candidates(flops_window)
    result = validate_entry(_flops_entry(), flops_window, candidates)

    outcome = window_outcome(flops_window, candidates, [result])

    assert outcome.accepted_names == ("cluster_flops",)
    assert "cluster_flops" not in outcome.uncovered_candidates
    assert outcome.entries_seen == 1
    assert outcome.command_rejects == 0
    assert outcome.args_seen == outcome.args_kept == 5
    assert outcome.ordinal == 0
    assert outcome.provenance == FLOPS_PATH


def test_outcome_reports_uncovered_candidates(flops_window):
    candidates = ["cluster_flops", "detailed_placement"]
    result = validate_entry(_flops_entry(), flops_window, candidates)

    outcome = window_outcome(flops_window, candidates, [result])

    assert outcome.uncovered_candidates == ("detailed_placement",)


def test_outcome_folds_uncovered_assignments_into_the_ledger(flops_window):
    candidates = window_candidates(flops_window)
    result = validate_entry(_flops_entry(), flops_window, candidates)

    outcome = window_outcome(
        flops_window, candidates, [result], uncovered_args=["-never_answered"]
    )

    assert outcome.args_uncovered == 1
    assert outcome.uncovered_args == ("-never_answered",)
    assert [item.reason for item in outcome.rejections] == ["arg_not_returned"]


def test_window_outcome_caps_rejections_and_counts_the_overflow(flops_window):
    """A pathological entry (or a model that never stops inventing
    parameters) must not let a job report inherit an unbounded rejection
    list through its own failure records — the ledger caps it and counts
    what got cut rather than silently reading as "everything else was
    clean"."""
    candidates = window_candidates(flops_window)
    entry = _flops_entry(
        args=[{"name": f"-invented_{index}", "desc": ""} for index in range(30)]
    )

    result = validate_entry(entry, flops_window, candidates)
    assert len(result.rejections) == 30  # per-entry validation stays uncapped

    outcome = window_outcome(flops_window, candidates, [result])

    assert len(outcome.rejections) == MAX_WINDOW_REJECTIONS
    assert outcome.rejections_overflow == 30 - MAX_WINDOW_REJECTIONS


def test_catalog_stats_publish_the_command_reject_ratio(flops_window):
    candidates = window_candidates(flops_window)
    good = validate_entry(_flops_entry(), flops_window, candidates)
    bad = validate_entry(
        _flops_entry(command_name="detailed_placement"), flops_window, candidates
    )
    outcome = window_outcome(flops_window, candidates, [good, bad, bad])

    stats = catalog_stats([outcome])

    assert stats.windows == 1
    assert stats.entries_seen == 3
    assert stats.command_rejects == 2
    assert stats.command_reject_ratio == pytest.approx(0.6667, abs=1e-4)


def test_catalog_stats_publish_the_args_keep_ratio(flops_window):
    """The circuit breaker needs an args axis alongside the command-name
    reject ratio — an entry can pick the right command name and still invent
    every parameter, and a veto-rate-only breaker would miss that entirely."""
    candidates = window_candidates(flops_window)
    good = validate_entry(_flops_entry(), flops_window, candidates)
    entry = _flops_entry()
    entry["args"] = entry["args"] + [
        {"name": "-tray_capacity", "desc": "Invented.", "default": ""}
    ]
    partial = validate_entry(entry, flops_window, candidates)
    outcome = window_outcome(flops_window, candidates, [good, partial])

    stats = catalog_stats([outcome])

    assert stats.args_seen == 11
    assert stats.args_kept == 10
    assert stats.args_keep_ratio == pytest.approx(10 / 11, abs=1e-4)


def test_catalog_stats_count_the_windows_they_were_given(flops_window):
    """Skipped windows never reach the ledger, so neither ratio is diluted by
    the prose chapters the cost gate went past."""
    candidates = window_candidates(flops_window)
    result = validate_entry(_flops_entry(), flops_window, candidates)

    stats = catalog_stats(
        [window_outcome(flops_window, candidates, [result]) for _ in range(3)]
    )

    assert stats.windows == 3


def test_catalog_stats_on_nothing_do_not_divide_by_zero():
    stats = catalog_stats([])

    assert stats.entries_seen == 0
    assert stats.args_keep_ratio == 0.0
    assert stats.command_reject_ratio == 0.0


# --------------------------------------------------------------------- 7. e2e
def test_a_huge_parameter_table_survives_the_whole_chain():
    """e2e, real markdown all the way through: parse_markdown_text ->
    extraction_windows -> window_candidates -> validate_entry -> window_outcome.

    This is v1's exact failure, inverted. There, a 120-parameter table overran
    one section's budget and the whole element was dropped — the section came
    out as its own heading plus a syntax block, 1/120 args kept, and a
    command-reject ratio of 0.0 that never saw the failure because there was
    nothing left to see it in. Here the table is split across windows instead
    and every character of it is still in a prompt.

    It also pins the shape the cross-window merge exists for: the command's own
    window grounds all 120 parameters, and the table's windows carry parameters
    with no claimable name of their own — which is why the skip gate must ask
    about flags as well as candidates.
    """
    syntax = "big_command\n" + "\n".join(
        f"    [-param_{index:03d} value_{index:03d}]" for index in range(120)
    )
    rows = "\n".join(
        f"| `-param_{index:03d}` | Description for parameter number "
        f"{index:03d}, controlling one aspect of the big command's tuning "
        f"behavior in fine detail across many designs. Default is "
        f"`{index}`. |"
        for index in range(120)
    )
    markdown = (
        "## big_command\n\n"
        f"```\n{syntax}\n```\n\n"
        "| Switch Name | Description |\n"
        "| ----- | ----- |\n"
        f"{rows}\n"
    )
    # Ids come from the store, not the parser (`parse_markdown_text` leaves
    # them blank until the rows are persisted), so they are supplied here the
    # way `source_elements_for_chunking` would return them.
    elements = [
        {
            "id": f"el-e2e-{index:04d}",
            "element_type": element.element_type,
            "text": element.text,
            "section_path": element.metadata.get("section_path", ""),
        }
        for index, element in enumerate(parse_markdown_text("src-e2e", markdown), 1)
    ]

    windows = extraction_windows(elements)
    assert len(windows) > 1
    assert _rebuild(windows) == _expected_text(elements)

    # The command's own window: a model answering for all 120 parameters
    # grounds all 120, which is precisely what v1 lost.
    named = windows[0]
    candidates = window_candidates(named)
    assert "big_command" in candidates
    entry = {
        "command_name": "big_command",
        "args": [{"name": f"-param_{index:03d}"} for index in range(120)],
    }
    result = validate_entry(entry, named, candidates)
    outcome = window_outcome(named, candidates, [result])

    assert result.accepted is True
    assert outcome.args_seen == outcome.args_kept == 120

    # The table's windows: no claimable name of their own. Without the relay
    # this is where the extraction ended — the model had nothing it could
    # legally claim, so the whole options table came back empty.
    chain = _carry_chain(windows)
    for index, window in enumerate(windows[1:], 1):
        assert window_candidates(window) == []
        assert parameter_names(window.text)
        assert window_needs_model(window, chain[index]) is True
        assert extraction_slices(window)
        # Relayed from window 0, and still relayed after passing THROUGH
        # window 1, which named nothing either.
        assert chain[index] == ["big_command"]

        relayed = validate_entry(entry, window, [], carried=chain[index])
        assert relayed.accepted is True
        assert relayed.entry.suspect_related is False
        # Only the parameters this window actually holds ground here.
        held = set(parameter_names(window.text))
        assert {arg.name for arg in relayed.entry.args} == held
        assert 0 < len(held) < 120


def test_a_new_command_ends_the_previous_ones_relay():
    """The reset half, end to end: two commands in one document, each with a
    multi-window table. The second command's heading must stop the first one's
    relay, or every parameter in the book stays claimable by the first."""
    def manual(name, params):
        rows = "\n".join(
            f"| `-{name}_{index:02d}` | Description for parameter {index:02d} "
            f"of {name}, padded so the table spans several windows. |"
            for index in range(params)
        )
        return (
            f"## {name}\n\n"
            f"```\n{name} [-{name}_00 value]\n```\n\n"
            f"| Switch Name | Description |\n| ----- | ----- |\n{rows}\n"
        )

    elements = [
        {
            "id": f"el-reset-{index:04d}",
            "element_type": element.element_type,
            "text": element.text,
            "section_path": element.metadata.get("section_path", ""),
        }
        for index, element in enumerate(
            parse_markdown_text(
                "src-reset", manual("first_command", 80) + manual("second_command", 80)
            ),
            1,
        )
    ]

    windows = extraction_windows(elements, max_chars=1_500)
    assert _rebuild(windows) == _expected_text(elements)
    chain = _carry_chain(windows)

    relayed_names = [
        name for carried in chain for name in carried
    ]
    assert "first_command" in relayed_names
    assert "second_command" in relayed_names

    # Once the second command has been named, the first is no longer claimable
    # anywhere after it — the relay reset, it did not accumulate.
    second_named = next(
        index
        for index, window in enumerate(windows)
        if "second_command" in window_candidates(window)
    )
    assert all(
        "first_command" not in carried for carried in chain[second_named + 1:]
    )
    assert any("first_command" in carried for carried in chain[:second_named + 1])

    # And the veto follows the relay: the first command cannot be claimed in
    # the second command's continuation windows.
    tail = windows[second_named + 1]
    assert validate_entry(
        {"command_name": "first_command"}, tail, [], carried=chain[second_named + 1]
    ).accepted is False
