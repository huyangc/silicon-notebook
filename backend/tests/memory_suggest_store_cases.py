"""Backend-neutral cases for ``MemorySuggestStore`` (SQLite v106 / PostgreSQL
0087, auto user-memory plan T3). SQLite: ``tests/test_memory_suggest_store.py``
(through ``repo._runtime.memory_suggest_store``); PostgreSQL twin:
``tests/postgres/test_memory_suggest_store_pg.py``.

Each case takes an ``Env``: the backend's store, ``run`` (a write with ``?``
placeholders), ``rows`` (dict rows back) and ``json`` (the backend's JSON
parameter adapter). Instants are compared as instants (PostgreSQL renders them
in the session time zone).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from app.domain.vector_index import encode_vector
from app.services.query_intent import finalize_query_intent

OWNER = "u-ms-owner"
OTHER = "u-ms-other"
NB = "nb-ms"
NB2 = "nb-ms-2"
CONV = "conv-ms"
OTHER_CONV = "conv-ms-other"
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def at(minutes: int) -> str:
    return iso(T0 + timedelta(minutes=minutes))


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


@dataclass
class Env:
    store: Any
    run: Callable[[str, tuple], None]
    rows: Callable[[str, tuple], list[dict]]
    json: Callable[[Any], Any]

    def seed(self) -> None:
        for user in (OWNER, OTHER):
            self.run(
                "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", at(0), at(0)),
            )
        for notebook_id in (NB, NB2):
            self.run(
                "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (notebook_id, notebook_id, OWNER, "ready", at(0), at(0)),
            )
        for conversation, owner in ((CONV, OWNER), (OTHER_CONV, OTHER)):
            self.run(
                "INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (conversation, NB, "t", owner, at(0), at(0)),
            )

    def ask(
        self,
        job_id: str,
        minute: int,
        *,
        owner: str = OWNER,
        notebook_id: str = NB,
        status: str = "done",
        submitted_via: str = "web",
        output: str = "answer",
        report_id: str = "",
        question: str = "q",
        answer_id: str = "",
    ) -> None:
        self.run(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, notebook_id, CONV if owner == OWNER else OTHER_CONV, owner,
                "reasoning", question, status, answer_id, at(minute), at(minute),
                submitted_via, output, report_id,
            ),
        )

    def answer(self, answer_id: str, payload: dict, *, conversation: str = CONV) -> None:
        self.run(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            (answer_id, NB, "q", self.json(payload), at(0), conversation),
        )

    def memory(
        self,
        memory_id: str,
        *,
        status: str = "confirmed",
        signal: str | None = None,
        title: str = "t",
        owner: str = OWNER,
        notebook_id: str = NB,
        minute: int = 0,
        answer_id: str | None = None,
        origin: str = "ask_answer",
    ) -> None:
        self.run(
            "INSERT INTO memory_items(id,notebook_id,created_by,source_answer_id,origin,status,"
            "title,content_md,suggested_signal,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id, notebook_id, owner, answer_id, origin, status, title, "c",
                signal, at(minute), at(minute),
            ),
        )

    def vector(self, memory_id: str, values: list[float], minute: int = 0) -> None:
        self.run(
            "INSERT INTO memory_embeddings(memory_id,model,dimension,vector,updated_at) "
            "VALUES (?,?,?,?,?)",
            (memory_id, "m", len(values), encode_vector(values), at(minute)),
        )

    def job(self) -> dict | None:
        found = self.rows(
            "SELECT * FROM memory_suggest_jobs WHERE notebook_id = ? AND owner_id = ?",
            (NB, OWNER),
        )
        return found[0] if found else None


def enable(env: Env, owner: str = OWNER) -> None:
    env.store.set_memory_mode(owner, "suggest", now=at(0))


def seed_enabled(env: Env) -> None:
    """Seed, with both users' switches on (``note_signal`` counts only then)."""
    env.seed()
    enable(env, OWNER)
    enable(env, OTHER)


def member(env: Env, notebook_id: str, user: str) -> None:
    """A read-only membership (``note_signal`` counts only for a reader)."""
    env.run(
        "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
        (notebook_id, user, "reader", at(0)),
    )


