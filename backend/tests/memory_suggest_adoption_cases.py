"""Backend-neutral cases for ``MemoryService``'s auto user-memory suggestion
paths and ``MemorySuggestService`` (plan T8; design §5.3, §5.4, §6.2, §6.3,
§6.4, §9.2, D12, D16, D17; plan G10, G11, G14).
SQLite: ``tests/test_memory_suggest_adoption.py``; PostgreSQL twin:
``tests/postgres/test_memory_suggest_adoption_pg.py``.

Each case takes a ``World``: the T4b store ``Env``
(``tests/memory_suggest_write_cases.py``: the backend's real ``MemoryStore``
and ``MemorySuggestStore`` plus raw SQL) and a ``MemoryService`` built over
those real stores with recording fakes for everything around them (the
embedding and KG schedulers record instead of running, the event log keeps
its events). ``MEMBER`` reads ``NB`` through a membership row and owns
``NB2``.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.memory_suggest_contracts import (
    LIFECYCLE_SUGGESTION_CREATED,
    LIFECYCLE_SUGGESTION_EXPIRED,
    REVOKED_CLAIM_LOST,
    SuggestionRevoked,
)
from app.models.memory import MemberExitCounts, MemberExitOutcome
from app.services.memory_service import (
    ExitDisclosureRequired,
    MemberExitIncomplete,
    MemoryService,
)
from app.services.memory_suggest_service import (
    MemorySuggestService,
    MemorySuggestUnavailableError,
)
from tests import memory_suggest_write_cases as writes
from tests.memory_suggest_write_cases import MEMBER, NB, NOW, OWNER, Env, suggestion

NB2 = "nb-mw-own"
#: Older than the 30-day expiry at ``NOW``.
OLD = "2026-09-20T08:00:00+00:00"
#: Inside the 30-day window at ``NOW``.
RECENT = "2026-10-20T08:00:00+00:00"
EDIT = {"title": "Edited", "content_md": "edited body", "tags": ["kept"]}


class _Events:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.logger = logging.getLogger("tests.memory_suggest_adoption")

    def emit(self, event: dict[str, Any]) -> None:
        self.items.append(dict(event))


class _Notebooks:
    """``NotebookAccessRepository`` read through raw SQL: ownership or a
    membership row reads; an answer is its conversation creator's."""

    def __init__(self, env: Env) -> None:
        self.env = env

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        return bool(self.env.rows(
            "SELECT 1 AS x FROM notebooks nb WHERE nb.id = ? AND (nb.created_by = ? OR "
            "EXISTS (SELECT 1 FROM notebook_members m WHERE m.notebook_id = nb.id "
            "AND m.user_id = ?))",
            (notebook_id, user_id, user_id),
        ))

    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        return bool(self.env.rows(
            "SELECT 1 AS x FROM notebooks WHERE id = ? AND created_by = ?",
            (notebook_id, user_id),
        ))

    def user_owns_answer(self, answer_id: str, user_id: str) -> bool:
        return bool(self.env.rows(
            "SELECT 1 AS x FROM answers a JOIN conversations c ON c.id = a.conversation_id "
            "WHERE a.id = ? AND c.created_by = ?",
            (answer_id, user_id),
        ))


class _AskState:
    def __init__(self, env: Env) -> None:
        self.env = env

    def answer_memory_source(self, answer_id: str) -> dict[str, Any]:
        found = self.env.rows(
            "SELECT notebook_id, conversation_id FROM answers WHERE id = ?", (answer_id,))
        if not found:
            raise KeyError(answer_id)
        return {
            "answer_id": answer_id, "notebook_id": found[0]["notebook_id"],
            "question": "q", "answer": "a", "conversation_id": found[0]["conversation_id"],
            "mode": "chunk", "model": "", "evidence_level": "grounded",
            "anchors": [], "citations": [],
        }


class _MemoryKg:
    def memory_kg_eligible(self, notebook_id: str) -> bool:
        return True

    def memory_source_id(self, memory_id: str) -> None:
        return None

    def remove_memory_source(self, memory_id: str) -> None:
        return None

    def remove_memory_sources(self, source_ids: list[str]) -> None:
        return None


class _Membership:
    def __init__(self, env: Env) -> None:
        self.env = env

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        self.env.run(
            "DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
            (notebook_id, user_id),
        )

    def forget_member_state(self, notebook_id: str, user_id: str) -> None:
        return None


