"""KG 质量分析只读聚合的**后端中性**装配层(T1)。

承 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md`「T1 只读聚合查询层」。

为什么单独一个模块:SQLite 与 PostgreSQL 各写各的 SQL(占位符/JSON 算子/窗口函数
取舍都不同),但**返回给上层的形状必须逐字一致**——定长定序的分桶与分组、同名的字段、
同样的截断标志。把「SQL 结果 → 响应载荷」这一步抽成两侧共用的纯函数,parity 就是
结构性的而不是靠两边各自记得补桶。本模块只做纯计算:不碰连接、不拼 SQL、不判 dialect
(同 `app.repositories.text_whitespace` / `knowhow_asset_refs` 的中性 helper 惯例)。

分桶的 CASE 仍然写在各自的 SQL 里(分桶必须发生在库内——生产库 200 万簇 / 800 万边,
把明细拉进 Python 再聚合是红线),这里只负责补零、定序、算合计与截断标志。

**未知桶一律硬失败,绝不静默吞掉。** 反方向(SQL 冒出契约里没有的桶名)如果只是
`rows.get(name, 0)` 地取,那些行就凭空消失了:实测塞一个未知 key 进去,`clusters`
与 `member_rows` 会少算,而 `excluded_member_rows` 仍把它们算进分母——不变式
「member_rows + excluded == 总行数」当场断掉且没有任何报错。所以这里对未知桶名
`raise ValueError`,让 SQL 与契约的漂移在第一次调用就炸出来。
(`object_type` 是唯一的例外,见 `OTHER_OBJECT_TYPE_GROUP`:它本来就允许出现契约外的
取值,归进显式的「其他」组,不是错误。)
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from app.domain.knowledge_contracts import (
    CLUSTER_OBJECT_TYPE_GROUPS,
    CLUSTER_OBJECT_TYPES,
    CLUSTER_SIZE_BUCKETS,
    EMPTY_CLUSTER_BUCKET,
    OTHER_OBJECT_TYPE_GROUP,
    RELATION_EXCLUSION_BUCKETS,
    RELATION_PROVENANCE_BUCKETS,
)

_HISTOGRAM_BUCKETS = frozenset((*CLUSTER_SIZE_BUCKETS, EMPTY_CLUSTER_BUCKET))
_RELATION_BUCKETS = frozenset((*RELATION_PROVENANCE_BUCKETS, *RELATION_EXCLUSION_BUCKETS))


def clamp(value: int, maximum: int) -> int:
    """把调用方传的上限收进 [1, maximum]。无界返回在千万级库上就是一次 OOM。"""
    return max(1, min(int(value), int(maximum)))


def artifact_ledger_rows(
    rows: Iterable[Tuple[str, int, Any, str]],
) -> Dict[str, Dict[str, object]]:
    """``(kind, kg_mutation_seq, payload, created_at)`` → ``{kind: {…}}``(T3 读账本)。

    两个后端的 ``payload`` 列类型不同(SQLite 是 JSON **文本**,PostgreSQL 是
    ``jsonb`` → psycopg 直接给 dict),但上层拿到的形状必须逐字一致,所以解析与校验
    放在这个中性函数里,两侧 store 只管把原始行喂进来。

    **畸形 payload 一律硬失败,不吞。** 账本行的存在与否是「这份产物在不在」的唯一
    判据(见 `kg_analysis_precompute` 段头),所以「行在、内容读不出来」既不是「缺失」
    也不是「为空」—— 静默跳过它会让下游把一份坏掉的产物读成「从来没算过」,而那正是
    §3.3 要消灭的那类误读。写入口两侧都只写 dict(`check_artifact_payloads` +
    `allow_nan=False` / `_json_document`),所以这里炸出来只可能是库被手工改过。
    """
    out: Dict[str, Dict[str, object]] = {}
    for kind, seq, payload, created_at in rows:
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise ValueError(
                    f"KG 分析产物账本:kind={kind} 的 payload 不是合法 JSON({exc})"
                ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"KG 分析产物账本:kind={kind} 的 payload 不是对象"
                f"(拿到 {type(payload).__name__})"
            )
        out[str(kind)] = {
            "kg_mutation_seq": int(seq),
            "payload": payload,
            "created_at": str(created_at or ""),
        }
    return out


def _reject_unknown_buckets(seen, allowed: frozenset, what: str) -> None:
    unknown = sorted(set(seen) - allowed)
    if unknown:
        raise ValueError(
            f"{what}:SQL 返回了契约外的桶名 {unknown};"
            f"合法取值见 app.domain.knowledge_contracts。"
            "静默忽略会让这些行凭空消失、并悄悄打破「计数 + 排除量 = 总行数」"
        )


class _HistogramGroup:
    """一个 object_type 分组的累加器。纯记账,不对外暴露。"""

    __slots__ = ("buckets", "member_rows", "clusters", "excluded", "empty",
                 "empty_member_rows", "object_types")

    def __init__(self) -> None:
        self.buckets: Dict[str, Tuple[int, int]] = {}
        self.member_rows = 0
        self.clusters = 0
        self.excluded = 0
        self.empty = 0
        self.empty_member_rows = 0
        # 落进本组的**不同** object_type 个数。只有「其他」组会 > 1,而且只报个数、
        # 不报名字(自定义类型名是用户的表头,属于内容)。
        self.object_types: set = set()

    def add(self, object_type: str, bucket: str, clusters: int,
            members: int, excluded: int) -> None:
        self.object_types.add(object_type)
        self.excluded += excluded
        if bucket == EMPTY_CLUSTER_BUCKET:
            self.empty += clusters
            self.empty_member_rows += excluded
            return
        prev_clusters, prev_members = self.buckets.get(bucket, (0, 0))
        self.buckets[bucket] = (prev_clusters + clusters, prev_members + members)
        self.clusters += clusters
        self.member_rows += members

    def payload(self) -> Dict[str, object]:
        return {
            "member_rows": self.member_rows,
            "clusters": self.clusters,
            "excluded_member_rows": self.excluded,
            "empty_clusters": self.empty,
            "empty_cluster_member_rows": self.empty_member_rows,
            "buckets": [
                {
                    "bucket": name,
                    "clusters": self.buckets.get(name, (0, 0))[0],
                    "members": self.buckets.get(name, (0, 0))[1],
                }
                for name in CLUSTER_SIZE_BUCKETS
            ],
        }


def cluster_histogram_payload(
    rows: Mapping[Tuple[str, str], Tuple[int, int, int]],
) -> Dict[str, object]:
    """``{(object_type, bucket): (clusters, members, excluded_members)}`` → 分布载荷。

    ``rows`` 只含库里实际出现的 (类型, 桶) 组合;缺席的在这里补 0,并按
    CLUSTER_OBJECT_TYPE_GROUPS × CLUSTER_SIZE_BUCKETS 定序,调用方永远拿到
    **定长定序**的分组直方图。

    为什么按 object_type 分组而不是只算 concept:`concept_clusters` 同时装 concept /
    claim / formula / procedure 四类,后三类只做精确种子合并、天然几乎全是单元素簇,
    合在一起算「收敛率」会被稀释成噪音。分开报,每一类的收敛率各自可读,总量也不丢。

    契约外的 `object_type`(knowhow 投影用用户列名建的自定义类型)归进
    ``OTHER_OBJECT_TYPE_GROUP``;载荷只给它的计数与 ``object_types``(不同类型的个数),
    **不给类型名**。

    顶层的 ``member_rows`` / ``clusters`` / ``buckets`` 是**全类型合计**,由同一批分桶
    求和得出——与分组直方图天然自洽,不需要另跑 COUNT。
    """
    _reject_unknown_buckets(
        (bucket for _, bucket in rows), _HISTOGRAM_BUCKETS, "簇大小直方图"
    )
    groups: Dict[str, _HistogramGroup] = {
        name: _HistogramGroup() for name in CLUSTER_OBJECT_TYPE_GROUPS
    }
    total = _HistogramGroup()
    for (object_type, bucket), (n_clusters, n_members, n_excluded) in rows.items():
        key = object_type if object_type in CLUSTER_OBJECT_TYPES else OTHER_OBJECT_TYPE_GROUP
        groups[key].add(object_type, bucket, n_clusters, n_members, n_excluded)
        total.add(object_type, bucket, n_clusters, n_members, n_excluded)
    payload = total.payload()
    payload["by_object_type"] = [
        {
            "object_type": name,
            # 「其他」组里有几个不同的 object_type。已知四类恒为 0/1(有行就是 1),
            # 只有 other 组可能 > 1。名字刻意不出现在载荷里。
            "object_types": len(groups[name].object_types),
            **groups[name].payload(),
        }
        for name in CLUSTER_OBJECT_TYPE_GROUPS
    ]
    return payload


def largest_clusters_payload(
    rows: Sequence[Tuple[str, str, int]], limit: int
) -> Dict[str, object]:
    """``[(canonical_id, canonical_name, members)]`` → 头部簇载荷。

    查询多取一行来判截断,这里负责切掉那一行并落 ``truncated``——绝不静默截断。
    边界是 ``> limit`` 而不是 ``>= limit``:恰好取到 ``limit`` 个簇时**没有**被截断,
    写成 ``>=`` 会在「簇数恰等于上限」这一档谎报截断(回归用例
    ``test_largest_clusters_exactly_at_the_limit_is_not_truncated`` 钉这一档)。

    ``object_type`` 显式声明榜单口径:只取 concept(整句 claim 当名字上榜没有意义)。
    其他类型的成员行数与簇数由 ``cluster_size_histogram`` 按类型分组报出,不在这里
    重复扫一遍表。
    """
    truncated = len(rows) > limit
    return {
        "object_type": "concept",
        "limit": limit,
        "truncated": truncated,
        "clusters": [
            {"canonical_id": cid, "canonical_name": name, "members": members}
            for cid, name, members in rows[:limit]
        ],
    }


def community_overview_payload(
    communities: Sequence[Tuple[str, int, Sequence[str]]],
    total: int,
    level: int,
    limit: int,
    top_k: int,
) -> Dict[str, object]:
    """``[(community_id, size, top_member_names)]`` → 主题板块列表载荷。

    ``total``/``returned``/``truncated`` 三件套让「还有多少板块没返回」是显式的;每个
    板块另带 ``top_members_truncated``(成员数 > K),避免把 top-K 误读成板块全貌。
    """
    return {
        "level": level,
        "total": total,
        "returned": len(communities),
        "truncated": total > len(communities),
        "limit": limit,
        "top_members_limit": top_k,
        "communities": [
            {
                "id": cid,
                "size": size,
                "top_members": list(names),
                "top_members_truncated": size > len(names),
            }
            for cid, size, names in communities
        ],
    }


def relation_provenance_payload(rows: Mapping[str, int]) -> Dict[str, object]:
    """``{bucket: count}`` → 边出处构成载荷。

    出处桶与排除桶各自补 0 定序。``counted`` 是真正进入产品图的边(通过口径的),
    ``total_rows`` = counted + 全部排除量 —— 被排除的边不凭空消失。
    """
    _reject_unknown_buckets(rows, _RELATION_BUCKETS, "边出处构成")
    buckets = {name: int(rows.get(name, 0)) for name in RELATION_PROVENANCE_BUCKETS}
    excluded = {name: int(rows.get(name, 0)) for name in RELATION_EXCLUSION_BUCKETS}
    counted = sum(buckets.values())
    relink = sum(
        count for name, count in buckets.items() if name.startswith("relink:")
    )
    return {
        "counted": counted,
        "relink": relink,
        "buckets": buckets,
        "excluded": excluded,
        "total_rows": counted + sum(excluded.values()),
    }
