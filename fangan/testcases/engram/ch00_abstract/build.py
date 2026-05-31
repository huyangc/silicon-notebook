#!/usr/bin/env python3
"""Builder for ch00_abstract gold.yaml (schema v0.3.3, profile article_research).

Regenerates source.md (viewer-only verbatim slice) and gold.yaml from the
authoritative MinerU source file. All spans are computed by locating verbatim
substrings (anchors) in the source; YAML spans are never hand-written.

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
import re
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

# Authoritative slice: title/authors (1-7) for source_meta context; Abstract heading
# line 9, paragraph line 11. We emit the viewer slice as lines 9-11 (the Abstract block).
SLICE_LINE_START = 9
SLICE_LINE_END = 11

# ---------------------------------------------------------------------------
# Load source, compute per-line char offsets into source_file.
# ---------------------------------------------------------------------------
SRC = open(SOURCE_FILE, encoding="utf-8").read()
SRC_LINES = SRC.split("\n")

def line_char_start(text, line_no):
    """Return char offset (0-based) where 1-based line_no begins."""
    off = 0
    for i in range(line_no - 1):
        off += len(text.split("\n")[i]) + 1
    return off

# precompute offsets for the lines we touch
_LINE_OFF = {}
_acc = 0
for i, ln in enumerate(SRC_LINES, start=1):
    _LINE_OFF[i] = _acc
    _acc += len(ln) + 1

ABS_LINE = 11
ABS_START = _LINE_OFF[ABS_LINE]
ABS_TEXT = SRC_LINES[ABS_LINE - 1]

# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice of lines 9-11 + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY

# offset of the abstract paragraph line within the viewer file
def viewer_line_offset(line_no_in_slice):
    """offset into VIEWER_TEXT of the slice's nth line (1-based within slice)."""
    off = len(VIEWER_HEADER)
    sl = VIEWER_BODY.split("\n")
    for i in range(line_no_in_slice - 1):
        off += len(sl[i]) + 1
    return off

# abstract paragraph is the 3rd line of the slice (9=#Abstract,10=blank,11=para)
ABS_VIEWER_LINE_IN_SLICE = 3
ABS_VIEWER_START = viewer_line_offset(ABS_VIEWER_LINE_IN_SLICE)
# its source-file viewer line number
ABS_VIEWER_LINENO = 1 + 1 + (SLICE_LINE_END - SLICE_LINE_START + 1) - 1  # header(1)+...
# simpler: header is line1, slice line9 ->line2, line10->line3, line11->line4
ABS_VIEWER_LINENO = 4


# ---------------------------------------------------------------------------
# Span helper: locate a verbatim substring of the abstract paragraph and emit
# both source_span (authoritative) and viewer_span.
# ---------------------------------------------------------------------------
def span_for(raw):
    """raw must be a verbatim contiguous substring of the abstract paragraph."""
    idx = ABS_TEXT.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND in abstract paragraph: %r" % raw[:60])
    # ensure unique
    if ABS_TEXT.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS substring (matches >1): %r" % raw[:60])
    src_cs = ABS_START + idx
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch"
    v_cs = ABS_VIEWER_START + idx
    v_ce = v_cs + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch"
    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", ABS_LINE), ("line_end", ABS_LINE),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", ABS_VIEWER_LINENO), ("line_end", ABS_VIEWER_LINENO),
        ("char_start", v_cs), ("char_end", v_ce),
        ("viewer_only", True),
    ])
    return source_span, viewer_span


def atom(aid, atom_type, raw, normalized, evidence_strength, se_id, metadata=None):
    ss, vs = span_for(raw)
    d = OrderedDict()
    d["id"] = aid
    d["section_id"] = "SEC-ABS"
    d["atom_type"] = atom_type
    d["source_element_id"] = se_id
    d["source_span"] = ss
    d["viewer_span"] = vs
    d["raw_text"] = raw
    d["normalized_text"] = normalized
    d["evidence_strength"] = evidence_strength
    if metadata:
        d["metadata"] = metadata
    return d


