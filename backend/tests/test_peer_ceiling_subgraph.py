"""PR-D0 任务 D0-1:联邦 KG 的 1-hop 扩展节点必须过逐库来源天花板。

`source_scope.scoped_subgraph_nodes` 只判库维度——图节点载荷是
`{object_id, object_type, name, tier, notebook_id}`,不带 `source_id`,所以
「这个节点有没有被它自己那一库天花板之内的来源支撑」它无从回答。种子那一侧
已经安全(`federated_retrieve` 经 `filter_retrieval_items` 的 knowledge 支按
每条命中自己那一库的天花板复核过);1-hop **扩展**出来的节点不是:
`render_subgraph_context` 会把它的**名字**与入边的第一条证据**引文**写进提示词、
并铸一个活的 `k{n}` 锚点。本文件钉住
`CandidateRetrievalService._ceiling_scoped_subgraph` 关上这个口之后的六条不变量:

* 只由天花板外来源支撑的扩展节点,名字与引文都不进 `kg_block`/`kg_id_map`;
* 一条边上天花板内外各一条 evidence 时,渲染出的引文只来自天花板内那条;
* 合法种子不被这道闸误杀(它有天花板内的证据,所以不需要豁免);
* 不设逐库天花板时输出逐值相等,且新裁剪函数与它的那次批量读**都没有发生**;
* 子图取自进程级缓存的联邦图,裁剪绝不就地改——同一子图连取两次,第二次仍完整;
* `frozenset()` 是「冻结为零来源」的显式拒绝(不是「无天花板」),该库节点全丢。

生产上今天不可达(没有任何地方构造 `notebook_source_ceilings`),所以入口是
`source_scope_context(active, None, None, notebook_source_ceilings={...})`。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedKnowledge
from app.models.common import Evidence
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


_NOW = "2026-09-20T00:00:00+00:00"

# 天花板之外的来源(参考库自己的隐藏 Memory 投影)支撑的那个扩展节点。
HIDDEN_NAME = "隐藏投影实体"
HIDDEN_QUOTE = "只有隐藏投影才有的引文"
# 天花板之内的来源支撑的扩展节点,以及它入边上正反两条 evidence 的引文。
VISIBLE_NAME = "可见邻居实体"
DENIED_QUOTE = "天花板之外的那条引文"
ALLOWED_QUOTE = "天花板之内的那条引文"
SEED_NAME = "参考库种子实体"
ACTIVE_SEED_NAME = "当前库种子实体"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    instance.settings.query_rewrite_enabled = False
    return instance


def _seed_source(repo, notebook_id: str, source_id: str, *,
                 source_type: str = "markdown") -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, source_type, "ready",
             "parsed", _NOW, _NOW),
        )


def _evidence_json(pairs) -> str:
    """``[(source_id, quote), ...]`` → knowledge_* 表里 evidence 列的 JSON。"""
    return json.dumps([
        {"source_id": source_id, "source_title": source_id,
         "element_id": f"el-{source_id}-{index}", "element_type": "paragraph",
         "location_label": "p", "quote": quote, "quoted_span": quote,
         "confidence": 1.0}
        for index, (source_id, quote) in enumerate(pairs)
    ])


def _seed_object(repo, notebook_id: str, object_id: str, name: str,
                 evidence_pairs) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
            "owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (object_id, notebook_id, "concept", "approved", "",
             json.dumps({"name": name}), _evidence_json(evidence_pairs),
             evidence_pairs[0][0] if evidence_pairs else None, _NOW, _NOW),
        )


def _seed_relation(repo, notebook_id: str, relation_id: str,
                   source_object_id: str, target_object_id: str,
                   evidence_pairs) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (relation_id, notebook_id, None, source_object_id,
             target_object_id, "kind_of", _evidence_json(evidence_pairs),
             _NOW),
        )


@pytest.fixture
def federation(repo):
    """(active, peer) 两库,peer 已挂载;种子在两库各一,扩展节点都在 peer。

    peer 的两篇来源:``src-peer-ok``(普通可见)与 ``src-peer-mem``(该库自己的
    隐藏 Memory 投影,典型的「天花板之外」)。

    图形状(全部 concept --kind_of--> concept,边都在 peer 名下):

        o-peer-seed ──r-hidden(ev: src-peer-mem)──> o-peer-hidden
                    └─r-visible(ev: src-peer-mem, src-peer-ok)──> o-peer-visible

    `o-peer-hidden` 只有 ``src-peer-mem`` 一条证据 → 天花板在绑时整条丢弃;
    `o-peer-visible` 有 ``src-peer-ok`` 的证据 → 留下,但入边证据要被收窄。
    """
    peer = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(peer.id)
    active = repo.create_notebook(NotebookCreate(name="我的笔记本"))
    repo.replace_notebook_bases(active.id, [peer.id], "user-local")
    repo.collection_catalog.invalidate()

    _seed_source(repo, active.id, "src-act")
    _seed_object(repo, active.id, "o-act-seed", ACTIVE_SEED_NAME,
                 [("src-act", "当前库引文")])

    _seed_source(repo, peer.id, "src-peer-ok")
    _seed_source(repo, peer.id, "src-peer-mem", source_type="memory")
    _seed_object(repo, peer.id, "o-peer-seed", SEED_NAME,
                 [("src-peer-ok", "种子引文")])
    _seed_object(repo, peer.id, "o-peer-hidden", HIDDEN_NAME,
                 [("src-peer-mem", HIDDEN_QUOTE)])
    _seed_object(repo, peer.id, "o-peer-visible", VISIBLE_NAME,
                 [("src-peer-ok", "邻居自己的引文")])
    _seed_relation(repo, peer.id, "r-hidden", "o-peer-seed", "o-peer-hidden",
                   [("src-peer-mem", HIDDEN_QUOTE)])
    _seed_relation(repo, peer.id, "r-visible", "o-peer-seed", "o-peer-visible",
                   [("src-peer-mem", DENIED_QUOTE),
                    ("src-peer-ok", ALLOWED_QUOTE)])
    return active.id, peer.id


def _seed_hits(active: str, peer: str):
    """两条种子命中(当前库一条、参考库一条),形状与 ``federated_retrieve`` 一致。"""
    def _hit(object_id: str, name: str, source_id: str, notebook_id: str):
        return RetrievedKnowledge(
            object_id=object_id, object_type="concept",
            payload={"name": name},
            evidence=[Evidence(
                source_id=source_id, source_title=source_id,
                element_id=f"el-{source_id}-0", element_type="paragraph",
                location_label="p", quoted_span="种子", confidence=1.0,
            )],
            notebook_id=notebook_id,
        )

    return [
        _hit("o-act-seed", ACTIVE_SEED_NAME, "src-act", active),
        _hit("o-peer-seed", SEED_NAME, "src-peer-ok", peer),
    ]


@pytest.fixture
def overlay(repo, federation, monkeypatch):
    """``ceilings -> (block, id_map)``,种子腿钉死,其余全走真实 SQLite/真实图。

    只桩种子腿(``federated_retrieve``/``federated_retrieve_relations``),因为
    它们是本用例不关心的那一半;被测的 1-hop 走查、进程级缓存的联邦图、以及
    裁剪函数用到的 evidence 批量反查全部是真的。
    """
    active, peer = federation
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(
        candidates, "federated_retrieve",
        lambda *args, **kwargs: _seed_hits(active, peer),
    )
    monkeypatch.setattr(
        candidates, "federated_retrieve_relations",
        lambda *args, **kwargs: [],
    )

    def _run(ceilings=None):
        with source_scope_context(
            active, None, None, notebook_source_ceilings=ceilings,
        ):
            block, id_map, _hits, _supports = candidates._chunk_kg_overlay(
                active, "问题", "", 1000,
            )
        return block, id_map

    return _run


def _names(id_map) -> set:
    return {str(row.get("name") or "") for row in id_map.values()}


def _full_ceilings(active: str, peer: str) -> dict:
    """写入方(PR-D 的全局问答)将来安装的形状:每个参与库各一份冻结清单。"""
    return {active: frozenset({"src-act"}),
            peer: frozenset({"src-peer-ok"})}


# ---------------------------------------------------------------------------
# 1. 只由天花板外来源支撑的扩展节点,名字与引文都不进提示词
# ---------------------------------------------------------------------------


def test_expanded_node_outside_ceiling_never_reaches_prompt(
    overlay, federation,
):
    active, peer = federation

    baseline_block, baseline_id_map = overlay()
    # 对照臂:没有天花板时它确实会进提示词 —— 否则下面的断言可以凭空成立。
    assert HIDDEN_NAME in baseline_block
    assert HIDDEN_QUOTE in baseline_block
    assert HIDDEN_NAME in _names(baseline_id_map)

    block, id_map = overlay(_full_ceilings(active, peer))

    assert HIDDEN_NAME not in block
    assert HIDDEN_QUOTE not in block
    assert HIDDEN_NAME not in _names(id_map)
    assert all(
        row["object_id"] != "o-peer-hidden" for row in id_map.values()
    )
    # 引文也不会从 id_map 的 snippet 里漏出去(锚点是活的,前端会显示它)。
    assert all(
        HIDDEN_QUOTE not in str(row.get("snippet") or "")
        for row in id_map.values()
    )


# ---------------------------------------------------------------------------
# 2. 边证据被收窄到天花板之内
# ---------------------------------------------------------------------------


def test_edge_evidence_is_narrowed_to_the_ceiling(overlay, federation):
    """同一条边上天花板内外各一条 evidence:渲染只取第一条,所以「收窄」与
    「整条留着」在输出上是可区分的。"""
    active, peer = federation

    baseline_block, _baseline_id_map = overlay()
    assert DENIED_QUOTE in baseline_block and ALLOWED_QUOTE not in baseline_block

    block, id_map = overlay(_full_ceilings(active, peer))

    assert VISIBLE_NAME in block, "天花板内有支撑的邻居必须留下"
    assert DENIED_QUOTE not in block
    assert ALLOWED_QUOTE in block
    snippet = next(
        row["snippet"] for row in id_map.values()
        if row["object_id"] == "o-peer-visible"
    )
    assert snippet == ALLOWED_QUOTE


# ---------------------------------------------------------------------------
# 3. 合法种子不被误杀
# ---------------------------------------------------------------------------


def test_seed_nodes_survive(overlay, federation):
    """种子刻意**不**豁免:它经 `filter_retrieval_items` 复核过,必有天花板内的
    证据,所以这道闸本来就留得住它。豁免反而会在那道复核哪天变形时留一个洞。"""
    active, peer = federation

    block, id_map = overlay(_full_ceilings(active, peer))

    assert SEED_NAME in block
    assert ACTIVE_SEED_NAME in block
    assert {"o-peer-seed", "o-act-seed"} <= {
        row["object_id"] for row in id_map.values()
    }


# ---------------------------------------------------------------------------
# 4. 不设天花板 → 逐值相等,且裁剪整段没有执行
# ---------------------------------------------------------------------------


def test_absent_ceiling_is_byte_identical(
    repo, overlay, federation, monkeypatch,
):
    active, peer = federation
    candidates = repo.retrieval.candidates
    prune_calls: list = []
    evidence_reads: list = []
    real_prune = candidates._ceiling_scoped_subgraph
    real_rows = candidates.knowledge.object_evidence_rows
    monkeypatch.setattr(
        candidates, "_ceiling_scoped_subgraph",
        lambda *args, **kwargs: (
            prune_calls.append(1), real_prune(*args, **kwargs)
        )[1],
    )
    monkeypatch.setattr(
        candidates.knowledge, "object_evidence_rows",
        lambda db, object_ids: (
            evidence_reads.append(list(object_ids)), real_rows(db, object_ids)
        )[1],
    )

    first_block, first_id_map = overlay()
    second_block, second_id_map = overlay(None)

    assert first_block == second_block
    assert first_id_map == second_id_map
    # 裁剪函数根本没有被调用,那次批量读当然也没有发生。
    assert prune_calls == []
    assert evidence_reads == []
    # 对照臂:装上天花板之后两者都发生,证明上面的计数不是永远为空的空转。
    overlay(_full_ceilings(active, peer))
    assert prune_calls == [1]
    assert len(evidence_reads) == 1


# ---------------------------------------------------------------------------
# 5. 进程级缓存不被就地改
# ---------------------------------------------------------------------------


def test_process_cache_is_not_mutated(overlay, federation):
    """联邦图 memo 在 ``_vector_cache`` 里、键与 scope 无关,所以裁剪必须拷贝。

    就地过滤边证据(或删节点)会把这一次请求的天花板烙进缓存,泄漏给此后
    同一进程里的**每一次**问答。
    """
    active, peer = federation

    scoped_block, _scoped_id_map = overlay(_full_ceilings(active, peer))
    assert HIDDEN_NAME not in scoped_block and DENIED_QUOTE not in scoped_block

    later_block, later_id_map = overlay()

    # 同一份缓存图,第二次不带天花板 → 被裁掉的节点与被收窄掉的边证据都还在。
    assert HIDDEN_NAME in later_block
    assert HIDDEN_QUOTE in later_block
    assert DENIED_QUOTE in later_block
    assert HIDDEN_NAME in _names(later_id_map)


# ---------------------------------------------------------------------------
# 6. 空天花板是显式拒绝,不是「无天花板」
# ---------------------------------------------------------------------------


def test_empty_ceiling_denies_the_library(overlay, federation):
    active, peer = federation

    block, id_map = overlay({active: frozenset({"src-act"}),
                             peer: frozenset()})

    assert ACTIVE_SEED_NAME in block, "当前库的天花板没有变,它不受影响"
    for name in (SEED_NAME, VISIBLE_NAME, HIDDEN_NAME):
        assert name not in block
    assert all(
        not str(row["object_id"]).startswith("o-peer-")
        for row in id_map.values()
    )


# ---------------------------------------------------------------------------
# 7. 有别的库的天花板、但本 run 的库一个都没被点名 → 整段不生效、零查询
# ---------------------------------------------------------------------------


def test_a_run_whose_own_libraries_have_no_ceiling_reads_nothing(
    repo, overlay, federation, monkeypatch,
):
    """``peer_ceiling_active`` 为真只说明「这个 run 上有某个库被冻结了」。

    判据必须逐 owner 问 ``source_ceiling_for(owner) is not None``:本 run 走到的
    两个库都没有被点名时,节点一个都不许被裁,那次批量 evidence 读也一次都不许
    发出去 —— 否则「装了天花板的 run」会替**每一个**库付一次读,而那些库压根
    不在天花板清单里。
    """
    active, peer = federation
    candidates = repo.retrieval.candidates
    evidence_reads: list = []
    real_rows = candidates.knowledge.object_evidence_rows
    monkeypatch.setattr(
        candidates.knowledge, "object_evidence_rows",
        lambda db, object_ids: (
            evidence_reads.append(list(object_ids)), real_rows(db, object_ids)
        )[1],
    )

    baseline_block, baseline_id_map = overlay()
    assert evidence_reads == [], "对照臂本身不该读"

    block, id_map = overlay({"nb-somewhere-else": frozenset({"whatever"})})

    assert block == baseline_block
    # ⚠ 只比这道闸自己拥有的东西(留下哪些节点、各自的引文)。``id_map`` 整体
    # 不可比:任何逐库天花板在场都会让 ``citation_active_id`` 进对等模式,当前库
    # 那条引用的 ``notebook_id`` 徽章从空串变成真实 id —— 那是 D0-4 的既定语义,
    # 与本闸无关。
    def _content(rows):
        return {(row["object_id"], row["name"], row["snippet"])
                for row in rows.values()}

    assert _content(id_map) == _content(baseline_id_map)
    assert HIDDEN_NAME in block, "没有天花板绑住这两个库,谁都不该被裁"
    assert evidence_reads == []

    # 对照臂:真的点名了本 run 的库之后,读与裁剪都发生。
    overlay(_full_ceilings(active, peer))
    assert len(evidence_reads) == 1


# ---------------------------------------------------------------------------
# 8. 批量 evidence 读必须分批:节点数是可调环境变量的函数,不是协议常量
# ---------------------------------------------------------------------------


def _governed_subgraph(peer: str, count: int) -> list:
    """``count`` 个同属 ``peer`` 的扩展节点(形状同 ``multihop_subgraph`` 的输出)。"""
    return [
        ({"object_id": f"o-bulk-{index}", "object_type": "concept",
          "name": f"bulk-{index}", "tier": "base", "notebook_id": peer},
         None, None)
        for index in range(count)
    ]


def test_bulk_evidence_read_is_batched_and_survives_the_variable_limit(
    repo, federation, monkeypatch,
):
    """节点数 > 一个批次时分批读,且结果与不分批逐值相等。

    ``chunk_kg_max_depth`` / ``chunk_kg_fan_out`` 是**部署可调的环境变量**,
    所以「走查自己的上限已经约束了它」约束的是配置、不是协议常量:调大之后
    一次性 ``IN (?,…)`` 会撞上 ``SQLITE_MAX_VARIABLE_NUMBER``,异常从
    ``_chunk_kg_overlay`` 一路冒到 ``_mix_retrieve`` —— 整条臂失败,不是降级。

    用例把本线程那条复用连接的变量上限调到 SQLite 的历史默认 999(本机构建
    编译出来的上限是 25 万,不调的话这条路根本压不出来),再喂进 2 个批次的
    节点。

    **变异锚点**:去掉 ``_in_batches`` 改回一次性 ``list(owners)`` →
    ``OperationalError: too many SQL variables``。
    """
    import sqlite3

    _active, peer = federation
    candidates = repo.retrieval.candidates
    count = candidates._IN_CHUNK * 2
    subgraph = _governed_subgraph(peer, count)

    connection = candidates.database.connect()
    previous = connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    batches: list[int] = []
    real_rows = candidates.knowledge.object_evidence_rows

    def _counting(db, object_ids):
        ids = list(object_ids)
        batches.append(len(ids))
        return real_rows(db, ids)

    monkeypatch.setattr(
        candidates.knowledge, "object_evidence_rows", _counting)
    try:
        with source_scope_context(
            _active, None, None,
            notebook_source_ceilings={peer: frozenset({"src-peer-ok"})},
        ):
            from app.services.source_scope import current_source_scope

            kept = candidates._ceiling_scoped_subgraph(
                subgraph, current_source_scope())
    finally:
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)

    assert len(batches) == 2 and batches == [candidates._IN_CHUNK] * 2
    assert max(batches) <= candidates._IN_CHUNK
    # 这批节点一个都没有真实 evidence 行 → 全部无天花板内支撑 → 全裁掉。
    # 与「不分批时同一份输入」的结果逐值相等由下一条对照臂钉住。
    assert kept == []


def test_batched_and_unbatched_reads_agree_value_for_value(repo, federation):
    """分批只改查询次数,不改结果:跨批次的「有支撑 / 无支撑」判定必须一致。

    造一批节点,其中跨越批次边界的若干个有天花板内来源的真实 evidence 行,
    断言幸存集合恰好是它们 —— 一个「只处理第一批」的实现会漏掉后面那些。
    """
    _active, peer = federation
    candidates = repo.retrieval.candidates
    count = candidates._IN_CHUNK + 5
    supported_ids = {f"o-bulk-{index}"
                     for index in (0, candidates._IN_CHUNK - 1,
                                   candidates._IN_CHUNK, count - 1)}
    for object_id in sorted(supported_ids):
        _seed_object(repo, peer, object_id, object_id,
                     [("src-peer-ok", f"{object_id} 的引文")])

    subgraph = _governed_subgraph(peer, count)
    with source_scope_context(
        _active, None, None,
        notebook_source_ceilings={peer: frozenset({"src-peer-ok"})},
    ):
        from app.services.source_scope import current_source_scope

        kept = candidates._ceiling_scoped_subgraph(
            subgraph, current_source_scope())

    assert {str(node["object_id"]) for node, _edge, _src in kept} == supported_ids