# --------------------------------------------------------------------- switch
def assert_set_memory_mode_single_transaction_clears_jobs(env: Env) -> None:
    env.seed()
    enable(env)
    state = env.store.user_state(OWNER)
    assert state["enabled"] is True and instant(state["enabled_at"]) == T0
    assert env.rows("SELECT memory_mode FROM user_profiles WHERE user_id = ?", (OWNER,)) == [
        {"memory_mode": "suggest"}
    ]
    env.store.note_signal(NB, OWNER, ask_job_id="", now=at(1))
    env.store.note_signal(NB2, OWNER, ask_job_id="", now=at(1))
    env.store.set_memory_mode(OWNER, "manual", now=at(2))
    state = env.store.user_state(OWNER)
    assert state["enabled"] is False
    assert instant(state["enabled_at"]) == T0  # the last opt-in is kept
    assert env.rows("SELECT memory_mode FROM user_profiles WHERE user_id = ?", (OWNER,)) == [
        {"memory_mode": "manual"}
    ]
    assert env.rows("SELECT * FROM memory_suggest_jobs WHERE owner_id = ?", (OWNER,)) == []
    with pytest.raises(ValueError):
        env.store.set_memory_mode(OWNER, "auto", now=at(3))
    with pytest.raises(KeyError):
        env.store.set_memory_mode("u-missing", "suggest", now=at(3))
    assert env.store.user_state("u-missing") is None


# --------------------------------------------------------------------- quota
def assert_quota_resets_on_utc_day_change(env: Env) -> None:
    env.seed()
    enable(env)
    assert env.store.reserve_daily_quota(OWNER, today_utc="2026-10-01", cap=2, now=at(1))
    assert env.store.reserve_daily_quota(OWNER, today_utc="2026-10-01", cap=2, now=at(2))
    assert not env.store.reserve_daily_quota(OWNER, today_utc="2026-10-01", cap=2, now=at(3))
    assert env.store.reserve_daily_quota(OWNER, today_utc="2026-10-02", cap=2, now=at(4))
    state = env.store.user_state(OWNER)
    assert (state["quota_day"], state["quota_used"]) == ("2026-10-02", 1)


def assert_quota_zero_rows_at_cap(env: Env) -> None:
    env.seed()
    assert not env.store.reserve_daily_quota(OWNER, today_utc="2026-10-01", cap=3, now=at(1))
    enable(env)
    for _ in range(3):
        assert env.store.reserve_daily_quota(OWNER, today_utc="2026-10-01", cap=3, now=at(1))
    assert not env.store.reserve_daily_quota(OWNER, today_utc="2026-10-01", cap=3, now=at(1))
    assert env.store.user_state(OWNER)["quota_used"] == 3


# --------------------------------------------------------------------- note_signal
def _cursor(job: dict) -> tuple[datetime, str]:
    value = job["cursor_created_at"]
    stamp = value if isinstance(value, datetime) else instant(str(value))
    return stamp.astimezone(timezone.utc), job["cursor_id"]


def assert_note_signal_takes_trigger_ask_created_at(env: Env) -> None:
    seed_enabled(env)
    env.ask("job-a", 5)
    assert env.store.note_signal(NB, OWNER, ask_job_id="job-a", now=at(30)) == 1
    job = env.job()
    assert _cursor(job) == (instant(at(5)), "job-a")
    assert int(job["cursor_inclusive"]) == 1
    assert job["status"] == "idle"


def assert_note_signal_respects_current_opt_in(env: Env) -> None:
    env.seed()
    env.ask("old-manual", 1)
    enabled = "2026-10-01T10:05:00.000200+02:00"
    env.store.set_memory_mode(OWNER, "suggest", now=enabled)
    env.store.note_signal(NB, OWNER, ask_job_id="old-manual", now=at(10))
    job = env.job()
    assert _cursor(job) == (instant(enabled), "")
    window = job["window_started_at"]
    assert instant(str(window)) == instant(enabled)
    assert env.store.clarified_scan(
        OWNER, NB, cursor_created_at=str(job["cursor_created_at"]), cursor_id="",
        inclusive=True, limit=20,
    ) == []
    for job_id, stamp, expected_id in (
        ("micro-before", "2026-10-01T08:05:00.000100+00:00", ""),
        ("micro-equal", "2026-10-01T04:05:00.000200-04:00", "micro-equal"),
        ("micro-after", "2026-10-01T08:05:00.000201+00:00", "micro-after"),
    ):
        env.run("DELETE FROM memory_suggest_jobs", ())
        env.ask(job_id, 5)
        env.run("UPDATE ask_jobs SET created_at=? WHERE id=?", (stamp, job_id))
        env.store.note_signal(NB, OWNER, ask_job_id=job_id, now=at(10))
        assert _cursor(env.job()) == (max(instant(stamp), instant(enabled)), expected_id)
    # An idempotent mode write is the same opt-in period.
    env.store.set_memory_mode(OWNER, "suggest", now=at(11))
    assert instant(env.store.user_state(OWNER)["enabled_at"]) == instant(enabled)
    env.store.set_memory_mode(OWNER, "manual", now=at(12))
    env.ask("during-manual", 13)
    env.store.set_memory_mode(OWNER, "suggest", now=at(15))
    env.store.note_signal(NB, OWNER, ask_job_id="during-manual", now=at(16))
    assert _cursor(env.job()) == (instant(at(15)), "")
    # Equal-instant spelling must keep the triggering id, not add a text tie.
    env.run("DELETE FROM memory_suggest_jobs", ())
    env.ask("at-boundary", 15)
    env.store.note_signal(NB, OWNER, ask_job_id="at-boundary", now=at(17))
    assert _cursor(env.job()) == (instant(at(15)), "at-boundary")


