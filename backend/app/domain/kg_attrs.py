"""Read-time KG node attrs: Concept aliases and Formula variable verification.

Spec: docs/superpowers/specs/2026-10-28-kg-node-attrs-design_zh.md §7.2–§7.5.
Module-level pure functions only (no class methods, no Settings, no I/O except
``rules_signature`` reading this package's own source). Callers must write
``from app.domain import kg_attrs`` and call ``kg_attrs.<function>(...)`` —
``tests/test_kg_attrs_isolation_guard.py`` enforces the spelling so a runtime
spy patched on this module's attributes sees every call site.

Alias rules (each hit carries its ``rule``):

* R1 — the name itself is "Full (ACR)" and ACR is the initialism of Full.
* R2 — an element text contains "<name> (ACR)" / "<name>（ACR）" and ACR is the
  initialism of the name.
* R3 — the name is an ASCII acronym and an element text contains
  "Full (<name>)" where the name is the initialism of Full.
* R4 — the name contains CJK and an element text has "<name>(ACR)" right after
  it, ACR shaped like an acronym and not an instance label (M1, Q5).

R2–R4 run only on items whose ``element_id`` is non-empty AND whose
``element_read`` is True (the text was read from an element of this library,
not the 400-character stored quote of a dangling or foreign pointer).
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.domain import kg_attrs_contracts as contracts
from app.domain.kg_attrs_contracts import (
    ALIAS_CHARS_MAX,
    ALIAS_RULES,
    ALIASES_MAX,
    ATTR_SPECS,
    ENABLED_ALIAS_RULES,
    FORMULA_STATUS_FAILED,
    FORMULA_STATUS_NONE_GROUNDED,
    FORMULA_STATUS_NONE_STATED,
    FORMULA_STATUS_OK,
    KNOWHOW_OBJECT_ID_PREFIX,
    MEANING_CHARS_MAX,
    PAIR_CLAUSE_BREAKS,
    REJECT_DUPLICATE,
    REJECT_MEANING_NOT_IN_CONTEXT,
    REJECT_NEARER_OTHER_SYMBOL,
    REJECT_OVER_CAP,
    REJECT_SYMBOL_NOT_IN_CONTEXT,
    REJECT_SYMBOL_NOT_IN_FORMULA,
    REJECT_TOO_FAR,
    SYMBOL_CHARS_MAX,
    VARIABLES_MAX,
)
from app.domain.kg_concept_norm import norm_concept_name
from app.domain.kg_merge_seed import (
    _ACRONYM_TOKEN_RE,
    _PAREN_ACRONYM_RE,
    _is_initialism_of,
)

# Reply hint of the on-demand formula call: one example item, so the strict
# projection closes (tests/test_model_schema_hint_snapshots.py).
FORMULA_VARIABLES_HINT = '{"variables":[{"symbol":"","meaning":""}]}'

# --- aliases ----------------------------------------------------------------

# An acronym as R3 (the name) and R4 (the paren token) read it.
_ACRONYM_SHAPE_RE = re.compile(r"^[A-Z][A-Za-z0-9-]{1,7}$")
# Instance labels of one device/node (M1, Q5, C12) are not aliases.
_INSTANCE_LABEL_RE = re.compile(r"^[A-Z]{1,2}\d+$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_OPEN = r"\s*[(（]\s*"
_CLOSE = r"\s*[)）]"


def _name_pattern(name: str) -> str:
    """The name as a regex: escaped, any whitespace run matches any run."""
    return r"\s+".join(re.escape(part) for part in name.split())


def _readable(item: Mapping[str, Any]) -> bool:
    return bool(item.get("element_id")) and item.get("element_read") is True


def _r1(name: str) -> list[str]:
    match = _PAREN_ACRONYM_RE.match(name)
    if not match:
        return []
    head, token = match.group(1), match.group(2)
    if _ACRONYM_TOKEN_RE.match(token) and _is_initialism_of(token, head):
        return [token]
    return []


def _r2(name: str, text: str) -> list[str]:
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?i:" + _name_pattern(name) + r")"
        + _OPEN + r"([A-Za-z0-9]+)" + _CLOSE
    )
    return [m.group(1) for m in pattern.finditer(text) if _is_initialism_of(m.group(1), name)]


def _r3(name: str, text: str) -> list[str]:
    if not _ACRONYM_SHAPE_RE.match(name):
        return []
    words = len(re.sub(r"[^a-z0-9]+", "", name.lower()))
    full = r"((?:[A-Za-z0-9]+[\s\-_]+){%d}[A-Za-z0-9]+)" % (words - 1)
    pattern = re.compile(
        r"(?<![A-Za-z0-9])" + full + _OPEN + re.escape(name) + _CLOSE
    )
    return [m.group(1) for m in pattern.finditer(text) if _is_initialism_of(name, m.group(1))]


def _r4(name: str, text: str) -> list[str]:
    if not _CJK_RE.search(name):
        return []
    pattern = re.compile(_name_pattern(name) + _OPEN + r"([A-Za-z0-9-]+)" + _CLOSE)
    return [
        m.group(1) for m in pattern.finditer(text)
        if _ACRONYM_SHAPE_RE.match(m.group(1)) and not _INSTANCE_LABEL_RE.match(m.group(1))
    ]


_TEXT_RULES = {"R2": _r2, "R3": _r3, "R4": _r4}


def trace_concept_aliases(
    name: str,
    items: Sequence[Mapping[str, Any]],
    *,
    exclude_names: Iterable[str],
    rules: Iterable[str],
) -> list[dict]:
    """Every alias hit of ``rules``, each tagged with its ``rule``.

    Hits are de-duplicated within one rule (first occurrence wins) but NOT
    across rules, so Gate 0 can credit each rule separately. An alias equal
    (by ``norm_concept_name``) to the name or to any of ``exclude_names``, or
    longer than ``ALIAS_CHARS_MAX``, is not a hit. ``items`` is never mutated.
    """
    name = name or ""
    wanted = set(rules)
    excluded = {norm_concept_name(n) for n in (name, *exclude_names)}
    hits: list[dict] = []
    for rule in ALIAS_RULES:
        if rule not in wanted:
            continue
        seen: set[str] = set()

        def keep(text: str, element_id: str | None) -> None:
            key = norm_concept_name(text)
            if not key or key in excluded or key in seen or len(text) > ALIAS_CHARS_MAX:
                return
            seen.add(key)
            hit = {"rule": rule, "text": text}
            if element_id:
                hit["element_id"] = element_id
            hits.append(hit)

        if rule == "R1":
            anchor = next((str(i["element_id"]) for i in items if _readable(i)), None)
            for text in _r1(name):
                keep(text, anchor)
            continue
        for item in items:
            if not _readable(item):
                continue
            for text in _TEXT_RULES[rule](name, str(item.get("element_text") or "")):
                keep(text, str(item["element_id"]))
    return hits


def derive_concept_aliases(
    name: str,
    items: Sequence[Mapping[str, Any]],
    *,
    exclude_names: Iterable[str],
    rules: Iterable[str] = ENABLED_ALIAS_RULES,
) -> list[dict]:
    """``[{text, element_id?}]``: hits of the enabled rules, de-duplicated
    across rules in rule order and truncated (not dropped) to ``ALIASES_MAX``."""
    seen: set[str] = set()
    out: list[dict] = []
    for hit in trace_concept_aliases(name, items, exclude_names=exclude_names, rules=rules):
        key = norm_concept_name(hit["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append({k: v for k, v in hit.items() if k != "rule"})
        if len(out) == ALIASES_MAX:
            break
    return out


def attrs_eligible(object_id: str, object_type: str) -> bool:
    """attrs apply to concept/formula objects that are not Knowhow projections."""
    return object_type in ATTR_SPECS and not (object_id or "").startswith(KNOWHOW_OBJECT_ID_PREFIX)


# --- LaTeX normalization ----------------------------------------------------

# Commands whose single braced argument is kept as is (nested braces allowed).
_WRAPPERS = frozenset({"mathrm", "text", "mathit", "operatorname", "boldsymbol"})
# Two-argument fractions read as "a/b".
_FRACTIONS = frozenset({"frac", "dfrac", "tfrac"})
_COMMAND_RE = re.compile(r"\\([A-Za-z]+)")
_GREEK_NAMES = (
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
    "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
)
# U+03C2 (final sigma) and U+03A2 (unassigned) are skipped: 24 letters each.
_GREEK_LOWER = [chr(c) for c in range(0x3B1, 0x3CA) if c != 0x3C2]
_GREEK_UPPER = [chr(c) for c in range(0x391, 0x3AA) if c != 0x3A2]
_GREEK = {
    **dict(zip(_GREEK_NAMES, _GREEK_LOWER)),
    **{n.capitalize(): u for n, u in zip(_GREEK_NAMES, _GREEK_UPPER)},
}
_WS_RE = re.compile(r"\s+")


def _ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _squeeze_whitespace(text: str) -> str:
    """Drop whitespace, except one space between two ASCII letters/digits so
    word boundaries survive ("where a is" must not read as "whereais")."""
    def repl(m: re.Match) -> str:
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        return " " if _ascii_alnum(before) and _ascii_alnum(after) else ""
    return _WS_RE.sub(repl, text)


def _braced(text: str, at: int) -> tuple[str, int] | None:
    """The balanced ``{...}`` argument starting at ``at`` (spaces skipped):
    (content, index after the closing brace), or None."""
    while at < len(text) and text[at].isspace():
        at += 1
    if at >= len(text) or text[at] != "{":
        return None
    depth = 0
    for index in range(at, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[at + 1:index], index + 1
    return None


def _rewrite_commands(text: str) -> str:
    """Unwrap \\mathrm-style wrappers (nested braces allowed) and read
    \\frac{a}{b} as a/b; Greek commands become Unicode; every other
    \\command name is dropped whole (\\sqrt, \\cdot ...), its braced
    argument staying as plain content."""
    out: list[str] = []
    at = 0
    while at < len(text):
        match = _COMMAND_RE.match(text, at)
        if not match:
            out.append(text[at])
            at += 1
            continue
        name, end = match.group(1), match.end()
        if name in _WRAPPERS and (arg := _braced(text, end)) is not None:
            out.append(_rewrite_commands(arg[0]))
            at = arg[1]
        elif name in _FRACTIONS and (top := _braced(text, end)) is not None \
                and (bottom := _braced(text, top[1])) is not None:
            out.append(_rewrite_commands(top[0]) + "/" + _rewrite_commands(bottom[0]))
            at = bottom[1]
        elif name in _GREEK:
            out.append(_GREEK[name])
            at = end
        else:
            at = end
    return "".join(out)


def normalize_latex(text: str) -> str:
    """N(·) of spec §7.3, applied identically to the formula, the context,
    the symbol and the meaning: NFKC; drop the \\( \\) delimiters; unwrap
    \\mathrm/\\text/\\mathit/\\operatorname/\\boldsymbol (nested braces
    allowed); \\frac{a}{b} -> a/b; Greek commands -> Unicode; every other
    \\command name dropped whole; drop $ { } and stray backslashes; drop
    whitespace except one space between two ASCII letters/digits; NFKC +
    casefold."""
    out = unicodedata.normalize("NFKC", text or "")
    out = out.replace("\\(", "").replace("\\)", "")
    out = re.sub(r"\\[,;:! ]", "", out)  # spacing commands, not punctuation
    out = _rewrite_commands(out)
    out = re.sub(r"[${}\\]", "", out)
    out = _squeeze_whitespace(out)
    return unicodedata.normalize("NFKC", out).casefold()


# --- formula context and prompt --------------------------------------------

Segment = tuple[str, str]


def build_formula_context(
    first: Segment, following: Sequence[Segment], *, max_chars: int
) -> list[Segment]:
    """The evidence element, then the following element(s), as
    ``(element_id, text)`` segments truncated to ``max_chars`` in total."""
    out: list[Segment] = []
    budget = max_chars
    for element_id, text in (first, *following):
        if budget <= 0:
            break
        piece = (text or "")[:budget]
        budget -= len(piece)
        if piece.strip():
            out.append((element_id, piece))
    return out


def formula_variables_messages(formula: str, segments: Sequence[Segment]) -> list[dict]:
    context = "\n\n".join(text for _eid, text in segments)
    system = (
        "You read one formula and the source text around it, and report what "
        "the source text explicitly says its symbols mean. You never use "
        "outside knowledge."
    )
    user = (
        f"Formula (LaTeX):\n{formula}\n\n"
        f"Source text (the element containing the formula, then the element after it):\n"
        f"<<<\n{context}\n>>>\n\n"
        "List every symbol of the formula whose meaning the source text states "
        "explicitly, e.g. \"where g_m is the transconductance\" or "
        "\"其中 g_m 为跨导\".\n"
        "Rules:\n"
        "- symbol: the symbol as it is written in the formula.\n"
        "- meaning: copied verbatim from the source text, in its original "
        "language. Do not translate, paraphrase, shorten or add words.\n"
        "- Only meanings the text states. Never infer a meaning from general knowledge.\n"
        "- If the text states no meaning for any symbol, return an empty variables list.\n"
        "Return JSON only."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _reply_variables(raw: Any) -> list[dict] | None:
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("variables"), list):
        return None
    items = payload["variables"]
    for item in items:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("symbol"), str)
            and isinstance(item.get("meaning"), str)
        ):
            return None
    return items


def formula_variables_reply_ok(raw: Any) -> bool:
    """Shape only (the cache admission validator): a dict whose ``variables``
    is a list (empty allowed) of ``{symbol: str, meaning: str}`` dicts.
    Verification outcomes do not affect it, so "stated nothing" and "all
    rejected" replies are cached like any other well-formed reply."""
    return _reply_variables(raw) is not None