def wiring(*, enabled: bool = True, clarified: bool = True, repeated: bool = True) -> Any:
    return SimpleNamespace(
        auto_memory_suggest_enabled=enabled,
        auto_memory_suggest_clarified_enabled=clarified,
        auto_memory_suggest_repeated_enabled=repeated,
    )


@dataclass
class World:
    env: Env
    service: MemoryService
    events: _Events
    embeds: list[str] = field(default_factory=list)
    kg: list[tuple[str, str]] = field(default_factory=list)

    def switch(self, settings: Any = None) -> MemorySuggestService:
        return MemorySuggestService(
            self.env.suggest, self.service, settings or wiring(), lambda: NOW)

    def reset(self) -> None:
        self.events.items.clear()
        self.embeds.clear()
        self.kg.clear()

    def lifecycle(self, memory_id: str) -> list[dict[str, Any]]:
        return [
            event for event in self.events.items
            if event.get("kind") == "memory_lifecycle" and event.get("memory_id") == memory_id
        ]

    def seed(self) -> None:
        self.env.seed()
        self.env.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (NB2, "Own", MEMBER, "ready", NOW, NOW),
        )

    def memory(
        self, memory_id: str, *, title: str, status: str = "confirmed",
        signal: str | None = None, answer_id: str | None = None,
        origin: str = "ask_answer", notebook_id: str = NB, created_at: str = NOW,
    ) -> None:
        self.env.run(
            "INSERT INTO memory_items(id,notebook_id,created_by,source_answer_id,origin,status,"
            "title,content_md,suggested_signal,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, notebook_id, MEMBER, answer_id, origin, status, title, "body",
             signal, created_at, created_at),
        )

    def status(self, memory_id: str) -> str | None:
        found = self.env.rows("SELECT status FROM memory_items WHERE id = ?", (memory_id,))
        return found[0]["status"] if found else None

    def suggest_rows(self, *writes_: Any, token: str = writes.TOKEN) -> list[Any]:
        """Switch on, claim a run, write ``writes_`` through the service."""
        self.env.start_run(token=token)
        result = self.service.create_suggestions(
            notebook_id=NB, owner_id=MEMBER, claim_token=token, writes=list(writes_),
            proposed_fingerprints=["fp-1"], now=NOW,
        )
        return list(result.created)


def build_world(env: Env, *, wired: bool = True) -> World:
    events = _Events()
    world = World(env=env, service=None, events=events)  # type: ignore[arg-type]
    counter = itertools.count(1)
    service = MemoryService(
        env.store,
        _AskState(env),
        _Notebooks(env),
        SimpleNamespace(dim=2, embed_texts=lambda texts: [[0.0, 1.0] for _ in texts]),
        events,  # type: ignore[arg-type]
        lambda prefix: f"{prefix}-adopt-{next(counter)}",
        lambda: NOW,
        embedding_scheduler=lambda fn, job: world.embeds.append(job.item.id),
        kg_ingest_scheduler=lambda fn, key: world.kg.append(key),
        membership=_Membership(env),
        memory_suggest_store=env.suggest if wired else None,
        suggestion_expiry_days=30,
    )
    service.set_memory_kg_service(_MemoryKg())
    world.service = service
    return world


def _side_effects(world: World, memory_id: str, log_head: int) -> dict[str, Any]:
    """Every observable effect of adopting ``memory_id``, its id left out."""
    revisions = [
        {**row, "title": row["title"].replace(memory_id, "<id>")}
        for row in world.env.rows(
            "SELECT revision, change_reason, changed_by, status, title FROM memory_revisions "
            "WHERE memory_id = ? ORDER BY revision",
            (memory_id,),
        )
    ]
    events = [
        {key: value for key, value in event.items() if key != "memory_id"}
        for event in world.lifecycle(memory_id)
    ]
    logged_provenance = writes._logged(world.env, "memory_provenance", log_head)
    logged_revisions = writes._logged(world.env, "memory_revisions", log_head)
    children = {
        row["id"] for row in world.env.rows(
            "SELECT id FROM memory_provenance WHERE memory_id = ?", (memory_id,))
    } | {
        row["id"] for row in world.env.rows(
            "SELECT id FROM memory_revisions WHERE memory_id = ?", (memory_id,))
    }
    item = world.service.get(memory_id, MEMBER)
    return {
        "revisions": revisions,
        "events": events,
        "embeds": world.embeds.count(memory_id),
        "kg": world.kg.count((memory_id, MEMBER)),
        "resent": sorted(
            operation for key, operation in logged_provenance + logged_revisions
            if key in children
        ),
        "record": (item.status, item.suggested_signal, item.title, item.content_md,
                   list(item.tags), item.confirmed_by),
    }


