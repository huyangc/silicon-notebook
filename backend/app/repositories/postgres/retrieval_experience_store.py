"""PostgreSQL row persistence for Agentic Memory P2's retrieval-strategy
experience library (``retrieval_experiences``), partitioned by notebook since
schema 0059.

A behavioural mirror of ``app/repositories/sqlite/retrieval_experience_store``:
same method names, same bounds, same return shapes (``situation`` decoded to a
dict, ``provenance`` to a list of strings, timestamps normalised to ISO
strings). The genuine differences are the ones the backends disagree about —
``jsonb`` instead of JSON text, ``timestamptz`` instead of ISO strings,
``FOR UPDATE`` row locking in place of SQLite's process-wide write
serialisation, and ``COLLATE "C"`` on the ordering key so a non-C-collated
database pages in the same order SQLite does.

See ``RetrievalExperienceStorePort`` in ``app/repositories/ports.py`` for the
contract, including why ``notebook_id`` is a partition key rather than a
tenancy column and what carries the isolation argument instead.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    json_value,
    jsonb,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase


def _canonical_situation(situation: Mapping[str, Any]) -> dict:
    """The situation as it goes into ``jsonb``.

    Unlike the SQLite mirror this returns a dict rather than text — ``jsonb``
    takes the structure and imposes its own storage order regardless of what we
    hand it. The sort on the SQLite side exists precisely so that both backends
    read BACK the same canonical shape; here the database does that for us, and
    round-tripping through ``json.dumps(..., sort_keys=True)`` first would be a
    no-op the next reader would misread as load-bearing.
    """
    return dict(situation)


def _partition_argument(value: object) -> str:
    """Refuse a non-string partition argument instead of coercing it — the
    SQLite mirror's ``_partition_argument`` carries the full reasoning, and
    the two must agree: ``None`` silently becoming ``""`` would send a read,
    or an eviction, at the SHARED global partition.

    ``count`` is the registered exception on both backends; ``None`` there
    means the whole table.
    """
    if not isinstance(value, str):
        raise TypeError(
            "retrieval experience partition must be a string; "
            f"got {type(value).__name__}"
        )
    return value


class RetrievalExperienceStore:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        now: Callable[[], TimestampInput],
    ) -> None:
        self.database = database
        self.now = normalized_clock(now)
        # No ``new_id`` seam, same note as the SQLite mirror: every id in this
        # table is CONTENT-ADDRESSED and computed by the caller.
        # codex #524 R12 P2:进程内单调修订计数(镜像 SQLite 侧,理由见彼处)。
        # 2026-09-22:按**分区**记,不再是全表一个数——理由同样见 SQLite 侧。
        self._mutations: dict[str, int] = {}

    def _bump_revision(self, partition: str) -> None:
        """Record one write against ``partition``. Mirror of the SQLite side."""
        self._mutations[partition] = self._mutations.get(partition, 0) + 1

    def _revision(self, partition: str) -> int:
        """How many writes THIS partition has taken in this process. Mirror of
        the SQLite side, including why it is not summed with the global one."""
        return self._mutations.get(partition, 0)

    @staticmethod
    def _experience_row(row) -> dict:
        return {
            "id": row["id"],
            "situation": json_value(row["situation_json"], {}) or {},
            "action": row["action"],
            "polarity": row["polarity"],
            "rationale": row["rationale"],
            "support": int(row["support"]),
            "adopted": int(row["adopted"]),
            "provenance": [
                str(item) for item in (json_value(row["provenance_json"], []) or [])
            ],
            "notebook_id": row["notebook_id"],
            "created_at": iso_timestamp(row["created_at"]),
            "updated_at": iso_timestamp(row["updated_at"]),
        }

    def read_partition(self, notebook_id: str, limit: int) -> list[dict]:
        """ONE partition (``""`` = the global one) — see the SQLite mirror's
        docstring for why the index this read uses arrived with v79 / 0059
        rather than with the original table, and why the read is an equality
        never a prefix or a union.

        ``ORDER BY id COLLATE "C"`` rather than a bare ``ORDER BY id``: the ids
        are lowercase hex, so any sane collation agrees today — but the SQLite
        mirror orders by raw byte value, and pinning the collation is what
        keeps "two reads over an unchanged partition are byte-identical ACROSS
        backends" true rather than accidentally true.

        ⚠ That same ``COLLATE "C"`` is why the 0059 index is declared over
        ``(notebook_id, id)`` with both columns already ``COLLATE "C"``: the
        index's own order then IS the order this statement asks for, so one
        index can answer the predicate AND supply the order. Unlike the SQLite
        mirror, no test asserts a PLAN here — this planner legitimately
        weighs that index against the id-ordered primary key by statistics,
        so a plan assertion would be a flake. The index DEFINITION is what is
        pinned on this side.

        A non-string ``notebook_id`` is refused rather than coerced; see
        ``_partition_argument``.
        """
        partition = _partition_argument(notebook_id)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM retrieval_experiences WHERE notebook_id=%s "
                'ORDER BY id COLLATE "C" LIMIT %s',
                (partition, max(0, int(limit))),
            ).fetchall()
        return [self._experience_row(row) for row in rows]

    def read_experience(self, experience_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM retrieval_experiences WHERE id=%s",
                (experience_id,),
            ).fetchone()
        return self._experience_row(row) if row is not None else None

    def upsert_experience(
        self,
        experience_id: str,
        *,
        situation: Mapping[str, Any],
        action: str,
        polarity: str,
        rationale: str,
        provenance: Sequence[str],
        provenance_max: int,
        replace_conclusion: bool,
        notebook_id: str = "",
    ) -> dict:
        """Create or merge ONE entry — see the port docstring for the merge
        semantics, including why ``notebook_id`` is written on the INSERT
        branch only and why a mismatch on the merge branch is an error rather
        than a silent no-op. This backend's specifics:

        ⚠ Both merge-branch reads pull ``notebook_id`` alongside the two
        columns they already needed — no extra statement — so the
        cross-partition case can be REFUSED (see the SQLite mirror for the
        failure it closes). The second read matters as much as the first: on
        the lost-the-conflict path it is the WINNER's row, and the winner may
        be in a different partition than this caller believes it is writing
        to. The raise happens before any UPDATE, inside the write
        transaction, so the refused call rolls back to a byte-identical table
        — including the ``ON CONFLICT DO NOTHING`` insert attempt, which by
        definition changed nothing on that path.

        ⚠ The read that decides the merge takes ``FOR UPDATE``. SQLite gets the
        same guarantee from ``begin_immediate``'s process-wide write lock;
        here, READ COMMITTED gives each statement its own snapshot, so without
        the row lock two distillation runs merging overlapping batches would
        each read the pre-merge provenance list and each add their run ids —
        the de-duplication that makes this design cursor-free would stop
        working with both writes reporting success.

        The INSERT uses ``ON CONFLICT (id) DO NOTHING`` and then re-reads,
        rather than ``DO UPDATE``: the merge is not expressible as a single
        conflict action (the support delta depends on how many of the incoming
        run ids the STORED list already knows), and a silent
        ``DO UPDATE SET support = support + N`` would be exactly the double
        count the provenance set exists to prevent.

        ⚠ Whether the INSERT actually landed is decided by
        ``cursor.rowcount`` — 1 means this call's row won, 0 means
        ``ON CONFLICT`` fired because a concurrent writer's INSERT committed
        between this method's own ``SELECT ... FOR UPDATE`` (which found
        nothing — Postgres does not lock rows that do not exist yet) and this
        INSERT. A row count of 0 falls through to the SAME merge branch the
        "row already existed" path uses, re-reading the winner's row under
        ``FOR UPDATE`` first. Before this the branch unconditionally set
        ``inserted = True`` regardless of what actually happened, which made
        the merge branch dead code on this path: a losing concurrent INSERT
        silently discarded its own provenance and rationale instead of
        merging into the winner's row.

        ⚠ ``provenance`` arrives NEWEST-FIRST — see the SQLite mirror's
        docstring; reversed here, ONCE, before it touches
        ``fresh``/``added``/``merged``, for the same reason.
        """
        now = self.now()
        partition = _partition_argument(notebook_id)
        keep = max(1, int(provenance_max))
        incoming = list(
            reversed([str(item) for item in provenance if str(item)])
        )
        with self.database.write() as db:
            row = db.execute(
                "SELECT support, provenance_json, notebook_id "
                "FROM retrieval_experiences WHERE id=%s FOR UPDATE",
                (experience_id,),
            ).fetchone()
            if row is None:
                fresh = list(dict.fromkeys(incoming))[-keep:]
                insert_cursor = db.execute(
                    "INSERT INTO retrieval_experiences "
                    "(id,situation_json,action,polarity,rationale,support,"
                    "adopted,provenance_json,notebook_id,created_at,"
                    "updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        experience_id,
                        jsonb(_canonical_situation(situation)),
                        action,
                        polarity,
                        rationale,
                        len(fresh),
                        jsonb(fresh),
                        partition,
                        now,
                        now,
                    ),
                )
                inserted = insert_cursor.rowcount == 1
                row = db.execute(
                    "SELECT support, provenance_json, notebook_id "
                    "FROM retrieval_experiences WHERE id=%s FOR UPDATE",
                    (experience_id,),
                ).fetchone()
            else:
                inserted = False
            if row is not None and not inserted:
                if str(row["notebook_id"] or "") != partition:
                    # No id in the message, same reason as the SQLite mirror:
                    # a content-addressed key is the hash of a situation
                    # fingerprint, and it has no business in a log line.
                    raise ValueError(
                        "retrieval experience partition mismatch"
                    )
                known = [
                    str(item)
                    for item in (json_value(row["provenance_json"], []) or [])
                ]
                seen = set(known)
                added = [
                    run_id
                    for run_id in dict.fromkeys(incoming)
                    if run_id not in seen
                ]
                if added or replace_conclusion:
                    merged = (known + added)[-keep:]
                    if replace_conclusion:
                        db.execute(
                            "UPDATE retrieval_experiences SET polarity=%s,"
                            "rationale=%s,situation_json=%s,action=%s,support=%s,"
                            "provenance_json=%s,updated_at=%s WHERE id=%s",
                            (
                                polarity,
                                rationale,
                                jsonb(_canonical_situation(situation)),
                                action,
                                int(row["support"]) + len(added),
                                jsonb(merged),
                                now,
                                experience_id,
                            ),
                        )
                    else:
                        db.execute(
                            "UPDATE retrieval_experiences SET support=%s,"
                            "provenance_json=%s,updated_at=%s WHERE id=%s",
                            (
                                int(row["support"]) + len(added),
                                jsonb(merged),
                                now,
                                experience_id,
                            ),
                        )
                # else: nothing changed, and ``updated_at`` stays put — same
                # eviction-ordering reason as the SQLite mirror.
            result = db.execute(
                "SELECT * FROM retrieval_experiences WHERE id=%s", (experience_id,)
            ).fetchone()
        self._bump_revision(partition)
        return self._experience_row(result)

    def note_adopted(self, experience_ids: Sequence[str], delta: int = 1) -> int:
        """Increment ``adopted`` for entries a run actually acted on. Mirror of
        the SQLite method, including the refusal to clamp a negative delta and
        the deliberate refusal to touch ``updated_at`` — see the SQLite
        mirror's docstring for why: that column is the eviction tie-break AND
        half of ``version_signal()``'s memo key, so an adoption must stay
        invisible to both."""
        ids = [str(item) for item in experience_ids if str(item)]
        if int(delta) < 0:
            raise ValueError(
                "retrieval experience adopted delta must not be negative"
            )
        if not ids:
            return 0
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE retrieval_experiences SET adopted=adopted+%s "
                "WHERE id = ANY(%s)",
                (int(delta), ids),
            )
        return cursor.rowcount

    def evict_to_limit(self, max_entries: int, notebook_id: str = "") -> int:
        """Trim ONE partition to ``max_entries`` rows, ascending by
        ``(adopted, support, updated_at, id)``. Returns the row count deleted.

        One statement rather than SQLite's count-then-delete pair: PostgreSQL
        can name the survivors directly as "the first ``max_entries`` in the
        REVERSED ordering", so there is no window in which a concurrent insert
        could invalidate a separately computed overflow.

        ⚠ The ``notebook_id`` predicate belongs on the INNER select, which is
        the only place it can express "the best ``max_entries`` OF THIS
        PARTITION". Moved to the outer ``DELETE`` instead, the ``OFFSET`` would
        still be counted over the whole table, so a partition under its own cap
        could have rows deleted the moment some other partition grew — see the
        SQLite mirror for the same trap stated from the count side.

        ⚠ Note the ordering is ``DESC`` here while the SQLite mirror's is
        ``ASC``, and the two still delete the same rows: SQLite takes the
        ``overflow`` WORST entries (ascending, LIMIT), this takes everything
        past the ``max_entries`` BEST ones (descending, OFFSET). They coincide
        only because the ordering is TOTAL — which is what the ``id`` tie-break
        is for. Drop it on either side and the two backends start disagreeing
        about which of several tied entries survived.

        A non-string ``notebook_id`` is refused rather than coerced, and it
        matters most here: a lost id coerced to ``""`` would trim the SHARED
        library instead of the caller's own. See ``_partition_argument``.
        """
        keep = max(0, int(max_entries))
        partition = _partition_argument(notebook_id)
        with self.database.write() as db:
            cursor = db.execute(
                "DELETE FROM retrieval_experiences WHERE id IN ("
                "SELECT id FROM retrieval_experiences WHERE notebook_id=%s "
                "ORDER BY adopted DESC, support DESC, updated_at DESC, "
                'id COLLATE "C" DESC OFFSET %s)',
                (partition, keep),
            )
        self._bump_revision(partition)
        return cursor.rowcount

    def count(self, notebook_id: str | None = None) -> int:
        """One partition's rows, or (``None``) the whole table's.

        ⚠ The ONE method here where ``None`` is a legal argument rather than a
        refused one — see ``_partition_argument`` and the port docstring for
        why the asymmetry is deliberate.
        """
        with self.database.connect() as db:
            if notebook_id is None:
                row = db.execute(
                    "SELECT COUNT(*) AS n FROM retrieval_experiences"
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) AS n FROM retrieval_experiences "
                    "WHERE notebook_id=%s",
                    (str(notebook_id),),
                ).fetchone()
        return int(row["n"])

    def version_signal(
        self, notebook_id: str
    ) -> tuple[tuple[int, int, str], tuple[int, int, str]]:
        """``(this partition's signal, the global partition's signal)`` — the
        injection side's memo key for one run. See the SQLite mirror for why
        the revision exists (offset-carrying text MAX is not a content
        identity), why the two halves are returned separately, why one grouped
        aggregate rather than two statements, and why an adoption is
        deliberately invisible here.

        ``::text`` rather than the raw ``timestamptz``: the value is only ever
        compared for equality against the previously observed one, and a
        rendered string compares identically on both backends, so the memo key
        has one shape instead of one per driver.
        """
        partition = _partition_argument(notebook_id)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT notebook_id, COUNT(*) AS n, "
                "COALESCE(MAX(updated_at)::text, '') AS m "
                "FROM retrieval_experiences WHERE notebook_id IN (%s, '') "
                "GROUP BY notebook_id",
                (partition,),
            ).fetchall()
        totals = {
            str(row["notebook_id"] or ""): (int(row["n"]), str(row["m"] or ""))
            for row in rows
        }
        shared = totals.get("", (0, ""))
        own = totals.get(partition, (0, ""))
        return (
            (self._revision(partition), own[0], own[1]),
            (self._revision(""), shared[0], shared[1]),
        )
