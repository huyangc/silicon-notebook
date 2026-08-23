"""Stable trace-step narrowing shared by repositories and services.

Moved out of ``app.repositories.ports`` (2026-08-23): ``project_run_step`` is
consumed both by repository adapters (``ask_state_store.py``, SQLite AND
PostgreSQL — narrowing a raw persisted trace row before it ever leaves the
store) and by ``app.services.retrieval_experience_projection`` (folding the
narrowed row into the deployment-global experience library). Repositories may
not import services (see ``scripts/architecture_boundary_baseline.json`` ::
core_models_service_imports and the zero-slack architecture guard), so this
whole self-contained narrowing cluster — not just ``project_run_step`` itself
— lives here in ``app.domain``, the one layer both repositories and services
may import forward from. Nothing here changed: every function body, docstring
and constant is carried over verbatim; only the module changed.
"""
from __future__ import annotations

import json
from typing import Mapping

from app.models.ask import TRACE_ANCHOR_EVIDENCE_IDS_MAX, TRACE_RESULT_IDS_MAX


#: Per-item clip for the two free-text fields that survive the projection (the
#: member's own question, and a step's human summary). They are the member's
#: own words about their own run, but they still ride into a prompt on a
#: budget, so one pathological 5 000-character question cannot spend the whole
#: usage section.
AGENT_PROFILE_TRACE_TEXT_MAX_CHARS = 120

#: The ONLY ``detail`` keys the projection keeps, and it keeps at most one of
#: them, as an int. An allowlist rather than "copy ``detail`` through": trace
#: details are free-form dicts that some steps fill with error strings
#: (``str(exc)[:120]``) and model-written reflection text. None of that is
#: needed to say "this retrieval came back empty", and a pass-through would
#: quietly widen what the overlay prompt sees every time a new step type is
#: added anywhere in the retrieval stack.
#:
#: Grep-verified against every real ``TraceStep(detail=...)`` call site in
#: ``reasoning_retrieval.py`` / ``ask_service.py`` / ``structured_retrieval.py``
#: (2026-08-18): ``"results"``/``"rows"``/``"hits"`` are never emitted as a
#: trace-step detail key anywhere in the repo (``"rows"``/``"hits"`` only show
#: up in unrelated progress dicts and a report-profile helper that never
#: touches ``TraceStep``), so they are dropped rather than kept "just in
#: case". ``"found"`` (``expand``/``exact_lookup``) and ``"returned_total"``
#: (the collection-enumeration ``enumerate`` step's running total across the
#: whole chain) are real emitters and were missing entirely — without them
#: every ``expand``/``exact_lookup``/``enumerate`` step projected ``count=None``
#: and could never register as a zero-hit signal for ``usage_gaps``.
_TRACE_COUNT_KEYS = ("count", "found", "returned_total", "new", "citations")

#: ``step_type == "retrieve"`` is special-cased in ``project_trace_step``
#: below: its ``add_subquery``/confirmed-direction emit sites only ever carry
#: ``{"query", "new"}`` (see ``reasoning_retrieval.py``'s two "补充..." record
#: sites), and ``new == 0`` there means "nothing NEW beyond what was already
#: collected" — a query that reproduces existing candidates, not one that
#: found nothing. The retrieval that DID genuinely return nothing is the
#: first, unconditional "初检索" step, which uses ``"count"`` instead. Reading
#: ``"new"`` for zero-hit purposes would flag every de-duplicating follow-up
#: query as an empty search. Only the initial-retrieval ``"count"`` key is
#: trustworthy as a zero-hit signal for this step type, so retrieve steps
#: read ONLY it; other step types (``expand``/``follow_chain``/``exact_lookup``/
#: ``enumerate``) still get the full allowlist above, and ``"new"`` remains
#: available there for plain display purposes (it is never their zero-hit key).
_RETRIEVE_COUNT_KEYS = ("count",)


def _clip_trace_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > AGENT_PROFILE_TRACE_TEXT_MAX_CHARS:
        return text[: AGENT_PROFILE_TRACE_TEXT_MAX_CHARS - 1] + "…"
    return text


