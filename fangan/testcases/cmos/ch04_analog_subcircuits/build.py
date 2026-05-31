#!/usr/bin/env python3
"""Builder for ch04_analog_subcircuits gold.yaml (schema v0.3.3-textbook).

Upgrades the Chapter 4 (Analog CMOS Subcircuits) textbook fixture from v0.1 to
v0.3.3-textbook. Keeps every v0.1 atom/chunk/object/relation ID and its meaning
(the knowledge content / IDs from the v0.1 gold); only re-expresses them in the
v0.3.3 schema and adds verbatim dual coordinates (authoritative source_span into
the MinerU .md + viewer_span into source.md). TEXTBOOK atom/object/relation types
are retained.

All spans are computed by locating verbatim substrings (anchors) in the
authoritative MinerU source file; YAML spans are NEVER hand-written.

Authoritative slice: source_file lines 4873-7479 (Chapter 4: 4.1 .. 4.7 + Problems).

Source quirks:
 - Fig 4.0-1 is a <details><summary>flowchart</summary> mermaid graph TD </details>
   block (op-amp composition). The op-amp hierarchy ATOM (A-HIER-OPAMP) anchors on
   the PROSE paragraph (line 4877) that states the composition, NOT the mermaid; the
   raw mermaid node labels ("Operational Amplifier", "Biasing Circuits", ...) are
   listed in do_not_extract as figure_label (figure-internal node text).
 - Formula tags reset per sub-section (4.1 tag1..8, 4.2 tag1..6, 4.3 restarts at 1,
   4.4 restarts, ...). Each formula atom is located by its verbatim $$ line WITHIN
   its sub-section window, so identical-looking tags never collide.
 - MinerU spaces digits inside math ('1 9. 7', '0. 0 5', '2 5 \\times 1 0 ^ {9}')
   and renders inline math in $...$; normalized_text gives the readable ascii form.

Run:  python3 build.py        (writes source.md + gold.yaml)
"""
import os
import re
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = ("/Users/hzf/workspace/pdf_parser/notebook_papers_mineru_skill_results/"
               "CMOS_Analog_Circuit_Design_-_Allen_Holberg/"
               "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md")
SOURCE_NAME = "CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md"
VIEWER_NAME = "source.md"

SLICE_LINE_START = 4873
SLICE_LINE_END = 7479

# ---------------------------------------------------------------------------
# Load source; build the viewer slice (verbatim + header comment).
# ---------------------------------------------------------------------------
SRC = open(SOURCE_FILE, encoding="utf-8").read()
SRC_LINES = SRC.split("\n")

VIEWER_HEADER = (
    "<!-- source.md = VIEWER-ONLY verbatim slice of {src}, original lines {a}-{b} (Chapter 4).\n"
    "     Authoritative gold coordinates are in gold.yaml under each atom's source_span (file=the mineru .md).\n"
    "     viewer_span here is optional/debug. -->\n"
).format(src=SOURCE_NAME, a=SLICE_LINE_START, b=SLICE_LINE_END)

VIEWER_BODY = "\n".join(SRC_LINES[SLICE_LINE_START - 1:SLICE_LINE_END]) + "\n"
VIEWER_TEXT = VIEWER_HEADER + VIEWER_BODY


def line_of_offset(text, off):
    return text.count("\n", 0, off) + 1


_SLICE_SRC_START = sum(len(SRC_LINES[i]) + 1 for i in range(SLICE_LINE_START - 1))
_SLICE_SRC_END = _SLICE_SRC_START + len(VIEWER_BODY)  # body length matches slice text


def _window_bounds(line_lo, line_hi):
    """Char offsets in SRC for [line_lo, line_hi] inclusive (1-based)."""
    start = sum(len(SRC_LINES[i]) + 1 for i in range(line_lo - 1))
    end = sum(len(SRC_LINES[i]) + 1 for i in range(line_hi))
    return start, end


def span_for(raw, win=None):
    """raw must be a verbatim, contiguous substring of the slice, unique within
    its search window. `win=(lo,hi)` restricts the search to source lines lo..hi
    (used for per-sub-section formula tags that repeat across sections).

    Returns (source_span, viewer_span)."""
    if win is None:
        lo, hi = SLICE_LINE_START, SLICE_LINE_END
    else:
        lo, hi = win
    w_start, w_end = _window_bounds(lo, hi)
    region = SRC[w_start:w_end]
    idx = region.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND in window %r: %r" % ((lo, hi), raw[:70]))
    if region.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS (>1 match) in window %r: %r" % ((lo, hi), raw[:70]))
    src_cs = w_start + idx
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "source span mismatch: %r" % raw[:40]

    # viewer
    v_idx = VIEWER_TEXT.find(raw)
    assert v_idx >= 0, "viewer locate fail: %r" % raw[:40]
    if VIEWER_TEXT.find(raw, v_idx + 1) >= 0:
        # disambiguate viewer by mapping the source offset through the slice shift
        shift = len(VIEWER_HEADER) - _SLICE_SRC_START
        v_idx = src_cs + shift
    v_cs = v_idx
    v_ce = v_cs + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "viewer span mismatch: %r" % raw[:40]

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


ATOMS = []


def atom(aid, section_id, atom_type, se_id, raw, normalized, win=None,
         evidence_strength="direct", metadata=None):
    ss, vs = span_for(raw, win)
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
    ATOMS.append(d)
    return d


# sub-section line windows (for tag disambiguation)
W41 = (4906, 5321)
W42 = (5322, 5445)
W43 = (5446, 5895)
W44 = (5896, 6343)
W45 = (6344, 6778)
W46 = (6779, 7057)
W47 = (7058, 7065)
WPR = (7066, 7463)
W40 = (4873, 4905)

# ===========================================================================
# 4.0 chapter intro / op-amp hierarchy
# ===========================================================================
atom("A-INTRO-SUB", "SEC-4", "concept_definition_atom", "SE-4-INTRO",
     "These simple circuits consist of one or more transistors; they are simple; and they generally perform only one function. A subcircuit is typically combined with other simple circuits to generate a more complex circuit function. Consequently, the circuits of this and the next chapter can be considered as building blocks.",
     "Subcircuits are simple circuits of one or more transistors that perform one function; they are combined with other simple circuits to generate more complex circuit functions, so they are the building blocks of this and the next chapter.",
     win=W40, metadata={"context_only": True})

atom("A-HIER-OPAMP", "SEC-4", "concept_definition_atom", "SE-4-HIER",
     "Working our way backward, we note that one of the stages of an op amp is the differential amplifier. The differential amplifier consists of simple circuits that might include a current sink, a current-mirror load, and a sourcecoupled pair. Another stage of the op amp is a second gain stage, which might consist of an inverter and a current-sink load. If the op amp is to be able to drive a low-impedance load, an output stage is necessary. The output stage might consist of a source follower and a current-sink load. It is also necessary to provide a stabilized bias for each of the previous stages. The biasing stage could consist of a current sink and current mirrors to distribute the bias currents to the other stages.",
     "Op-amp hierarchy (Fig 4.0-1): an operational amplifier is composed of biasing circuits, an input differential amplifier, a second gain stage, and an output stage. The differential amplifier consists of a current sink, a current-mirror load, and a source-coupled pair; the second gain stage of an inverter and a current-sink load; the output stage of a source follower and a current-sink load; the biasing stage of a current sink and current mirrors.",
     win=W40, metadata={"figure": "Fig 4.0-1 op-amp hierarchy (rendered as a <details> mermaid flowchart; this atom anchors on the prose statement of the composition, not the mermaid node labels)"})

# ===========================================================================
# 4.1 MOS Switch
# ===========================================================================
atom("A-SW-MODEL", "SEC-4.1", "component_model_atom", "SE-4.1-MODEL",
     "The most important characteristics of a switch are its ON resistance, $r _ { \\mathrm { O N } }$ , and its OFF resistance , $r _ { \\mathrm { O F F } }$ . Ideally $r _ { \\mathrm { O N } }$ is zero and $r _ { \\mathrm { O F F } }$ is infinite.",
     "The most important switch characteristics are its ON resistance r_ON and OFF resistance r_OFF; ideally r_ON is zero and r_OFF is infinite (a voltage-controlled switch, Fig 4.1-1, with offset V_OS, OFF leakage I_OFF, terminal leakage I_A/I_B, and parasitic capacitors C_A,C_B,C_AB,C_AC,C_BC).",
     win=W41)

atom("A-SW-ID-1", "SEC-4.1", "formula_atom", "SE-4.1-EQ1",
     "I _ {D} = \\frac {K ^ {\\prime} W}{L} \\left[ \\left(V _ {G S} - V _ {T}\\right) V _ {D S} - \\frac {V _ {D S} ^ {2}}{2} \\right] \\tag {1}",
     "I_D = (K' W/L)[(V_GS - V_T) V_DS - V_DS^2/2]",
     win=W41, metadata={"tag": "1", "role": "nonsaturation_drain_current"})

atom("A-SW-RON-2", "SEC-4.1", "formula_atom", "SE-4.1-EQ2",
     "r _ {\\mathrm{ON}} = \\frac {1}{\\partial I _ {D} / \\partial V _ {D S}} = \\frac {L}{K ^ {\\prime} W (V _ {G S} - V _ {T} - V _ {D S})} \\tag {2}",
     "r_ON = 1/(dI_D/dV_DS) = L/[K' W (V_GS - V_T - V_DS)]",
     win=W41, metadata={"tag": "2", "role": "on_resistance"})

atom("A-SW-RON-WL", "SEC-4.1", "physical_effect_atom", "SE-4.1-RONWL",
     "It is seen that a lower value of $r _ { \\mathrm { O N } }$ is achieved for larger values of W/L. When $V _ { G S }$ approaches",
     "r_ON decreases for larger W/L; as V_GS approaches V_T the switch turns off and r_ON approaches infinity (Fig 4.1-4).",
     win=W41)

atom("A-SW-OFF", "SEC-4.1", "physical_effect_atom", "SE-4.1-OFF",
     "When, $V _ { G S }$ is less than or equal to $V _ { T }$ the switch is OFF and $r _ { \\mathrm { O F F } }$ is ideally infinite. Of course, it is never infinite, but because it is so large, the performance in the OFF state is dominated by the drain-bulk and source-bulk leakage cur",
     "When V_GS <= V_T the switch is OFF; r_OFF is ideally infinite but never is, so OFF performance is dominated by drain-bulk and source-bulk junction leakage (plus subthreshold leakage), modeled as I_A,I_B (about 1 fA/um^2 at room temperature, doubling every 8 degC).",
     win=W41)

atom("A-SW-CAPS", "SEC-4.1", "component_model_atom", "SE-4.1-CAPS",
     "The offset voltage modeled in Fig. 4.1-1 does not exist in MOS switches and thus is not a consideration in MOS switch performance. The capacitors $C _ { A } , C _ { B } , C _ { A C } ,$ , and $C _ { B C }$ of Fig. 4.1-1 correspond directly to the capacitors $C _ { B S } , \\bar { C } _ { B D } , C _",
     "The offset voltage V_OS does not exist in MOS switches. The switch parasitics C_A,C_B,C_AC,C_BC correspond to the MOS capacitors C_BS,C_BD,C_GS,C_GD (C_AB is small and usually negligible).",
     win=W41)

atom("A-SW-CLOCKFEED", "SEC-4.1", "physical_effect_atom", "SE-4.1-CLOCK",
     "One of the most serious limitations of monolithic switches is the clock feedthrough effect. Clock feedthrough (also called charge injection, and charge feedthrough) is due to the coupling capacitance from the gate to both source and drain. This coupling allows charge to be transferred from the gate",
     "Clock feedthrough (also called charge injection / charge feedthrough) is one of the most serious limitations of monolithic switches: the gate-to-source/drain coupling capacitance transfers charge from the gate (clock) to the source/drain nodes.",
     win=W41)

atom("A-SW-GVT-3", "SEC-4.1", "formula_atom", "SE-4.1-EQ3",
     "v _ {G} = V _ {H} - U t \\tag {3}",
     "v_G = V_H - U t  (linear gate-voltage ramp; slope U)",
     win=W41, metadata={"tag": "3", "role": "gate_voltage_ramp",
                        "supported_by_context_atoms": ["A-SW-VERR-SLOW-6", "A-SW-VERR-FAST-8"]})

atom("A-SW-VERR-SLOW-6", "SEC-4.1", "formula_atom", "SE-4.1-EQ6",
     "V _ {e r r o r} = \\left(\\frac {W \\cdot \\mathrm{CGD0} + \\frac {C _ {\\text {channel}}}{2}}{C _ {L}}\\right) \\sqrt {\\frac {\\pi U C _ {L}}{2 \\beta}} + \\frac {W \\cdot \\mathrm{CGD0}}{C _ {L}} (V _ {S} + V _ {T} - V _ {L}) \\tag {6}",
     "V_error(slow) = [(W*CGD0 + C_channel/2)/C_L] sqrt(pi U C_L/(2 beta)) + (W*CGD0/C_L)(V_S + V_T - V_L)",
     win=W41, metadata={"tag": "6", "role": "charge_feedthrough_error_slow"})

atom("A-SW-VERR-FAST-8", "SEC-4.1", "formula_atom", "SE-4.1-EQ7",
     "\\frac {\\beta V _ {H T} ^ {2}}{2 C _ {L}} <   <   U \\tag {7}",
     "beta V_HT^2/(2 C_L) << U  (fast-regime condition; the fast-regime V_error uses (V_HT - beta V_HT^3/(6 U C_L)), Eq.8)",
     win=W41, metadata={"tag": "7", "role": "charge_feedthrough_fast_regime"})

atom("A-SW-FEEDFIX", "SEC-4.1", "design_principle_atom", "SE-4.1-FIX",
     "It is possible to partially cancel some of the feedthrough effects using the technique illustrated in Fig. 4.1-11. Here a dummy MOS transistor MD (with source and drain both attached to the signal line and the gate attached to the inverse clock) is used to app",
     "Clock feedthrough can be partially cancelled with a dummy MOS transistor whose gate is driven by the inverse clock (Fig 4.1-11), and reduced by using the largest capacitors possible, minimum-geometry switches, and the smallest clock swing - each of which creates compromises elsewhere.",
     win=W41)

atom("A-SW-CMOS", "SEC-4.1", "component_model_atom", "SE-4.1-CMOS",
     "Using CMOS technology, a switch is usually constructed by connecting p-channel and n-channel enhancement transistors in parallel",
     "A CMOS switch connects p-channel and n-channel enhancement transistors in parallel; its primary advantage over the single-channel MOS switch is greatly increased ON-state dynamic analog-signal range (Fig 4.1-12/4.1-13).",
     win=W41)

# Example 4.1-1
atom("A-EX411-PROBLEM", "SEC-EX411", "example_problem_atom", "SE-EX411-P",
     "Calculate the effect of charge feedthrough on the circuit shown in Fig. 4.1-9 where",
     "Example 4.1-1: calculate the charge-feedthrough error on the circuit of Fig 4.1-9 for two gate-fall cases, using Eqs (3)-(8).",
     win=W41)

