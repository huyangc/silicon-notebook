"""KG 质量分析预计算产物(T2)的回归门。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md` §3.2/§3.3/§3.4。

分两层:
  · 纯折叠(head_community_ids / fold_cross_community_edges / SourceProfileFolder)
    不起库直接钉边界与并列消歧;
  · 端到端跑真 `rebuild_communities`,钉三张产物表的内容、账本的版本戳、
    「一次预计算原子可见」、以及每一条降级路径都**缺失**而不是写半份。

双后端一致性(同一批夹具在 SQLite / PostgreSQL 上产出同一份产物)在
`tests/postgres/test_knowledge_store_conformance.py` 里,那边有真 PostgreSQL 泳道。
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg_analysis_precompute import (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_KINDS,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_SOURCE_PROFILES,
    CLUSTER_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_SEQ_PAYLOAD_KEY,
    MAINSTREAM_COVERAGE,
    MAX_PERSISTED_COMMUNITY_EDGES,
    CrossCommunityEdgeFolder,
    SourceProfileFolder,
    analysis_ledger_is_current,
    artifact_is_current,
    check_artifact_payloads,
    fold_cross_community_edges,
    head_community_ids,
    required_artifact_kinds,
    reusable_artifact_payloads,
    stamp_cluster_seq,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


# --------------------------------------------------------------- 纯折叠


def test_mainstream_coverage_threshold_is_a_named_half():
    """阈值必须是具名常量:它随产物写进账本,日后改口径旧产物才不会被误读。"""
    assert MAINSTREAM_COVERAGE == 0.5


def test_head_community_ids_keeps_the_board_that_crosses_the_threshold():
    # 6+3+1 = 10,阈值 5.0。最大板块 6 >= 5.0 → 头部只有它。
    head, covered, total = head_community_ids([("b", 3), ("a", 6), ("c", 1)])
    assert (head, covered, total) == (frozenset({"a"}), 6, 10)


def test_head_community_ids_covers_at_least_half_on_an_exact_tie():
    """两个等大板块:第一个恰好覆盖 50%。

    `>` 而不是 `>=` 的写法会在这一档返回空集(严格大于才收),而空的头部集合会让
    **每一个**来源的 mainstream_share 都变成 0.0 —— 报告会宣称整库都是关联稀疏来源。
    """
    head, covered, total = head_community_ids([("a", 4), ("b", 4)])
    assert head == frozenset({"a"})
    assert covered / total >= MAINSTREAM_COVERAGE


def test_head_community_ids_breaks_size_ties_by_id_not_by_input_order():
    # 3 个等大板块(5 each,总 15,阈值 7.5):累积到第二个才越过阈值,所以头部是
    # 字典序最小的两个。输入顺序绝不能影响结果 —— kept_rows 的顺序来自 Louvain。
    forward = head_community_ids([("z", 5), ("a", 5), ("m", 5)])[0]
    backward = head_community_ids([("m", 5), ("a", 5), ("z", 5)])[0]
    assert forward == backward == frozenset({"a", "m"})


def test_head_community_ids_is_empty_without_members():
    assert head_community_ids([]) == (frozenset(), 0, 0)
    assert head_community_ids([("a", 0), ("b", 0)]) == (frozenset(), 0, 0)


def test_fold_cross_community_edges_normalizes_direction_and_splits_intra():
    # community_of_index[i] = 节点下标 i 的板块(刻意是按下标的 list,不是 str->str dict)
    folder = fold_cross_community_edges(
        {
            (0, 1): 2,   # cm-b <-> cm-a
            (3, 2): 5,   # cm-b <-> cm-a,反向 —— 必须折进同一个键
            (1, 2): 7,   # cm-a 内部
            (0, 3): 4,   # cm-b 内部
        },
        ["cm-b", "cm-a", "cm-a", "cm-b"],
    )
    assert folder.top_edges(10) == [("cm-a", "cm-b", 7)]
    assert (folder.intra_weight, folder.cross_weight) == (11, 7)


def test_fold_cross_community_edges_skips_canonicals_outside_kept_boards():
    """min-size 过滤掉的板块成员在 `communities` 里根本不存在,折进去就是悬空引用。"""
    folder = fold_cross_community_edges(
        {(0, 1): 3, (0, 2): 9},
        ["cm-a", "cm-b", None],   # 下标 2 的节点落选
    )
    assert folder.top_edges(10) == [("cm-a", "cm-b", 3)]
    assert (folder.intra_weight, folder.cross_weight) == (0, 3)


def test_cross_edge_folder_truncates_by_weight_and_reports_the_total():
    """明细表有硬上界(生产上跨板块边数没有任何结构性约束),截断绝不静默。"""
    folder = CrossCommunityEdgeFolder()
    folder.add("cm-a", "cm-b", 1)
    folder.add("cm-a", "cm-c", 9)
    folder.add("cm-b", "cm-c", 5)
    assert folder.total_edges == 3
    assert folder.cross_weight == 15
    # 按 weight 降序取,不是按 id 序 —— 俯瞰图要的是最重的那些边。
    assert folder.top_edges(2) == [("cm-a", "cm-c", 9), ("cm-b", "cm-c", 5)]
    assert folder.top_edges(0) == []


def test_cross_edge_folder_breaks_weight_ties_deterministically():
    """并列必须按 (src, dst) 升序 —— dict 的迭代顺序不能泄漏进产物。"""
    forward = CrossCommunityEdgeFolder()
    for pair in (("cm-z", "cm-y"), ("cm-a", "cm-b"), ("cm-m", "cm-n")):
        forward.add(pair[0], pair[1], 4)
    backward = CrossCommunityEdgeFolder()
    for pair in (("cm-m", "cm-n"), ("cm-a", "cm-b"), ("cm-z", "cm-y")):
        backward.add(pair[0], pair[1], 4)
    assert forward.top_edges(2) == backward.top_edges(2) == [
        ("cm-a", "cm-b", 4), ("cm-m", "cm-n", 4),
    ]


def test_cross_edge_folder_row_stream_matches_the_preaggregated_fold():
    """两条喂入路径(整数边权 dict / 逐行 weight=1)必须给出逐字相同的产物。

    只补账本那条路径按行喂,全量重建那条路径喂 `ew`。`ew` 只是「同一对 canonical 出现
    了几次」的预聚合,所以两者对每个板块对的合计恒等 —— 这条把它钉住。
    """
    index_map = ["cm-a", "cm-a", "cm-b", "cm-c"]
    edge_weights = {(0, 2): 2, (1, 2): 1, (2, 3): 4, (0, 1): 3}
    pre_aggregated = fold_cross_community_edges(edge_weights, index_map)

    streamed = CrossCommunityEdgeFolder()
    for (left, right), weight in edge_weights.items():
        for _ in range(weight):          # 同一对 canonical 出现 weight 次
            streamed.add(index_map[left], index_map[right], 1)

    assert streamed.top_edges(10) == pre_aggregated.top_edges(10)
    assert streamed.intra_weight == pre_aggregated.intra_weight
    assert streamed.cross_weight == pre_aggregated.cross_weight


def test_source_profile_folder_computes_the_four_documented_quantities():
    folder = SourceProfileFolder(frozenset({"cm-main"}))
    folder.add("src", "cm-main", 6)
    folder.add("src", "cm-side", 2)
    folder.add("src", None, 4)          # 没进任何板块的对象
    (source_id, n_objects, n_graph_objects, top, top_share, spread,
     mainstream) = folder.rows()[0]
    assert source_id == "src"
    assert n_objects == 12              # 6 + 2 + 4,含没进板块的
    assert n_graph_objects == 8         # 只有进了板块的进分母
    assert (top, top_share) == ("cm-main", 0.75)
    assert spread == 2                  # NULL 组不算一个板块
    assert mainstream == 0.75


def test_source_profile_folder_breaks_top_board_ties_by_id_regardless_of_order():
    """GROUP BY 的行到达顺序在两个后端上都未定义 —— 并列必须按 id 消歧。

    没有这条,同一份数据在 SQLite 与 PostgreSQL 上可以给出不同的 top_community_id,
    而两边都「没错」;parity 会以最难查的方式碎掉。
    """
    forward = SourceProfileFolder(frozenset())
    for community_id in ("cm-z", "cm-a", "cm-m"):
        forward.add("src", community_id, 4)
    backward = SourceProfileFolder(frozenset())
    for community_id in ("cm-m", "cm-a", "cm-z"):
        backward.add("src", community_id, 4)
    assert forward.rows()[0][3] == backward.rows()[0][3] == "cm-a"


def test_source_profile_folder_reports_a_fully_unboarded_source():
    """一个对象都没进板块的来源是最强的「关联稀疏」信号,必须出现在结果里而不是消失。"""
    folder = SourceProfileFolder(frozenset({"cm-main"}))
    folder.add("src", None, 3)
    assert folder.rows() == [("src", 3, 0, "", 0.0, 0, 0.0)]


def test_source_profile_rows_are_source_id_ordered():
    folder = SourceProfileFolder(frozenset())
    for source_id in ("src-c", "src-a", "src-b"):
        folder.add(source_id, "cm", 1)
    assert [row[0] for row in folder.rows()] == ["src-a", "src-b", "src-c"]


def _full_payloads(*, cluster_seq: int = 0, **overrides) -> dict:
    """一批合法的账本 payload —— 簇世代照生产路径经 `stamp_cluster_seq` 盖上。

    夹具刻意**不手抄**那个 key:分类(哪几份依赖合并结果)一旦改了,夹具跟着改,
    不会出现「守卫按新分类查、夹具还按旧分类拼」这种两边各自自洽却对不上的局面。
    """
    payloads = {kind: {"level": 0} for kind in ARTIFACT_KINDS}
    payloads[ARTIFACT_COMMUNITY_EDGES] = {"level": 0, "communities": 2}
    payloads.update(overrides)
    return stamp_cluster_seq(payloads, cluster_seq)


def _ledger(seqs: dict, *, cluster_seq: int = 0) -> dict:
    """``{kind: kg_seq}`` → `kg_analysis_artifact_rows` 的形状(闸真正吃的那个)。"""
    stamped = stamp_cluster_seq({kind: {"level": 0} for kind in seqs}, cluster_seq)
    return {
        kind: {"kg_mutation_seq": seq, "payload": stamped[kind],
               "created_at": "2026-07-27"}
        for kind, seq in seqs.items()
    }


def test_check_artifact_payloads_rejects_unknown_kinds():
    check_artifact_payloads(_full_payloads())
    with pytest.raises(ValueError, match="契约外的 kind"):
        check_artifact_payloads(_full_payloads(board_edges={}))


def test_check_artifact_payloads_rejects_a_missing_kind():
    """账本是整批重写的:少写一行,下游读到的是「从来没算过」,而且不会有任何报错。"""
    for kind in (ARTIFACT_CLUSTER_HISTOGRAM, ARTIFACT_LARGEST_CLUSTERS,
                 ARTIFACT_RELATION_PROVENANCE, ARTIFACT_COMMUNITY_EDGES):
        payloads = _full_payloads()
        payloads.pop(kind)
        with pytest.raises(ValueError, match="缺少必需的 kind"):
            check_artifact_payloads(payloads)


def test_check_artifact_payloads_allows_absent_profiles_only_without_boards():
    empty = _full_payloads()
    empty.pop(ARTIFACT_SOURCE_PROFILES)
    empty[ARTIFACT_COMMUNITY_EDGES] = dict(
        empty[ARTIFACT_COMMUNITY_EDGES], communities=0
    )
    check_artifact_payloads(empty)                      # 零板块:合法缺席

    with_boards = _full_payloads()
    with_boards.pop(ARTIFACT_SOURCE_PROFILES)
    with pytest.raises(ValueError, match="才允许缺席"):
        check_artifact_payloads(with_boards)            # 有板块却缺席 = 漏写


def test_check_artifact_payloads_demands_the_cluster_stamp_exactly_where_it_belongs():
    """簇世代的戳:依赖合并结果的四份必须有,不依赖的那份必须没有。

    两个方向都拦是有理由的 —— 漏盖不会报错,只会让这个库的预计算**永远**重跑
    (闸判它不新鲜)、读侧永远报「合并世代未知」;多盖则相反,给一份输入根本没变的
    产物编造一个落后量。两种都是静默的错。
    """
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS):
        missing = _full_payloads()
        missing[kind] = {
            key: value for key, value in missing[kind].items()
            if key != CLUSTER_SEQ_PAYLOAD_KEY
        }
        with pytest.raises(ValueError, match="必须带整数"):
            check_artifact_payloads(missing)
        # 盖了但不是整数,与没盖同档(库被手工改过)。
        garbled = _full_payloads()
        garbled[kind] = dict(garbled[kind], **{CLUSTER_SEQ_PAYLOAD_KEY: "7"})
        with pytest.raises(ValueError, match="必须带整数"):
            check_artifact_payloads(garbled)

    extra = _full_payloads()
    extra[ARTIFACT_RELATION_PROVENANCE] = dict(
        extra[ARTIFACT_RELATION_PROVENANCE], **{CLUSTER_SEQ_PAYLOAD_KEY: 3}
    )
    with pytest.raises(ValueError, match="不依赖合并结果"):
        check_artifact_payloads(extra)


def test_relation_provenance_is_the_only_kind_outside_the_cluster_classification():
    """分类的依据是「那条查询读没读 concept_clusters」,这里把结论钉死。

    钉住它不是形式主义:分类同时决定**哪些产物会被合并写入作废**。一刀切多算一份,
    每次纯合并写入都要白付一趟 836 万边的全表扫;少算一份,那份就永远陈旧而报告
    还说「与当前一致」—— 正是本次修复要消灭的那一档。
    """
    assert set(ARTIFACT_KINDS) - CLUSTER_DEPENDENT_ARTIFACT_KINDS == {
        ARTIFACT_RELATION_PROVENANCE
    }


def test_analysis_ledger_is_current_requires_every_kind_at_the_same_seq():
    full = _ledger({kind: 7 for kind in ARTIFACT_KINDS})
    assert analysis_ledger_is_current(full, 7, 0, has_boards=True)
    assert not analysis_ledger_is_current({}, 7, 0, has_boards=True)
    assert not analysis_ledger_is_current(full, 8, 0, has_boards=True)
    partial = dict(full)
    partial.pop(ARTIFACT_RELATION_PROVENANCE)
    assert not analysis_ledger_is_current(partial, 7, 0, has_boards=True)
    # 一行落后 = 整份不新鲜(否则「补一半」会被判成齐了)
    mixed = dict(full)
    mixed[ARTIFACT_LARGEST_CLUSTERS] = {
        **full[ARTIFACT_LARGEST_CLUSTERS], "kg_mutation_seq": 6,
    }
    assert not analysis_ledger_is_current(mixed, 7, 0, has_boards=True)


def test_analysis_ledger_is_current_also_tracks_the_cluster_generation():
    """簇世代漂了就必须补跑 —— 哪怕 KG 世代一动没动(codex 第 5 轮报的那一条)。

    ⚠ 与簇无关的那份**不能**被簇世代拖下水:否则每一次纯合并写入都要白付一趟
    836 万边的全表扫,而它的输入一个字节都没变。
    """
    ledger = _ledger({kind: 7 for kind in ARTIFACT_KINDS}, cluster_seq=3)
    assert analysis_ledger_is_current(ledger, 7, 3, has_boards=True)
    assert not analysis_ledger_is_current(ledger, 7, 4, has_boards=True)

    only_provenance = {ARTIFACT_RELATION_PROVENANCE: ledger[ARTIFACT_RELATION_PROVENANCE]}
    assert analysis_ledger_is_current(
        only_provenance, 7, 999, has_boards=False
    ) is (required_artifact_kinds(has_boards=False) <= {ARTIFACT_RELATION_PROVENANCE})
    # 直接对那一份问判据:簇世代再怎么漂,它都新鲜。
    assert artifact_is_current(
        ARTIFACT_RELATION_PROVENANCE,
        7,
        ledger[ARTIFACT_RELATION_PROVENANCE]["payload"],
        seq=7,
        cluster_seq=999,
    )
    # 依赖合并结果的那几份没盖戳(修复之前写下的行)= 不新鲜,而不是默认新鲜。
    unstamped = {
        ARTIFACT_CLUSTER_HISTOGRAM: {
            "kg_mutation_seq": 7, "payload": {"level": 0}, "created_at": "x",
        }
    }
    assert not artifact_is_current(
        ARTIFACT_CLUSTER_HISTOGRAM, 7, unstamped[ARTIFACT_CLUSTER_HISTOGRAM]["payload"],
        seq=7, cluster_seq=0,
    )


def test_analysis_ledger_without_boards_does_not_require_source_profiles():
    """零板块的库合法地没有来源画像;要求它存在会让这类库每次调用都重跑全部重活。"""
    without_profiles = _ledger({
        kind: 7 for kind in ARTIFACT_KINDS if kind != ARTIFACT_SOURCE_PROFILES
    })
    assert analysis_ledger_is_current(without_profiles, 7, 0, has_boards=False)
    assert not analysis_ledger_is_current(without_profiles, 7, 0, has_boards=True)


def test_reusable_payloads_only_carry_cluster_independent_kinds_at_the_same_seq():
    """复用只覆盖与簇无关、且 KG 世代未动的那几份 —— 判据与闸赖以成立的是同一条。"""
    ledger = _ledger({kind: 7 for kind in ARTIFACT_KINDS}, cluster_seq=3)
    assert set(reusable_artifact_payloads(ledger, 7)) == {ARTIFACT_RELATION_PROVENANCE}
    # KG 世代动了 → 输入可能变了 → 一份都不复用。
    assert reusable_artifact_payloads(ledger, 8) == {}


# ------------------------------------------------------------- 端到端


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _claim(local_id: str) -> dict:
    return {"local_id": local_id, "object_type": "claim",
            "payload": {"name": local_id, "section_path": "1"}, "evidence": []}


def _rel(source: str, target: str) -> dict:
    return {"source_local_id": source, "target_local_id": target,
            "edge_type": "supports", "evidence": []}


def _add_sources(repo, notebook_id: str, *source_ids: str) -> None:
    """`knowledge_relations.source_id` 有外键指向 sources,所以夹具必须建真来源行。"""
    with repo._write() as db:
        db.executemany(
            "INSERT INTO sources "
            "(id, notebook_id, title, source_type, status, parse_status, "
            " created_at, updated_at) "
            "VALUES (?,?,?, 'markdown', 'extracted', 'parsed', "
            "        '2026-01-01', '2026-01-01')",
            [(sid, notebook_id, sid) for sid in source_ids],
        )


def _seed(repo) -> str:
    """两个不等大的板块 + 一条跨板块边 + 三种「不该进画像」的对象。

    板块大小刻意不同(4 vs 3):头部板块集合的阈值是总成员的 50%,等大时哪个板块进头部
    取决于 id 的字典序,而 id 是随机铸的 —— 断言会变成掷骰子。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a", "src-b", "src-c", "src-d")
    # 大板块(4 个节点)全部来自 src-a
    repo.store_kg(
        notebook.id, "src-a",
        [_claim(name) for name in ("A", "B", "C", "G")],
        [_rel("A", "B"), _rel("B", "C"), _rel("A", "C"),
         _rel("C", "G"), _rel("A", "G")],
    )
    # 小板块(3 个节点)全部来自 src-b
    repo.store_kg(
        notebook.id, "src-b",
        [_claim(name) for name in ("D", "E", "F")],
        [_rel("D", "E"), _rel("E", "F"), _rel("D", "F")],
    )
    # 孤立对象:有来源、但不在图里 → n_graph_objects=0 的极端关联稀疏来源
    repo.store_kg(notebook.id, "src-c", [_claim("Z")], [])
    # 不挂来源的对象:共享同一个空 source_id,绝不能被算成一个「空来源」
    repo.store_kg(notebook.id, None, [_claim("N")], [])
    # 一条跨板块边(C 属大板块、D 属小板块)。store_kg 的 local_id 只在单次调用内有效,
    # 所以这条边直接按库内 id 写。
    with repo._connect() as db:
        ids = {
            json.loads(row["payload"])["name"]: row["id"]
            for row in db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
                (notebook.id,),
            )
        }
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id, notebook_id, source_id, source_object_id, target_object_id, "
            " edge_type, evidence, created_at) "
            "VALUES ('rel-cross',?, 'src-a', ?, ?, 'supports', '[]', '2026-01-01')",
            (notebook.id, ids["C"], ids["D"]),
        )
        # 一个 deprecated 对象:口径要求它不进 n_objects。
        db.execute(
            "UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
            (ids["Z"],),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, payload, evidence, source_id, "
            " created_at, updated_at) "
            "VALUES ('ko-live-c', ?, 'claim', 'approved', '{\"name\":\"Z2\"}', '[]', "
            "        'src-c', '2026-01-01', '2026-01-01')",
            (notebook.id,),
        )
    return notebook.id