def assert_note_signal_foreign_ask_id_falls_back_to_now(env: Env) -> None:
    seed_enabled(env)
    env.ask("job-foreign", 5, owner=OTHER)
    env.ask("job-other-nb", 6, notebook_id=NB2)
    env.store.note_signal(NB, OWNER, ask_job_id="job-foreign", now=at(30))
    assert _cursor(env.job()) == (instant(at(30)), "")
    env.run("DELETE FROM memory_suggest_jobs", ())
    env.store.note_signal(NB, OWNER, ask_job_id="job-other-nb", now=at(31))
    assert _cursor(env.job()) == (instant(at(31)), "")
    env.run("DELETE FROM memory_suggest_jobs", ())
    env.store.note_signal(NB, OWNER, ask_job_id="", now=at(32))
    assert _cursor(env.job()) == (instant(at(32)), "")


def assert_note_signal_increments_only_on_conflict(env: Env) -> None:
    seed_enabled(env)
    env.ask("job-a", 5)
    env.ask("job-b", 6)
    assert env.store.note_signal(NB, OWNER, ask_job_id="job-a", now=at(30)) == 1
    assert env.store.note_signal(NB, OWNER, ask_job_id="job-b", now=at(31)) == 2
    assert env.store.note_signal(NB, OWNER, ask_job_id="", now=at(32)) == 3
    job = env.job()
    assert _cursor(job) == (instant(at(5)), "job-a")
    window = job["window_started_at"]
    window = window if isinstance(window, datetime) else instant(str(window))
    assert window.astimezone(timezone.utc) == instant(at(5))


def assert_note_signal_counts_only_while_the_switch_is_on(env: Env) -> None:
    env.seed()
    env.ask("job-a", 5)
    # No switch row at all.
    assert env.store.note_signal(NB, OWNER, ask_job_id="job-a", now=at(30)) == 0
    assert env.job() is None
    enable(env)
    assert env.store.note_signal(NB, OWNER, ask_job_id="job-a", now=at(31)) == 1
    env.store.set_memory_mode(OWNER, "manual", now=at(32))
    assert env.job() is None
    assert env.store.note_signal(NB, OWNER, ask_job_id="job-a", now=at(33)) == 0
    assert env.job() is None


def _jobs_of(env: Env, user: str) -> list[dict]:
    return env.rows(
        "SELECT * FROM memory_suggest_jobs WHERE notebook_id = ? AND owner_id = ?",
        (NB, user),
    )


def assert_note_signal_without_read_access_writes_nothing(env: Env) -> None:
    """Plan B9: the switch is on, but OTHER cannot read NB (not the owner, no
    membership, no grant): nothing is counted and no row appears -- neither
    on the insert branch nor, once a row exists from a past membership, on
    the conflict branch. A notebook that left the live states counts for
    nobody, its owner included."""
    seed_enabled(env)
    assert env.store.note_signal(NB, OTHER, ask_job_id="", now=at(1)) == 0
    assert _jobs_of(env, OTHER) == []
    member(env, NB, OTHER)
    assert env.store.note_signal(NB, OTHER, ask_job_id="", now=at(2)) == 1
    # Membership row deleted behind the cleanup's back: the row stays, but the
    # conflict branch must not count for a non-reader either.
    env.run("DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?", (NB, OTHER))
    assert env.store.note_signal(NB, OTHER, ask_job_id="", now=at(3)) == 0
    assert int(_jobs_of(env, OTHER)[0]["pending_signal"]) == 1
    env.run("UPDATE notebooks SET status='deleting' WHERE id=?", (NB,))
    assert env.store.note_signal(NB, OWNER, ask_job_id="", now=at(4)) == 0
    assert env.job() is None


