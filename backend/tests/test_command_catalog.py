"""Command-manual catalog extraction, the pure layer.

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
"""
from __future__ import annotations

import pytest

from app.services.command_catalog import (
    MAX_CANDIDATES,
    MAX_SECTION_REJECTIONS,
    MAX_SLICE_WINDOW_CHARS,
    SLICE_PARAM_LIMIT,
    CommandSection,
    SectionElement,
    SectionMeta,
    catalog_stats,
    command_candidates,
    command_sections,
    detect_manual_shape,
    extraction_slices,
    normalize_syntax,
    parameter_names,
    section_outcome,
    validate_entry,
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

# OpenROAD `gpl` — a second command, so grouping has a sibling to separate.
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
    return _elements(
        ("heading", FLOPS_PATH, "Cluster Flops"),
        ("paragraph", FLOPS_PATH, CLUSTER_FLOPS_INTRO),
        ("code_block", FLOPS_PATH, CLUSTER_FLOPS_SYNTAX),
        ("heading", FLOPS_OPTIONS_PATH, "Options"),
        ("table", FLOPS_OPTIONS_PATH, CLUSTER_FLOPS_OPTIONS),
    )


def _section(text, *, title="Cluster Flops", path=FLOPS_PATH, hint="cluster_flops"):
    """A hand-made section, for validation tests that need no grouping."""
    return CommandSection(
        title=title,
        section_path=path,
        shape="syntax_block",
        command_hint=hint,
        elements=(SectionElement(id="el-0001", element_type="paragraph", text=text),),
        text=text,
    )


@pytest.fixture
def flops_section():
    sections = command_sections(_flops_elements())
    assert len(sections) == 1
    return sections[0]


# ------------------------------------------------------------ 1. shape signal
def _manual_metas():
    """Seven real OpenROAD sections, as a caller would sample them."""
    return [
        SectionMeta("Global Placement", f"{GPL_INTRO}\n{GPL_SYNTAX}"),
        SectionMeta("Options", GPL_OPTIONS),
        SectionMeta(
            "Cluster Flops",
            f"{CLUSTER_FLOPS_INTRO}\n{CLUSTER_FLOPS_SYNTAX}",
        ),
        SectionMeta("Options", CLUSTER_FLOPS_OPTIONS),
        SectionMeta(
            "Set Don't Use",
            f"{SET_DONT_USE_INTRO}\n{SET_DONT_USE_SYNTAX}",
        ),
        SectionMeta(
            "Placement Clusters",
            "This command defines instances placed as a single cluster.\n"
            + PLACEMENT_CLUSTER_SYNTAX,
        ),
        SectionMeta(
            "Remove Fillers",
            "This command removes filler cells.\nremove_fillers",
        ),
        SectionMeta("License", "BSD 3-Clause License."),
    ]


# A paper's appendix numbering, which is what the numbering filter actually
# buys: bare `2.1` is already excluded upstream by the ASCII-letter rule, while
# `A.1.2` has a letter, a dot and five characters and so clears every other
# gate. Eight of them, all command-shaped without the filter, so the fixture
# demonstrates the product consequence — a survey classified as a manual — and
# not merely a counter moving.
PAPER_METAS = [
    SectionMeta("A.1.2 Notation",
                "Table A.1 lists the symbols used throughout the paper."),
    SectionMeta("B.2.1 Hyperparameters",
                "We train for three epochs with a small learning rate."),
    SectionMeta("C.3.4 Proofs",
                "The proof follows from the convexity of the objective."),
    SectionMeta("D.1.5 Datasets",
                "We evaluate on three public question answering datasets."),
    SectionMeta("E.2.1 Ablations",
                "Removing the reranker reduces recall by four points."),
    SectionMeta("F.4.2 Limitations",
                "Our approach assumes documents fit the context window."),
    SectionMeta("G.1.3 Broader Impact",
                "We discuss deployment considerations for practitioners."),
    SectionMeta("H.2.6 Reproducibility",
                "Code and configurations are released with the paper."),
]


def test_openroad_sections_read_as_a_command_manual():
    signal = detect_manual_shape(_manual_metas())

    # Five command-shaped sections out of eight — the two `Options`
    # sub-sections and the licence do not count, which is the point: the
    # signal estimates *commands*, not "pages that mention a flag".
    assert signal.command_shaped_sections == 5
    assert signal.syntax_sections == 5
    assert signal.is_manual is True
    assert signal.reason == "manual"


def test_numbered_paper_headings_are_not_read_as_command_names():
    """A survey's `A.1.2 Notation` is numbering, not a command.

    Every one of these headings clears the ASCII-letter, minimum-length and
    dotted-separator gates, so without the numbering filter all eight count as
    identifier-named and a paper is offered command extraction.
    """
    signal = detect_manual_shape(PAPER_METAS)

    assert signal.identifier_headings == 0
    assert signal.command_shaped_sections == 0
    assert signal.is_manual is False
    assert signal.reason == "too_few_command_sections"


def test_prose_source_without_commands_is_not_a_manual():
    metas = [
        SectionMeta(f"Chapter {index}",
                    "Placement algorithms trade wirelength against timing.")
        for index in range(1, 13)
    ]

    signal = detect_manual_shape(metas)

    assert signal.is_manual is False
    assert signal.command_shaped_sections == 0


def test_flag_density_is_reported_but_never_votes():
    """Flag-dense prose is evidence for a person, not a verdict.

    An `Options` table is flag-dense and is not a command; counting it would
    score one command twice and let any page of shell examples through.
    """
    metas = [
        SectionMeta(f"Tips {index}",
                    "Pass -verbose to see more.\nUse -jobs 8 for speed.\n"
                    "Add -color always when piping.")
        for index in range(1, 9)
    ]

    signal = detect_manual_shape(metas)

    assert signal.flag_sections == 8
    assert signal.flag_ratio == 1.0
    assert signal.command_shaped_sections == 0
    assert signal.is_manual is False


def test_empty_input_is_reported_rather_than_guessed():
    signal = detect_manual_shape([])

    assert signal.total_sections == 0
    assert signal.is_manual is False
    assert signal.reason == "no_sections"


# Dotted-only headings (no underscore) clear every gate a real underscore-
# joined command name clears — ASCII letter, minimum length, dotted
# separator — but real commands are never spelled this way. Eight repeats
# used to be enough to read as a manual on strength alone (P2-1).
FILENAME_METAS = [
    SectionMeta(name, f"See {name} for the full configuration reference.")
    for name in [
        "README.md",
        "config.yaml",
        "config.yaml",
        "settings.json",
        "docker-compose.yaml",
        "config.yaml",
        "package.json",
        "tsconfig.json",
    ]
]


def test_dotted_filename_headings_alone_do_not_read_as_a_manual():
    """`README.md` / `config.yaml` headings, repeated, used to score
    `is_manual=True` on strength alone — a dotted-only heading is *weak*
    identifier evidence, not the strong (underscore-joined) kind a real
    `### set_db` heading carries, and here there is no corroborating syntax
    line or parameter table anywhere in the source either."""
    signal = detect_manual_shape(FILENAME_METAS)

    assert signal.identifier_headings == 8
    assert signal.command_shaped_sections == 8
    assert signal.is_manual is False
    assert signal.reason == "weak_identifier_headings_only"


def test_strong_identifier_headings_still_read_as_a_manual():
    """Underscore-joined headings are strong evidence on their own,
    corroborating evidence or not — a real `### set_db`-shaped manual must
    not regress just because the weak (dotted-only) path got stricter."""
    metas = [
        SectionMeta(name, f"Documentation for {name}.")
        for name in [
            "set_db", "get_db", "add_to_db", "remove_from_db", "list_db_entries",
        ]
    ]

    signal = detect_manual_shape(metas)

    assert signal.identifier_headings == 5
    assert signal.is_manual is True
    assert signal.reason == "manual"


def test_section_meta_accepts_tuples_and_mappings():
    tuples = detect_manual_shape(
        [("Global Placement", f"{GPL_INTRO}\n{GPL_SYNTAX}")]
    )
    mappings = detect_manual_shape(
        # An extraneous `section_path` key (e.g. a raw chunk-store row) is
        # tolerated, not read — `detect_manual_shape` counts sections, it
        # never groups them.
        [{"section_path": GPL_PATH, "heading_text": "Global Placement",
          "text_sample": f"{GPL_INTRO}\n{GPL_SYNTAX}"}]
    )

    assert tuples.syntax_sections == 1
    assert mappings.syntax_sections == 1


# --------------------------------------------------------------- 2. grouping
def test_title_bodied_section_owns_its_options_subsection():
    """`### Global Placement` + `#### Options` is one command, not two."""
    sections = command_sections(_gpl_elements())

    assert len(sections) == 1
    section = sections[0]
    assert section.title == "Global Placement"
    assert section.command_hint == "global_placement"
    assert section.shape == "syntax_block"
    # The parameter table is inside the section it documents — the whole
    # reason this grouping exists.
    assert "-tray_weight" not in section.text
    assert "`-density`" in section.text
    assert len(section.element_ids) == 5


def test_sibling_command_opens_its_own_section():
    sections = command_sections(_gpl_elements() + _flops_elements())

    assert [section.command_hint for section in sections] == [
        "global_placement",
        "cluster_flops",
    ]
    assert "cluster_flops" not in sections[0].text
    assert "global_placement" not in sections[1].text


def test_front_matter_before_the_first_command_is_dropped():
    elements = _elements(
        ("heading", "Global Placement", "Global Placement"),
        ("paragraph", "Global Placement", "The gpl module performs placement."),
        ("heading", "Global Placement > Commands", "Commands"),
    ) + _gpl_elements()

    sections = command_sections(elements)

    assert len(sections) == 1
    assert "The gpl module performs placement." not in sections[0].text


def test_trailing_chapter_does_not_join_the_last_command():
    """`## License` is shallower than the command — it closes the section."""
    elements = _gpl_elements() + _elements(
        ("heading", "Global Placement > License", "License"),
        ("paragraph", "Global Placement > License", "BSD 3-Clause License."),
    )

    sections = command_sections(elements)

    assert len(sections) == 1
    assert "BSD 3-Clause" not in sections[0].text


def test_sibling_subsection_keeps_its_parameter_table():
    """A manual that nests unevenly still keeps the table with the command.

    Measured on OpenROAD `rsz`: `replace_arith_modules` is documented under an
    `####` heading whose `#### Options` table is a *sibling*, not a child. A
    descendant-only rule drops that table on the floor.
    """
    base = "Gate Resizer > Commands > Optimizing Arithmetic Modules"
    elements = _elements(
        ("heading", f"{base} > Requirements", "Requirements"),
        ("paragraph", f"{base} > Requirements", "Hierarchically linked design."),
        ("code_block", f"{base} > Requirements",
         "replace_arith_modules\n    [-path_count num_critical_paths]"),
        ("heading", f"{base} > Options", "Options"),
        ("table", f"{base} > Options",
         "| `-path_count` | Number of critical paths. Default is `1000`. |"),
    )

    sections = command_sections(elements)

    assert len(sections) == 1
    assert sections[0].command_hint == "replace_arith_modules"
    assert "-path_count" in sections[0].text


def test_cross_reference_prose_does_not_fork_a_section():
    """`#### SEE ALSO` holding a bare command name is not a command section.

    Prose is weak evidence and loses to a sibling; a code block would not.
    """
    base = "Gate Resizer > Commands > Optimizing Arithmetic Modules"
    elements = _elements(
        ("heading", f"{base} > Requirements", "Requirements"),
        ("code_block", f"{base} > Requirements", "replace_arith_modules"),
        ("heading", f"{base} > SEE ALSO", "SEE ALSO"),
        ("paragraph", f"{base} > SEE ALSO", "replace_hier_module"),
    )

    sections = command_sections(elements)

    assert len(sections) == 1
    assert sections[0].command_hint == "replace_arith_modules"


def test_one_line_command_with_a_positional_argument_is_found():
    """`set_dont_use lib_cells` — no flags, no bracket list, still a command.

    Omitting this shape is not a corner case: it lost five real commands in
    one OpenROAD module.
    """
    path = "Gate Resizer > Commands > Set Don't Use"
    elements = _elements(
        ("heading", path, "Set Don't Use"),
        ("paragraph", path, SET_DONT_USE_INTRO),
        ("code_block", path, SET_DONT_USE_SYNTAX),
    )

    sections = command_sections(elements)

    assert [section.command_hint for section in sections] == ["set_dont_use"]


def test_prose_sentence_opening_with_an_identifier_is_not_a_usage_line():
    path = "Guide > Notes"
    elements = _elements(
        ("heading", path, "Notes"),
        ("paragraph", path, "global_placement performs the placement step."),
        ("paragraph", path, "Fig.2 shows the resulting layout."),
    )

    assert command_sections(elements) == []


def test_flag_bearing_sentences_are_not_usage_lines():
    """B2's exact repro shape: a flag inside a sentence must not let the
    flag-carrying branch skip the same sentence-punctuation and snake_case
    guards the positional-argument branch already has to pass.

    Four failing shapes, two mechanisms: `Fig.2 shows the -density sweep.`
    and `config.yaml lists the -density defaults` are dotted, not snake_case
    (rejected regardless of punctuation — `config.yaml`'s sentence
    deliberately has no trailing period, so only that guard can catch it);
    `global_placement accepts -density values.` and its Chinese-punctuation
    twin are snake_case but end in a sentence (English and Chinese
    respectively — "(中英文句读)"). Eight sections' worth of any of these
    used to be enough to make a paper with no commands read as a manual.
    """
    path = "Guide > Notes"
    elements = _elements(
        ("heading", path, "Notes"),
        ("paragraph", path, "Fig.2 shows the -density sweep."),
        ("paragraph", path, "global_placement accepts -density values."),
        ("paragraph", path, "global_placement 会读取 -density 参数。"),
        ("paragraph", path, "config.yaml lists the -density defaults"),
    )

    assert command_sections(elements) == []


def test_flag_carrying_usage_line_is_still_recognised():
    """The real shape the flag branch exists for must survive the reorder."""
    path = "Global Placement > Commands > Global Placement"
    elements = _elements(
        ("heading", path, "Global Placement"),
        ("code_block", path, "get_global_placement_uniform_density -pad_left"),
    )

    sections = command_sections(elements)

    assert [section.command_hint for section in sections] == [
        "get_global_placement_uniform_density"
    ]


def test_element_stream_without_breadcrumbs_groups_by_heading_order():
    """The MinerU shape: headings, no section paths, no code_block type."""
    elements = _elements(
        ("heading", "", "Global Placement"),
        ("paragraph", "", GPL_INTRO),
        ("paragraph", "", "global_placement [-timing_driven] [-density target_density]"),
        ("heading", "", "Options"),
        ("paragraph", "", "-timing_driven | Enable timing-driven mode."),
        ("heading", "", "Cluster Flops"),
        ("paragraph", "", "cluster_flops [-tray_weight tray_weight]"),
    )

    sections = command_sections(elements)

    assert [section.command_hint for section in sections] == [
        "global_placement",
        "cluster_flops",
    ]
    # Without breadcrumbs the Options block cannot be tested for descent, so
    # the plain "fold into the previous command" rule applies.
    assert "Enable timing-driven mode" in sections[0].text


def test_examples_repeating_the_command_do_not_fork_a_section():
    elements = _elements(
        ("heading", "", "Global Placement"),
        ("code_block", "", GPL_SYNTAX),
        ("heading", "", "Examples"),
        ("code_block", "", "global_placement -density 0.6"),
    )

    sections = command_sections(elements)

    assert len(sections) == 1
    assert "0.6" in sections[0].text


def test_descendant_subsection_citing_another_command_does_not_fork_a_section():
    """A nested subsection whose code block cites a DIFFERENT command must
    lose to the descendant guard even though the evidence is code (`from_code`
    does not gate descent). The existing regression for this `_classify`
    branch (`test_examples_repeating_the_command_do_not_fork_a_section`) only
    covers the *same*-command case, caught by the earlier
    `usage == current.command_hint` check alone — this is the other half."""
    base = "Gate Resizer > Commands > Optimizing Arithmetic Modules > Requirements"
    elements = _elements(
        ("heading", base, "Requirements"),
        ("code_block", base,
         "replace_arith_modules\n    [-path_count num_critical_paths]"),
        ("heading", f"{base} > Notes", "Notes"),
        ("code_block", f"{base} > Notes", "report_timing -digits 3"),
    )

    sections = command_sections(elements)

    assert len(sections) == 1
    assert sections[0].command_hint == "replace_arith_modules"
    assert "report_timing" in sections[0].text


def test_usage_evidence_prefers_a_later_code_block_over_earlier_prose():
    """Measured on OpenROAD `rsz`: the section documenting
    `replace_arith_modules` opens with prose citing `link_design top -hier`
    (a cross-reference to a *different*, hierarchy-related command), and the
    code block naming the actual documented command comes after it in
    document order. Reading elements in document order would pick the wrong
    name; `_usage_evidence` runs a dedicated code-only pass first instead."""
    base = "Gate Resizer > Commands > Optimizing Arithmetic Modules > Requirements"
    elements = _elements(
        ("heading", base, "Requirements"),
        ("paragraph", base,
         "Hierarchically linked design. The design needs to be linked to "
         "preserve hierarchical boundaries. For example,"),
        ("paragraph", base, "link_design top -hier"),
        ("code_block", base,
         "replace_arith_modules\n    [-path_count num_critical_paths]"),
    )

    sections = command_sections(elements)

    assert len(sections) == 1
    assert sections[0].command_hint == "replace_arith_modules"


# OpenROAD `rsz`'s real `## Example scripts` appendix (P2-2): top-level, no
# sibling/descendant relationship to any command, and its first code line
# cites `read_sdc` — a command rsz's own manual never documents anywhere
# else. Trimmed to the shape the bug needs; text copied verbatim from rsz.md.
RSZ_ARITH_BASE = "Gate Resizer > Commands > Optimizing Arithmetic Modules"
RSZ_ARITH_REQUIREMENTS = f"{RSZ_ARITH_BASE} > Requirements for Arithmetic Module Swap"
RSZ_ARITH_SYNTAX = (
    "replace_arith_modules \n"
    "    [-path_count num_critical_paths]\n"
    "    [-slack_threshold float]\n"
    "    [-target setup | hold | power | area]"
)
RSZ_ARITH_OPTIONS = (
    "| Switch Name | Description |\n"
    "| ----------- | ---------- |\n"
    "| `-path_count` | Number of critical paths to analyze. Default `1000`. |\n"
    "| `-slack_threshold` | Slack threshold. Default `0.0`. |\n"
    "| `-target` | Optimization target. Default `setup`. |"
)
RSZ_EXAMPLE_SCRIPT = (
    "read_sdc gcd.sdc\n\n"
    "set_wire_rc -layer metal2\n\n"
    "set_dont_use {CLKBUF_* AOI211_X1 OAI211_X1}\n\n"
    "buffer_ports\n"
    "repair_design -max_wire_length 100\n"
    "repair_tie_fanout LOGIC0_X1/Z\n"
    "repair_tie_fanout LOGIC1_X1/Z\n"
    "#clock tree synthesis...\n"
    "repair_timing"
)


def test_rsz_example_scripts_section_does_not_open_a_read_sdc_command():
    """Real rsz shape: `## Example scripts` cites `read_sdc`, undocumented
    anywhere in rsz's own manual. Without the source-level fix this section
    opens its own `read_sdc` command and passes every grounding check —
    fixing it downstream in C1b could not tell it apart from a real one."""
    elements = _elements(
        ("heading", RSZ_ARITH_BASE, "Optimizing Arithmetic Modules"),
        ("paragraph", RSZ_ARITH_BASE,
         "The `replace_arith_modules` command optimizes design performance "
         "by intelligently swapping hierarchical arithmetic modules."),
        ("heading", RSZ_ARITH_REQUIREMENTS,
         "Requirements for Arithmetic Module Swap"),
        ("code_block", RSZ_ARITH_REQUIREMENTS, RSZ_ARITH_SYNTAX),
        ("heading", f"{RSZ_ARITH_BASE} > Options", "Options"),
        ("table", f"{RSZ_ARITH_BASE} > Options", RSZ_ARITH_OPTIONS),
        ("heading", f"{RSZ_ARITH_BASE} > SEE ALSO", "SEE ALSO"),
        ("paragraph", f"{RSZ_ARITH_BASE} > SEE ALSO", "replace_hier_module"),
        ("heading", "Gate Resizer > Example scripts", "Example scripts"),
        ("paragraph", "Gate Resizer > Example scripts",
         "A typical `resizer` command file (after a design and Liberty "
         "libraries have been read) is shown below."),
        ("code_block", "Gate Resizer > Example scripts", RSZ_EXAMPLE_SCRIPT),
    )

    sections = command_sections(elements)

    assert [section.command_hint for section in sections] == [
        "replace_arith_modules"
    ]
    # Folded, not dropped: the example's own text lands on the preceding
    # command's evidence rather than vanishing.
    assert "read_sdc gcd.sdc" in sections[0].text


# OpenROAD `cts`'s real `## Example scripts` appendix: re-invokes
# `clock_tree_synthesis`, already documented above it, purely as a usage
# illustration. Without the fix this opens a *duplicate*
# `clock_tree_synthesis` section.
CTS_PATH = "Clock Tree Synthesis > Commands > Clock Tree Synthesis"
CTS_SYNTAX = (
    "clock_tree_synthesis\n"
    "    [-root_buf root_buf]\n"
    "    [-buf_list buf_list]\n"
    "    [-wire_unit wire_unit]"
)
RESET_CTS_PATH = "Clock Tree Synthesis > Commands > Reset CTS configuration"
RESET_CTS_INTRO = (
    "This command is used to reset the configuration of CTS. The flags "
    "determine which configurations will be reset to their default values."
)
RESET_CTS_SYNTAX = (
    "reset_cts_config\n    [-apply_ndr]\n    [-root_buf]\n    [-wire_unit]"
)
CTS_EXAMPLE_SCRIPT = (
    'clock_tree_synthesis -root_buf "BUF_X4" \\\n'
    '                     -buf_list "BUF_X4" \\\n'
    '                     -wire_unit 20\n'
    'report_cts "file.txt"'
)


def test_cts_example_scripts_section_does_not_duplicate_an_earlier_command():
    elements = _elements(
        ("heading", CTS_PATH, "Clock Tree Synthesis"),
        ("code_block", CTS_PATH, CTS_SYNTAX),
        ("heading", RESET_CTS_PATH, "Reset CTS configuration"),
        ("paragraph", RESET_CTS_PATH, RESET_CTS_INTRO),
        ("code_block", RESET_CTS_PATH, RESET_CTS_SYNTAX),
        ("heading", "Clock Tree Synthesis > Useful Developer Commands",
         "Useful Developer Commands"),
        ("table", "Clock Tree Synthesis > Useful Developer Commands",
         "| Command Name | Description |\n| ----- | ----- |\n"
         "| `clock_tree_synthesis_debug` | Option to plot the CTS to GUI. |"),
        ("heading", "Clock Tree Synthesis > Example scripts",
         "Example scripts"),
        ("code_block", "Clock Tree Synthesis > Example scripts",
         CTS_EXAMPLE_SCRIPT),
    )

    sections = command_sections(elements)

    assert [section.command_hint for section in sections] == [
        "clock_tree_synthesis",
        "reset_cts_config",
    ]


def test_section_text_is_bounded_and_the_loss_is_accounted():
    elements = _elements(
        ("heading", GPL_PATH, "Global Placement"),
        ("code_block", GPL_PATH, GPL_SYNTAX),
        ("paragraph", GPL_PATH, "x" * 5_000),
        ("paragraph", GPL_PATH, "y" * 5_000),
    )

    # Heading plus syntax block fit; the two 5000-char paragraphs do not.
    sections = command_sections(elements, max_chars=2_000)
    section = sections[0]

    assert len(section.text) <= 2_000
    assert section.truncation.applied is True
    assert section.truncation.dropped_elements == 2
    assert section.truncation.original_chars > 10_000
    # Elements and text describe the same content: a grounding check that
    # searched text the model never saw would pass hallucinations.
    assert len(section.elements) == len(section.element_ids) == 2
    assert section.text == "\n".join(item.text for item in section.elements)
    assert section.truncation.kept_chars == len(section.text)


def test_single_oversized_element_is_clipped_rather_than_dropped():
    """"No text at all" is a worse answer than "the first N characters"."""
    sections = command_sections(
        _elements(("heading", "", "set_db options"), ("paragraph", "", "q" * 500)),
        max_chars=10,
    )

    assert sections and sections[0].command_hint == "set_db"
    assert sections[0].text == "set_db opt"
    assert sections[0].truncation.applied is True
    assert sections[0].truncation.dropped_elements == 1


def test_oversized_table_element_is_truncated_in_bounds_not_dropped():
    """B1's exact repro: heading + a single over-budget table element used to
    make the WHOLE table vanish, not just its tail — `not kept` only ever
    fires on the very first element, and the heading is always that first
    element, so the table (the first *real* content) took the whole-block
    drop instead of being clipped. Measured: a 120-parameter section came out
    as 36 characters of heading text, 1/120 args kept, and a command-reject
    ratio of 0.0 that never saw the failure because there was nothing left to
    see it in."""
    rows = "\n".join(
        f"| `-param_{index:03d}` | Description for parameter number "
        f"{index:03d}, controlling one aspect of the big command's "
        f"behavior. Default is `{index}`. |"
        for index in range(120)
    )
    elements = _elements(
        ("heading", "", "big_command options"),
        ("table", "", rows),
    )

    sections = command_sections(elements, max_chars=12_000)

    assert len(sections) == 1
    section = sections[0]
    assert section.truncation.applied is True
    # The table's own prefix survives — not just the heading.
    assert "-param_000" in section.text
    assert len(section.text) <= 12_000
    # Truncation must not starve the circuit breaker of its own evidence:
    # enough parameters survive that slicing still produces multiple slices.
    slices = extraction_slices(section)
    assert len(slices) >= 2


def test_truncation_accounting_survives_the_full_extraction_chain():
    """e2e, real markdown all the way through: parse_markdown_text ->
    command_sections -> extraction_slices -> validate_entry ->
    section_outcome. B1's fix has to hold at every layer, not just inside
    `_pack` in isolation — an answer covering all 120 parameters must ground
    exactly as many as actually survived truncation, not zero and not all
    120."""
    rows = "\n".join(
        f"| `-param_{index:03d}` | Description for parameter number "
        f"{index:03d}, controlling one aspect of the big command's tuning "
        f"behavior in fine detail across many designs. Default is "
        f"`{index}`. |"
        for index in range(120)
    )
    markdown = (
        "## big_command options\n\n"
        "| Switch Name | Description |\n"
        "| ----- | ----- |\n"
        f"{rows}\n"
    )
    raw_elements = parse_markdown_text("src-e2e", markdown)
    elements = [
        {
            "id": element.id,
            "element_type": element.element_type,
            "text": element.text,
            "section_path": element.metadata.get("section_path", ""),
        }
        for element in raw_elements
    ]

    sections = command_sections(elements)
    assert len(sections) == 1
    section = sections[0]
    assert section.truncation.applied is True

    surviving_names = parameter_names(section.text)
    assert SLICE_PARAM_LIMIT < len(surviving_names) < 120

    slices = extraction_slices(section)
    assert len(slices) >= 2

    # A model answering for all 120 parameters the *original* table held —
    # only the ones whose lines actually survived truncation can ground.
    entry = {
        "command_name": "big_command",
        "args": [{"name": f"-param_{index:03d}"} for index in range(120)],
    }
    result = validate_entry(entry, section, ["big_command"])
    outcome = section_outcome(section, ["big_command"], [result])

    assert outcome.args_seen == 120
    assert outcome.args_kept == len(surviving_names)
    assert 0 < outcome.args_kept < 120


def test_empty_stream_yields_nothing():
    assert command_sections([]) == []


# ------------------------------------------------------------- 3. candidates
def test_candidates_lead_with_the_documented_command(flops_section):
    candidates = command_candidates(flops_section)

    assert candidates[0] == "cluster_flops"


def test_flag_names_never_become_candidates(flops_section):
    """A syntax block's flags would crowd the real name off a 16-slot list."""
    candidates = command_candidates(flops_section)

    assert "tray_weight" not in candidates
    assert "-tray_weight" not in candidates
    assert "max_split_size" not in candidates


def test_inline_code_identifiers_are_offered():
    section = command_sections(_gpl_elements())[0]

    candidates = command_candidates(section)

    assert "global_placement" in candidates
    # Named only in the prose, via inline code.
    assert "repair_design" in candidates
    assert "set_wire_rc" in candidates


def test_candidate_list_is_capped():
    text = "\n".join(f"command_{index} -flag" for index in range(40))
    section = _section(text, hint="command_0")

    assert len(command_candidates(section)) == MAX_CANDIDATES
    assert len(command_candidates(section, limit=3)) == 3


# ---------------------------------------------------------------- 4. slicing
def test_small_command_is_one_slice_carrying_the_overview(flops_section):
    slices = extraction_slices(flops_section)

    assert len(slices) == 1
    assert slices[0].include_overview is True
    assert slices[0].param_names == (
        "-tray_weight",
        "-timing_weight",
        "-max_split_size",
        "-num_paths",
        "-clock_power_weight",
    )


def test_command_without_parameters_still_gets_a_slice():
    section = _section(PLACEMENT_CLUSTER_SYNTAX, hint="placement_cluster")

    slices = extraction_slices(section)

    assert len(slices) == 1
    assert slices[0].param_names == ()
    assert slices[0].include_overview is True


def test_hundred_parameter_section_splits_into_at_least_five_slices():
    """C0 measured a ~100-parameter section hitting `finish_reason=length`
    and then returning empty content on retry — silent loss, not an error."""
    body = "big_command\n" + "\n".join(
        f"    [-param_{index:02d} value_{index:02d}]" for index in range(100)
    )
    section = _section(body, hint="big_command")

    slices = extraction_slices(section)

    assert len(slices) >= 5
    assert all(len(item.param_names) <= SLICE_PARAM_LIMIT for item in slices)
    # Only the first slice carries syntax/description/examples, so they are
    # asked for exactly once.
    assert [item.include_overview for item in slices] == [True] + [False] * (
        len(slices) - 1
    )
    assert all(item.total == len(slices) for item in slices)


def test_slice_parameter_subsets_partition_the_parameter_list():
    body = "big_command\n" + "\n".join(
        f"    [-param_{index:02d} value_{index:02d}]" for index in range(100)
    )
    section = _section(body, hint="big_command")

    slices = extraction_slices(section)
    flattened = [name for item in slices for name in item.param_names]

    assert flattened == parameter_names(section.text)
    assert len(flattened) == len(set(flattened)) == 100


def test_real_large_section_engages_slicing():
    section = command_sections(_gpl_elements())[0]

    slices = extraction_slices(section)

    assert len(slices) >= 2
    assert "-density" in slices[0].param_names


def test_text_window_narrows_each_slice_far_below_the_full_section():
    """C1b's per-slice prompt window: each slice's own view of the source
    should be far smaller than the whole section, and no two slices' windows
    should collapse to the same thing — a later slice's window must reflect
    ITS OWN parameters, not just repeat slice 0's."""
    body = "big_command\n" + "\n".join(
        f"    [-param_{index:02d} value_{index:02d}]" for index in range(100)
    )
    section = _section(body, hint="big_command")

    slices = extraction_slices(section)
    windows = [item.text_window for item in slices]

    assert len(windows) >= 5
    assert len(set(windows)) == len(windows)
    assert all(window for window in windows)
    assert all(len(window) < len(section.text) for window in windows)
    assert all(len(window) <= MAX_SLICE_WINDOW_CHARS for window in windows)
    # Each slice's window carries its OWN parameters, not another slice's.
    for item in slices:
        for name in item.param_names:
            assert name in item.text_window


def test_first_slice_window_carries_the_overview_and_syntax_block(flops_section):
    slices = extraction_slices(flops_section)

    assert len(slices) == 1
    assert slices[0].text_window
    assert "cluster_flops" in slices[0].text_window


def test_command_without_parameters_still_gets_a_nonempty_window():
    section = _section(PLACEMENT_CLUSTER_SYNTAX, hint="placement_cluster")

    slices = extraction_slices(section)

    assert slices[0].text_window


def test_parameter_names_keep_their_dash_and_skip_prose():
    text = (
        "Use -timing_driven for state-of-the-art results.\n"
        "- A markdown bullet is not a flag.\n"
        "The non-virtual repair runs at -0.5 slack.\n"
        "[-density target_density]"
    )

    assert parameter_names(text) == ["-timing_driven", "-density"]


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


def test_every_real_parameter_survives_validation(flops_section):
    """Zero false kills on a genuine entry — the precondition for the veto
    being worth having at all."""
    candidates = command_candidates(flops_section)

    result = validate_entry(_flops_entry(), flops_section, candidates)

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


def test_invented_parameter_is_rejected_with_a_complete_record(flops_section):
    entry = _flops_entry()
    entry["args"] = entry["args"] + [
        {"name": "-tray_capacity", "desc": "Invented.", "default": ""}
    ]

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

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
    flops_section,
):
    """C0's measured infidelity: the prompt asks for `-tray_weight`, the model
    answers `tray_weight`, and the manual's own prose contains the bare word —
    so plain containment restores the dash the model dropped."""
    entry = _flops_entry(
        args=[{"name": "tray_weight", "desc": "Tray weight.", "default": ""}]
    )

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.entry.args == ()
    assert [(item.value, item.reason) for item in result.rejections] == [
        ("tray_weight", "dash_stripped")
    ]


