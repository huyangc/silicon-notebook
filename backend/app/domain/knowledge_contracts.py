"""Frozen knowledge-domain contracts (Task 13; sunk to app.domain in B3).

Canonical home of the knowledge status vocabularies and the knowledge-graph
size guard. ``app.services.knowledge_contracts`` re-exports every name here
for existing importers (``app.services.sqlite_repository`` in turn re-exports
that as its frozen compatibility surface, the manifest pins those export
sites), and ``app.repositories.sqlite.notebook_store`` re-exports
``USABLE_STATUSES`` for its Task-8 consumers — all references resolve to the
SAME objects.
"""
from __future__ import annotations

from dataclasses import dataclass

# Knowledge statuses that may be surfaced in answers/retrieval (§12 governance).
# 'deprecated' is excluded; 'conflict' is retrieved but flagged elsewhere.
USABLE_STATUSES = ("approved", "reviewed", "project_specific", "conflict")

# Every status a curator can stamp on a knowledge object.
KNOWLEDGE_STATUSES = ("approved", "reviewed", "deprecated", "conflict", "project_specific")

# ---------------------------------------------------------------------------
# KG 质量分析的只读聚合契约(设计:docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md)
#
# 桶名同时是 SQLite / PostgreSQL 两侧 SQL 里的字面量。放在这里的理由是 parity:
# 两个后端各写各的 SQL,但**呈现给上层的桶集合与顺序必须逐字一致**,否则同一个报告
# 换后端就换了坐标轴。store 层负责把缺席的桶补 0 并按这里的顺序返回,调用方因此永远
# 拿到定长、定序的直方图,不必知道库里恰好出现了哪些桶。
# ---------------------------------------------------------------------------

# 簇大小(合并后一个知识对象吸收了几条原始对象)的固定分桶,按顺序返回。
# 顶档叫 "51+" 而不是 "50+":实际语义是 **> 50**(恰好 50 归 "11-50"),而桶名会
# 直接上屏给用户,"50+" 会被读成「含 50」。
CLUSTER_SIZE_BUCKETS = ("1", "2", "3", "4-5", "6-10", "11-50", "51+")

# 「成员全被排除、整簇落空」的那一档。它**不是**一个尺寸桶(不进 CLUSTER_SIZE_BUCKETS),
# 而是单独报出的排除量——混进 "1" 会把落空的簇算成单元素簇。
#
# 它和上面的尺寸桶一样是两侧 SQL 里的 CASE 字面量,所以必须和它们并排放在这个契约
# 模块里(初版把它藏在 kg_analysis_payloads.py,正好违反本段开头立的那条原则)。
EMPTY_CLUSTER_BUCKET = "empty"

# `concept_clusters.object_type` 的**已知**取值,按这个顺序分组返回。
# 真源是 app.services.extraction_profiles.OBJECT_TYPE_LABELS(抽取管线只产出这四类;
# 见 knowledge_lifecycle 的 _TYPE_MERGE:concept 走向量近重复合并,claim/formula/
# procedure 只做精确种子合并)。这里刻意重抄一份而不 import,是为了不让契约模块反向
# 依赖抽取配置;drift 由 tests/test_kg_analysis_queries.py 的一致性用例钉住。
CLUSTER_OBJECT_TYPES = ("concept", "claim", "formula", "procedure")

# 上面四类之外的 object_type 统一归进这一组。knowhow 投影会用**用户的列名**建自定义
# 类型(本机库里真实出现过「周几」「根因分析动作」这类值),那是内容不是词表——所以
# 这个查询层只报「其他组的成员行数/簇数」与「不同类型的个数」,**绝不把类型名放进
# 返回载荷**;要不要脱敏、怎么脱敏由上层决定,查询层不制造泄漏面。
OTHER_OBJECT_TYPE_GROUP = "other"

# 直方图按 object_type 分组返回时的定长定序分组键。
CLUSTER_OBJECT_TYPE_GROUPS = (*CLUSTER_OBJECT_TYPES, OTHER_OBJECT_TYPE_GROUP)

# 边的出处分桶。只有 relink 会往 evidence[0].basis 写标记(见 services/kg/relink.py);
# 没有 basis 的边**不能**记成「LLM 抽取」——knowhow 投影写的 about 边就是 evidence='[]',
# 与 LLM 抽取的边在这里不可区分,故一律归 "untagged",别替它认领出处。
RELATION_PROVENANCE_BUCKETS = (
    "relink:shared-element",
    "relink:name-match",
    "relink:other",
    "tagged:other",
    "untagged",
)

# 被口径排除、必须单独报出的边(设计 §3.5:被排除的量不凭空消失)。
RELATION_EXCLUSION_BUCKETS = ("rejected", "endpoint_unusable")

# 有界返回的硬上限。调用方传的 N 会被 clamp 到这里,返回值带截断标志。
LARGEST_CLUSTERS_MAX = 200
COMMUNITY_OVERVIEW_MAX = 200
COMMUNITY_TOP_MEMBERS_MAX = 20

# T3 的两个只读端点的有界返回上限(同上:store 侧 clamp,载荷带截断/覆盖率)。
#
# `kg_source_profiles` 每 notebook 至多「来源数」行(生产 base 库 48 836),一页最多
# 200 行 —— 4.8 万行一次返回既是几 MB 的 payload,也不是任何人能读的东西,所以分页
# 是硬要求而不是优化。
KG_SOURCE_PAGE_MAX = 200
# `kg_community_edges` 每 notebook 至多 `MAX_PERSISTED_COMMUNITY_EDGES`(20 万)行。
# 俯瞰图只画最重的那几条,2000 已经比任何可读的图多一个数量级;上限的作用是让「返回
# 多少」与「库里存了多少」永远分得开(载荷同时给 returned / stored / stored_total)。
KG_COMMUNITY_EDGES_MAX = 2000

# `concept_detail` 的 hub 簇成员分页大小(KG-4 应用侧修复,R3·T-B2)。生产已见
# 8-9M 簇行的 hub 簇——`concept_cluster_detail_rows` 此前无 LIMIT,一次性把全部
# 成员连同 payload/evidence 整批返回。200 与 KG_SOURCE_PAGE_MAX 同量级,既是默认
# 页大小也是硬上限;`attached`/`evidence` 随之改为按页内成员计算(分页语义,见
# product-and-api 文档),前端「加载更多成员」翻页即可看到完整集合。
CONCEPT_DETAIL_PAGE_MAX = 200


class KnowledgeGraphTooLargeError(Exception):
    """Raised by knowledge_graph() (legacy GET /notebooks/{id}/graph) when the
    notebook exceeds settings.viz_sync_build_max_objects — that endpoint has
    no bounded fallback (unlike unified_graph), so it refuses outright rather
    than materializing an unbounded in-memory graph. The route maps this to
    HTTP 413."""


@dataclass(frozen=True)
class PromotionApproval:
    """Outcome of the in-transaction promotion approval primitive
    (GovernanceStore.approve_promotion_in_transaction)."""

    candidate_id: str
    source_notebook_id: str
    source_object_id: str
    base_notebook_id: str
    base_object_id: str
    created_new_object: bool
