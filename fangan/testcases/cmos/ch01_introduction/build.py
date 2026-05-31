#!/usr/bin/env python3
"""Builder for ch01_introduction gold.yaml (schema v0.3.3-textbook, profile textbook).

Upgrades the CMOS Chapter 1 (Introduction and Background) fixture from v0.1 to
v0.3.3-textbook. Keeps EVERY v0.1 atom/chunk/object/relation/mention ID and its
meaning; only re-expresses them in the v0.3.3 schema and adds verbatim dual
coordinates (authoritative source_span into the MinerU .md + viewer_span into
source.md). TEXTBOOK type vocabulary is retained (Concept/Formula/ProcessFlow/
DesignPrinciple/ExampleProblem/ProblemStatement, etc.) -- NOT article types.

All spans are computed by locating verbatim substrings (anchors) in the
authoritative MinerU source file; YAML spans are NEVER hand-written.

Authoritative source : CMOS_Analog_Circuit_Design_-_Allen_Holberg_mineru.md lines 3-742.
Viewer slice         : source.md (verbatim copy of those lines + header comment).

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

SLICE_LINE_START = 3
SLICE_LINE_END = 742

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
# Viewer file source.md : verbatim slice of lines 3-742 + header comment.
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
    return 2 + (src_line_no - SLICE_LINE_START)


def _line_of_offset(text, off):
    return text.count("\n", 0, off) + 1


# ---------------------------------------------------------------------------
# Span helper #1 (by_line / by_anchor): locate a verbatim substring of a given
# source LINE. Substring must be unique within that line.
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


# ---------------------------------------------------------------------------
# Span helper #2 (multi-line region): locate a verbatim, contiguous, multi-line
# substring anywhere in the slice (used for the numbered 7-step list which spans
# several source lines). Substring must be globally unique within the slice.
# ---------------------------------------------------------------------------
SLICE_SRC_START = _LINE_OFF[SLICE_LINE_START]
SLICE_SRC_END = _LINE_OFF[SLICE_LINE_END] + len(SRC_LINES[SLICE_LINE_END - 1]) + 1
SLICE_REGION = SRC[SLICE_SRC_START:SLICE_SRC_END]


def span_for_region(raw):
    idx = SLICE_REGION.find(raw)
    if idx < 0:
        raise SystemExit("NOT FOUND in slice: %r" % raw[:70])
    if SLICE_REGION.find(raw, idx + 1) >= 0:
        raise SystemExit("AMBIGUOUS in slice: %r" % raw[:70])
    src_cs = SLICE_SRC_START + idx
    src_ce = src_cs + len(raw)
    assert SRC[src_cs:src_ce] == raw, "region source span mismatch"
    source_span = OrderedDict([
        ("file", SOURCE_NAME),
        ("line_start", _line_of_offset(SRC, src_cs)),
        ("line_end", _line_of_offset(SRC, src_ce - 1)),
        ("char_start", src_cs), ("char_end", src_ce),
    ])
    v_idx = VIEWER_TEXT.find(raw)
    assert v_idx >= 0 and VIEWER_TEXT.find(raw, v_idx + 1) < 0, "region viewer locate"
    v_cs = v_idx
    v_ce = v_cs + len(raw)
    assert VIEWER_TEXT[v_cs:v_ce] == raw, "region viewer span mismatch"
    viewer_span = OrderedDict([
        ("file", VIEWER_NAME),
        ("line_start", _line_of_offset(VIEWER_TEXT, v_cs)),
        ("line_end", _line_of_offset(VIEWER_TEXT, v_ce - 1)),
        ("char_start", v_cs), ("char_end", v_ce),
        ("viewer_only", True),
    ])
    return source_span, viewer_span


# ---------------------------------------------------------------------------
# Span helper #3 (by_tag within a sub-section line window): a display formula is
# located by its '\tag {N}' inside the given sub-section's [lo, hi] line window
# (tags reset per sub-section). Chapter 1 has exactly one display formula, Eq.(1)
# (tag 1) in 1.1; this mirrors the engram/cmos-ch2 by_tag locator generically.
# ---------------------------------------------------------------------------
def span_for_tag(tag, lo, hi):
    needle = "\\tag {%s}" % tag
    found = None
    for n in range(lo, hi + 1):
        if needle in src_line(n):
            if found is not None:
                raise SystemExit("AMBIGUOUS tag %s in window [%d,%d]" % (tag, lo, hi))
            found = n
    if found is None:
        raise SystemExit("tag %s not found in window [%d,%d]" % (tag, lo, hi))
    raw = src_line(found)
    return raw, span_for(raw, found)


# ---------------------------------------------------------------------------
# atom() factory.
# ---------------------------------------------------------------------------
def atom(aid, section_id, atom_type, se_id, raw, normalized, line_no,
         evidence_strength="direct", metadata=None, context_only=False,
         region=False):
    if region:
        ss, vs = span_for_region(raw)
    else:
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
    if context_only:
        d["context_only"] = True
    return d


ATOMS = []
A = ATOMS.append

# ---------- 1.0 chapter intro (context_only framing) ----------
A(atom(
    "A-INTRO-CMOS", "SEC-1", "concept_definition_atom", "SE-1-P0A",
    "Complementary metal-oxide semiconductor (CMOS) technology has been the mainstay in mixed-signal1 implementations because it provides density and power savings on the digital side, and a good mix of components for analog design. By reason of its wide-spread use, CMOS technology is the subject of this text.",
    "VLSI now integrates complete systems combining analog and digital functions on one chip. CMOS is the mainstay for mixed-signal designs (density/power on the digital side, a good mix of components for analog) and is the subject of this text.",
    5, context_only=True,
))
A(atom(
    "A-INTRO-CAD", "SEC-1", "concept_definition_atom", "SE-1-P0B",
    "Due in part to the regularity and granularity of digital circuits, computer aided design (CAD) methodologies have been very successful in automating the design of digital systems given a behavioral description of the function desired. Such is not the case for analog circuit design. Analog design still requires a “hands on” design approach in general.",
    "CAD methodologies automate digital design from a behavioral description; analog design still requires a hands-on approach, so this book gives a hierarchical organization and general principles of analog IC design.",
    7, context_only=True,
))

# ---------- 1.1 signal definitions + Eq.(1) ----------
A(atom(
    "A-SIG-DEF", "SEC-1.1", "concept_definition_atom", "SE-1.1-SIGDEF",
    "A signal will be considered to be any detectable value of voltage, current, or charge. A signal should convey information about the state or behavior of a physical system.",
    "A signal is any detectable value of voltage, current, or charge that conveys information about a physical system.",
    13,
))
A(atom(
    "A-SIG-ANALOG", "SEC-1.1", "concept_definition_atom", "SE-1.1-SIGDEF",
    "An analog signal is a signal that is defined over a continuous range of time and a continuous range of amplitudes. An analog signal is illustrated in Fig. 1.1-1(a).",
    "An analog signal is defined over a continuous range of time AND a continuous range of amplitudes (Fig. 1.1-1a).",
    13,
))
A(atom(
    "A-SIG-DIGITAL", "SEC-1.1", "concept_definition_atom", "SE-1.1-SIGDEF",
    "A digital signal is a signal that is defined only at discrete values of amplitude, or said another way, a digital signal is quantized to discrete values. Typically, the digital signal is a binary-weighted sum of signals having only two defined values of amplitude as illustrated in Fig. 1.1-1(b) and shown in Eq. (1). Figure 1.1-1(b) is a 3-bit representation of the analog signal shown in Fig. 1.1-1(a).",
    "A digital signal is defined only at discrete values of amplitude (quantized); typically a binary-weighted sum of two-valued signals (Fig. 1.1-1b, Eq.(1)). Fig.1.1-1(b) is a 3-bit representation of the analog signal.",
    13,
))
_eq1_raw, (_eq1_ss, _eq1_vs) = span_for_tag("1", 13, 17)
_eq1 = OrderedDict()
_eq1["id"] = "A-SIG-EQ1"
_eq1["section_id"] = "SEC-1.1"
_eq1["atom_type"] = "formula_atom"
_eq1["source_element_id"] = "SE-1.1-EQ1"
_eq1["source_span"] = _eq1_ss
_eq1["viewer_span"] = _eq1_vs
_eq1["raw_text"] = _eq1_raw
_eq1["normalized_text"] = "D = b_0 2^-1 + b_1 2^-2 + b_2 2^-3 + ... + b_{N-1} 2^-(N-1) = sum_{i=0}^{N-1} b_i 2^-i"
_eq1["evidence_strength"] = "direct"
_eq1["metadata"] = OrderedDict([("tag", "1"), ("role", "digital_representation")])
A(_eq1)
A(atom(
    "A-SIG-SAMPLED", "SEC-1.1", "concept_definition_atom", "SE-1.1-SAMPLED",
    "An analog sampled-data signal is a signal that is defined over a continuous range of amplitudes but only at discrete points in time. Often the sampled analog signal is held at the value present at the end of the sample period, resulting in a sampled-and-held signal. An analog sampled-and-held signal is illustrated in Fig. 1.1-1(c).",
    "An analog sampled-data signal is defined over a continuous range of amplitudes but only at discrete points in time; if held to the end of the sample period it is a sampled-and-held signal (Fig. 1.1-1c).",
    77,
))
A(atom(
    "A-ANALYSIS-DESIGN", "SEC-1.1", "concept_definition_atom", "SE-1.1-ANADES",
    "The analysis of a circuit, illustrated in Fig. 1.1-2(a), is the process by which one starts with the circuit and finds its properties. An important characteristic of the analysis process is that the solution or properties are unique. On the other hand, the synthesis or design of a circuit is the process by which one starts with a desired set of properties and finds a circuit that satisfies them. In a design problem the solution is not unique thus giving opportunity for the designer to be creative.",
    "Analysis starts with a circuit and finds its (unique) properties (Fig.1.1-2a). Synthesis/design starts with desired properties and finds a circuit satisfying them; the design solution is NOT unique, giving the designer room to be creative (Fig.1.1-2b).",
    79,
))
A(atom(
    "A-IC-VS-DISCRETE", "SEC-1.1", "design_principle_atom", "SE-1.1-ICDISC",
    "The differences between integrated and discrete analog circuit design are important. Unlike integrated circuits, discrete circuits use active and passive components that are not on the same substrate. A major benefit of components sharing the same substrate in close proximity is that component matching can be used as a tool for design. Another difference between the two design methods is that the geometry of active devices and passive components in integrated circuit design are under the control of the designer. This control over geometry gives the designer a new degree of freedom in the design process. A second difference is due to the fact that it is impractical to breadboard the integrated-circuit design. Consequently, the designer must turn to computer simulation methods to confirm the design’s performance. Another difference between integrated and discrete analog design is that the integrated-circuit designer is restricted to a more limited class of components that are compatible with the technology being used.",
    "Integrated vs discrete analog design: shared substrate enables component matching as a design tool; device/component geometry is under the designer's control; the IC cannot be breadboarded so the designer must rely on computer simulation; and only a limited, technology-compatible class of components is available.",
    109,
))
A(atom(
    "A-DESIGN-STEPS", "SEC-1.1", "process_flow_atom", "SE-1.1-STEPS",
    "The major steps in the design process are:\n\n1. Definition   \n2. Synthesis or implementation   \n3. Simulation or modeling   \n4. Geometrical description   \n5. Simulation including the geometrical parasitics   \n6. Fabrication   \n7. Testing and verification",
    "The major steps in the design process are: 1 Definition; 2 Synthesis or implementation; 3 Simulation or modeling; 4 Geometrical description; 5 Simulation including the geometrical parasitics; 6 Fabrication; 7 Testing and verification.",
    None, region=True,
    metadata=OrderedDict([("note", "verbatim 7-step numbered list (source lines 111-119). The 'designer responsible for all steps except fabrication' clause is restated verbatim on line 121 (A-DESIGN-SIM-PARASITIC's element); the normalized summary keeps it as the in-list designer-responsibility note.")]),
))
A(atom(
    "A-DESIGN-SIM-PARASITIC", "SEC-1.1", "design_principle_atom", "SE-1.1-SIMPAR",
    "The designer makes approximations about the physical definition of the circuit initially. Later, once the layout is complete, simulations are checked using parasitic information derived from the layout. At this point, the designer may iterate using the simulation results to improve the circuit’s performance.",
    "Initially the designer simulates with approximations about the physical circuit; later, once layout is complete, simulations are re-checked using parasitic information extracted from the layout, and the designer iterates on these post-layout simulation results to improve performance before fabrication.",
    121,
))
A(atom(
    "A-DESIGN-FORMATS", "SEC-1.1", "concept_definition_atom", "SE-1.1-FORMATS",
    "In accomplishing the design steps described above, the designer works with three different types of description formats. These include: the design description, the physical description, and the model/simulation description. The format of the design description is the way in which the circuit is specified; the physical description format is the geometrical definition of the circuit; the model/simulation format is the means by which the circuit can be simulated.",
    "The designer works in three description formats: design description (how the circuit is specified), physical description (geometrical definition), and model/simulation description (means to simulate).",
    164,
))
A(atom(
    "A-HIER-CONCEPT", "SEC-1.1", "concept_definition_atom", "SE-1.1-HIER",
    "Analog integrated-circuit design can also be characterized from the viewpoint of hierarchy. Table 1.1-1 shows a vertical hierarchy consisting of devices, circuits, and systems, and horizontal description formats consisting of design, physical, and model. The device level is the lowest level of design.",
    "Analog IC design is organized as a vertical hierarchy of devices < circuits < systems: device level is lowest, circuits are expressed in terms of devices, and the systems level (highest) is expressed in terms of circuits.",
    166,
))

# ---------- Table 1.1-1 (caption L168 + <table> L170 anchored cells) ----------
A(atom(
    "A-T111-CAPTION", "SEC-1.1", "table_caption_atom", "SE-1.1-T111-CAP",
    "Table 1.1-1 Hierarchy and Description of the Analog Circuit Design Process.",
    "Table 1.1-1 Hierarchy and Description of the Analog Circuit Design Process.",
    168, metadata=OrderedDict([("table_id", "Table 1.1-1")]),
))
A(atom(
    "A-T111-HEADER", "SEC-1.1", "table_header_atom", "SE-1.1-T111",
    "<tr><td>Hierarchy</td><td>Design</td><td>Physical</td><td>Model</td></tr>",
    "Columns: Hierarchy | Design | Physical | Model.",
    170, metadata=OrderedDict([("table_id", "Table 1.1-1"), ("role", "column_header")]),
))
A(atom(
    "A-T111-SYSTEMS", "SEC-1.1", "table_row_atom", "SE-1.1-T111",
    "<tr><td>Systems</td><td>System Specifications</td><td>Floor plan</td><td>Behavioral Model</td></tr>",
    "Systems row: Design = System Specifications; Physical = Floor plan; Model = Behavioral Model.",
    170, metadata=OrderedDict([("table_id", "Table 1.1-1"), ("row", "Systems")]),
))
A(atom(
    "A-T111-CIRCUITS", "SEC-1.1", "table_row_atom", "SE-1.1-T111",
    "<tr><td>Circuits</td><td>Circuit Specifications</td><td>Parameterized Blocks/Cells</td><td>Macromodels</td></tr>",
    "Circuits row: Design = Circuit Specifications; Physical = Parameterized Blocks/Cells; Model = Macromodels.",
    170, metadata=OrderedDict([("table_id", "Table 1.1-1"), ("row", "Circuits")]),
))
A(atom(
    "A-T111-DEVICES", "SEC-1.1", "table_row_atom", "SE-1.1-T111",
    "<tr><td>Devices</td><td>Device Specifications</td><td>Geometrical Description</td><td>Device Models</td></tr>",
    "Devices row: Design = Device Specifications; Physical = Geometrical Description; Model = Device Models.",
    170, metadata=OrderedDict([("table_id", "Table 1.1-1"), ("row", "Devices")]),
))
# Table 1.1-2 design-level -> chapter map (single <table> line 176)
A(atom(
    "A-T112-MAP", "SEC-1.1", "table_row_atom", "SE-1.1-T112",
    "<table><tr><td>Design Level</td><td colspan=\"3\">CMOS Technology</td></tr><tr><td>Systems</td><td>Chapter 9Switched CapacitorCircuits</td><td>Chapter 10D/A and A/D Converters</td><td></td></tr><tr><td>Complex Circuits</td><td>Chapter 6Simple Operational Amplifiers</td><td>Chapter 7Complex Operational Amplifiers</td><td>Chapter 8Comparators</td></tr><tr><td>Simple Circuits</td><td>Chapter 4Analog Subcircuits</td><td></td><td>Chapter 5Amplifiers</td></tr><tr><td>Devices</td><td>Chapter 2Technology</td><td>Chapter 3Modeling</td><td>Appendix BCharacterization</td></tr></table>",
    "Design-level to chapter map: Devices -> Chapter 2 Technology, Chapter 3 Modeling, Appendix B Characterization; Simple Circuits -> Chapter 4 Analog Subcircuits, Chapter 5 Amplifiers; Complex Circuits -> Chapter 6 Simple Operational Amplifiers, Chapter 7 Complex Operational Amplifiers, Chapter 8 Comparators; Systems -> Chapter 9 Switched Capacitor Circuits, Chapter 10 D/A and A/D Converters.",
    176, metadata=OrderedDict([("table_id", "Table 1.1-2"),
                               ("note", "MinerU dropped spaces ('Chapter 9Switched CapacitorCircuits') and used colspan=3 for the CMOS Technology banner row.")]),
))

# ---------- 1.2 notation + Table 1.2-1 ----------
# The notation block is a reference convention table: v0.1 homed NO typed object on it
# (it is background convention consumed by no object/relation), so these atoms are flagged
# context_only:true to satisfy the global no-orphan invariant without inventing new objects.
A(atom(
    "A-NOTATION-INTRO", "SEC-1.2", "concept_definition_atom", "SE-1.2-NOTE",
    "The first item of importance is the notation (the symbols) for currents and voltages. Signals will generally be designated as a quantity with a subscript. The quantity and the subscript will be either upper or lower case according to the convention illustrated in Table 1.2-1.",
    "Currents/voltages are written as a quantity with a subscript; both quantity and subscript are upper- or lower-case per Table 1.2-1, consistent with standard undergraduate-text conventions and SI units.",
    184, context_only=True,
))
A(atom(
    "A-T121-CAPTION", "SEC-1.2", "table_caption_atom", "SE-1.2-T121-CAP",
    "Table 1.2-1 Definition of the Symbols for Various Signals.",
    "Table 1.2-1 Definition of the Symbols for Various Signals.",
    186, metadata=OrderedDict([("table_id", "Table 1.2-1")]), context_only=True,
))
A(atom(
    "A-T121-HEADER", "SEC-1.2", "table_header_atom", "SE-1.2-T121",
    "<tr><td>Signal Definition</td><td>Quantity</td><td>Subscript</td><td>Example</td></tr>",
    "Columns: Signal Definition | Quantity | Subscript | Example.",
    188, metadata=OrderedDict([("table_id", "Table 1.2-1"), ("role", "column_header")]), context_only=True,
))
A(atom(
    "A-T121-TOTAL", "SEC-1.2", "table_row_atom", "SE-1.2-T121",
    "<tr><td>Total instantaneous value of the signal</td><td>Lowercase</td><td>Uppercase</td><td> $q_{A}$ </td></tr>",
    "Total instantaneous value: Quantity lowercase, Subscript uppercase; example q_A (e.g. i_D).",
    188, metadata=OrderedDict([("table_id", "Table 1.2-1"), ("row", "total_instantaneous")]), context_only=True,
))
A(atom(
    "A-T121-DC", "SEC-1.2", "table_row_atom", "SE-1.2-T121",
    "<tr><td>dc value of the signal</td><td>Uppercase</td><td>Uppercase</td><td> $Q_{A}$ </td></tr>",
    "dc value: Quantity uppercase, Subscript uppercase; example Q_A (e.g. I_D).",
    188, metadata=OrderedDict([("table_id", "Table 1.2-1"), ("row", "dc")]), context_only=True,
))
A(atom(
    "A-T121-AC", "SEC-1.2", "table_row_atom", "SE-1.2-T121",
    "<tr><td>ac value of the signal</td><td>Lowercase</td><td>Lowercase</td><td> $q_{a}$ </td></tr>",
    "ac value: Quantity lowercase, Subscript lowercase; example q_a (e.g. i_d).",
    188, metadata=OrderedDict([("table_id", "Table 1.2-1"), ("row", "ac")]), context_only=True,
))
A(atom(
    "A-T121-RMS", "SEC-1.2", "table_row_atom", "SE-1.2-T121",
    "<tr><td>Complex variable, phasor, or rms value of the signal</td><td>Uppercase</td><td>Lowercase</td><td> $Q_{a}$ </td></tr>",
    "Complex variable, phasor, or rms value: Quantity uppercase, Subscript lowercase; example Q_a (e.g. I_d).",
    188, metadata=OrderedDict([("table_id", "Table 1.2-1"), ("row", "rms")]), context_only=True,
))

# ---------- 1.3 signal-processing system ----------
A(atom(
    "A-SP-SYSTEM", "SEC-1.3", "concept_definition_atom", "SE-1.3-SYS",
    "The first block of Fig. 1.3-1 is a preprocessing block. Typically, this block will consist of filters, an automatic-gain-control circuit, and an analogto-digital converter (ADC or A/D). Often, very strict speed and accuracy requirements are placed on the components in this block. The next block of the analog signal processor is a digital signal processor.",
    "A typical signal-processing system (Fig. 1.3-1) is a pipeline: analog input -> pre-processing (filters, AGC, A/D converter) -> digital signal processor -> post-processing (D/A converter, amplification, filtering) -> analog output.",
    302,
))
A(atom(
    "A-SP-ANALOG-DIGITAL", "SEC-1.3", "design_principle_atom", "SE-1.3-PART",
    "The first step in the design of an analog signal-processing system is to examine the specifications and decide what part of the system should be analog and what part should be digital. In most cases, the input signal is analog.",
    "The first design step is to decide which part of the system is analog and which is digital; digital processing offers smallest-geometry cost/speed, extra freedom (e.g. linear-phase filters), and programmability. Analog circuits rarely stand alone but combine with digital to perform signal processing.",
    302,
))
A(atom(
    "A-SP-BANDWIDTH", "SEC-1.3", "concept_definition_atom", "SE-1.3-BW",
    "In a signal processing system, one of the important system consideration is the bandwidth of the signal to be processed. A graph of the operating frequency of a variety of signals is given in Fig. 1.3-2.",
    "Signal bandwidth is one of the important system considerations; the operating frequency of a variety of signals is given in Fig. 1.3-2.",
    324,
    metadata=OrderedDict([("note", "the seismic ~1 Hz / microwave ~30 GHz endpoints and the CMOS-integration trend are on source lines 324 (tail) / 346, outside this span; the digital cost/speed/programmability rationale is on line 302 (A-SP-ANALOG-DIGITAL element). normalized is kept within the line-324 span.")]),
))

# ---------- 1.4 mixed-signal example ----------
A(atom(
    "A-EX-READCHANNEL", "SEC-1.4", "example_problem_atom", "SE-1.4-GOAL",
    "Figure 1.4-1 shows the block diagram of a fully-integrated digital read/write channel for disk drive recording applications. The device employs partial response maximum likelihood (PRML) sequence detection when reading data to enhance bit-error-rate versus signal-to-noise ratio performance. The device supports data rates up to 64 Mbits/sec and is fabricated in a 0.8 m double-metal CMOS process.",
    "Worked example: a fully-integrated digital read/write channel IC for disk-drive recording, using PRML (partial-response maximum-likelihood) sequence detection, supporting data rates up to 64 Mbits/sec, fabricated in a 0.8 um single-polysilicon double-metal CMOS process.",
    369,
    metadata=OrderedDict([("note", "source line 369 prints '0.8 m double-metal' (MinerU dropped 'u'/single-polysilicon); 'single-polysilicon ... 0.8 m CMOS process' is restated on line 603 (A-EX-RESULT element). normalized keeps the v0.1 fuller spec; numeric facts 64/0.8 are in-span.")]),
))
A(atom(
    "A-EX-READPATH", "SEC-1.4", "process_flow_atom", "SE-1.4-READPATH",
    "This differential read pulse is first amplified by a variable gain amplifier (VGA) under control of a real-time digital gain-control loop. After amplification, the signal is passed to a 7-pole 2-zero equiripple-phase low-pass filter.",
    "Read path (start): the differential read pulse is first amplified by a variable gain amplifier (VGA) under a real-time digital gain-control loop, then passed to a 7-pole 2-zero equiripple-phase low-pass filter.",
    371,
    metadata=OrderedDict([("note", "raw_text anchors the read-path start (VGA -> 7-pole 2-zero LPF) on line 371; the downstream stages (buffer/6-bit flash A/D + VCO, FIR, Viterbi sequence detector, RLL decoder) continue on source lines 512/514/516 and inform the PROCESS-READPATH object payload.")]),
))
A(atom(
    "A-EX-MASTERPLL", "SEC-1.4", "design_principle_atom", "SE-1.4-PLL",
    "The control-voltage, VCON, is derived from the “Master PLL” composed of a replica of the filter configured as a voltage-controlled oscillator in a phase-locked-loop configuration as illustrated in Fig. 1.4-3. The frequency of oscillation is inversely proportional to the characteristic time constant, $C / g _ { m }$ , of the replica filter’s stages. By forcing the oscillator to be phase and frequency-locked to an external frequency reference through variation of the VCON terminal voltage, the characteristic time constant is held fixed. To the extent that the circuit elements in the low-pass filter match those in the master filter, the characteristic time constants of the low-pass filter (and thus the frequency response) are also fixed.",
    "The low-pass filter is built from transconductance (g_m) stages and capacitors; VCON from a Master PLL (a replica filter as a VCO phase-locked to an external reference) holds the characteristic time constant C/g_m fixed, compensating for process/temperature/supply variation. Matching the LPF to the master filter fixes the frequency response.",
    461,
))
A(atom(
    "A-EX-RESULT", "SEC-1.4", "result_atom", "SE-1.4-RESULT",
    "Figure 1.4-6 shows a photomicrograph of the read-channel chip described. The circuit was fabricated in a single-polysilicon, double-metal, 0.8 m CMOS process.",
    "The example emphasizes the hierarchical structure of analog IC design and shows how device/circuit/system topics of later chapters combine into one complex mixed-signal chip (read-channel photomicrograph, Fig.1.4-6).",
    603,
))

# ---------- 1.5 summary (context_only) ----------
A(atom(
    "A-SUMMARY", "SEC-1.5", "summary_atom", "SE-1.5-SUM",
    "This chapter has presented an introduction to the design of CMOS analog integrated circuits. Section 1.1 gave a definition of signals in analog circuits and defined analog, digital, and analog sampled-data signals. The difference between analysis and design was discussed. The design differences between discrete and integrated analog circuits are primarily due to the designer’s control over circuit geometry and the need to computersimulate rather than build a breadboard. The first section also presented an overview of the text and showed in Table 1.1-2 how the various chapters tied together.",
    "Chapter summary: Section 1.1 defined analog, digital, and analog sampled-data signals; contrasted analysis vs design; noted that integrated vs discrete analog design differs mainly in the designer's control over circuit geometry and the need to computer-simulate rather than breadboard; and gave an overview of the text via Table 1.1-2.",
    607, context_only=True,
))

# ---------- PROBLEMS ----------
A(atom(
    "A-PROB-1", "SEC-PROB", "problem_statement_atom", "SE-PROB-1",
    "1. Using Eq. (1) of Sec 1.1, give the base-10 value for the 5-bit binary number 11010 $( b _ { 4 }$ b3 b2 $b _ { 1 }$ b0 ordering).",
    "1. Using Eq.(1) of Sec 1.1, give the base-10 value for the 5-bit binary number 11010 (b_4 b_3 b_2 b_1 b_0 ordering).",
    619,
))
A(atom(
    "A-PROB-2", "SEC-PROB", "problem_statement_atom", "SE-PROB-2",
    "2. Process the sinusoid in Fig. P1.2 through an analog sample and hold. The sample points are given at each integer value of t/T.",
    "2. Process the sinusoid in Fig. P1.2 through an analog sample and hold; sample points given at each integer value of t/T.",
    620, context_only=True,
))
A(atom(
    "A-PROB-3", "SEC-PROB", "problem_statement_atom", "SE-PROB-3",
    "3. Digitize the sinusoid given in Fig. P1.2 according to Eq. (1) in Sec. 1.1 using a four-bit digitizer.",
    "3. Digitize the sinusoid given in Fig. P1.2 according to Eq.(1) in Sec.1.1 using a four-bit digitizer.",
    645,
))
# raw_text taken verbatim from the source line so the Ω glyph (file uses U+03A9) matches exactly.
_PROB12_RAW = src_line(730)
A(atom(
    "A-PROB-12", "SEC-PROB", "problem_statement_atom", "SE-PROB-12",
    _PROB12_RAW,
    "12. For an ideal voltage amplifier A_v=0.99 with R=50 kOhm fed back from output to input, find the input resistance via the Miller simplification concept.",
    730, context_only=True,
))

ATOM_IDS = [a["id"] for a in ATOMS]
ATOM_TYPE = {a["id"]: a["atom_type"] for a in ATOMS}

# ---------------------------------------------------------------------------
# source_elements: one per source_element_id, auto-derived from atoms' spans.
# ---------------------------------------------------------------------------
SE_TYPE_OVERRIDE = {
    "SE-1.1-EQ1": "formula",
    "SE-1.1-T111-CAP": "table", "SE-1.1-T111": "table", "SE-1.1-T112": "table",
    "SE-1.2-T121-CAP": "table", "SE-1.2-T121": "table",
}


def build_source_elements(atoms):
    groups = OrderedDict()
    for a in atoms:
        groups.setdefault(a["source_element_id"], []).append(a)
    elems = []
    for se_id, members in groups.items():
        ls = min(m["source_span"]["line_start"] for m in members)
        le = max(m["source_span"]["line_end"] for m in members)
        etype = SE_TYPE_OVERRIDE.get(se_id, "paragraph")
        elems.append(OrderedDict([
            ("id", se_id), ("type", etype),
            ("file", SOURCE_NAME), ("line_start", ls), ("line_end", le),
        ]))
    return elems


SOURCE_ELEMENTS = build_source_elements(ATOMS)

# ---------------------------------------------------------------------------
# section_tree (unchanged from v0.1).
# ---------------------------------------------------------------------------
SECTION_TREE = [
    OrderedDict([("id", "SEC-1"), ("path", "1"), ("title", "Introduction and Background")]),
    OrderedDict([("id", "SEC-1.1"), ("path", "1 > 1.1"), ("title", "Analog Integrated Circuit Design"), ("parent", "SEC-1")]),
    OrderedDict([("id", "SEC-1.2"), ("path", "1 > 1.2"), ("title", "Notation, Symbology, and Terminology"), ("parent", "SEC-1")]),
    OrderedDict([("id", "SEC-1.3"), ("path", "1 > 1.3"), ("title", "Analog Signal Processing"), ("parent", "SEC-1")]),
    OrderedDict([("id", "SEC-1.4"), ("path", "1 > 1.4"), ("title", "Example of Analog VLSI Mixed-Signal Circuit Design"), ("parent", "SEC-1"), ("kind", "example")]),
    OrderedDict([("id", "SEC-1.5"), ("path", "1 > 1.5"), ("title", "Summary"), ("parent", "SEC-1")]),
    OrderedDict([("id", "SEC-PROB"), ("path", "1 > PROBLEMS"), ("title", "PROBLEMS"), ("parent", "SEC-1")]),
]

# ---------------------------------------------------------------------------
# semantic_chunks (same 9 chunks / atom membership as v0.1).
# ---------------------------------------------------------------------------
SEMANTIC_CHUNKS = [
    OrderedDict([
        ("id", "C-OV"), ("profile", "textbook"), ("chunk_type", "chapter_overview_block"),
        ("section_path", "1"),
        ("atom_ids", ["A-INTRO-CMOS", "A-INTRO-CAD", "A-SUMMARY"]),
        ("central_atom_ids", ["A-INTRO-CMOS"]),
        ("boundary_reason", "chapter scope (intro framing + 1.5 summary) kept as one overview block"),
        ("extraction_targets", ["Concept"]),
        ("gold_must_cover_atoms", ["A-INTRO-CMOS", "A-SUMMARY"]),
    ]),
    OrderedDict([
        ("id", "C-SIGNALS"), ("profile", "textbook"), ("chunk_type", "concept_definition_block"),
        ("section_path", "1 > 1.1"),
        ("atom_ids", ["A-SIG-DEF", "A-SIG-ANALOG", "A-SIG-DIGITAL", "A-SIG-EQ1", "A-SIG-SAMPLED"]),
        ("central_atom_ids", ["A-SIG-ANALOG", "A-SIG-DIGITAL"]),
        ("boundary_reason", "the three signal definitions + Eq.(1) form one definition cluster; formula kept with the digital-signal definition it formalizes (formula_continuity)"),
        ("extraction_targets", ["Concept", "Formula"]),
        ("gold_must_cover_atoms", ["A-SIG-ANALOG", "A-SIG-DIGITAL", "A-SIG-EQ1", "A-SIG-SAMPLED"]),
    ]),
    OrderedDict([
        ("id", "C-ANALYSIS-DESIGN"), ("profile", "textbook"), ("chunk_type", "concept_definition_block"),
        ("section_path", "1 > 1.1"),
        ("atom_ids", ["A-ANALYSIS-DESIGN", "A-IC-VS-DISCRETE"]),
        ("central_atom_ids", ["A-ANALYSIS-DESIGN"]),
        ("boundary_reason", "analysis-vs-design contrast + integrated-vs-discrete differences (a related contrast pair)"),
        ("extraction_targets", ["Concept", "DesignPrinciple"]),
        ("gold_must_cover_atoms", ["A-ANALYSIS-DESIGN", "A-IC-VS-DISCRETE"]),
    ]),
    OrderedDict([
        ("id", "C-DESIGN-PROCESS"), ("profile", "textbook"), ("chunk_type", "design_process_block"),
        ("section_path", "1 > 1.1"),
        ("atom_ids", ["A-DESIGN-STEPS", "A-DESIGN-SIM-PARASITIC", "A-DESIGN-FORMATS"]),
        ("central_atom_ids", ["A-DESIGN-STEPS"]),
        ("boundary_reason", "the 7-step design flow + the simulation/post-layout-parasitics principle + the three description formats kept together (process_flow_continuity)"),
        ("extraction_targets", ["ProcessFlow", "DesignPrinciple", "Concept"]),
        ("gold_must_cover_atoms", ["A-DESIGN-STEPS", "A-DESIGN-SIM-PARASITIC"]),
    ]),
    OrderedDict([
        ("id", "C-HIERARCHY"), ("profile", "textbook"), ("chunk_type", "hierarchy_table_block"),
        ("section_path", "1 > 1.1"),
        ("atom_ids", ["A-HIER-CONCEPT", "A-T111-CAPTION", "A-T111-HEADER", "A-T111-SYSTEMS", "A-T111-CIRCUITS", "A-T111-DEVICES", "A-T112-MAP"]),
        ("central_atom_ids", ["A-T111-HEADER"]),
        ("boundary_reason", "Table 1.1-1 caption+header+rows kept together (table_header_row_dependency); the hierarchy prose and the chapter-map Table 1.1-2 row belong to the same hierarchy topic"),
        ("extraction_targets", ["Concept"]),
        ("gold_must_cover_atoms", ["A-HIER-CONCEPT", "A-T111-SYSTEMS", "A-T111-CIRCUITS", "A-T111-DEVICES"]),
    ]),
    OrderedDict([
        ("id", "C-NOTATION"), ("profile", "textbook"), ("chunk_type", "concept_definition_block"),
        ("section_path", "1 > 1.2"),
        ("atom_ids", ["A-NOTATION-INTRO", "A-T121-CAPTION", "A-T121-HEADER", "A-T121-TOTAL", "A-T121-DC", "A-T121-AC", "A-T121-RMS"]),
        ("central_atom_ids", ["A-T121-HEADER"]),
        ("boundary_reason", "Table 1.2-1 caption+header+four convention rows kept together (table_header_row_dependency)"),
        ("extraction_targets", ["Concept"]),
        ("gold_must_cover_atoms", ["A-T121-TOTAL", "A-T121-DC", "A-T121-AC", "A-T121-RMS"]),
    ]),
    OrderedDict([
        ("id", "C-SIGPROC"), ("profile", "textbook"), ("chunk_type", "concept_definition_block"),
        ("section_path", "1 > 1.3"),
        ("atom_ids", ["A-SP-SYSTEM", "A-SP-ANALOG-DIGITAL", "A-SP-BANDWIDTH"]),
        ("central_atom_ids", ["A-SP-SYSTEM"]),
        ("boundary_reason", "signal-processing block diagram + analog/digital partitioning + bandwidth considerations (one system-level topic)"),
        ("extraction_targets", ["Concept", "DesignPrinciple"]),
        ("gold_must_cover_atoms", ["A-SP-SYSTEM", "A-SP-ANALOG-DIGITAL"]),
    ]),
    OrderedDict([
        ("id", "C-EX-READCHANNEL"), ("profile", "textbook"), ("chunk_type", "example_solution_block"),
        ("section_path", "1 > 1.4"),
        ("atom_ids", ["A-EX-READCHANNEL", "A-EX-READPATH", "A-EX-MASTERPLL", "A-EX-RESULT"]),
        ("central_atom_ids", ["A-EX-READCHANNEL"]),
        ("boundary_reason", "worked mixed-signal example: goal/spec -> read-path steps -> master-PLL tuning principle -> hierarchical-design takeaway (example continuity)"),
        ("extraction_targets", ["ExampleProblem", "ProcessFlow", "DesignPrinciple"]),
        ("gold_must_cover_atoms", ["A-EX-READCHANNEL", "A-EX-READPATH", "A-EX-MASTERPLL"]),
    ]),
    OrderedDict([
        ("id", "C-PROB"), ("profile", "textbook"), ("chunk_type", "problem_set_block"),
        ("section_path", "1 > PROBLEMS"),
        ("atom_ids", ["A-PROB-1", "A-PROB-2", "A-PROB-3", "A-PROB-12"]),
        ("central_atom_ids", ["A-PROB-1"]),
        ("boundary_reason", "end-of-chapter problems"),
        ("extraction_targets", ["ProblemStatement"]),
        ("gold_must_cover_atoms", ["A-PROB-1", "A-PROB-2", "A-PROB-3"]),
    ]),
]

CHUNK_ATOMS = {c["id"]: c["atom_ids"] for c in SEMANTIC_CHUNKS}
CHUNK_PKG = {
    "C-OV": "PKG-OV", "C-SIGNALS": "PKG-SIGNALS", "C-ANALYSIS-DESIGN": "PKG-ANALYSIS-DESIGN",
    "C-DESIGN-PROCESS": "PKG-DESIGN-PROCESS", "C-HIERARCHY": "PKG-HIERARCHY",
    "C-NOTATION": "PKG-NOTATION", "C-SIGPROC": "PKG-SIGPROC",
    "C-EX-READCHANNEL": "PKG-EX-READCHANNEL", "C-PROB": "PKG-PROB",
}
PKG_CHUNK = {v: k for k, v in CHUNK_PKG.items()}

# ---------------------------------------------------------------------------
# objects (same 18 IDs / meaning as v0.1; drop confidence; add gold_label /
# difficulty / evidence_strength + local/supporting evidence + home_package).
# ---------------------------------------------------------------------------
def split_evidence(home_pkg, all_evidence):
    home_atoms = set(CHUNK_ATOMS[PKG_CHUNK[home_pkg]])
    local = [a for a in all_evidence if a in home_atoms]
    supporting = [a for a in all_evidence if a not in home_atoms]
    return local, supporting


def obj(oid, otype, section_path, home_pkg, payload, all_evidence, difficulty, evidence_strength):
    local, supporting = split_evidence(home_pkg, all_evidence)
    assert local, "%s has no local evidence in %s" % (oid, home_pkg)
    d = OrderedDict()
    d["id"] = oid
    d["type"] = otype
    d["section_path"] = section_path
    d["home_package"] = home_pkg
    d["payload"] = payload
    d["local_evidence_atom_ids"] = local
    d["supporting_context_atom_ids"] = supporting
    d["gold_label"] = True
    d["difficulty"] = difficulty
    d["evidence_strength"] = evidence_strength
    return d


OBJECTS = [
    obj("CONCEPT-ANALOG-SIGNAL", "Concept", "1 > 1.1", "PKG-SIGNALS",
        OrderedDict([("term", "analog signal"),
                     ("definition", "A signal defined over a continuous range of time and a continuous range of amplitudes.")]),
        ["A-SIG-ANALOG", "A-SIG-DEF"], "easy", "direct"),
    obj("CONCEPT-DIGITAL-SIGNAL", "Concept", "1 > 1.1", "PKG-SIGNALS",
        OrderedDict([("term", "digital signal"),
                     ("definition", "A signal defined only at discrete (quantized) amplitude values; typically a binary-weighted sum of two-valued signals.")]),
        ["A-SIG-DIGITAL"], "easy", "direct"),
    obj("CONCEPT-SAMPLED-SIGNAL", "Concept", "1 > 1.1", "PKG-SIGNALS",
        OrderedDict([("term", "analog sampled-data signal"),
                     ("definition", "A signal continuous in amplitude but defined only at discrete points in time; if held to the sample-period end it is sampled-and-held.")]),
        ["A-SIG-SAMPLED"], "easy", "direct"),
    obj("CONCEPT-ANALYSIS", "Concept", "1 > 1.1", "PKG-ANALYSIS-DESIGN",
        OrderedDict([("term", "analysis"),
                     ("definition", "Starting from a circuit and finding its properties; the solution is unique.")]),
        ["A-ANALYSIS-DESIGN"], "easy", "direct"),
    obj("CONCEPT-DESIGN", "Concept", "1 > 1.1", "PKG-ANALYSIS-DESIGN",
        OrderedDict([("term", "synthesis / design"),
                     ("definition", "Starting from desired properties and finding a circuit that satisfies them; the solution is not unique.")]),
        ["A-ANALYSIS-DESIGN"], "easy", "direct"),
    obj("CONCEPT-HIERARCHY", "Concept", "1 > 1.1", "PKG-HIERARCHY",
        OrderedDict([("term", "analog design hierarchy"),
                     ("definition", "Vertical hierarchy devices < circuits < systems crossed with design/physical/model description formats (Table 1.1-1)."),
                     ("levels", ["Devices", "Circuits", "Systems"]),
                     ("formats", ["Design", "Physical", "Model"]),
                     ("table", OrderedDict([
                         ("Systems", ["System Specifications", "Floor plan", "Behavioral Model"]),
                         ("Circuits", ["Circuit Specifications", "Parameterized Blocks/Cells", "Macromodels"]),
                         ("Devices", ["Device Specifications", "Geometrical Description", "Device Models"]),
                     ]))]),
        ["A-HIER-CONCEPT", "A-T111-CAPTION", "A-T111-HEADER", "A-T111-SYSTEMS", "A-T111-CIRCUITS", "A-T111-DEVICES", "A-T112-MAP"],
        "medium", "direct"),
    obj("CONCEPT-DESIGN-FORMATS", "Concept", "1 > 1.1", "PKG-DESIGN-PROCESS",
        OrderedDict([("term", "description formats"),
                     ("definition", "Design, physical, and model/simulation descriptions used throughout the design process.")]),
        ["A-DESIGN-FORMATS"], "easy", "direct"),
    obj("CONCEPT-SP-SYSTEM", "Concept", "1 > 1.3", "PKG-SIGPROC",
        OrderedDict([("term", "signal-processing system"),
                     ("definition", "Pipeline: analog input -> pre-processing (filter/AGC/A-D) -> digital signal processor -> post-processing (D-A/amp/filter) -> analog output.")]),
        ["A-SP-SYSTEM"], "easy", "direct"),
    # --- Formula ---
    obj("FORMULA-DIGITAL-REP", "Formula", "1 > 1.1", "PKG-SIGNALS",
        OrderedDict([("name", "digital (binary-weighted) representation"),
                     ("expression", "D = sum_{i=0}^{N-1} b_i 2^-i"),
                     ("variables", OrderedDict([("D", "digital value"), ("b_i", "i-th bit, 0 or 1"), ("N", "number of bits")]))]),
        ["A-SIG-EQ1"], "easy", "direct"),
    # --- ProcessFlow ---
    obj("PROCESS-DESIGN", "ProcessFlow", "1 > 1.1", "PKG-DESIGN-PROCESS",
        OrderedDict([("name", "Analog IC design process"),
                     ("steps", ["Definition", "Synthesis or implementation", "Simulation or modeling",
                                "Geometrical description (layout)", "Simulation including geometrical parasitics",
                                "Fabrication", "Testing and verification"]),
                     ("note", "Designer responsible for all steps except fabrication.")]),
        ["A-DESIGN-STEPS"], "medium", "direct"),
    obj("PROCESS-READPATH", "ProcessFlow", "1 > 1.4", "PKG-EX-READCHANNEL",
        OrderedDict([("name", "Read-channel read path"),
                     ("steps", ["VGA", "7-pole 2-zero low-pass filter", "buffer", "6-bit flash A/D (VCO-clocked)",
                                "FIR filter", "sequence detector (Viterbi)", "RLL decoder"])]),
        ["A-EX-READPATH"], "medium", "direct"),
    # --- DesignPrinciple ---
    obj("PRINCIPLE-SIM-PARASITIC", "DesignPrinciple", "1 > 1.1", "PKG-DESIGN-PROCESS",
        OrderedDict([("statement", "Confirm an analog IC by simulation rather than breadboarding; after layout, re-check simulations with extracted parasitics and iterate before fabrication."),
                     ("applies_to", ["analog IC design", "pre-fabrication verification"])]),
        ["A-DESIGN-SIM-PARASITIC"], "medium", "direct"),
    obj("PRINCIPLE-IC-VS-DISCRETE", "DesignPrinciple", "1 > 1.1", "PKG-ANALYSIS-DESIGN",
        OrderedDict([("statement", "Integrated analog design exploits a shared substrate (matching), designer-controlled geometry, and a limited technology-compatible component set, and relies on simulation since the IC cannot be breadboarded."),
                     ("applies_to", ["integrated analog circuit design"])]),
        ["A-IC-VS-DISCRETE"], "medium", "direct"),
    obj("PRINCIPLE-ANALOG-DIGITAL-PARTITION", "DesignPrinciple", "1 > 1.3", "PKG-SIGPROC",
        OrderedDict([("statement", "Partition a signal-processing system between analog and digital domains; favor digital for cost/speed/programmability, with analog used as needed; the boundary depends on application, performance, and area."),
                     ("applies_to", ["mixed-signal system partitioning"])]),
        ["A-SP-ANALOG-DIGITAL", "A-SP-BANDWIDTH"], "medium", "direct"),
    obj("PRINCIPLE-MASTER-PLL", "DesignPrinciple", "1 > 1.4", "PKG-EX-READCHANNEL",
        OrderedDict([("statement", "Tune a g_m-C filter by locking a replica-filter VCO (Master PLL) to an external reference, fixing the characteristic time constant C/g_m against process/temperature/supply variation; matching the LPF to the master filter fixes its response."),
                     ("applies_to", ["continuous-time g_m-C filter tuning"])]),
        ["A-EX-MASTERPLL"], "medium", "direct"),
    # --- ExampleProblem ---
    obj("EXAMPLE-READCHANNEL", "ExampleProblem", "1 > 1.4", "PKG-EX-READCHANNEL",
        OrderedDict([("title", "Disk-drive read/write channel IC"),
                     ("goal", "Illustrate analog IC design methodology on a fully-integrated mixed-signal chip."),
                     ("spec", "PRML sequence detection; up to 64 Mbits/sec; 0.8 um single-poly double-metal CMOS."),
                     ("result", "Demonstrates the hierarchical device->circuit->system structure of analog IC design.")]),
        ["A-EX-READCHANNEL", "A-EX-RESULT"], "medium", "direct"),
    # --- ProblemStatement ---
    obj("PROB-1", "ProblemStatement", "1 > PROBLEMS", "PKG-PROB",
        OrderedDict([("question", "Using Eq.(1), give the base-10 value of the 5-bit binary number 11010.")]),
        ["A-PROB-1"], "easy", "direct"),
    obj("PROB-3", "ProblemStatement", "1 > PROBLEMS", "PKG-PROB",
        OrderedDict([("question", "Digitize the sinusoid of Fig. P1.2 per Eq.(1) using a four-bit digitizer.")]),
        ["A-PROB-3"], "easy", "direct"),
]

OBJECT_IDS = [o["id"] for o in OBJECTS]

# ---------------------------------------------------------------------------
# context_packages: ONE per chunk. atoms == chunk.atom_ids. expected_objects =
# objects homed in this package. context_only-only packages carry a note.
# ---------------------------------------------------------------------------
DOC_TITLE = "CMOS Analog Circuit Design"

PKG_META = {
    "PKG-OV": OrderedDict([
        ("section_path", "Chapter 1 > Introduction and Background > chapter overview"),
        ("linked_context", OrderedDict([("previous_heading", "(book front matter)"), ("next_heading", "1.1 Analog Integrated Circuit Design")])),
        ("extraction_targets", ["Concept"]),
        ("note", "all atoms are chapter intro/summary framing (context_only:true); this package yields no typed object."),
    ]),
    "PKG-SIGNALS": OrderedDict([
        ("section_path", "Chapter 1 > 1.1 Analog Integrated Circuit Design > Signal Definitions"),
        ("linked_context", OrderedDict([
            ("figure_caption", "Figure 1.1-1 Signals. (a) Analog/continuous time. (b) Digital. (c) Analog sampled data."),
            ("formula_context", "Eq.(1) D = sum b_i 2^-i is the binary-weighted representation underlying the digital-signal definition."),
            ("previous_heading", "1.1 Analog Integrated Circuit Design"),
            ("next_heading", "Analysis vs Design"),
        ])),
        ("extraction_targets", ["Concept", "Formula"]),
    ]),
    "PKG-ANALYSIS-DESIGN": OrderedDict([
        ("section_path", "Chapter 1 > 1.1 Analog Integrated Circuit Design > Analysis vs Design, Integrated vs Discrete"),
        ("linked_context", OrderedDict([
            ("figure_caption", "Figure 1.1-2 (a) The analysis process. (b) The design process."),
            ("previous_heading", "Signal Definitions"),
            ("next_heading", "The Design Process"),
        ])),
        ("extraction_targets", ["Concept", "DesignPrinciple"]),
    ]),
    "PKG-DESIGN-PROCESS": OrderedDict([
        ("section_path", "Chapter 1 > 1.1 Analog Integrated Circuit Design > The Design Process"),
        ("linked_context", OrderedDict([
            ("figure_caption", "Figure 1.1-3 The design process for analog integrated circuits."),
            ("previous_heading", "Integrated vs Discrete Design"),
            ("next_heading", "Design Hierarchy (Table 1.1-1)"),
        ])),
        ("extraction_targets", ["ProcessFlow", "DesignPrinciple", "Concept"]),
    ]),
    "PKG-HIERARCHY": OrderedDict([
        ("section_path", "Chapter 1 > 1.1 Analog Integrated Circuit Design > Design Hierarchy"),
        ("linked_context", OrderedDict([
            ("table_caption", "Table 1.1-1 Hierarchy and Description of the Analog Circuit Design Process; Table 1.1-2 Relationship of the Book Chapters to Analog Circuit Design."),
            ("previous_heading", "The Design Process"),
            ("next_heading", "1.2 Notation, Symbology, and Terminology"),
        ])),
        ("extraction_targets", ["Concept"]),
    ]),
    "PKG-NOTATION": OrderedDict([
        ("section_path", "Chapter 1 > 1.2 Notation, Symbology, and Terminology"),
        ("linked_context", OrderedDict([
            ("table_caption", "Table 1.2-1 Definition of the Symbols for Various Signals."),
            ("figure_caption", "Figure 1.2-1 (notation applied to a periodic signal on a dc value)."),
            ("previous_heading", "Design Hierarchy"),
            ("next_heading", "1.3 Analog Signal Processing"),
        ])),
        ("extraction_targets", ["Concept"]),
        ("note", "A-NOTATION-INTRO frames the notation convention; the typed Concept objects in this package are scored on the Table 1.2-1 rows. No standalone Concept object is homed here (notation rows are evidence for the convention table, not separate gold objects in v0.1); package expected_objects is empty."),
    ]),
    "PKG-SIGPROC": OrderedDict([
        ("section_path", "Chapter 1 > 1.3 Analog Signal Processing"),
        ("linked_context", OrderedDict([
            ("figure_caption", "Figure 1.3-1 A typical signal-processing system block diagram; Figure 1.3-2 frequency of signals; Figure 1.3-3 technology speeds."),
            ("previous_heading", "1.2 Notation, Symbology, and Terminology"),
            ("next_heading", "1.4 Example of Analog VLSI Mixed-Signal Circuit Design"),
        ])),
        ("extraction_targets", ["Concept", "DesignPrinciple"]),
    ]),
    "PKG-EX-READCHANNEL": OrderedDict([
        ("section_path", "Chapter 1 > 1.4 Example of Analog VLSI Mixed-Signal Circuit Design"),
        ("linked_context", OrderedDict([
            ("figure_caption", "Figure 1.4-1 Read/Write channel block diagram; Figure 1.4-3 Master filter phase-locked loop; Figure 1.4-6 read-channel photomicrograph."),
            ("previous_heading", "1.3 Analog Signal Processing"),
            ("next_heading", "1.5 Summary"),
        ])),
        ("extraction_targets", ["ExampleProblem", "ProcessFlow", "DesignPrinciple"]),
    ]),
    "PKG-PROB": OrderedDict([
        ("section_path", "Chapter 1 > PROBLEMS"),
        ("linked_context", OrderedDict([
            ("previous_heading", "1.5 Summary"),
            ("next_heading", "REFERENCES"),
        ])),
        ("extraction_targets", ["ProblemStatement"]),
    ]),
}


def build_package(pid, chunk_id):
    meta = PKG_META[pid]
    home_objs = [o["id"] for o in OBJECTS if o["home_package"] == pid]
    d = OrderedDict([
        ("id", pid),
        ("profile", "textbook"),
        ("chunk_id", chunk_id),
        ("section_path", meta["section_path"]),
        ("document_title", DOC_TITLE),
        ("atoms", [OrderedDict([("atom_id", aid), ("atom_type", ATOM_TYPE[aid])])
                   for aid in CHUNK_ATOMS[chunk_id]]),
        ("linked_context", meta["linked_context"]),
        ("extraction_targets", meta["extraction_targets"]),
        ("expected_objects", home_objs),
    ])
    if "note" in meta:
        d["note"] = meta["note"]
    return d


CONTEXT_PACKAGES = [build_package(CHUNK_PKG[c["id"]], c["id"]) for c in SEMANTIC_CHUNKS]

# ---------------------------------------------------------------------------
# mentions (same 12 as v0.1).
# ---------------------------------------------------------------------------
def men(mid, text, mtype, atom_id, key):
    return OrderedDict([("id", mid), ("text", text), ("type", mtype),
                        ("atom_id", atom_id), ("canonical_key", key)])


MENTIONS = [
    men("M-01", "analog signal", "Concept", "A-SIG-ANALOG", "analog_signal"),
    men("M-02", "digital signal", "Concept", "A-SIG-DIGITAL", "digital_signal"),
    men("M-03", "analog sampled-data signal", "Concept", "A-SIG-SAMPLED", "sampled_data_signal"),
    men("M-04", "binary-weighted sum", "Formula", "A-SIG-EQ1", "digital_representation"),
    men("M-05", "synthesis (design)", "Concept", "A-ANALYSIS-DESIGN", "design_synthesis"),
    men("M-06", "analysis", "Concept", "A-ANALYSIS-DESIGN", "circuit_analysis"),
    men("M-07", "design process", "ProcessFlow", "A-DESIGN-STEPS", "analog_ic_design_process"),
    men("M-08", "parasitic extraction", "DesignPrinciple", "A-DESIGN-SIM-PARASITIC", "post_layout_parasitic_sim"),
    men("M-09", "design hierarchy", "Concept", "A-HIER-CONCEPT", "design_hierarchy"),
    men("M-10", "signal-processing system", "Concept", "A-SP-SYSTEM", "signal_processing_system"),
    men("M-11", "read/write channel", "ExampleProblem", "A-EX-READCHANNEL", "read_channel_example"),
    men("M-12", "Master PLL", "DesignPrinciple", "A-EX-MASTERPLL", "master_pll_tuning"),
]

CANONICALIZATION = [
    OrderedDict([("canonical", "digital_representation"),
                 ("aliases", ["binary-weighted sum", "Eq.(1)", "digital representation", "D = sum b_i 2^-i"])]),
    OrderedDict([("canonical", "analog_ic_design_process"),
                 ("aliases", ["design process", "the major steps in the design process", "Fig. 1.1-3 design process"])]),
    OrderedDict([("canonical", "post_layout_parasitic_sim"),
                 ("aliases", ["parasitic extraction", "simulation including the geometrical parasitics", "post-layout simulation"]),
                 ("note", "正文步骤 5 'Simulation including the geometrical parasitics' 与 Fig.1.1-3 节点 'Parasitic extraction' 同指。")]),
    OrderedDict([("canonical", "analog_vs_digital"),
                 ("aliases", ["analog signal", "digital signal", "analog and digital"]),
                 ("note", "analog/digital 既是两类信号也是两类设计方法；对比关系见 relations R-01。")]),
]

# ---------------------------------------------------------------------------
# relations (same 13 IDs / endpoints / meaning as v0.1; drop confidence; add
# gold_label / difficulty / evidence_strength). Textbook relation_types kept.
# ---------------------------------------------------------------------------
def rel(rid, rtype, src, tgt, atoms, difficulty, evidence_strength):
    return OrderedDict([
        ("id", rid), ("relation_type", rtype),
        ("source_object_id", src), ("target_object_id", tgt),
        ("evidence_atom_ids", atoms),
        ("gold_label", True), ("difficulty", difficulty), ("evidence_strength", evidence_strength),
    ])


RELATIONS = [
    rel("R-01", "concept_contrasts_with_concept", "CONCEPT-ANALOG-SIGNAL", "CONCEPT-DIGITAL-SIGNAL",
        ["A-SIG-ANALOG", "A-SIG-DIGITAL"], "easy", "direct"),
    rel("R-02", "concept_contrasts_with_concept", "CONCEPT-SAMPLED-SIGNAL", "CONCEPT-ANALOG-SIGNAL",
        ["A-SIG-SAMPLED", "A-SIG-ANALOG"], "medium", "direct"),
    rel("R-03", "concept_contrasts_with_concept", "CONCEPT-ANALYSIS", "CONCEPT-DESIGN",
        ["A-ANALYSIS-DESIGN"], "easy", "direct"),
    rel("R-04", "formula_defines_variable", "FORMULA-DIGITAL-REP", "CONCEPT-DIGITAL-SIGNAL",
        ["A-SIG-EQ1", "A-SIG-DIGITAL"], "medium", "direct"),
    rel("R-05", "process_flow_has_step", "PROCESS-DESIGN", "PRINCIPLE-SIM-PARASITIC",
        ["A-DESIGN-STEPS", "A-DESIGN-SIM-PARASITIC"], "medium", "direct"),
    rel("R-06", "design_principle_applies_to_scenario", "PRINCIPLE-SIM-PARASITIC", "PROCESS-DESIGN",
        ["A-DESIGN-SIM-PARASITIC"], "medium", "direct"),
    rel("R-07", "design_principle_applies_to_scenario", "PRINCIPLE-IC-VS-DISCRETE", "PROCESS-DESIGN",
        ["A-IC-VS-DISCRETE"], "medium", "indirect"),
    rel("R-08", "concept_defines_term", "CONCEPT-HIERARCHY", "CONCEPT-DESIGN-FORMATS",
        ["A-HIER-CONCEPT", "A-DESIGN-FORMATS"], "medium", "direct"),
    rel("R-09", "design_principle_applies_to_scenario", "PRINCIPLE-ANALOG-DIGITAL-PARTITION", "CONCEPT-SP-SYSTEM",
        ["A-SP-ANALOG-DIGITAL", "A-SP-SYSTEM"], "medium", "direct"),
    rel("R-10", "process_flow_has_step", "EXAMPLE-READCHANNEL", "PROCESS-READPATH",
        ["A-EX-READCHANNEL", "A-EX-READPATH"], "medium", "direct"),
    rel("R-11", "design_principle_applies_to_scenario", "PRINCIPLE-MASTER-PLL", "PROCESS-READPATH",
        ["A-EX-MASTERPLL", "A-EX-READPATH"], "medium", "direct"),
    rel("R-12", "formula_used_in_example", "FORMULA-DIGITAL-REP", "PROB-1",
        ["A-PROB-1"], "easy", "direct"),
    rel("R-13", "formula_used_in_example", "FORMULA-DIGITAL-REP", "PROB-3",
        ["A-PROB-3"], "easy", "direct"),
]

# ---------------------------------------------------------------------------
# do_not_extract negatives (textbook conventions, as in CMOS ch02).
# ---------------------------------------------------------------------------
DO_NOT_EXTRACT = [
    OrderedDict([
        ("pattern", "bracket_reference_citation"),
        ("examples", ["[1]", "[2]", "[3]", "[4]", "[5]", "[6]"]),
        ("reason", "inline numeric reference citations (e.g. analog-MOS attempts [1,2,3,4], silicon-gate CMOS [5,6], tuning [3], comparator offset [4]) are not knowledge objects"),
        ("kind", "citation_policy"),
    ]),
    OrderedDict([
        ("text", "[3]"), ("atom_id", "A-EX-MASTERPLL"),
        ("reason", "inline numeric reference citation kept verbatim in raw_text; not a knowledge object"),
        ("kind", "citation"),
    ]),
    OrderedDict([
        ("pattern", "figure_or_equation_cross_reference"),
        ("examples", ["Fig. 1.1-1(a)", "Fig. 1.1-2(a)", "Fig. 1.1-3", "Fig. 1.3-1", "Fig. 1.4-1", "Fig. 1.4-3", "Eq. (1)"]),
        ("reason", "cross-references to figures/equations are pointers, not objects to extract"),
        ("kind", "reference"),
    ]),
    OrderedDict([
        ("pattern", "figure_internal_label"),
        ("examples", ["VCON", "VCOREF", "VCO", "Gain Control", "Low-Pass Filter", "RLL Encoder", "Write Precompensation"]),
        ("reason", "OCR'd node labels inside the <details> mermaid/flowchart blocks of Fig.1.4-1/1.4-4 (rendered as a corrupted graph of duplicate VCO/VCOREF edges) are not knowledge objects"),
        ("kind", "figure_label"),
    ]),
    OrderedDict([
        ("pattern", "image_markup"),
        ("examples", ["![](images/<hash>.jpg)"]),
        ("reason", "image embeds carry no extractable text"),
        ("kind", "image_markup"),
    ]),
    OrderedDict([
        ("pattern", "out_of_chapter_cross_reference"),
        ("examples", ["Chapter 2", "Chapter 3", "Appendix B", "Appendix A", "Chapter 9", "Chapter 10"]),
        ("reason", "references to other chapters/appendices (e.g. the Table 1.1-2 chapter map and the 1.5 'study Appendix A' pointer) are not Chapter-1 objects"),
        ("kind", "cross_reference"),
    ]),
]

# ---------------------------------------------------------------------------
# source_meta
# ---------------------------------------------------------------------------
SOURCE_META = OrderedDict([
    ("source_id", "cmos"),
    ("scope", "Chapter 1 (Introduction and Background) only"),
    ("title", "CMOS Analog Circuit Design - Allen & Holberg - Chapter 1 Introduction and Background"),
    ("source_file", SOURCE_NAME),
    ("source_line_range", [SLICE_LINE_START, SLICE_LINE_END]),
    ("viewer_file", "source.md (human-readable verbatim slice of source_file lines %d-%d; NOT the authoritative source)" % (SLICE_LINE_START, SLICE_LINE_END)),
    ("profile", "textbook"),
    ("profile_detected_as", "textbook"),
    ("profile_cues", ["Chapter 1", "1.1 Analog Integrated Circuit Design", "Eq. (1)", "Table 1.1-1",
                      "Table 1.1-2", "Table 1.2-1", "Fig. 1.1-1", "PROBLEMS"]),
    ("extraction_targets", ["Concept", "Formula", "ProcessFlow", "DesignPrinciple", "ExampleProblem", "ProblemStatement"]),
    ("conventions", OrderedDict([
        ("coordinate_policy", "AUTHORITATIVE coordinates are atom.source_span (file=source_file). Evaluators MUST verify source_file[source_span.char_start:char_end] == raw_text. atom.viewer_span (file=source.md) is OPTIONAL/viewer_only and MUST NOT be the primary evaluation coordinate."),
        ("raw_vs_normalized", "raw_text is a verbatim contiguous span of source_file (it may include inline [n] citations, MinerU LaTeX in $...$ / $$...$$, and the missing-space artifacts MinerU introduces, e.g. 'analogdigital', 'Chapter 9Switched CapacitorCircuits', '0.8 m'). normalized_text renders EXACTLY that span: it may rephrase, transliterate math (phi/rho/eps/mu, b_i 2^-i), and condense, but MUST NOT introduce facts/names/section-refs not in the span. EXCEPTION: a formula_atom may carry symbolic interpretation via metadata.supported_by_context_atoms."),
        ("object_evidence", "Each object lists local_evidence_atom_ids (atoms inside its home_package's chunk) + supporting_context_atom_ids (atoms from other chunks). Package object-recall (expected_objects) is scored on local evidence only."),
        ("expected_local_fields", "For a cross-package object, a package may declare expected_local_fields[object_id] = payload fields derivable from that package alone. Chapter 1 has no cross-package object, so no package declares expected_local_fields."),
        ("labels", "gold objects/relations use gold_label + difficulty + evidence_strength instead of a decimal confidence."),
        ("tables_and_figures", "The three tables (Table 1.1-1 hierarchy, Table 1.1-2 design-level->chapter map with colspan=3, Table 1.2-1 signal notation) render as single-line HTML <table> elements; caption atoms anchor the 'Table 1.x-1' caption line and header/row atoms anchor <tr>/<td> substrings of the <table> line. Waveform sub-tables and flowcharts render inside <details> blocks; Fig.1.4-1/1.4-4 mermaid is corrupted (duplicate VCO/VCOREF edges) and is NOT used as graph evidence -- only the prose read-path is. Figure-internal OCR labels and inline [n] citations are listed in do_not_extract."),
        ("context_only", "an atom with context_only:true is background/framing (chapter intro A-INTRO-CMOS/A-INTRO-CAD and the 1.5 summary A-SUMMARY) that should NOT yield a typed object; its home package (PKG-OV) legitimately lists no expected_objects."),
        ("packages", "a context_package.atoms list IS the LLM input and equals its chunk's atom_ids; expected_objects are the objects whose home_package is that package."),
    ])),
    ("validation", OrderedDict([
        ("required", "for every atom: source_file[source_span.char_start:source_span.char_end] == raw_text"),
        ("optional_debug", "for every atom with viewer_span: source.md[viewer_span.char_start:viewer_span.char_end] == raw_text"),
    ])),
    ("parsing_notes", [
        "Chapter 1 is definition-heavy with almost no formulas: Eq.(1) is the chapter's only numbered display formula ($$ ... \\tag {1} $$, source line 16, the binary-weighted digital representation). Formula tags reset per sub-section; A-SIG-EQ1 is located by tag 1 within the 1.1 line window.",
        "MinerU rendered all three tables as single-line HTML <table> elements: Table 1.1-1 (line 170, hierarchy x design/physical/model), Table 1.1-2 (line 176, design-level->chapter map, with colspan=3 'CMOS Technology' banner and dropped spaces e.g. 'Chapter 9Switched CapacitorCircuits'), and Table 1.2-1 (line 188, signal notation with $q_{A}$/$Q_{A}$/$q_{a}$/$Q_{a}$ example cells).",
        "Waveform sub-tables and block diagrams render inside <details><summary>line|flowchart|text_image|other</summary>...</details>; analysis/design, design-process, and signal-processing-system diagrams are ```mermaid``` flowcharts.",
        "Fig.1.4-1/Fig.1.4-4 mermaid is parsed by MinerU into a corrupted graph (many duplicate pseudo VCO/VCOREF/Gain-Control edges) and is NOT trusted; the 1.4 example uses only the prose-described read-path steps. A-EX-READPATH anchors the verbatim read-path start (line 371); downstream stages are on lines 512/514/516 and inform the object payload.",
        "The 7-step design list is a numbered list (source lines 113-119, A-DESIGN-STEPS spans the contiguous 'The major steps...:' through '7. Testing and verification'); Fig.1.1-3 flowchart node wording differs slightly (Implementation/Physical definition/Parasitic extraction) and is treated as corroboration, prose list as authoritative.",
        "Fig.1.3-2/1.3-3 frequency tables are OCR'd into many '~10^1' values that are clearly distorted; their numbers are NOT extracted, only the qualitative bandwidth/CMOS-integration conclusions (A-SP-BANDWIDTH).",
        "MinerU drops some spaces ('analogdigital', 'computersimulate', 'readchannel') and the micro sign ('0.8 m' for 0.8 um); raw_text preserves these artifacts verbatim while normalized_text restores readable forms.",
        "Inline reference citations are numeric brackets [1]..[6] (the chapter's REFERENCES list is at source lines 732-739, outside the 1.x/PROBLEMS content); they are kept verbatim in raw_text and listed in do_not_extract.",
    ]),
])

# ---------------------------------------------------------------------------
# Assemble + dump.
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
        "# Gold fixture v0.3.3-textbook - CMOS Analog Circuit Design, Chapter 1 Introduction and Background (profile: textbook)\n"
        "# Upgraded from v0.1 WITHOUT changing atom/chunk/object/relation/mention IDs or meaning:\n"
        "# dual coordinates (source_span authoritative + viewer_span), verbatim raw_text +\n"
        "# within-span normalized_text, object evidence split into local/supporting + home_package,\n"
        "# one context_package per chunk with expected_objects, labels gold_label/difficulty/evidence_strength\n"
        "# (NO decimal confidence), bracket-citation / cross-ref / figure-label do_not_extract negatives,\n"
        "# chapter intro/summary atoms flagged context_only. TEXTBOOK type vocabulary retained (NOT article).\n"
        "# Spans are computed by build.py against the MinerU source; do NOT hand-edit.\n"
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
