"""In-document navigation behind a narrow live run view."""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Protocol, Sequence, Tuple

from app.core.ask_retrieval_policy import AskRetrievalLimits
from app.core.config import Settings
from app.domain.retrieval_control import RetrievalControlError
from app.models.ask import TraceStep
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.collection_enumeration import UNNAMED_SOURCE_LABEL
from app.services.reports.policy import ReasoningActionPolicy, reasoning_action_policy
from app.services.retrieval import RetrievedChunk, RetrievedElement
from app.services.retrieval_run import retrieval_fanout_slot
from app.services.section_outline import (
    UNDER_NO_NUMBERING,
    UNTITLED,
    AddressBook,
    Ambiguous,
    Outline,
    Resolved,
    SourceRanking,
    build_outline,
    filter_outline,
    parse_section_id,
    rank_sections,
    render_hints,
    render_outline,
    render_ranked,
    resolve_section,
    select_hints,
)
from app.services.section_retrieval import collect_section_vector_stats, embedded_query_texts

if TYPE_CHECKING:
    from app.repositories.ports import RetrievalPort


class SectionQuery(Protocol):
    query: str


class SectionEnumeration(Protocol):
    collection: str
    items: list


class SectionRosterNumbering(Protocol):
    issued: Dict[str, str]

    def sync(self, chains: Mapping[tuple, object]) -> SectionRosterNumbering: ...


@dataclass(frozen=True)
class SectionNavigationDependencies:
    """Existing retrieval/policy operations, read live through explicit callables."""

    settings: Callable[[], Settings]
    retrieval: Callable[[], RetrievalPort]
    cancel_event: Callable[[], CancelEvent]
    fail_closed: Callable[[], bool]
    navigation_active: Callable[[AskRetrievalLimits], bool]
    filter_chunks: Callable[[list], list]
    resolve_roster_rows: Callable[[list, str, Mapping[str, str]], list]
    cap_result_ids: Callable[[List[str]], Tuple[List[str], bool]]
    admit_chunks: Callable[[list, set, list], list]

BROWSE_SECTIONS_ACTION = "browse_sections"
READ_SECTION_ACTION = "read_section"
SECTION_QUERY_MAX_CHARS = 300
_SECTION_ECHO_CHARS = 200
ENUMERATE_SOURCES_COLLECTION = "sources"


def _section_text(value) -> str:
    """一行有界的显示文本:折叠空白,超过 `_SECTION_ECHO_CHARS` 截断并加省略号。"""
    text = " ".join(str(value or "").split())
    if len(text) <= _SECTION_ECHO_CHARS:
        return text
    return text[:_SECTION_ECHO_CHARS - 1] + "…"


def _section_source_title(issued, source_id, raw_title) -> str:
    """章节导航(目录、读取、线索块)回显的来源标题。

    无标题文档在本 run 花名册里编了号(「未命名来源（第 N 篇）」,`issued` 是 run 级
    发号簿)时回显那个号,与清单账目、已读账目同一个串——否则两篇无名文档的目录、
    读取记录都叫「未命名来源」,模型读完分不清读的是哪一篇。只有一篇无名文档(不编号)
    或不是无名文档时与以前相同。
    """
    title = _section_text(raw_title or UNNAMED_SOURCE_LABEL)
    if title == UNNAMED_SOURCE_LABEL and source_id in issued:
        return _section_text(issued[source_id])
    return title


def _display_occurrence(occurrence):
    """渲染用的出现副本:标签与面包屑各段按 `_section_text` 截成有界的一行。地址簿
    里登记的仍是原出现(只有显示被截,解析与读取不受影响)。"""
    return replace(
        occurrence, label=_section_text(occurrence.label),
        crumbs=tuple(_section_text(crumb) for crumb in occurrence.crumbs))


class _SectionSource(NamedTuple):
    """文档内导航的来源候选一行(鸭子类型兼容 `_resolve_roster_rows` 读的三格)。
    `notebook_id` 恒为真实属主 id(活动库也是活动库 id)。"""

    source_id: str
    source_title: str
    notebook_id: str


