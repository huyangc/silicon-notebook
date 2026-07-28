"""KG 质量分析只读聚合查询层(T1)。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md`「T1 只读聚合查询层」。
测的是 UnifiedKgStore 的四个只读聚合(经 facade 的一跳委托进去),重点钉五件事:

1. **口径**与产品一致 —— 对象只算 USABLE_STATUSES;边只算 review_status != 'rejected'
   且**两端都是本 notebook 的可用对象**;被排除的量单独报,不凭空消失。
2. **有界** —— 每个上限都会 clamp,超限一律带 truncated 标志,绝不静默截断;
   「恰好等于上限」这一档单独钉(`>` 写成 `>=` 会在这里谎报截断)。
3. **分桶/分组发生在 SQL 里** —— 载荷是定长定序的分组直方图,缺席的补 0;
   反方向(SQL 冒出契约外的桶名)硬失败,不静默吞掉。
4. **真的只读** —— 用语句级 trace + 相关表的行数/指纹快照两道观测量钉,而不是看一个
   只由 mark_dirty 推进、直写根本不碰的计数器。
5. **一致快照** —— `community_overview` 的多条语句必须看同一份库,并发 rebuild 一劈
   不得出现「有 N 个板块,一个都没有」。

PostgreSQL 侧的同名方法与这里**语义等价**(同口径、同分组分桶、同载荷),但 SQL 形态
允许分岔(`community_overview` 在 PG 上是一条窗口函数查询)。载荷装配复用
`app.repositories.kg_analysis_payloads` 的中性纯函数;两侧的行为对照在
tests/postgres/ 的 conformance lane 里跑(需要真 PG,不在本离线门内)。
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from app.core.config import Settings
from app.services.knowledge_contracts import (
    CLUSTER_OBJECT_TYPE_GROUPS,
    CLUSTER_OBJECT_TYPES,
    CLUSTER_SIZE_BUCKETS,
    COMMUNITY_OVERVIEW_MAX,
    COMMUNITY_TOP_MEMBERS_MAX,
    EMPTY_CLUSTER_BUCKET,
    LARGEST_CLUSTERS_MAX,
    OTHER_OBJECT_TYPE_GROUP,
    RELATION_EXCLUSION_BUCKETS,
    RELATION_PROVENANCE_BUCKETS,
)
from app.services.sqlite_repository import SQLiteRepository

NB = "nb-analysis"
OTHER_NB = "nb-other"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    with repository._write() as db:
        for nb in (NB, OTHER_NB):
            db.execute(
                "INSERT INTO notebooks (id,name,created_at,updated_at) "
                "VALUES (?,?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
                (nb, nb),
            )
    return repository


def _object(db, oid: str, *, notebook: str = NB, status: str = "approved",
            object_type: str = "concept") -> None:
    """真 schema 的默认值:status 默认 'approved'。这里显式传,便于钉口径。"""
    db.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'[]','2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (oid, notebook, object_type, status, json.dumps({"name": oid})),
    )


def _cluster(db, canonical_id: str, member_object_id: str, *, name: str = "",
             object_type: str = "concept") -> None:
    db.execute(
        "INSERT INTO concept_clusters "
        "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
        "VALUES (?,?,?,?,?,?,'2026-01-01T00:00:00')",
        (f"cc-{canonical_id}-{member_object_id}", NB, canonical_id,
         member_object_id, canonical_id if name == "" else name, object_type),
    )


def _relation(
    db,
    rid: str,
    src: str,
    tgt: str,
    *,
    evidence: str = "[]",
    review_status: str = "pending",
    notebook: str = NB,
) -> None:
    """真 schema 的默认值:review_status 默认 'pending'、evidence 默认 '[]'。"""
    db.execute(
        "INSERT INTO knowledge_relations "
        "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,"
        " review_status,created_at) "
        "VALUES (?,?,?,?,'about',?,?,'2026-01-01T00:00:00')",
        (rid, notebook, src, tgt, evidence, review_status),
    )


def _bucket(payload, name: str) -> dict:
    return next(b for b in payload["buckets"] if b["bucket"] == name)


def _group(payload, object_type: str) -> dict:
    return next(
        g for g in payload["by_object_type"] if g["object_type"] == object_type
    )


# ---------------------------------------------------------------- 簇大小分布

def test_cluster_size_histogram_buckets_are_fixed_length_and_ordered(repo):
    """库里只出现两档,载荷仍然给出全部 7 档、按契约顺序,缺席的补 0。"""
    with repo._write() as db:
        for i in range(3):
            _object(db, f"o-solo-{i}")
            _cluster(db, f"K-solo-{i}", f"o-solo-{i}")
        for i in range(2):
            _object(db, f"o-pair-{i}")
            _cluster(db, "K-pair", f"o-pair-{i}")

    payload = repo.kg_cluster_size_histogram(NB)

    assert [b["bucket"] for b in payload["buckets"]] == list(CLUSTER_SIZE_BUCKETS)
    assert _bucket(payload, "1") == {"bucket": "1", "clusters": 3, "members": 3}
    assert _bucket(payload, "2") == {"bucket": "2", "clusters": 1, "members": 2}
    assert _bucket(payload, "51+") == {"bucket": "51+", "clusters": 0, "members": 0}
    assert payload["clusters"] == 4
    assert payload["member_rows"] == 5
    assert payload["excluded_member_rows"] == 0
    assert payload["empty_clusters"] == 0


@pytest.mark.parametrize(
    "size,bucket",
    [(1, "1"), (2, "2"), (3, "3"), (4, "4-5"), (5, "4-5"),
     (6, "6-10"), (10, "6-10"), (11, "11-50"), (50, "11-50"), (51, "51+")],
)
def test_cluster_size_histogram_bucket_edges(repo, size, bucket):
    """每个分桶边界都钉一次:4-5 / 6-10 / 11-50 的上下沿与 51+ 的起点。
    顶档叫 51+ 正因为 50 不属于它(语义是 > 50),这里 (50,'11-50') 与 (51,'51+')
    两行就是那个命名的依据。"""
    with repo._write() as db:
        for i in range(size):
            _object(db, f"o-{i}")
            _cluster(db, "K", f"o-{i}")

    payload = repo.kg_cluster_size_histogram(NB)

    assert _bucket(payload, bucket)["clusters"] == 1
    assert _bucket(payload, bucket)["members"] == size
    assert payload["member_rows"] == size
    assert payload["clusters"] == 1


def test_cluster_size_histogram_counts_only_usable_objects(repo):
    """deprecated 成员不计入簇大小,但作为 excluded_member_rows 单独报出——
    口径要求被排除的量不凭空消失。"""
    with repo._write() as db:
        _object(db, "o-ok-1")
        _object(db, "o-ok-2")
        _object(db, "o-dead", status="deprecated")
        for oid in ("o-ok-1", "o-ok-2", "o-dead"):
            _cluster(db, "K", oid)
        # 成员对象根本不存在(损坏数据)同样算排除,不能被当成有效成员。
        _cluster(db, "K", "o-missing")
        # 成员对象属于别的 notebook —— 不是本库的可用对象。
        _object(db, "o-foreign", notebook=OTHER_NB)
        _cluster(db, "K", "o-foreign")

    payload = repo.kg_cluster_size_histogram(NB)

    assert _bucket(payload, "2")["clusters"] == 1
    assert payload["member_rows"] == 2
    assert payload["clusters"] == 1
    assert payload["excluded_member_rows"] == 3


def test_cluster_size_histogram_reports_fully_excluded_clusters_separately(repo):
    """成员全被排除的簇不能混进 '1' 档冒充单元素簇,要单独计 empty_clusters。"""
    with repo._write() as db:
        _object(db, "o-dead-1", status="deprecated")
        _object(db, "o-dead-2", status="deprecated")
        _cluster(db, "K-dead", "o-dead-1")
        _cluster(db, "K-dead", "o-dead-2")
        _object(db, "o-live")
        _cluster(db, "K-live", "o-live")

    payload = repo.kg_cluster_size_histogram(NB)

    assert payload["empty_clusters"] == 1
    assert payload["empty_cluster_member_rows"] == 2
    assert payload["excluded_member_rows"] == 2
    assert _bucket(payload, "1")["clusters"] == 1
    assert payload["clusters"] == 1
    assert payload["member_rows"] == 1


def test_cluster_size_histogram_conflict_status_is_usable(repo):
    """'conflict' 在 USABLE_STATUSES 里(会进检索、只是被单独标注),必须计入。"""
    with repo._write() as db:
        _object(db, "o-conflict", status="conflict")
        _cluster(db, "K", "o-conflict")

    assert repo.kg_cluster_size_histogram(NB)["member_rows"] == 1


def test_cluster_size_histogram_is_scoped_to_the_notebook(repo):
    with repo._write() as db:
        _object(db, "o-1")
        _cluster(db, "K", "o-1")

    assert repo.kg_cluster_size_histogram(OTHER_NB)["member_rows"] == 0
    assert repo.kg_cluster_size_histogram(OTHER_NB)["clusters"] == 0
    assert [b["bucket"] for b in repo.kg_cluster_size_histogram(OTHER_NB)["buckets"]] == \
        list(CLUSTER_SIZE_BUCKETS)


# --------------------------------------------------- 簇大小分布:按类型分组(D1)

def test_cluster_size_histogram_groups_are_fixed_length_and_ordered(repo):
    """空库也给出全部 5 个分组、按契约顺序,每组自带定长的 7 档直方图。"""
    payload = repo.kg_cluster_size_histogram(NB)

    assert [g["object_type"] for g in payload["by_object_type"]] == \
        list(CLUSTER_OBJECT_TYPE_GROUPS)
    for group in payload["by_object_type"]:
        assert [b["bucket"] for b in group["buckets"]] == list(CLUSTER_SIZE_BUCKETS)
        assert group["member_rows"] == 0
        assert group["clusters"] == 0
        assert group["object_types"] == 0


def test_cluster_size_histogram_reports_each_object_type_separately(repo):
    """concept / claim / formula / procedure 各报各的:claim 那种「天然全是单元素簇」
    的分布不能把 concept 的收敛率稀释掉。顶层仍给全类型合计。"""
    with repo._write() as db:
        # concept:一个 3 成员簇(真正收敛了)
        for i in range(3):
            _object(db, f"o-c-{i}")
            _cluster(db, "K-c", f"o-c-{i}")
        # claim:两个单元素簇
        for i in range(2):
            _object(db, f"o-l-{i}", object_type="claim")
            _cluster(db, f"KL-{i}", f"o-l-{i}", object_type="claim")
        # formula / procedure 各一个单元素簇
        _object(db, "o-f", object_type="formula")
        _cluster(db, "KF-1", "o-f", object_type="formula")
        _object(db, "o-p", object_type="procedure")
        _cluster(db, "KP-1", "o-p", object_type="procedure")

    payload = repo.kg_cluster_size_histogram(NB)

    concept = _group(payload, "concept")
    assert concept["member_rows"] == 3
    assert concept["clusters"] == 1
    assert concept["object_types"] == 1
    assert [b["clusters"] for b in concept["buckets"]] == [0, 0, 1, 0, 0, 0, 0]

    claim = _group(payload, "claim")
    assert claim["member_rows"] == 2
    assert claim["clusters"] == 2
    assert [b["clusters"] for b in claim["buckets"]] == [2, 0, 0, 0, 0, 0, 0]

    assert _group(payload, "formula")["clusters"] == 1
    assert _group(payload, "procedure")["clusters"] == 1
    assert _group(payload, OTHER_OBJECT_TYPE_GROUP)["clusters"] == 0

    # 顶层 = 全类型合计
    assert payload["member_rows"] == 7
    assert payload["clusters"] == 5
    assert _bucket(payload, "1")["clusters"] == 4
    assert _bucket(payload, "3")["clusters"] == 1


def test_cluster_size_histogram_folds_unknown_object_types_without_leaking_names(repo):
    """knowhow 投影会用**用户的列名**建自定义类型。这类行不得静默丢弃(顶层合计必须
    含它们),也不得把类型名塞进载荷——只报计数与「有几个不同类型」。"""
    with repo._write() as db:
        _object(db, "o-c")
        _cluster(db, "K-c", "o-c")
        for i, custom in enumerate(("根因分析动作", "周几")):
            _object(db, f"o-x-{i}", object_type=custom)
            _cluster(db, f"KX-{i}", f"o-x-{i}", object_type=custom)

    payload = repo.kg_cluster_size_histogram(NB)

    other = _group(payload, OTHER_OBJECT_TYPE_GROUP)
    assert other["clusters"] == 2
    assert other["member_rows"] == 2
    assert other["object_types"] == 2          # 两个不同的自定义类型
    assert payload["clusters"] == 3            # 顶层合计没有丢它们
    assert payload["member_rows"] == 3
    # 类型名是用户内容,绝不能出现在这个查询层的返回里。
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "根因分析动作" not in serialized
    assert "周几" not in serialized


def test_cluster_size_histogram_excluded_rows_are_attributed_to_their_type(repo):
    """被排除的成员行也按类型归属,不能全堆到 concept 上。"""
    with repo._write() as db:
        _object(db, "o-c-dead", status="deprecated")
        _cluster(db, "K-c", "o-c-dead")
        _object(db, "o-l", object_type="claim")
        _object(db, "o-l-dead", object_type="claim", status="deprecated")
        _cluster(db, "KL-1", "o-l", object_type="claim")
        _cluster(db, "KL-1", "o-l-dead", object_type="claim")

    payload = repo.kg_cluster_size_histogram(NB)

    assert _group(payload, "concept")["excluded_member_rows"] == 1
    assert _group(payload, "concept")["empty_clusters"] == 1
    assert _group(payload, "claim")["excluded_member_rows"] == 1
    assert _group(payload, "claim")["empty_clusters"] == 0
    assert payload["excluded_member_rows"] == 2


def test_cluster_size_histogram_splits_a_canonical_id_shared_across_types(repo):
    """分组键必须是 **(object_type, canonical_id)**,不是光 canonical_id。

    今天的 canonical_id 带类型前缀(K-/KL-/KF-/KP-),所以「同一个 canonical_id 出现在
    两个类型下」在正常数据里不会发生 —— 也正因如此,只用 `GROUP BY c.canonical_id`
    在普通夹具上**测不出来**(SQLite 允许 bare column,会随便挑一行的 object_type 报;
    PostgreSQL 则直接是语法错)。这条用例刻意造出那种行,让分组键的漂移当场报红。
    """
    with repo._write() as db:
        _object(db, "o-c")
        _object(db, "o-l", object_type="claim")
        _cluster(db, "SHARED", "o-c", object_type="concept")
        _cluster(db, "SHARED", "o-l", object_type="claim")

    payload = repo.kg_cluster_size_histogram(NB)

    # 两个类型各自一个单元素簇,而不是一个合并的双成员簇。
    assert _group(payload, "concept")["clusters"] == 1
    assert _group(payload, "concept")["member_rows"] == 1
    assert _group(payload, "claim")["clusters"] == 1
    assert _group(payload, "claim")["member_rows"] == 1
    assert payload["clusters"] == 2
    assert _bucket(payload, "1")["clusters"] == 2
    assert _bucket(payload, "2")["clusters"] == 0


def test_cluster_object_types_match_the_extraction_pipeline(repo):
    """契约里的四类必须与抽取管线真正会产出的类型逐字一致(drift 守卫)。"""
    from app.services.extraction_profiles import OBJECT_TYPE_LABELS

    assert set(CLUSTER_OBJECT_TYPES) == set(OBJECT_TYPE_LABELS)
    assert OTHER_OBJECT_TYPE_GROUP not in CLUSTER_OBJECT_TYPES


def test_empty_bucket_name_is_not_a_size_bucket():
    """落空簇的桶名不能撞进尺寸桶,否则 '成员全被排除' 会被算成一档簇大小。"""
    assert EMPTY_CLUSTER_BUCKET not in CLUSTER_SIZE_BUCKETS


# ------------------------------------------------------------------ 头部簇

def test_largest_clusters_orders_by_members_and_flags_truncation(repo):
    with repo._write() as db:
        for size, cid in ((3, "K-big"), (2, "K-mid"), (1, "K-small")):
            for i in range(size):
                _object(db, f"o-{cid}-{i}")
                _cluster(db, cid, f"o-{cid}-{i}", name=f"name of {cid}")

    full = repo.kg_largest_clusters(NB, limit=10)
    assert [c["canonical_id"] for c in full["clusters"]] == ["K-big", "K-mid", "K-small"]
    assert full["clusters"][0] == {
        "canonical_id": "K-big", "canonical_name": "name of K-big", "members": 3,
    }
    assert full["truncated"] is False
    assert full["object_type"] == "concept"

    capped = repo.kg_largest_clusters(NB, limit=2)
    assert [c["canonical_id"] for c in capped["clusters"]] == ["K-big", "K-mid"]
    assert capped["truncated"] is True
    assert capped["limit"] == 2


def test_largest_clusters_exactly_at_the_limit_is_not_truncated(repo):
    """边界档:簇数**恰好等于** limit 时没有被截断。
    `truncated = len(rows) > limit` 写成 `>=` 只有这一档抓得住 —— 「少于上限」和
    「多于上限」两档在两种写法下结果相同。"""
    with repo._write() as db:
        for i in range(3):
            _object(db, f"o-{i}")
            _cluster(db, f"K-{i}", f"o-{i}")

    exact = repo.kg_largest_clusters(NB, limit=3)
    assert len(exact["clusters"]) == 3
    assert exact["truncated"] is False

    # 紧邻的另一侧:多一个簇就必须报截断。
    with repo._write() as db:
        _object(db, "o-extra")
        _cluster(db, "K-extra", "o-extra")
    assert repo.kg_largest_clusters(NB, limit=3)["truncated"] is True


def test_largest_clusters_clamps_the_caller_limit(repo):
    """无界返回在 200 万簇的库上就是把整张表搬进内存 —— limit 必须被 clamp,
    而且 clamp 后的有效值要回给调用方。"""
    assert repo.kg_largest_clusters(NB, limit=10**9)["limit"] == LARGEST_CLUSTERS_MAX
    assert repo.kg_largest_clusters(NB, limit=0)["limit"] == 1
    assert repo.kg_largest_clusters(NB, limit=-5)["limit"] == 1


def test_largest_clusters_excludes_unusable_members(repo):
    with repo._write() as db:
        _object(db, "o-live")
        _object(db, "o-dead", status="deprecated")
        _cluster(db, "K", "o-live")
        _cluster(db, "K", "o-dead")

    clusters = repo.kg_largest_clusters(NB, limit=10)["clusters"]
    assert clusters == [
        {"canonical_id": "K", "canonical_name": "K", "members": 1}
    ]


def test_largest_clusters_excludes_members_owned_by_another_notebook(repo):
    """成员对象属于**别的 notebook** 时不算本库的可用成员。

    `concept_clusters.member_object_id` 没有外键、也没有 notebook 归属约束,所以
    JOIN 上必须显式带 `o.notebook_id = c.notebook_id`。这条用例是那个谓词的唯一证人:
    只用 deprecated 成员的用例在删掉归属约束后照样绿(实测确认过)。
    """
    with repo._write() as db:
        _object(db, "o-mine")
        _object(db, "o-theirs", notebook=OTHER_NB)
        _cluster(db, "K", "o-mine")
        _cluster(db, "K", "o-theirs")

    assert repo.kg_largest_clusters(NB, limit=10)["clusters"] == [
        {"canonical_id": "K", "canonical_name": "K", "members": 1}
    ]


def test_largest_clusters_drops_clusters_with_no_usable_member(repo):
    with repo._write() as db:
        _object(db, "o-dead", status="deprecated")
        _cluster(db, "K-dead", "o-dead")

    assert repo.kg_largest_clusters(NB, limit=10)["clusters"] == []


def test_largest_clusters_prefers_a_non_empty_canonical_name(repo):
    """同一 canonical_id 下 canonical_name 并不保证一致(kg_merge 对新簇用对象自己的
    name,无名对象就是空串),而空串在裸 MIN() 下永远排第一 —— 榜首会变成
    {"canonical_name": "", "members": N}。无名对象在生产 base 库真实存在。"""
    with repo._write() as db:
        for i in range(3):
            _object(db, f"o-{i}")
        _cluster(db, "K", "o-0", name=" ")            # 占位:见下面显式改成空串
        _cluster(db, "K", "o-1", name="热阻 Rth")
        _cluster(db, "K", "o-2", name="Zzz late name")
        db.execute("UPDATE concept_clusters SET canonical_name='' WHERE id='cc-K-o-0'")

    clusters = repo.kg_largest_clusters(NB, limit=10)["clusters"]
    assert clusters == [
        {"canonical_id": "K", "canonical_name": "Zzz late name", "members": 3}
    ]


def test_largest_clusters_falls_back_to_empty_when_every_name_is_blank(repo):
    """全组都无名时 MIN(NULLIF(...)) 返回 NULL,必须兜回空串而不是 None。"""
    with repo._write() as db:
        _object(db, "o-0")
        _cluster(db, "K", "o-0")
        db.execute("UPDATE concept_clusters SET canonical_name=''")

    assert repo.kg_largest_clusters(NB, limit=10)["clusters"] == [
        {"canonical_id": "K", "canonical_name": "", "members": 1}
    ]


def test_largest_clusters_only_ranks_concept_clusters(repo):
    """D1:榜单只算 concept —— 整句 claim 当名字上榜没有意义。其他类型的计数由
    cluster_size_histogram 按类型报出,不在这里静默丢。"""
    with repo._write() as db:
        for i in range(5):
            _object(db, f"o-l-{i}", object_type="claim")
            _cluster(db, "KL-huge", f"o-l-{i}", object_type="claim",
                     name="这是一整句论断,不该出现在概念榜上")
        _object(db, "o-c")
        _cluster(db, "K-c", "o-c", name="热阻")

    payload = repo.kg_largest_clusters(NB, limit=10)

    assert payload["object_type"] == "concept"
    assert [c["canonical_id"] for c in payload["clusters"]] == ["K-c"]
    # 但它们没有消失:直方图那边如实报出。
    assert _group(repo.kg_cluster_size_histogram(NB), "claim")["member_rows"] == 5


# -------------------------------------------------------------- 主题板块列表

def _community(db, cid: str, members: list[tuple[str, float]], *, level: int = 0) -> None:
    db.execute(
        "INSERT INTO communities (id,notebook_id,level,member_ids,size,created_at) "
        "VALUES (?,?,?,?,?, '2026-01-01T00:00:00')",
        (cid, NB, level, json.dumps([m for m, _ in members]), len(members)),
    )
    for canonical_id, centrality in members:
        db.execute(
            "INSERT INTO community_members "
            "(canonical_id,notebook_id,level,community_id,canonical_name,centrality) "
            "VALUES (?,?,?,?,?,?)",
            (canonical_id, NB, level, cid, f"name of {canonical_id}", centrality),
        )


def test_community_overview_ranks_boards_by_size_and_members_by_centrality(repo):
    with repo._write() as db:
        _community(db, "cm-small", [("a", 0.9), ("b", 0.1)])
        _community(db, "cm-big", [("x", 0.1), ("y", 0.9), ("z", 0.5)])

    payload = repo.kg_community_overview(NB, limit=10, top_k=2)

    assert payload["level"] == 0
    assert payload["total"] == 2
    assert payload["returned"] == 2
    assert payload["truncated"] is False
    assert [c["id"] for c in payload["communities"]] == ["cm-big", "cm-small"]
    big = payload["communities"][0]
    assert big["size"] == 3
    assert big["top_members"] == ["name of y", "name of z"]   # centrality 降序
    assert big["top_members_truncated"] is True               # 3 个成员只给了 2 个
    small = payload["communities"][1]
    assert small["top_members"] == ["name of a", "name of b"]
    assert small["top_members_truncated"] is False


def test_community_overview_flags_board_truncation_and_reports_the_total(repo):
    """绝不静默截断:板块被砍掉时 total/returned/truncated 三件套要说得清。"""
    with repo._write() as db:
        for i in range(5):
            _community(db, f"cm-{i}", [(f"n-{i}", 1.0)] * 1)

    payload = repo.kg_community_overview(NB, limit=2, top_k=3)

    assert payload["total"] == 5
    assert payload["returned"] == 2
    assert payload["truncated"] is True
    assert payload["limit"] == 2
    assert payload["top_members_limit"] == 3


def test_community_overview_clamps_both_limits(repo):
    payload = repo.kg_community_overview(NB, limit=10**9, top_k=10**9)
    assert payload["limit"] == COMMUNITY_OVERVIEW_MAX
    assert payload["top_members_limit"] == COMMUNITY_TOP_MEMBERS_MAX

    payload = repo.kg_community_overview(NB, limit=0, top_k=0)
    assert payload["limit"] == 1
    assert payload["top_members_limit"] == 1


def test_community_overview_is_scoped_to_the_requested_level(repo):
    with repo._write() as db:
        _community(db, "cm-l0", [("a", 1.0)], level=0)
        _community(db, "cm-l1", [("b", 1.0)], level=1)

    level0 = repo.kg_community_overview(NB, level=0)
    level1 = repo.kg_community_overview(NB, level=1)

    assert [c["id"] for c in level0["communities"]] == ["cm-l0"]
    assert [c["id"] for c in level1["communities"]] == ["cm-l1"]
    assert level1["level"] == 1


def test_community_overview_keeps_a_board_with_no_member_rows(repo):
    """成员行缺失(损坏/半写数据)的板块不能从列表里消失 —— 它照样出现,
    top_members 为空且 top_members_truncated 说明「还有 size 个没给出来」。"""
    with repo._write() as db:
        _community(db, "cm-ok", [("a", 1.0)])
        db.execute(
            "INSERT INTO communities (id,notebook_id,level,member_ids,size,created_at) "
            "VALUES ('cm-orphan',?,0,'[\"x\",\"y\"]',2,'2026-01-01T00:00:00')", (NB,))

    payload = repo.kg_community_overview(NB, limit=10, top_k=3)

    assert [c["id"] for c in payload["communities"]] == ["cm-orphan", "cm-ok"]
    orphan = payload["communities"][0]
    assert orphan["size"] == 2
    assert orphan["top_members"] == []
    assert orphan["top_members_truncated"] is True


def test_community_overview_on_a_notebook_without_communities(repo):
    payload = repo.kg_community_overview(OTHER_NB)
    assert payload == {
        "level": 0, "total": 0, "returned": 0, "truncated": False,
        "limit": 50, "top_members_limit": 5, "communities": [],
    }


def test_community_overview_reads_one_consistent_snapshot(repo, tmp_path):
    """并发 rebuild_communities 在报告生成中途提交,载荷不得自相矛盾。

    没有显式只读事务时,Python 的 sqlite3 不为 SELECT 开事务,连续两条 SELECT 各取
    一个快照:先数到 N 个板块、再一个都取不到 —— `total=N, returned=0`(「有 N 个
    板块,一个都没有」)。这里在**第一条语句之后**用另一条真连接提交一次整表删除
    (rebuild 的 DELETE 阶段),断言载荷仍然自洽。
    """
    with repo._write() as db:
        _community(db, "cm-a", [("a", 0.9), ("b", 0.5)])
        _community(db, "cm-b", [("c", 0.7)])

    db_path = repo._runtime.database.db_path
    fired: list[str] = []

    def wipe_from_another_connection() -> None:
        other = sqlite3.connect(db_path, timeout=10)
        try:
            other.execute("PRAGMA busy_timeout = 10000")
            other.execute("BEGIN IMMEDIATE")
            other.execute("DELETE FROM community_members")
            other.execute("DELETE FROM communities")
            other.commit()
        finally:
            other.close()

    connection = repo._connect()
    original_execute = connection.execute

    def spying_execute(sql, parameters=()):
        cursor = original_execute(sql, parameters)
        if "FROM communities" in sql and not fired:
            fired.append(sql)
            # 另一条真连接、独立事务:与被测连接不共享事务状态。
            worker = threading.Thread(target=wipe_from_another_connection)
            worker.start()
            worker.join(timeout=20)
            assert not worker.is_alive(), "并发写者被读事务卡死了(WAL 下不应发生)"
        return cursor

    connection.execute = spying_execute
    try:
        payload = repo.kg_community_overview(NB, limit=10, top_k=5)
    finally:
        del connection.execute

    assert fired, "探针没打中:第一条语句不是对 communities 的读"
    assert payload["total"] == 2
    assert payload["returned"] == 2, (
        "板块列表落在了删除之后的快照上 —— total/returned 自相矛盾"
    )
    assert payload["truncated"] is False
    assert [c["id"] for c in payload["communities"]] == ["cm-a", "cm-b"]
    assert payload["communities"][0]["top_members"] == ["name of a", "name of b"]


# ------------------------------------------------------------- 边的出处构成

def _relink(basis: str) -> str:
    return json.dumps([{"basis": basis, "quote": ""}])


def test_relation_provenance_splits_relink_from_untagged(repo):
    with repo._write() as db:
        for oid in ("a", "b", "c"):
            _object(db, oid)
        _relation(db, "r1", "a", "b", evidence=_relink("relink:shared-element"))
        _relation(db, "r2", "b", "c", evidence=_relink("relink:name-match"))
        # knowhow 投影写的 about 边:evidence='[]',与 LLM 抽取的边不可区分 → untagged。
        _relation(db, "r3", "a", "c")

    payload = repo.kg_relation_provenance_counts(NB)

    assert list(payload["buckets"]) == list(RELATION_PROVENANCE_BUCKETS)
    assert payload["buckets"]["relink:shared-element"] == 1
    assert payload["buckets"]["relink:name-match"] == 1
    assert payload["buckets"]["untagged"] == 1
    assert payload["relink"] == 2
    assert payload["counted"] == 3
    assert list(payload["excluded"]) == list(RELATION_EXCLUSION_BUCKETS)
    assert payload["excluded"] == {"rejected": 0, "endpoint_unusable": 0}
    assert payload["total_rows"] == 3


def test_relation_provenance_buckets_unknown_and_other_relink_bases(repo):
    with repo._write() as db:
        for oid in ("a", "b"):
            _object(db, oid)
        _relation(db, "r1", "a", "b", evidence=_relink("relink:future-rule"))
        _relation(db, "r2", "b", "a", evidence=_relink("delegation"))

    payload = repo.kg_relation_provenance_counts(NB)

    assert payload["buckets"]["relink:other"] == 1
    assert payload["buckets"]["tagged:other"] == 1
    assert payload["relink"] == 1


@pytest.mark.parametrize("evidence", ["", "not json", "{}", "[]", '[42]', '["x"]'])
def test_relation_provenance_treats_unreadable_evidence_as_untagged(repo, evidence):
    """畸形 / 非对象的 evidence 绝不能炸查询,也不能替它认领出处 —— 一律 untagged。"""
    with repo._write() as db:
        for oid in ("a", "b"):
            _object(db, oid)
        _relation(db, "r1", "a", "b", evidence=evidence)

    payload = repo.kg_relation_provenance_counts(NB)
    assert payload["buckets"]["untagged"] == 1
    assert payload["counted"] == 1


def test_relation_provenance_excludes_rejected_edges(repo):
    with repo._write() as db:
        for oid in ("a", "b"):
            _object(db, oid)
        _relation(db, "r1", "a", "b", evidence=_relink("relink:name-match"),
                  review_status="rejected")
        _relation(db, "r2", "b", "a", evidence=_relink("relink:name-match"))

    payload = repo.kg_relation_provenance_counts(NB)

    assert payload["buckets"]["relink:name-match"] == 1
    assert payload["excluded"]["rejected"] == 1
    assert payload["counted"] == 1
    assert payload["total_rows"] == 2


def test_relation_provenance_excludes_edges_whose_endpoint_is_not_usable(repo):
    """对端 deprecated / 不存在 / 属于别的 notebook —— 三种情况对产品是同一件事:
    那个端点不在可用节点集里,这条边进不了图。

    ⚠ **两个方向都要有跨 notebook 的证人**(`r-foreign` 是 target 跨库,
    `r-foreign-src` 是 source 跨库)。少了 source 那一条,删掉
    `s.notebook_id = r.notebook_id` 这个谓词后整个文件仍然全绿 —— 实测确认过。
    """
    with repo._write() as db:
        _object(db, "live")
        _object(db, "dead", status="deprecated")
        _object(db, "foreign", notebook=OTHER_NB)
        _relation(db, "r-dead", "live", "dead", evidence=_relink("relink:name-match"))
        _relation(db, "r-missing", "live", "ghost", evidence=_relink("relink:name-match"))
        _relation(db, "r-foreign", "live", "foreign", evidence=_relink("relink:name-match"))
        _relation(db, "r-foreign-src", "foreign", "live",
                  evidence=_relink("relink:name-match"))
        _relation(db, "r-reverse", "dead", "live", evidence=_relink("relink:name-match"))

    payload = repo.kg_relation_provenance_counts(NB)

    assert payload["counted"] == 0
    assert payload["excluded"]["endpoint_unusable"] == 5
    assert payload["total_rows"] == 5


def test_relation_provenance_counts_a_rejected_bad_edge_only_once(repo):
    """既被否决、端点又不可用的边只能计一次(rejected 判在最前),否则
    counted + excluded 就不再等于总行数。"""
    with repo._write() as db:
        _object(db, "live")
        _relation(db, "r", "live", "ghost", review_status="rejected")

    payload = repo.kg_relation_provenance_counts(NB)

    assert payload["excluded"] == {"rejected": 1, "endpoint_unusable": 0}
    assert payload["total_rows"] == 1


def test_relation_provenance_is_scoped_to_the_notebook(repo):
    with repo._write() as db:
        _object(db, "a")
        _object(db, "b")
        _relation(db, "r1", "a", "b", evidence=_relink("relink:name-match"))

    assert repo.kg_relation_provenance_counts(OTHER_NB)["total_rows"] == 0
    assert repo.kg_relation_provenance_counts(OTHER_NB)["counted"] == 0


def test_relation_provenance_on_an_empty_notebook_still_returns_all_buckets(repo):
    payload = repo.kg_relation_provenance_counts(NB)

    assert payload["buckets"] == {name: 0 for name in RELATION_PROVENANCE_BUCKETS}
    assert payload["excluded"] == {name: 0 for name in RELATION_EXCLUSION_BUCKETS}
    assert payload["total_rows"] == 0


# ------------------------------------------------------- 未知桶不得被静默吞掉

def test_assembly_rejects_bucket_names_outside_the_contract():
    """反方向的守卫:SQL 冒出契约里没有的桶名必须硬失败。

    静默 `rows.get(name, 0)` 会让那些行凭空消失 —— clusters / member_rows 少算,而
    excluded 仍把它们算进分母,不变式「计数 + 排除量 = 总行数」当场断掉且不报错。
    """
    from app.repositories.kg_analysis_payloads import (
        cluster_histogram_payload,
        relation_provenance_payload,
    )

    with pytest.raises(ValueError, match="gone"):
        cluster_histogram_payload({("concept", "gone"): (9, 9, 0)})
    with pytest.raises(ValueError, match="mystery"):
        relation_provenance_payload({"mystery": 7})

    # 契约内的桶名照常通过(守卫不是把一切都拒了)。
    assert cluster_histogram_payload({("concept", "1"): (1, 1, 0)})["clusters"] == 1
    assert relation_provenance_payload({"untagged": 1})["counted"] == 1


def _analysis_bucket_literals(cls, method: str) -> set:
    """抽出某个方法 SQL 里 CASE 分支产出的桶名字面量(`THEN '…'` / `ELSE '…'`)。

    只看 THEN/ELSE:`WHEN tag = 'relink:name-match'` 那种比较用的字面量不是桶名,
    `c.object_type = 'concept'` 之类的过滤条件同理。
    """
    import inspect
    import re

    source = inspect.getsource(getattr(cls, method))
    return set(re.findall(r"(?:THEN|ELSE)\s+'([^']*)'", source))


@pytest.mark.parametrize("module,cls_name", [
    ("app.repositories.sqlite.unified_kg_store", "UnifiedKgStore"),
    ("app.repositories.postgres.unified_kg_store", "UnifiedKgStore"),
])
def test_sql_case_bucket_literals_equal_the_contract(module, cls_name):
    """两侧 SQL 的 CASE 里出现的桶字面量必须与契约元组**逐字相等**。

    双向都要钉:少一个 → 那一档永远返回 0 且没人发现;多一个 → 装配层的未知桶守卫
    会在运行时炸,但那要等到有人真的调到那条数据。源码级守卫让漂移在门上就红。
    """
    import importlib

    cls = getattr(importlib.import_module(module), cls_name)

    assert _analysis_bucket_literals(cls, "cluster_size_histogram") == \
        set(CLUSTER_SIZE_BUCKETS) | {EMPTY_CLUSTER_BUCKET}
    assert _analysis_bucket_literals(cls, "relation_provenance_counts") == \
        set(RELATION_PROVENANCE_BUCKETS) | set(RELATION_EXCLUSION_BUCKETS)


# ------------------------------------------------------------------ 边界契约

_ANALYSIS_TABLES = (
    "concept_clusters",
    "knowledge_objects",
    "knowledge_relations",
    "communities",
    "community_members",
    "unified_kg_state",
)

_DML_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
              "TRUNCATE", "VACUUM")


def _table_fingerprints(db) -> dict:
    """每张表的 (行数, 全表内容指纹)。指纹用 group_concat(quote(全部列)) 排序后取,
    行数不变但内容被改写(UPDATE)时也会变 —— 这是 total_changes 之外的第二道观测量,
    而且它能抓到**经另一条连接**发生的写。"""
    out = {}
    for table in _ANALYSIS_TABLES:
        columns = [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]
        projection = " || '|' || ".join(f"quote({c})" for c in columns)
        row = db.execute(
            f"SELECT COUNT(*) AS n, "
            f"COALESCE(group_concat(sig, char(10)), '') AS sig FROM "
            f"(SELECT {projection} AS sig FROM {table} ORDER BY sig)"
        ).fetchone()
        out[table] = (int(row["n"]), row["sig"])
    return out


def test_analysis_queries_never_write(repo):
    """只读,用两道**对写真正敏感**的观测量钉:

    1. 语句级 trace —— 拦下被测连接上执行的每一条 SQL,出现任何 DML/DDL 动词就失败。
       它连「改了 0 行的 UPDATE」都抓得住。
    2. 相关表的行数 + 全表内容指纹 —— 抓经**另一条**连接(例如 database.write())
       发生的写,trace 看不见那些。

    这两道替换了初版那条空守卫:它断言 `kg_mutation_seq` 不变,而那个计数器**只由
    mark_dirty 推进**,直写根本不碰它 —— 在 cluster_size_histogram 里插
    `DELETE FROM concept_clusters` + `UPDATE knowledge_objects SET status='deprecated'`
    照样 1 passed。
    """
    with repo._write() as db:
        _object(db, "a")
        _object(db, "b")
        _cluster(db, "K", "a")
        _relation(db, "r1", "a", "b")
        _community(db, "cm", [("K", 1.0)])
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at) "
            "VALUES (?, 0, 7, '2026-01-01T00:00:00')", (NB,),
        )

    connection = repo._connect()
    before = _table_fingerprints(connection)

    seen: list[str] = []
    connection.set_trace_callback(seen.append)
    try:
        repo.kg_cluster_size_histogram(NB)
        repo.kg_largest_clusters(NB)
        repo.kg_community_overview(NB)
        repo.kg_relation_provenance_counts(NB)
    finally:
        connection.set_trace_callback(None)

    assert seen, "trace 没装上:没有观察到任何语句"
    offenders = [
        sql for sql in seen
        if any(sql.lstrip().upper().startswith(verb) for verb in _DML_VERBS)
    ]
    assert offenders == [], f"只读查询里出现了写语句:{offenders}"

    after = _table_fingerprints(connection)
    assert after == before, "相关表的行数/内容在只读查询前后发生了变化"


def _dotted(node) -> str:
    """`self._runtime.unified_kg.foo` → 'self._runtime.unified_kg.foo'。
    只比末段(`.attr == 'unified_kg'`)钉不住深度——`self._runtime.x.unified_kg.foo`
    的末段同样是 unified_kg,两跳会溜过去。必须比整条链。"""
    import ast

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_facade_delegates_reach_the_unified_store_in_one_hop():
    """架构红线:facade 只做**一跳**委托,不许多绕一层。

    强度如实说明:本守卫比对的是「方法体里恰好一次属性调用,且调用链恰好是
    `self._runtime.unified_kg.<member>`」。它**不**覆盖非 Call 的后处理(对返回值取
    下标、做算术等不产生 ast.Call 节点,不被这里计数)—— 也就是说「聚合逻辑爬回
    facade」只被挡住了「再调一个函数」那一半。要连另一半也挡住,得改成「方法体只有
    一条 return 语句」的形状断言;现在没做,是因为四个委托本来就都是单条 return,
    而更严的形状断言会把将来合理的参数规整(如 clamp)一并禁掉。
    """
    import ast
    import inspect

    from app.services.repository_facade import RepositoryFacade

    for name, member in (
        ("kg_cluster_size_histogram", "cluster_size_histogram"),
        ("kg_largest_clusters", "largest_clusters"),
        ("kg_community_overview", "community_overview"),
        ("kg_relation_provenance_counts", "relation_provenance_counts"),
    ):
        source = inspect.getsource(getattr(RepositoryFacade, name))
        tree = ast.parse(source.lstrip())
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert len(calls) == 1, name
        assert _dotted(calls[0].func) == f"self._runtime.unified_kg.{member}", name


def test_both_backend_stores_declare_the_same_analysis_surface():
    """parity 红线:SQLite 与 PostgreSQL 两侧必须都实现,且都在 port 上声明。"""
    from app.repositories.ports import UnifiedKgStorePort
    from app.repositories.postgres.unified_kg_store import (
        UnifiedKgStore as PostgresUnifiedKgStore,
    )
    from app.repositories.sqlite.unified_kg_store import (
        UnifiedKgStore as SqliteUnifiedKgStore,
    )

    for name in (
        "cluster_size_histogram",
        "largest_clusters",
        "community_overview",
        # ⚠ connection-taking 的那半必须一起钉:查询本体住在它里面,只钉自开入口的话
        # 一侧把 SQL 改成另一种语义、另一侧不动,这条守卫看不见(codex 第 12 轮 P2)。
        "community_overview_on",
        "relation_provenance_counts",
    ):
        assert name in SqliteUnifiedKgStore.__dict__, name
        assert name in PostgresUnifiedKgStore.__dict__, name
        assert hasattr(UnifiedKgStorePort, name), name
        assert (
            inspect_signature(SqliteUnifiedKgStore, name)
            == inspect_signature(PostgresUnifiedKgStore, name)
        ), name


def inspect_signature(cls, name):
    """两侧比对用的签名 —— **只把 `db` 的标注摘掉**,其余逐字比。

    connection-taking 的方法第一个参数是那一侧的连接类型(`sqlite3.Connection` 对
    `Any`),两侧本来就该不同:那是 connection-taking 的应有形态,不是漂移。逐字比会
    让这条 parity 守卫变成「不许有 connection-taking 方法」。摘掉的**只有它的标注**,
    参数名/位置/种类/默认值与其余全部参数的标注照比。
    """
    import inspect

    signature = inspect.signature(getattr(cls, name))
    parameters = [
        parameter.replace(annotation=inspect.Parameter.empty)
        if parameter.name == "db" else parameter
        for parameter in signature.parameters.values()
    ]
    return str(signature.replace(parameters=parameters))


_ANALYSIS_METHODS = (
    "cluster_size_histogram",
    "largest_clusters",
    "community_overview",
    # 板块列表的 SQL 在 `_on` 里(自开快照的 `community_overview` 只剩守卫 + 开快照),
    # 漏了它这条嗅探守卫就只在看一个两行的包装。
    "community_overview_on",
    "relation_provenance_counts",
    "_read_snapshot",
)

# 「这条连接/库是哪个后端」的运行时嗅探形态。后端选择只存在于 factory,store 里出现
# 任何一条都意味着某侧 adapter 开始自己判 dialect。
_DIALECT_SNIFFS = (
    "__class__.__name__",
    "type(self.database)",
    "isinstance(self.database",
    "PostgresDatabase",
    "SqliteDatabase",
    "postgresql",
    "dialect",
    "backend ==",
    'backend == "postgres"',
    "is_postgres",
    "is_sqlite",
)


def _method_body_without_docstring(method) -> str:
    """方法体源码,**只**去掉 docstring。

    刻意不用 `source.split('\"\"\"')[-1]`:那在「方法体里还有三引号 SQL」时会连 SQL
    一起切掉(community_overview / cluster_size_histogram 都是这种形状),守卫就只剩
    最后几行可看。走 ast:精确摘掉 docstring 节点,其余(含 SQL 字面量)原样保留。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    function = tree.body[0]
    body = function.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


