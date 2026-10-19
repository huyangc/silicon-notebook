"""KG-analysis **findings list** contracts (design 2026-10-19, §三/§四/§八).

Pure vocabulary + pure checks, shared by the rule engine
(``app.services.kg_findings``), the publish path that will persist it, the
store write entry (both backends) and the read side. Zero
app.services / app.repositories dependency.

What lives here and why it is one module:

  · rule ids, severities, subject / action kinds — the wire codes. The API only
    ever carries these codes; the frontend maps them to interface words (原设计
    T3 红线), so a code is a contract, not a label;
  · the rule thresholds, as **named constants** written into every findings
    ledger payload together with ``RULES_VERSION`` — a report can prove which
    thresholds produced it. They are product parameters (same class as
    ``MAINSTREAM_COVERAGE``), deliberately NOT deployment-tunable Settings;
    the one cost budget (F2's candidate-pair cap) is a Settings field instead;
  · the per-rule persistence cap and the checks the store write entry runs
    (``check_findings_ledger``: the ledger's ``persisted`` must equal the detail
    rows actually written, per rule);
  · the ledger-contract stamp (``LEDGER_CONTRACT_KEY``) that lets the read side
    tell "this ledger predates findings" (upgrade, grey) from "a required
    artifact is missing" (red);
  · the unit tables of every numeric leaf the engine emits, so one field name
    has exactly one meaning inside a payload (the same guard
    ``kg_analysis.ARTIFACT_UNITS`` runs on the other artifacts).

⚠ Threshold provenance: every value below was calibrated on ONE local library
(``nb-b37185f4ae`` in the developer's PostgreSQL ``silicon_notebook_local_pg16_live``:
84 sources, 41,713 usable visible objects) and is an **initial value** pending
the production read-only rehearsal (design §十四③ ruling: implement with the
local values now, re-check against the production distribution when it comes
back). Changing any of them, or any rule's logic, MUST bump ``RULES_VERSION``:
the comparison between two builds is only meaningful under one rule set.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ------------------------------------------------------------------ rule codes
# A tier (§四): the user can act on these from the panel.
RULE_OFF_TOPIC_SOURCE = "F1"       # 疑似内容错配 → delete source
RULE_DUPLICATE_IMPORT = "F2"       # 疑似重复导入 → delete one
RULE_PARSE_DAMAGE = "F3"           # 疑似解析残缺 → reparse
RULE_UNDER_MERGE = "F4"            # 疑似没合并的同名概念 → review merge
RULE_ISOLATED_OBJECTS = "F5"       # 没建立关联的内容 → relink
# B tier (§五): only development can fix it; shown without any button.
RULE_SEED_DEFECT = "S1"            # 合并种子归一漏洞(窄的一半)

FINDING_RULES: Tuple[str, ...] = (
    RULE_OFF_TOPIC_SOURCE,
    RULE_DUPLICATE_IMPORT,
    RULE_PARSE_DAMAGE,
    RULE_UNDER_MERGE,
    RULE_ISOLATED_OBJECTS,
)
"""Rules that produce detail rows (``kg_analysis_findings``). S1 is a count in
the ledger payload, never a row: there is nothing per-subject to act on."""
DEV_RULES: Tuple[str, ...] = (RULE_SEED_DEFECT,)
ALL_RULES: Tuple[str, ...] = FINDING_RULES + DEV_RULES

# ----------------------------------------------------------------- severities
# Ordering and colour only — never summed into a score (§3.3).
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_RANK: Dict[str, int] = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
RULE_SEVERITY: Dict[str, str] = {
    RULE_OFF_TOPIC_SOURCE: SEVERITY_HIGH,
    RULE_DUPLICATE_IMPORT: SEVERITY_MEDIUM,
    RULE_PARSE_DAMAGE: SEVERITY_MEDIUM,
    RULE_UNDER_MERGE: SEVERITY_LOW,
    RULE_ISOLATED_OBJECTS: SEVERITY_LOW,
}

# -------------------------------------------------------------- subject kinds
SUBJECT_SOURCE = "source"
SUBJECT_SOURCE_PAIR = "source_pair"
SUBJECT_CONCEPT_GROUP = "concept_group"
SUBJECT_NOTEBOOK = "notebook"
RULE_SUBJECT_KIND: Dict[str, str] = {
    RULE_OFF_TOPIC_SOURCE: SUBJECT_SOURCE,
    RULE_DUPLICATE_IMPORT: SUBJECT_SOURCE_PAIR,
    RULE_PARSE_DAMAGE: SUBJECT_SOURCE,
    RULE_UNDER_MERGE: SUBJECT_CONCEPT_GROUP,
    RULE_ISOLATED_OBJECTS: SUBJECT_NOTEBOOK,
}

# Stable subject keys (§3.2). F1/F3: the source id. F2: the two source ids in
# lexicographic order joined by this separator (an unordered pair, independent
# of discovery order; source ids are ``src-<hex>`` and never contain it). F4:
# the squash key (``kg_merge_seed.concept_squash_key``) — NOT a canonical id,
# which moves whenever a cluster's minimum member changes. F5: one row per
# notebook under this literal key. No rule ever keys on a board id: board ids
# are re-minted on every full rebuild.
SOURCE_PAIR_KEY_SEPARATOR = "|"
NOTEBOOK_SUBJECT_KEY = "notebook"


def source_pair_key(source_a: str, source_b: str) -> str:
    """F2's subject key: the unordered pair, smaller id first."""
    low, high = sorted((str(source_a), str(source_b)))
    return f"{low}{SOURCE_PAIR_KEY_SEPARATOR}{high}"


