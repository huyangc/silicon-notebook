"""Navigation-page detection on synthetic documents (plan 2026-10-21 §3.1, T1).

The thresholds in ``app.services.navigation_chunks`` are calibrated on these
documents: each case states what a reader would call a navigation page and
what not, independent of how the page is labelled. Chunks are built by the
real ``build_chunks`` so the ``[label] `` prefix and the merge boundaries are
the production ones.
"""
from __future__ import annotations

import itertools

import pytest

from app.services import navigation_chunks as nav
from app.services.chunking import build_chunks
from app.services.navigation_chunks import detect_navigation_chunks, normalise, title_key


class _Doc:
    def __init__(self) -> None:
        self.elements: list[dict] = []
        self._ids = itertools.count(1)

    def h(self, text: str, section_path: str = "") -> "_Doc":
        self.elements.append({
            "id": f"el-{next(self._ids):05d}", "element_type": "heading",
            "text": text, "section_path": section_path,
        })
        return self

    def p(self, text: str) -> "_Doc":
        self.elements.append({
            "id": f"el-{next(self._ids):05d}", "element_type": "paragraph",
            "text": text, "section_path": "",
        })
        return self

    def detect(self, target_chars: int = 600):
        chunks = build_chunks(self.elements, target_chars=target_chars)
        result = detect_navigation_chunks(
            self.elements, [(c["section_path"], c["text"]) for c in chunks]
        )
        assert len(result.flags) == len(chunks)
        return chunks, result


def _flagged_sections(chunks, result) -> list[str]:
    return [c["section_path"] for c, flag in zip(chunks, result.flags) if flag]


def _metrics_for(chunks, result, section_path: str) -> list:
    return [m for c, m in zip(chunks, result.metrics) if c["section_path"] == section_path]


def _unflagged_sections(chunks, result) -> set[str]:
    return {c["section_path"] for c, flag in zip(chunks, result.flags) if not flag}


# --------------------------------------------------------------- the spec case
SPEC_SECTIONS = [
    "1 Introduction to Chiplet Interconnect",
    "1.1 Package Integration Options",
    "1.2 Key Performance Targets",
    "2 Protocol Layer Architecture",
    "2.1 Flit Format Definition",
    "2.2 Retry Buffer Management",
    "3 Die-to-Die Adapter Behavior",
    "3.1 Link Initialization Sequence",
    "4 Logical Physical Layer",
    "4.1 Lane Reversal Support",
    "5 Electrical Layer Specification",
    "5.7 Channel Characteristics",
    "5.7.1 Voltage Transfer Function",
    "5.7.2 Advanced Package Channel",
    "5.7.2.1 Loss and Crosstalk Mask",
    "5.7.3 Standard Package Channel",
    "5.7.3.1 Insertion Loss Requirements",
    "6 Sideband Signaling Interface",
]
SPEC_FIGURES = [
    "Figure 1-1. Chiplet Package Cross Section",
    "Figure 2-1. Flit Layout for Streaming Protocol",
    "Figure 3-1. Adapter State Machine Overview",
    "Figure 4-1. Lane Reversal Mapping Example",
    "Figure 5-1. Voltage Transfer Function Measurement Setup",
    "Figure 5-2. Eye Mask Definition Diagram",
    "Figure 5-3. Crosstalk Aggressor Arrangement",
    "Figure 6-1. Sideband Packet Timing Diagram",
]
SPEC_TABLES = [
    "Table 1-1. Performance Target Summary",
    "Table 2-1. Flit Header Field Encoding",
    "Table 3-1. Adapter Timeout Values",
    "Table 4-1. Lane Map Encoding Rules",
    "Table 5-17. Advanced Package Loss Budget",
    "Table 5-18. Standard Package Channel Parameters",
    "Table 5-25. Receiver Termination Limits",
    "Table 6-1. Sideband Opcode Assignment",
]
_BODY = (
    "The transmitter shall drive the lane with a swing between the stated "
    "limits and the receiver shall tolerate the specified common mode noise. "
    "Compliance is verified at the package bump with a calibrated fixture. "
)


def _toc_line(title: str, page: int) -> str:
    # MinerU table-cell residue: the number, then the title split across a
    # cell boundary where the original line wrapped, then dot leaders + page.
    number, _, rest = title.partition(" ")
    if "Voltage Transfer" in rest:
        rest = rest.replace("Voltage Transfer", "Voltage T | ransfer")
    return f"| {number} | {rest} | {'.' * 18} | {page} |"


def _spec_doc() -> _Doc:
    doc = _Doc()
    doc.h("Contents")
    for index, title in enumerate(SPEC_SECTIONS):
        doc.p(_toc_line(title, 10 + index * 7))
    doc.h("List of Figures")
    for index, title in enumerate(SPEC_FIGURES):
        doc.p(f"{title} {'.' * 20} {12 + index * 9}")
    doc.h("List of Tables")
    for index, title in enumerate(SPEC_TABLES):
        doc.p(f"{title} {'.' * 20} {14 + index * 9}")
    figures = iter(SPEC_FIGURES)
    tables = iter(SPEC_TABLES)
    for title in SPEC_SECTIONS:
        doc.h(title)
        doc.p(_BODY + "See the related requirements in this section for the exact values.")
        if title.startswith(("1.2", "2.1", "3.1", "4.1", "5.7.1", "5.7.2.1", "5.7.3.1", "6 ")):
            doc.h(next(figures))
            doc.p("The figure shows the arrangement used for the measurement described above.")
            doc.h(next(tables))
            doc.p("| Parameter | Min | Max |\n| Insertion loss at Nyquist | - | 6 dB |")
    # body text that cross-references a few sections in passing
    doc.h("6.1 Sideband Error Handling")
    doc.p(
        "Errors are reported as described in Retry Buffer Management and the "
        "Link Initialization Sequence; electrical limits follow the Loss and "
        "Crosstalk Mask. " + _BODY
    )
    return doc


def test_spec_toc_and_lists_of_figures_and_tables_are_flagged():
    chunks, result = _spec_doc().detect()
    flagged = _flagged_sections(chunks, result)
    assert result.skipped == ""
    assert "Contents" in flagged
    assert "List of Figures" in flagged
    assert "List of Tables" in flagged
    # every chunk of the three navigation sections that lists >= 6 entries is
    # flagged; no body section is
    assert set(flagged) == {"Contents", "List of Figures", "List of Tables"}
    unflagged = _unflagged_sections(chunks, result)
    assert "5.7.2.1 Loss and Crosstalk Mask" in unflagged
    assert "6.1 Sideband Error Handling" in unflagged


