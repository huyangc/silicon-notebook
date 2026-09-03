"""KG 质量分析报告 service + 端点(T3)。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md`「T3 service + 端点」。
钉的是六件事,每一条都对应设计里一条硬要求:

1. **绝不在请求路径上做全表扫**(§3.2)—— 用一个 spy store 钉住那四条重活一次都没被
   调到。这是本批最容易被后来的「顺手加个实时数字」破坏的一条。
2. **逐指标新鲜度**(§3.3)—— 每份产物带自己的 built_at_seq / seq_behind / stale,
   板块列表按 community_seq 而不是账本 seq 标注。
3. **口径来源可分辨**(§3.5)—— 实时 USABLE 口径与社区快照口径各带 basis。
4. **单位显式**—— 载荷里每一个数值叶子都必须在 units 里有一条,否则报红。
5. **缺失 ≠ 为空**—— never_computed / expected / unexpected 三档分得开。
6. **截断绝不静默**—— 落库级与请求级两级截断都透出;/sources 分页在并列上稳定。

⚠ 这条特性至今已经出过 **5 个空守卫**(T1 四个、T2 一个),所以本文件的每一条守卫都
按「删除变异 + 移动变异」实测过会报红,不是写完就算。
"""
from __future__ import annotations

import json
import threading

import pytest

from app.core.config import Settings
from app.models.kg_analysis import KgAnalysisResponse, SourceProfilePageResponse
from app.services.kg_analysis import (
    ABSENCE_EXPECTED,
    ABSENCE_NEVER_COMPUTED,
    ABSENCE_UNEXPECTED,
    ARTIFACT_UNITS,
    BASIS_COMMUNITY_SNAPSHOT,
    BASIS_UNIFIED_REBUILD_SNAPSHOT,
    BASIS_USABLE_LIVE,
    BOARD_EDGE_UNITS,
    BOARD_UNITS,
    LEDGER_COMPLETE,
    LEDGER_EMPTY,
    LEDGER_PARTIAL,
    ORDER_CONNECTED,
    ORDER_SPARSE,
    SOURCE_PAGE_UNITS,
    STATE_UNITS,
    KgAnalysisService,
)
from app.services.kg_analysis_precompute import (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_KINDS,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_SOURCE_PROFILES,
    BOARD_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_DEPENDENT_ARTIFACT_KINDS,
    stamp_cluster_seq,
)
from app.services.knowledge_contracts import (
    COMMUNITY_OVERVIEW_MAX,
    COMMUNITY_TOP_MEMBERS_MAX,
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
)
from app.services.sqlite_repository import SQLiteRepository

NB = "nb-analysis"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    with repository._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_at,updated_at) "
            "VALUES (?,?, '2026-01-01T00:00:00','2026-01-01T00:00:00')",
            (NB, NB),
        )
    return repository


# ------------------------------------------------------------------ 夹具工具


def _state(db, *, seq: int = 10, community_seq: int = 10, dirty: int = 0,
           objects: int = 7, relations: int = 5, clusters: int = 4) -> None:
    db.execute(
        "INSERT INTO unified_kg_state "
        "(notebook_id, dirty, kg_mutation_seq, community_seq, cluster_mutation_seq, "
        " canonical_rel_seq, last_rebuild_at, object_count, relation_count, "
        " cluster_count, updated_at) "
        "VALUES (?,?,?,?,?,?, '2026-01-02T00:00:00', ?,?,?, '2026-01-02T00:00:00')",
        (NB, dirty, seq, community_seq, seq, seq, objects, relations, clusters),
    )


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


def _artifact(db, kind: str, payload: dict, *, seq: int = 10,
              created_at: str = "2026-01-02T00:00:00",
              cluster_seq: "int | None" = None, stamp: bool = True,
              partition_rebuilt: bool = True) -> None:
    """写一行账本。默认照生产路径盖簇世代的戳(`stamp_cluster_seq` 知道该盖哪几份)。

    ``cluster_seq=None`` → 跟 ``seq`` 走(``_state`` 的两个计数器默认也是同一个数,
    所以不特意错开时一切对齐)。``stamp=False`` 模拟**本次修复之前**写下的行:依赖
    合并结果却没盖戳,读侧必须报「无从判断」而不是默认新鲜。
    """
    stamped = (
        stamp_cluster_seq(
            {kind: payload}, seq if cluster_seq is None else cluster_seq,
            partition_rebuilt=partition_rebuilt,
        )[kind]
        if stamp else payload
    )
    db.execute(
        "INSERT INTO kg_analysis_artifacts "
        "(notebook_id, kind, kg_mutation_seq, payload, created_at) VALUES (?,?,?,?,?)",
        (NB, kind, seq, json.dumps(stamped, ensure_ascii=False), created_at),
    )


def _edge(db, src: str, dst: str, weight: int) -> None:
    db.execute(
        "INSERT INTO kg_community_edges "
        "(notebook_id, src_community_id, dst_community_id, weight) VALUES (?,?,?,?)",
        (NB, src, dst, weight),
    )


def _profile(db, source_id: str, *, mainstream: float, n_objects: int = 10,
             n_graph_objects: int = 8, top: str = "cm-1", top_share: float = 0.5,
             spread: int = 2) -> None:
    db.execute(
        "INSERT INTO kg_source_profiles "
        "(notebook_id, source_id, n_objects, n_graph_objects, top_community_id, "
        " top_share, community_spread, mainstream_share) VALUES (?,?,?,?,?,?,?,?)",
        (NB, source_id, n_objects, n_graph_objects, top, top_share, spread, mainstream),
    )


def _source(db, source_id: str, title: str) -> None:
    db.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,created_at,updated_at) "
        "VALUES (?,?,?, 'file', '2026-01-01T00:00:00','2026-01-01T00:00:00')",
        (source_id, NB, title),
    )


def _histogram_payload() -> dict:
    """`cluster_size_histogram` 的真实形状(定长定序的桶 + 五个分组)。

    刻意手写而不是跑一次真查询:本文件测的是**装配与标注**,让夹具依赖那条重活
    等于把「请求路径不跑重活」那条守卫的证据链绕回它自己。形状漂移由
    test_unit_tables_cover_the_real_query_payloads(真的跑一次 T1 查询)钉住。
    """
    from app.repositories.kg_analysis_payloads import cluster_histogram_payload

    return cluster_histogram_payload({("concept", "1"): (3, 3, 0)})


def _relation_payload() -> dict:
    from app.repositories.kg_analysis_payloads import relation_provenance_payload

    return relation_provenance_payload({"untagged": 5, "rejected": 1})


def _largest_payload() -> dict:
    from app.repositories.kg_analysis_payloads import largest_clusters_payload

    return largest_clusters_payload([("K", "concept K", 3)], 20)


def _edges_payload(**overrides) -> dict:
    payload = {
        "level": 0,
        "edges": 2,
        "edges_total": 2,
        "truncated": False,
        "edge_limit": 200_000,
        "cross_weight": 30,
        "intra_weight": 12,
        "communities": 3,
    }
    payload.update(overrides)
    return payload


def _profiles_payload(**overrides) -> dict:
    payload = {
        "level": 0,
        "sources": 3,
        "mainstream_coverage": 0.5,
        "head_communities": 1,
        "head_members": 4,
        "total_members": 7,
    }
    payload.update(overrides)
    return payload


def _seed_complete_ledger(db, *, seq: int = 10, created_at: str = "2026-01-02T00:00:00",
                          cluster_seq: "int | None" = None,
                          partition_rebuilt: bool = True, **edge_overrides) -> None:
    common = {"seq": seq, "created_at": created_at, "cluster_seq": cluster_seq,
              "partition_rebuilt": partition_rebuilt}
    _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload(), **common)
    _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload(), **common)
    _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload(), **common)
    _artifact(db, ARTIFACT_COMMUNITY_EDGES, _edges_payload(**edge_overrides), **common)
    _artifact(db, ARTIFACT_SOURCE_PROFILES, _profiles_payload(), **common)


def _service(repo, store=None) -> KgAnalysisService:
    runtime = repo._runtime
    return KgAnalysisService(
        database=runtime.database,
        unified_kg=store if store is not None else runtime.unified_kg,
        now=lambda: "2026-01-03T00:00:00",
    )


def _artifact_of(overview, kind: str):
    return next(a for a in overview.artifacts if a.kind == kind)


class _SpyStore:
    """透传到真 store,同时记下被调过的方法名。

    刻意用 `__getattr__` 透传而不是逐个包 —— 新增一条重活时它自动被覆盖,不需要
    记得来这里补一行(而「记得补一行」正是守卫失效的头号原因)。
    """

    HEAVY = (
        "cluster_size_histogram",
        "largest_clusters",
        "relation_provenance_counts",
        "source_canonical_rows",
    )

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def recorded(*args, **kwargs):
            self.calls.append(name)
            return attr(*args, **kwargs)

        return recorded


# ------------------------------------------------------- §3.2 请求路径无全表扫


