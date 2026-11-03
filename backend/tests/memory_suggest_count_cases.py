"""Backend-neutral cases for the count readers that leave unadopted system
suggestions out of "your memories" (auto user-memory plan T5, design §6.3).
SQLite: ``tests/test_memory_suggest_counts.py``; PostgreSQL twin:
``tests/postgres/test_memory_suggest_counts_pg.py``.

The readers (all in ``QueryStore`` plus the delete-job store's audience):

* ``memory_counts_by_owner_notebook`` -- the notebook cards' ``memory_count``;
* ``list_user_usage()['memory_count']`` -- the admin user overview;
* ``pending_delete_notices`` -- the deletion-window notice (who gets one, and
  the ``memory_count`` it shows);
* ``delete_notice_audience`` -- a superset, deliberately unchanged.

An unadopted suggestion is a row with a ``suggested_signal`` in ``candidate``
or ``rejected``; an adopted one (``confirmed`` / ``deprecated``) is the user's
memory like any other.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

OWNER = "u-sc-owner"
READER = "u-sc-reader"
OTHER = "u-sc-other"
NB_MIXED = "nb-sc-mixed"
NB_DRAFTS = "nb-sc-drafts"
NB_ADOPTED = "nb-sc-adopted"
NB_DELETING = "nb-sc-deleting"
NB_WIN_MIXED = "nb-sc-win-mixed"
NB_WIN_DRAFTS = "nb-sc-win-drafts"
NB_WIN_ADOPTED = "nb-sc-win-adopted"
NB_WIN_NONE = "nb-sc-win-none"
NOW = "2099-01-01T08:00:00+00:00"
DEADLINE = "2099-01-02T08:00:00+00:00"

#: The pre-suggestion statements, verbatim -- the oracle for "a library with no
#: suggestion in it reads exactly as before". ``{live}`` is the backend's
#: ``NOTEBOOK_LIVE_SQL``.
OLD_CARD_COUNTS_SQL = (
    "SELECT created_by, notebook_id, COUNT(*) AS c FROM memory_items "
    "WHERE created_by=? GROUP BY created_by, notebook_id"
)
OLD_ADMIN_MEMORY_COUNT_SQL = (
    "SELECT mi.created_by AS k,COUNT(*) AS c FROM memory_items mi "
    "JOIN notebooks nb ON nb.id=mi.notebook_id "
    "WHERE mi.status<>'rejected' AND nb.{live} "
    "GROUP BY mi.created_by"
)
OLD_NOTICE_HOLDS_SQL = (
    "SELECT EXISTS (SELECT 1 FROM memory_items om "
    "WHERE om.created_by = ? AND om.notebook_id = ?) AS held"
)
OLD_NOTICE_MEMORY_COUNT_SQL = (
    "SELECT COUNT(*) AS c FROM memory_items m WHERE m.created_by = ? "
    "AND m.notebook_id = ?"
)


@dataclass
class Env:
    repo: Any
    postgres: bool

    @property
    def runtime(self) -> Any:
        return self.repo._runtime

    @property
    def queries(self) -> Any:
        return self.runtime.queries

    @property
    def live_sql(self) -> str:
        if self.postgres:
            from app.repositories.postgres import access_sql
        else:
            from app.repositories.sqlite import access_sql
        return access_sql.NOTEBOOK_LIVE_SQL

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def run(self, sql: str, params: tuple = ()) -> None:
        database = self.runtime.database
        if self.postgres:
            with database.write() as db:
                db.execute(self._sql(sql), params)
        else:
            with database.write(operation="test.seed") as db:
                db.execute(sql, params)

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.runtime.database.connect() as db:
            return [dict(row) for row in db.execute(self._sql(sql), params).fetchall()]

    # ------------------------------------------------------------------ seed

    def seed(self) -> None:
        for user in (OWNER, READER, OTHER):
            self.run(
                "INSERT INTO users(id,email,display_name,role,status,username,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", "active", user, NOW, NOW),
            )

    def notebook(
        self, notebook_id: str, *, status: str = "ready", owner: str = OWNER,
        readers: tuple[str, ...] = (), in_window: bool = False,
    ) -> None:
        self.run(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (notebook_id, notebook_id, owner, "ready" if in_window else status, NOW, NOW),
        )
        if in_window:
            self.run(
                "UPDATE notebooks SET status='pending_delete',status_before_delete='ready',"
                "delete_after=? WHERE id=?",
                (DEADLINE, notebook_id),
            )
        for reader in readers:
            self.run(
                "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
                "VALUES (?,?,?,?)",
                (notebook_id, reader, "reader", NOW),
            )

    def memory(
        self, memory_id: str, *, notebook_id: str, owner: str = OWNER,
        status: str = "confirmed", signal: str | None = None,
    ) -> None:
        self.run(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,suggested_signal,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (memory_id, notebook_id, owner, "ask_answer", status, "t", "c", signal, NOW, NOW),
        )

    def adopted_and_drafts(self, notebook_id: str, owner: str, prefix: str) -> None:
        """One memory of each shape in ``notebook_id``: two plain (confirmed,
        candidate), one rejected, an adopted suggestion (confirmed) and a
        deprecated one, an open draft and a dismissed draft."""
        self.memory(f"{prefix}-confirmed", notebook_id=notebook_id, owner=owner)
        self.memory(f"{prefix}-candidate", notebook_id=notebook_id, owner=owner,
                    status="candidate")
        self.memory(f"{prefix}-rejected", notebook_id=notebook_id, owner=owner,
                    status="rejected")
        self.memory(f"{prefix}-adopted", notebook_id=notebook_id, owner=owner,
                    signal="clarified")
        self.memory(f"{prefix}-deprecated", notebook_id=notebook_id, owner=owner,
                    status="deprecated", signal="repeated")
        self.memory(f"{prefix}-draft-open", notebook_id=notebook_id, owner=owner,
                    status="candidate", signal="clarified")
        self.memory(f"{prefix}-draft-dismissed", notebook_id=notebook_id, owner=owner,
                    status="rejected", signal="repeated")

    # --------------------------------------------------------------- readers

    def card_counts(self, user_id: str) -> dict[tuple[str, str], int]:
        with self.runtime.database.connect() as db:
            return self.queries.memory_counts_by_owner_notebook(db, user_id)

    def admin_memory_count(self, user_id: str) -> int:
        usage = {row["id"]: row for row in self.queries.list_user_usage()}
        return int(usage[user_id]["memory_count"])

    def notices(self, user_id: str, notebook_id: str | None = None) -> dict[str, dict]:
        return {
            row["notebook_id"]: row
            for row in self.queries.pending_delete_notices(
                user_id, now=NOW, notebook_id=notebook_id)
        }


# ------------------------------------------------------------------- cases


def assert_card_counts_exclude_unadopted(env: Env) -> None:
    env.seed()
    env.notebook(NB_MIXED)
    env.notebook(NB_DRAFTS)
    env.notebook(NB_ADOPTED)
    env.adopted_and_drafts(NB_MIXED, OWNER, "mx")
    # Somebody else's memories in the same notebook never count for OWNER.
    env.adopted_and_drafts(NB_MIXED, OTHER, "ot")
    env.memory("dr-1", notebook_id=NB_DRAFTS, status="candidate", signal="clarified")
    env.memory("dr-2", notebook_id=NB_DRAFTS, status="rejected", signal="repeated")
    env.memory("ad-1", notebook_id=NB_ADOPTED, signal="clarified")
    env.memory("ad-2", notebook_id=NB_ADOPTED, status="deprecated", signal="repeated")

    counts = env.card_counts(OWNER)
    # mixed: confirmed, candidate, rejected, adopted, deprecated = 5 of 7 rows.
    assert counts[(OWNER, NB_MIXED)] == 5
    # only drafts: nothing of "yours" (absent or 0 -- the callers default to 0).
    assert counts.get((OWNER, NB_DRAFTS), 0) == 0
    # adopted suggestions are the user's memories.
    assert counts[(OWNER, NB_ADOPTED)] == 2
    assert {key[0] for key in counts} == {OWNER}
    assert env.card_counts(OTHER)[(OTHER, NB_MIXED)] == 5


def assert_catalog_prefilter_still_superset_of_confirmed(env: Env) -> None:
    """``notebook_catalog`` skips ``notebook_has_confirmed_memory`` when the card
    count is 0, so the count must stay a superset of "has a confirmed row": the
    excluded rows are candidate/rejected, never confirmed."""
    env.seed()
    for notebook_id in (NB_MIXED, NB_DRAFTS, NB_ADOPTED):
        env.notebook(notebook_id)
    env.adopted_and_drafts(NB_MIXED, OWNER, "mx")
    env.memory("dr-1", notebook_id=NB_DRAFTS, status="candidate", signal="clarified")
    env.memory("dr-2", notebook_id=NB_DRAFTS, status="rejected", signal="repeated")
    env.memory("ad-1", notebook_id=NB_ADOPTED, signal="clarified")
    counts = env.card_counts(OWNER)
    seen_confirmed = 0
    with env.runtime.database.connect() as db:
        for notebook_id in (NB_MIXED, NB_DRAFTS, NB_ADOPTED):
            confirmed = env.queries.notebook_has_confirmed_memory(db, notebook_id, OWNER)
            seen_confirmed += confirmed
            assert counts.get((OWNER, notebook_id), 0) > 0 or not confirmed, notebook_id
    assert seen_confirmed == 2  # mixed and adopted-only; the drafts-only notebook has none


def assert_admin_memory_count_subtracts_only_unadopted_candidates(env: Env) -> None:
    """The overview already left ``rejected`` out, so only open drafts
    (``candidate`` + ``suggested_signal``) come off; a dismissed draft must not
    be subtracted a second time, and a draft in a notebook the overview does
    not count (deleting) must not be subtracted at all."""
    env.seed()
    env.notebook(NB_MIXED)
    env.notebook(NB_DELETING, status="deleting")
    env.adopted_and_drafts(NB_MIXED, OWNER, "mx")
    env.memory("del-confirmed", notebook_id=NB_DELETING)
    env.memory("del-draft", notebook_id=NB_DELETING, status="candidate", signal="clarified")
    env.memory("rd-draft", notebook_id=NB_MIXED, owner=READER, status="candidate",
               signal="repeated")
    # live rows not rejected: confirmed, candidate, adopted, deprecated (the
    # open draft comes off; the dismissed one was never in).
    assert env.admin_memory_count(OWNER) == 4
    assert env.admin_memory_count(READER) == 0
    assert env.admin_memory_count(OTHER) == 0


def _window_world(env: Env) -> None:
    env.seed()
    for notebook_id in (NB_WIN_MIXED, NB_WIN_DRAFTS, NB_WIN_ADOPTED, NB_WIN_NONE):
        env.notebook(notebook_id, readers=(READER,), in_window=True)
    env.memory("wm-own", notebook_id=NB_WIN_MIXED, owner=READER)
    env.memory("wm-draft", notebook_id=NB_WIN_MIXED, owner=READER, status="candidate",
               signal="clarified")
    env.memory("wd-open", notebook_id=NB_WIN_DRAFTS, owner=READER, status="candidate",
               signal="clarified")
    env.memory("wd-dismissed", notebook_id=NB_WIN_DRAFTS, owner=READER, status="rejected",
               signal="repeated")
    env.memory("wa-adopted", notebook_id=NB_WIN_ADOPTED, owner=READER, signal="repeated")


def assert_delete_window_notice_skips_draft_only_readers(env: Env) -> None:
    _window_world(env)
    mine = env.notices(READER)
    # A reader with a memory of their own gets the notice; the draft is not in
    # the count it shows. An adopted suggestion is the reader's memory.
    assert set(mine) == {NB_WIN_MIXED, NB_WIN_ADOPTED}
    assert (mine[NB_WIN_MIXED]["memory_count"], mine[NB_WIN_MIXED]["transferable_count"]) == (1, 1)
    assert mine[NB_WIN_ADOPTED]["memory_count"] == 1
    # The owner is in the audience whatever they hold.
    assert set(env.notices(OWNER)) == {
        NB_WIN_MIXED, NB_WIN_DRAFTS, NB_WIN_ADOPTED, NB_WIN_NONE}
    # The one-notebook explanation is for any reader, drafts-only included.
    explained = env.notices(READER, NB_WIN_DRAFTS)
    assert set(explained) == {NB_WIN_DRAFTS}
    assert explained[NB_WIN_DRAFTS]["memory_count"] == 0
    assert env.notices(READER, NB_WIN_NONE)[NB_WIN_NONE]["memory_count"] == 0
    # A non-reader sees nothing either way.
    assert env.notices(OTHER) == {}


def assert_delete_notice_audience_superset_unchanged(env: Env) -> None:
    """The deletion job's audience is the owner plus everyone holding a row in
    any status, drafts included (a superset of the notice list above)."""
    _window_world(env)
    jobs = env.runtime.notebook_delete_jobs
    assert jobs.delete_notice_audience(NB_WIN_DRAFTS) == sorted([OWNER, READER])
    assert jobs.delete_notice_audience(NB_WIN_MIXED) == sorted([OWNER, READER])
    assert jobs.delete_notice_audience(NB_WIN_ADOPTED) == sorted([OWNER, READER])
    assert jobs.delete_notice_audience(NB_WIN_NONE) == [OWNER]
    notice_audience = {
        notebook_id for notebook_id in (
            NB_WIN_MIXED, NB_WIN_DRAFTS, NB_WIN_ADOPTED, NB_WIN_NONE)
        if notebook_id in env.notices(READER)
    }
    assert notice_audience == {NB_WIN_MIXED, NB_WIN_ADOPTED}
    for notebook_id in notice_audience:
        assert READER in jobs.delete_notice_audience(notebook_id)


def assert_readers_unchanged_without_suggestions(env: Env) -> None:
    """With no suggestion in the library every rewritten reader returns what the
    pre-suggestion statement returned (the statements are kept above)."""
    env.seed()
    for notebook_id in (NB_MIXED, NB_ADOPTED, NB_DELETING):
        env.notebook(notebook_id, status="deleting" if notebook_id == NB_DELETING else "ready")
    for notebook_id in (NB_WIN_MIXED, NB_WIN_NONE):
        env.notebook(notebook_id, readers=(READER, OTHER), in_window=True)
    for index, (user, notebook_id, status) in enumerate((
        (OWNER, NB_MIXED, "confirmed"), (OWNER, NB_MIXED, "candidate"),
        (OWNER, NB_MIXED, "rejected"), (OWNER, NB_MIXED, "deprecated"),
        (OWNER, NB_ADOPTED, "confirmed"), (OWNER, NB_DELETING, "confirmed"),
        (READER, NB_MIXED, "candidate"), (READER, NB_WIN_MIXED, "confirmed"),
        (READER, NB_WIN_MIXED, "candidate"), (OTHER, NB_WIN_MIXED, "rejected"),
    )):
        env.memory(f"plain-{index}", notebook_id=notebook_id, owner=user, status=status)

    for user in (OWNER, READER, OTHER):
        old = {
            (row["created_by"], row["notebook_id"]): int(row["c"])
            for row in env.rows(OLD_CARD_COUNTS_SQL, (user,))
        }
        assert env.card_counts(user) == old, user

    old_admin = {
        row["k"]: int(row["c"])
        for row in env.rows(OLD_ADMIN_MEMORY_COUNT_SQL.format(live=env.live_sql))
    }
    for user in (OWNER, READER, OTHER):
        assert env.admin_memory_count(user) == old_admin.get(user, 0), user

    for user in (OWNER, READER, OTHER):
        listed = env.notices(user)
        for notebook_id in (NB_WIN_MIXED, NB_WIN_NONE):
            held = bool(env.rows(OLD_NOTICE_HOLDS_SQL, (user, notebook_id))[0]["held"])
            owns = env.rows(
                "SELECT created_by FROM notebooks WHERE id=?", (notebook_id,))[0]["created_by"]
            assert (notebook_id in listed) == (held or owns == user), (user, notebook_id)
            if notebook_id in listed:
                old_count = int(
                    env.rows(OLD_NOTICE_MEMORY_COUNT_SQL, (user, notebook_id))[0]["c"])
                assert listed[notebook_id]["memory_count"] == old_count, (user, notebook_id)


# ------------------------------------------------------- form of the statements


@contextmanager
def recorded(env: Env):
    """Every ``memory_items`` statement the readers run, as ``(sql, params)``."""
    statements: list[tuple[str, Any]] = []
    if env.postgres:
        import psycopg

        original = psycopg.Connection.execute

        def recording(self, query, params=None, **options):
            if isinstance(query, str) and "FROM memory_items" in query:
                statements.append((query, params))
            return original(self, query, params, **options)

        psycopg.Connection.execute = recording
        try:
            yield statements
        finally:
            psycopg.Connection.execute = original
        return
    database = env.runtime.database
    connect = database.connect

    class Recording:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, sql: str, params: Any = ()) -> Any:
            if "FROM memory_items" in sql:
                statements.append((sql, params))
            return self._inner.execute(sql, params)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def __enter__(self) -> "Recording":
            self._inner.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self._inner.__exit__(*exc)

    database.connect = lambda *a, **k: Recording(connect(*a, **k))
    try:
        yield statements
    finally:
        database.connect = connect


def assert_count_readers_floor_the_difference(env: Env) -> None:
    """The subtraction is done inside each reader's one statement and floored at
    zero (``GREATEST`` / scalar ``MAX``). A negative difference cannot be built
    from data: in one statement both counts see one snapshot, and the
    subtrahend's rows are a subset of the minuend's. So the floor is pinned by
    the statements' form -- the behavioural guard is the interleaving case."""
    _window_world(env)
    floor = "GREATEST(0," if env.postgres else "MAX(0,"
    with recorded(env) as statements:
        env.card_counts(OWNER)
        env.admin_memory_count(OWNER)
        env.notices(READER)
        env.notices(READER, NB_WIN_DRAFTS)
    counted = [
        sql for sql, _params in statements
        if "COUNT(*)" in sql and "suggested_signal IS NOT NULL" in sql
    ]
    # card counts, admin overview, notice list, one-notebook notice.
    assert len(counted) == 4, [sql for sql, _ in statements]
    for sql in counted:
        assert floor in sql, sql


