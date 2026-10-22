"""Section outline projection and hint selection (pure functions).

Covers ``app.services.section_outline``: occurrence folding, hierarchy
(numbering depth via the shared ``navigation_chunks.heading_numbering`` parser,
breadcrumbs, leaves, virtual parents), ranges, ``under``, ranking, hint
selection, run-local addresses, resolution and rendering.
"""
from __future__ import annotations

import pytest

from app.services.navigation_chunks import HeadingNumbering, heading_numbering
from app.services.section_outline import (
    HINT_BLOCK_TITLE,
    KIND_BREADCRUMB,
    KIND_LEAF,
    KIND_NUMBERED,
    KIND_VIRTUAL,
    RENDER_MIN_CHARS,
    UNDER_NO_NUMBERING,
    UNDER_OK,
    UNDER_UNPARSED,
    AddressBook,
    Ambiguous,
    NotFound,
    Resolved,
    SourceRanking,
    build_outline,
    filter_outline,
    parse_section_id,
    rank_sections,
    render_hints,
    render_outline,
    render_ranked,
    resolve_section,
    select_hints,
)


def _rows(*spec):
    """``(label, n_chunks[, navigation])`` -> rows with ids c0, c1, ..."""
    rows = []
    for item in spec:
        label, count = item[0], item[1]
        navigation = item[2] if len(item) > 2 else False
        for _ in range(count):
            rows.append((f"c{len(rows)}", label, navigation))
    return rows


def _by_label(outline, label):
    found = [occ for occ in outline.occurrences if occ.label == label]
    assert len(found) == 1, (label, found)
    return found[0]


def _labels(occurrences):
    return [occ.label for occ in occurrences]


# A UCIe-like flat MinerU source: one-segment section paths, captions as
# headings, navigation pages at the front, no chapter-5 / 5.7 heading.
UCIE = _rows(
    ("UCIe Specification", 1),
    ("Contents", 3, True),
    ("Tables", 1, True),
    ("Figures", 1, True),
    ("1 Introduction", 1),
    ("1.4 UCIe Key Performance Targets", 2),
    ("5.7.1 Overview", 1),
    ("Figure 5-30. Channel Reach", 1),
    ("5.7.2 Advanced Package", 1),
    ("5.7.2.1 Loss and Crosstalk Mask", 2),
    ("Table 5-17. Advanced Package Loss and Crosstalk Mask", 1),
    ("5.7.3 Standard Package", 1),
    ("Table 5-18. Standard Package Insertion Loss", 1),
    ("5.7.3.1 Driver Architecture", 1),
    ("Notes", 1),
    ("5.8 Electrical Specification", 1),
)


# ---------------------------------------------------------------- parser


@pytest.mark.parametrize(
    "text, expected",
    [
        ("5.7.2.1 Loss and Crosstalk Mask", HeadingNumbering(number="5.7.2.1")),
        ("A.2 Register Map", HeadingNumbering(number="A.2")),
        ("Chapter 5 Electrical", HeadingNumbering(caption_word="Chapter", caption_chapter="5")),
        ("第5章 电气层", HeadingNumbering(caption_word="第", caption_chapter="5")),
        ("Appendix A", HeadingNumbering(caption_word="Appendix", caption_chapter="A")),
        (
            "Figure A-1. Bump Map",
            HeadingNumbering(caption_word="Figure", caption_chapter="A", caption_item=True),
        ),
        (
            "Table 5-18. Standard Package",
            HeadingNumbering(caption_word="Table", caption_chapter="5", caption_item=True),
        ),
        ("DDR4 Timing", HeadingNumbering()),
        ("1. Install", HeadingNumbering()),  # trailing dot: not read as numbered
        ("Notes", HeadingNumbering()),
    ],
)
def test_heading_numbering_is_the_detector_parse(text, expected):
    assert heading_numbering(text) == expected


# ---------------------------------------------------------------- folding


def test_consecutive_rows_fold_into_one_occurrence():
    outline = build_outline(_rows(("1 Intro", 3), ("2 Body", 2)))
    assert _labels(outline.occurrences) == ["1 Intro", "2 Body"]
    assert outline.occurrences[0].chunk_ids == ("c0", "c1", "c2")
    assert outline.occurrences[1].chunk_ids == ("c3", "c4")
    assert [occ.index for occ in outline.occurrences] == [0, 1]
    assert outline.scanned_rows == 5 and outline.truncated is False
    assert build_outline([], truncated=True).truncated is True


def test_duplicate_labels_are_separate_occurrences():
    outline = build_outline(
        _rows(("1 Intro", 1), ("Overview", 1), ("2 Body", 1), ("Overview", 2))
    )
    overviews = [occ for occ in outline.occurrences if occ.label == "Overview"]
    assert len(overviews) == 2
    assert overviews[0].chunk_ids == ("c1",) and overviews[1].chunk_ids == ("c3", "c4")
    assert overviews[0].parent == 0 and overviews[1].parent == 2