def test_request_path_never_runs_the_full_table_scans(repo):
    """**本文件最重要的一条**:两个端点的整条路径上,那四条全表重活一次都不能被调到。

    它们是 T2 在 rebuild 那一批里算完落库的(生产库 200 万簇行 / 836 万边,冷态与
    仓库那次「835 万边冷扫 39 分钟」同量级)。把其中任何一条挪回请求路径,用户点开
    报告就会挂住整个后端 —— 而且在小库上完全测不出来,只有这条守卫能拦。
    """
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0), ("K2", 0.5)])
        _seed_complete_ledger(db)
        _edge(db, "cm-1", "cm-2", 20)
        _profile(db, "s-1", mainstream=0.1)

    spy = _SpyStore(repo._runtime.unified_kg)
    service = _service(repo, store=spy)
    service.overview(NB)
    service.source_profiles(NB)

    heavy = [name for name in spy.calls if name in _SpyStore.HEAVY]
    assert heavy == [], f"在线请求路径上跑了全表重活:{heavy}"
    assert spy.calls, "spy 没接上:一次 store 调用都没观察到"


def test_analysis_endpoints_never_write(repo):
    """只读,两道观测量(与 T1 同款):语句级 trace + 相关表的行数/内容指纹。

    单看 trace 不够 —— 经 `database.write()` 的另一条连接发生的写它看不见;单看指纹
    也不够 —— 改了 0 行的 UPDATE 指纹不变。两道一起才钉得住。
    """
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db)
        _edge(db, "cm-1", "cm-2", 20)
        _profile(db, "s-1", mainstream=0.1)

    connection = repo._connect()
    before = _table_fingerprints(connection)
    seen: list[str] = []
    connection.set_trace_callback(seen.append)
    try:
        service = _service(repo)
        service.overview(NB)
        service.source_profiles(NB)
    finally:
        connection.set_trace_callback(None)

    assert seen, "trace 没装上:没有观察到任何语句"
    offenders = [
        sql for sql in seen
        if any(sql.lstrip().upper().startswith(verb) for verb in _DML_VERBS)
    ]
    assert offenders == [], f"只读装配里出现了写语句:{offenders}"
    assert _table_fingerprints(connection) == before


def test_service_refuses_to_run_inside_a_write_transaction(repo):
    """在写事务里装配报告会读到提交前的库(不报错、静默过时),SQLite 上还把进程级
    写锁按住整趟装配。所以入口当场硬失败,而不是靠注释约束调用方。

    这条必须是**运行时**守卫:形状守卫抓不到它 —— 这些读一张产物表之外的东西都不碰,
    在 SQLite 上还跑在另一条连接上(T1/T2 已经被「移动变异全绿」教训过一次)。

    ⚠ 只断言 `raises(RuntimeError)` **不够**:把守卫从入口挪到方法末尾照样能满足它,
    而那时四条读已经在写事务里跑完了(危害已经发生)。所以还要断言**一条 store 读都
    没发生** —— 这条才对「移动变异」敏感。
    """
    spy = _SpyStore(repo._runtime.unified_kg)
    service = _service(repo, store=spy)
    with repo._write() as _db:
        with pytest.raises(RuntimeError, match="写事务"):
            service.overview(NB)
        assert spy.calls == [], f"守卫拦下之前已经读了库:{spy.calls}"
        with pytest.raises(RuntimeError, match="写事务"):
            service.source_profiles(NB)
        assert spy.calls == [], f"守卫拦下之前已经读了库:{spy.calls}"


# ------------------------------------------------------------ §3.3 逐指标新鲜度


def test_born_state_row_reports_like_a_never_written_notebook(repo):
    """codex #601 R1 P2: create_notebook now seeds a `unified_kg_state` row at
    birth (the provenance certificate). ``present`` means "has KG history",
    never "the row exists" — the frontend renders 「上次整理时的规模:0·0·0」
    off ``state.present``, so a zero-history row must produce an overview
    byte-identical to the historical row-absent shape."""
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="fresh"))
    born = _service(repo).overview(nb.id)
    assert born.state.present is False

    with repo._write() as db:
        db.execute(
            "DELETE FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        )
    absent = _service(repo).overview(nb.id)
    assert born == absent


def test_every_artifact_kind_is_reported_even_when_absent(repo):
    """`artifacts` 恒为五条、恒定顺序。缺席的那几份也在列表里。

    只返回在场的那几份,「这份产物不在」就要靠调用方自己去和 ARTIFACT_KINDS 做差集,
    而漏做差集的表现是报告上少一张卡片、没有任何提示 —— 恰恰是 §3.3 要消灭的那类
    静默。
    """
    with repo._write() as db:
        _state(db)

    overview = _service(repo).overview(NB)

    assert [a.kind for a in overview.artifacts] == list(ARTIFACT_KINDS)
    assert all(not a.present for a in overview.artifacts)
    assert overview.ledger_state == LEDGER_EMPTY


def test_each_artifact_carries_its_own_seq_and_lag(repo):
    """逐指标标注:每份产物报自己的 built_at_seq 与落后量,不是整份报告一个横幅。"""
    with repo._write() as db:
        _state(db, seq=42, community_seq=42)
        _seed_complete_ledger(db, seq=30)

    overview = _service(repo).overview(NB)

    for view in overview.artifacts:
        assert view.freshness.built_at_seq == 30
        assert view.freshness.seq_behind == 12
        assert view.freshness.stale is True
    assert overview.state.kg_mutation_seq == 42
    assert overview.ledger_consistent is True


def test_absent_artifact_reports_null_lag_not_zero(repo):
    """缺席时 built_at_seq / seq_behind / stale 全是 None —— 0 会被读成「刚建好」。"""
    with repo._write() as db:
        _state(db, seq=5)

    view = _artifact_of(_service(repo).overview(NB), ARTIFACT_CLUSTER_HISTOGRAM)

    assert (view.freshness.built_at_seq, view.freshness.seq_behind,
            view.freshness.stale) == (None, None, None)


def test_ledger_ahead_of_current_seq_is_reported_not_clamped(repo):
    """账本比当前 seq 还新 = 库被手工改过。负的落后量如实报出,不 clamp 到 0 ——
    压成 0 等于替读者把异常藏起来。"""
    with repo._write() as db:
        _state(db, seq=3, community_seq=3)
        _seed_complete_ledger(db, seq=9)

    view = _artifact_of(_service(repo).overview(NB), ARTIFACT_LARGEST_CLUSTERS)

    assert view.freshness.seq_behind == -6
    assert view.freshness.stale is True


def test_mixed_ledger_seqs_are_flagged_as_inconsistent(repo):
    """正常写路径把五行盖同一个 seq(一个事务整批写)。不一致只可能是库被改过,
    此时报告里的数字互相不可比 —— 必须让读者知道。"""
    with repo._write() as db:
        _state(db, seq=10)
        _seed_complete_ledger(db, seq=10)
        db.execute(
            "UPDATE kg_analysis_artifacts SET kg_mutation_seq=4 WHERE kind=?",
            (ARTIFACT_RELATION_PROVENANCE,),
        )

    overview = _service(repo).overview(NB)

    assert overview.ledger_consistent is False
    assert _artifact_of(overview, ARTIFACT_RELATION_PROVENANCE).freshness.seq_behind == 6
    assert _artifact_of(overview, ARTIFACT_CLUSTER_HISTOGRAM).freshness.seq_behind == 0


def test_a_merge_only_drift_is_reported_per_artifact_not_swallowed(repo):
    """codex 第 5 轮:合并世代单独漂了,依赖它的四份必须报陈旧。

    形状是真实的:合并的写路径刻意不动 `kg_mutation_seq`,所以 KG 世代对齐、合并世代
    落后。只看前者的话这四份会齐刷刷地说「与当前一致」,而它们的数字全是从
    `concept_clusters` 算出来的。
    """
    with repo._write() as db:
        _state(db, seq=40, community_seq=40)             # cluster_mutation_seq 也 = 40
        db.execute(
            "UPDATE unified_kg_state SET cluster_mutation_seq=43 WHERE notebook_id=?",
            (NB,),
        )
        _seed_complete_ledger(db, seq=40, cluster_seq=40)

    overview = _service(repo).overview(NB)

    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS):
        freshness = _artifact_of(overview, kind).freshness
        assert freshness.seq_behind == 0, kind          # KG 世代确实没动
        assert freshness.built_at_cluster_seq == 40, kind
        assert freshness.cluster_seq_behind == 3, kind
        assert freshness.stale is True, kind
    # 与合并无关的那份不被拖下水:它的两个输入表都没变,报陈旧是在制造假警报。
    provenance = _artifact_of(overview, ARTIFACT_RELATION_PROVENANCE).freshness
    assert (provenance.built_at_cluster_seq, provenance.cluster_seq_behind) == (None, None)
    assert provenance.stale is False
    assert overview.state.cluster_mutation_seq == 43


def test_a_backfilled_board_product_is_reported_as_unknown_not_as_current(repo):
    """codex 第 7 轮 P2 的**读侧**:混合世代的产物不许显示成「与当前一致」。

    形状是「只补账本」那一轮的产物:板块划分是库里现成的(建在哪一代合并结果上没有
    地方记),边与来源映射按当前 cluster_map 现算。它既不是当前的、也说不出落后几代
    ——所以三个字段一起是 None,而**不是** ``stale=False``:后者会让报告理直气壮地说
    「与当前一致」,正是本视图存在理由的反面。
    """
    with repo._write() as db:
        _state(db, seq=40, community_seq=40)
        db.execute(
            "UPDATE unified_kg_state SET cluster_mutation_seq=43 WHERE notebook_id=?",
            (NB,),
        )
        # 两条统计快照当场重算(戳 43),依赖板块的两份记「无从判断」。
        _seed_complete_ledger(db, seq=40, cluster_seq=43, partition_rebuilt=False)

    overview = _service(repo).overview(NB)

    for kind in sorted(BOARD_DEPENDENT_ARTIFACT_KINDS):
        freshness = _artifact_of(overview, kind).freshness
        assert _artifact_of(overview, kind).present is True, kind
        assert freshness.seq_behind == 0, kind
        assert freshness.built_at_cluster_seq is None, kind
        assert freshness.cluster_seq_behind is None, kind
        assert freshness.stale is None, kind            # ⚠ 不是 False
    # 两条统计快照的世代是知道的,不能被一起拖成「未知」。
    for kind in sorted(CLUSTER_DEPENDENT_ARTIFACT_KINDS
                       - set(BOARD_DEPENDENT_ARTIFACT_KINDS)):
        freshness = _artifact_of(overview, kind).freshness
        assert freshness.built_at_cluster_seq == 43, kind
        assert freshness.stale is False, kind
    # 账本仍然是齐的、仍然自洽 —— 「世代无从判断」不是「产物缺失」。
    assert overview.ledger_state == LEDGER_COMPLETE
    assert overview.ledger_consistent is True
    # 跨板块关联那一格读的是同一份账本行,必须给出同一句话。
    assert overview.board_edges.freshness.stale is None


