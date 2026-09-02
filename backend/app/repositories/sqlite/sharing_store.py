from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Sequence

from app.core.config import Settings
from app.repositories.group_rows import (
    GROUP_GRANT_COUNT_SQL,
    GROUP_GRANT_EXISTS_SQL,
)
from app.repositories.ports import NotebookTooLargeToCopyError
from app.repositories.sqlite.access_sql import (
    MEMBER_PROBE_SQL,
    NOTEBOOK_ADMIN_SQL,
    NOTEBOOK_DELETE_OWNER_SQL,
    NOTEBOOK_LIVE_SQL,
    NOTEBOOK_READ_SQL,
    NOTEBOOK_WRITE_SQL,
    admin_access_params,
    read_access_params,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowhow_history_store import record_change

# knowhow-table content, PR-2+3 Task 13: knowledge_objects/knowledge_relations
# derived FROM a knowhow hidden source stay EXCLUDED from a deep copy — they
# are rebuilt from scratch by project_table (design doc §④), never copied
# directly, so a stale/renamed-away copy of them could never happen. Every
# OTHER leg (the hidden source itself, its elements, its chunks+vectors, and
# the five knowhow business tables + notebook_assets below) DOES travel with
# a copy (PR-2 supersedes PR-1's blanket exclusion — see
# app.services.notebook_sharing.NotebookCopyService.copy_notebook for the id
# remap: element/chunk ids are RECOMPUTED via
# app.services.knowhow.projection.element_id/cell_chunk_id, the same stable
# formula project_table itself uses, so the copy's post-copy reprojection
# finds every chunk already (id, text, section_path)-identical and makes
# ZERO additional embedder calls). validate_copy applies the SAME
# still-excluded predicate to its source-side parity counts so the
# deliberate KO/relation omission is never mistaken for a copy error.
_KNOWHOW_SOURCE_IDS = "SELECT id FROM sources WHERE source_type = 'knowhow'"

# Deep-copy row snapshot: table -> the exact SELECT the former mixin issued.
# "notebooks" carries the single source row that the copy service rewrites
# into the destination's hidden 'copying' sentinel. Order matters only for
# readability; the INSERT order (FK-safe) is owned by NotebookCopyService.
#
# Deliberately absent: `catalog_jobs` / `catalog_candidates` (command-catalog
# extraction, schema v38). Those rows are transient *process* state — one run's
# job progress and its not-yet-confirmed review queue — not knowledge. What a
# person actually confirmed already lives in an ordinary knowhow table, which
# this snapshot does copy. A copy therefore arrives with no half-reviewed
# queue, which is the right shape: the queue belongs to the run that produced
# it. Recorded here rather than left silent so the next reader can tell a
# decision from an omission.
#
# Also deliberately absent: `notebook_grants` (group knowledge sharing P1,
# schema v49), for the same reason `notebook_members` (share-token readers)
# already isn't copied. Access-control state is not knowledge: who else can
# read a notebook is a property of the ORIGINAL notebook, not of the
# knowledge inside it, and a copy is a brand-new notebook the new owner alone
# controls. Carrying grants across would silently hand the copy's owner's
# collaborators to whoever they were on the source — the new owner must
# re-grant access explicitly. `groups`/`group_members` need no mention here:
# they hang off no `notebook_id` at all, so they were never candidates for
# this per-notebook snapshot in the first place.
#
# Also deliberately absent: `notebook_share_requests` (group knowledge
# sharing P2, schema v50). It doubles as both reasons above: a pending
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
# (Agentic Memory P1, schema v51). The copy starts its understanding from
# scratch — registered as intentional in the feature's design doc. What those
# blocks hold is how this library came to be *used and read*, not the knowledge
# in it: the shared base layer describes a corpus that the copy's owner will
# grow differently from here, and each per-member overlay is that one member's
# retrieval habit, which no copy recipient inherits. The job rows are the same
# kind of transient *process* state as `catalog_jobs` above (one chain's run
# status plus its threshold counter), and a copy arriving mid-count would be
# counting source-notebook activity toward a notebook that has had none.
#
# Also deliberately absent: `agent_observations` (Agentic Memory P3, T1,
# schema v55). Same reasoning as `agent_notebook_profile`/`agent_profile_jobs`
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
# `retrieval_experiences` (Agentic Memory P2, schema v53) is absent for a
# structurally different reason and is NOT a decision this snapshot could make
# either way: it is deployment-GLOBAL — it has no `notebook_id` column and no
# owner column at all, so every query in this list is built on a predicate it
# does not have. Deep copy cannot reach it, the same sentence that already
# covers `groups`/`group_members`. Its rows are general tactics for HOW to
# search rather than anything belonging to a notebook, so a copy inherits them
# by simply existing in the same deployment.
_COPY_SNAPSHOT_QUERIES: tuple[tuple[str, str], ...] = (
    # codex #659 R14 P1: liveness-filtered — a tombstone landing between the
    # route's token resolution and this snapshot must stop the copy here,
    # not materialise a deleting/half-cleared notebook. Every OTHER query in
    # this tuple is anchored off the root's own id/joins and reads inside
    # the SAME pinned snapshot transaction as this one (BEGIN above pins the
    # view at the first read, i.e. `_copy_limit_violation`'s own first
    # query) — a concurrent delete job's phase-3 commits are invisible to
    # this transaction regardless, so re-adding the predicate to every
    # child-table query below would be redundant, not merely optional: this
    # one row's liveness (checked in the SAME snapshot) is definitionally
    # what every other row in this dict is consistent with.
    ("notebooks", f"SELECT * FROM notebooks WHERE id = ? AND {NOTEBOOK_LIVE_SQL}"),
    ("sources", "SELECT * FROM sources WHERE notebook_id = ?"),
    (
        # 1:1 with sources (PK = source_id) — joined the same way as the
        # sibling per-source tables below rather than filtered on its own
        # notebook_id column, so the knowhow exclusion predicate stays
        # identical everywhere (a knowhow hidden source never gets paper
        # meta in practice, but this keeps the filter style uniform).
        "source_paper_meta",
        "SELECT spm.* FROM source_paper_meta spm JOIN sources s ON s.id = spm.source_id "
        "WHERE s.notebook_id = ? AND s.source_type != 'knowhow'",
    ),
    (
        "source_authors",
        "SELECT sa.* FROM source_authors sa JOIN sources s ON s.id = sa.source_id "
        "WHERE s.notebook_id = ? AND s.source_type != 'knowhow'",
    ),
    (
        "source_elements",
        "SELECT se.* FROM source_elements se JOIN sources s ON s.id = se.source_id "
        "WHERE s.notebook_id = ?",
    ),
    ("chunks", "SELECT * FROM chunks WHERE notebook_id = ?"),
    (
        "knowledge_objects",
        f"SELECT * FROM knowledge_objects WHERE notebook_id = ? "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})",
    ),
    (
        "knowledge_source_facts",
        f"SELECT * FROM knowledge_source_facts WHERE notebook_id = ? "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})",
    ),
    (
        "knowledge_source_fact_elements",
        f"SELECT * FROM knowledge_source_fact_elements WHERE notebook_id = ? "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})",
    ),
    (
        "knowledge_source_fact_backfills",
        f"SELECT * FROM knowledge_source_fact_backfills WHERE notebook_id = ? "
        f"AND status IN ('complete','incomplete') "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})",
    ),
    (
        "knowledge_relations",
        f"SELECT * FROM knowledge_relations WHERE notebook_id = ? "
        f"AND (source_id IS NULL OR source_id NOT IN ({_KNOWHOW_SOURCE_IDS}))",
    ),
    ("chunk_embeddings", "SELECT * FROM chunk_embeddings WHERE notebook_id = ?"),
    ("chunk_questions", "SELECT * FROM chunk_questions WHERE notebook_id = ?"),
    (
        # element_embeddings still excludes knowhow: the projector's
        # _write_elements never embeds an element (only chunks get vectors),
        # so this filter has always been (and remains) a harmless no-op for
        # knowhow rows — kept knowhow-filtered only for symmetry with the
        # (still-excluded) KO/relation legs, not because any row would
        # otherwise appear.
        "element_embeddings",
        "SELECT ee.* FROM element_embeddings ee JOIN sources s ON s.id = ee.source_id "
        "WHERE s.notebook_id = ? AND s.source_type != 'knowhow'",
    ),
    # knowledge_embeddings / relation_embeddings / concept_clusters never hold
    # knowhow rows (the projector writes none), so they need no knowhow filter.
    ("knowledge_embeddings", "SELECT * FROM knowledge_embeddings WHERE notebook_id = ?"),
    ("relation_embeddings", "SELECT * FROM relation_embeddings WHERE notebook_id = ?"),
    ("concept_clusters", "SELECT * FROM concept_clusters WHERE notebook_id = ?"),
    (
        "notebook_object_schemas",
        "SELECT * FROM notebook_object_schemas WHERE notebook_id = ?",
    ),
    # --- PR-2+3 Task 13: knowhow business tables (their own source of truth;
    # travel WITH the copy, fresh ids, joined down from knowhow_tables since
    # none of columns/rows/cells/cell_code carry a notebook_id of their own).
    ("knowhow_tables", "SELECT * FROM knowhow_tables WHERE notebook_id = ?"),
    (
        "knowhow_columns",
        "SELECT kc.* FROM knowhow_columns kc "
        "JOIN knowhow_tables kt ON kt.id = kc.table_id WHERE kt.notebook_id = ?",
    ),
    (
        "knowhow_rows",
        "SELECT kr.* FROM knowhow_rows kr "
        "JOIN knowhow_tables kt ON kt.id = kr.table_id WHERE kt.notebook_id = ?",
    ),
    (
        "knowhow_cells",
        "SELECT kc.* FROM knowhow_cells kc "
        "JOIN knowhow_rows kr ON kr.id = kc.row_id "
        "JOIN knowhow_tables kt ON kt.id = kr.table_id WHERE kt.notebook_id = ?",
    ),
    (
        "knowhow_cell_code",
        "SELECT kcc.* FROM knowhow_cell_code kcc "
        "JOIN knowhow_rows kr ON kr.id = kcc.row_id "
        "JOIN knowhow_tables kt ON kt.id = kr.table_id WHERE kt.notebook_id = ?",
    ),
    ("notebook_assets", "SELECT * FROM notebook_assets WHERE notebook_id = ?"),
)

