"""batch-3-W1 T-5a PostgreSQL twin: the pre-reset drain's store primitives on
a real PostgreSQL (ctid form-two page, exists-at-offset backlog probe) plus
the end-to-end ``delete_notebook_kg`` equivalence — SQLite-side behaviour pins
live in ``backend/tests/test_kg_graph_drain.py``."""
from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp

pytestmark = pytest.mark.xdist_group(name="postgres_kg_graph_drain")


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _seed_graph(postgres_repository, notebook_id, *, doc_objects, memory_objects=0):
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for source_id, source_type in (("src-doc", "markdown"), ("src-mem", "memory")):
            if source_type == "memory" and not memory_objects:
                continue
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
                "file_name,file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
                "VALUES (%s,%s,%s,%s,'extracted','parsed','', '',0,%s,'',%s,%s,'')",
                (source_id, notebook_id, source_id, source_type, source_id, now, now),
            )
        for i in range(doc_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (%s,%s,'concept','approved',"
                "%s,%s,%s,%s,%s)",
                (f"ko-doc-{i}", notebook_id, jsonb({}), jsonb([]), "src-doc", now, now),
            )
        for i in range(memory_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (%s,%s,'concept','approved',"
                "%s,%s,%s,%s,%s)",
                (f"ko-mem-{i}", notebook_id, jsonb({}), jsonb([]), "src-mem", now, now),
            )


@pytest.mark.postgres_integration
def test_drain_primitives_page_and_probe_on_postgres(postgres_repository):
    """ctid 形二分页 + LIMIT/OFFSET 探针在真 PG 上的行为:探针命名超阈值的表、
    每页有界、排干后探针转 None。"""
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="drain-primitives")
    ).id
    _seed_graph(postgres_repository, notebook_id, doc_objects=7)
    runtime = postgres_repository._runtime
    store = runtime.knowledge

    with runtime.database.connect() as db:
        assert store.graph_drain_backlog(db, notebook_id, 3) == ("knowledge_objects", 6)
        assert store.graph_drain_backlog(db, notebook_id, 7) is None
        # start 游标(评审 F4):从 knowledge_objects(预排水步加入后下标 6)
        # 之后起扫,看不到它的积压。
        assert store.graph_drain_backlog(db, notebook_id, 0, 7) is None

    with runtime.database.write() as db:
        assert store.drain_notebook_graph_rows_page(
            db, notebook_id, 6, 3
        )["knowledge_objects"] == 3
    with runtime.database.write() as db:
        assert store.drain_notebook_graph_rows_page(
            db, notebook_id, 6, 3
        )["knowledge_objects"] == 3
    with runtime.database.connect() as db:
        assert store.graph_drain_backlog(db, notebook_id, 0) == ("knowledge_objects", 6)
    with runtime.database.write() as db:
        assert store.drain_notebook_graph_rows_page(
            db, notebook_id, 6, 3
        )["knowledge_objects"] == 1
    with runtime.database.connect() as db:
        assert store.graph_drain_backlog(db, notebook_id, 0) is None
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["c"] == 0

    with pytest.raises(ValueError, match="unknown graph drain step"):
        with runtime.database.write() as db:
            store.drain_notebook_graph_rows_page(db, notebook_id, 99, 1)


@pytest.mark.postgres_integration
def test_delete_notebook_kg_drains_then_resets_on_postgres(
    postgres_repository, monkeypatch,
):
    """端到端等价:超阈值图走排水后,终局效果与未排水逐字节同契约——用户文档
    对象清空、memory 投影保留、counts 报全量、epoch+1 seq 0。"""
    monkeypatch.setattr(
        postgres_repository._runtime.knowledge_lifecycle,
        "_graph_drain_budget_rows", 3,
    )
    notebook_id = postgres_repository.create_notebook(
        NotebookCreate(name="drain-e2e")
    ).id
    _seed_graph(postgres_repository, notebook_id, doc_objects=10, memory_objects=2)
    runtime = postgres_repository._runtime

    pages = []
    store = runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, step, limit):
        deleted = original(db, nb, step, limit)
        pages.append((step, deleted))
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    counts = postgres_repository.delete_notebook_kg(notebook_id)

    assert pages, "超阈值的图必须走排水"
    assert all(d.get("knowledge_objects", 0) <= 3 for _s, d in pages)
    assert counts["knowledge_objects"] == 10
    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s "
            "AND source_id='src-doc'", (notebook_id,),
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s "
            "AND source_id='src-mem'", (notebook_id,),
        ).fetchone()["c"] == 2
        state = runtime.unified_kg.state_row(db, notebook_id)
    assert state["kg_mutation_seq"] == 0
    assert state["kg_reset_epoch"] >= 1


