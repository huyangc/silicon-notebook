"""Ask 模式引擎与答案合成 (Task 24) — chunk/reasoning 双引擎、follow-up
改写、refine/retry/mix 合成 helper、未配模型短路、index-required 装饰与答案落存
的唯一所有者。SQLiteRepository 只保留冻结签名 delegate。

组合规则 (Gate 8):
* 引擎只持窄端口 —— ask_state(prepare_turn_for_job/begin_durable_job/
  save_answer_for_job/finish_job,Task 22)、
  retrieval(candidates+graph,Task 21)、evidence_context(上下文/锚点/引用/
  tier,Task 21)、model_clients/model_errors(RuntimeModelProvider,一个所有
  者，测试仅通过显式 workload 绑定模型替身)、communities 工厂(逐次新建,
  sibling_min_bridge 在调用时读取——镜像 ReportEngine 的 per-launch 构造)、
  scale_profiles 工厂 + scale_index_probe(_needs_index 逐调用现读
  _vector_cache,保 facade 换缓存测试语义)、notebooks(存在性守卫)、
  schemas(effective_schemas)、source_titles。
* 持久化身份显式:``user_id`` 关键字由调用方传入(facade delegate 适配
  current_user().id;流式路径由 AskExecutionCoordinator 每次 start 传入),
  本模块绝不读请求 ContextVar、绝不 import facade/runtime、绝不开私有 DB 缝。
* 模型身份走注入的 process-owned provider；每个调用点使用稳定 workload ID
  解析只读 adapter，不读取请求用户的模型配置。
* 两模式派发仍以 ask_modes.ASK_MODES 冻结注册表为唯一真源(getattr 派发 +
  fast/global/graph 退役别名);控制流与 facade 基线逐字一致 —— ask goldens
  (test_ask_repository_golden)按字节冻结着每条路径。
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import (
    Any, Callable, Dict, Iterable, List, Optional, Sequence, TYPE_CHECKING,
)

if TYPE_CHECKING:
    from app.application.ask_reasoning import ResponseDraftStage
    from app.repositories.ports import (
        AskCandidatePort,
        AskModelClientProvider,
        PreparedAskTurn,
        RetrievalPort,
    )

from app.core.ask_context import _ASK_EMBED_CACHE, _ASK_MODEL_ERRORS
from app.core.ask_retrieval_policy import RetrievalEffort, ask_retrieval_limits
from app.core.config import Settings
from app.core.llm import cap_kwargs
from app.domain.gap_consult import (
    GAP_CONSULT_MAX_GAP_PHRASES,
    GAP_CONSULT_MAX_SUGGESTIONS,
    GAP_CONSULT_PHRASE_MAX_CHARS,
    GAP_CONSULT_QUESTION_MAX_CHARS,
    GapConsultCallContext,
    GapConsultQuery,
    GapSuggestion,
    gap_consult_host_is_dormant,
)
from app.models.ask import (
    TRACE_ANCHOR_EVIDENCE_IDS_MAX,
    AnswerAnchor,
    AskGapSuggestion,
    AskRequest,
    AskResponse,
    Citation,
    ConversationBulkDeleteResult,
    ModelError,
    QueryIntentContract,
    TraceStep,
)
from app.models.knowledge import (
    KnowledgeFieldValue,
    KnowledgeRecord,
)
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.citation_markers import LOOSE_MARKER_RE, MARKER_RE, marker_keys
from app.services.evidence_context import anchor_image_targets
from app.services.model_work import ModelNotConfiguredError
from app.services.prompts import (
    ANSWER_SCHEMA_HINT,
    FOLLOWUP_REWRITE_SCHEMA_HINT,
    answer_prompt,
    followup_rewrite_prompt,
)
from app.services.retrieval import (
    RetrievedKnowledge,
    classify_evidence,
    is_generated_question_only_chunk,
    merge_retrieval_supports,
    prefer_stronger_chunk_candidate,
)
from app.services.search_profile import render_style_block
from app.services.source_graph_activation import (
    UNCONFIGURED_SELECTED_SOURCE_GRAPH_CAPABILITIES,
    SelectedSourceGraphContributionCall,
    selected_evidence_lane_is_dormant,
    selected_source_graph_call_context,
)
from app.services.source_scope import source_scope_context, source_scope_restricted
from app.services.source_element_selection import (
    rank_source_elements,
    source_chunk_content_key,
)

# Matches both one provenance marker and the comma-group form models commonly
# emit (`[k1, k3]`). A group binds only when every key exists in id_map.
_MARKER_GROUP_RE = MARKER_RE

# Tolerant variant that ALSO matches malformed markers with internal whitespace
# (e.g. `[ k1]`). Used only to scrub citation-shaped tokens that did NOT bind to
# a real anchor, so no fabricated/malformed marker reaches the user. Kept
# separate from _MARKER_GROUP_RE so strict anchor resolution is unchanged.
_LOOSE_MARKER_GROUP_RE = LOOSE_MARKER_RE

_NO_RETRIEVAL_EVIDENCE_MESSAGE = (
    "当前检索没有找到足以支撑回答的来源证据。资料可能已经导入，"
    "但本次没有命中；请尝试补充文章标题、关键词或原文中的术语后重试。"
)


def _merge_multi_direct_chunk_hits(collected: dict, direct_hits) -> None:
    """Fold historical direct hits into a multi-query candidate mapping.

    A question-only row may have the higher optional score, but it must never
    remain the canonical object once a historical keyword/exact producer finds
    that chunk.  Keeping the direct object also leaves the old question-only
    per-query row untouched, so quota fusion cannot use its optional score to
    reorder the baseline.  Collisions between historical producers retain the
    previous best-relevance behavior while preserving every provenance.
    """
    by_content = {
        source_chunk_content_key(chunk): chunk for chunk in collected.values()
    }
    for candidate in direct_hits:
        current = collected.get(candidate.chunk_id)
        if current is None:
            current = by_content.get(source_chunk_content_key(candidate))
        if current is None:
            collected[candidate.chunk_id] = candidate
            by_content[source_chunk_content_key(candidate)] = candidate
            continue
        chosen = prefer_stronger_chunk_candidate(current, candidate)
        if chosen is not current:
            # Reinsert at this direct producer's position.  Assigning over the
            # old key would retain the optional row's earlier dict position and
            # change stable tie ordering versus a feature-off direct stream.
            collected.pop(current.chunk_id, None)
            collected[chosen.chunk_id] = chosen
        by_content[source_chunk_content_key(chosen)] = chosen


def _merge_direct_chunk_hits(candidates: list, direct_hits) -> list:
    """Fold keyword/exact hits into a list without retaining question scores.

    The historical single/mix union keeps the existing object on collision.
    That remains the rule for every historical candidate.  A question-only
    object is different: once a direct producer finds the same chunk, the
    direct object must become canonical (including its historical relevance)
    and occupy the position where that direct stream first produced it.  This
    makes feature-on selection identical to feature-off selection while still
    preserving the optional provenance marker.
    """
    if not direct_hits:
        return candidates
    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    by_content = {
        source_chunk_content_key(candidate): candidate for candidate in candidates
    }
    out = list(candidates)
    for candidate in direct_hits:
        current = by_id.get(candidate.chunk_id)
        if current is None:
            current = by_content.get(source_chunk_content_key(candidate))
        if current is None:
            out.append(candidate)
            by_id[candidate.chunk_id] = candidate
            by_content[source_chunk_content_key(candidate)] = candidate
            continue
        supports = merge_retrieval_supports(
            current.retrieval_supports, candidate.retrieval_supports
        )
        if is_generated_question_only_chunk(current):
            out.remove(current)
            candidate.retrieval_supports = supports
            out.append(candidate)
            by_id.pop(current.chunk_id, None)
            by_id[candidate.chunk_id] = candidate
            by_content[source_chunk_content_key(candidate)] = candidate
        else:
            # Preserve the old list-union contract: the existing historical
            # semantic/lexical object stays canonical even when a later direct
            # producer reports a larger score.
            current.retrieval_supports = supports
    return out


@dataclass
class _SectionedSynthesis:
    """按节合成成功后的整篇产物(设计文档 §3.1)。

    ``counts`` 是各节装配计数之和 —— 同一条证据被两节绑上就计两次,那是诚实的:
    它确实进了两次 prompt。
    """

    answer: str
    llm_grounded: bool
    anchors: list
    sections: int
    counts: dict
    section_grounding: list
    baseline_assemblies: list


def _keep_only_section_markers(answer: str, section_keys: set) -> str:
    """按节合成:把一节的正文里**不属于本节号段**的 `[k…]` 标记清掉。

    为什么必须在拼接**之前**做(codex PR#407 R3 P1):按节解析已经正确地不给跨节标记
    发锚点了,但正文里那个 `[k1]` 还原样留着 —— 而**前端是按合并后的引用表解析正文
    标记的**(`buildAnswerReferences` 拿 `answer.matchAll` 去查 `anchorsByKey`)。第二节
    写出的 `[k1]` 于是照样绑到第一节那条毫不相干的证据上:按节解析防住的误绑,从
    渲染这道后门原样回来了。号段偏移买到的隔离,必须在**送到读者眼前的那份文本**上
    也成立。

本函数的两处设计取舍:

    * **混合组保留合法子集**而不是整组丢弃。号段互不相交是**服务端**造的事实,
      本节模型手里从来就没有过 `k1` 这个号,它只可能是幻觉,而同组里的
      `k10001` 是服务端确确实实发给这一节、它也确确实实看见了的证据。连着好的一半
      一起丢,是拿一次幻觉惩罚一条真引用。
    * **空白收敛带 `(?<=\\S)` 前置断言**:标记按 prompt 规则 1 挂在句末,删掉只会留下
      「词__词」这种**跟在非空白字符后面**的空格串。不加断言的话,分节答案里的嵌套
      列表(`  - 项`)与四空格代码块会被一起压平 —— 那对长文比漏一个标记严重得多。

    合法标记逐字保留(顺带把 `[ k10001]` 这类畸形写法规范化成可绑定的形态)。整组
    全非法时连同方括号一起移除,所以不会留下空括号或多余逗号。
    """
    def _sub(match: re.Match) -> str:
        keys = marker_keys(match.group(0))
        kept = [key for key in keys if key in section_keys]
        return ("[" + ", ".join(kept) + "]") if kept else ""

    cleaned = _LOOSE_MARKER_GROUP_RE.sub(_sub, answer or "")
    return re.sub(r"(?<=\S)[ \t]{2,}", " ", cleaned).strip()


def knowledge_record(object_type: str, obj: dict, schema) -> KnowledgeRecord:
    """KG 对象 → KnowledgeRecord 展示投影(canonical body;facade 的
    _knowledge_record 与 ask_reasoning 的 related_knowledge 共用)。"""
    from app.services.knowledge_governance import knowledge_headline

    payload = obj.get("payload") or {}
    visible_payload_keys = [
        key for key in payload if not str(key).startswith("_")
    ]
    # A definition controls preferred field order; it must not hide persisted
    # user data when an owner later removes a field from the definition. Keep
    # any additional payload keys after the configured fields so existing
    # objects remain completely browsable and can be governed/migrated.
    keys = (
        [*schema.fields, *(
            key for key in visible_payload_keys if key not in schema.fields
        )]
        if schema
        else visible_payload_keys
    )
    fields: List[KnowledgeFieldValue] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            text = ", ".join(str(v) for v in value if str(v).strip())
        elif value is None:
            text = ""
        else:
            text = str(value)
        if text.strip():
            fields.append(KnowledgeFieldValue(key=key, value=text.strip()))
    return KnowledgeRecord(
        id=obj["id"],
        object_type=object_type,
        headline=knowledge_headline(object_type, payload),
        fields=fields,
        status=obj.get("status", "approved"),
        owner=obj.get("owner", ""),
        last_reviewed=obj.get("last_reviewed", ""),
        evidence=obj.get("evidence", []),
    )


def _egress_phrase(value: object, limit: int) -> str:
    """Bound and de-noise one string on its way OUT of the deployment.

    Citation-shaped tokens are stripped because ``[k3]`` names a server-owned
    evidence key: outside this deployment it is meaningless at best, and a
    handle worth correlating at worst.  Whitespace is collapsed so a phrase
    cannot smuggle structure (or a newline-delimited payload) past the length
    rail.
    """
    if type(value) is not str:
        return ""
    return " ".join(_LOOSE_MARKER_GROUP_RE.sub(" ", value).split())[:limit]


def _egress_question(prepared: object) -> str:
    """The single question string a gap-consult plugin gets to see.

    Exactly two candidates, in order, and both of them are wording the user
    has actually SEEN: the reviewed final question, but only when a human
    really reviewed it — the clarification gate ran (``needs_clarification``
    on the confirmed contract), where the user answers the ambiguities and can
    edit the final wording — and otherwise the raw question as typed.  A
    clear-intent Ask is auto-confirmed without pausing, so its
    ``resolved_question`` is a model rewrite (it may fold in earlier turns'
    wording) that no human ever looked at; preferring it here would egress
    model-composed text under the "reviewed question" label (codex #584 R5
    P1).  ``research_question`` is deliberately not among the candidates
    either — it is the intent contract's composite string (objective plus
    mandatory topics plus constraints plus assumptions), the same shape
    ``_uncovered_directions_from_trace`` refuses to send outward.

    Bounded by ``GAP_CONSULT_QUESTION_MAX_CHARS``, which is a privacy rail
    rather than a budget: it is the ceiling on how much of the user's own text
    leaves the deployment per consultation.

    That 300-character prefix is **egress minimization, not data loss**, and
    the distinction is the whole reason it is allowed to exist (codex #584 R1
    P2, rejected — see ``docs/superpowers/plans/2026-08-24-ask-gap-consult.md``
    for the decision).  What this builds is a *retrieval hint* handed to a
    third party; it is never stored, never rendered, and never read back.  The
    "user data must not be silently truncated" rail governs write and render
    paths — what gets persisted, and what the reader is shown — and the
    question itself is untouched by this function on every one of them: the
    full text is what the run searched with, what ``answers.question`` holds,
    and what the conversation replays.  Shortening the hint costs the plugin
    some context; lengthening it would send more of the user's words to a party
    that has no need for them.  The limit is registered in
    ``docs/product-and-api*.md``; the reverse guard that pins both halves of
    this claim together is
    ``test_gap_consult_ask_wiring.py::
    test_egress_question_truncation_is_the_privacy_bound_not_data_loss``.
    """
    projection = getattr(prepared, "intent_projection", None)
    reviewed = ""
    if projection is not None:
        # ``intent_json`` is the confirmed contract verbatim; a truthy
        # ``needs_clarification`` on it means the run could only have been
        # confirmed through the human gate (required ambiguities must be
        # answered before confirmation), so the resolved wording was reviewed.
        try:
            contract = json.loads(getattr(prepared, "intent_json", "") or "{}")
            if isinstance(contract, dict) and contract.get("needs_clarification"):
                reviewed = getattr(projection, "resolved_question", "")
        except (ValueError, TypeError):
            reviewed = ""
    for candidate in (reviewed, getattr(prepared, "question", "")):
        phrase = _egress_phrase(candidate, GAP_CONSULT_QUESTION_MAX_CHARS)
        if phrase:
            return phrase
    return ""


def _admitted_gap_suggestions(raw: object) -> tuple[AskGapSuggestion, ...]:
    """Whole-batch admission on whatever the gap-consult host answered.

    The frozen host sanitizes each contributor's output, so a well-behaved one
    can only ever answer a tuple of :class:`GapSuggestion`.  This is the check
    on the host itself — the seat is public (``gap_consult_host=`` all the way
    down the injection chain), so "the host is well behaved" is an assumption
    about an injected object, not a property of this module.

    Admission is all-or-nothing on purpose.  A batch with one wrong item is a
    host answering a shape it was never asked for, and there is nothing to
    learn from the items that happened to typecheck; keeping them would mean
    passing a partially-understood payload to the disclosure the reader sees.
    Rejection is silent by design: the batch is worth strictly less than the
    answer it accompanies, and ``count: 0`` on the trace step already says
    nothing came back.
    """
    if not isinstance(raw, tuple):
        return ()
    # The cap check comes first because it is O(1) and bounds the per-item
    # scan below: a faulty host answering an arbitrarily large tuple must not
    # get a walk over it on the Ask critical path, and a batch over the
    # documented maximum is the same "shape it was never asked for" as a
    # wrong item type — all-or-nothing applies to it too.
    if len(raw) > GAP_CONSULT_MAX_SUGGESTIONS:
        return ()
    if any(type(item) is not GapSuggestion for item in raw):
        return ()
    return tuple(
        AskGapSuggestion(
            title=item.title,
            url=item.url,
            summary=item.summary,
            source_label=item.source_label,
        )
        for item in raw
    )


def _uncovered_directions_from_trace(trace: object) -> tuple[str, ...]:
    """Confirmed directions this run finished without ever executing.

    Read from the terminal disclosure step the retriever writes after its
    reflect loop — the only place that answers "still uncovered when the run
    *ended*" rather than "uncovered when the seeding budget ran out"
    (``run()`` deliberately recomputes it there for exactly that reason).

    What comes back is the registry's short LABEL for each direction, never
    the direction itself: an ``uncovered_intent_queries`` element is the
    direction concatenated with the whole confirmed intent contract, and that
    composite must not leave the deployment.  Labels are already bounded
    upstream; this re-bounds them anyway, because an egress rail that depends
    on its producer's rail is one refactor away from not being a rail.

    The scan runs BACKWARDS and stops at the first hit, so the LAST such step
    wins.  "Terminal" is the whole semantics of this read: a trace carrying
    more than one of these steps is one where a later, more accurate account of
    what stayed uncovered supersedes an earlier one, and taking the earliest
    (or the union) would ask a third party about directions the run went on to
    execute after all.
    """
    from app.services.reasoning_retrieval import (
        INTENT_COVERAGE_INCOMPLETE_REASON,
    )

    terminal = None
    for step in reversed(tuple(trace or ())):
        if getattr(step, "step_type", "") != "skip":
            continue
        detail = getattr(step, "detail", None)
        if not isinstance(detail, dict):
            continue
        if detail.get("reason") == INTENT_COVERAGE_INCOMPLETE_REASON:
            terminal = detail
            break
    if terminal is None:
        return ()
    gaps: list[str] = []
    seen: set[str] = set()
    directions = terminal.get("directions")
    for item in directions if isinstance(directions, (list, tuple)) else ():
        phrase = _egress_phrase(item, GAP_CONSULT_PHRASE_MAX_CHARS)
        if phrase and phrase not in seen:
            seen.add(phrase)
            gaps.append(phrase)
    return tuple(gaps[:GAP_CONSULT_MAX_GAP_PHRASES])


class DefaultResponseDraftStage:
    """The shipped ``ResponseDraftStage``: AskService's own synthesis segment.

    Thin on purpose.  Making the ``retrieval evidence -> response draft`` step
    an *object* is what makes it injectable -- a test or a future deployment
    hands ``AskService`` a different ``draft_response`` and the orchestrator
    is unchanged -- while the default body stays a method of the service it
    has always been part of.  That keeps the move out of
    ``_run_reasoning_stage`` a pure relocation instead of a rewrite of
    ``self`` across 650 lines of synthesis and citation binding.

    It holds the service only; it owns no repository, connection, budget,
    cancellation authority or persistence port of its own, and it cannot
    reach the atomic save -- ``_commit_reasoning_draft`` runs after this seam
    returns, in the orchestrator.
    """

    __slots__ = ("_service",)

    def __init__(self, service: "AskService") -> None:
        self._service = service

    def draft_response(self, stage, runtime):
        return self._service._draft_reasoning_response(stage, runtime)


class AskService:
    # mix 合成的 KG 段编号基点 / prompt 结构预留 token(与 facade 冻结常量同值)。
    _MIX_KG_KEY_BASE = 1000
    _MIX_PROMPT_BUFFER_TOKENS = 2000
    _MEMORY_KEY_BASE = 3000
    _ELEMENT_KEY_BASE = 4000
    _COLLECTION_KEY_BASE = 5000

    def __init__(
        self,
        *,
        ask_state,
        retrieval: "RetrievalPort",
        candidates: "AskCandidatePort",
        evidence_context,
        model_clients: "AskModelClientProvider",
        model_errors,
        communities: Callable[[], Any],
        scale_profiles: Callable[[], Any],
        scale_index_probe: Callable[[str], bool],
        settings: Settings,
        event_log,
        notebooks,
        schemas,
        source_titles: Callable[[List[str]], Dict[str, str]],
        knowhow_store=None,
        memory_retriever=None,
        current_user_id: Callable[[], str] = lambda: "",
        cancellations=None,
        collection_catalog=None,
        collection_enumeration=None,
        agent_profile=None,
        retrieval_experiences=None,
        identity_store=None,
        selected_source_graph=None,
        retrieval_contributors=None,
        retrieval_connection_probe=None,
        retrieval_contributor_hydrate: Callable[
            [str, str, Any], Any
        ] = lambda _notebook_id, _actor_id, _ids: (),
        scale_version: Callable[[str], Any] = lambda _notebook_id: None,
        selected_graph_hydrate: Callable[[Any], Any] = lambda _ids: (),
        response_draft_stage: "ResponseDraftStage | None" = None,
        gap_consult_host=None,
        ask_engine_host=None,
        ask_engine_participant_notebooks: Callable[[str], Sequence[str]] = (
            lambda notebook_id: (notebook_id,)
        ),
        ask_engine_visible_sources: Callable[[str], Sequence[str]] = (
            lambda _notebook_id: ()
        ),
        ask_engine_hidden_sources: Callable[[str, str], Sequence[str]] = (
            lambda _notebook_id, _actor_id: ()
        ),
    ) -> None:
        self.ask_state = ask_state
        self.retrieval = retrieval
        self.candidates = candidates
        self.evidence_context = evidence_context
        self.model_clients = model_clients
        self.model_errors = model_errors
        self.communities = communities
        self.scale_profiles = scale_profiles
        self.scale_index_probe = scale_index_probe
        self.settings = settings
        self.event_log = event_log
        self.notebooks = notebooks
        self.schemas = schemas
        self.source_titles = source_titles
        self.knowhow_store = knowhow_store
        self.memory_retriever = memory_retriever
        self.current_user_id = current_user_id
        self.cancellations = cancellations
        # 逐步推理的类型化集合地图/清单服务。缺省 None:窄测试替身与未接线的
        # 组合根照旧可构造,只是那条 run 不提供枚举工具。
        self.collection_catalog = collection_catalog
        self.collection_enumeration = collection_enumeration
        # Agentic Memory P1:Agent 对该库的已有理解 store(``AgentProfileStorePort``)。
        # 缺省 None ⇒ 那条 run 与接入前逐字相同(见 ``profile_wiring_active``)。
        self.agent_profile = agent_profile
        # Agentic Memory P2:部署级全局的检索打法库
        # (``RetrievalExperienceStorePort``)。同样缺省 None,而且注入还另有一把
        # 默认**关闭**的开关 —— 两者任一不满足,这条 run 与接入前逐字相同
        # (见 ``experience_wiring_active``)。
        self.retrieval_experiences = retrieval_experiences
        # Agentic Memory P3(B-Profile,T8):``IdentityStorePort``,用于按本次
        # 提问的 user_id 点读该用户的检索/回答风格偏好文档(``search_profile_json``)
        # 并渲染进 answer_prompt 的 ``style_block`` —— 与 agent_profile/
        # retrieval_experiences 同款:缺省 None ⇒ 那条 run 与接入前逐字相同
        # (见 ``reasoning_retrieval.search_profile_wiring_active``)。⚠ 报告面
        # (``report_engine.py``/``ReportEngineDependencies``)刻意**不**接这个座位
        # ——B-Profile v1 只覆盖 Ask(计划 T8 点 7),这条留空本身就是那条边界的
        # 落地方式,不需要另一把开关。
        self.identity_store = identity_store
        self.selected_source_graph = selected_source_graph
        self.retrieval_contributors = retrieval_contributors
        self.retrieval_connection_probe = retrieval_connection_probe
        self.retrieval_contributor_hydrate = retrieval_contributor_hydrate
        self.scale_version = scale_version
        self.selected_graph_hydrate = selected_graph_hydrate
        # ``retrieval evidence -> response draft`` 这一级的可注入 stage seam
        # (application 合同 ``ResponseDraftStage``)。缺省 = 历史内联逻辑本身,
        # 所以不接线的组合根与窄测试替身逐字不变;注入别的实现只替换草稿产出
        # ——持久化(``_commit_reasoning_draft``)与 job 终态仍在 core 手里。
        self.response_draft_stage = (
            response_draft_stage
            if response_draft_stage is not None
            else DefaultResponseDraftStage(self)
        )
        # X9 PR-A: the frozen ``ask.gap_consult`` host.  ``None`` (and a host
        # with no deployment plugin behind it) means this run is byte-identical
        # to the one before the point existed — see ``_consult_gap_sources``.
        self.gap_consult_host = gap_consult_host
        self.ask_engine_host = ask_engine_host
        self.ask_engine_participant_notebooks = ask_engine_participant_notebooks
        self.ask_engine_visible_sources = ask_engine_visible_sources
        self.ask_engine_hidden_sources = ask_engine_hidden_sources

    def _ask_modes(self):
        host = getattr(self, "ask_engine_host", None)
        if host is None:
            return ()
        modes = host.modes()
        if type(modes) is not tuple:
            return ()
        return tuple(mode for mode in modes if host.is_available(mode.id))

    def _resolve_ask_mode(self, mode: str | None):
        from app.services.ask_modes import resolve_mode

        return resolve_mode(mode, self._ask_modes())

    def _activate_selected_source_graph(
        self,
        notebook_id: str,
        chunks,
        *,
        top_hits=(),
        max_results: int = 20,
    ):
        """Append quality-approved G after frozen B; otherwise return B."""
        service = getattr(self, "selected_source_graph", None)
        host = getattr(self, "retrieval_contributors", None)
        connection_probe = getattr(self, "retrieval_connection_probe", None)
        if host is None or connection_probe is None:
            return list(chunks), None
        disabled_capabilities: frozenset[str] = frozenset()
        if service is None:
            # Unconfigured feature: say so up front instead of letting the host
            # rediscover it after building a context it will throw away.  When
            # nothing else contributes to this point the lane is dormant and B
            # is returned before any host work at all.
            disabled_capabilities = (
                UNCONFIGURED_SELECTED_SOURCE_GRAPH_CAPABILITIES
            )
            if selected_evidence_lane_is_dormant(host):
                return list(chunks), None
            call = SelectedSourceGraphContributionCall(
                None, notebook_id, chunks, max_results=max_results
            )
        else:
            object_seeds = {
                str(hit.object_id): float(
                    getattr(hit, "relevance", 0.0) or 0.0
                )
                for hit in top_hits
                if str(getattr(hit, "object_id", "") or "")
            }
            chunk_seeds = {
                str(chunk.chunk_id): float(
                    getattr(chunk, "relevance", 0.0) or 0.0
                )
                for chunk in chunks
                if str(getattr(chunk, "chunk_id", "") or "")
            }
            call = SelectedSourceGraphContributionCall(
                service,
                notebook_id,
                chunks,
                object_seeds=object_seeds,
                chunk_seeds=chunk_seeds,
                source_titles=self.source_titles,
                hydrate_chunk_ids=self.selected_graph_hydrate,
                parent_version=lambda: self.scale_version(notebook_id),
                max_results=max_results,
                unsafe_scope_drift=lambda: bool(
                    getattr(
                        self.retrieval,
                        "unsafe_source_scope_restricted",
                        lambda _nb: False,
                    )(notebook_id)
                ),
            )
        from app.services.retrieval_run import current_retrieval_run

        run = current_retrieval_run()
        cancel_event = run.cancel_event if run is not None else None
        try:
            call_context = selected_source_graph_call_context(
                call,
                actor_id=str(self.current_user_id() or ""),
                cancel_event=cancel_event,
                connection_probe=connection_probe,
                admission_hydrate=self.retrieval_contributor_hydrate,
                max_results=max_results,
                max_tokens=int(
                    self.settings.selected_source_graph_enrichment_tokens
                ),
            )
            host_chunks = host.run(
                chunks,
                invocation="selected_evidence",
                call_context=call_context,
                baseline_identity=lambda chunk: chunk.chunk_id,
                cancellation=call_context.cancellation,
                event_sink=getattr(
                    getattr(self, "event_log", None), "emit", None
                ),
                disabled_capabilities=disabled_capabilities,
            )
            return call.visible_result(host_chunks)
        except AskCancelled:
            raise
        except Exception:
            return call.fail_closed_result("activation_seam_failed")

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def ask(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        job_id: str = "",
        cancel_event: CancelEvent = None,
        on_trace: "Callable[[Any], None] | None" = None,
    ) -> AskResponse:
        """Dispatch to the engine named by payload.mode, resolved through the
        frozen ask_modes registry (fast/global aliases included). Unknown modes
        raise UnknownAskMode — never a silent fall-through. on_trace only
        reaches streaming engines (mirrors the frozen route-runner split)."""
        from app.services.retrieval_run import retrieval_run

        spec = self._resolve_ask_mode(getattr(payload, "mode", None))
        handler = getattr(self, spec.handler)
        with source_scope_context(
            notebook_id,
            getattr(payload, "source_scope", None),
            getattr(payload, "base_scope", None),
        ):
            # Plugin engines construct their own stable-kind retrieval run in
            # ``ask_plugin_engine`` so plugin mode ids never enter the allowed
            # observability vocabulary. Built-ins keep their frozen path.
            if spec.handler == "ask_plugin_engine":
                return handler(
                    notebook_id, payload, user_id=user_id, job_id=job_id,
                    on_trace=on_trace, cancel_event=cancel_event,
                )
            # All built-in Ask modes share the same request-local query-embedding
            # memo.  Ask keeps its historical internal concurrency: the
            # report-only database fan-out gate is deliberately absent here.
            with retrieval_run(
                run_kind=f"ask_{spec.id}",
                event_log=getattr(self, "event_log", None),
                correlation_id=job_id,
                actor_id=user_id,
                cancel_event=cancel_event,
            ):
                if spec.streaming:
                    return handler(
                        notebook_id, payload, user_id=user_id, job_id=job_id,
                        on_trace=on_trace, cancel_event=cancel_event,
                    )
                return handler(
                    notebook_id, payload, user_id=user_id, job_id=job_id,
                    cancel_event=cancel_event,
                )

    def ask_current(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Run the synchronous Ask surface through the durable job lifecycle.

        Streaming and synchronous callers now share the same state-store
        primitives: create/touch conversation plus running job atomically,
        pass the job into the engine's atomic final save, then finalize and
        unregister. The job id remains internal to this blocking protocol.
        """
        user_id = self.current_user_id()
        mode = self._resolve_ask_mode(getattr(payload, "mode", None))
        self.validate_reasoning_submission(notebook_id, payload)
        cancel_event = threading.Event()
        job_id, _conversation_id = self.begin_job_current(
            notebook_id, payload, mode.id, cancel_event
        )
        try:
            response = self.ask(
                notebook_id,
                payload,
                user_id=user_id,
                job_id=job_id,
                cancel_event=cancel_event,
            )
        except AskCancelled:
            self.finish_job(job_id, "cancelled")
            raise
        except BaseException as exc:
            self.finish_job(
                job_id, "failed", error=f"{type(exc).__name__}: {exc}"
            )
            raise
        answer_id = str(getattr(response, "answer_id", "") or "")
        if not answer_id:
            error = RuntimeError("synchronous Ask completed without a durable answer")
            self.finish_job(
                job_id, "failed", error=f"{type(error).__name__}: {error}"
            )
            raise error
        self.finish_job(job_id, "done", answer_id=answer_id)
        return response

    def ask_chunk_current(
        self, notebook_id: str, payload: AskRequest, cancel_event: CancelEvent = None
    ) -> AskResponse:
        return self.ask_chunk(
            notebook_id, payload, user_id=self.current_user_id(),
            cancel_event=cancel_event,
        )

    def ask_reasoning_current(
        self, notebook_id: str, payload: AskRequest, on_trace=None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        return self.ask_reasoning(
            notebook_id, payload, user_id=self.current_user_id(),
            on_trace=on_trace, cancel_event=cancel_event,
        )

    def unconfigured_model_response_current(
        self, notebook_id: str, question: str, conversation_id: str, mode: str,
    ) -> AskResponse:
        return self._unconfigured_model_response(
            notebook_id, question, conversation_id, mode,
            user_id=self.current_user_id(),
        )

    def save_answer_current(
        self, notebook_id: str, question: str, response: AskResponse,
        conversation_id: Optional[str] = None,
    ) -> str:
        return self._save_answer(
            notebook_id, question, response, conversation_id,
            user_id=self.current_user_id(),
        )

    def ensure_conversation_current(
        self, db, notebook_id: str, conversation_id: Optional[str], question: str
    ) -> str:
        return self.ask_state.ensure_conversation(
            db, notebook_id, conversation_id, question, self.current_user_id()
        )

    def begin_job_current(
        self, notebook_id: str, payload, mode: str, cancel_event
    ) -> tuple[str, str]:
        self.notebooks.get_notebook(notebook_id)
        job_id, conversation_id = self.ask_state.begin_durable_job(
            notebook_id, payload, mode, self.current_user_id()
        )
        self.cancellations.register(job_id, cancel_event)
        return job_id, conversation_id

    def finish_job(
        self, job_id: str, status: str, *, answer_id: str = "", error: str = ""
    ) -> None:
        """Finalize one durable Ask and remove its in-process cancel handle."""
        conversation_id = self.ask_state.finish_job(
            job_id, status, answer_id=answer_id, error=error
        )
        self.cancellations.unregister(job_id)
        if status in ("cancelled", "failed") and conversation_id:
            self.ask_state.cleanup_empty_conversation(conversation_id)

    def cancel_job(self, job_id: str, user_id: str) -> dict:
        state = self.ask_state.cancel_running_job(job_id, user_id)
        if state["cancelled"]:
            self.cancellations.cancel(job_id)
            if state["conversation_id"]:
                self.ask_state.cleanup_empty_conversation(state["conversation_id"])
        return {"status": state["status"], "job_id": job_id}

    def append_trace_fail_open(self, job_id: str, step: dict) -> None:
        try:
            self.ask_state.append_trace("", job_id, step, "")
        except Exception:  # noqa: BLE001
            self.event_log.logger.exception("append_ask_trace failed for %s", job_id)

    def list_conversations_current(self, notebook_id: str):
        self.notebooks.get_notebook(notebook_id)
        return self.ask_state.list_conversations(
            notebook_id, self.current_user_id()
        )

    def bulk_delete_conversations_current(
        self, notebook_id: str, older_than_days: int
    ) -> ConversationBulkDeleteResult:
        if older_than_days < 1:
            raise ValueError("older_than_days must be >= 1")
        self.notebooks.get_notebook(notebook_id)
        return self.ask_state.bulk_delete_conversations(
            notebook_id, older_than_days, self.current_user_id()
        )

    # ------------------------------------------------------------------
    # shared seams (verbatim facade bodies over the narrow ports)
    # ------------------------------------------------------------------

    def _primary_llm_unconfigured(self) -> bool:
        return self.model_clients.primary_unconfigured()

    def _collections_reachable(self, notebook_id: str) -> bool:
        """作用域里是否还有集合枚举工具真的能列出来的东西。

        这是「本笔记本还没有知识图谱」早退的唯一放行条件(见调用处)。判据是
        **地图上有非零集合**,而不是宽松的「有来源」——注意这两者现在不再等价,
        因为「有来源」正是三个集合之一(见下面那条订正)。

        接线判据与 run 内的总闸共用 ``enumeration_wiring_active`` ——各写一份的
        话,kill switch 一关就会出现「早退放行了、run 里却没有工具」的空转。

        地图本身随后会在 ``ReasoningRetriever.run`` 里再建一次;计数走的是按源
        变更信号 keyed 的有界缓存,第二次基本只付参与者与信号查询,而且这条路径
        原本是直接早退的,增量成本只落在它自己身上。

        fail-open 的方向在这里是**反的**:地图建不出来就回到早退(接入前的行为),
        而不是把用户送进一轮什么都拿不到的循环。

        **来源计数计入放行(codex R5 P1 订正)。** PR-2.5 第一版刻意把它排除在外,
        理由是「非空库恒 ≥1,算进来等于拆掉这道闸,那句明确的『请先构建知识图谱』
        会对所有纯文本库消失」。那个理由的**前半段成立、结论是错的**:

        * 被挡住的恰好是来源清单的主力场景。一个只被纯文本解析器处理过的论文库
          既没有公式/表格/图片/代码块、也没有知识对象,但它**有文档**——而
          「库里有哪几篇 / 逐篇分析当前 notebook」这类问题问的就是那些文档。
          用户问文档目录,拿回「请先构建知识图谱」,那不是一句更明确的提示,
          那是没有回答被问的问题;
        * 来源清单不需要图谱:它是零 LLM 的,读的就是 ``sources`` 行。挡住它
          换不来任何正确性;
        * 这与 PR-2 立下的判断是同一个:自动 KG 抽取默认关,「解析了来源但没建图」
          是常态,所以早退才收窄成「无图**且**拿不出任何集合」。加进第三个集合
          之后没跟着更新这个判定函数,是漏跟,不是一个独立的决定。

        保留的语义(两条,都在测试里钉住):零源库仍然早退(计数为 0),地图构建
        失败仍然早退(上面的 except)。放行之后 ``kg_required`` 仍如实为 True,
        它是响应契约的一部分,不因为这一轮跑通了就变成假的。
        """
        from app.services.reasoning_retrieval import enumeration_wiring_active
        from app.services.source_scope import current_source_scope

        source_scope = current_source_scope()
        if source_scope is not None and source_scope.restricted:
            # Scoped runs deliberately disable whole-collection enumeration,
            # but selected documents still expose raw element search. Keep the
            # no-KG guard from short-circuiting before that tool can run.
            return (
                source_scope.mode == "exclude"
                or bool(source_scope.source_ids)
            )

        if not enumeration_wiring_active(
            self.settings, self.collection_catalog, self.collection_enumeration
        ):
            return False
        try:
            collection_map = self.collection_catalog.collection_map(notebook_id)
        except AskCancelled:
            raise
        except Exception:       # noqa: BLE001 — 见 docstring:退回早退
            return False
        return (
            any(item.count > 0 for item in collection_map.elements)
            or any(count > 0 for _object_type, count in collection_map.kg_objects)
            # 用户可见来源数。零源库因此仍然早退——这不是「有 notebook 就放行」。
            or collection_map.sources > 0
        )

    def _tier_map_for(self, notebook_ids: Iterable[str]) -> Dict[str, str]:
        return self.evidence_context.tier_map(list(notebook_ids))

    def _chunk_answer_context(self, chunks, budget_chars: "int | None" = None,
                              notebook_id: str = "", id_offset: int = 0) -> tuple:
        return self.evidence_context.chunk_context(
            chunks, notebook_id=notebook_id, id_offset=id_offset,
            budget_chars=budget_chars)

    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge],
                        id_offset: int = 0, budget_chars: int | None = None) -> tuple:
        return self.evidence_context.knowledge_context(
            notebook_id, top_hits, id_offset=id_offset,
            budget_chars=budget_chars)

    def _parse_answer_anchors(self, answer: str, id_map: dict) -> list:
        return self.evidence_context.parse_anchors(answer, id_map)

    def _memory_hits(self, user_id: str, notebook_id: str, query: str):
        # Memory/Knowhow projection sources are intentionally absent from the
        # checkbox list. Once the user narrows that list, only selected imported
        # sources (plus mounted bases) may contribute evidence.
        if source_scope_restricted():
            return []
        if self.memory_retriever is None:
            return []
        return self.memory_retriever.notebook_memory_hits(
            user_id, notebook_id, query, 8
        )

    def preview_reasoning_intent(
        self,
        notebook_id: str,
        question: str,
        history: str = "",
        cancel_event: CancelEvent = None,
    ) -> QueryIntentContract:
        """Understand a reasoning request before any corpus retrieval starts."""
        from app.services.query_intent import plan_query_intent

        status: dict[str, bool] = {}
        contract = plan_query_intent(
            self.model_clients.chat("reasoning_agent"),
            question,
            history,
            max_topics=self.settings.reasoning_max_subqueries,
            purpose="step-by-step evidence-grounded answer",
            cancel_event=cancel_event,
            status=status,
        )
        result = QueryIntentContract(**contract)
        result._understanding_succeeded = status.get(
            "understanding_succeeded", False
        )
        return result

    def validate_reasoning_submission(
        self, notebook_id: str, payload: AskRequest,
    ) -> None:
        """Freeze a submitted reasoning intent before a durable Ask is created.

        Both Ask entry points call this above their job-publishing step —
        ``ask_current`` before ``begin_job_current`` and the streaming
        coordinator before ``begin_durable_job`` — so an invalid submission
        fails before a durable job or stream header exists.  It no longer
        depends on the request's source scope: the checkbox ceiling is applied
        by ``source_scope_context`` at retrieval boundaries, and the
        model-inferred scope this used to cross-check is gone.
        """
        if self._resolve_ask_mode(getattr(payload, "mode", None)).id != "reasoning":
            return
        if payload.intent is None:
            return
        self._confirmed_reasoning_intent(payload, "")

    @staticmethod
    def _confirmed_reasoning_intent(
        payload: AskRequest,
        history: str,
    ) -> QueryIntentContract:
        """Freeze submitted intent; direct legacy callers get deterministic guard."""
        from app.services.query_intent import (
            finalize_query_intent,
            plan_query_intent,
        )

        original = payload.question.strip()
        if payload.intent is not None:
            seed = payload.intent.contract.model_dump()
            if str(seed.get("objective") or "").strip() != original:
                raise ValueError("问题理解与当前问题不匹配，请重新确认")
            final = finalize_query_intent(
                seed,
                resolved_question=payload.intent.resolved_question,
                answers=[row.model_dump() for row in payload.intent.answers],
            )
            return QueryIntentContract(**final)

        # Repository/MCP compatibility callers may not have used the HTTP
        # preview endpoint.  Preserve zero-extra-model-call behavior for clear
        # questions, while still failing closed on deterministic missing
        # referents/generic requests instead of retrieving against guesswork.
        seed = plan_query_intent(None, original, history, max_topics=4)
        if seed.get("needs_clarification"):
            raise ValueError("问题仍有关键歧义，请先确认问题理解")
        # A caller that bypasses preview has not reviewed decomposition
        # metadata. Keep its compatibility query byte-for-byte equal to the
        # original instead of silently promoting fallback topics to authority.
        seed["mandatory_topics"] = []
        return QueryIntentContract(**finalize_query_intent(seed))

    def _append_memory_context(self, context_block: str, id_map: dict, hits):
        if not hits:
            return context_block, id_map
        block, memory_map = self.memory_retriever.context(
            hits, id_offset=self._MEMORY_KEY_BASE
        )
        if block and block != "(none)":
            context_block = f"{context_block}\n\n[Confirmed Memory]\n{block}"
        return context_block, {**id_map, **memory_map}

    @staticmethod
    def _bounded_context_append(
        context_block: str,
        id_map: dict,
        block: str,
        block_map: dict,
        *,
        budget_chars: int,
        heading: str = "",
        admission_sink: dict | None = None,
    ) -> tuple[str, dict]:
        """Append one evidence block without crossing a partition hard limit."""
        if not block or block == "(none)":
            return context_block, id_map
        prefix = ("\n\n" if context_block else "")
        if heading:
            prefix += f"[{heading}]\n"
        remaining = max(0, int(budget_chars) - len(context_block))
        if remaining <= len(prefix):
            return context_block, id_map
        rendered = (prefix + block)[:remaining]
        if admission_sink is not None and len(prefix) + len(block) > remaining:
            admission_sink["ambiguous_truncations"] = int(
                admission_sink.get("ambiguous_truncations") or 0
            ) + 1
        merged = dict(id_map)
        for key, value in block_map.items():
            if re.search(rf"(?m)^{re.escape(str(key))}:", rendered):
                merged[key] = value
        return context_block + rendered, merged

    @staticmethod
    def _memory_citations(anchors, hits) -> list[Citation]:
        by_id = {hit.memory_id: hit for hit in hits}
        out: list[Citation] = []
        for anchor in anchors:
            hit = by_id.get(anchor.object_id) if anchor.object_type == "memory" else None
            if hit is None:
                continue
            out.append(Citation(
                label=f"Memory · {hit.title}",
                source_id="",
                element_id="",
                location_label="Memory",
                quoted_span=hit.text[:200],
                tier="personal",
                memory_id=hit.memory_id,
                provenance=dict(hit.provenance),
            ))
        return out

    def _needs_index(self, notebook_id: str) -> bool:
        """大库且磁盘完全无 scale 索引(从未建过)→ True。用于 AskResponse.index_required:
        大库检索强制走索引,无索引时检索降级(FTS/skip/refuse),需提示用户手动建索引。
        小库(copyable=True)允许暴力、不要求索引 → False。已建索引(含 stale/有 delta)→
        False(那是恒定成本·最终一致态,由「N 源待索引」徽章覆盖,不重复提示)。
        两处判定都廉价:copystats 版本 memo;索引探针经磁盘身份缓存 O(1)。"""
        try:
            has_index = self.scale_index_probe(notebook_id)
            return self.scale_profiles().requires_index(
                notebook_id, has_disk_index=has_index)
        except Exception:  # noqa: BLE001 — 判定失败不拖垮 ask,退化为不提示
            return False

    def _prepare_turn(
        self,
        notebook_id: str,
        conversation_id: Optional[str],
        question: str,
        *,
        user_id: str,
        job_id: str,
    ) -> "PreparedAskTurn":
        """Prepare through the durable lease when this is a public Ask job.

        Legacy engine-level compatibility calls without a job keep the old
        create-or-continue behavior.  Once a durable job exists, however, its
        exact parent and running status are authoritative: cancellation or
        deletion raises before any fallback conversation can be created.
        """
        if not job_id:
            return self.ask_state.prepare_turn(
                notebook_id, conversation_id, question, user_id
            )
        turn = self.ask_state.prepare_turn_for_job(
            job_id, notebook_id, conversation_id, user_id
        )
        if turn is None:
            raise AskCancelled()
        return turn

    def _save_answer(
        self,
        notebook_id: str,
        question: str,
        response: AskResponse,
        conversation_id: Optional[str] = None,
        *,
        user_id: str,
        job_id: str = "",
        asked_at: str = "",
    ) -> str:
        # 所有 ask handler 的唯一收口:在持久化/返回前给 response 打大库无索引提示位。
        # 覆盖 chunk/reasoning/graph 三 handler 的全部 return 路径(含早退),避免逐 handler
        # 多 return 点漏赋值。小库/已索引 → False(默认),无副作用。
        response.index_required = self._needs_index(notebook_id)
        # 同一收口:把本轮实际检索范围的**只读**回执随答案一起落库。None = 本次请求
        # 两个维度都没收窄(或调用方根本不经 API 入口),此时字段缺席、序列化与历史
        # 答案逐位一致。此处只写不读——它不参与任何检索判定。
        from app.services.source_scope import current_retrieval_scope_receipt

        receipt = current_retrieval_scope_receipt()
        if receipt is not None:
            response.retrieval_scope = receipt
        response.asked_at = asked_at or response.asked_at
        if not job_id:
            return self.ask_state.save_answer(
                notebook_id, conversation_id, question, response, user_id
            )
        answer_id = self.ask_state.save_answer_for_job(
            job_id,
            notebook_id,
            conversation_id,
            question,
            response,
            user_id,
        )
        if answer_id is None:
            raise AskCancelled()
        return answer_id

    def ask_plugin_engine(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        job_id: str = "",
        cancel_event: CancelEvent = None,
        on_trace: "Callable[[Any], None] | None" = None,
    ) -> AskResponse:
        """Run one deployment engine while core retains every authority.

        v1 deliberately supplies neither conversation history nor intent/KG
        ports. The provider returns prose plus opaque run-issued handles; this
        method alone admits citations, constructs response objects and saves.
        """
        from app.application.ask_plugin import PreparedPluginAsk, PluginResponseDraft
        from app.application.ask_reasoning import StageBoundaryError
        from app.domain.ask_engine import (
            AskEngineContext,
            AskPluginEngineError,
            safe_plugin_engine_error_code,
        )
        from app.services.plugin_ask_engine import (
            PluginCancellationToken,
            PluginEngineModelAccess,
            PluginEngineTrace,
            PluginRetrievalAccess,
            admit_plugin_engine_result,
            append_plugin_engine_trace_disclosure,
            complete_plugin_engine_trace,
            finish_plugin_engine_trace,
            plugin_engine_trace_steps,
            release_plugin_engine_ports,
        )
        from contextlib import ExitStack

        from app.services.retrieval_run import current_retrieval_run, retrieval_run
        from app.services.source_scope import (
            current_source_scope,
            source_scope_context,
        )

        host = self.ask_engine_host
        mode_id = str(payload.mode or "")
        if host is None or host.mode(mode_id) is None:
            raise AskPluginEngineError("plugin_engine_unavailable")
        turn = self._prepare_turn(
            notebook_id,
            payload.conversation_id,
            payload.question,
            user_id=user_id,
            job_id=job_id,
        )
        prepared = PreparedPluginAsk(
            mode_id=mode_id,
            notebook_id=notebook_id,
            question=payload.question,
            conversation_id=turn.conversation_id,
            user_id=user_id,
            job_id=job_id,
            asked_at=payload.asked_at,
        )
        with retrieval_run(
            run_kind="ask_plugin_engine",
            event_log=getattr(self, "event_log", None),
            correlation_id=job_id,
            actor_id=user_id,
            cancel_event=cancel_event,
        ):
            run = current_retrieval_run()
            if (
                run is None
                or run.run_kind != "ask_plugin_engine"
                or run.actor_id != prepared.user_id
                or run.correlation_id != prepared.job_id
                or run.cancel_event is not cancel_event
            ):
                raise StageBoundaryError("invalid plugin Ask retrieval authority")
            scope = current_source_scope()
            if scope is not None and scope.notebook_id != prepared.notebook_id:
                raise StageBoundaryError("plugin Ask source scope changed")
            owned_ports: list[object] = []
            hidden_cache: dict[tuple[str, str], tuple[str, ...]] = {}

            def plugin_hidden_sources(
                notebook_id: str, actor_id: str
            ) -> tuple[str, ...]:
                # Structural Memory exclusion (codex #603 R4 P1): a plugin
                # citation cannot carry Memory identity (`Citation.memory_id`
                # is what the MCP memory:read filter recognizes, and the port
                # has no channel to mint it), so the caller's own Memory
                # projection sources never enter the plugin retrieval face at
                # all — filtering the frozen universe here beats trying to
                # re-identify Memory-backed rows after the fact. Knowhow
                # projections stay: the grid is notebook-shared content.
                cached = hidden_cache.get((notebook_id, actor_id))
                if cached is not None:
                    return cached
                ids = tuple(
                    self.ask_engine_hidden_sources(notebook_id, actor_id)
                )
                if ids:
                    metadata = self.evidence_context.source_metadata(ids)
                    ids = tuple(
                        source_id for source_id in ids
                        if (metadata.get(source_id) or {}).get("source_type")
                        != "memory"
                    )
                hidden_cache[(notebook_id, actor_id)] = ids
                return ids

            scope_stack = ExitStack()
            try:
                # The two scope dimensions are independently optional, so
                # every OMITTED dimension is synthesized on its own while a
                # supplied one passes through field-faithfully (codex #604
                # R2 P2: a base-only or local-only submission left the other
                # dimension unfrozen for the un-narrowed KG lane). The result
                # is the frozen all-selected snapshot shape the browser
                # freezes for every request, so the KG candidate seam behaves
                # identically on every face: ANN arm on, snapshot applied at
                # evidence hydrate. narrowed=False keeps every channel open,
                # and the display receipt is a separate ContextVar only the
                # API route sets on real narrowing — nothing user-visible is
                # produced (this is per-run internal freezing, deliberately
                # unlike persisted report scopes where an omitted dimension
                # must stay omitted). The local hidden half MUST be the RAW
                # set (the caller's Memory projections included, exactly what
                # the browser snapshot carries): the seam's universe-drift
                # probe compares it against the live `hidden_source_ids`
                # read, and a Memory-stripped copy would never match —
                # silently re-closing the ANN arm for every user who has one
                # confirmed Memory (ANN-arm review P2-1). Memory exclusion is
                # NOT this ceiling's job; its authorities are the port
                # universe (`plugin_hidden_sources`) and the out-of-universe
                # drop rule in `_issue_kg_evidence`. The library half freezes
                # as include of the mounted-at-synthesis set (codex #604 R1
                # P2): a None base scope leaves `base_ceiling_active` false
                # and a library mounted mid-run would join the seam path.
                # The supplied-half pass-through rebuilds raw dicts from the
                # live scope object rather than the persistence payload
                # helpers — those deliberately drop hidden ids, which would
                # re-break the drift-probe equality.
                needs_local = scope is None or not scope.source_provided
                needs_base = scope is None or not scope.base_provided
                if needs_local or needs_base:
                    local_raw = (
                        {
                            "mode": "include",
                            "source_ids": list(self.ask_engine_visible_sources(
                                prepared.notebook_id
                            )),
                            "hidden_source_ids": list(
                                self.ask_engine_hidden_sources(
                                    prepared.notebook_id, prepared.user_id
                                )
                            ),
                            "narrowed": False,
                            "owner_id": prepared.user_id,
                        }
                        if needs_local else
                        {
                            "mode": scope.mode,
                            "source_ids": list(scope.source_ids),
                            "narrowed": scope.narrowed,
                            "hidden_source_ids": list(scope.hidden_source_ids),
                            "owner_id": scope.owner_id,
                        }
                    )
                    base_raw = (
                        {
                            "mode": "include",
                            "notebook_ids": [
                                participant for participant in
                                self.ask_engine_participant_notebooks(
                                    prepared.notebook_id
                                )
                                if participant != prepared.notebook_id
                            ],
                            "narrowed": False,
                        }
                        if needs_base else
                        {
                            "mode": scope.base_mode,
                            "notebook_ids": list(scope.base_notebook_ids),
                            "narrowed": scope.base_narrowed,
                        }
                    )
                    scope_stack.enter_context(source_scope_context(
                        prepared.notebook_id, local_raw, base_raw
                    ))
                retrieval = PluginRetrievalAccess(
                    active_notebook_id=prepared.notebook_id,
                    actor_id=prepared.user_id,
                    cancellation=cancel_event,
                    participant_notebook_ids=(
                        self.ask_engine_participant_notebooks
                    ),
                    all_visible_source_ids=self.ask_engine_visible_sources,
                    hidden_source_ids=plugin_hidden_sources,
                    search_elements=(
                        self.retrieval.federated_retrieve_elements
                    ),
                    source_info=self.evidence_context.citation_source_info,
                    # Injected callables, not imports: the KG read port must not
                    # give this module a retrieval-layer dependency edge. Missing
                    # seats resolve to None and fail only if a plugin actually
                    # calls that capability, so an unwired seat cannot turn a run
                    # that never touches KG into a failure.
                    search_knowledge=getattr(
                        self.retrieval, "federated_retrieve", None
                    ),
                    object_neighbors=getattr(
                        self.retrieval, "retrieve_neighbors", None
                    ),
                    collection_overview=getattr(
                        self.collection_catalog, "collection_map_text", None
                    ),
                    evidence_elements=getattr(
                        self.evidence_context, "evidence_elements", None
                    ),
                    kg_max_calls=(
                        self.settings.ask_plugin_engine_kg_search_max_calls
                    ),
                    max_k=self.settings.ask_plugin_engine_retrieval_max_k,
                    max_calls=self.settings.ask_plugin_engine_search_max_calls,
                    evidence_chars=(
                        self.settings.ask_plugin_engine_evidence_max_chars
                    ),
                    query_chars=(
                        self.settings.ask_plugin_engine_prompt_max_chars
                    ),
                )
                owned_ports.append(retrieval)
                model_client = self.model_clients.chat("plugin_engine")
                model = PluginEngineModelAccess(
                    model_client,
                    cancellation=cancel_event,
                    max_calls=self.settings.ask_plugin_engine_model_max_calls,
                    max_chars=self.settings.ask_plugin_engine_prompt_max_chars,
                )
                owned_ports.append(model)
                cancellation = PluginCancellationToken(cancel_event)
                owned_ports.append(cancellation)
                engine_context = AskEngineContext(
                    question=prepared.question,
                    cancellation=cancellation,
                )
                # Construct the recorder immediately before the host call so
                # its wall clock covers the extension-engine stage itself,
                # not unrelated port/context setup above it.
                trace = PluginEngineTrace(
                    max_steps=self.settings.ask_plugin_engine_trace_max_steps,
                    label_chars=(
                        self.settings.ask_plugin_engine_trace_label_max_chars
                    ),
                    detail_chars=(
                        self.settings.ask_plugin_engine_trace_detail_max_chars
                    ),
                    on_trace=on_trace,
                )
                owned_ports.append(trace)
                try:
                    result = host.answer(
                        mode_id,
                        engine_context,
                        retrieval,
                        model,
                        trace,
                        event_sink=getattr(self.event_log, "emit", None),
                        on_provider_finished=lambda: finish_plugin_engine_trace(
                            trace
                        ),
                    )
                except AskCancelled:
                    raise
                except BaseException:
                    try:
                        finish_plugin_engine_trace(trace)
                        complete_plugin_engine_trace(trace, status="failed")
                    except BaseException:
                        # Preserve the provider/host error as the authority.
                        pass
                    raise
                else:
                    finish_plugin_engine_trace(trace)
                try:
                    if (
                        engine_context.question != prepared.question
                        or engine_context.cancellation is not cancellation
                    ):
                        raise StageBoundaryError(
                            "plugin Ask request identity changed"
                        )
                    answer, records, admission_notes = admit_plugin_engine_result(
                        retrieval, result.answer_markdown, result.citations
                    )
                except BaseException:
                    try:
                        complete_plugin_engine_trace(trace, status="failed")
                    except BaseException:
                        # Preserve identity/admission failure as authoritative.
                        pass
                    raise
                if admission_notes:
                    append_plugin_engine_trace_disclosure(
                        trace,
                        summary="引用核验未全部通过",
                        detail="；".join(admission_notes),
                    )
                complete_plugin_engine_trace(trace, status="completed")
                trace_steps = plugin_engine_trace_steps(trace)
            except AskCancelled:
                raise
            except StageBoundaryError:
                raise
            except AskPluginEngineError:
                raise
            except Exception as exc:
                code = safe_plugin_engine_error_code(
                    getattr(exc, "code", "plugin_engine_failed")
                )
                raise AskPluginEngineError(code) from None
            finally:
                release_plugin_engine_ports(*owned_ports)
                scope_stack.close()

            tier_map = self._tier_map_for(
                {record.notebook_id for record in records}
            )
            knowhow_refs = self.evidence_context.knowhow_refs_for(
                record.element_id for record in records
            )
            citations: list[Citation] = []
            for record in records:
                evidence = record.evidence
                # `record.quoted_span` is the bound evidence element's own
                # verbatim excerpt; `evidence.text` for a KG hit is a
                # model-authored name+definition summary, not a quote from
                # the source. The fallback re-admits the summary only when the
                # KG evidence binding carried no excerpt — every current
                # producer writes a non-empty quote, so this is not an
                # invariant, just the least-bad rendering for legacy rows.
                # The element path never fills `quoted_span` and falls back to
                # its own already-verbatim `evidence.text`.
                citations.append(Citation(
                    label=(
                        f"{evidence.source_title} · {evidence.location_label}"
                    ).strip(" ·"),
                    source_id=record.source_id,
                    element_id=record.element_id,
                    location_label=evidence.location_label,
                    quoted_span=record.quoted_span or evidence.text,
                    source_file_name=record.source_file_name,
                    tier=tier_map.get(record.notebook_id, "personal"),
                    notebook_id=(
                        record.notebook_id
                        if record.notebook_id != prepared.notebook_id else ""
                    ),
                    knowhow=knowhow_refs.get(record.element_id),
                ))
            anchors: list[AnswerAnchor] = []
            seen_indices: set[int] = set()
            for marker in _MARKER_GROUP_RE.findall(answer):
                for key in marker_keys(marker):
                    index = int(key[1:])
                    if index < 1 or index > len(records):
                        # Structurally unreachable post-admission (the final
                        # body only carries compacted verified indexes); kept
                        # so a future admission bug degrades to a missing
                        # anchor rather than an uncaught 500.
                        continue
                    if index in seen_indices:
                        continue
                    seen_indices.add(index)
                    record = records[index - 1]
                    evidence = record.evidence
                    anchors.append(AnswerAnchor(
                        key=f"k{index}",
                        object_id=record.element_id,
                        # Deliberately "element" even for KG hits: object_id
                        # above is an ELEMENT id (the object's first surviving
                        # evidence element — the registered citation contract),
                        # and any non-element object_type makes the browser
                        # offer "在知识图谱中定位" and feed that element id to
                        # the graph as a node id, a click that can never
                        # succeed. object_type and object_id must stay
                        # same-sourced; the KG node type still reaches the
                        # plugin via EngineEvidence.object_type.
                        object_type="element",
                        label=evidence.source_title or f"k{index}",
                        snippet=record.quoted_span or evidence.text,
                        source_title=evidence.source_title,
                        source_file_name=record.source_file_name,
                        location_label=evidence.location_label,
                        source_id=record.source_id,
                        element_id=record.element_id,
                        tier=tier_map.get(record.notebook_id, "personal"),
                        notebook_id=(
                            record.notebook_id
                            if record.notebook_id != prepared.notebook_id else ""
                        ),
                        knowhow=knowhow_refs.get(record.element_id),
                    ))
            self.evidence_context.attach_citation_images([
                *((anchor, (anchor.element_id,)) for anchor in anchors),
                *((citation, (citation.element_id,)) for citation in citations),
            ])
            grounded = bool(records)
            response = AskResponse(
                conclusion=answer,
                answer=answer,
                grounded=grounded,
                evidence_level="grounded" if grounded else "inferred",
                anchors=anchors,
                citations=citations,
                llm_mode=str(getattr(model_client, "model", "") or ""),
                mode=prepared.mode_id,
                conversation_id=prepared.conversation_id,
                retrieval_query=prepared.question,
                top_relevance=max(
                    (record.score for record in records), default=0.0
                ),
                reasoning_trace=list(trace_steps),
            )
            draft = PluginResponseDraft(
                mode_id=prepared.mode_id,
                notebook_id=prepared.notebook_id,
                question=prepared.question,
                conversation_id=prepared.conversation_id,
                user_id=prepared.user_id,
                job_id=prepared.job_id,
                asked_at=prepared.asked_at,
                response=response,
            )
            if type(draft) is not PluginResponseDraft or type(response) is not AskResponse:
                raise StageBoundaryError("invalid plugin Ask response draft")
            for field in (
                "mode_id", "notebook_id", "question", "conversation_id",
                "user_id", "job_id", "asked_at",
            ):
                if getattr(draft, field) != getattr(prepared, field):
                    raise StageBoundaryError(
                        f"plugin Ask response changed {field}"
                    )
            if (
                response.mode != prepared.mode_id
                or response.conversation_id != prepared.conversation_id
            ):
                raise StageBoundaryError("plugin Ask response identity changed")
            if (
                current_retrieval_run() is not run
                or run.actor_id != prepared.user_id
                or run.correlation_id != prepared.job_id
                or run.cancel_event is not cancel_event
            ):
                raise StageBoundaryError("plugin Ask retrieval authority changed")
            current_scope = current_source_scope()
            if current_scope is not scope:
                raise StageBoundaryError("plugin Ask source scope authority changed")
            raise_if_cancelled(cancel_event)
            response.answer_id = self._save_answer(
                prepared.notebook_id,
                prepared.question,
                response,
                prepared.conversation_id,
                user_id=prepared.user_id,
                job_id=prepared.job_id,
                asked_at=prepared.asked_at,
            )
            return response

    # ------------------------------------------------------------------
    # synthesis helpers
    # ------------------------------------------------------------------

    def _search_profile_style_block(self, user_id: str) -> str:
        """Agentic Memory P3(B-Profile,T8):按本次提问 ``user_id`` 一次主键点读
        该用户的检索/回答风格偏好文档,渲染成一句有界英文提示,供
        ``answer_prompt`` 的 ``style_block`` 形参消费(合成侧独立于
        ``ReasoningRetriever.run()`` 自己为规划侧做的那次点读——两个消费点各自
        fail-open、各自按需读一次,不共享缓存,镜像 P1/P2 两个既有块「谁读 store
        就在哪读」的形态,不新造一条跨组件传值的路)。

        ⚠ 绝不回退 ``current_user()`` ContextVar —— 后台/跨用户路径会在
        ContextVar 未设时读到 seeded admin,把一个人的偏好注给另一个人的回答
        (同 ``ReasoningRetriever.profile_owner_id`` 的红线)。空 ``user_id``、
        关闸(``search_profile_wiring_active`` 假)与任何读取/渲染异常都 fail-open
        成空串——调用形状与接入前逐字相同,风格提示是背景,不是任何一次问答的
        必需品。
        """
        if not user_id:
            return ""
        from app.services.reasoning_retrieval import search_profile_wiring_active
        if not search_profile_wiring_active(self.settings, self.identity_store):
            return ""
        try:
            profile = self.identity_store.get_user_search_profile(user_id)
            if not profile:
                return ""
            return render_style_block(profile)
        except Exception:  # noqa: BLE001 — 风格提示是背景,不是必需品
            return ""

    def _answer_chunks(
        self,
        question,
        chunks,
        history="",
        cancel_event: CancelEvent = None,
        notebook_id: str = "",
        memory_hits=None,
        llm_client=None,
        baseline_sink: dict | None = None,
        style_block: str = "",
    ) -> tuple:
        """长上下文综合:把 MMR 精选的 chunk 原文喂给答案 LLM。返回
        (answer, llm_grounded, anchors)。复用 answer_prompt 的 [k] 标注协议。
        notebook_id:转发给 _chunk_answer_context 解 anchor.tier(见其 docstring);
        chunk 自带 notebook_id(跨库召回)优先,这只是单库 chunk 的回退值。
        ``style_block``:Agentic Memory P3(T8)——调用方按本次提问 user_id 点读
        并渲染好的风格提示,空串=接入前逐字行为(见 ``_search_profile_style_block``)。"""
        raise_if_cancelled(cancel_event)
        context_block, id_map = self._chunk_answer_context(chunks, notebook_id=notebook_id)
        context_block, id_map = self._append_memory_context(
            context_block, id_map, memory_hits or []
        )
        if baseline_sink is not None:
            baseline_sink.clear()
            baseline_sink.update({
                "context_block": context_block,
                "id_map": dict(id_map),
                "ordered_handles": tuple(id_map),
                "budget_chars": self.settings.chunk_answer_budget_chars,
            })
        llm_client = llm_client or self.model_clients.chat("ask_answer")
        raw = llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(
                question, context_block, history, style_block=style_block)}],
            ANSWER_SCHEMA_HINT,
            cancel_event=cancel_event,
            **cap_kwargs(llm_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = data.get("grounded", False) is True
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def _answer_mix(
        self,
        question,
        chunks,
        kg_block,
        kg_id_map,
        history="",
        cancel_event: CancelEvent = None,
        notebook_id: str = "",
        memory_hits=None,
        llm_client=None,
        baseline_sink: dict | None = None,
        style_block: str = "",
    ) -> tuple:
        """mix 长上下文综合:chunk 段(k1..kN)+ KG 段(k1001+),统一 id_map。
        chunk 段不再二次预算(选择阶段已 token 预算),故 budget_chars 给极大值。
        返回 (answer, llm_grounded, anchors)。notebook_id:转发给 _chunk_answer_context
        解 anchor.tier(见其 docstring);chunk 自带 notebook_id(跨库召回)优先,
        这只是单库 chunk 的回退值。``style_block``:见 ``_answer_chunks`` 同名参数。"""
        # chunk 段编号 k1..kN,KG 段从 _MIX_KG_KEY_BASE 起;若 chunk 数逼近 base 会在
        # 合并 id_map 时撞 KG key(静默覆盖)。按 base-1 硬截(token 预算下通常远不及此)。
        chunks = chunks[: self._MIX_KG_KEY_BASE - 1]
        raise_if_cancelled(cancel_event)
        chunk_block, chunk_id_map = self._chunk_answer_context(
            chunks, budget_chars=10**9, notebook_id=notebook_id)
        if kg_block and kg_block != "(none)":
            context_block = f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
        else:
            context_block = chunk_block
        id_map = {**chunk_id_map, **kg_id_map}
        context_block, id_map = self._append_memory_context(
            context_block, id_map, memory_hits or []
        )
        if baseline_sink is not None:
            baseline_sink.clear()
            baseline_sink.update({
                "context_block": context_block,
                "id_map": dict(id_map),
                "ordered_handles": tuple(id_map),
                "budget_chars": len(context_block),
            })
        llm_client = llm_client or self.model_clients.chat("ask_answer")
        raw = llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(
                question, context_block, history, style_block=style_block)}],
            ANSWER_SCHEMA_HINT,
            cancel_event=cancel_event,
            **cap_kwargs(llm_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = data.get("grounded", False) is True
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def _rewrite_followup_query(
        self,
        history: str,
        question: str,
        cancel_event: CancelEvent = None,
    ) -> str:
        """Resolve an elliptical follow-up into a standalone retrieval query using
        prior turns. Runs whenever there IS history (any non-first turn) — the
        rewrite model itself returns the question unchanged when it's already
        standalone, so we no longer pre-gate with a brittle keyword heuristic.
        Uses the dedicated ``query_rewrite`` workload; always falls back to the
        raw question on any failure."""
        if not history.strip():
            return question
        client = self.model_clients.chat("query_rewrite")
        if not getattr(client, "configured", False):
            return question
        raise_if_cancelled(cancel_event)
        try:
            raw = client.chat_json(
                [{"role": "user", "content": followup_rewrite_prompt(history, question)}],
                FOLLOWUP_REWRITE_SCHEMA_HINT,
                cancel_event=cancel_event,
            )
            data = json.loads(raw)
            if not isinstance(data, dict):
                return question
            rewritten = str(data.get("query", "")).strip()
            return rewritten or question
        except AskCancelled:
            raise
        except Exception:
            return question

    def _refine_context(
        self,
        question: str,
        context_block: str,
        client,
        cancel_event: CancelEvent = None,
        budget_chars: int | None = None,
    ) -> str:
        """问题感知证据精炼:把 context_block 喂给 evidence_refine LLM,抽"相关要点"
        前置成聚焦上下文(参考性,不产生 [k] 锚点)。默认开(kg_query_refine_enabled);
        client 未配/失败/无内容 → 原样返回。所有调用方都必须传入已按
        ``evidence_refine`` workload 绑定的 client。"""
        if not (self.settings.kg_query_refine_enabled
                and getattr(client, "configured", False)
                and context_block.strip() and context_block.strip() != "(none)"):
            return context_block
        raise_if_cancelled(cancel_event)
        from app.services.prompts import evidence_refine_prompt, EVIDENCE_REFINE_SCHEMA_HINT
        ev_block = context_block[: self.settings.query_refine_max_chars]
        try:
            raw = client.chat_json(
                [{"role": "user", "content": evidence_refine_prompt(question, ev_block)}],
                EVIDENCE_REFINE_SCHEMA_HINT,
                timeout=self.settings.reasoning_timeout_seconds,
                max_retries=self.settings.reasoning_max_retries,
                cancel_event=cancel_event)
            rel = json.loads(raw).get("relevant")
            if not isinstance(rel, list):
                rel = []
            rel = [str(x).strip() for x in rel if str(x).strip()]
        except AskCancelled:
            raise
        except Exception:
            rel = []
        if rel:
            focused = ("Focused relevant evidence (for this question):\n"
                       + "\n".join(
                           f"- {x}" for x in
                           rel[: self.settings.query_refine_max_items]
                       )
                       + "\n\n")
            candidate = focused + context_block
            # Refinement is optional metadata.  Never evict already-budgeted,
            # citable evidence merely to prepend a model-generated summary.
            if budget_chars is None or len(candidate) <= budget_chars:
                context_block = candidate
        return context_block

    def _answer_with_retry(
        self, synth, model_label, service="llm"
    ):
        """答案合成有界重试(治思考型模型偶发空 content)。synth() 返回
        (answer, grounded, anchors);answer 空(思考型模型偶把输出预算耗在
        reasoning_content 上→content 空→chat_json 兜底 "{}"→空 answer,不抛异常、
        status=ok)或抛错 → 重试一次("{}" 不入 LLM 缓存,故重试是真·重掷);两次皆空/
        抛错 → emit 一条 model_error(空 content 本身静默不可见,补此条让"检索到却答不出"
        可追踪:前端横幅 + events.jsonl)。返回 (answer, grounded, anchors, ok)。

        **重试成功不留假报警**:第一次尝试失败、第二次拿到完整答案时,把本次调用
        期间新增的那几条响应内 model_error 摘掉。理由与按节合成回退那处同源——
        那次故障已经被同一轮吸收,用户拿到的是一份完整答案,再挂一条红色横幅
        「本次回答可能不完整」是在报一个并没有影响到结果的错误(真机:第一次合成
        耗时 4 分 25 秒返回空 content,重试后答案 78 锚点 / 5829 字,横幅照挂)。
        摘除**只**针对 ``mark`` 之后本次尝试自己记的那几条:同一 run 里更早的其它
        workload 报错(evidence_refine、embedding/rerank 等)一条都不许动。
        两次都失败时全部保留,包括最后那条 empty-content 的 RuntimeError ——
        「检索到却答不出」必须可见。``events.jsonl`` 始终记全:``note_model_error``
        的两个副作用里,只有响应里的这一份是给用户看的。"""
        # sink 是本轮 Ask 的响应内报警列表(ContextVar,请求局部)。直调/离线路径
        # 下它是 None:那时 note_model_error 只写事件日志、不写响应,没有可摘的
        # 东西,保持现状即可。
        sink = _ASK_MODEL_ERRORS.get()
        mark = len(sink) if sink is not None else None
        answer, grounded, anchors = "", False, []
        for _ in range(2):
            try:
                answer, grounded, anchors = synth()
            except AskCancelled:
                raise
            except Exception as exc:
                self.model_errors.note_model_error(
                    "answer", exc, workload_id="ask_answer"
                )
                answer, grounded, anchors = "", False, []
            if answer:
                if mark is not None:
                    # 只摘**本次调用自己**记下的那条 answer 报警,不是「mark 之后
                    # 的一切」。今天 synth() 内部没有别的 note_model_error 调用
                    # (`_refine_context` 静默吞异常),所以两种写法等价——但那是
                    # 一个会被下一次改动打破的巧合:给证据精炼补一条
                    # note_model_error 是很自然的一步,那之后位置式摘除会在
                    # 「首次即成功 + 精炼失败」时把别人的报警一起删掉,正好违反
                    # 「其它 workload 一条都不许动」。按身份过滤让这条前提不必
                    # 靠注释维持。
                    sink[mark:] = [
                        item for item in sink[mark:]
                        if item.get("workload_id") != "ask_answer"
                    ]
                return answer, grounded, anchors, True
        self.model_errors.note_model_error(
            "answer",
            RuntimeError(
                "answer synthesis produced empty content after retry "
                "(reasoning model likely spent output budget on discarded chain-of-thought)"
            ),
            workload_id="ask_answer",
        )
        return answer, grounded, anchors, False

    def _answer_reasoning(
        self,
        notebook_id,
        question,
        top_hits,
        elements,
        history="",
        cancel_event: CancelEvent = None,
        chunks=None,
        chains=None,
        memory_hits=None,
        answer_client=None,
        kg_context_chars: int | None = None,
        chunk_context_chars: int | None = None,
        element_items: int = 6,
        structured_block: str = "",
        structured_map: dict | None = None,
        collection_map_block: str = "",
        counts_sink: dict | None = None,
        baseline_sink: dict | None = None,
        sectioned: bool = False,
        key_offset: int = 0,
        section_title: str = "",
        section_index: int = 0,
        section_total: int = 0,
        style_block: str = "",
    ):
        """Synthesise the reasoning-mode answer. When PPR chunks are present they
        become first-class [k]-citable evidence: chunk segment k1..N + KG reasoning
        chain segment k1001+ (mirrors _answer_mix's keying), with final synthesis
        still handled by ``ask_answer``. Otherwise KG-only (legacy).
        Direct ``SourceElement`` passages use the isolated k4001+ namespace and
        are first-class citations rather than unbound prompt decoration; at most
        ``element_items`` are admitted, chosen by retrieval relevance descending
        (tie-break ``element_id``) rather than insertion order.
        ``collection_map_block`` is the run's deterministic collection counts,
        placed at the head of the source partition and carrying no ``[k]`` id
        (see ``collection_enumeration_answer.collection_map_block``). Context refinement
        uses ``evidence_refine`` while final synthesis uses ``ask_answer``. Returns
        (answer, llm_grounded, anchors, counts) where ``counts`` reports how many
        KG/chunk/element entries actually entered the prompt (``included_kg``/
        ``included_chunks``/``included_elements``).

        按节合成(设计文档 §3.1)复用这同一个装配器。``sectioned`` 是**唯一**的模式
        判定(不看标题真值,见 ``prompts._answer_section_directive``):它开着时
        ``key_offset`` 把本节的每一段 ``[k]`` 号整体平移(见
        ``outline_synthesis.OUTLINE_SECTION_KEY_STRIDE``),prompt 多一段节级指令,
        证据精炼跳过。缺省为「不在按节合成里」,单次合成路径逐字节不变。节模式下
        调用方只传该节绑定的证据,Memory / 推导链 / 集合地图 / 结构化预览一律不传
        ——它们不可被大纲绑定,留在回退路径上(v1 刻意的边界)。``style_block``:
        见 ``_answer_chunks`` 同名参数,单次合成与按节合成共用调用方传入的同一份。"""
        raise_if_cancelled(cancel_event)
        chunks = chunks or []
        chains = chains or []
        chunk_budget = max(0, int(
            self.settings.chunk_answer_budget_chars
            if chunk_context_chars is None else chunk_context_chars
        ))
        kg_budget = max(0, int(
            self.settings.answer_context_budget_chars
            if kg_context_chars is None else kg_context_chars
        ))
        source_context = ""
        source_map: dict = {}
        baseline_admission: dict = {}
        # The collection MAP goes in FIRST, ahead of every other block. It is
        # the smallest thing in the prompt (a fixed header plus one hard-capped
        # counts line) and the only one that cannot be recovered from anywhere
        # else: the reflect model is told to answer a too-large collection with
        # its count instead of paging it, and this is the only path that number
        # takes to the model writing the answer. Appended last it would be the
        # first casualty of a full chunk budget — precisely in the runs that
        # need it.
        if collection_map_block:
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, collection_map_block, {},
                budget_chars=chunk_budget, admission_sink=baseline_admission,
            )
        if structured_block:
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, structured_block, structured_map or {},
                budget_chars=chunk_budget, admission_sink=baseline_admission,
            )
        if chunks:
            # 按相关度降序(_chunk_answer_context 自带 char 预算,保留最相关;跨 PPR run
            # 的归一分仅大致可比,只影响预算边缘取舍,不破坏 [0,1]);chunk 段 k1..N + KG 段
            # k1001+,合并 id_map,两段都可 [k] 引用。无需 _answer_mix 的 base-1 截断:chunk
            # 数 ≤ ppr_top_chunks×(1 seed + _MAX_PPR_RETRIEVES) + 精确查找通道贡献(每次
            # 调用至多 max_sections×max_chunks_per_section=3×12=36,run 内至多 1 seed +
            # _MAX_EXACT_LOOKUPS=3 次调用 → ≤144)≪ _MIX_KG_KEY_BASE(1000)。
            # 同分不再按 chunk_id 打破平手:chunk id 是随机 128 位代理键,而精确
            # 通道整节取齐后那一节的每一块都是 1.0 同分——按 id 排等于在同分组内
            # 随机洗牌,而 `_chunk_answer_context` 的字符预算恰好切在这个组里,
            # 于是「参数表进不进 prompt」成了掷骰子。Python 的稳定排序保留插入序
            # (= 检索序 / 节内文档序),这既是确定的,也正好是该节该被读的顺序。
            ordered = sorted(chunks, key=lambda c: -c.relevance)
            chunk_block, chunk_id_map = self._chunk_answer_context(
                ordered, notebook_id=notebook_id, id_offset=key_offset,
                budget_chars=max(0, chunk_budget - len(source_context)))
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, chunk_block, chunk_id_map,
                budget_chars=chunk_budget, heading="Retrieved chunks",
                admission_sink=baseline_admission,
            )

        # Direct source elements share the Chunk/source partition instead of
        # bypassing its advertised hard ceiling. Admit the top `element_items`
        # by retrieval relevance descending (tie-break element_id) rather than
        # whatever order the retriever happened to collect them in — an
        # insertion-order slice silently drops the most relevant elements
        # whenever the retriever collects more than the cap.
        if elements and len(source_context) < chunk_budget:
            ranked_elements = rank_source_elements(elements, element_items)
            element_block, element_id_map = self.evidence_context.element_context(
                ranked_elements, notebook_id=notebook_id,
                id_offset=key_offset + self._ELEMENT_KEY_BASE,
                budget_chars=max(0, chunk_budget - len(source_context)),
            )
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, element_block, element_id_map,
                budget_chars=chunk_budget, heading="Direct source elements",
                admission_sink=baseline_admission,
            )

        # Reserve the inter-partition separator inside the KG budget so the
        # final evidence block never exceeds kg_context_chars+chunk_context_chars.
        effective_kg_budget = max(0, kg_budget - (2 if source_context else 0))
        kg_context, kg_map = self._answer_context(
            notebook_id, top_hits,
            id_offset=key_offset + (self._MIX_KG_KEY_BASE if chunks else 0),
            budget_chars=effective_kg_budget,
        )
        if kg_context == "(none)":
            kg_context, kg_map = "", {}
        # Captured before Memory/derived-chain blocks merge into kg_map so
        # "included_kg" reports KG objects/relations only, not those other
        # evidence categories.
        included_kg = len(kg_map)
        if memory_hits and len(kg_context) < effective_kg_budget:
            memory_block, memory_map = self.memory_retriever.context(
                memory_hits, id_offset=key_offset + self._MEMORY_KEY_BASE
            )
            kg_context, kg_map = self._bounded_context_append(
                kg_context, kg_map, memory_block, memory_map,
                budget_chars=effective_kg_budget, heading="Confirmed Memory",
                admission_sink=baseline_admission,
            )
        if chains:
            from app.services.kg.follow_chain import render_follow_chain_context
            chain_block, chain_id_map = render_follow_chain_context(
                chains, id_offset=key_offset + 2000,
                active_notebook_id=notebook_id)
            kg_context, kg_map = self._bounded_context_append(
                kg_context, kg_map, chain_block, chain_id_map,
                budget_chars=effective_kg_budget, heading="Derived chains",
                admission_sink=baseline_admission,
            )
        context_block = source_context
        if kg_context:
            context_block = (
                f"{context_block}\n\n{kg_context}" if context_block else kg_context
            )
        id_map = {**source_map, **kg_map}
        total_context_budget = chunk_budget + kg_budget
        answer_client = answer_client or self.model_clients.chat("ask_answer")
        # 按节合成不做证据精炼。两条理由都硬:①精炼是**每次装配一次**模型调用,
        # 节模式下会把 k 次合成变成 2k 次调用——成本合同只承诺了合成那一半;
        # ②精炼是给「上下文太大、要先挑重点」准备的,而一节的切片至多 8 条绑定
        # 证据,本来就没有中段可丢。
        if not sectioned:
            refine_client = self.model_clients.chat("evidence_refine")
            context_block = self._refine_context(
                question, context_block, refine_client, cancel_event,
                budget_chars=total_context_budget,
            )
        context_block = context_block[:total_context_budget]
        # Partition the merged *source* map (structured preview + chunks +
        # elements) by numeric key range rather than trusting chunk_id_map /
        # element_id_map's sizes from before the append: _bounded_context_append
        # can drop an entire block (remaining budget <= prefix) or filter it
        # down to only the ids whose "k<n>:" line actually survived truncation
        # — counting before that step over-reports what really entered the
        # prompt. Elements live in the isolated k4001+ (_ELEMENT_KEY_BASE)
        # namespace; everything else in source_map here is a chunk. A hybrid-
        # scope request CAN pass structured_block (and the collection-map
        # block) together with chunks, but both are appended with an empty id
        # map and contribute no keys, so every low-range key counted here is a
        # surviving chunk line — no ambiguity.
        # 分区判定按**去掉本节偏移之后**的号段来做:节偏移是 10000 的倍数,不减
        # 掉的话第 2 节的 chunk(k10001)会被算成"集合清单"(>=5000),整份 synthesis
        # 计数从第二节起全错。
        included_elements = 0
        included_collections = 0
        for key in source_map:
            try:
                key_num = int(key[1:]) - key_offset
            except (TypeError, ValueError):
                continue
            if key_num >= self._COLLECTION_KEY_BASE:
                included_collections += 1
            elif key_num >= self._ELEMENT_KEY_BASE:
                included_elements += 1
        included_chunks = len(source_map) - included_elements - included_collections
        counts = {
            "included_kg": included_kg,
            "included_chunks": included_chunks,
            "included_elements": included_elements,
            "included_collections": included_collections,
        }
        # Fill the sink BEFORE the model call: when the answer client raises
        # or returns malformed JSON, the synthesis trace must still report the
        # evidence that really was assembled and sent, not zeros
        # (codex PR#391 round-2).
        if counts_sink is not None:
            counts_sink.clear()
            counts_sink.update(counts)
        if baseline_sink is not None:
            baseline_sink.clear()
            baseline_sink.update({
                "context_block": context_block,
                "id_map": dict(id_map),
                "ordered_handles": tuple(id_map),
                "budget_chars": total_context_budget,
                "capture_error_count": int(
                    baseline_admission.get("ambiguous_truncations") or 0
                ),
            })
        raw = answer_client.chat_json(
            [{"role": "user", "content": answer_prompt(
                question, context_block, history,
                sectioned=sectioned,
                section_title=section_title,
                section_index=section_index,
                section_total=section_total,
                style_block=style_block,
            )}],
            ANSWER_SCHEMA_HINT,
            timeout=self.settings.reasoning_timeout_seconds,
            max_retries=self.settings.reasoning_max_retries,
            cancel_event=cancel_event,
            **cap_kwargs(answer_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = data.get("grounded", False) is True
        # 节模式:先按本节号段清洗正文,再解析锚点。
        #
        # 顺序是有讲究的。**清洗必须在解析之前**,因为 `parse_anchors` 对混合组
        # (`[k10001, k1]`)是整组失效的 —— 先解析的话,那一组里合法的 k10001 也拿不到
        # 锚点,而清洗后正文却写着 `[k10001]`,变成一个永远绑不上的标记。先洗再解,
        # 两边说的是同一件事。
        #
        # **清洗本身是必需的**(codex PR#407 R3 P1):按节解析只决定谁能拿到锚点,
        # 而前端是拿正文里的标记去查**合并后**的引用表 —— 不清洗,第二节那个 `[k1]`
        # 会绑到第一节的无关证据上,按节解析防住的误绑从渲染这道后门回来。
        if sectioned:
            answer = _keep_only_section_markers(answer, set(id_map))
        # 锚点按**本节自己的** id_map 解析(节模式下 id_map 只含本节证据)。合并
        # 之后再统一解析拼好的全文是错的:一节写出别节号段的 `[k]`(它根本没见过
        # 那个号)只可能是幻觉,合并解析会把它一本正经地绑到别节的证据上,而按节
        # 解析直接丢弃 —— 这正是号段偏移要买到的东西。合法标记两种口径结果相同
        # (号段互不相交),差别只在幻觉这一种情况上。
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors, counts

    def _answer_reasoning_sections(
        self,
        notebook_id: str,
        question: str,
        slices,
        *,
        history: str,
        limits,
        answer_client,
        cancel_event: CancelEvent = None,
        on_section=None,
        style_block: str = "",
    ):
        """按节合成(设计文档 §3.1):每节一次合成调用,只喂该节绑定的证据。

        返回 ``_SectionedSynthesis``;**任何一节失败就返回 None**,由调用方整体
        回退到单次合成。绝不半篇拼接:少写一节的答案与「这一节没有内容」在屏幕上
        长得一模一样,而实际发生的是一次模型故障 —— 宁可多付一次单次合成,也不
        交付一份看不出缺口的残篇。

        每节沿用 ``_answer_with_retry`` 的重试语义(空 content 重掷一次、两次皆空
        才记 model_error),所以「思考型模型偶发空 content」不会把整轮拖进回退。
        取消异常照常穿透 —— 用户按了中断不是合成失败。

        ``on_section(step)`` 在每节写完后立刻收到一条轻量进度步(不带 ``duration_ms``,
        理由见发送点)。分节合成是整轮里最长的一段(k 次模型调用),不发进度的话实时
        轨迹会在「合成」上静止几分钟。**进度步实时发出、不因后续回退而回收**:那一节
        确实写完了、那笔钱确实付了,事后抹掉等于让轨迹少报整轮做过的工作;回退由收尾
        那条 synthesis 步的 ``outline_fallback`` 说清楚。

        ``style_block``:见 ``_answer_chunks`` 同名参数,原样转发给每一节的
        ``_answer_reasoning`` 调用——同一份风格提示对全篇一致,不按节重新点读。
        """
        from app.services.outline_synthesis import outline_answer_text

        rendered: list = []
        merged_anchors: list = []
        seen_anchor_keys: set = set()
        merged_counts = {
            "included_kg": 0, "included_chunks": 0,
            "included_elements": 0, "included_collections": 0,
        }
        grounded = False
        section_grounding_detail: list = []
        baseline_assemblies: list[dict] = []
        total = len(slices)
        model_label = getattr(answer_client, "model", "")
        for item in slices:
            section_counts: dict = {}
            section_baseline: dict = {}

            def _synth_section(
                item=item,
                section_counts=section_counts,
                section_baseline=section_baseline,
            ):
                text_, grounded_, anchors_, _counts = self._answer_reasoning(
                    notebook_id, question, item.hits, item.elements, history,
                    cancel_event=cancel_event, chunks=item.chunks,
                    answer_client=answer_client,
                    kg_context_chars=limits.kg_context_chars,
                    chunk_context_chars=limits.chunk_context_chars,
                    element_items=limits.answer_element_items,
                    counts_sink=section_counts,
                    baseline_sink=section_baseline,
                    sectioned=True,
                    key_offset=item.key_offset,
                    section_title=item.section.title,
                    section_index=item.index + 1,
                    section_total=total,
                    style_block=style_block,
                )
                return text_, grounded_, anchors_

            text, section_llm_grounded, section_anchors, ok = self._answer_with_retry(
                _synth_section, model_label, service="ask_answer",
            )
            if not ok:
                return None
            baseline_assemblies.append(dict(section_baseline))
            # 分节判定必须在本节自己的证据池与锚点上完成。只留模型自报会绕过
            # classify_evidence 这道真实门;拿合并后的锚点回算又会把另一节的高分
            # 证据借给本节。这个逐节结果既驱动全局确定性降级,也给 v2 留下可测信号。
            from types import SimpleNamespace
            section_evidence = list(item.hits) + list(item.chunks) + [
                SimpleNamespace(
                    object_id=element.element_id,
                    relevance=float(element.score or 0.0),
                )
                for element in item.elements
            ]
            section_level, _ = classify_evidence(
                section_evidence,
                section_anchors,
                section_llm_grounded,
                self.settings.evidence_tau_low,
                self.settings.evidence_tau_high,
            )
            section_is_grounded = section_level == "grounded"
            section_grounding_detail.append({
                "id": item.section.id,
                "title": item.section.title[:60],
                "grounded": section_is_grounded,
                "evidence_level": section_level,
            })
            if on_section is not None:
                # 复用 synthesis 这个 step_type:前端 reasoning-trace.ts 的
                # synthesis 分支只在 `detail.anchors` 是数字时给出细节文案,进度步
                # 不带 anchors,于是它落到「第 i/共 N 节」那条进度分支上。
                #
                # **刻意不带 duration_ms**(codex PR#407 R1 P2):前端的轨迹总耗时
                # 是**所有**步的 duration_ms 求和,而末尾那条总 synthesis 步已经
                # 独家记了从 `synthesis_started` 起的整段合成时间——进度步再各记
                # 一份,分节答案的合成耗时就报成约两倍(回退 run 还会把已完成节的
                # 那几次尝试也算进去)。进度步是**进度标记**,不是独立的耗时区间;
                # 「合成耗时必须进轨迹总耗时」那条红线由总步承担,恰好一次。
                on_section(TraceStep(
                    step_type="synthesis",
                    # 措辞与前端 reasoning-trace.ts 的进度细节分支同款
                    # (「第 i/共 N 节」),summary 与 detail 读起来才是一句话。
                    summary=f"已写完第 {item.index + 1}/共 {total} 节",
                    detail={
                        "section_index": item.index + 1,
                        "section_total": total,
                        "section_title": item.section.title[:60],
                    },
                ))
            for key in merged_counts:
                merged_counts[key] += int(section_counts.get(key, 0) or 0)
            rendered.append((item, text))
            grounded = grounded or section_llm_grounded
            # 号段互不相交,所以跨节不可能撞 key;去重只防同一节内重复标记(
            # parse_anchors 自己已经去过一次,这一层是拼接侧的常数级防御)。
            for anchor in section_anchors:
                if anchor.key in seen_anchor_keys:
                    continue
                seen_anchor_keys.add(anchor.key)
                merged_anchors.append(anchor)
        return _SectionedSynthesis(
            answer=outline_answer_text(rendered),
            # 这里仅汇总模型自报供全局 classify_evidence 使用;真正的逐节判定已经
            # 独立落在 section_grounding,后续整体档位再按其全/部分/零支撑确定性封顶。
            llm_grounded=grounded,
            anchors=merged_anchors,
            sections=len(rendered),
            counts=merged_counts,
            section_grounding=section_grounding_detail,
            baseline_assemblies=baseline_assemblies,
        )

    def _unconfigured_model_response(self, notebook_id: str, question: str,
                                     conversation_id: str, mode: str,
                                     *, user_id: str, job_id: str = "",
                                     asked_at: str = "",
                                     intent: QueryIntentContract | None = None,
                                     retrieval_query: str = "",
                                     retrieval_effort: RetrievalEffort = "standard",
                                     completeness_unavailable: bool = False,
                                     reasoning_trace: "list[TraceStep] | None" = None,
                                     persist: bool = True,
                                     ) -> AskResponse:
        """系统模型已启用但漏绑问答工作负载时的统一短路响应。

        ``reasoning_trace`` 带上调用方**已经推送给客户端**的那几步:短路响应一旦
        作为 final 事件替换掉在途 turn,不带轨迹就等于把用户刚看着走过的几步
        当场抹掉,历史里也留不下。"""
        msg = "系统未配置当前问答所需的模型服务，请联系维护人员"
        if completeness_unavailable:
            msg = (
                "当前精确完整枚举支持 Knowhow 整表物理行清单与直接行计数，"
                "以及元素清单（公式/表格/图片/代码块）、知识对象清单与来源清单；"
                "本次请求不能视为全部结果。\n\n"
                + msg
            )
        response = AskResponse(
            answer_id="", conclusion=msg, conversation_id=conversation_id,
            retrieval_query=retrieval_query or question, llm_mode="deterministic",
            intent=intent, retrieval_effort=retrieval_effort,
            reasoning_trace=reasoning_trace or None)
        response.mode = mode
        response.model_errors = [
            ModelError(stage="answer", model="", message="missing_config")
        ]
        if persist:
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id,
                user_id=user_id, job_id=job_id, asked_at=asked_at)
        return response

    # ------------------------------------------------------------------
    # chunk engine
    # ------------------------------------------------------------------

    def ask_chunk(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        job_id: str = "",
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """chunk-native 通用问答:大召回 → MMR 多样性精选 → 长上下文综合 →
        引用绑回 chunk。KG 不参与(严格推理走 ask_reasoning)。"""
        ask_started = time.perf_counter()

        def ask_stage(name: str, started: float, **extra) -> None:
            self.event_log.emit({
                "kind": "ask_stage", "notebook_id": notebook_id, "stage": name,
                "latency_ms": round((time.perf_counter() - started) * 1000), **extra,
            })

        self.notebooks.get_notebook(notebook_id)
        question = payload.question.strip()
        raise_if_cancelled(cancel_event)
        turn = self._prepare_turn(
            notebook_id,
            payload.conversation_id,
            question,
            user_id=user_id,
            job_id=job_id,
        )
        conversation_id, history = turn.conversation_id, turn.history
        from app.services.source_scope import scoped_conversation_history

        # A previous answer may contain evidence from sources that are no
        # longer selected.  Do not let it influence rewrite, retrieval, or
        # synthesis under the new source ceiling.
        history = scoped_conversation_history(history)
        raise_if_cancelled(cancel_event)
        # Agentic Memory P3(T8):合成侧的风格提示,一次点读、贯穿本次 ask_chunk
        # 用到的每个 answer_prompt 调用(见 _search_profile_style_block)。
        style_block = self._search_profile_style_block(user_id)
        retrieval_query = self._rewrite_followup_query(history, question, cancel_event)
        memory_hits = self._memory_hits(user_id, notebook_id, retrieval_query)

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        try:
            if self._primary_llm_unconfigured():
                self.model_errors.note_model_error(
                    "answer",
                    ModelNotConfiguredError(
                        "系统未配置当前问答所需的模型服务，请联系维护人员"
                    ),
                    workload_id="ask_answer",
                )
            _t = time.perf_counter()
            from app.services.query_rewrite import expand_query
            from app.services.retrieval import (
                est_tokens,
                partition_generated_question_chunks,
                quota_fuse_baseline_first,
            )
            ex = None
            raise_if_cancelled(cancel_event)
            if self.settings.query_rewrite_enabled:
                ex = expand_query(self.model_clients.chat("query_rewrite"),
                                  retrieval_query, history,
                                  max_subqueries=self.settings.chunk_max_subqueries,
                                  corpus_langs=self.candidates.notebook_languages(notebook_id),
                                  # codex #535 R7 P2:chunk 规划同样收风格块,
                                  # 否则「注入规划与合成两处」只对 reasoning 成立。
                                  style_block=style_block,
                                  cancel_event=cancel_event)
                sub_queries = [s.query for s in ex.sub_queries]
            else:
                sub_queries = [retrieval_query]
            # 对比题:焦点兄弟追加为子查询(chunk 无 agent 循环,借 expand 的 comparison
            # 字段触发)。共提优先、社区回退(resolve_comparison_peers)。无 base → 跳过。
            # 共提兄弟不应被社区层开关单独关死——P2 实测社区层对兄弟无效,操作员关
            # community_layer 时 mention 桥仍需生效(与 reasoning 分支行为对齐)。
            if ex and ex.comparison and (self.settings.community_layer_enabled
                                          or self.settings.mention_bridge_enabled):
                communities = self.communities()
                base_ids = communities.mounted_base_ids(notebook_id)
                for base_nb in base_ids:
                    peers, _src = communities.resolve_comparison_peers(
                        base_nb, ex.comparison["focal"], retrieval_query,
                        top_k=self.settings.community_peers_topk,
                        candidates=self.settings.community_rerank_candidates)
                    for pname in peers:
                        if pname not in sub_queries:
                            sub_queries.append(pname)
            hl = " ".join(ex.high_level_keywords) if ex else ""
            # Bilingual keyword string (high+low level, both corpus languages) for
            # the CHUNK lexical union — this is how "FTS carries the 2nd language"
            # reaches chunks (not just the KG-name/relation FTS). Computed ONCE;
            # merged into whichever candidate branch runs below (dedup by chunk_id).
            kw_str = " ".join(ex.high_level_keywords + ex.low_level_keywords) if ex else ""
            kw_hits = (self.candidates.keyword_chunk_candidates(notebook_id, kw_str)
                       if kw_str.strip() else [])
            # Exact-identifier fast path: when the question names a full command
            # (`set_db`), locate its section precisely and take the WHOLE section,
            # so the 600-char chunker cannot hand back the prose while dropping the
            # Arguments table. Zero model calls; a question without an identifier
            # does no I/O at all. Computed ONCE per ask, like kw_hits above, and
            # merged into whichever candidate branch runs below.
            exact_hits = (self.candidates.exact_lookup_chunks(notebook_id, retrieval_query)
                          if self.settings.exact_lookup_enabled else [])
            exact_ids = {c.chunk_id for c in exact_hits}
            ask_stage("expand_query", _t, n=len(sub_queries))

            # ── 检索 + 选择 ──
            # mix(overlay 开 + rerank 配齐 + 有 KG):三路并池 → rerank 排序 → token 预算截。
            # 否则走现状 chunk-only(MMR / quota_fuse),与历史字节等价。
            # W2.2:一次读齐 chunk-path flag/knob → 不可变 plan;overlay_on / strategy /
            # 各 knob 下面统一读 plan(不再就地读 self.settings)。plan.overlay_on 与旧三元
            # AND 逐字等价,答案/引用分支继续按 overlay_on 分派。
            plan = self.candidates.chunk_plan(notebook_id, sub_queries)
            overlay_on = plan.overlay_on
            kg_block, kg_id_map, kg_hits = "", {}, []
            baseline_chunk_candidates = []
            _t = time.perf_counter()
            raise_if_cancelled(cancel_event)
            if plan.strategy == "mix":
                candidates, kg_block, kg_id_map, kg_hits, concept_walk_n = (
                    self.candidates.mixed_chunk_candidates(
                        notebook_id, retrieval_query, hl, sub_queries))
                # Direct historical producers replace a question-only
                # canonical row on collision, so the optional score/position
                # cannot influence rerank tie order or token truncation.
                candidates = _merge_direct_chunk_hits(candidates, kw_hits)
                candidates = _merge_direct_chunk_hits(candidates, exact_hits)
                raise_if_cancelled(cancel_event)
                baseline_candidates, supplemental_candidates = (
                    partition_generated_question_chunks(candidates)
                )
                rerank_client = self.model_clients.rerank("retrieval_rerank")
                order = rerank_client.rerank(
                    retrieval_query, [c.text for c in baseline_candidates],
                    on_error=lambda e: self.model_errors.note_model_error(
                        "rerank",
                        e,
                        workload_id="retrieval_rerank",
                    ))
                raise_if_cancelled(cancel_event)
                ranked = [baseline_candidates[i] for i in order]
                if supplemental_candidates:
                    supplemental_order = rerank_client.rerank(
                        retrieval_query,
                        [c.text for c in supplemental_candidates],
                        on_error=lambda e: self.model_errors.note_model_error(
                            "rerank",
                            e,
                            workload_id="retrieval_rerank",
                        ),
                    )
                    ranked.extend(
                        supplemental_candidates[i] for i in supplemental_order
                    )
                baseline_chunk_candidates = list(baseline_candidates)
                kg_budget = self.settings.max_entity_tokens + self.settings.max_relation_tokens
                untruncated_kg_block = kg_block
                kg_block = self.evidence_context.truncate_kg_block(kg_block, kg_budget)
                baseline_kg_truncated = kg_block != untruncated_kg_block
                chunk_budget = max(0, self.settings.max_total_tokens
                                   - est_tokens(kg_block) - self._MIX_PROMPT_BUFFER_TOKENS)
                from app.services.retrieval import (
                    exact_section_reserve_rule, graph_reserve_rule,
                    select_with_reserves_baseline_first,
                )

                # Two floors inside ONE budget. The reranker is a general
                # relevance model and routinely ranks an Arguments table below
                # prose that merely talks about the command, so without a
                # reserved seat the exact section is assembled and then
                # truncated away. Neither reserve enlarges the budget nor
                # evicts what the other is holding; a reserve set to 0 is fully
                # inert, so `chunk_graph_reserve` keeps its historical
                # behaviour byte-for-byte.
                selected = select_with_reserves_baseline_first(ranked, chunk_budget, (
                    graph_reserve_rule(max(0, self.settings.chunk_graph_reserve)),
                    exact_section_reserve_rule(
                        max(0, self.settings.exact_section_reserve), exact_ids),
                ))
                ask_stage("mix_rerank", _t, recall=len(candidates),
                          selected=len(selected), kg_nodes=len(kg_id_map),
                          concept_walk=concept_walk_n)
            elif plan.strategy == "multi":
                baseline_kg_truncated = False
                collected, per_query, _ids, _mat = (
                    self.candidates.retrieve_chunk_candidates_multi(notebook_id, sub_queries))
                raise_if_cancelled(cancel_event)
                # ∪ bilingual-keyword chunk hits: merge into collected (best relevance)
                # and add as an extra per_query group so quota_fuse can surface them.
                if kw_hits:
                    _merge_multi_direct_chunk_hits(collected, kw_hits)
                    per_query = per_query + [{c.chunk_id: c for c in kw_hits}]
                # ∪ exact-identifier whole-section hits, treated identically:
                # its own per_query group is what gives quota_fuse a reason to
                # surface a section chunk whose standalone relevance is low.
                if exact_hits:
                    _merge_multi_direct_chunk_hits(collected, exact_hits)
                    per_query = per_query + [{c.chunk_id: c for c in exact_hits}]
                baseline_chunk_candidates, _supplemental_candidates = (
                    partition_generated_question_chunks(list(collected.values()))
                )
                selected, _counts = quota_fuse_baseline_first(
                    collected,
                    per_query,
                    plan.fuse_k,
                    relevance=lambda c: c.relevance,
                )
                ask_stage("retrieve_fuse", _t, recall=len(collected), selected=len(selected))
            else:
                baseline_kg_truncated = False
                scored, ids, mat = self.candidates.retrieve_chunk_candidates(
                    notebook_id, sub_queries[0])
                # Match feature-off MMR input when a direct producer collides
                # with a question-only supplement.
                scored = _merge_direct_chunk_hits(scored, kw_hits)
                scored = _merge_direct_chunk_hits(scored, exact_hits)
                baseline_chunk_candidates, _supplemental_candidates = (
                    partition_generated_question_chunks(scored)
                )
                raise_if_cancelled(cancel_event)
                selected = self.candidates.select_chunk_candidates(
                    scored, ids, mat, plan.mmr_k, plan.mmr_lambda)
                ask_stage("retrieve_mmr", _t, recall=len(scored), selected=len(selected))

            historical_selected = list(selected)
            selected, source_graph_status = self._activate_selected_source_graph(
                notebook_id,
                historical_selected,
                top_hits=kg_hits,
                max_results=self.settings.ppr_top_chunks,
            )

            answer, llm_grounded, anchors = "", False, []
            synth_failed = False
            chunk_baseline: dict = {}
            _t = time.perf_counter()
            raise_if_cancelled(cancel_event)
            answer_client = self.model_clients.chat("ask_answer")
            if answer_client.configured and (selected or kg_id_map or memory_hits):
                # 空 content 有界重试 + 诚实降级 + 可观测(见 _answer_with_retry docstring)。
                answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                    lambda: (self._answer_mix(
                                 question, selected, kg_block, kg_id_map, history,
                                 cancel_event=cancel_event, notebook_id=notebook_id,
                                 memory_hits=memory_hits, llm_client=answer_client,
                                 baseline_sink=chunk_baseline, style_block=style_block)
                             if overlay_on else
                             self._answer_chunks(
                                 question, selected, history, cancel_event=cancel_event,
                                 notebook_id=notebook_id, memory_hits=memory_hits,
                                 llm_client=answer_client,
                                 baseline_sink=chunk_baseline, style_block=style_block)),
                    getattr(answer_client, "model", ""))
                synth_failed = not _ok
            ask_stage("answer_llm", _t)

            # 引用绑回 chunk。mix:绑到被答案引用的 chunk anchor(候选池大,不可全列)。
            # 非 mix:每个精选 chunk 一条(字节等价于历史)。
            # tier:selected 通常单库(personal),但概念漫游(PPR,第三路 merge 进
            # _mix_retrieve 的 candidates)可掺 base 库 chunk——那些 c.notebook_id
            # 非空;其余(单库路径)留空,回退本次 ask 的 notebook_id。一次批量
            # 查询解出 {notebook_id: tier},citations 数量再多也只查一次。
            citations: List[Citation] = []
            raise_if_cancelled(cancel_event)
            chunk_tier_map = self._tier_map_for(
                {c.notebook_id or notebook_id for c in selected})
            def _chunk_tier(c) -> str:
                return chunk_tier_map.get(c.notebook_id or notebook_id, "personal")
            # Task 14 codex r4 fix: c.notebook_id 同样会被 PPR(_mix_retrieve 第三路
            # 概念漫游,merge 进 selected 的 chunk)对 active 库自己的命中打上 active
            # 自己的 id,并非只在跨库命中时才打标——citations_from 同一根因的镜像
            # 修复(见 evidence_context.py citations_from 的 codex r4 fix 注释)。这
            # 里直接构造 Citation(不经 citations_from),必须同样与调用方
            # notebook_id 比较,相等则归零,否则前端会显示一个多余的「来自「当前
            # 笔记本」」徽章。
            def _cite_notebook_id(c) -> str:
                return c.notebook_id if c.notebook_id != notebook_id else ""
            # Task 12b（引用跳转扩面）：chunk 模式此前从未富化过
            # citation.knowhow（此前只有 reasoning 模式的 citations_from 会查）
            # ——同池同权补上。批量查一次 knowhow 定位标签，覆盖 selected 里
            # 每个 chunk 的首个 element_id；不管 mix/plain 哪条分支最终建多少
            # 条引用，只发生一次 store 读取（运行效率是一等约束，镜像
            # citations_from 的批量口径）。
            knowhow_refs = self.evidence_context.knowhow_refs_for(
                c.element_ids[0] for c in selected if c.element_ids)
            citation_source_info = self.evidence_context.citation_source_info(
                c.source_id for c in selected
            )
            # 本段附图: 每条 chunk 引用的候选是该 chunk 的**整个** element_ids,
            # 不是上面 knowhow 用的首个 eid —— 配图元素通常不是 chunk 的第一个
            # 元素。引用与它的 chunk 在这里配好对,因为 Citation 自己不带 chunk_id。
            citation_image_targets: List[tuple[Citation, Sequence[str]]] = []
            if overlay_on:
                by_id = {c.chunk_id: c for c in selected}
                for a in anchors:
                    if a.object_type == "chunk" and a.object_id in by_id:
                        c = by_id[a.object_id]
                        eid = c.element_ids[0] if c.element_ids else ""
                        source_info = citation_source_info.get(c.source_id) or {}
                        source_title = source_info.get("title", c.source_title)
                        citation = Citation(
                            label=f"{source_title} · {c.section_path}".strip(" ·"),
                            source_id=c.source_id, element_id=eid,
                            location_label=c.section_path, quoted_span=c.text[:200],
                            source_file_name=source_info.get("file_name", ""),
                            tier=_chunk_tier(c), notebook_id=_cite_notebook_id(c),
                            knowhow=knowhow_refs.get(eid))
                        citations.append(citation)
                        citation_image_targets.append((citation, c.element_ids))
            else:
                for c in selected:
                    eid = c.element_ids[0] if c.element_ids else ""
                    source_info = citation_source_info.get(c.source_id) or {}
                    source_title = source_info.get("title", c.source_title)
                    citation = Citation(
                        label=f"{source_title} · {c.section_path}".strip(" ·"),
                        source_id=c.source_id, element_id=eid,
                        location_label=c.section_path, quoted_span=c.text[:200],
                        source_file_name=source_info.get("file_name", ""),
                        tier=_chunk_tier(c), notebook_id=_cite_notebook_id(c),
                        knowhow=knowhow_refs.get(eid))
                    citations.append(citation)
                    citation_image_targets.append((citation, c.element_ids))
            citations.extend(self._memory_citations(anchors, memory_hits))
            # 锚点与引用一次调用(共享每答案预算,见 attach_citation_images
            # docstring),一次 store 读取。
            self.evidence_context.attach_citation_images(
                anchor_image_targets(
                    anchors, {c.chunk_id: c.element_ids for c in selected}
                ) + citation_image_targets
            )

            # grounding 在 chunk∪KG 合并集上;各项用其融合 relevance(rerank 分不参与)。
            combined_hits = list(selected) + list(kg_hits) + list(memory_hits)
            evidence_level, top_relevance = classify_evidence(
                combined_hits, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
            grounded = evidence_level == "grounded"

            from app.services.retrieval_baseline import (
                build_retrieval_baseline_manifest,
            )
            _baseline_manifest = build_retrieval_baseline_manifest(
                notebook_id=notebook_id,
                query=retrieval_query,
                mode="chunk",
                settings=self.settings,
                candidate_knowledge=kg_hits,
                candidate_chunks=baseline_chunk_candidates,
                selected_knowledge=kg_hits,
                selected_chunks=historical_selected,
                final_context_block=str(chunk_baseline.get("context_block") or ""),
                final_id_map=dict(chunk_baseline.get("id_map") or {}),
                final_ordered_handles=tuple(
                    chunk_baseline.get("ordered_handles") or ()
                ),
                final_budget_chars=int(chunk_baseline.get("budget_chars") or 0),
                capture_error_count=1 if baseline_kg_truncated else 0,
            )

            if answer:
                conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                llm_mode = "grounded" if grounded else "ungrounded"
            elif synth_failed:
                # 诚实降级:LLM 跑了但没产出答案(空 content 或抛错)——不冒充成
                # "Retrieved N passage(s)" 那样的成功样子;如实说明并保留下方证据(citations)。
                llm_mode = "synthesis_failed"
                conclusion = (
                    f"已检索到 {len(selected)} 条相关内容,但本次答案合成未产出内容"
                    "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。"
                    if selected else
                    "本次答案合成未产出内容,请重试该问题。")
            else:
                # deterministic:未配 LLM(synth 未跑)→ 有内容仍如实报「检索到 N 段」。
                llm_mode = "deterministic"
                conclusion = (
                    f"Retrieved {len(selected)} relevant passage(s) for this question."
                    if selected else
                    "No indexed content matches this question yet. Upload sources or build chunks.")

            response = AskResponse(
                answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                retrieval_query=retrieval_query, top_relevance=top_relevance)
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
        response.mode = "chunk"
        response.model_errors = [ModelError(**e) for e in _err_sink]
        raise_if_cancelled(cancel_event)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id,
            user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
        ask_stage("total", ask_started)
        return response

    # ------------------------------------------------------------------
    # reasoning engine
    # ------------------------------------------------------------------

    def ask_reasoning(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        job_id: str = "",
        on_trace=None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """Compatibility entry for the immutable reasoning application stage."""
        from app.application.ask_reasoning import (
            ReasoningRetrievalRuntime,
        )
        from app.services.retrieval_run import current_retrieval_run
        from app.services.source_scope import current_source_scope

        runtime = ReasoningRetrievalRuntime(
            scope=current_source_scope(),
            retrieval_run=current_retrieval_run(),
            cancellation=cancel_event,
            trace_sink=on_trace,
            connection_probe=self.retrieval_connection_probe,
        )
        prepared = self._prepare_reasoning_ask(
            notebook_id, payload, user_id=user_id, job_id=job_id,
            runtime=runtime,
        )
        return self._run_reasoning_stage(prepared, runtime).response

    def _assert_reasoning_runtime(
        self, runtime, point: str, *, notebook_id: str, user_id: str,
    ) -> None:
        from app.application.ask_reasoning import (
            ReasoningRetrievalRuntime,
            StageBoundaryError,
        )
        from app.services.retrieval_run import current_retrieval_run
        from app.services.source_scope import current_source_scope

        if type(runtime) is not ReasoningRetrievalRuntime:
            raise StageBoundaryError(f"invalid Ask reasoning runtime at {point}")
        if type(notebook_id) is not str or not notebook_id:
            raise StageBoundaryError(
                f"invalid Ask reasoning notebook authority at {point}"
            )
        if type(user_id) is not str or not user_id:
            raise StageBoundaryError(
                f"invalid Ask reasoning actor authority at {point}"
            )
        if runtime.cancellation is not None and not isinstance(
            runtime.cancellation, threading.Event
        ):
            raise StageBoundaryError(
                f"invalid Ask reasoning cancellation authority at {point}"
            )
        scope = current_source_scope()
        run = current_retrieval_run()
        if scope is not runtime.scope:
            raise StageBoundaryError(f"Ask reasoning scope changed at {point}")
        if run is not runtime.retrieval_run:
            raise StageBoundaryError(
                f"Ask reasoning retrieval run changed at {point}"
            )
        if runtime.connection_probe is not self.retrieval_connection_probe:
            raise StageBoundaryError(
                f"Ask reasoning connection authority changed at {point}"
            )
        if scope is not None:
            scope_notebook_id = getattr(scope, "notebook_id", None)
            if (
                type(scope_notebook_id) is not str
                or not scope_notebook_id
                or scope_notebook_id != notebook_id
            ):
                raise StageBoundaryError(
                    f"Ask reasoning scope notebook changed at {point}"
                )
        if run is not None:
            run_kind = getattr(run, "run_kind", None)
            if type(run_kind) is not str or run_kind != "ask_reasoning":
                raise StageBoundaryError(
                    f"Ask reasoning run kind changed at {point}"
                )
            if getattr(run, "cancel_event", None) is not runtime.cancellation:
                raise StageBoundaryError(
                    f"Ask reasoning cancellation authority changed at {point}"
                )
            actor_id = getattr(run, "actor_id", None)
            if (
                type(actor_id) is not str
                or not actor_id
                or actor_id != user_id
            ):
                raise StageBoundaryError(
                    f"Ask reasoning actor authority changed at {point}"
                )
        checker = getattr(runtime.connection_probe, "is_connection_held", None)
        if runtime.connection_probe is not None and not callable(checker):
            raise StageBoundaryError(
                f"invalid Ask reasoning connection probe at {point}"
            )
        if runtime.trace_sink is not None and not callable(runtime.trace_sink):
            raise StageBoundaryError(
                f"invalid Ask reasoning trace sink at {point}"
            )
        if callable(checker):
            try:
                held = checker()
            except Exception as exc:
                raise StageBoundaryError(
                    f"Ask reasoning connection probe failed at {point}"
                ) from exc
            if type(held) is not bool:
                raise StageBoundaryError(
                    f"invalid Ask reasoning connection state at {point}"
                )
            if held:
                raise StageBoundaryError(
                    f"Ask reasoning holds a database connection at {point}"
                )

    def _prepare_reasoning_ask(
        self, notebook_id, payload, *, user_id, job_id, runtime,
    ):
        from app.application.ask_reasoning import (
            PreparedReasoningAsk,
            ReasoningIntentProjection,
        )

        self._assert_reasoning_runtime(
            runtime, "prepare", notebook_id=notebook_id, user_id=user_id,
        )
        cancel_event = runtime.cancellation
        self.notebooks.get_notebook(notebook_id)
        question = payload.question.strip()
        raise_if_cancelled(cancel_event)
        self.validate_reasoning_submission(notebook_id, payload)
        turn = self._prepare_turn(
            notebook_id,
            payload.conversation_id,
            question,
            user_id=user_id,
            job_id=job_id,
        )
        conversation_id, history = turn.conversation_id, turn.history
        from app.services.source_scope import scoped_conversation_history

        history = scoped_conversation_history(history)
        raise_if_cancelled(cancel_event)
        # Agentic Memory P3(T8):合成侧的风格提示,一次点读、贯穿本次 ask_reasoning
        # 的单次合成与按节合成两条路径(见 _search_profile_style_block)。
        style_block = self._search_profile_style_block(user_id)
        intent_contract = self._confirmed_reasoning_intent(payload, history)
        reasoning_history = history
        limits = ask_retrieval_limits(payload.retrieval_effort)
        from app.services.query_intent import (
            confirmed_intent_queries,
            confirmed_research_question,
        )
        auto_confirmed_clear_intent = bool(
            payload.intent is not None
            and not payload.intent.contract.needs_clarification
            and not payload.intent.answers
        )
        research_question = confirmed_research_question(
            intent_contract.model_dump(),
            question,
            objective_is_authoritative=auto_confirmed_clear_intent,
        )
        intent_queries = (
            confirmed_intent_queries(
                intent_contract.model_dump(),
                question,
                # The first slot is always the whole confirmed question; the
                # remaining slots are the reviewed directions.  Ask for one
                # seed per mandatory topic even when that exceeds this effort's
                # first-round width: the effort limit bounds first-round
                # *concurrency*, and the retriever defers the overflow into its
                # bounded coverage pass (run inside the same step budget, and
                # disclosed in the trace when that budget runs out).  Capping
                # the request at the first-round width instead would discard the
                # later topics before the retriever ever sees them, which is
                # what made "every mandatory topic gets a seed" false at low
                # efforts.  The ceiling remains the contract's own bound
                # (1 whole question + at most 16 mandatory topics).
                max_queries=max(
                    limits.max_initial_subqueries,
                    1 + len(intent_contract.mandatory_topics),
                ),
                objective_is_authoritative=auto_confirmed_clear_intent,
            )
            if payload.intent is not None else []
        )
        intent_projection = ReasoningIntentProjection(
            resolved_question=intent_contract.resolved_question,
            result_scope=intent_contract.result_scope,
            completeness_required=intent_contract.completeness_required,
            retrieval_effort=payload.retrieval_effort,
            entities=tuple(intent_contract.entities),
            constraints=tuple(intent_contract.constraints),
            excluded_topics=tuple(intent_contract.excluded_topics),
            assumptions=tuple(intent_contract.assumptions),
            expected_output=intent_contract.expected_output,
            mandatory_topics=tuple(
                topic.question for topic in intent_contract.mandatory_topics
            ),
        )
        intent_step = TraceStep(
            step_type="intent",
            summary="已按确认后的问题理解开始检索",
            detail=intent_projection.as_json_mapping(),
            # The understanding phase runs entirely in ``/ask/intent``, before
            # this durable job exists, so the server cannot time it.  The UI
            # reports what it measured; without it the replayed trace would
            # silently drop that whole phase from the run's total.
            duration_ms=(
                payload.intent.understanding_ms if payload.intent is not None else None
            ),
        )
        return PreparedReasoningAsk(
            notebook_id=notebook_id,
            question=question,
            conversation_id=conversation_id,
            history=reasoning_history,
            style_block=style_block,
            intent_json=intent_contract.model_dump_json(),
            research_question=research_question,
            intent_queries=tuple(intent_queries),
            limits=limits,
            intent_projection=intent_projection,
            intent_trace_duration_ms=intent_step.duration_ms,
            user_id=user_id,
            job_id=job_id,
            asked_at=payload.asked_at,
            retrieval_effort=payload.retrieval_effort,
        )

    def _commit_reasoning_draft(self, draft, runtime, prepared):
        """The only reasoning answer persistence boundary.

        Beyond ``mode``, this re-verifies every identity field the draft
        envelope carries against ``prepared`` -- the same frozen input every
        stage (shipped or injected) actually received.  A stage constructs
        ``ReasoningResponseDraft`` itself, so nothing upstream of this
        boundary previously stopped an injected implementation from writing
        the answer into a *different* job or conversation than the one it was
        asked to serve; even a benign implementation that left a field at its
        dataclass default would silently persist inconsistent payload
        metadata (codex #571 R2 P2).  ``notebook_id``/``user_id`` also get a
        narrower cross-check a few lines down from ``_assert_reasoning_runtime``
        (against the retrieval run actor and, when the request scoped
        sources, the source scope's own notebook) -- this sweep is the one
        place all six fields are compared against the same source of truth
        regardless of whether those narrower authorities happen to be armed.

        The cancellation checkpoint is deliberately before the atomic save.
        Once that save succeeds, the completed answer wins; a late token must
        not turn a committed job back into a cancelled response.
        """
        from app.application.ask_reasoning import (
            CommittedReasoningAnswer,
            PreparedReasoningAsk,
            ReasoningResponseDraft,
            ReasoningRetrievalRuntime,
            StageBoundaryError,
        )

        if type(draft) is not ReasoningResponseDraft:
            raise StageBoundaryError("invalid reasoning response draft")
        if type(runtime) is not ReasoningRetrievalRuntime:
            raise StageBoundaryError("invalid reasoning commit runtime")
        if type(prepared) is not PreparedReasoningAsk:
            raise StageBoundaryError("invalid reasoning commit prepared input")
        if type(draft.response) is not AskResponse:
            raise StageBoundaryError("invalid reasoning response graph")
        if draft.response.mode != "reasoning":
            raise StageBoundaryError("response draft changed the ask mode")
        if draft.notebook_id != prepared.notebook_id:
            raise StageBoundaryError("response draft changed notebook_id")
        if draft.question != prepared.question:
            raise StageBoundaryError("response draft changed question")
        if draft.conversation_id != prepared.conversation_id:
            raise StageBoundaryError("response draft changed conversation_id")
        if draft.user_id != prepared.user_id:
            raise StageBoundaryError("response draft changed user_id")
        if draft.job_id != prepared.job_id:
            raise StageBoundaryError("response draft changed job_id")
        if draft.asked_at != prepared.asked_at:
            raise StageBoundaryError("response draft changed asked_at")
        # The payload/returned AskResponse carries its own conversation_id --
        # the browser adopts it via setConversationId(response.conversation_id)
        # -- so an injected stage that leaves it blank or points it elsewhere
        # would detach the next turn even though the row itself is saved under
        # the prepared conversation (codex #571 R3 P2).
        if draft.response.conversation_id != prepared.conversation_id:
            raise StageBoundaryError("response draft changed the response conversation_id")
        response = draft.response
        self._assert_reasoning_runtime(
            runtime,
            "before-persist",
            notebook_id=draft.notebook_id,
            user_id=draft.user_id,
        )
        raise_if_cancelled(runtime.cancellation)
        response.answer_id = self._save_answer(
            draft.notebook_id,
            draft.question,
            response,
            draft.conversation_id,
            user_id=draft.user_id,
            job_id=draft.job_id,
            asked_at=draft.asked_at,
        )
        return CommittedReasoningAnswer(
            response=response,
            baseline_manifest=draft.baseline_manifest,
        )

    def _consult_gap_sources(
        self,
        prepared,
        limits,
        trace,
        *,
        top_hits,
        chunks,
        elements,
        cancellation,
        sink,
        on_step,
    ):
        """Ask deployment plugins what exists OUTSIDE this notebook.

        Called once per reasoning run, **after the draft stage returned** and
        before persistence, and only when the run has something to admit to:
        a confirmed direction it never executed, or an evidence pool thinner
        than this effort tier's own ``ranked_final_floor``.  Both conditions
        are read off what the run had already produced before drafting
        (``trace`` here is the pre-draft list, deliberately not the response's
        own trace — an injected stage may rewrite that one) — no extra query,
        no extra model call, and nothing at all when neither holds.

        What comes back is **not evidence**, which is why nothing of it — not
        the suggestions, and not even the disclosure step that says a
        consultation happened — goes through ``ResponseDraftInput``: the step
        lands in ``sink`` (the caller points it at the response that will be
        persisted), so a drafting stage, injected or not, structurally cannot
        vary the prose on any of it (codex #584 R3).  The answering model
        therefore never sees it, it takes no ``[k]`` key, it enters neither
        ``anchors`` nor ``citations``, and it cannot have moved a word of the
        answer.

        Every failure is fail-open: no host, a dormant point, a malformed
        trace, a raising plugin, an exhausted budget, a host answering a
        shape the port never promised — all of them return ``()`` and leave
        the answer exactly as it would otherwise have been.  Cancellation is
        the sole exception: it is the caller's own signal and must keep
        propagating.

        "Fail-open" here means the *answer* is untouched and no error state
        is surfaced — it does NOT mean the disclosure step below disappears.
        Two genuinely different things happen on the two sides of the
        ``try``/``except`` below: before it, a ``return ()`` (no host, a
        dormant point, neither trigger condition, an empty question) really
        is zero attempt and correctly produces no step at all — nothing was
        asked, so there is nothing to disclose.  Inside the ``try``, once
        ``host.consult()`` has actually been called, a raise/timeout/malformed
        result still falls through to the unconditional ``TraceStep`` build
        below with ``suggestions = ()`` — the reader is entitled to know a
        bounded egress attempt happened even when it came back with nothing,
        exactly as much as when it came back with something.  A codex #584
        R4 review comment proposed suppressing the step specifically on the
        failure path; that was reviewed and deliberately rejected for this
        reason (see the reverse-guard test
        ``test_gap_consult_ask_wiring.py::
        test_plugin_raises_leaves_the_answer_verbatim``, which pins the step
        staying present with a zero count).
        """
        host = getattr(self, "gap_consult_host", None)
        if host is None or gap_consult_host_is_dormant(host):
            return ()
        gaps = _uncovered_directions_from_trace(trace)
        thin = limits is not None and (
            len(top_hits) + len(chunks) + len(elements)
            < limits.ranked_final_floor
        )
        if not gaps and not thin:
            return ()
        question = _egress_question(prepared)
        if not question:
            return ()
        started = time.monotonic()
        try:
            # The guard spans the CONVERSION too, not just the call.  Building
            # an ``AskGapSuggestion`` runs pydantic validation against the wire
            # rails, so a host answering an over-long title raises here rather
            # than inside ``consult`` — leaving that outside the guard would
            # let a misbehaving host take down the whole Ask on the last step
            # before the answer is drafted.
            suggestions = _admitted_gap_suggestions(
                host.consult(
                    GapConsultCallContext(
                        GapConsultQuery(
                            question, gaps, GAP_CONSULT_MAX_SUGGESTIONS
                        ),
                        cancellation,
                        self.retrieval_connection_probe,
                        started + self.settings.ask_gap_consult_timeout_seconds,
                    ),
                    event_sink=self.event_log.emit,
                )
            )
        except AskCancelled:
            raise
        except Exception:
            # Defence in depth: the host already fails open per contributor,
            # so reaching here means the host itself misbehaved.  A gap
            # suggestion is worth strictly less than the answer it accompanies.
            suggestions = ()
        # Deliberately unconditional: the step below is built and appended
        # whether ``suggestions`` came from a clean call or from the
        # ``except`` branch above.  This is egress transparency, not failure
        # noise — the step's job is disclosing that a bounded outside-the-
        # notebook query happened at all, and a reader is owed that even
        # when it produced zero suggestions.  A codex #584 R4 review comment
        # suggested suppressing this step on the failure path; reviewed and
        # rejected (see the docstring above and
        # ``test_gap_consult_ask_wiring.py::
        # test_plugin_raises_leaves_the_answer_verbatim``).
        step = TraceStep(
            step_type="gap_consult",
            summary=(
                # Neither wording attributes a CAUSE.  The thin-evidence
                # branch fires just as well when retrieval itself degraded
                # fail-open, and telling the reader their notebook came up
                # short would be this step inventing a diagnosis it never made.
                f"已向站外来源询问 {len(gaps)} 个缺口，"
                f"得到 {len(suggestions)} 条建议（不参与本次回答）"
                if gaps
                else f"已向站外来源询问相关资料，"
                     f"得到 {len(suggestions)} 条建议（不参与本次回答）"
            ),
            detail={
                "reason": "uncovered_directions" if gaps else "thin_evidence",
                "count": len(suggestions),
                "gaps": len(gaps),
            },
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        sink.append(step)
        if on_step:
            on_step(step)
        return suggestions

    def _run_reasoning_stage(self, prepared, runtime):
        """Orchestrate one prepared reasoning Ask across the typed stages.

        Only orchestration lives here: the pre-retrieval short circuits, the
        typed retrieval seam, and then the injectable ``ResponseDraftStage``
        that turns retrieval evidence into the response draft.  The commit
        boundary stays core-owned below both seams -- no stage implementation
        reaches the atomic save.
        """
        from app.application.ask_reasoning import (
            CommittedReasoningAnswer,
            PreparedReasoningAsk,
            ReasoningResponseDraft,
            ReasoningRetrievalRuntime,
            ResponseDraftInput,
            StageBoundaryError,
            execute_response_draft_stage,
        )
        from app.services.reasoning_retrieval import (
            ReasoningResult,
            ReasoningRetriever,
        )

        if type(prepared) is not PreparedReasoningAsk:
            raise StageBoundaryError("invalid prepared Ask reasoning input")
        if type(runtime) is not ReasoningRetrievalRuntime:
            raise StageBoundaryError("invalid Ask reasoning runtime")
        self._assert_reasoning_runtime(
            runtime,
            "reasoning-stage-entry",
            notebook_id=prepared.notebook_id,
            user_id=prepared.user_id,
        )
        notebook_id = prepared.notebook_id
        question = prepared.question
        conversation_id = prepared.conversation_id
        history = prepared.history
        reasoning_history = prepared.history
        style_block = prepared.style_block
        from app.models.ask import QueryIntentContract

        intent_contract = QueryIntentContract.model_validate_json(
            prepared.intent_json
        )
        research_question = prepared.research_question
        intent_queries = prepared.intent_queries
        limits = prepared.limits
        intent_step = TraceStep(
            step_type="intent",
            summary="已按确认后的问题理解开始检索",
            detail=prepared.intent_projection.as_json_mapping(),
            duration_ms=prepared.intent_trace_duration_ms,
        )
        user_id = prepared.user_id
        job_id = prepared.job_id
        asked_at = prepared.asked_at
        retrieval_effort = prepared.retrieval_effort
        on_trace = runtime.trace_sink
        cancel_event = runtime.cancellation
        pre_trace: list[TraceStep] = [intent_step]
        intent_streamed = False

        def commit_response(response, baseline_manifest=None):
            return self._commit_reasoning_draft(
                ReasoningResponseDraft(
                    notebook_id=notebook_id,
                    question=question,
                    response=response,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    job_id=job_id,
                    asked_at=asked_at,
                    baseline_manifest=baseline_manifest,
                ),
                runtime,
                prepared,
            )

        def stream_intent() -> None:
            nonlocal intent_streamed
            if not intent_streamed and on_trace:
                for step in pre_trace:
                    on_trace(step)
            intent_streamed = True

        def checked_pre_trace(step: TraceStep) -> None:
            raise_if_cancelled(cancel_event)
            pre_trace.append(step)
            if on_trace:
                on_trace(step)

        def streamed_pre_trace() -> "list[TraceStep] | None":
            """短路返回要带上的轨迹:客户端已经看到的那几步。

            有 `on_trace` 就说明 intent(以及命中时的 memory)已经推送出去了 ——
            final 事件不带它们,就是在替换在途 turn 的同一刻把用户刚看着走过的
            轨迹抹掉,历史里也留不下。没有流消费者的直调则保持原状:空轨迹仍然
            表示「agentic loop 没跑」。"""
            return pre_trace if on_trace is not None else None

        structured_batch = None
        completeness_unavailable = False
        if (
            intent_contract.completeness_required
            and self.knowhow_store is not None
            and not source_scope_restricted()
        ):
            from app.services.structured_retrieval import (
                enumerate_knowhow,
                is_knowhow_enumeration_query,
                render_structured_answer,
                supports_structured_knowhow_query,
            )
            scope_question = intent_contract.resolved_question
            catalog_loader = getattr(
                self.knowhow_store, "knowhow_enumeration_catalog", None
            )
            if callable(catalog_loader):
                knowhow_catalog = catalog_loader(
                    notebook_id,
                    limit=limits.structured_max_tables,
                    query=scope_question,
                )
            else:  # narrow compatibility doubles; production ports implement it
                legacy_tables = list(
                    self.knowhow_store.list_knowhow_tables(notebook_id) or []
                )[:limits.structured_max_tables]
                knowhow_catalog = {
                    "tables": legacy_tables,
                    "known_tables": len(legacy_tables),
                    "known_total_rows": sum(
                        int(row.get("row_count") or 0) for row in legacy_tables
                    ),
                    "fingerprint": [],
                }
            knowhow_tables = list(knowhow_catalog.get("tables") or [])
            if (
                is_knowhow_enumeration_query(knowhow_tables, scope_question)
                and supports_structured_knowhow_query(
                    scope_question, intent_contract.result_scope, knowhow_tables
                )
            ):
                stream_intent()
                structured_batch = enumerate_knowhow(
                    self.knowhow_store,
                    notebook_id,
                    scope_question,
                    limits,
                    catalog_snapshot=knowhow_catalog,
                    cancel_event=cancel_event,
                    on_step=checked_pre_trace,
                )
            else:
                completeness_unavailable = True
        elif intent_contract.completeness_required:
            completeness_unavailable = True

        if structured_batch is not None and intent_contract.result_scope in {
            "complete", "aggregate"
        }:
            conclusion, answer = render_structured_answer(
                structured_batch,
                aggregate=intent_contract.result_scope == "aggregate",
                inline_rows=limits.inline_answer_rows,
                cell_excerpt_chars=limits.cell_excerpt_chars,
            )
            answer_step = TraceStep(
                step_type="answer",
                summary=(
                    f"完整枚举 {structured_batch.returned_rows} 行"
                    if structured_batch.complete else
                    f"部分枚举 {structured_batch.returned_rows}/"
                    f"{structured_batch.known_total_rows} 行"
                ),
                detail={
                    "complete": structured_batch.complete,
                    "scanned_rows": structured_batch.scanned_rows,
                    "returned_rows": structured_batch.returned_rows,
                    "known_total_rows": structured_batch.known_total_rows,
                    "truncated_reason": structured_batch.truncated_reason,
                },
            )
            checked_pre_trace(answer_step)
            response = AskResponse(
                answer_id="",
                conclusion=conclusion,
                answer=answer,
                grounded=structured_batch.complete,
                evidence_level="grounded" if structured_batch.complete else "overview",
                llm_mode="structured",
                conversation_id=conversation_id,
                retrieval_query=research_question,
                reasoning_trace=pre_trace,
                intent=intent_contract,
                retrieval_effort=retrieval_effort,
                result_sets=structured_batch.result_sets,
                result_coverage=structured_batch.coverage(),
            )
            response.mode = "reasoning"
            return commit_response(response)

        # Stream the intent step BEFORE memory retrieval.  Memory hits cost an
        # embedding round trip plus a vector scan, and until this step lands the
        # UI has nothing after the synthetic "start" — the reader is left
        # watching an empty trace through work that is already under way.
        stream_intent()
        memory_started = time.perf_counter()
        memory_hits = self._memory_hits(user_id, notebook_id, research_question)
        # The duration covers the embedding round trip plus the vector scan
        # above: this step is the trace's only account of that work, so leaving
        # it untimed would drop it from a total advertised as covering the run.
        memory_ms = round((time.perf_counter() - memory_started) * 1000)

        def record_memory_step() -> None:
            """记录本轮召回到的私有记忆(0 命中不记 —— 那在没有记忆的笔记本里全是噪声)。

            措辞只声称**召回**,不声称被答案采纳:合成未必会发生(模型未配置、
            或注册表为空的离线确定性模式),那时说「参考了 N 条」就是假账。调用点
            也刻意排在短路返回之后,让根本没产生答案的那几轮干脆不提记忆。

            刻意**不**改成「只在记忆真被模型绑成锚点时才记」(codex 第 7 轮 P2 的
            建议),两个理由:
            1. 那会把这一步推到答案合成之后才发出,实时轨迹里它就不再出现在事情
               真正发生的位置 —— 而这一步携带的正是召回本身的耗时。
            2. 记忆进了 prompt 却没被引用是常态,按锚点过滤会变成**漏报**:
               轨迹会说没查过记忆,而实际上查了、也喂给了模型。
            轨迹记录的是引擎做过什么,不是答案归因了什么;归因由答案里的 [k] 引用
            承担。反向护栏见 test_reasoning_stream.py 的
            test_memory_step_reports_recall_not_attribution。"""
            if memory_hits:
                checked_pre_trace(TraceStep(
                    step_type="memory",
                    summary=f"找到 {len(memory_hits)} 条相关记忆",
                    detail={"count": len(memory_hits)},
                    duration_ms=memory_ms,
                ))
            else:
                # 零命中也要记一步:候选查询与 embedding 调用照样发生了,把
                # memory_ms 丢掉,「轨迹覆盖整轮」这句就不成立(codex 第 10 轮 P2)。
                # 用 skip 而不是 memory —— 「记忆」这个标签只在真的找到东西时出现,
                # 与 test_memory_step_reports_recall_not_attribution 的口径一致;
                # 检索器本来就用 skip 记录跳过的工作,这里沿用同一套词汇。
                checked_pre_trace(TraceStep(
                    step_type="skip",
                    summary="未找到相关记忆",
                    duration_ms=memory_ms,
                ))

        if self._primary_llm_unconfigured():
            if structured_batch is not None:
                from app.services.structured_retrieval import render_structured_answer
                coverage_conclusion, coverage_answer = render_structured_answer(
                    structured_batch,
                    aggregate=False,
                    inline_rows=limits.inline_answer_rows,
                    cell_excerpt_chars=limits.cell_excerpt_chars,
                )
                response = AskResponse(
                    conclusion=(
                        f"{coverage_conclusion}\n\n分析阶段未运行：系统未配置当前"
                        "问答所需的模型服务。"
                    ),
                    answer=coverage_answer,
                    grounded=structured_batch.complete,
                    evidence_level=(
                        "grounded" if structured_batch.complete else "overview"
                    ),
                    llm_mode="structured",
                    conversation_id=conversation_id,
                    retrieval_query=research_question,
                    reasoning_trace=pre_trace,
                    intent=intent_contract,
                    retrieval_effort=retrieval_effort,
                    result_sets=structured_batch.result_sets,
                    result_coverage=structured_batch.coverage(),
                    model_errors=[ModelError(
                        stage="answer", model="", message="missing_config"
                    )],
                )
                response.mode = "reasoning"
                return commit_response(response)
            response = self._unconfigured_model_response(
                notebook_id, question, conversation_id, "reasoning",
                user_id=user_id, job_id=job_id, asked_at=asked_at,
                intent=intent_contract,
                retrieval_query=research_question,
                retrieval_effort=retrieval_effort,
                completeness_unavailable=completeness_unavailable,
                reasoning_trace=streamed_pre_trace(),
                persist=False,
            )
            return commit_response(response)

        # 无图 ≠ 无法作答。元素/知识对象清单工具在「解析了来源但没建图」的库里
        # 照样能给出精确清单,而自动 KG 抽取默认是关的——那正是常态。所以早退的
        # 条件收窄成「无图 **且** 枚举工具在这个作用域里也拿不出任何东西」;
        # 放行后进入正常 reasoning 循环:图是空的,初检索/expand 自然返回空,
        # 地图、enumerate 与 search_elements 照常工作。
        # kg_required 的语义原样保留(无图且无可用参考库 = True),它只是不再顺带
        # 阻断执行——旗标是响应契约的一部分,不能因为这一轮跑通了就变成假的。
        no_usable_kg = not memory_hits and not (
            self.candidates.has_kg(notebook_id)
            or self.candidates.any_base_has_kg(notebook_id))
        if no_usable_kg and not self._collections_reachable(notebook_id):
            coverage_prefix = ""
            coverage_answer = ""
            if structured_batch is not None:
                from app.services.structured_retrieval import render_structured_answer
                coverage_prefix, coverage_answer = render_structured_answer(
                    structured_batch,
                    aggregate=False,
                    inline_rows=limits.inline_answer_rows,
                    cell_excerpt_chars=limits.cell_excerpt_chars,
                )
            response = AskResponse(
                answer_id="",
                conclusion=(
                    (f"{coverage_prefix}\n\n" if coverage_prefix else "")
                    + (
                        "当前精确完整枚举支持 Knowhow 整表物理行清单与直接行计数，"
                        "以及元素清单（公式/表格/图片/代码块）、知识对象清单与来源清单；"
                        "本次请求不能视为全部结果。\n\n"
                        if completeness_unavailable else ""
                    )
                    + "本笔记本尚未构建知识图谱,也没有已建图的参考库;"
                    "请先点『构建知识图谱』,或为本笔记本挂载一个已建图的"
                    "公共知识库。"
                ),
                answer=coverage_answer,
                grounded=bool(structured_batch and structured_batch.complete),
                evidence_level=(
                    "grounded"
                    if structured_batch and structured_batch.complete else "inferred"
                ),
                conversation_id=conversation_id, retrieval_query=research_question,
                llm_mode="deterministic", kg_required=True,
                intent=intent_contract,
                retrieval_effort=retrieval_effort,
                result_sets=(structured_batch.result_sets if structured_batch else []),
                result_coverage=(
                    structured_batch.coverage() if structured_batch else None
                ),
                reasoning_trace=(
                    pre_trace
                    if structured_batch is not None else streamed_pre_trace()
                ))
            response.mode = "reasoning"
            return commit_response(response)

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        # P1-A(本轮 scope):只挂 reasoning 模式。ask_chunk 同样受益(已退役的
        # graph 引擎曾也受益),但等价性回放验证只覆盖了 reasoning——留作 fast-follow。
        _emb_token = _ASK_EMBED_CACHE.set({})
        source_graph_status = None
        historical_reasoning_chunks = []
        try:
            # intent already streamed above (before memory retrieval); stream_intent
            # stays idempotent because the structured branch may emit it earlier.
            # 记忆那一步排在这里:上面每一条短路返回都不会产出答案,在它们之前记
            # 就等于给一次没发生的作答留下「用过你的记忆」的痕迹。
            record_memory_step()

            def checked_trace(step):
                raise_if_cancelled(cancel_event)
                if on_trace:
                    on_trace(step)

            result = ReasoningResult()
            try:
                # 端口化构造(与冻结的 from_repository 工厂逐字段同源):检索/模型/
                # 社区端口直通,communities 逐次新建 —— sibling_min_bridge 调用时读。
                from app.application.ask_reasoning import (
                    ReasoningRetrievalRuntime,
                    ReasoningRunInput,
                    RetrievedReasoningAsk,
                    execute_reasoning_retrieval_stage,
                )

                retriever = ReasoningRetriever(
                    retrieval=self.retrieval,
                    model_clients=self.model_clients,
                    communities=self.communities(),
                    settings=self.settings,
                    cancel_event=cancel_event,
                    collection_catalog=self.collection_catalog,
                    collection_enumeration=self.collection_enumeration,
                    agent_profile=self.agent_profile,
                    # 提问者身份**显式**传入(绝不让 retriever 回退 ContextVar,
                    # 那会在 ContextVar 未设时读到 seeded admin 的覆盖层)。这条
                    # 路径上 `user_id` 就是本次提问的持久化归属。
                    profile_owner_id=user_id,
                    # Agentic Memory P2:检索打法库。刻意**没有** owner 参数
                    # ——那张表没有任何租户维度,条目也不属于任何人。
                    retrieval_experiences=self.retrieval_experiences,
                    # Agentic Memory P3(T8):规划侧的风格提示座位。retriever 内部
                    # 用 profile_owner_id(= user_id)自己点读一次,与上面 style_block
                    # (合成侧、供 _answer_reasoning 用)各自独立读取——两个消费点
                    # 各自 fail-open,不共享一次读取(见 _search_profile_style_block
                    # 的模块注释)。
                    identity_store=self.identity_store,
                )
                retrieval_runtime = ReasoningRetrievalRuntime(
                    scope=runtime.scope,
                    retrieval_run=runtime.retrieval_run,
                    cancellation=runtime.cancellation,
                    trace_sink=checked_trace,
                    connection_probe=runtime.connection_probe,
                )
                evidence = execute_reasoning_retrieval_stage(
                    retriever,
                    ReasoningRunInput(
                        notebook_id=notebook_id,
                        question=research_question,
                        history=reasoning_history,
                        top_n=None,
                        max_steps=None,
                        intent_queries=tuple(intent_queries),
                        limits=limits,
                        # 关闭字段投影与 intent trace 同源；检索器只能通过
                        # ``as_mapping`` 进入既有 project_run_step 收窄点。
                        intent=prepared.intent_projection,
                    ),
                    retrieval_runtime,
                )
                retrieved = RetrievedReasoningAsk(
                    prepared=prepared,
                    evidence=evidence,
                )
                result = retrieved.evidence
                top_hits, elements, trace, chunks, chains = (
                    result.top_hits, result.elements, result.trace, result.chunks,
                    result.chains)
                # 类型化集合清单(CollectionEnumerationOutcome)原样带出,本任务
                # 不消费:进证据 prompt 与 AskResponse.result_sets 是 T5 的地盘。
                enumerations = result.enumerations
                # 本 run 的集合地图。run 内已经建过一次(计数走有界缓存),这里
                # 带出来直接进合成上下文,不重建。
                collection_map_text = result.collection_map_text
                # 终态大纲(仅穷尽档非空)+ 被 top_n 截断挤出 top_hits 的绑定对象。
                # 按节合成按每节的绑定证据分片装配上下文,两者合起来才解析得全
                # (见 reasoning_retrieval.outline_truncated_kg_evidence)。
                reasoning_outline = result.outline
                reasoning_outline_evidence = result.outline_evidence
                trace = [*pre_trace, *trace]
                historical_reasoning_chunks = list(chunks)
                chunks, source_graph_status = self._activate_selected_source_graph(
                    notebook_id,
                    historical_reasoning_chunks,
                    top_hits=top_hits,
                    max_results=self.settings.ppr_top_chunks,
                )
            except (AskCancelled, StageBoundaryError):
                raise
            except Exception:
                top_hits, elements, trace, chunks, chains = (
                    [], [], list(pre_trace), [], []
                )
                enumerations = []
                collection_map_text = ""
                reasoning_outline = []
                reasoning_outline_evidence = []

            # 检索之后、合成之前的取消检查。草稿阶段做的第一件事是按笔记本
            # 读 schema 覆盖层(一次真实 I/O);历史检查排在那次读取**之后**,
            # 于是被取消的一轮会先白付一次立刻丢弃的读。提到边界之前结局仍是
            # 同一个 AskCancelled,只是不再付那笔钱(与「拿到槽位后、发起 I/O
            # 前再检查一次」同一条口径)。已取消且那次 schema 读自身也会抛错时,
            # 异常类型从该读的异常变成 AskCancelled(取消优先)。
            raise_if_cancelled(cancel_event)
            # 权威复核同样上提到编排器:曾经是 ``_draft_reasoning_response``
            # (出厂默认实现)自己的 prologue 才做的检查,现在挪到这里、紧挨
            # 进入 seam 之前 —— 这样注入实现也在已验证权威下进入,而不是只有
            # 出厂路径受保护。``_draft_reasoning_response`` 自己保留两条
            # ``type(...)`` 检查作纵深防御,但不再重复这次(权威不会在同一次
            # 调用内两次漂移)的全量校验。
            self._assert_reasoning_runtime(
                runtime,
                "response-draft",
                notebook_id=notebook_id,
                user_id=user_id,
            )
            draft = execute_response_draft_stage(
                self.response_draft_stage,
                ResponseDraftInput(
                    prepared=prepared,
                    intent_contract=intent_contract,
                    top_hits=tuple(top_hits),
                    elements=tuple(elements),
                    trace=tuple(trace),
                    chunks=tuple(chunks),
                    chains=tuple(chains),
                    enumerations=tuple(enumerations),
                    collection_map_text=collection_map_text,
                    outline=tuple(reasoning_outline),
                    outline_evidence=tuple(reasoning_outline_evidence),
                    historical_chunks=tuple(historical_reasoning_chunks),
                    memory_hits=tuple(memory_hits),
                    structured_batch=structured_batch,
                    completeness_unavailable=completeness_unavailable,
                    kg_required=no_usable_kg,
                    # 检索器自己的候选账本(broad-except 降级时 ``result`` 仍是
                    # 进入这一轮时的那个对象,取不到就是 None)——原样保留旧的
                    # getattr 语义,只是把它算在编排侧、不让整个检索结果对象
                    # 跨过边界。
                    candidate_manifest=getattr(result, "baseline_manifest", None),
                ),
                runtime,
            )
            # ``model_errors`` 由 core 统一填充,不再要求(也不再允许假定)stage
            # 自己写了它:检索阶段(memory 召回、``ReasoningRetriever.run()`` 内部
            # 的规划/反思模型调用)记的报警都落在这同一个请求局部 ``_err_sink``
            # 里,一个从不 import 私有 ``_ASK_MODEL_ERRORS`` 的注入 stage 不会
            # 因此丢掉它们(codex #571 R2 P2)。放在 stage 返回之后、``finally``
            # 的 reset 之前——``_err_sink`` 是一个局部变量,值不因 ContextVar
            # reset 而改变,但填充动作留在权威读取仍然有效的这一刻,不必去
            # reset 之后再论证为什么局部变量还能用。
            # 类型防御同 ``_commit_reasoning_draft``:``draft.response`` 若不是
            # ``AskResponse``,让那个边界自己的类型检查去报
            # "invalid reasoning response graph",这里不重复、也不因此崩溃。
            if type(draft.response) is AskResponse:
                draft.response.model_errors = [
                    ModelError(**e) for e in _err_sink
                ]
                # 缺口外扩:draft stage 返回**之后**才发起(codex #584 R3)——
                # 注入的 stage 从 ``ResponseDraftInput``(含 trace 元组)里
                # 结构上看不到任何 gap 痕迹,连「发生过一次外扩」这个事实都
                # 看不到,散文因此不可能因它而变。触发判据读的仍是 stage 之前
                # 就冻结的本地 ``trace`` 与证据计数;披露步经 ``gap_steps``
                # 由 core 直接落到将被持久化的响应上,与 ``model_errors`` 的
                # 回填同款。零外扩时不物化 ``reasoning_trace``(None 与 []
                # 在持久化 payload 上不是一回事,别替无关请求改 wire 形状)。
                gap_steps: list = []
                gap_suggestions = self._consult_gap_sources(
                    prepared, limits, trace,
                    top_hits=top_hits, chunks=chunks, elements=elements,
                    cancellation=cancel_event, sink=gap_steps,
                    on_step=on_trace,
                )
                if gap_steps:
                    if draft.response.reasoning_trace is None:
                        draft.response.reasoning_trace = []
                    draft.response.reasoning_trace.extend(gap_steps)
                # 同一条先例:非证据字段由 core 在 stage 之后填,注入实现够不着。
                draft.response.gap_suggestions = list(gap_suggestions)
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
            _ASK_EMBED_CACHE.reset(_emb_token)
        # 持久化边界仍由 core 独占:最后一次取消检查与原子 save 都在
        # ``_commit_reasoning_draft`` 里,任何 stage 实现都够不到它们。
        return self._commit_reasoning_draft(draft, runtime, prepared)

    def _draft_reasoning_response(self, stage, runtime):
        """The shipped ``ResponseDraftStage`` body: evidence -> response draft.

        Everything below the rebinding prologue is the historical inline
        segment of ``_run_reasoning_stage``, moved verbatim: answer synthesis,
        sectioned synthesis, ``classify_evidence``, ``[k]`` anchor binding,
        the synthesis trace step and ``AskResponse`` assembly keep their exact
        order and their exact object graph.  ``self`` is deliberately not
        renamed -- the seam is the injected ``DefaultResponseDraftStage``
        object, not a rename across 650 lines of body.

        The response graph is *finished* here, including ``mode`` -- so the
        only mutations left after the frozen envelope are ``model_errors``
        (filled by the orchestrator right after this stage returns, from the
        same request-local sink, so every stage gets the same treatment
        without needing to import it -- see ``_run_reasoning_stage``) and
        ``answer_id`` (which the commit boundary owns).
        """
        from app.application.ask_reasoning import (
            ReasoningResponseDraft,
            ReasoningRetrievalRuntime,
            ResponseDraftInput,
            StageBoundaryError,
        )

        # Defense-in-depth only: the full authority sweep for this point now
        # runs once in the orchestrator (``_run_reasoning_stage``), right
        # before it enters this seam, so every stage implementation -- shipped
        # or injected -- is admitted under the same already-verified runtime.
        # These two ``type(...)`` checks stay here because this method is
        # itself directly callable (it is the shipped stage's own body), and
        # a malformed envelope should still fail loudly even when invoked
        # outside the orchestrator's protection.
        if type(stage) is not ResponseDraftInput:
            raise StageBoundaryError("invalid reasoning response draft input")
        if type(runtime) is not ReasoningRetrievalRuntime:
            raise StageBoundaryError("invalid reasoning response draft runtime")
        prepared = stage.prepared
        notebook_id = prepared.notebook_id
        conversation_id = prepared.conversation_id
        reasoning_history = prepared.history
        style_block = prepared.style_block
        research_question = prepared.research_question
        limits = prepared.limits
        retrieval_effort = prepared.retrieval_effort
        intent_contract = stage.intent_contract
        memory_hits = list(stage.memory_hits)
        structured_batch = stage.structured_batch
        completeness_unavailable = stage.completeness_unavailable
        no_usable_kg = stage.kg_required
        top_hits = list(stage.top_hits)
        elements = list(stage.elements)
        trace = list(stage.trace)
        chunks = list(stage.chunks)
        chains = list(stage.chains)
        enumerations = list(stage.enumerations)
        collection_map_text = stage.collection_map_text
        reasoning_outline = list(stage.outline)
        reasoning_outline_evidence = list(stage.outline_evidence)
        historical_reasoning_chunks = list(stage.historical_chunks)
        on_trace = runtime.trace_sink
        cancel_event = runtime.cancellation
        # 与 ``_answer_with_retry`` 同一条惯例:响应内报警表是请求局部
        # ContextVar,由 ``_run_reasoning_stage`` 设置、在它的 ``finally`` 里
        # 复位。直调/离线路径下它是 None —— 那时 ``note_model_error`` 只写事件
        # 日志、不写响应,没有可摘也没有可报的东西。
        _err_sink = _ASK_MODEL_ERRORS.get()
        if _err_sink is None:
            _err_sink = []

        # Federated hits carry their owning notebook. Render each payload
        # against THAT notebook's schema overlay: the active notebook may
        # customize the same object_type, but its field order/primary label
        # must never be projected onto an object from a mounted base.
        schema_registries = {
            owner_id: self.schemas.effective_schemas(owner_id)
            for owner_id in {
                item.notebook_id or notebook_id for item in top_hits
            }
        }
        seen_ids: set = set()
        related_knowledge: List[KnowledgeRecord] = []
        raise_if_cancelled(cancel_event)
        for item in top_hits:
            if item.object_id in seen_ids:
                continue
            seen_ids.add(item.object_id)
            related_knowledge.append(knowledge_record(
                item.object_type,
                {"id": item.object_id, "payload": item.payload, "status": item.status,
                 "owner": getattr(item, "owner", ""),
                 "last_reviewed": getattr(item, "last_reviewed", ""),
                 "evidence": item.evidence},
                schema_registries[item.notebook_id or notebook_id].get(
                    item.object_type
                )))
        related_knowledge = related_knowledge[
            : self.settings.ask_related_knowledge_limit
        ]

        cited_element_ids = {ev.element_id for item in top_hits
                             for ev in item.evidence if ev.element_id}
        citations = self.evidence_context.citations_from(
            top_hits, cited_element_ids, "KG evidence", notebook_id=notebook_id)

        answer, llm_grounded, anchors = "", False, []
        synth_failed = False
        synthesis_ran = False
        synthesis_started = time.perf_counter()
        # _answer_reasoning returns a 4th `counts` element (included_kg/
        # chunks/elements); _answer_with_retry's synth() contract is shared
        # with other answer paths and stays a 3-tuple, so this closure
        # captures counts as a side effect instead of widening that contract.
        reasoning_counts: dict = {}
        reasoning_baseline: dict = {}
        raise_if_cancelled(cancel_event)
        answer_client = self.model_clients.chat("ask_answer")
        # 地图块:确定性服务端小块,不依赖 enumerations 是否为空——「集合太大
        # 就别枚举、直接报数」这条路径下恰恰一条清单都没有,而那个数正是答案
        # 本身。渲染纯字符串拼接,不可能抛,故不需要 try 包(与下方清单块不同,
        # 那里有映射/预算计算)。
        from app.services.collection_enumeration_answer import (
            collection_map_block as _collection_map_block,
        )
        collection_map_prompt_block = (
            _collection_map_block(collection_map_text)
            if answer_client.configured else ""
        )
        structured_block = ""
        if structured_batch is not None and answer_client.configured:
            from app.services.structured_retrieval import structured_prompt_block
            structured_block = structured_prompt_block(
                structured_batch,
                inline_rows=limits.inline_answer_rows,
                cell_excerpt_chars=limits.cell_excerpt_chars,
                budget_chars=limits.chunk_context_chars,
            )
        # 类型化集合清单(T4 产出的 enumerations)映射成 AskResponse.result_sets
        # 行 + 合成证据块(T5)。映射与 prompt 块拼接放在**同一个** try 里:两者
        # 失败都只应让清单这一整份"锦上添花"的产出消失,不该出现"卡片有了
        # 但块裸抛穿整轮 Ask"或"块建起来了但卡片已经在别的异常里被清空"的
        # 一半状态(codex 评审实测复现过后一种)。`enumerations` 在上面的 broad
        # except 分支里可能已经被清空过一次,这里的 try 只防映射/渲染本身再
        # 出岔子。
        typed_collection_result_sets: list = []
        enumeration_block_dropped = False
        collection_item_citations: dict = {}
        structured_map: dict = {}
        if enumerations:
            try:
                from app.services.collection_enumeration_answer import (
                    apply_synthesis_preview_counts,
                    delivered_outcomes,
                    enumeration_prompt_block,
                    typed_collection_results,
                )
                # 载荷闸在这里按**真实 wire 形状**收口:执行器的池量的是
                # 紧凑 dataclass,而联合体两臂的默认字段 + 结果元数据会让
                # 下发/持久化的 JSON 明显更宽(见该函数 docstring)。
                collection_items = [
                    item for outcome in enumerations for item in outcome.items
                ]
                collection_item_citations = (
                    self.evidence_context.collection_item_citations(
                        collection_items,
                        active_notebook_id=notebook_id,
                    )
                )
                typed_collection_result_sets = typed_collection_results(
                    enumerations,
                    payload_chars=limits.structured_payload_chars,
                    citations_by_item_id=collection_item_citations,
                )
                if answer_client.configured:
                    from app.services.collection_enumeration_answer import (
                        enumeration_sub_budget,
                    )
                    # 枚举块在后:与既有 knowhow structured_block 拼接,整体
                    # 仍经 structured_block 这一个参数进 _answer_reasoning,
                    # 装配位保持在 source 分区最前(语义不变,见该函数 843
                    # 行起)。子预算三层夹(见 enumeration_sub_budget 的
                    # docstring):减去 knowhow 已占字符 + 两者之间 "\n\n"
                    # 拼接符本身的 2 个字符,再夹到 chunk_context_chars 的
                    # 一半,防止模型"顺便列出所有表格"把另一半问题
                    # (chunks/elements)的证据预算整个挤空。
                    enum_budget_chars = enumeration_sub_budget(
                        chunk_context_chars=limits.chunk_context_chars,
                        structured_block_len=len(structured_block),
                    )
                    # 预览渲染的是**结果卡真正拿到的那份**:wire 闸裁过的
                    # 集合若照原 outcome 渲染,prompt 里会出现卡片没有的行,
                    # 头部还写着「complete」——prompt 与卡片对同一份清单说
                    # 两套话,正是 coverage 合同要防的东西。
                    preview = enumeration_prompt_block(
                        delivered_outcomes(
                            enumerations, typed_collection_result_sets
                        ),
                        inline_rows=limits.inline_answer_rows,
                        budget_chars=enum_budget_chars,
                        citations_by_item_id=collection_item_citations,
                    )
                    structured_map = preview.evidence_by_id
                    apply_synthesis_preview_counts(
                        typed_collection_result_sets, preview.shown_rows
                    )
                    if preview.text:
                        structured_block = (
                            f"{structured_block}\n\n{preview.text}"
                            if structured_block else preview.text
                        )
                    else:
                        # 预算把连块头都挤没了(极端场景,如 knowhow 已经吃满
                        # chunk_context_chars 的绝大部分):清单结果卡仍然
                        # 存在(上面 typed_collection_result_sets 不受影响),
                        # 只是这一轮没能把它塞进合成证据——这条轨迹detail是
                        # 唯一挂点(trace 已闭合,不能另起一条独立 trace 步)。
                        enumeration_block_dropped = True
            except Exception:
                typed_collection_result_sets = []
                enumeration_block_dropped = False
        def _synth_reasoning():
            # counts_sink 在模型调用前就被 _answer_reasoning 填充:合成模型
            # 抛错/吐畸形 JSON 时,synthesis 步仍能报出真实装配计数而非全零。
            ans, llm_grounded_, anchors_, _counts = self._answer_reasoning(
                notebook_id, research_question, top_hits, elements,
                reasoning_history,
                cancel_event=cancel_event, chunks=chunks, chains=chains,
                memory_hits=memory_hits, answer_client=answer_client,
                kg_context_chars=limits.kg_context_chars,
                chunk_context_chars=limits.chunk_context_chars,
                element_items=limits.answer_element_items,
                structured_block=structured_block,
                structured_map=structured_map,
                collection_map_block=collection_map_prompt_block,
                counts_sink=reasoning_counts,
                baseline_sink=reasoning_baseline,
                style_block=style_block)
            return ans, llm_grounded_, anchors_

        # ---------------------------------------------- 按节合成(设计文档 §3.1)
        # 终态大纲有 ≥2 个能装配出证据的节时,逐节合成再拼接:每节只看见自己
        # 绑上的那几条证据(DualGraph 的产出侧借鉴,避免 lost-in-the-middle)。
        # 闸与 O1 同一个 —— 只在穷尽档提供,k 次合成调用的成本由用户显式选择
        # 「穷尽」来承担;门关着或大纲够不到两节时,下面那条单次合成路径逐字
        # 节不变。
        outline_slices: list = []
        outline_skipped: list[str] = []
        outline_attempted = False
        outline_planned = False
        sectioned = None
        # 分节阶段可能记下的 model_error 起点。回退**成功**后要把这一段摘掉:
        # 那次故障已经被同一轮的重试路径吸收,用户拿到了完整答案,再挂一条红色
        # 横幅是假报警。事件日志(events.jsonl)不受影响 —— note_model_error 的
        # 两个副作用里,只有响应里的这一份是给用户看的。
        outline_err_mark = len(_err_sink)

        def record_section_step(step: TraceStep) -> None:
            """分节进度步:实时推给客户端,同时留在本轮轨迹里。"""
            raise_if_cancelled(cancel_event)
            trace.append(step)
            if on_trace:
                on_trace(step)

        if answer_client.configured and reasoning_outline:
            from app.services.outline_synthesis import plan_outline_sections
            from app.services.reasoning_retrieval import outline_wiring_active
            if outline_wiring_active(self.settings, limits):
                # top_hits 的重排分数优先:同一个对象两处都有时,以最终选集
                # 那一份为准(补集只补它没有的)。
                outline_kg_by_id = {
                    hit.object_id: hit for hit in reasoning_outline_evidence
                }
                outline_kg_by_id.update({hit.object_id: hit for hit in top_hits})
                outline_slices, outline_skipped = plan_outline_sections(
                    reasoning_outline,
                    kg_by_id=outline_kg_by_id,
                    element_by_id={item.element_id: item for item in elements},
                    chunk_by_id={item.chunk_id: item for item in chunks},
                )
                outline_planned = True
                # 产出过集合清单/结构化整表枚举的 run 保持单次合成(codex r7):
                # 节切片刻意只装该节绑定证据,清单块与结构化行不进切片——
                # 「按来源列出全部公式」这类请求若被节化,合成会拿 ranked 样本
                # 写散文,把手上已有的完整清单丢在回退路径里,甚至自称不完整。
                # 单次合成路径的清单预览/覆盖披露机制是成熟的,清单类问题本来
                # 就该走它;清单进节切片是 v2 的设计题,不在绕过里偷做。
                if (len(outline_slices) >= 2 and not enumerations
                        and structured_batch is None):
                    outline_attempted = True
                    sectioned = self._answer_reasoning_sections(
                        notebook_id, research_question, outline_slices,
                        history=reasoning_history, limits=limits,
                        answer_client=answer_client, cancel_event=cancel_event,
                        on_section=record_section_step,
                        style_block=style_block,
                    )
        # 按节合成失败(某一节两次都吐不出内容)→ 整体回退单次合成,已经产出
        # 的分节文本全部丢弃。多付一次合成调用是 fail-open 的价钱。
        outline_fallback = outline_attempted and sectioned is None
        if sectioned is not None:
            answer = sectioned.answer
            llm_grounded = sectioned.llm_grounded
            anchors = sectioned.anchors
            reasoning_counts.clear()
            reasoning_counts.update(sectioned.counts)
            reasoning_baseline.clear()
            reasoning_baseline.update({
                "context_block": "\n".join(
                    str(row.get("context_block") or "")
                    for row in sectioned.baseline_assemblies
                ),
                "id_map": {
                    key: value
                    for row in sectioned.baseline_assemblies
                    for key, value in dict(row.get("id_map") or {}).items()
                },
                "ordered_handles": tuple(
                    handle
                    for row in sectioned.baseline_assemblies
                    for handle in tuple(row.get("ordered_handles") or ())
                ),
                "budget_chars": sum(
                    int(row.get("budget_chars") or 0)
                    for row in sectioned.baseline_assemblies
                ),
                "capture_error_count": sum(
                    int(row.get("capture_error_count") or 0)
                    for row in sectioned.baseline_assemblies
                ),
            })
            synthesis_ran = True
        # 地图也是合成的触发条件之一:大集合场景里模型 reflect 直接 answer、
        # 一条证据都没检索到,而正确答案就是那个计数——没有这一项,合成压根
        # 不跑,用户拿到的是空答案(codex 第 4 轮 P2)。地图非空意味着枚举
        # 工具接线成功且作用域里有可数的东西(空库在更早的早退里就返回了),
        # 所以这不是给空库额外加一次模型调用。
        elif answer_client.configured and (
                top_hits or elements or chunks or chains or memory_hits
                or structured_batch is not None or enumerations
                or collection_map_prompt_block):
            # 空 content 有界重试 + 诚实降级 + 可观测,统一走 _answer_with_retry(见其 docstring)。
            fallback_err_mark = len(_err_sink)
            answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                _synth_reasoning,
                getattr(answer_client, "model", ""),
                service="ask_answer",
            )
            synth_failed = not _ok
            synthesis_ran = True
            if outline_fallback and _ok:
                # 只摘分节那一段(mark..fallback_mark)。单次合成自己那几条
                # 已经由 `_answer_with_retry` 按同一条规则处理过了(重试成功
                # 自摘、两次都失败全留),所以这里刻意仍用**闭区间上界**
                # `fallback_err_mark` 而不是 `del _err_sink[outline_err_mark:]`:
                # 上界是这两段的边界,写成开区间就等于宣称「回退阶段的报警一律
                # 由这一行负责」,哪天单次合成路径多记一条本该保留的错误就会被
                # 这一行悄悄吞掉。
                del _err_sink[outline_err_mark:fallback_err_mark]

        from app.services.retrieval_baseline import (
            build_retrieval_baseline_manifest,
        )
        candidate_manifest = stage.candidate_manifest
        final_baseline_manifest = build_retrieval_baseline_manifest(
            notebook_id=notebook_id,
            query=research_question,
            mode="reasoning",
            settings=self.settings,
            candidate_knowledge=(
                candidate_manifest.candidate_knowledge
                if candidate_manifest is not None else top_hits
            ),
            candidate_chunks=(
                candidate_manifest.candidate_chunks
                if candidate_manifest is not None else chunks
            ),
            candidate_elements=(
                candidate_manifest.candidate_elements
                if candidate_manifest is not None else elements
            ),
            selected_knowledge=top_hits,
            selected_chunks=historical_reasoning_chunks,
            selected_elements=elements,
            final_context_block=str(reasoning_baseline.get("context_block") or ""),
            final_id_map=dict(reasoning_baseline.get("id_map") or {}),
            final_ordered_handles=tuple(
                reasoning_baseline.get("ordered_handles") or ()
            ),
            final_budget_chars=int(reasoning_baseline.get("budget_chars") or 0),
            prior_manifest=candidate_manifest,
            capture_error_count=int(
                reasoning_baseline.get("capture_error_count") or 0
            ),
            baseline_step_usage=len(trace),
        )

        # chunks 直接进证据池:RetrievedChunk.object_id 属性=chunk_id,与 chunk 锚的
        # object_id 对齐,classify_evidence 即可正确计 anchored_rel(守 tau)。
        # Relation anchors need classifier entries, but chain trust is NOT
        # query relevance.  Each chain carries only the relevance of the
        # candidate that authorized that action; unrelated high-scoring hits
        # elsewhere in the answer cannot elevate its anchors over tau.
        from types import SimpleNamespace
        from app.services.kg.follow_chain import chain_anchor_relevances
        relation_relevances = chain_anchor_relevances(chains)
        chain_evidence = [SimpleNamespace(
            object_id=relation_id, relevance=relevance,
        ) for relation_id, relevance in relation_relevances.items()]
        citations.extend(self._memory_citations(anchors, memory_hits))
        element_by_id = {item.element_id: item for item in elements}
        element_refs = self.evidence_context.knowhow_refs_for(element_by_id)
        element_source_info = self.evidence_context.citation_source_info(
            item.source_id for item in elements
        )
        element_tier = self._tier_map_for({notebook_id}).get(
            notebook_id, "personal"
        )
        for anchor in anchors:
            if anchor.object_type != "element" or anchor.object_id not in element_by_id:
                continue
            item = element_by_id[anchor.object_id]
            source_info = element_source_info.get(item.source_id) or {}
            source_title = source_info.get("title", item.source_title)
            citations.append(Citation(
                label=f"{source_title} · {item.location_label}".strip(" ·"),
                source_id=item.source_id,
                element_id=item.element_id,
                location_label=item.location_label,
                quoted_span=item.text[:200],
                source_file_name=source_info.get("file_name", ""),
                tier=element_tier,
                notebook_id="",
                knowhow=element_refs.get(item.element_id),
            ))
        # Typed collection rows have their own deterministic k5001+
        # bindings.  Mirror the cited rows into the fallback Citation
        # contract as well, while keeping the anchor path authoritative.
        seen_collection_citations: set[tuple[str, str]] = set()
        # 这些 Citation **实例**同时躺在 `typed_collection_result_sets` 的行
        # 里(`typed_collection_results` 把同一个对象挂进 `TypedCollectionItem
        # .citation`),所以下面那次 `attach_citation_images` 必须按身份把它们
        # 排除——理由写在那处。
        collection_citation_ids: set[int] = set()
        for anchor in anchors:
            citation = collection_item_citations.get(anchor.object_id)
            if citation is None:
                continue
            identity = (citation.source_id, citation.element_id)
            if identity in seen_collection_citations:
                continue
            seen_collection_citations.add(identity)
            citations.append(citation)
            collection_citation_ids.add(id(citation))
        element_evidence = [SimpleNamespace(
            object_id=item.element_id, relevance=float(item.score or 0.0),
        ) for item in elements]
        # 按节合成真正写进 prompt 的知识对象里,有一部分被 top_n 截断挤出了
        # top_hits(见 outline_truncated_kg_evidence)。它们进不了这个池子的话,
        # 引用了它们的句子在 classify_evidence 眼里就是"引了个不存在的东西",
        # 整篇答案被降级成 inferred —— 而模型引的恰恰是服务端指名喂给它的证据。
        # 只在按节合成真的产出答案时并入:回退路径与关闭态的池子逐字不变。
        outline_pool_extra: list = []
        if sectioned is not None:
            pooled_ids = {hit.object_id for hit in top_hits}
            for item in outline_slices:
                for hit in item.hits:
                    if hit.object_id in pooled_ids:
                        continue
                    pooled_ids.add(hit.object_id)
                    outline_pool_extra.append(hit)
        evidence_pool = (
            list(top_hits) + outline_pool_extra + list(chunks) + chain_evidence
            + element_evidence + list(memory_hits)
        )
        collection_exact_keys = {
            key
            for key, context in structured_map.items()
            if context.get("source_id") and context.get("element_id")
        }
        evidence_level, top_relevance = classify_evidence(
            evidence_pool, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high,
            exact_evidence_keys=collection_exact_keys,
        )
        if sectioned is not None:
            grounded_sections = sum(
                1 for item in sectioned.section_grounding
                if item["grounded"]
            )
            if grounded_sections < sectioned.sections:
                # 部分或零节通过逐节 grounded 门时,整体最多 overview。这里是
                # 封顶而非强制设置:全局分类若已是 inferred 不会被反向抬高;
                # 零节各自为 overview 时也不会被误写成「未命中笔记本依据」。
                if evidence_level == "grounded":
                    evidence_level = "overview"
        grounded = evidence_level == "grounded"

        # An enumeration answer with no bound [k] marker has no verifiable
        # attribution.  Do not let unrelated ranked-retrieval citations
        # masquerade as sources for a checklist the model may have copied.
        if enumerations and synthesis_ran and not anchors:
            citations = []

        # 本段附图: 统一挂在**锚点最终确定之后**——按节合成(sectioned)会整体
        # 换掉 anchors,在它之前富化等于富化一批被丢弃的对象;citations 也要等
        # 到上面那条枚举清零判完,否则会给一批马上要被扔掉的引用白发一次读取。
        # 这里的引用(KG / element)各自都带 element_id,所以只有 chunk 锚点需要
        # 额外候选。锚点与引用同一次调用,共享每答案预算。
        #
        # **集合枚举行的引用按身份排除**(collection_citation_ids):它们是嵌在
        # `typed_collection_result_sets` 里的**同一个** Citation 实例,就地填
        # `.images` 会绕过 `typed_collection_results` 已经按 wire 形状收完的
        # 载荷计费(每张图约 +77 字节未计费),把「响应不会大于它声明的」这条
        # 保证拆掉;而清单卡本来就自带 `TypedCollectionItem.asset_id`,附图对它
        # 纯属冗余。k5001 锚点那条腿不受影响——锚点是另一批对象。
        self.evidence_context.attach_citation_images(
            anchor_image_targets(
                anchors, {item.chunk_id: item.element_ids for item in chunks}
            ) + [
                (citation, ()) for citation in citations
                if id(citation) not in collection_citation_ids
            ]
        )

        if synthesis_ran:
            # The retriever's own last step reports which evidence it ADOPTED;
            # writing the answer (and assembling its citations) happens out
            # here and used to be invisible.  Without this step the trace
            # stalls on "合成" for the whole generation call and the trace
            # total silently omits it — usually the largest slice of a run.
            synthesis_step = TraceStep(
                step_type="synthesis",
                summary=(
                    # anchors 才是模型真正绑上的 [k];citations 是「每条检索到
                    # 的证据一张卡」,模型一个锚点都没吐出来时它还会被当兜底
                    # 列表展示(见 evidence_context.citations_from 的注释)。
                    # 拿它当引用数,会在零绑定的回答上写出「引用 10 处证据」。
                    f"已生成答案，引用 {len(anchors)} 处证据"
                    if answer else "答案合成未产出内容"
                ),
                detail={
                    "citations": len(citations),
                    "anchors": len(anchors),
                    "evidence_level": evidence_level,
                    # Actual counts that entered the synthesis prompt (post
                    # per-partition budget truncation), distinct from the
                    # earlier "answer" step's pre-truncation candidate pool.
                    "included_kg": reasoning_counts.get("included_kg", 0),
                    "included_chunks": reasoning_counts.get("included_chunks", 0),
                    "included_elements": reasoning_counts.get("included_elements", 0),
                    "included_collections": reasoning_counts.get(
                        "included_collections", 0
                    ),
                    # 本轮产生的类型化集合清单数(诊断字段,不上屏)。清单本身
                    # 进合成 prompt / 结果卡由 T5 接管;在这里露一个数,是为了
                    # 让「工具跑了但答案没体现」这种情况在轨迹里可查。
                    "enumerated_collections": len(enumerations),
                    # 枚举块因预算太紧(连块头都放不下)被整体挤出合成证据;
                    # 结果卡不受影响(typed_collection_result_sets 仍然完整),
                    # 只是模型这一轮没看到清单预览。trace 已闭合,这是唯一
                    # 挂点。
                    "enumeration_block_dropped": enumeration_block_dropped,
                    # Agentic Memory P4 (T1): the answer's actually-bound
                    # [k] anchors, by object_id — the raw material for
                    # step→anchor attribution (see TRACE_ANCHOR_EVIDENCE_
                    # IDS_MAX's docstring in app.models.ask for the
                    # disclosure argument and the cap's derivation).
                    # Written unconditionally, including the empty-list
                    # zero-anchor case — a synthesis step with no anchors
                    # is itself a real signal, not an absent one.
                    "anchor_evidence_ids": [
                        anchor.object_id for anchor in anchors
                    ][:TRACE_ANCHOR_EVIDENCE_IDS_MAX],
                },
                duration_ms=round((time.perf_counter() - synthesis_started) * 1000),
            )
            if len(anchors) > TRACE_ANCHOR_EVIDENCE_IDS_MAX:
                # Sparse marker (mirrors the "neighbor_truncated" pattern
                # in reasoning_retrieval.py's expand step): the cap is a
                # protocol ceiling that should never actually bind in
                # practice under the existing per-tier retrieval budgets,
                # so this key only appears on the (unexpected) day it does.
                synthesis_step.detail["anchor_evidence_ids_truncated"] = True
            if outline_planned:
                # 这组键在大纲**规划跑过**时就出现,而不只在按节合成真的被尝试
                # 过时(codex r6):大纲只装配出 1 个有证据节时按节合成被绕过,
                # 但另一节「问到了没找到」的披露不能跟着消失——否则单节答案看
                # 起来是完整的。没有大纲、低档位与关闭态下规划不会跑,synthesis
                # 步的 detail 仍逐键不变(冻结基线口径)。
                synthesis_step.detail.update({
                    # 实际合成的节数;回退时为 0(分节产物已全部丢弃)。
                    "outline_sections": (
                        sectioned.sections if sectioned is not None else 0
                    ),
                    "outline_fallback": outline_fallback,
                    # 被跳过的空节标题:它们是「问到了但没找到」的诚实记录,
                    # 只在轨迹里露面,答案里不留空壳标题。
                    "outline_skipped": outline_skipped,
                    # 只落 trace detail,不扩 AskResponse。每节结果已经过该节
                    # 自己的 classify_evidence,不是模型裸自报。
                    "section_grounded": (
                        sectioned.section_grounding
                        if sectioned is not None else []
                    ),
                    "ungrounded_sections": (
                        [
                            item["title"]
                            for item in sectioned.section_grounding
                            if not item["grounded"]
                        ]
                        if sectioned is not None else []
                    ),
                })
            trace.append(synthesis_step)
            if on_trace:
                on_trace(synthesis_step)

        if answer:
            conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        elif synth_failed:
            # 诚实降级:检索成功但答案合成未产出内容 —— 绝不冒充成 "Found N objects"
            # (那读起来像"成功但偷懒")。如实说明并保留下方证据(related_knowledge/citations)。
            llm_mode = "synthesis_failed"
            conclusion = (
                f"已检索到 {len(top_hits)} 条相关证据,但本次答案合成未产出内容"
                "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。"
                if top_hits else
                "本次答案合成未产出内容,请重试该问题。")
        else:
            llm_mode = "deterministic"
            conclusion = _NO_RETRIEVAL_EVIDENCE_MESSAGE

        # 抑制免责声明须是确定性规则,不是"有任何一张卡就消音"。四个条件
        # 全部成立才抑制,任一不成立就保留警告——方向是**宁可多警告**:
        # 多一句免责最多显得啰嗦,少一句就是把「相关性抽样」说成了「全部」。
        #
        # ①result_scope=="aggregate"(如"库里有多少种公式"这类去重/种类计数)
        # 从不抑制——枚举工具只会精确统计"表里的物理条目数",证明不了模型
        # 自己在归并去重后的种类数,红线要求这类问题必须回退到相关性检索并
        # 保留警告,哪怕模型顺手枚举出了一张卡。
        # ②意图合同里带**谓词**(约束/排除项/前提)时同样不抑制。清单卡的
        # coverage 只证明「某个物理集合被完整走了一遍」,证明不了那个集合就是
        # 用户要的那个子集——模型完全可能枚举了无关的 kind、或者把带条件的
        # 请求做成了不过滤的全集(codex 第 1 轮 P1-2)。这里刻意**不做**语义
        # 匹配(「这张卡是否覆盖了这条约束」没有确定性判据,做出来的只会是
        # 又一个说不清对错的启发式),而是按合同字段是否为空一刀切。
        #   assumptions(前提)也算在内:一条「只统计 2023 年之后的」与一条
        #   「假定用户指当前笔记本」在字段层面长得一模一样,区分它们需要的
        #   正是上面刚否掉的语义匹配。宁可在有前提时多留一句免责。
        # ③卡有但 returned_total==0(例如枚举了一个空集合)不算「已产出清单
        # 结果」——0 条清单不能替这道题的「全部结果」背书。
        # ④而且必须至少有一张 complete=True 的卡:部分清单自己的 partial
        # 徽章只说明「这张卡没列完」,承担不了「你要的那种请求本产品还不
        # 支持完整枚举」这句披露。
        #
        # 四条都过时,每张卡自己的 coverage 徽章已经承担「完整/部分」的披露,
        # 再前置一句免责反而会让「明明列出了公式清单」的回答开头像在道歉。
        has_complete_collection_result = any(
            row.coverage.complete and row.coverage.returned_total > 0
            for row in typed_collection_result_sets
        )
        has_scoping_predicate = bool(
            intent_contract.constraints
            or intent_contract.excluded_topics
            or intent_contract.assumptions
        )
        suppress_completeness_warning = (
            intent_contract.result_scope != "aggregate"
            and not has_scoping_predicate
            and has_complete_collection_result
        )
        if completeness_unavailable and not suppress_completeness_warning:
            warning = (
                "当前精确完整枚举支持 Knowhow 整表物理行清单与直接行计数，"
                "以及元素清单（公式/表格/图片/代码块）、知识对象清单与来源清单；"
                "条件筛选、去重、分组或其他集合请求本次仍来自相关性检索，"
                "不能视为全部结果。"
            )
            conclusion = f"{warning}\n\n{conclusion}"
            answer = f"> {warning}\n\n{answer}" if answer else warning
        elif structured_batch is not None:
            enumeration_line = (
                f"Knowhow 枚举：{structured_batch.returned_rows}/"
                f"{structured_batch.known_total_rows} 行，"
                f"{'完整' if structured_batch.complete else '部分'}。"
            )
            analysis_line = (
                "分析未运行。"
                if structured_batch.synthesis_complete is None else
                f"分析覆盖：{structured_batch.synthesis_rows}/"
                f"{structured_batch.known_total_rows} 行，"
                f"{'完整' if structured_batch.synthesis_complete else '部分'}。"
            )
            coverage_line = f"{enumeration_line} {analysis_line}"
            conclusion = f"{coverage_line}\n\n{conclusion}"
            answer = f"> {coverage_line}\n\n{answer}" if answer else coverage_line

        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors,
            related_knowledge=related_knowledge, citations=citations,
            llm_mode=llm_mode, conversation_id=conversation_id,
            retrieval_query=research_question, top_relevance=top_relevance,
            reasoning_trace=trace or None,
            intent=intent_contract,
            retrieval_effort=retrieval_effort,
            # Knowhow 的整表批(kind="knowhow")在前,本轮类型化集合清单
            # (kind="collection")在后——顺序与 result_sets 判别 union 的
            # 追加顺序一致(见 app.models.ask.AskResponse.result_sets),T6
            # 按 kind 分派渲染,不依赖顺序本身携带语义。
            result_sets=(
                (structured_batch.result_sets if structured_batch else [])
                + typed_collection_result_sets
            ),
            result_coverage=(
                structured_batch.coverage() if structured_batch else None
            ),
            # 走到这里说明这一轮真的跑了检索与作答,但「本笔记本还没有知识
            # 图谱」这件事没有因此变成假的:旗标继续如实上报,前端的建图提示
            # 与答案并存。它只是不再是一道闸。
            kg_required=no_usable_kg,
        )

        response.mode = "reasoning"
        # ``model_errors`` is deliberately *not* set here any more: the
        # orchestrator (``_run_reasoning_stage``) fills
        # ``draft.response.model_errors`` from the same request-local
        # ``_err_sink`` right after this stage returns, so every stage --
        # shipped or injected -- gets the same treatment and an injected
        # implementation is never required to import the private
        # ``_ASK_MODEL_ERRORS`` ContextVar just to keep retrieval-side
        # warnings visible (codex #571 R2 P2).  Setting it here too would
        # only assign it twice with the same value.
        return ReasoningResponseDraft(
            notebook_id=notebook_id,
            question=prepared.question,
            response=response,
            conversation_id=conversation_id,
            user_id=prepared.user_id,
            job_id=prepared.job_id,
            asked_at=prepared.asked_at,
            baseline_manifest=final_baseline_manifest,
        )

