"""跨库证据选择,以及作业层对「引用冻结」与「逐库回执」的把关。

D1-4 之后这里删掉了三条旧流水线专属的用例,理由各自不同,都不是「让它绿」:

  · ``test_changed_later_element_rebuilds_only_from_surviving_notebook``
    —— 失效证据剔除后**重新合成一次**这个补救动作被刻意收窄成整份作废(见
    计划 1.8):再调一次引擎会重跑全部检索、重新冻结来源,语义上不是同一份
    答案。替代用例是 ``test_global_ask_engine_parity.py`` 的
    ``test_changed_evidence_returns_retry_copy``。
  · ``test_revocation_during_validation_prevents_second_model_call``
    —— 「第二次模型调用」在新引擎下不存在,这条断言恒真。
  · ``test_total_retrieval_budget_discloses_unsearched_remaining_notebooks``
    —— 阶段时限与 ``queue_deadline`` 搬进了联邦通道,由
    ``tests/test_federated_global_budget.py`` 在它真正的宿主上钉。

D1-7 又删掉了三条:``test_context_reserves_other_library_before_admitting_near_budget_chunk``、
``test_context_does_not_replace_a_fitting_best_passage_with_a_weaker_tail``、
``test_shared_answer_retry_recovers_empty_model_response``——三条钉的都是旧独立
流水线合成阶段(其 ``_context``/``answer_with_retry``)自己的跨库预算分配与重试
逻辑,而那一整个模块随退役的旧独立流水线一起删除(零生产调用方,合成早已并入
``AskService`` 单引擎)。``chunk()`` 辅助函数本身与那个模块无关(其余
``peer_evidence`` 用例一直在用它构造纯 ``RetrievedChunk``),因此原地保留,不再
从那个已删模块的测试文件借道导入。
"""
from dataclasses import replace
from threading import Event

from app.domain.retrieval import RetrievedChunk
from app.models.ask import Citation
from app.models.global_ask import GlobalAskRequest
from app.services.global_evidence import peer_evidence
from tests.test_global_ask import setup, finished, _LibraryUnavailable


def chunk(suffix="a", *, text="原文完整内容。", notebook_id="notebook-a"):
    return RetrievedChunk(
        chunk_id=f"chunk-{suffix}", source_id=f"source-{suffix}",
        source_title=f"研究 {suffix}", section_path="结果 / 低温",
        text=text, element_ids=[f"element-{suffix}"], notebook_id=notebook_id,
    )


def test_twenty_four_libraries_get_first_round_before_dominant_scores_repeat():
    pools = [[replace(chunk(f"{nb}-{i}", text=f"evidence {nb} {i}", notebook_id=f"nb-{nb}"),
                      relevance=1000 if nb == 0 else 0.01) for i in range(30)] for nb in range(24)]
    selected = peer_evidence(pools, 48)
    assert len(selected) == 48
    assert all(sum(hit.notebook_id == f"nb-{nb}" for hit in selected) == 2 for nb in range(24))


def test_duplicate_text_does_not_consume_other_library_unique_slot():
    selected = peer_evidence([
        [chunk("a", text="same", notebook_id="a")],
        [chunk("b", text="same", notebook_id="b"), chunk("b2", text="different", notebook_id="b")],
    ], 2)
    assert [hit.text for hit in selected] == ["same", "different"]


def test_focused_question_does_not_reserve_slots_for_noise_libraries():
    strong = [replace(chunk(f"a-{i}", text=f"relevant {i}", notebook_id="a"), relevance=0.9 - i * 0.01)
              for i in range(12)]
    noise = [[replace(chunk(f"n-{i}", text=f"weak {i}", notebook_id=f"noise-{i}"), relevance=0.13)]
             for i in range(31)]
    selected = peer_evidence([strong, *noise], 12, min_relevance=0.25, relative_relevance=0.6)
    assert selected == strong


def test_library_reservation_keeps_relevant_minority_but_not_its_weak_tail():
    strong = [replace(chunk(f"a-{i}", text=f"strong {i}", notebook_id="a"), relevance=0.9)
              for i in range(10)]
    minority = [replace(chunk("b", text="minority", notebook_id="b"), relevance=0.5),
                replace(chunk("weak", text="weak tail", notebook_id="b"), relevance=0.26)]
    selected = peer_evidence([strong, minority], 6, min_relevance=0.25, relative_relevance=0.6)
    assert selected[:2] == [strong[0], minority[0]]
    assert sum(hit.notebook_id == "a" for hit in selected) == 5
    assert minority[1] not in selected


def test_remaining_capacity_prefers_local_confidence_over_equal_turns():
    sustained = [replace(chunk(f"a-{i}", text=f"sustained {i}", notebook_id="a"), relevance=0.9)
                 for i in range(3)]
    falling = [replace(chunk("b0", text="b best", notebook_id="b"), relevance=0.9),
               replace(chunk("b1", text="b tail", notebook_id="b"), relevance=0.4)]
    selected = peer_evidence([sustained, falling], 4, min_relevance=0.25, relative_relevance=0.4)
    assert selected == [sustained[0], falling[0], sustained[1], sustained[2]]


