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
# Full rebuilds seed concepts in space 2 (``CONCEPT_SEED_VERSION`` below);
# the alias table, ``_seed_with_alias``, ``bridge_canonical_id``, the
# unified-rebuild seed stream, catch-up and incremental fusion are all
# parameterised by seed version (``app.services.kg_merge.ConceptSeedSpace``),
# and a library keeps placing new objects in the space it PUBLISHED until its
# next full rebuild. The findings rules use both (F4 narrow / wide split, S1
# "needs development" count).
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


def _seed_base(name: str) -> str:
    """NFKC → paren-acronym strip → strip → lower: the start shared by v1 and v2."""
    return _strip_paren_acronym(unicodedata.normalize("NFKC", name or "")).strip().lower()


def _v1_unaliased_from_base(base: str) -> str:
    cleaned = _NON_SEED_CHARS_RE.sub(" ", base)
    return _ORIGINAL_SEPARATORS_RE.sub(" ", cleaned).strip()


def _concept_seed_v1_unaliased(name: str) -> str:
    """The v1 seed BEFORE the alias table: NFKC → paren-acronym strip → strip/lower
    → every non-``[\\w+/ ]`` char to a space → ``[\\s\\-_]+`` runs to one space.

    It is the exact intermediate of ``_norm`` (``_norm`` is this followed by the
    ``_ALIASES`` lookup); ``test_v1_unaliased_form_is_norm_before_aliases`` pins
    the two equal, because ``_norm`` itself is deliberately left untouched. v2
    looks its aliases up on THIS form, so an alias key keeps matching the words
    it was written for ("op amp", "mos transistor")."""
    return _v1_unaliased_from_base(_seed_base(name))


# The alias keys with their spaces removed. Deleting every space from the v1
# form leaves the base minus ``[\s\-_]`` minus every non-``[\w+/ ]`` char — and
# so does deleting every space from v2's own un-aliased text. A v1 form can
# therefore only BE an alias key when the space-free v2 text is one of these, so
# v2 builds the v1 form (two more regex passes) only for those few names. It runs
# once per concept canonical name in the findings pass (millions in production).
_ALIAS_KEYS_SQUASHED = frozenset(key.replace(" ", "") for key in _ALIASES)


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
    base = _seed_base(name)
    text = _v2_text_before_aliases(base)
    if text.replace(" ", "") in _ALIAS_KEYS_SQUASHED:
        alias = _ALIASES.get(_v1_unaliased_from_base(base))
        if alias is not None:
            return alias.replace(" ", "")
    return text


def concept_squash_key(name: str) -> str:
    """The squash key of a concept name: the v2 seed with its spaces removed.

    Coarser than the v2 seed (it also erases the punctuation-made spaces, so
    "Llama 3.1" and "Llama 31" share ``llama31``) but it still keeps ``+`` and
    ``/`` ("C++" ≠ "C", "I/O" ≠ "IO"). It is the F4 subject key and the one
    function F4 and the squash candidate tier of the pending refresh
    (``kg_merge.squash_pending_candidates``, by ``seed_squash_key``) group by:

      · v1 seeds differ, v2 seeds equal  → the **narrow** half: a seed defect S1
        fixes automatically ("Q-Former" / "Qformer", "RMS Norm" / "RMSNorm");
      · v2 seeds differ, squash keys equal → the **wide** half: F4 proper, left
        to a human because a dot or bracket may carry meaning
        ("Llama 3.1" / "Llama 31").
    """
    return concept_seed_v2(name).replace(" ", "")


# ---------------------------------------------------------------------------
# The concept seed SPACE a notebook publishes (PR-4, design §五 S1 「实施细化」).
#
# ``CONCEPT_SEED_VERSION`` is the space the running code builds full merges in.
# Every notebook records the space of its PUBLISHED merge in
# ``unified_kg_state.concept_seed_version`` (written by the flip statement that
# publishes a full rebuild; 1 for every merge built before this column, and
# after a graph delete / index publication that leaves nothing published).
# Incremental fusion, catch-up, bridge ids, duplicate blocking and promotion
# dedup follow the PUBLISHED space, so a library that has not been rebuilt yet
# keeps placing "Q-Former" in ``K-q former``; the next full rebuild switches it.
# Claim / formula / procedure seeds have no version: they are unchanged.
# ---------------------------------------------------------------------------

# ⚠ ``unified_kg_state.concept_seed_version``'s column DEFAULT is this value
# (SQLite v100 / PostgreSQL 0081): a state row created from now on starts in
# the space the code builds in. A future space 3 needs a migration that moves
# the default as well.
CONCEPT_SEED_VERSION = 2
CONCEPT_SEED_VERSIONS = (1, 2)
# A v2 decision row whose seed starts with this prefix keeps the OLD v1 seed of
# the objects it names (the collision exception of the decision recompute,
# ``kg_merge.translate_v1_decisions``): ``"~v1:" + <v1 seed>``. ``~`` never
# survives either normalizer, so no real name can produce it.
OVERRIDE_SEED_PREFIX = "~v1:"


def check_concept_seed_version(version: object) -> int:
    """The version as an int, or ``ValueError`` — never a silent default."""
    if isinstance(version, bool) or not isinstance(version, int) \
            or version not in CONCEPT_SEED_VERSIONS:
        raise ValueError(f"unknown concept seed version: {version!r}")
    return version


def concept_seed_of(name: str, version: int) -> str:
    """The concept seed of ``name`` in seed space ``version`` (static aliases
    applied, the per-batch acronym alias map NOT — see
    ``kg_merge.ConceptSeedSpace`` for the full seed)."""
    if check_concept_seed_version(version) == 1:
        return concept_seed_v1(name)
    return concept_seed_v2(name)


