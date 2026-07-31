from __future__ import annotations

from app.core.config import Settings
from app.models.schemas import Evidence
from app.services.evidence_context import EvidenceContextService
from app.services.retrieval import RetrievedChunk, RetrievedElement, RetrievedKnowledge


class _Notebooks:
    def tier_map(self, notebook_ids):
        tiers = {"active": "personal", "base": "base"}
        return {notebook_id: tiers[notebook_id] for notebook_id in notebook_ids if notebook_id in tiers}

    def participant_notebook_ids(self, active_notebook_id):
        return [active_notebook_id, "base"]


class _Sources:
    def __init__(self, metadata=None, elements=None):
        self.metadata = metadata or {}
        self.elements = elements or {}

    def evidence_elements(self, element_ids):
        return {element_id: self.elements[element_id]
                for element_id in element_ids if element_id in self.elements}

    def source_metadata(self, source_ids):
        return {source_id: self.metadata[source_id]
                for source_id in source_ids if source_id in self.metadata}


class _Knowledge:
    def cluster_map(self, notebook_id):
        return {}

    def node_context(self, notebook_id, object_id):
        return {
            "occurrences": [{
                "element_text": "source excerpt",
                "source_title": "Source A",
                "section_path": "§1",
            }],
            "definition": "stable definition",
            "steps": None,
        }

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_count(self, notebook_id, source_id, edge_type, target_id):
        return 1


def _service(*, source_metadata=None, elements=None):
    return EvidenceContextService(
        notebooks=_Notebooks(),
        sources=_Sources(source_metadata, elements),
        knowledge=_Knowledge(),
        settings=Settings(),
    )


def test_citation_titles_prefer_grounded_paper_title_and_keep_other_sources():
    service = _service(source_metadata={
        "paper": {
            "title": "2401.01234.pdf", "file_name": "2401.01234.pdf", "notebook_id": "active",
            "is_paper": True, "paper_title": "A Useful Article Title",
        },
        "ordinary": {
            "title": "design-notes.md", "file_name": "design-notes.md", "notebook_id": "active",
            "is_paper": False, "paper_title": "Ignored stale title",
        },
        "untitled-paper": {
            "title": "scan.pdf", "file_name": "scan.pdf", "notebook_id": "active",
            "is_paper": True, "paper_title": "   ",
        },
    })

    assert service.citation_titles(["paper", "ordinary", "untitled-paper"]) == {
        "paper": "A Useful Article Title",
        "ordinary": "design-notes.md",
        "untitled-paper": "scan.pdf",
    }


def test_element_context_and_anchor_expose_parsed_paper_title():
    service = _service(source_metadata={
        "s-paper": {
            "title": "opaque-file-name.pdf", "is_paper": True,
            "paper_title": "Meaningful Paper Name",
        },
    })
    element = RetrievedElement(
        element_id="e-paper", source_id="s-paper",
        source_title="opaque-file-name.pdf", element_type="paragraph",
        location_label="p. 2", text="quoted evidence", score=0.9,
    )

    block, evidence = service.element_context([element], notebook_id="active")
    anchors = service.parse_anchors("claim [k4001]", evidence)

    assert "Meaningful Paper Name" in block
    assert "opaque-file-name.pdf" not in block
    assert evidence["k4001"]["source_title"] == "Meaningful Paper Name"
    assert anchors[0].source_title == "Meaningful Paper Name"


def test_fallback_citation_label_uses_parsed_paper_title():
    service = _service(source_metadata={
        "s-paper": {
            "title": "opaque-file-name.pdf", "is_paper": True,
            "paper_title": "Meaningful Paper Name",
        },
    })
    hit = RetrievedKnowledge(
        object_id="o-paper", object_type="claim", payload={"name": "Claim"},
        evidence=[Evidence(
            source_id="s-paper", source_title="opaque-file-name.pdf",
            element_id="e-paper", element_type="paragraph",
            location_label="p. 2", quoted_span="quoted evidence", confidence=1.0,
        )],
    )

    citations = service.citations_from(
        [hit], {"e-paper"}, "KG evidence", notebook_id="active",
    )

    assert citations[0].label == "Meaningful Paper Name · p. 2"