def _one_strong_and_n_noise(noise_count=7):
    strong = [replace(chunk(f"a-{i}", text=f"relevant {i}", notebook_id="a"), relevance=0.9 - i * 0.001)
              for i in range(40)]
    noise = [[replace(chunk(f"n{n}-0", text=f"unrelated {n}", notebook_id=f"n{n}"), relevance=0.05)]
             for n in range(noise_count)]
    return strong, noise


def test_peer_floor_denies_reserved_slots_to_libraries_far_below_the_best():
    strong, noise = _one_strong_and_n_noise()
    selected = peer_evidence([strong, *noise], 8, peer_floor=0.5)
    assert selected == strong[:8]


def test_peer_floor_zero_is_value_identical_to_the_historical_merge():
    strong, noise = _one_strong_and_n_noise()
    pools = [strong, *noise]
    assert peer_evidence(pools, 8, peer_floor=0) == peer_evidence(pools, 8)
    # 同样对带阈值的既有调用形状成立（全局问答那条调用点的参数组合）。
    scoped = dict(min_relevance=0.25, relative_relevance=0.6)
    assert peer_evidence(pools, 12, peer_floor=0, **scoped) == peer_evidence(pools, 12, **scoped)


def test_peer_floor_keeps_a_genuinely_relevant_minority_library():
    strong, noise = _one_strong_and_n_noise(5)
    minority = [replace(chunk("b-0", text="minority best", notebook_id="b"), relevance=0.6)]
    selected = peer_evidence([strong, minority, *noise], 4, peer_floor=0.5)
    assert minority[0] in selected, "peak 0.6 ≥ 0.9×0.5，保底名额不得被大库挤掉"
    assert {hit.notebook_id for hit in selected} == {"a", "b"}


def test_peer_floor_lets_a_denied_library_still_win_leftover_capacity():
    """没有保底名额 ≠ 被排除：预算有余时它的命中仍按跨库归一的优先级参与竞争。"""
    strong = [replace(chunk("a-0", text="relevant", notebook_id="a"), relevance=0.9)]
    weak = [replace(chunk("w-0", text="weak", notebook_id="w"), relevance=0.05)]
    assert peer_evidence([strong, weak], 4, peer_floor=0.5) == [strong[0], weak[0]]


def test_exact_dedup_preserves_semantically_different_code_indentation():
    a = chunk("a", text="if enabled:\n    run()\nfinish()", notebook_id="a")
    b = chunk("b", text="if enabled:\n    run()\n    finish()", notebook_id="b")
    assert peer_evidence([[a], [b]], 2) == [a, b]


def test_the_retrieval_snapshot_outranks_a_freshly_read_baseline(setup):
    """检索时刻的指纹是「之前」,复核时现读的那一份不是。

    把「之前」也现读一遍,两边自然恒等,这道闸就变成一句空话:答案会带着一段
    **已经被改过**的原文照常交付。所以累积快照必须赢过现读的基线。
    """
    service, _, _, _ = setup
    service.ask.retrieve = lambda nb, query: ([replace(
        chunk(f"c-{nb}", text=f"evidence {nb}", notebook_id=nb),
        source_id=f"s-{nb}", element_ids=[f"e-{nb}"],
    )], [], None)
    snapshot = {"e-a": ("s-a", "old"), "e-b": ("s-b", "old")}
    live = {"e-a": ("s-a", "new"), "e-b": ("s-b", "new")}
    reads = []

    def fingerprints(ids):
        reads.append(tuple(ids))
        # 第一次是联邦通道在检索时刻读的快照,之后是复核现读。
        source = snapshot if len(reads) == 1 else live
        return {key: source[key] for key in ids if key in source}

    service.sources.evidence_fingerprints = fingerprints

    def synthesize(question, chunks, *args):
        hit = chunks[0]
        return "stale claim", True, [], [Citation(
            label="s", source_id=hit.source_id, element_id=hit.element_ids[0],
            location_label="", quoted_span=hit.text, notebook_id=hit.notebook_id,
        )]

    service.ask.synthesize = synthesize
    result = finished(service, service.start(GlobalAskRequest(question="q"), user_id="u"))

    assert result.status == "done" and not result.answer.grounded
    assert "stale claim" not in result.answer.answer
    assert not result.answer.citations


def test_polling_and_cancellation_retain_completed_notebook_progress(setup):
    """已经收到的逐库回执在取消之后仍然留在作业行上。

    回执是「这次 run 到底搜过哪几个库」的唯一凭据;取消把它抹掉,用户看到的就是
    一次什么都没发生的提问。
    """
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def retrieve(nb, query):
        if nb == "a":
            raise _LibraryUnavailable("timeout")
        return [], [], None

    def synthesize(question, chunks, names, history, cancel):
        entered.set()
        assert release.wait(5)
        return "late", False, [], []

    service.ask.retrieve = retrieve
    service.ask.synthesize = synthesize
    request = service.start(GlobalAskRequest(question="progress"), user_id="u")
    try:
        assert entered.wait(5)
        progress = service.get_job(request.job_id, user_id="u")
        assert progress.status == "running"
        assert [row.notebook_id for row in progress.skipped_notebooks] == ["a"]
        assert progress.searched_notebook_ids == ["b"]
        stopped = service.cancel(request.job_id, user_id="u")
        assert stopped.skipped_notebooks == progress.skipped_notebooks
    finally:
        release.set()
    assert finished(service, request).status == "cancelled"