# --- verification -----------------------------------------------------------

@dataclass(frozen=True)
class FormulaVerification:
    kept: tuple[dict, ...]
    rejected: tuple[dict, ...]
    status: str


def _wordish(ch: str) -> bool:
    return _ascii_alnum(ch) or ch in ("_", "^")


def _symbol_spans(context: str, symbol: str) -> list[tuple[int, int]]:
    """Occurrences of ``symbol`` that stand on their own: not glued to an
    ASCII word or subscript on an ASCII edge ("a" inside "bias", "V" inside
    "V_DD"), and never the subscript of something else."""
    spans = []
    start = context.find(symbol)
    while start != -1:
        end = start + len(symbol)
        before = context[start - 1] if start else ""
        after = context[end] if end < len(context) else ""
        glued_before = before in ("_", "^") or (_ascii_alnum(symbol[0]) and _wordish(before))
        glued_after = after in ("_", "^") or (_ascii_alnum(symbol[-1]) and _wordish(after))
        if not glued_before and not glued_after:
            spans.append((start, end))
        start = context.find(symbol, start + 1)
    return spans


def _all_spans(context: str, needle: str) -> list[tuple[int, int]]:
    spans = []
    start = context.find(needle)
    while start != -1:
        spans.append((start, start + len(needle)))
        start = context.find(needle, start + 1)
    return spans


