#!/usr/bin/env python3
"""Builder for ch05_long_context gold.yaml (schema v0.3.3, profile article_research).

Regenerates source.md (viewer-only verbatim slice) and gold.yaml from the
authoritative MinerU source file. All spans are computed by locating verbatim
substrings (anchors) in the source line; YAML spans are never hand-written.

Upgrades the v0.1 fixture to v0.3.3: keeps every atom/chunk/object/relation ID
and its meaning, but re-expresses each atom as a dual-coordinate verbatim span
(source_span authoritative + viewer_span viewer-only) with raw_text + within-span
normalized_text, splits object evidence into local/supporting + home_package,
adds per-chunk context_packages, gold_label/difficulty/evidence_strength (no
decimal confidence), and do_not_extract negatives.

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/engram_paper_mineru.md"
SOURCE_NAME = "engram_paper_mineru.md"
VIEWER_NAME = "source.md"

# Authoritative slice: lines 190-221 (Table 2 caption + Table 2 HTML + Section 5
# intro + 5.1 Experimental Setup + 5.2 Experimental Results prose).
SLICE_LINE_START = 190
SLICE_LINE_END = 221

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

# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice of lines 190-221 + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY

# Per source-line: viewer-file line number and viewer-file char offset of line start.
# Viewer file = header line (1) + slice lines.
_VIEWER_LINE_OFF = {}  # source line_no -> char offset in VIEWER_TEXT of that line's start
_VIEWER_LINE_NO = {}   # source line_no -> 1-based line number in VIEWER_TEXT
_voff = len(VIEWER_HEADER)
for _k, _srcln in enumerate(range(SLICE_LINE_START, SLICE_LINE_END + 1)):
    _VIEWER_LINE_OFF[_srcln] = _voff
    _VIEWER_LINE_NO[_srcln] = 2 + _k  # header is line 1
    _voff += len(SRC_LINES[_srcln - 1]) + 1


def span_for(raw, line_no):
    """raw must be a verbatim contiguous substring of source line `line_no`."""
    line_text = SRC_LINES[line_no - 1]
    idx = line_text.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND on line %d: %r" % (line_no, raw[:70]))
    if line_text.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS on line %d (matches >1): %r" % (line_no, raw[:70]))
    src_cs = _LINE_OFF[line_no] + idx
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch %r" % raw[:60]
    v_cs = _VIEWER_LINE_OFF[line_no] + idx
    v_ce = v_cs + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch %r" % raw[:60]
    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", line_no), ("line_end", line_no),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", _VIEWER_LINE_NO[line_no]), ("line_end", _VIEWER_LINE_NO[line_no]),
        ("char_start", v_cs), ("char_end", v_ce),
        ("viewer_only", True),
    ])
    return source_span, viewer_span


def atom(aid, section_id, atom_type, raw, line_no, normalized, evidence_strength, se_id, metadata=None):
    ss, vs = span_for(raw, line_no)
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
# Source element ids (auto line ranges filled later from atoms; headings added).
# ---------------------------------------------------------------------------
SE_CAPTION = "SE-5.2-T2CAP"   # line 190 (Table 2 caption)
SE_TABLE = "SE-5.2-T2"        # line 192 (Table 2 HTML)
SE_INTRO = "SE-5-INTRO"       # line 200 (Section 5 intro paragraph)
SE_SETUP_TD = "SE-5.1-TD"     # line 204 (Training Details)
SE_SETUP_MC = "SE-5.1-MC"     # line 206 (Model Configurations)
SE_SETUP_EB = "SE-5.1-EB"     # line 208 (Evaluation Benchmarks)
SE_RES_COUP = "SE-5.2-COUP"   # line 214 (finding 1: coupling)
SE_RES_ISOLOSS = "SE-5.2-ISOLOSS"   # line 218 (Iso-Loss bullet)
SE_RES_ISOFLOPS = "SE-5.2-ISOFLOPS" # line 219 (Iso-FLOPs bullet)
SE_RES_EXTREME = "SE-5.2-EXTREME"   # line 220 (Extreme bullet)

# ---------------------------------------------------------------------------
# Evidence atoms.  raw_text = verbatim substrings of the indicated source line.
# normalized_text renders EXACTLY that span (ascii math, in-span pronoun resolve).
# IDs and meaning preserved from the v0.1 fixture.
# ---------------------------------------------------------------------------
ATOMS = []

# ---------- 5.0 chapter intro: core claim + mechanism (line 200) ----------
ATOMS.append(atom(
    "A-INTRO-MECH", "SEC-5", "mechanism_sentence",
    "By offloading local dependency modeling to static lookups, the Engram architecture preserves valuable attention capacity for managing global context.",
    200,
    "By offloading local dependency modeling to static lookups, the Engram architecture preserves valuable attention capacity for managing global context.",
    "direct", SE_INTRO,
))
ATOMS.append(atom(
    "A-INTRO-CLAIM", "SEC-5", "claim_sentence",
    "we demonstrate that Engram yields significant gains in long-range retrieval and reasoning tasks.",
    200,
    "we demonstrate that Engram yields significant gains in long-range retrieval and reasoning tasks.",
    "direct", SE_INTRO,
))

# ---------- 5.1 Experimental Setup ----------
# A-SETUP-YARN (line 204): YaRN + 32768 + 5000 steps + hyperparameters. The
# hyperparameter clause contains inline math $s = 10, \alpha = 1, \beta = 32$.
ATOMS.append(atom(
    "A-SETUP-YARN", "SEC-5.1", "experiment_setup_atom",
    "Following the pre-training stage, we apply YaRN (Peng et al., 2024) for context window extension in a 32768-token context training stage for 5,000 steps (30B tokens of high-quality, long-context data). The hyper-parameters are scale $s = 10, \\alpha = 1, \\beta = 32$ and the scaling factor f = 0.707.",
    204,
    "Following the pre-training stage, we apply YaRN (Peng et al., 2024) for context window extension in a 32768-token context training stage for 5,000 steps (30B tokens of high-quality, long-context data). The hyper-parameters are scale s = 10, alpha = 1, beta = 32 and the scaling factor f = 0.707.",
    "direct", SE_SETUP_TD,
    metadata=OrderedDict([("role", "training_details"),
                          ("method", "YaRN context extension (DeepSeek-V3 strategy)")]),
))
# A-SETUP-CONFIGS (line 206): four configurations / four checkpoints.
ATOMS.append(atom(
    "A-SETUP-CONFIGS", "SEC-5.1", "experiment_setup_atom",
    "We compare context extensions across four distinct model configurations. We utilize the final pre-training checkpoints (50k steps) for both MoE-27B and Engram-27B. Additionally, to rigorously benchmark architectural efficiency, we select two intermediate checkpoints for Engram-27B at 41k and 46k steps. Despite differing initialization stages, all variants undergo the exact same context extension training protocol.",
    206,
    "We compare context extensions across four distinct model configurations. We utilize the final pre-training checkpoints (50k steps) for both MoE-27B and Engram-27B. Additionally, to rigorously benchmark architectural efficiency, we select two intermediate checkpoints for Engram-27B at 41k and 46k steps. Despite differing initialization stages, all variants undergo the exact same context extension training protocol.",
    "direct", SE_SETUP_MC,
    metadata=OrderedDict([("role", "model_configurations")]),
))
# A-SETUP-ISOLOSS (line 206): Iso-Loss control rationale.
ATOMS.append(atom(
    "A-SETUP-ISOLOSS", "SEC-5.1", "experiment_setup_atom",
    "Crucially, Engram-27B (46k) is selected because it exhibits the same pre-training loss as the fully trained MoE-27B (50k). This creates a controlled \"Iso-Loss\" setting, ensuring that any performance divergence during context extension is attributable to the architecture rather than the starting quality of the model.",
    206,
    "Engram-27B (46k) is selected because it exhibits the same pre-training loss as the fully trained MoE-27B (50k). This creates a controlled \"Iso-Loss\" setting, ensuring that any performance divergence during context extension is attributable to the architecture rather than the starting quality of the model.",
    "direct", SE_SETUP_MC,
    metadata=OrderedDict([("role", "iso_loss_control")]),
))
# A-SETUP-LONGPPL (line 208): LongPPL four categories.
ATOMS.append(atom(
    "A-SETUP-LONGPPL", "SEC-5.1", "experiment_setup_atom",
    "For LongPPL, we construct evaluation sets spanning four categories: long books, research papers, code repositories, and long chain-of-thought (CoT) trajectories.",
    208,
    "For LongPPL, we construct evaluation sets spanning four categories: long books, research papers, code repositories, and long chain-of-thought (CoT) trajectories.",
    "direct", SE_SETUP_EB,
    metadata=OrderedDict([("role", "benchmark"), ("benchmark", "LongPPL")]),
))
# A-SETUP-RULER (line 208): RULER 14 subsets -> 8 categories.
ATOMS.append(atom(
    "A-SETUP-RULER", "SEC-5.1", "experiment_setup_atom",
    "For RULER, we evaluate on 14 subsets aggregated into 8 categories: Single (S), Multi-keys (MK), Multi-values (MV) and Multi-queries (MQ) Needle-in-a-Haystack; Multi-hop Variable Tracking (VT), Common Words Extraction (CWE), Frequent Words Extraction (FWE), and Question Answering (QA).",
    208,
    "For RULER, we evaluate on 14 subsets aggregated into 8 categories: Single (S), Multi-keys (MK), Multi-values (MV) and Multi-queries (MQ) Needle-in-a-Haystack; Multi-hop Variable Tracking (VT), Common Words Extraction (CWE), Frequent Words Extraction (FWE), and Question Answering (QA).",
    "direct", SE_SETUP_EB,
    metadata=OrderedDict([("role", "benchmark"), ("benchmark", "RULER")]),
))

# ---------- Table 2 (result table; caption line 190, HTML line 192) ----------
# A-T2-CAPTION (line 190): caption sentence + parenthetical-values explanation.
ATOMS.append(atom(
    "A-T2-CAPTION", "SEC-5.2", "table_caption_atom",
    "Table 2 | Long-context performance comparison. Parenthetical values (e.g. (50k, 1.62)) denote the pre-training steps and the corresponding loss prior to the long-context extension.",
    190,
    "Table 2 | Long-context performance comparison. Parenthetical values (e.g. (50k, 1.62)) denote the pre-training steps and the corresponding loss prior to the long-context extension.",
    "direct", SE_CAPTION,
    metadata=OrderedDict([("table_id", "Table 2")]),
))
# A-T2-HEADER (line 192): anchored substring capturing the nested 3-row header
# (Model rowspan=3; LongPPL colspan=4 / RULER colspan=8; sub-rows; detail cols).
T2_HEADER_RAW = (
    '<table><tr><td rowspan="3">Model</td><td colspan="4">LongPPL (32k)</td><td colspan="8">RULER (32k)</td></tr>'
    '<tr><td colspan="4">Perplexity (↓)</td><td colspan="4">NIAH Accuracy (↑)</td><td colspan="4">Other Tasks (↑)</td></tr>'
    '<tr><td>Book</td><td>Paper</td><td>Code</td><td>L-CoT</td><td>S</td><td>MK</td><td>MV</td><td>MQ</td><td>VT</td><td>CWE</td><td>FWE</td><td>QA</td></tr>'
)
ATOMS.append(atom(
    "A-T2-HEADER", "SEC-5.2", "table_header_atom",
    T2_HEADER_RAW,
    192,
    "Nested 3-row header. Row1: Model (rowspan 3) | LongPPL (32k) (colspan 4) | RULER (32k) (colspan 8). "
    "Row2: Perplexity (lower better) (colspan 4) | NIAH Accuracy (higher better) (colspan 4) | Other Tasks (higher better) (colspan 4). "
    "Row3 detail columns: Book, Paper, Code, L-CoT | S, MK, MV, MQ | VT, CWE, FWE, QA.",
    "direct", SE_TABLE,
    metadata=OrderedDict([("table_id", "Table 2"), ("header_rows", 3)]),
))
# Table rows: each raw is the anchored <tr>...</tr> for one model row.
T2_ROW_MOE50K = '<tr><td>MoE-27B (50k, 1.63)</td><td>4.38</td><td>2.91</td><td>2.49</td><td>14.16</td><td>100.0</td><td>88.0</td><td>92.7</td><td>84.2</td><td>77.0</td><td>4.5</td><td>73.0</td><td>34.5</td></tr>'
T2_ROW_ENG41K = '<tr><td>Engram-27B (41k, 1.66)</td><td>4.37</td><td>2.92</td><td>2.50</td><td>14.26</td><td>99.6</td><td>88.3</td><td>93.0</td><td>89.5</td><td>83.2</td><td>3.8</td><td>99.6</td><td>44.0</td></tr>'
T2_ROW_ENG46K = '<tr><td>Engram-27B (46k, 1.63)</td><td>4.19</td><td>2.84</td><td>2.45</td><td>13.59</td><td>97.6</td><td>89.0</td><td>95.5</td><td>97.0</td><td>87.2</td><td>4.3</td><td>98.6</td><td>37.5</td></tr>'
T2_ROW_ENG50K = '<tr><td>Engram-27B (50k, 1.62)</td><td>4.14</td><td>2.82</td><td>2.44</td><td>13.41</td><td>99.3</td><td>89.3</td><td>96.5</td><td>97.0</td><td>89.0</td><td>5.9</td><td>99.3</td><td>40.5</td></tr>'

ATOMS.append(atom(
    "A-T2-ROW-MOE50K", "SEC-5.2", "table_row_atom", T2_ROW_MOE50K, 192,
    "MoE-27B (50k, 1.63): LongPPL Book 4.38 / Paper 2.91 / Code 2.49 / L-CoT 14.16; "
    "RULER NIAH S 100.0 / MK 88.0 / MV 92.7 / MQ 84.2; VT 77.0 / CWE 4.5 / FWE 73.0 / QA 34.5.",
    "direct", SE_TABLE,
    metadata=OrderedDict([("table_id", "Table 2"), ("row", "MoE-27B (50k)"), ("note", "baseline")]),
))
ATOMS.append(atom(
    "A-T2-ROW-ENG41K", "SEC-5.2", "table_row_atom", T2_ROW_ENG41K, 192,
    "Engram-27B (41k, 1.66): LongPPL Book 4.37 / Paper 2.92 / Code 2.50 / L-CoT 14.26; "
    "RULER NIAH S 99.6 / MK 88.3 / MV 93.0 / MQ 89.5; VT 83.2 / CWE 3.8 / FWE 99.6 / QA 44.0.",
    "direct", SE_TABLE,
    metadata=OrderedDict([("table_id", "Table 2"), ("row", "Engram-27B (41k)"), ("note", "early-stopped, ~82% FLOPs")]),
))
ATOMS.append(atom(
    "A-T2-ROW-ENG46K", "SEC-5.2", "table_row_atom", T2_ROW_ENG46K, 192,
    "Engram-27B (46k, 1.63): LongPPL Book 4.19 / Paper 2.84 / Code 2.45 / L-CoT 13.59; "
    "RULER NIAH S 97.6 / MK 89.0 / MV 95.5 / MQ 97.0; VT 87.2 / CWE 4.3 / FWE 98.6 / QA 37.5.",
    "direct", SE_TABLE,
    metadata=OrderedDict([("table_id", "Table 2"), ("row", "Engram-27B (46k)"), ("note", "Iso-Loss vs MoE-27B(50k)")]),
))
ATOMS.append(atom(
    "A-T2-ROW-ENG50K", "SEC-5.2", "table_row_atom", T2_ROW_ENG50K, 192,
    "Engram-27B (50k, 1.62): LongPPL Book 4.14 / Paper 2.82 / Code 2.44 / L-CoT 13.41; "
    "RULER NIAH S 99.3 / MK 89.3 / MV 96.5 / MQ 97.0; VT 89.0 / CWE 5.9 / FWE 99.3 / QA 40.5.",
    "direct", SE_TABLE,
    metadata=OrderedDict([("table_id", "Table 2"), ("row", "Engram-27B (50k)"), ("note", "Iso-FLOPs vs MoE-27B(50k); best across the board")]),
))

# ---------- 5.2 Experimental Results (interpretation) ----------
# A-RES-COUPLING (line 214): long-context coupled with base-model quality.
ATOMS.append(atom(
    "A-RES-COUPLING", "SEC-5.2", "result_sentence",
    "Observing the trajectory of Engram (41k → 50k), we find that long-context performance improves monotonically with pre-training progression, even when controlling for identical model architecture and a fixed computational budget during the context extension stage. This suggests that long-context performance is intrinsically coupled with the general modeling ability of the base model. Consequently, a rigorous architectural comparison must control for this confounding variable by aligning base model loss, rather than merely aligning training steps.",
    214,
    "Observing the trajectory of Engram (41k -> 50k), we find that long-context performance improves monotonically with pre-training progression, even when controlling for identical model architecture and a fixed computational budget during the context extension stage. This suggests that long-context performance is intrinsically coupled with the general modeling ability of the base model. Consequently, a rigorous architectural comparison must control for this confounding variable by aligning base model loss, rather than merely aligning training steps.",
    "direct", SE_RES_COUP,
))
# A-RES-ISOLOSS (line 218): Iso-Loss 46k vs baseline; MQ NIAH 97.0 vs 84.2; VT 87.2 vs 77.0.
ATOMS.append(atom(
    "A-RES-ISOLOSS", "SEC-5.2", "result_sentence",
    "When comparing Engram-27B (46k) against the fully trained MoE-27B (50k)—models aligned on pre-training loss—Engram demonstrates significant gains. Specifically, it outperforms the baseline on complex retrieval tasks (e.g., Multi-Query NIAH: 97.0 vs. 84.2; VT: 87.2 vs. 77.0).",
    218,
    "When comparing Engram-27B (46k) against the fully trained MoE-27B (50k)--models aligned on pre-training loss--Engram demonstrates significant gains. Specifically, it outperforms the baseline on complex retrieval tasks (e.g., Multi-Query NIAH: 97.0 vs. 84.2; VT: 87.2 vs. 77.0).",
    "direct", SE_RES_ISOLOSS,
    metadata=OrderedDict([("setting", "Iso-Loss")]),
))
# A-RES-ISOFLOPS (line 219): Iso-FLOPs 50k widens gap, highest across the board.
ATOMS.append(atom(
    "A-RES-ISOFLOPS", "SEC-5.2", "result_sentence",
    "Under the standard iso-compute budget, Engram-27B (50k) further widens this gap, establishing the highest performance across the board.",
    219,
    "Under the standard iso-compute budget, Engram-27B (50k) further widens this gap, establishing the highest performance across the board.",
    "direct", SE_RES_ISOFLOPS,
    metadata=OrderedDict([("setting", "Iso-FLOPs")]),
))
# A-RES-EXTREME (line 220): Extreme ~82% compute, 41k matches LongPPL, surpasses RULER.
ATOMS.append(atom(
    "A-RES-EXTREME", "SEC-5.2", "result_sentence",
    "Even the early-stopped Engram-27B (41k) remains highly competitive against the fully trained MoE-27B (50k). It matches the baseline on LongPPL and surpasses it on RULER, underscoring the intrinsic superiority of the Engram architecture.",
    220,
    "Even the early-stopped Engram-27B (41k) remains highly competitive against the fully trained MoE-27B (50k). It matches the baseline on LongPPL and surpasses it on RULER, underscoring the intrinsic superiority of the Engram architecture.",
    "direct", SE_RES_EXTREME,
    metadata=OrderedDict([("setting", "Extreme (~82% compute)")]),
))

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# source_elements: headings + paragraph/table elements grouped by source_element_id.
# ---------------------------------------------------------------------------
SE_TYPE = {
    SE_CAPTION: "table_caption",
    SE_TABLE: "table",
    SE_INTRO: "paragraph",
    SE_SETUP_TD: "paragraph",
    SE_SETUP_MC: "paragraph",
    SE_SETUP_EB: "paragraph",
    SE_RES_COUP: "paragraph",
    SE_RES_ISOLOSS: "paragraph",
    SE_RES_ISOFLOPS: "paragraph",
    SE_RES_EXTREME: "paragraph",
}


def build_source_elements(atoms):
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    elems = []
    # Headings for the three sections (lines 198 / 202 / 210).
    elems.append(OrderedDict([("id", "SE-H-5"), ("type", "heading"),
                              ("file", SOURCE_NAME), ("line_start", 198), ("line_end", 198)]))
    elems.append(OrderedDict([("id", "SE-H-5.1"), ("type", "heading"),
                              ("file", SOURCE_NAME), ("line_start", 202), ("line_end", 202)]))
    elems.append(OrderedDict([("id", "SE-H-5.2"), ("type", "heading"),
                              ("file", SOURCE_NAME), ("line_start", 210), ("line_end", 210)]))
    # element ordering by first line
    ordered = sorted(groups.items(), key=lambda kv: min(m["source_span"]["line_start"] for m in kv[1]))
    for se_id, members in ordered:
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        elems.append(OrderedDict([("id", se_id), ("type", SE_TYPE[se_id]),
                                  ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le)]))
    return elems


SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ---------------------------------------------------------------------------
# section_tree
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-5"), ("path", "5"), ("title", "Long Context Training")]),
    OrderedDict([("id", "SEC-5.1"), ("path", "5 > 5.1"), ("title", "Experimental Setup"), ("parent", "SEC-5")]),
    OrderedDict([("id", "SEC-5.2"), ("path", "5 > 5.2"), ("title", "Experimental Results"), ("parent", "SEC-5")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks: C-INTRO / C-SETUP / C-RESULT (IDs & membership preserved).
# ---------------------------------------------------------------------------
SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C-INTRO"), ("profile", "article_research"),
        ("chunk_type", "article_core_claim_block"), ("section_path", "5"),
        ("atom_ids", ["A-INTRO-MECH", "A-INTRO-CLAIM"]),
        ("central_atom_ids", ["A-INTRO-CLAIM"]),
        ("boundary_reason",
         "Section-opening claim (significant gains in long-range retrieval and reasoning) plus its mechanistic "
         "rationale (offloading local dependencies to static lookups frees attention capacity for global context); "
         "kept together as one core-claim block (qiefen Section 5.3)."),
        ("extraction_targets", ["ArticleClaim", "MechanisticExplanation"]),
        ("gold_must_cover_atoms", ["A-INTRO-CLAIM", "A-INTRO-MECH"]),
    ]),
    OrderedDict([
        ("id", "C-SETUP"), ("profile", "article_research"),
        ("chunk_type", "experiment_setup_block"), ("section_path", "5 > 5.1"),
        ("atom_ids", ["A-SETUP-YARN", "A-SETUP-CONFIGS", "A-SETUP-ISOLOSS", "A-SETUP-LONGPPL", "A-SETUP-RULER"]),
        ("central_atom_ids", ["A-SETUP-CONFIGS", "A-SETUP-ISOLOSS"]),
        ("boundary_reason",
         "Training details + four checkpoints + Iso-Loss control + both benchmarks form one self-contained setup "
         "unit (experiment_setup_result_dependency keeps it adjacent to the result block); not split at the run-in "
         "bold sub-headings Training Details./Model Configurations./Evaluation Benchmarks."),
        ("extraction_targets", ["ExperimentSetup"]),
        ("gold_must_cover_atoms", ["A-SETUP-YARN", "A-SETUP-CONFIGS", "A-SETUP-ISOLOSS", "A-SETUP-LONGPPL", "A-SETUP-RULER"]),
    ]),
    OrderedDict([
        ("id", "C-RESULT"), ("profile", "article_research"),
        ("chunk_type", "experiment_result_block"), ("section_path", "5 > 5.2"),
        ("atom_ids", ["A-T2-CAPTION", "A-T2-HEADER", "A-T2-ROW-MOE50K", "A-T2-ROW-ENG41K",
                      "A-T2-ROW-ENG46K", "A-T2-ROW-ENG50K", "A-RES-COUPLING", "A-RES-ISOLOSS",
                      "A-RES-ISOFLOPS", "A-RES-EXTREME"]),
        ("central_atom_ids", ["A-T2-CAPTION", "A-RES-ISOLOSS"]),
        ("boundary_reason",
         "Table 2 (caption + nested header + all four model rows) kept with its prose interpretation "
         "(table_header_row_dependency; the result sentences cite exact table numbers, e.g. MQ NIAH 97.0 vs 84.2)."),
        ("extraction_targets", ["ExperimentResult", "ArticleClaim"]),
        ("gold_must_cover_atoms", ["A-T2-CAPTION", "A-T2-HEADER", "A-T2-ROW-MOE50K", "A-T2-ROW-ENG46K",
                                   "A-T2-ROW-ENG50K", "A-RES-COUPLING", "A-RES-ISOLOSS", "A-RES-EXTREME"]),
    ]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}
CHUNK_BY_ID = {c["id"]: c for c in SEMANTIC_CHUNKS}

# ---------------------------------------------------------------------------
# objects (IDs preserved). home_package + local/supporting evidence split.
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
    obj("CLAIM-FREE-ATTENTION", "ArticleClaim", "5", "PKG-INTRO",
        OrderedDict([
            ("statement", "By offloading local dependency modeling to static lookups, Engram frees attention capacity for global context, yielding significant gains in long-range retrieval and reasoning."),
            ("scope", "long-context extension training"),
        ]),
        ["A-INTRO-CLAIM", "A-INTRO-MECH"],
        ["A-T2-ROW-ENG46K", "A-T2-ROW-MOE50K", "A-RES-ISOLOSS"],
        "medium", "direct"),
    obj("CLAIM-ALIGN-LOSS", "ArticleClaim", "5 > 5.2", "PKG-RESULT",
        OrderedDict([
            ("statement", "Long-context performance is intrinsically coupled with base-model quality; a rigorous architectural comparison must align base-model loss rather than training steps."),
            ("evidence_basis", "Engram trajectory 41k -> 50k improves monotonically under fixed extension compute"),
        ]),
        ["A-RES-COUPLING"], [], "medium", "indirect"),
    obj("MECH-LOOKUP-DELEGATION", "MechanisticExplanation", "5", "PKG-INTRO",
        OrderedDict([
            ("mechanism", "Delegating local dependency modeling to static lookups relieves attention of local modeling, preserving attention capacity for managing global / long-range context."),
        ]),
        ["A-INTRO-MECH"], [], "medium", "direct"),
    obj("SETUP-LONGCTX", "ExperimentSetup", "5 > 5.1", "PKG-SETUP",
        OrderedDict([
            ("name", "Long-context extension protocol"),
            ("method", "YaRN context window extension to 32768 tokens"),
            ("budget", "5,000 steps / 30B tokens long-context data"),
            ("hyperparameters", "s = 10, alpha = 1, beta = 32, f = 0.707"),
            ("configurations", ["MoE-27B (50k)", "Engram-27B (50k)", "Engram-27B (46k)", "Engram-27B (41k)"]),
            ("controls", ["Iso-Loss: Engram-27B(46k) pre-train loss == MoE-27B(50k) loss",
                          "all variants share the exact same context extension protocol"]),
            ("benchmarks", ["LongPPL (books / papers / code / long CoT)",
                            "RULER (14 subsets -> 8 categories: S/MK/MV/MQ NIAH, VT, CWE, FWE, QA)"]),
        ]),
        ["A-SETUP-YARN", "A-SETUP-CONFIGS", "A-SETUP-ISOLOSS", "A-SETUP-LONGPPL", "A-SETUP-RULER"],
        [], "medium", "direct"),
    obj("RESULT-TABLE2", "ExperimentResult", "5 > 5.2", "PKG-RESULT",
        OrderedDict([
            ("name", "Table 2 - Long-context performance comparison"),
            ("metric_groups", OrderedDict([
                ("LongPPL_perplexity_lower_better", ["Book", "Paper", "Code", "L-CoT"]),
                ("RULER_NIAH_higher_better", ["S", "MK", "MV", "MQ"]),
                ("RULER_other_higher_better", ["VT", "CWE", "FWE", "QA"]),
            ])),
            ("key_rows", OrderedDict([
                ("MoE-27B (50k)", OrderedDict([("MQ_NIAH", 84.2), ("VT", 77.0), ("FWE", 73.0)])),
                ("Engram-27B (46k)", OrderedDict([("MQ_NIAH", 97.0), ("VT", 87.2)])),
                ("Engram-27B (50k)", OrderedDict([("MQ_NIAH", 97.0), ("VT", 89.0)])),
            ])),
            ("findings", ["Engram-27B(50k) best across the board (Iso-FLOPs)",
                          "Engram-27B(46k) beats MoE-27B(50k) on retrieval under Iso-Loss",
                          "Engram-27B(41k) matches LongPPL and surpasses on RULER at ~82% compute"]),
        ]),
        ["A-T2-CAPTION", "A-T2-HEADER", "A-T2-ROW-MOE50K", "A-T2-ROW-ENG41K", "A-T2-ROW-ENG46K",
         "A-T2-ROW-ENG50K", "A-RES-ISOLOSS", "A-RES-ISOFLOPS", "A-RES-EXTREME"],
        [], "medium", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids. expected_objects =
# objects whose home_package is this package. expected_local_fields for any
# cross-package object whose local evidence sits (partly) here.
# ---------------------------------------------------------------------------
def build_package(pid, chunk_id, doc_title, section_path, linked_context, targets,
                  expected_objects, expected_local_fields=None, expected_classification=None):
    d = OrderedDict([
        ("id", pid),
        ("profile", "article_research"),
        ("chunk_id", chunk_id),
        ("section_path", section_path),
        ("document_title", doc_title),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", linked_context),
        ("extraction_targets", targets),
        ("expected_objects", expected_objects),
    ])
    if expected_local_fields:
        d["expected_local_fields"] = expected_local_fields
    if expected_classification:
        d["expected_classification"] = expected_classification
    return d


DOC_TITLE = "Conditional Memory via Scalable Lookup: A New Axis of Sparsity for Large Language Models"

CONTEXT_PACKAGES = [
    build_package(
        "PKG-INTRO", "C-INTRO", DOC_TITLE, "Chapter 5 > 5 Long Context Training",
        OrderedDict([
            ("previous_heading", "4.2 Experimental Results"),
            ("next_heading", "5.1 Experimental Setup"),
        ]),
        ["ArticleClaim", "MechanisticExplanation"],
        [o["id"] for o in OBJECTS if o["home_package"] == "PKG-INTRO"],
    ),
    build_package(
        "PKG-SETUP", "C-SETUP", DOC_TITLE, "Chapter 5 > 5.1 Experimental Setup",
        OrderedDict([
            ("previous_heading", "5. Long Context Training"),
            ("next_heading", "5.2 Experimental Results"),
        ]),
        ["ExperimentSetup"],
        [o["id"] for o in OBJECTS if o["home_package"] == "PKG-SETUP"],
    ),
    build_package(
        "PKG-RESULT", "C-RESULT", DOC_TITLE, "Chapter 5 > 5.2 Experimental Results",
        OrderedDict([
            ("table_caption", "Table 2 | Long-context performance comparison"),
            ("table_headers", "Model | LongPPL(32k){Book,Paper,Code,L-CoT} | RULER(32k){NIAH:S,MK,MV,MQ ; Other:VT,CWE,FWE,QA}"),
            ("previous_heading", "5.1 Experimental Setup"),
            ("next_heading", "6. Analysis"),
        ]),
        ["ExperimentResult", "ArticleClaim"],
        [o["id"] for o in OBJECTS if o["home_package"] == "PKG-RESULT"],
        # CLAIM-FREE-ATTENTION is a cross-package object (home=PKG-INTRO) whose
        # supporting evidence (the Iso-Loss row/sentence) lives in this package;
        # from PKG-RESULT atoms alone only its scope-of-support is derivable.
        expected_local_fields=OrderedDict([
            ("CLAIM-FREE-ATTENTION", ["scope"]),
        ]),
    ),
]

# ---------------------------------------------------------------------------
# mentions (IDs preserved; atom_id rebound to verbatim atoms).
# ---------------------------------------------------------------------------
MENTIONS = [
    OrderedDict([("id", "M-01"), ("text", "YaRN"), ("type", "ExperimentSetup"), ("atom_id", "A-SETUP-YARN"), ("canonical_key", "yarn_context_extension")]),
    OrderedDict([("id", "M-02"), ("text", "Iso-Loss"), ("type", "ExperimentSetup"), ("atom_id", "A-SETUP-ISOLOSS"), ("canonical_key", "iso_loss_setting")]),
    OrderedDict([("id", "M-03"), ("text", "LongPPL"), ("type", "ExperimentSetup"), ("atom_id", "A-SETUP-LONGPPL"), ("canonical_key", "longppl_benchmark")]),
    OrderedDict([("id", "M-04"), ("text", "RULER"), ("type", "ExperimentSetup"), ("atom_id", "A-SETUP-RULER"), ("canonical_key", "ruler_benchmark")]),
    OrderedDict([("id", "M-05"), ("text", "Multi-Query NIAH"), ("type", "ExperimentResult"), ("atom_id", "A-RES-ISOLOSS"), ("canonical_key", "mq_niah")]),
    OrderedDict([("id", "M-06"), ("text", "VT"), ("type", "ExperimentResult"), ("atom_id", "A-RES-ISOLOSS"), ("canonical_key", "ruler_vt")]),
    OrderedDict([("id", "M-07"), ("text", "Engram-27B (46k, 1.63)"), ("type", "ExperimentResult"), ("atom_id", "A-T2-ROW-ENG46K"), ("canonical_key", "engram_27b_46k")]),
    OrderedDict([("id", "M-08"), ("text", "Engram-27B (50k, 1.62)"), ("type", "ExperimentResult"), ("atom_id", "A-T2-ROW-ENG50K"), ("canonical_key", "engram_27b_50k")]),
    OrderedDict([("id", "M-09"), ("text", "MoE-27B (50k, 1.63)"), ("type", "ExperimentResult"), ("atom_id", "A-T2-ROW-MOE50K"), ("canonical_key", "moe_27b_50k")]),
    OrderedDict([("id", "M-10"), ("text", "attention capacity"), ("type", "MechanisticExplanation"), ("atom_id", "A-INTRO-MECH"), ("canonical_key", "freed_attention_capacity")]),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "iso_loss_setting"),
                 ("aliases", ["Iso-Loss", "iso-pretraining-loss", "loss-aligned", "Engram-27B (46k)"]),
                 ("note", "46k checkpoint chosen so pre-training loss equals MoE-27B(50k).")]),
    OrderedDict([("canonical", "iso_flops_setting"),
                 ("aliases", ["iso-pretraining-FLOPs", "iso-compute", "iso-compute budget", "Engram-27B (50k)"])]),
    OrderedDict([("canonical", "mq_niah"),
                 ("aliases", ["Multi-Query NIAH", "MQ", "Multi-queries Needle-in-a-Haystack"])]),
    OrderedDict([("canonical", "ruler_vt"),
                 ("aliases", ["VT", "Multi-hop Variable Tracking"])]),
    OrderedDict([("canonical", "longppl_benchmark"),
                 ("aliases", ["LongPPL", "LongPPL (32k)"])]),
]

# ---------------------------------------------------------------------------
# relations (IDs preserved; evidence atom_ids rebound to verbatim atoms).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


RELATIONS = [
    rel("R-01", "experiment_tests_claim", "SETUP-LONGCTX", "CLAIM-FREE-ATTENTION",
        ["A-SETUP-CONFIGS", "A-SETUP-ISOLOSS"], "medium", "direct"),
    rel("R-02", "result_supports_claim", "RESULT-TABLE2", "CLAIM-FREE-ATTENTION",
        ["A-T2-ROW-ENG46K", "A-T2-ROW-MOE50K", "A-RES-ISOLOSS"], "medium", "direct"),
    rel("R-03", "result_supports_claim", "RESULT-TABLE2", "CLAIM-ALIGN-LOSS",
        ["A-T2-ROW-ENG41K", "A-T2-ROW-ENG50K", "A-RES-COUPLING"], "medium", "indirect"),
    rel("R-04", "mechanism_explains_result", "MECH-LOOKUP-DELEGATION", "RESULT-TABLE2",
        ["A-INTRO-MECH", "A-RES-ISOLOSS"], "hard", "indirect"),
    rel("R-05", "mechanism_explains_claim", "MECH-LOOKUP-DELEGATION", "CLAIM-FREE-ATTENTION",
        ["A-INTRO-MECH", "A-INTRO-CLAIM"], "medium", "direct"),
    rel("R-06", "result_reported_in_setup", "RESULT-TABLE2", "SETUP-LONGCTX",
        ["A-T2-CAPTION", "A-SETUP-RULER"], "medium", "direct"),
]

# ---------------------------------------------------------------------------
# do_not_extract: inline author-year citations in slice, citation policy,
# figure reference (Figure 4 caption just outside the result prose).
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([("text", "(Peng et al., 2024)"), ("atom_id", "A-SETUP-YARN"),
                 ("reason", "inline author-year citation, not a knowledge object."), ("kind", "citation")]),
    OrderedDict([("text", "(Fang et al.)"), ("atom_id", "A-SETUP-LONGPPL"),
                 ("reason", "inline citation for the LongPPL benchmark; the benchmark is the object, not the citation."), ("kind", "citation")]),
    OrderedDict([("text", "(Press et al., 2022; Su et al., 2024; Xiao et al., 2024; Yang et al., 2025)"),
                 ("source_lines", [214, 214]),
                 ("reason", "inline citation cluster in the result prose."), ("kind", "citation")]),
    OrderedDict([("pattern", "inline_author_year_citation"),
                 ("examples", ["(Peng et al., 2024)", "(Fang et al.)", "(Hsieh et al.)", "(Liu et al., 2024a)", "(Gao et al., 2025)"]),
                 ("reason", "inline author-year citations are not knowledge objects; suppress citation over-extraction consistently."),
                 ("kind", "citation_policy")]),
    OrderedDict([("text", "Figure 4 | Analysis of representational alignment and convergence speed."),
                 ("source_lines", [222, 222]),
                 ("reason", "Figure 4 belongs to Section 6 Analysis (physically just after the 5.2 prose); a forward figure reference, not a Section 5 result object."),
                 ("kind", "cross_section_forward_reference")]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "engram"),
    ("scope", "Chapter 5 (Long Context Training) only"),
    ("title", "Engram - Long Context Training (Chapter 5)"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 190-221; NOT the authoritative source)"),
    ("profile", "article_research"),
    ("profile_detected_as", "article_research"),
    ("profile_cues", ["Experimental Setup", "Experimental Results", "Table 2", "RULER", "LongPPL",
                      "we demonstrate", "our results indicate", "iso-pretraining-loss"]),
    ("extraction_targets", ["ArticleClaim", "ExperimentSetup", "ExperimentResult", "MechanisticExplanation"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=engram_paper_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span: it may rephrase, transliterate math (alpha/beta, arrow ->, em-dash --), and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs/numbers not inside the span. EXCEPTION: a formula_atom MAY carry a symbolic interpretation supplied by atoms in metadata.supported_by_context_atoms / interpretation_supported_by."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside the object's home_package's chunk) and supporting_context_atom_ids (atoms from OTHER chunks needed to fully define it). The object's full evidence is the union. Package object-recall (expected_objects) is scored against local_evidence only; supporting_context atoms are NOT required to be present in the home package's input."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = the payload fields derivable from that package's atoms alone. CLAIM-FREE-ATTENTION is defined in PKG-INTRO but its supporting Iso-Loss evidence sits in PKG-RESULT, which therefore declares its locally derivable field (scope)."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of decimal confidence."),
        ("figures", "figure atoms separate physical_section_id from semantic_section_ids; forward references are isolated as do_not_extract cross_section_forward_reference. Chapter 5's slice contains no figure of its own; Figure 4 (line 222) belongs to Section 6."),
        ("external_evidence", "atoms whose claim is only asserted here but proven elsewhere set requires_external_evidence:true + external_evidence_ref. Chapter 5 has none."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects lists the object ids that input should yield."),
        ("context_only", "an atom not consumed by any object or relation may be flagged metadata.context_only:true; Chapter 5 has no such atoms (every atom feeds an object or relation)."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
        ("numeric_over_span", "for every table_row_atom: each number appearing in normalized_text must occur verbatim in raw_text (no over-span numeric leakage across rows)."),
    ])),
    ("parsing_notes", [
        "Table 2 renders as a single-line HTML <table> on source line 192 with a NESTED 3-row header: row1 Model(rowspan=3) + LongPPL (32k)(colspan=4) + RULER (32k)(colspan=8); row2 Perplexity(colspan=4)/NIAH Accuracy(colspan=4)/Other Tasks(colspan=4); row3 detail columns Book/Paper/Code/L-CoT, S/MK/MV/MQ, VT/CWE/FWE/QA. A-T2-HEADER's raw_text is the anchored substring covering exactly these three header <tr> rows.",
        "Each table_row_atom raw_text is the anchored <tr>...</tr> for one model row; the 12 numbers in its normalized_text are exactly the 12 <td> cells of that row.",
        "Table 2 is physically rendered before the Section 5 prose (caption line 190, table line 192, immediately after Chapter 4), but its caption, the (steps, loss) annotations and all interpretation belong to Chapter 5, so it is kept in this chapter.",
        "checkpoint annotations (steps, loss): MoE-27B(50k,1.63), Engram-27B(41k,1.66)/(46k,1.63)/(50k,1.62); the parenthetical loss is the pre-training loss before long-context extension.",
        "YaRN hyper-parameters render as inline math '$s = 10, \\alpha = 1, \\beta = 32$' plus 'f = 0.707'; normalized to ASCII 's = 10, alpha = 1, beta = 32, f = 0.707'.",
        "Section 5.1 paragraphs start with run-in bold sub-headings 'Training Details.', 'Model Configurations.', 'Evaluation Benchmarks.'; these are separate MinerU paragraph elements (lines 204/206/208) but one semantic setup chunk.",
        "Section 5.2 finding 1 (line 214) and the three result bullets (lines 218-220) carry a Unicode arrow (41k -> 50k) and em-dashes; normalized to ASCII.",
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
        "# Gold fixture v0.3.3 - Engram paper Chapter 5 Long Context Training (profile: article_research)\n"
        "# Upgraded from v0.1: dual coordinates (source_span authoritative + viewer_span),\n"
        "# raw/normalized discipline with verbatim spans, Table 2 nested header + per-row anchored\n"
        "# table_row_atoms, object evidence split into local/supporting + home_package, per-chunk\n"
        "# context_packages with expected_objects (+expected_local_fields), gold_label/difficulty/\n"
        "# evidence_strength (no decimal confidence), and do_not_extract negatives.\n"
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
