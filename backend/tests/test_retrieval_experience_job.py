"""Agentic Memory P2 (T5): the distillation chain and the projection that
guards its input.

Three groups of coverage, and the FIRST one is the point of the feature:

1. **Structural** — ``RunObservation`` and everything reachable from it carry
   no free text. The full privacy guard (input-face scanning, the reverse guard
   on the action vocabulary, the mutation tests) is T6's, but this module's own
   invariant belongs with the module that establishes it: a T5 that ships with
   a ``str`` field in the observation has already lost the argument that makes
   a deployment-global table acceptable, whether or not T6 has landed yet.
2. **Projection** — what one persisted run becomes.
3. **The run** — gating, aggregation, reply validation, idempotence.
"""
from __future__ import annotations

import typing
from dataclasses import fields, is_dataclass

import pytest

from app.domain.retrieval_experience import project_run_step
from app.repositories.ports import (
    RETRIEVAL_EXPERIENCE_BATCH_RUNS,
    RETRIEVAL_EXPERIENCE_BATCH_STEPS,
    RETRIEVAL_EXPERIENCE_MAX_ENTRIES,
    RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES,
    RETRIEVAL_EXPERIENCE_PROVENANCE_MAX,
    RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS,
)
from app.services import background_jobs
from app.services.retrieval_experience_job import (
    _MANUAL_DISTILL_COOLDOWN_SECONDS,
    _MAX_QUEUED_PARTITIONS,
    _offered_entries,
    MANUAL_DISTILL_BUSY,
    MANUAL_DISTILL_COOLDOWN,
    MANUAL_DISTILL_DISABLED,
    MANUAL_DISTILL_INVALID,
    MANUAL_DISTILL_STARTED,
    RetrievalExperienceDistillationService,
    distillation_wiring_active,
    parse_distillation_reply,
    render_existing,
    render_observations,
    _group_by_situation,
)
from app.services.retrieval_experience_projection import (
    RETRIEVAL_ACTIONS,
    RunObservation,
    SITUATION_KEYS,
    experience_id,
    project_run,
    situation_domain,
    situation_similarity,
    validate_situation,
)


# --------------------------------------------------------------- structural

def _reachable_field_types(root: type) -> list[tuple[str, str, object]]:
    """Every ``(owner, field, annotation)`` reachable from ``root``."""
    seen: set[type] = set()
    pending = [root]
    found: list[tuple[str, str, object]] = []
    while pending:
        current = pending.pop()
        if current in seen or not is_dataclass(current):
            continue
        seen.add(current)
        hints = typing.get_type_hints(current)
        for field in fields(current):
            annotation = hints[field.name]
            found.append((current.__name__, field.name, annotation))
            for arg in (annotation, *typing.get_args(annotation)):
                if isinstance(arg, type) and is_dataclass(arg):
                    pending.append(arg)
    return found


def _is_closed(annotation: object) -> bool:
    if annotation in (int, bool):
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return all(isinstance(value, str) for value in typing.get_args(annotation))
    if origin is tuple:
        args = [arg for arg in typing.get_args(annotation) if arg is not Ellipsis]
        return bool(args) and all(
            is_dataclass(arg) if isinstance(arg, type) else _is_closed(arg)
            for arg in args
        )
    return False


def test_the_observation_carries_no_free_text_anywhere_it_can_reach():
    """The whole reason a cross-user, cross-notebook table is acceptable.

    Everywhere else in this repository, isolation is a predicate in the reading
    SQL. This table has no tenancy column and no predicate to write, so the
    guarantee lives here instead: the model that authors an entry's rationale
    is fed nothing but ints, bools and closed vocabulary words, and therefore
    cannot carry a question, a document or a person into a row every user
    reads.

    A bare ``str``/``Any``/``dict``/``list[str]`` field is not a small
    widening — it is the difference between a guarantee and a promise.
    """
    for owner, name, annotation in _reachable_field_types(RunObservation):
        assert _is_closed(annotation), (
            f"{owner}.{name}: {annotation!r} 不是 int / bool / Literal / 由它们"
            "构成的 tuple。这个类型是全局经验库的输入面——一个自由文本字段就足以"
            "把某个人的问题、某份文档的标题带进一张全体用户都读得到的表,而且不会"
            "有任何报错。"
        )


def test_the_observation_is_not_empty():
    """Companion to the rule above, and not redundant with it.

    "Every field is closed" is trivially true of a dataclass with no fields, so
    without a floor the guard could be satisfied by gutting the type — which is
    exactly what someone would do to make a failing widening test pass.
    """
    found = _reachable_field_types(RunObservation)
    assert len(found) >= 11, found


def test_the_action_vocabulary_cannot_reach_retrieval_scope():
    """An experience influences HOW a run searches, never WHAT it may read.

    Retrieval scope is the user's own checkbox selection and the one thing this
    feature must not touch. Making that structural rather than a review promise
    means the THEN-side vocabulary simply contains no scope-shaped action.
    """
    forbidden = ("source", "base", "scope", "notebook", "mount", "library")
    for action in RETRIEVAL_ACTIONS:
        assert not any(word in action for word in forbidden), action


def test_the_action_vocabulary_excludes_memory():
    """``memory`` was removed from the THEN-side vocabulary in the T5 fix
    round (not overlooked — see ``RETRIEVAL_ACTIONS``'s docstring): a
    ``memory`` trace step is only ever emitted on a HIT (a miss is a ``skip``
    step instead), so ``zero_hits`` for it is structurally always 0 — and
    there is no reflect action id for the model to reach for, so an entry
    about it would recommend a channel nobody can act on. Pinned as an exact
    count, not just an exclusion, so a re-addition under a different name
    still moves the number this test is watching.
    """
    assert "memory" not in RETRIEVAL_ACTIONS
    # 10 = 原 8 个 + `search_chunks`(逐步推理的原文段落检索通道,D-2 追加)
    # + `read_document`(按篇读取有界原文取样,PR-A 追加)。
    assert len(RETRIEVAL_ACTIONS) == 10


def test_the_vocabulary_covers_the_raw_passage_channel():
    """D-2:``search_chunks`` 进 THEN 侧词表,否则无图 run 的**主**检索通道在
    经验库里是盲区(与今天 ``search_elements`` 记成 ``fallback`` 步、被投影整步
    丢弃一样)。追加值对既有条目是无损的:它只是词表末尾多一个词。"""
    assert RETRIEVAL_ACTIONS[-2] == "search_chunks"
    assert RETRIEVAL_ACTIONS[:-2] == (
        "retrieve", "ppr", "exact_lookup", "expand", "expand_community",
        "follow_chain", "enumerate", "outline")


def test_the_vocabulary_covers_the_per_document_read_channel():
    """PR-A:``read_document`` 进 THEN 侧词表,同 ``search_chunks`` 的追加原则
    ——只在词表末尾多一个词,既有条目一字不动。"""
    assert RETRIEVAL_ACTIONS[-1] == "read_document"


def test_the_projection_counts_search_chunks_invocations_and_zero_hits():
    """验收 5:investigation/zero-hit 统计对新通道生效。

    播种步与动作步都是 ``step_type="search_chunks"``,两者的 ``found`` 都被
    ``_TRACE_COUNT_KEYS`` 读成 ``count``,所以零命中是可观测的——这正是这条
    通道该拿到的那半个信号。
    """
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "search_chunks", "summary": "",
                 "detail": {"found": 2, "phase": "seed",
                            "result_ids": ["ck-1", "ck-2"]}},
                {"step_type": "search_chunks", "summary": "",
                 "detail": {"query": "布局", "found": 0, "result_ids": []}},
                {"step_type": "synthesis", "summary": "",
                 "detail": {"citations": 3, "anchors": 1,
                            "anchor_evidence_ids": ["ck-1"]}},
            ]
        )
    )
    assert observed is not None
    action = next(a for a in observed.observation.actions
                  if a.action == "search_chunks")
    assert action.invocations == 2
    assert action.zero_hits == 1
    # result_ids 两步都写了 ⇒ 可归因;交集只数真的被答案引用的那一条。
    assert (action.attributable, action.anchored_hits) == (True, 1)


def test_the_batch_never_reads_more_runs_than_one_entry_can_remember():
    """The invariant that makes a cursor-free distillation correct.

    An entry de-duplicates ``support`` against the run ids it retains. If one
    batch could carry more runs than one entry retains, the ids pushed off the
    tail would come back in the next overlapping batch and be counted a second
    time — and ``support`` would drift upward with distillation frequency
    rather than with evidence.
    """
    assert RETRIEVAL_EXPERIENCE_BATCH_RUNS <= RETRIEVAL_EXPERIENCE_PROVENANCE_MAX


# --------------------------------------------------------------- projection

def _intent_step(**overrides) -> dict:
    detail = {
        "resolved_question": "这个库里 set_db 是怎么用的?",
        "result_scope": "ranked",
        "completeness_required": False,
        "retrieval_effort": "standard",
        "entities": ["set_db"],
        "constraints": [],
        "excluded_topics": [],
        "assumptions": ["假设指的是 2024 版"],
        "expected_output": "一段说明",
        "mandatory_topics": ["set_db 的参数", "set_db 的默认值"],
    }
    detail.update(overrides)
    return {"step_type": "intent", "summary": "已按确认后的问题理解开始检索",
            "detail": detail, "duration_ms": 10}


def _run(steps, *, run_id="job-1", mode="reasoning") -> dict:
    projected = [project_run_step(step) for step in steps]
    return {
        "run_id": run_id,
        "mode": mode,
        "steps": [step for step in projected if step is not None],
    }