# --------------------------------------------------------------- action kinds
ACTION_DELETE_SOURCE = "delete_source"
ACTION_REPARSE_SOURCE = "reparse_source"
ACTION_REVIEW_MERGE = "review_merge"
ACTION_RELINK = "relink"
ACTION_NONE = "none"
ACTION_KINDS: Tuple[str, ...] = (
    ACTION_DELETE_SOURCE, ACTION_REPARSE_SOURCE, ACTION_REVIEW_MERGE,
    ACTION_RELINK, ACTION_NONE,
)
# Capability names exactly as the routes guard them (`require_notebook_capability`).
# The panel only shows a button; the backend stays the sole authority.
CAPABILITY_SOURCES_WRITE = "sources:write"
CAPABILITY_KG_WRITE = "kg:write"

# ----------------------------------------------------------------- thresholds
# All calibrated on nb-b37185f4ae (see module docstring); initial values until
# the production read-only rehearsal (§十四③).

# F1 — share of a source's concept canonicals that also occur in at least one
# other, NON-duplicate-partner source. Strictly below → hit. Local: 12 hits in
# 0%–4.5%, next source 7.1% (a clean gap); 10% → 15 hits, 2% → 10.
F1_SHARED_MAX = 0.05
# F1 — sources with fewer concept canonicals are not judged ("概念太少"). Local
# hits have ≥ 16; floors 5 and 10 both give 12 hits, 20 gives 11.
F1_MIN_CONCEPTS = 10
# F2 — concept-canonical Jaccard at or above → duplicate import. Local: 8 pairs
# in 0.48–0.65, the next pair is 0.20 (Qwen2.5 vs Qwen2, sibling papers).
DUP_J_MIN = 0.45
# F2 — concepts present in MORE than this many sources are "通用概念" and left
# out of both sets: they say nothing about whether two files are the same one.
# ⚠ Over 4.9 万 production sources 30 may be too small — a rehearsal item.
UBIQUITOUS_SOURCE_COUNT = 30
# F3a — a concept-canonical name with a run of at least this many consecutive
# single-character tokens ("L l a m a 3 . 1") is a broken name.
F3A_MIN_SINGLE_CHAR_RUN = 4
# F3a — the source hits when broken names are at least this share of its
# concept canonicals AND at least F3A_MIN_NAMES of them. Local: one source has
# 2 broken names of 73 (2.7%) — no hit; F3 has no local positive sample at all.
F3A_SHARE_MIN = 0.10
F3A_MIN_NAMES = 5
# F3b — relations-per-object below this fraction of the library median → hit.
# Local p10–p90 of the ratio: 1.08–1.41.
F3B_RELATIONS_PER_OBJECT_FRACTION = 0.5
# F3b — that sub-signal only judges sources with at least this many objects,
# and its median is taken over those sources only: a three-object note with no
# relation says nothing about parsing (main-session ruling, 2026-10-19).
F3B_MIN_OBJECTS = 20
# F3b — isolated-object share strictly above max(F3B_ISOLATED_SHARE_FACTOR ×
# library median, F3B_ISOLATED_SHARE_FLOOR) AND at least F3B_MIN_ISOLATED
# isolated objects → hit. The floor stops a 0 median from flagging 20 isolated
# objects that are 1% of a source (same ruling). Local p50 4%, p90 8%, max 11%.
F3B_ISOLATED_SHARE_FACTOR = 3.0
F3B_ISOLATED_SHARE_FLOOR = 0.10
F3B_MIN_ISOLATED = 20
# F3b's third signal in the design ("objects per chunk below 1/3 of the
# median") is NOT part of rules v1: it needs a new full GROUP BY over chunks
# whose production cost is unknown (plan §三; the rehearsal script measures it).