def test_navigation_occurrences_are_excluded():
    outline = build_outline(
        _rows(
            ("Contents", 2, True),
            ("1 Intro", 1),
            ("1 Intro", 1, True),  # a navigation chunk inside a real section
            ("1 Intro", 1),
            ("2 Body", 1),
        )
    )
    assert _labels(outline.occurrences) == ["1 Intro", "2 Body"]
    assert outline.occurrences[0].chunk_ids == ("c2", "c4")
    assert all("c0" not in occ.range_chunk_ids for occ in outline.occurrences)


def test_navigation_run_between_same_labels_keeps_two_occurrences():
    outline = build_outline(_rows(("Notes", 1), ("Contents", 1, True), ("Notes", 1)))
    assert _labels(outline.occurrences) == ["Notes", "Notes"]


# ---------------------------------------------------------------- hierarchy


def test_numbering_depth_and_5_70_not_under_5_7():
    outline = build_outline(
        _rows(("5.7 Channel", 1), ("5.7.1 Mask", 1), ("5.70 Far Away", 1), ("5.8 Next", 1))
    )
    s57 = _by_label(outline, "5.7 Channel")
    s5701 = _by_label(outline, "5.7.1 Mask")
    s570 = _by_label(outline, "5.70 Far Away")
    assert s5701.parent == s57.index and s5701.depth == s57.depth + 1
    assert s570.parent != s57.index and s570.depth == s57.depth
    assert s57.range_chunk_ids == ("c0", "c1")
    assert _labels(filter_outline(outline, "5.7").entries) == ["5.7 Channel", "5.7.1 Mask"]


def test_appendix_numbering_expands():
    outline = build_outline(
        _rows(("9 Last Chapter", 1), ("A.1 Bump Map", 1), ("A.2 Registers", 1), ("A.2.1 Link", 1))
    )
    virtual_a = _by_label(outline, "A")
    assert virtual_a.kind == KIND_VIRTUAL
    assert virtual_a.range_chunk_ids == ("c1", "c2", "c3")
    a2 = _by_label(outline, "A.2 Registers")
    assert a2.parent == virtual_a.index
    assert a2.range_chunk_ids == ("c2", "c3")
    assert _labels(filter_outline(outline, "A.2").entries) == ["A.2 Registers", "A.2.1 Link"]


@pytest.mark.parametrize("chapter", ["Chapter 5 Electrical Layer", "第5章 电气层"])
def test_word_enumerated_chapter_heading_opens_its_chapter(chapter):
    outline = build_outline(
        _rows(
            ("4.9 Previous", 1),
            (chapter, 1),
            ("5.1 Overview", 1),
            ("Figure 5-1. Stack", 1),
            ("5.2 Details", 1),
            ("6.1 Next Chapter", 1),
        )
    )
    head = _by_label(outline, chapter)
    assert head.kind == KIND_NUMBERED and head.number == "5"
    assert head.range_chunk_ids == ("c1", "c2", "c3", "c4")
    assert _by_label(outline, "5.1 Overview").parent == head.index
    # A chapter heading exists: no virtual "5" is synthesised.
    assert not any(occ.virtual and occ.number == "5" for occ in outline.occurrences)
    assert _labels(filter_outline(outline, "5").entries)[0] == chapter


def test_uncorroborated_bare_enumerators_are_leaves():
    outline = build_outline(
        _rows(
            ("3.2 Lanes", 1),
            ("Table 12", 1),  # document-wide numbering, not chapter 12
            ("A Brief History", 1),  # a lone capital letter, no A.x follows
            ("DDR4 Timing", 1),
            ("3.3 Clocks", 1),
            ("Appendix B", 1),
            ("B.1 Bump Map", 1),
        )
    )
    for label in ("Table 12", "A Brief History", "DDR4 Timing"):
        assert _by_label(outline, label).kind == KIND_LEAF
    # The leaves did not end 3.2.
    assert _by_label(outline, "3.2 Lanes").range_chunk_ids == ("c0", "c1", "c2", "c3")
    appendix = _by_label(outline, "Appendix B")
    assert appendix.kind == KIND_NUMBERED and appendix.number == "B"
    assert appendix.range_chunk_ids == ("c5", "c6")