def seed_squash_key(seed: str) -> str:
    """The squash key of a v2 SEED (its spaces removed) — equal to
    ``concept_squash_key(name)`` for every name whose v2 seed it is. The
    pending refresh groups seeds by it; seeds starting with ``~`` (the per-object
    sentinel of an empty name, the ``~v1:`` override) have none (``""``)."""
    if not seed or seed.startswith("~"):
        return ""
    return seed.replace(" ", "")


def _v2_text_before_aliases(base: str) -> str:
    """v2's steps 3–4 on a ``_seed_base`` result, before any alias lookup."""
    text = _ORIGINAL_SEPARATORS_RE.sub("", base)
    text = _NON_SEED_CHARS_RE.sub(" ", text)
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()


def concept_seed_parts(name: str) -> "tuple[str, str]":
    """``(v2 seed, squashed pre-alias text)`` in one normalisation pass.

    The second value — the name with every char outside ``[\\w+/]`` and every
    ``_`` removed — is ALSO what deleting the spaces from the v1 form before the
    static aliases gives (``_ALIAS_KEYS_SQUASHED``' argument). It lets a caller
    decide cheaply whether the v1 seed can be one of a few keys
    (``kg_merge.ConceptSeedSpace``) before paying for it."""
    base = _seed_base(name)
    text = _v2_text_before_aliases(base)
    squashed = text.replace(" ", "")
    if squashed in _ALIAS_KEYS_SQUASHED:
        alias = _ALIASES.get(_v1_unaliased_from_base(base))
        if alias is not None:
            return alias.replace(" ", ""), squashed
    return text, squashed


def concept_decision_seeds(name: str) -> "frozenset[str]":
    """Every seed a stored merge decision may name a member called ``name`` by:
    its v1 seed (rows of seed version 1, and the canonical-id fallback of rows
    written in the v1 space) and its v2 seed. An override seed
    ``~v1:<v1 seed>`` (the collision exception) names the member by its v1 seed
    once ``decision_match_key`` strips the prefix. The findings rules match
    decisions by these, so one matching works before a library's first v2
    rebuild, after it, and across a recompute interrupted between the pending
    refresh and the flip. Empty seeds are left out (a decision never names a
    member by the per-object sentinel through its name)."""
    return frozenset(s for s in (concept_seed_v1(name), concept_seed_v2(name)) if s)


def decision_match_key(seed: str) -> str:
    """A stored decision key as the findings rules match it against member
    seeds: an override seed ``~v1:<v1 seed>`` names exactly the members whose
    v1 seed it carries, so the prefix is dropped; any other key as is."""
    if seed.startswith(OVERRIDE_SEED_PREFIX):
        return seed[len(OVERRIDE_SEED_PREFIX):]
    return seed


def seed_concept_v2(obj) -> str:
    """``seed_concept`` in seed space 2. obj may be a dict or a str."""
    if isinstance(obj, str):
        return concept_seed_v2(obj)
    return concept_seed_v2(obj.get("name", ""))


def concept_seed_in_space(name: str, version: int, override=frozenset()) -> str:
    """The concept seed of ``name`` in seed space ``version`` with the
    notebook's override keys (v1 seeds a decision recompute kept as
    ``~v1:<seed>``), WITHOUT the per-batch acronym redirect — the shape the
    object-level dedups compare (promotion into a public library), matched on
    the un-aliased v1 seed like the bridge ids."""
    if check_concept_seed_version(version) == 2 and override:
        v1 = concept_seed_v1(name)
        if v1 and v1 in override:
            return OVERRIDE_SEED_PREFIX + v1
    return concept_seed_of(name, version)


def seed_fn_for_type(object_type: str, *, concept_seed_version: int,
                     override=frozenset()):
    """The seed function of a KG object type, concepts in seed space
    ``concept_seed_version`` (the notebook's published space) with its
    ``override`` keys; claim / formula / procedure seeds have no version.
    Unknown types fall back to ``seed_claim`` (the historical default of the
    stores' ``seed_fn_for``)."""
    version = check_concept_seed_version(concept_seed_version)
    if object_type == "concept":
        if version == 1:
            return seed_concept
        if override:
            keys = frozenset(override)
            return lambda obj: concept_seed_in_space(
                obj if isinstance(obj, str) else obj.get("name", ""), 2, keys)
        return seed_concept_v2
    return {
        "claim": seed_claim,
        "formula": seed_formula,
        "procedure": seed_procedure,
    }.get(object_type, seed_claim)


class ConceptSeedSpaceMoved(RuntimeError):
    """An incremental placement computed in one concept seed space met another
    one under the cluster write lock: the library's full rebuild published a
    new space in between (S1). Nothing was written; the writer places again
    in the space now published."""

    def __init__(self, notebook_id: str, placed_in: int, published: int):
        super().__init__(f"concept seed space moved from {placed_in} to {published}")
        self.notebook_id = notebook_id
        self.placed_in = placed_in
        self.published = published


def published_concept_seed_version(state_row) -> int:
    """The concept seed space of a library's published merge, read off its
    ``unified_kg_state`` row. ``None`` — no row: a notebook that never wrote a
    graph, a deep copy (which carries its source's published clusters) — reads
    as ``CONCEPT_SEED_VERSION``, the column's default for every row created
    after the upgrade; only the rows that existed before it hold 1."""
    if state_row is None:
        return CONCEPT_SEED_VERSION
    return check_concept_seed_version(int(state_row["concept_seed_version"]))


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
