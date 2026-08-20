"""Centralized Deep Report and reasoning heuristics/resource rails."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


DEFAULT_REPORT_MIN_RELEVANT_ITEMS = 3
DEFAULT_REPORT_MIN_FAMILIES = 2
DEFAULT_REPORT_COMPLETE_MIN_FAMILIES = 3
DEFAULT_REPORT_MAX_TOP_FAMILY_SHARE = 0.8

DEFAULT_REASONING_MAX_PPR_RETRIEVES = 3
DEFAULT_REASONING_MAX_EXACT_LOOKUPS = 3
DEFAULT_REASONING_MAX_FOLLOW_CHAIN_ACTIONS = 3
DEFAULT_REASONING_COMMUNITY_PEERS_CAP_FACTOR = 2
DEFAULT_REASONING_MAX_OUTLINE_UPDATES = 6
# Agentic Memory P4 (T5).
DEFAULT_REASONING_MAX_CONSULT_MEMORY = 2

# Structural protocol bounds are invariant rather than deployment tuning.
OUTLINE_MAX_SECTIONS = 12
OUTLINE_TITLE_CHARS = 60
OUTLINE_ID_CHARS = 32
OUTLINE_EVIDENCE_KEY_CHARS = 48
OUTLINE_MAX_EVIDENCE = 8

REASONING_PREFER_WEIGHTS: Mapping[str, tuple[float, float]] = MappingProxyType({
    "balanced": (0.4, 0.6),
    "keyword": (0.7, 0.3),
    "semantic": (0.2, 0.8),
})


@dataclass(frozen=True)
class ReportSufficiencyPolicy:
    min_relevant_items: int
    min_families: int
    complete_min_families: int
    max_top_family_share: float

    def classify(self, *, relevant_items: int, family_count: int,
                 top_family_share: float,
                 completeness_required: bool) -> tuple[bool, bool]:
        required_families = (
            self.complete_min_families
            if completeness_required else self.min_families
        )
        enough = (
            relevant_items >= self.min_relevant_items
            and family_count >= required_families
            and top_family_share <= self.max_top_family_share
        )
        thin = relevant_items > 0 and family_count > 0
        return enough, thin


@dataclass(frozen=True)
class ReasoningActionPolicy:
    max_ppr_retrieves: int
    max_exact_lookups: int
    max_follow_chain_actions: int
    community_peers_cap_factor: int
    max_outline_updates: int
    max_consult_memory: int

    @property
    def max_pending_outline_evidence(self) -> int:
        return OUTLINE_MAX_EVIDENCE * (self.max_outline_updates + 1)


def report_sufficiency_policy(settings) -> ReportSufficiencyPolicy:
    return ReportSufficiencyPolicy(
        min_relevant_items=int(getattr(
            settings,
            "report_sufficiency_min_relevant_items",
            DEFAULT_REPORT_MIN_RELEVANT_ITEMS,
        )),
        min_families=int(getattr(
            settings,
            "report_sufficiency_min_families",
            DEFAULT_REPORT_MIN_FAMILIES,
        )),
        complete_min_families=int(getattr(
            settings,
            "report_sufficiency_complete_min_families",
            DEFAULT_REPORT_COMPLETE_MIN_FAMILIES,
        )),
        max_top_family_share=float(getattr(
            settings,
            "report_sufficiency_max_top_family_share",
            DEFAULT_REPORT_MAX_TOP_FAMILY_SHARE,
        )),
    )


def reasoning_action_policy(settings) -> ReasoningActionPolicy:
    return ReasoningActionPolicy(
        max_ppr_retrieves=int(getattr(
            settings,
            "reasoning_max_ppr_retrieves",
            DEFAULT_REASONING_MAX_PPR_RETRIEVES,
        )),
        max_exact_lookups=int(getattr(
            settings,
            "reasoning_max_exact_lookups",
            DEFAULT_REASONING_MAX_EXACT_LOOKUPS,
        )),
        max_follow_chain_actions=int(getattr(
            settings,
            "reasoning_max_follow_chain_actions",
            DEFAULT_REASONING_MAX_FOLLOW_CHAIN_ACTIONS,
        )),
        community_peers_cap_factor=int(getattr(
            settings,
            "reasoning_community_peers_cap_factor",
            DEFAULT_REASONING_COMMUNITY_PEERS_CAP_FACTOR,
        )),
        max_outline_updates=int(getattr(
            settings,
            "reasoning_max_outline_updates",
            DEFAULT_REASONING_MAX_OUTLINE_UPDATES,
        )),
        max_consult_memory=int(getattr(
            settings,
            "reasoning_max_consult_memory",
            DEFAULT_REASONING_MAX_CONSULT_MEMORY,
        )),
    )


__all__ = [
    "DEFAULT_REASONING_COMMUNITY_PEERS_CAP_FACTOR",
    "DEFAULT_REASONING_MAX_CONSULT_MEMORY",
    "DEFAULT_REASONING_MAX_EXACT_LOOKUPS",
    "DEFAULT_REASONING_MAX_FOLLOW_CHAIN_ACTIONS",
    "DEFAULT_REASONING_MAX_OUTLINE_UPDATES",
    "DEFAULT_REASONING_MAX_PPR_RETRIEVES",
    "OUTLINE_EVIDENCE_KEY_CHARS",
    "OUTLINE_ID_CHARS",
    "OUTLINE_MAX_EVIDENCE",
    "OUTLINE_MAX_SECTIONS",
    "OUTLINE_TITLE_CHARS",
    "REASONING_PREFER_WEIGHTS",
    "ReasoningActionPolicy",
    "ReportSufficiencyPolicy",
    "reasoning_action_policy",
    "report_sufficiency_policy",
]