atom("A-EX411-GIVEN", "SEC-EX411", "given_atom", "SE-EX411-G",
     "$V _ { S } = 1 . 0$ volts, $C _ { L } = 2 0 0$ fF, $\\ W / \\mathrm { L } = 0 . 8 \\mu \\mathrm { m } / 0 . 8 \\mu \\mathrm { m }$",
     "Given: V_S = 1.0 V, C_L = 200 fF, W/L = 0.8 um/0.8 um (Case 1 falls 5 V -> 0 V in 0.2 ns, U=25e9 V/s; Case 2 in 10 ns, U=5e8 V/s).",
     win=W41)

atom("A-EX411-USE", "SEC-EX411", "formula_usage_atom", "SE-EX411-U",
     "V _ {H T} = V _ {H} - V _ {S} - V _ {T} = 5 - 1 - 0. 8 8 7 = 3. 1 1 3",
     "V_HT = V_H - V_S - V_T = 5 - 1 - 0.887 = 3.113; test beta V_HT^2/(2 C_L) = 2.66e9 vs U to pick the regime (Case 1 fast -> Eq.8; Case 2 slow -> Eq.6).",
     win=W41)

atom("A-EX411-RESULT", "SEC-EX411", "result_atom", "SE-EX411-R",
     "V _ {e r r o r} = 1 9. 7 \\mathrm{mV}",
     "Case 1 (fast): V_error = 19.7 mV. (Case 2 (slow): V_error = 10.95 mV.)",
     win=W41, metadata={"note": "Case 1 result line; the Case 2 result V_error = 10.95 mV is on a separate $$ line (A-EX411-RESULT2)."})

atom("A-EX411-RESULT2", "SEC-EX411", "result_atom", "SE-EX411-R2",
     "V _ {e r r o r} = 1 0. 9 5 \\mathrm{mV}",
     "Case 2 (slow): V_error = 10.95 mV.",
     win=W41, metadata={"note": "Case 2 result line."})

# ===========================================================================
# 4.2 MOS Diode / Active Resistor
# ===========================================================================
atom("A-DIODE-DEF", "SEC-4.2", "component_model_atom", "SE-4.2-DEF",
     "When the gate and drain of an MOS transistor are tied together as illustrated in Fig. 4.2-1(a) and (b), the I-V characteristics are qualitatively similar to a pn-junction diode, thus the name MOS diode. The MOS diode is used as a component of a current mirror (Sec. 4.4) and for level translation (vo",
     "An MOS diode ties gate to drain, forcing saturation; its I-V is qualitatively like a pn-junction diode. It is used as a component of a current mirror (Sec 4.4) and for level translation (voltage drop).",
     win=W42)

atom("A-DIODE-I-1", "SEC-4.2", "formula_atom", "SE-4.2-EQ1",
     "I = I _ {D} = \\left(\\frac {K ^ {\\prime} W}{2 L}\\right) \\left[ \\left(V _ {G S} - V _ {T}\\right) ^ {2} \\right] = \\frac {\\beta}{2} \\left(V _ {G S} - V _ {T}\\right) ^ {2} \\tag {1}",
     "I = I_D = (K' W/(2 L))(V_GS - V_T)^2 = (beta/2)(V_GS - V_T)^2",
     win=W42, metadata={"tag": "1", "role": "mos_diode_current"})

atom("A-DIODE-V-2", "SEC-4.2", "formula_atom", "SE-4.2-EQ2",
     "V = V _ {G S} = V _ {D S} = V _ {T} + \\sqrt {2 I _ {D} / \\beta} \\tag {2}",
     "V = V_GS = V_DS = V_T + sqrt(2 I_D/beta)",
     win=W42, metadata={"tag": "2", "role": "mos_diode_voltage"})

atom("A-DIODE-ROUT-3", "SEC-4.2", "formula_atom", "SE-4.2-EQ3",
     "r _ {\\text { out }} = \\frac {1}{g _ {m} + g _ {m b s} + g _ {d s}} \\cong \\frac {1}{g _ {m}} \\tag {3}",
     "r_out = 1/(g_m + g_mbs + g_ds) ~= 1/g_m  (g_m >> g_mbs, g_ds)",
     win=W42, metadata={"tag": "3", "role": "active_resistor_resistance"})

atom("A-EX421-PROBLEM", "SEC-EX421", "example_problem_atom", "SE-EX421-P",
     "The floating active resistor of Fig. 4.2-3 is to be used to design a 1 kΩ resistance.",
     "Example 4.2-1: design the Fig 4.2-3 floating active resistor (n-channel, gate at 5 V, V_DS=0, V_A,B=2 V, bulk 0 V) for 1 kOhm; find W/L (Table 3.1-2).",
     win=W42)

atom("A-EX421-RESULT", "SEC-EX421", "result_atom", "SE-EX421-R",
     "the new $V _ { T }$ is found to be 1.022 volts. Equating Eq. (5) to 1000 Ω gives a W/L of $4 . 5 9",
     "The new V_T due to |V_BS|=2 V is 1.022 V; equating r_ds to 1000 Ohm gives W/L = 4.597 ~= 4.6.",
     win=W42)

# ===========================================================================
# 4.3 Current Sinks and Sources
# ===========================================================================
atom("A-CS-DEF", "SEC-4.3", "component_model_atom", "SE-4.3-DEF",
     "A current sink and current source are two terminal components whose current at any instant of time is independent of the voltage across their terminals.",
     "A current sink/source is a two-terminal component whose current is independent of the terminal voltage; a sink has its negative node at V_SS, a source its positive node at V_DD. An MOS device implements it with a fixed gate bias and must stay in saturation (v_OUT must exceed V_MIN).",
     win=W43)

atom("A-CS-VMIN-1", "SEC-4.3", "formula_atom", "SE-4.3-EQ1",
     "v _ {\\text { OUT }} \\geq V _ {G G} - V _ {T 0} - V _ {S S} \\tag {1}",
     "v_OUT >= V_GG - V_T0 - V_SS  (minimum output voltage for the simple current sink, Fig 4.3-1a)",
     win=W43, metadata={"tag": "1", "role": "current_sink_vmin"})

atom("A-CS-ROUT-2", "SEC-4.3", "formula_atom", "SE-4.3-EQ2",
     "r _ {\\text { out }} = \\frac {1 + \\lambda V _ {D S}}{\\lambda I _ {D}} \\cong \\frac {1}{\\lambda I _ {D}} \\tag {2}",
     "r_out = (1 + lambda V_DS)/(lambda I_D) ~= 1/(lambda I_D)  (simple current sink/source output resistance)",
     win=W43, metadata={"tag": "2", "role": "current_source_output_resistance"})

atom("A-CS-CASCODE-PRIN", "SEC-4.3", "design_principle_atom", "SE-4.3-CASC",
     "However, there are two areas in which their performance may need to be improved for certain applications. One improvement is to increase the small-signal output resistance",
     "The simple sink/source has two performance limits: low output resistance and high V_MIN. The output resistance is increased with the common-gate (cascode) configuration, which multiplies the source resistance r by the common-gate voltage gain g_m2 r_ds2.",
     win=W43)

atom("A-CS-CASCODE-ROUT-7", "SEC-4.3", "formula_atom", "SE-4.3-EQ7",
     "r _ {\\text { out }} \\cong (g _ {m 2} r _ {d s 2}) r _ {d s 1} \\tag {7}",
     "r_out ~= (g_m2 r_ds2) r_ds1  (cascode current-sink output resistance, increased by the factor g_m2 r_ds2; full form r_ds1 + r_ds2 + g_m2 r_ds1 r_ds2 (1+eta_2), Eq.6)",
     win=W43, metadata={"tag": "7", "role": "cascode_output_resistance"})

atom("A-CS-VON-PRIN", "SEC-4.3", "design_principle_atom", "SE-4.3-VON",
     "V _ {G S} = V _ {O N} + V _ {T} \\tag {8}",
     "Biasing principle: V_GS = V_ON + V_T (Eq.8) where V_ON is the excess over V_T; v_DS(sat)=V_ON (Eq.9); i_D=(K'W/2L)V_ON^2 (Eq.10). For series devices with equal current (W1/L1)/(W2/L2)=(V_ON2)^2/(V_ON1)^2 (Eq.13); for equal V_GS, i_D1(W2/L2)=i_D2(W1/L1) (Eq.14).",
     win=W43, metadata={"tag": "8", "role": "von_biasing_principle",
                        "note": "anchored on the Eq.8 line; the principle spans Eqs 8-14."})

atom("A-CS-HIGHSWING", "SEC-4.3", "design_principle_atom", "SE-4.3-HS",
     "It can be seen that a voltage of $2 { \\cal V } _ { O N }$ is across both M1 and M2 giving the lowest value of $V _ { \\mathrm { M I N } }$ and still keeping both M1 and M2 in saturation.",
     "The standard cascode (Fig 4.3-5) has V_MIN = V_T + 2 V_ON (Eq.16). Making M4 W/L one quarter of M1-M3 raises the M2 gate so V_MIN = 2 V_ON (Eq.17) - the lowest V_MIN keeping M1,M2 saturated. A series M5 (Fig 4.3-7) equalizes the M1/M3 drain voltages to remove channel-length-modulation and drain-induced-threshold-shift errors.",
     win=W43)

# Example 4.3-1
atom("A-EX431-PROBLEM", "SEC-EX431", "example_problem_atom", "SE-EX431-P",
     "Use the model parameters of Table 3.1-2 to calculate: (a) the small-signal output resistance for the simple current sink of Fig. 4.3-1(a) if",
     "Example 4.3-1: (a) the small-signal output resistance of the simple current sink (Fig 4.3-1a) at I_OUT=100 uA, and (b) the output resistance when placed in the cascode configuration of Fig 4.3-4a.",
     win=W43)

atom("A-EX431-GIVEN", "SEC-EX431", "given_atom", "SE-EX431-G",
     "(a) Using $\\lambda = 0 . 0 4$ and $I _ { \\mathrm { O U T } } = 1 0 0 $ A gives a small-signal output resistance of 250 kΩ.",
     "Given: Table 3.1-2 params, W1/L1=W2/L2=1, lambda=0.04, I_OUT=100 uA (g_mbs2 neglected).",
     win=W43)

atom("A-EX431-USE", "SEC-EX431", "formula_usage_atom", "SE-EX431-U",
     "Equation (6) of Sec. 3.3 gives $g _ { m 1 } = g _ { m 2 } = 1 4 8 \\mu \\mathrm { A } /",
     "(a) r_out = 1/(lambda I_D) with lambda=0.04, I_OUT=100 uA. (b) g_m1=g_m2=148 uA/V into Eq.(7) r_out ~= (g_m2 r_ds2) r_ds1.",
     win=W43)

atom("A-EX431-RESULT", "SEC-EX431", "result_atom", "SE-EX431-R",
     "gives a small-signal output resistance of 250 kΩ.",
     "(a) simple current sink r_out = 250 kOhm; (b) cascode current sink r_out = 9.25 MOhm.",
     win=W43)

# Example 4.3-2
atom("A-EX432-PROBLEM", "SEC-EX432", "example_problem_atom", "SE-EX432-P",
     "Use the cascode current-sink configuration of Fig. 4.3-6(a) to design a current sink of 100 $\\mu \\mathrm { A }$ and a $V _ { \\mathrm { M I N } }$ of 1 V.",
     "Example 4.3-2: design the high-swing cascode current sink (Fig 4.3-6a) for I_OUT=100 uA and V_MIN=1 V.",
     win=W43)

atom("A-EX432-GIVEN", "SEC-EX432", "given_atom", "SE-EX432-G",
     "With $V _ { \\mathrm { M I N } }$ of 1 V, choose $V _ { O N } = 0 . 5 \\ : \\mathrm { V }$ .",
     "Given: Table 3.1-2 params (K'=110 uA/V^2); V_MIN=1 V so choose V_ON=0.5 V.",
     win=W43)

atom("A-EX432-USE", "SEC-EX432", "formula_usage_atom", "SE-EX432-U",
     "\\frac {W}{L} = \\frac {2 i _ {\\mathrm{OUT}}}{K ^ {\\prime} V _ {O N} ^ {2}} = \\frac {2 \\times 1 0 0 \\times 1 0 ^ {- 6}}{1 1 0 \\times 1 0 ^ {- 6} \\times 0 . 2 5} = 7. 2 7",
     "W/L = 2 i_OUT/(K' V_ON^2) = 2*100e-6/(110e-6*0.25) = 7.27.",
     win=W43)

atom("A-EX432-RESULT", "SEC-EX432", "result_atom", "SE-EX432-R",
     "The W /L ratio of M4 will be 1/4 this value or 1.82.",
     "W/L of M1-M3 = 7.27; W/L of M4 = 1/4 of that = 1.82.",
     win=W43)

# ===========================================================================
# 4.4 Current Mirrors
# ===========================================================================
atom("A-CM-DEF", "SEC-4.4", "component_model_atom", "SE-4.4-DEF",
     "Current mirrors are simply an extension of the current sink/source of the previous section.",
     "A current mirror is an extension of the current sink/source: if the gate-source potentials of two identical MOS transistors are equal, their channel currents are equal. M1 (diode-connected) is in saturation and the output current i_O mirrors the input i_I (Fig 4.4-1).",
     win=W44)

atom("A-CM-RATIO-1", "SEC-4.4", "formula_atom", "SE-4.4-EQ1",
     "\\frac {i _ {O}}{i _ {I}} = \\left(\\frac {L _ {1} W _ {2}}{W _ {1} L _ {2}}\\right) \\left(\\frac {V _ {G S} - V _ {T 2}}{V _ {G S} - V _ {T 1}}\\right) ^ {2} \\left[ \\frac {1 + \\lambda v _ {D S 2}}{1 + \\lambda v _ {D S 1}} \\left(\\frac {K _ {2}}{K _ {1}}\\right) \\right] \\tag {1}",
     "i_O/i_I = (L1 W2/(W1 L2)) ((V_GS-V_T2)/(V_GS-V_T1))^2 [((1+lambda v_DS2)/(1+lambda v_DS1))(K2/K1)]  (general ratio)",
     win=W44, metadata={"tag": "1", "role": "current_mirror_ratio_general"})

atom("A-CM-RATIO-3", "SEC-4.4", "formula_atom", "SE-4.4-EQ3",
     "\\frac {i _ {O}}{i _ {I}} = \\left(\\frac {L _ {1} W _ {2}}{W _ {1} L _ {2}}\\right) \\tag {3}",
     "i_O/i_I = (L1 W2)/(W1 L2)  (ideal: matched devices, equal v_DS; ratio set by designer-controlled aspect ratios)",
     win=W44, metadata={"tag": "3", "role": "current_mirror_ratio_ideal"})

atom("A-CM-NONIDEAL", "SEC-4.4", "physical_effect_atom", "SE-4.4-NONID",
     "There are three effects that cause the current mirror to be different than the ideal situation of Eq. (3). These effects are: (1) channel-length modulation, (2) threshold offset between the two transistors, and (3) imperfect geometrical matching.",
     "Three effects make a real mirror deviate from the ideal: (1) channel-length modulation (unequal v_DS), (2) threshold offset Delta V_T between the two transistors (typically <10 mV), and (3) imperfect geometrical (aspect-ratio) matching.",
     win=W44)

