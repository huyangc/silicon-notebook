"""KG-analysis artifact-ledger contracts (sunk from app.services.kg_analysis_precompute in B3).

The artifact-kind vocabulary and the write-side ledger guard
(``check_artifact_payloads``) are consumed directly by app.repositories
(unified_kg_store, both backends) to validate/batch the precomputed KG
quality-analysis artifacts before they hit the DB. This module also carries
their transitive dependency closure (``artifact_cluster_seq`` /
``artifact_cluster_seq_is_unknown`` / ``CLUSTER_DEPENDENT_ARTIFACT_KINDS`` /
``CLUSTER_SEQ_PAYLOAD_KEY``) because ``check_artifact_payloads`` calls them
directly. ``batched`` is an unrelated pure batching helper the same store
modules import from the same historical module.

``app.services.kg_analysis_precompute`` re-exports every name here unchanged
(and keeps its own ``stamp_cluster_seq`` / ``artifact_is_current`` /
``_cluster_generation_verdict`` / ``_generation_verdict`` — NOT part of this
move — importing these same constants back for its own internal use, so
normalization stays a single source of truth).

Pure, zero app.services/app.repositories dependency.
"""
from __future__ import annotations

from typing import Dict, Iterable, Iterator, List, Mapping, Optional


# 产物账本 `kg_analysis_artifacts.kind` 的**全部**合法取值。
# 前三条的 payload 就是产物本身(T1 三个只读聚合的返回载荷);后两条是明细表
# (kg_community_edges / kg_source_profiles)的账本行,payload 只放汇总与口径参数。
#
# ⚠ 账本行的**存在与否**才是「这份产物在不在」的唯一判据,明细表的行数不是:
# 单一板块的图 legitimately 产出 0 条跨板块边,靠行数分不出「空」与「没算过」。
ARTIFACT_CLUSTER_HISTOGRAM = "cluster_size_histogram"
ARTIFACT_LARGEST_CLUSTERS = "largest_clusters"
ARTIFACT_RELATION_PROVENANCE = "relation_provenance"
ARTIFACT_COMMUNITY_EDGES = "community_edges"
ARTIFACT_SOURCE_PROFILES = "source_profiles"
ARTIFACT_KINDS = (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_SOURCE_PROFILES,
)
_ARTIFACT_KINDS = frozenset(ARTIFACT_KINDS)

# **唯一**允许合法缺席的产物。一个板块都没有的库(对象有、关系没有,或者关系全被
# min-size 过滤掉)算不出任何来源画像:每个来源的 `mainstream_share` 都会是 0.0,
# 而那张表单独看是在说「所有来源都关联稀疏」—— 一句彻头彻尾的谎话。账本 payload 里
# 的 `head_communities: 0` 分辨得出来,但明细表会被单独查询、单独渲染。
#
# 所以这一档的处置是**让产物真的缺失**(不写账本行、不写明细行),而不是写一张全 0 的
# 表再要求下游先读账本才敢信它。缺失是诚实的,全 0 不是。
OPTIONAL_ARTIFACT_KINDS = frozenset({ARTIFACT_SOURCE_PROFILES})
REQUIRED_ARTIFACT_KINDS = _ARTIFACT_KINDS - OPTIONAL_ARTIFACT_KINDS

