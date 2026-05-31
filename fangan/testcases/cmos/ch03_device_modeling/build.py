#!/usr/bin/env python3
"""Builder for ch03_device_modeling gold.yaml (schema v0.3.3-textbook, profile textbook).

Regenerates source.md (viewer-only verbatim slice of the MinerU source lines
3189-4872) and gold.yaml from the authoritative MinerU source file. All spans are
computed by locating verbatim text in the source; YAML spans are never hand-written.

Span helpers (textbook ch02 template):
  by_tag(N, win_start, win_end)   -> the formula line carrying \\tag {N} WITHIN a
                                     sub-section line window (tags reset per sub-section).
  by_anchor(raw, win_start, win_end) -> the unique line in a window containing the
                                     verbatim substring raw.
  by_line(raw, line_no)           -> verbatim substring on an exact line.

Upgrades the v0.1 fixture to v0.3.3-textbook: keeps every atom/chunk/object/relation
ID and its meaning, re-expresses each atom as a dual-coordinate verbatim span
(source_span authoritative + viewer_span viewer-only) with raw_text + within-span
normalized_text, splits object evidence into local/supporting + home_package, adds
one context_package per chunk with expected_objects (+expected_local_fields for
cross-package objects), gold_label/difficulty/evidence_strength (no decimal
confidence), and do_not_extract negatives.

Run:  python3 build.py
"""
import os
import re
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = "/Users/hzf/workspace/pdf_parser/notebook_papers_mineru_skill_results/CMOS_Analog_Circuit_Design_-_Allen_Holberg/CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md"
SOURCE_NAME = "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md"
VIEWER_NAME = "source.md"

SLICE_LINE_START = 3189
SLICE_LINE_END = 4872

SRC = open(SOURCE_FILE, encoding="utf-8").read()
SRC_LINES = SRC.split("\n")

_LINE_OFF = {}
_acc = 0
for _i, _ln in enumerate(SRC_LINES, start=1):
    _LINE_OFF[_i] = _acc
    _acc += len(_ln) + 1

# ---------------------------------------------------------------------------
# Viewer file source.md : verbatim slice + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY

_VIEWER_LINE_OFF = {}
_VIEWER_LINE_NO = {}
_voff = len(VIEWER_HEADER)
for _k, _srcln in enumerate(range(SLICE_LINE_START, SLICE_LINE_END + 1)):
    _VIEWER_LINE_OFF[_srcln] = _voff
    _VIEWER_LINE_NO[_srcln] = 2 + _k
    _voff += len(SRC_LINES[_srcln - 1]) + 1


def _spans_from(line_no, idx, raw):
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


def by_line(raw, line_no):
    """raw is a verbatim substring of source line `line_no` (unique on that line)."""
    line_text = SRC_LINES[line_no - 1]
    idx = line_text.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND on line %d: %r" % (line_no, raw[:70]))
    if line_text.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS on line %d: %r" % (line_no, raw[:70]))
    return _spans_from(line_no, idx, raw)


def by_anchor(raw, win_start, win_end):
    """Find the unique line in [win_start, win_end] whose text contains raw."""
    hits = []
    for ln in range(win_start, win_end + 1):
        if raw in SRC_LINES[ln - 1]:
            hits.append(ln)
    if not hits:
        raise SystemExit("ANCHOR NOT FOUND in [%d,%d]: %r" % (win_start, win_end, raw[:70]))
    if len(hits) > 1:
        raise SystemExit("ANCHOR AMBIGUOUS in [%d,%d] lines %s: %r" % (win_start, win_end, hits, raw[:70]))
    ln = hits[0]
    idx = SRC_LINES[ln - 1].find(raw)
    return _spans_from(ln, idx, raw)


def line_between(line_no, start_marker, end_marker, include_end=True):
    """Return the verbatim substring of `line_no` from start_marker to end_marker."""
    t = SRC_LINES[line_no - 1]
    i = t.find(start_marker)
    if i < 0:
        raise SystemExit("start_marker not on line %d: %r" % (line_no, start_marker[:50]))
    j = t.find(end_marker, i + len(start_marker))
    if j < 0:
        raise SystemExit("end_marker not on line %d: %r" % (line_no, end_marker[:50]))
    end = j + len(end_marker) if include_end else j
    return t[i:end]


_TAG_RE = re.compile(r"\\tag \{([^}]+)\}")


def by_tag(tagval, win_start, win_end):
    """Find the formula line carrying \\tag {tagval} within the sub-section window.

    Returns (source_span, viewer_span, raw) where raw is the FULL formula line.
    tags reset per sub-section, so the window pins the right occurrence.
    """
    hits = []
    for ln in range(win_start, win_end + 1):
        m = _TAG_RE.search(SRC_LINES[ln - 1])
        if m and m.group(1) == str(tagval):
            hits.append(ln)
    if not hits:
        raise SystemExit("TAG %s NOT FOUND in [%d,%d]" % (tagval, win_start, win_end))
    if len(hits) > 1:
        raise SystemExit("TAG %s AMBIGUOUS in [%d,%d] lines %s" % (tagval, win_start, win_end, hits))
    ln = hits[0]
    raw = SRC_LINES[ln - 1]
    ss, vs = _spans_from(ln, 0, raw)
    return ss, vs, raw


ATOMS = []


def add_formula(aid, section_id, tagval, win, normalized, se_id, role, evidence_strength="direct",
                extra_meta=None):
    ss, vs, raw = by_tag(tagval, win[0], win[1])
    md = OrderedDict([("tag", str(tagval))])
    if role:
        md["role"] = role
    if extra_meta:
        md.update(extra_meta)
    d = OrderedDict([
        ("id", aid), ("section_id", section_id), ("atom_type", "formula_atom"),
        ("source_element_id", se_id), ("source_span", ss), ("viewer_span", vs),
        ("raw_text", raw), ("normalized_text", normalized),
        ("evidence_strength", evidence_strength), ("metadata", md),
    ])
    ATOMS.append(d)
    return d


def add_anchor(aid, section_id, atom_type, raw, win, normalized, se_id, evidence_strength="direct",
               metadata=None):
    ss, vs = by_anchor(raw, win[0], win[1])
    d = OrderedDict([
        ("id", aid), ("section_id", section_id), ("atom_type", atom_type),
        ("source_element_id", se_id), ("source_span", ss), ("viewer_span", vs),
        ("raw_text", raw), ("normalized_text", normalized),
        ("evidence_strength", evidence_strength),
    ])
    if metadata:
        d["metadata"] = metadata
    ATOMS.append(d)
    return d


def add_line(aid, section_id, atom_type, raw, line_no, normalized, se_id, evidence_strength="direct",
             metadata=None):
    ss, vs = by_line(raw, line_no)
    d = OrderedDict([
        ("id", aid), ("section_id", section_id), ("atom_type", atom_type),
        ("source_element_id", se_id), ("source_span", ss), ("viewer_span", vs),
        ("raw_text", raw), ("normalized_text", normalized),
        ("evidence_strength", evidence_strength),
    ])
    if metadata:
        d["metadata"] = metadata
    ATOMS.append(d)
    return d


# ---------------------------------------------------------------------------
# Sub-section line windows (tags reset per window).
# ---------------------------------------------------------------------------
W_INTRO = (3189, 3204)
W_31 = (3205, 3408)      # 3.1 Simple MOS Large-Signal
W_EX311 = (3409, 3448)   # Example 3.1-1
W_32 = (3449, 3862)      # 3.2 Other Large-Signal Params
W_33 = (3863, 3954)      # 3.3 Small-Signal (Eqs 1-9)
W_EX331 = (3955, 3966)   # Example 3.3-1 + Table 3.3-1
W_33B = (3967, 3990)     # 3.3 nonsat g_m eqs 10/11/12 (between examples)
W_EX332 = (3991, 4018)   # Example 3.3-2
W_34 = (4019, 4026)      # 3.4 intro
W_L3 = (4027, 4150)      # SPICE Level 3 drain/threshold/sat/mob/clm (tags 1..22)
W_L3T = (4151, 4208)     # Level 3 temperature block (Table 3.4-1 + tags 15..24)
W_BSIM = (4209, 4230)    # BSIM3v3
W_35 = (4231, 4337)      # 3.5 Subthreshold
W_36 = (4338, 4452)      # 3.6 prose (netlist/multiplier)
W_EX361 = (4453, 4574)   # Example 3.6-1
W_EX363 = (4575, 4634)   # Example 3.6-3
W_EX364 = (4635, 4693)   # Example 3.6-4
W_SUMMARY = (4694, 4699)
W_PROB = (4700, 4838)

# Source element ids (one per logical MinerU element we anchor into).
SE = {}


def se(seid, ln_start, ln_end, etype):
    SE[seid] = (etype, ln_start, ln_end)
    return seid


# ===========================================================================
# 3.0 chapter intro + summary (context_only)
# ===========================================================================
add_anchor(
    "A-INTRO", "SEC-3", "concept_definition_atom",
    "This text will concentrate only three of these models. The simplest model which is appropriate for hand calculations was described in Sec 2.3 and will be further developed here to include capacitance, noise, and ohmic resistance. In SPICE terminology, this simple model is called the LEVEL 1 model. Next, a small-signal model is derived from the LEVEL 1 large-signal model and is presented in Sec. 3.3.",
    (3195, 3195),
    "This text concentrates on three SPICE models. The simplest model, appropriate for hand calculations (described in Sec 2.3 and developed here to include capacitance, noise, and ohmic resistance), is in SPICE terminology the LEVEL 1 model. A small-signal model is then derived from the LEVEL 1 large-signal model in Sec. 3.3.",
    se("SE-3-INTRO", 3195, 3195, "paragraph"),
    metadata=OrderedDict([("context_only", True)]),
)
add_anchor(
    "A-SUMMARY", "SEC-3.7", "summary_atom",
    "This program normally has three levels of MOS models which are available to the user. The function of models is to solve for the dc operating conditions and then use this information to develop a linear small-signal model. Sec. 3.1 described the LEVEL 1 model used by SPICE to solve for the dc operating point.",
    (4694, 4699),
    "SPICE has three levels of MOS models. Models solve for the dc operating conditions and then build a linear small-signal model. Sec. 3.1 described the LEVEL 1 model used to solve for the dc operating point.",
    se("SE-3.7-SUM", 4696, 4696, "paragraph"),
    metadata=OrderedDict([("context_only", True)]),
)

# ===========================================================================
# 3.1 Simple large-signal model
# ===========================================================================
add_formula("A-LS-SAH-1", "SEC-3.1", 1, W_31,
            "i_D = (mu_o C_ox W / L) [ (v_GS - V_T) - (v_DS / 2) ] v_DS",
            se("SE-3.1-EQ1", 3233, 3233, "formula"), "drain_current_physical")
add_formula("A-LS-VT-2", "SEC-3.1", 2, W_31,
            "V_T = V_T0 + gamma [ sqrt(2|phi_F| + v_SB) - sqrt(2|phi_F|) ]",
            se("SE-3.1-EQ2", 3249, 3249, "formula"), "threshold_voltage")
add_formula("A-LS-GAMMA-4", "SEC-3.1", 4, W_31,
            "gamma = bulk threshold parameter (V^1/2) = sqrt(2 eps_si q N_SUB) / C_ox",
            se("SE-3.1-EQ4", 3257, 3257, "formula"), "body_factor")
add_anchor(
    "A-LS-PARAMS", "SEC-3.1", "definition_atom",
    "The terminal voltages and currents have been defined in the previous chapter. The various parameters of (1) are defined as",
    W_31,
    "mu_o = surface mobility of the channel (cm2/V-s); C_ox = eps_ox/t_ox = gate-oxide capacitance per unit area (F/cm2); W = effective channel width; L = effective channel length; phi_F = strong-inversion surface potential.",
    se("SE-3.1-PARAMS", 3236, 3236, "paragraph"),
    metadata=OrderedDict([("supported_by_context_atoms", ["A-LS-SAH-1"])]),
)
add_formula("A-LS-ID-K-12", "SEC-3.1", 12, W_31,
            "i_D = K'(W/L) [ (v_GS - V_T) - v_DS/2 ] v_DS",
            se("SE-3.1-EQ12", 3312, 3312, "formula"), "drain_current_nonsat")
add_formula("A-LS-BETA-13", "SEC-3.1", 13, W_31,
            "beta = (K')(W/L) ~= (mu_o C_ox)(W/L)  (A/V^2)",
            se("SE-3.1-EQ13", 3318, 3318, "formula"), "transconductance_param")
add_line(
    "A-LS-T312", "SEC-3.1", "table_row_atom",
    SRC_LINES[3325 - 1], 3325,
    "Table 3.1-2 simple-model parameters (0.8um Si-Gate Bulk CMOS n-Well) N/P: V_T0 = 0.7/-0.7 V; K' = 110.0/50.0 uA/V^2 (in saturation); gamma = 0.4/0.57 V^1/2; lambda = 0.04/0.05 V^-1 (L=1um); 2|phi_F| = 0.7/0.8 V.",
    se("SE-3.1-T312", 3325, 3325, "table"),
    metadata=OrderedDict([("table_id", "Table 3.1-2")]),
)
add_formula("A-LS-CUTOFF-14", "SEC-3.1", 14, W_31,
            "i_D = 0,  v_GS - V_T <= 0",
            se("SE-3.1-EQ14", 3330, 3330, "formula"), None,
            extra_meta=OrderedDict([("atom_role", "condition")]))