def test_mineru_split_words_and_dot_leaders_still_match():
    assert normalise("Voltage T | ransfer Function ....... 87") == "voltagetransferfunction87"
    chunks, result = _spec_doc().detect()
    toc = [
        (c, m) for c, m in zip(chunks, result.metrics) if c["section_path"] == "Contents"
    ]
    assert toc
    assert any("ransfer" in c["text"] for c, _m in toc)
    for chunk, metrics in toc:
        if "ransfer" in chunk["text"]:
            assert metrics.letters_coverage >= nav.NAV_MIN_LETTER_COVERAGE
            assert metrics.ordered_refs >= nav.NAV_MIN_REFERENCES


def test_body_with_occasional_section_citations_is_not_flagged():
    chunks, result = _spec_doc().detect()
    cite = [
        m for c, m in zip(chunks, result.metrics)
        if c["section_path"] == "6.1 Sideband Error Handling"
    ]
    assert cite and cite[0].hits == 3
    assert cite[0].ordered_refs < nav.NAV_MIN_REFERENCES


# ------------------------------------------------------- breadcrumb markdown
def _breadcrumb_doc(toc_heading: str) -> _Doc:
    root = "Operator Handbook"
    sections = [
        ("Installation and Setup", ["System Requirements Overview", "Package Installation Steps"]),
        ("Configuration Reference", ["Environment Variable Catalog", "Logging Configuration Options"]),
        ("Backup and Restore", ["Scheduled Snapshot Policy", "Point in Time Recovery"]),
        ("Troubleshooting Guide", ["Diagnostic Log Collection"]),
    ]
    doc = _Doc()
    doc.h(root, root)
    doc.h(toc_heading, f"{root} > {toc_heading}")
    for parent, children in sections:
        doc.p(f"- [{parent}](#{parent.lower().replace(' ', '-')})")
        for child in children:
            doc.p(f"  - [{child}](#{child.lower().replace(' ', '-')})")
    for parent, children in sections:
        doc.h(parent, f"{root} > {parent}")
        doc.p("This part explains the procedure in detail. " + _BODY)
        for child in children:
            doc.h(child, f"{root} > {parent} > {child}")
            text = _BODY
            if child == "Point in Time Recovery":
                text = "Restore from the newest snapshot first (see Scheduled Snapshot Policy). " + _BODY
            doc.p(text)
    return doc


@pytest.mark.parametrize("toc_heading", ["Contents", "目录", "Where to find things"])
def test_breadcrumb_markdown_toc_section_is_flagged_whatever_it_is_called(toc_heading):
    chunks, result = _breadcrumb_doc(toc_heading).detect()
    flagged = _flagged_sections(chunks, result)
    assert flagged == [f"Operator Handbook > {toc_heading}"]
    recovery = [
        m for c, m in zip(chunks, result.metrics)
        if c["section_path"].endswith("Point in Time Recovery")
    ]
    assert recovery and recovery[0].hits == 1


# ------------------------------------------------- CJK titles without spaces
CJK_SECTIONS = [
    "5.7电气通道特性",
    "5.7.1电压传递函数",
    "5.7.2先进封装通道",
    "5.7.2.1损耗",
    "5.7.2.2串扰",
    "5.7.2.3回波损耗",
    "5.7.3标准封装通道",
    "5.7.3.1插入损耗",
    "5.7.3.2近端串扰",
]


def test_cjk_numbered_titles_without_spaces():
    assert title_key("5.7.2.1损耗") == "5721损耗"  # stripped "损耗" is too short
    assert title_key("5.7.3标准封装通道") == "标准封装通道"
    assert title_key("1概述") == ""
    doc = _Doc().h("目录")
    for index, title in enumerate(CJK_SECTIONS):
        doc.p(f"{title}{'…' * 12}{30 + index}")
    for title in CJK_SECTIONS:
        doc.h(title).p("本节规定通道在奈奎斯特频率处的插入损耗与串扰上限，测试夹具需事先校准。")
    chunks, result = doc.detect()
    assert _flagged_sections(chunks, result) == ["目录"]


# ---------------------------------------------- chapter-opening overview text
def test_chapter_opening_overview_paragraph_is_not_flagged():
    """A chapter's opening paragraph that lists its own subsections in order
    looks like a TOC by every other measure, but its whole chain lies inside
    its own section's subtree (locality 1): it is chapter text, kept."""
    subsections = [
        "5.1 Electrical Layer Overview",
        "5.2 Transmitter Equalization Settings",
        "5.3 Receiver Sensitivity Limits",
        "5.4 Clocking Architecture Details",
        "5.5 Lane Reversal Support",
        "5.6 Channel Characteristics",
        "5.7 Sideband Signaling Interface",
    ]
    doc = _Doc().h("5 Electrical Layer Specification")
    doc.p(
        "This chapter covers " + ", ".join(t.split(" ", 1)[1] for t in subsections)
        + "."
    )
    for title in subsections:
        doc.h(title).p(_BODY)
    chunks, result = doc.detect()
    overview = _metrics_for(chunks, result, "5 Electrical Layer Specification")[0]
    assert overview.ordered_refs == 7 and overview.letters_coverage > 0.9
    assert overview.locality == 1.0
    assert _flagged_sections(chunks, result) == []


# ------------------------- parameter table whose row labels are section titles
def test_parameter_table_with_section_titles_out_of_order_is_not_flagged():
    titles = [
        "5.1 Transmitter Swing Range",
        "5.2 Receiver Sensitivity Limits",
        "5.3 Termination Resistance Values",
        "5.4 Clock Jitter Tolerance",
        "5.5 Lane Skew Budget",
        "5.6 Supply Noise Rejection",
        "5.7 Crosstalk Isolation Margin",
        "5.8 Return Loss Requirement",
    ]
    row_order = [5, 7, 2, 4, 0, 3, 6, 1]  # longest in-order run: 3
    doc = _Doc()
    for title in titles:
        doc.h(title).p(_BODY)
    doc.h("5.9 Electrical Parameter Summary")
    doc.p("| Parameter | Min | Typ | Max |")
    for index in row_order:
        doc.p(f"| {titles[index].split(' ', 1)[1]} | 0.{index} | 1.{index} | 2.{index} |")
    chunks, result = doc.detect()
    table = [
        m for c, m in zip(chunks, result.metrics)
        if c["section_path"] == "5.9 Electrical Parameter Summary"
    ]
    assert table and table[0].hits == 8
    assert table[0].letters_coverage >= nav.NAV_MIN_LETTER_COVERAGE  # mostly titles ...
    assert table[0].ordered_refs == 3  # ... but not in document order
    assert _flagged_sections(chunks, result) == []


