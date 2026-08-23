"""Pure KG-merge seed normalizers (sunk from app.services.kg_merge in B3).

seed_concept / seed_claim / seed_formula / seed_procedure are consumed
directly by app.repositories (governance_store, both backends) via
``seed_fn_for`` to pick the right seed function for an object type. This
module also carries their full private helper cluster (acronym
stripping/aliasing, statement/formula normalizers) because those helpers are
NOT solely used by the four seed functions — the rest of
app.services.kg_merge's clustering algorithm (``seed_or_unique``,
``build_acronym_alias_map``, and the pairwise-merge core) uses the SAME
normalizers, and duplicating them would let the two copies drift. Moving the
whole cluster here (rather than re-implementing a second copy in
app.services.kg_merge) keeps normalization a single source of truth;
``app.services.kg_merge`` imports these same names back for its own
internal use and re-exports the four seed functions unchanged.

Pure, zero app.services/app.repositories dependency (only ``re`` and
``unicodedata``).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable


_ALIASES = {
    "vco": "voltage controlled oscillator",
    "pll": "phase locked loop",
    "lna": "low noise amplifier",
    "mos": "mos transistor",
    "mosfet": "mos transistor",
    "bjt": "bipolar junction transistor",
    "opamp": "op amp",
    "op amp": "op amp",
}


# "Full (ACR)" shape: capture the part before the paren and the paren token.
_PAREN_ACRONYM_RE = re.compile(r"^(.*\S)\s*\(([^)]+)\)\s*$")
# Cheap sanity guard: paren token is a single run of letters/digits (no spaces).
_ACRONYM_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+$")


def _is_initialism_of(acr: str, full: str) -> bool:
    """True iff ``acr`` is the initialism of ``full`` — the concatenated first
    characters of full's words (split on space/-/_) equal acr's letters/digits.
    This is what separates a genuine abbreviation ("CSA" of "Compressed Sparse
    Attention", "MoE" of "Mixture-of-Experts") from a qualifier/discriminator
    ("LP" of "Filter", "n" of "Channel", "v3" of "Type") that merely happens to
    be short or upper-case. Only initialisms are safe to strip/alias."""
    initials = "".join(w[0] for w in re.split(r"[\s\-_]+", full or "") if w).lower()
    acr_clean = re.sub(r"[^a-z0-9]+", "", (acr or "").lower())
    return bool(initials) and initials == acr_clean


def _strip_paren_acronym(name: str) -> str:
    """If ``name`` is "Full (ACR)" where ACR is the initialism of Full, return
    "Full" (the part before the paren). Otherwise return ``name`` unchanged.
    Qualifier parens (LP/HP/n/p/v3) are NOT initialisms of the head → left
    intact, so e.g. "Filter (LP)" and "Filter (HP)" stay distinct."""
    m = _PAREN_ACRONYM_RE.match(name or "")
    if not m:
        return name
    head, tok = m.group(1), m.group(2)
    if _ACRONYM_TOKEN_RE.match(tok) and _is_initialism_of(tok, head):
        return head
    return name


def _norm(name: str) -> str:
    # NFKC 先行:中文语料常见全角拉丁/数字/括号(（）ＡＢＣ１２３)折到 ASCII,
    # 让 acronym 剥离与 _ALIASES 能看见;纯 ASCII 输入是恒等变换。
    folded = unicodedata.normalize("NFKC", name or "")
    stripped = _strip_paren_acronym(folded)
    # Unicode \w 保留 CJK/希腊/带音标字母 —— 旧 [^a-z0-9+/ ] 把纯中文名清成空
    # seed,全库此类实体确定性塌缩进同一个 "K-" 簇(实测中文库 54% concept)。
    # 纯 ASCII 名输出与旧类逐字节相同(下划线两版都归并为空格)。
    cleaned = re.sub(r"[^\w+/ ]+", " ", stripped.strip().lower())
    cleaned = re.sub(r"[\s\-_]+", " ", cleaned).strip()
    return _ALIASES.get(cleaned, cleaned)


def _norm_statement(s: str) -> str:
    """Claim/step text normalizer: lowercase, drop punctuation, collapse whitespace."""
    cleaned = re.sub(r"[^\w\s]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _norm_formula(s: str) -> str:
    """Formula normalizer: drop ALL whitespace, lowercase (expression identity)."""
    return re.sub(r"\s+", "", (s or "").lower())


def _steps_signature(payload: dict) -> str:
    steps = (payload or {}).get("steps")
    if not isinstance(steps, list):
        return ""
    names = sorted(_norm_statement(st.get("name", ""))
                   for st in steps if isinstance(st, dict) and st.get("name"))
    return "|".join(n for n in names if n)


def seed_concept(obj) -> str:
    """Seed function for concepts. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm(obj)
    return _norm(obj.get("name", ""))


def seed_claim(obj) -> str:
    """Seed function for claims. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_statement(obj)
    return _norm_statement(obj.get("name", ""))


def seed_formula(obj) -> str:
    """Seed function for formulas. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_formula(obj)
    return _norm_formula(obj.get("name", ""))


def seed_procedure(obj) -> str:
    """Seed function for procedures. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_statement(obj)
    nm = _norm_statement(obj.get("name", ""))
    sig = _steps_signature(obj.get("payload") or {})
    return f"{nm}#{sig}" if sig else nm
