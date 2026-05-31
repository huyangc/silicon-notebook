#!/usr/bin/env python3
"""Builder for ch08_conclusion gold.yaml (schema v0.3.3, profile article_research).

Regenerates source.md (viewer-only verbatim slice) and gold.yaml from the
authoritative MinerU source file. All spans are computed by locating verbatim
substrings (anchors) within the chapter-8 slice of the source; YAML spans are
never hand-written.

This is the v0.3.3 re-expression of the original v0.1 ch08 gold: the SAME
atom/object/relation IDs and meanings are preserved, but every atom now carries
a verbatim raw_text span (source_span authoritative + viewer_span debug),
within-span normalized_text, objects split evidence into local/supporting +
home_package, the single conclusion_block maps to ONE package, relations use
gold_label/difficulty/evidence_strength (no confidence), and do_not_extract
lists the inline citation policy.

Quirks handled (see source_meta.parsing_notes):
  - line 343 (A-CONC-ENGRAM) lacks a terminal period ("...for static patterns").
  - the mechanism claim (A-CONC-MECH) spans a PDF page break: source lines
    347-349 ("...attention capacity to focus" / blank / "on global context...").
  - $O(1)$ is inline math kept verbatim in raw_text, normalized to O(1).
  - "deepen" appears with curly quotes U+201C/U+201D in the source; kept verbatim.

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

# Authoritative slice: heading (341) .. last conclusion paragraph (349).
# '# References' is line 351 -> lower boundary, excluded.
SLICE_LINE_START = 341
SLICE_LINE_END = 349

# ---------------------------------------------------------------------------
# Load source, compute per-line char offsets into source_file.
# ---------------------------------------------------------------------------
SRC = open(SOURCE_FILE, encoding="utf-8").read()
SRC_LINES = SRC.split("\n")

_LINE_OFF = {}
_acc = 0
for i, ln in enumerate(SRC_LINES, start=1):
    _LINE_OFF[i] = _acc
    _acc += len(ln) + 1

# char offset (into source_file) where the slice begins / ends
SLICE_START = _LINE_OFF[SLICE_LINE_START]
# end = start of line after SLICE_LINE_END (i.e. through its trailing newline)
SLICE_END = _LINE_OFF[SLICE_LINE_END] + len(SRC_LINES[SLICE_LINE_END - 1]) + 1
SLICE_SRC = SRC[SLICE_START:SLICE_END]

# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice of lines 341-349 + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- source.md = VIEWER-ONLY verbatim slice of {src}, original lines {a}-{b}.\n"
    "     Authoritative gold coordinates live in gold.yaml under each atom's source_span "
    "(file={src}).\n"
    "     viewer_span here is optional/debug and may drift if this file is reformatted. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

# body = the verbatim slice (same text that lives at SRC[SLICE_START:SLICE_END])
VIEWER_BODY = SLICE_SRC
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY
VIEWER_BODY_OFFSET = len(VIEWER_HEADER)  # where the slice body starts in source.md


def _lineno_in_source(char_off_in_src):
    """1-based source-file line number containing absolute char offset."""
    lo = 1
    for i in range(1, len(SRC_LINES) + 1):
        if _LINE_OFF[i] <= char_off_in_src:
            lo = i
        else:
            break
    return lo


def _viewer_lineno(char_off_in_viewer):
    """1-based line number within source.md for an absolute viewer offset."""
    return VIEWER_TEXT.count("\n", 0, char_off_in_viewer) + 1


# ---------------------------------------------------------------------------
# Span helper: locate a verbatim substring inside the chapter-8 slice and emit
# source_span (authoritative, file=source_file) + viewer_span (file=source.md).
# Anchoring search to the slice keeps matches unambiguous (e.g. "effectively"
# also occurs in the Abstract, but not within this slice).
# ---------------------------------------------------------------------------
def span_for(raw):
    """raw must be a verbatim contiguous substring of the chapter-8 slice."""
    idx = SLICE_SRC.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND in ch8 slice: %r" % raw[:60])
    if SLICE_SRC.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS substring (matches >1): %r" % raw[:60])

    src_cs = SLICE_START + idx
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch"

    v_cs = VIEWER_BODY_OFFSET + idx
    v_ce = v_cs + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch"

    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", _lineno_in_source(src_cs)),
        ("line_end", _lineno_in_source(src_ce - 1)),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", _viewer_lineno(v_cs)),
        ("line_end", _viewer_lineno(v_ce - 1)),
        ("char_start", v_cs), ("char_end", v_ce),
        ("viewer_only", True),
    ])
    return source_span, viewer_span


def atom(aid, atom_type, raw, normalized, evidence_strength, se_id, metadata=None):
    ss, vs = span_for(raw)
    d = OrderedDict()
    d["id"] = aid
    d["section_id"] = "SEC-8"
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
# Evidence atoms.  Same 8 IDs/meanings as v0.1, now bound to verbatim spans.
# raw_text = verbatim contiguous slice; normalized_text renders EXACTLY that span
# (ascii O(1), straight quotes), no out-of-span facts.
# Three source paragraphs (elements):
#   SE-8-P1 = line 343  (thesis + Engram/O(1))
#   SE-8-P2 = line 345  (U-shaped law + 27B result)
#   SE-8-P3 = lines 347-349 (mechanism across page break + long-ctx + infra + vision)
# ---------------------------------------------------------------------------
SE_P1 = "SE-8-P1"
SE_P2 = "SE-8-P2"
SE_P3 = "SE-8-P3"

ATOMS = [
    # --- paragraph 1 (line 343) ---
    atom(
        "A-CONC-THESIS", "claim_sentence",
        "In this work, we introduce conditional memory as a complementary sparsity axis to the prevailing conditional computation paradigm (MoE), aiming to resolve the inefficiency of simulating knowledge retrieval through dynamic computation.",
        "In this work, we introduce conditional memory as a complementary sparsity axis to the prevailing conditional computation paradigm (MoE), aiming to resolve the inefficiency of simulating knowledge retrieval through dynamic computation.",
        "direct", SE_P1,
    ),
    atom(
        # NOTE: line 343 ends with no terminal period -> raw_text ends at "static patterns".
        "A-CONC-ENGRAM", "claim_sentence",
        "We instantiate this concept via Engram, a module that modernizes classic N-gram embeddings to enable scalable, constant-time $O(1)$ lookups for static patterns",
        "We instantiate this concept via Engram, a module that modernizes classic N-gram embeddings to enable scalable, constant-time O(1) lookups for static patterns",
        "direct", SE_P1,
    ),
    # --- paragraph 2 (line 345) ---
    atom(
        "A-CONC-USHAPE", "claim_sentence",
        "By formulating the Sparsity Allocation problem, we uncover a U-shaped scaling law, demonstrating that a hybrid allocation of sparse capacity between MoE experts and Engram memory strictly outperforms pure MoE baselines.",
        "By formulating the Sparsity Allocation problem, we uncover a U-shaped scaling law, demonstrating that a hybrid allocation of sparse capacity between MoE experts and Engram memory strictly outperforms pure MoE baselines.",
        "direct", SE_P2,
    ),
    atom(
        "A-CONC-SCALE27B", "result_sentence",
        "Guided by this law, we scale Engram to 27B parameters, achieving superior performance across diverse domains. Notably, while the memory module intuitively aids knowledge retrieval, we observe even larger gains in general reasoning, code, and mathematics.",
        "Guided by this law, we scale Engram to 27B parameters, achieving superior performance across diverse domains. Notably, while the memory module intuitively aids knowledge retrieval, we observe even larger gains in general reasoning, code, and mathematics.",
        "direct", SE_P2,
    ),
    # --- paragraph 3 (lines 347-349; mechanism spans the page break) ---
    atom(
        # raw_text spans the 347->349 page break: "...to focus" + blank line + "on global context...".
        # "deepen" keeps its source curly quotes in raw_text; normalized uses straight quotes.
        "A-CONC-MECH", "mechanism_sentence",
        "Our mechanistic analysis reveals that Engram effectively “deepen” the network by relieving early layers from static reconstruction tasks, thereby freeing up attention capacity to focus\n\non global context and complex reasoning.",
        "Our mechanistic analysis reveals that Engram effectively \"deepen\" the network by relieving early layers from static reconstruction tasks, thereby freeing up attention capacity to focus on global context and complex reasoning.",
        "direct", SE_P3,
    ),
    atom(
        "A-CONC-LONGCTX", "result_sentence",
        "This architectural shift translates into substantial improvements in long-context capabilities, as evidenced by performance gains in LongPPL and RULER.",
        "This architectural shift translates into substantial improvements in long-context capabilities, as evidenced by performance gains in LongPPL and RULER.",
        "direct", SE_P3,
    ),
    atom(
        "A-CONC-INFRA", "claim_sentence",
        "Finally, Engram advocates for infrastructure-aware efficiency as a first-class design principle. Its deterministic addressing allows for the decoupling of storage and compute, enabling the offloading of massive parameter tables to host memory with negligible inference overhead.",
        "Finally, Engram advocates for infrastructure-aware efficiency as a first-class design principle. Its deterministic addressing allows for the decoupling of storage and compute, enabling the offloading of massive parameter tables to host memory with negligible inference overhead.",
        "direct", SE_P3,
    ),
    atom(
        "A-CONC-VISION", "claim_sentence",
        "We envision conditional memory functions as an indispensable modeling primitive for next-generation sparse models.",
        "We envision conditional memory functions as an indispensable modeling primitive for next-generation sparse models.",
        "direct", SE_P3,
    ),
]

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# source_elements: heading + three paragraph elements (grouped by element id).
# ---------------------------------------------------------------------------
def build_source_elements(atoms):
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    elems = [OrderedDict([
        ("id", "SE-8-H"), ("type", "heading"),
        ("file", SOURCE_NAME), ("line_start", 341), ("line_end", 341),
    ])]
    for se_id, members in groups.items():
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        e = OrderedDict([
            ("id", se_id), ("type", "paragraph"),
            ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le),
        ])
        if se_id == SE_P3:
            e["note"] = "conclusion text spans a page break: mechanism claim breaks across lines 347-349"
        elems.append(e)
    return elems


SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ---------------------------------------------------------------------------
# section_tree
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-8"), ("path", "8"), ("title", "Conclusion")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks: ONE conclusion_block covering all 8 atoms.
# ---------------------------------------------------------------------------
SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C-CONCLUSION"),
        ("profile", "article_research"),
        ("chunk_type", "conclusion_block"),
        ("section_path", "8"),
        ("atom_ids", list(ATOM_IDS)),
        ("central_atom_ids", ["A-CONC-THESIS", "A-CONC-VISION"]),
        ("boundary_reason",
         "Single conclusion section between the related-work paragraphs and the References heading "
         "(source line 351, excluded); all claims summarize the paper and share one rhetorical unit, so "
         "they stay in one block. The cut is the heading_change at '# References' (qiefen Section 5.3)."),
        ("extraction_targets", ["ArticleClaim", "Implication", "DerivedRuleCandidate"]),
        ("gold_must_cover_atoms",
         ["A-CONC-THESIS", "A-CONC-ENGRAM", "A-CONC-USHAPE", "A-CONC-SCALE27B",
          "A-CONC-MECH", "A-CONC-INFRA", "A-CONC-VISION"]),
    ]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}

# ---------------------------------------------------------------------------
# objects: SAME ids/meanings as v0.1. home_package=PKG-CONCLUSION for all
# (single chunk -> supporting_context empty). gold_label/difficulty/evidence_strength.
# ---------------------------------------------------------------------------
def obj(oid, otype, payload, local, supporting, difficulty, evidence_strength):
    d = OrderedDict()
    d["id"] = oid
    d["type"] = otype
    d["section_path"] = "8"
    d["home_package"] = "PKG-CONCLUSION"
    d["payload"] = payload
    d["local_evidence_atom_ids"] = local
    d["supporting_context_atom_ids"] = supporting
    d["gold_label"] = True
    d["difficulty"] = difficulty
    d["evidence_strength"] = evidence_strength
    return d


OBJECTS = [
    # --- ArticleClaim (core thesis + summarized contributions) ---
    obj("CLAIM-CONDITIONAL-MEMORY", "ArticleClaim",
        OrderedDict([
            ("statement", "Conditional memory is a complementary sparsity axis to conditional computation (MoE), resolving the inefficiency of simulating knowledge retrieval through dynamic computation."),
            ("role", "core_thesis"),
        ]),
        ["A-CONC-THESIS"], [], "medium", "direct"),
    obj("CLAIM-ENGRAM-O1", "ArticleClaim",
        OrderedDict([
            ("statement", "Engram modernizes classic N-gram embeddings to provide scalable, constant-time O(1) lookups for static patterns."),
            ("instantiates", "conditional memory"),
        ]),
        ["A-CONC-ENGRAM"], [], "medium", "direct"),
    obj("CLAIM-USHAPE-HYBRID", "ArticleClaim",
        OrderedDict([
            ("statement", "A U-shaped scaling law over the Sparsity Allocation problem shows hybrid MoE+Engram allocation strictly outperforms pure MoE."),
            ("summarizes_chapter", "ch03 Sparsity Allocation"),
        ]),
        ["A-CONC-USHAPE"], [], "medium", "direct"),
    obj("CLAIM-SCALE-27B", "ArticleClaim",
        OrderedDict([
            ("statement", "Engram scales to 27B parameters with superior performance across diverse domains, with the largest gains in general reasoning, code, and mathematics."),
            ("summarizes_chapter", "ch04 Large-scale Pre-training"),
        ]),
        ["A-CONC-SCALE27B"], [], "medium", "direct"),
    obj("CLAIM-INFRA-EFFICIENCY", "ArticleClaim",
        OrderedDict([
            ("statement", "Engram's deterministic addressing decouples storage from compute, allowing massive parameter tables to be offloaded to host memory with negligible inference overhead."),
            ("role", "system_design_claim"),
            ("summarizes_chapter", "ch07 System Efficiency"),
        ]),
        ["A-CONC-INFRA"], [], "medium", "direct"),
    # --- MechanisticExplanation (summarized from ch06) ---
    obj("MECH-EFFECTIVE-DEPTH", "MechanisticExplanation",
        OrderedDict([
            ("mechanism", "Engram relieves early layers from static reconstruction, effectively deepening the network and freeing attention capacity for global context and complex reasoning."),
            ("observed_effect", "improved long-context capability (LongPPL, RULER)"),
            ("summarizes_chapter", "ch06 Analysis"),
        ]),
        ["A-CONC-MECH", "A-CONC-LONGCTX"], [], "hard", "indirect"),
    # --- Implication (future direction / vision) ---
    obj("IMPL-PRIMITIVE", "Implication",
        OrderedDict([
            ("statement", "Conditional memory should function as an indispensable modeling primitive for next-generation sparse models."),
            ("direction", "future_architecture"),
        ]),
        ["A-CONC-VISION"], [], "easy", "direct"),
    # --- DerivedRuleCandidate (actionable design rule implied by the U-shaped law) ---
    obj("RULE-HYBRID-ALLOCATION", "DerivedRuleCandidate",
        OrderedDict([
            ("candidate_rule", "When allocating sparse capacity in a large model, split it between MoE experts and conditional-memory (Engram) rather than committing fully to MoE, following the U-shaped allocation law."),
            ("derived_from", "U-shaped scaling law"),
        ]),
        ["A-CONC-USHAPE", "A-CONC-THESIS"], [], "hard", "indirect"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids. expected_objects =
# objects whose home_package is this package. Single chunk -> no cross-package
# objects -> no expected_local_fields.
# ---------------------------------------------------------------------------
CONTEXT_PACKAGES = [
    OrderedDict([
        ("id", "PKG-CONCLUSION"),
        ("profile", "article_research"),
        ("chunk_id", "C-CONCLUSION"),
        ("section_path", "Chapter 8 > 8 Conclusion"),
        ("document_title", "Engram: Conditional Memory as a Complementary Sparsity Axis"),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS["C-CONCLUSION"]]),
        ("linked_context", OrderedDict([
            ("previous_heading", "Mechanisms of Knowledge Storage (Related Work)"),
            ("next_heading", "References"),
            ("formula_context", "O(1) lookup; Sparsity Allocation ratio rho (defined in ch03)"),
        ])),
        ("extraction_targets", ["ArticleClaim", "Implication", "DerivedRuleCandidate"]),
        ("expected_objects", [o["id"] for o in OBJECTS if o["home_package"] == "PKG-CONCLUSION"]),
    ]),
]

# ---------------------------------------------------------------------------
# mentions (same 12 as v0.1; atom_ids unchanged).
# ---------------------------------------------------------------------------
MENTIONS = [
    OrderedDict([("id", "M-01"), ("text", "conditional memory"), ("type", "ArticleClaim"), ("atom_id", "A-CONC-THESIS"), ("canonical_key", "conditional_memory")]),
    OrderedDict([("id", "M-02"), ("text", "conditional computation (MoE)"), ("type", "ArticleClaim"), ("atom_id", "A-CONC-THESIS"), ("canonical_key", "moe_conditional_computation")]),
    OrderedDict([("id", "M-03"), ("text", "Engram"), ("type", "ArchitectureComponent"), ("atom_id", "A-CONC-ENGRAM"), ("canonical_key", "engram")]),
    OrderedDict([("id", "M-04"), ("text", "N-gram embeddings"), ("type", "ArticleClaim"), ("atom_id", "A-CONC-ENGRAM"), ("canonical_key", "ngram_embeddings")]),
    OrderedDict([("id", "M-05"), ("text", "Sparsity Allocation"), ("type", "ArticleClaim"), ("atom_id", "A-CONC-USHAPE"), ("canonical_key", "sparsity_allocation")]),
    OrderedDict([("id", "M-06"), ("text", "U-shaped scaling law"), ("type", "ScalingLaw"), ("atom_id", "A-CONC-USHAPE"), ("canonical_key", "u_shaped_scaling_law")]),
    OrderedDict([("id", "M-07"), ("text", "Engram-27B"), ("type", "ExperimentResult"), ("atom_id", "A-CONC-SCALE27B"), ("canonical_key", "engram_27b_result")]),
    OrderedDict([("id", "M-08"), ("text", "mechanistic analysis"), ("type", "MechanisticExplanation"), ("atom_id", "A-CONC-MECH"), ("canonical_key", "effective_depth_analysis")]),
    OrderedDict([("id", "M-09"), ("text", "LongPPL"), ("type", "ExperimentResult"), ("atom_id", "A-CONC-LONGCTX"), ("canonical_key", "longppl")]),
    OrderedDict([("id", "M-10"), ("text", "RULER"), ("type", "ExperimentResult"), ("atom_id", "A-CONC-LONGCTX"), ("canonical_key", "ruler")]),
    OrderedDict([("id", "M-11"), ("text", "deterministic addressing"), ("type", "SystemDesignClaim"), ("atom_id", "A-CONC-INFRA"), ("canonical_key", "deterministic_addressing")]),
    OrderedDict([("id", "M-12"), ("text", "host memory offloading"), ("type", "SystemDesignClaim"), ("atom_id", "A-CONC-INFRA"), ("canonical_key", "host_memory_offload")]),
]

# canonicalization: within-file only; notes flag the cross-chapter aliases
# (conclusion claims mirror earlier-chapter result objects) but do NOT merge
# across files.
CANONICALIZATION = [
    OrderedDict([("canonical", "u_shaped_scaling_law"),
                 ("aliases", ["U-shaped scaling law", "U-shaped relationship", "Sparsity Allocation law"]),
                 ("note", "Mirrors the ch03 (Sparsity Allocation) ScalingLaw object; cross-chapter alias recorded for downstream merge, but canonicalization here stays within-file (no new independent scaling-law fact).")]),
    OrderedDict([("canonical", "engram_27b_result"),
                 ("aliases", ["Engram-27B", "27B parameters", "superior performance across diverse domains"]),
                 ("note", "Mirrors the ch04 (Large-scale Pre-training) ExperimentResult object (cross-chapter alias).")]),
    OrderedDict([("canonical", "effective_depth_analysis"),
                 ("aliases", ["mechanistic analysis", "deepen the network", "relieving early layers"]),
                 ("note", "Mirrors the ch06 (Analysis / effective depth) MechanisticExplanation object (cross-chapter alias).")]),
    OrderedDict([("canonical", "longppl"),
                 ("aliases", ["LongPPL", "long-context perplexity"]),
                 ("note", "Mirrors the ch05 (Long Context Training) ExperimentResult object (cross-chapter alias).")]),
    OrderedDict([("canonical", "ruler"),
                 ("aliases", ["RULER"]),
                 ("note", "Mirrors the ch05 (Long Context Training) ExperimentResult object (cross-chapter alias).")]),
    OrderedDict([("canonical", "deterministic_addressing"),
                 ("aliases", ["deterministic addressing", "decoupling of storage and compute", "host memory offloading"]),
                 ("note", "Mirrors the ch07 / system-efficiency SystemDesignClaim object (cross-chapter alias).")]),
]

# ---------------------------------------------------------------------------
# relations: SAME 7 ids/meanings as v0.1, now gold_label/difficulty/evidence_strength.
# relation_types from the v0.3.3 vocabulary (claim_suggests_design_rule /
# result_supports_claim / mechanism_explains_result / system_design_enables_efficiency
# / claim_instantiated_by_method / claim_suggests_implication).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


RELATIONS = [
    rel("R-01", "claim_instantiated_by_method", "CLAIM-CONDITIONAL-MEMORY", "CLAIM-ENGRAM-O1",
        ["A-CONC-THESIS", "A-CONC-ENGRAM"], "medium", "direct"),
    rel("R-02", "result_supports_claim", "CLAIM-USHAPE-HYBRID", "CLAIM-CONDITIONAL-MEMORY",
        ["A-CONC-USHAPE"], "medium", "direct"),
    rel("R-03", "result_supports_claim", "CLAIM-SCALE-27B", "CLAIM-CONDITIONAL-MEMORY",
        ["A-CONC-SCALE27B"], "medium", "indirect"),
    rel("R-04", "mechanism_explains_result", "MECH-EFFECTIVE-DEPTH", "CLAIM-SCALE-27B",
        ["A-CONC-MECH", "A-CONC-LONGCTX"], "hard", "indirect"),
    rel("R-05", "claim_suggests_design_rule", "CLAIM-USHAPE-HYBRID", "RULE-HYBRID-ALLOCATION",
        ["A-CONC-USHAPE"], "hard", "indirect"),
    rel("R-06", "system_design_enables_efficiency", "CLAIM-ENGRAM-O1", "CLAIM-INFRA-EFFICIENCY",
        ["A-CONC-ENGRAM", "A-CONC-INFRA"], "medium", "direct"),
    rel("R-07", "claim_suggests_implication", "CLAIM-CONDITIONAL-MEMORY", "IMPL-PRIMITIVE",
        ["A-CONC-THESIS", "A-CONC-VISION"], "medium", "direct"),
]

# ---------------------------------------------------------------------------
# do_not_extract: the conclusion slice contains no inline citations or figures,
# but the inline-citation policy is recorded for consistency with other chapters.
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([
        ("pattern", "inline_author_year_citation"),
        ("examples", ["(Vaswani et al., 2017)", "(Xie et al., 2025)"]),
        ("reason", "inline author-year citations are not knowledge objects; the Conclusion slice itself contains none, but this policy entry suppresses citation over-extraction consistently with other chapters."),
        ("kind", "citation_policy"),
    ]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Chapter 8 (Conclusion) only"),
    ("title", "Engram: Conditional Memory as a Complementary Sparsity Axis - 8. Conclusion"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 341-349; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "article_research"),
    ("profile_cues", ["we introduce", "we instantiate", "U-shaped scaling law",
                      "mechanistic analysis", "we envision", "27B parameters"]),
    ("extraction_targets", ["ArticleClaim", "Implication", "DerivedRuleCandidate"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span: it may rephrase, transliterate math (O(1)), use straight quotes, and join the page-break newline, but MUST NOT introduce facts/names/section-refs not inside the span. EXCEPTION: a formula_atom MAY carry a symbolic interpretation supplied by atoms in metadata.supported_by_context_atoms / interpretation_supported_by."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks needed to fully define it). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only; supporting_context atoms are NOT required to be present in the home package's input."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone. Chapter 8 is a single chunk/package, so there are no cross-package objects and no expected_local_fields."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "figure atoms separate physical_section_id from semantic_section_ids; forward references set include_in_chapterN_core_chunks:false and are isolated in a cross_reference_block. The Conclusion contains no figures."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("canonicalization_scope", "Within-file only. Conclusion claims mirror earlier-chapter result/finding objects (ch03/04/05/06/07); these cross-chapter aliases are recorded in canonicalization notes for downstream merge but are NOT merged here, and no new independent facts are created in this chapter."),
        ("context_only", "an atom not consumed by any object or relation may be flagged metadata.context_only:true; the Conclusion has no such atoms (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "Chapter 8 spans source lines 341-349; '# References' (line 351) is the lower-boundary heading and is excluded.",
        "Source line 343 ends WITHOUT a terminal period ('...constant-time $O(1)$ lookups for static patterns'); A-CONC-ENGRAM raw_text therefore ends at 'static patterns' and the missing period must not be read as an unfinished paragraph.",
        "The mechanism claim (A-CONC-MECH) spans a PDF page break: source lines 347-349 are split into '...attention capacity to focus' / blank line / 'on global context...'. raw_text keeps the embedded '\\n\\n'; normalized_text joins it into one sentence.",
        "Inside line 343, $O(1)$ is inline math and is normalized to ASCII 'O(1)'.",
        "'deepen' appears with curly double quotes (U+201C/U+201D) in the source; raw_text keeps them verbatim and normalized_text uses straight ASCII quotes.",
        "Conclusion claims restate earlier-chapter results (Sparsity Allocation/U-shaped law -> ch03; 27B result -> ch04; long-context LongPPL/RULER -> ch05; mechanistic effective-depth -> ch06; deterministic addressing -> ch07). These are recorded as cross-chapter aliases in canonicalization; downstream cross-file merge should link them back rather than create parallel facts.",
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
    with open(os.path.join(HERE, "source.md"), "w", encoding="utf-8") as f:
        f.write(VIEWER_TEXT)
    header = (
        "# Gold fixture v0.3.3 - Engram paper Chapter 8 Conclusion (profile: article_research)\n"
        "# Upgraded from v0.1: SAME atom/chunk/object/relation IDs and meanings, now re-expressed in the\n"
        "# v0.3.3 schema with verbatim spans -- dual coordinates (source_span authoritative + viewer_span),\n"
        "# raw/normalized discipline, object evidence split into local/supporting + home_package,\n"
        "# ONE context_package per conclusion_block with expected_objects, gold_label/difficulty/\n"
        "# evidence_strength (no decimal confidence), do_not_extract citation policy, auto source_elements.\n"
        "# Quirks: line 343 lacks a terminal period; A-CONC-MECH spans the 347-349 page break; $O(1)$ and\n"
        "# curly-quoted \"deepen\" are kept verbatim in raw_text. Spans computed by build.py; do NOT hand-edit.\n"
    )
    body = yaml.dump(GOLD, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=10000)
    with open(os.path.join(HERE, "gold.yaml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    print("wrote source.md (%d bytes) and gold.yaml" % len(VIEWER_TEXT))
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d mentions=%d" % (
        len(ATOMS), len(SOURCE_ELEMENTS), len(SEMANTIC_CHUNKS),
        len(CONTEXT_PACKAGES), len(OBJECTS), len(RELATIONS), len(MENTIONS)))


if __name__ == "__main__":
    main()
