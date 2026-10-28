"""Office Math (OMML) → LaTeX, for the local DOCX / PPTX fallback parsers.

Word and PowerPoint store equations as OMML (`m:oMath` inline, `m:oMathPara`
display). mammoth drops them with an "unrecognised element" warning,
python-docx's `paragraph.text` and python-pptx's `text_frame.text` skip them,
and PowerPoint additionally wraps every equation shape in `mc:AlternateContent`,
which python-pptx's shape tree never yields — so before this module every
formula in a locally parsed Office file vanished, often with the text box
around it.

The conversion is a deterministic XML walk with no dependency beyond the
stdlib. It works on both ElementTree and lxml elements (python-docx / python-pptx
hand out lxml nodes); only the shared `.tag` / `.attrib` / `.text` / iteration
API is used. Output follows the repo's MinerU convention so retrieval, KG
Formula extraction and the frontend need no change:

* display math (`m:oMathPara`) becomes its own `formula` element whose text is
  bare LaTeX (no delimiters) with ``metadata.text_format == "latex"``;
* inline math (`m:oMath` inside running text) stays in the paragraph text as
  ``$latex$``.

Never raises: a construct this module does not know degrades to its text
content (every `m:t` under it, symbol-mapped), and an unexpected failure in a
whole formula degrades the same way. Text is never dropped.
"""

from __future__ import annotations

import re
import tempfile
import unicodedata
import zipfile
from typing import Any, Dict, List, Sequence, Tuple
from xml.etree.ElementTree import TreeBuilder
from xml.parsers import expat

OMML_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "http://purl.oclc.org/ooxml/officeDocument/math",
)
_OMML_PREFIXES = tuple("{%s}" % ns for ns in OMML_NAMESPACES)
#: Clark tags of the two math roots, for `element.iter(tag)` presence checks.
OMML_ROOT_TAGS = tuple(
    f"{prefix}{local}" for prefix in _OMML_PREFIXES for local in ("oMath", "oMathPara")
)

_W_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
#: Byte-level pre-check for a part that may hold math: every OMML root tag
#: contains it, while the `m:` namespace declaration Word / PowerPoint write
#: into math-free parts does not. A hit only means "parse and look".
OMML_MARKER = b"oMath"

# --- symbol tables ---------------------------------------------------------

_GREEK = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\varepsilon", "ϵ": r"\epsilon", "ζ": r"\zeta", "η": r"\eta",
    "θ": r"\theta", "ϑ": r"\vartheta", "ι": r"\iota", "κ": r"\kappa",
    "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "ο": "o",
    "π": r"\pi", "ϖ": r"\varpi", "ρ": r"\rho", "ϱ": r"\varrho",
    "σ": r"\sigma", "ς": r"\varsigma", "τ": r"\tau", "υ": r"\upsilon",
    "φ": r"\varphi", "ϕ": r"\phi", "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Α": "A", "Β": "B", "Γ": r"\Gamma", "Δ": r"\Delta", "Ε": "E", "Ζ": "Z",
    "Η": "H", "Θ": r"\Theta", "Ι": "I", "Κ": "K", "Λ": r"\Lambda", "Μ": "M",
    "Ν": "N", "Ξ": r"\Xi", "Ο": "O", "Π": r"\Pi", "Ρ": "P", "Σ": r"\Sigma",
    "Τ": "T", "Υ": r"\Upsilon", "Φ": r"\Phi", "Χ": "X", "Ψ": r"\Psi",
    "Ω": r"\Omega",
}