def test_virtual_parent_range_is_all_descendants():
    outline = build_outline(UCIE)
    s57 = _by_label(outline, "5.7")
    assert s57.kind == KIND_VIRTUAL and s57.chunk_ids == ()
    members = [
        "5.7.1 Overview",
        "Figure 5-30. Channel Reach",
        "5.7.2 Advanced Package",
        "5.7.2.1 Loss and Crosstalk Mask",
        "Table 5-17. Advanced Package Loss and Crosstalk Mask",
        "5.7.3 Standard Package",
        "Table 5-18. Standard Package Insertion Loss",
        "5.7.3.1 Driver Architecture",
        "Notes",
    ]
    expected = tuple(cid for label in members for cid in _by_label(outline, label).chunk_ids)
    assert s57.range_chunk_ids == expected
    assert not set(_by_label(outline, "5.8 Electrical Specification").chunk_ids) & set(expected)
    virtual_5 = _by_label(outline, "5")
    assert virtual_5.kind == KIND_VIRTUAL and s57.parent == virtual_5.index
    # Virtual nodes sit right before their first descendant.
    assert outline.occurrences[s57.index + 1].label == "5.7.1 Overview"


def test_leaf_range_is_the_leaf_and_does_not_end_the_section():
    outline = build_outline(UCIE)
    table = _by_label(outline, "Table 5-17. Advanced Package Loss and Crosstalk Mask")
    assert table.kind == KIND_LEAF and table.range_chunk_ids == table.chunk_ids
    mask = _by_label(outline, "5.7.2.1 Loss and Crosstalk Mask")
    assert table.parent == mask.index
    assert mask.range_chunk_ids == mask.chunk_ids + table.chunk_ids
    standard = _by_label(outline, "5.7.3 Standard Package")
    table18 = _by_label(outline, "Table 5-18. Standard Package Insertion Loss")
    notes = _by_label(outline, "Notes")
    assert set(table18.chunk_ids) <= set(standard.range_chunk_ids)
    assert set(notes.chunk_ids) <= set(standard.range_chunk_ids)


def test_all_leaf_document_gets_no_expansion():
    outline = build_outline(_rows(("Overview", 2), ("Usage", 1), ("Notes", 1)))
    assert all(occ.kind == KIND_LEAF for occ in outline.occurrences)
    assert all(occ.range_chunk_ids == occ.chunk_ids for occ in outline.occurrences)
    assert all(occ.depth == 0 for occ in outline.occurrences)
    assert not outline.numbered


def test_preamble_without_heading_is_a_leaf():
    outline = build_outline(_rows(("", 1), ("1 Intro", 1)))
    assert outline.occurrences[0].label == "" and outline.occurrences[0].kind == KIND_LEAF
    assert outline.occurrences[0].title == "（无标题）"


def test_breadcrumb_depth_and_numbering_refinement():
    outline = build_outline(
        _rows(
            ("Spec", 1),
            ("Spec > 5.7 Channel", 1),
            ("Spec > 5.7.1 Mask", 1),  # same crumb depth, deeper number
            ("Spec > 5.7.1 Mask > Notes", 1),
            ("Spec > 5.8 Other", 1),
            ("Annex", 1),
        )
    )
    spec = _by_label(outline, "Spec")
    channel = _by_label(outline, "Spec > 5.7 Channel")
    mask = _by_label(outline, "Spec > 5.7.1 Mask")
    notes = _by_label(outline, "Spec > 5.7.1 Mask > Notes")
    other = _by_label(outline, "Spec > 5.8 Other")
    assert spec.kind == KIND_BREADCRUMB and channel.kind == KIND_BREADCRUMB
    # Same crumb depth, numbering refines: 5.7.1 sits under 5.7, and the
    # missing numbered parent 5 is synthesised under the crumb parent.
    virtual_5 = _by_label(outline, "5")
    assert virtual_5.kind == KIND_VIRTUAL and virtual_5.parent == spec.index
    assert channel.parent == virtual_5.index and mask.parent == channel.index
    assert notes.parent == mask.index and other.parent == virtual_5.index
    assert channel.range_chunk_ids == ("c1", "c2", "c3")
    assert spec.range_chunk_ids == ("c0", "c1", "c2", "c3", "c4")
    # A breadcrumbed document: an unnumbered top-level heading without
    # children is still a top-level node and ends ``Spec``.
    assert _by_label(outline, "Annex").kind == KIND_BREADCRUMB
    assert _by_label(outline, "Annex").depth == 0
    assert _labels(filter_outline(outline, "5.7").entries) == [
        "Spec > 5.7 Channel",
        "Spec > 5.7.1 Mask",
        "Spec > 5.7.1 Mask > Notes",
    ]


def test_stray_crumb_separator_in_flat_source_keeps_captions_leaves():
    outline = build_outline(
        _rows(
            ("5.7.1 Overview", 1),
            ("Figure 5-1. Stack", 1),
            ("Voltage > 0.5 V Range", 1),  # a flat heading containing " > "
            ("Notes", 1),
            ("5.7.2 Details", 1),
        )
    )
    assert _by_label(outline, "Figure 5-1. Stack").kind == KIND_LEAF
    assert _by_label(outline, "Notes").kind == KIND_LEAF
    overview = _by_label(outline, "5.7.1 Overview")
    assert {"c1", "c3"} <= set(overview.range_chunk_ids)


