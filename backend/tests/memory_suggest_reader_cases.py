"""Backend-neutral cases for the Memory store readers that leave unadopted
system suggestions out of "your memories" (auto user-memory plan T4a, design
§6.3). SQLite: ``tests/test_memory_suggest_readers.py`` (through
``repo._runtime.memory_store``); PostgreSQL twin:
``tests/postgres/test_memory_suggest_readers_pg.py``.

An unadopted suggestion is a row with a ``suggested_signal`` in ``candidate``
or ``rejected``; an adopted one (``confirmed`` / ``deprecated``) is the
user's memory like any other.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

OWNER = "u-mr-owner"
FRIEND = "u-mr-friend"
NB = "nb-mr"
NB_GROUP = "nb-mr-group"
NB_DRAFTS = "nb-mr-drafts"
GROUP = "grp-mr"
NOW = "2026-10-29T08:00:00+00:00"


def at(minute: int) -> str:
    return f"2026-10-29T08:{minute:02d}:00+00:00"


@dataclass
class Env:
    store: Any
    run: Callable[[str, tuple], None]
    rows: Callable[[str, tuple], list[dict]]

    def seed(self) -> None:
        for user in (OWNER, FRIEND):
            self.run(
                "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", NOW, NOW),
            )
        self.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (NB, "Mine", OWNER, "ready", NOW, NOW),
        )

    def memory(
        self,
        memory_id: str,
        *,
        status: str = "confirmed",
        signal: str | None = None,
        notebook_id: str = NB,
        title: str = "t",
        minute: int = 0,
        answer_id: str | None = None,
        owner: str = OWNER,
    ) -> None:
        self.run(
            "INSERT INTO memory_items(id,notebook_id,created_by,source_answer_id,origin,status,"
            "title,content_md,suggested_signal,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, notebook_id, owner, answer_id, "ask_answer", status, title, "c",
             signal, at(minute), at(minute)),
        )

    def mixed(self) -> None:
        """Two of the user's own memories, one adopted suggestion, and three
        unadopted ones (two open, one dismissed)."""
        self.memory("own-confirmed", minute=1)
        self.memory("own-candidate", status="candidate", minute=2)
        self.memory("adopted", status="confirmed", signal="clarified", minute=3)
        self.memory("draft-open-1", status="candidate", signal="clarified", minute=4)
        self.memory("draft-open-2", status="candidate", signal="repeated", minute=5)
        self.memory("draft-dismissed", status="rejected", signal="repeated", minute=6)


def assert_overview_excludes_unadopted(env: Env) -> None:
    env.seed()
    env.mixed()
    overview = env.store.notebook_content_overview(OWNER, NB)
    assert (overview["total"], overview["confirmed"], overview["candidate"]) == (3, 2, 1)
    assert [row["id"] for row in overview["recent"]] == [
        "adopted", "own-candidate", "own-confirmed"]


def assert_adopted_suggestion_counts_as_yours(env: Env) -> None:
    env.seed()
    env.memory("adopted-confirmed", status="confirmed", signal="clarified", minute=1)
    env.memory("adopted-deprecated", status="deprecated", signal="repeated", minute=2)
    overview = env.store.notebook_content_overview(OWNER, NB)
    assert (overview["total"], overview["confirmed"], overview["candidate"]) == (2, 1, 0)
    snapshot = env.store.memory_export_snapshot(
        NB, OWNER, page_size=100, max_bytes=1_000_000)
    assert {item.id for item in snapshot.items} == {"adopted-confirmed", "adopted-deprecated"}
    assert snapshot.omitted_suggestion_count == 0
    assert {item.suggested_signal for item in snapshot.items} == {"clarified", "repeated"}


def assert_export_omits_unadopted_and_reports_count(env: Env) -> None:
    env.seed()
    env.mixed()
    snapshot = env.store.memory_export_snapshot(NB, OWNER, page_size=2, max_bytes=1_000_000)
    assert [item.id for item in snapshot.items] == [
        "own-confirmed", "own-candidate", "adopted"]
    assert snapshot.omitted_suggestion_count == 3


def assert_group_exit_preview_counts_only_your_memories(env: Env) -> None:
    """The friend reads two notebooks only through a group: one holds a memory
    of theirs and a draft (counted 1), the other only drafts (not listed)."""
    env.seed()
    for notebook_id in (NB_GROUP, NB_DRAFTS):
        env.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (notebook_id, notebook_id, OWNER, "ready", NOW, NOW),
        )
    env.run(
        "INSERT INTO groups(id,name,kind,created_by,owner_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (GROUP, "g", "project", OWNER, OWNER, NOW, NOW),
    )
    env.run(
        "INSERT INTO group_members(group_id,user_id,role,added_at) VALUES (?,?,?,?)",
        (GROUP, FRIEND, "member", NOW),
    )
    for index, notebook_id in enumerate((NB_GROUP, NB_DRAFTS)):
        env.run(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?)",
            (f"grant-{index}", notebook_id, "group", GROUP, "reader", OWNER, NOW),
        )
    env.memory("f-own", notebook_id=NB_GROUP, owner=FRIEND)
    env.memory("f-draft", notebook_id=NB_GROUP, owner=FRIEND, status="candidate",
               signal="clarified")
    env.memory("f-draft-only", notebook_id=NB_DRAFTS, owner=FRIEND, status="rejected",
               signal="repeated")
    preview = env.store.group_exit_memory_preview(GROUP, FRIEND, now=NOW)
    assert [(row["notebook_id"], row["memory_count"]) for row in preview] == [(NB_GROUP, 1)]


def assert_answer_links_exclude_drafts_states_include_them(env: Env) -> None:
    env.seed()
    env.memory("m-manual", answer_id="ans-manual")
    env.memory("m-draft", status="candidate", signal="repeated", answer_id="ans-draft")
    env.memory("m-dismissed", status="rejected", signal="repeated", answer_id="ans-dismissed")
    env.memory("m-adopted", status="confirmed", signal="repeated", answer_id="ans-adopted")
    answers = ["ans-manual", "ans-draft", "ans-dismissed", "ans-adopted", "ans-none"]
    links = env.store.answer_memory_links(NB, OWNER, answers)
    assert links == {"ans-manual": "m-manual", "ans-adopted": "m-adopted"}
    both = env.store.answer_memory_link_rows(NB, OWNER, answers)
    assert both["links"] == links
    assert both["states"] == {
        "ans-manual": {"memory_id": "m-manual", "status": "confirmed", "suggested": False},
        "ans-draft": {"memory_id": "m-draft", "status": "candidate", "suggested": True},
        "ans-dismissed": {"memory_id": "m-dismissed", "status": "rejected", "suggested": True},
        "ans-adopted": {"memory_id": "m-adopted", "status": "confirmed", "suggested": True},
    }
    assert env.store.answer_memory_link_rows(NB, OWNER, []) == {"links": {}, "states": {}}


def assert_origin_filter_suggested_totals_match_with_and_without_query(env: Env) -> None:
    env.seed()
    env.memory("plain", title="voltage regulator plain", minute=1)
    env.memory("sug-open", status="candidate", signal="clarified",
               title="voltage regulator open", minute=2)
    env.memory("sug-adopted", signal="repeated", title="voltage regulator adopted", minute=3)
    env.memory("sug-other", status="candidate", signal="clarified",
               title="thermal design notes", minute=4)

    def listed(**kwargs):
        page = env.store.list_memories(
            OWNER, notebook_id=NB, status=kwargs.get("status"),
            origin=kwargs.get("origin"), query=kwargs.get("query", ""),
            offset=0, limit=50)
        return page.total_count, {item.id for item in page.items}

    assert listed(origin="suggested") == (3, {"sug-open", "sug-adopted", "sug-other"})
    assert listed(origin="suggested", query="voltage regulator") == (
        2, {"sug-open", "sug-adopted"})
    assert listed(origin="suggested", status="candidate") == (2, {"sug-open", "sug-other"})
    assert listed(origin="suggested", status="candidate", query="voltage regulator") == (
        1, {"sug-open"})
    # Stored origin stays ask_answer, but the public filter means manually
    # saved answers and excludes every system suggestion, including adopted.
    assert listed(origin="ask_answer") == (1, {"plain"})
    assert listed(origin="ask_answer", query="voltage regulator") == (1, {"plain"})
    assert listed(origin="ask_answer", status="candidate") == (0, set())


def assert_readers_unchanged_without_suggestions(env: Env, old_overview_sql: str) -> None:
    """With no suggestion in the library, the rewritten overview returns
    exactly what the pre-suggestion statement returns."""
    env.seed()
    env.memory("a", minute=1)
    env.memory("b", status="candidate", minute=2)
    env.memory("c", status="rejected", minute=3)
    env.memory("d", status="deprecated", minute=4)
    old = env.rows(old_overview_sql, (OWNER, NB))[0]
    new = env.store.notebook_content_overview(OWNER, NB)
    assert (new["total"], new["confirmed"], new["candidate"]) == (
        int(old["total"]), int(old["confirmed"]), int(old["candidate"]))
    assert [row["id"] for row in new["recent"]] == ["b", "a"]


OLD_OVERVIEW_SQL = (
    "SELECT COUNT(*) AS total,"
    "COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END),0) AS confirmed,"
    "COALESCE(SUM(CASE WHEN status='candidate' THEN 1 ELSE 0 END),0) AS candidate "
    "FROM memory_items WHERE created_by=? AND notebook_id=?"
)