# Bump on ANY change of a threshold above or of a rule's logic (§3.2): two
# builds are only compared when both carry the same version.
RULES_VERSION = 1

# Detail rows persisted per rule (§7.2 / §八). The ledger records
# ``total`` / ``persisted`` / ``truncated`` per rule — a cap is never silent.
MAX_PERSISTED_FINDINGS = 500
# Sample names carried per finding (design: "最多 5 个样例名").
MAX_SAMPLES = 5
# F5 evidence: the sources with the most isolated objects.
F5_TOP_SOURCES = 5
# F4 subject_refs (sources holding visible members) are capped; the full count
# stays in the evidence (``member_sources``).
MAX_SUBJECT_REFS = 50
# S1 examples shown in the "needs development" zone.
S1_EXAMPLES = 3
# Comparison summary: names of findings that disappeared since the last build.
MAX_RESOLVED_SAMPLES = 20

RULE_THRESHOLDS: Dict[str, float] = {
    "f1_shared_max": F1_SHARED_MAX,
    "f1_min_concepts": F1_MIN_CONCEPTS,
    "dup_j_min": DUP_J_MIN,
    "ubiquitous_source_count": UBIQUITOUS_SOURCE_COUNT,
    "f3a_min_single_char_run": F3A_MIN_SINGLE_CHAR_RUN,
    "f3a_share_min": F3A_SHARE_MIN,
    "f3a_min_names": F3A_MIN_NAMES,
    "f3b_relations_per_object_fraction": F3B_RELATIONS_PER_OBJECT_FRACTION,
    "f3b_min_objects": F3B_MIN_OBJECTS,
    "f3b_isolated_share_factor": F3B_ISOLATED_SHARE_FACTOR,
    "f3b_isolated_share_floor": F3B_ISOLATED_SHARE_FLOOR,
    "f3b_min_isolated": F3B_MIN_ISOLATED,
    "max_persisted_findings": MAX_PERSISTED_FINDINGS,
}
"""Written verbatim into every findings ledger payload under ``thresholds``."""

# Stored merge decisions (``concept_merge_candidates.status``) that mean "these
# are NOT the same thing": F4 / S1 never report a pair the user judged so (§4.4).
# Decisions are keyed by v1 SEEDS, never by canonical id — a canonical id moves
# whenever its cluster's minimum member changes (`decided_seed_pairs_from`).
DECISION_APART_STATUSES: Tuple[str, ...] = ("rejected", "deferred")

# Rule status values in the ledger (§3.4 「已通过」: checked-and-clean and
# could-not-check are different states and must never both render as a tick).
RULE_STATUS_RAN = "ran"
RULE_STATUS_SKIPPED = "skipped"
SKIP_NO_OBJECTS = "no_objects"
SKIP_FEWER_THAN_TWO_SOURCES = "fewer_than_two_sources"
SKIP_NO_NAMED_CONCEPTS = "no_named_concepts"