atom("A-CM-KVT-14", "SEC-4.4", "formula_atom", "SE-4.4-EQ14",
     "\\frac {i _ {O}}{i _ {I}} \\cong 1 + \\frac {\\Delta K ^ {\\prime}}{K ^ {\\prime}} - \\frac {2 \\Delta V _ {T}}{\\left(v _ {G S} - V _ {T}\\right)} \\tag {14}",
     "i_O/i_I ~= 1 + (Delta K'/K') - 2 Delta V_T/(v_GS - V_T)  (first-order mirror gain error; e.g. +/-5% K and +/-10% relative V_T gives a 15% gain error)",
     win=W44, metadata={"tag": "14", "role": "mirror_gain_error"})

atom("A-CM-MATCH", "SEC-4.4", "design_principle_atom", "SE-4.4-MATCH",
     "A solution to this problem can be achieved by using proper layout techniques. The correct one-to-five ratio should be implemented using five duplicates of the transistor M1. In this way, the toleranc",
     "Aspect-ratio matching: a 1-to-N current amplifier should be built from N duplicates of the unit transistor M1 (with W,L > 10 um and common-centroid layout) so the W tolerance is not multiplied by the gain.",
     win=W44)

atom("A-CM-ROUT-15", "SEC-4.4", "formula_atom", "SE-4.4-EQ15",
     "r _ {\\text { out }} = \\frac {1}{g _ {d s}} \\cong \\frac {1}{\\lambda I _ {D}} \\tag {15}",
     "r_out = 1/g_ds ~= 1/(lambda I_D)  (output resistance of the simple n-channel mirror)",
     win=W44, metadata={"tag": "15", "role": "simple_mirror_output_resistance"})

atom("A-CM-CASCODE-16", "SEC-4.4", "formula_atom", "SE-4.4-EQ16",
     "r _ {\\text { out }} = r _ {d s 2} + r _ {d s 4} + g _ {m 4} r _ {d s 2} r _ {d s 4} \\left(1 + \\eta_ {4}\\right) \\tag {16}",
     "r_out = r_ds2 + r_ds4 + g_m4 r_ds2 r_ds4 (1 + eta_4)  (cascode current mirror, Fig 4.4-6; same form as Example 4.3-1)",
     win=W44, metadata={"tag": "16", "role": "cascode_mirror_output_resistance"})

atom("A-CM-WILSON", "SEC-4.4", "component_model_atom", "SE-4.4-WILSON",
     "This circuit is an n-channel implementation of the well-known Wilson current mirror [5]. The output resistance of the Wilson current mirror is increased through the use of negative, current feedback.",
     "The Wilson current mirror (Fig 4.4-8) raises r_out via negative current feedback; the regulated cascode (Fig 4.4-10) achieves an output resistance on the order of g_m^2 r^3.",
     win=W44)

# Example 4.4-1
atom("A-EX441-PROBLEM", "SEC-EX441", "example_problem_atom", "SE-EX441-P",
     "Figure 4.4-4 shows the layout of a one-to-four current amplifier. Assume that the lengths are identical $( L _ { 1 } = L _ { 2 } )$ and find the ratio error",
     "Example 4.4-1: find the ratio error of a one-to-four current amplifier (Fig 4.4-4) with identical lengths.",
     win=W44)

atom("A-EX441-GIVEN", "SEC-EX441", "given_atom", "SE-EX441-G",
     "W _ {2} = 2 0 \\pm 0. 0 5 \\mu \\mathrm{m}",
     "Given: W1 = 5 +/- 0.05 um, W2 = 20 +/- 0.05 um, L1 = L2 (variations assumed same sign).",
     win=W44)

atom("A-EX441-USE", "SEC-EX441", "formula_usage_atom", "SE-EX441-U",
     "\\frac {i _ {O}}{i _ {I}} = \\frac {W _ {2}}{W _ {1}} = \\frac {2 0 \\pm 0 . 0 5}{5 \\pm 0 . 0 5} = 4 \\pm 0. 0 5",
     "i_O/i_I = W2/W1 = (20 +/- 0.05)/(5 +/- 0.05) = 4 +/- 0.05; the tolerance is NOT multiplied by the nominal gain of 4.",
     win=W44)

atom("A-EX441-RESULT", "SEC-EX441", "result_atom", "SE-EX441-R",
     "It is seen that this ratio error is 1.25% of the desired current ratio or gain.",
     "i_O/i_I = 4 +/- 0.05; ratio error = 1.25% of the desired gain. (Example 4.4-2: building W2 from four copies of W1 gives exactly 4, removing the error.)",
     win=W44)

# ===========================================================================
# 4.5 Current and Voltage References
# ===========================================================================
atom("A-REF-DEF", "SEC-4.5", "concept_definition_atom", "SE-4.5-DEF",
     "An ideal voltage or current reference is independent of power supply and temperature.",
     "An ideal voltage or current reference is independent of power supply and temperature; 'reference' implies more precision and stability than a source, and a high-performance voltage reference can implement a high-performance current reference and vice versa.",
     win=W45)

atom("A-REF-PN-4", "SEC-4.5", "formula_atom", "SE-4.5-EQ2",
     "V _ {\\mathrm{REF}} = V _ {E B} = \\frac {k T}{q} \\ln \\left(\\frac {I}{I _ {s}}\\right) \\tag {2}",
     "V_REF = V_EB = (kT/q) ln(I/I_s)  (pn-junction / substrate-BJT reference; with I ~= V_DD/R, V_REF ~= (kT/q) ln(V_DD/(R I_s)), Eq.4; sensitivity S = 1/ln(I/I_s) < 1, Eq.5)",
     win=W45, metadata={"tag": "2", "role": "pn_voltage_reference"})

atom("A-REF-MOS-8", "SEC-4.5", "formula_atom", "SE-4.5-EQ8",
     "V _ {\\mathrm{REF}} = V _ {T} - \\frac {1}{\\beta R} + \\sqrt {\\frac {2 \\left(V _ {D D} - V _ {T}\\right)}{\\beta R} + \\frac {1}{\\beta^ {2} R ^ {2}}} \\tag {8}",
     "V_REF = V_T - 1/(beta R) + sqrt(2(V_DD - V_T)/(beta R) + 1/(beta^2 R^2))  (MOS V_T-based reference; V_DD=5 V, W/L=2, R=100 kOhm gives V_REF=1.281 V, sensitivity 0.281)",
     win=W45, metadata={"tag": "8", "role": "mos_vt_voltage_reference"})

atom("A-REF-BOOTSTRAP", "SEC-4.5", "design_principle_atom", "SE-4.5-BOOT",
     "If the voltage across the active device is used to create a current and this current is somehow used to provide the original current through the device, then a current or",
     "V_T-referenced (bootstrap) source: the reference's own voltage creates a current that is fed back to provide the original current, giving an equilibrium I_Q = V_T1/R + ... (Eq.13) essentially independent of V_DD. Two equilibrium points exist (one at the origin), so a start-up circuit is required; a V_BE-referenced variant uses I_2 R = V_BE1 = V_t ln(I_1/I_s) (Eq.14).",
     win=W45)

atom("A-REF-TCF", "SEC-4.5", "physical_effect_atom", "SE-4.5-TCF",
     "Unfortunately, supply-independent references are not necessarily temperature independent because the pn junction and gate-source voltage drops are temperature dependent as noted in Sec. 2.5.",
     "Supply-independent references are not temperature-independent: pn-junction and gate-source drops are temperature dependent. TC_F = (1/V_REF) dV_REF/dT; the simple pn reference (V_REF=0.6 V) has TC_F ~= -2500 ppm/degC.",
     win=W45)

atom("A-EX451-PROBLEM", "SEC-EX451", "example_problem_atom", "SE-EX451-P",
     "Calculate the temperature coefficient of the circuit in Fig. 4.5-4(a) where W/L=2,",
     "Example 4.5-1: calculate the temperature coefficient of the MOS V_T reference of Fig 4.5-4(a), W/L=2, V_DD=5, R=100 kOhm (polysilicon, +1500 ppm/degC).",
     win=W45)

atom("A-EX451-RESULT", "SEC-EX451", "result_atom", "SE-EX451-R",
     "T C _ {F} = - 1. 1 8 9 \\times 1 0 ^ {- 3} \\left(\\frac {1}{1 . 2 8 1}\\right) = - 9 2 8 \\mathrm{ppm} / ^ {\\circ} \\mathrm{C}",
     "TC_F = -1.189e-3 * (1/1.281) = -928 ppm/degC (V_REF=1.281 V; unrealistically good because alpha and the resistor TC_F lack the implied accuracy).",
     win=W45)

# ===========================================================================
# 4.6 Bandgap Reference
# ===========================================================================
atom("A-BG-PRINCIPLE", "SEC-4.6", "design_principle_atom", "SE-4.6-PRIN",
     "A voltage $V _ { B E }$ is generated from a pn-junction diode having a temperature coefficient of approximately −2.2 mV/°C at room temperature. Also generated is a thermal voltage $V _ { t } \\ ( V _ { t } \\ = k \\ T / q )$ which is proportional to absolute temperature (PTAT) and has a temperature coefficient of $+ 0 . 0 8 5 ~ \\mathrm { m V / ^ { \\circ } C }$ at room temperature. If the $V _ { t }$ voltage is multiplied by a constant K and summed with the $V _ { B E }$ voltage, then the output voltage is given as",
     "Bandgap principle (Fig 4.6-1): sum a V_BE from a pn-junction diode (CTAT, ~ -2.2 mV/degC at room temperature) with a thermal voltage V_t = kT/q (PTAT, ~ +0.085 mV/degC) multiplied by a constant K. Choosing K so the two temperature slopes cancel gives a near-zero temperature coefficient (~10 ppm/degC over 0-70 degC).",
     win=W46)

atom("A-BG-VREF-1", "SEC-4.6", "formula_atom", "SE-4.6-EQ1",
     "V _ {\\mathrm{REF}} = V _ {B E} + K V _ {t} \\tag {1}",
     "V_REF = V_BE + K V_t  (V_BE is CTAT, K V_t is PTAT; K chosen for zero tempco)",
     win=W46, metadata={"tag": "1", "role": "bandgap_reference"})

atom("A-BG-DVBE-12", "SEC-4.6", "formula_atom", "SE-4.6-EQ12",
     "\\Delta V _ {B E} = \\frac {k T}{q} \\ln \\left(\\frac {J _ {C 1}}{J _ {C 2}}\\right) \\tag {12}",
     "Delta V_BE = (kT/q) ln(J_C1/J_C2)  (PTAT difference of two BJT base-emitter voltages at different current densities)",
     win=W46, metadata={"tag": "12", "role": "delta_vbe_ptat"})

atom("A-BG-K-16", "SEC-4.6", "formula_atom", "SE-4.6-EQ16",
     "K = \\frac {V _ {G 0} - V _ {B E 0} + (\\gamma - \\alpha) V _ {t 0}}{V _ {t 0}} \\tag {16}",
     "K = (V_G0 - V_BE0 + (gamma - alpha) V_t0)/V_t0  (zero-tempco condition; then V_REF|T0 = V_G0 + V_t0(gamma - alpha), Eq.18; gamma=3.2, alpha=1 gives V_REF=1.262 V at 300 K, with V_G0=1.205 V the bandgap voltage)",
     win=W46, metadata={"tag": "16", "role": "bandgap_zero_tempco"})

atom("A-BG-CONV-22", "SEC-4.6", "formula_atom", "SE-4.6-EQ22",
     "V _ {\\mathrm{REF}} = V _ {E B 2} + \\left(\\frac {R _ {2}}{R _ {1}}\\right) V _ {t} \\ln \\left(\\frac {R _ {2} A _ {E 1}}{R _ {3} A _ {E 2}}\\right) \\tag {22}",
     "V_REF = V_EB2 + (R2/R1) V_t ln(R2 A_E1/(R3 A_E2))  (conventional CMOS n-well bandgap, Fig 4.6-3; K = (R2/R1) ln(R2 A_E1/(R3 A_E2)), Eq.23, set by resistor and emitter-base-area ratios; op-amp offset V_OS degrades V_REF, Eq.24)",
     win=W46, metadata={"tag": "22", "role": "conventional_bandgap"})

atom("A-BG-2NDORDER", "SEC-4.6", "physical_effect_atom", "SE-4.6-2ND",
     "Other effects include the mismatch in the betas of Q1 and Q2 and the mismatch in the finite base resistors of Q1 and Q2. Yet another source of complication is that the silicon bandgap voltage varies as a function of temperature over wide temperature ranges.",
     "Second-order errors limit the conventional bandgap to ~100 ppm/degC: op-amp input-offset V_OS (and its drift), resistor temperature coefficients, beta and base-resistance mismatch of Q1/Q2, and curvature of the silicon bandgap V_G0. With curvature/offset compensation, TC down to 13 ppm/degC over 0-70 degC has been achieved.",
     win=W46)

# Example 4.6-1
atom("A-EX461-PROBLEM", "SEC-EX461", "example_problem_atom", "SE-EX461-P",
     "Find $R _ { 2 } / R _ { 1 }$ to give a zero temperature coefficient at room temperature. If $V _ { O S } = 1 0 \\mathrm { m V }$ , find the chan",
     "Example 4.6-1: find R2/R1 for zero tempco at room temperature, and the change in V_REF if V_OS=10 mV.",
     win=W46)

atom("A-EX461-GIVEN", "SEC-EX461", "given_atom", "SE-EX461-G",
     "Assume that $A _ { E 1 } = 1 0 \\ A _ { E 2 }$ , VEB2 = 0.7 V, $R _ { 2 } = R _ { 3 }$ , and $V _ { t } = ~ 0 . 0 2 6 \\mathrm { ~ V ~ }$ at room temperature.",
     "Given: A_E1 = 10 A_E2, V_EB2 = 0.7 V, R2 = R3, V_t = 0.026 V (assume V_REF = 1.262 V).",
     win=W46)

atom("A-EX461-USE", "SEC-EX461", "formula_usage_atom", "SE-EX461-U",
     "Using the values of $V _ { E B 2 }$ and $V _ { t }$ in Eq. (1) and assuming that $V _ { \\mathrm { R E F } } { = } 1 . 2 6 2 \\ : \\mathrm { V }$ gives a value of K equal to 21.62. Eq. (23) gives $R _ { 2 } / R _ { 1 } = 9 . 3 9$ .",
     "Eq.(1) with V_EB2, V_t gives K = 21.62; Eq.(23) gives R2/R1 = 9.39; Eq.(24) gives the V_OS-degraded V_REF (iterate).",
     win=W46)

atom("A-EX461-RESULT", "SEC-EX461", "result_atom", "SE-EX461-R",
     "gives a value of K equal to 21.62. Eq. (23) gives $R _ { 2 } / R _ { 1 } = 9 . 3 9$ .",
     "K = 21.62; R2/R1 = 9.39; with V_OS=10 mV, V_REF drops from 1.262 V to 1.153 V.",
     win=W46)