# 依赖**板块划分**的两份产物 —— 它们的明细行(`kg_community_edges` /
# `kg_source_profiles`)直接存 `community_id`。
#
# ⚠ `replace_communities` 会**重铸板块 id**(`_new_id("cm")`),所以板块一被重写,
# 这两份的每一行立刻变成悬空引用。它们必须与重铸**同事务**作废,否则:
#   · 报告会把已经不存在的板块画进俯瞰图、列进来源画像的「最集中板块」;
#   · T3 的记忆化签名(state 的 seq + 账本行的 seq/created_at)一个字段都不会变 ——
#     `force=True` 在**同一个** `kg_mutation_seq` 上重铸板块、而随后的预计算又恰好
#     失败时,已预热的缓存会**无限期**继续吐上一套板块,直到 LRU 淘汰或进程重启。
#     作废账本行同时也就动了签名,这是这份缓存唯一 O(1) 的失效手段:签名里放不下
#     「板块世代」——任何从 `communities` 现算的世代标记都是 O(板块数) 的读,而那正是
#     这份缓存要省掉的那笔钱(生产 88 580 个板块)。
#
# ⚠ **但这个手段只在「确实有一行可作废」时成立**(codex 第 10 轮 P2):上一轮预计算失败
# 后账本可能整个为空、也可能只剩三条与板块无关的统计快照,那两种形态下这次作废是个
# **no-op** —— 板块换了一整套,签名一个字节没动。补法不在写侧(这里已经没有更多东西可
# 删了),而在读侧:`kg_analysis._signature_tracks_board_recasts` 在那一档干脆不写缓存。
# 改动本常量覆盖的集合时,那道判据自动跟着改 —— 它就是按这个集合导出的。
#
# 另三份(三条统计快照)与板块划分**无关**,刻意不在此列:作废它们只会平白丢掉一份
# 仍然可读、只是「落后 N 次变更」的快照。
#
# ⚠ 这两份的簇世代戳记的是**板块划分建在哪一代合并结果上**,不是「折叠时读到的
# cluster_map 是哪一代」——两者在「只补账本」那条路径上不是同一个数,而把后者当前者
# 盖上去,就是 codex 第 7 轮评审的 P2。判据与写法见 `stamp_cluster_seq`。
BOARD_DEPENDENT_ARTIFACT_KINDS = (ARTIFACT_COMMUNITY_EDGES, ARTIFACT_SOURCE_PROFILES)
_BOARD_DEPENDENT_ARTIFACT_KINDS = frozenset(BOARD_DEPENDENT_ARTIFACT_KINDS)

# 依赖**簇世代**(`unified_kg_state.cluster_mutation_seq`)的产物。
#
# ⚠ 为什么这是一条独立于 `kg_mutation_seq` 的判据:`concept_clusters` 的写路径
# (`write_clusters` / `append_clusters` / rebuild 的 cluster-map swap)**刻意不动**
# `kg_mutation_seq` —— 合并是幂等的重算,让它抬 KG 世代会把每一次「整理」都伪装成
# 一次内容变更。变化信号独立成列(见 `unified_kg_store.bump_cluster_seq`)。于是只看
# `kg_mutation_seq` 的新鲜度契约对**整条簇写路径完全失明**:簇变了、直方图与收敛率
# 跟着变了,而账本闸短路、读侧报「与当前一致」。codex 第 5 轮评审报的就是这一条。
#
# 分类的依据是**那几条查询到底读没读 `concept_clusters`**,不是「感觉上相关」:
#   · cluster_size_histogram  `FROM concept_clusters c LEFT JOIN knowledge_objects o
#                              … GROUP BY c.object_type, c.canonical_id` —— 它数的就是
#                              这张表的行;
#   · largest_clusters        `FROM concept_clusters c JOIN knowledge_objects o …
#                              WHERE c.object_type='concept' GROUP BY c.canonical_id`;
#   · community_edges         折叠的输入边表来自 `community_graph_rows`,那条
#                              `LEFT JOIN concept_clusters cs/ct` 把每条边的两端映射到
#                              canonical —— 簇一变,同一批关系折出来的板块对就不同;
#   · source_profiles         `source_community_counts` 的 `COALESCE(c.canonical_id,
#                              o.id)` 同样经 `concept_clusters` 再 join
#                              `community_members`。
#
# **`relation_provenance` 刻意不在此列**,而且这不是省事:它的 SQL 只有
# `knowledge_relations` 全扫 + 两次 `knowledge_objects` 端点探查,一个字都没提
# `concept_clusters`(见两侧 store 的 `relation_provenance_counts`)。它同时是五份里
# **最贵**的一份(生产 836 万边、每行两次随机 PK 探查,与仓库那次「835 万边冷扫
# 39 分钟」同量级)。把它一刀切进作废集合,等于每一次纯合并写入都白付一趟全表扫,
# 而它的输入一个字节都没变。
CLUSTER_DEPENDENT_ARTIFACT_KINDS = frozenset({
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_SOURCE_PROFILES,
})

