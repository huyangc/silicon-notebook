"""E4-3 / M1 写侧的 PostgreSQL 孪生(`test_memory_kg_isolation_write.py` 的 SQLite 侧)。

PostgreSQL 是主环境:这里在真 PG 上钉住

* `merge_objects_in_transaction` 在加锁的那条语句里判定:别人的(含孤儿)Memory 派生对象
  当作不存在(KeyError → 路由 404,与未知 id 同一回答),调用者本人的跨类合并、以及证据里
  夹着别的来源的同主人合并拒绝(409 文案),拒绝时一行不写;同主人、只由其 Memory 支撑的
  两个对象照常合并(§13.2 第 9 条);共享对象照常合并;合并先锁 Memory 来源再锁对象,
  与 Memory 删除的锁序一致(并发删除等合并提交后摘掉合并写进的证据);
* 按 id 写的路由(PATCH、合并、贡献申请、关系审核)对非属主与「不存在」同答,属主保留 409;
  审批时对象已不存在 → 关闭为 object_missing;含 Memory 的笔记本不能发布为公共库(H11 计数);
* 增量融合的语句数与库里 Memory 来源条数无关(N = 0 / 5 / 1000,两个分支);
* 冲突检测的两个读者排除 Memory 派生对象、向量与关系(排除在 `LIMIT` 护栏之前);
* `incremental_fuse_source` 经真实 `PostgresRepository`:Memory 来源入口即返回、不写簇行;
  共享来源融合时 Tier2(暴力分支)不桥接到 Memory 派生 concept(对照组为普通来源);
* 新增 SQL 的 EXPLAIN pin(同 `test_memory_sql_explain_pins.py` 的判据:关掉
  seqscan/bitmapscan,断言索引路径与反连接形态)。
"""
from __future__ import annotations

import json

import pytest

from app.domain.memory_kg_isolation import (
    CROSS_CLASS_MESSAGE,
    MemoryKnowledgeMergeRefused,
)
from app.domain.vector_index import encode_vector
from app.repositories.postgres._store_utils import execute_many
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.repository_runtime import RepositoryCompatibilitySeams

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_kg_isolation_write"),
]

NOW = "2026-09-29T00:00:00+00:00"


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-test",
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed(database) -> None:
    """nb: shared source `src-s` (+ `src-s2`), Memory sources of u-a (`src-ma1`,
    `src-ma2`), of u-b (`src-mb`) and one orphan (`src-mo`, memory_id NULL); one
    concept per source, one relation per source."""
    with database.write() as db:
        execute_many(
            db,
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
            [(user, f"{user}@example.test", user, NOW, NOW, user)
             for user in ("u-a", "u-b")],
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nb','N','','','ready','u-a',%s,%s,'personal')",
            (NOW, NOW),
        )
        execute_many(
            db,
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "VALUES (%s,'nb',%s,'ask_answer','confirmed','t','x',%s,%s)",
            [(memory_id, owner, NOW, NOW)
             for memory_id, owner in (("mem-a1", "u-a"), ("mem-a2", "u-a"), ("mem-b", "u-b"))],
        )
        sources = (
            ("src-s", "file", None),
            ("src-s2", "file", None),
            ("src-ma1", "memory", "mem-a1"),
            ("src-ma2", "memory", "mem-a2"),
            ("src-mb", "memory", "mem-b"),
            ("src-mo", "memory", None),
        )
        # Batch each table in foreign-key order; every call still owns one
        # independent schema, real migration and write transaction.
        execute_many(
            db,
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) VALUES (%s,'nb',%s,%s,%s,%s,%s)",
            [(source_id, source_id, source_type, memory_id, NOW, NOW)
             for source_id, source_type, memory_id in sources],
        )
        objects = [("ko-" + source_id[4:], source_id) for source_id, _, _ in sources]
        execute_many(
            db,
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES (%s,'nb','claim','approved',%s,%s,%s,%s,%s)",
            [(object_id, json.dumps({"name": object_id}),
              json.dumps([{"source_id": source_id, "element_id": f"el-{source_id}",
                           "quoted_span": object_id}]),
              source_id, NOW, NOW)
             for object_id, source_id in objects],
        )
        execute_many(
            db,
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "VALUES (%s,'nb',%s,%s)",
            [(object_id, encode_vector([1.0] + [0.0] * 15), NOW)
             for object_id, _ in objects],
        )
        execute_many(
            db,
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) VALUES (%s,'nb',%s,%s,%s,'r',%s)",
            [("kr-" + source_id[4:], source_id, object_id, object_id, NOW)
             for object_id, source_id in objects],
        )


@pytest.fixture
def seeded(postgres_database):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    return postgres_database, GovernanceStore(postgres_database, _seams())


def _snapshot(database) -> dict:
    with database.connect() as db:
        return {
            r["id"]: (r["status"], json.dumps(r["evidence"], sort_keys=True))
            for r in db.execute(
                "SELECT id, status, evidence FROM knowledge_objects WHERE notebook_id='nb'"
            ).fetchall()
        }


@pytest.mark.parametrize(
    ("source_id", "into_id", "message"),
    [
        ("ko-ma1", "ko-s", CROSS_CLASS_MESSAGE),
        ("ko-s", "ko-ma1", CROSS_CLASS_MESSAGE),
    ],
)
def test_pg_store_refuses_the_owners_memory_merges_and_writes_nothing(
    seeded, source_id, into_id, message
):
    database, store = seeded
    before = _snapshot(database)
    with pytest.raises(MemoryKnowledgeMergeRefused) as refused:
        with database.write() as db:
            store.merge_objects_in_transaction(
                db, "nb", source_id, into_id, NOW, actor_id="u-a"
            )
    assert refused.value.user_message == message
    assert _snapshot(database) == before


def _evidence_sources(database, object_id: str) -> "set | None":
    with database.connect() as db:
        row = db.execute(
            "SELECT evidence FROM knowledge_objects WHERE id=%s", (object_id,)
        ).fetchone()
    return None if row is None else {item["source_id"] for item in row["evidence"]}


def test_pg_store_merges_two_of_the_owners_memory_objects(seeded):
    """§13.2-9 (user ruling 2026-10-21): both u-a's, both backed by her
    Memory alone → merged; the result keeps its Memory source (still hers
    alone) and cites her two Memory sources only."""
    database, store = seeded
    with database.write() as db:
        row = store.merge_objects_in_transaction(
            db, "nb", "ko-ma1", "ko-ma2", NOW, actor_id="u-a"
        )
    assert row["id"] == "ko-ma2" and row["source_id"] == "src-ma2"
    snap = _snapshot(database)
    assert snap["ko-ma1"][0] == "deprecated" and snap["ko-ma2"][0] == "approved"
    assert _evidence_sources(database, "ko-ma2") == {"src-ma1", "src-ma2"}
    for actor in ("u-b", None):
        with pytest.raises(KeyError):
            with database.write() as db:
                store.merge_objects_in_transaction(
                    db, "nb", "ko-ma2", "ko-ma1", NOW, actor_id=actor
                )


@pytest.mark.parametrize("cited", ["src-s", "src-mb", "src-mo", ""])
@pytest.mark.parametrize("carrier", ["ko-ma1", "ko-ma2"])
def test_pg_store_refuses_an_own_pair_not_backed_by_her_memory_alone(
    seeded, cited, carrier
):
    """Same owner is not enough: a shared source, another member's Memory, an
    orphan Memory source or an entry without a source id in either side's
    evidence refuses the pair (cross-class copy) and writes nothing."""
    database, store = seeded
    with database.write() as db:
        db.execute(
            "UPDATE knowledge_objects SET evidence = evidence || "
            "jsonb_build_array(jsonb_build_object('source_id', %s::text, "
            "'element_id', 'el-x', 'quoted_span', 'x')) WHERE id = %s",
            (cited, carrier),
        )
    before = _snapshot(database)
    with pytest.raises(MemoryKnowledgeMergeRefused) as refused:
        with database.write() as db:
            store.merge_objects_in_transaction(
                db, "nb", "ko-ma1", "ko-ma2", NOW, actor_id="u-a"
            )
    assert refused.value.user_message == CROSS_CLASS_MESSAGE
    assert _snapshot(database) == before


class _CommitsBetween:
    """Runs ``side_effect`` once, right after the first statement whose text
    contains ``marker`` — a concurrent commit landing between two statements
    of the store method under test."""

    def __init__(self, connection, marker: str, side_effect):
        self.connection = connection
        self.marker = marker
        self.side_effect = side_effect

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=None, **kwargs):
        result = self.connection.execute(sql, params, **kwargs)
        if self.side_effect is not None and self.marker in str(sql):
            effect, self.side_effect = self.side_effect, None
            effect()
        return result


def test_pg_merge_refuses_when_the_source_gained_a_memory_after_the_pre_lock(seeded):
    """The pre-lock read pinned the source object's Memory sources; another
    merge then commits a new Memory citation into it before the row lock.
    That source was never locked in order, so the merge is refused (retry
    copy) and writes nothing — it never takes a source lock out of order."""
    import psycopg

    from app.domain.memory_kg_isolation import MEMORY_MERGE_RETRY_MESSAGE

    database, store = seeded

    def concurrent_merge_commits():
        with psycopg.connect(database.settings.database_url, autocommit=True) as other:
            other.execute(
                "UPDATE knowledge_objects SET evidence = evidence || "
                "jsonb_build_array(jsonb_build_object('source_id', 'src-ma2', "
                "'element_id', 'el-src-ma2', 'quoted_span', 'y')) WHERE id = 'ko-ma1'"
            )

    before = _snapshot(database)
    with pytest.raises(MemoryKnowledgeMergeRefused) as refused:
        with database.write() as db:
            store.merge_objects_in_transaction(
                _CommitsBetween(db, "FOR KEY SHARE", concurrent_merge_commits),
                "nb", "ko-ma1", "ko-ma2", NOW, actor_id="u-a",
            )
    assert refused.value.user_message == MEMORY_MERGE_RETRY_MESSAGE
    after = _snapshot(database)
    assert {k: v for k, v in after.items() if k != "ko-ma1"} == {
        k: v for k, v in before.items() if k != "ko-ma1"
    }
    assert after["ko-ma1"][0] == "approved"


