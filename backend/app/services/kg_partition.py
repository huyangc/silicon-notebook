"""主题板块划分:后端中性的纯计算模块(规格 2026-10-19 §五 S2,计划 P4-T9)。

承 `docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md` §五 S2 的方案 C。
本模块只做计算:不碰连接、不拼 SQL、不判 dialect、不铸 `communities.id`。接入
`KnowledgeLifecycleService.rebuild_communities` 是 P4-T10 的事。

为什么要换掉今天的划分
----------------------
今天的划分是「整图、节点按关系 id 的首次出现序编号、Louvain、seed=42」。原样重跑逐字
相同,但**对输入不连续**:删一篇与主连通块零连接的孤岛来源,节点下标整体移位、随机访问
顺序跟着全变,主连通块 80 个板块只剩 18 个原样(规格 §五 S2 的实测表)。本模块把划分
改成下面这个函数:

    (当前 canonical 图, 可选的上一代划分) → 板块

算法
----
1. **节点按 canonical id 排名**(Python 字符串序,与 `sorted(members)` 及 PG 的
   ``COLLATE "C"`` 同序)。输入列表的顺序、边的到达顺序、边的方向都不进入结果。
2. **连通分量逐个跑。** 分量按「最小成员的排名」排序;分量内节点按排名编号,边按
   (lo, hi) 排序。
3. 每个分量跑 igraph ``community_leiden(objective_function="modularity", weights=…,
   resolution=m_c / m, initial_membership=…)``,一轮一轮地跑到不动点:
   · ``resolution = 分量边权 / 全图边权`` —— 这样每个分量优化的正是**全图模块度**在该
     分量上的那一项(igraph 的 modularity 目标在内部把 resolution 除以分量自己的
     2·m_c,于是惩罚项回到 k_i·k_j / 2m)。取 1 会让孤岛按「自己是全世界」切碎(规格
     表里 B' 那一行:模块度掉 0.03)。
   · **每一轮开跑前把 RNG 重置为 seed=42**:分量之间、轮与轮之间都不共享随机序列,增删
     一个分量不会改动别的分量拿到的随机数。
   · 冷启动第一轮单点起步;热启动第一轮用上一代的板块,上一代不在任何板块里的节点单点
     起步。
   · 每个起点都先归一成**起点形式**(`_restart_from`):每个社区按自己内部的边拆成连通的
     几段,不足 ``min_size`` 的段拆成单点,再按首次出现序编号。所以发布出去的板块一定
     内部连通。
   · 之后每一轮从上一轮的结果(同样归一)起步;某一轮原样回到起点,或者模块度抬高不超过
     `LEIDEN_MIN_ROUND_GAIN`、又没有在模块度不降的前提下让更多节点进入板块,就作废这一轮、
     输出它的起点(那段说明写了为什么不用 igraph 的 ``n_iterations=-1``)。于是「以本次
     发布结果热启动」在构造上就是不动点,冷启动的输出也不例外。
   · 总边权 m_c 满足 m_c² < 2m 的分量直接整块成一个社区,不调 igraph:那是可证的唯一
     最优(`_whole_component_is_optimal`),生产上绝大多数孤岛都在这一档。
4. 尺寸 < ``min_size`` 的分量**不跑算法**,其节点不在任何板块里。与今天「先跑 Louvain
   再按 ``community_min_size`` 过滤」在**保留下来的板块上逐个等价**:模块度优化产出的
   社区从不跨连通分量(把一个节点挪进与它零连接的社区,增益恒为负),所以小于
   ``min_size`` 的分量只可能产出小于 ``min_size`` 的社区,今天它们也全被过滤掉。分量
   内部被切出来的小社区照旧过滤,与今天同口径。孤立节点(无边)只有在
   ``min_size <= 1`` 时才成板块,不调算法。
5. 板块按「最小成员的 canonical id」排序输出;成员按 canonical id 升序。

确定性契约(取代原设计 §3.32 的「Louvain 输出逐字不变」)
-------------------------------------------------------
**同一输入图 + 同一上一代划分 → 逐字相同的输出。** 输入边的到达顺序、方向、节点列表
的顺序、上一代划分的流式到达顺序、上一代 board_key 的拼写,都不影响板块的成员划分;
board_key 只取决于成员集合与上一代的 board_key。热启动从自己的输出起步是不动点
(用例钉住,真实库上实测见下)。

代价是**依赖历史**:划分是「当前图 + 上一代划分」的函数。冷启动与增量演化出来的
划分不同。何时冷启动由调用方按规格判据决定(没有已发布划分 / 已发布划分的
`PARTITION_ALGO_VERSION` 与当前不同 / 显式 ``--cold-partition``),⚠ **不能以 ``force``
为判据**——每次真正的重新合并都以 ``force=True`` 调 `rebuild_communities`,照那个判据热启动
永远不会发生。

调用方须知:``min_size`` 必须 >= 1(否则 ``ValueError``)。``COMMUNITY_MIN_SIZE`` 今天没有
下界、设 0 也能跑,所以 P4-T10 接线时要先钳位到 >= 1。

networkx 回退
-------------
没装 igraph 时(它是 ``requirements.txt`` 的依赖,只在缺包时出现),保持今天的冷启动
Louvain:整图、``louvain_communities(weight="weight", seed=42)``、resolution 1,
``mode = "cold_fallback"``,**不热启动、不假装稳定**。唯一的差别是节点与边按本模块的
排名序喂入,而不是首次出现序——回退路径也因此与到达顺序无关。board_key 续用照常做:
它只比较成员集合,与划分怎么来的无关。

board_key 续用
--------------
`communities.id` 每一代重铸、全库唯一(`community_members` 的主键与同步键要求它),所以
跨代身份落在新增的 `communities.board_key` 上(规格 §五 S2 最后一段):

· 交集:对(新板块,旧板块)复合键排序计数——不建按板块的成员集合;
· 新板块 b 的最佳旧板块 = Jaccard 最大者(并列取旧 key 字典序最小);旧板块 o 的最佳
  新板块 = Jaccard 最大者(并列取最小成员更小的那个);**互为最佳且 J ≥ 1/2** 时沿用
  旧 key。J 用整数交叉相乘比较,没有浮点边界问题;旧板块的大小按它的**全部**成员
  计(包括已不在当前图里的 canonical),所以删掉一半成员的板块 J 恰好 1/2。
· 否则新铸:成员 canonical id 升序序列的 sha256(`mint_board_key`)。代内撞键(新铸的
  与沿用的、或新铸的彼此)时按板块序依次加盐再散列,确定性。

内存模型(生产量级:171 万节点 / 836 万边;本段的 MB 数是**推算**)
-------------------------------------------------------------------
输入是调用方已有的列式边表(三个 int32 数组,约 100 MB),本模块**不改它**。工作集:

· 节点排名与分量布局:排名靠一次 ``sorted(range(n), key=…)``(Python 整数列表,临时约
  60 MB),之后每节点几个 int32/int64 数组,峰值约 100 MB,常驻约 30 MB。
· 连通分量:scipy ``connected_components`` 内部把邻接转成 float64 CSR 并做一次转置,
  临时约 200 MB,返回前释放。
· 边按(分量, lo, hi)排序:int64 复合键 + int64 排序下标 + 排好的键,峰值约 200 MB;
  之后只留三列 int32(lo、hi 换成分量内局部下标;边权),约 100 MB。**不为每个分量复制
  整张边表**:每个分量只拿这三列上的一段切片。
· igraph:每个分量单独建图(``Graph(n); add_edges(ndarray)``),跑完当场释放。峰值落在
  主连通块的 Leiden 上。每一轮归一起点时对「两端同社区的边」求一次连通段(scipy,临时
  约「同社区边数 × 十几字节」)。
· 上一代划分:每节点一个 int32 标签(约 6.8 MB)+ 每个旧板块一个 key 与大小;续用的
  交集计数是一次复合键排序(约 14 MB)。

实测(2026-10-19,本机 macOS / igraph 1.0.0;每个变体一个新进程,输入先载入,数字是
调用期间相对起点的 RSS 峰值增量;「今天」= `rebuild_communities` 现行 igraph 分支原样照抄):

· **合成**的生产规模图(不是生产数据:随机植入社区 + 孤岛,171 万节点 / 796 万唯一边 /
  边权和 1210 万,主连通块 125 万节点):今天 36.3–42.8 s、ru_maxrss 增长 1.56–1.64 GB;
  冷启动 22.9–23.0 s、1.54–1.75 GB;删一个社区里的 27 个节点再加删一个孤岛之后热启动
  7.3 s、1.20 GB。同一删改下,今天 23.6% 的节点换板块,冷启动之间 26.6%,热启动 0%、
  45412/45413 个 board_key 续用(删掉的那个孤岛退役)。macOS 上跨进程的 RSS 有 ~100 MB
  量级的波动,冷启动与今天只能说同量级。
· 本机真实库 nb-b37185f4ae(3.6 万节点 / 4.9 万边;耗时与内存五次取中位,每次新进程):
  今天 0.17 s、+24–27 MB;冷启动 0.40–0.51 s、+15–17 MB;热启动 0.15 s、+9–11 MB。以冷启动
  结果为上一代热启动,主连通块里(今天三种删改都是 18/80 原样、16–18% 换板块):

  =====================  =========  ======  =====================  ======
  删改                   原样板块   换板块  board_key 续用 / 新铸  模块度
  =====================  =========  ======  =====================  ======
  删孤岛来源 LMCache     82/82      0%      1195 / 0               0.9425
  删孤岛来源 Titans      82/82      0%      1209 / 0               0.9428
  删重复来源 Mamba2_full 81/82      0.13%   1219 / 5               0.9427
  再把 Mamba2_full 加回  52/83      1.06%   1217 / 3               0.9430
  =====================  =========  ======  =====================  ======

  「加回」以删掉它之后的冷启动结果为上一代;它带回的概念并进已有板块时连带挪动,与
  「进板块」那半条无关(关掉那半条,输出逐字相同)。四个场景的输出都是热启动的不动点,
  内部不连通的板块 0 个(今天的划分里有 1 个)。模块度今天 0.9383–0.9396,冷启动 0.9431。

生产量级仍须在 §14③ 的只读演练里实测;合成图只说明量级与趋势,不得当生产实测引用。

线程与随机数
------------
igraph 的随机数源是进程级的全局设置。本模块用一个**专属**的 ``random.Random`` 实例,
在模块级锁里设置、每一轮 Leiden 前重置为 seed=42,跑完把 igraph 的随机数源还原成它的
默认值(``random`` 模块)。两个笔记本同时划分时互不干扰;不改动全局 ``random`` 的状态
(今天的 `rebuild_communities` 会 ``random.seed(42)`` 改掉它)。
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

PARTITION_MODE_WARM = "warm"
PARTITION_MODE_COLD = "cold"
PARTITION_MODE_COLD_FALLBACK = "cold_fallback"

# 每个连通分量开跑前把随机数源重置成这个种子;networkx 回退沿用今天的 seed=42。
LEIDEN_SEED = 42
LOUVAIN_FALLBACK_SEED = 42

# 每个分量逐轮跑 Leiden。一轮 = 一次 ``community_leiden(n_iterations=1)``,开跑前随机数重置为
# seed=42。每一轮的起点(以及热启动的第一个起点)都先归一成**起点形式**(`_restart_from`):
# 按「两端同一个社区的边」把每个社区拆成连通的几段 → 不足 min_size 的段拆成单点 → 按首次
# 出现序编号。这**正是**下一代以本次发布结果热启动时的起点(发布的板块都连通、都不小于
# min_size,再归一一次原样不变)。一轮的结果(同样归一之后)若原样回到起点,或者既没有把
# 分量的模块度抬高超过 `LEIDEN_MIN_ROUND_GAIN`、又没有「在模块度不降的前提下让更多节点进入
# 板块」,这一轮作废、输出它的起点;否则以它为下一轮的起点。作废与否只取决于(图, 起点),
# 所以下一代以本次输出热启动时,第一轮就做出同一个「作废」判断、原样输出:**构造上的不动点**
# (对任何确定性的作废规则都成立)。
#
# 拆不连通的社区必须在起点里做,不能放到输出前当后处理:上一代把只挂在 Y 上的 f1–f2 留在
# X 的板块里时,起点里拆开,这一代就能把它们并进 Y、下一代原样不变;后处理拆开则这一代
# 只把它们踢出板块,下一代才并进 Y——不动点就破了(用例钉住)。
#
# 为什么不用 igraph 自带的 ``n_iterations=-1``(「迭代到某一轮没有改动」):
#   1. **它可能不返回。** 4 个 K4 连成环、相邻两团之间两条权 6 的边,以「两两成对」为
#      ``initial_membership``:``n_iterations=-1`` 永不返回(本机 igraph 1.0.0 复现,卡在 C 层),
#      而 ``n_iterations=1/2/5/50`` 都立即返回同一个划分。推断是它判「有改动」看的不只是最终
#      划分(对称结构上精化每轮换一种子社区)。生产图里只要有一个这样的分量,整次重建就挂死。
#   2. **它的输出不是重置随机数之后的不动点。** 它停下的那一轮用的是续下去的随机序列;下一代
#      重置为 seed=42 热启动时,精化可能抽到不同的子社区(本机真实库:冷启动结果被热启动挪走
#      2 个节点)。
#
# 为什么要有增益下限,而不是只等「原样回到起点」:精化每轮随机抽子社区,大分量上几乎每轮都
# 能再找到一点点改进,每轮却要挪动成千上万个节点。合成的生产规模图(主连通块 125 万节点 /
# 738 万边,每轮 ~3 s)上,40 轮之后每轮仍有 ~5e-6 的增益、挪动数千个节点,「原样回到起点」
# 等不到。下限取 1e-4(分量模块度的万分之一):
#   · 合成图:第 1–3 轮增益 3.1e-2 / 2.5e-4 / 1.4e-4,第 4 轮 8.7e-5 作废 —— 4 次调用就停;
#   · 本机真实库主连通块(2.6 万节点):逐轮 9.3e-1 / 2.5e-3 / 6.4e-4 / 1.7e-4 / 1.6e-4 /
#     2.3e-4 / 2.7e-5 …,第 7 轮作废;比跑到「原样回到起点」(第 12 轮)少拿的模块度合计
#     约 4.6e-5。
# 「让更多节点进入板块」那半条是给增量新增的:新导入的几个概念挂到大分量上,摆放它们的模块度
# 增益可能远小于下限(一个节点 ~ 度数/分量边权),只看增益就会让它们一直停在板块之外。这半条
# 要求模块度**不降**:否则「多进板块、模块度下降」与「模块度回升、少进板块」两种轮次可以交替
# 被接受,来回振荡到轮数上限。
LEIDEN_MIN_ROUND_GAIN = 1e-4

# 轮数上限。被接受的轮次里模块度从不下降,靠下限被接受的那种每次至少抬高 1e-4,靠「进板块」
# 被接受的那种每次至少多进一个节点——所以不会振荡,但理论上界只是「模块度区间 / 下限 × 节点
# 数」这种量级,实际上合成图 4 次、真实库 7 次调用就停。触顶的分量数如实记进
# `BoardPartition.unstable_components`,不静默;主连通块触顶的代价约 64 × 一轮(合成图上一轮
# ~3 s)。
LEIDEN_MAX_ITERATIONS = 64

# board_key 续用的 Jaccard 下限(含):分子 / 分母,按整数交叉相乘比较。
BOARD_KEY_CARRY_JACCARD = (1, 2)

BOARD_KEY_PREFIX = "bk-"
# sha256 十六进制的前 24 位(96 bit)。代内撞键另有确定性再散列兜底。
BOARD_KEY_HEX_DIGITS = 24

# 分区算法版本:写进 `unified_kg_state.community_partition_version`(P4-T10)。与已发布划分的
# 版本不同就冷启动——上一代是另一套算法的产物,拿它热启动没有意义。上面任何一个影响划分或
# board_key 的常量、或者算法本身改了,都必须 +1;`tests/test_kg_partition.py` 把版本号与这些
# 常量的取值钉在一起,只改常量不改版本会红。
PARTITION_ALGO_VERSION = 1

_LABEL_DTYPE = np.int32
_NO_LABEL = -1

# igraph 的随机数源是进程级的:设置 + 逐轮重置 + 还原必须是一个临界区。
_IGRAPH_RNG_LOCK = threading.Lock()


@dataclass(frozen=True, eq=False)
class PreviousPartition:
    """上一代已发布的划分,按**当前**节点列表对齐(列式,不建 canonical→key 的 dict)。

    · ``keys[o]``:旧板块 o 的 board_key,两两不同;
    · ``sizes[o]``:旧板块 o 的**全部**成员数,包括已经不在当前图里的 canonical ——
      续用的 Jaccard 按它算;
    · ``label_of_node[i]``:当前第 i 个节点所在的旧板块,``-1`` 表示不在任何旧板块里。
    """

    keys: Tuple[str, ...]
    sizes: np.ndarray
    label_of_node: np.ndarray

    def __post_init__(self) -> None:
        sizes = np.asarray(self.sizes, dtype=np.int64)
        labels = np.asarray(self.label_of_node, dtype=_LABEL_DTYPE)
        if sizes.ndim != 1 or sizes.size != len(self.keys):
            raise ValueError("PreviousPartition: sizes 必须与 keys 一一对应")
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("PreviousPartition: 旧 board_key 必须两两不同")
        if labels.ndim != 1:
            raise ValueError("PreviousPartition: label_of_node 必须是一维")
        if labels.size and (labels.min() < _NO_LABEL or labels.max() >= len(self.keys)):
            raise ValueError("PreviousPartition: label_of_node 越界")
        in_graph = np.bincount(labels[labels >= 0], minlength=len(self.keys))
        if np.any(in_graph > sizes):
            raise ValueError("PreviousPartition: 旧板块在当前图里的成员多于它的总成员数")
        object.__setattr__(self, "sizes", sizes)
        object.__setattr__(self, "label_of_node", labels)

    @classmethod
    def from_mapping(
        cls, canonical_ids: Sequence[str], board_key_of: Mapping[str, str]
    ) -> "PreviousPartition":
        """由 canonical id → 旧 board_key 构造(测试与小库用;生产走 builder 流式读)。

        映射里不在 ``canonical_ids`` 中的 canonical 只计入旧板块的大小。
        """
        index_of = {c: i for i, c in enumerate(canonical_ids)}
        builder = PreviousPartitionBuilder(len(canonical_ids))
        for canonical_id in sorted(board_key_of):
            builder.add(index_of.get(canonical_id), board_key_of[canonical_id])
        return builder.build()


class PreviousPartitionBuilder:
    """流式攒一份 `PreviousPartition`:每读到一行(canonical, 旧 board_key)喂一次。

    调用方先把 canonical 映射成当前节点下标(不在当前图里就传 ``None``),本类只存每节点
    一个 int32 与每个旧板块一个计数。行的到达顺序不影响划分与续用的结果(旧板块的编号
    只是内部序号:热启动按分量内首次出现序重新编号,续用的并列按 key 字符串消歧)。
    """

    __slots__ = ("_labels", "_ordinal_of_key", "_sizes", "_built")

    def __init__(self, n_nodes: int) -> None:
        self._labels = np.full(int(n_nodes), _NO_LABEL, dtype=_LABEL_DTYPE)
        self._ordinal_of_key: Dict[str, int] = {}
        self._sizes: List[int] = []
        self._built = False

    def add(self, node_index: Optional[int], board_key: str) -> None:
        if self._built:
            raise RuntimeError("PreviousPartitionBuilder.build 之后不能再 add")
        ordinal = self._ordinal_of_key.get(board_key)
        if ordinal is None:
            ordinal = len(self._sizes)
            self._ordinal_of_key[board_key] = ordinal
            self._sizes.append(0)
        self._sizes[ordinal] += 1
        if node_index is None:
            return
        if not 0 <= node_index < self._labels.size:
            # 负下标会被 numpy 静默当成「从末尾数」——`can2idx.get(c, -1)` 的 -1 就会把旧板块
            # 标到最后一个节点上。不在当前图里请传 None。
            raise ValueError(
                f"PreviousPartitionBuilder: 节点下标 {node_index} 越界(共 {self._labels.size} 个)")
        if self._labels[node_index] != _NO_LABEL:
            # 同一代里一个 canonical 只能在一个板块里;出现两次说明读到的不是一代划分。
            raise ValueError(
                f"PreviousPartitionBuilder: 节点 {node_index} 在上一代划分里出现了两次")
        self._labels[node_index] = ordinal

    def build(self) -> PreviousPartition:
        if self._built:
            raise RuntimeError("PreviousPartitionBuilder.build 只能调一次")
        self._built = True
        keys = [""] * len(self._sizes)
        for key, ordinal in self._ordinal_of_key.items():
            keys[ordinal] = key
        self._ordinal_of_key.clear()
        return PreviousPartition(
            keys=tuple(keys),
            sizes=np.asarray(self._sizes, dtype=np.int64),
            label_of_node=self._labels,
        )


@dataclass(frozen=True)
class Board:
    """一个保留下来的板块(成员数 >= ``min_size``)。"""

    key: str
    members: Tuple[str, ...]   # canonical id 升序
    carried: bool              # True:沿用了上一代的 board_key


@dataclass(frozen=True, eq=False)
class BoardPartition:
    """`partition_boards` 的输出。数组都按**输入**节点列表对齐。

    · ``boards``:按最小成员的 canonical id 排序;
    · ``board_of_node[i]``:第 i 个节点所在板块在 ``boards`` 里的下标,``-1`` 表示不在任何
      保留板块里(所在分量或社区小于 ``min_size``);
    · ``degree[i]``:第 i 个节点在唯一边图上的邻居数(无权),与今天 igraph / networkx
      的 ``degree()`` 同口径,是 `community_members.centrality` 的来源;
    · ``carried`` / ``minted``:沿用 / 新铸 board_key 的板块数;``rehashed``:新铸时撞键、
      再散列过的板块数;``previous_boards``:上一代的板块数(没有上一代为 0);
    · ``components``:连通分量数;``components_skipped``:因小于 ``min_size`` 没有跑
      算法的分量数(networkx 回退整图跑,恒为 0);``unstable_components``:Leiden 轮数
      触顶(`LEIDEN_MAX_ITERATIONS`)仍未到不动点的分量数(正常为 0;networkx 回退恒为 0)。
    """

    mode: str
    boards: Tuple[Board, ...]
    board_of_node: np.ndarray
    degree: np.ndarray
    carried: int
    minted: int
    rehashed: int
    previous_boards: int
    components: int
    components_skipped: int
    unstable_components: int


def mint_board_key(members: Sequence[str], attempt: int = 0) -> str:
    """新铸 board_key:成员 canonical id 升序序列的稳定哈希。

    JSON(``ensure_ascii``)编码让序列的切分无歧义、与平台无关;``attempt > 0`` 是代内
    撞键时的确定性再散列。
    """
    payload = json.dumps(list(members), ensure_ascii=True, separators=(",", ":"))
    if attempt:
        payload = f"{payload}#{int(attempt)}"
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
    return BOARD_KEY_PREFIX + digest[:BOARD_KEY_HEX_DIGITS]


def partition_boards(
    canonical_ids: Sequence[str],
    src: np.ndarray,
    dst: np.ndarray,
    weight: np.ndarray,
    *,
    min_size: int,
    previous: Optional[PreviousPartition] = None,
    warm: bool = False,
) -> BoardPartition:
    """划分主题板块。

    ``canonical_ids[i]`` 是节点 i;``(src[k], dst[k], weight[k])`` 是一条无向边(与
    `CanonicalEdgeTable` 同形态:int32、节点下标、权重 > 0;方向与重复不限,重复的边
    权重相加)。``warm=True`` 时以 ``previous`` 热启动;``previous`` 给了而 ``warm=False``
    时冷启动,只用它续用 board_key。没装 igraph 时一律走 networkx 回退
    (``mode="cold_fallback"``)。

    ``min_size < 1`` 抛 ``ValueError``。⚠ ``COMMUNITY_MIN_SIZE``(`config.py`)今天没有下界、
    设 0 也能跑:调用方(P4-T10 的 `rebuild_communities`)负责把它钳位到 >= 1 再传进来。
    """
    min_size = int(min_size)
    if min_size < 1:
        raise ValueError("partition_boards: min_size 必须 >= 1")
    if warm and previous is None:
        raise ValueError("partition_boards: 热启动需要上一代划分")
    n = len(canonical_ids)
    src, dst, weight = _checked_edges(src, dst, weight, n)
    if previous is not None and previous.label_of_node.size != n:
        raise ValueError("partition_boards: 上一代划分没有按当前节点列表对齐")
    igraph = _import_igraph()
    layout = _NodeLayout.build(canonical_ids, src, dst)
    edges = _ComponentEdges.build(layout, src, dst, weight)
    degree_of_pos = edges.degree(n)
    if igraph is None:
        mode = PARTITION_MODE_COLD_FALLBACK
        community_of_pos = _louvain_fallback_communities(n, edges)
        components_skipped = unstable = 0
    else:
        mode = PARTITION_MODE_WARM if warm else PARTITION_MODE_COLD
        warm_labels = (
            previous.label_of_node[layout.node_of_pos] if warm else None
        )
        edges.localize(layout)
        community_of_pos, components_skipped, unstable = _leiden_communities(
            igraph, layout, edges, warm_labels, min_size)
    del edges
    boards_pos, board_of_pos = _kept_boards(community_of_pos, layout, min_size)
    del community_of_pos
    members = [_member_ids(canonical_ids, layout, positions) for positions in boards_pos]
    keys, carried, rehashed = _board_keys(members, layout, boards_pos, board_of_pos, previous)
    del boards_pos
    boards = tuple(
        Board(key=keys[b], members=members[b], carried=carried[b])
        for b in range(len(members))
    )
    del members
    board_of_node = np.empty(n, dtype=_LABEL_DTYPE)
    board_of_node[layout.node_of_pos] = board_of_pos
    degree = np.empty(n, dtype=np.int32)
    degree[layout.node_of_pos] = degree_of_pos
    n_carried = int(sum(carried))
    return BoardPartition(
        mode=mode,
        boards=boards,
        board_of_node=board_of_node,
        degree=degree,
        carried=n_carried,
        minted=len(boards) - n_carried,
        rehashed=rehashed,
        previous_boards=0 if previous is None else len(previous.keys),
        components=layout.n_components,
        components_skipped=components_skipped,
        unstable_components=unstable,
    )


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _import_igraph():
    """与 `rebuild_communities` 同一个探测口径:导入失败(含 ``sys.modules`` 置 None)即回退。"""
    try:
        import igraph
    except Exception:  # noqa: BLE001 — 缺包、坏包都按「没有 igraph」处理
        return None
    return igraph


def _checked_edges(src, dst, weight, n: int):
    src = np.asarray(src)
    dst = np.asarray(dst)
    weight = np.asarray(weight)
    if src.ndim != 1 or src.shape != dst.shape or src.shape != weight.shape:
        raise ValueError("partition_boards: src / dst / weight 必须是等长的一维数组")
    for column in (src, dst, weight):
        if column.size and not np.issubdtype(column.dtype, np.integer):
            raise TypeError("partition_boards: 边表必须是整数数组")
    if src.size:
        if min(int(src.min()), int(dst.min())) < 0 or max(int(src.max()), int(dst.max())) >= n:
            raise ValueError("partition_boards: 边的端点越界")
        if bool(np.any(src == dst)):
            raise ValueError("partition_boards: 边表里不能有自环")
        if int(weight.min()) <= 0:
            raise ValueError("partition_boards: 边权必须 > 0")
    return src, dst, weight


def _first_appearance_labels(codes: "np.ndarray") -> "np.ndarray":
    """把任意整数标签重编成 0..k-1,编号 = 该标签第一次出现的位置序。"""
    if codes.size == 0:
        return np.empty(0, dtype=np.int64)
    _, first, inverse = np.unique(codes, return_index=True, return_inverse=True)
    rank = np.empty(first.size, dtype=np.int64)
    rank[np.argsort(first)] = np.arange(first.size, dtype=np.int64)
    return rank[inverse.reshape(-1)]


def _initial_membership(labels: "np.ndarray") -> List[int]:
    """热启动的 ``initial_membership``:``labels`` 里 ``-1`` 的节点各自单点起步。

    按分量内首次出现序编号,所以结果只取决于「哪些节点同组」,与旧板块的序号、key 的
    拼写都无关。热启动的第一轮与之后每一轮共用这一个函数——两者算出的起点必须逐字
    相同,「某一轮原样返回起点 ⟺ 下一代热启动是不动点」才成立。
    """
    codes = labels.astype(np.int64)
    fresh = codes < 0
    codes[fresh] = -1 - np.flatnonzero(fresh)
    return _first_appearance_labels(codes).tolist()


class _NodeLayout:
    """节点的三套下标:输入下标 i、canonical 排名 r、分量布局位置 p。

    布局位置 = 先按分量(分量按最小成员的排名排序),再按排名。于是一个分量是一段连续
    的位置 ``[comp_start[c], comp_start[c + 1])``,段内的位置顺序就是 canonical id 顺序。
    """

    __slots__ = ("n", "rank_of_pos", "node_of_pos", "pos_of_node", "comp_of_pos",
                 "comp_start", "n_components")

    @classmethod
    def build(cls, canonical_ids: Sequence[str], src, dst) -> "_NodeLayout":
        self = cls()
        n = len(canonical_ids)
        self.n = n
        node_of_rank = np.fromiter(
            sorted(range(n), key=canonical_ids.__getitem__), dtype=np.int64, count=n)
        previous_id = None
        for i in node_of_rank.tolist():
            current = canonical_ids[i]
            if current == previous_id:
                raise ValueError(f"partition_boards: canonical id 重复:{current!r}")
            previous_id = current
        rank_of_node = np.empty(n, dtype=np.int64)
        rank_of_node[node_of_rank] = np.arange(n, dtype=np.int64)
        n_components, comp_of_node = _connected_components(src, dst, n)
        # 分量按「最小成员的排名」重新编号——不依赖 scipy 的遍历序。按排名序走一遍,
        # 每个分量第一次出现的位置就是它最小成员的排名。
        _, first_rank = np.unique(comp_of_node[node_of_rank], return_index=True)
        del node_of_rank
        renumber = np.empty(n_components, dtype=np.int64)
        renumber[np.argsort(first_rank)] = np.arange(n_components, dtype=np.int64)
        comp_of_node = renumber[comp_of_node]
        del first_rank, renumber
        order_key = comp_of_node * max(n, 1) + rank_of_node
        node_of_pos = np.argsort(order_key).astype(np.int32)
        del order_key
        self.node_of_pos = node_of_pos
        self.rank_of_pos = rank_of_node[node_of_pos].astype(np.int32)
        del rank_of_node
        self.pos_of_node = np.empty(n, dtype=np.int32)
        self.pos_of_node[node_of_pos] = np.arange(n, dtype=np.int32)
        self.comp_of_pos = comp_of_node[node_of_pos].astype(np.int32)
        del comp_of_node
        sizes = np.bincount(self.comp_of_pos, minlength=n_components)
        self.comp_start = np.zeros(n_components + 1, dtype=np.int64)
        np.cumsum(sizes, out=self.comp_start[1:])
        self.n_components = int(n_components)
        return self


def _connected_components(src, dst, n: int) -> Tuple[int, "np.ndarray"]:
    if n == 0:
        return 0, np.empty(0, dtype=np.int64)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    adjacency = coo_matrix(
        (np.ones(src.size, dtype=np.int8), (src, dst)), shape=(n, n))
    count, labels = connected_components(adjacency, directed=False)
    del adjacency
    return int(count), labels.astype(np.int64)


class _ComponentEdges:
    """唯一边表,按(分量, lo, hi)排序;lo < hi,先是布局位置,`localize` 之后是分量内下标。"""

    __slots__ = ("lo", "hi", "weight", "edge_start", "comp_weight", "total_weight",
                 "localized")

    @classmethod
    def build(cls, layout: _NodeLayout, src, dst, weight) -> "_ComponentEdges":
        self = cls()
        n = layout.n
        self.localized = False
        if src.size == 0:
            self.lo = np.empty(0, dtype=np.int32)
            self.hi = np.empty(0, dtype=np.int32)
            self.weight = np.empty(0, dtype=np.int32)
            self.total_weight = 0
            self.edge_start = np.zeros(layout.n_components + 1, dtype=np.int64)
            self.comp_weight = np.zeros(layout.n_components, dtype=np.int64)
            return self
        a = layout.pos_of_node[src]
        b = layout.pos_of_node[dst]
        # 复合键 lo * n + hi(int64;n 上界 ~171 万 → 键上界 ~2.9e12)。同一分量内布局位置
        # 保持排名序,所以按键排序 = 按(分量, lo, hi)排序。
        key = np.minimum(a, b).astype(np.int64)
        key *= max(n, 1)
        key += np.maximum(a, b)
        del a, b
        order = np.argsort(key)
        key = key[order]
        w = weight[order]
        del order
        if key.size > 1 and bool(np.any(key[1:] == key[:-1])):
            # 重复的边(同一对节点出现多次):权重相加。`CanonicalEdgeTable` 本来就去过重,
            # 这一支只在调用方喂了未聚合的边时才走。
            is_start = np.empty(key.size, dtype=bool)
            is_start[0] = True
            np.not_equal(key[1:], key[:-1], out=is_start[1:])
            starts = np.flatnonzero(is_start)
            del is_start
            w = np.add.reduceat(w.astype(np.int64), starts)
            key = key[starts]
            del starts
        if int(w.max()) > np.iinfo(np.int32).max:
            raise ValueError("partition_boards: 聚合后的边权超出 int32")
        self.lo = np.empty(key.size, dtype=np.int32)
        self.hi = np.empty(key.size, dtype=np.int32)
        np.floor_divide(key, max(n, 1), out=self.lo, casting="unsafe")
        np.remainder(key, max(n, 1), out=self.hi, casting="unsafe")
        del key
        self.total_weight = int(w.sum(dtype=np.int64))
        self.weight = np.asarray(w, dtype=np.int32)
        del w
        edge_comp = layout.comp_of_pos[self.lo]
        counts = np.bincount(edge_comp, minlength=layout.n_components)
        # 分量边权和:float64 累加整数在 2^53 以内是精确的(总边权是关系行数,生产 ~836 万)。
        self.comp_weight = np.bincount(
            edge_comp, weights=self.weight, minlength=layout.n_components
        ).astype(np.int64)
        del edge_comp
        self.edge_start = np.zeros(layout.n_components + 1, dtype=np.int64)
        np.cumsum(counts, out=self.edge_start[1:])
        return self

    def degree(self, n: int) -> "np.ndarray":
        """每个布局位置的唯一邻居数(必须在 `localize` 之前调)。"""
        if self.localized:
            raise RuntimeError("_ComponentEdges.degree 必须在 localize 之前调")
        degree = np.bincount(self.lo, minlength=n)
        degree += np.bincount(self.hi, minlength=n)
        return degree.astype(np.int32)

    def localize(self, layout: _NodeLayout) -> None:
        """把端点从布局位置原地换成分量内下标(减去所在分量的起点)。逐段原地减,不分配
        与边表等长的临时数组。"""
        edge_start = self.edge_start.tolist()
        comp_start = layout.comp_start.tolist()
        for c in range(layout.n_components):
            e0, e1 = edge_start[c], edge_start[c + 1]
            base = comp_start[c]
            if e0 != e1 and base:
                self.lo[e0:e1] -= base
                self.hi[e0:e1] -= base
        self.localized = True


def _leiden_communities(igraph, layout: _NodeLayout, edges: _ComponentEdges,
                        warm_labels: Optional["np.ndarray"], min_size: int):
    """逐分量、逐轮跑 Leiden 到不动点(轮次规则见 `LEIDEN_MIN_ROUND_GAIN` 上方的说明)。

    返回(每个布局位置的社区号,没跑算法的分量数,轮数触顶的分量数)。
    """
    community_of_pos = np.full(layout.n, _NO_LABEL, dtype=_LABEL_DTYPE)
    total_weight = int(edges.total_weight)
    comp_start = layout.comp_start.tolist()
    edge_start = edges.edge_start.tolist()
    comp_weight = edges.comp_weight.tolist()
    next_label = 0
    skipped = 0
    unstable = 0
    rng = random.Random()
    with _IGRAPH_RNG_LOCK:
        igraph.set_random_number_generator(rng)
        try:
            for c in range(layout.n_components):
                n0, n1 = comp_start[c], comp_start[c + 1]
                size = n1 - n0
                if size < min_size:
                    skipped += 1
                    continue
                e0, e1 = edge_start[c], edge_start[c + 1]
                if e0 == e1 or _whole_component_is_optimal(comp_weight[c], total_weight):
                    # 孤立节点(只在 min_size <= 1 时走到这里),或总边权小到「整个分量一个
                    # 社区」是唯一最优的分量:不调算法,见 `_whole_component_is_optimal`。
                    community_of_pos[n0:n1] = next_label
                    next_label += 1
                    continue
                lo, hi = edges.lo[e0:e1], edges.hi[e0:e1]
                graph = igraph.Graph(n=size)
                graph.add_edges(np.column_stack((lo, hi)))
                component_weight = edges.weight[e0:e1]
                resolution = comp_weight[c] / total_weight

                # 冷启动从单点起步;热启动从上一代的板块起步。两者都先归一成起点形式——上一代
                # 的板块在当前图里可能已经断成几段,或者只剩不到 min_size 个成员。
                start = _restart_from(
                    np.arange(size, dtype=np.int64) if warm_labels is None
                    else warm_labels[n0:n1].astype(np.int64),
                    lo, hi, min_size)

                def quality(membership):
                    return graph.modularity(membership, weights=component_weight,
                                            resolution=resolution, directed=False)

                start_quality = quality(start)
                start_boarded = _boarded(start, min_size)
                for _ in range(LEIDEN_MAX_ITERATIONS):
                    rng.seed(LEIDEN_SEED)
                    nxt = _restart_from(np.asarray(graph.community_leiden(
                        objective_function="modularity",
                        weights=component_weight,
                        resolution=resolution,
                        n_iterations=1,
                        initial_membership=start,
                    ).membership, dtype=np.int64), lo, hi, min_size)
                    if nxt == start:
                        break
                    nxt_quality = quality(nxt)
                    nxt_boarded = _boarded(nxt, min_size)
                    gained = nxt_quality - start_quality > LEIDEN_MIN_ROUND_GAIN
                    boarded_more = (nxt_boarded > start_boarded
                                    and nxt_quality >= start_quality)
                    if not (gained or boarded_more):
                        break                      # 这一轮作废,输出它的起点
                    start, start_quality, start_boarded = nxt, nxt_quality, nxt_boarded
                else:
                    unstable += 1
                local = np.asarray(start, dtype=np.int64)
                del graph, quality, lo, hi
                community_of_pos[n0:n1] = local + next_label
                next_label += int(local.max()) + 1
        finally:
            igraph.set_random_number_generator(random)
    return community_of_pos, skipped, unstable


def _whole_component_is_optimal(component_weight: int, total_weight: int) -> bool:
    """总边权 m_c 满足 m_c² < 2m 的连通分量,「整个分量一个社区」是模块度的唯一最优。

    证明:合并两个相邻社区 A、B 的模块度增益是 e_AB/m − d_A·d_B/(2m²)。边权是正整数,
    相邻 ⇒ e_AB ≥ 1;d_A + d_B ≤ 2m_c ⇒ d_A·d_B ≤ m_c²。于是增益 ≥ 1/m − m_c²/(2m²),
    在 m_c² < 2m 时严格为正:分量的任何多于一个社区的划分,都能靠合并一对相邻社区
    严格变好,直到只剩一个。每个分量的 Leiden 目标(resolution = m_c/m)正是全图模块度
    在该分量上的那一项,所以 Leiden 在这类分量上收敛到的也是它(用例逐个分量比对钉住)。

    这条只是省掉调用:生产 836 万边时界在 m_c < ~4089,绝大多数孤岛都落在这里,
    逐分量调 igraph 的固定开销(建图 + 至少一轮 Leiden + 两次模块度 + 重编号)随之省掉。
    本机真实库 1860 个分量里 1857 个落在这一档,逐个跑 Leiden 比对,全部是一个社区。
    整数比较,无浮点。
    """
    return component_weight * component_weight < 2 * total_weight


def _boarded(membership: List[int], min_size: int) -> int:
    """起点形式的划分里落在板块(成员数 >= min_size 的社区)里的节点数。"""
    labels = np.asarray(membership, dtype=np.int64)
    return int(np.count_nonzero(np.bincount(labels)[labels] >= min_size))


def _restart_from(labels: "np.ndarray", lo: "np.ndarray", hi: "np.ndarray",
                  min_size: int) -> List[int]:
    """把一个分量的划分归一成**起点形式**,顺序固定:

    1. 按「两端同一个社区的边」求连通段——每个社区拆成它连通的几段(``-1`` 的节点不属于
       任何社区,各自一段);
    2. 不足 ``min_size`` 的段拆成单点(它们不会被发布,下一代热启动时本来就是单点);
    3. 按首次出现序编号(`_initial_membership`)。

    幂等:发布出去的板块都连通、都不小于 min_size,再归一一次原样不变——下一代热启动的
    起点因此与本代停下时的起点逐字相同。``lo`` / ``hi`` 是本分量的局部边端点。
    """
    size = int(labels.size)
    same = labels[lo] == labels[hi]
    if size and int(labels.min()) < 0:
        same &= labels[lo] >= 0
    pieces = _connected_pieces(size, lo[same], hi[same])
    del same
    if min_size > 1:
        small = np.bincount(pieces)[pieces] < min_size
        if small.any():
            pieces[small] = _NO_LABEL
    return _initial_membership(pieces)


def _connected_pieces(size: int, lo: "np.ndarray", hi: "np.ndarray") -> "np.ndarray":
    """``size`` 个节点在边 (lo, hi) 下的连通段编号(int64)。"""
    if lo.size == 0:
        return np.arange(size, dtype=np.int64)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    adjacency = coo_matrix((np.ones(lo.size, dtype=np.int8), (lo, hi)), shape=(size, size))
    _, pieces = connected_components(adjacency, directed=False)
    return pieces.astype(np.int64)


def _louvain_fallback_communities(n: int, edges: _ComponentEdges) -> "np.ndarray":
    """networkx 回退:今天的整图冷启动 Louvain(seed=42),按布局位置喂入。"""
    import networkx as nx
    from networkx.algorithms.community import louvain_communities

    graph = nx.Graph()
    graph.add_nodes_from(range(n))
    graph.add_weighted_edges_from(
        zip(edges.lo.tolist(), edges.hi.tolist(), edges.weight.tolist()))
    communities = louvain_communities(graph, weight="weight", seed=LOUVAIN_FALLBACK_SEED)
    del graph
    community_of_pos = np.full(n, _NO_LABEL, dtype=_LABEL_DTYPE)
    for label, members in enumerate(communities):
        community_of_pos[np.fromiter(members, dtype=np.int64, count=len(members))] = label
    return community_of_pos


def _kept_boards(community_of_pos: "np.ndarray", layout: _NodeLayout, min_size: int):
    """保留成员数 >= min_size 的社区,按最小成员的排名排序。

    返回(每个板块的成员布局位置数组——段内按排名升序,每个布局位置的板块下标)。
    """
    n = layout.n
    board_of_pos = np.full(n, _NO_LABEL, dtype=_LABEL_DTYPE)
    labelled = np.flatnonzero(community_of_pos >= 0)
    if labelled.size == 0:
        return [], board_of_pos
    labels = community_of_pos[labelled]
    sizes = np.bincount(labels)
    keep = sizes[labels] >= min_size
    positions = labelled[keep]
    labels = labels[keep]
    del labelled, keep, sizes
    if positions.size == 0:
        return [], board_of_pos
    # 稳定排序:同一社区内保持布局位置序 = canonical 排名序(社区不跨分量)。
    order = np.argsort(labels, kind="stable")
    positions = positions[order]
    labels = labels[order]
    del order
    is_start = np.empty(labels.size, dtype=bool)
    is_start[0] = True
    np.not_equal(labels[1:], labels[:-1], out=is_start[1:])
    starts = np.flatnonzero(is_start)
    del is_start, labels
    ends = np.append(starts[1:], positions.size)
    first_rank = layout.rank_of_pos[positions[starts]]
    boards: List["np.ndarray"] = []
    for b, g in enumerate(np.argsort(first_rank).tolist()):
        members = positions[starts[g]:ends[g]]
        board_of_pos[members] = b
        boards.append(members)
    return boards, board_of_pos


def _member_ids(canonical_ids: Sequence[str], layout: _NodeLayout,
                positions: "np.ndarray") -> Tuple[str, ...]:
    return tuple(canonical_ids[i] for i in layout.node_of_pos[positions].tolist())


def _board_keys(members: List[Tuple[str, ...]], layout: _NodeLayout,
                boards_pos: List["np.ndarray"], board_of_pos: "np.ndarray",
                previous: Optional[PreviousPartition]):
    """续用或新铸每个板块的 board_key。返回(keys, carried 标志, 再散列过的板块数)。

    先占沿用的 key,再按板块序新铸:撞上已占的 key 就加盐重算,直到不撞。
    """
    n_boards = len(boards_pos)
    keys: List[Optional[str]] = [None] * n_boards
    carried = [False] * n_boards
    if previous is not None and n_boards and previous.keys:
        for b, o in _carry_matches(layout, boards_pos, board_of_pos, previous):
            keys[b] = previous.keys[o]
            carried[b] = True
    used = {key for key in keys if key is not None}
    rehashed = 0
    for b in range(n_boards):
        if keys[b] is not None:
            continue
        attempt = 0
        key = mint_board_key(members[b])
        while key in used:
            attempt += 1
            key = mint_board_key(members[b], attempt)
        if attempt:
            rehashed += 1
        used.add(key)
        keys[b] = key
    return keys, carried, rehashed


def _carry_matches(layout: _NodeLayout, boards_pos: List["np.ndarray"],
                   board_of_pos: "np.ndarray", previous: PreviousPartition):
    """(新板块, 旧板块)里互为最佳且 J >= 下限的那些对。"""
    old_of_pos = previous.label_of_node[layout.node_of_pos]
    both = (board_of_pos >= 0) & (old_of_pos >= 0)
    n_old = len(previous.keys)
    pair_key = board_of_pos[both].astype(np.int64) * n_old + old_of_pos[both]
    del both, old_of_pos
    pairs, inter = np.unique(pair_key, return_counts=True)
    del pair_key
    if pairs.size == 0:
        return []
    new_size = [int(members.size) for members in boards_pos]
    old_size = previous.sizes.tolist()
    keys = previous.keys
    best_old: Dict[int, Tuple[int, int, int]] = {}   # b -> (inter, union, o)
    best_new: Dict[int, Tuple[int, int, int]] = {}   # o -> (inter, union, b)
    for pair, i in zip(pairs.tolist(), inter.tolist()):
        b, o = divmod(pair, n_old)
        u = new_size[b] + old_size[o] - i
        current = best_old.get(b)
        if current is None or _jaccard_beats(i, u, current[0], current[1],
                                             keys[o] < keys[current[2]]):
            best_old[b] = (i, u, o)
        current = best_new.get(o)
        if current is None or _jaccard_beats(i, u, current[0], current[1], b < current[2]):
            best_new[o] = (i, u, b)
    numerator, denominator = BOARD_KEY_CARRY_JACCARD
    matches = []
    for b, (i, u, o) in best_old.items():
        if best_new[o][2] == b and i * denominator >= u * numerator:
            matches.append((b, o))
    return matches


def _jaccard_beats(inter: int, union: int, best_inter: int, best_union: int,
                   wins_tie: bool) -> bool:
    """inter/union 是否严格大于 best_inter/best_union;相等时看 ``wins_tie``。整数比较。"""
    left = inter * best_union
    right = best_inter * union
    return left > right or (left == right and wins_tie)