# ----------------------------------------------------------------- suggestion writes
def assert_create_suggestions_events_without_vector_or_kg(world: World) -> None:
    world.seed()
    created = world.suggest_rows(
        suggestion("s-clar"), suggestion("s-rep", signal="repeated", answer_id="ans-1"))
    assert [item.id for item in created] == ["s-clar", "s-rep"]
    assert [
        (event["memory_id"], event["action"], event["suggested_signal"], event["status"])
        for event in world.events.items
    ] == [
        ("s-clar", LIFECYCLE_SUGGESTION_CREATED, "clarified", "candidate"),
        ("s-rep", LIFECYCLE_SUGGESTION_CREATED, "repeated", "candidate"),
    ]
    for event in world.events.items:
        assert not {"title", "content", "content_md", "question", "answer"} & set(event)
    # A draft is neither embedded nor extracted: both wait for adoption.
    assert world.embeds == [] and world.kg == []
    # A revoked write propagates unchanged and emits nothing.
    world.reset()
    with pytest.raises(SuggestionRevoked) as revoked:
        world.service.create_suggestions(
            notebook_id=NB, owner_id=MEMBER, claim_token="tok-stale",
            writes=[suggestion("s-late")], proposed_fingerprints=[], now=NOW)
    assert revoked.value.reason == REVOKED_CLAIM_LOST
    assert world.events.items == [] and world.status("s-late") is None


# ----------------------------------------------------------------- adoption (§5.3)
def assert_two_adoption_entries_identical_side_effects(world: World) -> None:
    """The memory page's ``confirm`` and the save-from-answer CAS of an open
    suggestion leave exactly the same trail: the revision, the vector, the KG
    extraction, the event and the D15(a) re-send of provenance and revisions."""
    world.seed()
    world.env.answer("ans-a")
    world.env.answer("ans-b")
    world.suggest_rows(
        suggestion("s-page", signal="repeated", answer_id="ans-a"),
        suggestion("s-save", signal="repeated", answer_id="ans-b"),
    )
    world.env.run(
        "INSERT INTO sync_capture_control(singleton, enabled) VALUES (?, ?)", (1, True))
    world.reset()
    head = writes._log_head(world.env)
    confirmed = world.service.confirm("s-page", MEMBER, dict(EDIT))
    page = _side_effects(world, "s-page", head)
    head = writes._log_head(world.env)
    saved = world.service.create_from_answer(
        NB, MEMBER, "ans-b", EDIT["title"], EDIT["content_md"], EDIT["tags"])
    save = _side_effects(world, "s-save", head)
    assert (confirmed.id, saved.id) == ("s-page", "s-save")
    assert page == save
    assert page["revisions"] == [
        {"revision": 1, "change_reason": "suggested", "changed_by": MEMBER,
         "status": "candidate", "title": "title <id>"},
        {"revision": 2, "change_reason": "confirmed", "changed_by": MEMBER,
         "status": "confirmed", "title": "Edited"},
    ]
    assert page["embeds"] == 1 and page["kg"] == 1
    assert [(event["action"], event["suggested_signal"]) for event in page["events"]] == [
        ("confirmed", "repeated")]
    # Provenance and BOTH revisions re-sent (the new revision is also logged
    # by its own insert).
    assert page["resent"].count("upsert") >= 3
    assert page["record"] == ("confirmed", "repeated", "Edited", "edited body", ["kept"],
                              MEMBER)


def assert_save_from_answer_adopts_open_suggestion(world: World) -> None:
    """§5.3 row 1: the open suggestion bound to the answer becomes the saved
    memory, with what the user submitted; no second row."""
    world.seed()
    world.env.answer("ans-1")
    world.suggest_rows(suggestion("s-open", signal="repeated", answer_id="ans-1"))
    world.reset()
    saved = world.service.create_from_answer(
        NB, MEMBER, "ans-1", "Mine now", "my words", ["t"], extract_kg=False)
    assert (saved.id, saved.status, saved.suggested_signal) == ("s-open", "confirmed",
                                                               "repeated")
    assert (saved.title, saved.content_md, saved.tags) == ("Mine now", "my words", ["t"])
    assert world.env.memory_ids() == {"s-open"}
    assert world.embeds == ["s-open"]
    assert world.kg == []  # the checkbox was off
    # Saving again is the plain idempotent return (row 3 now).
    world.reset()
    again = world.service.create_from_answer(NB, MEMBER, "ans-1", "x", "y", [])
    assert (again.id, again.title) == ("s-open", "Mine now")
    assert world.events.items == [] and world.embeds == []