def test_positional_argument_without_a_dash_is_kept():
    """The dash test is evidence-based, not shape-based: a command that really
    does take a positional argument must not be gutted."""
    section = _section(SET_DONT_USE_SYNTAX, title="Set Don't Use",
                       hint="set_dont_use")
    entry = {
        "command_name": "set_dont_use",
        "args": [{"name": "lib_cells", "desc": "Cells to exclude."}],
    }

    result = validate_entry(entry, section, ["set_dont_use"])

    assert [arg.name for arg in result.entry.args] == ["lib_cells"]
    assert result.rejections == ()


def test_the_attribution_exemption_is_scoped_to_an_empty_assignment(flops_section):
    """R4 P1's reverse guard, pinned in one place because the two halves are
    one rule: an EMPTY assignment exempts a returned name from attribution (a
    flagless command's positional argument was never on any list, and C1b's
    prompt now asks for it outright), while a NON-EMPTY assignment must keep
    rejecting a name that belongs to another slice.

    Both directions in one test on purpose: the fix for the first is a
    one-token change away from gutting the second (`if not reason and
    assignment and ...` -> `if not reason and ...`), and a test that only
    proved the exemption would go green on that mutation.
    """
    flagless = _section(SET_DONT_USE_SYNTAX, title="Set Don't Use",
                        hint="set_dont_use")
    positional = {
        "command_name": "set_dont_use",
        "args": [{"name": "lib_cells", "desc": "Cells to exclude."}],
    }

    exempt = validate_entry(positional, flagless, ["set_dont_use"], assigned=())

    assert [arg.name for arg in exempt.entry.args] == ["lib_cells"]
    assert exempt.rejections == ()

    # Same section text, same grounding — only the assignment differs. Every
    # name below is verbatim in `flops_section`, so nothing but attribution can
    # reject them.
    off_assignment = _flops_entry(
        args=[
            {"name": "-num_paths", "desc": "Another slice's.", "default": ""},
            {"name": "-tray_weight", "desc": "This slice's.", "default": ""},
        ]
    )

    constrained = validate_entry(
        off_assignment,
        flops_section,
        command_candidates(flops_section),
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
    section = _section("set_db -name value sets a database property.",
                       title="set_db", hint="set_db")

    result = validate_entry({"command_name": "set_d"}, section,
                            ["set_d", "set_db"])

    assert result.accepted is False
    assert result.entry is None
    assert [(item.field, item.reason) for item in result.rejections] == [
        ("command_name", "not_in_text")
    ]
    assert result.stats.command_rejected is True


def test_command_name_outside_the_candidate_list_vetoes_the_entry(flops_section):
    result = validate_entry(
        _flops_entry(command_name="detailed_placement"),
        flops_section,
        command_candidates(flops_section),
    )

    assert result.accepted is False
    assert result.rejections[0].reason == "not_in_candidates"
    # The veto is whole-entry: nothing from an entry about the wrong command
    # is trustworthy.
    assert result.entry is None
    assert result.stats.args_seen == 0


def test_missing_command_name_vetoes_the_entry(flops_section):
    result = validate_entry({"syntax": "cluster_flops"}, flops_section,
                            command_candidates(flops_section))

    assert result.accepted is False
    assert result.rejections[0].reason == "empty"


def test_line_wrapped_syntax_normalises_and_passes(flops_section):
    """The manual wraps with backslashes; the model answers on one line."""
    flat = (
        "cluster_flops [-tray_weight tray_weight] [-timing_weight timing_weight] "
        "[-max_split_size max_split_size] [-num_paths num_paths] "
        "[-clock_power_weight clock_power_weight]"
    )

    result = validate_entry(
        _flops_entry(syntax=flat), flops_section, command_candidates(flops_section)
    )

    assert result.stats.syntax_kept is True
    assert result.entry.syntax == flat
    assert not [item for item in result.rejections if item.field == "syntax"]


def test_syntax_that_is_not_in_the_section_is_cleared_but_not_fatal(
    flops_section,
):
    result = validate_entry(
        _flops_entry(syntax="cluster_flops [-tray_capacity capacity]"),
        flops_section,
        command_candidates(flops_section),
    )

    assert result.accepted is True
    assert result.entry.syntax == ""
    assert result.stats.syntax_seen is True
    assert result.stats.syntax_kept is False
    assert [item.reason for item in result.rejections if item.field == "syntax"] == [
        "not_contiguous"
    ]


def test_ungrounded_default_is_cleared_while_the_parameter_survives(
    flops_section,
):
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "default": "64.0"}]
    )

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-tray_weight", "")
    ]
    assert [item.field for item in result.rejections] == ["default"]
    assert result.stats.defaults_seen == 1
    assert result.stats.defaults_kept == 0