# --------------------------------------------------------------------- claim/settle
def assert_claim_settle_cas_rejects_stale_token(env: Env) -> None:
    seed_enabled(env)
    env.ask("job-a", 5)
    env.ask("job-b", 6)
    env.store.note_signal(NB, OWNER, ask_job_id="job-a", now=at(10))
    env.store.note_signal(NB, OWNER, ask_job_id="", now=at(11))
    claimed = env.store.claim(NB, OWNER, claim_token="tok-1", now=at(12))
    assert claimed["status"] == "running" and claimed["pending_signal"] == 2
    assert instant(claimed["cursor_created_at"]) == instant(at(5))
    assert claimed["cursor_inclusive"] is True
    assert env.store.claim(NB, OWNER, claim_token="tok-2", now=at(12)) is None
    env.store.note_signal(NB, OWNER, ask_job_id="", now=at(13))
    assert not env.store.settle(
        NB, OWNER, claim_token="tok-stale", outcome="done", failure_reason="",
        cursor=None, consecutive_failures=0, consumed_pending=2, candidates_written=1,
        now=at(14),
    )
    assert env.store.settle(
        NB, OWNER, claim_token="tok-1", outcome="done", failure_reason="",
        cursor=(at(6), "job-b", False), consecutive_failures=0, consumed_pending=2,
        candidates_written=2, now=at(14),
    )
    job = env.job()
    assert job["status"] == "idle" and job["claim_token"] == ""
    assert int(job["pending_signal"]) == 1  # the signal noted while running survives
    assert _cursor(job) == (instant(at(6)), "job-b")
    assert int(job["cursor_inclusive"]) == 0
    assert (int(job["runs"]), int(job["candidates_written"])) == (1, 2)
    assert job["last_outcome"] == "done"
    # settled once: the same token no longer matches.
    assert not env.store.settle(
        NB, OWNER, claim_token="tok-1", outcome="done", failure_reason="",
        cursor=None, consecutive_failures=0, consumed_pending=0, candidates_written=0,
        now=at(15),
    )
    claimed = env.store.claim(NB, OWNER, claim_token="tok-3", now=at(16))
    assert env.store.settle(
        NB, OWNER, claim_token="tok-3", outcome="failed", failure_reason="RuntimeError",
        cursor=None, consecutive_failures=1, consumed_pending=claimed["pending_signal"],
        candidates_written=0, now=at(17),
    )
    job = env.job()
    assert _cursor(job) == (instant(at(6)), "job-b")  # cursor None keeps it
    assert (job["last_outcome"], job["failure_reason"]) == ("failed", "RuntimeError")
    assert int(job["consecutive_failures"]) == 1 and int(job["pending_signal"]) == 0
    with pytest.raises(ValueError):
        env.store.claim(NB, OWNER, claim_token="", now=at(18))


def assert_sweep_resets_running(env: Env) -> None:
    seed_enabled(env)
    env.store.note_signal(NB, OWNER, ask_job_id="", now=at(1))
    env.store.note_signal(NB2, OWNER, ask_job_id="", now=at(1))
    claimed = env.store.claim(NB, OWNER, claim_token="tok", now=at(2))
    assert env.store.sweep_stale_on_start(now=at(3)) == 1
    assert {(row["status"], row["claim_token"]) for row in env.rows(
        "SELECT status, claim_token FROM memory_suggest_jobs", ())} == {("idle", "")}
    swept = env.job()
    # One failed run of the same batch: cursor kept, failures and runs + 1.
    assert (swept["last_outcome"], swept["failure_reason"]) == ("failed", "interrupted")
    assert (int(swept["consecutive_failures"]), int(swept["runs"])) == (1, 1)
    assert _cursor(swept) == (instant(claimed["cursor_created_at"]), claimed["cursor_id"])
    untouched = env.rows(
        "SELECT runs, consecutive_failures, last_outcome FROM memory_suggest_jobs "
        "WHERE notebook_id = ? AND owner_id = ?", (NB2, OWNER))
    assert untouched == [{"runs": 0, "consecutive_failures": 0, "last_outcome": ""}]
    assert env.store.sweep_stale_on_start(now=at(4)) == 0


def assert_clear_job_rows(env: Env) -> None:
    seed_enabled(env)
    member(env, NB, OTHER)  # note_signal counts only for a reader
    env.store.note_signal(NB, OWNER, ask_job_id="", now=at(1))
    env.store.note_signal(NB, OTHER, ask_job_id="", now=at(1))
    env.store.note_signal(NB2, OWNER, ask_job_id="", now=at(1))
    assert env.store.clear_job_rows(NB, [OWNER, OTHER, "u-none"]) == 0
    env.run("DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?", (NB, OTHER))
    assert env.store.clear_job_rows(NB, [OTHER, "u-none"]) == 1
    assert env.store.clear_job_rows(NB, []) == 0
    assert {(r["notebook_id"], r["owner_id"]) for r in env.rows(
        "SELECT notebook_id, owner_id FROM memory_suggest_jobs", ())} == {
        (NB, OWNER), (NB2, OWNER)}


# --------------------------------------------------------------------- sampling
def assert_clarified_scan_ascending_after_cursor_inclusive_start(env: Env) -> None:
    env.seed()
    for minute in (1, 2, 3, 4):
        env.ask(f"job-{minute}", minute)
    rows = env.store.clarified_scan(
        OWNER, NB, cursor_created_at=at(2), cursor_id="job-2", inclusive=True, limit=10)
    assert [row["id"] for row in rows] == ["job-2", "job-3", "job-4"]
    rows = env.store.clarified_scan(
        OWNER, NB, cursor_created_at=at(2), cursor_id="job-2", inclusive=False, limit=10)
    assert [row["id"] for row in rows] == ["job-3", "job-4"]
    rows = env.store.clarified_scan(
        OWNER, NB, cursor_created_at=at(0), cursor_id="", inclusive=True, limit=2)
    assert [row["id"] for row in rows] == ["job-1", "job-2"]
    assert set(rows[0]) == {"id", "created_at", "question", "answer_id", "conversation_id", "mode"}


