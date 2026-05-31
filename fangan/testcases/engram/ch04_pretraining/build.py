#!/usr/bin/env python3
"""Builder for ch04_pretraining gold.yaml (schema v0.3.3, profile article_research).

Upgrades the Chapter 4 (Large Scale Pre-training) fixture from v0.1 to v0.3.3.
Keeps every v0.1 atom/chunk/object/relation ID and its meaning; only re-expresses
them in the v0.3.3 schema and adds verbatim spans (dual coordinates).

All spans are computed by locating verbatim substrings (anchors) in the
authoritative MinerU source file; YAML spans are NEVER hand-written.

Authoritative slice: source_file lines 158-189 (4. / 4.1 / 4.2 + Table 1).
Table 2 (lines 190-192) is OUT of slice. The two tail prose lines of 4.2
(MinerU rendered them AFTER Table 2, at lines 194 and 196) are still part of
Section 4.2; they carry authoritative source_span only (no viewer_span, since
they fall outside the 158-189 viewer slice) and are flagged out_of_viewer_slice.

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

SLICE_LINE_START = 158
SLICE_LINE_END = 189

# ---------------------------------------------------------------------------
# Load source, compute per-line char offsets into source_file.
# ---------------------------------------------------------------------------
SRC = open(SOURCE_FILE, encoding="utf-8").read()
SRC_LINES = SRC.split("\n")

_LINE_OFF = {}
_acc = 0
for _i, _ln in enumerate(SRC_LINES, start=1):
    _LINE_OFF[_i] = _acc
    _acc += len(_ln) + 1


def src_line(n):
    return SRC_LINES[n - 1]


# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice of lines 158-189 + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY

# offset of each source line inside the viewer file
_VIEWER_LINE_OFF = {}
_voff = len(VIEWER_HEADER)
for _ln_no in range(SLICE_LINE_START, SLICE_LINE_END + 1):
    _VIEWER_LINE_OFF[_ln_no] = _voff
    _voff += len(SRC_LINES[_ln_no - 1]) + 1


def viewer_lineno(src_line_no):
    """1-based line number inside source.md for a given source-file line."""
    # header is line 1; slice line SLICE_LINE_START -> line 2, etc.
    return 2 + (src_line_no - SLICE_LINE_START)


# ---------------------------------------------------------------------------
# Span helper: locate a verbatim substring of a given source line and emit
# source_span (authoritative) and, when the line is inside the viewer slice,
# viewer_span. Substring must be unique within that line.
# ---------------------------------------------------------------------------
def span_for(raw, line_no):
    text = src_line(line_no)
    idx = text.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND on line %d: %r" % (line_no, raw[:70]))
    if text.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS on line %d: %r" % (line_no, raw[:70]))
    src_cs = _LINE_OFF[line_no] + idx
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch on line %d" % line_no
    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", line_no), ("line_end", line_no),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = None
    if SLICE_LINE_START <= line_no <= SLICE_LINE_END:
        v_cs = _VIEWER_LINE_OFF[line_no] + idx
        v_ce = v_cs + len(raw)
        assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch on line %d" % line_no
        vln = viewer_lineno(line_no)
        viewer_span = OrderedDict([
            ("file", VIEWER_NAME),
            ("line_start", vln), ("line_end", vln),
            ("char_start", v_cs), ("char_end", v_ce),
            ("viewer_only", True),
        ])
    return source_span, viewer_span


def atom(aid, section_id, atom_type, se_id, raw, normalized, line_no,
         evidence_strength="direct", metadata=None):
    ss, vs = span_for(raw, line_no)
    d = OrderedDict()
    d["id"] = aid
    d["section_id"] = section_id
    d["atom_type"] = atom_type
    d["source_element_id"] = se_id
    d["source_span"] = ss
    if vs is not None:
        d["viewer_span"] = vs
    d["raw_text"] = raw
    d["normalized_text"] = normalized
    d["evidence_strength"] = evidence_strength
    if metadata:
        d["metadata"] = metadata
    return d


# ---------------------------------------------------------------------------
# Evidence atoms. raw_text = verbatim substrings of the indicated source line.
# Same atom IDs / meaning as the v0.1 fixture, re-expressed in v0.3.3.
# ---------------------------------------------------------------------------
L_TABLE = 164  # the single MinerU HTML <table> line
ATOMS = []

# ---------- 4.0 chapter intro (split across lines 160 + 166 by Table 1) ----------
# A-CH4-INTRO keeps its meaning; we anchor it on the verbatim line-160 fragment.
ATOMS.append(atom(
    "A-CH4-INTRO", "SEC-4", "experiment_setup_atom", "SE-4-INTRO",
    "Specifically, we train four models: (1) Dense-4B (4.1B total parameters), (2) MoE-27B",
    "Engram is scaled to the multi-billion-parameter regime for real-world LM pre-training; four models are trained: (1) Dense-4B (4.1B total parameters), (2) MoE-27B [...].",
    160,
    metadata={"note": "MinerU split this sentence: Table 1 (caption line 162 + <table> line 164) is inserted, and the sentence continues on line 166 with models (3)/(4) and the control-variable clause (see A-CH4-INTRO-CONT)."},
))
ATOMS.append(atom(
    "A-CH4-INTRO-CONT", "SEC-4", "experiment_setup_atom", "SE-4-INTRO-CONT",
    "(26.7B total parameters), (3) Engram-27B (26.7B total parameters), and (4) Engram-40B (39.5B total parameters). All models are trained using an identical data curriculum (same token budget and order) and are strictly matched in the number of activated parameters.",
    "[...] (26.7B total parameters), (3) Engram-27B (26.7B total parameters), and (4) Engram-40B (39.5B total parameters). All models use an identical data curriculum (same token budget and order) and are strictly matched in the number of activated parameters.",
    166,
    metadata={"note": "continuation of A-CH4-INTRO after Table 1; supplies models (3)/(4) total params + control variables."},
))

# ---------- 4.1 Experimental Setup ----------
ATOMS.append(atom(
    "A-SETUP-DATA", "SEC-4.1", "experiment_setup_atom", "SE-4.1-DATA",
    "All models are pre-trained on a corpus of 262 billion tokens and we utilize the tokenizer from DeepSeek-v3 (Liu et al., 2024a) with a vocabulary size of 128k.",
    "All models are pre-trained on a corpus of 262 billion tokens using the DeepSeek-v3 tokenizer with a vocabulary size of 128k.",
    170,
))
ATOMS.append(atom(
    "A-SETUP-BACKBONE", "SEC-4.1", "experiment_setup_atom", "SE-4.1-BACKBONE",
    "Each block integrates a Multi-head Latent Attention (MLA) (DeepSeek-AI et al., 2024) with 32 heads, connected to FFNs via mHC (Xie et al., 2025) with an expansion rate of 4. All models are optimized using Muon (Jordan et al., 2024; Team et al., 2025)",
    "Shared backbone (30-block Transformer, hidden size 2560): each block uses Multi-head Latent Attention (MLA) with 32 heads, connected to FFNs via mHC with an expansion rate of 4; all models are optimized using Muon.",
    172,
    metadata={"note": "hidden size 2560 / '30-block Transformer' are on line 170-172 boundary; backbone normalized summary keeps in-span numbers (32 heads, expansion rate 4)."},
))
ATOMS.append(atom(
    "A-SETUP-DENSE", "SEC-4.1", "experiment_setup_atom", "SE-4.1-DENSE",
    "Dense-4B serves as the baseline model. It utilizes the backbone architecture described above, incorporating a standard dense FFN into every block.",
    "Dense-4B is the baseline: the shared backbone with a standard dense FFN in every block.",
    174,
))
ATOMS.append(atom(
    "A-SETUP-MOE", "SEC-4.1", "experiment_setup_atom", "SE-4.1-MOE",
    "MoE-27B replaces the standard dense FFN with a DeepSeekMoE module (Dai et al., 2024). Configured with 72 routed experts and 2 shared experts (activating the top-k = 6 routed experts per token), this model scales to 26.7B total parameters while maintaining the same activated parameters as Dense-4B.",
    "MoE-27B replaces the dense FFN with a DeepSeekMoE module: 72 routed experts + 2 shared experts, activating the top-k = 6 routed experts per token; 26.7B total parameters while keeping the same activated parameters as Dense-4B.",
    175,
))
ATOMS.append(atom(
    "A-SETUP-ENGRAM27", "SEC-4.1", "experiment_setup_atom", "SE-4.1-ENGRAM27",
    "Engram-27B is strictly derived from the MoE-27B architecture to ensure fair comparison. We reduce the number of routed experts from 72 to 55 and reallocate the freed parameters to a 5.7B-parameter embedding module ( $\\rho = 74.3\\%$ ), keeping the total model size constant at 26.7B. Regarding the Engram configuration, we instantiate the module at layers 2 and 15 and set the maximum $N$ -gram size to 3, the number of heads to 8, and the dimension to 1280. For optimization, the embedding parameters are updated using Adam (Kingma, 2014) with a learning rate scaled by $5\\times$ and no weight decay, while the convolution parameters are initialized to zero to strictly preserve the identity mapping at the start of training.",
    "Engram-27B is derived from MoE-27B by reducing routed experts from 72 to 55 and reallocating the freed parameters to a 5.7B-parameter embedding module (rho = 74.3%), keeping total model size at 26.7B. The Engram module is instantiated at layers 2 and 15, with maximum N-gram size 3, 8 heads, and dimension 1280. Embedding parameters use Adam with learning rate scaled by 5x and no weight decay; convolution parameters are zero-initialized to preserve the identity mapping at the start of training.",
    176,
))
ATOMS.append(atom(
    "A-SETUP-ENGRAM40", "SEC-4.1", "experiment_setup_atom", "SE-4.1-ENGRAM40",
    "Engram-40B retains the same backbone and computation budget as Engram-27B but scales the sparse embedding module to 18.5B parameters (totaling 39.5B parameters). This model is designed to investigate the scaling properties of Engram.",
    "Engram-40B keeps the same backbone and compute budget as Engram-27B but scales the sparse embedding module to 18.5B parameters (39.5B total), to investigate Engram's scaling properties.",
    177,
))
ATOMS.append(atom(
    "A-SETUP-EVAL", "SEC-4.1", "experiment_setup_atom", "SE-4.1-EVAL",
    "We evaluate models on a diverse suite of benchmarks spanning language modeling, knowledge, reasoning, reading comprehension, and code/math. For each benchmark, we follow standard prompting protocols and evaluation metrics.",
    "Evaluation spans language modeling, knowledge, reasoning, reading comprehension, and code/math; each benchmark uses standard prompting protocols and evaluation metrics.",
    179,
))

# ---------- Table 1 : caption + header + key rows (anchored substrings of line 164) ----------
ATOMS.append(atom(
    "A-T1-CAP", "SEC-4", "table_caption_atom", "SE-T1-CAP",
    "Table 1 | Pre-training performance comparison between dense, MoE, and Engram models. All models are trained for 262B tokens and are matched in activated parameters (3.8B). Engram-27B is iso-parameters with MoE-27B by reallocating parameters from routed experts (72 → 55) to a 5.7B-parameter Engram memory. Engram-40B further increases Engram memory (18.5B parameters) while keeping the activated-parameter budget fixed. Full training-time benchmark trajectories are reported in Appendix B.",
    "Table 1 | Pre-training performance comparison between dense, MoE, and Engram models. All models trained for 262B tokens and matched in activated parameters (3.8B). Engram-27B is iso-parameters with MoE-27B by reallocating routed experts (72 -> 55) to a 5.7B-parameter Engram memory; Engram-40B further increases Engram memory (18.5B parameters) at a fixed activated-parameter budget. Full training-time benchmark trajectories are reported in Appendix B.",
    162,
    metadata={"table_id": "Table 1"},
))
ATOMS.append(atom(
    "A-T1-HEADER", "SEC-4", "table_header_atom", "SE-T1",
    "<tr><td></td><td>Benchmark (Metric)</td><td># Shots</td><td>Dense-4B</td><td>MoE-27B</td><td>Engram-27B</td><td>Engram-40B</td></tr>",
    "Benchmark (Metric) | # Shots | Dense-4B | MoE-27B | Engram-27B | Engram-40B",
    L_TABLE,
    metadata={"table_id": "Table 1", "role": "column_header",
              "note": "single HTML header <tr>; leading empty <td> is the rowspan group-label placeholder column, not a data column."},
))
ATOMS.append(atom(
    "A-T1-TOTALPARAMS", "SEC-4", "table_row_atom", "SE-T1",
    "<td># Total Params</td><td></td><td>4.1B</td><td>26.7B</td><td>26.7B</td><td>39.5B</td>",
    "# Total Params | 4.1B | 26.7B | 26.7B | 39.5B",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Model Config"},
))
ATOMS.append(atom(
    "A-T1-ENGRAMPARAMS", "SEC-4", "table_row_atom", "SE-T1",
    "<td># Engram Params</td><td></td><td>-</td><td>-</td><td>5.7B</td><td>18.5B</td>",
    "# Engram Params | - | - | 5.7B | 18.5B",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Model Config"},
))
ATOMS.append(atom(
    "A-T1-VALLOSS", "SEC-4", "table_row_atom", "SE-T1",
    "<td>Validation Set (loss)</td><td>-</td><td>1.768</td><td>1.634</td><td>1.622</td><td>1.610</td>",
    "Validation Set (loss) | 1.768 | 1.634 | 1.622 | 1.610 (lower is better)",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Language Modeling", "metric": "loss"},
))
ATOMS.append(atom(
    "A-T1-MMLU", "SEC-4", "table_row_atom", "SE-T1",
    "<td>MMLU (Acc.)</td><td>5-shot</td><td>48.6</td><td>57.4</td><td>60.4</td><td>60.6</td>",
    "MMLU (Acc.) 5-shot | 48.6 | 57.4 | 60.4 | 60.6",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Knowledge and Reasoning"},
))
ATOMS.append(atom(
    "A-T1-MMLUPRO", "SEC-4", "table_row_atom", "SE-T1",
    "<td>MMLU-Pro (Acc.)</td><td>5-shot</td><td>21.1</td><td>28.3</td><td>30.1</td><td>31.3</td>",
    "MMLU-Pro (Acc.) 5-shot | 21.1 | 28.3 | 30.1 | 31.3",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Knowledge and Reasoning"},
))
ATOMS.append(atom(
    "A-T1-CMMLU", "SEC-4", "table_row_atom", "SE-T1",
    "<td>CMMLU (Acc.)</td><td>5-shot</td><td>47.9</td><td>57.9</td><td>61.9</td><td>63.4</td>",
    "CMMLU (Acc.) 5-shot | 47.9 | 57.9 | 61.9 | 63.4",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Knowledge and Reasoning"},
))
ATOMS.append(atom(
    "A-T1-BBH", "SEC-4", "table_row_atom", "SE-T1",
    "<td>BBH (EM)</td><td>3-shot</td><td>42.8</td><td>50.9</td><td>55.9</td><td>57.5</td>",
    "BBH (EM) 3-shot | 42.8 | 50.9 | 55.9 | 57.5",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Knowledge and Reasoning"},
))
ATOMS.append(atom(
    "A-T1-ARCC", "SEC-4", "table_row_atom", "SE-T1",
    "<td>ARC-Challenge (Acc.)</td><td>25-shot</td><td>59.3</td><td>70.1</td><td>73.8</td><td>76.4</td>",
    "ARC-Challenge (Acc.) 25-shot | 59.3 | 70.1 | 73.8 | 76.4",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Knowledge and Reasoning"},
))
ATOMS.append(atom(
    "A-T1-DROP", "SEC-4", "table_row_atom", "SE-T1",
    "<td>DROP (F1)</td><td>1-shot</td><td>41.6</td><td>55.7</td><td>59.0</td><td>60.7</td>",
    "DROP (F1) 1-shot | 41.6 | 55.7 | 59.0 | 60.7",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Reading Comprehension"},
))
ATOMS.append(atom(
    "A-T1-HUMANEVAL", "SEC-4", "table_row_atom", "SE-T1",
    "<td>HumanEval (Pass@1)</td><td>0-shot</td><td>26.8</td><td>37.8</td><td>40.8</td><td>38.4</td>",
    "HumanEval (Pass@1) 0-shot | 26.8 | 37.8 | 40.8 | 38.4",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Code and Math"},
))
ATOMS.append(atom(
    "A-T1-GSM8K", "SEC-4", "table_row_atom", "SE-T1",
    "<td>GSM8K (EM)</td><td>8-shot</td><td>35.5</td><td>58.4</td><td>60.6</td><td>62.6</td>",
    "GSM8K (EM) 8-shot | 35.5 | 58.4 | 60.6 | 62.6",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Code and Math"},
))
ATOMS.append(atom(
    "A-T1-MATH", "SEC-4", "table_row_atom", "SE-T1",
    "<td>MATH (EM)</td><td>4-shot</td><td>15.2</td><td>28.3</td><td>30.7</td><td>30.6</td>",
    "MATH (EM) 4-shot | 15.2 | 28.3 | 30.7 | 30.6",
    L_TABLE,
    metadata={"table_id": "Table 1", "row_group": "Code and Math"},
))

# ---------- 4.2 Experimental Results (result_sentence) ----------
# A-RES-SPARSE-DENSE is IN the viewer slice (line 188). The other three 4.2 prose
# atoms were rendered by MinerU AFTER Table 2 (lines 194/196) -> source_span only,
# no viewer_span, flagged out_of_viewer_slice (still Section 4.2 content).
ATOMS.append(atom(
    "A-RES-SPARSE-DENSE", "SEC-4.2", "result_sentence", "SE-4.2-P1",
    "First, consistent with prior literature (Borgeaud et al., 2022; He, 2024; Shazeer et al., 2017), sparse architectures demonstrate superior scaling laws compared to dense models. Under the same training compute budget, all three sparse variants (MoE-27B, Engram-27B/40B) significantly outperform the iso-FLOPs Dense-4B baseline across all benchmarks.",
    "Consistent with prior literature, sparse architectures demonstrate superior scaling laws compared to dense models: under the same training compute budget all three sparse variants (MoE-27B, Engram-27B/40B) significantly outperform the iso-FLOPs Dense-4B baseline across all benchmarks.",
    188,
))
ATOMS.append(atom(
    "A-RES-ENGRAM-VS-MOE", "SEC-4.2", "result_sentence", "SE-4.2-P2",
    "More importantly, Engram-27B consistently improves over the iso-parameter and iso-FLOPs MoE-27B baseline. Interestingly, these gains are not limited to knowledge-intensive tasks (e.g., MMLU: +3.0, MMLU-Pro: +1.8, CMMLU: +4.0), where memory capacity is intuitively beneficial. We observe even more significant improvements in general-reasoning domains (e.g., BBH: +5.0, ARC-Challenge: +3.7, DROP: +3.3), as well as code and mathematical reasoning (e.g., HumanEval: +3.0, MBPP: +1.6, GSM8K: +2.2, MATH: +2.4).",
    "Engram-27B consistently improves over the iso-parameter, iso-FLOPs MoE-27B baseline. Gains are not limited to knowledge tasks (MMLU +3.0, MMLU-Pro +1.8, CMMLU +4.0) but extend to general reasoning (BBH +5.0, ARC-Challenge +3.7, DROP +3.3) and code/math (HumanEval +3.0, MBPP +1.6, GSM8K +2.2, MATH +2.4).",
    194,
    metadata={"out_of_viewer_slice": True,
              "note": "Section 4.2 prose that MinerU rendered after Table 2 (line 194); authoritative source_span only, no viewer_span (outside the 158-189 viewer slice)."},
))
ATOMS.append(atom(
    "A-RES-CLAIM", "SEC-4.2", "result_sentence", "SE-4.2-P2",
    "These results support our hypothesis that introducing a dedicated knowledge lookup primitive improves representation efficiency beyond what can be achieved by allocating the entire sparse budget to conditional computation.",
    "These results support the hypothesis that a dedicated knowledge-lookup primitive improves representation efficiency beyond allocating the entire sparse budget to conditional computation.",
    194,
    metadata={"out_of_viewer_slice": True,
              "note": "Section 4.2 conclusion sentence rendered after Table 2 (line 194); source_span only."},
))
ATOMS.append(atom(
    "A-RES-ENGRAM40", "SEC-4.2", "result_sentence", "SE-4.2-P3",
    "Finally, scaling to Engram-40B further reduces pre-training loss and improves performance across most benchmarks. Although it does not yet strictly dominate Engram-27B on every task, this is likely an artifact of under-training. We observe that the training loss gap between Engram-40B and the baselines continues to widen towards the end of training, suggesting that the expanded memory capacity has not yet fully saturated within the current token budget.",
    "Engram-40B further reduces pre-training loss and improves most benchmarks but does not strictly dominate Engram-27B on every task, likely an artifact of under-training: the training-loss gap to the baselines keeps widening toward the end of training, so the expanded memory capacity is not yet saturated within the token budget.",
    196,
    metadata={"out_of_viewer_slice": True,
              "note": "Section 4.2 prose rendered after Table 2 (line 196); source_span only."},
))

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# NUMERIC over-span audit (build-time): every number in a table_row_atom's
# normalized_text must appear in its raw_text substring.
# ---------------------------------------------------------------------------
import re as _re
_NUM = _re.compile(r"\d+(?:\.\d+)?")
for a in ATOMS:
    if a["atom_type"] == "table_row_atom":
        raw = a["raw_text"]
        for tok in _NUM.findall(a["normalized_text"]):
            if tok not in raw:
                raise SystemExit("NUMERIC over-span: %s normalized number %r not in raw substring" % (a["id"], tok))

# ---------------------------------------------------------------------------
# source_elements
# ---------------------------------------------------------------------------
SOURCE_ELEMENTS = [
    OrderedDict([("id", "SE-H-4"), ("type", "heading"), ("file", SOURCE_NAME), ("line_start", 158), ("line_end", 158)]),
    OrderedDict([("id", "SE-4-INTRO"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 160), ("line_end", 160)]),
    OrderedDict([("id", "SE-T1-CAP"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 162), ("line_end", 162),
                 ("note", "Table 1 caption paragraph (above the <table>)")]),
    OrderedDict([("id", "SE-T1"), ("type", "table"), ("file", SOURCE_NAME), ("line_start", 164), ("line_end", 164),
                 ("metadata", OrderedDict([("table_id", "Table 1"), ("format", "html"),
                                           ("note", "single MinerU HTML <table>: 1 header <tr> + metric rows; rowspan cells (Language Modeling / Knowledge & Reasoning / Reading Comprehension / Code & Math) are metric-group labels, not data columns.")]))]),
    OrderedDict([("id", "SE-4-INTRO-CONT"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 166), ("line_end", 166),
                 ("note", "continuation of the chapter-intro sentence after Table 1")]),
    OrderedDict([("id", "SE-H-4.1"), ("type", "heading"), ("file", SOURCE_NAME), ("line_start", 168), ("line_end", 168)]),
    OrderedDict([("id", "SE-4.1-DATA"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 170), ("line_end", 170),
                 ("note", "run-in heading 'Training Data and Model Configurations'; continues on line 172 across a page break")]),
    OrderedDict([("id", "SE-4.1-BACKBONE"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 172), ("line_end", 172)]),
    OrderedDict([("id", "SE-4.1-DENSE"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 174), ("line_end", 174)]),
    OrderedDict([("id", "SE-4.1-MOE"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 175), ("line_end", 175)]),
    OrderedDict([("id", "SE-4.1-ENGRAM27"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 176), ("line_end", 176)]),
    OrderedDict([("id", "SE-4.1-ENGRAM40"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 177), ("line_end", 177)]),
    OrderedDict([("id", "SE-4.1-EVAL"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 179), ("line_end", 179),
                 ("note", "run-in heading 'Evaluation Protocol'")]),
    OrderedDict([("id", "SE-H-4.2"), ("type", "heading"), ("file", SOURCE_NAME), ("line_start", 186), ("line_end", 186)]),
    OrderedDict([("id", "SE-4.2-P1"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 188), ("line_end", 188)]),
    OrderedDict([("id", "SE-4.2-P2"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 194), ("line_end", 194),
                 ("note", "4.2 prose rendered after Table 2 by MinerU; OUT of the 158-189 viewer slice")]),
    OrderedDict([("id", "SE-4.2-P3"), ("type", "paragraph"), ("file", SOURCE_NAME), ("line_start", 196), ("line_end", 196),
                 ("note", "4.2 prose rendered after Table 2 by MinerU; OUT of the 158-189 viewer slice")]),
]

# ---------------------------------------------------------------------------
# section_tree
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-4"), ("path", "4"), ("title", "Large Scale Pre-training")]),
    OrderedDict([("id", "SEC-4.1"), ("path", "4 > 4.1"), ("title", "Experimental Setup"), ("parent", "SEC-4")]),
    OrderedDict([("id", "SEC-4.2"), ("path", "4 > 4.2"), ("title", "Experimental Results"), ("parent", "SEC-4")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks (same two chunks as v0.1, plus the intro-continuation atom)
# ---------------------------------------------------------------------------
SETUP_ATOMS = ["A-CH4-INTRO", "A-CH4-INTRO-CONT", "A-SETUP-DATA", "A-SETUP-BACKBONE",
               "A-SETUP-DENSE", "A-SETUP-MOE", "A-SETUP-ENGRAM27", "A-SETUP-ENGRAM40", "A-SETUP-EVAL"]
TABLE1_ATOMS = ["A-T1-CAP", "A-T1-HEADER", "A-T1-TOTALPARAMS", "A-T1-ENGRAMPARAMS", "A-T1-VALLOSS",
                "A-T1-MMLU", "A-T1-MMLUPRO", "A-T1-CMMLU", "A-T1-BBH", "A-T1-ARCC", "A-T1-DROP",
                "A-T1-HUMANEVAL", "A-T1-GSM8K", "A-T1-MATH",
                "A-RES-SPARSE-DENSE", "A-RES-ENGRAM-VS-MOE", "A-RES-CLAIM", "A-RES-ENGRAM40"]

SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C-SETUP"), ("profile", "article_research"),
        ("chunk_type", "experiment_setup_block"), ("section_path", "4 > 4.1"),
        ("atom_ids", list(SETUP_ATOMS)),
        ("central_atom_ids", ["A-SETUP-ENGRAM27", "A-SETUP-BACKBONE"]),
        ("boundary_reason",
         "experiment_setup_result_dependency: the data curriculum, shared backbone, the four model "
         "configurations, and the evaluation protocol form one complete setup unit kept in a single chunk "
         "so it binds to one ExperimentSetup object (qiefen Section 5.3)."),
        ("extraction_targets", ["ExperimentSetup"]),
        ("gold_must_cover_atoms", ["A-SETUP-DATA", "A-SETUP-BACKBONE", "A-SETUP-MOE",
                                   "A-SETUP-ENGRAM27", "A-SETUP-ENGRAM40"]),
    ]),
    OrderedDict([
        ("id", "C-TABLE1"), ("profile", "article_research"),
        ("chunk_type", "experiment_result_block"), ("section_path", "4 > 4.2"),
        ("atom_ids", list(TABLE1_ATOMS)),
        ("central_atom_ids", ["A-T1-VALLOSS", "A-RES-ENGRAM-VS-MOE"]),
        ("boundary_reason",
         "table_header_row_dependency: caption + header + all metric rows must stay with the prose result "
         "sentences, otherwise the table rows lose their column semantics; result_sentence atoms share the "
         "chunk with the table_row atoms they summarize so ExperimentResult -> ArticleClaim is supported."),
        ("extraction_targets", ["ExperimentResult", "ArticleClaim"]),
        ("gold_must_cover_atoms", ["A-T1-CAP", "A-T1-HEADER", "A-T1-VALLOSS", "A-T1-MMLU", "A-T1-BBH",
                                   "A-T1-ARCC", "A-T1-GSM8K", "A-T1-MATH", "A-RES-ENGRAM-VS-MOE"]),
    ]),
]
CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids.
# ---------------------------------------------------------------------------
DOC_TITLE = "Engram: Conditional Memory Complements Conditional Computation"


def build_package(pid, chunk_id, section_path, linked_context, targets,
                  expected_objects, expected_local_fields=None):
    d = OrderedDict([
        ("id", pid), ("profile", "article_research"), ("chunk_id", chunk_id),
        ("section_path", section_path), ("document_title", DOC_TITLE),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", linked_context),
        ("extraction_targets", targets),
        ("expected_objects", expected_objects),
    ])
    if expected_local_fields:
        d["expected_local_fields"] = expected_local_fields
    return d


CONTEXT_PACKAGES = [
    build_package(
        "PKG-SETUP", "C-SETUP", "Chapter 4 > 4.1 Experimental Setup",
        OrderedDict([
            ("previous_heading", "4. Large Scale Pre-training"),
            ("next_heading", "4.2 Experimental Results"),
        ]),
        ["ExperimentSetup"],
        ["SETUP-PRETRAIN"],
    ),
    build_package(
        "PKG-TABLE1", "C-TABLE1", "Chapter 4 > 4.2 Experimental Results > Table 1",
        OrderedDict([
            ("table_caption", "Table 1 | Pre-training performance comparison between dense, MoE, and Engram models (262B tokens, 3.8B activated)."),
            ("table_headers", "Benchmark (Metric) | # Shots | Dense-4B | MoE-27B | Engram-27B | Engram-40B"),
            ("previous_heading", "4.2 Experimental Results"),
            ("next_heading", "5. Long Context Training"),
        ]),
        ["ExperimentResult", "ArticleClaim"],
        ["RESULT-TABLE1", "CLAIM-MEMORY-BEYOND-KNOWLEDGE", "CLAIM-SPARSE-OVER-DENSE"],
    ),
]

# ---------------------------------------------------------------------------
# mentions (same 16 mentions as v0.1)
# ---------------------------------------------------------------------------
def mention(mid, text, mtype, atom_id, key):
    return OrderedDict([("id", mid), ("text", text), ("type", mtype), ("atom_id", atom_id), ("canonical_key", key)])


MENTIONS = [
    mention("M-01", "Dense-4B", "ExperimentSetup", "A-SETUP-DENSE", "model_dense_4b"),
    mention("M-02", "MoE-27B", "ExperimentSetup", "A-SETUP-MOE", "model_moe_27b"),
    mention("M-03", "Engram-27B", "ExperimentSetup", "A-SETUP-ENGRAM27", "model_engram_27b"),
    mention("M-04", "Engram-40B", "ExperimentSetup", "A-SETUP-ENGRAM40", "model_engram_40b"),
    mention("M-05", "DeepSeek-v3 tokenizer", "ExperimentSetup", "A-SETUP-DATA", "deepseek_v3_tokenizer"),
    mention("M-06", "MLA", "ExperimentSetup", "A-SETUP-BACKBONE", "multi_head_latent_attention"),
    mention("M-07", "Muon", "ExperimentSetup", "A-SETUP-BACKBONE", "muon_optimizer"),
    mention("M-08", "DeepSeekMoE", "ExperimentSetup", "A-SETUP-MOE", "deepseekmoe"),
    mention("M-09", "validation loss", "ExperimentResult", "A-T1-VALLOSS", "validation_loss"),
    mention("M-10", "MMLU", "ExperimentResult", "A-T1-MMLU", "mmlu"),
    mention("M-11", "BBH", "ExperimentResult", "A-T1-BBH", "bbh"),
    mention("M-12", "ARC-Challenge", "ExperimentResult", "A-T1-ARCC", "arc_challenge"),
    mention("M-13", "GSM8K", "ExperimentResult", "A-T1-GSM8K", "gsm8k"),
    mention("M-14", "MATH", "ExperimentResult", "A-T1-MATH", "math_benchmark"),
    mention("M-15", "iso-FLOPs", "ArticleClaim", "A-RES-SPARSE-DENSE", "iso_flops"),
    mention("M-16", "conditional computation", "ArticleClaim", "A-RES-CLAIM", "conditional_computation"),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "model_engram_27b"),
                 ("aliases", ["Engram-27B", "Engram 27B", "the Engram-27B model"]),
                 ("note", "iso-parameter / iso-FLOPs counterpart to MoE-27B; rho=74.3%, 5.7B Engram.")]),
    OrderedDict([("canonical", "model_moe_27b"),
                 ("aliases", ["MoE-27B", "MoE 27B", "the MoE-27B baseline", "iso-parameter MoE baseline"])]),
    OrderedDict([("canonical", "validation_loss"),
                 ("aliases", ["validation loss", "Validation Set (loss)", "validation-set loss"])]),
    OrderedDict([("canonical", "conditional_memory"),
                 ("aliases", ["conditional memory", "Engram memory", "knowledge lookup primitive", "dedicated knowledge lookup primitive"]),
                 ("note", "qiefen Section 7.3 style: Engram's memory mechanism is referred to by several co-referring names.")]),
]

# ---------------------------------------------------------------------------
# objects (same 4 objects + IDs as v0.1; confidence removed; local/supporting
# + home_package + gold_label/difficulty/evidence_strength added).
# ---------------------------------------------------------------------------
def obj(oid, otype, section_path, home_package, payload, local, supporting,
        difficulty, evidence_strength):
    return OrderedDict([
        ("id", oid), ("type", otype), ("section_path", section_path),
        ("home_package", home_package), ("payload", payload),
        ("local_evidence_atom_ids", local),
        ("supporting_context_atom_ids", supporting),
        ("gold_label", True), ("difficulty", difficulty),
        ("evidence_strength", evidence_strength),
    ])


OBJECTS = [
    obj("SETUP-PRETRAIN", "ExperimentSetup", "4 > 4.1", "PKG-SETUP",
        OrderedDict([
            ("name", "Large-scale pre-training setup"),
            ("models", ["Dense-4B (4.1B total / 3.8B act)",
                        "MoE-27B (26.7B / 3.8B act, 72 routed + 2 shared, top-6)",
                        "Engram-27B (26.7B / 3.8B act, 55 routed + 2 shared, 5.7B Engram, rho=74.3%)",
                        "Engram-40B (39.5B / 3.8B act, 18.5B Engram)"]),
            ("tokens", "262B"),
            ("tokenizer", "DeepSeek-v3, 128k vocab"),
            ("backbone", "30-block Transformer, hidden 2560, MLA 32 heads, mHC expansion 4, Muon optimizer"),
            ("engram_config", "layers 2 and 15, max N-gram 3, 8 heads, dim 1280; Adam lr x5, no weight decay, zero-init conv (identity at start)"),
            ("control", "identical data curriculum (same token budget and order); strictly matched activated parameters (3.8B)"),
            ("evaluation", "language modeling, knowledge, reasoning, reading comprehension, code/math; standard prompting protocols and metrics per benchmark"),
        ]),
        ["A-CH4-INTRO", "A-CH4-INTRO-CONT", "A-SETUP-DATA", "A-SETUP-BACKBONE",
         "A-SETUP-DENSE", "A-SETUP-MOE", "A-SETUP-ENGRAM27", "A-SETUP-ENGRAM40", "A-SETUP-EVAL"],
        [], "medium", "direct"),
    obj("RESULT-TABLE1", "ExperimentResult", "4 > 4.2", "PKG-TABLE1",
        OrderedDict([
            ("name", "Table 1 pre-training benchmark results"),
            ("table_id", "Table 1"),
            ("models_compared", ["Dense-4B", "MoE-27B", "Engram-27B", "Engram-40B"]),
            ("config", ["total params 4.1B / 26.7B / 26.7B / 39.5B",
                        "Engram params - / - / 5.7B / 18.5B"]),
            ("key_findings", ["validation loss 1.768 / 1.634 / 1.622 / 1.610 (lower better)",
                              "MMLU 48.6 / 57.4 / 60.4 / 60.6", "CMMLU 47.9 / 57.9 / 61.9 / 63.4",
                              "BBH 42.8 / 50.9 / 55.9 / 57.5", "ARC-Challenge 59.3 / 70.1 / 73.8 / 76.4",
                              "DROP 41.6 / 55.7 / 59.0 / 60.7", "HumanEval 26.8 / 37.8 / 40.8 / 38.4",
                              "GSM8K 35.5 / 58.4 / 60.6 / 62.6", "MATH 15.2 / 28.3 / 30.7 / 30.6"]),
            ("ordering", "all sparse variants > Dense-4B across all benchmarks; Engram-27B > MoE-27B on essentially all metrics; Engram-40B lowest loss but not strictly dominant (under-training)"),
        ]),
        ["A-T1-CAP", "A-T1-HEADER", "A-T1-TOTALPARAMS", "A-T1-ENGRAMPARAMS", "A-T1-VALLOSS",
         "A-T1-MMLU", "A-T1-MMLUPRO", "A-T1-CMMLU", "A-T1-BBH", "A-T1-ARCC", "A-T1-DROP",
         "A-T1-HUMANEVAL", "A-T1-GSM8K", "A-T1-MATH", "A-RES-ENGRAM40"],
        [], "medium", "direct"),
    obj("CLAIM-MEMORY-BEYOND-KNOWLEDGE", "ArticleClaim", "4 > 4.2", "PKG-TABLE1",
        OrderedDict([
            ("statement", "A dedicated knowledge-lookup primitive (Engram conditional memory) improves representation efficiency beyond allocating the entire sparse budget to conditional computation; gains extend past knowledge to general reasoning, code, and math."),
            ("supporting_deltas", "Engram-27B over MoE-27B: MMLU +3.0, MMLU-Pro +1.8, CMMLU +4.0, BBH +5.0, ARC-Challenge +3.7, DROP +3.3, HumanEval +3.0, MBPP +1.6, GSM8K +2.2, MATH +2.4"),
        ]),
        ["A-RES-ENGRAM-VS-MOE", "A-RES-CLAIM"], [], "medium", "direct"),
    obj("CLAIM-SPARSE-OVER-DENSE", "ArticleClaim", "4 > 4.2", "PKG-TABLE1",
        OrderedDict([
            ("statement", "Sparse architectures (MoE / Engram) follow superior scaling laws to dense models: under iso-FLOPs all sparse variants outperform Dense-4B across all benchmarks."),
        ]),
        ["A-RES-SPARSE-DENSE"], [], "easy", "direct"),
]
OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# relations (same 3 relations + IDs as v0.1; confidence removed; relation_type
# labels preserved; experiment_tests_setup kept as a documented extension).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


RELATIONS = [
    rel("R-01", "experiment_tests_setup", "RESULT-TABLE1", "SETUP-PRETRAIN",
        ["A-T1-CAP", "A-SETUP-ENGRAM27"], "medium", "direct"),
    rel("R-02", "result_supports_claim", "RESULT-TABLE1", "CLAIM-MEMORY-BEYOND-KNOWLEDGE",
        ["A-T1-MMLU", "A-T1-CMMLU", "A-T1-BBH", "A-T1-ARCC", "A-T1-DROP",
         "A-T1-HUMANEVAL", "A-T1-GSM8K", "A-T1-MATH", "A-RES-ENGRAM-VS-MOE"], "medium", "direct"),
    rel("R-03", "result_supports_claim", "RESULT-TABLE1", "CLAIM-SPARSE-OVER-DENSE",
        ["A-T1-VALLOSS", "A-RES-SPARSE-DENSE"], "easy", "direct"),
]

# ---------------------------------------------------------------------------
# do_not_extract: inline citations + policy; Table 2 / figure refs as out-of-slice.
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([("text", "(Liu et al., 2024a)"), ("atom_id", "A-SETUP-DATA"),
                 ("reason", "inline author-year citation, not a knowledge object"), ("kind", "citation")]),
    OrderedDict([("text", "(Xie et al., 2025)"), ("atom_id", "A-SETUP-BACKBONE"),
                 ("reason", "inline author-year citation, not a knowledge object"), ("kind", "citation")]),
    OrderedDict([("text", "(Borgeaud et al., 2022; He, 2024; Shazeer et al., 2017)"), ("atom_id", "A-RES-SPARSE-DENSE"),
                 ("reason", "inline author-year citations, not knowledge objects"), ("kind", "citation")]),
    OrderedDict([("pattern", "inline_author_year_citation"),
                 ("examples", ["(Liu et al., 2024a)", "(DeepSeek-AI et al., 2024)", "(Xie et al., 2025)",
                               "(Jordan et al., 2024; Team et al., 2025)", "(Dai et al., 2024)", "(Kingma, 2014)",
                               "(Borgeaud et al., 2022; He, 2024; Shazeer et al., 2017)"]),
                 ("reason", "inline author-year citations are not knowledge objects; this policy suppresses citation over-extraction consistently across chapters."),
                 ("kind", "citation_policy")]),
    OrderedDict([("text", "Appendix B"), ("atom_id", "A-T1-CAP"),
                 ("reason", "cross-reference to an appendix outside the chapter slice; not a knowledge object"),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "the Appendix A"), ("atom_id", "A-SETUP-BACKBONE"),
                 ("reason", "cross-reference to an appendix outside the chapter slice"),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "Table 2 | Long-context performance comparison"), ("source_lines", [190, 190]),
                 ("reason", "Table 2 belongs to Chapter 5 (Long Context Training); it is outside the 158-189 slice and must not be atomized here."),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "Figure 3 (right)"), ("source_lines", [156, 156]),
                 ("reason", "Figure 3 (scaling-law figure) belongs to Chapter 3; it precedes the slice and is referenced out of scope."),
                 ("kind", "out_of_slice_reference")]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Chapter 4 only (4.1 Experimental Setup + 4.2 Experimental Results + Table 1)"),
    ("title", "Engram: Conditional Memory Complements Conditional Computation - Chapter 4 Large Scale Pre-training"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 158-189; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "article_research"),
    ("profile_cues", ["Experimental Setup", "Experimental Results", "Table 1", "we train four models",
                      "validation loss", "iso-FLOPs", "Appendix B"]),
    ("extraction_targets", ["ExperimentSetup", "ExperimentResult", "ArticleClaim"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate. Some Section 4.2 prose atoms (rendered by MinerU after Table 2 at lines 194/196) carry source_span only and are flagged metadata.out_of_viewer_slice:true."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span: it may rephrase, transliterate math (rho, 5x, N-gram, arrow ->), and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs not inside the span. For table_row_atom the normalized text re-expresses the anchored <tr>/<td> substring as a readable row; every number in the normalized text must occur in the raw substring (NUMERIC over-span audit)."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone. Chapter 4 has no cross-package objects (every object's evidence is local to its home package), so no expected_local_fields are declared."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "Chapter 4 contains no figures; Figure 3 (Chapter 3) is referenced just before the slice and is recorded in do_not_extract as out_of_slice_reference."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref; none needed in Chapter 4."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("tables", "Table 1 is a single MinerU HTML <table> on one line. Its caption is a separate paragraph atom (table_caption_atom). The header <tr> and each kept metric row are anchored as verbatim substrings of the <table> line (table_header_atom / table_row_atom). rowspan cells (Language Modeling / Knowledge & Reasoning / Reading Comprehension / Code & Math) are metric-group labels, not data columns; the HTML entity &amp; is normalized to 'and'."),
        ("context_only", "an atom not consumed by any object or relation may be flagged metadata.context_only:true; Chapter 4 has none (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "Table 1 is a single HTML <table> (source line 164): one header <tr> + metric rows; left rowspan cells (Language Modeling / Knowledge & Reasoning / Reading Comprehension / Code & Math) are metric-group labels, not data columns. The leading empty <td> in the header is the rowspan placeholder column.",
        "Header columns are: Benchmark (Metric) | # Shots | Dense-4B | MoE-27B | Engram-27B | Engram-40B. Each table_row_atom is the anchored verbatim <tr>/<td> substring for that row; its normalized text re-expresses the four model columns.",
        "The HTML entity &amp; appears in the group labels 'Knowledge &amp; Reasoning' and 'Code &amp; Math' and is normalized to 'and'.",
        "The Table 1 caption (line 162, above the <table>) holds the key control variables (262B tokens, 3.8B activated, 72 -> 55 expert reallocation, 5.7B / 18.5B Engram) and is a separate table_caption_atom.",
        "The chapter-intro sentence is split by Table 1: it starts on line 160 (models 1-2) and resumes on line 166 (models 3-4 + control variables). The two halves are atoms A-CH4-INTRO and A-CH4-INTRO-CONT.",
        "MinerU rendered three Section 4.2 prose sentences AFTER Table 2: line 194 (Engram-27B vs MoE-27B deltas + the support-hypothesis sentence) and line 196 (Engram-40B under-training). They are Section 4.2 content but fall OUTSIDE the 158-189 viewer slice, so atoms A-RES-ENGRAM-VS-MOE / A-RES-CLAIM / A-RES-ENGRAM40 carry source_span only (no viewer_span) and are flagged out_of_viewer_slice.",
        "Table 2 (lines 190-192) belongs to Chapter 5 (Long Context Training) and is OUT of slice (see do_not_extract).",
        "Inline math is ASCII-normalized: $\\rho = 74.3\\%$ -> rho = 74.3%, $5\\times$ -> 5x, $N$ -gram -> N-gram.",
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
        "# Gold fixture v0.3.3 - Engram paper Chapter 4 Large Scale Pre-training (profile: article_research)\n"
        "# Upgraded from v0.1: dual coordinates (source_span authoritative + viewer_span), verbatim raw_text +\n"
        "# within-span normalized_text, Table 1 atomized as caption + header <tr> + anchored <tr>/<td> row\n"
        "# substrings (NUMERIC over-span audited), object evidence split into local/supporting + home_package,\n"
        "# per-chunk context_packages with expected_objects, gold_label/difficulty/evidence_strength\n"
        "# (no decimal confidence), and do_not_extract negatives (citations + out-of-slice Table 2 / Figure 3).\n"
        "# Spans are computed by build.py against engram_paper_mineru.md; do NOT hand-edit.\n"
    )
    body = yaml.dump(GOLD, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=100000)
    with open(os.path.join(HERE, "gold.yaml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    print("wrote source.md (%d bytes) and gold.yaml" % len(VIEWER_TEXT))
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d mentions=%d do_not_extract=%d" % (
        len(ATOMS), len(SOURCE_ELEMENTS), len(SEMANTIC_CHUNKS),
        len(CONTEXT_PACKAGES), len(OBJECTS), len(RELATIONS), len(MENTIONS), len(DO_NOT_EXTRACT)))


if __name__ == "__main__":
    main()