def _step_detail_mapping(step: object) -> Mapping | None:
    """Best-effort ``detail`` mapping for a possibly-JSON-string step row.

    Agentic Memory P4 (T2): a third, self-contained copy of the JSON-decode-
    then-extract-``detail`` pattern ``project_run_step`` already repeats
    inline inside its synthesis/answer and ``intent`` branches below — pulled
    out here rather than duplicated a third time inline, since the new
    ``result_ids`` branch needs the exact same mapping a third time and
    reusing this one helper keeps that branch a two-line check instead of
    another six-line try/except block.
    """
    raw = step
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, Mapping):
        return None
    detail = raw.get("detail")
    return detail if isinstance(detail, Mapping) else None


def _bounded_id_list(raw: object, cap: int) -> list[str]:
    """Coerce ``raw`` into a bounded list of non-empty string ids.

    Agentic Memory P4 (T2): the read-side twin of the write-side truncation
    already applied at every ``TraceStep(detail=...)`` emit site — the write
    side already caps to the same constants, so this is a defense-in-depth
    re-application, not the primary enforcement point. Drops anything that is
    not a non-empty ``str`` (a stray ``int``, ``None``, ``bool``, or an empty
    string a malformed row might carry) rather than trying to repair it, then
    truncates to ``cap`` entries. Shared by both ``result_ids`` (non-synthesis
    steps) and ``anchor_evidence_ids`` (synthesis/answer steps) below.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    result: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            result.append(item)
        if len(result) >= cap:
            break
    return result


def project_trace_step(step: object) -> dict | None:
    """Narrow ONE persisted trace step to the fields the overlay chain may see.

    ⚠ SINGLE SOURCE OF TRUTH for both backends, for the same reason as
    ``append_profile_history`` above and with a sharper edge: SQLite stores
    ``step_json`` as TEXT and PostgreSQL as ``jsonb``, so the two stores hand
    this function different Python types for the same row — and a per-backend
    copy of a *narrowing* rule is a copy that can silently widen on one side
    only. What gets into an understanding block would then depend on which
    database the deployment runs.

    Returns ``None`` for anything that is not a step object, so a corrupt row
    is skipped rather than failing the read (the same tolerance ``read_trace``
    has had since the trace sub-table existed).

    ⚠ Agentic Memory P4 (T2): the ``result_ids`` / ``anchor_evidence_ids``
    step→anchor attribution raw material ``project_run_step`` below reads is
    deliberately NOT projected here. This function feeds the agent-profile
    overlay chain (readable only by the member whose own run produced it, via
    a rendered PROSE block), which has no use for a bare id list; growing this
    function's return shape widens what every caller of it — including ones
    added later, that may not share ``project_run_step``'s narrower privacy
    argument — sees, for no benefit.
    """
    if isinstance(step, (str, bytes, bytearray)):
        try:
            step = json.loads(step)
        except (TypeError, ValueError):
            return None
    if not isinstance(step, Mapping):
        return None
    detail = step.get("detail")
    step_type = str(step.get("step_type") or "")
    count: int | None = None
    if isinstance(detail, Mapping):
        # "retrieve" reads a narrower key set than every other step type —
        # see the comment on ``_RETRIEVE_COUNT_KEYS`` above.
        candidate_keys = (
            _RETRIEVE_COUNT_KEYS if step_type == "retrieve" else _TRACE_COUNT_KEYS
        )
        for key in candidate_keys:
            raw = detail.get(key)
            if isinstance(raw, bool):
                continue
            if isinstance(raw, int):
                count = int(raw)
                break
    duration = step.get("duration_ms")
    return {
        "step_type": step_type,
        "summary": _clip_trace_text(step.get("summary")),
        "duration_ms": int(duration) if isinstance(duration, int) and not isinstance(duration, bool) else None,
        "count": count,
    }


#: Agentic Memory P2 (T5): the CLOSED vocabularies the global experience
#: library's "situation" side may take values from. They live here, beside
#: ``project_run_step`` which enforces them, rather than in the service module
#: that consumes them, for the same reason ``project_trace_step`` lives here:
#: this is the one narrowing both backends share, and a second spelling of a
#: narrowing rule is a spelling that can silently widen on one side only.
#: ``retrieval_experience_projection.py`` imports these tuples rather than
#: restating them.
#:
#: Anything outside a vocabulary collapses to ``SITUATION_UNKNOWN`` — never to
#: the raw value. That is what makes "no free text reaches an experience entry"
#: a property of this function rather than of the caller's diligence: a model
#: that writes an unexpected ``result_scope`` into its intent contract cannot
#: get that string past this point.
SITUATION_UNKNOWN = "unknown"
SITUATION_ASK_MODES = ("chunk", "reasoning", "graph")
SITUATION_RESULT_SCOPES = ("ranked", "complete", "aggregate", "hybrid")
SITUATION_RETRIEVAL_EFFORTS = (
    "overview", "standard", "deep", "thorough", "exhaustive",
)


def _closed_value(raw: object, vocabulary: tuple[str, ...]) -> str:
    text = str(raw or "").strip().lower()
    return text if text in vocabulary else SITUATION_UNKNOWN


def _list_len(raw: object) -> int:
    return len(raw) if isinstance(raw, (list, tuple)) else 0


def project_run_step(step: object) -> dict | None:
    """Narrow ONE persisted trace step for the GLOBAL experience library.

    Agentic Memory P2 (T5). Strictly narrower than ``project_trace_step``,
    which it delegates to, and the difference is the entire privacy argument of
    the feature: ``summary`` is DROPPED. That field is a human sentence written
    per step, and several emitters interpolate model text or an error string
    into it. It is fine in the agent-profile overlay sample — that block is
    readable only by the member whose run produced it — and it is unacceptable
    here, where the resulting entry is visible to every user of the deployment.

    What survives is the action type, one count, one duration, and (Agentic
    Memory P4, T2) a bounded list of opaque result/anchor ids: enough to say
    "this action ran and came back empty", which is exactly the half of the
    outcome signal P2 keeps at step granularity, PLUS enough for a later
    consumer to compute step→anchor attribution — the "not recoverable" gap
    ``RunObservation``'s docstring registers as a later phase.

    ⚠ The id lists themselves do NOT go into ``RunObservation`` — that
    dataclass stays ``int`` / ``bool`` / closed-``Literal``-only, per the
    privacy guard (``test_retrieval_experience_privacy_guard.py``). The
    intended shape for a later phase is: the CALLER (``project_run`` in
    ``retrieval_experience_projection.py``) intersects one step's
    ``result_ids`` against the run's own ``anchor_evidence_ids`` LOCALLY,
    inside its own loop, and folds the result down to a count (an
    ``attributable: bool`` / ``anchored_hits: int`` pair) before it ever
    touches a dataclass field — the raw ids themselves live only in this
    function's return value and that loop's local variables, never in a
    field that a global, all-users-readable table stores. This function's
    job stops at making the raw material available; it neither computes nor
    persists the intersection itself.

    The ``intent`` step additionally contributes the run's SITUATION, and this
    is the only place any of it is read. Every value is either a ``bool``, an
    ``int`` count, or a member of a closed vocabulary above — the step's
    ``resolved_question``, its per-topic questions and its ``assumptions`` /
    ``expected_output`` prose are not read at all, and the two enumerated
    fields cannot pass an unexpected string through (see ``_closed_value``).
    Entity and topic LISTS contribute their LENGTH only; the service layer
    buckets those into coarse bands, and their contents never leave this
    function.
    """
    base = project_trace_step(step)
    if base is None:
        return None
    projected = {
        "step_type": base["step_type"],
        "duration_ms": base["duration_ms"],
        "count": base["count"],
    }
    if projected["step_type"] not in ("synthesis", "answer"):
        # Agentic Memory P4 (T2): pass ``result_ids`` through by KEY
        # PRESENCE, not by step type — this file does not import the service
        # layer's ``RETRIEVAL_ACTIONS`` vocabulary (that would be a layering
        # inversion: ports is beneath services), so it cannot tell "this step
        # type is a retrieval action" from the step alone. Key presence is
        # the write side's own signal: every write site that dispatched I/O
        # writes "result_ids" unconditionally (including an empty list on a
        # genuine zero-hit result), while a "skip" branch and every trace row
        # persisted before this phase never has the key at all. Projecting
        # only when the key exists is what lets a later consumer tell "old
        # trace, or a step type that never emits this" apart from "ran, found
        # nothing" — writing an empty list either way would erase that
        # distinction.
        step_detail = _step_detail_mapping(step)
        if isinstance(step_detail, Mapping) and "result_ids" in step_detail:
            projected["result_ids"] = _bounded_id_list(
                step_detail.get("result_ids"), TRACE_RESULT_IDS_MAX
            )
            # 修复轮 spec②: 稀疏截断标——只在写侧真的截过 result_ids 时才
            # 出现(镜像 anchor_ids_truncated 的"detail 逐键不变"冻结基线
            # 口径)。下游 project_run 据它把这一次调用的 attribution 单独
            # 判 poison,而不是把截掉的尾巴悄悄当成"就这么多"。
            if step_detail.get("result_ids_truncated"):
                projected["result_ids_truncated"] = True
    if projected["step_type"] in ("synthesis", "answer"):
        # codex #524 R9 P2:接地信号只认 ``anchors``(模型真正绑上的 [k])。
        # ``citations`` 是「每条检索证据一张卡」的兜底列表,零绑定的回答里它
        # 照样非零——经通用键序读它会把不接地的回答学成成功信号。旧轨迹两个
        # 键都发(P1 起同批落),缺 anchors 的按 0 处理而不是回退 citations。
        raw_step = step
        if isinstance(raw_step, (str, bytes, bytearray)):
            try:
                raw_step = json.loads(raw_step)
            except (TypeError, ValueError):
                raw_step = None
        anchors = None
        anchor_ids: list[str] = []
        anchor_ids_truncated = False
        # 修复轮 Q-P1-1: 不止一种 step_type=="synthesis"/"answer" 的行——
        # 逐节撰写进度步、按枚举回答分支的 "answer" 步、reasoning_retrieval.py
        # 那条候选池汇总的 "answer" 步都同名但从不带 anchor_evidence_ids;
        # 只有 ask_service.py 那唯一一处最终答案写点带这个键。
        # ``step_limit`` 还会把一个 run 的 trace 行按 seq 截尾(见
        # ``recent_completed_ask_runs``),真正带锚点的那条"synthesis"步完全
        # 可能被切掉而只留下前面那条不带锚点的"answer"候选步——这不是「一个
        # 空锚点集」,是「这条 run 没有可用的锚点信号」,两者不能用同一个值
        # 表达。
        has_anchor_key = False
        if isinstance(raw_step, Mapping):
            detail = raw_step.get("detail")
            if isinstance(detail, Mapping):
                candidate = detail.get("anchors")
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    anchors = candidate
                has_anchor_key = "anchor_evidence_ids" in detail
                if has_anchor_key:
                    anchor_ids = _bounded_id_list(
                        detail.get("anchor_evidence_ids"),
                        TRACE_ANCHOR_EVIDENCE_IDS_MAX,
                    )
                    anchor_ids_truncated = bool(
                        detail.get("anchor_evidence_ids_truncated")
                    )
        projected["count"] = anchors if anchors is not None else 0
        if has_anchor_key:
            # 按键存在投影,镜像上面 result_ids 的规则——没有这个键的行(包括
            # 上面枚举的那几种同名但不携带锚点的行)整段不投影这个字段,而不是
            # 投影一个看起来"零锚点"的空列表。project_run 的 pass 1 据此把
            # "这条 run 没带锚点信号"与"这条 run 确认锚点为空"区分开。
            projected["anchor_evidence_ids"] = anchor_ids
            if anchor_ids_truncated:
                # Sparse — only present on the (unexpected) day the write-side
                # cap actually bound. Mirrors the "detail 逐键不变" frozen-
                # baseline convention every other conditional trace-detail key
                # follows.
                projected["anchor_ids_truncated"] = True
        return projected
    if projected["step_type"] != "intent":
        return projected
    if isinstance(step, (str, bytes, bytearray)):
        try:
            step = json.loads(step)
        except (TypeError, ValueError):
            return projected
    if not isinstance(step, Mapping):
        return projected
    detail = step.get("detail")
    if not isinstance(detail, Mapping):
        return projected
    projected["situation"] = {
        "result_scope": _closed_value(
            detail.get("result_scope"), SITUATION_RESULT_SCOPES
        ),
        "retrieval_effort": _closed_value(
            detail.get("retrieval_effort"), SITUATION_RETRIEVAL_EFFORTS
        ),
        "completeness_required": bool(detail.get("completeness_required")),
        "entity_count": _list_len(detail.get("entities")),
        "topic_count": _list_len(detail.get("mandatory_topics")),
        "has_constraints": _list_len(detail.get("constraints")) > 0,
        "has_exclusions": _list_len(detail.get("excluded_topics")) > 0,
    }
    return projected