def test_the_step_projection_drops_the_summary_and_the_question():
    """``project_run_step`` is strictly narrower than ``project_trace_step``.

    The difference IS the feature's privacy argument: a step summary is a human
    sentence that several emitters interpolate model text or an error string
    into, and the resolved question is the member's own words. Both are fine in
    the per-member overlay block; neither may reach a deployment-wide table.
    """
    projected = project_run_step(_intent_step())
    assert projected is not None
    assert "summary" not in projected
    assert "resolved_question" not in str(projected)
    assert set(projected["situation"]) == {
        "result_scope", "retrieval_effort", "completeness_required",
        "entity_count", "topic_count", "has_constraints", "has_exclusions",
    }


def test_an_unexpected_enum_value_collapses_to_unknown_rather_than_passing_through():
    """The narrowing has to hold even when an upstream model misbehaves.

    ``result_scope`` is written by the intent model. If a broken one wrote a
    sentence there, passing it through would put free text into a situation
    fingerprint — and from there into a prompt and a shared row.
    """
    projected = project_run_step(
        _intent_step(result_scope="whatever the user meant by 'all of them'")
    )
    assert projected["situation"]["result_scope"] == "unknown"


def test_a_run_without_an_intent_step_is_skipped():
    """No situation means the entry's IF side would be "all unknown", which
    matches every future run equally — worse than having no entry at all."""
    assert project_run(_run([{"step_type": "retrieve", "detail": {"count": 3}}])) is None


def test_the_projection_counts_zero_hits_per_action_and_citations_per_run():
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "retrieve", "summary": "初检索", "detail": {"count": 0}},
                {"step_type": "ppr", "summary": "", "detail": {"count": 7}},
                {"step_type": "exact_lookup", "summary": "", "detail": {"found": 0}},
                {"step_type": "exact_lookup", "summary": "", "detail": {"found": 0}},
                # 真实发射器两键并存:citations 是兜底卡数(零绑定也非零),
                # anchors 才是真接地信号(codex #524 R9 P2)
                {"step_type": "synthesis", "summary": "",
                 "detail": {"citations": 9, "anchors": 4}},
            ]
        )
    )
    assert observed is not None
    by_action = {a.action: a for a in observed.observation.actions}
    assert by_action["retrieve"].zero_hits == 1
    assert by_action["ppr"].zero_hits == 0
    assert by_action["exact_lookup"].invocations == 2
    assert by_action["exact_lookup"].zero_hits == 2
    assert observed.observation.citations == 4
    assert observed.observation.answered is True


# --------------------------------------- step→anchor attribution (P4, T3)

def test_a_run_with_no_result_ids_at_all_is_not_attributable():
    """老轨迹(``result_ids`` 键整体缺席):即使 synthesis 步存在且带
    ``anchor_evidence_ids``,per-action 的 ``attributable``/``anchored_hits``
    也必须是 (False, 0),其余字段与升级前逐字相同。"""
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "ppr", "detail": {"count": 3}},  # 无 result_ids
                {"step_type": "synthesis",
                 "detail": {"anchors": 2, "anchor_evidence_ids": ["ko-1", "ko-2"]}},
            ]
        )
    )
    assert observed is not None
    ppr = next(a for a in observed.observation.actions if a.action == "ppr")
    assert (ppr.attributable, ppr.anchored_hits) == (False, 0)
    # 其余字段逐字同升级前
    assert ppr.invocations == 1
    assert ppr.zero_hits == 0
    assert observed.observation.citations == 2
    assert observed.observation.answered is True


def test_a_run_with_results_but_zero_anchors_is_attributable_with_no_hits():
    """新轨迹,有 result_ids,但答案零锚点:可归因,命中数为 0——``(True, 0)``,
    与老轨迹的 ``(False, 0)`` 手感不同但数值恰好一样,靠 ``attributable`` 区分。"""
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "retrieve",
                 "detail": {"count": 3, "result_ids": ["c-1", "c-2", "c-3"]}},
                {"step_type": "synthesis",
                 "detail": {"anchors": 0, "anchor_evidence_ids": []}},
            ]
        )
    )
    assert observed is not None
    retrieve = next(a for a in observed.observation.actions if a.action == "retrieve")
    assert (retrieve.attributable, retrieve.anchored_hits) == (True, 0)


def test_partial_binding_counts_only_the_intersecting_ids():
    """部分绑定:该动作的 result_ids 里只有一部分真的被答案引用——
    ``anchored_hits`` 只数交集,不是该动作全部结果数,也不是答案全部锚点数。"""
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "retrieve",
                 "detail": {"count": 4,
                            "result_ids": ["c-1", "c-2", "c-3", "c-4"]}},
                {"step_type": "ppr",
                 "detail": {"count": 2, "result_ids": ["c-5", "c-6"]}},
                {"step_type": "synthesis",
                 # c-2/c-3 来自 retrieve,c-5 来自 ppr,ko-9 谁都没产出过
                 "detail": {"anchors": 4,
                            "anchor_evidence_ids": ["c-2", "c-3", "c-5", "ko-9"]}},
            ]
        )
    )
    assert observed is not None
    by_action = {a.action: a for a in observed.observation.actions}
    assert (by_action["retrieve"].attributable, by_action["retrieve"].anchored_hits) == (True, 2)
    assert (by_action["ppr"].attributable, by_action["ppr"].anchored_hits) == (True, 1)


def test_a_truncated_anchor_list_makes_the_whole_run_unattributable():
    """``anchor_ids_truncated`` 在场:答案锚点列表本身被协议上限截断过,
    「没命中」与「被截掉的尾巴本会命中」区分不开——整个 run 的每个动作都判
    False,不只是 synthesis 步自己。"""
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "retrieve",
                 "detail": {"count": 1, "result_ids": ["c-1"]}},
                {"step_type": "synthesis",
                 "detail": {"anchors": 999, "anchor_evidence_ids": ["c-1"],
                            "anchor_evidence_ids_truncated": True}},
            ]
        )
    )
    assert observed is not None
    retrieve = next(a for a in observed.observation.actions if a.action == "retrieve")
    assert (retrieve.attributable, retrieve.anchored_hits) == (False, 0)


def test_a_truncated_result_ids_list_poisons_only_that_action_not_the_whole_run():
    """修复轮 spec②:某个动作自己的 result_ids 被写侧截断(``result_ids_
    truncated``)时,只有**这个动作**在本 run 里 attributable=False——被截掉
    的尾巴里可能恰好是答案绑定的锚点,"没交上截断的那部分"与"确实没命中"
    分不清楚。没被截断的另一个动作不受连坐,镜像的是 pass 1 对锚点列表
    本身截断时"整个 run 判 False"的语义,但这里爆炸半径只到"这一个动作"。
    """
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "retrieve",
                 "detail": {"count": 21,
                            "result_ids": ["c-1"] * 20,
                            "result_ids_truncated": True}},
                {"step_type": "ppr",
                 "detail": {"count": 1, "result_ids": ["c-2"]}},
                {"step_type": "synthesis",
                 "detail": {"anchors": 1, "anchor_evidence_ids": ["c-2"]}},
            ]
        )
    )
    assert observed is not None
    by_action = {a.action: a for a in observed.observation.actions}
    assert (by_action["retrieve"].attributable,
           by_action["retrieve"].anchored_hits) == (False, 0)
    assert (by_action["ppr"].attributable,
           by_action["ppr"].anchored_hits) == (True, 1)


def test_a_run_whose_synthesis_step_was_dropped_by_the_step_limit_is_not_attributable():
    """不能依赖步序/步的存在——一个 run 的 trace 步数被
    ``RETRIEVAL_EXPERIENCE_BATCH_STEPS`` 截断,synthesis 步整个不在
    ``steps`` 里(``_run`` helper 之外、手工模拟"店家已经截过"的最终形状):
    没有可用的锚点集合,attributable 必须是 False,即使该动作自己带着
    result_ids。"""
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "retrieve",
                 "detail": {"count": 1, "result_ids": ["c-1"]}},
                # 无 synthesis/answer 步——store 的 step_limit 把它截掉了
            ]
        )
    )
    assert observed is not None
    retrieve = next(a for a in observed.observation.actions if a.action == "retrieve")
    assert (retrieve.attributable, retrieve.anchored_hits) == (False, 0)


def test_a_surviving_answer_step_with_no_anchor_key_is_not_a_usable_anchor_source():
    """修复轮 Q-P1-1:比上一条更精确的截尾常态形状——不是"synthesis/answer
    步整个不在 steps 里",而是**同名但不带锚点**的另一种 "answer" 步活了
    下来(reasoning_retrieval.py 自己的候选池汇总步,``summary="合成候选"``,
    detail 只有 kg/elements 计数,从来不写 anchor_evidence_ids),真正带
    ``anchor_evidence_ids`` 的那条 synthesis 步被 step_limit 切掉了。

    按 step_type 判定会把这条"answer"步误认成一个合法的(空)锚点来源,让
    这个 run 的每个动作的 anchored_hits 全部被算成 0——而真相是这条 run
    的锚点根本不可读,必须与"没有 synthesis/answer 步"同一判决:
    ``(False, 0)``。"""
    observed = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "retrieve",
                 "detail": {"count": 1, "result_ids": ["c-1"]}},
                # 候选池汇总的 "answer" 步——同名但从不携带 anchor_evidence_ids。
                {"step_type": "answer", "summary": "合成候选",
                 "detail": {"kg": 5, "elements": 3}},
                # 真正带锚点的 synthesis 步不在这里——模拟它被 step_limit 切掉。
            ]
        )
    )
    assert observed is not None
    retrieve = next(a for a in observed.observation.actions if a.action == "retrieve")
    assert (retrieve.attributable, retrieve.anchored_hits) == (False, 0)