# Tables copy_notebook validates row-parity on after the copy completes, each
# with the knowhow-exclusion predicate (if any) that matches its snapshot
# query above. Applied to BOTH source and destination counts: a no-op on
# "" entries and the real filter on the still-excluded KO/relation legs.
_COPY_VALIDATED_TABLES: tuple[tuple[str, str], ...] = (
    # PR-2+3 Task 13 makes the knowhow hidden source + its chunks travel WITH
    # the copy (snapshot `sources`/`chunks` above carry no knowhow exclusion),
    # so their parity predicate is empty. paper_meta/authors still exclude
    # knowhow, matching their `source_type != 'knowhow'` snapshot filter (a
    # knowhow hidden source never has paper metadata in practice).
    ("sources", ""),
    ("source_paper_meta", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("source_authors", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("chunks", ""),
    ("chunk_questions", ""),
    ("knowledge_objects", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("knowledge_source_facts", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("knowledge_source_fact_elements", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("knowledge_source_fact_backfills", f"AND status IN ('complete','incomplete') "
     f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("knowledge_relations", f"AND (source_id IS NULL OR source_id NOT IN ({_KNOWHOW_SOURCE_IDS}))"),
    ("concept_clusters", ""),
    ("notebook_object_schemas", ""),
    ("knowhow_tables", ""),
    ("notebook_assets", ""),
)

# knowhow_columns/rows/cells/cell_code carry no notebook_id column of their
# own (see migrations.py _migration_16/_migration_17) — validate_copy's
# generic "WHERE notebook_id = ? {extra}" shape above cannot express their
# parity check, so each gets its own full COUNT query joined down through
# knowhow_tables/knowhow_rows instead. Same both-sides-same-filter contract
# as _COPY_VALIDATED_TABLES: run once against the destination id, once
# against the source id.
_COPY_VALIDATED_JOIN_TABLES: tuple[tuple[str, str], ...] = (
    (
        "knowhow_columns",
        "SELECT COUNT(*) FROM knowhow_columns kc "
        "JOIN knowhow_tables kt ON kt.id = kc.table_id WHERE kt.notebook_id = ?",
    ),
    (
        "knowhow_rows",
        "SELECT COUNT(*) FROM knowhow_rows kr "
        "JOIN knowhow_tables kt ON kt.id = kr.table_id WHERE kt.notebook_id = ?",
    ),
    (
        "knowhow_cells",
        "SELECT COUNT(*) FROM knowhow_cells kc "
        "JOIN knowhow_rows kr ON kr.id = kc.row_id "
        "JOIN knowhow_tables kt ON kt.id = kr.table_id WHERE kt.notebook_id = ?",
    ),
    (
        "knowhow_cell_code",
        "SELECT COUNT(*) FROM knowhow_cell_code kcc "
        "JOIN knowhow_rows kr ON kr.id = kcc.row_id "
        "JOIN knowhow_tables kt ON kt.id = kr.table_id WHERE kt.notebook_id = ?",
    ),
)


class SharingStore:
    """SQLite sharing/membership rows plus the connection-taking deep-copy
    primitives (snapshot / chunked insert / compensate / sweep / validate).

    Row-level only — share-token generation policy, ID remapping, filesystem
    copy and compensation ORDERING live in app.services.notebook_sharing.
    ``insert_row`` is the facade's ``_insert_row`` compatibility seat injected
    late so per-instance monkeypatches keep observing every copied row.
    """

    def __init__(
        self,
        database: SqliteDatabase,
        settings: Settings,
        *,
        now: Callable[[], str],
        insert_row: Callable[[sqlite3.Connection, str, dict], None],
    ) -> None:
        self.database = database
        self.settings = settings
        self.now = now
        self.insert_row = insert_row

    def bind_insert_row(self, insert_row: Callable) -> None:
        self.insert_row = insert_row

    # ------------------------------------------------------------ share rows
    def set_share_token(self, notebook_id: str, token: str) -> str:
        """Mark shared.  ``token`` is the candidate for a first-time share; an
        already-shared notebook keeps its existing token (idempotent re-share)
        — the read-choose-write stays inside ONE write transaction exactly as
        the former mixin did."""
        with self.database.write() as db:
            row = db.execute(
                "SELECT is_shared, share_token FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
            chosen = (
                row["share_token"]
                if row["is_shared"] and row["share_token"]
                else token
            )
            db.execute(
                "UPDATE notebooks SET is_shared = 1, share_token = ?, updated_at = ? WHERE id = ?",
                (chosen, self.now(), notebook_id),
            )
        return str(chosen)

    def clear_share(self, notebook_id: str) -> None:
        """Unshare + kick every member in ONE write transaction."""
        with self.database.write() as db:
            db.execute(
                "UPDATE notebooks SET is_shared = 0, share_token = NULL, updated_at = ? WHERE id = ?",
                (self.now(), notebook_id),
            )
            db.execute("DELETE FROM notebook_members WHERE notebook_id = ?", (notebook_id,))

    def find_by_token(self, token: str) -> "str | None":
        """codex #659 R11 P1：并入 ``NOTEBOOK_LIVE_SQL``——tombstone 落地
        （``status='deleting'``）之后这一行仍然真实存在（相位 5 之前），
        不带这条谓词就会让 ``/shared/{token}``、``/shared/{token}/copy``、
        ``/shared/{token}/join`` 这三条路由（全部只经这一个解析点找
        notebook_id）在一本正在删除的库上继续放行——直接违反
        product-and-api.md:2334 的「立即不可见」契约。只动读侧：写侧的
        ``set_share_token``/``clear_share`` 沿用既有的 ``copying`` 哨兵纪律，
        不在这次改动范围内。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM notebooks WHERE share_token = ? AND is_shared = 1 "
                f"AND {NOTEBOOK_LIVE_SQL}",
                (token,),
            ).fetchone()
        return row["id"] if row else None

    def list_shared_by_owner(self, user_id: str) -> list[sqlite3.Row]:
        """「已分享」总览的行。

        P1-T4:范围与卡片上的「已分享」徽标同一个判据——只读共享(`is_shared`)
        **或**共享给了某个群组。只按 `is_shared` 取,徽标会亮着而这张总览说
        「尚未分享任何笔记本」,而群组共享恰恰是 owner 最需要在这里看到的一条。
        群组那半没有分享链接(`share_token` 为 NULL),由 `group_count` 自我标注,
        消费方据此渲染成「已共享给 N 个群组」而不是一个空链接框。

        codex #659 R11 P1：并入 ``NOTEBOOK_LIVE_SQL``——owner 自己的「已分享」
        总览页不应该继续挂着一本正在删除中的库；tombstone 落地后这一行必须
        立刻从这个列表里消失，与其它任何读侧列表同一口径。
        """
        with self.database.connect() as db:
            return db.execute(
                "SELECT id, name, share_token, "
                + GROUP_GRANT_COUNT_SQL + " AS group_count "
                "FROM notebooks WHERE created_by = ? "
                "AND (is_shared = 1 OR " + GROUP_GRANT_EXISTS_SQL + ") "
                f"AND {NOTEBOOK_LIVE_SQL} "
                "ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()

    def notebook_row(self, notebook_id: str) -> "sqlite3.Row | None":
        """codex #659 R16 (reverses the R11 exemption): the route-level
        ``notebook:configure`` guard alone leaves a TOCTOU — a delete
        tombstone can commit between the guard's check and this read, and an
        unfiltered read would hand ``share_state`` a tombstoned row (200
        with a share token on a deleting notebook). Filter here too; the
        guard stays as the authorization layer, this is the liveness
        layer."""
        with self.database.connect() as db:
            return db.execute(
                f"SELECT * FROM notebooks WHERE id = ? AND {NOTEBOOK_LIVE_SQL}",
                (notebook_id,),
            ).fetchone()

    @staticmethod
    def notebook_row_on(
        db: sqlite3.Connection, notebook_id: str
    ) -> "sqlite3.Row | None":
        """Read a notebook on the caller's summary-hydration snapshot.

        codex #659 R11 P1：并入 ``NOTEBOOK_LIVE_SQL``——唯一消费点是
        ``NotebookSharingService.join_shared``，经 ``POST /shared/{token}/
        join`` 到达，这条路由**没有**任何 notebook 能力守卫（凭 token 而非
        notebook_id 路径参数鉴权），不像 ``notebook_row`` 那样已经被上游
        路由守卫保护过——必须自己带这条谓词。"""
        return db.execute(
            f"SELECT * FROM notebooks WHERE id = ? AND {NOTEBOOK_LIVE_SQL}",
            (notebook_id,),
        ).fetchone()

    def shared_preview_rows(self, notebook_id: str) -> "tuple[str, list[str]]":
        """(owner_display, first-50 source titles) for the share preview.

        Excludes Memory-derived AND knowhow-table hidden synthetic sources
        (source_type IN ('memory', 'knowhow')): this title list is shown to a
        prospective copier/joiner in the /shared/{token} modal — a
        user-facing surface, hidden the same as list_sources. The preview's
        numeric source_count is hydrated from the same visible NotebookSummary;
        only copyability and size accounting use physical notebook_copy_stats."""
        with self.database.connect() as db:
            owner = db.execute(
                "SELECT u.username FROM notebooks nb LEFT JOIN users u ON u.id = nb.created_by "
                "WHERE nb.id = ?",
                (notebook_id,),
            ).fetchone()
            titles = [
                row["title"]
                for row in db.execute(
                    "SELECT title FROM sources WHERE notebook_id = ? "
                    "AND source_type NOT IN ('memory', 'knowhow') "
                    "ORDER BY created_at LIMIT 50",
                    (notebook_id,),
                ).fetchall()
            ]
        return (owner["username"] if owner and owner["username"] else "", titles)

    # ------------------------------------------------------- access & members
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        """写权:仅 owner。谓词见 `access_sql.NOTEBOOK_WRITE_SQL`。"""
        with self.database.connect() as db:
            row = db.execute(
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
        with self.database.connect() as db:
            row = db.execute(
                NOTEBOOK_DELETE_OWNER_SQL, (notebook_id, user_id)
            ).fetchone()
        return row is not None

    def user_can_admin_notebook(self, notebook_id: str, user_id: str) -> bool:
        """管理权:owner ∪ 管理级有效授权边。谓词见 `access_sql.NOTEBOOK_ADMIN_SQL`。

        P2 能力翻转的判定入口(裁决 P2-1)。刻意与 `user_can_access_notebook` **并列**
        而不是取代它:`notebook:delete` 与 Agent/MCP 面仍要恒 owner 的那一条,两条谓词
        必须能被分别引用。
        """
        with self.database.connect() as db:
            row = db.execute(
                NOTEBOOK_ADMIN_SQL, (notebook_id, *admin_access_params(user_id))
            ).fetchone()
        return row is not None

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        """读权:owner ∪ 只读成员 ∪ 有效授权边。谓词见 `access_sql.NOTEBOOK_READ_SQL`。

        一次查询答完全部分支——service 层曾写成 `写权 or is_member`(两次查询),
        收口到唯一定义点后顺带省掉一跳。
        """
        with self.database.connect() as db:
            row = db.execute(
                NOTEBOOK_READ_SQL, (notebook_id, *read_access_params(user_id))
            ).fetchone()
        return row is not None

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        with self.database.connect() as db:
            row = db.execute(MEMBER_PROBE_SQL, (notebook_id, user_id)).fetchone()
        return row is not None

    def add_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "INSERT OR IGNORE INTO notebook_members (notebook_id, user_id, role, added_at) "
                "VALUES (?, ?, 'reader', ?)",
                (notebook_id, user_id, self.now()),
            )

    @staticmethod
    def insert_member_if_live(
        db: sqlite3.Connection, notebook_id: str, user_id: str, now: str,
    ) -> int:
        """codex #659 R12 P1：``join_shared`` 专属的原子插入——**不自己开
        事务**（与 ``notebook_row_on`` 同款静态方法形状），调用方必须已经
        持有一个 ``database.write()`` 连接贯穿"读活性行 + 插入成员 + 水合
        摘要"整条链路，全程只取一次连接。

        与 ``add_member``（独立公开方法，`repository_facade`/群组等其它
        调用方仍在用，不能改它的签名或语义）刻意分开：这里是 ``INSERT OR
        IGNORE ... SELECT ... WHERE EXISTS(...)`` 形——已是成员时仍幂等
        no-op（同 ``add_member`` 的 upsert 语义），但笔记本不在场/非活时
        整条 SELECT 空集，一行都不插；这与 ``ensure_conversation``
        （round 6 P2）同一款「读侧可见性并入写语句」用法，不碰写侧
        ``copying``/``deleting`` 哨兵纪律本身。

        codex #659 R14 P2：返回受影响行数（0 或 1）——rowcount==0 本身有
        歧义（「已是成员」的幂等 no-op 与「活性谓词挡下」都会是 0），调用方
        （``join_shared``）必须在同一事务内用 ``is_member_on`` 再判一次区分
        这两种情况，见那边的完整理由。"""
        cursor = db.execute(
            "INSERT OR IGNORE INTO notebook_members (notebook_id, user_id, role, added_at) "
            "SELECT ?, ?, 'reader', ? WHERE EXISTS ("
            f"SELECT 1 FROM notebooks WHERE id = ? AND {NOTEBOOK_LIVE_SQL})",
            (notebook_id, user_id, now, notebook_id),
        )
        return cursor.rowcount

    @staticmethod
    def is_member_on(db: sqlite3.Connection, notebook_id: str, user_id: str) -> bool:
        """codex #659 R14 P2：``is_member`` 的同连接变体——供
        ``join_shared`` 在 ``insert_member_if_live`` 返回 0 行后，在**同一个**
        写事务/连接内再判一次「是否已是成员」，不为此额外取第二个连接
        （R12 P1 刚收口的池耗尽风险）。"""
        row = db.execute(MEMBER_PROBE_SQL, (notebook_id, user_id)).fetchone()
        return row is not None

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user_id),
            )

    def kick_all_members(self, notebook_id: str) -> None:
        with self.database.write() as db:
            db.execute("DELETE FROM notebook_members WHERE notebook_id = ?", (notebook_id,))

    def list_members(self, notebook_id: str) -> list:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT u.username AS username, m.added_at AS added_at "
                "FROM notebook_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.notebook_id = ? ORDER BY m.added_at ASC",
                (notebook_id,),
            ).fetchall()
        return [
            {"username": row["username"], "added_at": row["added_at"]} for row in rows
        ]

    # ------------------------------------------------------------- ownership
    def source_owner(self, source_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM sources s "
                "JOIN notebooks nb ON nb.id = s.notebook_id WHERE s.id = ?",
                (source_id,),
            ).fetchone()
        return row["owner"] if row else None

    def conversation_owner(self, conversation_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT created_by AS owner FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return row["owner"] if row else None

    def answer_owner(self, answer_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM answers a "
                "JOIN notebooks nb ON nb.id = a.notebook_id WHERE a.id = ?",
                (answer_id,),
            ).fetchone()
        return row["owner"] if row else None

    def source_notebook_id(self, source_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    def answer_notebook_id(self, answer_id: str) -> "str | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM answers WHERE id = ?", (answer_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    # ------------------------------------------------------- copy primitives
    def snapshot_copy_rows(self, notebook_id: str) -> dict[str, list[dict]]:
        """Read every copyable table's rows in ONE stable-snapshot read
        transaction, with the copyable-row bound enforced atomically inside it.

        `BEGIN` (deferred) pins the WAL read snapshot at the first read, so the
        row-count check and every table fetchall below observe the SAME data — a
        concurrent ingestion commit cannot slip an over-limit notebook between
        the check and the materialisation (codex PR#353 r3: the former
        separate-connection recheck couldn't guarantee this). Over the copyable
        row limit → raise BEFORE any fetchall, so the oversized rows are never
        materialised (the 300GB+ OOM this guards). f.chunks + f.nodes = all
        chunks + all knowledge_objects (mirrors load_notebook_scale_facts /
        copy_stats).

        The chunks+nodes gate above does NOT bound the other materialised tables
        (relations / embeddings / elements / knowhow), whose combined row payload
        is what the copy actually holds in memory. A source whose graph/embedding
        fan-out dwarfs its chunk+node count (few nodes, millions of relations)
        passes that gate yet would materialise gigabytes here. The second bound
        caps the SUM of every snapshot table's rows — counted, not fetched, in the
        SAME snapshot transaction — and raises before any fetchall, so peak copy
        memory is decoupled from pathological fan-out (codex PR#353 r5 P2). The
        enclosing ``with`` commits (read-only no-op) or, on a raise, rolls back.

        codex #659 R14 P1: the root ``notebooks`` row is read liveness-filtered
        (see ``_COPY_SNAPSHOT_QUERIES``'s comment) — an empty result means
        either the id never existed or a tombstone landed before this snapshot
        pinned, in either case indistinguishable from "not found" to the
        caller. Raising ``KeyError`` HERE (rather than letting the caller's
        ``snapshot["notebooks"][0]`` bare-index) gives the route's existing
        ``except KeyError: 404`` something to catch, matching every other
        share-surface TOCTOU guard from R11."""
        snapshot: dict[str, list[dict]] = {}
        with self.database.connect() as db:
            db.execute("BEGIN")
            violation = self._copy_limit_violation(db, notebook_id)
            if violation is not None:
                raise NotebookTooLargeToCopyError(violation)
            for table, sql in _COPY_SNAPSHOT_QUERIES:
                snapshot[table] = [
                    dict(row) for row in db.execute(sql, (notebook_id,)).fetchall()
                ]
                if table == "notebooks" and not snapshot[table]:
                    raise KeyError(notebook_id)
        return snapshot

    def _copy_limit_violation(self, db, notebook_id: str) -> "str | None":
        """Both copy bounds, counted (not fetched) on the caller's connection:
        the message to raise if either is crossed, else None. Single source of
        truth shared by snapshot_copy_rows (atomic, in its BEGIN read snapshot)
        and snapshot_copy_within_limits (the fresh share-routing recheck) — so the
        copyable verdict and the guard can never disagree on which notebooks are
        over-limit (codex PR#354 r2 P2)."""
        total = int(db.execute(
            "SELECT (SELECT COUNT(*) FROM chunks WHERE notebook_id=?) + "
            "(SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?) AS n",
            (notebook_id, notebook_id),
        ).fetchone()["n"])
        if total > self.settings.notebook_copy_max_rows:
            return (
                f"notebook {notebook_id} crossed the copy-size limit "
                f"({total} rows > {self.settings.notebook_copy_max_rows}); "
                f"share read-only instead"
            )
        materialised = sum(
            int(db.execute(f"SELECT COUNT(*) FROM ({sql}) AS _c", (notebook_id,)).fetchone()[0])
            for _table, sql in _COPY_SNAPSHOT_QUERIES
        )
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
        copy_stats (assets/sources grow without bumping that version) — a notebook
        the guard would 409 is offered as read-only join, not a dead-end copy
        (codex PR#354 r2 P2). Own read transaction; COUNT-only."""
        with self.database.connect() as db:
            db.execute("BEGIN")
            return self._copy_limit_violation(db, notebook_id) is None

    def insert_copy_rows(
        self,
        table: str,
        rows: Sequence[dict],
        *,
        chunk_size: int,
    ) -> None:
        """Chunked insert: one write transaction per chunk (the write lock is
        released between chunks, P1-4), every row through the facade's
        ``_insert_row`` seat."""
        for index in range(0, len(rows), chunk_size):
            with self.database.write() as db:
                for data in rows[index:index + chunk_size]:
                    self.insert_row(db, table, data)

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
        创世流水（codex 第 2 轮 P2）。

        单表传输（``copy_table``/``move_table``）早就在自己事务的最后记了这条
        流水，但整本深拷贝走的是本 store 的批量 ``insert_copy_rows``、绕过了那条
        hook——于是拷贝来的表没有任何可回退到的创世点：它的第一次编辑会成为
        seq 1 并带上一个"编辑后"的指纹，谁都无法把表恢复到刚拷贝好的样子。

        **必须在该表的列/行/格/代码全部插完之后调用**：``record_change`` 记的
        指纹要反映拷贝完成后的完整表状态。title/description/columns 刻意从库里
        现查（而非依赖调用方手里可能已被 insert seat 改过的 dict），payload 形状
        与 ``create_knowhow_table``/``copy_table`` 产出的创世流水完全一致
        （``rows`` 恒为 ``[]``）。``origin='import'`` 同 ``copy_table``——这是一次性
        整表搬运、不是逐格手敲，复制/移动的语义由 ``note`` 如实区分。
        """
        if not table_ids:
            return
        with self.database.write() as db:
            for table_id in table_ids:
                trow = db.execute(
                    "SELECT title, description FROM knowhow_tables WHERE id = ?",
                    (table_id,),
                ).fetchone()
                if trow is None:
                    continue  # 防御：表已不在（正常拷贝路径不会发生）
                columns = [
                    {
                        "id": c["id"], "name": c["name"],
                        "role": c["role"], "position": c["position"],
                    }
                    for c in db.execute(
                        "SELECT id, name, role, position FROM knowhow_columns "
                        "WHERE table_id = ? ORDER BY position",
                        (table_id,),
                    ).fetchall()
                ]
                record_change(
                    db, new_id=new_id, now=now, table_id=table_id,
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

    def insert_fts_rows(
        self,
        sql: str,
        rows: Sequence[tuple],
        *,
        chunk_size: int,
    ) -> None:
        """Chunked executemany for the FTS mirror tables (same statement shape
        as the former mixin — NOT routed through the per-row insert seat)."""
        for index in range(0, len(rows), chunk_size):
            with self.database.write() as db:
                db.executemany(sql, rows[index:index + chunk_size])

    def validate_copy(self, source_notebook_id: str, new_id: str) -> None:
        """Post-copy integrity self-check: row parity per table + no dangling
        relation endpoints.  Raises RuntimeError (copy is then compensated)."""
        with self.database.connect() as db:
            for table, extra in _COPY_VALIDATED_TABLES:
                copied_count = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE notebook_id = ? {extra}",
                    (new_id,),
                ).fetchone()[0]
                source_count = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE notebook_id = ? {extra}",
                    (source_notebook_id,),
                ).fetchone()[0]
                if copied_count != source_count:
                    raise RuntimeError(
                        f"copy_notebook: {table} 行数不一致 {copied_count}!={source_count}"
                    )
            for table, query in _COPY_VALIDATED_JOIN_TABLES:
                copied_count = db.execute(query, (new_id,)).fetchone()[0]
                source_count = db.execute(query, (source_notebook_id,)).fetchone()[0]
                if copied_count != source_count:
                    raise RuntimeError(
                        f"copy_notebook: {table} 行数不一致 {copied_count}!={source_count}"
                    )
            dangling = db.execute(
                "SELECT COUNT(*) FROM knowledge_relations r WHERE r.notebook_id = ? AND ("
                "r.source_object_id NOT IN "
                "(SELECT id FROM knowledge_objects WHERE notebook_id = ?) OR "
                "r.target_object_id NOT IN "
                "(SELECT id FROM knowledge_objects WHERE notebook_id = ?))",
                (new_id, new_id, new_id),
            ).fetchone()[0]
            if dangling:
                raise RuntimeError("copy_notebook: 关系存在悬空引用")

    def publish_copy(self, notebook_id: str, status: str) -> None:
        """Flip the hidden 'copying' sentinel to the source's original status."""
        with self.database.write() as db:
            db.execute(
                "UPDATE notebooks SET status = ?, updated_at = ? WHERE id = ?",
                (status, self.now(), notebook_id),
            )

    def compensate_copy(self, notebook_id: str) -> None:
        """Failure compensation: remove the destination's FTS mirrors, the
        no-FK knowledge_embeddings rows, and the 'copying' sentinel (children
        cascade off the notebooks row).  Never touches the source."""
        with self.database.write() as db:
            db.execute("DELETE FROM kg_objects_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute("DELETE FROM chunks_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id = ?", (notebook_id,)
            )
            db.execute(
                "DELETE FROM notebooks WHERE id = ? AND status = 'copying'", (notebook_id,)
            )

    def sweep_stale_copies(self, *, created_by: "str | None" = None) -> int:
        """Delete expired copy sentinels without touching concurrent copies."""
        cutoff = (
            datetime.now()
            - timedelta(seconds=max(1, self.settings.notebook_copy_stale_seconds))
        ).replace(microsecond=0).isoformat()
        with self.database.write() as db:
            if created_by is not None:
                rows = db.execute(
                    "SELECT id FROM notebooks WHERE status = 'copying' AND created_by = ? "
                    "AND created_at < ?",
                    (created_by, cutoff),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id FROM notebooks WHERE status = 'copying' AND created_at < ?",
                    (cutoff,),
                ).fetchall()
            stuck_ids = [row["id"] for row in rows]
            if not stuck_ids:
                return 0
            placeholders = ",".join("?" for _ in stuck_ids)
            db.execute(
                f"DELETE FROM kg_objects_fts WHERE notebook_id IN ({placeholders})", stuck_ids
            )
            db.execute(
                f"DELETE FROM chunks_fts WHERE notebook_id IN ({placeholders})", stuck_ids
            )
            db.execute(
                f"DELETE FROM knowledge_embeddings WHERE notebook_id IN ({placeholders})",
                stuck_ids,
            )
            db.execute(f"DELETE FROM notebooks WHERE id IN ({placeholders})", stuck_ids)
        return len(stuck_ids)

    @staticmethod
    def insert_row_values(db: sqlite3.Connection, table: str, data: dict) -> None:
        """Insert one dict-shaped row (Task 26: the facade `_insert_row`
        compatibility seat's SQL body, moved verbatim — the seat itself stays
        the injectable per-row boundary for copy failure injection)."""
        columns = list(data.keys())
        db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            [data[column] for column in columns],
        )