ATOMS[-1]["atom_type"] = "condition_atom"
add_formula("A-LS-VDSAT-15", "SEC-3.1", 15, W_31,
            "v_DS(sat) = v_GS - V_T",
            se("SE-3.1-EQ15", 3338, 3338, "formula"), "saturation_voltage")
add_formula("A-LS-NONSAT-16", "SEC-3.1", 16, W_31,
            "i_D = (K')(W/L) [ (v_GS - V_T) - v_DS/2 ] v_DS;  0 < v_DS <= (v_GS - V_T)",
            se("SE-3.1-EQ16", 3357, 3357, "formula"), "drain_current_nonsat")
add_formula("A-LS-SAT-17", "SEC-3.1", 17, W_31,
            "i_D = K'(W/2L)(v_GS - V_T)^2,  0 < (v_GS - V_T) <= v_DS",
            se("SE-3.1-EQ17", 3365, 3365, "formula"), "drain_current_sat")
add_anchor(
    "A-LS-CLM-EFFECT", "SEC-3.1", "physical_effect_atom",
    "As drain voltage increases, the channel length is reduced resulting in increased current. This phenomenon is called channel length modulation and is accounted for in the saturation model with the addition of the factor, $( 1 + \\lambda \\nu _ { D S } )$ where $\\nu _ { D S }$ is the actual drain-source voltage and not $\\nu _ { D S } ( \\mathrm { s a t } )$ .",
    W_31,
    "As drain voltage increases the channel length is reduced, increasing current. This is channel length modulation, accounted for in the saturation model by the factor (1 + lambda v_DS), where v_DS is the actual drain-source voltage, not v_DS(sat).",
    se("SE-3.1-CLM", 3368, 3368, "paragraph"),
)
add_formula("A-LS-SAT-CLM-18", "SEC-3.1", 18, W_31,
            "i_D = K'(W/2L)(v_GS - V_T)^2 (1 + lambda v_DS),  0 < (v_GS - V_T) <= v_DS",
            se("SE-3.1-EQ18", 3371, 3371, "formula"), "drain_current_sat_clm")
add_anchor(
    "A-LS-FIVE-PARAMS", "SEC-3.1", "definition_atom",
    "This simple model has five electrical and process parameters that completely define it. These parameters are K', $V _ { T } , \\gamma , \\lambda$ , and $2 \\phi _ { F }$ .",
    W_31,
    "The simple (SPICE Level 1) model is completely defined by five electrical/process parameters: K', V_T, gamma, lambda, and 2 phi_F.",
    se("SE-3.1-FIVE", 3405, 3405, "paragraph"),
)
add_formula("A-LS-VON-20", "SEC-3.1", 20, (3205, 3448),
            "V_ON = sqrt(2 i_D / beta)",
            se("SE-3.1-EQ20", 3444, 3444, "formula"), "overdrive_voltage",
            extra_meta=OrderedDict([("related_tags", ["19", "20"]),
                                    ("supported_by_context_atoms", ["A-LS-VDSAT-15"])]))

# Example 3.1-1
add_anchor(
    "A-EX311-PROBLEM", "SEC-EX311", "example_problem_atom",
    "If the drain, gate, source, and bulk voltages of the n-channel transistor is 3 V, 2 V, 0 V, and 0 V, respectively, find the drain current. Repeat for the p-channel transistor if the drain, gate, source, and bulk voltages are",
    W_EX311,
    "For W/L = 5um/1um and Table 3.1-2 parameters, find the n-channel drain current with v_D,v_G,v_S,v_B = 3,2,0,0 V; repeat for the p-channel transistor with -3,-2,0,0 V.",
    se("SE-EX311-PROB", 3411, 3411, "paragraph"),
)
add_anchor(
    "A-EX311-GIVEN", "SEC-EX311", "given_atom",
    "Assume that the transistors in Fig. 3.1-1 have a W/L ratio of 5 m/1 m and that the large signal model parameters are those given in Table 3.1-2.",
    W_EX311,
    "Given: W/L = 5um/1um; large-signal model parameters from Table 3.1-2 (K'_N=110, K'_P=50 uA/V^2, V_T=0.7 V, lambda_N=0.04, lambda_P=0.05 V^-1).",
    se("SE-EX311-GIVEN", 3411, 3411, "paragraph"),
)
add_anchor(
    "A-EX311-USE", "SEC-EX311", "formula_usage_atom",
    "Eq. (15) gives $\\nu _ { D S } ( \\mathrm { s a t } )$ as 2 V − 0.7 V = 1.3 V. Since $\\nu _ { D S }$ is 3 V, the n-channel transistor is in the saturation region. Using Eq. (18) and the values from Table 3.1-2 we have",
    W_EX311,
    "Eq. (15) gives v_DS(sat) = 2 V - 0.7 V = 1.3 V. Since v_DS is 3 V, the n-channel transistor is in saturation; apply Eq. (18) saturation-with-CLM current using Table 3.1-2 values.",
    se("SE-EX311-USE", 3413, 3413, "paragraph"),
)
add_line(
    "A-EX311-RESULT", "SEC-EX311", "result_atom",
    "= \\frac {1 1 0 \\times 1 0 ^ {- 6} (5 \\mu \\mathrm{m})}{2 (1 \\mu \\mathrm{m})} (2 - 0. 7) ^ {2} (1 + 0. 0 4 \\times 3) = 5 2 0 \\mu \\mathrm{A}",
    3420,
    "n-channel: i_D = (110e-6 x 5um)/(2 x 1um) (2 - 0.7)^2 (1 + 0.04 x 3) = 520 uA.",
    se("SE-EX311-RES", 3420, 3420, "formula"),
    metadata=OrderedDict([("note", "p-channel result 243 uA is computed on line 3432 (separate sub-equation block).")]),
)

# ===========================================================================
# 3.2 Other large-signal parameters
# ===========================================================================
add_formula("A-LS2-DIODE-1", "SEC-3.2", 1, W_32,
            "i_BD = I_S [ exp(q v_BD / kT) - 1 ]",
            se("SE-3.2-EQ1", 3466, 3466, "formula"), "bulk_junction_diodes",
            extra_meta=OrderedDict([("related_tags", ["1", "2"])]))
add_anchor(
    "A-LS2-RDRS", "SEC-3.2", "component_model_atom",
    "The resistors $r _ { D }$ and $r _ { S }$ represent the ohmic resistance of the drain and source, respectively. Typically, these resistors may be 50 to 100 ohmsi and can often be ignored at low drain currents.",
    W_32,
    "Resistors r_D and r_S model the ohmic resistance of drain and source (typically 50 to 100 ohms) and can often be ignored at low drain currents.",
    se("SE-3.2-RDRS", 3477, 3477, "paragraph"),
)
add_formula("A-LS2-CBX-3", "SEC-3.2", 3, W_32,
            "C_BX = (CJ)(AX) [ 1 - v_BX/PB ]^(-MJ),  v_BX <= (FC)(PB)",
            se("SE-3.2-EQ3", 3488, 3488, "formula"), "depletion_cap_region1")
add_formula("A-LS2-CBX-4", "SEC-3.2", 4, W_32,
            "C_BX = (CJ)(AX)/(1-FC)^(1+MJ) [ 1 - (1+MJ)FC + MJ v_BX/PB ],  v_BX > (FC)(PB)",
            se("SE-3.2-EQ4", 3524, 3524, "formula"), "depletion_cap_region2")
add_formula("A-LS2-CBX-SW-5", "SEC-3.2", 5, W_32,
            "C_BX = (CJ)(AX)/[1-(v_BX/PB)]^MJ + (CJSW)(PX)/[1-(v_BX/PB)]^MJSW",
            se("SE-3.2-EQ5", 3549, 3549, "formula"), "depletion_cap_sidewall")
add_formula("A-LS2-COVERLAP-7", "SEC-3.2", 7, W_32,
            "C_1 = C_3 ~= (LD)(W_eff) C_ox = (CGXO) W_eff",
            se("SE-3.2-EQ7", 3626, 3626, "formula"), "overlap_capacitance")
add_formula("A-LS2-C2-8", "SEC-3.2", 8, W_32,
            "C_2 = W_eff (L - 2 LD) C_ox = W_eff (L_eff) C_ox",
            se("SE-3.2-EQ8", 3667, 3667, "formula"), "gate_channel_capacitance")
add_anchor(
    "A-LS2-CGREGIONS", "SEC-3.2", "formula_atom",
    "As a consequence of the above considerations, we shall use the following formulas for the charge-storage capacitances of the MOS device in the indicated regions.",
    W_32,
    "Charge-storage caps by region: Off C_GB=C_2+2C_5, C_GS=C_1, C_GD=C_3 (Eq.9); Saturation C_GB=2C_5, C_GS=C_1+(2/3)C_2, C_GD=C_3 (Eq.10); Nonsaturated C_GB=2C_5, C_GS=C_1+0.5C_2, C_GD=C_3+0.5C_2 (Eq.11).",
    se("SE-3.2-CGREG", 3710, 3710, "paragraph"),
    metadata=OrderedDict([("related_tags", ["9a", "9b", "9c", "10a", "10b", "10c", "11a", "11b", "11c"]),
                          ("note", "Eqs 9/10/11 render as multi-line sub-equations (lines 3714-3764); this atom anchors the prose that introduces the per-region charge-storage capacitances; formula content is in the object payload.")]),
)
add_line(
    "A-LS2-T321", "SEC-3.2", "table_row_atom",
    SRC_LINES[3619 - 1], 3619,
    "Table 3.2-1 capacitance values/coefficients (t_ox=140A) P/N: CGSO 220e-12/220e-12 F/m; CGDO 220e-12/220e-12 F/m; CGBO 700e-12/700e-12 F/m; CJ 770e-6/560e-6 F/m2; CJSW 350e-12/380e-12 F/m; MJ 0.5/0.5; MJSW 0.35/0.38.",
    se("SE-3.2-T321", 3619, 3619, "table"),
    metadata=OrderedDict([("table_id", "Table 3.2-1")]),
)
add_formula("A-LS2-NOISE-12", "SEC-3.2", 12, W_32,
            "i_N^2 = [ 8 kT g_m (1+eta)/3 + (KF) I_D/(f C_ox L^2) ] df",
            se("SE-3.2-EQ12N", 3773, 3773, "formula"), "drain_current_noise")

# ===========================================================================
# 3.3 Small-signal model
# ===========================================================================
add_anchor(
    "A-SS-INTRO", "SEC-3.3", "concept_definition_atom",
    "The small-signal model is a linear model which helps to simplify calculations. It is only valid over voltage or current regions where the large-signal voltage and currents can be adequately represented by a straight line.",
    W_33,
    "The small-signal model is a linear model valid over regions where the large-signal variables can be represented by a straight line; its parameters are partial derivatives of one large-signal variable with respect to another at the quiescent point.",
    se("SE-3.3-INTRO", 3865, 3865, "paragraph"),
)
add_formula("A-SS-GBDGBS-1", "SEC-3.3", 1, W_33,
            "g_bd = dI_BD/dV_BD ~= 0",
            se("SE-3.3-EQ1", 3872, 3872, "formula"), "bulk_conductances",
            extra_meta=OrderedDict([("related_tags", ["1", "2"])]))
add_formula("A-SS-DEFS-3", "SEC-3.3", 3, W_33,
            "g_m = dI_D/dV_GS",
            se("SE-3.3-EQ3", 3884, 3884, "formula"), "channel_conductance_defs",
            extra_meta=OrderedDict([("related_tags", ["3", "4", "5"])]))
add_formula("A-SS-GM-6", "SEC-3.3", 6, W_33,
            "g_m = sqrt((2 K' W/L) |I_D| (1 + lambda V_DS)) ~= sqrt((2 K' W/L) |I_D|)",
            se("SE-3.3-EQ6", 3928, 3928, "formula"), "gm_saturation")
add_formula("A-SS-GMBS-8", "SEC-3.3", 8, W_33,
            "g_mbs = g_m gamma / (2 (2|phi_F| + V_SB)^1/2) = eta g_m",
            se("SE-3.3-EQ8", 3940, 3940, "formula"), "gmbs")
add_formula("A-SS-GDS-9", "SEC-3.3", 9, W_33,
            "g_ds = g_o = I_D lambda / (1 + lambda V_DS) ~= I_D lambda",
            se("SE-3.3-EQ9", 3948, 3948, "formula"), "gds")
