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
        memory_retriever=None,
        current_user_id: Callable[[], str] = lambda: "",
        cancellations=None,
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
        self.memory_retriever = memory_retriever
        self.current_user_id = current_user_id
        self.cancellations = cancellations

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

    def _tier_map_for(self, notebook_ids: Iterable[str]) -> Dict[str, str]:
        return self.evidence_context.tier_map(list(notebook_ids))

    def _chunk_answer_context(self, chunks, budget_chars: "int | None" = None,
                              notebook_id: str = "") -> tuple:
        return self.evidence_context.chunk_context(
            chunks, notebook_id=notebook_id, budget_chars=budget_chars)

    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge],
                        id_offset: int = 0) -> tuple:
        return self.evidence_context.knowledge_context(
            notebook_id, top_hits, id_offset=id_offset)

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
    ) -> str:
        # 所有 ask handler 的唯一收口:在持久化/返回前给 response 打大库无索引提示位。
        # 覆盖 chunk/reasoning/graph 三 handler 的全部 return 路径(含早退),避免逐 handler
        # 多 return 点漏赋值。小库/已索引 → False(默认),无副作用。
        response.index_required = self._needs_index(notebook_id)
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
            context_block = ("Focused relevant evidence (for this question):\n"
                             + "\n".join(f"- {x}" for x in rel[:12])
                             + "\n\n" + context_block)
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
    ):
        """Synthesise the reasoning-mode answer. When PPR chunks are present they
        become first-class [k]-citable evidence: chunk segment k1..N + KG reasoning
        chain segment k1001+ (mirrors _answer_mix's keying), with final synthesis
        still handled by ``ask_answer``. Otherwise KG-only (legacy).
        Direct ``SourceElement`` passages use the isolated k4001+ namespace and
        are first-class citations rather than unbound prompt decoration. Context refinement uses
        ``evidence_refine`` while final synthesis uses ``ask_answer``. Returns
        (answer, llm_grounded, anchors)."""
        raise_if_cancelled(cancel_event)
        chunks = chunks or []
        chains = chains or []
        if chunks:
            # 按相关度降序(_chunk_answer_context 自带 char 预算,保留最相关;跨 PPR run
            # 的归一分仅大致可比,只影响预算边缘取舍,不破坏 [0,1]);chunk 段 k1..N + KG 段
            # k1001+,合并 id_map,两段都可 [k] 引用。无需 _answer_mix 的 base-1 截断:chunk
            # 数 ≤ ppr_top_chunks×(1 seed + _MAX_PPR_RETRIEVES) ≪ _MIX_KG_KEY_BASE(1000)。
            ordered = sorted(chunks, key=lambda c: (-c.relevance, c.chunk_id))
            chunk_block, chunk_id_map = self._chunk_answer_context(
                ordered, notebook_id=notebook_id)
            kg_block, kg_id_map = self._answer_context(
                notebook_id, top_hits, id_offset=self._MIX_KG_KEY_BASE)
            if kg_block and kg_block != "(none)":
                context_block = f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
            else:
                context_block = chunk_block
            id_map = {**chunk_id_map, **kg_id_map}
        else:
            context_block, id_map = self._answer_context(notebook_id, top_hits)
        context_block, id_map = self._append_memory_context(
            context_block, id_map, memory_hits or []
        )
        if chains:
            from app.services.kg.follow_chain import render_follow_chain_context
            chain_block, chain_id_map = render_follow_chain_context(
                chains, id_offset=2000, active_notebook_id=notebook_id)
            if chain_block and chain_block != "(none)":
                context_block = f"{context_block}\n\n{chain_block}"
                id_map = {**id_map, **chain_id_map}
        if elements:
            element_block, element_id_map = self.evidence_context.element_context(
                elements[:6], notebook_id=notebook_id,
                id_offset=self._ELEMENT_KEY_BASE,
            )
            if element_block and element_block != "(none)":
                context_block = f"{context_block}\n\n[Direct source elements]\n{element_block}"
                id_map = {**id_map, **element_id_map}
        answer_client = answer_client or self.model_clients.chat("ask_answer")
        refine_client = self.model_clients.chat("evidence_refine")
        context_block = self._refine_context(
            question, context_block, refine_client, cancel_event)
        raw = answer_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
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
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def _unconfigured_model_response(self, notebook_id: str, question: str,
                                     conversation_id: str, mode: str,
                                     *, user_id: str, job_id: str = "",
                                     intent: QueryIntentContract | None = None,
                                     retrieval_query: str = "") -> AskResponse:
        """系统模型已启用但漏绑问答工作负载时的统一短路响应。"""
        msg = "系统未配置当前问答所需的模型服务，请联系维护人员"
        response = AskResponse(
            answer_id="", conclusion=msg, conversation_id=conversation_id,
            retrieval_query=retrieval_query or question, llm_mode="deterministic",
            intent=intent)
        response.mode = mode
        response.model_errors = [
            ModelError(stage="answer", model="", message="missing_config")
        ]
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id,
            user_id=user_id, job_id=job_id)
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
        import time
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
                selected = truncate_by_tokens(ranked, lambda c: c.text, chunk_budget)
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
                selected, _counts = quota_fuse(collected, per_query, plan.fuse_k,
                                               relevance=lambda c: c.relevance)
                ask_stage("retrieve_fuse", _t, recall=len(collected), selected=len(selected))
            else:
                scored, ids, mat = self.candidates.retrieve_chunk_candidates(
                    notebook_id, sub_queries[0])
                # ∪ bilingual-keyword chunk hits (dedup by chunk_id; keep existing on collision)
                scored = self.candidates.merge_chunk_candidates(scored, kw_hits)
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
            user_id=user_id, job_id=job_id)
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
                # Preserve the normal topic budget and add one whole-question
                # guard seed so decomposition cannot crowd out the user topic.
                max_queries=self.settings.reasoning_max_subqueries + 1,
                objective_is_authoritative=auto_confirmed_clear_intent,
            )
            if payload.intent is not None else []
        )
        memory_hits = self._memory_hits(
            user_id, notebook_id, research_question
        )

        if self._primary_llm_unconfigured():
            return self._unconfigured_model_response(
                notebook_id, question, conversation_id, "reasoning",
                user_id=user_id, job_id=job_id, intent=intent_contract,
                retrieval_query=research_question)

        if not memory_hits and not (
                self.candidates.has_kg(notebook_id)
                or self.candidates.any_base_has_kg(notebook_id)):
            response = AskResponse(
                answer_id="",
                conclusion="本笔记本尚未构建知识图谱,也没有已建图的参考库;"
                           "请先点『构建知识图谱』,或为本笔记本挂载一个已建图的"
                           "公共知识库。",
                conversation_id=conversation_id, retrieval_query=research_question,
                llm_mode="deterministic", kg_required=True,
                intent=intent_contract)
            response.mode = "reasoning"
            raise_if_cancelled(cancel_event)
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id,
                user_id=user_id, job_id=job_id)
            return response

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        # P1-A(本轮 scope):只挂 reasoning 模式。ask_graph/ask_chunk 同样受益,
        # 但等价性回放验证只覆盖了 reasoning——留作后续 fast-follow。
        _emb_token = _ASK_EMBED_CACHE.set({})
        try:
            intent_step = TraceStep(
                step_type="intent",
                summary="已按确认后的问题理解开始检索",
                detail={
                    "resolved_question": intent_contract.resolved_question,
                    "entities": intent_contract.entities,
                    "constraints": intent_contract.constraints,
                    "excluded_topics": intent_contract.excluded_topics,
                    "assumptions": intent_contract.assumptions,
                    "expected_output": intent_contract.expected_output,
                    "mandatory_topics": [
                        topic.question for topic in intent_contract.mandatory_topics
                    ],
                },
            )

            def checked_trace(step):
                raise_if_cancelled(cancel_event)
                if on_trace:
                    on_trace(step)

            try:
                checked_trace(intent_step)
                # 端口化构造(与冻结的 from_repository 工厂逐字段同源):检索/模型/
                # 社区端口直通,communities 逐次新建 —— sibling_min_bridge 调用时读。
                result = ReasoningRetriever(
                    retrieval=self.retrieval,
                    model_clients=self.model_clients,
                    communities=self.communities(),
                    settings=self.settings,
                    cancel_event=cancel_event,
                ).run(
                    notebook_id,
                    research_question,
                    history,
                    on_step=checked_trace,
                    intent_queries=intent_queries,
                )
                top_hits, elements, trace, chunks, chains = (
                    result.top_hits, result.elements, result.trace, result.chunks,
                    result.chains)
                trace = [intent_step, *trace]
            except AskCancelled:
                raise
            except Exception:
                top_hits, elements, trace, chunks, chains = (
                    [], [], [intent_step], [], []
                )

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
            raise_if_cancelled(cancel_event)
            answer_client = self.model_clients.chat("ask_answer")
            if answer_client.configured and (
                    top_hits or elements or chunks or chains or memory_hits):
                # 空 content 有界重试 + 诚实降级 + 可观测,统一走 _answer_with_retry(见其 docstring)。
                answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                    lambda: self._answer_reasoning(
                        notebook_id, research_question, top_hits, elements, history,
                        cancel_event=cancel_event, chunks=chunks, chains=chains,
                        memory_hits=memory_hits, answer_client=answer_client),
                    getattr(answer_client, "model", ""),
                    service="ask_answer",
                )
                synth_failed = not _ok

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
            element_evidence = [SimpleNamespace(
                object_id=item.element_id, relevance=float(item.score or 0.0),
            ) for item in elements]
            evidence_pool = (
                list(top_hits) + list(chunks) + chain_evidence
                + element_evidence + list(memory_hits)
            )
            evidence_level, top_relevance = classify_evidence(
                evidence_pool, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
            grounded = evidence_level == "grounded"

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

            response = AskResponse(
                answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                evidence_level=evidence_level, anchors=anchors,
                related_knowledge=related_knowledge, citations=citations,
                llm_mode=llm_mode, conversation_id=conversation_id,
                retrieval_query=research_question, top_relevance=top_relevance,
                reasoning_trace=trace or None,
                intent=intent_contract,
            )
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
            _ASK_EMBED_CACHE.reset(_emb_token)
        response.mode = "reasoning"
        response.model_errors = [ModelError(**e) for e in _err_sink]
        raise_if_cancelled(cancel_event)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id,
            user_id=user_id, job_id=job_id)
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
                user_id=user_id, job_id=job_id)

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
                user_id=user_id, job_id=job_id)
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
                    user_id=user_id, job_id=job_id)
                return response

            # HippoRAG 式 PPR 跨文档检索(opt-in)。命中即走 chunk 答案路径:PPR 把
            # 别的文档相关 chunk 也召回,_answer_chunks 出 chunk 引用(跨多篇)。
            if self.settings.graph_ppr_enabled:
                raise_if_cancelled(cancel_event)
                ppr_chunks = self.retrieval.ppr_retrieve(notebook_id, question)
                raise_if_cancelled(cancel_event)
                if ppr_chunks:
                    from app.services.retrieval import RetrievedChunk
                    reports = self.community_reports(
                        notebook_id)[: self.settings.ppr_community_context_top_n]
                    community_chunks = [RetrievedChunk(
                        chunk_id=f"community:{i}", source_id="",
                        source_title="Knowledge base theme", section_path=r["title"],
                        text=f"{r['title']}. {r['summary']}", element_ids=[], relevance=1.0)
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
                        user_id=user_id, job_id=job_id)
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
                    user_id=user_id, job_id=job_id)
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
                    user_id=user_id, job_id=job_id)
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
            user_id=user_id, job_id=job_id)
        return response
