"""Corpus-blind query-intent contracts shared by reports and reasoning Ask.

This module deliberately has no repository/retrieval dependency. It may use a
model to understand the user's wording, but it must finish before corpus
candidates are allowed to influence the requested topic.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from app.core.ask_retrieval_policy import RESULT_SCOPES
from app.services.cancellation import AskCancelled, CancelEvent


_UNRESOLVED_REFERENCE = re.compile(
    r"(?:这个|那个|这些|那些|上述|前述|刚才|之前提到|该问题|该方案|它们?)"
    r"|\b(?:this|that|these|those|it|they|above|previous|former|latter)\b",
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
    r"完整(?:地)?(?:列出|罗列|枚举|读取|覆盖)|完整(?:的)?(?:清单|列表|集合)|逐一|逐项|"
    r"每(?:一)?种|每一项|每一个|列全|无遗漏|穷举)"
    r"|\b(?:all|every|each|entire|exhaustive(?:ly)?|complete\s+list|"
    r"without\s+omission)\b",
    re.IGNORECASE,
)
_COMPLETE_NEGATION = re.compile(
    r"(?:不(?:用|必|需要|要求|是)?|无需|并非(?:必须|需要)?|非)"
    r"(?:列出|包含|覆盖|枚举)?(?:全部|所有|完整|逐一|逐项)"
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


def _result_scope(data: dict, question: str) -> tuple[str, bool]:
    """Normalize model scope and deterministically protect full-set wording.

    The lexical fallback only upgrades completeness; it never lets a model turn
    an explicit "all/every" request into ranked top-N.  Explicit negation (for
    example "不需要所有") prevents a false upgrade.  Exact aggregate requests
    also require full collection coverage even when they do not list every row.
    """
    # Completeness changes the executor from relevance ranking to bounded
    # collection enumeration, so the model may not upgrade it on its own.
    # Only deterministic, reviewable request wording below can do that.  This
    # avoids treating domain nouns such as “统计方法” or “数据完整性” as an
    # instruction to scan an entire table.
    raw_scope = str(data.get("result_scope") or "ranked").strip().lower()
    scope = raw_scope if raw_scope in RESULT_SCOPES else "ranked"
    wants_complete = _has_unnegated_complete_request(question)
    wants_aggregate = _has_unnegated_aggregate_request(question)
    wants_analysis = bool(_ANALYSIS_REQUEST.search(question))
    if wants_aggregate:
        # "列出所有方法并比较优缺点" remains hybrid; a plain exact count/group
        # is aggregate.  Both still require complete collection coverage.
        scope = "hybrid" if wants_analysis else "aggregate"
    elif wants_complete:
        scope = "hybrid" if wants_analysis else "complete"
    else:
        scope = "ranked"
    completeness_required = scope != "ranked"
    return scope, completeness_required


def _bounded_strings(value: object, limit: int = 8, item_chars: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()[:item_chars]
        for item in value
        if str(item).strip()
    ][:limit]


def plan_query_intent(
    client: Any,
    question: str,
    history: str = "",
    *,
    max_topics: int = 6,
    purpose: str = "evidence-grounded answer",
    cancel_event: CancelEvent = None,
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
    except AskCancelled:
        raise
    except Exception:
        data = {}

    topics: list[dict] = []
    raw_topics = data.get("mandatory_topics")
    if isinstance(raw_topics, list):
        for index, topic in enumerate(raw_topics[:max_topics], 1):
            if not isinstance(topic, dict):
                continue
            topic_question = str(topic.get("question") or "").strip()[:1000]
            title = str(topic.get("title") or topic_question).strip()[:200]
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
        topics = [{
            "id": "intent-1",
            "title": question[:80] or "分析",
            "question": question,
            "retrieval_queries": [question],
        }]

    ambiguities: list[dict] = []
    raw_ambiguities = data.get("ambiguities")
    if isinstance(raw_ambiguities, list):
        for index, item in enumerate(raw_ambiguities[:8], 1):
            if not isinstance(item, dict):
                continue
            prompt = str(item.get("question") or "").strip()[:500]
            if not prompt:
                continue
            ambiguities.append({
                "id": f"ambiguity-{index}",
                "question": prompt,
                "reason": str(item.get("reason") or "").strip()[:300],
                "required": item.get("required") is not False,
                "options": _bounded_strings(item.get("options"), 4, 200),
            })

    normalized_candidate = str(data.get("normalized_question") or "").strip()
    entities = _bounded_strings(data.get("entities"))
    context_for_referent = f"{question}\n{history}".casefold()
    has_verified_referent = any(
        entity.casefold() in context_for_referent for entity in entities
    )
    deterministic_question = ""
    deterministic_reason = ""
    if (
        _UNRESOLVED_REFERENCE.search(question)
        and (
            not normalized_candidate
            or _UNRESOLVED_REFERENCE.search(normalized_candidate)
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
        # The model may legitimately return eight of its own, so inserting the
        # deterministic row unconditionally can produce a ninth and make the
        # contract unconstructable — a pydantic ValidationError on an ordinary
        # unresolved-referent question, i.e. exactly the deterministic failure
        # this whole area is supposed to avoid.  Drop the model's least
        # important row instead: the server's own finding is inserted first and
        # must survive.
        del ambiguities[8:]
    if data.get("needs_clarification") is True and not ambiguities:
        ambiguities.append({
            "id": "ambiguity-1",
            "question": "为了准确检索，还需要补充哪项会改变问题方向的关键信息？",
            "reason": "问题理解模型判断当前请求仍存在会改变检索主题的歧义。",
            "required": True,
            "options": [],
        })

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    intent_type = str(data.get("intent_type") or "other").strip().lower()
    if intent_type not in _INTENT_TYPES:
        intent_type = "other"
    normalized_question = (
        str(data.get("normalized_question") or "").strip()[:4000] or question
    )
    result_scope, completeness_required = _result_scope(data, question)
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
        "expected_output": str(data.get("expected_output") or "").strip()[:1000],
        "assumptions": _bounded_strings(data.get("assumptions")),
        "ambiguities": ambiguities,
        "confidence": confidence,
        "needs_clarification": any(
            row.get("required") is not False for row in ambiguities
        ),
        "confirmed": False,
    }
    return contract


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
                {}, f"{resolved}\n{answer_text}"
            )
        else:
            result_scope, completeness_required = _result_scope({}, resolved)
    elif wording_changed:
        result_scope, completeness_required = _result_scope({}, resolved)
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