def test_collection_kg_item_gets_bounded_cross_library_original_source():
    from app.services.collection_enumeration import KgObjectItem

    service = _service(
        source_metadata={
            "s-base": {"title": "base.pdf", "is_paper": True,
                       "paper_title": "Reference Paper", "notebook_id": "base"},
        },
        elements={
            "e-base": {
                "id": "e-base", "source_id": "s-base",
                "element_type": "paragraph", "location_label": "p. 9",
                "text": "authoritative excerpt", "metadata": "{}",
            },
        },
    )
    item = KgObjectItem(
        object_id="o-base", object_type="claim", name="Claim",
        section_path="§2", notebook_id="base", tier="base",
        evidence_element_ids=("missing", "e-base"),
    )

    citations = service.collection_item_citations(
        [item], active_notebook_id="active"
    )

    citation = citations["o-base"]
    assert citation.label == "Reference Paper · p. 9"
    assert citation.source_id == "s-base"
    assert citation.element_id == "e-base"
    assert citation.quoted_span == "authoritative excerpt"
    assert citation.notebook_id == "base"


def test_collection_kg_item_rejects_an_evidence_id_from_another_notebook():
    from app.services.collection_enumeration import KgObjectItem

    service = _service(
        source_metadata={
            "s-other": {"title": "private.pdf", "notebook_id": "other"},
        },
        elements={
            "e-other": {
                "id": "e-other", "source_id": "s-other",
                "element_type": "paragraph", "location_label": "p. 1",
                "text": "must not leak", "metadata": "{}",
            },
        },
    )
    item = KgObjectItem(
        object_id="o-base", object_type="claim", name="Corrupt historical row",
        section_path="", notebook_id="base", tier="base",
        evidence_element_ids=("e-other",),
    )

    citations = service.collection_item_citations(
        [item], active_notebook_id="active"
    )

    assert citations == {}


def test_collection_citation_rejects_a_non_participant_even_when_source_matches():
    from app.services.collection_enumeration import KgObjectItem

    service = _service(
        source_metadata={
            "s-other": {"title": "private.pdf", "notebook_id": "other"},
        },
        elements={
            "e-other": {
                "id": "e-other", "source_id": "s-other",
                "element_type": "paragraph", "location_label": "p. 1",
                "text": "must not leak", "metadata": "{}",
            },
        },
    )
    item = KgObjectItem(
        object_id="o-other", object_type="claim", name="Unauthorized row",
        section_path="", notebook_id="other", tier="personal",
        evidence_element_ids=("e-other",),
    )

    assert service.collection_item_citations(
        [item], active_notebook_id="active"
    ) == {}


def test_evidence_context_chunk_golden_matches_master():
    chunks = [
        RetrievedChunk(
            chunk_id="c-active", source_id="s1", source_title="Paper A",
            section_path="1.1", text="active text", relevance=0.9,
            notebook_id="active",
        ),
        RetrievedChunk(
            chunk_id="c-base", source_id="s2", source_title="Paper B",
            section_path="2.2", text="base text", relevance=0.8,
            notebook_id="base",
        ),
    ]
    block, evidence = _service().chunk_context(chunks, notebook_id="active")
    assert block == "k1: active text\nk2: base text"
    assert list(evidence) == ["k1", "k2"]
    # Task 12b review fix: chunk anchors resolve knowhow too (grounded-path
    # button reachability). These chunks carry no element_ids, so the field
    # is present-but-None and no store lookup fires at all.
    # Task 14 codex r2 fix: c-active 的 notebook_id 显式等于本次 ask 的
    # notebook_id("active")——联邦/PPR 检索对本库命中同样会打上 active 自己的
    # notebook_id(并非只在跨库命中时才打标),chunk_context 必须把它归一成空串
    # (镜像 follow_chain.py 的 `hop.notebook_id != active_notebook_id` 处理),
    # 否则前端引用徽章会显示一个多余的"来自「当前笔记本」"库名。
    assert evidence["k1"] == {
        "object_id": "c-active", "object_type": "chunk", "name": "1.1",
        "definition": None, "snippet": "active text", "source_title": "Paper A",
        "location_label": "1.1", "tier": "personal", "notebook_id": "",
        "source_id": "s1", "element_id": "", "relevance": 0.9,
        "knowhow": None,
    }
    # k2(c-base)真正跨库:tier 解析为 base 且 notebook_id 原样带出,供 Task 14
    # 的引用徽章库名映射消费。
    assert evidence["k2"]["tier"] == "base"
    assert evidence["k2"]["notebook_id"] == "base"