def test_default_digit_is_rejected_though_the_longer_number_contains_it(
    flops_section,
):
    """B3: the manual states "default value is 500"; a hallucinated "5"
    must not ground just because it is a bare substring of 500 — the bug a
    plain `in` check had."""
    entry = _flops_entry(
        args=[{"name": "-max_split_size", "desc": "Split.", "default": "5"}]
    )

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-max_split_size", "")
    ]
    assert [item.field for item in result.rejections] == ["default"]


def test_default_matching_the_full_number_still_grounds(flops_section):
    entry = _flops_entry(
        args=[{"name": "-max_split_size", "desc": "Split.", "default": "500"}]
    )

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-max_split_size", "500")
    ]
    assert result.rejections == ()


def test_decimal_default_grounds_at_its_own_token_boundaries():
    """"0.5" must ground when the section text genuinely says `0.5`."""
    section = _section(
        "cluster_flops [-density density]\n"
        "`-density` default value is `0.5`.",
        hint="cluster_flops",
    )
    entry = {
        "command_name": "cluster_flops",
        "args": [{"name": "-density", "default": "0.5"}],
    }

    result = validate_entry(entry, section, ["cluster_flops"])

    assert [(arg.name, arg.default) for arg in result.entry.args] == [
        ("-density", "0.5")
    ]
    assert result.rejections == ()


