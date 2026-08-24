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

    def cluster_fold(self, notebook_id, object_ids):
        # Mirrors the old cluster_map()'s empty-map default: every id misses
        # and _canonical() falls back to the id itself.
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

    def relation_support_counts(self, notebook_id, triples):
        return {triple: 1 for triple in triples}


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
            "file_name": "opaque-file-name.pdf",
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
    assert evidence["k4001"]["source_file_name"] == "opaque-file-name.pdf"
    assert anchors[0].source_title == "Meaningful Paper Name"
    assert anchors[0].source_file_name == "opaque-file-name.pdf"


def test_fallback_citation_label_uses_parsed_paper_title():
    service = _service(source_metadata={
        "s-paper": {
            "title": "opaque-file-name.pdf", "is_paper": True,
            "paper_title": "Meaningful Paper Name",
            "file_name": "opaque-file-name.pdf",
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
    assert citations[0].source_file_name == "opaque-file-name.pdf"


def test_collection_kg_item_gets_bounded_cross_library_original_source():
    from app.services.collection_enumeration import KgObjectItem

    service = _service(
        source_metadata={
            "s-base": {"title": "base.pdf", "file_name": "base.pdf",
                       "is_paper": True, "paper_title": "Reference Paper",
                       "notebook_id": "base"},
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
    assert citation.source_file_name == "base.pdf"
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


def test_chunk_context_duplicate_does_not_spend_the_character_budget():
    chunks = [
        RetrievedChunk(
            chunk_id="header-1", source_id="s1", source_title="Paper",
            section_path="1", text="Repeated header", relevance=0.9,
            notebook_id="active",
        ),
        RetrievedChunk(
            chunk_id="header-2", source_id="s1", source_title="Paper",
            section_path="2", text=" repeated\nheader ", relevance=0.8,
            notebook_id="active",
        ),
        RetrievedChunk(
            chunk_id="body", source_id="s1", source_title="Paper",
            section_path="2", text="Distinct body", relevance=0.7,
            notebook_id="active",
        ),
    ]

    block, evidence = _service().chunk_context(
        chunks, notebook_id="active", budget_chars=100
    )

    assert block == "k1: Repeated header\nk2: Distinct body"
    assert [row["object_id"] for row in evidence.values()] == ["header-1", "body"]


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


def test_evidence_context_accepts_chinese_bracket_citation_markers():
    evidence = {
        "k1": {"object_id": "o1", "object_type": "claim", "name": "A"},
        "k2": {"object_id": "o2", "object_type": "claim", "name": "B"},
    }
    anchors = _service().parse_anchors(
        "中文括号【k1】与中文逗号复合引用【k2，k1】。", evidence
    )
    assert [(anchor.key, anchor.object_id) for anchor in anchors] == [
        ("k1", "o1"), ("k2", "o2")
    ]


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
        def cluster_fold(self, notebook_id, object_ids):
            table = (
                {"m1": "c-active"} if notebook_id == "active"
                else {"m1": "c-base", "m2": "c-base"}
            )
            return {oid: table[oid] for oid in object_ids if oid in table}

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


def test_evidence_context_knowledge_context_never_loads_full_cluster_map():
    """T3(B2 有界化,批 1)守卫:knowledge_context 绝不再调用整表 cluster_map()
    ——调用即报红,直接钉住「B2 热点」被拔掉这件事(MUT-4 的反向验证目标)。
    cluster_fold() 收到的 ids 必须恰为本次装配需要折叠的集合(命中
    object_id ∪ priority_object_ids),既不是空、也不是超出这个集合的「全库」
    (MUT-5 的反向验证目标)。本用例的 priority_object_ids=["o3"] 刻意不是
    hits 的子集(o3 不在 hits 里),用来测契约的并集上限本身——生产唯一调用方
    report_engine.py 今天传入的 bound_ids 恒 ⊆ hits(见 evidence_context.py
    needed_ids 处的注释),但 knowledge_context 的实现不能依赖这条调用方今天
    才成立的不变量。"""
    class _CountingKnowledge(_Knowledge):
        def __init__(self):
            self.fold_calls: list[tuple[str, list[str]]] = []

        def cluster_map(self, notebook_id):
            raise AssertionError("knowledge_context must not call cluster_map()")

        def cluster_fold(self, notebook_id, object_ids):
            ids = list(object_ids)
            self.fold_calls.append((notebook_id, ids))
            return {}

    knowledge = _CountingKnowledge()
    service = EvidenceContextService(
        notebooks=_Notebooks(), sources=_Sources(),
        knowledge=knowledge, settings=Settings(),
    )
    hits = [
        RetrievedKnowledge(
            object_id="o1", object_type="concept", payload={"name": "First"},
            evidence=[], tier="personal", notebook_id="active",
        ),
        RetrievedKnowledge(
            object_id="o2", object_type="concept", payload={"name": "Second"},
            evidence=[], tier="base", notebook_id="base",
        ),
    ]
    service.knowledge_context("active", hits, priority_object_ids=["o3"])

    # _Notebooks.participant_notebook_ids("active") == ["active", "base"]:
    # one bounded fold call per participant, none of them the full table.
    assert [notebook_id for notebook_id, _ids in knowledge.fold_calls] == ["active", "base"]
    for _notebook_id, ids in knowledge.fold_calls:
        assert ids, "cluster_fold must not be called with an empty id set"
        assert set(ids) == {"o1", "o2", "o3"}, (
            "cluster_fold must receive exactly the hit ∪ priority id set, "
            f"got {sorted(ids)}"
        )


def test_evidence_context_relation_support_groups_by_relation_source_notebook():
    """P2-1(评审 MUT-A 反向验证):relations 段必须按每条关系行自己的
    ``row["notebook_id"]``(挂载库的真实归属)分组去查 relation_support_counts,
    绝不能被换成调用方的 active notebook_id——真实 canonical_relations 是按各
    自库存的表,用 active 的 id 去查一条挂载库贡献的边必然 miss(退回默认
    support=1,×N源 后缀因此消失)。

    这里用一个按 notebook_id 区分行为的假 Knowledge 端口来模拟这个真实语义:
    关系行来自挂载库("base"),只有拿 "base" 去查才返回 support=2,拿别的 id
    (比如误传进来的 active)去查一律回退默认值 1——与真实
    ``graph_retrieval.relation_support_counts`` 命中/未命中的落地行为一致。

    MUT-A 反向验证:把 evidence_context.py 里 ``self.knowledge
    .relation_support_counts(nb_id, triples)`` 的第一个参数从 ``nb_id``(行自己
    的归属)换成外层调用方的 ``notebook_id``(active),这条测试必须报红
    ——挂载库关系在 active 库查不到,×N源 后缀会消失。"""
    class _MultiNotebookKnowledge(_Knowledge):
        def in_network_relations(self, participant_ids, object_ids):
            return [{
                "source_object_id": "o1", "edge_type": "supports",
                "target_object_id": "o2", "notebook_id": "base",
            }]

        def relation_support_counts(self, notebook_id, triples):
            # 镜像真实存储语义:canonical_relations 是按各自库存的表,拿错
            # notebook_id 去查必然 miss、回退默认值 1(见
            # graph_retrieval.relation_support_count 的 `hit[1] if hit else 1`)。
            support = 2 if notebook_id == "base" else 1
            return {triple: support for triple in triples}

    service = EvidenceContextService(
        notebooks=_Notebooks(), sources=_Sources(),
        knowledge=_MultiNotebookKnowledge(), settings=Settings(),
    )
    hits = [
        RetrievedKnowledge(
            object_id="o1", object_type="concept", payload={"name": "First"},
            evidence=[], tier="base", notebook_id="base",
        ),
        RetrievedKnowledge(
            object_id="o2", object_type="concept", payload={"name": "Second"},
            evidence=[], tier="base", notebook_id="base",
        ),
    ]
    block, _evidence = service.knowledge_context("active", hits)
    assert "relations:" in block
    assert "(×2源)" in block, (
        "relation support lookup must be grouped/queried by the relation "
        f"row's OWN notebook_id (base), not the caller's active notebook; "
        f"got: {block!r}")