def test_evidence_context_knowledge_golden_matches_master():
    hit = RetrievedKnowledge(
        object_id="o1", object_type="concept", payload={"name": "Cascode"},
        evidence=[], tier="base", notebook_id="base",
    )
    block, evidence = _service().knowledge_context("active", [hit])
    assert block == "k1: [concept][base] Cascode — def: stable definition"
    # Task 12b: non-knowhow payload (no table_id/rows) resolves knowhow to
    # None — present as a key so downstream .get("knowhow") reads it, but
    # never populated for ordinary KG concepts/claims.
    assert evidence["k1"] == {
        "object_id": "o1", "object_type": "concept", "name": "Cascode",
        "definition": "stable definition", "snippet": "source excerpt",
        "source_title": "Source A", "location_label": "§1", "tier": "base",
        "notebook_id": "base", "source_id": "", "element_id": "",
        "relevance": 0.0, "knowhow": None,
    }


def test_evidence_context_elements_are_precise_citable_anchors():
    element = RetrievedElement(
        element_id="el-7", source_id="src-2", source_title="Device paper",
        location_label="p. 7", element_type="paragraph",
        text="Body effect changes the threshold voltage.", score=0.91,
    )
    block, evidence = _service().element_context(
        [element], notebook_id="active", id_offset=4000,
    )
    assert block.startswith("k4001: [source-element][personal]")
    assert evidence["k4001"] == {
        "object_id": "el-7", "object_type": "element", "name": "p. 7",
        "definition": element.text, "snippet": element.text,
        "source_id": "src-2", "element_id": "el-7",
        "source_title": "Device paper", "location_label": "p. 7",
        "tier": "personal", "notebook_id": "", "relevance": 0.91,
        "knowhow": None,
    }
    anchors = _service().parse_anchors("supported [k4001]", evidence)
    assert [(item.object_type, item.object_id, item.source_title)
            for item in anchors] == [("element", "el-7", "Device paper")]


def test_evidence_context_knowledge_context_blanks_self_notebook_id():
    """codex r2 fix: federated_retrieve()(retrieval_candidates.py 的
    `_federated_retrieve_impl`)对 active 库自己的命中同样会把
    RetrievedKnowledge.notebook_id 打成 active 自己的 id——resolve_participants
    首项恒为 active 本身(notebook_store.py:66-69),`for nid in notebook_ids:` 第
    一轮 `h.notebook_id = nid` 无条件执行,并不是只有跨库命中才打标(旧假设,
    test_evidence_context_chunk_golden_matches_master 曾经的注释也这样错误
    假设过)。knowledge_context 必须把等于调用方 notebook_id 的值归一成空串,
    镜像 follow_chain.py 的 `hop.notebook_id != active_notebook_id` 处理,否则
    前端引用徽章会显示一个多余的"来自「当前笔记本」"库名。o2 真正跨库
    (来自 base)必须仍然原样带出 notebook_id——不能连带误伤这条既有不变量。"""
    hits = [
        RetrievedKnowledge(
            object_id="o1", object_type="concept", payload={"name": "Self hit"},
            evidence=[], tier="personal", notebook_id="active",
        ),
        RetrievedKnowledge(
            object_id="o2", object_type="concept", payload={"name": "Cross hit"},
            evidence=[], tier="base", notebook_id="base",
        ),
    ]
    _, evidence = _service().knowledge_context("active", hits)
    assert evidence["k1"]["notebook_id"] == "", (
        "hit.notebook_id 等于调用方 active 时必须归一成空串,实为 "
        f"{evidence['k1']['notebook_id']!r}")
    assert evidence["k1"]["tier"] == "personal"
    assert evidence["k2"]["notebook_id"] == "base", (
        "真正跨库命中(o2 来自 base)必须原样带出 notebook_id,实为 "
        f"{evidence['k2']['notebook_id']!r}")
    assert evidence["k2"]["tier"] == "base"


def test_evidence_context_numeric_group_anchors_match_master():
    evidence = {
        "k1": {"object_id": "o1", "object_type": "claim", "name": "A", "tier": "base"},
        "k2": {"object_id": "o2", "object_type": "claim", "name": "B", "tier": "personal"},
    }
    service = _service()
    anchors = service.parse_anchors("supported [k1, k2]; duplicate [k1]", evidence)
    assert [(anchor.key, anchor.object_id, anchor.tier) for anchor in anchors] == [
        ("k1", "o1", "base"), ("k2", "o2", "personal")
    ]
    assert service.parse_anchors("mixed [k1, k999]", evidence) == []
    # Task 14: 两条 evidence 都没带 "notebook_id" 键(render_subgraph_context 的
    # 纯 graph-BFS 节点尚未填充这个键)——`.get` 必须安全回退空串,不抛 KeyError,
    # 徽章優雅退回泛化 tier 文案。
    assert [anchor.notebook_id for anchor in anchors] == ["", ""]