# ------------------------------------------------------------- short documents
def test_short_document_toc_cannot_reach_the_reference_floor():
    titles = ["Getting Started Quickly", "Uploading Your Documents",
              "Asking Good Questions", "Sharing With Colleagues"]
    doc = _Doc().h("Contents")
    for title in titles:
        doc.p(f"{title} ........ 3")
    for title in titles:
        doc.h(title).p(_BODY)
    chunks, result = doc.detect()
    assert _flagged_sections(chunks, result) == []


# ------------------------------------------------------ duplicate short titles
def test_duplicate_titles_count_once_and_short_titles_do_not_take_part():
    doc = _Doc()
    for number in range(1, 9):
        doc.h(f"{number} Transceiver Block {chr(64 + number)} Requirements").p(_BODY)
        doc.h("Notes").p("Notes apply to the whole block.")
        doc.h("Electrical Parameters Table").p("| Parameter | Value |")
    doc.h("9 Cross Reference")
    doc.p(" ".join(["Electrical Parameters Table."] * 10))
    chunks, result = doc.detect()
    cross = [
        m for c, m in zip(chunks, result.metrics) if c["section_path"] == "9 Cross Reference"
    ]
    assert cross[0].coverage > 0.9
    assert cross[0].hits == 1 and cross[0].ordered_refs == 1
    assert title_key("Notes") == ""
    assert title_key("Overview") == ""
    assert _flagged_sections(chunks, result) == []


def test_toc_listing_duplicate_titles_is_still_flagged():
    doc = _Doc().h("Table of Contents")
    blocks = [f"{n} Transceiver Block {chr(64 + n)} Requirements" for n in range(1, 9)]
    for block in blocks:
        doc.p(f"{block} .... 1")
        doc.p("Electrical Parameters Table .... 2")
    for block in blocks:
        doc.h(block).p(_BODY)
        doc.h("Electrical Parameters Table").p("| Parameter | Value |")
    chunks, result = doc.detect()
    assert _flagged_sections(chunks, result) == ["Table of Contents"]


# ------------------------------------------------------------ appendix numbers
def test_appendix_numbering():
    assert title_key("A.2 Reference Channel Models") == "referencechannelmodels"
    appendix = [
        "A.1 Compliance Test Fixtures",
        "A.2 Reference Channel Models",
        "A.3 Package Model Extraction",
        "A.4 Statistical Eye Analysis",
        "A.5 Jitter Decomposition Method",
        "A.6 Crosstalk Summation Rules",
    ]
    doc = _Doc().h("Appendix Contents")
    for index, title in enumerate(appendix):
        doc.p(f"{title} ...... {200 + index}")
    doc.h("A Compliance Appendix").p(_BODY)
    for title in appendix:
        doc.h(title).p(_BODY)
    doc.h("A.2.1 Channel Model Usage").p(
        "Use the Reference Channel Models with the Compliance Test Fixtures. " + _BODY
    )
    chunks, result = doc.detect()
    assert _flagged_sections(chunks, result) == ["Appendix Contents"]
    usage = [
        m for c, m in zip(chunks, result.metrics)
        if c["section_path"] == "A.2.1 Channel Model Usage"
    ]
    # its ancestor A.2 is excluded by numbering, so only A.1 is a hit
    assert usage[0].hits == 1


def test_own_section_and_ancestors_are_not_hits():
    doc = _Doc()
    root = "Platform Guide"
    doc.h(root, root)
    doc.h("Storage Engine Internals", f"{root} > Storage Engine Internals")
    doc.h("Write Ahead Logging", f"{root} > Storage Engine Internals > Write Ahead Logging")
    doc.p(
        "Platform Guide / Storage Engine Internals / Write Ahead Logging: the "
        "log is flushed before the page."
    )
    chunks, result = doc.detect()
    assert result.metrics[-1].hits == 0


# --------------------------------------------------------------- the size cap
def test_source_over_the_title_cap_is_skipped():
    doc = _Doc().h("Contents")
    for number in range(nav.NAV_MAX_TITLES + 1):
        doc.h(f"{number} Generated Section Title {number}")
    doc.p("body")
    chunks, result = doc.detect()
    assert result.skipped == nav.SKIPPED_TOO_MANY_TITLES
    assert result.title_count == nav.NAV_MAX_TITLES + 2
    assert not any(result.flags)
    assert result.metrics == ()


def test_empty_inputs():
    result = detect_navigation_chunks([], [("", "some text")])
    assert result.flags == (False,)
    assert detect_navigation_chunks([], []).flags == ()


# ------------------------------------------- contained-title deduplication
def test_a_section_title_inside_a_cited_figure_caption_counts_once():
    """Body text that cites figures whose captions contain section titles:
    each caption is ONE hit, the section title inside it is not counted again
    (without the containment rule the same text reads as six hits)."""
    sections = ["5.1 Voltage Transfer Function", "5.2 Eye Mask Definition",
                "5.3 Crosstalk Aggressor Arrangement"]
    figures = [f"Figure 5-{i}. {title.split(' ', 1)[1]} Measurement Setup"
               for i, title in enumerate(sections, start=1)]
    doc = _Doc()
    for section, figure in zip(sections, figures):
        doc.h(section).p(_BODY)
        doc.h(figure).p("The figure shows the setup.")
    doc.h("5.4 Measurement Summary")
    doc.p("See " + "; ".join(figures) + ".")
    chunks, result = doc.detect()
    summary = [m for c, m in zip(chunks, result.metrics)
               if c["section_path"] == "5.4 Measurement Summary"]
    assert summary[0].hits == 3
    assert summary[0].ordered_refs == 3
    assert _flagged_sections(chunks, result) == []