def assert_repeated_window_descending_latest_within_lower_bound(env: Env) -> None:
    env.seed()
    for minute in range(1, 46):
        env.ask(f"job-{minute:02d}", minute)
    rows = env.store.repeated_window(OWNER, NB, lower=at(3), limit=40)
    assert [row["id"] for row in rows] == [f"job-{m:02d}" for m in range(45, 5, -1)]
    rows = env.store.repeated_window(OWNER, NB, lower=at(40), limit=40)
    assert [row["id"] for row in rows] == [f"job-{m:02d}" for m in range(45, 39, -1)]


def assert_sampling_predicate_excludes_mcp_evidence_report_unfinished_foreign(env: Env) -> None:
    env.seed()
    env.ask("keep", 1)
    env.ask("mcp", 2, submitted_via="mcp")
    env.ask("evidence", 3, output="evidence")
    env.ask("followup", 4, report_id="rep-1")
    env.ask("running", 5, status="running")
    env.ask("failed", 6, status="failed")
    env.ask("foreign", 7, owner=OTHER)
    env.ask("other-nb", 8, notebook_id=NB2)
    scanned = env.store.clarified_scan(
        OWNER, NB, cursor_created_at=at(0), cursor_id="", inclusive=True, limit=40)
    window = env.store.repeated_window(OWNER, NB, lower=at(0), limit=40)
    assert [row["id"] for row in scanned] == ["keep"]
    assert [row["id"] for row in window] == ["keep"]


def finalized_intent() -> dict:
    """What production persists: ``finalize_query_intent`` over a reviewed
    contract with one answered ambiguity (it empties ``ambiguities`` and keeps
    ``{id, question, answer}`` rows)."""
    seed = {
        "objective": "o",
        "resolved_question": "Which regulator topology?",
        "ambiguities": [{
            "id": "a1", "question": "Which supply rail?", "reason": "secret-reason",
            "required": True, "options": ["3.3 V", "5 V"],
        }],
        "needs_clarification": True,
    }
    return finalize_query_intent(seed, answers=[{"id": "a1", "answer": "3.3 V"}])


def assert_answer_projection_owner_only(env: Env) -> None:
    env.seed()
    intent = finalized_intent()
    assert intent["ambiguities"] == []
    payload = {"answer": "x" * 50, "evidence_level": "grounded", "intent": intent}
    env.answer("ans-own", payload)
    env.answer("ans-foreign", payload, conversation=OTHER_CONV)
    env.answer("ans-plain", {"answer": "short"})
    got = env.store.answer_projections(
        OWNER, ["ans-own", "ans-foreign", "ans-plain", "ans-missing"],
        excerpt_chars=10, clarification_chars=200)
    assert set(got) == {"ans-own", "ans-plain"}
    own = got["ans-own"]
    assert own["clarification_answers"] == [
        {"id": "a1", "question": "Which supply rail?", "answer": "3.3 V"}]
    assert "reason" not in own["clarification_answers"][0]
    assert "ambiguities" not in own and "options" not in own
    assert own["evidence_level"] == "grounded"
    assert own["excerpt"] == "x" * 10
    assert own["conversation_id"] == CONV
    assert got["ans-plain"]["clarification_answers"] == []
    assert got["ans-plain"]["excerpt"] == "short"
    cut = env.store.answer_projections(
        OWNER, ["ans-own"], excerpt_chars=10, clarification_chars=5)
    assert cut["ans-own"]["clarification_answers"][0]["question"] == "Which"
    assert cut["ans-own"]["clarification_answers"][0]["answer"] == "3.3 V"
    assert env.store.answer_projections(
        OWNER, [], excerpt_chars=10, clarification_chars=200) == {}


def assert_answer_projection_sources_owner_only(env: Env) -> None:
    """Plan appendix B14: ``with_sources`` adds citations and anchors in the
    same owner-bound statement; another user's answer id projects nothing,
    and a non-list value reads as ``[]``."""
    env.seed()
    citations = [{"source_id": "s-1", "element_id": "e-1", "index": 1}]
    anchors = [{"source_id": "s-1", "text": "anchor"}]
    payload = {"answer": "a", "citations": citations, "anchors": anchors}
    env.answer("ans-own", payload)
    env.answer("ans-foreign", payload, conversation=OTHER_CONV)
    env.answer("ans-odd", {"answer": "a", "citations": {"not": "a list"}, "anchors": "x"})
    got = env.store.answer_projections(
        OWNER, ["ans-own", "ans-foreign", "ans-odd"],
        excerpt_chars=1, clarification_chars=1, with_sources=True)
    assert set(got) == {"ans-own", "ans-odd"}
    assert got["ans-own"]["citations"] == citations
    assert got["ans-own"]["anchors"] == anchors
    assert (got["ans-odd"]["citations"], got["ans-odd"]["anchors"]) == ([], [])
    assert env.store.answer_projections(
        OWNER, ["ans-foreign"], excerpt_chars=1, clarification_chars=1,
        with_sources=True) == {}
    # Without the flag the projection carries neither key.
    plain = env.store.answer_projections(
        OWNER, ["ans-own"], excerpt_chars=1, clarification_chars=1)
    assert "citations" not in plain["ans-own"] and "anchors" not in plain["ans-own"]