# ---------------------------------------------------------------------------
# K1 (debt-fix 2026-10-12): conflict candidates, merge-review row, derived
# tables kept (ruling 11)
# ---------------------------------------------------------------------------

_DERIVED = (
    "canonical_relations", "communities", "community_members",
    "concept_comentions", "mention_edges",
)


def _seed_derived(postgres_repository, notebook_id, n, *, tag):
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO canonical_relations (notebook_id,canonical_src,edge_type,"
                "canonical_tgt,updated_at) VALUES (%s,%s,'supports',%s,%s)",
                (notebook_id, f"K-{tag}{i:03d}", f"K-{tag}t{i:03d}", now),
            )
            db.execute(
                "INSERT INTO mention_edges (notebook_id,claim_object_id,"
                "concept_canonical_id) VALUES (%s,%s,%s)",
                (notebook_id, f"ko-{tag}c{i:03d}", f"K-{tag}{i:03d}"),
            )
            db.execute(
                "INSERT INTO concept_comentions (notebook_id,canonical_a,canonical_b) "
                "VALUES (%s,%s,%s)",
                (notebook_id, f"K-{tag}{i:03d}", f"K-{tag}z{i:03d}"),
            )
            db.execute(
                "INSERT INTO communities (id,notebook_id,level,member_ids,size,"
                "created_at,generation) VALUES (%s,%s,%s,%s,3,%s,1)",
                (f"cm-{tag}{i:03d}", notebook_id, i % 2, jsonb([]), now),
            )
            db.execute(
                "INSERT INTO community_members (canonical_id,notebook_id,level,"
                "community_id,generation) VALUES (%s,%s,0,%s,1)",
                (f"K-{tag}{i:03d}", notebook_id, f"cm-{tag}{i // 3:03d}"),
            )


def _derived_counts(runtime, notebook_id):
    with runtime.database.connect() as db:
        return {
            t: db.execute(f"SELECT COUNT(*) AS c FROM {t} WHERE notebook_id=%s",
                          (notebook_id,)).fetchone()["c"]
            for t in _DERIVED
        }


@pytest.mark.postgres_integration
def test_delete_notebook_kg_conflicts_review_and_kept_tables_on_postgres(
    postgres_repository, monkeypatch,
):
    """K1 PG 孪生:超阈值的图走排水后,失效冲突候选(含一侧仍在、一侧已删的
    half 候选)与非运行的预审行清掉,两侧仍在的 node / edge 候选、运行中的
    预审行、重建检查点保留;五张派生表(裁决 11)一行不动,排水页里也不出现。
    变异钉:PG 冲突谓词 OR→AND → half 候选留下,报红。"""
    runtime = postgres_repository._runtime
    service = runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    a = postgres_repository.create_notebook(NotebookCreate(name="k1-a")).id
    b = postgres_repository.create_notebook(NotebookCreate(name="k1-b")).id
    _seed_graph(postgres_repository, a, doc_objects=8, memory_objects=2)
    _seed_derived(postgres_repository, a, 10, tag="a")
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for i in range(6):  # dead refs, over budget: drained in pages
            db.execute(
                "INSERT INTO kg_conflict_candidates (id,notebook_id,kind,left_ref,"
                "right_ref,status,created_at,updated_at) "
                "VALUES (%s,%s,'node',%s,%s,%s,%s,%s)",
                (f"kcc-dead-{i}", a, f"ko-doc-{i}", f"ko-doc-{i + 1}",
                 ("applied", "rejected", "pending")[i % 3], now, now),
            )
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES ('rel-mem-0',%s,'src-mem','ko-mem-0','ko-mem-1','supports',%s,%s),"
            "('rel-mem-1',%s,'src-mem','ko-mem-1','ko-mem-0','contradicts',%s,%s)",
            (a, jsonb([]), now, a, jsonb([]), now),
        )
        # 与 SQLite 孪生同一组格(质量评审 P2-2):一侧保留、一侧已删的 half
        # 候选要删(OR 谓词),两侧都在的 node / edge 候选要留。
        for cid, kind, left, right, status in (
            ("kcc-half", "node", "ko-mem-0", "ko-doc-7", "applied"),
            ("kcc-keep", "node", "ko-mem-0", "ko-mem-1", "pending"),
            ("kcc-keep-edge", "edge", "rel-mem-0", "rel-mem-1", "pending"),
        ):
            db.execute(
                "INSERT INTO kg_conflict_candidates (id,notebook_id,kind,left_ref,"
                "right_ref,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (cid, a, kind, left, right, status, now, now),
            )
        db.execute(
            "INSERT INTO merge_review_jobs (notebook_id,status,total,done,updated_at,"
            "error) VALUES (%s,'failed',5,1,%s,'x'),(%s,'running',5,1,%s,'')",
            (a, now, b, now),
        )
        db.execute(
            "INSERT INTO kg_rebuild_checkpoint (notebook_id,input_version,stage,"
            "item_key,payload,created_at) VALUES (%s,'v1','merge_review','k',%s,%s)",
            (a, jsonb({}), now),
        )

    pages = []
    store = runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, step, limit):
        deleted = original(db, nb, step, limit)
        pages.append(dict(deleted))
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    counts = postgres_repository.delete_notebook_kg(a)

    assert any("kg_conflict_candidates" in d for d in pages), "失效候选超阈值须走排水"
    assert all(d.get("kg_conflict_candidates", 0) <= 3 for d in pages)
    assert counts["kg_conflict_candidates"] == 7
    assert not any(t in d for d in pages for t in _DERIVED)
    assert _derived_counts(runtime, a) == dict.fromkeys(_DERIVED, 10)
    with runtime.database.connect() as db:
        conflicts = {r["id"] for r in db.execute(
            "SELECT id FROM kg_conflict_candidates WHERE notebook_id=%s", (a,)
        ).fetchall()}
        assert conflicts == {"kcc-keep", "kcc-keep-edge"}
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_rebuild_checkpoint WHERE notebook_id=%s",
            (a,)).fetchone()["c"] == 1
    governance = runtime.knowledge_governance
    assert governance.merge_review_job_status(a)["status"] == "idle"
    postgres_repository.delete_notebook_kg(b)
    assert governance.merge_review_job_status(b) == {
        "status": "running", "total": 5, "done": 1, "error": "",
    }, "运行中的预审行必须挺过删除"