# 簇世代盖在账本 payload 里的字段名。
#
# ⚠ **刻意不加列。** `kg_analysis_artifacts` 的 `payload` 两侧分别是 JSON 文本与
# jsonb,加字段不需要迁移;而加一列要追加 `_migration_N` + bump `SCHEMA_VERSION`,
# 波及全仓的迁移计数断言。世代是**每行**盖的(不是整批一个),这样每一份产物都能
# 独立回答「我建在哪一代合并结果上」—— 与 `kg_mutation_seq` 那一列同样的粒度。
CLUSTER_SEQ_PAYLOAD_KEY = "built_at_cluster_seq"


def artifact_cluster_seq(kind: str, payload: Mapping[str, object]) -> Optional[int]:
    """这份产物记下的簇世代;``None`` = 无从判断。

    三种 ``None``,前两种刻意不区分(**下游对它们的处置相同**):
      · 这个 kind 与簇世代无关(`relation_provenance`)—— 没有落后量可报;
      · 依赖簇却没盖戳(库被手工改过,或者是本次修复**之前**写下的行)—— 判不出
        它建在哪一代合并结果上,那就当它不新鲜:闸会补跑、读侧报「未知」。
    第三种要区分,由 `artifact_cluster_seq_is_unknown` 单独回答:依赖板块的两份**显式**
    记下 ``None``(板块划分是库里现成的,建在哪一代无从判断,见 `stamp_cluster_seq`)。
    它不是「漏盖」,补跑也补不出来,所以闸不能拿它当「要重算」。
    """
    if kind not in CLUSTER_DEPENDENT_ARTIFACT_KINDS:
        return None
    value = payload.get(CLUSTER_SEQ_PAYLOAD_KEY)
    # bool 是 int 的子类,`True` 会被 `isinstance(value, int)` 放行并当成 1。
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def artifact_cluster_seq_is_unknown(
    kind: str, payload: Mapping[str, object]
) -> bool:
    """这一份是不是**显式**记着「板块划分建在哪一代合并结果上无从判断」。

    只有依赖板块的两份可能是这一档(`stamp_cluster_seq` 只对它们写 ``None``),而且
    必须是**键在、值为 None**:键干脆不在是「漏盖」,值是别的类型是「被改坏」——
    那两种都判不新鲜,而这一档判「无从判断」。三者的差别是这条修复的全部要害,
    所以判据写在一处、由 `check_artifact_payloads` 在写入口反向封死。
    """
    return (
        kind in _BOARD_DEPENDENT_ARTIFACT_KINDS
        and CLUSTER_SEQ_PAYLOAD_KEY in payload
        and payload[CLUSTER_SEQ_PAYLOAD_KEY] is None
    )


