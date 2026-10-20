"""KG 分析「问题清单」的**读侧派生**(设计 2026-10-19 §3.5 / §4.5 / §8.4)。纯函数。

`KgAnalysisService` 在一个 `read_snapshot()` 里把账本、明细、标记、F4 组按种子过滤的已存决定、
这一页涉及的来源状态读出来,交给这里装配;这里不碰连接、不判后端 —— 两个后端因此结构性地
拿到逐字相同的一页。

**状态**(读侧派生,不靠前端记忆,也不靠面板按钮写标记 —— 用户在别处做的处置同样认得出):

  ``open``              待处理;
  ``handled``           已处理、更新后核验(仍在待处理区,不消失):
                          F1 / F3 来源已删除、正在解析/抽取、或问题建成之后重新解析/抽取过;
                          F2 两个来源任一已删除(重新解析不解决重复);
                          F4 有一条「确认合并」连起了这一组的两种写法;
                          F5 「补上关联」在报告建成之后改变过图谱(标记世代晚于账本);
  ``dismissed``         成员标为正常(可撤销);
  ``decided_not_same``  F4:已存的驳回 / 暂缓决定把这一组各写法两两分开了 —— 当场进「已通过」
                        (驳回不置 dirty,等更新后消失会让它永远「仍在」);
  ``relink_spent``      F5:「补上关联」跑过、之后没有新内容 —— 再跑也补不上(它确定、幂等),
                        移出待处理,不放按钮。

**区**:``open`` = open + handled;``passed`` = decided_not_same + relink_spent;
``dismissed`` = dismissed。区的划分只依赖标记、F4 决定与 F5 标记(全部有界),来源点查只做
**当前这一页**(§3.5「只查当前这一页」)—— handled 与 open 同在待处理区,来源状态不改变分区。

「问题建成之后」以 `findings` 账本行的 ``created_at`` 为准(§3.5)。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from app.domain.kg_analysis_contracts import MARK_DISMISSED, MARK_RELINK_ATTEMPTED
from app.domain.kg_findings_contracts import (
    NOTEBOOK_SUBJECT_KEY,
    RULE_DUPLICATE_IMPORT,
    RULE_ISOLATED_OBJECTS,
    RULE_OFF_TOPIC_SOURCE,
    RULE_PARSE_DAMAGE,
    RULE_SEED_DEFECT,
    RULE_STATUS_RAN,
    RULE_UNDER_MERGE,
    finding_actions,
    finding_sort_key,
)
from app.domain.kg_merge_seed import OVERRIDE_SEED_PREFIX
from app.services.kg_findings import (
    UNDER_MERGE_CONFIRMED,
    UNDER_MERGE_DECIDED_APART,
    under_merge_decision,
)

ZONE_OPEN = "open"
ZONE_PASSED = "passed"
ZONE_DISMISSED = "dismissed"
FINDING_ZONES = (ZONE_OPEN, ZONE_PASSED, ZONE_DISMISSED)

STATUS_OPEN = "open"
STATUS_HANDLED = "handled"
STATUS_DISMISSED = "dismissed"
STATUS_DECIDED_NOT_SAME = "decided_not_same"
STATUS_RELINK_SPENT = "relink_spent"
_ZONE_OF_STATUS = {
    STATUS_OPEN: ZONE_OPEN,
    STATUS_HANDLED: ZONE_OPEN,
    STATUS_DECIDED_NOT_SAME: ZONE_PASSED,
    STATUS_RELINK_SPENT: ZONE_PASSED,
    STATUS_DISMISSED: ZONE_DISMISSED,
}

# Why a row reads "handled" (an internal code; the panel maps it to words).
HANDLED_SOURCE_DELETED = "source_deleted"
HANDLED_SOURCE_PROCESSING = "source_processing"
HANDLED_SOURCE_REPROCESSED = "source_reprocessed"
HANDLED_MERGE_CONFIRMED = "merge_confirmed"
HANDLED_RELINKED = "relinked"

# The candidate statuses that are a stored decision (`decided_seed_pairs_from`).
DECIDED_CANDIDATE_STATUSES = ("confirmed", "rejected", "deferred")

# Stored evidence the read side uses and never returns (codex #941 R3). F4's
# ``member_seeds`` are the v1 seeds of EVERY member of the group's canonicals,
# hidden (Memory / Knowhow) members included — normalised, readable names past
# the reader-visible boundary (docs/development.md: who may read a Memory- or
# Knowhow-derived source). They exist to match stored merge decisions
# (`f4_member_seeds`); ``seeds_truncated`` only qualifies them. Everything else
# a row carries is a count, a visible source's id / title, a canonical id (the
# identifier pending merges and the graph return to the same readers) or a
# name read from visible sources' own objects.
INTERNAL_EVIDENCE_KEYS = frozenset({"member_seeds", "seeds_truncated"})
# A source still in the pipeline: queued → parsing → parsed → extracting → extracted.
# ``parsed`` waits for extraction but nothing runs on it; the extraction run's own
# ``running`` covers the KG pass on an already-extracted source.
_PROCESSING_SOURCE_STATUSES = frozenset({"queued", "parsing", "extracting"})


# ------------------------------------------------------------ small helpers
def _timestamp(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _later(value: object, reference: object) -> bool:
    """``value`` strictly after ``reference``; unparseable or empty → False."""
    left, right = _timestamp(value), _timestamp(reference)
    return left is not None and right is not None and left > right


def source_is_processing(state: Mapping[str, object]) -> bool:
    return (str(state.get("status") or "") in _PROCESSING_SOURCE_STATUSES
            or str(state.get("last_extraction_status") or "") == "running")


def f4_member_seeds(rows: Iterable[Mapping[str, object]]) -> Dict[str, Set[str]]:
    """canonical id → decision seeds (v1 and v2), for every canonical of every F4
    group in the detail,
    straight from the evidence (``member_seeds``, written at publication from ALL
    members, hidden ones included) plus the canonical id's own seed (``K-`` + min
    seed, the key legacy decision rows fell back to). No member object is read:
    the page's cost does not grow with group sizes. A canonical whose seeds were
    capped (``seeds_truncated``) can only miss a decision — it stays open, the
    safe direction."""
    seeds: Dict[str, Set[str]] = {}
    for row in rows:
        if row["rule_id"] != RULE_UNDER_MERGE:
            continue
        evidence = row["evidence"] or {}
        stored = evidence.get("member_seeds") or {}            # type: ignore[union-attr]
        for cls in evidence.get("classes") or []:              # type: ignore[union-attr]
            for cid in cls:
                cid = str(cid)
                own = {cid[2:]} if cid.startswith("K-") else set()
                seeds.setdefault(cid, set()).update(
                    own, (str(seed) for seed in stored.get(cid) or []))
    return seeds


def decision_lookup_seeds(member_seeds: Mapping[str, Set[str]]) -> List[str]:
    """The stored decision keys that can name these members: each member seed
    (v1 or v2) as is, and as the override seed ``~v1:<seed>`` a recomputed
    decision keeps a collided v1 seed under (``kg_merge_seed.decision_match_key``
    reads it back as the seed). Sorted, deduplicated — the bound list of
    ``merge_candidate_states_for_seeds``."""
    plain = set().union(*member_seeds.values()) if member_seeds else set()
    return sorted(plain | {OVERRIDE_SEED_PREFIX + seed for seed in plain})


def decisions_from_candidates(rows: Iterable[Mapping[str, object]]) -> Dict[frozenset, str]:
    """Candidate-state rows → ``{frozenset({seed_a, seed_b}): status}``, the exact
    derivation of ``decided_seed_pairs_from`` (legacy rows with empty seeds fall
    back to the canonical id minus ``K-``)."""
    def strip(cid: str) -> str:
        return cid[2:] if cid.startswith("K-") else cid

    out: Dict[frozenset, str] = {}
    for row in rows:
        a = str(row["seed_a"] or "") or strip(str(row["canonical_a"]))
        b = str(row["seed_b"] or "") or strip(str(row["canonical_b"]))
        out[frozenset((a, b))] = str(row["status"])
    return out


def relink_view(
    marks: Iterable[Mapping[str, object]], current_seq: int,
) -> Dict[str, object]:
    """The "补上关联" durable mark as the panel reads it (§4.5).

    ``may_change``: True when it never ran, or content arrived after it ran
    (``kg_mutation_seq`` moved past the mark) — running it again may link more;
    False when it ran on exactly the current content (deterministic and
    idempotent: what it could not link then, it cannot link now)."""
    for mark in marks:
        if (mark["rule_id"] == RULE_ISOLATED_OBJECTS
                and mark["subject_key"] == NOTEBOOK_SUBJECT_KEY
                and mark["kind"] == MARK_RELINK_ATTEMPTED):
            at = int(mark["at_kg_seq"])   # type: ignore[arg-type]
            return {"attempted": True, "at_kg_seq": at, "at": str(mark["created_at"]),
                    "may_change": int(current_seq) > at}
    return {"attempted": False, "at_kg_seq": None, "at": None, "may_change": True}


# --------------------------------------------------------------- statuses
def zone_status(
    row: Mapping[str, object],
    *,
    dismissed: Mapping[Tuple[str, str], Mapping[str, object]],
    decisions: Mapping[frozenset, str],
    member_seeds: Mapping[str, Iterable[str]],
    relink: Mapping[str, object],
    built_seq: int,
) -> Tuple[str, Optional[str]]:
    """The status that decides the row's zone, and a handled reason when it is
    already known without the page's source lookups (F4 / F5)."""
    key = (str(row["rule_id"]), str(row["subject_key"]))
    if key in dismissed:
        return STATUS_DISMISSED, None
    rule = key[0]
    if rule == RULE_UNDER_MERGE and decisions:
        verdict = under_merge_decision(
            (row["evidence"] or {}).get("classes") or [],   # type: ignore[union-attr]
            member_seeds, decisions)
        if verdict == UNDER_MERGE_DECIDED_APART:
            return STATUS_DECIDED_NOT_SAME, None
        if verdict == UNDER_MERGE_CONFIRMED:
            return STATUS_HANDLED, HANDLED_MERGE_CONFIRMED
    if rule == RULE_ISOLATED_OBJECTS and relink["attempted"]:
        if int(relink["at_kg_seq"]) > int(built_seq):       # type: ignore[arg-type]
            # it changed the graph after this report: verify on the next update
            return STATUS_HANDLED, HANDLED_RELINKED
        if relink["may_change"] is False:
            return STATUS_RELINK_SPENT, None
    return STATUS_OPEN, None