def _gap(context: str, a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    """(clause breaks, characters) strictly between two spans; 0 if they overlap."""
    lo, hi = (a[1], b[0]) if a[1] <= b[0] else (b[1], a[0])
    if lo >= hi:
        return (0, 0)
    between = context[lo:hi]
    return (sum(ch in PAIR_CLAUSE_BREAKS for ch in between), hi - lo)


def _segment_at(bounds: Sequence[tuple[int, str]], offset: int) -> str:
    element_id = bounds[0][1] if bounds else ""
    for start, eid in bounds:
        if start > offset:
            break
        element_id = eid
    return element_id


def verify_formula_variables(
    formula: str,
    segments: Sequence[Segment],
    reply: Any,
    *,
    pair_window: int,
) -> FormulaVerification:
    """Keep a reply item only when every check of spec §7.3 passes; each
    rejected item records the first failed check as ``reason``.

    1. N(symbol) is a substring of N(formula) and stands on its own in N(context);
    2. N(meaning) is a substring of N(context);
    3. some meaning occurrence is within ``pair_window`` normalized characters
       of a symbol occurrence, and no other reply symbol that passes check 1 is
       nearer to that meaning occurrence (fewer clause breaks first, then
       fewer characters);
    4. symbol/meaning within their character caps, first item per N(symbol)
       kept, at most ``VARIABLES_MAX`` kept.

    A kept item's ``element_id`` is the context segment its meaning falls in.
    """
    items = _reply_variables(reply)
    if items is None:
        return FormulaVerification((), (), FORMULA_STATUS_FAILED)
    norm_formula = normalize_latex(formula)
    bounds: list[tuple[int, str]] = []
    parts: list[str] = []
    offset = 0
    for element_id, text in segments:
        part = normalize_latex(text)
        bounds.append((offset, element_id))
        parts.append(part)
        offset += len(part)
    context = "".join(parts)
    symbols = [normalize_latex(i["symbol"]) for i in items]
    # Rivals for "nearest symbol": only reply symbols that pass checks 1-2
    # (in the formula, standing on their own in the context).
    contenders = {
        sym for sym in symbols
        if sym and sym in norm_formula and _symbol_spans(context, sym)
    }
    kept: list[dict] = []
    rejected: list[dict] = []
    kept_symbols: set[str] = set()
    for item, symbol in zip(items, symbols):
        raw_symbol, raw_meaning = item["symbol"].strip(), item["meaning"].strip()
        meaning = normalize_latex(raw_meaning)
        own = _symbol_spans(context, symbol) if symbol else []
        reason, element_id = None, ""
        if not symbol or symbol not in norm_formula:
            reason = REJECT_SYMBOL_NOT_IN_FORMULA
        elif not own:
            reason = REJECT_SYMBOL_NOT_IN_CONTEXT
        elif not meaning or meaning not in context:
            reason = REJECT_MEANING_NOT_IN_CONTEXT
        else:
            pairs = [
                (_gap(context, s, m), m)
                for m in _all_spans(context, meaning) for s in own
            ]
            pairs = [(key, m) for key, m in pairs if key[1] <= pair_window]
            if not pairs:
                reason = REJECT_TOO_FAR
            else:
                own_key, spot = min(pairs, key=lambda p: (p[0], p[1]))
                rivals = [
                    _gap(context, s, spot)
                    for other in contenders - {symbol}
                    for s in _symbol_spans(context, other)
                ]
                if rivals and min(rivals) < own_key:
                    reason = REJECT_NEARER_OTHER_SYMBOL
                elif len(raw_symbol) > SYMBOL_CHARS_MAX or len(raw_meaning) > MEANING_CHARS_MAX:
                    reason = REJECT_OVER_CAP
                elif symbol in kept_symbols:
                    reason = REJECT_DUPLICATE
                elif len(kept) >= VARIABLES_MAX:
                    reason = REJECT_OVER_CAP
                else:
                    element_id = _segment_at(bounds, spot[0])
        if reason is None:
            kept_symbols.add(symbol)
            entry = {"symbol": raw_symbol, "meaning": raw_meaning}
            if element_id:
                entry["element_id"] = element_id
            kept.append(entry)
        else:
            rejected.append({"symbol": raw_symbol, "meaning": raw_meaning, "reason": reason})
    if kept:
        status = FORMULA_STATUS_OK
    elif not items:
        status = FORMULA_STATUS_NONE_STATED
    else:
        status = FORMULA_STATUS_NONE_GROUNDED
    return FormulaVerification(tuple(kept), tuple(rejected), status)


# --- drift signature --------------------------------------------------------

def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _assigned_names(node: ast.stmt) -> set[str]:
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    return set()


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# What rules_signature() covers outside this module and kg_attrs_contracts
# (dumped whole). tests/test_kg_attrs_domain.py pins that every name this
# module imports from another app.domain module is listed here.
SIGNATURE_DEPENDENCIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("kg_merge_seed", ("_PAREN_ACRONYM_RE", "_ACRONYM_TOKEN_RE", "_is_initialism_of")),
    ("kg_concept_norm", ("norm_concept_name",)),
)