def test_evidence_context_parse_anchors_carries_notebook_id_when_present():
    """chunk_context/knowledge_context 填了 "notebook_id" 键时,parse_anchors
    必须原样透传到 AnswerAnchor,供 Task 14 的引用徽章库名映射消费。"""
    evidence = {
        "k1": {
            "object_id": "o1", "object_type": "claim", "name": "A", "tier": "base",
            "notebook_id": "base-nb",
        },
        "k2": {
            "object_id": "o2", "object_type": "claim", "name": "B", "tier": "personal",
            "notebook_id": "",
        },
    }
    anchors = _service().parse_anchors("[k1, k2]", evidence)
    assert [(anchor.key, anchor.notebook_id) for anchor in anchors] == [
        ("k1", "base-nb"), ("k2", "")
    ]


def test_evidence_context_preserves_tier_and_source_metadata():
    hit = RetrievedKnowledge(
        object_id="o1", object_type="claim", payload={"name": "Claim"},
        evidence=[Evidence(
            source_id="s1", source_title="Source title", element_id="e1",
            element_type="text", location_label="p. 4", quoted_span="quoted",
            confidence=1.0,
        )], tier="base",
    )
    citations = _service().citations_from([hit], {"e1"}, "KG evidence", notebook_id="active")
    assert len(citations) == 1
    assert citations[0].tier == "base"
    assert citations[0].source_id == "s1"
    assert citations[0].location_label == "p. 4"
    # Task 14: 这条 hit 没显式打 notebook_id(RetrievedKnowledge 默认 ""——即
    # "本库,不是跨库联邦命中"),citation.notebook_id 必须原样留空,而不是回填
    # 成某个"当前 notebook"。
    assert citations[0].notebook_id == ""


def test_evidence_context_citations_from_carries_cross_tier_notebook_id():
    """hit 显式标了来源库(federated_retrieve 的产出)时,citations_from 必须把
    它原样带到 Citation.notebook_id,供 Task 14 的引用徽章库名映射消费。"""
    hit = RetrievedKnowledge(
        object_id="o1", object_type="claim", payload={"name": "Claim"},
        evidence=[Evidence(
            source_id="s1", source_title="Source title", element_id="e1",
            element_type="text", location_label="p. 4", quoted_span="quoted",
            confidence=1.0,
        )], tier="base", notebook_id="base-nb",
    )
    citations = _service().citations_from([hit], {"e1"}, "KG evidence", notebook_id="active")
    assert len(citations) == 1
    assert citations[0].notebook_id == "base-nb"


def test_evidence_context_citations_from_blanks_self_notebook_id():
    """codex r4 fix: federated_retrieve()(_federated_retrieve_impl)对 active
    库自己的命中同样会把 RetrievedKnowledge.notebook_id 打成 active 自己的
    id——resolve_participants 首项恒为 active 本身,`for nid in notebook_ids:`
    第一轮 `h.notebook_id = nid` 无条件执行,并不是只有跨库命中才打标(与
    test_evidence_context_knowledge_context_blanks_self_notebook_id 验证过的
    knowledge_context 同一根因)。citations_from 此前没有调用方 notebook_id
    可比较,原样透传——当答案合成失败/模型没吐出任何 [k] 锚点时,前端会把
    这批 citation 当回退列表直接展示,「本库自己」的证据就会被误标成"来自
    「当前笔记本」"。必须把等于调用方 notebook_id 的值归一成空串,镜像
    chunk_context/knowledge_context/render_follow_chain_context 的既有处理;
    真正跨库命中(hit_cross 来自 base)必须仍然原样带出——不能连带误伤既有
    不变量。"""
    hit_self = RetrievedKnowledge(
        object_id="o1", object_type="concept", payload={"name": "Self hit"},
        evidence=[Evidence(
            source_id="s-own", source_title="Own doc", element_id="e-own",
            element_type="text", location_label="p. 1", quoted_span="own quote",
            confidence=1.0,
        )], tier="personal", notebook_id="active",
    )
    hit_cross = RetrievedKnowledge(
        object_id="o2", object_type="concept", payload={"name": "Cross hit"},
        evidence=[Evidence(
            source_id="s-base", source_title="Base doc", element_id="e-base",
            element_type="text", location_label="p. 2", quoted_span="base quote",
            confidence=1.0,
        )], tier="base", notebook_id="base",
    )
    citations = _service().citations_from(
        [hit_self, hit_cross], {"e-own", "e-base"}, "KG evidence", notebook_id="active")
    by_source = {c.source_id: c for c in citations}
    assert by_source["s-own"].notebook_id == "", (
        "hit.notebook_id 等于调用方 active 时必须归一成空串,实为 "
        f"{by_source['s-own'].notebook_id!r}")
    assert by_source["s-own"].tier == "personal"
    assert by_source["s-base"].notebook_id == "base", (
        "真正跨库命中(s-base 来自 base)必须原样带出 notebook_id,实为 "
        f"{by_source['s-base'].notebook_id!r}")
    assert by_source["s-base"].tier == "base"