def test_pg_editing_a_merged_memory_keeps_the_other_memorys_object(pg_repository):
    """An edit re-ingests the Memory: under the source lock its evidence is
    stripped from the object the other Memory owns, then its old graph is
    cleared — the merged object survives with its own evidence only, instead
    of being deleted by evidence reference."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    with database.write() as db:
        GovernanceStore.merge_objects_in_transaction(
            db, "nb", "ko-ma1", "ko-ma2", NOW, actor_id="u-a"
        )
    assert _evidence_sources(database, "ko-ma2") == {"src-ma1", "src-ma2"}
    assert repo._runtime.source_ingestion.ingest_memory_source(
        "nb", "mem-a1", "t", "edited text"
    ) == "src-ma1"
    assert _evidence_sources(database, "ko-ma1") is None
    assert _evidence_sources(database, "ko-ma2") == {"src-ma2"}


def test_pg_memory_removal_waits_for_an_uncommitted_own_merge(pg_repository):
    """Lock order (source → derived rows): u-a's merge of her two Memory
    objects holds FOR KEY SHARE on the merged-away Memory's source; deleting
    that Memory (the path every delete, transfer and member exit takes)
    blocks on its FOR UPDATE until the merge commits, then strips exactly
    the entries the merge wrote. Without the pre-lock the teardown's strip
    runs before the commit, finds nothing, and the merged object keeps the
    deleted Memory's text (or is deleted with it)."""
    import threading
    import time

    import psycopg
    from psycopg.rows import dict_row

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    url = database.settings.database_url
    outcome: dict = {}

    def remove():
        try:
            outcome["removed"] = repo._runtime.source_ingestion.remove_memory_sources(
                ["src-ma1"]
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["removed"] = repr(exc)

    with psycopg.connect(url, row_factory=dict_row) as merge_tx, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        GovernanceStore.merge_objects_in_transaction(
            merge_tx, "nb", "ko-ma1", "ko-ma2", NOW, actor_id="u-a"
        )
        worker = threading.Thread(target=remove)
        worker.start()
        deadline = time.monotonic() + 10
        waiting = 0
        while time.monotonic() < deadline:
            assert worker.is_alive(), f"the removal finished without waiting: {outcome!r}"
            # Same probe as tests/postgres/test_concurrency.py: this database
            # only, an active backend blocked on the teardown's source lock.
            waiting = probe.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND datname = current_database() "
                "AND wait_event_type = 'Lock' AND state = 'active' "
                "AND query ILIKE %s",
                ("%FROM sources%FOR UPDATE%",),
            ).fetchone()["n"]
            if waiting:
                break
            time.sleep(0.01)
        assert waiting, "the Memory removal never waited on the merge's source lock"
        assert worker.is_alive()
        merge_tx.commit()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcome == {"removed": 1}
    assert _evidence_sources(database, "ko-ma1") is None
    assert _evidence_sources(database, "ko-ma2") == {"src-ma2"}
    assert _pg_status(database, "ko-ma2") == "approved"


def _pg_status(database, object_id: str) -> "str | None":
    with database.connect() as db:
        row = db.execute(
            "SELECT status FROM knowledge_objects WHERE id=%s", (object_id,)
        ).fetchone()
    return None if row is None else row["status"]


def _pg_change_memory(repo, memory_id: str, change: str) -> None:
    if change == "edit":
        assert repo._runtime.source_ingestion.ingest_memory_source(
            "nb", memory_id, "t", "edited text"
        )
    elif change == "transfer":
        with repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at,tier) VALUES ('nb-dst','D','','','ready','u-a',"
                "%s,%s,'personal') ON CONFLICT DO NOTHING",
                (NOW, NOW),
            )
        (result,) = repo._runtime.memory_service.transfer(
            "u-a", [memory_id], "nb-dst", "move", False
        )
        assert result["ok"] is True, result
    else:
        repo._runtime.memory_service.delete(memory_id, "u-a")


def _pg_own_merge(repo, source_id: str, into_id: str) -> None:
    with repo._runtime.database.write() as db:
        GovernanceStore.merge_objects_in_transaction(
            db, "nb", source_id, into_id, NOW, actor_id="u-a"
        )


@pytest.mark.parametrize("change", ["edit", "transfer", "delete"])
def test_pg_clearing_the_merge_result_restores_the_merged_away_object(
    pg_repository, change
):
    """User ruling 2026-10-21: ko-ma1 merged into ko-ma2 (mem-a2's); editing,
    transferring or deleting mem-a2 clears ko-ma2 and, just before, restores
    ko-ma1 (its Memory still exists) to approved with its own evidence."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    assert _pg_status(database, "ko-ma1") == "deprecated"
    _pg_change_memory(repo, "mem-a2", change)
    assert _pg_status(database, "ko-ma2") is None
    assert _pg_status(database, "ko-ma1") == "approved"
    assert _evidence_sources(database, "ko-ma1") == {"src-ma1"}


def test_pg_clearing_the_merged_away_memory_restores_nothing(pg_repository):
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_change_memory(repo, "mem-a1", "delete")
    assert _pg_status(database, "ko-ma1") is None
    assert _pg_status(database, "ko-ma2") == "approved"
    assert _evidence_sources(database, "ko-ma2") == {"src-ma2"}


def test_pg_an_orphaned_merged_away_object_is_not_restored(pg_repository):
    """mem-a1's row is gone (its source is an orphan): clearing the merge
    result does not bring ko-ma1 back."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    with database.write() as db:
        db.execute("UPDATE sources SET memory_id=NULL WHERE id='src-ma1'")
    _pg_change_memory(repo, "mem-a2", "delete")
    assert _pg_status(database, "ko-ma2") is None
    assert _pg_status(database, "ko-ma1") == "deprecated"


def _pg_add_third_memory(database) -> None:
    """u-a's third Memory (mem-a3 → src-ma3 → ko-ma3) for the chain cases."""
    with database.write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "VALUES ('mem-a3','nb','u-a','ask_answer','confirmed','t','x',%s,%s)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) VALUES ('src-ma3','nb','src-ma3','memory','mem-a3',%s,%s)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES ('ko-ma3','nb','claim','approved','{}',%s,'src-ma3',%s,%s)",
            (json.dumps([{"source_id": "src-ma3", "element_id": "el-src-ma3",
                          "quoted_span": "ko-ma3"}]), NOW, NOW),
        )


def test_pg_a_merge_chain_restores_one_level_per_cleared_result(pg_repository):
    """ko-ma1 → ko-ma2 → ko-ma3: deleting mem-a3 restores ko-ma2 (still
    carrying ko-ma1's entries), ko-ma1 stays deprecated; deleting mem-a2
    then restores ko-ma1."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_own_merge(repo, "ko-ma2", "ko-ma3")
    _pg_change_memory(repo, "mem-a3", "delete")
    assert _pg_status(database, "ko-ma2") == "approved"
    assert _pg_status(database, "ko-ma1") == "deprecated"
    assert _evidence_sources(database, "ko-ma2") == {"src-ma1", "src-ma2"}
    _pg_change_memory(repo, "mem-a2", "delete")
    assert _pg_status(database, "ko-ma1") == "approved"


def _wait_for_lock_wait(probe, needle: str, worker, outcome) -> None:
    """Block until another backend of this database is waiting on a lock in
    a statement matching ``needle`` (the test_concurrency.py probe)."""
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert worker.is_alive(), f"finished without waiting: {outcome!r}"
        waiting = probe.execute(
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE pid <> pg_backend_pid() AND datname = current_database() "
            "AND wait_event_type = 'Lock' AND state = 'active' AND query ILIKE %s",
            (needle,),
        ).fetchone()["n"]
        if waiting:
            return
        time.sleep(0.01)
    raise AssertionError(f"no backend waited on {needle!r}")


@pytest.mark.parametrize("holder_ends", ["rollback", "commit"])
def test_pg_a_skipped_restore_is_rerun_after_the_commit(pg_repository, holder_ends):
    """Quality P2-1: while mem-a1's source is held FOR UPDATE by something
    that does NOT clear ko-ma1 — a transaction that then rolls back, or a
    relation-completion / extraction-publish style holder that commits
    without touching the graph — deleting mem-a2 does not wait inside its
    teardown (ko-ma2 is gone while the holder still holds the lock; the
    restore was skipped), then re-runs the restore after its commit in a
    short transaction that waits; when the holder ends, ko-ma1 is restored."""
    import threading

    import psycopg
    from psycopg.rows import dict_row

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    outcome: dict = {}

    def remove():
        try:
            outcome["removed"] = repo._runtime.source_ingestion.remove_memory_sources(
                ["src-ma2"]
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["removed"] = repr(exc)

    url = database.settings.database_url
    with psycopg.connect(url, row_factory=dict_row) as holder, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        holder.execute("SELECT id FROM sources WHERE id='src-ma1' FOR UPDATE")
        worker = threading.Thread(target=remove)
        worker.start()
        # The teardown committed without waiting; its follow-up now waits.
        _wait_for_lock_wait(probe, "%FOR KEY SHARE OF s%", worker, outcome)
        assert _pg_status(database, "ko-ma2") is None
        assert _pg_status(database, "ko-ma1") == "deprecated"
        if holder_ends == "rollback":
            holder.rollback()
        else:
            holder.execute("UPDATE sources SET updated_at = updated_at WHERE id='src-ma1'")
            holder.commit()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcome == {"removed": 1}
    assert _pg_status(database, "ko-ma1") == "approved"


def test_pg_shared_merges_record_no_merge_marker(seeded):
    """Shared objects' merges write no marker: the merged-away row's payload
    is what it was."""
    database, store = seeded
    with database.connect() as db:
        before = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id='ko-s'"
        ).fetchone()["payload"]
    with database.write() as db:
        store.merge_objects_in_transaction(db, "nb", "ko-s", "ko-s2", NOW)
    with database.connect() as db:
        after = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id='ko-s'"
        ).fetchone()["payload"]
    assert after == before and "_merged_into" not in after


def test_pg_clearing_a_merged_away_link_repoints_its_followers(pg_repository):
    """ko-ma1 → ko-ma2 → ko-ma3, then mem-a2 is deleted: ko-ma1 is not
    restored (ko-ma3 carries its evidence) but now points at ko-ma3."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_own_merge(repo, "ko-ma2", "ko-ma3")
    _pg_change_memory(repo, "mem-a2", "delete")
    assert _pg_status(database, "ko-ma1") == "deprecated"
    with database.connect() as db:
        payload = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id='ko-ma1'"
        ).fetchone()["payload"]
    assert payload["_merged_into"] == "ko-ma3"
    _pg_change_memory(repo, "mem-a3", "delete")
    assert _pg_status(database, "ko-ma1") == "approved"


def test_pg_a_hand_status_change_clears_the_merge_marker(pg_repository):
    """Only a merge writes the marker; a PATCH of status (the owner restoring
    and deprecating ko-ma1 by hand) clears it, so clearing ko-ma2 then
    leaves the hand-deprecated ko-ma1 alone."""
    from app.models.knowledge import KnowledgeUpdate

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    for status in ("approved", "deprecated"):
        with database.write() as db:
            GovernanceStore.update_object_in_transaction(
                db, "nb", "ko-ma1", KnowledgeUpdate(status=status), NOW, actor_id="u-a"
            )
    with database.connect() as db:
        payload = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id='ko-ma1'"
        ).fetchone()["payload"]
    assert "_merged_into" not in payload
    _pg_change_memory(repo, "mem-a2", "delete")
    assert _pg_status(database, "ko-ma1") == "deprecated"