# ------------------------------- duplicate titles in document order (LIS)
def test_a_duplicate_title_extends_the_ordered_run_at_most_once():
    """A title shared by several headings stands for ONE of them per
    occurrence (its orders are fed in descending order), and the run is
    strictly increasing (a repeated title does not extend it). Here the
    distinct-title cap (6) does not mask either: the correct run is 2, an
    ascending feed would give 4 and a non-strict run 3."""
    singles = [f"{n} Interface Block {chr(64 + n)} Overview" for n in range(1, 6)]
    duplicate = "Electrical Parameters Table"
    doc = _Doc()
    for title in singles:
        doc.h(title).p(_BODY)
    for _ in range(3):
        doc.h(duplicate).p("| Parameter | Value |")
    doc.h("9 Summary Of Changes")
    names = [title.split(" ", 1)[1] for title in singles]
    doc.p(" ".join(reversed(names)) + " " + names[0] + " " + duplicate)
    chunks, result = doc.detect()
    summary = [m for c, m in zip(chunks, result.metrics)
               if c["section_path"] == "9 Summary Of Changes"]
    assert summary[0].hits == 6
    assert summary[0].ordered_refs == 2


# ==================================================================== the five
# structural conditions (refs / ratio / letters coverage / locality / density)
_WORDS = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]


def _candidates(chunks, result, section_path: str) -> list:
    """Metrics of the section's chunks that reach the reference floor."""
    return [m for m in _metrics_for(chunks, result, section_path)
            if m.ordered_refs >= nav.NAV_MIN_REFERENCES]


def _dense_sections(chapters: int = 8, per_chapter: int = 5, deep: bool = False):
    """``(title, depth)`` of a realistic numbered document: every chapter has
    ``per_chapter`` sections; ``deep`` adds levels 3 and 4."""
    sections = []
    for c in range(1, chapters + 1):
        sections.append((f"{c} Chapter {_WORDS[c - 1]} Main Topic", 1))
        for s in range(1, per_chapter + 1):
            sections.append((f"{c}.{s} Section {_WORDS[c - 1]} {_WORDS[s - 1]} Behaviour Rules", 2))
            if deep:
                for t in range(1, 3):
                    sections.append((f"{c}.{s}.{t} Clause {_WORDS[c - 1]} {_WORDS[s - 1]} {_WORDS[t - 1]} Detail", 3))
                    sections.append((f"{c}.{s}.{t}.1 Item {_WORDS[c - 1]} {_WORDS[s - 1]} {_WORDS[t - 1]} Fine Print", 4))
    return sections


def _with_body(doc: _Doc, sections) -> _Doc:
    for title, _depth in sections:
        doc.h(title).p(_BODY)
    return doc


# --------------------------------------------------- tables inside one chapter
_REGISTERS = [
    "UCIe Link Capability Register", "UCIe Link Control Register", "UCIe Link Status Register",
    "Link Event Notification Control", "Error Notification Control",
    "Physical Layer Capability Register", "Physical Layer Control Register",
    "Physical Layer Status Register", "Sideband Mailbox Index Register",
    "Sideband Mailbox Data Register", "Training Setup Register One", "Training Setup Register Two",
    "Adapter Timeout Control Register", "Retimer Capability Register",
    "Lane Margining Control Register", "Lane Margining Status Register",
]


def test_register_map_of_its_own_chapter_is_not_flagged():
    """16 register rows that are exactly the section's own subsections, in
    order: everything but locality says TOC; locality 1 says it is content."""
    doc = _Doc()
    for ch in range(1, 9):
        doc.h(f"{ch} Chapter Heading Topic {ch}").p(_BODY)
    doc.h("9 Configuration Registers").p(_BODY)
    doc.h("9.5 Register Map").p("| Offset | Register | Section |")
    for i, register in enumerate(_REGISTERS):
        doc.p(f"| 0x{i * 4:02X}h | {register} | 9.5.{i + 1} |")
    for i, register in enumerate(_REGISTERS):
        doc.h(f"9.5.{i + 1} {register}").p(_BODY)
    chunks, result = doc.detect()
    table = _candidates(chunks, result, "9.5 Register Map")
    assert table and all(m.locality == 1.0 for m in table)
    assert table[0].ratio == 1.0 and table[0].density == 1.0
    assert _flagged_sections(chunks, result) == []


def test_in_order_parameter_summary_of_its_own_chapter_is_not_flagged():
    titles = ["Transmitter Swing Range", "Receiver Sensitivity Limits",
              "Termination Resistance Values", "Clock Jitter Tolerance", "Lane Skew Budget",
              "Supply Noise Rejection", "Crosstalk Isolation Margin", "Return Loss Requirement"]
    doc = _Doc()
    for ch in range(1, 5):
        doc.h(f"{ch} Chapter Heading Topic {ch}").p(_BODY)
    doc.h("5 Electrical").p(_BODY)
    for i, title in enumerate(titles):
        doc.h(f"5.{i + 1} {title}").p(_BODY)
    doc.h("5.9 Electrical Parameter Summary").p("| Parameter | Min | Typ | Max | Unit |")
    for i, title in enumerate(titles):
        doc.p(f"| {title} | 0.{i}5 | 1.{i}0 | 2.{i}5 | mV |")
    chunks, result = doc.detect()
    summary = _candidates(chunks, result, "5.9 Electrical Parameter Summary")
    assert summary and summary[0].locality == 1.0  # the parent 5's subtree
    assert _flagged_sections(chunks, result) == []


def test_breadcrumb_summary_table_of_its_own_part_is_not_flagged():
    root, part = "Guide", "Storage Engine"
    kids = ["Write Ahead Logging", "Buffer Pool Manager", "Checkpoint Scheduling",
            "Vacuum Process Tuning", "Index Build Pipeline", "Lock Manager Design",
            "Replication Slot Handling"]
    doc = _Doc().h(root, root).h(part, f"{root} > {part}")
    for kid in kids:
        doc.h(kid, f"{root} > {part} > {kid}").p(_BODY)
    doc.h("Parameter Summary", f"{root} > {part} > Parameter Summary").p("| Area | Default |")
    for i, kid in enumerate(kids):
        doc.p(f"| {kid} | {i * 8} MB |")
    doc.h("Other Part", f"{root} > Other Part").p(_BODY)
    chunks, result = doc.detect()
    summary = _candidates(chunks, result, f"{root} > {part} > Parameter Summary")
    # its parent ("Guide > Storage Engine") is deeper than the common root
    assert summary and summary[0].locality == 1.0
    assert _flagged_sections(chunks, result) == []


# ------------------------------------------- tables that cross the chapters
_REVISION_FORMATS = {
    "updated-section-number-title": "| 2.0 | Updated Section {n} {s} |",
    "section-number-title": "| 2.0 | Section {n} {s} |",
    "updated-title": "| 2.0 | Updated {s} |",
    "title-only": "| 2.0 | {s} |",
    "prose": "| 2.{i} | 2024-0{m}-15 | Clarified the {s} wording and fixed a typo |",
}