def test_the_action_tuple_order_follows_the_vocabulary_not_the_run():
    """Two runs with the same actions must produce identical observations.

    The batch groups runs by their situation fingerprint and aggregates them;
    an observation whose tuple order depended on which step happened to run
    first would make the same batch distil differently between two reads.
    """
    forward = project_run(
        _run([_intent_step(),
              {"step_type": "ppr", "detail": {"count": 1}},
              {"step_type": "retrieve", "detail": {"count": 1}}])
    )
    backward = project_run(
        _run([_intent_step(),
              {"step_type": "retrieve", "detail": {"count": 1}},
              {"step_type": "ppr", "detail": {"count": 1}}])
    )
    assert forward.observation.actions == backward.observation.actions


def test_counts_become_bands_rather_than_exact_numbers():
    few = project_run(_run([_intent_step(entities=["a"])]))
    many = project_run(_run([_intent_step(entities=["a", "b", "c", "d"])]))
    none = project_run(_run([_intent_step(entities=[])]))
    assert few.observation.entity_count == "few"
    assert many.observation.entity_count == "many"
    assert none.observation.entity_count == "none"


# ------------------------------------------------------- situation registry

def test_every_registered_key_has_a_non_empty_domain():
    for key in SITUATION_KEYS:
        assert situation_domain(key), key


def test_validate_situation_rejects_an_unregistered_key_or_value():
    good = project_run(_run([_intent_step()])).observation.situation()
    assert validate_situation(good) == good
    assert validate_situation({**good, "extra": "x"}) is None
    assert validate_situation({k: v for k, v in good.items() if k != "mode"}) is None
    assert validate_situation({**good, "result_scope": "made-up"}) is None


def test_the_id_is_stable_and_order_independent():
    """Content addressing is what makes the cross-deployment union correct."""
    left = {"mode": "chunk", "result_scope": "ranked"}
    right = {"result_scope": "ranked", "mode": "chunk"}
    assert experience_id(left, "ppr") == experience_id(right, "ppr")
    assert experience_id(left, "ppr") != experience_id(left, "retrieve")


def test_similarity_is_a_plain_agreement_ratio():
    a = project_run(_run([_intent_step()])).observation.situation()
    assert situation_similarity(a, a) == 1.0
    assert situation_similarity(a, {**a, "mode": "chunk"}) == pytest.approx(
        (len(SITUATION_KEYS) - 1) / len(SITUATION_KEYS)
    )


# --------------------------------------------------------------- the run

class _Store:
    def __init__(self, existing=None):
        self.rows = list(existing or [])
        self.upserts = []
        self.evicted = 0
        self.reads = []
        self.evictions = []

    def read_partition(self, notebook_id, limit):
        self.reads.append((notebook_id, limit))
        return self.rows[:limit]

    def upsert_experience(self, experience_id, **kwargs):
        self.upserts.append((experience_id, kwargs))
        return {"id": experience_id}

    def evict_to_limit(self, max_entries, notebook_id=""):
        self.evicted += 1
        self.evictions.append((max_entries, notebook_id))
        return 0

    def count(self, notebook_id=None):
        return len(self.rows)


class _AskState:
    """取数替身。``notebook_id`` 不是摆设:``by_partition`` 里的键就是分区,
    没有键的分区返回空列表——「另一个库 0 行」那条用例靠的正是这个。"""

    def __init__(self, runs, by_partition=None):
        self.runs = runs
        self.by_partition = dict(by_partition or {})
        self.calls = []

    def recent_completed_ask_runs(self, *, job_limit, step_limit, notebook_id=None):
        self.calls.append((job_limit, step_limit, notebook_id))
        if notebook_id is None:
            return self.runs
        return self.by_partition.get(notebook_id, [])


class _Client:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def chat_json(self, messages, schema_hint, max_tokens=None):
        self.prompts.append(messages[0]["content"])
        return self.reply


class _Models:
    def __init__(self, reply, configured=True):
        self.client = _Client(reply)
        self._configured = configured

    def configured(self, workload):
        return self._configured

    def chat(self, workload):
        return self.client


class _Events:
    def __init__(self):
        self.emitted = []

    def emit(self, payload):
        self.emitted.append(payload)


class _Settings:
    retrieval_experience_enabled = True
    retrieval_experience_trigger = 3
    # 单库阈值与全局阈值**不相等**,是为了让「两个计数各自到各自的阈值」在每
    # 一条用例里都是可分辨的:相等的话,一次完成同时点燃两条链路的 bug 会与
    # 正确行为长得一模一样。
    retrieval_experience_notebook_trigger = 2


def _service(
    runs, reply, *, settings=None, store=None, models=None, events=None,
    ask_state=None,
):
    return RetrievalExperienceDistillationService(
        settings=settings or _Settings(),
        experiences=store or _Store(),
        ask_state=ask_state or _AskState(runs),
        models=models or _Models(reply),
        event_log=events or _Events(),
    )


_REPLY = (
    '{"entries":[{"op":"ADD","situation":"s0","action":"exact_lookup",'
    '"polarity":"bad","rationale":"这类问题里精查基本空手"}]}'
)


def test_a_batch_writes_the_entry_and_then_evicts():
    store = _Store()
    events = _Events()
    service = _service(
        [
            _run([_intent_step(),
                  {"step_type": "exact_lookup", "detail": {"found": 0}}],
                 run_id="job-1"),
            _run([_intent_step(),
                  {"step_type": "exact_lookup", "detail": {"found": 0}}],
                 run_id="job-2"),
        ],
        _REPLY,
        store=store,
        events=events,
    )
    service.run()
    assert len(store.upserts) == 1
    entry_id, kwargs = store.upserts[0]
    assert kwargs["action"] == "exact_lookup"
    assert kwargs["polarity"] == "bad"
    assert kwargs["provenance"] == ["job-1", "job-2"]
    assert kwargs["replace_conclusion"] is False
    assert entry_id == experience_id(kwargs["situation"], "exact_lookup")
    assert store.evicted == 1
    assert events.emitted[-1]["status"] == "done"


def test_the_event_carries_counts_only():
    """The event log is a separate disclosure surface from the table.

    A stream of (situation, action) pairs beside their timestamps would let an
    operator reconstruct which shapes of question the deployment is currently
    seeing — the one aggregate this feature is careful not to publish.
    """
    events = _Events()
    service = _service(
        [_run([_intent_step(), {"step_type": "exact_lookup", "detail": {"found": 0}}])],
        _REPLY,
        events=events,
    )
    service.run()
    payload = events.emitted[-1]
    # 2026-09-22:多了 ``partition``,而且是这条用例唯一允许的非计数字段——它只
    # 取 ``global``/``notebook`` 两个封闭值(``_partition_label``),绝不带库 id。
    assert set(payload) == {
        "kind", "status", "latency_ms", "partition",
        "runs", "situations", "written", "evicted",
    }
    assert payload["partition"] in {"global", "notebook"}
    assert all(isinstance(value, (str, int)) for value in payload.values())


def test_the_prompt_contains_no_question_text():
    """The distillation prompt's two blocks are counts and enum words only."""
    models = _Models(_REPLY)
    service = _service(
        [_run([_intent_step(), {"step_type": "exact_lookup", "detail": {"found": 0}}])],
        _REPLY,
        models=models,
    )
    service.run()
    prompt = models.client.prompts[0]
    assert "set_db" not in prompt
    assert "初检索" not in prompt
    assert "job-1" not in prompt


def test_the_kill_switch_costs_nothing():
    class Off(_Settings):
        retrieval_experience_enabled = False

    ask_state = _AskState([_run([_intent_step()])])
    models = _Models(_REPLY)
    service = RetrievalExperienceDistillationService(
        settings=Off(),
        experiences=_Store(),
        ask_state=ask_state,
        models=models,
        event_log=_Events(),
    )
    service.run()
    assert ask_state.calls == []
    assert models.client.prompts == []
    assert distillation_wiring_active(Off(), _Store()) is False


def test_an_unconfigured_model_is_learned_before_any_read():
    ask_state = _AskState([_run([_intent_step()])])
    service = RetrievalExperienceDistillationService(
        settings=_Settings(),
        experiences=_Store(),
        ask_state=ask_state,
        models=_Models(_REPLY, configured=False),
        event_log=_Events(),
    )
    service.run()
    assert ask_state.calls == []


def test_the_trigger_fires_only_at_the_threshold():
    store = _Store()
    service = RetrievalExperienceDistillationService(
        settings=_Settings(),
        experiences=store,
        ask_state=_AskState([]),
        models=_Models(_REPLY),
        event_log=_Events(),
    )
    started = []
    service._submit_claimed = (  # type: ignore[assignment]
        lambda partition, snapshot: started.append(snapshot)
    )
    service.note_ask_completed("")
    service.note_ask_completed("")
    assert started == []
    service.note_ask_completed("")
    assert started == [3]          # 快照=认领时的全部积压
    # 认领临界区已消费计数并占住单飞位(worker 被 stub、不会释放):
    # 下一次完成只计数、不重复排批。
    service.note_ask_completed("")
    assert started == [3]
    assert service._pending == 1


def test_the_trigger_never_raises_into_the_ask_path():
    class Boom:
        retrieval_experience_enabled = True

        @property
        def retrieval_experience_trigger(self):
            raise RuntimeError("settings blew up")

    service = RetrievalExperienceDistillationService(
        settings=Boom(),
        experiences=_Store(),
        ask_state=_AskState([]),
        models=_Models(_REPLY),
        event_log=_Events(),
    )
    service.note_ask_completed("")  # must not raise


def test_a_failing_run_releases_the_single_flight_flag():
    class Exploding(_Store):
        def read_partition(self, notebook_id, limit):
            raise RuntimeError("boom")

    events = _Events()
    service = _service(
        [_run([_intent_step(), {"step_type": "ppr", "detail": {"count": 0}}])],
        _REPLY,
        store=Exploding(),
        events=events,
    )
    # Simulate the claim ``start()`` takes before ever submitting ``run()`` —
    # without this the assertion below is vacuous, since ``_running`` starts
    # ``False`` and a gated release correctly leaves it there either way.
    service._running = True
    service.run()
    assert events.emitted[-1]["status"] == "failed"
    # A flag left set is held until the process dies — this deployment would
    # never distil again, silently.
    assert service._running is False