# ===========================================================================
# 4.7 Summary
# ===========================================================================
atom("A-SUMMARY", "SEC-4.7", "summary_atom", "SE-4.7-SUM",
     "This chapter has introduced CMOS subcircuits, including the switch, active resistors, current sinks/sources, current mirrors or amplifiers, and voltage and current references. The general principles of each circuit were covered as was their large-signal and smallsignal performance.",
     "Chapter 4 introduced CMOS subcircuits - the switch, active resistors, current sinks/sources, current mirrors/amplifiers, and voltage and current references - each analyzed for large-signal and small-signal performance; these circuits are rarely used alone but are joined to implement complete analog functions.",
     win=W47, metadata={"context_only": True})

# ===========================================================================
# Problems
# ===========================================================================
atom("A-PROB-2", "SEC-PROB", "problem_statement_atom", "SE-PROB-2",
     "2. The circuit shown in Fig. P4.1 illustrates a single-channel MOS resistor with a W/L of $2 \\mu \\mathrm { m } / 1 \\mu \\mathrm { m }$ . Using Table 3.1-2 model parameters, calculate the small-signal on resistance of the MOS transistor at various values for $V _ { S }$",
     "Problem 2: for a single-channel MOS resistor (W/L=2um/1um, Fig P4.1), calculate the small-signal ON resistance at various V_S (Table 3.1-2).",
     win=WPR)

atom("A-PROB-12", "SEC-PROB", "problem_statement_atom", "SE-PROB-12",
     "12. Figure P4.12 illustrates a source-degenerated current source. Using Table 3.1-2 model parameters calculate the output resistance at the given current bias.",
     "Problem 12: for the source-degenerated current source of Fig P4.12, calculate the output resistance at the given current bias (Table 3.1-2).",
     win=WPR)

atom("A-PROB-20", "SEC-PROB", "problem_statement_atom", "SE-PROB-20",
     "20. Consider the simple current mirror illustrated in Fig. P4.20. Over process, the absolute variations of physical parameters are as follows:",
     "Problem 20: for the simple current mirror of Fig P4.20, with +/-5% W, +/-5% L, +/-5% K', +/-5 mV V_T and identical drain voltages, find the min and max output current over process.",
     win=WPR)

atom("A-PROB-22", "SEC-PROB", "problem_statement_atom", "SE-PROB-22",
     "22 An improved bandgap reference generator is illustrated in Fig. P4-22. Assume that the devices M1 through M5 are identical in W/L. Further assume that the area ratio for the bipolar transistors is 10:1. Design the components to achieve an output reference voltage of 1.262 volts.",
     "Problem 22: design the improved bandgap reference of Fig P4-22 (bipolar area ratio 10:1, M1-M5 identical W/L) for an output reference voltage of 1.262 V.",
     win=WPR)

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# NUMERIC over-span audit: every number in a formula/result/given/usage atom's
# normalized_text that is meant to come from the span must appear (best effort:
# we audit formula_atom normalized digits against raw, skipping the gloss tail).
# Full numeric audit is enforced in validate.py.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# source_elements (one per atom anchor; derived from each atom's source_span)
# ---------------------------------------------------------------------------
SE_TYPE = {}
for a in ATOMS:
    aid = a["source_element_id"]
    at = a["atom_type"]
    t = "formula" if at == "formula_atom" else "paragraph"
    SE_TYPE[aid] = (t, a["source_span"]["line_start"], a["source_span"]["line_end"])

SOURCE_ELEMENTS = []
for seid, (t, ls, le) in SE_TYPE.items():
    SOURCE_ELEMENTS.append(OrderedDict([
        ("id", seid), ("type", t), ("file", SOURCE_NAME),
        ("line_start", ls), ("line_end", le),
    ]))

# ---------------------------------------------------------------------------
# section_tree
# ---------------------------------------------------------------------------
def sec(sid, path, title, parent=None, kind=None):
    d = OrderedDict([("id", sid), ("path", path), ("title", title)])
    if parent:
        d["parent"] = parent
    if kind:
        d["kind"] = kind
    return d


SECTION_TREE = [
    sec("SEC-4", "4", "Analog CMOS Subcircuits"),
    sec("SEC-4.1", "4 > 4.1", "MOS Switch", "SEC-4"),
    sec("SEC-EX411", "4 > 4.1 > Example 4.1-1", "Example 4.1-1 Calculation of Charge Feedthrough Error", "SEC-4.1", "example"),
    sec("SEC-4.2", "4 > 4.2", "MOS Diode/Active Resistor", "SEC-4"),
    sec("SEC-EX421", "4 > 4.2 > Example 4.2-1", "Example 4.2-1 Calculation of the Resistance of an Active Resistor", "SEC-4.2", "example"),
    sec("SEC-4.3", "4 > 4.3", "Current Sinks and Sources", "SEC-4"),
    sec("SEC-EX431", "4 > 4.3 > Example 4.3-1", "Example 4.3-1 Calculation of Output Resistance for a Current Sink", "SEC-4.3", "example"),
    sec("SEC-EX432", "4 > 4.3 > Example 4.3-2", "Example 4.3-2 Designing the Cascode Current Sink for a Given V_MIN", "SEC-4.3", "example"),
    sec("SEC-4.4", "4 > 4.4", "Current Mirrors", "SEC-4"),
    sec("SEC-EX441", "4 > 4.4 > Example 4.4-1", "Example 4.4-1 Aspect Ratio Errors in Current Amplifiers", "SEC-4.4", "example"),
    sec("SEC-4.5", "4 > 4.5", "Current and Voltage References", "SEC-4"),
    sec("SEC-EX451", "4 > 4.5 > Example 4.5-1", "Example 4.5-1 Calculation of Threshold Voltage Reference Circuit", "SEC-4.5", "example"),
    sec("SEC-4.6", "4 > 4.6", "Bandgap Reference", "SEC-4"),
    sec("SEC-EX461", "4 > 4.6 > Example 4.6-1", "Example 4.6-1 The Design of a Bandgap-Voltage Reference", "SEC-4.6", "example"),
    sec("SEC-4.7", "4 > 4.7", "Summary", "SEC-4"),
    sec("SEC-PROB", "4 > Problems", "Problems", "SEC-4"),
]

# ---------------------------------------------------------------------------
# semantic_chunks (16 chunks, same as v0.1; A-EX411-RESULT2 added to C-EX-411)
# ---------------------------------------------------------------------------
def chunk(cid, ctype, path, atom_ids, central, boundary, targets, must):
    return OrderedDict([
        ("id", cid), ("profile", "textbook"), ("chunk_type", ctype),
        ("section_path", path), ("atom_ids", atom_ids),
        ("central_atom_ids", central), ("boundary_reason", boundary),
        ("extraction_targets", targets), ("gold_must_cover_atoms", must),
    ])


SEMANTIC_CHUNKS = [
    chunk("C-HIER", "circuit_hierarchy_block", "4",
          ["A-INTRO-SUB", "A-HIER-OPAMP", "A-SUMMARY"], ["A-HIER-OPAMP"],
          "chapter scope + the Fig 4.0-1 op-amp composition hierarchy (the source of the circuit_block_composed_of_block edges) kept with the intro/summary framing text",
          ["CircuitBlock", "Concept"], ["A-HIER-OPAMP", "A-INTRO-SUB"]),
    chunk("C-SW-MODEL", "component_model_block", "4 > 4.1",
          ["A-SW-MODEL", "A-SW-ID-1", "A-SW-RON-2", "A-SW-RON-WL", "A-SW-OFF", "A-SW-CAPS"],
          ["A-SW-MODEL", "A-SW-RON-2"],
          "switch model: r_ON formula with its nonsaturation source, OFF leakage, parasitic caps kept together (formula-explanation continuity)",
          ["ComponentModel", "Formula", "PhysicalEffect"],
          ["A-SW-MODEL", "A-SW-RON-2", "A-SW-OFF"]),
    chunk("C-SW-CLOCK", "physical_effect_block", "4 > 4.1",
          ["A-SW-CLOCKFEED", "A-SW-GVT-3", "A-SW-VERR-SLOW-6", "A-SW-VERR-FAST-8", "A-SW-FEEDFIX", "A-SW-CMOS"],
          ["A-SW-CLOCKFEED"],
          "clock feedthrough / charge injection effect + slow/fast error model + mitigation + CMOS switch, one effect cluster",
          ["PhysicalEffect", "Formula", "DesignPrinciple", "ComponentModel"],
          ["A-SW-CLOCKFEED", "A-SW-VERR-SLOW-6", "A-SW-VERR-FAST-8", "A-SW-FEEDFIX"]),
    chunk("C-EX-411", "example_solution_block", "4 > 4.1 > Example 4.1-1",
          ["A-EX411-PROBLEM", "A-EX411-GIVEN", "A-EX411-USE", "A-EX411-RESULT", "A-EX411-RESULT2"],
          ["A-EX411-PROBLEM"],
          "problem/given/formula-usage/result(s) continuous (example_solution_continuity); Case 1 (19.7 mV) and Case 2 (10.95 mV) results both kept",
          ["ExampleProblem", "ExampleSolution"],
          ["A-EX411-PROBLEM", "A-EX411-GIVEN", "A-EX411-USE", "A-EX411-RESULT"]),
    chunk("C-DIODE", "component_model_block", "4 > 4.2",
          ["A-DIODE-DEF", "A-DIODE-I-1", "A-DIODE-V-2", "A-DIODE-ROUT-3"], ["A-DIODE-DEF"],
          "MOS diode/active resistor model + its I, V, r_out formulas",
          ["ComponentModel", "Formula"], ["A-DIODE-DEF", "A-DIODE-ROUT-3"]),
    chunk("C-EX-421", "example_solution_block", "4 > 4.2 > Example 4.2-1",
          ["A-EX421-PROBLEM", "A-EX421-RESULT"], ["A-EX421-PROBLEM"],
          "active-resistor sizing worked example",
          ["ExampleSolution"], ["A-EX421-PROBLEM", "A-EX421-RESULT"]),
    chunk("C-CS", "component_model_block", "4 > 4.3",
          ["A-CS-DEF", "A-CS-VMIN-1", "A-CS-ROUT-2", "A-CS-CASCODE-PRIN", "A-CS-CASCODE-ROUT-7", "A-CS-VON-PRIN", "A-CS-HIGHSWING"],
          ["A-CS-DEF", "A-CS-CASCODE-ROUT-7"],
          "current sink/source model, r_out, cascode boosting principle, V_ON biasing principle and high-swing V_MIN reduction kept together (composition + formula continuity)",
          ["ComponentModel", "Formula", "DesignPrinciple"],
          ["A-CS-DEF", "A-CS-ROUT-2", "A-CS-CASCODE-ROUT-7", "A-CS-HIGHSWING"]),
    chunk("C-EX-431", "example_solution_block", "4 > 4.3 > Example 4.3-1",
          ["A-EX431-PROBLEM", "A-EX431-GIVEN", "A-EX431-USE", "A-EX431-RESULT"], ["A-EX431-PROBLEM"],
          "output-resistance worked example (simple vs cascode) continuous",
          ["ExampleProblem", "ExampleSolution"],
          ["A-EX431-PROBLEM", "A-EX431-USE", "A-EX431-RESULT"]),
    chunk("C-EX-432", "example_solution_block", "4 > 4.3 > Example 4.3-2",
          ["A-EX432-PROBLEM", "A-EX432-GIVEN", "A-EX432-USE", "A-EX432-RESULT"], ["A-EX432-PROBLEM"],
          "cascode-sink design-for-V_MIN worked example continuous",
          ["ExampleProblem", "ExampleSolution"],
          ["A-EX432-PROBLEM", "A-EX432-GIVEN", "A-EX432-RESULT"]),
    chunk("C-CM", "component_model_block", "4 > 4.4",
          ["A-CM-DEF", "A-CM-RATIO-1", "A-CM-RATIO-3", "A-CM-NONIDEAL", "A-CM-KVT-14", "A-CM-MATCH", "A-CM-ROUT-15", "A-CM-CASCODE-16", "A-CM-WILSON"],
          ["A-CM-DEF", "A-CM-NONIDEAL"],
          "current-mirror model, ideal/general ratio, three nonidealities, matching principle, simple/cascode/Wilson output resistance kept together",
          ["ComponentModel", "Formula", "PhysicalEffect", "DesignPrinciple"],
          ["A-CM-DEF", "A-CM-RATIO-3", "A-CM-NONIDEAL", "A-CM-MATCH"]),
    chunk("C-EX-441", "example_solution_block", "4 > 4.4 > Example 4.4-1",
          ["A-EX441-PROBLEM", "A-EX441-GIVEN", "A-EX441-USE", "A-EX441-RESULT"], ["A-EX441-PROBLEM"],
          "aspect-ratio-error worked example continuous",
          ["ExampleProblem", "ExampleSolution"],
          ["A-EX441-PROBLEM", "A-EX441-GIVEN", "A-EX441-RESULT"]),
    chunk("C-REF", "design_principle_block", "4 > 4.5",
          ["A-REF-DEF", "A-REF-PN-4", "A-REF-MOS-8", "A-REF-BOOTSTRAP", "A-REF-TCF"], ["A-REF-BOOTSTRAP"],
          "voltage/current references: pn (V_T/V_BE-based) and MOS V_T-based references built from current mirrors, bootstrap principle, and their temperature limitation",
          ["Concept", "Formula", "DesignPrinciple", "PhysicalEffect"],
          ["A-REF-DEF", "A-REF-PN-4", "A-REF-MOS-8", "A-REF-BOOTSTRAP"]),
    chunk("C-EX-451", "example_solution_block", "4 > 4.5 > Example 4.5-1",
          ["A-EX451-PROBLEM", "A-EX451-RESULT"], ["A-EX451-PROBLEM"],
          "V_T-reference temperature-coefficient worked example",
          ["ExampleSolution"], ["A-EX451-PROBLEM", "A-EX451-RESULT"]),
    chunk("C-BG", "design_principle_block", "4 > 4.6",
          ["A-BG-PRINCIPLE", "A-BG-VREF-1", "A-BG-DVBE-12", "A-BG-K-16", "A-BG-CONV-22", "A-BG-2NDORDER"],
          ["A-BG-PRINCIPLE"],
          "bandgap principle (CTAT V_BE + PTAT K*V_t -> zero tempco) with its V_REF/Delta V_BE/K formulas, conventional realization, and second-order errors",
          ["DesignPrinciple", "Formula", "PhysicalEffect"],
          ["A-BG-PRINCIPLE", "A-BG-VREF-1", "A-BG-K-16"]),
    chunk("C-EX-461", "example_solution_block", "4 > 4.6 > Example 4.6-1",
          ["A-EX461-PROBLEM", "A-EX461-GIVEN", "A-EX461-USE", "A-EX461-RESULT"], ["A-EX461-PROBLEM"],
          "bandgap design worked example continuous",
          ["ExampleProblem", "ExampleSolution"],
          ["A-EX461-PROBLEM", "A-EX461-GIVEN", "A-EX461-RESULT"]),
    chunk("C-PROB", "problem_set_block", "4 > Problems",
          ["A-PROB-2", "A-PROB-12", "A-PROB-20", "A-PROB-22"], ["A-PROB-2"],
          "end-of-chapter problems",
          ["ProblemStatement"], ["A-PROB-2", "A-PROB-12", "A-PROB-20", "A-PROB-22"]),
]
CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}
# map each atom -> its home chunk
ATOM_CHUNK = {}
for c in SEMANTIC_CHUNKS:
    for aid in c["atom_ids"]:
        ATOM_CHUNK[aid] = c["id"]

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids.
# ---------------------------------------------------------------------------
DOC_TITLE = "CMOS Analog Circuit Design - Allen & Holberg"


