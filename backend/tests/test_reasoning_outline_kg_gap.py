"""大纲便签的 KG 弱支撑边回喂(PR-4 G1,设计文档 §3.3)。

覆盖:探测的有界性与阈值、显示名解析的优先级与丢弃、run 级去重账目、便签段的
行/段字符界与条数上限、展示顺序、trace detail、双闸的关闭态零查询逐字回归,
以及一条 e2e:模型绑定证据后,下一轮 prompt 真的拿到了对应的弱支撑边提示。

PostgreSQL parity 用例在 `backend/tests/postgres/test_knowledge_store_conformance.py`
(那里是双后端 store 语义的同构位置)。
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import (
    OUTLINE_ACTION,
    ReasoningRetriever,
    _KG_GAP_EDGE_CHARS,
    _KG_GAP_NOTE_CHARS,
    _KG_GAP_NOTE_HEAD,
    _KG_GAP_NOTE_LINE_CHARS,
    _KG_GAP_NOTE_LINES,
    _MAX_OUTLINE_UPDATES,
    _outline_note,
    kg_gap_note_segment,
)
from app.services.retrieval import GapRelationRow
from app.services.retrieval_candidates import (
    _KG_GAP_MAX_SEEDS,
    _KG_GAP_PROBE_LIMIT,
    _KG_GAP_SOURCE_MAX,
    _first_relation_sample,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-07-30T00:00:00+08:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    return instance


def _source(repo, notebook_id, source_id):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,summary,created_at,updated_at) "
            "VALUES (?,?,?,'markdown','extracted','extracted','',?,?)",
            (source_id, notebook_id, source_id, NOW, NOW))


def _concept(local_id, name):
    return {"local_id": local_id, "object_type": "concept",
            "payload": {"name": name, "section_path": "1"}, "evidence": []}


def _edge(source_local, target_local, edge_type="kind_of"):
    return {"source_local_id": source_local, "target_local_id": target_local,
            "edge_type": edge_type, "evidence": []}


def _seed(repo, *, well_supported_sources=3):
    """一个 notebook:锚点概念「版图设计」有两条出边。

    * `kind_of → 寄生电容` 只有 1 个来源撑着 —— 弱支撑,应当被探到;
    * `part_of → 工艺角` 由 `well_supported_sources` 个来源撑着 —— 默认 3,
      超过阈值,不该被探到(这就是阈值用例的对照组)。

    来源 id 带 notebook 前缀,好让同一个 repo 里种两个对照库(开关的 A/B 对照
    必须跑在同一份数据上,换一份数据的对照证明不了任何事)。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    first = _source_id(notebook.id, 1)
    _source(repo, notebook.id, first)
    repo.store_kg(notebook.id, first, [
        _concept("A", "版图设计"), _concept("B", "寄生电容"),
        _concept("C", "工艺角"),
    ], [_edge("A", "B", "kind_of"), _edge("A", "C", "part_of")])
    for index in range(2, well_supported_sources + 1):
        source_id = _source_id(notebook.id, index)
        _source(repo, notebook.id, source_id)
        repo.store_kg(notebook.id, source_id, [
            _concept("A", "版图设计"), _concept("C", "工艺角"),
        ], [_edge("A", "C", "part_of")])
    repo.rebuild_unified_kg(notebook.id)
    return notebook


def _source_id(notebook_id, index):
    return f"{notebook_id}-s{index}"


def _object_ids(repo, notebook_id, name):
    with repo._connect() as db:
        return [
            row["id"] for row in db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? "
                "ORDER BY id", (notebook_id,))
            if json.loads(row["payload"]).get("name") == name
        ]


class _SeqLLM:
    """plan 固定;reflect 按序返回(耗尽后默认 answer)。条目可以是接收本轮
    prompt 正文的可调用对象 —— 「模型只能引用它看到的候选 id」这条合同只有让
    脚本真的从 prompt 里读出 id 才测得到。"""

    configured = True

    def __init__(self, reflects, plan=None):
        self._plan = plan or {"sub_queries": [{"query": "版图设计"}]}
        self._reflects = list(reflects)
        self.reflect_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        self.reflect_prompts.append(content)
        if self._reflects:
            reply = self._reflects.pop(0)
            if callable(reply):
                reply = reply(content)
            return json.dumps(reply)
        return json.dumps({"next_action": "answer", "sufficient": True})


ANSWER = {"next_action": "answer", "sufficient": True}


def _outline_action(sections, **extra):
    return {"next_action": OUTLINE_ACTION, "reason": "整理大纲",
            "outline": {"sections": sections}, **extra}


def _retriever(repo, llm, *, effort="exhaustive"):
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    return retriever, ask_retrieval_limits(effort)


def _steps(result, step_type):
    return [step for step in result.trace if step.step_type == step_type]


def _row(src="源", tgt="目标", *, sources=1, edge="kind_of",
         canonical_src="", canonical_tgt=""):
    return GapRelationRow(
        canonical_src=canonical_src or f"K-{src}",
        canonical_tgt=canonical_tgt or f"K-{tgt}",
        src_name=src, tgt_name=tgt, edge_type=edge, source_count=sources,
    )


def _set_counts(repo, notebook_id, canonical_tgt, *, support, sources):
    """直接改写一条 canonical 边的两个计数。

    `store_kg` 造不出「多行、单源」那种形状(一篇文档里的重复关系会被折叠),而
    那恰好是本特性最要救的一类边,所以夹具直接落到列上。
    """
    with repo._write() as db:
        db.execute(
            "UPDATE canonical_relations SET support_count=?, source_count=? "
            "WHERE notebook_id=? AND canonical_tgt=?",
            (support, sources, notebook_id, canonical_tgt))


# ------------------------------------------------------------- 探测层(服务)

