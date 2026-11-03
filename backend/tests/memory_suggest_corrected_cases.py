"""Backend-neutral corrected-signal behavior, exercised by both adapters."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.models.ask import AskRequest, AskResponse
from app.services.memory_retrieval import MemoryRetriever
from tests.memory_suggest_job_cases import (
    CONV,
    MEMBER,
    NB,
    OWNER,
    Env,
    at,
    build,
    decline_all,
    instant,
    keep_all,
)


def corrected_world(env: Env, **settings):
    return build(env, auto_memory_suggest_clarified_enabled=False,
                 auto_memory_suggest_repeated_enabled=False,
                 auto_memory_suggest_corrected_enabled=True, **settings)


def pair(env: Env):
    first = env.ask("first", 1, "clock latency source definition", excerpt="The clock latency is twelve cycles.")
    second = env.ask("second", 2, "Correction clock latency is ten cycles", excerpt="The cited timing document says twelve cycles.")
    env.open_window("first")
    return first, second


def scan(env: Env, *, lower=1, cursor=1, limit=40):
    return env.runtime.memory_suggest_store.corrected_scan(
        MEMBER, NB, cursor_created_at=at(cursor), cursor_id="", inclusive=True,
        window_started_at=at(lower), limit=limit,
    )


def _retime(env: Env, ask_id: str, stamp: str):
    env.run("UPDATE ask_jobs SET created_at=?,updated_at=? WHERE id=?", (stamp, stamp, ask_id))
    env.run("UPDATE answers SET created_at=? WHERE id=?", (stamp, f"ans-{ask_id}"))


def _exact_scan(env: Env, stamp: str, ask_id: str, *, inclusive=True, lower=None):
    return env.runtime.memory_suggest_store.corrected_scan(
        MEMBER, NB, cursor_created_at=stamp, cursor_id=ask_id,
        inclusive=inclusive, window_started_at=lower or stamp, limit=40,
    )


def _strict_read_period(env: Env):
    stamps = {"at-loss": "2026-10-01T09:00:00.000200+01:00",
              "fresh": "2026-10-01T08:00:00.000201+00:00",
              "fresh-next": "2026-10-01T07:00:00.000202-01:00"}
    for index, (ask_id, stamp) in enumerate(stamps.items(), start=1):
        env.ask(ask_id, index, f"clock latency correction {ask_id}", excerpt=f"answer from {ask_id}")
        _retime(env, ask_id, stamp)
    # Persist the sampler's real boundary independently of revocation writers,
    # whose capture/transaction semantics are covered in the owning lane.
    env.run("INSERT INTO memory_suggest_read_fences(notebook_id,owner_id,lost_at) VALUES (?,?,?)",
            (NB, MEMBER, "2026-10-01T08:00:00.000200+00:00"))
    return stamps


def assert_corrected_later_at_loss_is_excluded_but_next_microsecond_survives(env: Env):
    corrected_world(env)
    stamps = _strict_read_period(env)
    rows = _exact_scan(env, stamps["at-loss"], "", lower=stamps["at-loss"])
    assert [row["id"] for row in rows] == ["fresh", "fresh-next"]


def assert_corrected_predecessor_at_loss_cannot_enter_a_fresh_pair(env: Env):
    corrected_world(env)
    stamps = _strict_read_period(env)
    rows = _exact_scan(env, stamps["fresh"], "fresh", lower=stamps["at-loss"])
    assert [(row["id"], row["previous_answer_id"]) for row in rows] == [
        ("fresh", ""), ("fresh-next", "ans-fresh"),
    ]


def assert_corrected_period_does_not_skip_an_ineligible_raw_middle_turn(env: Env):
    corrected_world(env)
    stamps = _strict_read_period(env)
    env.run("UPDATE ask_jobs SET submitted_via='mcp' WHERE id='fresh-next'")
    env.ask("after-mcp", 4, "clock latency correction after agent turn")
    _retime(env, "after-mcp", "2026-10-01T08:00:00.000203+00:00")
    rows = _exact_scan(env, stamps["fresh"], "fresh", lower=stamps["at-loss"])
    assert [(row["id"], row["previous_answer_id"]) for row in rows] == [("fresh", ""), ("after-mcp", "")]


def assert_corrected_job_never_replays_a_pair_across_the_last_read_loss(env: Env):
    world = corrected_world(env)
    stamps = _strict_read_period(env)
    env.open_window("at-loss")
    assert world.start()
    assert env.drafts_per_answer() == {"ans-fresh-next": 1}
    assert "answer from at-loss" not in world.models.prompts[0]
    assert env.job_row()["cursor"] == (instant(stamps["fresh-next"]), "fresh-next", False)
    assert world.start() and len(world.models.prompts) == 1


def assert_corrected_equal_offset_instants_use_id_for_both_turns(env: Env):
    corrected_world(env)
    stamps = {"a-equal": "2026-10-01T09:00:00.000100+01:00",
              "z-equal": "2026-10-01T08:00:00.000100+00:00"}
    for index, (ask_id, stamp) in enumerate(stamps.items(), start=1):
        env.ask(ask_id, index, "clock latency correction")
        _retime(env, ask_id, stamp)
    rows = _exact_scan(env, stamps["a-equal"], "a-equal")
    assert [(row["id"], row["previous_answer_id"]) for row in rows] == [
        ("a-equal", ""), ("z-equal", "ans-a-equal"),
    ]
    rows = _exact_scan(env, stamps["a-equal"], "a-equal", inclusive=False)
    assert [row["id"] for row in rows] == ["z-equal"]


def _microsecond_turns(env: Env):
    stamps = {"z-100": "2026-10-01T09:00:00.000100+01:00",
              "a-200": "2026-10-01T08:00:00.000200+00:00",
              "m-201": "2026-10-01T07:00:00.000201-01:00"}
    for index, (ask_id, stamp) in enumerate(stamps.items(), start=1):
        env.ask(ask_id, index, f"clock latency correction {ask_id}")
        _retime(env, ask_id, stamp)
    return stamps


def assert_corrected_microsecond_prefix_and_raw_predecessor_agree(env: Env):
    corrected_world(env)
    stamps = _microsecond_turns(env)
    rows = _exact_scan(env, stamps["z-100"], "z-100")
    assert [(row["id"], row["previous_answer_id"]) for row in rows] == [
        ("z-100", ""), ("a-200", "ans-z-100"), ("m-201", "ans-a-200"),
    ]
    rows = _exact_scan(env, stamps["a-200"], "a-200", inclusive=False, lower=stamps["z-100"])
    assert [(row["id"], row["previous_answer_id"]) for row in rows] == [("m-201", "ans-a-200")]
    env.run("UPDATE ask_jobs SET submitted_via='mcp' WHERE id='a-200'")
    rows = _exact_scan(env, stamps["z-100"], "z-100")
    assert [(row["id"], row["previous_answer_id"]) for row in rows] == [("z-100", ""), ("m-201", "")]


def assert_corrected_window_rejects_a_predecessor_one_microsecond_too_old(env: Env):
    corrected_world(env)
    for index, (ask_id, micros) in enumerate((("a-prior", "200"), ("z-later", "201")), start=1):
        env.ask(ask_id, index, "clock latency correction")
        _retime(env, ask_id, f"2026-10-01T08:00:00.000{micros}+00:00")
    rows = _exact_scan(env, "2026-10-01T08:00:00+00:00", "",
                       lower="2026-10-01T09:00:00.000201+01:00")
    assert [(row["id"], row["previous_answer_id"]) for row in rows] == [("a-prior", ""), ("z-later", "")]


def assert_corrected_microsecond_rescan_writes_each_pair_once(env: Env):
    world = corrected_world(env, auto_memory_suggest_items_per_run=1)
    stamps = _microsecond_turns(env)
    env.open_window("z-100")
    assert world.start()
    assert env.job_row()["cursor"] == (instant(stamps["m-201"]), "m-201", True)
    assert env.drafts_per_answer() == {"ans-a-200": 1}
    assert world.start()
    assert env.drafts_per_answer() == {"ans-a-200": 1, "ans-m-201": 1}
    assert env.job_row()["cursor"] == (instant(stamps["m-201"]), "m-201", False)
    assert world.start() and len(world.models.prompts) == 2


def assert_corrected_sampling_is_adjacent_private_and_not_mcp(env: Env):
    corrected_world(env)
    pair(env)
    env.ask("middle-mcp", 3, "private agent instruction")
    env.run("UPDATE ask_jobs SET submitted_via='mcp' WHERE id=?", ("middle-mcp",))
    env.ask("after-mcp", 4, "correction after intervening agent")
    env.ask("foreign", 5, "foreign secret question")
    env.run("UPDATE ask_jobs SET created_by=? WHERE id=?", (OWNER, "foreign"))
    env.ask("after-foreign", 6, "correction after other author")
    env.ask("report", 7, "report continuation")
    env.run("UPDATE ask_jobs SET report_id='report-x' WHERE id=?", ("report",))
    env.ask("after-report", 8, "correction after report")
    env.ask("evidence", 9, "evidence only question")
    env.run("UPDATE ask_jobs SET output='evidence' WHERE id=?", ("evidence",))
    env.ask("after-evidence", 10, "correction after evidence")
    env.ask("running", 11, "still pending", status="running")
    env.ask("after-running", 12, "correction after pending")
    env.run("INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("other-conv", NB, "other", MEMBER, at(0), at(0)))
    env.ask("new-conversation", 13, "correction in separate conversation")
    env.run("UPDATE ask_jobs SET conversation_id='other-conv' WHERE id=?", ("new-conversation",))
    pairs = [(row["id"], row.get("previous_answer_id")) for row in scan(env) if row.get("previous_answer_id")]
    assert pairs == [("second", "ans-first")]
    assert "foreign secret" not in json.dumps(scan(env))


def assert_corrected_window_does_not_backfill_previous_membership(env: Env):
    corrected_world(env)
    pair(env)
    rows = scan(env, lower=2, cursor=2)
    assert rows[0]["id"] == "second" and not rows[0].get("previous_answer_id")
    env.ask("third", 3, "Correction clock latency source unit")
    rows = scan(env, lower=2, cursor=3)
    assert [(row["id"], row.get("previous_answer_id")) for row in rows] == [("third", "ans-second")]


def assert_corrected_does_not_occupy_answer_index(env: Env):
    world = corrected_world(env)
    first, second = pair(env)
    assert world.start()
    drafts = env.drafts()
    assert len(drafts) == 1 and drafts[0]["suggested_signal"] == "corrected"
    assert drafts[0]["source_answer_id"] is None
    record = env.runtime.memory_service.get(str(drafts[0]["id"]), MEMBER)
    assert record.provenance["suggestion"]["answer_ids"] == [first, second]
    assert record.provenance["suggestion"]["conflicts_with_sources"] is False
    # The pair's answers retain their independent manual-save uniqueness slots.
    env.memory("manual-first", status="confirmed", answer_id=first)
    env.memory("manual-second", status="confirmed", answer_id=second)


def assert_corrected_conflict_flag_is_persisted(env: Env):
    world = corrected_world(env)
    pair(env)
    def conflicting(prompt):
        payload = json.loads(keep_all(prompt))
        for entry in payload["items"]:
            entry["conflicts_with_sources"] = True
        return json.dumps(payload)
    world.models.reply = conflicting
    assert world.start()
    record = env.runtime.memory_service.get(str(env.drafts()[0]["id"]), MEMBER)
    assert record.provenance["suggestion"]["conflicts_with_sources"] is True
    assert "twelve cycles" in world.models.prompts[-1]
    assert "ten cycles" in world.models.prompts[-1]


def assert_corrected_missing_conflict_flag_rejects_entire_batch(env: Env):
    world = corrected_world(env)
    pair(env)
    env.ask("third", 3, "Correction clock latency is nine cycles")
    def missing(prompt):
        payload = json.loads(keep_all(prompt))
        assert len(payload["items"]) == 2
        payload["items"][-1].pop("conflicts_with_sources", None)
        return json.dumps(payload)
    world.models.reply = missing
    assert world.start()
    assert env.job_row()["outcome"] == "model_rejected"
    assert env.drafts() == []
    assert env.rows("SELECT fingerprint FROM memory_suggest_fingerprints") == []
    assert env.job_row()["cursor"] == env.cursor_at("first", 1, True)


def assert_corrected_fingerprint_survives_deletion(env: Env):
    world = corrected_world(env)
    pair(env)
    assert world.start() and len(env.drafts()) == 1
    env.run("DELETE FROM memory_items WHERE id=?", (env.drafts()[0]["id"],))
    env.run("UPDATE memory_suggest_jobs SET cursor_created_at=?,cursor_id='',cursor_inclusive=1 WHERE notebook_id=? AND owner_id=?",
            (at(1), NB, MEMBER))
    assert world.start()
    assert len(world.models.prompts) == 1 and env.drafts() == []


def assert_corrected_disabled_has_zero_model_calls(env: Env):
    world = corrected_world(env)
    pair(env)
    world.settings.auto_memory_suggest_corrected_enabled = False
    assert world.start()
    assert world.models.prompts == [] and world.models.embedded == []
    assert env.quota_used() == 0


def assert_corrected_write_cap_holds_cursor_without_duplicate_drafts(env: Env):
    world = corrected_world(env, auto_memory_suggest_candidates_per_run=1)
    pair(env)
    env.ask("third", 3, "Correction clock latency is nine cycles")
    assert world.start()
    assert env.job_row()["cursor"] == env.cursor_at("third", 3, True)
    assert len(env.drafts()) == 1
    assert world.start()
    assert len(env.drafts()) == 2
    assert env.job_row()["cursor"] == env.cursor_at("third", 3, False)


def assert_mixed_ascending_class_cap_holds_first_corrected(env: Env):
    world = build(env, auto_memory_suggest_clarified_enabled=True,
                  auto_memory_suggest_repeated_enabled=False,
                  auto_memory_suggest_corrected_enabled=True,
                  auto_memory_suggest_items_per_run=1)
    env.clarified("first", 1, "clock latency measurement")
    env.ask("second", 2, "Correction clock latency source unit")
    env.open_window("first")
    assert world.start()
    assert env.job_row()["cursor"] == env.cursor_at("second", 2, True)
    assert world.start()
    assert [row["suggested_signal"] for row in env.drafts()].count("corrected") == 1


def assert_completion_after_empty_clarified_scan_is_revisited(env: Env):
    world = build(env, auto_memory_suggest_clarified_enabled=True,
                  auto_memory_suggest_repeated_enabled=False,
                  auto_memory_suggest_corrected_enabled=True)
    env.clarified("first", 1, "clock latency measurement", status="running")
    env.open_window("first")
    store = env.runtime.memory_suggest_store
    original = store.clarified_scan

    def finish_after_scan(*args, **kwargs):
        sampled = original(*args, **kwargs)
        assert sampled == []
        env.finish("first")
        return sampled

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "clarified_scan", finish_after_scan)
        assert world.start()
    assert env.job_row()["outcome"] == "no_signal"
    assert env.job_row()["cursor"] == env.cursor_at("first", 1, True)
    assert world.start()
    assert [row["suggested_signal"] for row in env.drafts()] == ["clarified"]
    assert env.job_row()["cursor"] == env.cursor_at("first", 1, False)


def assert_completion_after_nonempty_clarified_scan_keeps_early_fence(env: Env):
    world = build(env, auto_memory_suggest_clarified_enabled=True,
                  auto_memory_suggest_repeated_enabled=False,
                  auto_memory_suggest_corrected_enabled=True)
    env.ask("boundary", 0, "starting boundary")
    env.open_window("boundary")
    assert world.start()
    env.clarified("first", 1, "clock latency measurement", status="running")
    env.ask("second", 2, "later clock latency question")
    store = env.runtime.memory_suggest_store
    original_clarified = store.clarified_scan
    original_corrected = store.corrected_scan
    scans = {}

    def finish_after_read(*args, **kwargs):
        rows = original_clarified(*args, **kwargs)
        scans["clarified"] = [row["id"] for row in rows]
        env.finish("first")
        return rows

    def observe_corrected(*args, **kwargs):
        rows = original_corrected(*args, **kwargs)
        scans["corrected"] = [row["id"] for row in rows]
        return rows

    world.models.reply = decline_all
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "clarified_scan", finish_after_read)
        patch.setattr(store, "corrected_scan", observe_corrected)
        assert world.start()
    assert scans == {"clarified": ["second"], "corrected": ["first", "second"]}
    assert env.job_row()["cursor"] == env.cursor_at("first", 1, True)
    assert env.drafts() == []
    world.models.reply = keep_all
    assert world.start()
    assert env.drafts_per_answer() == {"ans-first": 1}
    assert env.job_row()["cursor"] == env.cursor_at("second", 2, False)


def assert_disabled_clarified_empty_scan_does_not_hold_corrected(env: Env):
    world = corrected_world(env)
    pair(env)
    env.run("UPDATE ask_jobs SET status='running'")
    store = env.runtime.memory_suggest_store
    original = store.clarified_scan

    def finish_after_scan(*args, **kwargs):
        sampled = original(*args, **kwargs)
        assert sampled == []
        env.finish("first")
        env.finish("second")
        return sampled

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "clarified_scan", finish_after_scan)
        assert world.start()
    assert [row["suggested_signal"] for row in env.drafts()] == ["corrected"]
    # The before-scan unfinished fence is independent of disabled signal
    # classes. Once those asks finish, it holds this run once; the next run
    # advances, without the disabled empty clarified scan or a duplicate draft.
    assert env.job_row()["cursor"] == env.cursor_at("first", 1, True)
    assert world.start()
    assert [row["suggested_signal"] for row in env.drafts()] == ["corrected"]
    assert env.job_row()["cursor"] == env.cursor_at("second", 2, False)


def assert_corrected_waits_for_earlier_unfinished_turn(env: Env):
    world = corrected_world(env)
    env.ask("first", 1, "clock latency measurement")
    env.ask("second", 2, "Correction clock latency second", status="running")
    env.ask("third", 3, "Correction clock latency third")
    env.ask("fourth", 4, "Correction clock latency fourth")
    env.open_window("first")
    assert world.start() and len(env.drafts()) == 1
    assert env.job_row()["cursor"] == env.cursor_at("second", 2, True)
    env.finish("second")
    assert world.start() and len(env.drafts()) == 3
    assert env.job_row()["cursor"] == env.cursor_at("fourth", 4, False)


def assert_corrected_rejects_foreign_conversation_ownership(env: Env):
    world = corrected_world(env)
    pair(env)
    env.run("UPDATE conversations SET created_by=? WHERE id=?", (OWNER, CONV))
    assert scan(env) == []
    assert world.start() and env.drafts() == [] and world.models.prompts == []


def assert_corrected_answer_projection_must_be_in_the_pair_conversation(env: Env):
    world = corrected_world(env)
    first, _ = pair(env)
    env.run("INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("other-conv", NB, "other", MEMBER, at(0), at(0)))
    env.run("UPDATE answers SET conversation_id='other-conv' WHERE id=?", (first,))
    assert world.start() and world.models.prompts == [] and env.drafts() == []


def assert_corrected_excludes_an_answer_saved_after_the_followup(env: Env):
    world = corrected_world(env)
    first, _ = pair(env)
    env.run("UPDATE answers SET created_at=? WHERE id=?", (at(3), first))
    assert world.start() and world.models.prompts == [] and env.drafts() == []


def assert_answer_projection_time_comes_from_real_save_not_asked_at(env: Env):
    env.seed()
    state = env.runtime.ask_state
    clock = SimpleNamespace(value=at(5))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(state, "seams", SimpleNamespace(new_id=state.seams.new_id, now=lambda: clock.value))
        response = AskResponse(conclusion="saved answer", asked_at=at(0))
        answer = state.save_answer(NB, CONV, "question", response, MEMBER)
        projection = env.runtime.memory_suggest_store.answer_projections(
            MEMBER, [answer], excerpt_chars=10, clarification_chars=10)[answer]
        assert instant(projection["created_at"]) == instant(at(5))
        assert instant(response.answered_at) == instant(at(5))
        clock.value = at(7)
        job_id, conversation_id = state.begin_durable_job(
            NB, AskRequest(question="next question", conversation_id=CONV, asked_at=at(0)), "chunk", MEMBER,
            submitted_via="web")
        clock.value = at(10)
        final = AskResponse(conclusion="atomic saved answer", asked_at=at(0))
        saved = state.save_answer_for_job(job_id, NB, conversation_id, "next question", final, MEMBER)
        projection = env.runtime.memory_suggest_store.answer_projections(
            MEMBER, [saved], excerpt_chars=10, clarification_chars=10)[saved]
        assert instant(projection["created_at"]) == instant(at(10))
        assert instant(final.answered_at) == instant(at(10))


def assert_corrected_scan_limit_never_skips_the_next_pair(env: Env):
    world = corrected_world(env, auto_memory_suggest_items_per_run=100)
    world.models.reply = decline_all
    for index in range(1, 42):
        env.ask(f"ask-{index:02d}", index, f"Q{index} clock latency correction")
    env.open_window("ask-01")
    assert world.start()
    assert env.job_row()["cursor"] == env.cursor_at("ask-40", 40, False)
    assert "current question: Q41" not in world.models.prompts[-1]
    assert world.start()
    assert "current question: Q41" in world.models.prompts[-1]
    assert env.job_row()["cursor"] == env.cursor_at("ask-41", 41, False)


def assert_corrected_draft_stays_out_of_formal_readers(env: Env):
    world = corrected_world(env)
    pair(env)
    assert world.start()
    memory_id = str(env.drafts()[0]["id"])
    retriever = MemoryRetriever(env.runtime.memory_store, SimpleNamespace(
        embed_query=lambda _query: pytest.fail("a suggestion without a vector must not embed")))
    assert retriever.notebook_memory_hits(MEMBER, NB, "content") == []
    assert [hit.memory_id for hit in retriever.agent_memory_hits(MEMBER, NB, "content", True)] == [memory_id]
    overview = env.runtime.memory_store.notebook_content_overview(MEMBER, NB)
    assert overview["total"] == 0
    snapshot = env.runtime.memory_store.memory_export_snapshot(NB, MEMBER, page_size=50, max_bytes=100_000)
    assert snapshot.items == () and snapshot.omitted_suggestion_count == 1
    # The generic reader contract changes only once the row is confirmed.
    env.run("UPDATE memory_items SET status='confirmed' WHERE id=?", (memory_id,))
    assert [hit.memory_id for hit in retriever.notebook_memory_hits(MEMBER, NB, "content")] == [memory_id]


CASES = {
    "period_later_strict": assert_corrected_later_at_loss_is_excluded_but_next_microsecond_survives,
    "period_predecessor_strict": assert_corrected_predecessor_at_loss_cannot_enter_a_fresh_pair,
    "period_raw_middle_turn": assert_corrected_period_does_not_skip_an_ineligible_raw_middle_turn,
    "period_job_no_replay": assert_corrected_job_never_replays_a_pair_across_the_last_read_loss,
    "offset_equal_id_order": assert_corrected_equal_offset_instants_use_id_for_both_turns,
    "microsecond_prefix_predecessor": assert_corrected_microsecond_prefix_and_raw_predecessor_agree,
    "microsecond_window": assert_corrected_window_rejects_a_predecessor_one_microsecond_too_old,
    "microsecond_rescan": assert_corrected_microsecond_rescan_writes_each_pair_once,
    "adjacent_private_not_mcp": assert_corrected_sampling_is_adjacent_private_and_not_mcp,
    "no_window_backfill": assert_corrected_window_does_not_backfill_previous_membership,
    "answer_index_unoccupied": assert_corrected_does_not_occupy_answer_index,
    "conflict_marker": assert_corrected_conflict_flag_is_persisted,
    "conflict_flag_required": assert_corrected_missing_conflict_flag_rejects_entire_batch,
    "fingerprint_survives_deletion": assert_corrected_fingerprint_survives_deletion,
    "disabled_zero_models": assert_corrected_disabled_has_zero_model_calls,
    "write_cap_cursor": assert_corrected_write_cap_holds_cursor_without_duplicate_drafts,
    "mixed_signal_cursor": assert_mixed_ascending_class_cap_holds_first_corrected,
    "completion_after_empty_scan": assert_completion_after_empty_clarified_scan_is_revisited,
    "completion_after_nonempty_scan": assert_completion_after_nonempty_clarified_scan_keeps_early_fence,
    "disabled_empty_scan": assert_disabled_clarified_empty_scan_does_not_hold_corrected,
    "unfinished_cursor_fence": assert_corrected_waits_for_earlier_unfinished_turn,
    "conversation_owner": assert_corrected_rejects_foreign_conversation_ownership,
    "answer_conversation": assert_corrected_answer_projection_must_be_in_the_pair_conversation,
    "concurrent_answer_timing": assert_corrected_excludes_an_answer_saved_after_the_followup,
    "real_answer_save_time": assert_answer_projection_time_comes_from_real_save_not_asked_at,
    "scan_limit": assert_corrected_scan_limit_never_skips_the_next_pair,
    "formal_readers": assert_corrected_draft_stays_out_of_formal_readers,
}