def test_breadcrumb_numbering_synthesises_virtual_under_crumb_parent():
    outline = build_outline(
        _rows(("5 Electrical", 1), ("5 Electrical > 5.7.1 Mask", 1), ("5 Electrical > 5.7.2 IL", 1))
    )
    virtual = _by_label(outline, "5.7")
    assert virtual.kind == KIND_VIRTUAL
    assert virtual.parent == _by_label(outline, "5 Electrical").index
    assert virtual.range_chunk_ids == ("c1", "c2")


def test_unnumbered_breadcrumb_document_top_levels_end_subtrees():
    outline = build_outline(
        _rows(("Intro", 1), ("Intro > Scope", 2), ("Background", 1), ("Usage", 1), ("Usage > CLI", 1))
    )
    assert _by_label(outline, "Background").kind == KIND_BREADCRUMB
    assert _by_label(outline, "Intro").range_chunk_ids == ("c0", "c1", "c2")
    assert _by_label(outline, "Intro > Scope").range_chunk_ids == ("c1", "c2")
    assert _by_label(outline, "Usage").range_chunk_ids == ("c4", "c5")


# ---------------------------------------------------------------- under


def test_under_filter_and_unsupported():
    outline = build_outline(UCIE)
    listed = _labels(filter_outline(outline, "5.7.3").entries)
    assert listed == [
        "5.7.3 Standard Package",
        "Table 5-18. Standard Package Insertion Loss",
        "5.7.3.1 Driver Architecture",
        "Notes",
    ]
    assert _labels(filter_outline(outline, "5.7.3 Standard Package").entries) == listed
    assert filter_outline(outline, "").entries == outline.occurrences
    assert filter_outline(outline, "9.9").entries == ()
    assert filter_outline(outline, "9.9").under_supported is True
    no_number_under = filter_outline(outline, "Standard Package")
    assert no_number_under.under_supported is False and no_number_under.entries == ()
    flat = build_outline(_rows(("Overview", 1), ("Usage", 1)))
    view = filter_outline(flat, "5.7")
    assert view.under_supported is False and view.entries == ()


# ---------------------------------------------------------------- ranking


def test_rank_sections_scores_range_and_skips_covered():
    outline = build_outline(UCIE)
    ids = {occ.label: occ.chunk_ids for occ in outline.occurrences}
    sims = {
        ids["1.4 UCIe Key Performance Targets"][0]: 0.99,
        ids["5.7.2.1 Loss and Crosstalk Mask"][1]: 0.9,
        ids["Table 5-18. Standard Package Insertion Loss"][0]: 0.85,
        ids["5.8 Electrical Specification"][0]: 0.3,
        "c1": 1.0,  # a navigation chunk: never reachable
    }
    covered = frozenset({ids["1.4 UCIe Key Performance Targets"][1]})
    ranked = rank_sections(outline, sims, exclude_chunk_ids=covered)
    labels = [item.occurrence.label for item in ranked]
    # 1.4 is covered (its own chunk is in the run); its ancestor scores from
    # uncovered chunks only (1 Introduction has none with a similarity).
    assert "1.4 UCIe Key Performance Targets" not in labels
    assert "1 Introduction" not in labels
    # Nested hits collapse to the deepest structural occurrence; the caption
    # leaf inside 5.7.3 yields 5.7.3; ancestors (5.7.2, virtual 5.7 / 5) drop.
    assert labels == [
        "5.7.2.1 Loss and Crosstalk Mask",
        "5.7.3 Standard Package",
        "5.8 Electrical Specification",
    ]
    assert [item.score for item in ranked] == [0.9, 0.85, 0.3]
    assert ranked[0].best_chunk_id == ids["5.7.2.1 Loss and Crosstalk Mask"][1]


def test_rank_sections_excluded_chunk_does_not_score_ancestor():
    outline = build_outline(_rows(("1 Intro", 1), ("1.1 Scope", 1), ("2 Body", 1)))
    ranked = rank_sections(outline, {"c1": 0.9, "c2": 0.5}, exclude_chunk_ids={"c1"})
    assert [item.occurrence.label for item in ranked] == ["2 Body"]


def test_rank_sections_restricted_to_entries():
    outline = build_outline(UCIE)
    view = filter_outline(outline, "5.7.3")
    sims = {cid: 0.5 for occ in outline.occurrences for cid in occ.chunk_ids}
    ranked = rank_sections(outline, sims, entries=view.entries)
    assert {item.occurrence.label for item in ranked} <= set(_labels(view.entries))


def test_rank_sections_all_leaf_document():
    outline = build_outline(_rows(("Overview", 1), ("Usage", 1), ("Notes", 1)))
    ranked = rank_sections(outline, {"c0": 0.2, "c1": 0.8, "c2": 0.5})
    assert [item.occurrence.label for item in ranked] == ["Usage", "Notes", "Overview"]