# --------------------------------------------------------- ledger contract stamp
# §7.4: after an upgrade the deployed ledgers have no ``findings`` row, and the
# read side would report "a required artifact is missing" (red) on every
# library until someone rebuilds. To tell "built before findings existed"
# (grey, "not generated since the upgrade") from a genuinely missing artifact,
# every ledger payload carries this stamp: the service stamps it, the store
# write entry checks it, a missing key fails hard. A ledger row WITHOUT the key
# was written before this contract existed.
#
# ⚠ Not wired yet: ``check_artifact_payloads`` does not call
# ``check_ledger_contract`` until the producer (the findings publish step) stamps
# every payload in the same commit — wiring the check first would fail every
# rebuild.
LEDGER_CONTRACT_KEY = "ledger_contract"
LEDGER_CONTRACT_VERSION = 1


def stamp_ledger_contract(payloads: Mapping[str, dict]) -> Dict[str, dict]:
    """Return a copy of ``payloads`` with the contract stamp on every kind."""
    return {
        kind: {**payload, LEDGER_CONTRACT_KEY: LEDGER_CONTRACT_VERSION}
        for kind, payload in payloads.items()
    }


def check_ledger_contract(payloads: Mapping[str, Mapping[str, object]]) -> None:
    """Store write entry: every ledger payload must carry the current stamp.

    Stamping and checking live in different functions on purpose (a guard that
    stamps and checks in one place is always true). ``bool`` is rejected
    explicitly — ``True`` would pass ``isinstance(value, int)`` as 1."""
    for kind in sorted(payloads):
        value = payloads[kind].get(LEDGER_CONTRACT_KEY)
        if isinstance(value, bool) or value != LEDGER_CONTRACT_VERSION:
            raise ValueError(
                f"KG 分析产物账本:{kind} 的 payload 缺少或带错了 {LEDGER_CONTRACT_KEY}"
                f"(应为 {LEDGER_CONTRACT_VERSION},见 stamp_ledger_contract)。少了它,"
                "读侧就分不清「升级后还没生成」与「本该有却缺失」"
            )


def ledger_row_predates_findings(payload: Mapping[str, object]) -> bool:
    """True when this ledger row was written before the findings contract
    existed — the read side's "upgrade pending" (grey) evidence for a ledger
    that lacks ``findings``."""
    return LEDGER_CONTRACT_KEY not in payload


# ------------------------------------------------------------------- units
# Unit codes. The first block reuses ``app.services.kg_analysis``'s vocabulary
# verbatim (pinned by a test, since domain cannot import services); the rest are
# new for findings. One field name → one unit, inside one payload.
UNIT_OBJECTS = "objects"
UNIT_CANONICAL = "canonical"
UNIT_SOURCES = "sources"
UNIT_RATIO = "ratio"
UNIT_SEQ = "seq"
UNIT_FINDINGS = "findings"                     # finding rows
UNIT_SOURCE_PAIRS = "source_pairs"             # unordered source pairs
UNIT_CANDIDATE_PAIRS = "candidate_pairs"       # F2 distinct candidate pairs (prefix join)
UNIT_PAIR_GENERATIONS = "pair_generations"     # F2 pairs generated, repeats included (the capped count)
UNIT_COMPONENTS = "components"                 # connected components of the canonical graph
UNIT_CONCEPT_GROUPS = "concept_groups"         # squash-key groups / v2-seed classes
UNIT_SPELLINGS = "spellings"                   # distinct v2 seeds inside one group
UNIT_RELATIONS_PER_OBJECT = "relations_per_object"
UNIT_FACTOR = "factor"                         # multiple / fraction of a median
UNIT_TOKENS = "tokens"                         # name tokens
UNIT_VERSION = "version"                       # contract / rule-set version numbers
SHARED_KG_ANALYSIS_UNITS = (
    UNIT_OBJECTS, UNIT_CANONICAL, UNIT_SOURCES, UNIT_RATIO, UNIT_SEQ,
)