@pytest.mark.postgres_integration
def test_conflict_write_epoch_gate_on_postgres(postgres_repository):
    """K1:PG 上 ``write_conflict_candidate(reset_epoch=…)`` 的条件插入(含 FOR
    SHARE 子查询)真能执行:纪元一致落行、不一致返回 None 不写;删除后纪元
    前进,按删除前纪元写入被拒。"""
    runtime = postgres_repository._runtime
    governance = runtime.knowledge_governance
    nb = postgres_repository.create_notebook(NotebookCreate(name="epoch-pg")).id
    with runtime.database.connect() as db:
        epoch = runtime.unified_kg.graph_seq_row(db, nb)[3]
    assert governance.write_conflict_candidate(
        nb, "node", "a", "b", reset_epoch=epoch) is not None
    postgres_repository.delete_notebook_kg(nb)
    assert governance.write_conflict_candidate(
        nb, "node", "a", "b", reset_epoch=epoch) is None
    with runtime.database.connect() as db:
        fresh = runtime.unified_kg.graph_seq_row(db, nb)[3]
        assert fresh == epoch + 1
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_conflict_candidates WHERE notebook_id=%s",
            (nb,)).fetchone()["c"] == 0
    assert governance.write_conflict_candidate(
        nb, "node", "a", "b", reset_epoch=fresh) is not None


@pytest.mark.postgres_integration
def test_drain_only_steps_stay_out_of_the_post_commit_check_on_postgres(
    postgres_repository, monkeypatch,
):
    """K1 PG 孪生(「新步骤参与复查」变异钉):终局提交与阈值 0 复查之间落地
    的失效冲突候选 / 已结束的预审行不得多烧一轮重置(终局恰一轮,行留到下次
    删除)。"""
    runtime = postgres_repository._runtime
    store = runtime.knowledge
    original_backlog = store.graph_drain_backlog
    original_final = store.delete_notebook_graph_rows

    def _run(name, inject):
        nb = postgres_repository.create_notebook(NotebookCreate(name=name)).id
        injected, finals = {"done": False}, []

        def _backlog(db, notebook_id, threshold, start=0, **kw):
            if threshold == 0 and not injected["done"]:
                injected["done"] = True
                now = normalize_timestamp(runtime.seams.now())
                with runtime.database.write() as w:
                    inject(w, notebook_id, now)
            return original_backlog(db, notebook_id, threshold, start, **kw)

        def _final(db, notebook_id, now):
            finals.append(notebook_id)
            return original_final(db, notebook_id, now)

        monkeypatch.setattr(store, "graph_drain_backlog", _backlog)
        monkeypatch.setattr(store, "delete_notebook_graph_rows", _final)
        postgres_repository.delete_notebook_kg(nb)
        assert injected["done"]
        return nb, finals

    def _late_conflict(w, nb, now):
        w.execute(
            "INSERT INTO kg_conflict_candidates (id,notebook_id,kind,left_ref,"
            "right_ref,status,created_at,updated_at) "
            "VALUES ('kcc-late-pg',%s,'node','ko-gone-a','ko-gone-b','pending',%s,%s)",
            (nb, now, now),
        )
        w.execute(
            "INSERT INTO merge_review_jobs (notebook_id,status,total,done,updated_at,"
            "error) VALUES (%s,'done',3,3,%s,'')", (nb, now),
        )

    nb, finals = _run("settle-exempt-pg", _late_conflict)
    assert len(finals) == 1, f"冲突候选/预审行不得参与提交后复查:{len(finals)}"
    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_conflict_candidates WHERE notebook_id=%s",
            (nb,)).fetchone()["c"] == 0, "复查之后的清扫须带走失效候选"
    assert runtime.knowledge_governance.merge_review_job_status(nb)["status"] == "idle"