def test_the_read_side_and_the_precompute_gate_share_one_verdict(repo):
    """读侧的 `stale` 与写侧的闸**必须**同一个判据 —— 不是「看起来一样」。

    分岔的表现是一份自相矛盾的报告:闸说「不用重算」而读侧说「陈旧」,或者反过来
    (闸短路、读侧报「与当前一致」,那正是第 5 轮报的那一条)。这里对同一批账本行
    分别问两侧,逐 kind 比对结论。
    """
    from app.services.kg_analysis_precompute import analysis_ledger_is_current

    verdicts = set()
    for cluster_now, kg_now in ((40, 40), (43, 40), (40, 41), (43, 41)):
        with repo._write() as db:
            db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (NB,))
            db.execute("DELETE FROM unified_kg_state WHERE notebook_id=?", (NB,))
            _state(db, seq=kg_now, community_seq=kg_now)
            db.execute(
                "UPDATE unified_kg_state SET cluster_mutation_seq=? WHERE notebook_id=?",
                (cluster_now, NB),
            )
            _seed_complete_ledger(db, seq=40, cluster_seq=40)

        overview = _service(repo).overview(NB)
        store = repo._runtime.unified_kg
        with repo._connect() as db:
            ledger = store.kg_analysis_artifact_rows(db, NB)
        gate_says_current = analysis_ledger_is_current(
            ledger, kg_now, cluster_now, has_boards=True
        )
        read_says_current = all(
            view.freshness.stale is False for view in overview.artifacts
        )
        assert gate_says_current == read_says_current, (
            f"闸与读侧对同一批账本给出了不同结论(kg={kg_now}, cluster={cluster_now})"
        )
        verdicts.add(gate_says_current)
    # 四个组合里两种结论都必须出现过 —— 否则这条守卫只是在比对一个恒定值。
    assert verdicts == {True, False}


def test_an_unstamped_legacy_row_reports_unknown_not_fresh(repo):
    """修复**之前**写下的账本行(依赖合并结果却没盖戳)必须报「无从判断」+ 陈旧。

    默认成新鲜是最坏的一档:它会让一批建在未知合并结果上的数字冒充当前值,而这正是
    本视图存在的理由的反面。判不出来就补跑 + 明说,不猜。
    """
    with repo._write() as db:
        _state(db, seq=10, community_seq=10)
        _seed_complete_ledger(db, seq=10)
        db.execute(
            "DELETE FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
            (NB, ARTIFACT_CLUSTER_HISTOGRAM),
        )
        _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload(),
                  seq=10, stamp=False)

    freshness = _artifact_of(
        _service(repo).overview(NB), ARTIFACT_CLUSTER_HISTOGRAM
    ).freshness

    assert freshness.built_at_seq == 10 and freshness.seq_behind == 0
    assert freshness.built_at_cluster_seq is None
    assert freshness.cluster_seq_behind is None
    assert freshness.stale is True


def test_boards_are_stamped_with_community_seq_not_the_ledger_seq(repo):
    """板块列表**实时读**板块表,而那张表是上次社区重建的快照 —— 所以它的戳必须是
    community_seq。

    这正是 §3.3 那次真实事故的形状:在生产库上按 88 580 个板块推出「图散成一地」,
    而那些板块建于一个早得多的 KG 状态。产物用账本 seq、板块用 community_seq,
    两者可以各自陈旧,标错一个就会把陈旧的板块说成新鲜的。
    """
    with repo._write() as db:
        _state(db, seq=40, community_seq=12)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db, seq=40)

    overview = _service(repo).overview(NB)

    assert overview.boards.freshness.built_at_seq == 12
    assert overview.boards.freshness.seq_behind == 28
    assert _artifact_of(overview, ARTIFACT_COMMUNITY_EDGES).freshness.seq_behind == 0


def test_boards_never_built_report_null_not_a_lag(repo):
    """community_seq = -1(从没建过社区)与「落后 N 次」是两件事。"""
    with repo._write() as db:
        _state(db, seq=8, community_seq=-1)

    freshness = _service(repo).overview(NB).boards.freshness

    assert (freshness.built_at_seq, freshness.seq_behind, freshness.stale) == (
        None, None, None)


def test_boards_report_unknown_merge_generation_after_a_cluster_only_write(repo):
    """codex 第 8 轮 P2:纯合并写入之后,板块那一格**不许**说「与当前一致」。

    簇写路径刻意不动 `kg_mutation_seq`,所以合并单独动过之后 `community_seq` 与
    `kg_mutation_seq` 仍然相等 —— 只比 KG 世代的旧判据据此把这一格判成 stale=False,
    而板块划分的簇世代戳**根本建立不起来**。同一屏上依赖板块的两份产物(第 7 轮修复后)
    已经如实报「对不上合并进度」,板块本身却说「与当前一致」:一份能同时说出这两句话的
    报告本身就不可信,而这正是本视图存在的理由的反面。
    """
    with repo._write() as db:
        _state(db, seq=10, community_seq=10)
        db.execute(
            "UPDATE unified_kg_state SET cluster_mutation_seq=11 WHERE notebook_id=?",
            (NB,),
        )
        _community(db, "cm-1", [("K1", 1.0)])
        # 「只补账本」那一轮的账本:板块划分是库里现成的 → 依赖板块的两份显式记「无从判断」
        _seed_complete_ledger(db, seq=10, cluster_seq=11, partition_rebuilt=False)

    overview = _service(repo).overview(NB)
    boards = overview.boards.freshness

    # 前提事实:KG 世代确实一动没动 —— 旧判据正是据此说「与当前一致」的。
    assert (boards.built_at_seq, boards.seq_behind) == (10, 0)
    assert boards.stale is None, "板块的合并世代无从判断,不能报成「与当前一致」"
    assert boards.built_at_cluster_seq is None
    assert boards.cluster_seq_behind is None
    # 同屏的那两份产物说的是同一句话 —— 这一条才是「不自相矛盾」本身。
    for kind in BOARD_DEPENDENT_ARTIFACT_KINDS:
        assert _artifact_of(overview, kind).freshness.stale is None, kind


def test_boards_and_the_board_dependent_products_answer_the_merge_question_alike(repo):
    """「这套板块划分建在哪一代合并结果上」是**一件事**,同屏不许有两个答案。

    第 8 轮报的就是这个形状:两处各判一遍,然后分岔。判据合流之后,板块那一格与依赖
    板块的两份产物拿的是**同一个** `built_at_cluster_seq`(`board_partition_cluster_seq`
    从账本读)、走**同一个** `_generation_verdict`,所以三个字段必须逐个相等。

    ⚠ 三种结论都要出现过,否则这条守卫只是在比对一个恒定值(本 PR 已经因此出过 16 个
    空守卫)。
    """
    verdicts = set()
    for cluster_now, stamped_cluster, partition_rebuilt in (
        (10, 10, True),      # 全量重建那一轮:世代知道,且对齐
        (11, 10, True),      # 之后又合并过:世代知道,明确落后
        (11, 11, False),     # 只补账本:世代无从判断
    ):
        with repo._write() as db:
            for table in ("kg_analysis_artifacts", "unified_kg_state",
                          "communities", "community_members"):
                db.execute(f"DELETE FROM {table} WHERE notebook_id=?", (NB,))
            _state(db, seq=10, community_seq=10)
            db.execute(
                "UPDATE unified_kg_state SET cluster_mutation_seq=? WHERE notebook_id=?",
                (cluster_now, NB),
            )
            _community(db, "cm-1", [("K1", 1.0)])
            _seed_complete_ledger(db, seq=10, cluster_seq=stamped_cluster,
                                  partition_rebuilt=partition_rebuilt)

        boards = _service(repo).overview(NB).boards.freshness
        overview = _service(repo).overview(NB)
        for kind in BOARD_DEPENDENT_ARTIFACT_KINDS:
            product = _artifact_of(overview, kind).freshness
            assert (
                boards.built_at_cluster_seq,
                boards.cluster_seq_behind,
                boards.stale,
            ) == (
                product.built_at_cluster_seq,
                product.cluster_seq_behind,
                product.stale,
            ), (
                f"板块与 {kind} 对同一件事给出了不同答案"
                f"(cluster_now={cluster_now}, stamped={stamped_cluster})"
            )
        verdicts.add(boards.stale)
    assert verdicts == {False, True, None}, f"没覆盖到三种结论:{verdicts}"