def test_pg_a_merged_away_object_without_evidence_is_restored(pg_repository):
    """Spec P3-3: ko-ma1's evidence is empty; merged into ko-ma2, it is
    restored when mem-a2 is deleted."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    with database.write() as db:
        db.execute("UPDATE knowledge_objects SET evidence='[]'::jsonb WHERE id='ko-ma1'")
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_change_memory(repo, "mem-a2", "delete")
    assert _pg_status(database, "ko-ma1") == "approved"


def test_pg_the_backstop_repoints_instead_of_restoring_a_second_copy(pg_repository):
    """ko-ma1 → ko-ma2 → ko-ma3, then ko-ma2's row vanishes without its
    follow-ups: ko-ma3 still carries all of ko-ma1's evidence, so the
    backstop re-points ko-ma1 there instead of restoring it."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_own_merge(repo, "ko-ma2", "ko-ma3")
    with database.write() as db:
        db.execute("DELETE FROM knowledge_objects WHERE id='ko-ma2'")
    assert repo._runtime.source_ingestion.heal_memory_merges() == 1
    assert _pg_status(database, "ko-ma1") == "deprecated"
    with database.connect() as db:
        payload = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id='ko-ma1'"
        ).fetchone()["payload"]
    assert payload["_merged_into"] == "ko-ma3"


def test_pg_a_long_held_lock_bounds_the_follow_up_and_hands_it_to_the_backstop(
    postgres_settings, tmp_path, monkeypatch
):
    """Quality P3-1: the after-commit follow-up runs on the delete's own
    thread, so its wait is bounded by its own ``SET LOCAL lock_timeout`` —
    not only by the deployment's (``POSTGRES_LOCK_TIMEOUT_SECONDS`` and the
    statement timeout, both set to 30 s here so the bound under test is the
    follow-up's). With mem-a1's
    source held far longer, deleting mem-a2 returns in bounded time, ko-ma1
    still deprecated, and the notebook is handed to a background backstop
    pass — which restores ko-ma1 once the lock goes."""
    from app.services import background_jobs
    from app.services import source_ingestion as ingestion_module
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.kg_auto_extract = False
    postgres_settings.embed_dim = 16
    postgres_settings.postgres_lock_timeout_seconds = 30
    postgres_settings.postgres_statement_timeout_seconds = 30
    repo = PostgresRepository(postgres_settings)
    try:
        _run_the_bounded_follow_up_case(repo, monkeypatch, ingestion_module, background_jobs)
    finally:
        repo._runtime.database.close()


def _run_the_bounded_follow_up_case(repo, monkeypatch, ingestion_module, background_jobs):
    import threading
    import time

    import psycopg
    from psycopg.rows import dict_row

    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    monkeypatch.setattr(ingestion_module, "MERGE_FOLLOWUP_LOCK_TIMEOUT_MS", 300)
    scheduled: list = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, **kwargs: scheduled.append((fn, kwargs, name)),
    )
    outcome: dict = {}

    def remove():
        outcome["removed"] = repo._runtime.source_ingestion.remove_memory_sources(
            ["src-ma2"]
        )

    with psycopg.connect(database.settings.database_url, row_factory=dict_row) as holder:
        holder.execute("SELECT id FROM sources WHERE id='src-ma1' FOR UPDATE")
        started = time.monotonic()
        worker = threading.Thread(target=remove)
        worker.start()
        worker.join(timeout=10)
        elapsed = time.monotonic() - started
        assert not worker.is_alive(), "the delete waited past its bound"
        assert outcome == {"removed": 1} and elapsed < 5
        assert _pg_status(database, "ko-ma2") is None
        assert _pg_status(database, "ko-ma1") == "deprecated"
        holder.rollback()
    ((fn, kwargs, name),) = scheduled
    assert name == "memory-merge-heal" and kwargs == {}
    assert fn() == 1
    assert _pg_status(database, "ko-ma1") == "approved"


@pytest.mark.parametrize("level", ["concept", "object"])
def test_pg_the_owner_graph_never_shows_internal_payload_keys(pg_repository, level):
    """Spec P2-1 on PostgreSQL: after ko-ma1 → ko-ma2, the owner's graph
    nodes carry no ``_`` payload key (the gate ``_absorbs`` stays internal)."""
    from app.core.request_context import reset_request_user, set_request_user
    from app.models.identity import UserProfile

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    token = set_request_user(UserProfile(
        id="u-a", email="u-a@example.test", display_name="u-a", role="user",
    ))
    try:
        graph = repo.unified_graph("nb", level=level)
    finally:
        reset_request_user(token)
    nodes = [n if isinstance(n, dict) else n.model_dump() for n in graph["nodes"]]
    if level == "object":
        assert "ko-ma2" in {str(n["id"]) for n in nodes}
    for node in nodes:
        assert not any(key.startswith("_") for key in (node.get("payload") or {})), node


def test_pg_one_backstop_pass_per_notebook_however_many_follow_ups_time_out(
    pg_repository, monkeypatch
):
    """Quality P3-1: two removals of the same notebook (two pages of a
    member-exit purge, say) whose follow-ups both time out on the same held
    lock hand the notebook to ONE background pass, in the light maintenance
    pool; that pass restores what both left."""
    import psycopg
    from psycopg.rows import dict_row

    from app.services import background_jobs
    from app.services import source_ingestion as ingestion_module

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    with database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES ('ko-ma1c','nb','claim','approved','{}',%s,'src-ma1',%s,%s)",
            (json.dumps([{"source_id": "src-ma1", "element_id": "el-ko-ma1c",
                          "quoted_span": "ko-ma1c"}]), NOW, NOW),
        )
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_own_merge(repo, "ko-ma1c", "ko-ma3")
    monkeypatch.setattr(ingestion_module, "MERGE_FOLLOWUP_LOCK_TIMEOUT_MS", 200)
    scheduled: list = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, **kwargs: scheduled.append((fn, name)),
    )
    assert background_jobs._maintenance_pool("memory-merge-heal") == (
        "light", "memory-merge-heal"
    )
    ingestion = repo._runtime.source_ingestion
    with psycopg.connect(database.settings.database_url, row_factory=dict_row) as holder:
        holder.execute("SELECT id FROM sources WHERE id='src-ma1' FOR UPDATE")
        assert ingestion.remove_memory_sources(["src-ma2"]) == 1
        assert ingestion.remove_memory_sources(["src-ma3"]) == 1
        holder.rollback()
    assert [name for _fn, name in scheduled] == ["memory-merge-heal"]
    (fn, _name), = scheduled
    assert fn() == 2
    assert _pg_status(database, "ko-ma1") == "approved"
    assert _pg_status(database, "ko-ma1c") == "approved"


def test_pg_a_follow_up_timing_out_during_the_pass_gets_another_round(
    pg_repository, monkeypatch
):
    """Quality P3-1(a): the notebook's backstop pass is running — it has
    already read its page — when another follow-up of the same notebook
    times out. That request submits no second pass but marks the notebook;
    the running pass reads it again when it is done, so both candidates are
    restored, and the notebook never had two passes."""
    import psycopg
    from psycopg.rows import dict_row

    from app.services import background_jobs
    from app.services import source_ingestion as ingestion_module

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    with database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES ('ko-ma1c','nb','claim','approved','{}',%s,'src-ma1',%s,%s)",
            (json.dumps([{"source_id": "src-ma1", "element_id": "el-ko-ma1c",
                          "quoted_span": "ko-ma1c"}]), NOW, NOW),
        )
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_own_merge(repo, "ko-ma1c", "ko-ma3")
    monkeypatch.setattr(ingestion_module, "MERGE_FOLLOWUP_LOCK_TIMEOUT_MS", 200)
    scheduled: list = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, name=None, **kwargs: scheduled.append(fn),
    )
    ingestion = repo._runtime.source_ingestion
    url = database.settings.database_url

    def hold_src_ma1_and(action):
        with psycopg.connect(url, row_factory=dict_row) as holder:
            holder.execute("SELECT id FROM sources WHERE id='src-ma1' FOR UPDATE")
            action()
            holder.rollback()

    hold_src_ma1_and(lambda: ingestion.remove_memory_sources(["src-ma2"]))
    (run,) = scheduled
    original = ingestion.memory_merge_heal_candidates
    reads: list = []

    def read_then_second_timeout(*args, **kwargs):
        page, after = original(*args, **kwargs)
        reads.append([a.object_id for a in page])
        if len(reads) == 1:
            # While the pass runs (its page already read): mem-a3 is deleted
            # and its follow-up times out on the same held source.
            hold_src_ma1_and(lambda: ingestion.remove_memory_sources(["src-ma3"]))
        return page, after

    monkeypatch.setattr(ingestion, "memory_merge_heal_candidates", read_then_second_timeout)
    assert run() == 2
    assert len(scheduled) == 1  # the second timeout submitted nothing
    assert reads[0] == ["ko-ma1"] and "ko-ma1c" in reads[1]
    assert _pg_status(database, "ko-ma1") == "approved"
    assert _pg_status(database, "ko-ma1c") == "approved"
    assert ingestion._merge_heal_pending == set() and ingestion._merge_heal_again == set()