def _raw_connection(postgres_repository):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        postgres_repository._runtime.database.settings.database_url,
        row_factory=dict_row,
    )


@pytest.mark.postgres_integration
def test_conflicts_committed_during_the_final_reset_are_swept_on_postgres(
    postgres_repository, monkeypatch,
):
    """K1(质量评审 P3-1,race 场景 A/B):终局事务的 REPEATABLE READ 快照之后、
    纪元 UPSERT 之前提交的失效候选,终局 DELETE 看不见、也没有行锁可等;
    它由提交后的一次清扫带走,不多烧重置轮。变异钉:拿掉清扫 → 删除返回
    后仍留一条指向已删对象的候选,报红。"""
    runtime = postgres_repository._runtime
    store = runtime.knowledge
    nb = postgres_repository.create_notebook(NotebookCreate(name="race-ab")).id
    _seed_graph(postgres_repository, nb, doc_objects=2)
    original_final = store.delete_notebook_graph_rows
    finals = []

    def _final_then_commit_a_candidate(db, notebook_id, now):
        counts = original_final(db, notebook_id, now)
        # 终局语句都跑完、事务还没提交:另一个连接提交一条失效候选
        # (相当于条件插入在 UPSERT 之前就拿到了旧纪元)。
        with _raw_connection(postgres_repository) as other:
            other.execute(
                "INSERT INTO kg_conflict_candidates (id,notebook_id,kind,left_ref,"
                "right_ref,status,created_at,updated_at) VALUES "
                "('kcc-race',%s,'node','ko-doc-0','ko-doc-1','pending',now(),now())",
                (notebook_id,),
            )
            other.commit()
        finals.append(notebook_id)
        return counts

    monkeypatch.setattr(store, "delete_notebook_graph_rows",
                        _final_then_commit_a_candidate)
    counts = postgres_repository.delete_notebook_kg(nb)

    assert len(finals) == 1, f"清扫不得消耗重置轮:{finals}"
    assert counts.get("kg_conflict_candidates", 0) == 1
    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_conflict_candidates WHERE notebook_id=%s",
            (nb,)).fetchone()["c"] == 0


@pytest.mark.postgres_integration
def test_conflict_write_waits_for_an_in_flight_reset_on_postgres(
    postgres_repository,
):
    """K1(质量评审 P2-1):删除的纪元 UPSERT 已执行、尚未提交时,按旧纪元的
    条件插入在 ``FOR SHARE`` 上等待;删除提交后它读到新纪元,返回 None、
    不落行。变异钉:去掉 ``FOR SHARE`` → 插入不等待、立刻按旧纪元落一行,
    报红。"""
    import threading
    import time

    runtime = postgres_repository._runtime
    governance = runtime.knowledge_governance
    nb = postgres_repository.create_notebook(NotebookCreate(name="for-share")).id
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, kg_reset_epoch, updated_at) "
            "VALUES (%s, 1, now()) ON CONFLICT (notebook_id) DO UPDATE SET "
            "kg_reset_epoch = 1", (nb,),
        )
    with _raw_connection(postgres_repository) as deleter:
        deleter.execute(
            "UPDATE unified_kg_state SET kg_reset_epoch = kg_reset_epoch + 1 "
            "WHERE notebook_id = %s", (nb,),
        )
        result = {}

        def _write():
            result["id"] = governance.write_conflict_candidate(
                nb, "node", "ko-old-a", "ko-old-b", reset_epoch=1)

        writer = threading.Thread(target=_write)
        writer.start()
        writer.join(0.3)
        assert writer.is_alive(), "条件插入必须等在途的纪元推进提交"
        deleter.commit()
    writer.join(5)
    assert not writer.is_alive()
    assert result == {"id": None}
    with runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM kg_conflict_candidates WHERE notebook_id=%s",
            (nb,)).fetchone()["c"] == 0