# Numeric leaves of the findings LEDGER payload (rule statuses, thresholds,
# S3 structure, comparison summary). Keys are leaf field names anywhere in the
# nested payload; ``test_every_numeric_leaf_has_a_unit`` walks real engine
# output against this table.
FINDINGS_PAYLOAD_UNITS: Dict[str, str] = {
    "rules_version": UNIT_VERSION,
    LEDGER_CONTRACT_KEY: UNIT_VERSION,
    # thresholds
    "f1_shared_max": UNIT_RATIO,
    "f1_min_concepts": UNIT_CANONICAL,
    "dup_j_min": UNIT_RATIO,
    "ubiquitous_source_count": UNIT_SOURCES,
    "f3a_min_single_char_run": UNIT_TOKENS,
    "f3a_share_min": UNIT_RATIO,
    "f3a_min_names": UNIT_CANONICAL,
    "f3b_relations_per_object_fraction": UNIT_FACTOR,
    "f3b_min_objects": UNIT_OBJECTS,
    "f3b_isolated_share_factor": UNIT_FACTOR,
    "f3b_isolated_share_floor": UNIT_RATIO,
    "f3b_min_isolated": UNIT_OBJECTS,
    "max_persisted_findings": UNIT_FINDINGS,
    # per-rule status, common
    "total": UNIT_FINDINGS,
    "persisted": UNIT_FINDINGS,
    # F1
    "sources_checked": UNIT_SOURCES,
    "sources_too_few_concepts": UNIT_SOURCES,
    "sources_hit": UNIT_SOURCES,
    "sources_deferred_to_f3": UNIT_SOURCES,
    # F2 — distinct pairs verified vs pairs generated: the cap bounds the latter
    # (one generation per shared prefix concept), so the two never share a unit.
    "candidate_pairs": UNIT_CANDIDATE_PAIRS,
    "candidate_pair_generations": UNIT_PAIR_GENERATIONS,
    "candidate_pair_generation_limit": UNIT_PAIR_GENERATIONS,
    "pairs_hit": UNIT_SOURCE_PAIRS,
    "pairs_absorbed_into_f1": UNIT_SOURCE_PAIRS,
    # F3
    "median_relations_per_object": UNIT_RELATIONS_PER_OBJECT,
    "median_isolated_share": UNIT_RATIO,
    "sources_hit_fragmented_names": UNIT_SOURCES,
    "sources_hit_low_relations": UNIT_SOURCES,
    "sources_hit_isolated": UNIT_SOURCES,
    # F4 / S1
    "canonicals_checked": UNIT_CANONICAL,
    "unnamed_concept_canonicals": UNIT_CANONICAL,
    "groups_decided_apart": UNIT_CONCEPT_GROUPS,
    "groups_unreproducible": UNIT_CONCEPT_GROUPS,
    "narrow_groups": UNIT_CONCEPT_GROUPS,
    "narrow_group_objects": UNIT_OBJECTS,
    # F5
    "objects_checked": UNIT_OBJECTS,
    # S3 structure (connected components only — never the board partition)
    "graph_nodes": UNIT_CANONICAL,
    "components": UNIT_COMPONENTS,
    "giant_nodes": UNIT_CANONICAL,
    "usable_objects": UNIT_OBJECTS,
    "giant_objects": UNIT_OBJECTS,
    "giant_object_share": UNIT_RATIO,
    "island_components": UNIT_COMPONENTS,
    "island_sources": UNIT_SOURCES,
    # comparison summary (finalize_findings)
    "previous_built_at_seq": UNIT_SEQ,
    "previous_built_at_cluster_seq": UNIT_SEQ,
    "resolved": UNIT_FINDINGS,
    "persisting": UNIT_FINDINGS,
    "new": UNIT_FINDINGS,
    "impact_objects": UNIT_OBJECTS,
}