@pytest.mark.parametrize("style", sorted(_REVISION_FORMATS))
def test_revision_history_citing_one_section_per_chapter_is_not_flagged(style):
    """A revision history citing one of each chapter's five sections, in
    order, in four terse spellings and in prose: the chain skips four of every
    five sections, so its density is about 0.2."""
    sections = _dense_sections()
    cited = [t for t, depth in sections if depth == 2 and t.split()[0].endswith(".2")]
    doc = _Doc().h("Revision History")
    for i, title in enumerate(cited):
        number, _, rest = title.partition(" ")
        doc.p(_REVISION_FORMATS[style].format(n=number, s=rest, i=i, m=i % 9 + 1))
    chunks, result = _with_body(doc, sections).detect()
    for metrics in _candidates(chunks, result, "Revision History"):
        assert metrics.density < nav.NAV_MIN_DENSITY
    assert _flagged_sections(chunks, result) == []


def test_front_matter_summary_with_one_row_per_chapter_is_not_flagged():
    sections = _dense_sections()
    cited = [t for t, depth in sections if depth == 2 and t.split()[0].endswith(".2")]
    doc = _Doc().h("Key Parameters at a Glance").p("| Item | Value |")
    for i, title in enumerate(cited):
        doc.p(f"| {title.partition(' ')[2]} | {i * 3 + 2} GT/s |")
    chunks, result = _with_body(doc, sections).detect()
    summary = _candidates(chunks, result, "Key Parameters at a Glance")
    assert summary and summary[0].density < nav.NAV_MIN_DENSITY
    assert summary[0].letters_coverage >= nav.NAV_MIN_LETTER_COVERAGE  # density decides
    assert _flagged_sections(chunks, result) == []


_ADJECTIVES = ["Transmitter", "Receiver", "Clock", "Lane", "Supply", "Crosstalk", "Return",
               "Termination", "Sideband", "Package", "Channel", "Jitter"]
_NOUNS = ["Swing Range", "Sensitivity Limit", "Skew Budget", "Noise Rejection",
          "Isolation Margin", "Loss Requirement", "Resistance Value", "Timing Margin"]


def _parameter_doc(n: int, rng, order) -> _Doc:
    pairs = rng.sample([(a, b) for a in _ADJECTIVES for b in _NOUNS], n)
    titles = [f"5.{i + 1} {a} {b}" for i, (a, b) in enumerate(pairs)]
    doc = _Doc()
    for title in titles:
        doc.h(title).p("Body text describing the requirement in detail at the bump.")
    doc.h("6 Electrical Parameter Summary")
    for i in order(n, rng):
        doc.p(f"| {titles[i].split(' ', 1)[1]} | 0.{i} | 1.{i} | 2.{i} |")
    return doc


@pytest.mark.parametrize("n", [10, 20, 40])
def test_shuffled_parameter_tables_are_never_flagged(n):
    """20 fixed seeds per size: a table whose rows are section titles in a
    random order never reaches the order ratio (or the reference floor)."""
    import random

    def shuffled(count, rng):
        order = list(range(count))
        rng.shuffle(order)
        return order

    worst = 0.0
    for seed in range(20):
        rng = random.Random(9000 + n * 100 + seed)
        chunks, result = _parameter_doc(n, rng, shuffled).detect()
        assert _flagged_sections(chunks, result) == [], seed
        for m in _metrics_for(chunks, result, "6 Electrical Parameter Summary"):
            if m.ordered_refs >= nav.NAV_MIN_REFERENCES:
                worst = max(worst, m.ratio)
    assert worst < nav.NAV_MIN_ORDER_RATIO


# ----------------------------------------------------- real navigation pages
def _big_spec() -> _Doc:
    """8 chapters, chapter 5 with 60 sections (every third with two clauses),
    a TOC of all of them and a list of 40 figures that all sit in chapter 5."""
    sections = []
    for ch in range(1, 9):
        sections.append(f"{ch} Chapter Topic Number {_WORDS[ch - 1]} Layer")
        for s in range(1, (60 if ch == 5 else 6) + 1):
            sections.append(f"{ch}.{s} Subsection {ch} Point {s} Requirements")
            if ch == 5 and s % 3 == 0:
                for t in (1, 2):
                    sections.append(f"{ch}.{s}.{t} Detail Clause {ch} {s} {t} Limits")
    figures = [f"Figure 5-{i}. Electrical Figure Number {i} Setup" for i in range(1, 41)]
    doc = _Doc().h("Contents")
    for i, title in enumerate(sections):
        doc.p(_toc_line(title, 100 + i * 3))
    doc.h("List of Figures")
    for i, figure in enumerate(figures):
        doc.p(f"{figure} {'.' * 20} {300 + i * 4}")
    figure_iter = iter(figures)
    for title in sections:
        doc.h(title).p(_BODY)
        number = title.split()[0]
        if number.startswith("5.") and number.count(".") == 1 and int(number.split(".")[1]) <= 40:
            doc.h(next(figure_iter)).p("The figure shows the setup.")
    return doc


@pytest.mark.parametrize("section", ["Contents", "List of Figures"])
def test_big_specification_toc_and_single_chapter_figure_list_are_flagged(section):
    """Every chunk of the TOC (one chapter of 60 sections) and of the list of
    figures (all in chapter 5) that reaches the reference floor is flagged;
    a chunk OUTSIDE the lists (here: none) never is."""
    chunks, result = _big_spec().detect()
    candidates = [
        (m, flag) for c, m, flag in zip(chunks, result.metrics, result.flags)
        if c["section_path"] == section and m.ordered_refs >= nav.NAV_MIN_REFERENCES
    ]
    assert len(candidates) >= 4
    assert all(flag for _m, flag in candidates)
    assert set(_flagged_sections(chunks, result)) == {"Contents", "List of Figures"}


_CJK_SHORT = ["1 概述说明", "1.1 封装选项", "1.2 性能目标", "2 协议层", "2.1 帧格式", "2.2 重传机制",
              "3 适配层", "3.1 链路初始化", "5.7 通道特性", "5.7.1 电压传递", "5.7.2 先进封装",
              "5.7.2.1 损耗与串扰", "5.7.3 标准封装", "5.7.3.1 插入损耗"]


