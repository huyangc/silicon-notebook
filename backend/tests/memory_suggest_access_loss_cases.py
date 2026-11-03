"""Backend-neutral cases: every way a person loses read access to a notebook
deletes their auto-memory suggestion job row there (plan T10 / G17), and
getting access back starts a new sampling window. SQLite:
``tests/test_memory_suggest_access_loss.py``; PostgreSQL twin:
``tests/postgres/test_memory_suggest_access_loss_pg.py``.

Each case takes an ``Env`` holding the repository runtime (``rt``: ``sharing``,
``groups``, ``memory_suggest_store``) and ``run`` / ``rows`` for seeding with
``?`` placeholders.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

OWNER = "u-al-owner"
FRIEND = "u-al-friend"
NB = "nb-al"
GROUP = "grp-al"
GROUP_B = "grp-al-b"
NOW = "2026-10-29T09:00:00+00:00"


def at(minute: int) -> str:
    return f"2026-10-29T09:{minute:02d}:00+00:00"


@dataclass
class Env:
    rt: Any
    run: Callable[[str, tuple], None]
    rows: Callable[[str, tuple], list[dict]]

    @property
    def store(self) -> Any:
        return self.rt.memory_suggest_store

    def seed(self) -> None:
        # Synthetic permission events share the same clock as the synthetic asks.
        self.rt.groups.now = self.rt.sharing._store.now = self.rt.memory_store.now = lambda: NOW
        for user in (OWNER, FRIEND):
            self.run(
                "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", NOW, NOW),
            )
        self.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (NB, "n", OWNER, "ready", NOW, NOW),
        )
        self.store.set_memory_mode(FRIEND, "suggest", now=at(0))

    def member(self, user: str = FRIEND) -> None:
        self.run(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
            (NB, user, "reader", NOW),
        )

    def group(self, *, friend_role: str = "member") -> None:
        self.run(
            "INSERT INTO groups(id,name,kind,created_by,owner_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (GROUP, "g", "project", OWNER, OWNER, NOW, NOW),
        )
        for user, role in ((OWNER, "admin"), (FRIEND, friend_role)):
            self.run(
                "INSERT INTO group_members(group_id,user_id,role,added_at) VALUES (?,?,?,?)",
                (GROUP, user, role, NOW),
            )

    def grant(self, grant_id: str, principal_type: str, principal_id: str) -> None:
        self.run(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?)",
            (grant_id, NB, principal_type, principal_id, "viewer", OWNER, NOW),
        )

    def ask(self, job_id: str, minute: int) -> None:
        self.run(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, NB, "conv", FRIEND, "reasoning", "q", "done", "", at(minute),
             at(minute), "web", "answer", ""),
        )

    def signal(self, ask_job_id: str = "", minute: int = 1) -> int:
        return self.store.note_signal(NB, FRIEND, ask_job_id=ask_job_id, now=at(minute))

    def has_job(self, user: str = FRIEND) -> bool:
        return bool(self.rows(
            "SELECT 1 FROM memory_suggest_jobs WHERE notebook_id = ? AND owner_id = ?",
            (NB, user),
        ))

    def cursor(self) -> tuple[datetime, str]:
        row = self.rows(
            "SELECT cursor_created_at, cursor_id FROM memory_suggest_jobs "
            "WHERE notebook_id = ? AND owner_id = ?",
            (NB, FRIEND),
        )[0]
        value = row["cursor_created_at"]
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return stamp.astimezone(timezone.utc), row["cursor_id"]


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def assert_remove_member_clears_job(env: Env) -> None:
    env.seed()
    env.member()
    env.signal()
    assert env.has_job()
    env.rt.sharing.remove_member(NB, FRIEND)
    assert not env.has_job()


def assert_kick_all_clears_jobs(env: Env) -> None:
    env.seed()
    env.run("UPDATE notebooks SET is_shared=1, share_token='tok-al' WHERE id=?", (NB,))
    env.member()
    env.signal()
    env.rt.sharing.unshare_notebook(NB)
    assert not env.has_job()


def assert_kick_all_samples_loss_clock_after_member_deletion(env: Env) -> None:
    env.seed()
    env.member()
    env.run("UPDATE notebooks SET is_shared=1,share_token='clock-token' WHERE id=?", (NB,))
    env.ask("before-unshare", 4)
    env.signal("before-unshare", minute=4)
    removed = False

    class ObservedConnection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, params=(), **kwargs):
            nonlocal removed
            result = self.inner.execute(sql, params, **kwargs)
            if " ".join(sql.split()).startswith("DELETE FROM notebook_members "):
                removed = True
            return result

    class ObservedDatabase:
        @contextmanager
        def write(self, **kwargs):
            with env.rt.database.write(**kwargs) as db:
                yield ObservedConnection(db)

    store = copy.copy(env.rt.sharing._store)
    store.database = ObservedDatabase()
    store.now = lambda: at(5) if removed else at(3)
    assert store.clear_share(NB) == [FRIEND]
    fence = env.rows("SELECT lost_at FROM memory_suggest_read_fences WHERE notebook_id=? AND owner_id=?", (NB, FRIEND))[0]
    instant = fence["lost_at"]
    assert _instant(instant.isoformat() if isinstance(instant, datetime) else instant) == _instant(at(5))
    env.grant("restored-after-unshare", "user", FRIEND)
    env.ask("after-unshare", 7)
    env.signal("before-unshare", minute=8)
    boundary, cursor_id = env.cursor()
    rows = env.store.clarified_scan(FRIEND, NB, cursor_created_at=boundary.isoformat(),
                                    cursor_id=cursor_id, inclusive=True, limit=20)
    assert [row["id"] for row in rows] == ["after-unshare"]


def assert_self_exit_clears_job(env: Env) -> None:
    """The self-exit deletes its membership row itself, then runs the shared
    per-member cleanup."""
    env.seed()
    env.member()
    env.signal()
    env.run("DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?", (NB, FRIEND))
    env.rt.sharing.forget_member_state(NB, FRIEND)
    assert not env.has_job()


def assert_group_leave_clears_job(env: Env) -> None:
    """Self-leave and an administrator's removal both run ``remove_member``."""
    env.seed()
    env.group()
    env.grant("g-group", "group", GROUP)
    env.signal()
    assert env.rt.groups.remove_member(GROUP, FRIEND) is True
    assert not env.has_job()


