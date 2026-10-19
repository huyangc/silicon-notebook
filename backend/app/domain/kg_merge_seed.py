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


# ---------------------------------------------------------------------------
# Concept seed spaces v1 / v2 and the squash key (KG-analysis findings design
# 2026-10-19, §4.4 + §五 S1 「实施细化」).
#
# These are PURE functions added ahead of the seed switch. Nothing calls v2 to
# build clusters yet: the switch (alias table, ``_seed_with_alias``,
# ``bridge_canonical_id``, the unified-rebuild seed stream, catch-up and
# incremental fusion, all parameterised by seed version) lands with PR-4 of
# that plan. Today they feed only the findings rules (F4 narrow / wide split,
# S1 "needs development" count).
#
# ⚠ ``_norm`` is deliberately NOT changed. ``kg_merge._discriminative_conflict``
# tokenises ``_norm`` output on spaces to detect "only one contrast word
# differs" (low/high, first/second …); squashing spaces inside ``_norm`` would
# turn every multi-word name into a single token and that guard would go silently
# blind. v2 lives beside it instead.
# ---------------------------------------------------------------------------

# Original-text separators that v2 removes (whitespace, hyphen, underscore) —
# BEFORE punctuation is turned into spaces, so they never become a token
# boundary. Spaces that punctuation (``.``, ``(``, ``·`` …) turns into survive.
_ORIGINAL_SEPARATORS_RE = re.compile(r"[\s\-_]+")
_NON_SEED_CHARS_RE = re.compile(r"[^\w+/ ]+")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _concept_seed_v1_unaliased(name: str) -> str:
    """The v1 seed BEFORE the alias table: NFKC → paren-acronym strip → strip/lower
    → every non-``[\\w+/ ]`` char to a space → ``[\\s\\-_]+`` runs to one space.

    It is the exact intermediate of ``_norm`` (``_norm`` is this followed by the
    ``_ALIASES`` lookup); ``test_v1_unaliased_form_is_norm_before_aliases`` pins
    the two equal, because ``_norm`` itself is deliberately left untouched. v2
    looks its aliases up on THIS form, so an alias key keeps matching the words
    it was written for ("op amp", "mos transistor")."""
    folded = unicodedata.normalize("NFKC", name or "")
    stripped = _strip_paren_acronym(folded)
    cleaned = _NON_SEED_CHARS_RE.sub(" ", stripped.strip().lower())
    return _ORIGINAL_SEPARATORS_RE.sub(" ", cleaned).strip()


def concept_seed_v1(name: str) -> str:
    """FROZEN v1 concept seed — byte-for-byte today's ``seed_concept`` / ``_norm``.

    "Frozen" is a contract, pinned by golden values in
    ``tests/test_kg_merge_seed_versions.py``: when PR-4 parameterises the seed
    machinery by version, v1 must keep producing exactly these strings, because
    every stored merge decision (``concept_merge_candidates.seed_a/seed_b``)
    and every published ``K-`` canonical id was minted in this space."""
    return _norm(name)


def concept_seed_v2(name: str) -> str:
    """v2 concept seed (S1): drop ORIGINAL whitespace / ``-`` / ``_`` first, then
    punctuation → space, then collapse.

    Steps, in this order (the order is the whole point):

      1. NFKC, paren-acronym strip ("Mixture-of-Experts (MoE)" → head), strip,
         lower — identical to v1.
      2. Alias lookup on the **v1 form** (``_concept_seed_v1_unaliased``); a hit
         returns the alias VALUE with its spaces removed ("op-amp" → "opamp").
      3. Delete every original ``[\\s\\-_]`` run — "Q-Former", "Qformer" and
         "Q Former" all become "qformer".
      4. Every remaining non-``[\\w+/ ]`` char becomes a space, runs collapse:
         "llama3.1" → "llama3 1". The space a dot or bracket leaves behind is a
         token boundary v2 keeps on purpose.

    Consequence (design §4.4): "Llama 3.1" → ``llama3 1`` and "Llama 31" →
    ``llama31`` stay TWO seeds — that difference is left to a human (F4) —
    while "Llama-3-1" → ``llama31`` joins "Llama 31". Doing step 3 after step 4
    would erase the dot-made space too and merge "Llama 3.1" with "Llama 31";
    ``test_v2_drops_original_separators_before_punctuation`` pins the order.
    """
    unaliased = _concept_seed_v1_unaliased(name)
    alias = _ALIASES.get(unaliased)
    if alias is not None:
        return alias.replace(" ", "")
    folded = unicodedata.normalize("NFKC", name or "")
    text = _strip_paren_acronym(folded).strip().lower()
    text = _ORIGINAL_SEPARATORS_RE.sub("", text)
    text = _NON_SEED_CHARS_RE.sub(" ", text)
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()


def concept_squash_key(name: str) -> str:
    """The squash key of a concept name: the v2 seed with its spaces removed.

    Coarser than the v2 seed (it also erases the punctuation-made spaces, so
    "Llama 3.1" and "Llama 31" share ``llama31``) but it still keeps ``+`` and
    ``/`` ("C++" ≠ "C", "I/O" ≠ "IO"). It is the F4 subject key and the one
    function F4 (and later PR-4's squash candidate tier) groups by:

      · v1 seeds differ, v2 seeds equal  → the **narrow** half: a seed defect S1
        fixes automatically ("Q-Former" / "Qformer", "RMS Norm" / "RMSNorm");
      · v2 seeds differ, squash keys equal → the **wide** half: F4 proper, left
        to a human because a dot or bracket may carry meaning
        ("Llama 3.1" / "Llama 31").
    """
    return concept_seed_v2(name).replace(" ", "")


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