# ---------------------------------------------------------------------------
# Evidence atoms.  raw_text = verbatim substrings of the abstract paragraph.
# normalized_text renders EXACTLY that span (ascii: O(1), arrow ->), no out-of-span facts.
# All atoms live in one source element SE-ABS-P (the paragraph) under SEC-ABS.
# ---------------------------------------------------------------------------
SE_ID = "SE-ABS-P"

ATOMS = [
    atom(
        "A-ABS-PROBLEM", "claim_sentence",
        "While Mixture-of-Experts (MoE) scales capacity via conditional computation, Transformers lack a native primitive for knowledge lookup, forcing them to inefficiently simulate retrieval through computation.",
        "While Mixture-of-Experts (MoE) scales capacity via conditional computation, Transformers lack a native primitive for knowledge lookup, forcing them to inefficiently simulate retrieval through computation.",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-INTRO-ENGRAM", "method_sentence",
        "To address this, we introduce conditional memory as a complementary sparsity axis, instantiated via Engram, a module that modernizes classic N-gram embedding for $O(1)$ lookup.",
        "To address this, we introduce conditional memory as a complementary sparsity axis, instantiated via Engram, a module that modernizes classic N-gram embedding for O(1) lookup.",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-SCALING-LAW", "scaling_law_result_atom",
        "By formulating the Sparsity Allocation problem, we uncover a U-shaped scaling law that optimizes the trade-off between neural computation (MoE) and static memory (Engram).",
        "By formulating the Sparsity Allocation problem, we uncover a U-shaped scaling law that optimizes the trade-off between neural computation (MoE) and static memory (Engram).",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-SCALE-27B", "result_sentence",
        "Guided by this law, we scale Engram to 27B parameters, achieving superior performance over a strictly iso-parameter and iso-FLOPs MoE baseline.",
        "Guided by this law, we scale Engram to 27B parameters, achieving superior performance over a strictly iso-parameter and iso-FLOPs MoE baseline.",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-RESULT-KNOWLEDGE", "result_sentence",
        "while the memory module is expected to aid knowledge retrieval (e.g., MMLU +3.4; CMMLU +4.0)",
        "while the memory module is expected to aid knowledge retrieval (e.g., MMLU +3.4; CMMLU +4.0)",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-RESULT-REASONING", "result_sentence",
        "we observe even larger gains in general reasoning (e.g., BBH +5.0; ARC-Challenge +3.7)",
        "we observe even larger gains in general reasoning (e.g., BBH +5.0; ARC-Challenge +3.7)",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-RESULT-CODEMATH", "result_sentence",
        "code/math domains (HumanEval +3.0; MATH +2.4)",
        "code/math domains (HumanEval +3.0; MATH +2.4)",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-MECH-DEPTH", "mechanism_sentence",
        "Mechanistic analyses reveal that Engram relieves the backbone's early layers from static reconstruction, effectively deepening the network for complex reasoning.",
        "Mechanistic analyses reveal that Engram relieves the backbone's early layers from static reconstruction, effectively deepening the network for complex reasoning.",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-MECH-ATTENTION", "mechanism_sentence",
        "by delegating local dependencies to lookups, it frees up attention capacity for global context, substantially boosting long-context retrieval (e.g., Multi-Query NIAH: 84.2 → 97.0).",
        "by delegating local dependencies to lookups, it [Engram] frees up attention capacity for global context, substantially boosting long-context retrieval (e.g., Multi-Query NIAH: 84.2 -> 97.0).",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-SYS-EFFICIENCY", "claim_sentence",
        "Engram establishes infrastructure-aware efficiency: its deterministic addressing enables runtime prefetching from host memory, incurring negligible overhead.",
        "Engram establishes infrastructure-aware efficiency: its deterministic addressing enables runtime prefetching from host memory, incurring negligible overhead.",
        "direct", SE_ID,
    ),
    atom(
        "A-ABS-VISION", "claim_sentence",
        "We envision conditional memory as an indispensable modeling primitive for next-generation sparse models. Code available at: https://github.com/deepseek-ai/Engram",
        "We envision conditional memory as an indispensable modeling primitive for next-generation sparse models. Code available at: https://github.com/deepseek-ai/Engram",
        "direct", SE_ID,
    ),
]

