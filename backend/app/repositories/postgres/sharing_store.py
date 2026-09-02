from __future__ import annotations

import json
from datetime import timedelta
from typing import Callable, Sequence

from psycopg import sql
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.repositories.group_rows import (
    GROUP_GRANT_COUNT_SQL,
    GROUP_GRANT_EXISTS_SQL,
)
from app.repositories.ports import NotebookTooLargeToCopyError
from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    normalized_clock,
    normalize_timestamp_row,
    sqlite_compatible_notebook_row,
    sqlite_compatible_row,
    utc_now,
)
from app.repositories.postgres.access_sql import (
    MEMBER_PROBE_SQL,
    NOTEBOOK_ADMIN_SQL,
    NOTEBOOK_DELETE_OWNER_SQL,
    NOTEBOOK_LIVE_SQL,
    NOTEBOOK_READ_SQL,
    NOTEBOOK_WRITE_SQL,
    admin_access_params,
    read_access_params,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.knowhow_history_store import record_change


_KNOWHOW_SOURCE_IDS = "SELECT id FROM sources WHERE source_type='knowhow'"

# Deliberately absent: `notebook_grants` (group knowledge sharing P1, schema
# v27), for the same reason `notebook_members` (share-token readers) already
# isn't copied. Access-control state is not knowledge: who else can read a
# notebook is a property of the ORIGINAL notebook, not of the knowledge
# inside it, and a copy is a brand-new notebook the new owner alone controls.
# Carrying grants across would silently hand the copy's owner's collaborators
# to whoever they were on the source — the new owner must re-grant access
# explicitly. `groups`/`group_members` need no mention here: they hang off no
# `notebook_id` at all, so they were never candidates for this per-notebook
# snapshot in the first place.
#
# Also deliberately absent: `notebook_share_requests` (group knowledge
# sharing P2, schema v28). It doubles as both reasons above: a pending
# request is transient *process* state, not knowledge — like
# `catalog_jobs`/`catalog_candidates`, it belongs to the run (here, the
# member's ask) that produced it, and an approved request has already done
# its job by writing a `notebook_grants` row, which itself isn't copied. It
# is also access-control-adjacent — who asked to share the ORIGINAL notebook
# with which group is a property of that notebook's collaboration history,
# not of the knowledge inside it. A copy is a brand-new notebook with no
# such history; its owner starts from a clean slate and requests sharing
# explicitly if they want it.
#
# Also deliberately absent: `agent_notebook_profile` / `agent_profile_jobs`
# (Agentic Memory P1, schema v29). The copy starts its understanding from
# scratch — registered as intentional in the feature's design doc. What those
# blocks hold is how this library came to be *used and read*, not the knowledge
# in it: the shared base layer describes a corpus that the copy's owner will
# grow differently from here, and each per-member overlay is that one member's
# retrieval habit, which no copy recipient inherits. The job rows are transient
# *process* state (one chain's run status plus its threshold counter), the same
# reason `catalog_jobs`/`catalog_candidates` are absent from this snapshot on
# the SQLite side.
#
# Also deliberately absent: `agent_observations` (Agentic Memory P3, T1,
# schema v33). Same reasoning as `agent_notebook_profile`/`agent_profile_jobs`
# directly above, and the copy starts with an empty observation log: an
# observation is process state about how an external Agent has been using
# THIS notebook, not knowledge a copy should inherit — the copy's owner
# hasn't done any of the work an observation line describes yet.
#
# ⚠ This is a DIFFERENT operation from `scripts/merge_dbs.py`, which DOES
# carry `agent_observations` forward (in its `NOTEBOOK_SCOPED_TABLES`, see
# that script's own comment on the entry). The two do not contradict each
# other: a deep copy manufactures a brand-new notebook whose owner has no
# history yet, so carrying usage state forward would misattribute someone
# else's activity to a notebook that has had none; a merge reconciles two
# copies of the SAME deployment/notebook lineage, where a secondary
# database's observation rows describe real activity against that same
# notebook and belong with the rest of its knowledge. Do not "fix" either
# file to match the other.
#
# `retrieval_experiences` (Agentic Memory P2, schema v31) is absent for a
# structurally different reason and is NOT a decision this snapshot could make
# either way: it is deployment-GLOBAL — no `notebook_id` column, no owner
# column — so every query in this list is built on a predicate it does not
# have. Deep copy cannot reach it, exactly as it cannot reach
# `groups`/`group_members`. Its rows are general tactics for HOW to search
# rather than anything belonging to a notebook, so a copy inherits them by
# simply living in the same deployment.
_COPY_SNAPSHOT_QUERIES: tuple[tuple[str, str], ...] = (
    # codex #659 R14 P1: liveness-filtered — see the SQLite twin's comment
    # for the full rationale (a tombstone landing between the route's token
    # resolution and this snapshot must stop the copy here; every other
    # query below reads inside the SAME REPEATABLE READ transaction this
    # one pins, so re-adding the predicate to each child-table query would
    # be redundant, not merely optional).
    ("notebooks", f"SELECT * FROM notebooks WHERE id=%s AND {NOTEBOOK_LIVE_SQL}"),
    ("sources", "SELECT * FROM sources WHERE notebook_id=%s"),
    (
        "source_paper_meta",
        "SELECT m.* FROM source_paper_meta m JOIN sources s ON s.id=m.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'",
    ),
    (
        "source_authors",
        "SELECT a.* FROM source_authors a JOIN sources s ON s.id=a.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'",
    ),
    (
        "source_elements",
        "SELECT e.* FROM source_elements e JOIN sources s ON s.id=e.source_id "
        "WHERE s.notebook_id=%s ORDER BY e.ordinal",
    ),
    ("chunks", "SELECT * FROM chunks WHERE notebook_id=%s ORDER BY ordinal"),
    (
        "knowledge_objects",
        "SELECT * FROM knowledge_objects WHERE notebook_id=%s "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS}) ORDER BY ordinal",
    ),
    (
        "knowledge_source_facts",
        "SELECT * FROM knowledge_source_facts WHERE notebook_id=%s "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})",
    ),
    (
        "knowledge_source_fact_elements",
        "SELECT * FROM knowledge_source_fact_elements WHERE notebook_id=%s "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})",
    ),
    (
        "knowledge_source_fact_backfills",
        "SELECT * FROM knowledge_source_fact_backfills WHERE notebook_id=%s "
        "AND status IN ('complete','incomplete') "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})",
    ),
    (
        "knowledge_relations",
        "SELECT * FROM knowledge_relations WHERE notebook_id=%s "
        f"AND (source_id IS NULL OR source_id NOT IN ({_KNOWHOW_SOURCE_IDS}))",
    ),
    ("chunk_embeddings", "SELECT * FROM chunk_embeddings WHERE notebook_id=%s"),
    ("chunk_questions", "SELECT * FROM chunk_questions WHERE notebook_id=%s"),
    (
        "element_embeddings",
        "SELECT e.* FROM element_embeddings e JOIN sources s ON s.id=e.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'",
    ),
    ("knowledge_embeddings", "SELECT * FROM knowledge_embeddings WHERE notebook_id=%s"),
    ("relation_embeddings", "SELECT * FROM relation_embeddings WHERE notebook_id=%s"),
    ("concept_clusters", "SELECT * FROM concept_clusters WHERE notebook_id=%s"),
    (
        "notebook_object_schemas",
        "SELECT * FROM notebook_object_schemas WHERE notebook_id=%s",
    ),
    ("knowhow_tables", "SELECT * FROM knowhow_tables WHERE notebook_id=%s"),
    (
        "knowhow_columns",
        "SELECT c.* FROM knowhow_columns c JOIN knowhow_tables t ON t.id=c.table_id "
        "WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_rows",
        "SELECT r.* FROM knowhow_rows r JOIN knowhow_tables t ON t.id=r.table_id "
        "WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cells",
        "SELECT c.* FROM knowhow_cells c JOIN knowhow_rows r ON r.id=c.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cell_code",
        "SELECT c.* FROM knowhow_cell_code c JOIN knowhow_rows r ON r.id=c.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
    ("notebook_assets", "SELECT * FROM notebook_assets WHERE notebook_id=%s"),
)
_COPY_VALIDATED_TABLES = (
    ("sources", ""),
    ("source_paper_meta", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("source_authors", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("chunks", ""),
    ("chunk_questions", ""),
    ("knowledge_objects", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("knowledge_source_facts", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("knowledge_source_fact_elements", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("knowledge_source_fact_backfills", "AND status IN ('complete','incomplete') "
     f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    (
        "knowledge_relations",
        f"AND (source_id IS NULL OR source_id NOT IN ({_KNOWHOW_SOURCE_IDS}))",
    ),
    ("concept_clusters", ""),
    ("notebook_object_schemas", ""),
    ("knowhow_tables", ""),
    ("notebook_assets", ""),
)
_COPY_VALIDATED_JOIN_TABLES = (
    (
        "knowhow_columns",
        "SELECT COUNT(*) AS c FROM knowhow_columns x "
        "JOIN knowhow_tables t ON t.id=x.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_rows",
        "SELECT COUNT(*) AS c FROM knowhow_rows x "
        "JOIN knowhow_tables t ON t.id=x.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cells",
        "SELECT COUNT(*) AS c FROM knowhow_cells x JOIN knowhow_rows r ON r.id=x.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cell_code",
        "SELECT COUNT(*) AS c FROM knowhow_cell_code x JOIN knowhow_rows r ON r.id=x.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
)
_COPY_TABLES = frozenset(table for table, _query in _COPY_SNAPSHOT_QUERIES)
# Operational extraction history is intentionally absent from the source
# snapshot, but NotebookCopyService synthesizes one copy-local completed KG
# generation for copied source facts. Keep snapshot eligibility and safe
# insertion eligibility separate so adding this row cannot start copying run
# history accidentally.
_COPY_INSERT_TABLES = _COPY_TABLES | {"extraction_runs"}
_JSON_COLUMNS = {
    "source_paper_meta": {"keywords", "raw_json"},
    "source_elements": {"metadata"},
    "chunks": {"element_ids"},
    "knowledge_objects": {"payload", "evidence"},
    "knowledge_source_facts": {"payload", "evidence"},
    "knowledge_relations": {"evidence"},
    "notebook_object_schemas": {"fields", "list_fields"},
}


def _snapshot_compat_row(table: str, row: dict) -> dict:
    if table == "notebooks":
        result = sqlite_compatible_notebook_row(row)
    else:
        result = sqlite_compatible_row(row, json_columns=_JSON_COLUMNS.get(table, set()))
    assert result is not None
    # SQLite SELECT * never exposed its implicit rowid. PostgreSQL's explicit
    # compatibility ordinal must likewise stay adapter-private: a deep copy is
    # a new insertion and must allocate a fresh ordinal instead of colliding
    # with the source row's globally unique value.
    result.pop("ordinal", None)
    return result


class SharingStore:
    """PostgreSQL sharing/member rows and backend-neutral deep-copy primitives."""

    def __init__(
        self,
        database: PostgresDatabase,
        settings: Settings,
        *,
        now: Callable[[], TimestampInput],
        insert_row: Callable,
    ) -> None:
        self.database = database
        self.settings = settings
        self.now = normalized_clock(now)
        self.insert_row = insert_row

    def bind_insert_row(self, insert_row: Callable) -> None:
        self.insert_row = insert_row

    def set_share_token(self, notebook_id: str, token: str) -> str:
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT is_shared,share_token FROM notebooks WHERE id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            chosen = row["share_token"] if row["is_shared"] and row["share_token"] else token
            connection.execute(
                "UPDATE notebooks SET is_shared=1,share_token=%s,updated_at=%s WHERE id=%s",
                (chosen, self.now(), notebook_id),
            )
        return str(chosen)

    def clear_share(self, notebook_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE notebooks SET is_shared=0,share_token=NULL,updated_at=%s WHERE id=%s",
                (self.now(), notebook_id),
            )
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id=%s", (notebook_id,)
            )

    def find_by_token(self, token: str) -> str | None:
        """codex #659 R11 P1：见 SQLite 孪生的完整理由（逐字同义）——并入
        ``NOTEBOOK_LIVE_SQL``，只动读侧。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM notebooks WHERE share_token=%s AND is_shared=1 "
                f"AND {NOTEBOOK_LIVE_SQL}",
                (token,),
            ).fetchone()
        return row["id"] if row else None

    def list_shared_by_owner(self, user_id: str) -> list[dict]:
        """`sqlite/sharing_store.py::list_shared_by_owner` 的镜像(P1-T4 起含
        群组共享)。codex #659 R11 P1：并入 ``NOTEBOOK_LIVE_SQL``，理由同
        SQLite 孪生。"""
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT id,name,share_token,"
                + GROUP_GRANT_COUNT_SQL + " AS group_count "
                "FROM notebooks WHERE created_by=%s "
                "AND (is_shared=1 OR " + GROUP_GRANT_EXISTS_SQL + ") "
                f"AND {NOTEBOOK_LIVE_SQL} "
                "ORDER BY updated_at DESC,id COLLATE \"C\"",
                (user_id,),
            ).fetchall()

    def notebook_row(self, notebook_id: str) -> dict | None:
        """codex #659 R16: the route-level ``notebook:configure`` guard is
        NOT enough on its own — under READ COMMITTED a delete tombstone can
        commit between the guard's check and this query, and an unfiltered
        read would then hand ``share_state`` a tombstoned row (200 with a
        share token on a deleting notebook). Filter here too; the guard
        stays as the authorization layer, this is the liveness layer."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM notebooks WHERE id=%s AND {NOTEBOOK_LIVE_SQL}",
                (notebook_id,),
            ).fetchone()
        return sqlite_compatible_notebook_row(row)

    @staticmethod
    def notebook_row_on(connection, notebook_id: str) -> dict | None:
        """codex #659 R11 P1：并入 ``NOTEBOOK_LIVE_SQL``——见 SQLite 孪生的
        完整理由（唯一消费点 ``join_shared`` 经 ``POST /shared/{token}/
        join`` 到达，没有任何路由层能力守卫保护）。"""
        row = connection.execute(
            f"SELECT * FROM notebooks WHERE id=%s AND {NOTEBOOK_LIVE_SQL}",
            (notebook_id,),
        ).fetchone()
        return sqlite_compatible_notebook_row(row)

    def shared_preview_rows(self, notebook_id: str) -> tuple[str, list[str]]:
        with self.database.connect() as connection:
            owner = connection.execute(
                "SELECT u.username FROM notebooks n LEFT JOIN users u ON u.id=n.created_by "
                "WHERE n.id=%s",
                (notebook_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT title FROM sources WHERE notebook_id=%s "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY created_at,id COLLATE \"C\" LIMIT 50",
                (notebook_id,),
            ).fetchall()
        return (owner["username"] if owner and owner["username"] else "", [r["title"] for r in rows])

    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        """写权:仅 owner。谓词见 `access_sql.NOTEBOOK_WRITE_SQL`。"""
        with self.database.connect() as connection:
            row = connection.execute(
                NOTEBOOK_WRITE_SQL, (notebook_id, user_id)
            ).fetchone()
        return row is not None

    def user_owns_notebook_regardless_of_lifecycle(
        self, notebook_id: str, user_id: str
    ) -> bool:
        """`DELETE /api/notebooks/{id}` 依赖专属（codex #659 R6 P2）:仅 owner,
        但**不**要求 notebook 处于 live 状态。谓词见
        `access_sql.NOTEBOOK_DELETE_OWNER_SQL` 的完整理由——唯一消费点是
        `require_notebook_delete`,任何其它写端点都不得复用这个方法。"""
        with self.database.connect() as connection:
            row = connection.execute(
                NOTEBOOK_DELETE_OWNER_SQL, (notebook_id, user_id)
            ).fetchone()
        return row is not None

    def user_can_admin_notebook(self, notebook_id: str, user_id: str) -> bool:
        """管理权:owner ∪ 管理级有效授权边。谓词见 `access_sql.NOTEBOOK_ADMIN_SQL`。

        P2 能力翻转的判定入口(裁决 P2-1);与 `user_can_access_notebook` 并列存在,
        理由见 SQLite 那一份。
        """
        with self.database.connect() as connection:
            row = connection.execute(
                NOTEBOOK_ADMIN_SQL, (notebook_id, *admin_access_params(user_id))
            ).fetchone()
        return row is not None

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        """读权:owner ∪ 只读成员 ∪ 有效授权边。谓词见 `access_sql.NOTEBOOK_READ_SQL`。"""
        with self.database.connect() as connection:
            row = connection.execute(
                NOTEBOOK_READ_SQL, (notebook_id, *read_access_params(user_id))
            ).fetchone()
        return row is not None

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                MEMBER_PROBE_SQL, (notebook_id, user_id)
            ).fetchone()
        return row is not None

    def add_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
                "VALUES (%s,%s,'reader',%s) ON CONFLICT(notebook_id,user_id) DO NOTHING",
                (notebook_id, user_id, self.now()),
            )

    @staticmethod
    def insert_member_if_live(connection, notebook_id: str, user_id: str, now) -> int:
        """codex #659 R12 P1：``join_shared`` 专属的原子插入——**不自己开
        事务**（与 ``notebook_row_on`` 同款静态方法形状；见 SQLite 孪生的
        docstring）。``PostgresDatabase.write()`` 不允许嵌套（会抛
        ``NestedPostgresWriteError``），所以调用方必须已经持有一个
        ``database.write()`` 连接贯穿"读活性行 + 插入成员 + 水合摘要"整条
        链路，全程只取一次连接——这正是 R11 引入、R12 要修的
        ``POSTGRES_POOL_MAX_SIZE=1`` 死等的根因。

        codex #659 R14 P2：返回受影响行数——同一事务内、活性读之后、这条
        INSERT 之前，若并发的删除作业在此刻提交了 tombstone（PG 默认 READ
        COMMITTED：同一事务里后一条语句仍能看见新提交），``WHERE EXISTS``
        会让这条 INSERT 插 0 行；但「已是成员」的 ``ON CONFLICT DO
        NOTHING`` 幂等 no-op 同样是 0 行——两种 0 行的含义完全不同，旧代码
        忽略 rowcount 会把前者也当成功放行。调用方须在 rowcount==0 时用
        ``is_member_on`` 再判一次区分。"""
        cursor = connection.execute(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
            "SELECT %s,%s,'reader',%s WHERE EXISTS ("
            f"SELECT 1 FROM notebooks WHERE id=%s AND {NOTEBOOK_LIVE_SQL}) "
            "ON CONFLICT(notebook_id,user_id) DO NOTHING",
            (notebook_id, user_id, now, notebook_id),
        )
        return cursor.rowcount

    @staticmethod
    def is_member_on(connection, notebook_id: str, user_id: str) -> bool:
        """codex #659 R14 P2：``is_member`` 的同连接变体——供 ``join_shared``
        在 ``insert_member_if_live`` 返回 0 行后，在同一个写事务/连接内再判
        一次「是否已是成员」，不为此额外取第二个连接。"""
        row = connection.execute(
            MEMBER_PROBE_SQL, (notebook_id, user_id)
        ).fetchone()
        return row is not None

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id=%s AND user_id=%s",
                (notebook_id, user_id),
            )

    def kick_all_members(self, notebook_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id=%s", (notebook_id,)
            )

    def list_members(self, notebook_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT u.username AS username,m.added_at AS added_at "
                "FROM notebook_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.notebook_id=%s ORDER BY m.added_at,u.username COLLATE \"C\"",
                (notebook_id,),
            ).fetchall()
        return [
            {
                "username": row["username"],
                "added_at": iso_timestamp(row["added_at"]),
            }
            for row in rows
        ]

    def source_owner(self, source_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT n.created_by AS owner FROM sources s "
                "JOIN notebooks n ON n.id=s.notebook_id WHERE s.id=%s",
                (source_id,),
            ).fetchone()
        return row["owner"] if row else None

    def conversation_owner(self, conversation_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT created_by AS owner FROM conversations WHERE id=%s",
                (conversation_id,),
            ).fetchone()
        return row["owner"] if row else None

    def answer_owner(self, answer_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT n.created_by AS owner FROM answers a "
                "JOIN notebooks n ON n.id=a.notebook_id WHERE a.id=%s",
                (answer_id,),
            ).fetchone()
        return row["owner"] if row else None

    def source_notebook_id(self, source_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT notebook_id FROM sources WHERE id=%s", (source_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    def answer_notebook_id(self, answer_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT notebook_id FROM answers WHERE id=%s", (answer_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    def snapshot_copy_rows(self, notebook_id: str) -> dict[str, list[dict]]:
        """Read every copyable table's rows in ONE REPEATABLE READ transaction,
        with the copyable-row bound enforced atomically inside it (codex PR#353
        r3). READ COMMITTED (the default) would let each statement see newer
        commits, so the count and the per-table fetchalls could disagree; under
        REPEATABLE READ every read observes the snapshot fixed at the first
        statement, so a concurrent ingestion commit cannot slip an over-limit
        notebook between the check and the materialisation. Over the copyable row
        limit → raise BEFORE any fetchall (never materialises the oversized rows,
        the 300GB+ OOM). f.chunks + f.nodes = all chunks + all knowledge_objects.
        `SET TRANSACTION` is the first statement so it governs this transaction.

        The chunks+nodes gate does NOT bound the other materialised tables
        (relations / embeddings / elements / knowhow); a source whose graph or
        embedding fan-out dwarfs its chunk+node count passes it yet would
        materialise gigabytes here. The second bound caps the SUM of every
        snapshot table's rows — counted, not fetched, in the SAME REPEATABLE READ
        snapshot — and raises before any fetchall (codex PR#353 r5 P2).

        codex #659 R14 P1: the root ``notebooks`` row is read liveness-
        filtered (see ``_COPY_SNAPSHOT_QUERIES``'s comment) — an empty result
        (never existed, or a tombstone landed before this snapshot pinned)
        raises ``KeyError`` HERE rather than letting the caller's
        ``snapshot["notebooks"][0]`` bare-index, giving the route's existing
        ``except KeyError: 404`` something to catch.

        codex #659 R23 P1: that liveness-filtered root read is the FIRST
        query of the transaction — it is what pins the REPEATABLE READ
        snapshot. The old order (size counts first, root read later) pinned
        the snapshot at the COUNT, so a tombstone committing between the
        pin and the root read was invisible (the pinned snapshot still said
        'live') and a full copy could persist mid-delete. With the root
        read first, either the tombstone committed before the pin (empty →
        KeyError → 404) or the ENTIRE snapshot precedes the tombstone —
        serializable as copy-completed-before-delete, which the delete job
        then proceeds over normally (it only removes the source)."""
        snapshot: dict[str, list[dict]] = {}
        with self.database.connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            root_table, root_query = _COPY_SNAPSHOT_QUERIES[0]
            assert root_table == "notebooks"
            snapshot[root_table] = [
                _snapshot_compat_row(root_table, row)
                for row in connection.execute(root_query, (notebook_id,)).fetchall()
            ]
            if not snapshot[root_table]:
                raise KeyError(notebook_id)
            violation = self._copy_limit_violation(connection, notebook_id)
            if violation is not None:
                raise NotebookTooLargeToCopyError(violation)
            for table, query in _COPY_SNAPSHOT_QUERIES[1:]:
                snapshot[table] = [
                    _snapshot_compat_row(table, row)
                    for row in connection.execute(query, (notebook_id,)).fetchall()
                ]
        return snapshot

    def _copy_limit_violation(self, connection, notebook_id: str) -> "str | None":
        """Both copy bounds, counted (not fetched) on the caller's connection: the
        message to raise if either is crossed, else None. Single source of truth
        shared by snapshot_copy_rows (atomic, in its REPEATABLE READ snapshot) and
        snapshot_copy_within_limits (the fresh share-routing recheck), so the
        copyable verdict and the guard never disagree (codex PR#354 r2 P2)."""
        row = connection.execute(
            "SELECT (SELECT COUNT(*) FROM chunks WHERE notebook_id=%s) + "
            "(SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=%s) AS n",
            (notebook_id, notebook_id),
        ).fetchone()
        total = int(row["n"] if hasattr(row, "keys") else row[0])
        if total > self.settings.notebook_copy_max_rows:
            return (
                f"notebook {notebook_id} crossed the copy-size limit "
                f"({total} rows > {self.settings.notebook_copy_max_rows}); "
                f"share read-only instead"
            )
        materialised = 0
        for _table, query in _COPY_SNAPSHOT_QUERIES:
            crow = connection.execute(
                f"SELECT COUNT(*) AS n FROM ({query}) AS _c", (notebook_id,)
            ).fetchone()
            materialised += int(crow["n"] if hasattr(crow, "keys") else crow[0])
        if materialised > self.settings.notebook_copy_max_snapshot_rows:
            return (
                f"notebook {notebook_id} crossed the copy-materialisation limit "
                f"({materialised} rows across all tables > "
                f"{self.settings.notebook_copy_max_snapshot_rows}); share read-only instead"
            )
        return None

    def snapshot_copy_within_limits(self, notebook_id: str) -> bool:
        """Fresh, non-materialising twin of snapshot_copy_rows' bounds: True iff a
        deep copy would pass BOTH copy bounds right now. The share-routing facade
        consults it so the copy-vs-read-only verdict reflects the total-
        materialisation bound WITHOUT the staleness of the KG-version-cached
        copy_stats (codex PR#354 r2 P2). Own REPEATABLE READ transaction;
        COUNT-only, never materialises the rows."""
        with self.database.connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return self._copy_limit_violation(connection, notebook_id) is None

    def insert_copy_rows(self, table: str, rows: Sequence[dict], *, chunk_size: int) -> None:
        if table not in _COPY_INSERT_TABLES:
            raise ValueError("unsupported copy table")
        for index in range(0, len(rows), chunk_size):
            with self.database.write() as connection:
                for data in rows[index : index + chunk_size]:
                    self.insert_row(
                        connection,
                        table,
                        normalize_timestamp_row(table, data),
                    )

    def seed_copied_knowhow_genesis(
        self,
        table_ids: Sequence[str],
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        actor: str,
        note: str,
    ) -> None:
        """整本 notebook 深拷贝后，为每个拷贝来的 knowhow 表补一条 ``table_create``
        创世流水（PostgreSQL 侧镜像 SQLite ``SharingStore`` 的同名方法，codex 第 2
        轮 P2）。

        单表传输（``copy_table``/``move_table``）早就在自己事务的最后记了这条
        流水，但整本深拷贝走的是本 store 的批量 ``insert_copy_rows``、绕过了那条
        hook——于是拷贝来的表没有任何可回退到的创世点。

        **必须在该表的列/行/格/代码全部插完之后调用**：``record_change`` 记的
        指纹要反映拷贝完成后的完整表状态。title/description/columns 刻意从库里
        现查（而非依赖调用方手里可能已被 insert seat 改过的 dict），payload 形状
        与 ``create_knowhow_table``/``copy_table`` 产出的创世流水完全一致
        （``rows`` 恒为 ``[]``）。``origin='import'`` 同 ``copy_table``——这是一次性
        整表搬运、不是逐格手敲，复制/移动的语义由 ``note`` 如实区分。
        """
        if not table_ids:
            return
        with self.database.write() as connection:
            for table_id in table_ids:
                trow = connection.execute(
                    "SELECT title, description FROM knowhow_tables WHERE id = %s",
                    (table_id,),
                ).fetchone()
                if trow is None:
                    continue  # 防御：表已不在（正常拷贝路径不会发生）
                columns = [
                    {
                        "id": c["id"], "name": c["name"],
                        "role": c["role"], "position": c["position"],
                    }
                    for c in connection.execute(
                        "SELECT id, name, role, position FROM knowhow_columns "
                        "WHERE table_id = %s ORDER BY position, id COLLATE \"C\"",
                        (table_id,),
                    ).fetchall()
                ]
                record_change(
                    connection, new_id=new_id, now=now, table_id=table_id,
                    kind="table_create",
                    payload={
                        "table": {
                            "title": trow["title"], "description": trow["description"],
                        },
                        "columns": columns,
                        "rows": [],
                    },
                    actor=actor, origin="import", note=note,
                )

    def insert_fts_rows(self, sql_text: str, rows: Sequence[tuple], *, chunk_size: int) -> None:
        # PostgreSQL GIN/trigram indexes are maintained from base rows; there is
        # no copied FTS mirror table. Keep the neutral copy-service hook a no-op.
        del sql_text, rows, chunk_size

    def validate_copy(self, source_notebook_id: str, new_id: str) -> None:
        with self.database.connect() as connection:
            for table, extra in _COPY_VALIDATED_TABLES:
                copied = connection.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=%s {extra}",
                    (new_id,),
                ).fetchone()["c"]
                source = connection.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=%s {extra}",
                    (source_notebook_id,),
                ).fetchone()["c"]
                if copied != source:
                    raise RuntimeError(f"copy_notebook: {table} 行数不一致 {copied}!={source}")
            for table, query in _COPY_VALIDATED_JOIN_TABLES:
                copied = connection.execute(query, (new_id,)).fetchone()["c"]
                source = connection.execute(query, (source_notebook_id,)).fetchone()["c"]
                if copied != source:
                    raise RuntimeError(f"copy_notebook: {table} 行数不一致 {copied}!={source}")
            dangling = connection.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations r WHERE r.notebook_id=%s AND ("
                "r.source_object_id NOT IN (SELECT id FROM knowledge_objects WHERE notebook_id=%s) OR "
                "r.target_object_id NOT IN (SELECT id FROM knowledge_objects WHERE notebook_id=%s))",
                (new_id, new_id, new_id),
            ).fetchone()["c"]
            if dangling:
                raise RuntimeError("copy_notebook: 关系存在悬空引用")

    def publish_copy(self, notebook_id: str, status: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE notebooks SET status=%s,updated_at=%s WHERE id=%s",
                (status, self.now(), notebook_id),
            )

    def compensate_copy(self, notebook_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=%s", (notebook_id,)
            )
            connection.execute(
                "DELETE FROM notebooks WHERE id=%s AND status='copying'", (notebook_id,)
            )

    def sweep_stale_copies(self, *, created_by: str | None = None) -> int:
        cutoff = utc_now() - timedelta(
            seconds=max(1, self.settings.notebook_copy_stale_seconds)
        )
        with self.database.write() as connection:
            if created_by is None:
                rows = connection.execute(
                    "SELECT id FROM notebooks WHERE status='copying' AND created_at<%s FOR UPDATE",
                    (cutoff,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id FROM notebooks WHERE status='copying' AND created_by=%s "
                    "AND created_at<%s FOR UPDATE",
                    (created_by, cutoff),
                ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=ANY(%s)", (ids,)
            )
            connection.execute("DELETE FROM notebooks WHERE id=ANY(%s)", (ids,))
        return len(ids)

    @staticmethod
    def insert_row_values(connection, table: str, data: dict) -> None:
        if table not in _COPY_INSERT_TABLES:
            raise ValueError("unsupported copy table")
        data = normalize_timestamp_row(table, data)
        columns = list(data)
        values = []
        json_columns = _JSON_COLUMNS.get(table, set())
        for column in columns:
            value = data[column]
            if column in json_columns:
                if isinstance(value, str):
                    value = json.loads(value or "null")
                value = Jsonb(value)
            values.append(value)
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns),
        )
        connection.execute(statement, values)