def test_the_documented_bounds_are_the_ones_in_the_code():
    """契约里的数字与代码里的常量对上。

    单独钉一条,是因为下面的用例大多拿常量本身当期望值 —— 只改常量的话它们会
    跟着一起动、全绿放行(「替换打空」)。数字必须在某一处以字面量出现。
    """
    assert (_KG_GAP_SOURCE_MAX, _KG_GAP_PROBE_LIMIT, _KG_GAP_MAX_SEEDS) == (
        2, 24, 96)
    assert (_KG_GAP_NOTE_LINES, _KG_GAP_NOTE_LINE_CHARS, _KG_GAP_NOTE_CHARS,
            _KG_GAP_EDGE_CHARS) == (6, 80, 520, 24)


def test_probe_returns_only_weakly_supported_edges(repo):
    """阈值只放支撑数 ≤2 的边。

    失败场景:阈值失效(或写成 `<`/`>=`)时,一条被 3 篇文档反复印证的边会跟着
    进提示 —— 那正好是**最不**需要补证的方向,而它挤掉的是真正的缺口。
    """
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert [(row.src_name, row.edge_type, row.tgt_name, row.source_count)
            for row in rows] == [("版图设计", "kind_of", "寄生电容", 1)]


def test_probe_accepts_the_threshold_boundary(repo):
    """边界本身在内:恰好 2 个来源支撑的边仍是「弱支撑」。"""
    notebook = _seed(repo, well_supported_sources=2)
    anchors = _object_ids(repo, notebook.id, "版图设计")

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert sorted((row.tgt_name, row.source_count) for row in rows) == [
        ("寄生电容", 1), ("工艺角", 2)]
    assert all(row.source_count <= _KG_GAP_SOURCE_MAX for row in rows)


def test_the_threshold_counts_sources_not_relation_rows(repo):
    """判据是**来源数**,不是聚合掉的原始关系行数。

    失败场景(这是本特性最要救的那批边):别名归一与 claim 聚簇会让同一篇文档里
    的好几条原始关系折叠进同一条 canonical 边,于是一条**单源**边攒出 5 行
    `support_count`。按行数过滤恰好把它滤掉,而它正是「只有一篇文献提到、最该补证」
    的那条;反过来,一条 5 篇文献都印证过的边只要行数少就会被当成缺口塞进提示。
    """
    notebook = _seed(repo, well_supported_sources=2)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    # 多行、单源 → 必须**入选**(行数 9 远超阈值,来源数 1 在阈值内)。
    _set_counts(repo, notebook.id, "K-寄生电容", support=9, sources=1)
    # 少行、多源 → 必须**落选**(行数 1 在阈值内,来源数 9 超阈值)。
    _set_counts(repo, notebook.id, "K-工艺角", support=1, sources=9)

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert [(row.tgt_name, row.source_count) for row in rows] == [("寄生电容", 1)]


def test_the_rendered_count_is_the_source_count(repo):
    """`(支撑N)` 里的 N 是来源数,与头行「仅 1-2 源支撑」同口径。

    失败场景:渲染 `support_count` 的话,一条单源边会带着「支撑9」出现在一段声称
    「仅 1-2 源」的清单里 —— 提示行自己拆自己的台,模型据此判断优先级只会更糟。
    """
    notebook = _seed(repo, well_supported_sources=2)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    _set_counts(repo, notebook.id, "K-寄生电容", support=9, sources=1)
    _set_counts(repo, notebook.id, "K-工艺角", support=1, sources=2)

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)
    segment, _used = kg_gap_note_segment(rows)

    assert "寄生电容(支撑1)" in segment and "支撑9" not in segment
    assert "工艺角(支撑2)" in segment


def test_the_store_order_decides_which_rows_survive_the_limit(repo):
    """SQL 的 `ORDER BY` 是**截断口径**,不是可有可无的整理。

    失败场景:删掉它,行按主键序回来 —— 主键是 `(…, canonical_src, edge_type,
    canonical_tgt)`,与来源数毫无关系。服务层随后那次排序会把**取回来的**行摆正,
    所以最终顺序看着还对;真正丢掉的是「取哪 24 行」:LIMIT 截断的将是主键靠前的
    那些,而不是支撑最薄弱的那些。这里让主键序与来源数序**相反**,再用 LIMIT 1
    把差别逼出来。
    """
    notebook = _seed(repo, well_supported_sources=2)
    # 主键序:同源端下按 (edge_type, canonical_tgt) → kind_of/寄生电容 在
    # part_of/工艺角 之前。来源数序刻意相反。
    _set_counts(repo, notebook.id, "K-寄生电容", support=1, sources=2)
    _set_counts(repo, notebook.id, "K-工艺角", support=1, sources=1)
    store = repo.retrieval.candidates.unified_kg

    with repo._connect() as db:
        pk_order = [
            row["canonical_tgt"] for row in db.execute(
                "SELECT canonical_tgt FROM canonical_relations WHERE notebook_id=?",
                (notebook.id,))
        ]
        capped = store.weak_support_relation_rows(
            db, notebook.id, ["K-版图设计"], _KG_GAP_SOURCE_MAX, 1)

    # 前置条件:主键序确实把「来源数更多」的那条排在前面(否则本用例是空跑)。
    assert pk_order[0] == "K-寄生电容"
    assert [row["canonical_tgt"] for row in capped] == ["K-工艺角"]


