#!/usr/bin/env python3
"""Builder for ch06_analysis gold.yaml (schema v0.3.3, profile article_research).

Regenerates source.md (viewer-only verbatim slice of engram_paper_mineru.md lines
224-321) and gold.yaml from the authoritative MinerU source file. Every atom carries
dual spans: source_span (authoritative, file=source_file) + viewer_span
(viewer_only, file=source.md). raw_text is a verbatim contiguous substring of the
source slice; normalized_text renders EXACTLY that span. Spans are located by
verbatim substring search (anchored to the slice line window) and asserted, never
hand-written.

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

SLICE_LINE_START = 224
SLICE_LINE_END = 321

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

# char window of the authoritative slice (used to disambiguate substring search)
SLICE_CHAR_START = _LINE_OFF[SLICE_LINE_START]
SLICE_CHAR_END = _LINE_OFF[SLICE_LINE_END] + len(SRC_LINES[SLICE_LINE_END - 1])

# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice of lines 224-321 + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY


def _line_of_offset(text, off):
    """1-based line number containing char offset `off` in `text`."""
    return text.count("\n", 0, off) + 1


def _src_line(off):
    return _line_of_offset(SRC, off)


def _viewer_line(off):
    return _line_of_offset(VIEWER_TEXT, off)


# ---------------------------------------------------------------------------
# Span helper: locate a verbatim substring inside the slice window and emit
# source_span (authoritative) + viewer_span. Requires uniqueness within slice.
# ---------------------------------------------------------------------------
def span_for(raw):
    idx = SRC.find(raw, SLICE_CHAR_START, SLICE_CHAR_END)
    if idx < 0:
        raise SystemExit("NOT FOUND in slice: %r" % raw[:70])
    if SRC.find(raw, idx + 1, SLICE_CHAR_END) >= 0:
        raise SystemExit("AMBIGUOUS substring (matches >1 in slice): %r" % raw[:70])
    src_cs = idx
    src_ce = idx + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch"

    # viewer offset: same raw text in VIEWER_TEXT (after header). Require unique too.
    vidx = VIEWER_TEXT.find(raw)
    if vidx < 0:
        raise SystemExit("NOT FOUND in viewer: %r" % raw[:70])
    if VIEWER_TEXT.find(raw, vidx + 1) >= 0:
        raise SystemExit("AMBIGUOUS substring in viewer: %r" % raw[:70])
    v_cs = vidx
    v_ce = vidx + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch"

    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", _src_line(src_cs)), ("line_end", _src_line(src_ce - 1)),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", _viewer_line(v_cs)), ("line_end", _viewer_line(v_ce - 1)),
        ("char_start", v_cs), ("char_end", v_ce),
        ("viewer_only", True),
    ])
    return source_span, viewer_span


def atom(aid, section_id, atom_type, raw, normalized, evidence_strength, se_id, metadata=None):
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


# ===========================================================================
# Evidence atoms. raw_text = verbatim contiguous substrings of the slice.
# ===========================================================================
ATOMS = [
    # ---------- 6.0 section intro (line 226) ----------
    atom("A-INTRO", "SEC-6", "claim_sentence",
         "In this section, we investigate the internal mechanisms of Engram, including its effective depth (Section 6.1), core module design (Section 6.2), and parametric sensitivity (Section 6.3). Additionally, we evaluate the inference throughput with offloading (Section 6.4) and conclude with a case study (Section 6.5).",
         "Section 6 investigates Engram's internal mechanisms: effective depth (6.1), core module design (6.2), parametric sensitivity (6.3), inference throughput with offloading (6.4), and a case study (6.5).",
         "direct", "SE-6-INTRO"),

    # ---------- 6.1 hypothesis + Table 3 (lines 230,232,240,242) ----------
    atom("A-DEPTH-MOTIV", "SEC-6.1", "mechanism_sentence",
         "Current LLMs lack a dedicated knowledge lookup primitive and they rely on computation to simulate memory recall. As shown in Table 3, to recognize the entity \"Diana, Princess of Wales\", an LLM must consume multiple layers of Attention and FFNs to progressively compose features",
         "Current LLMs lack a dedicated knowledge lookup primitive and rely on computation to simulate memory recall; as shown in Table 3, recognizing the entity \"Diana, Princess of Wales\" requires consuming multiple layers of Attention and FFNs to progressively compose features.",
         "direct", "SE-6.1-P1"),
    atom("A-DEPTH-HYP", "SEC-6.1", "claim_sentence",
         "we posit that by equipping the model with an explicit knowledge lookup capability, Engram effectively mimics an increase in model depth by relieving the model of the early stages of feature composition.",
         "We posit that by equipping the model with an explicit knowledge lookup capability, Engram effectively mimics an increase in model depth by relieving the model of the early stages of feature composition.",
         "direct", "SE-6.1-P2"),
    atom("A-DEPTH-TOOLS", "SEC-6.1", "method_sentence",
         "To validate this hypothesis, we employ two mechanistic interpretability tools: LogitLens (Belrose et al., 2023; nostalgebraist, 2020) and Centered Kernel Alignment analysis (CKA) (Davari et al., 2023; Kornblith et al., 2019).",
         "To validate this hypothesis, two mechanistic interpretability tools are employed: LogitLens and Centered Kernel Alignment analysis (CKA).",
         "direct", "SE-6.1-P2"),
    # Table 3
    atom("A-T3-CAP", "SEC-6.1", "table_caption_atom",
         "Table 3 | Entity resolution example reproduced from Ghandeharioun et al. (2024). This table illustrates how LLMs gradually integrate context tokens through layers of attention and FFNs to construct the internal representation of the entity: \"Diana, Princess of Wales\". The \"Latent State Translation\" column displays the automatically generated text for the last token: \"Wales\" by PatchScope (Ghandeharioun et al., 2024), while the \"Explanation\" column presents the manual interpretation provided by the original authors.",
         "Table 3: Entity resolution example (reproduced from Ghandeharioun et al. 2024) showing how LLMs gradually integrate context tokens through layers of attention and FFNs to construct the internal representation of \"Diana, Princess of Wales\"; the \"Latent State Translation\" column is PatchScope-generated text for the last token \"Wales\", and the \"Explanation\" column is the original authors' manual interpretation.",
         "direct", "SE-6.1-T3CAP", metadata={"table_id": "Table 3"}),
    atom("A-T3-HDR", "SEC-6.1", "table_header_atom",
         "<tr><td>Layer</td><td>Latent State Translation</td><td>Explanation</td></tr>",
         "Layer | Latent State Translation | Explanation",
         "direct", "SE-6.1-T3", metadata={"table_id": "Table 3"}),
    atom("A-T3-R12", "SEC-6.1", "table_row_atom",
         "<tr><td>1-2</td><td>: Country in the United Kingdom</td><td>Wales</td></tr>",
         "Layers 1-2: 'Country in the United Kingdom' -> Explanation: Wales.",
         "direct", "SE-6.1-T3", metadata={"table_id": "Table 3"}),
    atom("A-T3-R4", "SEC-6.1", "table_row_atom",
         "<tr><td>4</td><td>: Title held by female sovereigns in their own right or by queens consort</td><td>Princess of Wales (unspecific)</td></tr>",
         "Layer 4: 'Title held by female sovereigns in their own right or by queens consort' -> Explanation: Princess of Wales (unspecific).",
         "direct", "SE-6.1-T3", metadata={"table_id": "Table 3"}),
    atom("A-T3-R6", "SEC-6.1", "table_row_atom",
         "<tr><td>6</td><td>: Diana, Princess of Wales (1961-1997), the first wife of Prince Charles, Prince of Wales, who was famous for her beauty and humanitarian work</td><td>Diana, Princess of Wales</td></tr>",
         "Layer 6: 'Diana, Princess of Wales (1961-1997), the first wife of Prince Charles...' -> Explanation: Diana, Princess of Wales (fully resolved only by layer 6).",
         "direct", "SE-6.1-T3", metadata={"table_id": "Table 3"}),

    # ---------- 6.1.1 LogitLens (lines 236,238,244) ----------
    atom("A-LL-METHOD", "SEC-6.1.1", "method_sentence",
         "By projecting each intermediate layer's hidden state with the final LM Head, we compute the Kullback–Leibler divergence (Kullback and Leibler, 1951) between the intermediate output distribution and the model's final output distribution. This metric quantifies how close a latent representation is to being “prediction-ready”",
         "LogitLens projects each intermediate layer's hidden state with the final LM Head and computes the Kullback-Leibler divergence between the intermediate output distribution and the model's final output distribution; this metric quantifies how close a latent representation is to being 'prediction-ready'.",
         "direct", "SE-6.1.1-P1"),
    atom("A-LL-RESULT", "SEC-6.1.1", "result_sentence",
         "Figure 4 (a) reports the layer-wise KL divergence. Compared to the MoE baseline, both Engram variants exhibit systematically smaller KL divergence, with the most pronounced gap",
         "Figure 4(a) reports the layer-wise KL divergence: compared to the MoE baseline, both Engram variants exhibit systematically smaller KL divergence, with the most pronounced gap (appearing in the early blocks).",
         "direct", "SE-6.1.1-P2", metadata={"figure": "Figure 4(a)"}),
    atom("A-LL-INTERP", "SEC-6.1.1", "mechanism_sentence",
         "The steeper descent in the Engram curves indicates that the model finishes feature composition much faster. This observation aligns with our hypothesis: by accessing external knowledge explicitly, Engram reduces the computational steps required, thereby reaching high-confidence, valid predictions earlier in the network hierarchy.",
         "The steeper descent in the Engram curves indicates the model finishes feature composition much faster: by accessing external knowledge explicitly, Engram reduces the computational steps required, reaching high-confidence valid predictions earlier in the network hierarchy.",
         "direct", "SE-6.1.1-P2"),

    # ---------- 6.1.2 CKA + effective depth (lines 248,251,254,256,259,262,266,268,270) ----------
    atom("A-CKA-METHOD", "SEC-6.1.2", "method_sentence",
         "where $K = XX^{\\top}$ and $L = YY^{\\top}$ denote the Gram matrices (using a linear kernel) and HSIC is Hilbert-Schmidt Independence Criterion (Gretton et al., 2005). We employ a minibatch implementation with an unbiased estimator of HSIC (Davari et al., 2023) and evaluate on the Few-NERD dataset (Ding et al., 2021), extracting hidden states corresponding to the final token of named entities.",
         "K = X X^T and L = Y Y^T are the linear-kernel Gram matrices, and HSIC is the Hilbert-Schmidt Independence Criterion; a minibatch unbiased HSIC estimator is used, evaluated on the Few-NERD dataset using hidden states of the final token of named entities.",
         "direct", "SE-6.1.2-P2"),
    atom("A-CKA-EQ8", "SEC-6.1.2", "formula_atom",
         "\\mathrm{CKA} (K, L) = \\frac {\\mathrm{HSIC} (K , L)}{\\sqrt {\\mathrm{HSIC} (K , K) \\mathrm{HSIC} (L , L)}} \\tag {8}",
         "CKA(K,L) = HSIC(K,L) / sqrt( HSIC(K,K) HSIC(L,L) )",
         "direct", "SE-6.1.2-EQ8",
         metadata={"tag": "8", "role": "cka_definition", "supported_by_context_atoms": ["A-CKA-METHOD"]}),
    atom("A-CKA-EQ9", "SEC-6.1.2", "formula_atom",
         "a _ {j} = \\frac {\\sum_ {i \\in \\mathcal {I} _ {j}} S _ {i , j} \\cdot i}{\\sum_ {i \\in \\mathcal {I} _ {j}} S _ {i , j}}, \\quad \\text {where} \\mathcal {I} _ {j} = \\underset {i} {\\operatorname{argtop}} k (S _ {i, j}). \\tag {9}",
         "a_j = ( sum_{i in I_j} S_{i,j} * i ) / ( sum_{i in I_j} S_{i,j} ), where I_j = argtop-k_i(S_{i,j})",
         "direct", "SE-6.1.2-EQ9",
         metadata={"tag": "9", "role": "soft_alignment_index", "supported_by_context_atoms": ["A-CKA-AINDEX"]}),
    atom("A-CKA-AINDEX", "SEC-6.1.2", "method_sentence",
         "we first compute the pairwise CKA similarity matrix $S \\in [0,1]^{L \\times L}$ , where L is the number of layers. We then introduce a soft alignment index $a_{j}$ , defined as the weighted centroid of the top-k most similar MoE layers for each Engram layer j:",
         "The pairwise CKA similarity matrix S in [0,1]^(LxL) (L = number of layers) is computed; the soft alignment index a_j is the weighted centroid of the top-k most similar MoE layers for each Engram layer j.",
         "direct", "SE-6.1.2-P3"),
    atom("A-CKA-AINDEX2", "SEC-6.1.2", "method_sentence",
         "serves as a robust proxy for the “effective MoE depth” corresponding to Engram layer j, utilizing top-k filtering (with k = 5) to mitigate low-similarity noise.",
         "a_j serves as a robust proxy for the 'effective MoE depth' corresponding to Engram layer j, using top-k filtering (k = 5) to mitigate low-similarity noise.",
         "direct", "SE-6.1.2-P4"),
    atom("A-CKA-RESULT", "SEC-6.1.2", "result_sentence",
         "We observe a distinct upward shift from the diagonal, meaning that $a_{j} > j$ for a wide range of layers. For instance, the representations formed at layer 5 of Engram-27B align most closely with those at approximately layer 12 of the MoE baseline.",
         "A distinct upward shift from the diagonal means a_j > j for a wide range of layers; e.g. representations at layer 5 of Engram-27B align most closely with approximately layer 12 of the MoE baseline.",
         "direct", "SE-6.1.2-P5", metadata={"figure": "Figure 4(b-c)"}),
    atom("A-DEPTH-CONCL", "SEC-6.1.2", "claim_sentence",
         "The consistent off-diagonal shift, which aligns with the LogitLens results (Section 6.1.1), confirms that Engram achieves deeper representations at earlier layers. This validates our central hypothesis: by bypassing early-stage feature composition via explicit lookups, Engram is functionally equivalent to increasing the model's effective depth.",
         "The consistent off-diagonal shift, aligning with the LogitLens results, confirms Engram achieves deeper representations at earlier layers: by bypassing early-stage feature composition via explicit lookups, Engram is functionally equivalent to increasing the model's effective depth.",
         "direct", "SE-6.1.2-P6"),

    # ---------- 6.2 Structural Ablation (lines 274,276,278,280,282,284,286,288) ----------
    atom("A-ABL-SETUP", "SEC-6.2", "experiment_setup_atom",
         "Unless otherwise specified, the backbone is a 12-layer 3B MoE model (0.56B activated parameters) trained for 100B tokens. Figure 5 reports validation loss. The dashed orange line denotes the 3B MoE baseline (Val Loss = 1.808).",
         "The ablation backbone is a 12-layer 3B MoE model (0.56B activated parameters) trained for 100B tokens; Figure 5 reports validation loss, with the 3B MoE baseline at Val Loss = 1.808.",
         "direct", "SE-6.2-P1"),
    atom("A-ABL-REFCFG", "SEC-6.2", "experiment_setup_atom",
         "We augment the backbone with a fixed 1.6B-parameter Engram memory. Our reference model uses $\\{2,3\\}$ -grams and inserts Engram at Layers 2 and 6, achieving Val Loss = 1.768, a substantial improvement over the MoE baseline ( $\\Delta = 0.04$ ). All structural ablations below are defined relative to this reference.",
         "The reference configuration augments the backbone with a fixed 1.6B-parameter Engram memory using {2,3}-grams inserted at Layers 2 and 6, achieving Val Loss = 1.768 (Delta = 0.04 vs the MoE baseline); all structural ablations are defined relative to this reference.",
         "direct", "SE-6.2-P2"),
    atom("A-ABL-TRADEOFF", "SEC-6.2", "mechanism_sentence",
         "Injecting Engram early allows it to offload local pattern reconstruction before the backbone expends computational depth, aligning with the backbone's natural hierarchical processing (Ghandeharioun et al., 2024; Jin et al., 2025; Li and Subramani, 2025; Tenney et al., 2019). However, this incurs a cost in gating precision: early hidden states have not yet aggregated sufficient global context via attention, and the parallel branches lack the representational divergence required for fine-grained modulation (Xie et al., 2025; Zhu et al., 2025). Consequently, optimal placement requires balancing (i) offloading static local patterns early and (ii) utilizing stronger contextual queries for gating later.",
         "Injecting Engram early offloads local pattern reconstruction before the backbone expends computational depth, but early hidden states have not yet aggregated sufficient global context via attention (hurting gating precision); optimal placement balances (i) offloading static local patterns early and (ii) stronger contextual queries for gating later.",
         "direct", "SE-6.2-P4"),
    atom("A-ABL-LAYER2", "SEC-6.2", "ablation_finding_atom",
         "The sweep shows that Layer 2 achieves the best single-layer performance (Val Loss = 1.770), outperforming Layer 1 and degrading as the insertion point moves deeper. This indicates that one round of attention is already sufficient to provide a meaningfully contextualized $h_{t}$ for gating, while still being early enough to replace the backbone's bottom-layer local aggregation.",
         "Sweeping a single Engram module, Layer 2 achieves the best single-layer performance (Val Loss = 1.770), outperforming Layer 1 and degrading as insertion moves deeper; one round of attention already yields a meaningfully contextualized h_t for gating.",
         "direct", "SE-6.2-P5", metadata={"figure": "Figure 5 (Layer Sweep)"}),
    atom("A-ABL-LAYER26", "SEC-6.2", "ablation_finding_atom",
         "we find that dividing the same 1.6B memory into two smaller modules (achieved by reducing the embedding dimension $d_{mem}$ ) and placing them at Layers 2 and 6 performs even better (Val Loss = 1.768). This layered design reconciles the trade-off by combining early intervention with rich, late-stage contextual gating. More importantly, layered insertion also provides a practical system advantage, enabling better utilization of the memory hierarchy",
         "Dividing the same 1.6B memory into two smaller modules (reduced d_mem) at Layers 2 and 6 performs even better (Val Loss = 1.768), combining early intervention with rich late-stage contextual gating and improving utilization of the memory hierarchy.",
         "direct", "SE-6.2-P6"),
    atom("A-ABL-COMPONENTS", "SEC-6.2", "ablation_finding_atom",
         "We find that three components yield the most significant gains: (i) branch-specific fusion within the multi-branch backbone, (ii) context-aware gating, and (iii) tokenizer compression. Removing any of these causes the largest regressions in validation loss. Specifically, for the “w/o multi branch” ablation, we retain the mHC backbone structure but replace the branch-specific gating with a single Engram fusion applied to the hidden states after the pre-mapping $H^{pre}$",
         "Three components yield the most significant gains and the largest validation-loss regressions when removed: (i) branch-specific fusion within the multi-branch backbone, (ii) context-aware gating, and (iii) tokenizer compression. ('w/o multi branch' replaces branch-specific gating with a single Engram fusion on the hidden states H^pre.)",
         "direct", "SE-6.2-P7", metadata={"figure": "Figure 5 (Component Ablation markers)"}),
    atom("A-ABL-MINOR", "SEC-6.2", "ablation_finding_atom",
         "Other changes have smaller effects: removing the lightweight depthwise convolution only marginally degrades performance. Allocating capacity to 4-grams is slightly suboptimal under a fixed 1.6B budget—likely because it dilutes capacity from the more frequent 2/3-gram patterns—though we do not rule out that higher-order N-grams become beneficial at larger memory scales.",
         "Smaller effects: removing the lightweight depthwise convolution only marginally degrades performance; allocating capacity to 4-grams is slightly suboptimal under a fixed 1.6B budget (dilutes capacity from more frequent 2/3-gram patterns), though higher-order N-grams may help at larger memory scales.",
         "direct", "SE-6.2-P8"),

    # ---------- 6.3 Sensitivity (lines 292,294,298) ----------
    atom("A-SENS-METHOD", "SEC-6.3", "experiment_setup_atom",
         "we evaluate the model by completely suppressing the sparse embedding output during inference while keeping the backbone unchanged. Crucially, this post-hoc ablation induces a training-inference inconsistency, potentially introducing noise in complex, mixed-capability tasks. Consequently, we prioritize the analysis of Factual Knowledge and Reading Comprehension—the two extremes of the sensitivity spectrum—which exhibit the highest signal-to-noise ratio under this stress test.",
         "The model is evaluated by completely suppressing the sparse embedding (Engram) output during inference while keeping the backbone unchanged; this post-hoc ablation induces a training-inference inconsistency, so analysis prioritizes Factual Knowledge and Reading Comprehension, the two extremes with the highest signal-to-noise ratio.",
         "direct", "SE-6.3-P1"),
    atom("A-SENS-FACTUAL", "SEC-6.3", "result_sentence",
         "benchmarks suffer a catastrophic collapse, retaining only 29–44% of the original performance (e.g., TriviaQA at 29%), confirming that the Engram module acts as the primary repository for parametric knowledge.",
         "Factual-knowledge benchmarks suffer a catastrophic collapse, retaining only 29-44% of original performance (e.g. TriviaQA at 29%), confirming Engram acts as the primary repository for parametric knowledge.",
         "direct", "SE-6.3-P2", metadata={"figure": "Figure 6"}),
    atom("A-SENS-READING", "SEC-6.3", "result_sentence",
         "Conversely, reading comprehension tasks are remarkably resilient, retaining 81–93% (e.g., C3 at 93%), suggesting that context-grounded tasks rely primarily on the backbone's attention mechanism rather than Engram.",
         "Reading-comprehension tasks are remarkably resilient, retaining 81-93% (e.g. C3 at 93%), suggesting context-grounded tasks rely primarily on the backbone's attention mechanism rather than Engram.",
         "direct", "SE-6.3-P2", metadata={"figure": "Figure 6"}),

    # ---------- 6.4 System Efficiency (lines 302,304,306,308,310,312,314) ----------
    atom("A-SYS-DETERMINISTIC", "SEC-6.4", "claim_sentence",
         "A pivotal system advantage of Engram over routing-based MoE is that its sparse activations are addressed by explicit, static hash IDs. This yields a strictly deterministic memory access pattern: indices for the next Engram lookup are fixed once the token sequence is known and can be computed before the corresponding layer executes.",
         "A pivotal system advantage of Engram over routing-based MoE: its sparse activations are addressed by explicit, static hash IDs, yielding a strictly deterministic memory access pattern whose indices are fixed once the token sequence is known and can be computed before the corresponding layer executes.",
         "direct", "SE-6.4-P1"),
    atom("A-SYS-SETUP", "SEC-6.4", "experiment_setup_atom",
         "we benchmark on two dense backbones (Dense-4B and Dense-8B). We insert a massive 100B-parameter Engram layer into the second Transformer block, with the entire embedding table resident in host DRAM. During inference, the system prefetches embeddings for the Engram layer asynchronously, overlapping the PCIe transfer with the computation of the first block.",
         "Benchmarked on two dense backbones (Dense-4B and Dense-8B): a 100B-parameter Engram layer is inserted into the second Transformer block with the entire embedding table resident in host DRAM, prefetched asynchronously to overlap the PCIe transfer with the first block's computation.",
         "direct", "SE-6.4-P2"),
    atom("A-SYS-HARNESS", "SEC-6.4", "experiment_setup_atom",
         "We implemented an inference harness based on nano-vLLM $^{1}$ —a streamlined prototype of the industry-standard vLLM engine (Kwon et al., 2023).",
         "The inference harness is based on nano-vLLM, a streamlined prototype of the industry-standard vLLM engine.",
         "direct", "SE-6.4-P2"),
    atom("A-SYS-RESULT", "SEC-6.4", "result_sentence",
         "As detailed in Table 4, offloading a 100B-parameter embedding table incurs a negligible throughput penalty, peaking at only 2.8% on the 8B backbone. This confirms that the compute intensity of early dense blocks provides a sufficient temporal window to mask the retrieval latency. Crucially, the effective communication volume per step scales with the number of activated slots rather than the total embedding table size.",
         "Offloading a 100B-parameter embedding table incurs a negligible throughput penalty, peaking at only 2.8% on the 8B backbone; the compute intensity of early dense blocks provides enough temporal window to mask retrieval latency, and per-step communication volume scales with the number of activated slots rather than the total embedding table size.",
         "direct", "SE-6.4-P3", metadata={"figure": "Table 4"}),
    atom("A-SYS-CONSERVATIVE", "SEC-6.4", "claim_sentence",
         "this experiment serves as a conservative baseline. While the hierarchical design in Section 2.5 exploits Zipfian locality to cache frequent items in HBM, our experimental setup forces all retrievals to traverse the PCIe bus from host memory.",
         "This experiment is a conservative baseline: unlike the hierarchical design that exploits Zipfian locality to cache frequent items in HBM, the setup forces all retrievals to traverse the PCIe bus from host memory.",
         "direct", "SE-6.4-P4"),
    atom("A-SYS-CONSERVATIVE2", "SEC-6.4", "claim_sentence",
         "retrieval strategy yields minimal overhead strongly suggests that a fully optimized, locality-aware implementation would incur negligible throughput penalty.",
         "That this baseline retrieval strategy yields minimal overhead strongly suggests a fully optimized, locality-aware implementation would incur negligible throughput penalty.",
         "direct", "SE-6.4-P5"),
    # Table 4
    atom("A-T4-CAP", "SEC-6.4", "table_caption_atom",
         "Table 4 | End-to-end Inference Throughput. We measure infernce throughput with a 100B-parameter Engram layer entirely offloaded to host memory.",
         "Table 4: End-to-end inference throughput, measured with a 100B-parameter Engram layer entirely offloaded to host memory.",
         "direct", "SE-6.4-T4CAP", metadata={"table_id": "Table 4"}),
    atom("A-T4-HDR", "SEC-6.4", "table_header_atom",
         "<tr><td>Base Model</td><td>Configuration</td><td>Throughput (tok/s)</td></tr>",
         "Base Model | Configuration | Throughput (tok/s)",
         "direct", "SE-6.4-T4", metadata={"table_id": "Table 4"}),
    atom("A-T4-R4B", "SEC-6.4", "table_row_atom",
         "<tr><td rowspan=\"2\">4B-Dense</td><td>Baseline</td><td>9,031.62</td></tr><tr><td>+ 100B Engram (CPU Offload)</td><td>8,858.28</td></tr>",
         "4B-Dense: Baseline = 9,031.62 tok/s; + 100B Engram (CPU Offload) = 8,858.28 tok/s (penalty ~1.9%).",
         "direct", "SE-6.4-T4", metadata={"table_id": "Table 4"}),
    atom("A-T4-R8B", "SEC-6.4", "table_row_atom",
         "<tr><td rowspan=\"2\">8B-Dense</td><td>Baseline</td><td>6,315.52</td></tr><tr><td>+ 100B Engram (CPU Offload)</td><td>6,140.02</td></tr>",
         "8B-Dense: Baseline = 6,315.52 tok/s; + 100B Engram (CPU Offload) = 6,140.02 tok/s (peak penalty 2.8%).",
         "direct", "SE-6.4-T4", metadata={"table_id": "Table 4"}),
    atom("A-T4-SETUP", "SEC-6.4", "table_row_atom",
         "<tr><td>Hardware</td><td></td><td>NVIDIA H800</td></tr><tr><td>Workload</td><td></td><td>512 Sequences</td></tr><tr><td>Sequence Length</td><td></td><td>Uniform(100, 1024)</td></tr>",
         "Experimental setup: Hardware = NVIDIA H800; Workload = 512 Sequences; Sequence Length = Uniform(100, 1024).",
         "direct", "SE-6.4-T4", metadata={"table_id": "Table 4"}),

    # ---------- 6.5 Case Study (lines 318,320) ----------
    atom("A-CASE-SETUP", "SEC-6.5", "method_sentence",
         "In Section 2.3, we introduced the context-aware gating mechanism, designed to dynamically modulate the integration of retrieved static memory into the backbone. To empirically validate whether Engram behaves as intended, we visualize the gating scalar $\\alpha_{t}$ of Engram-27B $^{2}$ across various samples in Figure 7.",
         "To validate the context-aware gating mechanism (which dynamically modulates the integration of retrieved static memory into the backbone), the gating scalar alpha_t of Engram-27B is visualized across samples in Figure 7.",
         "direct", "SE-6.5-P1", metadata={"figure": "Figure 7"}),
    atom("A-CASE-EN", "SEC-6.5", "result_sentence",
         "The gating mechanism consistently activates (shown in red) upon completing local, static patterns. In English, we observe strong activations on multi-token named entities (e.g., “Alexander the Great”, “the Milky Way”) and formulaic phrases (e.g., “By the way”, “Princess of Wales”).",
         "The gating mechanism consistently activates (red) upon completing local static patterns; in English, strong activations on multi-token named entities (e.g. 'Alexander the Great', 'the Milky Way') and formulaic phrases (e.g. 'By the way', 'Princess of Wales').",
         "direct", "SE-6.5-P2", metadata={"figure": "Figure 7"}),
    atom("A-CASE-ZH", "SEC-6.5", "result_sentence",
         "This behavior generalizes effectively across languages. In the Chinese examples, Engram identifies and retrieves distinct idiomatic expressions and historical entities, such as “Four Great Inventions” (四大发明) and “Zhang Zhongjing” (张仲景).",
         "The behavior generalizes across languages; in the Chinese examples Engram identifies idiomatic expressions and historical entities such as 'Four Great Inventions' (sida famming) and 'Zhang Zhongjing'.",
         "direct", "SE-6.5-P2", metadata={"figure": "Figure 7", "note": "Chinese originals transliterated to ASCII: sida famming, Zhang Zhongjing"}),
    atom("A-CASE-CONCL", "SEC-6.5", "claim_sentence",
         "These qualitative results confirm that Engram successfully identifies and handles stereotyped linguistic dependencies, effectively relieving the Transformer backbone from memorizing these static associations.",
         "These qualitative results confirm Engram successfully identifies and handles stereotyped linguistic dependencies, relieving the Transformer backbone from memorizing these static associations.",
         "direct", "SE-6.5-P2"),
]

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}
ATOM_SECTION = {a["id"]: a["section_id"] for a in ATOMS}

# ===========================================================================
# source_elements: heading + content elements. Built so that every atom's
# source_element_id is present; line ranges cover the member atoms (or the
# heading/formula/table line).
# ===========================================================================
HEADINGS = [
    ("SE-H-6", 224), ("SE-H-6.1", 228), ("SE-H-6.1.1", 234), ("SE-H-6.1.2", 246),
    ("SE-H-6.2", 272), ("SE-H-6.3", 290), ("SE-H-6.4", 300), ("SE-H-6.5", 316),
]
# explicit content elements (id -> (type, line_start, line_end, metadata))
CONTENT_ELEMENTS = [
    ("SE-6-INTRO", "paragraph", 226, 226, None),
    ("SE-6.1-P1", "paragraph", 230, 230, None),
    ("SE-6.1-P2", "paragraph", 232, 232, None),
    ("SE-6.1-T3CAP", "paragraph", 240, 240, None),
    ("SE-6.1-T3", "table", 242, 242, {"table_id": "Table 3"}),
    ("SE-6.1.1-P1", "paragraph", 236, 236, None),
    ("SE-6.1.1-P2", "paragraph", 238, 244, {"note": "prose split by Table 3 insertion (lines 238 + 244)"}),
    ("SE-6.1.2-P2", "paragraph", 254, 254, None),
    ("SE-6.1.2-EQ8", "formula", 250, 252, {"tag": "8"}),
    ("SE-6.1.2-EQ9", "formula", 258, 260, {"tag": "9"}),
    ("SE-6.1.2-P3", "paragraph", 256, 256, None),
    ("SE-6.1.2-P4", "paragraph", 262, 266, {"note": "prose split by Figure 5 insertion (lines 262 + 266)"}),
    ("SE-6.1.2-P5", "paragraph", 268, 268, None),
    ("SE-6.1.2-P6", "paragraph", 270, 270, None),
    ("SE-6.2-P1", "paragraph", 274, 274, None),
    ("SE-6.2-P2", "paragraph", 276, 276, None),
    ("SE-6.2-P4", "paragraph", 280, 280, None),
    ("SE-6.2-P5", "paragraph", 282, 282, None),
    ("SE-6.2-P6", "paragraph", 284, 284, None),
    ("SE-6.2-P7", "paragraph", 286, 286, None),
    ("SE-6.2-P8", "paragraph", 288, 288, None),
    ("SE-6.3-P1", "paragraph", 292, 292, None),
    ("SE-6.3-P2", "paragraph", 294, 298, {"note": "prose split by Figure 6 insertion (lines 294 + 298)"}),
    ("SE-6.4-P1", "paragraph", 302, 302, None),
    ("SE-6.4-P2", "paragraph", 304, 304, None),
    ("SE-6.4-P3", "paragraph", 306, 306, None),
    ("SE-6.4-P4", "paragraph", 308, 308, None),
    ("SE-6.4-P5", "paragraph", 314, 314, None),
    ("SE-6.4-T4CAP", "paragraph", 310, 310, None),
    ("SE-6.4-T4", "table", 312, 312, {"table_id": "Table 4"}),
    ("SE-6.5-P1", "paragraph", 318, 318, None),
    ("SE-6.5-P2", "paragraph", 320, 320, None),
]


def build_source_elements():
    elems = []
    for hid, ln in HEADINGS:
        elems.append(OrderedDict([
            ("id", hid), ("type", "heading"),
            ("file", SOURCE_NAME), ("line_start", ln), ("line_end", ln),
        ]))
    for cid, ctype, ls, le, meta in CONTENT_ELEMENTS:
        d = OrderedDict([
            ("id", cid), ("type", ctype),
            ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le),
        ])
        if meta:
            d["metadata"] = meta
        elems.append(d)
    return elems


SOURCE_ELEMENTS = build_source_elements()
SE_IDS = {e["id"] for e in SOURCE_ELEMENTS}
# sanity: every atom's source_element_id exists
for a in ATOMS:
    assert a["source_element_id"] in SE_IDS, "missing SE: %s" % a["source_element_id"]

# ===========================================================================
# section_tree
# ===========================================================================
SECTION_TREE = [
    OrderedDict([("id", "SEC-6"), ("path", "6"), ("title", "Analysis")]),
    OrderedDict([("id", "SEC-6.1"), ("path", "6 > 6.1"), ("title", "Is Engram functionally equivalent to increasing the model's depth?"), ("parent", "SEC-6")]),
    OrderedDict([("id", "SEC-6.1.1"), ("path", "6 > 6.1 > 6.1.1"), ("title", "Accelerated Prediction Convergence"), ("parent", "SEC-6.1")]),
    OrderedDict([("id", "SEC-6.1.2"), ("path", "6 > 6.1 > 6.1.2"), ("title", "Representational Alignment and Effective Depth"), ("parent", "SEC-6.1")]),
    OrderedDict([("id", "SEC-6.2"), ("path", "6 > 6.2"), ("title", "Structural Ablation and Layer Sensitivity"), ("parent", "SEC-6")]),
    OrderedDict([("id", "SEC-6.3"), ("path", "6 > 6.3"), ("title", "Sensitivity Analysis"), ("parent", "SEC-6")]),
    OrderedDict([("id", "SEC-6.4"), ("path", "6 > 6.4"), ("title", "System Efficiency"), ("parent", "SEC-6")]),
    OrderedDict([("id", "SEC-6.5"), ("path", "6 > 6.5"), ("title", "Case Study: Gating Visualization"), ("parent", "SEC-6")]),
]

# ===========================================================================
# semantic_chunks
# ===========================================================================
def chunk(cid, ctype, section_path, atom_ids, central, boundary, targets, must_cover):
    return OrderedDict([
        ("id", cid), ("profile", "article_research"), ("chunk_type", ctype),
        ("section_path", section_path), ("atom_ids", atom_ids),
        ("central_atom_ids", central), ("boundary_reason", boundary),
        ("extraction_targets", targets), ("gold_must_cover_atoms", must_cover),
    ])


SEMANTIC_CHUNKS = [
    chunk("C-INTRO", "article_core_claim_block", "6",
          ["A-INTRO"], ["A-INTRO"],
          "Section roadmap; one short overview unit listing the five analysis threads.",
          ["Implication"], ["A-INTRO"]),
    chunk("C-DEPTH-LL", "ablation_finding_block", "6 > 6.1 > 6.1.1",
          ["A-DEPTH-MOTIV", "A-DEPTH-HYP", "A-DEPTH-TOOLS", "A-T3-CAP", "A-T3-HDR", "A-T3-R12", "A-T3-R4", "A-T3-R6", "A-LL-METHOD", "A-LL-RESULT", "A-LL-INTERP"],
          ["A-DEPTH-HYP", "A-LL-RESULT", "A-LL-INTERP"],
          "Effective-depth hypothesis + the Table 3 motivating example + the LogitLens method/result/interpretation kept together (mechanism_explains_result dependency); table caption/header/rows stay with their analysis.",
          ["MechanisticExplanation", "ExperimentResult"],
          ["A-DEPTH-HYP", "A-T3-R6", "A-LL-RESULT", "A-LL-INTERP"]),
    chunk("C-DEPTH-CKA", "ablation_finding_block", "6 > 6.1 > 6.1.2",
          ["A-CKA-METHOD", "A-CKA-EQ8", "A-CKA-EQ9", "A-CKA-AINDEX", "A-CKA-AINDEX2", "A-CKA-RESULT", "A-DEPTH-CONCL"],
          ["A-CKA-RESULT", "A-DEPTH-CONCL"],
          "CKA definition (Eq.8) + soft-alignment index (Eq.9) + a_j>j result + effective-depth conclusion held together (formula-explanation continuity, mechanism_explains_result).",
          ["MechanisticExplanation", "Formula"],
          ["A-CKA-EQ8", "A-CKA-EQ9", "A-CKA-RESULT", "A-DEPTH-CONCL"]),
    chunk("C-ABL", "ablation_finding_block", "6 > 6.2",
          ["A-ABL-SETUP", "A-ABL-REFCFG", "A-ABL-TRADEOFF", "A-ABL-LAYER2", "A-ABL-LAYER26", "A-ABL-COMPONENTS", "A-ABL-MINOR"],
          ["A-ABL-REFCFG", "A-ABL-COMPONENTS"],
          "Controlled ablation: setup + reference config + placement trade-off + layer sweep + component importance all share the same baseline (experiment_uses_setup, ablation_supports_component_importance).",
          ["AblationFinding", "ExperimentSetup", "ArchitectureComponent"],
          ["A-ABL-REFCFG", "A-ABL-LAYER2", "A-ABL-LAYER26", "A-ABL-COMPONENTS"]),
    chunk("C-SENS", "ablation_finding_block", "6 > 6.3",
          ["A-SENS-METHOD", "A-SENS-FACTUAL", "A-SENS-READING"],
          ["A-SENS-FACTUAL", "A-SENS-READING"],
          "Suppression-at-inference setup + the factual-vs-reading dichotomy results (setup-result dependency).",
          ["AblationFinding", "ExperimentResult", "MechanisticExplanation"],
          ["A-SENS-FACTUAL", "A-SENS-READING"]),
    chunk("C-SYS", "system_efficiency_block", "6 > 6.4",
          ["A-SYS-DETERMINISTIC", "A-SYS-SETUP", "A-SYS-HARNESS", "A-SYS-RESULT", "A-SYS-CONSERVATIVE", "A-SYS-CONSERVATIVE2", "A-T4-CAP", "A-T4-HDR", "A-T4-SETUP", "A-T4-R4B", "A-T4-R8B"],
          ["A-SYS-DETERMINISTIC", "A-SYS-RESULT"],
          "Deterministic-hash-ID claim enables the offloading design; setup + Table 4 throughput results + conservative-baseline caveat kept together (system_design_enables_efficiency, setup-result-table dependency).",
          ["SystemDesignClaim", "ExperimentResult", "ExperimentSetup"],
          ["A-SYS-DETERMINISTIC", "A-SYS-RESULT", "A-T4-R4B", "A-T4-R8B"]),
    chunk("C-CASE", "ablation_finding_block", "6 > 6.5",
          ["A-CASE-SETUP", "A-CASE-EN", "A-CASE-ZH", "A-CASE-CONCL"],
          ["A-CASE-EN", "A-CASE-CONCL"],
          "Gating-visualization case study: method + English/Chinese activation examples + conclusion form one qualitative finding.",
          ["MechanisticExplanation", "ArchitectureComponent"],
          ["A-CASE-EN", "A-CASE-ZH", "A-CASE-CONCL"]),
]

CHUNK_BY_ID = {c["id"]: c for c in SEMANTIC_CHUNKS}
CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}
# atom -> chunk id (each atom in exactly one chunk)
ATOM_CHUNK = {}
for c in SEMANTIC_CHUNKS:
    for aid in c["atom_ids"]:
        ATOM_CHUNK[aid] = c["id"]

# ===========================================================================
# context_packages: ONE per chunk. atoms == chunk.atom_ids.
# ===========================================================================
PKG_FOR_CHUNK = {
    "C-INTRO": "PKG-INTRO",
    "C-DEPTH-LL": "PKG-DEPTH-LL",
    "C-DEPTH-CKA": "PKG-EFFECTIVE-DEPTH",
    "C-ABL": "PKG-ABLATION",
    "C-SENS": "PKG-SENSITIVITY",
    "C-SYS": "PKG-SYSTEM",
    "C-CASE": "PKG-CASE",
}
CHUNK_FOR_PKG = {v: k for k, v in PKG_FOR_CHUNK.items()}

PKG_META = {
    "PKG-INTRO": ("6", ["Implication"],
                  OrderedDict([("previous_heading", "5 Experiments"), ("next_heading", "6.1 Is Engram functionally equivalent to increasing the model's depth?")])),
    "PKG-DEPTH-LL": ("6 > 6.1 > 6.1.1 Accelerated Prediction Convergence", ["MechanisticExplanation", "ExperimentResult"],
                     OrderedDict([
                         ("table_caption", "Table 3: layer-wise entity resolution for 'Diana, Princess of Wales' (PatchScope Latent State Translation + manual Explanation)."),
                         ("figure", "Figure 4(a): layer-wise KL divergence; both Engram variants below MoE baseline, steepest in early blocks."),
                         ("previous_heading", "6.1 Is Engram functionally equivalent to increasing the model's depth?"),
                         ("next_heading", "6.1.2 Representational Alignment and Effective Depth"),
                     ])),
    "PKG-EFFECTIVE-DEPTH": ("6 > 6.1 > 6.1.2 Representational Alignment and Effective Depth", ["MechanisticExplanation", "Formula"],
                            OrderedDict([
                                ("formula_context", "Eq.(8) CKA defines layer-pair similarity; Eq.(9) a_j is the top-k weighted centroid used to read effective MoE depth."),
                                ("figure", "Figure 4(b-c): CKA heatmap with soft-alignment curve a_j showing upward shift a_j > j (Engram layer 5 ~ MoE layer 12)."),
                                ("previous_heading", "6.1.1 Accelerated Prediction Convergence"),
                                ("next_heading", "6.2 Structural Ablation and Layer Sensitivity"),
                            ])),
    "PKG-ABLATION": ("6 > 6.2 Structural Ablation and Layer Sensitivity", ["AblationFinding", "ExperimentSetup", "ArchitectureComponent"],
                     OrderedDict([
                         ("figure", "Figure 5: dark-blue Layer Sweep curve (Layer 2 optimal) + right-side Component Ablation markers; MoE baseline Val Loss 1.808."),
                         ("previous_heading", "6.1.2 Representational Alignment and Effective Depth"),
                         ("next_heading", "6.3 Sensitivity Analysis"),
                     ])),
    "PKG-SENSITIVITY": ("6 > 6.3 Sensitivity Analysis", ["AblationFinding", "ExperimentResult", "MechanisticExplanation"],
                        OrderedDict([
                            ("figure", "Figure 6: retained performance under Engram ablation; factual knowledge collapses, reading comprehension preserved."),
                            ("previous_heading", "6.2 Structural Ablation and Layer Sensitivity"),
                            ("next_heading", "6.4 System Efficiency"),
                        ])),
    "PKG-SYSTEM": ("6 > 6.4 System Efficiency", ["SystemDesignClaim", "ExperimentResult", "ExperimentSetup"],
                   OrderedDict([
                       ("table_caption", "Table 4: end-to-end inference throughput with a 100B Engram layer offloaded to host memory."),
                       ("table_headers", "Base Model | Configuration | Throughput (tok/s); setup: NVIDIA H800, 512 sequences, Uniform(100,1024)."),
                       ("previous_heading", "6.3 Sensitivity Analysis"),
                       ("next_heading", "6.5 Case Study: Gating Visualization"),
                   ])),
    "PKG-CASE": ("6 > 6.5 Case Study: Gating Visualization", ["MechanisticExplanation", "ArchitectureComponent"],
                 OrderedDict([
                     ("figure", "Figure 7: gating scalar alpha_t of Engram-27B across English/Chinese samples; red on completed static patterns."),
                     ("previous_heading", "6.4 System Efficiency"),
                     ("next_heading", "7 Related Work"),
                 ])),
}

DOC_TITLE = "Engram: Conditional Memory for Language Models"


def build_package(pid, expected_objects, expected_local_fields=None):
    chunk_id = CHUNK_FOR_PKG[pid]
    section_path, targets, linked = PKG_META[pid]
    d = OrderedDict([
        ("id", pid), ("profile", "article_research"), ("chunk_id", chunk_id),
        ("section_path", section_path), ("document_title", DOC_TITLE),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])]) for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", linked),
        ("extraction_targets", targets),
        ("expected_objects", expected_objects),
    ])
    if expected_local_fields:
        d["expected_local_fields"] = expected_local_fields
    return d

# ===========================================================================
# objects
# ===========================================================================
def obj(oid, otype, section_path, home_package, payload, local, supporting, difficulty, evidence_strength):
    return OrderedDict([
        ("id", oid), ("type", otype), ("section_path", section_path),
        ("home_package", home_package), ("payload", payload),
        ("local_evidence_atom_ids", local),
        ("supporting_context_atom_ids", supporting),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


OBJECTS = [
    # --- Implication (intro) ---
    obj("IMPL-SCOPE", "Implication", "6", "PKG-INTRO",
        OrderedDict([("statement", "Section 6 establishes Engram's effective-depth mechanism (6.1), validates its core module design via ablation (6.2), characterizes its parametric-knowledge role (6.3), shows offloading is cheap (6.4), and visualizes gating (6.5).")]),
        ["A-INTRO"], [], "easy", "direct"),

    # --- MechanisticExplanation: effective depth (cross-package: spans 6.1.1 + 6.1.2) ---
    obj("MECH-EFFECTIVE-DEPTH", "MechanisticExplanation", "6 > 6.1", "PKG-EFFECTIVE-DEPTH",
        OrderedDict([
            ("claim", "Engram is functionally equivalent to increasing the model's effective depth."),
            ("mechanism", "An explicit knowledge lookup relieves the model of early-stage feature composition, so high-confidence predictions are reached earlier in the hierarchy."),
            ("evidence", ["LogitLens: lower early-layer KL divergence => faster prediction convergence",
                          "CKA: soft alignment a_j > j, e.g. Engram layer 5 ~ MoE layer 12"]),
        ]),
        ["A-CKA-RESULT", "A-DEPTH-CONCL"],
        ["A-DEPTH-HYP", "A-LL-INTERP"], "hard", "direct"),
    obj("MECH-CONVERGENCE", "MechanisticExplanation", "6 > 6.1 > 6.1.1", "PKG-DEPTH-LL",
        OrderedDict([
            ("name", "accelerated prediction convergence"),
            ("finding", "Both Engram variants show systematically smaller layer-wise KL divergence than the MoE baseline, with the largest gap in early blocks (Figure 4a)."),
            ("tool", "LogitLens (KL divergence of intermediate vs final output distribution)"),
            ("validation_tools", "LogitLens and CKA (two mechanistic interpretability tools)"),
        ]),
        ["A-DEPTH-TOOLS", "A-LL-METHOD", "A-LL-RESULT", "A-LL-INTERP"], [], "medium", "direct"),
    obj("RESULT-ENTITY-RESOLUTION", "ExperimentResult", "6 > 6.1 > 6.1.1", "PKG-DEPTH-LL",
        OrderedDict([
            ("name", "Table 3 entity-resolution example (reproduced from Ghandeharioun et al. 2024)"),
            ("finding", "An LLM resolves the entity 'Diana, Princess of Wales' only gradually across layers (layers 1-2: a UK country; layer 4: unspecific title; layer 6: fully resolved), illustrating that recognition consumes multiple layers of attention/FFNs."),
            ("method", "PatchScope Latent State Translation of the last token 'Wales' plus the authors' manual Explanation, read layer by layer."),
            ("motivates", "the need for an explicit knowledge-lookup primitive (effective-depth hypothesis)."),
        ]),
        ["A-DEPTH-MOTIV", "A-T3-CAP", "A-T3-HDR", "A-T3-R12", "A-T3-R4", "A-T3-R6"], [], "medium", "indirect"),
    obj("MECH-ALIGNMENT", "MechanisticExplanation", "6 > 6.1 > 6.1.2", "PKG-EFFECTIVE-DEPTH",
        OrderedDict([
            ("name", "representational alignment / effective depth"),
            ("finding", "a_j > j for a wide layer range; layer 5 of Engram-27B aligns with ~layer 12 of the MoE baseline."),
            ("tool", "CKA (Eq.8) with soft alignment index a_j (Eq.9), top-k=5, on Few-NERD"),
        ]),
        ["A-CKA-METHOD", "A-CKA-EQ8", "A-CKA-EQ9", "A-CKA-AINDEX", "A-CKA-AINDEX2", "A-CKA-RESULT"], [], "medium", "direct"),
    obj("MECH-FUNCTIONAL-DICHOTOMY", "MechanisticExplanation", "6 > 6.3", "PKG-SENSITIVITY",
        OrderedDict([
            ("name", "factual vs reading-comprehension dichotomy"),
            ("finding", "Suppressing Engram collapses factual knowledge (retain 29-44%, TriviaQA 29%) but reading comprehension is resilient (retain 81-93%, C3 93%)."),
            ("interpretation", "Engram is the primary repository for parametric knowledge; context-grounded tasks rely on backbone attention."),
        ]),
        ["A-SENS-FACTUAL", "A-SENS-READING"], [], "medium", "direct"),
    obj("MECH-GATING-SELECTIVITY", "MechanisticExplanation", "6 > 6.5", "PKG-CASE",
        OrderedDict([
            ("name", "gating selectivity on static patterns"),
            ("finding", "alpha_t activates on completed local static patterns: English named entities/phrases ('Alexander the Great', 'Princess of Wales') and Chinese idioms/entities ('Four Great Inventions', 'Zhang Zhongjing')."),
            ("interpretation", "Engram handles stereotyped linguistic dependencies, relieving the backbone from memorizing static associations."),
        ]),
        ["A-CASE-SETUP", "A-CASE-EN", "A-CASE-ZH", "A-CASE-CONCL"], [], "medium", "direct"),

    # --- Formula ---
    obj("FORMULA-CKA", "Formula", "6 > 6.1 > 6.1.2", "PKG-EFFECTIVE-DEPTH",
        OrderedDict([
            ("name", "Centered Kernel Alignment"),
            ("expression", "CKA(K,L) = HSIC(K,L)/sqrt(HSIC(K,K) HSIC(L,L))"),
            ("variables", OrderedDict([
                ("K", "X X^T linear-kernel Gram matrix"),
                ("L", "Y Y^T Gram matrix"),
                ("HSIC", "Hilbert-Schmidt Independence Criterion"),
            ])),
        ]),
        ["A-CKA-EQ8", "A-CKA-METHOD"], [], "medium", "direct"),
    obj("FORMULA-ALIGNMENT-INDEX", "Formula", "6 > 6.1 > 6.1.2", "PKG-EFFECTIVE-DEPTH",
        OrderedDict([
            ("name", "soft alignment index"),
            ("expression", "a_j = (sum_{i in I_j} S_{i,j} i)/(sum_{i in I_j} S_{i,j}), I_j = argtop-k_i(S_{i,j})"),
            ("variables", OrderedDict([
                ("a_j", "effective MoE depth proxy for Engram layer j"),
                ("S_ij", "CKA similarity of MoE layer i and Engram layer j"),
                ("k", "5"),
            ])),
        ]),
        ["A-CKA-EQ9", "A-CKA-AINDEX", "A-CKA-AINDEX2"], [], "medium", "direct"),

    # --- ExperimentSetup ---
    obj("SETUP-ABLATION", "ExperimentSetup", "6 > 6.2", "PKG-ABLATION",
        OrderedDict([
            ("backbone", "12-layer 3B MoE (0.56B activated)"),
            ("training", "100B tokens"),
            ("engram_budget", "fixed 1.6B parameters"),
            ("reference_config", "{2,3}-grams at Layers 2 and 6"),
            ("baseline_val_loss", 1.808),
            ("reference_val_loss", 1.768),
            ("metric", "validation loss (Figure 5)"),
        ]),
        ["A-ABL-SETUP", "A-ABL-REFCFG"], [], "medium", "direct"),
    obj("SETUP-SENSITIVITY", "ExperimentSetup", "6 > 6.3", "PKG-SENSITIVITY",
        OrderedDict([
            ("method", "suppress the sparse embedding (Engram) output at inference, backbone unchanged"),
            ("caveat", "post-hoc ablation induces training-inference inconsistency; focus on Factual Knowledge and Reading Comprehension extremes."),
        ]),
        ["A-SENS-METHOD"], [], "medium", "direct"),
    obj("SETUP-THROUGHPUT", "ExperimentSetup", "6 > 6.4", "PKG-SYSTEM",
        OrderedDict([
            ("harness", "nano-vLLM"),
            ("hardware", "NVIDIA H800"),
            ("workload", "512 sequences"),
            ("seq_len", "Uniform(100, 1024)"),
            ("config", "100B-parameter Engram layer in the 2nd Transformer block, embedding table in host DRAM, async prefetch overlapping first-block compute"),
            ("backbones", ["Dense-4B", "Dense-8B"]),
        ]),
        ["A-SYS-SETUP", "A-SYS-HARNESS", "A-T4-CAP", "A-T4-HDR", "A-T4-SETUP"], [], "medium", "direct"),

    # --- AblationFinding ---
    obj("ABL-LAYER-PLACEMENT", "AblationFinding", "6 > 6.2", "PKG-ABLATION",
        OrderedDict([
            ("question", "Where should Engram memory be injected?"),
            ("finding", "Layer 2 is the best single-layer placement (Val Loss 1.770); splitting the 1.6B memory across Layers 2 and 6 is better still (Val Loss 1.768)."),
            ("trade_off", "early offloading of static local patterns vs stronger contextual queries for gating later"),
        ]),
        ["A-ABL-TRADEOFF", "A-ABL-LAYER2", "A-ABL-LAYER26"], [], "medium", "direct"),
    obj("ABL-COMPONENT-IMPORTANCE", "AblationFinding", "6 > 6.2", "PKG-ABLATION",
        OrderedDict([
            ("question", "Which components matter?"),
            ("finding", "Removing branch-specific fusion, context-aware gating, or tokenizer compression causes the largest validation-loss regressions; removing the depthwise convolution is marginal; 4-grams are slightly suboptimal at fixed 1.6B budget."),
            ("most_important", ["branch-specific fusion", "context-aware gating", "tokenizer compression"]),
        ]),
        ["A-ABL-COMPONENTS", "A-ABL-MINOR"], [], "medium", "direct"),

    # --- ArchitectureComponent (4) --- COMP-GATING is cross-package (6.2 ablation + 6.5 case study)
    obj("COMP-BRANCH-FUSION", "ArchitectureComponent", "6 > 6.2", "PKG-ABLATION",
        OrderedDict([("name", "branch-specific fusion"),
                     ("role", "fuses Engram memory per branch within the multi-branch (mHC) backbone; replacing it with a single fusion on H^pre regresses loss.")]),
        ["A-ABL-COMPONENTS"], [], "medium", "direct"),
    obj("COMP-GATING", "ArchitectureComponent", "6 > 6.2", "PKG-ABLATION",
        OrderedDict([("name", "context-aware gating"),
                     ("role", "scalar gate alpha_t in [0,1] modulating integration of retrieved static memory; one of the three most important components; activates on completed local static patterns.")]),
        ["A-ABL-COMPONENTS"], ["A-CASE-SETUP"], "medium", "direct"),
    obj("COMP-TOKENIZER", "ArchitectureComponent", "6 > 6.2", "PKG-ABLATION",
        OrderedDict([("name", "tokenizer compression"),
                     ("role", "one of the three most important Engram components; removal causes a large validation-loss regression.")]),
        ["A-ABL-COMPONENTS"], [], "medium", "direct"),
    obj("COMP-CONV", "ArchitectureComponent", "6 > 6.2", "PKG-ABLATION",
        OrderedDict([("name", "lightweight depthwise convolution"),
                     ("role", "minor component; removal only marginally degrades performance.")]),
        ["A-ABL-MINOR"], [], "easy", "direct"),

    # --- ExperimentResult ---
    obj("RESULT-SENSITIVITY", "ExperimentResult", "6 > 6.3", "PKG-SENSITIVITY",
        OrderedDict([
            ("name", "Engram-suppression sensitivity (Figure 6)"),
            ("factual_knowledge", "retain 29-44% (TriviaQA 29%)"),
            ("reading_comprehension", "retain 81-93% (C3 93%)"),
        ]),
        ["A-SENS-FACTUAL", "A-SENS-READING"], [], "medium", "direct"),
    obj("RESULT-THROUGHPUT", "ExperimentResult", "6 > 6.4", "PKG-SYSTEM",
        OrderedDict([
            ("name", "End-to-end inference throughput (Table 4)"),
            ("dense_4b", OrderedDict([("baseline", "9,031.62 tok/s"), ("with_100b_engram_cpu_offload", "8,858.28 tok/s")])),
            ("dense_8b", OrderedDict([("baseline", "6,315.52 tok/s"), ("with_100b_engram_cpu_offload", "6,140.02 tok/s")])),
            ("peak_penalty", "2.8% (8B backbone)"),
        ]),
        ["A-SYS-RESULT", "A-T4-R4B", "A-T4-R8B"], [], "medium", "direct"),

    # --- SystemDesignClaim ---
    obj("CLAIM-DETERMINISTIC-ACCESS", "SystemDesignClaim", "6 > 6.4", "PKG-SYSTEM",
        OrderedDict([
            ("statement", "Engram's sparse activations use explicit static hash IDs, giving a strictly deterministic memory access pattern whose indices are known before the layer executes, enabling asynchronous prefetch and host-DRAM offloading."),
            ("advantage_over", "routing-based MoE"),
            ("caveat", "the throughput experiment is a conservative baseline (no HBM caching of Zipfian-frequent items)."),
        ]),
        ["A-SYS-DETERMINISTIC", "A-SYS-CONSERVATIVE", "A-SYS-CONSERVATIVE2"], [], "medium", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]
OBJ_BY_ID = {o["id"]: o for o in OBJECTS}

# ===========================================================================
# context_packages assembly (expected_objects = objects whose home_package==pkg)
# expected_local_fields for cross-package objects (whose evidence partly lives
# in OTHER chunks, declared on the package(s) that hold the supporting atoms).
# ===========================================================================
EXPECTED = {}
for pid in CHUNK_FOR_PKG:
    EXPECTED[pid] = [o["id"] for o in OBJECTS if o["home_package"] == pid]

# Cross-package objects:
#   MECH-EFFECTIVE-DEPTH home=PKG-EFFECTIVE-DEPTH, supporting atoms A-DEPTH-HYP(C-DEPTH-LL),
#       A-LL-INTERP(C-DEPTH-LL) -> PKG-DEPTH-LL can locally derive partial fields.
#   COMP-GATING home=PKG-ABLATION, supporting atom A-CASE-SETUP(C-CASE) -> PKG-CASE partial.
EXPECTED_LOCAL_FIELDS = {
    "PKG-DEPTH-LL": OrderedDict([
        ("MECH-EFFECTIVE-DEPTH", ["claim", "mechanism", "evidence"]),
    ]),
    "PKG-CASE": OrderedDict([
        ("COMP-GATING", ["name", "role"]),
    ]),
}

CONTEXT_PACKAGES = [
    build_package("PKG-INTRO", EXPECTED["PKG-INTRO"]),
    build_package("PKG-DEPTH-LL", EXPECTED["PKG-DEPTH-LL"], EXPECTED_LOCAL_FIELDS["PKG-DEPTH-LL"]),
    build_package("PKG-EFFECTIVE-DEPTH", EXPECTED["PKG-EFFECTIVE-DEPTH"]),
    build_package("PKG-ABLATION", EXPECTED["PKG-ABLATION"]),
    build_package("PKG-SENSITIVITY", EXPECTED["PKG-SENSITIVITY"]),
    build_package("PKG-SYSTEM", EXPECTED["PKG-SYSTEM"]),
    build_package("PKG-CASE", EXPECTED["PKG-CASE"], EXPECTED_LOCAL_FIELDS["PKG-CASE"]),
]

# ===========================================================================
# mentions
# ===========================================================================
def mention(mid, text, mtype, atom_id, ckey):
    return OrderedDict([("id", mid), ("text", text), ("type", mtype), ("atom_id", atom_id), ("canonical_key", ckey)])


MENTIONS = [
    mention("M-01", "LogitLens", "ExperimentSetup", "A-LL-METHOD", "logitlens"),
    mention("M-02", "KL divergence", "ExperimentResult", "A-LL-RESULT", "kl_divergence"),
    mention("M-03", "CKA", "ExperimentSetup", "A-CKA-EQ8", "cka"),
    mention("M-04", "soft alignment index a_j", "Formula", "A-CKA-EQ9", "soft_alignment_index_aj"),
    mention("M-05", "effective depth", "MechanisticExplanation", "A-DEPTH-CONCL", "effective_depth"),
    mention("M-06", "Engram", "ArchitectureComponent", "A-ABL-REFCFG", "engram"),
    mention("M-07", "MoE baseline", "ExperimentSetup", "A-ABL-SETUP", "moe_baseline"),
    mention("M-08", "context-aware gating", "ArchitectureComponent", "A-ABL-COMPONENTS", "context_aware_gating"),
    mention("M-09", "branch-specific fusion", "ArchitectureComponent", "A-ABL-COMPONENTS", "branch_specific_fusion"),
    mention("M-10", "tokenizer compression", "ArchitectureComponent", "A-ABL-COMPONENTS", "tokenizer_compression"),
    mention("M-11", "factual knowledge", "ExperimentResult", "A-SENS-FACTUAL", "factual_knowledge_sensitivity"),
    mention("M-12", "TriviaQA", "ExperimentResult", "A-SENS-FACTUAL", "triviaqa"),
    mention("M-13", "C3", "ExperimentResult", "A-SENS-READING", "c3"),
    mention("M-14", "static hash IDs", "SystemDesignClaim", "A-SYS-DETERMINISTIC", "static_hash_ids"),
    mention("M-15", "host DRAM", "SystemDesignClaim", "A-SYS-SETUP", "host_dram_offload"),
    mention("M-16", "throughput", "ExperimentResult", "A-SYS-RESULT", "inference_throughput"),
    mention("M-17", "nano-vLLM", "ExperimentSetup", "A-SYS-HARNESS", "nano_vllm"),
    mention("M-18", "gating scalar", "ArchitectureComponent", "A-CASE-SETUP", "context_aware_gating"),
    mention("M-19", "Princess of Wales", "ExperimentResult", "A-CASE-EN", "princess_of_wales"),
    mention("M-20", "Val Loss", "ExperimentResult", "A-ABL-REFCFG", "validation_loss"),
    mention("M-21", "Few-NERD", "ExperimentSetup", "A-CKA-METHOD", "few_nerd"),
    mention("M-22", "HSIC", "Formula", "A-CKA-EQ8", "hsic"),
    mention("M-23", "PCIe", "SystemDesignClaim", "A-SYS-SETUP", "pcie"),
    mention("M-24", "Zipfian locality", "SystemDesignClaim", "A-SYS-CONSERVATIVE", "zipfian_locality"),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "context_aware_gating"),
                 ("aliases", ["context-aware gating", "gating mechanism", "gating scalar", "alpha_t"]),
                 ("note", "6.5 introduces the gating scalar alpha_t as the same context-aware gating component ablated in 6.2.")]),
    OrderedDict([("canonical", "effective_depth"),
                 ("aliases", ["effective depth", "effective MoE depth", "increasing the model's effective depth", "deeper representations at earlier layers"])]),
    OrderedDict([("canonical", "engram"),
                 ("aliases", ["Engram", "Engram-27B", "Engram memory", "Engram module", "sparse embedding"])]),
    OrderedDict([("canonical", "moe_baseline"),
                 ("aliases", ["MoE baseline", "3B MoE", "12-layer 3B MoE model", "routing-based MoE"])]),
]

# ===========================================================================
# relations
# ===========================================================================
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


RELATIONS = [
    # effective depth: mechanism explains result
    rel("R-01", "mechanism_explains_result", "MECH-EFFECTIVE-DEPTH", "MECH-CONVERGENCE", ["A-LL-RESULT", "A-LL-INTERP"], "medium", "direct"),
    rel("R-02", "mechanism_explains_result", "MECH-EFFECTIVE-DEPTH", "MECH-ALIGNMENT", ["A-CKA-RESULT", "A-DEPTH-CONCL"], "medium", "direct"),
    rel("R-03", "component_defined_by_formula", "MECH-ALIGNMENT", "FORMULA-CKA", ["A-CKA-EQ8", "A-CKA-METHOD"], "medium", "direct"),
    rel("R-04", "component_defined_by_formula", "MECH-ALIGNMENT", "FORMULA-ALIGNMENT-INDEX", ["A-CKA-EQ9", "A-CKA-AINDEX"], "medium", "direct"),
    rel("R-05", "formula_generalizes_formula", "FORMULA-CKA", "FORMULA-ALIGNMENT-INDEX", ["A-CKA-EQ8", "A-CKA-EQ9"], "hard", "indirect"),
    # ablation -> component importance
    rel("R-06", "ablation_supports_component_importance", "ABL-COMPONENT-IMPORTANCE", "COMP-BRANCH-FUSION", ["A-ABL-COMPONENTS"], "medium", "direct"),
    rel("R-07", "ablation_supports_component_importance", "ABL-COMPONENT-IMPORTANCE", "COMP-GATING", ["A-ABL-COMPONENTS"], "medium", "direct"),
    rel("R-08", "ablation_supports_component_importance", "ABL-COMPONENT-IMPORTANCE", "COMP-TOKENIZER", ["A-ABL-COMPONENTS"], "medium", "direct"),
    rel("R-09", "ablation_supports_component_importance", "ABL-COMPONENT-IMPORTANCE", "COMP-CONV", ["A-ABL-MINOR"], "easy", "direct"),
    rel("R-10", "experiment_tests_claim", "ABL-COMPONENT-IMPORTANCE", "SETUP-ABLATION", ["A-ABL-SETUP", "A-ABL-REFCFG"], "medium", "direct"),
    rel("R-11", "experiment_tests_claim", "ABL-LAYER-PLACEMENT", "SETUP-ABLATION", ["A-ABL-REFCFG"], "medium", "direct"),
    # sensitivity: setup -> result -> mechanism
    rel("R-12", "experiment_tests_claim", "RESULT-SENSITIVITY", "SETUP-SENSITIVITY", ["A-SENS-METHOD"], "medium", "direct"),
    rel("R-13", "mechanism_explains_result", "MECH-FUNCTIONAL-DICHOTOMY", "RESULT-SENSITIVITY", ["A-SENS-FACTUAL", "A-SENS-READING"], "medium", "direct"),
    rel("R-14", "result_supports_claim", "RESULT-SENSITIVITY", "MECH-FUNCTIONAL-DICHOTOMY", ["A-SENS-FACTUAL"], "medium", "direct"),
    # system efficiency
    rel("R-15", "system_design_enables_efficiency", "CLAIM-DETERMINISTIC-ACCESS", "RESULT-THROUGHPUT", ["A-SYS-DETERMINISTIC", "A-SYS-RESULT"], "medium", "direct"),
    rel("R-16", "experiment_tests_claim", "RESULT-THROUGHPUT", "SETUP-THROUGHPUT", ["A-SYS-SETUP", "A-T4-CAP"], "medium", "direct"),
    rel("R-17", "system_design_has_tradeoff", "CLAIM-DETERMINISTIC-ACCESS", "RESULT-THROUGHPUT", ["A-SYS-CONSERVATIVE", "A-SYS-CONSERVATIVE2"], "hard", "indirect"),
    # case study -> gating component / effective depth
    rel("R-18", "mechanism_explains_result", "MECH-GATING-SELECTIVITY", "COMP-GATING", ["A-CASE-EN", "A-CASE-ZH"], "medium", "direct"),
    rel("R-19", "result_supports_claim", "MECH-GATING-SELECTIVITY", "MECH-EFFECTIVE-DEPTH", ["A-CASE-CONCL"], "hard", "indirect"),
    # Table 3 entity-resolution example motivates the effective-depth mechanism
    rel("R-20", "result_supports_claim", "RESULT-ENTITY-RESOLUTION", "MECH-EFFECTIVE-DEPTH", ["A-DEPTH-MOTIV", "A-T3-R6"], "medium", "indirect"),
]

# ===========================================================================
# do_not_extract: inline citations + Section 2.5/2.3 policy refs + transliterated
# Chinese tokens (keep ASCII) + subfigure/forward refs.
# ===========================================================================
DO_NOT_EXTRACT = [
    OrderedDict([("text", "(Ghandeharioun et al., 2024; Jin et al., 2025; Li and Subramani, 2025)"), ("atom_id", "A-DEPTH-MOTIV"),
                 ("reason", "inline author-year citations are not knowledge objects."), ("kind", "citation")]),
    OrderedDict([("text", "(Belrose et al., 2023; nostalgebraist, 2020)"), ("atom_id", "A-DEPTH-TOOLS"),
                 ("reason", "inline author-year citations."), ("kind", "citation")]),
    OrderedDict([("text", "(Xie et al., 2025; Zhu et al., 2025)"), ("atom_id", "A-ABL-TRADEOFF"),
                 ("reason", "inline author-year citations."), ("kind", "citation")]),
    OrderedDict([("text", "四大发明"), ("atom_id", "A-CASE-ZH"),
                 ("reason", "Chinese-script token; transliterated to ASCII 'sida famming' in normalized_text, not a separate knowledge object."), ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "张仲景"), ("atom_id", "A-CASE-ZH"),
                 ("reason", "Chinese-script token; transliterated to ASCII 'Zhang Zhongjing' in normalized_text, not a separate knowledge object."), ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "Section 2.5"), ("atom_id", "A-ABL-LAYER26"),
                 ("reason", "forward/cross-section reference to the memory-hierarchy discussion in Section 2.5, outside this slice."), ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "Section 2.3"), ("atom_id", "A-CASE-SETUP"),
                 ("reason", "back-reference to where context-aware gating was introduced; not a Section 6 object."), ("kind", "out_of_slice_reference")]),
    OrderedDict([("pattern", "inline_author_year_citation"),
                 ("examples", ["(Ghandeharioun et al., 2024)", "(Kornblith et al., 2019)", "(Kwon et al., 2023)", "(Xie et al., 2025)"]),
                 ("reason", "inline author-year citations are not knowledge objects; this policy suppresses citation over-extraction consistently across chapters."),
                 ("kind", "citation_policy")]),
]

# ===========================================================================
# source_meta
# ===========================================================================
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Section 6 (Analysis) only"),
    ("title", "Engram: Conditional Memory for Language Models - Section 6 Analysis"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 224-321; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "article_research"),
    ("profile_cues", ["Section 6 Analysis", "we ablate", "validation loss", "Table 3", "Table 4",
                      "Figure 4/5/6/7", "LogitLens", "CKA", "throughput", "case study"]),
    ("extraction_targets", ["MechanisticExplanation", "AblationFinding", "ArchitectureComponent",
                            "ExperimentResult", "ExperimentSetup", "SystemDesignClaim", "Formula", "Implication"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span: it may rephrase, transliterate math/Chinese to ASCII (X^T, a_j, alpha_t, sida famming), and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs not inside the span. EXCEPTION: a formula_atom MAY carry a symbolic interpretation supplied by atoms in metadata.supported_by_context_atoms / interpretation_supported_by."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks needed to fully define it). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only; supporting_context atoms are NOT required to be present in the home package's input."),
        ("expected_local_fields", "For a cross-package object, a package holding only some of its evidence may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone. Here MECH-EFFECTIVE-DEPTH (home PKG-EFFECTIVE-DEPTH, supporting atoms in C-DEPTH-LL) and COMP-GATING (home PKG-ABLATION, supporting atom A-CASE-SETUP in C-CASE) declare partial fields on PKG-DEPTH-LL / PKG-CASE."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "Figures 4-7 are referenced by caption/prose only (no images in the text slice); mechanistic atoms bind to the prose with metadata.figure recording the figure id. No forward-reference figure atoms are isolated in Section 6."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("context_only", "an atom not consumed by any object or relation may be flagged metadata.context_only:true; Section 6 has no such atoms (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "Equation tags are paper-continuous; only Eq.(8) (CKA, lines 250-252) and Eq.(9) (soft alignment index a_j, lines 258-260) appear in this slice. LaTeX uses \\top, \\mathcal{I}, \\operatorname{argtop} -> normalized to ASCII (X^T, I_j, argtop-k).",
        "Eq.(9)'s tag is on the formula line (259) inside the $$...$$ block; the a_j prose definition is split across lines 256 (before Figure 5) and 266 (after Figure 5).",
        "Table 3 (line 242) and Table 4 (line 312) are single-line MinerU HTML <table> blocks. Table 4 uses colspan=3 section banners and rowspan=2 on the Base Model column, so table_row_atoms (A-T4-R4B/R8B) reassemble each backbone's baseline-vs-offload pair as anchored <tr>...</tr> substrings.",
        "Several prose paragraphs are split by inserted floats: 6.1.1 (lines 238 + 244 around Table 3), 6.1.2 a_j (256 + 266 around Figure 5), 6.3 (294 + 298 around Figure 6), 6.4 conservative-baseline (308 + 314 around Table 4). Atoms use contiguous single-line substrings on each side; source_element notes record the split.",
        "Figures 4-7 are referenced by caption/prose; mechanistic/result atoms bind to the prose with metadata.figure. Chinese case-study tokens are transliterated to ASCII in normalized_text (originals listed in do_not_extract).",
        "'infernce' in the Table 4 caption (line 310) is a source typo; raw_text preserves it verbatim, normalized_text corrects to 'inference'.",
    ]),
])

# ===========================================================================
# Assemble + dump
# ===========================================================================
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
        "# Gold fixture v0.3.3 - Engram paper Section 6 Analysis (profile: article_research)\n"
        "# Upgraded from v0.1: dual coordinates (source_span authoritative + viewer_span),\n"
        "# raw/normalized discipline, object evidence split into local/supporting + home_package,\n"
        "# per-chunk context_packages with expected_objects (+ expected_local_fields for cross-package\n"
        "# objects MECH-EFFECTIVE-DEPTH and COMP-GATING), gold_label/difficulty/evidence_strength\n"
        "# (no decimal confidence), do_not_extract negatives, and auto-generated source_elements.\n"
        "# Spans are computed by build.py against engram_paper_mineru.md; do NOT hand-edit.\n"
    )
    body = yaml.dump(GOLD, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=100000)
    with open(os.path.join(HERE, "gold.yaml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    print("wrote source.md (%d bytes) and gold.yaml" % len(VIEWER_TEXT))
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d mentions=%d" % (
        len(ATOMS), len(SOURCE_ELEMENTS), len(SEMANTIC_CHUNKS),
        len(CONTEXT_PACKAGES), len(OBJECTS), len(RELATIONS), len(MENTIONS)))


if __name__ == "__main__":
    main()
