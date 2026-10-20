"""Backend-agnostic cases: a global Ask turn after a participant library is deleted.

Product decision 2026-10-19 (``app/domain/global_ask_retention.py``): the
owner keeps reading the turn -- question and answer intact, the deleted
library's citations degraded -- for ``USER_ACTIVITY_RETENTION_DAYS`` after the
deletion, then it is purged by the same two points that purge
``retained_user_activity`` (startup recovery and the next deletion).

Every case drives a REAL repository: rows are inserted with plain SQL (spelled
through the store's own marker), a library is deleted through the notebook
store's delete transaction (the one that writes the activity projection), and
reads go through the runtime's ``GlobalAskService`` / query store. Both
``tests/test_global_ask_retention.py`` (SQLite) and
``tests/postgres/test_global_ask_retention_pg.py`` run the same list, so the two
adapters cannot drift.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.ask import AskResponse, Citation
from app.models.global_ask import GlobalAskJob, GlobalNotebookScope
from app.services.global_ask import GlobalAskError

NOW = "2026-10-19T00:00:00+00:00"
OWNER, OTHER, LIBRARIAN = "user-owner", "user-other", "user-lib"


class World:
    def __init__(self, repo):
        self.repo = repo
        self.store = repo._runtime.global_ask_store
        self.service = repo._runtime.global_ask_service()
        self.clock = 0

    def sql(self, text):
        return self.store._sql(text)

    def execute(self, text, params=()):
        with self.store.database.write() as db:
            return db.execute(self.sql(text), params)

    def fetch(self, text, params=()):
        with self.store.database.connect() as db:
            return db.execute(self.sql(text), params).fetchall()

    def stamp(self):
        """A strictly increasing creation instant (the keyset needs order)."""
        self.clock += 1
        return f"2026-10-01T00:00:{self.clock:02d}+00:00"

    def turn(self, job_id, conversation_id, resolved, *, cited=None, question=None,
             new_conversation=False):
        """One finished global turn over ``resolved`` citing ``cited``."""
        cited = list(resolved if cited is None else cited)
        job = GlobalAskJob(
            job_id=job_id, conversation_id=conversation_id, status="running",
            question=question or f"问题 {job_id}", created_at=self.stamp(),
            notebook_scope=GlobalNotebookScope(mode="include", notebook_ids=list(resolved)),
            resolved_notebook_ids=list(resolved),
        )
        self.store.create(job, OWNER, None, "{}", "web", new_conversation=new_conversation)
        job.status = "done"
        job.searched_notebook_ids = list(resolved)
        job.cited_notebook_ids = cited
        job.answer = AskResponse(
            answer_id=job_id, conclusion=f"回答 {job_id}", answer=f"回答 {job_id} [1]",
            citations=[
                Citation(label=f"来源 {nb}", source_id=f"src-{nb}", element_id=f"el-{nb}",
                         location_label="p. 1", quoted_span=f"摘录 {nb}", notebook_id=nb)
                for nb in cited
            ],
        )
        assert self.store.save(job, OWNER)
        return job

    def delete_notebook(self, notebook_id):
        self.repo._runtime.notebook_store.delete_row_and_orphan_embeddings(notebook_id)

    def expire(self, job_id):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.execute("UPDATE global_ask_jobs SET retained_until=? WHERE id=?", (past, job_id))

    def retained_until(self, job_id):
        rows = self.fetch("SELECT retained_until FROM global_ask_jobs WHERE id=?", (job_id,))
        return rows[0]["retained_until"] if rows else "missing"


def build(repo) -> World:
    world = World(repo)
    for user in (OWNER, OTHER, LIBRARIAN):
        world.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (user, f"{user}@example.test", user, "user", "active", user, NOW, NOW),
        )
    for notebook_id, owner in (
        ("nb-a", OWNER), ("nb-b", OWNER), ("nb-d", OWNER), ("nb-c", LIBRARIAN),
    ):
        world.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (notebook_id, f"NB {notebook_id}", owner, "ready", NOW, NOW),
        )
    # The owner reads nb-c as a member of someone else's library.
    repo._runtime.sharing_store.add_member("nb-c", OWNER)
    return world


def _expect_404(call):
    with pytest.raises(GlobalAskError) as refused:
        call()
    assert refused.value.status_code == 404


def case_the_owner_still_reads_a_turn_whose_library_was_deleted(repo):
    world = build(repo)
    before = world.turn("job-1", "conv-1", ["nb-a"], new_conversation=True)
    both = world.turn("job-2", "conv-1", ["nb-a", "nb-b"])
    world.turn("job-3", "conv-1", ["nb-a"])

    world.delete_notebook("nb-b")

    detail = world.service.conversation("conv-1", user_id=OWNER)
    turns = {turn.job_id: turn for turn in detail.turns}
    assert list(turns) == ["job-1", "job-2", "job-3"]
    # The turn that ran over nb-b carries its window and names the gone library;
    # its body and BOTH citations are intact -- only the interface degrades the
    # nb-b one.
    retained = turns["job-2"]
    assert retained.retained_until
    window = datetime.fromisoformat(retained.retained_until) - datetime.now(timezone.utc)
    days = world.repo._runtime.settings.user_activity_retention_days
    assert timedelta(days=days) - timedelta(minutes=5) < window <= timedelta(days=days)
    assert retained.deleted_notebook_ids == ["nb-b"]
    assert retained.answer.answer == both.answer.answer
    assert [c.notebook_id for c in retained.answer.citations] == ["nb-a", "nb-b"]
    # The conversation's other turns did not run over nb-b: untouched.
    for job_id in ("job-1", "job-3"):
        assert turns[job_id].retained_until == ""
        assert turns[job_id].deleted_notebook_ids == []
    assert turns["job-1"].answer.answer == before.answer.answer
    # The single-job read degrades the same way.
    assert world.service.get_job("job-2", user_id=OWNER).deleted_notebook_ids == ["nb-b"]
    # The read-time facts never reach the stored payload.
    payload = json.loads(world.fetch(
        "SELECT payload_json FROM global_ask_jobs WHERE id=?", ("job-2",)
    )[0]["payload_json"])
    assert "deleted_notebook_ids" not in payload and "retained_until" not in payload
    assert [item.id for item in world.service.list_conversations(user_id=OWNER)] == ["conv-1"]


def case_a_non_owner_and_an_allowlisted_caller_still_get_404(repo):
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], new_conversation=True)
    world.delete_notebook("nb-b")

    _expect_404(lambda: world.service.get_job("job-1", user_id=OTHER))
    _expect_404(lambda: world.service.conversation("conv-1", user_id=OTHER))
    # An Agent token's allowlist cannot show it was ever allowed the deleted
    # library: the strict rule stays for it.
    _expect_404(lambda: world.service.get_job(
        "job-1", user_id=OWNER, allowed_notebook_ids=["nb-a"]))
    _expect_404(lambda: world.service.conversation(
        "conv-1", user_id=OWNER, allowed_notebook_ids=["nb-a"]))


def case_a_live_library_the_owner_lost_still_refuses_the_whole_turn(repo):
    """The conservative half of the decision: only a library that no longer
    EXISTS stops taking part. One that still exists but the owner can no longer
    read (left / removed from it) keeps hiding every turn that ran over it --
    the turn may restate that library's material."""
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-b", "nb-c"], new_conversation=True)
    world.delete_notebook("nb-b")
    assert world.service.get_job("job-1", user_id=OWNER).deleted_notebook_ids == ["nb-b"]

    world.repo._runtime.sharing_store.remove_member("nb-c", OWNER)
    _expect_404(lambda: world.service.get_job("job-1", user_id=OWNER))
    _expect_404(lambda: world.service.conversation("conv-1", user_id=OWNER))

    # Access restored: the same turn reads again (nothing was destroyed).
    world.repo._runtime.sharing_store.add_member("nb-c", OWNER)
    assert world.service.get_job("job-1", user_id=OWNER).deleted_notebook_ids == ["nb-b"]