def test_analysis_stores_never_sniff_the_backend_at_runtime():
    """store 不判 dialect(后端选择只存在于 factory),也不 import 对侧 adapter。

    初版这条只查跨侧 import,名不副实:往 sqlite store 塞
    `if self.database.__class__.__name__ == "PostgresDatabase":` 照样绿。现在**两件事
    都查**——分析方法体里不得出现运行时后端嗅探,文件级不得 import 对侧 adapter。

    注意:两侧 SQL **形态**允许分岔(community_overview 在 PG 上是窗口函数),被禁的是
    「同一份代码在运行时判自己跑在哪个后端上」,不是「两侧各写各的方言 SQL」。
    """
    import inspect
    from pathlib import Path

    from app.repositories.postgres.unified_kg_store import (
        UnifiedKgStore as PostgresUnifiedKgStore,
    )
    from app.repositories.sqlite.unified_kg_store import (
        UnifiedKgStore as SqliteUnifiedKgStore,
    )

    for cls in (SqliteUnifiedKgStore, PostgresUnifiedKgStore):
        for method in _ANALYSIS_METHODS:
            body = _method_body_without_docstring(getattr(cls, method))
            for sniff in _DIALECT_SNIFFS:
                assert sniff not in body, f"{cls.__module__}.{method}: {sniff}"

    root = Path(__file__).resolve().parents[1] / "app" / "repositories"
    sqlite_source = (root / "sqlite" / "unified_kg_store.py").read_text(encoding="utf-8")
    postgres_source = (root / "postgres" / "unified_kg_store.py").read_text(encoding="utf-8")

    assert "app.repositories.postgres" not in sqlite_source
    assert "app.repositories.sqlite" not in postgres_source
    neutral = (root / "kg_analysis_payloads.py").read_text(encoding="utf-8")
    assert "app.repositories.sqlite" not in neutral
    assert "app.repositories.postgres" not in neutral
    assert ".execute(" not in neutral        # 中性装配层不许拼 SQL