def assert_admin_demotion_clears_admin_only_job(env: Env) -> None:
    env.seed()
    env.group(friend_role="admin")
    env.grant("g-admins", "group_admins", GROUP)
    env.signal()
    env.rt.groups.upsert_member(GROUP, FRIEND, role="member", added_by=OWNER)
    assert not env.has_job()


def assert_delete_grant_clears_job(env: Env) -> None:
    env.seed()
    env.group()
    env.grant("g-group", "group", GROUP)
    env.signal()
    assert env.rt.groups.delete_grant(NB, "g-group") is True
    assert not env.has_job()


def assert_delete_group_grants_for_notebook_clears_job(env: Env) -> None:
    env.seed()
    env.group()
    env.grant("g-group", "group", GROUP)
    env.grant("g-admins", "group_admins", GROUP)
    env.signal()
    assert env.rt.groups.delete_group_grants_for_notebook(GROUP, NB) == 2
    assert not env.has_job()


def assert_delete_group_clears_jobs(env: Env) -> None:
    env.seed()
    env.group()
    env.grant("g-group", "group", GROUP)
    env.signal()
    assert env.rt.groups.delete_group(GROUP) is True
    assert not env.has_job()


def assert_keeps_job_when_access_survives_via_other_grant(env: Env) -> None:
    """Every path above leaves the row of a person who still reads the
    notebook another way (here a direct membership), and never touches the
    owner's row."""
    env.seed()
    env.store.set_memory_mode(OWNER, "suggest", now=at(0))
    env.store.note_signal(NB, OWNER, ask_job_id="", now=at(1))
    env.member()
    env.group(friend_role="admin")
    env.grant("g-group", "group", GROUP)
    env.grant("g-admins", "group_admins", GROUP)
    env.grant("g-user", "user", FRIEND)
    env.signal()
    env.rt.groups.upsert_member(GROUP, FRIEND, role="member", added_by=OWNER)
    assert env.rt.groups.delete_user_grant(NB, FRIEND) is True
    env.rt.groups.delete_group_grants_for_notebook(GROUP, NB)
    env.grant("g-group-2", "group", GROUP)
    env.rt.groups.remove_member(GROUP, FRIEND)
    env.rt.groups.delete_group(GROUP)
    assert env.has_job()
    assert env.has_job(OWNER)


def assert_keeps_job_when_access_survives_via_another_group(env: Env) -> None:
    """Losing one group's way in keeps the row of a person another group's
    grant still lets read the notebook."""
    env.seed()
    env.group()
    env.run(
        "INSERT INTO groups(id,name,kind,created_by,owner_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (GROUP_B, "g2", "project", OWNER, OWNER, NOW, NOW),
    )
    env.run(
        "INSERT INTO group_members(group_id,user_id,role,added_at) VALUES (?,?,?,?)",
        (GROUP_B, FRIEND, "member", NOW),
    )
    env.grant("g-group", "group", GROUP)
    env.grant("g-group-b", "group", GROUP_B)
    env.signal()
    env.rt.groups.remove_member(GROUP, FRIEND)
    env.rt.groups.delete_group_grants_for_notebook(GROUP, NB)
    env.grant("g-group-2", "group", GROUP)
    env.rt.groups.delete_group(GROUP)
    assert env.has_job()