def test_a_claimed_run_releases_the_flag_on_success():
    """The mirror of the failure-path test above: a run that was genuinely
    claimed (``start()``'s pre-claim, simulated here) must still release the
    flag on the happy path."""
    service = _service(
        [_run([_intent_step(), {"step_type": "ppr", "detail": {"count": 0}}])],
        _REPLY,
    )
    service._running = True
    service.run()
    assert service._running is False


def test_an_unclaimed_call_never_touches_the_single_flight_flag():
    """Only ``start()`` may transition ``_running`` ``False -> True``. A call
    to ``run()`` that finds the flag already ``False`` at entry — every
    direct call in this test module, and the future manual "distil now"
    control the module docstring anticipates — must leave the flag alone in
    ``finally`` rather than unconditionally writing ``False``.

    A genuinely concurrent legitimate claim appearing WHILE this (unclaimed)
    call is still running is simulated by flipping the flag ``True`` from
    inside the store's read: an unconditional release in ``finally`` would
    clobber that other claim on its way out.
    """
    service = _service(
        [_run([_intent_step(), {"step_type": "ppr", "detail": {"count": 0}}])],
        _REPLY,
    )

    class ClaimingDuringRead(_Store):
        def read_partition(self, notebook_id, limit):
            service._running = True
            return super().read_partition(notebook_id, limit)

    service.experiences = ClaimingDuringRead()
    assert service._running is False
    service.run()
    assert service._running is True, (
        "run() released a slot it never claimed itself: this call started "
        "with _running already False, so any release in its `finally` can "
        "only be clobbering a claim that appeared while it was running"
    )


# ------------------------------------------------------- reply validation

def _groups():
    runs = [
        project_run(
            _run([_intent_step(), {"step_type": "ppr", "detail": {"count": 0}}],
                 run_id=f"job-{i}")
        )
        for i in range(3)
    ]
    return _group_by_situation(runs)


@pytest.mark.parametrize(
    "entry",
    [
        {"op": "ADD", "situation": "s9", "action": "ppr", "polarity": "bad",
         "rationale": "x"},
        {"op": "ADD", "situation": "s0", "action": "ppr_retrieve",
         "polarity": "bad", "rationale": "x"},
        {"op": "ADD", "situation": "s0", "action": "ppr", "polarity": "maybe",
         "rationale": "x"},
        {"op": "ADD", "situation": "s0", "action": "ppr", "polarity": "bad",
         "rationale": ""},
        {"op": "ADD", "situation": "s0", "action": "ppr", "polarity": "bad",
         "rationale": "x" * (RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS + 1)},
        {"op": "REPLACE", "situation": "s0", "action": "ppr", "polarity": "bad",
         "rationale": "x"},
        "not an object",
    ],
)
def test_a_malformed_entry_is_dropped(entry):
    """Per-entry rejection, never repair. ``ppr_retrieve`` is in this list on
    purpose: it is the reflect ACTION id, not the trace STEP type, and guessing
    that the model meant ``ppr`` is how an entry ends up about a channel nobody
    was writing about."""
    groups = _groups()
    assert parse_distillation_reply({"entries": [entry]}, groups) == []


def test_a_rationale_carrying_an_id_shaped_token_is_dropped():
    """A tripwire on the input narrowing, not a sanitiser.

    The model is fed no ids at all, so an id in its output means something got
    through upstream. Scrubbing the rationale would keep the entry and hide the
    failure.
    """
    groups = _groups()
    entry = {
        "op": "ADD", "situation": "s0", "action": "ppr", "polarity": "bad",
        "rationale": "别用 nb-797c0793d47640c283f4559145a39eb7 那种库",
    }
    assert parse_distillation_reply({"entries": [entry]}, groups) == []


def test_one_bad_entry_does_not_discard_its_sound_neighbours():
    groups = _groups()
    offered = [(0, {"action": "ppr", "situation": dict(groups[0].situation)})]
    parsed = parse_distillation_reply(
        {
            "entries": [
                {"op": "ADD", "situation": "s0", "action": "nonsense",
                 "polarity": "bad", "rationale": "x"},
                {"op": "UPDATE", "situation": "s0", "action": "ppr",
                 "polarity": "good", "rationale": "值得先试"},
            ]
        },
        groups,
        offered,
    )
    assert len(parsed) == 1
    assert parsed[0]["replace"] is True


def test_an_update_resolves_to_the_offered_entrys_own_identity():
    """codex #524 R2 P2:offered 条目的情境可能只是「相似」而非相同——UPDATE
    必须落在被展示那一行自己的身份上,否则旧结论留在库里、新结论另起一行,
    两条互相矛盾的打法都可被注入。"""
    groups = _groups()
    stored = dict(groups[0].situation)
    # 与 group 情境相差一键(相似但不同)的已存条目
    flipped_key = next(iter(stored))
    stored = {**stored, flipped_key: "unknown" if stored[flipped_key] != "unknown" else "none"}
    offered = [(0, {"action": "ppr", "situation": stored})]
    parsed = parse_distillation_reply(
        {"entries": [{"op": "UPDATE", "situation": "s0", "action": "ppr",
                      "polarity": "good", "rationale": "改判"}]},
        groups,
        offered,
    )
    assert len(parsed) == 1
    assert parsed[0]["replace"] is True
    assert parsed[0]["situation"] == stored          # 用的是已存行的身份
    assert parsed[0]["situation"] != dict(groups[0].situation)


def test_an_update_naming_an_unoffered_entry_downgrades_to_a_plain_add():
    groups = _groups()
    parsed = parse_distillation_reply(
        {"entries": [{"op": "UPDATE", "situation": "s0", "action": "ppr",
                      "polarity": "good", "rationale": "凭空更新"}]},
        groups,
        offered=(),
    )
    assert len(parsed) == 1
    assert parsed[0]["replace"] is False              # 不许改写从未展示过的行


def test_a_noop_writes_nothing():
    groups = _groups()
    assert parse_distillation_reply(
        {"entries": [{"op": "NOOP", "situation": "s0"}]}, groups
    ) == []


def test_a_reply_that_is_not_an_entries_list_is_discarded_whole():
    groups = _groups()
    assert parse_distillation_reply({"entries": "nope"}, groups) == []
    assert parse_distillation_reply(["entries"], groups) == []


def test_the_provenance_of_an_entry_is_every_run_in_its_group():
    groups = _groups()
    parsed = parse_distillation_reply(
        {"entries": [{"op": "ADD", "situation": "s0", "action": "ppr",
                      "polarity": "bad", "rationale": "空手"}]},
        groups,
    )
    assert parsed[0]["provenance"] == ["job-0", "job-1", "job-2"]


# ------------------------------------------------------------- aggregation

def test_grouping_is_deterministic_and_capped():
    runs = []
    for i in range(3):
        runs.append(project_run(_run([_intent_step()], run_id=f"a-{i}")))
    runs.append(project_run(_run([_intent_step(result_scope="aggregate")],
                                 run_id="b-0")))
    groups = _group_by_situation(runs)
    assert [g.runs for g in groups] == [3, 1]
    assert _group_by_situation(list(reversed(runs)))[0].key == groups[0].key


def test_renderers_emit_counts_and_vocabulary_only():
    groups = _groups()
    text = render_observations(groups)
    assert "s0:" in text
    assert "runs=3" in text
    assert "ppr: used=3 came_back_empty=3" in text
    assert render_existing([]).endswith("(none)")


def test_runs_with_actions_excludes_step_truncated_runs():
    """A run's trace steps can be truncated down to just its intent step
    (``RETRIEVAL_EXPERIENCE_BATCH_STEPS``). It still belongs to the situation
    — its question shape happened, so it counts toward ``runs`` — but it must
    NOT count toward the action denominator, or a busy shape with many
    step-truncated runs would make the action tallies read against an
    inflated ``runs`` number, understating how common each action really is
    among the runs that had anything to tally at all.
    """
    with_action = project_run(
        _run([_intent_step(), {"step_type": "ppr", "detail": {"count": 1}}],
             run_id="job-1")
    )
    truncated = project_run(_run([_intent_step()], run_id="job-2"))
    groups = _group_by_situation([with_action, truncated])
    assert len(groups) == 1
    group = groups[0]
    assert group.runs == 2
    assert group.runs_with_actions == 1


def test_renderers_show_the_action_denominator_alongside_total_runs():
    groups = _groups()
    text = render_observations(groups)
    # All three runs in _groups() carry a ppr step, so the two numbers agree
    # here — the point is that BOTH are present, not that they differ.
    assert "runs=3 (3 with sampled actions)" in text


# ------------------------------------- rendering: step→anchor attribution

def test_an_all_old_shape_batch_renders_byte_identical_to_pre_t4():
    """中性回归的硬验收点:一批全是老轨迹(没有一个 run 带 result_ids)时,
    ``render_observations`` 的输出必须与 T4 落地前**逐字节**相同——升级后
    第一批老轨迹的蒸馏 prompt 不该有任何变化。"""
    groups = _groups()
    text = render_observations(groups)
    assert text == (
        "[Recent searches, grouped by question shape]\n"
        "s0: completeness_required=no, entity_count=few, has_constraints=no, "
        "has_exclusions=no, mode=reasoning, result_scope=ranked, "
        "retrieval_effort=standard, topic_count=few\n"
        "  runs=3 (3 with sampled actions) total_citations=0\n"
        "  ppr: used=3 came_back_empty=3 (in 3 of 3 runs)"
    )
    assert "anchored=" not in text