def source_handled_reason(
    row: Mapping[str, object],
    states: Mapping[str, Mapping[str, object]],
    built_at: object,
) -> Optional[str]:
    """F1 / F2 / F3 "handled" from the page's source point lookups (§3.5)."""
    rule = row["rule_id"]
    refs = [str(ref) for ref in row["subject_refs"] or []]   # type: ignore[union-attr]
    if rule == RULE_DUPLICATE_IMPORT:
        if any(not states.get(ref, {}).get("exists") for ref in refs):
            return HANDLED_SOURCE_DELETED
        return None
    if rule not in (RULE_OFF_TOPIC_SOURCE, RULE_PARSE_DAMAGE):
        return None
    if rule == RULE_PARSE_DAMAGE:
        refs = [str(row["subject_key"])]
    for ref in refs:
        state = states.get(ref) or {}
        if not state.get("exists"):
            return HANDLED_SOURCE_DELETED
    for ref in refs:
        state = states[ref]
        if source_is_processing(state):
            return HANDLED_SOURCE_PROCESSING
        # not `sources.updated_at`: renaming a source moves it too. `chunked_at`
        # moves with a reparse, the latest extraction run with a re-extraction.
        if (_later(state.get("chunked_at"), built_at)
                or _later(state.get("last_extraction_at"), built_at)):
            return HANDLED_SOURCE_REPROCESSED
    return None