# Numeric leaves of each finding's ``evidence``, per rule (only numbers the
# criterion actually used, §3.1 — "for reference" numbers belong to the
# diagnostics zone).
FINDING_EVIDENCE_UNITS: Dict[str, Dict[str, str]] = {
    RULE_OFF_TOPIC_SOURCE: {
        "shared_share": UNIT_RATIO,
        "giant_share": UNIT_RATIO,
        "concepts": UNIT_CANONICAL,
    },
    RULE_DUPLICATE_IMPORT: {
        "jaccard": UNIT_RATIO,
        "shared_concepts": UNIT_CANONICAL,
        "objects": UNIT_OBJECTS,
    },
    RULE_PARSE_DAMAGE: {
        "fragmented_names": UNIT_CANONICAL,
        "fragmented_share": UNIT_RATIO,
        "fragmented_objects": UNIT_OBJECTS,
        "relations_per_object": UNIT_RELATIONS_PER_OBJECT,
        "median_relations_per_object": UNIT_RELATIONS_PER_OBJECT,
        "isolated_objects": UNIT_OBJECTS,
        "isolated_share": UNIT_RATIO,
        "median_isolated_share": UNIT_RATIO,
        "isolated_share_threshold": UNIT_RATIO,
    },
    RULE_UNDER_MERGE: {
        "canonicals": UNIT_CANONICAL,
        "spellings": UNIT_SPELLINGS,
        "member_sources": UNIT_SOURCES,
        "objects": UNIT_OBJECTS,
    },
    RULE_ISOLATED_OBJECTS: {
        "isolated_objects": UNIT_OBJECTS,
        "isolated_share": UNIT_RATIO,
        "usable_objects": UNIT_OBJECTS,
    },
}

# ------------------------------------------------------------------ rows
# Column names of one detail row (``kg_analysis_findings``, §8.2). The
# ``first_seen_*`` pair is filled by ``finalize_findings`` inside the publish
# transaction; "built at" is the ledger row's seq, never per row.
FINDING_ROW_FIELDS: Tuple[str, ...] = (
    "rule_id", "subject_key", "subject_kind", "subject_label", "subject_refs",
    "severity", "impact_objects", "evidence",
    "first_seen_kg_seq", "first_seen_cluster_seq",
)


def finding_sort_key(row: Mapping[str, object]) -> Tuple[int, int, str, str]:
    """Display order (§3.3): severity, then impact descending, then the primary
    key ``(rule_id, subject_key)`` ascending — deterministic for equal impact."""
    return (
        SEVERITY_RANK[str(row["severity"])],
        -int(row["impact_objects"]),        # type: ignore[arg-type]
        str(row["rule_id"]),
        str(row["subject_key"]),
    )


def finding_actions(
    rule_id: str,
    subject_key: str,
    subject_refs: Sequence[str],
    evidence: Mapping[str, object],
) -> List[Dict[str, str]]:
    """The recommended actions of one finding, DERIVED from the persisted row.

    Derived rather than stored: the detail table (§8.2) has no action column, and
    a stored copy could drift from the evidence it was decided on. Only the
    suggestion lives here — whether a viewer sees a button is decided by the
    existing entry points' own rules (read-only workspace, mirror fences,
    promotion sources), and the backend stays the only authority.

      · F1: delete the source — for two duplicated off-topic copies merged into
        one row, one delete per copy (both should go, §4.1);
      · F2: delete the copy NOT marked ``keep`` in ``evidence.sources``;
      · F3: batch reparse (``POST /notebooks/{id}/sources/reparse``);
      · F4: open the pending-merge review; F5: relink.
    """
    if rule_id == RULE_OFF_TOPIC_SOURCE:
        return [
            {"kind": ACTION_DELETE_SOURCE, "capability": CAPABILITY_SOURCES_WRITE,
             "source_id": str(source_id)}
            for source_id in subject_refs
        ]
    if rule_id == RULE_DUPLICATE_IMPORT:
        sides = evidence.get("sources") or []
        kept = {str(side["source_id"]) for side in sides if side.get("keep")}  # type: ignore[union-attr]
        return [
            {"kind": ACTION_DELETE_SOURCE, "capability": CAPABILITY_SOURCES_WRITE,
             "source_id": str(source_id)}
            for source_id in subject_refs if str(source_id) not in kept
        ]
    if rule_id == RULE_PARSE_DAMAGE:
        return [{"kind": ACTION_REPARSE_SOURCE, "capability": CAPABILITY_SOURCES_WRITE,
                 "source_id": str(subject_key)}]
    if rule_id == RULE_UNDER_MERGE:
        return [{"kind": ACTION_REVIEW_MERGE, "capability": CAPABILITY_KG_WRITE}]
    if rule_id == RULE_ISOLATED_OBJECTS:
        return [{"kind": ACTION_RELINK, "capability": CAPABILITY_KG_WRITE}]
    return [{"kind": ACTION_NONE, "capability": ""}]