ATOM_IDS = [a["id"] for a in ATOMS]

# ---------------------------------------------------------------------------
# source_elements: auto-generate by grouping atoms by source_element_id.
# The Abstract has one heading element (line 9) + one paragraph element (line 11).
# ---------------------------------------------------------------------------
def build_source_elements(atoms):
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    elems = []
    # heading element first (line 9), not carrying atoms
    elems.append(OrderedDict([
        ("id", "SE-ABS-H"), ("type", "heading"),
        ("file", SOURCE_NAME), ("line_start", 9), ("line_end", 9),
    ]))
    for se_id, members in groups.items():
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        elems.append(OrderedDict([
            ("id", se_id), ("type", "paragraph"),
            ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le),
        ]))
    return elems

SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ---------------------------------------------------------------------------
# section_tree
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-ABS"), ("path", "Abstract"), ("title", "Abstract")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks: single core-claim block (no cross_reference block here).
# ---------------------------------------------------------------------------
SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C-ABS"),
        ("profile", "article_research"),
        ("chunk_type", "article_core_claim_block"),
        ("section_path", "Abstract"),
        ("atom_ids", list(ATOM_IDS)),
        ("central_atom_ids", ["A-ABS-INTRO-ENGRAM", "A-ABS-SCALING-LAW"]),
        ("boundary_reason",
         "The Abstract is a single contiguous paragraph forming the paper's core claim/contribution block; "
         "problem->method->scaling law->scale->results->mechanism->efficiency->vision are kept intact because "
         "they jointly support one set of ArticleClaim/ArticleMethod/ScalingLaw objects (qiefen Section 5.3: "
         "keep the core-claim block intact)."),
        ("extraction_targets", ["ArticleClaim", "ArticleMethod", "ScalingLaw", "ExperimentResult",
                                "MechanisticExplanation", "SystemDesignClaim", "Implication"]),
        ("gold_must_cover_atoms", ["A-ABS-INTRO-ENGRAM", "A-ABS-SCALING-LAW", "A-ABS-SCALE-27B",
                                   "A-ABS-RESULT-KNOWLEDGE", "A-ABS-RESULT-REASONING"]),
    ]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}

# ---------------------------------------------------------------------------
# objects: home_package=PKG-ABS for all (single chunk). local = atoms in chunk,
# supporting = empty (only one chunk). gold_label/difficulty/evidence_strength.
# ---------------------------------------------------------------------------
def obj(oid, otype, home_package, payload, local, supporting, difficulty, evidence_strength):
    d = OrderedDict()
    d["id"] = oid
    d["type"] = otype
    d["section_path"] = "Abstract"
    d["home_package"] = home_package
    d["payload"] = payload
    d["local_evidence_atom_ids"] = local
    d["supporting_context_atom_ids"] = supporting
    d["gold_label"] = True
    d["difficulty"] = difficulty
    d["evidence_strength"] = evidence_strength
    return d

