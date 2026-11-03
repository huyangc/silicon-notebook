"""Backend-neutral cases for the Memory store's write side of auto user-memory
suggestions (plan T4b; design §4.2 step 8, §5.1, §5.3, §5.4, §6.6 (a) 2).
SQLite: ``tests/test_memory_suggest_writes.py`` (through the runtime seats);
PostgreSQL twin: ``tests/postgres/test_memory_suggest_writes_pg.py``.

Each case takes an ``Env``: the backend's ``MemoryStore`` (``store``), its
``MemorySuggestStore`` (``suggest``), ``run`` (a write with ``?``
placeholders) and ``rows`` (dict rows back). ``MEMBER`` reads ``NB`` through a
membership row (the notebook is ``OWNER``'s), so every access check below
goes through the membership probe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable

import pytest

from app.domain.memory_suggest_contracts import (
    REVOKED_CLAIM_LOST,
    SKIP_NO_READ_ACCESS,
    SKIP_NOTEBOOK_UNAVAILABLE,
    SKIP_USER_DISABLED,
    SuggestionRevoked,
)
from app.models.memory import MemoryWrite

OWNER = "u-mw-owner"
MEMBER = "u-mw-member"
NB = "nb-mw"
CONV = "conv-mw"
MEMBER_SINCE = "2026-10-01T08:00:00+00:00"
NOW = "2026-10-29T08:00:00+00:00"
TOKEN = "tok-mw-1"


def at(minute: int) -> str:
    return f"2026-10-29T08:{minute:02d}:00+00:00"


@dataclass
class Env:
    store: Any
    suggest: Any
    run: Callable[[str, tuple], None]
    rows: Callable[[str, tuple], list[dict]]
    json: Callable[[Any], Any]

    def seed(self) -> None:
        for user in (OWNER, MEMBER):
            self.run(
                "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", NOW, NOW),
            )
        self.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (NB, "Shared", OWNER, "ready", NOW, NOW),
        )
        self.run(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
            (NB, MEMBER, "reader", MEMBER_SINCE),
        )
        self.run(
            "INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (CONV, NB, "t", MEMBER, NOW, NOW),
        )

    def answer(self, answer_id: str) -> None:
        self.run(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            (answer_id, NB, "q", self.json({"answer": "a", "mode": "chunk"}), NOW, CONV),
        )

    def memory(
        self,
        memory_id: str,
        *,
        status: str = "confirmed",
        signal: str | None = None,
        answer_id: str | None = None,
        owner: str = MEMBER,
        origin: str = "ask_answer",
    ) -> None:
        self.run(
            "INSERT INTO memory_items(id,notebook_id,created_by,source_answer_id,origin,status,"
            "title,content_md,suggested_signal,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, NB, owner, answer_id, origin, status, "t", "c", signal, NOW, NOW),
        )

    def start_run(self, owner: str = MEMBER, token: str = TOKEN) -> None:
        """Switch on, a job row, claimed under ``token``."""
        self.suggest.set_memory_mode(owner, "suggest", now=NOW)
        assert self.suggest.note_signal(NB, owner, ask_job_id="", now=NOW) >= 1
        assert self.suggest.claim(NB, owner, claim_token=token, now=at(1)) is not None

    def create(self, writes, *, fingerprints=("fp-1",), token: str = TOKEN, owner=MEMBER):
        return self.store.create_suggestions_with_initial_revision(
            notebook_id=NB, owner_id=owner, claim_token=token, writes=list(writes),
            proposed_fingerprints=list(fingerprints), now=NOW,
        )

    def job_rows(self, owner: str = MEMBER) -> list[dict]:
        return self.rows(
            "SELECT status, claim_token FROM memory_suggest_jobs "
            "WHERE notebook_id = ? AND owner_id = ?",
            (NB, owner),
        )

    def fingerprints(self, owner: str = MEMBER) -> dict[str, str]:
        return {
            row["fingerprint"]: row["kind"]
            for row in self.rows(
                "SELECT fingerprint, kind FROM memory_suggest_fingerprints "
                "WHERE notebook_id = ? AND owner_id = ?",
                (NB, owner),
            )
        }

    def memory_ids(self, owner: str = MEMBER) -> set[str]:
        return {
            row["id"]
            for row in self.rows(
                "SELECT id FROM memory_items WHERE notebook_id = ? AND created_by = ?",
                (NB, owner),
            )
        }

    def children(self, memory_id: str) -> tuple[int, int]:
        provenance = self.rows(
            "SELECT COUNT(*) AS n FROM memory_provenance WHERE memory_id = ?", (memory_id,)
        )[0]["n"]
        revisions = self.rows(
            "SELECT COUNT(*) AS n FROM memory_revisions WHERE memory_id = ?", (memory_id,)
        )[0]["n"]
        return int(provenance), int(revisions)

    def is_member(self, user: str = MEMBER) -> bool:
        return bool(self.rows(
            "SELECT 1 AS x FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
            (NB, user),
        ))


def suggestion(
    memory_id: str,
    *,
    signal: str = "clarified",
    answer_id: str | None = None,
    owner: str = MEMBER,
) -> MemoryWrite:
    return MemoryWrite(
        id=memory_id,
        notebook_id=NB,
        created_by=owner,
        origin="ask_answer",
        status="candidate",
        title=f"title {memory_id}",
        content_md="draft body",
        tags=("draft",),
        created_at=NOW,
        updated_at=NOW,
        source_answer_id=answer_id,
        provenance={
            "question": "q",
            "answer_id": answer_id or "",
            "suggestion": {
                "signal": signal, "kind": "term", "answer_ids": [answer_id or "a-x"],
                "cluster_size": 3, "refines": None,
            },
        },
        suggested_signal=signal,
    )


def manual_save(memory_id: str, answer_id: str) -> MemoryWrite:
    """The write ``MemoryService.create_from_answer`` builds."""
    return MemoryWrite(
        id=memory_id,
        notebook_id=NB,
        created_by=MEMBER,
        source_answer_id=answer_id,
        origin="ask_answer",
        status="confirmed",
        title="manual title",
        content_md="manual body",
        tags=["mine"],
        confirmed_by=MEMBER,
        confirmed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        provenance={},
    )


# ----------------------------------------------------------------- exit
def assert_member_exit_snapshot_counts_memories_and_suggestions_apart(env: Env) -> None:
    env.seed()
    env.memory("own-confirmed")
    env.memory("own-candidate", status="candidate", origin="external_agent")
    env.memory("adopted", signal="clarified")
    env.memory("adopted-deprecated", status="deprecated", signal="repeated")
    env.memory("draft-open", status="candidate", signal="clarified")
    env.memory("draft-dismissed", status="rejected", signal="repeated")
    disclosure = env.store.member_exit_snapshot(NB, MEMBER, claim=False)
    assert (disclosure.memory_count, disclosure.suggestion_count) == (4, 2)
    assert disclosure.memory_ids == ()
    claimed = env.store.member_exit_snapshot(NB, MEMBER, claim=True)
    assert (claimed.memory_count, claimed.suggestion_count) == (4, 2)
    # The claim's ids are "your memories" only: the drafts are left to the
    # finish, which deletes whatever is still unadopted under the lock.
    assert set(claimed.memory_ids) == {
        "own-confirmed", "own-candidate", "adopted", "adopted-deprecated"}
    # The owner keeps reading their own notebook: nothing is counted.
    env.memory("owner-own", owner=OWNER)
    env.memory("owner-draft", owner=OWNER, status="candidate", signal="clarified")
    owner_view = env.store.member_exit_snapshot(NB, OWNER, claim=False)
    assert (owner_view.is_member, owner_view.keeps_access) == (False, True)
    assert (owner_view.memory_count, owner_view.suggestion_count) == (0, 0)


def assert_exit_claim_writes_nothing_finish_deletes_job_row(env: Env) -> None:
    """Review P3-4: the claim deletes nothing -- an exit whose acknowledgement
    then mismatches leaves the job row too. The finish that ends the
    membership deletes it, and an in-flight run then writes nothing."""
    env.seed()
    env.start_run()
    env.start_run(owner=OWNER, token="tok-owner")
    env.store.member_exit_snapshot(NB, MEMBER, claim=False)
    claimed = env.store.member_exit_snapshot(NB, MEMBER, claim=True)
    assert env.job_rows() == [{"status": "running", "claim_token": TOKEN}]
    finish = env.store.finish_member_exit(NB, MEMBER, claimed.membership_token)
    assert (finish.memory_count, finish.ended, finish.deleted_suggestion_count) == (0, True, 0)
    assert env.job_rows() == []
    # The in-flight run's own write now fails: no read access, no row.
    with pytest.raises(SuggestionRevoked):
        env.create([suggestion("late")])
    assert env.memory_ids() == set()
    assert env.fingerprints() == {}
    # The owner's own job row is untouched.
    assert len(env.job_rows(OWNER)) == 1


def assert_exit_race_suggestion_between_purge_and_finish(env: Env) -> None:
    """A run writes a draft after the exit's claim and purge: the finish
    deletes it (with the draft the claim left) under the membership lock and
    the exit completes, reporting both."""
    env.seed()
    env.memory("own")
    env.memory("old-draft", status="candidate", signal="clarified")
    env.suggest.write_fingerprints(
        MEMBER, NB, ["fp-declined"], kind="declined", expires_at=at(59), now=NOW)
    claimed = env.store.member_exit_snapshot(NB, MEMBER, claim=True)
    assert set(claimed.memory_ids) == {"own"}
    assert claimed.suggestion_count == 1
    assert env.store.bulk_delete_memories(MEMBER, list(claimed.memory_ids)) == 1
    # Race window: a signal re-creates the job row, a new run claims it and
    # writes one draft (and its proposed fingerprint).
    env.start_run(token="tok-race")
    result = env.create([suggestion("race-draft")], fingerprints=["fp-race"], token="tok-race")
    assert [item.id for item in result.created] == ["race-draft"]
    assert env.memory_ids() == {"old-draft", "race-draft"}
    finish = env.store.finish_member_exit(NB, MEMBER, claimed.membership_token)
    assert (finish.memory_count, finish.ended, finish.deleted_suggestion_count) == (0, True, 2)
    assert not env.is_member()
    assert env.memory_ids() == set()
    assert env.children("race-draft") == (0, 0)
    assert env.fingerprints() == {}
    assert env.job_rows() == []


def assert_finish_keeps_a_suggestion_adopted_after_the_claim(env: Env) -> None:
    """Review P2-1: a draft the leaver adopts between the claim and the
    finish is "your memory" by then -- kept; the membership stays, and
    nothing else is deleted either."""
    env.seed()
    env.memory("draft-a", status="candidate", signal="clarified")
    env.memory("draft-b", status="rejected", signal="repeated")
    claimed = env.store.member_exit_snapshot(NB, MEMBER, claim=True)
    assert (claimed.memory_ids, claimed.suggestion_count) == ((), 2)
    env.run("UPDATE memory_items SET status='confirmed' WHERE id=?", ("draft-a",))
    finish = env.store.finish_member_exit(NB, MEMBER, claimed.membership_token)
    assert (finish.memory_count, finish.ended) == (1, False)
    assert (finish.deleted_suggestion_count, finish.remaining_suggestion_count) == (0, 1)
    assert env.is_member()
    assert env.memory_ids() == {"draft-a", "draft-b"}


def assert_finish_member_exit_still_counts_all_states(env: Env) -> None:
    """``remaining`` counts every status but an unadopted suggestion (the §6.3
    exemption) and then nothing is deleted -- not even the draft -- and the
    membership stays."""
    env.seed()
    env.suggest.write_fingerprints(MEMBER, NB, ["fp-keep"], kind="proposed", expires_at=None,
                                   now=NOW)
    claimed = env.store.member_exit_snapshot(NB, MEMBER, claim=True)
    assert claimed.memory_ids == ()
    env.memory("agent-candidate", status="candidate", origin="external_agent")
    env.memory("manual-rejected", status="rejected")
    env.memory("adopted-deprecated", status="deprecated", signal="repeated")
    env.memory("draft", status="candidate", signal="clarified")
    finish = env.store.finish_member_exit(NB, MEMBER, claimed.membership_token)
    assert (finish.memory_count, finish.ended) == (3, False)
    assert (finish.deleted_suggestion_count, finish.remaining_suggestion_count) == (0, 1)
    assert env.is_member()
    assert env.memory_ids() == {
        "agent-candidate", "manual-rejected", "adopted-deprecated", "draft"}
    assert env.fingerprints() == {"fp-keep": "proposed"}


# ----------------------------------------------------------------- suggestion writes
def _assert_nothing_written(env: Env) -> None:
    assert env.memory_ids() == set()
    assert env.fingerprints() == {}


def assert_create_suggestions_revoked_when_disabled(env: Env) -> None:
    env.seed()
    env.start_run()
    # Only the switch: the job row stays (``set_memory_mode`` would delete it too).
    env.run("UPDATE memory_suggest_users SET enabled = 0 WHERE owner_id = ?", (MEMBER,))
    with pytest.raises(SuggestionRevoked) as revoked:
        env.create([suggestion("s1")])
    assert revoked.value.reason == SKIP_USER_DISABLED
    _assert_nothing_written(env)
    env.suggest.set_memory_mode(MEMBER, "manual", now=NOW)
    with pytest.raises(SuggestionRevoked) as revoked:
        env.create([suggestion("s1")])
    assert revoked.value.reason == SKIP_USER_DISABLED
    _assert_nothing_written(env)


def assert_create_suggestions_revoked_when_job_row_deleted(env: Env) -> None:
    env.seed()
    env.start_run()
    env.run("DELETE FROM memory_suggest_jobs WHERE notebook_id=? AND owner_id=?", (NB, MEMBER))
    with pytest.raises(SuggestionRevoked) as revoked:
        env.create([suggestion("s1")])
    assert revoked.value.reason == REVOKED_CLAIM_LOST
    _assert_nothing_written(env)


def assert_create_suggestions_revoked_when_token_mismatch(env: Env) -> None:
    env.seed()
    env.start_run()
    with pytest.raises(SuggestionRevoked) as revoked:
        env.create([suggestion("s1")], token="tok-stale")
    assert revoked.value.reason == REVOKED_CLAIM_LOST
    _assert_nothing_written(env)
    # The right token after the run settled (idle, token cleared) is stale too.
    assert env.suggest.settle(
        NB, MEMBER, claim_token=TOKEN, outcome="done", failure_reason="", cursor=None,
        consecutive_failures=0, consumed_pending=1, candidates_written=0, now=at(2))
    with pytest.raises(SuggestionRevoked) as revoked:
        env.create([suggestion("s1")])
    assert revoked.value.reason == REVOKED_CLAIM_LOST
    _assert_nothing_written(env)


def assert_create_suggestions_revoked_without_read_access(env: Env) -> None:
    env.seed()
    env.start_run()
    env.run("UPDATE notebooks SET status = 'pending_delete' WHERE id = ?", (NB,))
    with pytest.raises(SuggestionRevoked) as revoked:
        env.create([suggestion("s1")])
    assert revoked.value.reason == SKIP_NOTEBOOK_UNAVAILABLE
    env.run("UPDATE notebooks SET status = 'ready' WHERE id = ?", (NB,))
    env.run("DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?", (NB, MEMBER))
    with pytest.raises(SuggestionRevoked) as revoked:
        env.create([suggestion("s1")])
    assert revoked.value.reason == SKIP_NO_READ_ACCESS
    _assert_nothing_written(env)


def assert_repeated_suggestion_answer_once_conflict_is_skipped(env: Env) -> None:
    env.seed()
    env.memory("manual", answer_id="ans-1")
    env.suggest.write_fingerprints(
        MEMBER, NB, ["fp-a"], kind="declined", expires_at=at(59), now=NOW)
    env.start_run()
    result = env.create(
        [
            suggestion("dup", signal="repeated", answer_id="ans-1"),
            suggestion("rep", signal="repeated", answer_id="ans-2"),
            suggestion("clar", signal="clarified"),
        ],
        fingerprints=["fp-a", "fp-b", "fp-a"],
    )
    assert result.deduped == 1
    assert [item.id for item in result.created] == ["rep", "clar"]
    created = {item.id: item for item in result.created}
    assert created["rep"].source_answer_id == "ans-2"
    assert created["clar"].source_answer_id is None
    assert {item.status for item in result.created} == {"candidate"}
    assert {item.suggested_signal for item in result.created} == {"repeated", "clarified"}
    assert created["rep"].provenance["suggestion"]["signal"] == "repeated"
    assert env.memory_ids() == {"manual", "rep", "clar"}
    # The pre-existing memory of that answer is untouched (still the manual one).
    manual = env.rows("SELECT status, suggested_signal FROM memory_items WHERE id = ?",
                      ("manual",))[0]
    assert (manual["status"], manual["suggested_signal"]) == ("confirmed", None)
    for memory_id in ("rep", "clar"):
        revisions = env.rows(
            "SELECT revision, change_reason, changed_by, status FROM memory_revisions "
            "WHERE memory_id = ?",
            (memory_id,),
        )
        assert revisions == [{"revision": 1, "change_reason": "suggested",
                              "changed_by": MEMBER, "status": "candidate"}]
        assert env.children(memory_id) == (1, 1)
    # Same §5.1 priority as write_fingerprints: declined upgraded to proposed.
    assert env.fingerprints() == {"fp-a": "proposed", "fp-b": "proposed"}
    expiry = env.rows(
        "SELECT expires_at FROM memory_suggest_fingerprints WHERE fingerprint = ?", ("fp-a",))
    assert expiry == [{"expires_at": None}]


def assert_create_suggestions_rejects_malformed_writes(env: Env) -> None:
    env.seed()
    env.start_run()
    for bad in (
        suggestion("bad-1", signal="clarified", answer_id="ans-1"),
        suggestion("bad-2", owner=OWNER),
        replace(suggestion("bad-3"), suggested_signal=None),
        replace(suggestion("bad-4"), status="confirmed"),
    ):
        with pytest.raises(ValueError):
            env.create([bad])
    _assert_nothing_written(env)


# ----------------------------------------------------------------- adoption
def assert_adopt_from_answer_cas(env: Env) -> None:
    env.seed()
    env.memory("manual", answer_id="ans-manual")
    env.memory("dismissed", status="rejected", signal="repeated", answer_id="ans-dismissed")
    env.start_run()
    env.create([suggestion("open", signal="repeated", answer_id="ans-open")])
    fields = {"title": "Edited", "content_md": "edited body", "tags": ["kept"]}

    def adopt(answer_id: str, notebook_id: str = NB):
        return env.store.adopt_suggestion_from_answer(
            notebook_id, MEMBER, answer_id, fields=fields, changed_by=MEMBER, reason="confirmed")

    # Nothing to adopt: no row, a manual memory, a dismissed suggestion,
    # another notebook — None and nothing changes.
    assert adopt("ans-none") is None
    assert adopt("ans-manual") is None
    assert adopt("ans-dismissed") is None
    assert adopt("ans-open", notebook_id="nb-elsewhere") is None
    assert env.rows("SELECT status FROM memory_items WHERE id = ?", ("open",)) == [
        {"status": "candidate"}]
    adopted = adopt("ans-open")
    assert adopted is not None
    assert (adopted.id, adopted.status, adopted.suggested_signal) == ("open", "confirmed",
                                                                     "repeated")
    assert (adopted.title, adopted.content_md, adopted.tags) == (
        "Edited", "edited body", ["kept"])
    assert adopted.confirmed_by == MEMBER
    assert adopted.embedding_status == "pending"
    revisions = env.rows(
        "SELECT revision, change_reason, status, title FROM memory_revisions "
        "WHERE memory_id = ? ORDER BY revision",
        ("open",),
    )
    assert revisions == [
        {"revision": 1, "change_reason": "suggested", "status": "candidate",
         "title": "title open"},
        {"revision": 2, "change_reason": "confirmed", "status": "confirmed", "title": "Edited"},
    ]
    # The CAS lost: the row is no longer an open suggestion.
    assert adopt("ans-open") is None
    assert len(env.rows("SELECT 1 AS x FROM memory_revisions WHERE memory_id = ?",
                        ("open",))) == 2


def assert_replace_rejected_suggestion(env: Env) -> None:
    env.seed()
    env.answer("ans-1")
    env.answer("ans-2")
    env.start_run()
    env.create(
        [suggestion("old", signal="repeated", answer_id="ans-1"),
         suggestion("open", signal="repeated", answer_id="ans-2")],
        fingerprints=["fp-1"],
    )
    env.store.transition_with_revision(
        "old", MEMBER, {"candidate"}, "rejected", fields=None, changed_by=MEMBER,
        reason="rejected")
    replaced = env.store.replace_rejected_suggestion_with_answer(
        manual_save("m-new", "ans-1"), MEMBER, "created_from_answer")
    assert (replaced.id, replaced.status, replaced.suggested_signal) == (
        "m-new", "confirmed", None)
    assert replaced.source_answer_id == "ans-1"
    assert replaced.provenance["answer_id"] == "ans-1"
    assert env.children("old") == (0, 0)
    assert env.memory_ids() == {"m-new", "open"}
    assert env.fingerprints() == {"fp-1": "proposed"}
    # An open suggestion is not replaced: no-op, the row comes back untouched.
    untouched = env.store.replace_rejected_suggestion_with_answer(
        manual_save("m-other", "ans-2"), MEMBER, "created_from_answer")
    assert (untouched.id, untouched.status) == ("open", "candidate")
    assert env.memory_ids() == {"m-new", "open"}


def assert_concurrent_manual_save_noop(env: Env) -> None:
    """A second save on the same answer (a concurrent duplicate request, or a
    replace that lost to another one) returns the existing row and writes
    nothing."""
    env.seed()
    env.answer("ans-1")
    env.memory("old", status="rejected", signal="repeated", answer_id="ans-1")
    first = env.store.replace_rejected_suggestion_with_answer(
        manual_save("m-first", "ans-1"), MEMBER, "created_from_answer")
    assert first.id == "m-first"
    again = env.store.replace_rejected_suggestion_with_answer(
        manual_save("m-second", "ans-1"), MEMBER, "created_from_answer")
    plain = env.store.create_answer_with_initial_revision(
        manual_save("m-third", "ans-1"), MEMBER, "created_from_answer")
    assert again.id == plain.id == "m-first"
    assert env.memory_ids() == {"m-first"}
    assert env.children("m-first") == (1, 1)


# ----------------------------------------------------------------- D15(a) re-send
def _logged(env: Env, table: str, after: int) -> list[tuple[str, str]]:
    found = []
    for row in env.rows(
        "SELECT key_json, operation FROM sync_change_log WHERE table_name = ? AND seq > ? "
        "ORDER BY seq",
        (table, after),
    ):
        key = row["key_json"]
        key = json.loads(key) if isinstance(key, str) else key
        found.append((str(key.get("id")), str(row["operation"])))
    return found


def _log_head(env: Env) -> int:
    return int(env.rows("SELECT COALESCE(MAX(seq), 0) AS s FROM sync_change_log", ())[0]["s"])


def assert_resync_children_touch_only_when_capture_enabled(env: Env) -> None:
    env.seed()
    env.memory("manual", status="candidate", origin="external_agent")
    env.run(
        "INSERT INTO memory_provenance(id,memory_id,origin,payload_json,created_at) "
        "VALUES (?,?,?,?,?)",
        ("prov-manual", "manual", "external_agent", env.json({}), NOW),
    )
    env.start_run()
    env.create([
        suggestion("s-off", signal="repeated", answer_id="ans-off"),
        suggestion("s-flag", signal="repeated", answer_id="ans-flag"),
        suggestion("s-on", signal="repeated", answer_id="ans-on"),
        suggestion("s-adopt", signal="repeated", answer_id="ans-adopt"),
    ])

    def first_children(memory_id: str) -> tuple[str, str]:
        provenance = env.rows(
            "SELECT id FROM memory_provenance WHERE memory_id = ?", (memory_id,))[0]["id"]
        revision = env.rows(
            "SELECT id FROM memory_revisions WHERE memory_id = ? AND revision = 1",
            (memory_id,))[0]["id"]
        return provenance, revision

    def confirm(memory_id: str, *, resync: bool) -> None:
        env.store.transition_with_revision(
            memory_id, MEMBER, {"candidate"}, "confirmed", fields=None, changed_by=MEMBER,
            reason="confirmed", resync_children=resync)

    # Capture closed: the adoption writes nothing to the log at all.
    head = _log_head(env)
    confirm("s-off", resync=True)
    assert _logged(env, "memory_provenance", head) == []
    assert _logged(env, "memory_revisions", head) == []

    env.run("INSERT INTO sync_capture_control(singleton, enabled) VALUES (?, ?)", (1, True))
    # Capture open, no flag: only the new revision is logged (by its insert).
    head = _log_head(env)
    confirm("s-flag", resync=False)
    provenance, first_revision = first_children("s-flag")
    assert _logged(env, "memory_provenance", head) == []
    assert (first_revision, "upsert") not in _logged(env, "memory_revisions", head)

    # Capture open, flag set, a suggestion: provenance and EVERY revision re-sent.
    head = _log_head(env)
    confirm("s-on", resync=True)
    provenance, first_revision = first_children("s-on")
    assert _logged(env, "memory_provenance", head) == [(provenance, "upsert")]
    logged_revisions = _logged(env, "memory_revisions", head)
    all_revisions = {
        row["id"] for row in env.rows(
            "SELECT id FROM memory_revisions WHERE memory_id = ?", ("s-on",))}
    assert len(all_revisions) == 2
    assert {key for key, operation in logged_revisions if operation == "upsert"} == all_revisions
    assert (first_revision, "upsert") in logged_revisions

    # The flag on a memory that is not a suggestion touches nothing.
    head = _log_head(env)
    confirm("manual", resync=True)
    assert _logged(env, "memory_provenance", head) == []

    # The answer-side adoption entry re-sends the same way.
    head = _log_head(env)
    adopted = env.store.adopt_suggestion_from_answer(
        NB, MEMBER, "ans-adopt", fields={}, changed_by=MEMBER, reason="confirmed")
    assert adopted is not None and adopted.status == "confirmed"
    provenance, first_revision = first_children("s-adopt")
    assert _logged(env, "memory_provenance", head) == [(provenance, "upsert")]
    assert (first_revision, "upsert") in _logged(env, "memory_revisions", head)