add_line(
    "A-SS-T331", "SEC-3.3", "table_row_atom",
    SRC_LINES[3963 - 1], 3963,
    "Table 3.3-1 saturation small-signal relations: g_m ~= (2 K' I_D W/L)^1/2 or (K'W/L)(V_GS-V_T); g_mbs = gamma(2 I_D beta)^1/2 / (2(2|phi_F|+V_SB)^1/2); g_ds ~= lambda I_D.",
    se("SE-3.3-T331", 3963, 3963, "table"),
    metadata=OrderedDict([("table_id", "Table 3.3-1")]),
)
# Example 3.3-1
add_anchor(
    "A-EX331-PROBLEM", "SEC-EX331", "example_problem_atom",
    "Find the values of $g _ { m } , g _ { m b s } ,$ and $g _ { d s }$ using the large signal model parameters in Table 3.1-2 for both an n-channel and p-channel device if the dc value of the magnitude of the drain current is 50 A and the magnitude of the dc value of the source-bulk voltage is 2 V. Assume that the W/L ratio is 1 m/1 m.",
    W_EX331,
    "Find g_m, g_mbs, g_ds (Table 3.1-2 parameters) for n- and p-channel devices if |I_D| = 50 uA, |V_SB| = 2 V, W/L = 1um/1um.",
    se("SE-EX331-PROB", 3957, 3957, "paragraph"),
)
add_anchor(
    "A-EX331-USE", "SEC-EX331", "formula_usage_atom",
    "Using the values of Table 3.1-2 and Eqs. (6), (8), and (9) gives",
    W_EX331,
    "Apply saturation small-signal Eqs.(6),(8),(9) with Table 3.1-2 values.",
    se("SE-EX331-USE", 3959, 3959, "paragraph"),
)
add_anchor(
    "A-EX331-RESULT", "SEC-EX331", "result_atom",
    line_between(3959, "$g _ { m } = 1 0 5", "for the p-channel device."),
    W_EX331,
    "n-channel: g_m = 105 uA/V, g_mbs = 12.8 uA/V, g_ds ~= 2.0 uA/V. p-channel: g_m = 70.7 uA/V, g_mbs = 12.0 uA/V, g_ds ~= 2.5 uA/V.",
    se("SE-EX331-RES", 3959, 3959, "paragraph"),
)
# Example 3.3-2
add_anchor(
    "A-EX332-PROBLEM", "SEC-EX332", "example_problem_atom",
    "Find the values of the small-signal model parameters in the nonsaturation region for an n-channel and p-channel transistor if $V _ { G S } = 5 \\mathrm { ~ V } , V _ { D S } = 1 \\mathrm { ~ V } ,$ and $| V _ { B S } | = 2 \\mathrm { V } .$ . Assume that the W/L ratios of both transistors is 1 m/1 m.",
    W_EX332,
    "Find nonsaturation-region small-signal parameters for n- and p-channel transistors if V_GS=5 V, V_DS=1 V, |V_BS|=2 V, W/L=1um/1um (K' same as saturation).",
    se("SE-EX332-PROB", 3993, 3993, "paragraph"),
)
add_anchor(
    "A-EX332-RESULT", "SEC-EX332", "result_atom",
    line_between(3995, "we get $g _ { m } = 1 1 0", "for the pchannel transistor."),
    W_EX332,
    "n-channel: g_m = 110 uA/V, g_mbs = 46.6 uA/V, r_ds = 3.05 KOhm. p-channel: g_m = 50 uA/V, g_mbs = 28.6 uA/V, r_ds = 6.99 KOhm.",
    se("SE-EX332-RES", 3995, 3995, "paragraph"),
)

# ===========================================================================
# 3.4 Computer simulation models
# ===========================================================================
add_anchor(
    "A-CS-INTRO", "SEC-3.4", "concept_definition_atom",
    "The SPICE Level 3 dc model will be covered in some detail because it is a relatively straightforward extension of the Level 2 model. The BSIM3v3 model will be introduced but the detailed equations will not be presented because of the volume of equations required to describe it",
    W_34,
    "The SPICE Level 3 dc model is covered in detail (a straightforward extension of the Level 2 model); BSIM3v3 is introduced without detailed equations because of their volume. The simple large-signal model neglects second-order effects of narrow/short channels; many simulators offer model binning across W/L geometries.",
    se("SE-3.4-INTRO", 4023, 4023, "paragraph"),
    metadata=OrderedDict([("supported_by_context_atoms", [])]),
)
add_formula("A-CS-L3-DRAIN-1", "SEC-3.4-L3", 1, W_L3,
            "i_DS = BETA [ v_GS - V_T - ((1+f_b)/2) v_DE ] . v_DE",
            se("SE-3.4-L3-EQ1", 4036, 4036, "formula"), "level3_drain_current",
            extra_meta=OrderedDict([("related_tags", ["1", "2", "5", "6"])]))
add_anchor(
    "A-CS-L3-PHI-NOTE", "SEC-3.4-L3", "definition_atom",
    "the bulk threshold parameter, GAMMA ( ) in $\\mathrm { v o l t } ^ { 1 / 2 }$ , the surface potential at strong inversion, PHI (2 f), in volts",
    W_36,
    "PHI is the SPICE model term for 2 phi_f (always positive in SPICE regardless of transistor type); KP, GAMMA, COX, U0, NSUB are the SPICE uppercase names for K', gamma, C_ox, mu_o, N_SUB.",
    se("SE-3.4-L3-PHI", 4417, 4417, "paragraph"),
    metadata=OrderedDict([("section_note", "definition_text appears in the 3.6 SPICE-parameter prose; conceptually defines the Level 3 / SPICE naming.")]),
)
add_formula("A-CS-L3-VT-12", "SEC-3.4-L3", 12, W_L3,
            "V_T = V_bi - (ETA . 8.15e-22/(C_ox L_eff^3)) v_DS + GAMMA f_s (PHI+v_SB)^1/2 + f_n (PHI+v_SB)",
            se("SE-3.4-L3-EQ12", 4088, 4088, "formula"), "level3_threshold")
add_formula("A-CS-L3-VSAT-16", "SEC-3.4-L3", 16, W_L3,
            "v_DS(sat) = v_sat + v_C - (v_sat^2 + v_C^2)^1/2",
            se("SE-3.4-L3-EQ16", 4108, 4108, "formula"), "level3_saturation_voltage",
            extra_meta=OrderedDict([("related_tags", ["15", "16"])]))
add_formula("A-CS-L3-MOB-18", "SEC-3.4-L3", 18, W_L3,
            "mu_s = U0/(1 + THETA(v_GS - V_T)) when VMAX=0; mu_eff = mu_s/(1 + v_DE/v_C) when VMAX>0",
            se("SE-3.4-L3-EQ18", 4120, 4120, "formula"), "level3_effective_mobility",
            extra_meta=OrderedDict([("related_tags", ["18", "19"])]))
add_formula("A-CS-L3-CLM-20", "SEC-3.4-L3", 20, W_L3,
            "deltaL = xd [ KAPPA (v_DS - v_DS(sat)) ]^1/2 (VMAX=0); i_DS = i_DS/(1 - deltaL)",
            se("SE-3.4-L3-EQ20", 4132, 4132, "formula"), "level3_channel_length_modulation",
            extra_meta=OrderedDict([("related_tags", ["20", "21"])]))
add_formula("A-CS-L3-MOBT-15", "SEC-3.4-L3", 15, W_L3T,
            "UO(T) = UO(T0)(T/T0)^BEX",
            se("SE-3.4-L3-EQ15T", 4158, 4158, "formula"), "mobility_temperature")
add_line(
    "A-CS-L3-T341", "SEC-3.4-L3", "table_row_atom",
    SRC_LINES[4153 - 1], 4153,
    "Table 3.4-1 Level-3 parameters (0.8um Si-Gate Bulk CMOS n-Well) N/P: VTO 0.7/-0.7 V; U0 660/210 cm2/V-s; DELTA 2.4/1.25; ETA 0.1/0.1; KAPPA 0.15/2.5 1/V; THETA 0.1/0.1 1/V; NSUB 3e16/6e16 cm-3; TOX 140 A; XJ 0.2 um; LD 0.016/0.015 um; NFS 7e11/6e11 cm-2.",
    se("SE-3.4-L3-T341", 4153, 4153, "table"),
    metadata=OrderedDict([("table_id", "Table 3.4-1")]),
)
add_anchor(
    "A-CS-BSIM-HISTORY", "SEC-3.4-BSIM", "component_model_atom",
    "In 1994, U.C. Berkeley introduced the BSIM3 model (version 2) which, unlike the earlier BSIM models, returned to a more device-physic based modeling approach. The model is simpler to use and only has 40 dc parameters. Moreover, the BSIM3 provides good performance when applied to analog as well as digital circuit simulation. In its third version, BSIM3v3 [26], it has become the industry standard MOS transistor model.",
    W_BSIM,
    "BSIM model lineage (UC Berkeley): BSIM1 (1984, 60 dc params, curve-fit), BSIM2 (1991, 99 dc params, better output-resistance/hot-electron modeling), BSIM3 v2 (1994, device-physics based, 40 dc params, analog+digital). BSIM3v3 has become the industry-standard MOS transistor model; detailed equations are not given due to their volume.",
    se("SE-3.4-BSIM-HIST", 4213, 4213, "paragraph"),
)
add_anchor(
    "A-CS-BSIM-EFFECTS", "SEC-3.4-BSIM", "physical_effect_atom",
    "The BSIM3 model addresses the following important effects seen in deep-submicron MOSFET operation:",
    W_BSIM,
    "BSIM3 addresses deep-submicron effects: threshold voltage reduction, mobility degradation from a vertical field, velocity saturation, drain-induced barrier lowering (DIBL), channel length modulation, subthreshold (weak inversion) conduction, source/drain parasitic resistance, and hot-electron effects on output resistance.",
    se("SE-3.4-BSIM-EFF", 4215, 4215, "paragraph"),
)
add_anchor(
    "A-CS-MODEL-COMPARE", "SEC-3.4-BSIM", "result_atom",
    "Assuming that the BSIM3v3 model closely approximates actual transistor performance, this figure indicates that the Level 1 model is grossly in error, the Level 3 model shows a significant difference in modeling the transition from non-saturation to linear region.",
    W_BSIM,
    "Fig.3.4-2 compares a 20/0.8 device using Level 1, Level 3, and BSIM3v3. Assuming BSIM3v3 approximates actual behavior, the Level 1 model is grossly in error and the Level 3 model differs significantly in the nonsaturation-to-linear transition.",
    se("SE-3.4-BSIM-CMP", 4226, 4226, "paragraph"),
)

# ===========================================================================
# 3.5 Subthreshold model
# ===========================================================================
add_anchor(
    "A-ST-CONCEPT", "SEC-3.5", "physical_effect_atom",
    "As vGS approaches $V _ { T }$ the $i _ { D } - \\nu _ { G S }$ characteristics change from square-law to exponential. Whereas the region where $\\nu _ { G S }$ is above the threshold is called the strong inversion region, the region below (actually, the transition between the two regions is not well defined as will be explained later) is called the subthreshold, or weak inversion region.",
    W_35,
    "Subthreshold (weak inversion) conduction: as v_GS approaches V_T the i_D - v_GS characteristic changes from square-law (strong inversion) to exponential (weak inversion). Current flows at or below threshold, contradicting the strong-inversion models.",
    se("SE-3.5-CONCEPT", 4233, 4233, "paragraph"),
)
add_formula("A-ST-VON-1", "SEC-3.5", 1, W_35,
            "v_on = V_T + fast",
            se("SE-3.5-EQ1", 4268, 4268, "formula"), "weak_inversion_transition",
            extra_meta=OrderedDict([("related_tags", ["1", "2"])]))
add_formula("A-ST-IDS-3", "SEC-3.5", 3, W_35,
            "i_DS = i_DS(v_on, v_DE, v_SB) exp((v_GS - v_on)/fast)",
            se("SE-3.5-EQ3", 4280, 4280, "formula"), "weak_inversion_current_spice")
add_formula("A-ST-IDS-HAND-5", "SEC-3.5", 5, W_35,
            "i_D ~= (W/L) I_DO exp(v_GS / (n (kT/q)))",
            se("SE-3.5-EQ5", 4292, 4292, "formula"), "weak_inversion_current_hand")
add_formula("A-ST-ENTRY-6", "SEC-3.5", 6, W_35,
            "v_gs < V_T + n (kT/q)",
            se("SE-3.5-EQ6", 4298, 4298, "formula"), None)
ATOMS[-1]["atom_type"] = "condition_atom"
add_anchor(
    "A-ST-TEMP", "SEC-3.5", "physical_effect_atom",
    "the temperature coefficient of the threshold voltage is negative in the subthreshold region. The variation of current due to temperature of a device operating in weak inversion is dominated by the negative temperature coefficient of the threshold voltage. Therefore, for a given gate-source voltage, subthreshold current increases as the temperature increases.",
    W_35,
    "In weak inversion the threshold-voltage temperature coefficient is negative, so for a given v_GS the subthreshold current increases as temperature increases. Weak-inversion operation is important for low-power circuits.",
    se("SE-3.5-TEMP", 4317, 4317, "paragraph"),
)