def case_an_unstamped_turn_with_a_missing_library_keeps_the_strict_rule(repo):
    """A library that disappeared WITHOUT a stamp (deleted before the
    retention column existed, or a turn imported from elsewhere) has no window
    to retain the turn for: it stays 404, as it was."""
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-gone"], new_conversation=True)
    _expect_404(lambda: world.service.get_job("job-1", user_id=OWNER))
    _expect_404(lambda: world.service.conversation("conv-1", user_id=OWNER))
    # Even beside a retained turn that names the same missing library (so the
    # page's existence read does see it as gone), the unstamped turn keeps
    # the strict rule: the tolerance is per turn, never per page.
    world.turn("job-2", "conv-2", ["nb-a", "nb-b"], cited=["nb-a", "nb-b", "nb-gone"],
               new_conversation=True)
    world.turn("job-3", "conv-2", ["nb-a", "nb-gone"])
    world.delete_notebook("nb-b")
    assert world.service.get_job("job-2", user_id=OWNER).deleted_notebook_ids == ["nb-b", "nb-gone"]
    _expect_404(lambda: world.service.conversation("conv-2", user_id=OWNER))


def case_a_public_link_keeps_its_per_open_recheck(repo):
    world = build(repo)
    # A link whose snapshot drew on nb-b dies with nb-b, exactly as before.
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], new_conversation=True)
    touching = world.service.share_conversation("conv-1", user_id=OWNER)["share_token"]
    # A link whose snapshot never touched nb-b keeps serving, even though a
    # LATER, unpublished turn of the same conversation ran over it.
    world.turn("job-2", "conv-2", ["nb-a"], new_conversation=True)
    clean = world.service.share_conversation(
        "conv-2", user_id=OWNER, expected_through_id="job-2")["share_token"]
    world.turn("job-3", "conv-2", ["nb-a", "nb-b"])
    assert world.service.public_conversation(touching) is not None

    world.delete_notebook("nb-b")

    assert world.service.public_conversation(touching) is None
    served = world.service.public_conversation(clean)
    assert served is not None and len(served["turns"]) == 1
    # And a share whose range now includes the retained turn is refused: the
    # owner may read it, a public link may not carry it.
    _expect_404(lambda: world.service.share_conversation(
        "conv-2", user_id=OWNER, expected_through_id="job-3"))