def _artifacts(repo, notebook_id: str) -> dict:
    with repo._connect() as db:
        return {
            row["kind"]: {
                "seq": int(row["kg_mutation_seq"]),
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in db.execute(
                "SELECT kind, kg_mutation_seq, payload, created_at "
                "FROM kg_analysis_artifacts WHERE notebook_id=?", (notebook_id,),
            )
        }


def _edges(repo, notebook_id: str) -> list:
    with repo._connect() as db:
        return [
            (row["src_community_id"], row["dst_community_id"], int(row["weight"]))
            for row in db.execute(
                "SELECT src_community_id, dst_community_id, weight "
                "FROM kg_community_edges WHERE notebook_id=? "
                "ORDER BY src_community_id, dst_community_id", (notebook_id,),
            )
        ]


def _profiles(repo, notebook_id: str) -> dict:
    with repo._connect() as db:
        return {
            row["source_id"]: (
                int(row["n_objects"]), int(row["n_graph_objects"]),
                row["top_community_id"], float(row["top_share"]),
                int(row["community_spread"]), float(row["mainstream_share"]),
            )
            for row in db.execute(
                "SELECT * FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,)
            )
        }


def _boards(repo, notebook_id: str) -> dict:
    with repo._connect() as db:
        return {
            row["id"]: int(row["size"])
            for row in db.execute(
                "SELECT id, size FROM communities WHERE notebook_id=? AND level=0",
                (notebook_id,),
            )
        }


def test_rebuild_writes_every_artifact_kind_under_one_version_stamp(repo):
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2

    artifacts = _artifacts(repo, notebook_id)
    assert set(artifacts) == set(ARTIFACT_KINDS)
    with repo._connect() as db:
        state = db.execute(
            "SELECT kg_mutation_seq, community_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    # 设计 §3.3:每一份产物都要能自证建于哪个 kg_mutation_seq,而且五份必须同一批。
    assert {entry["seq"] for entry in artifacts.values()} == {
        int(state["kg_mutation_seq"])
    }
    assert int(state["community_seq"]) == int(state["kg_mutation_seq"])


def test_cross_board_edges_fold_the_one_bridge(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)

    boards = _boards(repo, notebook_id)
    assert sorted(boards.values()) == [3, 4]
    big = next(cid for cid, size in boards.items() if size == 4)
    small = next(cid for cid, size in boards.items() if size == 3)
    expected = (min(big, small), max(big, small), 1)
    assert _edges(repo, notebook_id) == [expected]

    payload = _artifacts(repo, notebook_id)[ARTIFACT_COMMUNITY_EDGES]["payload"]
    assert payload["edges"] == 1
    assert payload["edges_total"] == 1
    assert payload["truncated"] is False
    assert payload["edge_limit"] == MAX_PERSISTED_COMMUNITY_EDGES
    assert payload["cross_weight"] == 1
    assert payload["communities"] == 2
    # 板块内边:大板块 5 条 + 小板块 3 条。
    assert payload["intra_weight"] == 8


def test_source_profiles_apply_the_documented_field_semantics(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)

    boards = _boards(repo, notebook_id)
    big = next(cid for cid, size in boards.items() if size == 4)
    small = next(cid for cid, size in boards.items() if size == 3)
    profiles = _profiles(repo, notebook_id)

    # 不挂来源的对象绝不能凝成一个「空来源」的画像。
    assert set(profiles) == {"src-a", "src-b", "src-c"}
    # src-a:4 个对象全在大板块。
    assert profiles["src-a"] == (4, 4, big, 1.0, 1, 1.0)
    # src-b:3 个对象全在小板块 —— 头部集合只有大板块(4/7 >= 50%),所以它的
    # mainstream_share 是 0.0。这正是「关联稀疏的来源」要暴露的形态。
    assert profiles["src-b"] == (3, 3, small, 1.0, 1, 0.0)
    # src-c:一个 deprecated(不计)+ 一个可用但不在图里的对象。
    assert profiles["src-c"] == (1, 0, "", 0.0, 0, 0.0)

    payload = _artifacts(repo, notebook_id)[ARTIFACT_SOURCE_PROFILES]["payload"]
    assert payload["sources"] == 3
    assert payload["mainstream_coverage"] == MAINSTREAM_COVERAGE
    assert (payload["head_communities"], payload["head_members"],
            payload["total_members"]) == (1, 4, 7)


def test_source_profiles_drop_hidden_sources_but_keep_orphan_references(repo):
    """两类**长得很像、处置必须相反**的来源,一条测试同时钉住。

      · 合成来源(`source_type IN ('memory','knowhow')`)→ **排除**。产品其余各处一律
        把它们当隐藏源;它们的 `title` 是用户内容(knowhow 的是表名、Memory 的是那条
        记忆的抬头),而且天生只连自己那一小片,会把「最不连通的来源」这张榜的头部占满。
      · 孤儿来源(`source_id` 在 `sources` 里没有对应行,历史清理会留下)→ **保留**,
        并在读侧标 `source_missing`。那是本视图**有意**的诊断能力。

    ⚠ 一条测试同时断两边是刻意的:这两件事由**同一个谓词**决定,而最容易犯的错正是
    「修好一边、顺手把另一边弄坏」—— 把 `NOT EXISTS` 写成 `JOIN sources` 或
    `LEFT JOIN … WHERE s.source_type NOT IN (…)`(NULL 让谓词为 NULL)就会把孤儿一起
    吞掉,而那两种写法对「排除合成来源」这一半是完全正确的。分成两条测试的话,
    先跑的那条会绿,读的人不一定注意到另一条在报什么。

    账本 payload 的 `sources` 一并断:排除发生在预计算,所以 `len(profiles)` 与读侧的
    分页 `total` 必须还是同一个口径 —— 只在读侧过滤的修法会让这两个数字分岔。
    """
    notebook_id = _seed(repo)
    with repo._write() as db:
        db.executemany(
            "INSERT INTO sources "
            "(id, notebook_id, title, source_type, status, parse_status, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?, 'extracted', 'parsed', '2026-01-01', '2026-01-01')",
            [
                ("src-knowhow", notebook_id, "季度奖金核算口径", "knowhow"),
                ("src-memory", notebook_id, "老板不喜欢周一开会", "memory"),
            ],
        )
        # 隐藏合成来源的对象:形态与 src-c 那条一模一样(可用、有来源、不在图里),
        # 唯一的差别就是来源类型 —— 所以排除只可能来自类型谓词。
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, payload, evidence, source_id, "
            " created_at, updated_at) "
            "VALUES (?,?, 'claim', 'approved', ?, '[]', ?, "
            "        '2026-01-01', '2026-01-01')",
            [
                ("ko-knowhow", notebook_id, '{"name":"KH"}', "src-knowhow"),
                ("ko-memory", notebook_id, '{"name":"MEM"}', "src-memory"),
                # 孤儿:src-deleted 在 sources 里根本没有行。
                ("ko-orphan", notebook_id, '{"name":"ORPH"}', "src-deleted"),
            ],
        )
    repo.rebuild_communities(notebook_id, force=True)

    profiles = _profiles(repo, notebook_id)
    assert "src-knowhow" not in profiles, (
        "knowhow 投影的来源进了来源画像 —— 它的标题是用户的表名"
    )
    assert "src-memory" not in profiles, "Memory 的合成来源进了来源画像"
    # 孤儿必须还在,而且带完整数据(1 个可用对象、不在图里)。
    assert profiles["src-deleted"] == (1, 0, "", 0.0, 0, 0.0), (
        "孤儿来源被一起排除了 —— 一个有意的诊断信号变成了静默丢弃"
    )
    assert set(profiles) == {"src-a", "src-b", "src-c", "src-deleted"}

    payload = _artifacts(repo, notebook_id)[ARTIFACT_SOURCE_PROFILES]["payload"]
    assert payload["sources"] == 4

    # 读侧:孤儿要能与「有来源但没标题」分得开。
    with repo._connect() as db:
        total, rows = repo._runtime.unified_kg.kg_source_profile_page(
            db, notebook_id, limit=50, offset=0
        )
    by_id = {row["source_id"]: row for row in rows}
    assert total == 4 and set(by_id) == set(profiles)
    assert by_id["src-deleted"]["source_missing"] is True
    assert by_id["src-deleted"]["title"] == ""
    assert by_id["src-a"]["source_missing"] is False
    # 泄漏面的直接断言:隐藏来源的标题一个字都不能出现在响应里。
    assert not {row["title"] for row in rows} & {
        "季度奖金核算口径", "老板不喜欢周一开会"
    }


def test_statistic_snapshots_round_trip_the_query_layer_payloads(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    artifacts = _artifacts(repo, notebook_id)
    store = repo._runtime.unified_kg

    # 依赖合并结果的两份多带一个簇世代的戳(`stamp_cluster_seq` 盖的),其余逐字相同。
    def _unstamped(kind: str) -> dict:
        return {
            key: value for key, value in artifacts[kind]["payload"].items()
            if key != CLUSTER_SEQ_PAYLOAD_KEY
        }

    assert _unstamped(ARTIFACT_CLUSTER_HISTOGRAM) == json.loads(
        json.dumps(store.cluster_size_histogram(notebook_id))
    )
    assert _unstamped(ARTIFACT_LARGEST_CLUSTERS) == json.loads(
        json.dumps(store.largest_clusters(notebook_id, 20))
    )
    # 与合并无关的那份**不盖**戳:它逐字就是查询层的载荷。
    assert artifacts[ARTIFACT_RELATION_PROVENANCE]["payload"] == json.loads(
        json.dumps(store.relation_provenance_counts(notebook_id))
    )
    # 载荷结构必须仍能按类型分组还原(T1 的分组直方图)。
    groups = artifacts[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]["by_object_type"]
    assert [group["object_type"] for group in groups] == [
        "concept", "claim", "formula", "procedure", "other"
    ]


def test_a_single_board_still_gets_an_edge_ledger_row(repo):
    """空产物与缺失产物必须可区分:0 条跨板块边照样要有账本行。"""
    notebook = repo.create_notebook(NotebookCreate(name="one-board"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a")
    repo.store_kg(
        notebook.id, "src-a", [_claim(n) for n in ("A", "B", "C")],
        [_rel("A", "B"), _rel("B", "C"), _rel("A", "C")],
    )
    assert repo.rebuild_communities(notebook.id) == 1
    assert _edges(repo, notebook.id) == []
    ledger = _artifacts(repo, notebook.id)
    assert ledger[ARTIFACT_COMMUNITY_EDGES]["payload"]["edges"] == 0
    assert ledger[ARTIFACT_COMMUNITY_EDGES]["seq"] >= 1


PRODUCT_TABLES = ("kg_community_edges", "kg_source_profiles", "kg_analysis_artifacts")


ANALYSIS_SNAPSHOTS = (
    "cluster_size_histogram", "largest_clusters", "relation_provenance_counts",
)


def test_analysis_snapshots_refuse_to_run_inside_a_write_transaction(repo):
    """把全表级只读聚合放进写事务里必须**当场炸**,不是静默读到旧数据。

    两个危害:读的是提交前的库(过时报告,零报错),以及 `SqliteDatabase.write()` 的
    **进程级写锁**会被按住一整趟全表扫(同形状数据点:835 万边冷扫 39 分钟,期间全库
    写入排队)。
    """
    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    with repo._write():
        for name in ANALYSIS_SNAPSHOTS:
            with pytest.raises(RuntimeError, match="写事务"):
                getattr(store, name)(notebook_id)
        with pytest.raises(RuntimeError, match="写事务"):
            store.community_overview(notebook_id)


def test_precompute_takes_every_snapshot_outside_any_write_transaction(repo, monkeypatch):
    """**语义**守卫:三条全表重活被调用的那一刻,本线程的写深度必须是 0。

    为什么不能只靠下面那条形状守卫(「哪个写事务碰过产物表」):这三条查询一张产物表
    都不碰,在 SQLite 上还跑在**另一条**连接上,所以把它们从 `_write()` 外面搬到里面
    时,那条断言完全看不见 —— 评审实测过这个移动变异,25 条测试全绿。这里改成直接记录
    调用时刻的 `write_depth`,移动变异会当场把它顶成 1。
    """
    notebook_id = _seed(repo)
    database = repo._runtime.database
    store = repo._runtime.unified_kg
    depths: dict[str, list[int]] = {}

    def instrument(name):
        original = getattr(store, name)

        def wrapper(*args, **kwargs):
            depths.setdefault(name, []).append(
                getattr(database._local, "write_depth", 0)
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(store, name, wrapper)

    for name in (*ANALYSIS_SNAPSHOTS, "source_community_counts"):
        instrument(name)
    repo.rebuild_communities(notebook_id)

    assert set(depths) == {*ANALYSIS_SNAPSHOTS, "source_community_counts"}, (
        f"仪器没装上或有查询没被调用:{sorted(depths)}"
    )
    assert all(depth == 0 for calls in depths.values() for depth in calls), (
        f"全表级重活在写事务内被调用(write_depth != 0):{depths}"
    )


BOARD_TABLE = "communities"

# ⚠ 表名**不能**用裸子串匹配:trace 回调拿到的是参数已展开的 SQL,而账本 payload 里
# 恰好有一个 `"communities"` 字段(板块个数)—— 于是落库事务会被误判成板块重铸事务,
# 两类混成一类、断言全空。只认 FROM / INTO / UPDATE 后面的那个标识符。
_DML_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


def _trace_product_table_transactions(repo, monkeypatch) -> list:
    """把每个写事务**写过**的表名记下来,供下面两条形态守卫共用。

    返回一个 list,每个元素是**一个写事务**碰过的表名集合(按开启顺序)。
    """
    database = repo._runtime.database
    original_write = database.write
    per_transaction: list[set] = []
    watched = {*PRODUCT_TABLES, BOARD_TABLE}

    @contextmanager
    def tracing_write(**kwargs):
        with original_write(**kwargs) as db:
            touched: set = set()
            per_transaction.append(touched)

            def trace(statement: str) -> None:
                touched.update(watched.intersection(_DML_TABLE.findall(statement)))

            db.set_trace_callback(trace)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(database, "write", tracing_write)
    return per_transaction


def test_all_three_product_tables_are_written_in_one_transaction(repo, monkeypatch):
    """设计 §3.3 的**结构性**证据:三张产物表必须落在同一个写事务里。

    下面的 `test_precompute_is_all_or_nothing` 证明「整体失败会整体回滚」,但它抓不到
    「拆成两个事务、第一个已提交、第二个才炸」—— 那种形态下异常仍在第二个 `with` 内,
    第二个事务照样回滚,而**第一个已经落库了**。所以这里直接钉住形态本身:整个
    rebuild 期间,只有**一个**写事务**落库**产物,而且它把三张表全碰了。

    ⚠ 碰产物表的写事务现在有**两个**,不是一个:板块重铸那一个也会碰(它同事务作废
    依赖板块的两份产物,见 `discard_board_dependent_kg_analysis_artifacts`)。两者靠
    「有没有同时碰 `communities`」区分 —— 落库事务绝不碰板块表,重铸事务必碰。
    把落库拆成两个事务、或者把作废挪出重铸事务,这条都会报红。

    ⚠ 这条**只**管产物表的写。它对「重活被搬进写事务」是瞎的(那三条查询不碰产物表),
    那一档由上面两条守卫负责。
    """
    notebook_id = _seed(repo)
    per_transaction = _trace_product_table_transactions(repo, monkeypatch)
    repo.rebuild_communities(notebook_id)

    products = set(PRODUCT_TABLES)
    touching = [touched for touched in per_transaction if touched & products]
    persisting = [touched for touched in touching if BOARD_TABLE not in touched]
    assert len(persisting) == 1, (
        f"产物被 {len(persisting)} 个写事务落库 —— 一次预计算必须是原子的:{touching}"
    )
    assert persisting[0] == products
    # 另一类只能是板块重铸那一个,而且它必须把两张明细表 + 账本一起作废。
    minting = [touched for touched in touching if BOARD_TABLE in touched]
    assert len(minting) == 1, f"碰产物表的写事务不止两类:{touching}"
    assert minting[0] == products | {BOARD_TABLE}


def test_reminting_boards_discards_the_board_dependent_products_in_the_same_transaction(
    repo, monkeypatch
):
    """P2:板块 id 一被重铸,依赖板块的两份产物必须**同事务**作废。

    为什么必须同事务、而不是「反正预计算马上就整批重写了」:预计算是一段重活,中间
    可能失败、被取消、或者进程崩。任何一种都会把**悬空**的产物留在库里 —— 明细行指着
    已经不存在的板块 id,而 T3 的记忆化签名(state 的 seq + 账本行的 seq/created_at)
    对「同一个 `kg_mutation_seq` 上的 force 重铸」一个字段都不会变,已预热的缓存会
    **无限期**继续吐上一套板块。

    这条钉的是**形态**(作废与重铸同事务),不是结果 —— 结果由
    `test_a_failed_precompute_after_a_force_rebuild_stops_serving_the_old_boards` 钉。
    把 `discard_board_dependent_kg_analysis_artifacts` 挪进 `_precompute_kg_analysis`
    的落库事务里(一个看起来更「整洁」的位置),这条会当场报红。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    # KG 变动 → 下一次调用真的重建图(而不是走「只补账本」)。
    repo.store_kg(notebook_id, "src-d", [_claim("Q")], [])

    per_transaction = _trace_product_table_transactions(repo, monkeypatch)
    store = repo._runtime.unified_kg
    # 预计算整段失败:如果作废被挪进了落库事务,它会跟着一起回滚。
    monkeypatch.setattr(
        store, "cluster_size_histogram",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    repo.rebuild_communities(notebook_id)

    minting = [t for t in per_transaction if BOARD_TABLE in t]
    assert len(minting) == 1, f"板块重铸不在恰好一个写事务里:{per_transaction}"
    assert minting[0] == set(PRODUCT_TABLES) | {BOARD_TABLE}, (
        "板块重铸事务没有同时作废依赖板块的产物"
    )
    # 落库失败了,所以库里剩下的必须是「作废已生效」的状态。
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}
    assert set(_artifacts(repo, notebook_id)) == {
        ARTIFACT_CLUSTER_HISTOGRAM, ARTIFACT_LARGEST_CLUSTERS,
        ARTIFACT_RELATION_PROVENANCE,
    }, "与板块无关的三条统计快照被连坐删掉了 —— 它们仍是可读的陈旧快照"


def test_precompute_is_all_or_nothing(repo, monkeypatch):
    """设计 §3.3:一次预计算要么整批可见、要么完全不可见。

    先成功产出一批,再让第二批在**写完之后**炸 —— 落库事务必须整个回滚,不允许出现
    「跨板块边是新的、来源画像还是旧的」这种隐蔽矛盾。

    ⚠ 判据里的「旧的」在两张明细表上是**空**,不是上一批的行:force 重建已经在重铸
    板块的那一刻把依赖板块的产物作废了(见
    `test_reminting_boards_discards_the_board_dependent_products_in_the_same_transaction`)。
    这不削弱本条 —— 恰恰相反:落库若被拆成两个事务、第一个已提交,`kg_community_edges`
    就会是**非空**,这条立刻报红。与板块无关的三条统计快照仍必须原样停在第一批的戳上。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    first_artifacts = _artifacts(repo, notebook_id)
    assert _edges(repo, notebook_id) and _profiles(repo, notebook_id)
    assert set(first_artifacts) == set(ARTIFACT_KINDS)
    first_seq = {entry["seq"] for entry in first_artifacts.values()}

    # KG 变动 → seq 闸不再短路
    repo.store_kg(notebook_id, "src-d", [_claim("Q")], [])
    store = repo._runtime.unified_kg
    original = store.replace_kg_analysis_artifacts

    def explode(db, *args, **kwargs):
        original(db, *args, **kwargs)          # 先真的写进去
        raise RuntimeError("injected artifact write failure")

    monkeypatch.setattr(store, "replace_kg_analysis_artifacts", explode)
    repo.rebuild_communities(notebook_id, force=True)

    # 落库整段回滚 —— 半份产物一行都不许留下。
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}
    assert "src-d" not in _profiles(repo, notebook_id)
    survivors = _artifacts(repo, notebook_id)
    assert set(survivors) == {
        ARTIFACT_CLUSTER_HISTOGRAM, ARTIFACT_LARGEST_CLUSTERS,
        ARTIFACT_RELATION_PROVENANCE,
    }
    # 版本戳也必须停在第一批:否则报告会宣称自己是新的。
    assert {entry["seq"] for entry in survivors.values()} == first_seq


def test_a_failed_precompute_after_a_force_rebuild_stops_serving_the_old_boards(
    repo, monkeypatch
):
    """P2 的**结果**守卫:force 重建成功 + 预计算失败,报告不得再吐上一套板块。

    这是评审报的那个窗口:板块 id 与成员都换了,而缓存签名(state 的全部 seq/dirty +
    账本每行的 seq/created_at)一个字段都没变 —— `force=True` 在**同一个**
    `kg_mutation_seq` 上重铸,账本又因为预计算失败而原封不动。已预热的缓存会
    **无限期**返回上一套板块,直到 LRU 淘汰或进程重启,而端点自称读的是 live 快照。

    ⚠ 必须先 `overview()` 预热一次,否则测的是「冷缓存」——那本来就不会错。
    """
    from app.services.kg_analysis import KgAnalysisService

    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    service = KgAnalysisService(
        database=repo._runtime.database,
        unified_kg=repo._runtime.unified_kg,
        now=lambda: "2026-01-03T00:00:00",
    )
    warmed = sorted(
        board["id"] for board in service.overview(notebook_id).boards.payload["communities"]
    )
    assert warmed == sorted(_boards(repo, notebook_id))

    monkeypatch.setattr(
        repo._runtime.unified_kg, "source_community_counts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    repo.rebuild_communities(notebook_id, force=True)
    monkeypatch.undo()

    live = sorted(_boards(repo, notebook_id))
    assert live != warmed, "force 重建没有重铸板块 id —— 本条的前提不成立"
    served = sorted(
        board["id"] for board in service.overview(notebook_id).boards.payload["communities"]
    )
    assert served == live, f"缓存吐出了已经不存在的板块:{served} != {live}"


def test_precompute_failure_is_reported_and_does_not_break_the_rebuild(repo, monkeypatch):
    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(
        store, "source_community_counts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    emitted = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", emitted.append)

    # 核心路径不受影响:社区照建、返回值照常。
    assert repo.rebuild_communities(notebook_id) == 2
    kinds = [event["kind"] for event in emitted]
    assert "kg_analysis_precompute_failed" in kinds
    assert "communities_rebuilt" in kinds
    failure = next(e for e in emitted if e["kind"] == "kg_analysis_precompute_failed")
    assert "RuntimeError" in failure["error"]
    # 产物缺失,不是半份。
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}


def test_precompute_does_not_swallow_a_user_abort(repo, monkeypatch):
    """取消不是失败:`KgBuildAborted` 必须冒泡,否则「中止」看起来像成功了。"""
    from app.services.kg.run_control import KgBuildAborted, KgBuildFailure

    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    aborted = KgBuildAborted(KgBuildFailure(code="cancelled", user_message="已中止"))

    def abort(*args, **kwargs):
        raise aborted

    monkeypatch.setattr(store, "source_community_counts", abort)
    with pytest.raises(KgBuildAborted):
        repo.rebuild_communities(notebook_id)


def test_disabled_community_layer_writes_no_artifacts(repo):
    notebook_id = _seed(repo)
    repo.settings.community_layer_enabled = False
    assert repo.rebuild_communities(notebook_id) == 0
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}


def test_refused_large_graph_build_writes_no_artifacts(repo, monkeypatch):
    """大库守卫拒建社区时,产物必须**缺失**,而不是写出一份没有板块的画像。"""
    import sys

    notebook_id = _seed(repo)
    monkeypatch.setitem(sys.modules, "igraph", None)   # networkx 回退
    monkeypatch.setattr(repo, "notebook_copy_stats", lambda _nb: {"copyable": False})
    monkeypatch.setattr(
        repo._runtime.scale_artifacts, "load", lambda _nb, allow_stale=False: None
    )
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    assert repo.rebuild_communities(notebook_id) == 0
    assert any(e.get("kind") == "community_build_refused" for e in events)
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}


def test_version_gate_does_not_recompute_when_the_kg_is_unchanged(repo, monkeypatch):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    created_at = {
        kind: entry["created_at"]
        for kind, entry in _artifacts(repo, notebook_id).items()
    }
    calls = []
    store = repo._runtime.unified_kg
    original = store.source_community_counts
    monkeypatch.setattr(
        store, "source_community_counts",
        lambda *args, **kwargs: (calls.append(args), original(*args, **kwargs))[1],
    )
    repo.rebuild_communities(notebook_id)
    assert calls == []
    assert {
        kind: entry["created_at"]
        for kind, entry in _artifacts(repo, notebook_id).items()
    } == created_at


def _merge_only_write(repo, notebook_id: str) -> int:
    """一次**只动合并结果**的写入 —— `kg_mutation_seq` 刻意不动(生产语义)。

    走的是真的写路径 `append_clusters`(facade 的公开入口,`incremental_fuse_source`
    也用它),不是手工 UPDATE 计数器:被测的正是「簇写路径不抬 KG 世代」这条真实行为。
    返回写入的簇成员行数。
    """
    with repo._connect() as db:
        object_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=? "
                "AND status != 'deprecated' ORDER BY id",
                (notebook_id,),
            )
        ]
    return repo.append_clusters(
        notebook_id,
        [{"canonical_id": "K-merged", "canonical_name": "merged",
          "member_object_id": object_id} for object_id in object_ids[:3]],
        object_type="concept",
    )