def assert_earliest_unfinished_ask_owner_and_predicate(env: Env) -> None:
    """Review open question 2: the owner's earliest RUNNING ask here that the
    sampling predicate would read once done; nobody else's, no MCP / evidence
    / report ask, no other notebook."""
    env.seed()
    scope = {"cursor_created_at": at(0), "cursor_id": "", "inclusive": True}
    assert env.store.earliest_unfinished_ask(OWNER, NB, **scope) is None
    env.ask("done-1", 1)
    env.ask("other-owner", 2, owner=OTHER, status="running")
    env.ask("mcp", 3, status="running", submitted_via="mcp")
    env.ask("evidence", 4, status="running", output="evidence")
    env.ask("report", 5, status="running", report_id="rep-1")
    assert env.store.earliest_unfinished_ask(OWNER, NB, **scope) is None
    env.ask("run-7", 7, status="running")
    env.ask("run-6", 6, status="running")
    found = env.store.earliest_unfinished_ask(OWNER, NB, **scope)
    assert found["id"] == "run-6"
    assert instant(found["created_at"]) == instant(at(6))
    assert env.store.earliest_unfinished_ask(OTHER, NB, **scope)["id"] == "other-owner"
    # The predicate must run BEFORE LIMIT: old running rows cannot hide the
    # next in-window row, and a cursor at that row respects inclusive/exclusive.
    scope = {"cursor_created_at": at(6), "cursor_id": "run-6", "inclusive": True}
    assert env.store.earliest_unfinished_ask(OWNER, NB, **scope)["id"] == "run-6"
    scope["inclusive"] = False
    assert env.store.earliest_unfinished_ask(OWNER, NB, **scope)["id"] == "run-7"
    scope["cursor_id"] = "run-7"
    scope["cursor_created_at"] = at(7)
    assert env.store.earliest_unfinished_ask(OWNER, NB, **scope) is None


def assert_sampling_orders_offsets_and_microseconds_like_cursor(env: Env) -> None:
    env.seed()
    stamps = {"a": "2026-10-01T09:00:00.000100+01:00",
              "z": "2026-10-01T08:00:00.000100+00:00",
              "fine": "2026-10-01T08:00:00.000200+00:00",
              "later": "2026-10-01T04:00:00.000201-04:00"}
    for job_id, stamp in stamps.items():
        env.ask(job_id, 1)
        env.run("UPDATE ask_jobs SET created_at=? WHERE id=?", (stamp, job_id))
    lower = "2026-10-01T08:00:00.000000+00:00"
    args = {"cursor_created_at": lower, "cursor_id": "", "inclusive": True, "limit": 2}
    first = env.store.clarified_scan(OWNER, NB, **args)
    assert [row["id"] for row in first] == ["a", "z"]
    args.update(cursor_created_at=stamps["z"], cursor_id="z", inclusive=False)
    assert [row["id"] for row in env.store.clarified_scan(OWNER, NB, **args)] == ["fine", "later"]
    window = env.store.repeated_window(OWNER, NB, lower=stamps["fine"], limit=4)
    assert [row["id"] for row in window] == ["later", "fine"]
    env.run("UPDATE ask_jobs SET status='running' WHERE notebook_id=?", (NB,))
    scope = {"cursor_created_at": stamps["a"], "cursor_id": "a", "inclusive": False}
    assert env.store.earliest_unfinished_ask(OWNER, NB, **scope)["id"] == "z"
    scope.update(cursor_created_at=stamps["fine"], cursor_id="fine", inclusive=True)
    assert env.store.earliest_unfinished_ask(OWNER, NB, **scope)["id"] == "fine"


def assert_latest_feedback_owner_and_latest(env: Env) -> None:
    env.seed()
    env.answer("ans-own", {"answer": "a"})
    env.answer("ans-foreign", {"answer": "a"}, conversation=OTHER_CONV)
    for feedback_id, answer_id, rating, minute in (
        ("f1", "ans-own", "useful", 1),
        ("f2", "ans-own", "not_useful", 2),
        ("f3", "ans-foreign", "useful", 3),
    ):
        env.run(
            "INSERT INTO feedback(id,answer_id,notebook_id,rating,comment,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (feedback_id, answer_id, NB, rating, "", at(minute)),
        )
    assert env.store.latest_feedback(OWNER, ["ans-own", "ans-foreign"]) == {
        "ans-own": "not_useful"}


