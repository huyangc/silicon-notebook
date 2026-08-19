"""SQLite row persistence for Agentic Memory P2's deployment-GLOBAL
retrieval-strategy experience library (``retrieval_experiences``).

Row-level only, mirroring ``agent_profile_store.py``'s split: what an entry
MEANS, when distillation fires and how a batch of runs becomes entries all
belong to ``app/services/retrieval_experience_job.py``. This module owns
exactly one table, and every read it exposes is bounded by construction — see
``RetrievalExperienceStorePort`` in ``app/repositories/ports.py`` for the full
contract, including why a store with no tenancy column at all is nonetheless
the right shape here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from app.repositories.sqlite.database import SqliteDatabase


def _canonical_situation(situation: Mapping[str, Any]) -> str:
    """Serialise a situation fingerprint the ONE way both backends store it.

    ``sort_keys=True`` is not cosmetic: PostgreSQL keeps this column as
    ``jsonb``, which discards key order and whitespace, so a row read back
    there re-serialises in sorted order no matter what went in. Sorting on the
    way in on BOTH backends is what keeps "an entry's content-addressed id can
    be re-verified from its stored row" true on either one — and that
    verifiability is the only thing standing between a merged database and an
    entry whose id no longer describes its content.
    """
    return json.dumps(dict(situation), sort_keys=True, ensure_ascii=False)


def _loads_obj(raw: object, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _experience_row(row) -> dict:
    return {
        "id": row["id"],
        "situation": _loads_obj(row["situation_json"], {}),
        "action": row["action"],
        "polarity": row["polarity"],
        "rationale": row["rationale"],
        "support": int(row["support"]),
        "adopted": int(row["adopted"]),
        "provenance": [str(item) for item in _loads_obj(row["provenance_json"], [])],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class RetrievalExperienceStore:
    def __init__(self, database: SqliteDatabase, *, now: Callable[[], str]) -> None:
        self.database = database
        self.now = now
        # No ``new_id`` seam on purpose. Every id in this table is
        # CONTENT-ADDRESSED and computed by the caller from the entry's own
        # (situation, action) — a random id would break both the primary key's
        # "one row per (situation, action)" guarantee and the cross-deployment
        # union in scripts/merge_dbs.py. Taking the seam and not using it would
        # invite exactly the wrong reflex from the next person to add a write
        # path here.

    def read_all(self, limit: int) -> list[dict]:
        """The whole library. Bounded RETURN, UNBOUNDED SCAN — say both plainly
        rather than let "bounded" cover for both.

        The ``LIMIT`` bounds what comes BACK, and the distillation's own
        eviction bounds how big the table ever gets (a few hundred rows). But
        the scan itself is genuinely unbounded: there is deliberately no index
        behind ``ORDER BY id`` — entry selection scores each row's situation
        against the current run's by set overlap over closed enum values,
        which is not a predicate any index could answer anyway, so an index
        here would buy nothing but pay for a migration on a query that runs
        once every ``RETRIEVAL_EXPERIENCE_TRIGGER`` completed asks. Registered,
        not overlooked: if the cap ever grows past a few hundred rows, revisit
        this alongside the next schema hop rather than opening one just for it.

        ``ORDER BY id`` (the content hash) rather than by any counter — the
        caller ranks in memory, and a stable order makes two reads over an
        unchanged table byte-identical, which is what lets the injection side
        memoise the result.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM retrieval_experiences ORDER BY id LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        return [_experience_row(row) for row in rows]

    def read_experience(self, experience_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_experiences WHERE id=?",
                (experience_id,),
            ).fetchone()
        return _experience_row(row) if row is not None else None

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
    ) -> dict:
        """Create or merge ONE entry — see the port docstring for the merge
        semantics. This backend's specifics:

        ⚠ ``begin_immediate`` opens the write transaction BEFORE the read.
        ``write()``'s mutex only serialises this process's writers, and the
        read-modify-write here (read provenance → subtract → write) is exactly
        the shape another process sharing the file can interleave with. Two
        distillation runs merging overlapping batches would then each see the
        pre-merge provenance list and each add their run ids to it — the
        de-duplication that makes this whole design cursor-free would silently
        stop working, with both writes reporting success. Same rule, same
        reason, as ``agent_profile_store.write_block``.

        The INSERT branch translates ``sqlite3.IntegrityError`` into a retry of
        the merge branch rather than propagating: a primary-key collision here
        means a concurrent writer created the same content-addressed entry
        between the SELECT and the INSERT, and "the same entry" is precisely
        the case this method already knows how to merge into. It is not an
        error condition — with ``begin_immediate`` in place it is unreachable
        on this backend, but the PostgreSQL mirror's ``ON CONFLICT`` has to
        cover it and both backends should end in the same state.

        ⚠ ``provenance`` arrives NEWEST-FIRST — the only caller
        (``retrieval_experience_job.py``) builds it by absorbing rows from a
        query ordered ``created_at DESC``. Reversed here, ONCE, before it
        touches ``fresh``/``added``/``merged``: every downstream trailing
        ``[-keep:]`` slice assumes the tail is the newest entry, and without
        this reversal a batch whose SIZE exceeds what fits would drop the
        genuinely NEWEST run ids from THIS batch (they sit at the front of an
        unreversed newest-first list) while keeping older ones — the opposite
        of what the eviction is supposed to do. See
        ``test_new_runs_add_support_and_the_provenance_list_stays_bounded``
        and the R1/R2/R3 overlapping-batch test for the shape this fixes.
        """
        now = self.now()
        keep = max(1, int(provenance_max))
        incoming = list(
            reversed([str(item) for item in provenance if str(item)])
        )
        with self.database.write() as db:
            self.database.begin_immediate(db)
            row = db.execute(
                "SELECT support, provenance_json FROM retrieval_experiences WHERE id=?",
                (experience_id,),
            ).fetchone()
            if row is None:
                fresh = list(dict.fromkeys(incoming))[-keep:]
                try:
                    db.execute(
                        "INSERT INTO retrieval_experiences "
                        "(id,situation_json,action,polarity,rationale,support,"
                        "adopted,provenance_json,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,0,?,?,?)",
                        (
                            experience_id,
                            _canonical_situation(situation),
                            action,
                            polarity,
                            rationale,
                            len(fresh),
                            json.dumps(fresh, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    row = db.execute(
                        "SELECT support, provenance_json FROM retrieval_experiences "
                        "WHERE id=?",
                        (experience_id,),
                    ).fetchone()
            if row is not None:
                known = [
                    str(item) for item in _loads_obj(row["provenance_json"], [])
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
                            "UPDATE retrieval_experiences SET polarity=?,rationale=?,"
                            "situation_json=?,action=?,support=?,provenance_json=?,"
                            "updated_at=? WHERE id=?",
                            (
                                polarity,
                                rationale,
                                _canonical_situation(situation),
                                action,
                                int(row["support"]) + len(added),
                                json.dumps(merged, ensure_ascii=False),
                                now,
                                experience_id,
                            ),
                        )
                    else:
                        db.execute(
                            "UPDATE retrieval_experiences SET support=?,"
                            "provenance_json=?,updated_at=? WHERE id=?",
                            (
                                int(row["support"]) + len(added),
                                json.dumps(merged, ensure_ascii=False),
                                now,
                                experience_id,
                            ),
                        )
                # else: nothing changed. ``updated_at`` deliberately stays put
                # — it is the last tie-break of the eviction ordering, and an
                # entry re-observed with no new runs and no new conclusion
                # would otherwise refresh itself into immortality.
            result = db.execute(
                "SELECT * FROM retrieval_experiences WHERE id=?", (experience_id,)
            ).fetchone()
        return _experience_row(result)

    def note_adopted(self, experience_ids: Sequence[str], delta: int = 1) -> int:
        """Increment ``adopted`` for entries a run actually acted on.

        No CAS and no read-modify-write: ``adopted = adopted + ?`` is decided
        by the database, so two concurrent runs adopting the same entry both
        count. A negative delta is rejected rather than clamped — this counter
        only ever grows, and the one thing that could make it shrink is a bug
        at the call site, which clamping would hide.

        ⚠ Deliberately does NOT touch ``updated_at``. That column is the last
        tie-break of the eviction ordering, and it is also half of
        ``version_signal()``'s memo key — letting an adoption refresh it would
        make a frequently-injected entry immortal AND invalidate the
        injection-side memo on every single run that adopts anything, which
        defeats the memo's entire purpose. An adoption is therefore invisible
        to both eviction recency and the memo, which is correct: ``adopted``
        is neither rendered into the prompt block nor part of the injection
        selection ordering.
        """
        ids = [str(item) for item in experience_ids if str(item)]
        if not ids or int(delta) < 0:
            if int(delta) < 0:
                raise ValueError("retrieval experience adopted delta must not be negative")
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE retrieval_experiences SET adopted=adopted+? "
                f"WHERE id IN ({placeholders})",
                (int(delta), *ids),
            )
        return cursor.rowcount

    def evict_to_limit(self, max_entries: int) -> int:
        """Trim to ``max_entries`` rows, ascending by
        ``(adopted, support, updated_at, id)``. Returns the row count deleted.

        Count and delete share one transaction so a concurrent insert cannot
        make the computed overflow describe a table that no longer exists.
        ``scripts/merge_dbs.py::_evict_experiences_to_limit`` mirrors this
        ordering for the post-union recap (codex #524 R1 P2) — change either
        side only together with the other.
        ``id`` as the final tie-break makes the choice deterministic even for
        entries written in the same second by the same batch (SQLite's clock is
        second-granular) — without it, "which of the tied entries survived"
        would differ run to run and backend to backend.
        """
        keep = max(0, int(max_entries))
        with self.database.write() as db:
            self.database.begin_immediate(db)
            total = int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM retrieval_experiences"
                ).fetchone()["n"]
            )
            overflow = total - keep
            if overflow <= 0:
                return 0
            cursor = db.execute(
                "DELETE FROM retrieval_experiences WHERE id IN ("
                "SELECT id FROM retrieval_experiences "
                "ORDER BY adopted ASC, support ASC, updated_at ASC, id ASC LIMIT ?)",
                (overflow,),
            )
        return cursor.rowcount

    def count(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM retrieval_experiences"
            ).fetchone()
        return int(row["n"])

    def version_signal(self) -> tuple[int, str]:
        """``(row count, newest updated_at)`` — the injection side's memo key.

        Both halves are needed and neither is redundant: the count alone misses
        an in-place UPDATE (a re-distilled entry keeps its content-addressed
        id), and the max alone misses an eviction (deleting the oldest rows
        leaves the newest timestamp untouched). Together they change whenever
        anything ``read_all`` would return has changed.

        ⚠ ``note_adopted`` deliberately does NOT move ``updated_at`` (see its
        own docstring: ``updated_at`` is the last tie-break of the eviction
        ordering, and letting an adoption refresh it would make a
        frequently-injected entry immortal). So an adoption is invisible to
        this signal — which is correct for its one consumer: ``adopted`` is
        neither rendered into the prompt block nor part of the selection
        ordering, so a memo that misses it still serves identical rows.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS m "
                "FROM retrieval_experiences"
            ).fetchone()
        return int(row["n"]), str(row["m"] or "")