OBJECTS = [
    obj("CLAIM-CONDITIONAL-MEMORY", "ArticleClaim", "PKG-ABS",
        OrderedDict([
            ("statement", "Conditional memory is a complementary sparsity axis to MoE's conditional computation, addressing Transformers' lack of a native knowledge-lookup primitive."),
            ("problem_addressed", "Transformers inefficiently simulate retrieval through computation."),
            ("novelty", "introduces conditional memory as a new sparsity axis, instantiated via Engram (O(1) lookup)."),
        ]),
        ["A-ABS-PROBLEM", "A-ABS-INTRO-ENGRAM"], [], "medium", "direct"),
    obj("CLAIM-VISION-PRIMITIVE", "ArticleClaim", "PKG-ABS",
        OrderedDict([
            ("statement", "Conditional memory should be an indispensable modeling primitive for next-generation sparse models."),
        ]),
        ["A-ABS-VISION"], [], "easy", "direct"),
    obj("METHOD-ENGRAM", "ArticleMethod", "PKG-ABS",
        OrderedDict([
            ("name", "Engram"),
            ("description", "A module that modernizes classic N-gram embedding to provide O(1) knowledge lookup, instantiating conditional memory as a sparsity axis."),
            ("mechanism", ["modernized N-gram embedding", "O(1) lookup",
                           "deterministic addressing enabling host-memory runtime prefetch"]),
            ("scale", "scaled to 27B parameters"),
        ]),
        ["A-ABS-INTRO-ENGRAM", "A-ABS-SCALE-27B", "A-ABS-SYS-EFFICIENCY"], [], "medium", "direct"),
    obj("SCALINGLAW-U-SHAPED", "ScalingLaw", "PKG-ABS",
        OrderedDict([
            ("name", "U-shaped Sparsity Allocation scaling law"),
            ("statement", "Validation performance follows a U-shaped law in the allocation ratio between neural computation (MoE) and static memory (Engram); an optimal trade-off exists."),
            ("governs", "trade-off between MoE compute and Engram memory"),
        ]),
        ["A-ABS-SCALING-LAW"], [], "medium", "direct"),
    obj("RESULT-VS-MOE-BASELINE", "ExperimentResult", "PKG-ABS",
        OrderedDict([
            ("setup", "Engram-27B vs strictly iso-parameter and iso-FLOPs MoE baseline"),
            ("finding", "Engram achieves superior overall performance over the iso-param/iso-FLOPs MoE baseline."),
            ("knowledge", OrderedDict([("MMLU", "+3.4"), ("CMMLU", "+4.0")])),
            ("reasoning", OrderedDict([("BBH", "+5.0"), ("ARC-Challenge", "+3.7")])),
            ("code_math", OrderedDict([("HumanEval", "+3.0"), ("MATH", "+2.4")])),
        ]),
        ["A-ABS-SCALE-27B", "A-ABS-RESULT-KNOWLEDGE", "A-ABS-RESULT-REASONING", "A-ABS-RESULT-CODEMATH"],
        [], "medium", "direct"),
    obj("RESULT-LONG-CONTEXT", "ExperimentResult", "PKG-ABS",
        OrderedDict([
            ("metric", "Multi-Query NIAH (long-context retrieval)"),
            ("before", "84.2"), ("after", "97.0"),
            ("note", "improvement attributed to freed attention capacity for global context."),
        ]),
        ["A-ABS-MECH-ATTENTION"], [], "medium", "direct"),
    obj("MECH-EFFECTIVE-DEPTH", "MechanisticExplanation", "PKG-ABS",
        OrderedDict([
            ("mechanism", "Engram relieves the backbone's early layers from static reconstruction, effectively deepening the network for complex reasoning."),
            ("explains", "larger-than-expected gains in general reasoning"),
        ]),
        ["A-ABS-MECH-DEPTH", "A-ABS-RESULT-REASONING"], [], "hard", "indirect"),
    obj("MECH-FREED-ATTENTION", "MechanisticExplanation", "PKG-ABS",
        OrderedDict([
            ("mechanism", "By delegating local dependencies to lookups, Engram frees up attention capacity for global context."),
            ("explains", "boosted long-context retrieval (Multi-Query NIAH 84.2 -> 97.0)"),
        ]),
        ["A-ABS-MECH-ATTENTION"], [], "medium", "direct"),
    obj("SYS-DETERMINISTIC-PREFETCH", "SystemDesignClaim", "PKG-ABS",
        OrderedDict([
            ("claim", "Engram establishes infrastructure-aware efficiency."),
            ("mechanism", "deterministic addressing enables runtime prefetching from host memory"),
            ("benefit", "negligible overhead"),
        ]),
        ["A-ABS-SYS-EFFICIENCY"], [], "medium", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids. expected_objects =
# objects whose home_package is this package. No cross-package objects -> no
# expected_local_fields needed.
# ---------------------------------------------------------------------------
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

def build_package(pid, chunk_id, expected_objects):
    return OrderedDict([
        ("id", pid),
        ("profile", "article_research"),
        ("chunk_id", chunk_id),
        ("section_path", "Abstract"),
        ("document_title", "Conditional Memory via Scalable Lookup: A New Axis of Sparsity for Large Language Models"),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", OrderedDict([
            ("previous_heading", "(document title) Conditional Memory via Scalable Lookup"),
            ("next_heading", "1. Introduction"),
        ])),
        ("extraction_targets", ["ArticleClaim", "ArticleMethod", "ScalingLaw", "ExperimentResult",
                                "MechanisticExplanation", "SystemDesignClaim"]),
        ("expected_objects", expected_objects),
    ])