# ---------------------------------------------------------------- hints


def _ranking(source_id, outline, sims):
    return SourceRanking(
        notebook_id="",
        source_id=source_id,
        source_title=f"Title {source_id}",
        ranked=tuple(rank_sections(outline, sims)),
    )


def test_select_hints_floor_cap_and_cross_source_merge():
    flat = build_outline(_rows(("1 A", 1), ("2 B", 1), ("3 C", 1), ("4 D", 1)))
    first = _ranking("s-a", flat, {"c0": 0.9, "c1": 0.8, "c2": 0.75, "c3": 0.5})
    second = _ranking("s-b", flat, {"c0": 0.7, "c1": 0.95})
    hints = select_hints(
        [first, second], max_entries=4, relative_floor=0.8, best_chunk_sim=0.9
    )
    # floor = 0.72: s-a keeps 0.9/0.8/0.75, s-b keeps 0.95 (0.7 is below).
    got = [(h.source_id, h.occurrence.label, h.rank) for h in hints]
    assert got == [
        ("s-b", "2 B", 1),
        ("s-a", "1 A", 1),
        ("s-a", "2 B", 2),
        ("s-a", "3 C", 3),
    ]
    capped = select_hints([first, second], max_entries=2, relative_floor=0.8, best_chunk_sim=0.9)
    assert [(h.source_id, h.rank) for h in capped] == [("s-b", 1), ("s-a", 1)]
    assert select_hints([first], max_entries=0, relative_floor=0.8, best_chunk_sim=0.9) == []
    assert select_hints([first], max_entries=8, relative_floor=0.8, best_chunk_sim=2.0) == []


# ---------------------------------------------------------------- addresses


def test_address_book_is_stable_and_run_local():
    outline = build_outline(UCIE)
    mask = _by_label(outline, "5.7.2.1 Loss and Crosstalk Mask")
    standard = _by_label(outline, "5.7.3 Standard Package")
    book = AddressBook()
    assert book.assign("", "src-1", mask) == "s1"
    assert book.assign("", "src-1", standard) == "s2"
    assert book.assign("", "src-1", mask) == "s1"
    assert book.assign("nb-peer", "src-1", mask) == "s3"  # another owner
    assert book.assign("", "src-2", mask) == "s4"
    address = book.get("[S1]")
    assert address is not None
    assert (address.notebook_id, address.source_id, address.label) == (
        "",
        "src-1",
        "5.7.2.1 Loss and Crosstalk Mask",
    )
    assert address.chunk_ids == mask.range_chunk_ids
    assert address.occurrence == mask.index
    assert book.sid_of("", "src-1", standard.index) == "s2"
    assert "s2" in book and "s9" not in book and len(book) == 4
    assert set(book.addresses) == {"s1", "s2", "s3", "s4"}
    assert parse_section_id(" s12 ") == "s12" and parse_section_id("5.7") == ""


# ---------------------------------------------------------------- resolving


def test_resolve_section_forms():
    outline = build_outline(UCIE)
    standard = _by_label(outline, "5.7.3 Standard Package")
    assert resolve_section(outline, "5.7.3 Standard Package", frozenset()) == Resolved(standard)
    assert resolve_section(outline, "  5.7.3   standard package ", frozenset()) == Resolved(standard)
    assert resolve_section(outline, "5.7.3", frozenset()) == Resolved(standard)
    # A wrong title after the right number still reads the number.
    assert resolve_section(outline, "5.7.3 Standard Pkg", frozenset()) == Resolved(standard)
    virtual = resolve_section(outline, "5.7", frozenset())
    assert isinstance(virtual, Resolved) and virtual.occurrence.kind == KIND_VIRTUAL
    assert resolve_section(outline, "9.9.9", frozenset()) == NotFound()
    assert resolve_section(outline, "Nonexistent Section", frozenset()) == NotFound()
    assert resolve_section(outline, "", frozenset()) == NotFound()


def test_resolve_section_breadcrumb_last_segment():
    outline = build_outline(_rows(("Spec", 1), ("Spec > 5.7 Channel", 1)))
    result = resolve_section(outline, "5.7 Channel", frozenset())
    assert isinstance(result, Resolved) and result.occurrence.label == "Spec > 5.7 Channel"


def test_resolve_section_duplicates():
    outline = build_outline(
        _rows(("1 Part One", 1), ("Overview", 2), ("2 Part Two", 1), ("Overview", 1))
    )
    first, second = [occ for occ in outline.occurrences if occ.label == "Overview"]
    ambiguous = resolve_section(outline, "Overview", frozenset())
    assert ambiguous == Ambiguous((first, second))
    assert resolve_section(outline, "Overview", frozenset({"c4"})) == Resolved(second)
    assert resolve_section(outline, "Overview", frozenset({"c2"})) == Resolved(first)
    both = resolve_section(outline, "Overview", frozenset({"c1", "c4"}))
    assert both == Ambiguous((first, second))