def package(pid, chunk_id, section_path, linked_context, targets,
            expected_objects, expected_local_fields=None, note=None,
            expected_classification=None):
    d = OrderedDict([
        ("id", pid), ("profile", "textbook"), ("chunk_id", chunk_id),
        ("section_path", section_path), ("document_title", DOC_TITLE),
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
    if note:
        d["note"] = note
    return d


CONTEXT_PACKAGES = [
    package("PKG-HIER", "C-HIER", "Chapter 4 > 4.0 Analog CMOS Subcircuits > Hierarchy",
            OrderedDict([
                ("figure", "Fig 4.0-1 op-amp composition hierarchy (rendered as a <details> mermaid flowchart: op amp -> biasing/diff-amp/2nd-stage/output; diff-amp -> current sink + current-mirror load + source-coupled pair)"),
                ("previous_heading", "Chapter 4 Analog CMOS Subcircuits"),
                ("next_heading", "4.1 MOS Switch"),
            ]),
            ["CircuitBlock", "Concept"],
            ["CIRCUIT-ANALOG-SUBCIRCUITS", "CIRCUIT-OPAMP", "CIRCUIT-DIFF-AMP",
             "CIRCUIT-CURRENT-SINK", "CIRCUIT-CURRENT-MIRROR", "CIRCUIT-MOS-SWITCH",
             "CIRCUIT-MOS-DIODE", "CIRCUIT-CURRENT-REFERENCE", "CIRCUIT-BANDGAP"],
            expected_local_fields={
                "CIRCUIT-ANALOG-SUBCIRCUITS": ["name", "role", "members"],
                "CIRCUIT-OPAMP": ["name", "note"],
                "CIRCUIT-DIFF-AMP": ["name", "note"],
                "CIRCUIT-CURRENT-SINK": ["name"],
                "CIRCUIT-CURRENT-MIRROR": ["name"],
                "CIRCUIT-MOS-SWITCH": ["name"],
                "CIRCUIT-MOS-DIODE": ["name"],
                "CIRCUIT-CURRENT-REFERENCE": ["name"],
                "CIRCUIT-BANDGAP": ["name"],
            },
            note="A-INTRO-SUB and A-SUMMARY are context_only framing text. The hierarchy package supplies the composition edges; CIRCUIT-ANALOG-SUBCIRCUITS / CIRCUIT-OPAMP / CIRCUIT-DIFF-AMP home here, while the leaf CircuitBlock objects (MOS switch, current sink, current mirror, MOS diode, current/voltage reference, bandgap) home in their own sub-section packages and are reachable here only by their A-INTRO-SUB / A-HIER-OPAMP composition statement, so this package declares expected_local_fields = ['name'] for those cross-package objects (the only field derivable from the hierarchy atoms)."),
    package("PKG-SW-MODEL", "C-SW-MODEL", "Chapter 4 > 4.1 MOS Switch > Switch model",
            OrderedDict([
                ("formula_context", "r_ON = L/[K' W (V_GS - V_T - V_DS)] (Eq.2) derived from the nonsaturation I_D (Eq.1)"),
                ("previous_heading", "4.1 MOS Switch"),
                ("next_heading", "Clock feedthrough"),
            ]),
            ["ComponentModel", "Formula", "PhysicalEffect"],
            ["CIRCUIT-MOS-SWITCH", "COMP-SWITCH-MODEL", "FORMULA-RON", "PHYS-RON-OFF"],
            expected_local_fields={
                "CIRCUIT-MOS-SWITCH": ["name", "properties", "nonidealities"],
            }),
    package("PKG-SW-CLOCK", "C-SW-CLOCK", "Chapter 4 > 4.1 MOS Switch > Clock feedthrough",
            OrderedDict([
                ("formula_context", "slow/fast charge-feedthrough V_error (Eqs 6/8) selected by beta V_HT^2/(2 C_L) vs U"),
                ("previous_heading", "Switch model"),
                ("next_heading", "Example 4.1-1"),
            ]),
            ["PhysicalEffect", "Formula", "DesignPrinciple", "ComponentModel"],
            ["PHYS-CLOCK-FEEDTHROUGH", "FORMULA-CHARGE-FEEDTHROUGH", "PRINCIPLE-FEEDTHROUGH-MIT", "CIRCUIT-CMOS-SWITCH"]),
    package("PKG-EX-411", "C-EX-411", "Chapter 4 > 4.1 > Example 4.1-1",
            OrderedDict([
                ("previous_heading", "Clock feedthrough"),
                ("next_heading", "4.2 MOS Diode/Active Resistor"),
            ]),
            ["ExampleProblem", "ExampleSolution"], ["EXAMPLE-4.1-1"]),
    package("PKG-DIODE", "C-DIODE", "Chapter 4 > 4.2 MOS Diode/Active Resistor",
            OrderedDict([
                ("formula_context", "r_out = 1/(g_m + g_mbs + g_ds) ~= 1/g_m (Eq.3)"),
                ("previous_heading", "4.2 MOS Diode/Active Resistor"),
                ("next_heading", "Example 4.2-1"),
            ]),
            ["ComponentModel", "Formula"], ["CIRCUIT-MOS-DIODE", "FORMULA-DIODE-ROUT"],
            expected_local_fields={"CIRCUIT-MOS-DIODE": ["name", "note"]}),
    package("PKG-EX-421", "C-EX-421", "Chapter 4 > 4.2 > Example 4.2-1",
            OrderedDict([
                ("previous_heading", "4.2 MOS Diode/Active Resistor"),
                ("next_heading", "4.3 Current Sinks and Sources"),
            ]),
            ["ExampleSolution"], ["EXAMPLE-4.2-1"]),
    package("PKG-CS", "C-CS", "Chapter 4 > 4.3 Current Sinks and Sources",
            OrderedDict([
                ("formula_context", "cascode r_out ~= (g_m2 r_ds2) r_ds1 (Eq.7) boosts the simple-sink r_out ~= 1/(lambda I_D) (Eq.2); high-swing V_MIN = 2 V_ON (Eq.17)"),
                ("previous_heading", "4.3 Current Sinks and Sources"),
                ("next_heading", "Example 4.3-1"),
            ]),
            ["ComponentModel", "Formula", "DesignPrinciple"],
            ["CIRCUIT-CURRENT-SINK", "CIRCUIT-CASCODE-SINK", "FORMULA-CS-ROUT",
             "FORMULA-CASCODE-ROUT", "PRINCIPLE-CASCODE", "PRINCIPLE-VON-BIAS"],
            expected_local_fields={
                "CIRCUIT-CURRENT-SINK": ["name", "note"],
                "CIRCUIT-CASCODE-SINK": ["name", "note", "composed_of"],
            }),
    package("PKG-EX-431", "C-EX-431", "Chapter 4 > 4.3 > Example 4.3-1",
            OrderedDict([
                ("previous_heading", "4.3 Current Sinks and Sources"),
                ("next_heading", "Example 4.3-2"),
            ]),
            ["ExampleProblem", "ExampleSolution"], ["EXAMPLE-4.3-1"]),
    package("PKG-EX-432", "C-EX-432", "Chapter 4 > 4.3 > Example 4.3-2",
            OrderedDict([
                ("previous_heading", "Example 4.3-1"),
                ("next_heading", "4.4 Current Mirrors"),
            ]),
            ["ExampleProblem", "ExampleSolution"], ["EXAMPLE-4.3-2"]),
    package("PKG-CM", "C-CM", "Chapter 4 > 4.4 Current Mirrors",
            OrderedDict([
                ("formula_context", "ideal ratio i_O/i_I = (L1 W2)/(W1 L2) (Eq.3); first-order gain error (Eq.14); simple r_out (Eq.15); cascode r_out (Eq.16)"),
                ("previous_heading", "4.4 Current Mirrors"),
                ("next_heading", "Example 4.4-1"),
            ]),
            ["ComponentModel", "Formula", "PhysicalEffect", "DesignPrinciple"],
            ["CIRCUIT-CURRENT-MIRROR", "COMP-WILSON", "FORMULA-MIRROR-RATIO",
             "FORMULA-MIRROR-ERROR", "PHYS-ASPECT-RATIO-ERROR", "PRINCIPLE-MIRROR-MATCH"],
            expected_local_fields={
                "CIRCUIT-CURRENT-MIRROR": ["name", "note", "composed_of"],
            }),
    package("PKG-EX-441", "C-EX-441", "Chapter 4 > 4.4 > Example 4.4-1",
            OrderedDict([
                ("previous_heading", "4.4 Current Mirrors"),
                ("next_heading", "4.5 Current and Voltage References"),
            ]),
            ["ExampleProblem", "ExampleSolution"], ["EXAMPLE-4.4-1"]),
    package("PKG-REF", "C-REF", "Chapter 4 > 4.5 Current and Voltage References",
            OrderedDict([
                ("formula_context", "pn reference V_REF = V_EB = (kT/q) ln(I/I_s) (Eq.2); MOS V_T reference (Eq.8); bootstrap I_Q (Eq.13)"),
                ("previous_heading", "4.5 Current and Voltage References"),
                ("next_heading", "Example 4.5-1"),
            ]),
            ["Concept", "Formula", "DesignPrinciple", "PhysicalEffect"],
            ["CIRCUIT-CURRENT-REFERENCE", "FORMULA-PN-REF", "FORMULA-MOS-VT-REF",
             "PRINCIPLE-BOOTSTRAP", "PHYS-REF-TEMPDEP"],
            expected_local_fields={
                "CIRCUIT-CURRENT-REFERENCE": ["name", "note", "composed_of"],
            }),
    package("PKG-EX-451", "C-EX-451", "Chapter 4 > 4.5 > Example 4.5-1",
            OrderedDict([
                ("previous_heading", "4.5 Current and Voltage References"),
                ("next_heading", "4.6 Bandgap Reference"),
            ]),
            ["ExampleSolution"], ["EXAMPLE-4.5-1"]),
    package("PKG-BG", "C-BG", "Chapter 4 > 4.6 Bandgap Reference",
            OrderedDict([
                ("formula_context", "V_REF = V_BE + K V_t (Eq.1); Delta V_BE PTAT (Eq.12); zero-tempco K (Eq.16); conventional bandgap (Eq.22/23)"),
                ("previous_heading", "4.6 Bandgap Reference"),
                ("next_heading", "Example 4.6-1"),
            ]),
            ["DesignPrinciple", "Formula", "PhysicalEffect"],
            ["CIRCUIT-BANDGAP", "FORMULA-BANDGAP", "FORMULA-BANDGAP-CONV",
             "PHYS-PTAT", "PHYS-BANDGAP-2NDORDER", "PRINCIPLE-BANDGAP-CANCEL"],
            expected_local_fields={
                "CIRCUIT-BANDGAP": ["name", "note", "composed_of"],
            }),
    package("PKG-EX-461", "C-EX-461", "Chapter 4 > 4.6 > Example 4.6-1",
            OrderedDict([
                ("previous_heading", "4.6 Bandgap Reference"),
                ("next_heading", "4.7 Summary"),
            ]),
            ["ExampleProblem", "ExampleSolution"], ["EXAMPLE-4.6-1"]),
    package("PKG-PROB", "C-PROB", "Chapter 4 > Problems",
            OrderedDict([
                ("previous_heading", "4.7 Summary"),
                ("next_heading", "References"),
            ]),
            ["ProblemStatement"], ["PROB-2", "PROB-12", "PROB-20", "PROB-22"]),
]

# ---------------------------------------------------------------------------
# mentions / canonicalization (same 16 mentions as v0.1)
# ---------------------------------------------------------------------------
def mention(mid, text, mtype, atom_id, key):
    return OrderedDict([("id", mid), ("text", text), ("type", mtype),
                        ("atom_id", atom_id), ("canonical_key", key)])


MENTIONS = [
    mention("M-01", "MOS switch", "CircuitBlock", "A-SW-MODEL", "mos_switch"),
    mention("M-02", "ON resistance", "Variable", "A-SW-RON-2", "on_resistance_ron"),
    mention("M-03", "clock feedthrough", "PhysicalEffect", "A-SW-CLOCKFEED", "clock_feedthrough"),
    mention("M-04", "charge injection", "PhysicalEffect", "A-SW-CLOCKFEED", "clock_feedthrough"),
    mention("M-05", "MOS diode", "CircuitBlock", "A-DIODE-DEF", "mos_diode_active_resistor"),
    mention("M-06", "current sink", "CircuitBlock", "A-CS-DEF", "current_sink_source"),
    mention("M-07", "cascode current sink", "CircuitBlock", "A-CS-CASCODE-ROUT-7", "cascode_current_sink"),
    mention("M-08", "current mirror", "CircuitBlock", "A-CM-DEF", "current_mirror"),
    mention("M-09", "aspect-ratio error", "PhysicalEffect", "A-CM-NONIDEAL", "aspect_ratio_error"),
    mention("M-10", "Wilson current mirror", "CircuitBlock", "A-CM-WILSON", "wilson_current_mirror"),
    mention("M-11", "voltage reference", "CircuitBlock", "A-REF-DEF", "voltage_reference"),
    mention("M-12", "bootstrap reference", "CircuitBlock", "A-REF-BOOTSTRAP", "current_reference"),
    mention("M-13", "bandgap reference", "CircuitBlock", "A-BG-PRINCIPLE", "bandgap_reference"),
    mention("M-14", "PTAT", "PhysicalEffect", "A-BG-DVBE-12", "ptat_voltage"),
    mention("M-15", "operational amplifier", "CircuitBlock", "A-HIER-OPAMP", "operational_amplifier"),
    mention("M-16", "differential amplifier", "CircuitBlock", "A-HIER-OPAMP", "differential_amplifier"),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "clock_feedthrough"),
                 ("aliases", ["clock feedthrough", "charge injection", "charge feedthrough"]),
                 ("note", "4.1 states 'Clock feedthrough (also called charge injection, and charge feedthrough)' - the three names co-refer.")]),
    OrderedDict([("canonical", "current_sink_source"),
                 ("aliases", ["current sink", "current source", "current sink/source"]),
                 ("note", "sink and source are the same two-terminal device named by node polarity.")]),
    OrderedDict([("canonical", "cascode_current_sink"),
                 ("aliases", ["cascode current sink", "cascode current source", "high-swing cascode", "standard cascode current sink"])]),
]

# ---------------------------------------------------------------------------
# objects (same IDs/types/meaning as v0.1; confidence -> gold_label/difficulty/
# evidence_strength; evidence -> local_evidence + supporting_context + home_package).
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