def _seqs(repo, notebook_id: str) -> tuple:
    with repo._connect() as db:
        row = db.execute(
            "SELECT kg_mutation_seq, cluster_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    return int(row["kg_mutation_seq"]), int(row["cluster_mutation_seq"])


def test_a_merge_only_write_does_not_move_the_kg_generation(repo):
    """本次修复的**前提事实**:合并写入只抬簇世代,KG 世代一动没动。

    钉住它是因为整条修复都建立在这上面 —— 如果哪天簇写路径开始 bump
    `kg_mutation_seq`,下面那几条守卫会全部变成恒真的空守卫而没人发现。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    kg_before, cluster_before = _seqs(repo, notebook_id)
    assert _merge_only_write(repo, notebook_id) == 3
    kg_after, cluster_after = _seqs(repo, notebook_id)
    assert kg_after == kg_before, "簇写路径不该抬 KG 世代"
    assert cluster_after == cluster_before + 1


def test_a_merge_only_write_makes_the_ledger_stale_and_a_plain_rebuild_heals_it(repo):
    """codex 第 5 轮:纯合并写入之后闸必须判「不新鲜」,而且非 force 调用就能补上。

    修复之前:闸只比 `kg_mutation_seq`,合并写入之后它直接短路 —— 簇大小直方图与最大
    簇榜单(**从 `concept_clusters` 算出来**)永远停在旧内容上,而账本的戳一动没动,
    读侧照报「与当前一致」。这里用**产物内容真的变了**来证明它确实重算过,不是只把
    戳改了一下。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    before = _artifacts(repo, notebook_id)
    _merge_only_write(repo, notebook_id)

    assert repo.rebuild_communities(notebook_id) == 2      # 刻意不带 force

    after = _artifacts(repo, notebook_id)
    kg_seq, cluster_seq = _seqs(repo, notebook_id)
    # 两条统计快照的内容真的变了(多了一个 3 成员的簇),不是只换了个戳。
    assert (before[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]["clusters"]
            < after[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]["clusters"])
    # 依赖合并结果的四份都盖上了当前簇世代;与合并无关的那份仍然不带这个戳。
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS):
        assert after[kind]["payload"][CLUSTER_SEQ_PAYLOAD_KEY] == cluster_seq
        assert after[kind]["seq"] == kg_seq
    assert CLUSTER_SEQ_PAYLOAD_KEY not in after[ARTIFACT_RELATION_PROVENANCE]["payload"]

    # 补完之后闸重新短路:再调一次不写任何东西(否则每次打开视图都在重算)。
    settled = {kind: entry["created_at"] for kind, entry in after.items()}
    repo.rebuild_communities(notebook_id)
    assert {
        kind: entry["created_at"]
        for kind, entry in _artifacts(repo, notebook_id).items()
    } == settled