_SYMBOLS = {
    **_GREEK,
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div", "·": r"\cdot",
    "⋅": r"\cdot", "∗": r"\ast", "∘": r"\circ", "∙": r"\bullet",
    "≤": r"\le", "≥": r"\ge", "≦": r"\leqq", "≧": r"\geqq", "≠": r"\ne",
    "≈": r"\approx", "≡": r"\equiv", "∼": r"\sim", "≃": r"\simeq",
    "≅": r"\cong", "∝": r"\propto", "≪": r"\ll", "≫": r"\gg",
    "≜": r"\triangleq", "≔": r"\coloneqq", "≺": r"\prec", "≻": r"\succ",
    "∈": r"\in", "∉": r"\notin", "∋": r"\ni", "⊂": r"\subset",
    "⊃": r"\supset", "⊆": r"\subseteq", "⊇": r"\supseteq", "∪": r"\cup",
    "∩": r"\cap", "∅": r"\emptyset", "∖": r"\setminus",
    "∞": r"\infty", "∂": r"\partial", "∇": r"\nabla", "∀": r"\forall",
    "∃": r"\exists", "∄": r"\nexists", "¬": r"\neg", "∧": r"\wedge",
    "∨": r"\vee", "⊕": r"\oplus", "⊗": r"\otimes", "⊙": r"\odot",
    "⊥": r"\perp", "∥": r"\parallel", "∠": r"\angle", "△": r"\triangle",
    "→": r"\rightarrow", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "↦": r"\mapsto", "↑": r"\uparrow", "↓": r"\downarrow",
    "⟶": r"\longrightarrow", "⟹": r"\Longrightarrow",
    "…": r"\ldots", "⋯": r"\cdots", "⋮": r"\vdots", "⋱": r"\ddots",
    "ℏ": r"\hbar", "ℓ": r"\ell", "ℜ": r"\Re", "ℑ": r"\Im", "ℵ": r"\aleph",
    "℘": r"\wp", "†": r"\dagger", "‡": r"\ddagger", "°": r"^{\circ}",
    "′": "'", "″": "''", "‴": "'''", "√": r"\surd",
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∮": r"\oint",
    "⟨": r"\langle", "⟩": r"\rangle", "‖": r"\|", "⌊": r"\lfloor",
    "⌋": r"\rfloor", "⌈": r"\lceil", "⌉": r"\rceil",
    "−": "-", "∶": ":",
    # Invisible operators Word inserts (function application, invisible
    # times / separator / plus) and zero-width space carry no glyph.
    "\u2061": "", "\u2062": "", "\u2063": "", "\u2064": "", "\u200b": "",
    "\u00a0": " ", "\u2009": " ", "\u2005": " ", "\u2006": " ",
    # LaTeX-active characters.
    "\\": r"\backslash", "{": r"\{", "}": r"\}", "#": r"\#", "$": r"\$",
    "%": r"\%", "&": r"\&", "_": r"\_", "~": r"\sim", "^": r"\hat{}",
}

#: Characters that escape differently inside `\text{...}`.
_TEXT_MODE_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "#": r"\#", "$": r"\$",
    "%": r"\%", "&": r"\&", "_": r"\_", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_NARY_OPERATORS = {
    "∑": r"\sum", "∏": r"\prod", "∐": r"\coprod", "∫": r"\int",
    "∬": r"\iint", "∭": r"\iiint", "∮": r"\oint", "∯": r"\oiint",
    "∰": r"\oiiint", "⋃": r"\bigcup", "⋂": r"\bigcap", "⋁": r"\bigvee",
    "⋀": r"\bigwedge", "⨁": r"\bigoplus", "⨂": r"\bigotimes",
    "⨀": r"\bigodot", "⨄": r"\biguplus", "⨆": r"\bigsqcup",
}
_INTEGRALS = {r"\int", r"\iint", r"\iiint", r"\oint", r"\oiint", r"\oiiint"}

_DELIMITERS = {
    "(": "(", ")": ")", "[": "[", "]": "]", "{": r"\{", "}": r"\}",
    "|": "|", "‖": r"\|", "⟨": r"\langle", "〈": r"\langle", "⟩": r"\rangle",
    "〉": r"\rangle", "⌊": r"\lfloor", "⌋": r"\rfloor", "⌈": r"\lceil",
    "⌉": r"\rceil", "/": "/",
}

_ACCENTS = {
    "\u0302": r"\hat", "^": r"\hat", "\u0303": r"\tilde", "~": r"\tilde",
    "\u0307": r"\dot", "˙": r"\dot", "\u0308": r"\ddot", "¨": r"\ddot",
    "\u20db": r"\dddot", "\u20d7": r"\vec", "→": r"\vec",
    "\u0304": r"\bar", "\u0305": r"\overline", "¯": r"\bar",
    "\u0301": r"\acute", "´": r"\acute", "\u0300": r"\grave", "`": r"\grave",
    "\u0306": r"\breve", "˘": r"\breve", "\u030c": r"\check", "ˇ": r"\check",
    "\u20d6": r"\overleftarrow", "\u20e1": r"\overleftrightarrow",
}

_FUNCTION_NAMES = frozenset({
    "sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "coth",
    "arcsin", "arccos", "arctan", "log", "ln", "lg", "exp", "lim", "liminf",
    "limsup", "max", "min", "sup", "inf", "det", "dim", "ker", "hom", "deg",
    "arg", "gcd", "Pr",
})
#: Operators whose under/over limits read as sub/superscripts (`\lim_{n\to\infty}`).
_LIMIT_OPERATORS = frozenset(
    {r"\lim", r"\liminf", r"\limsup", r"\max", r"\min", r"\sup", r"\inf",
     r"\det", r"\gcd", r"\Pr", r"\arg"}
)