def _split(local_candidates, home_pkg_chunk):
    """Return (local, supporting) given a list of (atom) candidates and the
    home chunk's atom set."""
    home = set(CHUNK_ATOMS[home_pkg_chunk])
    local = [a for a in local_candidates if a in home]
    supporting = [a for a in local_candidates if a not in home]
    return local, supporting


OBJECTS = []


def add_obj(oid, otype, section_path, home_package, payload, evidence,
            difficulty, evidence_strength):
    home_chunk = next(p["chunk_id"] for p in CONTEXT_PACKAGES if p["id"] == home_package)
    local, supporting = _split(evidence, home_chunk)
    OBJECTS.append(obj(oid, otype, section_path, home_package, payload,
                       local, supporting, difficulty, evidence_strength))


# --- CircuitBlock: hierarchy root + sub-circuits ---
add_obj("CIRCUIT-ANALOG-SUBCIRCUITS", "CircuitBlock", "4", "PKG-HIER",
        OrderedDict([
            ("name", "Analog CMOS Subcircuits"),
            ("role", "Chapter-4 building blocks composing more complex analog functions"),
            ("members", ["MOS switch", "MOS diode/active resistor", "current sink/source",
                         "current mirror", "current/voltage reference", "bandgap reference"]),
        ]),
        ["A-INTRO-SUB"], "easy", "direct")
add_obj("CIRCUIT-OPAMP", "CircuitBlock", "4", "PKG-HIER",
        OrderedDict([
            ("name", "operational amplifier"),
            ("note", "complex circuit (Fig 4.0-1) composed of biasing circuits, input differential amplifier, second gain stage, and output stage."),
        ]),
        ["A-HIER-OPAMP"], "medium", "direct")
add_obj("CIRCUIT-DIFF-AMP", "CircuitBlock", "4", "PKG-HIER",
        OrderedDict([
            ("name", "input differential amplifier"),
            ("note", "stage composed of a current sink, a current-mirror load, and a source-coupled pair."),
        ]),
        ["A-HIER-OPAMP"], "medium", "direct")
add_obj("CIRCUIT-MOS-SWITCH", "CircuitBlock", "4 > 4.1", "PKG-SW-MODEL",
        OrderedDict([
            ("name", "MOS switch"),
            ("properties", ["r_ON", "r_OFF", "parasitic capacitors C_GS/C_GD/C_BS/C_BD"]),
            ("nonidealities", ["clock feedthrough", "OFF leakage I_OFF"]),
        ]),
        ["A-SW-MODEL", "A-SW-CAPS", "A-INTRO-SUB"], "medium", "direct")
add_obj("CIRCUIT-CMOS-SWITCH", "CircuitBlock", "4 > 4.1", "PKG-SW-CLOCK",
        OrderedDict([
            ("name", "CMOS switch"),
            ("note", "p-channel and n-channel enhancement transistors in parallel; larger ON-state dynamic range."),
        ]),
        ["A-SW-CMOS"], "medium", "direct")
add_obj("CIRCUIT-MOS-DIODE", "CircuitBlock", "4 > 4.2", "PKG-DIODE",
        OrderedDict([
            ("name", "MOS diode / active resistor"),
            ("note", "gate-drain tied, in saturation; r_out ~= 1/g_m; used in current mirrors and for level translation."),
        ]),
        ["A-DIODE-DEF", "A-DIODE-I-1", "A-DIODE-V-2", "A-DIODE-ROUT-3", "A-INTRO-SUB"], "medium", "direct")
add_obj("CIRCUIT-CURRENT-SINK", "CircuitBlock", "4 > 4.3", "PKG-CS",
        OrderedDict([
            ("name", "current sink/source"),
            ("note", "two-terminal constant-current device; r_out ~= 1/(lambda I_D); requires v_OUT > V_MIN."),
        ]),
        ["A-CS-DEF", "A-CS-VMIN-1", "A-CS-ROUT-2", "A-INTRO-SUB", "A-HIER-OPAMP"], "medium", "direct")
add_obj("CIRCUIT-CASCODE-SINK", "CircuitBlock", "4 > 4.3", "PKG-CS",
        OrderedDict([
            ("name", "cascode current sink"),
            ("note", "common-gate stack (M2 over M1) boosting r_out by g_m2 r_ds2; high-swing variant gives V_MIN = 2 V_ON."),
            ("composed_of", ["common-gate device M2", "current-sink device M1"]),
        ]),
        ["A-CS-CASCODE-PRIN", "A-CS-CASCODE-ROUT-7", "A-CS-HIGHSWING"], "medium", "direct")
add_obj("CIRCUIT-CURRENT-MIRROR", "CircuitBlock", "4 > 4.4", "PKG-CM",
        OrderedDict([
            ("name", "current mirror"),
            ("note", "two matched MOS transistors with common V_GS; i_O/i_I set by aspect ratios."),
            ("composed_of", ["MOS diode (M1)", "output transistor (M2)"]),
        ]),
        ["A-CM-DEF", "A-CM-RATIO-3", "A-CM-ROUT-15", "A-INTRO-SUB", "A-HIER-OPAMP"], "medium", "direct")
add_obj("CIRCUIT-CURRENT-REFERENCE", "CircuitBlock", "4 > 4.5", "PKG-REF",
        OrderedDict([
            ("name", "current/voltage reference"),
            ("note", "bootstrap (V_T- or V_BE-referenced) source; built from current mirrors (M3/M4) forcing I_1=I_2 plus a start-up circuit; supply-independent."),
            ("composed_of", ["current mirror", "reference device (V_T or V_BE)", "resistor R", "start-up circuit"]),
        ]),
        ["A-REF-DEF", "A-REF-BOOTSTRAP", "A-INTRO-SUB"], "medium", "direct")
add_obj("CIRCUIT-BANDGAP", "CircuitBlock", "4 > 4.6", "PKG-BG",
        OrderedDict([
            ("name", "bandgap reference"),
            ("note", "V_REF = V_BE + K V_t; CTAT V_BE from a BJT summed with PTAT K*V_t; ~10 ppm/degC."),
            ("composed_of", ["BJT (V_BE, CTAT)", "PTAT delta-V_BE current branch", "op amp", "resistor ratio R2/R1"]),
        ]),
        ["A-BG-PRINCIPLE", "A-BG-CONV-22", "A-INTRO-SUB"], "medium", "direct")

# --- ComponentModel ---
add_obj("COMP-SWITCH-MODEL", "ComponentModel", "4 > 4.1", "PKG-SW-MODEL",
        OrderedDict([
            ("name", "nonideal switch model"),
            ("elements", ["r_ON", "r_OFF", "V_OS (zero for MOS)", "I_OFF", "I_A/I_B leakage", "C_A,C_B,C_AB,C_AC,C_BC"]),
        ]),
        ["A-SW-MODEL", "A-SW-CAPS"], "medium", "direct")
add_obj("COMP-WILSON", "ComponentModel", "4 > 4.4", "PKG-CM",
        OrderedDict([
            ("name", "Wilson / regulated cascode mirror"),
            ("note", "Wilson uses negative current feedback; the cascode mirror (Fig 4.4-6) boosts r_out (Eq.16); the regulated cascode reaches r_out ~ g_m^2 r^3."),
        ]),
        ["A-CM-CASCODE-16", "A-CM-WILSON"], "medium", "direct")

# --- Formula ---
add_obj("FORMULA-RON", "Formula", "4 > 4.1", "PKG-SW-MODEL",
        OrderedDict([
            ("name", "switch ON resistance"),
            ("expression", "r_ON = L/[K' W (V_GS - V_T - V_DS)]"),
            ("variables", OrderedDict([
                ("K_prime", "transconductance parameter"), ("W/L", "aspect ratio"),
                ("V_GS", "gate-source voltage"), ("V_T", "threshold")])),
        ]),
        ["A-SW-RON-2", "A-SW-ID-1"], "easy", "direct")
add_obj("FORMULA-CHARGE-FEEDTHROUGH", "Formula", "4 > 4.1", "PKG-SW-CLOCK",
        OrderedDict([
            ("name", "charge-feedthrough error"),
            ("expression", "V_error depends on regime: slow uses sqrt(pi U C_L/(2 beta)); fast uses (V_HT - beta V_HT^3/(6 U C_L))"),
            ("condition", "slow if beta V_HT^2/(2 C_L) >> U; fast if << U"),
        ]),
        ["A-SW-GVT-3", "A-SW-VERR-SLOW-6", "A-SW-VERR-FAST-8"], "hard", "direct")
add_obj("FORMULA-DIODE-ROUT", "Formula", "4 > 4.2", "PKG-DIODE",
        OrderedDict([
            ("name", "MOS-diode small-signal resistance"),
            ("expression", "r_out = 1/(g_m + g_mbs + g_ds) ~= 1/g_m"),
        ]),
        ["A-DIODE-ROUT-3"], "easy", "direct")
add_obj("FORMULA-CS-ROUT", "Formula", "4 > 4.3", "PKG-CS",
        OrderedDict([
            ("name", "current-source output resistance"),
            ("expression", "r_out = (1 + lambda V_DS)/(lambda I_D) ~= 1/(lambda I_D)"),
        ]),
        ["A-CS-ROUT-2"], "easy", "direct")
add_obj("FORMULA-CASCODE-ROUT", "Formula", "4 > 4.3", "PKG-CS",
        OrderedDict([
            ("name", "cascode output resistance"),
            ("expression", "r_out ~= (g_m2 r_ds2) r_ds1  (~ g_m r_ds^2)"),
            ("note", "common-gate gain g_m2 r_ds2 multiplies the lower device r_ds1"),
        ]),
        ["A-CS-CASCODE-ROUT-7"], "medium", "direct")
add_obj("FORMULA-MIRROR-RATIO", "Formula", "4 > 4.4", "PKG-CM",
        OrderedDict([
            ("name", "ideal current-mirror ratio"),
            ("expression", "i_O/i_I = (L1 W2)/(W1 L2)"),
        ]),
        ["A-CM-RATIO-3", "A-CM-RATIO-1"], "easy", "direct")
add_obj("FORMULA-MIRROR-ERROR", "Formula", "4 > 4.4", "PKG-CM",
        OrderedDict([
            ("name", "first-order mirror gain error"),
            ("expression", "i_O/i_I ~= 1 + (Delta K'/K') - 2 Delta V_T/(v_GS - V_T)"),
        ]),
        ["A-CM-KVT-14"], "medium", "direct")
add_obj("FORMULA-PN-REF", "Formula", "4 > 4.5", "PKG-REF",
        OrderedDict([
            ("name", "pn-junction voltage reference"),
            ("expression", "V_REF = V_EB = (kT/q) ln(I/I_s); S = 1/ln(I/I_s) < 1"),
        ]),
        ["A-REF-PN-4"], "medium", "direct")
add_obj("FORMULA-MOS-VT-REF", "Formula", "4 > 4.5", "PKG-REF",
        OrderedDict([
            ("name", "MOS V_T-based reference"),
            ("expression", "V_REF = V_T - 1/(beta R) + sqrt(2(V_DD - V_T)/(beta R) + 1/(beta^2 R^2))"),
        ]),
        ["A-REF-MOS-8"], "medium", "direct")
add_obj("FORMULA-BANDGAP", "Formula", "4 > 4.6", "PKG-BG",
        OrderedDict([
            ("name", "bandgap reference voltage"),
            ("expression", "V_REF = V_BE + K V_t; V_REF|T0 = V_G0 + V_t0(gamma - alpha)"),
            ("variables", OrderedDict([
                ("V_BE", "CTAT, -2.2 mV/degC"), ("V_t", "PTAT, kT/q"),
                ("K", "scaling constant for zero tempco"), ("V_G0", "bandgap voltage 1.205 V")])),
        ]),
        ["A-BG-VREF-1", "A-BG-K-16", "A-BG-DVBE-12"], "medium", "direct")
add_obj("FORMULA-BANDGAP-CONV", "Formula", "4 > 4.6", "PKG-BG",
        OrderedDict([
            ("name", "conventional CMOS bandgap V_REF"),
            ("expression", "V_REF = V_EB2 + (R2/R1) V_t ln(R2 A_E1/(R3 A_E2)); K = (R2/R1) ln(R2 A_E1/(R3 A_E2))"),
        ]),
        ["A-BG-CONV-22"], "medium", "direct")

# --- ExampleSolution ---
add_obj("EXAMPLE-4.1-1", "ExampleSolution", "4 > 4.1 > Example 4.1-1", "PKG-EX-411",
        OrderedDict([
            ("title", "Example 4.1-1 Charge Feedthrough Error"),
            ("problem", "Charge-feedthrough error for two gate-fall cases (Fig 4.1-9)."),
            ("given", "V_S=1.0 V, C_L=200 fF, W/L=0.8/0.8; Case1 fall 0.2 ns, Case2 fall 10 ns."),
            ("result", "Case1 (fast) V_error=19.7 mV; Case2 (slow) V_error=10.95 mV."),
        ]),
        ["A-EX411-PROBLEM", "A-EX411-GIVEN", "A-EX411-USE", "A-EX411-RESULT", "A-EX411-RESULT2"],
        "medium", "direct")
add_obj("EXAMPLE-4.2-1", "ExampleSolution", "4 > 4.2 > Example 4.2-1", "PKG-EX-421",
        OrderedDict([
            ("title", "Example 4.2-1 Active Resistor"),
            ("problem", "Size an n-channel floating active resistor for 1 kOhm."),
            ("result", "V_T(due to |V_BS|=2 V)=1.022 V; W/L = 4.6."),
        ]),
        ["A-EX421-PROBLEM", "A-EX421-RESULT"], "medium", "direct")
add_obj("EXAMPLE-4.3-1", "ExampleSolution", "4 > 4.3 > Example 4.3-1", "PKG-EX-431",
        OrderedDict([
            ("title", "Example 4.3-1 Output Resistance for a Current Sink"),
            ("problem", "Output resistance: simple vs cascode current sink."),
            ("given", "I_OUT=100 uA, W1/L1=W2/L2=1, lambda=0.04, g_m1=g_m2=148 uA/V."),
            ("result", "(a) simple sink 250 kOhm; (b) cascode sink 9.25 MOhm."),
        ]),
        ["A-EX431-PROBLEM", "A-EX431-GIVEN", "A-EX431-USE", "A-EX431-RESULT"], "medium", "direct")
add_obj("EXAMPLE-4.3-2", "ExampleSolution", "4 > 4.3 > Example 4.3-2", "PKG-EX-432",
        OrderedDict([
            ("title", "Example 4.3-2 Cascode Sink for a Given V_MIN"),
            ("problem", "Design high-swing cascode sink, I_OUT=100 uA, V_MIN=1 V."),
            ("given", "V_ON=0.5 V, K'=110 uA/V^2."),
            ("result", "W/L (M1-M3) = 7.27; W/L (M4) = 1.82."),
        ]),
        ["A-EX432-PROBLEM", "A-EX432-GIVEN", "A-EX432-USE", "A-EX432-RESULT"], "medium", "direct")