def test_command_named_only_in_prose_is_flagged_suspect_not_rejected():
    """Grounded, but possibly a *mentioned* command rather than the documented
    one. A veto here would drop correct entries."""
    section = _section(
        "This section explains placement. See also repair_design for timing.",
        title="Placement Notes",
        path="Manual > Placement Notes",
        hint="",
    )

    result = validate_entry({"command_name": "repair_design"}, section,
                            ["repair_design"])

    assert result.accepted is True
    assert result.entry.suspect_related is True


def test_description_and_examples_pass_through_with_an_anchor_seat(
    flops_section,
):
    result = validate_entry(_flops_entry(), flops_section,
                            command_candidates(flops_section))

    assert result.entry.description == "Clusters flops into multi-bit trays."
    assert result.entry.examples == ("cluster_flops -tray_weight 32.0",)
    assert result.entry.anchor_element_ids == ()


# ------------------------------------------- 5b. field-type coercion (P2 fix)
def test_required_string_false_is_coerced_not_truthy(flops_section):
    """The reported bug: `bool("false")` is `True`, so a model that quotes its
    booleans (`"required": "false"`) would previously flip every such
    parameter to required. Coercion must go by MEANING, not by truthiness."""
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "required": "false"}]
    )

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.entry.args[0].required is False
    assert result.rejections == ()