_STYLE_WRAPPERS = {"b": r"\mathbf", "bi": r"\boldsymbol"}
_SCRIPT_WRAPPERS = {
    "double-struck": r"\mathbb", "script": r"\mathcal", "fraktur": r"\mathfrak",
    "sans-serif": r"\mathsf", "monospace": r"\mathtt",
}

_TRAILING_COMMAND = re.compile(r"\\[A-Za-z]+$")
_SINGLE_TOKEN = re.compile(r"^(?:[A-Za-z0-9]|\\[A-Za-z]+)$")
_FALSE_VALUES = frozenset({"0", "off", "false"})


# --- element helpers -------------------------------------------------------


def _omml_local(tag: Any) -> str:
    """Local name of an OMML-namespace tag, "" for any other tag or a comment."""
    if not isinstance(tag, str):
        return ""
    for prefix in _OMML_PREFIXES:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return ""


def _local(tag: Any) -> str:
    """Local name of any namespaced tag ("" for comments / processing instructions)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(element: Any) -> List[Any]:
    return [child for child in element if isinstance(child.tag, str)]


def _omml_child(element: Any, local: str) -> Any:
    for child in element:
        if _omml_local(child.tag) == local:
            return child
    return None


def _omml_children(element: Any, local: str) -> List[Any]:
    return [child for child in element if _omml_local(child.tag) == local]


def _val(element: Any) -> str | None:
    for key, value in element.attrib.items():
        if _local(key) == "val":
            return value
    return None


def _property(element: Any, properties: str, name: str) -> str | None:
    """`m:val` of `<properties><name m:val=.../></properties>`; None when absent.

    A present property element without `m:val` returns "" — for the on/off
    flags that means "on" (see `_flag`), for characters it means "none".
    """
    bag = _omml_child(element, properties)
    if bag is None:
        return None
    prop = _omml_child(bag, name)
    if prop is None:
        return None
    value = _val(prop)
    return "" if value is None else value


def _flag(element: Any, properties: str, name: str) -> bool:
    value = _property(element, properties, name)
    return value is not None and value.lower() not in _FALSE_VALUES


def contains_omml(element: Any) -> bool:
    """True when `element` or any descendant is an OMML math root."""
    try:
        # lxml (python-docx / python-pptx trees): one C-level pass for all tags.
        found = element.iter(*OMML_ROOT_TAGS)
    except TypeError:
        # ElementTree takes one tag per pass.
        return any(next(element.iter(tag), None) is not None for tag in OMML_ROOT_TAGS)
    return next(found, None) is not None


# --- LaTeX assembly --------------------------------------------------------


def _join(parts: Sequence[str]) -> str:
    """Concatenate LaTeX fragments, inserting a space only where a control word
    would otherwise swallow the next letter (`\\alpha` + `x` ≠ `\\alphax`)."""
    out: List[str] = []
    for part in parts:
        if not part:
            continue
        if out and part[0].isascii() and part[0].isalpha() and _TRAILING_COMMAND.search(out[-1]):
            out.append(" ")
        out.append(part)
    return "".join(out)


def _base(latex: str) -> str:
    """Brace a script base unless it is one character or one control word."""
    return latex if _SINGLE_TOKEN.match(latex) else "{" + latex + "}"


def _is_cjk(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")


def _map_char(char: str, raw_ampersand: bool) -> str:
    if char == "&" and raw_ampersand:
        return "&"
    mapped = _SYMBOLS.get(char)
    if mapped is not None:
        return mapped
    code = ord(char)
    if 0x1D400 <= code <= 0x1D7FF:
        # Mathematical Alphanumeric Symbols (𝑥, 𝛼, 𝟐): fold to the base glyph.
        base = unicodedata.normalize("NFKC", char)
        if base != char:
            return "".join(_map_char(c, raw_ampersand) for c in base)
    return char


def _map_text(text: str, raw_ampersand: bool = False) -> str:
    return _join([_map_char(char, raw_ampersand) for char in text])


def _text_mode(text: str) -> str:
    return "".join(_TEXT_MODE_ESCAPES.get(char, char) for char in text)


def _plain_text(element: Any) -> str:
    """Every visible `t` under `element`, in document order (the degradation path)."""
    parts: List[str] = []
    stack = [element]
    while stack:
        node = stack.pop()
        local = _local(node.tag)
        if local in ("del", "moveFrom", "Fallback") or local.endswith("Pr"):
            continue
        if local == "t" and node.text:
            parts.append(node.text)
            continue
        stack.extend(reversed(_children(node)))
    return "".join(parts)


class _Converter:
    def __init__(self) -> None:
        self._function_name_depth = 0
        self._equation_array_depth = 0

    # -- dispatch ----------------------------------------------------------
    def convert(self, element: Any) -> str:
        local = _omml_local(element.tag)
        if local:
            handler = self._HANDLERS.get(local)
            if handler is not None:
                return handler(self, element)
            if local.endswith("Pr"):
                return ""  # property bag (fPr, naryPr, ctrlPr, ...)
            # Unknown OMML construct: keep whatever its children say. Leaves
            # are runs (`m:r`), so its text survives even when its layout
            # meaning is lost.
            return self.sequence(element)
        return self._foreign(element)

    def _foreign(self, element: Any) -> str:
        """Non-OMML nodes inside a formula (w:/a: run properties, tracked
        changes, markup-compatibility wrappers)."""
        local = _local(element.tag)
        if local in ("del", "moveFrom", "Fallback", "delText") or local.endswith("Pr"):
            return ""
        if local == "t":
            return self._run_text(element.text or "")
        if local in ("tab", "br"):
            return " "
        if local == "AlternateContent":
            for child in element:
                if _local(child.tag) == "Choice":
                    return self.sequence(child)
            return ""
        return self.sequence(element)

    def sequence(self, element: Any) -> str:
        return _join([self.convert(child) for child in _children(element)])

    def _argument(self, element: Any, local: str) -> str:
        child = _omml_child(element, local)
        return "" if child is None else self.sequence(child)

    # -- runs --------------------------------------------------------------
    def _run_text(self, text: str) -> str:
        return _map_text(text, raw_ampersand=self._equation_array_depth > 0)

    def run(self, element: Any) -> str:
        text_parts: List[str] = []
        for child in _children(element):
            local = _local(child.tag)
            if local == "t":
                text_parts.append(child.text or "")
            elif local in ("tab", "br"):
                text_parts.append(" ")
        text = "".join(text_parts)
        if not text:
            return ""
        normal = _flag(element, "rPr", "nor")
        style = _property(element, "rPr", "sty")
        script = _property(element, "rPr", "scr")
        if normal or any(_is_cjk(char) for char in text):
            return r"\text{" + _text_mode(text) + "}"
        word = text.strip()
        if word.isascii() and word.isalpha():
            if word in _FUNCTION_NAMES and (style == "p" or self._function_name_depth):
                return "\\" + word
            if self._function_name_depth and len(word) > 1:
                return r"\operatorname{" + word + "}"
            if style == "p":
                return r"\mathrm{" + word + "}"
        latex = self._run_text(text)
        wrapper = _SCRIPT_WRAPPERS.get(script or "") or _STYLE_WRAPPERS.get(style or "")
        if wrapper and latex.strip():
            return wrapper + "{" + latex + "}"
        return latex

    # -- structures --------------------------------------------------------
    def fraction(self, element: Any) -> str:
        numerator = self._argument(element, "num")
        denominator = self._argument(element, "den")
        kind = _property(element, "fPr", "type")
        if kind in ("lin", "skw"):
            return _base(numerator) + "/" + _base(denominator)
        if kind == "noBar":
            return r"\genfrac{}{}{0pt}{}{" + numerator + "}{" + denominator + "}"
        return r"\frac{" + numerator + "}{" + denominator + "}"

    def superscript(self, element: Any) -> str:
        return _base(self._argument(element, "e")) + "^{" + self._argument(element, "sup") + "}"

    def subscript(self, element: Any) -> str:
        return _base(self._argument(element, "e")) + "_{" + self._argument(element, "sub") + "}"

    def sub_superscript(self, element: Any) -> str:
        return (
            _base(self._argument(element, "e"))
            + "_{" + self._argument(element, "sub") + "}"
            + "^{" + self._argument(element, "sup") + "}"
        )

    def pre_script(self, element: Any) -> str:
        return _join([
            "{}_{" + self._argument(element, "sub") + "}^{" + self._argument(element, "sup") + "}",
            self._argument(element, "e"),
        ])

    def radical(self, element: Any) -> str:
        body = self._argument(element, "e")
        degree = "" if _flag(element, "radPr", "degHide") else self._argument(element, "deg")
        if degree:
            return r"\sqrt[" + degree + "]{" + body + "}"
        return r"\sqrt{" + body + "}"

    def nary(self, element: Any) -> str:
        char = _property(element, "naryPr", "chr")
        char = "∫" if char is None else char
        operator = _NARY_OPERATORS.get(char) or _map_text(char)
        location = _property(element, "naryPr", "limLoc")
        if operator in _INTEGRALS and location == "undOvr":
            operator += r"\limits"
        elif operator not in _INTEGRALS and location == "subSup" and operator.startswith("\\"):
            operator += r"\nolimits"
        lower = "" if _flag(element, "naryPr", "subHide") else self._argument(element, "sub")
        upper = "" if _flag(element, "naryPr", "supHide") else self._argument(element, "sup")
        if lower:
            operator += "_{" + lower + "}"
        if upper:
            operator += "^{" + upper + "}"
        body = self._argument(element, "e")
        return operator + " " + body if body else operator

    def delimiter(self, element: Any) -> str:
        begin = _property(element, "dPr", "begChr")
        end = _property(element, "dPr", "endChr")
        separator = _property(element, "dPr", "sepChr")
        begin = "(" if begin is None else begin
        end = ")" if end is None else end
        separator = "|" if separator is None else separator
        if separator in ("|", "‖"):
            separator_latex = r"\middle" + _DELIMITERS[separator]
        else:
            separator_latex = _map_text(separator)
        items = [self.sequence(child) for child in _omml_children(element, "e")]
        inner = (" " + separator_latex + " ").join(items) if separator_latex else "".join(items)
        left = _DELIMITERS.get(begin) if begin else "."
        right = _DELIMITERS.get(end) if end else "."
        if left is None or right is None:
            # A bracket \left cannot take: keep the characters as plain text.
            return _join([_map_text(begin), inner, _map_text(end)])
        return _join([r"\left" + left, " ", inner, " ", r"\right" + right])

    def matrix(self, element: Any) -> str:
        rows = [
            " & ".join(self.sequence(cell) for cell in _omml_children(row, "e"))
            for row in _omml_children(element, "mr")
        ]
        return r"\begin{matrix} " + r" \\ ".join(rows) + r" \end{matrix}"

    def equation_array(self, element: Any) -> str:
        self._equation_array_depth += 1
        try:
            rows = [self.sequence(row) for row in _omml_children(element, "e")]
        finally:
            self._equation_array_depth -= 1
        environment = "aligned" if any("&" in row.replace(r"\&", "") for row in rows) else "gathered"
        return (
            r"\begin{" + environment + "} " + r" \\ ".join(rows) + r" \end{" + environment + "}"
        )

    def function(self, element: Any) -> str:
        self._function_name_depth += 1
        try:
            name = self._argument(element, "fName")
        finally:
            self._function_name_depth -= 1
        return name + "{" + self._argument(element, "e") + "}"

    def accent(self, element: Any) -> str:
        char = _property(element, "accPr", "chr")
        char = "\u0302" if char is None else char
        body = self._argument(element, "e")
        command = _ACCENTS.get(char)
        if command:
            return command + "{" + body + "}"
        if not char:
            return body
        return r"\overset{" + _map_text(char) + "}{" + body + "}"

    def bar(self, element: Any) -> str:
        position = _property(element, "barPr", "pos")
        command = r"\overline" if position == "top" else r"\underline"
        return command + "{" + self._argument(element, "e") + "}"

    def group_character(self, element: Any) -> str:
        char = _property(element, "groupChrPr", "chr")
        char = "\u23df" if char is None else char
        position = _property(element, "groupChrPr", "pos")
        body = self._argument(element, "e")
        if char == "\u23df":
            return r"\underbrace{" + body + "}"
        if char == "\u23de":
            return r"\overbrace{" + body + "}"
        if not char:
            return body
        command = r"\overset" if position == "top" else r"\underset"
        return command + "{" + _map_text(char) + "}{" + body + "}"

    def lower_limit(self, element: Any) -> str:
        body = self._argument(element, "e")
        limit = self._argument(element, "lim")
        if body.strip() in _LIMIT_OPERATORS:
            return body.strip() + "_{" + limit + "}"
        return r"\underset{" + limit + "}{" + body + "}"

    def upper_limit(self, element: Any) -> str:
        body = self._argument(element, "e")
        limit = self._argument(element, "lim")
        if body.strip() in _LIMIT_OPERATORS:
            return body.strip() + "^{" + limit + "}"
        return r"\overset{" + limit + "}{" + body + "}"

    def border_box(self, element: Any) -> str:
        return r"\boxed{" + self._argument(element, "e") + "}"

    def phantom(self, element: Any) -> str:
        body = self._argument(element, "e")
        show = _property(element, "phantPr", "show")
        if show is not None and show.lower() in _FALSE_VALUES:
            return r"\phantom{" + body + "}"
        return body

    def math_paragraph(self, element: Any) -> str:
        lines = [line for line in (self.convert(m) for m in _omml_children(element, "oMath")) if line]
        if len(lines) > 1:
            return r"\begin{gathered} " + r" \\ ".join(lines) + r" \end{gathered}"
        return lines[0] if lines else ""

    _HANDLERS: Dict[str, Any] = {
        "oMath": sequence,
        "oMathPara": math_paragraph,
        "r": run,
        "f": fraction,
        "sSup": superscript,
        "sSub": subscript,
        "sSubSup": sub_superscript,
        "sPre": pre_script,
        "rad": radical,
        "nary": nary,
        "d": delimiter,
        "m": matrix,
        "eqArr": equation_array,
        "func": function,
        "acc": accent,
        "bar": bar,
        "groupChr": group_character,
        "limLow": lower_limit,
        "limUpp": upper_limit,
        "box": sequence,
        "borderBox": border_box,
        "phant": phantom,
    }


def _normalize(latex: str) -> str:
    return " ".join(latex.split())


def omml_to_latex(element: Any) -> str:
    """LaTeX for one OMML element (normally `m:oMath` / `m:oMathPara`).

    Single line, no math delimiters, `$` escaped. Never raises: any failure
    (including a pathological nesting depth) degrades to the element's
    symbol-mapped text content.
    """
    try:
        return _normalize(_Converter().convert(element))
    except Exception:
        # Conversion must never cost the text: fall back to the plain symbols.
        try:
            return _normalize(_map_text(_plain_text(element)))
        except Exception:
            return ""


def omml_display_lines(element: Any) -> List[str]:
    """One LaTeX string per equation line of an `m:oMathPara` (each `m:oMath`
    child is a separately numbered display line in Word/PowerPoint). Any other
    element yields its single conversion. Empty lines are dropped."""
    if _omml_local(element.tag) == "oMathPara":
        lines = [omml_to_latex(child) for child in _omml_children(element, "oMath")]
    else:
        lines = [omml_to_latex(element)]
    return [line for line in lines if line]


def inline_math(latex: str) -> str:
    """The inline-formula form used inside running text (`$latex$`)."""
    return f"${latex}$" if latex else ""


def inline_math_for(element: Any) -> str:
    """Inline text for any math root; a multi-line display block collapses to
    one `gathered` formula because inline contexts cannot hold separate lines."""
    return inline_math(omml_to_latex(element))


# --- DrawingML / WordprocessingML text with math ----------------------------
#
# These walkers run only on a text body / paragraph that `contains_omml`
# already flagged: math-free content keeps the libraries' own `.text`, so its
# parse output stays byte-for-byte what it was.


def _drawingml_paragraph(node: Any, segments: List[Tuple[str, Any]]) -> None:
    for child in _children(node):
        omml = _omml_local(child.tag)
        if omml == "oMathPara":
            segments.append(("display", omml_display_lines(child)))
            continue
        if omml == "oMath":
            segments.append(("text", inline_math_for(child)))
            continue
        local = _local(child.tag)
        if local in ("r", "fld"):
            segments.append(("text", "".join(t.text or "" for t in child if _local(t.tag) == "t")))
        elif local == "br":
            segments.append(("text", "\v"))
        elif local == "AlternateContent":
            choice = next((c for c in child if _local(c.tag) == "Choice"), None)
            if choice is not None:
                _drawingml_paragraph(choice, segments)
        elif not local.endswith("Pr") and local != "Fallback":
            # `a14:m` and any other wrapper: look inside for runs and math.
            _drawingml_paragraph(child, segments)


def drawingml_segments(text_body: Any) -> List[Tuple[str, Any]]:
    """Reading-order segments of a DrawingML text body (`p:txBody` / `a:txBody`).

    `("text", str)` carries running text with inline math already rendered as
    `$latex$`; paragraphs are separated by a "\\n" text segment, like
    python-pptx's `text_frame.text`. `("display", [latex, ...])` is one
    `m:oMathPara`, one LaTeX string per equation line.
    """
    segments: List[Tuple[str, Any]] = []
    paragraphs = [child for child in text_body if _local(child.tag) == "p"]
    for index, paragraph in enumerate(paragraphs):
        if index:
            segments.append(("text", "\n"))
        _drawingml_paragraph(paragraph, segments)
    return segments


def drawingml_inline_text(text_body: Any) -> str:
    """`drawingml_segments` flattened to one string, display math inline."""
    return "".join(
        value if kind == "text" else "".join(inline_math(line) for line in value)
        for kind, value in drawingml_segments(text_body)
    )


def _wordprocessing_run_text(run: Any) -> str:
    parts: List[str] = []
    for child in _children(run):
        local = _local(child.tag)
        if local == "t":
            parts.append(child.text or "")
        elif local in ("tab", "ptab"):
            parts.append("\t")
        elif local in ("br", "cr"):
            parts.append("\n")
        elif local == "noBreakHyphen":
            parts.append("-")
    return "".join(parts)


def wordprocessing_paragraph_text(paragraph: Any) -> str:
    """A `w:p`'s text the way python-docx's `paragraph.text` reads it (direct
    runs and hyperlink runs), plus its math as inline `$latex$` in place."""
    parts: List[str] = []
    for child in _children(paragraph):
        omml = _omml_local(child.tag)
        if omml in ("oMath", "oMathPara"):
            parts.append(inline_math_for(child))
            continue
        local = _local(child.tag)
        if local == "r":
            parts.append(_wordprocessing_run_text(child))
        elif local == "hyperlink":
            for run in _children(child):
                if _omml_local(run.tag) in ("oMath", "oMathPara"):
                    parts.append(inline_math_for(run))
                elif _local(run.tag) == "r":
                    parts.append(_wordprocessing_run_text(run))
    return "".join(parts)


# --- DOCX package rewrite (mammoth path) ------------------------------------
#
# mammoth is a closed converter: it ignores `m:oMath` and offers no hook for
# unknown elements. So the package is rewritten before mammoth sees it: each
# outermost math element is replaced by a plain run whose text is a sentinel
# token (`<S>index<S>`, S a private-use character absent from every text part),
# and the LaTeX is kept out of band. The block parser then swaps tokens back:
# inline formulas as `$latex$`, display formulas as their own elements.
#
# The splice is byte-level, guided by expat's byte offsets, so every other
# byte of the part — prefixes, `mc:Ignorable`, unknown extensions — is left
# exactly as Word wrote it (an ElementTree round trip would rename prefixes).

_W_NAMESPACES = (_W_NAMESPACE, "http://purl.oclc.org/ooxml/wordprocessingml/main")
#: The parts mammoth turns into text (body, footnotes, endnotes, comments).
_DOCX_TEXT_PARTS = ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml", "word/comments.xml")
#: Containers whose direct children are block-level: a run spliced straight
#: into one of these needs a paragraph around it to be read at all.
_W_BLOCK_CONTAINERS = frozenset(
    {"body", "tc", "txbxContent", "footnote", "endnote", "comment", "hdr", "ftr", "docPartBody"}
)
#: The rewritten package spills to disk past this size, so a math-bearing DOCX
#: full of images never doubles its footprint in memory.
_PACKAGE_SPOOL_BYTES = 16 * 1024 * 1024
_NON_UTF8_DECLARATION = re.compile(rb"^\s*<\?xml[^>]*encoding=[\"'](?!utf-?8[\"'])", re.IGNORECASE)


class DocxFormula:
    __slots__ = ("display", "inline", "lines")

    def __init__(self, element: Any) -> None:
        self.display = _omml_local(element.tag) == "oMathPara"
        self.inline = inline_math_for(element)
        self.lines = omml_display_lines(element) if self.display else []


class DocxMath:
    """A DOCX package with its equations swapped for sentinel tokens."""

    def __init__(self, package: Any, formulas: List[DocxFormula], sentinel: str) -> None:
        self.package = package
        self.formulas = formulas
        self.token = re.compile(re.escape(sentinel) + r"(\d+)" + re.escape(sentinel))

    def formula(self, match: "re.Match[str]") -> DocxFormula | None:
        index = int(match.group(1))
        return self.formulas[index] if index < len(self.formulas) else None

    def close(self) -> None:
        self.package.close()


def _clark(name: str) -> str:
    uri, separator, local = name.rpartition(" ")
    return "{%s}%s" % (uri, local) if separator else name


class _DocxMathScanner:
    """One expat pass over a WordprocessingML part, collecting the byte span
    and an ElementTree copy of every outermost `m:oMath` / `m:oMathPara`."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.parser = expat.ParserCreate(namespace_separator=" ")
        self.parser.StartElementHandler = self._start
        self.parser.EndElementHandler = self._end
        self.parser.CharacterDataHandler = self._characters
        self.containers: List[str] = []
        self.builder: TreeBuilder | None = None
        self.depth = 0
        self.start_index = 0
        self.needs_paragraph = False
        self.spans: List[Tuple[int, int, Any, bool]] = []

    def scan(self) -> List[Tuple[int, int, Any, bool]]:
        self.parser.Parse(self.data, True)
        return self.spans

    def _start(self, name: str, attrs: Dict[str, str]) -> None:
        if self.builder is not None:
            self.depth += 1
            self.builder.start(_clark(name), {_clark(k): v for k, v in attrs.items()})
            return
        uri, _, local = name.rpartition(" ")
        if uri in OMML_NAMESPACES and local in ("oMath", "oMathPara"):
            self.builder = TreeBuilder()
            self.depth = 1
            self.start_index = self.parser.CurrentByteIndex
            self.needs_paragraph = not self.containers or self.containers[-1] != "p"
            self.builder.start(_clark(name), {_clark(k): v for k, v in attrs.items()})
            return
        if uri in _W_NAMESPACES and (local == "p" or local in _W_BLOCK_CONTAINERS):
            self.containers.append(local)

    def _end(self, name: str) -> None:
        if self.builder is not None:
            self.builder.end(_clark(name))
            self.depth -= 1
            if self.depth == 0:
                element = self.builder.close()
                self.builder = None
                index = self.parser.CurrentByteIndex
                # An empty `<m:oMath/>` has no end tag and nothing to say: leave it.
                if self.data.startswith(b"</", index):
                    end = self.data.index(b">", index) + 1
                    self.spans.append((self.start_index, end, element, self.needs_paragraph))
            return
        uri, _, local = name.rpartition(" ")
        if uri in _W_NAMESPACES and (local == "p" or local in _W_BLOCK_CONTAINERS):
            if self.containers:
                self.containers.pop()

    def _characters(self, text: str) -> None:
        if self.builder is not None:
            self.builder.data(text)