add_obj("EXAMPLE-4.4-1", "ExampleSolution", "4 > 4.4 > Example 4.4-1", "PKG-EX-441",
        OrderedDict([
            ("title", "Example 4.4-1 Aspect Ratio Errors in Current Amplifiers"),
            ("problem", "Ratio error of a 1-to-4 current amplifier."),
            ("given", "W1=5+/-0.05 um, W2=20+/-0.05 um, L1=L2."),
            ("result", "i_O/i_I = 4 +/- 0.05; ratio error 1.25%."),
        ]),
        ["A-EX441-PROBLEM", "A-EX441-GIVEN", "A-EX441-USE", "A-EX441-RESULT"], "medium", "direct")
add_obj("EXAMPLE-4.5-1", "ExampleSolution", "4 > 4.5 > Example 4.5-1", "PKG-EX-451",
        OrderedDict([
            ("title", "Example 4.5-1 Threshold Voltage Reference Circuit"),
            ("problem", "TC_F of the MOS V_T reference (Fig 4.5-4a)."),
            ("result", "V_REF=1.281 V; dV_REF/dT=-1.189e-3 V/degC; TC_F=-928 ppm/degC."),
        ]),
        ["A-EX451-PROBLEM", "A-EX451-RESULT"], "medium", "direct")
add_obj("EXAMPLE-4.6-1", "ExampleSolution", "4 > 4.6 > Example 4.6-1", "PKG-EX-461",
        OrderedDict([
            ("title", "Example 4.6-1 Design of a Bandgap-Voltage Reference"),
            ("problem", "Find R2/R1 for zero tempco; change in V_REF with V_OS=10 mV."),
            ("given", "A_E1=10 A_E2, V_EB2=0.7 V, R2=R3, V_t=0.026 V."),
            ("result", "K=21.62; R2/R1=9.39; V_REF drops 1.262 -> 1.153 V with V_OS=10 mV."),
        ]),
        ["A-EX461-PROBLEM", "A-EX461-GIVEN", "A-EX461-USE", "A-EX461-RESULT"], "medium", "direct")

# --- PhysicalEffect ---
add_obj("PHYS-CLOCK-FEEDTHROUGH", "PhysicalEffect", "4 > 4.1", "PKG-SW-CLOCK",
        OrderedDict([
            ("name", "clock feedthrough / charge injection"),
            ("description", "Gate-to-source/drain coupling capacitance transfers clock charge to the signal nodes; depends on layout, dimensions, impedance, and gate waveform; the most serious switch limitation."),
        ]),
        ["A-SW-CLOCKFEED"], "medium", "direct")
add_obj("PHYS-RON-OFF", "PhysicalEffect", "4 > 4.1", "PKG-SW-MODEL",
        OrderedDict([
            ("name", "r_ON / OFF leakage"),
            ("description", "r_ON falls with W/L and rises to infinity as V_GS -> V_T; OFF state dominated by junction and subthreshold leakage (1 fA/um^2, doubles every 8 degC)."),
        ]),
        ["A-SW-RON-WL", "A-SW-OFF"], "medium", "direct")
add_obj("PHYS-ASPECT-RATIO-ERROR", "PhysicalEffect", "4 > 4.4", "PKG-CM",
        OrderedDict([
            ("name", "aspect-ratio / matching error"),
            ("description", "Channel-length modulation, V_T offset (<10 mV), and W/L mismatch cause mirror-ratio error; 15% gain error for +/-5% K' and +/-10% relative V_T."),
        ]),
        ["A-CM-NONIDEAL", "A-CM-KVT-14"], "medium", "direct")
add_obj("PHYS-REF-TEMPDEP", "PhysicalEffect", "4 > 4.5", "PKG-REF",
        OrderedDict([
            ("name", "reference temperature dependence"),
            ("description", "Supply-independent references still drift with temperature: simple pn reference TC_F ~= -2500 ppm/degC."),
        ]),
        ["A-REF-TCF"], "medium", "direct")
add_obj("PHYS-PTAT", "PhysicalEffect", "4 > 4.6", "PKG-BG",
        OrderedDict([
            ("name", "PTAT thermal voltage"),
            ("description", "Delta V_BE = (kT/q) ln(J_C1/J_C2) is proportional to absolute temperature (+0.085 mV/degC), opposing the CTAT V_BE (-2.2 mV/degC)."),
        ]),
        ["A-BG-DVBE-12", "A-BG-PRINCIPLE"], "medium", "direct")
add_obj("PHYS-BANDGAP-2NDORDER", "PhysicalEffect", "4 > 4.6", "PKG-BG",
        OrderedDict([
            ("name", "bandgap second-order errors"),
            ("description", "Op-amp V_OS (and its drift), resistor TC, beta and base-resistance mismatch, and V_G0 curvature limit a conventional bandgap to ~100 ppm/degC (13 ppm/degC with compensation)."),
        ]),
        ["A-BG-2NDORDER"], "medium", "direct")

# --- DesignPrinciple ---
add_obj("PRINCIPLE-FEEDTHROUGH-MIT", "DesignPrinciple", "4 > 4.1", "PKG-SW-CLOCK",
        OrderedDict([
            ("statement", "Reduce clock feedthrough with a dummy transistor on the inverse clock, the largest capacitors possible, minimum-geometry switches, and the smallest clock swing."),
            ("applies_to", ["sampled-data / switched-capacitor circuits"]),
            ("tradeoff", "dummy transistor needs an inverted clock and may worsen feedthrough; larger caps and smaller swing create problems elsewhere."),
        ]),
        ["A-SW-FEEDFIX"], "medium", "direct")
add_obj("PRINCIPLE-CASCODE", "DesignPrinciple", "4 > 4.3", "PKG-CS",
        OrderedDict([
            ("statement", "Use a common-gate (cascode) device to multiply output resistance by g_m r_ds; the high-swing cascode (M4 W/L = 1/4) lowers V_MIN from V_T+2V_ON to 2V_ON."),
            ("applies_to", ["current sinks/sources", "current mirrors"]),
            ("tradeoff", "more stacked devices reduce voltage headroom; reducing V_MIN at the output can raise V_I(min) at the input."),
        ]),
        ["A-CS-CASCODE-PRIN", "A-CS-HIGHSWING"], "medium", "direct")
add_obj("PRINCIPLE-VON-BIAS", "DesignPrinciple", "4 > 4.3", "PKG-CS",
        OrderedDict([
            ("statement", "Biasing relationships from V_GS=V_ON+V_T and i_D=(K'W/2L)V_ON^2: for equal series currents the W/L ratio is inversely proportional to V_ON^2; for equal V_GS the currents scale with W/L."),
            ("applies_to", ["MOS biasing", "current scaling"]),
        ]),
        ["A-CS-VON-PRIN"], "medium", "direct")
add_obj("PRINCIPLE-MIRROR-MATCH", "DesignPrinciple", "4 > 4.4", "PKG-CM",
        OrderedDict([
            ("statement", "Current-mirror accuracy comes from device ratios: keep equal drain-source voltages and high output resistance; build a 1-to-N amplifier from N unit copies of M1 (with W,L >10 um and common-centroid layout) so tolerances scale with the gain."),
            ("applies_to", ["current mirrors", "current amplifiers"]),
            ("tradeoff", "larger devices and common-centroid layout cost area."),
        ]),
        ["A-CM-MATCH", "A-CM-NONIDEAL"], "medium", "direct")
add_obj("PRINCIPLE-BOOTSTRAP", "DesignPrinciple", "4 > 4.5", "PKG-REF",
        OrderedDict([
            ("statement", "A bootstrap (V_T- or V_BE-referenced) source feeds the reference's own current back through a current mirror so I_Q is essentially V_DD-independent; a start-up circuit is required to avoid the zero-current equilibrium."),
            ("applies_to", ["supply-independent biasing"]),
            ("tradeoff", "needs a start-up circuit; not temperature independent."),
        ]),
        ["A-REF-BOOTSTRAP"], "medium", "direct")
add_obj("PRINCIPLE-BANDGAP-CANCEL", "DesignPrinciple", "4 > 4.6", "PKG-BG",
        OrderedDict([
            ("statement", "Bandgap tempco cancellation: sum a CTAT V_BE (~ -2.2 mV/degC) with a PTAT K*V_t (~ +0.085 mV/degC scaled by K) so the slopes cancel at T0, giving V_REF ~= V_G0 (1.262 V) with near-zero temperature coefficient."),
            ("applies_to", ["precision voltage/current references"]),
            ("tradeoff", "needs matched BJTs, accurate resistor ratios, and a low-offset op amp; curvature limits the achievable TC."),
        ]),
        ["A-BG-PRINCIPLE", "A-BG-K-16"], "medium", "direct")

# --- ProblemStatement ---
add_obj("PROB-2", "ProblemStatement", "4 > Problems", "PKG-PROB",
        OrderedDict([("question", "Calculate the small-signal ON resistance of a single-channel MOS resistor (W/L=2/1) at various V_S.")]),
        ["A-PROB-2"], "easy", "direct")
add_obj("PROB-12", "ProblemStatement", "4 > Problems", "PKG-PROB",
        OrderedDict([("question", "Calculate the output resistance of a source-degenerated current source (Fig P4.12).")]),
        ["A-PROB-12"], "easy", "direct")
add_obj("PROB-20", "ProblemStatement", "4 > Problems", "PKG-PROB",
        OrderedDict([("question", "Find min/max output current of a simple current mirror over +/-5% W,L,K' and +/-5 mV V_T.")]),
        ["A-PROB-20"], "medium", "direct")
add_obj("PROB-22", "ProblemStatement", "4 > Problems", "PKG-PROB",
        OrderedDict([("question", "Design the improved bandgap reference of Fig P4-22 (area ratio 10:1) for V_REF=1.262 V.")]),
        ["A-PROB-22"], "medium", "direct")

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# relations (same 50 relations + IDs as v0.1; confidence -> gold_label/
# difficulty/evidence_strength; relation_type labels preserved).
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