def zone_rows(
    rows: Sequence[Mapping[str, object]],
    zone: str,
    *,
    marks: Iterable[Mapping[str, object]],
    decisions: Mapping[frozenset, str],
    member_seeds: Mapping[str, Iterable[str]],
    relink: Mapping[str, object],
    built_seq: int,
) -> List[Tuple[Mapping[str, object], str, Optional[str], Optional[str]]]:
    """The rows of one zone in display order (severity → impact → key, §3.3),
    each with its zone status, handled reason and dismissal time."""
    dismissed = {
        (str(mark["rule_id"]), str(mark["subject_key"])): mark
        for mark in marks if mark["kind"] == MARK_DISMISSED
    }
    out = []
    for row in sorted(rows, key=finding_sort_key):
        status, reason = zone_status(
            row, dismissed=dismissed, decisions=decisions, member_seeds=member_seeds,
            relink=relink, built_seq=built_seq)
        if _ZONE_OF_STATUS[status] != zone:
            continue
        mark = dismissed.get((str(row["rule_id"]), str(row["subject_key"])))
        out.append((row, status, reason, str(mark["created_at"]) if mark else None))
    return out


def page_sources(rows: Iterable[Mapping[str, object]]) -> List[str]:
    """Source ids the page's rows refer to (``subject_refs``; F3's subject)."""
    ids: Set[str] = set()
    for row in rows:
        ids.update(str(ref) for ref in row["subject_refs"] or [])   # type: ignore[union-attr]
        if row["rule_id"] in (RULE_OFF_TOPIC_SOURCE, RULE_PARSE_DAMAGE):
            ids.add(str(row["subject_key"]))
    return sorted(ids)


def live_source(source_id: str, state: Optional[Mapping[str, object]]) -> Dict[str, object]:
    state = state or {}
    exists = bool(state.get("exists"))
    return {
        "source_id": source_id,
        "exists": exists,
        "title": str(state.get("title") or "") if exists else "",
        "source_type": str(state.get("source_type") or "") if exists else "",
        "processing": bool(exists and source_is_processing(state)),
    }