def test_state_exposes_the_last_rebuild_scale_snapshot(repo):
    """§3.3 点名要透出的那组:当前 seq、各产物 seq、dirty、上次 rebuild 的时刻与规模。
    读者拿它与当前真实计数一比就能量化陈旧程度。"""
    with repo._write() as db:
        _state(db, seq=11, community_seq=9, dirty=1,
               objects=880, relations=830, clusters=170)

    state = _service(repo).overview(NB).state

    assert state.present is True
    assert (state.kg_mutation_seq, state.community_seq) == (11, 9)
    assert state.dirty is True
    assert state.last_rebuild.at == "2026-01-02T00:00:00"
    assert (state.last_rebuild.object_count, state.last_rebuild.relation_count,
            state.last_rebuild.cluster_count) == (880, 830, 170)
    assert state.last_rebuild.basis == BASIS_UNIFIED_REBUILD_SNAPSHOT


def test_missing_state_row_still_produces_a_report(repo):
    """从没写过 KG 的库照样出报告,每一格都在说「没有」——而不是 500。"""
    overview = _service(repo).overview(NB)

    assert overview.state.present is False
    assert overview.state.kg_mutation_seq == 0
    assert overview.state.community_seq == -1
    assert overview.ledger_state == LEDGER_EMPTY


# ---------------------------------------------------------- §3.5 口径来源可分辨


def test_live_and_snapshot_bases_are_distinguishable(repo):
    """同一份报告里两种口径并列:簇大小直方图是实时 USABLE 口径,板块规模来自上次
    社区重建的快照。不标来源,读者没有任何线索分辨为什么两个数字对不上。"""
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db)

    overview = _service(repo).overview(NB)

    assert _artifact_of(overview, ARTIFACT_CLUSTER_HISTOGRAM).freshness.basis == (
        BASIS_USABLE_LIVE)
    assert _artifact_of(overview, ARTIFACT_LARGEST_CLUSTERS).freshness.basis == (
        BASIS_USABLE_LIVE)
    assert _artifact_of(overview, ARTIFACT_RELATION_PROVENANCE).freshness.basis == (
        BASIS_USABLE_LIVE)
    assert _artifact_of(overview, ARTIFACT_COMMUNITY_EDGES).freshness.basis == (
        BASIS_COMMUNITY_SNAPSHOT)
    assert _artifact_of(overview, ARTIFACT_SOURCE_PROFILES).freshness.basis == (
        BASIS_COMMUNITY_SNAPSHOT)
    assert overview.boards.freshness.basis == BASIS_COMMUNITY_SNAPSHOT


# ------------------------------------------------------------------ 单位显式


