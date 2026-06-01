"""LLM-as-segmenter atomization (opt-in, QIEFEN_LLM_ATOMIZE).

The LLM decides atom boundaries over large CONTIGUOUS source windows (so
segmentation granularity is the model's, matching gold's hand curation), rather
than a selector over deterministic sentence splits. Formula/table/figure atoms
stay deterministic and are ALL kept; each formula records its role (context).

Hyperparameters (env, tunable):
  QIEFEN_WINDOW_CHARS   N   window size in chars            (default 9000)
  QIEFEN_WINDOW_OVERLAP M   overlap between windows         (default 5% of N)
  QIEFEN_ATOM_CAP           per-window atom cap; 0 = let the model decide (default 0)

Span invariant: source_text[char_start:char_end] == raw_text. The model returns
each atom as a VERBATIM quote; we locate it in the window (exact, then
whitespace-flexible) and keep the SOURCE substring + offsets. Ungroundable quotes
are dropped. Overlapping windows are de-duplicated by span.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Tuple

from app.services.qiefen.atomizer import _normalize
from app.services.qiefen.models import EvidenceAtom, SourceElementQ, SourceSpan

WINDOW_CHARS = int(os.environ.get("QIEFEN_WINDOW_CHARS", "9000"))
WINDOW_OVERLAP = int(os.environ.get("QIEFEN_WINDOW_OVERLAP", str(WINDOW_CHARS // 20)))
ATOM_CAP = int(os.environ.get("QIEFEN_ATOM_CAP", "0"))  # 0 = model decides

_ARTICLE_TYPES = ("claim_sentence", "method_sentence", "result_sentence",
                  "scaling_law_result_atom", "mechanism_sentence",
                  "experiment_setup_atom", "risk_sentence", "limitation_sentence")
_TEXTBOOK_TYPES = ("concept_definition_atom", "definition_atom",
                   "design_principle_atom", "design_rule_atom",
                   "process_flow_atom", "process_step_atom", "physical_effect_atom",
                   "example_problem_atom", "problem_statement_atom",
                   "given_atom", "result_atom", "formula_usage_atom",
                   "component_model_atom")
_SCHEMA = '{"atoms":[{"text":"<verbatim quote>","type":"<one type>"}]}'

# Mermaid / flow-diagram syntax that leaks in as prose text.
_MERMAID = re.compile(r'-->|\w+\s*\[".*?"\]|\bgraph\s+(TD|LR)\b|\|\s*\w+\s*\|')


def _types(profile: str):
    return _ARTICLE_TYPES if profile == "article_research" else _TEXTBOOK_TYPES


def _is_noise(text: str) -> bool:
    t = text.strip()
    return len(t) < 4 or bool(_MERMAID.search(t))


def _empty_formula(text: str) -> bool:
    body = text.replace("$", "").replace("\\[", "").replace("\\]", "").strip()
    return len(body) < 2


def _prompt(window_text: str, profile: str) -> str:
    kind = "research paper" if profile == "article_research" else "textbook"
    types = ", ".join(_types(profile))
    cap_line = (f"Return AT MOST {ATOM_CAP} atoms for this passage. "
                if ATOM_CAP > 0 else
                "Decide yourself how many — but most prose sentences yield NONE. ")
    return f"""Below is a passage from a {kind}. EXTRACT only the HIGH-VALUE
knowledge atoms — the standalone statements worth citing (a definition, a
formula's meaning, a quantitative result, a design principle/rule, a physical
effect, a named process step, an example/problem statement, or a core
claim/method/mechanism). This passage is mostly ordinary prose: SKIP narrative,
motivation, background, transitions, restatements, and elaboration. {cap_line}
When one sentence states two distinct facts (e.g. two metrics), you may split it
into two atoms at the clause boundary.

For each kept atom return:
- "text": an EXACT verbatim substring copied from the passage (character for
  character; do NOT paraphrase, re-order, or fix anything). Quotes must appear
  in the passage IN ORDER and must NOT overlap each other.
- "type": one of: {types}

Passage:
\"\"\"{window_text}\"\"\"