def test_pg_a_repoint_whose_target_is_deleted_while_waiting_is_deferred(pg_repository):
    """Quality P3-1(b): the backstop plans "re-point ko-ma1 at ko-ma3" and
    waits on ko-ma3's row lock; the holder deletes ko-ma3 and commits. The
    locked rows then lack the target: the re-point is deferred, the
    backstop's next round re-checks ko-ma1 without ko-ma3 and restores it."""
    import threading

    import psycopg
    from psycopg.rows import dict_row

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_own_merge(repo, "ko-ma2", "ko-ma3")
    with database.write() as db:
        db.execute("DELETE FROM knowledge_objects WHERE id='ko-ma2'")
    outcome: dict = {}

    def heal():
        try:
            outcome["applied"] = repo._runtime.source_ingestion.heal_memory_merges(
                lock_timeout_ms=10000
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["applied"] = repr(exc)

    url = database.settings.database_url
    with psycopg.connect(url, row_factory=dict_row) as holder, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        holder.execute("SELECT id FROM knowledge_objects WHERE id='ko-ma3' FOR UPDATE")
        worker = threading.Thread(target=heal)
        worker.start()
        _wait_for_lock_wait(probe, "%FROM knowledge_objects%FOR UPDATE%", worker, outcome)
        holder.execute("DELETE FROM knowledge_objects WHERE id='ko-ma3'")
        holder.commit()
        worker.join(timeout=15)
    assert not worker.is_alive()
    assert outcome == {"applied": 1}
    assert _pg_status(database, "ko-ma3") is None
    assert _pg_status(database, "ko-ma1") == "approved"


def test_pg_the_backstop_never_repoints_to_a_hand_deprecated_holder(pg_repository):
    """Spec P3-1(a) on PostgreSQL: ko-ma1 → ko-ma3, the owner restores ko-ma1
    and deprecates ko-ma3 by hand, ko-ma1 → ko-ma2, and ko-ma2 vanishes
    without its follow-ups: the hand-deprecated ko-ma3 is not an eligible
    target, so ko-ma1 is restored."""
    from app.models.knowledge import KnowledgeUpdate

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma3")
    for object_id, status in (("ko-ma1", "approved"), ("ko-ma3", "deprecated")):
        with database.write() as db:
            GovernanceStore.update_object_in_transaction(
                db, "nb", object_id, KnowledgeUpdate(status=status), NOW, actor_id="u-a"
            )
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    with database.write() as db:
        db.execute("DELETE FROM knowledge_objects WHERE id='ko-ma2'")
    assert repo._runtime.source_ingestion.heal_memory_merges() == 1
    assert _pg_status(database, "ko-ma1") == "approved"
    assert _pg_status(database, "ko-ma3") == "deprecated"


def test_pg_a_repoint_opens_the_follower_gate_on_its_target(pg_repository):
    """ko-ma1 → ko-ma2 → ko-ma3 with ko-ma3's gate removed: clearing mem-a2
    re-points ko-ma1 at ko-ma3 and opens its gate (locked with the follower
    in one id-ordered statement); clearing mem-a3 then restores ko-ma1."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_add_third_memory(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    _pg_own_merge(repo, "ko-ma2", "ko-ma3")
    with database.write() as db:
        db.execute(
            "UPDATE knowledge_objects SET payload = payload - '_absorbs' WHERE id='ko-ma3'"
        )
    _pg_change_memory(repo, "mem-a2", "delete")
    with database.connect() as db:
        target = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id='ko-ma3'"
        ).fetchone()["payload"]
    assert target.get("_absorbs") is True
    _pg_change_memory(repo, "mem-a3", "delete")
    assert _pg_status(database, "ko-ma1") == "approved"


def test_pg_the_backstop_walks_the_memory_volume_not_the_tables(seeded):
    """Quality P2-A: on the bulk notebook with nothing to heal, the backstop
    reads the Memory sources through their partial index and their objects
    through the source index — no Seq Scan, no walk of a primary key."""
    from contextlib import contextmanager

    database, store = seeded
    _bulk_seed(database)
    captured: list = []
    original_connect = database.connect

    @contextmanager
    def spying_connect():
        with original_connect() as db:
            spy = _CapturingConnection(db)
            captured.append(spy)
            yield spy

    database.connect = spying_connect
    try:
        page, _after = store.heal_merged_away_candidates(10, ("", ""))
    finally:
        database.connect = original_connect
    assert page == []
    (spy,) = captured
    statements = spy.statements
    # The Memory sources once, then their objects in batches of 200 sources
    # (the two notebooks hold 204 Memory sources: two batches).
    assert len(statements) == 3
    with database.connect() as db:
        plans = [_plan(db, *s) for s in statements]
    for plan in plans:
        assert "Seq Scan" not in plan, plan
        assert "pk_knowledge_objects on knowledge_objects ko" not in plan, plan
        assert "pk_sources on sources s" not in plan, plan
    assert "idx_sources_nb_hidden_type" in plans[0], plans[0]
    for plan in plans[1:]:
        assert "idx_knowledge_objects_source" in plan, plan


def test_pg_the_startup_backstop_restores_what_a_crash_left_behind(pg_repository):
    """Self-healing criterion on PostgreSQL: deprecated, the marker names an
    object that no longer exists, its own Memory still exists."""
    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")
    with database.write() as db:
        db.execute("DELETE FROM knowledge_objects WHERE id='ko-ma2'")
    ingestion = repo._runtime.source_ingestion
    assert ingestion.has_memory_merges_to_heal() is True
    assert ingestion.heal_memory_merges(page_size=1) == 1
    assert _pg_status(database, "ko-ma1") == "approved"
    assert ingestion.has_memory_merges_to_heal() is False


def test_pg_cross_merged_memories_removed_at_once_do_not_deadlock(pg_repository):
    """Quality P2-2: A(S)→B(T) and D(T)→C(S), then T and S are removed at
    once, both source locks taken first. Each teardown is driven step by step
    on its own connection — the steps of ``remove_memory_sources``: source
    lock, merge half (``detach_memory_merges_on``), graph clear, source-row
    delete. T runs its merge half, S runs its merge half in a thread, then T
    deletes. Without the ordered up-front lock T's strip takes C and S's strip
    takes B, S's delete waits on C and T's delete waits on B: a deadlock.
    With it S queues on C before taking B, and both removals finish."""
    import threading

    import psycopg
    from psycopg.rows import dict_row

    from app.repositories.postgres.source_store import SourceStore

    repo = pg_repository
    database = repo._runtime.database
    _seed(database)
    with database.write() as db:
        for object_id, source_id in (("ko-ma1c", "src-ma1"), ("ko-ma2d", "src-ma2")):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (%s,'nb','claim','approved','{}',%s,%s,%s,%s)",
                (object_id, json.dumps([{"source_id": source_id,
                                         "element_id": f"el-{object_id}",
                                         "quoted_span": object_id}]),
                 source_id, NOW, NOW),
            )
    _pg_own_merge(repo, "ko-ma1", "ko-ma2")    # A(S) → B(T)
    _pg_own_merge(repo, "ko-ma2d", "ko-ma1c")  # D(T) → C(S)
    knowledge = repo._runtime.knowledge
    sources = repo._runtime.source_store
    url = database.settings.database_url
    outcome: dict = {}

    def finish(connection, source_id: str) -> None:
        knowledge.clear_sources_extraction_state(
            connection, [source_id], "nb", clear_embeddings=True
        )
        sources.delete_source_rows(connection, [source_id])
        connection.commit()

    with psycopg.connect(url, row_factory=dict_row) as t_tx, \
            psycopg.connect(url, row_factory=dict_row) as s_tx, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:

        def remove_s():
            try:
                GovernanceStore.detach_memory_merges_on(s_tx, "nb", ["src-ma1"], NOW)
                finish(s_tx, "src-ma1")
                outcome["s"] = "ok"
            except Exception as exc:  # surfaced below
                s_tx.rollback()
                outcome["s"] = type(exc).__name__

        SourceStore.lock_sources_for_teardown_tx(t_tx, ["src-ma2"])
        SourceStore.lock_sources_for_teardown_tx(s_tx, ["src-ma1"])
        GovernanceStore.detach_memory_merges_on(t_tx, "nb", ["src-ma2"], NOW)
        worker = threading.Thread(target=remove_s)
        worker.start()
        _wait_for_lock_wait(probe, "%knowledge_objects%", worker, outcome)
        try:
            finish(t_tx, "src-ma2")
            outcome["t"] = "ok"
        except Exception as exc:  # surfaced below
            t_tx.rollback()
            outcome["t"] = type(exc).__name__
        worker.join(timeout=15)
    assert not worker.is_alive()
    assert outcome == {"t": "ok", "s": "ok"}
    with database.connect() as db:
        left = db.execute(
            "SELECT id FROM knowledge_objects WHERE id IN "
            "('ko-ma1','ko-ma1c','ko-ma2','ko-ma2d')"
        ).fetchall()
    assert left == []


@pytest.mark.parametrize(
    ("actor", "source_id", "into_id"),
    [
        ("u-a", "ko-ma1", "ko-mb"),     # another member's Memory
        ("u-a", "ko-mb", "ko-s"),
        ("u-a", "ko-ma1", "ko-mo"),     # orphan Memory source: nobody's
        ("u-b", "ko-ma1", "ko-s"),
        (None, "ko-ma1", "ko-s"),       # internal caller owns no Memory
        ("u-a", "ko-unknown", "ko-s"),  # the answer they must all equal
    ],
)
def test_pg_store_treats_foreign_memory_as_missing(seeded, actor, source_id, into_id):
    database, store = seeded
    before = _snapshot(database)
    with pytest.raises(KeyError) as missing:
        with database.write() as db:
            store.merge_objects_in_transaction(
                db, "nb", source_id, into_id, NOW, actor_id=actor
            )
    assert type(missing.value) is KeyError
    assert _snapshot(database) == before


def test_pg_store_still_merges_shared_objects(seeded):
    database, store = seeded
    with database.write() as db:
        row = store.merge_objects_in_transaction(db, "nb", "ko-s", "ko-s2", NOW)
    assert row["id"] == "ko-s2"
    assert _snapshot(database)["ko-s"][0] == "deprecated"


def test_pg_conflict_readers_exclude_memory_rows(seeded):
    database, store = seeded
    memory_objects = {"ko-ma1", "ko-ma2", "ko-mb", "ko-mo"}
    memory_relations = {"kr-ma1", "kr-ma2", "kr-mb", "kr-mo"}
    with database.connect() as db:
        objects, vectors, _nb = store.conflict_resolution_rows(db, "nb")
        relations = store.conflict_relation_rows(db, "nb")
        bounded = store.conflict_relation_rows(db, "nb", max_rows=2)
    assert {r["id"] for r in objects} == {"ko-s", "ko-s2"}
    assert {r["object_id"] for r in vectors} == {"ko-s", "ko-s2"}
    assert {r["id"] for r in relations} == {"kr-s", "kr-s2"}
    assert {r["id"] for r in bounded} == {"kr-s", "kr-s2"}
    assert not ({r["id"] for r in objects} & memory_objects)
    assert not ({r["id"] for r in relations} & memory_relations)


# --------------------------------------------------------------------------
# incremental_fuse_source through the real PostgreSQL repository
# --------------------------------------------------------------------------

@pytest.fixture
def pg_repository(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.kg_auto_extract = False
    postgres_settings.embed_dim = 16
    repository = PostgresRepository(postgres_settings)
    yield repository
    repository._runtime.database.close()


def _fusion_seed(repository, existing_source_type: str) -> None:
    from app.services.kg_merge import _norm

    with repository._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','u-a@example.test','u-a','user','active',%s,%s,'u-a','','',0)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nbf','N','','','ready','u-a',%s,%s,'personal')",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "VALUES ('mem-f','nbf','u-a','ask_answer','confirmed','t','x',%s,%s)",
            (NOW, NOW),
        )
        for source_id, source_type, memory_id in (
            ("src-e", existing_source_type,
             "mem-f" if existing_source_type == "memory" else None),
            ("src-new", "file", None),
        ):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) VALUES (%s,'nbf',%s,%s,%s,%s,%s)",
                (source_id, source_id, source_type, memory_id, NOW, NOW),
            )
        for object_id, name, source_id, vector in (
            ("ko-e", "Expert Routing", "src-e", [1.0, 0.05] + [0.0] * 14),
            ("ko-new", "MoE Gating", "src-new", [0.99, 0.04] + [0.0] * 14),
        ):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (%s,'nbf','concept','approved',%s,'[]',%s,%s,%s)",
                (object_id, json.dumps({"name": name}), source_id, NOW, NOW),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
                "VALUES (%s,'nbf',%s,%s)",
                (object_id, encode_vector(vector), NOW),
            )
        # Legacy (pre-migration) cluster row on the existing concept, so the
        # Tier-2 exclusion is what keeps the Memory side out.
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "VALUES ('cc-e','nbf',%s,'ko-e','Expert Routing','concept',%s,0)",
            ("K-" + _norm("Expert Routing"), NOW),
        )
        # A space-1 canonical id: the library was built before S1, so its
        # published merge is in concept seed space 1.
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, concept_seed_version, updated_at) "
            "VALUES ('nbf', 1, %s) ON CONFLICT (notebook_id) DO UPDATE SET "
            "concept_seed_version = 1", (NOW,),
        )


def _clusters_and_candidates(repository):
    with repository._runtime.database.connect() as db:
        members = {r["member_object_id"] for r in db.execute(
            "SELECT member_object_id FROM concept_clusters WHERE notebook_id='nbf'"
        ).fetchall()}
        pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in db.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
            "WHERE notebook_id='nbf'"
        ).fetchall()}
    return members, pairs


@pytest.mark.parametrize("existing_source_type", ["file", "memory"])
def test_pg_tier2_never_bridges_to_a_memory_concept(pg_repository, existing_source_type):
    from app.services.kg_merge import _norm

    _fusion_seed(pg_repository, existing_source_type)
    pg_repository._runtime.knowledge_lifecycle.incremental_fuse_source("nbf", "src-new")
    members, pairs = _clusters_and_candidates(pg_repository)
    assert "ko-new" in members
    pair = tuple(sorted(("K-" + _norm("MoE Gating"), "K-" + _norm("Expert Routing"))))
    assert pairs == ({pair} if existing_source_type == "file" else set())


def test_pg_memory_source_fusion_returns_at_entry(pg_repository):
    _fusion_seed(pg_repository, "memory")
    with pg_repository._runtime.database.write() as db:
        db.execute("DELETE FROM concept_clusters WHERE notebook_id='nbf'")
    pg_repository._runtime.knowledge_lifecycle.incremental_fuse_source("nbf", "src-e")
    members, pairs = _clusters_and_candidates(pg_repository)
    assert members == set() and pairs == set()


# --------------------------------------------------------------------------
# EXPLAIN pins for the SQL this task added
# --------------------------------------------------------------------------

def _plan(connection, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


class _CapturingConnection:
    """Records each statement a store method issues, then runs it."""

    def __init__(self, connection):
        self.connection = connection
        self.statements: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=None, **kwargs):
        self.statements.append((str(sql), tuple(params or ())))
        return self.connection.execute(sql, params, **kwargs)


_EXPLAIN_TABLES = ("sources", "memory_items", "knowledge_objects",
                   "knowledge_relations", "knowledge_embeddings")


def _bulk_seed(database) -> None:
    """A non-trivial second notebook (same shape as the E0 memory_sql pins:
    4000 sources of which 200 Memory, 8000 objects/relations) so the planner
    has real cost differences to choose between."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nbx','X','','','ready','u-a',%s,%s,'personal')",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'memx-'||g,'nbx',CASE WHEN g%%2=0 THEN 'u-a' ELSE 'u-b' END,"
            "'ask_answer','confirmed','t','x',%s,%s FROM generate_series(0,199) g",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'srcx-'||g,'nbx','t',"
            "CASE WHEN g<200 THEN 'memory' ELSE 'upload' END,"
            "CASE WHEN g<200 THEN 'memx-'||g END,%s,%s FROM generate_series(0,3999) g",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "created_at,updated_at) SELECT 'kox-'||g,'nbx','concept','approved',"
            "'srcx-'||(g%%4000),%s,%s FROM generate_series(0,7999) g",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "SELECT 'kox-'||g,'nbx',%s,%s FROM generate_series(0,7999) g",
            (encode_vector([1.0] + [0.0] * 15), NOW),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) SELECT 'krx-'||g,'nbx','srcx-'||(g%%4000),"
            "'kox-0','kox-1','r',%s FROM generate_series(0,7999) g",
            (NOW,),
        )
    import psycopg

    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        for table in _EXPLAIN_TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


