"""批 3·W2 PR-2 §5:代际引物在真 PG 上的状态机 pin。

钉四件事:取号微事务的三种结局(抢到/被占拒绝/TTL 抢占);释放 CAS 只清
自己的认领;翻转双 CAS 在「published 被动过」「在飞认领被抢占」两个方向都
作废;残代回收页只删 keep 之外的代。这些是设计 v3 §1.2/D-W2-5/D-W2-7 的
并发正确性根基——任何一条 CAS 谓词被削弱,读者就可能看到半态。
"""
from __future__ import annotations

import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_derived_generation_primitives"),
]

_NOW = normalize_timestamp("2026-01-01T00:00:00+00:00")


def _seed_notebook(postgres_database, notebook_id: str) -> None:
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,'g',%s,%s)",
            (notebook_id, _NOW, _NOW),
        )


def test_claim_lifecycle_and_ttl_preemption(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 51
    _seed_notebook(postgres_database, "nb-claim")
    with postgres_database.write() as db:
        first = UnifiedKgStore.claim_derived_generation(
            db, "nb-claim", ttl_seconds=3600
        )
        assert first is not None
        assert first["generation"] == 1
        assert first["cluster_generation"] == 0
        assert first["catchup_from"] is None
        # 被占且未过 TTL:拒绝,不烧号。
        assert (
            UnifiedKgStore.claim_derived_generation(db, "nb-claim", ttl_seconds=3600)
            is None
        )
        assert UnifiedKgStore.derived_claim_still_held(db, "nb-claim", 1)
        # 模拟崩溃陈旧认领(PG 的 now() 在事务内冻结,同事务里无法靠
        # TTL=0 触发 `claimed_at < now()`;生产抢占本就发生在数小时后的
        # 另一事务):回拨 claimed_at 后按正常 TTL 抢占,counter 单调 +1
        # (绝不复用被抢占者的号)。
        db.execute(
            "UPDATE unified_kg_state SET "
            "derived_building_claimed_at = now() - interval '2 hours' "
            "WHERE notebook_id='nb-claim'"
        )
        preempt = UnifiedKgStore.claim_derived_generation(
            db, "nb-claim", ttl_seconds=3600
        )
        assert preempt is not None and preempt["generation"] == 2
        # 被抢占者复读认领:早停信号。
        assert not UnifiedKgStore.derived_claim_still_held(db, "nb-claim", 1)
        # 被抢占者迟到的 finally 释放:CAS 不匹配,no-op,不误清抢占者。
        UnifiedKgStore.release_derived_claim(db, "nb-claim", 1)
        assert UnifiedKgStore.derived_claim_still_held(db, "nb-claim", 2)
        # 自己的释放正常生效。
        UnifiedKgStore.release_derived_claim(db, "nb-claim", 2)
        assert not UnifiedKgStore.derived_claim_still_held(db, "nb-claim", 2)


def test_flip_double_cas_rejects_both_stale_directions(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 51
    _seed_notebook(postgres_database, "nb-flip")
    with postgres_database.write() as db:
        claim = UnifiedKgStore.claim_derived_generation(
            db, "nb-flip", ttl_seconds=3600
        )
        assert claim is not None
        # 方向一:在飞认领已被抢占(building≠自己)→ 作废。
        assert not UnifiedKgStore.flip_cluster_generation(
            db, "nb-flip", published_from=0, generation=claim["generation"] + 7,
            catchup_from_ts=claim["ts"], now=_NOW,
        )
        # 正常翻转:指针前进、认领清零、催收欠账落库、cseq bump 同语句。
        before = db.execute(
            "SELECT cluster_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id='nb-flip'"
        ).fetchone()["cluster_mutation_seq"]
        assert UnifiedKgStore.flip_cluster_generation(
            db, "nb-flip", published_from=0, generation=claim["generation"],
            catchup_from_ts=claim["ts"], now=_NOW,
        )
        state = db.execute(
            "SELECT cluster_generation, cluster_mutation_seq, "
            "derived_building_generation, derived_building_claimed_at, "
            "derived_catchup_from FROM unified_kg_state "
            "WHERE notebook_id='nb-flip'"
        ).fetchone()
        assert state["cluster_generation"] == claim["generation"]
        assert state["cluster_mutation_seq"] == before + 1
        assert state["derived_building_generation"] == 0
        assert state["derived_building_claimed_at"] is None
        assert state["derived_catchup_from"] is not None
        # 方向二:published 已被动过(delete_notebook_kg 重置/别人翻过)
        # → 拿着旧 published 快照的翻转作废。
        stale = UnifiedKgStore.claim_derived_generation(
            db, "nb-flip", ttl_seconds=3600
        )
        assert stale is not None
        assert not UnifiedKgStore.flip_cluster_generation(
            db, "nb-flip", published_from=0, generation=stale["generation"],
            catchup_from_ts=stale["ts"], now=_NOW,
        )
        UnifiedKgStore.release_derived_claim(db, "nb-flip", stale["generation"])
        # 催收标记 CAS:别人的 ts 清不动,自己的 ts 清得掉。
        UnifiedKgStore.clear_catchup_marker(
            db, "nb-flip", "2001-01-01T00:00:00+00:00"
        )
        assert db.execute(
            "SELECT derived_catchup_from FROM unified_kg_state "
            "WHERE notebook_id='nb-flip'"
        ).fetchone()["derived_catchup_from"] is not None
        UnifiedKgStore.clear_catchup_marker(db, "nb-flip", claim["ts"])
        assert db.execute(
            "SELECT derived_catchup_from FROM unified_kg_state "
            "WHERE notebook_id='nb-flip'"
        ).fetchone()["derived_catchup_from"] is None


def test_community_flip_shares_the_same_double_cas(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 51
    _seed_notebook(postgres_database, "nb-cflip")
    with postgres_database.write() as db:
        claim = UnifiedKgStore.claim_derived_generation(
            db, "nb-cflip", ttl_seconds=3600
        )
        assert claim is not None
        assert not UnifiedKgStore.flip_community_generation(
            db, "nb-cflip", published_from=99, generation=claim["generation"],
            now=_NOW,
        )
        assert UnifiedKgStore.flip_community_generation(
            db, "nb-cflip", published_from=0, generation=claim["generation"],
            now=_NOW,
        )
        assert db.execute(
            "SELECT community_generation FROM unified_kg_state "
            "WHERE notebook_id='nb-cflip'"
        ).fetchone()["community_generation"] == claim["generation"]
        UnifiedKgStore.release_derived_claim(db, "nb-cflip", claim["generation"])


def test_reap_page_deletes_only_generations_outside_keep(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 51
    _seed_notebook(postgres_database, "nb-reap")
    with postgres_database.write() as db:
        for gen in (0, 1, 2, 3):
            db.execute(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                "object_type,created_at,generation) "
                "SELECT 'cc-'||%s||'-'||g, %s, 'can', 'ko-'||g, 'N', 'concept', "
                "%s, %s FROM generate_series(0, 4) g",
                (gen, "nb-reap", _NOW, gen),
            )
        deleted = UnifiedKgStore.reap_derived_generations_page(
            db, "nb-reap", "concept_clusters", (2, 3), 100
        )
        assert deleted == 10
        left = db.execute(
            "SELECT DISTINCT generation FROM concept_clusters "
            "WHERE notebook_id='nb-reap' ORDER BY generation"
        ).fetchall()
        assert [r["generation"] for r in left] == [2, 3]
        # 表名白名单:非派生表响亮拒绝。
        with pytest.raises(AssertionError):
            UnifiedKgStore.reap_derived_generations_page(
                db, "nb-reap", "knowledge_objects", (0,), 10
            )