def assert_answers_with_memory_any_status_owner_only(env: Env) -> None:
    env.seed()
    env.memory("m1", status="rejected", answer_id="ans-1")
    env.memory("m2", status="candidate", answer_id="ans-2", signal="repeated")
    env.memory("m3", owner=OTHER, answer_id="ans-3")
    assert env.store.answers_with_memory(OWNER, ["ans-1", "ans-2", "ans-3", "ans-4"]) == {
        "ans-1", "ans-2"}
    assert env.store.answers_with_memory(OWNER, []) == set()


def assert_unadopted_candidate_count(env: Env) -> None:
    env.seed()
    env.memory("s1", status="candidate", signal="clarified")
    env.memory("s2", status="candidate", signal="repeated")
    env.memory("s3", status="rejected", signal="clarified")
    env.memory("s4", status="confirmed", signal="clarified")
    env.memory("m1", status="candidate")
    env.memory("s5", status="candidate", signal="clarified", notebook_id=NB2)
    env.memory("s6", status="candidate", signal="clarified", owner=OTHER)
    assert env.store.unadopted_candidate_count(OWNER, NB) == 2


# --------------------------------------------------------------------- fingerprints
def _kinds(env: Env) -> dict[str, tuple[str, Any]]:
    out = {}
    for row in env.rows(
        "SELECT fingerprint, kind, expires_at FROM memory_suggest_fingerprints "
        "WHERE notebook_id = ? AND owner_id = ?", (NB, OWNER)):
        expires = row["expires_at"]
        if expires is not None and not isinstance(expires, datetime):
            expires = instant(str(expires))
        out[row["fingerprint"]] = (row["kind"], expires)
    return out


def assert_fingerprint_declined_upgrades_to_proposed(env: Env) -> None:
    env.seed()
    env.store.write_fingerprints(OWNER, NB, ["fp"], kind="declined", expires_at=at(100), now=at(1))
    env.store.write_fingerprints(OWNER, NB, ["fp"], kind="proposed", expires_at=None, now=at(2))
    assert _kinds(env) == {"fp": ("proposed", None)}


def assert_fingerprint_proposed_never_downgrades(env: Env) -> None:
    env.seed()
    env.store.write_fingerprints(OWNER, NB, ["fp"], kind="proposed", expires_at=None, now=at(1))
    env.store.write_fingerprints(OWNER, NB, ["fp"], kind="declined", expires_at=at(100), now=at(2))
    assert _kinds(env) == {"fp": ("proposed", None)}


def assert_fingerprint_declined_extends_to_later_expiry_only(env: Env) -> None:
    env.seed()
    env.store.write_fingerprints(OWNER, NB, ["fp"], kind="declined", expires_at=at(100), now=at(1))
    env.store.write_fingerprints(OWNER, NB, ["fp"], kind="declined", expires_at=at(50), now=at(2))
    assert _kinds(env)["fp"][1].astimezone(timezone.utc) == instant(at(100))
    env.store.write_fingerprints(OWNER, NB, ["fp"], kind="declined", expires_at=at(200), now=at(3))
    assert _kinds(env)["fp"][1].astimezone(timezone.utc) == instant(at(200))
    with pytest.raises(ValueError):
        env.store.write_fingerprints(OWNER, NB, ["fp"], kind="declined", expires_at=None, now=at(3))
    with pytest.raises(ValueError):
        env.store.write_fingerprints(OWNER, NB, ["fp"], kind="other", expires_at=None, now=at(3))


def assert_fingerprint_presence_rules(env: Env) -> None:
    env.seed()
    env.store.write_fingerprints(OWNER, NB, ["p"], kind="proposed", expires_at=None, now=at(1))
    env.store.write_fingerprints(OWNER, NB, ["d-live"], kind="declined", expires_at=at(100), now=at(1))
    env.store.write_fingerprints(OWNER, NB, ["d-dead"], kind="declined", expires_at=at(10), now=at(1))
    env.store.write_fingerprints(OTHER, NB, ["o"], kind="proposed", expires_at=None, now=at(1))
    env.store.write_fingerprints(OWNER, NB2, ["n2"], kind="proposed", expires_at=None, now=at(1))
    present = env.store.fingerprints_present(
        OWNER, NB, ["p", "d-live", "d-dead", "o", "n2", "absent"], now=at(50))
    assert present == {"p", "d-live"}
    assert env.store.fingerprints_present(OWNER, NB, [], now=at(50)) == set()