def assert_save_from_answer_replaces_dismissed_suggestion(world: World) -> None:
    """§5.3 row 2 / D16: a dismissed (or expired) suggestion bound to the
    answer is replaced by a plain saved memory; the fingerprint stays."""
    world.seed()
    world.env.answer("ans-1")
    world.suggest_rows(suggestion("s-old", signal="repeated", answer_id="ans-1"))
    world.service.reject("s-old", MEMBER)
    world.reset()
    saved = world.service.create_from_answer(NB, MEMBER, "ans-1", "Saved", "body", ["a"])
    assert saved.id != "s-old"
    assert (saved.status, saved.suggested_signal, saved.source_answer_id) == (
        "confirmed", None, "ans-1")
    assert world.status("s-old") is None
    assert world.env.memory_ids() == {saved.id}
    assert [event["action"] for event in world.lifecycle(saved.id)] == ["answer_confirmed"]
    assert world.embeds == [saved.id] and world.kg == [(saved.id, MEMBER)]
    assert world.env.fingerprints() == {"fp-1": "proposed"}
    # The replacement still needs the author: another member's answer is refused.
    world.env.run(
        "INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("conv-owner", NB, "t", OWNER, NOW, NOW),
    )
    world.env.run(
        "INSERT INTO answers(id,notebook_id,question,payload,created_at,conversation_id) "
        "VALUES (?,?,?,?,?,?)",
        ("ans-foreign", NB, "q", world.env.json({"answer": "a"}), NOW, "conv-owner"),
    )
    world.memory("s-foreign", title="draft", status="rejected", signal="repeated",
                 answer_id="ans-foreign")
    with pytest.raises(PermissionError):
        world.service.create_from_answer(NB, MEMBER, "ans-foreign", "t", "c", [])
    assert world.status("s-foreign") == "rejected"


def assert_save_from_answer_returns_other_rows_unchanged(world: World) -> None:
    """§5.3 row 3: a manual memory, an agent candidate, an adopted (even
    deprecated) suggestion -- returned as they are, nothing written."""
    world.seed()
    world.memory("manual", title="manual", answer_id="ans-m")
    world.memory("agent", title="agent", status="candidate", origin="external_agent",
                 answer_id="ans-g")
    world.memory("adopted", title="adopted", signal="repeated", answer_id="ans-a")
    world.memory("retired", title="retired", status="deprecated", signal="clarified",
                 answer_id="ans-d")
    for answer_id, memory_id, status in (
        ("ans-m", "manual", "confirmed"), ("ans-g", "agent", "candidate"),
        ("ans-a", "adopted", "confirmed"), ("ans-d", "retired", "deprecated"),
    ):
        found = world.service.create_from_answer(NB, MEMBER, answer_id, "new", "new", [])
        assert (found.id, found.status, found.title) == (memory_id, status, memory_id)
    assert world.events.items == [] and world.embeds == [] and world.kg == []
    # Another notebook's row for the answer is a 404, as before.
    with pytest.raises(KeyError):
        world.service.create_from_answer(NB2, MEMBER, "ans-m", "new", "new", [])


def assert_save_from_answer_concurrent_insert_returns_existing(world: World) -> None:
    """§5.3 row 4: the save read no row, then another save of the same
    answer landed first -- the insert's answer-once conflict returns that row:
    no-op, no error, no event."""
    world.seed()
    world.env.answer("ans-1")
    store = world.env.store
    first = world.service.create_from_answer(NB, MEMBER, "ans-1", "first", "body", [])
    world.reset()
    store.memory_by_answer = lambda user_id, answer_id: None  # the stale read
    try:
        again = world.service.create_from_answer(NB, MEMBER, "ans-1", "second", "body", [])
    finally:
        del store.memory_by_answer
    assert (again.id, again.title) == (first.id, "first")
    assert world.env.memory_ids() == {first.id}
    assert world.events.items == [] and world.embeds == [] and world.kg == []


