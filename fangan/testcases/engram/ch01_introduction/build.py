#!/usr/bin/env python3
"""Builder for ch01_introduction gold.yaml (schema v0.3.3, profile article_research).

Upgrades the v0.1 fixture to v0.3.3 WITHOUT changing the meaning or IDs of any
atom/chunk/object/relation; it only re-expresses them in the v0.3.3 schema and
adds verbatim spans.

All spans are computed by locating verbatim substrings (anchors) in the
authoritative MinerU source file; YAML spans are NEVER hand-written.

Authoritative source : engram_paper_mineru.md lines 13-30 (1. Introduction).
Viewer slice         : source.md (verbatim copy of those lines + header comment).

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

# Authoritative slice: the whole Chapter-1 Introduction block.
SLICE_LINE_START = 13
SLICE_LINE_END = 30

# ---------------------------------------------------------------------------
# Load source; build the viewer slice.
# ---------------------------------------------------------------------------
SRC = open(SOURCE_FILE, encoding="utf-8").read()
SRC_LINES = SRC.split("\n")

VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY


# ---------------------------------------------------------------------------
# Char-offset helpers for both files.
# ---------------------------------------------------------------------------
def line_starts(text):
    """Map 1-based line number -> char offset where that line begins."""
    offs = {}
    acc = 0
    for i, ln in enumerate(text.split("\n"), start=1):
        offs[i] = acc
        acc += len(ln) + 1
    return offs

SRC_LINE_OFF = line_starts(SRC)
VIEWER_LINE_OFF = line_starts(VIEWER_TEXT)


def line_of_offset(text, off):
    """1-based line number containing char offset `off`."""
    return text.count("\n", 0, off) + 1


def span_for(raw):
    """raw must be a verbatim, unique, contiguous substring of the slice.

    Returns (source_span, viewer_span) computed by anchor lookup. Authoritative
    coordinates index engram_paper_mineru.md; viewer coordinates index source.md.
    """
    # locate authoritatively inside the slice region of the source file
    slice_src_start = SRC_LINE_OFF[SLICE_LINE_START]
    slice_src_end = (SRC_LINE_OFF[SLICE_LINE_END]
                     + len(SRC_LINES[SLICE_LINE_END - 1]) + 1)
    region = SRC[slice_src_start:slice_src_end]
    idx = region.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND in slice: %r" % raw[:60])
    if region.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS substring (>1 match in slice): %r" % raw[:60])

    src_cs = slice_src_start + idx
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch for %r" % raw[:40]

    # viewer: same substring inside source.md (header offsets shift it)
    v_idx = VIEWER_TEXT.find(raw)
    assert v_idx >= 0 and VIEWER_TEXT.find(raw, v_idx + 1) < 0, "viewer locate"
    v_cs = v_idx
    v_ce = v_cs + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch for %r" % raw[:40]

    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", line_of_offset(SRC, src_cs)),
        ("line_end", line_of_offset(SRC, src_ce - 1)),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", line_of_offset(VIEWER_TEXT, v_cs)),
        ("line_end", line_of_offset(VIEWER_TEXT, v_ce - 1)),
        ("char_start", v_cs), ("char_end", v_ce),
        ("viewer_only", True),
    ])
    return source_span, viewer_span


def atom(aid, atom_type, raw, normalized, evidence_strength, se_id, metadata=None):
    ss, vs = span_for(raw)
    d = OrderedDict()
    d["id"] = aid
    d["section_id"] = "SEC-1"
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
# Evidence atoms.  raw_text = verbatim slice substrings (preserving in-line
# citations, em-dashes, inline math $O(1)$, the 'knowl- / edge' page-break).
# normalized_text renders EXACTLY that span (ascii; $O(1)$ -> O(1); em-dash -> -;
# 'knowl- edge'/'knowl-\n\nedge' -> 'knowledge'; resolve in-span pronouns) and
# introduces NO out-of-span facts/names/section-refs. Inline (Author, year)
# citations are kept verbatim as evidence but are negatives (see do_not_extract).
#
# Each paragraph is one source_element. SE ids group atoms by physical paragraph.
# ---------------------------------------------------------------------------
SE_P15 = "SE-1-P15"   # line 15 (sparsity/MoE problem framing)
SE_P17 = "SE-1-P17"   # lines 17-19 (linguistic duality; page-break paragraph)
SE_P21 = "SE-1-P21"   # line 21 (conditional memory thesis + Engram)
SE_P23 = "SE-1-P23"   # line 23 (Sparsity Allocation -> 27B -> gains)
SE_P25 = "SE-1-P25"   # line 25 (mechanistic + long context)
SE_P27 = "SE-1-P27"   # line 27 (infra-aware efficiency)
SE_FIG1 = "SE-1-FIG1"  # line 29 (Figure 1 caption)

ATOMS = [
    # ---------- problem framing: sparsity / MoE ----------
    atom(
        "A-SPARSITY-PRINCIPLE", "claim_sentence",
        "Sparsity is a recurring design principle for intelligent systems, spanning from biological neural circuits (Lennie, 2003; Olshausen and Field, 1997) to modern Large Language Models (LLMs). Currently, this principle is primarily realized through Mixture-of-Experts (MoE) (Dai et al., 2024; Shazeer et al., 2017), which scales capacity via conditional computation.",
        "Sparsity is a recurring design principle for intelligent systems, spanning from biological neural circuits to modern Large Language Models (LLMs); currently this principle is primarily realized through Mixture-of-Experts (MoE), which scales capacity via conditional computation.",
        "direct", SE_P15,
    ),
    atom(
        "A-MOE-DEFACTO", "claim_sentence",
        "Owing to its ability to drastically increase model size without proportional increases in compute, MoE has become the de facto standard for frontier models (Comanici et al., 2025; Guo et al., 2025; Team et al., 2025).",
        "Owing to its ability to drastically increase model size without proportional increases in compute, MoE has become the de facto standard for frontier models.",
        "direct", SE_P15,
    ),
    # ---------- linguistic heterogeneity / duality ----------
    atom(
        "A-LINGUISTIC-DUALITY", "claim_sentence",
        "Specifically, language modeling entails two qualitatively different sub-tasks: compositional reasoning and knowl-\n\nedge retrieval. While the former demands deep, dynamic computation, a substantial portion of text—such as named entities and formulaic patterns—is local, static, and highly stereotyped (Constant et al., 2017; Erman, 2000).",
        "Specifically, language modeling entails two qualitatively different sub-tasks: compositional reasoning and knowledge retrieval. While the former demands deep, dynamic computation, a substantial portion of text - such as named entities and formulaic patterns - is local, static, and highly stereotyped.",
        "direct", SE_P17,
    ),
    atom(
        "A-NGRAM-LOCAL", "mechanism_sentence",
        "The effectiveness of classical N-gram models (Brants et al., 2007; Liu et al., 2024b; Nguyen, 2024) in capturing such local dependencies implies that these regularities are naturally represented as computationally inexpensive lookups.",
        "The effectiveness of classical N-gram models in capturing such local dependencies implies that these regularities are naturally represented as computationally inexpensive lookups.",
        "direct", SE_P17,
    ),
    # ---------- Transformer lacks lookup primitive -> simulate retrieval ----------
    atom(
        "A-NO-LOOKUP-PRIMITIVE", "claim_sentence",
        "Since standard Transformers (Vaswani et al., 2017) lack a native knowledge lookup primitive, current LLMs are forced to simulate retrieval through computation.",
        "Since standard Transformers lack a native knowledge lookup primitive, current LLMs are forced to simulate retrieval through computation.",
        "direct", SE_P17,
    ),
    atom(
        "A-RECONSTRUCTION-WASTE", "mechanism_sentence",
        "For instance, resolving a common multi-token entity requires consuming multiple early layers of attention and feed-forward networks (Ghandeharioun et al., 2024; Jin et al., 2025) (see Table 3). This process essentially amounts to an expensive runtime reconstruction of a static lookup table, wasting valuable sequential depth on trivial operations that could otherwise be allocated to higher-level reasoning.",
        "For instance, resolving a common multi-token entity requires consuming multiple early layers of attention and feed-forward networks (see Table 3). This process essentially amounts to an expensive runtime reconstruction of a static lookup table, wasting valuable sequential depth on trivial operations that could otherwise be allocated to higher-level reasoning.",
        "direct", SE_P17,
    ),
    # ---------- proposal: conditional memory + Engram ----------
    atom(
        "A-CONDITIONAL-MEMORY", "claim_sentence",
        "To align model architecture with this linguistic duality, we advocate for a complementary axis of sparsity: conditional memory. Whereas conditional computation sparsely activates parameters to process dynamic logic (Bengio et al., 2013; Shazeer et al., 2017), conditional memory relies on sparse lookup operations to retrieve static embeddings for fixed knowledge.",
        "To align model architecture with this linguistic duality, we advocate for a complementary axis of sparsity: conditional memory. Whereas conditional computation sparsely activates parameters to process dynamic logic, conditional memory relies on sparse lookup operations to retrieve static embeddings for fixed knowledge.",
        "direct", SE_P21,
    ),
    atom(
        "A-NGRAM-O1-LOOKUP", "mechanism_sentence",
        "As a preliminary exploration of this paradigm, we revisit N-gram embeddings (Bojanowski et al., 2017) as a canonical instantiation: local context serves as a key to index a massive embedding table via constant-time $O(1)$ lookups (Huang et al., 2025a; Pagnoni et al., 2025; Tito Svenstrup et al., 2017; Yu et al., 2025). Our investigation reveals that, perhaps surprisingly, this static retrieval mechanism can serve as an ideal complement to modern MoE architecture—but only if it is properly designed.",
        "As a preliminary exploration of this paradigm, we revisit N-gram embeddings as a canonical instantiation: local context serves as a key to index a massive embedding table via constant-time O(1) lookups. Our investigation reveals that, perhaps surprisingly, this static retrieval mechanism can serve as an ideal complement to modern MoE architecture - but only if it is properly designed.",
        "direct", SE_P21,
    ),
    atom(
        "A-ENGRAM-METHOD", "method_sentence",
        "In this paper, we propose Engram, a conditional memory module grounded in the classic N-gram structure but equipped with modern adaptations such as tokenizer compression, multi-head hashing, contextualized gating, and multi-branch integration (detailed in Section 2).",
        "In this paper, we propose Engram, a conditional memory module grounded in the classic N-gram structure but equipped with modern adaptations such as tokenizer compression, multi-head hashing, contextualized gating, and multi-branch integration (detailed in Section 2).",
        "direct", SE_P21,
    ),
    # ---------- Sparsity Allocation + U-shaped law + scale + gains ----------
    atom(
        "A-SPARSITY-ALLOCATION", "method_sentence",
        "To quantify the synergy between these two primitives, we formulate the Sparsity Allocation problem: given a fixed total parameter budget, how should capacity be distributed between MoE experts and Engram memory?",
        "To quantify the synergy between these two primitives, we formulate the Sparsity Allocation problem: given a fixed total parameter budget, how should capacity be distributed between MoE experts and Engram memory?",
        "direct", SE_P23,
    ),
    atom(
        "A-USHAPED-LAW", "scaling_law_result_atom",
        "Our experiments uncover a distinct U-shaped scaling law, revealing that even simple lookup mechanisms, when treated as a first-class modeling primitive, act as essential complements to neural computation.",
        "Our experiments uncover a distinct U-shaped scaling law, revealing that even simple lookup mechanisms, when treated as a first-class modeling primitive, act as essential complements to neural computation.",
        "direct", SE_P23,
    ),
    atom(
        "A-SCALE-27B", "result_sentence",
        "Guided by this allocation law, we scale Engram to a 27B-parameter model. Compared to a strictly iso-parameter and iso-FLOPs MoE baseline, Engram-27B achieves superior efficiency across diverse domains.",
        "Guided by this allocation law, we scale Engram to a 27B-parameter model; compared to a strictly iso-parameter and iso-FLOPs MoE baseline, Engram-27B achieves superior efficiency across diverse domains.",
        "direct", SE_P23,
    ),
    atom(
        "A-GAINS-PREVIEW", "result_sentence",
        "Crucially, the gains are not limited to knowledge-intensive tasks (e.g., MMLU: +3.4; CMMLU: +4.0; MMLU-Pro: +1.8), where memory capacity is intuitively beneficial; we observe even more significant improvements in general reasoning (e.g., BBH: +5.0; ARC-Challenge: +3.7; DROP: +3.3) and code/math domains (e.g., HumanEval: +3.0; MATH: +2.4; GSM8K: +2.2).",
        "Crucially, the gains are not limited to knowledge-intensive tasks (e.g., MMLU: +3.4; CMMLU: +4.0; MMLU-Pro: +1.8), where memory capacity is intuitively beneficial; we observe even more significant improvements in general reasoning (e.g., BBH: +5.0; ARC-Challenge: +3.7; DROP: +3.3) and code/math domains (e.g., HumanEval: +3.0; MATH: +2.4; GSM8K: +2.2).",
        "direct", SE_P23,
    ),
    # ---------- mechanism + long context ----------
    atom(
        "A-MECH-EARLY-LAYERS", "mechanism_sentence",
        "Mechanistic analysis via LogitLens (nostalgebraist, 2020) and CKA (Hendrycks et al., 2021a) reveals the source of these gains: Engram relieves the backbone from reconstructing static knowledge in early layers, thereby increasing effective depth available for complex reasoning.",
        "Mechanistic analysis via LogitLens and CKA reveals the source of these gains: Engram relieves the backbone from reconstructing static knowledge in early layers, thereby increasing effective depth available for complex reasoning.",
        "direct", SE_P25,
    ),
    atom(
        "A-LONGCONTEXT", "result_sentence",
        "Furthermore, by delegating local dependencies to lookups, Engram frees up attention capacity to focus on global context, enabling exceptional performance in long-context scenarios—substantially outperforming baselines on LongPPL (Fang et al.) and RULER (Hsieh et al.) (e.g., Multi-Query NIAH: 97.0 vs. 84.2; Variable Tracking: 89.0 vs. 77.0).",
        "Furthermore, by delegating local dependencies to lookups, Engram frees up attention capacity to focus on global context, enabling exceptional performance in long-context scenarios - substantially outperforming baselines on LongPPL and RULER (e.g., Multi-Query NIAH: 97.0 vs. 84.2; Variable Tracking: 89.0 vs. 77.0).",
        "direct", SE_P25,
    ),
    # ---------- infra-aware efficiency ----------
    atom(
        "A-INFRA-PREFETCH", "method_sentence",
        "Finally, we establish infrastructure-aware efficiency as a first-class principle. Unlike MoE's dynamic routing, Engram employs deterministic IDs to enable runtime prefetching, overlapping communication with computation.",
        "Finally, we establish infrastructure-aware efficiency as a first-class principle. Unlike MoE's dynamic routing, Engram employs deterministic IDs to enable runtime prefetching, overlapping communication with computation.",
        "direct", SE_P27,
    ),
    atom(
        "A-INFRA-RESULT", "result_sentence",
        "Empirical results show that offloading a 100B-parameter table to host memory incurs negligible overhead (< 3%). This demonstrates that Engram effectively bypasses GPU memory constraints, facilitating aggressive parameter expansion.",
        "Empirical results show that offloading a 100B-parameter table to host memory incurs negligible overhead (< 3%). This demonstrates that Engram effectively bypasses GPU memory constraints, facilitating aggressive parameter expansion.",
        "direct", SE_P27,
    ),
    # ---------- Figure 1 caption ----------
    atom(
        "A-FIG1-CAPTION", "figure_caption_atom",
        "Figure 1 | The Engram Architecture. The module augments the backbone by retrieving static N-gram memory and fusing it with dynamic hidden states via context-aware gating. This module is applied only to specific layers to decouple memory from compute, leaving the standard input embedding and un-embedding module intact.",
        "Figure 1 | The Engram Architecture. The module augments the backbone by retrieving static N-gram memory and fusing it with dynamic hidden states via context-aware gating. This module is applied only to specific layers to decouple memory from compute, leaving the standard input embedding and un-embedding module intact.",
        "direct", SE_FIG1,
        metadata=OrderedDict([
            ("figure", "Figure 1"),
            ("physical_section_id", "SEC-1"),
            ("semantic_section_ids", ["SEC-1"]),
        ]),
    ),
]

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# source_elements: one heading + one paragraph element per physical paragraph,
# auto-derived from the atoms' source_element_id and their source-line spans.
# ---------------------------------------------------------------------------
def build_source_elements(atoms):
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    elems = [OrderedDict([
        ("id", "SE-1-H"), ("type", "heading"),
        ("file", SOURCE_NAME), ("line_start", 13), ("line_end", 13),
    ])]
    notes = {
        "SE-1-P17": "linguistic-duality paragraph; MinerU split it across a page break "
                    "(lines 17 and 19, blank line 18) with a hyphenated word 'knowl- / edge'.",
    }
    for se_id, members in groups.items():
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        etype = "figure_caption" if se_id == "SE-1-FIG1" else "paragraph"
        d = OrderedDict([
            ("id", se_id), ("type", etype),
            ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le),
        ])
        if se_id in notes:
            d["note"] = notes[se_id]
        elems.append(d)
    return elems

SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ---------------------------------------------------------------------------
# section_tree (unchanged: single Introduction node).
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-1"), ("path", "1"), ("title", "Introduction")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks (same 4 chunks / atom membership as v0.1).
# ---------------------------------------------------------------------------
SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C-PROBLEM"),
        ("profile", "article_research"),
        ("chunk_type", "article_core_claim_block"),
        ("section_path", "1"),
        ("atom_ids", ["A-SPARSITY-PRINCIPLE", "A-MOE-DEFACTO", "A-LINGUISTIC-DUALITY",
                      "A-NGRAM-LOCAL", "A-NO-LOOKUP-PRIMITIVE", "A-RECONSTRUCTION-WASTE"]),
        ("central_atom_ids", ["A-NO-LOOKUP-PRIMITIVE", "A-RECONSTRUCTION-WASTE"]),
        ("boundary_reason",
         "problem framing: sparsity principle + MoE + linguistic duality + Transformers lack a "
         "lookup primitive and waste early-layer depth reconstructing static tables; kept together "
         "as one motivation block (qiefen 5.3 claim-evidence continuity)."),
        ("extraction_targets", ["ArticleClaim", "MechanisticExplanation"]),
        ("gold_must_cover_atoms", ["A-NO-LOOKUP-PRIMITIVE", "A-RECONSTRUCTION-WASTE", "A-LINGUISTIC-DUALITY"]),
    ]),
    OrderedDict([
        ("id", "C-METHOD"),
        ("profile", "article_research"),
        ("chunk_type", "article_core_claim_block"),
        ("section_path", "1"),
        ("atom_ids", ["A-CONDITIONAL-MEMORY", "A-NGRAM-O1-LOOKUP", "A-ENGRAM-METHOD", "A-FIG1-CAPTION"]),
        ("central_atom_ids", ["A-CONDITIONAL-MEMORY", "A-ENGRAM-METHOD"]),
        ("boundary_reason",
         "thesis + method overview: conditional memory as a complementary sparsity axis, instantiated "
         "via Engram (4 adaptations); the Figure 1 caption is the architecture overview and stays with "
         "the method (qiefen 5.3)."),
        ("extraction_targets", ["ArticleClaim", "ArticleMethod", "ArchitectureComponent"]),
        ("gold_must_cover_atoms", ["A-CONDITIONAL-MEMORY", "A-ENGRAM-METHOD", "A-NGRAM-O1-LOOKUP"]),
    ]),
    OrderedDict([
        ("id", "C-ALLOCATION"),
        ("profile", "article_research"),
        ("chunk_type", "scaling_law_block"),
        ("section_path", "1"),
        ("atom_ids", ["A-SPARSITY-ALLOCATION", "A-USHAPED-LAW", "A-SCALE-27B", "A-GAINS-PREVIEW"]),
        ("central_atom_ids", ["A-USHAPED-LAW", "A-GAINS-PREVIEW"]),
        ("boundary_reason",
         "Sparsity Allocation problem -> U-shaped law -> scale to 27B -> headline gains; the allocation "
         "question and its result + scaled-up evidence kept contiguous (qiefen 5.3 setup-result continuity)."),
        ("extraction_targets", ["ScalingLaw", "ExperimentResult"]),
        ("gold_must_cover_atoms", ["A-SPARSITY-ALLOCATION", "A-USHAPED-LAW", "A-SCALE-27B", "A-GAINS-PREVIEW"]),
    ]),
    OrderedDict([
        ("id", "C-MECH-EFF"),
        ("profile", "article_research"),
        ("chunk_type", "system_efficiency_block"),
        ("section_path", "1"),
        ("atom_ids", ["A-MECH-EARLY-LAYERS", "A-LONGCONTEXT", "A-INFRA-PREFETCH", "A-INFRA-RESULT"]),
        ("central_atom_ids", ["A-MECH-EARLY-LAYERS", "A-INFRA-PREFETCH"]),
        ("boundary_reason",
         "why-it-works previews: mechanistic (effective depth) + long-context (attention freed) + "
         "infra-aware efficiency (deterministic prefetch, <3% overhead); all explain/quantify the same "
         "contribution rather than introducing a new method."),
        ("extraction_targets", ["MechanisticExplanation", "SystemDesignClaim", "ExperimentResult"]),
        ("gold_must_cover_atoms", ["A-MECH-EARLY-LAYERS", "A-LONGCONTEXT", "A-INFRA-PREFETCH", "A-INFRA-RESULT"]),
    ]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}
# atom_id -> chunk_id
ATOM_CHUNK = {}
for c in SEMANTIC_CHUNKS:
    for aid in c["atom_ids"]:
        ATOM_CHUNK[aid] = c["id"]
# chunk_id -> package_id
CHUNK_PKG = {"C-PROBLEM": "PKG-PROBLEM", "C-METHOD": "PKG-METHOD",
             "C-ALLOCATION": "PKG-ALLOCATION", "C-MECH-EFF": "PKG-MECH-EFF"}

# ---------------------------------------------------------------------------
# objects.  Same IDs / meaning as v0.1; drop confidence; add gold_label /
# difficulty / evidence_strength + local_evidence_atom_ids /
# supporting_context_atom_ids / home_package.
#
# home_package = the package of the chunk holding the object's central evidence.
# local_evidence = evidence atoms inside that chunk; supporting_context = evidence
# atoms from OTHER chunks. (v0.1 evidence_atom_ids = local ∪ supporting.)
# ---------------------------------------------------------------------------
def split_evidence(home_chunk, all_evidence):
    home_atoms = set(CHUNK_ATOMS[home_chunk])
    local = [a for a in all_evidence if a in home_atoms]
    supporting = [a for a in all_evidence if a not in home_atoms]
    return local, supporting


def obj(oid, otype, home_pkg, payload, all_evidence, difficulty, evidence_strength):
    home_chunk = next(c for c, p in CHUNK_PKG.items() if p == home_pkg)
    local, supporting = split_evidence(home_chunk, all_evidence)
    assert local, "%s has no local evidence in %s" % (oid, home_chunk)
    d = OrderedDict()
    d["id"] = oid
    d["type"] = otype
    d["section_path"] = "1"
    d["home_package"] = home_pkg
    d["payload"] = payload
    d["local_evidence_atom_ids"] = local
    d["supporting_context_atom_ids"] = supporting
    d["gold_label"] = True
    d["difficulty"] = difficulty
    d["evidence_strength"] = evidence_strength
    return d

OBJECTS = [
    # --- ArticleClaim ---
    obj("CLAIM-PROBLEM", "ArticleClaim", "PKG-PROBLEM",
        OrderedDict([
            ("statement", "Transformers lack a native knowledge lookup primitive, forcing LLMs to simulate retrieval through computation; resolving multi-token entities wastes early-layer depth on runtime reconstruction of a static lookup table."),
            ("role", "problem_statement"),
            ("supporting_evidence", "linguistic duality: compositional reasoning vs static/local stereotyped patterns (N-grams)"),
        ]),
        ["A-NO-LOOKUP-PRIMITIVE", "A-RECONSTRUCTION-WASTE", "A-LINGUISTIC-DUALITY", "A-NGRAM-LOCAL"],
        "medium", "direct"),
    obj("CLAIM-CONDITIONAL-MEMORY", "ArticleClaim", "PKG-METHOD",
        OrderedDict([
            ("statement", "Conditional memory is a complementary axis of sparsity to conditional computation (MoE): sparse lookups retrieve static embeddings for fixed knowledge, serving as an ideal complement to MoE when properly designed."),
            ("role", "thesis"),
        ]),
        ["A-CONDITIONAL-MEMORY", "A-NGRAM-O1-LOOKUP"],
        "medium", "direct"),
    obj("CLAIM-MOE-PRIOR", "ArticleClaim", "PKG-PROBLEM",
        OrderedDict([
            ("statement", "Sparsity is realized in current LLMs primarily through MoE / conditional computation, the de facto standard for frontier models."),
            ("role", "prior_work_baseline"),
        ]),
        ["A-SPARSITY-PRINCIPLE", "A-MOE-DEFACTO"],
        "easy", "direct"),
    # --- ArticleMethod ---
    obj("METHOD-ENGRAM", "ArticleMethod", "PKG-METHOD",
        OrderedDict([
            ("name", "Engram"),
            ("description", "A conditional memory module grounded in the classic N-gram structure with modern adaptations; indexes a massive embedding table via O(1) lookups and fuses with hidden states."),
            ("components", ["tokenizer compression", "multi-head hashing", "contextualized gating", "multi-branch integration"]),
            ("detailed_in", "Section 2"),
        ]),
        ["A-ENGRAM-METHOD", "A-NGRAM-O1-LOOKUP", "A-FIG1-CAPTION"],
        "medium", "direct"),
    obj("METHOD-SPARSITY-ALLOCATION", "ArticleMethod", "PKG-ALLOCATION",
        OrderedDict([
            ("name", "Sparsity Allocation problem"),
            ("description", "Given a fixed total parameter budget, distribute capacity between MoE experts and Engram memory."),
            ("outcome", "yields the U-shaped scaling law and guides scaling to 27B"),
        ]),
        ["A-SPARSITY-ALLOCATION"],
        "medium", "direct"),
    # --- ArchitectureComponent ---
    obj("COMPONENT-CONTEXT-GATING", "ArchitectureComponent", "PKG-METHOD",
        OrderedDict([
            ("name", "contextualized (context-aware) gating"),
            ("role", "dynamically modulate retrieved static embeddings by the current hidden state during fusion."),
        ]),
        ["A-ENGRAM-METHOD", "A-FIG1-CAPTION"],
        "medium", "direct"),
    # --- ScalingLaw ---
    obj("SCALINGLAW-USHAPED", "ScalingLaw", "PKG-ALLOCATION",
        OrderedDict([
            ("name", "U-shaped sparsity-allocation scaling law"),
            ("finding", "Optimal trade-off between neural computation (MoE) and static memory (Engram) is U-shaped; even simple lookups, as a first-class primitive, are essential complements to computation."),
        ]),
        ["A-USHAPED-LAW"],
        "medium", "direct"),
    # --- ExperimentResult ---
    obj("RESULT-27B-GAINS", "ExperimentResult", "PKG-ALLOCATION",
        OrderedDict([
            ("setting", "Engram-27B vs strictly iso-parameter and iso-FLOPs MoE baseline"),
            ("knowledge", "MMLU +3.4; CMMLU +4.0; MMLU-Pro +1.8"),
            ("reasoning", "BBH +5.0; ARC-Challenge +3.7; DROP +3.3"),
            ("code_math", "HumanEval +3.0; MATH +2.4; GSM8K +2.2"),
        ]),
        ["A-SCALE-27B", "A-GAINS-PREVIEW"],
        "medium", "direct"),
    obj("RESULT-LONGCONTEXT", "ExperimentResult", "PKG-MECH-EFF",
        OrderedDict([
            ("setting", "long-context retrieval on LongPPL and RULER"),
            ("numbers", "Multi-Query NIAH 97.0 vs 84.2; Variable Tracking 89.0 vs 77.0"),
        ]),
        ["A-LONGCONTEXT"],
        "medium", "direct"),
    obj("RESULT-INFRA", "ExperimentResult", "PKG-MECH-EFF",
        OrderedDict([
            ("setting", "offloading a 100B-parameter table to host memory"),
            ("numbers", "negligible overhead (< 3%)"),
        ]),
        ["A-INFRA-RESULT"],
        "medium", "direct"),
    # --- MechanisticExplanation ---
    obj("MECH-EFFECTIVE-DEPTH", "MechanisticExplanation", "PKG-MECH-EFF",
        OrderedDict([
            ("mechanism", "Engram relieves the backbone from reconstructing static knowledge in early layers (shown via LogitLens and CKA), increasing effective depth available for complex reasoning."),
        ]),
        ["A-MECH-EARLY-LAYERS"],
        "medium", "direct"),
    obj("MECH-ATTENTION-FREED", "MechanisticExplanation", "PKG-MECH-EFF",
        OrderedDict([
            ("mechanism", "Delegating local dependencies to lookups frees attention capacity to focus on global context, improving long-context retrieval."),
        ]),
        ["A-LONGCONTEXT"],
        "medium", "direct"),
    # --- SystemDesignClaim ---
    obj("SYS-PREFETCH", "SystemDesignClaim", "PKG-MECH-EFF",
        OrderedDict([
            ("claim", "Engram's deterministic IDs enable runtime prefetching that overlaps communication with computation (unlike MoE dynamic routing), establishing infrastructure-aware efficiency and bypassing GPU memory constraints."),
        ]),
        ["A-INFRA-PREFETCH", "A-INFRA-RESULT"],
        "medium", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]
OBJ_BY_ID = {o["id"]: o for o in OBJECTS}

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids. expected_objects =
# objects whose home_package is this package. expected_local_fields for any
# cross-package object whose supporting evidence is in this package's chunk.
# ---------------------------------------------------------------------------
DOC_TITLE = "Conditional Memory via Scalable Lookup: A New Axis of Sparsity for LLMs"

PKG_META = {
    "PKG-PROBLEM": OrderedDict([
        ("section_path", "Engram > 1. Introduction > problem framing (sparsity / MoE / linguistic duality)"),
        ("linked_context", OrderedDict([
            ("previous_heading", "(document Abstract)"),
            ("next_heading", "1. Introduction (proposal of conditional memory / Engram)"),
        ])),
        ("extraction_targets", ["ArticleClaim", "MechanisticExplanation"]),
    ]),
    "PKG-METHOD": OrderedDict([
        ("section_path", "Engram > 1. Introduction > proposal of conditional memory / Engram"),
        ("linked_context", OrderedDict([
            ("figure_context", "Figure 1 | The Engram Architecture (retrieval + context-aware gating fusion, applied to specific layers)"),
            ("previous_heading", "1. Introduction (problem framing)"),
            ("next_heading", "2. Architecture (Section 2 detail)"),
        ])),
        ("extraction_targets", ["ArticleClaim", "ArticleMethod", "ArchitectureComponent"]),
    ]),
    "PKG-ALLOCATION": OrderedDict([
        ("section_path", "Engram > 1. Introduction > Sparsity Allocation / scaling to 27B"),
        ("linked_context", OrderedDict([
            ("previous_heading", "1. Introduction (proposal of conditional memory / Engram)"),
            ("next_heading", "1. Introduction (mechanism / long-context / infra-aware efficiency)"),
        ])),
        ("extraction_targets", ["ScalingLaw", "ExperimentResult", "ArticleMethod"]),
    ]),
    "PKG-MECH-EFF": OrderedDict([
        ("section_path", "Engram > 1. Introduction > mechanism / long-context / infra-aware efficiency"),
        ("linked_context", OrderedDict([
            ("previous_heading", "1. Introduction (Sparsity Allocation / scaling to 27B)"),
            ("next_heading", "2. Architecture"),
        ])),
        ("extraction_targets", ["MechanisticExplanation", "SystemDesignClaim", "ExperimentResult"]),
    ]),
}


def build_package(pid, chunk_id):
    meta = PKG_META[pid]
    home_objs = [o["id"] for o in OBJECTS if o["home_package"] == pid]
    # expected_local_fields: cross-package objects whose supporting evidence lands
    # in THIS chunk -> the payload fields derivable from this package's atoms alone.
    chunk_atoms = set(CHUNK_ATOMS[chunk_id])
    elf = OrderedDict()
    for o in OBJECTS:
        if o["home_package"] == pid:
            continue
        if set(o["supporting_context_atom_ids"]) & chunk_atoms:
            # only the gating component is cross-package supported here; declare
            # the fields its local-here atoms can yield.
            if o["id"] == "COMPONENT-CONTEXT-GATING":
                elf[o["id"]] = ["name"]
    d = OrderedDict([
        ("id", pid),
        ("profile", "article_research"),
        ("chunk_id", chunk_id),
        ("section_path", meta["section_path"]),
        ("document_title", DOC_TITLE),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", meta["linked_context"]),
        ("extraction_targets", meta["extraction_targets"]),
        ("expected_objects", home_objs),
    ])
    if elf:
        d["expected_local_fields"] = elf
    return d

CONTEXT_PACKAGES = [build_package(CHUNK_PKG[c["id"]], c["id"]) for c in SEMANTIC_CHUNKS]

# ---------------------------------------------------------------------------
# mentions (same 12 as v0.1).
# ---------------------------------------------------------------------------
def men(mid, text, mtype, atom_id, key):
    return OrderedDict([("id", mid), ("text", text), ("type", mtype),
                        ("atom_id", atom_id), ("canonical_key", key)])

MENTIONS = [
    men("M-01", "Sparsity", "Concept", "A-SPARSITY-PRINCIPLE", "sparsity"),
    men("M-02", "Mixture-of-Experts (MoE)", "ArchitectureComponent", "A-SPARSITY-PRINCIPLE", "moe"),
    men("M-03", "conditional computation", "Concept", "A-CONDITIONAL-MEMORY", "conditional_computation"),
    men("M-04", "conditional memory", "Concept", "A-CONDITIONAL-MEMORY", "conditional_memory"),
    men("M-05", "knowledge lookup primitive", "Concept", "A-NO-LOOKUP-PRIMITIVE", "knowledge_lookup_primitive"),
    men("M-06", "N-gram embeddings", "Concept", "A-NGRAM-O1-LOOKUP", "ngram_embedding"),
    men("M-07", "Engram", "ArticleMethod", "A-ENGRAM-METHOD", "engram"),
    men("M-08", "contextualized gating", "ArchitectureComponent", "A-ENGRAM-METHOD", "contextualized_gating"),
    men("M-09", "Sparsity Allocation problem", "Concept", "A-SPARSITY-ALLOCATION", "sparsity_allocation"),
    men("M-10", "U-shaped scaling law", "ScalingLaw", "A-USHAPED-LAW", "u_shaped_scaling_law"),
    men("M-11", "Engram-27B", "ArticleMethod", "A-SCALE-27B", "engram"),
    men("M-12", "linguistic duality", "Concept", "A-LINGUISTIC-DUALITY", "linguistic_duality"),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "engram"),
                 ("aliases", ["Engram", "Engram-27B", "conditional memory module"]),
                 ("note", "method and its 27B instantiation are one entity.")]),
    OrderedDict([("canonical", "conditional_memory"),
                 ("aliases", ["conditional memory", "sparse lookup", "scalable lookup"])]),
    OrderedDict([("canonical", "moe"),
                 ("aliases", ["MoE", "Mixture-of-Experts", "conditional computation paradigm"])]),
    OrderedDict([("canonical", "contextualized_gating"),
                 ("aliases", ["contextualized gating", "context-aware gating"]),
                 ("note", "Intro text and Figure 1 caption co-refer.")]),
]

# ---------------------------------------------------------------------------
# relations (same 10 IDs / endpoints / meaning as v0.1; drop confidence; add
# gold_label / difficulty / evidence_strength).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])

RELATIONS = [
    rel("R-01", "method_addresses_problem", "METHOD-ENGRAM", "CLAIM-PROBLEM",
        ["A-ENGRAM-METHOD", "A-NO-LOOKUP-PRIMITIVE"], "medium", "direct"),
    rel("R-02", "claim_extends_prior_work", "CLAIM-CONDITIONAL-MEMORY", "CLAIM-MOE-PRIOR",
        ["A-CONDITIONAL-MEMORY", "A-SPARSITY-PRINCIPLE"], "medium", "direct"),
    rel("R-03", "method_has_component", "METHOD-ENGRAM", "COMPONENT-CONTEXT-GATING",
        ["A-ENGRAM-METHOD", "A-FIG1-CAPTION"], "medium", "direct"),
    rel("R-04", "method_has_component", "METHOD-ENGRAM", "METHOD-SPARSITY-ALLOCATION",
        ["A-SPARSITY-ALLOCATION"], "medium", "indirect"),
    rel("R-05", "result_supports_claim", "RESULT-27B-GAINS", "CLAIM-CONDITIONAL-MEMORY",
        ["A-SCALE-27B", "A-GAINS-PREVIEW"], "medium", "direct"),
    rel("R-06", "result_supports_claim", "SCALINGLAW-USHAPED", "CLAIM-CONDITIONAL-MEMORY",
        ["A-USHAPED-LAW"], "hard", "indirect"),
    rel("R-07", "mechanism_explains_result", "MECH-EFFECTIVE-DEPTH", "RESULT-27B-GAINS",
        ["A-MECH-EARLY-LAYERS", "A-GAINS-PREVIEW"], "hard", "indirect"),
    rel("R-08", "mechanism_explains_result", "MECH-ATTENTION-FREED", "RESULT-LONGCONTEXT",
        ["A-LONGCONTEXT"], "medium", "direct"),
    rel("R-09", "system_design_enables_efficiency", "SYS-PREFETCH", "RESULT-INFRA",
        ["A-INFRA-PREFETCH", "A-INFRA-RESULT"], "medium", "direct"),
    rel("R-10", "result_supports_claim", "SCALINGLAW-USHAPED", "METHOD-SPARSITY-ALLOCATION",
        ["A-USHAPED-LAW", "A-SPARSITY-ALLOCATION"], "medium", "direct"),
]

# ---------------------------------------------------------------------------
# do_not_extract: every inline (Author, year) citation present in the slice
# (kept verbatim in atom text as evidence, NOT extracted as objects), an
# inline_author_year_citation policy, and forward / cross-section references.
# ---------------------------------------------------------------------------
CITATIONS = [
    ("(Lennie, 2003; Olshausen and Field, 1997)", "A-SPARSITY-PRINCIPLE"),
    ("(Dai et al., 2024; Shazeer et al., 2017)", "A-SPARSITY-PRINCIPLE"),
    ("(Comanici et al., 2025; Guo et al., 2025; Team et al., 2025)", "A-MOE-DEFACTO"),
    ("(Constant et al., 2017; Erman, 2000)", "A-LINGUISTIC-DUALITY"),
    ("(Brants et al., 2007; Liu et al., 2024b; Nguyen, 2024)", "A-NGRAM-LOCAL"),
    ("(Vaswani et al., 2017)", "A-NO-LOOKUP-PRIMITIVE"),
    ("(Ghandeharioun et al., 2024; Jin et al., 2025)", "A-RECONSTRUCTION-WASTE"),
    ("(Bengio et al., 2013; Shazeer et al., 2017)", "A-CONDITIONAL-MEMORY"),
    ("(Bojanowski et al., 2017)", "A-NGRAM-O1-LOOKUP"),
    ("(Huang et al., 2025a; Pagnoni et al., 2025; Tito Svenstrup et al., 2017; Yu et al., 2025)", "A-NGRAM-O1-LOOKUP"),
    ("(nostalgebraist, 2020)", "A-MECH-EARLY-LAYERS"),
    ("(Hendrycks et al., 2021a)", "A-MECH-EARLY-LAYERS"),
    ("(Fang et al.)", "A-LONGCONTEXT"),
    ("(Hsieh et al.)", "A-LONGCONTEXT"),
]

DO_NOT_EXTRACT = []
for text, aid in CITATIONS:
    DO_NOT_EXTRACT.append(OrderedDict([
        ("text", text), ("atom_id", aid),
        ("reason", "inline author-year citation kept verbatim as evidence; not a knowledge object."),
        ("kind", "citation"),
    ]))
DO_NOT_EXTRACT.append(OrderedDict([
    ("pattern", "inline_author_year_citation"),
    ("examples", ["(Vaswani et al., 2017)", "(Dai et al., 2024; Shazeer et al., 2017)",
                  "(nostalgebraist, 2020)", "(Fang et al.)"]),
    ("reason", "inline author-year citations are not knowledge objects; suppress citation over-extraction."),
    ("kind", "citation_policy"),
]))
# forward / cross-section references inside the slice.
DO_NOT_EXTRACT.append(OrderedDict([
    ("text", "(see Table 3)"), ("atom_id", "A-RECONSTRUCTION-WASTE"),
    ("reason", "forward reference to a results table in a later section; not an Introduction object."),
    ("kind", "cross_section_forward_reference"),
]))
DO_NOT_EXTRACT.append(OrderedDict([
    ("text", "(detailed in Section 2)"), ("atom_id", "A-ENGRAM-METHOD"),
    ("reason", "forward reference to the Architecture chapter; not an Introduction object."),
    ("kind", "cross_section_forward_reference"),
]))

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Chapter 1 (Introduction) only"),
    ("title", "Conditional Memory via Scalable Lookup: A New Axis of Sparsity for LLMs - Introduction"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 13-30; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "article_research"),
    ("profile_cues", ["1. Introduction", "we propose Engram", "we formulate the Sparsity Allocation problem",
                      "U-shaped scaling law", "iso-parameter and iso-FLOPs MoE baseline", "(see Table 3)",
                      "Figure 1", "(detailed in Section 2)"]),
    ("extraction_targets", ["ArticleClaim", "ArticleMethod", "ArchitectureComponent",
                            "MechanisticExplanation", "ScalingLaw", "SystemDesignClaim", "ExperimentResult"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file (it may include inline (Author, year) citations, em-dashes, inline math $O(1)$, and a page-break-hyphenated word 'knowl- / edge'). normalized_text renders EXACTLY that span: it may rephrase, transliterate math ($O(1)$ -> O(1)), normalize the em-dash and the broken word, and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs not inside the span. EXCEPTION: a formula_atom MAY carry a symbolic interpretation supplied by atoms in metadata.supported_by_context_atoms / interpretation_supported_by (the Introduction has no display formula, hence no formula_atom)."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks needed to fully define it). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only; supporting_context atoms are NOT required to be present in the home package's input."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone (the remaining fields require supporting_context atoms from other packages)."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "figure atoms separate physical_section_id from semantic_section_ids; forward references set include_in_chapter2_core_chunks:false and are isolated in a cross_reference_block. The Introduction's only figure is Figure 1, whose caption is physically and semantically in Chapter 1 (architecture overview), so it stays in the method chunk."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref. The Introduction previews results detailed in later chapters but states their numbers verbatim, so no atom is marked external."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("context_only", "an atom not consumed by any object or relation may be flagged context_only:true; the Introduction has no such atoms (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "The Introduction is a single-level section (# 1. Introduction) with no sub-sections; section_tree has only SEC-1.",
        "PAGE-BREAK SPLIT: MinerU split one semantic paragraph (source lines 17 and 19, with blank line 18 between) leaving a hyphenated page-break word 'compositional reasoning and knowl-' / 'edge retrieval'. A-LINGUISTIC-DUALITY's raw_text spans the break verbatim ('knowl-\\n\\nedge'); normalized_text rejoins it to 'knowledge'.",
        "The only math is inline $O(1)$ (no $$...\\tag{N}$$ block) -> the Introduction has no formula_atom.",
        "Em-dashes are Unicode U+2014 ('text—such as ...'); normalized_text renders them as ' - '.",
        "Inline author-year citations (e.g., (Vaswani et al., 2017)) are dense; they are kept verbatim in raw_text as evidence but dropped from normalized_text and listed in do_not_extract.",
        "Forward references '(see Table 3)' and '(detailed in Section 2)' point outside Chapter 1 and are negatives (do_not_extract).",
        "Figure 1 caption (source line 29) renders as a 'Figure 1 | ...' text paragraph (no image) and is the architecture overview; it is the figure_caption_atom A-FIG1-CAPTION in the method chunk.",
        "Benchmark gains use 'NAME: +X.Y' format inside one sentence; A-GAINS-PREVIEW keeps them as one atom, A-LONGCONTEXT keeps the 'X vs. Y' pairs.",
    ]),
])

# ---------------------------------------------------------------------------
# Assemble + dump.
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
        "# Gold fixture v0.3.3 - Engram paper Chapter 1 Introduction (profile: article_research)\n"
        "# Upgraded from v0.1 WITHOUT changing atom/chunk/object/relation IDs or meaning:\n"
        "# dual coordinates (source_span authoritative + viewer_span), verbatim raw_text +\n"
        "# normalized_text discipline, object evidence split into local/supporting + home_package,\n"
        "# per-chunk context_packages with expected_objects (+ expected_local_fields), labels\n"
        "# gold_label/difficulty/evidence_strength (NO decimal confidence), inline-citation and\n"
        "# forward-reference do_not_extract negatives, auto-generated source_elements.\n"
        "# Spans are computed by build.py against engram_paper_mineru.md; do NOT hand-edit.\n"
    )
    body = yaml.dump(GOLD, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=10000)
    with open(os.path.join(HERE, "gold.yaml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    print("wrote source.md (%d bytes) and gold.yaml" % len(VIEWER_TEXT))
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d mentions=%d do_not_extract=%d" % (
        len(ATOMS), len(SOURCE_ELEMENTS), len(SEMANTIC_CHUNKS), len(CONTEXT_PACKAGES),
        len(OBJECTS), len(RELATIONS), len(MENTIONS), len(DO_NOT_EXTRACT)))


if __name__ == "__main__":
    main()