CONTEXT_PACKAGES = [
    build_package("PKG-ABS", "C-ABS",
                  [o["id"] for o in OBJECTS if o["home_package"] == "PKG-ABS"]),
]

# ---------------------------------------------------------------------------
# mentions
# ---------------------------------------------------------------------------
MENTIONS = [
    OrderedDict([("id", "M-01"), ("text", "conditional memory"), ("type", "Concept"), ("atom_id", "A-ABS-INTRO-ENGRAM"), ("canonical_key", "conditional_memory")]),
    OrderedDict([("id", "M-02"), ("text", "Engram"), ("type", "ArticleMethod"), ("atom_id", "A-ABS-INTRO-ENGRAM"), ("canonical_key", "engram")]),
    OrderedDict([("id", "M-03"), ("text", "MoE"), ("type", "ArticleMethod"), ("atom_id", "A-ABS-PROBLEM"), ("canonical_key", "moe")]),
    OrderedDict([("id", "M-04"), ("text", "conditional computation"), ("type", "Concept"), ("atom_id", "A-ABS-PROBLEM"), ("canonical_key", "conditional_computation")]),
    OrderedDict([("id", "M-05"), ("text", "O(1) lookup"), ("type", "Concept"), ("atom_id", "A-ABS-INTRO-ENGRAM"), ("canonical_key", "o1_lookup")]),
    OrderedDict([("id", "M-06"), ("text", "Sparsity Allocation"), ("type", "ScalingLaw"), ("atom_id", "A-ABS-SCALING-LAW"), ("canonical_key", "sparsity_allocation")]),
    OrderedDict([("id", "M-07"), ("text", "U-shaped scaling law"), ("type", "ScalingLaw"), ("atom_id", "A-ABS-SCALING-LAW"), ("canonical_key", "u_shaped_scaling_law")]),
    OrderedDict([("id", "M-08"), ("text", "MMLU"), ("type", "ExperimentResult"), ("atom_id", "A-ABS-RESULT-KNOWLEDGE"), ("canonical_key", "mmlu")]),
    OrderedDict([("id", "M-09"), ("text", "BBH"), ("type", "ExperimentResult"), ("atom_id", "A-ABS-RESULT-REASONING"), ("canonical_key", "bbh")]),
    OrderedDict([("id", "M-10"), ("text", "Multi-Query NIAH"), ("type", "ExperimentResult"), ("atom_id", "A-ABS-MECH-ATTENTION"), ("canonical_key", "multi_query_niah")]),
    OrderedDict([("id", "M-11"), ("text", "deterministic addressing"), ("type", "SystemDesignClaim"), ("atom_id", "A-ABS-SYS-EFFICIENCY"), ("canonical_key", "deterministic_prefetch")]),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "conditional_memory"),
                 ("aliases", ["conditional memory", "static memory", "Engram memory module", "memory module"]),
                 ("note", "In the Abstract 'static memory (Engram)' and 'the memory module' both refer to conditional memory as the sparsity axis.")]),
    OrderedDict([("canonical", "u_shaped_scaling_law"),
                 ("aliases", ["U-shaped scaling law", "Sparsity Allocation problem",
                              "trade-off between neural computation (MoE) and static memory (Engram)"])]),
    OrderedDict([("canonical", "engram"),
                 ("aliases", ["Engram", "Engram-27B", "conditional memory module"])]),
]