def test_pg_new_statements_keep_index_paths(seeded, monkeypatch):
    database, store = seeded
    _bulk_seed(database)
    with database.connect() as db:
        captured = _CapturingConnection(db)
        store.conflict_resolution_rows(captured, "nbx")
        # The production rail: kg_conflict_max_relations (1,000,000) + 1.
        store.conflict_relation_rows(captured, "nbx", max_rows=1_000_001)
        detection = [s for s in captured.statements if "memory" in s[0]]
    assert len(detection) == 3  # objects, vectors, relations
    with database.write() as db:
        merge_capture = _CapturingConnection(db)
        store.merge_objects_in_transaction(
            merge_capture, "nb", "ko-s", "ko-s2", NOW, actor_id="u-a"
        )
    (locking,) = [s for s in merge_capture.statements if "FOR UPDATE" in s[0]]
    # An own-Memory pair in the bulk notebook (kox-0 / kox-2: u-a's Memory
    # sources srcx-0 / srcx-2) exercises the pre-lock's evidence arm and the
    # own-evidence check.
    with database.write() as db:
        for object_id, source in (("kox-0", "srcx-0"), ("kox-2", "srcx-2")):
            db.execute(
                "UPDATE knowledge_objects SET evidence = jsonb_build_array("
                "jsonb_build_object('source_id', %s::text, 'element_id', %s::text, "
                "'quoted_span', 'q')) WHERE id = %s",
                (source, f"el-{source}", object_id),
            )
    with database.write() as db:
        own_capture = _CapturingConnection(db)
        store.merge_objects_in_transaction(
            own_capture, "nbx", "kox-0", "kox-2", NOW, actor_id="u-a"
        )
    (pre_lock,) = [s for s in own_capture.statements if "FOR KEY SHARE" in s[0]]
    (own_check,) = [
        s for s in own_capture.statements
        if "FROM sources s WHERE" in s[0] and "rm.created_by" in s[0]
    ]
    (shared_pre_lock,) = [s for s in merge_capture.statements if "FOR KEY SHARE" in s[0]]

    with database.connect() as db:
        plans = [_plan(db, sql, params) for sql, params in detection]
        merge_plan = _plan(db, *locking)
        pre_lock_plans = [_plan(db, *pre_lock), _plan(db, *shared_pre_lock)]
        own_plan = _plan(db, *own_check)
    for plan in pre_lock_plans:
        # Two primary-key probes of the source object, then primary-key
        # probes of the cited sources: nothing scales with the notebook.
        assert "Seq Scan" not in plan, plan
        assert "pk_knowledge_objects" in plan and "pk_sources" in plan, plan
    assert "Seq Scan" not in own_plan, own_plan
    assert "pk_sources" in own_plan and "pk_memory_items" in own_plan, own_plan

    # The merge half of clearing kox-2's Memory (kox-0 was merged into it):
    # the up-front lock reaches the source's objects and the reverse index by
    # index; the follower read is driven by the notebook's Memory sources;
    # the follow-up lock and UPDATE reach the recorded ids by an index — the
    # primary key, or (notebook, status='deprecated') when statistics say
    # the notebook holds few deprecated rows. Never a scan.
    with database.write() as db:
        merge_half = _CapturingConnection(db)
        detached = GovernanceStore.detach_memory_merges_on(
            merge_half, "nbx", ["srcx-2"], NOW
        )
        db.execute("ROLLBACK")
    assert [a.object_id for a in detached.applied] == ["kox-0"]
    assert detached.skipped == ()
    (up_front,) = [s for s in merge_half.statements if "FOR UPDATE OF ko" in s[0]]
    (followers,) = [s for s in merge_half.statements if "JOIN knowledge_objects ko ON" in s[0]]
    follow_ups = [
        s for s in merge_half.statements
        if "FOR KEY SHARE OF s SKIP LOCKED" in s[0] or "FOR UPDATE SKIP LOCKED" in s[0]
        or s[0].startswith("UPDATE knowledge_objects SET payload = %s, status = %s")
    ]
    assert len(follow_ups) == 3
    with database.connect() as db:
        up_front_plan = _plan(db, *up_front)
        followers_plan = _plan(db, *followers)
        follow_up_plans = [_plan(db, *s) for s in follow_ups]
    for plan in (up_front_plan, followers_plan, *follow_up_plans):
        assert "Seq Scan" not in plan, plan
    # nbx has no certified reverse index: the candidates are read first (by
    # source index / containment, as the strip reads them) and locked by id.
    assert (
        "idx_knowledge_objects_source" in up_front_plan
        or "using pk_knowledge_objects" in up_front_plan
    ), up_front_plan
    # Either side of the join may drive, by statistics: the notebook's Memory
    # sources, or its deprecated objects — both notebook-bounded indexes.
    assert (
        "idx_sources_nb_hidden_type" in followers_plan
        or "using idx_knowledge_objects_nb_status" in followers_plan
    ), followers_plan
    for plan in follow_up_plans:
        assert (
            "using pk_knowledge_objects" in plan
            or "using idx_knowledge_objects_nb_status" in plan
        ), plan

    # A plain Memory removal (no merge anywhere near it: none of its objects
    # has ABSORBS_PAYLOAD_KEY, so no follower read) pays, over the strip it
    # always ran: with a certified reverse index (production's normal state)
    # exactly one indexed statement — the up-front lock; without one, also
    # the read of the sources' own object ids (the containment scan itself is
    # read once and handed to the strip).
    def statement_delta() -> int:
        with database.write() as db:
            plain = _CapturingConnection(db)
            nothing = GovernanceStore.detach_memory_merges_on(
                plain, "nbx", ["srcx-4"], NOW
            )
            db.execute("ROLLBACK")
        assert nothing.applied == () and nothing.skipped == ()
        with database.write() as db:
            strip_only = _CapturingConnection(db)
            GovernanceStore.strip_sources_evidence_on(strip_only, "nbx", ["srcx-4"], NOW)
            db.execute("ROLLBACK")
        return len(plain.statements) - len(strip_only.statements)

    assert statement_delta() == 2
    from app.repositories.postgres.knowledge_store import KnowledgeStore as _Knowledge

    monkeypatch.setattr(
        _Knowledge, "source_index_backfilled", staticmethod(lambda db, nb: True)
    )
    assert statement_delta() == 1
    monkeypatch.undo()

    for plan in plans:
        assert "Seq Scan" not in plan, plan
        # D4 classifier decorrelated into an anti join whose inner side reads
        # the Memory sources through idx_sources_nb_hidden_type (all of the
        # deployment's: the classifier is not notebook-scoped) — no per-row
        # SubPlan.
        assert "Anti Join" in plan and "SubPlan" not in plan, plan
        assert "idx_sources_nb_hidden_type" in plan, plan
    assert "Seq Scan" not in merge_plan, merge_plan
    # The classifier and the "not someone else's Memory" filter are
    # primary-key probes on the two locked rows' sources / memory rows.
    assert "pk_sources" in merge_plan, merge_plan
    assert "pk_memory_items" in merge_plan, merge_plan


# --------------------------------------------------------------------------
# Promotion through the real HTTP routes on PostgreSQL
# (scenarios shared with SQLite: tests/memory_promotion_isolation_cases.py)
# --------------------------------------------------------------------------