def test_cjk_toc_of_short_titles_with_three_digit_pages_is_flagged():
    """Short CJK titles against their numbers and three-digit pages: by length
    the stream is mostly digits (coverage below one half), but nearly every
    character that is not a digit is a title -- which is what letters
    coverage measures."""
    doc = _Doc().h("目录")
    for i, title in enumerate(_CJK_SHORT):
        doc.p(f"{title} {'…' * 12} {100 + i * 37}")
    for title in _CJK_SHORT:
        doc.h(title).p("发送端应在规定摆幅范围内驱动通道，接收端应容忍规定的共模噪声。" * 4)
    chunks, result = doc.detect()
    toc = _metrics_for(chunks, result, "目录")[0]
    assert toc.coverage < 0.5
    assert toc.letters_coverage >= 0.86
    assert _flagged_sections(chunks, result) == ["目录"]


def test_cjk_list_of_figures_is_flagged():
    figures = [f"图 {c}-{i} {name}" for c, i, name in (
        (2, 1, "协议层帧格式示意"), (2, 2, "重传缓冲状态转移"), (3, 1, "链路初始化时序"),
        (4, 1, "通道反转映射示例"), (5, 1, "电压传递函数测量装置"), (5, 2, "眼图模板定义"),
        (5, 3, "串扰干扰源排布"), (6, 1, "边带报文时序"))]
    doc = _Doc().h("插图目录")
    for i, figure in enumerate(figures):
        doc.p(f"{figure}{'…' * 10}{20 + i * 9}")
    for figure in figures:
        doc.h(f"{figure.split()[1][0]} 第{figure.split()[1][0]}章正文内容说明").p("本章内容。" * 10)
        doc.h(figure).p("该图展示了测量装置。")
    chunks, result = doc.detect()
    assert _flagged_sections(chunks, result) == ["插图目录"]


def test_list_of_tables_with_short_captions_is_flagged():
    """Lower bound of letters coverage: captions whose stripped titles are
    barely long enough to take part, so the uncovered word ``Table`` weighs
    almost a third of each entry's letters."""
    captions = ["Skew Limits", "Swing Range", "Noise Floor", "Jitter Caps", "Loss Budget",
                "Return Loss", "Clock Drift", "Lane Delays"]
    tables = [f"Table 5-{i}. {caption}" for i, caption in enumerate(captions, start=1)]
    doc = _Doc().h("List of Tables")
    for i, table in enumerate(tables):
        doc.p(f"{table} {'.' * 20} {40 + i * 3}")
    doc.h("5 Electrical Layer").p(_BODY)
    for table in tables:
        doc.h(table).p("| Parameter | Value |")
    chunks, result = doc.detect()
    lot = _candidates(chunks, result, "List of Tables")
    assert lot and nav.NAV_MIN_LETTER_COVERAGE <= lot[0].letters_coverage < 0.75
    assert _flagged_sections(chunks, result) == ["List of Tables"]


def test_near_ordered_toc_is_flagged():
    """Order-ratio tolerance: a 20-entry TOC with two entries swapped."""
    titles = [f"5.{i + 1} {a} {b}" for i, (a, b) in enumerate(
        (a, b) for a in _ADJECTIVES[:4] for b in _NOUNS[:5])]
    order = list(range(20))
    order[3], order[4] = order[4], order[3]
    order[12], order[14] = order[14], order[12]
    doc = _Doc().h("Contents")
    for i in order:
        doc.p(f"{titles[i]} {'.' * 16} {20 + i * 3}")
    for title in titles:
        doc.h(title).p(_BODY)
    chunks, result = doc.detect()
    toc = _candidates(chunks, result, "Contents")
    assert toc and min(m.ratio for m in toc) < 1.0
    assert set(_flagged_sections(chunks, result)) == {"Contents"}
    assert len(_flagged_sections(chunks, result)) == len(toc)


def test_toc_listing_three_of_four_levels_is_flagged():
    """Density counts only the levels the chain itself reaches: a TOC that
    lists levels 1-3 of a four-level document is dense, not sparse."""
    sections = _dense_sections(deep=True)
    doc = _Doc().h("Contents")
    for i, (title, depth) in enumerate(sections):
        if depth <= 3:
            doc.p(_toc_line(title, 10 + i))
    chunks, result = _with_body(doc, sections).detect()
    toc = _candidates(chunks, result, "Contents")
    assert len(toc) >= 10 and all(m.density == 1.0 for m in toc)
    flagged = [flag for c, m, flag in zip(chunks, result.metrics, result.flags)
               if c["section_path"] == "Contents" and m.ordered_refs >= nav.NAV_MIN_REFERENCES]
    assert all(flagged)
    assert _flagged_sections(chunks, result) == ["Contents"] * len(flagged)


# --------------------------------------------- residual risk, recorded as such
def test_residual_risk_cross_chapter_register_summaries_are_flagged():
    """Recorded, not wanted: an appendix register summary that lists whole
    register subtrees of other chapters (9.5.x and 10.3.x), and one that lists
    two whole chapters, are navigation by every structural measure -- many
    references, all in order, mostly titles, outside the summary's own
    section, and dense because each run is a complete subtree. No structural
    condition tells them from a list of registers that IS navigation, so they
    are flagged; the read-side switch is the remedy if that ever matters."""
    doc = _Doc()
    registers: list[str] = []
    for c in (8, 9, 10):
        doc.h(f"{c} Chapter {_WORDS[c - 1]} Main Topic").p(_BODY)
        for s in range(1, 7):
            doc.h(f"{c}.{s} Section {_WORDS[c - 1]} {_WORDS[s - 1]} Behaviour Rules").p(_BODY)
            if (c, s) in ((9, 5), (10, 3)):
                for r in range(1, 7):
                    title = f"{c}.{s}.{r} {_WORDS[c - 1]} {_WORDS[s - 1]} Register {_WORDS[r - 1]} Layout"
                    registers.append(title)
                    doc.h(title).p(_BODY)
    doc.h("B Register Summary").p("| Offset | Register |")
    for i, title in enumerate(registers):
        doc.p(f"| 0x{i * 4:02X}h | {title.partition(' ')[2]} |")
    chunks, result = doc.detect()
    assert _flagged_sections(chunks, result) == ["B Register Summary"]

    doc = _Doc()
    whole: list[str] = []
    for c in (9, 10):
        doc.h(f"{c} Chapter {_WORDS[c - 1]} Registers").p(_BODY)
        for r in range(1, 7):
            title = f"{c}.{r} {_WORDS[c - 1]} Register {_WORDS[r - 1]} Layout"
            whole.append(title)
            doc.h(title).p(_BODY)
    doc.h("B Register Summary").p("| Offset | Register |")
    for i, title in enumerate(whole):
        doc.p(f"| 0x{i * 4:02X}h | {title.partition(' ')[2]} |")
    chunks, result = doc.detect()
    assert _flagged_sections(chunks, result) == ["B Register Summary"]