# ===========================================================================
# 3.6 SPICE simulation
# ===========================================================================
add_anchor(
    "A-SP-NETLIST", "SEC-3.6", "technology_process_atom",
    "The four numbers following”M1” specify the nets (or nodes) to which the drain, gate, source, and substrate (bulk) are connected.",
    W_36,
    "A SPICE MOS simulation needs instance declarations and model descriptions. Instance: M<number> <DRAIN> <GATE> <SOURCE> <BULK> <model> W=.. L=.. ; the four nets after M1 are drain, gate, source, substrate (bulk). The .MODEL line gives type NMOS/PMOS, LEVEL, and parameters (VTO, KP, GAMMA, LAMBDA, PHI).",
    se("SE-3.6-NETLIST", 4348, 4348, "paragraph"),
)
add_anchor(
    "A-SP-MULT", "SEC-3.6", "design_principle_atom",
    "This principle prescribes that when one device needs to be “M” times larger than another device, then the larger device should be made from “M” units of the smaller device.",
    W_36,
    "The unit-matching principle (Sec.2.6): an M-times-larger device should be made from M parallel unit devices, instantiated in SPICE with the multiplier parameter M= rather than a single scaled-up device.",
    se("SE-3.6-MULT", 4372, 4372, "paragraph"),
)
# Example 3.6-1
add_anchor(
    "A-EX361-PROBLEM", "SEC-EX361", "example_problem_atom",
    "Use SPICE to obtain the output characteristics of the n-channel transistor shown in Fig. 3.6-2 using the LEVEL 1 model and the parameter values of Table 3.1-2. The output curves are to be plotted for drain-source voltages from 0 to 5 V and for gate-source voltages of 1, 2, 3, 4, and 5 V. Assume that the bulk voltage is zero.",
    W_EX361,
    "Use SPICE (LEVEL 1, Table 3.1-2 parameters) to obtain the output characteristics of an n-channel transistor: i_D vs v_DS from 0 to 5 V for v_GS = 1,2,3,4,5 V, bulk at 0 V.",
    se("SE-EX361-PROB", 4455, 4455, "paragraph"),
)
add_line(
    "A-EX361-NETLIST", "SEC-EX361", "formula_usage_atom",
    ".MODEL MOS1 NMOS VTO=0.7 KP=110U GAMMA=0.4 LAMBDA=0.04 PHI=0.7",
    4484,
    "Table 3.6-1 SPICE input (.MODEL MOS1 NMOS VTO=0.7 KP=110U GAMMA=0.4 LAMBDA=0.04 PHI=0.7; with M1 2 1 0 0 MOS1 W=5U L=1.0U; .DC VDS 0 5 0.2 VGS 1 5 1; .PRINT DC V(2) I(VDS); .END) - a nested dc sweep.",
    se("SE-EX361-NET", 4484, 4484, "code"),
    metadata=OrderedDict([("table_id", "Table 3.6-1"), ("code_block_lines", [4479, 4488])]),
)
add_anchor(
    "A-EX361-RESULT", "SEC-EX361", "result_atom",
    "The “.PRINT...” line directs the program to print the values of the dc sweeps. The last line of every SPICE input file must be .END which is line eleven. Fig. 3.6-3 shows the output plot of this analysis.",
    W_EX361,
    "Fig.3.6-3 output: a family of i_D vs v_DS curves rising and then saturating, one curve per v_GS = 2,3,4,5 V (higher v_GS gives higher saturated current).",
    se("SE-EX361-RES", 4455, 4455, "paragraph"),
)
# Example 3.6-3
add_anchor(
    "A-EX363-PROBLEM", "SEC-EX363", "example_problem_atom",
    "Use SPICE to obtain a small signal frequency response of $V _ { \\mathrm { O U T } } ( \\omega ) / V _ { \\mathrm { I N } } ( \\omega )$ when the amplifier is biased in the transition region. Assume that a 5 pF capacitor is attached to the output of Fig. 3.6-4 and find the magnitude and phase response over the frequency range of 100 Hz to 100 MHz.",
    W_EX363,
    "Use SPICE for a small-signal ac analysis V_OUT(w)/V_IN(w) of the Fig.3.6-4 amplifier biased in the transition region, with a 5 pF output capacitor, from 100 Hz to 100 MHz.",
    se("SE-EX363-PROB", 4577, 4577, "paragraph"),
)
add_line(
    "A-EX363-RESULT", "SEC-EX363", "result_atom",
    "VIN 1 0 DC -2.42 AC 1.0",
    4592,
    ".AC DEC 20 100 100MEG with VIN 1 0 DC -2.42 AC 1.0. Result (Fig.3.6-6): ~36 dB low-frequency gain rolling off above ~100 kHz; phase from 180 deg to 90 deg.",
    se("SE-EX363-RES", 4592, 4592, "code"),
    metadata=OrderedDict([("result_figure", "Fig.3.6-6 (36 dB; 180->90 deg)")]),
)
# Example 3.6-4
add_anchor(
    "A-EX364-PROBLEM", "SEC-EX364", "example_problem_atom",
    "The last simulation to be made with Fig. 3.6-4 is the transient response to an input pulse. This simulation will include the 5 pF output capacitor of the previous example and will be made from time zero to 4 microseconds.",
    W_EX364,
    "Use SPICE for the transient response of the Fig.3.6-4 amplifier (5 pF output cap) to an input pulse, from 0 to 4 microseconds.",
    se("SE-EX364-PROB", 4637, 4637, "paragraph"),
)
add_line(
    "A-EX364-RESULT", "SEC-EX364", "result_atom",
    "VIN 1 0 PWL(0 0V 1U 0V 1.05U 3V 3U 3V 3.05U 0V 6U 0V)",
    4665,
    ".TRAN 0.01U 4U with the input as a PWL pulse (VIN 1 0 PWL(0 0V 1U 0V 1.05U 3V 3U 3V 3.05U 0V 6U 0V)). Output Fig.3.6-7 shows v_IN(t) and v_OUT(t) (inverted pulse response).",
    se("SE-EX364-RES", 4665, 4665, "code"),
    metadata=OrderedDict([("result_figure", "Fig.3.6-7")]),
)

# ===========================================================================
# PROBLEMS
# ===========================================================================
add_anchor(
    "A-PROB-1", "SEC-PROB", "problem_statement_atom",
    "1 Sketch to scale the output characteristics of an enhancement n-channel device if $V _ { T } = 0 . 7$ volt and $I _ { D } = 5 0 0 \\mu \\mathrm { A }$ when $V _ { G S } = 5 \\mathrm { ~ V ~ }$ in saturation. Choose values of $V _ { G S }$ $= 1 , 2 , 3 , 4 ,$ , and 5 V. Assume that the channel modulation parameter is zero.",
    W_PROB,
    "1. Sketch to scale the output characteristics of an enhancement n-channel device if V_T=0.7 V and I_D=500 uA at V_GS=5 V in saturation, for V_GS=1,2,3,4,5 V, with channel modulation parameter zero.",
    se("SE-PROB-1", 4702, 4702, "paragraph"),
)
add_anchor(
    "A-PROB-5", "SEC-PROB", "problem_statement_atom",
    "How would you change Eq. (18) so that it would agree with Eq. (11) at $\\nu _ { D S } = \\nu _ { D S }$ (sat)?",
    W_PROB,
    "5. Eqs.(11) and (18) of Sec.3.1 do not agree at the nonsaturation-saturation transition. How would you change Eq.(18) so it agrees with Eq.(11) at v_DS = v_DS(sat)?",
    se("SE-PROB-5", 4713, 4713, "paragraph"),
)
add_anchor(
    "A-PROB-11", "SEC-PROB", "problem_statement_atom",
    "11. Repeat Examples 3.3-1 and 3.3-2 if the W/L ratio is 100 m/10 m.",
    W_PROB,
    "11. Repeat Examples 3.3-1 and 3.3-2 if the W/L ratio is 100um/10um.",
    se("SE-PROB-11", 4754, 4754, "paragraph"),
)
add_anchor(
    "A-PROB-15", "SEC-PROB", "problem_statement_atom",
    "15. Calculate the value for $V _ { O N }$ for n MOS transistor in weak inversion assuming that $f s$ and fn can be approximated to be unity (1.0).",
    W_PROB,
    "15. Calculate V_ON for an MOS transistor in weak inversion assuming fs and fn can be approximated as unity.",
    se("SE-PROB-15", 4784, 4784, "paragraph"),
)
add_anchor(
    "A-PROB-16", "SEC-PROB", "problem_statement_atom",
    "16. Develop an expression for the small signal transconductance of a MOS device operating in weak inversion using the large signal expression of Eq. (5) of Sec. 3.5.",
    W_PROB,
    "16. Develop an expression for the small-signal transconductance of a MOS device in weak inversion using the large-signal expression of Eq.(5) of Sec.3.5.",
    se("SE-PROB-16", 4785, 4785, "paragraph"),
)

# ---------------------------------------------------------------------------
ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# source_elements (built from SE table; line ranges authoritative).
# ---------------------------------------------------------------------------
SOURCE_ELEMENTS = []
for seid in sorted(SE, key=lambda k: SE[k][1]):
    etype, ls, le = SE[seid]
    SOURCE_ELEMENTS.append(OrderedDict([
        ("id", seid), ("type", etype), ("file", SOURCE_NAME),
        ("line_start", ls), ("line_end", le),
    ]))