def assert_keeps_job_when_access_survives_via_everyone(env: Env) -> None:
    """An ``everyone`` grant keeps the row through the loss of a personal and
    of a group grant."""
    env.seed()
    env.group()
    env.grant("g-everyone", "everyone", "")
    env.grant("g-user", "user", FRIEND)
    env.grant("g-group", "group", GROUP)
    env.signal()
    assert env.rt.groups.delete_user_grant(NB, FRIEND) is True
    env.rt.groups.remove_member(GROUP, FRIEND)
    env.rt.groups.delete_group(GROUP)
    assert env.has_job()


def assert_regrant_after_loss_starts_new_window(env: Env, lose: str) -> None:
    """After the loss, getting access back and asking again recreates the row
    at the NEW ask: the window never reaches back to the earlier asks."""
    env.seed()
    env.ask("ask-before", 1)
    if lose == "delete_grant":
        env.group()
        env.grant("g-group", "group", GROUP)
    elif lose in {"delete_group_grants", "delete_group", "group_leave"}:
        env.group()
        env.grant("g-group", "group", GROUP)
    elif lose == "remove_member":
        env.member()
    env.signal("ask-before", minute=2)
    assert env.cursor() == (_instant(at(1)), "ask-before")
    if lose == "delete_grant":
        assert env.rt.groups.delete_grant(NB, "g-group") is True
        env.grant("g-group-2", "group", GROUP)
    elif lose == "delete_group_grants":
        env.rt.groups.delete_group_grants_for_notebook(GROUP, NB)
        env.grant("g-group-2", "group", GROUP)
    elif lose == "delete_group":
        env.rt.groups.delete_group(GROUP)
        env.group()
        env.grant("g-group-2", "group", GROUP)
    elif lose == "group_leave":
        env.rt.groups.remove_member(GROUP, FRIEND)
        env.run(
            "INSERT INTO group_members(group_id,user_id,role,added_at) VALUES (?,?,?,?)",
            (GROUP, FRIEND, "member", NOW),
        )
    elif lose == "remove_member":
        env.rt.sharing.remove_member(NB, FRIEND)
        env.member()
    assert not env.has_job()
    env.ask("ask-after", 10)
    assert env.signal("ask-after", minute=11) == 1
    assert env.cursor() == (_instant(at(10)), "ask-after")


REGRANT_PATHS = (
    "delete_grant", "delete_group_grants", "delete_group", "group_leave", "remove_member",
)


INDEPENDENT_REVOCATIONS = (
    ("grant", "grant"), ("group-leave", "group-leave"),
    ("group-delete", "group-delete"), ("group-demote", "user"),
    ("grant", "user"), ("user", "grant"), ("grant", "member"),
    ("grant", "unshare"), ("grant", "member-self"),
    ("group-bulk", "grant-self"),
)


def assert_member_hygiene_preserves_live_window(env: Env) -> None:
    env.seed()
    env.member()
    env.grant("keeps-read", "user", FRIEND)
    env.ask("first-window", 1)
    env.signal("first-window", minute=2)
    env.rt.sharing.remove_member(NB, FRIEND)
    assert env.cursor() == (_instant(at(1)), "first-window")
    env.rt.sharing.unshare_notebook(NB)
    assert env.cursor() == (_instant(at(1)), "first-window")
    env.rt.groups.delete_user_grant(NB, FRIEND)
    assert not env.has_job()
    env.grant("regranted", "user", FRIEND)
    env.ask("new-window", 10)
    env.signal("new-window", minute=11)
    env.rt.sharing.forget_member_state(NB, FRIEND)
    assert env.cursor() == (_instant(at(10)), "new-window")


def assert_delayed_old_completion_cannot_reopen_membership_window(env: Env) -> None:
    env.seed()
    env.member()
    env.ask("before-removal", 1)
    env.signal("before-removal", minute=2)
    env.rt.sharing._store.now = lambda: at(5)
    env.rt.sharing.remove_member(NB, FRIEND)
    assert not env.has_job()
    env.run(
        "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
        (NB, FRIEND, "reader", at(10)),
    )
    env.ask("after-rejoin", 11)
    assert env.signal("before-removal", minute=12) == 1
    boundary, cursor_id = env.cursor()
    assert boundary > _instant(at(1)), "delayed completion must not reopen the old read period"
    rows = env.store.clarified_scan(
        FRIEND, NB, cursor_created_at=boundary.isoformat(), cursor_id=cursor_id,
        inclusive=True, limit=20,
    )
    assert [row["id"] for row in rows] == ["after-rejoin"]