def test_prose_that_walks_every_section_in_order_is_not_flagged():
    """A change summary that names every section of a chapter run in order,
    each in a sentence: in order, outside its own section and dense -- but
    most of its text is not titles (letters coverage)."""
    sections = _dense_sections()
    doc = _Doc().h("Summary of Changes")
    for title, depth in sections:
        if depth == 2:
            doc.p(f"{title.partition(' ')[2]}: timing limits were relaxed for low power parts.")
    chunks, result = _with_body(doc, sections).detect()
    summary = _candidates(chunks, result, "Summary of Changes")
    assert summary
    for metrics in summary:
        assert metrics.ratio == 1.0 and metrics.locality == 0.0
        assert metrics.density >= nav.NAV_MIN_DENSITY
        assert metrics.letters_coverage < nav.NAV_MIN_LETTER_COVERAGE
    assert _flagged_sections(chunks, result) == []


# ============================================================ review follow-ups
_SHARED = ["Electrical Characteristics", "Register Description Table", "Functional Description"]


def _shared_title_sections() -> list[str]:
    """Ten chapters whose sections .2/.4/.6 carry the same three titles."""
    sections = []
    for c in range(1, 11):
        sections.append(f"{c} Peripheral {_WORDS[c - 1]} Main Topic")
        for s in range(1, 7):
            if s in (2, 4, 6):
                sections.append(f"{c}.{s} {_SHARED[s // 2 - 1]}")
            else:
                sections.append(f"{c}.{s} Section {_WORDS[c - 1]} {_WORDS[s - 1]} Behaviour Rules")
    return sections


@pytest.mark.parametrize("target_chars", [400, 500, 800, 1200])
def test_toc_with_titles_shared_by_every_chapter_is_flagged_at_any_chunk_size(target_chars):
    """A TOC chunk that starts inside chapter 7 with a title every chapter
    shares must not have its chain start at chapter 1's copy of that title:
    the chain is re-pinned to the latest heading that keeps it in order, so
    its density reads the stretch the chunk really lists."""
    sections = _shared_title_sections()
    doc = _Doc().h("Contents")
    for i, title in enumerate(sections):
        doc.p(_toc_line(title, 100 + i))
    for title in sections:
        doc.h(title).p(_BODY)
    chunks, result = doc.detect(target_chars=target_chars)
    toc = [(m, flag) for c, m, flag in zip(chunks, result.metrics, result.flags)
           if c["section_path"] == "Contents" and m.ordered_refs >= nav.NAV_MIN_REFERENCES]
    assert len(toc) >= 3
    assert all(m.density == 1.0 for m, _flag in toc)
    assert all(flag for _m, flag in toc)


_OVERVIEW_SUBSECTIONS = ["Electrical Layer Overview", "Transmitter Equalization Settings",
                         "Receiver Sensitivity Limits", "Clocking Architecture Details",
                         "Lane Reversal Support", "Channel Characteristics",
                         "Sideband Signaling Interface"]


@pytest.mark.parametrize(("chapter_heading", "number"), [
    ("Chapter 5 Electrical Layer Specification", "5"),
    ("第5章 电气层规范说明", "5"),
    ("Appendix A Electrical Layer Specification", "A"),
])
def test_chapter_overview_under_a_worded_chapter_heading_is_not_flagged(chapter_heading, number):
    """The chunk's own section is found through its label and read with the
    same parsing as every heading: ``Chapter 5`` / ``第5章`` / ``Appendix A``
    are chapters 5 / 5 / A, so the overview's chain (5.x / A.x) is local."""
    doc = _Doc().h(chapter_heading)
    doc.p("This chapter covers " + ", ".join(_OVERVIEW_SUBSECTIONS) + ".")
    for i, title in enumerate(_OVERVIEW_SUBSECTIONS, start=1):
        doc.h(f"{number}.{i} {title}").p(_BODY)
    chunks, result = doc.detect()
    overview = _candidates(chunks, result, chapter_heading)
    assert overview and overview[0].locality == 1.0
    assert _flagged_sections(chunks, result) == []


@pytest.mark.parametrize("review_heading", ["Chapter 6 Review Of Chapter Five", "6 Review Of Chapter Five"])
def test_a_list_of_another_chapters_sections_is_flagged(review_heading):
    """The other side of locality: a chunk of chapter 6 (worded or a
    single-segment number, which takes no parent) that lists chapter 5's
    sections in order is outside what it lists."""
    doc = _Doc()
    for i, title in enumerate(_OVERVIEW_SUBSECTIONS, start=1):
        doc.h(f"5.{i} {title}").p(_BODY)
    doc.h(review_heading)
    for i, title in enumerate(_OVERVIEW_SUBSECTIONS, start=1):
        doc.p(f"5.{i} {title} {'.' * 12} {40 + i}")
    chunks, result = doc.detect()
    review = _candidates(chunks, result, review_heading)
    assert review and review[0].locality == 0.0
    assert _flagged_sections(chunks, result) == [review_heading]


def test_register_map_with_unnumbered_breadcrumb_children_is_not_flagged():
    """A numbered section (``9.5 Register Map``) whose children are
    unnumbered breadcrumb sections: the breadcrumb reading counts as well as
    the numbering, so the register table of its own children is local."""
    doc = _Doc()
    for ch in range(1, 9):
        title = f"{ch} Chapter Heading Topic {ch}"
        doc.h(title, title).p(_BODY)
    doc.h("9 Configuration Registers", "9 Configuration Registers").p(_BODY)
    parent = "9 Configuration Registers > 9.5 Register Map"
    doc.h("9.5 Register Map", parent).p("| Offset | Register |")
    for i, register in enumerate(_REGISTERS[:9]):
        doc.p(f"| 0x{i * 4:02X}h | {register} |")
    for register in _REGISTERS[:9]:
        doc.h(register, f"{parent} > {register}").p(_BODY)
    chunks, result = doc.detect()
    table = _candidates(chunks, result, parent)
    assert table and table[0].locality == 1.0
    assert _flagged_sections(chunks, result) == []