def case_a_later_deletion_never_extends_an_earlier_window(repo):
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], new_conversation=True)
    world.delete_notebook("nb-b")
    first = world.retained_until("job-1")
    assert first
    world.delete_notebook("nb-a")
    assert world.retained_until("job-1") == first
    turn = world.service.get_job("job-1", user_id=OWNER)
    assert turn.deleted_notebook_ids == ["nb-a", "nb-b"]


def case_an_expired_turn_vanishes_then_startup_recovery_purges_it(repo):
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], question="第一问", new_conversation=True)
    world.turn("job-2", "conv-1", ["nb-a"], question="第二问")
    world.turn("job-3", "conv-2", ["nb-b"], question="只问已删的库", new_conversation=True)
    token = world.service.share_conversation("conv-1", user_id=OWNER)["share_token"]
    world.delete_notebook("nb-b")
    world.expire("job-1")
    world.expire("job-3")

    # The automatic title named the expired first turn: every surface that shows
    # a title reads the earliest live question from now on, before any purge.
    assert world.service.conversation("conv-1", user_id=OWNER).title == "第二问"
    assert world.service.list_conversations(user_id=OWNER)[0].title == "第二问"
    public = world.service.public_conversation(token)
    assert public is not None and public["title"] == "第二问"
    assert [turn["question"] for turn in public["turns"]] == ["第二问"]

    # Logical gate: gone for the owner the moment the window passes.
    _expect_404(lambda: world.service.get_job("job-1", user_id=OWNER))
    detail = world.service.conversation("conv-1", user_id=OWNER)
    assert [turn.job_id for turn in detail.turns] == ["job-2"]
    _expect_404(lambda: world.service.conversation("conv-2", user_id=OWNER))
    assert [item.id for item in world.service.list_conversations(user_id=OWNER)] == ["conv-1"]
    # ... and for the administrator's activity feed too.
    feed = world.repo._runtime.queries.list_user_activity(
        OWNER, include_inaccessible_questions=True)
    assert {item["id"] for item in feed["items"]} >= {"job-2"}
    assert not {"job-1", "job-3"} & {item["id"] for item in feed["items"]}
    questions = world.repo._runtime.queries.list_admin_questions(scope="global")
    assert [item["id"] for item in questions["items"]] == ["job-2"]
    usage = {row["id"]: row for row in world.repo._runtime.queries.list_user_usage()}
    assert usage[OWNER]["questions"] == 1
    assert usage[OWNER]["conversations"] == 1

    world.repo._recover_interrupted_jobs()

    assert world.retained_until("job-1") == "missing"
    assert world.retained_until("job-3") == "missing"
    assert world.fetch("SELECT id FROM global_ask_conversations WHERE id=?", ("conv-2",)) == []
    # The automatic title named the purged first turn: it moves on.
    title = world.fetch("SELECT title FROM global_ask_conversations WHERE id=?", ("conv-1",))
    assert title[0]["title"] == "第二问"