READ_PERIOD_PATHS = ("member", "user", "everyone", "group-grant", "group-member",
                     "group-admin", "group-delete", "unshare", "member-self", "grant-self")


def assert_read_loss_boundary_is_open_at_full_microsecond_precision(env: Env, *, survives: bool) -> None:
    env.seed()
    env.member()
    loss = "2026-10-29T09:05:00.000100+00:00"
    old_spelling = "2026-10-29T11:05:00.000100+02:00"
    new_spelling = "2026-10-29T05:05:00.000101-04:00"
    env.rt.sharing._store.now = lambda: loss
    env.ask("old", 5)
    env.ask("running-old", 5)
    env.run("UPDATE ask_jobs SET created_at=?", (old_spelling,))
    env.run("UPDATE ask_jobs SET status='running' WHERE id='running-old'", ())
    if survives:
        env.grant("survivor", "everyone", "")
    env.rt.sharing.remove_member(NB, FRIEND)
    if not survives:
        env.grant("restored", "user", FRIEND)
    env.ask("new", 5)
    env.run("UPDATE ask_jobs SET created_at=? WHERE id='new'", (new_spelling,))
    env.signal("old", minute=12)
    boundary, cursor_id = env.cursor()
    kwargs = dict(cursor_created_at=boundary.isoformat(), cursor_id=cursor_id, inclusive=True)
    rows = env.store.clarified_scan(FRIEND, NB, limit=20, **kwargs)
    assert [row["id"] for row in rows] == (["old", "new"] if survives else ["new"])
    repeated = env.store.repeated_window(FRIEND, NB, lower=loss, limit=20)
    assert [row["id"] for row in repeated] == (["new", "old"] if survives else ["new"])
    unfinished = env.store.earliest_unfinished_ask(FRIEND, NB, **kwargs)
    assert (None if unfinished is None else unfinished["id"]) == ("running-old" if survives else None)


def assert_owner_and_public_read_period_semantics(env: Env) -> None:
    env.seed()
    env.run("UPDATE notebooks SET tier='base' WHERE id=?", (NB,))
    env.ask("old", 1)
    # The public-library tier alone is not an implicit read arm.
    assert env.signal("old", minute=2) == 0
    env.run("UPDATE notebooks SET created_by=? WHERE id=?", (FRIEND, NB))
    env.member()
    env.rt.sharing.remove_member(NB, FRIEND)
    assert env.rows("SELECT * FROM memory_suggest_read_fences", ()) == []
    assert env.signal("old", minute=3) == 1
    assert env.cursor() == (_instant(at(1)), "old")


def assert_delayed_completion_read_period(env: Env, path: str, *, with_job: bool, survives: bool) -> None:
    env.seed()
    env.rt.groups.now = env.rt.sharing._store.now = env.rt.memory_store.now = lambda: at(5)
    if path in {"member", "unshare", "member-self"}:
        env.member()
    elif path.startswith("group-"):
        env.group(friend_role="admin")
        env.grant("original", "group_admins" if path == "group-admin" else "group", GROUP)
    else:
        env.grant("original", "everyone" if path == "everyone" else "user", "" if path == "everyone" else FRIEND)
    env.ask("old-period", 1)
    if with_job:
        env.signal("old-period", minute=2)
    if survives:
        # Read continuity starts at the removed arm's t0, not at the t3 survivor.
        env.run(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("survivor", NB, "everyone" if path != "everyone" else "user",
             "" if path != "everyone" else FRIEND, "viewer", OWNER, at(3)),
        )
    if path == "member":
        env.rt.sharing.remove_member(NB, FRIEND)
    elif path == "unshare":
        env.rt.sharing.unshare_notebook(NB)
    elif path == "member-self":
        token = env.rows("SELECT added_at FROM notebook_members WHERE notebook_id=? AND user_id=?", (NB, FRIEND))[0]["added_at"]
        assert env.rt.memory_store.finish_member_exit(NB, FRIEND, token).ended
    elif path == "grant-self":
        assert env.rt.memory_store.finish_grant_exit(NB, FRIEND, "original").ended
    elif path == "user":
        env.rt.groups.delete_user_grant(NB, FRIEND)
    elif path in {"everyone", "group-grant"}:
        env.rt.groups.delete_grant(NB, "original")
    elif path == "group-member":
        env.rt.groups.remove_member(GROUP, FRIEND)
    elif path == "group-admin":
        env.rt.groups.upsert_member(GROUP, FRIEND, role="member", added_by=OWNER)
    else:
        env.rt.groups.delete_group(GROUP)
    fences = env.rows("SELECT lost_at FROM memory_suggest_read_fences WHERE notebook_id=? AND owner_id=?", (NB, FRIEND))
    assert bool(fences) is not survives
    if not survives:
        assert not env.has_job()
        assert _instant(str(fences[0]["lost_at"])) == _instant(at(5))
        if path == "group-admin":
            # Promotion keeps old added_at: deriving a period from it would replay.
            env.rt.groups.upsert_member(GROUP, FRIEND, role="admin", added_by=OWNER)
        else:
            env.grant("restored", "user", FRIEND)
    env.ask("current-period", 11)
    assert env.signal("old-period", minute=12) >= 1
    boundary, cursor_id = env.cursor()
    assert boundary == _instant(at(1) if survives else at(5))
    rows = env.store.clarified_scan(FRIEND, NB, cursor_created_at=boundary.isoformat(),
                                    cursor_id=cursor_id, inclusive=True, limit=20)
    assert [row["id"] for row in rows] == (["old-period", "current-period"] if survives else ["current-period"])