def assert_clarified_suggestion_does_not_occupy_answer_index(world: World) -> None:
    """A clarified-class suggestion binds no answer (``source_answer_id`` is
    NULL): saving the answer it was drawn from makes a plain new memory and
    leaves the draft alone."""
    world.seed()
    world.env.answer("ans-1")
    [draft] = world.suggest_rows(replace(
        suggestion("s-clar"),
        provenance={"answer_id": "ans-1", "suggestion": {"signal": "clarified"}}))
    assert draft.source_answer_id is None
    saved = world.service.create_from_answer(NB, MEMBER, "ans-1", "Saved", "body", [])
    assert saved.id != "s-clar"
    assert (saved.status, saved.suggested_signal, saved.source_answer_id) == (
        "confirmed", None, "ans-1")
    assert world.status("s-clar") == "candidate"


def assert_answer_memory_link_view(world: World) -> None:
    world.seed()
    world.memory("manual", title="m", answer_id="ans-m")
    world.memory("draft", title="d", status="candidate", signal="repeated", answer_id="ans-d")
    view = world.service.answer_memory_link_view(NB, MEMBER, ["ans-m", "ans-d", "ans-none"])
    assert view["links"] == {"ans-m": "manual"}
    assert view["states"] == {
        "ans-m": {"memory_id": "manual", "status": "confirmed", "suggested": False},
        "ans-d": {"memory_id": "draft", "status": "candidate", "suggested": True},
    }
    with pytest.raises(PermissionError):
        world.service.answer_memory_link_view("nb-none", MEMBER, ["ans-m"])


def assert_origin_filters_keep_manual_answers_apart_from_all_suggestion_states(world: World) -> None:
    """List/page/count agree before and after lexical matching, while answer
    states retain the stored suggestion identity even after adoption/dismissal.
    """
    world.seed()
    statuses = ("candidate", "confirmed", "rejected", "deprecated")
    groups = {"ask_answer": set(), "suggested": set(), "external_agent": set()}
    expected_states = {}
    for status in statuses:
        for group, signal, origin in (("ask_answer", None, "ask_answer"),
                                      ("suggested", "repeated", "ask_answer"),
                                      ("external_agent", None, "external_agent")):
            memory_id = f"{group}-{status}"
            answer_id = f"ans-{memory_id}"
            world.memory(memory_id, title="clock gating", status=status, signal=signal,
                         origin=origin, answer_id=answer_id)
            groups[group].add(memory_id)
            expected_states[answer_id] = {"memory_id": memory_id, "status": status, "suggested": signal is not None}
    for query in ("", "clock"):
        for status in (None, *statuses):
            for origin, group in groups.items():
                expected = {memory_id for memory_id in group if status is None or memory_id.endswith(status)}
                page = world.env.store.list_memories(MEMBER, notebook_id=NB, status=status,
                                                    origin=origin, query=query, offset=0, limit=2)
                second = world.env.store.list_memories(MEMBER, notebook_id=NB, status=status,
                                                      origin=origin, query=query, offset=2, limit=2)
                assert page.total_count == second.total_count == len(expected), (query, status, origin)
                assert {row.id for row in (*page.items, *second.items)} == expected, (query, status, origin)
    view = world.service.answer_memory_link_view(NB, MEMBER, list(expected_states))
    assert view["states"] == expected_states
    assert not any(answer_id.startswith("ans-suggested-") for answer_id in view["links"]
                   if expected_states[answer_id]["status"] in {"candidate", "rejected"})


# ----------------------------------------------------------------- exit (G10)
def assert_exit_with_only_drafts_ends_membership(world: World) -> None:
    world.seed()
    world.memory("draft-open", title="d1", status="candidate", signal="clarified")
    world.memory("draft-dismissed", title="d2", status="rejected", signal="repeated")
    assert world.service.exit_disclosure(NB, MEMBER) == MemberExitCounts(0, 2)
    outcome = world.service.leave_notebook(NB, MEMBER, 0)
    assert outcome == MemberExitOutcome(deleted_memory_count=0, deleted_suggestion_count=2, ended=True)
    assert not world.env.is_member()
    assert world.env.memory_ids() == set()


def assert_exit_ack_compares_your_memories_only(world: World) -> None:
    world.seed()
    world.memory("own", title="own")
    world.memory("draft-open", title="d1", status="candidate", signal="clarified")
    world.memory("draft-dismissed", title="d2", status="rejected", signal="repeated")
    assert world.service.exit_disclosure(NB, MEMBER) == MemberExitCounts(1, 2)
    for ack in (None, 0, 3, 2):
        with pytest.raises(ExitDisclosureRequired) as required:
            world.service.leave_notebook(NB, MEMBER, ack)
        assert (required.value.memory_count, required.value.suggestion_count) == (1, 2)
        assert world.env.is_member()
        assert world.env.memory_ids() == {"own", "draft-open", "draft-dismissed"}
    outcome = world.service.leave_notebook(NB, MEMBER, 1)
    assert outcome == MemberExitOutcome(deleted_memory_count=1, deleted_suggestion_count=2, ended=True)
    assert not world.env.is_member()
    assert world.env.memory_ids() == set()
    # The owner keeps reading: zeros, nothing deleted.
    assert world.service.exit_disclosure(NB, OWNER) == MemberExitCounts(0, 0)