class SectionRunView(Protocol):
    """Only this capability's live run fields; authority stays on the coordinator."""

    action_policy: ReasoningActionPolicy
    attempted: Mapping[str, SectionQuery]
    chunks: List[RetrievedChunk]
    elements: List[RetrievedElement]
    enum_chains: Mapping[tuple, object]
    enum_limits: AskRetrievalLimits
    enumerations: Sequence[SectionEnumeration]
    max_steps: int
    notebook_id: str
    question: str
    record: Callable[[TraceStep], None]
    roster_numbering: SectionRosterNumbering
    section_browse_cache: Dict[tuple, tuple]
    section_browses: int
    section_hint_detail: Dict[str, Any]
    section_hint_sims: Dict[tuple, tuple]
    section_listing_delivered_this_turn: bool
    section_listing_turn: int
    section_listings: Dict[tuple, str]
    section_log: List[str]
    section_outlines: Dict[tuple, 'Outline']
    section_read_keys: set
    section_reads: int
    section_titles: Dict[tuple, str]
    seen_chunks: set
    steps: int
    surfaced_sections: AddressBook
    zero_hit_by_action: Dict[str, int]


class SectionNavigationDecision(Protocol):
    """The browse/read request already parsed by reflection."""

    browse_sections_source: str
    browse_sections_under: str
    browse_sections_query: str
    read_section_source: str
    read_section_section: str