def public_evidence(evidence: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """A row's evidence as the API returns it: the stored evidence minus
    ``INTERNAL_EVIDENCE_KEYS`` (hidden-member-derived; read side only)."""
    return {key: value for key, value in (evidence or {}).items()
            if key not in INTERNAL_EVIDENCE_KEYS}


def page_row(
    row: Mapping[str, object],
    status: str,
    reason: Optional[str],
    dismissed_at: Optional[str],
    states: Mapping[str, Mapping[str, object]],
    built_at: object,
) -> Dict[str, Any]:
    """One row of the page in the API shape (internal codes only). The status was
    decided on the STORED evidence; the response carries `public_evidence`."""
    if status == STATUS_OPEN:
        reason = source_handled_reason(row, states, built_at)
        if reason is not None:
            status = STATUS_HANDLED
    refs = [str(ref) for ref in row["subject_refs"] or []]   # type: ignore[union-attr]
    sources = list(refs)
    if row["rule_id"] == RULE_PARSE_DAMAGE and str(row["subject_key"]) not in sources:
        sources.insert(0, str(row["subject_key"]))
    return {
        "rule_id": row["rule_id"],
        "severity": row["severity"],
        "subject": {"kind": row["subject_kind"], "key": row["subject_key"],
                    "label": row["subject_label"]},
        "subject_refs": refs,
        "evidence": public_evidence(row["evidence"]),   # type: ignore[arg-type]
        "impact": {"objects": int(row["impact_objects"])},   # type: ignore[arg-type]
        "actions": finding_actions(
            str(row["rule_id"]), str(row["subject_key"]), refs,
            row["evidence"] or {}),   # type: ignore[arg-type]
        "first_seen": {"kg_seq": int(row["first_seen_kg_seq"]),          # type: ignore[arg-type]
                       "cluster_seq": int(row["first_seen_cluster_seq"])},  # type: ignore[arg-type]
        "status": status,
        "handled_reason": reason if status == STATUS_HANDLED else None,
        "dismissed_at": dismissed_at,
        "live_sources": [live_source(sid, states.get(sid)) for sid in sources],
    }


# ---------------------------------------------------------------- summary
# ``dev_items[].status``: whether the running code already fixes the defect.
DEV_ITEM_OPEN = "open"
# The fix is deployed, the library's published merge predates it; the next
# "更新分析" applies it (S1: the library still publishes concept seed space 1
# while the code builds in space 2 — design §五 S1 「实施细化」 last item).
DEV_ITEM_FIXED_PENDING_UPDATE = "fixed_pending_update"


def dev_items(
    rules: Mapping[str, Mapping[str, object]], *,
    published_seed_version: int, code_seed_version: int,
) -> List[Dict[str, object]]:
    """The "needs development" zone: B-tier items measured to affect THIS library
    (today only S1; §3.4 — an item without measurable impact is not shown).

    ``status``: ``fixed_pending_update`` while the library's published merge
    is in an older concept seed space than the running code (its seeds still
    carry the defect; "更新分析" rebuilds it in the fixed space), else ``open``.
    Judged off ``unified_kg_state.concept_seed_version`` at read time, so it
    flips as soon as a rebuild publishes, even before the findings are
    recomputed."""
    s1 = rules.get(RULE_SEED_DEFECT) or {}
    if s1.get("status") != RULE_STATUS_RAN or not int(s1.get("narrow_groups") or 0):  # type: ignore[arg-type]
        return []
    return [{
        "item_id": RULE_SEED_DEFECT,
        "status": (DEV_ITEM_FIXED_PENDING_UPDATE
                   if int(published_seed_version) < int(code_seed_version)
                   else DEV_ITEM_OPEN),
        "groups": int(s1.get("narrow_groups") or 0),             # type: ignore[arg-type]
        "objects": int(s1.get("narrow_group_objects") or 0),     # type: ignore[arg-type]
        "samples": list(s1.get("examples") or []),               # type: ignore[arg-type]
    }]


def summary_counts(
    rules: Mapping[str, Mapping[str, object]], dismissed: int,
) -> Dict[str, object]:
    """Rows per rule, and the dismissals whose subject is in the CURRENT detail
    (dismissals outlive the detail — §8.3 — so the raw mark count is not it)."""
    return {
        "by_rule": {rule: int(status.get("persisted") or 0)   # type: ignore[arg-type]
                    for rule, status in rules.items() if "persisted" in status},
        "dismissed": int(dismissed),
    }