def test_enumerated_item_key_agrees_across_the_two_layers():
    """引用键的两处拼写必须逐位一致(#402 × PR-2.5 接缝)。

    `evidence_context.collection_item_citations` **建** 这张映射,
    `collection_enumeration_answer` 三处 **查** 它。两边不能共用一个函数——
    evidence_context 是下层,不认识枚举答案胶水层——所以由这条守卫钉住等价。

    这不是假想风险:集成 #402 时 master 有三处副本,漏改一处的后果是**不可见的**
    (卡片出处照常显示,只有答案锚点悄悄丢掉定位器)。
    """
    from types import SimpleNamespace

    from app.services.collection_enumeration_answer import enumerated_item_id
    from app.services.evidence_context import _is_document_row

    element = SimpleNamespace(element_id="el-1", source_id="src-1")
    kg = SimpleNamespace(object_id="ko-1", evidence_element_ids=["el-9"])
    document = SimpleNamespace(source_id="src-1", source_title="论文一")

    def evidence_context_key(item):
        # 与 collection_item_citations 里那段逐字同形。
        return str(
            getattr(item, "element_id", "")
            or getattr(item, "object_id", "")
            or getattr(item, "source_id", "")
            or ""
        )

    for item, expected in ((element, "el-1"), (kg, "ko-1"), (document, "src-1")):
        assert enumerated_item_id(item) == expected
        assert evidence_context_key(item) == expected

    # 元素行绝不退化成按来源计键——否则它会与同一份文档的文档行撞同一个键。
    assert enumerated_item_id(element) != enumerated_item_id(document)
    # 文档行的判定同样只认「有 source_id、无 element/object id」这一种形状。
    assert _is_document_row(document) is True
    assert _is_document_row(element) is False
    assert _is_document_row(kg) is False


def test_evidence_context_folds_clusters_without_merging_participant_maps():
    """codex r5 fix: knowledge_context 逐 hit 在参与库缓存 map 里逆序查找,不再
    把全部参与库 map 合并进新 dict——那是每次答案合成付一遍的 O(全库) 整表
    拷贝(scale 下 cluster map 可达 5M 条),按节合成还要按节数放大。语义必须
    与原 dict.update 逐库覆盖完全一致:后一个参与库(base)对同一 member 的
    canonical 覆盖前一个(active)。本用例的数据形状刻意让两种查找方向给出
    不同答案——active 把 m1 折到 c-active,base 把 m1/m2 都折到 c-base;
    「后库优先」下两个 hit 同折 c-base、簇去重后只剩一条;若逆序被改回正序
    (m1→c-active),或折叠整个失效(m1→m1),都会输出两条而报红。"""
    class _SplitClusterKnowledge(_Knowledge):
        def cluster_map(self, notebook_id):
            if notebook_id == "active":
                return {"m1": "c-active"}
            return {"m1": "c-base", "m2": "c-base"}

    service = EvidenceContextService(
        notebooks=_Notebooks(), sources=_Sources(),
        knowledge=_SplitClusterKnowledge(), settings=Settings(),
    )
    hits = [
        RetrievedKnowledge(
            object_id="m1", object_type="concept", payload={"name": "First"},
            evidence=[], tier="personal", notebook_id="active",
        ),
        RetrievedKnowledge(
            object_id="m2", object_type="concept", payload={"name": "Second"},
            evidence=[], tier="base", notebook_id="base",
        ),
    ]
    _, evidence = service.knowledge_context("active", hits)
    assert list(evidence) == ["k1"], (
        "m1/m2 在「后挂载库覆盖」语义下同折 c-base,第二个 hit 必须被簇去重,"
        f"实得 {list(evidence)}")
    assert evidence["k1"]["object_id"] == "m1"