@pytest.fixture
def pg_promotion_world(postgres_scope, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.deps import repository
    from app.core.config import get_settings
    from app.main import create_app
    from tests import memory_promotion_isolation_cases as cases

    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "4")
    get_settings.cache_clear()
    client = TestClient(create_app())
    repo = repository()
    assert type(repo).__name__ == "PostgresRepository"
    try:
        yield cases, cases.build(client, repo, lambda sql: sql.replace("?", "%s"))
    finally:
        repo.close()


def test_pg_member_cannot_propose_an_object_derived_from_anothers_memory(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.member_b_cannot_propose_a_memory_object(world)


def test_pg_owner_is_told_why_her_memory_object_cannot_be_proposed(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_cannot_propose_her_memory_object_here(world)


def test_pg_non_owner_write_routes_answer_like_a_missing_id(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.non_owner_write_routes_answer_like_a_missing_id(world)


def test_pg_owner_keeps_the_reason_on_write_routes(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_keeps_the_reason(world)


def test_pg_owner_merges_her_own_memory_objects_through_the_route(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_merges_her_own_memory_objects(world)


def test_pg_owner_memory_object_with_shared_evidence_cannot_merge(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_memory_object_with_shared_evidence_cannot_merge(world)


def test_pg_owner_merge_follows_ownership_after_promotion(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_merge_follows_ownership_after_promotion(world)


def test_pg_proposal_of_a_vanished_object_is_closed_on_approval(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.proposal_of_a_vanished_object_is_closed_on_approval(world)


def test_pg_publishing_a_notebook_with_memory_is_refused(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.publishing_a_notebook_with_memory_is_refused(world)


def test_pg_checkup_counts_memory_in_a_public_library(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.checkup_counts_memory_in_a_public_library(world)


def test_pg_shared_review_surfaces_leave_memory_rows_out(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.shared_review_surfaces_leave_memory_rows_out(world)


def test_pg_owner_lists_her_own_memory_duplicates(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_lists_her_own_memory_duplicates(world)


def test_pg_owner_duplicates_follow_the_memory_channel(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.owner_duplicates_follow_the_memory_channel(world)


def test_pg_reader_merges_only_her_own_memory_pair(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.reader_merges_only_her_own_memory_pair(world)


def test_pg_merges_on_a_mirror_keep_the_write_fence(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.merges_on_a_mirror_keep_the_write_fence(world)


def test_pg_reader_restores_her_own_merged_object(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.reader_restores_her_own_merged_object(world)


def test_pg_status_patches_on_a_mirror_keep_the_write_fence(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.status_patches_on_a_mirror_keep_the_write_fence(world)


def test_pg_reader_merges_approved_own_objects_only(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.reader_merges_approved_own_objects_only(world)


@pytest.mark.parametrize("write", ["merge", "patch"])
def test_pg_a_reader_is_judged_on_the_locked_row(pg_promotion_world, write):
    """The unlocked prefilter only ever refuses; acceptance is decided on the
    row the locking read returns. The reader's request passes the prefilter
    (both objects hers, her evidence alone), then waits on the row lock while
    another transaction gives the object a shared evidence entry and commits:
    the locked read sees it and refuses with the reader's 404, writing nothing."""
    import threading

    import psycopg
    from psycopg.rows import dict_row

    cases, world = pg_promotion_world
    c = cases._reader_with_memory(world)
    target = "ko-c-dup-1" if write == "merge" else "ko-c-dup-2"
    url = world.repo._runtime.database.settings.database_url
    outcome: dict = {}

    def ask() -> None:
        if write == "merge":
            response = cases._merge(world, c, "ko-c-dup-2", "ko-c-dup-1")
        else:
            response = cases._patch(world, c, "ko-c-dup-2", {"status": "deprecated"})
        outcome["answer"] = (response.status_code, response.json())

    with psycopg.connect(url, row_factory=dict_row) as holder, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        holder.execute("SELECT id FROM knowledge_objects WHERE id=%s FOR UPDATE", (target,))
        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        _wait_for_lock_wait(probe, "%FOR UPDATE%", worker, outcome)
        holder.execute(
            "UPDATE knowledge_objects SET evidence = evidence || %s::jsonb WHERE id=%s",
            (json.dumps([{"source_id": "src-document", "element_id": "el-src-document",
                          "quoted_span": "Shared text."}]), target),
        )
        holder.commit()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcome["answer"] == (404, {"detail": "Notebook not found"}), outcome
    assert cases._object_row(world, "ko-c-dup-2")["status"] == "approved"


def test_pg_a_readers_refused_merge_takes_no_lock(pg_promotion_world):
    """A read-only member's merge of a pair that is not her own is refused by
    an unlocked read before the merge's locking statements: with the shared
    objects (and A's Memory source) held FOR UPDATE by another transaction, her
    refusals still answer at once, so she can neither queue behind nor hold up
    other writers with ids that are not hers. Her own pair, untouched by the
    held locks, still merges."""
    import threading

    import psycopg

    cases, world = pg_promotion_world
    c = cases._reader_with_memory(world)
    url = world.repo._runtime.database.settings.database_url
    with psycopg.connect(url) as holder:
        holder.execute(
            "SELECT id FROM knowledge_objects WHERE id = ANY(%s) FOR UPDATE",
            ([cases.ORDINARY_OBJECT, cases.ORDINARY_OBJECT_2, cases.MEMORY_OBJECT,
              "ko-c-mixed"],),
        )
        holder.execute("SELECT id FROM sources WHERE id='src-memory-a' FOR UPDATE")
        answers: dict = {}

        def ask() -> None:
            for key, (source_id, into_id) in {
                "shared": (cases.ORDINARY_OBJECT, cases.ORDINARY_OBJECT_2),
                "cross": ("ko-c-dup-1", cases.ORDINARY_OBJECT),
                "foreign": ("ko-c-dup-1", cases.MEMORY_OBJECT),
                # her own object, but carrying shared evidence: the evidence half
                # of the prefilter refuses it before the row lock
                "mixed": ("ko-c-mixed", "ko-c-dup-1"),
                "own": ("ko-c-dup-2", "ko-c-dup-1"),
            }.items():
                response = cases._merge(world, c, source_id, into_id)
                answers[key] = (response.status_code, response.json())
            # the read-level status PATCH refuses before its row lock too
            response = cases._patch(world, c, cases.ORDINARY_OBJECT, {"status": "deprecated"})
            answers["patch-shared"] = (response.status_code, response.json())

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "a refused reader merge waited on a held lock"
        holder.rollback()
    refused = (404, {"detail": "Notebook not found"})
    assert answers["shared"] == answers["cross"] == answers["foreign"] == refused, answers
    assert answers["mixed"] == refused, answers
    assert answers["patch-shared"] == refused, answers
    assert answers["own"][0] == 200, answers


def test_pg_queued_memory_object_proposal_is_refused_and_closed(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.queued_memory_proposal_is_refused_on_approval(world)


def test_pg_withdrawn_memory_object_proposal_stays_withdrawn(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.withdrawn_memory_proposal_stays_withdrawn(world)


def test_pg_creator_memory_promotion_still_works_end_to_end(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.creator_memory_promotion_still_works(world)


def test_pg_ordinary_object_still_promotes(pg_promotion_world):
    cases, world = pg_promotion_world
    cases.ordinary_object_still_promotes(world)


def test_pg_promotion_statements_keep_index_paths(seeded):
    """EXPLAIN pin for the two promotion reads this task changed: both add the
    D4 classifier to a primary-key read of one object."""
    from app.domain.memory_kg_isolation import MemoryPromotionRefused

    database, store = seeded
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nb-pub','P','','','ready','u-a',%s,%s,'base')",
            (NOW, NOW),
        )
        store.insert_promotion_candidate(
            db, "promo-pin", "nb", "ko-ma1", "claim", NOW, target_base_id="nb-pub"
        )
    with database.connect() as db:
        captured = _CapturingConnection(db)
        row = store.promotion_object_type_row(captured, "nb", "ko-ma1", actor_id="u-a")
        assert row["memory_derived"] is True
        assert store.promotion_object_type_row(db, "nb", "ko-ma1", actor_id="u-b") is None
        assert store.promotion_object_type_row(
            db, "nb", "ko-s", actor_id="u-b"
        )["memory_derived"] is False
        (type_row,) = captured.statements
    with pytest.raises(MemoryPromotionRefused):
        with database.write() as db:
            approve_capture = _CapturingConnection(db)
            store.approve_promotion_in_transaction(approve_capture, "promo-pin", NOW)
    (object_read,) = [
        s for s in approve_capture.statements if "AS memory_derived" in s[0]
    ]
    _bulk_seed(database)  # realistic statistics before planning
    with database.connect() as db:
        type_plan = _plan(db, *type_row)
        approve_plan = _plan(db, *object_read)
    for plan in (type_plan, approve_plan):
        assert "Seq Scan" not in plan, plan
        assert "pk_knowledge_objects" in plan, plan
        assert "pk_sources" in plan, plan


# --------------------------------------------------------------------------
# Review round: EXPLAIN pins for every statement this round changed, taken
# from what the store methods actually execute (captured, then planned on the
# 4000-source / 8000-object notebook).
# --------------------------------------------------------------------------

def test_pg_owner_scoped_writes_and_fusion_reads_keep_index_paths(seeded):
    from app.models.knowledge import KnowledgeUpdate
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.repositories.postgres.notebook_store import NotebookStore

    database, store = seeded
    _bulk_seed(database)
    captured: dict[str, tuple[str, tuple]] = {}

    def last(conn: _CapturingConnection, needle: str) -> tuple[str, tuple]:
        (hit,) = [s for s in conn.statements if needle in s[0]]
        return hit

    with database.write() as db:
        c = _CapturingConnection(db)
        store.update_object_in_transaction(
            c, "nbx", "kox-500", KnowledgeUpdate(status="approved"), NOW, actor_id="u-a"
        )
        captured["update_object"] = last(c, "FOR UPDATE")
        c = _CapturingConnection(db)
        store.update_edge_review(c, "nbx", "krx-500", "verified", actor_id="u-a")
        captured["edge_review"] = last(c, "UPDATE knowledge_relations")
        db.rollback()
    with database.connect() as db:
        c = _CapturingConnection(db)
        KnowledgeStore.incremental_object_rows(
            c, "nbx", "srcx-3999", "concept", exclude_source=True,
            exclude_memory_derived=True,
        )
        captured["tier2_pool"] = c.statements[-1]
        c = _CapturingConnection(db)
        KnowledgeStore.valid_object_ids(
            c, [f"kox-{i}" for i in range(0, 400, 7)], exclude_memory_derived=True
        )
        captured["ann_alive"] = c.statements[-1]
        c = _CapturingConnection(db)
        NotebookStore.public_library_memory_source_count(c, "nbx")
        captured["h11"] = c.statements[-1]
    # The publish UPDATE, captured from the real store method on a notebook
    # with Memory (refused; the conditional UPDATE itself still runs).
    from contextlib import contextmanager

    from app.domain.memory_kg_isolation import NotebookHoldsMemory

    publish: list[tuple[str, tuple]] = []

    class _CaptureDatabase:
        @contextmanager
        def write(self):
            with database.write() as db:
                conn = _CapturingConnection(db)
                try:
                    yield conn
                finally:
                    publish.extend(conn.statements)

    notebooks = NotebookStore(
        _CaptureDatabase(), new_id=lambda prefix: prefix, now=lambda: NOW,
        activity_retention_days=30,
    )
    with pytest.raises(NotebookHoldsMemory):
        notebooks.set_tier("nbx", "base")
    captured["publish"] = next(s for s in publish if s[0].startswith("UPDATE notebooks"))

    with database.connect() as db:
        plans = {name: _plan(db, sql, params) for name, (sql, params) in captured.items()}
    for name, plan in plans.items():
        assert "Seq Scan" not in plan, (name, plan)
        assert "SubPlan" not in plan or name in ("update_object", "edge_review"), (name, plan)
    # Owner filter on the two single-row writes: primary-key probes.
    for name in ("update_object", "edge_review"):
        assert "pk_sources" in plans[name], (name, plans[name])
    assert "pk_knowledge_objects" in plans["update_object"], plans["update_object"]
    # The Tier-2 pool and the ANN alive-check fold the D4 classifier into the
    # read they already were: anti join on the Memory-source index.
    for name in ("tier2_pool", "ann_alive"):
        assert "Anti Join" in plans[name], (name, plans[name])
    assert "pk_knowledge_objects" in plans["ann_alive"], plans["ann_alive"]
    # Publish refusal and H11: the (notebook_id, source_type) index.
    for name in ("publish", "h11"):
        assert "idx_sources" in plans[name], (name, plans[name])


# --------------------------------------------------------------------------
# Cost pin: incremental fusion statements do not grow with Memory sources
# --------------------------------------------------------------------------

def _count_pg_statements(monkeypatch) -> dict:
    from app.repositories.postgres import database as pgdb

    counter = {"n": 0}
    original = pgdb._SafeDiagnosticConnection.execute

    def counted(self, *args, **kwargs):
        sql = str(args[0]) if args else ""
        # Pool housekeeping (connection reset / session settings on checkout)
        # varies with pool reuse, not with the work: not counted.
        if sql.strip() and not sql.startswith("RESET") and "set_config(" not in sql:
            counter["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pgdb._SafeDiagnosticConnection, "execute", counted)
    return counter


_FAR_CONCEPTS = 3000


def _cost_seed(
    repository, notebook_id: str, n: int, *, deprecated_ordinary: bool, far: bool = True
) -> None:
    from app.services.kg_merge import _norm

    with repository._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES (%s,'N','','','ready','u-c',%s,%s,'personal')",
            (notebook_id, NOW, NOW),
        )
        if n:
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                "content_md,created_at,updated_at) SELECT %s||'-mem'||g,%s,'u-c',"
                "'ask_answer','confirmed','t','x',%s,%s FROM generate_series(1,%s) g",
                (notebook_id, notebook_id, NOW, NOW, n),
            )
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) SELECT %s||'-srcm'||g,%s,'t',%s,"
                "CASE WHEN %s THEN NULL ELSE %s||'-mem'||g END,%s,%s "
                "FROM generate_series(1,%s) g",
                (notebook_id, notebook_id, "file" if deprecated_ordinary else "memory",
                 deprecated_ordinary, notebook_id, NOW, NOW, n),
            )
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) SELECT %s||'-kom'||g,%s,'concept',"
                "%s,jsonb_build_object('name','Private notion '||g),'[]',"
                "%s||'-srcm'||g,%s,%s FROM generate_series(1,%s) g",
                (notebook_id, notebook_id, "deprecated" if deprecated_ordinary else "approved",
                 notebook_id, NOW, NOW, n),
            )
            memory_vectors = []
            for g in range(1, n + 1):
                # Distinct vectors (HNSW recall degrades on exact duplicates),
                # all behind the shared concept but well above the bridge
                # threshold, so they crowd the ANN window.
                vec = [1.0] + [0.0] * 15
                vec[1 + g % 14] = 0.2 + (g % 50) * 0.002
                memory_vectors.append(
                    (f"{notebook_id}-kom{g}", notebook_id, encode_vector(vec), NOW)
                )
            execute_many(
                db,
                "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
                "VALUES (%s,%s,%s,%s)",
                memory_vectors,
            )
        for source_id in ("src-A", "src-B", "src-far"):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
                "VALUES (%s,%s,%s,'file',%s,%s)",
                (f"{notebook_id}-{source_id}", notebook_id, source_id, NOW, NOW),
            )
        # Far-away ordinary concepts (cosine to the query well under lo): the
        # widening loop's tail drops below lo on them long before k reaches
        # n_labels, so no arm ever asks hnswlib for every label (the query
        # that intermittently fails, quality re-review P1-1).
        far_rows = []
        for g in range(_FAR_CONCEPTS if far else 0):
            vec = [0.0] * 16
            vec[8 + g % 8] = 1.0
            vec[1 + g % 7] = 0.1 + (g % 97) * 0.003
            far_rows.append((f"{notebook_id}-kof{g}", notebook_id,
                        json.dumps({"name": f"Unrelated {g}"}),
                        f"{notebook_id}-src-far", NOW, NOW, encode_vector(vec)))
        cursor = db.cursor()
        cursor.executemany(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES (%s,%s,'concept','approved',%s,'[]',%s,%s,%s)",
            [row[:6] for row in far_rows],
        )
        cursor.executemany(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "VALUES (%s,%s,%s,%s)",
            [(row[0], row[1], row[6], NOW) for row in far_rows],
        )
        shared = [1.0] + [0.0] * 15
        shared[15] = 0.01
        for object_id, name, source_id, vec in (
            ("ko-s", "Expert Routing", "src-A", shared),
            ("ko-new", "MoE Gating", "src-B", [1.0] + [0.0] * 15),
        ):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (%s,%s,'concept','approved',%s,'[]',%s,%s,%s)",
                (f"{notebook_id}-{object_id}", notebook_id, json.dumps({"name": name}),
                 f"{notebook_id}-{source_id}", NOW, NOW),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
                "VALUES (%s,%s,%s,%s)",
                (f"{notebook_id}-{object_id}", notebook_id, encode_vector(vec), NOW),
            )
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "VALUES (%s,%s,%s,%s,'Expert Routing','concept',%s,0)",
            (f"{notebook_id}-cc", notebook_id, "K-" + _norm("Expert Routing"),
             f"{notebook_id}-ko-s", NOW),
        )
        # A space-1 canonical id: built before S1 (published space 1).
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, concept_seed_version, updated_at) "
            "VALUES (%s, 1, %s) ON CONFLICT (notebook_id) DO UPDATE SET "
            "concept_seed_version = 1", (notebook_id, NOW),
        )


@pytest.mark.parametrize("branch", ["bruteforce", "ann"])
def test_pg_fusion_statements_do_not_grow_with_memory_sources(pg_repository, monkeypatch, branch):
    """Twin of the SQLite cost pin (quality review P1-1 / F2): brute force is
    flat over 0 / 5 / 1,000 Memory sources, and so is ANN: the scale index
    leaves Memory-derived objects out of its ANN labels (E4-6), so 1,000
    Memory concepts never crowd the window and cost what none cost -- never
    one read per Memory source, and never more than 1,000 deprecated ordinary
    concepts in the same places (which do crowd it: the existing bounded
    doubling loop)."""
    from app.services.kg_merge import _norm

    with pg_repository._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-c','u-c@example.test','u-c','user','active',%s,%s,'u-c','','',0)",
            (NOW, NOW),
        )
    counts = {}
    for label, n, deprecated in (("0", 0, False), ("5", 5, False),
                                 ("1000", 1000, False), ("deprecated", 1000, True)):
        notebook_id = f"nbc{label}"
        # Far-away concepts only matter to the ANN window; the brute-force arm
        # stays small (it runs the whole pool through Python cosine).
        _cost_seed(pg_repository, notebook_id, n, deprecated_ordinary=deprecated,
                   far=branch == "ann")
        if branch == "ann":
            pg_repository.build_scale_index(notebook_id)
            monkeypatch.setattr(pg_repository.settings, "kg_incremental_tier2_max_entities", 0)
        new_source = f"{notebook_id}-src-B"
        pg_repository._runtime.knowledge_lifecycle.incremental_fuse_source(notebook_id, new_source)  # warm-up
        with pg_repository._runtime.database.write() as db:
            db.execute("DELETE FROM concept_merge_candidates WHERE notebook_id=%s", (notebook_id,))
        counter = _count_pg_statements(monkeypatch)
        pg_repository._runtime.knowledge_lifecycle.incremental_fuse_source(notebook_id, new_source)
        counts[label] = counter["n"]
        monkeypatch.undo()
        with pg_repository._runtime.database.connect() as db:
            pairs = {tuple(sorted((r["canonical_a"], r["canonical_b"]))) for r in db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=%s", (notebook_id,)
            ).fetchall()}
        assert pairs == {tuple(sorted(("K-" + _norm("MoE Gating"),
                                       "K-" + _norm("Expert Routing"))))}, (label, pairs)
    assert counts["0"] == counts["5"] == counts["1000"], counts
    if branch == "bruteforce":
        assert counts["1000"] == counts["deprecated"], counts
    else:
        assert counts["1000"] <= counts["deprecated"], counts


# --------------------------------------------------------------------------
# Publish window (fix3 P2): a Memory confirmed before the notebook is
# published never lands in the public library
# --------------------------------------------------------------------------

def _pg_memory_world(pg_repository, monkeypatch):
    from app.models.schemas import NotebookCreate

    repo = pg_repository
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-p','u-p@example.test','u-p','user','active',%s,%s,'u-p','','',0)",
            (NOW, NOW),
        )
    nb = repo.create_notebook(NotebookCreate(name="to be published")).id
    with repo._runtime.database.write() as db:
        db.execute("UPDATE notebooks SET created_by='u-p' WHERE id=%s", (nb,))
    monkeypatch.setattr(repo._runtime.source_ingestion.settings, "kg_auto_extract", True)
    seen: list = []
    original = repo._runtime.event_log.emit
    monkeypatch.setattr(
        repo._runtime.event_log, "emit",
        lambda event: (seen.append(dict(event)), original(event))[1],
    )
    return repo, nb, seen


def _pg_memory_rows(repo, nb: str) -> tuple[list, list]:
    with repo._runtime.database.connect() as db:
        sources = [r["id"] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=%s AND source_type='memory'", (nb,)
        ).fetchall()]
        objects = [r["id"] for r in db.execute(
            "SELECT ko.id FROM knowledge_objects ko JOIN sources s ON s.id = ko.source_id "
            "WHERE ko.notebook_id=%s AND s.source_type='memory'", (nb,)
        ).fetchall()]
    return sources, objects


def test_pg_memory_queued_before_publish_never_reaches_the_public_library(
    pg_repository, monkeypatch
):
    repo, nb, seen = _pg_memory_world(pg_repository, monkeypatch)
    service = repo._runtime.memory_service
    queued: list = []
    monkeypatch.setattr(service, "kg_ingest_scheduler", lambda fn, item: queued.append((fn, item)))
    memory = repo.create_memory_candidate(nb, "u-p", None, "req-pg-q", "Queued", "Private x", [], "", {}, [])
    repo.confirm_memory(memory.id, "u-p")
    assert len(queued) == 1

    repo.mark_notebook_base(nb)
    fn, item = queued[0]
    fn(item)

    assert _pg_memory_rows(repo, nb) == ([], [])
    assert {c.code: c.count for c in repo.checkup.run(nb).checks}["H11"] == 0
    assert [e for e in seen if e.get("status") == "skipped"] == [
        {"kind": "memory_kg", "notebook_id": nb, "memory_id": memory.id,
         "status": "skipped", "reason": "not_eligible"}
    ]


def test_pg_memory_source_insert_is_refused_in_a_public_library(pg_repository, monkeypatch):
    repo, nb, seen = _pg_memory_world(pg_repository, monkeypatch)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES ('mem-pgpub',%s,'u-p','ask_answer',"
            "'confirmed','t','Private x',%s,%s)",
            (nb, NOW, NOW),
        )
    repo.mark_notebook_base(nb)
    ingestion = repo._runtime.source_ingestion
    assert ingestion.ingest_memory_source(nb, "mem-pgpub", "t", "Private x") is None
    assert _pg_memory_rows(repo, nb) == ([], [])
    assert [e for e in seen if e.get("status") == "skipped"] == [
        {"kind": "memory_kg", "notebook_id": nb, "memory_id": "mem-pgpub",
         "status": "skipped", "reason": "public_library"}
    ]
    # Store level, and the control on a shared notebook.
    store = repo._runtime.source_store
    kwargs = dict(title="t", source_type="memory", status="active", parse_status="parsed",
                  file_name="", file_path="", file_size=0, file_hash="h", summary="",
                  doc_type="", memory_id="mem-pgpub", unless_public_library=True)
    assert store.insert_source(source_id="src-pgpub-1", notebook_id=nb, **kwargs) is False
    repo.set_notebook_personal(nb)
    assert store.insert_source(source_id="src-pgpub-2", notebook_id=nb, **kwargs) is True
    assert _pg_memory_rows(repo, nb)[0] == ["src-pgpub-2"]


def test_pg_conditional_memory_insert_keeps_the_notebook_primary_key_path(pg_repository):
    """EXPLAIN pin for the INSERT … SELECT … FOR SHARE: a primary-key read of
    the one notebook row."""
    repo = pg_repository
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="pin")).id
    store = repo._runtime.source_store
    with repo._runtime.database.write() as db:
        captured = _CapturingConnection(db)
        assert store.insert_source(
            source_id="src-pin", notebook_id=nb, title="t", source_type="memory",
            status="active", parse_status="parsed", file_name="", file_path="",
            file_size=0, file_hash="h", summary="", doc_type="", memory_id="",
            connection=captured, unless_public_library=True,
        ) is True
        (insert,) = [s for s in captured.statements if s[0].startswith("INSERT INTO sources")]
        db.rollback()
    with repo._runtime.database.write() as db:
        plan = _plan(db, *insert)
        db.rollback()
    assert "Seq Scan" not in plan, plan
    assert "pk_notebooks" in plan and "LockRows" in plan, plan


def test_pg_publish_waiting_on_an_uncommitted_memory_insert_is_refused(pg_repository):
    """Statement-level race: a Memory-source insert holds the notebook row FOR
    SHARE and has not committed yet; a publish arrives and waits. When the
    insert commits, the publish must see the new source and refuse — its
    decision is taken on a snapshot from after the lock was granted."""
    import threading
    import time

    from app.domain.memory_kg_isolation import NotebookHoldsMemory
    from app.models.schemas import NotebookCreate

    repo = pg_repository
    nb = repo.create_notebook(NotebookCreate(name="race")).id
    store = repo._runtime.source_store
    outcome: dict = {}

    def publish():
        try:
            repo._runtime.notebook_store.set_tier(nb, "base")
            outcome["result"] = "published"
        except NotebookHoldsMemory:
            outcome["result"] = "refused"
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["result"] = repr(exc)

    import psycopg
    from psycopg.rows import dict_row

    url = repo._runtime.database.settings.database_url  # carries the test schema
    # Raw connections outside the (small) test pool: the insert transaction
    # held open, and a probe that watches the publish block.
    with psycopg.connect(url, row_factory=dict_row) as insert_tx, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        assert store.insert_source(
            source_id="src-race", notebook_id=nb, title="t", source_type="memory",
            status="active", parse_status="parsed", file_name="", file_path="",
            file_size=0, file_hash="h", summary="", doc_type="", memory_id="",
            connection=insert_tx, unless_public_library=True,
        ) is True
        worker = threading.Thread(target=publish)
        worker.start()
        deadline = time.monotonic() + 10
        waiting = 0
        while time.monotonic() < deadline:
            waiting = probe.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query ILIKE '%notebooks%'"
            ).fetchone()["n"]
            if waiting:
                break
            time.sleep(0.02)
        assert waiting, "the publish never blocked on the notebook row"
        insert_tx.commit()
    worker.join(timeout=10)
    assert outcome == {"result": "refused"}
    with repo._runtime.database.connect() as db:
        tier = db.execute("SELECT tier FROM notebooks WHERE id=%s", (nb,)).fetchone()["tier"]
    assert tier == "personal"


def test_pg_shared_review_readers_keep_index_paths(seeded):
    """EXPLAIN pins (quality re-review P2-1): the edge review queue and the
    duplicate seed read leave Memory-derived rows out with the D4 classifier —
    an anti join, no Seq Scan, no per-row SubPlan."""
    from app.repositories.postgres.knowledge_store import KnowledgeStore

    database, store = seeded
    _bulk_seed(database)
    with database.connect() as db:
        c = _CapturingConnection(db)
        store.review_queue_rows(c, "nbx")
        (queue_read,) = [s for s in c.statements if "FROM knowledge_relations kr" in s[0]]
        c = _CapturingConnection(db)
        KnowledgeStore.duplicate_seed_rows(c, "nbx", "concept")
        KnowledgeStore.duplicate_seed_rows(c, "nbx", "procedure")
        seed_reads = list(c.statements)
    assert len(seed_reads) == 2
    with database.connect() as db:
        queue_plan = _plan(db, *queue_read)
        seed_plans = [_plan(db, *s) for s in seed_reads]
    # The queue read keeps its pre-existing per-row SubPlan (the evidence
    # anchor flag over jsonb_array_elements); the Memory classifier itself is
    # the decorrelated anti join on the relation's source.
    assert "Seq Scan" not in queue_plan, queue_plan
    assert "Anti Join" in queue_plan and "(kr.source_id = ds.id)" in queue_plan, queue_plan
    for plan in seed_plans:
        assert "Seq Scan" not in plan, plan
        assert "Anti Join" in plan and "SubPlan" not in plan, plan


def test_pg_bell_edge_count_keeps_its_index_path(seeded, monkeypatch):
    """EXPLAIN pin for the bell's 「关系审核」 count (fix4 P2): the pending-edge
    GROUP BY now carries the Memory exclusion — relations still reached through
    a notebook_id index, the classifier an anti join."""
    from app.repositories.postgres import database as pgdb
    from app.repositories.postgres.query_store import QueryStore

    database, _store = seeded
    _bulk_seed(database)
    captured: list[tuple[str, tuple]] = []
    original = pgdb._SafeDiagnosticConnection.execute

    def capture(self, query, params=None, *args, **kwargs):
        text = query.as_string(self) if hasattr(query, "as_string") else str(query)
        if "knowledge_relations" in text and "GROUP BY notebook_id" in text:
            captured.append((text, tuple(params or ())))
        return original(self, query, params, *args, **kwargs)

    monkeypatch.setattr(pgdb._SafeDiagnosticConnection, "execute", capture)
    projection = QueryStore(database).pending_actions_projection_rows("u-a")
    monkeypatch.undo()
    assert "nbx" in projection["notebook_ids"]
    (edge_count,) = captured
    with database.connect() as db:
        plan = _plan(db, *edge_count)
    assert "Seq Scan" not in plan, plan
    # Relations reached by a notebook_id index (the planner picks between the
    # notebook-leading indexes by statistics), never a full scan.
    assert "on knowledge_relations" in plan and "Index Cond: (notebook_id = ANY" in plan, plan
    assert "Anti Join" in plan and "SubPlan" not in plan, plan


def test_pg_memory_insert_waiting_on_an_uncommitted_publish_inserts_nothing(pg_repository):
    """Reverse order of the race above (fix4 P3-2): the publish holds the
    notebook row (FOR NO KEY UPDATE, then its UPDATE) and has not committed; a
    Memory-source insert arrives and waits on its FOR SHARE read. When the
    publish commits, the insert's locking read re-checks the new row version,
    finds tier='base' and inserts nothing."""
    import threading
    import time

    import psycopg
    from psycopg.rows import dict_row

    from app.models.schemas import NotebookCreate

    repo = pg_repository
    nb = repo.create_notebook(NotebookCreate(name="race-reverse")).id
    store = repo._runtime.source_store
    outcome: dict = {}
    url = repo._runtime.database.settings.database_url  # carries the test schema

    def insert():
        with psycopg.connect(url, row_factory=dict_row) as tx:
            try:
                outcome["inserted"] = store.insert_source(
                    source_id="src-race-rev", notebook_id=nb, title="t",
                    source_type="memory", status="active", parse_status="parsed",
                    file_name="", file_path="", file_size=0, file_hash="h",
                    summary="", doc_type="", memory_id="",
                    connection=tx, unless_public_library=True,
                )
                tx.commit()
            except Exception as exc:  # pragma: no cover - surfaced below
                outcome["inserted"] = repr(exc)

    with psycopg.connect(url, row_factory=dict_row) as publish_tx, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        publish_tx.execute("SELECT 1 FROM notebooks WHERE id=%s FOR NO KEY UPDATE", (nb,))
        publish_tx.execute("UPDATE notebooks SET tier='base' WHERE id=%s", (nb,))
        worker = threading.Thread(target=insert)
        worker.start()
        deadline = time.monotonic() + 10
        waiting = 0
        while time.monotonic() < deadline:
            waiting = probe.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query ILIKE '%INSERT INTO sources%'"
            ).fetchone()["n"]
            if waiting:
                break
            time.sleep(0.02)
        assert waiting, "the Memory insert never blocked on the notebook row"
        publish_tx.commit()
    worker.join(timeout=10)
    assert outcome == {"inserted": False}
    with repo._runtime.database.connect() as db:
        rows = db.execute("SELECT id FROM sources WHERE id='src-race-rev'").fetchall()
    assert rows == []