class AdoptAfterClaim:
    """The service's store, adopting ``memory_id`` (the memory page's
    ``confirm``) right after the exit's claim returns -- the window between
    the claim and the purge."""

    def __init__(self, world: World, memory_id: str) -> None:
        self.inner = world.env.store
        self.world = world
        self.memory_id = memory_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def member_exit_snapshot(self, notebook_id: str, user_id: str, *, claim: bool) -> Any:
        snapshot = self.inner.member_exit_snapshot(notebook_id, user_id, claim=claim)
        if claim:
            self.world.service.confirm(self.memory_id, user_id, dict(EDIT))
        return snapshot


def assert_exit_keeps_a_suggestion_adopted_after_the_claim(world: World) -> None:
    """Review P2-1: the claim does not take the drafts by id, so a draft
    adopted before the purge survives the exit; the membership stays and the
    counts are exact (no guessing which deleted rows were drafts)."""
    world.seed()
    world.memory("own", title="own")
    world.memory("draft-a", title="a", status="candidate", signal="clarified")
    world.memory("draft-b", title="b", status="rejected", signal="repeated")
    assert world.service.exit_disclosure(NB, MEMBER) == MemberExitCounts(1, 2)
    world.service.store = AdoptAfterClaim(world, "draft-a")
    with pytest.raises(MemberExitIncomplete) as incomplete:
        world.service.leave_notebook(NB, MEMBER, 1)
    assert (
        incomplete.value.deleted_memory_count,
        incomplete.value.memory_count,
        incomplete.value.deleted_suggestion_count,
    ) == (1, 1, 0)
    assert world.env.is_member()
    assert world.status("draft-a") == "confirmed"
    assert world.status("draft-b") == "rejected"
    assert world.status("own") is None
    # The next disclosure and exit see the adopted one as "your memory".
    world.service.store = world.env.store
    assert world.service.exit_disclosure(NB, MEMBER) == MemberExitCounts(1, 1)
    outcome = world.service.leave_notebook(NB, MEMBER, 1)
    assert outcome == MemberExitOutcome(deleted_memory_count=1, deleted_suggestion_count=1, ended=True)
    assert not world.env.is_member()
    assert world.env.memory_ids() == set()


def assert_exit_of_only_drafts_keeps_one_adopted_after_the_claim(world: World) -> None:
    """C = 0: nothing to purge, the adopted draft makes the finish refuse --
    a 409 with the server's numbers, and truly nothing deleted."""
    world.seed()
    world.memory("draft-a", title="a", status="candidate", signal="clarified")
    world.memory("draft-b", title="b", status="rejected", signal="repeated")
    world.service.store = AdoptAfterClaim(world, "draft-a")
    with pytest.raises(ExitDisclosureRequired) as required:
        world.service.leave_notebook(NB, MEMBER, 0)
    assert (required.value.memory_count, required.value.suggestion_count) == (1, 1)
    assert world.env.is_member()
    assert world.env.memory_ids() == {"draft-a", "draft-b"}


def assert_exit_ack_mismatch_leaves_the_job_row(world: World) -> None:
    """Review P3-4: a 409 ``exit_disclosure_required`` deletes nothing at all
    -- the suggestion job row included; the exit that finishes deletes it."""
    world.seed()
    world.memory("own", title="own")
    world.env.start_run()
    with pytest.raises(ExitDisclosureRequired):
        world.service.leave_notebook(NB, MEMBER, 0)
    assert world.env.job_rows() == [{"status": "running", "claim_token": writes.TOKEN}]
    assert world.service.leave_notebook(NB, MEMBER, 1) == MemberExitOutcome(1, 0, ended=True)
    assert world.env.job_rows() == []


# ----------------------------------------------------------------- export (§6.3, §9.2)
def _export_text(world: World) -> str:
    return "".join(world.service.export_markdown(NB, MEMBER).pieces)


