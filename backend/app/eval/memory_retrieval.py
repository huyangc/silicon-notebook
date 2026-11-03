"""Fixed two-plane Memory retrieval evaluation with deterministic fixtures."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.models.memory import MemoryHit, MemoryRecord
from app.services.embedding import FakeEmbedder
from app.services.memory_retrieval import MemoryRetriever

#: The stored statuses of an unadopted system suggestion (design 6.3: the
#: ``suggested_signal IS NOT NULL AND status IN ('candidate','rejected')``
#: SQL fragment, spelled out here because the eval has no database).
UNADOPTED_SUGGESTION_STATUSES = frozenset({"candidate", "rejected"})


def _dcg(relevances: list[int]) -> float:
    return sum(rel / math.log2(index + 2) for index, rel in enumerate(relevances))


def _returned_field(item: MemoryHit | Mapping[str, Any], name: str, default=""):
    return getattr(item, name, item.get(name, default) if isinstance(item, Mapping) else default)


def evaluate_cases(
    cases: Iterable[Mapping[str, Any]],
    k: int = 5,
    *,
    item_scope: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, float | int]:
    """Score actual ranked hits.

    Ownership/notebook isolation is checked against fixture repository metadata,
    not fields invented on ``MemoryHit`` (whose production contract deliberately
    excludes owner and notebook identity).

    The formal plane is ``("confirmed",)``: a notebook-plane hit that is not
    confirmed -- by its own ``status`` or by the stored status in ``item_scope``
    -- counts in ``candidate_to_notebook_leaks``. ``unadopted_suggestion_leaks``
    is the subset the metadata identifies as an unadopted system suggestion
    (``suggested_signal`` set, stored status ``candidate`` or ``rejected``;
    design 6.3), reported separately so a regression names its cause.

    A case may list ``distractors`` (confirmed-but-wrong memories on the same
    topic, e.g. an adopted suggestion with wrong content). It counts in
    ``distractor_displacements`` when one of them is ranked ahead of the first
    expected memory, or when no expected memory made the top ``k`` while a
    distractor did; ``distractor_cases`` is how many cases carry distractors
    (so a zero is not vacuous).
    """
    rows = list(cases)
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    candidate_leaks = cross_user = cross_notebook = 0
    unadopted_leaks = displacements = distractor_cases = 0
    scope = item_scope or {}
    for case in rows:
        expected = {str(item) for item in case.get("expected", [])}
        returned = list(case.get("returned", []))
        ranked_ids = [
            str(_returned_field(item, "memory_id")) for item in returned[:k]
        ]
        found = expected.intersection(ranked_ids)
        recalls.append(len(found) / len(expected) if expected else 1.0)
        first = next((index for index, item in enumerate(ranked_ids, 1) if item in expected), 0)
        reciprocal_ranks.append(1.0 / first if first else 0.0)
        rels = [1 if item in expected else 0 for item in ranked_ids]
        ideal = [1] * min(len(expected), k)
        denom = _dcg(ideal)
        ndcgs.append(_dcg(rels) / denom if denom else 1.0)
        distractors = {str(item) for item in case.get("distractors", [])}
        if distractors:
            distractor_cases += 1
            first_distractor = next(
                (index for index, item in enumerate(ranked_ids, 1) if item in distractors), 0
            )
            if first_distractor and (not first or first_distractor < first):
                displacements += 1
        for item in returned:
            memory_id = str(_returned_field(item, "memory_id"))
            metadata = scope.get(memory_id, {})
            if case.get("plane") == "notebook":
                stored_status = metadata.get("status") or _returned_field(item, "status")
                unadopted = bool(metadata.get("suggested_signal")) and (
                    stored_status in UNADOPTED_SUGGESTION_STATUSES
                )
                if _returned_field(item, "status") != "confirmed" or stored_status != "confirmed":
                    candidate_leaks += 1
                if unadopted:
                    unadopted_leaks += 1
            if metadata and metadata.get("created_by") != str(case.get("user_id", "")):
                cross_user += 1
            if metadata and metadata.get("notebook_id") != str(case.get("notebook_id", "")):
                cross_notebook += 1
    count = len(rows) or 1
    return {
        "recall_at_5": sum(recalls) / count,
        "mrr": sum(reciprocal_ranks) / count,
        "ndcg": sum(ndcgs) / count,
        "candidate_to_notebook_leaks": candidate_leaks,
        "unadopted_suggestion_leaks": unadopted_leaks,
        "cross_user_leaks": cross_user,
        "cross_notebook_leaks": cross_notebook,
        "distractor_cases": distractor_cases,
        "distractor_displacements": displacements,
    }


def _record(
    memory_id: str,
    user_id: str,
    notebook_id: str,
    status: str,
    title: str,
    content: str,
    suggested_signal: str | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id,
        notebook_id=notebook_id,
        created_by=user_id,
        agent_profile_id="fixture-agent",
        origin="external_agent",
        status=status,
        title=title,
        content_md=content,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        provenance={"fixture": memory_id},
        suggested_signal=suggested_signal,
    )


_FIXTURE_RECORDS = (
    _record(
        "memory-confirmed-timing", "alice", "timing", "confirmed",
        "Confirmed timing decision", "Fix setup timing closure",
    ),
    _record(
        "memory-candidate-constraint", "alice", "timing", "candidate",
        "Agent constraint candidate", "agent constraint candidate",
    ),
    _record(
        "memory-confirmed-repeat", "alice", "timing", "confirmed",
        "Avoid repeated investigation", "reuse the saved result to avoid repeated investigation",
    ),
    _record(
        "memory-alice-other", "alice", "power", "confirmed",
        "Other notebook timing", "setup timing closure",
    ),
    _record(
        "memory-bob-timing", "bob", "timing", "confirmed",
        "Other user timing", "setup timing closure",
    ),
    _record(
        "memory-bob-power", "bob", "power", "confirmed",
        "Power integrity decision", "power integrity decoupling",
    ),
    # Auto user-memory suggestions (design 7.1), in a world of their own so the
    # alice/bob rows above -- and the Agent A/B harness built on them -- keep
    # their numbers. carol/clock holds one correct manual memory, two ADOPTED
    # suggestions with deliberately wrong content on the same topic (the
    # distractors: confirmed, so they ARE in the formal plane and must merely
    # not out-rank the right memory), and two UNADOPTED suggestions whose text
    # matches the gold query best (the leak probes: candidate and rejected must
    # never reach the formal plane, so either showing up would rank first).
    _record(
        "memory-clock-correct", "carol", "clock", "confirmed",
        "Clock uncertainty guidance",
        "include clock uncertainty margin in setup analysis",
    ),
    _record(
        "memory-clock-wrong-clarified", "carol", "clock", "confirmed",
        "Clock uncertainty note",
        "clock uncertainty setup analysis can skip the margin",
        suggested_signal="clarified",
    ),
    _record(
        "memory-clock-wrong-repeated", "carol", "clock", "confirmed",
        "Clock uncertainty reminder",
        "clock uncertainty setup analysis never needs a margin",
        suggested_signal="repeated",
    ),
    _record(
        "memory-clock-suggest-candidate", "carol", "clock", "candidate",
        "Clock uncertainty suggestion",
        "include clock uncertainty margin in setup analysis suggestion",
        suggested_signal="clarified",
    ),
    _record(
        "memory-clock-suggest-rejected", "carol", "clock", "rejected",
        "Clock uncertainty dismissed suggestion",
        "include clock uncertainty margin in setup analysis dismissed",
        suggested_signal="repeated",
    ),
)


class _FixtureMemoryStore:
    """Small deterministic implementation of the production retrieval port."""

    def __init__(self, records: Sequence[MemoryRecord] = _FIXTURE_RECORDS) -> None:
        self.records = tuple(records)
        self.scope_by_id = {
            item.id: {
                "created_by": item.created_by,
                "notebook_id": item.notebook_id,
                "status": item.status,
                "suggested_signal": item.suggested_signal or "",
            }
            for item in self.records
        }

    def memory_retrieval_rows(
        self,
        user_id: str,
        notebook_id: str,
        statuses: Sequence[str],
        query: str,
        *,
        lexical_limit: int,
        vector_limit: int,
        phrase_queries: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        del query, vector_limit, phrase_queries
        allowed = set(statuses)
        return [
            {"record": item, "vector": None}
            for item in self.records
            if item.created_by == user_id
            and item.notebook_id == notebook_id
            and item.status in allowed
        ][:lexical_limit]


def _fixture_retriever() -> tuple[MemoryRetriever, Mapping[str, Mapping[str, str]]]:
    store = _FixtureMemoryStore()
    return MemoryRetriever(store, FakeEmbedder(dim=8)), store.scope_by_id


def _gold_path() -> Path:
    return Path(__file__).with_name("memory_gold.yaml")


def run_fixed_gold(path: str | Path | None = None) -> dict[str, float | int]:
    gold_path = Path(path) if path else _gold_path()
    # JSON is a strict YAML subset and avoids a runtime dependency for this
    # offline safety gate.
    payload = json.loads(gold_path.read_text(encoding="utf-8"))
    retriever, scope = _fixture_retriever()
    executed = []
    for raw_case in payload.get("cases", []):
        case = dict(raw_case)
        if case.get("plane") == "agent":
            hits = retriever.agent_memory_hits(
                str(case["user_id"]),
                str(case["notebook_id"]),
                str(case["query"]),
                bool(case.get("include_candidates", False)),
                5,
            )
        else:
            hits = retriever.notebook_memory_hits(
                str(case["user_id"]),
                str(case["notebook_id"]),
                str(case["query"]),
                5,
            )
        case["returned"] = hits
        executed.append(case)
    return evaluate_cases(executed, k=5, item_scope=scope)


if __name__ == "__main__":  # pragma: no cover - manual evaluation entrypoint
    print(json.dumps(run_fixed_gold(), ensure_ascii=False, indent=2))