# ---------------------------------------------------------------- rendering


def test_render_outline_tree_and_notes():
    outline = build_outline(UCIE)
    view = filter_outline(outline, "5.7")
    book = AddressBook()
    entries = [(book.assign("", "src", occ), occ) for occ in view.entries]
    full = render_outline(entries, max_chars=0, truncated_scan=False, scan_rows=len(UCIE))
    lines = full.text.split("\n")
    assert lines[0] == "[s1] 5.7（10 段）"
    assert lines[1] == "  [s2] 5.7.1 Overview（2 段）"  # + its figure caption leaf
    assert "      [s6] Table 5-17. Advanced Package Loss and Crosstalk Mask（1 段）" in lines
    assert full.listed == len(entries) and full.omitted == 0

    scan = render_outline(entries, max_chars=0, truncated_scan=True, scan_rows=4000)
    assert scan.text.endswith("大纲只覆盖本篇前 4000 段")

    cut = render_outline(entries, max_chars=120, truncated_scan=True, scan_rows=4000)
    assert len(cut.text) <= 120
    assert cut.listed + cut.omitted == len(entries) and cut.omitted > 0
    assert f"另有 {cut.omitted} 条未列出，可用 under/query 收窄" in cut.text
    assert cut.text.endswith("大纲只覆盖本篇前 4000 段")
    assert cut.text.split("\n")[: cut.listed] == lines[: cut.listed]


def test_render_breadcrumb_shows_last_segment():
    outline = build_outline(_rows(("Spec", 1), ("Spec > Scope", 2)))
    entries = [(f"s{i + 1}", occ) for i, occ in enumerate(outline.occurrences)]
    text = render_outline(entries, max_chars=3000, truncated_scan=False, scan_rows=3).text
    assert text == "[s1] Spec（3 段）\n  [s2] Scope（2 段）"


def test_render_hints():
    outline = build_outline(UCIE)
    mask = _by_label(outline, "5.7.2.1 Loss and Crosstalk Mask")
    ranking = SourceRanking(
        notebook_id="",
        source_id="src",
        source_title="UCIe 2.0",
        ranked=tuple(rank_sections(outline, {mask.chunk_ids[0]: 0.9})),
    )
    hints = select_hints([ranking], max_entries=8, relative_floor=0.8, best_chunk_sim=0.9)
    text = render_hints([("s3", hint) for hint in hints])
    assert text == (
        f"{HINT_BLOCK_TITLE}\n"
        "[s3]《UCIe 2.0》 5.7.2.1 Loss and Crosstalk Mask（3 段）"
    )
    assert HINT_BLOCK_TITLE == "相关章节线索（可用 read_section 按章节读取原文）"
    assert render_hints([]) == ""


# ---------------------------------------------------------------- UCIe-like


def test_ucie_like_flat_outline():
    outline = build_outline(UCIE, truncated=False)
    labels = _labels(outline.occurrences)
    for navigation in ("Contents", "Tables", "Figures"):
        assert navigation not in labels
    mask = _by_label(outline, "5.7.2.1 Loss and Crosstalk Mask")
    table17 = _by_label(outline, "Table 5-17. Advanced Package Loss and Crosstalk Mask")
    standard = _by_label(outline, "5.7.3 Standard Package")
    table18 = _by_label(outline, "Table 5-18. Standard Package Insertion Loss")
    driver = _by_label(outline, "5.7.3.1 Driver Architecture")
    assert set(table17.chunk_ids) <= set(mask.range_chunk_ids)
    assert set(table18.chunk_ids) <= set(standard.range_chunk_ids)
    assert set(driver.chunk_ids) <= set(standard.range_chunk_ids)
    assert not set(standard.range_chunk_ids) & set(mask.range_chunk_ids)
    # Front matter title page is a top-level leaf; navigation chunk ids never
    # appear in any range.
    assert _by_label(outline, "UCIe Specification").kind == KIND_LEAF
    navigation_ids = {cid for cid, _label, nav in UCIE if nav}
    assert not any(navigation_ids & set(occ.range_chunk_ids) for occ in outline.occurrences)
    assert outline.section_count == len(labels) - 2  # virtual 5 and 5.7
    assert outline.numbered


# ---------------------------------------------------------------- review follow-ups