def rules_signature() -> str:
    """sha256 over the normalized AST of everything a Gate 0 result depends on.

    Covers ``kg_attrs.py`` and ``kg_attrs_contracts.py`` (minus docstrings and
    minus the ``ENABLED_ALIAS_RULES`` assignment, which only selects rules),
    plus the reused ``kg_merge_seed`` matchers and ``norm_concept_name``. Any
    change to a rule, the verification, the prompt or a bound changes it;
    comments, docstrings and the enabled-rule set do not.
    """
    from app.domain import kg_concept_norm, kg_merge_seed

    dumps: list[str] = []
    own = _strip_docstrings(_module_tree(Path(__file__)))
    dumps.append(ast.dump(own, include_attributes=False))
    contract_tree = _strip_docstrings(_module_tree(Path(contracts.__file__)))
    contract_tree.body = [
        node for node in contract_tree.body
        if "ENABLED_ALIAS_RULES" not in _assigned_names(node)
    ]
    dumps.append(ast.dump(contract_tree, include_attributes=False))
    modules = {"kg_merge_seed": kg_merge_seed, "kg_concept_norm": kg_concept_norm}
    for module_name, names in SIGNATURE_DEPENDENCIES:
        module = modules[module_name]
        tree = _strip_docstrings(_module_tree(Path(module.__file__)))
        for name in names:
            node = next(n for n in tree.body if name in _assigned_names(n))
            dumps.append(ast.dump(node, include_attributes=False))
    return hashlib.sha256("\n".join(dumps).encode("utf-8")).hexdigest()