# ---------------------------------------------------------------------------
# relations: gold_label/difficulty/evidence_strength. Endpoints in OBJECTS.
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])

RELATIONS = [
    rel("R-01", "method_addresses_problem", "METHOD-ENGRAM", "CLAIM-CONDITIONAL-MEMORY",
        ["A-ABS-PROBLEM", "A-ABS-INTRO-ENGRAM"], "medium", "direct"),
    rel("R-02", "result_supports_claim", "RESULT-VS-MOE-BASELINE", "CLAIM-CONDITIONAL-MEMORY",
        ["A-ABS-SCALE-27B", "A-ABS-RESULT-KNOWLEDGE"], "medium", "direct"),
    rel("R-03", "result_supports_claim", "SCALINGLAW-U-SHAPED", "CLAIM-CONDITIONAL-MEMORY",
        ["A-ABS-SCALING-LAW"], "hard", "indirect"),
    rel("R-04", "experiment_tests_claim", "RESULT-VS-MOE-BASELINE", "METHOD-ENGRAM",
        ["A-ABS-SCALE-27B"], "medium", "direct"),
    rel("R-05", "mechanism_explains_result", "MECH-EFFECTIVE-DEPTH", "RESULT-VS-MOE-BASELINE",
        ["A-ABS-MECH-DEPTH", "A-ABS-RESULT-REASONING"], "hard", "indirect"),
    rel("R-06", "mechanism_explains_result", "MECH-FREED-ATTENTION", "RESULT-LONG-CONTEXT",
        ["A-ABS-MECH-ATTENTION"], "medium", "direct"),
    rel("R-07", "system_design_enables_efficiency", "METHOD-ENGRAM", "SYS-DETERMINISTIC-PREFETCH",
        ["A-ABS-SYS-EFFICIENCY"], "medium", "direct"),
    rel("R-08", "result_supports_claim", "RESULT-LONG-CONTEXT", "CLAIM-VISION-PRIMITIVE",
        ["A-ABS-MECH-ATTENTION", "A-ABS-VISION"], "hard", "indirect"),
    rel("R-09", "claim_guided_by_scaling_law", "METHOD-ENGRAM", "SCALINGLAW-U-SHAPED",
        ["A-ABS-SCALING-LAW", "A-ABS-SCALE-27B"], "hard", "indirect"),
]

