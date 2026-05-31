#!/usr/bin/env python3
"""Builder for ch03_scaling_laws gold.yaml (schema v0.3.3, profile article_research).

Upgrades the v0.1 fixture (Engram paper Chapter 3, sections 3.1-3.2) to v0.3.3:
re-expresses the SAME atoms/chunks/objects/relations (IDs and meaning preserved)
in the v0.3.3 schema and adds verbatim dual spans.

All spans are computed by locating verbatim substrings (anchors) in the
authoritative MinerU source file; YAML spans are never hand-written. The viewer
file source.md is a verbatim slice of source_file lines 110-157 (viewer-only).

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

SLICE_LINE_START = 110
SLICE_LINE_END = 157

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

# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice of lines 110-157 + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY

# offset (into VIEWER_TEXT) of the start of source line N (N within slice).
_HEADER_LEN = len(VIEWER_HEADER)
_VIEWER_LINE_OFF = {}
_vacc = _HEADER_LEN
_vbody_lines = VIEWER_BODY.split("\n")
for k, _ln in enumerate(_vbody_lines):
    src_lineno = SLICE_LINE_START + k
    _VIEWER_LINE_OFF[src_lineno] = _vacc
    _vacc += len(_ln) + 1

# viewer line number (1-based within the viewer file) for a given source line.
def viewer_lineno(src_lineno):
    # header occupies viewer line 1; slice line SLICE_LINE_START -> viewer line 2.
    return 1 + (src_lineno - SLICE_LINE_START) + 1


# ---------------------------------------------------------------------------
# Span helper: locate a verbatim contiguous substring spanning one or more
# source lines and emit source_span (authoritative) + viewer_span.
# `raw` must occur exactly once in the source slice.
# ---------------------------------------------------------------------------
SLICE_START_CHAR = _LINE_OFF[SLICE_LINE_START]
SLICE_END_CHAR = _LINE_OFF[SLICE_LINE_END] + len(SRC_LINES[SLICE_LINE_END - 1])
SLICE_SRC = SRC[SLICE_START_CHAR:SLICE_END_CHAR]


def _line_of_char(src_char):
    """1-based source line number containing absolute char offset src_char."""
    lineno = SLICE_LINE_START
    for n in range(SLICE_LINE_START, SLICE_LINE_END + 1):
        if _LINE_OFF[n] <= src_char:
            lineno = n
        else:
            break
    return lineno


def span_for(raw):
    rel = SLICE_SRC.find(raw)
    if rel < 0:
        raise SystemExit("NOT FOUND in slice: %r" % raw[:70])
    if SLICE_SRC.find(raw, rel + 1) >= 0:
        raise SystemExit("AMBIGUOUS (matches >1): %r" % raw[:70])
    src_cs = SLICE_START_CHAR + rel
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch"

    src_ls = _line_of_char(src_cs)
    src_le = _line_of_char(src_ce - 1)

    # viewer coordinates: same relative offset, shifted by header + per-line
    # delta. Since viewer body is the verbatim slice, the substring sits at the
    # same intra-line offset; compute via viewer line offset of the start line.
    v_cs = _VIEWER_LINE_OFF[src_ls] + (src_cs - _LINE_OFF[src_ls])
    v_ce = v_cs + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch"

    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", src_ls), ("line_end", src_le),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", viewer_lineno(src_ls)), ("line_end", viewer_lineno(src_le)),
        ("char_start", v_cs), ("char_end", v_ce),
        ("viewer_only", True),
    ])
    return source_span, viewer_span


def atom(aid, section_id, atom_type, se_id, raw, normalized, evidence_strength, metadata=None):
    ss, vs = span_for(raw)
    d = OrderedDict()
    d["id"] = aid
    d["section_id"] = section_id
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
# Evidence atoms. raw_text = verbatim contiguous spans of the slice.
# normalized_text renders EXACTLY that span (ascii rho/P_tot/...; no out-of-span
# facts). NUMERIC AUDIT (see validate.py): every number in normalized appears in
# its raw span.
# ---------------------------------------------------------------------------
ATOMS = [
    # ---------- 3.0 chapter intro: two research questions ----------
    atom(
        "A-SCALE-CLAIM-001", "SEC-3", "claim_sentence", "SE-3-INTRO",
        "Engram, as an instantiation of conditional memory, is structurally complementary to the conditional computation provided by MoE experts. This section investigates the scaling properties of this duality and how to optimally allocate sparse capacity.",
        "Engram, as an instantiation of conditional memory, is structurally complementary to the conditional computation provided by MoE experts. This section investigates the scaling properties of this duality and how to optimally allocate sparse capacity.",
        "direct",
    ),
    atom(
        "A-SCALE-Q-001", "SEC-3", "claim_sentence", "SE-3-Q",
        "1. Allocation under Finite Constraints. When total parameters and training compute are fixed (Iso-parameter and Iso-FLOPs), how should we split the sparse capacity between MoE experts and Engram embeddings?\n2. Infinite Memory Regime. Considering the non-scaling $O(1)$ overhead of Engram, if the memory budget is relaxed or scaled aggressively, what scaling behavior does Engram exhibit by itself?",
        "Two questions drive the study: (1) Allocation under finite constraints - given fixed total params and training compute (iso-parameter, iso-FLOPs), how to split sparse capacity between MoE experts and Engram embeddings; (2) Infinite memory regime - given Engram's non-scaling O(1) overhead, what scaling behavior does Engram exhibit by itself under aggressive memory scaling.",
        "direct",
    ),

    # ---------- 3.1 Optimal Allocation Ratio ----------
    atom(
        "A-ALLOC-DEF-001", "SEC-3.1", "definition_atom", "SE-3.1-PDEF",
        "- $P_{\\text{tot}}$ : total trainable parameters, excluding vocabulary embedding and LM head.\n- $P_{\\text{act}}$ : activated parameters per token. This quantity determines the training cost (FLOPs).\n- $P_{\\text{sparse}} \\triangleq P_{\\text{tot}} - P_{\\text{act}}$ : the inactive parameters, which represents the “free” parameter budget available for scaling model size without incurring computational cost (e.g., unselected experts or unretrieved embeddings).",
        "Three parameter metrics: P_tot = total trainable parameters (excluding vocabulary embedding and LM head); P_act = activated parameters per token (determines training cost / FLOPs); P_sparse := P_tot - P_act = the inactive 'free' parameter budget for scaling model size without incurring computational cost (e.g., unselected experts or unretrieved embeddings).",
        "direct",
    ),
    atom(
        "A-ALLOC-CONTROL-001", "SEC-3.1", "experiment_setup_atom", "SE-3.1-CONTROL",
        "We keep $P_{tot}$ and $P_{act}$ fixed within each FLOPs budget, so that models have the same number of parameters and the same per-token FLOPs. For MoE, $P_{act}$ is determined by the top-k selected experts, while the parameters of non-selected experts contribute to $P_{sparse}$ . For Engram, only a constant number of slots are retrieved per token, so scaling the number of embedding slots increases $P_{tot}$ without increasing per-token FLOPs.",
        "Control: P_tot and P_act are held fixed within each FLOPs budget (same number of parameters, same per-token FLOPs). For MoE, P_act is determined by the top-k selected experts and non-selected experts contribute to P_sparse; for Engram only a constant number of slots are retrieved per token, so scaling embedding slots increases P_tot without increasing per-token FLOPs.",
        "direct",
    ),
    atom(
        "A-ALLOC-RHO-007", "SEC-3.1", "formula_atom", "SE-3.1-EQ7",
        "P _ {\\mathrm{MoE}} ^ {(\\text {sparse})} = \\rho P _ {\\text {sparse}}, \\quad P _ {\\text {Engram}} = (1 - \\rho) P _ {\\text {sparse}}. \\tag {7}",
        "P_MoE^(sparse) = rho * P_sparse ;  P_Engram = (1 - rho) * P_sparse",
        "direct",
        metadata=OrderedDict([
            ("tag", "7"),
            ("role", "allocation_ratio"),
            ("supported_by_context_atoms", ["A-ALLOC-RHO-DEF"]),
            ("note", "rho in [0,1] is the fraction of the inactive-parameter budget assigned to MoE (definition lives in adjacent prose A-ALLOC-RHO-DEF)."),
        ]),
    ),
    atom(
        "A-ALLOC-RHO-DEF", "SEC-3.1", "definition_atom", "SE-3.1-RHODEF",
        "Allocation ratio. We define the allocation ratio $\\rho \\in [0,1]$ as the fraction of the inactive-parameter budget assigned to MoE expert capacity:",
        "Allocation ratio: rho in [0,1] is the fraction of the inactive-parameter budget assigned to MoE expert capacity.",
        "direct",
    ),
    atom(
        "A-ALLOC-RHO-LIMITS", "SEC-3.1", "definition_atom", "SE-3.1-RHOLIM",
        "- $\\rho = 1$ corresponds to a pure MoE model (all inactive parameters are routed experts).\n- $\\rho < 1$ reduces the number of routed experts and reallocates the freed parameters to Engram embedding slots.",
        "rho = 1 corresponds to a pure MoE model (all inactive parameters are routed experts); rho < 1 reduces the number of routed experts and reallocates the freed parameters to Engram embedding slots.",
        "direct",
    ),
    atom(
        "A-ALLOC-SETUP-001", "SEC-3.1", "experiment_setup_atom", "SE-3.1-SETUP",
        "Experimental protocol. We evaluate this trade-off at two compute regimes and maintain a constant sparsity ratio $P_{tot}/P_{act} \\approx 10$ across both settings:\n\n- $C = 2 \\times 10^{20}$ FLOPs: $P_{\\text{tot}} \\approx 5.7 \\text{B}$ and $P_{\\text{act}} = 568 \\text{M}$ . The baseline ( $\\rho = 1$ ) has a total of 106 experts.\n- $C = 6 \\times 10^{20}$ FLOPs: $P_{\\text{tot}} \\approx 9.9\\text{B}$ and $P_{\\text{act}} = 993\\text{M}$ . The baseline ( $\\rho = 1$ ) has a total of 99 experts.\n\nFor different $\\rho$ , we construct the corresponding model only by adjusting the number of routed experts and the number of Engram embedding slots. All runs use the identical training pipeline and optimization hyperparameters.",
        "Experimental protocol: two compute regimes with constant sparsity ratio P_tot/P_act ~= 10. C = 2 x 10^20 FLOPs: P_tot ~= 5.7B, P_act = 568M, baseline (rho = 1) has 106 experts. C = 6 x 10^20 FLOPs: P_tot ~= 9.9B, P_act = 993M, baseline (rho = 1) has 99 experts. For each rho only the routed-expert count and Engram embedding-slot count change; all runs use the identical training pipeline and optimization hyperparameters.",
        "direct",
    ),
    atom(
        "A-ALLOC-RESULT-USHAPE", "SEC-3.1", "scaling_law_result_atom", "SE-3.1-RESULT",
        "Results and Analysis. Figure 3 (left) reveals a consistent U-shaped relationship between validation loss and the allocation ratio $\\rho$ . Remarkably, the Engram model achieves comparable performance to the pure MoE baseline ( $\\rho = 100\\%$ ) even when the MoE allocation is reduced to just $\\rho \\approx 40\\%$ (i.e., a total of 46 experts for the 5.7B model and 43 experts for the 9.9B model).",
        "Figure 3 (left) shows a consistent U-shaped relationship between validation loss and the allocation ratio rho. The Engram model matches the pure MoE baseline (rho = 100%) even when the MoE allocation is reduced to just rho ~= 40% (46 experts for the 5.7B model and 43 experts for the 9.9B model).",
        "direct",
    ),
    atom(
        "A-ALLOC-RESULT-OPT", "SEC-3.1", "scaling_law_result_atom", "SE-3.1-RESULT",
        "Furthermore, the pure MoE baseline proves suboptimal: reallocating roughly 20%–25% of the sparse parameter budget to Engram yields the best performance. Quantitatively, in the 10B regime ( $C = 6 \\times 10^{20}$ ), validation loss improves from 1.7248 (at $\\rho = 100\\%$ ) to 1.7109 near the optimum of $\\rho \\approx 80\\%$ ( $\\Delta = 0.0139$ ). Crucially, the location of this optimum is stable across regimes ( $\\rho \\approx 75\\%–80\\%$ ), suggesting a robust allocation preference across the examined scales (under fixed sparsity).",
        "The pure MoE baseline is suboptimal: reallocating roughly 20%-25% of the sparse parameter budget to Engram yields the best performance. In the 10B regime (C = 6 x 10^20), validation loss improves from 1.7248 (at rho = 100%) to 1.7109 near the optimum of rho ~= 80% (delta = 0.0139). The optimum is stable across regimes (rho ~= 75%-80%) under fixed sparsity.",
        "direct",
    ),
    atom(
        "A-ALLOC-MECH-MOE", "SEC-3.1", "mechanism_sentence", "SE-3.1-MECH-MOE",
        "- MoE-dominated ( $\\rho \\rightarrow 100\\%$ ): The model lacks dedicated memory for static patterns, forcing it to inefficiently reconstruct them through depth and computation.",
        "MoE-dominated (rho -> 100%): the model lacks dedicated memory for static patterns, forcing it to inefficiently reconstruct them through depth and computation.",
        "direct",
    ),
    atom(
        "A-ALLOC-MECH-ENGRAM", "SEC-3.1", "mechanism_sentence", "SE-3.1-MECH-ENGRAM",
        "- Engram-dominated ( $\\rho \\rightarrow 0\\%$ ): The model loses conditional computation capacity, hurting tasks that require dynamic, context-dependent reasoning; memory cannot replace computation in this regime.",
        "Engram-dominated (rho -> 0%): the model loses conditional computation capacity, hurting tasks that require dynamic, context-dependent reasoning; memory cannot replace computation in this regime.",
        "direct",
    ),

    # ---------- 3.2 Infinite Memory Regime ----------
    atom(
        "A-INF-SETUP-001", "SEC-3.2", "experiment_setup_atom", "SE-3.2-SETUP",
        "Experimental protocol. We utilize a fixed MoE backbone with $P_{tot} \\approx 3B$ and $P_{act} = 568M$ , trained for 100B tokens to ensure convergence. On top of this backbone, we attach an Engram table and sweep the number of slots M from $2.58 \\times 10^{5}$ to $1.0 \\times 10^{7}$ (adding up to $\\approx 13$ billion parameters).",
        "Aggressive-memory-scaling setup: a fixed MoE backbone with P_tot ~= 3B and P_act = 568M, trained for 100B tokens to ensure convergence; an Engram table is attached and the slot count M is swept from 2.58 x 10^5 to 1.0 x 10^7 (adding up to ~= 13 billion parameters).",
        "direct",
    ),
    atom(
        "A-INF-BASELINE-001", "SEC-3.2", "experiment_setup_atom", "SE-3.2-SETUP",
        "For baselines, we compare against OverEncoding (Huang et al., 2025a), which integrates N-gram embeddings via averaging with the vocabulary embedding. We note that while other work such as SCONE (Yu et al., 2025) also investigates large-scale embeddings, it is primarily inference-focused and includes extra module (f-gram model) and additional training FLOPs, rendering it incompatible with the strict iso-compute constraints of this study.",
        "Baseline is OverEncoding (Huang et al., 2025a), which integrates N-gram embeddings by averaging with the vocabulary embedding. SCONE (Yu et al., 2025) is excluded: it is primarily inference-focused and adds an extra module (f-gram model) and additional training FLOPs, making it incompatible with the strict iso-compute constraints of this study.",
        "direct",
    ),
    atom(
        "A-INF-RESULT-POWERLAW", "SEC-3.2", "scaling_law_result_atom", "SE-3.2-RESULT",
        "Results. Figure 3 (right) demonstrates that scaling the number of memory slots yields a clear and consistent improvement in validation loss. Across the explored range, the curve follows a strict power law (linear in log-space), indicating that Engram provides a predictable scaling knob: larger memory continues to pay off without requiring additional computation.",
        "Figure 3 (right) shows that scaling the number of memory slots yields a clear, consistent improvement in validation loss; across the explored range the curve follows a strict power law (linear in log-space), so Engram provides a predictable scaling knob - larger memory keeps paying off without requiring additional computation.",
        "direct",
    ),
    atom(
        "A-INF-RESULT-VS-OE", "SEC-3.2", "result_sentence", "SE-3.2-RESULT",
        "Crucially, regarding scaling efficiency: while the direct averaging approach of OverEncoding benefits from larger memory tables, Engram unlocks much larger scaling potential from the same memory budget.",
        "On scaling efficiency: while OverEncoding's direct averaging approach also benefits from larger memory tables, Engram unlocks much larger scaling potential from the same memory budget.",
        "direct",
    ),
    atom(
        "A-INF-CLAIM-001", "SEC-3.2", "claim_sentence", "SE-3.2-RESULT",
        "Together with the allocation law in Section 3.1, these results validate that conditional memory serves as a distinct, scalable axis of sparse capacity that complements the conditional computation of MoE.",
        "Together with the allocation law of Section 3.1, these results validate that conditional memory is a distinct, scalable axis of sparse capacity that complements the conditional computation of MoE.",
        "direct",
    ),
]

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# source_elements: built from atom source spans grouped by source_element_id,
# plus the three section headings.
# ---------------------------------------------------------------------------
def build_source_elements(atoms):
    elems = [
        OrderedDict([("id", "SE-H-3"), ("type", "heading"), ("file", SOURCE_NAME),
                     ("line_start", 110), ("line_end", 110)]),
        OrderedDict([("id", "SE-H-3.1"), ("type", "heading"), ("file", SOURCE_NAME),
                     ("line_start", 117), ("line_end", 117)]),
        OrderedDict([("id", "SE-H-3.2"), ("type", "heading"), ("file", SOURCE_NAME),
                     ("line_start", 150), ("line_end", 150)]),
    ]
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    for se_id, members in groups.items():
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        etype = "formula" if any(m["atom_type"] == "formula_atom" for m in members) else "paragraph"
        d = OrderedDict([
            ("id", se_id), ("type", etype),
            ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le),
        ])
        if se_id == "SE-3.1-EQ7":
            d["metadata"] = OrderedDict([("tag", "7")])
        elems.append(d)
    return elems

SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ---------------------------------------------------------------------------
# section_tree
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-3"), ("path", "3"), ("title", "Scaling Laws and Sparsity Allocation")]),
    OrderedDict([("id", "SEC-3.1"), ("path", "3 > 3.1"), ("title", "Optimal Allocation Ratio Between MoE and Engram"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-3.2"), ("path", "3 > 3.2"), ("title", "Engram under Infinite Memory Regime"), ("parent", "SEC-3")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks (same partition as v0.1).
# ---------------------------------------------------------------------------
SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C3-INTRO"), ("profile", "article_research"),
        ("chunk_type", "article_core_claim_block"), ("section_path", "3"),
        ("atom_ids", ["A-SCALE-CLAIM-001", "A-SCALE-Q-001"]),
        ("central_atom_ids", ["A-SCALE-CLAIM-001"]),
        ("boundary_reason", "section framing: the complementarity thesis plus the two research questions that scope 3.1/3.2; kept as one core-claim block."),
        ("extraction_targets", ["ArticleClaim"]),
        ("gold_must_cover_atoms", ["A-SCALE-CLAIM-001", "A-SCALE-Q-001"]),
    ]),
    OrderedDict([
        ("id", "C3-ALLOC"), ("profile", "article_research"),
        ("chunk_type", "scaling_law_block"), ("section_path", "3 > 3.1"),
        ("atom_ids", ["A-ALLOC-DEF-001", "A-ALLOC-CONTROL-001", "A-ALLOC-RHO-007", "A-ALLOC-RHO-DEF",
                      "A-ALLOC-RHO-LIMITS", "A-ALLOC-SETUP-001", "A-ALLOC-RESULT-USHAPE",
                      "A-ALLOC-RESULT-OPT", "A-ALLOC-MECH-MOE", "A-ALLOC-MECH-ENGRAM"]),
        ("central_atom_ids", ["A-ALLOC-RESULT-USHAPE", "A-ALLOC-RHO-007"]),
        ("boundary_reason", "qiefen Section 5.2 anchor expansion: anchor 'U-shaped relationship between validation loss and allocation ratio' expands BACK over P_def/control/rho definition+formula (Eq.7)/setup and FORWARD over optimum rho + loss numbers + MoE-/Engram-dominated mechanism; the whole scaling law is one knowledge unit and must not be split (formula_continuity + experiment setup-result coupling)."),
        ("extraction_targets", ["ScalingLaw", "ExperimentSetup", "ExperimentResult", "MechanisticExplanation", "DerivedRuleCandidate"]),
        ("gold_must_cover_atoms", ["A-ALLOC-RHO-007", "A-ALLOC-SETUP-001", "A-ALLOC-RESULT-USHAPE", "A-ALLOC-RESULT-OPT", "A-ALLOC-MECH-MOE"]),
    ]),
    OrderedDict([
        ("id", "C3-INF"), ("profile", "article_research"),
        ("chunk_type", "scaling_law_block"), ("section_path", "3 > 3.2"),
        ("atom_ids", ["A-INF-SETUP-001", "A-INF-BASELINE-001", "A-INF-RESULT-POWERLAW",
                      "A-INF-RESULT-VS-OE", "A-INF-CLAIM-001"]),
        ("central_atom_ids", ["A-INF-RESULT-POWERLAW"]),
        ("boundary_reason", "infinite-memory experiment: setup + baseline kept with their power-law result and the OverEncoding comparison (experiment setup-result coupling); the closing claim that conditional memory is a distinct scalable axis stays in-block as the result it supports."),
        ("extraction_targets", ["ExperimentSetup", "ExperimentResult", "ScalingLaw", "ArticleClaim"]),
        ("gold_must_cover_atoms", ["A-INF-SETUP-001", "A-INF-RESULT-POWERLAW", "A-INF-RESULT-VS-OE"]),
    ]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}

# ---------------------------------------------------------------------------
# objects (same IDs/meaning as v0.1). local/supporting + home_package + labels.
# ---------------------------------------------------------------------------
def obj(oid, otype, section_path, home_package, payload, local, supporting, difficulty, evidence_strength):
    d = OrderedDict()
    d["id"] = oid
    d["type"] = otype
    d["section_path"] = section_path
    d["home_package"] = home_package
    d["payload"] = payload
    d["local_evidence_atom_ids"] = local
    d["supporting_context_atom_ids"] = supporting
    d["gold_label"] = True
    d["difficulty"] = difficulty
    d["evidence_strength"] = evidence_strength
    return d

OBJECTS = [
    obj("CLAIM-COMPLEMENTARITY", "ArticleClaim", "3", "PKG-INTRO",
        OrderedDict([
            ("statement", "Conditional memory (Engram) is a distinct, scalable axis of sparse capacity that is structurally complementary to the conditional computation of MoE experts."),
            ("scoped_by", "two research questions: allocation under finite constraints (3.1) and the infinite memory regime (3.2)"),
        ]),
        ["A-SCALE-CLAIM-001", "A-SCALE-Q-001"], ["A-INF-CLAIM-001"], "medium", "direct"),

    obj("SETUP-ALLOC", "ExperimentSetup", "3 > 3.1", "PKG-ALLOC",
        OrderedDict([
            ("design", "Iso-parameter / iso-FLOPs allocation sweep over the allocation ratio rho"),
            ("metrics", "P_tot, P_act, P_sparse := P_tot - P_act"),
            ("control", "P_tot and P_act fixed within each FLOPs budget; only the routed-expert count and Engram slot count vary; identical pipeline and hyperparameters"),
            ("regimes", [
                "C = 2 x 10^20 FLOPs: P_tot ~= 5.7B, P_act = 568M, baseline rho=1 has 106 experts",
                "C = 6 x 10^20 FLOPs: P_tot ~= 9.9B, P_act = 993M, baseline rho=1 has 99 experts",
            ]),
            ("sparsity_ratio", "P_tot/P_act ~= 10 (constant across regimes)"),
        ]),
        ["A-ALLOC-DEF-001", "A-ALLOC-CONTROL-001", "A-ALLOC-RHO-007", "A-ALLOC-RHO-DEF",
         "A-ALLOC-RHO-LIMITS", "A-ALLOC-SETUP-001"], [], "medium", "direct"),

    obj("SCALINGLAW-USHAPE", "ScalingLaw", "3 > 3.1", "PKG-ALLOC",
        OrderedDict([
            ("shape", "U-shaped: validation loss vs allocation ratio rho (Figure 3 left)"),
            ("finding", "Engram matches pure MoE (rho=100%) even at rho ~= 40% (46 experts @5.7B, 43 experts @9.9B); pure MoE is suboptimal"),
            ("optimum", "rho ~= 75%-80% (reallocate ~20%-25% of the sparse budget to Engram), stable across regimes"),
            ("quantitative", "10B regime (C = 6 x 10^20): loss 1.7248 (rho=100%) -> 1.7109 (rho ~= 80%), delta = 0.0139"),
        ]),
        ["A-ALLOC-RESULT-USHAPE", "A-ALLOC-RESULT-OPT"], [], "medium", "direct"),

    obj("RESULT-ALLOC", "ExperimentResult", "3 > 3.1", "PKG-ALLOC",
        OrderedDict([
            ("result", "U-shaped loss-vs-rho curve; optimum at rho ~= 75%-80%; best 10B-regime loss 1.7109 vs 1.7248 baseline (delta 0.0139)"),
            ("figure", "Figure 3 (left)"),
        ]),
        ["A-ALLOC-RESULT-USHAPE", "A-ALLOC-RESULT-OPT"], [], "medium", "direct"),

    obj("MECH-COMPLEMENTARITY", "MechanisticExplanation", "3 > 3.1", "PKG-ALLOC",
        OrderedDict([
            ("moe_dominated", "rho -> 100%: no dedicated memory; static patterns reconstructed inefficiently via depth/computation"),
            ("engram_dominated", "rho -> 0%: loses conditional computation; hurts dynamic context-dependent reasoning; memory cannot replace computation"),
            ("conclusion", "the U-shape confirms the structural complementarity of the two modules"),
        ]),
        ["A-ALLOC-MECH-MOE", "A-ALLOC-MECH-ENGRAM"], [], "medium", "direct"),

    obj("RULE-OPTIMAL-RHO", "DerivedRuleCandidate", "3 > 3.1", "PKG-ALLOC",
        OrderedDict([
            ("rule", "Allocate ~20%-25% of the sparse-parameter budget to Engram (allocation ratio rho ~= 75%-80%) rather than using pure MoE."),
            ("basis", "U-shaped optimum stable across the 5.7B and 9.9B regimes under fixed sparsity"),
        ]),
        ["A-ALLOC-RESULT-OPT"], [], "hard", "indirect"),

    obj("SETUP-INF", "ExperimentSetup", "3 > 3.2", "PKG-INF",
        OrderedDict([
            ("design", "Aggressive memory scaling on a fixed backbone"),
            ("backbone", "fixed MoE backbone P_tot ~= 3B, P_act = 568M, 100B training tokens"),
            ("sweep", "Engram slot count M from 2.58 x 10^5 to 1.0 x 10^7 (adds up to ~= 13 billion params)"),
            ("baseline", "OverEncoding (Huang et al., 2025a); SCONE excluded as inference-focused with extra f-gram FLOPs (breaks iso-compute)"),
        ]),
        ["A-INF-SETUP-001", "A-INF-BASELINE-001"], [], "medium", "direct"),

    obj("SCALINGLAW-INF", "ScalingLaw", "3 > 3.2", "PKG-INF",
        OrderedDict([
            ("shape", "Strict power law (linear in log-space): validation loss vs number of memory slots (Figure 3 right)"),
            ("finding", "Memory is a predictable scaling knob; larger memory keeps improving loss without extra compute"),
            ("vs_baseline", "Engram unlocks much larger scaling potential than OverEncoding from the same memory budget"),
        ]),
        ["A-INF-RESULT-POWERLAW", "A-INF-RESULT-VS-OE"], [], "medium", "direct"),

    obj("RESULT-INF", "ExperimentResult", "3 > 3.2", "PKG-INF",
        OrderedDict([
            ("result", "Power-law (log-linear) validation-loss improvement with memory slots; Engram > OverEncoding in scaling efficiency"),
            ("figure", "Figure 3 (right)"),
        ]),
        ["A-INF-RESULT-POWERLAW", "A-INF-RESULT-VS-OE"], [], "medium", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# context_packages: ONE per core chunk. atoms == chunk.atom_ids.
# expected_objects = objects whose home_package is this package.
# expected_local_fields for cross-package objects.
# ---------------------------------------------------------------------------
def build_package(pid, chunk_id, section_path, linked_context, extraction_targets,
                  expected_objects, expected_local_fields=None):
    d = OrderedDict([
        ("id", pid),
        ("profile", "article_research"),
        ("chunk_id", chunk_id),
        ("section_path", section_path),
        ("document_title", "Engram: Conditional Memory as a Scalable Axis of Sparsity"),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", linked_context),
        ("extraction_targets", extraction_targets),
        ("expected_objects", expected_objects),
    ])
    if expected_local_fields:
        d["expected_local_fields"] = expected_local_fields
    return d

CONTEXT_PACKAGES = [
    build_package(
        "PKG-INTRO", "C3-INTRO", "Chapter 3 > 3. Scaling Laws and Sparsity Allocation",
        OrderedDict([
            ("previous_heading", "2.5 Infrastructure-aware Efficiency"),
            ("next_heading", "3.1 Optimal Allocation Ratio Between MoE and Engram"),
        ]),
        ["ArticleClaim"],
        ["CLAIM-COMPLEMENTARITY"],
        # CLAIM-COMPLEMENTARITY is a cross-package object: its statement (the
        # distinct-scalable-axis wording) is completed by A-INF-CLAIM-001 in
        # PKG-INF; from the intro atoms alone only the framing fields are local.
        expected_local_fields=OrderedDict([
            ("CLAIM-COMPLEMENTARITY", ["statement", "scoped_by"]),
        ]),
    ),
    build_package(
        "PKG-ALLOC", "C3-ALLOC", "Chapter 3 > 3.1 Optimal Allocation Ratio Between MoE and Engram",
        OrderedDict([
            ("formula_context", "Eq.(7) allocation ratio rho splits P_sparse between MoE experts and Engram embeddings"),
            ("figure", "Figure 3 (left): validation loss vs allocation ratio rho (U-shaped)"),
            ("previous_heading", "3. Scaling Laws and Sparsity Allocation"),
            ("next_heading", "3.2 Engram under Infinite Memory Regime"),
        ]),
        ["ScalingLaw", "ExperimentSetup", "ExperimentResult", "MechanisticExplanation", "DerivedRuleCandidate"],
        ["SETUP-ALLOC", "SCALINGLAW-USHAPE", "RESULT-ALLOC", "MECH-COMPLEMENTARITY", "RULE-OPTIMAL-RHO"],
    ),
    build_package(
        "PKG-INF", "C3-INF", "Chapter 3 > 3.2 Engram under Infinite Memory Regime",
        OrderedDict([
            ("figure", "Figure 3 (right): validation loss vs number of memory slots (power law)"),
            ("previous_heading", "3.1 Optimal Allocation Ratio Between MoE and Engram"),
            ("next_heading", "4. Large Scale Pre-training"),
        ]),
        ["ExperimentSetup", "ExperimentResult", "ScalingLaw", "ArticleClaim"],
        ["SETUP-INF", "SCALINGLAW-INF", "RESULT-INF"],
        # CLAIM-COMPLEMENTARITY's home is PKG-INTRO but A-INF-CLAIM-001 here
        # supplies the distinct-scalable-axis conclusion; declare the locally
        # derivable field so cross-package recall is scored fairly.
        expected_local_fields=OrderedDict([
            ("CLAIM-COMPLEMENTARITY", ["statement"]),
        ]),
    ),
]

# ---------------------------------------------------------------------------
# mentions (same set as v0.1; atom_id targets preserved).
# ---------------------------------------------------------------------------
MENTIONS = [
    OrderedDict([("id", "M3-01"), ("text", "P_sparse"), ("type", "Variable"), ("atom_id", "A-ALLOC-DEF-001"), ("canonical_key", "p_sparse")]),
    OrderedDict([("id", "M3-02"), ("text", "allocation ratio rho"), ("type", "Variable"), ("atom_id", "A-ALLOC-RHO-007"), ("canonical_key", "allocation_ratio_rho")]),
    OrderedDict([("id", "M3-03"), ("text", "iso-FLOPs setup"), ("type", "ExperimentSetup"), ("atom_id", "A-ALLOC-SETUP-001"), ("canonical_key", "alloc_iso_flops_setup")]),
    OrderedDict([("id", "M3-04"), ("text", "U-shaped relationship"), ("type", "ScalingLaw"), ("atom_id", "A-ALLOC-RESULT-USHAPE"), ("canonical_key", "u_shaped_allocation_law")]),
    OrderedDict([("id", "M3-05"), ("text", "optimal rho 75-80%"), ("type", "DerivedRuleCandidate"), ("atom_id", "A-ALLOC-RESULT-OPT"), ("canonical_key", "optimal_allocation_rho")]),
    OrderedDict([("id", "M3-06"), ("text", "MoE-dominated regime"), ("type", "MechanisticExplanation"), ("atom_id", "A-ALLOC-MECH-MOE"), ("canonical_key", "moe_dominated_mechanism")]),
    OrderedDict([("id", "M3-07"), ("text", "infinite memory regime"), ("type", "ExperimentSetup"), ("atom_id", "A-INF-SETUP-001"), ("canonical_key", "infinite_memory_setup")]),
    OrderedDict([("id", "M3-08"), ("text", "OverEncoding"), ("type", "ArticleClaim"), ("atom_id", "A-INF-BASELINE-001"), ("canonical_key", "overencoding_baseline")]),
    OrderedDict([("id", "M3-09"), ("text", "power law (log-linear)"), ("type", "ScalingLaw"), ("atom_id", "A-INF-RESULT-POWERLAW"), ("canonical_key", "infinite_memory_power_law")]),
    OrderedDict([("id", "M3-10"), ("text", "conditional memory"), ("type", "Concept"), ("atom_id", "A-SCALE-CLAIM-001"), ("canonical_key", "conditional_memory")]),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "allocation_ratio_rho"),
                 ("aliases", ["allocation ratio", "rho", "rho in [0,1]", "fraction of inactive budget to MoE"])]),
    OrderedDict([("canonical", "u_shaped_allocation_law"),
                 ("aliases", ["U-shaped relationship", "U-shape", "U-shaped scaling law", "allocation law"])]),
    OrderedDict([("canonical", "optimal_allocation_rho"),
                 ("aliases", ["optimal rho", "rho ~= 75%-80%", "rho ~= 80%", "best allocation"]),
                 ("note", "DerivedRuleCandidate: allocate roughly 20-25% of the sparse budget to Engram (rho 75-80%) for the optimum.")]),
    OrderedDict([("canonical", "infinite_memory_power_law"),
                 ("aliases", ["power law", "linear in log-space", "log-linear scaling", "memory-slot scaling"])]),
    OrderedDict([("canonical", "conditional_memory"),
                 ("aliases", ["conditional memory", "Engram", "memory module"]),
                 ("note", "Engram instantiates conditional memory, complementary to MoE's conditional computation.")]),
]

# ---------------------------------------------------------------------------
# relations (same R3-01..R3-08; endpoints preserved).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])

RELATIONS = [
    rel("R3-01", "experiment_tests_claim", "SETUP-ALLOC", "CLAIM-COMPLEMENTARITY",
        ["A-ALLOC-SETUP-001", "A-ALLOC-RHO-007"], "medium", "direct"),
    rel("R3-02", "result_supports_claim", "RESULT-ALLOC", "CLAIM-COMPLEMENTARITY",
        ["A-ALLOC-RESULT-USHAPE", "A-ALLOC-RESULT-OPT"], "medium", "direct"),
    rel("R3-03", "result_supports_claim", "RESULT-ALLOC", "SCALINGLAW-USHAPE",
        ["A-ALLOC-RESULT-USHAPE"], "medium", "direct"),
    rel("R3-04", "mechanism_explains_result", "MECH-COMPLEMENTARITY", "RESULT-ALLOC",
        ["A-ALLOC-MECH-MOE", "A-ALLOC-MECH-ENGRAM"], "medium", "direct"),
    rel("R3-05", "claim_suggests_design_rule", "SCALINGLAW-USHAPE", "RULE-OPTIMAL-RHO",
        ["A-ALLOC-RESULT-OPT"], "hard", "indirect"),
    rel("R3-06", "experiment_tests_claim", "SETUP-INF", "CLAIM-COMPLEMENTARITY",
        ["A-INF-SETUP-001"], "hard", "indirect"),
    rel("R3-07", "result_supports_claim", "RESULT-INF", "SCALINGLAW-INF",
        ["A-INF-RESULT-POWERLAW"], "medium", "direct"),
    rel("R3-08", "result_supports_claim", "RESULT-INF", "CLAIM-COMPLEMENTARITY",
        ["A-INF-RESULT-POWERLAW", "A-INF-CLAIM-001"], "hard", "indirect"),
]

# ---------------------------------------------------------------------------
# do_not_extract: inline citations + policy + figure/forward refs.
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([
        ("text", "(Huang et al., 2025a)"), ("atom_id", "A-INF-BASELINE-001"),
        ("reason", "inline author-year citation for the OverEncoding baseline; not a knowledge object."),
        ("kind", "citation"),
    ]),
    OrderedDict([
        ("text", "(Yu et al., 2025)"), ("atom_id", "A-INF-BASELINE-001"),
        ("reason", "inline author-year citation for SCONE; not a knowledge object."),
        ("kind", "citation"),
    ]),
    OrderedDict([
        ("pattern", "inline_author_year_citation"),
        ("examples", ["(Huang et al., 2025a)", "(Yu et al., 2025)", "(Vaswani et al., 2017)"]),
        ("reason", "inline author-year citations are not knowledge objects; suppress citation over-extraction consistently with other chapters."),
        ("kind", "citation_policy"),
    ]),
    OrderedDict([
        ("text", "Figure 3 (left)"), ("atom_id", "A-ALLOC-RESULT-USHAPE"),
        ("reason", "figure reference label; the numerical results are read from prose, the figure itself is not an extractable object in this slice."),
        ("kind", "figure_label"),
    ]),
    OrderedDict([
        ("text", "Figure 3 (right)"), ("atom_id", "A-INF-RESULT-POWERLAW"),
        ("reason", "figure reference label; the numerical results are read from prose, the figure itself is not an extractable object in this slice."),
        ("kind", "figure_label"),
    ]),
    OrderedDict([
        ("text", "detailed in Section 2.5"), ("atom_id", "A-INF-SETUP-001"),
        ("reason", "backward cross-reference to Section 2.5 (decoupled storage/compute), outside this chapter slice."),
        ("kind", "out_of_slice_reference"),
    ]),
    OrderedDict([
        ("text", "Section 3.1"), ("atom_id", "A-INF-CLAIM-001"),
        ("reason", "cross-reference to the sibling section 3.1; not itself a knowledge object."),
        ("kind", "out_of_slice_reference"),
    ]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Chapter 3 (Scaling Laws and Sparsity Allocation, sections 3.1-3.2) only"),
    ("title", "Engram: Conditional Memory as a Scalable Axis of Sparsity - Chapter 3 Scaling Laws and Sparsity Allocation"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 110-157; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "article_research"),
    ("profile_cues", ["Compute-matched formulation", "Experimental protocol", "Results and Analysis",
                      "validation loss", "Figure 3", "FLOPs", "U-shaped relationship", "power law"]),
    ("extraction_targets", ["ArticleClaim", "ScalingLaw", "ExperimentSetup", "ExperimentResult",
                            "MechanisticExplanation", "DerivedRuleCandidate"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span: it may rephrase, transliterate math (rho, ->, x for multiplication, delta), and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs not inside the span. EXCEPTION: a formula_atom MAY carry a symbolic interpretation supplied by atoms in metadata.supported_by_context_atoms / interpretation_supported_by."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks needed to fully define it). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only; supporting_context atoms are NOT required to be present in the home package's input."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone. CLAIM-COMPLEMENTARITY is the only cross-package object here (home PKG-INTRO, completed by A-INF-CLAIM-001 in PKG-INF)."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "figure atoms separate physical_section_id from semantic_section_ids; forward references set include_in_chapter2_core_chunks:false and are isolated in a cross_reference_block. Chapter 3 renders no figure as image/table: Figure 3 (left/right) results are read from prose, so 'Figure 3 (left)'/'Figure 3 (right)' labels are listed in do_not_extract as figure_label."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("context_only", "an atom not consumed by any object or relation may be flagged context_only:true; Chapter 3 has no such atoms (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
        ("numeric_audit", "for every non-formula atom: every numeric token in normalized_text must also occur in its raw_text span (no over-span numbers)."),
    ])),
    ("parsing_notes", [
        "Formula tags are globally continuous across the paper; Eq.(7) (allocation ratio) is the ONLY tagged display formula in this chapter, rendered as $$...\\tag{7}$$ on source line 130 (delimiters on lines 129/131).",
        "Figure 3 (left)=allocation U-shape; Figure 3 (right)=infinite-memory power law; neither figure is rendered as <table>/image in the slice, so all result numbers (1.7248->1.7109, rho 75-80%, slot sweep) come from prose.",
        "MinerU renders the dotted section numbers as level-1 headings: '# 3.', '# 3.1.', '# 3.2.'.",
        "Inline variables ($P_{\\text{tot}}$, $P_{\\text{act}}$, $P_{\\text{sparse}} \\triangleq P_{\\text{tot}} - P_{\\text{act}}$, $\\rho \\in [0,1]$) normalize to ASCII (P_tot/P_act/P_sparse/rho); := for \\triangleq.",
        "rho is written with both percent (100%/40%/80%) and the [0,1] fraction in the source; normalized_text keeps the percent form as it appears in each span. The en-dashes in '20%-25%' and '75%-80%' (U+2013) are normalized to ASCII hyphen.",
        "The P_tot/P_act/P_sparse definition is a 3-item bullet list (source lines 121-123) captured as one contiguous span; the two FLOPs regimes are bullets on lines 140-141 inside the setup span (138-143).",
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
        "# Gold fixture v0.3.3 - Engram paper Chapter 3 Scaling Laws and Sparsity Allocation (profile: article_research)\n"
        "# Upgraded from v0.1: dual coordinates (source_span authoritative + viewer_span), verbatim raw_text +\n"
        "# within-span normalized_text, object evidence split into local/supporting + home_package, ONE context_package\n"
        "# per chunk with expected_objects (+ expected_local_fields for the cross-package CLAIM-COMPLEMENTARITY),\n"
        "# gold_label/difficulty/evidence_strength (no decimal confidence), do_not_extract negatives, source_meta\n"
        "# conventions/validation/parsing_notes. Same atom/chunk/object/relation IDs and meaning as v0.1.\n"
        "# Spans are computed by build.py against engram_paper_mineru.md; do NOT hand-edit.\n"
    )
    body = yaml.dump(GOLD, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=10000)
    with open(os.path.join(HERE, "gold.yaml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    print("wrote source.md (%d bytes) and gold.yaml" % len(VIEWER_TEXT))
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d mentions=%d do_not_extract=%d" % (
        len(ATOMS), len(SOURCE_ELEMENTS), len(SEMANTIC_CHUNKS),
        len(CONTEXT_PACKAGES), len(OBJECTS), len(RELATIONS), len(MENTIONS), len(DO_NOT_EXTRACT)))


if __name__ == "__main__":
    main()