def check_findings_ledger(
    payload: Mapping[str, object], rows: Iterable[Mapping[str, object]]
) -> None:
    """Store write entry for the findings ledger row + its detail rows.

    Fails hard on any of:
      · ``rules_version`` missing or not an int;
      · a finding rule without its status entry, or an unknown rule id;
      · per rule: ``persisted`` ≠ the number of detail rows written for it,
        ``persisted`` > ``MAX_PERSISTED_FINDINGS``, ``total`` < ``persisted``,
        or ``truncated`` false while ``persisted < total`` (a cap is never silent);
      · a duplicate ``(rule_id, subject_key)``, or a row whose severity / subject
        kind is not its rule's.

    Same reasoning as ``check_artifact_payloads``: "the artifact exists" is read
    off the ledger row, so a ledger that claims rows the table does not hold (or
    the reverse) is a silent lie on the read side.
    """
    version = payload.get("rules_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("findings 账本:payload 缺少整数 rules_version")
    statuses = payload.get("rules")
    if not isinstance(statuses, Mapping):
        raise ValueError("findings 账本:payload 缺少 rules(每条规则的状态)")
    unknown = sorted(set(statuses) - set(ALL_RULES))
    if unknown:
        raise ValueError(f"findings 账本:契约外的规则 {unknown}")
    missing = sorted(set(ALL_RULES) - set(statuses))
    if missing:
        raise ValueError(f"findings 账本:缺少规则状态 {missing}")
    written: Dict[str, int] = {rule: 0 for rule in FINDING_RULES}
    seen: set = set()
    for row in rows:
        rule = str(row["rule_id"])
        if rule not in written:
            raise ValueError(f"findings 明细:规则 {rule!r} 不产出明细行")
        key = (rule, str(row["subject_key"]))
        if key in seen:
            raise ValueError(f"findings 明细:主键重复 {key}")
        seen.add(key)
        if row["severity"] != RULE_SEVERITY[rule]:
            raise ValueError(f"findings 明细:{key} 的严重度不是 {RULE_SEVERITY[rule]}")
        if row["subject_kind"] != RULE_SUBJECT_KIND[rule]:
            raise ValueError(f"findings 明细:{key} 的主体类型不是 {RULE_SUBJECT_KIND[rule]}")
        written[rule] += 1
    for rule in FINDING_RULES:
        status = statuses[rule]
        persisted = int(status.get("persisted", 0) or 0)   # type: ignore[union-attr]
        total = int(status.get("total", 0) or 0)           # type: ignore[union-attr]
        if persisted != written[rule]:
            raise ValueError(
                f"findings 账本:{rule} 记了 persisted={persisted},实际写了 "
                f"{written[rule]} 行明细 —— 账本与明细必须同批、同数"
            )
        if persisted > MAX_PERSISTED_FINDINGS:
            raise ValueError(
                f"findings 账本:{rule} 落库 {persisted} 行,超过上限 {MAX_PERSISTED_FINDINGS}"
            )
        if total < persisted:
            raise ValueError(f"findings 账本:{rule} 的 total={total} < persisted={persisted}")
        if persisted < total and not status.get("truncated"):       # type: ignore[union-attr]
            raise ValueError(
                f"findings 账本:{rule} 只落了 {persisted}/{total} 行却没记 truncated —— 截断绝不静默"
            )


def check_findings_comparison(
    comparison: Optional[Mapping[str, object]],
) -> None:
    """``finalize_findings``'s summary must say whether it is comparable, and a
    comparable summary must carry all three counts."""
    if comparison is None:
        raise ValueError("findings 账本:缺少对比摘要 comparison")
    if not isinstance(comparison.get("comparable"), bool):
        raise ValueError("findings 账本:comparison.comparable 必须是布尔值")
    if comparison["comparable"]:
        for name in ("resolved", "persisting", "new"):
            value = comparison.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"findings 账本:可比的对比摘要缺少整数 {name}")