def test_a_merge_only_backfill_does_not_rescan_the_relation_table(repo, monkeypatch):
    """成本主张:纯合并写入触发的补账本**不重扫关系表**。

    `relation_provenance` 只读 `knowledge_relations` + 两次端点探查,一个字都没提
    `concept_clusters`,而 KG 世代没动 ⟹ 它的输入一个字节都没变。它同时是五份里最贵的
    一份(生产 836 万边、每行两次随机 PK 探查)。一刀切作废等于每次合并都白付一趟。

    反向也钉住:`force=True` 必须重扫 —— 那是抽取口径/代码改了之后唯一的人工恢复手段,
    在它上面省这趟等于宣布旧载荷永远换不掉。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    baseline = _artifacts(repo, notebook_id)[ARTIFACT_RELATION_PROVENANCE]["payload"]

    store = repo._runtime.unified_kg
    calls: list[int] = []
    original = store.relation_provenance_counts
    monkeypatch.setattr(
        store, "relation_provenance_counts",
        lambda *args, **kwargs: (calls.append(1), original(*args, **kwargs))[1],
    )

    _merge_only_write(repo, notebook_id)
    assert repo.rebuild_communities(notebook_id) == 2
    assert calls == [], "纯合并写入的补账本不该重扫 knowledge_relations"
    # 复用的必须是**逐字相同**的载荷,不是一个空壳。
    assert (_artifacts(repo, notebook_id)[ARTIFACT_RELATION_PROVENANCE]["payload"]
            == baseline)

    repo.rebuild_communities(notebook_id, force=True)
    assert len(calls) == 1, "force 是人工恢复手段,必须真的重扫"


def test_replace_artifacts_consumes_edges_and_profiles_as_one_shot_iterators(repo):
    """落库入口按**可迭代**消费,不得要求 Sequence(len / 下标 / 二次遍历)。

    契约意义:落库这一刻 `rebuild_communities` 的栈帧上还压着整张 `ew`,store 再把行
    物化成一份完整列表就是在峰值上叠峰值。一次性迭代器能跑通,就说明它没有回头再读一遍。
    """
    notebook = repo.create_notebook(NotebookCreate(name="stream"))
    _add_sources(repo, notebook.id, "src-a")
    store = repo._runtime.unified_kg
    payloads = _full_payloads(**{ARTIFACT_COMMUNITY_EDGES: {"level": 0, "communities": 1}})
    edges = iter([("cm-a", "cm-b", 3)])
    profiles = iter([("src-a", 5, 4, "cm-a", 1.0, 1, 1.0)])
    with repo._write() as db:
        store.replace_kg_analysis_artifacts(
            db, notebook.id, 9, edges, profiles, payloads, "2026-01-01T00:00:00"
        )
    assert next(edges, None) is None and next(profiles, None) is None
    assert _edges(repo, notebook.id) == [("cm-a", "cm-b", 3)]
    assert set(_profiles(repo, notebook.id)) == {"src-a"}


def _wipe_ledger(repo, notebook_id: str) -> None:
    """把产物抹成「本特性上线前」的样子:社区图完好、账本一片空白。"""
    with repo._write() as db:
        db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (notebook_id,))
        db.execute("DELETE FROM kg_community_edges WHERE notebook_id=?", (notebook_id,))
        db.execute("DELETE FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,))


def test_aligned_communities_with_an_empty_ledger_still_get_backfilled(repo):
    """B1:社区已按当前 seq 建好、账本为空时,**非 force** 的调用必须补出账本。

    这正是生产 base 库的形态:它在本特性上线**之前**就已经 rebuild 过,
    `community_seq == kg_mutation_seq`。若预计算跟着社区图的闸走,部署之后任何非 force
    调用都会在闸上直接返回,账本永远是空的 —— 用户只看得到「产物缺失」,唯一恢复手段是
    force 全量重建(836 万边 join + Louvain + 重写 171 万成员行)。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    expected_edges = _edges(repo, notebook_id)
    expected_profiles = _profiles(repo, notebook_id)
    boards_before = _boards(repo, notebook_id)
    _wipe_ledger(repo, notebook_id)
    assert _artifacts(repo, notebook_id) == {}

    assert repo.rebuild_communities(notebook_id) == 2       # 刻意不带 force

    artifacts = _artifacts(repo, notebook_id)
    assert set(artifacts) == set(ARTIFACT_KINDS)
    with repo._connect() as db:
        state = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,)).fetchone()
    assert {entry["seq"] for entry in artifacts.values()} == {
        int(state["kg_mutation_seq"])
    }
    # 补账本**不得**动社区图:板块 id 与规模必须原样(重铸 id 会让既有引用全部失效)。
    assert _boards(repo, notebook_id) == boards_before
    # 两条喂入路径产出逐字相同的产物。
    assert _edges(repo, notebook_id) == expected_edges
    assert _profiles(repo, notebook_id) == expected_profiles