def test_required_string_true_is_coerced(flops_section):
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "required": "TRUE"}]
    )

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.entry.args[0].required is True
    assert result.rejections == ()


def test_required_non_boolean_value_degrades_and_is_reported(flops_section):
    """A shape that is neither a bool nor a recognisable true/false string
    (a bare number, here) cannot be trusted either way, so it degrades to
    `False` — the same "cleared, never fatal" treatment `default` gets — and
    the degrade is recorded rather than silent."""
    entry = _flops_entry(
        args=[{"name": "-tray_weight", "desc": "Tray weight.", "required": 1}]
    )

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.accepted is True
    assert result.entry.args[0].required is False
    rejection = next(item for item in result.rejections if item.field == "required")
    assert rejection.value == "1"
    assert rejection.reason == "not_boolean"


def test_args_field_returned_as_an_object_is_not_iterated_as_keys(flops_section):
    """A model returning `args` as an object rather than a list used to be
    walked with a bare `for`, which iterates a dict's KEYS — plain strings
    that then failed the `isinstance(..., Mapping)` check and vanished with
    no record. The whole field must instead be treated as malformed."""
    entry = _flops_entry(args={"name": "-tray_weight", "required": False})

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.accepted is True
    assert result.entry.args == ()
    assert result.stats.args_seen == 0
    rejection = next(item for item in result.rejections if item.field == "args")
    assert rejection.reason == "model_response_unusable"


