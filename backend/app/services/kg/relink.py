"""Relink core — 确定性重连孤立(degree 0)KG 节点的纯函数(无 DB/IO/网络/LLM)。

约 22% 的节点因 gleaning 无边、首遍未被任何边引用、且边仅在窗口内产生而孤立。
用两类确定性信号在**同源(intra-source)**内补边:
  1. 共享证据元素(shared element_id)— 两节点的 evidence 引用了同一 source-element,
     说明它们在原文同处共现 → 真相关。
  2. name-in-text — 概念名以词边界出现在某 claim/formula 的文本里 → about。

输入契约(dict;调用方 Task 2 负责适配):
  node = {
    "id": str, "object_type": "concept"|"claim"|"formula"|"procedure"(小写),
    "name": str, "source_id": str, "element_ids": set[str] | iterable[str],
  }
  edges = iterable[(source_object_id, target_object_id)]  # 现存边的有向对
输出:list[新边 dict] {"source_object_id","target_object_id","edge_type","basis"},
  basis ∈ {"relink:shared-element","relink:name-match"},edge_type ∈ {"about","used_in"}。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

# relink 只产出这两种边(固定边词汇的安全子集)。
_ABOUT = "about"
_USED_IN = "used_in"
_BASIS_SHARED = "relink:shared-element"
_BASIS_NAME = "relink:name-match"

# rule-2 通用词停用表:这些概念名过于泛化,出现在 claim 文本里不构成真链接。
_GENERIC_STOPLIST = frozenset({
    "model", "system", "method", "value", "data", "result", "function",
    "process", "approach", "network", "layer", "input", "output",
})

# 名称归一:与 filters._norm 同口径(小写,空白/连字符/下划线塌缩为单空格)。
_WS_RE = re.compile(r"[\s\-_]+")


def _norm(name: str) -> str:
    return _WS_RE.sub(" ", (name or "").strip().lower())


def _element_ids(node: Dict) -> set:
    """读 element_ids 为 set(容忍 None / 任意 iterable)。

    已经是 set 的输入按引用返回而不复制:唯一消费方只做只读交集,而整库
    relink 路径本就把 element_ids 物化成了 set——再复制一份等于给受影响
    source 的证据索引留第二份全量拷贝。
    """
    raw = node.get("element_ids") or ()
    if isinstance(raw, set):
        return raw
    return set(raw)


def _shared_edge(n: Dict, m: Dict) -> Tuple[str, str, str] | None:
    """按两节点类型决定 rule-1 的边(有向 + 类型);无安全类型则 None。

    relink 只在「一端是 concept」时补边:
      claim/formula(N) ↔ concept(M)  → about,源头是 claim/formula
      procedure        ↔ concept     → used_in,concept → procedure
    concept↔concept、claim↔claim、claim↔formula 等(无 concept 或两端皆 concept)
    无安全边类型 → 跳过。
    """
    tn, tm = n["object_type"], m["object_type"]
    # about: claim/formula 是 about 的源头,指向 concept。
    if tn in ("claim", "formula") and tm == "concept":
        return (n["id"], m["id"], _ABOUT)
    if tn == "concept" and tm in ("claim", "formula"):
        return (m["id"], n["id"], _ABOUT)
    # used_in: concept used_in procedure(concept → procedure)。
    if tn == "procedure" and tm == "concept":
        return (m["id"], n["id"], _USED_IN)
    if tn == "concept" and tm == "procedure":
        return (n["id"], m["id"], _USED_IN)
    return None


def _name_pattern(concept_name: str) -> "re.Pattern[str] | None":
    """概念名的词边界匹配 pattern,已通过防误链护栏则返回**已编译**对象,否则
    None(护栏未过 → 该概念永不参与 rule-2,调用方按概念缓存本函数的结果,
    每个概念名只归一 + 编译一次,不随参与比较的候选节点数重复)。

    护栏:归一后长度 ≥ 4;若为单 token 则要求 ≥ 6;命中通用停用表则拒。
    """
    norm = _norm(concept_name)
    if len(norm) < 4:
        return None
    if norm in _GENERIC_STOPLIST:
        return None
    if " " not in norm and len(norm) < 6:   # 单 token 需更长以防泛化误链
        return None
    # 词边界短语匹配:把概念名的 token 用 \W+ 连接,首尾加 \b。
    tokens = [re.escape(t) for t in norm.split(" ") if t]
    if not tokens:
        return None
    pattern = r"\b" + r"\W+".join(tokens) + r"\b"
    return re.compile(pattern, re.IGNORECASE)


def _pair_key(a: str, b: str) -> Tuple[str, str]:
    """无向对的规范表示(顺序无关,两端排序后取元组);同一对无论以哪个方向
    到来都归一到同一个 key,供 existing-edge 去重按无向语义查找。"""
    return (a, b) if a <= b else (b, a)


def complete_isolated_edges(
    nodes: Iterable[Dict],
    edges: Iterable[Tuple[str, str]],
    *,
    max_per_node: int = 3,
    enable_name_match: bool = True,
) -> List[Dict]:
    """为孤立(degree 0)节点提议新边。纯函数,详见模块 docstring。"""
    nodes = list(nodes)
    by_id = {n["id"]: n for n in nodes}

    # degree: 出现在任一现存边(作为 src 或 tgt)的节点即非孤立。
    connected: set = set()
    existing_pairs: set = set()        # 无向对(_pair_key),用于反向去重
    for src, tgt in edges:
        connected.add(src)
        connected.add(tgt)
        existing_pairs.add(_pair_key(src, tgt))

    isolated_nodes = [n for n in nodes if n["id"] not in connected]
    if not isolated_nodes:
        # 稳态零预计算:relink 在每次抽取成功后自动跑,「LLM 已把全部节点连
        # 通、零孤立」是常态而非边缘形态——评审实测这条早退之前的「无条件三
        # 份预计算」在零孤立输入下比这条早退慢 28× 且多吃 70% 内存。不算
        # element_ids、不排序 rule-2 候选表、不编译 pattern,直接返回。
        return []

    # 只对「含至少一个孤立节点」的 source 做预计算——候选只在源内找,故只需
    # 覆盖这些 source 的全部节点(Rule-1 要查孤立节点的所有同源 sibling 的
    # element_ids;Rule-2 的候选表要覆盖同源全部 concept),未受影响 source
    # 的节点连 _element_ids 都不调用。
    affected_sources = {n.get("source_id") for n in isolated_nodes}
    by_source: Dict[str, List[Dict]] = {}
    for n in nodes:
        sid = n.get("source_id")
        if sid in affected_sources:
            by_source.setdefault(sid, []).append(n)

    # element_ids 惰性 memo:只有真被 Rule-1 触碰的节点才求值(孤立节点无
    # 证据时其 sibling 一个都不会被扫),且 _element_ids 对已是 set 的输入
    # 返回引用——两者合起来避免为受影响 source 保留第二份全量证据索引。
    _elem_cache: Dict[str, set] = {}

    def _elems(node: Dict) -> set:
        node_id = node["id"]
        cached = _elem_cache.get(node_id)
        if cached is None:
            cached = _element_ids(node)
            _elem_cache[node_id] = cached
        return cached

    # rule-2 候选表:每受影响 source 下按(名长降序、id 兜底)排序的 concept
    # 列表,与触发 rule-2 的具体 N 无关,故按 source 只排一次;`m["id"] != n["id"]`
    # 的自身排除挪到下面循环内对这份预排序表做过滤(过滤不改变相对顺序,
    # 与先排除再排序等价)。
    concept_cands_by_source: Dict[str, List[Dict]] = {
        sid: sorted(
            (m for m in siblings if m["object_type"] == "concept"),
            key=lambda m: (-len(m.get("name") or ""), m["id"]),
        )
        for sid, siblings in by_source.items()
    }

    # rule-2 pattern:改成首次真正被拿去 .search() 时才编译并缓存(而不是像
    # elem_ids/候选表那样对受影响 source 内全部 concept 无条件预算)——一个
    # source 里的候选 concept 常远多于实际会被测试到的(N 一旦命中或到
    # max_per_node 就 break),提前编译等于白付一次 re.compile。
    #
    # 不能写成 `concept_patterns.setdefault(mid, _name_pattern(name))`:
    # setdefault 的第二个参数是**调用时立即求值**的,那样 _name_pattern(连带
    # re.compile)仍会对每一对候选重新调用一次,只是多余的编译结果被静默丢
    # 弃——不起到防抖作用,反而会让 test_name_pattern_compiled_once_per_
    # concept_not_per_pair 那类"编译次数=概念数"的守卫失真。这里改用显式
    # "先查是否已算过、没算过才调用" 达到同样的惰性 memo 语义。
    concept_patterns: Dict[str, "re.Pattern[str] | None"] = {}

    def _pattern_for(node_id: str, name: str) -> "re.Pattern[str] | None":
        if node_id not in concept_patterns:
            concept_patterns[node_id] = _name_pattern(name)
        return concept_patterns[node_id]

    new_edges: List[Dict] = []
    emitted_keys: set = set()          # 新边自去重 (src,tgt,edge_type)
    # 每个节点 id 累计获得的 relink 边数。max_per_node 对边的**两端**都生效,
    # 故无论迭代顺序如何,任一节点的 relink 度数都不超过上限(一个被多篇 claim
    # 指向的热门 concept 也不会被灌爆)。
    degree_added: Dict[str, int] = {}

    def _at_cap(node_id: str) -> bool:
        return degree_added.get(node_id, 0) >= max_per_node

    def _try_emit(src: str, tgt: str, edge_type: str, basis: str) -> bool:
        """加一条新边;遇自环/与现存边(任一方向)/重复/任一端到顶则跳过。"""
        if src == tgt:
            return False
        if _at_cap(src) or _at_cap(tgt):
            return False
        key = (src, tgt, edge_type)
        if key in emitted_keys:
            return False
        if _pair_key(src, tgt) in existing_pairs:   # 无向去重:反向已存在亦跳过
            return False
        emitted_keys.add(key)
        degree_added[src] = degree_added.get(src, 0) + 1
        degree_added[tgt] = degree_added.get(tgt, 0) + 1
        new_edges.append({
            "source_object_id": src,
            "target_object_id": tgt,
            "edge_type": edge_type,
            "basis": basis,
        })
        return True

    # 孤立节点保持输入顺序处理,产出稳定(isolated_nodes 已是 nodes 的顺序子
    # 集,等价于原先「逐 n 现查 connected」但省一次成员测试)。
    for n in isolated_nodes:
        emitted_for_n = 0
        n_elems = _elems(n)
        siblings = by_source.get(n.get("source_id"), ())

        # --- Rule 1: 共享证据元素 ---
        if n_elems:
            candidates: List[Tuple[int, int, int, str]] = []  # (shared, concept?, namelen, id)
            for m in siblings:
                if m["id"] == n["id"]:
                    continue
                overlap = len(n_elems & _elems(m))
                if overlap <= 0:
                    continue
                if _shared_edge(n, m) is None:        # 无安全边类型 → 不作候选
                    continue
                candidates.append((
                    overlap,                          # 共享越多越优先
                    # 再偏好 concept——但该项在单一候选表内恒为常量:
                    # _shared_edge 已把合法 m 类型限定死(N 为 claim/formula/
                    # procedure 时唯一合法 m 是 concept;N 为 concept 时合法
                    # m 只能是 claim/formula/procedure、永不是 concept),故
                    # 同一个 N 的候选要么全 1 要么全 0,这项从不参与真实排
                    # 序,保留只为键形状(4 元组)稳定。
                    1 if m["object_type"] == "concept" else 0,
                    len(m.get("name") or ""),         # 再偏好更长名
                    m["id"],
                ))
            # 排序:共享数↓、concept 优先、名长↓;id 兜底稳定。
            candidates.sort(key=lambda c: (-c[0], -c[1], -c[2], c[3]))
            for _shared, _isc, _nlen, mid in candidates:
                if _at_cap(n["id"]):
                    break
                edge = _shared_edge(n, by_id[mid])
                if edge is None:
                    continue
                if _try_emit(edge[0], edge[1], edge[2], _BASIS_SHARED):
                    emitted_for_n += 1

        # --- Rule 2: name-in-text(仅当 N 仍孤立 + 启用 + N 为 claim/formula)---
        # 注意:concept 节点不发起 name-match——about 方向由 claim/formula 端驱动,
        # 故孤立的 concept 只能靠 rule-1 共享元素重连(rule-2 永不以 concept 为 N)。
        if (
            emitted_for_n == 0
            and enable_name_match
            and n["object_type"] in ("claim", "formula")
        ):
            text = n.get("name") or ""
            # 概念名长者优先(更具体),稳定 tie-break 用 id;取预排序表按本节点
            # 自身 id 过滤,过滤不改变剩余元素的相对顺序,与逐 N 现排等价。
            concept_cands = (
                m for m in concept_cands_by_source.get(n.get("source_id"), ())
                if m["id"] != n["id"]
            )
            for m in concept_cands:
                if _at_cap(n["id"]):
                    break
                pattern = _pattern_for(m["id"], m.get("name") or "")
                if pattern is not None and pattern.search(text) is not None:
                    if _try_emit(n["id"], m["id"], _ABOUT, _BASIS_NAME):
                        emitted_for_n += 1

    return new_edges