@pytest.mark.parametrize(
    "inside",
    ["16 GT/s Data Rate", "2种低功耗模式", "6 Lanes Configuration"],
)
def test_bare_number_inside_a_section_is_a_leaf(inside):
    outline = build_outline(
        _rows(
            ("5.7.2 Advanced Package", 1),
            ("5.7.3 Standard Package", 1),
            (inside, 1),
            ("Table 5-18. Standard Package Insertion Loss", 1),
            ("5.7.4 Next", 1),
        )
    )
    assert _by_label(outline, inside).kind == KIND_LEAF
    standard = _by_label(outline, "5.7.3 Standard Package")
    assert standard.range_chunk_ids == ("c1", "c2", "c3")
    virtual = [occ.label for occ in outline.occurrences if occ.virtual]
    assert virtual == ["5", "5.7"]
    resolved = resolve_section(outline, "5.7", frozenset())
    assert isinstance(resolved, Resolved)
    assert resolved.occurrence.range_chunk_ids == ("c0", "c1", "c2", "c3", "c4")


def test_bare_numbers_in_chapter_sequence_are_numbered():
    outline = build_outline(
        _rows(
            ("1 Scope", 1),
            ("2 References", 1),
            ("3 Terms", 1),
            ("3.1 Abbreviations", 1),
            ("4 Electrical", 1),
            ("4.9 Last", 1),
            ("5 Mechanical", 1),  # last chapter, no subsection
            ("A.1 Bump Map", 1),
            ("B Glossary", 1),  # letter sequence after A
        )
    )
    for label, number in (
        ("1 Scope", "1"),
        ("2 References", "2"),
        ("3 Terms", "3"),
        ("4 Electrical", "4"),
        ("5 Mechanical", "5"),
        ("B Glossary", "B"),
    ):
        occ = _by_label(outline, label)
        assert occ.kind == KIND_NUMBERED and occ.number == number, label
    assert _by_label(outline, "4 Electrical").range_chunk_ids == ("c4", "c5")


def test_caption_word_needs_every_occurrence_corroborated():
    outline = build_outline(
        _rows(
            ("1 Intro", 1),
            ("Table 1 Pins", 1),
            ("Table 2 Timing", 1),
            ("2 Electrical", 1),
            ("2.1 DC", 1),
        )
    )
    assert _by_label(outline, "Table 1 Pins").kind == KIND_LEAF
    assert _by_label(outline, "Table 2 Timing").kind == KIND_LEAF
    assert _by_label(outline, "1 Intro").range_chunk_ids == ("c0", "c1", "c2")
    resolved = resolve_section(outline, "2", frozenset())
    assert isinstance(resolved, Resolved) and resolved.occurrence.label == "2 Electrical"


def test_rank_keeps_subsections_of_an_ancestor_with_its_own_best_chunk():
    rows = [("r0", "Doc", False)] + [(f"c{i}", f"Doc > S{i}", False) for i in range(10)]
    outline = build_outline(rows)
    sims = {"r0": 0.9, **{f"c{i}": 0.8 for i in range(10)}}
    ranked = rank_sections(outline, sims)
    assert [item.occurrence.label for item in ranked] == ["Doc"] + [f"Doc > S{i}" for i in range(10)]
    assert len(rank_sections(outline, sims, limit=3)) == 3

    flat = build_outline(
        _rows(("5 Electrical", 1), ("5.1 DC", 1), ("5.2 AC", 1), ("6 Next", 1))
    )
    ranked = rank_sections(flat, {"c0": 0.95, "c1": 0.9, "c2": 0.85, "c3": 0.1})
    assert [item.occurrence.label for item in ranked] == [
        "5 Electrical",
        "5.1 DC",
        "5.2 AC",
        "6 Next",
    ]


def test_resolve_number_fallback_only_reads_dotted_or_digit_numbers():
    outline = build_outline(
        _rows(
            ("7 Registers", 1),
            ("7.1 Map", 1),
            ("Appendix A", 1),
            ("A.1 Bump Map", 1),
            ("5.7 Channel", 1),
        )
    )
    assert resolve_section(outline, "Table 7", frozenset()) == NotFound()
    assert resolve_section(outline, "A Brief History", frozenset()) == NotFound()
    full_width = resolve_section(outline, "Ａ.1", frozenset())
    assert isinstance(full_width, Resolved) and full_width.occurrence.label == "A.1 Bump Map"
    wide = resolve_section(outline, "５.７", frozenset())
    assert isinstance(wide, Resolved) and wide.occurrence.label == "5.7 Channel"
    by_label = resolve_section(outline, "appendix  a", frozenset())
    assert isinstance(by_label, Resolved) and by_label.occurrence.label == "Appendix A"


def test_filter_outline_tells_refusals_apart():
    outline = build_outline(UCIE)
    assert filter_outline(outline, "").status == UNDER_OK
    assert _labels(filter_outline(outline, "5.7.3.").entries)[0] == "5.7.3 Standard Package"
    assert _labels(filter_outline(outline, "５.７.３").entries)[0] == "5.7.3 Standard Package"
    unparsed = filter_outline(outline, "Standard Package")
    assert unparsed.status == UNDER_UNPARSED and not unparsed.under_supported
    flat = build_outline(_rows(("Overview", 1), ("Usage", 1)))
    no_numbering = filter_outline(flat, "5.7")
    assert no_numbering.status == UNDER_NO_NUMBERING and no_numbering.entries == ()