def check_artifact_payloads(payloads: Mapping[str, dict]) -> None:
    """账本写入口的守卫:**多写**与**少写**都硬失败。

    只拒未知 kind 是不够的。「产物在不在 = 账本行在不在」这条判据依赖五行(空板块库
    是四行)一起写出来;少写一行,下游看到的是「这份产物从来没算过」,而实际上是这一轮
    忘了算 —— 两者在读侧完全无法区分,而且没有任何报错。

    ``ARTIFACT_SOURCE_PROFILES`` 是唯一允许缺席的一份,理由见
    ``OPTIONAL_ARTIFACT_KINDS``;而且它只在**真的一个板块都没有**时才可以缺席,
    这一条由 `community_edges` 账本里的 ``communities`` 计数当场复核 —— 否则
    「允许缺席」就会变成「随便漏一份也不报错」。

    **簇世代的戳同样是硬要求**:依赖簇的四份必须带 ``built_at_cluster_seq`` 这个键
    (由 service 侧的 `stamp_cluster_seq` 盖),不依赖簇的 `relation_provenance` 必须
    **不带**。漏盖的表现极其隐蔽 —— 读侧只会把它报成「合并世代未知」、闸每次都判它不
    新鲜,于是这个库的预计算**永远**重跑,而没有任何报错;多盖则相反,给一份根本不依赖
    合并的产物编造一个落后量。两个方向都在这里拦。

    ⚠ 值的类型分两档(codex 第 7 轮 P2):两条统计快照当场从 `concept_clusters` 重算,
    世代永远是知道的,**必须是 int**;依赖板块的两份可以是 ``None``(= 板块划分是库里
    现成的、建在哪一代无从判断),但**键必须在** —— 允许「键干脆不写」就等于把「漏盖」
    和「盖不出来」揉成一档,上面那道守卫当场作废。
    """
    unknown = sorted(set(payloads) - _ARTIFACT_KINDS)
    if unknown:
        raise ValueError(
            f"KG 分析产物账本:契约外的 kind {unknown};"
            f"合法取值见 app.domain.kg_analysis_contracts.ARTIFACT_KINDS"
        )
    missing = sorted(REQUIRED_ARTIFACT_KINDS - set(payloads))
    if missing:
        raise ValueError(
            f"KG 分析产物账本:缺少必需的 kind {missing}。账本是整批重写的,"
            "少写一行 = 下游把它读成「从来没算过」,而且不会有任何报错"
        )
    if ARTIFACT_SOURCE_PROFILES not in payloads:
        boards = int(payloads[ARTIFACT_COMMUNITY_EDGES].get("communities", 0) or 0)
        if boards:
            raise ValueError(
                f"KG 分析产物账本:{ARTIFACT_SOURCE_PROFILES} 只有在一个主题板块都没有时"
                f"才允许缺席,但这一轮有 {boards} 个板块"
            )
    for kind in sorted(payloads):
        payload = payloads[kind]
        stamped = CLUSTER_SEQ_PAYLOAD_KEY in payload
        depends = kind in CLUSTER_DEPENDENT_ARTIFACT_KINDS
        known = artifact_cluster_seq(kind, payload) is not None
        unknown = artifact_cluster_seq_is_unknown(kind, payload)
        if depends and not (known or unknown):
            raise ValueError(
                f"KG 分析产物账本:{kind} 依赖合并结果,payload 必须带 "
                f"{CLUSTER_SEQ_PAYLOAD_KEY}(见 stamp_cluster_seq):两条统计快照是整数,"
                "依赖板块的两份可以是 None(板块划分建在哪一代无从判断)但键必须在。"
                "漏盖不会报错,只会让这个库的预计算永远重跑、读侧永远报「合并世代未知」"
            )
        if not depends and stamped:
            raise ValueError(
                f"KG 分析产物账本:{kind} 不依赖合并结果,不该带 "
                f"{CLUSTER_SEQ_PAYLOAD_KEY} —— 盖了它,读侧就会替一份输入根本没变的"
                "产物编造一个落后量"
            )


def batched(rows: Iterable[tuple], size: int) -> Iterator[List[tuple]]:
    """把一个可迭代的落库行流切成定长批 —— 供两侧 store 的分批 `executemany` 共用。

    存在的理由是**不再物化第二份完整列表**:store 拿到的行本身可能已是一份有界物化
    (见 `CrossCommunityEdgeFolder.top_edges`),再 `[... for ... in edges]` 一遍就是
    在落库那一刻又多一份同样大的拷贝。
    """
    batch: List[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