def test_backfill_does_not_rerun_louvain_or_rewrite_the_membership_tables(repo, monkeypatch):
    """补账本必须跳过整个重建:不重跑 Louvain、不整表重写 communities/community_members。

    这一条是 B1 的**成本**主张,不是它的正确性主张 —— 没有它,「给预计算一个自己的闸」
    可以被实现成「闸没过就整个重来」,那和 force=True 一样贵。
    """
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    _wipe_ledger(repo, notebook_id)

    store = repo._runtime.unified_kg
    calls: list[str] = []
    for name in ("replace_communities", "set_community_seq"):
        monkeypatch.setattr(
            store, name,
            (lambda n: lambda *a, **k: calls.append(n))(name),
        )
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", events.append)

    assert repo.rebuild_communities(notebook_id) == 2
    assert calls == [], f"补账本却重写了社区表:{calls}"
    kinds = [event["kind"] for event in events]
    # 事件必须如实说「只补了账本」,不能伪装成一次图重建。
    assert "kg_analysis_backfilled" in kinds
    assert "communities_rebuilt" not in kinds
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_a_failed_precompute_heals_on_the_next_plain_rebuild(repo, monkeypatch):
    """预计算失败后**不需要** force 才能自愈:下一次普通调用就会补上。"""
    notebook_id = _seed(repo)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(
        store, "source_community_counts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert repo.rebuild_communities(notebook_id) == 2
    assert _artifacts(repo, notebook_id) == {}

    monkeypatch.undo()
    assert repo.rebuild_communities(notebook_id) == 2       # 刻意不带 force
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_a_library_without_boards_writes_no_source_profile_product(repo):
    """S4:一个板块都没有时,来源画像必须**缺失**,而不是写一张全 0 的表。

    对象有、关系没有的库算不出任何「主体板块」,每个来源的 mainstream_share 都会是 0.0。
    明细表单独看是在说「所有来源都关联稀疏」—— 一句谎话。产物缺失是诚实的。
    """
    notebook = repo.create_notebook(NotebookCreate(name="no-boards"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a")
    repo.store_kg(notebook.id, "src-a", [_claim(n) for n in ("A", "B", "C")], [])

    assert repo.rebuild_communities(notebook.id) == 0
    artifacts = _artifacts(repo, notebook.id)
    assert set(artifacts) == set(ARTIFACT_KINDS) - {ARTIFACT_SOURCE_PROFILES}
    assert _profiles(repo, notebook.id) == {}
    assert artifacts[ARTIFACT_COMMUNITY_EDGES]["payload"]["communities"] == 0
    # 三条统计快照与板块无关,照常产出(形状完整,不是缺一块)。
    assert "by_object_type" in artifacts[ARTIFACT_CLUSTER_HISTOGRAM]["payload"]
    assert "buckets" in artifacts[ARTIFACT_RELATION_PROVENANCE]["payload"]


# ------------------------------------------------- 两个闸的对称性(三个方向)
# 这道闸是**对称的**:放松一边就会紧另一边。它当初是为了修 B1(「已部署库永不回填」)
# 才加的;后来又发现它把「零板块」误判成「没建成」,让那类库每次刷新都重跑全部重活。
# 三个方向必须**同时**钉住,只测一头的守卫会在下一次调参时静默失效。

_HEAVY_WORK = (
    "community_graph_rows",        # canonical 边图:生产 836 万边的一次 join
    "cluster_size_histogram",      # 三条全表统计快照
    "largest_clusters",
    "relation_provenance_counts",
)


def _spy_heavy_work(repo, monkeypatch) -> list:
    """记录整趟 rebuild 里真正跑过的重活(闸有没有短路,看这个列表空不空)。"""
    store = repo._runtime.unified_kg
    calls: list[str] = []
    for name in _HEAVY_WORK:
        monkeypatch.setattr(
            store, name,
            (lambda n, original: lambda *a, **k: (
                calls.append(n), original(*a, **k)
            )[1])(name, getattr(store, name)),
        )
    return calls


def _no_board_notebook(repo) -> str:
    """对象有、关系没有 —— 所有连通块都小于 `community_min_size`,合法的零板块库。"""
    notebook = repo.create_notebook(NotebookCreate(name="no-boards"))
    repo.settings.community_min_size = 3
    _add_sources(repo, notebook.id, "src-a")
    repo.store_kg(notebook.id, "src-a", [_claim(n) for n in ("A", "B", "C")], [])
    return notebook.id


def test_a_zero_board_library_short_circuits_instead_of_recomputing_forever(
    repo, monkeypatch
):
    """方向一(零板块短路):板块数 0 是**合法终态**,不是「没建成」。

    拿板块数当「建过」的标记,这类库的两个闸就永远过不去:每一次「刷新图谱」都要重跑
    canonical 边图 + 三条全表统计快照,而结果永远是同一个零。生产量级上那是一次
    「分钟到小时」的空转(437 GB / 836 万边)。

    `has_boards` 同样必须传实际值:零板块的库合法地不写来源画像(S4),硬写 True 等于
    宣布这类库的账本永远不完整 —— 同一个空转,换一个入口。
    """
    notebook_id = _no_board_notebook(repo)
    assert repo.rebuild_communities(notebook_id) == 0
    before = _artifacts(repo, notebook_id)
    assert set(before) == set(ARTIFACT_KINDS) - {ARTIFACT_SOURCE_PROFILES}

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 0        # 刻意不带 force
    assert calls == [], f"零板块库又跑了一遍重活:{calls}"
    # 账本一行都没被重写(created_at 会变)。
    assert _artifacts(repo, notebook_id) == before


def test_a_stale_ledger_with_boards_still_backfills(repo, monkeypatch):
    """方向二(B1 回填):有板块 + 账本陈旧 → 必须真的补,不得被闸吞掉。

    这是这道闸最初要修的那件事(生产 base 库上线前就已 `community_seq ==
    kg_mutation_seq`)。方向一的修法**不得**让它复发,所以两条并排放。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    _wipe_ledger(repo, notebook_id)

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 2        # 刻意不带 force
    assert sorted(set(calls)) == sorted(_HEAVY_WORK), f"账本陈旧却没补:{calls}"
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_boards_with_a_missing_source_profile_row_still_backfill(repo, monkeypatch):
    """`has_boards` 必须来自**实际板块数**,不得从账本自己推导。

    从账本推(「来源画像行在不在」)是**循环判据**:这一行一丢,反而会被读成「这个库
    一个板块都没有,所以它合法缺席」,于是永远不补 —— 一个看起来完全合理、却让缺席
    自我合理化的实现。这条把判据的来源钉死在库里的板块数上。
    """
    notebook_id = _seed(repo)
    assert repo.rebuild_communities(notebook_id) == 2
    with repo._write() as db:
        db.execute(
            "DELETE FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
            (notebook_id, ARTIFACT_SOURCE_PROFILES),
        )
        db.execute(
            "DELETE FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,)
        )

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 2        # 刻意不带 force
    assert calls, "有板块却把来源画像的缺席判成合法 —— 它永远补不回来了"
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)
    assert _profiles(repo, notebook_id)


def test_a_never_built_library_really_builds(repo, monkeypatch):
    """方向三(从未建过):`community_seq == -1` 必须真正构建,不许被当成「零板块」短路。

    这是把「建过」的标记从板块数换成 seq 之后唯一的风险点。列默认 -1、
    `kg_mutation_seq` 恒 >= 0,两者永远不等 —— 这条把那个前提钉住。
    """
    notebook_id = _seed(repo)
    with repo._connect() as db:
        state = db.execute(
            "SELECT community_seq, kg_mutation_seq FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,)).fetchone()
    assert int(state["community_seq"]) == -1
    assert int(state["kg_mutation_seq"]) >= 0

    calls = _spy_heavy_work(repo, monkeypatch)
    assert repo.rebuild_communities(notebook_id) == 2
    assert sorted(set(calls)) == sorted(_HEAVY_WORK), f"从没建过却短路了:{calls}"
    assert _boards(repo, notebook_id)
    assert set(_artifacts(repo, notebook_id)) == set(ARTIFACT_KINDS)


def test_deleting_the_notebook_takes_the_artifacts_with_it(repo):
    notebook_id = _seed(repo)
    repo.rebuild_communities(notebook_id)
    assert _artifacts(repo, notebook_id)
    repo.delete_notebook(notebook_id)
    assert _artifacts(repo, notebook_id) == {}
    assert _edges(repo, notebook_id) == []
    assert _profiles(repo, notebook_id) == {}