def assert_export_header_and_type_labels(world: World) -> None:
    world.seed()
    world.memory("manual", title="Manual one", answer_id="ans-m")
    world.memory("agent", title="Agent one", status="candidate", origin="external_agent")
    world.memory("adopted", title="Adopted one", signal="repeated", answer_id="ans-a")
    world.memory("retired", title="Retired one", status="deprecated", signal="clarified")
    world.memory("draft-open", title="Open draft", status="candidate", signal="clarified")
    world.memory("draft-dismissed", title="Dismissed draft", status="rejected",
                 signal="repeated")
    text = _export_text(world)
    header = text.split("\n---\n", 1)[0]
    assert "另有 2 条未处理的系统建议未导出。" in header
    sections = {
        part.split("\n", 1)[0].split(". ", 1)[1]: part
        for part in text.split("\n## ")[1:]
    }
    assert set(sections) == {"Manual one", "Agent one", "Adopted one", "Retired one"}
    assert "- 类型：问答保存" in sections["Manual one"]
    assert "- 类型：Agent 提议" in sections["Agent one"]
    assert "- 类型：系统建议（已采纳）" in sections["Adopted one"]
    assert "- 类型：系统建议（已采纳）" in sections["Retired one"]
    assert "Open draft" not in text and "Dismissed draft" not in text
    assert "共导出 4 条记忆。" in text
    # No drafts: no such line at all.
    world.env.run(
        "DELETE FROM memory_items WHERE id IN (?, ?)", ("draft-open", "draft-dismissed"))
    assert "未处理的系统建议" not in _export_text(world)


# ----------------------------------------------------------------- transfer (§6.3)
def assert_transfer_rejects_unadopted(world: World) -> None:
    world.seed()
    world.memory("draft-open", title="d1", status="candidate", signal="clarified")
    world.memory("draft-dismissed", title="d2", status="rejected", signal="repeated")
    world.memory("adopted", title="a", signal="repeated", answer_id="ans-a")
    results = world.service.transfer(
        MEMBER, ["draft-open", "draft-dismissed", "adopted"], NB2, "copy")
    by_id = {result["source_id"]: result for result in results}
    for memory_id in ("draft-open", "draft-dismissed"):
        assert by_id[memory_id]["ok"] is False
        assert by_id[memory_id]["status"] == "failed"
        assert by_id[memory_id]["new_id"] is None
        assert "只能传输 confirmed" in by_id[memory_id]["error"]
    assert by_id["adopted"]["ok"] is True
    assert world.env.rows(
        "SELECT COUNT(*) AS n FROM memory_items WHERE notebook_id = ?", (NB2,)
    )[0]["n"] == 1


# ----------------------------------------------------------------- switch (§6.2, G14)
def assert_enable_refused_when_wiring_off(world: World) -> None:
    world.seed()
    for settings in (wiring(enabled=False), wiring(clarified=False, repeated=False)):
        with pytest.raises(MemorySuggestUnavailableError):
            world.switch(settings).set_mode(MEMBER, "suggest")
        assert world.env.suggest.user_state(MEMBER) is None
    # Turning it off is always allowed.
    change = world.switch(wiring(enabled=False)).set_mode(MEMBER, "manual")
    assert (change.memory_mode, change.deleted_suggestion_count) == ("manual", 0)
    with pytest.raises(ValueError):
        world.switch().set_mode(MEMBER, "auto")
    change = world.switch().set_mode(MEMBER, "suggest")
    assert change.memory_mode == "suggest"
    assert world.env.suggest.user_state(MEMBER)["enabled"]


def assert_disable_delete_unadopted_removes_only_candidates_keeps_rejected(
    world: World,
) -> None:
    world.seed()
    world.memory("draft-open", title="d1", status="candidate", signal="clarified")
    world.memory("draft-own-nb", title="d2", status="candidate", signal="repeated",
                 notebook_id=NB2)
    world.memory("draft-dismissed", title="d3", status="rejected", signal="repeated")
    world.memory("adopted", title="a", signal="repeated", answer_id="ans-a")
    world.memory("agent", title="g", status="candidate", origin="external_agent")
    world.memory("manual", title="m")
    world.memory("manual-rejected", title="r", status="rejected")
    # More open drafts than one delete page holds.
    for index in range(203):
        world.memory(f"bulk-{index:03d}", title="b", status="candidate", signal="clarified",
                     notebook_id=NB2)
    switch = world.switch()
    switch.set_mode(MEMBER, "suggest")
    # Turning off without asking deletes nothing.
    assert switch.set_mode(MEMBER, "manual").deleted_suggestion_count == 0
    switch.set_mode(MEMBER, "suggest")
    change = switch.set_mode(MEMBER, "manual", delete_unadopted=True)
    assert (change.memory_mode, change.deleted_suggestion_count) == ("manual", 205)
    assert not world.env.suggest.user_state(MEMBER)["enabled"]
    left = {
        row["id"] for row in world.env.rows(
            "SELECT id FROM memory_items WHERE created_by = ?", (MEMBER,))
    }
    assert left == {"draft-dismissed", "adopted", "agent", "manual", "manual-rejected"}
    # Turning ON with the flag set deletes nothing.
    world.memory("draft-new", title="n", status="candidate", signal="clarified")
    assert switch.set_mode(MEMBER, "suggest", delete_unadopted=True) == type(change)(
        "suggest", 0)
    assert world.status("draft-new") == "candidate"