def _numeric_leaf_names(value, out: set) -> set:
    """载荷里每一个数值叶子的**字段名**。bool 不算(True/False 不是量)。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                out.add(key)
            else:
                _numeric_leaf_names(item, out)
    elif isinstance(value, list):
        for item in value:
            _numeric_leaf_names(item, out)
    return out


def test_every_numeric_field_declares_a_unit(repo):
    """载荷里每一个数值叶子都必须在 units 里有一条。

    为什么这条守卫值得存在:T2 已经在中性模块顶部记明「canonical 计数 / 对象计数 /
    板块对 / 关系行」四种单位互不可比,但那是**注释**,新增一个字段时没有任何东西会
    提醒你补。这里把它变成机器可检的:漏一个字段就报红,并且报得出是哪个。
    """
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0), ("K2", 0.5)])
        _seed_complete_ledger(db)
        _edge(db, "cm-1", "cm-2", 20)
        _profile(db, "s-1", mainstream=0.1)

    service = _service(repo)
    overview = service.overview(NB)
    page = service.source_profiles(NB)

    for view in overview.artifacts:
        if view.payload is None:
            continue
        missing = _numeric_leaf_names(view.payload, set()) - set(view.units)
        assert missing == set(), f"{view.kind} 的这些数值字段没有单位:{sorted(missing)}"
    assert _numeric_leaf_names(overview.boards.payload, set()) <= set(
        overview.boards.units)
    assert {"limit", "returned", "returned_weight", "stored", "stored_total",
            "edge_limit", "cross_weight", "weight"} <= set(overview.board_edges.units)
    assert {"n_objects", "n_graph_objects", "top_share", "community_spread",
            "mainstream_share"} <= set(page.units)
    for row in page.rows:
        missing = _numeric_leaf_names(row, set()) - set(page.units)
        assert missing == set(), f"来源画像行的这些字段没有单位:{sorted(missing)}"


def test_unit_tables_cover_the_real_query_payloads(repo):
    """单位表对着**真查询**的载荷校一遍,而不是只对着上面那几个手写夹具。

    上面的守卫用夹具喂载荷,所以它只能证明「夹具里的字段都有单位」。这一条真的跑一次
    T1 的三条聚合(测试里跑重活是可以的,不能挂在请求路径上的是端点),把口径漂移
    (T1 给载荷加了字段而这里忘了补单位)也钉住。
    """
    payloads = {
        ARTIFACT_CLUSTER_HISTOGRAM: repo.kg_cluster_size_histogram(NB),
        ARTIFACT_LARGEST_CLUSTERS: repo.kg_largest_clusters(NB),
        ARTIFACT_RELATION_PROVENANCE: repo.kg_relation_provenance_counts(NB),
    }
    for kind, payload in payloads.items():
        missing = _numeric_leaf_names(payload, set()) - set(ARTIFACT_UNITS[kind])
        assert missing == set(), f"{kind} 的这些数值字段没有单位:{sorted(missing)}"
    boards = repo.kg_community_overview(NB)
    assert _numeric_leaf_names(boards, set()) <= set(BOARD_UNITS)


def test_unit_vocabulary_separates_the_four_incomparable_counts():
    """四种计数必须落在**不同**的单位代号上,否则 T4 会把它们拼成一个比例。

    这一条是纯契约检查(不起库):`head_members` 是 canonical 计数、`n_graph_objects`
    是对象计数、`edges` 是板块对、`cross_weight` 是关系行 —— 同屏并列前两个再相除
    恒小于 1 却没有任何意义(合并把多个对象压成一个 canonical)。
    """
    units = {
        ARTIFACT_UNITS[ARTIFACT_SOURCE_PROFILES]["head_members"],
        SOURCE_PAGE_UNITS["n_graph_objects"],
        ARTIFACT_UNITS[ARTIFACT_COMMUNITY_EDGES]["edges"],
        ARTIFACT_UNITS[ARTIFACT_COMMUNITY_EDGES]["cross_weight"],
    }
    assert len(units) == 4, f"四种不可比的计数共用了单位代号:{units}"
    assert STATE_UNITS["object_count"] == SOURCE_PAGE_UNITS["n_objects"]
    assert BOARD_UNITS["size"] == ARTIFACT_UNITS[ARTIFACT_SOURCE_PROFILES][
        "total_members"]


# -------------------------------------------------------- 缺失 / 为空 / 合法缺席


def test_zero_board_library_absence_of_source_profiles_is_expected(repo):
    """唯一合法缺席的一档:一个板块都没有的库不写来源画像(写一张全 0 的表等于在说
    「所有来源都关联稀疏」)。判据与写入口 `check_artifact_payloads` 同一条:
    跨板块边账本里的 communities == 0。"""
    with repo._write() as db:
        _state(db)
        _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload())
        _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload())
        _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload())
        _artifact(db, ARTIFACT_COMMUNITY_EDGES,
                  _edges_payload(communities=0, edges=0, edges_total=0, cross_weight=0))

    view = _artifact_of(_service(repo).overview(NB), ARTIFACT_SOURCE_PROFILES)

    assert view.present is False
    assert view.optional is True
    assert view.absence == ABSENCE_EXPECTED


@pytest.mark.parametrize(
    "boards, expected_absence, expected_state",
    [
        # 零板块:来源画像合法缺席,账本仍然是**齐全**的(S4 的决定 —— 见
        # `OPTIONAL_ARTIFACT_KINDS`)。判成 partial 会让这类库永远「不齐」。
        (0, ABSENCE_EXPECTED, LEDGER_COMPLETE),
        # 有板块却没有来源画像:这一轮漏写了。两个格子必须**同时**说这件事。
        (3, ABSENCE_UNEXPECTED, LEDGER_PARTIAL),
    ],
)
def test_ledger_state_never_says_complete_while_absence_says_unexpected(
    repo, boards, expected_absence, expected_state
):
    """账本档位与 absence 必须是**同一条**判据的两种表述,不能同屏互相打脸。

    这条钉的是 codex 第 3 轮评审那个矛盾:有板块却缺来源画像时,`_absence` 判
    「本该有却缺失」(红档),而 `_ledger_state` 当时只看四份恒定必需的,照报「齐全」。
    一份能同时说出这两句话的完整性报告本身就不可信 —— 读者无从知道该信哪一句,而这
    正是本视图存在的理由。

    **两个方向一起钉,不能只钉一个**:
      · 只钉 boards=3 → 把 source_profiles 无条件塞进必需集也全绿,而那会让零板块库
        永远报「残缺」,直接违反 S4;
      · 只钉 boards=0 → 就是把 bug 本身钉住。
    """
    with repo._write() as db:
        _state(db)
        _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload())
        _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload())
        _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload())
        _artifact(db, ARTIFACT_COMMUNITY_EDGES, _edges_payload(communities=boards))

    overview = _service(repo).overview(NB)

    assert _artifact_of(overview, ARTIFACT_SOURCE_PROFILES).absence == expected_absence
    assert overview.ledger_state == expected_state
    # 反过来也钉死:凡是有任何一份产物报「本该有却缺失」,档位就不能是「齐全」。
    unexpected = [a.kind for a in overview.artifacts if a.absence == ABSENCE_UNEXPECTED]
    assert not (unexpected and overview.ledger_state == LEDGER_COMPLETE), (
        f"{unexpected} 报「本该有却缺失」,账本档位却是「齐全」"
    )


def test_missing_required_artifact_is_unexpected_and_ledger_is_partial(repo):
    with repo._write() as db:
        _state(db)
        _artifact(db, ARTIFACT_COMMUNITY_EDGES, _edges_payload())
        _artifact(db, ARTIFACT_SOURCE_PROFILES, _profiles_payload())

    overview = _service(repo).overview(NB)

    assert overview.ledger_state == LEDGER_PARTIAL
    assert _artifact_of(overview, ARTIFACT_CLUSTER_HISTOGRAM).absence == (
        ABSENCE_UNEXPECTED)
    assert _artifact_of(overview, ARTIFACT_SOURCE_PROFILES).absence is None


def test_empty_ledger_is_never_computed_not_unexpected(repo):
    """整个库没跑过预计算 ≠ 这一轮漏写了。前者是正常起点,后者是异常。"""
    with repo._write() as db:
        _state(db)

    overview = _service(repo).overview(NB)

    assert {a.absence for a in overview.artifacts} == {ABSENCE_NEVER_COMPUTED}


def test_empty_product_with_a_ledger_row_is_present_not_absent(repo):
    """单一板块的图 legitimately 产出 **0 条**跨板块边。账本行在 → 产物「在场」,
    只是空的。靠明细表的行数判定就会把它误报成「从来没算过」。"""
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db, edges=0, edges_total=0, cross_weight=0,
                              communities=1)

    overview = _service(repo).overview(NB)

    assert _artifact_of(overview, ARTIFACT_COMMUNITY_EDGES).present is True
    assert overview.board_edges.present is True
    assert overview.board_edges.returned == 0
    assert overview.board_edges.weight_coverage is None


def test_malformed_ledger_payload_fails_loudly(repo):
    """行在、内容读不出来:既不是「缺失」也不是「为空」。静默跳过会让下游把一份坏掉的
    产物读成「从来没算过」。"""
    with repo._write() as db:
        _state(db)
        db.execute(
            "INSERT INTO kg_analysis_artifacts "
            "(notebook_id, kind, kg_mutation_seq, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (NB, ARTIFACT_CLUSTER_HISTOGRAM, 3, "[1,2,3]", "2026-01-02T00:00:00"),
        )

    with pytest.raises(ValueError, match="payload 不是对象"):
        _service(repo).overview(NB)


# --------------------------------------- 账本说不在 → 明细表一行都不许回到响应里


def test_absent_edge_ledger_never_returns_orphan_detail_rows(repo):
    """账本行不在,`kg_community_edges` 里却还躺着行 —— 响应里一条都不许出现。

    这两张明细表与账本之间**没有外键、没有任何数据库级约束**保证同生同灭。写侧确实
    是同事务整批重写/整批作废,但那是纪律不是不变量:库被手工改过、迁移只跑了一半、
    或者某个未来的写路径漏一步,留下的就是这个形状。

    不门控的话,同一份响应会既报 `present=false`(前端据此打「本该有却缺失」的红标),
    又把那些行发出去 —— 而俯瞰图**照着 edges 画连线**,于是视图同屏既说「这份产物
    缺失」又把它的边画出来。那些边按定义是悬空的(板块 id 可能已被重铸)。

    顺带钉住:这一趟**连查都不查**。查完再丢掉虽然也能给出同样的响应,但那是在为一份
    已知不存在的产物付一次扫描。
    """
    with repo._write() as db:
        _state(db)
        _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload())
        _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload())
        _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload())
        _edge(db, "cm-1", "cm-2", 30)
        _edge(db, "cm-1", "cm-3", 20)

    spy = _SpyStore(repo._runtime.unified_kg)
    view = _service(repo, store=spy).overview(NB).board_edges

    assert view.present is False
    assert view.edges == []
    assert view.returned == 0
    assert view.returned_weight == 0
    assert view.weight_coverage is None
    assert "kg_community_edges_top" not in spy.calls

    # 对照(否则「edges 恒为空」也能全绿):账本行补上,同样这两行就必须回来。
    with repo._write() as db:
        _artifact(db, ARTIFACT_COMMUNITY_EDGES, _edges_payload())
    present = _service(repo).overview(NB).board_edges
    assert present.present is True
    assert [e["weight"] for e in present.edges] == [30, 20]


def test_absent_profile_ledger_never_returns_orphan_detail_rows(repo):
    """/sources 的同一条:账本行不在 → `present=false` 且**一行都不发**,连查都不查。

    这里用的是**合法缺席**那一档(零板块库不写来源画像),因为它是生产上真会发生的
    形状:上一轮有板块、写过一整张画像表;这一轮板块归零,预计算整份跳过来源画像 ——
    而明细行会不会被上一轮的残留留下来,取决于写路径有没有走到那一步。响应必须只听
    账本的:说不在,就一行都不给。

    `total` 也必须是 0:它是分页的分母,拿悬空行去数会给出一份「说产物不存在、却告诉
    你有 2 个来源、还能翻页」的响应。
    """
    with repo._write() as db:
        _state(db)
        _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload())
        _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload())
        _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload())
        _artifact(db, ARTIFACT_COMMUNITY_EDGES, _edges_payload(communities=0))
        _profile(db, "s-1", mainstream=0.0)
        _profile(db, "s-2", mainstream=0.4)

    spy = _SpyStore(repo._runtime.unified_kg)
    page = _service(repo, store=spy).source_profiles(NB, limit=1)

    assert page.present is False
    assert page.absence == ABSENCE_EXPECTED
    assert page.rows == []
    assert page.total == 0
    assert page.returned == 0
    assert page.has_more is False
    assert page.summary is None
    assert "kg_source_profile_page" not in spy.calls

    # 对照:账本行补上,同样这两行就必须回来(证明上面的空不是「表里本来就没行」)。
    with repo._write() as db:
        _artifact(db, ARTIFACT_SOURCE_PROFILES, _profiles_payload())
    restored = _service(repo).source_profiles(NB, limit=1)
    assert restored.present is True
    assert restored.total == 2
    assert [r["source_id"] for r in restored.rows] == ["s-1"]
    assert restored.has_more is True


# ------------------------------------------------------------------ 截断显式


def test_board_edges_disclose_both_truncation_levels(repo):
    """两级截断都要说清:落库级(预计算把 20 万条上限之外的丢了)与请求级(这次只
    取了 top-N)。只报一级,读者会把「图上这几条」当成「库里只有这几条」。"""
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db, edges=3, edges_total=57, truncated=True,
                              edge_limit=3, cross_weight=100)
        _edge(db, "cm-1", "cm-2", 30)
        _edge(db, "cm-1", "cm-3", 20)
        _edge(db, "cm-2", "cm-3", 10)

    view = _service(repo).overview(NB).board_edges

    # 落库级:库里只存了 3 条,而板块对总数是 57 —— 54 条在预计算那一刻就被丢了。
    assert view.stored == 3 and view.stored_total == 57
    assert view.stored_truncated is True and view.edge_limit == 3
    # 请求级:这次要了 200 条(默认),库里只有 3 条,所以没有二次截断。
    assert view.limit == 200 and view.returned == 3
    # cross_weight 是**全部**跨板块边权,不随任何一级上限变化 —— 返回的 60 只覆盖 60%。
    assert view.cross_weight == 100
    assert view.returned_weight == 60
    assert view.weight_coverage == pytest.approx(0.6)


def test_board_edges_request_level_truncation_and_coverage(repo):
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db, edges=3, edges_total=3, cross_weight=60)
        _edge(db, "cm-1", "cm-2", 30)
        _edge(db, "cm-1", "cm-3", 20)
        _edge(db, "cm-2", "cm-3", 10)

    view = _service(repo).overview(NB, edge_limit=2).board_edges

    assert view.limit == 2
    assert view.returned == 2
    assert [e["weight"] for e in view.edges] == [30, 20]   # 按 weight 降序
    assert view.returned_weight == 50
    assert view.cross_weight == 60
    assert view.weight_coverage == pytest.approx(50 / 60)


def test_board_edges_are_clamped_to_the_hard_ceiling(repo):
    """调用方传一个天文数字也拿不到无界返回 —— store 侧硬 clamp。"""
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        _edge(db, "cm-1", "cm-2", 5)

    view = _service(repo).overview(NB, edge_limit=10**9).board_edges

    assert view.limit == KG_COMMUNITY_EDGES_MAX


# ------------------------------------------------------------- /sources 分页


def test_source_page_is_ordered_by_mainstream_share_ascending(repo):
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        _profile(db, "s-hi", mainstream=0.9)
        _profile(db, "s-lo", mainstream=0.05)
        _profile(db, "s-mid", mainstream=0.5)

    page = _service(repo).source_profiles(NB)

    assert [r["source_id"] for r in page.rows] == ["s-lo", "s-mid", "s-hi"]
    assert page.order == ORDER_SPARSE
    assert page.total == 3 and page.returned == 3 and page.has_more is False
    # summary 就是账本行的 payload 原样(含生产路径盖上的簇世代戳)。
    assert page.summary == stamp_cluster_seq(
        {ARTIFACT_SOURCE_PROFILES: _profiles_payload()}, 10, partition_rebuilt=True
    )[ARTIFACT_SOURCE_PROFILES]


def test_source_page_order_connected_reverses(repo):
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        _profile(db, "s-hi", mainstream=0.9)
        _profile(db, "s-lo", mainstream=0.05)

    page = _service(repo).source_profiles(NB, order=ORDER_CONNECTED)

    assert [r["source_id"] for r in page.rows] == ["s-hi", "s-lo"]


def test_source_pages_do_not_repeat_or_drop_rows_on_ties(repo):
    """**分页正确性**:`mainstream_share` 上并列极多(混杂库里一大片恰好 0.0)。
    没有 `source_id` 的次级排序键,两页之间会重复/漏行 —— 而且两个后端各给一种顺序。
    这条把「翻完全部页 == 全集且无重复」钉死。

    ⚠ `reversed(...)` 不是随手写的:按升序插入时**堆顺序恰好等于目标顺序**,并列消歧
    不做功 —— 删掉 SQL 里的 `, source_id ASC` 这条照样全绿(评审实测)。倒序插入让
    堆顺序与目标顺序相反,次级键这才变成载重件。
    """
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        for i in reversed(range(9)):
            _profile(db, f"s-{i:02d}", mainstream=0.0)

    service = _service(repo)
    seen: list[str] = []
    for offset in (0, 4, 8):
        page = service.source_profiles(NB, limit=4, offset=offset)
        seen.extend(r["source_id"] for r in page.rows)
        assert page.total == 9
        assert page.has_more is (offset + len(page.rows) < 9)

    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 9


def test_last_page_that_is_exactly_full_reports_no_more(repo):
    """`has_more` 的边界:**末页恰好装满**。

    实现是 `offset + len(rows) < total`。写成 `len(rows) == limit` 也能过上面那条
    (9 行 / 每页 4:末页只有 1 行,两种写法答案相同),但在 total=8 / limit=4 /
    offset=4 这一档会谎报「还有下一页」—— 用户点进去看到一张空表。
    反方向也钉一次:第一页(offset=0)必须说还有。
    """
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        for i in reversed(range(8)):
            _profile(db, f"s-{i:02d}", mainstream=0.0)

    service = _service(repo)
    first = service.source_profiles(NB, limit=4, offset=0)
    last = service.source_profiles(NB, limit=4, offset=4)

    assert first.returned == 4 and first.limit == 4 and first.has_more is True
    assert last.returned == 4 and last.limit == 4
    assert last.total == 8
    assert last.has_more is False


def test_source_page_limit_is_clamped(repo):
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        _profile(db, "s-1", mainstream=0.1)

    page = _service(repo).source_profiles(NB, limit=10**9)

    assert page.limit == KG_SOURCE_PAGE_MAX


def test_store_clamps_the_source_page_even_when_the_service_is_bypassed(repo):
    """**store 侧**的 clamp 单独钉一次。

    上面那条走 service,而 service 自己先 clamp 过一遍 —— store 永远收不到大数字,
    于是「store 的 clamp」在那条测试下是**空的**:删掉它照样全绿。这里直接调 store,
    让那一层也变成载重件(纵深防御的每一层都得自己有守卫,否则它只是注释)。
    """
    with repo._write() as db:
        for i in range(KG_SOURCE_PAGE_MAX + 3):
            _profile(db, f"s-{i:04d}", mainstream=i / 1000.0)

    with repo._connect() as db:
        total, rows = repo._runtime.unified_kg.kg_source_profile_page(
            db, NB, limit=10**9, offset=0
        )

    assert total == KG_SOURCE_PAGE_MAX + 3
    assert len(rows) == KG_SOURCE_PAGE_MAX


def test_store_clamps_the_community_edge_top_n_even_when_the_service_is_bypassed(repo):
    """同上,跨板块边那一侧。service 先 clamp 过,所以 store 的 clamp 只有直接调它才
    测得到 —— 而预计算路径之外,任何新调用方都是直接调 store 的。"""
    with repo._write() as db:
        for i in range(KG_COMMUNITY_EDGES_MAX + 5):
            _edge(db, "cm-src", f"cm-{i:05d}", i)

    with repo._connect() as db:
        rows = repo._runtime.unified_kg.kg_community_edges_top(db, NB, 10**9)

    assert len(rows) == KG_COMMUNITY_EDGES_MAX
    assert [w for _s, _d, w in rows] == sorted(
        (w for _s, _d, w in rows), reverse=True)


@pytest.mark.parametrize(
    "relative_path, tie_break",
    [
        ("app/repositories/sqlite/unified_kg_store.py", "source_id ASC"),
        ("app/repositories/postgres/unified_kg_store.py", 'source_id COLLATE "C" ASC'),
    ],
)
def test_both_backends_keep_the_tie_break_on_the_outer_order_by(relative_path, tie_break):
    """`kg_source_profile_page` 的**外层** ORDER BY 也必须带并列消歧键。

    为什么这一条是**文本形状**守卫而不是行为守卫 —— 它是唯一诚实的做法:
    内层(子查询)的 `, source_id ASC` 决定**取哪一页**,行为上测得到(上面那条倒序
    插入的分页测试);外层的 `, p.source_id ASC` 只决定 LEFT JOIN **之后**的行序。
    SQLite 在 ≤200 行、走主键点查的 nested loop 上恰好保序,删掉它 42 条全绿;
    PostgreSQL 不保证 —— 规划器一旦在真实数据量上改选 hash join(并把已分页的 200 行
    放进哈希侧),输出顺序就跟着哈希桶走,同一页会以乱序返回给读者。
    要在测试里稳定逼出那个计划得去改 GUC(`enable_nestloop=off` 之类)并赌规划器
    版本,那种守卫比它守的代码还脆。所以这里直接钉住 SQL 的形状。

    两个后端一起钉,而且**内外两处分别断言**:只数出现次数的话,「把外层那句删掉、
    在内层重复一遍」这种移动式改动照样满足计数。
    """
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[1]
    source = (backend_root / relative_path).read_text(encoding="utf-8")
    start = source.index("def kg_source_profile_page(")
    end = source.index("def source_canonical_rows(", start)
    body = source[start:end]

    inner = f"ORDER BY mainstream_share {{direction}}, {tie_break}"
    outer = f"ORDER BY p.mainstream_share {{direction}}, p.{tie_break}"
    assert inner in body, f"{relative_path}:内层分页 ORDER BY 少了并列消歧键"
    assert outer in body, f"{relative_path}:外层 ORDER BY 少了并列消歧键"


def test_source_page_reports_orphan_source_rows(repo):
    """`source_id` 没有外键(历史清理会留下孤儿引用)。标题为空到底是「没标题」还是
    「这个来源已经不在了」,读报告的人有权分辨。"""
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        _source(db, "s-live", "活着的来源")
        _profile(db, "s-live", mainstream=0.2)
        _profile(db, "s-gone", mainstream=0.3)

    rows = {r["source_id"]: r for r in _service(repo).source_profiles(NB).rows}

    assert rows["s-live"]["title"] == "活着的来源"
    assert rows["s-live"]["source_missing"] is False
    assert rows["s-gone"]["title"] == ""
    assert rows["s-gone"]["source_missing"] is True


def test_source_page_absence_matches_the_overview(repo):
    """两个端点对「这份产物在不在」必须给同一个答案 —— 它们看的是同一行账本。"""
    with repo._write() as db:
        _state(db)
        _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload())
        _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload())
        _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload())
        _artifact(db, ARTIFACT_COMMUNITY_EDGES, _edges_payload(communities=0))

    service = _service(repo)
    overview_view = _artifact_of(service.overview(NB), ARTIFACT_SOURCE_PROFILES)
    page = service.source_profiles(NB)

    assert page.present is overview_view.present is False
    assert page.absence == overview_view.absence == ABSENCE_EXPECTED
    assert page.summary is None
    assert page.rows == []


def test_source_page_rejects_an_unknown_order(repo):
    with pytest.raises(ValueError, match="order"):
        _service(repo).source_profiles(NB, order="whatever")


# ------------------------------------------------------------------ 共享快照
# 两个端点各自的多条读必须看**同一份库**。裸 `connect()` 在两个后端上都是「每条语句
# 各取一个快照」(SQLite 自动提交 / PostgreSQL READ COMMITTED),而并发的预计算是把
# 三张产物表**整批重写** —— 提交劈在两次读之间,响应就会把两代库拼成一份。
#
# 下面两条把「并发提交」用一条**独立连接的真写事务**钉死在两次读的正中间,所以不靠
# 时序碰运气:判据是确定的,修复前必红、修复后必绿。


def _commit_from_another_connection(repo, mutate) -> None:
    """在读的正中间提交一次真写事务(独立连接,与读连接不共享快照)。"""
    with repo._write() as write_db:
        mutate(write_db)


def test_the_sources_page_reads_one_shared_snapshot(repo, monkeypatch):
    """codex 第 8 轮 P2:`/sources` 的四次读(state / 账本 / COUNT / 一页)看同一份库。

    没有共享快照时,并发预计算提交在中间会让 ``total`` 与 ``rows`` 对不上 —— 一份
    「有 N 个来源、这一页却是别的世代」的分页,读者无从分辨是并发还是数据坏了。
    """
    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)
        _profile(db, "s-1", mainstream=0.1)

    store = repo._runtime.unified_kg
    read_ledger = store.kg_analysis_artifact_rows

    def ledger_then_a_concurrent_precompute(db, notebook_id):
        ledger = read_ledger(db, notebook_id)
        _commit_from_another_connection(repo, lambda write_db: [
            write_db.execute(
                "DELETE FROM kg_source_profiles WHERE notebook_id=?", (NB,)),
            *[_profile(write_db, f"s-new-{index}", mainstream=0.9)
              for index in range(3)],
        ])
        return ledger

    monkeypatch.setattr(
        store, "kg_analysis_artifact_rows", ledger_then_a_concurrent_precompute
    )
    page = _service(repo).source_profiles(NB)

    assert (page.total, [row["source_id"] for row in page.rows]) == (1, ["s-1"]), (
        f"COUNT 与那一页看到了不同世代的库:total={page.total}, "
        f"rows={[row['source_id'] for row in page.rows]}"
    )


def test_the_overview_reads_the_state_row_and_the_ledger_from_one_snapshot(
    repo, monkeypatch
):
    """state 行与账本必须来自同一份库,否则板块那一格会凭空报一个**假异常**。

    板块的合并世代取自账本的 ``built_at_cluster_seq``,落后量却是拿 state 的
    ``cluster_mutation_seq`` 减出来的。两次读劈在一次并发预计算的两侧,减出来就是负数
    —— 而负数在本契约里的含义是「账本比库还新 = 库被手工改过」。凭空报一个假异常比
    不报更糟:这个视图的全部价值就是让读者能相信自己看到的标注。
    """
    with repo._write() as db:
        _state(db, seq=10, community_seq=10)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db, seq=10, cluster_seq=10)

    store = repo._runtime.unified_kg
    read_state = store.state_row

    def state_then_a_concurrent_precompute(db, notebook_id):
        row = read_state(db, notebook_id)
        _commit_from_another_connection(repo, lambda write_db: (
            write_db.execute(
                "UPDATE unified_kg_state SET cluster_mutation_seq=11 "
                "WHERE notebook_id=?", (NB,)),
            write_db.execute(
                "DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (NB,)),
            _seed_complete_ledger(write_db, seq=10, cluster_seq=11),
        ))
        return row

    monkeypatch.setattr(store, "state_row", state_then_a_concurrent_precompute)
    boards = _service(repo).overview(NB).boards.freshness

    assert (boards.built_at_cluster_seq, boards.cluster_seq_behind) == (10, 0), (
        "state 行与账本来自两代库:板块那一格报出了一个凭空的落后量"
    )
    assert boards.stale is False


def test_the_overview_reads_the_boards_from_the_snapshot_that_stamped_them(
    repo, monkeypatch
):
    """codex 第 12 轮 P2 的前一半:板块列表必须与新鲜度元数据来自**同一份库**。

    并发的社区重建是「整批重铸板块 id」。state 行与账本读在前、板块列表读在后,响应就
    会把**上一代的新鲜度戳**(``built_at_cluster_seq`` / ``stale``)盖在**新一代的板块
    id** 上 —— 读者看到的每一条标注都是在描述另一套板块,而这个视图的全部价值就是让人
    能相信自己看到的标注。

    探针放在账本读之后:那时快照(如果真开了)已经建立,重建提交在它之后,所以「板块
    列表落在了新一代」这件事只可能来自「板块列表没骑那个快照」。
    """
    with repo._write() as db:
        _state(db, seq=10, community_seq=10)
        _community(db, "cm-old", [("K1", 1.0)])
        _seed_complete_ledger(db, seq=10, created_at="2026-01-02T00:00:00")

    store = repo._runtime.unified_kg
    read_ledger = store.kg_analysis_artifact_rows

    def ledger_then_a_concurrent_board_recast(db, notebook_id):
        ledger = read_ledger(db, notebook_id)
        _commit_from_another_connection(repo, lambda write_db: (
            write_db.execute(
                "DELETE FROM community_members WHERE notebook_id=?", (NB,)),
            write_db.execute("DELETE FROM communities WHERE notebook_id=?", (NB,)),
            _community(write_db, "cm-new", [("K1", 1.0)]),
            write_db.execute(
                "DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (NB,)),
            _seed_complete_ledger(
                write_db, seq=10, created_at="2026-01-02T09:00:00"),
        ))
        return ledger

    monkeypatch.setattr(
        store, "kg_analysis_artifact_rows", ledger_then_a_concurrent_board_recast
    )
    overview = _service(repo).overview(NB)

    assert [c["id"] for c in overview.boards.payload["communities"]] == ["cm-old"], (
        "板块列表落在了重铸之后的库上,而新鲜度戳还是重铸之前那一代的 —— "
        "一份被盖了错世代戳的板块列表"
    )
    # 戳本身:上面那条断言之所以是「必须 cm-old」而不是「必须 cm-new」,就是因为这一份
    # 元数据已经在快照里定死了,板块列表只能配它这一代。
    assert overview.boards.freshness.built_at_cluster_seq == 10


def test_the_overview_reads_the_board_edges_from_the_same_snapshot_as_the_boards(
    repo, monkeypatch
):
    """codex 第 12 轮 P2 的后一半:跨板块边必须与板块列表来自**同一份库**。

    俯瞰图**照着 edges 画连线**,端点是板块 id。板块列表读在前、边读在后,并发重铸提交
    在中间,画出来的就是一整幅悬空连线 —— 每一条的两端都指向这份响应里根本不存在的板块。

    探针放在板块列表读之后,所以「边落在了新一代」只可能来自「边没骑板块列表那个快照」。
    """
    with repo._write() as db:
        _state(db)
        _community(db, "cm-a", [("K1", 1.0)])
        _community(db, "cm-b", [("K2", 1.0)])
        _seed_complete_ledger(db)
        _edge(db, "cm-a", "cm-b", 30)

    store = repo._runtime.unified_kg
    read_boards = store.community_overview_on

    def boards_then_a_concurrent_board_recast(db, notebook_id, **bounds):
        payload = read_boards(db, notebook_id, **bounds)
        _commit_from_another_connection(repo, lambda write_db: (
            write_db.execute(
                "DELETE FROM kg_community_edges WHERE notebook_id=?", (NB,)),
            _edge(write_db, "cm-x", "cm-y", 99),
        ))
        return payload

    monkeypatch.setattr(
        store, "community_overview_on", boards_then_a_concurrent_board_recast
    )
    view = _service(repo).overview(NB)

    boards = {c["id"] for c in view.boards.payload["communities"]}
    dangling = [
        edge for edge in view.board_edges.edges
        if edge["src"] not in boards or edge["dst"] not in boards
    ]
    assert dangling == [], (
        f"俯瞰图上出现了悬空连线:板块列表是 {sorted(boards)},边却指向 {dangling}"
    )
    assert [
        (edge["src"], edge["dst"], edge["weight"]) for edge in view.board_edges.edges
    ] == [("cm-a", "cm-b", 30)]


def test_each_endpoint_opens_exactly_one_shared_snapshot(repo):
    """一趟**一个**快照 —— 「每条读各开一个 `read_snapshot()`」和不开一样坏。

    强度如实说明,这条与上面几条互补、不能互相替代:
      · 它不依赖探针位置,所以「每条读各开一个」这一形态在这里是直接可数的;
      · 但它只看**本 service 开了几个**。store 自己在方法内部开的那种(自开快照的
        `community_overview`)在这里数不到 —— 那一档由上面两条行为守卫兜。
    """
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db)
        _edge(db, "cm-1", "cm-2", 20)
        _profile(db, "s-1", mainstream=0.1)

    spy = _SpyStore(repo._runtime.unified_kg)
    service = _service(repo, store=spy)

    service.overview(NB)
    assert spy.calls.count("read_snapshot") == 1, (
        f"总览的四条读没共用一个快照:{spy.calls}"
    )
    assert spy.calls.count("community_overview_on") == 1, (
        "板块列表没经 connection-taking 入口读进来(压根没读,或者走了自开快照的那个"
        "同名方法)—— 上面那个 1 是空数出来的"
    )

    service.source_profiles(NB)
    assert spy.calls.count("read_snapshot") == 2, (
        f"/sources 的四条读没共用一个快照:{spy.calls}"
    )


# ---------------------------------------------------------------- 记忆化


def test_overview_reuses_the_expensive_reads_while_the_signature_holds(repo):
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db)

    spy = _SpyStore(repo._runtime.unified_kg)
    service = _service(repo, store=spy)
    service.overview(NB)
    service.overview(NB)

    assert spy.calls.count("community_overview_on") == 1
    assert spy.calls.count("kg_community_edges_top") == 1
    assert spy.calls.count("state_row") == 2          # 签名每次现读


def test_overview_cache_invalidates_when_the_ledger_is_rewritten_at_the_same_seq(repo):
    """一次 ``force=True`` 的重建会在**同一个** kg_mutation_seq 上重铸板块 id 并整批
    重写产物。只把 seq 放进签名的话缓存不会失效,板块 id 会串到上一套 —— 所以
    ``created_at`` 必须进签名。这条就是那个变异的回归钉。"""
    with repo._write() as db:
        _state(db)
        _community(db, "cm-old", [("K1", 1.0)])
        _seed_complete_ledger(db, created_at="2026-01-02T00:00:00")

    spy = _SpyStore(repo._runtime.unified_kg)
    service = _service(repo, store=spy)
    first = service.overview(NB)

    with repo._write() as db:
        db.execute("DELETE FROM communities WHERE notebook_id=?", (NB,))
        db.execute("DELETE FROM community_members WHERE notebook_id=?", (NB,))
        db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (NB,))
        _community(db, "cm-new", [("K1", 1.0)])
        _seed_complete_ledger(db, created_at="2026-01-02T09:00:00")

    second = service.overview(NB)

    assert [c["id"] for c in first.boards.payload["communities"]] == ["cm-old"]
    assert [c["id"] for c in second.boards.payload["communities"]] == ["cm-new"]
    assert spy.calls.count("community_overview_on") == 2


@pytest.mark.parametrize("seed_stat_snapshots", [False, True], ids=["empty", "partial"])
def test_overview_refuses_to_cache_when_a_board_recast_would_not_move_it(
    repo, seed_stat_snapshots
):
    """账本里**一份依赖板块的产物都没有**时,同事务作废是个 no-op —— 板块 id 换了,
    签名一个字节没动。这一档必须**不写缓存**(codex 第 10 轮 P2)。

    上一条钉的是「账本行在,所以作废动得了签名」;这一条钉的是它的前提不成立的那一档:
    上一轮预计算失败(整个账本为空,或只剩三条与板块无关的统计快照)→ 一次
    ``force=True`` 在**同一个** kg_mutation_seq 上重铸板块 id、作废什么都没删 → 预计算
    又失败。此时签名逐字段相同,已预热的缓存会**无限期**继续吐上一套板块 id,直到 LRU
    淘汰或进程重启,而端点自称读的是 live 快照。

    走真 store 的发布协议(取号 → 写新代 → 指针翻转,批 3·W2)+
    `discard_board_dependent_kg_analysis_artifacts` 而不是手写 SQL:要复现的正是
    「作废在这一档是 no-op」,手写 DELETE 会把这个前提绕过去。
    """
    with repo._write() as db:
        _state(db)
        _community(db, "cm-old", [("K1", 1.0)])
        if seed_stat_snapshots:
            # 三条统计快照与板块无关,重铸时刻意不作废 —— 所以账本非空、却仍然没有
            # 任何一行会被那次作废动到。
            _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload())
            _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload())
            _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload())

    service = _service(repo)
    first = service.overview(NB)

    unified = repo._runtime.unified_kg
    with repo._write() as db:
        claim = unified.claim_derived_generation(db, NB, ttl_seconds=3600)
        assert claim is not None
        unified.write_communities_generation(
            db, NB, 0, [("cm-new", ["K1"])], {"K1": "name of K1"}, {"K1": 1.0},
            "2026-01-02T09:00:00", claim["generation"],
        )
        assert unified.flip_community_generation(
            db, NB, published_from=claim["community_generation"],
            generation=claim["generation"], now="2026-01-02T09:00:00",
        )
        unified.discard_board_dependent_kg_analysis_artifacts(db, NB)
        unified.release_derived_claim(db, NB, claim["generation"])

    second = service.overview(NB)

    assert [c["id"] for c in first.boards.payload["communities"]] == ["cm-old"]
    assert [c["id"] for c in second.boards.payload["communities"]] == ["cm-new"], (
        "板块被重铸,而账本里没有一行会被同事务作废动到 —— 签名没动,缓存无限期"
        "继续吐上一套板块 id"
    )


def test_overview_still_caches_while_one_board_dependent_row_survives(repo):
    """反向钉:判据是「有没有会被作废动到的行」,不是「账本齐不齐」。

    少了来源画像的账本是 `partial`(而且 `absence` 报红),但跨板块边那一行还在 ——
    重铸时它会被同事务删掉、签名跟着动,所以这一档**照常缓存**。把判据写成「账本齐全
    才缓存」会在这里白付一次板块列表读(生产 88 580 个板块、`communities` 上没有 size
    索引),而那正是这份记忆化存在的理由。
    """
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _artifact(db, ARTIFACT_CLUSTER_HISTOGRAM, _histogram_payload())
        _artifact(db, ARTIFACT_LARGEST_CLUSTERS, _largest_payload())
        _artifact(db, ARTIFACT_RELATION_PROVENANCE, _relation_payload())
        _artifact(db, ARTIFACT_COMMUNITY_EDGES, _edges_payload())

    spy = _SpyStore(repo._runtime.unified_kg)
    service = _service(repo, store=spy)
    first = service.overview(NB)
    service.overview(NB)

    assert first.ledger_state == LEDGER_PARTIAL, "夹具没落在 partial 档,这条就白测了"
    assert spy.calls.count("community_overview_on") == 1


def test_overview_cache_is_keyed_on_the_request_bounds(repo):
    """不同的 limit 不能互相串用缓存(否则调大 limit 会拿回上次那份更短的列表)。"""
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _community(db, "cm-2", [("K2", 1.0)])
        _seed_complete_ledger(db)

    service = _service(repo)

    assert service.overview(NB, board_limit=1).boards.payload["returned"] == 1
    assert service.overview(NB, board_limit=2).boards.payload["returned"] == 2


def test_overview_cache_is_bounded(repo):
    """参数来自查询串,键的基数由客户端决定 —— LRU 上界是防「有人把 limit 从 1 试到
    200」把进程内存撑爆的唯一手段。"""
    from app.services.kg_analysis import _OVERVIEW_CACHE_MAX

    with repo._write() as db:
        _state(db)
        _seed_complete_ledger(db)

    service = _service(repo)
    for limit in range(1, _OVERVIEW_CACHE_MAX + 12):
        service.overview(NB, edge_limit=limit)

    assert len(service._overview_cache) == _OVERVIEW_CACHE_MAX


def test_overview_cache_is_thread_safe(repo):
    """缓存跟着 runtime 单例跨请求存活,而 FastAPI 的同步路由跑在线程池里。"""
    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db)

    service = _service(repo)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            for _ in range(20):
                service.overview(NB)
        except BaseException as exc:   # noqa: BLE001 — 线程里的异常要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


# --------------------------------------------------------------- 端点契约


def test_response_models_accept_the_service_dataclasses(repo):
    """路由就是 `Model(**asdict(result))` —— 这条钉住 dataclass 与 pydantic 模型不漂移
    (漏一个字段的表现是 500,而且只在真跑过这条路径时才暴露)。"""
    from dataclasses import asdict

    with repo._write() as db:
        _state(db)
        _community(db, "cm-1", [("K1", 1.0)])
        _seed_complete_ledger(db)
        _edge(db, "cm-1", "cm-2", 20)
        _source(db, "s-1", "标题")
        _profile(db, "s-1", mainstream=0.1)

    service = _service(repo)
    body = KgAnalysisResponse(**asdict(service.overview(NB)))
    page = SourceProfilePageResponse(**asdict(service.source_profiles(NB)))

    assert body.notebook_id == NB
    assert len(body.artifacts) == len(ARTIFACT_KINDS)
    assert body.board_edges.edges[0].weight == 20
    assert page.rows[0].title == "标题"
    assert page.units == SOURCE_PAGE_UNITS


def test_endpoint_query_bounds_match_the_contract_ceilings():
    """端点的 `le=` 必须钉在契约常量上。掉了 `le=` 的表现是「传多大都收」,而 store 侧
    的 clamp 会把它悄悄压回去 —— 用户以为拿到了 5000 条,实际只有 2000,没有任何提示。
    """
    import inspect

    import annotated_types

    from app.api.kg_routes import kg_analysis_overview, kg_analysis_sources

    def bound(function, name: str, kind) -> object:
        """FastAPI 的 Query 把 ge/le 存在 `metadata` 里(annotated_types),不是属性。
        取不到就返回 None —— 那正是「`le=` 被删掉」的表现,断言会当场报红。"""
        parameter = inspect.signature(function).parameters[name].default
        return next(
            (getattr(item, kind.__name__.lower())
             for item in getattr(parameter, "metadata", [])
             if isinstance(item, kind)),
            None,
        )

    assert bound(kg_analysis_overview, "boards", annotated_types.Le) == (
        COMMUNITY_OVERVIEW_MAX)
    assert bound(kg_analysis_overview, "top_members", annotated_types.Le) == (
        COMMUNITY_TOP_MEMBERS_MAX)
    assert bound(kg_analysis_overview, "edges", annotated_types.Le) == (
        KG_COMMUNITY_EDGES_MAX)
    assert bound(kg_analysis_sources, "limit", annotated_types.Le) == KG_SOURCE_PAGE_MAX
    assert bound(kg_analysis_sources, "offset", annotated_types.Ge) == 0


def test_endpoints_are_read_guarded():
    """只读端点用 `require_notebook_read`(只读成员也能看),不是 owner-only 的
    `require_notebook_access`。"""
    from app.api import kg_routes
    from app.api.deps import require_notebook_read

    routes = {
        "/notebooks/{notebook_id}/kg-analysis",
        "/notebooks/{notebook_id}/kg-analysis/sources",
    }
    guarded = {
        route.path: {d.dependency for d in route.dependencies}
        for route in kg_routes.router.routes
        if getattr(route, "path", None) in routes
    }

    assert set(guarded) == routes
    for path, dependencies in guarded.items():
        assert require_notebook_read in dependencies, path


# --------------------------------------------------------------------- 工具

_ANALYSIS_TABLES = (
    "kg_analysis_artifacts",
    "kg_community_edges",
    "kg_source_profiles",
    "communities",
    "community_members",
    "unified_kg_state",
)

_DML_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
              "TRUNCATE", "VACUUM")


def _table_fingerprints(db) -> dict:
    """每张表的 (行数, 全表内容指纹)。行数不变但内容被改写(UPDATE)时也会变,而且
    它抓得到**经另一条连接**发生的写 —— trace 看不见那些。"""
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
