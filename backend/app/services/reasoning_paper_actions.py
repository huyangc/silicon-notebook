"""Paper-tool execution behind a narrow live run view."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from app.core.ask_retrieval_policy import AskRetrievalLimits
from app.domain.retrieval_control import RetrievalControlError
from app.models.ask import TraceStep
from app.services.cancellation import AskCancelled, CancelEvent
from app.services.paper_extraction import (
    EXTRACT_MAX_SOURCES,
    ExtractionRequestError,
    normalize_fields,
)
from app.services.reasoning_paper_tools import (
    EXTRACT_FROM_SOURCES_ACTION,
    FIND_SOURCES_ACTION,
    GET_PAPER_CARDS_ACTION,
    PAPER_TOOL_ACTIONS,
    FoundSourcesOutcome,
    PaperCardOutcome,
    PaperExtractionItem,
    PaperExtractionOutcome,
    PaperHandles,
    ReasoningPaperTools,
    cap_find_terms,
    card_anchor_ids,
    card_has_content,
    clip,
    extraction_value_count,
    note_top_level_arguments,
    paper_tools_note,
    resolve_source_tokens,
    year_value,
)
from app.services.reports.policy import ReasoningActionPolicy
from app.services.source_finder import (
    FIND_SOURCES_MAX_TERMS,
    FIND_SOURCES_PAGE_SIZE,
    FindSourcesCursorError,
    FindSourcesCursorScopeChangedError,
    FindSourcesCursorStaleError,
    FindSourcesError,
    FindSourcesRequest,
)


@dataclass(frozen=True)
class PaperActionDependencies:
    """Live policy/identity reads and existing leaf services; no run owner."""

    flags: Callable[[AskRetrievalLimits, frozenset], Tuple[bool, bool, bool]]
    source_scope_pairs: Callable[[str], frozenset]
    ceiling_binds: Callable[[], bool]
    tools: Callable[[], ReasoningPaperTools]
    cancel_event: Callable[[], CancelEvent]
    fail_closed: Callable[[], bool]
    actor_id: Callable[[], str]
    cap_result_ids: Callable[[List[str]], Tuple[List[str], bool]]
    display_text: Callable[[object], str]


class PaperRunView(Protocol):
    """Only this capability's live run fields; authority stays on the coordinator."""

    action_policy: ReasoningActionPolicy
    answer_layout_requested: str
    enum_limits: AskRetrievalLimits
    extract_sources_used: int
    find_sources_calls: int
    notebook_id: str
    paper_allowed: Optional[frozenset]
    paper_card_generations: int
    paper_card_reads: int
    paper_cards: Dict[tuple, 'PaperCardOutcome']
    paper_deferred_this_turn: bool
    paper_extracted_keys: set
    paper_extractions: List['PaperExtractionOutcome']
    paper_finds: List['FoundSourcesOutcome']
    paper_handles: PaperHandles
    paper_log: List[str]
    paper_progress: int
    record: Callable[[TraceStep], None]


class PaperActionDecision(Protocol):
    """The three paper requests already parsed by reflection."""

    find_sources_query: str
    find_sources_terms: Tuple[str, ...]
    find_sources_year_from: str
    find_sources_year_to: str
    find_sources_venue: str
    find_sources_author: str
    find_sources_kind: str
    find_sources_scope: str
    find_sources_cursor: str
    paper_card_sources: Tuple[str, ...]
    extract_sources: Tuple[str, ...]
    extract_fields: Tuple[str, ...]
    paper_arguments_top_level: str