class ReasoningSectionNavigation:
    """Owns section addresses, hint ranking, listing replay and passage reads."""

    def __init__(self, dependencies: SectionNavigationDependencies) -> None:
        self._dependencies = dependencies

    @staticmethod
    def _section_evidence_sources(state: "SectionRunView") -> "List[_SectionSource]":
        """本 run 两条原文通道里出现过的来源,首次出现序:先原文段(`state.chunks`),
        再原文元素(`state.elements`,search_elements 与首轮元素兜底)——候选摘要把
        两者的来源标题都给模型看了,所以两者都能被导航动作按标题解析。属主库一律取
        真实 id(活动库命中不带 notebook_id 时取 `state.notebook_id`),与地址簿
        「一个 run 一种口径」一致。零 I/O。"""
        rows: Dict[str, _SectionSource] = {}
        for hit in [*state.chunks, *state.elements]:
            rows.setdefault(hit.source_id, _SectionSource(
                hit.source_id, str(hit.source_title or ""),
                str(getattr(hit, "notebook_id", "") or state.notebook_id)))
        return list(rows.values())

    def _section_source_rows(self, state: "SectionRunView") -> list:
        """来源解析的候选:本 run 证据里出现过的来源(原文段与原文元素,首次出现序)
        ∪ 已列出的来源清单行。零 I/O。"""
        rows: Dict[str, _SectionSource] = {
            row.source_id: row for row in self._section_evidence_sources(state)}
        for listed in state.enumerations:
            if listed.collection != ENUMERATE_SOURCES_COLLECTION:
                continue
            for row in listed.items:
                rows.setdefault(row.source_id, _SectionSource(
                    row.source_id, str(row.source_title or ""),
                    str(getattr(row, "notebook_id", "") or state.notebook_id)))
        return list(rows.values())

    def _resolve_section_source(
        self, state: "SectionRunView", requested: str, verb: str,
    ) -> "Tuple[Optional[_SectionSource], str]":
        """模型抄来的来源标题 → 唯一一行候选,或 (None, skip 文案)。

        解析复用 `_resolve_roster_rows`(逐步精确匹配,同名序号占位串与清单同一
        发号簿)。0 篇与多篇的下一步不同,文案分叉,措辞与按篇读取同一口径。候选
        只来自本 run 已出现的来源(都已过天花板),所以「越界」与「不存在」在这里
        本来就是同一件事:不在候选里。
        """
        issued = state.roster_numbering.sync(state.enum_chains).issued
        matches = self._dependencies.resolve_roster_rows(
            self._section_source_rows(state), str(requested or ""), issued)
        if len(matches) > 1:
            # 本库与参考库同名:优先本库那一篇(与花名册「本库优先」的读法一致);
            # 要读参考库那篇,抄花名册给它的「（第 N 篇）」占位串。
            local = [row for row in matches if row.notebook_id == state.notebook_id]
            if len(local) == 1:
                matches = local
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            return None, (f"跳过{verb}:候选与来源清单里有 {len(matches)} 篇同名文档,"
                          "按标题无法区分,请改用章节线索里的 [sN] 或换一篇")
        return None, (f"跳过{verb}:本轮候选与来源清单里没有匹配这个标题的文档,"
                      "请从候选或清单里逐字复制标题后重试")

    def _section_outline_for(
        self, state: "SectionRunView", owner: str, source_id: str,
        title: str = "",
    ) -> "Outline":
        """这个来源的大纲,本 run 只读一次(地址簿的出现序号依赖它不变)。

        与 `search_chunks` 同路:I/O 在 `retrieval_fanout_slot()` 里(这是单库叶子
        读取,对等 run 也一样)。权限在检索层(越界与不存在都返回空大纲)。"""
        key = (owner, source_id)
        if title and key not in state.section_titles:
            state.section_titles[key] = title
        cached = state.section_outlines.get(key)
        if cached is None:
            with retrieval_fanout_slot():
                rows, truncated = self._dependencies.retrieval().section_outline(owner, source_id)
            cached = build_outline(rows, truncated)
            state.section_outlines[key] = cached
        return cached

    def _section_similarities(
        self, owner: str, source_id: str, chunk_ids, query_texts,
    ) -> Dict[str, float]:
        """`RetrievalPort.section_chunk_similarities` 的包装(扇出槽同 search_chunks)。"""
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return {}
        with retrieval_fanout_slot():
            return dict(self._dependencies.retrieval().section_chunk_similarities(
                owner, source_id, ids, list(query_texts)))

    def _section_similarities_parallel(
        self, requests: "List[Tuple[str, str, List[str]]]", texts,
    ) -> "Dict[Tuple[str, str], Dict[str, float]]":
        """线索块的多来源相似度:`(库, 来源, chunk ids)` 各一次端口调用,并发跑
        (冷读整源向量是 I/O 加解码,串行时 4 个来源的冷读全压在首次 reflect 之前)。

        有界线程池(至多 4 路);每个任务用 `contextvars.copy_context().run` 继承
        检索 run(扇出槽、run 内记忆)、来源天花板与统计作用域;扇出槽仍在
        `_section_similarities` 里逐次取。取消:提交前与收结果时各查一次,任务内的
        端口等待也会查;第一个异常原样抛给调用方(失败开放在 `_section_hint_block`)。"""
        if not requests:
            return {}
        raise_if_cancelled(self._dependencies.cancel_event())
        if len(requests) == 1:
            owner, source_id, ids = requests[0]
            return {(owner, source_id): self._section_similarities(
                owner, source_id, ids, texts)}
        with ThreadPoolExecutor(max_workers=min(len(requests), 4)) as pool:
            futures = {
                (owner, source_id): pool.submit(
                    contextvars.copy_context().run,
                    self._section_similarities, owner, source_id, ids, texts)
                for owner, source_id, ids in requests}
            results = {}
            for key, future in futures.items():
                results[key] = future.result()
                raise_if_cancelled(self._dependencies.cancel_event())
        return results

    @staticmethod
    def _take_section_hint_detail(state: "SectionRunView") -> Dict[str, Any]:
        """本轮 reflect 步 detail 的稀疏 `section_hints` 键(没算线索时为空),取走即清。"""
        taken = dict(state.section_hint_detail)
        state.section_hint_detail = {}
        return {"section_hints": taken} if taken else {}

    def _section_hint_block(self, state: "SectionRunView") -> str:
        """章节向量线索块(§3.7),惰性计算,**失败开放**(出不来就不出块、不记 skip)。

        * 来源:本 run 证据里首次出现的前 `REASONING_SECTION_HINT_SOURCES` 个来源,
          只取大纲真实出现 ≥ 2 的;
        * 查询文本:问题 + 已执行的查询(`attempted`,含跑过的已确认方向),查询向量走检索层
          的 run 内记忆;相似度按 (来源, 查询文本) 缓存在 state 上,新增子查询才重算,
          chunk 向量由检索层按 (库, 来源) 记忆、不重复解码;
        * 排除本 run 已有 chunk 覆盖的节与导航节(`rank_sections`;导航出现不进大纲);
        * 相对阈值的基准 = 本 run 已有 chunk 在同一把尺子下的最高相似度。没有任何已有
          chunk 拿得到相似度(向量不可用)时没有基准,整块不出。基准来自**已覆盖**的
          高分块,而线索只列**未覆盖**的节(天然更低分),所以默认比例取 0.5;噪声靠
          名次上限 `REASONING_SECTION_HINT_MAX_ENTRIES` 控制。
        """
        state.section_hint_detail = {}
        # read_section 不提供或次数已用完:线索没有可用的下一步,不出块也不重算。
        if not state.chunks or not self._section_read_left(state):
            return ""
        max_entries = int(getattr(self._dependencies.settings(), "reasoning_section_hint_max_entries", 0) or 0)
        max_sources = int(getattr(self._dependencies.settings(), "reasoning_section_hint_sources", 0) or 0)
        if max_entries <= 0 or max_sources <= 0:
            return ""
        try:
            # 只计本次线索计算(含它开的线程)的读取:run 级累计被报告并行章节共享。
            with collect_section_vector_stats() as hint_reads:
                text, sources_used, entries = self._section_hints_text(
                    state, max_entries, max_sources)
            delta = hint_reads.snapshot()
            # 稀疏:只在这一轮真的读了/借了向量,或线索块非空时写(冻结金样里
            # 闸关、没算或全命中 run 内记忆的轮次一个键都不多)。
            if text or delta.get("vector_loads") or delta.get("process_cache_hits"):
                state.section_hint_detail = {
                    "sources": sources_used, "entries": entries,
                    "vectors_read": delta.get("vectors_read", 0),
                    "vector_loads": delta.get("vector_loads", 0),
                    "process_cache_hits": delta.get("process_cache_hits", 0),
                    "vector_ms": delta.get("vector_ms", 0),
                }
            return text
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception:  # noqa: BLE001 — 线索是提示,不是必需品(失败开放)
            if self._dependencies.fail_closed():
                raise
            return ""

    def _section_hints_text(
        self, state: "SectionRunView", max_entries: int, max_sources: int,
    ) -> "Tuple[str, int, int]":
        """`_section_hint_block` 的计算体(异常由调用方失败开放):
        `(线索块文本, 参与排序的来源数, 列出的条数)`。"""
        # 与来源解析同一份候选、同一个首次出现序(原文段,再原文元素)。
        issued = state.roster_numbering.sync(state.enum_chains).issued
        sources: Dict[tuple, str] = {
            (row.notebook_id, row.source_id): _section_source_title(
                issued, row.source_id, row.source_title)
            for row in self._section_evidence_sources(state)[:max_sources]}
        # 只用本 run 已经嵌入过的文本(问题 + 已执行的查询),且只取检索 run 的嵌入
        # 记忆里已有向量的那些(`embedded_query_texts`):线索块不新增嵌入调用;未执行
        # 的已确认方向还没有贡献过任何证据。
        texts = tuple(embedded_query_texts(self._dependencies.settings(), (
            str(text or "") for text in (
                [state.question]
                + [entry.query for entry in state.attempted.values()]))))
        run_ids = {chunk.chunk_id for chunk in state.chunks}
        outlines: List[Tuple[str, str, str, "Outline"]] = []
        for (owner, source_id), title in sources.items():
            source_outline = self._section_outline_for(state, owner, source_id, title)
            if source_outline.section_count >= 2:
                outlines.append((owner, source_id, title, source_outline))
        recompute = [
            (owner, source_id, [cid for occ in source_outline.occurrences
                                for cid in occ.chunk_ids])
            for owner, source_id, _title, source_outline in outlines
            if (state.section_hint_sims.get((owner, source_id)) or (None,))[0] != texts]
        for key, sims in self._section_similarities_parallel(recompute, texts).items():
            state.section_hint_sims[key] = (texts, sims)
        rankings: List[SourceRanking] = []
        best = None
        for owner, source_id, title, source_outline in outlines:
            sims = state.section_hint_sims[(owner, source_id)][1]
            for chunk_id in run_ids:
                if chunk_id in sims and (best is None or sims[chunk_id] > best):
                    best = sims[chunk_id]
            rankings.append(SourceRanking(
                owner, source_id, title, tuple(rank_sections(
                    source_outline, sims, exclude_chunk_ids=run_ids,
                    limit=max_entries))))
        if best is None:
            return "", len(rankings), 0
        hints = select_hints(
            rankings, max_entries=max_entries,
            relative_floor=float(getattr(
                self._dependencies.settings(), "reasoning_section_hint_relative_floor", 0.5)),
            best_chunk_sim=best)
        # 标签按显示口径截(地址簿里存的仍是原出现):条目数已有上限,每行也有界。
        return render_hints([
            (state.surfaced_sections.assign(hint.notebook_id, hint.source_id,
                                            hint.occurrence),
             replace(hint, occurrence=_display_occurrence(hint.occurrence)))
            for hint in hints]), len(rankings), len(hints)

    def _section_skip(
        self, state: "SectionRunView", reason: str, summary: str,
        *, feed: bool = True, **detail,
    ) -> None:
        """导航动作的 skip:零命中写法(不写 `result_ids`),教学措辞同时进账目。
        账目行带「未执行:」前缀,与「已浏览/已读」的执行记录分开。"""
        if feed:
            state.section_log.append("未执行:" + summary.removeprefix("跳过"))
        state.record(TraceStep(step_type="skip", summary=summary,
                               detail={"reason": reason, **detail}))

    def _section_actions_offered(
        self, section_nav: bool, spent: "frozenset[str]" = frozenset(),
    ) -> "Tuple[bool, bool]":
        """(browse_sections, read_section) 各自提供不提供:总闸开、上限 > 0、本 run
        没用完。prompt / schema / 白名单三处读这一个结论。"""
        policy = reasoning_action_policy(self._dependencies.settings())
        return (
            bool(section_nav and policy.max_section_browses > 0
                 and BROWSE_SECTIONS_ACTION not in spent),
            bool(section_nav and policy.max_section_reads > 0
                 and READ_SECTION_ACTION not in spent),
        )

    @staticmethod
    def _section_spent(state: "SectionRunView") -> "frozenset[str]":
        """本 run 已用完次数的导航动作(上限为 0 的不算「用完」,那由闸自己关)。"""
        return frozenset(
            name for name, used, cap in (
                (BROWSE_SECTIONS_ACTION, state.section_browses,
                 state.action_policy.max_section_browses),
                (READ_SECTION_ACTION, state.section_reads,
                 state.action_policy.max_section_reads))
            if cap > 0 and used >= cap)

    def _section_read_left(self, state: "SectionRunView") -> bool:
        """本轮还能不能 read_section(总闸开、上限 > 0、没用完)。"""
        return self._section_actions_offered(
            self._dependencies.navigation_active(state.enum_limits),
            self._section_spent(state))[1]

    def _section_listing_note(self, state: "SectionRunView") -> str:
        """上一轮新交付了章节目录时,替代「无新证据」提示的那一句;否则空串。"""
        if state.section_listing_turn != state.steps - 1:
            return ""
        return (
            "（系统提示:上一步列出了章节目录(目录不是证据,所以没有新增证据)。"
            + ("可用 read_section 按目录里的 [sN] 读取需要的章节原文,"
               if self._section_read_left(state) else
               "可按目录里文档自己的章节用词改写检索,")
            + "或改用其他检索动作。）")

    def _section_failed(
        self, state: "SectionRunView", reason: str, verb: str, exc: Exception,
    ) -> None:
        """一次导航 I/O 失败收成 skip(与按篇读取的失败同形,codex #724 R5):
        本 run 已收集的证据与轨迹不因一次读失败作废;只记异常类名。"""
        self._section_skip(
            state, reason,
            f"跳过{verb}:读取时出错({type(exc).__name__}),本次没有结果;"
            "请改用别的章节或检索动作",
            error=type(exc).__name__)

    def _action_browse_sections(
        self, state: "SectionRunView", decision: SectionNavigationDecision,
    ) -> None:
        """reflect 的 `browse_sections`(§3.8):列出一篇来源的章节目录,不进证据。

        `query` 非空时按 §3.7 的向量打分在该来源(及 `under` 范围)内按名次列出;
        为空时按文档序列出章节树。目录进下一轮 reflect 摘要(每个来源只留最近一次),
        列出的地址进 `surfaced_sections`;本轮新交付了目录则 stale 持平。同参数重复
        浏览零 I/O 地把缓存的目录放回摘要(不扣次数、不持平 stale)。读取失败收成
        `section_browse_failed`(扣一次),整次提问不作废。skip 都不写 `result_ids`。"""
        verb = "查看章节目录"
        if not self._dependencies.navigation_active(state.enum_limits):
            # 纵深防御:闸关时动作不在 allowed_actions 里,正常路径到不了这里。
            self._section_skip(state, "section_nav_disabled",
                               f"跳过{verb}(未启用)", feed=False)
            return
        if state.steps >= state.max_steps:
            # 纵深防御:最后一轮 browse 不在 allowed_actions 里。目录无人消费,
            # 不扣次数、不进账目(账目同样只给下一轮看)。
            self._section_skip(state, "section_browse_last_turn",
                               f"跳过{verb}(已是最后一轮,目录无人消费)", feed=False)
            return
        cap = state.action_policy.max_section_browses
        if state.section_browses >= cap:
            self._section_skip(state, "section_browse_cap",
                               f"跳过{verb}(已达次数上限 {cap})")
            return
        row, unresolved = self._resolve_section_source(
            state, decision.browse_sections_source, verb)
        if row is None:
            self._section_skip(
                state, "section_source_unresolved", unresolved,
                requested_title=decision.browse_sections_source[:_SECTION_ECHO_CHARS])
            return
        under = " ".join(decision.browse_sections_under.split())[:_SECTION_ECHO_CHARS]
        query = " ".join(decision.browse_sections_query.split())[:SECTION_QUERY_MAX_CHARS]
        browse_key = (row.notebook_id, row.source_id, under.casefold(), query.casefold())
        title = _section_source_title(
            state.roster_numbering.sync(state.enum_chains).issued,
            row.source_id, row.source_title)
        if browse_key in state.section_browse_cache:
            self._replay_section_listing(state, row, title, browse_key, under, query)
            return
        counted = False
        try:
            source_outline = self._section_outline_for(
                state, row.notebook_id, row.source_id, title)
            if not source_outline.occurrences:
                self._section_skip(state, "section_outline_empty",
                                   f"跳过{verb}:《{title}》没有可列出的章节")
                return
            view = filter_outline(source_outline, under)
            if not view.under_supported:
                self._section_skip(
                    state, "section_under_unsupported",
                    f"跳过{verb}:《{title}》的章节没有编号,under 不可用,请留空 under 或改用 query"
                    if view.status == UNDER_NO_NUMBERING else
                    f"跳过{verb}:under「{under}」不是章节编号,请填写如 5.7 这样的编号或留空",
                    under=under)
                return
            if not view.entries:
                # 不用空目录覆盖同一来源上一份目录,也不扣次数。
                self._section_skip(
                    state, "section_under_empty",
                    f"跳过{verb}:《{title}》里没有编号在 {under} 之下的章节,"
                    "请换一个编号或留空 under", under=under)
                return
            state.section_browses += 1
            counted = True
            self._deliver_section_listing(
                state, row, title, view.entries, under, query, browse_key)
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one failed read must not void the run
            # 失败也扣一次(不论败在读大纲还是读向量),同参数重试受上限约束。
            if not counted:
                state.section_browses += 1
            self._section_failed(state, "section_browse_failed", verb, exc)

    def _replay_section_listing(
        self, state: "SectionRunView", row: "_SectionSource", title: str,
        browse_key: tuple, under: str, query: str,
    ) -> None:
        """同参数的第二次浏览:把缓存的目录重新放回摘要(成为该来源最近一份),
        零 I/O、不扣次数;它不是新信息,所以不持平 stale。"""
        listing, listed, scope = state.section_browse_cache[browse_key]
        key = (row.notebook_id, row.source_id)
        state.section_listings.pop(key, None)
        state.section_listings[key] = listing
        state.section_log.append(f"已浏览《{title}》{scope}(本轮已列过,目录已放回)")
        state.record(TraceStep(
            step_type="browse_sections",
            summary=f"查看章节目录:《{title}》{scope},列出 {listed} 条(本轮已列过)",
            detail={"source": title, "under": under, "query": query,
                    "listed": listed, "repeat": True}))

    def _deliver_section_listing(
        self, state: "SectionRunView", row: "_SectionSource", title: str,
        entries, under: str, query: str, browse_key: tuple,
    ) -> None:
        """渲染并交付一次目录:按名次(有 query 且拿得到相似度)或按文档序。

        地址只发给真正列出来的那一段前缀(先按预计号渲染,再按同一顺序发号——
        渲染保留的是一段前缀,所以两者逐个相同),未列出的条目不进封闭集合。"""
        key = (row.notebook_id, row.source_id)
        source_outline = state.section_outlines[key]
        ranked = []
        if query:
            sims = self._section_similarities(
                row.notebook_id, row.source_id,
                [cid for occ in entries for cid in occ.range_chunk_ids], [query])
            ranked = [item.occurrence for item in rank_sections(
                source_outline, sims, entries=entries)]
        listed_entries = ranked or list(entries)
        fresh = 0
        provisional = []
        for occ in listed_entries:
            sid = state.surfaced_sections.sid_of(row.notebook_id, row.source_id, occ.index)
            if not sid:
                fresh += 1
                sid = f"s{len(state.surfaced_sections) + fresh}"
            provisional.append((sid, occ))
        render = render_ranked if ranked else render_outline
        rendered = render(
            [(sid, _display_occurrence(occ)) for sid, occ in provisional],
            max_chars=int(getattr(self._dependencies.settings(), "reasoning_section_browse_max_chars", 3000)),
            truncated_scan=source_outline.truncated,
            scan_rows=source_outline.scanned_rows)
        for _sid, occ in provisional[:rendered.listed]:
            state.surfaced_sections.assign(row.notebook_id, row.source_id, occ)
        scope = (f" under {under}" if under else "") + (
            f" 按「{query}」排序" if ranked else
            f" 「{query}」(向量不可用,按文档顺序)" if query else "")
        listing = (
            f"（章节目录:《{title}》{scope},列出 {rendered.listed} 条;目录不是证据"
            + (",用 read_section 按 [sN] 读取原文" if self._section_read_left(state)
               else "")
            + "）\n" + rendered.text)
        state.section_listings.pop(key, None)
        state.section_listings[key] = listing
        state.section_browse_cache[browse_key] = (listing, rendered.listed, scope)
        state.section_listing_delivered_this_turn = True
        state.section_listing_turn = state.steps
        state.section_log.append(f"已浏览《{title}》{scope}(列出 {rendered.listed} 条)")
        state.record(TraceStep(
            step_type="browse_sections",
            summary=f"查看章节目录:《{title}》{scope},列出 {rendered.listed} 条",
            detail={"source": title, "under": under, "query": query,
                    "listed": rendered.listed}))

    def _resolve_section_address(
        self, state: "SectionRunView", decision: SectionNavigationDecision,
    ):
        """`read_section.section` → `(SectionAddress, 来源标题)`,或 `(None, (原因码,
        skip 文案, detail))`。§3.5:`sN` 只认本 run 发过的号;标题/编号文本在该来源
        的(本 run 只建一次的)大纲里解析——唯一即采用,多处优先含本 run 已有 chunk
        的那一处,仍不唯一则回喂各处的 `sN`。地址是封闭集合,不经过 exact_probe_terms。"""
        verb = "按章节读取原文"
        text = " ".join(decision.read_section_section.split())
        echo = text[:_SECTION_ECHO_CHARS]
        sid = parse_section_id(text)
        if sid or not text:
            address = state.surfaced_sections.get(sid) if sid else None
            if address is None:
                return None, ("section_not_surfaced",
                              f"跳过{verb}:「{echo}」不是本轮列出过的章节地址,"
                              "请从章节线索或 browse_sections 的目录里复制 [sN]",
                              {"section": echo})
            return address, state.section_titles.get(
                (address.notebook_id, address.source_id), "")
        row, unresolved = self._resolve_section_source(
            state, decision.read_section_source, verb)
        if row is None:
            return None, ("section_source_unresolved", unresolved, {
                "requested_title": decision.read_section_source[:_SECTION_ECHO_CHARS]})
        title = _section_text(row.source_title or UNNAMED_SOURCE_LABEL)
        source_outline = self._section_outline_for(
            state, row.notebook_id, row.source_id, title)
        found = resolve_section(
            source_outline, text, {chunk.chunk_id for chunk in state.chunks})
        if isinstance(found, Resolved):
            state.surfaced_sections.assign(
                row.notebook_id, row.source_id, found.occurrence)
            return state.surfaced_sections.get(state.surfaced_sections.sid_of(
                row.notebook_id, row.source_id, found.occurrence.index)), title
        if isinstance(found, Ambiguous):
            return None, self._ambiguous_section_skip(
                state, row, title, echo, found.candidates, verb)
        return None, ("section_not_surfaced",
                      f"跳过{verb}:《{title}》的目录里没有「{echo}」,"
                      "请先用 browse_sections 列出目录再复制 [sN]",
                      {"section": echo})

    def _ambiguous_section_skip(
        self, state: "SectionRunView", row: "_SectionSource", title: str,
        echo: str, candidates, verb: str,
    ) -> tuple:
        """同名多处的教学 skip:候选按目录的有界渲染器列出(预算
        `REASONING_SECTION_BROWSE_MAX_CHARS`,与 browse 同一份已校验的 Settings),
        只给**显示出来**的候选发 `sN`(先按预计号渲染,再按同一顺序发号),其余如实
        注明省略数。这行文本会留在账目里、每轮重放,所以必须有界。"""
        fresh = 0
        provisional = []
        for occ in candidates:
            sid = state.surfaced_sections.sid_of(row.notebook_id, row.source_id, occ.index)
            if not sid:
                fresh += 1
                sid = f"s{len(state.surfaced_sections) + fresh}"
            provisional.append((sid, occ))
        rendered = render_ranked(
            [(sid, _display_occurrence(occ)) for sid, occ in provisional],
            max_chars=int(getattr(self._dependencies.settings(), "reasoning_section_browse_max_chars", 3000)),
            truncated_scan=False, scan_rows=0)
        for _sid, occ in provisional[:rendered.listed]:
            state.surfaced_sections.assign(row.notebook_id, row.source_id, occ)
        # 渲染器在截断时把它自己的省略说明放在最后一行;这里换成同名场景的措辞,
        # 列表本身改成一行(账目是一行一条)。
        lines = rendered.text.split("\n")
        if rendered.omitted:
            lines = lines[:-1]
        listed = "、".join(line for line in lines if line)
        omitted = (f";另有 {rendered.omitted} 处同名未列出,"
                   "可用 browse_sections 的 under/query 收窄" if rendered.omitted else "")
        return ("section_ambiguous",
                f"跳过{verb}:「{echo}」在《{title}》里出现 {len(candidates)} 处,"
                f"请改用其中一个 [sN]:{listed}{omitted}",
                {"section": echo, "matches": len(candidates),
                 "listed": rendered.listed})

    def _action_read_section(
        self, state: "SectionRunView", decision: SectionNavigationDecision,
    ) -> None:
        """reflect 的 `read_section`(§3.9):把一个已展示过的章节整节读回证据池。

        取数 `RetrievalPort.section_chunks`(权限、归属核验、导航页剔除、与普通
        chunk 检索同一把尺子的 relevance、`section_read` 标志都在检索层),再过
        `_filter_candidates("chunk", …)`,按文档序 `take_distinct_chunk_hits` 并进
        `state.chunks`,不开新证据号段。整节 chunk id 交给端口,由端口按上限截取
        (导航页不占名额);本节可读段数超过上限时 trace 注明截取。读取失败(解析
        大纲或读回整节)收成 `section_read_failed`(扣一次),整次提问不作废。"""
        verb = "按章节读取原文"
        if not self._dependencies.navigation_active(state.enum_limits):
            self._section_skip(state, "section_nav_disabled",
                               f"跳过{verb}(未启用)", feed=False)
            return
        cap = state.action_policy.max_section_reads
        if state.section_reads >= cap:
            self._section_skip(state, "section_read_cap",
                               f"跳过{verb}(已达次数上限 {cap})")
            return
        try:
            address, title_or_skip = self._resolve_section_address(state, decision)
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one failed read must not void the run
            state.section_reads += 1
            self._section_failed(state, "section_read_failed", verb, exc)
            return
        if address is None:
            reason, summary, detail = title_or_skip
            self._section_skip(state, reason, summary, **detail)
            return
        title = _section_source_title(
            state.roster_numbering.sync(state.enum_chains).issued,
            address.source_id, title_or_skip)
        label = _section_text(address.label or UNTITLED)
        read_key = (address.notebook_id, address.source_id, address.occurrence)
        if read_key in state.section_read_keys:
            self._section_skip(
                state, "section_read_repeat",
                f"跳过{verb}:《{title}》 {label} 本轮已经读过,请改读别的章节或直接作答")
            return
        state.section_reads += 1
        state.section_read_keys.add(read_key)
        limit = int(getattr(self._dependencies.settings(), "reasoning_section_read_max_chunks", 12))
        try:
            with retrieval_fanout_slot():
                got = self._dependencies.retrieval().section_chunks(
                    address.notebook_id or state.notebook_id, address.source_id,
                    list(address.chunk_ids), limit, state.question)
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - one failed read must not void the run
            # 次数已在上面扣过、这一节进了防重键:同一节的持续性失败不反复重试。
            self._section_failed(state, "section_read_failed", verb, exc)
            return
        new = self._dependencies.admit_chunks(
            self._dependencies.filter_chunks(got), state.seen_chunks, state.chunks)
        state.chunks.extend(new)
        state.zero_hit_by_action["read_section"] = (
            0 if new else state.zero_hit_by_action.get("read_section", 0) + 1)
        # 范围内的 chunk 已剔除导航页(大纲折叠时),所以「本节可读段数 > 上限」
        # 就是端口截取了。另一种不完整与上限无关:大纲扫描在这一节里停下
        # (`SectionAddress.incomplete`),地址里只有扫到的前缀——不论走线索、
        # 标题还是 sN 进来,都如实报未完。
        cut = len(address.chunk_ids) > limit
        scan_cut = address.incomplete
        result_ids, result_ids_truncated = self._dependencies.cap_result_ids([c.chunk_id for c in new])
        detail = {"source": title, "section": label, "found": len(new),
                  "result_ids": result_ids}
        if result_ids_truncated:
            detail["result_ids_truncated"] = True
        if cut or scan_cut:
            detail["truncated"] = True
        if scan_cut:
            detail["scan_truncated"] = True
        scanned = getattr(state.section_outlines.get(
            (address.notebook_id, address.source_id)), "scanned_rows", 0)
        tail = (f"(截取本节前 {limit} 段)" if cut else "") + (
            f"(大纲只覆盖本篇前 {scanned} 段,本节之后可能还有内容)" if scan_cut else "")
        state.section_log.append(f"已读《{title}》 {label}(新增 {len(new)} 段){tail}")
        state.record(TraceStep(
            step_type="read_section",
            summary=f"按章节读取原文:《{title}》 {label},新增 {len(new)} 段{tail}",
            detail=detail))
