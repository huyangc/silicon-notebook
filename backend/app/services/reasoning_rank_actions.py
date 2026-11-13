"""Concept ranking actions behind a narrow live run view (design §5)."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from app.core.ask_retrieval_policy import AskRetrievalLimits
from app.domain.concept_ranking_contracts import (
    CONCEPT_RANK_MAX_CHAINS,
    CONCEPT_RANK_PAGE_SIZE,
    RANK_REASON_RANK_FAILED,
    ranking_failure_instruction,
)
from app.domain.retrieval_control import RetrievalControlError
from app.models.ask import TraceStep
from app.services.cancellation import AskCancelled, CancelEvent
from app.services.concept_ranking import (
    ConceptRankComputation,
    ConceptRankingService,
    ConceptRankPage,
    ConceptRankRequest,
    ConceptRankRow,
    LibraryRankCoverage,
    LibraryRanking,
    RankCursorError,
    RankCursorStaleError,
    RankRequestError,
)
from app.services.concept_ranking_paging import (
    encode_rank_cursor,
    rank_fingerprint,
    rank_page_valid,
)

RANK_CONCEPTS_ACTION = "rank_concepts"


@dataclass(frozen=True)
class RankActionDependencies:
    active: Callable[[AskRetrievalLimits, frozenset], bool]
    service: Callable[[], ConceptRankingService]
    retrieval: Callable[[], object]
    ceiling_binds: Callable[[], bool]
    cancel_event: Callable[[], CancelEvent]
    fail_closed: Callable[[], bool]
    display_text: Callable[[object], str]


class RankRunView(Protocol):
    enum_limits: AskRetrievalLimits
    notebook_id: str
    rank_calls_used: int
    rank_progress: int
    rank_handles: Dict[tuple, str]
    rank_chains: Dict[str, "RankChain"]
    rank_memo: dict
    rank_log: List[str]
    rankings: List["ConceptRankingOutcome"]
    record: Callable[[TraceStep], None]


@dataclass
class RankChain:
    fingerprint: str
    query: str
    scope: str
    last_page: ConceptRankPage
    pages: Dict[int, ConceptRankPage]
    chain_id: str = ""
    request_key: str = ""
    successful_pages: Tuple[ConceptRankPage, ...] = ()


@dataclass(frozen=True)
class ConceptRankingOutcome:
    fingerprint: str
    query: str
    scope: str
    computation: ConceptRankComputation
    last_page_rows: Tuple[ConceptRankRow, ...]
    fetched_pages: Tuple[int, ...]
    handles: Dict[Tuple[str, str], str]
    successful_pages: Tuple[ConceptRankPage, ...] = ()


class ReasoningRankActions:
    """Own rank admission, per-run budget, stable handles and the reflect ledger."""

    def __init__(self, dependencies: RankActionDependencies) -> None:
        self._dependencies = dependencies

    def _rank_spent(self, state: RankRunView) -> frozenset:
        if not self._dependencies.active(state.enum_limits, frozenset()):
            return frozenset()
        return (frozenset({RANK_CONCEPTS_ACTION})
                if self._rank_left(state) == 0 else frozenset())

    @staticmethod
    def _rank_left(state: RankRunView) -> Optional[int]:
        cap = state.enum_limits.rank_calls_per_run
        return max(0, cap - state.rank_calls_used) if cap > 0 else None

    @staticmethod
    def _rank_skip(state: RankRunView, summary: str, reason: str,
                   note: str, **detail) -> None:
        state.rank_log.append(note)
        state.record(TraceStep(step_type="skip", summary=summary,
                               detail={"reason": reason, **detail}))

    def _rank_failed(self, state: RankRunView, exc: Exception,
                     request: ConceptRankRequest | None = None) -> None:
        # Phase B rework §9: ordinary whole-call errors are user-visible
        # outcomes even for strict callers; control exceptions never enter here.
        request = request or ConceptRankRequest()
        request_key = rank_fingerprint(request.query, request.scope, (state.notebook_id,))
        chain = self._rank_request_chain(state, request_key)
        fingerprint = chain.fingerprint if chain else request_key
        computation = ConceptRankComputation(
            fingerprint, request.query.strip(), request.scope,
            (LibraryRanking(LibraryRankCoverage(
                notebook_id=state.notebook_id, notebook_name="", status="unavailable",
                reason=RANK_REASON_RANK_FAILED), ()),), (), "")
        self._rank_publish(state, ConceptRankPage(computation, 0, CONCEPT_RANK_PAGE_SIZE, ""), request_key)
        # The chain owns its latest failure; do not keep a second stale failure
        # in rank_log after a successful retry replaces this outcome.
        state.record(TraceStep(
            step_type="skip", summary="跳过概念排行（执行失败）",
            detail={"reason": "rank_concepts_error", "error_type": type(exc).__name__}))

    @staticmethod
    def _rank_request_chain(state: RankRunView, request_key: str) -> RankChain | None:
        return next((chain for chain in state.rank_chains.values()
                     if chain.request_key == request_key), None)

    def _rank_publish(self, state: RankRunView, page: ConceptRankPage, request_key: str) -> None:
        computation = page.computation
        rows = page.page_rows()
        for row in rows:
            key = (row.notebook_id, row.canonical_id)
            if key not in state.rank_handles:
                state.rank_handles[key] = f"c{len(state.rank_handles) + 1}"
                state.rank_progress += 1
        chain = self._rank_request_chain(state, request_key)
        delivered = bool(rows) or not page.failed_libraries()
        previous_page = next(reversed(chain.pages.values()), None) if chain and chain.pages else None
        same_snapshot = previous_page is not None and (
            previous_page.computation.seqs == computation.seqs
            and previous_page.computation.ceiling_digest == computation.ceiling_digest
            and previous_page.computation.fingerprint == computation.fingerprint)
        pages = dict(chain.pages) if chain and (same_snapshot or not delivered) else {}
        successful_pages = chain.successful_pages if chain and (same_snapshot or not delivered) else ()
        if delivered:
            # Preserve actual delivered computations: a recovered library can
            # change quotas without changing graph identity. Insertion order
            # records the most recent successful delivery for inline evidence.
            pages.pop(page.page_number, None)
            pages[page.page_number] = page
            successful_pages = (*successful_pages, page)
        elif pages:
            rows = next(reversed(pages.values())).page_rows()
        chain_id = chain.chain_id if chain else f"c{len(state.rank_chains) + 1}"
        if chain is not None and chain.fingerprint != computation.fingerprint:
            del state.rank_chains[chain.fingerprint]
        state.rank_chains[computation.fingerprint] = RankChain(
            computation.fingerprint, computation.query, computation.scope, page, pages,
            chain_id, request_key, successful_pages)
        outcome = ConceptRankingOutcome(
            computation.fingerprint, computation.query, computation.scope,
            computation, rows, tuple(sorted(pages)), dict(state.rank_handles), successful_pages)
        for index, previous in enumerate(state.rankings):
            if previous.fingerprint in {computation.fingerprint, chain.fingerprint if chain else ""}:
                state.rankings[index] = outcome
                break
        else:
            state.rankings.append(outcome)

    @staticmethod
    def _rank_cursor(state: RankRunView, request: ConceptRankRequest) -> str:
        if not request.cursor:
            return ""
        token, separator, raw_page = request.cursor.partition(":p")
        chain = next((item for item in state.rank_chains.values() if item.chain_id == token), None)
        if not separator or chain is None or not raw_page.isdecimal():
            raise RankCursorError("unknown rank cursor handle")
        page_number = int(raw_page)
        snapshot = (next(reversed(chain.pages.values())).computation
                    if chain.pages else chain.last_page.computation)
        if not rank_page_valid(snapshot, page_number, request.page_size):
            raise RankCursorError("invalid rank cursor page")
        if chain.query != request.query.strip() or chain.scope != request.scope:
            raise RankCursorStaleError("rank cursor parameters changed")
        return encode_rank_cursor(snapshot, page_number, request.page_size)

    def _rank_ledger(self, state: RankRunView) -> str:
        if not state.rank_chains and not state.rank_log:
            return ""
        lines = ["\n概念排行账目（按提及它的来源篇数统计）："]
        for chain in state.rank_chains.values():
            libraries = chain.last_page.computation.libraries
            lines.append(f"rank_concepts query={self._dependencies.display_text(chain.query)} scope={chain.scope}")
            for library in libraries:
                coverage = library.coverage
                lines.append(
                    f"路径 {coverage.path or '未统计'}；库 {self._dependencies.display_text(coverage.notebook_name)}；"
                    f"范围来源数 {coverage.scope_sources}")
                for row in chain.last_page.page_rows():
                    if row.notebook_id != coverage.notebook_id:
                        continue
                    suffix = f"（同名组 {row.same_name_group}）" if row.same_name_group else ""
                    if len(libraries) > 1:
                        suffix += f"（库 {self._dependencies.display_text(coverage.notebook_name)}）"
                    lines.append(
                        f"{state.rank_handles[(row.notebook_id, row.canonical_id)]} "
                        f"{self._dependencies.display_text(row.name)} — {row.source_count} 篇来源 / "
                        f"{row.object_count} 条{suffix}")
                marks = []
                for field in fields(coverage):
                    if field.name in {"notebook_id", "notebook_name", "path", "scope_sources", "status", "reason", "skipped"}:
                        continue
                    value = getattr(coverage, field.name)
                    if value or field.name == "exact":
                        marks.append(f"{field.name}={value}")
                if marks:
                    lines.append("覆盖：" + ", ".join(marks))
                if coverage.status != "ok" or coverage.skipped:
                    lines.append(ranking_failure_instruction(
                        "library_skipped" if coverage.skipped else coverage.reason))
            if chain.last_page.next_cursor:
                lines.append(f"继续翻页 cursor={chain.chain_id}:p{chain.last_page.page_number + 1}")
        lines.extend(state.rank_log)
        lines.append(f"rank_concepts 剩余 {self._rank_left(state)} 次（翻页也计次）。")
        return "\n".join(lines) + "\n"

    def _action_rank_concepts(self, state: RankRunView, decision) -> None:
        if not self._dependencies.active(state.enum_limits, self._rank_spent(state)):
            self._rank_skip(
                state, "跳过概念排行（本次不提供或额度已用完）", "rank_concepts_unavailable",
                "rank_concepts 本次不可用（额度已用完或未提供），请根据已有排行作答")
            return
        request = ConceptRankRequest(
            query=decision.rank_concepts_query,
            scope=decision.rank_concepts_scope or "all",
            cursor=decision.rank_concepts_cursor)
        request_key = rank_fingerprint(request.query, request.scope, (state.notebook_id,))
        if (not request.cursor and self._rank_request_chain(state, request_key) is None
                and len(state.rank_chains) >= CONCEPT_RANK_MAX_CHAINS):
            self._rank_skip(state, "跳过概念排行（调用链额度已用完）", "rank_concepts_unavailable",
                            "rank_concepts 本次调用链额度已用完，请根据已有排行作答")
            return
        encoded_cursor = ""
        try:
            encoded_cursor = self._rank_cursor(state, request)
            service_request = ConceptRankRequest(
                query=request.query, scope=request.scope,
                cursor=encoded_cursor)
            page = self._dependencies.service().rank(
                state.notebook_id, service_request, retrieval=self._dependencies.retrieval(),
                ceiling_binds=self._dependencies.ceiling_binds(),
                cancel_event=self._dependencies.cancel_event(), memo=state.rank_memo)
        except RankCursorStaleError:
            self._rank_skip(
                state, "跳过概念排行（游标已失效）", "rank_cursor_stale",
                "rank_concepts.cursor 已失效：图谱已变化，请清空 cursor 从第一页重新排行（已发的句柄不变）")
            return
        except RankCursorError:
            if encoded_cursor:
                # This cursor was minted from a known run chain, so service
                # identity rejection means its participant fingerprint moved.
                self._rank_skip(
                    state, "跳过概念排行（游标已失效）", "rank_cursor_stale",
                    "rank_concepts.cursor 已失效：图谱已变化，请清空 cursor 从第一页重新排行（已发的句柄不变）")
            else:
                self._rank_skip(state, "跳过概念排行（游标无效）", "rank_cursor_invalid",
                                "rank_concepts.cursor 无效，请清空 cursor 从第一页重新排行")
            return
        except RankRequestError:
            self._rank_skip(state, "跳过概念排行（参数无效）", "rank_concepts_invalid",
                            "rank_concepts 参数无效，请修正 query、scope 后重试")
            return
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one action, not the run
            self._rank_failed(state, exc, request)
            return
        state.rank_calls_used += 1
        computation = page.computation
        rows = page.page_rows()
        self._rank_publish(state, page, request_key)
        failed = page.failed_libraries()
        state.record(TraceStep(
            step_type=RANK_CONCEPTS_ACTION,
            summary=("概念排行（翻页）" if request.cursor else "概念排行") + f"：{len(rows)} 行",
            detail={"path": "/".join(lib.coverage.path for lib in computation.libraries),
                    "query": self._dependencies.display_text(computation.query),
                    "scope": computation.scope, "listed": len(rows),
                    "returned_total": len(rows), "libraries": len(computation.libraries),
                    "status": failed[0].status if failed else "ok",
                    "reason": (failed[0].reason or failed[0].skipped) if failed else "",
                    "has_more": bool(page.next_cursor)}))
