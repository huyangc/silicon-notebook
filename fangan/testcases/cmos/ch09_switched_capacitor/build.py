#!/usr/bin/env python3
"""Builder for ch09_switched_capacitor gold.yaml (schema v0.3.3-textbook, profile textbook).

Upgrades the v0.1 fixture (CMOS Analog Circuit Design, Chapter 9 Switched
Capacitor Circuits, source lines 7496-13705) to v0.3.3-textbook: re-expresses the
SAME ~69 atoms / chunks / objects / relations (IDs and meaning preserved from the
v0.1 gold) in the v0.3.3 schema and adds verbatim dual coordinate spans.

All spans are computed by locating verbatim substrings (anchors) in the
authoritative MinerU source file; YAML spans are never hand-written. The viewer
file source.md is a verbatim slice of source_file lines 7496-13705 (viewer-only).

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = ("/Users/hzf/workspace/pdf_parser/notebook_papers_mineru_skill_results/"
               "CMOS_Analog_Circuit_Design_-_Allen_Holberg/"
               "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md")
SOURCE_NAME = "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md"
VIEWER_NAME = "source.md"

SLICE_LINE_START = 7496
SLICE_LINE_END = 13705

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
# Viewer file source.md : verbatim slice of lines + header comment.
# ---------------------------------------------------------------------------
VIEWER_HEADER = (
    "<!-- VIEWER-ONLY verbatim slice of {src} lines {a}-{b}. "
    "NOT authoritative; all gold coordinates point at {src}. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY

_HEADER_LEN = len(VIEWER_HEADER)
_VIEWER_LINE_OFF = {}
_vacc = _HEADER_LEN
_vbody_lines = VIEWER_BODY.split("\n")
for k, _ln in enumerate(_vbody_lines):
    src_lineno = SLICE_LINE_START + k
    _VIEWER_LINE_OFF[src_lineno] = _vacc
    _vacc += len(_ln) + 1


def viewer_lineno(src_lineno):
    return 1 + (src_lineno - SLICE_LINE_START) + 1


# ---------------------------------------------------------------------------
# Span helper: locate a verbatim contiguous substring within the slice.
# `raw` must occur exactly once in the slice.
# ---------------------------------------------------------------------------
SLICE_START_CHAR = _LINE_OFF[SLICE_LINE_START]
SLICE_END_CHAR = _LINE_OFF[SLICE_LINE_END] + len(SRC_LINES[SLICE_LINE_END - 1])
SLICE_SRC = SRC[SLICE_START_CHAR:SLICE_END_CHAR]


def _line_of_char(src_char):
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
        raise SystemExit("NOT FOUND in slice: %r" % raw[:80])
    if SLICE_SRC.find(raw, rel + 1) >= 0:
        raise SystemExit("AMBIGUOUS (matches >1): %r" % raw[:80])
    src_cs = SLICE_START_CHAR + rel
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch"

    src_ls = _line_of_char(src_cs)
    src_le = _line_of_char(src_ce - 1)

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


def atom(aid, section_id, atom_type, se_id, raw, normalized, evidence_strength,
         metadata=None, context_only=False):
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
    if context_only:
        d["context_only"] = True
    return d


def meta(**kw):
    return OrderedDict(kw.items())


# ===========================================================================
# Evidence atoms. raw_text = verbatim contiguous spans of the slice.
# normalized_text renders EXACTLY that span (ascii z^-1, omega, alpha, ...; no
# out-of-span facts). FORMULA/CONDITION atoms: normalized is formula-only (no gloss).
# ===========================================================================
ATOMS = [
    # ---------- 9.0 chapter intro (context_only) ----------
    atom(
        "A-INTRO", "SEC-9", "concept_definition_atom", "SE-9-INTRO",
        "These circuits are called switched capacitor circuits and have become a popular method of implementing analog signal processing circuits in standard CMOS technologies.",
        "These circuits are called switched capacitor circuits and have become a popular method of implementing analog signal processing circuits in standard CMOS technologies.",
        "direct", context_only=True),
    atom(
        "A-INTRO-ACCURACY", "SEC-9", "design_principle_atom", "SE-9-INTRO-ACC",
        "One the important reasons for the success of switched capacitor circuits is that the accuracy of the signal processing function is proportional to the accuracy of capacitor ratios.",
        "One important reason for the success of switched capacitor circuits is that the accuracy of the signal processing function is proportional to the accuracy of capacitor ratios.",
        "direct"),

    # ---------- 9.1 Resistor Emulation ----------
    atom(
        "A-SC-RES-CONCEPT", "SEC-9.1", "concept_definition_atom", "SE-9.1-RES-CONCEPT",
        "The parallel switched capacitor equivalent resistor circuit in Fig. 9.1-1(a) consists two independent voltage sources, $\\nu _ { 1 } ( t )$ and $\\nu _ { 2 } ( t )$ , two controlled switches, $S _ { 1 }$ and $\\mathbf { S } _ { 2 }$ and a capacitor, C.",
        "The parallel switched capacitor equivalent resistor circuit (Fig. 9.1-1a) consists of two independent voltage sources v_1(t) and v_2(t), two controlled switches S_1 and S_2, and a capacitor C.",
        "direct"),
    atom(
        "A-SC-NONOVERLAP", "SEC-9.1", "concept_definition_atom", "SE-9.1-NONOVERLAP",
        "Note, that $\\phi _ { 1 }$ and $\\phi _ { 2 }$ never have the value of 1 at the same time. This type of clock is called a nonoverlapping clock. The period of the clock waveforms in Fig. 9.1-2 is T. The width of each individual clock is slightly less than T/2.",
        "phi_1 and phi_2 never have the value of 1 at the same time; this type of clock is called a nonoverlapping clock. The period of the clock waveforms is T and the width of each individual clock is slightly less than T/2.",
        "direct"),
    atom(
        "A-SC-IAVG-6", "SEC-9.1", "formula_atom", "SE-9.1-EQ6",
        "i _ {1} (\\text { average }) = \\frac {C [ v _ {C} (T / 2) - v _ {C} (0) ]}{T}. \\tag {6}",
        "i_1(average) = C[v_C(T/2) - v_C(0)] / T",
        "direct", metadata=meta(tag="6", role="avg_current_into_C")),
    atom(
        "A-SC-IAVG-10", "SEC-9.1", "formula_atom", "SE-9.1-EQ10",
        "i _ {1} (\\text { average }) = \\frac {C (V _ {1} - V _ {2})}{T}. \\tag {10}",
        "i_1(average) = C(V_1 - V_2) / T",
        "direct", metadata=meta(tag="10",
            note="assuming v_1,v_2 nearly constant over the clock period T")),
    atom(
        "A-SC-REQ-12", "SEC-9.1", "formula_atom", "SE-9.1-EQ12",
        "\\mathrm{R} = \\frac {\\mathrm{T}}{\\mathrm{C}}. \\tag {12}",
        "R = T/C",
        "direct", metadata=meta(tag="12", role="equivalent_resistance",
            note="equivalent resistance of the parallel switched-capacitor resistor; R = 1/(f_c C); valid when changes in v_1,v_2 are negligible over period T")),
    atom(
        "A-SC-REQ-SP-17", "SEC-9.1", "formula_atom", "SE-9.1-EQ17",
        "R = \\frac {T}{C _ {1} + C _ {2}}. \\tag {17}",
        "R = T/(C_1 + C_2)",
        "direct", metadata=meta(tag="17", role="equivalent_resistance_seriesparallel")),
    atom(
        "A-SC-TABLE-911", "SEC-9.1", "table_row_atom", "SE-9.1-TABLE",
        "Table 9.1-1 summarizes the equivalent resistance of each of the four switched capacitor resistor emulation circuits that we have considered. It is significant to note that in each case, the emulated resistance is proportional to the reciprocal of the capacitance.",
        "Table 9.1-1 summarizes the equivalent resistance of each of the four switched capacitor resistor emulation circuits; in each case the emulated resistance is proportional to the reciprocal of the capacitance.",
        "direct", metadata=meta(table_id="Table 9.1-1")),
    atom(
        "A-SC-ACC-TAUC-20", "SEC-9.1", "formula_atom", "SE-9.1-EQ20",
        "\\frac {d \\tau_ {C}}{\\tau_ {C}} = \\frac {d R _ {1}}{R _ {1}} + \\frac {d C _ {2}}{C _ {2}}. \\tag {20}",
        "d(tau_C)/tau_C = dR_1/R_1 + dC_2/C_2",
        "direct", metadata=meta(tag="20", role="ct_time_constant_accuracy",
            note="continuous-time RC time-constant accuracy = sum of R and C accuracy; 5% to 20% in CMOS")),
    atom(
        "A-SC-ACC-TAUD-22", "SEC-9.1", "formula_atom", "SE-9.1-EQ22",
        "\\frac {d \\tau_ {D}}{\\tau_ {D}} = \\frac {d C _ {2}}{C _ {2}} - \\frac {d C _ {1}}{C _ {1}} - \\frac {d f _ {c}}{f _ {c}}. \\tag {22}",
        "d(tau_D)/tau_D = dC_2/C_2 - dC_1/C_1 - df_c/f_c",
        "direct", metadata=meta(tag="22", role="sc_time_constant_accuracy",
            note="discrete-time SC time-constant accuracy = relative accuracy of C_1,C_2 and clock; as small as 0.1% in CMOS")),
    atom(
        "A-SC-ZTRANS-25", "SEC-9.1", "formula_atom", "SE-9.1-EQ25",
        "V (z) = \\sum_ {n = 0} ^ {\\infty} v (n T) z ^ {- n} = v (0) + v (T) z ^ {- 1} + v (2 T) z ^ {- 2} + \\dots \\tag {25}",
        "V(z) = sum_{n=0..inf} v(nT) z^-n = v(0) + v(T) z^-1 + v(2T) z^-2 + ...",
        "direct", metadata=meta(tag="25", role="z_transform")),
    atom(
        "A-SC-LPF-HZ-37", "SEC-9.1", "formula_atom", "SE-9.1-EQ37",
        "H ^ {o o} (z) = \\frac {V _ {2} ^ {o} (z)}{V _ {1} ^ {o} (z)} = \\frac {z ^ {- 1} \\left(\\frac {C _ {1}}{C _ {1} + C _ {2}}\\right)}{1 - z ^ {- 1} \\left(\\frac {C _ {2}}{C _ {1} + C _ {2}}\\right)} = \\frac {z ^ {- 1}}{1 + \\alpha - \\alpha z ^ {- 1}} \\tag {37}",
        "H^oo(z) = V_2^o(z)/V_1^o(z) = z^-1 (C_1/(C_1+C_2)) / (1 - z^-1 (C_2/(C_1+C_2))) = z^-1 / (1 + alpha - alpha z^-1)",
        "direct", metadata=meta(tag="37", role="sc_lowpass_transfer_function",
            note="alpha = C_2/C_1 (Eq.38); z-domain transfer function of the SC first-order lowpass of Fig. 9.1-7a")),
    # Example 9.1-1
    atom(
        "A-EX911-PROBLEM", "SEC-EX911", "example_problem_atom", "SE-EX911-P",
        "If the clock frequency of Fig. 9.1-1(a) is 100kHz, find the value of the capacitor C that will emulate a 1MΩ resistor.",
        "If the clock frequency of Fig. 9.1-1a is 100 kHz, find the value of the capacitor C that will emulate a 1 MOhm resistor.",
        "direct"),
    atom(
        "A-EX911-USE", "SEC-EX911", "formula_usage_atom", "SE-EX911-USE",
        "The period of a 100kHz clock waveform is 10µsec. Therefore, using Eq. (12) we get that",
        "The period of a 100 kHz clock waveform is 10 usec; therefore, using Eq.(12) R = T/C.",
        "direct"),
    atom(
        "A-EX911-RESULT", "SEC-EX911", "result_atom", "SE-EX911-R",
        "C = \\frac {T}{R} = \\frac {1 0 ^ {- 5}}{1 0 ^ {6}} = 1 0 p F",
        "C = T/R = 10^-5 / 10^6 = 10 pF",
        "direct"),
    # Example 9.1-2
    atom(
        "A-EX912-PROBLEM", "SEC-EX912", "example_problem_atom", "SE-EX912-P",
        "If C1 = C2 = C, find the value of C that will emulate a 1MΩ resistor if the clock frequency is 250kHz.",
        "If C1 = C2 = C, find the value of C that will emulate a 1 MOhm resistor if the clock frequency is 250 kHz.",
        "direct"),
    atom(
        "A-EX912-RESULT", "SEC-EX912", "result_atom", "SE-EX912-R",
        "2 \\mathrm{C} = \\frac {\\mathrm{T}}{\\mathrm{R}} = \\frac {4 \\mathrm{x} 1 0 ^ {- 6}}{1 0 6} = 4 \\mathrm{pF}",
        "2C = T/R = 4 x 10^-6 / 10^6 = 4 pF  (so C1 = C2 = C = 2 pF)",
        "direct"),
    # Example 9.1-3
    atom(
        "A-EX913-PROBLEM", "SEC-EX913", "example_problem_atom", "SE-EX913-P",
        "Use the above approach to find the z-domain transfer function of the first-order, lowpass switched capacitor circuit shown in Fig. 9.1-7a. This circuit was developed by replacing the resistor, $R _ { 1 } ,$ of Fig. 9.1-4 with the parallel switched capacitor resistor circuit of Table 9.1-1.",
        "Find the z-domain transfer function of the first-order lowpass SC circuit of Fig. 9.1-7a, developed by replacing the resistor R_1 of Fig. 9.1-4 with the parallel switched capacitor resistor circuit of Table 9.1-1.",
        "direct"),
    atom(
        "A-EX913-RESULT", "SEC-EX913", "result_atom", "SE-EX913-R",
        "v _ {2} ^ {o} (n T) = \\left(\\frac {C _ {1}}{C _ {1} + C _ {2}}\\right) v _ {1} ^ {o} (n - 1) T + \\left(\\frac {C _ {2}}{C _ {1} + C _ {2}}\\right) v _ {2} ^ {o} (n - 1) T. \\tag {32}",
        "v_2^o(nT) = (C_1/(C_1+C_2)) v_1^o((n-1)T) + (C_2/(C_1+C_2)) v_2^o((n-1)T)  -> H^oo(z) = z^-1/(1 + alpha - alpha z^-1), alpha = C_2/C_1",
        "direct", metadata=meta(tag="32")),

    # ---------- 9.2 Amplifiers ----------
    atom(
        "A-AMP-CT-12", "SEC-9.2", "formula_atom", "SE-9.2-EQ1",
        "\\frac {v _ {O U T}}{v _ {I N}} = \\frac {R _ {1} + R _ {2}}{R _ {1}} \\tag {1}",
        "v_OUT/v_IN = (R_1 + R_2)/R_1   (noninverting continuous-time amplifier gain, Av -> inf)",
        "direct", metadata=meta(tag="1", role="ct_amp_gain",
            note="inverting counterpart v_OUT/v_IN = -R_2/R_1 (Eq.2)"), context_only=True),
    atom(
        "A-AMP-AVD-3", "SEC-9.2", "formula_atom", "SE-9.2-EQ3",
        "A _ {v d} (s) = \\frac {A _ {v d} (0) \\omega_ {a}}{s + \\omega_ {a}} = \\frac {G B}{s + \\omega_ {a}} \\approx \\frac {G B}{s} \\quad \\text { if } \\omega > > \\omega_ {a} \\tag {3}",
        "A_vd(s) = A_vd(0) omega_a / (s + omega_a) = GB / (s + omega_a) ~= GB/s  if omega >> omega_a",
        "direct", metadata=meta(tag="3", role="opamp_gain_model",
            note="A_vd(0) low-frequency gain, GB unity-gainbandwidth"), context_only=True),
    atom(
        "A-AMP-FINITE-6", "SEC-9.2", "formula_atom", "SE-9.2-EQ6",
        "\\frac {V _ {\\text {out}}}{V _ {\\text {in}}} = \\frac {\\frac {- R _ {2} A _ {v d} (0)}{R _ {1} + R _ {2}}}{1 + \\frac {A _ {v d} (0) R _ {1}}{R _ {1} + R _ {2}}} = - \\left(\\frac {R _ {2}}{R _ {1}}\\right) \\frac {\\frac {R _ {1} A _ {v d} (0)}{R _ {1} + R _ {2}}}{1 + \\frac {A _ {v d} (0) R _ {1}}{R _ {1} + R _ {2}}} = - \\left(\\frac {R _ {2}}{R _ {1}}\\right) \\frac {L G}{1 + L G}. \\tag {6}",
        "V_out/V_in = -(R_2/R_1) LG/(1+LG),  LG = A_vd(0) R_1/(R_1+R_2)  (finite-gain inverting amplifier; approaches ideal -R_2/R_1 for large LG)",
        "direct", metadata=meta(tag="6", role="finite_gain_amp"), context_only=True),
    atom(
        "A-CHARGE-AMP-12", "SEC-9.2", "formula_atom", "SE-9.2-EQ12",
        "\\frac {V _ {\\text { out }}}{V _ {\\text { in }}} = - \\left(\\frac {C _ {1}}{C _ {2}}\\right) \\frac {L G}{1 + L G}, \\tag {12}",
        "V_out/V_in = -(C_1/C_2) LG/(1+LG)   (inverting charge amplifier; resistors replaced by capacitors, LG = A_vd(0) C_2/(C_1+C_2))",
        "direct", metadata=meta(tag="12", role="charge_amp_gain"), context_only=True),
    atom(
        "A-CHARGE-AMP-USE", "SEC-9.2", "concept_definition_atom", "SE-9.2-CHARGE-USE",
        "It turns out in switched capacitor circuits that the voltages are redefined at least once every clock cycle. Therefore, charge amplifiers find use in switched capacitor circuits as voltage amplifiers.",
        "In switched capacitor circuits the voltages are redefined at least once every clock cycle, so leakage has no effect; therefore charge amplifiers find use in switched capacitor circuits as voltage amplifiers.",
        "direct", context_only=True),
    atom(
        "A-SCAMP-EVOLVE", "SEC-9.2", "concept_definition_atom", "SE-9.2-EVOLVE",
        "Figure 9.2-4 shows the evolution of an inverting switched capacitor amplifier. The resistors of the inverting voltage amplifier of Fig. 9.2-1b are replaced by the parallel switched capacitor resistor emulation of Fig. 9.1-1 or of Table 9.1-1 resulting in Fig. 9.2- 4a.",
        "The inverting switched capacitor amplifier (Fig. 9.2-4) is obtained by replacing the resistors of the continuous-time inverting voltage amplifier of Fig. 9.2-1b with the parallel switched capacitor resistor emulation of Fig. 9.1-1 / Table 9.1-1.",
        "direct"),
    atom(
        "A-SCAMP-HZ-19", "SEC-9.2", "formula_atom", "SE-9.2-EQ19",
        "H ^ {o e} (z) = \\frac {V _ {\\text { out }} ^ {e} (z)}{V _ {\\text { in }} ^ {o} (z)} = - \\left(\\frac {C _ {1}}{C _ {2}}\\right) z ^ {- 1 / 2}. \\tag {19}",
        "H^oe(z) = V_out^e(z)/V_in^o(z) = -(C_1/C_2) z^-1/2   (SC inverting amplifier; H^ee(z) = -(C_1/C_2) z^-1, Eq.23; gain magnitude = C_1/C_2 set by a capacitor ratio)",
        "direct", metadata=meta(tag="19", role="sc_amp_gain")),
    atom(
        "A-SCAMP-TRANSR-28", "SEC-9.2", "formula_atom", "SE-9.2-EQ28",
        "R _ {T} = \\frac {v _ {1} (t)}{i _ {2} (t)} = \\frac {v _ {1}}{i _ {2} (\\text { average })}. \\tag {28}",
        "R_T = v_1(t)/i_2(t) = v_1/i_2(average)   (transresistance of Fig. 9.2-6; Fig. 9.2-6b gives R_T = -T/C, Fig. 9.2-6a gives R_T = +T/C, valid only when f_c >> f)",
        "direct", metadata=meta(tag="28", role="transresistance")),
    atom(
        "A-SCAMP-PARASITIC", "SEC-9.2", "design_principle_atom", "SE-9.2-PARASITIC",
        "Switched capacitor circuits have been developed that are insensitive to the capacitor parasitics [7]. Figure 9.2-6a and 9.2-6b show a positive and negative, switched capacitor transresistor equivalent circuit that are independent of the capacitor parasitics.",
        "Switched capacitor circuits insensitive to capacitor parasitics have been developed: Fig. 9.2-6a and 9.2-6b show positive and negative switched capacitor transresistor equivalent circuits that are independent of the capacitor parasitics.",
        "direct"),
    atom(
        "A-SCAMP-NOEXCESS", "SEC-9.2", "design_principle_atom", "SE-9.2-NOEXCESS",
        "Compared with Fig. 9.2-4b which is characterized by Eqs. (19) or (23), Fig. 9.2-7b has the same magnitude response but has no excess phase delay.",
        "Compared with Fig. 9.2-4b (characterized by Eqs.19/23), the parasitic-insensitive inverting amplifier Fig. 9.2-7b has the same magnitude response but no excess phase delay.",
        "direct"),
    atom(
        "A-SCAMP-FEEDTHRU-50", "SEC-9.2", "formula_atom", "SE-9.2-EQ50",
        "v _ {o u t} \\approx \\frac {C _ {1}}{C _ {2}} \\left(v _ {i n} - \\frac {C _ {O L}}{2 C _ {1}} v _ {i n}\\right) + \\left(\\frac {C _ {O L}}{C _ {2}}\\right) V _ {T} = \\left(\\frac {C _ {1}}{C _ {2}}\\right) v _ {i n} - \\left(\\frac {C _ {O L}}{2 C _ {2}}\\right) v _ {i n} + \\left(\\frac {C _ {O L}}{C _ {2}}\\right) V _ {T}. \\tag {50}",
        "v_out ~= (C_1/C_2) v_in - (C_OL/2C_2) v_in + (C_OL/C_2) V_T   (clock-feedthrough output: ideal term + input-dependent distortion term + constant offset term; C_OL = MOSFET overlap capacitance, V_T = switch threshold)",
        "direct", metadata=meta(tag="50", role="clock_feedthrough")),
    atom(
        "A-SCAMP-FEEDTHRU-FIX", "SEC-9.2", "design_principle_atom", "SE-9.2-FIX",
        "The input independent or constant term can be eliminated using the concept of autozeroing, discussed in Chap. 8. The most annoying term is the one that is proportional to the input because this introduces distortion into the signal being processed by the switched capacitor circuit. We will show in the next section, how to eliminate the input dependent term of Eq. (50) by modifying the clocks slightly.",
        "The input-independent (constant) feedthrough term can be eliminated by autozeroing; the input-dependent (distortion) term, which is the most annoying, can be eliminated by modifying/delaying the clocks slightly.",
        "direct"),
    atom(
        "A-SCAMP-AVD-52", "SEC-9.2", "formula_atom", "SE-9.2-EQ52",
        "H ^ {o e} (z) = \\frac {V _ {\\text { out }} ^ {e} (z)}{V _ {\\text { in }} ^ {o} (z)} = \\left(\\frac {C _ {1}}{C _ {2}}\\right) z ^ {- 1 / 2} \\left[ \\frac {1}{1 + \\frac {C _ {1} + C _ {2}}{A _ {v d} (0) C _ {2}}} \\right]. \\tag {52}",
        "H^oe(z) = V_out^e(z)/V_in^o(z) = (C_1/C_2) z^-1/2 [ 1 / (1 + (C_1+C_2)/(A_vd(0) C_2)) ]   (finite op-amp gain affects the magnitude response only, not phase)",
        "direct", metadata=meta(tag="52", role="finite_gain_sc_amp")),
    # Example 9.2-1
    atom(
        "A-EX921-PROBLEM", "SEC-EX921", "example_problem_atom", "SE-EX921-P",
        "Assume that the voltage amplifiers of Fig. 9.2-1 have been designed for a voltage gain of +10 and -10. If $A _ { \\nu d } ( 0 )$ is 1000, find the actual voltage gains for each amplifier.",
        "Voltage amplifiers of Fig. 9.2-1 designed for gains +10 and -10 with A_vd(0) = 1000; find the actual voltage gains.",
        "direct"),
    atom(
        "A-EX921-RESULT", "SEC-EX921", "result_atom", "SE-EX921-R",
        "From Eq. (4), the actual gain is $1 0 ( 1 0 0 / 1 0 1 ) = 9 . 9 0 1$ rather than 10. For the inverting amplifier, the ratio of ${ R _ { 2 } } / { R _ { 1 } }$ is 10. In this case, the feedback loop gain is $L G = 1 0 0 0 / ( 1 { + } 1 0 ) = 9 0 . 9 0 9$ . Substituting this value in Eq. (6) gives an actual gain of -9.891 rather than -10.",
        "Noninverting: actual gain 10(100/101) = 9.901. Inverting: R_2/R_1 = 10, LG = 1000/(1+10) = 90.909, actual gain -9.891 (from Eq.6).",
        "direct"),
    # Example 9.2-4
    atom(
        "A-EX924-PROBLEM", "SEC-EX924", "example_problem_atom", "SE-EX924-P",
        "For the noninverting, voltage amplifier of Fig. 9.2-9, find the ideal output voltage and the input dependent and independent terms due to feedthrough if $C _ { 1 } =$ 10pF, $C _ { 2 } = 1 \\mathrm { p F }$ , and $C _ { O L } = 1 0 0 \\mathrm { f F }$ . Assume that ${ \\nu _ { i n } } = 0 . 1 \\mathrm { V }$ and $V _ { T } = 1 \\mathrm { V }$ .",
        "For the noninverting voltage amplifier of Fig. 9.2-9, find the ideal output and the input-dependent and -independent feedthrough terms with C_1 = 10 pF, C_2 = 1 pF, C_OL = 100 fF, v_in = 0.1 V, V_T = 1 V.",
        "direct"),
    atom(
        "A-EX924-RESULT", "SEC-EX924", "result_atom", "SE-EX924-R",
        "v _ {o u t} = 1 0 v _ {i n} - 0. 0 5 v _ {i n} - 0. 1 V = 1 V - 0. 0 0 5 V + 0. 1 V.",
        "v_out = 10 v_in - 0.05 v_in - 0.1 V = 1 V - 0.005 V + 0.1 V",
        "direct"),

    # ---------- 9.3 Integrators ----------
    atom(
        "A-INT-ROLE", "SEC-9.3", "design_principle_atom", "SE-9.3-ROLE",
        "The switched capacitor integrator is a key building block in analog signal processing circuits. All filter design can be reduced to noninverting and inverting integrators.",
        "The switched capacitor integrator is a key building block in analog signal processing; all filter design can be reduced to noninverting and inverting integrators.",
        "direct"),
    atom(
        "A-INT-CT-1", "SEC-9.3", "formula_atom", "SE-9.3-EQ1",
        "\\frac {V _ {\\text {out}} (j \\omega)}{V _ {\\text {in}} (j \\omega)} = \\frac {1}{j \\omega R _ {1} C _ {2}} = \\frac {\\omega_ {I}}{j \\omega} = \\frac {- j \\omega_ {I}}{\\omega} \\tag {1}",
        "V_out(jw)/V_in(jw) = 1/(jw R_1 C_2) = omega_I/(jw) = -j omega_I/omega   (continuous-time noninverting integrator; inverting counterpart is the negative, Eq.2; omega_I is the integrator frequency)",
        "direct", metadata=meta(tag="1", role="ct_integrator")),
    atom(
        "A-INT-NONINV-16", "SEC-9.3", "formula_atom", "SE-9.3-EQ16",
        "H ^ {o o} (z) = \\frac {V _ {\\text { out }} ^ {o} (z)}{V _ {\\text { in }} ^ {o} (z)} = \\left(\\frac {C _ {1}}{C _ {2}}\\right) \\frac {z ^ {- 1}}{1 - z ^ {- 1}} = \\left(\\frac {C _ {1}}{C _ {2}}\\right) \\frac {1}{z - 1}. \\tag {16}",
        "H^oo(z) = V_out^o(z)/V_in^o(z) = (C_1/C_2) z^-1/(1 - z^-1) = (C_1/C_2) 1/(z - 1)   (noninverting SC integrator, Fig. 9.3-4a)",
        "direct", metadata=meta(tag="16", role="noninverting_integrator_Hz")),
    atom(
        "A-INT-INV-24", "SEC-9.3", "formula_atom", "SE-9.3-EQ24",
        "H ^ {e e} (z) = \\frac {V _ {\\text { out }} ^ {e} (z)}{V _ {\\text { in }} ^ {e} (z)} = - \\left(\\frac {C _ {1}}{C _ {2}}\\right) \\frac {1}{1 - z ^ {- 1}} = - \\left(\\frac {C _ {1}}{C _ {2}}\\right) \\frac {z}{z - 1}. \\tag {24}",
        "H^ee(z) = V_out^e(z)/V_in^e(z) = -(C_1/C_2) 1/(1 - z^-1) = -(C_1/C_2) z/(z - 1)   (inverting SC integrator, Fig. 9.3-4b; same magnitude as noninverting, opposite-sign phase error)",
        "direct", metadata=meta(tag="24", role="inverting_integrator_Hz")),
    atom(
        "A-INT-WI-19", "SEC-9.3", "formula_atom", "SE-9.3-EQ19",
        "\\omega_ {I} = \\frac {C _ {1}}{T C _ {2}}. \\tag {19}",
        "omega_I = C_1/(T C_2) = (C_1/C_2) f_c   (integrator frequency; well defined because proportional to a capacitor ratio)",
        "direct", metadata=meta(tag="19", role="integrator_frequency")),
    atom(
        "A-INT-PHASE-CANCEL", "SEC-9.3", "design_principle_atom", "SE-9.3-PHASE",
        "In many applications, a noninverting and inverting integrator are in series in a feedback loop. It should be noted using the configurations of Fig. 9.3-4 that the phase errors of each integrator cancel resulting in no phase error.",
        "When a noninverting and an inverting integrator (Fig. 9.3-4) are placed in series in a feedback loop, the phase errors of each integrator cancel, resulting in no phase error.",
        "direct"),
    atom(
        "A-INT-NONIDEAL", "SEC-9.3", "concept_definition_atom", "SE-9.3-NONIDEAL",
        "The nonideal behavior of switched capacitor integrators includes clock feedthrough, finite differential voltage gain of the op amp, finite unity gainbandwidth of the op amp, and the slew rate of the op amp.",
        "The nonideal behavior of switched capacitor integrators includes clock feedthrough, finite differential voltage gain of the op amp, finite unity-gainbandwidth GB, and slew rate of the op amp.",
        "direct"),
    atom(
        "A-INT-AVD-ERR-36", "SEC-9.3", "formula_atom", "SE-9.3-EQ36",
        "m (j \\omega) = - \\frac {1}{A _ {v d} (0)} \\left[ 1 + \\frac {C _ {1}}{2 C _ {2}} \\right] \\tag {36}",
        "m(jw) = -(1/A_vd(0))[1 + C_1/(2C_2)]   (integrator magnitude error from finite A_vd(0); phase error theta(jw) = (C_1/C_2)/(2 A_vd(0) tan(wT/2)), Eq.37; the phase error is generally more serious)",
        "direct", metadata=meta(tag="36", role="integrator_finite_gain_error")),
    atom(
        "A-INT-KTC-41", "SEC-9.3", "formula_atom", "SE-9.3-EQ41",
        "v _ {R _ {o n}} ^ {2} = \\frac {2 k T R _ {o n}}{\\pi} \\int_ {0} ^ {\\infty} \\frac {\\omega_ {1} {} ^ {2} d \\omega}{\\omega_ {1} {} ^ {2} + \\omega^ {2}} = \\frac {2 k T R _ {o n}}{\\pi} \\left(\\frac {\\pi \\omega_ {1}}{2}\\right) = \\frac {k T}{C} \\text { Volts(rms) } ^ {2} \\tag {41}",
        "v_Ron^2 = (2kT R_on/pi) integral_0^inf omega_1^2 dw/(omega_1^2 + w^2) = (2kT R_on/pi)(pi omega_1/2) = kT/C Volts(rms)^2   (switch thermal kT/C noise)",
        "direct", metadata=meta(tag="41", role="ktc_noise")),
    # Example 9.3-3
    atom(
        "A-EX933-PROBLEM", "SEC-EX933", "example_problem_atom", "SE-EX933-P",
        "Assume that the clock frequency and integrator frequency of a switch capacitor integrator is 100kHz and 10kHz, respectively. If the value of $A _ { \\nu d } ( 0 )$ is 100, find the value of m(j ) and $\\theta ( j \\omega )$ at 10kHz.",
        "For an SC integrator with clock frequency 100 kHz and integrator frequency 10 kHz, and A_vd(0) = 100, find m(jw) and theta(jw) at 10 kHz.",
        "direct"),
    atom(
        "A-EX933-RESULT", "SEC-EX933", "result_atom", "SE-EX933-R",
        "m (j \\omega) = - \\left[ 1 + \\frac {0 . 6 2 8 3}{2} \\right] = - 1. 0 1 3 1",
        "m(jw) = -[1 + 0.6283/2] = -1.0131",
        "direct"),

    # ---------- 9.4 z-domain models ----------
    atom(
        "A-Z-MODEL-CONCEPT", "SEC-9.4", "concept_definition_atom", "SE-9.4-CONCEPT",
        "The development of z-domain models for switched capacitor circuits using a twophase, nonoverlapping clock is based on decomposing time-varient circuits into timeinvarient circuits.",
        "The development of z-domain models for switched capacitor circuits using a two-phase nonoverlapping clock is based on decomposing time-variant circuits into time-invariant ones.",
        "direct"),
    atom(
        "A-Z-ADMITTANCE", "SEC-9.4", "formula_atom", "SE-9.4-EQ1",
        "I (z) = Y \\cdot z ^ {0} V (z) = Y \\cdot V (z) \\tag {1}",
        "I(z) = Y z^0 V(z) = Y V(z)   (no-delay admittance; the other two types are I(z) = Y z^-1/2 V(z) (Eq.2, half-period delay) and I(z) = (1 - z^-1) Y V(z) (Eq.3); Y equals the switched capacitor value C in all three)",
        "direct", metadata=meta(tag="1", role="z_admittance_types")),
    atom(
        "A-Z-FOURPORTS", "SEC-9.4", "component_model_atom", "SE-9.4-FOURPORTS",
        "They are the parallel or toggle switched capacitor of Fig. 9.1-1, the positive and negative switched capacitors of Fig. 9.2-6, and a capacitor in series with a switch which we have not used yet.",
        "The four switched-capacitor two-ports are the parallel/toggle switched capacitor (Fig. 9.1-1), the positive and negative switched capacitors (Fig. 9.2-6), and a capacitor in series with a switch.",
        "direct"),

    # ---------- 9.5 First-order ----------
    atom(
        "A-FO-APPROACH", "SEC-9.5", "design_principle_atom", "SE-9.5-APPROACH",
        "There are two approaches to switched capacitor filter design. One uses integrators coupled together and the other uses cascades of first-order and second-order switched capacitor building blocks.",
        "There are two approaches to SC filter design: one uses integrators coupled together, and the other uses cascades of first-order and second-order switched capacitor building blocks.",
        "direct"),
    atom(
        "A-FO-HS-1", "SEC-9.5", "formula_atom", "SE-9.5-EQ1",
        "H (s) = \\frac {s a _ {1} \\pm a _ {0}}{s + b _ {0}} \\tag {1}",
        "H(s) = (s a_1 +- a_0)/(s + b_0)   (general first-order s-domain TF: a_1=0 -> lowpass, a_0=0 -> highpass, neither -> allpass; z-domain H(z) = (A_1 +- A_0 z^-1)/(1 - B_0 z^-1), Eq.2)",
        "direct", metadata=meta(tag="1", role="first_order_tf")),
    atom(
        "A-FO-LP-4", "SEC-9.5", "formula_atom", "SE-9.5-EQ4",
        "\\frac {V _ {o} ^ {o} (z)}{V _ {i} ^ {o} (z)} = \\frac {\\alpha_ {1} z ^ {- 1}}{1 + \\alpha_ {2} - z ^ {- 1}} = \\frac {\\frac {\\alpha_ {1} z ^ {- 1}}{1 + \\alpha_ {2}}}{1 - \\frac {z ^ {- 1}}{1 + \\alpha_ {2}}} \\tag {4}",
        "V_o^o(z)/V_i^o(z) = alpha_1 z^-1/(1 + alpha_2 - z^-1)   (first-order SC lowpass, Fig. 9.5-1; design eqns alpha_1 = A_0/B_0, alpha_2 = (1-B_0)/B_0, Eq.5; alpha_2 C is the switched capacitor placed across the integrating capacitor C, the damping that turns the integrator into a lowpass)",
        "direct", metadata=meta(tag="4", role="first_order_lowpass")),
    atom(
        "A-FO-HP-13", "SEC-9.5", "formula_atom", "SE-9.5-EQ13",
        "H ^ {e e} (z) = \\frac {V _ {o} ^ {e} (z)}{V _ {i} ^ {e} (z)} = \\frac {- \\alpha_ {1} \\left(1 - z ^ {- 1}\\right)}{\\alpha_ {2} + 1 - z ^ {- 1}} = \\frac {\\frac {\\alpha_ {1}}{\\alpha_ {2} + 1} \\left(1 - z ^ {- 1}\\right)}{1 - \\frac {1}{\\alpha_ {2} + 1} z ^ {- 1}} \\tag {13}",
        "H^ee(z) = V_o^e(z)/V_i^e(z) = -alpha_1(1 - z^-1)/(alpha_2 + 1 - z^-1)   (first-order SC highpass, Fig. 9.5-3)",
        "direct", metadata=meta(tag="13", role="first_order_highpass")),
    atom(
        "A-FO-DIFF", "SEC-9.5", "design_principle_atom", "SE-9.5-DIFF",
        "In practice, differential versions of these circuits are used to reduce clock feedthrough, common mode noise sources and enhance the signal swing. Fig. 9.5-8 shows one possible differential version of Fig. 9.5-1, 9.5-3, and 9.5-5. Differential operation of switched capacitor circuits requires op amps or OTAs with differential outputs.",
        "In practice, differential versions of the first-order circuits are used to reduce clock feedthrough and common-mode noise and to enhance signal swing; differential operation requires op amps/OTAs with differential outputs.",
        "direct"),
    atom(
        "A-FO-DIFF-COST", "SEC-9.5", "design_principle_atom", "SE-9.5-DIFFCOST",
        "Although, differential operation increases the component count and the amplifier complexity, the signal swing is increased by a factor of two and the even harmonics are diminished.",
        "Differential operation increases component count and amplifier complexity, but the signal swing is doubled and the even harmonics are diminished.",
        "direct"),
    # Example 9.5-1
    atom(
        "A-EX951-PROBLEM", "SEC-EX951", "example_problem_atom", "SE-EX951-P",
        "Design a switched capacitor first-order circuit that has a low frequency gain of +10 and a -3dB frequency of 1kHz. Give the value of the capacitor ratios $\\alpha _ { 1 }$ and $\\alpha _ { 2 } .$ Use a clock frequency of 100kHz.",
        "Design a first-order SC circuit with low-frequency gain +10 and -3 dB frequency 1 kHz; give the capacitor ratios alpha_1 and alpha_2 for a clock frequency of 100 kHz.",
        "direct"),
    atom(
        "A-EX951-RESULT", "SEC-EX951", "result_atom", "SE-EX951-R",
        "Therefore, we see that $\\alpha _ { 2 } = 6 2 8 3 / 1 0 0 , 0 0 0 = 0 . 0 6 2 8$ and $\\begin{array} { r } { \\alpha _ { 1 } = 0 . 6 2 8 3 . } \\end{array}$",
        "alpha_2 = 6283/100,000 = 0.0628 and alpha_1 = 0.6283.",
        "direct"),

    # ---------- 9.6 Second-order ----------
    atom(
        "A-SO-CASCADE", "SEC-9.6", "design_principle_atom", "SE-9.6-CASCADE",
        "One approach to designing higher order filters is to take the design in polynomial form and break it into products of second-order products. If the filter order is odd, then one first-order product will result. The implementation of each product can then be accomplished by a cascade of second-order circuits that each individually realize a pair of complex conjugate poles and two zeros (which may be at infinity or zero or in between).",
        "Higher-order filters are realized by factoring the transfer function into products of second-order functions, each implemented as a cascade of second-order circuits realizing a pair of complex-conjugate poles and two zeros; if the order is odd, one first-order product remains.",
        "direct"),
    atom(
        "A-SO-BIQUAD-CONCEPT", "SEC-9.6", "concept_definition_atom", "SE-9.6-BIQUAD",
        "The biquad circuit has the ability to realize both complex poles and complex zeros. The poles are generated by the primary feedback path which generally consists of a noninverting integrator and an inverting integrator. One of the integrators is damped to avoid oscillation. The zeros are generated by how the input signal is applied to circuit.",
        "A biquad realizes both complex poles and complex zeros. The poles come from the primary feedback path - generally a noninverting integrator plus an inverting integrator, one of which is damped to avoid oscillation. The zeros come from how the input signal is applied.",
        "direct"),
    atom(
        "A-SO-BIQUAD-HS-1", "SEC-9.6", "formula_atom", "SE-9.6-EQ1",
        "H _ {a} (s) = \\frac {V _ {\\text { out }} (s)}{V _ {\\text { in }} (s)} = \\frac {- (K _ {2} s ^ {2} + K _ {1} s + K _ {0})}{s ^ {2} + \\frac {\\omega_ {o}}{Q} s + \\omega_ {o} ^ {2}} \\tag {1}",
        "H_a(s) = V_out(s)/V_in(s) = -(K_2 s^2 + K_1 s + K_0)/(s^2 + (omega_o/Q) s + omega_o^2)   (continuous-time biquad; omega_o = pole frequency, Q = pole Q, K_0,K_1,K_2 set the zeros)",
        "direct", metadata=meta(tag="1", role="biquad_tf")),
    atom(
        "A-SO-BIQUAD-ALPHA-11", "SEC-9.6", "formula_atom", "SE-9.6-EQ11",
        "\\alpha_ {1} = \\frac {K _ {0} T}{\\omega_ {o}}, \\quad \\alpha_ {2} = | \\alpha_ {5} | = \\omega_ {o} T, \\quad \\alpha_ {3} = K _ {2}, \\quad \\alpha_ {4} = K _ {1} T, \\text { and } \\quad \\alpha_ {6} = \\frac {\\omega_ {o} T}{Q}. \\tag {11}",
        "alpha_1 = K_0 T/omega_o, alpha_2 = |alpha_5| = omega_o T, alpha_3 = K_2, alpha_4 = K_1 T, alpha_6 = omega_o T/Q   (low-Q SC biquad capacitor ratios, Fig. 9.6-4)",
        "direct", metadata=meta(tag="11", role="biquad_capacitor_ratios")),
    atom(
        "A-SO-BIQUAD-LOWQ", "SEC-9.6", "design_principle_atom", "SE-9.6-LOWQ",
        "If Q becomes too large, i.e. greater than 5, $\\alpha _ { 5 }$ becomes too small which causes the biquad of Fig. 9.6-4 to be suitable for low-Q applications.",
        "If Q becomes greater than 5, alpha_5 becomes too small, so the biquad of Fig. 9.6-4 is suitable only for low-Q applications.",
        "direct"),
    # Example 9.6-1
    atom(
        "A-EX961-PROBLEM", "SEC-EX961", "example_problem_atom", "SE-EX961-P",
        "Assume that the specifications of a biquad ar $\\begin{array} { r } { \\mathbf { \\nabla } \\cdot f _ { o } = 1 \\mathrm { k H z } , Q = 2 , K _ { 0 } = K _ { \\gamma } = 0 , } \\end{array}$ , and $K _ { \\mathrm { 1 } } = 2 \\pi f _ { \\mathrm { 0 } } / Q$ (a bandpass filter). The clock frequency is 100kHz. Design the capacitor ratios of Fig. 9.6-4 and determine the maximum capacitor ratio and the total capacitance assuming that $C _ { 1 }$ and $C _ { 2 }$ have unit values.",
        "For a biquad with f_o = 1 kHz, Q = 2, K_0 = K_2 = 0, K_1 = 2 pi f_o/Q (bandpass) and clock frequency 100 kHz, design the capacitor ratios of Fig. 9.6-4 and find the maximum capacitor ratio and total capacitance (C_1 = C_2 = 1).",
        "direct"),
    atom(
        "A-EX961-RESULT", "SEC-EX961", "result_atom", "SE-EX961-R",
        "From Eq. (11) we get $\\alpha _ { 1 } = \\alpha _ { 3 } = 0 , \\alpha _ { 2 } = \\alpha _ { 5 } = 0 . 0 6 2 8$ , and $\\begin{array} { r } { \\alpha _ { 4 } = \\alpha _ { 6 } = 0 . 0 3 1 4 . } \\end{array}$ . The largest capacitor ratio is $\\alpha _ { 4 }$ or $\\alpha _ { 6 }$ and is 1/31.83.",
        "alpha_1 = alpha_3 = 0, alpha_2 = alpha_5 = 0.0628, alpha_4 = alpha_6 = 0.0314; the largest capacitor ratio is alpha_4 or alpha_6 and is 1/31.83.",
        "direct"),

    # ---------- 9.7 Filters ----------
    atom(
        "A-FILT-APP", "SEC-9.7", "concept_definition_atom", "SE-9.7-APP",
        "Based on the above discussion, all linear filters are characterized by three properties. These properties are the passband ripple, the transition frequency, and the stopband gain/attenuation.",
        "All linear filters are characterized by three properties: the passband ripple, the transition frequency, and the stopband gain/attenuation.",
        "direct"),
    atom(
        "A-FILT-CASCADE", "SEC-9.7", "design_principle_atom", "SE-9.7-CASCADE",
        "All approaches start with a normalized, low pass filter with a passband of 1 radian/second and an impedance of 1Ω that will satisfy the filter specification. In the cascade approach, the root locations for the desired are identified and then transformed to the roots of an unnormalized low pass realizations. If the filter is to be low pass, then these roots are grouped into products of second-order functions. If the filter order is odd, then one first-order product is required.",
        "Cascade approach: start from a normalized lowpass prototype (passband 1 rad/s, impedance 1 Ohm); identify the root locations, transform them to an unnormalized lowpass realization, and group the roots into products of second-order functions (one first-order product if the order is odd).",
        "direct"),
    atom(
        "A-FILT-LADDER", "SEC-9.7", "design_principle_atom", "SE-9.7-LADDER",
        "The second major approach to designing higher-order, switched capacitor circuits shown in Fig. 9.7-6 is called the ladder approach. It has the advantage of being less sensitive to the capacitor ratios than the cascade approach. Its disadvantage is that the design approach is slightly more complex and is only applicable to filters that can be expressed as RLC circuits. The ladder approach begins with the normalized, low pass, RLC prototype filter structure. Next, state equations are written based on the RLC prototype circuit. Lastly, the state equations are synthesized using the appropriate switched capacitor circuits.",
        "Ladder approach: begins with a normalized lowpass RLC prototype, writes state equations from the RLC circuit, and synthesizes them with the appropriate switched capacitor circuits. Advantage: less sensitive to capacitor ratios than the cascade approach. Disadvantage: more complex and only applicable to filters expressible as RLC circuits.",
        "direct"),

    # ---------- 9.8 Summary (context_only) ----------
    atom(
        "A-SUMMARY", "SEC-9.8", "summary_atom", "SE-9.8-SUMMARY",
        "A disadvantage of switched capacitor circuits is the clock feedthrough that occurs via the overlap capacitance of the switches. While the feedthrough can be minimized, it represents the ultimate accuracy of the switched capacitor circuit.",
        "A disadvantage of switched capacitor circuits is the clock feedthrough that occurs via the overlap capacitance of the switches; while it can be minimized, it represents the ultimate accuracy of the switched capacitor circuit.",
        "direct", context_only=True),

    # ---------- Homework Problems ----------
    atom(
        "A-PROB-911", "SEC-PROB", "problem_statement_atom", "SE-PROB-911",
        "9.1-1 Develop the equivalent resistance expression in Table 9.1-1 for the series switched capacitor resistor emulation circuit.",
        "9.1-1 Develop the equivalent resistance expression in Table 9.1-1 for the series switched capacitor resistor emulation circuit.",
        "direct"),
    atom(
        "A-PROB-913", "SEC-PROB", "problem_statement_atom", "SE-PROB-913",
        "9.1-3 What is the accuracy of a time constant implemented with a resistor and capacitor having a tolerance of 10% and 5%, respectively. What is the accuracy of a time constant implemented by a switched capacitor resistor emulation and a capacitor if the tolerances of the capacitors are 5% and the relative tolerance is 0.5%. Assume that the clock frequency is perfectly accurate.",
        "9.1-3 Compare the accuracy of a time constant built from a resistor (10% tolerance) and capacitor (5%) with one built from a switched-capacitor resistor emulation and a capacitor (5% absolute, 0.5% relative tolerance, clock perfectly accurate).",
        "direct"),
    atom(
        "A-PROB-955", "SEC-PROB", "problem_statement_atom", "SE-PROB-955",
        "9.5-1 Develop Eq. (6) for the inverting low pass circuit obtained from Fig. 9.1-5(a.) by reversing the phases of the leftmost two switches. Verify Eq. (7).",
        "9.5-1 Develop the transfer function (Eq.6) for the inverting first-order lowpass circuit obtained by reversing the phases of the two leftmost switches; verify Eq.(7).",
        "direct"),
]

# Formula/condition normalized must be FORMULA-ONLY (no gloss). Any trailing
# parenthetical explanation written after a 3-space marker is split off and
# merged into metadata.note (the symbolic-interpretation channel), keeping the
# normalized string a bare ascii formula.
_GLOSS = "   ("
for _a in ATOMS:
    if _a["atom_type"] not in ("formula_atom", "condition_atom"):
        continue
    nt = _a["normalized_text"]
    if _GLOSS in nt:
        formula, gloss = nt.split(_GLOSS, 1)
        gloss = gloss.rstrip()
        if gloss.endswith(")"):
            gloss = gloss[:-1]
        _a["normalized_text"] = formula.rstrip()
        md = _a.get("metadata")
        if md is None:
            md = OrderedDict()
            _a["metadata"] = md
        if "note" in md:
            md["note"] = md["note"] + "; " + gloss
        else:
            md["note"] = gloss

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ===========================================================================
# source_elements: built from atom source spans grouped by source_element_id,
# plus the section headings.
# ===========================================================================
HEADINGS = [
    ("SE-H-9", 7496), ("SE-H-9.1", 7504), ("SE-H-EX911", 7633), ("SE-H-EX912", 7684),
    ("SE-H-EX913", 7810), ("SE-H-9.2", 8100), ("SE-H-EX921", 8209), ("SE-H-EX924", 8731),
    ("SE-H-9.3", 8788), ("SE-H-EX933", 9325), ("SE-H-9.4", 9409), ("SE-H-9.5", 9985),
    ("SE-H-EX951", 10113), ("SE-H-9.6", 10350), ("SE-H-EX961", 10582), ("SE-H-9.7", 10930),
    ("SE-H-9.8", 12647), ("SE-H-PROB", 12655),
]


def build_source_elements(atoms):
    elems = [OrderedDict([("id", hid), ("type", "heading"), ("file", SOURCE_NAME),
                          ("line_start", ln), ("line_end", ln)]) for hid, ln in HEADINGS]
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    for se_id, members in groups.items():
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        is_formula = any(m["atom_type"] == "formula_atom" for m in members)
        is_table = any(m["atom_type"] == "table_row_atom" for m in members)
        etype = "formula" if is_formula else ("table" if is_table else "paragraph")
        d = OrderedDict([("id", se_id), ("type", etype), ("file", SOURCE_NAME),
                         ("line_start", ls), ("line_end", le)])
        tag = members[0].get("metadata", {}).get("tag") if members[0].get("metadata") else None
        if tag:
            d["metadata"] = OrderedDict([("tag", tag)])
        elems.append(d)
    return elems

SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ===========================================================================
# section_tree (same as v0.1)
# ===========================================================================
def sec(sid, path, title, parent=None, kind=None):
    d = OrderedDict([("id", sid), ("path", path), ("title", title)])
    if parent:
        d["parent"] = parent
    if kind:
        d["kind"] = kind
    return d

SECTION_TREE = [
    sec("SEC-9", "9", "Switched Capacitor Circuits"),
    sec("SEC-9.1", "9 > 9.1", "Switched Capacitor Circuits (Resistor Emulation)", "SEC-9"),
    sec("SEC-EX911", "9 > 9.1 > Example 9.1-1", "Example 9.1-1 Parallel SC Resistor Emulation", "SEC-9.1", "example"),
    sec("SEC-EX912", "9 > 9.1 > Example 9.1-2", "Example 9.1-2 Series-Parallel SC Resistor Emulation", "SEC-9.1", "example"),
    sec("SEC-EX913", "9 > 9.1 > Example 9.1-3", "Example 9.1-3 Analysis of a SC First-order Lowpass Filter", "SEC-9.1", "example"),
    sec("SEC-9.2", "9 > 9.2", "Switched Capacitor Amplifiers", "SEC-9"),
    sec("SEC-EX921", "9 > 9.2 > Example 9.2-1", "Example 9.2-1 Accuracy Limitation due to Finite Voltage Gain", "SEC-9.2", "example"),
    sec("SEC-EX924", "9 > 9.2 > Example 9.2-4", "Example 9.2-4 Clock Feedthrough on a SC Voltage Amplifier", "SEC-9.2", "example"),
    sec("SEC-9.3", "9 > 9.3", "Switched Capacitor Integrators", "SEC-9"),
    sec("SEC-EX933", "9 > 9.3 > Example 9.3-3", "Example 9.3-3 Integrator Errors due to Finite A_vd(0)", "SEC-9.3", "example"),
    sec("SEC-9.4", "9 > 9.4", "z-domain Models of Two-Phase SC Circuits", "SEC-9"),
    sec("SEC-9.5", "9 > 9.5", "First-Order Switched Capacitor Circuits", "SEC-9"),
    sec("SEC-EX951", "9 > 9.5 > Example 9.5-1", "Example 9.5-1 Design of a SC First-Order Circuit", "SEC-9.5", "example"),
    sec("SEC-9.6", "9 > 9.6", "Second-Order Switched Capacitor Circuits", "SEC-9"),
    sec("SEC-EX961", "9 > 9.6 > Example 9.6-1", "Example 9.6-1 Design of a SC Low-Q Biquad", "SEC-9.6", "example"),
    sec("SEC-9.7", "9 > 9.7", "Switched Capacitor Filters", "SEC-9"),
    sec("SEC-9.8", "9 > 9.8", "Summary", "SEC-9"),
    sec("SEC-PROB", "9 > Homework Problems", "Homework Problems", "SEC-9"),
]

# ===========================================================================
# semantic_chunks (same partition as v0.1).
# ===========================================================================
def chunk(cid, ctype, section_path, atom_ids, central, boundary, targets, must_cover):
    return OrderedDict([
        ("id", cid), ("profile", "textbook"), ("chunk_type", ctype),
        ("section_path", section_path), ("atom_ids", atom_ids),
        ("central_atom_ids", central), ("boundary_reason", boundary),
        ("extraction_targets", targets), ("gold_must_cover_atoms", must_cover),
    ])

SEMANTIC_CHUNKS = [
    chunk("C-OV", "chapter_overview_block", "9",
          ["A-INTRO", "A-INTRO-ACCURACY", "A-SUMMARY"], ["A-INTRO-ACCURACY"],
          "chapter framing: intro (9.0) + the foundational capacitor-ratio accuracy principle + summary (9.8) kept together. A-INTRO/A-SUMMARY are context_only framing.",
          ["Concept", "DesignPrinciple"], ["A-INTRO", "A-INTRO-ACCURACY", "A-SUMMARY"]),
    chunk("C-RES-EMU", "design_principle_block", "9 > 9.1",
          ["A-SC-RES-CONCEPT", "A-SC-NONOVERLAP", "A-SC-IAVG-6", "A-SC-IAVG-10", "A-SC-REQ-12", "A-SC-REQ-SP-17", "A-SC-TABLE-911"],
          ["A-SC-REQ-12"],
          "resistor-emulation derivation chain i_avg -> R=T/C kept with its concept, nonoverlapping-clock definition, and the Table 9.1-1 summary of all four emulations (formula-explanation continuity).",
          ["Concept", "Formula", "DesignPrinciple", "CircuitBlock"],
          ["A-SC-RES-CONCEPT", "A-SC-REQ-12", "A-SC-TABLE-911"]),
    chunk("C-ACCURACY", "design_principle_block", "9 > 9.1",
          ["A-SC-ACC-TAUC-20", "A-SC-ACC-TAUD-22"], ["A-SC-ACC-TAUD-22"],
          "the accuracy argument: continuous-time tau_C accuracy vs discrete-time tau_D accuracy (depends on capacitor ratio + clock) - the core SC value proposition.",
          ["Formula", "DesignPrinciple"], ["A-SC-ACC-TAUC-20", "A-SC-ACC-TAUD-22"]),
    chunk("C-Z-ANALYSIS", "formula_definition_block", "9 > 9.1",
          ["A-SC-ZTRANS-25", "A-SC-LPF-HZ-37"], ["A-SC-LPF-HZ-37"],
          "z-transform definition + the resulting SC lowpass z-domain transfer function.",
          ["Formula"], ["A-SC-ZTRANS-25", "A-SC-LPF-HZ-37"]),
    chunk("C-EX-911", "example_solution_block", "9 > 9.1 > Example 9.1-1",
          ["A-EX911-PROBLEM", "A-EX911-USE", "A-EX911-RESULT"], ["A-EX911-PROBLEM"],
          "problem/formula-usage/result continuous (example_solution_continuity).",
          ["ExampleProblem", "ExampleSolution"], ["A-EX911-PROBLEM", "A-EX911-USE", "A-EX911-RESULT"]),
    chunk("C-EX-912", "example_solution_block", "9 > 9.1 > Example 9.1-2",
          ["A-EX912-PROBLEM", "A-EX912-RESULT"], ["A-EX912-PROBLEM"],
          "series-parallel SC resistor worked example.",
          ["ExampleSolution"], ["A-EX912-PROBLEM", "A-EX912-RESULT"]),
    chunk("C-EX-913", "example_solution_block", "9 > 9.1 > Example 9.1-3",
          ["A-EX913-PROBLEM", "A-EX913-RESULT"], ["A-EX913-PROBLEM"],
          "SC lowpass z-domain analysis worked example.",
          ["ExampleSolution"], ["A-EX913-PROBLEM", "A-EX913-RESULT"]),
    chunk("C-AMP-CT", "formula_definition_block", "9 > 9.2",
          ["A-AMP-CT-12", "A-AMP-AVD-3", "A-AMP-FINITE-6", "A-CHARGE-AMP-12", "A-CHARGE-AMP-USE"],
          ["A-AMP-CT-12"],
          "continuous-time amplifier gains + finite-gain op-amp model + charge amplifier - the prerequisites the SC amplifier is built on.",
          ["Formula", "PhysicalEffect"], ["A-AMP-CT-12", "A-CHARGE-AMP-12"]),
    chunk("C-SCAMP", "circuit_hierarchy_block", "9 > 9.2",
          ["A-SCAMP-EVOLVE", "A-SCAMP-HZ-19", "A-SCAMP-TRANSR-28", "A-SCAMP-PARASITIC", "A-SCAMP-NOEXCESS"],
          ["A-SCAMP-HZ-19"],
          "SC amplifier built from SC-resistor/transresistance blocks; gain = -C1/C2; parasitic-insensitive variant - the composition story kept together.",
          ["CircuitBlock", "Formula", "DesignPrinciple"],
          ["A-SCAMP-HZ-19", "A-SCAMP-TRANSR-28", "A-SCAMP-PARASITIC"]),
    chunk("C-FEEDTHRU", "physical_effect_block", "9 > 9.2",
          ["A-SCAMP-FEEDTHRU-50", "A-SCAMP-FEEDTHRU-FIX", "A-SCAMP-AVD-52"],
          ["A-SCAMP-FEEDTHRU-50"],
          "clock feedthrough (three output terms) + its mitigations + finite-gain error - the SC nonideality cluster.",
          ["PhysicalEffect", "Formula", "DesignPrinciple"],
          ["A-SCAMP-FEEDTHRU-50", "A-SCAMP-FEEDTHRU-FIX"]),
    chunk("C-EX-921", "example_solution_block", "9 > 9.2 > Example 9.2-1",
          ["A-EX921-PROBLEM", "A-EX921-RESULT"], ["A-EX921-PROBLEM"],
          "finite-gain accuracy worked example.",
          ["ExampleSolution"], ["A-EX921-PROBLEM", "A-EX921-RESULT"]),
    chunk("C-EX-924", "example_solution_block", "9 > 9.2 > Example 9.2-4",
          ["A-EX924-PROBLEM", "A-EX924-RESULT"], ["A-EX924-PROBLEM"],
          "clock-feedthrough numeric worked example.",
          ["ExampleSolution"], ["A-EX924-PROBLEM", "A-EX924-RESULT"]),
    chunk("C-INT", "formula_definition_block", "9 > 9.3",
          ["A-INT-ROLE", "A-INT-CT-1", "A-INT-NONINV-16", "A-INT-INV-24", "A-INT-WI-19", "A-INT-PHASE-CANCEL"],
          ["A-INT-NONINV-16", "A-INT-INV-24"],
          "continuous-time integrator -> noninverting/inverting SC integrator z-domain transfer functions + integrator frequency + phase-cancellation, the core building block kept whole.",
          ["Formula", "CircuitBlock", "DesignPrinciple"],
          ["A-INT-NONINV-16", "A-INT-INV-24", "A-INT-WI-19"]),
    chunk("C-INT-NONIDEAL", "physical_effect_block", "9 > 9.3",
          ["A-INT-NONIDEAL", "A-INT-AVD-ERR-36", "A-INT-KTC-41"], ["A-INT-AVD-ERR-36"],
          "integrator nonidealities: feedthrough/finite-gain magnitude & phase error + kT/C noise.",
          ["PhysicalEffect", "Formula"], ["A-INT-NONIDEAL", "A-INT-AVD-ERR-36"]),
    chunk("C-EX-933", "example_solution_block", "9 > 9.3 > Example 9.3-3",
          ["A-EX933-PROBLEM", "A-EX933-RESULT"], ["A-EX933-PROBLEM"],
          "integrator finite-gain error worked example.",
          ["ExampleSolution"], ["A-EX933-PROBLEM", "A-EX933-RESULT"]),
    chunk("C-ZMODEL", "component_model_block", "9 > 9.4",
          ["A-Z-MODEL-CONCEPT", "A-Z-ADMITTANCE", "A-Z-FOURPORTS"], ["A-Z-ADMITTANCE"],
          "z-domain modeling method: decomposition concept + three admittance types + the catalog of two-port models.",
          ["Concept", "Formula", "ComponentModel"], ["A-Z-MODEL-CONCEPT", "A-Z-ADMITTANCE"]),
    chunk("C-FIRSTORDER", "design_process_block", "9 > 9.5",
          ["A-FO-APPROACH", "A-FO-HS-1", "A-FO-LP-4", "A-FO-HP-13", "A-FO-DIFF", "A-FO-DIFF-COST"],
          ["A-FO-LP-4"],
          "first-order building blocks: general first-order TF + lowpass/highpass realizations + design equations + differential practice.",
          ["Formula", "CircuitBlock", "DesignPrinciple"], ["A-FO-HS-1", "A-FO-LP-4"]),
    chunk("C-EX-951", "example_solution_block", "9 > 9.5 > Example 9.5-1",
          ["A-EX951-PROBLEM", "A-EX951-RESULT"], ["A-EX951-PROBLEM"],
          "first-order SC design worked example.",
          ["ExampleSolution"], ["A-EX951-PROBLEM", "A-EX951-RESULT"]),
    chunk("C-SECONDORDER", "circuit_hierarchy_block", "9 > 9.6",
          ["A-SO-CASCADE", "A-SO-BIQUAD-CONCEPT", "A-SO-BIQUAD-HS-1", "A-SO-BIQUAD-ALPHA-11", "A-SO-BIQUAD-LOWQ"],
          ["A-SO-BIQUAD-CONCEPT"],
          "biquad = two integrators (one damped) + input zeros; continuous-time TF + capacitor-ratio design; cascade composition into higher-order filters.",
          ["CircuitBlock", "Formula", "DesignPrinciple"],
          ["A-SO-BIQUAD-CONCEPT", "A-SO-BIQUAD-ALPHA-11"]),
    chunk("C-EX-961", "example_solution_block", "9 > 9.6 > Example 9.6-1",
          ["A-EX961-PROBLEM", "A-EX961-RESULT"], ["A-EX961-PROBLEM"],
          "low-Q biquad design worked example.",
          ["ExampleSolution"], ["A-EX961-PROBLEM", "A-EX961-RESULT"]),
    chunk("C-FILTERS", "circuit_hierarchy_block", "9 > 9.7",
          ["A-FILT-APP", "A-FILT-CASCADE", "A-FILT-LADDER"], ["A-FILT-LADDER"],
          "SC filters built from biquad/first-order blocks (cascade) or from RLC ladder prototypes via integrators (ladder) - the top of the composition hierarchy.",
          ["Concept", "DesignPrinciple", "CircuitBlock"], ["A-FILT-CASCADE", "A-FILT-LADDER"]),
    chunk("C-PROB", "problem_set_block", "9 > Homework Problems",
          ["A-PROB-911", "A-PROB-913", "A-PROB-955"], ["A-PROB-911"],
          "end-of-chapter problems.",
          ["ProblemStatement"], ["A-PROB-911", "A-PROB-913", "A-PROB-955"]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}

# ===========================================================================
# objects (same IDs/meaning as v0.1). local/supporting + home_package + labels.
# ===========================================================================
def obj(oid, otype, section_path, home_package, payload, local, supporting, difficulty, evidence_strength):
    return OrderedDict([
        ("id", oid), ("type", otype), ("section_path", section_path),
        ("home_package", home_package), ("payload", payload),
        ("local_evidence_atom_ids", local), ("supporting_context_atom_ids", supporting),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])

OBJECTS = [
    # --- Concept ---
    obj("CONCEPT-SC-CIRCUIT", "Concept", "9", "PKG-OV",
        OrderedDict([("term", "switched capacitor circuit"),
            ("definition", "Analog sampled-data circuit (continuous in amplitude, discrete in time) of MOSFET switches, capacitors and op amps that emulates resistors/time-constants for analog signal processing in CMOS.")]),
        ["A-INTRO"], [], "easy", "direct"),
    obj("CONCEPT-NONOVERLAP-CLOCK", "Concept", "9 > 9.1", "PKG-RES-EMU",
        OrderedDict([("term", "two-phase nonoverlapping clock"),
            ("definition", "Clocks phi_1, phi_2 never at logic 1 simultaneously; period T, each phase < T/2; controls the switches.")]),
        ["A-SC-NONOVERLAP"], [], "easy", "direct"),
    obj("CONCEPT-Z-MODEL", "Concept", "9 > 9.4", "PKG-ZMODEL",
        OrderedDict([("term", "z-domain model of SC circuit"),
            ("definition", "Decomposition of time-variant SC circuits into time-invariant z-domain models for analysis and SPICE-type frequency simulation.")]),
        ["A-Z-MODEL-CONCEPT"], [], "medium", "direct"),
    obj("CONCEPT-BIQUAD", "Concept", "9 > 9.6", "PKG-SECONDORDER",
        OrderedDict([("term", "biquad"),
            ("definition", "Second-order section realizing complex poles (from two integrators, one damped) and complex zeros (from how the input is applied).")]),
        ["A-SO-BIQUAD-CONCEPT"], [], "medium", "direct"),

    # --- Formula ---
    obj("FORMULA-REQ", "Formula", "9 > 9.1", "PKG-RES-EMU",
        OrderedDict([("name", "switched-capacitor equivalent resistance"),
            ("expression", "R = T/C = 1/(f_c C)"),
            ("variables", OrderedDict([("T", "clock period"), ("C", "switched capacitor"), ("f_c", "clock frequency")])),
            ("condition", "v_1,v_2 nearly constant over T (f signal << f clock)")]),
        ["A-SC-REQ-12", "A-SC-IAVG-10", "A-SC-IAVG-6"], [], "easy", "direct"),
    obj("FORMULA-REQ-SP", "Formula", "9 > 9.1", "PKG-RES-EMU",
        OrderedDict([("name", "series-parallel SC equivalent resistance"),
            ("expression", "R = T/(C_1 + C_2)")]),
        ["A-SC-REQ-SP-17"], [], "medium", "direct"),
    obj("FORMULA-TAUD-ACC", "Formula", "9 > 9.1", "PKG-ACCURACY",
        OrderedDict([("name", "SC time-constant accuracy"),
            ("expression", "d(tau_D)/tau_D = dC_2/C_2 - dC_1/C_1 - df_c/f_c"),
            ("note", "as small as 0.1% in CMOS vs 5-20% for continuous-time tau_C")]),
        ["A-SC-ACC-TAUD-22", "A-SC-ACC-TAUC-20"], [], "medium", "direct"),
    obj("FORMULA-SC-LPF", "Formula", "9 > 9.1", "PKG-Z-ANALYSIS",
        OrderedDict([("name", "SC first-order lowpass transfer function"),
            ("expression", "H^oo(z) = z^-1/(1 + alpha - alpha z^-1), alpha = C_2/C_1")]),
        ["A-SC-LPF-HZ-37", "A-SC-ZTRANS-25"], [], "medium", "direct"),
    obj("FORMULA-SC-AMP-GAIN", "Formula", "9 > 9.2", "PKG-SCAMP",
        OrderedDict([("name", "switched-capacitor amplifier gain"),
            ("expression", "H(z) = V_out/V_in = -(C_1/C_2) z^-1  (magnitude = C_1/C_2, set by a capacitor ratio)"),
            ("variables", OrderedDict([("C_1", "input capacitor"), ("C_2", "feedback capacitor")]))]),
        ["A-SCAMP-HZ-19"], [], "medium", "direct"),
    obj("FORMULA-TRANSRES", "Formula", "9 > 9.2", "PKG-SCAMP",
        OrderedDict([("name", "parasitic-insensitive SC transresistance"),
            ("expression", "R_T = +/- T/C"), ("condition", "f_c >> f")]),
        ["A-SCAMP-TRANSR-28"], [], "medium", "direct"),
    obj("FORMULA-FEEDTHRU", "Formula", "9 > 9.2", "PKG-FEEDTHRU",
        OrderedDict([("name", "clock-feedthrough output"),
            ("expression", "v_out ~= (C_1/C_2) v_in - (C_OL/2C_2) v_in + (C_OL/C_2) V_T"),
            ("note", "ideal term + input-dependent (distortion) term + constant (offset) term")]),
        ["A-SCAMP-FEEDTHRU-50"], [], "medium", "direct"),
    obj("FORMULA-INT-NONINV", "Formula", "9 > 9.3", "PKG-INT",
        OrderedDict([("name", "noninverting SC integrator transfer function"),
            ("expression", "H^oo(z) = (C_1/C_2) z^-1/(1 - z^-1)")]),
        ["A-INT-NONINV-16"], [], "medium", "direct"),
    obj("FORMULA-INT-INV", "Formula", "9 > 9.3", "PKG-INT",
        OrderedDict([("name", "inverting SC integrator transfer function"),
            ("expression", "H^ee(z) = -(C_1/C_2) 1/(1 - z^-1)")]),
        ["A-INT-INV-24"], [], "medium", "direct"),
    obj("FORMULA-INT-WI", "Formula", "9 > 9.3", "PKG-INT",
        OrderedDict([("name", "integrator frequency"),
            ("expression", "w_I = C_1/(T C_2) = (C_1/C_2) f_c"),
            ("note", "well defined because proportional to a capacitor ratio")]),
        ["A-INT-WI-19"], [], "medium", "direct"),
    obj("FORMULA-INT-AVD-ERR", "Formula", "9 > 9.3", "PKG-INT-NONIDEAL",
        OrderedDict([("name", "integrator finite-gain magnitude/phase error"),
            ("expression", "m(jw) = -(1/A_vd(0))[1 + C_1/(2C_2)]; theta(jw) = (C_1/C_2)/(2 A_vd(0) tan(wT/2))")]),
        ["A-INT-AVD-ERR-36"], [], "hard", "direct"),
    obj("FORMULA-KTC", "Formula", "9 > 9.3", "PKG-INT-NONIDEAL",
        OrderedDict([("name", "kT/C switch noise"),
            ("expression", "v_Ron^2 = kT/C Volts(rms)^2")]),
        ["A-INT-KTC-41"], [], "medium", "direct"),
    obj("FORMULA-Z-ADMITTANCE", "Formula", "9 > 9.4", "PKG-ZMODEL",
        OrderedDict([("name", "z-domain admittance types"),
            ("expression", "I=YV; I=Y z^-1/2 V; I=(1-z^-1) Y V; Y = switched capacitor value C")]),
        ["A-Z-ADMITTANCE"], [], "medium", "direct"),
    obj("FORMULA-FO-LP", "Formula", "9 > 9.5", "PKG-FIRSTORDER",
        OrderedDict([("name", "first-order SC lowpass"),
            ("expression", "V_o^o(z)/V_i^o(z) = alpha_1 z^-1/(1 + alpha_2 - z^-1); alpha_1=A_0/B_0, alpha_2=(1-B_0)/B_0")]),
        ["A-FO-LP-4", "A-FO-HS-1"], [], "medium", "direct"),
    obj("FORMULA-BIQUAD-HS", "Formula", "9 > 9.6", "PKG-SECONDORDER",
        OrderedDict([("name", "continuous-time biquad transfer function"),
            ("expression", "H_a(s) = -(K_2 s^2 + K_1 s + K_0)/(s^2 + (w_o/Q) s + w_o^2)")]),
        ["A-SO-BIQUAD-HS-1"], [], "medium", "direct"),
    obj("FORMULA-BIQUAD-ALPHA", "Formula", "9 > 9.6", "PKG-SECONDORDER",
        OrderedDict([("name", "low-Q biquad capacitor ratios"),
            ("expression", "alpha_1=K_0 T/w_o, alpha_2=|alpha_5|=w_o T, alpha_3=K_2, alpha_4=K_1 T, alpha_6=w_o T/Q")]),
        ["A-SO-BIQUAD-ALPHA-11"], [], "hard", "direct"),

    # --- CircuitBlock ---
    obj("BLOCK-SC-RESISTOR", "CircuitBlock", "9 > 9.1", "PKG-RES-EMU",
        OrderedDict([("name", "switched-capacitor resistor"),
            ("composition", "two switches + one capacitor + nonoverlapping clock"),
            ("function", "emulates R = T/C between two terminals"),
            ("variants", ["parallel", "series", "series-parallel", "bilinear"])]),
        ["A-SC-RES-CONCEPT", "A-SC-TABLE-911"], [], "easy", "direct"),
    obj("BLOCK-TRANSRESISTANCE", "CircuitBlock", "9 > 9.2", "PKG-SCAMP",
        OrderedDict([("name", "parasitic-insensitive SC transresistance"),
            ("function", "two-port giving R_T = +/- T/C; immune to capacitor parasitics")]),
        ["A-SCAMP-TRANSR-28", "A-SCAMP-PARASITIC"], [], "medium", "direct"),
    obj("BLOCK-SC-AMPLIFIER", "CircuitBlock", "9 > 9.2", "PKG-SCAMP",
        OrderedDict([("name", "switched-capacitor amplifier"),
            ("composition", "op amp + SC resistor/transresistance (input) + feedback capacitor"),
            ("function", "gain = -C_1/C_2; parasitic-insensitive realization in Fig 9.2-7")]),
        ["A-SCAMP-EVOLVE", "A-SCAMP-HZ-19", "A-SCAMP-PARASITIC"], [], "medium", "direct"),
    obj("BLOCK-SC-INTEGRATOR", "CircuitBlock", "9 > 9.3", "PKG-INT",
        OrderedDict([("name", "switched-capacitor integrator"),
            ("composition", "op amp + transresistance (input) + integrating capacitor C_2"),
            ("function", "noninverting H^oo=(C1/C2)z^-1/(1-z^-1) or inverting H^ee=-(C1/C2)/(1-z^-1); w_I=(C1/C2)f_c"),
            ("note", "key building block; all filters reduce to noninverting+inverting integrators")]),
        ["A-INT-ROLE", "A-INT-NONINV-16", "A-INT-INV-24", "A-INT-CT-1"], ["A-SCAMP-TRANSR-28", "A-INT-NONIDEAL"], "medium", "direct"),
    obj("BLOCK-FIRST-ORDER", "CircuitBlock", "9 > 9.5", "PKG-FIRSTORDER",
        OrderedDict([("name", "first-order SC building block"),
            ("composition", "integrator + a switched capacitor alpha_2 C across the integrating capacitor (damping)"),
            ("function", "lowpass / highpass / allpass first-order sections")]),
        ["A-FO-LP-4", "A-FO-HP-13", "A-FO-APPROACH"], [], "medium", "direct"),
    obj("BLOCK-BIQUAD", "CircuitBlock", "9 > 9.6", "PKG-SECONDORDER",
        OrderedDict([("name", "switched-capacitor biquad"),
            ("composition", "noninverting integrator + inverting integrator (one damped) in a feedback loop"),
            ("function", "second-order section realizing complex poles and zeros")]),
        ["A-SO-BIQUAD-CONCEPT", "A-SO-BIQUAD-ALPHA-11", "A-SO-BIQUAD-LOWQ"], [], "medium", "direct"),
    obj("BLOCK-SC-FILTER", "CircuitBlock", "9 > 9.7", "PKG-FILTERS",
        OrderedDict([("name", "switched-capacitor filter"),
            ("composition", "cascade of first-/second-order (biquad) sections, OR a ladder of integrators synthesized from an RLC prototype"),
            ("function", "frequency-selective filtering (lowpass/bandpass/highpass/bandstop)")]),
        ["A-FILT-APP", "A-FILT-CASCADE", "A-FILT-LADDER"], [], "medium", "direct"),

    # --- ComponentModel ---
    obj("COMP-Z-TWOPORTS", "ComponentModel", "9 > 9.4", "PKG-ZMODEL",
        OrderedDict([("name", "SC two-port z-domain models"),
            ("variants", ["parallel/toggle", "positive", "negative", "capacitor-in-series-with-switch", "unswitched capacitor", "capacitor + shunt switch"])]),
        ["A-Z-FOURPORTS"], [], "medium", "direct"),

    # --- DesignPrinciple ---
    obj("PRINCIPLE-CAP-RATIO", "DesignPrinciple", "9", "PKG-OV",
        OrderedDict([("statement", "SC signal-processing accuracy is proportional to the accuracy of capacitor RATIOS (not absolute R/C values); capacitor ratios are the most accurate aspect of CMOS, which is why SC techniques work."),
            ("applies_to", ["analog signal processing in CMOS", "time-constant accuracy"])]),
        ["A-INTRO-ACCURACY"], ["A-SC-ACC-TAUD-22"], "medium", "direct"),
    obj("PRINCIPLE-INTEGRATOR-BASIS", "DesignPrinciple", "9 > 9.3", "PKG-INT",
        OrderedDict([("statement", "All filter design can be reduced to noninverting and inverting integrators; pairing them in a loop also cancels their phase errors."),
            ("applies_to", ["SC filter synthesis"])]),
        ["A-INT-ROLE", "A-INT-PHASE-CANCEL"], [], "medium", "direct"),
    obj("PRINCIPLE-PARASITIC-INSENSITIVE", "DesignPrinciple", "9 > 9.2", "PKG-SCAMP",
        OrderedDict([("statement", "Use positive/negative transresistance circuits so capacitor parasitics sit at a virtual ground or are shorted, making SC amplifiers/integrators insensitive to parasitic capacitance."),
            ("applies_to", ["SC amplifier", "SC integrator"])]),
        ["A-SCAMP-PARASITIC", "A-SCAMP-NOEXCESS"], [], "medium", "direct"),
    obj("PRINCIPLE-FEEDTHRU-FIX", "DesignPrinciple", "9 > 9.2", "PKG-FEEDTHRU",
        OrderedDict([("statement", "Remove the constant feedthrough term by autozeroing and the input-dependent (distortion) term by delaying/modifying the clocks; differential circuits further reduce feedthrough."),
            ("applies_to", ["SC amplifier", "SC integrator", "first-order circuits"]),
            ("tradeoff", "differential operation doubles component count and amplifier complexity")]),
        ["A-SCAMP-FEEDTHRU-FIX"], ["A-FO-DIFF", "A-FO-DIFF-COST"], "medium", "direct"),
    obj("PRINCIPLE-OVERSAMPLING", "DesignPrinciple", "9", "PKG-OV",
        OrderedDict([("statement", "The clock frequency must be much greater than the signal bandwidth (oversampling) so the sampled-data and continuous-time domains are equivalent; this also limits SC filters to signal bands well below op-amp bandwidths."),
            ("applies_to", ["all SC circuits"])]),
        ["A-SUMMARY"], ["A-SC-REQ-12"], "medium", "indirect"),
    obj("PRINCIPLE-FILTER-DESIGN", "DesignPrinciple", "9 > 9.7", "PKG-FILTERS",
        OrderedDict([("statement", "Higher-order SC filters are designed either by cascading first-/second-order sections (cascade) or by synthesizing the state equations of a normalized RLC ladder prototype with integrators (ladder, less capacitor-ratio sensitive)."),
            ("applies_to", ["higher-order SC filter design"])]),
        ["A-FILT-CASCADE", "A-FILT-LADDER"], [], "medium", "direct"),

    # --- PhysicalEffect ---
    obj("PHYS-CLOCK-FEEDTHRU", "PhysicalEffect", "9 > 9.2", "PKG-FEEDTHRU",
        OrderedDict([("name", "clock feedthrough"),
            ("description", "MOSFET switch overlap capacitance C_OL couples the falling/rising clock onto the capacitors, adding an input-dependent distortion term and a constant offset to the output; sets the ultimate accuracy of SC circuits.")]),
        ["A-SCAMP-FEEDTHRU-50"], ["A-SUMMARY"], "medium", "direct"),
    obj("PHYS-FINITE-GAIN", "PhysicalEffect", "9 > 9.3", "PKG-INT-NONIDEAL",
        OrderedDict([("name", "finite op-amp gain error"),
            ("description", "A finite A_vd(0) introduces a gain (magnitude) error in SC amplifiers/integrators; for integrators it also yields a phase error theta(jw), generally the more serious.")]),
        ["A-INT-AVD-ERR-36"], ["A-SCAMP-AVD-52"], "medium", "direct"),
    obj("PHYS-KTC-NOISE", "PhysicalEffect", "9 > 9.3", "PKG-INT-NONIDEAL",
        OrderedDict([("name", "kT/C noise"),
            ("description", "Thermal noise of the switch on-resistance integrated over the RC bandwidth gives an rms noise of kT/C, independent of R_on.")]),
        ["A-INT-KTC-41"], [], "medium", "direct"),

    # --- ExampleSolution ---
    obj("EXAMPLE-9.1-1", "ExampleSolution", "9 > 9.1 > Example 9.1-1", "PKG-EX-911",
        OrderedDict([("title", "Example 9.1-1 Parallel SC Resistor Emulation"),
            ("problem", "C to emulate 1 MOhm at f_c=100kHz."), ("result", "C = T/R = 10pF.")]),
        ["A-EX911-PROBLEM", "A-EX911-USE", "A-EX911-RESULT"], [], "easy", "direct"),
    obj("EXAMPLE-9.1-2", "ExampleSolution", "9 > 9.1 > Example 9.1-2", "PKG-EX-912",
        OrderedDict([("title", "Example 9.1-2 Series-Parallel SC Resistor"),
            ("problem", "C1=C2=C to emulate 1 MOhm at 250kHz."), ("result", "2C=4pF, C=2pF.")]),
        ["A-EX912-PROBLEM", "A-EX912-RESULT"], [], "easy", "direct"),
    obj("EXAMPLE-9.1-3", "ExampleSolution", "9 > 9.1 > Example 9.1-3", "PKG-EX-913",
        OrderedDict([("title", "Example 9.1-3 SC Lowpass z-domain Analysis"),
            ("problem", "H(z) of Fig 9.1-7a."), ("result", "H^oo(z)=z^-1/(1+alpha-alpha z^-1), alpha=C_2/C_1.")]),
        ["A-EX913-PROBLEM", "A-EX913-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-9.2-1", "ExampleSolution", "9 > 9.2 > Example 9.2-1", "PKG-EX-921",
        OrderedDict([("title", "Example 9.2-1 Finite-Gain Accuracy"),
            ("problem", "+10/-10 designs, A_vd(0)=1000."), ("result", "actual gains 9.901 and -9.891.")]),
        ["A-EX921-PROBLEM", "A-EX921-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-9.2-4", "ExampleSolution", "9 > 9.2 > Example 9.2-4", "PKG-EX-924",
        OrderedDict([("title", "Example 9.2-4 Clock Feedthrough"),
            ("given", "C1=10pF, C2=1pF, C_OL=100fF, v_in=0.1V, V_T=1V."),
            ("result", "ideal 1V, input-dependent -5mV, input-independent 100mV.")]),
        ["A-EX924-PROBLEM", "A-EX924-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-9.3-3", "ExampleSolution", "9 > 9.3 > Example 9.3-3", "PKG-EX-933",
        OrderedDict([("title", "Example 9.3-3 Integrator Errors from Finite A_vd(0)"),
            ("given", "f_c=100kHz, w_I=10kHz, A_vd(0)=100."),
            ("result", "C1/C2=0.6283, m=-1.0131, theta=0.554 deg.")]),
        ["A-EX933-PROBLEM", "A-EX933-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-9.5-1", "ExampleSolution", "9 > 9.5 > Example 9.5-1", "PKG-EX-951",
        OrderedDict([("title", "Example 9.5-1 First-Order SC Design"),
            ("problem", "gain +10, -3dB 1kHz, f_c=100kHz."), ("result", "alpha_2=0.0628, alpha_1=0.6283.")]),
        ["A-EX951-PROBLEM", "A-EX951-RESULT"], [], "medium", "direct"),
    obj("EXAMPLE-9.6-1", "ExampleSolution", "9 > 9.6 > Example 9.6-1", "PKG-EX-961",
        OrderedDict([("title", "Example 9.6-1 Low-Q Biquad Design"),
            ("given", "f_o=1kHz, Q=2, bandpass, f_c=100kHz."),
            ("result", "alpha_2=alpha_5=0.0628, alpha_4=alpha_6=0.0314; max ratio 1/31.83; total 52.76 units.")]),
        ["A-EX961-PROBLEM", "A-EX961-RESULT"], [], "medium", "direct"),

    # --- ProblemStatement ---
    obj("PROB-9.1-1", "ProblemStatement", "9 > Homework Problems", "PKG-PROB",
        OrderedDict([("question", "Develop the equivalent resistance of the series SC resistor emulation (Table 9.1-1).")]),
        ["A-PROB-911"], [], "easy", "direct"),
    obj("PROB-9.1-3", "ProblemStatement", "9 > Homework Problems", "PKG-PROB",
        OrderedDict([("question", "Compare time-constant accuracy of an RC vs an SC-resistor + capacitor implementation.")]),
        ["A-PROB-913"], [], "easy", "direct"),
    obj("PROB-9.5-1", "ProblemStatement", "9 > Homework Problems", "PKG-PROB",
        OrderedDict([("question", "Develop the inverting first-order lowpass transfer function from the lowpass circuit with the two leftmost switch phases reversed.")]),
        ["A-PROB-955"], [], "medium", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]
OBJ_BY_ID = {o["id"]: o for o in OBJECTS}

# ===========================================================================
# context_packages: ONE per chunk. atoms == chunk.atom_ids.
# ===========================================================================
def build_package(pid, chunk_id, section_path, linked_context, extraction_targets,
                  expected_objects, expected_local_fields=None, note=None):
    d = OrderedDict([
        ("id", pid), ("profile", "textbook"), ("chunk_id", chunk_id),
        ("section_path", section_path), ("document_title", "CMOS Analog Circuit Design"),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", linked_context),
        ("extraction_targets", extraction_targets),
        ("expected_objects", expected_objects),
    ])
    if expected_local_fields:
        d["expected_local_fields"] = expected_local_fields
    if note:
        d["note"] = note
    return d


def _objs_homed_at(pid):
    return [o["id"] for o in OBJECTS if o["home_package"] == pid]

CONTEXT_PACKAGES = [
    build_package("PKG-OV", "C-OV", "Chapter 9 > 9 Switched Capacitor Circuits (overview/summary)",
        OrderedDict([("previous_heading", "Chapter 8"), ("next_heading", "9.1 Switched Capacitor Circuits")]),
        ["Concept", "DesignPrinciple"], _objs_homed_at("PKG-OV"),
        expected_local_fields=OrderedDict([
            ("PRINCIPLE-OVERSAMPLING", ["statement", "applies_to"]),
        ]),
        note="Chapter intro (A-INTRO) and summary (A-SUMMARY) are context_only framing; only A-INTRO-ACCURACY (capacitor-ratio principle) and the oversampling/clock principle are typed here."),
    build_package("PKG-RES-EMU", "C-RES-EMU", "Chapter 9 > 9.1 Switched Capacitor Circuits > Resistor Emulation",
        OrderedDict([
            ("formula_context", "R = T/C derived by equating i_1(average)=C(V_1-V_2)/T with i_1(average)=(V_1-V_2)/R"),
            ("table", "Table 9.1-1 emulated resistance of four SC resistor circuits (Parallel/Series T/C, Series-Parallel T/(C1+C2), Bilinear T/4C)"),
            ("previous_heading", "Chapter 9 intro"), ("next_heading", "Example 9.1-1")]),
        ["Concept", "Formula", "DesignPrinciple", "CircuitBlock"], _objs_homed_at("PKG-RES-EMU")),
    build_package("PKG-ACCURACY", "C-ACCURACY", "Chapter 9 > 9.1 > Accuracy of Switched Capacitor Circuits",
        OrderedDict([("formula_context", "continuous-time tau_C accuracy (Eq.20) vs discrete-time tau_D accuracy (Eq.22)"),
            ("previous_heading", "Resistor Emulation / Table 9.1-1"), ("next_heading", "Analysis Methods")]),
        ["Formula", "DesignPrinciple"], _objs_homed_at("PKG-ACCURACY"),
        expected_local_fields=OrderedDict([
            ("PRINCIPLE-CAP-RATIO", []),
        ]) if False else None),
    build_package("PKG-Z-ANALYSIS", "C-Z-ANALYSIS", "Chapter 9 > 9.1 > Analysis Methods (z-transform)",
        OrderedDict([("formula_context", "one-sided z-transform definition (Eq.25) and SC lowpass H^oo(z) (Eq.37)"),
            ("previous_heading", "Accuracy of Switched Capacitor Circuits"), ("next_heading", "Example 9.1-3")]),
        ["Formula"], _objs_homed_at("PKG-Z-ANALYSIS")),
    build_package("PKG-EX-911", "C-EX-911", "Chapter 9 > 9.1 > Example 9.1-1",
        OrderedDict([("formula_context", "Eq.(12) R = T/C"), ("previous_heading", "Resistor Emulation"), ("next_heading", "Example 9.1-2")]),
        ["ExampleProblem", "ExampleSolution"], _objs_homed_at("PKG-EX-911")),
    build_package("PKG-EX-912", "C-EX-912", "Chapter 9 > 9.1 > Example 9.1-2",
        OrderedDict([("formula_context", "Eq.(17) R = T/(C_1+C_2)"), ("previous_heading", "Example 9.1-1"), ("next_heading", "Table 9.1-1")]),
        ["ExampleSolution"], _objs_homed_at("PKG-EX-912")),
    build_package("PKG-EX-913", "C-EX-913", "Chapter 9 > 9.1 > Example 9.1-3",
        OrderedDict([("formula_context", "SC lowpass H^oo(z) (Eq.37)"), ("previous_heading", "Analysis Methods"), ("next_heading", "9.2 Switched Capacitor Amplifiers")]),
        ["ExampleSolution"], _objs_homed_at("PKG-EX-913")),
    build_package("PKG-AMP-CT", "C-AMP-CT", "Chapter 9 > 9.2 > Continuous-time / Charge Amplifiers",
        OrderedDict([("formula_context", "continuous-time gains (Eq.1/2), op-amp gain model (Eq.3), finite-gain inverting (Eq.6), charge amp (Eq.12)"),
            ("previous_heading", "9.2 Switched Capacitor Amplifiers"), ("next_heading", "Switched Capacitor Amplifiers")]),
        ["Formula", "PhysicalEffect"], _objs_homed_at("PKG-AMP-CT"),
        note="All atoms here are continuous-time/charge-amplifier prerequisites; no typed object is homed here (the SC amplifier objects live in PKG-SCAMP). Context-only support block."),
    build_package("PKG-SCAMP", "C-SCAMP", "Chapter 9 > 9.2 > Switched Capacitor Amplifiers",
        OrderedDict([("formula_context", "SC amplifier H(z) = -(C_1/C_2) z^-1 (Eq.19/23); transresistance R_T = +/- T/C (Eq.28/29)"),
            ("previous_heading", "Charge Amplifiers"), ("next_heading", "Clock Feedthrough")]),
        ["CircuitBlock", "Formula", "DesignPrinciple"], _objs_homed_at("PKG-SCAMP")),
    build_package("PKG-FEEDTHRU", "C-FEEDTHRU", "Chapter 9 > 9.2 > Clock Feedthrough / Finite Gain",
        OrderedDict([("formula_context", "clock-feedthrough output (Eq.50); finite-gain SC amp H^oe(z) (Eq.52)"),
            ("previous_heading", "Switched Capacitor Amplifiers"), ("next_heading", "Example 9.2-1")]),
        ["PhysicalEffect", "Formula", "DesignPrinciple"], _objs_homed_at("PKG-FEEDTHRU")),
    build_package("PKG-EX-921", "C-EX-921", "Chapter 9 > 9.2 > Example 9.2-1",
        OrderedDict([("formula_context", "finite-gain amp (Eq.5/6)"), ("previous_heading", "Switched Capacitor Amplifiers"), ("next_heading", "Example 9.2-2")]),
        ["ExampleSolution"], _objs_homed_at("PKG-EX-921")),
    build_package("PKG-EX-924", "C-EX-924", "Chapter 9 > 9.2 > Example 9.2-4",
        OrderedDict([("formula_context", "clock-feedthrough output (Eq.50)"), ("previous_heading", "Example 9.2-3"), ("next_heading", "Finite gain / GB / slew rate")]),
        ["ExampleSolution"], _objs_homed_at("PKG-EX-924")),
    build_package("PKG-INT", "C-INT", "Chapter 9 > 9.3 Switched Capacitor Integrators",
        OrderedDict([
            ("formula_context", "SC integrators obtained by replacing R_1 of the continuous-time integrator with positive/negative transresistance circuits of Fig 9.2-6"),
            ("previous_heading", "Continuous Time Integrators"), ("next_heading", "Nonideal Characteristics of Switched Capacitor Integrators")]),
        ["Formula", "CircuitBlock", "DesignPrinciple"], _objs_homed_at("PKG-INT")),
    build_package("PKG-INT-NONIDEAL", "C-INT-NONIDEAL", "Chapter 9 > 9.3 > Nonideal Characteristics",
        OrderedDict([("formula_context", "integrator finite-gain m(jw)/theta(jw) (Eq.36/37); kT/C noise (Eq.41)"),
            ("previous_heading", "Switched Capacitor Integrators"), ("next_heading", "Example 9.3-3")]),
        ["PhysicalEffect", "Formula"], _objs_homed_at("PKG-INT-NONIDEAL"),
        expected_local_fields=OrderedDict([
            ("PHYS-FINITE-GAIN", ["name", "description"]),
        ])),
    build_package("PKG-EX-933", "C-EX-933", "Chapter 9 > 9.3 > Example 9.3-3",
        OrderedDict([("formula_context", "integrator finite-gain error (Eq.36/37); w_I (Eq.19)"), ("previous_heading", "Nonideal Characteristics"), ("next_heading", "9.4 z-domain Models")]),
        ["ExampleSolution"], _objs_homed_at("PKG-EX-933")),
    build_package("PKG-ZMODEL", "C-ZMODEL", "Chapter 9 > 9.4 z-domain Models of Two-Phase SC Circuits",
        OrderedDict([("formula_context", "three z-domain admittance types (Eq.1/2/3), Y = C; catalog of two-port + irreducible models (Fig 9.4-3/9.4-4)"),
            ("previous_heading", "Example 9.3-3"), ("next_heading", "9.5 First-Order SC Circuits")]),
        ["Concept", "Formula", "ComponentModel"], _objs_homed_at("PKG-ZMODEL")),
    build_package("PKG-FIRSTORDER", "C-FIRSTORDER", "Chapter 9 > 9.5 First-Order Switched Capacitor Circuits",
        OrderedDict([("formula_context", "general first-order TF (Eq.1/2); SC lowpass (Eq.4/5); SC highpass (Eq.13)"),
            ("previous_heading", "9.5 First-Order SC Circuits"), ("next_heading", "Example 9.5-1")]),
        ["Formula", "CircuitBlock", "DesignPrinciple"], _objs_homed_at("PKG-FIRSTORDER")),
    build_package("PKG-EX-951", "C-EX-951", "Chapter 9 > 9.5 > Example 9.5-1",
        OrderedDict([("formula_context", "first-order SC lowpass (Eq.4) with z^-1 ~= 1 - sT approximation"), ("previous_heading", "First-Order Circuits"), ("next_heading", "High Pass Circuit")]),
        ["ExampleSolution"], _objs_homed_at("PKG-EX-951")),
    build_package("PKG-SECONDORDER", "C-SECONDORDER", "Chapter 9 > 9.6 Second-Order Switched Capacitor Circuits",
        OrderedDict([("formula_context", "continuous-time biquad H_a(s) (Eq.1); low-Q biquad capacitor ratios (Eq.11)"),
            ("previous_heading", "9.6 Second-Order SC Circuits"), ("next_heading", "Example 9.6-1")]),
        ["CircuitBlock", "Formula", "DesignPrinciple", "Concept"], _objs_homed_at("PKG-SECONDORDER")),
    build_package("PKG-EX-961", "C-EX-961", "Chapter 9 > 9.6 > Example 9.6-1",
        OrderedDict([("formula_context", "low-Q biquad capacitor ratios (Eq.11)"), ("previous_heading", "Low-Q Biquad"), ("next_heading", "High-Q Biquad")]),
        ["ExampleSolution"], _objs_homed_at("PKG-EX-961")),
    build_package("PKG-FILTERS", "C-FILTERS", "Chapter 9 > 9.7 Switched Capacitor Filters",
        OrderedDict([("formula_context", "three filter properties (passband ripple/transition/stopband); cascade vs ladder design routes"),
            ("previous_heading", "9.7 Switched Capacitor Filters"), ("next_heading", "9.8 Summary")]),
        ["Concept", "DesignPrinciple", "CircuitBlock"], _objs_homed_at("PKG-FILTERS")),
    build_package("PKG-PROB", "C-PROB", "Chapter 9 > Homework Problems",
        OrderedDict([("previous_heading", "9.8 Summary"), ("next_heading", "References")]),
        ["ProblemStatement"], _objs_homed_at("PKG-PROB")),
]

# ===========================================================================
# mentions (same set as v0.1; atom_id targets preserved).
# ===========================================================================
def mention(mid, text, mtype, atom_id, canonical_key):
    return OrderedDict([("id", mid), ("text", text), ("type", mtype), ("atom_id", atom_id), ("canonical_key", canonical_key)])

MENTIONS = [
    mention("M-01", "switched capacitor circuit", "Concept", "A-INTRO", "switched_capacitor_circuit"),
    mention("M-02", "capacitor ratio accuracy", "DesignPrinciple", "A-INTRO-ACCURACY", "capacitor_ratio_accuracy"),
    mention("M-03", "nonoverlapping clock", "Concept", "A-SC-NONOVERLAP", "nonoverlapping_clock"),
    mention("M-04", "R = T/C", "Formula", "A-SC-REQ-12", "equivalent_resistance_req"),
    mention("M-05", "parallel SC resistor", "CircuitBlock", "A-SC-RES-CONCEPT", "sc_resistor"),
    mention("M-06", "SC amplifier gain -C1/C2", "Formula", "A-SCAMP-HZ-19", "sc_amp_gain"),
    mention("M-07", "transresistance T/C", "CircuitBlock", "A-SCAMP-TRANSR-28", "sc_transresistance"),
    mention("M-08", "clock feedthrough", "PhysicalEffect", "A-SCAMP-FEEDTHRU-50", "clock_feedthrough"),
    mention("M-09", "switched capacitor integrator", "CircuitBlock", "A-INT-NONINV-16", "sc_integrator"),
    mention("M-10", "integrator frequency w_I", "Variable", "A-INT-WI-19", "integrator_frequency"),
    mention("M-11", "kT/C noise", "PhysicalEffect", "A-INT-KTC-41", "ktc_noise"),
    mention("M-12", "biquad", "CircuitBlock", "A-SO-BIQUAD-CONCEPT", "biquad"),
    mention("M-13", "switched capacitor filter", "CircuitBlock", "A-FILT-APP", "sc_filter"),
    mention("M-14", "ladder filter", "CircuitBlock", "A-FILT-LADDER", "ladder_filter"),
    mention("M-15", "first-order lowpass", "CircuitBlock", "A-FO-LP-4", "first_order_lowpass"),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "capacitor_ratio_accuracy"),
                 ("aliases", ["capacitor ratio accuracy", "accuracy proportional to capacitor ratios", "relative accuracy of capacitors"])]),
    OrderedDict([("canonical", "clock_feedthrough"),
                 ("aliases", ["clock feedthrough", "feedthrough", "charge injection", "switch overlap-capacitor feedthrough"])]),
    OrderedDict([("canonical", "sc_integrator"),
                 ("aliases", ["switched capacitor integrator", "SC integrator", "noninverting integrator", "inverting integrator"]),
                 ("note", "noninverting/inverting are two variants of the same building block (left two switches differ in phase only).")]),
    OrderedDict([("canonical", "equivalent_resistance_req"),
                 ("aliases", ["R = T/C", "R_eq", "equivalent resistance", "emulated resistance", "1/(f_c C)"])]),
]

# ===========================================================================
# relations (same R-01..R-41; endpoints preserved).
# ===========================================================================
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])

RELATIONS = [
    # resistor-emulation formula + block
    rel("R-01", "formula_defines_variable", "FORMULA-REQ", "BLOCK-SC-RESISTOR", ["A-SC-REQ-12", "A-SC-RES-CONCEPT"], "easy", "direct"),
    rel("R-02", "formula_derived_from_formula", "FORMULA-REQ", "FORMULA-REQ-SP", ["A-SC-REQ-12", "A-SC-REQ-SP-17"], "medium", "indirect"),
    rel("R-03", "formula_used_in_example", "FORMULA-REQ", "EXAMPLE-9.1-1", ["A-EX911-USE", "A-EX911-RESULT"], "easy", "direct"),
    rel("R-04", "formula_used_in_example", "FORMULA-REQ-SP", "EXAMPLE-9.1-2", ["A-EX912-RESULT"], "medium", "direct"),
    rel("R-05", "formula_used_in_example", "FORMULA-SC-LPF", "EXAMPLE-9.1-3", ["A-EX913-RESULT"], "medium", "direct"),
    # core design principles
    rel("R-06", "design_principle_applies_to_scenario", "PRINCIPLE-CAP-RATIO", "CONCEPT-SC-CIRCUIT", ["A-INTRO-ACCURACY"], "medium", "direct"),
    rel("R-07", "design_principle_applies_to_scenario", "PRINCIPLE-CAP-RATIO", "FORMULA-TAUD-ACC", ["A-SC-ACC-TAUD-22"], "medium", "direct"),
    rel("R-08", "design_principle_has_tradeoff", "PRINCIPLE-CAP-RATIO", "PHYS-CLOCK-FEEDTHRU", ["A-INTRO-ACCURACY", "A-SUMMARY"], "hard", "indirect"),
    rel("R-09", "design_principle_applies_to_scenario", "PRINCIPLE-OVERSAMPLING", "FORMULA-REQ", ["A-SUMMARY", "A-SC-REQ-12"], "medium", "indirect"),
    # amplifier composition + gain
    rel("R-10", "circuit_block_composed_of_block", "BLOCK-SC-AMPLIFIER", "BLOCK-TRANSRESISTANCE", ["A-SCAMP-EVOLVE", "A-SCAMP-PARASITIC"], "medium", "direct"),
    rel("R-11", "circuit_block_composed_of_block", "BLOCK-TRANSRESISTANCE", "BLOCK-SC-RESISTOR", ["A-SCAMP-TRANSR-28"], "medium", "indirect"),
    rel("R-12", "formula_defines_variable", "FORMULA-SC-AMP-GAIN", "BLOCK-SC-AMPLIFIER", ["A-SCAMP-HZ-19"], "medium", "direct"),
    rel("R-13", "design_principle_applies_to_scenario", "PRINCIPLE-PARASITIC-INSENSITIVE", "BLOCK-SC-AMPLIFIER", ["A-SCAMP-PARASITIC"], "medium", "direct"),
    rel("R-14", "formula_used_in_example", "FORMULA-SC-AMP-GAIN", "EXAMPLE-9.2-1", ["A-EX921-RESULT"], "medium", "indirect"),
    # clock feedthrough physical effect
    rel("R-15", "component_has_property", "BLOCK-SC-AMPLIFIER", "PHYS-CLOCK-FEEDTHRU", ["A-SCAMP-FEEDTHRU-50"], "medium", "direct"),
    rel("R-16", "formula_defines_variable", "FORMULA-FEEDTHRU", "PHYS-CLOCK-FEEDTHRU", ["A-SCAMP-FEEDTHRU-50"], "medium", "direct"),
    rel("R-17", "formula_used_in_example", "FORMULA-FEEDTHRU", "EXAMPLE-9.2-4", ["A-EX924-RESULT"], "medium", "direct"),
    rel("R-18", "design_principle_applies_to_scenario", "PRINCIPLE-FEEDTHRU-FIX", "PHYS-CLOCK-FEEDTHRU", ["A-SCAMP-FEEDTHRU-FIX"], "medium", "direct"),
    rel("R-19", "design_principle_has_tradeoff", "PRINCIPLE-FEEDTHRU-FIX", "BLOCK-FIRST-ORDER", ["A-FO-DIFF", "A-FO-DIFF-COST"], "hard", "indirect"),
    # integrator block + formulas + errors
    rel("R-20", "circuit_block_composed_of_block", "BLOCK-SC-INTEGRATOR", "BLOCK-TRANSRESISTANCE", ["A-INT-NONINV-16", "A-SCAMP-TRANSR-28"], "medium", "indirect"),
    rel("R-21", "formula_defines_variable", "FORMULA-INT-NONINV", "BLOCK-SC-INTEGRATOR", ["A-INT-NONINV-16"], "medium", "direct"),
    rel("R-22", "formula_defines_variable", "FORMULA-INT-INV", "BLOCK-SC-INTEGRATOR", ["A-INT-INV-24"], "medium", "direct"),
    rel("R-23", "formula_defines_variable", "FORMULA-INT-WI", "BLOCK-SC-INTEGRATOR", ["A-INT-WI-19"], "medium", "direct"),
    rel("R-24", "component_has_property", "BLOCK-SC-INTEGRATOR", "PHYS-FINITE-GAIN", ["A-INT-AVD-ERR-36"], "medium", "direct"),
    rel("R-25", "component_has_property", "BLOCK-SC-INTEGRATOR", "PHYS-KTC-NOISE", ["A-INT-KTC-41"], "medium", "direct"),
    rel("R-26", "formula_used_in_example", "FORMULA-INT-AVD-ERR", "EXAMPLE-9.3-3", ["A-EX933-RESULT"], "medium", "direct"),
    rel("R-27", "design_principle_applies_to_scenario", "PRINCIPLE-INTEGRATOR-BASIS", "BLOCK-SC-INTEGRATOR", ["A-INT-ROLE", "A-INT-PHASE-CANCEL"], "medium", "direct"),
    # first-order -> second-order -> filter hierarchy
    rel("R-28", "circuit_block_composed_of_block", "BLOCK-FIRST-ORDER", "BLOCK-SC-INTEGRATOR", ["A-FO-LP-4", "A-FO-APPROACH"], "medium", "direct"),
    rel("R-29", "formula_used_in_example", "FORMULA-FO-LP", "EXAMPLE-9.5-1", ["A-EX951-RESULT"], "medium", "direct"),
    rel("R-30", "circuit_block_composed_of_block", "BLOCK-BIQUAD", "BLOCK-SC-INTEGRATOR", ["A-SO-BIQUAD-CONCEPT"], "medium", "direct"),
    rel("R-31", "formula_defines_variable", "FORMULA-BIQUAD-ALPHA", "BLOCK-BIQUAD", ["A-SO-BIQUAD-ALPHA-11"], "medium", "direct"),
    rel("R-32", "formula_used_in_example", "FORMULA-BIQUAD-ALPHA", "EXAMPLE-9.6-1", ["A-EX961-RESULT"], "medium", "direct"),
    rel("R-33", "circuit_block_composed_of_block", "BLOCK-SC-FILTER", "BLOCK-BIQUAD", ["A-FILT-CASCADE", "A-SO-CASCADE"], "medium", "direct"),
    rel("R-34", "circuit_block_composed_of_block", "BLOCK-SC-FILTER", "BLOCK-FIRST-ORDER", ["A-FILT-CASCADE"], "medium", "indirect"),
    rel("R-35", "circuit_block_composed_of_block", "BLOCK-SC-FILTER", "BLOCK-SC-INTEGRATOR", ["A-FILT-LADDER"], "medium", "direct"),
    rel("R-36", "design_principle_applies_to_scenario", "PRINCIPLE-FILTER-DESIGN", "BLOCK-SC-FILTER", ["A-FILT-CASCADE", "A-FILT-LADDER"], "medium", "direct"),
    rel("R-37", "design_principle_has_tradeoff", "PRINCIPLE-FILTER-DESIGN", "BLOCK-SC-FILTER", ["A-FILT-LADDER"], "hard", "indirect"),
    # z-domain models
    rel("R-38", "formula_defines_variable", "FORMULA-Z-ADMITTANCE", "COMP-Z-TWOPORTS", ["A-Z-ADMITTANCE", "A-Z-FOURPORTS"], "medium", "indirect"),
    rel("R-39", "concept_defines_term", "CONCEPT-Z-MODEL", "COMP-Z-TWOPORTS", ["A-Z-MODEL-CONCEPT"], "medium", "direct"),
    # homework -> example/formula
    rel("R-40", "problem_extends_example", "PROB-9.1-1", "EXAMPLE-9.1-2", ["A-PROB-911"], "medium", "indirect"),
    rel("R-41", "problem_extends_example", "PROB-9.5-1", "EXAMPLE-9.5-1", ["A-PROB-955"], "medium", "indirect"),
]

# ===========================================================================
# do_not_extract: bracket [n] citations + policy, Fig./Eq. cross-refs,
# <details> chemical/text_image OCR labels, the big 9.7 PSpice netlist <table>,
# image markup, out-of-chapter cross-refs.
# ===========================================================================
DO_NOT_EXTRACT = [
    OrderedDict([("pattern", "bracket_reference_citation"),
        ("examples", ["[1,2]", "[3]", "[4]", "[7]", "[9,10,11]", "[12]", "[15, 16]", "[24,25]"]),
        ("reason", "inline numeric reference citations are not knowledge objects."),
        ("kind", "citation_policy")]),
    OrderedDict([("pattern", "figure_or_equation_cross_reference"),
        ("examples", ["Fig. 9.1-1", "Fig. 9.2-6", "Fig. 9.3-4", "Table 9.3-1", "Eq. (12)", "Eq. (50)"]),
        ("reason", "cross-references to figures/equations/tables are pointers, not objects to extract."),
        ("kind", "reference")]),
    OrderedDict([("text", "i₁(t) φ₁ φ₂ i₂(t)"), ("source_lines", [7517, 7517]),
        ("reason", "OCR'd node labels inside a <details> text_image figure block (Fig 9.1-1); SC schematic labels are scrambled and not knowledge objects."),
        ("kind", "figure_label")]),
    OrderedDict([("pattern", "details_figure_ocr_label"),
        ("examples", ["v₁(t) v_C(t) v₂(t)", "S₁ S₂", "φ₂ φ₁ φ₂ φ₁ φ₂", "Electrical circuit diagram with capacitors and switches labeled C1, C2, φ1, φ2, v1, v2", "Op amp with finite value of A_vd(0)"]),
        ("reason", "<details> chemical/text_image OCR blocks for SC schematics are noisy (node order scrambled, e/o superscripts lost); not knowledge objects."),
        ("kind", "figure_label")]),
    OrderedDict([("pattern", "image_markup"),
        ("examples", ["![](images/<hash>.jpg)", '<img src="images/<hash>.jpg"/>']),
        ("reason", "image embeds carry no extractable text."),
        ("kind", "image_markup")]),
    OrderedDict([("text", "PSpice 5.2 (Jul 1992)"), ("source_lines", [12294, 12294]),
        ("reason", "the 9.7 ladder-filter PSpice netlist is a single huge HTML <table> of node/subcircuit cards (NC11, PC1, DELAY, .SUBCKT, .AC ...); pure noise, excluded."),
        ("kind", "reference")]),
    OrderedDict([("text", "discussed in Chap. 8"), ("atom_id", "A-SCAMP-FEEDTHRU-FIX"),
        ("reason", "backward cross-reference to Chapter 8 (autozeroing), outside this chapter slice."),
        ("kind", "cross_reference")]),
    OrderedDict([("pattern", "out_of_chapter_cross_reference"),
        ("examples", ["In Section 5.1", "discussed in Chap. 8", "used extensively in the next chapter", "reference [7]"]),
        ("reason", "references to other chapters/sections/appendices are not Chapter-9 objects."),
        ("kind", "cross_reference")]),
]

# ===========================================================================
# source_meta
# ===========================================================================
SOURCE_META = OrderedDict([
    ("source_id", "cmos"),
    ("scope", "Chapter 9 (Switched Capacitor Circuits) only"),
    ("title", "CMOS Analog Circuit Design - Allen & Holberg - Chapter 9 Switched Capacitor Circuits"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines %d-%d; NOT the authoritative source)" % (SLICE_LINE_START, SLICE_LINE_END)),
    ("profile", "textbook"),
    ("profile_detected_as", "textbook"),
    ("profile_cues", ["Chapter 9", "Example 9.1-1", "Example 9.2-4", "Eq. (12)", "Fig. 9.1-1", "Table 9.1-1", "9.8 - Summary", "Homework Problems"]),
    ("extraction_targets", ["Concept", "Formula", "Variable", "CircuitBlock", "ComponentModel",
                            "PhysicalEffect", "DesignPrinciple", "Derivation", "ExampleProblem",
                            "ExampleSolution", "ProblemStatement"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file=%s). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate." % SOURCE_NAME),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span: it may rephrase, transliterate math (z^-1, omega, alpha, phi, tau, +-), and resolve in-span pronouns, but MUST NOT introduce facts/names/section-refs not inside the span. MinerU spaces digits inside math ('1 0 ^ {- 5}', '0. 6 2 8 3') and uses LaTeX in $...$ / $$...$$; normalized gives the readable ascii form. EXCEPTION: a formula_atom MAY carry symbolic interpretation/context via metadata.note (cross-tag glue) but the normalized formula itself stays formula-only."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside its home_package's chunk) + supporting_context_atom_ids (atoms from other chunks). Package object-recall (expected_objects) is scored on local evidence only."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = payload fields derivable from that package alone."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of a decimal confidence."),
        ("tables_and_figures", "Tables render as HTML <table> (Table 9.1-1, Table 9.3-1) sometimes with $...$ inline formulas; the 9.7 PSpice netlist is a huge noise <table> (excluded). SC schematics render as <details> chemical/text_image OCR blocks with scrambled node labels; figure-internal labels and inline [n] citations are listed in do_not_extract."),
        ("context_only", "an atom with context_only:true is background/framing (chapter intro A-INTRO, summary A-SUMMARY) that should NOT yield a typed object on its own; its home package may legitimately list no expected_object for it."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects are the objects whose home_package is that package. PKG-AMP-CT is an all-context prerequisite block (no homed object); its note records this."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
        ("numeric_audit", "for every non-formula atom: every numeric token in normalized_text must also occur in its raw_text span (no over-span numbers)."),
    ])),
    ("parsing_notes", [
        "Formula tags reset per sub-section (9.1 has tag1..51, 9.2 restarts at 1, 9.3/9.5/9.6/9.7 likewise); a formula atom is located by tag WITHIN its sub-section's line window (e.g. R=T/C is Eq.12 in 9.1 @ line 7628; SC amp gain is Eq.19 in 9.2 @ line 8384).",
        "MinerU spaces out digits inside math ('1 0 ^ {- 5}', '0. 6 2 8 3', '9 . 9 0 1') and renders variables as 'C _ {1}', '\\nu _ {1}', '\\phi _ {1}'; the LaTeX in formula raw_text is verbatim and may look scrambled.",
        "z-domain superscripts H^{oo}/H^{ee}/H^{oe} (even/odd phase) survive in the display formulas (Eq.37/16/24/19) but the e/o tags on inline variables and inside <details> OCR are often lost.",
        "SC schematics render as <details> chemical/text_image blocks whose OCR is noisy: node labels (phi_1/phi_2, C_1/C_2, v_C) are scrambled and the topology is unreliable, so all knowledge is taken from prose + numbered formulas, never the figure OCR.",
        "Table 9.1-1 (four SC resistor equivalents) and Table 9.3-1 (GB influence) are HTML <table> with $...$ inline formulas; the 9.7 ladder-filter PSpice netlist (starting at line ~12294) is one giant HTML <table> of SPICE cards and is excluded as noise.",
        "The viewer slice (lines 7496-13705, ~6200 lines) is large because this is the whole chapter; only the ~69 v0.1 atoms are spanned, located via each atom's sub-section line window.",
    ]),
])

# ===========================================================================
# Assemble (ordered) and dump.
# ===========================================================================
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
        "# Gold fixture v0.3.3-textbook - CMOS Analog Circuit Design Chapter 9 Switched Capacitor Circuits (profile: textbook)\n"
        "# Upgraded from v0.1: dual coordinates (source_span authoritative + viewer_span), verbatim raw_text +\n"
        "# within-span normalized_text (formula/condition normalized are formula-only), object evidence split into\n"
        "# local/supporting + home_package, ONE context_package per chunk with expected_objects (+ expected_local_fields\n"
        "# for cross-package objects), gold_label/difficulty/evidence_strength (no decimal confidence), do_not_extract\n"
        "# negatives, source_meta conventions/validation/parsing_notes. Same atom/chunk/object/relation IDs and meaning\n"
        "# as v0.1. Spans are computed by build.py against the MinerU .md; do NOT hand-edit.\n"
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