def test_a_mixed_batch_only_shows_anchored_on_the_attributable_action():
    """混合批次(同一情境下,一部分 run 老轨迹、一部分带归因证据):
    只有真的能归因的动作(ppr)带 ``anchored=`` 子句,分母是 attributable
    run 数分之 runs_using;从未归因过的动作(retrieve)保持沉默,与升级前
    一样。"""
    old_ppr_a = project_run(
        _run([_intent_step(), {"step_type": "ppr", "detail": {"count": 0}}],
             run_id="mix-1")
    )
    old_ppr_b = project_run(
        _run([_intent_step(), {"step_type": "ppr", "detail": {"count": 0}}],
             run_id="mix-2")
    )
    new_ppr = project_run(
        _run(
            [
                _intent_step(),
                {"step_type": "ppr",
                 "detail": {"count": 2, "result_ids": ["x-1", "x-2"]}},
                {"step_type": "synthesis",
                 "detail": {"anchors": 2,
                            "anchor_evidence_ids": ["x-1", "z-9"]}},
            ],
            run_id="mix-3",
        )
    )
    old_retrieve = project_run(
        _run([_intent_step(), {"step_type": "retrieve", "detail": {"count": 5}}],
             run_id="mix-4")
    )
    groups = _group_by_situation([old_ppr_a, old_ppr_b, new_ppr, old_retrieve])
    assert len(groups) == 1, "四个 run 必须共享同一份默认 situation 才能同组"
    text = render_observations(groups)
    assert (
        "  ppr: used=3 came_back_empty=2 (in 3 of 4 runs) "
        "anchored=1 (attributable in 1 of 3 runs)" in text
    )
    assert "  retrieve: used=1 came_back_empty=0 (in 1 of 4 runs)" in text
    retrieve_line = next(
        line for line in text.splitlines() if line.strip().startswith("retrieve:")
    )
    assert "anchored=" not in retrieve_line


def test_completions_arriving_while_the_worker_is_busy_are_not_lost(monkeypatch):
    """codex #524 R1/R3 P2:单飞占用时**不消费**,阈值计数保留——认领与消费在
    同一临界区,busy 期间攒满的整批由下一次完成补触发,不丢也不重复。"""
    service = _service([], _REPLY)
    service._running = True          # 模拟在飞 worker 占住单飞位
    trigger = max(1, int(service.settings.retrieval_experience_trigger))
    for _ in range(trigger):
        service.note_ask_completed("")
    assert service._pending >= trigger   # 没被清零(修复前这里是 0)
    service._running = False
    submitted = []
    monkeypatch.setattr(
        service, "_submit_claimed",
        lambda partition, snapshot: submitted.append(snapshot),
    )
    service.note_ask_completed("")        # 下一次完成立刻补触发
    assert submitted == [trigger + 1]     # 整批(含 busy 期间的)一次消费
    assert service._pending == 0
    assert service._running is True       # 认领已在临界区内完成


def test_a_fast_worker_cannot_double_schedule_the_same_signals(monkeypatch):
    """codex #524 R3 P2:认领与消费同临界区——快 worker 在消费落地前释放槽位
    也不可能让并发完成看到「未消费的阈值」而排出第二个重复批次。"""
    service = _service([], _REPLY)
    submitted = []
    monkeypatch.setattr(
        service, "_submit_claimed",
        lambda partition, snapshot: submitted.append(snapshot),
    )
    trigger = max(1, int(service.settings.retrieval_experience_trigger))
    for _ in range(trigger):
        service.note_ask_completed("")
    assert submitted == [trigger]
    # 模拟快 worker 立刻结束释放槽位——此刻 pending 已在认领临界区被消费为 0
    service._running = False
    service.note_ask_completed("")        # 竞入的下一次完成
    assert submitted == [trigger]         # 不会重复排批
    assert service._pending == 1


def test_the_worker_release_rearms_a_full_pending_batch(monkeypatch):
    """codex #524 R4 P2:busy 期间攒满的整批,worker 退出时原子复查再排——
    突发流量停止后不永久滞留。"""
    service = _service([], _REPLY)
    submitted = []
    monkeypatch.setattr(
        service, "_submit_claimed",
        lambda partition, snapshot: submitted.append(snapshot),
    )
    trigger = max(1, int(service.settings.retrieval_experience_trigger))
    # 第一批:正常触发并占住槽位(stub 不会释放)
    for _ in range(trigger):
        service.note_ask_completed("")
    assert submitted == [trigger]
    # busy 期间又攒满一整批
    for _ in range(trigger):
        service.note_ask_completed("")
    assert service._pending == trigger
    # worker 退出:run() 的 finally 释放并复查
    service.run()
    assert submitted[-1] == trigger       # 积压被再排
    assert service._pending == 0


def test_offered_entries_are_unique_per_situation_and_action():
    """codex #524 R6 P2:同一 (sN, action) 只展示相似度最高的一条——两条相似
    旧条目共享同一标签时,UPDATE 的指认必然歧义,首个匹配可能污染另一条打法。"""
    groups = _groups()
    base = dict(groups[0].situation)
    keys = list(base)
    near_a = {**base, keys[0]: "unknown" if base[keys[0]] != "unknown" else "none"}
    near_b = {**base, keys[1]: "unknown" if base[keys[1]] != "unknown" else "none"}
    existing = [
        {"id": "rx-b", "action": "ppr", "situation": near_b},
        {"id": "rx-a", "action": "ppr", "situation": near_a},
        {"id": "rx-exact", "action": "ppr", "situation": dict(base)},
    ]
    offered = _offered_entries(groups, existing)
    ppr_under_zero = [e for i, e in offered if i == 0
                      and str(e.get("action")) == "ppr"]
    assert len(ppr_under_zero) == 1            # 唯一
    assert ppr_under_zero[0]["id"] == "rx-exact"  # 且是相似度最高的


def test_provenance_and_support_belong_only_to_runs_that_used_the_action():
    """codex #524 R9 P2:一个 run 反复调同一动作不是跨 run 模式——条目的
    provenance 只归属真用过该动作的 run,没用过的 run 不得虚增 support。"""
    ppr_retry = [{"step_type": "ppr", "detail": {"count": 0}}] * 5
    fetch = [{"step_type": "retrieve", "detail": {"count": 3}}]
    runs = [
        project_run(_run([_intent_step(), *ppr_retry], run_id="job-1")),
        project_run(_run([_intent_step(), *ppr_retry], run_id="job-1b")),
        project_run(_run([_intent_step(), *fetch], run_id="job-2")),
        project_run(_run([_intent_step(), *fetch], run_id="job-3")),
    ]
    groups = _group_by_situation(runs)
    assert groups[0].runs_for("ppr") == ["job-1", "job-1b"]
    assert set(groups[0].runs_for("retrieve")) == {"job-2", "job-3"}
    parsed = parse_distillation_reply(
        {"entries": [{"op": "ADD", "situation": "s0", "action": "ppr",
                      "polarity": "bad", "rationale": "老是空手"}]},
        groups,
    )
    assert parsed[0]["provenance"] == ["job-1", "job-1b"]


def test_the_grounding_signal_reads_anchors_never_the_fallback_cards():
    """codex #524 R9 P2:citations 是兜底卡数,零绑定回答里照样非零——接地
    信号必须取 anchors,缺 anchors 的旧行按 0(保守,不学假成功)。"""
    from app.domain.retrieval_experience import project_run_step

    grounded = project_run_step(
        {"step_type": "synthesis", "summary": "",
         "detail": {"citations": 9, "anchors": 4}}
    )
    assert grounded["count"] == 4
    ungrounded = project_run_step(
        {"step_type": "synthesis", "summary": "",
         "detail": {"citations": 9}}          # 兜底卡满、零绑定/旧行
    )
    assert ungrounded["count"] == 0


def test_an_entry_for_an_action_no_run_invoked_is_rejected():
    """codex #524 R10 P2:词表合法 ≠ 被观测过。本批没有任何 run 用过的动作,
    ADD/UPDATE 一律拒收——否则 support=0 的幻觉打法落库后照样可注入。"""
    groups = _groups()          # 三个 run,只用过 ppr
    entry = {"op": "ADD", "situation": "s0", "action": "retrieve",
             "polarity": "good", "rationale": "从未发生过的经验"}
    assert parse_distillation_reply({"entries": [entry]}, groups) == []
    update = dict(entry, op="UPDATE")
    offered = [(0, {"situation": groups[0].situation, "action": "retrieve",
                    "polarity": "bad", "rationale": "旧结论"})]
    assert parse_distillation_reply(
        {"entries": [update]}, groups, offered
    ) == []
    # 同批里真用过的动作照常通过,证明拒收不是把整个 parse 关掉
    ok = dict(entry, action="ppr")
    assert len(parse_distillation_reply({"entries": [ok]}, groups)) == 1


def test_offer_slots_are_consumed_after_dedup_not_before():
    """codex #524 R11 P2:三条排前的同动作条目只该占一个名额,低一名的
    别的动作条目要能顶上——先切片再去重会让名单欠额。"""
    groups = _groups()
    situation = dict(groups[0].situation)
    existing = [
        {"id": f"ppr-{i}", "situation": situation, "action": "ppr",
         "polarity": "bad", "rationale": "r"}
        for i in range(3)
    ] + [
        {"id": "ret-1", "situation": situation, "action": "retrieve",
         "polarity": "good", "rationale": "r"},
        {"id": "ex-1", "situation": situation, "action": "exact_lookup",
         "polarity": "good", "rationale": "r"},
    ]
    offered = _offered_entries(groups, existing)
    got = [entry["id"] for _index, entry in offered]
    # 每动作一条、名额 3:ppr 去重后只剩 1,retrieve/exact_lookup 顶上
    assert len(got) == 3
    assert len({entry["action"] for _i, entry in offered}) == 3