# ------------------------------------------------------------- interleaving


def assert_counts_never_negative_under_interleaving(env: Env, *, cycles: int = 150) -> None:
    """One writer connection churns suggestion rows (insert, dismiss, delete;
    then insert, adopt, delete) while one reader thread per reader hammers it
    on its own connection. Each reader is a single statement, so every read
    answers from one snapshot: never negative, and in the first phase (no
    adoption) exactly the number of the user's own memories -- a draft coming
    and going never shows through. A reader that subtracted two separate reads
    would sometimes see the row in one and not in the other (+1, or -1)."""
    import threading

    env.seed()
    env.notebook(NB_MIXED)
    env.notebook(NB_DRAFTS)
    for notebook_id in (NB_WIN_MIXED, NB_WIN_DRAFTS):
        env.notebook(notebook_id, readers=(READER,), in_window=True)
    for index in range(3):
        env.memory(f"base-{index}", notebook_id=NB_MIXED)
    env.memory("base-win", notebook_id=NB_WIN_MIXED, owner=READER)
    tokens = (
        ("tok-a", NB_MIXED, OWNER), ("tok-b", NB_DRAFTS, OWNER),
        ("tok-w", NB_WIN_MIXED, READER), ("tok-d", NB_WIN_DRAFTS, READER),
    )
    database = env.runtime.database

    def write(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql, params)

    def churn(final_status: str) -> None:
        with database.write() as db:
            for token, notebook_id, owner in tokens:
                db.execute(
                    "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
                    "title,content_md,suggested_signal,created_at,updated_at) "
                    "VALUES (%s,%s,%s,'ask_answer','candidate','t','c','clarified',%s,%s)",
                    (token, notebook_id, owner, NOW, NOW))
        with database.write() as db:
            db.execute(
                "UPDATE memory_items SET status=%s WHERE id = ANY(%s)",
                (final_status, [token for token, _nb, _owner in tokens]))
        with database.write() as db:
            db.execute(
                "DELETE FROM memory_items WHERE id = ANY(%s)",
                ([token for token, _nb, _owner in tokens],))

    state = {"phase": 1, "done": False}
    violations: list[str] = []
    checks = {"card": 0, "admin": 0, "notice": 0}

    def card() -> tuple[str, list[tuple[int, int, int]]]:
        counts = env.card_counts(OWNER)
        a, b = counts.get((OWNER, NB_MIXED), 0), counts.get((OWNER, NB_DRAFTS), 0)
        return ("card", [(a, 3, 4), (b, 0, 1)])

    def admin() -> tuple[str, list[tuple[int, int, int]]]:
        return ("admin", [(env.admin_memory_count(OWNER), 3, 5)])

    def notice() -> tuple[str, list[tuple[int, int, int]]]:
        listed = env.notices(READER)
        explained = env.notices(READER, NB_WIN_DRAFTS)
        win = listed.get(NB_WIN_MIXED)
        rows = [(win["memory_count"] if win else -1, 1, 2),
                (explained[NB_WIN_DRAFTS]["memory_count"], 0, 1)]
        if state["phase"] == 1 and NB_WIN_DRAFTS in listed:
            rows.append((-1, 0, 0))  # a drafts-only reader never gets the notice
        return ("notice", rows)

    def hammer(read) -> None:
        try:
            while not state["done"]:
                phase = state["phase"]
                name, observed = read()
                if state["phase"] != phase:
                    continue  # the read straddled the switch of phase
                checks[name] += 1
                for value, low, high in observed:
                    bound = (low, low) if phase == 1 else (low, high)
                    if value < 0 or not bound[0] <= value <= bound[1]:
                        violations.append(f"{name} phase {phase}: {value} not in {bound}")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            violations.append(f"{read.__name__} raised {exc!r}")

    threads = [threading.Thread(target=hammer, args=(read,)) for read in (card, admin, notice)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(cycles):
            churn("rejected")
        state["phase"] = 2
        for _ in range(cycles):
            churn("confirmed")
    finally:
        state["done"] = True
        for thread in threads:
            thread.join(timeout=60)
    assert not violations, violations[:10]
    assert min(checks.values()) >= 10, checks
    # Settled: the same readers, once, with a known mix.
    write("DELETE FROM memory_items WHERE id = ANY(%s)",
          ([token for token, _nb, _owner in tokens],))
    env.memory("tok-a", notebook_id=NB_MIXED, status="candidate", signal="clarified")
    env.memory("tok-b", notebook_id=NB_DRAFTS, status="confirmed", signal="repeated")
    counts = env.card_counts(OWNER)
    assert (counts[(OWNER, NB_MIXED)], counts[(OWNER, NB_DRAFTS)]) == (3, 1)
    assert env.admin_memory_count(OWNER) == 4
