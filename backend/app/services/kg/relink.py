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
    """读 element_ids 为 set(容忍 None / 任意 iterable)。"""
    raw = node.get("element_ids") or ()
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


def _name_matches(concept_name: str, text: str) -> bool:
    """概念名是否以**词边界**命中 text(大小写不敏感)+ 防误链护栏。

    护栏:归一后长度 ≥ 4;若为单 token 则要求 ≥ 6;命中通用停用表则拒。
    """
    norm = _norm(concept_name)
    if len(norm) < 4:
        return False
    if norm in _GENERIC_STOPLIST:
        return False
    if " " not in norm and len(norm) < 6:   # 单 token 需更长以防泛化误链
        return False
    # 词边界短语匹配:把概念名的 token 用 \W+ 连接,首尾加 \b。
    tokens = [re.escape(t) for t in norm.split(" ") if t]
    if not tokens:
        return False
    pattern = r"\b" + r"\W+".join(tokens) + r"\b"
    return re.search(pattern, text or "", re.IGNORECASE) is not None


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
    existing_pairs: set = set()        # 无向对,用于反向去重
    for src, tgt in edges:
        connected.add(src)
        connected.add(tgt)
        existing_pairs.add(frozenset((src, tgt)))

    # 同源索引:source_id → 该源下的节点列表(候选只在源内找)。
    by_source: Dict[str, List[Dict]] = {}
    for n in nodes:
        by_source.setdefault(n.get("source_id"), []).append(n)

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
        if frozenset((src, tgt)) in existing_pairs:   # 有向去重:反向已存在亦跳过
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

    # 孤立节点保持输入顺序处理,产出稳定。
    for n in nodes:
        if n["id"] in connected:
            continue

        emitted_for_n = 0
        n_elems = _element_ids(n)
        siblings = by_source.get(n.get("source_id"), ())

        # --- Rule 1: 共享证据元素 ---
        if n_elems:
            candidates: List[Tuple[int, int, int, str]] = []  # (shared, concept?, namelen, id)
            for m in siblings:
                if m["id"] == n["id"]:
                    continue
                overlap = len(n_elems & _element_ids(m))
                if overlap <= 0:
                    continue
                if _shared_edge(n, m) is None:        # 无安全边类型 → 不作候选
                    continue
                candidates.append((
                    overlap,                          # 共享越多越优先
                    1 if m["object_type"] == "concept" else 0,  # 再偏好 concept
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
            # 概念名长者优先(更具体),稳定 tie-break 用 id。
            concept_cands = sorted(
                (m for m in siblings
                 if m["id"] != n["id"] and m["object_type"] == "concept"),
                key=lambda m: (-len(m.get("name") or ""), m["id"]),
            )
            for m in concept_cands:
                if _at_cap(n["id"]):
                    break
                if _name_matches(m.get("name") or "", text):
                    if _try_emit(n["id"], m["id"], _ABOUT, _BASIS_NAME):
                        emitted_for_n += 1

    return new_edges