class ReasoningPaperActions:
    """Owns paper admission, budgets, execution and ledger rendering."""

    def __init__(self, dependencies: PaperActionDependencies) -> None:
        self._dependencies = dependencies

    def _paper_tools_spent(self, state: "PaperRunView") -> "frozenset[str]":
        """本轮不再提供的论文工具:本 run 已用完档位额度的。三个都没开时恒空(调用形状
        不变)。来源集合为空**不**在这里关闸:本库一篇没勾、只勾了参考库是合法的
        范围,而参与库的并集要读库才知道;执行体对空集合各自如实回报(find_sources
        0 篇并带覆盖说明,卡片/抽取记「不在本次检索范围内」)。"""
        if not any(self._dependencies.flags(state.enum_limits, frozenset())):
            return frozenset()
        # 上限为 0 的不算「用完」(那由闸自己关),与 `_section_spent` 同一口径。
        left = self._paper_budget_left(state)
        return frozenset(
            name for name in PAPER_TOOL_ACTIONS
            if left[name] is not None and left[name] <= 0)

    @staticmethod
    def _paper_budget_left(state: "PaperRunView") -> Dict[str, Optional[int]]:
        """三个论文工具与卡片生成各自本 run 还剩多少(档位上限为 0 的为 None)。"""
        def left(cap: int, used: int) -> Optional[int]:
            return None if cap <= 0 else max(0, cap - used)

        return {
            FIND_SOURCES_ACTION: left(
                state.enum_limits.find_sources_calls_per_run, state.find_sources_calls),
            GET_PAPER_CARDS_ACTION: left(
                state.enum_limits.paper_card_reads_per_run, state.paper_card_reads),
            EXTRACT_FROM_SOURCES_ACTION: left(
                state.enum_limits.extract_sources_per_run, state.extract_sources_used),
            "generations": max(0, state.enum_limits.paper_card_generations_per_run
                               - state.paper_card_generations),
        }

    def _paper_allowed(self, state: "PaperRunView") -> frozenset:
        """本 run 的来源天花板 ``(属主库, source_id)``,首次需要时读一次:与
        ``enumerate_sources`` / ``find_sources`` 同一来源集合
        (``CollectionEnumerationService.source_scope_pairs``,同一份 run 判词)。"""
        if state.paper_allowed is None:
            state.paper_allowed = self._dependencies.source_scope_pairs(state.notebook_id)
        return state.paper_allowed

    @staticmethod
    def _paper_has_results(state: "PaperRunView") -> bool:
        return (any(card_has_content(o) for o in state.paper_cards.values())
                or any(extraction_value_count(o) for o in state.paper_extractions))

    def _paper_skip(self, state: "PaperRunView", summary: str, reason: str,
                    note: str, **detail) -> None:
        """论文工具的 skip:零命中写法(不写 ``result_ids``),教学措辞同时进账目。"""
        state.paper_log.append(note)
        state.record(TraceStep(step_type="skip", summary=summary,
                               detail={"reason": reason, **detail}))

    def _paper_tool_failed(self, state: "PaperRunView", action: str,
                           exc: Exception) -> None:
        """服务抛出非取消、非请求错误的异常:严格调用方照抛,其余记 skip 继续。"""
        if self._dependencies.fail_closed():
            raise exc
        self._paper_skip(
            state, "跳过论文工具(执行失败)", "paper_tool_error",
            f"{action} 这次执行失败({type(exc).__name__}),可以换一个动作或直接作答",
            action=action, error_type=type(exc).__name__)

    def _paper_tools_ledger(self, state: "PaperRunView") -> str:
        """论文工具账目(``paper_tools_note``)加本 run 剩余额度;没用过任何论文工具、
        也没写过 answer_layout 的 run 返回空串。"""
        if not (state.paper_finds or state.paper_cards or state.paper_extractions
                or state.paper_log or state.answer_layout_requested):
            return ""
        offer_find, offer_cards, offer_extract = self._dependencies.flags(state.enum_limits, frozenset())
        left = self._paper_budget_left(state)
        remaining: Dict[str, str] = {}
        if offer_find:
            remaining[FIND_SOURCES_ACTION] = f"{left[FIND_SOURCES_ACTION]} 次"
        if offer_cards:
            remaining[GET_PAPER_CARDS_ACTION] = (
                f"{left[GET_PAPER_CARDS_ACTION]} 篇(其中最多再生成 {left['generations']} 次)")
        if offer_extract:
            remaining[EXTRACT_FROM_SOURCES_ACTION] = f"{left[EXTRACT_FROM_SOURCES_ACTION]} 篇"
        return paper_tools_note(
            finds=state.paper_finds, find_calls=len(state.paper_finds),
            cards=state.paper_cards, extractions=state.paper_extractions,
            log=state.paper_log, remaining=remaining,
            layout_requested=state.answer_layout_requested,
            has_results=self._paper_has_results(state))

    def _resolved_paper_sources(self, state: "PaperRunView", action: str,
                                tokens) -> "Optional[tuple]":
        """句柄 / source_id → 本 run 天花板内的 ``ResolvedSource`` 列表,与被拒的条目
        一起返回;天花板读失败时记 skip 并返回 None。"""
        try:
            allowed = self._paper_allowed(state)
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one action, not the run
            self._paper_tool_failed(state, action, exc)
            return None
        resolved, rejected = resolve_source_tokens(tokens, state.paper_handles, allowed)
        if rejected:
            unknown = [r.token for r in rejected if r.reason == "unknown_handle"]
            outside = [r.token for r in rejected if r.reason == "out_of_scope"]
            parts = []
            if unknown:
                parts.append(f"句柄 {'、'.join(unknown)} 不是本次 find_sources 发过的号")
            if outside:
                parts.append(f"{'、'.join(outside)} 不在本次检索范围内(只能用 find_sources "
                             "给的句柄或范围内文档的 source_id)")
            state.paper_log.append(
                f"{action} 有 {len(rejected)} 个来源没有受理:" + ";".join(parts))
        return allowed, resolved, rejected

    def _action_find_sources(self, state: "PaperRunView", decision: PaperActionDecision) -> None:
        """reflect 的 `find_sources`:一页(20 篇)按主题排好序的文档,发放句柄。
        翻页(cursor)与新检索同样计入本 run 的次数;请求错误记 skip 不计次。"""
        if not self._dependencies.flags(state.enum_limits, self._paper_tools_spent(state))[0]:
            self._paper_skip(
                state, "跳过文档级检索(本次检索不提供或额度已用完)",
                "find_sources_unavailable",
                "find_sources 本次不可用(本 run 的次数已用完或未提供),请用已有句柄继续或直接作答")
            return
        note_top_level_arguments(state.paper_log, decision)
        terms, dropped = cap_find_terms(decision.find_sources_terms, FIND_SOURCES_MAX_TERMS)
        if dropped:
            # 超出上限的词截掉而不是整次拒绝(可接受偏差做兼容);MCP 直调仍由服务拒绝。
            state.paper_log.append(
                f"find_sources.terms 最多 {FIND_SOURCES_MAX_TERMS} 个,这次按前 "
                f"{FIND_SOURCES_MAX_TERMS} 个执行;没有使用:"
                + "、".join(f"「{clip(term, 32)}」" for term in dropped))
        request = FindSourcesRequest(
            query=decision.find_sources_query, terms=terms,
            year_from=year_value(decision.find_sources_year_from),
            year_to=year_value(decision.find_sources_year_to),
            venue=decision.find_sources_venue, author=decision.find_sources_author,
            kind=decision.find_sources_kind, scope=decision.find_sources_scope,
            cursor=decision.find_sources_cursor, limit=FIND_SOURCES_PAGE_SIZE)
        try:
            result = self._dependencies.tools().finder.find(
                state.notebook_id, request, ceiling_binds=self._dependencies.ceiling_binds(),
                cancel_event=self._dependencies.cancel_event())
        except FindSourcesCursorScopeChangedError:
            # 参数相同、来源集合在两页之间变了(来源被加入或删除):同样从第一页重来,但原因要说对。
            self._paper_skip(
                state, "跳过文档级检索(来源集合已变化,需从第一页重来)",
                "find_sources_cursor_stale",
                "find_sources.cursor 已失效:翻页之间笔记本的来源有增删,排序已经变化;"
                "请清空 cursor、用同样的条件从第一页重新检索"
                "(已发过的句柄不变,重新列出的文档沿用原句柄)")
            return
        except FindSourcesCursorStaleError:
            # 元数据刚被抽取(本 run 的 get_paper_cards 或别处),排序已变:旧偏移会漏篇或重复。
            self._paper_skip(
                state, "跳过文档级检索(元数据已变化,需从第一页重来)",
                "find_sources_cursor_stale",
                "find_sources.cursor 已失效:翻页之间论文元数据有更新(刚抽取了元数据),"
                "排序已经变化;请清空 cursor、用同样的条件从第一页重新检索"
                "(已发过的句柄不变,重新列出的文档沿用原句柄)")
            return
        except FindSourcesCursorError as exc:
            self._paper_skip(
                state, "跳过文档级检索(翻页游标无效)", "find_sources_cursor_invalid",
                f"find_sources.cursor 无效({exc}):翻页时只改 cursor、其余参数与上一次完全"
                "相同;要换条件就清空 cursor 重新检索")
            return
        except FindSourcesError as exc:
            self._paper_skip(
                state, "跳过文档级检索(参数不合法)", "find_sources_invalid",
                f"find_sources 参数不合法:{exc}")
            return
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one action, not the run
            self._paper_tool_failed(state, FIND_SOURCES_ACTION, exc)
            return
        state.find_sources_calls += 1
        handles = []
        for item in result.items:
            handle, issued = state.paper_handles.issue(
                item.notebook_id, item.source_id, item.title)
            handles.append(handle)
            state.paper_progress += int(issued)
        state.paper_finds.append(FoundSourcesOutcome(request, result, tuple(handles)))
        state.record(TraceStep(
            step_type="find_documents",
            summary=(("文档级检索(翻页)" if request.cursor else "文档级检索")
                     + f":强相关 {result.strong} 篇、相关 {result.related} 篇"),
            # ``listed`` 不是轨迹计数键:列出文档与列目录一样不算证据命中/零命中。
            detail={"query": self._dependencies.display_text(request.query),
                    "terms": [self._dependencies.display_text(t) for t in request.terms],
                    "kind": request.kind, "listed": len(result.items),
                    "strong": result.strong, "related": result.related,
                    "scope_total": result.filtered_scope_total,
                    "lexical_complete": result.lexical_complete,
                    "semantic_recall_capped": result.semantic_recall_capped,
                    "has_more": bool(result.next_cursor)}))

    def _action_get_paper_cards(self, state: "PaperRunView", decision: PaperActionDecision) -> None:
        """reflect 的 `get_paper_cards`:读缓存的论文卡片,缺失或过期的在本 run 的
        生成额度内懒生成。每篇来源先按本 run 的来源天花板校验;已有最终结果的篇不重复
        请求;单次 ≤10 篇、本 run 的读取篇数与生成次数按档位计。"""
        if not self._dependencies.flags(state.enum_limits, self._paper_tools_spent(state))[1]:
            self._paper_skip(
                state, "跳过论文卡片(本次检索不提供或额度已用完)", "paper_cards_unavailable",
                "get_paper_cards 本次不可用(本 run 的篇数已用完或未提供)")
            return
        note_top_level_arguments(state.paper_log, decision)
        checked = self._resolved_paper_sources(
            state, GET_PAPER_CARDS_ACTION, decision.paper_card_sources)
        if checked is None:
            return
        allowed, resolved, rejected = checked
        final = ("ready", "generated", "not_paper", "failed", "not_found")
        held = [r for r in resolved if r.key in state.paper_cards
                and state.paper_cards[r.key].result.status in final]
        wanted = [r for r in resolved if r not in held]
        take = min(state.action_policy.max_paper_cards_per_call,
                   state.enum_limits.paper_card_reads_per_run - state.paper_card_reads)
        over, wanted = wanted[take:], wanted[:take]
        if held:
            state.paper_log.append(
                "get_paper_cards:" + "、".join(r.label for r in held) + " 已有结果,未重复请求")
        if over:
            state.paper_log.append(
                f"get_paper_cards 单次最多 {state.action_policy.max_paper_cards_per_call} 篇、"
                "本 run 篇数有限:" + "、".join(r.label for r in over) + " 这次没有请求")
        if not wanted:
            if held and not over:
                # 要的都已经有结果了:不是「没填对来源」,别把模型教去重填句柄。
                self._paper_skip(
                    state, "跳过论文卡片(请求的来源都已有结果)", "paper_cards_repeat",
                    "get_paper_cards 这次请求的来源都已经有结果,不必再请求;可以对卡片缺的"
                    "字段用 extract_from_sources,或直接作答")
                return
            reason = ("paper_cards_out_of_scope" if rejected and not resolved
                      else "paper_cards_nothing_to_read")
            self._paper_skip(
                state, "跳过论文卡片(没有可读取的来源)", reason,
                "get_paper_cards 这次没有要读的来源:sources 填 find_sources 给的句柄(p1…)",
                rejected=len(rejected))
            return
        generate_left = max(0, state.enum_limits.paper_card_generations_per_run
                            - state.paper_card_generations)
        try:
            results = self._dependencies.tools().cards.get_cards(
                [r.key for r in wanted], allowed=allowed, generate_limit=generate_left,
                cancel_event=self._dependencies.cancel_event(), actor_id=self._dependencies.actor_id())
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one action, not the run
            self._paper_tool_failed(state, GET_PAPER_CARDS_ACTION, exc)
            return
        # 推迟的篇不计读取篇数:下次再请求它时才算(同一篇不会被扣两次)。
        state.paper_card_reads += sum(1 for r in results if r.status != "deferred")
        state.paper_card_generations += sum(int(getattr(r, "calls", 0) or 0) for r in results)
        by_key = {r.key: r for r in wanted}
        statuses: Dict[str, int] = {}
        anchor_ids: List[str] = []
        for result in results:
            source = by_key[(result.notebook_id, result.source_id)]
            outcome = PaperCardOutcome(source.handle, source.title, result)
            before = state.paper_cards.get(source.key)
            if card_has_content(outcome) and (before is None or not card_has_content(before)):
                state.paper_progress += 1
            # 键已在时原位替换(插入序 = 首次请求序)。
            state.paper_cards[source.key] = outcome
            statuses[result.status] = statuses.get(result.status, 0) + 1
            if card_has_content(outcome):
                anchor_ids.extend(card_anchor_ids(result.card))
        ids, truncated = self._dependencies.cap_result_ids(list(dict.fromkeys(anchor_ids)))
        found = statuses.get("ready", 0) + statuses.get("generated", 0)
        deferred = statuses.get("deferred", 0)
        state.paper_deferred_this_turn |= deferred > 0
        detail = {"requested": len(wanted), "generated": statuses.get("generated", 0),
                  "deferred": deferred, "statuses": statuses, "result_ids": ids}
        if deferred < len(results):
            # 全部推迟时**不写** found:那不是「查了没有」,经验投影不该把它记成零命中。
            detail["found"] = found
        if truncated:
            detail["result_ids_truncated"] = True
        state.record(TraceStep(
            step_type="paper_cards",
            summary=(f"读取论文卡片:{len(wanted)} 篇,得到 {found} 张"
                     + (f"(新生成 {statuses['generated']} 张)" if statuses.get("generated") else "")),
            detail=detail))

    def _action_extract_from_sources(self, state: "PaperRunView", decision: PaperActionDecision) -> None:
        """reflect 的 `extract_from_sources`:对每篇来源抽取至多 4 个临时字段(不缓存、
        不看是否论文)。来源先按本 run 的来源天花板校验;单次 ≤8 篇、本 run 的篇数按
        档位计(真正发起了模型调用的篇才计)。"""
        if not self._dependencies.flags(state.enum_limits, self._paper_tools_spent(state))[2]:
            self._paper_skip(
                state, "跳过逐篇提取(本次检索不提供或额度已用完)", "extract_unavailable",
                "extract_from_sources 本次不可用(本 run 的篇数已用完或未提供)")
            return
        note_top_level_arguments(state.paper_log, decision)
        try:
            fields = normalize_fields(decision.extract_fields)
        except ExtractionRequestError as exc:
            self._paper_skip(state, "跳过逐篇提取(字段不合法)", "extract_invalid",
                             f"extract_from_sources.fields 不合法:{exc}")
            return
        checked = self._resolved_paper_sources(
            state, EXTRACT_FROM_SOURCES_ACTION, decision.extract_sources)
        if checked is None:
            return
        allowed, resolved, rejected = checked
        # 同一篇、同一组字段本 run 已经抽过:不再花额度和模型调用(结果已在账目里)。
        fields_key = tuple(field_text.casefold() for field_text in fields)
        repeats = [r for r in resolved
                   if (r.notebook_id, r.source_id, fields_key) in state.paper_extracted_keys]
        if repeats:
            state.paper_log.append(
                "extract_from_sources:" + "、".join(r.label for r in repeats)
                + " 这组字段本次已经抽过,结果见上,未重复抽取")
            resolved = [r for r in resolved if r not in repeats]
            if not resolved:
                self._paper_skip(
                    state, "跳过逐篇提取(这些来源已经提取过)", "extract_repeat",
                    "extract_from_sources 这次请求的来源与字段都已经抽过,不必重复;"
                    "可以换字段、换来源,或直接作答")
                return
        per_call = min(state.action_policy.max_extract_sources_per_call, EXTRACT_MAX_SOURCES)
        over, batch = resolved[per_call:], resolved[:per_call]
        if over:
            state.paper_log.append(
                f"extract_from_sources 单次最多 {per_call} 篇:"
                + "、".join(r.label for r in over) + " 这次没有抽取")
        if not batch:
            reason = "extract_out_of_scope" if rejected else "extract_no_sources"
            self._paper_skip(
                state, "跳过逐篇提取(没有可提取的来源)", reason,
                "extract_from_sources 这次没有可抽取的来源:sources 填 find_sources 给的句柄(p1…)",
                rejected=len(rejected))
            return
        left = state.enum_limits.extract_sources_per_run - state.extract_sources_used
        try:
            results = self._dependencies.tools().extraction.extract(
                [r.key for r in batch], fields, allowed=allowed,
                limit=max(0, min(len(batch), left)),
                cancel_event=self._dependencies.cancel_event(), actor_id=self._dependencies.actor_id())
        except ExtractionRequestError as exc:
            self._paper_skip(state, "跳过逐篇提取(请求不合法)", "extract_invalid",
                             f"extract_from_sources 请求不合法:{exc}")
            return
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one action, not the run
            self._paper_tool_failed(state, EXTRACT_FROM_SOURCES_ACTION, exc)
            return
        by_key = {r.key: r for r in batch}
        items = tuple(
            PaperExtractionItem(by_key[(res.notebook_id, res.source_id)].handle,
                                by_key[(res.notebook_id, res.source_id)].title, res)
            for res in results)
        state.extract_sources_used += sum(
            1 for res in results
            if res.status != "not_found" and not (res.status == "deferred" and res.reason == "limit"))
        state.paper_extracted_keys.update(
            (res.notebook_id, res.source_id, fields_key) for res in results
            if res.status in ("extracted", "no_text"))
        outcome = PaperExtractionOutcome(tuple(fields), items)
        state.paper_extractions.append(outcome)
        found = extraction_value_count(outcome)
        state.paper_progress += found
        deferred = sum(1 for res in results if res.status == "deferred")
        state.paper_deferred_this_turn |= deferred > 0
        ids, truncated = self._dependencies.cap_result_ids(list(dict.fromkeys(
            value.element_id for res in results for value in res.fields
            if value.value and value.element_id)))
        detail = {"sources": len(batch), "fields": [self._dependencies.display_text(f) for f in fields],
                  "deferred": deferred, "result_ids": ids}
        if deferred < len(results):
            # 全部推迟时不写 found(不是零命中,见卡片那一步)。
            detail["found"] = found
        if truncated:
            detail["result_ids_truncated"] = True
        state.record(TraceStep(
            step_type="extract_fields",
            summary=f"逐篇提取 {len(fields)} 个字段:{len(batch)} 篇,提取到 {found} 个值",
            detail=detail))