def case_a_renamed_title_survives_the_purge(repo):
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-b"], question="第一问", new_conversation=True)
    world.turn("job-2", "conv-1", ["nb-a"], question="第二问")
    world.service.rename_conversation("conv-1", "我的标题", user_id=OWNER)
    world.delete_notebook("nb-b")
    world.expire("job-1")
    world.repo._recover_interrupted_jobs()
    title = world.fetch("SELECT title FROM global_ask_conversations WHERE id=?", ("conv-1",))
    assert title[0]["title"] == "我的标题"


def case_the_next_deletion_purges_expired_turns(repo):
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-b"], new_conversation=True)
    world.turn("job-2", "conv-2", ["nb-a"], new_conversation=True)
    world.delete_notebook("nb-b")
    world.expire("job-1")
    assert world.retained_until("job-1") != "missing"

    world.delete_notebook("nb-d")

    assert world.retained_until("job-1") == "missing"
    assert world.fetch("SELECT id FROM global_ask_conversations WHERE id=?", ("conv-1",)) == []
    assert world.retained_until("job-2") is None


def case_self_service_activity_follows_the_owner_rule(repo):
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-b", "nb-c"], new_conversation=True)
    world.delete_notebook("nb-b")

    def own_feed():
        feed = world.repo._runtime.queries.list_user_activity(OWNER)
        return {item["id"] for item in feed["items"]}

    assert "job-1" in own_feed()
    with world.store.guarded_admin_job_record("job-1", OWNER, reader_id=OWNER) as record:
        assert record is not None and record["job"].job_id == "job-1"
        # The detail pane degrades exactly what the global page degrades.
        assert record["job"].deleted_notebook_ids == ["nb-b"]
    world.repo._runtime.sharing_store.remove_member("nb-c", OWNER)
    assert "job-1" not in own_feed()
    with world.store.guarded_admin_job_record("job-1", OWNER, reader_id=OWNER) as record:
        assert record is None
    # The administrator keeps the audit record while it is retained.
    with world.store.guarded_admin_job_record("job-1", OWNER, reader_id=None) as record:
        assert record is not None


def case_the_bell_follows_the_conversation_read(repo):
    """The bell lists a running global ask only when its conversation's first
    page opens; that page now tolerates a deleted participant of a retained
    turn and hides an expired turn, so the bell must too (codex #785 R1's
    "one rule for both" contract)."""
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b", "nb-c"], new_conversation=True)
    world.delete_notebook("nb-b")
    running = GlobalAskJob(
        job_id="job-run", conversation_id="conv-1", status="running", question="追问",
        created_at=world.stamp(),
        notebook_scope=GlobalNotebookScope(mode="include", notebook_ids=["nb-a"]),
        resolved_notebook_ids=["nb-a"],
    )
    world.store.create(running, OWNER, None, "{}", "web", new_conversation=False)

    def bell():
        rows = world.repo._runtime.queries.pending_actions_projection_rows(OWNER)["items"]
        return [item["job_id"] for item in rows if item["type"] == "ask"]

    assert [turn.job_id for turn in world.service.conversation(
        "conv-1", user_id=OWNER).turns] == ["job-1", "job-run"]
    assert bell() == ["job-run"]
    # A live participant the owner lost hides the item, as it hides the page.
    world.repo._runtime.sharing_store.remove_member("nb-c", OWNER)
    _expect_404(lambda: world.service.conversation("conv-1", user_id=OWNER))
    assert bell() == []
    # Once that turn's window passes it is off the page, and off the bell's page.
    world.expire("job-1")
    assert [turn.job_id for turn in world.service.conversation(
        "conv-1", user_id=OWNER).turns] == ["job-run"]
    assert bell() == ["job-run"]