def test_the_experience_cache_never_serves_a_store_twin(monkeypatch):
    """codex #524 R11 P2:id() 可在旧 store 回收后被新对象复用,配上相同
    version signal 会跨库串缓存。真实 id 复用不可确定性复现,这里用模块级
    ``id`` 补丁把碰撞钉死:按 id 键的实现必然误命中(拿到 from-a),按弱引用
    身份的实现对不同活对象必然 miss 并重读。"""
    from app.services import reasoning_retrieval as rr

    class _Store:
        def __init__(self, rows):
            self.rows = rows
            self.reads = 0
        def version_signal(self):
            return (1, "2026-01-01T00:00:00")
        def read_partition(self, notebook_id, limit):
            self.reads += 1
            return list(self.rows)

    monkeypatch.setattr(rr, "id", lambda _obj: 42, raising=False)
    with rr._EXPERIENCE_CACHE_LOCK:
        rr._EXPERIENCE_CACHE.clear()
    a = _Store([{"id": "from-a"}])
    assert rr._cached_experiences(a) == [{"id": "from-a"}]
    b = _Store([{"id": "from-b"}])          # "id 碰撞"的孪生新库
    assert rr._cached_experiences(b) == [{"id": "from-b"}]
    assert b.reads == 1
    # 同一活对象、同签名:第二次命中缓存,不再读表
    assert rr._cached_experiences(b) == [{"id": "from-b"}]
    assert b.reads == 1
    with rr._EXPERIENCE_CACHE_LOCK:
        rr._EXPERIENCE_CACHE.clear()


def test_a_failed_upsert_still_evicts_the_overflow():
    """codex #524 R12 P2:每条 upsert 独立提交,批次中途炸掉若跳过驱逐,
    300 上限被突破后注入端按 id 只读前 300、任意遮蔽更好的条目。驱逐在
    finally,失败批也要收尾。"""

    class _ExplodingStore(_Store):
        def upsert_experience(self, experience_id, **kwargs):
            super().upsert_experience(experience_id, **kwargs)
            raise RuntimeError("db went away mid-batch")

    store = _ExplodingStore()
    events = _Events()
    service = _service(
        [
            _run([_intent_step(),
                  {"step_type": "exact_lookup", "detail": {"found": 0}}],
                 run_id="job-1"),
            _run([_intent_step(),
                  {"step_type": "exact_lookup", "detail": {"found": 0}}],
                 run_id="job-2"),
        ],
        _REPLY,
        store=store,
        events=events,
    )
    service.run()          # 失败被 run() 的 fail-open 吞掉,不冒泡
    assert len(store.upserts) == 1
    assert store.evicted == 1          # ← 批次失败,驱逐仍然跑了


def test_a_single_run_conclusion_is_rejected_but_a_real_update_is_not():
    """codex #524 R14 P2:「一个 run 从不构成模式」是服务端闸不是 prompt
    嘱咐——单 run 的 ADD 拒收;命中真实 offered 条目的 UPDATE 豁免到 ≥1
    (既有条目的历史 support 补齐了模式的另一半)。"""
    fetch = [{"step_type": "retrieve", "detail": {"count": 3}}]
    runs = [project_run(_run([_intent_step(), *fetch], run_id="only-run"))]
    groups = _group_by_situation(runs)
    add = {"op": "ADD", "situation": "s0", "action": "retrieve",
           "polarity": "good", "rationale": "只见过一次"}
    assert parse_distillation_reply({"entries": [add]}, groups) == []
    offered = [(0, {"situation": groups[0].situation, "action": "retrieve",
                    "polarity": "bad", "rationale": "旧结论"})]
    update = dict(add, op="UPDATE")
    parsed = parse_distillation_reply({"entries": [update]}, groups, offered)
    assert len(parsed) == 1 and parsed[0]["replace"] is True
    # 降级成 ADD 的 UPDATE(offered 里没有该动作)按 ADD 判,单 run 同样拒
    parsed = parse_distillation_reply({"entries": [update]}, groups, offered=[])
    assert parsed == []


def test_a_poisoned_action_contributes_zero_anchored_hits_to_aggregation():
    """codex #538 R1 P2:被截断 poison 的动作 anchored_hits 必须归零——留着
    保留前缀的交集,与别的可归因 run 同组聚合时会被计成有效成功。"""
    truncated_run = _run([
        _intent_step(),
        {"step_type": "ppr",
         "detail": {"count": 2, "result_ids": ["c1", "c2"],
                    "result_ids_truncated": True}},
        {"step_type": "synthesis", "summary": "",
         "detail": {"citations": 1, "anchors": 1,
                    "anchor_evidence_ids": ["c1"]}},
    ], run_id="poisoned")
    observed = project_run(truncated_run)
    ppr = next(a for a in observed.observation.actions if a.action == "ppr")
    assert ppr.attributable is False
    assert ppr.anchored_hits == 0, "poison 动作的保留前缀交集不得计入分子"


# ============================================================================
# 按笔记本分区(2026-09-22):两条链路,一个单飞槽位,一条有界去重队列
# ============================================================================

class _NotebooksOnlySettings:
    """只让单库链路动的阈值组合:全局阈值高到这段流量永远够不着。

    队列那两条用例要数的是「几个**库**排进了等待室」,而全局分区一旦也掺进来,
    它会被丢掉、又在下一次完成时重新入队,把一个本该是算术的断言变成一个要跟着
    实现细节改的数字。"""

    retrieval_experience_enabled = True
    retrieval_experience_trigger = 10_000
    retrieval_experience_notebook_trigger = 2


class _PartitionSettings:
    """真实默认值的形状:全局 40、单库 10(规格 §9)。"""

    retrieval_experience_enabled = True
    retrieval_experience_trigger = 40
    retrieval_experience_notebook_trigger = 10


def _zero_hit_run(run_id: str) -> dict:
    return _run(
        [_intent_step(), {"step_type": "exact_lookup", "detail": {"found": 0}}],
        run_id=run_id,
    )


def _inline_submit(monkeypatch) -> list:
    """让 ``background_jobs.submit`` 在调用线程里同步跑,并记下 (args, name)。"""
    calls: list = []

    def _submit(fn, *args, name=None, notify_pending=False, **kwargs):
        calls.append((args, name))
        fn(*args, **kwargs)
        return None

    monkeypatch.setattr(background_jobs, "submit", _submit)
    return calls


def test_ten_asks_in_one_library_distil_that_library_and_sample_only_it(monkeypatch):
    """验收判据 2。一个库连续 10 次 reasoning 完成 → 一批 ``partition="notebook"``
    的蒸馏,取数只看这个库,写入落这个库的分区,淘汰按 100 收容。

    全局链路在同一段流量里**不该**跑:10 < 40,而两个计数各走各的。
    """
    submits = _inline_submit(monkeypatch)
    store = _Store()
    events = _Events()
    ask_state = _AskState([], by_partition={"nb-a": [
        _zero_hit_run("job-1"), _zero_hit_run("job-2"),
    ]})
    service = _service(
        [], _REPLY, settings=_PartitionSettings(), store=store, events=events,
        ask_state=ask_state,
    )

    for _ in range(9):
        service.note_ask_completed("nb-a")
    assert submits == [], "阈值未到就排批 = 每次提问一次 LLM"
    service.note_ask_completed("nb-a")

    assert [name for _args, name in submits] == ["retrievalexperience-notebook"]
    assert [args for args, _name in submits] == [("nb-a",)]
    # 取数带谓词,而且只带这一个库;两个上限都按分区选。
    assert ask_state.calls == [
        (RETRIEVAL_EXPERIENCE_BATCH_RUNS, RETRIEVAL_EXPERIENCE_BATCH_STEPS, "nb-a")
    ]
    assert store.reads == [("nb-a", RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES)]
    assert store.evictions == [(RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES, "nb-a")]
    # 写入落该库分区,id 与列同源。
    entry_id, kwargs = store.upserts[0]
    assert kwargs["notebook_id"] == "nb-a"
    assert entry_id == experience_id(
        kwargs["situation"], kwargs["action"], partition="nb-a"
    )
    assert entry_id != experience_id(kwargs["situation"], kwargs["action"])
    done = events.emitted[-1]
    assert (done["status"], done["partition"], done["written"]) == ("done", "notebook", 1)
    # 单库计数已结算,全局计数原封不动地继续攒。
    assert service._notebook_pending == {}
    assert service._pending == 10


def test_another_library_reaching_its_threshold_sees_none_of_the_first_ones_runs(
    monkeypatch,
):
    """验收判据 2 的另一半:B 库攒够 10 次,取数只问 B——而 B 没有 run,所以这
    一批写 0 行。隔离在**取数**这一层就成立,不是靠事后过滤。"""
    _inline_submit(monkeypatch)
    store = _Store()
    events = _Events()
    ask_state = _AskState([], by_partition={"nb-a": [
        _zero_hit_run("job-1"), _zero_hit_run("job-2"),
    ]})
    service = _service(
        [], _REPLY, settings=_PartitionSettings(), store=store, events=events,
        ask_state=ask_state,
    )

    for _ in range(10):
        service.note_ask_completed("nb-b")

    assert ask_state.calls == [
        (RETRIEVAL_EXPERIENCE_BATCH_RUNS, RETRIEVAL_EXPERIENCE_BATCH_STEPS, "nb-b")
    ]
    assert store.upserts == []
    done = events.emitted[-1]
    assert (done["partition"], done["runs"], done["written"]) == ("notebook", 0, 0)