def test_args_list_item_that_is_not_an_object_is_dropped_and_reported(flops_section):
    entry = _flops_entry(args=["-tray_weight"])

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.accepted is True
    assert result.entry.args == ()
    rejection = next(item for item in result.rejections if item.field == "arg")
    assert rejection.reason == "not_object"


def test_examples_string_scalar_is_coerced_to_a_single_element_list(flops_section):
    """A model answering "one example" with a bare string instead of a
    one-element list got the wrapper wrong, not the content — coalescing is
    the reasonable read, not a rejection-worthy failure. Iterating the raw
    string directly, as the old code did, would have shredded it into
    one-character "examples" instead."""
    entry = _flops_entry(examples="cluster_flops -tray_weight 32.0")

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.entry.examples == ("cluster_flops -tray_weight 32.0",)
    assert result.rejections == ()


def test_examples_non_string_item_is_dropped_and_reported(flops_section):
    entry = _flops_entry(examples=["cluster_flops -tray_weight 32.0", 5])

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.entry.examples == ("cluster_flops -tray_weight 32.0",)
    rejection = next(item for item in result.rejections if item.field == "examples")
    assert rejection.reason == "not_string"
    assert rejection.value == "5"


def test_examples_non_list_non_string_field_is_dropped_and_reported(flops_section):
    entry = _flops_entry(examples={"one": "cluster_flops -tray_weight 32.0"})

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    assert result.entry.examples == ()
    rejection = next(item for item in result.rejections if item.field == "examples")
    assert rejection.reason == "not_list"