def independent_revocations(env: Env, kinds: tuple[str, str]) -> list[Callable]:
    """Real store operations; only their transaction wrapper may be controlled."""
    env.seed()
    actions = []
    for index, kind in enumerate(kinds):
        group_id, grant_id = f"grp-race-{index}", f"g-race-{index}"
        if kind in {"user", "grant-self"}:
            env.grant(grant_id, "user", FRIEND)
        elif kind in {"member", "unshare", "member-self"}:
            env.member()
        else:
            env.run(
                "INSERT INTO groups(id,name,kind,created_by,owner_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (group_id, "g", "project", OWNER, OWNER, NOW, NOW),
            )
            for user in (OWNER, FRIEND):
                env.run(
                    "INSERT INTO group_members(group_id,user_id,role,added_at) VALUES (?,?,?,?)",
                    (group_id, user, "admin", NOW),
                )
            env.grant(grant_id, "group_admins" if kind == "group-demote" else "group", group_id)

        token = env.rows(
            "SELECT added_at FROM notebook_members WHERE notebook_id=? AND user_id=?", (NB, FRIEND),
        )[0]["added_at"] if kind == "member-self" else None

        def revoke(database, *, kind=kind, group_id=group_id, grant_id=grant_id, token=token):
            groups = copy.copy(env.rt.groups)
            sharing = copy.copy(env.rt.sharing._store)
            memory = copy.copy(env.rt.memory_store)
            groups.database = sharing.database = memory.database = database
            if kind == "grant":
                return groups.delete_grant(NB, grant_id)
            if kind == "group-leave":
                return groups.remove_member(group_id, FRIEND)
            if kind == "group-delete":
                return groups.delete_group(group_id)
            if kind == "group-demote":
                return groups.upsert_member(group_id, FRIEND, role="member", added_by=OWNER)
            if kind == "group-bulk":
                return groups.delete_group_grants_for_notebook(group_id, NB)
            if kind == "user":
                return groups.delete_user_grant(NB, FRIEND)
            if kind == "member":
                return sharing.remove_member(NB, FRIEND)
            if kind == "unshare":
                return sharing.clear_share(NB)
            if kind == "member-self":
                return memory.finish_member_exit(NB, FRIEND, token)
            return memory.finish_grant_exit(NB, FRIEND, grant_id)

        actions.append(revoke)
    env.ask("old-window", 1)
    assert env.signal("old-window", minute=2) == 1
    assert env.store.claim(NB, FRIEND, claim_token="old-claim", now=at(3))
    return actions


def assert_revocation_window_replaced(env: Env) -> None:
    assert not env.has_job(), "the final read loss must delete the old claimed window"
    env.grant("new-grant", "user", FRIEND)
    env.ask("new-window", 10)
    assert env.signal("new-window", minute=11) == 1
    assert env.cursor() == (_instant(at(10)), "new-window")
    # Delayed per-member hygiene from the old removal must keep this new window.
    env.rt.sharing.forget_member_state(NB, FRIEND)
    assert env.cursor() == (_instant(at(10)), "new-window")
    assert not env.store.settle(
        NB, FRIEND, claim_token="old-claim", outcome="noop", failure_reason="",
        cursor=None, consecutive_failures=0, consumed_pending=1,
        candidates_written=0, now=at(12),
    )