def test_the_global_chain_still_fires_at_forty_with_no_predicate(monkeypatch):
    """验收判据 9 / 规格 §4:全局链路一个字都没变——40 次(跨库)一批,取数
    **不传** notebook_id,既有条目与淘汰都按 300。"""
    _inline_submit(monkeypatch)
    store = _Store()
    events = _Events()
    ask_state = _AskState([_zero_hit_run("job-1"), _zero_hit_run("job-2")])
    service = _service(
        [], _REPLY, settings=_PartitionSettings(), store=store, events=events,
        ask_state=ask_state,
    )

    # 40 次分散在 8 个库上:每个库只有 5 次(< 10),只有全局计数到 40。
    for index in range(40):
        service.note_ask_completed(f"nb-{index % 8}")
        if index < 39:
            assert store.upserts == [], index

    assert ask_state.calls == [
        (RETRIEVAL_EXPERIENCE_BATCH_RUNS, RETRIEVAL_EXPERIENCE_BATCH_STEPS, None)
    ]
    assert store.reads == [("", RETRIEVAL_EXPERIENCE_MAX_ENTRIES)]
    assert store.evictions == [(RETRIEVAL_EXPERIENCE_MAX_ENTRIES, "")]
    entry_id, kwargs = store.upserts[0]
    assert kwargs["notebook_id"] == ""
    # 全局分区的 id 与分区落地**之前**逐字相同(规格 §12 偏离 ④)。
    assert entry_id == experience_id(kwargs["situation"], kwargs["action"])
    assert events.emitted[-1]["partition"] == "global"


def test_a_library_that_crosses_its_threshold_twice_waits_in_line_only_once():
    """队列去重。同一个库排两批毫无意义——第二批会读到几乎同一批 run,再花一次
    模型调用重新推导刚写下的结论(provenance 去重挡得住 support 虚增,挡不住
    那次调用的账单)。

    顺带钉住全局分区**不进队列**:它攒到阈值只是让它在下一次认领时优先,队列
    里从头到尾只有笔记本分区。
    """
    service = _service([], _REPLY)        # 全局 3、单库 2
    service._running = True               # 单飞位被占,谁也跑不了

    for _ in range(6):
        service.note_ask_completed("nb-a")

    assert list(service._queue) == ["nb-a"]
    assert service._queued == {"nb-a"}
    # 计数在出队时才结算,所以两边都还完整地留着。
    assert service._pending == 6
    assert service._notebook_pending == {"nb-a": 6}


def test_a_full_waiting_room_refuses_the_newcomer_and_keeps_everyone_in_line():
    """有界队列:满了**拒绝新来的**,绝不赶走已经排上的(T2 评审轮 P1)。

    先前的写法从队头丢——而队头正是认领要取的那一位,于是持续压力下等待室
    只是在churn,谁也排不到前面,还每拒一次发一条事件。拒绝新来的则什么都不
    浪费:它的计数一动不动,下一趟 rearm 或它的下一次提问会再offer一次。
    """
    events = _Events()
    service = _service([], _REPLY, events=events, settings=_NotebooksOnlySettings())
    service._running = True

    libraries = _MAX_QUEUED_PARTITIONS + 5
    for index in range(libraries):
        for _ in range(2):                 # 单库阈值 2
            service.note_ask_completed(f"nb-{index:03d}")

    assert len(service._queue) == _MAX_QUEUED_PARTITIONS
    assert list(service._queue)[0] == "nb-000", "最先排上的必须还在队头"
    # 进不来的每个库各被拒一次,事件只有数量。
    refusals = [event for event in events.emitted if event.get("reason") == "queue_full"]
    assert len(refusals) == libraries - _MAX_QUEUED_PARTITIONS
    for event in refusals:
        assert event["dropped"] == 1
        assert event["partition"] == "notebook"
        assert "nb-" not in repr(event), event
    # 被拒的库计数原封不动。
    assert service._notebook_pending[f"nb-{libraries - 1:03d}"] == 2


def test_a_library_refused_over_and_over_is_reported_once_per_saturated_period():
    """饱和期的事件上界:**每个库每个饱和期一条**,不是每次提问一条
    (T2 评审轮 2,P2;实测 20000 次提问发了 12830 条,每条还带一次文件追加
    和一条 WARNING)。

    「被拒」是容量信号,不是事件流:同一个库在等待室满着的时候每完成一次提问
    都会被拒,但那说的还是同一件事。标记在它**真的拿到批次**时清掉——所以
    第二个饱和期会有第二条。
    """
    events = _Events()
    service = _service([], _REPLY, events=events, settings=_NotebooksOnlySettings())
    service._running = True

    for index in range(_MAX_QUEUED_PARTITIONS):
        for _ in range(2):
            service.note_ask_completed(f"nb-{index:03d}")
    assert len(service._queue) == _MAX_QUEUED_PARTITIONS
    baseline = len(events.emitted)

    # 一个排不进去的库连续五次完成提问:五次被拒,一条事件。
    for _ in range(5):
        service.note_ask_completed("late-comer")
    refusals = [
        event for event in events.emitted[baseline:]
        if event.get("reason") == "queue_full"
    ]
    assert len(refusals) == 1, refusals
    assert refusals[0]["dropped"] == 1
    assert service._notebook_pending["late-comer"] == 5   # 积压一次没丢

    # 它终于拿到批次(手工腾位 + 排队 + 认领),标记清掉;下一个饱和期重新
    # 报一条。
    service._queue.clear()
    service._queued.clear()
    service._running = False
    assert service._enqueue_locked("late-comer") is True
    assert "late-comer" in service._refused, "入队本身不清标记,认领才清"
    claimed = service._claim_next_locked()
    assert claimed == ("late-comer", 5)
    assert "late-comer" not in service._refused
    for index in range(_MAX_QUEUED_PARTITIONS):
        for _ in range(2):
            service.note_ask_completed(f"nb-{index:03d}")
    before_second = len(events.emitted)
    for _ in range(3):
        service.note_ask_completed("late-comer")
    second = [
        event for event in events.emitted[before_second:]
        if event.get("reason") == "queue_full"
    ]
    assert len(second) == 1, second


def test_a_crowded_waiting_room_never_starves_the_global_chain(monkeypatch):
    """T2 评审轮 P1 的验收用例:评审实测 300 个就绪库时全局分区 300 个周期被
    认领 0 次、并产生四万多条事件。

    这里跑一遍完整的轮转:上百个库同时就绪 + 全局也就绪,反复「worker 退出 →
    接下一批」。三条性质缺一不可——全局立刻轮到(它有自己的座位,不排队)、
    每个库最终各跑一次(拒绝不丢批)、事件是**聚合**的(每趟至多一条)。
    """
    events = _Events()
    service = _service([], _REPLY, events=events)   # 全局 3、单库 2
    service._running = True

    libraries = 150
    for index in range(libraries):
        for _ in range(2):
            service.note_ask_completed(f"nb-{index:03d}")

    assert "" not in service._queued
    assert service._pending == libraries * 2        # 全局早已就绪
    seeded_events = len(events.emitted)

    submitted: list[str] = []
    monkeypatch.setattr(
        service, "_submit_claimed",
        lambda partition, snapshot: submitted.append(partition),
    )
    longest_queue = len(service._queue)
    cycles = libraries + 10
    for _ in range(cycles):
        service._running = False            # 上一批退出
        service._maybe_requeue()            # run() 的 finally 做的正是这件事
        longest_queue = max(longest_queue, len(service._queue))

    assert submitted[0] == "", "全局分区必须在第一个周期就被认领"
    assert submitted.count("") == 1, "它只就绪过一次,不该被重复认领"
    assert sorted(submitted) == sorted(
        [""] + [f"nb-{index:03d}" for index in range(libraries)]
    ), "每个库都必须恰好跑到一次"
    assert longest_queue <= _MAX_QUEUED_PARTITIONS
    # 事件是聚合的:整轮转下来最多一趟一条,而不是一个被拒的库一条。
    drain_events = len(events.emitted) - seeded_events
    assert drain_events <= cycles, drain_events
    assert service._notebook_pending == {}


def test_under_saturation_the_global_seat_yields_every_other_batch(monkeypatch):
    """T2 评审轮 2 的 P2:优先座位在**吞吐跟不上到达**时会把笔记本链挤到零。

    仿真的正是那个条件——每 40 次提问只跑得完一批,也就是全局计数在每一次
    槽位空出来时都已经过阈值。严格优先下这一格是 200 个库 0 批(评审实测);
    弱让位(上一批是全局、且有库在等 → 让一位)把它变成交替:笔记本至少拿到
    全局批次数的一半,而全局一次都没有被饿到。

    宽松条件下不受影响那一半由别的用例钉(队列空时全局照样立刻认领)。
    """
    events = _Events()
    service = _service([], _REPLY, settings=_PartitionSettings(), events=events)
    claimed: list[str] = []
    monkeypatch.setattr(
        service, "_submit_claimed",
        lambda partition, snapshot: claimed.append(partition),
    )

    libraries = 4          # 4 个库 × 每轮 10 次 = 每轮 40 次到达 = 全局阈值
    rounds = 40
    longest_queue = 0
    for _ in range(rounds):
        service._running = True                    # 上一批还在跑
        for index in range(libraries):
            for _ in range(10):                    # 单库阈值 10
                service.note_ask_completed(f"nb-{index}")
        longest_queue = max(longest_queue, len(service._queue))
        service._running = False                   # 那一批退出
        service._maybe_requeue()                   # 只接下一批

    assert len(claimed) == rounds, claimed
    global_batches = claimed.count("")
    notebook_batches = len(claimed) - global_batches
    assert global_batches >= 1, "全局链路仍然必须跑得到"
    assert notebook_batches >= global_batches / 2, (global_batches, notebook_batches)
    # 交替的直接证据:没有任何两批连着都是全局的。
    assert not any(
        left == "" and right == ""
        for left, right in zip(claimed, claimed[1:])
    ), claimed
    assert longest_queue <= _MAX_QUEUED_PARTITIONS