def _rewrite_docx_part(data: bytes, formulas: List[DocxFormula], sentinel: str) -> bytes | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or _NON_UTF8_DECLARATION.match(data):
        return None  # byte offsets below assume the UTF-8 that Word always writes
    try:
        spans = _DocxMathScanner(data).scan()
    except (expat.ExpatError, ValueError):
        return None
    if not spans:
        return None
    namespace = f' xmlns:w="{_W_NAMESPACE}"'.encode()
    run = b'<w:r><w:t xml:space="preserve">%s</w:t></w:r>'
    pieces: List[bytes] = []
    cursor = 0
    for start, end, element, needs_paragraph in spans:
        token = f"{sentinel}{len(formulas)}{sentinel}".encode("utf-8")
        formulas.append(DocxFormula(element))
        if needs_paragraph:
            replacement = b"<w:p" + namespace + b">" + run % token + b"</w:p>"
        else:
            replacement = b"<w:r" + namespace + (run % token)[len(b"<w:r"):]
        pieces.append(data[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(data[cursor:])
    return b"".join(pieces)


def _pick_sentinel(parts: Sequence[bytes]) -> str | None:
    for code in range(0xE000, 0xF900):
        char = chr(code)
        needles = (
            char.encode("utf-8"),
            f"&#x{code:x};".encode(),
            f"&#x{code:X};".encode(),
            f"&#{code};".encode(),
        )
        if not any(needle in part for part in parts for needle in needles):
            return char
    return None


def docx_math_package(path: Any) -> DocxMath | None:
    """The DOCX at `path` with its equations tokenised, or None when it has none.

    The gate is a substring check for `oMath` (`OMML_MARKER`) in the parts
    mammoth renders text from, so a math-free document costs one extra read of
    those parts and is handed to mammoth untouched. Styles, numbering, theme and
    settings parts are never read here (`settings.xml` holds `m:mathPr`, which
    is not math content).
    """
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = {info.filename for info in infos}
        if "word/document.xml" in names:
            text_parts = [name for name in _DOCX_TEXT_PARTS if name in names]
        else:
            # Non-standard main part name: fall back to every WordprocessingML part.
            text_parts = sorted(n for n in names if n.startswith("word/") and n.endswith(".xml"))
        word_parts = {name: archive.read(name) for name in text_parts}
        if not any(OMML_MARKER in data for data in word_parts.values()):
            return None
        sentinel = _pick_sentinel(list(word_parts.values()))
        if sentinel is None:
            return None
        formulas: List[DocxFormula] = []
        rewritten: Dict[str, bytes] = {}
        for name, data in word_parts.items():
            if OMML_MARKER in data:
                new_data = _rewrite_docx_part(data, formulas, sentinel)
                if new_data is not None:
                    rewritten[name] = new_data
        if not rewritten:
            return None
        package = tempfile.SpooledTemporaryFile(max_size=_PACKAGE_SPOOL_BYTES)
        try:
            with zipfile.ZipFile(package, "w", zipfile.ZIP_STORED) as out:
                for info in infos:
                    if info.filename in rewritten:
                        data = rewritten[info.filename]
                    elif info.filename in word_parts:
                        data = word_parts[info.filename]
                    else:
                        data = archive.read(info)
                    out.writestr(info.filename, data)
        except BaseException:
            package.close()
            raise
    package.seek(0)
    return DocxMath(package, formulas, sentinel)