def test_two_flat_headings_with_crumb_separator_stay_flat():
    outline = build_outline(
        _rows(
            ("5.7 Package", 1),
            ("Figure 5-1", 1),
            ("VIN > VOUT behaviour", 1),
            ("Notes", 1),
            ("A > B compare", 1),
            ("5.8 Next", 1),
        )
    )
    for label in ("VIN > VOUT behaviour", "A > B compare", "Notes", "Figure 5-1"):
        assert _by_label(outline, label).kind == KIND_LEAF
    assert _by_label(outline, "5.7 Package").range_chunk_ids == ("c0", "c1", "c2", "c3", "c4")


def test_render_clips_an_overlong_first_line_and_never_exceeds():
    long_title = "X" * 400
    outline = build_outline(_rows((long_title, 1), ("1 A", 1), ("1.1 B", 1)))
    entries = [(f"s{i + 1}", occ) for i, occ in enumerate(outline.occurrences)]
    cut = render_outline(entries, max_chars=RENDER_MIN_CHARS, truncated_scan=True, scan_rows=4000)
    assert len(cut.text) <= RENDER_MIN_CHARS
    first = cut.text.split("\n")[0]
    assert first.startswith("[s1] XXX") and first.endswith("…（1 段）")
    assert cut.listed == 1 and cut.omitted == 2
    assert "另有 2 条未列出" in cut.text and cut.text.endswith("大纲只覆盖本篇前 4000 段")
    for tiny in (1, 10, 30):
        squeezed = render_outline(entries, max_chars=tiny, truncated_scan=True, scan_rows=4000)
        assert len(squeezed.text) <= tiny


def test_render_ranked_is_flat_with_full_labels():
    outline = build_outline(_rows(("Spec", 1), ("Spec > Scope", 2)))
    scope = _by_label(outline, "Spec > Scope")
    spec = _by_label(outline, "Spec")
    text = render_ranked(
        [("s2", scope), ("s1", spec)], max_chars=3000, truncated_scan=False, scan_rows=3
    ).text
    assert text == "[s2] Spec > Scope（2 段）\n[s1] Spec（3 段）"


# ------------------------------------------------ open ranges (scan cut, R2)


def _cut_outline():
    """A scan cut inside 5.7.3: the rows past the bound were never read."""
    return build_outline(_rows(
        ("5 Electrical", 1), ("5.7.1 Overview", 1), ("5.7.2.1 Mask", 1),
        ("5.7.3 Standard Package", 2),
    ), truncated=True)


def test_a_cut_scan_marks_every_range_reaching_its_end_open():
    outline = _cut_outline()
    open_labels = [occ.label for occ in outline.occurrences if occ.range_open]
    # 5.7.3 is where the scan stopped; 5 and the virtual 5.7 still enclose it
    assert open_labels == ["5 Electrical", "5.7", "5.7.3 Standard Package"]
    for label in ("5.7.1 Overview", "5.7.2", "5.7.2.1 Mask"):
        assert _by_label(outline, label).range_open is False


def test_an_uncut_scan_has_no_open_range():
    outline = build_outline(_rows(("5 Electrical", 1), ("5.7.3 Standard Package", 2)))
    assert not any(occ.range_open for occ in outline.occurrences)


def test_open_ranges_travel_into_addresses_and_every_rendering():
    outline = _cut_outline()
    book = AddressBook()
    std = _by_label(outline, "5.7.3 Standard Package")
    mask = _by_label(outline, "5.7.2.1 Mask")
    sid_std, sid_mask = book.assign("", "src", std), book.assign("", "src", mask)
    assert book.get(sid_std).incomplete is True
    assert book.get(sid_mask).incomplete is False
    tree = render_outline([(sid_std, std), (sid_mask, mask)], max_chars=0,
                          truncated_scan=True, scan_rows=5)
    assert "Standard Package（2 段，可能未完）" in tree.text
    assert "Mask（1 段）" in tree.text
    ranked = render_ranked([(sid_std, std)], max_chars=0, truncated_scan=False,
                           scan_rows=0)
    assert ranked.text == f"[{sid_std}] 5.7.3 Standard Package（2 段，可能未完）"
    hints = render_hints([(sid_std, SimpleHint(std))])
    assert hints.splitlines()[1].endswith("5.7.3 Standard Package（2 段，可能未完）")


class SimpleHint:
    """Duck-typed ``SectionHint`` (render_hints reads two fields)."""

    def __init__(self, occurrence, source_title="T"):
        self.occurrence = occurrence
        self.source_title = source_title