RELATIONS = [
    # circuit_block_composed_of_block (16)
    rel("R-01", "circuit_block_composed_of_block", "CIRCUIT-OPAMP", "CIRCUIT-DIFF-AMP", ["A-HIER-OPAMP"], "medium", "direct"),
    rel("R-02", "circuit_block_composed_of_block", "CIRCUIT-OPAMP", "CIRCUIT-CURRENT-SINK", ["A-HIER-OPAMP"], "medium", "direct"),
    rel("R-03", "circuit_block_composed_of_block", "CIRCUIT-OPAMP", "CIRCUIT-CURRENT-MIRROR", ["A-HIER-OPAMP"], "medium", "direct"),
    rel("R-04", "circuit_block_composed_of_block", "CIRCUIT-DIFF-AMP", "CIRCUIT-CURRENT-SINK", ["A-HIER-OPAMP"], "medium", "direct"),
    rel("R-05", "circuit_block_composed_of_block", "CIRCUIT-DIFF-AMP", "CIRCUIT-CURRENT-MIRROR", ["A-HIER-OPAMP"], "medium", "direct"),
    rel("R-06", "circuit_block_composed_of_block", "CIRCUIT-ANALOG-SUBCIRCUITS", "CIRCUIT-MOS-SWITCH", ["A-INTRO-SUB"], "easy", "direct"),
    rel("R-07", "circuit_block_composed_of_block", "CIRCUIT-ANALOG-SUBCIRCUITS", "CIRCUIT-MOS-DIODE", ["A-INTRO-SUB"], "easy", "direct"),
    rel("R-08", "circuit_block_composed_of_block", "CIRCUIT-ANALOG-SUBCIRCUITS", "CIRCUIT-CURRENT-SINK", ["A-INTRO-SUB"], "easy", "direct"),
    rel("R-09", "circuit_block_composed_of_block", "CIRCUIT-ANALOG-SUBCIRCUITS", "CIRCUIT-CURRENT-MIRROR", ["A-INTRO-SUB"], "easy", "direct"),
    rel("R-10", "circuit_block_composed_of_block", "CIRCUIT-ANALOG-SUBCIRCUITS", "CIRCUIT-CURRENT-REFERENCE", ["A-INTRO-SUB"], "easy", "direct"),
    rel("R-11", "circuit_block_composed_of_block", "CIRCUIT-ANALOG-SUBCIRCUITS", "CIRCUIT-BANDGAP", ["A-INTRO-SUB"], "easy", "direct"),
    rel("R-12", "circuit_block_composed_of_block", "CIRCUIT-CASCODE-SINK", "CIRCUIT-CURRENT-SINK", ["A-CS-CASCODE-PRIN", "A-CS-CASCODE-ROUT-7"], "medium", "direct"),
    rel("R-13", "circuit_block_composed_of_block", "CIRCUIT-CURRENT-MIRROR", "CIRCUIT-MOS-DIODE", ["A-CM-DEF", "A-DIODE-DEF"], "medium", "indirect"),
    rel("R-14", "circuit_block_composed_of_block", "CIRCUIT-CURRENT-REFERENCE", "CIRCUIT-CURRENT-MIRROR", ["A-REF-BOOTSTRAP"], "medium", "indirect"),
    rel("R-15", "circuit_block_composed_of_block", "CIRCUIT-BANDGAP", "CIRCUIT-CURRENT-MIRROR", ["A-BG-CONV-22"], "hard", "indirect"),
    rel("R-16", "circuit_block_composed_of_block", "CIRCUIT-CURRENT-MIRROR", "COMP-WILSON", ["A-CM-WILSON"], "medium", "indirect"),

    # component_has_property (7)
    rel("R-17", "component_has_property", "CIRCUIT-MOS-SWITCH", "FORMULA-RON", ["A-SW-RON-2"], "medium", "direct"),
    rel("R-18", "component_has_property", "CIRCUIT-MOS-SWITCH", "PHYS-RON-OFF", ["A-SW-RON-WL", "A-SW-OFF"], "medium", "direct"),
    rel("R-19", "component_has_property", "CIRCUIT-MOS-DIODE", "FORMULA-DIODE-ROUT", ["A-DIODE-ROUT-3"], "medium", "direct"),
    rel("R-20", "component_has_property", "CIRCUIT-CURRENT-SINK", "FORMULA-CS-ROUT", ["A-CS-ROUT-2"], "medium", "direct"),
    rel("R-21", "component_has_property", "CIRCUIT-CASCODE-SINK", "FORMULA-CASCODE-ROUT", ["A-CS-CASCODE-ROUT-7"], "medium", "direct"),
    rel("R-22", "component_has_property", "CIRCUIT-CURRENT-MIRROR", "FORMULA-MIRROR-RATIO", ["A-CM-RATIO-3"], "medium", "direct"),
    rel("R-23", "component_has_property", "CIRCUIT-BANDGAP", "FORMULA-BANDGAP", ["A-BG-VREF-1"], "medium", "direct"),

    # component_has_nonideality (4)
    rel("R-24", "component_has_nonideality", "CIRCUIT-MOS-SWITCH", "PHYS-CLOCK-FEEDTHROUGH", ["A-SW-CLOCKFEED"], "medium", "direct"),
    rel("R-25", "component_has_nonideality", "CIRCUIT-CURRENT-MIRROR", "PHYS-ASPECT-RATIO-ERROR", ["A-CM-NONIDEAL", "A-CM-KVT-14"], "medium", "direct"),
    rel("R-26", "component_has_nonideality", "CIRCUIT-CURRENT-REFERENCE", "PHYS-REF-TEMPDEP", ["A-REF-TCF"], "medium", "direct"),
    rel("R-27", "component_has_nonideality", "CIRCUIT-BANDGAP", "PHYS-BANDGAP-2NDORDER", ["A-BG-2NDORDER"], "medium", "direct"),

    # formula_used_in_example (8)
    rel("R-28", "formula_used_in_example", "FORMULA-CHARGE-FEEDTHROUGH", "EXAMPLE-4.1-1", ["A-EX411-USE", "A-EX411-RESULT"], "medium", "direct"),
    rel("R-29", "formula_used_in_example", "FORMULA-CASCODE-ROUT", "EXAMPLE-4.3-1", ["A-EX431-USE", "A-EX431-RESULT"], "medium", "direct"),
    rel("R-30", "formula_used_in_example", "FORMULA-CS-ROUT", "EXAMPLE-4.3-1", ["A-EX431-USE", "A-EX431-RESULT"], "medium", "direct"),
    rel("R-31", "formula_used_in_example", "FORMULA-MIRROR-RATIO", "EXAMPLE-4.4-1", ["A-EX441-USE", "A-EX441-RESULT"], "medium", "direct"),
    rel("R-32", "formula_used_in_example", "FORMULA-MOS-VT-REF", "EXAMPLE-4.5-1", ["A-EX451-RESULT"], "medium", "direct"),
    rel("R-33", "formula_used_in_example", "FORMULA-BANDGAP-CONV", "EXAMPLE-4.6-1", ["A-EX461-USE", "A-EX461-RESULT"], "medium", "direct"),
    rel("R-34", "formula_used_in_example", "FORMULA-DIODE-ROUT", "EXAMPLE-4.2-1", ["A-EX421-RESULT"], "medium", "indirect"),
    rel("R-35", "formula_used_in_example", "FORMULA-BANDGAP", "EXAMPLE-4.6-1", ["A-EX461-USE"], "medium", "indirect"),

    # design_principle_applies_to_scenario (7: R-36..41 + R-47)
    rel("R-36", "design_principle_applies_to_scenario", "PRINCIPLE-CASCODE", "CIRCUIT-CASCODE-SINK", ["A-CS-CASCODE-PRIN", "A-CS-HIGHSWING"], "medium", "direct"),
    rel("R-37", "design_principle_applies_to_scenario", "PRINCIPLE-MIRROR-MATCH", "CIRCUIT-CURRENT-MIRROR", ["A-CM-MATCH"], "medium", "direct"),
    rel("R-38", "design_principle_applies_to_scenario", "PRINCIPLE-FEEDTHROUGH-MIT", "CIRCUIT-MOS-SWITCH", ["A-SW-FEEDFIX"], "medium", "direct"),
    rel("R-39", "design_principle_applies_to_scenario", "PRINCIPLE-BOOTSTRAP", "CIRCUIT-CURRENT-REFERENCE", ["A-REF-BOOTSTRAP"], "medium", "direct"),
    rel("R-40", "design_principle_applies_to_scenario", "PRINCIPLE-BANDGAP-CANCEL", "CIRCUIT-BANDGAP", ["A-BG-PRINCIPLE"], "medium", "direct"),
    rel("R-41", "design_principle_applies_to_scenario", "PRINCIPLE-VON-BIAS", "CIRCUIT-CASCODE-SINK", ["A-CS-VON-PRIN"], "medium", "direct"),

    # design_principle_has_tradeoff (5)
    rel("R-42", "design_principle_has_tradeoff", "PRINCIPLE-CASCODE", "CIRCUIT-CASCODE-SINK", ["A-CS-HIGHSWING"], "hard", "indirect"),
    rel("R-43", "design_principle_has_tradeoff", "PRINCIPLE-FEEDTHROUGH-MIT", "PHYS-CLOCK-FEEDTHROUGH", ["A-SW-FEEDFIX"], "hard", "indirect"),
    rel("R-44", "design_principle_has_tradeoff", "PRINCIPLE-MIRROR-MATCH", "PHYS-ASPECT-RATIO-ERROR", ["A-CM-MATCH"], "hard", "indirect"),
    rel("R-45", "design_principle_has_tradeoff", "PRINCIPLE-BANDGAP-CANCEL", "PHYS-BANDGAP-2NDORDER", ["A-BG-2NDORDER"], "hard", "indirect"),
    rel("R-46", "design_principle_has_tradeoff", "PRINCIPLE-BOOTSTRAP", "PHYS-REF-TEMPDEP", ["A-REF-TCF", "A-REF-BOOTSTRAP"], "hard", "indirect"),

    # bandgap PTAT principle link
    rel("R-47", "design_principle_applies_to_scenario", "PRINCIPLE-BANDGAP-CANCEL", "PHYS-PTAT", ["A-BG-DVBE-12", "A-BG-PRINCIPLE"], "medium", "direct"),

    # problem_extends_example (3)
    rel("R-48", "problem_extends_example", "PROB-20", "EXAMPLE-4.4-1", ["A-PROB-20"], "medium", "indirect"),
    rel("R-49", "problem_extends_example", "PROB-22", "EXAMPLE-4.6-1", ["A-PROB-22"], "medium", "indirect"),
    rel("R-50", "problem_extends_example", "PROB-12", "EXAMPLE-4.3-1", ["A-PROB-12"], "medium", "indirect"),
]

# ---------------------------------------------------------------------------
# do_not_extract: bracket [n] citations + policy; Fig./Eq. cross-refs;
# <details> mermaid/figure-internal node labels; image markup; out-of-chapter refs.
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([("text", "[1]"), ("atom_id", "A-INTRO-SUB"),
                 ("reason", "bracket reference-list citation marker, not a knowledge object"), ("kind", "citation")]),
    OrderedDict([("pattern", "bracket_citation"),
                 ("examples", ["[1]", "[2]", "[3]", "[4]", "[5]", "[7,8,9,10,11]"]),
                 ("reason", "Allen & Holberg cite the reference list with bracketed numbers; these are not knowledge objects."),
                 ("kind", "citation_policy")]),
    OrderedDict([("text", "Operational Amplifier"), ("source_lines", [4886, 4886]),
                 ("reason", "raw mermaid node label inside the Fig 4.0-1 <details> flowchart block; figure-internal node text, not an extractable mention (the op-amp hierarchy is captured by the prose atom A-HIER-OPAMP)."),
                 ("kind", "figure_label")]),
    OrderedDict([("text", "Biasing Circuits"), ("source_lines", [4886, 4886]),
                 ("reason", "raw mermaid node label inside the Fig 4.0-1 flowchart <details> block."),
                 ("kind", "figure_label")]),
    OrderedDict([("text", "Source Coupled Pair"), ("source_lines", [4893, 4893]),
                 ("reason", "raw mermaid node label inside the Fig 4.0-1 flowchart <details> block."),
                 ("kind", "figure_label")]),
    OrderedDict([("pattern", "mermaid_flowchart_node_label"),
                 ("examples", ['A["Operational Amplifier"]', 'B["Biasing Circuits"]',
                               'C["Input Differential Amplifier"]', 'F["Current Source"]',
                               'I["Source Coupled Pair"]', 'K["Inverter"]', 'M["Source Follower"]']),
                 ("reason", "the Fig 4.0-1 op-amp hierarchy is a <details><summary>flowchart</summary> mermaid graph TD block; its raw node labels (and the graph TD / --> syntax) are figure-internal and must not be atomized - the composition is carried by the prose atom A-HIER-OPAMP and emitted as circuit_block_composed_of_block edges."),
                 ("kind", "figure_label")]),
    OrderedDict([("pattern", "details_text_image_node_label"),
                 ("examples", ["I_OFF", "r_OFF", "C_AB", "M1", "MD", "iOUT", "VGG", "VBIAS", "Startup"]),
                 ("reason", "circuit schematics render as <details><summary>text_image</summary> ... </details> blocks of OCR node labels (M1/M2/iOUT/VGG/...); these are figure-internal pin/device labels, not knowledge objects."),
                 ("kind", "figure_label")]),
    OrderedDict([("pattern", "image_markup"),
                 ("examples", ["![](images/0748fad0f0ed002a067b97516a8899ffa8209a6353db9790ae0678d0333383c2.jpg)"]),
                 ("reason", "MinerU image markdown for the figure raster; not text content."),
                 ("kind", "figure_label")]),
    OrderedDict([("pattern", "figure_equation_cross_reference"),
                 ("examples", ["Fig. 4.1-1", "Fig. 4.0-1", "Fig. 4.3-6(a)", "Eq. (8)", "Eq. (23)", "Table 3.1-2"]),
                 ("reason", "in-text Fig./Eq./Table cross-references are navigation pointers, not extractable knowledge objects."),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "to be covered in Chapters 6 and 7"), ("atom_id", "A-HIER-OPAMP"),
                 ("reason", "forward reference to Chapters 6-7 (op-amp), outside the Chapter-4 slice."),
                 ("kind", "out_of_slice_reference")]),
    OrderedDict([("text", "Chapter 5 will examine more complex circuits like CMOS amplifiers"), ("atom_id", "A-INTRO-SUB"),
                 ("reason", "forward reference to Chapter 5, outside the Chapter-4 slice."),
                 ("kind", "out_of_slice_reference")]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "cmos"),
    ("scope", "Chapter 4 (Analog CMOS Subcircuits) only"),
    ("title", "CMOS Analog Circuit Design - Allen & Holberg - Chapter 4 Analog CMOS Subcircuits"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines 4873-7479; NOT the authoritative source)"),
    ("profile", "textbook"),
    ("profile_detected_as", "textbook"),
    ("profile_cues", ["Chapter 4", "Example 4.1-1", "Example 4.3-1", "Example 4.4-1",
                      "Example 4.6-1", "Problems", "Eq. (7)", "Fig. 4.0-1", "Figure 4.6-3"]),
    ("extraction_targets", ["CircuitBlock", "ComponentModel", "Formula", "Variable",
                            "ExampleSolution", "PhysicalEffect", "DesignPrinciple", "ProblemStatement"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be used as the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file. normalized_text renders EXACTLY that span (may rephrase, transliterate math like beta/lambda/eta/gamma/alpha/Delta and ASCII digit spacing, and resolve in-span pronouns) but MUST NOT introduce facts/names/section-refs not inside the span. MinerU spaces digits inside math ('1 9. 7', '0. 0 5') and uses LaTeX in $...$ / $$...$$; normalized gives the readable ascii form. EXCEPTION: a formula_atom may carry symbolic interpretation / neighbouring-equation context via metadata.supported_by_context_atoms."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside its home_package's chunk) + supporting_context_atom_ids (atoms from other chunks). Package object-recall (expected_objects) is scored on local evidence only."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = payload fields derivable from that package alone. The leaf CircuitBlocks reachable from PKG-HIER (MOS switch, current sink, current mirror, MOS diode, current/voltage reference, bandgap) home in their own sub-section package; PKG-HIER declares expected_local_fields for the three hierarchy-local objects only."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of a decimal confidence."),
        ("tables_and_figures", "Tables/characteristic curves render as markdown | tables | or <details> blocks; circuit schematics and the Fig 4.0-1 op-amp hierarchy render as <details><summary>flowchart|text_image</summary> ... </details> OCR/mermaid blocks. Figure-internal node labels, the raw mermaid graph TD node text, image markup, and in-text Fig./Eq./Table cross-references are listed in do_not_extract."),
        ("context_only", "an atom with context_only:true is background/framing (chapter intro A-INTRO-SUB and summary A-SUMMARY) that should NOT yield a typed object on its own; A-INTRO-SUB additionally supports the Analog-Subcircuits composition edges as supporting evidence."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects are the objects whose home_package is that package."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "Fig 4.0-1 is the op-amp composition hierarchy, rendered by MinerU as a <details><summary>flowchart</summary> ```mermaid graph TD ... ``` </details> block (source lines 4881-4900) plus the caption 'Figure 4.0-1 ...' (line 4902). The hierarchy ATOM A-HIER-OPAMP anchors on the PROSE paragraph (line 4877) that states the composition; the raw mermaid node labels (Operational Amplifier / Biasing Circuits / Source Coupled Pair / ...) are figure-internal and listed in do_not_extract as figure_label / mermaid_flowchart_node_label. The composition is emitted as circuit_block_composed_of_block edges (R-01..R-05).",
        "Formula tags reset per sub-section (4.1 tag1..8, 4.2 tag1..6, 4.3 restarts at 1 and runs to 17, 4.4 restarts, 4.5 restarts, 4.6 restarts). Each formula atom is located by its verbatim $$ line WITHIN its sub-section line window (build.py windows W41/W42/W43/W44/W45/W46), so identical-looking tags in different sections never collide.",
        "MinerU spaces digits inside math: '1 9. 7 \\mathrm{mV}', '7. 2 7', '0. 0 5', '2 5 \\times 1 0 ^ {9}'. raw_text keeps the spacing verbatim; normalized_text gives the readable ascii number (19.7 mV, 7.27, 0.05).",
        "Charge-feedthrough fast-regime: MinerU labelled the fast-regime condition line beta V_HT^2/(2 C_L) << U as \\tag {7} (Eq.6 is the slow-regime V_error). A-SW-VERR-FAST-8 anchors that tag-7 condition line; the Eq.(8) fast V_error and the slow/fast selector are carried as normalized gloss / supported_by_context_atoms.",
        "Example 4.1-1 has two result lines (Case 1 fast = 19.7 mV, Case 2 slow = 10.95 mV), each its own $$ V_error = ... mV $$ block; both are atomized (A-EX411-RESULT / A-EX411-RESULT2).",
        "Many circuits are building blocks of higher-level circuits: the current reference is built from current mirrors, the cascode sink from stacked transistors, the current mirror from a diode-connected M1, and the bandgap from a BJT + PTAT branch + op-amp + resistor ratio; these are the circuit_block_composed_of_block edges R-12..R-16.",
    ]),
])

# ---------------------------------------------------------------------------
# Assemble (ordered) and dump.
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
        "# Gold fixture v0.3.3-textbook - CMOS Analog Circuit Design Chapter 4 Analog CMOS Subcircuits (profile: textbook)\n"
        "# Upgraded from v0.1: dual coordinates (source_span authoritative into the mineru .md + viewer_span into source.md),\n"
        "# verbatim raw_text + within-span normalized_text, source_elements, object evidence split into local/supporting +\n"
        "# home_package, one context_package per chunk with expected_objects (+ expected_local_fields for cross-package\n"
        "# CircuitBlocks reachable from the hierarchy package), gold_label/difficulty/evidence_strength (no decimal confidence),\n"
        "# and do_not_extract negatives (bracket [n] citations + policy, Fig./Eq. cross-refs, mermaid/figure-internal node\n"
        "# labels, image markup, out-of-chapter refs). This is the CIRCUIT-HIERARCHY chapter: 16 circuit_block_composed_of_block\n"
        "# edges. Spans are computed by build.py against the mineru .md; do NOT hand-edit.\n"
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
