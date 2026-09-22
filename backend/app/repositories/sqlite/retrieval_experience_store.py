"""SQLite row persistence for Agentic Memory P2's retrieval-strategy
experience library (``retrieval_experiences``), partitioned by notebook since
schema v79.

Row-level only, mirroring ``agent_profile_store.py``'s split: what an entry
MEANS, when distillation fires and how a batch of runs becomes entries all
belong to ``app/services/retrieval_experience_job.py``. This module owns
exactly one table, and every read it exposes is bounded by construction — see
``RetrievalExperienceStorePort`` in ``app/repositories/ports.py`` for the full
contract, including why ``notebook_id`` is a partition key rather than a
tenancy column and what carries the safety argument instead.
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


def _partition_argument(value: object) -> str:
    """The ONE place a partition argument becomes a string — by refusing, not
    by coercing.

    ``str(value or "")`` was the obvious spelling and it is the wrong one:
    ``None`` would quietly become ``""``, which is not "no partition asked
    for" but a REAL partition — the global one, the one every pre-v79 row
    lives in and the one every notebook falls back to. A caller that lost a
    notebook id somewhere upstream would then read, and worse EVICT, the
    shared library instead of its own, and nothing anywhere would say so.

    ⚠ ``count`` deliberately does NOT go through here: ``None`` is a
    meaningful argument there (the whole table) precisely because it is the
    absent-argument case, and ``""`` still means the global partition. The
    asymmetry is registered in ``RetrievalExperienceStorePort.count``.
    """
    if not isinstance(value, str):
        raise TypeError(
            "retrieval experience partition must be a string; "
            f"got {type(value).__name__}"
        )
    return value


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
        "notebook_id": row["notebook_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class RetrievalExperienceStore:
    def __init__(self, database: SqliteDatabase, *, now: Callable[[], str]) -> None:
        self.database = database
        self.now = now
        # codex #524 R12 P2:进程内单调修订计数,version_signal 的第一元。
        # (count, MAX(updated_at)) 不是内容身份:updated_at 是带 offset 的
        # ISO 文本,MAX() 按字典序——offset 变化(DST/改时区)或时钟回拨时,
        # 一次真实更新可以不改变这两个数,注入侧缓存就永久陈旧。计数器只在
        # 本进程有效,而缓存键本就含"同一个活 store 对象"(弱引用),两者恰好
        # 同界;跨进程写入仍由 (count, max) 兜底,与修复前一致。
        #
        # 2026-09-22(PR-3 注入默认开):计数**按分区**记,不再是全表一个数。
        # 一个进程同时服务很多库,全表一个计数意味着任何一个库蒸出一条就把所有
        # 库的注入缓存一起作废——注入闸默认关时这只是理论损耗,默认开之后它是
        # 每条 reasoning 提问都要付的账。键是分区 id、值是该分区的写次数;每个
        # 键只是一个 int,进程见过多少库就有多少键(与注入侧那份按行数计的 LRU
        # 快照不是一个量级,不设上界)。
        self._mutations: dict[str, int] = {}
        # No ``new_id`` seam on purpose. Every id in this table is
        # CONTENT-ADDRESSED and computed by the caller from the entry's own
        # (situation, action) — a random id would break both the primary key's
        # "one row per (situation, action)" guarantee and the cross-deployment
        # union in scripts/merge_dbs.py. Taking the seam and not using it would
        # invite exactly the wrong reflex from the next person to add a write
        # path here.

    def _bump_revision(self, partition: str) -> None:
        """Record one write against ``partition``. Callers pass the partition
        they actually wrote, never ``""`` as a stand-in for "somewhere"."""
        self._mutations[partition] = self._mutations.get(partition, 0) + 1

    def _revision(self, partition: str) -> int:
        """How many writes THIS partition has taken in this process.

        Strictly this partition's own — not summed with the global one, even
        though every run reads both. ``version_signal`` hands the two halves
        back separately precisely so each layer can be memoised against the
        writes that can actually change IT: folding the global partition's
        count into a notebook's revision would make a global-chain batch
        re-read every notebook partition in the process for no content change.
        """
        return self._mutations.get(partition, 0)

    def read_partition(self, notebook_id: str, limit: int) -> list[dict]:
        """ONE partition (``""`` = the global one). Bounded RETURN, bounded
        SCAN — both, now, and the second half is new in v79.

        Before v79 this read was the whole table with no predicate to write,
        and the docstring said so plainly: the scan was genuinely unbounded and
        the missing index was registered rather than overlooked, on the
        argument that entry selection scores situations by set overlap over
        closed enum values — not something an index can answer — and the table
        stayed a few hundred rows. Partitioning changes exactly that argument:
        ``notebook_id = ?`` IS a predicate an index answers, and the table is
        no longer capped by one number (see
        ``RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES``), so v79 adds
        ``idx_retrieval_experiences_notebook`` with the read that justifies it.

        Equality, never a prefix and never a union: a caller that wants this
        notebook's entries with the global ones as a fallback calls twice and
        decides the precedence itself.

        ``ORDER BY id`` (the content hash) rather than by any counter — the
        caller ranks in memory, and a stable order makes two reads over an
        unchanged partition byte-identical, which is what lets the injection
        side memoise the result. The v79 index is ``(notebook_id, id)``, not
        ``notebook_id`` alone, and this statement is why: with both columns
        ``EXPLAIN QUERY PLAN`` reports ``SEARCH ... USING COVERING INDEX
        (notebook_id=?)`` — one seek into the partition, already in ``id``
        order. With the leading column alone the planner instead walks the
        whole table through the primary key (which already supplies the
        order) and filters, i.e. exactly the cost partitioning was meant to
        remove. Pinned by a test.

        A non-string ``notebook_id`` is refused rather than coerced; see
        ``_partition_argument``.
        """
        partition = _partition_argument(notebook_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM retrieval_experiences WHERE notebook_id=? "
                "ORDER BY id LIMIT ?",
                (partition, max(0, int(limit))),
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
        notebook_id: str = "",
    ) -> dict:
        """Create or merge ONE entry — see the port docstring for the merge
        semantics, including why ``notebook_id`` is written on the INSERT
        branch only and why a mismatch on the merge branch is an error rather
        than a silent no-op. This backend's specifics:

        ⚠ The merge branch's SELECT reads ``notebook_id`` alongside the two
        columns it already needed — no extra round trip, no extra row touched
        — purely so the cross-partition case can be REFUSED. The id the caller
        computed already encodes the partition, so "this id exists but in a
        different partition" means the caller derived the two from different
        values; writing anyway would merge one library's evidence into
        another's entry while every counter still added up. The raise happens
        before any statement mutates a row, inside the same transaction, so a
        refused call leaves the table byte-identical.

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
        partition = _partition_argument(notebook_id)
        keep = max(1, int(provenance_max))
        incoming = list(
            reversed([str(item) for item in provenance if str(item)])
        )
        with self.database.write() as db:
            self.database.begin_immediate(db)
            row = db.execute(
                "SELECT support, provenance_json, notebook_id "
                "FROM retrieval_experiences WHERE id=?",
                (experience_id,),
            ).fetchone()
            if row is None:
                fresh = list(dict.fromkeys(incoming))[-keep:]
                try:
                    db.execute(
                        "INSERT INTO retrieval_experiences "
                        "(id,situation_json,action,polarity,rationale,support,"
                        "adopted,provenance_json,notebook_id,created_at,"
                        "updated_at) "
                        "VALUES (?,?,?,?,?,?,0,?,?,?,?)",
                        (
                            experience_id,
                            _canonical_situation(situation),
                            action,
                            polarity,
                            rationale,
                            len(fresh),
                            json.dumps(fresh, ensure_ascii=False),
                            partition,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    row = db.execute(
                        "SELECT support, provenance_json, notebook_id "
                        "FROM retrieval_experiences WHERE id=?",
                        (experience_id,),
                    ).fetchone()
            if row is not None:
                if str(row["notebook_id"] or "") != partition:
                    # No id in the message: this exception is reported, and an
                    # id here would put a content-addressed key — the hash of
                    # a situation fingerprint — into a log line that the
                    # partitioning exists to keep out of shared surfaces.
                    raise ValueError(
                        "retrieval experience partition mismatch"
                    )
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
        # 无论落在哪个分支都 bump:少数"什么都没改"的调用多付一次聚合读,
        # 换掉"哪个分支算写"的分支追踪——那正是会随下一个分支悄悄漂移的账。
        self._bump_revision(partition)
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

    def evict_to_limit(self, max_entries: int, notebook_id: str = "") -> int:
        """Trim ONE partition to ``max_entries`` rows, ascending by
        ``(adopted, support, updated_at, id)``. Returns the row count deleted.

        ⚠ Both halves carry the ``notebook_id`` predicate, and the inner
        ``SELECT`` needs it just as much as the ``COUNT`` does: with the
        predicate on the count alone, a partition that is three rows over its
        own cap would delete the three worst rows IN THE WHOLE TABLE — almost
        certainly another partition's, since the caller has just written to
        this one and refreshed its ``updated_at``.

        Count and delete share one transaction so a concurrent insert cannot
        make the computed overflow describe a partition that no longer exists.
        ``scripts/merge_dbs.py::_evict_experiences_to_limit`` mirrors this
        ordering AND this per-partition confinement for the post-union recap
        (codex #524 R1 P2) — change either side only together with the other.
        ``id`` as the final tie-break makes the choice deterministic even for
        entries written in the same second by the same batch (SQLite's clock is
        second-granular) — without it, "which of the tied entries survived"
        would differ run to run and backend to backend.

        A non-string ``notebook_id`` is refused rather than coerced, and it
        matters most here: a lost id coerced to ``""`` would trim the SHARED
        library instead of the caller's own. See ``_partition_argument``.
        """
        keep = max(0, int(max_entries))
        partition = _partition_argument(notebook_id)
        with self.database.write() as db:
            self.database.begin_immediate(db)
            total = int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM retrieval_experiences "
                    "WHERE notebook_id=?",
                    (partition,),
                ).fetchone()["n"]
            )
            overflow = total - keep
            if overflow <= 0:
                return 0
            cursor = db.execute(
                "DELETE FROM retrieval_experiences WHERE id IN ("
                "SELECT id FROM retrieval_experiences WHERE notebook_id=? "
                "ORDER BY adopted ASC, support ASC, updated_at ASC, id ASC LIMIT ?)",
                (partition, overflow),
            )
        self._bump_revision(partition)
        return cursor.rowcount

    def count(self, notebook_id: str | None = None) -> int:
        """One partition's rows, or (``None``) the whole table's.

        ⚠ The ONE method here where ``None`` is a legal argument rather than a
        refused one — see ``_partition_argument`` and the port docstring for
        why the asymmetry is deliberate.
        """
        with self.database.connect() as connection:
            if notebook_id is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM retrieval_experiences"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM retrieval_experiences "
                    "WHERE notebook_id=?",
                    (str(notebook_id),),
                ).fetchone()
        return int(row["n"])

    def version_signal(
        self, notebook_id: str
    ) -> tuple[tuple[int, int, str], tuple[int, int, str]]:
        """``(this partition's signal, the global partition's signal)`` — the
        injection side's memo key for one run. Each is
        ``(mutation revision, row count, newest updated_at)``.

        ONE aggregate, scoped by ``notebook_id IN (?, '')`` so the planner
        answers it from ``idx_retrieval_experiences_notebook`` (the
        ``(notebook_id, id)`` index ``read_partition`` already requires)
        instead of scanning a table that now grows with the number of
        notebooks that have traffic. ``GROUP BY notebook_id`` rather than two
        statements: the two halves must describe the SAME instant, or a
        distillation landing between them hands the caller a pair of layers
        taken from two different versions of the table.

        The halves are returned separately because the global partition is
        read by every run in the process while the notebook half is not:
        folding them into one number would make merely switching notebooks
        invalidate the shared global snapshot. When ``notebook_id`` is ``""``
        it IS the global partition and both halves are the same signal.

        The count alone misses an in-place UPDATE (a re-distilled entry keeps
        its content-addressed id), and the max alone misses an eviction
        (deleting the oldest rows leaves the newest timestamp untouched). And
        the two DB halves TOGETHER are still not a content identity (codex
        #524 R12 P2): ``updated_at`` is offset-carrying ISO text and ``MAX()``
        compares lexicographically, so a UTC-offset change (DST, a timezone
        move) or a clock step backwards can make a real update invisible. The
        in-process ``_mutations`` revision closes that class by construction,
        and it is exactly co-extensive with the cache it feeds — the memo key
        also requires the SAME live store object (weakref identity), so every
        write the cached object can see bumps the revision it reports.
        Cross-process writes remain covered by the two DB halves, as before.

        ⚠ ``note_adopted`` deliberately does NOT move ``updated_at`` (see its
        own docstring: ``updated_at`` is the last tie-break of the eviction
        ordering, and letting an adoption refresh it would make a
        frequently-injected entry immortal). So an adoption is invisible to
        this signal — which is correct for its one consumer: ``adopted`` is
        neither rendered into the prompt block nor part of the selection
        ordering, so a memo that misses it still serves identical rows.
        """
        partition = _partition_argument(notebook_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT notebook_id, COUNT(*) AS n, "
                "COALESCE(MAX(updated_at), '') AS m "
                "FROM retrieval_experiences WHERE notebook_id IN (?, '') "
                "GROUP BY notebook_id",
                (partition,),
            ).fetchall()
        # An empty partition produces no group row at all, which is why the
        # default is built here rather than read off the result.
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
