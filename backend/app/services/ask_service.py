"""Ask 模式引擎与答案合成 (Task 24) — chunk/reasoning/graph 三引擎、follow-up
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
  schemas(effective_schemas)、community_reports、source_titles。
* 持久化身份显式:``user_id`` 关键字由调用方传入(facade delegate 适配
  current_user().id;流式路径由 AskExecutionCoordinator 每次 start 传入),
  本模块绝不读请求 ContextVar、绝不 import facade/runtime、绝不开私有 DB 缝。
* 模型身份走注入的 process-owned provider；每个调用点使用稳定 workload ID
  解析只读 adapter，不读取请求用户的模型配置。
* 三模式派发仍以 ask_modes.ASK_MODES 冻结注册表为唯一真源(getattr 派发 +
  fast/global 退役别名);控制流与 facade 基线逐字一致 —— ask goldens
  (test_ask_repository_golden)按字节冻结着每条路径。
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.repositories.ports import (
        AskCandidatePort,
        AskGraphPort,
        AskModelClientProvider,
        PreparedAskTurn,
        RetrievalPort,
    )

from app.core.ask_context import _ASK_EMBED_CACHE, _ASK_MODEL_ERRORS
from app.core.ask_retrieval_policy import RetrievalEffort, ask_retrieval_limits
from app.core.config import Settings
from app.core.llm import cap_kwargs
from app.models.ask import (
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
from app.services.model_work import ModelNotConfiguredError
from app.services.prompts import (
    ANSWER_SCHEMA_HINT,
    FOLLOWUP_REWRITE_SCHEMA_HINT,
    answer_prompt,
    followup_rewrite_prompt,
)
from app.services.retrieval import RetrievedKnowledge, classify_evidence

# Matches both one provenance marker and the comma-group form models commonly
# emit (`[k1, k3]`). A group binds only when every key exists in id_map.
_MARKER_GROUP_RE = re.compile(r"\[((?:k\d+\s*,\s*)*k\d+)\]")

# Tolerant variant that ALSO matches malformed markers with internal whitespace
# (e.g. `[ k1]`). Used only to scrub citation-shaped tokens that did NOT bind to
# a real anchor, so no fabricated/malformed marker reaches the user. Kept
# separate from _MARKER_GROUP_RE so strict anchor resolution is unchanged.
_LOOSE_MARKER_GROUP_RE = re.compile(r"\[\s*k\d+(?:\s*,\s*k\d+)*\s*\]")


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


def _graph_classification_hits(
    *, top_hits, memory_hits, anchors, neighbour_relevance: float, notebook_id: str
):
    """Build graph-mode classifier evidence without inflating Memory scores.

    Graph nodes reached through a verified chain may inherit discounted seed
    relevance. Memory anchors already have their own retrieval score and must
    never be mistaken for graph neighbours.
    """
    hits = list(top_hits) + list(memory_hits)
    scored_oids = {hit.object_id for hit in hits}
    for anchor in anchors:
        if anchor.object_id in scored_oids or anchor.object_type == "memory":
            continue
        scored_oids.add(anchor.object_id)
        hits.append(RetrievedKnowledge(
            object_id=anchor.object_id,
            object_type=anchor.object_type,
            payload={"name": anchor.name},
            relevance=neighbour_relevance,
            tier=getattr(anchor, "tier", "personal"),
            notebook_id=notebook_id,
        ))
    return hits


def _strip_unbound_markers(answer: str, bound_keys: set) -> str:
    """Normalise the `[k…]`-shaped tokens in `answer` against `bound_keys` (the
    keys that actually resolved to an anchor):
      - key in bound_keys  → rewrite to the canonical `[key]` form (repairs a
        malformed spaced `[ k1]` so it reads as a clean citation, not a fabricated
        one, while still pointing at its real anchor);
      - key not in bound_keys → drop the token (out-of-map ids like `[k99]`, or a
        spaced id with no anchor).
    Collapses the double space a removed mid-sentence marker would leave behind."""
    def _sub(m: re.Match) -> str:
        keys = [part.strip() for part in m.group(0).strip("[]").split(",")]
        # Mixed known/unknown groups fail closed. Keeping only the known subset
        # would silently alter which premises the sentence claims to cite.
        return ("[" + ", ".join(keys) + "]"
                if keys and all(key in bound_keys for key in keys) else "")
    cleaned = _LOOSE_MARKER_GROUP_RE.sub(_sub, answer or "")
    # A stripped marker between words leaves "word  word"; normalise to one space
    # without disturbing newlines / other whitespace runs the model intended.
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def knowledge_record(object_type: str, obj: dict, schema) -> KnowledgeRecord:
    """KG 对象 → KnowledgeRecord 展示投影(canonical body;facade 的
    _knowledge_record 与 ask_reasoning 的 related_knowledge 共用)。"""
    from app.services.knowledge_governance import knowledge_headline

    payload = obj.get("payload") or {}
    keys = (
        schema.fields
        if schema
        else [k for k in payload if not str(k).startswith("_")]
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
        graph: "AskGraphPort",
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
        community_reports: Callable[[str], list],
        source_titles: Callable[[List[str]], Dict[str, str]],
        knowhow_store=None,
        memory_retriever=None,
        current_user_id: Callable[[], str] = lambda: "",
        cancellations=None,
        collection_catalog=None,
        collection_enumeration=None,
    ) -> None:
        self.ask_state = ask_state
        self.retrieval = retrieval
        self.candidates = candidates
        self.graph = graph
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
        self.community_reports = community_reports
        self.source_titles = source_titles
        self.knowhow_store = knowhow_store
        self.memory_retriever = memory_retriever
        self.current_user_id = current_user_id
        self.cancellations = cancellations
        # 逐步推理的类型化集合地图/清单服务。缺省 None:窄测试替身与未接线的
        # 组合根照旧可构造,只是那条 run 不提供枚举工具。
        self.collection_catalog = collection_catalog
        self.collection_enumeration = collection_enumeration

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
        from app.services.ask_modes import resolve_mode

        spec = resolve_mode(getattr(payload, "mode", None))
        handler = getattr(self, spec.handler)
        if spec.streaming:
            return handler(notebook_id, payload, user_id=user_id,
                           job_id=job_id, on_trace=on_trace, cancel_event=cancel_event)
        return handler(notebook_id, payload, user_id=user_id,
                       job_id=job_id, cancel_event=cancel_event)

    def ask_current(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Run the synchronous Ask surface through the durable job lifecycle.

        Streaming and synchronous callers now share the same state-store
        primitives: create/touch conversation plus running job atomically,
        pass the job into the engine's atomic final save, then finalize and
        unregister. The job id remains internal to this blocking protocol.
        """
        from app.services.ask_modes import resolve_mode

        user_id = self.current_user_id()
        mode = resolve_mode(getattr(payload, "mode", None))
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

    def ask_graph_current(
        self, notebook_id: str, payload: AskRequest,
        seed_ids: Optional[List[str]] = None, cancel_event: CancelEvent = None,
    ) -> AskResponse:
        return self.ask_graph(
            notebook_id, payload, user_id=self.current_user_id(),
            seed_ids=seed_ids, cancel_event=cancel_event,
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
        if self.memory_retriever is None:
            return []
        return self.memory_retriever.notebook_memory_hits(
            user_id, notebook_id, query, 8
        )

    def preview_reasoning_intent(
        self,
        question: str,
        history: str = "",
        cancel_event: CancelEvent = None,
    ) -> QueryIntentContract:
        """Understand a reasoning request before any corpus retrieval starts."""
        from app.services.query_intent import plan_query_intent

        contract = plan_query_intent(
            self.model_clients.chat("reasoning_agent"),
            question,
            history,
            max_topics=self.settings.reasoning_max_subqueries,
            purpose="step-by-step evidence-grounded answer",
            cancel_event=cancel_event,
        )
        return QueryIntentContract(**contract)

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

    # ------------------------------------------------------------------
    # synthesis helpers
    # ------------------------------------------------------------------

    def _answer_chunks(
        self,
        question,
        chunks,
        history="",
        cancel_event: CancelEvent = None,
        notebook_id: str = "",
        memory_hits=None,
        llm_client=None,
    ) -> tuple:
        """长上下文综合:把 MMR 精选的 chunk 原文喂给答案 LLM。返回
        (answer, llm_grounded, anchors)。复用 answer_prompt 的 [k] 标注协议。
        notebook_id:转发给 _chunk_answer_context 解 anchor.tier(见其 docstring);
        chunk 自带 notebook_id(跨库召回)优先,这只是单库 chunk 的回退值。"""
        raise_if_cancelled(cancel_event)
        context_block, id_map = self._chunk_answer_context(chunks, notebook_id=notebook_id)
        context_block, id_map = self._append_memory_context(
            context_block, id_map, memory_hits or []
        )
        llm_client = llm_client or self.model_clients.chat("ask_answer")
        raw = llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
            cancel_event=cancel_event,
            **cap_kwargs(llm_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
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
    ) -> tuple:
        """mix 长上下文综合:chunk 段(k1..kN)+ KG 段(k1001+),统一 id_map。
        chunk 段不再二次预算(选择阶段已 token 预算),故 budget_chars 给极大值。
        返回 (answer, llm_grounded, anchors)。notebook_id:转发给 _chunk_answer_context
        解 anchor.tier(见其 docstring);chunk 自带 notebook_id(跨库召回)优先,
        这只是单库 chunk 的回退值。"""
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
        llm_client = llm_client or self.model_clients.chat("ask_answer")
        raw = llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
            cancel_event=cancel_event,
            **cap_kwargs(llm_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
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
                       + "\n".join(f"- {x}" for x in rel[:12])
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
        可追踪:前端横幅 + events.jsonl)。返回 (answer, grounded, anchors, ok)。"""
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
        sectioned: bool = False,
        key_offset: int = 0,
        section_title: str = "",
        section_index: int = 0,
        section_total: int = 0,
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
        ——它们不可被大纲绑定,留在回退路径上(v1 刻意的边界)。"""
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
                budget_chars=chunk_budget,
            )
        if structured_block:
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, structured_block, structured_map or {},
                budget_chars=chunk_budget,
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
            )

        # Direct source elements share the Chunk/source partition instead of
        # bypassing its advertised hard ceiling. Admit the top `element_items`
        # by retrieval relevance descending (tie-break element_id) rather than
        # whatever order the retriever happened to collect them in — an
        # insertion-order slice silently drops the most relevant elements
        # whenever the retriever collects more than the cap.
        if elements and len(source_context) < chunk_budget:
            ranked_elements = sorted(
                elements, key=lambda e: (-float(e.score or 0.0), e.element_id)
            )[:element_items]
            element_block, element_id_map = self.evidence_context.element_context(
                ranked_elements, notebook_id=notebook_id,
                id_offset=key_offset + self._ELEMENT_KEY_BASE,
                budget_chars=max(0, chunk_budget - len(source_context)),
            )
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, element_block, element_id_map,
                budget_chars=chunk_budget, heading="Direct source elements",
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
            )
        if chains:
            from app.services.kg.follow_chain import render_follow_chain_context
            chain_block, chain_id_map = render_follow_chain_context(
                chains, id_offset=key_offset + 2000,
                active_notebook_id=notebook_id)
            kg_context, kg_map = self._bounded_context_append(
                kg_context, kg_map, chain_block, chain_id_map,
                budget_chars=effective_kg_budget, heading="Derived chains",
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
        raw = answer_client.chat_json(
            [{"role": "user", "content": answer_prompt(
                question, context_block, history,
                sectioned=sectioned,
                section_title=section_title,
                section_index=section_index,
                section_total=section_total,
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
        llm_grounded = bool(data.get("grounded", False))
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
    ):
        """按节合成(设计文档 §3.1):每节一次合成调用,只喂该节绑定的证据。

        返回 ``_SectionedSynthesis``;**任何一节失败就返回 None**,由调用方整体
        回退到单次合成。绝不半篇拼接:少写一节的答案与「这一节没有内容」在屏幕上
        长得一模一样,而实际发生的是一次模型故障 —— 宁可多付一次单次合成,也不
        交付一份看不出缺口的残篇。

        每节沿用 ``_answer_with_retry`` 的重试语义(空 content 重掷一次、两次皆空
        才记 model_error),所以「思考型模型偶发空 content」不会把整轮拖进回退。
        取消异常照常穿透 —— 用户按了中断不是合成失败。

        ``on_section(step)`` 在每节写完后立刻收到一条轻量进度步。分节合成是整轮
        里最长的一段(k 次模型调用),不发进度的话实时轨迹会在「合成」上静止几分钟。
        **进度步实时发出、不因后续回退而回收**:那一节确实写完了、那笔钱确实付了,
        事后抹掉等于让轨迹少报整轮做过的工作;回退由收尾那条 synthesis 步的
        ``outline_fallback`` 说清楚。
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
        total = len(slices)
        model_label = getattr(answer_client, "model", "")
        for item in slices:
            section_counts: dict = {}
            section_started = time.perf_counter()

            def _synth_section(item=item, section_counts=section_counts):
                text_, grounded_, anchors_, _counts = self._answer_reasoning(
                    notebook_id, question, item.hits, item.elements, history,
                    cancel_event=cancel_event, chunks=item.chunks,
                    answer_client=answer_client,
                    kg_context_chars=limits.kg_context_chars,
                    chunk_context_chars=limits.chunk_context_chars,
                    element_items=limits.answer_element_items,
                    counts_sink=section_counts,
                    sectioned=True,
                    key_offset=item.key_offset,
                    section_title=item.section.title,
                    section_index=item.index + 1,
                    section_total=total,
                )
                return text_, grounded_, anchors_

            text, section_grounded, section_anchors, ok = self._answer_with_retry(
                _synth_section, model_label, service="ask_answer",
            )
            if not ok:
                return None
            if on_section is not None:
                # 复用 synthesis 这个 step_type:前端 reasoning-trace.ts 的
                # synthesis 分支只在 `detail.anchors` 是数字时给出细节文案,进度步
                # 不带 anchors,于是它自然只显示 summary —— 不必等 O3 补渲染就
                # 是可读的,而 O3 想把节标题做成细节行时 detail 已经在那里了。
                on_section(TraceStep(
                    step_type="synthesis",
                    summary=f"已写完第 {item.index + 1}/{total} 节",
                    detail={
                        "section_index": item.index + 1,
                        "section_total": total,
                        "section_title": item.section.title[:60],
                    },
                    duration_ms=round(
                        (time.perf_counter() - section_started) * 1000),
                ))
            for key in merged_counts:
                merged_counts[key] += int(section_counts.get(key, 0) or 0)
            rendered.append((item, text))
            grounded = grounded or section_grounded
            # 号段互不相交,所以跨节不可能撞 key;去重只防同一节内重复标记(
            # parse_anchors 自己已经去过一次,这一层是拼接侧的常数级防御)。
            for anchor in section_anchors:
                if anchor.key in seen_anchor_keys:
                    continue
                seen_anchor_keys.add(anchor.key)
                merged_anchors.append(anchor)
        return _SectionedSynthesis(
            answer=outline_answer_text(rendered),
            # 任一节自报 grounded 即算 grounded:每节的 grounded 只描述**它自己**
            # 的切片,全篇要求 all 会让一节合理的常识补充把整篇降级。真正的门仍在
            # classify_evidence —— 它还要求存在相关度 >= tau_high 的**被引用**证据,
            # 所以 any 单独抬不高任何东西。
            llm_grounded=grounded,
            anchors=merged_anchors,
            sections=len(rendered),
            counts=merged_counts,
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
        raise_if_cancelled(cancel_event)
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
            from app.services.retrieval import quota_fuse, est_tokens, truncate_by_tokens
            ex = None
            raise_if_cancelled(cancel_event)
            if self.settings.query_rewrite_enabled:
                ex = expand_query(self.model_clients.chat("query_rewrite"),
                                  retrieval_query, history,
                                  max_subqueries=self.settings.chunk_max_subqueries,
                                  corpus_langs=self.candidates.notebook_languages(notebook_id),
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
            _t = time.perf_counter()
            raise_if_cancelled(cancel_event)
            if plan.strategy == "mix":
                candidates, kg_block, kg_id_map, kg_hits, concept_walk_n = (
                    self.candidates.mixed_chunk_candidates(
                        notebook_id, retrieval_query, hl, sub_queries))
                # ∪ bilingual-keyword chunk hits (dedup by chunk_id; keep existing on collision)
                candidates = self.candidates.merge_chunk_candidates(candidates, kw_hits)
                # ∪ exact-identifier whole-section hits (same dedup contract)
                candidates = self.candidates.merge_chunk_candidates(candidates, exact_hits)
                raise_if_cancelled(cancel_event)
                rerank_client = self.model_clients.rerank("retrieval_rerank")
                order = rerank_client.rerank(
                    retrieval_query, [c.text for c in candidates],
                    on_error=lambda e: self.model_errors.note_model_error(
                        "rerank",
                        e,
                        workload_id="retrieval_rerank",
                    ))
                raise_if_cancelled(cancel_event)
                ranked = [candidates[i] for i in order]
                kg_budget = self.settings.max_entity_tokens + self.settings.max_relation_tokens
                kg_block = self.evidence_context.truncate_kg_block(kg_block, kg_budget)
                chunk_budget = max(0, self.settings.max_total_tokens
                                   - est_tokens(kg_block) - self._MIX_PROMPT_BUFFER_TOKENS)
                from app.services.retrieval import (
                    exact_section_reserve_rule, graph_reserve_rule,
                    select_with_reserves,
                )

                # Two floors inside ONE budget. The reranker is a general
                # relevance model and routinely ranks an Arguments table below
                # prose that merely talks about the command, so without a
                # reserved seat the exact section is assembled and then
                # truncated away. Neither reserve enlarges the budget nor
                # evicts what the other is holding; a reserve set to 0 is fully
                # inert, so `chunk_graph_reserve` keeps its historical
                # behaviour byte-for-byte.
                selected = select_with_reserves(ranked, chunk_budget, (
                    graph_reserve_rule(max(0, self.settings.chunk_graph_reserve)),
                    exact_section_reserve_rule(
                        max(0, self.settings.exact_section_reserve), exact_ids),
                ))
                ask_stage("mix_rerank", _t, recall=len(candidates),
                          selected=len(selected), kg_nodes=len(kg_id_map),
                          concept_walk=concept_walk_n)
            elif plan.strategy == "multi":
                collected, per_query, _ids, _mat = (
                    self.candidates.retrieve_chunk_candidates_multi(notebook_id, sub_queries))
                raise_if_cancelled(cancel_event)
                # ∪ bilingual-keyword chunk hits: merge into collected (best relevance)
                # and add as an extra per_query group so quota_fuse can surface them.
                if kw_hits:
                    for c in kw_hits:
                        cur = collected.get(c.chunk_id)
                        if cur is None or c.relevance > cur.relevance:
                            collected[c.chunk_id] = c
                    per_query = per_query + [{c.chunk_id: c for c in kw_hits}]
                # ∪ exact-identifier whole-section hits, treated identically:
                # its own per_query group is what gives quota_fuse a reason to
                # surface a section chunk whose standalone relevance is low.
                if exact_hits:
                    for c in exact_hits:
                        cur = collected.get(c.chunk_id)
                        if cur is None or c.relevance > cur.relevance:
                            collected[c.chunk_id] = c
                    per_query = per_query + [{c.chunk_id: c for c in exact_hits}]
                selected, _counts = quota_fuse(collected, per_query, plan.fuse_k,
                                               relevance=lambda c: c.relevance)
                ask_stage("retrieve_fuse", _t, recall=len(collected), selected=len(selected))
            else:
                scored, ids, mat = self.candidates.retrieve_chunk_candidates(
                    notebook_id, sub_queries[0])
                # ∪ bilingual-keyword chunk hits (dedup by chunk_id; keep existing on collision)
                scored = self.candidates.merge_chunk_candidates(scored, kw_hits)
                # ∪ exact-identifier whole-section hits (same dedup contract)
                scored = self.candidates.merge_chunk_candidates(scored, exact_hits)
                raise_if_cancelled(cancel_event)
                selected = self.candidates.select_chunk_candidates(
                    scored, ids, mat, plan.mmr_k, plan.mmr_lambda)
                ask_stage("retrieve_mmr", _t, recall=len(scored), selected=len(selected))

            answer, llm_grounded, anchors = "", False, []
            synth_failed = False
            _t = time.perf_counter()
            raise_if_cancelled(cancel_event)
            answer_client = self.model_clients.chat("ask_answer")
            if answer_client.configured and (selected or kg_id_map or memory_hits):
                # 空 content 有界重试 + 诚实降级 + 可观测(见 _answer_with_retry docstring)。
                answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                    lambda: (self._answer_mix(
                                 question, selected, kg_block, kg_id_map, history,
                                 cancel_event=cancel_event, notebook_id=notebook_id,
                                 memory_hits=memory_hits, llm_client=answer_client)
                             if overlay_on else
                             self._answer_chunks(
                                 question, selected, history, cancel_event=cancel_event,
                                 notebook_id=notebook_id, memory_hits=memory_hits,
                                 llm_client=answer_client)),
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
            citation_titles = self.evidence_context.citation_titles(
                c.source_id for c in selected
            )
            if overlay_on:
                by_id = {c.chunk_id: c for c in selected}
                for a in anchors:
                    if a.object_type == "chunk" and a.object_id in by_id:
                        c = by_id[a.object_id]
                        eid = c.element_ids[0] if c.element_ids else ""
                        source_title = citation_titles.get(c.source_id, c.source_title)
                        citations.append(Citation(
                            label=f"{source_title} · {c.section_path}".strip(" ·"),
                            source_id=c.source_id, element_id=eid,
                            location_label=c.section_path, quoted_span=c.text[:200],
                            tier=_chunk_tier(c), notebook_id=_cite_notebook_id(c),
                            knowhow=knowhow_refs.get(eid)))
            else:
                for c in selected:
                    eid = c.element_ids[0] if c.element_ids else ""
                    source_title = citation_titles.get(c.source_id, c.source_title)
                    citations.append(Citation(
                        label=f"{source_title} · {c.section_path}".strip(" ·"),
                        source_id=c.source_id, element_id=eid,
                        location_label=c.section_path, quoted_span=c.text[:200],
                        tier=_chunk_tier(c), notebook_id=_cite_notebook_id(c),
                        knowhow=knowhow_refs.get(eid)))
            citations.extend(self._memory_citations(anchors, memory_hits))

            # grounding 在 chunk∪KG 合并集上;各项用其融合 relevance(rerank 分不参与)。
            combined_hits = list(selected) + list(kg_hits) + list(memory_hits)
            evidence_level, top_relevance = classify_evidence(
                combined_hits, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
            grounded = evidence_level == "grounded"

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
        """Reasoning-mode ask: agentic plan→retrieve→reflect(自由深挖)→answer。
        检索委托 ReasoningRetriever;答案/证据分档复用 fast 路径口径;响应携带
        reasoning_trace。任何阶段异常不向用户抛出(逐层容错 + 兜底空候选)。"""
        from app.services.reasoning_retrieval import ReasoningRetriever
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
        raise_if_cancelled(cancel_event)
        intent_contract = self._confirmed_reasoning_intent(payload, history)
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
        intent_step = TraceStep(
            step_type="intent",
            summary="已按确认后的问题理解开始检索",
            detail={
                "resolved_question": intent_contract.resolved_question,
                "result_scope": intent_contract.result_scope,
                "completeness_required": intent_contract.completeness_required,
                "retrieval_effort": payload.retrieval_effort,
                "entities": intent_contract.entities,
                "constraints": intent_contract.constraints,
                "excluded_topics": intent_contract.excluded_topics,
                "assumptions": intent_contract.assumptions,
                "expected_output": intent_contract.expected_output,
                "mandatory_topics": [
                    topic.question for topic in intent_contract.mandatory_topics
                ],
            },
            # The understanding phase runs entirely in ``/ask/intent``, before
            # this durable job exists, so the server cannot time it.  The UI
            # reports what it measured; without it the replayed trace would
            # silently drop that whole phase from the run's total.
            duration_ms=(
                payload.intent.understanding_ms if payload.intent is not None else None
            ),
        )
        pre_trace: list[TraceStep] = [intent_step]
        intent_streamed = False

        def stream_intent() -> None:
            nonlocal intent_streamed
            if not intent_streamed and on_trace:
                on_trace(intent_step)
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
        if intent_contract.completeness_required and self.knowhow_store is not None:
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
                retrieval_effort=payload.retrieval_effort,
                result_sets=structured_batch.result_sets,
                result_coverage=structured_batch.coverage(),
            )
            response.mode = "reasoning"
            raise_if_cancelled(cancel_event)
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id,
                user_id=user_id, job_id=job_id, asked_at=payload.asked_at,
            )
            return response

        # Stream the intent step BEFORE memory retrieval.  Memory hits cost an
        # embedding round trip plus a vector scan, and until this step lands the
        # UI has nothing after the synthetic "start" — the reader is left
        # watching an empty trace through work that is already under way.
        stream_intent()
        memory_started = time.perf_counter()
        memory_hits = self._memory_hits(
            user_id, notebook_id, research_question
        )
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
                    retrieval_effort=payload.retrieval_effort,
                    result_sets=structured_batch.result_sets,
                    result_coverage=structured_batch.coverage(),
                    model_errors=[ModelError(
                        stage="answer", model="", message="missing_config"
                    )],
                )
                response.mode = "reasoning"
                response.answer_id = self._save_answer(
                    notebook_id, question, response, conversation_id,
                    user_id=user_id, job_id=job_id, asked_at=payload.asked_at,
                )
                return response
            return self._unconfigured_model_response(
                notebook_id, question, conversation_id, "reasoning",
                user_id=user_id, job_id=job_id, asked_at=payload.asked_at,
                intent=intent_contract,
                retrieval_query=research_question,
                retrieval_effort=payload.retrieval_effort,
                completeness_unavailable=completeness_unavailable,
                reasoning_trace=streamed_pre_trace())

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
                retrieval_effort=payload.retrieval_effort,
                result_sets=(structured_batch.result_sets if structured_batch else []),
                result_coverage=(
                    structured_batch.coverage() if structured_batch else None
                ),
                reasoning_trace=(
                    pre_trace
                    if structured_batch is not None else streamed_pre_trace()
                ))
            response.mode = "reasoning"
            raise_if_cancelled(cancel_event)
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id,
                user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
            return response

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        # P1-A(本轮 scope):只挂 reasoning 模式。ask_graph/ask_chunk 同样受益,
        # 但等价性回放验证只覆盖了 reasoning——留作后续 fast-follow。
        _emb_token = _ASK_EMBED_CACHE.set({})
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

            try:
                # 端口化构造(与冻结的 from_repository 工厂逐字段同源):检索/模型/
                # 社区端口直通,communities 逐次新建 —— sibling_min_bridge 调用时读。
                result = ReasoningRetriever(
                    retrieval=self.retrieval,
                    model_clients=self.model_clients,
                    communities=self.communities(),
                    settings=self.settings,
                    cancel_event=cancel_event,
                    collection_catalog=self.collection_catalog,
                    collection_enumeration=self.collection_enumeration,
                ).run(
                    notebook_id,
                    research_question,
                    history,
                    on_step=checked_trace,
                    intent_queries=intent_queries,
                    limits=limits,
                )
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
            except AskCancelled:
                raise
            except Exception:
                top_hits, elements, trace, chunks, chains = (
                    [], [], list(pre_trace), [], []
                )
                enumerations = []
                collection_map_text = ""
                reasoning_outline = []
                reasoning_outline_evidence = []

            registry = self.schemas.effective_schemas()
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
                    registry.get(item.object_type)))
            related_knowledge = related_knowledge[:12]

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
                    notebook_id, research_question, top_hits, elements, history,
                    cancel_event=cancel_event, chunks=chunks, chains=chains,
                    memory_hits=memory_hits, answer_client=answer_client,
                    kg_context_chars=limits.kg_context_chars,
                    chunk_context_chars=limits.chunk_context_chars,
                    element_items=limits.answer_element_items,
                    structured_block=structured_block,
                    structured_map=structured_map,
                    collection_map_block=collection_map_prompt_block,
                    counts_sink=reasoning_counts)
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
                    if len(outline_slices) >= 2:
                        outline_attempted = True
                        sectioned = self._answer_reasoning_sections(
                            notebook_id, research_question, outline_slices,
                            history=history, limits=limits,
                            answer_client=answer_client, cancel_event=cancel_event,
                            on_section=record_section_step,
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
                    # 只摘分节那一段(mark..fallback_mark),单次合成自己记的那几条
                    # 原样保留 —— 「重试一次才成功」在既有口径里就是要报的。
                    del _err_sink[outline_err_mark:fallback_err_mark]

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
            element_titles = self.evidence_context.citation_titles(
                item.source_id for item in elements
            )
            element_tier = self._tier_map_for({notebook_id}).get(
                notebook_id, "personal"
            )
            for anchor in anchors:
                if anchor.object_type != "element" or anchor.object_id not in element_by_id:
                    continue
                item = element_by_id[anchor.object_id]
                source_title = element_titles.get(item.source_id, item.source_title)
                citations.append(Citation(
                    label=f"{source_title} · {item.location_label}".strip(" ·"),
                    source_id=item.source_id,
                    element_id=item.element_id,
                    location_label=item.location_label,
                    quoted_span=item.text[:200],
                    tier=element_tier,
                    notebook_id="",
                    knowhow=element_refs.get(item.element_id),
                ))
            # Typed collection rows have their own deterministic k5001+
            # bindings.  Mirror the cited rows into the fallback Citation
            # contract as well, while keeping the anchor path authoritative.
            seen_collection_citations: set[tuple[str, str]] = set()
            for anchor in anchors:
                citation = collection_item_citations.get(anchor.object_id)
                if citation is None:
                    continue
                identity = (citation.source_id, citation.element_id)
                if identity in seen_collection_citations:
                    continue
                seen_collection_citations.add(identity)
                citations.append(citation)
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
            grounded = evidence_level == "grounded"

            # An enumeration answer with no bound [k] marker has no verifiable
            # attribution.  Do not let unrelated ranked-retrieval citations
            # masquerade as sources for a checklist the model may have copied.
            if enumerations and synthesis_ran and not anchors:
                citations = []

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
                    },
                    duration_ms=round((time.perf_counter() - synthesis_started) * 1000),
                )
                if outline_attempted:
                    # 三个键只在按节合成**真的被尝试过**时出现:没有大纲、低档位
                    # 与关闭态下 synthesis 步的 detail 逐键不变(冻结基线口径)。
                    synthesis_step.detail.update({
                        # 实际合成的节数;回退时为 0(分节产物已全部丢弃)。
                        "outline_sections": (
                            sectioned.sections if sectioned is not None else 0
                        ),
                        "outline_fallback": outline_fallback,
                        # 被跳过的空节标题:它们是「问到了但没找到」的诚实记录,
                        # 只在轨迹里露面,答案里不留空壳标题。
                        "outline_skipped": outline_skipped,
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
                conclusion = (
                    "The notebook does not yet contain approved knowledge that matches "
                    "this question. Upload and review sources to build coverage.")

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
                retrieval_effort=payload.retrieval_effort,
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
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
            _ASK_EMBED_CACHE.reset(_emb_token)
        response.mode = "reasoning"
        response.model_errors = [ModelError(**e) for e in _err_sink]
        raise_if_cancelled(cancel_event)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id,
            user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
        return response

    # ------------------------------------------------------------------
    # graph engine
    # ------------------------------------------------------------------

    def ask_graph(
        self,
        notebook_id: str,
        payload: "AskRequest",
        *,
        user_id: str,
        job_id: str = "",
        seed_ids: Optional[List[str]] = None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """Multi-hop graph reasoning mode.

        1. Retrieve top seeds via federated_retrieve (active + base-tier notebooks).
        2. Build the federated rx graph via _federated_rx_graph.
        3. BFS from seed object_ids along DEFAULT_REASONING_EDGES.
        4. Render subgraph → (context_block, id_map) via render_subgraph_context.
        5. Feed context_block to the existing answer LLM + grounding path.

        The [k] anchor markers, _parse_answer_anchors, and classify_evidence are
        shared helpers reused across ask modes. There is no longer a "fast path" —
        ask_fast was retired in P4-5; _answer_kg also deleted (dead code). Context
        is now evidence-refined via the ``evidence_refine`` workload before being
        fed to the ``ask_answer`` workload.
        """
        from app.services.kg.graph_reason import (
            DEFAULT_REASONING_EDGES, multihop_subgraph, render_subgraph_context,
        )
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
        raise_if_cancelled(cancel_event)
        memory_hits = self._memory_hits(user_id, notebook_id, question)

        if self._primary_llm_unconfigured():
            return self._unconfigured_model_response(
                notebook_id, question, conversation_id, "graph",
                user_id=user_id, job_id=job_id, asked_at=payload.asked_at)

        if not memory_hits and not (
                self.candidates.has_kg(notebook_id)
                or self.candidates.any_base_has_kg(notebook_id)):
            response = AskResponse(
                answer_id="",
                conclusion="本笔记本尚未构建知识图谱,也没有已建图的参考库;"
                           "请先点『构建知识图谱』,或为本笔记本挂载一个已建图的"
                           "公共知识库。",
                conversation_id=conversation_id, retrieval_query=question,
                llm_mode="deterministic", kg_required=True)
            response.mode = "graph"
            raise_if_cancelled(cancel_event)
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id,
                user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
            return response

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        try:
            # Seed: top-N by relevance (federated across base notebooks).
            raise_if_cancelled(cancel_event)
            top_hits = self.retrieval.federated_retrieve(
                notebook_id, question)[:self.settings.retrieval_top_n]
            raise_if_cancelled(cancel_event)
            if not top_hits and not seed_ids:
                if not memory_hits:
                    response = AskResponse(
                        answer_id="",
                        conclusion="The notebook does not yet contain approved knowledge "
                                   "that matches this question. Upload and review sources "
                                   "to build coverage.",
                        conversation_id=conversation_id, retrieval_query=question,
                        llm_mode="deterministic",
                    )
                else:
                    answer_client = self.model_clients.chat("ask_answer")
                    answer, llm_grounded, anchors, ok = self._answer_with_retry(
                        lambda: self._answer_chunks(
                            question, [], history, cancel_event=cancel_event,
                            notebook_id=notebook_id, memory_hits=memory_hits,
                            llm_client=answer_client,
                        ),
                        getattr(answer_client, "model", ""),
                    )
                    evidence_level, top_relevance = classify_evidence(
                        memory_hits, anchors, llm_grounded,
                        self.settings.evidence_tau_low,
                        self.settings.evidence_tau_high,
                    )
                    response = AskResponse(
                        answer_id="",
                        conclusion=(
                            _MARKER_GROUP_RE.sub("", answer).strip()
                            if answer else
                            "Confirmed Memory matched, but answer synthesis failed."
                        ),
                        answer=answer,
                        grounded=evidence_level == "grounded",
                        evidence_level=evidence_level,
                        anchors=anchors,
                        citations=self._memory_citations(anchors, memory_hits),
                        conversation_id=conversation_id,
                        retrieval_query=question,
                        top_relevance=top_relevance,
                        llm_mode=(
                            "grounded" if evidence_level == "grounded"
                            else "ungrounded" if ok else "synthesis_failed"
                        ),
                    )
                response.mode = "graph"
                response.model_errors = [ModelError(**e) for e in _err_sink]
                raise_if_cancelled(cancel_event)
                response.answer_id = self._save_answer(
                    notebook_id, question, response, conversation_id,
                    user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
                return response

            # HippoRAG 式 PPR 跨文档检索(opt-in)。命中即走 chunk 答案路径:PPR 把
            # 别的文档相关 chunk 也召回,_answer_chunks 出 chunk 引用(跨多篇)。
            if self.settings.graph_ppr_enabled:
                raise_if_cancelled(cancel_event)
                ppr_chunks = self.retrieval.ppr_retrieve(notebook_id, question)
                raise_if_cancelled(cancel_event)
                if ppr_chunks:
                    from app.services.retrieval import RetrievedChunk, RetrievalSupport
                    reports = self.community_reports(
                        notebook_id)[: self.settings.ppr_community_context_top_n]
                    community_chunks = [RetrievedChunk(
                        chunk_id=f"community:{i}", source_id="",
                        source_title="Knowledge base theme", section_path=r["title"],
                        text=f"{r['title']}. {r['summary']}", element_ids=[], relevance=1.0,
                        retrieval_supports=(RetrievalSupport(
                            "kg_source", "object", str(r.get("community_id") or ""), 1.0
                        ),))
                        for i, r in enumerate(reports)]
                    ppr_chunks = community_chunks + ppr_chunks
                    answer, llm_grounded, anchors = "", False, []
                    synth_failed = False
                    answer_client = self.model_clients.chat("ask_answer")
                    if getattr(answer_client, "configured", False):
                        answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                            lambda: self._answer_chunks(
                                question, ppr_chunks, history, cancel_event=cancel_event,
                                notebook_id=notebook_id, memory_hits=memory_hits,
                                llm_client=answer_client),
                            getattr(answer_client, "model", ""),
                        )
                        synth_failed = not _ok
                    citations: List[Citation] = []
                    by_id = {c.chunk_id: c for c in ppr_chunks}
                    # ppr_chunks = 合成 community_chunks(无 notebook_id,回退本 nb)
                    # + PPR 检索结果(可掺 base 库 chunk,notebook_id 已标)。
                    ppr_tier_map = self._tier_map_for(
                        {c.notebook_id or notebook_id for c in ppr_chunks})
                    # Task 14 codex r4 fix: _ppr_retrieve 对 active 库自己的命中
                    # 同样会打上 active 自己的 id(scale_ppr 的 combined_chunk_ids
                    # 跨 base ⊕ active,逐 chunk 原样带出 chunk_notebook_id,并非
                    # 只在跨库命中时才打标)——citations_from 同一根因的镜像修复
                    # (见 evidence_context.py citations_from 的 codex r4 fix 注
                    # 释)。这里直接构造 Citation,必须同样与调用方 notebook_id
                    # 比较,相等则归零,否则前端会显示一个多余的「来自「当前笔记
                    # 本」」徽章。
                    def _cite_notebook_id(c) -> str:
                        return c.notebook_id if c.notebook_id != notebook_id else ""
                    # Task 12b(引用跳转扩面):graph 模式的 PPR 引用同样此前从未
                    # 富化过 citation.knowhow——批量查一次,覆盖 ppr_chunks 里每
                    # 个 chunk 的首个 element_id,一次 store 读取(同 ask_chunk
                    # 侧口径,运行效率是一等约束)。
                    knowhow_refs = self.evidence_context.knowhow_refs_for(
                        c.element_ids[0] for c in ppr_chunks if c.element_ids)
                    citation_titles = self.evidence_context.citation_titles(
                        c.source_id for c in ppr_chunks
                    )
                    for a in anchors:
                        if a.object_type == "chunk" and a.object_id in by_id:
                            c = by_id[a.object_id]
                            eid = c.element_ids[0] if c.element_ids else ""
                            source_title = citation_titles.get(c.source_id, c.source_title)
                            citations.append(Citation(
                                label=f"{source_title} · {c.section_path}".strip(" ·"),
                                source_id=c.source_id, element_id=eid,
                                location_label=c.section_path, quoted_span=c.text[:200],
                                tier=ppr_tier_map.get(c.notebook_id or notebook_id, "personal"),
                                notebook_id=_cite_notebook_id(c),
                                knowhow=knowhow_refs.get(eid)))
                    citations.extend(self._memory_citations(anchors, memory_hits))
                    evidence_level, top_relevance = classify_evidence(
                        list(ppr_chunks) + list(memory_hits), anchors, llm_grounded,
                        self.settings.evidence_tau_low, self.settings.evidence_tau_high)
                    grounded = evidence_level == "grounded"
                    if answer:
                        conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                        llm_mode = "grounded" if grounded else "ungrounded"
                    elif synth_failed:
                        conclusion = (
                            f"已检索到 {len(ppr_chunks)} 条跨文档相关内容,但本次答案合成未产出内容"
                            "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。")
                        llm_mode = "synthesis_failed"
                    else:
                        conclusion = f"PPR retrieved {len(ppr_chunks)} cross-document passage(s)."
                        llm_mode = "deterministic"
                    resp = AskResponse(
                        answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                        evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                        citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                        retrieval_query=question, top_relevance=top_relevance,
                        reasoning_trace=[TraceStep(step_type="ppr",
                            summary=f"概念漫游:跨文档召回 {len(ppr_chunks)} 个 chunk",
                            detail={"chunks": len(ppr_chunks),
                                    "sources": len({c.source_id for c in ppr_chunks})})])
                    resp.mode = "graph"
                    resp.model_errors = [ModelError(**e) for e in _err_sink]
                    raise_if_cancelled(cancel_event)
                    resp.answer_id = self._save_answer(
                        notebook_id, question, resp, conversation_id,
                        user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
                    return resp

            # 大库守卫(与 PPR 检索的 Fix 1 同一「大」定义):下方
            # _federated_rx_graph 是全库 rustworkx 建图(Python 边循环),在百万级
            # 节点库上=数十分钟 + 数 GB 内存(与 1.13M 节点 reasoning 冻结同机制)。
            # 空图喂给 multihop_subgraph 的下游是 subgraph=[] → src_chunks=[] →
            # id_map={} → 不调答案 LLM → 只剩 "Graph traversal found 0 node(s)"
            # 的空壳 deterministic 文案 —— 对用户等于空答案。故大库直接早退一条
            # 带解释的降级回答(镜像上方无 KG 时的 deterministic 回答形态),
            # 并发 graph_walk_refused 事件;顺带省掉 _graph_seed_fusion 的
            # expand_query LLM 调用。放在 PPR 分支之后:大库若有 scale 索引,
            # PPR 分支仍可正常出跨文档答案,不受此守卫影响。
            if self.candidates.graph_is_large(notebook_id):
                self.event_log.emit({
                    "kind": "graph_walk_refused",
                    "notebook_id": notebook_id,
                    "reason": "large_notebook",
                    "site": "ask_graph",
                })
                response = AskResponse(
                    answer_id="",
                    conclusion="该知识库规模过大,graph 模式的全图漫游在此库上不可用"
                               "(已跳过以避免长时间无响应)。请改用 chunk 或 reasoning "
                               "模式提问;若已构建规模化检索索引(scale index),"
                               "graph 模式的跨文档 PPR 检索仍可正常工作。",
                    conversation_id=conversation_id, retrieval_query=question,
                    llm_mode="deterministic",
                )
                response.mode = "graph"
                response.model_errors = [ModelError(**e) for e in _err_sink]
                raise_if_cancelled(cancel_event)
                response.answer_id = self._save_answer(
                    notebook_id, question, response, conversation_id,
                    user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
                return response

            base_seeds = seed_ids if seed_ids else [h.object_id for h in top_hits[:5]]
            raise_if_cancelled(cancel_event)
            use_seeds = self.candidates.fuse_graph_seeds(
                notebook_id, question, base_seeds, cancel_event)

            G, idx_to_oid, oid_to_idx = self.graph.federated_graph(notebook_id)
            raise_if_cancelled(cancel_event)
            subgraph = multihop_subgraph(
                G, oid_to_idx, idx_to_oid,
                seed_ids=use_seeds,
                # TD2: include "synonym" so multihop walks THROUGH the transit-
                # only cross-doc cluster hubs (their member edges are "synonym").
                # Scoped to this call only — DEFAULT_REASONING_EDGES (a frozenset)
                # is NOT broadened globally. The hub node itself is still filtered
                # from the result/render/verify by build_rx_graph + multihop_subgraph
                # (kind="cluster" pass-through), so the LLM never cites a hub.
                edge_types=DEFAULT_REASONING_EDGES | {"synonym"},
                max_depth=getattr(self.settings, "graph_max_depth", 3),
                max_fan_out=getattr(self.settings, "graph_max_fan_out", 8),
            )
            # Render subgraph into (context_block, id_map) — same k{i} format as
            # _answer_context so grouped marker resolution works unchanged.
            context_block, id_map = render_subgraph_context(subgraph, id_offset=0)
            raise_if_cancelled(cancel_event)

            # Answer-time chain verification: an adversarial LLM check per chain edge.
            # Flagged edges get their confidence demoted to 0.05; the context is then
            # re-rendered so the demotion is visible to the answer LLM. chain_trust is
            # the weakest-link confidence over all edges (1.0 when there are no edges).
            verify_result = {"chain_trust": 1.0, "flagged": [], "edge_results": [],
                             "authority_notes": []}
            verify_client = self.model_clients.chat("graph_chain_verify")
            if getattr(verify_client, "configured", False):
                from app.services.kg.graph_reason import verify_chain_edges
                verify_result = verify_chain_edges(
                    subgraph, verify_client,
                    votes=1, timeout=self.settings.reasoning_timeout_seconds,
                    cancel_event=cancel_event,
                )
                raise_if_cancelled(cancel_event)
                if verify_result["flagged"]:
                    flagged_types = {f["edge_type"] for f in verify_result["flagged"]}
                    for _node, edge, _src in subgraph:
                        if edge and edge.get("edge_type") in flagged_types:
                            edge["confidence"] = 0.05
                    context_block, id_map = render_subgraph_context(subgraph, id_offset=0)

            # 原文增强:子图 KG 节点的源 chunk 整段也喂模型(复用 chunk overlay 的 mix)。
            # 有源 chunk → 走 _answer_mix(KG 段 k1001+ / chunk 段 k1..N)、出 chunk 引用、直接 return;
            # 无源 chunk → 落到下方现状 KG-only 答案,行为不变。
            from app.services.retrieval import est_tokens, truncate_by_tokens
            src_chunks = self.graph.source_chunks(
                notebook_id, [n["object_id"] for n, _e, _s in subgraph])
            if src_chunks:
                mix_kg_block, mix_id_map = render_subgraph_context(
                    subgraph, id_offset=self._MIX_KG_KEY_BASE)
                mix_kg_block = self.evidence_context.truncate_kg_block(
                    mix_kg_block,
                    self.settings.max_entity_tokens + self.settings.max_relation_tokens)
                chunk_budget = max(0, self.settings.max_total_tokens
                                   - est_tokens(mix_kg_block) - self._MIX_PROMPT_BUFFER_TOKENS)
                src_chunks = truncate_by_tokens(src_chunks, lambda c: c.text, chunk_budget)
                # 源 chunk 的 source_title 补全(供引用标签;_kg_source_chunks 留空)
                _sids = list({c.source_id for c in src_chunks})
                _titles = self.evidence_context.citation_titles(_sids) if _sids else {}
                for c in src_chunks:
                    c.source_title = _titles.get(c.source_id, "")
                answer, llm_grounded, anchors = "", False, []
                synth_failed = False
                answer_client = self.model_clients.chat("ask_answer")
                if getattr(answer_client, "configured", False):
                    answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                        lambda: self._answer_mix(
                            question, src_chunks, mix_kg_block, mix_id_map, history,
                            cancel_event=cancel_event, notebook_id=notebook_id,
                            memory_hits=memory_hits, llm_client=answer_client),
                        getattr(answer_client, "model", ""),
                    )
                    synth_failed = not _ok
                citations: List[Citation] = []
                by_id = {c.chunk_id: c for c in src_chunks}
                # src_chunks 来自 _kg_source_chunks(notebook_id, ...):subgraph 节点虽可能
                # 跨 base(_federated_rx_graph),但 element→chunk 反查经 _elem_chunk_map(
                # notebook_id) 单库范围,base 节点的 element 天生查不到 chunk——凡是这里
                # 真返回的 chunk 必属 notebook_id 自己,故只需查这一个 notebook 的 tier。
                src_chunk_tier = self._tier_map_for({notebook_id}).get(notebook_id, "personal")
                # Task 14 codex r4 fix: c.notebook_id 在这条分支里目前恒为 ""
                # (_kg_source_chunks 从不设置 RetrievedChunk.notebook_id,见其上方
                # 注释),但仍与调用方 notebook_id 比较后再传入,而非原样透传——
                # 与 citations_from/ask_chunk 两处 mix 分支/ask_graph 的 PPR 分支
                # 保持同一写法,防止 _kg_source_chunks 未来改动(或换一条同名字段
                # 的产出源)时悄悄退化成第四处同类泄漏。
                def _cite_notebook_id(c) -> str:
                    return c.notebook_id if c.notebook_id != notebook_id else ""
                # Task 12b(引用跳转扩面):graph 模式的源原文引用(mix)同样此前
                # 从未富化过 citation.knowhow——批量查一次,覆盖 src_chunks 里
                # 每个 chunk 的首个 element_id,一次 store 读取(同 ask_chunk
                # 侧口径,运行效率是一等约束)。
                knowhow_refs = self.evidence_context.knowhow_refs_for(
                    c.element_ids[0] for c in src_chunks if c.element_ids)
                for a in anchors:
                    if a.object_type == "chunk" and a.object_id in by_id:
                        c = by_id[a.object_id]
                        eid = c.element_ids[0] if c.element_ids else ""
                        citations.append(Citation(
                            label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                            source_id=c.source_id, element_id=eid,
                            location_label=c.section_path, quoted_span=c.text[:200],
                            tier=src_chunk_tier, notebook_id=_cite_notebook_id(c),
                            knowhow=knowhow_refs.get(eid)))
                citations.extend(self._memory_citations(anchors, memory_hits))
                evidence_level, top_relevance = classify_evidence(
                    list(src_chunks) + list(memory_hits), anchors, llm_grounded,
                    self.settings.evidence_tau_low, self.settings.evidence_tau_high)
                grounded = evidence_level == "grounded"
                if answer:
                    conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                    llm_mode = "grounded" if grounded else "ungrounded"
                elif synth_failed:
                    conclusion = (
                        f"已检索到 {len(src_chunks)} 段源原文,但本次答案合成未产出内容"
                        "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。")
                    llm_mode = "synthesis_failed"
                else:
                    conclusion = f"Graph retrieved {len(src_chunks)} source passage(s) for this question."
                    llm_mode = "deterministic"
                resp = AskResponse(
                    answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                    evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                    citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                    retrieval_query=question, top_relevance=top_relevance,
                    reasoning_trace=[TraceStep(step_type="graph_src_chunks",
                        summary=f"BFS 子图 + {len(src_chunks)} 段源原文",
                        detail={"chunks": len(src_chunks),
                                "sources": len({c.source_id for c in src_chunks})})])
                resp.mode = "graph"
                resp.model_errors = [ModelError(**e) for e in _err_sink]
                resp.answer_id = self._save_answer(
                    notebook_id, question, resp, conversation_id,
                    user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
                return resp

            # Synthesise the answer through the existing LLM + grounding path.
            context_block, id_map = self._append_memory_context(
                context_block, id_map, memory_hits
            )
            context_block = self._refine_context(
                question, context_block, self.model_clients.chat("evidence_refine"), cancel_event)
            answer, llm_grounded, anchors = "", False, []
            synth_failed = False
            raise_if_cancelled(cancel_event)
            answer_client = self.model_clients.chat("ask_answer")
            if getattr(answer_client, "configured", False) and id_map:
                def _synth_kg():
                    llm_client = answer_client
                    raw = llm_client.chat_json(
                        [{"role": "user",
                          "content": answer_prompt(question, context_block, history)}],
                        ANSWER_SCHEMA_HINT,
                        cancel_event=cancel_event,
                        **cap_kwargs(llm_client, "answer_max_tokens"),
                    )
                    raise_if_cancelled(cancel_event)
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        return "", False, []
                    _ans = str(data.get("answer", "")).strip()
                    _g = bool(data.get("grounded", False))
                    _anc = self._parse_answer_anchors(_ans, id_map)
                    # Scrub citation-shaped tokens that did NOT bind to a real
                    # id_map entry (out-of-map ids like [k99], malformed [ k1]).
                    # Unlike the fast path — whose id_map IS top_hits, so the LLM
                    # rarely invents ids — graph mode shows a wider subgraph and
                    # the answer LLM occasionally emits markers the strict anchor
                    # parser can't bind; left in place they read as fabricated
                    # citations. Strip them so only resolved [k] markers ship.
                    return _strip_unbound_markers(_ans, {a.key for a in _anc}), _g, _anc
                answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                    _synth_kg,
                    getattr(answer_client, "model", ""),
                )
                synth_failed = not _ok

            # classify_evidence keys "grounded" off the relevance of the CITED hit.
            # In the fast path id_map IS built from top_hits, so every anchor is a
            # scored hit. In graph mode the cited node can be a multi-hop NEIGHBOUR
            # that is in id_map but NOT in top_hits → its relevance would read 0 and
            # the answer would be demoted to "overview" even though it cites a real,
            # chain-connected node (the q17/q18 "overview while citing specifics"
            # contradiction). Mirror the fast-path invariant: give each cited
            # neighbour a relevance inherited from the strongest seed, discounted by
            # chain_trust (the verifier's weakest-link confidence), so a trusted
            # chain can reach "grounded" while a flagged/weak one still falls back.
            raise_if_cancelled(cancel_event)
            seed_rel = max((h.relevance for h in top_hits), default=0.0)
            neighbour_rel = seed_rel * float(verify_result.get("chain_trust", 1.0))
            hits_for_classify = _graph_classification_hits(
                top_hits=top_hits,
                memory_hits=memory_hits,
                anchors=anchors,
                neighbour_relevance=neighbour_rel,
                notebook_id=notebook_id,
            )

            evidence_level, top_relevance = classify_evidence(
                hits_for_classify, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
            # Report the genuine seed relevance, not the synthetic neighbour value.
            top_relevance = max(
                (h.relevance for h in [*top_hits, *memory_hits]),
                default=top_relevance,
            )
            grounded = evidence_level == "grounded"
            if answer:
                conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                llm_mode = "grounded" if grounded else "ungrounded"
            elif synth_failed:
                conclusion = (
                    f"已检索到 {len(subgraph)} 个相关节点,但本次答案合成未产出内容"
                    "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。")
                llm_mode = "synthesis_failed"
            else:
                conclusion = (
                    f"Graph traversal found {len(subgraph)} node(s) across "
                    f"{len(use_seeds)} seed(s).")
                llm_mode = "deterministic"

            graph_trace = [TraceStep(
                step_type="graph_verify",
                summary=(f"chain_trust={verify_result['chain_trust']:.2f}; "
                         f"{len(verify_result['flagged'])} edge(s) flagged; "
                         f"{len(subgraph)} node(s) traversed"),
                detail={**verify_result,
                        "authority_notes": verify_result.get("authority_notes", [])},
            )]

            response = AskResponse(
                answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                citations=self._memory_citations(anchors, memory_hits), llm_mode=llm_mode,
                conversation_id=conversation_id, retrieval_query=question,
                top_relevance=top_relevance, reasoning_trace=graph_trace,
            )
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
        response.mode = "graph"
        response.model_errors = [ModelError(**e) for e in _err_sink]
        raise_if_cancelled(cancel_event)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id,
            user_id=user_id, job_id=job_id, asked_at=payload.asked_at)
        return response