def case_the_read_gate_covers_every_content_reader(repo):
    """Each reader that serves a turn's content honours the stamp: the
    follow-up history, the share window, the MCP answer link, the learning
    chains' samples and the administrator's per-user totals."""
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], question="第一问", new_conversation=True)
    world.turn("job-2", "conv-1", ["nb-a", "nb-b"], question="第二问")
    world.turn("job-3", "conv-1", ["nb-a"], question="第三问")
    world.execute("UPDATE global_ask_jobs SET submitted_via='mcp', mode='reasoning'")
    world.execute("UPDATE global_ask_jobs SET status='failed' WHERE id=?", ("job-2",))
    world.delete_notebook("nb-b")
    store, ask_state = world.store, world.repo._runtime.ask_state

    def samples():
        return (
            len(ask_state.recent_user_ask_traces("nb-a", OWNER, job_limit=10, step_limit=10)),
            len(ask_state.recent_completed_ask_runs(job_limit=10, step_limit=10)),
            len(ask_state.recent_user_ask_languages(OWNER, limit=10)),
        )

    assert store.public_job("job-1", OWNER) is not None
    assert len(store.completed_history("conv-1", OWNER, 10)) == 2
    before = samples()
    usage = {row["id"]: row for row in world.repo._runtime.queries.list_user_usage()}
    assert (usage[OWNER]["questions_30d"], usage[OWNER]["questions_failed"]) == (3, 1)

    world.expire("job-1")
    world.expire("job-2")

    assert store.public_job("job-1", OWNER) is None
    assert [turn["question"] for turn in store.completed_history("conv-1", OWNER, 10)] == ["第三问"]
    assert [job["job_id"] for job in store.share_window("conv-1", OWNER)["jobs"]] == ["job-3"]
    after = samples()
    assert all(later < earlier for earlier, later in zip(before, after)), (before, after)
    usage = {row["id"]: row for row in world.repo._runtime.queries.list_user_usage()}
    assert (usage[OWNER]["questions"], usage[OWNER]["questions_30d"]) == (1, 1)
    assert usage[OWNER]["questions_failed"] == 0


def case_a_reopened_retained_conversation_can_be_continued(repo):
    """The scope the browser restores no longer names the deleted library, so
    the next follow-up is not refused for it; a scope left with nothing is not
    widened to "all"."""
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], new_conversation=True)
    world.turn("job-2", "conv-2", ["nb-b"], new_conversation=True)
    world.delete_notebook("nb-b")
    scope = world.service.conversation("conv-1", user_id=OWNER).notebook_scope
    assert (scope.mode, scope.notebook_ids) == ("include", ["nb-a"])
    untouched = world.service.conversation("conv-2", user_id=OWNER).notebook_scope
    assert (untouched.mode, untouched.notebook_ids) == ("include", ["nb-b"])


def case_attribution_rows_follow_the_turn_through_deletion_and_purge(repo):
    """The attribution rows (SQLite v98 / PostgreSQL 0078) of a turn that ran
    over a deleted library stay with the retained turn -- the other library's
    panel keeps counting it during the window -- stop counting the moment the
    window passes, and go with the turn when it is purged (FK cascade)."""
    world = build(repo)
    world.turn("job-1", "conv-1", ["nb-a", "nb-b"], new_conversation=True)
    world.turn("job-2", "conv-1", ["nb-a"])

    def rows():
        return sorted(
            (row["job_id"], row["notebook_id"])
            for row in world.fetch("SELECT job_id, notebook_id FROM global_ask_job_notebooks")
        )

    def global_answers(notebook_id):
        return world.repo._runtime.queries.notebook_analytics(notebook_id).answers_global

    assert rows() == [("job-1", "nb-a"), ("job-1", "nb-b"), ("job-2", "nb-a")]
    world.delete_notebook("nb-b")
    assert rows() == [("job-1", "nb-a"), ("job-1", "nb-b"), ("job-2", "nb-a")]
    assert global_answers("nb-a") == 2
    world.expire("job-1")
    assert global_answers("nb-a") == 1
    world.repo._recover_interrupted_jobs()
    assert rows() == [("job-2", "nb-a")]


CASES = [
    case_the_owner_still_reads_a_turn_whose_library_was_deleted,
    case_a_non_owner_and_an_allowlisted_caller_still_get_404,
    case_a_live_library_the_owner_lost_still_refuses_the_whole_turn,
    case_an_unstamped_turn_with_a_missing_library_keeps_the_strict_rule,
    case_a_public_link_keeps_its_per_open_recheck,
    case_a_later_deletion_never_extends_an_earlier_window,
    case_an_expired_turn_vanishes_then_startup_recovery_purges_it,
    case_a_renamed_title_survives_the_purge,
    case_the_next_deletion_purges_expired_turns,
    case_self_service_activity_follows_the_owner_rule,
    case_the_bell_follows_the_conversation_read,
    case_the_read_gate_covers_every_content_reader,
    case_a_reopened_retained_conversation_can_be_continued,
    case_attribution_rows_follow_the_turn_through_deletion_and_purge,
]
CASE_IDS = [case.__name__.removeprefix("case_") for case in CASES]