def assert_disable_delete_keeps_a_suggestion_adopted_after_the_page_read(
    world: World,
) -> None:
    """The owner adopts an open suggestion in another tab after the delete
    loop read its page and before the delete runs: the conditional delete
    re-checks the locked row and keeps the now-confirmed memory."""
    world.seed()
    world.memory("draft-a", title="a", status="candidate", signal="clarified")
    world.memory("draft-b", title="b", status="candidate", signal="clarified")

    class AdoptBetweenReadAndDelete:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.adopted = False

        def list_memories(self, *args: Any, **kwargs: Any) -> Any:
            page = self.inner.list_memories(*args, **kwargs)
            if not self.adopted:
                self.adopted = True
                world.env.run(
                    "UPDATE memory_items SET status='confirmed' WHERE id=?", ("draft-a",))
            return page

        def delete_open_suggestions(self, user_id: str, memory_ids: Any) -> int:
            return self.inner.delete_open_suggestions(user_id, memory_ids)

    switch = MemorySuggestService(
        world.env.suggest, AdoptBetweenReadAndDelete(world.service), wiring(), lambda: NOW)
    switch.set_mode(MEMBER, "suggest")
    change = switch.set_mode(MEMBER, "manual", delete_unadopted=True)
    assert change.deleted_suggestion_count == 1
    assert world.status("draft-a") == "confirmed"
    assert world.status("draft-b") is None


# ----------------------------------------------------------------- expiry (§5.4, G11)
def assert_expire_suggestions_ignores_deployment_switch_and_emits_events(
    world: World,
) -> None:
    world.seed()
    created = world.suggest_rows(
        replace(suggestion("s-old-clar"), created_at=OLD, updated_at=OLD),
        replace(suggestion("s-old-rep", signal="repeated", answer_id="ans-1"),
                created_at=OLD, updated_at=OLD),
        replace(suggestion("s-recent"), created_at=RECENT, updated_at=RECENT),
    )
    assert len(created) == 3
    world.memory("agent-old", title="g", status="candidate", origin="external_agent",
                 created_at=OLD)
    world.memory("adopted-old", title="a", signal="clarified", created_at=OLD)
    # The deployment switch is off and the owner turned suggestions off: the
    # drafts still expire on schedule.
    world.switch(wiring(enabled=False)).set_mode(MEMBER, "manual")
    world.reset()
    assert world.service.expire_suggestions(now=NOW, limit=500) == 2
    assert world.status("s-old-clar") == world.status("s-old-rep") == "rejected"
    assert world.status("s-recent") == "candidate"
    assert world.status("agent-old") == "candidate"
    assert world.status("adopted-old") == "confirmed"
    assert sorted(
        (event["memory_id"], event["action"], event["suggested_signal"], event["status"],
         event["owner_id"], event["notebook_id"])
        for event in world.events.items
    ) == [
        ("s-old-clar", LIFECYCLE_SUGGESTION_EXPIRED, "clarified", "rejected", MEMBER, NB),
        ("s-old-rep", LIFECYCLE_SUGGESTION_EXPIRED, "repeated", "rejected", MEMBER, NB),
    ]
    reasons = world.env.rows(
        "SELECT change_reason FROM memory_revisions WHERE memory_id = ? ORDER BY revision",
        ("s-old-clar",),
    )
    assert [row["change_reason"] for row in reasons] == ["suggested", "expired"]
    # Idempotent; and a narrower scope that matches nothing moves nothing.
    assert world.service.expire_suggestions(now=NOW, limit=500) == 0
    assert world.service.expire_suggestions(now=NOW, limit=500, notebook_id=NB2) == 0
    # Not wired: a no-op.
    assert build_world(world.env, wired=False).service.expire_suggestions(
        now=NOW, limit=500) == 0