def test_rejection_diagnostics_stay_bounded(flops_section):
    entry = _flops_entry(args=[{"name": "-" + "x" * 400, "desc": ""}])

    result = validate_entry(entry, flops_section, command_candidates(flops_section))

    rejection = result.rejections[0]
    assert len(rejection.value) <= 120
    assert len(rejection.window) <= 200


def test_rejection_window_centers_on_a_real_hit_in_long_text():
    """`_window`'s hit branch (a near-miss, like C0's dropped-dash case) must
    centre on where the value was actually found in a genuinely long section,
    not just return the section's head — the miss branch is already covered
    by `test_rejection_diagnostics_stay_bounded`, whose invented value is
    never in the text at all."""
    padding_before = "Unrelated padding text. " * 20
    padding_after = " More unrelated trailing text." * 20
    text = (
        f"{padding_before}"
        "cluster_flops [-tray_weight tray_weight]"
        f"{padding_after}"
    )
    section = _section(text, hint="cluster_flops")
    entry = {
        "command_name": "cluster_flops",
        "args": [{"name": "tray_weight", "desc": "Tray weight."}],
    }

    result = validate_entry(entry, section, ["cluster_flops"])

    rejection = next(item for item in result.rejections if item.field == "arg")
    assert rejection.reason == "dash_stripped"
    assert len(text) > 400
    assert len(rejection.window) <= 200
    # Centred on the hit, not the head of a 400+-char section.
    assert rejection.window != text[:200]
    assert "tray_weight" in rejection.window