def test_the_service_breaks_ties_by_source_then_target(repo):
    """真并列时按 `(src, tgt)` 定序 —— 与 SQL 的 `(tgt, src)` 次键刻意不同。

    两个次键各有各的活:SQL 那份要让 LIMIT 截断确定,服务层这份要让**展示**顺序
    确定。用真库造这种并列要凑一堆同分边,所以这里直接把存储层换成替身,让 SQL
    侧按它自己的次键交出行,再看服务层有没有按自己的次键重排。砍掉服务层排序键
    的尾巴(只留来源数)时,稳定排序会原样保留输入序 → 本用例转红。
    """
    from contextlib import nullcontext

    class _Store:
        @staticmethod
        def cluster_fold_rows(_db, _notebook_id, ids):
            return [{"member_object_id": oid, "canonical_id": f"K-{oid}",
                     "canonical_name": ""} for oid in ids]

        @staticmethod
        def weak_support_relation_rows(_db, _nb, _ids, _source_max, _limit):
            # SQL 次键是 canonical_tgt:K-a 在 K-b 之前。
            return [
                {"canonical_src": "K-b", "edge_type": "kind_of",
                 "canonical_tgt": "K-a", "source_count": 1,
                 "sample_relation_ids": '["rel-1"]'},
                {"canonical_src": "K-a", "edge_type": "kind_of",
                 "canonical_tgt": "K-b", "source_count": 1,
                 "sample_relation_ids": '["rel-2"]'},
            ]

        @staticmethod
        def relation_endpoint_name_rows(_db, _nb, rids):
            return [{"rid": rid, "src_name": f"src-{rid}",
                     "tgt_name": f"tgt-{rid}"} for rid in rids]

    from app.services.retrieval_candidates import CandidateRetrievalService

    service = CandidateRetrievalService.__new__(CandidateRetrievalService)
    service._connect = lambda: nullcontext(None)
    service.unified_kg = _Store()

    rows = service.weak_support_relations("nb", ["a", "b"])

    assert [(row.canonical_src, row.canonical_tgt) for row in rows] == [
        ("K-a", "K-b"), ("K-b", "K-a")]


