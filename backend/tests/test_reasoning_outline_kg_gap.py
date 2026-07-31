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
    _KG_GAP_SUPPORT_MAX,
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


def _row(src="源", tgt="目标", *, support=1, edge="kind_of",
         canonical_src="", canonical_tgt=""):
    return GapRelationRow(
        canonical_src=canonical_src or f"K-{src}",
        canonical_tgt=canonical_tgt or f"K-{tgt}",
        src_name=src, tgt_name=tgt, edge_type=edge, support_count=support,
    )


# ------------------------------------------------------------- 探测层(服务)

def test_the_documented_bounds_are_the_ones_in_the_code():
    """契约里的数字与代码里的常量对上。

    单独钉一条,是因为下面的用例大多拿常量本身当期望值 —— 只改常量的话它们会
    跟着一起动、全绿放行(「替换打空」)。数字必须在某一处以字面量出现。
    """
    assert (_KG_GAP_SUPPORT_MAX, _KG_GAP_PROBE_LIMIT, _KG_GAP_MAX_SEEDS) == (
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

    assert [(row.src_name, row.edge_type, row.tgt_name, row.support_count)
            for row in rows] == [("版图设计", "kind_of", "寄生电容", 1)]


def test_probe_accepts_the_threshold_boundary(repo):
    """边界本身在内:恰好 2 个来源支撑的边仍是「弱支撑」。"""
    notebook = _seed(repo, well_supported_sources=2)
    anchors = _object_ids(repo, notebook.id, "版图设计")

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert sorted((row.tgt_name, row.support_count) for row in rows) == [
        ("寄生电容", 1), ("工艺角", 2)]
    assert all(row.support_count <= _KG_GAP_SUPPORT_MAX for row in rows)


def test_probe_is_ordered_by_support_then_endpoint_ids(repo):
    """展示顺序稳定可测:支撑数升序,tie 按 (src, tgt) 字典序。

    失败场景:靠存储顺序的话,同一个库两次跑出两份顺序不同的提示,回归用例就只
    能测长度 —— 而「支撑最薄弱的先说」正是这段提示的全部价值。
    """
    notebook = _seed(repo, well_supported_sources=2)
    anchors = _object_ids(repo, notebook.id, "版图设计")

    rows = repo.retrieval.weak_support_relations(notebook.id, anchors)

    assert [(row.support_count, row.canonical_src, row.canonical_tgt)
            for row in rows] == sorted(
        (row.support_count, row.canonical_src, row.canonical_tgt)
        for row in rows)


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
    assert probes == [(["K-版图设计"], _KG_GAP_SUPPORT_MAX, _KG_GAP_PROBE_LIMIT)]


def test_the_store_probe_honours_the_row_limit(repo):
    """LIMIT 是**存储层**的硬界,不是调用方的一句约定。

    失败场景:服务端传了 24、存储层却不带 LIMIT 下去时,一个高出度锚点会把上千条
    边一次性拉回内存 —— 上面那条只断言「传对了参数」的用例对此完全无感。
    """
    notebook = _seed(repo, well_supported_sources=2)
    store = repo.retrieval.candidates.unified_kg
    with repo._connect() as db:
        full = store.weak_support_relation_rows(
            db, notebook.id, ["K-版图设计"], _KG_GAP_SUPPORT_MAX, 24)
        capped = store.weak_support_relation_rows(
            db, notebook.id, ["K-版图设计"], _KG_GAP_SUPPORT_MAX, 1)

    assert len(full) == 2 and len(capped) == 1
    # 截断取的是排序里靠前的那条(支撑最薄弱优先),不是随便一条。
    assert capped[0]["canonical_tgt"] == full[0]["canonical_tgt"]
    assert int(capped[0]["support_count"]) == 1


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
    segment, used = kg_gap_note_segment([_row("版图设计", "寄生电容", support=1)])

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
    long_row = _row("甲" * 200, "乙" * 200, support=2)

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
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": f"第 {len(probes)} 版",
             "evidence": [_shown_kg_ids(prompt)[0]]},
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
