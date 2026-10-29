"""Named contracts of the read-time KG node attrs (spec 2026-10-28 §五, §7.2, §7.3).

Zero-storage design: Concept ``aliases`` are derived at read time and Formula
``variables`` are identified on demand; nothing here is persisted. Every bound
a response may carry is a named constant so the API docs, the derivation code
and the tests read one value.

``ENABLED_ALIAS_RULES`` is written by the Gate 0 decision PR (PR-G0b) and is
deliberately excluded from ``kg_attrs.rules_signature()``: flipping which rules
are live must not invalidate the calibration fixture, while changing what a
rule matches must.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

# --- response bounds (spec §五) ---------------------------------------------
ALIASES_MAX = 8
ALIAS_CHARS_MAX = 80
VARIABLES_MAX = 24
SYMBOL_CHARS_MAX = 32
MEANING_CHARS_MAX = 200

# --- alias rules (spec §7.2) -----------------------------------------------
ALIAS_RULES: tuple[str, ...] = ("R1", "R2", "R3", "R4")
# Rules that passed Gate 0. Empty until PR-G0b records the decision.
ENABLED_ALIAS_RULES: frozenset[str] = frozenset()

# --- formula context (spec §7.3) -------------------------------------------
# The "next element" of a formula's evidence element is the first element of
# the same source whose ordinal is at most this far after it.
FORMULA_NEXT_ELEMENT_ORDINAL_WINDOW = 64

# Characters that end a clause inside the normalized context. A symbol on the
# far side of one of these is farther from a meaning than any symbol on the
# near side, whatever the character counts ("where a is X, b is Y": X belongs
# to a, not to the b that follows the comma). Values are post-NFKC, so the
# full-width ， ； fold onto , ; here.
#
# SEGMENT_SEPARATOR joins the context segments (one per source element) after
# normalization. N(.) never leaves a newline behind, so the separator only
# ever marks an element boundary, and it is a clause break: a symbol in the
# next element is never "nearer" than one in the same sentence.
SEGMENT_SEPARATOR = "\n"
PAIR_CLAUSE_BREAKS = frozenset(",;.。、" + SEGMENT_SEPARATOR)

# --- formula statuses (spec §7.3) ------------------------------------------
FORMULA_STATUS_OK = "ok"
FORMULA_STATUS_NONE_STATED = "none_stated"
FORMULA_STATUS_NONE_GROUNDED = "none_grounded"
FORMULA_STATUS_FAILED = "failed"
FORMULA_STATUSES: tuple[str, ...] = (
    FORMULA_STATUS_OK,
    FORMULA_STATUS_NONE_STATED,
    FORMULA_STATUS_NONE_GROUNDED,
    FORMULA_STATUS_FAILED,
)

# --- verification reject reasons (spec §7.3, in check order) --------------
REJECT_SYMBOL_NOT_IN_FORMULA = "symbol_not_in_formula"
REJECT_SYMBOL_NOT_IN_CONTEXT = "symbol_not_in_context"
REJECT_MEANING_NOT_IN_CONTEXT = "meaning_not_in_context"
REJECT_TOO_FAR = "too_far"
REJECT_NEARER_OTHER_SYMBOL = "nearer_other_symbol"
REJECT_DUPLICATE = "duplicate"
REJECT_OVER_CAP = "over_cap"
REJECT_REASONS: tuple[str, ...] = (
    REJECT_SYMBOL_NOT_IN_FORMULA,
    REJECT_SYMBOL_NOT_IN_CONTEXT,
    REJECT_MEANING_NOT_IN_CONTEXT,
    REJECT_TOO_FAR,
    REJECT_NEARER_OTHER_SYMBOL,
    REJECT_DUPLICATE,
    REJECT_OVER_CAP,
)

# --- scope (spec §五, D15) --------------------------------------------------
# Knowhow projections are excluded by id prefix (no SQL): their object_type
# is a column name, and a column that happens to be called "formula" is not a
# formula. services/knowhow/ids.py builds its ids from this constant.
KNOWHOW_OBJECT_ID_PREFIX = "ko-kh-"


@dataclass(frozen=True)
class AttrSpec:
    """One object type's attrs: the response key and its item cap."""

    key: str
    max_items: int


# Only concept and formula carry attrs (spec §四). Keys are stored
# ``knowledge_objects.object_type`` values (lower case).
ATTR_SPECS: Mapping[str, AttrSpec] = MappingProxyType({
    "concept": AttrSpec(key="aliases", max_items=ALIASES_MAX),
    "formula": AttrSpec(key="variables", max_items=VARIABLES_MAX),
})