def _reextract_pg(runtime, notebook_id, canonical_id, name, member, source_id):
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (%s,%s,'concept',"
            "'approved',%s,%s,%s,%s,%s)",
            (member, notebook_id, jsonb({}), jsonb([]), source_id, now, now),
        )
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at) VALUES (%s,%s,%s,%s,%s,'concept',%s)",
            (f"cc-{member}", notebook_id, canonical_id, member, name, now),
        )


def _derived_reads_pg(postgres_repository, notebook_id):
    from app.services.communities import CommunityQueryService

    runtime = postgres_repository._runtime
    uk = runtime.unified_kg
    service = CommunityQueryService(
        notebooks=runtime.notebook_store, unified_kg=uk,
        event_log=runtime.event_log, sibling_min_bridge=1,
    )
    with runtime.database.connect() as db:
        direct = {
            "weak_support": [dict(r) for r in uk.weak_support_relation_rows(
                db, notebook_id, ["K-a000", "K-a001"], 10, 10)],
            "relation_support": [dict(r) for r in uk.relation_support_rows(
                db, notebook_id, [("K-a000", "supports", "K-at000")])],
            "edge_support": [tuple(r.values()) for r in uk.edge_support_rows(
                db, notebook_id).fetchall()],
            "mention": [dict(r) for r in uk.mention_rows(db, notebook_id)],
            "communities": uk.communities_count(db, notebook_id, 0),
            "community_members": uk.community_member_ids(db, notebook_id, 0),
        }
    return {
        **direct,
        "sibling_peers": service.sibling_peers(notebook_id, "foo"),
        "top_community": uk.top_community_for(notebook_id, "K-a000"),
        "member_peers": [dict(r) for r in uk.community_member_peers(
            notebook_id, "cm-a000", "", 10)],
        "comention_peers": uk.comention_peers(notebook_id, "K-a000", 1, 10),
    }


@pytest.mark.postgres_integration
def test_derived_layers_stay_unread_after_delete_and_reextraction_on_postgres(
    postgres_repository,
):
    """K1 读侧闸 PG 孪生(规格评审 P2-1 / P3-1):删除 → 重抽同名(同一个规范
    id 以代 0 入簇)→ 每个派生层读者的结果与「从没建过派生表」的库逐值相同,
    包括迁移前遗留的代 0 社区;随后的重建重新打开派生层。"""
    runtime = postgres_repository._runtime
    had = postgres_repository.create_notebook(NotebookCreate(name="had")).id
    never = postgres_repository.create_notebook(NotebookCreate(name="never")).id
    _seed_graph(postgres_repository, had, doc_objects=2)
    _seed_derived(postgres_repository, had, 3, tag="a")
    with runtime.database.write() as db:  # legacy generation-0 communities
        db.execute("UPDATE communities SET generation=0 WHERE notebook_id=%s", (had,))
        db.execute(
            "UPDATE community_members SET generation=0 WHERE notebook_id=%s", (had,))
    postgres_repository.delete_notebook_kg(had)
    postgres_repository.delete_notebook_kg(never)
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,created_at,updated_at,"
            "doc_type) VALUES ('src-never',%s,'s','markdown','extracted','parsed','',"
            "'',0,'h',%s,now(),now(),'')", (never, ""),
        )
    for nb, src, tag in ((had, "src-doc", "h"), (never, "src-never", "n")):
        _reextract_pg(runtime, nb, "K-a000", "foo", f"ko-re-{tag}-1", src)
        _reextract_pg(runtime, nb, "K-az000", "bar", f"ko-re-{tag}-2", src)

    assert _derived_counts(runtime, had) == dict.fromkeys(_DERIVED, 3)
    assert _derived_reads_pg(postgres_repository, had) == _derived_reads_pg(
        postgres_repository, never)

    runtime.knowledge_lifecycle.rebuild_unified_kg(had, force=True)
    _seed_derived(postgres_repository, had, 1, tag="post")
    with runtime.database.connect() as db:
        assert runtime.unified_kg.relation_support_rows(
            db, had, [("K-post000", "supports", "K-postt000")]), "重建后派生层须恢复可读"
        assert runtime.unified_kg.mention_rows(db, had)