def test_a_notebook_batch_earned_while_busy_runs_when_the_slot_frees(monkeypatch):
    """busy 期间攒满不丢——分区版的 codex #524 R4 P2。突发流量停止后,等待队列
    里的分区由退出的 worker 在 ``finally`` 里接上,而不是等下一次提问。"""
    service = _service([], _REPLY)
    service._running = True
    for _ in range(2):
        service.note_ask_completed("nb-a")
    assert list(service._queue) == ["nb-a"]

    submitted = []
    monkeypatch.setattr(
        service, "_submit_claimed",
        lambda partition, snapshot: submitted.append((partition, snapshot)),
    )
    service.run()                      # 占着槽位的那一批退出

    assert submitted == [("nb-a", 2)]
    assert service._notebook_pending == {}   # 出队即结算
    assert list(service._queue) == []


def test_distill_now_is_refused_while_the_feature_is_off_and_costs_nothing():
    """规格 §4/§8:手动触发**自己**过总闸——``start()`` 的 docstring 一直写着
    共享入口不自闸。关闭态零查询、零模型调用、不占槽位。"""

    class Off(_Settings):
        retrieval_experience_enabled = False

    ask_state = _AskState([_run([_intent_step()])])
    models = _Models(_REPLY)
    service = RetrievalExperienceDistillationService(
        settings=Off(),
        experiences=_Store(),
        ask_state=ask_state,
        models=models,
        event_log=_Events(),
    )

    assert service.distill_now("nb-a") == MANUAL_DISTILL_DISABLED
    assert ask_state.calls == []
    assert models.client.prompts == []
    assert service._running is False


def test_distill_now_claims_the_slot_once_and_then_reports_busy(monkeypatch):
    """第二次按钮不是第二个写者:单飞槽位被占时回 ``busy``,而不是排第二批。"""
    submitted = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs:
            submitted.append((args, name)),
    )
    service = _service([], _REPLY)

    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED
    assert submitted == [(("nb-a",), "retrievalexperience-notebook")]
    assert service.distill_now("nb-a") == MANUAL_DISTILL_BUSY
    assert len(submitted) == 1
    # 手动触发不消费任何分区的积压:它是**额外**的一批,不是那个库攒的那一批。
    assert service._pending == 0
    assert service._notebook_pending == {}


def test_distill_now_refuses_a_missing_partition_rather_than_running_the_global_one():
    """T2 评审轮 P3:按钮挂在**一个笔记本**上,空 id 是「id 在半路丢了」的症状,
    不是「跑全局那一批」的功能。退回全局会成功、会计费、还会往全体可见的分区
    里写条目——一个看起来完全正常的错误答案。"""
    events = _Events()
    service = _service([], _REPLY, events=events)

    assert service.distill_now("") == MANUAL_DISTILL_INVALID
    assert service.distill_now(None) == MANUAL_DISTILL_INVALID
    assert service.distill_now(12345) == MANUAL_DISTILL_INVALID

    assert service._running is False, "被拒的调用不得占住单飞槽位"
    assert [event["reason"] for event in events.emitted] == ["invalid_partition"] * 3
    for event in events.emitted:
        assert event["partition"] == "notebook"
        assert "12345" not in repr(event), "事件不带那个不可信的值"


def test_distill_now_cools_down_after_a_finished_batch_with_no_new_asks(monkeypatch):
    """按钮不是「每按一次买一次模型调用」。

    一批跑完之后、这个库又没有新的提问,再按一次只会把同一批 run 重读一遍、
    什么也写不出来——却照付一次有界模型调用。冷却期内直接拒,并记一条
    ``reason="cooldown"`` 的 skipped 事件(只带封闭的分区词,不带 id)。
    """
    submitted = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs:
            submitted.append(args),
    )
    events = _Events()
    service = _service([], _REPLY, events=events)

    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED
    service.run("nb-a")                     # 那一批跑完(假 submit 没真的起线程)
    assert service._running is False
    events.emitted.clear()

    assert service.distill_now("nb-a") == MANUAL_DISTILL_COOLDOWN
    assert len(submitted) == 1, "冷却期内不得再排一批"
    assert [event["reason"] for event in events.emitted] == ["cooldown"]
    assert events.emitted[0]["partition"] == "notebook"
    assert "nb-a" not in repr(events.emitted[0]), "事件不带分区 id"

    # 冷却是**这个库**自己的:别的库照按不误。
    assert service.distill_now("nb-b") == MANUAL_DISTILL_STARTED


def test_pending_asks_override_the_cooldown(monkeypatch):
    """有新提问就一定放行——冷却挡的是「没有新输入还要付钱」,不是「刚跑过」。

    两个条件的组合方向是 pending **压过** 冷却,不是叠加:一个库刚跑完一批、
    随后又来了一次提问,那一次提问正是新的一批该看的东西。
    """
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs: None,
    )
    service = _service([], _REPLY)

    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED
    service.run("nb-a")
    assert service.distill_now("nb-a") == MANUAL_DISTILL_COOLDOWN

    service.note_ask_completed("nb-a")      # 一次新提问,远不到阈值
    assert service._notebook_pending["nb-a"] == 1
    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED


def test_a_manual_batch_settles_the_backlog_so_the_cooldown_can_bite(monkeypatch):
    """codex #771 R1 P2:不消费积压时冷却形同虚设。

    ``start()`` 刻意不动计数,而冷却的判据是「pending 为 0」。两条规则撞在一起,
    一次**低于阈值**的提问就把 pending 永远钉在 1,于是每一次点击都压过冷却、
    对同一批输入再买一次模型调用。手动认领因此要像自动路径一样结清这个库的账。
    """
    submitted = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs:
            submitted.append(args),
    )
    service = _service([], _REPLY)

    service.note_ask_completed("nb-a")                 # 一次提问,远不到阈值 10
    assert service._notebook_pending["nb-a"] == 1

    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED
    # 认领即结清:这一批看的正是那一次提问。
    assert "nb-a" not in service._notebook_pending
    service.run("nb-a")

    # 立刻再点 —— 这一次冷却真的挡住了(修复前这里是 STARTED,第二次模型调用)
    assert service.distill_now("nb-a") == MANUAL_DISTILL_COOLDOWN
    assert len(submitted) == 1

    # 又来一次提问 → 有新输入,放行。
    service.note_ask_completed("nb-a")
    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED
    assert len(submitted) == 2


def test_a_manual_claim_takes_the_partition_out_of_the_waiting_room(monkeypatch):
    """手动认领要顺带摘掉等待室里的那张号。

    留着它,这一批跑完之后 ``_maybe_requeue`` 会立刻再排一批——同一个库、几乎
    完全重叠的样本、零积压,正是等待室去重要防的那件事。
    """
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs: None,
    )
    service = _service([], _REPLY)
    # 先占住槽位,让 nb-a 攒够阈值后只能排队。
    service.start("")
    for _ in range(10):
        service.note_ask_completed("nb-a")
    assert list(service._queue) == ["nb-a"]

    # 释放槽位但不走 requeue,模拟「队里还排着 nb-a 时用户按了按钮」。
    with service._lock:
        service._running = False
        service._running_partition = None

    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED
    assert list(service._queue) == []
    assert "nb-a" not in service._queued
    assert "nb-a" not in service._notebook_pending


def test_is_active_reports_the_running_and_the_queued_partition(monkeypatch):
    """``is_active`` 是清空端点唯一的判据:正在跑、或排着队,都算「在动」。"""
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs: None,
    )
    service = _service([], _REPLY)
    assert service.is_active("nb-a") is False

    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED
    assert service.is_active("nb-a") is True
    # 只对**那一个**分区为真:别的库、以及全局分区,都不受这一批影响。
    assert service.is_active("nb-b") is False
    assert service.is_active("") is False

    # 排队中的分区同样算「在动」——它马上就会拿到槽位并写回这个分区。
    for _ in range(10):
        service.note_ask_completed("nb-b")
    assert list(service._queue) == ["nb-b"]
    assert service.is_active("nb-b") is True

    service.run("nb-a")                      # 那一批跑完,槽位与队列都被结算
    assert service.is_active("nb-a") is False


def test_a_restart_leaves_the_button_usable(monkeypatch):
    """§13-Q4 的兜底场景:重启后 pending 归零,但 ``_last_finished`` 也是空的。

    空字典必须读成「没冷却」而不是「很久以前跑过」——否则这个按钮恰恰在它唯一
    存在理由(重启丢了计数)的那一刻失灵。
    """
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs: None,
    )
    service = _service([], _REPLY)          # 全新进程的形态:两个字典都是空的

    assert service._notebook_pending == {}
    assert service._last_finished == {}
    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED


def test_the_cooldown_expires_on_the_monotonic_clock(monkeypatch):
    """冷却是**时长**,量在单调钟上——墙钟被回拨不会把按钮冻住。"""
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, notify_pending=False, **kwargs: None,
    )
    service = _service([], _REPLY)
    service.run("nb-a")
    assert service.distill_now("nb-a") == MANUAL_DISTILL_COOLDOWN

    # 把那条记录推到冷却窗口之外(等价于「过了十分钟」),不睡真的时间。
    service._last_finished["nb-a"] -= _MANUAL_DISTILL_COOLDOWN_SECONDS + 1
    assert service.distill_now("nb-a") == MANUAL_DISTILL_STARTED


def test_the_kill_switch_stops_the_counters_before_they_move():
    """规格评审 Finding 2:``RETRIEVAL_EXPERIENCE_ENABLED=false`` 时**零计数**
    (验收判据 1),不只是零查询——早退必须发生在 ``_pending += 1`` 之前,否则
    关着的特性照样在进程内攒状态,一旦被打开就立刻排出一批凭空的蒸馏。"""

    class Off(_Settings):
        retrieval_experience_enabled = False

    service = _service([], _REPLY, settings=Off())
    for _ in range(50):
        service.note_ask_completed("nb-a")

    assert service._pending == 0
    assert service._notebook_pending == {}
    assert list(service._queue) == []
    assert service._running is False