Return JSON ONLY: {_SCHEMA}
"""


def _locate(window: str, quote: str, cursor: int) -> Optional[Tuple[int, str]]:
    """Forward-only locate of quote in window at/after cursor; returns
    (start, source_substring) or None. Returns the REAL source substring so the
    span invariant holds."""
    if not quote or len(quote.strip()) < 4:
        return None
    i = window.find(quote, cursor)
    if i >= 0:
        return i, quote
    pat = r"\s+".join(re.escape(tok) for tok in quote.split())
    m = re.search(pat, window[cursor:])
    if m:
        return cursor + m.start(), m.group(0)
    return None


def _atomize_window(source_text: str, win_start: int, win_end: int, profile: str,
                    client: Any) -> List[Tuple[int, str, str]]:
    from app.services.qiefen.llm_extract import safe_json
    window = source_text[win_start:win_end]
    try:
        raw = client.chat_json([{"role": "user", "content": _prompt(window, profile)}], _SCHEMA)
    except Exception:
        return []
    items = safe_json(raw).get("atoms")
    if not isinstance(items, list):
        return []
    allowed = set(_types(profile))
    default = "claim_sentence" if profile == "article_research" else "concept_definition_atom"
    out: List[Tuple[int, str, str]] = []
    cursor = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        loc = _locate(window, str(it.get("text", "")), cursor)
        if loc is None:
            continue
        local, matched = loc
        cursor = local + len(matched)
        if _is_noise(matched):
            continue
        atype = it.get("type")
        out.append((win_start + local, matched, atype if atype in allowed else default))
    return out


def _section_windows(prose: List[SourceElementQ]) -> List[Tuple[int, int]]:
    """Contiguous N-char windows (with M overlap) over the section's prose span."""
    if not prose:
        return []
    start = min(e.char_start for e in prose)
    end = max(e.char_end for e in prose)
    step = max(1, WINDOW_CHARS - WINDOW_OVERLAP)
    windows = []
    s = start
    while s < end:
        windows.append((s, min(s + WINDOW_CHARS, end)))
        if s + WINDOW_CHARS >= end:
            break
        s += step
    return windows


def llm_atomize(source_text: str, elements: List[SourceElementQ], section_id: str,
                profile: str, client: Any, workers: int = 8) -> List[EvidenceAtom]:
    from app.services.qiefen import atomizer as det

    atoms: List[EvidenceAtom] = []
    n = 0

    def add(cstart: int, raw: str, atom_type: str, role: str = "") -> None:
        nonlocal n
        if not raw.strip():
            return
        cend = cstart + len(raw)
        assert source_text[cstart:cend] == raw, "span/raw mismatch"
        n += 1
        meta = {"role": role} if role else {}
        atoms.append(EvidenceAtom(
            id=f"A-{section_id}-{n}", section_id=section_id, atom_type=atom_type,
            source_element_id="", source_span=SourceSpan(
                file=elements[0].file if elements else "",
                line_start=source_text.count("\n", 0, cstart) + 1,
                line_end=source_text.count("\n", 0, cend) + 1,
                char_start=cstart, char_end=cend),
            raw_text=raw, normalized_text=_normalize(raw), metadata=meta))

    # Structural atoms: keep ALL formulas/tables/figures (deterministic, exact).
    # Drop empty formulas; record each formula's role = nearest preceding prose.
    structural = [e for e in elements if e.type in ("formula", "figure_caption", "table")]
    prose = [e for e in elements if e.type in ("paragraph", "list_item")]
    prose_sorted = sorted(prose, key=lambda e: e.char_start)
    for sa in det.atomize(source_text, structural, section_id, profile):
        if sa.atom_type == "formula_atom":
            if _empty_formula(sa.raw_text):
                continue
            prev = [e for e in prose_sorted if e.char_end <= sa.source_span.char_start]
            role = (prev[-1].text[:160] if prev else "")
            sa.metadata = dict(sa.metadata or {}, role=role)
        atoms.append(sa)

    # Prose: contiguous N-char windows (M overlap) -> LLM segmentation.
    windows = _section_windows(prose)
    if windows and client is not None and getattr(client, "configured", False):
        try:
            client.client()
        except Exception:
            pass
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(windows)))) as pool:
            results = list(pool.map(
                lambda w: _atomize_window(source_text, w[0], w[1], profile, client),
                windows))
        for win_atoms in results:
            for cstart, raw, atype in win_atoms:
                add(cstart, raw, atype)

    # De-duplicate by span (overlapping windows repeat atoms): keep first, drop
    # any atom whose span overlaps an already-kept atom by >50%.
    atoms.sort(key=lambda a: (a.source_span.char_start, a.source_span.char_end))
    kept: List[EvidenceAtom] = []
    for a in atoms:
        s, e = a.source_span.char_start, a.source_span.char_end
        dup = False
        for k in kept:
            ks, ke = k.source_span.char_start, k.source_span.char_end
            inter = max(0, min(e, ke) - max(s, ks))
            if inter > 0.5 * min(e - s, ke - ks):
                dup = True
                break
        if not dup:
            kept.append(a)
    for i, a in enumerate(kept, start=1):
        a.id = f"A-{section_id}-{i}"
    return kept
