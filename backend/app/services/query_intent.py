"""Corpus-blind query-intent contracts shared by reports and reasoning Ask.

This module deliberately has no repository/retrieval dependency. It may use a
model to understand the user's wording, but it must finish before corpus
candidates are allowed to influence the requested topic.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from app.core.model_values import as_text
from app.core.ask_retrieval_policy import (
    RESOLVED_QUESTION_MAX_CHARS,
    AMBIGUITY_QUESTION_MAX_CHARS,
    AMBIGUITY_ROWS_MAX,
    RESULT_SCOPES,
)
from app.services.cancellation import AskCancelled, CancelEvent


_UNRESOLVED_REFERENCE = re.compile(
    r"(?:这个|那个|这些|那些|上述|前述|刚才|之前提到|该问题|该方案|它们?)"
    r"|\b(?:this|that|these|those|it|they|above|previous|former|latter)\b",
    re.IGNORECASE,
)
# The active retrieval container is supplied by the request, not discovered in
# corpus text. Mask only that deictic noun phrase; another "it/this" in the same
# question must still pass the ordinary referent check.
_CURRENT_CONTAINER_REFERENCE = re.compile(
    r"这个\s*(?:笔记本|知识库|参考库|notebook|库)"
    r"(?=里|中|内|的|有|包含|包括|收录|[，。！？?\s]|$)"
    r"|\bthis\s+(?:notebook|library|knowledge\s+base)\b",
    re.IGNORECASE,
)
_GENERIC_REQUEST = re.compile(
    r"^(?:(?:帮我)?(?:分析|研究|介绍|讲讲|说说|看看|总结|比较|对比|优化)(?:一下|下)?"
    r"(?:这个|那个|它|问题|方案|内容|东西)?|"
    r"(?:please\s+)?(?:analy[sz]e|review|compare|explain|optimi[sz]e)\s*"
    r"(?:this|that|it|them)?)\s*[。.!！?？]*$",
    re.IGNORECASE,
)
_INTENT_TYPES = {"explain", "compare", "diagnose", "design", "review", "other"}
_COMPLETE_REQUEST = re.compile(
    r"(?:全部|所有(?!权|制)|全量|全套|"
    r"完整(?:地)?(?:列出|罗列|枚举|读取|覆盖)|完整(?:的)?(?:清单|列表|集合)|逐一|逐项|逐篇|"
    r"每(?:一)?种|每一项|每一个|列全|无遗漏|穷举)"
    r"|\b(?:all|every|each|entire|exhaustive(?:ly)?|complete\s+list|"
    r"without\s+omission)\b",
    re.IGNORECASE,
)
_COMPLETE_NEGATION = re.compile(
    r"(?:不(?:用|必|需要|要求|是|要|需)?|无需|并非(?:必须|需要)?|非)"
    r"(?:列出|包含|覆盖|枚举)?(?:全部|所有|完整|逐一|逐项|逐篇)"
    r"|\b(?:not|do\s+not|don't|no\s+need\s+to)\s+"
    r"(?:need(?:\s+to)?\s+|list\s+)?(?:all|every|each)\b",
    re.IGNORECASE,
)
_RANKED_SCOPE_ANSWER = re.compile(
    r"(?:最相关|最重要|优先级最高|"
    r"(?:只|仅)(?:要|需|给|列出|返回)?[^，。；?!！？]{0,16}(?:相关|前\s*\d+)|"
    r"前\s*\d+\s*(?:个|项|条|种)?|\btop\s*[-:]?\s*\d+\b|"
    r"(?:不(?:用|需要|必)|无需|并非(?:必须|需要)?)[^，。；?!！？]{0,12}(?:全部|所有))",
    re.IGNORECASE,
)
_RANKED_POSITIVE_NEGATION = re.compile(
    r"(?:不要|不是|并非|不(?:取|选|看)|别)\s*$",
    re.IGNORECASE,
)
_AGGREGATE_REQUEST = re.compile(
    r"(?:一共(?:有)?多少(?:种|个|项|条|行|类|张)|有多少(?:种|个|项|条|行|类|张)|"
    r"多少(?:种|个|项|条|行|类|张)|总数|"
    r"数量(?:是|为|有)?多少|"
    r"统计(?:一下|下)?[^，。；?!！？]{0,40}(?:总数|数量|有多少|多少)|"
    r"(?:按[^，。；?!！？]{1,40})?分组(?:统计|汇总)|分类汇总)"
    r"|\b(?:how\s+many|count|aggregate|group\s+by|in\s+total|total\s+(?:number|count))\b",
    re.IGNORECASE,
)
_AGGREGATE_PREFIX_NEGATION = re.compile(
    r"(?:不要|无需|并非|不是|不(?:需要|用|必|要))\s*$",
    re.IGNORECASE,
)
_AGGREGATE_SUFFIX_NEGATION = re.compile(
    r"^\s*(?:不要|无需|不(?:用|需要|重要|必|要|统计)|"
    r"并非(?:重点|所需)|不是(?:重点|所需))",
    re.IGNORECASE,
)
_ANALYSIS_REQUEST = re.compile(
    r"(?:并|以及|同时).*(?:分析|比较|对比|优缺点|差异|趋势|原因|建议)"
    r"|(?:分析|比较|对比|优缺点|差异|趋势|原因|建议).*"
    r"(?:全部|所有|完整|逐项)"
    r"|\b(?:compare|analyse|analyze|trade-?offs?|pros\s+and\s+cons)\b",
    re.IGNORECASE,
)
_PER_DOCUMENT_ANALYSIS_REQUEST = re.compile(
    r"逐篇(?=[^，,。；;!?！？]*(?:分析|比较|对比|优缺点|差异|趋势|原因|建议))"
)


def _has_unresolved_reference(text: str) -> bool:
    return bool(_UNRESOLVED_REFERENCE.search(
        _CURRENT_CONTAINER_REFERENCE.sub("", text)
    ))


def _understanding_response_is_valid(data: object) -> bool:
    """Require the fields that let the model alone decide whether to ask.

    Every ambiguity row must carry a question and the list must fit the
    contract's row ceiling: a row the parser would drop is malformed output,
    and it must fall back to the wording rules instead of silently clearing
    the request.
    """
    if not isinstance(data, dict):
        return False
    ambiguities = data.get("ambiguities")
    return (
        isinstance(data.get("normalized_question"), str)
        and bool(data["normalized_question"].strip())
        and str(data.get("intent_type") or "").strip().lower() in _INTENT_TYPES
        and str(data.get("result_scope") or "").strip().lower()
        in RESULT_SCOPES
        and isinstance(data.get("completeness_required"), bool)
        and isinstance(data.get("mandatory_topics"), list)
        and isinstance(ambiguities, list)
        and len(ambiguities) <= AMBIGUITY_ROWS_MAX
        and all(
            isinstance(row, dict) and bool(as_text(row.get("question")))
            for row in ambiguities
        )
        and isinstance(data.get("needs_clarification"), bool)
    )


def _complete_match_is_negated(question: str, match: re.Match) -> bool:
    prefix = question[max(0, match.start() - 24):match.start()]
    prefix = re.split(r"[，,。；;!?！？]", prefix)[-1]
    return bool(_COMPLETE_NEGATION.search(prefix + match.group(0)))


def _has_unnegated_complete_request(question: str) -> bool:
    """Return true when at least one completeness instruction is not negated.

    Negation is deliberately local to each lexical match.  A sentence may state
    that not every item is applicable and then independently ask to list every
    item; a negation in the first clause must not cancel the later instruction.
    """
    for match in _COMPLETE_REQUEST.finditer(question):
        if not _complete_match_is_negated(question, match):
            return True
    return False


def _has_per_document_analysis(question: str) -> bool:
    # This new completeness spelling must not inherit an analysis request from
    # a rejected clause, e.g. "不用逐篇分析，逐一列出标题" is a title list only.
    return any(
        not _complete_match_is_negated(question, match)
        for match in _PER_DOCUMENT_ANALYSIS_REQUEST.finditer(question)
    )


def _aggregate_match_is_negated(question: str, match: re.Match) -> bool:
    prefix = question[max(0, match.start() - 16):match.start()]
    prefix = re.split(r"[，,。；;!?！？]", prefix)[-1]
    suffix = question[match.end():match.end() + 16]
    suffix = re.split(r"[，,。；;!?！？]", suffix)[0]
    return bool(
        _AGGREGATE_PREFIX_NEGATION.search(prefix)
        or _AGGREGATE_SUFFIX_NEGATION.search(suffix)
    )


def _has_unnegated_aggregate_request(question: str) -> bool:
    return any(
        not _aggregate_match_is_negated(question, match)
        for match in _AGGREGATE_REQUEST.finditer(question)
    )


def _has_negated_scope_request(question: str) -> bool:
    """Whether the question explicitly declines a full-set / count request
    (and no unnegated one is present — callers check those first)."""
    return any(
        _complete_match_is_negated(question, match)
        for match in _COMPLETE_REQUEST.finditer(question)
    ) or any(
        _aggregate_match_is_negated(question, match)
        for match in _AGGREGATE_REQUEST.finditer(question)
    )


def _clarification_scope_signal(answer: str) -> str:
    """Return the last non-negated explicit scope choice in an answer."""
    signals: list[tuple[int, str]] = []
    for match in _COMPLETE_REQUEST.finditer(answer):
        if not _complete_match_is_negated(answer, match):
            signals.append((match.start(), "complete"))
    for match in _AGGREGATE_REQUEST.finditer(answer):
        if not _aggregate_match_is_negated(answer, match):
            signals.append((match.start(), "aggregate"))
    for match in _RANKED_SCOPE_ANSWER.finditer(answer):
        matched = match.group(0)
        is_negative_complete = bool(_COMPLETE_NEGATION.search(matched))
        prefix = answer[max(0, match.start() - 12):match.start()]
        prefix = re.split(r"[，,。；;!?！？]", prefix)[-1]
        if not is_negative_complete and _RANKED_POSITIVE_NEGATION.search(prefix):
            continue
        signals.append((match.start(), "ranked"))
    return max(signals, default=(-1, ""), key=lambda row: row[0])[1]


# A model widening the scope on its own (no full-set wording in the question)
# is trusted only when its reply is internally consistent AND it says it is
# confident enough. Below this the classification counts as a guess and the
# cheaper ranked retrieval stands; the user can still widen at confirmation.
MODEL_SCOPE_MIN_CONFIDENCE = 0.5


def _result_scope(
    data: dict, question: str, *, status: dict | None = None,
) -> tuple[str, bool]:
    """Decide the result scope: the model decides, deterministic wording bounds it.

    Harness principle (user decision, 2026-09-14): the model is the brain and
    owns this classification; the server only catches the two ways it can be
    wrong.

    1. **Lexical floor.** Explicit full-set wording in the question ("所有 /
       every / 一共多少 …", with clause-local negation respected) fixes the
       scope deterministically — a model may never turn "list every method"
       into a ranked top-N, and an exact count always needs full coverage.
    2. **Model decision.** Without such wording the model's ``result_scope``
       stands, including a widening to complete / aggregate / hybrid — *if*
       the reply is consistent (``completeness_required`` true for a non-ranked
       scope) and its ``confidence`` is at least ``MODEL_SCOPE_MIN_CONFIDENCE``.
       A low-confidence or self-contradicting widening falls back to ranked:
       collection enumeration is the expensive executor, so a guess does not
       get to choose it. (Before this rule the server overrode the model
       unconditionally and the prompt's classification instructions were
       decoration.)

    ``status["scope_source"]`` records which rule decided ("lexical", "model",
    "default") so a run's trace can say why a collection scan happened.
    """
    raw_scope = as_text(data.get("result_scope")).lower()
    model_scope = raw_scope if raw_scope in RESULT_SCOPES else ""
    wants_complete = _has_unnegated_complete_request(question)
    wants_aggregate = _has_unnegated_aggregate_request(question)
    wants_analysis = bool(_ANALYSIS_REQUEST.search(question)) or _has_per_document_analysis(question)
    source = "default"
    if wants_aggregate:
        # "列出所有方法并比较优缺点" remains hybrid; a plain exact count/group
        # is aggregate.  Both still require complete collection coverage.
        scope = "hybrid" if wants_analysis else "aggregate"
        source = "lexical"
    elif wants_complete:
        scope = "hybrid" if wants_analysis else "complete"
        source = "lexical"
    elif (
        _has_negated_scope_request(question)
        or _clarification_scope_signal(question) == "ranked"
    ):
        # "不需要所有方法" / "只给最相关的几个": the user explicitly declined
        # the full set or asked for the most relevant few, which outranks a
        # model (or an earlier accepted scope) that still wants to enumerate.
        scope, source = "ranked", "lexical"
    elif model_scope and model_scope != "ranked":
        consistent = data.get("completeness_required") is True
        confidence = _confidence_value(data.get("confidence"))
        if consistent and confidence >= MODEL_SCOPE_MIN_CONFIDENCE:
            scope, source = model_scope, "model"
        else:
            scope = "ranked"
    else:
        scope = "ranked"
        if model_scope == "ranked":
            source = "model"
    if status is not None:
        status["scope_source"] = source
    completeness_required = scope != "ranked"
    return scope, completeness_required


def _accepted_scope(seed: dict) -> dict:
    """The seed contract's scope re-expressed as an already-accepted model
    decision, so confirmation-time recomputation keeps it unless the final
    wording or a clarification answer overrides it (codex #725 R1).

    Recomputing from ``{}`` silently reset a model-chosen complete/aggregate
    scope to ranked whenever the user answered an unrelated clarification or
    lightly edited the wording — the decision was accepted at plan time, so
    it re-enters the rule set as consistent and confident.
    """
    scope = as_text(seed.get("result_scope")).lower()
    if scope not in RESULT_SCOPES or scope == "ranked":
        return {}
    # Provenance without a wire field: if the ORIGINAL question's wording
    # already yields a non-ranked scope, the seed scope came from wording the
    # user may have just removed, so the edited wording is judged from
    # scratch. Only a scope the wording could not have produced was the
    # model's decision (codex #725 R2).
    lexical_scope, _ = _result_scope({}, as_text(seed.get("objective")))
    if lexical_scope != "ranked":
        return {}
    return {"result_scope": scope, "completeness_required": True, "confidence": 1.0}


def _confidence_value(value: object) -> float:
    """The model's 0..1 confidence as a float; anything unusable is 0.0."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(1.0, number))


def _bounded_strings(value: object, limit: int = 8, item_chars: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    # String-only: the shape boundary delivers off-type items, and str() would
    # turn a dict/list item into container syntax that reads as prose.
    return [
        item.strip()[:item_chars]
        for item in value
        if isinstance(item, str) and item.strip()
    ][:limit]


def plan_query_intent(
    client: Any,
    question: str,
    history: str = "",
    *,
    max_topics: int = 6,
    purpose: str = "evidence-grounded answer",
    cancel_event: CancelEvent = None,
    status: dict[str, bool] | None = None,
) -> dict:
    """Build one bounded intent contract without reading corpus content."""
    from app.services.prompts import (
        QUERY_INTENT_SCHEMA_HINT,
        query_intent_prompt,
    )

    question = str(question or "").strip()
    history = str(history or "")[:8000]
    max_topics = max(1, min(int(max_topics), 16))
    data: dict = {}
    understanding_succeeded = False
    try:
        if getattr(client, "configured", False):
            raw = client.chat_json(
                [{"role": "user", "content": query_intent_prompt(
                    question,
                    max_topics=max_topics,
                    history_block=history,
                    purpose=purpose,
                )}],
                QUERY_INTENT_SCHEMA_HINT,
                cancel_event=cancel_event,
            )
            parsed = json.loads(raw)
            data = parsed if isinstance(parsed, dict) else {}
            understanding_succeeded = _understanding_response_is_valid(data)
    except AskCancelled:
        raise
    except Exception:
        data = {}
    if status is not None:
        status["understanding_succeeded"] = understanding_succeeded

    topics: list[dict] = []
    raw_topics = data.get("mandatory_topics")
    if isinstance(raw_topics, list):
        for index, topic in enumerate(raw_topics[:max_topics], 1):
            if not isinstance(topic, dict):
                continue
            topic_question = as_text(topic.get("question"))[:1000]
            title = (as_text(topic.get("title")) or topic_question)[:200]
            queries = _bounded_strings(topic.get("retrieval_queries"), 4, 1000)
            if not title or not topic_question:
                continue
            topics.append({
                "id": f"intent-{index}",
                "title": title,
                "question": topic_question,
                "retrieval_queries": queries or [topic_question],
            })
    if not topics:
        # Bounded like the model-supplied rows above: ``QueryIntentTopic``
        # caps ``question`` and each retrieval query at 1000 characters, and a
        # longer question with no usable model topics (model unconfigured,
        # timed out, or malformed JSON) used to make the contract itself
        # unconstructable. The whole question stays authoritative regardless:
        # the first retrieval slot is always the confirmed question verbatim.
        topics = [{
            "id": "intent-1",
            "title": question[:80] or "分析",
            "question": question[:1000],
            "retrieval_queries": [question[:1000]],
        }]

    ambiguities: list[dict] = []
    raw_ambiguities = data.get("ambiguities")
    if isinstance(raw_ambiguities, list):
        for index, item in enumerate(raw_ambiguities[:AMBIGUITY_ROWS_MAX], 1):
            if not isinstance(item, dict):
                continue
            prompt = (
                as_text(item.get("question"))[:AMBIGUITY_QUESTION_MAX_CHARS]
            )
            if not prompt:
                continue
            ambiguities.append({
                "id": f"ambiguity-{index}",
                "question": prompt,
                "reason": as_text(item.get("reason"))[:300],
                "required": item.get("required") is not False,
                "options": _bounded_strings(item.get("options"), 4, 200),
            })

    entities = _bounded_strings(data.get("entities"))
    deterministic_question = ""
    deterministic_reason = ""
    # A valid model understanding alone decides whether to ask: wording such as
    # "它/这个/that" is the model's to resolve from the question and history.
    # The two wording rules below only stand in when no usable understanding
    # exists (unconfigured, failed or malformed model output), which includes
    # every ``client=None`` gate on the direct-compatibility path.
    if not understanding_succeeded:
        normalized_candidate = as_text(data.get("normalized_question"))
        context_for_referent = f"{question}\n{history}".casefold()
        has_verified_referent = any(
            entity.casefold() in context_for_referent for entity in entities
        )
        if (
            _has_unresolved_reference(question)
            and (
                not normalized_candidate
                or _has_unresolved_reference(normalized_candidate)
                or not has_verified_referent
            )
        ):
            deterministic_question = "你提到的对象具体是什么？请给出名称或简要背景。"
            deterministic_reason = "问题包含无法从当前会话上下文解析的指代。"
        elif _GENERIC_REQUEST.fullmatch(question):
            deterministic_question = "你希望分析的具体对象和最关心的问题是什么？"
            deterministic_reason = "当前输入缺少可确定检索主题的对象或目标。"
    if deterministic_question and not any(
        row["question"] == deterministic_question for row in ambiguities
    ):
        ambiguities.insert(0, {
            "id": "ambiguity-input",
            "question": deterministic_question,
            "reason": deterministic_reason,
            "required": True,
            "options": [],
        })
        # ``QueryIntentContract.ambiguities`` has a hard ceiling of eight rows.
        # A malformed model response may still carry eight rows of its own
        # (they are kept above even when the response fails validation), so
        # inserting the deterministic row unconditionally can produce a ninth
        # and make the
        # contract unconstructable — a pydantic ValidationError on an ordinary
        # unresolved-referent question, i.e. exactly the deterministic failure
        # this whole area is supposed to avoid.  Drop the model's least
        # important row instead: the server's own finding is inserted first and
        # must survive.
        del ambiguities[AMBIGUITY_ROWS_MAX:]
    # The model's explicit "ask" verdict stands even when every row it gave is
    # optional; with the wording rules no longer overriding a valid
    # understanding, nothing else would pause such a request.
    if data.get("needs_clarification") is True and not any(
        row["required"] for row in ambiguities
    ):
        taken = {row["id"] for row in ambiguities}
        index = 1
        while f"ambiguity-{index}" in taken:
            index += 1
        ambiguities.insert(0, {
            "id": f"ambiguity-{index}",
            "question": "为了准确检索，还需要补充哪项会改变问题方向的关键信息？",
            "reason": "问题理解模型判断当前请求仍存在会改变检索主题的歧义。",
            "required": True,
            "options": [],
        })
        del ambiguities[AMBIGUITY_ROWS_MAX:]

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    intent_type = str(data.get("intent_type") or "other").strip().lower()
    if intent_type not in _INTENT_TYPES:
        intent_type = "other"
    normalized_question = (
        as_text(data.get("normalized_question"))[:RESOLVED_QUESTION_MAX_CHARS]
        or question
    )
    result_scope, completeness_required = _result_scope(data, question, status=status)
    contract = {
        "objective": question,
        "resolved_question": normalized_question,
        "intent_type": intent_type,
        "result_scope": result_scope,
        "completeness_required": completeness_required,
        "entities": entities,
        "mandatory_topics": topics,
        "comparison_axes": _bounded_strings(data.get("comparison_axes")),
        "constraints": _bounded_strings(data.get("constraints")),
        "excluded_topics": _bounded_strings(data.get("excluded_topics")),
        "expected_output": as_text(data.get("expected_output"))[:1000],
        "assumptions": _bounded_strings(data.get("assumptions")),
        "ambiguities": ambiguities,
        "confidence": confidence,
        "needs_clarification": any(
            row.get("required") is not False for row in ambiguities
        ),
        "confirmed": False,
    }
    return contract


def conversation_intent_history(turns: Iterable[Any]) -> str:
    """Render the history block the corpus-blind understanding step may see.

    Only the user's own wording of the last five turns may resolve references.
    Assistant answers are corpus-derived and would let retrieved material bias
    this otherwise corpus-blind step indirectly. One construction shared by
    the HTTP ``/ask/intent`` preview and MCP ``ask_notebook``'s in-call
    understanding, so the two entry points cannot drift on what the model is
    allowed to look at.
    """
    recent = list(turns)[-5:]
    return "\n".join(f"User: {turn.question}" for turn in recent)


_INTENT_MISMATCH_MESSAGE = "问题理解与当前问题不匹配，请重新确认"
_MISSING_ANSWERS_MESSAGE = "请先回答所有必填澄清问题"
_EMPTY_RESOLVED_MESSAGE = "确认后的问题不能为空"


def validate_confirmed_intent(
    question: str,
    contract: dict,
    *,
    resolved_question: str,
    answers: Iterable[dict],
) -> dict:
    """Freeze a client-confirmed intent, or raise the user-facing reason.

    The one rail every entry point that accepts a confirmed intent runs
    before a durable Ask job exists -- HTTP ``/ask`` and ``/ask/stream``
    translate the ``ValueError`` into a 422, MCP ``ask_notebook`` surfaces it
    as the tool error verbatim. The messages are complete user copy, so a
    caller may show them as-is. ``objective`` must be the question byte for
    byte: a contract reviewed for one question never confirms another.
    """
    if str(contract.get("objective") or "").strip() != question.strip():
        raise ValueError(_INTENT_MISMATCH_MESSAGE)
    try:
        return finalize_query_intent(
            contract, resolved_question=resolved_question, answers=answers
        )
    except ValueError as exc:
        if "必填澄清" in str(exc):
            raise ValueError(_MISSING_ANSWERS_MESSAGE) from None
        raise ValueError(_EMPTY_RESOLVED_MESSAGE) from None


_CLARIFICATION_GATE_PREFIX = "问题仍有关键歧义，请先确认问题理解"
_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧"
assert len(_CIRCLED_DIGITS) == AMBIGUITY_ROWS_MAX, (
    "one circled digit per admissible ambiguity row; raise both together"
)


def clarification_gate_message(seed: dict) -> str:
    """Render a deterministic-ambiguity gate's error text with the questions.

    Every fail-closed clarification gate (HTTP direct ``/ask``, the engine's
    compatibility branch, MCP ``ask_notebook``) shares this one construction
    so the wording and rules -- at most ``AMBIGUITY_ROWS_MAX`` rows, each
    capped at ``AMBIGUITY_QUESTION_MAX_CHARS`` (the same named ceilings
    ``QueryIntentAmbiguity`` enforces), numbered with circled digits, blank
    rows skipped
    -- cannot drift apart across the three call sites. Only
    ``seed["ambiguities"][*]["question"]`` is used; ``reason`` and the
    caller's original wording (``seed["objective"]``) are deliberately never
    echoed back into an error string.
    """
    raw_ambiguities = seed.get("ambiguities") if isinstance(seed, dict) else None
    questions: list[str] = []
    if isinstance(raw_ambiguities, list):
        for row in raw_ambiguities:
            if not isinstance(row, dict):
                continue
            question = str(row.get("question") or "").strip()
            if not question:
                continue
            questions.append(question[:AMBIGUITY_QUESTION_MAX_CHARS])
            if len(questions) == AMBIGUITY_ROWS_MAX:
                break
    if not questions:
        return _CLARIFICATION_GATE_PREFIX
    numbered = "；".join(
        f"{_CIRCLED_DIGITS[index]} {question}"
        for index, question in enumerate(questions)
    )
    return f"{_CLARIFICATION_GATE_PREFIX}：{numbered}"


def followup_gate(
    original: str,
    resolved: str,
    history: str = "",
    *,
    max_topics: int,
) -> tuple[dict | None, str]:
    """Judge a follow-up question's clarity, optionally through a rewrite.

    The one construction every caller shares that must decide whether a
    reasoning Ask may proceed without a client-confirmed intent. Returns
    ``(seed, "")`` when the request may run and ``(None, message)`` when it
    must fail closed, so each caller raises its own error type over identical
    copy instead of re-deriving it.

    ``resolved`` is a model rewrite of ``original`` (empty when no rewrite was
    attempted). Two rules keep that rewrite from widening what the gate
    accepts or from leaking into what the user reads:

    * The gate copy always comes from ``original``'s own seed. A rewrite is
      machine-generated text *about* the user's question, and echoing it back
      inside "问题仍有关键歧义…" would ask the user to clarify wording they
      never wrote.
    * Only ``resolved_question`` follows the rewrite. ``objective``,
      ``result_scope``, ``completeness_required`` and ``mandatory_topics``
      stay the original seed's, so a rewrite that drops "全部" cannot silently
      downgrade a complete-collection request to a ranked top-N.

    ``history`` is forwarded to both deterministic plans for parity with the
    engine's call shape, but with ``client=None`` it does not take part in
    the verdict (no model reads it; the deterministic rules look only at the
    question). It stays so a future model-backed probe needs no signature
    change.

    An empty rewrite — or one that is the original verbatim — takes the
    original seed's own verdict, byte for byte what a caller with no rewrite
    step does. So does a rewrite longer than ``RESOLVED_QUESTION_MAX_CHARS``:
    it could not be assembled into a contract anyway (the model would raise
    ``ValidationError`` after the entry gate had already let the request
    through), and truncating it would hand retrieval half a sentence — an
    over-long rewrite is a failed rewrite, not a shorter one.
    """
    seed = plan_query_intent(None, original, history, max_topics=max_topics)
    rewritten = str(resolved or "").strip()
    if len(rewritten) > RESOLVED_QUESTION_MAX_CHARS:
        rewritten = ""
    if not rewritten or rewritten == str(original or "").strip():
        if seed.get("needs_clarification"):
            return None, clarification_gate_message(seed)
        return seed, ""
    probe = plan_query_intent(None, rewritten, history, max_topics=max_topics)
    if probe.get("needs_clarification"):
        return None, clarification_gate_message(seed)
    seed["resolved_question"] = rewritten
    # The rewrite resolved what the original left open. ``needs_clarification``
    # is this dict's own restatement of ``ambiguities`` (``plan_query_intent``
    # derives one from the other), so clearing one without the other would
    # hand consumers a contract that contradicts itself.
    seed["ambiguities"] = []
    seed["needs_clarification"] = False
    return seed, ""


def finalize_query_intent(
    seed: dict,
    *,
    resolved_question: str = "",
    answers: Iterable[dict] = (),
) -> dict:
    """Freeze the reviewed contract; never ask a model to reinterpret it."""
    ambiguities = {
        str(row.get("id") or ""): row
        for row in (seed.get("ambiguities") or [])
        if isinstance(row, dict) and row.get("id")
    }
    submitted = {
        str(row.get("id") or "").strip(): str(row.get("answer") or "").strip()
        for row in answers
        if isinstance(row, dict)
        and str(row.get("id") or "").strip() in ambiguities
        and str(row.get("answer") or "").strip()
    }
    if any(
        row.get("required") is not False and not submitted.get(ambiguity_id)
        for ambiguity_id, row in ambiguities.items()
    ):
        raise ValueError("请先回答所有必填澄清问题")
    resolved = str(
        resolved_question
        or seed.get("resolved_question")
        or seed.get("objective")
        or ""
    ).strip()
    if not resolved:
        raise ValueError("确认后的问题不能为空")
    answer_rows = [{
        "id": ambiguity_id,
        "question": str(ambiguities[ambiguity_id].get("question") or ""),
        "answer": answer,
    } for ambiguity_id, answer in submitted.items()]
    final = dict(seed)
    # The user-visible confirmation field is authoritative when it was edited
    # (or clarification answers changed the reviewed direction).  A clear
    # auto-confirmed preview is different: product policy keeps the original
    # wording authoritative, so a model normalization that drops "all" must not
    # silently downgrade the deterministic scope.
    seed_resolved = str(seed.get("resolved_question") or "").strip()
    wording_changed = bool(
        str(resolved_question or "").strip()
        and str(resolved_question or "").strip() != seed_resolved
    )
    if answer_rows:
        answer_text = "\n".join(row["answer"] for row in answer_rows)
        answer_scope = _clarification_scope_signal(answer_text)
        if answer_scope == "ranked":
            result_scope, completeness_required = "ranked", False
        elif answer_scope in {"complete", "aggregate"}:
            # Answers are authoritative for collection scope, while the
            # confirmed wording still supplies analysis/aggregation context.
            result_scope, completeness_required = _result_scope(
                _accepted_scope(seed), f"{resolved}\n{answer_text}"
            )
        else:
            # No positive scope choice in the answer — but a negated one
            # ("总数不需要") must still cap an accepted model scope, so the
            # answer text stays in the judged wording (codex #725 R3).
            result_scope, completeness_required = _result_scope(
                _accepted_scope(seed), f"{resolved}\n{answer_text}"
            )
    elif wording_changed:
        result_scope, completeness_required = _result_scope(
            _accepted_scope(seed), resolved
        )
    else:
        result_scope = str(seed.get("result_scope") or "ranked")
        completeness_required = bool(seed.get("completeness_required"))
    final.update(
        resolved_question=resolved,
        result_scope=result_scope,
        completeness_required=completeness_required,
        ambiguities=[],
        needs_clarification=False,
        confirmed=True,
        clarification_answers=answer_rows,
    )
    final.pop("confirmed_input", None)
    return final


def confirmed_research_question(
    intent_contract: dict,
    fallback: str,
    *,
    objective_is_authoritative: bool = False,
    include_assumptions: bool = True,
) -> str:
    """Combine reviewed wording and explicit answers for every retrieval plane."""
    objective = str(intent_contract.get("objective") or fallback).strip()
    resolved = str(intent_contract.get("resolved_question") or objective).strip()
    # Clear Ask questions auto-continue without a human review step. In that
    # path the model's normalization may enrich the query, but it must never
    # replace the user's original wording as the primary authority.
    base = objective if objective_is_authoritative and objective else resolved
    supplements: list[str] = []
    if objective_is_authoritative and resolved and resolved != base:
        supplements.append("结构化问题理解（仅作补充）：" + resolved)
    entities = _bounded_strings(intent_contract.get("entities"))
    if entities:
        supplements.append("研究对象：" + "、".join(entities))
    topics = []
    for row in (intent_contract.get("mandatory_topics") or [])[:16]:
        if not isinstance(row, dict):
            continue
        topic = str(row.get("question") or "").strip()
        if not topic:
            continue
        # A clarification can make the reviewed resolved question authoritative
        # while the corpus-blind seed topic still contains "this/that". Do not
        # reintroduce that unresolved wording into the downstream research query.
        if _GENERIC_REQUEST.fullmatch(topic) or _UNRESOLVED_REFERENCE.search(topic):
            topic = base
        if topic and topic not in topics:
            topics.append(topic)
    if topics:
        supplements.append("必须覆盖的问题：\n" + "\n".join(
            f"- {topic[:1000]}" for topic in topics
        ))
    scoped_fields = [
        ("比较维度", "comparison_axes"),
        ("约束条件", "constraints"),
        ("明确排除范围", "excluded_topics"),
    ]
    # Reports keep model-suggested assumptions visible to writers and auditors,
    # but do not lexicalise them into retrieval.  Other consumers retain the
    # historical combined query by default.
    if include_assumptions:
        scoped_fields.append(("成立前提", "assumptions"))
    for label, key in scoped_fields:
        values = _bounded_strings(intent_contract.get(key))
        if values:
            supplements.append(f"{label}：" + "、".join(values))
    expected_output = str(intent_contract.get("expected_output") or "").strip()
    if expected_output:
        supplements.append(f"期望输出：{expected_output[:1000]}")
    for row in (intent_contract.get("clarification_answers") or [])[:8]:
        if not isinstance(row, dict):
            continue
        answer = str(row.get("answer") or "").strip()
        if not answer:
            continue
        prompt = str(row.get("question") or row.get("id") or "补充信息").strip()
        supplements.append(f"用户确认：{prompt[:200]}：{answer[:500]}")
    if not supplements:
        return base
    return f"{base}\n\n用户确认的补充信息与问题契约：\n" + "\n".join(supplements)


def confirmed_intent_queries(
    intent_contract: dict,
    fallback: str,
    *,
    max_queries: int,
    objective_is_authoritative: bool = False,
) -> list[str]:
    """Seed bounded initial retrieval directly from every reviewed direction.

    The round-robin order preserves at least one direction per mandatory topic
    before spending the remaining budget on additional aliases/comparison sides.
    Each seed carries the frozen contract so clarification answers and constraints
    apply to retrieval rather than only to final synthesis.
    """
    # One authoritative whole-question seed plus at most one seed for each of
    # the contract's 16 bounded mandatory topics.
    limit = max(1, min(int(max_queries), 17))
    authoritative = confirmed_research_question(
        intent_contract,
        fallback,
        objective_is_authoritative=objective_is_authoritative,
    )
    buckets: list[list[str]] = []
    for row in (intent_contract.get("mandatory_topics") or [])[:16]:
        if not isinstance(row, dict):
            continue
        topic = str(row.get("question") or "").strip()
        queries = _bounded_strings(row.get("retrieval_queries"), 4, 1000)
        bucket = queries or ([topic] if topic else [])
        if bucket:
            buckets.append(bucket)
    if not buckets:
        return [authoritative]

    # The complete confirmed question always runs first. This prevents model-
    # proposed decompositions from narrowing or replacing the user's topic.
    seeds: list[str] = [authoritative]
    if len(seeds) >= limit:
        return seeds
    for direction_index in range(4):
        for bucket in buckets:
            if direction_index >= len(bucket):
                continue
            direction = bucket[direction_index]
            if (
                _GENERIC_REQUEST.fullmatch(direction)
                or _UNRESOLVED_REFERENCE.search(direction)
            ):
                query = authoritative
            elif direction == authoritative:
                query = direction
            else:
                query = (
                    f"{direction}\n\n检索必须服从以下已确认问题契约：\n"
                    f"{authoritative}"
                )[:8000]
            if query not in seeds:
                seeds.append(query)
            if len(seeds) >= limit:
                return seeds
    return seeds or [authoritative]