def test_list_of_figures_across_a_chapter_and_an_appendix_is_flagged():
    """``Figure A-1`` is a caption (a capital-letter enumerator, as in the
    appendix numbering): figures of chapter 7 and appendix A form one kind,
    so the list's density stays whole."""
    figures = ([f"Figure 7-{i}. Seven Figure {_WORDS[i - 1]} Setup Diagram" for i in range(1, 5)]
               + [f"Figure A-{i}. Appendix Figure {_WORDS[i - 1]} Setup Diagram" for i in range(1, 5)])
    doc = _Doc().h("List of Figures")
    for i, figure in enumerate(figures):
        doc.p(f"{figure} {'.' * 20} {300 + i * 4}")
    doc.h("7 Chapter Seven Main Topic").p(_BODY)
    for i, figure in enumerate(figures[:4]):
        doc.h(f"7.{i + 1} Section Seven {_WORDS[i]} Behaviour Rules").p(_BODY)
        for k in range(3):
            doc.h(f"Unnumbered Register {_WORDS[i]} {_WORDS[k]} Layout").p(_BODY)
        doc.h(figure).p("fig")
    doc.h("A Appendix Material Topic").p(_BODY)
    for i, figure in enumerate(figures[4:]):
        doc.h(f"A.{i + 1} Appendix Section {_WORDS[i]} Rules").p(_BODY)
        doc.h(figure).p("fig")
    chunks, result = doc.detect()
    figure_list = _candidates(chunks, result, "List of Figures")
    assert figure_list and figure_list[0].density == 1.0
    assert _flagged_sections(chunks, result) == ["List of Figures"]


def test_a_list_of_its_own_chapters_figures_is_not_flagged():
    """``Figure 5-x`` belongs to chapter 5: a section of chapter 5 that lists
    the chapter's figures is local (the caption's chapter is what says so)."""
    doc = _Doc().h("5 Electrical Layer").p(_BODY)
    figures = [f"Figure 5-{i}. Electrical Figure {_WORDS[i - 1]} Setup" for i in range(1, 9)]
    for i, figure in enumerate(figures, start=1):
        doc.h(f"5.{i} Section Five {_WORDS[i - 1]} Behaviour Rules").p(_BODY)
        doc.h(figure).p("The figure shows the setup.")
    doc.h("5.9 Figures In This Chapter")
    for i, figure in enumerate(figures):
        doc.p(f"{figure} {'.' * 12} {60 + i}")
    chunks, result = doc.detect()
    listing = _candidates(chunks, result, "5.9 Figures In This Chapter")
    assert listing and listing[0].locality == 1.0
    assert _flagged_sections(chunks, result) == []


@pytest.mark.parametrize(("title", "kind", "chapter"), [
    ("Figure 5-1. Voltage Transfer Setup", "cap:figure", "5"),
    ("Figure A-1. Compliance Fixture Setup", "cap:figure", "A"),
    ("Chapter 5 Electrical Layer", "cap:chapter", "5"),
    ("第5章 电气层规范", "cap:第", "5"),
    ("表3-2 参数汇总列表", "cap:表", "3"),
    ("Appendix A Compliance Testing", "cap:appendix", "A"),
    ("DDR4 Memory Controller Setup", "plain", ""),
    ("I2C Bus Timing Requirements", "plain", ""),
    ("UART1 Interface Configuration", "plain", ""),
    ("LPDDR5X Channel Training Flow", "plain", ""),
])
def test_caption_enumeration_needs_a_separated_enumerator(title, kind, chapter):
    [heading] = nav._headings([{"element_type": "heading", "text": title}])
    assert heading.kind[0] == kind
    assert heading.chapter == chapter


_REPEATED_SUBSECTIONS = ["Electrical Layer Overview", "Transmitter Equalization Settings",
                         "Receiver Sensitivity Limits", "Clocking Architecture Details",
                         "Lane Reversal Support", "Channel Characteristics",
                         "Sideband Signaling Interface"]


def _two_chapters_sharing_subsections() -> _Doc:
    """Chapters 1 and 5 carry the same seven subsection titles; each opens
    with the same overview paragraph; a TOC lists the whole document."""
    sections: list[str] = []
    for chapter in (1, 2, 3, 4, 5):
        sections.append(f"{chapter} Chapter {_WORDS[chapter - 1]} Specification")
        names = (_REPEATED_SUBSECTIONS if chapter in (1, 5)
                 else [f"Section {_WORDS[chapter - 1]} {_WORDS[s]} Rules" for s in range(7)])
        sections += [f"{chapter}.{i} {name}" for i, name in enumerate(names, start=1)]
    doc = _Doc().h("Contents")
    for i, title in enumerate(sections):
        doc.p(_toc_line(title, 10 + i * 3))
    overview = "This chapter covers " + ", ".join(_REPEATED_SUBSECTIONS) + "."
    for title in sections:
        doc.h(title)
        doc.p(overview if title.split()[0] in ("1", "5") else _BODY)
    return doc


def test_each_chapters_overview_is_local_when_chapters_share_subsection_titles():
    """codex #971 P2: the text cannot tell chapter 1's ``Lane Reversal
    Support`` from chapter 5's, and the chain is pinned by document order,
    so chapter 5's own overview used to read as a list of chapter 1's
    sections (locality 0, flagged) while the identical text under chapter 1
    was local. A node is local when any heading with its title is."""
    chunks, result = _two_chapters_sharing_subsections().detect()
    for heading in ("1 Chapter One Specification", "5 Chapter Five Specification"):
        overview = _candidates(chunks, result, heading)
        assert overview and overview[0].locality == 1.0, heading
    assert "1 Chapter One Specification" not in _flagged_sections(chunks, result)
    assert "5 Chapter Five Specification" not in _flagged_sections(chunks, result)


def test_toc_of_chapters_sharing_subsection_titles_is_still_flagged():
    chunks, result = _two_chapters_sharing_subsections().detect()
    toc = [(m, flag) for c, m, flag in zip(chunks, result.metrics, result.flags)
           if c["section_path"] == "Contents" and m.ordered_refs >= nav.NAV_MIN_REFERENCES]
    assert len(toc) >= 2 and all(m.locality == 0.0 for m, _flag in toc)
    assert all(flag for _m, flag in toc)
    assert set(_flagged_sections(chunks, result)) == {"Contents"}