def test_normalize_syntax_erases_continuations_and_collapses_space():
    assert normalize_syntax("a \\\n   b\\\nc") == "a b c"
    assert normalize_syntax("  a   b  ") == "a b"


# ----------------------------------------------------------------- 6. ledger
def test_outcome_lists_the_candidates_nothing_was_extracted_for(flops_section):
    candidates = command_candidates(flops_section)
    result = validate_entry(_flops_entry(), flops_section, candidates)

    outcome = section_outcome(flops_section, candidates, [result])

    assert outcome.accepted_names == ("cluster_flops",)
    assert "cluster_flops" not in outcome.uncovered_candidates
    assert outcome.entries_seen == 1
    assert outcome.command_rejects == 0
    assert outcome.args_seen == outcome.args_kept == 5
    assert outcome.section_path == FLOPS_PATH


def test_outcome_reports_uncovered_candidates(flops_section):
    candidates = ["cluster_flops", "detailed_placement"]
    result = validate_entry(_flops_entry(), flops_section, candidates)

    outcome = section_outcome(flops_section, candidates, [result])

    assert outcome.uncovered_candidates == ("detailed_placement",)


def test_section_outcome_caps_rejections_and_counts_the_overflow(flops_section):
    """B4: a pathological entry (or a model that never stops inventing
    parameters) must not let a job report inherit an unbounded rejection
    list through its own failure records — the ledger caps it and counts
    what got cut rather than silently reading as "everything else was
    clean"."""
    candidates = command_candidates(flops_section)
    entry = _flops_entry(
        args=[{"name": f"-invented_{index}", "desc": ""} for index in range(30)]
    )

    result = validate_entry(entry, flops_section, candidates)
    assert len(result.rejections) == 30  # per-entry validation stays uncapped

    outcome = section_outcome(flops_section, candidates, [result])

    assert len(outcome.rejections) == MAX_SECTION_REJECTIONS
    assert outcome.rejections_overflow == 30 - MAX_SECTION_REJECTIONS


def test_catalog_stats_publish_the_command_reject_ratio(flops_section):
    candidates = command_candidates(flops_section)
    good = validate_entry(_flops_entry(), flops_section, candidates)
    bad = validate_entry(
        _flops_entry(command_name="detailed_placement"), flops_section, candidates
    )
    outcome = section_outcome(flops_section, candidates, [good, bad, bad])

    stats = catalog_stats([outcome])

    assert stats.sections == 1
    assert stats.entries_seen == 3
    assert stats.command_rejects == 2
    assert stats.command_reject_ratio == pytest.approx(0.6667, abs=1e-4)
    assert stats.truncated_sections == 0


def test_catalog_stats_publish_the_args_keep_ratio(flops_section):
    """B4: C1b's circuit breaker needs an args axis alongside the
    command-name reject ratio — an entry can pick the right command name and
    still invent every parameter, and a veto-rate-only breaker would miss
    that entirely."""
    candidates = command_candidates(flops_section)
    good = validate_entry(_flops_entry(), flops_section, candidates)
    entry = _flops_entry()
    entry["args"] = entry["args"] + [
        {"name": "-tray_capacity", "desc": "Invented.", "default": ""}
    ]
    partial = validate_entry(entry, flops_section, candidates)
    outcome = section_outcome(flops_section, candidates, [good, partial])

    stats = catalog_stats([outcome])

    assert stats.args_seen == 11
    assert stats.args_kept == 10
    assert stats.args_keep_ratio == pytest.approx(10 / 11, abs=1e-4)


def test_catalog_stats_on_nothing_do_not_divide_by_zero():
    stats = catalog_stats([])

    assert stats.entries_seen == 0
    assert stats.args_keep_ratio == 0.0
    assert stats.command_reject_ratio == 0.0
