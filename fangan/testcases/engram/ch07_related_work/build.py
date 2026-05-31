#!/usr/bin/env python3
"""Builder for ch07_related_work gold.yaml (schema v0.3.3, profile article_research).

Upgrades the v0.1 fixture to v0.3.3: same atom/chunk/object/relation IDs and
meaning, re-expressed in the v0.3.3 schema with anchor-based verbatim spans.

source.md is a VIEWER-ONLY verbatim slice of the authoritative MinerU file
(lines 322-339, Section 7 Related Work). Per atom we emit source_span
(authoritative, file=source_file) + viewer_span (viewer_only, file=source.md) and
assert source_file[source_span] == raw_text and source.md[viewer_span] == raw_text.

All spans are computed by locating verbatim substrings (anchors); never hand-written.

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

SLICE_LINE_START = 322
SLICE_LINE_END = 339

# ---------------------------------------------------------------------------
# Load source, precompute per-line char offsets into source_file.
# ---------------------------------------------------------------------------
SRC = open(SOURCE_FILE, encoding="utf-8").read()
SRC_LINES = SRC.split("\n")

_LINE_OFF = {}
_acc = 0
for _i, _ln in enumerate(SRC_LINES, start=1):
    _LINE_OFF[_i] = _acc
    _acc += len(_ln) + 1

def src_line_of_offset(off):
    """1-based source line containing char offset `off`."""
    lo, hi = 1, len(SRC_LINES)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _LINE_OFF[mid] <= off:
            lo = mid
        else:
            hi = mid - 1
    return lo

# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice of lines 322-339 + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b} (Section 7 Related Work). "
    "NOT authoritative; all gold coordinates point at {src}. "
    "Figure 7 caption (source line 326) physically falls inside this range but "
    "belongs to Section 6 (gating visualization) and is NOT atomized -> see do_not_extract. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY

_VIEWER_LINES = VIEWER_TEXT.split("\n")
_VIEWER_LINE_OFF = {}
_vacc = 0
for _i, _ln in enumerate(_VIEWER_LINES, start=1):
    _VIEWER_LINE_OFF[_i] = _vacc
    _vacc += len(_ln) + 1

def viewer_line_of_offset(off):
    lo, hi = 1, len(_VIEWER_LINES)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _VIEWER_LINE_OFF[mid] <= off:
            lo = mid
        else:
            hi = mid - 1
    return lo

# ---------------------------------------------------------------------------
# Span helper: locate a verbatim contiguous substring in source_file, emit
# source_span (authoritative) + viewer_span (viewer-only, into source.md).
# ---------------------------------------------------------------------------
def span_for(raw):
    idx = SRC.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND in source: %r" % raw[:70])
    if SRC.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS substring (matches >1): %r" % raw[:70])
    src_cs = idx
    src_ce = idx + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch"
    s_ls = src_line_of_offset(src_cs)
    s_le = src_line_of_offset(src_ce - 1)

    vidx = VIEWER_TEXT.find(raw)
    if vidx < 0:
        raise SystemExit("NOT FOUND in viewer slice: %r" % raw[:70])
    if VIEWER_TEXT.find(raw, vidx + 1) >= 0:
        raise SystemExit("AMBIGUOUS in viewer slice: %r" % raw[:70])
    v_cs = vidx
    v_ce = vidx + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch"
    v_ls = viewer_line_of_offset(v_cs)
    v_le = viewer_line_of_offset(v_ce - 1)

    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", s_ls), ("line_end", s_le),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", v_ls), ("line_end", v_le),
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
# Evidence atoms.  raw_text = verbatim contiguous substrings of source_file.
# normalized_text renders EXACTLY that span (summarize dense inline citations;
# do NOT add facts/names/sections outside the span).
# IDs and meaning preserved from the v0.1 fixture.
# ---------------------------------------------------------------------------
ATOMS = [
    # ---------- N-gram Modeling and Embedding Scaling (SEC-7-NGRAM) ----------
    atom(
        "A-NGRAM-ORIGIN", "SEC-7-NGRAM", "claim_sentence", "SE-7-NGRAM-P1",
        "Originating from Shannon's framework (Shannon, 1948), N-gram models rely on local history to predict tokens, traditionally employing smoothing techniques (Katz, 1987; Kneser and Ney, 1995) to mitigate data sparsity. Despite the paradigm shift toward neural architectures (Bengio et al., 2003) for capturing long-range dependencies, the computational efficiency of N-gram lookups has been preserved in modern representation learning, as exemplified by seminal works like FastText (Bojanowski et al., 2017).",
        "N-gram models, originating from Shannon's framework, predict tokens from local history and use smoothing to mitigate data sparsity; despite the shift to neural architectures, the computational efficiency of N-gram lookups persists in modern representation learning, e.g., FastText.",
        "direct",
    ),
    atom(
        "A-NGRAM-RESURGE", "SEC-7-NGRAM", "claim_sentence", "SE-7-NGRAM-P2",
        "Recently, this paradigm has resurged as embedding scaling. While architectures such as Per-Layer Embeddings (Team, 2025) and DeepEmbed (RWKV Team, 2025) expand capacity via massive tables, a distinct line of pioneering research—most relevant to our approach—integrates compositional N-gram structures directly into the representation space.",
        "The N-gram paradigm has resurged as embedding scaling: Per-Layer Embeddings and DeepEmbed expand capacity via massive tables, while a distinct line of research integrates compositional N-gram structures directly into the representation space.",
        "direct",
    ),
    atom(
        "A-NGRAM-SUPERBPE-SCONE", "SEC-7-NGRAM", "mechanism_sentence", "SE-7-NGRAM-P2",
        "SuperBPE (Liu et al., 2025) and SCONE (Yu et al., 2025) explicitly target high-frequency patterns: the former by merging multi-word expressions into “superword” tokens, and the latter via an auxiliary encoding model.",
        "SuperBPE and SCONE explicitly target high-frequency patterns: SuperBPE merges multi-word expressions into 'superword' tokens, while SCONE uses an auxiliary encoding model.",
        "direct",
    ),
    atom(
        "A-NGRAM-OVERENC-BLT", "SEC-7-NGRAM", "mechanism_sentence", "SE-7-NGRAM-P2",
        "In parallel, OverEncoding (Huang et al., 2025a) and Byte Latent Transformer (BLT) (Pagnoni et al., 2025) adopt hash N-gram embeddings to capture local dependencies at the token and byte levels, respectively. These studies collectively demonstrate the efficacy of scaling parameters through N-gram representations with minimal computational overhead.",
        "OverEncoding and Byte Latent Transformer (BLT) adopt hash N-gram embeddings to capture local dependencies at the token and byte levels respectively, demonstrating that parameters can be scaled through N-gram representations with minimal computational overhead.",
        "direct",
    ),
    atom(
        "A-DIFF1-PRIMITIVE", "SEC-7-NGRAM", "claim_sentence", "SE-7-NGRAM-DIFF1",
        "- First, regarding modeling and evaluation protocols. Prior approaches often treat $N$ -gram embeddings as external augmentations without validating their efficiency under strictly fair comparison protocols. For instance, SCONE (Yu et al., 2025) is inference-focused and relies on auxiliary modules that incur additional training FLOPs. Similarly, OverEncoding (Huang et al., 2025a) fails to yield meaningful improvements on sparse MoE backbones even under a non-isoparametric setting. In contrast, we treat conditional memory as a first-class modeling primitive instantiated via the carefully designed Engram module. By rigorously evaluating this design within our Sparsity Allocation framework, we demonstrate its clear advantage over strictly iso-parameter and iso-FLOPs MoE baselines.",
        "First difference (modeling/evaluation): prior approaches treat N-gram embeddings as external augmentations without strictly fair protocols (SCONE is inference-focused with auxiliary modules incurring extra training FLOPs; OverEncoding fails to improve on sparse MoE backbones). In contrast, we treat conditional memory as a first-class modeling primitive via the Engram module, and within the Sparsity Allocation framework demonstrate clear advantage over strictly iso-parameter and iso-FLOPs MoE baselines.",
        "direct",
    ),
    atom(
        "A-DIFF2-COMUDESIGN", "SEC-7-NGRAM", "mechanism_sentence", "SE-7-NGRAM-DIFF2",
        "- Second, from a system perspective, we advocate for algorithm-system co-design. Existing approaches place embeddings strictly at the input layer (Layer 0), which inherently serializes memory access and computation (Huang et al., 2025a; Yu et al., 2025). Engram, conversely, strategically injects memory into deeper layers to enable communication-computation overlap. Furthermore, by exploiting the inherent Zipfian distribution of $N$ -grams, we could maximize the utility of the hardware memory hierarchy. This holistic design allows Engram to scale to massive parameters with negligible inference overhead.",
        "Second difference (system): existing approaches place embeddings strictly at the input layer (Layer 0), serializing memory access and computation. Engram instead injects memory into deeper layers to enable communication-computation overlap, and exploits the Zipfian distribution of N-grams to maximize use of the hardware memory hierarchy, scaling to massive parameters with negligible inference overhead.",
        "direct",
    ),

    # ---------- Mixture-of-Experts (SEC-7-MOE) ----------
    atom(
        "A-MOE-INTRO", "SEC-7-MOE", "claim_sentence", "SE-7-MOE-P1",
        "MoE architectures decouple model capacity from computational cost by conditionally activating a sparse subset of experts per token, a paradigm introduced by Shazeer et al. (2017). Subsequent innovations such as GShard (Lepikhin et al., 2020), BASE (Lewis et al., 2021), Switch Transformer (Fedus et al., 2022) and GLaM (Du et al., 2022) enabled super-\n\nlinear parameter scaling while maintaining constant inference costs.",
        "MoE architectures decouple model capacity from computational cost by conditionally activating a sparse subset of experts per token (introduced by Shazeer et al., 2017); GShard, BASE, Switch Transformer and GLaM enabled super-linear parameter scaling at constant inference cost.",
        "direct",
    ),
    atom(
        "A-MOE-DEEPSEEK", "SEC-7-MOE", "mechanism_sentence", "SE-7-MOE-P2",
        "More recently, DeepSeek-MoE (Dai et al., 2024) demonstrated superior efficiency, significantly outperforming dense models with equivalent active parameters via fine-grained expert segmentation and shared expert isolation. Adopting this architecture, state-of-the-art models such as DeepSeek-V3 (Liu et al., 2024a) and Kimi-k2 (Team et al., 2025) have further pushed total parameters to hundreds of billions scale.",
        "DeepSeek-MoE outperforms dense models with equivalent active parameters via fine-grained expert segmentation and shared expert isolation; adopting this architecture, DeepSeek-V3 and Kimi-k2 push total parameters to the hundreds-of-billions scale.",
        "direct",
    ),

    # ---------- Memory Network (SEC-7-MEM) ----------
    atom(
        "A-MEM-PARAMETRIC", "SEC-7-MEM", "mechanism_sentence", "SE-7-MEM-P1",
        "Parametric memory methods, such as PKM (Lample et al., 2019), PEER (He, 2024), Selfmem (Cheng et al., 2023b), Memory+ (Berges et al., 2025) and UltraMem (Huang et al., 2025b,c), integrate large-scale, sparse key-value stores directly into the model layers, thereby significantly increasing capacity with negligible impact on FLOPs.",
        "Parametric memory methods (PKM, PEER, Selfmem, Memory+, UltraMem) integrate large-scale sparse key-value stores directly into the model layers, increasing capacity with negligible impact on FLOPs.",
        "direct",
    ),
    atom(
        "A-MEM-NONPARAMETRIC", "SEC-7-MEM", "mechanism_sentence", "SE-7-MEM-P2",
        "Conversely, non-parametric memory approaches like REALM (Guu et al., 2020), RETRO (Borgeaud et al., 2022; Wang et al., 2023), and PlugLM (Cheng et al., 2023a) decouple knowledge storage from model processing, treating the external memory as an editable and scalable key-value store that allows the model to adapt to evolving information without retraining.",
        "Non-parametric memory approaches (REALM, RETRO, PlugLM) decouple knowledge storage from model processing, treating external memory as an editable, scalable key-value store that lets the model adapt to evolving information without retraining.",
        "direct",
    ),

    # ---------- Mechanisms of Knowledge Storage (SEC-7-KNOW) ----------
    atom(
        "A-KNOW-FFN-KV", "SEC-7-KNOW", "mechanism_sentence", "SE-7-KNOW-P1",
        "The Feed-Forward Networks (FFNs) are widely hypothesized to function as Key-Value memories (Geva et al., 2021). Under this framework, the first layer acts as a pattern detector (\"keys\") while the second layer projects specific information into the residual stream (\"values\"). This modularity is evidenced by the identification of specific \"knowledge neurons\" responsible for storing distinct facts (Dai et al., 2022).",
        "FFNs are hypothesized to function as key-value memories: the first layer acts as a pattern detector ('keys') and the second projects information into the residual stream ('values'); specific 'knowledge neurons' are identified as storing distinct facts.",
        "direct",
    ),
    atom(
        "A-KNOW-EDITING", "SEC-7-KNOW", "mechanism_sentence", "SE-7-KNOW-P2",
        "Further validation is provided by causal tracing methodologies, which map the information flow of factual recall to specific FFN layers (Meng et al., 2022). These insights have enabled precise model editing algorithms such as ROME (Meng et al., 2022) and MEMIT (Meng et al., 2023), which allow for the direct update of factual associations without retraining.",
        "Causal tracing maps the information flow of factual recall to specific FFN layers, enabling precise model-editing algorithms such as ROME and MEMIT that directly update factual associations without retraining.",
        "direct",
    ),
    atom(
        "A-KNOW-WORLDMODEL", "SEC-7-KNOW", "claim_sentence", "SE-7-KNOW-P3",
        "Moreover, investigations into internal representations, such as those in Othello-GPT (Li et al., 2023a), suggest that these storage mechanisms may facilitate the emergence of structured \"world models\" rather than mere statistical memorization.",
        "Investigations into internal representations such as Othello-GPT suggest these storage mechanisms may facilitate the emergence of structured 'world models' rather than mere statistical memorization.",
        "direct",
    ),
]

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# source_elements: heading + one paragraph element per source_element_id,
# line ranges derived from the atoms' source_spans.
# ---------------------------------------------------------------------------
def build_source_elements(atoms):
    elems = []
    elems.append(OrderedDict([
        ("id", "SE-H-7"), ("type", "heading"),
        ("file", SOURCE_NAME), ("line_start", 322), ("line_end", 322),
    ]))
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    for se_id, members in groups.items():
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        el = OrderedDict([
            ("id", se_id), ("type", "paragraph"),
            ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le),
        ])
        if se_id in ("SE-7-NGRAM-DIFF1", "SE-7-NGRAM-DIFF2"):
            el["note"] = "markdown bullet item ('- First,' / '- Second,'); Engram's key difference"
        if se_id == "SE-7-MOE-P1":
            el["note"] = "MoE intro; 'super-linear' is hyphen-split across a page break (lines 333/335)"
        elems.append(el)
    return elems

SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ---------------------------------------------------------------------------
# section_tree (same as v0.1).
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-7"), ("path", "7"), ("title", "Related Work")]),
    OrderedDict([("id", "SEC-7-NGRAM"), ("path", "7 > N-gram Modeling and Embedding Scaling"),
                 ("title", "N-gram Modeling and Embedding Scaling"), ("parent", "SEC-7"),
                 ("kind", "run_in_heading")]),
    OrderedDict([("id", "SEC-7-MOE"), ("path", "7 > Mixture-of-Experts"),
                 ("title", "Mixture-of-Experts"), ("parent", "SEC-7"), ("kind", "run_in_heading")]),
    OrderedDict([("id", "SEC-7-MEM"), ("path", "7 > Memory Network"),
                 ("title", "Memory Network"), ("parent", "SEC-7"), ("kind", "run_in_heading")]),
    OrderedDict([("id", "SEC-7-KNOW"), ("path", "7 > Mechanisms of Knowledge Storage"),
                 ("title", "Mechanisms of Knowledge Storage"), ("parent", "SEC-7"),
                 ("kind", "run_in_heading")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks: 4 related_work_comparison_block chunks (same as v0.1).
# ---------------------------------------------------------------------------
SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C-RW-NGRAM"), ("profile", "article_research"),
        ("chunk_type", "related_work_comparison_block"),
        ("section_path", "7 > N-gram Modeling and Embedding Scaling"),
        ("atom_ids", ["A-NGRAM-ORIGIN", "A-NGRAM-RESURGE", "A-NGRAM-SUPERBPE-SCONE",
                      "A-NGRAM-OVERENC-BLT", "A-DIFF1-PRIMITIVE", "A-DIFF2-COMUDESIGN"]),
        ("central_atom_ids", ["A-DIFF1-PRIMITIVE", "A-DIFF2-COMUDESIGN"]),
        ("boundary_reason",
         "N-gram/embedding-scaling prior work and Engram's two key differences kept in one block; "
         "the diff bullets only make sense alongside the prior work they contrast "
         "(SCONE/OverEncoding/Layer-0 placement)."),
        ("extraction_targets", ["RelatedWorkComparison", "ArticleClaim", "SystemDesignClaim"]),
        ("gold_must_cover_atoms", ["A-NGRAM-SUPERBPE-SCONE", "A-NGRAM-OVERENC-BLT",
                                   "A-DIFF1-PRIMITIVE", "A-DIFF2-COMUDESIGN"]),
    ]),
    OrderedDict([
        ("id", "C-RW-MOE"), ("profile", "article_research"),
        ("chunk_type", "related_work_comparison_block"),
        ("section_path", "7 > Mixture-of-Experts"),
        ("atom_ids", ["A-MOE-INTRO", "A-MOE-DEEPSEEK"]),
        ("central_atom_ids", ["A-MOE-INTRO"]),
        ("boundary_reason",
         "MoE lineage (Shazeer -> Switch/GLaM -> DeepSeek-V3/Kimi-k2) as one comparison topic."),
        ("extraction_targets", ["RelatedWorkComparison"]),
        ("gold_must_cover_atoms", ["A-MOE-INTRO", "A-MOE-DEEPSEEK"]),
    ]),
    OrderedDict([
        ("id", "C-RW-MEM"), ("profile", "article_research"),
        ("chunk_type", "related_work_comparison_block"),
        ("section_path", "7 > Memory Network"),
        ("atom_ids", ["A-MEM-PARAMETRIC", "A-MEM-NONPARAMETRIC"]),
        ("central_atom_ids", ["A-MEM-PARAMETRIC", "A-MEM-NONPARAMETRIC"]),
        ("boundary_reason",
         "parametric vs non-parametric memory-network taxonomy kept together as one contrast."),
        ("extraction_targets", ["RelatedWorkComparison"]),
        ("gold_must_cover_atoms", ["A-MEM-PARAMETRIC", "A-MEM-NONPARAMETRIC"]),
    ]),
    OrderedDict([
        ("id", "C-RW-KNOW"), ("profile", "article_research"),
        ("chunk_type", "related_work_comparison_block"),
        ("section_path", "7 > Mechanisms of Knowledge Storage"),
        ("atom_ids", ["A-KNOW-FFN-KV", "A-KNOW-EDITING", "A-KNOW-WORLDMODEL"]),
        ("central_atom_ids", ["A-KNOW-FFN-KV"]),
        ("boundary_reason",
         "knowledge-storage mechanism literature (FFN-as-KV -> editing -> world models) as one topic."),
        ("extraction_targets", ["RelatedWorkComparison", "ArticleClaim"]),
        ("gold_must_cover_atoms", ["A-KNOW-FFN-KV", "A-KNOW-EDITING"]),
    ]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}

# ---------------------------------------------------------------------------
# objects (same IDs/meaning as v0.1; now with home_package + local/supporting
# evidence + gold_label/difficulty/evidence_strength instead of confidence).
# OBJ-DIFF-PRIMITIVE / OBJ-DIFF-CODESIGN / OBJ-RW-NGRAM all live in PKG-RW-NGRAM.
# OBJ-DIFF-PRIMITIVE also references A-MOE-INTRO (the iso-param/iso-FLOPs MoE
# baseline contrast) -> that atom is in C-RW-MOE, so it is a supporting_context atom.
# ---------------------------------------------------------------------------
def obj(oid, otype, section_path, home_package, payload, local, supporting,
        difficulty, evidence_strength):
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
    obj("OBJ-DIFF-PRIMITIVE", "ArticleClaim",
        "7 > N-gram Modeling and Embedding Scaling", "PKG-RW-NGRAM",
        OrderedDict([
            ("claim_type", "comparison"),
            ("statement", "Unlike prior N-gram embedding work that treats embeddings as external augmentations without fair protocols, Engram treats conditional memory as a first-class modeling primitive and shows clear advantage under strictly iso-parameter and iso-FLOPs MoE baselines within the Sparsity Allocation framework."),
            ("contrasted_with", ["SCONE (inference-focused, extra training FLOPs)",
                                  "OverEncoding (fails on sparse MoE backbones)"]),
            ("key_dimension", "modeling and evaluation protocol"),
        ]),
        ["A-DIFF1-PRIMITIVE", "A-NGRAM-SUPERBPE-SCONE", "A-NGRAM-OVERENC-BLT"],
        ["A-MOE-INTRO"], "medium", "direct"),
    obj("OBJ-DIFF-CODESIGN", "SystemDesignClaim",
        "7 > N-gram Modeling and Embedding Scaling", "PKG-RW-NGRAM",
        OrderedDict([
            ("claim_type", "comparison"),
            ("statement", "Unlike prior work placing embeddings strictly at Layer 0 (serializing memory access and computation), Engram injects memory into deeper layers for communication-computation overlap and exploits the Zipfian distribution of N-grams to maximize hardware-memory-hierarchy utility, scaling to massive parameters with negligible inference overhead."),
            ("contrasted_with", ["Layer-0 input-embedding placement (Huang et al., 2025a; Yu et al., 2025)"]),
            ("key_dimension", "algorithm-system co-design"),
        ]),
        ["A-DIFF2-COMUDESIGN"], [], "medium", "direct"),
    obj("OBJ-RW-NGRAM", "RelatedWorkComparison",
        "7 > N-gram Modeling and Embedding Scaling", "PKG-RW-NGRAM",
        OrderedDict([
            ("topic", "N-gram modeling and embedding scaling"),
            ("prior_work", ["FastText", "Per-Layer Embeddings", "DeepEmbed",
                            "SuperBPE", "SCONE", "OverEncoding", "BLT"]),
            ("summary", "N-gram lookups from Shannon onward resurge as embedding scaling; compositional N-gram structures integrated into representation space target high-frequency patterns with minimal compute overhead."),
        ]),
        ["A-NGRAM-ORIGIN", "A-NGRAM-RESURGE", "A-NGRAM-SUPERBPE-SCONE", "A-NGRAM-OVERENC-BLT"],
        [], "medium", "direct"),
    obj("OBJ-RW-MOE", "RelatedWorkComparison",
        "7 > Mixture-of-Experts", "PKG-RW-MOE",
        OrderedDict([
            ("topic", "Mixture-of-Experts"),
            ("prior_work", ["Shazeer et al. 2017", "GShard", "BASE", "Switch Transformer",
                            "GLaM", "DeepSeek-MoE", "DeepSeek-V3", "Kimi-k2"]),
            ("summary", "MoE decouples capacity from compute via sparse expert activation, enabling super-linear parameter scaling at constant inference cost; recent fine-grained/shared-expert designs scale to hundreds of billions of parameters."),
        ]),
        ["A-MOE-INTRO", "A-MOE-DEEPSEEK"], [], "medium", "direct"),
    obj("OBJ-RW-MEM", "RelatedWorkComparison",
        "7 > Memory Network", "PKG-RW-MEM",
        OrderedDict([
            ("topic", "Memory networks"),
            ("prior_work", ["PKM", "PEER", "Selfmem", "Memory+", "UltraMem",
                            "REALM", "RETRO", "PlugLM"]),
            ("taxonomy", ["parametric (key-value stores inside layers)",
                          "non-parametric (external editable key-value store)"]),
            ("summary", "Memory-augmented networks expand capacity without proportional compute, split into parametric (in-layer KV stores) and non-parametric (decoupled external memory) approaches."),
        ]),
        ["A-MEM-PARAMETRIC", "A-MEM-NONPARAMETRIC"], [], "medium", "direct"),
    obj("OBJ-RW-KNOW", "RelatedWorkComparison",
        "7 > Mechanisms of Knowledge Storage", "PKG-RW-KNOW",
        OrderedDict([
            ("topic", "Mechanisms of knowledge storage"),
            ("prior_work", ["FFN-as-KV (Geva et al., 2021)", "knowledge neurons (Dai et al., 2022)",
                            "causal tracing (Meng et al., 2022)", "ROME", "MEMIT", "Othello-GPT"]),
            ("summary", "FFNs hypothesized as key-value memories with identifiable knowledge neurons; causal tracing enables ROME/MEMIT editing; Othello-GPT suggests emergent structured world models."),
        ]),
        ["A-KNOW-FFN-KV", "A-KNOW-EDITING", "A-KNOW-WORLDMODEL"], [], "medium", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk (4 chunks -> 4 packages). atoms == chunk.atom_ids.
# expected_objects = objects whose home_package is this package.
# OBJ-DIFF-PRIMITIVE has supporting evidence A-MOE-INTRO (in C-RW-MOE) -> its home
# package PKG-RW-NGRAM declares expected_local_fields for the fields derivable
# from the N-gram atoms alone (everything except the MoE-baseline detail).
# ---------------------------------------------------------------------------
PKG_FOR_CHUNK = {
    "C-RW-NGRAM": "PKG-RW-NGRAM",
    "C-RW-MOE": "PKG-RW-MOE",
    "C-RW-MEM": "PKG-RW-MEM",
    "C-RW-KNOW": "PKG-RW-KNOW",
}
DOC_TITLE = "Engram: Conditional Memory as a Complementary Sparsity Axis"

def build_package(pid, chunk_id, section_path, linked_context, extraction_targets,
                  expected_objects, expected_local_fields=None):
    d = OrderedDict([
        ("id", pid), ("profile", "article_research"), ("chunk_id", chunk_id),
        ("section_path", section_path), ("document_title", DOC_TITLE),
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
        "PKG-RW-NGRAM", "C-RW-NGRAM",
        "Section 7 Related Work > N-gram Modeling and Embedding Scaling",
        OrderedDict([
            ("previous_heading", "6. Analysis (gating visualization, Figure 7)"),
            ("next_heading", "Mixture-of-Experts"),
            ("formula_context", "Engram's first difference is grounded in the Sparsity Allocation framework introduced in Section 3 (iso-param/iso-FLOPs comparison)."),
        ]),
        ["RelatedWorkComparison", "ArticleClaim", "SystemDesignClaim"],
        ["OBJ-DIFF-PRIMITIVE", "OBJ-DIFF-CODESIGN", "OBJ-RW-NGRAM"],
        # OBJ-DIFF-PRIMITIVE spans into C-RW-MOE (A-MOE-INTRO); the fields below are
        # what the N-gram package's atoms alone support (the iso-param/iso-FLOPs MoE
        # baseline reference is corroborated by the MoE package).
        OrderedDict([
            ("OBJ-DIFF-PRIMITIVE", ["claim_type", "statement", "contrasted_with", "key_dimension"]),
        ]),
    ),
    build_package(
        "PKG-RW-MOE", "C-RW-MOE", "Section 7 Related Work > Mixture-of-Experts",
        OrderedDict([
            ("previous_heading", "N-gram Modeling and Embedding Scaling"),
            ("next_heading", "Memory Network"),
        ]),
        ["RelatedWorkComparison"], ["OBJ-RW-MOE"],
    ),
    build_package(
        "PKG-RW-MEM", "C-RW-MEM", "Section 7 Related Work > Memory Network",
        OrderedDict([
            ("previous_heading", "Mixture-of-Experts"),
            ("next_heading", "Mechanisms of Knowledge Storage"),
        ]),
        ["RelatedWorkComparison"], ["OBJ-RW-MEM"],
    ),
    build_package(
        "PKG-RW-KNOW", "C-RW-KNOW", "Section 7 Related Work > Mechanisms of Knowledge Storage",
        OrderedDict([
            ("previous_heading", "Memory Network"),
            ("next_heading", "8. Conclusion"),
        ]),
        ["RelatedWorkComparison", "ArticleClaim"], ["OBJ-RW-KNOW"],
    ),
]

# ---------------------------------------------------------------------------
# mentions (same 24 as v0.1).
# ---------------------------------------------------------------------------
def mention(mid, text, mtype, atom_id, key):
    return OrderedDict([("id", mid), ("text", text), ("type", mtype),
                        ("atom_id", atom_id), ("canonical_key", key)])

MENTIONS = [
    mention("M-01", "FastText", "RelatedWork", "A-NGRAM-ORIGIN", "fasttext"),
    mention("M-02", "Per-Layer Embeddings", "RelatedWork", "A-NGRAM-RESURGE", "per_layer_embeddings"),
    mention("M-03", "DeepEmbed", "RelatedWork", "A-NGRAM-RESURGE", "deepembed"),
    mention("M-04", "SuperBPE", "RelatedWork", "A-NGRAM-SUPERBPE-SCONE", "superbpe"),
    mention("M-05", "SCONE", "RelatedWork", "A-NGRAM-SUPERBPE-SCONE", "scone"),
    mention("M-06", "OverEncoding", "RelatedWork", "A-NGRAM-OVERENC-BLT", "overencoding"),
    mention("M-07", "Byte Latent Transformer", "RelatedWork", "A-NGRAM-OVERENC-BLT", "blt"),
    mention("M-08", "conditional memory", "ArchitectureComponent", "A-DIFF1-PRIMITIVE", "conditional_memory"),
    mention("M-09", "Sparsity Allocation", "Concept", "A-DIFF1-PRIMITIVE", "sparsity_allocation"),
    mention("M-10", "algorithm-system co-design", "SystemDesignClaim", "A-DIFF2-COMUDESIGN", "algorithm_system_codesign"),
    mention("M-11", "Zipfian distribution", "Concept", "A-DIFF2-COMUDESIGN", "zipfian_distribution"),
    mention("M-12", "Mixture-of-Experts", "RelatedWork", "A-MOE-INTRO", "mixture_of_experts"),
    mention("M-13", "DeepSeek-MoE", "RelatedWork", "A-MOE-DEEPSEEK", "deepseek_moe"),
    mention("M-14", "DeepSeek-V3", "RelatedWork", "A-MOE-DEEPSEEK", "deepseek_v3"),
    mention("M-15", "Kimi-k2", "RelatedWork", "A-MOE-DEEPSEEK", "kimi_k2"),
    mention("M-16", "PKM", "RelatedWork", "A-MEM-PARAMETRIC", "pkm"),
    mention("M-17", "UltraMem", "RelatedWork", "A-MEM-PARAMETRIC", "ultramem"),
    mention("M-18", "REALM", "RelatedWork", "A-MEM-NONPARAMETRIC", "realm"),
    mention("M-19", "RETRO", "RelatedWork", "A-MEM-NONPARAMETRIC", "retro"),
    mention("M-20", "FFN as key-value memories", "MechanisticExplanation", "A-KNOW-FFN-KV", "ffn_key_value_memory"),
    mention("M-21", "knowledge neurons", "MechanisticExplanation", "A-KNOW-FFN-KV", "knowledge_neurons"),
    mention("M-22", "ROME", "RelatedWork", "A-KNOW-EDITING", "rome"),
    mention("M-23", "MEMIT", "RelatedWork", "A-KNOW-EDITING", "memit"),
    mention("M-24", "Othello-GPT", "RelatedWork", "A-KNOW-WORLDMODEL", "othello_gpt"),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "conditional_memory"),
                 ("aliases", ["conditional memory", "first-class modeling primitive", "Engram module"]),
                 ("note", "Engram treats conditional memory as a first-class modeling primitive.")]),
    OrderedDict([("canonical", "parametric_memory"),
                 ("aliases", ["parametric memory", "PKM", "PEER", "UltraMem", "Memory+"]),
                 ("note", "key-value stores embedded directly inside model layers.")]),
    OrderedDict([("canonical", "non_parametric_memory"),
                 ("aliases", ["non-parametric memory", "REALM", "RETRO", "PlugLM"]),
                 ("note", "knowledge storage decoupled from model processing.")]),
    OrderedDict([("canonical", "scone"),
                 ("aliases", ["SCONE", "auxiliary encoding model"]),
                 ("note", "inference-focused, incurs extra training FLOPs.")]),
]

# ---------------------------------------------------------------------------
# relations (same 4 IDs/types/endpoints as v0.1; confidence -> label triple).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])

RELATIONS = [
    rel("R-01", "claim_extends_prior_work", "OBJ-DIFF-PRIMITIVE", "OBJ-RW-NGRAM",
        ["A-DIFF1-PRIMITIVE"], "medium", "direct"),
    rel("R-02", "claim_contrasts_with_work", "OBJ-DIFF-PRIMITIVE", "OBJ-RW-NGRAM",
        ["A-DIFF1-PRIMITIVE", "A-NGRAM-SUPERBPE-SCONE", "A-NGRAM-OVERENC-BLT"], "medium", "direct"),
    rel("R-03", "claim_contrasts_with_work", "OBJ-DIFF-CODESIGN", "OBJ-RW-NGRAM",
        ["A-DIFF2-COMUDESIGN"], "medium", "direct"),
    rel("R-04", "claim_contrasts_with_work", "OBJ-DIFF-PRIMITIVE", "OBJ-RW-MOE",
        ["A-DIFF1-PRIMITIVE", "A-MOE-INTRO"], "hard", "indirect"),
]

# ---------------------------------------------------------------------------
# do_not_extract: Figure 7 caption (in slice, cross-section), the dense inline
# author-year citations, and the inline_author_year_citation policy with examples.
# ---------------------------------------------------------------------------
FIG7_TEXT = SRC_LINES[326 - 1]
DO_NOT_EXTRACT = [
    OrderedDict([
        ("text", "Figure 7 | Visualization of the gating mechanism of Engram."),
        ("source_lines", [326, 326]),
        ("reason", "Figure 7 caption physically falls inside the Section 7 line range (322-339) but belongs to Section 6 (gating visualization); it is a cross-section element and is NOT a Related Work atom."),
        ("kind", "cross_section_forward_reference"),
    ]),
    OrderedDict([
        ("text", "(Shannon, 1948)"),
        ("atom_id", "A-NGRAM-ORIGIN"),
        ("reason", "inline author-year citation, not a knowledge object."),
        ("kind", "citation"),
    ]),
    OrderedDict([
        ("text", "(Yu et al., 2025)"),
        ("atom_id", "A-NGRAM-SUPERBPE-SCONE"),
        ("reason", "inline author-year citation, not a knowledge object."),
        ("kind", "citation"),
    ]),
    OrderedDict([
        ("text", "(Huang et al., 2025a; Yu et al., 2025)"),
        ("atom_id", "A-DIFF2-COMUDESIGN"),
        ("reason", "inline author-year citations attached to the Layer-0 placement claim; not knowledge objects."),
        ("kind", "citation"),
    ]),
    OrderedDict([
        ("text", "(Geva et al., 2021)"),
        ("atom_id", "A-KNOW-FFN-KV"),
        ("reason", "inline author-year citation, not a knowledge object."),
        ("kind", "citation"),
    ]),
    OrderedDict([
        ("pattern", "inline_author_year_citation"),
        ("examples", ["(Shannon, 1948)", "(Katz, 1987; Kneser and Ney, 1995)", "(Bengio et al., 2003)",
                      "(Bojanowski et al., 2017)", "(Team, 2025)", "(RWKV Team, 2025)",
                      "(Liu et al., 2025)", "(Yu et al., 2025)", "(Huang et al., 2025a)",
                      "(Pagnoni et al., 2025)", "Shazeer et al. (2017)", "(Lepikhin et al., 2020)",
                      "(Dai et al., 2024)", "(Liu et al., 2024a)", "(Team et al., 2025)",
                      "(Lample et al., 2019)", "(He, 2024)", "(Guu et al., 2020)",
                      "(Borgeaud et al., 2022; Wang et al., 2023)", "(Geva et al., 2021)",
                      "(Dai et al., 2022)", "(Meng et al., 2022)", "(Meng et al., 2023)",
                      "(Li et al., 2023a)"]),
        ("reason", "Related Work prose is saturated with inline (Author, Year) citations; they anchor prior-work mentions but are NOT themselves knowledge objects. This policy suppresses citation over-extraction across the whole section."),
        ("kind", "citation_policy"),
    ]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Section 7 (Related Work) only"),
    ("title", "Engram: Conditional Memory as a Complementary Sparsity Axis - Section 7 Related Work"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 322-339; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "article_research"),
    ("profile_cues", ["Related Work", "(Shannon, 1948)", "Mixture-of-Experts", "Memory Network",
                      "our work diverges fundamentally in two key dimensions"]),
    ("extraction_targets", ["ArticleClaim", "SystemDesignClaim", "RelatedWorkComparison",
                            "MechanisticExplanation"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file (Related Work prose, including dense inline (Author, Year) citations kept verbatim). normalized_text renders EXACTLY that span: it may summarize/condense the citations and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs not inside the span. There are no formula atoms in this section."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks needed to fully define it). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only; supporting_context atoms are NOT required to be present in the home package's input."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone. Here OBJ-DIFF-PRIMITIVE (home PKG-RW-NGRAM) references A-MOE-INTRO from the MoE chunk as supporting context."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "figure atoms separate physical_section_id from semantic_section_ids; forward/cross references are isolated. Figure 7 (source line 326) physically falls in the Section 7 line range but belongs to Section 6, so it is NOT atomized and is recorded in do_not_extract."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref. Section 7 has none."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("context_only", "an atom not consumed by any object or relation may be flagged context_only:true; Section 7 has no such atoms (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "# 7. Related Work (source line 322); the four topics use run-in bold lead-ins (N-gram Modeling and Embedding Scaling. / Mixture-of-Experts. / Memory Network. / Mechanisms of Knowledge Storage.) rather than separate markdown headings -> SEC-7-NGRAM/MOE/MEM/KNOW.",
        "No formulas, no tables; citations appear densely as (Author, Year) -> see do_not_extract inline_author_year_citation policy.",
        "Engram's two key differences are a markdown bullet list ('- First, ...' line 330 / '- Second, ...' line 331), the highest-value claims of the section (A-DIFF1-PRIMITIVE / A-DIFF2-COMUDESIGN).",
        "The MoE intro (A-MOE-INTRO) is hyphen-split across a page break: 'super-' ends line 333 and 'linear ...' resumes on line 335 (blank line 334 in between); the source_span is contiguous across that break.",
        "Figure 7 caption (source line 326) physically sits between the two N-gram paragraphs but belongs to Section 6 (gating visualization); excluded from atoms and listed in do_not_extract as cross_section_forward_reference.",
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
        "# Gold fixture v0.3.3 - Engram paper Section 7 Related Work (profile: article_research)\n"
        "# Upgraded from v0.1: same atom/chunk/object/relation IDs and meaning, re-expressed in the\n"
        "# v0.3.3 schema. Adds dual coordinates (source_span authoritative + viewer_span viewer_only),\n"
        "# verbatim raw_text + within-span normalized_text (dense inline citations kept verbatim in raw,\n"
        "# summarized in normalized), object evidence split into local/supporting + home_package, one\n"
        "# context_package per related_work_comparison_block chunk with expected_objects, and\n"
        "# gold_label/difficulty/evidence_strength (no decimal confidence). do_not_extract covers the\n"
        "# Figure 7 cross-section caption and the inline_author_year_citation policy.\n"
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