# --------------------------------------------------------------------- neighbours
def assert_neighbors_vector_side_latest_scan_by_updated_at(env: Env, total: int) -> None:
    """``total`` = scan bound + 1: the oldest vector-bearing memory is the best
    cosine match but falls outside the most recently updated window."""
    env.seed()
    env.memory("m-oldest", minute=0, title="alpha")
    env.vector("m-oldest", [1.0, 0.0])
    for index in range(1, total):
        memory_id = f"m-{index:04d}"
        env.memory(memory_id, minute=index, title=f"other {index}")
        env.vector(memory_id, [0.0, 1.0] if index > 1 else [0.8, 0.6], minute=index)
    found = env.store.suggest_neighbors(
        OWNER, NB, query_vectors=[[1.0, 0.0]], lexical_queries=["zzzz"],
        statuses=["confirmed"], k=1)
    assert [[item["id"] for item in items] for items in found] == [["m-0001"]]
    assert found[0][0]["side"] == "vector"


def assert_neighbors_lexical_includes_rejected_and_deprecated(env: Env) -> None:
    env.seed()
    env.memory("m-rej", status="rejected", title="voltage regulator layout")
    env.memory("m-dep", status="deprecated", title="voltage regulator thermal")
    env.memory("m-foreign", owner=OTHER, title="voltage regulator other")
    env.memory("m-nb2", notebook_id=NB2, title="voltage regulator elsewhere")
    found = env.store.suggest_neighbors(
        OWNER, NB, query_vectors=[], lexical_queries=["voltage regulator", ""],
        statuses=["candidate", "confirmed", "rejected", "deprecated"], k=3)
    assert len(found) == 2
    assert {item["id"] for item in found[0]} == {"m-rej", "m-dep"}
    assert {item["side"] for item in found[0]} == {"lexical"}
    assert found[1] == []
    narrowed = env.store.suggest_neighbors(
        OWNER, NB, query_vectors=[], lexical_queries=["voltage regulator"],
        statuses=["confirmed"], k=3)
    assert narrowed == [[]]


def assert_neighbors_rejects_unknown_status(env: Env) -> None:
    env.seed()
    with pytest.raises(ValueError):
        env.store.suggest_neighbors(
            OWNER, NB, query_vectors=[], lexical_queries=["x"], statuses=["archived"], k=3)
    with pytest.raises(ValueError):
        env.store.suggest_neighbors(
            OWNER, NB, query_vectors=[[1.0]], lexical_queries=["a", "b"],
            statuses=["confirmed"], k=3)


# --------------------------------------------------------------------- expiry
def assert_expire_overdue_only_suggested_candidates(env: Env) -> None:
    env.seed()
    env.memory("s-old-1", status="candidate", signal="clarified", minute=1)
    env.memory("s-old-2", status="candidate", signal="repeated", minute=2)
    env.memory("s-old-3", status="candidate", signal="clarified", minute=3)
    env.memory("s-new", status="candidate", signal="clarified", minute=100)
    env.memory("s-adopted", status="confirmed", signal="clarified", minute=1)
    env.memory("manual", status="candidate", minute=1)
    env.memory("agent", status="candidate", minute=1, origin="external_agent")
    first = env.store.expire_overdue(now=at(200), cutoff=at(50), limit=2)
    assert [row["id"] for row in first] == ["s-old-1", "s-old-2"]
    assert first[0] == {
        "id": "s-old-1", "owner_id": OWNER, "notebook_id": NB, "suggested_signal": "clarified"}
    second = env.store.expire_overdue(now=at(200), cutoff=at(50), limit=2)
    assert [row["id"] for row in second] == ["s-old-3"]
    assert env.store.expire_overdue(now=at(200), cutoff=at(50), limit=2) == []
    statuses = {row["id"]: row["status"] for row in env.rows(
        "SELECT id, status FROM memory_items", ())}
    assert statuses == {
        "s-old-1": "rejected", "s-old-2": "rejected", "s-old-3": "rejected",
        "s-new": "candidate", "s-adopted": "confirmed", "manual": "candidate",
        "agent": "candidate",
    }
    revisions = env.rows(
        "SELECT memory_id, revision, status, changed_by, change_reason FROM memory_revisions "
        "ORDER BY memory_id", ())
    assert revisions == [
        {"memory_id": f"s-old-{n}", "revision": 1, "status": "rejected",
         "changed_by": OWNER, "change_reason": "expired"}
        for n in (1, 2, 3)
    ]


def assert_expire_overdue_scoped_to_owner_notebook(env: Env) -> None:
    env.seed()
    env.memory("s-own", status="candidate", signal="clarified", minute=1)
    env.memory("s-nb2", status="candidate", signal="clarified", minute=1, notebook_id=NB2)
    env.memory("s-other", status="candidate", signal="clarified", minute=1, owner=OTHER)
    scoped = env.store.expire_overdue(
        now=at(200), cutoff=at(50), limit=10, owner_id=OWNER, notebook_id=NB)
    assert [row["id"] for row in scoped] == ["s-own"]