# ---------------------------------------------------------------------------
# do_not_extract: inline author-year citations in slice (none in Abstract para),
# the github code link, inline_author_year_citation policy. No figures in slice.
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([
        ("text", "https://github.com/deepseek-ai/Engram"),
        ("atom_id", "A-ABS-VISION"),
        ("reason", "Code repository URL; a resource link, not a knowledge object."),
        ("kind", "out_of_slice_reference"),
    ]),
    OrderedDict([
        ("pattern", "inline_author_year_citation"),
        ("examples", ["(Vaswani et al., 2017)", "(Xie et al., 2025)"]),
        ("reason", "inline author-year citations are not knowledge objects; the Abstract paragraph itself contains none, but this policy entry suppresses citation over-extraction consistently with other chapters."),
        ("kind", "citation_policy"),
    ]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Abstract only"),
    ("title", "Conditional Memory via Scalable Lookup: A New Axis of Sparsity for Large Language Models - Abstract"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [9, 11]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 9-11; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "academic_paper"),
    ("profile_cues", ["Abstract", "we introduce", "we observe", "scaling law",
                      "iso-parameter and iso-FLOPs", "MMLU +3.4", "Code available at"]),
    ("extraction_targets", ["ArticleClaim", "ArticleMethod", "ScalingLaw", "ExperimentResult",
                            "MechanisticExplanation", "SystemDesignClaim", "Implication"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span: it may rephrase, transliterate math (O(1), arrow ->), and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs not inside the span. EXCEPTION: a formula_atom MAY carry a symbolic interpretation supplied by atoms in metadata.supported_by_context_atoms / interpretation_supported_by."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks needed to fully define it). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only; supporting_context atoms are NOT required to be present in the home package's input."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone. The Abstract is a single chunk/package, so there are no cross-package objects and no expected_local_fields."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "figure atoms separate physical_section_id from semantic_section_ids; forward references set include_in_chapter2_core_chunks:false and are isolated in a cross_reference_block. The Abstract contains no figures."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("context_only", "an atom not consumed by any object or relation may be flagged context_only:true; the Abstract has no such atoms (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "The entire Abstract is a single contiguous paragraph (source line 11) with no sub-sections, lists, or display formulas; section_tree has only the Abstract node.",
        "Author superscripts on lines 1-7 render as inline math ($^{1,2}$ / $^{2}$); they belong to the title/author block and are not atomized.",
        "Inside the Abstract, $O(1)$ is inline math and is normalized to ASCII 'O(1)'.",
        "The long-context arrow '84.2 → 97.0' is a Unicode right arrow (U+2192) normalized to '84.2 -> 97.0'.",
        "Benchmark gains use inline '+3.4' style numbers; the knowledge/reasoning/code-math results sit inside one sentence (a 'while ... we observe ...' construction) and are split into sub-span atoms.",
        "All gains are relative to the strictly iso-parameter and iso-FLOPs MoE baseline.",
    ]),
])

# ---------------------------------------------------------------------------
# Assemble (ordered) and dump.
# ---------------------------------------------------------------------------
GOLD = OrderedDict([
    ("schema_version", "0.3.3"),
    ("source_meta", SOURCE_META),
    ("source_elements", SOURCE_ELEMENTS),
    ("section_tree", SECTION_TREE),
    ("evidence_atoms", ATOMS),
    ("semantic_chunks", SEMANTIC_CHUNKS),
    ("context_packages", CONTEXT_PACKAGES),
    ("mentions", MENTIONS),
    ("canonicalization", CANONICALIZATION),
    ("objects", OBJECTS),
    ("relations", RELATIONS),
    ("do_not_extract", DO_NOT_EXTRACT),
])


class _Dumper(yaml.SafeDumper):
    pass

def _od_repr(dumper, data):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())

_Dumper.add_representer(OrderedDict, _od_repr)


def main():
    # write viewer file
    with open(os.path.join(HERE, "source.md"), "w", encoding="utf-8") as f:
        f.write(VIEWER_TEXT)
    # write gold
    header = (
        "# Gold fixture v0.3.3 - Engram paper Abstract (profile: article_research)\n"
        "# Upgraded from v0.1: dual coordinates (source_span authoritative + viewer_span),\n"
        "# raw/normalized discipline, object evidence split into local/supporting + home_package,\n"
        "# per-chunk context_packages with expected_objects, gold_label/difficulty/evidence_strength\n"
        "# (no decimal confidence), do_not_extract negatives, and auto-generated source_elements.\n"
        "# Spans are computed by build.py against engram_paper_mineru.md; do NOT hand-edit.\n"
    )
    body = yaml.dump(GOLD, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=10000)
    with open(os.path.join(HERE, "gold.yaml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    print("wrote source.md (%d bytes) and gold.yaml" % len(VIEWER_TEXT))
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d" % (
        len(ATOMS), len(SOURCE_ELEMENTS), len(SEMANTIC_CHUNKS),
        len(CONTEXT_PACKAGES), len(OBJECTS), len(RELATIONS)))


if __name__ == "__main__":
    main()