# ---------------------------------------------------------------------------
# section_tree (IDs preserved from v0.1).
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-3"), ("path", "3"), ("title", "CMOS Device Modeling")]),
    OrderedDict([("id", "SEC-3.1"), ("path", "3 > 3.1"), ("title", "Simple MOS Large-Signal Model"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-EX311"), ("path", "3 > 3.1 > Example 3.1-1"), ("title", "Example 3.1-1 Application of the Simple MOS Large Signal Model"), ("parent", "SEC-3.1"), ("kind", "example")]),
    OrderedDict([("id", "SEC-3.2"), ("path", "3 > 3.2"), ("title", "Other MOS Large-Signal Model Parameters"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-3.3"), ("path", "3 > 3.3"), ("title", "Small-Signal Model for the MOS Transistor"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-EX331"), ("path", "3 > 3.3 > Example 3.3-1"), ("title", "Example 3.3-1 Typical Values of Small Signal Model Parameters"), ("parent", "SEC-3.3"), ("kind", "example")]),
    OrderedDict([("id", "SEC-EX332"), ("path", "3 > 3.3 > Example 3.3-2"), ("title", "Example 3.3-2 Small-Signal Parameters in the Nonsaturated Region"), ("parent", "SEC-3.3"), ("kind", "example")]),
    OrderedDict([("id", "SEC-3.4"), ("path", "3 > 3.4"), ("title", "Computer Simulation Models"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-3.4-L3"), ("path", "3 > 3.4 > SPICE Level 3 Model"), ("title", "SPICE Level 3 Model"), ("parent", "SEC-3.4")]),
    OrderedDict([("id", "SEC-3.4-BSIM"), ("path", "3 > 3.4 > BSIM3v3 Model"), ("title", "BSIM3v3 Model"), ("parent", "SEC-3.4")]),
    OrderedDict([("id", "SEC-3.5"), ("path", "3 > 3.5"), ("title", "Subthreshold MOS Model"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-3.6"), ("path", "3 > 3.6"), ("title", "SPICE Simulation of MOS Circuits"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-EX361"), ("path", "3 > 3.6 > Example 3.6-1"), ("title", "Example 3.6-1 Use of SPICE to Simulate MOS Output Characteristics"), ("parent", "SEC-3.6"), ("kind", "example")]),
    OrderedDict([("id", "SEC-EX363"), ("path", "3 > 3.6 > Example 3.6-3"), ("title", "Example 3.6-3 ac Analysis of Fig. 3.6-4"), ("parent", "SEC-3.6"), ("kind", "example")]),
    OrderedDict([("id", "SEC-EX364"), ("path", "3 > 3.6 > Example 3.6-4"), ("title", "Example 3.6-4 Transient Analysis of Fig. 3.6-4"), ("parent", "SEC-3.6"), ("kind", "example")]),
    OrderedDict([("id", "SEC-3.7"), ("path", "3 > 3.7"), ("title", "Summary"), ("parent", "SEC-3")]),
    OrderedDict([("id", "SEC-PROB"), ("path", "3 > PROBLEMS"), ("title", "PROBLEMS"), ("parent", "SEC-3")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks (IDs & membership preserved from v0.1).
# ---------------------------------------------------------------------------
def chunk(cid, ctype, spath, atom_ids, central, reason, targets, must):
    return OrderedDict([
        ("id", cid), ("profile", "textbook"), ("chunk_type", ctype),
        ("section_path", spath), ("atom_ids", atom_ids), ("central_atom_ids", central),
        ("boundary_reason", reason), ("extraction_targets", targets),
        ("gold_must_cover_atoms", must),
    ])


SEMANTIC_CHUNKS = [
    chunk("C-OV", "chapter_overview_block", "3", ["A-INTRO", "A-SUMMARY"], ["A-INTRO"],
          "chapter scope: three model levels (intro + summary); framing text, not typed objects.",
          ["Concept"], ["A-INTRO"]),
    chunk("C-LS-MODEL", "formula_definition_block", "3 > 3.1",
          ["A-LS-SAH-1", "A-LS-VT-2", "A-LS-GAMMA-4", "A-LS-PARAMS", "A-LS-ID-K-12", "A-LS-BETA-13", "A-LS-T312"],
          ["A-LS-SAH-1", "A-LS-ID-K-12"],
          "Sah/Shichman-Hodges drain-current equation with its parameter definitions (mu_o,C_ox,V_T,gamma,K',beta) and Table 3.1-2; formula + variable definitions kept together.",
          ["Formula", "Variable"], ["A-LS-SAH-1", "A-LS-ID-K-12", "A-LS-BETA-13", "A-LS-PARAMS"]),
    chunk("C-LS-REGIONS", "derivation_block", "3 > 3.1",
          ["A-LS-CUTOFF-14", "A-LS-VDSAT-15", "A-LS-NONSAT-16", "A-LS-SAT-17", "A-LS-CLM-EFFECT", "A-LS-SAT-CLM-18", "A-LS-FIVE-PARAMS", "A-LS-VON-20"],
          ["A-LS-SAT-CLM-18", "A-LS-VDSAT-15"],
          "regions of operation derived from Eq.(1): cutoff -> v_DS(sat) boundary -> nonsaturation -> saturation -> saturation with channel-length modulation; one continuous derivation chain.",
          ["Formula", "PhysicalEffect", "Variable"],
          ["A-LS-VDSAT-15", "A-LS-NONSAT-16", "A-LS-SAT-17", "A-LS-SAT-CLM-18", "A-LS-CLM-EFFECT"]),
    chunk("C-EX-311", "example_solution_block", "3 > 3.1 > Example 3.1-1",
          ["A-EX311-PROBLEM", "A-EX311-GIVEN", "A-EX311-USE", "A-EX311-RESULT"], ["A-EX311-PROBLEM"],
          "problem/given/formula-usage/result continuous (example_solution_continuity).",
          ["ExampleProblem", "ExampleSolution"],
          ["A-EX311-PROBLEM", "A-EX311-GIVEN", "A-EX311-USE", "A-EX311-RESULT"]),
    chunk("C-LS2-JUNC", "formula_definition_block", "3 > 3.2",
          ["A-LS2-DIODE-1", "A-LS2-RDRS", "A-LS2-CBX-3", "A-LS2-CBX-4", "A-LS2-CBX-SW-5"], ["A-LS2-CBX-3"],
          "bulk-source/drain pn-junction diodes, ohmic r_D/r_S, and the two-region (plus sidewall) depletion-capacitance formulas kept together.",
          ["Formula", "ComponentModel"], ["A-LS2-DIODE-1", "A-LS2-CBX-3", "A-LS2-CBX-SW-5"]),
    chunk("C-LS2-CAPS", "component_model_block", "3 > 3.2",
          ["A-LS2-COVERLAP-7", "A-LS2-C2-8", "A-LS2-CGREGIONS", "A-LS2-T321", "A-LS2-NOISE-12"], ["A-LS2-CGREGIONS"],
          "charge-storage capacitor model (overlap C_1/C_3, gate-channel C_2, per-region C_GS/C_GD/C_GB) + Table 3.2-1 coefficients + noise current source.",
          ["Formula", "ComponentModel", "PhysicalEffect"],
          ["A-LS2-COVERLAP-7", "A-LS2-CGREGIONS", "A-LS2-T321"]),
    chunk("C-SS-MODEL", "component_model_block", "3 > 3.3",
          ["A-SS-INTRO", "A-SS-GBDGBS-1", "A-SS-DEFS-3", "A-SS-GM-6", "A-SS-GMBS-8", "A-SS-GDS-9", "A-SS-T331"], ["A-SS-GM-6"],
          "small-signal MOS model: definitions of g_m/g_mbs/g_ds as partials + their saturation-region closed forms + Table 3.3-1; one component model.",
          ["Concept", "Formula", "Variable", "ComponentModel"],
          ["A-SS-GM-6", "A-SS-GMBS-8", "A-SS-GDS-9", "A-SS-DEFS-3"]),
    chunk("C-EX-331", "example_solution_block", "3 > 3.3 > Example 3.3-1",
          ["A-EX331-PROBLEM", "A-EX331-USE", "A-EX331-RESULT"], ["A-EX331-PROBLEM"],
          "small-signal-parameter worked example (saturation) continuous.",
          ["ExampleSolution"], ["A-EX331-PROBLEM", "A-EX331-RESULT"]),
    chunk("C-EX-332", "example_solution_block", "3 > 3.3 > Example 3.3-2",
          ["A-EX332-PROBLEM", "A-EX332-RESULT"], ["A-EX332-PROBLEM"],
          "small-signal-parameter worked example (nonsaturation) continuous.",
          ["ExampleSolution"], ["A-EX332-PROBLEM", "A-EX332-RESULT"]),
    chunk("C-L3", "formula_definition_block", "3 > 3.4 > SPICE Level 3 Model",
          ["A-CS-INTRO", "A-CS-L3-DRAIN-1", "A-CS-L3-PHI-NOTE", "A-CS-L3-VT-12", "A-CS-L3-VSAT-16", "A-CS-L3-MOB-18", "A-CS-L3-CLM-20", "A-CS-L3-MOBT-15", "A-CS-L3-T341"],
          ["A-CS-L3-DRAIN-1"],
          "SPICE Level 3 model parameter set: drain current, threshold, saturation voltage, effective mobility, channel-length modulation subsections + Table 3.4-1; one model definition.",
          ["Formula", "Variable", "ComponentModel", "PhysicalEffect"],
          ["A-CS-L3-DRAIN-1", "A-CS-L3-VT-12", "A-CS-L3-MOB-18", "A-CS-L3-CLM-20", "A-CS-L3-T341"]),
    chunk("C-BSIM", "component_model_block", "3 > 3.4 > BSIM3v3 Model",
          ["A-CS-BSIM-HISTORY", "A-CS-BSIM-EFFECTS", "A-CS-MODEL-COMPARE"], ["A-CS-BSIM-HISTORY"],
          "BSIM model family + deep-submicron effects it models + Level1/Level3/BSIM3v3 accuracy comparison.",
          ["ComponentModel", "PhysicalEffect"], ["A-CS-BSIM-HISTORY", "A-CS-BSIM-EFFECTS"]),
    chunk("C-SUBTHRESHOLD", "physical_effect_block", "3 > 3.5",
          ["A-ST-CONCEPT", "A-ST-VON-1", "A-ST-IDS-3", "A-ST-IDS-HAND-5", "A-ST-ENTRY-6", "A-ST-TEMP"],
          ["A-ST-CONCEPT", "A-ST-IDS-HAND-5"],
          "weak-inversion physical effect + both SPICE Level 3 and hand-calculation exponential i_D models + entry condition + temperature behavior.",
          ["PhysicalEffect", "Formula", "Variable"], ["A-ST-CONCEPT", "A-ST-IDS-HAND-5", "A-ST-VON-1"]),
    chunk("C-SPICE", "technology_process_block", "3 > 3.6",
          ["A-SP-NETLIST", "A-SP-MULT"], ["A-SP-NETLIST"],
          "SPICE instance/model syntax + unit-matching-to-multiplier principle.",
          ["DesignPrinciple"], ["A-SP-NETLIST"]),
    chunk("C-EX-361", "example_solution_block", "3 > 3.6 > Example 3.6-1",
          ["A-EX361-PROBLEM", "A-EX361-NETLIST", "A-EX361-RESULT"], ["A-EX361-PROBLEM"],
          "SPICE output-characteristic example: problem + netlist (Table 3.6-1) + plotted result kept together.",
          ["ExampleSolution"], ["A-EX361-PROBLEM", "A-EX361-NETLIST", "A-EX361-RESULT"]),
    chunk("C-EX-363", "example_solution_block", "3 > 3.6 > Example 3.6-3",
          ["A-EX363-PROBLEM", "A-EX363-RESULT"], ["A-EX363-PROBLEM"],
          "SPICE ac-analysis example continuous.",
          ["ExampleSolution"], ["A-EX363-PROBLEM", "A-EX363-RESULT"]),
    chunk("C-EX-364", "example_solution_block", "3 > 3.6 > Example 3.6-4",
          ["A-EX364-PROBLEM", "A-EX364-RESULT"], ["A-EX364-PROBLEM"],
          "SPICE transient-analysis example continuous.",
          ["ExampleSolution"], ["A-EX364-PROBLEM", "A-EX364-RESULT"]),
    chunk("C-PROB", "problem_set_block", "3 > PROBLEMS",
          ["A-PROB-1", "A-PROB-5", "A-PROB-11", "A-PROB-15", "A-PROB-16"], ["A-PROB-1"],
          "end-of-chapter problems.",
          ["ProblemStatement"], ["A-PROB-1", "A-PROB-11", "A-PROB-15"]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}
CHUNK_BY_ID = {c["id"]: c for c in SEMANTIC_CHUNKS}

# ---------------------------------------------------------------------------
# objects (IDs preserved). home_package + local/supporting evidence split.
# ---------------------------------------------------------------------------
def obj(oid, otype, section_path, home_package, payload, local, supporting, difficulty, evidence_strength):
    return OrderedDict([
        ("id", oid), ("type", otype), ("section_path", section_path),
        ("home_package", home_package), ("payload", payload),
        ("local_evidence_atom_ids", local), ("supporting_context_atom_ids", supporting),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


OBJECTS = [
    obj("CONCEPT-SMALL-SIGNAL", "Concept", "3 > 3.3", "PKG-SS-MODEL",
        OrderedDict([("term", "small-signal model"),
                     ("definition", "Linear MOS model whose parameters are partial derivatives of large-signal variables at the quiescent point; valid where large-signal behavior is approximately linear.")]),
        ["A-SS-INTRO"], [], "medium", "direct"),
    obj("FORMULA-SAH-DRAIN", "Formula", "3 > 3.1", "PKG-LS-MODEL",
        OrderedDict([("name", "Sah/Shichman-Hodges drain current"),
                     ("expression", "i_D = K'(W/L)[(v_GS - V_T) - v_DS/2] v_DS"),
                     ("variables", OrderedDict([("K'", "mu_o C_ox = transconductance parameter"),
                                                ("W/L", "aspect ratio"), ("V_T", "threshold voltage"),
                                                ("v_DS", "drain-source voltage")]))]),
        ["A-LS-SAH-1", "A-LS-ID-K-12", "A-LS-PARAMS"], [], "medium", "direct"),
    obj("FORMULA-BETA", "Formula", "3 > 3.1", "PKG-LS-MODEL",
        OrderedDict([("name", "transconductance parameter"),
                     ("expression", "beta = K'(W/L) ~= (mu_o C_ox)(W/L)")]),
        ["A-LS-BETA-13"], [], "easy", "direct"),
    obj("FORMULA-VDSSAT", "Formula", "3 > 3.1", "PKG-LS-REGIONS",
        OrderedDict([("name", "saturation voltage"), ("expression", "v_DS(sat) = v_GS - V_T"),
                     ("note", "= V_ON; boundary between nonsaturation and saturation.")]),
        ["A-LS-VDSAT-15", "A-LS-VON-20"], [], "easy", "direct"),
    obj("FORMULA-ID-NONSAT", "Formula", "3 > 3.1", "PKG-LS-REGIONS",
        OrderedDict([("name", "nonsaturation drain current"),
                     ("expression", "i_D = K'(W/L)[(v_GS - V_T) - v_DS/2] v_DS, 0 < v_DS <= v_GS - V_T")]),
        ["A-LS-NONSAT-16"], [], "medium", "direct"),
    obj("FORMULA-ID-SAT", "Formula", "3 > 3.1", "PKG-LS-REGIONS",
        OrderedDict([("name", "saturation drain current with channel-length modulation"),
                     ("expression", "i_D = K'(W/2L)(v_GS - V_T)^2 (1 + lambda v_DS)"),
                     ("variables", OrderedDict([("lambda", "channel-length modulation parameter (V^-1)")]))]),
        ["A-LS-SAT-17", "A-LS-SAT-CLM-18"], [], "medium", "direct"),
    obj("FORMULA-THRESHOLD", "Formula", "3 > 3.1", "PKG-LS-MODEL",
        OrderedDict([("name", "threshold voltage with body effect"),
                     ("expression", "V_T = V_T0 + gamma[sqrt(2|phi_F| + v_SB) - sqrt(2|phi_F|)]")]),
        ["A-LS-VT-2", "A-LS-GAMMA-4"], [], "medium", "direct"),
    obj("VAR-LAMBDA", "Variable", "3 > 3.1", "PKG-LS-REGIONS",
        OrderedDict([("symbol", "lambda"), ("name", "channel-length modulation parameter"),
                     ("units", "V^-1"), ("typical", "0.04 (N) / 0.05 (P) at L=1um (Table 3.1-2)")]),
        ["A-LS-SAT-CLM-18"], ["A-LS-T312"], "medium", "direct"),
    obj("VAR-KPRIME", "Variable", "3 > 3.1", "PKG-LS-MODEL",
        OrderedDict([("symbol", "K'"), ("name", "transconductance parameter"),
                     ("expression", "mu_o C_ox"), ("units", "uA/V^2"),
                     ("typical", "110 (N) / 50 (P) in saturation")]),
        ["A-LS-BETA-13", "A-LS-T312"], [], "medium", "direct"),
    obj("DERIV-REGIONS", "Derivation", "3 > 3.1", "PKG-LS-REGIONS",
        OrderedDict([("name", "regions of operation of the MOS drain current"),
                     ("steps", ["cutoff i_D=0 (Eq.14)", "boundary v_DS(sat)=v_GS-V_T (Eq.15)",
                                "nonsaturation (Eq.16)", "saturation lambda=0 (Eq.17)",
                                "saturation with channel-length modulation (Eq.18)"])]),
        ["A-LS-CUTOFF-14", "A-LS-VDSAT-15", "A-LS-NONSAT-16", "A-LS-SAT-17", "A-LS-SAT-CLM-18"],
        [], "medium", "direct"),
    obj("EXAMPLE-3.1-1", "ExampleSolution", "3 > 3.1 > Example 3.1-1", "PKG-EX-311",
        OrderedDict([("title", "Example 3.1-1 Simple MOS Large-Signal Model"),
                     ("problem", "Find i_D of n- and p-channel transistors, W/L=5/1, given terminal voltages."),
                     ("given", "v_D,v_G,v_S,v_B = 3,2,0,0 (N) and -3,-2,0,0 (P)."),
                     ("result", "i_D = 520 uA (N), 243 uA (P).")]),
        ["A-EX311-PROBLEM", "A-EX311-GIVEN", "A-EX311-USE", "A-EX311-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-3.3-1", "ExampleSolution", "3 > 3.3 > Example 3.3-1", "PKG-EX-331",
        OrderedDict([("title", "Example 3.3-1 Typical Small-Signal Parameters (saturation)"),
                     ("problem", "Find g_m,g_mbs,g_ds for |I_D|=50 uA, |V_SB|=2 V, W/L=1/1."),
                     ("result", "N: g_m=105, g_mbs=12.8, g_ds=2.0 uA/V; P: g_m=70.7, g_mbs=12.0, g_ds=2.5 uA/V.")]),
        ["A-EX331-PROBLEM", "A-EX331-USE", "A-EX331-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-3.3-2", "ExampleSolution", "3 > 3.3 > Example 3.3-2", "PKG-EX-332",
        OrderedDict([("title", "Example 3.3-2 Small-Signal Parameters (nonsaturation)"),
                     ("problem", "Find nonsaturation g_m,g_mbs,r_ds for V_GS=5,V_DS=1,|V_BS|=2,W/L=1/1."),
                     ("result", "N: g_m=110 uA/V, g_mbs=46.6 uA/V, r_ds=3.05 KOhm; P: g_m=50 uA/V, g_mbs=28.6 uA/V, r_ds=6.99 KOhm.")]),
        ["A-EX332-PROBLEM", "A-EX332-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-3.6-1", "ExampleSolution", "3 > 3.6 > Example 3.6-1", "PKG-EX-361",
        OrderedDict([("title", "Example 3.6-1 SPICE Output Characteristics"),
                     ("problem", "Plot i_D vs v_DS (0-5 V) for v_GS=1..5 V using LEVEL 1 model."),
                     ("netlist", ".MODEL MOS1 NMOS VTO=0.7 KP=110U GAMMA=0.4 LAMBDA=0.04 PHI=0.7; .DC VDS 0 5 0.2 VGS 1 5 1"),
                     ("result", "Family of saturating i_D-v_DS curves (Fig.3.6-3).")]),
        ["A-EX361-PROBLEM", "A-EX361-NETLIST", "A-EX361-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-3.6-3", "ExampleSolution", "3 > 3.6 > Example 3.6-3", "PKG-EX-363",
        OrderedDict([("title", "Example 3.6-3 SPICE ac Analysis"),
                     ("problem", "Small-signal V_OUT/V_IN of Fig.3.6-4 amplifier, 5 pF load, 100 Hz-100 MHz."),
                     ("result", "~36 dB gain rolling off above ~100 kHz; phase 180->90 deg.")]),
        ["A-EX363-PROBLEM", "A-EX363-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-3.6-4", "ExampleSolution", "3 > 3.6 > Example 3.6-4", "PKG-EX-364",
        OrderedDict([("title", "Example 3.6-4 SPICE Transient Analysis"),
                     ("problem", "Transient response of Fig.3.6-4 amplifier to a PWL input pulse, 0-4 us."),
                     ("result", ".TRAN 0.01U 4U; inverted pulse response (Fig.3.6-7).")]),
        ["A-EX364-PROBLEM", "A-EX364-RESULT"], [], "medium", "direct"),
    obj("COMP-BULK-JUNCTIONS", "ComponentModel", "3 > 3.2", "PKG-LS2-JUNC",
        OrderedDict([("name", "bulk source/drain pn junctions"),
                     ("note", "i_BD=I_S[exp(q v_BD/kT)-1], i_BS=I_S[exp(q v_BS/kT)-1]; reverse-biased diodes modeling leakage; r_D,r_S = 50-100 Ohm ohmic resistance.")]),
        ["A-LS2-DIODE-1", "A-LS2-RDRS"], [], "medium", "direct"),
    obj("COMP-DEPLETION-CAPS", "ComponentModel", "3 > 3.2", "PKG-LS2-JUNC",
        OrderedDict([("name", "bulk-junction depletion capacitances C_BD/C_BS"),
                     ("expression", "C_BX = (CJ)(AX)[1-v_BX/PB]^(-MJ) (region 1); bottom + sidewall (CJSW,MJSW) form Eq.(5)"),
                     ("variants", ["bottom (CJ,MJ)", "sidewall (CJSW,MJSW)"])]),
        ["A-LS2-CBX-3", "A-LS2-CBX-4", "A-LS2-CBX-SW-5"], ["A-LS2-T321"], "medium", "direct"),
    obj("COMP-CHARGE-STORAGE-CAPS", "ComponentModel", "3 > 3.2", "PKG-LS2-CAPS",
        OrderedDict([("name", "gate charge-storage capacitances C_GS/C_GD/C_GB"),
                     ("note", "overlap C_1=C_3=(LD)(W_eff)C_ox; gate-channel C_2=W_eff L_eff C_ox; allocated by region (off/saturation/nonsaturation).")]),
        ["A-LS2-COVERLAP-7", "A-LS2-C2-8", "A-LS2-CGREGIONS", "A-LS2-T321"], [], "medium", "direct"),
    obj("COMP-SMALL-SIGNAL-MODEL", "ComponentModel", "3 > 3.3", "PKG-SS-MODEL",
        OrderedDict([("name", "MOS small-signal model"),
                     ("parameters", OrderedDict([("g_m", "sqrt(2 K' (W/L) I_D)"),
                                                 ("g_mbs", "g_m gamma/(2(2|phi_F|+V_SB)^1/2) = eta g_m"),
                                                 ("g_ds", "I_D lambda")]))]),
        ["A-SS-GBDGBS-1", "A-SS-DEFS-3", "A-SS-GM-6", "A-SS-GMBS-8", "A-SS-GDS-9", "A-SS-T331"], [], "medium", "direct"),
    obj("COMP-SPICE-LEVEL1", "ComponentModel", "3 > 3.1", "PKG-LS-REGIONS",
        OrderedDict([("name", "SPICE LEVEL 1 model"),
                     ("note", "the simple large-signal model; five defining parameters K', V_T, gamma, lambda, 2 phi_F; good for L,W > ~10um, hand calculations.")]),
        ["A-LS-FIVE-PARAMS"], ["A-LS-SAH-1"], "medium", "direct"),
    obj("COMP-SPICE-LEVEL3", "ComponentModel", "3 > 3.4 > SPICE Level 3 Model", "PKG-L3",
        OrderedDict([("name", "SPICE Level 3 model"),
                     ("note", "short-channel parameter set (BETA/KP, GAMMA, PHI, ETA, DELTA, KAPPA, THETA, U0, NSUB, NFS); drain current, threshold, saturation voltage, effective mobility, channel-length modulation; good to ~0.8um."),
                     ("effects", ["narrow/short-channel threshold adjust", "mobility degradation (THETA)",
                                  "channel-length modulation (KAPPA)", "weak inversion (NFS)"])]),
        ["A-CS-INTRO", "A-CS-L3-DRAIN-1", "A-CS-L3-PHI-NOTE", "A-CS-L3-VT-12", "A-CS-L3-VSAT-16", "A-CS-L3-MOB-18", "A-CS-L3-CLM-20", "A-CS-L3-MOBT-15", "A-CS-L3-T341"],
        [], "hard", "direct"),
    obj("COMP-BSIM3V3", "ComponentModel", "3 > 3.4 > BSIM3v3 Model", "PKG-BSIM",
        OrderedDict([("name", "BSIM3v3 model"),
                     ("note", "UC Berkeley deep-submicron MOS model; device-physics based with 40 dc parameters; industry-standard. Lineage BSIM1(60)->BSIM2(99)->BSIM3(40)->BSIM3v3.")]),
        ["A-CS-BSIM-HISTORY"], [], "medium", "direct"),
    obj("PHYS-CLM", "PhysicalEffect", "3 > 3.1", "PKG-LS-REGIONS",
        OrderedDict([("name", "channel length modulation"),
                     ("description", "Drain-voltage-induced shortening of the channel increases saturation current; modeled by the factor (1 + lambda v_DS).")]),
        ["A-LS-CLM-EFFECT", "A-LS-SAT-CLM-18"], [], "medium", "direct"),
    obj("PHYS-SUBTHRESHOLD", "PhysicalEffect", "3 > 3.5", "PKG-SUBTHRESHOLD",
        OrderedDict([("name", "subthreshold (weak inversion) conduction"),
                     ("description", "Below V_T the i_D-v_GS relation is exponential, not zero; current flows in weak inversion, important for low-power circuits; positive temperature dependence of current."),
                     ("expression", "i_D ~= (W/L) I_DO exp(v_GS/(n kT/q)), 1 < n < 3")]),
        ["A-ST-CONCEPT", "A-ST-IDS-HAND-5", "A-ST-ENTRY-6", "A-ST-TEMP"], [], "medium", "direct"),
    obj("PHYS-BSIM-EFFECTS", "PhysicalEffect", "3 > 3.4 > BSIM3v3 Model", "PKG-BSIM",
        OrderedDict([("name", "deep-submicron MOSFET effects"),
                     ("description", "Threshold reduction, vertical-field mobility degradation, velocity saturation, DIBL, channel-length modulation, weak-inversion conduction, S/D parasitic resistance, hot-electron output-resistance effects.")]),
        ["A-CS-BSIM-EFFECTS"], [], "medium", "direct"),
    obj("PHYS-NOISE", "PhysicalEffect", "3 > 3.2", "PKG-LS2-CAPS",
        OrderedDict([("name", "MOS drain-current noise"),
                     ("description", "Thermal + flicker noise; i_N^2 = [8 kT g_m(1+eta)/3 + KF I_D/(f C_ox L^2)] df.")]),
        ["A-LS2-NOISE-12"], [], "medium", "direct"),
    obj("PRINCIPLE-MULTIPLIER", "DesignPrinciple", "3 > 3.6", "PKG-SPICE",
        OrderedDict([("statement", "Implement an M-times-larger device as M parallel unit devices and use the SPICE multiplier parameter M= (unit-matching principle from Sec.2.6)."),
                     ("applies_to", ["device matching", "SPICE instantiation"])]),
        ["A-SP-MULT"], [], "medium", "direct"),
    obj("PROB-1", "ProblemStatement", "3 > PROBLEMS", "PKG-PROB",
        OrderedDict([("question", "Sketch output characteristics of an enhancement n-channel device (V_T=0.7, I_D=500uA at V_GS=5V), lambda=0.")]),
        ["A-PROB-1"], [], "easy", "direct"),
    obj("PROB-5", "ProblemStatement", "3 > PROBLEMS", "PKG-PROB",
        OrderedDict([("question", "Modify Eq.(18) so it agrees with Eq.(11) at v_DS = v_DS(sat) (model continuity at the saturation boundary).")]),
        ["A-PROB-5"], [], "medium", "direct"),
    obj("PROB-11", "ProblemStatement", "3 > PROBLEMS", "PKG-PROB",
        OrderedDict([("question", "Repeat Examples 3.3-1 and 3.3-2 if W/L = 100um/10um.")]),
        ["A-PROB-11"], [], "easy", "direct"),
    obj("PROB-15", "ProblemStatement", "3 > PROBLEMS", "PKG-PROB",
        OrderedDict([("question", "Calculate V_ON for an MOS transistor in weak inversion assuming fs=fn=1.")]),
        ["A-PROB-15"], [], "medium", "direct"),
    obj("PROB-16", "ProblemStatement", "3 > PROBLEMS", "PKG-PROB",
        OrderedDict([("question", "Derive the weak-inversion small-signal transconductance from Eq.(5) of Sec.3.5.")]),
        ["A-PROB-16"], [], "hard", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids.
# ---------------------------------------------------------------------------
DOC_TITLE = "CMOS Analog Circuit Design"


def build_package(pid, chunk_id, section_path, linked_context, targets,
                  expected_local_fields=None, note=None):
    expected = [o["id"] for o in OBJECTS if o["home_package"] == pid]
    d = OrderedDict([
        ("id", pid), ("profile", "textbook"), ("chunk_id", chunk_id),
        ("section_path", section_path), ("document_title", DOC_TITLE),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", linked_context),
        ("extraction_targets", targets),
        ("expected_objects", expected),
    ])
    if expected_local_fields:
        d["expected_local_fields"] = expected_local_fields
    if note:
        d["note"] = note
    return d


CONTEXT_PACKAGES = [
    build_package("PKG-OV", "C-OV", "Chapter 3 > 3 CMOS Device Modeling",
                  OrderedDict([("previous_heading", "Chapter 2 CMOS Technology"),
                               ("next_heading", "3.1 Simple MOS Large-Signal")]),
                  ["Concept"],
                  note="All atoms are context_only chapter framing (intro + summary); no typed objects are expected from this package."),
    build_package("PKG-LS-MODEL", "C-LS-MODEL", "Chapter 3 > 3.1 Simple MOS Large-Signal Model",
                  OrderedDict([("table", "Table 3.1-2 simple-model parameters (0.8um n-well process)"),
                               ("previous_heading", "Chapter 3 intro"),
                               ("next_heading", "Regions of Operation")]),
                  ["Formula", "Variable", "ComponentModel"]),
    build_package("PKG-LS-REGIONS", "C-LS-REGIONS", "Chapter 3 > 3.1 Simple MOS Large-Signal Model > Regions of Operation",
                  OrderedDict([("formula_context", "saturation current Eq.(17) modified by channel-length modulation factor (1+lambda v_DS) -> Eq.(18); boundary v_DS(sat)=v_GS-V_T"),
                               ("previous_heading", "3.1 Simple MOS Large-Signal Model (Eq.1 Sah equation)"),
                               ("next_heading", "Example 3.1-1 Application of the Simple MOS Large Signal Model")]),
                  ["Formula", "Variable", "PhysicalEffect", "Derivation", "ComponentModel"],
                  expected_local_fields=OrderedDict([
                      ("VAR-LAMBDA", ["symbol", "name", "units"]),
                      ("COMP-SPICE-LEVEL1", ["name", "note"]),
                  ])),
    build_package("PKG-EX-311", "C-EX-311", "Chapter 3 > 3.1 > Example 3.1-1",
                  OrderedDict([("formula_context", "uses Eq.(15) v_DS(sat) and Eq.(18) saturation current"),
                               ("previous_heading", "3.1 Simple MOS Large-Signal Model"),
                               ("next_heading", "3.2 Other MOS Large-Signal Model Parameters")]),
                  ["ExampleProblem", "ExampleSolution"]),
    build_package("PKG-LS2-JUNC", "C-LS2-JUNC", "Chapter 3 > 3.2 Other MOS Large-Signal Model Parameters",
                  OrderedDict([("figure", "Fig. 3.2-1 complete large-signal model"),
                               ("previous_heading", "Example 3.1-1"),
                               ("next_heading", "charge-storage capacitances")]),
                  ["Formula", "ComponentModel"]),
    build_package("PKG-LS2-CAPS", "C-LS2-CAPS", "Chapter 3 > 3.2 Other MOS Large-Signal Model Parameters",
                  OrderedDict([("table", "Table 3.2-1 capacitance values/coefficients"),
                               ("previous_heading", "depletion capacitance"),
                               ("next_heading", "3.3 Small-Signal Model")]),
                  ["Formula", "ComponentModel", "PhysicalEffect"]),
    build_package("PKG-SS-MODEL", "C-SS-MODEL", "Chapter 3 > 3.3 Small-Signal Model for the MOS Transistor",
                  OrderedDict([("table", "Table 3.3-1 saturation small-signal relations"),
                               ("previous_heading", "3.2 Other MOS Large-Signal Model Parameters"),
                               ("next_heading", "Example 3.3-1")]),
                  ["Concept", "Formula", "Variable", "ComponentModel"]),
    build_package("PKG-EX-331", "C-EX-331", "Chapter 3 > 3.3 > Example 3.3-1",
                  OrderedDict([("formula_context", "uses small-signal Eqs.(6),(8),(9) with Table 3.1-2"),
                               ("previous_heading", "3.3 Small-Signal Model"),
                               ("next_heading", "Example 3.3-2")]),
                  ["ExampleSolution"]),
    build_package("PKG-EX-332", "C-EX-332", "Chapter 3 > 3.3 > Example 3.3-2",
                  OrderedDict([("formula_context", "uses nonsaturation small-signal Eqs.(10),(11),(12)"),
                               ("previous_heading", "Example 3.3-1"),
                               ("next_heading", "3.4 Computer Simulation Models")]),
                  ["ExampleSolution"]),
    build_package("PKG-L3", "C-L3", "Chapter 3 > 3.4 Computer Simulation Models > SPICE Level 3 Model",
                  OrderedDict([("table", "Table 3.4-1 Level-3 model parameters for a 0.8um n-well process"),
                               ("previous_heading", "3.4 Computer Simulation Models"),
                               ("next_heading", "BSIM3v3 Model")]),
                  ["Formula", "Variable", "ComponentModel", "PhysicalEffect"]),
    build_package("PKG-BSIM", "C-BSIM", "Chapter 3 > 3.4 Computer Simulation Models > BSIM3v3 Model",
                  OrderedDict([("figure", "Fig. 3.4-2 Level1/Level3/BSIM3v3 comparison"),
                               ("previous_heading", "SPICE Level 3 Model"),
                               ("next_heading", "3.5 Subthreshold MOS Model")]),
                  ["ComponentModel", "PhysicalEffect"]),
    build_package("PKG-SUBTHRESHOLD", "C-SUBTHRESHOLD", "Chapter 3 > 3.5 Subthreshold MOS Model",
                  OrderedDict([("formula_context", "SPICE Level 3 v_on/weak-inversion current Eqs.(1)-(4); hand-calc exponential Eq.(5)"),
                               ("previous_heading", "BSIM3v3 Model"),
                               ("next_heading", "3.6 SPICE Simulation of MOS Circuits")]),
                  ["PhysicalEffect", "Formula", "Variable"]),
    build_package("PKG-SPICE", "C-SPICE", "Chapter 3 > 3.6 SPICE Simulation of MOS Circuits",
                  OrderedDict([("previous_heading", "3.5 Subthreshold MOS Model"),
                               ("next_heading", "Example 3.6-1")]),
                  ["DesignPrinciple"]),
    build_package("PKG-EX-361", "C-EX-361", "Chapter 3 > 3.6 > Example 3.6-1",
                  OrderedDict([("table", "Table 3.6-1 SPICE input file (LEVEL 1)"),
                               ("previous_heading", "3.6 SPICE Simulation of MOS Circuits"),
                               ("next_heading", "Example 3.6-3")]),
                  ["ExampleSolution"],
                  expected_local_fields=OrderedDict([
                      ("COMP-SPICE-LEVEL1", ["name"]),
                  ])),
    build_package("PKG-EX-363", "C-EX-363", "Chapter 3 > 3.6 > Example 3.6-3",
                  OrderedDict([("formula_context", ".AC DEC 20 100 100MEG ac analysis; 36 dB / 180->90 deg result"),
                               ("previous_heading", "Example 3.6-1"),
                               ("next_heading", "Example 3.6-4")]),
                  ["ExampleSolution"]),
    build_package("PKG-EX-364", "C-EX-364", "Chapter 3 > 3.6 > Example 3.6-4",
                  OrderedDict([("formula_context", ".TRAN 0.01U 4U transient analysis with PWL input"),
                               ("previous_heading", "Example 3.6-3"),
                               ("next_heading", "3.7 Summary")]),
                  ["ExampleSolution"]),
    build_package("PKG-PROB", "C-PROB", "Chapter 3 > PROBLEMS",
                  OrderedDict([("previous_heading", "3.7 Summary"),
                               ("next_heading", "REFERENCES")]),
                  ["ProblemStatement"]),
]

# ---------------------------------------------------------------------------
# mentions (IDs preserved; atom_ids rebound to verbatim atoms).
# ---------------------------------------------------------------------------
MENTIONS = [
    OrderedDict([("id", "M-01"), ("text", "Sah equation"), ("type", "Formula"), ("atom_id", "A-LS-SAH-1"), ("canonical_key", "sah_drain_current")]),
    OrderedDict([("id", "M-02"), ("text", "channel length modulation"), ("type", "PhysicalEffect"), ("atom_id", "A-LS-CLM-EFFECT"), ("canonical_key", "channel_length_modulation")]),
    OrderedDict([("id", "M-03"), ("text", "lambda"), ("type", "Variable"), ("atom_id", "A-LS-SAT-CLM-18"), ("canonical_key", "lambda_clm")]),
    OrderedDict([("id", "M-04"), ("text", "K'"), ("type", "Variable"), ("atom_id", "A-LS-BETA-13"), ("canonical_key", "transconductance_parameter_kprime")]),
    OrderedDict([("id", "M-05"), ("text", "threshold voltage"), ("type", "Variable"), ("atom_id", "A-LS-VT-2"), ("canonical_key", "threshold_voltage_vt")]),
    OrderedDict([("id", "M-06"), ("text", "saturation voltage"), ("type", "Variable"), ("atom_id", "A-LS-VDSAT-15"), ("canonical_key", "saturation_voltage_vdssat")]),
    OrderedDict([("id", "M-07"), ("text", "g_m"), ("type", "Variable"), ("atom_id", "A-SS-GM-6"), ("canonical_key", "transconductance_gm")]),
    OrderedDict([("id", "M-08"), ("text", "g_ds"), ("type", "Variable"), ("atom_id", "A-SS-GDS-9"), ("canonical_key", "output_conductance_gds")]),
    OrderedDict([("id", "M-09"), ("text", "g_mbs"), ("type", "Variable"), ("atom_id", "A-SS-GMBS-8"), ("canonical_key", "bulk_transconductance_gmbs")]),
    OrderedDict([("id", "M-10"), ("text", "small-signal model"), ("type", "ComponentModel"), ("atom_id", "A-SS-INTRO"), ("canonical_key", "small_signal_model")]),
    OrderedDict([("id", "M-11"), ("text", "SPICE Level 3 model"), ("type", "ComponentModel"), ("atom_id", "A-CS-L3-DRAIN-1"), ("canonical_key", "spice_level3_model")]),
    OrderedDict([("id", "M-12"), ("text", "BSIM3v3"), ("type", "ComponentModel"), ("atom_id", "A-CS-BSIM-HISTORY"), ("canonical_key", "bsim3v3_model")]),
    OrderedDict([("id", "M-13"), ("text", "weak inversion"), ("type", "PhysicalEffect"), ("atom_id", "A-ST-CONCEPT"), ("canonical_key", "subthreshold_conduction")]),
    OrderedDict([("id", "M-14"), ("text", "depletion capacitance"), ("type", "Formula"), ("atom_id", "A-LS2-CBX-3"), ("canonical_key", "bulk_depletion_capacitance")]),
    OrderedDict([("id", "M-15"), ("text", "overlap capacitance"), ("type", "Formula"), ("atom_id", "A-LS2-COVERLAP-7"), ("canonical_key", "gate_overlap_capacitance")]),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "channel_length_modulation"),
                 ("aliases", ["channel length modulation", "channel-length modulation", "CLM", "lambda effect"])]),
    OrderedDict([("canonical", "transconductance_parameter_kprime"),
                 ("aliases", ["K'", "KP", "intrinsic transconductance parameter", "device transconductance parameter"]),
                 ("note", "the simple model uses K' (= mu_o C_ox); SPICE Level 1/3 uses uppercase KP; same physical quantity.")]),
    OrderedDict([("canonical", "subthreshold_conduction"),
                 ("aliases", ["subthreshold", "weak inversion", "weak-inversion region", "subthreshold conduction"])]),
    OrderedDict([("canonical", "sah_drain_current"),
                 ("aliases", ["Sah equation", "Shichman-Hodges model", "Level 1 drain current", "simple large-signal model"])]),
    OrderedDict([("canonical", "surface_potential_2phiF"),
                 ("aliases", ["2 phi_F", "2|phi_F|", "PHI", "surface potential at strong inversion"]),
                 ("note", "Sec 3.6 states PHI is the SPICE term for 2 phi_f and is always positive in SPICE.")]),
]

# ---------------------------------------------------------------------------
# relations (IDs preserved; evidence atom_ids rebound).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


RELATIONS = [
    rel("R-01", "formula_defines_variable", "FORMULA-SAH-DRAIN", "VAR-KPRIME", ["A-LS-SAH-1", "A-LS-BETA-13"], "medium", "direct"),
    rel("R-02", "formula_derived_from_formula", "FORMULA-ID-SAT", "FORMULA-ID-NONSAT", ["A-LS-NONSAT-16", "A-LS-SAT-17"], "medium", "indirect"),
    rel("R-03", "formula_derived_from_formula", "FORMULA-ID-SAT", "FORMULA-SAH-DRAIN", ["A-LS-SAH-1", "A-LS-SAT-17"], "hard", "indirect"),
    rel("R-04", "physical_effect_modifies_formula", "PHYS-CLM", "FORMULA-ID-SAT", ["A-LS-CLM-EFFECT", "A-LS-SAT-CLM-18"], "medium", "direct"),
    rel("R-05", "formula_defines_variable", "FORMULA-ID-SAT", "VAR-LAMBDA", ["A-LS-SAT-CLM-18", "A-LS-T312"], "medium", "direct"),
    rel("R-06", "formula_derived_from_formula", "FORMULA-VDSSAT", "FORMULA-SAH-DRAIN", ["A-LS-VDSAT-15", "A-LS-SAH-1"], "hard", "indirect"),
    rel("R-07", "derivation_produces_formula", "DERIV-REGIONS", "FORMULA-ID-SAT", ["A-LS-SAT-CLM-18"], "medium", "direct"),
    rel("R-08", "formula_used_in_example", "FORMULA-ID-SAT", "EXAMPLE-3.1-1", ["A-EX311-USE", "A-EX311-RESULT"], "medium", "direct"),
    rel("R-09", "formula_used_in_example", "FORMULA-VDSSAT", "EXAMPLE-3.1-1", ["A-EX311-USE"], "medium", "direct"),
    rel("R-10", "model_includes_formula", "COMP-SPICE-LEVEL1", "FORMULA-SAH-DRAIN", ["A-LS-FIVE-PARAMS", "A-LS-SAH-1"], "medium", "direct"),
    rel("R-11", "model_includes_component", "COMP-SPICE-LEVEL1", "COMP-DEPLETION-CAPS", ["A-LS2-CBX-3"], "hard", "indirect"),
    rel("R-12", "model_includes_component", "COMP-SPICE-LEVEL1", "COMP-CHARGE-STORAGE-CAPS", ["A-LS2-CGREGIONS"], "hard", "indirect"),
    rel("R-13", "component_has_property", "COMP-BULK-JUNCTIONS", "COMP-DEPLETION-CAPS", ["A-LS2-CBX-3"], "medium", "indirect"),
    rel("R-14", "component_has_property", "COMP-SMALL-SIGNAL-MODEL", "PHYS-NOISE", ["A-LS2-NOISE-12"], "hard", "indirect"),
    rel("R-15", "formula_derived_from_formula", "COMP-SMALL-SIGNAL-MODEL", "FORMULA-ID-SAT", ["A-SS-GM-6", "A-SS-GDS-9"], "medium", "direct"),
    rel("R-16", "formula_defines_variable", "COMP-SMALL-SIGNAL-MODEL", "VAR-LAMBDA", ["A-SS-GDS-9"], "medium", "indirect"),
    rel("R-17", "formula_used_in_example", "COMP-SMALL-SIGNAL-MODEL", "EXAMPLE-3.3-1", ["A-EX331-USE", "A-EX331-RESULT"], "medium", "direct"),
    rel("R-18", "formula_used_in_example", "COMP-SMALL-SIGNAL-MODEL", "EXAMPLE-3.3-2", ["A-EX332-RESULT"], "medium", "direct"),
    rel("R-19", "concept_defines_term", "CONCEPT-SMALL-SIGNAL", "COMP-SMALL-SIGNAL-MODEL", ["A-SS-INTRO"], "medium", "direct"),
    rel("R-20", "model_refines_model", "COMP-SPICE-LEVEL3", "COMP-SPICE-LEVEL1", ["A-CS-INTRO", "A-CS-MODEL-COMPARE"], "medium", "direct"),
    rel("R-21", "model_refines_model", "COMP-BSIM3V3", "COMP-SPICE-LEVEL3", ["A-CS-BSIM-HISTORY", "A-CS-MODEL-COMPARE"], "medium", "direct"),
    rel("R-22", "component_models_effect", "COMP-BSIM3V3", "PHYS-BSIM-EFFECTS", ["A-CS-BSIM-EFFECTS"], "medium", "direct"),
    rel("R-23", "physical_effect_modifies_formula", "PHYS-CLM", "COMP-SPICE-LEVEL3", ["A-CS-L3-CLM-20"], "hard", "indirect"),
    rel("R-24", "component_models_effect", "COMP-SPICE-LEVEL3", "PHYS-SUBTHRESHOLD", ["A-ST-VON-1", "A-ST-IDS-3"], "medium", "direct"),
    rel("R-25", "physical_effect_modifies_formula", "PHYS-SUBTHRESHOLD", "FORMULA-ID-SAT", ["A-ST-CONCEPT"], "hard", "indirect"),
    rel("R-26", "model_used_in_example", "COMP-SPICE-LEVEL1", "EXAMPLE-3.6-1", ["A-SP-NETLIST", "A-EX361-NETLIST"], "medium", "direct"),
    rel("R-27", "design_principle_applies_to_scenario", "PRINCIPLE-MULTIPLIER", "COMP-SPICE-LEVEL1", ["A-SP-MULT"], "hard", "indirect"),
    rel("R-28", "problem_extends_example", "PROB-11", "EXAMPLE-3.3-1", ["A-PROB-11"], "medium", "direct"),
    rel("R-29", "problem_extends_formula", "PROB-5", "FORMULA-ID-SAT", ["A-PROB-5"], "medium", "direct"),
    rel("R-30", "problem_extends_effect", "PROB-15", "PHYS-SUBTHRESHOLD", ["A-PROB-15"], "medium", "direct"),
]

# ---------------------------------------------------------------------------
# do_not_extract negatives.
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([("pattern", "bracket_numeric_citation"),
                 ("examples", ["[1]", "[2]", "[3]", "[23]", "[25]", "[26]", "[27]"]),
                 ("reason", "bracket [n] reference-list citations are not knowledge objects."),
                 ("kind", "citation_policy")]),
    OrderedDict([("text", "[2]"), ("atom_id", "A-LS-SAH-1"),
                 ("reason", "bracket citation to Sah's paper within the Sah-equation line; the equation is the object, not the citation."),
                 ("kind", "citation")]),
    OrderedDict([("pattern", "equation_cross_reference"),
                 ("examples", ["Eq. (28) of Sec. 2.3", "Eq. (19) of Sec. 2.3", "Eq. (6) of Sec. 2.2", "Eqs. (5) and (6) of Sec. 3.2"]),
                 ("reason", "Eq./Fig./Table cross-references point at numbered artifacts, not new knowledge objects."),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "described in Sec 2.3"), ("atom_id", "A-INTRO"),
                 ("reason", "cross-reference to Chapter 2 (outside this chapter slice)."),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("pattern", "spice_keyword_token"),
                 ("examples", ["NMOS", "PMOS", "LEVEL=1", ".DC", ".AC", ".TRAN", ".PRINT", ".MODEL", ".END", ".OP", "PWL"]),
                 ("reason", "SPICE deck keywords / control-card tokens are syntax, not knowledge objects (the model object COMP-SPICE-LEVEL1 is the knowledge, not the .MODEL keyword)."),
                 ("kind", "citation_policy")]),
    OrderedDict([("text", "(a)"), ("source_lines", [3217, 3217]),
                 ("reason", "figure-internal sub-label for Fig. 3.1-1(a)."),
                 ("kind", "figure_label")]),
    OrderedDict([("text", "Figure 3.1-1 Positive sign convention for (a) n-channel, and (b) p-channel MOS transistor."),
                 ("source_lines", [3228, 3228]),
                 ("reason", "figure caption rendered as plain text; the MOSFET symbol figure is not a chapter knowledge object."),
                 ("kind", "figure_label")]),
    OrderedDict([("text", "Example 3.6-2 dc Analysis of Fig. 3.6-4."), ("source_lines", [4512, 4512]),
                 ("reason", "Example 3.6-2 (dc transfer) is present in the slice but not part of the curated example set; listed to suppress spurious extraction."),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "Chapter 2"), ("atom_id", "A-LS2-CBX-3"),
                 ("reason", "PB 'same as phi_o given in Eq.(6), sec. 2.2' is a cross-chapter reference, not a Chapter 3 object."),
                 ("kind", "out_of_slice_reference")]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "cmos"),
    ("scope", "Chapter 3 (CMOS Device Modeling) only"),
    ("title", "CMOS Analog Circuit Design - Allen & Holberg - Chapter 3 CMOS Device Modeling"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 3189-4872; NOT the authoritative source)"),
    ("profile", "textbook"),
    ("profile_detected_as", "textbook"),
    ("profile_cues", ["Chapter 3", "Example 3.1-1", "Example 3.3-1", "Example 3.6-1", "PROBLEMS",
                      "Eq. (18)", "Fig. 3.1-2", "Table 3.1-2", "SPICE LEVEL 1", "BSIM3v3"]),
    ("extraction_targets", ["Concept", "Definition", "Formula", "Variable", "Derivation",
                            "ExampleProblem", "ExampleSolution", "ComponentModel", "PhysicalEffect",
                            "DesignPrinciple", "ProblemStatement"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span (may rephrase, transliterate LaTeX math like mu/gamma/phi/lambda/eps, resolve in-span pronouns) but MUST NOT introduce facts/names/numbers outside the span. MinerU spaces digits ('1 1 0', '0. 7', '5 2 0') and uses LaTeX in $...$ / $$...$$; normalized gives the readable ascii form. EXCEPTION: a formula_atom / condition_atom may carry symbolic interpretation via metadata.supported_by_context_atoms, and its normalized renders only the formula (no surrounding gloss; semantics live in the object payload)."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside its home_package's chunk) + supporting_context_atom_ids (atoms from other chunks). Package object-recall (expected_objects) is scored on local evidence only."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = payload fields derivable from that package alone. VAR-LAMBDA and COMP-SPICE-LEVEL1 (home PKG-LS-REGIONS) draw supporting evidence from Table 3.1-2 / the Sah equation in PKG-LS-MODEL; PKG-EX-361 can identify COMP-SPICE-LEVEL1 by name from the LEVEL 1 netlist."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of a decimal confidence."),
        ("tables_and_figures", "Tables render as HTML <table> lines (Table 3.1-1/3.1-2/3.2-1/3.3-1/3.3-2/3.4-1). MOSFET/BJT symbol figures render as <details><summary>chemical</summary>; waveforms/curves as <details><summary>line</summary>; the overlap-capacitance profile as a mermaid flowchart. Figure-internal labels, bracket [n] citations, and SPICE deck keywords are listed in do_not_extract."),
        ("context_only", "an atom with context_only:true is background/framing (chapter intro A-INTRO, chapter summary A-SUMMARY) that should NOT yield a typed object; its home package (PKG-OV) legitimately lists no expected_object."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects are the objects whose home_package is that package."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
        ("numeric_over_span", "for every table_row_atom and result_atom: each number appearing in normalized_text must occur verbatim in raw_text (after removing MinerU digit-spacing), guarding against cross-row / cross-example numeric leakage."),
        ("formula_normalized_no_gloss", "for every formula_atom / condition_atom: normalized_text is the formula rendering only; descriptive semantics belong to the object payload, not the atom."),
    ])),
    ("parsing_notes", [
        "Formula tags reset per sub-section: 3.1 has tag1..20, 3.2 restarts at tag1 (with split sub-equations 9a/9b/9c, 10a/10b/10c, 11a/11b/11c), 3.3 restarts (Eqs 1..9, then nonsat 10/11/12 between the two examples), SPICE Level 3 restarts (tag1..22, then a second temperature block tag15..24), 3.5 restarts (tag1..6), Example 3.6 SPICE-parameter prose restarts (tag1..4). A formula atom is located by tag WITHIN its sub-section's line window (build.py by_tag).",
        "MinerU spaces out digits inside math ('1 1 0', '0. 7', '5 2 0 \\mu A'); applied voltages print with a U+2212 minus ('−3 V'); temperatures use 'K'; KOhm prints as 'K\\Omega'. normalized_text gives the ascii reading.",
        "Eqs 9/10/11 (per-region charge-storage capacitances) render as nine separate single-line sub-equations (lines 3714-3764); A-LS2-CGREGIONS anchors the introducing prose line and carries the region allocation in the object payload (the formula content is not a single contiguous line).",
        "SPICE Level 3 uses uppercase parameter names (BETA/KP/GAMMA/PHI/COX/U0/NSUB/KAPPA/THETA/ETA/DELTA/NFS) for the same physical quantities the simple model writes lowercase (mu_o, C_ox, 2 phi_F); canonicalization aligns K'~KP and 2|phi_F|~PHI.",
        "BSIM3v3 has no equations in the text, only a parameter-count lineage (BSIM1 60 / BSIM2 99 / BSIM3 40) and an 8-item deep-submicron effect list; modeled as a ComponentModel + a PhysicalEffect, linked to Level 3 by model_refines_model.",
        "Examples 3.6-1/-2/-3/-4 share the same Fig. 3.6-4 amplifier; their bodies are SPICE netlists in ```batch / ```txt code blocks plus simulation curves (whose <details><summary>line</summary> table values are approximate placeholders, not exact results). Example 3.6-2 (dc transfer) is present but excluded from the curated example set and listed in do_not_extract.",
        "The PHI/SPICE-naming definition (A-CS-L3-PHI-NOTE) physically sits in the 3.6 SPICE-parameter prose (line 4417) but conceptually defines the Level 3 / SPICE naming, so it is filed under SEC-3.4-L3.",
    ]),
])

# ---------------------------------------------------------------------------
# Assemble and dump.
# ---------------------------------------------------------------------------
GOLD = OrderedDict([
    ("schema_version", "0.3.3-textbook"),
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
        "# Gold fixture v0.3.3-textbook - CMOS Analog Circuit Design, Chapter 3 CMOS Device Modeling (profile: textbook)\n"
        "# Upgraded from the v0.1 fixture to the engram-ch2 v0.3.3 process (textbook TYPES retained):\n"
        "# dual coordinates (authoritative source_span into the mineru .md + viewer_span into source.md);\n"
        "# verbatim raw_text + within-span normalized_text (formula/condition normalized = formula only);\n"
        "# source_elements; gold_label/difficulty/evidence_strength (no decimal confidence);\n"
        "# objects with local_evidence/supporting_context/home_package; one package per chunk with\n"
        "# expected_objects (+expected_local_fields for cross-package VAR-LAMBDA/COMP-SPICE-LEVEL1);\n"
        "# do_not_extract negatives. Per-sub-section formula tag windows resolve the reset tag numbering.\n"
        "# Spans are computed by build.py; do NOT hand-edit.\n"
    )
    body = yaml.dump(GOLD, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=100000)
    with open(os.path.join(HERE, "gold.yaml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    print("wrote source.md (%d bytes) and gold.yaml" % len(VIEWER_TEXT))
    print("counts: atoms=%d source_elements=%d chunks=%d packages=%d objects=%d relations=%d mentions=%d do_not_extract=%d" % (
        len(ATOMS), len(SOURCE_ELEMENTS), len(SEMANTIC_CHUNKS), len(CONTEXT_PACKAGES),
        len(OBJECTS), len(RELATIONS), len(MENTIONS), len(DO_NOT_EXTRACT)))


if __name__ == "__main__":
    main()