def test_probe_is_silent_when_the_canonical_layer_was_never_built(repo):
    """canonical_relations 是**可选**派生产物:从未 rebuild 过就是空表。

    那不是错误(整段静默缺席即可),所以这里既不能抛、也不能退化成扫全图。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _source(repo, notebook.id, "s1")
    repo.store_kg(notebook.id, "s1", [
        _concept("A", "版图设计"), _concept("B", "寄生电容"),
    ], [_edge("A", "B")])
    anchors = _object_ids(repo, notebook.id, "版图设计")

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM canonical_relations WHERE notebook_id=?",
            (notebook.id,)).fetchone()["c"] == 0
    assert repo.retrieval.weak_support_relations(notebook.id, anchors) == []


def test_probe_with_no_seed_ids_issues_no_query(repo):
    """零 id = 零查询(空 IN 列表在两个后端上都是语法错误,不能靠数据库兜)。"""
    notebook = _seed(repo)
    calls = []
    original = repo.retrieval.candidates.unified_kg.weak_support_relation_rows

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    repo.retrieval.candidates.unified_kg.weak_support_relation_rows = spy
    try:
        assert repo.retrieval.weak_support_relations(notebook.id, []) == []
        assert repo.retrieval.weak_support_relations(notebook.id, ["", ""]) == []
    finally:
        repo.retrieval.candidates.unified_kg.weak_support_relation_rows = original
    assert calls == []


def test_probe_folds_only_this_round_ids_and_honours_the_limit(repo, monkeypatch):
    """折叠只查本轮 id(绝不物化整本 cluster map),探测带着 LIMIT 下去。

    失败场景:改成 `cluster_map()` 的话,百万级簇的库上每一次被接受的大纲更新都
    要物化一次全表 —— 那是本仓库反复吃过亏的 OOM 形状;删掉 LIMIT 的话,一个高
    出度锚点能把上千条边一次性拉回来。
    """
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    candidates = repo.retrieval.candidates
    folded_ids: list[list[str]] = []
    probes: list[tuple] = []
    real_fold = candidates.unified_kg.cluster_fold_rows
    real_probe = candidates.unified_kg.weak_support_relation_rows

    def spy_fold(db, notebook_id, ids):
        folded_ids.append(list(ids))
        return real_fold(db, notebook_id, ids)

    def spy_probe(db, notebook_id, canonical_ids, support_max, limit):
        probes.append((list(canonical_ids), support_max, limit))
        return real_probe(db, notebook_id, canonical_ids, support_max, limit)

    monkeypatch.setattr(candidates.unified_kg, "cluster_fold_rows", spy_fold)
    monkeypatch.setattr(
        candidates.unified_kg, "weak_support_relation_rows", spy_probe)
    monkeypatch.setattr(
        candidates, "cluster_map",
        lambda *_: pytest.fail("大库禁止物化整本 cluster map"))

    repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert folded_ids == [anchors]
    assert probes == [(["K-版图设计"], _KG_GAP_SOURCE_MAX, _KG_GAP_PROBE_LIMIT)]


def test_the_store_probe_honours_the_row_limit(repo):
    """LIMIT 是**存储层**的硬界,不是调用方的一句约定。

    失败场景:服务端传了 24、存储层却不带 LIMIT 下去时,一个高出度锚点会把上千条
    边一次性拉回内存 —— 上面那条只断言「传对了参数」的用例对此完全无感。
    """
    notebook = _seed(repo, well_supported_sources=2)
    store = repo.retrieval.candidates.unified_kg
    with repo._connect() as db:
        full = store.weak_support_relation_rows(
            db, notebook.id, ["K-版图设计"], _KG_GAP_SOURCE_MAX, 24)
        capped = store.weak_support_relation_rows(
            db, notebook.id, ["K-版图设计"], _KG_GAP_SOURCE_MAX, 1)

    assert len(full) == 2 and len(capped) == 1
    # 截断取的是排序里靠前的那条(支撑最薄弱优先),不是随便一条。
    assert capped[0]["canonical_tgt"] == full[0]["canonical_tgt"]
    assert int(capped[0]["source_count"]) == 1


def test_probe_clamps_the_seed_list(repo):
    """源端 IN 列表的长度由服务端说了算,不由上游的一个笔误决定。"""
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    candidates = repo.retrieval.candidates
    seen: list[list[str]] = []
    real_fold = candidates.unified_kg.cluster_fold_rows

    def spy_fold(db, notebook_id, ids):
        seen.append(list(ids))
        return real_fold(db, notebook_id, ids)

    candidates.unified_kg.cluster_fold_rows = spy_fold
    try:
        repo.retrieval.weak_support_relations(
            notebook.id, [f"ko-{index:04d}" for index in range(500)])
    finally:
        candidates.unified_kg.cluster_fold_rows = real_fold

    assert len(seen[0]) == _KG_GAP_MAX_SEEDS
    assert anchors  # 前置条件:真实库里确实有锚点(上面那条不是空跑)


def test_names_prefer_the_cluster_canonical_name(repo):
    """显示名优先取簇的 canonical_name,而不是某个成员对象的 payload name。

    失败场景:直接用成员名的话,同一个簇在不同轮次会以不同别名出现,模型据此提交
    的定向查询就会追着一个它上一轮没见过的名字跑。
    """
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    targets = _object_ids(repo, notebook.id, "寄生电容")
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_objects SET payload=? WHERE id=?",
            (json.dumps({"name": "寄生电容(别名)", "section_path": "1"}),
             targets[0]))
        db.execute(
            "UPDATE concept_clusters SET canonical_name='寄生电容' "
            "WHERE notebook_id=? AND member_object_id=?",
            (notebook.id, targets[0]))

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert [row.tgt_name for row in rows] == ["寄生电容"]


def test_the_source_name_comes_from_the_bound_object_own_cluster_row(repo):
    """源端名优先用**折叠本轮绑定对象**时拿到的簇名,而不是样本关系解析出来的。

    两者可以不同名:样本关系的源端往往是同一簇里的**另一个**成员行,而模型手上
    拿到的是它自己绑的那一个。失败场景:砍掉这个优先级(直接用样本解析名),提示
    行会用一个模型这一轮根本没见过的名字称呼它绑定的对象。

    顺带这也是零额外查询的那一步 —— 簇名在第 1 步的折叠里已经回来了。
    """
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    # 样本关系的源端是 s1 里那个对象;再给同簇加一个**只被绑定、不参与关系**的
    # 成员,并让两行的簇名不同,好把「用哪一行的名字」逼出来。
    bound_only = _source_id(notebook.id, "bound")
    _source(repo, notebook.id, bound_only)
    repo.store_kg(notebook.id, bound_only, [_concept("A", "版图设计")], [])
    repo.rebuild_unified_kg(notebook.id)
    extra = [oid for oid in _object_ids(repo, notebook.id, "版图设计")
             if oid not in anchors]
    assert len(extra) == 1                       # 前置条件:确实多了一个同簇成员
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='绑定行的名字' "
            "WHERE notebook_id=? AND member_object_id=?",
            (notebook.id, extra[0]))
        db.execute(
            "UPDATE concept_clusters SET canonical_name='样本行的名字' "
            "WHERE notebook_id=? AND member_object_id IN "
            f"({','.join('?' for _ in anchors)})",
            (notebook.id, *anchors))

    rows = repo.retrieval.weak_support_relations(notebook.id, extra)

    assert [row.src_name for row in rows] == ["绑定行的名字"]


def test_the_sampled_source_name_still_prefers_the_cluster_name(repo):
    """回退到样本关系解析时,那条 SQL 自己也必须簇名优先、payload name 其次。

    失败场景:把 `COALESCE` 的两臂对调,回退路径会拿成员对象的 payload 原名 ——
    同一个簇于是在「折叠拿到名字」和「折叠没拿到名字」两种情况下用两套称呼。

    ⚠ 要测到那条 SQL,**折叠侧必须先取不到名字**(否则它短路,SQL 的选择压根不
    参与结果)。所以这里绑定的是一个簇名为空的成员行,而样本关系的源端那一行簇名
    非空 —— 两个来源于是给出不同的答案,SQL 选哪一臂才看得出来。
    """
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    bound_only = _source_id(notebook.id, "bound")
    _source(repo, notebook.id, bound_only)
    repo.store_kg(notebook.id, bound_only, [_concept("A", "版图设计")], [])
    repo.rebuild_unified_kg(notebook.id)
    extra = [oid for oid in _object_ids(repo, notebook.id, "版图设计")
             if oid not in anchors]
    assert len(extra) == 1                       # 前置条件:确实多了一个同簇成员
    with repo._write() as db:
        # 绑定的那一行没有簇名 → 折叠侧交白卷,必须落到样本关系那条 SQL。
        db.execute(
            "UPDATE concept_clusters SET canonical_name='' "
            "WHERE notebook_id=? AND member_object_id=?",
            (notebook.id, extra[0]))
        db.execute(
            "UPDATE concept_clusters SET canonical_name='簇名' "
            "WHERE notebook_id=? AND member_object_id IN "
            f"({','.join('?' for _ in anchors)})",
            (notebook.id, *anchors))
        db.execute(
            "UPDATE knowledge_objects SET payload=? "
            f"WHERE id IN ({','.join('?' for _ in anchors)})",
            (json.dumps({"name": "payload 原名", "section_path": "1"}), *anchors))

    rows = repo.retrieval.weak_support_relations(notebook.id, extra)

    assert [row.src_name for row in rows] == ["簇名"]
    # 对照:簇名也空掉时才轮到 payload 名(回退链的另一半)。
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='' WHERE notebook_id=?",
            (notebook.id,))
    fallback = repo.retrieval.weak_support_relations(notebook.id, extra)
    assert [row.src_name for row in fallback] == ["payload 原名"]


def test_names_fall_back_to_the_object_payload_name(repo):
    """簇名为空时回退对象 payload name(非 concept 的 canonical 走的就是这条)。"""
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='' WHERE notebook_id=?",
            (notebook.id,))

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert [(row.src_name, row.tgt_name) for row in rows] == [
        ("版图设计", "寄生电容")]


def test_rows_whose_names_cannot_be_resolved_are_dropped(repo):
    """两端都解析不出名字的行丢弃 —— 一条只有 `K-<seed>` 的提示对模型毫无用处,
    而把内部 id 喂进 prompt,模型会把它当名字复述进答案。"""
    notebook = _seed(repo)
    anchors = _object_ids(repo, notebook.id, "版图设计")
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='' WHERE notebook_id=?",
            (notebook.id,))
        db.execute(
            "UPDATE knowledge_objects SET payload='{}' WHERE notebook_id=?",
            (notebook.id,))

    assert repo.retrieval.weak_support_relations(notebook.id, anchors) == []


def test_first_relation_sample_only_reads_json_text():
    """两个适配器都以 JSON 文本出参(PG 侧显式 `::text`);顺手接受 list 会把
    dialect 判断偷偷搬进服务层,parity 用例反而测不出两侧已经不同形。"""
    assert _first_relation_sample('["rel-1", "rel-2"]') == "rel-1"
    assert _first_relation_sample("[]") == ""
    assert _first_relation_sample("不是 JSON") == ""
    assert _first_relation_sample(None) == ""
    assert _first_relation_sample(["rel-1"]) == ""


# ------------------------------------------------------------- 便签段(渲染)

def test_note_segment_renders_the_documented_shape():
    segment, used = kg_gap_note_segment([_row("版图设计", "寄生电容", sources=1)])

    assert used == 1
    assert _KG_GAP_NOTE_HEAD in segment
    assert "- 版图设计 —kind_of→ 寄生电容(支撑1)" in segment


def test_note_segment_caps_the_rows_per_round():
    """每轮 ≤6 行:一轮塞满 24 行会把大纲便签本身挤到模型注意力之外。"""
    rows = [_row(f"源{index}", f"目标{index}") for index in range(20)]

    segment, used = kg_gap_note_segment(rows)

    assert used == _KG_GAP_NOTE_LINES
    assert segment.count("\n- ") == _KG_GAP_NOTE_LINES


def test_note_segment_clamps_each_line_by_shortening_the_names():
    """单行 ≤80 字符,而且必须靠**裁两端名字**达成。

    失败场景:对整行做尾截会把 `(支撑N)` 连同目标端一起削掉,留下一行读不出关系
    的残句,模型据此提交的定向查询会是半个名字。
    """
    long_row = _row("甲" * 200, "乙" * 200, sources=2)

    segment, used = kg_gap_note_segment([long_row])
    line = segment.splitlines()[-1]

    assert used == 1
    assert len(line) <= _KG_GAP_NOTE_LINE_CHARS
    assert line.startswith("- 甲")
    assert line.endswith("(支撑2)")          # 尾部信息没有被削掉
    assert "—kind_of→ 乙" in line           # 两端都还在


def test_note_segment_clamps_the_whole_block():
    """整段 ≤520 字符:装不下的行留到下一轮,而不是把预算撑爆。"""
    rows = [_row("甲" * 60, "乙" * 60) for _ in range(_KG_GAP_NOTE_LINES)]

    segment, used = kg_gap_note_segment(rows)

    assert len(segment) <= _KG_GAP_NOTE_CHARS
    assert 0 < used < _KG_GAP_NOTE_LINES


def test_note_segment_is_absent_without_candidates():
    assert kg_gap_note_segment([]) == ("", 0)


def test_note_line_collapses_whitespace_before_clamping():
    """名字先压成单行再截长。

    失败场景:KG 对象的名字来自语料,claim 的「名字」经常就是一整段带换行的正文。
    原样拼进便签的话,一条提示会被那个换行撕成两半 —— 后半截没有 `- ` 前缀也没有
    箭头,读起来像大纲的下一节,而 `(支撑N)` 落在了那半截上。截长封不住这件事:
    80 字符的行里塞得下好几个换行。
    """
    row = _row("第一行\n第二行  第三行", "目\n标", sources=1)

    segment, used = kg_gap_note_segment([row])
    body = segment.splitlines()

    assert used == 1
    # 头行 1 行 + 提示 1 行,没有第三行(名字里的换行没有漏出来)。
    assert len(body) == 3 and body[0] == ""
    assert body[2] == "- 第一行 第二行 第三行 —kind_of→ 目 标(支撑1)"


def test_the_repair_round_gets_no_gap_hint():
    """终态纠错轮不拼这一段。

    失败场景(评审端到端复现):同一份便签的开头写着「不得改结构或执行检索」,
    而这一段的头行写着「可用 add_subquery/follow_chain 定向补证」。模型照着后写的
    那句做,这一轮就以 `outline_overflow_repair_declined` 收场 —— 纠错资格白烧
    一次,未接纳的 key 一个都没换进来。
    """
    from app.services.reasoning_retrieval import OutlineSection

    sections = [OutlineSection(id="a", title="一节", evidence_keys=["k1"])]
    segment = kg_gap_note_segment([_row()])[0]
    overflow = {"a": ["k9"]}

    repair = _outline_note(sections, 0, overflow, repair_only=True,
                           kg_gap_segment=segment)
    ordinary = _outline_note(sections, 3, kg_gap_segment=segment)

    assert _KG_GAP_NOTE_HEAD not in repair
    assert "不得改结构或执行检索" in repair       # 前置条件:确实是纠错轮措辞
    # 只挡这一轮:普通轮与定稿轮里检索仍然合法,提示照常出现。
    assert _KG_GAP_NOTE_HEAD in ordinary
    assert _KG_GAP_NOTE_HEAD in _outline_note(
        sections, 0, kg_gap_segment=segment)


def test_the_repair_round_consumes_no_candidates():
    """纠错轮既不渲染也**不消费**:两件事同一个判据、同一处返回。

    失败场景:只挡渲染、照常按返回的行数摘队列的话,那批候选会被算作「展示过」
    却一行都没上屏 —— 从此再也不会出现(去重账目只认展示过一次)。所以这里断言
    的是返回值的**第二个**分量,它就是调用方要摘掉的行数。
    """
    rows = [_row(f"源{index}", f"目标{index}") for index in range(3)]

    assert kg_gap_note_segment(rows, repair_only=True) == ("", 0)
    # 对照:普通轮既渲染也消费。
    segment, used = kg_gap_note_segment(rows)
    assert used == 3 and _KG_GAP_NOTE_HEAD in segment


def test_the_outline_note_is_byte_identical_without_a_gap_segment():
    """关闭态/无候选时,大纲便签逐字回到接入前。"""
    from app.services.reasoning_retrieval import OutlineSection

    sections = [OutlineSection(id="a", title="一节", evidence_keys=["k1"])]

    assert _outline_note(sections, 3) == _outline_note(
        sections, 3, kg_gap_segment="")
    assert _KG_GAP_NOTE_HEAD not in _outline_note(sections, 3)
    assert _KG_GAP_NOTE_HEAD in _outline_note(
        sections, 3, kg_gap_segment=kg_gap_note_segment([_row()])[0])


# ----------------------------------------------------------------- 接线(run)

@pytest.mark.parametrize("effort,flag,expected", [
    ("exhaustive", True, True),
    ("exhaustive", False, False),
    ("thorough", True, False),      # 低档位:大纲不在场,回喂自然也不在
])
def test_the_gate_decides_whether_a_single_query_is_issued(
    repo, monkeypatch, effort, flag, expected
):
    """关闭态 = **零查询**,不是「查了但不展示」。

    失败场景:把闸做在渲染处的话,低档位与关闭部署仍要为每一次大纲更新付两条
    数据库往返 —— 而它们连大纲便签都看不到。
    """
    notebook = _seed(repo)
    repo.settings.reasoning_outline_kg_gap_enabled = flag
    probes: list[tuple] = []
    real = repo.retrieval.candidates.unified_kg.weak_support_relation_rows

    def spy(db, notebook_id, canonical_ids, support_max, limit):
        probes.append((notebook_id, list(canonical_ids)))
        return real(db, notebook_id, canonical_ids, support_max, limit)

    monkeypatch.setattr(
        repo.retrieval.candidates.unified_kg, "weak_support_relation_rows", spy)
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "锚点一节",
             "evidence": [_shown_kg_ids(prompt)[0]]},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm, effort=effort)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    assert bool(probes) is expected
    if not expected:
        assert _KG_GAP_NOTE_HEAD not in "".join(llm.reflect_prompts)
        for step in _steps(result, "outline"):
            assert "kg_gap_candidates" not in step.detail


def _shown_kg_ids(prompt: str) -> list[str]:
    """候选区里模型真正看到的 KG 对象 id(`[concept]`/`[claim]` 等行)。"""
    body = prompt.split("Candidates so far:", 1)[-1]
    return [
        line.rsplit("(id=", 1)[-1].rstrip(")")
        for line in body.splitlines()
        if line.startswith("- [") and "(id=" in line
        and not line.startswith(("- [element]", "- [chunk]", "- [inference]"))
    ]


def test_a_bound_object_feeds_the_gap_hint_into_the_next_round(repo):
    """e2e:模型绑上锚点 → 下一轮 prompt 真的带着那条弱支撑边。

    这是本特性的全部意义所在:提示要到得了**写下一个动作**的那次调用手里。
    """
    notebook = _seed(repo)
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "锚点一节",
             "evidence": [_shown_kg_ids(prompt)[0]]},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    assert _KG_GAP_NOTE_HEAD not in llm.reflect_prompts[0]
    assert _KG_GAP_NOTE_HEAD in llm.reflect_prompts[1]
    assert "版图设计 —kind_of→ 寄生电容(支撑1)" in llm.reflect_prompts[1]
    # 阈值那条对照:被 3 篇文档印证的边不该出现。
    assert "工艺角" not in llm.reflect_prompts[1].split(_KG_GAP_NOTE_HEAD)[-1]
    # 提示必须挂在大纲便签**内部**,而不是另起一段无处安放的关系清单:它讲的正是
    # 「已绑定的这些证据周边还有什么没查」,离开大纲上下文就只是一串孤立关系。
    prompt = llm.reflect_prompts[1]
    note_start = prompt.index("（本轮大纲便签")
    note_end = prompt.index("）", note_start)
    assert note_start < prompt.index(_KG_GAP_NOTE_HEAD) < note_end
    outline_steps = _steps(result, "outline")
    assert outline_steps[0].detail["kg_gap_candidates"] == 1


def test_each_edge_is_shown_at_most_once_per_run(repo):
    """run 级 (src, edge, tgt) 去重:它是提示不是状态,反复展示只烧 prompt 预算。

    失败场景:删掉账目的话,模型每绑一次证据就重新收到同一条边 —— 6 次大纲更新
    就是 6 份一模一样的提示,而真正的新缺口被挤出那 6 行。
    """
    notebook = _seed(repo)
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "锚点一节",
             "evidence": [_shown_kg_ids(prompt)[0]]},
        ]),
        lambda prompt: _outline_action([
            {"id": "a", "title": "锚点一节(改了标题)",
             "evidence": [_shown_kg_ids(prompt)[0]]},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    shown = [
        prompt for prompt in llm.reflect_prompts if _KG_GAP_NOTE_HEAD in prompt
    ]
    assert len(shown) == 1, "同一条边只该出现在一轮里"
    steps = _steps(result, "outline")
    assert steps[0].detail["kg_gap_candidates"] == 1
    # 第二次 apply 一条新候选都没有 → 不加这个键(常年挂 0 会让「开没开」分不出来)。
    assert "kg_gap_candidates" not in steps[1].detail


def test_only_kg_object_ids_seed_the_probe(repo, monkeypatch):
    """element/chunk 绑定键不参与:弱支撑边定义在 KG 图上。

    判别走 `collected`(候选池的键就是 object_id),不猜 id 前缀 —— 前缀是会变的
    实现细节,而误把 element id 当对象 id 塞进 IN 列表只会白付一次查询。
    """
    notebook = _seed(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            ("el-000", _source_id(notebook.id, 1), "formula", "p1",
             "版图设计 公式", "{}", NOW))
    seeds: list[list[str]] = []
    real = repo.retrieval.candidates.weak_support_relations

    def spy(notebook_id, object_ids):
        seeds.append(list(object_ids))
        return real(notebook_id, object_ids)

    monkeypatch.setattr(
        repo.retrieval.candidates, "weak_support_relations", spy)
    llm = _SeqLLM([
        {"next_action": "search_elements", "elements_query": "版图设计"},
        lambda prompt: _outline_action([
            {"id": "a", "title": "混合绑定的一节",
             "evidence": [_shown_kg_ids(prompt)[0], "el-000"]},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    # 前置条件:那一节确实同时绑上了 element 与 KG 对象。
    bound = result.outline[0].evidence_keys
    assert "el-000" in bound and len(bound) == 2
    assert seeds == [[key for key in bound if key != "el-000"]]


def test_a_rejected_outline_update_probes_nothing(repo, monkeypatch):
    """被拒/空提交不触发探测:没有新绑定就没有新邻域。"""
    notebook = _seed(repo)
    probes: list[tuple] = []
    real = repo.retrieval.candidates.weak_support_relations
    monkeypatch.setattr(
        repo.retrieval.candidates, "weak_support_relations",
        lambda notebook_id, object_ids: (
            probes.append(list(object_ids)) or real(notebook_id, object_ids)))
    llm = _SeqLLM([
        _outline_action([]),                       # 空提交 → skip
        _outline_action(["不是对象"]),               # 全部畸形 → skip
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    assert probes == []
    assert [step.detail.get("reason") for step in _steps(result, "skip")] == [
        "outline_empty", "outline_empty"]


def test_candidates_from_the_terminal_round_are_dropped(repo):
    """终态轮 apply 算出的候选无处展示,如实丢弃 —— 提示服务于**继续检索**的轮次。

    失败场景:硬塞进答案合成上下文的话,一段「还没查的方向」会以证据的姿态进入
    最终答案,而它一条原文都没有。
    """
    notebook = _seed(repo)
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "收尾一节",
             "evidence": [_shown_kg_ids(prompt)[0]]},
        ], sufficient=True),
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    steps = _steps(result, "outline")
    assert steps[-1].detail["kg_gap_candidates"] == 1
    assert _KG_GAP_NOTE_HEAD not in "".join(llm.reflect_prompts)
    assert result.trace[-1].step_type == "answer"


def test_the_hint_does_not_touch_the_evidence_pools(repo):
    """提示不进任何证据池:它是一行提问,不是一条来源。

    对照跑同一个脚本、只切换本特性的开关 —— 三个候选池与终态大纲都必须逐条相同。
    失败场景:哪天有人「顺手」把弱支撑边的对端也收进 collected,它就成了一条没有
    任何原文支撑、却能参与相关性排序并被 `[k]` 引用的证据。
    """
    def run_once(flag):
        repo.settings.reasoning_outline_kg_gap_enabled = flag
        notebook = _seed(repo)
        llm = _SeqLLM([
            lambda prompt: _outline_action([
                {"id": "a", "title": "锚点一节",
                 "evidence": [_shown_kg_ids(prompt)[0]]},
            ]),
            ANSWER,
        ])
        retriever, limits = _retriever(repo, llm)
        return retriever.run(notebook.id, "综述一下", "", limits=limits)

    on, off = run_once(True), run_once(False)

    def shape(result):
        return (
            sorted(hit.payload.get("name", "") for hit in result.top_hits),
            [(section.id, len(section.evidence_keys))
             for section in result.outline],
            len(result.elements), len(result.chunks),
        )

    assert shape(on) == shape(off)


def test_the_budget_bounds_the_number_of_probes(repo, monkeypatch):
    """探测只发生在**被接受**的那几次大纲更新之后,所以每 run 恰好 ≤ 更新预算。

    失败场景:把探测挪到预算/结构校验**之前**的话,一个反复提交大纲的模型能让
    这条回喂在一次 run 里跑上几十次数据库往返 —— 而那些提交一次都没被接受。
    """
    notebook = _seed(repo)
    probes: list[int] = []
    real = repo.retrieval.candidates.weak_support_relations
    monkeypatch.setattr(
        repo.retrieval.candidates, "weak_support_relations",
        lambda notebook_id, object_ids: (
            probes.append(1) or real(notebook_id, object_ids)))
    repo.settings.reasoning_stale_limit = 99
    rounds = iter(range(_MAX_OUTLINE_UPDATES + 4))
    llm = _SeqLLM([
        # 每轮换一个**新**对象,好让每次被接受的 apply 都有新 seed 可探
        # (否则 seed 账目会先把查询挡掉,这条就测不到预算了)。
        lambda prompt, _n=next(rounds): _outline_action([
            {"id": "a", "title": f"第 {_n} 版",
             "evidence": [_shown_kg_ids(prompt)[_n % 6]]},
        ])
        for _ in range(_MAX_OUTLINE_UPDATES + 4)
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    # 前置条件:确实有几轮因为预算耗尽被拒(否则这条只是在数成功次数)。
    rejected = [step for step in _steps(result, "skip")
                if step.detail.get("reason") == "outline_budget"]
    assert len(rejected) >= 4
    assert len(probes) == _MAX_OUTLINE_UPDATES


def test_an_unchanged_binding_set_probes_nothing(repo, monkeypatch):
    """绑定集合没变的 apply 零查询。

    图在一个 run 内只读,所以重探同一个对象必然拿回同一批边、再被去重账目全部
    滤掉 —— 一次纯白付的数据库往返。而「绑定没变」恰恰是常态:纯改标题、纯换
    parent、用 remove_evidence 腾位换键的 apply 都不引入新对象。
    """
    notebook = _seed(repo)
    seeds: list[list[str]] = []
    real = repo.retrieval.candidates.weak_support_relations
    monkeypatch.setattr(
        repo.retrieval.candidates, "weak_support_relations",
        lambda notebook_id, object_ids: (
            seeds.append(list(object_ids)) or real(notebook_id, object_ids)))
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "初版",
             "evidence": [_shown_kg_ids(prompt)[0]]},
        ]),
        # 同一个绑定、只换标题和 parent 结构 → 没有新 seed。
        lambda prompt: _outline_action([
            {"id": "a", "title": "改了标题的同一节",
             "evidence": [_shown_kg_ids(prompt)[0]]},
            {"id": "b", "title": "新加的空节"},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    # 前置条件:两次 apply 都被接受了(否则这条只是在测「第二次被拒」)。
    assert len(_steps(result, "outline")) == 2
    assert len(seeds) == 1


def test_the_terminal_repair_round_prompt_carries_no_gap_hint(repo, monkeypatch):
    """端到端:真正跑到终态纠错轮时,那一轮的 prompt 里没有这一段。

    上面那两条钉的是判据本身;这条钉的是**接线** —— run() 得真把
    `repair_only=terminal_overflow_repair` 递下去,而不是恒 False。
    """
    from app.services.reasoning_retrieval import _OUTLINE_MAX_EVIDENCE

    notebook = _seed(repo)
    real_ids = _object_ids(repo, notebook.id, "版图设计") + _object_ids(
        repo, notebook.id, "寄生电容")
    old_keys = (real_ids + [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)])[
        :_OUTLINE_MAX_EVIDENCE]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    # 攒够候选,好让第 2 轮展示 6 条之后队里还有剩 —— 否则纠错轮本来就没东西可拼,
    # 这条会变成空跑。
    monkeypatch.setattr(
        repo.retrieval.candidates, "weak_support_relations",
        lambda *_args, **_kwargs: [
            _row(f"源{index}", f"目标{index}", sources=1) for index in range(8)
        ])
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一节", "evidence": old_keys}]),
        dict(_outline_action([{"id": "a", "title": "一节",
                               "evidence": [new_key]}]), sufficient=True),
        _outline_action([{"id": "a", "title": "一节", "evidence": [new_key],
                          "remove_evidence": [old_keys[0]]}]),
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    # 前置条件:确实跑到了终态纠错轮,而且此前那一轮真的展示过提示。
    assert any(step.detail.get("overflow_repair")
               for step in _steps(result, "outline"))
    assert _KG_GAP_NOTE_HEAD in llm.reflect_prompts[1]
    assert _KG_GAP_NOTE_HEAD not in llm.reflect_prompts[-1]
    assert "不得改结构或执行检索" in llm.reflect_prompts[-1]


def test_the_run_loop_passes_the_repair_flag_to_the_segment_builder():
    """接线守卫:`run()` 那次 `kg_gap_note_segment` 调用必须带 `repair_only=`。

    为什么要一条读源码的守卫 —— 这条接线**当前没有行为签名**:纠错轮之后循环立刻
    `break`,所以「被误当成展示过而消费掉」的那批候选本来也不会再有机会上屏,而
    渲染侧 `_outline_note` 里还有一道同判据的闸把 prompt 兜住了。两道闸今天是冗余
    的,少任何一道都测不出来 —— 但一旦哪天纠错轮不再是最后一轮(或渲染侧那道闸被
    收走),漏掉这个 kwarg 就是「算作展示过、一行都没上屏、此后再也不出现」。行为
    用例钉不住的东西,就钉调用形状本身。
    """
    import inspect

    source = inspect.getsource(ReasoningRetriever.run)
    calls = [
        line for line in source.splitlines()
        if "kg_gap_note_segment(" in line
    ]

    assert len(calls) == 1, calls
    # 调用跨行,所以从调用点起截一小段看实参。只断言 kwarg 名出现是不够的:
    # `repair_only=False` 这种恒假值同样通过（镜像 `docs/development.md` 长任务按钮守卫里
    # 「disabled={false} 同样报红」的教训),所以钉到实参就是那只终态轮变量。
    start = source.index("kg_gap_note_segment(")
    assert "repair_only=terminal_overflow_repair" in source[start:start + 200]


def test_a_later_batch_can_outrank_what_is_still_queued(repo, monkeypatch):
    """跨批次重排:第二轮发现的更薄弱的边排在上一轮的剩余之前。

    失败场景:只按批次先后展示的话,上一轮溢出的那条 2 源边会挡在这一轮刚发现的
    单源边前面 —— 「支撑最薄弱的先说」于是只在单批内成立,而这段提示的全部价值
    就在那个排序上。
    """
    notebook = _seed(repo)
    batches = [
        [_row(f"旧{index}", f"旧目标{index}", sources=2) for index in range(7)],
        [_row("新", "新目标", sources=1)],
    ]
    monkeypatch.setattr(
        repo.retrieval.candidates, "weak_support_relations",
        lambda *_args, **_kwargs: batches.pop(0) if batches else [])
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "第一版", "evidence": [_shown_kg_ids(prompt)[0]]},
        ]),
        lambda prompt: _outline_action([
            {"id": "a", "title": "第二版",
             "evidence": [_shown_kg_ids(prompt)[0], _shown_kg_ids(prompt)[1]]},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "综述一下", "", limits=limits)

    # 第 2 轮:7 条里展示 6 条,剩 1 条 2 源边在队里。
    first = llm.reflect_prompts[1].split(_KG_GAP_NOTE_HEAD)[-1]
    assert first.count("\n- ") == _KG_GAP_NOTE_LINES
    # 第 3 轮:刚发现的单源边必须排在那条剩余之前。
    third = llm.reflect_prompts[2].split(_KG_GAP_NOTE_HEAD)[-1]
    # 末行带着便签的收尾全角括号,剥掉再比。
    lines = [line.rstrip("）") for line in third.splitlines()
             if line.startswith("- ")]
    assert len(lines) == 2
    assert lines[0] == "- 新 —kind_of→ 新目标(支撑1)"
    assert lines[1].endswith("(支撑2)")


def test_a_probe_failure_never_takes_down_the_run(repo, monkeypatch):
    """探测异常 fail-open(镜像 `collection_map_unavailable`)。

    这是一段可有可无的提示,让它把整个 run 打掉是本末倒置 —— 用户问的问题还在
    那儿。留一条 skip 步是底线:静默吞掉的话,「提示为什么一直不出现」在任何地方
    都看不出来。
    """
    notebook = _seed(repo)
    monkeypatch.setattr(
        repo.retrieval.candidates, "weak_support_relations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("图谱读挂了")))
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "锚点一节",
             "evidence": [_shown_kg_ids(prompt)[0]]},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    assert result.trace[-1].step_type == "answer"
    skipped = _skip_reasons(result)
    assert "kg_gap_unavailable" in skipped
    assert "图谱读挂了" in skipped["kg_gap_unavailable"].detail["error"]
    # 大纲本身照常落地:提示失败不该连累它。
    assert [section.id for section in result.outline] == ["a"]
    assert "kg_gap_candidates" not in _steps(result, "outline")[0].detail


def _skip_reasons(result):
    return {step.detail.get("reason"): step for step in _steps(result, "skip")}
